"""In-worker monitoring for the box booking worker (Thomas, Nov 16 2026).

Replaces the billable assistant routines ("Wake booking worker",
"Unbooked sign-up audit"). Everything here runs inside the worker loop at no
usage cost; the assistant is woken (one batched AMYI webhook) only when a real
problem is found.

  * fast intake sweep (~15 min, 24/7): queued jobs and retry jobs whose due
    time has passed -> run a normal pickup right away (the slot schedule is
    now only a backstop). Two cheap GETs when idle.
  * self-health (with the sweep): CDP port down and no run in progress ->
    relaunch Chrome; flag a browser that will not come back.
  * stuck-sign-up audit (~30 min): one GET of the whole queue, then flags
      - queued jobs still unbooked after a pickup/sweep started after their
        creation, or older than 45 minutes;
      - manual "Worker error" (browser/connection/timeout/crash) jobs: requeued
        once automatically, flagged only when that fails or they fail again;
      - new (26h) manual / submission_uncertain / needs_review jobs whose night
        is today or later, minus expected holds;
      - claimed jobs whose lease expired or that have not updated in 30 min;
      - unrecovered stalls and a browser that will not restart.
    Double bookings and venue changes are never flagged.
  * alerting: one batched webhook per audit through amyi.post_alert, each
    ref+problem at most once per 6 hours (data/alert_dedupe.json). Without a
    webhook the batch goes to data/alerts.jsonl with a WARNING. Never email.

Dry run (no requeue, no webhook, no pickup, no state writes):
    python3 worker_monitor.py --dry-run
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import stall_watchdog

log = logging.getLogger("amy.monitor")
PHOENIX = ZoneInfo("America/Phoenix")

SWEEP_SECONDS = int(os.environ.get("AMY_WORKER_SWEEP_SECONDS", "900"))
AUDIT_SECONDS = int(os.environ.get("AMY_WORKER_AUDIT_SECONDS", "1800"))
STUCK_QUEUED_SECONDS = 45 * 60
LEASE_STALE_SECONDS = 30 * 60
NEW_EXCEPTION_SECONDS = 26 * 3600
DEDUPE_SECONDS = 6 * 3600
SWEEP_STATES = ("queued", "pending", "retry")      # 'pending' kept for API compatibility
RUNNABLE_STATES = ("queued", "pending")
EXCEPTION_STATES = ("manual", "submission_uncertain", "needs_review")
CLAIMED_STATES = ("assigned", "claimed", "running", "awaiting_human_verification")
DEFAULT_ALERT_ENV = "/workspace/amy-worker/alert.env"

# ZOUK left notify-only on Nov 24 2026 (worker sends its confirmation), so a
# held ZOUK job is a real exception again; Ghostbar stays notify-only.
NOTIFY_ONLY_RE = re.compile(r"\b(ghostbar|ghost bar)\b", re.I)
SCOTTSDALE_RE = re.compile(r"\b(riot house|almost famous|el hefe|cake)\b", re.I)
NOT_POSTED_RE = re.compile(r"guest list not posted|no guest list posted", re.I)
WORKER_ERROR_RE = re.compile(r"^\s*worker error\b", re.I)
IGNORED_REASON_RE = re.compile(
    r"past or invalid source date|owner stop|held shanequa/ashley|"
    r"duplicate|double[- ]book|already booked|venue change", re.I)
COMPLETION_REFUSED_RE = re.compile(r"booked and emailed but completion refused", re.I)


# --- small helpers ------------------------------------------------------------

def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def guest_name(raw):
    return (raw.get("name") or " ".join(x for x in (raw.get("first_name"), raw.get("last_name")) if x)
            or raw.get("email") or "?").strip()


def venues_of(raw):
    v = raw.get("venues") or []
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in v if x]


def last_night(raw):
    for key in ("end_date", "start_date"):
        try:
            return date.fromisoformat(str(raw.get(key))[:10])
        except (TypeError, ValueError):
            continue
    return None


def has_verified_bookings(meta):
    meta = meta or {}
    for ev in (meta.get("evidence"), (meta.get("resolution") or {}).get("evidence")):
        if any((b or {}).get("verified") for b in (ev or {}).get("bookings") or []):
            return True
    bookings = (meta.get("progress") or {}).get("bookings") or {}
    return any((b or {}).get("verified") or (b or {}).get("order_id") for b in bookings.values()
               if isinstance(b, dict))


def expected_hold(job):
    """True for manual/uncertain jobs that are known, expected holds."""
    raw, reason = job.get("raw") or {}, job.get("reason") or ""
    if NOT_POSTED_RE.search(reason) or IGNORED_REASON_RE.search(reason):
        return True
    venues = venues_of(raw)
    if venues and all(NOTIFY_ONLY_RE.search(v) or SCOTTSDALE_RE.search(v) for v in venues):
        return True
    if COMPLETION_REFUSED_RE.search(reason) and has_verified_bookings(job.get("metadata")):
        return True
    return False


def item(job, problem, reason, now, tried="", since=None):
    raw = job.get("raw") or {}
    start, end = raw.get("start_date"), raw.get("end_date")
    dates = start if not end or end == start else f"{start} to {end}"
    since = since if since is not None else job.get("created") or now
    return {
        "problem": problem,
        "ref": job.get("ref") or "",
        "guest": guest_name(raw),
        "venue": ", ".join(venues_of(raw)),
        "dates": dates or "",
        "female": raw.get("female_count"),
        "male": raw.get("male_count"),
        "state": job.get("state") or "",
        "reason": (reason or job.get("reason") or "")[:500],
        "minutes_sitting": int(max(0, now - float(since)) // 60),
        "worker_tried": tried,
    }


def system_item(problem, reason, tried="", since=None, now=None):
    now = now or time.time()
    return {"problem": problem, "ref": problem, "guest": "", "venue": "", "dates": "",
            "female": None, "male": None, "state": "worker", "reason": reason[:500],
            "minutes_sitting": int(max(0, now - (since or now)) // 60), "worker_tried": tried}


# --- alert env + delivery -----------------------------------------------------

def load_alert_env(path=None):
    """Read AMYI_WEBHOOK_URL/AUTH from the process env, else the alert env file
    (KEY=VALUE lines). Re-read on every send so a new file needs no restart."""
    url, auth = os.environ.get("AMYI_WEBHOOK_URL", ""), os.environ.get("AMYI_WEBHOOK_AUTH", "")
    if url:
        return url, auth
    path = Path(path or os.environ.get("AMY_ALERT_ENV", DEFAULT_ALERT_ENV))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return "", ""
    vals = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        try:  # same quoting rules as bash `source` (the writer uses printf %q)
            v = " ".join(shlex.split(v))
        except ValueError:
            pass
        vals[k] = v
    return vals.get("AMYI_WEBHOOK_URL", ""), vals.get("AMYI_WEBHOOK_AUTH", "")


def default_sender(payload, env_path=None):
    """Returns True (2xx), False (failed) or None (no webhook configured)."""
    url, auth = load_alert_env(env_path)
    if not url:
        return None
    import amyi
    return amyi.post_alert(payload, url=url, auth=auth)


class Dedupe:
    def __init__(self, path, window=DEDUPE_SECONDS):
        self.path, self.window = Path(path), window

    def _load(self, now):
        data = _read_json(self.path, {})
        return {k: float(v) for k, v in data.items() if now - float(v) < self.window}

    def fresh(self, items, now):
        seen = self._load(now)
        return [i for i in items if f"{i['ref']}|{i['problem']}" not in seen]

    def mark(self, items, now):
        seen = self._load(now)
        for i in items:
            seen[f"{i['ref']}|{i['problem']}"] = now
        _write_json(self.path, seen)


# --- monitor ------------------------------------------------------------------

class Monitor:
    def __init__(self, api, work_dir, *, clock=time.time, sender=None, dry_run=False,
                 sweep_every=SWEEP_SECONDS, audit_every=AUDIT_SECONDS, browser=None,
                 browser_plain=None):
        self.api, self.dir, self.clock, self.dry_run = api, Path(work_dir), clock, dry_run
        self.sender = sender or default_sender
        self.sweep_every, self.audit_every = sweep_every, audit_every
        self.browser = browser  # object with healthy()/ensure(); default browser_host
        self.browser_plain = (os.environ.get("AMY_BROWSER_LAUNCH", "").lower() == "plain"
                              if browser_plain is None else browser_plain)
        self.last_sweep = self.last_audit = 0.0
        self.dedupe = Dedupe(self.dir / "alert_dedupe.json")
        self.requeue_file = self.dir / "auto_requeue.json"
        self.alerts_file = self.dir / "alerts.jsonl"
        self.health_items = []

    # -- schedule --
    def tick(self, run_pickup, now=None):
        """Called every worker-loop step. run_pickup(reason) runs a normal pickup."""
        now = self.clock() if now is None else now
        if run_in_progress(self.dir):
            return None
        out = {}
        if now - self.last_sweep >= self.sweep_every:
            self.last_sweep = now
            out["sweep"] = self.sweep(run_pickup, now)
        if now - self.last_audit >= self.audit_every:
            self.last_audit = now
            out["audit"] = self.audit(now=self.clock())
        return out or None

    # -- sweep + health --
    def due_jobs(self, now):
        due = []
        for state in SWEEP_STATES:
            if state == "pending":
                continue  # not a server state today; listed for completeness
            code, body = self.api.jobs(state, 100)
            if code != 200:
                log.warning("SWEEP queue read %s HTTP %s", state, code)
                continue
            for job in (body or {}).get("jobs") or []:
                if float(job.get("due") or 0) <= now:
                    due.append(job)
        return due

    def check_browser(self, now):
        """CDP down and no run -> relaunch. Returns a flag item when it won't come back."""
        if not self.browser_plain:
            return None
        b = self.browser
        if b is None:
            import browser_host as b
        if b.healthy():
            return None
        log.warning("HEALTH booking Chrome not answering on CDP; relaunching")
        if self.dry_run:
            return system_item("browser_down", "Booking Chrome is not answering on CDP (dry run: not relaunched)",
                               now=now)
        try:
            b.ensure()
        except Exception as exc:  # noqa: BLE001
            log.exception("HEALTH Chrome relaunch failed")
            return system_item("browser_down", f"Chrome relaunch failed: {type(exc).__name__}",
                               tried="relaunched Chrome via browser_host.ensure()", now=now)
        if b.healthy():
            log.info("HEALTH booking Chrome relaunched OK")
            return None
        return system_item("browser_down", "Booking Chrome still not answering on CDP after relaunch",
                           tried="relaunched Chrome via browser_host.ensure()", now=now)

    def sweep(self, run_pickup, now):
        bad = self.check_browser(now)
        self.health_items = [bad] if bad else []
        try:
            due = self.due_jobs(now)
        except Exception:  # noqa: BLE001
            log.exception("SWEEP queue read failed")
            return {"status": "error"}
        refs = [j.get("ref") for j in due]
        if not due:
            log.info("SWEEP idle: no due queued/retry jobs")
            return {"status": "idle", "due": 0}
        log.info("SWEEP %d due job(s) %s; starting pickup now", len(due), ",".join(refs))
        if self.dry_run:
            return {"status": "would_pickup", "due": len(due), "refs": refs}
        result = run_pickup("sweep")
        return {"status": "pickup", "due": len(due), "refs": refs, "result_status": (result or {}).get("status")}

    # -- audit --
    def list_all(self):
        code, body = self.api._call("GET", "/queue/jobs?limit=1000", None, self.api.queue_token)
        if code != 200:
            raise RuntimeError(f"queue listing HTTP {code}")
        return (body or {}).get("jobs") or []

    def audit(self, jobs=None, now=None):
        now = self.clock() if now is None else now
        try:
            jobs = self.list_all() if jobs is None else jobs
        except Exception as exc:  # noqa: BLE001
            log.warning("AUDIT queue read failed: %s", exc)
            items = [system_item("queue_unreachable", f"Audit could not read the queue: {exc}", now=now)]
            return self._report(items, now)
        items = self.find_problems(jobs, now)
        return self._report(items, now)

    def find_problems(self, jobs, now):
        today = datetime.fromtimestamp(now, PHOENIX).date()
        last_pickup = read_last_pickup(self.dir)
        requeued = {k: v for k, v in _read_json(self.requeue_file, {}).items()
                    if now - float(v) < 7 * 86400}
        items = list(self.health_items)
        for job in jobs:
            state, raw = job.get("state") or "", job.get("raw") or {}
            created = float(job.get("created") or now)
            updated = float(job.get("updated") or created)
            reason = job.get("reason") or ""
            night = last_night(raw)
            ref = job.get("ref") or ""
            if state in RUNNABLE_STATES:
                if float(job.get("due") or 0) > now:
                    continue
                age = now - created
                after_pickup = last_pickup and last_pickup > created + 60
                if age > STUCK_QUEUED_SECONDS or after_pickup:
                    why = ("still queued after a pickup started" if after_pickup
                           else "queued more than 45 minutes")
                    items.append(item(job, "unbooked_queued", why, now,
                                      tried="pickup/sweep ran" if after_pickup else "waiting for pickup"))
            elif state in CLAIMED_STATES:
                lease = job.get("lease_until")
                if (lease and float(lease) < now) or now - updated > LEASE_STALE_SECONDS:
                    items.append(item(job, "claim_stale",
                                      "claimed job lease expired or no update in 30+ minutes", now,
                                      since=updated))
            elif state == "manual" and WORKER_ERROR_RE.search(reason):
                if night is None or night < today or now - updated > NEW_EXCEPTION_SECONDS:
                    continue
                items.extend(self._requeue_worker_error(job, requeued, now))
            elif state in EXCEPTION_STATES:
                if now - created > NEW_EXCEPTION_SECONDS or night is None or night < today:
                    continue
                if expected_hold(job):
                    continue
                items.append(item(job, "needs_attention:" + state, reason, now, since=updated))
        for ev in unrecovered_stalls(self.dir, now):
            items.append(ev)
        return items

    def _requeue_worker_error(self, job, requeued, now):
        ref = job.get("ref")
        if ref in requeued:
            return [item(job, "worker_error_again", job.get("reason"), now,
                         tried="auto-requeued once already; failed again", since=job.get("updated"))]
        if self.dry_run:
            return [item(job, "worker_error_would_requeue", job.get("reason"), now,
                         tried="dry run: would auto-requeue once", since=job.get("updated"))]
        try:
            code, body = self.api.stall_requeue(ref, "Auto-requeue (worker monitor): browser/connection failure, never submitted")
        except Exception as exc:  # noqa: BLE001
            code, body = None, {"error": f"{type(exc).__name__}"}
        state = body.get("state") if isinstance(body, dict) else None
        requeued[ref] = now
        _write_json(self.requeue_file, requeued)
        if code == 200 and state == "queued":
            log.info("AUDIT auto-requeued worker-error job %s", ref)
            return []
        err = (body or {}).get("error") if isinstance(body, dict) else body
        return [item(job, "worker_error_requeue_failed", job.get("reason"), now,
                     tried=f"auto-requeue refused (HTTP {code}: {str(err)[:200]})", since=job.get("updated"))]

    def _report(self, items, now):
        if not items:
            log.info("AUDIT clean: nothing stuck")
            return {"flagged": 0, "items": [], "sent": 0}
        if self.dry_run:
            log.info("AUDIT dry run: %d item(s) would be flagged", len(items))
            return {"flagged": len(items), "items": items, "sent": 0, "dry_run": True}
        fresh = self.dedupe.fresh(items, now)
        log.warning("AUDIT %d flagged item(s), %d new since the 6h dedupe", len(items), len(fresh))
        if not fresh:
            return {"flagged": len(items), "items": items, "sent": 0}
        payload = {
            "type": "worker_alert",
            "source": "amy-box-worker",
            "at": datetime.fromtimestamp(now, PHOENIX).isoformat(),
            "count": len(fresh),
            "items": fresh,
        }
        ok = None
        try:
            ok = self.sender(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("AUDIT webhook send raised %s", type(exc).__name__)
            ok = False
        if ok is None or ok is False:
            with open(self.alerts_file, "a") as fh:
                fh.write(json.dumps({**payload, "delivered": False,
                                     "why": "webhook not configured" if ok is None else "webhook failed"},
                                    default=str) + "\n")
            try:
                os.chmod(self.alerts_file, 0o600)
            except OSError:
                pass
            log.warning("AUDIT alert NOT delivered (%s); wrote %d item(s) to %s",
                        "AMYI_WEBHOOK_URL not set" if ok is None else "webhook failed",
                        len(fresh), self.alerts_file)
        else:
            log.info("AUDIT alert delivered: %d item(s)", len(fresh))
        if ok is not False:   # failed delivery retries next audit; unconfigured is deduped
            self.dedupe.mark(fresh, now)
        return {"flagged": len(items), "items": items, "sent": len(fresh), "delivered": bool(ok)}


# --- shared state with the worker loop / stall supervisor ----------------------

def run_in_progress(directory):
    hb = stall_watchdog.read_heartbeat(directory) or {}
    return hb.get("status") == "running"


def mark_pickup(directory, now=None):
    try:
        p = Path(directory) / "last_pickup_at"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(now or time.time()))
    except OSError:
        log.warning("could not record last pickup time")


def read_last_pickup(directory):
    try:
        return float((Path(directory) / "last_pickup_at").read_text().strip())
    except (OSError, ValueError):
        return 0.0


def unrecovered_stalls(directory, now, window=NEW_EXCEPTION_SECONDS):
    out = []
    path = Path(directory) / stall_watchdog.EVENTS_NAME
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
            ts = datetime.fromisoformat(ev["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if now - ts > window:
            continue
        left = list(ev.get("left_manual") or []) + list(ev.get("uncertain") or [])
        if left or ev.get("chrome_ok") is False:
            bits = []
            if left:
                bits.append("jobs not recovered: " + ",".join(left))
            if ev.get("chrome_ok") is False:
                bits.append("Chrome did not restart")
            out.append(system_item(f"stall_unrecovered:{ev.get('slot')}:{ev.get('ts')}",
                                   f"Pickup {ev.get('slot')} stalled {ev.get('duration')}s; " + "; ".join(bits),
                                   tried="stall watchdog killed the worker, requeued unsubmitted jobs, "
                                         "restarted Chrome and started a catch-up run",
                                   since=ts, now=now))
    return out


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["--dry-run"]:
        raise SystemExit("usage: worker_monitor.py --dry-run")
    import booking_worker as bw
    work_dir = Path(os.environ.get("AMY_WORKER_DIR", "/workspace/amy-worker/data"))
    mon = Monitor(bw.Api.from_env(), work_dir, dry_run=True, sender=lambda p: None)
    now = time.time()
    due = mon.due_jobs(now)
    result = mon.audit(now=now)
    print(json.dumps({"sweep_due": [j.get("ref") for j in due], **result}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

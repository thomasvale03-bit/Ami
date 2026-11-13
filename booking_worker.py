"""AMY Railway booking worker: books queued guest-list jobs with no desktop.

Runs as its own Railway service (same image, start command
`python booking_worker.py --loop`) and talks to the AMY service only over its
authenticated HTTPS API (/queue/* and /send-branded), so the queue database
never leaves the AMY volume.

Per job: claim one -> re-check source rules -> for each exact source
venue/night: skip if already booked (worker ledger or saved progress), read the
live TAO listing, fill the form through Playmaker's promoter link, submit once,
read the order ID + screenshot -> save progress -> send guest + team emails via
/send-branded -> mark complete with evidence. Drai's skips Speakeasy entirely
and goes straight to the existing guest confirmation + team summary emails.

Anything unclear stops the job as manual/uncertain with a reason. Nothing is
retried in a loop and nothing is ever submitted twice:
  * a local ledger (AMY_WORKER_DIR, put it on a volume) records "submitting"
    BEFORE the final click and the order ID right after; a "submitting" entry
    with no order means the outcome is unknown -> uncertain, never resubmitted;
  * progress (order IDs, email receipt IDs) is also saved on the server job.

Modes (AMY_WORKER_MODE):
  dry-run  (default) read the soonest queued job WITHOUT claiming it, open the
           TAO form, fill it and stop before "Submit Order". No queue writes,
           no emails.
  live     claim, book, email, complete.
"""
import argparse
import stall_watchdog
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

log = logging.getLogger("amy.worker")

PHOENIX = ZoneInfo("America/Phoenix")
VEGAS = ZoneInfo("America/Los_Angeles")
DEFAULT_BASE_URL = "https://amy-production-1616.up.railway.app"
# One slow TAO page must not stall a whole pickup (the 4 PM Nov 12 hang
# sat in page.goto for 74 minutes). About six minutes is enough for a
# normal booking plus a couple of 60s page-load retries.
JOB_TIME_CAP_SECONDS = 360


class Stop(Exception):
    """End this job now with `action` (manual|uncertain|retry) and a reason."""

    def __init__(self, action, reason, **extra):
        super().__init__(reason)
        self.action, self.reason, self.extra = action, reason, extra


# --- HTTP client ------------------------------------------------------------

class Api:
    """Minimal client for the AMY service. Tokens come from env, never logged."""

    def __init__(self, base_url, queue_token, send_token, timeout=90):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("AMY_BASE_URL must be https")
        self.base, self.queue_token, self.send_token, self.timeout = base_url.rstrip("/"), queue_token, send_token, timeout

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("AMY_BASE_URL", DEFAULT_BASE_URL),
                   os.environ.get("AMY_QUEUE_API_TOKEN", ""), os.environ.get("BRANDED_SEND_TOKEN", ""))

    def _call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode() or "null")
            except ValueError:
                payload = None
            return exc.code, payload

    def claim(self):
        return self._call("POST", "/queue/claim", {"limit": 1}, self.queue_token)

    def jobs(self, state="queued", limit=100):
        return self._call("GET", f"/queue/jobs?state={quote(state)}&limit={limit}", None, self.queue_token)

    def update(self, ref, token, action, metadata=None, reason=""):
        return self._call("POST", "/queue/jobs/" + quote(ref, safe=""), {
            "claim_token": token, "action": action, "metadata": metadata or {}, "reason": reason[:1000]},
            self.queue_token)

    def send_branded(self, payload):
        return self._call("POST", "/send-branded", payload, self.send_token)

    def stall_requeue(self, ref, reason=""):
        return self._call("POST", "/queue/stall-requeue/" + quote(ref, safe=""),
                          {"reason": reason}, self.queue_token)


# --- Local no-double-book ledger ---------------------------------------------

class Ledger:
    """JSON file keyed "ref|venue|date". Written atomically before the final
    click ("submitting") and right after ("booked")."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def get(self, key):
        return self._load().get(key)

    def put(self, key, value):
        data = self._load()
        data[key] = {**data.get(key, {}), **value, "at": time.time()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def night_key(ref, venue, night):
    return f"{ref}|{venue}|{night}"


# --- Vendors (thin wrappers so tests can swap them) ---------------------------

class TaoBooker:
    def __init__(self):
        import tao_portal
        self.tao = tao_portal

    def reset(self):
        self.tao.reset_catalog()

    def posted(self, venue, night):
        """True/False when the live feed is readable; None when unknown."""
        snap = self.tao.catalog_snapshot()
        if not snap:
            return None
        return (venue, night) in snap

    def listing(self, venue, night):
        return self.tao.check_availability(venue, night)

    def submit(self, listing, guest, *, rehearse, screenshot_path, before_submit):
        return self.tao.submit_registration(listing, guest, rehearse=rehearse,
                                            screenshot_path=screenshot_path, before_submit=before_submit)


class DraisBooker:
    """SpeakeasyGo manager: add name + party to the listing date, verify the
    row appears, never add a name that is already there."""

    LIST_URL = "https://manager.speakeasygo.com/guest-details?date={d}"

    def add(self, listing_date, first, last, male, female, *, rehearse, screenshot_path, before_submit):
        import speakeasy
        from playwright.sync_api import sync_playwright
        name = f"{first} {last}".strip()
        url = self.LIST_URL.format(d=listing_date)
        import tao_portal
        with sync_playwright() as p:
            browser, page = tao_portal._browser_page(p)
            page.set_default_timeout(20000)
            try:
                speakeasy._login(page)
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                before = page.locator("body").inner_text()
                if name.lower() in before.lower():
                    raise Stop("manual", f"{name} is already on the Drai's {listing_date} list; not adding twice")
                if rehearse:
                    return {"rehearsal": True, "screenshot": _shot(page, screenshot_path)}
                speakeasy.MANAGER_URL = url
                original_goto = speakeasy._goto_date
                speakeasy._goto_date = lambda _page, _target: True  # the URL already selects the date
                try:
                    before_submit()
                    speakeasy._add_one(page, listing_date, first, last, male, female)
                finally:
                    speakeasy._goto_date = original_goto
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                after = page.locator("body").inner_text()
                shot = _shot(page, screenshot_path)
                if name.lower() not in after.lower():
                    raise Stop("uncertain", f"Saved {name} on Speakeasy {listing_date} but the row is not visible")
                row = next((ln.strip() for ln in after.splitlines() if name.lower() in ln.lower()), name)
                return {"verified": True, "row": row[:200], "screenshot": shot}
            finally:
                browser.close()


def _shot(page, path):
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def _sha256(path):
    if not path or not os.path.exists(path):
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- Job processing -----------------------------------------------------------

def source_nights(raw):
    from config import rules
    from rules_engine import date_range
    venues = [rules.normalize_venue_name(v) for v in raw.get("venues") or []]
    nights = list(date_range(raw["start_date"], raw["end_date"]))
    # Nightclubs still appear on dark nights so DEFAULT_ROUTING can cover them.
    # Verified-dark daylife / Drai's dates are dropped (no lookup, no retry).
    return [(v, d.isoformat()) for d in nights for v in venues
            if v in rules.NIGHTCLUBS or rules.venue_open_on(v, d) is not False]


def drais_offset():
    """Days between the guest's night and SpeakeasyGo's listing date.
    Default 0: Speakeasy entry goes on the SAME night as the Posh/request date
    (Thomas's decision, Nov 9 2026). DRAIS_SPEAKEASY_DAY_OFFSET overrides;
    an unparseable value => None => Drai's goes manual."""
    value = os.environ.get("DRAIS_SPEAKEASY_DAY_OFFSET", "").strip()
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return None


# --- Pickup schedule (Thomas, Nov 9 2026; America/Phoenix) ---------------------
# Mon-Wed: every 3 hours. Thu-Sun: nothing 1 AM-10 AM, then 10 AM, noon, 2 PM,
# 4 PM and every hour 6 PM through midnight. A run starts only in the first ten
# minutes of a scheduled hour (same :00-:09 window as the server's hourly
# pickup) and then claims ONE job at a time back to back until the queue is
# empty (at most RUN_MAX_JOBS), so every job keeps its own one-hour lease.
MON_WED_HOURS = frozenset({0, 3, 6, 9, 12, 15, 18, 21})
THU_SUN_HOURS = frozenset({0, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23})
RUN_MAX_JOBS = 25


# Minutes after a scheduled hour in which a run may still start. The box VM is
# paused while no agent is active, so a wake a few minutes late must still
# pick up (AMY_WORKER_RUN_WINDOW_MIN, default 30).
RUN_WINDOW_MIN = int(os.environ.get("AMY_WORKER_RUN_WINDOW_MIN", "30"))


def scheduled_slot(ts, window=None):
    """'YYYY-MM-DDTHH' when a pickup run may start at ts, else None."""
    local = datetime.fromtimestamp(ts, PHOENIX)
    if local.minute >= (RUN_WINDOW_MIN if window is None else window):
        return None
    hours = MON_WED_HOURS if local.weekday() <= 2 else THU_SUN_HOURS
    return local.strftime("%Y-%m-%dT%H") if local.hour in hours else None


def next_slot_label(ts):
    """Human label of the next scheduled run start, for the log."""
    t = int(ts // 600 + 1) * 600
    for _ in range(24 * 6 * 8):
        if scheduled_slot(t):
            return datetime.fromtimestamp(t, PHOENIX).strftime("%a %b %d %I:%M %p MST")
        t += 600
    return "unknown"


class SlotState:
    """Persists the last pickup slot run (survives restarts) so a kick or a
    restart never runs the same hour twice."""

    def __init__(self, work_dir):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.last_file, self.kick_file = self.dir / "last_slot", self.dir / "kick"
        self.run_now_file = self.dir / "run_now"

    def last(self):
        try:
            return self.last_file.read_text().strip() or None
        except FileNotFoundError:
            return None

    def mark(self, slot):
        tmp = self.last_file.with_suffix(".tmp")
        tmp.write_text(slot)
        os.replace(tmp, self.last_file)

    def take_run_now(self):
        try:
            self.run_now_file.unlink()
            return True
        except FileNotFoundError:
            return False

    def take_kick(self):
        try:
            self.kick_file.unlink()
            return True
        except FileNotFoundError:
            return False


def pickup_step(worker, state, now, kicked=False):
    """One wall-clock check. Runs a pickup when the current Phoenix time is in
    a scheduled slot not yet run. Returns (slot, result) or (None, None)."""
    slot = scheduled_slot(now)
    if kicked and not slot:
        log.info("kick ignored: %s is not a scheduled pickup time (next %s)",
                 datetime.fromtimestamp(now, PHOENIX).strftime("%a %I:%M %p"), next_slot_label(now))
    if not slot or slot == state.last():
        if kicked and slot:
            log.info("kick ignored: pickup %s already ran", slot)
        return None, None
    state.mark(slot)  # before running: a crash mid-run never re-runs the hour
    log.info("Pickup run %s starting (%s)", slot, "kick" if kicked else "schedule")
    stall_watchdog.start_run(state.dir, slot)
    try:
        try:
            result = worker.run_pickup()
        except Exception:  # noqa: BLE001
            log.exception("Pickup run failed")
            result = {"status": "error"}
        log.info("run %s: %s", slot, json.dumps(result, default=str)[:4000])
        log.info("next run %s", next_slot_label(time.time()))
        return slot, result
    finally:
        stall_watchdog.end_run(state.dir)


def run_now(worker, max_batches=8, work_dir=None):
    """Owner-requested pickup outside the schedule: work through the whole
    queue, one guest at a time, in batches of RUN_MAX_JOBS until a batch stops
    early (queue empty, claim refused or error). Does not touch last_slot."""
    directory = Path(work_dir or os.environ.get("AMY_WORKER_DIR", "/data/worker"))
    log.info("Off-schedule pickup (run_now) starting")
    stall_watchdog.start_run(directory, "run_now")
    try:
        batches = []
        for _ in range(max_batches):
            try:
                result = worker.run_pickup()
            except Exception:  # noqa: BLE001
                log.exception("run_now batch failed")
                result = {"status": "error", "handled": 0, "results": [{"status": "error"}]}
            batches.append(result)
            log.info("run_now batch %d: %s", len(batches), json.dumps(result, default=str)[:4000])
            last = (result.get("results") or [{}])[-1].get("status")
            if result.get("handled", 0) < RUN_MAX_JOBS or last in ("idle", "claim_refused", "error"):
                break
        total = sum(b.get("handled", 0) for b in batches)
        log.info("Off-schedule pickup done: %d batch(es), %d job(s) handled", len(batches), total)
        return {"status": "run_now_done", "batches": len(batches), "handled": total}
    finally:
        stall_watchdog.end_run(directory)


def live_loop(worker, work_dir, loop=True, step=5, heartbeat=600, clock=time.time, sleep=time.sleep):
    """Short sleeps, recomputed from wall-clock time every step (the box VM can
    be paused mid-sleep), a kick file for an immediate pickup, a heartbeat."""
    state = SlotState(work_dir)
    log.info("Pickup schedule (Phoenix): Mon-Wed %s; Thu-Sun %s; window :00-:%02d; next run %s",
             sorted(MON_WED_HOURS), sorted(THU_SUN_HOURS), RUN_WINDOW_MIN - 1, next_slot_label(clock()))
    last_beat = 0.0
    while True:
        now = clock()
        if state.take_run_now():
            run_now(worker, work_dir=state.dir)
            now = clock()
        slot, result = pickup_step(worker, state, now, kicked=state.take_kick())
        if not loop:
            return result or {"status": "not_scheduled", "next": next_slot_label(now)}
        if now - last_beat >= heartbeat:
            last_beat = now
            ensure_browser()
            log.info("heartbeat %s; last run %s; next run %s",
                     datetime.fromtimestamp(now, PHOENIX).strftime("%a %b %d %I:%M %p MST"),
                     state.last() or "none", next_slot_label(now))
        sleep(step)


def box_lock(ref):
    """AMY_WORKER_BOX_LOCK=true (running on the Grok Bot box): hold the same
    /workspace/.tao_browser.lock that AMYI's computerUse TAO runs use, so the
    worker and AMYI never sign up guests at the same moment. No-op otherwise."""
    import contextlib
    if os.environ.get("AMY_WORKER_BOX_LOCK", "").lower() != "true":
        return contextlib.nullcontext()
    from tools import browser_lock
    return browser_lock.held(f"worker:{ref}")


class Worker:
    def __init__(self, api, ledger, tao=None, drais=None, *, mode="dry-run", evidence_dir=None,
                 now=None, sleep=time.sleep, email_wait_seconds=None, duplicate_check=None, alerts=None,
                 job_time_cap=None):
        self.api, self.ledger, self.mode = api, ledger, mode
        self.tao, self.drais = tao or TaoBooker(), drais or DraisBooker()
        self.evidence_dir = Path(evidence_dir or os.environ.get("AMY_WORKER_DIR", "/data/worker")) / "evidence"
        self.now, self.sleep = now or time.time, sleep
        self.email_wait = (int(os.environ.get("AMY_WORKER_EMAIL_WAIT_SECONDS", "900"))
                           if email_wait_seconds is None else email_wait_seconds)
        self.job_time_cap = (int(os.environ.get("AMY_WORKER_JOB_SECONDS", str(JOB_TIME_CAP_SECONDS)))
                             if job_time_cap is None else job_time_cap)
        # Thomas, Nov 11 2026: off by default so additional sign-ups with the
        # same email/venue/night are booked. The per-job ledger still skips a
        # night already submitted for this job. Set AMY_WORKER_DUPLICATE_CHECK=true
        # to restore the old same-email manual stop.
        self.duplicate_check = (os.environ.get("AMY_WORKER_DUPLICATE_CHECK", "false").lower() == "true"
                                if duplicate_check is None else duplicate_check)
        self.alerts = (os.environ.get("AMY_WORKER_ALERTS", "").lower() == "true" if alerts is None else alerts)

    # -- one cycle --
    def run_once(self):
        if self.mode == "live":
            return self._live_once()
        return self._dry_run_once()

    def _dry_run_once(self):
        """Read-only: rehearse the soonest future queued job, stop before submit."""
        import queue_service
        code, body = self.api.jobs("queued", 100)
        if code != 200:
            return {"status": "error", "reason": f"queue read HTTP {code}"}
        today = datetime.fromtimestamp(self.now(), PHOENIX).date()
        jobs = sorted(body.get("jobs") or [], key=lambda j: ((j.get("raw") or {}).get("start_date") or "9999", j.get("created") or 0))
        for job in jobs:
            raw = job.get("raw") or {}
            if queue_service.source_policy(job["ref"], raw, today):
                continue
            try:
                result = self._book_all(job, raw, rehearse=True)
            except Stop as stop:
                return {"status": "rehearsal_stopped", "ref": job["ref"], "action": stop.action, "reason": stop.reason}
            return {"status": "rehearsed", "ref": job["ref"], "nights": result}
        return {"status": "idle"}

    def run_pickup(self, max_jobs=RUN_MAX_JOBS):
        """One scheduled run: claim and finish one job at a time until the
        queue is empty, a claim is refused, or max_jobs were handled."""
        ensure_browser()
        results = []
        for _ in range(max_jobs):
            result = self._live_once()
            results.append(result)
            if result.get("status") in ("idle", "claim_refused", "not_assigned", "error"):
                break
        handled = [r for r in results if r.get("ref") and r.get("status") not in ("not_assigned",)]
        return {"status": "run_done", "handled": len(handled), "results": results}

    def _live_once(self):
        code, body = self.api.claim()
        if code != 200:
            return {"status": "claim_refused", "http": code, "reason": (body or {}).get("error") if isinstance(body, dict) else body}
        jobs = body.get("jobs") or []
        if not jobs:
            return {"status": "idle"}
        job = jobs[0]
        if job.get("state") != "assigned" or not job.get("claim_token"):
            return {"status": "not_assigned", "ref": job.get("ref"), "state": job.get("state")}
        return self.process(job)

    # -- one job --
    def process(self, job):
        ref, token, raw = job["ref"], job["claim_token"], job.get("raw") or {}
        try:
            try:
                lock = box_lock(ref)
                lock.__enter__()
            except Exception as exc:  # noqa: BLE001 - lock timeout: nothing was touched yet
                raise Stop("retry", f"Box browser busy (AMYI computerUse holds the TAO lock): {exc}")
            try:
                stall_watchdog.mark_progress(self.evidence_dir.parent, ref)
                return self._run_job(job, ref, token, raw)
            finally:
                lock.__exit__(None, None, None)
        except Stop as stop:
            return self._finish_stop(ref, token, raw, stop)
        except Exception as exc:  # noqa: BLE001 - never loop on an unknown failure
            return self._handle_unexpected(job, ref, token, raw, exc)

    def _run_job(self, job, ref, token, raw):
        """Run one job under the hard time cap so a hung page cannot stall the pickup."""
        return run_with_deadline(
            lambda: self._process(job, ref, token, raw),
            self.job_time_cap,
            on_expire=lambda: self._unstick_browser(ref),
            clock=self.now,
        )

    def _unstick_browser(self, ref):
        log.warning("Job %s hit the %ss time cap; unsticking the booking browser",
                    ref, self.job_time_cap)
        try:
            import browser_host
            browser_host.restart()
        except Exception:  # noqa: BLE001
            log.exception("Could not unstick the booking browser after the job time cap")

    def _finish_stop(self, ref, token, raw, stop):
        meta = {"worker": "railway", **stop.extra}
        if stop.action == "retry":
            meta["submission_attempted"] = False
        code, body = self.api.update(ref, token, stop.action, meta, stop.reason)
        log.warning("Job %s -> %s: %s (HTTP %s)", ref, stop.action, stop.reason, code)
        if stop.action in ("manual", "uncertain"):
            self._alert(ref, stop.action, stop.reason, raw)
        return {"status": stop.action, "ref": ref, "reason": stop.reason, "http": code}

    def _handle_unexpected(self, job, ref, token, raw, exc):
        log.exception("Job %s failed unexpectedly", ref)
        attempted = any((self.ledger.get(night_key(ref, v, n)) or {}).get("state")
                        for v, n in self._safe_nights(raw))
        if not attempted and retryable_browser_error(exc):
            try:
                import browser_host
                log.warning("Job %s: browser error (%s); restarting and retrying once",
                            ref, type(exc).__name__)
                browser_host.restart()
                return self._run_job(job, ref, token, raw)
            except Stop as stop:
                return self._finish_stop(ref, token, raw, stop)
            except Exception as retry_exc:  # noqa: BLE001
                log.exception("Job %s retry after browser restart failed", ref)
                exc = retry_exc
                attempted = any((self.ledger.get(night_key(ref, v, n)) or {}).get("state")
                                for v, n in self._safe_nights(raw))
                if not attempted and retryable_browser_error(exc):
                    return self._finish_stop(ref, token, raw, Stop(
                        "retry",
                        f"Worker error ({type(exc).__name__}: {str(exc)[:300]}); "
                        "left queued for the next pickup",
                    ))
        action = "uncertain" if attempted else "manual"
        reason = f"Worker error ({type(exc).__name__}: {str(exc)[:300]}); not retried"
        self.api.update(ref, token, action, {"worker": "railway"}, reason)
        self._alert(ref, action, reason, raw)
        return {"status": action, "ref": ref, "reason": reason}

    @staticmethod
    def _safe_nights(raw):
        try:
            return source_nights(raw)
        except Exception:  # noqa: BLE001
            return []

    def _process(self, job, ref, token, raw):
        import queue_service
        today = datetime.fromtimestamp(self.now(), PHOENIX).date()
        reason = queue_service.source_policy(ref, raw, today)
        if reason:
            raise Stop("manual", reason)
        if self.duplicate_check:
            self._check_duplicates(ref, raw)
        bookings = self._book_all(job, raw, rehearse=False, token=token)
        from config import rules
        drais = any(rules.is_drais(v) for v in raw.get("venues") or [])
        evidence = {"verified": True, "worker": "railway", "bookings": [b["record"] for b in bookings]}
        evidence["email_receipts"] = self._send_emails(job, ref, token, raw, bookings, drais=drais)
        code, body = self.api.update(ref, token, "complete", {"evidence": evidence}, "Booked by Railway worker")
        if code != 200:
            raise Stop("manual", f"Booked and emailed but completion refused (HTTP {code}: "
                       f"{(body or {}).get('error') if isinstance(body, dict) else body}); DO NOT rebook",
                       evidence=evidence)
        log.info("Job %s complete: %s", ref, [b["record"].get("order_id") or b["record"].get("entry_id") or b["record"].get("system") for b in bookings])
        return {"status": "done", "ref": ref, "evidence": evidence}

    def _check_duplicates(self, ref, raw):
        """Same guest email + venue + night already completed in the queue."""
        code, body = self.api.jobs("done", 100)
        if code != 200:
            return
        mine = set(self._safe_nights(raw))
        email = (raw.get("email") or "").strip().lower()
        for other in body.get("jobs") or []:
            oraw = other.get("raw") or {}
            if other.get("ref") == ref or (oraw.get("email") or "").strip().lower() != email:
                continue
            overlap = mine & set(self._safe_nights(oraw))
            if overlap:
                v, n = sorted(overlap)[0]
                raise Stop("manual", f"Duplicate suspicion: {email} already booked {v} on {n} (job {other['ref']})")

    def _book_all(self, job, raw, *, rehearse, token=None):
        from config import rules
        from rules_engine import ActionNeeded, normalize_guest_request
        try:
            request = normalize_guest_request(raw)
        except ActionNeeded as exc:
            raise Stop("manual", exc.issue)
        except (KeyError, ValueError, TypeError) as exc:
            raise Stop("manual", f"Source request malformed ({exc})")
        progress = ((job.get("metadata") or {}).get("progress") or {}).get("bookings") or {}
        out = []
        self.tao.reset()
        nights = source_nights(raw)
        for venue, night in nights:
            key = night_key(job["ref"], venue, night)
            saved = progress.get(f"{venue}|{night}") or (self.ledger.get(key) or {}).get("record")
            if saved and saved.get("verified"):
                out.append({"record": saved, "meta": (self.ledger.get(key) or {}).get("meta", {})})
                continue
            if not rehearse and (self.ledger.get(key) or {}).get("state") == "submitting":
                raise Stop("uncertain", f"{venue} {night}: a submit started earlier with no confirmed result; "
                           "check TAO/Speakeasy before any resubmit")
            # A nightclub already booked this date (requested or fallback)
            # fills that nightclub slot; a dayclub on the same date is separate.
            if venue in rules.NIGHTCLUBS:
                existing = self._existing_night_booking(job["ref"], night, out)
                if existing and (existing.get("record") or {}).get("venue") in rules.NIGHTCLUBS:
                    out.append(existing)
                    continue
            if any(rules.is_manual_venue(v) for v in (raw.get("venues") or [])):
                raise Stop("manual", "LIV, XS, LIV Beach and EBC stay manual")
            if rules.is_drais(venue):
                if rules.venue_open_on(venue, date.fromisoformat(night)) is False:
                    continue
                item = self._book_drais(job["ref"], venue, night, request, rehearse, token)
            else:
                item = self._book_requested_or_fallback(
                    job["ref"], venue, night, request, rehearse, token, partial=bool(out), booked=out)
            if item:
                out.append(item)
        self._add_marquee_dayclub(job["ref"], nights, request, rehearse, token, out)
        return out

    def _add_marquee_dayclub(self, ref, nights, request, rehearse, token, out):
        """Add Marquee Dayclub on each itinerary date it is open, through 2027-03-31."""
        from config import rules
        if nights and all(rules.is_drais(v) for v, _ in nights):
            return
        dates = []
        for _, night in nights:
            if night not in dates:
                dates.append(night)
        for night in dates:
            if night > rules.MARQUEE_DAYCLUB_PUSH_END:
                continue
            key = night_key(ref, "Marquee Dayclub", night)
            saved = (self.ledger.get(key) or {}).get("record") or {}
            if saved.get("verified") and saved.get("order_id"):
                if not any((i.get("record") or {}).get("venue") == "Marquee Dayclub"
                           and (i.get("record") or {}).get("date") == night for i in out):
                    out.append({"record": saved, "meta": (self.ledger.get(key) or {}).get("meta") or {}})
                continue
            if any((i.get("record") or {}).get("venue") == "Marquee Dayclub"
                   and (i.get("record") or {}).get("date") == night for i in out):
                continue
            try:
                item = self._book_tao(ref, "Marquee Dayclub", night, request, rehearse, token, partial=False)
            except Stop as stop:
                if stop.extra.get("kind") in ("unposted", "unavailable", "access_code"):
                    continue  # not open that day; skip
                raise
            out.append(item)

    def _existing_night_booking(self, ref, night, booked):
        """Reuse a verified nightclub booking already made for this job on this
        date (requested venue or a fallback), so a later pass does not resubmit."""
        from config import rules
        for item in booked:
            rec = item.get("record") or {}
            if rec.get("verified") and rec.get("date") == night and rec.get("venue") in rules.NIGHTCLUBS and (
                    rec.get("order_id") or rec.get("system")):
                return item
        prefix, suffix = f"{ref}|", f"|{night}"
        for key, entry in (self.ledger._load() or {}).items():
            rec = (entry or {}).get("record") or {}
            if key.startswith(prefix) and key.endswith(suffix) and rec.get("venue") in rules.NIGHTCLUBS and rec.get("verified") and (
                    rec.get("order_id") or rec.get("system")):
                return {"record": rec, "meta": (entry or {}).get("meta") or {}}
        return None

    def _previous_nightclub(self, night, booked):
        prev = (date.fromisoformat(night) - timedelta(days=1)).isoformat()
        from config import rules
        for item in booked:
            rec = item.get("record") or {}
            if rec.get("date") == prev and rec.get("venue") in rules.NIGHTCLUBS:
                return rec["venue"]
        return None

    def _book_requested_or_fallback(self, ref, venue, night, request, rehearse, token, partial, booked):
        """Book the requested venue first. If it is closed, needs a code, or
        is not operating that night, fall through DEFAULT_ROUTING (OMNIA first)
        rather than parking the night as manual. A verified-dark night skips
        the TAO lookup and does not retry the requested venue. Lists not yet
        posted on a night the venue does operate still retry."""
        FALLBACK_KINDS = ("unposted", "unavailable", "access_code")
        from config import rules
        from rules_engine import fallback_venues_for, venue_operates_that_night
        night_date = date.fromisoformat(night)
        if rules.venue_open_on(venue, night_date) is False:
            if venue not in rules.NIGHTCLUBS:
                return None
            stop = Stop("retry", f"{venue} {night}: does not operate that night; nothing submitted",
                        kind="unavailable")
        else:
            try:
                return self._book_tao(ref, venue, night, request, rehearse, token, partial)
            except Stop as raised:
                stop = raised
        if stop.extra.get("kind") not in FALLBACK_KINDS:
            raise stop
        # Non-nightclubs (e.g. LAVO): keep retry for an unposted list.
        if venue not in rules.NIGHTCLUBS:
            if stop.extra.get("kind") == "unposted":
                raise Stop("retry", stop.reason, kind="unposted")
            raise stop
        if stop.extra.get("kind") == "unposted" and venue_operates_that_night(venue, night_date):
            raise Stop("retry", f"{venue} {night}: guest list not posted on TAO yet; nothing submitted",
                       kind="unposted")
        prev = self._previous_nightclub(night, booked)
        last_stop = stop
        for alt in fallback_venues_for(night_date, prev, skip={venue}):
            alt_key = night_key(ref, alt, night)
            alt_entry = self.ledger.get(alt_key) or {}
            saved = alt_entry.get("record") or {}
            if saved.get("verified") and saved.get("order_id"):
                return {"record": saved, "meta": alt_entry.get("meta") or {}}
            if not rehearse and alt_entry.get("state") == "submitting":
                raise Stop("uncertain", f"{alt} {night}: a submit started earlier with no confirmed result; "
                           "check TAO/Speakeasy before any resubmit")
            try:
                item = self._book_tao(ref, alt, night, request, rehearse, token, partial=False)
            except Stop as alt_stop:
                last_stop = alt_stop
                if alt_stop.extra.get("kind") in FALLBACK_KINDS:
                    continue
                raise
            item.setdefault("meta", {})["fallback"] = True
            item["meta"]["fallback_from"] = venue
            if item.get("record"):
                item["record"]["fallback"] = True
            if not rehearse and item.get("record"):
                self._checkpoint(ref, token, alt_key,
                                 {"state": "booked", "record": item["record"], "meta": item["meta"]})
            return item
        if last_stop.action == "uncertain":
            raise last_stop
        raise Stop("retry", f"{venue} {night}: closed, access-code or not operating; "
                   "DEFAULT_ROUTING fallbacks also not bookable yet; nothing submitted")

    def _shot_path(self, ref, venue, night, suffix):
        safe = "".join(c if c.isalnum() else "-" for c in venue)
        return str(self.evidence_dir / ref / f"{night}-{safe}-{suffix}.png")

    def _checkpoint(self, ref, token, key, entry):
        self.ledger.put(key, entry)
        stall_watchdog.mark_progress(self.evidence_dir.parent, ref)
        if token:
            self.api.update(ref, token, "progress", {"progress": {"bookings": {key.split("|", 1)[1]: entry.get("record")}}}
                            if entry.get("record") else {"progress": {"submitting": {key.split("|", 1)[1]: True}}})

    def _book_tao(self, ref, venue, night, request, rehearse, token, partial):
        import tao_portal
        key = night_key(ref, venue, night)
        night_date = date.fromisoformat(night)
        posted = self.tao.posted(venue, night_date)
        if posted is False:
            raise Stop("retry", f"{venue} {night}: guest list not posted on TAO yet; nothing submitted", kind="unposted")
        try:
            listing = self.tao.listing(venue, night_date)
        except tao_portal.TaoBlocked as exc:
            raise Stop("manual", f"{venue} {night}: {exc}; nothing submitted (waited for Cloudflare to clear)")
        if listing and listing.get("access_code_required"):
            raise Stop("retry", f"{venue} {night}: listing requires an access code; nothing submitted",
                       kind="access_code")
        if not listing:
            raise Stop("manual", f"{venue} {night}: no live free guest list (closed, sold out or not free); nothing submitted",
                       kind="unavailable")
        if listing.get("listing_type") != "Passes" or listing.get("price", 0) != 0:
            raise Stop("manual", f"{venue} {night}: listing is not a free guest-list pass", kind="unavailable")
        party = {"male_count": int(request["male_count"]), "female_count": int(request["female_count"])}
        shot = self._shot_path(ref, venue, night, "rehearsal" if rehearse else "confirmation")

        def before_submit():
            if not rehearse:
                self._checkpoint(ref, token, key, {"state": "submitting"})

        try:
            result = self.tao.submit(listing, request, rehearse=rehearse, screenshot_path=shot, before_submit=before_submit)
        except tao_portal.TaoBlocked as exc:
            raise Stop("manual", f"{venue} {night}: {exc}; nothing submitted")
        except tao_portal.SubmissionUncertain as exc:
            raise Stop("uncertain", f"{venue} {night}: submitted once but no confirmation ({exc}); check TAO, do not resubmit")
        if rehearse:
            if not result.get("rehearsal"):
                raise Stop("manual", f"{venue} {night}: rehearsal stopped: {result.get('reason')}")
            return {"venue": venue, "date": night, "rehearsal": True, "form_filled": True,
                    "event": listing.get("event"), "screenshot": result.get("screenshot"),
                    "submit_button": result.get("submit_button"), "page_url": result.get("page_url")}
        if result.get("assisted_job"):
            raise Stop("manual", f"{venue} {night}: assisted mode is on; worker never hands off. Nothing submitted")
        if not result.get("verified") or not result.get("confirmation_id"):
            reason = result.get("reason", "could not be verified")
            kind = "access_code" if "access code" in reason.lower() else None
            raise Stop("manual", f"{venue} {night}: not submitted — {reason}", **({"kind": kind} if kind else {}))
        record = {"verified": True, "venue": venue, "date": night, "order_id": result["confirmation_id"], "party": party}
        meta = {"event": result.get("event") or listing.get("event"), "event_time": listing.get("event_time"),
                "confirmation_url": result.get("confirmation_url"), "screenshot": result.get("screenshot"),
                "screenshot_sha256": _sha256(result.get("screenshot"))}
        self._checkpoint(ref, token, key, {"state": "booked", "record": record, "meta": meta})
        return {"record": record, "meta": meta}

    def _book_drais(self, ref, venue, night, request, rehearse, token):
        """Email-only (Thomas, Nov 10 2026): never open Speakeasy, never write
        before_submit/submitting ledger state. Confirmation emails are sent by
        _send_emails after every night is recorded."""
        key = night_key(ref, venue, night)
        male, female = int(request["male_count"]), int(request["female_count"])
        if rehearse:
            return {"venue": venue, "date": night, "rehearsal": True, "email_only": True}
        record = {"verified": True, "venue": venue, "date": night, "system": "email_only",
                  "party": {"male_count": male, "female_count": female}}
        self._checkpoint(ref, token, key, {"state": "email_only", "record": record, "meta": {}})
        return {"record": record, "meta": {}}

    def _send_emails(self, job, ref, token, raw, bookings, drais=False):
        import worker_emails
        from rules_engine import normalize_guest_request
        request = normalize_guest_request(raw)
        rows = [{**b["record"], **(b.get("meta") or {})} for b in bookings]
        if drais:
            builders = (("guest", worker_emails.drais_guest_email(request, rows)),
                        ("team", worker_emails.drais_team_email(ref, raw, request, rows)))
        else:
            builders = (("guest", worker_emails.guest_email(request, rows)),
                        ("team", worker_emails.team_email(ref, raw, request, rows)))
        saved = ((job.get("metadata") or {}).get("progress") or {}).get("emails") or {}
        saved.update((self.ledger.get(f"{ref}|emails") or {}).get("receipts") or {})
        receipts = []
        for kind, payload in builders:
            if saved.get(kind):
                receipts.append({"kind": kind, "id": saved[kind]})
                continue
            if not worker_emails.allowed_recipient(payload["to"]):
                raise Stop("manual", f"{kind} email recipient blocked; bookings done, DO NOT rebook")
            message_id = self._send_one(kind, payload)
            saved[kind] = message_id
            self.ledger.put(f"{ref}|emails", {"receipts": dict(saved)})
            self.api.update(ref, token, "progress", {"progress": {"emails": {kind: message_id}}})
            receipts.append({"kind": kind, "id": message_id})
        return receipts

    def _send_one(self, kind, payload):
        deadline = self.now() + self.email_wait
        while True:
            code, body = self.api.send_branded(payload)
            result = (body or {}).get("result") if isinstance(body, dict) else None
            if code == 200 and isinstance(result, dict) and result.get("sent") and result.get("id"):
                return result["id"]
            if code == 200 and isinstance(result, str) and "duplicate" in result.lower():
                raise Stop("manual", f"{kind} email: /send-branded reports it was already sent; find the prior "
                           "message ID before completing. Bookings done, DO NOT rebook")
            if code == 429 and self.now() + 120 <= deadline:
                log.info("send-branded hourly cap reached; waiting 2 min before sending the %s email", kind)
                self.sleep(120)
                continue
            raise Stop("manual", f"{kind} email not sent (HTTP {code}: {str(result or body)[:200]}); "
                       "bookings done, DO NOT rebook")

    def _alert(self, ref, state, reason, raw):
        if not self.alerts:
            return
        import worker_emails
        payload = worker_emails.alert_email(ref, state, reason, raw)
        if worker_emails.allowed_recipient(payload["to"]):
            try:
                self.api.send_branded(payload)
            except Exception:  # noqa: BLE001
                log.exception("Exception alert email failed for %s", ref)


# --- Connectivity probe (no secrets, no guest data, no submit) ---------------

def probe():
    """Open Playmaker's TAO promoter page from this host, report whether the
    Cloudflare check showed/cleared, then open one live guest-list form and
    list the fields found. Fills nothing and never clicks submit."""
    import re
    import tao_portal
    from config.rules import TAO_PROMOTER_URL
    from playwright.sync_api import sync_playwright
    report = {"outbound_proxy": bool(tao_portal.browser_proxy()), "promoter_url": TAO_PROMOTER_URL}
    started = time.time()
    with sync_playwright() as p:
        closable, page = tao_portal._browser_page(p)
        try:
            page.goto(TAO_PROMOTER_URL, wait_until="domcontentloaded")
            tao_portal._settle(page)
            report["challenge_on_first_load"] = tao_portal.is_security_check(page.title(), page.locator("body").inner_text())
            try:
                tao_portal._open_promoter_page(page)
                report["promoter_page"] = "loaded"
            except tao_portal.TaoBlocked as exc:
                report["promoter_page"] = f"blocked: {exc}"
                report["seconds"] = round(time.time() - started)
                return report
            links = page.locator("a").evaluate_all(
                "els => els.map(a => ({text: (a.textContent || '').trim(), href: a.href}))")
            passes = [l["href"] for l in links if re.fullmatch(r"passes?", l["text"], re.I) and tao_portal.is_safe_pass_url(l["href"])]
            report["pass_links"] = len(passes)
            if passes:
                url = passes[0]
                report["form_url"] = url
                tao_portal._open_via_promoter(page, url)
                body = page.locator("body").inner_text()
                report["form_challenge"] = tao_portal.is_security_check(page.title(), body)
                report["gender_selects"] = sorted(tao_portal._selects_by_gender(page))
                report["fields_present"] = [sel for sel in ("#OrderEmail", "#OrderFirstName", "#OrderLastName",
                                                            "#OrderPhone", "#OrderPostalCode")
                                            if page.locator(sel).count()]
                report["submit_button_present"] = bool(page.locator('button[type="submit"], input[type="submit"]').count())
        finally:
            closable.close()
    report["seconds"] = round(time.time() - started)
    return report


def probe_speakeasy(listing_date=None):
    """Log into the SpeakeasyGo manager and open one date's guest list.
    Read-only: adds nothing, reports only whether the list page loaded."""
    import speakeasy
    import tao_portal
    from playwright.sync_api import sync_playwright
    listing_date = listing_date or datetime.now(VEGAS).date().isoformat()
    report = {"email_set": bool(speakeasy.EMAIL), "password_set": bool(speakeasy.PASSWORD), "date": listing_date}
    if not (speakeasy.EMAIL and speakeasy.PASSWORD):
        report["result"] = "missing SPEAKEASY_EMAIL/SPEAKEASY_PASSWORD"
        return report
    started = time.time()
    with sync_playwright() as p:
        closable, page = tao_portal._browser_page(p)
        page.set_default_timeout(20000)
        try:
            speakeasy._login(page)
            report["after_login_url"] = page.url.split("?")[0]
            page.goto(DraisBooker.LIST_URL.format(d=listing_date), wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            body = page.locator("body").inner_text()
            report["list_url"] = page.url.split("?")[0]
            report["login_wall"] = "password" in body.lower() and "sign in" in body.lower()
            report["add_new_button"] = bool(page.get_by_role("button", name="Add new").count())
            report["page_chars"] = len(body)
        finally:
            closable.close()
    report["seconds"] = round(time.time() - started)
    return report



class JobTimeExpired(Exception):
    """The job's hard time cap elapsed; the page or browser was unstuck."""


class JobDeadline:
    """Wall-clock cap that interrupts a hung Playwright call by running on_expire.

    Playwright's own timeouts sometimes never fire (the 4 PM Nov 12 goto hang).
    Closing or restarting Chrome from this timer unblocks the main thread."""

    def __init__(self, seconds, *, on_expire=None, clock=time.time):
        self.seconds = seconds
        self.deadline_at = clock() + seconds
        self.clock = clock
        self.on_expire = on_expire
        self._timer = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._fired = False

    def start(self):
        remaining = self.deadline_at - self.clock()
        if remaining <= 0:
            self._fire()
            return self
        self._timer = threading.Timer(remaining, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def _fire(self):
        with self._lock:
            if self._cancelled or self._fired:
                return
            self._fired = True
        log.warning("Job time cap (%ss) elapsed", self.seconds)
        if self.on_expire:
            try:
                self.on_expire()
            except Exception:  # noqa: BLE001
                log.exception("job time cap unstick failed")

    def expired(self):
        return self._fired or self.clock() >= self.deadline_at

    def check(self):
        if self.expired():
            raise JobTimeExpired(f"Job exceeded the {self.seconds:.0f}s time cap")

    def cancel(self):
        with self._lock:
            self._cancelled = True
        if self._timer is not None:
            self._timer.cancel()


def run_with_deadline(fn, seconds, *, on_expire=None, clock=time.time):
    """Run fn; if it overruns `seconds`, call on_expire to unstick the browser."""
    if seconds is None or seconds <= 0:
        return fn()
    deadline = JobDeadline(seconds, on_expire=on_expire, clock=clock).start()
    try:
        deadline.check()
        return fn()
    finally:
        deadline.cancel()


def browser_lost(exc):
    """True when the failure is a dead/unreachable booking Chrome, not a booking rule."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(s in text for s in (
        "econnrefused",
        "connect_over_cdp",
        "retrieving websocket url",
        "browser has been closed",
        "browser closed",
        "target page, context or browser has been closed",
        "connection refused",
    ))


def retryable_browser_error(exc):
    """True for a pre-submit Playwright timeout, navigation failure, or dead Chrome.

    These are safe to retry only when nothing has been submitted yet. The
    caller must still check the ledger before retrying."""
    if isinstance(exc, JobTimeExpired) or browser_lost(exc):
        return True
    name = type(exc).__name__.lower()
    text = f"{name}: {exc}".lower()
    if "timeouterror" in name:
        return True
    return any(s in text for s in (
        "ms exceeded",
        "page.goto",
        "page.reload",
        "wait_for_load_state",
        "navigation failed",
        "navigation timeout",
        "net::err_",
        "err_connection",
        "err_aborted",
        "err_timed_out",
        "frame was detached",
        "execution context was destroyed",
        "target closed",
        "connection closed",
        "job exceeded the",
    ))


def ensure_browser():
    """Bring the plain Chrome back if it died between pickups. No-op when
    the worker is not using AMY_BROWSER_LAUNCH=plain."""
    try:
        import browser_host
        browser_host.ensure()
    except Exception:  # noqa: BLE001
        log.exception("Could not ensure the booking browser is running")


# --- Entry point -------------------------------------------------------------

def check_config(mode):
    problems = []
    if not os.environ.get("AMY_QUEUE_API_TOKEN"):
        problems.append("AMY_QUEUE_API_TOKEN is required")
    if mode == "live" and not os.environ.get("BRANDED_SEND_TOKEN"):
        problems.append("BRANDED_SEND_TOKEN is required in live mode")
    if os.environ.get("AMY_SIGNUP_MODE", "").strip().lower() == "assisted":
        problems.append("AMY_SIGNUP_MODE=assisted must be unset for the worker")
    if mode == "live" and os.environ.get("AMY_WORKER_LIVE_CONFIRM", "") != "yes":
        problems.append("live mode also needs AMY_WORKER_LIVE_CONFIRM=yes (explicit go-live switch)")
    return problems


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", action="store_true", help="keep running")
    parser.add_argument("--mode", choices=("dry-run", "live"), default=None)
    parser.add_argument("--probe", action="store_true",
                        help="only check TAO reachability/Cloudflare from this host, then idle")
    parser.add_argument("--probe-speakeasy", action="store_true",
                        help="only log into SpeakeasyGo and open one guest list (read-only)")
    args = parser.parse_args(argv)
    import browser_host
    browser_host.start()  # no-op unless AMY_BROWSER_LAUNCH=plain
    if args.probe_speakeasy:
        try:
            result = probe_speakeasy()
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        log.info("SPEAKEASY PROBE RESULT %s", json.dumps(result, default=str))
        return result
    if args.probe:
        os.environ.setdefault("TAO_CHALLENGE_RETRIES", "15")
        os.environ.setdefault("TAO_CHALLENGE_WAIT_SECONDS", "20")
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        log.info("PROBE RESULT %s", json.dumps(result, default=str))
        if args.loop:  # keep the container up so the log can be read; no further work
            while True:
                time.sleep(3600)
        return result
    mode = args.mode or os.environ.get("AMY_WORKER_MODE", "dry-run")
    if mode not in ("dry-run", "live"):
        sys.exit("AMY_WORKER_MODE must be dry-run or live")
    # Cloudflare: wait it out (default ~5 min), never click or bypass it.
    os.environ.setdefault("TAO_CHALLENGE_RETRIES", "15")
    os.environ.setdefault("TAO_CHALLENGE_WAIT_SECONDS", "20")
    problems = check_config(mode)
    if problems:
        sys.exit("; ".join(problems))
    work_dir = Path(os.environ.get("AMY_WORKER_DIR", "/data/worker"))
    worker = Worker(Api.from_env(), Ledger(work_dir / "ledger.json"), mode=mode, evidence_dir=work_dir)
    idle = max(int(os.environ.get("AMY_WORKER_IDLE_SECONDS", "120")), 30)
    log.info("AMY booking worker started in %s mode", mode)
    if mode == "live":
        return live_loop(worker, work_dir, loop=args.loop)
    while True:
        try:
            result = worker.run_once()
        except Exception:  # noqa: BLE001
            log.exception("Worker cycle failed")
            result = {"status": "error"}
        log.info("cycle: %s", json.dumps(result, default=str)[:2000])
        if not args.loop:
            return result
        time.sleep(max(idle, 1800))


if __name__ == "__main__":
    main()

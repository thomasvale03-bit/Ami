"""Box-worker stall watchdog (Thomas, Nov 11 2026).

If a pickup writes no heartbeat/progress for AMY_WORKER_STALL_SECONDS
(default 20 minutes), the supervisor kills the hung worker, releases the
worker-held TAO lock, requeues every claimed job that was never submitted
(ledger is the source of truth; submitted jobs are never resubmitted and
are finished with emails instead), records the event, and starts a catch-up
run immediately via the existing run_now file.

Log line prefix: STALL_RESTART
Event log: <AMY_WORKER_DIR>/stall_events.jsonl
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("stall_watchdog")

STALL_PREFIX = "STALL_RESTART"
STALL_SECONDS = int(os.environ.get("AMY_WORKER_STALL_SECONDS", "1200"))
POLL_SECONDS = float(os.environ.get("AMY_WORKER_STALL_POLL", "15"))
HEARTBEAT_NAME = "run_heartbeat.json"
EVENTS_NAME = "stall_events.jsonl"
PHOENIX = timezone(timedelta(hours=-7), "MST")


def work_dir(path=None):
    return Path(path or os.environ.get("AMY_WORKER_DIR", "/workspace/amy-worker/data"))


def _heartbeat_path(directory):
    return work_dir(directory) / HEARTBEAT_NAME


def _events_path(directory):
    return work_dir(directory) / EVENTS_NAME


def read_heartbeat(directory, *, clock=time.time):
    path = _heartbeat_path(directory)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_heartbeat(directory, data):
    directory = work_dir(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / HEARTBEAT_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        log.warning("could not write stall heartbeat under %s", directory)


def start_run(directory, slot, *, clock=time.time):
    now = clock()
    _write_heartbeat(directory, {
        "status": "running",
        "slot": slot,
        "started_at": now,
        "last_progress": now,
        "ref": None,
    })


def mark_progress(directory, ref=None, *, clock=time.time):
    data = read_heartbeat(directory) or {}
    if data.get("status") != "running":
        return
    data["last_progress"] = clock()
    if ref:
        data["ref"] = ref
    _write_heartbeat(directory, data)


def end_run(directory):
    path = _heartbeat_path(directory)
    data = read_heartbeat(directory) or {}
    data["status"] = "idle"
    data["ref"] = None
    try:
        _write_heartbeat(directory, data)
    except OSError:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def is_stalled(directory, *, now=None, stall_seconds=None):
    """True only when a run is active and last_progress is older than the limit.

    A long healthy pickup that keeps writing progress is not a stall.
    """
    data = read_heartbeat(directory)
    if not data or data.get("status") != "running":
        return False
    last = data.get("last_progress") or data.get("started_at")
    if last is None:
        return False
    age = (time.time() if now is None else now) - float(last)
    limit = STALL_SECONDS if stall_seconds is None else stall_seconds
    return age >= limit


def ledger_for_ref(ledger, ref):
    """Return (booked_entries, submitting_entries, email_receipts) for one job."""
    booked, submitting = [], []
    emails = {}
    data = ledger._load() if hasattr(ledger, "_load") else {}
    prefix = f"{ref}|"
    for key, value in data.items():
        if not key.startswith(prefix):
            continue
        if key == f"{ref}|emails":
            emails = (value or {}).get("receipts") or {}
            continue
        state = (value or {}).get("state")
        record = (value or {}).get("record") or {}
        if state in ("booked", "email_only") and record.get("verified"):
            booked.append({"key": key, "record": record, "meta": (value or {}).get("meta") or {}})
        elif state == "submitting" and not record.get("verified"):
            submitting.append({"key": key, "value": value})
    return booked, submitting, emails


def release_worker_lock(lock_dir=None):
    """Drop a lock held by the booking worker. Never release someone else's lock."""
    try:
        from tools import browser_lock
    except ImportError:
        lock_dir = Path(lock_dir or os.environ.get("TAO_LOCK_DIR", "/workspace/.tao_browser.lock"))
        owner = lock_dir / "owner"
        try:
            holder = owner.read_text().splitlines()[0]
        except (OSError, IndexError):
            return None
        if holder.startswith("worker:"):
            import shutil
            shutil.rmtree(lock_dir, ignore_errors=True)
            return holder
        return None
    directory = lock_dir or os.environ.get("TAO_LOCK_DIR", browser_lock.LOCK_DIR)
    holder, _ = browser_lock._owner(directory)
    if holder.startswith("worker:"):
        browser_lock.release(holder, lock_dir=directory)
        return holder
    return None


def request_catch_up(directory):
    """The restarted worker picks this up immediately (existing run_now path)."""
    directory = work_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_now").touch()


def record_stall(directory, *, slot, duration, requeued, clock=time.time, logger=None):
    """Write the STALL_RESTART log line and append one JSONL event."""
    logger = logger or log
    ids = list(requeued or [])
    logger.warning(
        "%s slot=%s duration=%ss requeued=%s",
        STALL_PREFIX, slot or "unknown", int(duration), ",".join(ids) or "none",
    )
    directory = work_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = clock()
    event = {
        "ts": datetime.fromtimestamp(now, PHOENIX).isoformat(),
        "slot": slot,
        "duration": int(duration),
        "requeued": ids,
    }
    path = directory / EVENTS_NAME
    with path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return event


def _stall_requeue(api, ref, reason):
    if hasattr(api, "stall_requeue"):
        return api.stall_requeue(ref, reason)
    if hasattr(api, "_call"):
        code, body = api._call(
            "POST", "/queue/stall-requeue/" + quote(ref, safe=""),
            {"reason": reason}, getattr(api, "queue_token", None),
        )
        if code == 404 and hasattr(api, "_call"):
            # Live API may already have owner requeue even if stall-requeue is not deployed yet.
            code, body = api._call(
                "POST", "/queue/requeue/" + quote(ref, safe=""),
                {"owner_approval": "pt907u"}, getattr(api, "queue_token", None),
            )
        return code, body
    raise RuntimeError("api cannot stall-requeue")


def _assigned_with_tokens(api):
    """Return in-flight assigned jobs with claim tokens, without claiming a new job."""
    code, body = api.jobs("assigned", 100)
    if code != 200 or not (body or {}).get("jobs"):
        return []
    ccode, cbody = api.claim()
    if ccode != 200:
        log.warning("stall recover: claim for assigned jobs HTTP %s", ccode)
        return []
    return (cbody or {}).get("jobs") or []


def _finish_submitted(worker, job, booked):
    """Send guest/team emails for already-booked nights and complete if the job is whole."""
    import booking_worker as bw
    ref, token, raw = job["ref"], job.get("claim_token"), job.get("raw") or {}
    bookings = [{"record": item["record"], "meta": item.get("meta") or {}} for item in booked]
    evidence = {"verified": True, "worker": "railway", "bookings": [b["record"] for b in bookings]}
    evidence["email_receipts"] = worker._send_emails(job, ref, token, raw, bookings)
    needed = set(bw.source_nights(raw))
    have = {(b["record"].get("venue"), b["record"].get("date")) for b in bookings}
    if needed - have:
        worker.api.update(
            ref, token, "manual",
            {"worker": "railway", "evidence": evidence, "progress": {"bookings": {
                f"{r['venue']}|{r['date']}": r for r in evidence["bookings"]
            }}},
            "Stall watchdog: some nights booked; remaining nights need review; DO NOT rebook",
        )
        return "partial"
    code, body = worker.api.update(ref, token, "complete", {"evidence": evidence}, "Booked by Railway worker")
    if code != 200:
        worker.api.update(
            ref, token, "manual",
            {"worker": "railway", "evidence": evidence},
            f"Stall watchdog: booked and emailed but completion refused (HTTP {code}); DO NOT rebook",
        )
        return "partial"
    return "finished"


def recover_assigned_jobs(api, ledger, *, worker=None, reason="Stall watchdog: never submitted"):
    """Requeue unsubmitted assigned jobs; finish submitted ones; never resubmit."""
    result = {"requeued": [], "finished": [], "uncertain": [], "left_manual": []}
    jobs = _assigned_with_tokens(api)
    for job in jobs:
        ref = job.get("ref")
        token = job.get("claim_token")
        if not ref:
            continue
        booked, submitting, _emails = ledger_for_ref(ledger, ref)
        try:
            if submitting and not booked:
                if token:
                    api.update(ref, token, "uncertain", {"worker": "railway"},
                               "Stall watchdog: a submit started with no confirmed result; check TAO, do not resubmit")
                result["uncertain"].append(ref)
                continue
            if booked:
                if worker is None:
                    result["left_manual"].append(ref)
                    continue
                outcome = _finish_submitted(worker, job, booked)
                (result["finished"] if outcome == "finished" else result["left_manual"]).append(ref)
                continue
            # Nothing submitted: put it back on the queue for the catch-up run.
            if token:
                api.update(ref, token, "manual", {"worker": "railway"}, reason)
            code, body = _stall_requeue(api, ref, reason)
            state = (body or {}).get("state") if isinstance(body, dict) else None
            if code == 200 and state == "queued":
                result["requeued"].append(ref)
            else:
                log.warning("stall requeue %s HTTP %s state=%s", ref, code, state)
                result["left_manual"].append(ref)
        except Exception:  # noqa: BLE001
            log.exception("stall recover failed for %s", ref)
            result["left_manual"].append(ref)
    return result


def stop_process(proc, *, wait=10, sleep=time.sleep):
    """Terminate the worker, then kill if it does not exit."""
    if proc is None or proc.poll() is not None:
        return
    pid = getattr(proc, "pid", None) or 0
    def _signal(sig, fallback):
        if pid > 1:
            try:
                os.killpg(os.getpgid(pid), sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        fallback()
    try:
        _signal(signal.SIGTERM, proc.terminate)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    if wait > 0:
        deadline = time.time() + wait
        while proc.poll() is None and time.time() < deadline:
            sleep(0.2)
    if proc.poll() is None:
        try:
            _signal(signal.SIGKILL, proc.kill)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=min(wait, 5) if wait else 0)
        except Exception:  # noqa: BLE001
            pass


def restart_chrome_if_needed():
    try:
        import browser_host
        if os.environ.get("AMY_BROWSER_LAUNCH", "").lower() != "plain":
            return None
        if browser_host.healthy():
            return browser_host.restart()  # stuck page after a hang; start clean
        return browser_host.ensure()
    except Exception:  # noqa: BLE001
        log.exception("Chrome restart after stall failed")
        return None


def handle_stall(proc, directory, api, ledger, *, clock=time.time, worker=None, sleep=time.sleep):
    hb = read_heartbeat(directory) or {}
    slot = hb.get("slot")
    started = hb.get("started_at")
    duration = (clock() - started) if started else 0
    stop_process(proc, wait=0 if sleep is not time.sleep else 10, sleep=sleep)
    recovery = {"requeued": [], "finished": [], "uncertain": [], "left_manual": []}
    try:
        recovery = recover_assigned_jobs(api, ledger, worker=worker)
    except Exception:  # noqa: BLE001
        log.exception("stall job recovery failed")
    try:
        release_worker_lock()
    except Exception:  # noqa: BLE001
        log.exception("stall lock release failed")
    try:
        restart_chrome_if_needed()
    except Exception:  # noqa: BLE001
        log.exception("stall chrome restart failed")
    record_stall(directory, slot=slot, duration=duration, requeued=recovery.get("requeued") or [], clock=clock)
    request_catch_up(directory)
    end_run(directory)
    return recovery


def _start_worker():
    return subprocess.Popen(
        [sys.executable, "booking_worker.py", "--loop"],
        start_new_session=True,
    )


def supervise(directory=None, *, stall_seconds=None, poll=None, clock=time.time, sleep=time.sleep,
              start_worker=_start_worker, recover=None):
    """Run the booking worker as a child and restart it if a pickup stalls."""
    directory = work_dir(directory)
    limit = STALL_SECONDS if stall_seconds is None else stall_seconds
    interval = POLL_SECONDS if poll is None else poll
    log.info("Stall watchdog on; will restart after %ss with no pickup progress", limit)
    while True:
        proc = start_worker()
        while proc.poll() is None:
            if is_stalled(directory, now=clock(), stall_seconds=limit):
                log.warning("Pickup stalled; supervisor is restarting the worker")
                if recover:
                    recover(proc, directory)
                else:
                    import booking_worker as bw
                    api = bw.Api.from_env()
                    ledger = bw.Ledger(directory / "ledger.json")
                    try:
                        worker = bw.Worker(api, ledger, mode="live", evidence_dir=directory,
                                           email_wait_seconds=120, duplicate_check=False, alerts=False)
                    except Exception:  # noqa: BLE001
                        worker = None
                    handle_stall(proc, directory, api, ledger, clock=clock, worker=worker, sleep=sleep)
                break
            sleep(interval)
        else:
            log.info("booking_worker exited %s", proc.returncode)
            return proc.returncode


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--supervise"] or not args:
        return supervise()
    raise SystemExit("usage: stall_watchdog.py --supervise")


if __name__ == "__main__":
    sys.exit(main() or 0)

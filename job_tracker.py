"""Persistent AMYI job ledger.

One row per guest job (keyed on ref), stored as JSON next to posh_webhook's
pending store (AMY_PENDING_DIR / Railway volume /data/pending), so it survives
redeploys. Fields: ref, order_id, guest, email, venue, dates, queued_at,
webhook_status, attempts, done_at, plus watchdog state (refired_at,
alerted_at, manual_at, status, payload)."""
import json
import logging
import os
import threading
import time

log = logging.getLogger("amy.jobs")
_lock = threading.Lock()

REFIRE_AFTER = 3 * 60
ALERT_AFTER = 5 * 60
MANUAL_AFTER = 15 * 60
MAX_FAILURES = 3


def _path():
    if os.environ.get("AMY_JOBS_FILE"):
        return os.environ["AMY_JOBS_FILE"]
    import posh_webhook
    return os.path.join(posh_webhook.pending_dir(), "jobs.json")


def _load():
    try:
        with open(_path()) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(rows):
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rows, fh, default=str, indent=1)
    os.replace(tmp, path)


def _dates(dates):
    if isinstance(dates, str):
        dates = [d.strip() for d in dates.split(",")]
    return sorted({str(d).strip() for d in (dates or []) if d and str(d).strip()})


def record(ref, order_id="", guest="", email="", venue="", dates=(), now=None):
    """Create (or return the existing) row for ref. Never raises."""
    if not ref:
        return None
    try:
        with _lock:
            rows = _load()
            row = rows.get(ref)
            if row is None:
                if isinstance(venue, (list, tuple, set)):
                    venue = ", ".join(sorted(str(v) for v in venue if v))
                row = {"ref": ref, "order_id": str(order_id or ""), "guest": guest or "",
                       "email": email or "", "venue": venue or "", "dates": _dates(dates),
                       "queued_at": now if now is not None else time.time(), "webhook_status": None,
                       "attempts": 0, "failures": 0, "done_at": None, "status": "queued"}
                rows[ref] = row
                _save(rows)
                log.info("Job %s queued (%s, %s)", ref, row["guest"], ", ".join(row["dates"]))
            return dict(row)
    except Exception as e:
        log.warning("Job ledger record failed for %s: %s", ref, e)
        return None


def update(ref, **fields):
    try:
        with _lock:
            rows = _load()
            if ref not in rows:
                return None
            rows[ref].update(fields)
            _save(rows)
            return dict(rows[ref])
    except Exception as e:
        log.warning("Job ledger update failed for %s: %s", ref, e)
        return None


def webhook_result(ref, status, attempts, ok, payload=None):
    """Write one webhook delivery outcome onto the row (if tracked)."""
    try:
        with _lock:
            rows = _load()
            row = rows.get(ref)
            if row is None:
                return None
            row["webhook_status"] = status
            row["attempts"] = int(row.get("attempts") or 0) + int(attempts)
            if not ok:
                row["failures"] = int(row.get("failures") or 0) + 1
            if payload is not None:
                row["payload"] = payload
            _save(rows)
            return dict(row)
    except Exception as e:
        log.warning("Job ledger webhook write failed for %s: %s", ref, e)
        return None


def mark_done(ref, now=None):
    row = update(ref, done_at=now if now is not None else time.time(), status="done")
    if row:
        log.info("Job %s marked done", ref)
    return row


def get(ref):
    return _load().get(ref)


def all_rows():
    return _load()


def watchdog(refire, alert, escalate, now=None):
    """One pass over open jobs. Callbacks take the row dict.
    ~3 min: re-fire once; ~5 min: alert once; 15 min or 3 failures: escalate
    and mark manual. Returns list of (ref, action)."""
    now = now if now is not None else time.time()
    actions = []
    for ref, row in list(_load().items()):
        if row.get("done_at") or row.get("status") in ("done", "manual"):
            continue
        queued = row.get("queued_at")
        age = now - float(queued if queued is not None else now)
        if row.get("status") == "pending_event":
            # Event not posted on TAO yet: AMYI books later; only delivery failures escalate.
            age = 0
        try:
            if age >= MANUAL_AFTER or int(row.get("failures") or 0) >= MAX_FAILURES:
                escalate(row)
                update(ref, status="manual", manual_at=now)
                actions.append((ref, "manual"))
            elif age >= ALERT_AFTER and not row.get("alerted_at"):
                alert(row)
                update(ref, alerted_at=now)
                actions.append((ref, "alert"))
            elif age >= REFIRE_AFTER and not row.get("refired_at"):
                update(ref, refired_at=now)
                refire(row)
                actions.append((ref, "refire"))
        except Exception:
            log.exception("Job watchdog failed for %s", ref)
    return actions


def has_open():
    return any(not r.get("done_at") and r.get("status") not in ("done", "manual", "pending_event")
               for r in _load().values())

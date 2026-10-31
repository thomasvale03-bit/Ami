"""Fire-and-forget pings to AMYI's routine webhook.

Set AMYI_WEBHOOK_URL and AMYI_WEBHOOK_AUTH (full Authorization header value).
Unset URL = no-op. Runs on a daemon thread; never raises into the caller.

Guards (each ping wakes a costly routine):
- never under unittest/pytest (or AMYI_DISABLED=true);
- never for test traffic: test=True, empty email, example.com/.test emails,
  or test/sample names;
- WAKE RULE (Tom, Oct 30 2026): every inbound Posh or website webhook wakes
  AMYI immediately. The ONLY dedupe is the same Posh order number (a Posh
  retry of the same order), persisted to AMYI_DEDUPE_FILE (default
  data/amyi_sent.json). Different order numbers from the same guest each
  wake; website submissions (no order number) always wake. The old 24h
  per-guest (ref / email+dates / party) dedupe from 60c2e3b is removed;
- up to 3 retries (~5s, 30s, 2min); the dedupe claim is written only after
  a 2xx, and every outcome (real HTTP status + body) is logged and written to
  the job ledger (job_tracker)."""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("amy.amyi")

DEDUPE_SECONDS = 30 * 24 * 3600  # how long a Posh order number is remembered
_lock = threading.Lock()
_sent = None  # {key: epoch}
TEST_NAME_RE = re.compile(r"\b(test|testing|sample|example|demo|dummy|fake|jane doe|john doe)\b", re.I)


def _under_tests():
    return ("pytest" in sys.modules or "unittest" in sys.modules and any(
        "unittest" in a or "pytest" in a for a in sys.argv) or bool(os.environ.get("PYTEST_CURRENT_TEST")))


def disabled():
    return (os.environ.get("AMYI_DISABLED", "").lower() in ("1", "true", "yes")
            or _under_tests() or not os.environ.get("AMYI_WEBHOOK_URL"))


def is_test_traffic(name, email, test=False):
    if test:
        return True
    e = (email or "").strip().lower()
    if not e or e.endswith("example.com") or e.endswith(".test") or e.endswith(".example"):
        return True
    return bool(TEST_NAME_RE.search(name or ""))


def _dedupe_file():
    return os.environ.get("AMYI_DEDUPE_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "amyi_sent.json")


def _load():
    global _sent
    if _sent is None:
        try:
            with open(_dedupe_file()) as fh:
                _sent = {k: float(v) for k, v in json.load(fh).items()}
        except Exception:
            _sent = {}
    return _sent


def _save(sent):
    try:
        path = _dedupe_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sent, fh)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("AMYI dedupe store not saved: %s", type(e).__name__)


def _keys(payload):
    """Dedupe keys: only the Posh order number. No order => no dedupe."""
    order = str(payload.get("order") or "").strip()
    return [f"order|{order}"] if order else []


def _party_keys(payload):  # kept for back-compat; party no longer dedupes
    return []


def is_duplicate(payload, now=None):
    """True if this ping already went out (2xx) in the last 24h, or one for the
    same keys is in flight. Writes nothing."""
    now = now or time.time()
    with _lock:
        sent = _load()
        for k in [k for k, ts in sent.items() if now - ts > DEDUPE_SECONDS]:
            del sent[k]
        keys = _keys(payload)
        return any(k in sent or k in _inflight for k in keys)


def record_sent(payload, now=None):
    """Write the dedupe claim (call only after a 2xx)."""
    now = now or time.time()
    with _lock:
        sent = _load()
        for k in _keys(payload) + _party_keys(payload):
            sent[k] = now
        _save(sent)


def release(payload):
    """Drop any claim for this payload (after a failed delivery)."""
    with _lock:
        sent = _load()
        for k in _keys(payload) + _party_keys(payload):
            sent.pop(k, None)
        _save(sent)


def claim(payload, now=None):
    """Back-compat: True (and record it) if not sent in the last 24h."""
    if is_duplicate(payload, now):
        return False
    record_sent(payload, now)
    return True


RETRY_DELAYS = (5, 30, 120)
_inflight = set()


def _post_once(payload, opener=None):
    """One HTTP attempt. Returns (status or None, body text)."""
    url = os.environ.get("AMYI_WEBHOOK_URL")
    if not url:
        return None, "AMYI_WEBHOOK_URL unset"
    headers = {"Content-Type": "application/json"}
    auth = (os.environ.get("AMYI_WEBHOOK_AUTH") or "").strip()
    if auth.lower().startswith("authorization:"):  # tolerate a full "Authorization: ..." line
        auth = auth.split(":", 1)[1].strip()
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                 headers=headers, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as resp:
            return resp.status, resp.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def deliver(payload, _sleep=time.sleep, opener=None, delays=RETRY_DELAYS):
    """POST with retries. Claim written only on 2xx; released on failure.
    Outcome logged and written to the job ledger. Returns True on 2xx."""
    import job_tracker
    keys = _keys(payload) + _party_keys(payload)
    with _lock:
        _inflight.update(keys)
    status, body, attempts = None, "", 0
    try:
        for i in range(len(delays) + 1):
            if i:
                _sleep(delays[i - 1])
            attempts += 1
            status, body = _post_once(payload, opener)
            log.info("AMYI ping %s ref=%s attempt %d -> HTTP %s body=%r",
                     payload.get("type"), payload.get("ref"), attempts, status, (body or "")[:500])
            if status is not None and 200 <= status < 300:
                break
        ok = status is not None and 200 <= status < 300
        try:
            if ok:
                record_sent(payload)
            else:
                release(payload)
                log.warning("AMYI ping %s ref=%s FAILED after %d attempts (last HTTP %s)",
                            payload.get("type"), payload.get("ref"), attempts, status)
        except Exception as e:
            log.warning("AMYI dedupe write failed: %s", e)
    finally:
        with _lock:
            _inflight.difference_update(keys)
    job_tracker.webhook_result(payload.get("ref") or "", status, attempts, ok, payload=payload)
    return ok


def _post(payload):
    try:
        return deliver(payload)
    except Exception as e:  # never break the caller
        log.warning("AMYI ping %s failed: %s", payload.get("type"), type(e).__name__)
        return False


def refire(payload):
    """Watchdog re-fire: one more delivery (with retries) on a daemon thread."""
    if disabled() or not payload:
        return False
    threading.Thread(target=_post, args=(payload,), daemon=True).start()
    return True


def _norm_dates(dates):
    if isinstance(dates, str):
        dates = [d.strip() for d in dates.split(",")]
    return ", ".join(sorted({str(d).strip() for d in dates if d and str(d).strip()}))


def build_payload(type, name="", email="", venue="", dates="", ref="",
                  phone="", event="", party="", source="", order=""):
    dates = _norm_dates(dates or [])
    if isinstance(venue, (list, tuple)):
        venue = ", ".join(str(v) for v in venue if v)
    return {"type": type, "name": name or "", "email": email or "", "phone": str(phone or ""),
            "event": event or "", "venue": venue or "", "dates": str(dates or ""),
            "date": str(dates or ""), "party": str(party or ""),
            "source": source or "", "ref": str(ref or ""),
            "order": str(order or "").strip()}


def notify(type, name="", email="", venue="", dates="", ref="", test=False, wait=False,
           phone="", event="", party="", source="", order=""):
    """Returns True if a ping was dispatched."""
    try:
        if disabled():
            return False
        if is_test_traffic(name, email, test):
            log.info("AMYI ping %s skipped (test traffic)", type)
            return False
        payload = build_payload(type, name, email, venue, dates, ref, phone, event, party, source, order)
        if is_duplicate(payload):
            log.info("AMYI ping %s skipped (Posh retry of same order %s)", type, payload["order"])
            return False
        log.info("AMYI wake %s order=%s ref=%s email=%s (immediate)", type,
                 payload["order"] or "-", payload["ref"] or "-", payload["email"])
        t = threading.Thread(target=_post, args=(payload,), daemon=True)
        t.start()
        if wait:
            t.join(15)
        return True
    except Exception:
        return False


# --- One ping per handled message --------------------------------------------
# handle_message() defers its contact_form ping; if the same message yields a
# signup_job, only the signup_job ping goes out.
_local = threading.local()


def begin_message():
    _local.pending = None
    _local.job_sent = False


def defer_contact_form(**kw):
    if getattr(_local, "job_sent", None) is None:
        return notify("contact_form", **kw)
    _local.pending = kw
    return False


def mark_job():
    _local.job_sent = True


def end_message():
    pending, job = getattr(_local, "pending", None), getattr(_local, "job_sent", False)
    _local.pending = None
    _local.job_sent = None
    if pending and not job:
        return notify("contact_form", **pending)
    return False

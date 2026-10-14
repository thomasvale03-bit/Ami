"""Fire-and-forget pings to AMYI's routine webhook.

Set AMYI_WEBHOOK_URL and AMYI_WEBHOOK_AUTH (full Authorization header value).
Unset URL = no-op. Runs on a daemon thread; never raises into the caller.

Guards (each ping wakes a costly routine):
- never under unittest/pytest (or AMYI_DISABLED=true);
- never for test traffic: test=True, empty email, example.com/.test emails,
  or test/sample names;
- at most once per 24h per type+ref and per type+email+venue+dates
  (persisted to AMYI_DEDUPE_FILE, default data/amyi_sent.json);
- a single attempt, no retries."""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request

log = logging.getLogger("amy.amyi")

DEDUPE_SECONDS = 24 * 3600
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
    t = payload["type"]
    keys = []
    if payload.get("ref"):
        keys.append(f"ref|{t}|{payload['ref']}")
    keys.append(f"ev|{t}|{payload['email'].lower()}|{payload['venue'].lower()}|{payload['dates']}")
    return keys


def claim(payload, now=None):
    """True (and record it) if this ping wasn't sent in the last 24h."""
    now = now or time.time()
    with _lock:
        sent = _load()
        for k in [k for k, ts in sent.items() if now - ts > DEDUPE_SECONDS]:
            del sent[k]
        keys = _keys(payload)
        if any(k in sent for k in keys):
            return False
        for k in keys:
            sent[k] = now
        _save(sent)
        return True


def _post(payload):
    """One attempt only; no retries."""
    try:
        url = os.environ.get("AMYI_WEBHOOK_URL")
        if not url:
            return
        headers = {"Content-Type": "application/json"}
        auth = (os.environ.get("AMYI_WEBHOOK_AUTH") or "").strip()
        if auth.lower().startswith("authorization:"):  # tolerate a full "Authorization: ..." line
            auth = auth.split(":", 1)[1].strip()
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("AMYI ping %s -> %s", payload.get("type"), resp.status)
    except Exception as e:  # never break the caller
        log.warning("AMYI ping %s failed: %s", payload.get("type"), type(e).__name__)


def build_payload(type, name="", email="", venue="", dates="", ref=""):
    if isinstance(dates, (list, tuple)):
        dates = ", ".join(str(d) for d in dates if d)
    if isinstance(venue, (list, tuple)):
        venue = ", ".join(str(v) for v in venue if v)
    return {"type": type, "name": name or "", "email": email or "",
            "venue": venue or "", "dates": str(dates or ""), "ref": str(ref or "")}


def notify(type, name="", email="", venue="", dates="", ref="", test=False, wait=False):
    """Returns True if a ping was dispatched."""
    try:
        if disabled():
            return False
        if is_test_traffic(name, email, test):
            log.info("AMYI ping %s skipped (test traffic)", type)
            return False
        payload = build_payload(type, name, email, venue, dates, ref)
        if not claim(payload):
            log.info("AMYI ping %s skipped (duplicate within 24h)", type)
            return False
        t = threading.Thread(target=_post, args=(payload,), daemon=True)
        t.start()
        if wait:
            t.join(15)
        return True
    except Exception:
        return False

"""Fire-and-forget pings to AMYI's routine webhook.

Set AMYI_WEBHOOK_URL and AMYI_WEBHOOK_AUTH (full Authorization header value).
Unset URL = no-op. Runs on a daemon thread; never raises into the caller."""
import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger("amy.amyi")


def _post(payload):
    try:
        url = os.environ.get("AMYI_WEBHOOK_URL")
        if not url:
            return
        headers = {"Content-Type": "application/json"}
        auth = os.environ.get("AMYI_WEBHOOK_AUTH")
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("AMYI ping %s -> %s", payload.get("type"), resp.status)
    except Exception as e:  # never break the caller
        log.warning("AMYI ping %s failed: %s", payload.get("type"), type(e).__name__)


def notify(type, name="", email="", venue="", dates="", ref="", test=False, wait=False):
    try:
        if isinstance(dates, (list, tuple)):
            dates = ", ".join(str(d) for d in dates if d)
        if isinstance(venue, (list, tuple)):
            venue = ", ".join(str(v) for v in venue if v)
        payload = {"type": type, "name": name or "", "email": email or "",
                   "venue": venue or "", "dates": str(dates or ""), "ref": str(ref or "")}
        if test:
            payload["test"] = True
        t = threading.Thread(target=_post, args=(payload,), daemon=True)
        t.start()
        if wait:
            t.join(15)
    except Exception:
        pass

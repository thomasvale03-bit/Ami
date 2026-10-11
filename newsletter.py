"""Playmaker newsletter opt-in / opt-out.

Per-guest signed links (HMAC-SHA256 with the NEWSLETTER_LINK_SECRET Railway
variable) go in every follow-up:
    GET /newsletter/optin?e=<email>&n=<name>&t=<sig>
    GET /unsubscribe?e=<email>&n=<name>&t=<sig>   (+ POST for one-click)
Each click records {email, name, status, timestamp, source}.

Store: Railway's disk is throwaway, so (like the Posh webhook) records live
in the intake Gmail mailbox: one inserted message per change, subject
"AMY NEWSLETTER STATUS <status> <email>", JSON body, label Amy/Newsletter.
Nothing is emailed to anyone. The newest record per email wins.
GET /newsletter/export (BRANDED_SEND_TOKEN) returns them as JSON or CSV.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

log = logging.getLogger("amy.newsletter")
SUBJECT_PREFIX = "AMY NEWSLETTER STATUS"
LABEL = "Amy/Newsletter"
STATUSES = ("opted_in", "opted_out")
ACTIONS = {"optin": "opted_in", "unsubscribe": "opted_out"}
DEFAULT_BASE = "https://amy-production-1616.up.railway.app"
CACHE_SECONDS = 300


def secret():
    return os.environ.get("NEWSLETTER_LINK_SECRET", "").strip()


def base_url():
    return (os.environ.get("AMY_PUBLIC_URL", "").strip() or DEFAULT_BASE).rstrip("/")


def norm(email):
    return (email or "").strip().lower()


def sign(email, action):
    key = secret()
    if not key:
        raise RuntimeError("NEWSLETTER_LINK_SECRET is not set")
    return hmac.new(key.encode(), f"{action}:{norm(email)}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(email, action, sig):
    if not (secret() and email and sig) or action not in ACTIONS:
        return False
    return hmac.compare_digest(sign(email, action), sig.strip())


def link(email, action, name=""):
    path = "/newsletter/optin" if action == "optin" else "/unsubscribe"
    q = {"e": norm(email), "t": sign(email, action)}
    if name:
        q["n"] = name
    return f"{base_url()}{path}?{urlencode(q)}"


def follow_up_links(email, name=""):
    """(optin_url, unsubscribe_url), or None when no secret is configured."""
    if not secret() or not email:
        return None
    return link(email, "optin", name), link(email, "unsubscribe", name)


def list_unsubscribe_headers(unsubscribe_url, mailto="info@playmakerentertainment.com"):
    return {"List-Unsubscribe": f"<{unsubscribe_url}>, <mailto:{mailto}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


class GmailStore:
    """Status records as inserted messages in the intake mailbox."""

    def __init__(self, service_factory, intake_email):
        self._factory, self._service, self.intake_email = service_factory, None, intake_email
        self._lock = threading.Lock()
        self._cache, self._cache_at, self._label = None, 0.0, None

    def service(self):
        if self._service is None:
            self._service = self._factory()
        return self._service

    def _label_id(self):
        if self._label is None:
            import gmail_client
            try:
                self._label = gmail_client.get_or_create_label(self.service(), LABEL)
            except Exception:  # noqa: BLE001 - a label is nice to have, not required
                log.exception("Could not create %s label", LABEL)
                self._label = ""
        return self._label

    def record(self, email, name, status, source):
        assert status in STATUSES
        rec = {"email": norm(email), "name": (name or "").strip()[:120], "status": status,
               "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "source": (source or "")[:60]}
        msg = EmailMessage()
        msg["To"] = msg["From"] = self.intake_email
        msg["Subject"] = f"{SUBJECT_PREFIX} {status} {rec['email']}"
        msg.set_content(json.dumps(rec))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        labels = [x for x in (self._label_id(),) if x]
        with self._lock:
            self.service().users().messages().insert(
                userId="me", body={"raw": raw, "labelIds": labels}).execute()
            if self._cache is not None:
                self._cache[rec["email"]] = rec
        log.info("Newsletter status %s for %s (%s)", status, rec["email"], source)
        return rec

    def _load(self):
        import gmail_client
        svc, out, token = self.service(), {}, None
        recs = []
        while True:
            kw = {"userId": "me", "q": f'subject:"{SUBJECT_PREFIX}" -in:trash', "maxResults": 500}
            if token:
                kw["pageToken"] = token
            resp = svc.users().messages().list(**kw).execute()
            for ref in resp.get("messages", []):
                _m, body = gmail_client.get_plain_text_body(svc, ref["id"])
                try:
                    rec = json.loads((body or "").strip())
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("email") and rec.get("status") in STATUSES:
                    recs.append(rec)
            token = resp.get("nextPageToken")
            if not token:
                break
        for rec in sorted(recs, key=lambda r: r.get("timestamp", "")):
            out[norm(rec["email"])] = rec
        return out

    def all(self, refresh=False):
        with self._lock:
            if refresh or self._cache is None or time.time() - self._cache_at > CACHE_SECONDS:
                self._cache, self._cache_at = self._load(), time.time()
            return dict(self._cache)

    def status(self, email):
        rec = self.all().get(norm(email))
        return rec["status"] if rec else None

    def is_opted_out(self, email):
        return self.status(email) == "opted_out"


def export(records, fmt="json"):
    rows = sorted(records.values(), key=lambda r: r.get("timestamp", ""))
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["email", "name", "status", "timestamp", "source"],
                           extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        return "text/csv; charset=utf-8", buf.getvalue()
    return "application/json", json.dumps({"count": len(rows), "records": rows})


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} | Playmaker Entertainment</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;700&display=swap" rel="stylesheet">
<style>body{{margin:0;background:#050505;color:#F5F5F5;font-family:Inter,Helvetica,Arial,sans-serif}}
.c{{max-width:560px;margin:48px auto;background:#0d0d0d;border:1px solid #2b2414;border-radius:14px;overflow:hidden;text-align:center}}
.bar{{height:4px;background:linear-gradient(90deg,#AA6C08,#F7DD72,#D4AF37,#AA6C08)}}
.p{{padding:36px 32px}}.k{{font-size:12px;letter-spacing:4px;text-transform:uppercase;color:#D4AF37;font-weight:700;margin:0 0 12px}}
h1{{font-family:'Playfair Display',Georgia,serif;font-size:32px;line-height:38px;margin:0}}
p.b{{color:#BDBDBD;font-size:16px;line-height:24px;margin:16px 0 0}}
a.btn{{display:inline-block;margin-top:26px;padding:13px 34px;border-radius:30px;background:#D4AF37;color:#0d0d0d;font-weight:700;text-decoration:none}}
.f{{color:#5f5f5f;font-size:12px;padding:0 32px 28px}}</style></head>
<body><div class="c"><div class="bar"></div><div class="p">
<img src="https://isteam.wsimg.com/genai-assistant/logoagent/customer/86557a2a-428d-40d8-87e7-0710f78e0dfa/session/272ddf34-d38d-44d8-8ae9-33cd91a55c2f/horizontal-transparent-0582e683-5deb-43fb-80b9-0ac1ca56ae18/logo-logo.png/:/rs=w:560" width="220" alt="Playmaker Entertainment" style="max-width:100%;margin-bottom:24px">
<p class="k">{kicker}</p><h1>{title}</h1><p class="b">{body}</p>
<a class="btn" href="https://playmakerentertainment.com">Visit Playmaker</a></div>
<div class="f">Playmaker Entertainment &middot; Las Vegas</div></div></body></html>"""


def page(kind, name=""):
    import html as _h
    who = f", {_h.escape(name.split()[0])}" if name.strip() else ""
    if kind == "opted_in":
        return PAGE.format(kicker="You're in", title=f"Thank you{who}!",
                           body="You're signed up for the Playmaker newsletter. Look for it every 3 months "
                                "with upcoming events and specials.")
    if kind == "opted_out":
        return PAGE.format(kicker="Unsubscribed", title=f"You're unsubscribed{who}.",
                           body="We won't send you any more Playmaker follow-up or marketing emails. "
                                "Guest-list confirmations you request will still arrive.")
    return PAGE.format(kicker="Link problem", title="That link didn't work.",
                       body="This link is invalid or incomplete. Reply STOP to any Playmaker email "
                            "and we'll take care of it.")

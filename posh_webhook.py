"""Posh webhook -> Amy.

Posh (posh.vip, org Playmaker Entertainment) POSTs each new order to
    https://<railway-domain>/webhooks/posh?token=<POSH_WEBHOOK_TOKEN>

Design (robust on Railway's throwaway disk):
  * The handler checks the token, ignores cancelled / refunded / disputed /
    in-person orders, computes the CORRECT night (see posh_night below), and
    turns the order into the same "NEW POSH SIGNUP" text the Zapier email
    used to carry. It drops that message straight into the intake Gmail
    inbox (Gmail API messages.insert - nothing is emailed to anyone).
  * Amy's normal inbox loop then picks it up exactly like any request:
    posh.parse_signup -> normalize_guest_request -> routing -> assisted
    jobs / concierge emails, labels, and the existing Posh order-number
    dedupe. Gmail is the durable record, so restarts never double-book.
  * Dedupe before inserting: any "NEW POSH SIGNUP" message already in the
    inbox (handled or not) with the same order number, or the same event_id
    + email, means "already have it" and nothing is inserted.

Pure helpers here are unit-tested; the HTTP server lives in serve().
"""
import hmac
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import amyi
import newsletter
import posh
import posh_lookup

log = logging.getLogger("amy.posh_webhook")
VEGAS = ZoneInfo("America/Los_Angeles")
SUBJECT = "NEW POSH SIGNUP (webhook)"
WEBHOOK_MARKER = "SOURCE: POSH WEBHOOK"
SIGNATURE_HEADER_RE = re.compile(r"sig|signature|hmac|posh|webhook|secret", re.I)
TOKEN_HEADERS = ("x-amy-token", "x-webhook-token")
MAX_BODY = 256 * 1024


# --- auth -------------------------------------------------------------------

def expected_token():
    return os.environ.get("POSH_WEBHOOK_TOKEN", "").strip()


def token_ok(path, headers):
    """Shared token from ?token=, X-Amy-Token / X-Webhook-Token, or
    "Authorization: Bearer". Constant-time compare. No token set => reject."""
    want = expected_token()
    if not want:
        return False
    given = (parse_qs(urlparse(path).query).get("token") or [""])[0]
    for h in TOKEN_HEADERS:
        given = given or (headers.get(h) or "")
    auth = headers.get("authorization") or ""
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:]
    return hmac.compare_digest(given.strip().encode(), want.encode())


def signature_header_names(headers):
    """Names (never values) of headers that look like a Posh signature."""
    return sorted(k for k in headers if SIGNATURE_HEADER_RE.search(k) and k.lower() not in TOKEN_HEADERS)


# --- date logic -------------------------------------------------------------

def _wall_clock(value):
    """Posh event times: by default read the clock time as Las Vegas wall
    time, ignoring the 'Z' (what real Zapier orders showed: a 10:30 PM show
    arrives as T22:30Z). Set POSH_EVENT_START_IS_UTC=true if the webhook
    turns out to send true UTC."""
    if not value:
        return None
    try:
        if os.environ.get("POSH_EVENT_START_IS_UTC", "").strip().lower() in ("1", "true", "yes"):
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(VEGAS).replace(tzinfo=None)
        return datetime.fromisoformat(value.strip()[:19])
    except ValueError:
        return None


def _instant_in_vegas(value):
    """date_purchased is a real timestamp (UTC 'Z') -> naive Vegas time."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VEGAS).replace(tzinfo=None)


def _night(dt):
    return (dt - timedelta(days=1)).date() if dt.hour < posh.NIGHT_ROLLOVER_HOUR else dt.date()


_DATE_PROMPT = re.compile(r"date|night|day|when|visit", re.I)


def date_from_custom_fields(custom_fields, reference):
    """A date the guest typed into a Posh custom question, if any."""
    for f in custom_fields or []:
        if not _DATE_PROMPT.search(f.get("prompt") or ""):
            continue
        answer = (f.get("answer") or "").strip()
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", answer)
        if m:
            y, mo, d = map(int, m.groups())
        else:
            m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", answer)
            if not m:
                continue
            mo, d = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else reference.year
            y = y + 2000 if y < 100 else y
        try:
            found = datetime(y, mo, d).date()
        except ValueError:
            continue
        if not m.lastindex or m.lastindex < 3 or not m.group(3):
            if found < reference.date():  # "10/3" typed in December means next year
                found = found.replace(year=found.year + 1)
        return found
    return None


def posh_night(event_start, date_purchased=None, event_end=None, custom_fields=None):
    """(night, how) - the Las Vegas night this order is for.

    1. A date answer in custom_fields (prompt mentions date/night/day) wins.
    2. No purchase time, or the event starts on/after the purchase: use
       event_start as-is (a real one-off or a future occurrence).
    3. event_start is before the purchase => it's a recurring series'
       first date. Walk forward in whole weeks (same weekday + start time)
       to the first occurrence that hasn't finished at purchase time (an
       occurrence still running counts; its length comes from event_end,
       else 6 h).
    Starts before 6 AM belong to the previous night (posh.NIGHT_ROLLOVER_HOUR).
    """
    start = _wall_clock(event_start)
    bought = _instant_in_vegas(date_purchased)
    custom = date_from_custom_fields(custom_fields, bought or datetime.now(VEGAS).replace(tzinfo=None))
    if custom:
        return custom, "custom_field"
    if start is None:
        return None, "no_event_start"
    if bought is None or start >= bought:
        return _night(start), "event_start"
    end = _wall_clock(event_end)
    length = (end - start) if end and end > start and end - start < timedelta(days=1) else timedelta(hours=6)
    weeks = max(0, (bought - (start + length)).days // 7)
    occ = start + timedelta(weeks=weeks)
    while occ + length <= bought:
        occ += timedelta(weeks=1)
    return _night(occ), "recurring_next_occurrence"


REVIEW_RULE = "recurring_guess_needs_review"


def lookup_enabled():
    return os.environ.get("POSH_LOOKUP_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def resolve_night(payload, lookup=None):
    """(night, how, start_local) using the child event's real start first.

    1. Real start of the child event (payload event_id) from its public Posh
       page -> "posh_page". This is the truth for recurring series.
    2. Otherwise posh_night() (custom field / event_start / weekly guess).
       A weekly guess (event_start before the purchase = series anchor) is
       returned as REVIEW_RULE: Amy flags it for the team instead of booking.
    """
    lookup = lookup or (posh_lookup.real_start if lookup_enabled() else (lambda *a, **k: None))
    bought = None
    if payload.get("date_purchased"):
        try:
            bought = datetime.fromisoformat(payload["date_purchased"].strip().replace("Z", "+00:00"))
            bought = bought if bought.tzinfo else bought.replace(tzinfo=timezone.utc)
        except ValueError:
            bought = None
    try:
        try:
            start = lookup(payload.get("event_id"), payload.get("event_name"), bought,
                           event_start=payload.get("event_start"), event_end=payload.get("event_end"))
        except TypeError:  # simple lookups (tests) take three arguments
            start = lookup(payload.get("event_id"), payload.get("event_name"), bought)
    except Exception as exc:  # noqa: BLE001 - a lookup failure just means "fall back"
        log.info("Posh event lookup failed: %s", exc)
        start = None
    if start:
        local = start.astimezone(VEGAS).replace(tzinfo=None)
        return _night(local), "posh_page", local
    night, how = posh_night(payload.get("event_start"), payload.get("date_purchased"),
                            payload.get("event_end"), payload.get("custom_fields"))
    if how == "recurring_next_occurrence":
        how = REVIEW_RULE
    return night, how, None


def needs_review(body):
    return f"Night Rule: {REVIEW_RULE}" in (body or "")


# --- payload -> request -----------------------------------------------------

def should_ignore(payload):
    """Reason string to skip this payload, or None to process it."""
    kind = payload.get("type")
    if kind != "new_order":
        return f"type {kind!r} is not new_order"
    for flag in ("cancelled", "refunded", "disputed", "isInPersonOrder"):
        if payload.get(flag):
            return f"order is {flag}"
    if not (payload.get("account_email") or "").strip():
        return "no account_email"
    return None


def ticket_names(items):
    return [re.sub(r"\s*,\s*", " ", (i.get("name") or "").strip()) for i in items or [] if i.get("name")]


def to_signup_text(payload, night, how, start_local=None):
    """The Zapier-style NEW POSH SIGNUP body posh.parse_signup reads. Event
    Date carries the corrected night at the event's own start time."""
    start = start_local or _wall_clock(payload.get("event_start"))
    clock = start.time() if start else datetime.min.time().replace(hour=22)
    corrected = datetime.combine(night, clock)
    if clock.hour < posh.NIGHT_ROLLOVER_HOUR:
        corrected += timedelta(days=1)  # keep "12:30 AM Saturday" for a Friday night
    name = f"{payload.get('account_first_name', '')} {payload.get('account_last_name', '')}".strip()
    lines = [
        "NEW POSH CLIENT", "CLIENT INFORMATION",
        f"Name: {name}", f"Email: {payload.get('account_email', '').strip()}",
        f"Phone: {payload.get('account_phone') or ''}",
        "EVENT INFORMATION",
        f"Event: {payload.get('event_name') or ''}",
        f"Event Date: {corrected.strftime('%Y-%m-%dT%H:%M:%S')}.000Z",
        f"Ticket: {','.join(ticket_names(payload.get('items')))}",
        "ORDER INFORMATION",
        f"Order Number: {payload.get('order_number') or ''}",
        f"Promo Code: {payload.get('promo_code') or ''}",
        f"Tracking Link: {payload.get('tracking_link') or ''}",
        f"Date Purchased: {payload.get('date_purchased') or ''}",
        f"Posh Event ID: {payload.get('event_id') or ''}",
        f"Posh Event Start (raw): {payload.get('event_start') or ''}",
        f"Night Rule: {how}",
        WEBHOOK_MARKER,
    ]
    return "\n".join(lines)


def webhook_only():
    """POSH_WEBHOOK_ONLY=true: the old Zapier "NEW POSH SIGNUP" emails (wrong
    dates for recurring events) are skipped; only webhook orders are booked."""
    return os.environ.get("POSH_WEBHOOK_ONLY", "").strip().lower() in ("1", "true", "yes")


def is_webhook_signup(body):
    return WEBHOOK_MARKER in (body or "")


def dedupe_query(payload):
    order = (payload.get("order_number") or "").strip()
    email = (payload.get("account_email") or "").strip()
    event_id = (payload.get("event_id") or "").strip()
    parts = []
    if order and event_id:
        parts.append(f'"Order Number: {order}" "{event_id}"')
    elif order:
        parts.append(f'"Order Number: {order}"')
    if event_id and email:
        parts.append(f'"{event_id}" "{email}"')
    if not parts:
        return None
    if webhook_only():  # Zapier copies (wrong dates) don't count
        parts = [f'{p} "{WEBHOOK_MARKER}"' for p in parts]
    return 'subject:"NEW POSH SIGNUP" -in:trash (' + " OR ".join(f"({p})" for p in parts) + ")"


class Intake:
    """Puts orders into the intake inbox. One Gmail client per instance
    (kept off the main loop's client: googleapiclient isn't thread-safe)."""

    def __init__(self, service_factory, intake_email, dry_run=False, lookup=None):
        self.lookup = lookup
        self._factory, self._service, self.intake_email, self.dry_run = service_factory, None, intake_email, dry_run
        self._seen = set()
        self._lock = threading.Lock()

    def service(self):
        if self._service is None:
            self._service = self._factory()
        return self._service

    def already_have(self, payload):
        q = dedupe_query(payload)
        if not q:
            return False
        resp = self.service().users().messages().list(userId="me", q=q, maxResults=1).execute()
        return bool(resp.get("messages"))

    def accept(self, payload):
        """Returns (status_code, message)."""
        reason = should_ignore(payload)
        if reason:
            return 200, f"ignored: {reason}"
        key = (payload.get("order_number") or "", payload.get("event_id") or "",
               (payload.get("account_email") or "").lower())
        night, how, start_local = resolve_night(payload, self.lookup)
        if night is None:
            how = "unknown"
        body = to_signup_text(payload, night, how, start_local) if night else to_signup_text(
            dict(payload, event_start=""), datetime.now(VEGAS).date(), how).replace(
            "Event Date: ", "Event Date: (unknown) ", 1)
        with self._lock:
            if key in self._seen:
                return 200, "duplicate (this run)"
            if self.dry_run:
                self._seen.add(key)
                log.info("[dry run] would add Posh order %s for %s (%s):\n%s", key[0], night, how, body)
                return 200, "dry run"
            if self.already_have(payload):
                self._seen.add(key)
                return 200, "duplicate"
            msg = EmailMessage()
            msg["To"] = self.intake_email
            msg["From"] = self.intake_email
            msg["Subject"] = f"{SUBJECT} #{key[0] or '?'} {payload.get('event_name') or ''}".strip()
            msg.set_content(body)
            import base64
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            self.service().users().messages().insert(
                userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).execute()
            self._seen.add(key)
        log.info("Posh order %s for %s -> night %s (%s)", key[0], key[2], night, how)
        _ping_amyi(payload, key, night)
        return 200, "queued"


def party_size(payload):
    """Tickets on the order (sum of quantities when Posh sends them)."""
    total = 0
    for i in payload.get("items") or []:
        try:
            total += int(i.get("quantity") or 1)
        except (TypeError, ValueError):
            total += 1
    return total or ""


def _ping_amyi(payload, key, night):
    """Every new Posh order (all events: Vegas, Drai's, Riot House Scottsdale,
    Almost Famous) pings AMYI once, before any booking filter."""
    name = (f"{payload.get('account_first_name', '')} {payload.get('account_last_name', '')}".strip()
            or payload.get("account_name") or "")
    event = payload.get("event_name") or ""
    try:
        venue = posh.venue_from_event(event) or ""
    except Exception:
        venue = ""
    return amyi.notify("posh_guest", name=name, email=key[2], phone=payload.get("account_phone") or "",
                       event=event, venue=venue or event, dates=str(night or ""),
                       party=party_size(payload), source="posh", ref=key[0] or key[1])


# --- Branded send (POST /send-branded) -------------------------------------

BRANDED_MAX_PER_HOUR = 20
_branded_times = []
_branded_lock = threading.Lock()


def branded_from():
    return os.environ.get("BRANDED_FROM_EMAIL", "").strip() or "info@playmakerentertainment.com"


def branded_token_ok(headers):
    """Bearer / X-Amy-Token match on BRANDED_SEND_TOKEN (header only, never query). Unset => reject."""
    want = os.environ.get("BRANDED_SEND_TOKEN", "").strip()
    if not want:
        return False
    given = headers.get("x-amy-token") or ""
    auth = headers.get("authorization") or ""
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:]
    return hmac.compare_digest(given.strip().encode(), want.encode())


def _gmail_call(fn, attempts=4, base_delay=5.0, _sleep=None):
    """Retry Gmail 403/429 rate-limit errors with backoff instead of failing hard."""
    import time
    _sleep = _sleep or time.sleep
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            status = getattr(getattr(e, "resp", None), "status", None)
            limited = status == 429 or (status == 403 and ("rateLimitExceeded" in str(e) or "Quota" in str(e)))
            if not limited or i == attempts - 1:
                raise
            _sleep(min(base_delay * (2 ** i), 60.0))


_zoho_sent = set()


def zoho_configured():
    return bool(os.environ.get("ZOHO_SMTP_USER", "").strip() and os.environ.get("ZOHO_SMTP_PASSWORD", "").strip())


def _zoho_send(to, subject, text, html, sender):
    """Send through Zoho SMTP (465 SSL, then 587 STARTTLS) so Zoho DKIM-signs it. Returns Message-ID."""
    import base64
    import email
    import email.utils
    import smtplib
    import gmail_client
    user = os.environ["ZOHO_SMTP_USER"].strip()
    pw = os.environ["ZOHO_SMTP_PASSWORD"].strip()
    host = os.environ.get("ZOHO_SMTP_HOST", "").strip() or "smtp.zoho.com"
    mid = email.utils.make_msgid(domain=sender.split("@")[-1])
    raw = gmail_client.build_message(to, subject, text, sender, html=html, message_id=mid.strip("<>"))["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    msg["Date"] = email.utils.formatdate(localtime=False)
    last = None
    for port in (465, 587):
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                s = smtplib.SMTP(host, port, timeout=15)
                s.starttls()
            with s:
                s.login(user, pw)
                s.send_message(msg, from_addr=sender, to_addrs=[to])
            return mid.strip("<>")
        except smtplib.SMTPAuthenticationError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Zoho SMTP %s:%s failed: %s: %s", host, port, type(e).__name__, e)
            last = e
    raise last


def send_branded(service, payload, _sleep=None):
    """Returns (status_code, result). payload: to, subject, html, text."""
    import time
    import gmail_client
    to, subject = (payload.get("to") or "").strip(), (payload.get("subject") or "").strip()
    html, text = payload.get("html") or "", payload.get("text") or ""
    if not (to and subject and html and text) or "@" not in to or any(c in to + subject for c in "\r\n,;"):
        return 400, "need to (one address), subject, html, text"
    now = time.time()
    with _branded_lock:
        _branded_times[:] = [t for t in _branded_times if now - t < 3600]
        if len(_branded_times) >= BRANDED_MAX_PER_HOUR:
            return 429, "branded send limit reached, try later"
        _branded_times.append(now)
    sender = branded_from()
    if _gmail_call(lambda: gmail_client.already_sent(service, f'to:{to} subject:"{subject}"'), _sleep=_sleep):
        return 200, "duplicate (already in Sent)"
    with _branded_lock:
        if (to.lower(), subject) in _zoho_sent:
            return 200, "duplicate (already in Sent)"
    if zoho_configured():
        try:
            mid = _zoho_send(to, subject, text, html, sender)
            with _branded_lock:
                _zoho_sent.add((to.lower(), subject))
            log.info("Branded email sent via zoho to %s from %s (id %s)", to, sender, mid)
            return 200, {"sent": True, "id": mid, "from": sender, "via": "zoho"}
        except Exception as e:  # noqa: BLE001
            log.warning("Branded zoho send failed (%s: %s); falling back to gmail", type(e).__name__, e)
    resp = _gmail_call(lambda: service.users().messages().send(
        userId="me", body=gmail_client.build_message(to, subject, text, sender, html=html)).execute(), _sleep=_sleep)
    log.info("Branded email sent via gmail to %s from %s (id %s)", to, sender, (resp or {}).get("id"))
    return 200, {"sent": True, "id": (resp or {}).get("id"), "from": sender, "via": "gmail"}


# --- HTTP server ------------------------------------------------------------

# --- Website form intake (POST /webhooks/website) ---------------------------
# Public browser form (GuestlistGo). No secret token: CORS origin allowlist,
# honeypot, size + rate limits, strict validation. Off unless enabled.
# Same subject/layout as the playmakerentertainment.com contact-form notice so
# gmail_client.SEARCH_QUERY + parse_request pick it up unchanged.
WEBSITE_SUBJECT = "New guest list request submission"
WEBSITE_MAX_BODY = 16 * 1024
WEBSITE_VENUES = {
    "Las Vegas": {"OMNIA", "Hakkasan", "Marquee", "TAO", "JEWEL", "AYU", "Drai's After Hours",
                  "Marquee Dayclub", "TAO Beach Dayclub", "OMNIA Dayclub", "Liquid Pool Lounge",
                  "Palm Tree Beach Club", "LAVO Party Brunch"},
    "Scottsdale": {"Almost Famous", "Riot House", "Cake", "El Hefe"},
}
WEBSITE_SOURCES = {"guestlistgo": "GuestlistGo", "playmaker": "Playmaker"}
# Playmaker site (preview + production) is always allowed next to the env list.
PLAYMAKER_ORIGINS = {"https://playmaker-web-production.up.railway.app",
                     "https://playmakerentertainment.com", "https://www.playmakerentertainment.com"}
WEBSITE_MAX_NIGHTS = 14
_web_times, _web_lock = {}, threading.Lock()


def website_enabled():
    return os.environ.get("WEBSITE_FORM_ENABLED", "").strip() == "1"


def website_origins():
    raw = (os.environ.get("ALLOWED_ORIGINS") or os.environ.get("WEBSITE_FORM_ORIGINS")
           or "https://guestlistgo.me,https://www.guestlistgo.me")
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()} | PLAYMAKER_ORIGINS


def website_rate_ok(ip, now=None, per_ip=10, total=200):
    now = now or datetime.now(timezone.utc).timestamp()
    with _web_lock:
        for k in list(_web_times):
            _web_times[k] = [t for t in _web_times[k] if now - t < 3600]
            if not _web_times[k]:
                del _web_times[k]
        if len(_web_times.get(ip, [])) >= per_ip or sum(map(len, _web_times.values())) >= total:
            return False
        _web_times.setdefault(ip, []).append(now)
        return True


def validate_website(p):
    """Returns (clean_dict, None) or (None, error)."""
    g = lambda k, n=200: str(p.get(k) or "").strip()[:n]
    src = g("source", 40).lower()
    if src not in WEBSITE_SOURCES:
        return None, "unknown source"
    first, last, email, city, venue, date = g("first_name", 60), g("last_name", 60), g("email", 120).lower(), g("city"), g("venue"), g("date", 10)
    phone = re.sub(r"[^\d+]", "", g("phone", 30))
    if not (first and last):
        return None, "name required"
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", email):
        return None, "bad email"
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 15:
        return None, "bad phone"
    if venue not in WEBSITE_VENUES.get(city, ()):
        return None, "bad city/venue"
    try:
        night = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None, "bad date"
    today = datetime.now(VEGAS).date()
    if not today <= night <= today + timedelta(days=90):
        return None, "date out of range"
    end = night
    if g("end_date", 10):
        try:
            end = datetime.strptime(g("end_date", 10), "%Y-%m-%d").date()
        except ValueError:
            return None, "bad end date"
        if not night <= end <= today + timedelta(days=90) or (end - night).days >= WEBSITE_MAX_NIGHTS:
            return None, "end date out of range"
    try:
        women, men = int(p.get("women") or 0), int(p.get("men") or 0)
    except (TypeError, ValueError):
        return None, "bad party"
    if not (0 <= women <= 30 and 0 <= men <= 30 and women + men >= 1):
        return None, "bad party"
    if p.get("age_21_confirmed") is not True:
        return None, "21+ not confirmed"
    ig = re.sub(r"^@", "", g("instagram", 31))
    if ig and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", ig):
        ig = ""
    return {"source": src, "promoter": WEBSITE_SOURCES[src], "first_name": first, "last_name": last,
            "name": f"{first} {last}", "email": email, "phone": phone, "city": city, "venue": venue,
            "event": f"{venue} | Guest List", "night": night, "end": end, "women": women, "men": men,
            "party": women + men, "instagram": ig, "notes": g("notes", 500),
            "ref": re.sub(r"[^\w-]", "", g("ref", 40))}, None


def to_website_text(c):
    """Mirror of the playmakerentertainment.com contact-form notice
    (tests/fixtures/new_form.txt). GuestlistGo: Promoter = GuestlistGo plus a
    Source line so Amy brands the guest side as GuestlistGo. Playmaker: no
    promoter, Source: playmaker (dropped by the parser), so Amy treats it
    exactly like a playmakerentertainment.com notice (Playmaker branding)."""
    night, end = c["night"].isoformat(), c.get("end", c["night"]).isoformat()
    comments = c["notes"]
    if c["instagram"]:
        comments = (comments + " | " if comments else "") + "Instagram: @" + c["instagram"]
    pm = c["source"] == "playmaker"
    site = "playmakerentertainment.com" if pm else "guestlistgo.me"
    return "\n".join([
        f" has received a new message -", "", f"From: {c['name']}", "", f"Email: {c['email']}", "",
        f"Submitted From: {site}", "", "---", "",
        "Message: New guest list request submission", "",
        f"Full name: {c['name']}", f"Email: {c['email']}", f"Phone: {c['phone']}", "ZIP code:",
        f"Visit start date: {night}", f"Visit end date: {end}",
        f"Requested venues: {c['venue']}",
        f"Female guests: {c['women']}", f"Male guests: {c['men']}",
        f"Promoter first name: {'' if pm else c['promoter']}", "Promoter last name:",
        f"Additional comments: {comments.replace(chr(10), ' ')}",
        "Authorization accepted: YES",
        f"Originating page: {c['promoter']} {c['city']} ({c['venue']})",
        f"Source: {c['source']}", f"Ref: {c['ref']}",
        f"Submission date and time: {datetime.now(VEGAS).strftime('%m/%d/%Y, %I:%M:%S %p')} PT",
        "", "---", "", f"This message came from the {c['promoter']} website form ({site})",
    ])


def accept_website(intake, payload):
    clean, err = validate_website(payload)
    if err:
        return 400, err
    key = ("web", clean["ref"] or clean["email"], clean["night"].isoformat(), clean["end"].isoformat(), clean["venue"])
    with intake._lock:
        if key in intake._seen:
            return 200, "duplicate (this run)"
        if intake.dry_run:
            intake._seen.add(key)
            log.info("[dry run] would add website signup %s:\n%s", key, to_website_text(clean))
            return 200, "dry run"
        msg = EmailMessage()
        msg["To"] = intake.intake_email
        msg["From"] = intake.intake_email
        msg["Subject"] = f"{WEBSITE_SUBJECT} - {clean['promoter']} {clean['venue']} {clean['night'].isoformat()}"
        msg.set_content(to_website_text(clean))
        import base64
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        intake.service().users().messages().insert(
            userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).execute()
        intake._seen.add(key)
        notify_team_website(intake.service(), clean, msg["Subject"])
    return 200, "queued"


def notify_team_website(service, clean, subject):
    """Also send the same notice to the team inbox (team@playmakerentertainment.com)
    via Amy's normal send path. Best effort: a failure here never breaks intake."""
    import gmail_client
    from config import rules
    tag = clean["ref"] or re.sub(r"[^\w-]", "", clean["email"])
    try:
        return gmail_client.send_once(
            service, (f"pm-web-{tag}-{clean['night'].isoformat()}@playmakerentertainment.com" if clean["source"] == "playmaker"
             else f"gg-web-{tag}-{clean['night'].isoformat()}@guestlistgo.me"), rules.PLAYMAKER_EMAIL,
            subject, to_website_text(clean), sender=rules.SENDER_EMAIL,
            dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} subject:"{subject}" "Ref: {tag}"')
    except Exception:
        log.exception("Website form: team email to %s failed", rules.PLAYMAKER_EMAIL)
        return False


def make_handler(intake, store=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, ctype, data):
            data = data.encode() if isinstance(data, str) else data
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _newsletter_click(self, action, source):
            q = parse_qs(urlparse(self.path).query)
            email = (q.get("e") or [""])[0].strip().lower()
            name = (q.get("n") or [""])[0][:120]
            sig = (q.get("t") or [""])[0]
            if not newsletter.verify(email, action, sig):
                return self._send(400, "text/html; charset=utf-8", newsletter.page("invalid"))
            status = newsletter.ACTIONS[action]
            if store is None:
                return self._send(503, "text/html; charset=utf-8", newsletter.page("invalid"))
            try:
                store.record(email, name, status, source)
            except Exception:
                log.exception("Newsletter %s failed for %s", action, email)
                return self._send(502, "text/html; charset=utf-8", newsletter.page("invalid"))
            self._send(200, "text/html; charset=utf-8", newsletter.page(status, name))

        def _newsletter_export(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            if not branded_token_ok(headers):
                return self._reply(401, "unauthorized")
            if store is None:
                return self._reply(503, "store unavailable")
            fmt = (parse_qs(urlparse(self.path).query).get("format") or ["json"])[0].lower()
            try:
                ctype, body = newsletter.export(store.all(refresh=True), "csv" if fmt == "csv" else "json")
            except Exception:
                log.exception("Newsletter export failed")
                return self._reply(502, "export failed")
            self._send(200, ctype, body)

        def _reply(self, code, text):
            data = json.dumps({"result": text}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # keep tokens in query strings out of logs
            log.info("%s %s", self.command, urlparse(self.path).path)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path == "/healthz":
                return self._reply(200, "ok")
            if path == "/newsletter/optin":
                return self._newsletter_click("optin", "follow_up_email")
            if path == "/unsubscribe":
                return self._newsletter_click("unsubscribe", "follow_up_email")
            if path == "/newsletter/export":
                return self._newsletter_export()
            self._reply(404, "not found")

        def _send_branded(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            if not branded_token_ok(headers):
                return self._reply(401, "unauthorized")
            if intake.dry_run:
                return self._reply(503, "dry run: not sending")
            length = int(headers.get("content-length") or 0)
            if length <= 0 or length > 1_000_000:
                return self._reply(400, "bad body")
            try:
                payload = json.loads(self.rfile.read(length).decode())
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self._reply(400, "bad json")
            if payload.get("marketing") and store is not None:
                try:
                    if store.is_opted_out(payload.get("to") or ""):
                        return self._reply(200, {"sent": False, "suppressed": "unsubscribed"})
                except Exception:
                    log.exception("Opt-out check failed")
                    return self._reply(502, "opt-out check failed")
            try:
                code, result = send_branded(intake.service(), payload)
            except Exception:
                log.exception("Branded send failed")
                return self._reply(502, "send failed")
            self._reply(code, result)

        def _cors(self):
            origin = (self.headers.get("Origin") or "").rstrip("/")
            return origin if origin in website_origins() else None

        def do_OPTIONS(self):
            origin = self._cors()
            if urlparse(self.path).path.rstrip("/") != "/webhooks/website" or not origin or not website_enabled():
                return self._reply(404, "not found")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def _website(self):
            if not website_enabled():
                return self._reply(404, "not found")
            origin = self._cors()
            if not origin:
                return self._reply(403, "origin not allowed")
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            if not website_rate_ok(ip):
                return self._reply(429, "slow down")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > WEBSITE_MAX_BODY:
                return self._reply(400, "bad body")
            try:
                payload = json.loads(self.rfile.read(length).decode())
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                return self._reply(400, "bad json")
            if payload.get("company"):  # honeypot
                code, text = 200, "ok"
            else:
                try:
                    code, text = accept_website(intake, payload)
                except Exception:
                    log.exception("Website form failed")
                    code, text = 500, "error"
            data = json.dumps({"ok": code == 200, "result": text}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if urlparse(self.path).path.rstrip("/") == "/webhooks/website":
                return self._website()
            if urlparse(self.path).path.rstrip("/") == "/send-branded":
                return self._send_branded()
            if urlparse(self.path).path.rstrip("/") == "/unsubscribe":  # RFC 8058 one-click
                return self._newsletter_click("unsubscribe", "list_unsubscribe_one_click")
            if urlparse(self.path).path.rstrip("/") != "/webhooks/posh":
                return self._reply(404, "not found")
            headers = {k.lower(): v for k, v in self.headers.items()}
            sigs = signature_header_names(headers)
            if sigs:
                log.info("Posh webhook signature-like headers present: %s", ", ".join(sigs))
            if not token_ok(self.path, headers):
                return self._reply(401, "unauthorized")
            length = int(headers.get("content-length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._reply(400, "bad body")
            try:
                payload = json.loads(self.rfile.read(length).decode())
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self._reply(400, "bad json")
            try:
                code, text = intake.accept(payload)
            except Exception:
                log.exception("Posh webhook failed")
                return self._reply(500, "error")
            self._reply(code, text)
    return Handler


def serve(intake, port=None, host="0.0.0.0", store=None):
    """Start the webhook server on a daemon thread; returns the server."""
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), make_handler(intake, store))
    threading.Thread(target=server.serve_forever, name="posh-webhook", daemon=True).start()
    log.info("Posh webhook listening on :%d (POST /webhooks/posh, GET /healthz, /newsletter/optin, /unsubscribe, /newsletter/export)%s", server.server_address[1],
             "" if expected_token() else " - POSH_WEBHOOK_TOKEN not set, every webhook will be rejected")
    return server

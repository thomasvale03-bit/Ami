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

    def __init__(self, service_factory, intake_email, dry_run=False, lookup=None, on_accept=None):
        self.lookup = lookup
        self.on_accept = on_accept
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
        event_name = payload.get("event_name") or ""
        from config import rules as _rules
        import speakeasy as _speakeasy
        if (os.environ.get("AMYI_RAW_POSH_PING", "").strip().lower() in ("1", "true", "yes")
                or (_rules.is_drais(event_name) and not _speakeasy.auto_on())):
            _ping_amyi(payload, key, night)
        if callable(getattr(self, "on_accept", None)):
            try:
                self.on_accept()
            except Exception:
                log.exception("on_accept callback failed")
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


GUESTLISTGO_FROM = "confirmations@guestlistgo.me"  # only extra sender allowed


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
    sender, from_name = branded_from(), None
    requested = (payload.get("from") or "").strip().lower()
    if requested == GUESTLISTGO_FROM:
        sender, from_name = GUESTLISTGO_FROM, "GuestlistGo"  # Gmail send-as (relayed via Zoho SMTP)
    elif requested:
        return 400, "from not allowed"
    is_followup = subject.lower().startswith("hope you had fun")
    if subject.lower().startswith(WEBSITE_SUBJECT.lower()):
        subject, dedupe_q = _branded_guestlist_subject_and_dedupe(to, subject, text, payload)
    elif payload.get("dedupe_query"):
        dedupe_q = str(payload["dedupe_query"])
    else:
        dedupe_q = (f"to:{to} {gmail_client.FOLLOW_UP_SEARCH} newer_than:60d"
                    if is_followup else f'to:{to} subject:"{subject}"')
    if _gmail_call(lambda: gmail_client.already_sent(service, dedupe_q), _sleep=_sleep):
        return 200, "duplicate (already in Sent)"
    with _branded_lock:
        if (to.lower(), subject) in _zoho_sent or (is_followup and (to.lower(), "__followup__") in _zoho_sent):
            return 200, "duplicate (already in Sent)"
    if zoho_configured() and not from_name:
        try:
            mid = _zoho_send(to, subject, text, html, sender)
            with _branded_lock:
                _zoho_sent.add((to.lower(), subject))
                if is_followup:
                    _zoho_sent.add((to.lower(), "__followup__"))
            gmail_client.record_branded_sent(to, subject, text)
            log.info("Branded email sent via zoho to %s from %s (id %s)", to, sender, mid)
            return 200, {"sent": True, "id": mid, "from": sender, "via": "zoho"}
        except Exception as e:  # noqa: BLE001
            log.warning("Branded zoho send failed (%s: %s); falling back to gmail", type(e).__name__, e)
    body = (gmail_client.build_message(to, subject, text, sender, html=html, from_name=from_name) if from_name
            else gmail_client.build_message(to, subject, text, sender, html=html))
    resp = _gmail_call(lambda: service.users().messages().send(userId="me", body=body).execute(), _sleep=_sleep)
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
    "Scottsdale": {"Almost Famous", "Riot House", "Cake", "El Hefe", "Maya Day Club", "Maya Night Club"},
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


WEBSITE_VENUE_ALIASES = (
    ("omnia dayclub", "Las Vegas", "OMNIA Dayclub"),
    ("omnia nightclub", "Las Vegas", "OMNIA"),
    ("omnia", "Las Vegas", "OMNIA"),
    ("hakkasan nightclub", "Las Vegas", "Hakkasan"),
    ("hakkasan", "Las Vegas", "Hakkasan"),
    ("marquee dayclub", "Las Vegas", "Marquee Dayclub"),
    ("marquee nightclub", "Las Vegas", "Marquee"),
    ("marquee", "Las Vegas", "Marquee"),
    ("tao beach dayclub", "Las Vegas", "TAO Beach Dayclub"),
    ("tao beach", "Las Vegas", "TAO Beach Dayclub"),
    ("tao nightclub", "Las Vegas", "TAO"),
    ("tao", "Las Vegas", "TAO"),
    ("jewel nightclub", "Las Vegas", "JEWEL"),
    ("jewel", "Las Vegas", "JEWEL"),
    ("ayu dayclub", "Las Vegas", "AYU"),
    ("ayu", "Las Vegas", "AYU"),
    ("drai's after hours", "Las Vegas", "Drai's After Hours"),
    ("drais after hours", "Las Vegas", "Drai's After Hours"),
    ("drai's", "Las Vegas", "Drai's After Hours"),
    ("liquid pool lounge", "Las Vegas", "Liquid Pool Lounge"),
    ("liquid pool", "Las Vegas", "Liquid Pool Lounge"),
    ("palm tree beach club", "Las Vegas", "Palm Tree Beach Club"),
    ("palm tree beach", "Las Vegas", "Palm Tree Beach Club"),
    ("lavo party brunch", "Las Vegas", "LAVO Party Brunch"),
    ("lavo", "Las Vegas", "LAVO Party Brunch"),
    ("almost famous", "Scottsdale", "Almost Famous"),
    ("riot house", "Scottsdale", "Riot House"),
    ("cake nightclub", "Scottsdale", "Cake"),
    ("cake", "Scottsdale", "Cake"),
    ("el hefe", "Scottsdale", "El Hefe"),
    ("maya day + nightclub", "Scottsdale", "Maya Day Club"),
    ("maya day club", "Scottsdale", "Maya Day Club"),
    ("maya dayclub", "Scottsdale", "Maya Day Club"),
    ("maya night club", "Scottsdale", "Maya Night Club"),
    ("maya nightclub", "Scottsdale", "Maya Night Club"),
    ("maya", "Scottsdale", "Maya Day Club"),
)


def _match_website_venue(raw_text):
    low = str(raw_text or "").strip().lower()
    if not low:
        return None, None
    for alias, v_city, canonical in WEBSITE_VENUE_ALIASES:
        if low == alias:
            return v_city, canonical
    prio = re.search(r"(?:priority|only)\s*:\s*([a-z0-9' +]+?)(?:\s*\(|,|;|\.|$)", low, re.I)
    if prio:
        p_low = prio.group(1).strip().lower()
        for alias, v_city, canonical in WEBSITE_VENUE_ALIASES:
            if p_low == alias or alias in p_low:
                return v_city, canonical
    best = None
    best_idx = 10**9
    for alias, v_city, canonical in WEBSITE_VENUE_ALIASES:
        m = re.search(r"(?:^|[^a-z0-9])" + re.escape(alias) + r"(?:[^a-z0-9]|$)", low, re.I)
        if m and m.start() < best_idx:
            best_idx = m.start()
            best = (v_city, canonical)
    return best or (None, None)


def _parse_kv_lines(text):
    out = {}
    for line in str(text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k and k not in out:
                out[k] = v.strip()
    return out


def _branded_guestlist_subject_and_dedupe(to, subject, text, payload):
    if payload.get("dedupe_query"):
        return subject, str(payload["dedupe_query"])
    kv = _parse_kv_lines(text)
    g_name = kv.get("full name") or ""
    g_email = (kv.get("email") or "").lower()
    g_start = kv.get("visit start date") or ""
    g_venue = kv.get("requested venues") or ""
    _, canon_venue = _match_website_venue(g_venue)
    if subject.strip().lower() in (WEBSITE_SUBJECT.lower(), f"{WEBSITE_SUBJECT.lower()} - guestlistgo"):
        brand = "GuestlistGo" if "guestlistgo" in subject.lower() else "Playmaker"
        parts = [WEBSITE_SUBJECT, "-", brand]
        if canon_venue:
            parts.append(canon_venue)
        if g_start:
            parts.append(g_start)
        if g_name or g_email:
            parts.append(f"({g_name or g_email})")
        if len(parts) > 3:
            subject = " ".join(parts)
    if g_email or g_start:
        terms = [f"to:{to}", f'subject:"{WEBSITE_SUBJECT}"']
        if g_email:
            terms.append(f'"{g_email}"')
        if g_start:
            terms.append(f'"{g_start}"')
        return subject, " ".join(terms)
    return subject, f'to:{to} subject:"{subject}"'


def normalize_website_payload(p):
    """Normalize Airo / server-to-server guest list payloads into canonical /webhooks/website fields."""
    if not isinstance(p, dict):
        return {}
    out = dict(p)
    conv = p.get("conversation") if isinstance(p.get("conversation"), dict) else {}
    d = conv.get("data") if isinstance(conv.get("data"), dict) else {}
    u = p.get("user") if isinstance(p.get("user"), dict) else {}
    msgs = conv.get("messages_attributes") if isinstance(conv.get("messages_attributes"), list) else []
    body_text = msgs[0].get("body", "") if msgs and isinstance(msgs[0], dict) else ""
    bf = _parse_kv_lines(body_text)

    orig_page = str(p.get("originatingPage") or p.get("originating_page") or d.get("Originating Page") or bf.get("originating page") or "").strip()
    if "source" not in p or p.get("source") is None or str(p.get("source")).strip() == "":
        promo_hint = f"{p.get('promoter') or ''} {d.get('Promoter First Name') or ''} {orig_page}"
        out["source"] = "guestlistgo" if re.search(r"guestlist\s*go", promo_hint, re.I) else "playmaker"

    if "first_name" not in p and "last_name" not in p:
        first = str(p.get("firstName") or d.get("First Name") or "").strip()
        last = str(p.get("lastName") or d.get("Last Name") or "").strip()
        if not first:
            full = str(p.get("name") or u.get("name") or d.get("Full Name") or bf.get("full name") or "").strip()
            parts = full.split()
            if parts:
                first = parts[0]
                last = " ".join(parts[1:]) if len(parts) > 1 else "-"
        elif not last:
            last = "-"
        out["first_name"] = first
        out["last_name"] = last

    if not out.get("email"):
        out["email"] = p.get("email") or u.get("email") or d.get("Email") or d.get("email") or bf.get("email") or ""
    if not out.get("phone"):
        out["phone"] = p.get("phone") or d.get("Phone") or d.get("phone") or u.get("phone") or bf.get("phone") or ""

    if "date" not in p or not str(p.get("date") or "").strip():
        dates_val = p.get("dates")
        d_start, d_end = "", ""
        if isinstance(dates_val, list) and dates_val:
            d_start, d_end = str(dates_val[0]).strip(), str(dates_val[-1]).strip()
        elif isinstance(dates_val, str) and dates_val.strip():
            iso = re.findall(r"\d{4}-\d{2}-\d{2}", dates_val)
            if iso:
                d_start, d_end = iso[0], iso[-1]
            else:
                 d_start = dates_val.strip()
        out["date"] = str(p.get("start_date") or p.get("startDate") or p.get("visitStartDate") or d.get("Visit Start Date") or d_start or bf.get("visit start date") or "").strip()
        if ("end_date" not in p or not str(p.get("end_date") or "").strip()) and d_end:
            out["end_date"] = d_end
    if "end_date" not in out or not str(out.get("end_date") or "").strip():
        end_candidate = str(p.get("endDate") or p.get("visitEndDate") or d.get("Visit End Date") or bf.get("visit end date") or "").strip()
        if end_candidate:
            out["end_date"] = end_candidate

    if "women" not in p:
        for k in ("females", "girls", "femaleGuests", "female_guests"):
            if p.get(k) is not None:
                out["women"] = p.get(k)
                break
        else:
            if d.get("Female Guests") is not None:
                out["women"] = d.get("Female Guests")
            elif bf.get("female guests") is not None:
                out["women"] = bf.get("female guests")
    if "men" not in p:
        for k in ("males", "guys", "maleGuests", "male_guests"):
            if p.get(k) is not None:
                out["men"] = p.get(k)
                break
        else:
            if d.get("Male Guests") is not None:
                out["men"] = d.get("Male Guests")
            elif bf.get("male guests") is not None:
                out["men"] = bf.get("male guests")

    if "promoter" not in p or not str(p.get("promoter") or "").strip():
        pf = str(p.get("promoterFirstName") or p.get("promoter_first_name") or d.get("Promoter First Name") or bf.get("promoter first name") or "").strip()
        pl = str(p.get("promoterLastName") or p.get("promoter_last_name") or d.get("Promoter Last Name") or bf.get("promoter last name") or "").strip()
        if pf or pl:
            out["promoter"] = f"{pf} {pl}".strip()

    raw_venue = p.get("venue") if "venue" in p else (p.get("venues") if "venues" in p else (d.get("Venues") or bf.get("requested venues") or ""))
    if isinstance(raw_venue, list):
        raw_venue = ", ".join(str(x).strip() for x in raw_venue if str(x).strip())
    raw_venue = str(raw_venue or "").strip()

    explicit_city = str(p.get("city") or d.get("City") or "").strip()
    if explicit_city in WEBSITE_VENUES and raw_venue in WEBSITE_VENUES[explicit_city]:
        out["city"] = explicit_city
        out["venue"] = raw_venue
    elif explicit_city and explicit_city not in WEBSITE_VENUES:
        out["city"] = explicit_city
        out["venue"] = raw_venue
    else:
        matched_city, matched_venue = _match_website_venue(raw_venue)
        is_open_pref = bool(re.search(r"open to (?:all |other )?venues|any venue|all venues", raw_venue, re.I)) or ("venue" not in p and not raw_venue)
        city = explicit_city or ("Scottsdale" if ("scottsdale" in raw_venue.lower() or matched_city == "Scottsdale") else "Las Vegas")
        if matched_venue and matched_venue in WEBSITE_VENUES.get(city, ()):
            out["city"] = city
            out["venue"] = matched_venue
        elif is_open_pref and city in WEBSITE_VENUES:
            out["city"] = city
            out["venue"] = "Almost Famous" if city == "Scottsdale" else "OMNIA"
        else:
            out["city"] = city
            out["venue"] = raw_venue

    notes = str(p.get("notes") or p.get("comments") or d.get("Additional Comments") or bf.get("additional comments") or "").strip()
    zip_code = str(p.get("zip") or p.get("zipCode") or p.get("zip_code") or d.get("ZIP Code") or bf.get("zip code") or "").strip()
    extra_notes = [notes] if notes else []
    if raw_venue and raw_venue.lower() != str(out.get("venue") or "").lower() and raw_venue.lower() not in notes.lower():
        extra_notes.append(f"Venues: {raw_venue}")
    if orig_page and "partyeveryday" in orig_page.lower() and "partyeveryday" not in notes.lower():
        extra_notes.append(f"Origin: {orig_page}")
    if zip_code and zip_code not in notes:
        extra_notes.append(f"ZIP: {zip_code}")
    if extra_notes:
        out["notes"] = " | ".join(extra_notes)[:500]

    if not out.get("ref"):
        ref_val = str(d.get("Ref") or bf.get("ref") or "").strip()
        if not ref_val:
            m_alert = re.search(r"alert\s*#([A-Za-z0-9_-]+)", f"{out.get('notes') or ''} {body_text}", re.I)
            if m_alert:
                ref_val = f"ped-{m_alert.group(1)}"
        if ref_val:
            out["ref"] = ref_val

    if "age_21_confirmed" not in p:
        auth_val = str(d.get("Authorization Accepted") or p.get("authorizationAccepted") or bf.get("authorization accepted") or "YES").strip().upper()
        out["age_21_confirmed"] = auth_val in ("YES", "TRUE", "1")

    return out


def validate_website(p):
    """Returns (clean_dict, None) or (None, error)."""
    p = normalize_website_payload(p)
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
    promo = re.sub(r"\s+", " ", re.sub(r"[^\w .'&-]", "", g("promoter", 60))).strip()
    if src != "playmaker" or promo.lower().replace(" ", "") == "guestlistgo":
        promo = ""  # GuestlistGo keeps its fixed promoter; never let a guest pick the GuestlistGo brand
    ig = re.sub(r"^@", "", g("instagram", 31))
    if ig and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", ig):
        ig = ""
    full_name = first if last == "-" else f"{first} {last}"
    return {"source": src, "promoter": WEBSITE_SOURCES[src], "first_name": first, "last_name": last,
            "name": full_name, "email": email, "phone": phone, "city": city, "venue": venue,
            "event": f"{venue} | Guest List", "night": night, "end": end, "women": women, "men": men,
            "party": women + men, "instagram": ig, "promoter_name": promo, "notes": g("notes", 500),
            "ref": re.sub(r"[^\w-]", "", g("ref", 40))}, None


def _promoter_lines(c):
    """Playmaker: the guest's optional promoter, split like the Airo form
    (first word / rest). Blank stays blank (no promoter)."""
    parts = (c.get("promoter_name") or "").split(" ", 1)
    return [f"Promoter first name: {parts[0]}", f"Promoter last name: {parts[1] if len(parts) > 1 else ''}"]


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
        *(_promoter_lines(c) if pm else [f"Promoter first name: {c['promoter']}", "Promoter last name:"]),
        f"Additional comments: {comments.replace(chr(10), ' ')}",
        "Authorization accepted: YES",
        f"Originating page: {c['promoter']} {c['city']} ({c['venue']})",
        f"Source: {c['source']}", f"Ref: {c['ref']}",
        f"Submission date and time: {datetime.now(VEGAS).strftime('%m/%d/%Y, %I:%M:%S %p')} PT",
        "", "---", "", f"This message came from the {c['promoter']} website form ({site})",
    ])


WEBSITE_RETRY_DELAYS = (1.0, 2.0, 4.0)


def pending_dir():
    """Where website sign-ups wait when Gmail is rate limited. Set
    AMY_PENDING_DIR to a Railway volume path so they survive redeploys."""
    d = os.environ.get("AMY_PENDING_DIR") or ("/data/pending" if os.path.isdir("/data") else
                                              os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pending"))
    os.makedirs(d, exist_ok=True)
    return d


def _pending_name(key):
    return re.sub(r"[^\w.-]", "_", "-".join(key))[:200] + ".json"


def _delivered_path():
    return os.path.join(pending_dir(), "delivered.txt")


def _delivered(key):
    try:
        with open(_delivered_path()) as fh:
            return "|".join(key) in {line.strip() for line in fh}
    except FileNotFoundError:
        return False


def _mark_delivered(key):
    with open(_delivered_path(), "a") as fh:
        fh.write("|".join(key) + "\n")


def _website_key(clean):
    return ("web", clean["ref"] or clean["email"], clean["night"].isoformat(), clean["end"].isoformat(), clean["venue"])


def _deliver_website(intake, clean):
    msg = EmailMessage()
    msg["To"] = intake.intake_email
    msg["From"] = intake.intake_email
    msg["Subject"] = f"{WEBSITE_SUBJECT} - {clean['promoter']} {clean['venue']} {clean['night'].isoformat()}"
    msg.set_content(to_website_text(clean))
    import base64
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    intake.service().users().messages().insert(
        userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).execute()
    key = _website_key(clean)
    intake._seen.add(key)
    try:
        _mark_delivered(key)
    except OSError:
        log.exception("Website form: could not record delivered %s", key)
    notify_team_website(intake.service(), clean, msg["Subject"])
    if callable(getattr(intake, "on_accept", None)):
        try:
            intake.on_accept()
        except Exception:
            log.exception("on_accept callback failed")


def accept_website(intake, payload, _sleep=None):
    import time
    import gmail_client
    _sleep = _sleep or time.sleep
    clean, err = validate_website(payload)
    if err:
        return 400, err
    key = _website_key(clean)
    with intake._lock:
        if key in intake._seen:
            return 200, "duplicate (this run)"
        if intake.dry_run:
            intake._seen.add(key)
            log.info("[dry run] would add website signup %s:\n%s", key, to_website_text(clean))
            return 200, "dry run"
        if _delivered(key):
            intake._seen.add(key)
            return 200, "duplicate"
        path = os.path.join(pending_dir(), _pending_name(key))
        if os.path.exists(path):
            return 200, "queued (pending)"
        for i, delay in enumerate((0.0,) + WEBSITE_RETRY_DELAYS):
            if delay:
                _sleep(delay)
            try:
                _deliver_website(intake, clean)
                return 200, "queued"
            except Exception as e:
                if not gmail_client.is_rate_limited(e):
                    raise
                log.warning("Website form: Gmail rate limited (%d/%d) for %s", i + 1, len(WEBSITE_RETRY_DELAYS) + 1, key)
        with open(path + ".tmp", "w") as fh:
            json.dump({"key": list(key), "payload": payload, "saved": datetime.now(timezone.utc).isoformat()}, fh)
        os.replace(path + ".tmp", path)
        log.warning("Website form: saved %s for retry (Gmail rate limited)", key)
        return 200, "queued (pending)"


def retry_pending_website(intake):
    """Deliver sign-ups saved while Gmail was rate limited. Dedupe-safe:
    a key (pm- ref + night + venue) already delivered is just cleared.
    Returns (delivered, left)."""
    import gmail_client
    done = left = 0
    d = pending_dir()
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        with intake._lock:
            try:
                with open(path) as fh:
                    item = json.load(fh)
                clean, err = validate_website(item["payload"])
                if err:
                    log.error("Website form: dropping invalid pending %s: %s", name, err)
                    os.remove(path)
                    continue
                key = _website_key(clean)
                if key in intake._seen or _delivered(key):
                    os.remove(path)
                    continue
                _deliver_website(intake, clean)
                os.remove(path)
                done += 1
                log.info("Website form: delivered pending %s", key)
            except Exception as e:
                left += 1
                if gmail_client.is_rate_limited(e):
                    log.warning("Website form: still rate limited; %s waits for next cycle", name)
                    break
                log.exception("Website form: pending %s failed", name)
    return done, left


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
                subj = (payload.get("subject") or "").strip()
                if subj.lower().startswith(WEBSITE_SUBJECT.lower()) and hasattr(intake, "_lock"):
                    kv = _parse_kv_lines(payload.get("text") or "")
                    if kv.get("full name") and kv.get("email") and kv.get("visit start date"):
                        try:
                            accept_website(intake, {
                                "name": kv.get("full name"),
                                "email": kv.get("email"),
                                "phone": kv.get("phone") or "0000000000",
                                "zip": kv.get("zip code") or "",
                                "date": kv.get("visit start date"),
                                "end_date": kv.get("visit end date") or kv.get("visit start date"),
                                "venues": kv.get("requested venues") or "OMNIA",
                                "females": kv.get("female guests") or 0,
                                "males": kv.get("male guests") or 0,
                                "promoterFirstName": kv.get("promoter first name") or "",
                                "promoterLastName": kv.get("promoter last name") or "",
                                "comments": kv.get("additional comments") or "",
                                "originatingPage": kv.get("originating page") or "",
                                "ref": kv.get("ref") or "",
                            })
                        except Exception:
                            log.exception("Branded guest-list intake bridge failed")
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
            raw_origin = (self.headers.get("Origin") or "").strip().rstrip("/")
            origin = (raw_origin if raw_origin in website_origins() else None) if raw_origin else "https://playmakerentertainment.com"
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

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
        return 200, "queued"


# --- HTTP server ------------------------------------------------------------

def make_handler(intake):
    class Handler(BaseHTTPRequestHandler):
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
            if urlparse(self.path).path == "/healthz":
                return self._reply(200, "ok")
            self._reply(404, "not found")

        def do_POST(self):
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


def serve(intake, port=None, host="0.0.0.0"):
    """Start the webhook server on a daemon thread; returns the server."""
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), make_handler(intake))
    threading.Thread(target=server.serve_forever, name="posh-webhook", daemon=True).start()
    log.info("Posh webhook listening on :%d (POST /webhooks/posh, GET /healthz)%s", server.server_address[1],
             "" if expected_token() else " - POSH_WEBHOOK_TOKEN not set, every webhook will be rejected")
    return server

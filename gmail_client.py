"""
Reads new guest-list request emails and parses them into the field
dict rules_engine.normalize_guest_request expects.

Uses the Gmail API (not IMAP) so it can run unattended with a refresh token.
Auth: create a Google Cloud OAuth client (Desktop App type), enable the
Gmail API, and run a one-time authorization flow to obtain token.json.
See README.md for the exact steps.

Email format (verified against a real GoDaddy/Airo notification, Sept 28 2026):
every form field is one "Label: value" line inside the block that follows
"Message: New guest list request submission".
"""
import base64
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from config import rules
from config.rules import normalize_venue_name

log = logging.getLogger("amy.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Amy's own labels. The previous guest-list process already uses
# Playmaker/Processed, Playmaker/Exception etc. in this inbox, so Amy never
# reads or writes those; only her own labels mark a request as handled.
PROCESSING_LABEL = "Amy/Processing"
PROCESSED_LABEL = "Amy/Processed"
EXCEPTION_LABEL = "Amy/Exception"

# Not limited to unread mail: the team often opens requests on a phone
# before Amy runs.
SEARCH_QUERY = (
    '(subject:"New guest list request submission" OR subject:"Guest List Request" '
    'OR subject:"NEW POSH SIGNUP") '
    f'newer_than:30d -in:trash -label:{PROCESSED_LABEL} -label:{EXCEPTION_LABEL}'
)

# Lowercased label -> internal field name. Older emails labeled the counts
# "Female guests (free before 1am)", so counts are matched by prefix below.
LABEL_TO_FIELD = {
    "full name": "name",
    "name": "name",
    "email": "email",
    "phone": "phone",
    "zip code": "billing_zip",
    "visit start date": "start_date",
    "visit end date": "end_date",
    "requested venues": "venues",
    "venues": "venues",
    "promoter first name": "promoter_first",
    "promoter last name": "promoter_last",
    "additional comments": "comments",
    "authorization accepted": "authorization",
    "originating page": "originating_page",
    "submission date and time": "submitted_at",
    "submission date": "submitted_at",
}

LINE_RE = re.compile(r"^[ \t]*([A-Za-z][A-Za-z \t\(\)/\-]*?)[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def get_service(token_path="token.json"):
    """Gmail API client for the intake inbox.

    Uses GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN from the
    environment when set (how the server runs), otherwise token.json (from
    authorize.py on a computer). Imported lazily so parsing can be tested
    without the Google libraries.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if os.environ.get("GMAIL_REFRESH_TOKEN"):
        creds = Credentials(
            None,
            refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
    elif os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
    else:
        raise SystemExit("No Gmail credentials: set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and "
                         "GMAIL_REFRESH_TOKEN (or create token.json with authorize.py).")
    return build("gmail", "v1", credentials=creds)


def get_account_email(service):
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def build_query(start_after=None):
    """start_after: Unix seconds. Requests received before it (already
    handled by the previous process) are never picked up."""
    return f"{SEARCH_QUERY} after:{int(start_after)}" if start_after else SEARCH_QUERY


def list_pending_requests(service, max_results=25, start_after=None):
    resp = service.users().messages().list(
        userId="me", q=build_query(start_after), maxResults=max_results
    ).execute()
    return resp.get("messages", [])


def get_plain_text_body(service, message_id):
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    def decode(data):
        return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")

    def walk(parts):
        for part in parts:
            if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                return decode(part["body"]["data"])
            if "parts" in part:
                found = walk(part["parts"])
                if found:
                    return found
        return None

    payload = msg["payload"]
    if "parts" in payload:
        body = walk(payload["parts"]) or ""
    else:
        body = decode(payload["body"].get("data", ""))
    return msg, body


def posh_order_already_handled(service, order_number, exclude_message_id):
    """True if another NEW POSH SIGNUP email with this Posh order number was
    already handled by Amy (labeled Amy/Processed or Amy/Exception)."""
    import posh
    if not order_number:
        return False
    resp = service.users().messages().list(
        userId="me",
        q=(f'subject:"NEW POSH SIGNUP" "{order_number}" '
           f"(label:{PROCESSED_LABEL} OR label:{EXCEPTION_LABEL})"),
        maxResults=20,
    ).execute()
    for ref in resp.get("messages", []):
        if ref["id"] == exclude_message_id:
            continue
        _, body = get_plain_text_body(service, ref["id"])
        if posh.parse_signup(ref["id"], body)["posh"].get("order_number") == order_number:
            return True
    return False


CONFIRMATION_SUBJECT = "Playmaker Guest List Confirmation"
# Concierge-era guest confirmation subject (what Amy sends now).
CONFIRMATION_SUBJECT_CONCIERGE = "You're on the list"
# AMYI's post-signup guest confirmation (same body layout). Follow-ups key off both.
CONFIRMATION_SUBJECT_POST_SIGNUP = "You're on the Playmaker Entertainment guest list"

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _resolve_date(month_name, day, reference):
    """A confirmation body shows 'Friday, October 9' (no year). Pick the year
    so the night falls on/after when the email was sent (the visit is upcoming),
    handling the Dec->Jan rollover."""
    month = _MONTHS.get(month_name)
    if not month:
        return None
    for year in (reference.year, reference.year + 1):
        try:
            candidate = date(year, month, int(day))
        except ValueError:
            return None
        if candidate >= reference - timedelta(days=7):
            return candidate
    return None


def recent_confirmations(service, days=45):
    """Guest confirmations Amy sent recently, for the 7-day follow-up:
    [{"id", "to", "first_name", "venues", "last_night"}]. Reads the clubs and
    the guest's last night out of the email body (the concierge confirmation
    subject carries no dates)."""
    resp = service.users().messages().list(
        userId="me",
        q=(f'in:sent (subject:"{CONFIRMATION_SUBJECT_CONCIERGE}" OR '
           f'subject:"{CONFIRMATION_SUBJECT_POST_SIGNUP}") newer_than:{days}d'), maxResults=200,
    ).execute()
    out = []
    for ref in resp.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata", metadataHeaders=["To"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sent = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc).date()
        _msg, body = get_plain_text_body(service, ref["id"])  # returns (message, text)
        body = (body or "").replace("\r\n", "\n")
        first = re.search(r"\bHi ([^,\s]+),", body)
        venues = [v for v in rules.ALL_AUTHORIZED_VENUES if v in body]
        dates = [d for d in (_resolve_date(m, day, sent)
                             for _wd, m, day in re.findall(r"([A-Z][a-z]+), ([A-Z][a-z]+) (\d{1,2})", body))
                 if d]
        out.append({"id": ref["id"], "to": headers.get("to", ""),
                    "first_name": first.group(1) if first else "",
                    "venues": venues, "last_night": max(dates) if dates else None})
    return out


def has_opted_out(service, address):
    """True if this guest ever replied asking to stop or unsubscribe."""
    resp = service.users().messages().list(
        userId="me", q=f"from:{address} (stop OR unsubscribe) -in:sent", maxResults=1,
    ).execute()
    return bool(resp.get("messages"))


def get_subject(msg):
    for header in msg.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "subject":
            return header.get("value", "")
    return ""


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _form_block(body_text):
    """Only parse the submission block, not the 'From:/Email:' header GoDaddy
    prepends (that header is the sender line, not form data)."""
    marker = "Message:"
    start = body_text.find(marker)
    block = body_text[start + len(marker):] if start != -1 else body_text
    end = block.find("\n---")
    if end != -1:
        block = block[:end]
    return block


def parse_request(message_id, body_text):
    fields = {"source_message_id": message_id}
    # Real notifications use CRLF line endings; a stray "\r" would otherwise
    # end up inside every value (e.g. dates).
    body_text = body_text.replace("\r\n", "\n").replace("\r", "\n")

    for label, value in LINE_RE.findall(_form_block(body_text)):
        key = label.lower().strip()
        if key.startswith("female guests"):
            fields["female_count"] = value
        elif key.startswith("male guests"):
            fields["male_count"] = value
        elif key in LABEL_TO_FIELD:
            fields.setdefault(LABEL_TO_FIELD[key], value)  # first occurrence wins

    # Some deliveries run fields together and add link text, e.g.
    # "Email: mailto:guest@icloud.com Phone: 7757221489". Keep only the address.
    raw_email = fields.get("email", "")
    found = EMAIL_RE.search(raw_email)
    if found:
        fields["email"] = found.group(0)
        phone = re.search(r"Phone:\s*([+\d][\d\s().-]{6,}\d)", raw_email, re.I)
        if phone and not fields.get("phone"):
            fields["phone"] = phone.group(1).strip()
    elif raw_email:
        fields.pop("email")  # not an address: treat as missing, never book it

    if "name" in fields:
        parts = fields["name"].split(" ", 1)
        fields["first_name"] = parts[0]
        fields["last_name"] = parts[1] if len(parts) > 1 else ""

    promoter = f"{fields.pop('promoter_first', '')} {fields.pop('promoter_last', '')}".strip()
    fields["promoter"] = promoter or None

    if fields.get("venues"):
        fields["venues"] = [
            normalize_venue_name(v) for v in fields["venues"].split(",") if v.strip()
        ]
    else:
        fields["venues"] = []

    missing = [f for f in ("email", "start_date", "end_date") if not fields.get(f)]
    if missing:
        fields["_missing_required"] = missing

    return fields


def get_or_create_label(service, name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def set_labels(service, message_id, label_ids, add=(), remove=()):
    """label_ids maps label name -> Gmail label id."""
    service.users().messages().modify(
        userId="me", id=message_id,
        body={
            "addLabelIds": [label_ids[name] for name in add],
            "removeLabelIds": [label_ids[name] for name in remove],
        },
    ).execute()


def build_message(to, subject, body, sender, cc=None, message_id=None, attachments=None):
    """attachments: list of file paths to attach (images)."""
    msg = EmailMessage()
    msg["From"] = f"Playmaker Entertainment <{sender}>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(body)
    for path in attachments or []:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
            subtype = "jpeg" if ext in ("jpg", "jpeg") else ext
            msg.add_attachment(data, maintype="image", subtype=subtype,
                               filename=os.path.basename(path))
        except OSError as exc:  # noqa: BLE001 - skip a missing/unreadable image, still send
            log.warning("Skipping attachment %s: %s", path, exc)
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}


def draft_once(service, dedupe_query, to, subject, body, sender, cc=None):
    """Save a Gmail draft unless one (or a sent copy) already matches
    dedupe_query, so a restart doesn't pile up duplicate drafts. Returns
    True if a draft was created."""
    resp = service.users().messages().list(
        userId="me", q=f"(in:draft OR in:sent) {dedupe_query}", maxResults=1).execute()
    if resp.get("messages"):
        return False
    service.users().drafts().create(
        userId="me", body={"message": build_message(to, subject, body, sender, cc)}).execute()
    return True


def already_sent(service, query):
    """True if the Sent folder has a message matching this Gmail search.

    (Searching by our own Message-ID header doesn't work: Gmail replaces it
    on messages sent through the API, which let the 2026-10-03 follow-up go
    out every hour. Each caller passes a search on what the email visibly
    contains instead.)"""
    resp = service.users().messages().list(
        userId="me", q=f"in:sent {query}", maxResults=1
    ).execute()
    return bool(resp.get("messages"))


def send_once(service, message_id, to, subject, body, sender, cc=None, dedupe_query=None,
              attachments=None):
    """Send unless the Sent folder already has a matching email (dedupe_query,
    a Gmail search; default: same recipient and exact subject), so a
    restarted run never emails the same person twice. Returns True if sent."""
    query = dedupe_query or f'to:{to} subject:"{subject}"'
    if already_sent(service, query):
        return False
    service.users().messages().send(
        userId="me",
        body=build_message(to, subject, body, sender, cc, message_id, attachments=attachments),
    ).execute()
    return True

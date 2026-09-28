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
import os
import re
from email.message import EmailMessage

from config.rules import normalize_venue_name

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Not limited to unread mail: the team often opens requests on a phone
# before Amy runs. The Processed/Exception labels mark a request as handled.
SEARCH_QUERY = (
    '(subject:"New guest list request submission" OR subject:"Guest List Request") '
    'newer_than:30d -in:trash -label:Playmaker/Processed -label:Playmaker/Exception'
)

PROCESSING_LABEL = "Playmaker/Processing"
PROCESSED_LABEL = "Playmaker/Processed"
EXCEPTION_LABEL = "Playmaker/Exception"

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
    else:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
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

    for label, value in LINE_RE.findall(_form_block(body_text)):
        key = label.lower().strip()
        if key.startswith("female guests"):
            fields["female_count"] = value
        elif key.startswith("male guests"):
            fields["male_count"] = value
        elif key in LABEL_TO_FIELD:
            fields.setdefault(LABEL_TO_FIELD[key], value)  # first occurrence wins

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


def build_message(to, subject, body, sender, cc=None, message_id=None):
    msg = EmailMessage()
    msg["From"] = f"Playmaker Entertainment <{sender}>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(body)
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}


def already_sent(service, message_id):
    resp = service.users().messages().list(
        userId="me", q=f"in:sent rfc822msgid:{message_id}", maxResults=1
    ).execute()
    return bool(resp.get("messages"))


def send_once(service, message_id, to, subject, body, sender, cc=None):
    """Send unless a message with this Message-ID was already sent, so a
    restarted run never emails the same person twice. Returns True if sent."""
    if already_sent(service, message_id):
        return False
    service.users().messages().send(
        userId="me", body=build_message(to, subject, body, sender, cc, message_id)
    ).execute()
    return True

"""
Reads new guest-list request emails and parses them into the field
dict rules_engine.normalize_guest_request expects.

Uses the Gmail API (not IMAP) so it can run unattended with a refresh token.
Auth: create a Google Cloud OAuth client (Desktop App type), enable the
Gmail API, and run a one-time authorization flow to obtain token.json.
See README.md for the exact steps.
"""
import base64
import re
from email.message import EmailMessage

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

SEARCH_QUERY = (
    'is:unread (subject:"New guest list request submission" OR '
    'subject:"Guest List Request") -label:Playmaker/Processed -label:Playmaker/Exception'
)

FIELD_PATTERNS = {
    "name": r"Name\s*\n+(.+)",
    "email": r"Email\s*\n+([\w\.\-\+]+@[\w\.\-]+)",
    "start_date": r"Visit start date\s*\n+([\d]{4}-[\d]{2}-[\d]{2})",
    "end_date": r"Visit end date\s*\n+([\d]{4}-[\d]{2}-[\d]{2})",
    "venues": r"Venues\s*\n+(.+)",
    "female_count": r"Female guests[^\n]*\n+(\d+)",
    "male_count": r"Male guests[^\n]*\n+(\d+)",
    "phone": r"Phone\s*\n+([\d\-\+\(\) ]+)",
    "promoter": r"Promoter\s*\n+(.+)",
}

# Form labels, used to reject a "value" that is really the next field's label
# (which is what the patterns above capture when a field is left blank).
FIELD_LABEL_RE = re.compile(
    r"^(Name|Email|Phone|Visit start date|Visit end date|Venues|Female guests|Male guests|Promoter)"
    r"(\s*\(.*\))?\s*$"
)


PROCESSED_LABEL = "Playmaker/Processed"
EXCEPTION_LABEL = "Playmaker/Exception"


def get_service(token_path="token.json"):
    # Imported here so the parsing helpers can be used (and tested) without
    # the Google client libraries installed.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_pending_requests(service, max_results=25):
    resp = service.users().messages().list(
        userId="me", q=SEARCH_QUERY, maxResults=max_results
    ).execute()
    return resp.get("messages", [])


def get_plain_text_body(service, message_id):
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    def walk(parts):
        for part in parts:
            if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "ignore")
            if "parts" in part:
                found = walk(part["parts"])
                if found:
                    return found
        return None

    payload = msg["payload"]
    if "parts" in payload:
        body = walk(payload["parts"]) or ""
    else:
        body = base64.urlsafe_b64decode(payload["body"].get("data", "")).decode("utf-8", "ignore")
    return msg, body


def parse_request(message_id, body_text):
    fields = {"source_message_id": message_id}
    for key, pattern in FIELD_PATTERNS.items():
        m = re.search(pattern, body_text)
        if m and not FIELD_LABEL_RE.match(m.group(1).strip()):
            fields[key] = m.group(1).strip()

    if "name" in fields:
        parts = fields["name"].split(" ", 1)
        fields["first_name"] = parts[0]
        fields["last_name"] = parts[1] if len(parts) > 1 else ""

    if "venues" in fields:
        fields["venues"] = [v.strip() for v in fields["venues"].split(",")]

    required = ["email", "start_date", "end_date"]
    missing = [f for f in required if f not in fields]
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


def mark_processed(service, message_id, label_id):
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]}
    ).execute()


def build_message(to, subject, body, sender=None):
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    msg.set_content(body)
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}


def send_email(service, to, subject, body, sender=None):
    return service.users().messages().send(
        userId="me", body=build_message(to, subject, body, sender)
    ).execute()

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
from html.parser import HTMLParser

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Not limited to unread mail: the team often opens these on a phone first.
# The Processed/Exception labels are what mark a request as handled.
SEARCH_QUERY = (
    '(subject:"New guest list request submission" OR subject:"Guest List Request") '
    'newer_than:14d -label:Playmaker/Processed -label:Playmaker/Exception'
)

# Form labels as they appear in the GoDaddy/Airo notification, mapped to
# field names. Matched case-insensitively, ignoring any "(...)" suffix such as
# "Female guests (free before 1am)" and a trailing colon.
FIELD_LABELS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "visit start date": "start_date",
    "visit end date": "end_date",
    "venues": "venues",
    "female guests": "female_count",
    "male guests": "male_count",
    "referred by – promoter first name": "promoter_first",
    "referred by - promoter first name": "promoter_first",
    "referred by – promoter last name": "promoter_last",
    "referred by - promoter last name": "promoter_last",
    "promoter": "promoter",
    "message": "message",
    "submission date": "submission_date",
}

# A value must look like this to be accepted for the field.
FIELD_VALUE_RE = {
    "email": re.compile(r"^[\w\.\-\+]+@[\w\.\-]+$"),
    "start_date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "end_date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "female_count": re.compile(r"^\d+$"),
    "male_count": re.compile(r"^\d+$"),
    "phone": re.compile(r"^[\d\-\+\(\) \.]+$"),
}

NOT_PROVIDED = {"", "not provided", "n/a", "none"}


def _label_key(text):
    """Return the field name if `text` is a form label, else None."""
    text = re.sub(r"\s*\(.*\)\s*$", "", text.strip().rstrip(":")).strip().lower()
    return FIELD_LABELS.get(text)


class _TextExtractor(HTMLParser):
    """Flatten the notification HTML into lines: one per label and per value."""
    BREAKS = {"br", "p", "div", "td", "tr", "table", "h1", "h2", "h3", "li", "b"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag in self.BREAKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BREAKS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.chunks).splitlines())
    return "\n".join(line for line in lines if line)


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


def get_form_text(service, message_id):
    """Return (message, text) for a request email.

    The GoDaddy notification's text/plain part only carries name, email and
    message; the visit dates, venues and guest counts are only in the HTML
    part. So the HTML part is preferred and flattened to text.
    """
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    found = {}

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html") and mime not in found:
            found[mime] = base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        for sub in part.get("parts", []):
            walk(sub)

    walk(msg["payload"])
    if "text/html" in found:
        return msg, html_to_text(found["text/html"])
    return msg, found.get("text/plain", "")


def parse_request(message_id, body_text):
    """Parse "Label" / "Value" lines (value on the next line, or after a
    colon on the same line) into the dict rules_engine expects."""
    fields = {"source_message_id": message_id}
    lines = [line.strip() for line in body_text.splitlines()]

    i = 0
    while i < len(lines):
        line = lines[i]
        key = _label_key(line)
        value = None
        if key:
            # Value is the next non-empty line, unless that is another label
            # (i.e. this field was left blank).
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines) and not _label_key(lines[j]):
                value = lines[j]
                i = j
        elif ":" in line:
            label, _, rest = line.partition(":")
            key = _label_key(label)
            value = rest.strip() if key else None
        i += 1

        if not key or value is None or key in fields:
            continue
        if value.lower() in NOT_PROVIDED:
            continue
        pattern = FIELD_VALUE_RE.get(key)
        if pattern and not pattern.match(value):
            continue
        fields[key] = value

    promoter = " ".join(fields.pop(k) for k in ("promoter_first", "promoter_last") if k in fields)
    if promoter and "promoter" not in fields:
        fields["promoter"] = promoter

    if "name" in fields:
        parts = fields["name"].split(" ", 1)
        fields["first_name"] = parts[0]
        fields["last_name"] = parts[1] if len(parts) > 1 else ""

    if "venues" in fields:
        fields["venues"] = [v.strip() for v in fields["venues"].split(",") if v.strip()]

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

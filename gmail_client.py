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
    f'newer_than:30d -in:trash -label:{PROCESSED_LABEL} -label:{EXCEPTION_LABEL} '
    # Amy's own team@ copy of a website sign-up (sent from this mailbox) is not a new request.
    '-(from:info@playmakerentertainment.com to:team@playmakerentertainment.com) '
    '-(from:me to:team@playmakerentertainment.com)'
)


def is_own_team_copy(msg):
    """Amy's outgoing team@ copy of a website notice: sent from this mailbox
    (SENT label, not in INBOX) to the team address. Never a new request.
    GoDaddy Airo notices to Team@ arrive from GoDaddy into INBOX, so they pass."""
    from config import rules
    labels = set((msg or {}).get("labelIds") or [])
    if "INBOX" in labels or "SENT" not in labels:
        return False
    hdrs = {h.get("name", "").lower(): h.get("value", "") for h in
            ((msg or {}).get("payload") or {}).get("headers") or []}
    return rules.PLAYMAKER_EMAIL.lower() in hdrs.get("to", "").lower()


def website_ref_already_handled(service, ref, exclude_message_id):
    """True if another notice carrying this website submission Ref was already
    handled (labeled Amy/Processed or Amy/Exception)."""
    if not ref:
        return False
    resp = service.users().messages().list(
        userId="me",
        q=f'"Ref: {ref}" (label:{PROCESSED_LABEL} OR label:{EXCEPTION_LABEL})',
        maxResults=10,
    ).execute()
    return any(m["id"] != exclude_message_id for m in resp.get("messages", []))

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
    "source": "source",  # GuestlistGo form notices: "Source: guestlistgo"
    "ref": "form_ref",
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


def is_rate_limited(e):
    """True for Gmail per-minute quota errors (403 rateLimitExceeded / 429)."""
    status = getattr(getattr(e, "resp", None), "status", None)
    return status == 429 or (status == 403 and ("rateLimitExceeded" in str(e) or "Quota exceeded" in str(e)
                                                 or "userRateLimitExceeded" in str(e)))


def get_account_email(service, attempts=6, base_delay=5.0, _sleep=None):
    """Startup profile check. Retries Gmail per-minute quota errors (403/429
    rateLimitExceeded) with backoff instead of crashing, which made Railway
    restart-loop and burn more quota."""
    import time
    from googleapiclient.errors import HttpError
    _sleep = _sleep or time.sleep
    for i in range(attempts):
        try:
            return service.users().getProfile(userId="me").execute()["emailAddress"]
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            limited = status in (403, 429) and ("rateLimitExceeded" in str(e) or "Quota exceeded" in str(e) or status == 429)
            if not limited or i == attempts - 1:
                raise
            delay = min(base_delay * (2 ** i), 60.0)
            log.warning("Gmail rate limited on getProfile; retrying in %.0fs (%d/%d)", delay, i + 1, attempts)
            _sleep(delay)


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

# Matches every follow-up wording used so far (Vegas and Scottsdale).
FOLLOW_UP_SEARCH = ('(subject:"Until next time" OR subject:"See you next time in Vegas" '
                    'OR subject:"Hope you had fun in Vegas" OR subject:"Hope you had fun at")')

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_WEEKDAY_RE = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)"
_MONTH_RE = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
             r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)")
_CONFIRMATION_DATE_RE = re.compile(rf"\b{_WEEKDAY_RE},?\s+({_MONTH_RE})\s+(\d{{1,2}})\b")


def _resolve_date(month_name, day, reference):
    """A confirmation body shows 'Friday, October 9' or 'Sat, Oct 17' (no year).
    Pick the year so the night falls on/after when the email was sent (the visit
    is upcoming), handling the Dec->Jan rollover."""
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


def _parse_confirmation(body, subject="", sent=None):
    """Extract (first_name, venues, last_night) from a confirmation email's
    plain-text body (and subject fallback). Supports both Vegas ('Hi <Name>,')
    and Scottsdale ('Hey <Name>,') templates and both full and abbreviated dates."""
    sent = sent or date.today()
    body = (body or "").replace("\r\n", "\n")
    subject = subject or ""
    combined = f"{subject}\n{body}"

    first_match = (
        re.search(r"\b(?:Hi|Hey)\s+([^,\s!]+),", body)
        or re.search(r"You['’]re on the list,\s*([^.\n,!]+)", body)
        or re.search(r"^([A-Z][A-Za-z'’-]+),\s+you['’]re on the list", body, re.M)
    )
    first_name = first_match.group(1).strip() if first_match else ""
    if first_name.lower() == "there":
        first_name = ""

    venues = [v for v in rules.ALL_AUTHORIZED_VENUES if v in combined]
    for sv in rules.SCOTTSDALE_VENUES:
        if sv not in venues and (
            re.search(re.escape(sv), combined, re.I)
            or (sv == "Riot House" and "riothouse" in combined.lower())
        ):
            venues.append(sv)

    # For "Booked:" / "Added:" subjects without "You're on the list", require
    # guest-list phrasing in the body so unrelated messages are ignored.
    subj_l = subject.lower()
    is_booked_added_only = (
        ("booked:" in subj_l or "added:" in subj_l)
        and CONFIRMATION_SUBJECT_CONCIERGE.lower() not in subj_l
        and CONFIRMATION_SUBJECT_POST_SIGNUP.lower() not in subj_l
    )
    if is_booked_added_only and "on the list" not in body.lower() and "guest list" not in body.lower():
        dates = []
    else:
        raw_dates = _CONFIRMATION_DATE_RE.findall(body) or _CONFIRMATION_DATE_RE.findall(subject)
        dates = [d for d in (_resolve_date(m, day, sent) for m, day in raw_dates) if d]

    return first_name, venues, (max(dates) if dates else None)


_CONFIRMATION_CACHE = None  # message id -> parsed confirmation (sent mail never changes)
_CACHE_VERSION = 2


def _cache_path():
    """Persist on the Railway volume (/data) so restarts don't re-read every
    confirmation. AMY_CACHE_DIR overrides; no /data -> memory only."""
    import os
    d = os.environ.get("AMY_CACHE_DIR") or ("/data" if os.path.isdir("/data") else "")
    return os.path.join(d, "confirmation_cache.json") if d else ""


def _load_cache():
    import json
    path = _cache_path()
    if path:
        try:
            with open(path) as fh:
                raw = json.load(fh)
            return {
                k: dict(v, last_night=date.fromisoformat(v["last_night"]) if v.get("last_night") else None)
                for k, v in raw.items()
                if isinstance(v, dict) and v.get("_v") == _CACHE_VERSION
            }
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - a bad cache just means re-reading
            log.exception("Ignoring unreadable confirmation cache %s", path)
    return {}


def _save_cache(cache):
    import json, os
    path = _cache_path()
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({
                k: dict(v, _v=_CACHE_VERSION,
                        last_night=v["last_night"].isoformat() if v.get("last_night") else None)
                for k, v in cache.items()
            }, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("Could not save confirmation cache %s", path)


def recent_confirmations(service, days=45, cache=None):
    """Guest confirmations Amy sent recently, for the 7-day follow-up:
    [{"id", "to", "first_name", "venues", "last_night"}]. Reads the clubs and
    the guest's last night out of the email body (the concierge confirmation
    subject carries no dates). Parsed messages are cached by id so each pass
    only fetches new confirmations (re-reading all of them every cycle used up
    the Gmail per-minute quota and broke the website form)."""
    global _CONFIRMATION_CACHE
    persist = cache is None
    if persist:
        if _CONFIRMATION_CACHE is None:
            _CONFIRMATION_CACHE = _load_cache()
        cache = _CONFIRMATION_CACHE
    try:
        return _recent_confirmations(service, days, cache)
    finally:
        if persist:
            _save_cache(cache)  # keep progress even when Gmail rate limits mid-pass


def _recent_confirmations(service, days, cache):
    resp = service.users().messages().list(
        userId="me",
        q=(f'in:sent (subject:"{CONFIRMATION_SUBJECT_CONCIERGE}" OR '
           f'subject:"{CONFIRMATION_SUBJECT_POST_SIGNUP}" OR '
           f'subject:"Booked:" OR subject:"Added:") newer_than:{days}d'), maxResults=200,
    ).execute()
    out = []
    for ref in resp.get("messages", []):
        if ref["id"] in cache:
            out.append(cache[ref["id"]])
            continue
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata", metadataHeaders=["To", "Subject"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sent = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc).date()
        _msg, body = get_plain_text_body(service, ref["id"])  # returns (message, text)
        first_name, venues, last_night = _parse_confirmation(body, headers.get("subject", ""), sent)
        item = {"id": ref["id"], "to": headers.get("to", ""),
                "first_name": first_name, "venues": venues, "last_night": last_night,
                "_v": _CACHE_VERSION}
        cache[ref["id"]] = item
        out.append(item)
    for idx, entry in enumerate(_load_branded_sent()):
        if entry.get("kind") == "confirmation" and entry.get("last_night"):
            try:
                ln = date.fromisoformat(entry["last_night"])
            except ValueError:
                continue
            out.append({
                "id": f"branded-{entry.get('ts', idx)}-{idx}",
                "to": entry["to"],
                "first_name": entry.get("first_name") or "",
                "venues": list(entry.get("venues") or []),
                "last_night": ln,
            })
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
    src = (fields.get("source") or "").strip().lower()
    if src == "guestlistgo" or (promoter or "").lower() == "guestlistgo":
        fields["source"] = "guestlistgo"
        fields["brand"] = "GuestlistGo"  # guest-facing mail must be GuestlistGo-branded
    else:
        fields.pop("source", None)

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


def build_message(to, subject, body, sender, cc=None, message_id=None, attachments=None, html=None,
                  headers=None, inline_images=None, from_name="Playmaker Entertainment"):
    """attachments: list of file paths to attach (images). html: optional
    text/html alternative; body stays as the plain-text fallback."""
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{sender}>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = f"<{message_id}>"
    for k, v in (headers or {}).items():  # e.g. List-Unsubscribe
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
        # inline_images: [(path, cid)] shown in the HTML via src="cid:<cid>"
        # (multipart/related under the html alternative).
        html_part = msg.get_payload()[-1]
        for path, cid in inline_images or []:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:  # noqa: BLE001
                log.warning("Skipping inline image %s: %s", path, exc)
                continue
            ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
            html_part.add_related(data, maintype="image", subtype="jpeg" if ext in ("jpg", "jpeg") else ext,
                                  cid=f"<{cid}>", filename=os.path.basename(path), disposition="inline")
    for item in attachments or []:
        # a file path, or (path, filename) to name the attachment
        path, fname = item if isinstance(item, (tuple, list)) else (item, os.path.basename(item))
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
            subtype = "jpeg" if ext in ("jpg", "jpeg") else ext
            msg.add_attachment(data, maintype="image", subtype=subtype,
                               filename=fname)
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


# Follow-up recipients already sent via /send-branded (Zoho SMTP) in Oct 2026,
# which do not appear in Gmail's in:sent folder.
_SEED_FOLLOWUP_RECIPIENTS = frozenset({
    "cleach27@icloud.com",
    "michaelkemas03@gmail.com",
    "djrrrpro@gmail.com",
    "camacho_efanny@yahoo.com",
    "ejya2020@gmail.com",
    "0pierre702@gmail.com",
    "richardwoodley47@gmail.com",
    "solblackwell@gmail.com",
    "guajardo_tanya@yahoo.com",
    "afzain.hossain2020@gmail.com",
    "perez.abbym@gmail.com",
    "solomonomage@gmail.com",
    "emilyamber14@icloud.com",
    "aurelliuscurry@gmail.com",
    "demetriiious1999@gmail.com",
    "tamar.sandonato@gmail.com",
})


def _branded_sent_path():
    d = os.environ.get("AMY_CACHE_DIR") or ("/data" if os.path.isdir("/data") else "")
    return os.path.join(d, "branded_sent.json") if d else ""


def _load_branded_sent():
    import json
    path = _branded_sent_path()
    if path:
        try:
            with open(path) as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Ignoring unreadable branded_sent log %s", path)
    return []


def record_branded_sent(to, subject, body=""):
    """Persist a /send-branded send (e.g. Zoho SMTP, which bypasses Gmail Sent)
    so already_sent() and send_follow_ups() never duplicate it, and confirmations
    sent via Zoho are also visible to recent_confirmations()."""
    import json, time
    path = _branded_sent_path()
    if not path:
        return
    m = EMAIL_RE.search(to or "")
    addr = (m.group(0) if m else (to or "")).strip().lower()
    if not addr:
        return
    subj = (subject or "").strip()
    entry = {"to": addr, "subject": subj, "ts": int(time.time())}
    sl = subj.lower()
    if (CONFIRMATION_SUBJECT_CONCIERGE.lower() in sl
            or CONFIRMATION_SUBJECT_POST_SIGNUP.lower() in sl
            or sl.startswith("booked:") or sl.startswith("added:")):
        first_name, venues, last_night = _parse_confirmation(body or "", subj, date.today())
        if last_night:
            entry["kind"] = "confirmation"
            entry["first_name"] = first_name
            entry["venues"] = venues
            entry["last_night"] = last_night.isoformat()
    entries = _load_branded_sent()
    entries.append(entry)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(entries[-2000:], fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("Could not save branded_sent log %s", path)


def _branded_sent_matches(query):
    """True if query matches a pre-seeded or persisted /send-branded email."""
    q = query or ""
    m_to = re.search(r"\bto:([^\s)]+)", q, re.I)
    if not m_to:
        return False
    addr = m_to.group(1).strip().strip('"').lower()
    ql = q.lower()
    is_followup = "hope you had fun" in ql or "until next time" in ql or "see you next time" in ql
    if is_followup and addr in _SEED_FOLLOWUP_RECIPIENTS:
        return True
    for entry in _load_branded_sent():
        if (entry.get("to") or "").lower() != addr:
            continue
        esub = entry.get("subject") or ""
        if is_followup and esub.lower().startswith("hope you had fun"):
            return True
        m_sub = re.search(r'subject:"([^"]+)"', q, re.I)
        if m_sub and esub == m_sub.group(1):
            return True
    return False


def already_sent(service, query):
    """True if the Sent folder (or persisted /send-branded log) has a message
    matching this search.

    (Searching by our own Message-ID header doesn't work: Gmail replaces it
    on messages sent through the API, which let the 2026-10-03 follow-up go
    out every hour. Each caller passes a search on what the email visibly
    contains instead.)"""
    resp = service.users().messages().list(
        userId="me", q=f"in:sent {query}", maxResults=1
    ).execute()
    if resp.get("messages"):
        return True
    return _branded_sent_matches(query)


def send_once(service, message_id, to, subject, body, sender, cc=None, dedupe_query=None,
              attachments=None, html=None, headers=None, inline_images=None):
    """Send unless the Sent folder already has a matching email (dedupe_query,
    a Gmail search; default: same recipient and exact subject), so a
    restarted run never emails the same person twice. Returns True if sent."""
    query = dedupe_query or f'to:{to} subject:"{subject}"'
    if already_sent(service, query):
        return False
    service.users().messages().send(
        userId="me",
        body=build_message(to, subject, body, sender, cc, message_id, attachments=attachments, html=html,
                           headers=headers, inline_images=inline_images),
    ).execute()
    return True

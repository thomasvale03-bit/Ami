"""
Posh signups: "NEW POSH SIGNUP" emails that Zapier sends from the intake
inbox (valeconsultingaz@gmail.com) to team@playmakerentertainment.com.

Zapier template (one field per line):

    NEW POSH CLIENT
    CLIENT INFORMATION
    Name: <first> <last>
    Email: <email>
    Phone: <phone>
    EVENT INFORMATION
    Event: <Posh event name>
    Event Date: <event start, ISO 8601 UTC, e.g. 2026-10-06T05:30:00.000Z>
    Ticket: <ticket name>
    ORDER INFORMATION
    Order Number / Promo Code / Tracking Link / Date Purchased
    SOURCE: POSH

parse_signup() turns it into the same dict gmail_client.parse_request
returns for website submissions, so it runs through the same workflow.
"""
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import rules

VEGAS = ZoneInfo("America/Los_Angeles")

# Events that start before this hour (Las Vegas) belong to the previous night.
NIGHT_ROLLOVER_HOUR = 6

FIELDS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "event": "event_name",
    "event date": "event_start",
    "ticket": "ticket",
    "order number": "order_number",
    "promo code": "promo_code",
    "tracking link": "tracking_link",
    "date purchased": "date_purchased",
}

LINE_RE = re.compile(r"^[ \t]*([A-Za-z][A-Za-z ]*?)[ \t]*:[ \t]*(.*?)[ \t]*$", re.MULTILINE)

FEMALE_RE = re.compile(r"\b(ladies|lady|female|females|women|woman|girls?)\b", re.I)
MALE_RE = re.compile(r"\b(men|man|male|males|gents?|gentlemen|guys?|boys?)\b", re.I)

# Longest aliases first so "omnia dayclub" wins over "omnia".
VENUE_ALIASES = sorted(rules.VENUE_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)


def is_posh_signup(subject, body):
    text = f"{subject or ''}\n{body or ''}"
    return bool(re.search(r"NEW POSH (SIGNUP|CLIENT)|SOURCE:\s*POSH", text, re.I))


def consent_on_file():
    """Set POSH_CONSENT_ON_FILE=true only once the Posh checkout requires the
    guest to confirm 21+ and authorize Playmaker to register them on the TAO
    guest list (TAO authorization sections 3-4). Until then Posh signups go
    to the team instead of being booked."""
    return os.environ.get("POSH_CONSENT_ON_FILE", "").strip().lower() in ("1", "true", "yes")


def _clean(value):
    # An unrendered Zapier placeholder ("{{=gives[...]}}") means no value.
    value = (value or "").strip()
    return "" if not value or "{{" in value else value


def venue_from_event(event_name):
    text = (event_name or "").lower()
    for alias, venue in VENUE_ALIASES:
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", text):
            return venue
    return None


def night_of(event_start):
    """The Las Vegas date of the night an event belongs to."""
    try:
        start = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=VEGAS)
    local = start.astimezone(VEGAS)
    if local.hour < NIGHT_ROLLOVER_HOUR:
        local -= timedelta(days=1)
    return local.date()


def gender_from_ticket(ticket):
    female, male = bool(FEMALE_RE.search(ticket or "")), bool(MALE_RE.search(ticket or ""))
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return None


def parse_signup(message_id, body_text):
    body_text = (body_text or "").replace("\r\n", "\n").replace("\r", "\n")
    posh = {}
    for label, value in LINE_RE.findall(body_text):
        key = FIELDS.get(label.lower().strip())
        if key and key not in posh:
            posh[key] = _clean(value)

    fields = {"source_message_id": message_id, "source": "Posh", "posh": posh}
    name = posh.get("name", "")
    if name:
        parts = name.split(None, 1)
        fields.update(name=name, first_name=parts[0], last_name=parts[1] if len(parts) > 1 else "")
    for key in ("email", "phone"):
        if posh.get(key):
            fields[key] = posh[key]

    night = night_of(posh.get("event_start"))
    if night:
        fields["start_date"] = fields["end_date"] = night.isoformat()

    venue = venue_from_event(posh.get("event_name"))
    fields["venues"] = [venue] if venue else []

    gender = gender_from_ticket(posh.get("ticket"))
    fields["female_count"] = "1" if gender == "female" else "0"
    fields["male_count"] = "1" if gender == "male" else "0"
    if not gender:
        fields["_action_needed"] = (
            f"Posh ticket \"{posh.get('ticket') or '(none)'}\" doesn't say female or male, "
            "so the guest-list type can't be chosen.",
            "Check the Posh order and register this guest manually.",
        )

    if posh.get("date_purchased"):
        fields["submitted_at"] = posh["date_purchased"]
    if consent_on_file():
        fields["authorization"] = "YES"
        fields["originating_page"] = "Posh checkout"

    missing = [f for f in ("email", "start_date") if not fields.get(f)]
    if missing:
        fields["_missing_required"] = missing
    return fields

"""Guest + team emails for the Railway booking worker.

Same wording/subjects AMYI used for queue jobs in November 2026
("You're on the Playmaker Entertainment guest list for <date>" to the guest,
"Signed up: <name> — <date>" to team@). Pure functions: no sending here.
"""
from datetime import date

from config import rules
import html as _html

from templates import html_emails

TEAM_TO = rules.PLAYMAKER_EMAIL
# Never route guest-list mail to these (Thomas, Nov 8 2026: no Jarvis).
BLOCKED_RECIPIENTS = frozenset({"infojarvis@mail.grokbot.com"})


def _long(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A %B')} {d.day}"


def _month_day(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}"


def date_label(start, end):
    if start == end:
        return _month_day(start)
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s.month == e.month:
        return f"{_month_day(start)} to {e.day}"
    return f"{_month_day(start)} to {_month_day(end)}"


def _party(female, male):
    parts = []
    if female:
        parts.append(f"{female} female guest{'s' if female != 1 else ''}")
    if male:
        parts.append(f"{male} male guest{'s' if male != 1 else ''}")
    return " and ".join(parts) or "0 guests"


def guest_email(request, bookings):
    """bookings: [{venue, date, event, event_time, order_id}] (TAO only)."""
    first = request.get("first_name") or "there"
    label = date_label(request["start_date"], request["end_date"])
    lines = []
    for b in sorted(bookings, key=lambda b: (b["date"], b["venue"])):
        event = (b.get("event") or "").strip()
        inner = f"{event} — Order ID {b['order_id']}" if event else f"Order ID {b['order_id']}"
        lines.append(f"{_long(b['date'])} - {b['venue']} ({inner})")
    text = "\n\n".join([
        f"Hi {first},",
        "You're on the Playmaker Entertainment guest list. Here's what you're set for:\n" + "\n".join(lines),
        "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission is "
        "subject to each venue's rules, dress code, and capacity.",
        "Questions? Email info@playmakerentertainment.com",
        "Playmaker Entertainment",
    ])
    rows = [(html_emails._short_date(b["date"]), b["venue"], b.get("event"))
            for b in sorted(bookings, key=lambda b: (b["date"], b["venue"]))]
    html = html_emails.render_confirmation(
        first, rows, html_emails.party_size(request["female_count"], request["male_count"]))
    subject = f"You're on the Playmaker Entertainment guest list for {label}"
    return {"to": request["email"], "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{request["email"]} subject:"{subject}"'}


def team_email(ref, raw, request, bookings):
    name = f"{request.get('first_name', '')} {request.get('last_name', '')}".strip() or raw.get("name", "")
    label = date_label(request["start_date"], request["end_date"])
    source = raw.get("source") or "website"
    posh = raw.get("posh") or {}
    if posh.get("order_number"):
        source = f"Posh #{posh['order_number']} {posh.get('event_name') or ''}".strip()
    nights = []
    for b in sorted(bookings, key=lambda b: (b["date"], b["venue"])):
        event = (b.get("event") or "").strip()
        when = f" - {b['event_time']}" if b.get("event_time") else ""
        nights.append(f"{_long(b['date'])} — {b['venue']}" + (f" ({event}{when})" if event else "")
                      + f"\n✅ Signed up — Order ID {b['order_id']}"
                      + (f"\n{b['confirmation_url']}" if b.get("confirmation_url") else ""))
    text = "\n".join([
        "Signed up — no action needed. AMY's Railway booking worker submitted the night(s) below.",
        "",
        "— Guest details —",
        f"Name: {name}",
        f"Email: {request['email']}",
        f"Phone: {raw.get('phone') or '(none)'}",
        f"Party: {_party(request['female_count'], request['male_count'])} (free)",
        f"Promoter: {request.get('promoter') or 'Not provided'}",
        f"Source: {source}",
        "",
        "— Nights —",
        "\n\n".join(nights),
        "",
        f"Amy Ref: {ref}",
    ])
    subject = f"Signed up: {name} — {label}"
    html = "<pre style=\"font-family:Helvetica,Arial,sans-serif;white-space:pre-wrap\">" + \
        _html.escape(text) + "</pre>"
    return {"to": TEAM_TO, "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{TEAM_TO} subject:"{subject}" "Amy Ref: {ref}"'}


def alert_email(ref, state, reason, raw):
    to = rules.WATCHDOG_ALERT_TO
    subject = f"AMY worker needs review: {raw.get('name') or ref} ({state})"
    text = "\n".join([
        f"The Railway booking worker stopped on Amy Ref {ref} and marked it {state}.",
        f"Reason: {reason}",
        f"Guest: {raw.get('name', '')} <{raw.get('email', '')}>",
        f"Venues: {', '.join(raw.get('venues') or [])}",
        f"Dates: {raw.get('start_date')} to {raw.get('end_date')}",
        "Nothing will be retried automatically. Review it in GET /queue/exceptions.",
    ])
    html = "<pre style=\"font-family:Helvetica,Arial,sans-serif;white-space:pre-wrap\">" + \
        _html.escape(text) + "</pre>"
    return {"to": to, "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{to} subject:"{subject}"'}


def allowed_recipient(address):
    return bool(address) and address.strip().lower() not in BLOCKED_RECIPIENTS

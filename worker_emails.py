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


def guest_subject(start, end):
    return f"You're on the Playmaker Entertainment guest list for {date_label(start, end)}"


def drais_guest_subject(start, end):
    return f"You're on the list: Drai's After Hours — Playmaker Entertainment ({date_label(start, end)})"


UPDATE_NOTE = "Updated confirmation: this adds your newest booking to anything we sent you before. All orders listed stay active."


def update_subject(subject, bookings):
    """'Updated:' repeat subject (owner rule) made unique by the order IDs it
    covers, so /send-branded never mistakes a new order for an already-sent
    confirmation, while a retry of the same update is still deduped."""
    ids = sorted({str(b.get("order_id"))[:8] for b in bookings if b.get("order_id")})
    tail = f" (Order {' + '.join(ids)})" if ids else ""
    return ("Updated: " + subject + tail).replace(",", "").replace(";", "")


def pending_note(pending):
    """Guest-facing line for nights of the same request still waiting for
    their guest list to open (they follow in one Updated confirmation)."""
    if not pending:
        return ""
    items = "; ".join(f"{_long(n)} - {v}" for v, n in sorted(pending, key=lambda p: (p[1], p[0])))
    return (f"Still to come: {items}. That guest list isn't posted yet. We'll sign you up as soon as "
            "it opens and send you one updated confirmation.")


def guest_email(request, bookings, update=False, pending=None):
    """bookings: [{venue, date, event, event_time, order_id}] (TAO only).
    pending: [(venue, iso)] nights of this request not bookable yet."""
    first = request.get("first_name") or "there"
    waiting = pending_note(pending)
    label = date_label(request["start_date"], request["end_date"])
    notice = html_emails.FALLBACK_NOTICE
    lines = []
    for b in sorted(bookings, key=lambda b: (b["date"], b["venue"])):
        event = (b.get("event") or "").strip()
        inner = f"{event} — Order ID {b['order_id']}" if event else f"Order ID {b['order_id']}"
        lines.append(f"{_long(b['date'])} - {b['venue']} ({inner})")
        if b.get("fallback"):
            lines.append(notice)
    text = "\n\n".join([
        f"Hi {first},",
        ((UPDATE_NOTE + "\n\n") if update else "")
        + "You're on the Playmaker Entertainment guest list. Here's what you're set for:\n" + "\n".join(lines)
        + (("\n\n" + waiting) if waiting else ""),
        "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission is "
        "subject to each venue's rules, dress code, and capacity.",
        "Questions? Email info@playmakerentertainment.com",
        "Playmaker Entertainment",
    ])
    rows = [(html_emails._short_date(b["date"]), b["venue"], b.get("event"))
            for b in sorted(bookings, key=lambda b: (b["date"], b["venue"]))]
    fallback_nights = [html_emails._short_date(b["date"]) for b in bookings if b.get("fallback")]
    html = html_emails.render_confirmation(
        first, rows, html_emails.party_size(request["female_count"], request["male_count"]),
        " ".join(x for x in ((UPDATE_NOTE if update else ""), waiting) if x) or None,
        fallback_nights=fallback_nights or None)
    subject = guest_subject(request["start_date"], request["end_date"])
    if update:
        subject = update_subject(subject, bookings)
    return {"to": request["email"], "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{request["email"]} subject:"{subject}"'}


def team_email(ref, raw, request, bookings, update=False, pending=None):
    name = f"{request.get('first_name', '')} {request.get('last_name', '')}".strip() or raw.get("name", "")
    label = date_label(request["start_date"], request["end_date"])
    source = raw.get("source") or "website"
    posh = raw.get("posh") or {}
    if posh.get("order_number"):
        source = f"Posh #{posh['order_number']} {posh.get('event_name') or ''}".strip()
    nights = []
    import request_plan
    per_night = request_plan.night_parties(raw)
    for b in sorted(bookings, key=lambda b: (b["date"], b["venue"])):
        event = (b.get("event") or "").strip()
        when = f" - {b['event_time']}" if b.get("event_time") else ""
        f, m = request_plan.party_for_night(raw, b["date"], request["female_count"], request["male_count"])
        party_bit = f" ({f}F {m}M)" if per_night else ""
        nights.append(f"{_long(b['date'])} — {b['venue']}" + (f" ({event}{when})" if event else "")
                      + party_bit
                      + f"\n✅ Signed up — Order ID {b['order_id']}"
                      + (f"\n{b['confirmation_url']}" if b.get("confirmation_url") else ""))
    text = "\n".join([
        ("Updated — no action needed. Another order was added for this guest (new order, added venue or "
         "second sign-up for the same night); every order below is active."
         if update else "Signed up — no action needed. AMY's Railway booking worker submitted the night(s) below."),
        "",
        "— Guest details —",
        f"Name: {name}",
        f"Email: {request['email']}",
        f"Party: {_party(request['female_count'], request['male_count'])} (free)",
        f"Promoter: {request.get('promoter') or raw.get('promoter') or 'Not provided'}",
        *_promoter_contact(raw),
        f"Source: {source}",
        "",
        "— Nights —",
        "\n\n".join(nights),
        *(["", "— Waiting (list not posted yet; booked later in one Updated email) —",
            "\n".join(f"{_long(n)} — {v}" for v, n in sorted(pending, key=lambda p: (p[1], p[0])))]
          if pending else []),
        "",
        f"Amy Ref: {ref}",
    ])
    subject = f"Signed up: {name} — {label}"
    if update:
        subject = update_subject(subject, bookings)
    html = "<pre style=\"font-family:Helvetica,Arial,sans-serif;white-space:pre-wrap\">" + \
        _html.escape(text) + "</pre>"
    return {"to": TEAM_TO, "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{TEAM_TO} subject:"{subject}" "Amy Ref: {ref}"'}


def _promoter_contact(raw):
    """The promoter's own email/phone from the intake form, when given (never
    the guest's phone)."""
    return ([f"Promoter email: {raw['promoter_email']}"] if raw.get("promoter_email") else []) + \
        ([f"Promoter phone: {raw['promoter_phone']}"] if raw.get("promoter_phone") else [])


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


# --- Drai's After Hours (SpeakeasyGo list; same emails AMYI sent by hand) ----

DRAIS_TEXT = """PLAYMAKER ENTERTAINMENT'S GUESTLIST
Drai's After Hours @ Vanderpump Hotel - doors open at 1 AM
Free champagne for ladies, 1-2 AM
Ladies: FREE until 3 AM
Guys: FREE until 3 AM with an even ratio (at least one girl per guy)
Reduced cover after 3 AM
At the door, tell them you're on Playmaker Entertainment's guestlist, and bring a valid 21+ photo ID.
Tag us on IG so we can repost your story: @playmaker.entertainment"""
DRAIS_VENUE = "Drai's After Hours"


def _drais_html(first, nights, party):
    page = html_emails.render_confirmation(first, nights, party)
    a, b = page.index("<!-- How to get passes -->"), page.index("<!-- Before you go -->")
    page = page[:a] + html_emails._read("partials/drais_details.html") + page[b:]
    page = (page.replace("https://booketing.com/microsite/pme/", "https://speakeasygo.com/ms/playmaker")
                .replace("Book a VIP Table", "Tickets &amp; VIP Tables")
                .replace("Follow the steps to grab your passes before you head out.", "Arrive before 3 AM.")
                .replace("Skip the line and lock in your own table", "Lock in your own table"))
    return page


def drais_guest_email(request, bookings):
    first = request.get("first_name") or "there"
    nights = [(html_emails._short_date(b["date"]), DRAIS_VENUE, None)
              for b in sorted(bookings, key=lambda b: b["date"])]
    text = (f"Hi {first},\n\nYou're on the list for:\n" + "\n".join(f"{d} - {v}" for d, v, _ in nights)
            + "\n\n" + DRAIS_TEXT + "\n\nTickets & VIP Tables: https://speakeasygo.com/ms/playmaker"
            "\n\nQuestions? Email info@playmakerentertainment.com\n\nPlaymaker Entertainment")
    text = text.replace("Skip the line and lock in your own table", "Lock in your own table")
    html = _drais_html(first, nights, html_emails.party_size(request["female_count"], request["male_count"]))
    for body in (html, text):
        low = body.lower()
        if "skip the line" in low or "discount" in low:
            raise ValueError("Drai's email must not promise skip-the-line or discounts")
    subject = drais_guest_subject(request["start_date"], request["end_date"])
    return {"to": request["email"], "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{request["email"]} subject:"{subject}"'}


def drais_team_email(ref, raw, request, bookings):
    name = f"{request.get('first_name', '')} {request.get('last_name', '')}".strip() or raw.get("name", "")
    f, m = int(request["female_count"]), int(request["male_count"])
    posh = raw.get("posh") or {}
    source = f"Posh order {posh['order_number']}" if posh.get("order_number") else (raw.get("source") or "website")
    lines = ["ALREADY SIGNED UP — NO ACTION NEEDED", f"Guest: {name}", f"Email: {request['email']}",
             f"Party: {f} women, {m} men",
             f"Promoter: {request.get('promoter') or raw.get('promoter') or 'Not provided'}",
             *_promoter_contact(raw)]
    # Thomas: no phone numbers in emails; Drai's is confirmation-email only (no Speakeasy).
    import request_plan
    per_night = request_plan.night_parties(raw)
    for b in sorted(bookings, key=lambda b: b["date"]):
        nf, nm = request_plan.party_for_night(raw, b["date"], request["female_count"], request["male_count"])
        party_bit = f"({nf}F {nm}M) " if per_night else ""
        lines.append(f"{DRAIS_VENUE}, {date.fromisoformat(b['date']).strftime('%a')} {b['date']} "
                     f"{party_bit}(confirmation email sent, no Speakeasy signup)")
    lines += [f"Source: {source}", f"AMY ref: {ref}"]
    text = "\n".join(lines)
    label = date_label(request["start_date"], request["end_date"])
    subject = f"Signed up: {name} - Drai's AH {label} ({f}F {m}M)".replace(",", "")
    html = "<pre>" + _html.escape(text, quote=False) + "</pre>"
    return {"to": TEAM_TO, "subject": subject, "text": text, "html": html,
            "dedupe_query": f'to:{TEAM_TO} subject:"{subject}" "AMY ref: {ref}"'}

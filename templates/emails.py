"""Confirmation email templates, verbatim from the approved Playmaker wording."""


def cutoff_sentence(female_cutoff=None, male_cutoff=None, single_cutoff=None,
                     first_300_male=False, cutoff_passed=False):
    if cutoff_passed and single_cutoff:
        return (f"The standard arrival cutoff was {single_cutoff}. Because that cutoff has "
                f"passed, admission may be subject to venue capacity, cover charge, and "
                f"management discretion.")
    if first_300_male:
        return ("Female guests should arrive before 1:00 AM. Complimentary admission for male "
                "guests is guaranteed only for the first 300 male arrivals; after that, entry "
                "is subject to capacity and cover charge.")
    if female_cutoff and male_cutoff:
        return f"Female guests should arrive before {female_cutoff}. Male guests should arrive before {male_cutoff}."
    if single_cutoff:
        return f"Please arrive before {single_cutoff}."
    return "Please check with the venue for arrival requirements."


TAO_APP_URL = "https://apps.apple.com/us/app/tao-group-hospitality-rewards/id1537602625"


def _guests(n, word):
    return f"{n} {word} guest{'s' if n != 1 else ''}"


def party_phrase(female_count, male_count):
    parts = [_guests(n, w) for n, w in ((female_count, "female"), (male_count, "male")) if n]
    return " and ".join(parts)


def _long_date(iso_date):
    from datetime import date
    d = date.fromisoformat(iso_date)
    return f"{d.strftime('%A, %B')} {d.day}"


def _event_line(r):
    when = _long_date(r["date"]) + (f" at {r['event_time']}" if r.get("event_time") else "")
    event = (r.get("event") or "").strip()
    what = r["venue"] if not event or event.lower().startswith(r["venue"].lower()) else f"{r['venue']}: {event}"
    return f"{when} — {what}\nOrder ID: {r['confirmation_id']}"


def drais_guestlist(drais):
    """Drai's After Hours has no portal: guests show this at the door
    (owner's text, Oct 4 2026). drais: name, female_count, male_count, nights."""
    nights = ", ".join(_long_date(n) for n in drais["nights"])
    return (
        "Playmaker Entertainment’s\n"
        "GUESTLIST\n\n"
        f"Name: {drais['name']}\n"
        f"Party: {party_phrase(drais['female_count'], drais['male_count'])}\n"
        f"Night{'s' if len(drais['nights']) > 1 else ''}: {nights}\n\n"
        "Drai’s After Hours @ Vanderpump Hotel, opens at 1 AM\n"
        "Free champagne for ladies 1–2 AM\n"
        "Ladies free until 3 AM\n"
        "Guys: even ratio (at least one girl per guy) free until 3 AM\n"
        "Reduced cover after 3 AM\n\n"
        "Tag me on IG so I can repost your story\n"
        "@playmaker.entertainment"
    )


def consolidated_confirmation(first_name, guest_email, date_label, registrations, drais=None):
    """One email for all verified registrations of a request (owner-approved
    wording, Sept 28 2026). registrations: dicts with venue, event,
    event_time (optional), date, female_count, male_count, confirmation_id.
    drais: see drais_guestlist; added when the guest asked for Drai's."""
    parts = [f"Hi {first_name},"]
    if registrations:
        first = registrations[0]
        parts += [
            f"Your Playmaker Entertainment guest-list registrations are confirmed for "
            f"{party_phrase(first['female_count'], first['male_count'])}:",
            "\n\n".join(_event_line(r) for r in registrations),
            "Please arrive early and bring a current, valid government-issued photo ID. "
            "All guests must be 21+.",
            "Your passes will be in the TAO ticket wallet:\n"
            f"1. Download the TAO Group Hospitality Rewards app: {TAO_APP_URL}\n"
            f"2. Sign up using the same email address used for the guest list: {guest_email}\n"
            "3. Open the Ticket Wallet section to view your passes once they're issued.",
        ]
    if drais:
        parts += [
            "You’re on the guest list for Drai’s After Hours. There’s no ticket for Drai’s: "
            "show this email at the door to get in.",
            drais_guestlist(drais),
        ]
        if not registrations:
            parts.append("Please bring a current, valid government-issued photo ID. All guests must be 21+.")
    parts += [
        "Guest-list admission is subject to each venue’s posted rules, arrival requirements, "
        "dress code, and capacity.",
        "Enjoy Las Vegas!",
        "Playmaker Entertainment",
    ]
    body = "\n\n".join(parts)
    subject = f"Playmaker Guest List Confirmation — {date_label}"
    return subject, body


def internal_processed_record(guest_name, guest_email, registrations, promoter, customer_email_status):
    lines = ["PROCESSED", "", f"Guest: {guest_name}", f"Email: {guest_email}"]
    for r in registrations:
        lines += [
            f"Venue: {r['venue']}",
            f"Event: {r['event']}",
            f"Date: {r['date']}",
            f"Female guests: {r['female_count']}",
            f"Male guests: {r['male_count']}",
            f"Promoter: {promoter or 'Not provided'}",
            f"Confirmation: {r['confirmation_id']}",
        ]
    lines.append(f"Customer email: {customer_email_status}")
    return "\n".join(lines)


def internal_action_needed(guest_name, guest_email, venue, date, issue, required_action):
    return (
        "ACTION NEEDED\n\n"
        f"Guest: {guest_name}\n"
        f"Email: {guest_email}\n"
        f"Venue: {venue}\n"
        f"Date: {date}\n"
        f"Issue: {issue}\n"
        f"Required action: {required_action}"
    )


FOLLOW_UP_DELAY_DAYS = 7


def _clubs(venues):
    """ "at the nightclub", "at the nightclubs and dayclub", ... from the venues booked."""
    from config import rules
    day = sum(1 for v in venues if v in rules.DAYCLUB_PRIORITY or "dayclub" in v.lower())
    night = len(venues) - day
    kinds = [f"{w}{'s' if n > 1 else ''}" for n, w in ((night, "nightclub"), (day, "dayclub")) if n]
    return "at the " + " and ".join(kinds) if kinds else "in Vegas"


def follow_up_email(first_name, venues=()):
    """Sent once, FOLLOW_UP_DELAY_DAYS after a confirmed guest's last night.
    Owner's wording (Oct 4 2026): thank them, ask for referrals, point to
    the website and Instagram. No phone number."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    subject = f"Hope you had fun in Vegas, {first_name}" if first_name else "Hope you had fun in Vegas"
    body = (
        f"{greeting}\n\n"
        f"Hope you had fun {_clubs(venues)}!\n\n"
        "If you have any friends or family coming into town, send them to "
        "PlaymakerEntertainment.com. We’ll make sure they have a great time.\n\n"
        "Have a question, or want to stay up to date with us? "
        "Follow us on Instagram @Playmaker.Entertainment.\n\n"
        "We can’t wait to connect with you again!\n\n"
        "Playmaker Entertainment\n\n"
        "If you’d rather not get emails like this, just reply STOP."
    )
    return subject, body

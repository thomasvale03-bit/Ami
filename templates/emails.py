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


def consolidated_confirmation(first_name, guest_email, date_label, registrations):
    """One email for all verified registrations of a request (owner-approved
    wording, Sept 28 2026). registrations: dicts with venue, event,
    event_time (optional), date, female_count, male_count, confirmation_id."""
    first = registrations[0]
    body = (
        f"Hi {first_name},\n\n"
        f"Your Playmaker Entertainment guest-list registrations are confirmed for "
        f"{party_phrase(first['female_count'], first['male_count'])}:\n\n"
        + "\n\n".join(_event_line(r) for r in registrations) +
        "\n\nPlease arrive early and bring a current, valid government-issued photo ID. "
        "All guests must be 21+.\n\n"
        "Your passes will be in the TAO ticket wallet:\n"
        f"1. Download the TAO Group Hospitality Rewards app: {TAO_APP_URL}\n"
        f"2. Sign up using the same email address used for the guest list: {guest_email}\n"
        "3. Open the Ticket Wallet section to view your passes once they're issued.\n\n"
        "Guest-list admission is subject to each venue’s posted rules, arrival requirements, "
        "dress code, and capacity.\n\n"
        "Enjoy Las Vegas!\n\n"
        "Playmaker Entertainment"
    )
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


def follow_up_email(first_name):
    """Sent once, FOLLOW_UP_DELAY_DAYS after a confirmed guest's last night
    (owner-approved wording, Sept 28 2026)."""
    greeting = f"Hi {first_name},\n\n" if first_name else ""
    body = (
        greeting
        + "Vegas isn’t goodbye — it’s see you next time. 🎲\n"
        "When you’re back, guestlist & VIP tables are handled.\n\n"
        "Got friends coming to Vegas? Send them my way — I’ll make sure they’re VIP. 💯\n\n"
        "Amy  | Playmaker Entertainment\n"
        "PlaymakerEntertainment.com\n"
        "📸 @playmaker.entertainment\n\n"
        "Reply STOP and we won’t send these emails again."
    )
    return "See you next time in Vegas 🎲", body

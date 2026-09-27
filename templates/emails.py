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


def consolidated_confirmation(first_name, guest_email, date_label, registrations):
    """
    registrations: list of dicts, each with keys:
        venue, event, female_count, male_count, arrival_text, confirmation_id
    """
    event_blocks = []
    for r in registrations:
        event_blocks.append(
            f"* {r['venue']} — {r['event']}\n\n"
            f"Guest list: {r['female_count']} female guest(s) + {r['male_count']} male guest(s)\n\n"
            f"Arrival: {r['arrival_text']}\n\n"
            f"Confirmation: {r['confirmation_id']}"
        )
    body = (
        f"Hi {first_name},\n\n"
        f"Your Playmaker Entertainment guest-list registrations for {date_label} have been confirmed:\n\n"
        + "\n\n".join(event_blocks) +
        "\n\nImportant: All guests must be 21 or older and present a current, valid, original "
        "government-issued photo ID. Complimentary admission is subject to venue capacity and "
        "the stated arrival requirements. Arriving after a cutoff may require paying a cover charge.\n\n"
        f"Your passes should be available through the TAO ticket wallet. Please register or sign "
        f"in using this same email address: {guest_email}.\n\n"
        "Enjoy Las Vegas!\n\n"
        "Playmaker Entertainment\n"
        "team@playmakerentertainment.com\n"
        "PlaymakerEntertainment.com"
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

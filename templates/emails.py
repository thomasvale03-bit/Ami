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


def manual_work_order(request, nights, promoter_url, drais_nights=()):
    """Team email for a request Amy can't auto-submit (TAO security check up).
    nights: [{"date": iso, "nightclubs": [...], "dayclubs": [...]}] in order.
    The team opens the promoter link, clears the check as a human, and books."""
    name = f"{request['first_name']} {request['last_name']}".strip()
    lines = [
        "MANUAL BOOKING — TAO's security check is up, so book this one by hand.",
        "",
        f"Guest: {name}",
        f"Email: {request['email']}",
        f"Phone: {request.get('phone') or '(none)'}",
        f"Party: {party_phrase(request['female_count'], request['male_count'])}",
        "",
        "Open this link, clear the “I’m human” check, then book each night:",
        promoter_url,
        "",
    ]
    for n in nights:
        clubs = ", ".join(n["nightclubs"][:5]) or "(no routed club)"
        lines.append(f"{_long_date(n['date'])} — book the first that's open: {clubs}")
        if n.get("dayclubs"):
            lines.append(f"    dayclub (only if a free Pass is live): {', '.join(n['dayclubs'][:3])}")
    if drais_nights:
        lines += ["", "Drai’s After Hours: no booking needed — the guest’s confirmation "
                  "already carries the door text."]
    lines += ["", "When it's booked, open the draft confirmation Amy saved "
              "(to the guest, team CC’d), fill in the club and Order ID, and send it."]
    subject = f"Amy — book by hand: {name}, {request['start_date']}" + (
        f" to {request['end_date']}" if request['end_date'] != request['start_date'] else "")
    return subject, "\n".join(lines)


def manual_confirmation_draft(request, nights, drais=None):
    """A confirmation pre-written for the guest, with the club and Order ID
    left as blanks for the booker to fill after booking by hand."""
    placeholders = [{
        "venue": "[club booked]", "event": "", "event_time": None,
        "date": n["date"], "female_count": request["female_count"],
        "male_count": request["male_count"], "confirmation_id": "[Order ID]",
    } for n in nights]
    date_label = request["start_date"] if request["start_date"] == request["end_date"] \
        else f"{request['start_date']} to {request['end_date']}"
    return consolidated_confirmation(
        request["first_name"], request["email"], date_label, placeholders, drais=drais)


def guest_signup_links_email(first_name, nights, promoter_url, drais=None):
    """Email to the guest with their free guest-list link for each night.
    nights: [{"date": iso, "venue": str, "url": str|None, "options": [str]}].
    When url is present the guest taps straight through; otherwise they get the
    promoter link and the clubs to pick, in order."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    parts = [greeting,
             "You're on the list with Playmaker Entertainment. Tap your link for each "
             "night and add your name — it's free and it's our guest list:"]
    for n in nights:
        when = _long_date(n["date"])
        if n.get("url"):
            parts.append(f"{when} — {n['venue']}\n{n['url']}")
        else:
            opts = ", ".join(n.get("options", [])[:3]) or "any open club"
            parts.append(f"{when} — open our guest list and pick {opts}:\n{promoter_url}")
    if drais:
        parts.append("Drai’s After Hours — no link needed, just show this at the door:\n\n"
                     + drais_guestlist(drais))
    parts += [
        "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list "
        "admission is subject to each venue’s rules, dress code, and capacity.",
        "Enjoy Las Vegas!",
        "Playmaker Entertainment",
    ]
    subject = f"Your Vegas guest list is ready, {first_name}" if first_name else "Your Vegas guest list is ready"
    return subject, "\n\n".join(parts)


def guest_confirmation(first_name, nights, drais=None):
    """Plain 'you're on the list' confirmation for the guest. No signup link;
    it just states the clubs they're set for. nights: [{'date','venue'}]."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    parts = [greeting, "You're on the Playmaker Entertainment guest list. Here's what you're set for:"]
    for n in nights:
        line = f"{_long_date(n['date'])} — {n['venue']}"
        if n.get("headliner"):
            line += f" ({n['headliner']})"
        if n.get("dayclub"):
            line += f"  +  {n['dayclub']} (daytime)"
        if n.get("rerouted_from"):
            line += (f"\n({n['rerouted_from']}'s guest list was already closed for same-day "
                     f"sign-ups, so we moved you to {n['venue']}.)")
        parts.append(line)
    if drais:
        parts.append("Drai’s After Hours — just show this at the door:\n\n" + drais_guestlist(drais))
    if nights:  # TAO venues issue passes through their app; Drai's-only has no wallet
        parts.append(
            "To access your passes:\n"
            "1. Download the TAO Group Hospitality Rewards app.\n"
            "2. Sign up using the same email address you used on the form.\n"
            "3. Open the Ticket Wallet section to view your passes once they're issued.\n"
            f"Download: {TAO_APP_URL}")
    parts += [
        "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission "
        "is subject to each venue’s rules, dress code, and capacity.",
        "See you in Vegas!",
        "Playmaker Entertainment",
    ]
    return "You're on the list — Playmaker Entertainment", "\n\n".join(parts)


def jose_signup_order(request, nights, drais_nights=()):
    """Team email telling the admin (Jose) exactly who to sign up on TAO.

    For each night the chosen venue's direct sign-up link is included when it's
    live on the feed, so Jose just taps it, clears the quick check himself, and
    submits — the guest's details are right here to drop in. The link opens the
    official TAO/TicketSauce page through the promoter link, so it credits
    Playmaker."""
    name = f"{request['first_name']} {request['last_name']}".strip()
    party = party_phrase(request['female_count'], request['male_count'])
    phone = request.get('phone') or '(none)'
    lines = [
        "Jose — tap each night's link below, clear the quick check, and submit. "
        "The guest's details to drop in are right here. Mark done after.", "",
        "— Copy-paste details —",
        f"Name: {name}", f"Email: {request['email']}", f"Phone: {phone}",
        f"Party: {party}",
        f"Promoter: {request['promoter']}" if request.get("promoter") else "Promoter: (none given)",
        "",
        "— Nights —",
    ]
    for n in nights:
        line = f"{_long_date(n['date'])} — {n['venue']}"
        if n.get("headliner"):
            line += f" ({n['headliner']})"
        lines.append(line)
        if n.get("signup_url"):
            lines.append(f"    👉 Sign up: {n['signup_url']}")
        else:
            lines.append("    (no direct link on the feed — open your promoter dashboard for this one.)")
        if n.get("backups"):
            lines.append(f"    backups if full: {', '.join(n['backups'])}")
        if n.get("dayclub"):
            lines.append(f"    + dayclub: {n['dayclub']} (daytime — add it too)")
            if n.get("dayclub_url"):
                lines.append(f"      👉 Sign up: {n['dayclub_url']}")
        if n.get("rerouted_from"):
            lines.append(f"    note: {n['rerouted_from']} skipped — its guest list is closed for "
                         f"same-day sign-ups on Fri/Sat.")
        if n.get("none_live"):
            lines.append("    note: none of the usual clubs show a live guest list tonight on the "
                         "master link — check it for what's open.")
        elif n.get("unverified"):
            lines.append("    note: couldn't read the master link just now — confirm this night "
                         "is open before signing up.")
        lines.append("")
    if drais_nights:
        lines += ["Drai’s After Hours: no signup needed — the guest’s "
                  "confirmation carries the door text.", ""]
    subject = f"Sign up — {name}, {request['start_date']}" + (
        f" to {request['end_date']}" if request['end_date'] != request['start_date'] else "")
    return subject, "\n".join(lines).rstrip() + "\n"


SIGNUP_URL = "PlaymakerEntertainment.com"


def weekly_lineup_email(week_start, days, attached=False):
    """Upbeat weekly newsletter to Playmaker's clients + promoters.

    week_start: ISO date (the Monday). days: list of
    {"date": iso, "venues": [(venue, headliner_or_None), ...]} for Mon–Sun.
    attached: True when flyer images are attached, so the body says so.
    """
    lines = [
        f"🎉 THIS WEEK IN VEGAS — {_long_date(week_start)}",
        "",
        "Here's what's popping off this week at the hottest clubs and dayclubs in town. "
        "Want in? Get on the guest list — it's on us.",
        "",
        f"👉 Sign up at {SIGNUP_URL} and we'll take care of the rest.",
        "",
        "— — —",
        "",
    ]
    any_events = False
    for day in days:
        header = _long_date(day["date"])
        venues = day.get("venues") or []
        if not venues:
            continue
        any_events = True
        lines.append(f"🔥 {header}")
        for venue, headliner in venues:
            lines.append(f"   • {venue}" + (f" — {headliner}" if headliner else ""))
        lines.append("")
    if not any_events:
        lines.append("This week's lineup is still landing — check back soon, or just "
                     f"request your guest list now at {SIGNUP_URL} and we'll slot you in.")
        lines.append("")
    if attached:
        lines += ["📸 This week's flyers are attached — tag us and share them around!", ""]
    lines += [
        "— — —",
        "",
        f"Ready to go out? Request your guest list at {SIGNUP_URL}.",
        "Bringing friends or family to town? Send them our way — we've got you covered.",
        "Follow us on Instagram @playmaker.entertainment for daily updates.",
        "",
        "See you in Vegas! 🍾",
        "Playmaker Entertainment",
    ]
    subject = f"🎉 This Week in Vegas — {_long_date(week_start)}"
    return subject, "\n".join(lines).replace("\n\n\n", "\n\n").strip() + "\n"

"""Guest + team emails for the Railway booking worker.

Same wording/subjects AMYI used for queue jobs in November 2026
("You're on the Playmaker Entertainment guest list for <date>" to the guest,
"Signed up: <name> — <date>" to team@). Pure functions: no sending here.
"""
import re
from datetime import date

from config import rules
import html as _html

from templates import html_emails
from templates.emails import CAPACITY_LINE

TEAM_TO = rules.PLAYMAKER_EMAIL
# Never route guest-list mail to these (Thomas, Nov 8 2026: no Jarvis).
BLOCKED_RECIPIENTS = frozenset({"infojarvis@mail.grokbot.com"})


# Thomas (Nov 21 2026): guest emails never promise line-jumping of any kind.
LINE_PRIORITY_RE = re.compile(r"skip(?:ping)?[\s-]+(?:the[\s-]+)?(?:line|queue)|line[\s-]+priority"
                              r"|priority[\s-]+(?:entry|line|access)|(?:front|jump|bypass)[\s-]+(?:of[\s-]+)?the[\s-]+line",
                              re.I)


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
    # urvenue refs (UV-...) are internal and never shown to the guest; those
    # nights are named by venue + date instead, which keeps each update's
    # subject unique for /send-branded dedupe (Thomas, Nov 21 2026).
    ids = sorted({str(b.get("order_id"))[:8] for b in bookings if b.get("order_id") and not is_urvenue(b)})
    uv = sorted({(b["date"], b["venue"]) for b in bookings if is_urvenue(b)})
    parts = ([f"Order {' + '.join(ids)}"] if ids else []) + [
        f"{v} {date.fromisoformat(d).strftime('%b')} {date.fromisoformat(d).day}" for d, v in uv]
    tail = f" ({' + '.join(parts)})" if parts else ""
    return ("Updated: " + subject + tail).replace(",", "").replace(";", "")


def pending_note(pending):
    """Guest-facing line for nights of the same request still waiting for
    their guest list to open (they follow in one Updated confirmation)."""
    if not pending:
        return ""
    items = "; ".join(f"{_long(n)} - {v}" for v, n in sorted(pending, key=lambda p: (p[1], p[0])))
    nye = nye_note([n for _, n in pending])
    return (f"Still to come: {items}. That guest list isn't posted yet. We'll sign you up as soon as "
            "it opens and send you one updated confirmation." + ((" " + nye) if nye else ""))


URVENUE_SYSTEM = "urvenue"


def is_urvenue(b):
    return (b or {}).get("system") == URVENUE_SYSTEM


# Wynn venues booked on urvenue: Wynn emails the guest and the reservation
# lives in the Wynn Resorts app (Thomas, Nov 21 2026). LIV / LIV Beach do not
# get this section.
WYNN_VENUES = frozenset({"XS Nightclub", "Encore Beach Club", "Encore Beach Club At Night"})
WYNN_INTRO = "Your guest list reservation for {names} lives in the Wynn Resorts app."
WYNN_STEPS = (
    ("Check your email.", "Wynn sends you a confirmation once we book you, and you're on the list from that moment."),
    ("Open the Wynn Resorts app.", "Tap the link in that Wynn confirmation. If you don't have the app yet, "
                                   "it takes you to download it first."),
    ("Sign in or join Wynn Rewards with the same email you signed up with.",
     "That's how the app finds your reservation."),
    ("Find it under My Itinerary.", "It's right on the app's home screen."),
    ("Show your QR code at the door.", "One QR code covers your whole reservation. It's tied to your account "
                                       "and can't be transferred."),
)


def is_wynn(b):
    return is_urvenue(b) and b.get("venue") in WYNN_VENUES


def _names(venues):
    venues = list(dict.fromkeys(venues))
    return venues[0] if len(venues) == 1 else ", ".join(venues[:-1]) + " and " + venues[-1]


def _sorted(bookings):
    return sorted(bookings, key=lambda b: (b["date"], b["venue"]))


LIV_VENUES = ("LIV Nightclub", "LIV Beach")
LIV_HEADINGS = {"LIV Nightclub": "LIV @ FONTAINEBLEAU HOTEL", "LIV Beach": "LIV BEACH @ FONTAINEBLEAU HOTEL"}
LIV_TOP = ("TOM\u2019s V (PME) GUESTLIST", "(Footfall promoter)", "", "Save or Screenshot This EMAIL to get in")
LIV_BOTTOM = ("-IMPORTANT-", "\U0001F6A8come earlier to avoid a big line wait & dress to impress\U0001F6A8", "",
              "NO ATHLETIC GEAR", "NO SHORTS(men)", "", "\U0001F525This Email Is Your Ticket \U0001F525", "",
              "*U R on TOM Vale (PME) guestlist", "-Show this EMAIL at Check in & ID")
LIV_NO_TERMS = "Guestlist terms will be confirmed"


def is_liv(b):
    return is_urvenue(b) and b.get("venue") in LIV_VENUES


def _liv_day(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A %b')} {d.day}"


def liv_nights(bookings, rules=None):
    """[(venue, [(night label lines...)], ...)] per LIV venue, Tom's format.
    Terms only from the rules file (never guessed)."""
    import liv_rules
    rules = liv_rules.load() if rules is None else rules
    out = []
    for venue in LIV_VENUES:
        nights = [b for b in _sorted(bookings) if is_liv(b) and b["venue"] == venue]
        if not nights:
            continue
        blocks = []
        for b in nights:
            rule = liv_rules.for_night(venue, b["date"], rules)
            event = rule.get("event") or (b.get("event") or "").strip()
            lines = [f"{_liv_day(b['date'])}" + (f" - {event}" if event else "")]
            if rule.get("men"):
                lines.append(f"M - {rule['men']}")
            if rule.get("women"):
                lines.append(f"F - {rule['women']}")
            if rule.get("cutoff"):
                lines.append(f"Guestlist til {rule['cutoff']}")
            if not liv_rules.has_terms(rule):
                lines.append(LIV_NO_TERMS)
            if rule.get("notes"):
                lines.append(rule["notes"])
            blocks.append(lines)
        out.append((venue, blocks))
    return out


def liv_missing(bookings, rules=None):
    """LIV / LIV Beach nights in this email with no terms on file."""
    import liv_rules
    rules = liv_rules.load() if rules is None else rules
    return [(b["venue"], b["date"]) for b in _sorted(bookings)
            if is_liv(b) and not liv_rules.has_terms(liv_rules.for_night(b["venue"], b["date"], rules))]


def urvenue_note(bookings, rules=None):
    """Tom's LIV / LIV Beach guest-list text (Thomas, Nov 21 2026)."""
    groups = liv_nights(bookings, rules)
    if not groups:
        return ""
    lines = list(LIV_TOP)
    for venue, blocks in groups:
        lines += ["", LIV_HEADINGS[venue]]
        for block in blocks:
            lines += [""] + block
    lines += [""] + list(LIV_BOTTOM)
    return "\n".join(lines)


def _liv_html(bookings, rules=None):
    groups = liv_nights(bookings, rules)
    if not groups:
        return ""
    e = _html.escape
    p = ('<p style="margin:0 0 4px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:15px;'
         'line-height:23px;color:#E6E6E6;">{}</p>')
    gold = ('<p style="margin:18px 0 8px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:13px;'
            'letter-spacing:2px;color:#D4AF37;font-weight:700;">{}</p>')
    parts = [
        '<p style="margin:0 0 2px 0;font-family:\'Playfair Display\',Georgia,serif;font-size:22px;line-height:28px;'
        f'color:#FFFFFF;font-weight:700;">{e(LIV_TOP[0])}</p>',
        p.format(e(LIV_TOP[1])),
        '<p style="margin:12px 0 0 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:15px;color:#FFFFFF;">'
        f'<strong>{e(LIV_TOP[3])}</strong></p>',
    ]
    for venue, blocks in groups:
        parts.append(gold.format(e(LIV_HEADINGS[venue])))
        for block in blocks:
            parts.append('<p style="margin:10px 0 2px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;'
                         f'font-size:15px;color:#FFFFFF;"><strong>{e(block[0])}</strong></p>')
            parts += [p.format(e(x)) for x in block[1:]]
    bottom = [x for x in LIV_BOTTOM]
    parts.append(gold.format(e(bottom[0])))
    parts.append(p.format(e(bottom[1])))
    parts.append('<p style="margin:12px 0 4px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:15px;'
                 f'color:#FFFFFF;font-weight:700;">{e(bottom[3])}<br>{e(bottom[4])}</p>')
    parts.append('<p style="margin:14px 0 4px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:17px;'
                 f'color:#FFFFFF;font-weight:700;">{e(bottom[6])}</p>')
    parts.append(p.format(e(bottom[8]) + "<br>" + e(bottom[9])))
    body = "\n      ".join(parts)
    return ('    <!-- LIV guest list (Tom\'s format) -->\n'
            '    <tr><td bgcolor="#0d0d0d" class="px bg-card" style="background-color:#0d0d0d;background-image:'
            'linear-gradient(#0d0d0d,#0d0d0d);padding:30px 40px 0 40px;">\n'
            '      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background-color:#141414;border-left:3px solid #D4AF37;border-radius:6px;">'
            '<tr><td style="padding:20px 22px;">\n      ' + body + '\n      </td></tr></table>\n    </td></tr>\n\n')


def _wynn_steps(email=None):
    steps = list(WYNN_STEPS)
    if email:
        head, body = steps[2]
        steps[2] = (head.replace("the same email you signed up with",
                                 f"the same email you signed up with ({email})"), body)
    return steps


XS_VENUES = ("XS Nightclub",)


def xs_terms(bookings, rules=None):
    """[[label, line, ...]] per XS night with terms on Tom's night rules file
    (never guessed: a night with no entry shows nothing)."""
    import liv_rules
    xs = [b for b in _sorted(bookings) if is_urvenue(b) and b.get("venue") in XS_VENUES]
    if not xs:
        return []
    rules = liv_rules.load() if rules is None else rules
    out = []
    for b in xs:
        rule = liv_rules.for_night(b["venue"], b["date"], rules)
        if not liv_rules.has_terms(rule):
            continue
        event = rule.get("event") or (b.get("event") or "").strip()
        lines = [f"XS {_liv_day(b['date'])}" + (f" - {event}" if event else "")]
        if rule.get("women"):
            lines.append(f"F - {rule['women']}")
        if rule.get("men"):
            lines.append(f"M - {rule['men']}")
        if rule.get("cutoff"):
            lines.append(f"Guestlist til {rule['cutoff']}")
        if rule.get("drinks") and rule["drinks"].strip().upper() not in ("N/A", "NA", "NONE", "-"):
            lines.append(f"Drinks - {rule['drinks']}")
        if rule.get("notes"):
            lines.append(rule["notes"])
        out.append(lines)
    return out


def wynn_note(bookings, email=None, rules=None):
    """Plain-text Wynn Resorts app steps for XS / Encore nights (+ XS terms)."""
    venues = [b["venue"] for b in _sorted(bookings) if is_wynn(b)]
    if not venues:
        return ""
    steps = "\n".join(f"{i}. {head} {body}" for i, (head, body) in enumerate(_wynn_steps(email), 1))
    terms = xs_terms(bookings, rules)
    extra = ("\n\nXS guest list terms:\n" + "\n\n".join("\n".join(t) for t in terms)) if terms else ""
    return WYNN_INTRO.format(names=_names(venues)) + "\n" + steps + extra


def _wynn_html(bookings, email=None, rules=None):
    venues = [b["venue"] for b in _sorted(bookings) if is_wynn(b)]
    row = html_emails._read("partials/wynn_steps.html")
    steps = []
    for i, (head, body) in enumerate(_wynn_steps(email), 1):
        steps.append(
            '        <tr>\n'
            '          <td width="44" valign="top" style="padding:0 14px 18px 0;"><table role="presentation" '
            'cellpadding="0" cellspacing="0" border="0"><tr><td align="center" width="32" height="32" bgcolor="#D4AF37" '
            'style="width:32px;height:32px;border-radius:16px;font-family:\'Inter\',Helvetica,Arial,sans-serif;'
            f'font-size:15px;font-weight:700;color:#0d0d0d;">{i}</td></tr></table></td>\n'
            '          <td valign="top" style="padding:4px 0 18px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;'
            f'font-size:15px;line-height:23px;color:#E6E6E6;"><strong style="color:#FFFFFF;">{_html.escape(head)}</strong>'
            f'<br>{_html.escape(body)}</td>\n'
            '        </tr>')
    html = (row.replace("{{heading}}", "Your Wynn Resorts app reservation")
               .replace("{{intro}}", _html.escape(WYNN_INTRO.format(names=_names(venues))))
               .replace("{{steps}}", "\n".join(steps)))
    return html + _xs_terms_html(xs_terms(bookings, rules))


def _xs_terms_html(terms):
    if not terms:
        return ""
    e = _html.escape
    p = ('<p style="margin:0 0 4px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:15px;'
         'line-height:23px;color:#E6E6E6;">{}</p>')
    parts = ['<p style="margin:0 0 6px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:13px;'
             'letter-spacing:2px;color:#D4AF37;font-weight:700;">XS GUEST LIST TERMS</p>']
    for block in terms:
        parts.append('<p style="margin:10px 0 2px 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;'
                     f'font-size:15px;color:#FFFFFF;"><strong>{e(block[0])}</strong></p>')
        parts += [p.format(e(x)) for x in block[1:]]
    return ('    <!-- XS guest list terms (Tom\'s night rules) -->\n'
            '    <tr><td bgcolor="#0d0d0d" class="px bg-card" style="background-color:#0d0d0d;background-image:'
            'linear-gradient(#0d0d0d,#0d0d0d);padding:12px 40px 0 40px;">\n'
            '      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background-color:#141414;border-left:3px solid #D4AF37;border-radius:6px;">'
            '<tr><td style="padding:20px 22px;">\n      ' + "\n      ".join(parts)
            + '\n      </td></tr></table>\n    </td></tr>\n\n')


def _urvenue_html(page, bookings, email=None, rules=None):
    """urvenue nights: Wynn app steps (XS / Encore) and/or the LIV door-list
    check-in. They replace the TAO passes steps when no TAO night is in the
    email, otherwise they are added after them."""
    parts = []
    if any(is_wynn(b) for b in bookings):
        parts.append(_wynn_html(bookings, email, rules))
    liv = _liv_html(bookings, rules)
    if liv:
        parts.append(liv)
    if not parts:
        return page
    only = all(is_urvenue(b) for b in bookings)
    a, b = page.index("<!-- How to get passes -->"), page.index("<!-- Before you go -->")
    if only:
        hero = ("Save this email and show it with your ID at check-in." if all(is_liv(x) for x in bookings)
                else "Your check-in details are below.")
        page = page.replace("Follow the steps to grab your passes before you head out.", hero)
        a, b = page.index("<!-- How to get passes -->"), page.index("<!-- Before you go -->")
        return page[:a] + "".join(parts) + page[b:]
    page = _label_tao_steps(page, [x["venue"] for x in _sorted(bookings) if not is_urvenue(x)])
    b = page.index("<!-- Before you go -->")
    return page[:b] + "".join(parts) + page[b:]


TAO_LABEL = ('<p style="margin:12px 0 0 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:13px;'
             'letter-spacing:2px;text-transform:uppercase;color:#D4AF37;font-weight:700;">For {names}</p>')


def _label_tao_steps(page, venues):
    """Mixed TAO + urvenue trip: say which venues the TAO pass steps are for."""
    if not venues:
        return page
    a = page.index("<!-- How to get passes -->")
    end = page.index("</table>", a) + len("</table>")   # after the gold divider, not inside it
    return page[:end] + "\n      " + TAO_LABEL.format(names=_html.escape(_names(venues))) + page[end:]


def _card_event(b):
    """Night-card subtitle: urvenue nights also show the start time when known."""
    event = (b.get("event") or "").strip()
    when = (b.get("event_time") or "").strip() if is_urvenue(b) else ""
    return " \u00b7 ".join(x for x in (event, when) if x) or b.get("event")


def guest_email(request, bookings, update=False, pending=None):
    """bookings: [{venue, date, event, event_time, order_id}] (TAO only).
    pending: [(venue, iso)] nights of this request not bookable yet."""
    first = request.get("first_name") or "there"
    waiting = pending_note(pending)
    label = date_label(request["start_date"], request["end_date"])
    notice = html_emails.FALLBACK_NOTICE
    rules = None
    if any(is_liv(b) or b.get("venue") in XS_VENUES for b in bookings):
        import liv_rules
        rules = liv_rules.load()  # read at send time: Tom edits it weekly
    liv_text = urvenue_note(bookings, rules)
    lines = []
    for b in sorted(bookings, key=lambda b: (b["date"], b["venue"])):
        event = (b.get("event") or "").strip()
        if is_urvenue(b):
            show = ", ".join(x for x in (event, (b.get("event_time") or "").strip()) if x)
            inner = f"{show} — on the guest list" if show else "on the guest list"
        else:
            inner = f"{event} — Order ID {b['order_id']}" if event else f"Order ID {b['order_id']}"
        lines.append(f"{_long(b['date'])} - {b['venue']} ({inner})")
        if b.get("fallback"):
            lines.append(notice)
    text = "\n\n".join([
        f"Hi {first},",
        ((UPDATE_NOTE + "\n\n") if update else "")
        + "You're on the Playmaker Entertainment guest list. Here's what you're set for:\n" + "\n".join(lines)
        + "\n\n" + CAPACITY_LINE
        + (("\n\n" + waiting) if waiting else "")
        + (("\n\n" + wynn_note(bookings, request["email"], rules)) if wynn_note(bookings) else "")
        + (("\n\n" + liv_text) if liv_text else ""),
        "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission is "
        "subject to each venue's rules, dress code, and capacity.",
        "Questions? Email info@playmakerentertainment.com",
        "Playmaker Entertainment",
    ])
    rows = [(html_emails._short_date(b["date"]), b["venue"], _card_event(b))
            for b in sorted(bookings, key=lambda b: (b["date"], b["venue"]))]
    fallback_nights = [html_emails._short_date(b["date"]) for b in bookings if b.get("fallback")]
    html = html_emails.render_confirmation(
        first, rows, html_emails.party_size(request["female_count"], request["male_count"]),
        " ".join(x for x in ((UPDATE_NOTE if update else ""), waiting) if x) or None,
        fallback_nights=fallback_nights or None)
    html = _urvenue_html(html, bookings, request["email"], rules)
    subject = guest_subject(request["start_date"], request["end_date"])
    if update:
        subject = update_subject(subject, bookings)
    for body in (text, html):
        if LINE_PRIORITY_RE.search(body):
            raise ValueError("Guest confirmation must not promise line-jumping")
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
                      + (f"\n✅ Added to the {b['venue']} guest list on urvenue ({b.get('list_item') or 'General List'}, "
                         f"owner Thomas Vale) — Ref {b['order_id']}" if is_urvenue(b)
                         else f"\n✅ Signed up — Order ID {b['order_id']}")
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
        *(["", "LIV night terms MISSING (sent without M/F/cutoff; add them to the LIV rules file): "
            + "; ".join(f"{v} {n}" for v, n in liv_missing(bookings))] if liv_missing(bookings) else []),
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
                .replace("Follow the steps to grab your passes before you head out.", "Arrive before 3 AM."))
    return page


def drais_guest_email(request, bookings):
    first = request.get("first_name") or "there"
    nights = [(html_emails._short_date(b["date"]), DRAIS_VENUE, None)
              for b in sorted(bookings, key=lambda b: b["date"])]
    text = (f"Hi {first},\n\nYou're on the list for:\n" + "\n".join(f"{d} - {v}" for d, v, _ in nights)
            + "\n\n" + CAPACITY_LINE
            + "\n\n" + DRAIS_TEXT + "\n\nTickets & VIP Tables: https://speakeasygo.com/ms/playmaker"
            "\n\nQuestions? Email info@playmakerentertainment.com\n\nPlaymaker Entertainment")
    html = _drais_html(first, nights, html_emails.party_size(request["female_count"], request["male_count"]))
    for body in (html, text):
        low = body.lower()
        if LINE_PRIORITY_RE.search(body) or "discount" in low:
            raise ValueError("Drai's email must not promise line-jumping or discounts")
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



# --- Guest "on hold" status email (Thomas, Nov 21 2026) ----------------------
# When a request (or some of its nights) is waiting for a guest list that is
# not open yet, needs an access code, or is closed with no open fallback, the
# guest hears about it once: ONE email per trip/ref covering every held night,
# to the guest with team@ on BCC. A new hold email goes out only when the hold
# materially changes (a new night/venue/status on hold), never per hourly retry.

HOLD_KINDS = ("unposted", "access_code", "closed")
HOLD_STATUS = {
    "unposted": "Guest list isn't open yet",
    "access_code": "This list needs an access code right now",
    "closed": "Guest list isn't open yet",
}
HOLD_SUBJECT_STATUS = {
    "unposted": "list not open yet",
    "access_code": "access code needed",
    "closed": "list not open yet",
}
GUESTLISTGO_FROM = "confirmations@guestlistgo.me"


def is_nye(iso):
    d = date.fromisoformat(iso)
    return d.month == 12 and d.day == 31


def nye_ticket_url():
    """Promoter-tracked TAO link (utm_source=promoter&utm_id=...)."""
    import ticketsauce
    return ticketsauce.promoter_link(rules.TAO_PROMOTER_URL)


def nye_note(nights):
    """Guest line for a New Year's Eve night still waiting (OMNIA has no NYE
    guest list; NYE lists open in late December)."""
    if not any(is_nye(n) for n in nights or ()):
        return ""
    url = nye_ticket_url()
    return ("Heads up for New Year's Eve: OMNIA doesn't have a guest list on NYE, and New Year's Eve "
            "guest lists usually don't open until late December. If you'd rather lock in your NYE plans "
            "now, tickets are here: " + url)


def hold_brand(raw):
    raw = raw or {}
    if raw.get("source") == "guestlistgo" or (raw.get("promoter") or "").replace(" ", "").lower() == "guestlistgo" \
            or (raw.get("brand") or "").lower() == "guestlistgo":
        return "guestlistgo"
    return "playmaker"


def hold_signature(held):
    return sorted({f"{v}|{n}|{k}" for v, n, k in held})


def _hold_short(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def hold_email(ref, raw, request, held, update=False, booked=False):
    """held: [(venue, iso_night, kind)] with kind in HOLD_KINDS."""
    held = sorted({(v, n, k if k in HOLD_KINDS else "unposted") for v, n, k in held}, key=lambda x: (x[1], x[0]))
    brand = hold_brand(raw)
    name = "GuestlistGo" if brand == "guestlistgo" else "Playmaker Entertainment"
    contact = ("Questions? Just reply to this email." if brand == "guestlistgo"
               else "Questions? Email info@playmakerentertainment.com")
    first = request.get("first_name") or "there"
    label = date_label(request["start_date"], request["end_date"])
    kinds = {k for _, _, k in held}
    many = len({n for _, n, _ in held}) > 1
    if kinds == {"access_code"}:
        why = ("the guest lists for those dates need an access code right now" if many
               else "the guest list for that date needs an access code right now")
    elif "access_code" in kinds:
        why = "some of those guest lists aren't open yet or need an access code right now"
    else:
        why = ("the guest lists for those dates aren't open yet" if many
               else "the guest list for that date isn't open yet")
    intro = (f"Thanks for your guest list request for {label}. We received it, but {why}, "
             "so your request is on hold for now.")
    if update:
        intro = f"Quick update on your guest list request for {label}: {why}, so your request is on hold for now."
    lines = [f"{_long(n)} - {v}: {HOLD_STATUS[k]}" for v, n, k in held]
    paras = [intro]
    paras.append("We'll sign you up as soon as the list opens and send you a confirmation email. "
                 "There's nothing else you need to do right now.")
    if booked:
        paras.append("Any nights that already went through stay active, and your confirmation email will list everything.")
        paras.append(CAPACITY_LINE)
    nye = nye_note([n for _, n, _ in held])
    if nye:
        paras.append(nye)
    text = "\n\n".join([
        f"Hi {first},",
        intro,
        "On hold:\n" + "\n".join(lines),
        *paras[1:],
        contact,
        name,
    ])
    rows = [(_hold_short(n), v, HOLD_STATUS[k]) for v, n, k in held]
    html = html_emails.render_hold_status(first, rows, paras, brand=brand,
                                          ticket_url=nye_ticket_url() if nye else None)
    subject = f"We received your guest list request for {label} - on hold for now"
    if update:
        nights = " + ".join(dict.fromkeys(_hold_short(n) for _, n, _ in held))
        status = " / ".join(dict.fromkeys(HOLD_SUBJECT_STATUS[k] for _, _, k in held))
        subject = f"Updated: guest list request for {label} - {nights} on hold ({status})"
    subject = subject.replace(",", "").replace(";", "")
    for body in (text, html):
        low = body.lower()
        if LINE_PRIORITY_RE.search(body) or "jarvis" in low:
            raise ValueError("Hold email must not promise line-jumping or mention Jarvis")
        if brand == "guestlistgo" and "playmaker" in low:
            raise ValueError("GuestlistGo hold email must not mention Playmaker")
    payload = {"to": request["email"], "subject": subject, "text": text, "html": html,
               "dedupe_query": f'to:{request["email"]} subject:"{subject}"'}
    if request["email"].strip().lower() != TEAM_TO.lower():
        payload["bcc"] = TEAM_TO
    if brand == "guestlistgo":
        payload["from"] = GUESTLISTGO_FROM
    return payload

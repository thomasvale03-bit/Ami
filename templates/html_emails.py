"""Branded HTML versions of guest emails (templates/email/, owner-approved
Oct 2026). Sent as the text/html part alongside the plain-text body, which
stays the fallback (and is what recent_confirmations parses)."""
import html as _h
import os
from datetime import date

from templates.emails import CAPACITY_LINE

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email")
VIP_MIN_PARTY = 1  # VIP block on every confirmation (approved 2026-10-11)


def _read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        s = fh.read()
    if name.startswith("partials/") and s.lstrip().startswith("<!--"):
        s = s.split("-->", 1)[1].lstrip("\n")  # strip the usage comment
    return s


def _fill(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("{{%s}}" % k, v)
    return tpl


def capacity_note_html():
    """Short capacity line for confirmation HTML (automatic and hand-sent wraps)."""
    return (
        '<p style="margin:12px 0 0 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;'
        'font-size:14px;line-height:22px;color:#BDBDBD;">'
        f"{_h.escape(CAPACITY_LINE)}</p>"
    )


def _short_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a, %b')} {d.day}"


def nights_block(nights):
    """nights: [(date_label, venue, headliner_or_None)]."""
    row = _read("partials/night_row.html")
    return "".join(_fill(row, date=_h.escape(d), venue=_h.escape(v), headliner=_h.escape(hl or ""))
                   for d, v, hl in nights)


SCOTTSDALE_FOLLOW_UP_SWAPS = (
    ("Hope you had an amazing time in Vegas! Remember us for your next trip.",
     "Hope you had an amazing time at {venue}! Remember us for your next night out."),
    ("we hope you enjoyed the night and that Vegas treated you right.",
     "we hope you enjoyed your night at {venue} in Scottsdale."),
    ("Remember us for your next Vegas trip. We'd love to have you back.",
     "Remember us for your next night out. We'd love to have you back."),
    ("Know someone heading to Vegas?",
     "Know someone who loves a night out?"),
    ("If you know anyone coming to Vegas, we'd love to get referred!",
     "If you know anyone looking for a great night out, we'd love to get referred!"),
    ("Free guest list at the top Las Vegas clubs and dayclubs",
     "Free guest list at top clubs in Scottsdale and Las Vegas"),
)


def render_follow_up(first_name, optin_url="https://playmakerentertainment.com",
                     unsubscribe_url="https://playmakerentertainment.com", venues=()):
    from config import rules
    out = _fill(_read("follow_up.html"), first_name=_h.escape(first_name or "there"),
                optin_url=_h.escape(optin_url, quote=True),
                unsubscribe_url=_h.escape(unsubscribe_url, quote=True))
    scottsdale = rules.scottsdale_venue(venues)
    if scottsdale:
        v = _h.escape(scottsdale)
        for old, new in SCOTTSDALE_FOLLOW_UP_SWAPS:
            out = out.replace(old, new.format(venue=v))
    return out


FALLBACK_NOTICE = ("Oh No, You Did Not Meet this Venue's GuestList Cut off. "
                   "But we signed you up for another amazing venue!")


def fallback_note_html(fallback_nights=None):
    """fallback_nights: True, or a list of night labels (e.g. ["Sat, Oct 17"]).
    Falsy => "" (no change for guests whose nights all matched)."""
    if not fallback_nights:
        return ""
    txt = ""
    if isinstance(fallback_nights, (list, tuple)) and fallback_nights:
        txt = " (" + ", ".join(_h.escape(str(n)) for n in fallback_nights) + ")"
    return _fill(_read("partials/fallback_note.html"), fallback_nights_text=txt)


def fallback_note_text(fallback_nights=None):
    """Plain-text version of the fallback notice ("" when not needed)."""
    if not fallback_nights:
        return ""
    if isinstance(fallback_nights, (list, tuple)) and fallback_nights:
        return FALLBACK_NOTICE + " (" + ", ".join(str(n) for n in fallback_nights) + ")"
    return FALLBACK_NOTICE


def render_confirmation(first_name, nights, party_size=1, closed_note=None, fallback_nights=None):
    note = (_fill(_read("partials/closed_note.html"), closed_note_text=_h.escape(closed_note))
            if closed_note else "")
    return _fill(_read("guest_confirmation.html"),
                 fallback_note=fallback_note_html(fallback_nights),
                 first_name=_h.escape(first_name or "there"),
                 nights_block=nights_block(nights),
                 closed_note=note,
                 vip_block=_read("partials/vip_block.html") if party_size >= VIP_MIN_PARTY else "")


def party_size(female_count, male_count):
    try:
        return int(female_count or 0) + int(male_count or 0)
    except (TypeError, ValueError):
        return 0


def concierge_confirmation_html(first_name, nights, size, drais=None):
    """HTML for emails.guest_confirmation. None when the HTML can't carry
    everything the text does (Drai's door list), so plain text goes alone."""
    if drais or not nights:
        return None
    rows, notes = [], []
    for n in nights:
        venue = n["venue"] + (f" + {n['dayclub']} (daytime)" if n.get("dayclub") else "")
        rows.append((_short_date(n["date"]), venue, n.get("headliner")))
        if n.get("rerouted_from"):
            notes.append(f"{n['rerouted_from']}'s guest list was already closed for same-day "
                         f"sign-ups, so we moved you to {n['venue']}.")
    fb = [_short_date(n["date"]) for n in nights if n.get("fallback")]
    return render_confirmation(first_name, rows, size, " ".join(notes) or None, fallback_nights=fb or None)


def consolidated_confirmation_html(first_name, registrations, drais=None):
    """HTML for emails.consolidated_confirmation (None with Drai's, as above)."""
    if drais or not registrations:
        return None
    first = registrations[0]
    rows = [(_short_date(r["date"]), r["venue"], r.get("event")) for r in registrations]
    fb = list(dict.fromkeys(_short_date(r["date"]) for r in registrations if r.get("fallback")))
    return render_confirmation(first_name, rows, party_size(first.get("female_count"), first.get("male_count")),
                               fallback_nights=fb or None)


def _af_night(date_label):
    return "Saturday" if (date_label or "").lower().startswith("sat") else "Friday"


def render_almost_famous(first_name, date_label):
    return _fill(_read("almost_famous.html"),
                 first_name=_h.escape(first_name or "there"),
                 date_label=_h.escape(date_label),
                 night=_af_night(date_label),
                 door_banner=_DOOR_SHOW_BANNER)


def render_almost_famous_text(first_name, date_label):
    fn = first_name or "there"
    night = _af_night(date_label)
    night_up = night.upper()
    return f"""YOU'RE ON THE LIST!
ALMOST FAMOUS {night_up}S
Your {night} Night. Your Crew. Your VIP Energy.

{DOOR_SHOW_LINE.upper()}
{DOOR_SHOW_DISCLAIMER}

Hey {fn}, get ready for a night of electrifying EDM, nonstop dancing, and unforgettable memories in the heart of Old Town Scottsdale! Your guest list registration with Playmaker Entertainment has been received.

YOUR NIGHT
Date:       {date_label}
Venue:      Almost Famous
Location:   7419 E Indian Plaza, Scottsdale, AZ 85251
            https://www.google.com/maps/search/?api=1&query=7419+E+Indian+Plaza%2C+Scottsdale%2C+AZ+85251
Doors open: 10:15 PM
Ladies:     FREE before 12:00 AM
Gentlemen:  FREE before 12:00 AM

All guest list spots are subject to capacity.

HOW TO GET IN
1) Let them know you're on THOMAS VALE'S GUEST LIST with PLAYMAKER ENTERTAINMENT.
2) Check in, head inside, and enjoy your night.

ARRIVE EARLY!
- Free guest list admission ends at midnight.
- Entry is subject to venue capacity and door policies.
- 21+ ONLY. VALID GOVERNMENT-ISSUED ID REQUIRED.

TAG US & GET FEATURED
Post your photos and videos and tag @playmaker.entertainment for a chance to be featured.
Follow @playmaker.entertainment: https://instagram.com/playmaker.entertainment

BIG BEATS. LATE NIGHTS. OLD TOWN ENERGY.
Come Early. Stay Late. Make It Almost Famous.
SEE YOU ON THE DANCE FLOOR!

--
Playmaker Entertainment
Your Nightlife. Your Experience.
www.playmakerentertainment.com
Questions? Email info@playmakerentertainment.com
Instagram: @playmaker.entertainment
Must be 21+ with valid ID. Entry not guaranteed; all admission at venue discretion.
"""



DOOR_SHOW_LINE = "Please show this email at the door for free entry before midnight."
DOOR_SHOW_DISCLAIMER = "Does not guarantee expedited check-in."

_DOOR_SHOW_BANNER = f"""    <!-- Show this email at the door -->
    <tr><td class="px" style="padding:18px 40px 8px 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#D4AF37" style="width:100%;background-color:#D4AF37;background-image:linear-gradient(#D4AF37,#D4AF37);border-radius:10px;">
        <tr><td align="center" style="padding:18px 20px 6px 20px;font-family:'Inter',Helvetica,Arial,sans-serif;font-size:20px;line-height:26px;color:#0d0d0d;font-weight:800;letter-spacing:0.2px;text-align:center;">
          {DOOR_SHOW_LINE}
        </td></tr>
        <tr><td align="center" style="padding:0 20px 16px 20px;font-family:'Inter',Helvetica,Arial,sans-serif;font-size:13px;line-height:18px;color:#3a2a08;font-weight:500;text-align:center;">
          {DOOR_SHOW_DISCLAIMER}
        </td></tr>
      </table>
    </td></tr>

"""

RIOT_HOUSE_EVENT = "Riot House | Guest List"
RIOT_HOUSE_HOURS = "Doors open 9:15 PM"

_RIOT_CHECKIN = """    <!-- Riot House check-in -->
    <tr><td bgcolor="#0d0d0d" class="px bg-card" style="background-color:#0d0d0d;background-image:linear-gradient(#0d0d0d,#0d0d0d);padding:30px 40px 0 40px;">
      <h2 style="margin:0 0 6px 0;font-family:'Playfair Display',Georgia,serif;font-size:24px;line-height:30px;color:#F5F5F5;font-weight:700;">How to get in</h2>
      <table role="presentation" width="60" cellpadding="0" cellspacing="0" border="0"><tr><td height="2" bgcolor="#D4AF37" style="height:2px;line-height:2px;font-size:0;">&nbsp;</td></tr></table>
      <p style="margin:16px 0 0 0;font-family:'Inter',Helvetica,Arial,sans-serif;font-size:15px;line-height:23px;color:#E6E6E6;">No app or pass needed. Just <strong style="color:#FFFFFF;">check in at the door under your name</strong>, you're on the <strong style="color:#D4AF37;">Playmaker Entertainment</strong> guest list.</p>
    </td></tr>

"""

_RIOT_DETAILS = """    <!-- Riot House guestlist details -->
    <tr><td bgcolor="#0d0d0d" class="px bg-card" style="background-color:#0d0d0d;background-image:linear-gradient(#0d0d0d,#0d0d0d);padding:26px 40px 0 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#111111" style="border:3px solid #D4AF37;border-radius:10px;background-color:#111111;background-image:linear-gradient(#111111,#111111);">
        <tr><td align="center" style="padding:24px 22px;font-family:'Inter',Helvetica,Arial,sans-serif;color:#F5F5F5;font-size:15px;line-height:24px;">
          <p style="margin:0 0 2px 0;font-size:14px;letter-spacing:3px;color:#D4AF37;font-weight:800;">THOMAS VALE&rsquo;S</p>
          <p style="margin:0;font-family:'Playfair Display',Georgia,serif;font-size:34px;line-height:40px;color:#D4AF37;font-weight:800;letter-spacing:2px;">GUESTLIST</p>
          <p style="margin:2px 0 18px 0;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:#BDBDBD;font-weight:600;">Playmaker Entertainment</p>
          <p style="margin:0;font-family:'Playfair Display',Georgia,serif;font-size:26px;line-height:32px;color:#FFFFFF;font-weight:700;letter-spacing:2px;">RIOT HOUSE</p>
          <p style="margin:0 0 16px 0;font-size:14px;color:#A8A8A8;">Scottsdale, AZ</p>
          <p style="margin:0;"><strong style="color:#D4AF37;">Open at 9:15 pm</strong></p>
          <p style="margin:0;">Ladies Free: <strong>until 12am</strong></p>
          <p style="margin:0 0 16px 0;">Males Free: <strong>until 12am</strong></p>
          <p style="margin:0;">&#128248; Tag US on IG so We can repost your story</p>
          <p style="margin:0;"><a href="https://www.instagram.com/playmaker.entertainment/" target="_blank" style="color:#D4AF37;text-decoration:none;font-weight:700;">@playmaker.entertainment</a></p>
        </td></tr>
      </table>
    </td></tr>

"""

RIOT_DETAILS_TEXT = """Thomas Vale's GUESTLIST
Playmaker Entertainment

RIOT HOUSE
Scottsdale, AZ

Open at 9:15 pm
Ladies Free: until 12am
Males Free: until 12am

Tag US on IG so We can repost your story
@playmaker.entertainment: https://www.instagram.com/playmaker.entertainment/"""


def _riot_nights(nights):
    return [((n if isinstance(n, str) else n[0]), "Riot House", f"{RIOT_HOUSE_EVENT} \u00b7 {RIOT_HOUSE_HOURS}") for n in nights]


def render_riot_house(first_name, nights):
    """Riot House (Scottsdale) guest list confirmation: door check-in, no TAO app/pass steps, no VIP block."""
    html = render_confirmation(first_name or "there", _riot_nights(nights))
    a = html.index("<!-- How to get passes -->"); b = html.index("<!-- Before you go -->")
    html = html[:a] + _RIOT_CHECKIN + html[b:]
    i = html.index("<!-- Nights -->")
    html = html[:i] + _DOOR_SHOW_BANNER + _RIOT_DETAILS + "    " + html[i:]
    a = html.index("<!-- Optional VIP upsell (parties of 4+) -->"); b = html.index("<!-- Sign-off -->")
    html = html[:a] + html[b:]
    html = html.replace("Here's how to get your passes for the night.", "Just check in at the door under your name.")
    html = html.replace("Follow the steps to grab your passes before you head out.", "Just check in at the door under your name.")
    html = html.replace("each venue's rules", "the venue's rules")
    return html


def render_riot_house_text(first_name, nights):
    fn = first_name or "there"
    lines = [f"{d} - {v}: {hl}" for d, v, hl in _riot_nights(nights)]
    return (f"Hi {fn},\n\n{DOOR_SHOW_LINE.upper()}\n{DOOR_SHOW_DISCLAIMER}\n\n"
            "You're on the Playmaker Entertainment guest list for:\n" + "\n".join(lines) +
            "\n\n" + CAPACITY_LINE + "\n\n" + RIOT_DETAILS_TEXT + "\n\nHow to get in: no app or pass needed. Just check in at the door under your name.\n\n"
            "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission is subject to "
            "the venue's rules, dress code, and capacity.\n\nQuestions? Email info@playmakerentertainment.com\n\nPlaymaker Entertainment")


def render_scottsdale_venue(first_name, venue_name, nights):
    """Generic Scottsdale venue confirmation (El Hefe, Cake Nightclub, Maya Dayclub)."""
    v_esc = _h.escape(venue_name.upper())
    addr = "4425 N Saddlebag Trail #101 &middot; Scottsdale, AZ 85251" if "el hefe" in venue_name.lower() else "Old Town Scottsdale, AZ"
    details = f"""    <!-- Scottsdale guestlist details -->
    <tr><td bgcolor="#0d0d0d" class="px bg-card" style="background-color:#0d0d0d;background-image:linear-gradient(#0d0d0d,#0d0d0d);padding:26px 40px 0 40px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#111111" style="border:3px solid #D4AF37;border-radius:10px;background-color:#111111;background-image:linear-gradient(#111111,#111111);">
        <tr><td align="center" style="padding:24px 22px;font-family:'Inter',Helvetica,Arial,sans-serif;color:#F5F5F5;font-size:15px;line-height:24px;">
          <p style="margin:0 0 2px 0;font-size:14px;letter-spacing:3px;color:#D4AF37;font-weight:800;">THOMAS VALE&rsquo;S</p>
          <p style="margin:0;font-family:'Playfair Display',Georgia,serif;font-size:34px;line-height:40px;color:#D4AF37;font-weight:800;letter-spacing:2px;">GUESTLIST</p>
          <p style="margin:2px 0 18px 0;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:#BDBDBD;font-weight:600;">Playmaker Entertainment</p>
          <p style="margin:0;font-family:'Playfair Display',Georgia,serif;font-size:26px;line-height:32px;color:#FFFFFF;font-weight:700;letter-spacing:2px;">{v_esc}</p>
          <p style="margin:0 0 16px 0;font-size:14px;color:#A8A8A8;">{addr}</p>
          <p style="margin:0;">Ladies Free: <strong>before 12am</strong></p>
          <p style="margin:12px 0 0 0;">&#128248; Tag US on IG so We can repost your story</p>
          <p style="margin:0;"><a href="https://www.instagram.com/playmaker.entertainment/" target="_blank" style="color:#D4AF37;text-decoration:none;font-weight:700;">@playmaker.entertainment</a></p>
        </td></tr>
      </table>
    </td></tr>

"""
    rows = [((n if isinstance(n, str) else n[0]), venue_name, f"{venue_name} | Guest List \u00b7 Old Town Scottsdale") for n in nights]
    html = render_confirmation(first_name or "there", rows)
    a = html.index("<!-- How to get passes -->"); b = html.index("<!-- Before you go -->")
    html = html[:a] + _RIOT_CHECKIN + html[b:]
    i = html.index("<!-- Nights -->")
    html = html[:i] + details + "    " + html[i:]
    a = html.index("<!-- Optional VIP upsell (parties of 4+) -->"); b = html.index("<!-- Sign-off -->")
    html = html[:a] + html[b:]
    html = html.replace("Here's how to get your passes for the night.", "Just check in at the door under your name.")
    html = html.replace("Follow the steps to grab your passes before you head out.", "Just check in at the door under your name.")
    html = html.replace("each venue's rules", "the venue's rules")
    return html


def render_scottsdale_venue_text(first_name, venue_name, nights):
    fn = first_name or "there"
    addr = "4425 N Saddlebag Trail #101, Scottsdale, AZ 85251" if "el hefe" in venue_name.lower() else "Old Town Scottsdale, AZ"
    rows = [f"{n if isinstance(n, str) else n[0]} - {venue_name}: {venue_name} | Guest List \u00b7 Old Town Scottsdale" for n in nights]
    return (f"Hi {fn},\n\nYou're on the Playmaker Entertainment guest list for:\n" + "\n".join(rows) +
            "\n\n" + CAPACITY_LINE +
            f"\n\nThomas Vale's GUESTLIST\nPlaymaker Entertainment\n\n{venue_name.upper()}\n{addr}\n\nLadies Free: before 12am\n\n"
            "Tag US on IG so We can repost your story\n@playmaker.entertainment: https://www.instagram.com/playmaker.entertainment/\n\n"
            "How to get in: no app or pass needed. Just check in at the door under your name.\n\n"
            "Arrive early and bring a valid, government-issued 21+ photo ID. Guest-list admission is subject to "
            "the venue's rules, dress code, and capacity.\n\nQuestions? Email info@playmakerentertainment.com\n\nPlaymaker Entertainment")


def scottsdale_confirmation_email(first_name, venue_name, dates_iso):
    """Return (subject, text, html) for a Scottsdale confirmation.
    Subjects never contain commas or semicolons so /send-branded accepts them."""
    date_objs = [date.fromisoformat(d) for d in dates_iso]
    if len(date_objs) == 1:
        subj_dates = f"{date_objs[0].strftime('%a %b')} {date_objs[0].day}"
    else:
        subj_dates = f"{date_objs[0].strftime('%a %b')} {date_objs[0].day} to {date_objs[-1].strftime('%a %b')} {date_objs[-1].day}"
    short_labels = [f"{d.strftime('%a, %b')} {d.day}" for d in date_objs]
    vl = (venue_name or "").lower()
    if "almost famous" in vl:
        long_label = f"{date_objs[0].strftime('%A, %B')} {date_objs[0].day}"
        subj = f"You're on the list: Almost Famous \u2014 {subj_dates} \u2014 Playmaker Entertainment"
        return subj, render_almost_famous_text(first_name, long_label), render_almost_famous(first_name, long_label)
    if "riot house" in vl:
        subj = f"You're on the list: Riot House \u2014 {subj_dates} \u2014 Playmaker Entertainment"
        return subj, render_riot_house_text(first_name, short_labels), render_riot_house(first_name, short_labels)
    canon = "El Hefe" if "el hefe" in vl else ("Cake Nightclub" if "cake" in vl else ("Maya Dayclub" if "maya" in vl else venue_name))
    subj = f"You're on the list: {canon} \u2014 {subj_dates} \u2014 Playmaker Entertainment"
    return subj, render_scottsdale_venue_text(first_name, canon, short_labels), render_scottsdale_venue(first_name, canon, short_labels)


PROMOTER_SLACK_INVITE = (
    "https://join.slack.com/t/playmakerente-frr3522/shared_invite/"
    "zt-4hfdayaac-JRguw6Cuh0S9kpPd06EA4w"
)
PROMOTER_WELCOME_SUBJECT = "Welcome to Playmaker Entertainment – Next Steps"
PROMOTER_WELCOME_PARAS = [
    "We’re so excited that you want to work with Playmaker Entertainment!",
    "As our next step, please join our official Playmaker Slack workspace using the invitation link below:",
    PROMOTER_SLACK_INVITE,
    "Once you join, we’ll add you to our #newhire channel, where you’ll receive onboarding information, updates, and instructions on getting started with the Playmaker team.",
    "Welcome to Playmaker! We’re looking forward to working with you.",
]


def promoter_welcome_text():
    """Exact plain-text wording for new promoter applications."""
    return "\n\n".join(PROMOTER_WELCOME_PARAS)


def render_promoter_welcome():
    """Same PME chrome as guest confirmations (header, logo, colors, footer)."""
    return _read("promoter_welcome.html")


# --- Guest "on hold" status (list not open yet / access code) ---------------

HOLD_BRANDS = {
    "playmaker": {
        "name": "Playmaker Entertainment",
        "logo": ('    <tr><td align="center" class="px" style="padding:34px 40px 10px 40px;">\n'
                 '      <a href="https://playmakerentertainment.com" target="_blank" style="text-decoration:none;">\n'
                 '        <img class="logo" src="https://playmakerentertainment.com/brand/logo-email.jpg" width="260" '
                 'alt="Playmaker Entertainment" style="display:block;width:260px;max-width:100%;height:auto;border:0;'
                 'color:#D4AF37;font-family:Georgia,serif;font-size:22px;font-weight:bold;letter-spacing:2px;">\n'
                 '      </a>\n    </td></tr>'),
        "footer": ('Questions? Email <a href="mailto:info@playmakerentertainment.com" style="color:#C9A84C;'
                   'text-decoration:none;">info@playmakerentertainment.com</a><br>\n'
                   '      <a href="https://playmakerentertainment.com" target="_blank" style="color:#C9A84C;'
                   'text-decoration:none;">playmakerentertainment.com</a>'),
    },
    "guestlistgo": {
        "name": "GuestlistGo",
        "logo": ('    <tr><td align="center" class="px" style="padding:34px 40px 10px 40px;">\n'
                 '      <a href="https://guestlistgo.me" target="_blank" style="text-decoration:none;'
                 'font-family:Georgia,serif;font-size:28px;font-weight:bold;letter-spacing:2px;color:#D4AF37;">'
                 'GuestlistGo</a>\n    </td></tr>'),
        "footer": ('Questions? Just reply to this email.<br>\n'
                   '      <a href="https://guestlistgo.me" target="_blank" style="color:#C9A84C;'
                   'text-decoration:none;">guestlistgo.me</a>'),
    },
}


def _hold_para(text):
    return ('<p style="margin:18px 0 0 0;font-family:\'Inter\',Helvetica,Arial,sans-serif;font-size:16px;'
            f'line-height:24px;color:#BDBDBD;">{text}</p>')


def render_hold_status(first_name, rows, paragraphs, brand="playmaker", ticket_url=None, headline=None,
                       ticket_label="NYE tickets"):
    """Branded "your request is on hold" email. rows: [(date_label, venue,
    status)], paragraphs: plain-text paragraphs (escaped here)."""
    b = HOLD_BRANDS.get(brand) or HOLD_BRANDS["playmaker"]
    items = "".join(
        '<tr><td style="padding:10px 0;border-bottom:1px solid #241f12;font-family:\'Inter\',Helvetica,Arial,'
        'sans-serif;font-size:15px;line-height:22px;color:#F5F5F5;">'
        f'<strong>{_h.escape(d)}</strong> &middot; {_h.escape(v)}<br>'
        f'<span style="color:#D4AF37;font-size:13px;">{_h.escape(s)}</span></td></tr>'
        for d, v, s in rows)
    body = [_hold_para(f"Hi {_h.escape(first_name or 'there')},")]
    body.append(_hold_para(_h.escape(paragraphs[0])))
    body.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="margin:14px 0 0 0;">{items}</table>')
    for p in paragraphs[1:]:
        html = _h.escape(p)
        if ticket_url and ticket_url in p:
            esc = _h.escape(ticket_url)
            label = _h.escape(ticket_label or "tickets")
            html = html.replace(esc, f'<a href="{esc}" target="_blank" style="color:#D4AF37;'
                                     f'text-decoration:underline;word-break:break-all;">{label}</a>')
        body.append(_hold_para(html))
    page = _read("hold_status.html")
    return _fill(page, TITLE=_h.escape(headline or "Your guest list request is on hold"),
                 PREHEADER=_h.escape(paragraphs[0]), LOGO=b["logo"], EYEBROW="Request received",
                 HEADLINE=_h.escape(headline or "On hold for now"), BODY="\n".join(body),
                 SIGNOFF=_h.escape(b["name"]), FOOTER=b["footer"])

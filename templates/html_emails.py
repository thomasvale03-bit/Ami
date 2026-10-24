"""Branded HTML versions of guest emails (templates/email/, owner-approved
Oct 2026). Sent as the text/html part alongside the plain-text body, which
stays the fallback (and is what recent_confirmations parses)."""
import html as _h
import os
from datetime import date

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

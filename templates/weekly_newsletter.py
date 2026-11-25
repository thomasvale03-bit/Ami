"""HTML "This Week in Vegas" newsletter (sent Wednesdays, covers the official
Tue-Mon week that includes the send day).

Layout (approved by Thomas, Oct 2026): compact header + one line of copy, the two
TAO "Weekly Events" lineup flyers (nightclub + dayclub, ALWAYS included), three
"Don't Miss" picks with their flyer (or a text card) and a guest-list button, a
compact "Also This Week" list, short footer. Images ride as CID inline parts,
and every flyer for the Tue-Mon week also rides as a regular attachment.

Rules: no phone numbers, never "See you in Vegas", contact is
info@playmakerentertainment.com.
"""
import html
import json
import os
import re
import shutil
from datetime import date, timedelta

CONTACT = "info@playmakerentertainment.com"
SIGNUP = "https://playmakerentertainment.com"
LOGO = "https://playmakerentertainment.com/brand/logo-email.jpg"
LINEUP_FILES = (("weekly_nightclub.jpg", "lineup-nightclub", "TAO Group nightclub lineup"),
                ("weekly_dayclub.jpg", "lineup-dayclub", "TAO Group dayclub lineup"))
VENUE_CODES = {"OMNIA Nightclub": "OMNC", "Hakkasan Nightclub": "HKLV", "JEWEL Nightclub": "JWL",
               "Marquee Nightclub": "MNC", "TAO Nightclub": "TNC", "OMNIA Dayclub": "OMDC",
               "Marquee Dayclub": "MDC", "Liquid Pool Lounge": "LQD", "Palm Tree Beach Club": "PTBC",
               "LIV": "LIV", "LIV Nightclub": "LIV", "XS": "XS", "XS Nightclub": "XS"}
NIGHTCLUBS = ("OMNIA Nightclub", "Hakkasan Nightclub", "JEWEL Nightclub", "Marquee Nightclub", "TAO Nightclub")
# Default ranking when data/top_picks.json isn't updated for the week: bigger
# draws first. Unknown acts rank after these, by venue order in NIGHTCLUBS.
STAR_RANK = ["zedd", "steve aoki", "tiesto", "tiësto", "calvin harris", "david guetta", "dj snake", "alesso",
             "martin garrix", "illenium", "marshmello", "kaskade", "deorro", "chainsmokers", "the chainsmokers",
             "moneybagg yo", "tyga", "ohgeesy", "chase b", "dj pauly d", "lil jon", "diplo", "twinsick", "mk"]


class MissingLineup(Exception):
    """A TAO weekly lineup flyer is missing; the newsletter must not go out."""


WEEK_START_WEEKDAY = 1  # Tuesday (Mon=0). Thomas's official week is Tue-Mon.


def week_window(today):
    """(tuesday, monday) the newsletter covers: the Tue on/before today."""
    tue = today - timedelta(days=(today.weekday() - WEEK_START_WEEKDAY) % 7)
    return tue, tue + timedelta(days=6)


def _date_order(picks):
    """Picks in calendar order (stable: same-night picks keep their order)."""
    return sorted(picks, key=lambda p: p["date"])


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _rank(artist):
    a = (artist or "").lower().strip()
    return STAR_RANK.index(a) if a in STAR_RANK else len(STAR_RANK)


# JEWEL closed after Saturday, Nov 21, 2026. Weeks that start after that
# date must not feature it; earlier weeks may still include it if it ranks.
JEWEL_CLOSED_AFTER = date(2026, 11, 21)
CAPACITY_NOTE = "All subject to capacity."
# Customer-facing copy may state venue terms (comp, open bar, list times) but
# must never promise entry.
_PROMISE_ENTRY = re.compile(
    r"\b(guaranteed entry|you're in|you are in|skip the line|skip-the-line|"
    r"promised entry|lock it in|to get in|we(?:'ll| will) get you in)\b",
    re.I,
)


def default_picks(start, end, headliners_map):
    """Biggest nightclub acts across venues, 3 total. JEWEL is not required,
    and is dropped once the week starts after it closed (Nov 21, 2026).
    headliners_map: {iso: {venue: artist}}."""
    cands = []
    d = start
    while d <= end:
        for venue, artist in (headliners_map.get(d.isoformat()) or {}).items():
            if venue in NIGHTCLUBS and artist:
                cands.append({"date": d.isoformat(), "venue": venue, "artist": artist})
        d += timedelta(days=1)
    if start > JEWEL_CLOSED_AFTER:
        cands = [p for p in cands if p["venue"] != "JEWEL Nightclub"]
    key = lambda p: (_rank(p["artist"]), NIGHTCLUBS.index(p["venue"]), p["date"])
    return sorted(cands, key=key)[:3]


def load_picks(data_dir, start, end, headliners_map):
    try:
        with open(os.path.join(data_dir, "top_picks.json")) as fh:
            cfg = json.load(fh)
        if cfg.get("week_start") == start.isoformat() and cfg.get("picks"):
            return _date_order(cfg["picks"][:3])
    except (OSError, ValueError):
        pass
    return _date_order(default_picks(start, end, headliners_map))


def load_also(data_dir, start, end, headliners_map, picks):
    """[(date, [(venue, artist), ...])] minus the picks. Uses
    data/also_this_week.json for the week if present, else headliners."""
    nights = None
    try:
        with open(os.path.join(data_dir, "also_this_week.json")) as fh:
            cfg = json.load(fh)
        if cfg.get("week_start") == start.isoformat():
            nights = [(n["date"], [tuple(e) for e in n["events"]]) for n in cfg["nights"]]
    except (OSError, ValueError, KeyError):
        nights = None
    if nights is None:
        nights, d = [], start
        while d <= end:
            ev = sorted((headliners_map.get(d.isoformat()) or {}).items())
            if ev:
                nights.append((d.isoformat(), ev))
            d += timedelta(days=1)
    picked = {(p["date"], _norm(p["artist"])) for p in picks}
    out = []
    for iso, ev in nights:
        ev = [(v, a) for v, a in ev if (iso, _norm(a)) not in picked]
        if ev:
            out.append((iso, ev))
    return out


def _clean_rules(text):
    """Keep venue terms; drop any phrase that promises entry."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", raw)
    kept = [s for s in parts if s and not _PROMISE_ENTRY.search(s)]
    return " ".join(kept).strip()


def _resolve_flyer(name, data_dir, flyers_dir):
    if not name:
        return None
    name = str(name).strip()
    candidates = [name]
    if flyers_dir:
        candidates.append(os.path.join(flyers_dir, os.path.basename(name)))
        candidates.append(os.path.join(flyers_dir, name))
    if data_dir:
        candidates.append(os.path.join(data_dir, name))
        candidates.append(os.path.join(data_dir, "flyers", os.path.basename(name)))
    seen = set()
    for path in candidates:
        path = os.path.abspath(path)
        if path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def stage_liv_xs_flyer(src_path, flyers_dir, venue, event_date, event):
    """Copy a LIV/XS flyer into the repo flyer dir with a TAO-style name,
    e.g. 261204_LIV_SOSA.jpg. Returns the destination path."""
    if not src_path or not os.path.isfile(src_path):
        raise FileNotFoundError(src_path)
    d = date.fromisoformat(event_date) if isinstance(event_date, str) else event_date
    code = VENUE_CODES.get(venue, re.sub(r"[^A-Z0-9]", "", (venue or "").upper()) or "LIV")
    act = re.sub(r"[^A-Za-z0-9-]+", "-", (event or "event")).strip("-") or "event"
    os.makedirs(flyers_dir, exist_ok=True)
    dest = os.path.join(flyers_dir, f"{d:%y%m%d}_{code}_{act}.jpg")
    ext = os.path.splitext(src_path)[1].lower()
    if ext in (".jpg", ".jpeg") and os.path.abspath(src_path) != os.path.abspath(dest):
        shutil.copy2(src_path, dest)
        return dest
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            im.convert("RGB").save(dest, "JPEG", quality=90, optimize=True)
        return dest
    except Exception:  # noqa: BLE001
        if os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.copy2(src_path, dest)
        return dest


def load_liv_xs(data_dir, start, end, flyers_dir=None):
    """Optional LIV & XS guest-list section for the current Tue-Mon week.

    data/liv_xs.json format::

        {
          "week_start": "2026-12-01",
          "enabled": true,
          "lineup_flyer": "261201_LIV_weekend-lineup.jpg",
          "events": [
            {
              "venue": "LIV",
              "date": "2026-12-04",
              "weekday": "Fri",
              "event": "SOSA",
              "rules": "Guys and girls comp with open bar, list until 2 AM.",
              "flyer": "261204_LIV_SOSA.jpg"
            }
          ],
          "notes": ["LIV is closed Sun Dec 6."]
        }

    Returns None (render nothing) when the file is missing, empty, enabled is
    false, week_start does not match this week's Tuesday, or week_start is in
    the past. A missing or unreadable flyer is skipped; the section still
    renders. Always carries a 'subject to capacity' footer. Never promises entry.
    """
    path = os.path.join(data_dir, "liv_xs.json")
    try:
        with open(path) as fh:
            raw = fh.read().strip()
        if not raw:
            return None
        cfg = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return None
    week_start = (cfg.get("week_start") or "").strip()
    if not week_start or week_start != start.isoformat():
        return None
    try:
        if date.fromisoformat(week_start) < start:
            return None
    except ValueError:
        return None
    events = []
    for item in cfg.get("events") or []:
        if not isinstance(item, dict):
            continue
        iso = (item.get("date") or "").strip()
        venue = (item.get("venue") or "").strip()
        event = (item.get("event") or item.get("headliner") or "").strip()
        if not (iso and venue and event):
            continue
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        rules = _clean_rules(item.get("rules") or "")
        weekday = (item.get("weekday") or "").strip() or d.strftime("%a")
        flyer_path = _resolve_flyer(item.get("flyer") or item.get("flyer_path") or "", data_dir, flyers_dir)
        events.append({
            "venue": venue,
            "date": iso,
            "weekday": weekday,
            "event": event,
            "rules": rules,
            "flyer_path": flyer_path,
            "cid": None,
        })
    notes = []
    for n in cfg.get("notes") or []:
        raw = n.strip() if isinstance(n, str) else (n.get("text") or "").strip() if isinstance(n, dict) else ""
        cleaned = _clean_rules(raw)
        if cleaned:
            notes.append(cleaned)
    lineup_path = _resolve_flyer(cfg.get("lineup_flyer") or cfg.get("lineup") or "", data_dir, flyers_dir)
    if not events and not notes and not lineup_path:
        return None
    return {
        "week_start": week_start,
        "events": events,
        "notes": notes,
        "lineup_path": lineup_path,
        "lineup_cid": None,
        "capacity_note": CAPACITY_NOTE,
    }


def find_flyer(flyers_dir, pick):
    d = date.fromisoformat(pick["date"])
    code = VENUE_CODES.get(pick["venue"], "")
    art = _norm(pick["artist"])
    try:
        names = sorted(os.listdir(flyers_dir))
    except OSError:
        return None
    for n in names:
        if not n.lower().endswith((".jpg", ".jpeg", ".png")) or not n.startswith(d.strftime("%y%m%d") + "_"):
            continue
        parts = n.split("_")
        if code and parts[1] != code:
            continue
        if art and art in _norm(n):
            return os.path.join(flyers_dir, n)
    return None


VENUE_LABELS = {"OMNC": "OMNIA", "OMDC": "OMNIA-Dayclub", "HKLV": "Hakkasan", "JWL": "JEWEL",
                "MNC": "Marquee", "MDC": "Marquee-Dayclub", "TNC": "TAO", "LQD": "Liquid",
                "PTBC": "Palm-Tree", "LIV": "LIV", "XS": "XS"}
SERIES_TAGS = {"ss", "mon", "dw", "glf", "lowkey"}  # TAO series prefixes, not the act
ACT_FIXES = {"linconjesser": "Lincoln-Jesser", "r-bae-wednesdays": "RnBae-Wednesdays", "zedd": "Zedd",
             "ritualsundays": "Ritual-Sundays"}  # tidy TAO's file-name typos/casing
ATTACH_LIMIT = 18 * 1024 * 1024  # stay well under Gmail's 25MB (base64 adds ~33%)


def _flyer_parse(name):
    """'261016_OMNC_Steve-Aoki_1080x1350.jpg' -> (date, venue code, act) or None."""
    stem, ext = os.path.splitext(name)
    m = re.match(r"^(\d{6})_([A-Z]+)_(.+)$", stem)
    if not m or ext.lower() not in (".jpg", ".jpeg", ".png"):
        return None
    try:
        d = date(2000 + int(m.group(1)[:2]), int(m.group(1)[2:4]), int(m.group(1)[4:]))
    except ValueError:
        return None
    parts = [x for x in m.group(3).split("_") if x and not re.fullmatch(r"\d+x\d+", x)]
    if len(parts) > 1:
        parts = [x for x in parts if x.lower() not in SERIES_TAGS] or parts
    act = re.sub(r"[^A-Za-z0-9-]+", "-", "-".join(parts)).strip("-")
    return d, m.group(2), ACT_FIXES.get(act.lower(), act)


def attachment_name(d, code, act, ext):
    """-> 'Fri-10-16_OMNIA_Steve-Aoki.jpg'"""
    return f"{d:%a-%m-%d}_{VENUE_LABELS.get(code, code)}_{act}{ext.lower().replace('.jpeg', '.jpg')}"


def _shrink(path, out_dir, quality=70, max_side=1080):
    """Re-encode a flyer smaller (needs Pillow; returns path unchanged without it)."""
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".jpg")
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side * 2))
            im.save(dest, "JPEG", quality=quality, optimize=True)
        return dest
    except Exception:  # noqa: BLE001 - keep the original if it can't be re-encoded
        return path


def week_attachments(flyers_dir, start, end, limit=ATTACH_LIMIT):
    """Every event flyer dated start..end (inclusive) plus the two TAO weekly
    lineups, as [(path, filename)] in date order, named like
    'Fri-10-16_OMNIA_Steve-Aoki.jpg'. If the total is over `limit`, re-encodes
    smaller; if still over, drops the largest-last until it fits."""
    import tempfile
    try:
        names = sorted(os.listdir(flyers_dir))
    except OSError:
        return []
    out, seen = [], set()
    for fn, _, alt in LINEUP_FILES:
        path = os.path.join(flyers_dir, fn)
        if os.path.isfile(path):
            out.append((path, f"TAO-Weekly-Lineup_{'Nightclub' if 'night' in fn else 'Dayclub'}.jpg"))
    events = []
    for n in names:
        parsed = _flyer_parse(n)
        if not parsed or not (start <= parsed[0] <= end):
            continue
        d, code, act = parsed
        fname = attachment_name(d, code, act, os.path.splitext(n)[1])
        if fname.lower() in seen:
            continue
        seen.add(fname.lower())
        events.append((d, list(VENUE_LABELS).index(code) if code in VENUE_LABELS else 99,
                       os.path.join(flyers_dir, n), fname))
    out += [(p, f) for _, _, p, f in sorted(events)]
    total = lambda items: sum(os.path.getsize(p) for p, _ in items)
    if total(out) > limit:
        tmp = tempfile.mkdtemp(prefix="flyers_")
        out = [(_shrink(p, tmp), os.path.splitext(f)[0] + ".jpg") for p, f in out]
    while out and total(out) > limit:
        out.pop()
    return out


def _day(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a %b} {d.day}"


F = "font-family:Inter,Helvetica,Arial,sans-serif;"


def _btn():
    return (f'<a href="{SIGNUP}" target="_blank" style="display:inline-block;background:#D4AF37;color:#050505;{F}'
            'font-size:12px;font-weight:700;line-height:12px;text-decoration:none;padding:10px 12px;'
            'border-radius:6px;">Get on the Guest List</a>')


def _card(p, cid):
    v, a = html.escape(p["venue"]), html.escape(p["artist"])
    if cid:
        vis = (f'<a href="{SIGNUP}" target="_blank"><img src="cid:{cid}" width="176" alt="{a} at {v}" '
               'style="display:block;width:100%;max-width:176px;height:auto;border-radius:8px;"></a>')
    else:
        vis = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
               '<td height="220" align="center" valign="middle" bgcolor="#141008" style="height:220px;'
               'border:1px solid #D4AF37;border-radius:8px;background:#141008;background-image:'
               'radial-gradient(circle at 50% 30%,#3a2c08,#0d0d0d 70%);">'
               f'<p style="margin:0;{F}font-size:10px;letter-spacing:3px;color:#D4AF37;font-weight:700;">'
               f'LIVE AT {v.upper()}</p><p style="margin:6px 0 0;font-family:\'Playfair Display\',Georgia,serif;'
               f'font-size:26px;line-height:30px;color:#F5F5F5;font-weight:700;">{a}</p></td></tr></table>')
    return (f'<td class="col" width="33%" valign="top" style="padding:6px;">{vis}'
            f'<p style="margin:8px 0 2px;{F}font-size:10px;line-height:14px;letter-spacing:2px;'
            f'text-transform:uppercase;color:#D4AF37;font-weight:700;">{_day(p["date"])} &middot; {v}</p>'
            f'<p style="margin:0 0 8px;{F}font-size:15px;line-height:20px;color:#F5F5F5;font-weight:700;">{a}</p>'
            f'{_btn()}</td>')


def _liv_xs_html(liv_xs):
    """Compact LIV & XS block, or empty string when the section is off."""
    if not liv_xs:
        return ""
    H = lambda t: (f'<tr><td style="padding:14px 14px 2px;"><p style="margin:0;{F}font-size:11px;letter-spacing:4px;'
                   f'text-transform:uppercase;color:#D4AF37;font-weight:700;">{t}</p></td></tr>')
    parts = [H("LIV &amp; XS")]
    if liv_xs.get("lineup_cid"):
        parts.append(
            '<tr><td style="padding:0 14px 8px;"><img src="cid:' + liv_xs["lineup_cid"] +
            '" width="572" alt="LIV weekend lineup" style="display:block;width:100%;max-width:572px;'
            'height:auto;border-radius:8px;"></td></tr>')
    flyer_ev = [e for e in liv_xs.get("events") or [] if e.get("cid")]
    if flyer_ev:
        cells = []
        width = 50 if len(flyer_ev) <= 2 else 33
        for e in flyer_ev:
            alt = html.escape(f"{e['event']} at {e['venue']}")
            cells.append(
                f'<td class="col" width="{width}%" valign="top" style="padding:6px;">'
                f'<img src="cid:{e["cid"]}" width="276" alt="{alt}" '
                'style="display:block;width:100%;max-width:276px;height:auto;border-radius:8px;"></td>')
        parts.append('<tr><td style="padding:0 8px 4px;"><table role="presentation" width="100%" '
                     f'cellpadding="0" cellspacing="0" border="0"><tr>{"".join(cells)}</tr></table></td></tr>')
    rows = []
    for e in liv_xs.get("events") or []:
        label = html.escape(f"{e['weekday']} {date.fromisoformat(e['date']).day} · {e['venue']} · {e['event']}")
        rules = html.escape(e["rules"]) if e.get("rules") else ""
        rules_html = (f'<div style="margin:2px 0 0;{F}font-size:12px;line-height:18px;color:#BDBDBD;">{rules}</div>'
                      if rules else "")
        rows.append(
            f'<tr><td style="padding:7px 0;border-bottom:1px solid #1f1a0c;">'
            f'<p style="margin:0;{F}font-size:13px;line-height:18px;color:#F5F5F5;font-weight:700;">{label}</p>'
            f'{rules_html}</td></tr>')
    for note in liv_xs.get("notes") or []:
        rows.append(
            f'<tr><td style="padding:7px 0;border-bottom:1px solid #1f1a0c;{F}font-size:12px;'
            f'line-height:18px;color:#BDBDBD;">{html.escape(note)}</td></tr>')
    cap = html.escape(liv_xs.get("capacity_note") or CAPACITY_NOTE)
    rows.append(
        f'<tr><td style="padding:8px 0 0;{F}font-size:11px;line-height:16px;color:#8a8a8a;">{cap}</td></tr>')
    parts.append('<tr><td style="padding:2px 14px 14px;"><table role="presentation" width="100%" '
                 f'cellpadding="0" cellspacing="0" border="0">{"".join(rows)}</table></td></tr>')
    return "".join(parts)


def render_html(start, end, picks, pick_cids, also, liv_xs=None):
    lineup = "".join(
        f'<td class="col" width="50%" valign="top" style="padding:6px;"><img src="cid:{cid}" width="276" '
        f'alt="{alt}" style="display:block;width:100%;max-width:276px;height:auto;border-radius:8px;"></td>'
        for _, cid, alt in LINEUP_FILES)
    top = "".join(_card(p, c) for p, c in zip(picks, pick_cids))
    rows = "".join(
        f'<tr><td valign="top" width="58" style="padding:5px 0;border-bottom:1px solid #1f1a0c;{F}font-size:11px;'
        f'line-height:17px;color:#D4AF37;font-weight:700;white-space:nowrap;">'
        f'{date.fromisoformat(iso):%a %d}'.upper() + '</td>'
        f'<td style="padding:5px 0 5px 8px;border-bottom:1px solid #1f1a0c;{F}font-size:12px;line-height:17px;'
        'color:#BDBDBD;">' + " &middot; ".join(
            f'<span style="color:#F5F5F5;font-weight:700;">{html.escape(a)}</span> @ {html.escape(v)}'
            for v, a in ev) + '</td></tr>' for iso, ev in also)
    H = lambda t: (f'<tr><td style="padding:14px 14px 2px;"><p style="margin:0;{F}font-size:11px;letter-spacing:4px;'
                   f'text-transform:uppercase;color:#D4AF37;font-weight:700;">{t}</p></td></tr>')
    names = ", ".join(html.escape(p["artist"]) for p in picks)
    also_block = (H("Also This Week") + '<tr><td style="padding:2px 14px 14px;"><table role="presentation" '
                  f'width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>') if also else ""
    liv_block = _liv_xs_html(liv_xs)
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="x-apple-disable-message-reformatting"><meta name="color-scheme" content="light only">
<title>This Week in Vegas | Playmaker Entertainment</title>
<style>body{{margin:0;padding:0;background:#050505;}}img{{border:0;}}@media screen and (max-width:620px){{.container{{width:100%!important;}}.col{{display:block!important;width:100%!important;box-sizing:border-box;}}.col img{{max-width:100%!important;}}}}</style></head>
<body bgcolor="#050505" style="margin:0;background:#050505;">
<div style="display:none;max-height:0;overflow:hidden;">{names} headline this week. Get on the list.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#050505"><tr><td align="center" style="padding:16px 8px;">
<table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#0d0d0d" style="width:100%;max-width:600px;background:#0d0d0d;border:1px solid #2a2310;border-radius:14px;">
<tr><td height="4" bgcolor="#D4AF37" style="height:4px;font-size:0;line-height:4px;background:#D4AF37;background-image:linear-gradient(90deg,#AA6C08,#F7DD72,#D4AF37,#AA6C08);border-radius:14px 14px 0 0;"></td></tr>
<tr><td align="center" style="padding:18px 20px 2px;"><img src="{LOGO}" width="180" alt="Playmaker Entertainment" style="display:block;width:180px;height:auto;"></td></tr>
<tr><td align="center" style="padding:8px 20px 6px;"><h1 style="margin:0;font-family:'Playfair Display',Georgia,serif;font-size:30px;line-height:36px;color:#F5F5F5;">This Week in Vegas</h1>
<p style="margin:6px 0 0;{F}font-size:14px;line-height:20px;color:#BDBDBD;">Big names, best rooms, and you&rsquo;re on the list.</p></td></tr>
{H(f"The Lineup &middot; {start:%b} {start.day}&ndash;{end:%b} {end.day}")}
<tr><td style="padding:0 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{lineup}</tr></table></td></tr>
{H("Don&rsquo;t Miss")}
<tr><td style="padding:0 8px 12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{top}</tr></table></td></tr>
{liv_block}
{also_block}
<tr><td align="center" style="padding:14px 20px 18px;border-top:1px solid #2a2310;{F}font-size:12px;line-height:18px;color:#8a8a8a;">Questions? <a href="mailto:{CONTACT}" style="color:#D4AF37;">{CONTACT}</a> &middot; <a href="https://instagram.com/playmaker.entertainment" style="color:#D4AF37;">@playmaker.entertainment</a><br>Playmaker Entertainment &middot; Las Vegas</td></tr>
</table></td></tr></table></body></html>'''


def render_text(start, end, picks, also, liv_xs=None):
    lines = [f"THIS WEEK IN VEGAS — {start:%b} {start.day}-{end:%b} {end.day}",
             "Big names, best rooms, and you're on the list.", "", "DON'T MISS"]
    lines += [f"• {_day(p['date'])} — {p['artist']} @ {p['venue']}" for p in picks]
    if liv_xs:
        lines += ["", "LIV & XS"]
        for e in liv_xs.get("events") or []:
            row = f"{e['weekday']} {date.fromisoformat(e['date']).day} — {e['event']} @ {e['venue']}"
            if e.get("rules"):
                row += f". {e['rules']}"
            lines.append(row)
        lines += list(liv_xs.get("notes") or [])
        lines.append(liv_xs.get("capacity_note") or CAPACITY_NOTE)
    if also:
        lines += ["", "ALSO THIS WEEK"]
        lines += [f"{_day(iso)}: " + " · ".join(f"{a} @ {v}" for v, a in ev) for iso, ev in also]
    lines += ["", f"Get on the guest list: {SIGNUP}",
              "All of this week's flyers are attached.", "",
              f"Questions? {CONTACT}", "Playmaker Entertainment · Las Vegas"]
    return "\n".join(lines) + "\n"


def build(today, data_dir, flyers_dir, headliners_map):
    """-> (subject, text, html, inline_images [(path, cid)], attachments
    [(path, filename)]). Raises
    MissingLineup if either TAO weekly lineup flyer is absent (fail closed).
    The LIV & XS section is omitted when data/liv_xs.json is missing, empty,
    disabled, or dated for another week; the rest of the newsletter still builds."""
    start, end = week_window(today)
    inline = []
    for fn, cid, _ in LINEUP_FILES:
        path = os.path.join(flyers_dir, fn)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise MissingLineup(fn)
        inline.append((path, cid))
    picks = load_picks(data_dir, start, end, headliners_map)
    cids = []
    for i, p in enumerate(picks):
        path = find_flyer(flyers_dir, p)
        cid = f"pick-{i + 1}" if path else None
        if path:
            inline.append((path, cid))
        cids.append(cid)
    also = load_also(data_dir, start, end, headliners_map, picks)
    liv_xs = load_liv_xs(data_dir, start, end, flyers_dir)
    if liv_xs:
        if liv_xs.get("lineup_path"):
            liv_xs["lineup_cid"] = "liv-xs-lineup"
            inline.append((liv_xs["lineup_path"], "liv-xs-lineup"))
        for i, ev in enumerate(liv_xs["events"]):
            if ev.get("flyer_path"):
                ev["cid"] = f"liv-xs-{i}"
                inline.append((ev["flyer_path"], ev["cid"]))
    # Subject names the Wednesday send day (unchanged wording, so the weekly
    # dedupe still matches an issue that already went out).
    send_day = start + timedelta(days=(2 - start.weekday()) % 7)
    subject = f"🎉 This Week in Vegas — {send_day:%A, %B} {send_day.day}"
    return (subject, render_text(start, end, picks, also, liv_xs),
            render_html(start, end, picks, cids, also, liv_xs), inline,
            week_attachments(flyers_dir, start, end))

"""Find a Posh child event's real start time from its public event page.

Posh recurring series are separate per-date child events, each with its own
event_id. The webhook sends the child's event_id but the SERIES' first
event_start. Posh has no public lookup by id (https://posh.vip/e/<id> just
returns an empty shell, and /api/ is disallowed in robots.txt), but each
public event page (https://posh.vip/e/<slug>, allowed in robots.txt):
  * carries schema.org JSON-LD:  "startDate": "2026-10-17T19:30:00-07:00"
  * lists its sibling dates as links keyed by event id:
        "6a80e79648e2b4a1334418aa",{"href":"/e/guestlist-tao-nc-2026-10-18-8-30"
Slugs are <slugified name>-<UTC END date Y-M-D, unpadded>-<UTC end H-MM>.

Lookup: find one page in the series (cached id->slug map, or a few slug
guesses around the purchase date), read its sibling map, open the target
id's own page, read startDate. Low volume: cached per event_id, a hard cap
on fetches per lookup, 1 s between fetches. Any failure returns None.
"""
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("amy.posh_lookup")
VEGAS = ZoneInfo("America/Los_Angeles")
BASE = "https://posh.vip/e/"
MAX_FETCHES = int(os.environ.get("POSH_LOOKUP_MAX_FETCHES", "80"))
# Seeded series: probe one page per week this far past the purchase. Posh
# pages no longer list sibling dates (Nov 2026), and LAVO/DESEO buyers order
# up to 5+ weeks ahead, so the target's own page must be found directly.
WEEKLY_PROBE_WEEKS = int(os.environ.get("POSH_LOOKUP_WEEKS", "12"))
# Seen on Playmaker series (UTC end): TAO NC 8-30/9-30, OMNIA/JEWEL 8-0/9-0, Hakkasan 11-30/12-30, LAVO 1-0/2-0.
DEFAULT_END_TIMES = ("8-30", "9-30", "8-0", "9-0", "11-30", "12-30", "12-0", "11-0", "1-0", "2-0", "6-0", "10-0", "10-30", "13-0", "11-0-1", "12-0-1")

SIBLING_RE = re.compile(r'"([0-9a-f]{24})",\{"href":"/e/([a-z0-9-]+)"')
EVENT_ID_RE = re.compile(r'"eventId"\s*:\s*"([0-9a-f]{24})"')
START_RE = re.compile(r'"startDate"\s*:\s*"([^"]+)"')
OG_URL_RE = re.compile(r'og:url" content="https://posh\.vip/e/([a-z0-9-]+)"')
SLUG_TAIL_RE = re.compile(r"-(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2}(?:-\d+)?)$")

_lock = threading.Lock()
_starts = {}    # event_id -> aware datetime
_slugs = {}     # event_id -> slug
_end_times = {}  # slug base -> {"8-30", ...}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _unescape(html):
    return (html or "").replace('\\"', '"')


def page_event_id(html):
    m = EVENT_ID_RE.search(_unescape(html))
    return m.group(1) if m else None


def siblings(html):
    out = dict(SIBLING_RE.findall(_unescape(html)))
    eid, slug = page_event_id(html), page_slug(html)
    if eid and slug and eid not in out:
        out[eid] = slug
    return out


def page_start(html):
    """Aware start datetime from the page's JSON-LD, else None."""
    m = START_RE.search(_unescape(html))
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def page_slug(html):
    m = OG_URL_RE.search(html or "")
    return m.group(1) if m else None


PREFIX_LEN = 18
_series = {}  # id prefix -> {slug bases}


def series_key(event_id):
    return (event_id or "")[:PREFIX_LEN] if len(event_id or "") == 24 else ""


# Posh child ids are MongoDB ObjectIds: 4-byte timestamp + 5-byte random +
# 3-byte counter. A long series minted in one burst can cross a 1-second
# timestamp boundary, so the first 8 hex chars change while the random
# (chars 8-17) stays the same. The 18-char seed prefix then misses later
# children (OMNIA 6acab072... -> 6acab073... on 2026-10-10 21:38:59Z).
_OID_TS_SLOP = 2  # seconds


def series_keys(event_id):
    """18-char prefixes for this id, including timestamp +/- slop with the
    same 5-byte random, so a series that crossed a 1-second ObjectId
    boundary still matches its seed and siblings."""
    key = series_key(event_id)
    if not key:
        return []
    keys = [key]
    try:
        ts = int(event_id[0:8], 16)
        rand = event_id[8:18]
        for delta in range(-_OID_TS_SLOP, _OID_TS_SLOP + 1):
            if delta == 0:
                continue
            keys.append(f"{(ts + delta) & 0xFFFFFFFF:08x}{rand}")
    except ValueError:
        pass
    return list(dict.fromkeys(keys))


def same_series(a, b):
    """True when two child event ids belong to one series, including
    across a 1-second ObjectId timestamp tick."""
    ka, kb = series_key(a), series_key(b)
    return bool(ka and kb) and (ka == kb or ka in series_keys(b) or kb in series_keys(a))


def seed_for(event_id):
    """Seed tuple for this child id, trying every adjacent ObjectId prefix."""
    known = seeds()
    for key in series_keys(event_id):
        if key in known:
            return known[key]
    return None


def seeds():
    """{prefix: (base, end_times)} from config plus POSH_SERIES_SEEDS env."""
    from config import rules
    out = dict(getattr(rules, "POSH_SERIES_SEEDS", {}))
    for item in os.environ.get("POSH_SERIES_SEEDS", "").split(";"):
        m = re.match(r"\s*([0-9a-f]{6,24})\s*=\s*([a-z0-9-]+)\s*(?::\s*([0-9/ -]+))?\s*$", item)
        if m:
            out[m.group(1)[:PREFIX_LEN]] = (m.group(2), tuple(t.strip() for t in (m.group(3) or "").split("/") if t.strip()))
    return out


def seed_weekdays(seed):
    """Weekdays (Mon=0) of a seeded series' slug end dates, or () if unknown."""
    return tuple(seed[2]) if seed and len(seed) > 2 and seed[2] else ()


def _base_and_time(slug):
    m = SLUG_TAIL_RE.search(slug)
    return (slug[:m.start()], f"{int(m.group(4))}-{m.group(5)}") if m else (None, None)


def _learn(found):
    for event_id, slug in found.items():
        _slugs[event_id] = slug
        base, t = _base_and_time(slug)
        if base:
            _end_times.setdefault(base, set()).add(t)
            if series_key(event_id):
                _series.setdefault(series_key(event_id), set()).add(base)


def series_bases(event_id):
    """Slug bases known for this event's series: learned first, then seeds."""
    keys = series_keys(event_id)
    if not keys:
        return []
    out = []
    for key in keys:
        out.extend(sorted(_series.get(key, ())))
    seed = seed_for(event_id)
    if seed:
        out.append(seed[0])
        for t in seed[1]:
            _end_times.setdefault(seed[0], set()).add(t)
    return list(dict.fromkeys(out))


def name_bases(event_name):
    """Slug bases to try: the plain slug, '&' variants ("R&BAE" ->
    r-bae / rbae / r-and-bae), and guestlist <-> guest-list swaps."""
    name = event_name or ""
    forms = [name, name.replace("&", ""), name.replace("&", " and ")]
    out = list(dict.fromkeys(b for b in map(slugify, forms) if b))
    base = out[0] if out else ""
    for a, b in (("guest-list", "guestlist"), ("guestlist", "guest-list")):
        for x in list(out):
            if a in x:
                out.append(x.replace(a, b))
    return list(dict.fromkeys(out))


def _hm(dt):
    return f"{dt.hour}-{dt.minute:02d}"


def _forms(dt):
    out = [_hm(dt)]
    if dt.minute == 0:
        out.append(f"{dt.hour}-0")  # on-the-hour slug form unconfirmed: try both
    return out


def _series_clock(value):
    """Naive wall clock from a payload time, which may be a str or datetime.

    String values keep the historic first-19-characters parse. Datetimes
    (seen when a stored payload is re-ingested) use their wall clock so
    derived_end_times never calls .strip() on a non-string.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).strip()[:19])
    except ValueError:
        return None


def derived_end_times(event_start=None, event_end=None, wide=False):
    """UTC end-time slug parts suggested by the payload's series times.

    Posh sends series times as Vegas wall clock marked 'Z' (Hakkasan: start
    T22:30Z = 10:30 PM, child slugs end in 11-30 = 4:30 AM PDT). The end
    (event_end, else start + 6 h) is turned into UTC for PDT (+7) and PST
    (+8). wide=True adds start + 5/7 h and the raw clock read as real UTC."""
    ends = []
    end_dt = _series_clock(event_end)
    start_dt = _series_clock(event_start)
    if end_dt:
        ends.append(end_dt)
    if start_dt:
        ends.append(start_dt + timedelta(hours=6))
        if wide:
            ends += [start_dt + timedelta(hours=5), start_dt + timedelta(hours=7)]
    out = []
    for e in ends:
        for x in (e + timedelta(hours=7), e + timedelta(hours=8)) + ((e,) if wide else ()):
            out += _forms(x)
    return list(dict.fromkeys(out))


def fallback_end_times():
    env = [t.strip() for t in os.environ.get("POSH_SLUG_END_TIMES", "").split(",") if t.strip()]
    return env or list(DEFAULT_END_TIMES)


def candidate_slugs(event_name, purchased_utc, days_before=1, days_after=13, event_start=None, event_end=None):
    """Slug guesses, most likely first (the fetch cap cuts the tail):
    1. end times already learned for this series, full date window
    2. times derived from the payload's series start/end, days -1..+7
    3. the common Playmaker end times (first 4 defaults), days 0..+7
    4. the alternate name form (guestlist <-> guest-list) with 2+3, days 0..+3
    5. everything else (wider derived times, other defaults), days 0..+3"""
    bases = name_bases(event_name)
    if not bases or purchased_utc is None:
        return []
    main, alts = bases[0], bases[1:]
    learned = sorted({t for b in bases for t in _end_times.get(b, ())})
    derived = derived_end_times(event_start, event_end)
    defaults = fallback_end_times()
    common, rest = defaults[:6], defaults[6:]
    wide = derived_end_times(event_start, event_end, wide=True)
    out = []

    def add(base_list, times, before, after):
        for offset in range(-before, after + 1):
            d = (purchased_utc + timedelta(days=offset)).date()
            for base in base_list:
                for t in times:
                    out.append(f"{base}-{d.year}-{d.month}-{d.day}-{t}")
    add([main], learned, days_before, days_after)
    add([main], derived, 1, 7)
    add([main], common, 0, 7)
    add(alts, list(dict.fromkeys(learned + derived + common)), 0, 3)
    add([main], wide + rest, 0, 3)
    return list(dict.fromkeys(out))


def _default_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "PlaymakerAmy/1.0 (+guest-list agent)",
                                               "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


class _Fetcher:
    def __init__(self, fetch, sleep, limit):
        self.fetch, self.sleep, self.left, self.first = fetch, sleep, limit, True
        # Runs in the webhook's background worker (Posh is acked first, 720078a).
        self.deadline = time.monotonic() + float(os.environ.get("POSH_LOOKUP_MAX_SECONDS", "150"))

    def __call__(self, slug):
        if self.left <= 0:
            raise RuntimeError("fetch budget used up")
        if time.monotonic() > self.deadline:
            raise RuntimeError("time budget used up")
        self.left -= 1
        if not self.first:
            self.sleep(1)
        self.first = False
        try:
            html = self.fetch(BASE + slug)
        except Exception as exc:  # noqa: BLE001
            log.info("Posh page %s not readable: %s", slug, exc)
            return None
        return html if page_start(html) else None  # not-found pages have no JSON-LD


def real_start(event_id, event_name, purchased_utc, fetch=_default_fetch, sleep=time.sleep,
               max_fetches=MAX_FETCHES, event_start=None, event_end=None):
    """Aware start datetime of the child event, or None."""
    if not event_id:
        return None
    with _lock:
        if event_id in _starts:
            return _starts[event_id]
        get = _Fetcher(fetch, sleep, max_fetches)
        try:
            start = _resolve(event_id, event_name, purchased_utc, get, event_start, event_end)
        except RuntimeError as exc:
            log.info("Posh lookup for %s gave up: %s", event_id, exc)
            start = None
        if start:
            _starts[event_id] = start
        return start


def _series_slugs(base, purchased_utc, before=1, after=7):
    times = sorted(_end_times.get(base, ())) or fallback_end_times()[:4]
    out = []
    for offset in range(-before, after + 1):
        d = (purchased_utc + timedelta(days=offset)).date()
        out += [f"{base}-{d.year}-{d.month}-{d.day}-{t}" for t in times]
    return out


def _all_candidates(event_id, event_name, purchased_utc, event_start, event_end):
    """Known series bases first (child events can be renamed, slugs keep the
    series name), then guesses from this order's own name."""
    if purchased_utc is None:
        return []
    out = []
    for base in series_bases(event_id):
        out += _series_slugs(base, purchased_utc)
    out += candidate_slugs(event_name, purchased_utc, event_start=event_start, event_end=event_end)
    return list(dict.fromkeys(out))



def _series_id_step(earlier_id, later_id):
    """Counter stride between two same-series ObjectIds.

    A 1-second timestamp tick adds ~2^56 to the integer id, so a raw
    subtraction cannot be used as the event stride. When the 5-byte
    random matches, use the 3-byte counters instead.
    """
    raw = int(later_id, 16) - int(earlier_id, 16)
    if 0 < raw < 0x1000000:
        return raw
    if later_id[8:18] == earlier_id[8:18]:
        return int(later_id[18:24], 16) - int(earlier_id[18:24], 16)
    return raw


def _extrapolate_beyond_carousel(found, target_id, key):
    """When target_id is beyond a series page's sibling carousel, project its
    slug from the series' hex-ID stride and active weekdays."""
    if not series_key(target_id):
        return []
    series_items = sorted((i, s) for i, s in found.items() if same_series(i, target_id) and i < target_id)
    if len(series_items) < 2:
        return []
    max_id, max_slug = series_items[-1]
    m = SLUG_TAIL_RE.search(max_slug)
    if not m:
        return []
    base = max_slug[:m.start()]
    try:
        d_max = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        target_diff = _series_id_step(max_id, target_id)
    except ValueError:
        return []
    if not target_diff or target_diff <= 0:
        return []
    t_max = f"{int(m.group(4))}-{m.group(5)}"
    times = list(dict.fromkeys([t_max] + sorted(_end_times.get(base, ()))))
    id_steps, weekdays = [], []
    for idx, (eid, s) in enumerate(series_items):
        ms = SLUG_TAIL_RE.search(s)
        if not ms:
            continue
        try:
            dt = date(int(ms.group(1)), int(ms.group(2)), int(ms.group(3)))
            weekdays.append(dt.weekday())
            if idx > 0:
                diff = int(eid, 16) - int(series_items[idx - 1][0], 16)
                # Ignore the 4-byte ObjectId timestamp jump (~2^56) when a
                # series crosses a 1-second boundary; the 3-byte counter is
                # under 0x1000000 and the usual stride is 3.
                if 0 < diff < 0x1000000:
                    id_steps.append(diff)
        except ValueError:
            continue
    if not id_steps or not weekdays:
        return []
    step = min(id_steps)
    if target_diff % step != 0:
        return []
    n_events = target_diff // step
    if n_events <= 0 or n_events > 60:
        return []
    active_wds = set(weekdays)
    cur, count = d_max, 0
    while count < n_events and (cur - d_max).days < 180:
        cur += timedelta(days=1)
        if cur.weekday() in active_wds:
            count += 1
    if count != n_events:
        return []
    out = []
    for delta in (0, -1, 1):
        d = cur + timedelta(days=delta)
        out += [f"{base}-{d.year}-{d.month}-{d.day}-{t}" for t in times]
    return out


def weekly_probe_slugs(base, times, weekdays, purchased_utc, weeks=None):
    """[(week, [slugs for that week])]: the series weekday(s) from the day
    before the purchase onward, one date per weekday per week."""
    weeks = WEEKLY_PROBE_WEEKS if weeks is None else weeks
    first = (purchased_utc - timedelta(days=1)).date()
    out = []
    for w in range(weeks):
        slugs = []
        for wd in weekdays:
            d = first + timedelta(days=(wd - first.weekday()) % 7 + 7 * w)
            slugs += [f"{base}-{d.year}-{d.month}-{d.day}-{t}" for t in times]
        out.append((w, slugs))
    return out


def _weekly_probe(event_id, purchased_utc, get, tried, event_start=None, event_end=None, need_weekdays=True):
    """Seeded series: walk the series week by week and match the child page's
    own event id (Posh pages no longer carry the sibling-date map).
    need_weekdays=True (first pass) only runs for seeds with known weekdays;
    the second pass (after the sibling search) learns them from nearby pages."""
    key = series_key(event_id)
    seed = seed_for(event_id)
    if not seed or purchased_utc is None:
        return None
    base = seed[0]
    weekdays = seed_weekdays(seed)
    if need_weekdays and not weekdays:
        return None
    learned = sorted(_end_times.get(base, ())) if not weekdays else []
    times = list(dict.fromkeys(learned + list(seed[1]) + derived_end_times(event_start, event_end)))
    if not weekdays:
        # Weekdays of series pages already read (sibling search above).
        seen = set()
        for eid, slug in list(_slugs.items()):
            m = SLUG_TAIL_RE.search(slug)
            if m and same_series(eid, event_id) and slug.startswith(base + "-"):
                try:
                    seen.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))).weekday())
                except ValueError:
                    pass
        weekdays = tuple(sorted(seen))
    if not weekdays:
        # Learn the weekday(s) from the first week of pages around the purchase.
        hits = set()
        for offset in range(-1, 7):
            d = (purchased_utc + timedelta(days=offset)).date()
            for t in times:
                slug = f"{base}-{d.year}-{d.month}-{d.day}-{t}"
                if slug in tried:
                    continue
                html = get(slug)
                tried.add(slug)
                if not html:
                    continue
                if page_event_id(html) == event_id:
                    return page_start(html)
                hits.add(d.weekday())
                times = [t] + [x for x in times if x != t]
                break
        weekdays = tuple(sorted(hits))
        if not weekdays:
            return None
    confirmed = None
    for _w, slugs in weekly_probe_slugs(base, times, weekdays, purchased_utc):
        if confirmed:  # the end time that worked last week first
            slugs = sorted(slugs, key=lambda s: not s.endswith("-" + confirmed))
        found_dates = set()
        for slug in slugs:
            m = SLUG_TAIL_RE.search(slug)
            day = m.group(1, 2, 3) if m else slug
            if slug in tried or day in found_dates:
                continue  # one page per weekday per week is enough
            html = get(slug)
            tried.add(slug)
            if not html:
                continue
            found_dates.add(day)
            pid = page_event_id(html)
            _learn({pid: slug} if pid else {})
            _, confirmed = _base_and_time(slug)
            if pid == event_id:
                return page_start(html)
            if pid and same_series(pid, event_id) and pid > event_id and len(weekdays) == 1:
                return None  # walked past the target: it is earlier than expected
    return None


def _resolve(event_id, event_name, purchased_utc, get, event_start=None, event_end=None):
    tried = set()
    key = series_key(event_id)

    def open_target(slug):
        html = get(slug)
        tried.add(slug)
        if html and page_slug(html) in (None, slug):
            _learn(siblings(html))
            return page_start(html)
        return None

    if event_id in _slugs:
        start = open_target(_slugs[event_id])
        if start:
            return start
    start = _weekly_probe(event_id, purchased_utc, get, tried, event_start, event_end)
    if start:
        return start
    for slug in _all_candidates(event_id, event_name, purchased_utc, event_start, event_end):
        if slug in tried:
            continue
        html = get(slug)
        tried.add(slug)
        if not html:
            continue
        for _hop in range(4):
            found = siblings(html)
            _learn(found)
            if event_id in found:
                if found[event_id] == page_slug(html):
                    return page_start(html)
                return open_target(found[event_id])
            for ext_slug in _extrapolate_beyond_carousel(found, event_id, key):
                if ext_slug in tried:
                    continue
                ext_html = get(ext_slug)
                tried.add(ext_slug)
                if not ext_html:
                    continue
                ext_found = siblings(ext_html)
                _learn(ext_found)
                if event_id in ext_found:
                    if ext_found[event_id] == page_slug(ext_html):
                        return page_start(ext_html)
                    return open_target(ext_found[event_id])
            series_ids = [i for i in found if same_series(i, event_id)] if key else []
            can_hop = bool(series_ids) and max(series_ids) < event_id
            later = [found[i] for i in series_ids if found[i] not in tried] if can_hop else []
            if not later:
                break  # another series, or nothing further: keep guessing
            nxt = later[-1]
            html = get(nxt)
            tried.add(nxt)
            if not html:
                break
    return _weekly_probe(event_id, purchased_utc, get, tried, event_start, event_end, need_weekdays=False)


def night_from_start(start, rollover_hour=6):
    local = start.astimezone(VEGAS)
    return (local - timedelta(days=1)).date() if local.hour < rollover_hour else local.date()


def reset_cache():
    with _lock:
        _starts.clear()
        _slugs.clear()
        _end_times.clear()
        _series.clear()

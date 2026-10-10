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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("amy.posh_lookup")
VEGAS = ZoneInfo("America/Los_Angeles")
BASE = "https://posh.vip/e/"
MAX_FETCHES = int(os.environ.get("POSH_LOOKUP_MAX_FETCHES", "40"))
# Seen on Playmaker series (UTC end): TAO NC 8-30/9-30, Hakkasan 11-30/12-30.
DEFAULT_END_TIMES = ("8-30", "9-30", "11-30", "12-30", "8-0", "9-0", "10-0", "10-30", "11-0", "12-0")

SIBLING_RE = re.compile(r'"([0-9a-f]{24})",\{"href":"/e/([a-z0-9-]+)"')
START_RE = re.compile(r'"startDate"\s*:\s*"([^"]+)"')
OG_URL_RE = re.compile(r'og:url" content="https://posh\.vip/e/([a-z0-9-]+)"')
SLUG_TAIL_RE = re.compile(r"-(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})$")

_lock = threading.Lock()
_starts = {}    # event_id -> aware datetime
_slugs = {}     # event_id -> slug
_end_times = {}  # slug base -> {"8-30", ...}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _unescape(html):
    return (html or "").replace('\\"', '"')


def siblings(html):
    return dict(SIBLING_RE.findall(_unescape(html)))


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


def seeds():
    """{prefix: (base, end_times)} from config plus POSH_SERIES_SEEDS env."""
    from config import rules
    out = dict(getattr(rules, "POSH_SERIES_SEEDS", {}))
    for item in os.environ.get("POSH_SERIES_SEEDS", "").split(";"):
        m = re.match(r"\s*([0-9a-f]{6,24})\s*=\s*([a-z0-9-]+)\s*(?::\s*([0-9/ -]+))?\s*$", item)
        if m:
            out[m.group(1)[:PREFIX_LEN]] = (m.group(2), tuple(t.strip() for t in (m.group(3) or "").split("/") if t.strip()))
    return out


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
    key = series_key(event_id)
    if not key:
        return []
    out = sorted(_series.get(key, ()))
    seed = seeds().get(key)
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


def derived_end_times(event_start=None, event_end=None, wide=False):
    """UTC end-time slug parts suggested by the payload's series times.

    Posh sends series times as Vegas wall clock marked 'Z' (Hakkasan: start
    T22:30Z = 10:30 PM, child slugs end in 11-30 = 4:30 AM PDT). The end
    (event_end, else start + 6 h) is turned into UTC for PDT (+7) and PST
    (+8). wide=True adds start + 5/7 h and the raw clock read as real UTC."""
    ends = []
    try:
        if event_end:
            ends.append(datetime.fromisoformat(event_end.strip()[:19]))
        if event_start:
            st = datetime.fromisoformat(event_start.strip()[:19])
            ends.append(st + timedelta(hours=6))
            if wide:
                ends += [st + timedelta(hours=5), st + timedelta(hours=7)]
    except ValueError:
        pass
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
    common, rest = defaults[:4], defaults[4:]
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

    def __call__(self, slug):
        if self.left <= 0:
            raise RuntimeError("fetch budget used up")
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
    for slug in _all_candidates(event_id, event_name, purchased_utc, event_start, event_end):
        if slug in tried:
            continue
        html = get(slug)
        tried.add(slug)
        if not html:
            continue
        for _hop in range(3):
            found = siblings(html)
            _learn(found)
            if event_id in found:
                if found[event_id] == page_slug(html):
                    return page_start(html)
                return open_target(found[event_id])
            same_series = key and any(series_key(i) == key for i in found)
            later = [s for s in found.values() if s not in tried] if same_series else []
            if not later:
                break  # another series, or nothing further: keep guessing
            nxt = later[-1]
            html = get(nxt)
            tried.add(nxt)
            if not html:
                break
    return None


def night_from_start(start, rollover_hour=6):
    local = start.astimezone(VEGAS)
    return (local - timedelta(days=1)).date() if local.hour < rollover_hour else local.date()


def reset_cache():
    with _lock:
        _starts.clear()
        _slugs.clear()
        _end_times.clear()
        _series.clear()

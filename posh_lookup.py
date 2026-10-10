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
MAX_FETCHES = int(os.environ.get("POSH_LOOKUP_MAX_FETCHES", "25"))
DEFAULT_END_TIMES = ("8-30", "9-30")  # seen on Playmaker series (UTC end, PDT/PST)

SIBLING_RE = re.compile(r'"([0-9a-f]{24})",\{"href":"/e/([a-z0-9-]+)"')
START_RE = re.compile(r'"startDate"\s*:\s*"([^"]+)"')
OG_URL_RE = re.compile(r'og:url" content="https://posh\.vip/e/([a-z0-9-]+)"')
SLUG_TAIL_RE = re.compile(r"-(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{2})$")

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


def _learn(found):
    for event_id, slug in found.items():
        _slugs[event_id] = slug
        m = SLUG_TAIL_RE.search(slug)
        if m:
            _end_times.setdefault(slug[:m.start()], set()).add(f"{int(m.group(4))}-{m.group(5)}")


def candidate_slugs(event_name, purchased_utc, days_before=1, days_after=13):
    base = slugify(event_name)
    if not base or purchased_utc is None:
        return []
    times = list(_end_times.get(base, ())) + [t for t in os.environ.get(
        "POSH_SLUG_END_TIMES", ",".join(DEFAULT_END_TIMES)).split(",") if t.strip()]
    times = list(dict.fromkeys(t.strip() for t in times))
    out = []
    for offset in range(-days_before, days_after + 1):
        d = (purchased_utc + timedelta(days=offset)).date()
        for t in times:
            out.append(f"{base}-{d.year}-{d.month}-{d.day}-{t}")
    return out


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
               max_fetches=MAX_FETCHES):
    """Aware start datetime of the child event, or None."""
    if not event_id:
        return None
    with _lock:
        if event_id in _starts:
            return _starts[event_id]
        get = _Fetcher(fetch, sleep, max_fetches)
        try:
            start = _resolve(event_id, event_name, purchased_utc, get)
        except RuntimeError as exc:
            log.info("Posh lookup for %s gave up: %s", event_id, exc)
            start = None
        if start:
            _starts[event_id] = start
        return start


def _resolve(event_id, event_name, purchased_utc, get):
    tried = set()

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
    for slug in candidate_slugs(event_name, purchased_utc):
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
            # Not in this window: hop to the furthest-out sibling and look again.
            later = [s for s in found.values() if s not in tried]
            if not later:
                return None
            nxt = later[-1]
            html = get(nxt)
            tried.add(nxt)
            if not html:
                return None
        return None
    return None


def night_from_start(start, rollover_hour=6):
    local = start.astimezone(VEGAS)
    return (local - timedelta(days=1)).date() if local.hour < rollover_hour else local.date()


def reset_cache():
    with _lock:
        _starts.clear()
        _slugs.clear()
        _end_times.clear()

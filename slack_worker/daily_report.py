"""Daily 6:00 AM MST last-night guest-list totals, posted to #generalchat.

Only confirmed bookings for last night's America/Phoenix date, by venue:
signup (party) counts and Girls/Guys (F/M). That date is yesterday at
6:00 AM (the evening that just wrapped, including that date's dayclubs).
No names, holds, queue, last-24h, sign-up, or pay figures. No guest
emails or phones.

Posts at most once per night at 6:00 AM America/Phoenix. The last-posted
night is saved to a dedicated state file so restarts do not double-post.
An optional channel-history check is a second guard if that file is missing.
A missed 6:00 AM slot may still post until 10:00 AM for that same evening's
date; nothing posts before 6:00 AM or at/after 10:00 AM. Older
last_posted_date values from the retired 10 AM schedule are ignored. A
night already posted under the retired 11 PM schedule is not posted again.
"""
from collections import OrderedDict
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from .views import (EMAIL_RE, SECRET_RE, SECRET_URL_RE, on_date, project,
                    saved_bookings, venues as venue_names)

log = logging.getLogger('amy.slack.daily_report')
PHOENIX = ZoneInfo('America/Phoenix')
REPORT_HOUR = 6  # 6:00 AM Phoenix
LATE_UNTIL_HOUR = 10  # missed 6 AM may post until 10:00, not including 10 AM
REPORT_TITLE = "Last night's guest list"
# Retired 11 PM heading; still recognized so a night already posted is not repeated.
LEGACY_REPORT_TITLES = ("Tonight's guest list",)
DEFAULT_CHANNEL = 'C0CJK07GMHV'  # #generalchat (workspace general; no #general)
CHANNEL_ENV = 'SLACK_DAILY_REPORT_CHANNEL'
DOLLAR_AMOUNT_RE = re.compile(r'\$\s*\d+(?:,\d{3})*(?:\.\d+)?')

# Short labels in Tom's numbers-post style. Keep dayclub / after-hours /
# brunch distinct from the nightclub of the same house.
SHORT_VENUES = {
    'OMNIA Nightclub': 'OMNIA',
    'Hakkasan Nightclub': 'Hakkasan',
    'TAO Nightclub': 'TAO',
    'TAO Beach Dayclub': 'TAO Beach',
    'Marquee Nightclub': 'Marquee',
    'Marquee Dayclub': 'Marquee Dayclub',
    'JEWEL Nightclub': 'JEWEL',
    'LAVO Party Brunch': 'LAVO',
    "Drai's": "Drai's",
    "Drai's After Hours": "Drai's After Hours",
    'LIV Nightclub': 'LIV',
    'LIV Beach': 'LIV Beach',
    'LIV Dayclub': 'LIV Beach',
    'Encore Beach Club': 'EBC',
    'Encore Beach Club At Night': 'EBC Night',
    'XS Nightclub': 'XS',
    'Ghostbar': 'Ghostbar',
    'ZOUK': 'ZOUK',
}


def short_venue(name):
    name = str(name or '').strip() or 'venue n/a'
    if name in SHORT_VENUES:
        return SHORT_VENUES[name]
    if name.endswith(' Nightclub'):
        return name[:-len(' Nightclub')]
    return name


def counts_of(job):
    raw = job.get('raw') if isinstance(job.get('raw'), dict) else {}

    def num(key):
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return num('female_count'), num('male_count')


def _confirmed(job):
    if (job.get('state') or '') == 'done':
        return True
    meta = job.get('metadata') if isinstance(job.get('metadata'), dict) else {}
    return bool(saved_bookings(meta))


def _venues_for_night(job, day):
    """Booked venue(s) for this date; fall back to requested venues if none."""
    meta = job.get('metadata') if isinstance(job.get('metadata'), dict) else {}
    raw = job.get('raw') if isinstance(job.get('raw'), dict) else {}
    names = []
    for rec in saved_bookings(meta):
        if str(rec.get('date') or '') != day.isoformat():
            continue
        v = str(rec.get('venue') or '').strip()
        if v and v not in names:
            names.append(v)
    if names:
        return names
    return venue_names(raw) or ['venue n/a']


def tonight_totals(jobs, today):
    """Confirmed bookings for the Phoenix date `today`, grouped by short venue.

    Venue lines count every confirmed list entry at that venue. Total counts
    each job once (a date-range or multi-venue party is not double-counted).
    """
    by_venue = {}
    total_jobs = total_f = total_m = 0
    seen = set()
    for i, j in enumerate(jobs or []):
        if not _confirmed(j):
            continue
        p = project(j)
        if not on_date(p, today):
            continue
        f, m = counts_of(j)
        ref = str(j.get('ref') or '') or f'#{i}'
        if ref not in seen:
            seen.add(ref)
            total_jobs += 1
            total_f += f
            total_m += m
        for v in _venues_for_night(j, today):
            short = short_venue(v)
            slot = by_venue.setdefault(short, {'jobs': 0, 'female': 0, 'male': 0})
            slot['jobs'] += 1
            slot['female'] += f
            slot['male'] += m
    ordered = OrderedDict(sorted(by_venue.items(), key=lambda kv: (-kv[1]['jobs'], kv[0].lower())))
    return {'date': today.isoformat(), 'venues': ordered,
            'jobs': total_jobs, 'female': total_f, 'male': total_m}


def build(jobs, status=None, now=None, night=None):
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(now, PHOENIX)
    night = night or when.date()
    return {'date': night.isoformat(), 'when': when, 'night': night,
            'tonight': tonight_totals(jobs, night)}


def _fmt_party(f, m):
    return f'{int(f)}F/{int(m)}M'


def _fmt_report_day(d):
    return d.strftime('%a %-m/%-d')


def render(data, *, test=False):
    night = data.get('night') or data['when'].date()
    header = f"*{REPORT_TITLE} — {_fmt_report_day(night)}*"
    if test:
        header = 'TEST — ' + header
    t = data['tonight']
    lines = [header, '', 'Venue: Signups | Girls/Guys', '']
    if not t['venues']:
        lines.append('None confirmed.')
    else:
        for venue, slot in t['venues'].items():
            lines.append(f"{venue}: {slot['jobs']} | {_fmt_party(slot['female'], slot['male'])}")
    lines.append('')
    lines.append(f"Total: {t['jobs']} | {_fmt_party(t['female'], t['male'])}")
    return '\n'.join(lines).rstrip() + '\n'


def sanitize(text):
    """Last-line defense for secrets, emails, and dollar amounts.

    Do not run the phone scrubber on the assembled report: ISO dates in
    reasons (2026-12-25) and hex job refs with long digit runs
    (1a1d718786368942) look like phone fragments. Reasons are already
    passed through short_reason/scrub when each job is projected.
    """
    text = SECRET_RE.sub('[hidden]', str(text or ''))
    text = SECRET_URL_RE.sub('[hidden]', text)
    text = EMAIL_RE.sub('[email]', text)
    text = DOLLAR_AMOUNT_RE.sub('[amount]', text)
    return text


def report_night(now):
    """Phoenix night this tick may post, or None if outside the 6 AM–10 AM window.

    6:00 AM–9:59 AM: the previous calendar evening (the night that just wrapped,
    including that date's dayclubs). 10:00 AM through 5:59 AM: do not post.
    """
    local = datetime.fromtimestamp(now, PHOENIX)
    if REPORT_HOUR <= local.hour < LATE_UNTIL_HOUR:
        return local.date() - timedelta(days=1)
    return None


def due(now, last_posted_night):
    """True in the 6 AM–10 AM window when that night's report has not posted.

    last_posted_night is the evening's date (YYYY-MM-DD). The retired 10 AM
    last_posted_date field is not consulted. A night already recorded from
    the retired 11 PM post also counts as posted.
    """
    night = report_night(now)
    if night is None:
        return False
    return last_posted_night != night.isoformat()


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def default_state_path():
    return os.environ.get('SLACK_DAILY_REPORT_STATE') or '/data/slack-worker/daily-report.json'


def report_channel():
    """Configurable destination; default #generalchat. Not the bookings channel."""
    raw = (os.environ.get(CHANNEL_ENV) or DEFAULT_CHANNEL).strip()
    return raw or DEFAULT_CHANNEL


def make_channel_poster(client, channel=None):
    """Post to the report channel, joining a public channel if the bot is not in it."""
    target = channel or report_channel()

    def post(text):
        try:
            client.chat_postMessage(channel=target, text=text, unfurl_links=False, unfurl_media=False)
        except Exception as exc:
            data = getattr(getattr(exc, 'response', None), 'data', None) or {}
            err = data.get('error') if isinstance(data, dict) else ''
            if err != 'not_in_channel':
                raise
            client.conversations_join(channel=target)
            client.chat_postMessage(channel=target, text=text, unfurl_links=False, unfurl_media=False)

    return post


def history_has_today(client, channel, today, now=None):
    """True if the report channel already has this night's official report. Fail-open to False.

    Messages prefixed with TEST — are previews and do not count, so a test post
    never blocks the scheduled 6:00 AM run. Official posts under the retired
    11 PM title also count, so a night already reported is not posted again.
    """
    if client is None or not channel:
        return False
    start = datetime(today.year, today.month, today.day, tzinfo=PHOENIX).timestamp()
    try:
        resp = client.conversations_history(channel=channel, oldest=str(start),
                                            latest=str((now or time.time()) + 1), limit=200)
    except Exception as exc:  # noqa: BLE001
        log.info('daily report history check skipped: %s', type(exc).__name__)
        return False
    for msg in (resp or {}).get('messages') or []:
        text = str(msg.get('text') or '').lstrip()
        if text.startswith('TEST —') or text.startswith('TEST -'):
            continue
        titles = (REPORT_TITLE.strip('*'),) + LEGACY_REPORT_TITLES
        if any(title in text[:120] for title in titles):
            return True
    return False


class DailyReporter:
    def __init__(self, api, post, state_path=None, clock=time.time,
                 channel=None, history_client=None):
        self.api = api
        self.post = post
        self.clock = clock
        self.path = Path(state_path or default_state_path())
        self.channel = channel or report_channel()
        self.history_client = history_client

    def _already_posted(self, night, now):
        state = load_state(self.path)
        if state.get('last_posted_night') == night.isoformat():
            return True
        if history_has_today(self.history_client, self.channel, night, now):
            # Persist so later ticks do not keep hitting history.
            state['last_posted_night'] = night.isoformat()
            state['last_posted_via'] = 'channel-history'
            try:
                save_state(self.path, state)
            except OSError as exc:
                log.warning('could not save daily report state: %s', type(exc).__name__)
            return True
        return False

    def tick(self):
        """Build and post last night's report if due. Returns the text posted, or None."""
        now = self.clock()
        night = report_night(now)
        if night is None or not due(now, load_state(self.path).get('last_posted_night')):
            return None
        if self._already_posted(night, now):
            return None
        try:
            jobs = self.api.jobs(limit=1000)
            status = self.api.status()
        except Exception as exc:  # noqa: BLE001
            log.warning('daily report skipped; AMY API unavailable: %s', type(exc).__name__)
            return None
        text = sanitize(render(build(jobs, status, now, night=night)))
        if not text.strip():
            return None
        try:
            self.post(text)
        except Exception:
            log.exception('daily report Slack post failed')
            return None
        try:
            save_state(self.path, {'last_posted_night': night.isoformat(),
                                   'last_posted_ts': now})
        except OSError as exc:
            log.warning('daily report posted but state not saved: %s', type(exc).__name__)
        log.info('posted daily report for %s (%s chars)', night.isoformat(), len(text))
        return text

    def run_forever(self, interval=None, sleep=time.sleep):
        interval = interval or int(os.environ.get('SLACK_DAILY_REPORT_POLL_SECONDS',
                                                  os.environ.get('SLACK_POLL_SECONDS', '60')))
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception('daily report tick crashed; continuing')
            sleep(interval)


__all__ = ['DailyReporter', 'build', 'render', 'sanitize', 'due', 'short_venue',
           'tonight_totals', 'REPORT_TITLE', 'DEFAULT_CHANNEL', 'report_channel',
           'make_channel_poster', 'report_night']

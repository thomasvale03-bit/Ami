"""Daily 10 AM MST operations report posted to #generalchat.

Built only from real AMY queue data (GET /queue/status and /queue/jobs).
Never includes pay/commission figures, guest emails, or phone numbers.
Check-ins are not inventable from AMY data, so the line is always
"Check-ins: not tracked yet".

Posts at most once per America/Phoenix calendar day. The last-posted date
is saved to a dedicated state file so restarts do not double-post. An
optional channel-history check is a second guard if that file is missing.
A missed 10:00 slot still posts later the same day; nothing posts before 10:00.
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

from .views import (ATTENTION, EMAIL_RE, IN_FLIGHT, SECRET_RE, SECRET_URL_RE,
                    fmt_day, guest_name, nights, on_date, project, saved_bookings,
                    venues as venue_names)

log = logging.getLogger('amy.slack.daily_report')
PHOENIX = ZoneInfo('America/Phoenix')
REPORT_HOUR = 10
REPORT_TITLE = '*Playmaker daily report*'
# Intentionally parked manual jobs Tom asked not to treat as open holds.
PARKED_REFS = frozenset({
    '1a1f476cef115d1c',  # Sydney Stevens, Marquee Dayclub Dec 21 — cancelled / parked
})
VALERIE_NYE_DATES = frozenset({'2026-12-31', '2027-01-01'})
SYDNEY_DEC21 = ('sydney stevens', '2026-12-21')
PENDING_STATES = frozenset({'queued', 'retry'})
CLAIMED_STATES = IN_FLIGHT  # assigned, claimed, running, awaiting_human_verification
MANUAL_STATES = ATTENTION  # manual, needs_review, submission_uncertain
FAILED_STATES = frozenset({'failed'})
HOLD_LIMIT = 15
UPCOMING_DAYS = 7
DEFAULT_CHANNEL = 'C0CJK07GMHV'  # #generalchat (workspace general; no #general)
CHANNEL_ENV = 'SLACK_DAILY_REPORT_CHANNEL'
DOLLAR_AMOUNT_RE = re.compile(r'\$\s*\d+(?:,\d{3})*(?:\.\d+)?')


def public_name(name):
    """First name + last initial, or the single given name. Never contact details."""
    parts = [p for p in str(name or '').strip().split() if p]
    if not parts:
        return 'Unknown'
    if len(parts) == 1:
        return parts[0]
    for last in reversed(parts[1:]):
        initial = next((ch.upper() for ch in last if ch.isalpha()), None)
        if initial:
            return f'{parts[0]} {initial}.'
    return parts[0]


def counts_of(job):
    raw = job.get('raw') if isinstance(job.get('raw'), dict) else {}

    def num(key):
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return num('female_count'), num('male_count')


def _norm_name(name):
    return ' '.join(str(name or '').lower().split())


def _date_set(start, end):
    if start is None:
        return set()
    end = end or start
    out, cur = set(), start
    # Cap a wild range so a bad payload cannot explode the report.
    for _ in range(min(32, (end - start).days + 1)):
        out.add(cur.isoformat())
        cur += timedelta(days=1)
    return out


def is_parked(job):
    """True for intentional parks (Valerie NYE, Sydney Stevens Dec 21)."""
    ref = str(job.get('ref') or '')
    if ref in PARKED_REFS:
        return True
    raw = job.get('raw') if isinstance(job.get('raw'), dict) else {}
    name = _norm_name(guest_name(raw) if raw else job.get('guest'))
    start, end = nights(raw) if raw else (None, None)
    days = _date_set(start, end)
    first = name.split(' ')[0] if name else ''
    if first == 'valerie' and days & VALERIE_NYE_DATES:
        return True
    if name == SYDNEY_DEC21[0] and SYDNEY_DEC21[1] in days:
        return True
    return False


def _ts_in_window(ts, now, seconds=86400):
    try:
        return (now - seconds) <= float(ts) <= now + 60
    except (TypeError, ValueError):
        return False


def queue_counts(jobs, status=None):
    counts = {}
    for j in jobs or []:
        state = j.get('state') or 'unknown'
        counts[state] = counts.get(state, 0) + 1
    for state, n in ((status or {}).get('counts') or {}).items():
        try:
            counts[state] = max(counts.get(state, 0), int(n))
        except (TypeError, ValueError):
            continue

    def bucket(states):
        return sum(counts.get(s, 0) for s in states)

    return {
        'pending': bucket(PENDING_STATES),
        'claimed': bucket(CLAIMED_STATES),
        'manual': bucket(MANUAL_STATES),
        'failed': bucket(FAILED_STATES),
        'raw': counts,
    }


def completed_last_24h(jobs, now):
    """Done jobs whose last update is in the past 24 hours, grouped by venue."""
    by_venue = OrderedDict()
    total_f = total_m = total_jobs = 0
    rows = []
    for j in jobs or []:
        if (j.get('state') or '') != 'done' or not _ts_in_window(j.get('updated'), now):
            continue
        raw = j.get('raw') if isinstance(j.get('raw'), dict) else {}
        f, m = counts_of(j)
        name = public_name(guest_name(raw))
        meta = j.get('metadata') if isinstance(j.get('metadata'), dict) else {}
        booked = saved_bookings(meta)
        names = []
        for rec in booked:
            v = str(rec.get('venue') or '').strip()
            if v and v not in names:
                names.append(v)
        if not names:
            names = venue_names(raw) or ['venue n/a']
        rows.append({'guest': name, 'female': f, 'male': m, 'venues': names,
                     'dates': project(j)['dates'], 'ref': j.get('ref') or ''})
        total_f += f
        total_m += m
        total_jobs += 1
        for v in names:
            slot = by_venue.setdefault(v, {'jobs': 0, 'female': 0, 'male': 0})
            slot['jobs'] += 1
            slot['female'] += f
            slot['male'] += m
    return {'by_venue': by_venue, 'jobs': total_jobs, 'female': total_f, 'male': total_m, 'rows': rows}


def signups_last_24h(jobs, now, status=None):
    """New guest-list requests: queue jobs created in the last 24 hours.

    Intake receipts created in the same window are counted only when they
    have not already become a job (best-effort: compare created timestamps
    and counts; receipts have no guest name here).
    """
    fresh = [j for j in (jobs or []) if _ts_in_window(j.get('created'), now)]
    by_source = OrderedDict()
    for j in fresh:
        raw = j.get('raw') if isinstance(j.get('raw'), dict) else {}
        posh = raw.get('posh') if isinstance(raw.get('posh'), dict) else {}
        source = 'Posh' if posh.get('order_number') else (str(raw.get('source') or 'website').strip() or 'website')
        if source.lower().startswith('posh'):
            source = 'Posh'
        by_source[source] = by_source.get(source, 0) + 1
    intake_extra = 0
    for r in (status or {}).get('intake_receipts') or []:
        if not isinstance(r, dict):
            continue
        if _ts_in_window(r.get('created'), now) and (r.get('state') or '') == 'pending':
            # Pending receipts are not yet jobs; count them as additional sign-ups.
            intake_extra += 1
    return {'jobs': len(fresh), 'by_source': by_source, 'intake_pending_new': intake_extra,
            'total': len(fresh) + intake_extra}


def upcoming_7_days(jobs, today):
    """Confirmed bookings (done, or with verified booking records) for today..today+6."""
    days = [today + timedelta(days=i) for i in range(UPCOMING_DAYS)]
    out = OrderedDict((d.isoformat(), OrderedDict()) for d in days)
    for j in jobs or []:
        raw = j.get('raw') if isinstance(j.get('raw'), dict) else {}
        meta = j.get('metadata') if isinstance(j.get('metadata'), dict) else {}
        booked = saved_bookings(meta)
        confirmed = (j.get('state') or '') == 'done' or bool(booked)
        if not confirmed:
            continue
        p = project(j)
        f, m = counts_of(j)
        guest = public_name(p['guest'])
        for d in days:
            if not on_date(p, d):
                continue
            here = [b for b in booked if str(b.get('date') or '') == d.isoformat()]
            names = []
            for b in here:
                v = str(b.get('venue') or '').strip()
                if v and v not in names:
                    names.append(v)
            if not names:
                names = p['venues'] or ['venue n/a']
            for v in names:
                slot = out[d.isoformat()].setdefault(v, {'jobs': 0, 'female': 0, 'male': 0, 'guests': []})
                slot['jobs'] += 1
                slot['female'] += f
                slot['male'] += m
                slot['guests'].append(guest)
    return [{'date': k, 'label': fmt_day(datetime.fromisoformat(k).date()), 'venues': v}
            for k, v in out.items() if v]


def open_holds(jobs, today=None):
    """Current holds: last night is today (Phoenix) or later. Parked jobs and
    older past-date manuals are counted but not listed."""
    if today is None:
        today = datetime.fromtimestamp(time.time(), PHOENIX).date()
    today_s = today.isoformat()
    holds = []
    parked = 0
    older = 0
    for j in jobs or []:
        if (j.get('state') or '') not in MANUAL_STATES:
            continue
        if is_parked(j):
            parked += 1
            continue
        p = project(j)
        last = p['end'] or p['start']
        if last and last < today_s:
            older += 1
            continue
        holds.append({
            'guest': public_name(p['guest']),
            'venues': p['venues'],
            'dates': p['dates'],
            'start': p['start'] or '9999-99-99',
            'party': p['party'],
            'state': p['state'],
            'reason': p['reason'],
            'ref': p['ref'],
        })
    holds.sort(key=lambda h: (h['start'], h['guest']))
    return {'holds': holds, 'parked': parked, 'older': older}


def build(jobs, status=None, now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, PHOENIX).date()
    when = datetime.fromtimestamp(now, PHOENIX)
    completed = completed_last_24h(jobs, now)
    signups = signups_last_24h(jobs, now, status)
    upcoming = upcoming_7_days(jobs, today)
    queue = queue_counts(jobs, status)
    holds = open_holds(jobs, today)
    return {
        'date': today.isoformat(),
        'when': when,
        'completed': completed,
        'signups': signups,
        'upcoming': upcoming,
        'queue': queue,
        'holds': holds,
    }


def _fmt_party(f, m):
    return f'{int(f)}F/{int(m)}M'


def render(data, *, test=False):
    when = data['when']
    header = f"{REPORT_TITLE} — {when.strftime('%a %b %-d')}, {when.strftime('%-I:%M %p')} MST"
    if test:
        header = 'TEST — ' + header
    lines = [header, '']

    c = data['completed']
    lines.append(f"*Booked in the last 24 hours:* {c['jobs']} "
                 f"({_fmt_party(c['female'], c['male'])})")
    if not c['by_venue']:
        lines.append('• none')
    else:
        for venue, slot in c['by_venue'].items():
            lines.append(f"• {venue} — {slot['jobs']} — {_fmt_party(slot['female'], slot['male'])}")
    lines.append('')

    s = data['signups']
    signup_line = f"*New sign-ups received:* {s['total']}"
    parts = [f"{n} {src}" for src, n in s['by_source'].items()]
    if s.get('intake_pending_new'):
        parts.append(f"{s['intake_pending_new']} intake pending")
    if parts:
        signup_line += ' (' + ', '.join(parts) + ')'
    lines.append(signup_line)
    lines.append('')

    lines.append('*Upcoming bookings (next 7 days):*')
    upcoming = data['upcoming']
    if not upcoming:
        lines.append('• none')
    else:
        for day in upcoming:
            lines.append(day['label'])
            for venue, slot in day['venues'].items():
                guests = ', '.join(slot['guests'][:12])
                extra = f" +{len(slot['guests']) - 12} more" if len(slot['guests']) > 12 else ''
                lines.append(f"  • {venue} — {slot['jobs']} — {_fmt_party(slot['female'], slot['male'])}"
                             + (f" — {guests}{extra}" if guests else ''))
    lines.append('')

    q = data['queue']
    lines.append(f"*Queue:* pending {q['pending']}, claimed {q['claimed']}, "
                 f"manual {q['manual']}, failed {q['failed']}")
    lines.append('')

    h = data['holds']
    open_n = len(h['holds'])
    title = f"*Open holds needing Tom:* {open_n}"
    extras = []
    if h.get('parked'):
        extras.append(f"{h['parked']} intentionally parked, not listed")
    if h.get('older'):
        extras.append(f"+{h['older']} older past-date manual jobs not shown")
    if extras:
        title += ' (' + ', '.join(extras) + ')'
    lines.append(title)
    if not h['holds']:
        lines.append('• none')
    else:
        for item in h['holds'][:HOLD_LIMIT]:
            venue = ', '.join(item['venues']) or 'no venue'
            reason = item['reason']
            reason_bit = f" — {reason}" if reason else ''
            lines.append(f"• {item['guest']} — {venue} — {item['dates']} — {item['party']}"
                         f"{reason_bit}  (ref {item['ref']})")
        leftover = open_n - HOLD_LIMIT
        if leftover > 0:
            lines.append(f'• …and {leftover} more')
    lines.append('')
    lines.append('Check-ins: not tracked yet')
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


def due(now, last_posted_date):
    """True when it is 10:00 AM MST or later and today's report has not posted."""
    local = datetime.fromtimestamp(now, PHOENIX)
    if local.hour < REPORT_HOUR:
        return False
    return last_posted_date != local.date().isoformat()


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
    """True if the report channel already has today's official report. Fail-open to False.

    Messages prefixed with TEST — are previews and do not count, so a test post
    never blocks the scheduled 10:00 AM run.
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
        if REPORT_TITLE.strip('*') in text[:120]:
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

    def _already_posted(self, today, now):
        state = load_state(self.path)
        if state.get('last_posted_date') == today.isoformat():
            return True
        if history_has_today(self.history_client, self.channel, today, now):
            # Persist so later ticks do not keep hitting history.
            state['last_posted_date'] = today.isoformat()
            state['last_posted_via'] = 'channel-history'
            try:
                save_state(self.path, state)
            except OSError as exc:
                log.warning('could not save daily report state: %s', type(exc).__name__)
            return True
        return False

    def tick(self):
        """Build and post today's report if due. Returns the text posted, or None."""
        now = self.clock()
        local = datetime.fromtimestamp(now, PHOENIX)
        today = local.date()
        if not due(now, load_state(self.path).get('last_posted_date')):
            return None
        if self._already_posted(today, now):
            return None
        try:
            jobs = self.api.jobs(limit=1000)
            status = self.api.status()
        except Exception as exc:  # noqa: BLE001
            log.warning('daily report skipped; AMY API unavailable: %s', type(exc).__name__)
            return None
        text = sanitize(render(build(jobs, status, now)))
        if not text.strip():
            return None
        try:
            self.post(text)
        except Exception:
            log.exception('daily report Slack post failed')
            return None
        try:
            save_state(self.path, {'last_posted_date': today.isoformat(),
                                   'last_posted_ts': now})
        except OSError as exc:
            log.warning('daily report posted but state not saved: %s', type(exc).__name__)
        log.info('posted daily report for %s (%s chars)', today.isoformat(), len(text))
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


__all__ = ['DailyReporter', 'build', 'render', 'sanitize', 'due', 'is_parked',
           'public_name', 'queue_counts', 'PARKED_REFS', 'REPORT_TITLE',
           'DEFAULT_CHANNEL', 'report_channel', 'make_channel_poster']

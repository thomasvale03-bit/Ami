"""Pure projections and Slack text for AMY queue jobs (read-only).

Never outputs phone numbers, emails, claim tokens or raw payloads. Reasons are
scrubbed of anything that looks like a phone number before display.
"""
from datetime import date, datetime, timedelta
import re
import time
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo('America/Phoenix')
ALL_STATES = ('queued', 'retry', 'claimed', 'running', 'assigned', 'awaiting_human_verification',
              'submission_uncertain', 'needs_review', 'manual', 'done')
IN_FLIGHT = frozenset({'assigned', 'claimed', 'running', 'awaiting_human_verification'})
ATTENTION = frozenset({'manual', 'needs_review', 'submission_uncertain'})
PHONE_RE = re.compile(r'(?:\+?\(?\d[\d \t().-]{6,}\d)')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
SECRET_RE = re.compile(r'(?:xox[abposr]-|xapp-|sk-|Bearer\s+)[\w-]+', re.I)
SECRET_URL_RE = re.compile(
    r'https?://[^\s<>]+[?&#](?:access_token|api[_-]?key|token|key|secret|password|auth|dashboard[_-]?key)=[^\s<>&]+',
    re.I,
)
ASSIGNED_SECRET_RE = re.compile(
    r'\b(?:api[_-]?key|access_token|secret|password|dashboard[_-]?key|OPENAI_API_KEY|AMY_QUEUE_API_TOKEN|SLACK_(?:BOT|APP)_TOKEN)\s*[:=]\s*\S+',
    re.I,
)


def scrub(text, limit=300):
    text = SECRET_RE.sub('[hidden]', str(text or ''))
    text = SECRET_URL_RE.sub('[hidden]', str(text or ''))
    text = ASSIGNED_SECRET_RE.sub('[hidden]', text)
    text = EMAIL_RE.sub('[email]', text)
    text = PHONE_RE.sub(lambda m: m.group(0) if _not_phone(m.group(0)) else '[phone]', text)
    text = '\n'.join(' '.join(line.split()) for line in text.splitlines()).strip()
    return text if len(text) <= limit else text[:limit - 1] + '\u2026'


def _not_phone(s):
    digits = re.sub(r'\D', '', s)
    if len(digits) < 10:
        return True
    # ISO dates / date ranges / UUID-ish order fragments are not phones.
    return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:\s*[-\u2013]\s*\d{4}-\d{2}-\d{2})?', s.strip()))


def guest_name(raw):
    name = str(raw.get('name') or '').strip()
    if name:
        return name
    return ' '.join(str(x) for x in (raw.get('first_name'), raw.get('last_name')) if x).strip() or 'Unknown'


def nights(raw):
    try:
        start = date.fromisoformat(str(raw.get('start_date') or ''))
    except ValueError:
        return None, None
    try:
        end = date.fromisoformat(str(raw.get('end_date') or start.isoformat()))
    except ValueError:
        end = start
    return start, max(start, end)


def fmt_day(d):
    return d.strftime('%a %b %-d') if d else '?'


def fmt_dates(raw):
    start, end = nights(raw)
    if start is None:
        return 'no date'
    return fmt_day(start) if end == start else f'{fmt_day(start)}\u2013{fmt_day(end)}'


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(float(ts), PHOENIX).strftime('%b %-d %-I:%M %p MST')
    except (TypeError, ValueError, OSError):
        return '?'


def fmt_ago(ts, now):
    try:
        secs = max(0, int(now - float(ts)))
    except (TypeError, ValueError):
        return '?'
    if secs < 90:
        return f'{secs}s ago'
    mins = secs // 60
    if mins < 120:
        return f'{mins}m ago'
    hours = mins // 60
    return f'{hours}h {mins % 60}m ago' if hours < 48 else f'{hours // 24}d ago'


def party(raw):
    def num(key):
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return f"{num('female_count')}F/{num('male_count')}M"


def venues(raw):
    v = raw.get('venues') or []
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()]


def saved_bookings(meta):
    """Verified booking records (final evidence, refused-completion evidence,
    or per-night progress), deduped by order id. Mirrors Store.saved_bookings."""
    meta = meta if isinstance(meta, dict) else {}
    out, seen = [], set()
    sources = [(meta.get('evidence') or {}).get('bookings'),
               ((meta.get('resolution') or {}).get('evidence') or {}).get('bookings'),
               list(((meta.get('progress') or {}).get('bookings') or {}).values())]
    for records in sources:
        for rec in records or []:
            if not isinstance(rec, dict) or rec.get('verified') is not True:
                continue
            key = rec.get('order_id') or (rec.get('venue'), rec.get('date'), rec.get('system'))
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)
    return out


def short_reason(reason):
    """One readable line: drop Playwright call logs/URLs, scrub, cap length."""
    text = str(reason or '').split('Call log:')[0]
    text = re.sub(r'https?://\S+', '[link]', text)
    return scrub(' '.join(text.split()), 220)


def short_ref(order_id):
    s = str(order_id or '').strip()
    return s if len(s) <= 10 else s[:8]


def project(job):
    """Safe, display-only view of one job (no phone/email/raw payload)."""
    raw = job.get('raw') if isinstance(job.get('raw'), dict) else {}
    meta = job.get('metadata') if isinstance(job.get('metadata'), dict) else {}
    start, end = nights(raw)
    posh = raw.get('posh') if isinstance(raw.get('posh'), dict) else {}
    booked = []
    for rec in saved_bookings(meta):
        booked.append({'venue': str(rec.get('venue') or '').strip() or '?', 'date': str(rec.get('date') or ''),
                       'order': short_ref(rec.get('order_id')) or ('email-only' if rec.get('email_only') else '')})
    return {
        'ref': str(job.get('ref') or ''),
        'guest': guest_name(raw),
        'state': str(job.get('state') or 'unknown'),
        'venues': venues(raw),
        'dates': fmt_dates(raw),
        'start': start.isoformat() if start else None,
        'end': end.isoformat() if end else None,
        'party': party(raw),
        'source': ('Posh #' + str(posh.get('order_number'))) if posh.get('order_number') else str(raw.get('source') or 'website'),
        'reason': short_reason(job.get('reason')),
        'booked': booked,
        'updated': job.get('updated'),
        'created': job.get('created'),
        'attempts': job.get('attempts'),
    }


def job_line(p, *, with_reason=True):
    venue = ', '.join(p['venues']) or 'no venue'
    line = f"*{p['guest']}* \u2014 {venue} \u2014 {p['dates']} \u2014 {p['party']} \u2014 `{p['state']}`"
    if p['booked']:
        line += ' \u2014 booked: ' + '; '.join(
            f"{b['venue']} {b['date']}" + (f" (#{b['order']})" if b['order'] else '') for b in p['booked'])
    if with_reason and p['reason'] and p['state'] != 'done':
        line += f"\n    _{p['reason']}_"
    return line + f"  (ref {p['ref']})"


def on_date(p, day):
    if not p['start']:
        return False
    return p['start'] <= day.isoformat() <= (p['end'] or p['start'])


def today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, PHOENIX).date()


def parse_date(text, now=None):
    """'11/21', '11/21/2026', '2026-11-21', 'today', 'tonight', 'tomorrow', weekday names."""
    t = (text or '').strip().lower()
    base = today(now)
    if t in ('', 'today', 'tonight'):
        return base
    if t == 'tomorrow':
        return base + timedelta(days=1)
    days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, d in enumerate(days):
        if t.startswith(d):
            return base + timedelta(days=(i - base.weekday()) % 7)
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r'(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?', t)
    if not m:
        return None
    month, day_, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day_)
        d = date(base.year, month, day_)
        # A bare month/day more than ~2 months in the past means next year.
        return d if d >= base - timedelta(days=60) else date(base.year + 1, month, day_)
    except ValueError:
        return None

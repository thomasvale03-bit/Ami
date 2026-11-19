"""Read-only queue dashboard for Thomas.

GET /dashboard is keyed by DASHBOARD_KEY (?key= or an HttpOnly cookie). It
renders live jobs from the queue store on every load. No booking, email,
claim, or mutation happens here. Never log the key.
"""
from datetime import date, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
import hmac
import html
import os
import re
import time
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import queue_service
from worker_store import Store

PHOENIX = ZoneInfo('America/Phoenix')
COOKIE_NAME = 'amy_dashboard'
MIN_KEY_LEN = 32
COOKIE_MAX_AGE = 15552000  # 180 days
KNOWN_STATES = (
    'queued', 'pending', 'retry', 'claimed', 'running', 'assigned',
    'awaiting_human_verification', 'submission_uncertain', 'needs_review',
    'manual', 'done',
)
ALWAYS_ATTENTION = frozenset({'queued', 'submission_uncertain', 'needs_review'})
IN_FLIGHT = frozenset({'assigned', 'claimed', 'running', 'awaiting_human_verification'})
WAITING_RE = re.compile(r'(?:guest list not posted|no guest list posted)', re.I)
KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')
SECTIONS = (
    ('attention', 'Needs attention'),
    ('waiting', 'Waiting for list'),
    ('upcoming', 'Upcoming booked'),
    ('recent', 'Recent past'),
    ('other', 'Other'),
)


def configured_key():
    key = os.environ.get('DASHBOARD_KEY', '')
    return key if len(key) >= MIN_KEY_LEN and KEY_RE.fullmatch(key) else ''


def key_matches(provided, expected):
    if not expected or not provided:
        return False
    if len(provided) != len(expected):
        hmac.compare_digest(expected.encode(), expected.encode())
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def query_key(url):
    return (parse_qs(urlparse(url).query).get('key') or [''])[0]


def cookie_value(headers):
    raw = ''
    if headers is not None:
        getter = getattr(headers, 'get', None)
        if getter:
            raw = getter('Cookie') or getter('cookie') or ''
        elif isinstance(headers, dict):
            raw = headers.get('Cookie') or headers.get('cookie') or ''
    if not raw:
        return ''
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return ''
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else ''


def cookie_header(key):
    return (f'{COOKIE_NAME}={key}; Path=/dashboard; Max-Age={COOKIE_MAX_AGE}; '
            'HttpOnly; Secure; SameSite=Strict')


def guest_name(raw):
    name = (raw.get('name') or '').strip()
    if name:
        return name
    return ' '.join(x for x in (raw.get('first_name'), raw.get('last_name')) if x).strip() or 'Unknown'


def truncate_email(email):
    email = (email or '').strip()
    if not email:
        return ''
    if len(email) <= 24:
        return email
    local, _, domain = email.partition('@')
    if not domain:
        return email[:18] + '\u2026'
    local_show = local if len(local) <= 10 else local[:8] + '\u2026'
    domain_show = domain if len(domain) <= 12 else domain[:8] + '\u2026'
    return f'{local_show}@{domain_show}'


def source_label(raw):
    posh = raw.get('posh') if isinstance(raw.get('posh'), dict) else {}
    order = str(posh.get('order_number') or '').strip()
    src = str(raw.get('source') or '').strip()
    if order or src.lower() == 'posh':
        return f'Posh #{order}' if order else 'Posh'
    return 'website'


def short_order(order_id):
    text = str(order_id or '').strip()
    if not text:
        return ''
    return text if len(text) <= 10 else text[:8]


def night_range(raw):
    try:
        start = date.fromisoformat(str(raw.get('start_date') or ''))
    except ValueError:
        return None, None
    try:
        end = date.fromisoformat(str(raw.get('end_date') or start.isoformat()))
    except ValueError:
        end = start
    if end < start:
        end = start
    return start, end


def fmt_dates(start, end):
    if start is None:
        return '\u2014'
    def part(d):
        return d.strftime('%b %-d')
    if end is None or end == start:
        return part(start)
    if start.year == end.year:
        return f'{part(start)}\u2013{part(end)}'
    return f'{part(start)} {start.year}\u2013{part(end)} {end.year}'


def fmt_mst(ts):
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return '\u2014'
    dt = datetime.fromtimestamp(ts, PHOENIX)
    return dt.strftime('%b %-d, %-I:%M %p MST')


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes}m'
    hours = minutes // 60
    if hours < 48:
        rem = minutes % 60
        return f'{hours}h {rem}m' if rem else f'{hours}h'
    days = hours // 24
    rem_h = hours % 24
    return f'{days}d {rem_h}h' if rem_h else f'{days}d'


def party_label(raw):
    try:
        female = int(raw.get('female_count') or 0)
    except (TypeError, ValueError):
        female = 0
    try:
        male = int(raw.get('male_count') or 0)
    except (TypeError, ValueError):
        male = 0
    return f'{female}F / {male}M'


def venues_label(raw):
    venues = raw.get('venues') or []
    if isinstance(venues, str):
        venues = [venues]
    names = [str(v).strip() for v in venues if str(v).strip()]
    return ', '.join(names) or '\u2014'


def booked_items(meta):
    items = []
    for rec in Store.saved_bookings(meta if isinstance(meta, dict) else {}):
        items.append({
            'venue': str(rec.get('venue') or '').strip() or '\u2014',
            'date': str(rec.get('date') or '').strip(),
            'order': short_order(rec.get('order_id')),
            'fallback': bool(rec.get('fallback')),
        })
    return items


def waiting_for_list(state, reason):
    return state in ('retry', 'manual') and bool(WAITING_RE.search(reason or ''))


def stale_lease(row, now):
    if row.get('state') not in IN_FLIGHT:
        return False
    lease = row.get('lease_until')
    try:
        return lease is None or float(lease) <= now
    except (TypeError, ValueError):
        return True


def past_due(row, now):
    try:
        return float(row.get('due') or 0) <= now
    except (TypeError, ValueError):
        return True


def classify(row, now, today):
    state = row.get('state') or ''
    reason = row.get('reason') or ''
    raw = row.get('raw') or {}
    start, end = night_range(raw)
    if waiting_for_list(state, reason):
        return 'waiting'
    if state in ALWAYS_ATTENTION:
        return 'attention'
    if state in ('pending', 'retry') and past_due(row, now):
        return 'attention'
    if stale_lease(row, now):
        return 'attention'
    if state == 'manual' and end and end >= today:
        return 'attention'
    if state == 'done' and end and end >= today:
        return 'upcoming'
    if end and today - timedelta(days=7) <= end < today:
        return 'recent'
    if state == 'done' and end is None:
        try:
            if now - float(row.get('updated') or 0) <= 7 * 86400:
                return 'recent'
        except (TypeError, ValueError):
            pass
    return 'other'


def project_row(row, now, today):
    raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
    meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    start, end = night_range(raw)
    state = row.get('state') or ''
    created = float(row.get('created') or 0)
    updated = float(row.get('updated') or created)
    if state == 'done':
        timing_label, timing = 'Booked in', fmt_duration(updated - created)
    else:
        timing_label, timing = 'Sitting', fmt_duration(now - created)
    booked = booked_items(meta)
    name = guest_name(raw)
    email = truncate_email(raw.get('email'))
    venues = venues_label(raw)
    dates = fmt_dates(start, end)
    party = party_label(raw)
    source = source_label(raw)
    reason = (row.get('reason') or '').strip()
    came_in = fmt_mst(created)
    section = classify(row, now, today)
    search = ' '.join([
        name, email, state, venues, dates, party, source, reason,
        ' '.join(f"{b['venue']} {b['order']}" for b in booked),
        row.get('ref') or '',
    ]).lower()
    return {
        'ref': row.get('ref') or '',
        'name': name,
        'email': email,
        'state': state,
        'venues': venues,
        'booked': booked,
        'dates': dates,
        'party': party,
        'source': source,
        'came_in': came_in,
        'timing_label': timing_label,
        'timing': timing,
        'reason': reason,
        'section': section,
        'search': search,
        'start': start,
        'created': created,
    }


def section_sort_key(row):
    start = row['start'] or date.max
    return (start, row['created'], row['name'])


def build(jobs, now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, PHOENIX).date()
    rows = [project_row(job, now, today) for job in jobs]
    counts = {}
    for job in jobs:
        state = job.get('state') or 'unknown'
        counts[state] = counts.get(state, 0) + 1
    grouped = {key: [] for key, _ in SECTIONS}
    for row in rows:
        grouped.setdefault(row['section'], []).append(row)
    for key, items in grouped.items():
        if key == 'recent':
            items.sort(key=lambda r: (r['start'] or date.min, r['created']), reverse=True)
        elif key == 'other':
            items.sort(key=lambda r: r['created'], reverse=True)
        else:
            items.sort(key=section_sort_key)
    return {
        'generated': now,
        'generated_label': fmt_mst(now),
        'today': today,
        'counts': counts,
        'total': len(jobs),
        'sections': [{'id': key, 'title': title, 'rows': grouped.get(key, [])} for key, title in SECTIONS],
        'states': list(KNOWN_STATES),
    }


def _e(value):
    return html.escape(str(value or ''), quote=True)


def booked_html(items):
    if not items:
        return '\u2014'
    parts = []
    for item in items:
        bit = _e(item['venue'])
        if item['date']:
            bit += f" {_e(item['date'])}"
        if item['order']:
            bit += f' <span class="oid">{_e(item["order"])}</span>'
        if item['fallback']:
            bit += ' <span class="fb">fallback</span>'
        parts.append(bit)
    return '<br>'.join(parts)


def error_page(code, message):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_e(message)}</title></head><body style="font-family:system-ui,'
        'sans-serif;margin:2rem;background:#111;color:#eee">'
        f'<p>{_e(code)} {_e(message)}</p></body></html>'
    )


_CSS = """
:root {
  --bg:#0e1116; --card:#171b22; --ink:#e8edf4; --muted:#9aa3b2;
  --line:#2a3140; --accent:#6ea8fe; --attn:#f0b429; --bad:#f07178;
  --ok:#7fd99a; --wait:#e0a458;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
header {
  position:sticky; top:0; z-index:2; background:rgba(14,17,22,.94);
  backdrop-filter:blur(8px); border-bottom:1px solid var(--line);
  padding:12px 16px 10px;
}
h1 { font-size:1.15rem; margin:0 0 4px; }
.sub, .empty, .muted { color:var(--muted); }
.sub { font-size:.9rem; margin:0 0 10px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 10px; }
.chip {
  background:var(--card); border:1px solid var(--line); border-radius:999px;
  padding:3px 10px; font-size:.8rem; color:var(--muted);
}
.chip b { color:var(--ink); font-weight:650; }
.chip[data-state="queued"] b, .chip[data-state="submission_uncertain"] b,
.chip[data-state="needs_review"] b, .chip[data-state="manual"] b { color:var(--attn); }
.chip[data-state="done"] b { color:var(--ok); }
.chip[data-state="retry"] b { color:var(--wait); }
.filters { display:flex; gap:8px; flex-wrap:wrap; }
.filters input, .filters select {
  flex:1; min-width:140px; background:var(--card); color:var(--ink);
  border:1px solid var(--line); border-radius:8px; padding:8px 10px; font:inherit;
}
main { padding:12px 16px 48px; display:flex; flex-direction:column; gap:18px; }
h2 { font-size:1rem; margin:0 0 8px; }
h2 .n { color:var(--muted); font-weight:600; }
.jobs { display:flex; flex-direction:column; gap:8px; }
.job {
  background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:12px 14px;
}
.job[data-section="attention"] { border-color:#5a4818; }
.job[data-section="waiting"] { border-color:#5a4020; }
.job[data-section="upcoming"] { border-color:#21553a; }
.top { display:flex; justify-content:space-between; gap:8px; align-items:baseline; }
.name { font-weight:650; }
.state {
  font-size:.75rem; font-weight:650; letter-spacing:.02em;
  background:#222834; border-radius:999px; padding:2px 8px; white-space:nowrap;
}
.state.queued, .state.pending, .state.submission_uncertain,
.state.needs_review, .state.manual { color:var(--attn); }
.state.retry { color:var(--wait); }
.state.done { color:var(--ok); }
.state.assigned, .state.claimed, .state.running,
.state.awaiting_human_verification { color:var(--bad); }
.grid { display:grid; gap:6px 14px; margin-top:8px; grid-template-columns:1fr; }
@media (min-width:700px) { .grid { grid-template-columns:1fr 1fr 1fr; } }
.k { display:block; font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.oid { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }
.fb { font-size:.75rem; color:var(--wait); }
.reason { margin-top:8px; color:var(--muted); font-size:.9rem; }
.empty { margin:0; padding:8px 0; }
"""

_JS = """
(function () {
  try {
    if (location.search.indexOf("key=") !== -1) {
      history.replaceState(null, "", location.pathname);
    }
  } catch (e) {}
  var q = document.getElementById("q");
  var st = document.getElementById("st");
  function norm(s) { return (s || "").toLowerCase(); }
  function apply() {
    var query = norm(q.value);
    var state = st.value;
    document.querySelectorAll(".job").forEach(function (el) {
      var ok = (!query || (el.getAttribute("data-search") || "").indexOf(query) !== -1)
        && (!state || el.getAttribute("data-state") === state);
      el.style.display = ok ? "" : "none";
    });
    document.querySelectorAll(".sec").forEach(function (sec) {
      var n = 0;
      sec.querySelectorAll(".job").forEach(function (el) {
        if (el.style.display !== "none") n++;
      });
      var badge = sec.querySelector("[data-count-for]");
      if (badge) badge.textContent = n;
    });
  }
  q.addEventListener("input", apply);
  st.addEventListener("change", apply);
})();
"""


def render(data):
    chips = []
    seen = set()
    for state in data['states']:
        seen.add(state)
        chips.append(_chip(state, data['counts'].get(state, 0)))
    for state in sorted(data['counts']):
        if state not in seen:
            chips.append(_chip(state, data['counts'][state]))
    options = ['<option value="">All states</option>']
    for state in data['states']:
        options.append(f'<option value="{_e(state)}">{_e(state)}</option>')
    for state in sorted(data['counts']):
        if state not in data['states']:
            options.append(f'<option value="{_e(state)}">{_e(state)}</option>')
    sections = []
    for section in data['sections']:
        rows = section['rows']
        body = ''.join(_job_card(r) for r in rows) or '<p class="empty">None right now.</p>'
        sections.append(
            f'<section class="sec" data-section="{_e(section["id"])}">'
            f'<h2>{_e(section["title"])} <span class="n" data-count-for="{_e(section["id"])}">'
            f'{len(rows)}</span></h2><div class="jobs">{body}</div></section>'
        )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta http-equiv="refresh" content="60">\n'
        '<title>AMY Queue</title>\n'
        f'<style>{_CSS}</style>\n</head>\n<body>\n<header>\n'
        '<h1>AMY Queue</h1>\n'
        f'<p class="sub">Updated {_e(data["generated_label"])} · refreshes every 60 seconds · {_e(data["total"])} jobs</p>\n'
        f'<div class="chips">{"".join(chips)}</div>\n'
        '<div class="filters">\n'
        '<input type="search" id="q" placeholder="Search guests, venues, orders\u2026" aria-label="Search">\n'
        f'<select id="st" aria-label="Filter by state">{"".join(options)}</select>\n'
        '</div>\n</header>\n<main>\n'
        f'{"".join(sections)}\n'
        f'</main>\n<script>{_JS}</script>\n</body>\n</html>\n'
    )


def _chip(state, count):
    return f'<span class="chip" data-state="{_e(state)}">{_e(state)} <b>{count}</b></span>'


def _job_card(row):
    email = f'<div class="muted">{_e(row["email"])}</div>' if row['email'] else ''
    reason = f'<div class="reason">{_e(row["reason"])}</div>' if row['reason'] else ''
    return (
        f'<article class="job" data-section="{_e(row["section"])}" data-state="{_e(row["state"])}" '
        f'data-search="{_e(row["search"])}">'
        f'<div class="top"><div><div class="name">{_e(row["name"])}</div>{email}</div>'
        f'<span class="state {_e(row["state"])}">{_e(row["state"])}</span></div>'
        f'<div class="grid">'
        f'<div><span class="k">Requested</span>{_e(row["venues"])}</div>'
        f'<div><span class="k">Booked</span>{booked_html(row["booked"])}</div>'
        f'<div><span class="k">Dates</span>{_e(row["dates"])}</div>'
        f'<div><span class="k">Party</span>{_e(row["party"])}</div>'
        f'<div><span class="k">Source</span>{_e(row["source"])}</div>'
        f'<div><span class="k">Came in</span>{_e(row["came_in"])}</div>'
        f'<div><span class="k">{_e(row["timing_label"])}</span>{_e(row["timing"])}</div>'
        f'</div>{reason}</article>'
    )


def handle(method, url, headers, *, s=None, now=None):
    """Return (status, content_type, body, extra_headers). Never logs the key."""
    if method != 'GET':
        return 405, 'text/plain; charset=utf-8', 'method not allowed', {}
    expected = configured_key()
    if not expected:
        return 503, 'text/html; charset=utf-8', error_page(503, 'Dashboard is not configured'), {}
    provided_q = query_key(url)
    provided_c = cookie_value(headers)
    ok = key_matches(provided_q, expected) or key_matches(provided_c, expected)
    if not ok:
        return 401, 'text/html; charset=utf-8', error_page(401, 'Unauthorized'), {}
    extra = {}
    if key_matches(provided_q, expected):
        extra['Set-Cookie'] = cookie_header(expected)
    now = time.time() if now is None else now
    s = s or queue_service.store()
    return 200, 'text/html; charset=utf-8', render(build(s.all_jobs(), now)), extra

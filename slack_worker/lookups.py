"""Read-only lookups used by keyword commands and by the OpenAI tool calls.

Each lookup returns a JSON-safe dict (for the model) and has a text renderer
(for keyword replies). No booking, cancelling, requeueing or email here.
"""
from collections import OrderedDict
import time

from . import health
from .views import (ALL_STATES, ATTENTION, fmt_ago, fmt_day, fmt_time, job_line, on_date, parse_date,
                    project, today)

MAX_LINES = 25


class Lookups:
    def __init__(self, api, clock=time.time):
        self.api = api
        self.clock = clock

    # ---- data -----------------------------------------------------------------
    def queue_status(self):
        status = self.api.status()
        jobs = self.api.jobs()
        counts = OrderedDict((s, 0) for s in ALL_STATES)
        for j in jobs:
            counts[j.get('state') or 'unknown'] = counts.get(j.get('state') or 'unknown', 0) + 1
        for state, n in (status.get('counts') or {}).items():
            counts[state] = max(counts.get(state, 0), int(n))
        receipts = status.get('intake_receipts') or []
        pending = sum(1 for r in receipts if isinstance(r, dict) and r.get('state') == 'pending')
        return {'counts': dict(counts), 'execution': status.get('execution'),
                'claims_paused': bool(status.get('claims_paused')),
                'intake_pending': pending, 'total': sum(counts.values())}

    def jobs_in_state(self, state, limit=15):
        state = (state or '').strip().lower()
        jobs = [project(j) for j in self.api.jobs() if (j.get('state') or '') == state]
        jobs.sort(key=lambda p: (p['start'] or '9999', p['guest']))
        return {'state': state, 'count': len(jobs), 'jobs': jobs[:max(1, min(int(limit), 50))]}

    def guest(self, name):
        q = ' '.join((name or '').lower().split())
        if len(q) < 2:
            return {'query': name, 'jobs': [], 'error': 'name too short'}
        tokens = q.split()
        found = [project(j) for j in self.api.jobs()]
        found = [p for p in found if all(t in p['guest'].lower() for t in tokens)]
        found.sort(key=lambda p: (p['start'] or '9999', p['guest']))
        return {'query': name, 'count': len(found), 'jobs': found[:MAX_LINES]}

    def bookings_on(self, day_text='today'):
        day = parse_date(day_text, self.clock())
        if day is None:
            return {'error': f'could not read a date from {day_text!r} (try 11/21 or 2026-11-21)'}
        by_venue = OrderedDict()
        for p in (project(j) for j in self.api.jobs()):
            if not on_date(p, day):
                continue
            booked_here = [b for b in p['booked'] if b['date'] == day.isoformat()]
            names = [b['venue'] for b in booked_here] or p['venues'] or ['no venue']
            for v in names:
                by_venue.setdefault(v, []).append(p)
        for v in by_venue:
            by_venue[v].sort(key=lambda p: (p['state'] != 'done', p['guest']))
        return {'date': day.isoformat(), 'label': fmt_day(day),
                'venues': {v: items for v, items in sorted(by_venue.items())}}

    def worker_health(self):
        now = self.clock()
        status = self.api.status()
        jobs = self.api.jobs()
        h = health.assess(jobs, status, now)
        return {'execution': h['execution'], 'worker_claims': h['worker_claims'],
                'claims_paused': h['claims_paused'],
                'last_activity': fmt_time(h['last_activity']) if h['last_activity'] else None,
                'last_activity_ago': fmt_ago(h['last_activity'], now) if h['last_activity'] else None,
                'in_flight': [project(j) for j in h['in_flight']],
                'stuck': [project(j) for j in h['stuck']],
                'missed_pickup': fmt_time(h['stalled_slot']) if h['stalled_slot'] else None,
                'next_pickup': fmt_time(h['next_slot']) if h['next_slot'] else None,
                'queued_or_retry': sum(1 for j in jobs if j.get('state') in ('queued', 'retry')),
                'note': 'Booking-worker heartbeat is not exposed by the AMY API; '
                        'activity is the newest job update after a claim.'}

    # ---- text renderers -------------------------------------------------------
    @staticmethod
    def render_status(d):
        lines = ['*AMY queue* (all states):']
        for state, n in d['counts'].items():
            mark = ' :warning:' if state in ATTENTION | {'queued', 'retry'} and n else ''
            lines.append(f'\u2022 `{state}`: {n}{mark}')
        lines.append(f"Total {d['total']} \u2022 pickup: {d['execution'] or '?'}"
                     + (' \u2022 *claims paused*' if d['claims_paused'] else '')
                     + f" \u2022 intake pending: {d['intake_pending']}")
        return '\n'.join(lines)

    @staticmethod
    def render_state(d):
        if not d['count']:
            return f"No jobs in `{d['state']}`."
        out = [f"*{d['count']} job(s) in `{d['state']}`*" + (f" (first {len(d['jobs'])})" if d['count'] > len(d['jobs']) else '') + ':']
        out += [job_line(p) for p in d['jobs']]
        return '\n'.join(out)

    @staticmethod
    def render_guest(d):
        if d.get('error'):
            return f"Give me at least two letters of the guest's name (`guest <name>`)."
        if not d['count']:
            return f"No jobs found for guest \u201c{d['query']}\u201d."
        return '\n'.join([f"*{d['count']} job(s) for \u201c{d['query']}\u201d:*"] + [job_line(p) for p in d['jobs']])

    @staticmethod
    def render_date(d):
        if d.get('error'):
            return d['error']
        if not d['venues']:
            return f"No bookings or requests for {d['label']}."
        out = [f"*{d['label']}* by venue:"]
        n = 0
        for venue, items in d['venues'].items():
            out.append(f"\n*{venue}* ({len(items)})")
            for p in items:
                n += 1
                if n > 60:
                    out.append('\u2026more not shown')
                    return '\n'.join(out)
                ref = ''
                for b in p['booked']:
                    if b['venue'] == venue and b['date'] == d['date'] and b['order']:
                        ref = f" #{b['order']}"
                out.append(f"\u2022 {p['guest']} \u2014 {p['party']} \u2014 `{p['state']}`{ref}")
        return '\n'.join(out)

    @staticmethod
    def render_health(d):
        out = ['*Booking worker health*',
               f"\u2022 Pickup mode: {d['execution'] or '?'}" + (' (*claims paused*)' if d['claims_paused'] else ''),
               f"\u2022 Last activity: {d['last_activity'] or 'none seen'}"
               + (f" ({d['last_activity_ago']})" if d['last_activity_ago'] else ''),
               f"\u2022 Next scheduled pickup: {d['next_pickup'] or '?'}",
               f"\u2022 Queued/retry waiting: {d['queued_or_retry']}",
               f"\u2022 In flight: {len(d['in_flight'])}"]
        out += ['    ' + job_line(p, with_reason=False) for p in d['in_flight']]
        if d['stuck']:
            out.append(f":rotating_light: {len(d['stuck'])} in-flight job(s) past their lease / not updated")
        if d['missed_pickup']:
            out.append(f":rotating_light: Pickup due {d['missed_pickup']} didn't run (queued work waiting, no activity since)")
        out.append(f"_{d['note']}_")
        return '\n'.join(out)

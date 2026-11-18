"""Turns read-only queue polls into Slack update messages.

Posts when a job becomes done (booked), or manual / needs_review /
submission_uncertain (with reason), when an in-flight job outlives its lease,
when a scheduled pickup is missed, and when the AMY API is unreachable.
Never alerts on double bookings or venue changes (Tom, Nov 16 2026). State is
saved to SLACK_WORKER_STATE so restarts neither re-post nor miss transitions.
"""
import json
import logging
import os
from pathlib import Path
import re
import time

from . import health
from .views import ATTENTION, fmt_time, project, short_ref

log = logging.getLogger('amy.slack.watcher')
# Double bookings / duplicate suspicion and venue changes: no alerts (Tom).
NO_ALERT_RE = re.compile(r'double[- ]?book|duplicate (?:suspicion|booking|request|job)|possible duplicate|'
                         r'same email \+ venue|already `?done`? in the queue|venue change', re.I)
API_DOWN_AFTER = 600  # seconds of consecutive failures before alerting


def booked_text(p):
    lines = []
    for b in p['booked']:
        lines.append(f"{b['venue']} {b['date']}" + (f" \u2014 order #{b['order']}" if b['order'] else ''))
    where = '\n'.join('\u2022 ' + x for x in lines) or (
        '\u2022 ' + (', '.join(p['venues']) or 'venue n/a') + ' ' + p['dates'])
    return (f":white_check_mark: *Booked: {p['guest']}* \u2014 {p['dates']} \u2014 {p['party']}"
            f" \u2014 {p['source']}\n{where}\n_ref {p['ref']}_")


def attention_text(p):
    label = {'manual': 'Needs manual', 'needs_review': 'Needs review',
             'submission_uncertain': 'Submission uncertain'}.get(p['state'], p['state'])
    venue = ', '.join(p['venues']) or 'no venue'
    booked = ''
    if p['booked']:
        booked = '\nAlready booked (do NOT rebook): ' + '; '.join(
            f"{b['venue']} {b['date']}" + (f" #{b['order']}" if b['order'] else '') for b in p['booked'])
    return (f":warning: *{label}: {p['guest']}* \u2014 {venue} \u2014 {p['dates']} \u2014 {p['party']}\n"
            f"Reason: {p['reason'] or 'none given'}{booked}\n_ref {p['ref']}_")


class Watcher:
    def __init__(self, api, post, state_path=None, clock=time.time):
        self.api = api
        self.post = post
        self.clock = clock
        self.path = Path(state_path or os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json'))
        self.state = self._load()
        self.fail_since = None

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get('states'), dict):
                data.setdefault('alerted', [])
                return data
        except (OSError, ValueError):
            pass
        return {'states': None, 'alerted': [], 'stall_slot': None, 'api_down': False}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning('could not save watcher state: %s', type(exc).__name__)

    def _once(self, key):
        if key in self.state['alerted']:
            return False
        self.state['alerted'] = (self.state['alerted'] + [key])[-3000:]
        return True

    def poll(self):
        """One poll. Returns the list of messages posted (for tests/logs)."""
        now = self.clock()
        try:
            status = self.api.status()
            jobs = self.api.jobs()
        except Exception as exc:  # noqa: BLE001
            self.fail_since = self.fail_since or now
            log.warning('AMY API poll failed: %s', exc)
            if now - self.fail_since >= API_DOWN_AFTER and not self.state.get('api_down'):
                self.state['api_down'] = True
                self._save()
                return self._send([f":rotating_light: *AMY API unreachable* since {fmt_time(self.fail_since)} ({exc}). "
                                   'I\u2019ll post again when it\u2019s back.'])
            return []
        out = []
        self.fail_since = None
        if self.state.get('api_down'):
            self.state['api_down'] = False
            out.append(':white_check_mark: AMY API is reachable again.')
        current = {str(j.get('ref')): str(j.get('state') or '') for j in jobs}
        prev = self.state.get('states')
        if prev is None:
            log.info('baseline: %d jobs, no alerts on first run', len(current))
        else:
            for job in jobs:
                ref, state = str(job.get('ref')), str(job.get('state') or '')
                if prev.get(ref) == state:
                    continue
                p = project(job)
                if state == 'done' and self._once(f'done:{ref}'):
                    out.append(booked_text(p))
                elif state in ATTENTION:
                    if NO_ALERT_RE.search(job.get('reason') or ''):
                        continue
                    if self._once(f'{state}:{ref}:{job.get("updated")}'):
                        out.append(attention_text(p))
        self.state['states'] = current
        out += self._health(jobs, status, now, baseline=prev is None)
        self._save()
        return self._send(out)

    def _health(self, jobs, status, now, baseline):
        out = []
        h = health.assess(jobs, status, now)
        for job in h['stuck']:
            p = project(job)
            if self._once(f"stuck:{p['ref']}:{job.get('lease_until')}") and not baseline:
                out.append(f":rotating_light: *Worker stalled on a job*: {p['guest']} \u2014 "
                           f"{', '.join(p['venues']) or 'no venue'} {p['dates']} is `{p['state']}` with no update since "
                           f"{fmt_time(p['updated'])} (lease expired). It will go to manual for review; do NOT rebook "
                           f"anything already booked.\n_ref {p['ref']}_")
        slot = h['stalled_slot']
        if slot and self.state.get('stall_slot') is None:
            self.state['stall_slot'] = slot
            waiting = sum(1 for j in jobs if j.get('state') in ('queued', 'retry'))
            out.append(f":rotating_light: *Booking worker missed the {fmt_time(slot)} pickup* \u2014 {waiting} job(s) "
                       'queued/retry and no worker activity since. Check the worker.')
        elif not slot and self.state.get('stall_slot') is not None:
            last = h['last_activity']
            if last and last > self.state['stall_slot']:
                self.state['stall_slot'] = None
                out.append(f":white_check_mark: Booking worker is picking up again (activity {fmt_time(last)}).")
            elif not any(j.get('state') in ('queued', 'retry') for j in jobs):
                self.state['stall_slot'] = None
        return out

    def _send(self, messages):
        sent = []
        for text in messages:
            try:
                self.post(text)
                sent.append(text)
            except Exception as exc:  # noqa: BLE001
                log.error('Slack post failed: %s', type(exc).__name__)
        return sent

    def run_forever(self, interval=None, sleep=time.sleep):
        interval = interval or int(os.environ.get('SLACK_POLL_SECONDS', '60'))
        while True:
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception('watcher poll crashed; continuing')
            sleep(interval)


__all__ = ['Watcher', 'booked_text', 'attention_text', 'short_ref']

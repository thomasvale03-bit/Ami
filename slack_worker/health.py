"""Worker health derived read-only from queue jobs + /queue/status.

The booking worker's own heartbeat is not exposed over the API, so "last
activity" is the newest update to any job that has left `queued`, and a stall is a scheduled pickup that should have run (queued
work was due) with no worker activity since.
"""
from datetime import datetime, timedelta
import os

from .views import IN_FLIGHT, PHOENIX

MON_WED_HOURS = frozenset({0, 3, 6, 9, 12, 15, 18, 21})
THU_SUN_HOURS = frozenset({0, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23})


def grace_minutes():
    return int(os.environ.get('SLACK_STALL_GRACE_MIN', '45'))


def lease_minutes():
    return int(os.environ.get('SLACK_STALL_INFLIGHT_MIN', '75'))


def slot_hours(ts, worker_claims):
    local = datetime.fromtimestamp(ts, PHOENIX)
    if not worker_claims:  # AMYI hourly desktop pickup
        return set(range(24))
    return MON_WED_HOURS if local.weekday() <= 2 else THU_SUN_HOURS


def last_slot_before(ts, worker_claims):
    local = datetime.fromtimestamp(ts, PHOENIX).replace(minute=0, second=0, microsecond=0)
    for _ in range(24 * 8):
        if local.hour in slot_hours(local.timestamp(), worker_claims):
            return local.timestamp()
        local -= timedelta(hours=1)
    return None


def next_slot_after(ts, worker_claims):
    local = datetime.fromtimestamp(ts, PHOENIX).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24 * 8):
        if local.hour in slot_hours(local.timestamp(), worker_claims):
            return local.timestamp()
        local += timedelta(hours=1)
    return None


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def assess(jobs, status, now):
    """Dict with last_activity, in_flight, stuck (in-flight too long), and
    stalled_slot (a missed pickup), plus next_slot."""
    worker_claims = bool((status or {}).get('worker_claims'))
    paused = bool((status or {}).get('claims_paused'))
    # Any non-queued job update (claims, completions, policy moves to manual
    # during a claim) shows the pickup ran.
    touched = [j for j in jobs if j.get('state') != 'queued']
    last_activity = max((_f(j.get('updated')) for j in touched), default=None)
    in_flight = [j for j in jobs if j.get('state') in IN_FLIGHT]
    stuck = [j for j in in_flight
             if _f(j.get('lease_until'), 0) <= now or now - _f(j.get('updated'), now) > lease_minutes() * 60]
    stalled_slot = None
    if not paused and not in_flight:
        slot = last_slot_before(now - grace_minutes() * 60, worker_claims)
        runnable = [j for j in jobs if j.get('state') in ('queued', 'retry') and _f(j.get('due'), now) <= (slot or 0)]
        if slot and runnable and (last_activity is None or last_activity < slot):
            stalled_slot = slot
    return {'worker_claims': worker_claims, 'claims_paused': paused, 'execution': (status or {}).get('execution'),
            'last_activity': last_activity, 'in_flight': in_flight, 'stuck': stuck,
            'stalled_slot': stalled_slot, 'next_slot': next_slot_after(now, worker_claims)}

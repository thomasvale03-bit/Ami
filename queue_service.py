"""Railway pure queue API. AMYI executes bookings only in her own desktop.

No browser launch, challenge clicks, email sends or booking retries happen here.
Import is side-effect free. Queue ownership is feature-gated and off by default.
"""
from datetime import date, datetime
import hmac
import os
from pathlib import Path
import time
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from worker_store import Conflict, Store

PHOENIX = ZoneInfo('America/Phoenix')
HELD_REFS = frozenset({'1a1b3491f8da2fff', '1a1b3778ba3bf808'})


def enabled():
    return os.environ.get('AMY_QUEUE_ENABLED', '').lower() == 'true'


def authorized(header):
    expected = os.environ.get('AMY_QUEUE_API_TOKEN', '')
    return len(expected) >= 32 and hmac.compare_digest(str(header), 'Bearer ' + expected)


def store():
    return Store(Path(os.environ.get('AMY_QUEUE_DIR', '/data/queue')) / 'queue.sqlite3')


def source_policy(ref, raw, today):
    """Preserve source: never silently infer dates or choose a fallback venue."""
    from config import rules
    if ref.split(':')[0] in HELD_REFS or raw.get('form_ref') in HELD_REFS:
        return 'Held Shanequa/Ashley exception; awaiting Thomas decision, no remediation'
    if raw.get('_missing_required') or raw.get('_action_needed'):
        return 'Source request requires review; do not guess'
    try:
        start, end = date.fromisoformat(raw['start_date']), date.fromisoformat(raw['end_date'])
        if (end - start).days > 13:
            return 'Date range exceeds 14 nights; manual review required'
        if start < today or end < start:
            return 'Past or invalid source date; never move to tonight'
        if not raw.get('submitted_at'):
            return 'Source submission timestamp missing; consent requires review'
        if str(raw.get('authorization', '')).upper() != 'YES':
            return 'Documented authorization/21+ consent missing; manual review required'
        if int(raw.get('male_count') or 0) < 0 or int(raw.get('female_count') or 0) < 0:
            return 'Invalid party counts'
        if int(raw.get('male_count') or 0) + int(raw.get('female_count') or 0) < 1:
            return 'Party size missing; manual review required'
        venues = [rules.normalize_venue_name(v) for v in raw.get('venues', [])]
        if not venues or any(not rules.is_known_vegas_venue(v) for v in venues):
            return 'Unspecified/unsupported venue; reviewed routing required'
        drais = [v for v in venues if rules.is_drais(v)]
        if drais and len(venues) != 1:
            return "Drai's is Speakeasy-only; mixed TAO/Drai's request requires review"
        if 'JEWEL Nightclub' in venues and end >= date(2026, 11, 22):
            return 'JEWEL after Nov 21 not allowed; no automatic fallback'
        if ('OMNIA Nightclub' in venues and start == today and today.weekday() in (4, 5)):
            return 'Same-day OMNIA Friday/Saturday closed; no automatic fallback'
    except (KeyError, TypeError, ValueError):
        return 'Exact source date/venue missing or malformed; reviewed routing required'
    return ''


def enqueue(ref, raw):
    if not isinstance(raw, dict) or not ref:
        raise ValueError('ref and raw payload required')
    now = time.time()
    reason = source_policy(ref, raw, datetime.fromtimestamp(now, PHOENIX).date())
    row = store().enqueue(ref, raw, now, reason)
    import job_tracker
    job_tracker.record(ref, guest=raw.get('name', ''), email=raw.get('email', ''),
                       venue=raw.get('venues', []), dates=[raw.get('start_date', '')])
    job_tracker.update(ref, queue_owned=True, status='queue:' + row['state'])
    return row


def validate_completion(raw, metadata):
    """Evidence supplied by AMYI after observing actual UI confirmation.

    The queue cannot observe a click or independently prove a receipt. It checks
    consistency and saves evidence; it NEVER claims these checks performed booking.
    No fallback mapping is changed here. Unclear routing remains manual.
    """
    from config import rules
    evidence = metadata.get('evidence')
    if not isinstance(evidence, dict) or evidence.get('verified') is not True:
        raise Conflict('Actual verified booking evidence required')
    records = evidence.get('bookings')
    if not isinstance(records, list) or not records:
        raise Conflict('Actual booking/order records required')
    try:
        venues = {rules.normalize_venue_name(v) for v in raw['venues']}
        start, end = date.fromisoformat(raw['start_date']), date.fromisoformat(raw['end_date'])
        expected = {(v, date.fromordinal(day).isoformat()) for v in venues for day in range(start.toordinal(), end.toordinal() + 1)}
        actual = set()
        drais = any(rules.is_drais(v) for v in venues)
        for record in records:
            if not isinstance(record, dict):
                raise Conflict('Invalid booking record')
            venue, night = rules.normalize_venue_name(record['venue']), record['date']
            if (venue, night) not in expected or record.get('verified') is not True:
                raise Conflict('Receipt must match exact source date/venue')
            party = record.get('party')
            expected_party = {'male_count': int(raw.get('male_count') or 0),
                              'female_count': int(raw.get('female_count') or 0)}
            if party != expected_party:
                raise Conflict('Receipt party counts must match the source')
            if record.get('closed') or record.get('access_code_required'):
                raise Conflict('Closed/access-code lists cannot be marked booked')
            if drais:
                if not rules.is_drais(venue) or not record.get('entry_id') or record.get('system') != 'speakeasy':
                    raise Conflict("Drai's requires actual Speakeasy entry ID, never TAO")
                if evidence.get('no_guest_or_team_email') is not True or evidence.get('email_receipts'):
                    raise Conflict("Drai's: no guest/team email")
                fields = record.get('submitted_fields')
                allowed = {'first_name', 'last_name', 'male_count', 'female_count', 'name', 'party_size'}
                if not isinstance(fields, list) or not fields or not set(fields) <= allowed:
                    raise Conflict("Drai's permits only name+party fields")
            elif not record.get('order_id'):
                raise Conflict('Actual order ID required')
            actual.add((venue, night))
        if actual != expected:
            raise Conflict('All exact source venue/date records required; incomplete jobs stay active/manual')
        if not drais:
            receipts = evidence.get('email_receipts')
            if not isinstance(receipts, list) or not receipts or any(not isinstance(r, dict) or not r.get('id') for r in receipts):
                raise Conflict('Actual email send receipts required before completion')
            if {r.get('kind') for r in receipts} != {'guest', 'team'}:
                raise Conflict('Approved guest and team receipt IDs required')
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, Conflict):
            raise
        raise Conflict('Malformed receipt/source metadata') from exc


def handle(method, url, headers, payload, *, s=None, now=None):
    if not enabled():
        return 404, {'error': 'queue disabled'}
    if not authorized(headers.get('Authorization', '')):
        return 401, {'error': 'unauthorized'}
    now = time.time() if now is None else now
    s = s or store()
    path = urlparse(url).path.rstrip('/')
    try:
        if method == 'GET' and path == '/queue/status':
            return 200, s.status()
        if method == 'GET' and path == '/queue/jobs':
            query = parse_qs(urlparse(url).query)
            limit = max(1, min(int(query.get('limit', ['50'])[0]), 100))
            return 200, {'jobs': s.listing(query.get('state', [None])[0], limit)}
        if method == 'GET' and path.startswith('/queue/jobs/'):
            row = s.get(unquote(path[len('/queue/jobs/'):]))
            return (200, row) if row else (404, {'error': 'unknown ref'})
        if method == 'POST' and path == '/queue/claim':
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            local = datetime.fromtimestamp(now, PHOENIX)
            jobs = s.claim(local.strftime('%Y-%m-%dT%H'), now, allow_new=local.minute < 10 and os.environ.get('AMY_QUEUE_CLAIMS_PAUSED', '').lower() != 'true',
                           limit=max(1, min(int(payload.get('limit', 10)), 25)))
            return 200, {'jobs': jobs, 'pickup': 'hourly', 'execution': 'amyi-own-desktop-only'}
        if method == 'POST' and path.startswith('/queue/jobs/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            ref = unquote(path[len('/queue/jobs/'):])
            row = s.update(ref, payload.get('claim_token'), payload.get('action'),
                           payload.get('metadata', {}), now, str(payload.get('reason', ''))[:1000])
            import job_tracker
            job_tracker.update(ref, queue_owned=True, status='done' if row['state'] == 'done' else 'queue:' + row['state'],
                               done_at=now if row['state'] == 'done' else None)
            return 200, row
        return 404, {'error': 'not found'}
    except (Conflict, ValueError, TypeError) as exc:
        return 409, {'error': str(exc)}

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


def worker_claims():
    """AMY_QUEUE_WORKER_CLAIMS=true: the Railway booking worker owns pickup and
    may claim at any minute. Off by default (AMYI hourly desktop pickup)."""
    return os.environ.get('AMY_QUEUE_WORKER_CLAIMS', '').lower() == 'true'


def execution():
    return 'railway-booking-worker' if worker_claims() else 'amyi-own-desktop-only'


def exceptions_summary(s, limit=100):
    """Everything a person must review, smallest useful projection first.

    No claim tokens; includes the guest/venue/date needed to act and the saved
    reason/progress (order IDs already booked, emails already sent)."""
    rows = s.exceptions(limit)
    items = []
    for row in rows:
        raw, meta = row.get('raw') or {}, row.get('metadata') or {}
        items.append({
            'ref': row['ref'], 'state': row['state'], 'reason': row.get('reason', ''),
            'updated': row.get('updated'), 'attempts': row.get('attempts'),
            'guest': raw.get('name') or ' '.join(x for x in (raw.get('first_name'), raw.get('last_name')) if x),
            'email': raw.get('email', ''), 'venues': raw.get('venues', []),
            'start_date': raw.get('start_date'), 'end_date': raw.get('end_date'),
            'party': {'female_count': raw.get('female_count'), 'male_count': raw.get('male_count')},
            'source': raw.get('source', ''),
            'progress': meta.get('progress') or {}, 'resolution': meta.get('resolution') or {},
            # Verified orders already placed for this job (so a "manual" job
            # that is really booked is never mistaken for an unbooked one).
            'booked': [{'venue': b.get('venue'), 'date': b.get('date'), 'order_id': b.get('order_id'),
                        'fallback': bool(b.get('fallback'))} for b in Store.saved_bookings(meta)],
        })
    counts = {}
    for item in items:
        counts[item['state']] = counts.get(item['state'], 0) + 1
    return {'count': len(items), 'by_state': counts, 'jobs': items}


def source_policy(ref, raw, today):
    """Preserve source: never silently infer dates or choose a fallback venue."""
    from config import rules
    if ref.split(':')[0] in HELD_REFS or raw.get('form_ref') in HELD_REFS:
        return 'Held Shanequa/Ashley exception; awaiting Thomas decision, no remediation'
    if raw.get('_missing_required') or raw.get('_action_needed'):
        return 'Source request requires review; do not guess'
    import request_plan
    plan_reason = request_plan.plan_problem(raw)
    if plan_reason:
        return plan_reason
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
        if any(rules.is_manual_venue(v) for v in venues):
            return 'Venue stays manual'
        if not venues or any(not rules.is_known_vegas_venue(v) and not rules.is_manual_venue(v) for v in venues):
            return 'Unspecified/unsupported venue; reviewed routing required'
        drais = [v for v in venues if rules.is_drais(v)]
        if drais and len(venues) != 1:
            return "Drai's is Speakeasy-only; mixed TAO/Drai's request requires review"
        # JEWEL after Nov 21 and same-day OMNIA Fri/Sat are CLOSED nights, not
        # review cases: the worker books the fallback chain (OMNIA first, then
        # any open club). Parking them here left guests unbooked (Thomas,
        # Nov 22 2026: "When the venue is not open, find whatever venue that
        # is open and book them on there").
    except (KeyError, TypeError, ValueError):
        return 'Exact source date/venue missing or malformed; reviewed routing required'
    return ''


def enqueue(ref, raw):
    if not isinstance(raw, dict) or not ref:
        raise ValueError('ref and raw payload required')
    now = time.time()
    reason = source_policy(ref, raw, datetime.fromtimestamp(now, PHOENIX).date())
    import job_tracker
    legacy = job_tracker.snapshot_strict().get(ref)
    if legacy and not legacy.get('queue_owned'):
        completed = legacy.get('done_at') or legacy.get('status') == 'done'
        reason = ('Legacy job completed; do not rebook' if completed
                  else 'Existing legacy job requires explicit review; no automatic migration')
        # Preserve the existing ledger row verbatim and expose its review path.
        return store().enqueue(ref, raw, now, reason)
    row = store().enqueue(ref, raw, now, reason)
    job_tracker.record(ref, guest=raw.get('name', ''), email=raw.get('email', ''),
                       venue=raw.get('venues', []), dates=[raw.get('start_date', '')])
    job_tracker.update(ref, queue_owned=True, status='queue:' + row['state'])
    return row


MDC_ADDON_VENUE = 'Marquee Dayclub'


def is_mdc_addon(venue, night, requested_nights, drais):
    """Marquee Dayclub add-on (owner rule 2026-10-25, through Mar 31 2027).

    The worker adds Marquee Dayclub on a requested night when it is open. That
    extra receipt is accepted ONLY for Marquee Dayclub, ONLY on a night that is
    in the request, never for Drai's jobs, and never in place of the requested
    venue (the requested venue/date receipts are still all required). Any other
    venue not in the request is handled by is_approved_fallback (accepted on
    the same requested night only, recorded and flagged to team@).
    """
    from config import rules
    return (not drais and venue == MDC_ADDON_VENUE and night in requested_nights
            and night <= rules.MARQUEE_DAYCLUB_PUSH_END)


# Venues the worker's approved routing may move a night to (Thomas: OMNIA is
# the first fallback for closed/code-required nights; weekday routing falls
# back to Marquee, TAO or Hakkasan; Nov 15 2026 OMNIA->TAO move).
FALLBACK_TARGETS = frozenset({'OMNIA Nightclub', 'Marquee Nightclub', 'TAO Nightclub', 'Hakkasan Nightclub'})


def is_approved_fallback(record, venue, night, requested_nights, drais):
    """A booked venue that differs from the request is accepted ONLY on a night
    that is in the request (exact-date rule never relaxes), never as a
    Drai's receipt, and only when the worker marked it fallback:true
    (nightclub) or it is one of the approved fallback venues. Open Drai's
    jobs still cannot complete on a TAO club; a closed / no-list Drai's
    night may fall back like other clubs.
    Each accepted change is returned by validate_completion, saved on the job
    and emailed to team@ (venue changes are always flagged)."""
    from config import rules
    if night not in requested_nights or rules.is_drais(venue) or not rules.is_known_vegas_venue(venue):
        return False
    if drais:
        # Open or off-schedule Drai's nights still cannot complete as a TAO
        # receipt. Weekly no_list/closed Drai's nights may fall back.
        night_date = date.fromisoformat(night) if isinstance(night, str) else night
        if not rules.night_rules_closed("Drai's After Hours", night_date):
            return False
    return venue in FALLBACK_TARGETS or (record.get('fallback') is True and 'Nightclub' in venue)


def validate_completion(raw, metadata, require_team_receipt=True):
    """Evidence supplied by AMYI after observing actual UI confirmation.

    The queue cannot observe a click or independently prove a receipt. It checks
    consistency and saves evidence; it NEVER claims these checks performed booking.
    A record marked fallback=True may cover a requested nightclub date
    when that venue was dark and DEFAULT_ROUTING booked a different club.
    Unclear routing remains manual.
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
        # Every requested venue/night, or exactly the nightly plan's pairs when
        # the request carries one (one job for a whole date-range trip).
        import request_plan
        expected = set(request_plan.requested_pairs(raw))
        if getattr(rules, 'keep_requested_night', None):
            # Verified-dark daylife dates are never booked by the worker.
            # Closed nightclubs / Drai's / urvenue nightclubs stay so fallback can cover them.
            expected = {(v, n) for v, n in expected
                        if rules.keep_requested_night(v, date.fromisoformat(n))}
        requested_nights = {night for _v, night in expected}
        actual = set()
        fallbacks = []
        drais = any(rules.is_drais(v) for v in venues)
        for record in records:
            if not isinstance(record, dict):
                raise Conflict('Invalid booking record')
            venue, night = rules.normalize_venue_name(record['venue']), record['date']
            if record.get('verified') is not True:
                raise Conflict('Receipt must match exact source date/venue')
            mdc_extra = (venue, night) not in expected and is_mdc_addon(venue, night, requested_nights, drais)
            fallback = ((venue, night) not in expected and not mdc_extra
                        and is_approved_fallback(record, venue, night, requested_nights, drais))
            if (venue, night) not in expected and not mdc_extra and not fallback:
                raise Conflict('Receipt must match exact source date/venue')
            party = record.get('party')
            import request_plan
            female, male = request_plan.party_for_night(raw, night)
            expected_party = {'male_count': int(male), 'female_count': int(female)}
            if party != expected_party:
                raise Conflict('Receipt party counts must match the source')
            if record.get('closed') or record.get('access_code_required'):
                raise Conflict('Closed/access-code lists cannot be marked booked')
            if drais and not fallback:
                if not rules.is_drais(venue):
                    raise Conflict("Drai's receipt must be a Drai's venue, never TAO")
                # Drai's (Thomas, Nov 10 2026): guest confirmation + team@
                # receipts with no Speakeasy add. The older Speakeasy entry-ID
                # path (no emails, or emails plus an entry) remains accepted.
                if evidence.get('no_guest_or_team_email') is True and evidence.get('email_receipts'):
                    raise Conflict("Drai's: no_guest_or_team_email conflicts with email receipts")
                if evidence.get('email_receipts'):
                    if record.get('system') not in (None, 'email_only', 'speakeasy'):
                        raise Conflict("Drai's email-receipt completion cannot use a non-Drai's system")
                else:
                    if evidence.get('no_guest_or_team_email') is not True:
                        raise Conflict("Drai's: guest/team receipts or no_guest_or_team_email required")
                    if not record.get('entry_id') or record.get('system') != 'speakeasy':
                        raise Conflict("Drai's requires actual Speakeasy entry ID, never TAO")
                    fields = record.get('submitted_fields')
                    allowed = {'first_name', 'last_name', 'male_count', 'female_count', 'name', 'party_size'}
                    if not isinstance(fields, list) or not fields or not set(fields) <= allowed:
                        raise Conflict("Drai's permits only name+party fields")
            elif not record.get('order_id'):
                raise Conflict('Actual order ID required')
            if fallback:
                fallbacks.append((venue, night, record))
            elif not mdc_extra:
                actual.add((venue, night))
        venue_changes = []
        for venue, night, record in fallbacks:
            open_pairs = sorted(p for p in expected - actual if p[1] == night)
            if not open_pairs:
                raise Conflict('Fallback receipt has no unbooked requested venue on that night')
            actual.add(open_pairs[0])
            venue_changes.append({'requested_venue': open_pairs[0][0], 'booked_venue': venue, 'date': night,
                                  'order_id': record.get('order_id')})
        if actual != expected:
            raise Conflict('All exact source venue/date records required; incomplete jobs stay active/manual')
        if not drais or evidence.get('email_receipts') or fallbacks:
            receipts = evidence.get('email_receipts')
            if not isinstance(receipts, list) or not receipts or any(not isinstance(r, dict) or not r.get('id') for r in receipts):
                raise Conflict('Actual email send receipts required before completion')
            kinds = {r.get('kind') for r in receipts}
            if require_team_receipt and kinds != {'guest', 'team'}:
                raise Conflict('Approved guest and team receipt IDs required')
            if not require_team_receipt and ('guest' not in kinds or not kinds <= {'guest', 'team'}):
                raise Conflict('Approved guest receipt ID required')
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, Conflict):
            raise
        raise Conflict('Malformed receipt/source metadata') from exc
    return {'venue_changes': venue_changes}


def flag_alerts_enabled():
    """Venue-change / double-booking alert emails to team@. OFF by default
    (Thomas, Nov 16 2026 10:14 AM: "Dont need emails sent to me. Ok to double
    book."). Only AMY_QUEUE_FLAG_ALERTS=true turns them back on. The read-only
    GET /queue/flags list is unaffected; double bookings are never blocked."""
    return os.environ.get('AMY_QUEUE_FLAG_ALERTS', '').lower() == 'true'


def _branded_sender(payload):
    import gmail_client
    import posh_webhook
    return posh_webhook.send_branded(gmail_client.get_service(), payload)


def send_flag_alert(s, ref, now):
    if not flag_alerts_enabled():
        return None
    import queue_flags
    return queue_flags.send_alert(s, ref, now, datetime.fromtimestamp(now, PHOENIX).date(), _branded_sender)


def _dispatch_alert(s, ref, now):
    """Background (never delays or fails the queue reply). Tests replace this."""
    if not flag_alerts_enabled():
        return
    import threading

    def run():
        try:
            send_flag_alert(s, ref, now)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger('amy.queue').exception('Flag alert failed for %s', ref)
    threading.Thread(target=run, name='flag-alert-' + ref, daemon=True).start()


def lookup_prior_send(raw, kind):
    """The earlier guest confirmation /send-branded refused as a duplicate
    (same recipient + subject the worker uses), from Gmail Sent or the
    branded send log. Read-only."""
    if kind != 'guest':
        return None
    import gmail_client
    import worker_emails
    from config import rules
    email = (raw.get('email') or '').strip()
    if not email:
        return None
    drais = any(rules.is_drais(rules.normalize_venue_name(v)) for v in raw.get('venues') or [])
    subject = (worker_emails.drais_guest_subject if drais else worker_emails.guest_subject)(
        raw['start_date'], raw['end_date'])
    return gmail_client.prior_sent(gmail_client.get_service(), f'to:{email} subject:"{subject}"')


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
            legacy = legacy_backlog(s)
            return 200, {**s.status(), 'execution': execution(), 'worker_claims': worker_claims(),
                         'claims_paused': os.environ.get('AMY_QUEUE_CLAIMS_PAUSED', '').lower() == 'true',
                         'legacy_review_required': sum(r['review_required'] for r in legacy),
                         'intake_receipts': s.intake_listing()}
        if method == 'GET' and path == '/queue/exceptions':
            return 200, exceptions_summary(s)
        if method == 'GET' and path == '/queue/legacy':
            return 200, {'jobs': legacy_backlog(s), 'runnable': False}
        if method == 'GET' and path == '/queue/intake':
            return 200, {'receipts': s.intake_listing()}
        if method == 'GET' and path.startswith('/queue/intake/'):
            receipt = s.intake_get(unquote(path[len('/queue/intake/'):]))
            return (200, receipt) if receipt else (404, {'error': 'unknown receipt'})
        if method == 'POST' and path.startswith('/queue/legacy/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            return 200, review_legacy(s, unquote(path[len('/queue/legacy/'):]), payload, now)
        if method == 'GET' and path == '/queue/door-list-preview':
            # DRY RUN of the Scottsdale 7:43 PM door lists: read-only Gmail
            # search + counts. Sends NO email to anyone.
            night = date.fromisoformat(parse_qs(urlparse(url).query).get('night', [''])[0])
            import gmail_client
            import main as amy_main
            return 200, amy_main.scottsdale_door_list_preview(gmail_client.get_service(), night)
        if method == 'GET' and path == '/queue/flags':
            # Every venue change and same-night double booking from today on
            # (or ?from=YYYY-MM-DD), across ALL job states. Read-only.
            import queue_flags
            start = parse_qs(urlparse(url).query).get('from', [''])[0]
            today = date.fromisoformat(start) if start else datetime.fromtimestamp(now, PHOENIX).date()
            return 200, queue_flags.upcoming_flags(s, today)
        if method == 'POST' and path.startswith('/queue/flags/alert/'):
            # Owner tool: send (or re-send; deduped by subject) one job's alert now.
            ref = unquote(path[len('/queue/flags/alert/'):])
            if not flag_alerts_enabled():
                raise Conflict('Flag alert emails are off (owner, Nov 16 2026)')
            if s.get(ref) is None:
                raise Conflict('Unknown job')
            return 200, {'ref': ref, 'alert': send_flag_alert(s, ref, now)}
        if method == 'GET' and path == '/queue/jobs':
            query = parse_qs(urlparse(url).query)
            limit = max(1, min(int(query.get('limit', ['50'])[0]), 1000))
            return 200, {'jobs': s.listing(query.get('state', [None])[0], limit)}
        if method == 'GET' and path.startswith('/queue/jobs/'):
            row = s.get(unquote(path[len('/queue/jobs/'):]))
            return (200, row) if row else (404, {'error': 'unknown ref'})
        if method == 'POST' and path == '/queue/claim':
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            if any(row['review_required'] for row in legacy_backlog(s)):
                raise Conflict('Legacy backlog review required; GET /queue/legacy before sole-queue pickup')
            local = datetime.fromtimestamp(now, PHOENIX)
            paused = os.environ.get('AMY_QUEUE_CLAIMS_PAUSED', '').lower() == 'true'
            limit = max(1, min(int(payload.get('limit', 10)), 25))
            if worker_claims():
                # Railway booking worker: claim whenever idle, no hourly window
                # and no hourly batch record. Still one job lane at a time.
                jobs = s.claim(None, now, allow_new=not paused, limit=limit)
                return 200, {'jobs': jobs, 'pickup': 'continuous', 'execution': execution()}
            jobs = s.claim(local.strftime('%Y-%m-%dT%H'), now, allow_new=local.minute < 10 and not paused,
                           limit=limit)
            return 200, {'jobs': jobs, 'pickup': 'hourly', 'execution': execution()}
        if method == 'POST' and path.startswith('/queue/requeue/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            return 200, s.owner_requeue(unquote(path[len('/queue/requeue/'):]), payload.get('owner_approval'), now,
                                        start_date=payload.get('start_date'), end_date=payload.get('end_date'),
                                        clear_unsubmitted=bool(payload.get('clear_unsubmitted')))
        if method == 'POST' and path.startswith('/queue/owner-complete/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            ref = unquote(path[len('/queue/owner-complete/'):])
            row = s.owner_complete(ref, payload.get('owner_approval'), now)
            import job_tracker
            job_tracker.update(ref, queue_owned=True, status='done', done_at=now)
            if payload.get('alert', True) is not False:
                _dispatch_alert(s, ref, now)
            return 200, row
        if method == 'POST' and path.startswith('/queue/owner-reconcile/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            ref = unquote(path[len('/queue/owner-reconcile/'):])
            row = s.owner_reconcile(ref, payload.get('owner_approval'), now, lookup_prior=lookup_prior_send, covered_by=payload.get('covered_by'))
            import job_tracker
            job_tracker.update(ref, queue_owned=True, status='done', done_at=now)
            if payload.get('alert', True) is not False:
                _dispatch_alert(s, ref, now)
            return 200, row
        if method == 'POST' and path.startswith('/queue/stall-requeue/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            ref = unquote(path[len('/queue/stall-requeue/'):])
            return 200, s.stall_requeue(ref, now, str(payload.get('reason', ''))[:1000])
        if method == 'POST' and path.startswith('/queue/jobs/'):
            if not isinstance(payload, dict):
                raise Conflict('JSON object required')
            ref = unquote(path[len('/queue/jobs/'):])
            row = s.update(ref, payload.get('claim_token'), payload.get('action'),
                           payload.get('metadata', {}), now, str(payload.get('reason', ''))[:1000])
            import job_tracker
            job_tracker.update(ref, queue_owned=True, status='done' if row['state'] == 'done' else 'queue:' + row['state'],
                               done_at=now if row['state'] == 'done' else None)
            if payload.get('action') in ('complete', 'manual', 'uncertain') and row['state'] in (
                    'done', 'manual', 'submission_uncertain'):
                _dispatch_alert(s, ref, now)
            return 200, row
        return 404, {'error': 'not found'}
    except (Conflict, ValueError, TypeError) as exc:
        return 409, {'error': str(exc)}


def persist_intake(source, payload):
    """Called only behind validated website intake/authenticated Posh HTTP.

    Store original receipt BEFORE acknowledgement. Never infer a venue/date or
    make this vendor payload runnable; the existing parser/consent bridge remains.
    """
    import hashlib
    import json
    if source not in ('website', 'posh') or not isinstance(payload, dict):
        raise ValueError('Unsupported intake source/object')
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    if len(canonical.encode()) > 1024 * 1024:
        raise ValueError('Intake payload too large')
    key = source + ':' + hashlib.sha256(canonical.encode()).hexdigest()
    return store().intake(key, source, payload, time.time())


def finish_intake(key, code, text):
    # Website pending-file acknowledgement isn't proof of Gmail insertion yet.
    store().finish_intake(key, 200 <= code < 300 and 'pending' not in str(text).lower(), time.time())


def retry_intake(intake, *, s=None, now=None):
    if not enabled() or intake.dry_run:
        return
    import posh_webhook
    s = s or store()
    now = time.time() if now is None else now
    for receipt in s.pending_intake(now):
        try:
            if receipt['source'] == 'posh':
                code, text = intake.accept(receipt['payload'])
            else:
                code, text = posh_webhook.accept_website(intake, receipt['payload'])
            s.finish_intake(receipt['key'], 200 <= code < 300 and 'pending' not in str(text).lower(), now)
        except Exception:
            # Persisted original remains available for review after three attempts.
            s.finish_intake(receipt['key'], False, now)


def legacy_backlog(s):
    """Read-only projection, never imports legacy rows into runnable jobs."""
    import hashlib
    import json
    import job_tracker
    reviews = s.legacy_reviews()
    out = []
    for ref, row in sorted(job_tracker.snapshot_strict().items()):
        if row.get('queue_owned'):
            continue
        fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        completed = bool(row.get('done_at')) or row.get('status') == 'done'
        held = ref.split(':')[0] in HELD_REFS
        classification = ('completed_do_not_rebook' if completed else 'held' if held
                          else 'pending_event_review' if row.get('status') == 'pending_event' else 'review_needed')
        review = reviews.get(ref)
        reviewed = bool(review and review['fingerprint'] == fingerprint)
        out.append({'ref': ref, 'classification': classification, 'legacy': row,
                    'fingerprint': fingerprint, 'review': review if reviewed else None,
                    'review_required': not completed and not reviewed})
    return out


def review_legacy(s, ref, payload, now):
    rows = {r['ref']: r for r in legacy_backlog(s)}
    row = rows.get(ref)
    if not row or row['fingerprint'] != payload.get('fingerprint'):
        raise Conflict('Legacy row changed/missing; read current review projection')
    decision, reason = payload.get('decision'), payload.get('reason')
    allowed = {'retain_hold', 'retain_pending_event', 'manual_followup', 'completed_do_not_rebook'}
    if decision not in allowed or not isinstance(reason, str) or not reason.strip():
        raise Conflict('Explicit non-runnable review decision/reason required')
    if row['classification'] == 'held' and decision != 'retain_hold':
        raise Conflict('Held exception cannot be remediated here')
    if row['classification'] == 'completed_do_not_rebook' and decision != 'completed_do_not_rebook':
        raise Conflict('Completed legacy jobs must not be replayed')
    if decision == 'completed_do_not_rebook' and row['classification'] != decision:
        raise Conflict('Review cannot invent completion evidence')
    s.legacy_review(ref, row['fingerprint'], decision, reason[:1000], now)
    return {'ref': ref, 'decision': decision, 'runnable': False}

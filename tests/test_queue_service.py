"""Offline queue-only API tests: no browser, email, network or guest actions."""
from datetime import datetime
import socket
from unittest.mock import Mock

import pytest
import queue_service as q
from worker_store import Store

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=q.PHOENIX).timestamp()
AUTH = {'Authorization': 'Bearer ' + 't' * 32}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', 't' * 32)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


@pytest.fixture
def raw():
    return dict(name='Mock Guest', first_name='Mock', last_name='Guest', email='guest@example.invalid',
                start_date='2026-11-25', end_date='2026-11-25', venues=['Marquee Nightclub'],
                male_count=1, female_count=2, source_message_id='mock', authorization='YES', submitted_at='2026-11-10T12:00:00-07:00')


def api(s, path, body=None, now=NOW, method='POST'):
    return q.handle(method, path, AUTH, body or {}, s=s, now=now)


def assigned(raw):
    s = q.store()
    s.enqueue('mock', raw, NOW)
    code, result = api(s, '/queue/claim')
    assert code == 200
    return s, result['jobs'][0]['claim_token']


def update(s, token, action, metadata=None, now=NOW, reason=''):
    return api(s, '/queue/jobs/mock', dict(claim_token=token, action=action,
        metadata=metadata or {}, reason=reason), now)


def evidence(raw):
    return {'evidence': {'verified': True, 'bookings': [dict(verified=True,
        venue=raw['venues'][0], date=raw['start_date'], order_id='MOCK-ORDER', party={'male_count': 1, 'female_count': 2})],
        'email_receipts': [dict(kind='guest', id='MOCK-GUEST'), dict(kind='team', id='MOCK-TEAM')]}}


def test_persistent_payload_idempotent_intake(raw):
    s = q.store()
    s.enqueue('mock', raw, NOW)
    s.enqueue('mock', {**raw, 'email': 'not-the-original@example.invalid'}, NOW)
    assert Store(s.path).get('mock')['raw'] == raw


def test_status_and_authenticated_payload_list(raw, monkeypatch):
    s = q.store()
    s.enqueue('mock', raw, NOW)
    assert q.handle('GET', '/queue/jobs', {}, None, s=s)[0] == 401
    assert api(s, '/queue/jobs?limit=1000', method='GET')[1]['jobs'][0]['raw'] == raw
    assert api(s, '/queue/jobs/mock', method='GET')[1]['raw'] == raw
    assert 'claim_token' not in api(s, '/queue/jobs/mock', method='GET')[1]
    assert api(s, '/queue/status', method='GET')[1]['execution'] == 'amyi-own-desktop-only'
    monkeypatch.delenv('AMY_QUEUE_API_TOKEN')
    assert not q.authorized('Bearer ')
    monkeypatch.delenv('AMY_QUEUE_ENABLED')
    assert api(s, '/queue/status', method='GET')[0] == 404


@pytest.mark.parametrize('hour', range(24))
def test_hourly_weekend_window_only(hour):
    s = q.store()
    now = datetime(2026, 11, 7, hour, 0, tzinfo=q.PHOENIX).timestamp()
    assert api(s, '/queue/claim', now=now)[0] == 200
    # An hour's retry returns the SAME batch; it never picks up new arrivals.
    assert api(s, '/queue/claim', now=now + 1200)[0] == 200


def test_late_first_claim_rejected():
    assert api(q.store(), '/queue/claim', now=NOW + 601)[0] == 409


def test_claim_atomic_repeat_same_batch_no_new_jobs(raw):
    s, token = assigned(raw)
    s.enqueue('second', raw, NOW + 1)
    jobs = api(Store(s.path), '/queue/claim')[1]['jobs']
    assert [j['ref'] for j in jobs] == ['mock']
    assert jobs[0]['claim_token'] == token
    assert update(s, token, 'complete', evidence(raw))[0] == 200
    assert api(s, '/queue/claim')[1]['jobs'][0]['state'] == 'done'
    assert s.get('second')['state'] == 'queued'
    assert api(s, '/queue/claim', now=NOW + 3600)[1]['jobs'][0]['ref'] == 'second'


def test_completion_evidence_consistency_and_idempotency(raw):
    s, token = assigned(raw)
    assert update(s, 'wrong', 'complete', evidence(raw))[0] == 409
    assert update(s, token, 'complete', {})[0] == 409
    bad = evidence(raw)
    bad['evidence']['bookings'][0]['date'] = '2026-11-26'
    assert update(s, token, 'complete', bad)[0] == 409
    assert update(s, token, 'complete', evidence(raw))[1]['state'] == 'done'
    assert update(s, token, 'complete', evidence(raw))[1]['state'] == 'done'


def test_human_pause_requires_actual_same_local_session_clearance(raw):
    s, token = assigned(raw)
    assert update(s, token, 'pause_human', {'desktop_session_id': 'mock-local-tab'}, reason='Cloudflare')[1]['state'] == 'awaiting_human_verification'
    assert update(s, token, 'complete', evidence(raw))[0] == 409
    assert update(s, token, 'resume_human', {'desktop_session_id': 'other', 'human_cleared': True, 'clearance_checked': True})[0] == 409
    assert update(s, token, 'resume_human', {'desktop_session_id': 'mock-local-tab', 'human_cleared': True})[0] == 409
    assert update(s, token, 'resume_human', {'desktop_session_id': 'mock-local-tab', 'human_cleared': True, 'clearance_checked': True})[1]['state'] == 'assigned'
    assert update(s, token, 'complete', evidence(raw))[1]['state'] == 'done'


def test_expired_human_session_never_auto_requeues(raw):
    s, token = assigned(raw)
    update(s, token, 'pause_human', {'desktop_session_id': 'mock'}, reason='Human check')
    assert update(s, token, 'resume_human', {}, now=NOW + 901)[0] == 409
    assert s.get('mock')['state'] == 'awaiting_human_verification'  # rejected transaction rolls back safely
    assert api(s, '/queue/claim', now=NOW + 3600)[0] == 200
    assert s.get('mock')['state'] == 'manual'


def test_unknown_submit_never_retry(raw):
    s, token = assigned(raw)
    assert update(s, token, 'retry', {'submission_attempted': True}, reason='uncertain')[0] == 409
    assert update(s, token, 'uncertain', reason='Click happened, no confirmation')[1]['state'] == 'submission_uncertain'
    assert api(s, '/queue/claim', now=NOW + 3600)[1]['jobs'] == []


def test_safe_pre_submit_retry_bounded_to_three(raw):
    s, token = assigned(raw)
    for attempt in range(3):
        now = NOW + 3600 * attempt
        assert update(s, token, 'retry', {'submission_attempted': False}, now=now, reason='Before any submit')[0] == 200
        jobs = api(s, '/queue/claim', now=now + 3600)[1]['jobs']
        if jobs:
            token = jobs[0]['claim_token']
    assert s.get('mock')['state'] == 'manual'
    assert s.get('mock')['attempts'] == 3


@pytest.mark.parametrize('ref', q.HELD_REFS)
def test_held_exceptions_not_claimed(raw, ref):
    s = q.store()
    s.enqueue(ref, raw, NOW)
    assert api(s, '/queue/claim')[1]['jobs'] == []
    assert s.get(ref)['state'] == 'manual'


@pytest.mark.parametrize('change', [
    {'start_date': '2026-11-09'}, {'venues': []}, {'venues': ['Unknown']},
    {'venues': ['LIV Nightclub']}, {'venues': ["Drai's After Hours", 'TAO Nightclub']},
    {'_action_needed': ['ambiguous date', 'review']},
])
def test_source_constraints_preserve_payload(raw, change):
    s = q.store()
    original = {**raw, **change}
    s.enqueue('mock', original, NOW)
    assert api(s, '/queue/claim')[1]['jobs'] == []
    assert s.get('mock')['state'] == 'manual'
    assert s.get('mock')['raw'] == original


def test_drais_speakeasy_only_no_emails(raw):
    raw['venues'] = ["Drai's After Hours"]
    s, token = assigned(raw)
    good = {'evidence': {'verified': True, 'no_guest_or_team_email': True, 'bookings': [dict(
        venue="Drai's After Hours", date='2026-11-25', verified=True, system='speakeasy', entry_id='MOCK-ENTRY', party={'male_count': 1, 'female_count': 2},
        submitted_fields=['first_name', 'last_name', 'male_count', 'female_count'])]}}
    bad = {'evidence': {**good['evidence'], 'email_receipts': [{'id': 'not-allowed'}]}}
    assert update(s, token, 'complete', bad)[0] == 409
    assert update(s, token, 'complete', good)[1]['state'] == 'done'


def test_drais_email_receipts_complete_without_speakeasy_entry(raw):
    raw['venues'] = ["Drai's After Hours"]
    s, token = assigned(raw)
    missing = {'evidence': {'verified': True, 'bookings': [dict(
        venue="Drai's After Hours", date='2026-11-25', verified=True, system='email_only',
        party={'male_count': 1, 'female_count': 2})]}}
    assert update(s, token, 'complete', missing)[0] == 409
    good = {'evidence': {**missing['evidence'], 'email_receipts': [
        dict(kind='guest', id='MOCK-GUEST'), dict(kind='team', id='MOCK-TEAM')]}}
    assert update(s, token, 'complete', good)[1]['state'] == 'done'
    ev = s.get('mock')['metadata']['evidence']
    assert ev['bookings'][0]['system'] == 'email_only' and 'entry_id' not in ev['bookings'][0]


def test_intake_no_browser_email_or_wake(raw, monkeypatch):
    import main
    monkeypatch.setattr(main.gmail_client, 'get_plain_text_body', Mock(return_value=({}, 'mock-source')))
    monkeypatch.setattr(main.gmail_client, 'get_subject', Mock(return_value='mock'))
    monkeypatch.setattr(main.posh, 'is_posh_signup', Mock(return_value=False))
    monkeypatch.setattr(main.gmail_client, 'is_own_team_copy', Mock(return_value=False))
    monkeypatch.setattr(main.gmail_client, 'parse_request', Mock(return_value=raw))
    browser, email = Mock(side_effect=AssertionError('No browser')), Mock(side_effect=AssertionError('No email'))
    monkeypatch.setattr(main, 'concierge_handoff', browser)
    monkeypatch.setattr(main.gmail_client, 'send_once', email)
    assert main.handle_message(Mock(), 'mock', False, {}) == main.gmail_client.PROCESSED_LABEL
    assert q.store().get('mock')['raw'] == raw
    browser.assert_not_called()
    email.assert_not_called()
    import amyi
    assert not amyi.notify('posh_guest', ref='mock')
    assert main.posh_webhook.send_branded(Mock(), {})[0] == 400  # existing endpoint retained


def test_persistence_failure_preserves_inbox_source(monkeypatch):
    import main
    monkeypatch.setattr(main.gmail_client, 'list_pending_requests', Mock(return_value=[{'id': 'mock'}]))
    monkeypatch.setattr(main, 'handle_message', Mock(side_effect=OSError('volume full')))
    labels = Mock()
    monkeypatch.setattr(main.gmail_client, 'set_labels', labels)
    main.run_once(Mock(), 'live', {})
    labels.assert_not_called()


def test_owned_watchdog_suppressed():
    import job_tracker
    job_tracker.record('mock', now=0)
    job_tracker.update('mock', queue_owned=True)
    callbacks = [Mock(), Mock(), Mock()]
    assert job_tracker.watchdog(*callbacks, now=NOW) == []
    for callback in callbacks:
        callback.assert_not_called()


def test_legacy_backlog_visible_review_gate_never_imports(raw):
    import job_tracker
    job_tracker.record('legacy-future', dates=['2026-12-01'], now=NOW)
    job_tracker.update('legacy-future', status='pending_event', payload={'date': '2026-12-01'})
    job_tracker.record('legacy-done', now=NOW)
    job_tracker.mark_done('legacy-done', now=NOW)
    s = q.store()
    s.enqueue('mock', raw, NOW)
    before = job_tracker.snapshot_strict()
    assert api(s, '/queue/claim')[0] == 409
    rows = api(s, '/queue/legacy', method='GET')[1]['jobs']
    future = next(r for r in rows if r['ref'] == 'legacy-future')
    assert future['classification'] == 'pending_event_review'
    assert future['legacy']['dates'] == ['2026-12-01']
    body = {'fingerprint': future['fingerprint'], 'decision': 'retain_pending_event', 'reason': 'Parent retains future review path'}
    assert api(s, '/queue/legacy/legacy-future', body)[1]['runnable'] is False
    assert job_tracker.snapshot_strict() == before
    assert s.get('legacy-future') is None
    assert api(s, '/queue/claim')[1]['jobs'][0]['ref'] == 'mock'
    job_tracker.update('legacy-future', status='unknown-new-state')
    assert api(s, '/queue/status', method='GET')[1]['legacy_review_required'] == 1


def test_legacy_held_and_corrupt_ledger_fail_closed(raw):
    import job_tracker
    from pathlib import Path
    ref = sorted(q.HELD_REFS)[0]
    job_tracker.record(ref, now=NOW)
    s = q.store()
    row = api(s, '/queue/legacy', method='GET')[1]['jobs'][0]
    assert api(s, '/queue/legacy/' + ref, {'fingerprint': row['fingerprint'], 'decision': 'manual_followup', 'reason': 'no'})[0] == 409
    assert api(s, '/queue/legacy/' + ref, {'fingerprint': row['fingerprint'], 'decision': 'retain_hold', 'reason': 'Awaiting Thomas'})[0] == 200
    Path(job_tracker._path()).write_text('{invalid')
    s.enqueue('mock', raw, NOW)
    assert api(s, '/queue/claim')[0] == 409
    assert s.get('mock')['state'] == 'queued'


def test_posh_http_receipt_precedes_ack_and_background(monkeypatch):
    import io, json
    import posh_webhook as pw
    from tests.test_posh_webhook import BASE
    intake = Mock(dry_run=False)
    cls = pw.make_handler(intake)
    handler = object.__new__(cls)
    payload = json.dumps(BASE).encode()
    handler.path, handler.headers = '/webhooks/posh', {'Content-Length': str(len(payload))}
    handler.rfile, handler._reply = io.BytesIO(payload), Mock()
    monkeypatch.setattr(pw, 'token_ok', Mock(return_value=True))
    thread = Mock()
    monkeypatch.setattr(pw.threading, 'Thread', thread)
    handler.do_POST()
    assert q.store().intake_listing()[0]['state'] == 'pending'
    assert q.store().intake_get(q.store().intake_listing()[0]['key'])['payload'] == BASE
    handler._reply.assert_called_once_with(200, 'accepted')
    intake.accept.assert_not_called()  # fake background thread not executed
    # A durable-write failure must not acknowledge/drop an authenticated request.
    handler.rfile, handler._reply = io.BytesIO(payload), Mock()
    monkeypatch.setattr(q, 'persist_intake', Mock(side_effect=OSError('mock full volume')))
    handler.do_POST()
    handler._reply.assert_called_once_with(503, 'durable intake unavailable')
    assert thread.call_count == 1


def test_unauthenticated_posh_never_persisted(monkeypatch):
    import posh_webhook as pw
    handler = object.__new__(pw.make_handler(Mock(dry_run=False)))
    handler.path, handler.headers, handler._reply = '/webhooks/posh', {}, Mock()
    monkeypatch.setattr(pw, 'token_ok', Mock(return_value=False))
    persist = Mock()
    monkeypatch.setattr(q, 'persist_intake', persist)
    handler.do_POST()
    handler._reply.assert_called_once_with(401, 'unauthorized')
    persist.assert_not_called()


def test_website_receipt_survives_bridge_failure(monkeypatch):
    import posh_webhook as pw
    from tests.test_website_form import payload
    p = payload()
    intake = pw.Intake(Mock(), 'inbox@example.invalid', dry_run=False)
    monkeypatch.setattr(pw, '_deliver_website', Mock(side_effect=RuntimeError('mock Gmail unavailable')))
    with pytest.raises(RuntimeError):
        pw.accept_website(intake, p)
    receipts = q.store().intake_listing()
    assert len(receipts) == 1
    assert q.store().intake_get(receipts[0]['key'])['payload'] == p
    assert q.store().listing() == []  # vendor payload never guessed into runnable booking
    assert pw.accept_website(intake, {**p, 'venue': 'unknown'})[0] == 400
    assert len(q.store().intake_listing()) == 1


def test_intake_recovery_bounded_and_readable(monkeypatch):
    s = q.store()
    s.intake('mock-receipt', 'posh', {'event_name': "Drai's", 'event_start': 'ambiguous'}, NOW - 301)
    intake = Mock(dry_run=False)
    intake.accept.side_effect = RuntimeError('mock outage')
    for i in range(4):
        q.retry_intake(intake, s=s, now=NOW + i * 301)
    assert intake.accept.call_count == 3
    assert s.intake_listing()[0]['state'] == 'review'
    assert api(s, '/queue/intake/mock-receipt', method='GET')[1]['payload']['event_name'] == "Drai's"
    assert s.listing() == []


def test_queue_mode_preserves_unrelated_scheduled_sends(monkeypatch):
    import main
    monkeypatch.setattr(main.sys, 'argv', ['main.py', '--live'])
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.setenv('AMY_START_AFTER', '0')
    monkeypatch.setenv('FOLLOW_UPS_ENABLED', 'true')
    monkeypatch.setenv('WEEKLY_LINEUP_ENABLED', 'true')
    monkeypatch.setenv('SCOTTSDALE_DOOR_LISTS_ENABLED', 'true')
    monkeypatch.setattr(main.gmail_client, 'get_service', Mock(return_value=Mock()))
    monkeypatch.setattr(main.gmail_client, 'get_account_email', Mock(return_value=main.rules.INTAKE_EMAIL))
    monkeypatch.setattr(main.gmail_client, 'get_or_create_label', Mock())
    monkeypatch.setattr(main, 'outbound_ip', Mock(return_value='mock'))
    mocks = {}
    for name in ['run_once', 'prep_weekly_lineup', 'send_weekly_lineup', 'send_follow_ups',
                 'send_scottsdale_door_lists', 'run_job_watchdog', 'run_speakeasy_test']:
        mocks[name] = Mock()
        monkeypatch.setattr(main, name, mocks[name])
    main.main()
    for name in ['run_once', 'send_weekly_lineup', 'send_follow_ups', 'send_scottsdale_door_lists']:
        mocks[name].assert_called_once()
    mocks['run_job_watchdog'].assert_not_called()
    mocks['run_speakeasy_test'].assert_not_called()


def test_queue_mode_branded_endpoint_preserves_cap_and_resend_guard(monkeypatch):
    import posh_webhook as pw
    import gmail_client
    service = Mock()
    service.users().messages().send().execute.return_value = {'id': 'MOCK-SEND'}
    monkeypatch.setattr(pw, '_branded_times', [])
    monkeypatch.setattr(pw, '_zoho_sent', set())
    monkeypatch.setattr(pw, 'zoho_configured', Mock(return_value=False))
    sent_check = Mock(return_value=False)
    monkeypatch.setattr(gmail_client, 'already_sent', sent_check)
    p = {'to': 'guest@example.invalid', 'subject': 'Mock', 'text': 'Mock', 'html': '<p>Mock</p>'}
    sent_check.return_value = True
    assert pw.send_branded(service, p)[1] == 'duplicate (already in Sent)'
    sent_check.return_value = False
    for i in range(19):  # Existing limiter counts deduped attempts too.
        assert pw.send_branded(service, {**p, 'subject': f'Mock-{i}'})[0] == 200
    assert pw.send_branded(service, p)[0] == 429


def test_completed_or_existing_legacy_ref_never_blindly_migrates(raw):
    import job_tracker
    job_tracker.record('old-complete', now=NOW)
    job_tracker.mark_done('old-complete', now=NOW)
    job_tracker.record('old-pending', dates=['2026-12-01'], now=NOW)
    job_tracker.update('old-pending', status='pending_event')
    before = job_tracker.snapshot_strict()
    for ref in before:
        assert q.enqueue(ref, raw)['state'] == 'manual'
    assert job_tracker.snapshot_strict() == before
    assert all(not r.get('queue_owned') for r in job_tracker.snapshot_strict().values())


def test_website_form_still_emails_team_in_queue_mode(monkeypatch):
    import datetime, main
    from config import rules
    send = Mock(return_value=True)
    monkeypatch.setattr(main.gmail_client, 'send_once', send)
    monkeypatch.setattr(main.posh_webhook, 'to_website_text', Mock(return_value='body'))
    clean = {'ref': 'r1', 'email': 'g@example.com', 'night': datetime.date(2026, 11, 20), 'source': 'playmaker'}
    assert main.posh_webhook.notify_team_website(Mock(), clean, 'subj')
    assert send.call_args[0][2] == rules.PLAYMAKER_EMAIL == 'team@playmakerentertainment.com'


def test_stall_requeue_puts_assigned_job_back_on_queue(raw, monkeypatch):
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    s, token = assigned(raw)
    assert s.get('mock')['state'] == 'assigned'
    code, row = api(s, '/queue/stall-requeue/mock', {'reason': 'Stall watchdog: never submitted'})
    assert code == 200
    assert row['state'] == 'queued' and row['due'] == NOW
    assert 'claim_token' not in row


def test_stall_requeue_refuses_submitted_progress(raw, monkeypatch):
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    s, token = assigned(raw)
    update(s, token, 'progress', {'progress': {'submitting': {'Marquee Nightclub|2026-11-25': True}}})
    code, body = api(s, '/queue/stall-requeue/mock', {'reason': 'stall'})
    assert code == 409
    assert s.get('mock')['state'] == 'assigned'

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
    {'venues': ['JEWEL Nightclub']}, {'venues': ["Drai's After Hours", 'TAO Nightclub']},
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
    assert not main.posh_webhook.notify_team_website(Mock(), {}, 'mock')
    assert main.posh_webhook.send_branded(Mock(), {})[0] == 409


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

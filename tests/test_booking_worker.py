"""Offline tests for the Railway booking worker. No browser, network or email:
the queue API runs in-process against a temp SQLite store, TAO/Speakeasy and
/send-branded are fakes."""
from datetime import datetime
import socket
from unittest.mock import Mock

import pytest

import booking_worker as bw
import queue_service as q
import tao_portal

NOW = datetime(2026, 11, 10, 18, 30, tzinfo=q.PHOENIX).timestamp()  # 6:30 PM: outside hourly window
TOKEN = 't' * 32


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def raw(**kw):
    base = dict(name='Mock Guest', first_name='Mock', last_name='Guest', email='guest@example.invalid',
                start_date='2026-11-25', end_date='2026-11-25', venues=['Marquee Nightclub'],
                male_count='1', female_count='2', source_message_id='mock', authorization='YES',
                submitted_at='2026-11-10T12:00:00-07:00', source='Posh', phone='7025550100')
    base.update(kw)
    return base


class InProcessApi:
    """Same contract as booking_worker.Api, backed by queue_service.handle."""

    def __init__(self, store, now=NOW):
        self.store, self.now, self.sent, self.updates = store, now, [], []
        self.send_responses = []

    def _h(self, method, path, body=None):
        return q.handle(method, path, {'Authorization': 'Bearer ' + TOKEN}, body or {}, s=self.store, now=self.now)

    def claim(self):
        return self._h('POST', '/queue/claim', {'limit': 1})

    def jobs(self, state='queued', limit=100):
        return self._h('GET', f'/queue/jobs?state={state}&limit={limit}')

    def update(self, ref, token, action, metadata=None, reason=''):
        self.updates.append((ref, action, reason))
        return self._h('POST', '/queue/jobs/' + ref, {'claim_token': token, 'action': action,
                                                       'metadata': metadata or {}, 'reason': reason})

    def send_branded(self, payload):
        self.sent.append(payload)
        if self.send_responses:
            return self.send_responses.pop(0)
        return 200, {'result': {'sent': True, 'id': f'MSG-{len(self.sent)}@playmakerentertainment.com'}}


class FakeTao:
    def __init__(self, posted=True, listing=True, submit=None):
        self.posted_value, self.listing_value, self.submit_fn = posted, listing, submit
        self.submits = []

    def reset(self):
        pass

    def posted(self, venue, night):
        return self.posted_value

    def listing(self, venue, night):
        if isinstance(self.listing_value, Exception):
            raise self.listing_value
        if not self.listing_value:
            return None
        return {'event': f'Guest List - {venue}', 'event_time': '10:30 PM', 'listing_type': 'Passes', 'price': 0,
                'listing_url': 'https://tickets.taogroup.com/e/guest-list-x-11-25-2026/tickets'}

    def submit(self, listing, guest, *, rehearse, screenshot_path, before_submit):
        self.submits.append((listing['event'], rehearse))
        if self.submit_fn:
            return self.submit_fn(before_submit)
        if rehearse:
            return {'rehearsal': True, 'submit_button': 'Submit Order'}
        before_submit()
        n = len(self.submits)
        return {'verified': True, 'confirmation_id': f'ORDER-{n}',
                'confirmation_url': f'https://tickets.taogroup.com/orders/confirmation/order-{n}'}


def make(tmp_path, tao=None, drais=None, **kw):
    s = q.store()
    api = InProcessApi(s)
    worker = bw.Worker(api, bw.Ledger(tmp_path / 'w' / 'ledger.json'), tao=tao or FakeTao(), drais=drais,
                       mode='live', evidence_dir=tmp_path / 'w', now=lambda: NOW, sleep=lambda s: None,
                       email_wait_seconds=0, alerts=False, **kw)
    return s, api, worker


def test_full_cycle_books_emails_and_completes(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(venues=['Hakkasan Nightclub', 'Marquee Dayclub']), NOW)
    result = worker.run_once()
    assert result['status'] == 'done', result
    row = s.get('a')
    assert row['state'] == 'done'
    orders = sorted(b['order_id'] for b in row['metadata']['evidence']['bookings'])
    assert orders == ['ORDER-1', 'ORDER-2']
    assert [p['to'] for p in api.sent] == ['guest@example.invalid', 'team@playmakerentertainment.com']
    assert api.sent[0]['subject'] == "You're on the Playmaker Entertainment guest list for November 25"
    assert 'ORDER-1' in api.sent[0]['text'] and 'Amy Ref: a' in api.sent[1]['text']
    assert all('jarvis' not in p['to'] for p in api.sent)
    # Nothing else to do: idle, and no second submit.
    assert worker.run_once()['status'] == 'idle'
    assert len(worker.tao.submits) == 2


def test_worker_claims_outside_hourly_window_and_hourly_mode_unchanged(tmp_path, monkeypatch):
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(), NOW)
    assert api.claim()[1]['jobs'][0]['state'] == 'assigned'
    monkeypatch.delenv('AMY_QUEUE_WORKER_CLAIMS')
    assert api.claim()[0] == 200  # in-flight job still returned
    assert api.claim()[1]['execution'] == 'amyi-own-desktop-only'


def test_past_dated_jobs_do_not_consume_the_claim_and_soonest_first(tmp_path, monkeypatch):
    monkeypatch.delenv('AMY_QUEUE_WORKER_CLAIMS')  # also fixed for the hourly pickup
    s = q.store()
    s.enqueue('old', raw(start_date='2026-11-07', end_date='2026-11-07'), NOW - 30)
    s.enqueue('later', raw(start_date='2026-12-01', end_date='2026-12-01'), NOW - 20)
    s.enqueue('soon', raw(start_date='2026-11-12', end_date='2026-11-12'), NOW - 10)
    hourly = datetime(2026, 11, 10, 19, 2, tzinfo=q.PHOENIX).timestamp()
    code, body = q.handle('POST', '/queue/claim', {'Authorization': 'Bearer ' + TOKEN}, {'limit': 1}, s=s, now=hourly)
    assert code == 200
    assert [j['ref'] for j in body['jobs']] == ['soon']
    assert s.get('old')['state'] == 'manual'
    assert s.get('later')['state'] == 'queued'


def test_crash_after_click_is_uncertain_never_resubmitted(tmp_path):
    def crash(before_submit):
        before_submit()
        raise RuntimeError('browser died')
    s, api, worker = make(tmp_path, tao=FakeTao(submit=crash))
    s.enqueue('a', raw(), NOW)
    assert worker.run_once()['status'] == 'uncertain'
    assert s.get('a')['state'] == 'submission_uncertain'
    assert api.sent == []


def test_ledger_submitting_marker_blocks_resubmit_on_reclaim(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(), NOW)
    worker.ledger.put(bw.night_key('a', 'Marquee Nightclub', '2026-11-25'), {'state': 'submitting'})
    assert worker.run_once()['status'] == 'uncertain'
    assert worker.tao.submits == []


def test_booked_night_is_reused_after_email_failure_no_rebook(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(), NOW)
    api.send_responses = [(502, {'result': 'send failed'})]
    result = worker.run_once()
    assert result['status'] == 'manual' and 'DO NOT rebook' in result['reason']
    row = s.get('a')
    assert row['metadata']['progress']['bookings']['Marquee Nightclub|2026-11-25']['order_id'] == 'ORDER-1'
    assert len(worker.tao.submits) == 1


def test_rate_limited_email_waits_then_sends(tmp_path):
    s, api, worker = make(tmp_path)
    worker.email_wait = 600
    s.enqueue('a', raw(), NOW)
    api.send_responses = [(429, {'result': 'branded send limit reached, try later'})]
    assert worker.run_once()['status'] == 'done'


def test_duplicate_send_response_is_manual_not_fabricated(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(), NOW)
    api.send_responses = [(200, {'result': 'duplicate (already in Sent)'})]
    assert worker.run_once()['status'] == 'manual'
    assert s.get('a')['state'] == 'manual'


@pytest.mark.parametrize('tao,expected,state', [
    (FakeTao(listing=False), 'manual', 'manual'),
    (FakeTao(listing=tao_portal.TaoBlocked('Cloudflare did not clear')), 'manual', 'manual'),
    (FakeTao(posted=False), 'retry', 'retry'),
    (FakeTao(submit=lambda b: (_ for _ in ()).throw(tao_portal.SubmissionUncertain('spinner'))), 'uncertain',
     'submission_uncertain'),
    (FakeTao(submit=lambda b: {'verified': False, 'reason': 'female quantity 2 is not available'}), 'manual', 'manual'),
])
def test_stops_are_terminal_with_reasons(tmp_path, tao, expected, state):
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == expected, result
    assert s.get('a')['state'] == state
    assert api.sent == []


def test_duplicate_suspicion_goes_manual(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('first', raw(), NOW - 5)
    assert worker.run_once()['status'] == 'done'
    s.enqueue('again', raw(source_message_id='again'), NOW)
    result = worker.run_once()
    assert result['status'] == 'manual' and 'Duplicate suspicion' in result['reason']
    assert len(worker.tao.submits) == 1


def test_drais_off_by_default_manual(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('d', raw(venues=["Drai's"]), NOW)
    result = worker.run_once()
    assert result['status'] == 'manual' and 'AMY_WORKER_DRAIS' in result['reason']


class FakeDrais:
    def __init__(self):
        self.calls = []

    def add(self, listing_date, first, last, male, female, *, rehearse, screenshot_path, before_submit):
        self.calls.append(listing_date)
        before_submit()
        return {'verified': True, 'row': f'{first} {last} {male + female} ({male}m, {female}f)'}


def test_drais_speakeasy_only_no_emails(tmp_path, monkeypatch):
    monkeypatch.setenv('AMY_WORKER_DRAIS', 'true')
    monkeypatch.setenv('DRAIS_SPEAKEASY_DAY_OFFSET', '1')
    drais = FakeDrais()
    s, api, worker = make(tmp_path, drais=drais)
    s.enqueue('d', raw(venues=["Drai's"]), NOW)
    assert worker.run_once()['status'] == 'done'
    assert drais.calls == ['2026-11-26']
    assert api.sent == []
    ev = s.get('d')['metadata']['evidence']
    assert ev['no_guest_or_team_email'] is True and ev['bookings'][0]['system'] == 'speakeasy'


def test_drais_without_offset_is_manual(tmp_path, monkeypatch):
    monkeypatch.setenv('AMY_WORKER_DRAIS', 'true')
    s, api, worker = make(tmp_path, drais=FakeDrais())
    s.enqueue('d', raw(venues=["Drai's"]), NOW)
    assert 'DRAIS_SPEAKEASY_DAY_OFFSET' in worker.run_once()['reason']


def test_dry_run_never_claims_or_sends(tmp_path):
    s, api, worker = make(tmp_path)
    worker.mode = 'dry-run'
    s.enqueue('old', raw(start_date='2026-11-07', end_date='2026-11-07'), NOW - 5)
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == 'rehearsed' and result['ref'] == 'a'
    assert worker.tao.submits == [('Guest List - Marquee Nightclub', True)]
    assert s.get('a')['state'] == 'queued' and s.get('old')['state'] == 'queued'
    assert api.sent == [] and api.updates == []


def test_exceptions_endpoint_lists_review_work(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(listing=False))
    s.enqueue('a', raw(), NOW)
    s.enqueue('held', raw(start_date='2026-11-01', end_date='2026-11-01'), NOW)
    worker.run_once()
    code, body = q.handle('GET', '/queue/exceptions', {'Authorization': 'Bearer ' + TOKEN}, None, s=s, now=NOW)
    assert code == 200
    assert {j['ref'] for j in body['jobs']} == {'a', 'held'}
    assert body['by_state'] == {'manual': 2}
    assert 'claim_token' not in str(body)
    assert q.handle('GET', '/queue/exceptions', {}, None, s=s)[0] == 401


def test_live_needs_explicit_switch_and_no_assisted(monkeypatch):
    monkeypatch.setenv('BRANDED_SEND_TOKEN', 'x')
    assert any('AMY_WORKER_LIVE_CONFIRM' in p for p in bw.check_config('live'))
    monkeypatch.setenv('AMY_WORKER_LIVE_CONFIRM', 'yes')
    assert bw.check_config('live') == []
    monkeypatch.setenv('AMY_SIGNUP_MODE', 'assisted')
    assert bw.check_config('live')


def test_blocked_recipient_guard():
    import worker_emails
    assert not worker_emails.allowed_recipient('infojarvis@mail.grokbot.com')
    assert worker_emails.allowed_recipient('team@playmakerentertainment.com')


def test_proxy_parsing(monkeypatch):
    monkeypatch.setenv('AMY_BROWSER_PROXY', 'http://us%40er:p%3Ass@proxy.example:8000')
    assert tao_portal.browser_proxy() == {'server': 'http://proxy.example:8000', 'username': 'us@er', 'password': 'p:ss'}
    monkeypatch.delenv('AMY_BROWSER_PROXY')
    assert tao_portal.browser_proxy() is None

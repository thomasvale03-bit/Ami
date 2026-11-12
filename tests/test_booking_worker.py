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
    def __init__(self, posted=True, listing=True, submit=None, dayclubs=False):
        self.posted_value, self.listing_value, self.submit_fn = posted, listing, submit
        self.dayclubs = dayclubs
        self.submits = []

    def reset(self):
        pass

    def posted(self, venue, night):
        if venue == "Marquee Dayclub":
            return bool(self.dayclubs)
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
    s, api, worker = make(tmp_path, tao=FakeTao(dayclubs=True))
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
    (FakeTao(listing=False), 'retry', 'retry'),
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
    s, api, worker = make(tmp_path, duplicate_check=True)
    s.enqueue('first', raw(), NOW - 5)
    assert worker.run_once()['status'] == 'done'
    s.enqueue('again', raw(source_message_id='again'), NOW)
    result = worker.run_once()
    assert result['status'] == 'manual' and 'Duplicate suspicion' in result['reason']
    assert len(worker.tao.submits) == 1


def test_duplicate_check_off_by_default_books_second_signup(tmp_path):
    s, api, worker = make(tmp_path)
    assert worker.duplicate_check is False
    s.enqueue('first', raw(), NOW - 5)
    assert worker.run_once()['status'] == 'done'
    s.enqueue('again', raw(source_message_id='again'), NOW)
    result = worker.run_once()
    assert result['status'] == 'done', result
    assert len(worker.tao.submits) == 2
    assert s.get('again')['state'] == 'done'


class FakeDrais:
    def __init__(self):
        self.calls = []

    def add(self, listing_date, first, last, male, female, *, rehearse, screenshot_path, before_submit):
        self.calls.append(listing_date)
        before_submit()
        raise AssertionError("Drai's jobs must not call Speakeasy")


def test_drais_skips_speakeasy_and_sends_guest_and_team(tmp_path):
    drais = FakeDrais()
    s, api, worker = make(tmp_path, drais=drais)
    s.enqueue('d', raw(venues=["Drai's"], start_date='2026-11-26', end_date='2026-11-26'), NOW)
    assert worker.run_once()['status'] == 'done'
    assert drais.calls == []  # Thomas, Nov 10 2026: never add on Speakeasy
    assert [p['to'] for p in api.sent] == ['guest@example.invalid', 'team@playmakerentertainment.com']
    guest, team = api.sent
    assert "Drai's After Hours" in guest['subject'] and 'PLAYMAKER ENTERTAINMENT' in guest['text']
    assert 'skip the line' not in guest['html'].lower() and 'speakeasygo.com/ms/playmaker' in guest['html']
    assert team['subject'].startswith('Signed up: Mock Guest - Drai') and ',' not in team['subject']
    assert 'AMY ref: d' in team['text']
    ev = s.get('d')['metadata']['evidence']
    assert ev['bookings'][0]['system'] == 'email_only' and 'entry_id' not in ev['bookings'][0]
    assert {r['kind'] for r in ev['email_receipts']} == {'guest', 'team'}
    assert worker.tao.submits == []  # never TAO for Drai's
    key = "d|Drai's|2026-11-26"
    assert worker.ledger.get(key)['state'] == 'email_only'


def test_drais_email_only_ignores_speakeasy_flags(tmp_path, monkeypatch):
    monkeypatch.setenv('AMY_WORKER_DRAIS', 'false')
    monkeypatch.setenv('DRAIS_SPEAKEASY_DAY_OFFSET', 'tomorrow')
    drais = FakeDrais()
    s, api, worker = make(tmp_path, drais=drais)
    s.enqueue('d', raw(venues=["Drai's"], start_date='2026-11-26', end_date='2026-11-26'), NOW)
    assert worker.run_once()['status'] == 'done'
    assert drais.calls == []
    ev = s.get('d')['metadata']['evidence']
    assert ev['bookings'][0]['system'] == 'email_only'
    assert {r['kind'] for r in ev['email_receipts']} == {'guest', 'team'}


def test_box_lock_busy_releases_job_for_retry(tmp_path, monkeypatch):
    lock = tmp_path / 'lock'
    lock.mkdir()
    (lock / 'owner').write_text('amyi-computeruse\n%d\n' % int(__import__('time').time()))
    monkeypatch.setenv('AMY_WORKER_BOX_LOCK', 'true')
    from tools import browser_lock
    monkeypatch.setattr(browser_lock, 'LOCK_DIR', str(lock))
    monkeypatch.setattr(browser_lock.acquire, '__defaults__',
                        (str(lock), str(tmp_path / 'l.log'), 0.01, 0.05, 720.0, __import__('time').time, lambda _s: None))
    s, api, worker = make(tmp_path)
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == 'retry', result
    assert s.get('a')['state'] == 'retry' and worker.tao.submits == []
    assert 'TIMEOUT' in (tmp_path / 'l.log').read_text()


def test_browser_host_refuses_computeruse_profile(monkeypatch):
    import browser_host
    monkeypatch.setenv('AMY_BROWSER_LAUNCH', 'plain')
    monkeypatch.setenv('AMY_BROWSER_PROFILE', '/home/box/chrome-profile/Fork-2')
    with pytest.raises(RuntimeError):
        browser_host.start()
    monkeypatch.delenv('AMY_BROWSER_LAUNCH')
    assert browser_host.start() is None


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
    s, api, worker = make(tmp_path, tao=FakeTao(listing=tao_portal.TaoBlocked('Cloudflare did not clear')))
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


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=q.PHOENIX).timestamp()


@pytest.mark.parametrize('day', [9, 10, 11])  # Mon-Wed Nov 9-11 2026
def test_schedule_mon_wed_every_three_hours(day):
    allowed = {h for h in range(24) if bw.scheduled_slot(_ts(2026, 11, day, h, 0))}
    assert allowed == {0, 3, 6, 9, 12, 15, 18, 21}


@pytest.mark.parametrize('day', [12, 13, 14, 15])  # Thu-Sun Nov 12-15 2026
def test_schedule_thu_sun(day):
    allowed = {h for h in range(24) if bw.scheduled_slot(_ts(2026, 11, day, h, 5))}
    assert allowed == {0, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23}
    assert not any(bw.scheduled_slot(_ts(2026, 11, day, h, 0)) for h in range(1, 10))


def test_schedule_window_and_legacy_ten_minute_option():
    assert bw.scheduled_slot(_ts(2026, 11, 9, 3, 9)) == '2026-11-09T03'
    assert bw.scheduled_slot(_ts(2026, 11, 9, 3, 10), window=10) is None
    assert 'Mon Nov 09 03:00 AM' in bw.next_slot_label(_ts(2026, 11, 9, 1, 45))


def test_run_pickup_clears_queue_one_job_at_a_time(tmp_path):
    s, api, worker = make(tmp_path)
    for i, d in enumerate(['2026-11-20', '2026-11-12', '2026-11-15']):
        s.enqueue(f'j{i}', raw(start_date=d, end_date=d, email=f'g{i}@example.invalid', source_message_id=f'm{i}'), NOW)
    claims = []
    real_claim = api.claim
    def claim():
        code, body = real_claim()
        claims.append([j['ref'] for j in body.get('jobs', [])])
        return code, body
    api.claim = claim
    result = worker.run_pickup()
    assert result['handled'] == 3
    assert claims[:3] == [['j1'], ['j2'], ['j0']]  # soonest date first, one per claim
    assert all(s.get(r)['state'] == 'done' for r in ('j0', 'j1', 'j2'))
    assert len(api.sent) == 6


def test_run_window_widened_to_thirty_minutes():
    assert bw.scheduled_slot(_ts(2026, 11, 9, 6, 29)) == '2026-11-09T06'
    assert bw.scheduled_slot(_ts(2026, 11, 9, 6, 30)) is None


class CountingWorker:
    def __init__(self):
        self.runs = 0

    def run_pickup(self):
        self.runs += 1
        return {'status': 'run_done', 'handled': 0}


def test_pickup_step_once_per_hour_even_across_restart(tmp_path):
    w = CountingWorker()
    state = bw.SlotState(tmp_path)
    t = _ts(2026, 11, 9, 6, 3)
    assert bw.pickup_step(w, state, t)[0] == '2026-11-09T06'
    assert bw.pickup_step(w, state, t + 60) == (None, None)
    assert bw.pickup_step(w, bw.SlotState(tmp_path), t + 120, kicked=True) == (None, None)  # restart + kick
    assert w.runs == 1
    assert bw.pickup_step(w, state, _ts(2026, 11, 9, 7, 0), kicked=True) == (None, None)  # not scheduled
    assert w.runs == 1


def test_live_loop_uses_wall_clock_after_vm_pause(tmp_path):
    """A paused VM resumes with wall time jumped ahead: the next short step
    sees the new time and runs the slot (late but inside the window)."""
    w = CountingWorker()
    times = iter([_ts(2026, 11, 9, 2, 58), _ts(2026, 11, 9, 2, 59), _ts(2026, 11, 9, 3, 17), _ts(2026, 11, 9, 3, 18)])
    calls = []

    def clock():
        try:
            return next(times)
        except StopIteration:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bw.live_loop(w, tmp_path, step=5, clock=clock, sleep=calls.append)
    assert w.runs == 1 and calls == [5, 5, 5]  # startup log read the clock once


def test_kick_file_triggers_run_in_window(tmp_path):
    w = CountingWorker()
    state = bw.SlotState(tmp_path)
    state.kick_file.touch()
    assert state.take_kick() and not state.take_kick()


class BatchWorker:
    def __init__(self, sizes):
        self.sizes, self.calls = list(sizes), 0

    def run_pickup(self):
        self.calls += 1
        n = self.sizes.pop(0)
        return {'status': 'run_done', 'handled': n,
                'results': [{'status': 'booked'}] * n + ([{'status': 'idle'}] if n < 25 else [])}


def test_run_now_claims_again_after_full_batch():
    w = BatchWorker([25, 2])
    assert bw.run_now(w) == {'status': 'run_now_done', 'batches': 2, 'handled': 27}


def test_run_now_file_runs_once_and_keeps_slot(tmp_path):
    w = BatchWorker([3])
    state = bw.SlotState(tmp_path)
    state.run_now_file.touch()
    times = iter([_ts(2026, 11, 9, 4, 1)] * 3)

    def clock():
        try:
            return next(times)
        except StopIteration:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bw.live_loop(w, tmp_path, step=5, clock=clock, sleep=lambda s: None)
    assert w.calls == 1 and state.last() is None and not state.run_now_file.exists()

def test_browser_lost_detects_cdp_refused():
    assert bw.browser_lost(RuntimeError(
        'BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9335'))
    assert bw.browser_lost(ConnectionError('Connection refused'))
    assert not bw.browser_lost(RuntimeError('list is closed'))
    assert not bw.browser_lost(ValueError('bad date'))


def test_ensure_is_noop_unless_plain_launch(monkeypatch):
    import browser_host
    monkeypatch.delenv('AMY_BROWSER_LAUNCH', raising=False)
    monkeypatch.delenv('AMY_BROWSER_CDP_URL', raising=False)
    assert browser_host.ensure() is None
    assert browser_host.start() is None
    assert browser_host.restart() is None


def test_ensure_reuses_healthy_chrome_and_restarts_when_down(monkeypatch):
    import browser_host
    monkeypatch.setenv('AMY_BROWSER_LAUNCH', 'plain')
    monkeypatch.setenv('AMY_BROWSER_CDP_URL', 'http://127.0.0.1:9335')
    monkeypatch.setattr(browser_host, 'healthy', lambda url=None, timeout=2: True)
    assert browser_host.ensure() == 'http://127.0.0.1:9335'
    calls = []
    monkeypatch.setattr(browser_host, 'healthy', lambda url=None, timeout=2: False)
    monkeypatch.setattr(browser_host, 'restart', lambda: calls.append('restart') or 'http://127.0.0.1:9335')
    assert browser_host.ensure() == 'http://127.0.0.1:9335' and calls == ['restart']


def test_cdp_refused_restarts_browser_and_retries_job(tmp_path, monkeypatch):
    calls = {'listings': 0, 'restarts': 0}

    class Flaky(FakeTao):
        def listing(self, venue, night):
            calls['listings'] += 1
            if calls['listings'] == 1:
                raise RuntimeError(
                    'BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9335\n'
                    'Call log:\n  - <ws preparing> retrieving websocket url from http://127.0.0.1:9335')
            return super().listing(venue, night)

    import browser_host
    monkeypatch.setattr(browser_host, 'restart', lambda: calls.__setitem__('restarts', calls['restarts'] + 1))
    s, api, worker = make(tmp_path, tao=Flaky())
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == 'done', result
    assert calls['restarts'] == 1 and calls['listings'] == 2
    assert s.get('a')['state'] == 'done'
    assert api.updates[-1][1] == 'complete'


def test_cdp_error_after_click_is_uncertain_not_retried(tmp_path, monkeypatch):
    def boom(before_submit):
        before_submit()
        raise RuntimeError('BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9335')

    restarts = []
    import browser_host
    monkeypatch.setattr(browser_host, 'restart', lambda: restarts.append(1))
    s, api, worker = make(tmp_path, tao=FakeTao(submit=boom))
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == 'uncertain', result
    assert restarts == []
    assert s.get('a')['state'] == 'submission_uncertain'


def test_cdp_retry_goes_manual_if_browser_stays_down(tmp_path, monkeypatch):
    class Down(FakeTao):
        def listing(self, venue, night):
            raise RuntimeError('BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9335')

    restarts = []
    import browser_host
    monkeypatch.setattr(browser_host, 'restart', lambda: restarts.append(1))
    s, api, worker = make(tmp_path, tao=Down())
    s.enqueue('a', raw(), NOW)
    result = worker.run_once()
    assert result['status'] == 'manual', result
    assert restarts == [1]
    assert 'not retried' in result['reason']
    assert s.get('a')['state'] == 'manual'

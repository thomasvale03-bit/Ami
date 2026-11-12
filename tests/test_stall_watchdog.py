"""Stall watchdog: 20 minutes with no progress restarts the run; ledger is honored."""
from datetime import datetime
import json
import socket
from unittest.mock import Mock
from pathlib import Path

import pytest

import booking_worker as bw
import queue_service as q
import stall_watchdog as sw
from tests.test_booking_worker import InProcessApi, FakeTao, raw, make, TOKEN, NOW as WORKER_NOW

NOW = datetime(2026, 11, 11, 18, 30, tzinfo=q.PHOENIX).timestamp()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setenv('AMY_WORKER_DIR', str(tmp_path / 'w'))
    monkeypatch.setenv('AMY_WORKER_LIVE_CONFIRM', 'yes')
    monkeypatch.setenv('AMY_BROWSER_LAUNCH', '')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def _api(tmp_path):
    s = q.store()
    api = InProcessApi(s, now=NOW)
    def stall_requeue(ref, reason=''):
        return api._h('POST', '/queue/stall-requeue/' + ref, {'reason': reason})
    api.stall_requeue = stall_requeue
    ledger = bw.Ledger(tmp_path / 'w' / 'ledger.json')
    return s, api, ledger


def _claim(s, api, ref='job1', **raw_kw):
    s.enqueue(ref, raw(source_message_id=ref, **raw_kw), NOW)
    api.now = NOW
    code, body = api.claim()
    assert code == 200 and body['jobs']
    return body['jobs'][0]


def test_idle_or_fresh_progress_is_not_a_stall(tmp_path):
    d = tmp_path / 'w'
    assert sw.is_stalled(d, now=NOW, stall_seconds=1200) is False
    sw.start_run(d, '2026-11-11T18', clock=lambda: NOW)
    assert sw.is_stalled(d, now=NOW + 1199, stall_seconds=1200) is False
    sw.mark_progress(d, 'job1', clock=lambda: NOW + 600)
    # Run has been going 25 minutes but progressed 5 minutes ago.
    assert sw.is_stalled(d, now=NOW + 1500, stall_seconds=1200) is False


def test_no_progress_for_20_minutes_is_a_stall(tmp_path):
    d = tmp_path / 'w'
    sw.start_run(d, '2026-11-11T18', clock=lambda: NOW)
    assert sw.is_stalled(d, now=NOW + 1200, stall_seconds=1200) is True
    sw.end_run(d)
    assert sw.is_stalled(d, now=NOW + 2400, stall_seconds=1200) is False


def test_unsubmitted_assigned_job_is_requeued(tmp_path):
    s, api, ledger = _api(tmp_path)
    job = _claim(s, api)
    result = sw.recover_assigned_jobs(api, ledger)
    assert result['requeued'] == [job['ref']]
    assert s.get(job['ref'])['state'] == 'queued'
    assert s.get(job['ref'])['due'] <= NOW


def test_submitted_job_is_not_requeued_and_is_finished(tmp_path):
    s, api, ledger = _api(tmp_path)
    job = _claim(s, api)
    ref = job['ref']
    ledger.put(f'{ref}|Marquee Nightclub|2026-11-25', {
        'state': 'booked',
        'record': {'verified': True, 'venue': 'Marquee Nightclub', 'date': '2026-11-25',
                   'order_id': 'ORDER-1', 'party': {'male_count': 1, 'female_count': 2}},
        'meta': {'event': 'Guest List'},
    })
    worker = bw.Worker(api, ledger, tao=FakeTao(), mode='live', evidence_dir=tmp_path / 'w',
                       now=lambda: NOW, sleep=lambda s: None, email_wait_seconds=0,
                       duplicate_check=False, alerts=False)
    result = sw.recover_assigned_jobs(api, ledger, worker=worker)
    assert result['requeued'] == []
    assert result['finished'] == [ref]
    assert s.get(ref)['state'] == 'done'
    assert [p['to'] for p in api.sent] == ['guest@example.invalid', 'team@playmakerentertainment.com']
    assert len(worker.tao.submits) == 0  # never resubmitted


def test_submitting_without_confirmation_goes_uncertain(tmp_path):
    s, api, ledger = _api(tmp_path)
    job = _claim(s, api)
    ref = job['ref']
    ledger.put(f'{ref}|Marquee Nightclub|2026-11-25', {'state': 'submitting'})
    result = sw.recover_assigned_jobs(api, ledger)
    assert result['uncertain'] == [ref]
    assert result['requeued'] == []
    assert s.get(ref)['state'] == 'submission_uncertain'


def test_stall_event_log_and_catch_up_file(tmp_path, caplog):
    d = tmp_path / 'w'
    sw.start_run(d, '2026-11-11T18', clock=lambda: NOW)
    event = sw.record_stall(d, slot='2026-11-11T18', duration=18000,
                            requeued=['abc', 'def'], clock=lambda: NOW + 18000)
    sw.request_catch_up(d)
    lines = (d / 'stall_events.jsonl').read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row['slot'] == '2026-11-11T18' and row['duration'] == 18000
    assert row['requeued'] == ['abc', 'def']
    assert row['ts'].endswith('-07:00') or '-07:00' in row['ts']
    assert (d / 'run_now').exists()
    assert event['requeued'] == ['abc', 'def']
    assert any('STALL_RESTART slot=2026-11-11T18 duration=18000s requeued=abc,def' in r.message
               for r in caplog.records)


def test_release_only_worker_lock(tmp_path):
    lock = tmp_path / 'lock'
    lock.mkdir()
    (lock / 'owner').write_text('amyi-desktop\n1\n')
    assert sw.release_worker_lock(lock_dir=str(lock)) is None
    assert lock.exists()
    (lock / 'owner').write_text('worker:job1\n1\n')
    assert sw.release_worker_lock(lock_dir=str(lock)) == 'worker:job1'
    assert not lock.exists()


class FakeProc:
    def __init__(self):
        self.pid = 4242
        self._alive = True
        self.returncode = None
        self.killed = False
        self.terminated = False

    def poll(self):
        return None if self._alive else (self.returncode or 0)

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        self._alive = False
        return self.returncode


def test_handle_stall_kills_requeues_and_requests_catch_up(tmp_path):
    s, api, ledger = _api(tmp_path)
    job = _claim(s, api)
    d = tmp_path / 'w'
    sw.start_run(d, '2026-11-11T18', clock=lambda: NOW)
    proc = FakeProc()
    recovery = sw.handle_stall(proc, d, api, ledger, clock=lambda: NOW + 1800, sleep=lambda s: None)
    assert proc.terminated or proc.killed
    assert recovery['requeued'] == [job['ref']]
    assert s.get(job['ref'])['state'] == 'queued'
    assert (d / 'run_now').exists()
    row = json.loads((d / 'stall_events.jsonl').read_text().strip())
    assert row['slot'] == '2026-11-11T18' and job['ref'] in row['requeued']
    assert sw.read_heartbeat(d)['status'] == 'idle'


def test_supervise_restarts_immediately_on_stall(tmp_path):
    d = tmp_path / 'w'
    sw.start_run(d, '2026-11-11T18', clock=lambda: NOW)
    started = []
    recovered = []

    def start():
        proc = FakeProc()
        started.append(proc)
        if len(started) == 2:
            # Second worker exits so supervise returns.
            proc._alive = False
            proc.returncode = 0
        return proc

    clock_state = {'t': NOW + 1200}

    def clock():
        return clock_state['t']

    def sleep(_s):
        # After the first stall handling, make the second worker look not-stalled
        # by being idle, then let it "exit".
        if len(started) >= 1:
            clock_state['t'] += 1

    def recover(proc, directory):
        recovered.append(directory)
        proc.terminate()
        sw.end_run(directory)

    rc = sw.supervise(d, stall_seconds=1200, poll=0, clock=clock, sleep=sleep,
                      start_worker=start, recover=recover)
    assert len(started) == 2
    assert recovered
    assert rc == 0

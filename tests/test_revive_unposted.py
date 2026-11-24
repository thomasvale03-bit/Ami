"""Tom (Nov 21 2026): LAVO Brunch Dec 5/12/19 were open on the promoter page but
AMY still had them parked. Cause: before Nov 19 a not-posted list used the
3-claim limit and went to manual for good. Claims now put those back in retry."""
import json

from tests.test_booking_worker import FakeTao, NOW, make, raw  # noqa: F401
from tests.test_booking_worker import isolated  # noqa: F401

REASON = 'LAVO Party Brunch 2026-12-05: guest list not posted on TAO yet; nothing submitted'


def park(s, ref, reason=REASON, metadata=None):
    with s.connect() as db:
        db.execute("UPDATE queue_jobs SET state='manual',attempts=3,reason=?,metadata=? WHERE ref=?",
                   (reason, json.dumps(metadata or {}), ref))


def lavo(**kw):
    base = dict(venues=['LAVO Party Brunch'], start_date='2026-12-05', end_date='2026-12-05')
    base.update(kw)
    return raw(**base)


def test_stranded_unposted_manual_job_is_revived_and_booked(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao())
    s.enqueue('l', lavo(), NOW)
    park(s, 'l')
    result = worker.run_once()
    assert result['status'] == 'done', result
    row = s.get('l')
    assert row['metadata']['revived']['from_reason'] == REASON
    assert [p['to'] for p in api.sent] == ['guest@example.invalid', 'team@playmakerentertainment.com']


def test_revived_job_still_unposted_waits_in_retry_not_manual(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    s.enqueue('l', lavo(), NOW)
    park(s, 'l')
    assert worker.run_once()['status'] == 'retry'
    assert s.get('l')['state'] == 'retry'


def test_not_revived_when_other_reason_past_date_or_progress(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao())
    s.enqueue('other', lavo(), NOW)
    park(s, 'other', reason='Source request requires review; do not guess')
    s.enqueue('prog', lavo(email='p@example.invalid'), NOW)
    park(s, 'prog', metadata={'progress': {'submitting': {'LAVO Party Brunch|2026-12-05': True}}})
    s.enqueue('today', lavo(email='t@example.invalid', start_date='2026-11-10', end_date='2026-11-10'), NOW)
    park(s, 'today')
    assert worker.run_once()['status'] == 'idle'
    for ref in ('other', 'prog', 'today'):
        assert s.get(ref)['state'] == 'manual', ref
    assert api.sent == []


def test_overlapping_guest_job_does_not_block_revival(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao())
    s.enqueue('a1', lavo(email='dup@example.invalid', venues=['OMNIA Nightclub'], start_date='2026-12-25',
                         end_date='2026-12-26'), NOW)
    s.enqueue('a2', lavo(email='dup@example.invalid', venues=['Marquee Nightclub'], start_date='2026-12-25',
                         end_date='2026-12-25'), NOW)
    park(s, 'a1')
    park(s, 'a2')
    result = worker.run_once()
    assert result['status'] == 'done', result
    states = {s.get('a1')['state'], s.get('a2')['state']}
    assert 'done' in states
    assert 'manual' not in states

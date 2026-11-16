"""Completion check: accept the Marquee Dayclub add-on only (Thomas, Nov 15 2026)."""
import pytest
import queue_service as q
from queue_service import Conflict

PARTY = {'male_count': 4, 'female_count': 3}


def raw(venues=('OMNIA Nightclub',), start='2026-11-15', end=None):
    return dict(venues=list(venues), start_date=start, end_date=end or start, male_count=4, female_count=3)


def rec(venue, night, order='ORDER'):
    return dict(verified=True, venue=venue, date=night, order_id=order, party=dict(PARTY))


def meta(*records):
    return {'evidence': {'verified': True, 'bookings': list(records),
                         'email_receipts': [dict(kind='guest', id='G'), dict(kind='team', id='T')]}}


def test_mdc_extra_on_requested_night_accepted():
    q.validate_completion(raw(), meta(rec('OMNIA Nightclub', '2026-11-15'), rec('Marquee Dayclub', '2026-11-15')))


def test_mdc_extra_multi_night_request_accepted():
    r = raw(start='2026-11-20', end='2026-11-21')
    q.validate_completion(r, meta(rec('OMNIA Nightclub', '2026-11-20'), rec('OMNIA Nightclub', '2026-11-21'),
                                  rec('Marquee Dayclub', '2026-11-21')))


def test_mdc_on_non_requested_night_refused():
    with pytest.raises(Conflict, match='exact source date/venue'):
        q.validate_completion(raw(), meta(rec('OMNIA Nightclub', '2026-11-15'), rec('Marquee Dayclub', '2026-11-16')))


def test_mdc_after_push_end_refused():
    with pytest.raises(Conflict):
        q.validate_completion(raw(start='2027-04-03'), meta(rec('OMNIA Nightclub', '2027-04-03'),
                                                            rec('Marquee Dayclub', '2027-04-03')))


def test_fallback_venue_still_refused():
    r = raw(venues=('Hakkasan Nightclub',), start='2026-11-20')
    with pytest.raises(Conflict, match='exact source date/venue'):
        q.validate_completion(r, meta(rec('JEWEL Nightclub', '2026-11-20')))
    with pytest.raises(Conflict, match='exact source date/venue'):
        q.validate_completion(r, meta(rec('JEWEL Nightclub', '2026-11-20'), rec('Marquee Dayclub', '2026-11-20')))


def test_mdc_alone_cannot_replace_requested_venue():
    with pytest.raises(Conflict, match='All exact source venue/date records required'):
        q.validate_completion(raw(), meta(rec('Marquee Dayclub', '2026-11-15')))


def test_mdc_extra_still_needs_order_party_and_verified():
    bad = rec('Marquee Dayclub', '2026-11-15'); bad['party'] = {'male_count': 1, 'female_count': 0}
    with pytest.raises(Conflict, match='party'):
        q.validate_completion(raw(), meta(rec('OMNIA Nightclub', '2026-11-15'), bad))
    bad = rec('Marquee Dayclub', '2026-11-15'); bad.pop('order_id')
    with pytest.raises(Conflict, match='order ID'):
        q.validate_completion(raw(), meta(rec('OMNIA Nightclub', '2026-11-15'), bad))
    bad = rec('Marquee Dayclub', '2026-11-15'); bad['verified'] = False
    with pytest.raises(Conflict):
        q.validate_completion(raw(), meta(rec('OMNIA Nightclub', '2026-11-15'), bad))


def test_mdc_extra_never_on_drais_jobs():
    r = raw(venues=("Drai's After Hours",), start='2026-11-21')
    m = meta(dict(verified=True, venue="Drai's After Hours", date='2026-11-21', system='email_only', party=dict(PARTY)),
             rec('Marquee Dayclub', '2026-11-21'))
    with pytest.raises(Conflict):
        q.validate_completion(r, m)


# --- owner-approved completion from saved receipts -------------------------
from datetime import datetime  # noqa: E402
import socket  # noqa: E402
from unittest.mock import Mock  # noqa: E402

NOW = datetime(2026, 11, 15, 2, 0, tzinfo=q.PHOENIX).timestamp()
AUTH = {'Authorization': 'Bearer ' + 't' * 32}


@pytest.fixture
def qenv(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', 't' * 32)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))
    return q.store()


def refused_job(s, r, records):
    """Worker outcome: booked + emailed, completion refused -> manual with the receipts saved."""
    import json
    full = dict(r, name='Mock Guest', first_name='Mock', last_name='Guest', email='g@example.invalid')
    s.enqueue('mock', full, NOW)
    m = {'worker': 'railway', **meta(*records)}
    with s.connect() as db:
        db.execute("UPDATE queue_jobs SET state='manual',reason=?,metadata=? WHERE ref='mock'",
                   ('Booked and emailed but completion refused', json.dumps({'resolution': m})))
    assert s.get('mock')['state'] == 'manual'


def test_owner_complete_from_saved_mdc_receipts(qenv, monkeypatch):
    refused_job(qenv, raw(), [rec('OMNIA Nightclub', '2026-11-15'), rec('Marquee Dayclub', '2026-11-15')])
    assert q.handle('POST', '/queue/owner-complete/mock', AUTH, {}, s=qenv, now=NOW)[0] == 409  # approval required
    code, row = q.handle('POST', '/queue/owner-complete/mock', AUTH, {'owner_approval': 'pt992u'}, s=qenv, now=NOW)
    assert code == 200 and row['state'] == 'done'
    assert q.handle('POST', '/queue/owner-complete/mock', AUTH, {'owner_approval': 'pt992u'}, s=qenv, now=NOW)[0] == 409  # done: not manual


def test_owner_complete_refuses_fallback_venue(qenv):
    refused_job(qenv, raw(venues=('Hakkasan Nightclub',), start='2026-11-20'), [rec('JEWEL Nightclub', '2026-11-20')])
    code, body = q.handle('POST', '/queue/owner-complete/mock', AUTH, {'owner_approval': 'pt992u'}, s=qenv, now=NOW)
    assert code == 409 and 'exact source date/venue' in body['error']
    assert qenv.get('mock')['state'] == 'manual'


def test_owner_requeue_pulls_retry_forward(qenv):
    import json
    qenv.enqueue('r1', dict(raw(venues=('TAO Nightclub',)), name='M G', email='g@example.invalid'), NOW)
    with qenv.connect() as db:
        db.execute("UPDATE queue_jobs SET state='retry',due=? WHERE ref='r1'", (NOW + 3600,))
    code, row = q.handle('POST', '/queue/requeue/r1', AUTH, {'owner_approval': 'pt995u'}, s=qenv, now=NOW)
    assert code == 200 and row['state'] == 'queued' and row['due'] <= NOW
    # A retry job with submission progress is never requeued.
    qenv.enqueue('r2', dict(raw(), name='M G', email='h@example.invalid'), NOW)
    with qenv.connect() as db:
        db.execute("UPDATE queue_jobs SET state='retry',metadata=? WHERE ref='r2'",
                   (json.dumps({'progress': {'submitting': {'OMNIA Nightclub|2026-11-15': True}}}),))
    assert q.handle('POST', '/queue/requeue/r2', AUTH, {'owner_approval': 'pt995u'}, s=qenv, now=NOW)[0] == 409

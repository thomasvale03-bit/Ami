"""Nov 16 2026 (Thomas: "I cant keep finding these issues"): booked jobs must
not hide in manual. Fallback venues complete on the exact night, duplicate
guest emails complete with the prior receipt, and venue changes / same-night
double bookings send ONE team@ alert. Nothing is canceled or rebooked."""
from datetime import datetime
import json
import socket
from unittest.mock import Mock

import pytest

import queue_flags
import queue_service as q
from queue_service import Conflict
from tests.test_booking_worker import InProcessApi, make, raw as wraw, NOW, TOKEN  # noqa: F401

PARTY = {'male_count': 0, 'female_count': 2}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def raw(venues=('OMNIA Nightclub',), start='2026-11-23', end=None, email='miia@example.invalid', name='Miia Sara Elkabes'):
    return dict(name=name, email=email, venues=list(venues), start_date=start, end_date=end or start,
                male_count='0', female_count='2', phone='0534313369', authorization='YES',
                submitted_at='2026-11-16T08:07:00-07:00')


def rec(venue, night='2026-11-23', order='ORDER', **kw):
    return dict(verified=True, venue=venue, date=night, order_id=order, party=dict(PARTY), **kw)


def meta(*records, receipts=('guest', 'team')):
    return {'evidence': {'verified': True, 'bookings': list(records),
                         'email_receipts': [dict(kind=k, id=k.upper()) for k in receipts]}}


# --- 1. fallback completion -------------------------------------------------

def test_omnia_to_marquee_fallback_same_night_accepted_and_recorded():
    out = q.validate_completion(raw(), meta(rec('Marquee Nightclub', order='c423', fallback=True)))
    assert out['venue_changes'] == [{'requested_venue': 'OMNIA Nightclub', 'booked_venue': 'Marquee Nightclub',
                                     'date': '2026-11-23', 'order_id': 'c423'}]


def test_omnia_to_tao_move_accepted():
    out = q.validate_completion(raw(start='2026-11-15'), meta(rec('TAO Nightclub', '2026-11-15', fallback=True)))
    assert out['venue_changes'][0]['booked_venue'] == 'TAO Nightclub'


def test_fallback_never_moves_the_date():
    with pytest.raises(Conflict, match='exact source date/venue'):
        q.validate_completion(raw(), meta(rec('Marquee Nightclub', '2026-11-24', fallback=True)))


def test_fallback_plus_requested_same_night_refused():
    with pytest.raises(Conflict, match='no unbooked requested venue'):
        q.validate_completion(raw(), meta(rec('OMNIA Nightclub', order='a'), rec('Marquee Nightclub', order='b', fallback=True)))


def test_drais_never_falls_back():
    with pytest.raises(Conflict):
        q.validate_completion(raw(venues=("Drai's",)), meta(rec('TAO Nightclub', fallback=True)))


def test_fallback_needs_every_night_covered():
    r = raw(start='2026-11-23', end='2026-11-24')
    with pytest.raises(Conflict, match='All exact source'):
        q.validate_completion(r, meta(rec('Marquee Nightclub', fallback=True)))


def test_live_completion_with_fallback_marks_done(tmp_path):
    s = q.store()
    s.enqueue('fb', raw(), NOW)
    job = q.handle('POST', '/queue/claim', {'Authorization': 'Bearer ' + TOKEN}, {'limit': 1}, s=s, now=NOW)[1]['jobs'][0]
    code, row = q.handle('POST', '/queue/jobs/fb', {'Authorization': 'Bearer ' + TOKEN},
                         {'claim_token': job['claim_token'], 'action': 'complete',
                          'metadata': meta(rec('Marquee Nightclub', order='c423', fallback=True))}, s=s, now=NOW)
    assert code == 200 and row['state'] == 'done'
    assert row['metadata']['venue_changes'][0]['order_id'] == 'c423'


# --- 2. duplicate guest email ------------------------------------------------

def test_duplicate_guest_email_covering_same_orders_completes_with_prior_receipt(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(), NOW)
    api.send_responses = [(200, {'result': {'sent': False, 'duplicate': True,
                                            'prior': {'id': 'gmail:abc', 'source': 'gmail_sent', 'order_ids': ['ORDER-1']}}})]
    assert worker.run_once()['status'] == 'done'
    receipts = {r['kind']: r['id'] for r in s.get('a')['metadata']['evidence']['email_receipts']}
    assert receipts['guest'] == 'gmail:abc' and receipts['team'].startswith('MSG-')
    assert all(p.get('report_prior') for p in api.sent)
    assert len(api.sent) == 2  # one guest attempt (refused as duplicate), one team email


def test_second_order_same_night_sends_updated_guest_and_team(tmp_path):
    """Samantha Nov 16: the earlier email listed another order, so the new
    order gets an Updated: guest confirmation and its own team notice."""
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(), NOW)
    other = {'id': 'MID-0', 'source': 'branded_log', 'order_ids': ['OTHER-ORDER']}
    api.send_responses = [(200, {'result': {'sent': False, 'duplicate': True, 'prior': other}}),
                          (200, {'result': {'sent': True, 'id': 'UPD-G'}}),
                          (200, {'result': {'sent': False, 'duplicate': True, 'prior': other}}),
                          (200, {'result': {'sent': True, 'id': 'UPD-T'}})]
    assert worker.run_once()['status'] == 'done'
    subjects = [p['subject'] for p in api.sent]
    assert subjects[1] == "Updated: You're on the Playmaker Entertainment guest list for November 25 (Order ORDER-1)"
    assert subjects[3].startswith('Updated: Signed up: Mock Guest') and subjects[3].endswith('(Order ORDER-1)')
    assert 'Updated confirmation' in api.sent[1]['text'] and 'Updated confirmation' in api.sent[1]['html']
    receipts = {r['kind']: r['id'] for r in s.get('a')['metadata']['evidence']['email_receipts']}
    assert receipts == {'guest': 'UPD-G', 'team': 'UPD-T'}


def test_unknown_prior_coverage_sends_updated(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(), NOW)
    api.send_responses = [(200, {'result': {'sent': False, 'duplicate': True, 'prior': {'id': 'branded-log:x', 'order_ids': None}}})]
    assert worker.run_once()['status'] == 'done'
    assert api.sent[1]['subject'].startswith('Updated: ')


def test_updated_also_already_sent_reuses_it_and_never_loops(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(), NOW)
    dup = (200, {'result': {'sent': False, 'duplicate': True, 'prior': {'id': 'P1', 'order_ids': []}}})
    api.send_responses = [dup, (200, {'result': {'sent': False, 'duplicate': True, 'prior': {'id': 'P2', 'order_ids': ['ORDER-1']}}})]
    assert worker.run_once()['status'] == 'done'
    assert {r['kind']: r['id'] for r in s.get('a')['metadata']['evidence']['email_receipts']}['guest'] == 'P2'
    assert len(api.sent) == 3


def test_duplicate_without_prior_id_stays_manual(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(), NOW)
    nothing = (200, {'result': {'sent': False, 'duplicate': True, 'prior': None}})
    api.send_responses = [nothing, nothing]
    assert worker.run_once()['status'] == 'manual'


def test_added_venue_after_confirmation_sends_updated(tmp_path):
    """Angela Nov 16: JEWEL booked and emailed earlier; Marquee Dayclub added
    later must not reuse the old receipts silently."""
    import time
    import booking_worker as bw
    from tests.test_booking_worker import FakeTao
    # Box worker FakeTao: Marquee Dayclub counts as posted only with dayclubs=True.
    s, api, worker = make(tmp_path, tao=FakeTao(dayclubs=True))
    s.enqueue('a', wraw(venues=['Hakkasan Nightclub', 'Marquee Dayclub']), NOW)
    old = {'verified': True, 'venue': 'Hakkasan Nightclub', 'date': '2026-11-25', 'order_id': 'OLD-1',
           'party': {'male_count': 1, 'female_count': 2}}
    worker.ledger.put(bw.night_key('a', 'Hakkasan Nightclub', '2026-11-25'), {'state': 'booked', 'record': old, 'meta': {}})
    time.sleep(0.01)
    worker.ledger.put('a|emails', {'receipts': {'guest': 'G0', 'team': 'T0'}})
    time.sleep(0.01)
    assert worker.run_once()['status'] == 'done'
    assert len(worker.tao.submits) == 1  # only the new venue was booked
    assert [p['subject'].split(':')[0] for p in api.sent] == ['Updated', 'Updated']
    assert 'OLD-1' in api.sent[0]['text'] and 'ORDER-1' in api.sent[0]['text']
    receipts = {r['kind']: r['id'] for r in s.get('a')['metadata']['evidence']['email_receipts']}
    assert receipts['guest'] != 'G0' and receipts['team'] != 'T0'


def test_saved_receipts_covering_all_orders_are_reused(tmp_path):
    import booking_worker as bw
    import time
    s, api, worker = make(tmp_path)
    s.enqueue('a', wraw(venues=['Hakkasan Nightclub']), NOW)
    old = {'verified': True, 'venue': 'Hakkasan Nightclub', 'date': '2026-11-25', 'order_id': 'OLD-1',
           'party': {'male_count': 1, 'female_count': 2}}
    worker.ledger.put(bw.night_key('a', 'Hakkasan Nightclub', '2026-11-25'), {'state': 'booked', 'record': old, 'meta': {}})
    time.sleep(0.01)
    worker.ledger.put('a|emails', {'receipts': {'guest': 'G0', 'team': 'T0'}})
    assert worker.run_once()['status'] == 'done'
    assert api.sent == []


def test_branded_log_records_order_ids(monkeypatch):
    import gmail_client
    gmail_client.record_branded_sent('g@example.invalid', 'Subj', 'x Order ID abcdef12-3456 y Order ID 99887766', message_id='M1')
    entry = gmail_client._branded_sent_entry('to:g@example.invalid subject:"Subj"')
    assert entry['order_ids'] == ['99887766', 'abcdef12-3456'] and entry['id'] == 'M1'


def test_send_branded_reports_prior_from_send_log(monkeypatch):
    import gmail_client
    import posh_webhook as pw
    monkeypatch.setattr(pw, '_branded_times', [])
    monkeypatch.setattr(pw, 'zoho_configured', lambda: True)
    monkeypatch.setattr(pw, '_zoho_send', lambda *a, **k: 'MID-1@playmakerentertainment.com')

    class G:
        def users(self): return self
        def messages(self): return self
        def list(self, **kw): return Mock(execute=lambda: {})
    p = {'to': 'g@example.invalid', 'subject': 'Hello there', 'html': '<p>x</p>', 'text': 'x', 'report_prior': True}
    assert pw.send_branded(G(), dict(p))[1]['id'] == 'MID-1@playmakerentertainment.com'
    pw._zoho_sent.clear()
    code, result = pw.send_branded(G(), dict(p))
    assert code == 200 and result['duplicate'] is True
    assert result['prior']['id'] == 'MID-1@playmakerentertainment.com'
    p.pop('report_prior')
    assert pw.send_branded(G(), dict(p)) == (200, 'duplicate (already in Sent)')  # old callers unchanged
    assert gmail_client.prior_sent(G(), 'to:nobody@example.invalid subject:"x"') is None


# --- 4. owner reconcile ------------------------------------------------------

def _manual(s, ref, r, bookings, emails=None, evidence=None, state='manual'):
    s.enqueue(ref, r, NOW)
    m = {'progress': {'bookings': {f"{b['venue']}|{b['date']}": b for b in bookings}}}
    if emails:
        m['progress']['emails'] = emails
    if evidence:
        m['resolution'] = {'evidence': evidence}
    with s.connect() as db:
        db.execute('UPDATE queue_jobs SET state=?,metadata=?,reason=? WHERE ref=?', (state, json.dumps(m), 'x', ref))


def test_reconcile_duplicate_guest_job_with_prior_lookup():
    s = q.store()
    _manual(s, 'j2', raw(venues=('Marquee Nightclub',)), [rec('Marquee Nightclub', order='1c22')])
    row = s.owner_reconcile('j2', 't1002u', NOW, lookup_prior=lambda r, k: {'id': 'gmail:prior', 'source': 'gmail_sent'})
    assert row['state'] == 'done'
    assert row['metadata']['owner_reconcile']['team_receipt_missing'] is True
    assert row['metadata']['evidence']['email_receipts'][0]['id'] == 'gmail:prior'


def test_reconcile_fallback_job_from_saved_evidence():
    s = q.store()
    b = rec('Marquee Nightclub', order='c423', fallback=True)
    _manual(s, 'j1', raw(), [b], emails={'guest': 'G1', 'team': 'T1'},
            evidence={'verified': True, 'bookings': [b], 'email_receipts': [{'kind': 'guest', 'id': 'G1'}, {'kind': 'team', 'id': 'T1'}]})
    row = s.owner_reconcile('j1', 't1002u', NOW)
    assert row['state'] == 'done' and row['metadata']['venue_changes'][0]['booked_venue'] == 'Marquee Nightclub'


def test_reconcile_refuses_partial_and_unbooked_and_no_receipt():
    s = q.store()
    _manual(s, 'p', raw(start='2026-11-23', end='2026-11-24'), [rec('OMNIA Nightclub', order='x')], emails={'guest': 'G'})
    with pytest.raises(Conflict, match='All exact source'):
        s.owner_reconcile('p', 'ok', NOW)
    assert s.get('p')['state'] == 'manual'
    s.enqueue('n', raw(), NOW)
    with s.connect() as db:
        db.execute("UPDATE queue_jobs SET state='manual' WHERE ref='n'")
    with pytest.raises(Conflict, match='No verified booking'):
        s.owner_reconcile('n', 'ok', NOW)
    _manual(s, 'r', raw(), [rec('OMNIA Nightclub', order='y')])
    with pytest.raises(Conflict, match='No guest email receipt'):
        s.owner_reconcile('r', 'ok', NOW, lookup_prior=lambda r, k: None)
    with pytest.raises(Conflict, match='approval'):
        s.owner_reconcile('r', '', NOW)


def test_reconcile_route_dispatches_alert(_no_flag_alert_threads, monkeypatch):
    s = q.store()
    _manual(s, 'j2', raw(venues=('Marquee Nightclub',)), [rec('Marquee Nightclub', order='1c22')], emails={'guest': 'G'})
    code, row = q.handle('POST', '/queue/owner-reconcile/j2', {'Authorization': 'Bearer ' + TOKEN},
                         {'owner_approval': 't1002u'}, s=s, now=NOW)
    assert code == 200 and row['state'] == 'done'
    assert _no_flag_alert_threads == ['j2']


# --- 3. flags + alert ---------------------------------------------------------

def _elkabes(s):
    b1 = rec('Marquee Nightclub', order='c423', fallback=True)
    _manual(s, 'j1', raw(), [b1], emails={'guest': 'G1', 'team': 'T1'})
    _manual(s, 'j2', raw(venues=('Marquee Nightclub',)), [rec('Marquee Nightclub', order='1c22')])
    _manual(s, 'j3', raw(start='2026-11-24'), [rec('OMNIA Nightclub', '2026-11-24', order='31eb'),
                                               rec('Marquee Dayclub', '2026-11-24', order='mdc')], emails={'guest': 'G'})


def test_upcoming_flags_lists_changes_and_doubles():
    s = q.store()
    _elkabes(s)
    flags = queue_flags.upcoming_flags(s, '2026-11-16')
    assert [(c['ref'], c['booked_venue'], c['order_id']) for c in flags['venue_changes']] == [('j1', 'Marquee Nightclub', 'c423')]
    assert len(flags['double_bookings']) == 1  # MDC add-on on Nov 24 is not a double
    d = flags['double_bookings'][0]
    assert d['date'] == '2026-11-23' and {o['order_id'] for o in d['orders']} == {'c423', '1c22'}
    assert queue_flags.upcoming_flags(s, '2026-11-24')['counts'] == {'venue_changes': 0, 'double_bookings': 0}
    code, out = q.handle('GET', '/queue/flags?from=2026-11-16', {'Authorization': 'Bearer ' + TOKEN}, None, s=s, now=NOW)
    assert code == 200 and out['counts'] == {'venue_changes': 1, 'double_bookings': 1}


def test_alert_payload_one_email_to_team_no_phone_no_jarvis():
    s = q.store()
    _elkabes(s)
    p = queue_flags.alert_payload(s, 'j1', '2026-11-16')
    assert p['to'] == 'team@playmakerentertainment.com'
    assert p['subject'] == 'AMY alert: Venue change + Double booking (2 orders) - Miia Sara Elkabes Mon Nov 23'
    for needle in ('Miia Sara Elkabes', 'OMNIA Nightclub', 'Marquee Nightclub', 'c423', '1c22', 'Monday November 23'):
        assert needle in p['text']
    assert '0534313369' not in p['text'] + p['html'] and 'jarvis' not in (p['text'] + p['subject']).lower()
    assert queue_flags.alert_payload(s, 'j3', '2026-11-16') is None
    assert queue_flags.alert_payload(s, 'j1', '2026-11-24') is None  # past nights are not alerted


def test_send_alert_records_outcome_once_per_job():
    s = q.store()
    _elkabes(s)
    sent = []
    out = queue_flags.send_alert(s, 'j2', NOW, '2026-11-16', lambda p: (sent.append(p), (200, {'sent': True, 'id': 'A1'}))[1])
    assert len(sent) == 1 and out['code'] == 200
    assert s.get('j2')['metadata']['flag_alert']['subject'].startswith('AMY alert: Double booking (2 orders)')
    assert queue_flags.send_alert(s, 'j3', NOW, '2026-11-16', lambda p: sent.append(p)) is None and len(sent) == 1


def test_exceptions_show_booked_orders():
    s = q.store()
    _elkabes(s)
    jobs = {j['ref']: j for j in q.exceptions_summary(s)['jobs']}
    assert jobs['j2']['booked'] == [{'venue': 'Marquee Nightclub', 'date': '2026-11-23', 'order_id': '1c22', 'fallback': False}]


def test_jobs_listing_limit_raised():
    s = q.store()
    for i in range(120):
        s.enqueue(f'r{i}', raw(), NOW)
    code, out = q.handle('GET', '/queue/jobs?state=queued&limit=500', {'Authorization': 'Bearer ' + TOKEN}, None, s=s, now=NOW)
    assert code == 200 and len(out['jobs']) == 120


def test_alert_multi_night_request_uses_a_range():
    s = q.store()
    _manual(s, 'm1', raw(venues=('Hakkasan Nightclub',), start='2026-11-16', end='2026-11-20'),
            [rec('OMNIA Nightclub', '2026-11-17', order='o17')])
    p = queue_flags.alert_payload(s, 'm1', '2026-11-16')
    assert 'Hakkasan Nightclub on Monday November 16 to Friday November 20' in p['text']
    assert p['subject'] == 'AMY alert: Venue change - Miia Sara Elkabes Tue Nov 17'


def test_flag_alert_emails_off_by_default(monkeypatch):
    """Thomas, Nov 16 2026: no alert emails; double booking is fine."""
    monkeypatch.delenv('AMY_QUEUE_FLAG_ALERTS', raising=False)
    assert q.flag_alerts_enabled() is False
    s = q.store()
    _elkabes(s)
    sent = []
    monkeypatch.setattr(q, '_branded_sender', lambda p: sent.append(p))
    assert q.send_flag_alert(s, 'j1', NOW) is None
    code, out = q.handle('POST', '/queue/flags/alert/j1', {'Authorization': 'Bearer ' + TOKEN}, {}, s=s, now=NOW)
    assert code == 409 and sent == []
    # read-only list still works
    code, out = q.handle('GET', '/queue/flags?from=2026-11-16', {'Authorization': 'Bearer ' + TOKEN}, None, s=s, now=NOW)
    assert code == 200 and out['counts']['double_bookings'] == 1

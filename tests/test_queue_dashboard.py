"""Read-only /dashboard tests: auth, sections, and no leaked secrets or phones."""
from datetime import datetime
import urllib.error
import urllib.request

import pytest
import queue_dashboard as dash
import queue_service as q
from worker_store import Store

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=q.PHOENIX).timestamp()
KEY = 'd' * 32


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('DASHBOARD_KEY', KEY)
    monkeypatch.delenv('AMY_QUEUE_API_TOKEN', raising=False)


def raw(**kw):
    base = dict(name='Mock Guest', first_name='Mock', last_name='Guest',
                email='guest@example.invalid', phone='602-555-0100',
                start_date='2026-11-25', end_date='2026-11-25',
                venues=['Marquee Nightclub'], male_count=1, female_count=2,
                source='website', source_message_id='mock',
                authorization='YES', submitted_at='2026-11-10T12:00:00-07:00')
    base.update(kw)
    return base


def job(s, ref, state, raw_payload, *, created=None, due=None, reason='',
        metadata=None, lease_until=None, updated=None):
    created = NOW if created is None else created
    s.enqueue(ref, raw_payload, created, reason if state == 'manual' and reason else '')
    import json, sqlite3
    with s.connect() as db:
        meta = json.dumps(metadata or {})
        due = created if due is None else due
        updated = created if updated is None else updated
        db.execute(
            "UPDATE queue_jobs SET state=?, reason=?, metadata=?, due=?, updated=?, lease_until=? WHERE ref=?",
            (state, reason, meta, due, updated, lease_until, ref),
        )
    return s.get(ref)


def evidence(order='ABCDEFGH-1234-5678-90AB-CDEF12345678', venue='Marquee Nightclub', day='2026-11-25'):
    return {'evidence': {'verified': True, 'bookings': [dict(
        verified=True, venue=venue, date=day, order_id=order,
        party={'male_count': 1, 'female_count': 2})]}}


def page(s, url='/dashboard?key=' + KEY, headers=None, now=NOW, method='GET'):
    return dash.handle(method, url, headers or {}, s=s, now=now)


def test_unauthorized_without_key_or_wrong_key():
    s = q.store()
    assert page(s, '/dashboard')[0] == 401
    assert page(s, '/dashboard?key=wrong')[0] == 401
    assert page(s, '/dashboard?key=' + 'x' * 32)[0] == 401
    code, _, body, extra = page(s, '/dashboard')
    assert 'Unauthorized' in body
    assert 'Set-Cookie' not in extra


def test_unconfigured_key_fails_closed(monkeypatch):
    monkeypatch.delenv('DASHBOARD_KEY')
    code, _, body, _ = page(q.store(), '/dashboard?key=' + KEY)
    assert code == 503
    monkeypatch.setenv('DASHBOARD_KEY', 'short')
    assert page(q.store(), '/dashboard?key=short')[0] == 503


def test_query_key_sets_httponly_cookie_and_renders():
    s = q.store()
    job(s, 'queued-1', 'queued', raw(name='Ana Mendoza'))
    code, ctype, body, extra = page(s)
    assert code == 200
    assert ctype.startswith('text/html')
    cookie = extra['Set-Cookie']
    assert cookie.startswith(dash.COOKIE_NAME + '=' + KEY)
    assert 'HttpOnly' in cookie
    assert 'Secure' in cookie
    assert 'SameSite=Strict' in cookie
    assert 'Path=/dashboard' in cookie
    assert 'Ana Mendoza' in body
    assert 'meta http-equiv="refresh" content="60"' in body
    assert 'MST' in body


def test_cookie_alone_is_enough():
    s = q.store()
    job(s, 'queued-1', 'queued', raw(name='Kirk Branch'))
    code, _, body, extra = page(s, '/dashboard', {'Cookie': f'{dash.COOKIE_NAME}={KEY}'})
    assert code == 200
    assert 'Set-Cookie' not in extra
    assert 'Kirk Branch' in body


def test_does_not_need_queue_api_token(monkeypatch):
    monkeypatch.delenv('AMY_QUEUE_API_TOKEN', raising=False)
    s = q.store()
    assert page(s)[0] == 200


def test_sections_and_counts():
    s = q.store()
    job(s, 'q1', 'queued', raw(name='Queued Guest', start_date='2026-11-16', end_date='2026-11-16'))
    job(s, 'retry-due', 'retry', raw(name='Retry Due', start_date='2026-11-17', end_date='2026-11-17'),
        due=NOW - 10, reason='browser failed')
    job(s, 'retry-wait', 'retry', raw(name='Aamery Fajardo', start_date='2026-12-31', end_date='2026-12-31',
                                      venues=['TAO Nightclub']),
        due=NOW + 3600, reason='TAO Nightclub 2026-12-31: guest list not posted on TAO yet; nothing submitted')
    job(s, 'manual-future', 'manual', raw(name='Future Manual', start_date='2026-11-20', end_date='2026-11-20'),
        reason='Needs a person')
    job(s, 'manual-past', 'manual', raw(name='Old Manual', start_date='2026-11-01', end_date='2026-11-01'),
        reason='Past or invalid source date; never move to tonight')
    job(s, 'manual-recent', 'manual', raw(name='Recent Manual', start_date='2026-11-14', end_date='2026-11-14'),
        reason='Past date')
    job(s, 'uncertain', 'submission_uncertain', raw(name='Uncertain Guest'),
        reason='Click happened, no confirmation')
    job(s, 'stale', 'assigned', raw(name='Stale Lease'), lease_until=NOW - 5)
    job(s, 'live', 'assigned', raw(name='Live Claim'), lease_until=NOW + 3000)
    job(s, 'done-up', 'done', raw(name='Upcoming Booked', start_date='2026-11-16', end_date='2026-11-16'),
        metadata=evidence(order='cd773791-037E-4A3A-8359-13396CE2E1DA', day='2026-11-16'),
        created=NOW - 3600, updated=NOW - 1800)
    job(s, 'done-past', 'done', raw(name='Recent Booked', start_date='2026-11-14', end_date='2026-11-14'),
        metadata=evidence(order='ORDER-14', day='2026-11-14'),
        created=NOW - 3 * 86400, updated=NOW - 2 * 86400)
    job(s, 'retry-later', 'retry', raw(name='Retry Later', start_date='2026-12-01', end_date='2026-12-01'),
        due=NOW + 7200, reason='try again later')

    data = dash.build(s.all_jobs(), NOW)
    by_name = {row['name']: row['section'] for sec in data['sections'] for row in sec['rows']}
    assert by_name['Queued Guest'] == 'attention'
    assert by_name['Retry Due'] == 'attention'
    assert by_name['Future Manual'] == 'attention'
    assert by_name['Uncertain Guest'] == 'attention'
    assert by_name['Stale Lease'] == 'attention'
    assert by_name['Aamery Fajardo'] == 'waiting'
    assert by_name['Upcoming Booked'] == 'upcoming'
    assert by_name['Recent Booked'] == 'recent'
    assert by_name['Recent Manual'] == 'recent'
    assert by_name['Old Manual'] == 'other'
    assert by_name['Live Claim'] == 'other'
    assert by_name['Retry Later'] == 'other'
    assert data['counts']['queued'] == 1
    assert data['counts']['retry'] == 3
    assert data['counts']['manual'] == 3
    assert data['counts']['submission_uncertain'] == 1
    assert data['counts']['assigned'] == 2
    assert data['counts']['done'] == 2

    body = dash.render(data)
    assert 'Needs attention' in body
    assert 'Waiting for list' in body
    assert 'Upcoming booked' in body
    assert 'Recent past' in body
    assert 'Aamery Fajardo' in body
    assert 'cd773791' in body
    assert 'cd773791-037E-4A3A-8359-13396CE2E1DA' not in body
    assert 'Booked in' in body
    assert 'Sitting' in body
    # All states appear in the filter even if count is 0
    for state in dash.KNOWN_STATES:
        assert f'value="{state}"' in body


def test_no_phone_or_claim_token_in_html():
    s = q.store()
    payload = raw(name='Nate Review', email='nate.verylongemailaddress@example.invalid',
                  phone='480-555-0199', account_phone='+15555550199')
    payload['mobile'] = '555-000-1111'
    job(s, 'p1', 'queued', payload)
    # Insert a claim token in the DB; decode()/all_jobs must drop it, and the
    # page must never render one even if it were present.
    with s.connect() as db:
        db.execute("UPDATE queue_jobs SET claim_token=? WHERE ref='p1'", ('CLAIMTOKENSECRETVALUE',))
    row = s.get('p1')
    assert 'claim_token' not in row
    _, _, body, _ = page(s)
    assert '480-555-0199' not in body
    assert '+15555550199' not in body
    assert '555-000-1111' not in body
    assert '602-555-0100' not in body
    assert 'CLAIMTOKENSECRETVALUE' not in body
    assert 'nate.very\u2026@example.inv\u2026' in body or 'nate.ver' in body
    assert 'nate.verylongemailaddress@example.invalid' not in body


def test_posh_and_website_source_labels():
    s = q.store()
    job(s, 'posh-1', 'queued', raw(name='Posh Guest', source='Posh',
                                   posh={'order_number': '99', 'event_name': 'OMNIA'}))
    job(s, 'web-1', 'queued', raw(name='Web Guest', source='playmaker'))
    data = dash.build(s.all_jobs(), NOW)
    labels = {r['name']: r['source'] for sec in data['sections'] for r in sec['rows']}
    assert labels['Posh Guest'] == 'Posh #99'
    assert labels['Web Guest'] == 'website'


def test_times_are_phoenix_mst():
    # 20:00 Phoenix on Nov 16 2026 is MST (no DST).
    label = dash.fmt_mst(NOW)
    assert label.endswith('MST')
    assert 'Nov 16' in label
    assert '8:00 PM' in label


def test_http_route_401_and_200(monkeypatch, tmp_path):
    import posh_webhook as pw
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('DASHBOARD_KEY', KEY)
    s = q.store()
    job(s, 'q1', 'queued', raw(name='HTTP Guest'))
    server = pw.serve(pw.Intake(lambda: None, 'i@x.com'), port=0, host='127.0.0.1')
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + '/dashboard', timeout=5)
        assert missing.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as wrong:
            urllib.request.urlopen(base + '/dashboard?key=nope', timeout=5)
        assert wrong.value.code == 401
        with urllib.request.urlopen(base + '/dashboard?key=' + KEY, timeout=5) as resp:
            assert resp.status == 200
            cookie = resp.headers.get('Set-Cookie', '')
            assert 'HttpOnly' in cookie
            body = resp.read().decode()
            assert 'HTTP Guest' in body
            assert KEY not in body  # key must not be echoed into the page
        # Cookie from the first visit is enough for a later request without ?key=.
        # Send it explicitly: the cookie is Secure, and this test server is HTTP.
        req = urllib.request.Request(
            base + '/dashboard', headers={'Cookie': f'{dash.COOKIE_NAME}={KEY}'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert 'HTTP Guest' in resp.read().decode()
    finally:
        server.shutdown()
        server.server_close()


def test_handler_log_omits_query_string():
    import logging
    import posh_webhook as pw
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = object.__new__(pw.make_handler(None))
    handler.command = 'GET'
    handler.path = '/dashboard?key=' + KEY
    cap = Capture()
    cap.setFormatter(logging.Formatter('%(message)s'))
    log = logging.getLogger('amy.posh_webhook')
    log.addHandler(cap)
    try:
        handler.log_message('%s %s', 'GET', '/dashboard?key=' + KEY)
        # BaseHTTPRequestHandler.log_message is overridden to log only the path.
        handler.log_message('ignored')
    finally:
        log.removeHandler(cap)
    text = '\n'.join(records)
    assert KEY not in text
    assert '/dashboard' in text or 'GET /dashboard' in text or records

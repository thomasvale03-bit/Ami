"""Thomas (Nov 19 2026): a date-range request is ONE intake submission (one
intake notice + one team@ notice) and ONE job, booked and confirmed together.
A nightly plan names the venue(s) per date."""
from datetime import datetime, timedelta
import socket
import threading
from unittest import mock
from unittest.mock import Mock

import pytest

import gmail_client
import posh_webhook as pw
import queue_service as q
import request_plan

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=q.PHOENIX).timestamp()
AUTH = {'Authorization': 'Bearer ' + 't' * 32}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', 't' * 32)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_PENDING_DIR', str(tmp_path / 'pending'))
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def _days(n):
    return (datetime.now(pw.VEGAS).date() + timedelta(days=n)).isoformat()


def trip_payload(**kw):
    p = {"source": "playmaker", "first_name": "Trip", "last_name": "Guest", "email": "trip@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "women": 3, "men": 0,
         "age_21_confirmed": True, "ref": "T900", "notes": "Birthday Sat",
         "nights": [{"date": _days(10), "venues": ["Hakkasan", "Marquee Dayclub"]},
                    {"date": _days(11), "venues": ["Marquee", "Marquee Dayclub"]},
                    {"date": _days(12), "venue": "OMNIA + Marquee Dayclub"},
                    {"date": _days(13), "venue": "Marquee Dayclub"}]}
    p.update(kw)
    return p


def test_plan_payload_validates_as_one_request():
    clean, err = pw.validate_website(trip_payload())
    assert err is None
    assert clean["night"].isoformat() == _days(10) and clean["end"].isoformat() == _days(13)
    assert clean["venues"] == ["Hakkasan", "Marquee Dayclub", "Marquee", "OMNIA"]
    text = pw.to_website_text(clean)
    assert "Requested venues: Hakkasan, Marquee Dayclub, Marquee, OMNIA" in text
    assert f"Nightly plan: {_days(10)} Hakkasan + Marquee Dayclub; " in text
    raw = gmail_client.parse_request("m1", text)
    assert raw["itinerary"][_days(12)] == ["OMNIA Nightclub", "Marquee Dayclub"]
    assert raw["start_date"] == _days(10) and raw["end_date"] == _days(13)
    assert set(raw["venues"]) == {"Hakkasan Nightclub", "Marquee Dayclub", "Marquee Nightclub", "OMNIA Nightclub"}
    assert "_action_needed" not in raw
    assert len(request_plan.requested_pairs(raw)) == 7


def test_plan_validation_errors():
    assert pw.validate_website(trip_payload(nights="tomorrow Hakkasan"))[1].startswith("bad nightly plan")
    assert pw.validate_website(trip_payload(nights=[{"date": _days(10), "venue": "Nowhere"}]))[1] == "bad nightly plan venue"
    # Ghostbar is confirmation-email only like ZOUK (Nov 24 2026): fine alone, never mixed.
    assert pw.validate_website(trip_payload(nights=[{"date": _days(10), "venue": "Ghostbar"}]))[1] is None
    assert pw.validate_website(trip_payload(nights=[{"date": _days(10), "venue": "Ghostbar"},
                                                    {"date": _days(11), "venue": "Hakkasan"}]))[1] == \
        "Ghostbar must be its own request"
    assert pw.validate_website(trip_payload(nights=[{"date": _days(10), "venue": "ZOUK"},
                                                    {"date": _days(11), "venue": "Hakkasan"}]))[1] == \
        "ZOUK must be its own request"
    assert pw.validate_website(trip_payload(nights=[{"date": _days(10), "venue": "Hakkasan"},
                                                    {"date": _days(11), "venue": "Riot House"}]))[1] == \
        "nightly plan venues must all be in one city"
    assert pw.validate_website(trip_payload(date=_days(11)))[1] == "nightly plan outside the date range"


def test_unreadable_plan_line_goes_to_review():
    clean, _ = pw.validate_website(trip_payload())
    text = pw.to_website_text(clean).replace(f"Nightly plan: {_days(10)}", "Nightly plan: soon")
    raw = gmail_client.parse_request("m1", text)
    assert raw.get("_action_needed") and "itinerary" not in raw


def test_accept_sends_one_intake_notice_and_one_team_email():
    svc = mock.MagicMock()
    intake = mock.MagicMock(dry_run=True, intake_email="intake@example.com", _seen=set())
    intake.dry_run = False
    intake._lock = threading.Lock()
    intake.service.return_value = svc
    with mock.patch.object(gmail_client, "send_once", return_value=True) as so, \
            mock.patch.object(q, "enabled", return_value=False):
        assert pw.accept_website(intake, trip_payload()) == (200, "queued")
        # Same request again: deduped, nothing sent twice.
        assert pw.accept_website(intake, trip_payload())[1].startswith("duplicate")
    svc.users().messages().insert.assert_called_once()
    so.assert_called_once()
    subject = so.call_args[0][3]
    assert subject == f"New guest list request submission - Playmaker Hakkasan +3 {_days(10)}"


def _plan_raw():
    plan = {"2026-11-25": ["Hakkasan Nightclub", "Marquee Dayclub"], "2026-11-26": ["TAO Nightclub"]}
    return dict(name='Mock Guest', first_name='Mock', last_name='Guest', email='guest@example.invalid',
                start_date='2026-11-25', end_date='2026-11-26', itinerary=plan,
                venues=request_plan.plan_venues(plan), male_count=1, female_count=2,
                source_message_id='mock', authorization='YES', submitted_at='2026-11-10T12:00:00-07:00')


def _rec(venue, day, n):
    return dict(verified=True, venue=venue, date=day, order_id=f'O-{n}', party={'male_count': 1, 'female_count': 2})


RECEIPTS = [dict(kind='guest', id='G'), dict(kind='team', id='T')]


def test_completion_follows_the_nightly_plan():
    raw = _plan_raw()
    ok = [_rec('Hakkasan Nightclub', '2026-11-25', 1), _rec('Marquee Dayclub', '2026-11-25', 2),
          _rec('TAO Nightclub', '2026-11-26', 3)]
    q.validate_completion(raw, {'evidence': {'verified': True, 'bookings': ok, 'email_receipts': RECEIPTS}})
    # Marquee Dayclub add-on on the TAO night is still accepted.
    q.validate_completion(raw, {'evidence': {'verified': True, 'email_receipts': RECEIPTS,
                                             'bookings': ok + [_rec('Marquee Dayclub', '2026-11-26', 4)]}})
    # Missing a planned night: refused.
    with pytest.raises(q.Conflict):
        q.validate_completion(raw, {'evidence': {'verified': True, 'bookings': ok[:2], 'email_receipts': RECEIPTS}})


def test_plan_outside_range_is_manual():
    raw = _plan_raw()
    raw['itinerary'] = {**raw['itinerary'], '2026-12-01': ['TAO Nightclub']}
    assert 'outside the request range' in q.source_policy('x', raw, datetime(2026, 11, 10).date())


def test_unposted_retry_keeps_waiting_past_three_claims():
    s = q.store()
    s.enqueue('mock', _plan_raw(), NOW)
    now = NOW
    for i in range(5):
        code, res = q.handle('POST', '/queue/claim', AUTH, {}, s=s, now=now)
        token = res['jobs'][0]['claim_token']
        code, row = q.handle('POST', '/queue/jobs/mock', AUTH, dict(
            claim_token=token, action='retry', reason='TAO Nightclub 2026-11-26: guest list not posted',
            metadata={'submission_attempted': False, 'kind': 'unposted'}), s=s, now=now)
        assert code == 200 and row['state'] == 'retry', row
        now += 3600
    # Any other pre-submit failure keeps the 3-claim limit.
    code, res = q.handle('POST', '/queue/claim', AUTH, {}, s=s, now=now)
    token = res['jobs'][0]['claim_token']
    code, row = q.handle('POST', '/queue/jobs/mock', AUTH, dict(
        claim_token=token, action='retry', reason='browser error', metadata={'submission_attempted': False}),
        s=s, now=now)
    assert row['state'] == 'manual'

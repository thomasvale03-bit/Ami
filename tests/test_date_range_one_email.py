"""Thomas (Nov 19 2026): "The request had a date range. We dont need mutiple
emails to be submitted or sent. They can all be put in one email."

A date-range trip is ONE job (nightly plan: venue(s) per date). The worker books
every night and sends ONE guest + ONE team email listing all of them. Nights
whose list is not posted yet are named as "still to come"; when they book, ONE
Updated email covers the whole trip."""
from datetime import date
from unittest.mock import Mock
import socket

import pytest

import request_plan
import queue_service as q
from tests.test_booking_worker import TOKEN, NOW, make, raw
from tests.test_dark_night_fallback import MapTao

PLAN = {
    "2026-11-25": ["Hakkasan Nightclub", "Marquee Dayclub"],
    "2026-11-26": ["TAO Nightclub", "Marquee Dayclub"],
    "2026-11-27": ["Marquee Nightclub", "Marquee Dayclub"],
    "2026-11-28": ["OMNIA Nightclub", "Marquee Dayclub"],
    "2026-11-29": ["Marquee Dayclub"],
}
ALL = {(v, d) for d, vs in PLAN.items() for v in vs}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def trip(**kw):
    base = dict(venues=request_plan.plan_venues(PLAN), start_date="2026-11-25", end_date="2026-11-29",
                itinerary=PLAN)
    base.update(kw)
    return raw(**base)


def test_requested_pairs_follow_the_plan():
    assert set(request_plan.requested_pairs(trip())) == ALL
    # No plan: unchanged meaning (every venue every night).
    assert len(request_plan.requested_pairs(raw(venues=["Hakkasan Nightclub", "Marquee Dayclub"],
                                                start_date="2026-11-25", end_date="2026-11-26"))) == 4


def test_plan_text_round_trip():
    text = request_plan.format_plan(PLAN)
    assert "," not in text
    assert request_plan.parse_plan(text) == PLAN
    with pytest.raises(ValueError):
        request_plan.parse_plan("next friday Hakkasan")


def test_plan_problems_go_to_review():
    assert request_plan.plan_problem(trip()) == ""
    bad = trip(itinerary={**PLAN, "2026-12-05": ["Hakkasan Nightclub"]})
    assert "outside the request range" in request_plan.plan_problem(bad)
    bad = trip(venues=["Hakkasan Nightclub"])
    assert "not in the requested venues" in request_plan.plan_problem(bad)
    assert q.source_policy("x", bad, date(2026, 11, 10))


def test_whole_trip_books_with_one_guest_and_one_team_email(tmp_path):
    s, api, worker = make(tmp_path, tao=MapTao(ALL))
    s.enqueue("trip", trip(), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    booked = {(b["venue"], b["date"]) for b in s.get("trip")["metadata"]["evidence"]["bookings"]}
    assert booked == ALL
    assert [p["to"] for p in api.sent] == ["guest@example.invalid", "team@playmakerentertainment.com"]
    guest, team = api.sent
    assert guest["subject"] == "You're on the Playmaker Entertainment guest list for November 25 to 29"
    assert not guest["subject"].startswith("Updated")
    for n in range(1, 10):
        assert f"ORDER-{n}" in guest["text"] and f"ORDER-{n}" in team["text"]
    assert "Still to come" not in guest["text"]
    assert worker.run_once()["status"] == "idle"


def test_unposted_night_is_named_then_one_updated_email(tmp_path):
    waiting = ("Marquee Dayclub", "2026-11-29")
    tao = MapTao(ALL - {waiting})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("trip", trip(), NOW)

    first = worker.run_once()
    assert first["status"] == "retry", first
    assert len(api.sent) == 2  # one guest + one team for the 8 booked nights
    assert "Still to come: Sunday November 29 - Marquee Dayclub" in api.sent[0]["text"]
    assert "ORDER-8" in api.sent[0]["text"]

    # Next hourly pass, still not posted: no new email, no resubmits.
    later = NOW + 3600
    api.now, worker.now = later, (lambda: later)
    second = worker.run_once()
    assert second["status"] == "retry", second
    assert len(api.sent) == 2
    assert len(tao.submits) == 8

    # Still waiting well past the old 3-claim limit: it keeps waiting, never manual.
    for i in range(2, 5):
        t = NOW + 3600 * i
        api.now, worker.now = t, (lambda t=t: t)
        assert worker.run_once()["status"] == "retry"
    assert s.get("trip")["state"] == "retry"
    assert len(api.sent) == 2

    # The list opens: book it and send ONE Updated email listing the whole trip.
    tao.open_nights.add(waiting)
    t = NOW + 3600 * 6
    api.now, worker.now = t, (lambda: t)
    done = worker.run_once()
    assert done["status"] == "done", done
    assert len(api.sent) == 4
    guest, team = api.sent[2:]
    assert guest["subject"].startswith("Updated: ") and team["subject"].startswith("Updated: ")
    assert "Sunday November 29 - Marquee Dayclub" in guest["text"]
    assert "Still to come" not in guest["text"]
    for n in range(1, 10):
        assert f"ORDER-{n}" in guest["text"]
    assert len(tao.submits) == 9

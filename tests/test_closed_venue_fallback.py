"""Thomas (Nov 22 2026): "When the venue is not open, find whatever venue that
is open and book them on there!" A closed / sold-out / dark requested venue
walks the fallback chain (OMNIA first, ... Hakkasan, Marquee, then any other
TAO nightclub) and only holds when nothing has an open list."""
from datetime import date, datetime
from unittest.mock import Mock
import socket

import pytest

import booking_worker as bw
import queue_service as q
from rules_engine import fallback_venues_for
from tests.test_booking_worker import TOKEN, FakeTao, InProcessApi, NOW, make, raw
from tests.test_dark_night_fallback import MapTao


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))


def make_at(tmp_path, when, tao):
    s = q.store()
    api = InProcessApi(s, now=when)
    worker = bw.Worker(api, bw.Ledger(tmp_path / 'w' / 'ledger.json'), tao=tao, mode='live',
                       evidence_dir=tmp_path / 'w', now=lambda: when, sleep=lambda s: None,
                       email_wait_seconds=0, alerts=False)
    return s, api, worker


def booked(s, ref):
    return [(b["venue"], b["date"], bool(b.get("fallback")))
            for b in s.get(ref)["metadata"]["evidence"]["bookings"]]


def test_same_day_friday_omnia_falls_back_instead_of_manual(tmp_path):
    friday = datetime(2026, 11, 13, 18, 30, tzinfo=q.PHOENIX).timestamp()
    tao = MapTao({("OMNIA Nightclub", "2026-11-13"), ("Hakkasan Nightclub", "2026-11-13")})
    s, api, worker = make_at(tmp_path, friday, tao)
    s.enqueue("o", raw(venues=["OMNIA Nightclub"], start_date="2026-11-13", end_date="2026-11-13"), friday)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "o") == [("Hakkasan Nightclub", "2026-11-13", True)]
    assert [ev for ev, _ in worker.tao.submits] == ["Guest List - Hakkasan Nightclub"]
    assert [p["to"] for p in api.sent] == ["guest@example.invalid", "team@playmakerentertainment.com"]


def test_quantity_not_available_falls_back_to_omnia(tmp_path):
    class SoldOutHakkasan(MapTao):
        def submit(self, listing, guest, *, rehearse, screenshot_path, before_submit):
            if "Hakkasan" in listing["event"]:
                self.submits.append((listing["event"], rehearse))
                return {"confirmation_id": None, "verified": False,
                        "reason": "male Guest List quantity 4 is not available"}
            return super().submit(listing, guest, rehearse=rehearse, screenshot_path=screenshot_path,
                                  before_submit=before_submit)

    tao = SoldOutHakkasan({("Hakkasan Nightclub", "2026-11-26"), ("OMNIA Nightclub", "2026-11-26")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-26", end_date="2026-11-26",
                       male_count="4"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "h") == [("OMNIA Nightclub", "2026-11-26", True)]


def test_marquee_dayclub_without_inventory_does_not_park_the_job(tmp_path):
    class NoDayclubRoom(MapTao):
        def submit(self, listing, guest, *, rehearse, screenshot_path, before_submit):
            if "Dayclub" in listing["event"]:
                return {"confirmation_id": None, "verified": False,
                        "reason": "female Guest List quantity 12 is not available"}
            return super().submit(listing, guest, rehearse=rehearse, screenshot_path=screenshot_path,
                                  before_submit=before_submit)

    tao = NoDayclubRoom({("Marquee Nightclub", "2026-11-25"), ("Marquee Dayclub", "2026-11-25")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("m", raw(venues=["Marquee Nightclub"], female_count="12"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "m") == [("Marquee Nightclub", "2026-11-25", False)]


def test_near_night_unposted_while_other_clubs_posted_is_closed(tmp_path):
    # Thu Nov 12 (2 days out): OMNIA has no list but TAO does -> OMNIA is dark.
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("t", raw(venues=["OMNIA Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "t") == [("TAO Nightclub", "2026-11-12", True)]


def test_far_future_unposted_still_holds(tmp_path):
    # Thu Nov 26 is 16 days out: OMNIA not posted yet is a hold, not closed.
    tao = MapTao({("TAO Nightclub", "2026-11-26")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("f", raw(venues=["OMNIA Nightclub"], start_date="2026-11-26", end_date="2026-11-26"), NOW)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert "not posted" in result["reason"]
    assert worker.tao.submits == []


def test_tonight_unposted_with_nothing_open_holds(tmp_path):
    tao = MapTao(set())
    tonight = datetime(2026, 11, 12, 18, 30, tzinfo=q.PHOENIX).timestamp()
    s, api, worker = make_at(tmp_path, tonight, tao)
    s.enqueue("n", raw(venues=["OMNIA Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), tonight)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert worker.tao.submits == []


def test_chain_exhausted_books_any_other_open_tao_club(tmp_path):
    # Fri Nov 20: Marquee requested but sold out; OMNIA, JEWEL, Hakkasan have
    # no list. TAO Nightclub (not on Friday's routing) is the last resort.
    class ClosedMarquee(MapTao):
        def listing(self, venue, night):
            if venue == "Marquee Nightclub":
                return None
            return super().listing(venue, night)

    tao = ClosedMarquee({("Marquee Nightclub", "2026-11-20"), ("TAO Nightclub", "2026-11-20")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("x", raw(venues=["Marquee Nightclub"], start_date="2026-11-20", end_date="2026-11-20"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "x") == [("TAO Nightclub", "2026-11-20", True)]


def test_fallback_books_a_second_signup_even_if_the_guest_is_already_booked(tmp_path):
    # Thomas, Nov 22 2026: each sign-up books its own order (dedupe off).
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("first", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    s.enqueue("second", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12",
                            female_count="5"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "second") == [("TAO Nightclub", "2026-11-12", True)]
    assert len(worker.tao.submits) == 2


def test_dedupe_on_does_not_book_a_guest_twice_for_one_night(tmp_path):
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao, duplicate_check=True)
    s.enqueue("first", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    # Different party for the same night: never a second booking -> manual.
    s.enqueue("second", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12",
                            female_count="5"), NOW)
    result = worker.run_once()
    assert result["status"] == "manual", result
    assert "already has TAO Nightclub booked" in result["reason"]
    assert len(worker.tao.submits) == 1


def test_closed_night_on_a_later_signup_books_its_own_fallback(tmp_path):
    # Multi-night trip split over jobs: Friday Hakkasan is closed, so this
    # sign-up books its own fallback even though another job already has TAO.
    tao = MapTao({("TAO Nightclub", "2026-11-12"), ("Hakkasan Nightclub", "2026-11-13")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("first", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    s.enqueue("trip", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-13"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "trip") == [("TAO Nightclub", "2026-11-12", True), ("Hakkasan Nightclub", "2026-11-13", False)]
    assert len(worker.tao.submits) == 3  # first TAO + this job's TAO fallback + Hakkasan


def test_dedupe_on_reuses_same_party_other_job_booking(tmp_path):
    tao = MapTao({("TAO Nightclub", "2026-11-12"), ("Hakkasan Nightclub", "2026-11-13")})
    s, api, worker = make(tmp_path, tao=tao, duplicate_check=True)
    s.enqueue("first", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    s.enqueue("trip", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-13"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert booked(s, "trip") == [("TAO Nightclub", "2026-11-12", True), ("Hakkasan Nightclub", "2026-11-13", False)]
    assert len(worker.tao.submits) == 2  # TAO once (first job) + Hakkasan


def test_fallback_order_and_same_day_omnia_skip():
    friday = date(2026, 11, 20)
    order = fallback_venues_for(friday)
    assert order[0] == "OMNIA Nightclub"
    assert order[-1] == "TAO Nightclub"  # last resort: any other TAO club
    assert "JEWEL Nightclub" not in fallback_venues_for(date(2026, 11, 27))
    assert "OMNIA Nightclub" not in fallback_venues_for(friday, today=friday)
    assert fallback_venues_for(friday, today=date(2026, 11, 19))[0] == "OMNIA Nightclub"
    # Never the same club two nights running: moved to the end.
    assert fallback_venues_for(friday, "OMNIA Nightclub")[-1] == "OMNIA Nightclub"


def test_second_signup_sends_its_own_confirmation(tmp_path):
    # Two sign-ups for one closed night: each books the open club and emails.
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("a", raw(venues=["OMNIA Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    sent = len(api.sent)
    s.enqueue("b", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    new = api.sent[sent:]
    assert [p["to"] for p in new] == ["guest@example.invalid", "team@playmakerentertainment.com"]
    assert len(worker.tao.submits) == 2


def test_dedupe_on_reused_night_sends_no_repeat_email(tmp_path):
    # Two sign-ups for one closed night: the first job books the open club and
    # emails; the second (no hold email sent) reuses it without a 2nd email.
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao, duplicate_check=True)
    s.enqueue("a", raw(venues=["OMNIA Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    sent = len(api.sent)
    s.enqueue("b", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert len(api.sent) == sent
    assert len(worker.tao.submits) == 1


def test_second_signup_after_hold_email_gets_its_own_confirmation(tmp_path):
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("a", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    s.enqueue("b", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    # The guest was told job b is on hold after job a's confirmation went out.
    worker.ledger.put("b|hold_notified", {"via": "hold_email", "signature": ["x"], "at": NOW + 10})
    sent = len(api.sent)
    result = worker.run_once()
    assert result["status"] == "done", result
    new = api.sent[sent:]
    assert [p["to"] for p in new] == ["guest@example.invalid", "team@playmakerentertainment.com"]
    assert new[0]["subject"].startswith("Updated")
    assert len(worker.tao.submits) == 2


def test_dedupe_on_reused_night_after_hold_gets_updated_confirmation(tmp_path):
    tao = MapTao({("TAO Nightclub", "2026-11-12")})
    s, api, worker = make(tmp_path, tao=tao, duplicate_check=True)
    s.enqueue("a", raw(venues=["TAO Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    assert worker.run_once()["status"] == "done"
    s.enqueue("b", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-12", end_date="2026-11-12"), NOW)
    worker.ledger.put("b|hold_notified", {"via": "hold_email", "signature": ["x"], "at": NOW + 10})
    sent = len(api.sent)
    result = worker.run_once()
    assert result["status"] == "done", result
    new = api.sent[sent:]
    assert [p["to"] for p in new] == ["guest@example.invalid", "team@playmakerentertainment.com"]
    assert new[0]["subject"].startswith("Updated")
    assert len(worker.tao.submits) == 1

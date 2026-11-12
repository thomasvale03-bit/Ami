"""When a requested nightclub is dark that weekday, fall through DEFAULT_ROUTING
instead of sending the night to manual. Lists not yet posted on a night the
venue does operate still retry (or go manual if other nights are already booked)."""
from datetime import date
from unittest.mock import Mock
import socket

import pytest

from rules_engine import scheduled_venues_for, venue_operates_that_night
import worker_emails

from tests.test_booking_worker import TOKEN, FakeTao, NOW, make, raw


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', TOKEN)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    monkeypatch.setenv('AMY_JOBS_FILE', str(tmp_path / 'jobs.json'))
    monkeypatch.setenv('AMY_QUEUE_WORKER_CLAIMS', 'true')
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network')))

NOTICE = "Oh No, You Did Not Meet this Venue's GuestList Cut off. But we signed you up for another amazing venue!"


class MapTao(FakeTao):
    """posted() / listing() vary by (venue, date). Dates may be date or iso str."""

    def __init__(self, open_nights):
        super().__init__()
        self.open_nights = {(v, _iso(n)) for v, n in open_nights}

    def posted(self, venue, night):
        return (venue, _iso(night)) in self.open_nights

    def listing(self, venue, night):
        if not self.posted(venue, night):
            return None
        return super().listing(venue, night)


def _iso(night):
    return night.isoformat() if hasattr(night, "isoformat") else str(night)


def test_scheduled_venues_and_operates():
    sunday = date(2026, 11, 15)
    wednesday = date(2026, 11, 25)
    friday = date(2026, 11, 13)
    assert scheduled_venues_for(sunday) == ["OMNIA Nightclub", "TAO Nightclub"]
    assert not venue_operates_that_night("JEWEL Nightclub", sunday)
    assert venue_operates_that_night("OMNIA Nightclub", sunday)
    assert venue_operates_that_night("TAO Nightclub", sunday)
    assert venue_operates_that_night("Hakkasan Nightclub", wednesday)
    assert venue_operates_that_night("Marquee Nightclub", wednesday)
    assert not venue_operates_that_night("JEWEL Nightclub", wednesday)
    # Friday/Saturday: main TAO clubs still count as operating.
    assert venue_operates_that_night("OMNIA Nightclub", friday)
    assert venue_operates_that_night("JEWEL Nightclub", friday)
    assert venue_operates_that_night("Hakkasan Nightclub", friday)


def test_jewel_sunday_falls_back_to_omnia_and_emails_notice(tmp_path):
    tao = MapTao({
        ("JEWEL Nightclub", "2026-11-13"),
        ("JEWEL Nightclub", "2026-11-14"),
        ("OMNIA Nightclub", "2026-11-15"),
        ("TAO Nightclub", "2026-11-15"),
    })
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("mak", raw(venues=["JEWEL Nightclub"], start_date="2026-11-13", end_date="2026-11-15"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("mak")["metadata"]["evidence"]["bookings"]
    assert [(b["venue"], b["date"]) for b in bookings] == [
        ("JEWEL Nightclub", "2026-11-13"),
        ("JEWEL Nightclub", "2026-11-14"),
        ("OMNIA Nightclub", "2026-11-15"),
    ]
    assert bookings[2].get("fallback") is True
    # OMNIA first, not TAO, even though both were posted.
    assert [ev for ev, _ in worker.tao.submits] == [
        "Guest List - JEWEL Nightclub",
        "Guest List - JEWEL Nightclub",
        "Guest List - OMNIA Nightclub",
    ]
    guest = api.sent[0]
    assert guest["to"] == "guest@example.invalid"
    assert NOTICE in guest["text"]
    assert guest["text"].split("Sunday November 15")[1].startswith(" - OMNIA Nightclub")
    assert NOTICE in guest["text"].split("Sunday November 15", 1)[1]
    assert NOTICE not in guest["text"].split("Sunday November 15", 1)[0]
    assert "Oh No, You Did Not Meet" in guest["html"]
    assert "7025550100" not in guest["text"] and "jarvis" not in guest["to"]


def test_dark_sunday_uses_tao_when_omnia_not_posted(tmp_path):
    tao = MapTao({("TAO Nightclub", "2026-11-15")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("sun", raw(venues=["JEWEL Nightclub"], start_date="2026-11-15", end_date="2026-11-15"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("sun")["metadata"]["evidence"]["bookings"]
    assert bookings[0]["venue"] == "TAO Nightclub" and bookings[0]["date"] == "2026-11-15"
    assert bookings[0].get("fallback") is True
    assert NOTICE in api.sent[0]["text"]


def test_dark_night_retries_when_fallbacks_also_unposted_even_if_other_nights_booked(tmp_path):
    # Fri/Sat JEWEL operate and are posted; Sunday is dark and fallbacks are not up yet.
    tao = MapTao({
        ("JEWEL Nightclub", "2026-11-13"),
        ("JEWEL Nightclub", "2026-11-14"),
    })
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("j", raw(venues=["JEWEL Nightclub"], start_date="2026-11-13", end_date="2026-11-15"), NOW)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert "other nights already booked" not in (result.get("reason") or "")
    assert "DEFAULT_ROUTING" in (result.get("reason") or "")
    assert s.get("j")["state"] == "retry"
    assert api.sent == []
    # Friday and Saturday were booked; Sunday was not submitted.
    assert len(worker.tao.submits) == 2
    ev = (s.get("j").get("metadata") or {}).get("progress") or {}
    assert ev.get("bookings", {}).get("JEWEL Nightclub|2026-11-13", {}).get("order_id")


def test_operating_venue_unposted_still_retries(tmp_path):
    # Wednesday Hakkasan is on DEFAULT_ROUTING; list not posted, nothing else posted.
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert "guest list not posted" in result["reason"]
    assert worker.tao.submits == []
    assert api.sent == []


def test_operating_venue_unposted_retries_when_other_nights_booked(tmp_path):
    # Hakkasan operates Wed and Thu. Wed is posted, Thu is not → retry, never park manual.
    tao = MapTao({("Hakkasan Nightclub", "2026-11-25")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-26"), NOW)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert "guest list not posted" in result["reason"]
    assert "other nights already booked" not in (result.get("reason") or "")
    assert api.sent == []
    assert len(worker.tao.submits) == 1


def test_fallback_skips_previous_night_club(tmp_path):
    # Saturday already booked at OMNIA (same job). Sunday JEWEL is dark;
    # OMNIA and TAO are both posted, but OMNIA was last night so TAO is used.
    tao = MapTao({
        ("OMNIA Nightclub", "2026-11-15"),
        ("TAO Nightclub", "2026-11-15"),
    })
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("x", raw(venues=["JEWEL Nightclub"], start_date="2026-11-14", end_date="2026-11-15"), NOW)
    worker.ledger.put(
        "x|OMNIA Nightclub|2026-11-14",
        {"state": "booked",
         "record": {"verified": True, "venue": "OMNIA Nightclub", "date": "2026-11-14",
                    "order_id": "PRE-1", "party": {"male_count": 1, "female_count": 2},
                    "fallback": True},
         "meta": {"event": "Guest List - OMNIA Nightclub", "fallback": True, "fallback_from": "JEWEL Nightclub"}},
    )
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("x")["metadata"]["evidence"]["bookings"]
    assert [(b["venue"], b["date"]) for b in bookings] == [
        ("OMNIA Nightclub", "2026-11-14"),
        ("TAO Nightclub", "2026-11-15"),
    ]
    assert [ev for ev, _ in worker.tao.submits] == ["Guest List - TAO Nightclub"]


def test_worker_guest_email_notice_only_on_fallback_nights():
    req = {"first_name": "Makayla", "email": "a@b.c", "start_date": "2026-11-13",
           "end_date": "2026-11-15", "female_count": 2, "male_count": 1}
    bookings = [
        {"venue": "JEWEL Nightclub", "date": "2026-11-13", "event": "DJ Buza", "order_id": "A"},
        {"venue": "JEWEL Nightclub", "date": "2026-11-14", "event": "DJ Paradice", "order_id": "B"},
        {"venue": "OMNIA Nightclub", "date": "2026-11-15", "event": "DESEO: Latin Sundays",
         "order_id": "C", "fallback": True},
    ]
    payload = worker_emails.guest_email(req, bookings)
    assert payload["text"].count(NOTICE) == 1
    assert NOTICE in payload["text"].split("Sunday November 15", 1)[1]
    assert NOTICE not in payload["text"].split("Sunday November 15", 1)[0]
    assert "Oh No, You Did Not Meet" in payload["html"]
    assert "Sun, Nov 15" in payload["html"]


def test_closed_requested_falls_back_to_omnia(tmp_path):
    # Friday lists are up but JEWEL is closed; OMNIA is top priority fallback.
    class ClosedJewel(MapTao):
        def listing(self, venue, night):
            if venue == "JEWEL Nightclub":
                return None
            return super().listing(venue, night)

        def posted(self, venue, night):
            return True

    s, api, worker = make(tmp_path, tao=ClosedJewel({("OMNIA Nightclub", "2026-11-13")}))
    # posted() is overridden to True for all, listing None for JEWEL, super listing for others
    worker.tao.open_nights.add(("OMNIA Nightclub", "2026-11-13"))
    s.enqueue("f", raw(venues=["JEWEL Nightclub"], start_date="2026-11-13", end_date="2026-11-13"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("f")["metadata"]["evidence"]["bookings"]
    assert bookings[0]["venue"] == "OMNIA Nightclub" and bookings[0].get("fallback") is True
    assert NOTICE in api.sent[0]["text"]
    assert api.sent[0]["to"] == "guest@example.invalid"
    assert api.sent[1]["to"] == "team@playmakerentertainment.com"
    assert "Phone:" not in api.sent[1]["text"]


def test_access_code_falls_back(tmp_path):
    class CodeThenTao(FakeTao):
        def posted(self, venue, night):
            return True

        def listing(self, venue, night):
            base = super().listing(venue, night)
            if venue == "OMNIA Nightclub":
                return {**base, "access_code_required": True}
            return base

    s, api, worker = make(tmp_path, tao=CodeThenTao())
    s.enqueue("s", raw(venues=["OMNIA Nightclub"], start_date="2026-11-15", end_date="2026-11-15"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("s")["metadata"]["evidence"]["bookings"]
    assert bookings[0]["venue"] == "TAO Nightclub" and bookings[0].get("fallback") is True
    assert NOTICE in api.sent[0]["text"]


def test_jewel_after_nov_21_routes_to_fallback(tmp_path):
    # Sunday Nov 22, 2026: JEWEL is past the cutoff; OMNIA is posted.
    tao = MapTao({("OMNIA Nightclub", "2026-11-22")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("j", raw(venues=["JEWEL Nightclub"], start_date="2026-11-22", end_date="2026-11-22"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("j")["metadata"]["evidence"]["bookings"]
    assert bookings[0]["venue"] == "OMNIA Nightclub" and bookings[0]["date"] == "2026-11-22"
    assert bookings[0].get("fallback") is True


def test_marquee_dayclub_added_when_open(tmp_path):
    tao = MapTao({
        ("Marquee Nightclub", "2026-11-25"),
        ("Marquee Dayclub", "2026-11-25"),
    })
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("m", raw(venues=["Marquee Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("m")["metadata"]["evidence"]["bookings"]
    venues = sorted((b["venue"], b["date"]) for b in bookings)
    assert venues == [
        ("Marquee Dayclub", "2026-11-25"),
        ("Marquee Nightclub", "2026-11-25"),
    ]
    assert "Marquee Dayclub" in api.sent[0]["text"]


def test_marquee_dayclub_skipped_when_not_posted(tmp_path):
    tao = MapTao({("Marquee Nightclub", "2026-11-25")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("m", raw(venues=["Marquee Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("m")["metadata"]["evidence"]["bookings"]
    assert [b["venue"] for b in bookings] == ["Marquee Nightclub"]


def test_lavo_requested_is_booked(tmp_path):
    tao = MapTao({("LAVO Party Brunch", "2026-11-14")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("l", raw(venues=["LAVO Party Brunch"], start_date="2026-11-14", end_date="2026-11-14"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert s.get("l")["metadata"]["evidence"]["bookings"][0]["venue"] == "LAVO Party Brunch"
    assert len(worker.tao.submits) == 1  # no nightclub fallback


def test_source_policy_manual_clubs_and_omnia_jewel_rules():
    import queue_service as q
    today = date(2026, 11, 13)  # Friday
    liv = raw(venues=["LIV"], start_date="2026-11-14", end_date="2026-11-14")
    assert "LIV" in q.source_policy("r", liv, today) or "manual" in q.source_policy("r", liv, today).lower()
    xs = raw(venues=["XS Nightclub"], start_date="2026-11-14", end_date="2026-11-14")
    assert q.source_policy("r", xs, today)
    ebc = raw(venues=["EBC"], start_date="2026-11-14", end_date="2026-11-14")
    assert q.source_policy("r", ebc, today)
    # OMNIA same-day Friday is not treated as closed.
    omnia = raw(venues=["OMNIA Nightclub"], start_date="2026-11-13", end_date="2026-11-13")
    assert q.source_policy("r", omnia, today) == ""
    # JEWEL after Nov 21 is no longer parked at intake; the worker falls back.
    jewel = raw(venues=["JEWEL Nightclub"], start_date="2026-11-22", end_date="2026-11-22")
    assert q.source_policy("r", jewel, today) == ""


def test_fallback_venues_put_omnia_first_and_skip_jewel_after_cutoff():
    from rules_engine import fallback_venues_for
    friday = fallback_venues_for(date(2026, 11, 13))
    assert friday[0] == "OMNIA Nightclub"
    assert "JEWEL Nightclub" in friday
    after = fallback_venues_for(date(2026, 11, 22))
    assert after[0] == "OMNIA Nightclub"
    assert "JEWEL Nightclub" not in after
    # No two nights in a row: OMNIA last night moves it to the end.
    after_omnia = fallback_venues_for(date(2026, 11, 15), previous_night_venue="OMNIA Nightclub")
    assert after_omnia[0] != "OMNIA Nightclub"
    assert after_omnia[-1] == "OMNIA Nightclub"

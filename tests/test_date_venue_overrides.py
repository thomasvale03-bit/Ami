"""Date-specific Vegas venue remaps (e.g. 2026-12-24 -> TAO Nightclub)."""
from datetime import date
import socket
from unittest.mock import Mock

import pytest

from config import rules
import queue_service as q
from tests.test_booking_worker import TOKEN, FakeTao, make, raw


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AMY_QUEUE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMY_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("AMY_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("AMY_QUEUE_WORKER_CLAIMS", "true")
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))


def test_dec24_vegas_nights_remap_to_tao_not_scottsdale():
    d = date(2026, 12, 24)
    for venue in ("OMNIA Nightclub", "Hakkasan Nightclub", "Marquee Nightclub",
                  "JEWEL Nightclub", "LIV Nightclub", "XS Nightclub",
                  "Drai's After Hours", "Marquee Dayclub"):
        assert rules.date_venue_override(d, venue) == "TAO Nightclub"
    assert rules.date_venue_override(d, "TAO Nightclub") is None
    assert rules.date_venue_override(d, "Riot House") is None
    assert rules.date_venue_override(d, "El Hefe") is None
    assert rules.date_venue_override(date(2026, 12, 25), "OMNIA Nightclub") is None


def test_completion_accepts_dec24_override_as_venue_change():
    raw_job = dict(name="Mock Guest", email="guest@example.invalid",
                   venues=["OMNIA Nightclub"], start_date="2026-12-24", end_date="2026-12-24",
                   male_count=1, female_count=2, authorization="YES",
                   submitted_at="2026-11-10T12:00:00-07:00")
    evidence = {"evidence": {"verified": True, "bookings": [
        {"verified": True, "venue": "TAO Nightclub", "date": "2026-12-24",
         "order_id": "TAO-1224", "party": {"male_count": 1, "female_count": 2},
         "fallback": True}],
        "email_receipts": [{"kind": "guest", "id": "G"}, {"kind": "team", "id": "T"}]}}
    out = q.validate_completion(raw_job, evidence)
    assert out["venue_changes"] == [{"requested_venue": "OMNIA Nightclub",
                                     "booked_venue": "TAO Nightclub",
                                     "date": "2026-12-24", "order_id": "TAO-1224"}]


def test_completion_accepts_dec24_drais_override():
    raw_job = dict(name="Mock Guest", email="guest@example.invalid",
                   venues=["Drai's After Hours"], start_date="2026-12-24", end_date="2026-12-24",
                   male_count=1, female_count=2, authorization="YES",
                   submitted_at="2026-11-10T12:00:00-07:00")
    evidence = {"evidence": {"verified": True, "bookings": [
        {"verified": True, "venue": "TAO Nightclub", "date": "2026-12-24",
         "order_id": "TAO-1224", "party": {"male_count": 1, "female_count": 2},
         "fallback": True}],
        "email_receipts": [{"kind": "guest", "id": "G"}, {"kind": "team", "id": "T"}]}}
    out = q.validate_completion(raw_job, evidence)
    assert out["venue_changes"][0]["booked_venue"] == "TAO Nightclub"


def test_worker_books_dec24_omnia_on_tao_and_skips_mdc(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(dayclubs=True))
    s.enqueue("a", raw(start_date="2026-12-24", end_date="2026-12-24",
                       venues=["OMNIA Nightclub"]), 1)
    # make() worker.now is Nov 10, which is fine; FakeTao marks the list posted.
    result = worker.run_once()
    assert result["status"] == "done", result
    bookings = s.get("a")["metadata"]["evidence"]["bookings"]
    assert [b["venue"] for b in bookings] == ["TAO Nightclub"]
    assert bookings[0].get("fallback") is True
    changes = s.get("a")["metadata"]["venue_changes"]
    assert changes[0]["requested_venue"] == "OMNIA Nightclub"
    assert changes[0]["booked_venue"] == "TAO Nightclub"
    assert changes[0]["date"] == "2026-12-24"


def test_worker_retries_dec24_when_tao_list_unposted(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    s.enqueue("a", raw(start_date="2026-12-24", end_date="2026-12-24",
                       venues=["Hakkasan Nightclub"]), 1)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert s.get("a")["state"] == "retry"
    reason = s.get("a")["reason"] or ""
    assert "TAO Nightclub 2026-12-24" in reason and "not posted" in reason
    assert worker.tao.submits == []

"""Weekly *-night-rules.json no_list/closed nights close venue_open_on
and fall back through DEFAULT_ROUTING (Thomas, Nov 24 2026)."""
import json
from datetime import date

import pytest

import liv_rules
from config import rules
from posh_webhook import is_notify_only
from tests.test_booking_worker import NOW, make, raw
from tests.test_dark_night_fallback import NOTICE, MapTao
from tests.test_queue_service import assigned, update


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    import socket
    from unittest.mock import Mock
    from tests.test_booking_worker import TOKEN
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AMY_QUEUE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMY_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("AMY_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("AMY_QUEUE_WORKER_CLAIMS", "true")
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))


def _write(tmp_path, monkeypatch, filename, nights):
    p = tmp_path / filename
    p.write_text(json.dumps({"nights": nights}))
    monkeypatch.setenv("NIGHT_RULES_DIR", str(tmp_path))
    return p


def _queue_raw(**kw):
    body = dict(name="Mock Guest", first_name="Mock", last_name="Guest",
                email="guest@example.invalid", start_date="2026-11-25", end_date="2026-11-25",
                venues=["Marquee Nightclub"], male_count=1, female_count=2,
                source_message_id="mock", authorization="YES",
                submitted_at="2026-11-10T12:00:00-07:00")
    body.update(kw)
    return body


def test_drais_thursday_closed_from_rules_friday_still_open(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "drais-night-rules.json", {
        "Drai's After Hours|2026-11-26": {"no_list": True, "event": "CLOSED"},
        "Drai's After Hours|2026-11-27": {
            "event": "Pasión Latin Night featuring RUDA",
            "women": "even ratio free until 3am",
            "men": "even ratio free until 3am",
        },
    })
    assert rules.venue_open_on("Drai's", date(2026, 11, 26)) is False
    assert rules.venue_open_on("Drai's After Hours", date(2026, 11, 26)) is False
    assert rules.venue_open_on("Drai's", date(2026, 11, 27)) is True
    assert rules.venue_open_on("Drai's", date(2026, 11, 28)) is True
    assert rules.venue_open_on("Drai's", date(2026, 11, 29)) is True
    # Wednesday was already dark on the verified schedule.
    assert rules.venue_open_on("Drai's", date(2026, 11, 25)) is False


def test_missing_or_invalid_rules_file_keeps_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHT_RULES_DIR", str(tmp_path))
    # No file: Thursday stays open (current schedule behavior).
    assert rules.venue_open_on("Drai's", date(2026, 11, 26)) is True
    (tmp_path / "drais-night-rules.json").write_text("{not json")
    assert rules.venue_open_on("Drai's", date(2026, 11, 26)) is True
    # Empty nights object is also a no-op.
    (tmp_path / "drais-night-rules.json").write_text(json.dumps({"nights": {}}))
    assert rules.venue_open_on("Drai's", date(2026, 11, 26)) is True


def test_closed_flag_and_generic_zouk_file(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "zouk-night-rules.json", {
        "ZOUK|2026-11-27": {"event": "James Hype", "women": "Comp until 1:30am",
                            "men": "Comp until 1:30am", "cutoff": "1:30am"},
        "ZOUK|2026-11-28": {"closed": True, "event": "CLOSED"},
    })
    assert rules.venue_open_on("ZOUK", date(2026, 11, 28)) is False
    assert rules.venue_open_on("ZOUK Nightclub", date(2026, 11, 28)) is False
    # Open night with terms: no verified schedule, so stay None (not forced closed).
    assert rules.venue_open_on("ZOUK", date(2026, 11, 27)) is None
    assert liv_rules.is_no_list(liv_rules.for_night("ZOUK", "2026-11-28"))
    assert not liv_rules.is_no_list(liv_rules.for_night("ZOUK", "2026-11-27"))
    assert liv_rules.for_night("ZOUK Nightclub", "2026-11-27")["event"] == "James Hype"


def test_is_no_list_accepts_closed_flag():
    assert liv_rules.is_no_list({"closed": True})
    assert liv_rules.is_no_list({"no_list": True})
    assert not liv_rules.is_no_list({"closed": False})
    assert not liv_rules.is_no_list({})


def test_load_globs_extra_night_rules_files(tmp_path, monkeypatch):
    (tmp_path / "drais-night-rules.json").write_text(json.dumps({
        "nights": {"Drai's After Hours|2026-11-26": {"no_list": True}},
    }))
    (tmp_path / "zouk-night-rules.json").write_text(json.dumps({
        "nights": {"ZOUK|2026-11-28": {"closed": True}},
    }))
    monkeypatch.setenv("NIGHT_RULES_DIR", str(tmp_path))
    loaded = liv_rules.load()
    assert "Drai's After Hours|2026-11-26" in loaded
    assert "ZOUK|2026-11-28" in loaded


def test_zouk_stays_notify_only_not_auto_booked():
    assert is_notify_only("Las Vegas", "ZOUK")
    assert "ZOUK" not in rules.URVENUE_VENUES
    assert "ZOUK Nightclub" not in rules.URVENUE_VENUES
    assert "ZOUK" not in rules.NIGHTCLUBS
    assert "ZOUK" not in rules.URVENUE_NIGHTCLUBS


def test_drais_closed_night_falls_back_to_omnia(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "drais-night-rules.json", {
        "Drai's After Hours|2026-11-26": {"no_list": True, "event": "CLOSED"},
    })
    assert rules.venue_open_on("Drai's", date(2026, 11, 26)) is False
    tao = MapTao({("OMNIA Nightclub", "2026-11-26")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("d", raw(venues=["Drai's"], start_date="2026-11-26", end_date="2026-11-26"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    rec = s.get("d")["metadata"]["evidence"]["bookings"][0]
    assert rec["venue"] == "OMNIA Nightclub" and rec["date"] == "2026-11-26"
    assert rec.get("fallback") is True
    assert worker.tao.submits and worker.tao.submits[0][0] == "Guest List - OMNIA Nightclub"
    guest = api.sent[0]
    assert NOTICE in guest["text"]
    assert "OMNIA Nightclub" in guest["text"]
    assert "Drai's After Hours" not in (guest.get("subject") or "")


def test_open_drais_still_rejects_tao_receipt(tmp_path, monkeypatch):
    """Without a closed entry, a Drai's job cannot complete on a TAO club."""
    monkeypatch.setenv("NIGHT_RULES_DIR", str(tmp_path))
    payload = _queue_raw(venues=["Drai's After Hours"], start_date="2026-11-26", end_date="2026-11-26")
    s, token = assigned(payload)
    ev = {"evidence": {"verified": True, "bookings": [dict(
        venue="OMNIA Nightclub", date="2026-11-26", verified=True, fallback=True,
        order_id="O-1", party={"male_count": 1, "female_count": 2})],
        "email_receipts": [dict(kind="guest", id="G"), dict(kind="team", id="T")]}}
    assert update(s, token, "complete", ev)[0] == 409


def test_closed_drais_fallback_receipt_completes(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "drais-night-rules.json", {
        "Drai's After Hours|2026-11-26": {"no_list": True, "event": "CLOSED"},
    })
    payload = _queue_raw(venues=["Drai's After Hours"], start_date="2026-11-26", end_date="2026-11-26")
    s, token = assigned(payload)
    ev = {"evidence": {"verified": True, "bookings": [dict(
        venue="OMNIA Nightclub", date="2026-11-26", verified=True, fallback=True,
        order_id="O-1", party={"male_count": 1, "female_count": 2})],
        "email_receipts": [dict(kind="guest", id="G"), dict(kind="team", id="T")]}}
    status, body = update(s, token, "complete", ev)
    assert status == 200, body
    assert body["state"] == "done"

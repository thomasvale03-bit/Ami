"""Tickets-only Vegas nights (e.g. 2026-12-25 -> Marquee Nightclub tickets)."""
from datetime import date
import socket
from unittest.mock import Mock

import pytest

from config import rules
import queue_service as q
import worker_emails
from tests.test_booking_worker import TOKEN, FakeTao, make, raw
from tests.test_hold_email import TEAM

DEC25_URL = (
    "https://tickets.taogroup.com/e/marquee-nightclub-las-vegas-12-25-2026/tickets"
    "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AMY_QUEUE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMY_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("AMY_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("AMY_QUEUE_WORKER_CLAIMS", "true")
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))


def test_dec25_vegas_is_tickets_only_not_scottsdale():
    d = date(2026, 12, 25)
    for venue in ("OMNIA Nightclub", "Hakkasan Nightclub", "Marquee Nightclub",
                  "JEWEL Nightclub", "LIV Nightclub", "XS Nightclub",
                  "TAO Nightclub", "Drai's After Hours", "Marquee Dayclub"):
        policy = rules.date_tickets_only(d, venue)
        assert policy is not None
        assert policy["venue"] == "Marquee Nightclub"
        assert policy["url"] == DEC25_URL
    assert rules.date_tickets_only(d, "Riot House") is None
    assert rules.date_tickets_only(d, "El Hefe") is None
    assert rules.date_tickets_only(date(2026, 12, 24), "OMNIA Nightclub") is None
    assert rules.date_tickets_only(date(2026, 12, 26), "OMNIA Nightclub") is None


def test_tickets_only_email_is_warm_and_branded():
    req = {"first_name": "Ali", "email": "guest@example.invalid",
           "start_date": "2026-12-25", "end_date": "2026-12-25",
           "female_count": 2, "male_count": 2}
    items = [{"date": "2026-12-25", "ticket_venue": "Marquee Nightclub", "url": DEC25_URL}]
    p = worker_emails.tickets_only_email({}, req, items)
    assert p["to"] == "guest@example.invalid"
    assert p["bcc"] == TEAM
    assert p["subject"] == "Your December 25 guest list request - tickets at Marquee Nightclub"
    assert "guest list isn't available" in p["text"]
    assert "Marquee Nightclub has tickets for sale" in p["text"]
    assert DEC25_URL in p["text"]
    assert "utm_source=promoter&amp;utm_id=68d79ff587c84397b19f00330a1e6107" in p["html"]
    assert "Playmaker Entertainment" in p["text"]
    assert "skip" not in p["text"].lower() and "skip" not in p["html"].lower()
    assert "702" not in p["text"] and "$" not in p["text"]
    q = worker_emails.tickets_only_email({"source": "guestlistgo"}, req, items)
    assert q["from"] == "confirmations@guestlistgo.me" and q["bcc"] == TEAM
    assert "playmaker" not in (q["text"] + q["html"]).lower()
    assert "GuestlistGo" in q["text"]
    assert q["subject"] == p["subject"]  # stable dedupe key, independent of brand


def test_completion_accepts_dec25_tickets_only_without_bookings():
    raw_job = dict(name="Mock Guest", email="guest@example.invalid",
                   venues=["OMNIA Nightclub"], start_date="2026-12-25", end_date="2026-12-25",
                   male_count=1, female_count=2, authorization="YES",
                   submitted_at="2026-11-10T12:00:00-07:00")
    evidence = {"evidence": {"verified": True, "tickets_only": [
        {"requested_venue": "OMNIA Nightclub", "date": "2026-12-25",
         "ticket_venue": "Marquee Nightclub", "url": DEC25_URL}],
        "bookings": [],
        "email_receipts": [{"kind": "guest", "id": "G"}]}}
    out = q.validate_completion(raw_job, evidence)
    assert out["venue_changes"] == []


def test_completion_still_requires_other_nights():
    raw_job = dict(name="Mock Guest", email="guest@example.invalid",
                   venues=["OMNIA Nightclub"], start_date="2026-12-25", end_date="2026-12-26",
                   male_count=1, female_count=2, authorization="YES",
                   submitted_at="2026-11-10T12:00:00-07:00")
    evidence = {"evidence": {"verified": True, "bookings": [],
                             "email_receipts": [{"kind": "guest", "id": "G"}]}}
    with pytest.raises(q.Conflict, match="booking/order records"):
        q.validate_completion(raw_job, evidence)


def test_worker_sends_tickets_email_and_completes_dec25_only(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=True, dayclubs=True))
    s.enqueue("a", raw(start_date="2026-12-25", end_date="2026-12-25",
                       venues=["OMNIA Nightclub"]), 1)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert s.get("a")["state"] == "done"
    assert "tickets-only email sent" in (s.get("a")["reason"] or "")
    assert worker.tao.submits == []
    assert len(api.sent) == 1
    mail = api.sent[0]
    assert mail["to"] == "guest@example.invalid"
    assert "Marquee Nightclub has tickets for sale" in mail["text"]
    assert DEC25_URL in mail["text"]
    ev = s.get("a")["metadata"]["evidence"]
    assert ev["bookings"] == []
    assert ev["email_receipts"][0]["kind"] == "guest"


def test_worker_skips_dec25_retries_other_nights_and_skips_mdc_on_the_25th(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False, dayclubs=True))
    s.enqueue("a", raw(start_date="2026-12-25", end_date="2026-12-26",
                       venues=["OMNIA Nightclub"]), 1)
    result = worker.run_once()
    assert result["status"] == "retry", result
    assert s.get("a")["state"] == "retry"
    reason = s.get("a")["reason"] or ""
    assert "OMNIA Nightclub 2026-12-26" in reason
    assert "2026-12-25" not in reason
    # MDC on Dec 26 is booked (dayclubs posted); Dec 25 is tickets-only so no MDC that day.
    bookings = ((s.get("a")["metadata"].get("progress") or {}).get("bookings") or {})
    assert "Marquee Dayclub|2026-12-25" not in bookings
    assert bookings.get("Marquee Dayclub|2026-12-26", {}).get("verified") is True
    assert worker.tao.submits == [("Guest List - Marquee Dayclub", False)]
    ticket_mails = [m for m in api.sent if "tickets at Marquee Nightclub" in m.get("subject", "")]
    assert len(ticket_mails) == 1
    assert DEC25_URL in ticket_mails[0]["text"]


def test_second_pickup_does_not_resend_tickets_email(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False, dayclubs=False))
    s.enqueue("a", raw(start_date="2026-12-25", end_date="2026-12-26",
                       venues=["OMNIA Nightclub"]), 1)
    assert worker.run_once()["status"] == "retry"
    first = [m for m in api.sent if "tickets at Marquee Nightclub" in m.get("subject", "")]
    assert len(first) == 1
    # Job is due in the future after a retry; bump due so the worker can reclaim.
    row = s.get("a")
    s._due = getattr(s, "_due", None)
    # Use stall-requeue / owner path: update due via SQLite store helpers if present.
    import time
    now = worker.now()
    # Directly set due if the store exposes it through enqueue/update
    with s.connect() as db:
        db.execute("UPDATE queue_jobs SET state='queued', due=?, claim_token=NULL, lease_until=NULL WHERE ref=?",
                   (now, "a"))
    assert worker.run_once()["status"] == "retry"
    again = [m for m in api.sent if "tickets at Marquee Nightclub" in m.get("subject", "")]
    assert len(again) == 1

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

DEC31_TICKETS = [
    ("OMNIA Nightclub",
     "https://tickets.taogroup.com/e/omnia-las-vegas-12-31-2026/tickets"
     "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"),
    ("Hakkasan Nightclub",
     "https://tickets.taogroup.com/e/hakkasan-nightclub-las-vegas-12-31-2026/tickets"
     "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"),
    ("Marquee Nightclub",
     "https://tickets.taogroup.com/e/marquee-nightclub-las-vegas-12-31-2026/tickets"
     "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"),
    ("TAO Nightclub",
     "https://tickets.taogroup.com/e/tao-nightclub-las-vegas-12-31-2026/tickets"
     "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"),
    ("OMNIA Dayclub",
     "https://tickets.taogroup.com/e/omnia-dayclub-las-vegas-12-31-2026/tickets"
     "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"),
    ("Calvin Harris at XS Nightclub",
     "https://booketing.com/microsite/playmaker/event/719/1115/calvin-harris"
     "?eventcode=EVE111500020261231"),
]
DEC31_URLS = [u for _label, u in DEC31_TICKETS]
NYE_SUBJECT = "New Year's Eve tickets - all venues"


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


def test_dec31_vegas_is_tickets_only_with_every_event_tom_is_selling():
    d = date(2026, 12, 31)
    for venue in ("OMNIA Nightclub", "Hakkasan Nightclub", "Marquee Nightclub",
                  "JEWEL Nightclub", "LIV Nightclub", "XS Nightclub",
                  "TAO Nightclub", "Drai's After Hours", "Marquee Dayclub"):
        policy = rules.date_tickets_only(d, venue)
        assert policy is not None
        assert not policy.get("url")
        tickets = policy["tickets"]
        assert [(t["label"], t["url"]) for t in tickets] == DEC31_TICKETS
    assert rules.date_tickets_only(d, "Riot House") is None
    assert rules.date_tickets_only(d, "El Hefe") is None
    # Neighboring nights are unchanged (Dec 25 stays Marquee tickets).
    assert rules.date_tickets_only(date(2026, 12, 30), "OMNIA Nightclub") is None
    dec25 = rules.date_tickets_only(date(2026, 12, 25), "OMNIA Nightclub")
    assert dec25["venue"] == "Marquee Nightclub" and dec25["url"] == DEC25_URL


def _nye_items():
    return [{"date": "2026-12-31", "ticket_venue": "", "url": "",
             "tickets": [{"label": label, "url": url} for label, url in DEC31_TICKETS]}]


def test_nye_tickets_only_email_lists_every_venue_link():
    req = {"first_name": "Will", "email": "guest@example.invalid",
           "start_date": "2026-12-31", "end_date": "2026-12-31",
           "female_count": 0, "male_count": 1}
    p = worker_emails.tickets_only_email({}, req, _nye_items())
    assert p["to"] == "guest@example.invalid"
    assert p["bcc"] == TEAM
    assert p["subject"] == NYE_SUBJECT
    assert "guest list isn't available for New Year's Eve" in p["text"]
    assert "book your spot" in p["text"]
    assert "use one of these links to get your tickets" in p["text"]
    for label, url in DEC31_TICKETS:
        assert label in p["text"] and url in p["text"]
        assert label in p["html"]
        assert url in p["html"] or url.replace("&", "&amp;") in p["html"]
    assert "Calvin Harris at XS Nightclub" in p["text"]
    assert "tickets at" not in p["subject"].lower()
    assert "Playmaker Entertainment" in p["text"]
    assert "skip" not in p["text"].lower() and "skip" not in p["html"].lower()
    assert "702" not in p["text"] and "$" not in p["text"]
    q = worker_emails.tickets_only_email({"source": "guestlistgo"}, req, _nye_items())
    assert q["from"] == "confirmations@guestlistgo.me" and q["bcc"] == TEAM
    # One official ticket URL path contains "playmaker"; branding in the
    # rest of the email must not.
    scrubbed = q["text"] + q["html"]
    for url in DEC31_URLS:
        scrubbed = scrubbed.replace(url, "").replace(url.replace("&", "&amp;"), "")
    assert "playmaker" not in scrubbed.lower()
    assert "GuestlistGo" in q["text"]
    assert q["subject"] == p["subject"]


def test_mixed_dec25_and_nye_email_includes_both_links():
    req = {"first_name": "Alex", "email": "guest@example.invalid",
           "start_date": "2026-12-25", "end_date": "2026-12-31"}
    items = [
        {"date": "2026-12-25", "ticket_venue": "Marquee Nightclub", "url": DEC25_URL},
        _nye_items()[0],
    ]
    p = worker_emails.tickets_only_email({}, req, items)
    assert p["subject"] == "Your guest list request - tickets"
    assert DEC25_URL in p["text"]
    for _label, url in DEC31_TICKETS:
        assert url in p["text"]
    assert "Marquee Nightclub has tickets for sale" in p["text"]
    assert "guest list isn't available" in p["text"]
    assert "book your spot" in p["text"]
    assert "skip" not in p["text"].lower()


def test_worker_sends_tickets_email_and_completes_dec31_only(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=True, dayclubs=True))
    s.enqueue("a", raw(start_date="2026-12-31", end_date="2026-12-31",
                       venues=["Hakkasan Nightclub"]), 1)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert s.get("a")["state"] == "done"
    assert "tickets-only email sent" in (s.get("a")["reason"] or "")
    assert worker.tao.submits == []
    assert len(api.sent) == 1
    mail = api.sent[0]
    assert mail["to"] == "guest@example.invalid"
    assert mail["subject"] == NYE_SUBJECT
    assert "guest list isn't available for New Year's Eve" in mail["text"]
    for label, url in DEC31_TICKETS:
        assert label in mail["text"] and url in mail["text"]
    ev = s.get("a")["metadata"]["evidence"]
    assert ev["bookings"] == []
    assert ev["email_receipts"][0]["kind"] == "guest"


def test_worker_skips_dec31_and_retries_other_nights(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False, dayclubs=True))
    s.enqueue("a", raw(start_date="2026-12-31", end_date="2027-01-02",
                       venues=["OMNIA Nightclub"]), 1)
    result = worker.run_once()
    assert result["status"] == "retry", result
    reason = s.get("a")["reason"] or ""
    assert "OMNIA Nightclub 2027-01-01" in reason or "OMNIA Nightclub 2027-01-02" in reason
    assert "2026-12-31" not in reason
    bookings = ((s.get("a")["metadata"].get("progress") or {}).get("bookings") or {})
    assert "Marquee Dayclub|2026-12-31" not in bookings
    ticket_mails = [m for m in api.sent if m.get("subject") == NYE_SUBJECT]
    assert len(ticket_mails) == 1
    for _label, url in DEC31_TICKETS:
        assert url in ticket_mails[0]["text"]
    assert "tickets at" not in ticket_mails[0]["subject"].lower()

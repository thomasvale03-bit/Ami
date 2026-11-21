"""Thomas (Nov 19 2026, approved): team emails name the promoter the client
entered on the website form (e.g. 'Promoter: Jonathan M'), plus the promoter's
own email/phone when the form sends them. Never the guest's phone."""
import os
import threading
from datetime import datetime, timedelta
from unittest import mock

import pytest

import gmail_client
import posh_webhook as pw
import worker_emails

NOTIFY = ("ZOUK", "Ghostbar")
GUEST_PHONE = "6025550100"


def payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=1)).isoformat()
    p = {"source": "playmaker", "firstName": "Gina", "lastName": "Martinez", "email": "gina@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "venue": "LIV", "date": night,
         "women": 2, "men": 0, "age_21_confirmed": True, "ref": "pm-test-promo", "promoter": "Jonathan M"}
    p.update(kw)
    return p


def _intake():
    i = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
    i._lock = threading.Lock()
    i.service.return_value = mock.MagicMock()
    return i


@pytest.fixture(autouse=True)
def pending(monkeypatch, tmp_path):
    monkeypatch.setenv("AMY_PENDING_DIR", str(tmp_path / "pending"))


@pytest.mark.parametrize("venue", NOTIFY)
def test_notify_only_team_email_names_the_promoter(venue):
    with mock.patch.object(pw, "send_branded", return_value=(200, {"sent": True})) as sb:
        assert pw.accept_website(_intake(), payload(venue=venue, ref="pm-" + venue[:3])) == (200, "team notified")
    sent = sb.call_args[0][1]
    assert sent["to"] == "team@playmakerentertainment.com"
    assert "Promoter: Jonathan M" in sent["text"] and "Promoter: Jonathan M" in sent["html"]
    assert GUEST_PHONE not in sent["text"] and GUEST_PHONE not in sent["html"]
    assert sb.call_count == 1  # team only, no guest email


def test_notify_only_without_promoter_says_not_provided():
    clean, err = pw.validate_website(payload(promoter=""))
    assert err is None
    assert "Promoter: Not provided" in pw.notify_only_text(clean)


def test_promoter_contact_fields_when_the_form_sends_them():
    clean, err = pw.validate_website(payload(promoterEmail="jon@example.com", promoter_phone="(702) 555-0199"))
    assert err is None
    text = pw.notify_only_text(clean)
    assert "Promoter email: jon@example.com" in text and "Promoter phone: 7025550199" in text
    assert GUEST_PHONE not in text
    # Junk contact values are dropped, never guessed.
    clean, _ = pw.validate_website(payload(promoter_email="nope", promoter_phone="12"))
    assert "Promoter email" not in pw.notify_only_text(clean) and "Promoter phone" not in pw.notify_only_text(clean)


def test_request_notice_and_signed_up_email_carry_promoter():
    clean, err = pw.validate_website(payload(venue="OMNIA", promoterEmail="jon@example.com"))
    assert err is None
    notice = pw.to_website_text(clean)
    assert "Promoter first name: Jonathan" in notice and "Promoter last name: M" in notice
    assert "Promoter email: jon@example.com" in notice
    raw = gmail_client.parse_request("m1", notice)
    assert raw["promoter"] == "Jonathan M" and raw["promoter_email"] == "jon@example.com"
    assert raw["email"] == "gina@example.com"  # guest email unaffected
    request = {"first_name": "Gina", "last_name": "Martinez", "email": raw["email"], "female_count": 2,
               "male_count": 0, "start_date": raw["start_date"], "end_date": raw["end_date"],
               "promoter": raw["promoter"]}
    rows = [{"venue": "OMNIA Nightclub", "date": raw["start_date"], "order_id": "O-1"}]
    team = worker_emails.team_email("ref1", raw, request, rows)
    assert "Promoter: Jonathan M" in team["text"] and "Promoter email: jon@example.com" in team["text"]
    assert GUEST_PHONE not in team["text"]
    drais = worker_emails.drais_team_email("ref1", raw, request, [{"date": raw["start_date"]}])
    assert "Promoter: Jonathan M" in drais["text"] and GUEST_PHONE not in drais["text"]

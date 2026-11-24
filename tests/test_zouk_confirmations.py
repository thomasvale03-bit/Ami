"""ZOUK website sign-ups (Thomas, Nov 24 2026: "Time to start sending
confirmations for Zouk using our pme branding"). ZOUK has no booking platform:
the worker records the night as email-only, emails the guest Tom's "Must show
this at the door" confirmation (PME branding, or GuestlistGo branding for
guestlistgo.me sign-ups) and emails team@ to add them to the ZOUK list. A
closed / no_list night in zouk-night-rules.json falls back through
DEFAULT_ROUTING (OMNIA first). Ghostbar stays notify-only."""
import json
import re
import threading
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

import gmail_client
import posh_webhook as pw
import queue_service as q
import worker_emails
from config import rules
from tests.test_booking_worker import NOW, TOKEN, make, raw
from tests.test_dark_night_fallback import NOTICE, MapTao
from tests.test_queue_service import assigned, update

ZOUK_RULES = {
    "ZOUK|2026-11-27": {"event": "James Hype", "women": "Comp until 1:30am", "men": "Comp until 1:30am",
                        "cutoff": "1:30am", "drinks": "Ladies: 1 free drink ticket before 1am",
                        "notes": "Doors 10:30pm. Notify-only — never auto-booked."},
    "ZOUK|2026-11-28": {"event": "CLOSED", "no_list": True, "closed": True, "women": "no list", "men": "no list"},
}
PHONE_RE = re.compile(r"(?<!\d)\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    import socket
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AMY_QUEUE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMY_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("AMY_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("AMY_QUEUE_WORKER_CLAIMS", "true")
    monkeypatch.setattr(socket, "create_connection", mock.Mock(side_effect=AssertionError("No network")))
    rdir = tmp_path / "rules"
    rdir.mkdir()
    (rdir / "zouk-night-rules.json").write_text(json.dumps({"nights": ZOUK_RULES}))
    monkeypatch.setenv("NIGHT_RULES_DIR", str(rdir))
    monkeypatch.setenv("LIV_NIGHT_RULES", str(rdir / "liv-night-rules.json"))
    monkeypatch.setenv("XS_NIGHT_RULES", str(rdir / "xs-night-rules.json"))
    return rdir


def zraw(**kw):
    base = dict(venues=["ZOUK Nightclub"], start_date="2026-11-27", end_date="2026-11-27", source=None,
                name="tom V", first_name="tom", last_name="V", email="obedvale2003@me.com",
                female_count="1", male_count="1", form_ref="pm-test-zouk")
    base.update(kw)
    r = raw(**base)
    if r.get("source") is None:
        r.pop("source")
    return r


def _all_text(p):
    return p["subject"] + "\n" + p["text"] + "\n" + p["html"]


# --- intake ------------------------------------------------------------------

def _payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=3)).isoformat()
    p = {"source": "playmaker", "first_name": "tom", "last_name": "V", "email": "test@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "venue": "ZOUK", "date": night,
         "women": 1, "men": 1, "age_21_confirmed": True, "ref": "pm-zouktest"}
    p.update(kw)
    return p


def _intake():
    i = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
    i._lock = threading.Lock()
    i.service.return_value = mock.MagicMock()
    return i


def test_zouk_website_signup_goes_to_the_normal_intake_not_notify_only(monkeypatch):
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "false")
    assert not pw.is_notify_only("Las Vegas", "ZOUK")
    assert pw.is_notify_only("Las Vegas", "Ghostbar")
    i = _intake()
    with mock.patch.object(pw, "send_branded") as sb, \
         mock.patch.object(pw, "_delivered", return_value=False), \
         mock.patch.object(pw, "_mark_delivered"), \
         mock.patch.object(pw, "notify_team_website") as team:
        assert pw.accept_website(i, _payload()) == (200, "queued")
    i.service().users().messages().insert.assert_called_once()  # -> parse -> queue job -> worker
    team.assert_called_once()  # the usual intake notice to team@
    sb.assert_not_called()     # no notify-only email


def test_already_delivered_ref_is_not_resent(monkeypatch):
    """A ref that already went through (e.g. the notify-only test signup) is a duplicate."""
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "false")
    i = _intake()
    with mock.patch.object(pw, "_delivered", return_value=True), \
         mock.patch.object(pw, "send_branded") as sb, mock.patch.object(pw, "notify_team_website") as team:
        assert pw.accept_website(i, _payload()) == (200, "duplicate")
    i.service().users().messages().insert.assert_not_called()
    sb.assert_not_called()
    team.assert_not_called()


def test_intake_text_parses_to_a_runnable_zouk_job():
    clean, err = pw.validate_website(_payload())
    assert err is None and clean["venue"] == "ZOUK"
    parsed = gmail_client.parse_request("m1", pw.to_website_text(clean))
    assert parsed["venues"] == ["ZOUK Nightclub"]
    assert rules.is_known_vegas_venue("ZOUK Nightclub") and rules.is_zouk("ZOUK Nightclub")
    import main
    assert main.non_vegas_venue_problem(parsed) is None
    today = datetime.now(q.PHOENIX).date()
    assert q.source_policy("m1", parsed, today) == ""


def test_mixed_zouk_and_other_venue_request_requires_review():
    today = date(2026, 11, 10)
    reason = q.source_policy("m", zraw(venues=["ZOUK Nightclub", "OMNIA Nightclub"]), today)
    assert reason.startswith("ZOUK is confirmation-email only")
    assert q.source_policy("m", zraw(venues=["ZOUK", "ZOUK Nightclub"]), today) == ""


# --- worker: open night --------------------------------------------------------

def test_open_zouk_night_sends_pme_door_confirmation_and_team_add_email(tmp_path):
    s, api, worker = make(tmp_path, tao=MapTao({("Marquee Dayclub", "2026-11-27")}))
    s.enqueue("z", zraw(), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert worker.tao.submits == []  # never TAO / urvenue, and no Marquee Dayclub add-on
    assert [p["to"] for p in api.sent] == ["obedvale2003@me.com", "team@playmakerentertainment.com"]
    guest, team = api.sent
    assert "from" not in guest  # PME default branded sender
    assert guest["subject"] == "You're on the list: ZOUK Nightclub — Playmaker Entertainment (November 27)"
    for want in ("MUST SHOW THIS AT THE DOOR!", "THOMAS VALE'S LIST", "ACADEMY", "ZOUK Nightclub",
                 "Resorts World Las Vegas", "Friday 11/27 James Hype", "Doors open 10:30 PM (arrive 10 to 11 PM)",
                 "Everyone: Complimentary entry until 1:30 AM", "Ladies: 1 free drink ticket before 1 AM",
                 "Guest list line is indoors by Redtail and can extend to the giant Sphere.",
                 "ACADEMY - Thomas Vale", "tom V +1", "10:30 PM to 1:30 AM",
                 "Tell them you're on PROMOTER Thomas Vale's guest list.",
                 "dress code will be enforced", "Thomas Vale - Academy", "subject to capacity"):
        assert want in guest["text"], want
    assert "James Hype" in guest["html"] and "MUST SHOW THIS AT THE DOOR" in guest["html"]
    assert "subject to capacity" in guest["html"]
    assert "Ticket Wallet" not in guest["html"] and "TAO Group" not in guest["html"]  # no TAO pass steps
    assert "notify-only" not in _all_text(guest).lower() and "never auto-booked" not in _all_text(guest).lower()
    for p in api.sent:
        body = _all_text(p).lower()
        assert "skip the line" not in body and "skip-the-line" not in body and "jarvis" not in body
        assert not PHONE_RE.search(p["text"]), p["text"]
        assert "7025550100" not in body and "555" not in body
    assert team["subject"] == "Add to ZOUK list: tom V - ZOUK November 27 (1F 1M)"
    assert "notify only" not in team["text"].lower()
    for want in ("add this guest to the ZOUK list with the venue", "sent the guest their ZOUK confirmation",
                 "Guest: tom V", "Email: obedvale2003@me.com", "Venue: ZOUK Nightclub",
                 "Date: Fri 2026-11-27 (James Hype)", "Female guests: 1", "Male guests: 1",
                 "Promoter: Not provided", "Source: playmaker", "Ref: pm-test-zouk", "AMY ref: z"):
        assert want in team["text"], want
    ev = s.get("z")["metadata"]["evidence"]
    assert ev["bookings"] == [{"verified": True, "venue": "ZOUK Nightclub", "date": "2026-11-27",
                               "system": "email_only", "party": {"male_count": 1, "female_count": 1}}]
    assert {r["kind"] for r in ev["email_receipts"]} == {"guest", "team"}
    assert worker.ledger.get("z|ZOUK Nightclub|2026-11-27")["state"] == "email_only"


def test_rerun_after_completion_sends_nothing_again(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("z", zraw(), NOW)
    assert worker.run_once()["status"] == "done"
    sent = len(api.sent)
    worker.run_once()  # nothing queued any more
    assert len(api.sent) == sent and s.get("z")["state"] == "done"


def test_no_rules_for_the_night_uses_toms_default_terms(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("z", zraw(start_date="2026-11-26", end_date="2026-11-26", female_count="2", male_count="4"), NOW)
    assert worker.run_once()["status"] == "done"
    guest, team = api.sent
    assert "Everyone free entry until 1:25 AM or earlier" in guest["text"]
    assert "Thursday 11/26\n" in guest["text"]
    assert "tom V +5" in guest["text"] and "10:30 PM to 1:30 AM" in guest["text"]
    assert "no ZOUK rules on file for this night" in team["text"]


def test_party_of_one_has_no_plus(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("z", zraw(female_count="1", male_count="0"), NOW)
    assert worker.run_once()["status"] == "done"
    assert "\ntom V\n" in api.sent[0]["text"] and "tom V +" not in api.sent[0]["text"]


def test_guestlistgo_zouk_signup_is_guestlistgo_branded(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("g", zraw(source="guestlistgo", brand="GuestlistGo", promoter="GuestlistGo", form_ref="gg-1"), NOW)
    assert worker.run_once()["status"] == "done"
    guest, team = api.sent
    assert guest["from"] == "confirmations@guestlistgo.me"
    assert guest["subject"] == "You're on the list: ZOUK Nightclub — GuestlistGo (November 27)"
    body = _all_text(guest).lower()
    for bad in ("playmaker", "thomas vale", "academy", "booketing"):
        assert bad not in body, bad
    assert not re.search(r"\bpme\b", body)
    for want in ("MUST SHOW THIS AT THE DOOR!", "GUESTLISTGO GUEST LIST", "Friday 11/27 James Hype",
                 "tom V +1", "subject to capacity", "Questions? Just reply to this email."):
        assert want in guest["text"], want
    assert "guestlistgo.me" in guest["html"]
    assert team["to"] == "team@playmakerentertainment.com"
    assert "GuestlistGo branding" in team["text"] and "Source: guestlistgo" in team["text"]


# --- worker: closed night -> fallback ---------------------------------------------

def test_closed_zouk_night_falls_back_to_omnia_without_zouk_confirmation(tmp_path):
    assert rules.venue_open_on("ZOUK Nightclub", date(2026, 11, 28)) is False
    s, api, worker = make(tmp_path, tao=MapTao({("OMNIA Nightclub", "2026-11-28")}))
    s.enqueue("c", zraw(start_date="2026-11-28", end_date="2026-11-28"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    rec = s.get("c")["metadata"]["evidence"]["bookings"]
    assert len(rec) == 1 and rec[0]["venue"] == "OMNIA Nightclub" and rec[0].get("fallback") is True
    assert worker.tao.submits[0][0] == "Guest List - OMNIA Nightclub"
    guest = api.sent[0]
    assert NOTICE in guest["text"] and "OMNIA Nightclub" in guest["text"]
    assert "ZOUK" not in guest["subject"] and "MUST SHOW THIS AT THE DOOR" not in guest["text"]
    assert not any(p["subject"].startswith("Add to ZOUK list") for p in api.sent)


def test_two_night_zouk_open_then_closed_one_email_with_fallback(tmp_path):
    s, api, worker = make(tmp_path, tao=MapTao({("OMNIA Nightclub", "2026-11-28")}))
    s.enqueue("m", zraw(start_date="2026-11-27", end_date="2026-11-28"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    venues = sorted((b["venue"], b["date"]) for b in s.get("m")["metadata"]["evidence"]["bookings"])
    assert venues == [("OMNIA Nightclub", "2026-11-28"), ("ZOUK Nightclub", "2026-11-27")]
    assert len(api.sent) == 2
    guest, team = api.sent
    assert "Friday 11/27 James Hype" in guest["text"] and "OMNIA Nightclub (Guest List - OMNIA Nightclub" in guest["text"]
    assert NOTICE in guest["text"] and "Ticket Wallet" in guest["text"]
    assert "Sat 2026-11-28 - OMNIA Nightclub (ZOUK closed that night: fallback) - Order ID ORDER-1" in team["text"]


# --- queue completion checks -------------------------------------------------------

def _complete(record, receipts=True):
    payload = dict(zraw(), source_message_id="mock")
    payload.pop("phone", None)
    s, token = assigned(payload)
    ev = {"evidence": {"verified": True, "bookings": [record]}}
    if receipts:
        ev["evidence"]["email_receipts"] = [dict(kind="guest", id="G"), dict(kind="team", id="T")]
    return update(s, token, "complete", ev)


def _rec(**kw):
    r = dict(venue="ZOUK Nightclub", date="2026-11-27", verified=True, system="email_only",
             party={"male_count": 1, "female_count": 1})
    r.update(kw)
    return r


def test_completion_accepts_email_only_zouk_receipt():
    status, body = _complete(_rec())
    assert status == 200, body


def test_completion_refuses_zouk_without_emails_or_with_an_order():
    assert _complete(_rec(), receipts=False)[0] == 409
    assert _complete(_rec(order_id="O-1"))[0] == 409
    assert _complete(_rec(system="urvenue"))[0] == 409


def test_lookup_prior_send_uses_the_zouk_subject():
    with mock.patch.object(gmail_client, "get_service"), \
         mock.patch.object(gmail_client, "prior_sent", return_value=None) as ps:
        q.lookup_prior_send(zraw(), "guest")
    assert 'subject:"You\'re on the list: ZOUK Nightclub — Playmaker Entertainment (November 27)"' in ps.call_args[0][1]


def test_ghostbar_unchanged_notify_only():
    assert pw.is_notify_only("Las Vegas", "Ghostbar")
    clean, err = pw.validate_website(_payload(venue="Ghost Bar", city=""))
    assert err is None and clean["venue"] == "Ghostbar"


def test_team_email_names_the_form_promoter_and_never_the_guest_phone(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("p", zraw(promoter="Jonathan M", promoter_email="jm@example.com"), NOW)
    assert worker.run_once()["status"] == "done"
    team = api.sent[1]
    assert "Promoter: Jonathan M" in team["text"] and "Promoter email: jm@example.com" in team["text"]
    assert "7025550100" not in team["text"]

"""Ghostbar website sign-ups (Thomas, Nov 24 2026, t1223u: "Now next is
GhostBar - Very similar! Show this at the door: Academy- Thomas Vale. Ghostbar
Rooftop Lounge @ Palms: CONFIRMED on Thomas Vale's Guestlist 9pm-1am. All free.
Arrive early with your entire group, dress code will be enforced.").

Same flow as ZOUK: normal intake -> queue job -> the worker records the night
email-only (no platform), emails the guest the door confirmation (PME, or
GuestlistGo for guestlistgo.me sign-ups) and team@ "Add to Ghostbar list". A
closed / no_list night in ghostbar-night-rules.json falls back (OMNIA first)."""
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
    rdir.mkdir()  # no ghostbar-night-rules.json unless a test writes one
    monkeypatch.setenv("NIGHT_RULES_DIR", str(rdir))
    monkeypatch.setenv("LIV_NIGHT_RULES", str(rdir / "liv-night-rules.json"))
    monkeypatch.setenv("XS_NIGHT_RULES", str(rdir / "xs-night-rules.json"))
    return rdir


def write_rules(rdir, nights):
    (rdir / "ghostbar-night-rules.json").write_text(json.dumps({"nights": nights}))


def graw(**kw):
    base = dict(venues=["Ghostbar"], start_date="2026-11-27", end_date="2026-11-27", source=None,
                name="Tom V", first_name="Tom", last_name="V", email="obedvale2003@me.com",
                female_count="1", male_count="1", form_ref="pm-test-ghostbar")
    base.update(kw)
    r = raw(**base)
    if r.get("source") is None:
        r.pop("source")
    return r


def _all_text(p):
    return p["subject"] + "\n" + p["text"] + "\n" + p["html"]


def _payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=3)).isoformat()
    p = {"source": "playmaker", "first_name": "Tom", "last_name": "V", "email": "test@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "venue": "Ghostbar", "date": night,
         "women": 1, "men": 1, "age_21_confirmed": True, "ref": "pm-ghosttest"}
    p.update(kw)
    return p


# --- intake ------------------------------------------------------------------

def test_ghostbar_website_signup_goes_to_the_normal_intake_not_notify_only(monkeypatch):
    monkeypatch.setenv("AMY_QUEUE_ENABLED", "false")
    assert not pw.is_notify_only("Las Vegas", "Ghostbar")
    i = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
    i._lock = threading.Lock()
    with mock.patch.object(pw, "send_branded") as sb, \
         mock.patch.object(pw, "_delivered", return_value=False), \
         mock.patch.object(pw, "_mark_delivered"), \
         mock.patch.object(pw, "notify_team_website") as team:
        assert pw.accept_website(i, _payload()) == (200, "queued")
    i.service().users().messages().insert.assert_called_once()
    team.assert_called_once()
    sb.assert_not_called()


def test_intake_text_parses_to_a_runnable_ghostbar_job():
    for name in ("Ghostbar", "Ghost Bar", "Ghostbar Rooftop Lounge"):
        clean, err = pw.validate_website(_payload(venue=name, city=""))
        assert err is None and clean["venue"] == "Ghostbar", name
    clean, _ = pw.validate_website(_payload())
    parsed = gmail_client.parse_request("m1", pw.to_website_text(clean))
    assert parsed["venues"] == ["Ghostbar"]
    assert rules.is_known_vegas_venue("Ghostbar") and rules.is_ghostbar("Ghostbar") and rules.is_door_venue("Ghostbar")
    assert "Ghostbar" not in rules.URVENUE_VENUES and "Ghostbar" not in rules.NIGHTCLUBS
    import main
    assert main.non_vegas_venue_problem(parsed) is None
    assert q.source_policy("m1", parsed, datetime.now(q.PHOENIX).date()) == ""


def test_mixed_ghostbar_requests_require_review():
    today = date(2026, 11, 10)
    assert q.source_policy("m", graw(venues=["Ghostbar", "OMNIA Nightclub"]), today).startswith(
        "Ghostbar is confirmation-email only")
    assert q.source_policy("m", graw(venues=["Ghostbar", "ZOUK Nightclub"]), today) != ""
    assert q.source_policy("m", graw(venues=["Ghostbar", "Ghost Bar"]), today) == ""


# --- worker: open night --------------------------------------------------------

def test_open_night_sends_pme_door_confirmation_and_team_add_email(tmp_path):
    s, api, worker = make(tmp_path, tao=MapTao({("Marquee Dayclub", "2026-11-27")}))
    s.enqueue("g", graw(), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert worker.tao.submits == []  # never TAO / urvenue, no Marquee Dayclub add-on
    assert [p["to"] for p in api.sent] == ["obedvale2003@me.com", "team@playmakerentertainment.com"]
    guest, team = api.sent
    assert "from" not in guest
    assert guest["subject"] == "You're on the list: Ghostbar Rooftop Lounge — Playmaker Entertainment (November 27)"
    for want in ("MUST SHOW THIS AT THE DOOR!", "THOMAS VALE'S LIST", "ACADEMY", "Ghostbar Rooftop Lounge",
                 "Palms Casino Resort", "Friday 11/27", "CONFIRMED on Thomas Vale's guest list", "9 PM to 1 AM",
                 "All free", "Arrive early with your entire group; dress code will be enforced.",
                 "Show this email at the door with your ID.", "subject to capacity"):
        assert want in guest["text"], want
    html = guest["html"]
    for want in ("MUST SHOW THIS AT THE DOOR", "CONFIRMED on Thomas Vale&#x27;s guest list", "9 PM to 1 AM",
                 "All free", "dress code will be enforced", "subject to capacity", "Palms Casino Resort"):
        assert want in html, want
    for body in (guest["text"], html):
        low = body.lower()
        assert low.count("show this email at the door") == 1
        assert "your confirmation" not in low and "show this text" not in low
    assert "Ticket Wallet" not in html and "TAO Group" not in html
    for p in api.sent:
        body = _all_text(p).lower()
        assert "skip the line" not in body and "skip-the-line" not in body and "jarvis" not in body
        assert "notify-only" not in body and "notify only" not in body
        assert not PHONE_RE.search(p["text"]), p["text"]
        assert "555" not in body
    assert team["subject"] == "Add to Ghostbar list: Tom V - Ghostbar November 27 (1F 1M)"
    for want in ("add this guest to the Ghostbar list with the venue", "sent the guest their Ghostbar confirmation",
                 "PME branding", "Guest: Tom V", "Email: obedvale2003@me.com", "Venue: Ghostbar Rooftop Lounge",
                 "Date: Fri 2026-11-27 [no Ghostbar rules on file for this night: default terms used]",
                 "Female guests: 1", "Male guests: 1", "Source: playmaker", "Ref: pm-test-ghostbar", "AMY ref: g"):
        assert want in team["text"], want
    ev = s.get("g")["metadata"]["evidence"]
    assert ev["bookings"] == [{"verified": True, "venue": "Ghostbar", "date": "2026-11-27",
                               "system": "email_only", "party": {"male_count": 1, "female_count": 1}}]
    assert {r["kind"] for r in ev["email_receipts"]} == {"guest", "team"}
    assert worker.ledger.get("g|Ghostbar|2026-11-27")["state"] == "email_only"


def test_thomas_vales_list_styled_exactly_like_academy(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("g", graw(), NOW)
    assert worker.run_once()["status"] == "done"
    html = api.sent[0]["html"]
    styles = {}
    for label in ("THOMAS VALE&#x27;S LIST", "ACADEMY"):
        m = re.search(r'<p style="([^"]*)">' + re.escape(label) + "</p>", html)
        assert m, label
        styles[label] = re.sub(r"margin:[^;]*;", "", m.group(1))
    assert styles["THOMAS VALE&#x27;S LIST"] == styles["ACADEMY"]
    # Same card as the approved ZOUK email.
    assert worker_emails.DOOR_LIST_STYLE == worker_emails.ZOUK_LIST_STYLE


def test_night_rules_file_drives_event_terms_and_hours(tmp_path, isolated):
    write_rules(isolated, {"Ghostbar|2026-11-27": {"event": "Rooftop Fridays", "hours": "10pm-2am",
                                                   "women": "Free", "men": "Free until 11pm",
                                                   "notes": "internal only"}})
    s, api, worker = make(tmp_path)
    s.enqueue("g", graw(), NOW)
    assert worker.run_once()["status"] == "done"
    guest, team = api.sent
    for want in ("Friday 11/27 Rooftop Fridays", "10 PM to 2 AM", "Ladies: Free", "Guys: Free until 11 PM"):
        assert want in guest["text"], want
    assert "All free" not in guest["text"] and "internal only" not in _all_text(guest)
    assert "Date: Fri 2026-11-27 (Rooftop Fridays)" in team["text"] and "no Ghostbar rules" not in team["text"]


def test_guestlistgo_signup_is_guestlistgo_branded(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("gg", graw(source="guestlistgo", brand="GuestlistGo", promoter="GuestlistGo", form_ref="gg-1"), NOW)
    assert worker.run_once()["status"] == "done"
    guest, team = api.sent
    assert guest["from"] == "confirmations@guestlistgo.me"
    assert guest["subject"] == "You're on the list: Ghostbar Rooftop Lounge — GuestlistGo (November 27)"
    body = _all_text(guest).lower()
    for bad in ("playmaker", "thomas vale", "academy", "booketing"):
        assert bad not in body, bad
    assert not re.search(r"\bpme\b", body)
    for want in ("MUST SHOW THIS AT THE DOOR!", "GUESTLISTGO GUEST LIST", "CONFIRMED on the GuestlistGo guest list",
                 "9 PM to 1 AM", "All free", "dress code will be enforced",
                 "Show this email at the door with your ID.", "subject to capacity",
                 "Questions? Just reply to this email."):
        assert want in guest["text"], want
    assert "You're on the GuestlistGo guest list for Ghostbar Rooftop Lounge." in guest["html"]
    assert team["to"] == "team@playmakerentertainment.com"
    assert "GuestlistGo branding" in team["text"] and "Source: guestlistgo" in team["text"]


def test_rerun_after_completion_sends_nothing_again(tmp_path):
    s, api, worker = make(tmp_path)
    s.enqueue("g", graw(), NOW)
    assert worker.run_once()["status"] == "done"
    sent = len(api.sent)
    worker.run_once()
    assert len(api.sent) == sent and s.get("g")["state"] == "done"


# --- worker: closed night -> fallback ---------------------------------------------

def test_closed_night_falls_back_to_omnia_without_ghostbar_confirmation(tmp_path, isolated):
    write_rules(isolated, {"Ghostbar|2026-11-28": {"closed": True}})
    assert rules.venue_open_on("Ghostbar", date(2026, 11, 28)) is False
    s, api, worker = make(tmp_path, tao=MapTao({("OMNIA Nightclub", "2026-11-28")}))
    s.enqueue("c", graw(start_date="2026-11-28", end_date="2026-11-28"), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    rec = s.get("c")["metadata"]["evidence"]["bookings"]
    assert len(rec) == 1 and rec[0]["venue"] == "OMNIA Nightclub" and rec[0].get("fallback") is True
    assert worker.tao.submits[0][0] == "Guest List - OMNIA Nightclub"
    guest = api.sent[0]
    assert NOTICE in guest["text"] and "Ghostbar" not in guest["subject"]
    assert "MUST SHOW THIS AT THE DOOR" not in guest["text"]
    assert not any(p["subject"].startswith("Add to Ghostbar list") for p in api.sent)


def test_no_list_night_also_falls_back(tmp_path, isolated):
    write_rules(isolated, {"Ghostbar|2026-11-28": {"no_list": True}})
    assert rules.venue_open_on("Ghostbar", date(2026, 11, 28)) is False
    s, api, worker = make(tmp_path, tao=MapTao({("OMNIA Nightclub", "2026-11-28")}))
    s.enqueue("n", graw(start_date="2026-11-27", end_date="2026-11-28"), NOW)
    assert worker.run_once()["status"] == "done"
    venues = sorted((b["venue"], b["date"]) for b in s.get("n")["metadata"]["evidence"]["bookings"])
    assert venues == [("Ghostbar", "2026-11-27"), ("OMNIA Nightclub", "2026-11-28")]
    guest, team = api.sent
    assert "CONFIRMED on Thomas Vale's guest list" in guest["text"] and NOTICE in guest["text"]
    assert guest["text"].lower().count("show this email at the door") == 1
    assert "Sat 2026-11-28 - OMNIA Nightclub (Ghostbar closed that night: fallback) - Order ID ORDER-1" in team["text"]


# --- queue completion checks -------------------------------------------------------

def _complete(record, receipts=True):
    payload = dict(graw(), source_message_id="mock")
    payload.pop("phone", None)
    s, token = assigned(payload)
    ev = {"evidence": {"verified": True, "bookings": [record]}}
    if receipts:
        ev["evidence"]["email_receipts"] = [dict(kind="guest", id="G"), dict(kind="team", id="T")]
    return update(s, token, "complete", ev)


def _rec(**kw):
    r = dict(venue="Ghostbar", date="2026-11-27", verified=True, system="email_only",
             party={"male_count": 1, "female_count": 1})
    r.update(kw)
    return r


def test_completion_accepts_email_only_receipt():
    assert _complete(_rec())[0] == 200


def test_completion_refuses_without_emails():
    assert _complete(_rec(), receipts=False)[0] == 409


def test_completion_refuses_an_order_id():
    assert _complete(_rec(order_id="O-1"))[0] == 409


def test_completion_refuses_a_booking_system():
    assert _complete(_rec(system="urvenue"))[0] == 409


def test_lookup_prior_send_uses_the_ghostbar_subject():
    with mock.patch.object(gmail_client, "get_service"), \
         mock.patch.object(gmail_client, "prior_sent", return_value=None) as ps:
        q.lookup_prior_send(graw(), "guest")
    assert ('subject:"You\'re on the list: Ghostbar Rooftop Lounge — Playmaker Entertainment (November 27)"'
            in ps.call_args[0][1])


def test_hours_parsing():
    assert worker_emails._hours("9pm-1am") == "9 PM to 1 AM"
    assert worker_emails._hours("10:30pm – 2am") == "10:30 PM to 2 AM"
    assert worker_emails._hours("") == ""

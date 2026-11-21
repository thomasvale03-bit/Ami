"""Thomas (Nov 21 2026): when a guest's list or booking goes on hold, AMY emails
the guest the status. ONE hold email per trip/ref (all held nights), team@ on
BCC, sent when the hold first appears or materially changes, never per hourly
retry. Suppression/rejection stops it for good. When the held night books, the
confirmation goes out as "Updated:"."""
from tests.test_booking_worker import FakeTao, InProcessApi, NOW, make, raw  # noqa: F401
from tests.test_booking_worker import isolated  # noqa: F401
from tests.test_dark_night_fallback import MapTao
import booking_worker as bw
import worker_emails

TEAM = "team@playmakerentertainment.com"


def hold_sends(api):
    return [p for p in api.sent if "on hold" in p["subject"]]


def next_hour(api, worker, hours=1):
    api.now += 3600 * hours
    worker.now = lambda: api.now


def test_all_nights_held_sends_one_hold_email_then_nothing_on_retries(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-26"), NOW)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1
    p = api.sent[0]
    assert p["to"] == "guest@example.invalid" and p["bcc"] == TEAM and "from" not in p
    assert p["subject"] == "We received your guest list request for November 25 to 26 - on hold for now"
    assert "isn't open yet" in p["text"] and "confirmation" in p["text"]
    assert "skip the line" not in (p["text"] + p["html"]).lower()
    assert "jarvis" not in (p["text"] + p["html"]).lower()
    assert "702" not in p["text"]  # no phone numbers
    marker = s.get("h")["metadata"]["progress"]["hold_notified"]
    assert marker["via"] == "hold_email" and marker["message_id"] == "MSG-1@playmakerentertainment.com"
    for _ in range(3):  # hourly retries: same hold, no new email
        next_hour(api, worker)
        assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1


def test_held_night_books_later_confirmation_is_updated(tmp_path):
    tao = FakeTao(posted=False)
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    worker.run_once()
    assert len(hold_sends(api)) == 1
    tao.posted_value = True
    next_hour(api, worker)
    assert worker.run_once()["status"] == "done"
    guest = [p for p in api.sent if p["to"] == "guest@example.invalid"]
    assert len(guest) == 2
    assert guest[1]["subject"].startswith("Updated: You're on the Playmaker Entertainment guest list")
    team = [p for p in api.sent if p["to"] == TEAM]
    assert len(team) == 1 and team[0]["subject"].startswith("Signed up:")


def test_partial_booking_confirmation_counts_as_the_hold_notice(tmp_path):
    tao = MapTao({("Hakkasan Nightclub", "2026-11-25")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("p", raw(venues=["Hakkasan Nightclub", "TAO Nightclub"], start_date="2026-11-25", end_date="2026-11-26",
                       itinerary={"2026-11-25": ["Hakkasan Nightclub"], "2026-11-26": ["TAO Nightclub"]}), NOW)
    s_before = len(api.sent)
    assert worker.run_once()["status"] == "retry"
    sent = api.sent[s_before:]
    assert [p["to"] for p in sent] == ["guest@example.invalid", TEAM]  # one guest + one team, no extra hold email
    assert "Still to come" in sent[0]["text"]
    assert hold_sends(api) == []
    marker = s.get("p")["metadata"]["progress"]["hold_notified"]
    assert marker["via"] == "confirmation"
    next_hour(api, worker)
    worker.run_once()
    assert len(api.sent) == 2


def test_material_change_sends_one_updated_hold_email(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    worker.run_once()
    job = s.get("h")
    # Same ref, new status for the night (now an access-code list): one Updated hold email.
    marker = worker._notify_hold(job, "h", None, job["raw"], [("Hakkasan Nightclub", "2026-11-25", "access_code")])
    assert marker and marker["sends"] == 2
    upd = hold_sends(api)[-1]
    assert upd["subject"].startswith("Updated: guest list request for November 25 - Nov 25 on hold (access code needed)")
    assert "access code" in upd["text"]
    # Repeating it is a no-op.
    worker._notify_hold(s.get("h"), "h", None, job["raw"], [("Hakkasan Nightclub", "2026-11-25", "access_code")])
    assert len(hold_sends(api)) == 2


def test_rejected_or_suppressed_address_is_never_retried(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    api.send_responses = [(422, {"sent": False, "rejected": True, "detail": "suppressed"})]
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1
    assert s.get("h")["metadata"]["progress"]["hold_notified"]["rejected"] is True
    next_hour(api, worker)
    worker.run_once()
    job = s.get("h")
    worker._notify_hold(job, "h", None, job["raw"], [("Hakkasan Nightclub", "2026-11-25", "access_code")])
    assert len(api.sent) == 1


def test_rate_limited_hold_email_tries_again_next_pickup_without_breaking_job(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    api.send_responses = [(429, {"result": "branded send limit reached, try later"})]
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    assert worker.run_once()["status"] == "retry"
    assert "hold_notified" not in (s.get("h")["metadata"].get("progress") or {})
    next_hour(api, worker)
    worker.run_once()
    assert len(api.sent) == 2 and api.sent[1]["subject"].endswith("on hold for now")


def test_nye_hold_mentions_omnia_and_promoter_ticket_link(tmp_path):
    req = {"first_name": "Elaina", "email": "e@example.invalid", "start_date": "2026-12-31",
           "end_date": "2027-01-03", "female_count": 1, "male_count": 1}
    p = worker_emails.hold_email("r", {}, req, [("OMNIA Nightclub", "2026-12-31", "unposted")])
    assert "OMNIA doesn't have a guest list on NYE" in p["text"]
    assert "utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107" in p["text"]
    assert "utm_source=promoter&amp;utm_id=68d79ff587c84397b19f00330a1e6107" in p["html"]
    q = worker_emails.hold_email("r", {}, req, [("OMNIA Nightclub", "2027-01-02", "unposted")])
    assert "NYE" not in q["text"]
    # The partial confirmation's "Still to come" line carries the same NYE note.
    assert "OMNIA doesn't have a guest list on NYE" in worker_emails.pending_note([("OMNIA Nightclub", "2026-12-31")])


def test_guestlistgo_hold_email_is_guestlistgo_branded():
    req = {"first_name": "Gia", "email": "g@example.invalid", "start_date": "2026-12-04",
           "end_date": "2026-12-04", "female_count": 2, "male_count": 0}
    p = worker_emails.hold_email("r", {"source": "guestlistgo"}, req, [("OMNIA Nightclub", "2026-12-04", "unposted")])
    assert p["from"] == "confirmations@guestlistgo.me" and p["bcc"] == TEAM
    assert "playmaker" not in (p["text"] + p["html"]).lower()
    assert "GuestlistGo" in p["text"]


def test_hold_email_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AMY_WORKER_HOLD_EMAIL", "false")
    s = __import__("queue_service").store()
    api = InProcessApi(s)
    worker = bw.Worker(api, bw.Ledger(tmp_path / "w" / "ledger.json"), tao=FakeTao(posted=False), mode="live",
                       evidence_dir=tmp_path / "w", now=lambda: NOW, sleep=lambda s: None,
                       email_wait_seconds=0, alerts=False)
    s.enqueue("h", raw(venues=["Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-25"), NOW)
    assert worker.run_once()["status"] == "retry"
    assert api.sent == []


def test_drais_never_gets_a_hold_email(tmp_path):
    s, api, worker = make(tmp_path, tao=FakeTao(posted=False))
    job = {"metadata": {}}
    assert worker._notify_hold(job, "d", None, raw(venues=["Drai's"]), []) is None
    assert api.sent == []


def test_job_confirmed_before_this_change_is_backfilled_not_re_emailed(tmp_path):
    """Elaina-style: partial confirmation (with "Still to come") went out before
    hold markers existed. The next retry records the marker; no extra email."""
    import json
    tao = MapTao({("Hakkasan Nightclub", "2026-11-25")})
    s, api, worker = make(tmp_path, tao=tao)
    s.enqueue("p", raw(venues=["Hakkasan Nightclub", "TAO Nightclub"], start_date="2026-11-25",
                       end_date="2026-11-26",
                       itinerary={"2026-11-25": ["Hakkasan Nightclub"], "2026-11-26": ["TAO Nightclub"]}), NOW)
    worker.run_once()
    assert len(api.sent) == 2
    # Simulate the pre-change state: no hold marker anywhere.
    prog = dict(s.get("p")["metadata"]["progress"])
    prog.pop("hold_notified")
    s.set_meta("p", "progress", prog)
    path = tmp_path / "w" / "ledger.json"
    data = json.loads(path.read_text())
    data.pop("p|hold_notified")
    path.write_text(json.dumps(data))
    next_hour(api, worker)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 2
    assert s.get("p")["metadata"]["progress"]["hold_notified"]["via"] == "confirmation"

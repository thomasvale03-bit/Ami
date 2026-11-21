"""urvenue.me sign-ups for LIV, LIV Beach, XS and Encore (Thomas, Nov 21 2026):
booked on the venue's urvenue guest list, verified, then ONE guest + ONE team
confirmation like TAO. No event = not posted -> hold + hourly re-check.
Closed LIV/XS -> OMNIA-first fallback; closed dayclub -> hold. Never added
twice; never a guest email claiming booked when nothing was added."""
import os
import shutil
from datetime import date

import pytest

import booking_worker as bw
import urvenue
from tests.test_booking_worker import FakeTao, NOW, make, raw  # noqa: F401
from tests.test_booking_worker import isolated  # noqa: F401

TEAM = "team@playmakerentertainment.com"


@pytest.fixture(autouse=True)
def urvenue_env(monkeypatch, tmp_path):
    monkeypatch.setenv("URVENUE_PASSWORD", "x")  # dummy; tests never log in for real
    monkeypatch.setenv("AMY_WORKER_DIR", str(tmp_path / "wd"))
    monkeypatch.delenv("URVENUE_VENUE_IDS", raising=False)


class FakeUrvenue:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.adds = []

    def add(self, venue, night, guest, *, ref, rehearse, screenshot_path, before_submit):
        out = self.outcomes.get((venue, night.isoformat()), "ok")
        if out == "notposted":
            raise urvenue.NotPosted("no event")
        if out == "closed":
            raise urvenue.NotOpen("Closed", "Some Night")
        if out == "already":
            raise urvenue.AlreadyListed("Mock Guest 2 1 1")
        if out == "error":
            raise urvenue.UrvenueError("Select Item missing")
        if rehearse:
            return {"rehearsal": True, "event": "Rufus Du Sol"}
        before_submit()
        self.adds.append((venue, night.isoformat(), guest["male_count"], guest["female_count"]))
        if out == "uncertain":
            raise urvenue.SubmissionUncertain("row not visible")
        return {"verified": True, "confirmation_id": urvenue.confirmation_ref(ref, venue, night.isoformat()),
                "row": "Active Mock Guest General List Thomas Vale 3 1 2", "event": "Rufus Du Sol",
                "event_time": "10:30pm", "page_url": "https://guestlist.urvenue.me/2/79/1115/guestlist/dayview/261125/0",
                "list_item": "General List"}


def mk(tmp_path, uv=None, tao=None):
    s, api, worker = make(tmp_path, tao=tao or FakeTao(), urvenue=uv or FakeUrvenue())
    return s, api, worker


# --- worker flow ------------------------------------------------------------

@pytest.mark.parametrize("venue,canon", [("LIV", "LIV Nightclub"), ("XS", "XS Nightclub"), ("LIV Beach", "LIV Beach"),
                                         ("EBC", "Encore Beach Club"),
                                         ("Encore Beach Club At Night", "Encore Beach Club At Night")])
def test_books_on_urvenue_and_confirms_once(tmp_path, venue, canon):
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("u", raw(venues=[venue]), NOW)
    result = worker.run_once()
    assert result["status"] == "done", result
    assert uv.adds == [(canon, "2026-11-25", 1, 2)]
    row = s.get("u")
    assert row["state"] == "done"
    rec = row["metadata"]["evidence"]["bookings"][0]
    assert rec["system"] == "urvenue" and rec["venue"] == canon and rec["order_id"].startswith("UV-")
    assert [p["to"] for p in api.sent] == ["guest@example.invalid", TEAM]
    guest, team = api.sent
    assert guest["subject"] == "You're on the Playmaker Entertainment guest list for November 25"
    assert "on the guest list" in guest["text"] and "Order ID" not in guest["text"]
    assert "TAO Group Hospitality Rewards" not in guest["html"]
    if canon in ("XS Nightclub", "Encore Beach Club", "Encore Beach Club At Night"):
        # Wynn (Thomas, Nov 21 2026): Wynn Resorts app steps, same email as the booking.
        for bit in ("Wynn Resorts app", "Wynn Rewards with the same email you signed up with (guest@example.invalid)",
                    "My Itinerary", "QR code", "can't be transferred"):
            assert bit in guest["text"], bit
        assert "Your Wynn Resorts app reservation" in guest["html"] and "My Itinerary" in guest["html"]
        assert "no app or passes needed" not in guest["text"]
    else:
        assert "no app or passes needed" in guest["text"] and "How to check in" in guest["html"]
        assert "wynn" not in (guest["text"] + guest["html"]).lower()
    assert "urvenue" in team["text"] and "General List" in team["text"] and "Amy Ref: u" in team["text"]
    for p in api.sent:
        body = (p["text"] + p["html"] + p["subject"]).lower()
        assert "jarvis" not in body and "7025550100" not in body and "702" not in p["text"]
    # second run: nothing more
    assert worker.run_once()["status"] == "idle"
    assert len(uv.adds) == 1 and len(api.sent) == 2


def test_not_posted_holds_once_then_books_with_updated(tmp_path):
    uv = FakeUrvenue({("XS Nightclub", "2026-11-25"): "notposted"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("h", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "retry"
    assert uv.adds == []
    assert len(api.sent) == 1 and "on hold" in api.sent[0]["subject"] and api.sent[0]["bcc"] == TEAM
    assert "XS Nightclub" in api.sent[0]["text"]
    job = s.get("h")
    assert job["state"] == "retry" and "not posted on urvenue" in job["reason"]
    api.now += 3600
    worker.now = lambda: api.now
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1  # no hourly repeat
    uv.outcomes.clear()
    api.now += 3600
    assert worker.run_once()["status"] == "done"
    guest = [p for p in api.sent if p["to"] == "guest@example.invalid"]
    assert guest[-1]["subject"].startswith("Updated: You're on the Playmaker Entertainment guest list")
    assert len([p for p in api.sent if p["to"] == TEAM]) == 1


def test_closed_liv_falls_back_to_omnia(tmp_path):
    uv = FakeUrvenue({("LIV Nightclub", "2026-11-25"): "closed"})  # Wednesday
    tao = FakeTao()
    s, api, worker = mk(tmp_path, uv, tao)
    s.enqueue("f", raw(venues=["LIV"], start_date="2026-11-27", end_date="2026-11-27"), NOW)
    uv.outcomes = {("LIV Nightclub", "2026-11-27"): "closed"}
    result = worker.run_once()
    assert result["status"] == "done", result
    rec = s.get("f")["metadata"]["evidence"]["bookings"][0]
    assert rec["venue"] == "OMNIA Nightclub" and rec["fallback"] is True
    assert uv.adds == []


def test_closed_liv_beach_goes_on_hold(tmp_path):
    uv = FakeUrvenue({("LIV Beach", "2026-11-25"): "closed"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("c", raw(venues=["LIV Beach"]), NOW)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1 and "on hold" in api.sent[0]["subject"]


def test_already_on_list_never_added_twice(tmp_path):
    uv = FakeUrvenue({("XS Nightclub", "2026-11-25"): "already"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("d", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "manual"
    assert uv.adds == [] and api.sent == []


def test_uncertain_after_click_is_never_resubmitted(tmp_path):
    uv = FakeUrvenue({("XS Nightclub", "2026-11-25"): "uncertain"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("x", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "uncertain"
    assert len(uv.adds) == 1 and api.sent == []
    assert s.get("x")["state"] == "submission_uncertain"


def test_pre_submit_error_goes_manual_without_emails(tmp_path):
    uv = FakeUrvenue({("XS Nightclub", "2026-11-25"): "error"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("e", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "manual"
    assert uv.adds == [] and api.sent == []
    assert "nothing submitted" in s.get("e")["reason"]


def test_missing_password_is_a_quiet_retry(tmp_path, monkeypatch):
    monkeypatch.delenv("URVENUE_PASSWORD", raising=False)
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("p", raw(venues=["LIV"]), NOW)
    result = worker.run_once()
    assert result["status"] == "retry" and "URVENUE_PASSWORD" in result["reason"]
    assert uv.adds == [] and api.sent == []  # never a booked email, nothing submitted


def test_trip_with_tao_and_urvenue_nights_one_email_each(tmp_path):
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("t", raw(venues=["XS", "Hakkasan Nightclub"], start_date="2026-11-25", end_date="2026-11-26",
                       itinerary={"2026-11-25": ["XS Nightclub"], "2026-11-26": ["Hakkasan Nightclub"]}), NOW)
    assert worker.run_once()["status"] == "done"
    assert [p["to"] for p in api.sent] == ["guest@example.invalid", TEAM]
    g = api.sent[0]
    assert "Order ID ORDER-" in g["text"] and "XS Nightclub (Rufus Du Sol — on the guest list)" in g["text"]
    assert "Your Wynn Resorts app reservation" in g["html"] and "TAO Group Hospitality Rewards" in g["html"]
    assert "My Itinerary" in g["text"]


def test_liv_and_xs_trip_gets_both_sections(tmp_path):
    s, api, worker = mk(tmp_path, FakeUrvenue())
    s.enqueue("lx", raw(venues=["XS", "LIV"], start_date="2026-11-27", end_date="2026-11-28",
                        itinerary={"2026-11-27": ["XS Nightclub"], "2026-11-28": ["LIV Nightclub"]}), NOW)
    assert worker.run_once()["status"] == "done"
    g = api.sent[0]
    assert "Your Wynn Resorts app reservation for XS Nightclub" in g["html"].replace("lives in", "") or \
        "reservation for XS Nightclub lives in the Wynn Resorts app" in g["text"]
    assert "LIV Nightclub: no app or passes needed" in g["text"]
    assert "LIV check-in" in g["html"] and "TAO Group Hospitality Rewards" not in g["html"]


def test_urvenue_line_uses_the_guests_own_email():
    line, _ = urvenue.booking_line({"first_name": "Mock", "last_name": "Guest", "email": "guest@example.invalid",
                                    "male_count": 1, "female_count": 2}, "r")
    assert " guest@example.invalid " in line and "playmaker" not in line.split("!")[0].lower()


def test_zouk_ghostbar_still_not_urvenue():
    from config import rules
    assert not rules.is_urvenue("ZOUK") and not rules.is_urvenue("Ghostbar")


# --- pure helpers -------------------------------------------------------------

def test_booking_line_format():
    line, name = urvenue.booking_line({"first_name": "Ana-María", "last_name": "O'Neil \"x\"",
                                       "email": "ana@example.com", "male_count": 2, "female_count": 3}, "ref1")
    assert name == "Ana-María O'Neil x"
    assert line == "Ana-María O'Neil x ana@example.com 2M 3F !Playmaker Entertainment AMY ref1"
    line, _ = urvenue.booking_line({"first_name": "A", "last_name": "B", "email": "a@b.co",
                                    "male_count": 0, "female_count": 2}, "r")
    assert " 2F " in line and "0M" not in line and "+1" not in line
    with pytest.raises(urvenue.UrvenueError):
        urvenue.booking_line({"first_name": "A", "last_name": "B", "email": "bad", "male_count": 1}, "r")


def test_parse_event_page():
    text = "URGUESTLIST\nGuest Lists Nov 21, 2026\nLIV Beach\nDiscolines - Open\nOperation Hours\nFrom 11:30am to 6:00pm\n"
    info = urvenue.parse_event_page(text)
    assert info == {"event": "Discolines", "status": "Open", "event_time": "11:30am", "date": date(2026, 11, 21)}
    closed = urvenue.parse_event_page("Guest Lists Dec 4, 2026\nKaskade - Closed\nOperation Hours\nFrom 10:30pm")
    assert closed["status"] == "Closed" and closed["date"] == date(2026, 12, 4)


def test_label_matching_never_confuses_venues():
    assert urvenue.label_matches("LIV Beach", "LIV Beach")
    assert not urvenue.label_matches("LIV Nightclub", "LIV Beach")
    assert urvenue.label_matches("LIV Nightclub", "LIV Las Vegas")
    assert urvenue.label_matches("Encore Beach Club", "Encore Beach Club")
    assert not urvenue.label_matches("Encore Beach Club", "Encore Beach Club At Night")
    assert urvenue.label_matches("Encore Beach Club At Night", "Encore Beach Club At Night")
    assert urvenue.label_matches("XS Nightclub", "XS Nightclub")


def test_label_year_across_new_year():
    assert urvenue.parse_label_year("Jan", 12, 2026) == 2027
    assert urvenue.parse_label_year("Dec", 1, 2027) == 2026
    assert urvenue.parse_label_year("Nov", 11, 2026) == 2026


def test_venue_ids_override_and_cache(monkeypatch):
    assert urvenue.venue_ids("XS Nightclub") == ("79", "1115")
    assert urvenue.venue_ids("LIV Nightclub") == ("2386", "1121561")
    assert urvenue.venue_ids("LIV Beach") == ("2386", "1214881")
    assert urvenue.venue_ids("Encore Beach Club") == ("79", "1103")
    assert urvenue.venue_ids("Encore Beach Club At Night") == ("79", "1163")
    urvenue.remember_venue_id("LIV Nightclub", "2386", "555")
    assert urvenue.venue_ids("LIV Nightclub") == ("2386", "555")
    monkeypatch.setenv("URVENUE_VENUE_IDS", '{"LIV Nightclub": "2386/999"}')
    assert urvenue.venue_ids("LIV Nightclub") == ("2386", "999")


def test_pick_arrows_prefers_icon_classes_and_ignores_offscreen():
    arrows = [{"x": 1157, "cls": "btn next-month"}, {"x": 1157, "cls": "fa fa-angle-left"},
              {"x": 1208, "cls": "btn next-month"}, {"x": 1208, "cls": "fa fa-angle-right"}]
    prev, nxt = urvenue.pick_arrows(arrows)
    assert prev["cls"].endswith("left") and nxt["cls"].endswith("right")
    prev, nxt = urvenue.pick_arrows([{"x": 10, "cls": ""}, {"x": 20, "cls": ""}])
    assert (prev["x"], nxt["x"]) == (10, 20)
    assert urvenue.pick_arrows([{"x": 1, "cls": ""}]) == (None, None)


def test_find_row():
    text = "STATUS NAME\nActive\tMock Guest\tGeneral List\tThomas Vale\t3\nNo data"
    assert urvenue.find_row(text, "mock guest") .startswith("Active")
    assert urvenue.find_row("No data to display", "Mock Guest") is None


def test_password_never_in_errors(monkeypatch):
    monkeypatch.setenv("URVENUE_PASSWORD", "s3cret-value")
    import inspect
    src = inspect.getsource(urvenue)
    assert "log.info(\"urvenue login ok\")" in src
    assert "password)" not in src.replace("pw.fill(password)", "")


# --- browser flow against a local stand-in (no real urvenue) -------------------

CHROME = shutil.which("google-chrome") or shutil.which("chromium")


@pytest.fixture
def site_browser(monkeypatch):
    if not CHROME:
        pytest.skip("no chrome")
    pytest.importorskip("playwright")
    import tao_portal
    from tests.fixtures import urvenue_mock

    def browser_page(p):
        b = p.chromium.launch(executable_path=CHROME, headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 720})
        page.set_default_timeout(15_000)
        return b, page

    monkeypatch.setattr(tao_portal, "_browser_page", browser_page)
    monkeypatch.setattr(urvenue, "WAIT_MS", 300)
    monkeypatch.setattr(urvenue, "GUESTLIST_HOST", "127.0.0.1")

    def start(venues, **kw):
        site = urvenue_mock.Site(venues, **kw).__enter__()
        monkeypatch.setattr(urvenue, "CALENDAR_URL", site.base + "/2/{acct}/{vid}/guestlist/dayview")
        monkeypatch.setattr(urvenue, "LOGIN_URL", site.base + "/login")
        monkeypatch.setattr(urvenue, "ACCOUNTS_URL", site.base + "/dashboard")
        return site
    return start, urvenue_mock


GUEST = {"first_name": "Mock", "last_name": "Guest", "email": "guest@example.invalid",
         "male_count": 1, "female_count": 2}


def test_browser_adds_guest_and_verifies_row(site_browser, tmp_path):
    start, mock = site_browser
    site = start({("79", "1115"): ("XS Nightclub", {"2026-12-04": {"name": "Kaskade", "status": "Open",
                                                                   "hours": "10:30pm to 4:00am"}})})
    try:
        calls = []
        out = urvenue.add_guest("XS Nightclub", date(2026, 12, 4), GUEST, ref="r1",
                                screenshot_path=str(tmp_path / "s.png"), before_submit=lambda: calls.append(1))
        assert out["verified"] and out["event"] == "Kaskade" and out["event_time"] == "10:30pm"
        assert "Mock Guest" in out["row"] and calls == [1]
        assert out["confirmation_id"] == urvenue.confirmation_ref("r1", "XS Nightclub", "2026-12-04")
    finally:
        site.__exit__()


def test_browser_navigates_months_and_detects_unposted(site_browser):
    start, mock = site_browser
    site = start({("79", "1103"): ("Encore Beach Club", {"2027-01-08": {"name": "Day", "status": "Open",
                                                                        "hours": "11:00am to 6:00pm"}})})
    try:
        with pytest.raises(urvenue.NotPosted):
            urvenue.add_guest("Encore Beach Club", date(2027, 1, 9), GUEST, ref="r2", before_submit=lambda: None)
        out = urvenue.add_guest("Encore Beach Club", date(2027, 1, 8), GUEST, ref="r2", rehearse=True)
        assert out["rehearsal"] is True
    finally:
        site.__exit__()


def test_browser_closed_event_and_wrong_venue(site_browser):
    start, mock = site_browser
    site = start({("79", "1115"): ("XS Nightclub", {"2026-11-27": {"name": "Two Friends", "status": "Closed",
                                                                   "hours": "10:30pm to 4:00am"}}),
                  ("79", "1103"): ("XS Nightclub", {})})
    try:
        with pytest.raises(urvenue.NotOpen):
            urvenue.add_guest("XS Nightclub", date(2026, 11, 27), GUEST, ref="r3", before_submit=lambda: None)
        # The Encore id showing another venue's name is refused (never books the wrong club).
        with pytest.raises(urvenue.UrvenueError):
            urvenue.add_guest("Encore Beach Club", date(2026, 11, 27), GUEST, ref="r3", before_submit=lambda: None)
    finally:
        site.__exit__()


def test_browser_existing_row_blocks_second_add(site_browser):
    start, mock = site_browser
    ev = {"name": "Diplo", "status": "Open", "hours": "10:30pm to 4:00am",
          "rows": [{"name": "Mock Guest", "email": "x", "m": 1, "f": 2}]}
    site = start({("79", "1115"): ("XS Nightclub", {"2026-12-05": ev})})
    try:
        with pytest.raises(urvenue.AlreadyListed):
            urvenue.add_guest("XS Nightclub", date(2026, 12, 5), GUEST, ref="r4",
                              before_submit=lambda: pytest.fail("must not submit"))
    finally:
        site.__exit__()


def test_browser_logs_in_two_step_and_finds_liv_nightclub(site_browser, monkeypatch):
    start, mock = site_browser
    monkeypatch.setenv("URVENUE_PASSWORD", mock.PASSWORD)
    monkeypatch.setitem(urvenue.VENUES, "LIV Nightclub", {**urvenue.VENUES["LIV Nightclub"], "vid": None})
    ev = {"2026-11-28": {"name": "LIV Saturday", "status": "Open", "hours": "10:30pm to 4:00am"}}
    site = start({("2386", "1214881"): ("LIV Beach", {}), ("2386", "777"): ("LIV Las Vegas", ev)},
                 require_login=True)
    try:
        out = urvenue.add_guest("LIV Nightclub", date(2026, 11, 28), GUEST, ref="r5", before_submit=lambda: None)
        assert out["verified"] and out["event"] == "LIV Saturday"
        assert urvenue.venue_ids("LIV Nightclub") == ("2386", "777")  # discovered via the switcher, cached
    finally:
        site.__exit__()


def test_browser_wrong_password_stops_before_anything(site_browser, monkeypatch):
    start, mock = site_browser
    monkeypatch.setenv("URVENUE_PASSWORD", "wrong")
    site = start({("79", "1115"): ("XS Nightclub", {})}, require_login=True)
    try:
        with pytest.raises(urvenue.UrvenueError) as exc:
            urvenue.add_guest("XS Nightclub", date(2026, 12, 4), GUEST, ref="r6", before_submit=lambda: None)
        assert "wrong" not in str(exc.value)
    finally:
        site.__exit__()

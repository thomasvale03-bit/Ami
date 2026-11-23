"""urvenue.me sign-ups for LIV, LIV Beach, XS and Encore (Thomas, Nov 21 2026):
booked on the venue's urvenue guest list, verified, then ONE guest + ONE team
confirmation like TAO. No event = not posted -> hold + hourly re-check.
Closed LIV/XS -> OMNIA-first fallback; closed dayclub -> hold. Never a guest email claiming booked when nothing was added."""
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
    monkeypatch.setenv("URVENUE_API_DISABLE", "true")  # browser-path tests; never hit the live API
    monkeypatch.setenv("AMY_WORKER_DIR", str(tmp_path / "wd"))
    monkeypatch.delenv("URVENUE_VENUE_IDS", raising=False)
    monkeypatch.setenv("LIV_NIGHT_RULES", str(tmp_path / "no-liv-rules.json"))  # never the real file


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
        # Tom's LIV format (Nov 21 2026); no terms on file -> none invented.
        heading = "LIV BEACH @ FONTAINEBLEAU HOTEL" if canon == "LIV Beach" else "LIV @ FONTAINEBLEAU HOTEL"
        for bit in ("TOM\u2019s V (PME) GUESTLIST", "(Footfall promoter)", "Save or Screenshot This EMAIL to get in",
                    heading, "Wednesday Nov 25 - Rufus Du Sol", "Guestlist terms will be confirmed",
                    "NO ATHLETIC GEAR", "NO SHORTS(men)", "This Email Is Your Ticket",
                    "*U R on TOM Vale (PME) guestlist", "-Show this EMAIL at Check in & ID"):
            assert bit in guest["text"], bit
            assert bit.replace("&", "&amp;") in guest["html"], bit
        for bad in ("M - ", "F - ", "Comp", "Guestlist til", "Text"):
            assert bad not in guest["text"], bad
        if canon == "LIV Nightclub":
            assert "LIV BEACH" not in guest["text"]
        assert "wynn" not in (guest["text"] + guest["html"]).lower()
        assert "LIV night terms MISSING" in team["text"]
        assert "grab your passes" not in guest["html"] and "show it with your ID" in guest["html"]
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
    assert "Order ID ORDER-" in g["text"] and "XS Nightclub (Rufus Du Sol, 10:30pm — on the guest list)" in g["text"]
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
    assert "LIV @ FONTAINEBLEAU HOTEL" in g["text"] and "Saturday Nov 28 - Rufus Du Sol" in g["text"]
    assert "Friday Nov 27" not in g["text"].split("TOM\u2019s V")[1]  # XS night is not in the LIV block
    assert "LIV @ FONTAINEBLEAU HOTEL" in g["html"] and "TAO Group Hospitality Rewards" not in g["html"]


def _rules(tmp_path, monkeypatch, nights):
    import json
    path = tmp_path / "liv-night-rules.json"
    path.write_text(json.dumps({"_example": {"LIV Nightclub|2026-11-25": {"men": "EXAMPLE"}}, "nights": nights}))
    monkeypatch.setenv("LIV_NIGHT_RULES", str(path))


def _alerts(tmp_path):
    import json
    path = tmp_path / "w" / "alerts.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_liv_rules_fill_the_night_block(tmp_path, monkeypatch):
    _rules(tmp_path, monkeypatch, {"LIV Nightclub|2026-11-28": {"event": "Dom Dolla", "men": "Comp",
                                                               "women": "Comp Open Bar", "cutoff": "1:30am"}})
    s, api, worker = mk(tmp_path, FakeUrvenue())
    s.enqueue("r", raw(venues=["LIV"], start_date="2026-11-28", end_date="2026-11-28"), NOW)
    assert worker.run_once()["status"] == "done"
    g, team = api.sent
    assert "Saturday Nov 28 - Dom Dolla\nM - Comp\nF - Comp Open Bar\nGuestlist til 1:30am" in g["text"]
    assert "Guestlist terms will be confirmed" not in g["text"]
    assert "Guestlist til 1:30am" in g["html"]
    assert "MISSING" not in team["text"]
    assert _alerts(tmp_path) == []


def test_liv_example_entries_are_never_used(tmp_path, monkeypatch):
    _rules(tmp_path, monkeypatch, {})
    s, api, worker = mk(tmp_path, FakeUrvenue())
    s.enqueue("x", raw(venues=["LIV"]), NOW)
    assert worker.run_once()["status"] == "done"
    assert "EXAMPLE" not in api.sent[0]["text"] and "Guestlist terms will be confirmed" in api.sent[0]["text"]
    notes = _alerts(tmp_path)
    assert len(notes) == 1 and notes[0]["kind"] == "liv_rules_missing"
    item = notes[0]["items"][0]
    assert item["ref"] == "x" and item["dates"] == "2026-11-25" and "LIV Nightclub" in item["venue"]


def test_liv_beach_and_liv_trip_two_headings_partial_rules(tmp_path, monkeypatch):
    _rules(tmp_path, monkeypatch, {"LIV Beach|2026-11-28": {"men": "Comp", "women": "Comp", "cutoff": "3pm",
                                                           "notes": "Pool open"}})
    s, api, worker = mk(tmp_path, FakeUrvenue())
    s.enqueue("b", raw(venues=["LIV Beach", "LIV"], start_date="2026-11-28", end_date="2026-11-28",
                       itinerary={"2026-11-28": ["LIV Beach", "LIV Nightclub"]}), NOW)
    assert worker.run_once()["status"] == "done"
    t = api.sent[0]["text"]
    assert t.index("LIV @ FONTAINEBLEAU HOTEL") < t.index("LIV BEACH @ FONTAINEBLEAU HOTEL")
    beach = t.split("LIV BEACH @ FONTAINEBLEAU HOTEL")[1]
    assert "Guestlist til 3pm" in beach and "Pool open" in beach
    night = t.split("LIV @ FONTAINEBLEAU HOTEL")[1].split("LIV BEACH")[0]
    assert "Guestlist terms will be confirmed" in night and "M - " not in night
    assert _alerts(tmp_path)[0]["items"][0]["dates"] == "2026-11-28"


def test_xs_confirmation_has_no_liv_block(tmp_path):
    s, api, worker = mk(tmp_path, FakeUrvenue())
    s.enqueue("w", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "done"
    assert "TOM\u2019s V" not in api.sent[0]["text"] and _alerts(tmp_path) == []


def test_rules_file_template_has_no_real_nights(tmp_path, monkeypatch):
    import json
    import liv_rules
    monkeypatch.setenv("LIV_NIGHT_RULES", str(tmp_path / "new" / "rules.json"))
    assert liv_rules.ensure_file() is True and liv_rules.ensure_file() is False
    data = json.loads((tmp_path / "new" / "rules.json").read_text())
    assert data["nights"] == {} and liv_rules.load() == {}
    assert "Dom Dolla" in json.dumps(data["_example"])


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


def test_browser_ng_select_item_behind_input(site_browser, tmp_path):
    """Live crash Nov 21 2026 (job 1a20042c105c1c11): the ng-select's <input>
    covers '- Select Item -', so a plain click on the placeholder times out.
    _choose_item opens it via .ng-select-container and verifies General List."""
    start, mock = site_browser
    site = start({("79", "1115"): ("XS Nightclub", {"2026-11-22": {"name": "Sunday", "status": "Open",
                                                                   "hours": "10:30pm to 4:00am", "ng": True}})})
    try:
        out = urvenue.add_guest("XS Nightclub", date(2026, 11, 22), GUEST, ref="ng1",
                                screenshot_path=str(tmp_path / "ng.png"), before_submit=lambda: None)
        assert out["verified"] and "Mock Guest" in out["row"] and "General List" in out["row"]
        assert (tmp_path / "ng.png").exists()
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


def test_browser_adds_even_when_a_row_already_exists(site_browser):
    start, mock = site_browser
    ev = {"name": "Diplo", "status": "Open", "hours": "10:30pm to 4:00am",
          "rows": [{"name": "Mock Guest", "email": "x", "m": 1, "f": 2}]}
    site = start({("79", "1115"): ("XS Nightclub", {"2026-12-05": ev})})
    try:
        out = urvenue.add_guest("XS Nightclub", date(2026, 12, 5), GUEST, ref="r4",
                                before_submit=lambda: None)
        assert out["verified"] is True
        assert "Mock Guest" in (out.get("row") or "")
    finally:
        site.__exit__()


def test_browser_existing_row_blocks_second_add_when_dedupe_on(site_browser, monkeypatch):
    monkeypatch.setenv("AMY_DEDUPE", "true")
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


# --- "There are no items available" + reused bookings (Nov 21 2026) ----------

def test_no_items_nye_liv_falls_back_then_holds_with_ticket_link(tmp_path):
    """LIV NYE has no guest list items: closed -> OMNIA-first fallback; no NYE
    list anywhere -> one hold email carrying the promoter ticket link."""
    class NoItems(FakeUrvenue):
        def add(self, venue, night, guest, **kw):
            raise urvenue.NotOpen("no guest list items", "Tiesto - NYE")
    tao = FakeTao(posted=False)
    s, api, worker = mk(tmp_path, NoItems(), tao)
    s.enqueue("n", raw(venues=["LIV"], start_date="2026-12-31", end_date="2026-12-31"), NOW)
    assert worker.run_once()["status"] == "retry"
    assert tao.submits == []
    assert len(api.sent) == 1
    hold = api.sent[0]
    assert "on hold" in hold["subject"] and hold["bcc"] == TEAM
    assert "utm_id=68d79ff587c84397b19f00330a1e6107" in hold["text"]
    assert "OMNIA doesn't have a guest list on NYE" in hold["text"]
    assert s.get("n")["state"] == "retry"


def test_no_items_liv_beach_goes_on_hold(tmp_path):
    class NoItems(FakeUrvenue):
        def add(self, venue, night, guest, **kw):
            raise urvenue.NotOpen("no guest list items", "Day")
    s, api, worker = mk(tmp_path, NoItems())
    s.enqueue("nb", raw(venues=["LIV Beach"]), NOW)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1 and "on hold" in api.sent[0]["subject"]


def _seed_reused(worker, ref, venue, night, order="0138d09f-cc19-4a55-8bca-bc94f5c1b269"):
    worker.ledger.put(bw.night_key(ref, venue, night), {
        "state": "booked",
        "record": {"date": night, "order_id": order, "party": {"female_count": 2, "male_count": 1},
                   "venue": venue, "verified": True},
        "meta": {"event": "Bijou", "event_time": "11:00 AM", "reused_from_job": "older-job",
                 "confirmation_url": f"https://tickets.taogroup.com/orders/confirmation/{order}/ordered=true"}})


def test_reused_only_with_unposted_night_sends_hold_not_fresh_confirmation(tmp_path):
    uv = FakeUrvenue({("LIV Nightclub", "2026-11-25"): "notposted"})
    s, api, worker = mk(tmp_path, uv, FakeTao(dayclubs=True))
    _seed_reused(worker, "e", "Marquee Dayclub", "2026-11-25")
    s.enqueue("e", raw(venues=["LIV"]), NOW)
    assert worker.run_once()["status"] == "retry"
    assert len(api.sent) == 1
    hold = api.sent[0]
    assert "on hold" in hold["subject"] and "You're on the Playmaker" not in hold["subject"]
    assert "Any nights that already went through" not in hold["text"]
    assert worker.tao.submits == []  # Marquee Dayclub never rebooked
    # list posts -> ONE "Updated:" confirmation with both nights, one team email
    uv.outcomes.clear()
    api.now += 3600
    worker.now = lambda: api.now
    assert worker.run_once()["status"] == "done"
    guest = [p for p in api.sent if p["to"] == "guest@example.invalid"]
    assert len(guest) == 2 and guest[-1]["subject"].startswith("Updated: You're on the Playmaker")
    assert "Marquee Dayclub" in guest[-1]["text"] and "LIV Nightclub" in guest[-1]["text"]
    assert len([p for p in api.sent if p["to"] == TEAM]) == 1
    assert worker.tao.submits == [] and len(uv.adds) == 1


def test_reused_plus_new_booking_is_an_updated_confirmation(tmp_path):
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv, FakeTao(dayclubs=True))
    _seed_reused(worker, "hb", "Marquee Dayclub", "2026-11-25")
    s.enqueue("hb", raw(venues=["XS"]), NOW)
    assert worker.run_once()["status"] == "done"
    guest, team = api.sent
    assert guest["subject"].startswith("Updated: You're on the Playmaker") and "UV-" not in guest["subject"]
    assert worker.tao.submits == [] and len(uv.adds) == 1


def test_fresh_partial_confirmation_unchanged_when_nothing_reused(tmp_path):
    """A newly booked night + an unposted night still gets the early partial
    confirmation ("Still to come"), as before."""
    uv = FakeUrvenue({("LIV Nightclub", "2026-11-26"): "notposted"})
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("p", raw(venues=["XS", "LIV"], start_date="2026-11-25", end_date="2026-11-26",
                       itinerary={"2026-11-25": ["XS Nightclub"], "2026-11-26": ["LIV Nightclub"]}), NOW)
    assert worker.run_once()["status"] == "retry"
    guest = [p for p in api.sent if p["to"] == "guest@example.invalid"]
    assert len(guest) == 1 and guest[0]["subject"].startswith("You're on the Playmaker")
    assert "Still to come" in guest[0]["text"]


def test_browser_no_items_popup_is_closed_not_error(site_browser):
    start, mock = site_browser
    site = start({("2386", "1121561"): ("LIV Nightclub", {"2026-12-31": {"name": "Tiesto - NYE", "status": "Open",
                                                                         "hours": "10:30pm to 4:00am",
                                                                         "no_items": True}})})
    try:
        with pytest.raises(urvenue.NotOpen) as exc:
            urvenue.add_guest("LIV Nightclub", date(2026, 12, 31), GUEST, ref="ni",
                              before_submit=lambda: pytest.fail("must not reach submit"))
        assert exc.value.status == "no guest list items" and "Tiesto" in exc.value.event
    finally:
        site.__exit__()

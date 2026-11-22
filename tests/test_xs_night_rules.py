"""Thomas (Nov 21 2026, t1153u): XS nightly rules like LIV. Parallel file
xs-night-rules.json (keys 'XS Nightclub|YYYY-MM-DD'); terms shown in the Wynn
section of XS confirmations; a 'no list' night is closed (XS -> OMNIA first)."""
import json

import liv_rules
import worker_emails as we
from tests.test_booking_worker import NOW, raw
from tests.test_booking_worker import isolated  # noqa: F401
from tests.test_urvenue import FakeUrvenue, mk, urvenue_env  # noqa: F401

XS_RULES = {
    "XS Nightclub|2026-11-21": {"event": "CALVIN HARRIS", "women": "no list", "men": "no list"},
    "XS Nightclub|2026-11-22": {"event": "RUFUS DU SOL", "women": "ladies free until 1am",
                                "men": "guys free until 11pm / reduced until 1am", "drinks": "N/A"},
    "XS Nightclub|2026-11-25": {"event": "TEST NIGHT", "women": "ladies free until 2am",
                                "men": "guys free until 1am", "drinks": "open bar until 11pm"},
}


def _xs(tmp_path, monkeypatch, nights=XS_RULES, liv=None):
    p = tmp_path / "xs-night-rules.json"
    p.write_text(json.dumps({"nights": nights}))
    monkeypatch.setenv("XS_NIGHT_RULES", str(p))
    if liv is not None:
        q = tmp_path / "liv-night-rules.json"
        q.write_text(json.dumps({"nights": liv}))
        monkeypatch.setenv("LIV_NIGHT_RULES", str(q))


def test_load_merges_liv_and_xs_files(tmp_path, monkeypatch):
    _xs(tmp_path, monkeypatch, liv={"LIV Nightclub|2026-11-21": {"men": "Comp"}})
    rules = liv_rules.load()
    assert "LIV Nightclub|2026-11-21" in rules and "XS Nightclub|2026-11-22" in rules
    assert liv_rules.for_night("XS Nightclub", "2026-11-22")["event"] == "RUFUS DU SOL"


def test_is_no_list():
    assert liv_rules.is_no_list({"women": "no list", "men": "No List"}, 1, 4)
    assert liv_rules.is_no_list({"no_list": True})
    assert not liv_rules.is_no_list({"women": "no list", "men": "guys free until 1am"}, 1, 1)
    assert liv_rules.is_no_list({"women": "no list", "men": "guys free"}, 2, 0)  # all-female party
    assert not liv_rules.is_no_list({}, 1, 1)
    assert not liv_rules.is_no_list({"women": "ladies free until 2am", "men": "guys free"}, 1, 1)


def test_xs_confirmation_shows_terms_in_wynn_section(tmp_path, monkeypatch):
    _xs(tmp_path, monkeypatch)
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("x", raw(venues=["XS"]), NOW)   # night 2026-11-25
    assert worker.run_once()["status"] == "done"
    g = api.sent[0]
    assert "XS guest list terms:" in g["text"]
    assert "F - ladies free until 2am\nM - guys free until 1am\nDrinks - open bar until 11pm" in g["text"]
    html = g["html"]
    assert "XS GUEST LIST TERMS" in html and "TEST NIGHT" in html
    assert html.index("Your Wynn Resorts app reservation") < html.index("XS GUEST LIST TERMS")


def test_drinks_na_hidden_and_no_rule_shows_nothing(tmp_path, monkeypatch):
    _xs(tmp_path, monkeypatch)
    xs = {"venue": "XS Nightclub", "date": "2026-11-22", "order_id": "UV-1", "system": "urvenue",
          "event": "Rufus", "event_time": "10:30pm"}
    req = {"first_name": "M", "email": "g@example.invalid", "start_date": "2026-11-22", "end_date": "2026-11-22",
           "female_count": 1, "male_count": 1}
    p = we.guest_email(req, [xs])
    assert "RUFUS DU SOL" in p["text"] and "Drinks" not in p["text"]
    p2 = we.guest_email({**req, "start_date": "2026-11-27", "end_date": "2026-11-27"}, [{**xs, "date": "2026-11-27"}])
    assert "XS guest list terms" not in p2["text"] and "XS GUEST LIST TERMS" not in p2["html"]


def test_xs_no_list_night_is_closed_and_falls_back_to_omnia(tmp_path, monkeypatch):
    _xs(tmp_path, monkeypatch)
    uv = FakeUrvenue()
    s, api, worker = mk(tmp_path, uv)
    s.enqueue("c", raw(venues=["XS"], start_date="2026-11-21", end_date="2026-11-21"), NOW)
    worker.run_once()
    assert uv.adds == []   # nothing submitted on urvenue
    rec = s.get("c")["metadata"]["evidence"]["bookings"][0]
    assert rec["venue"] == "OMNIA Nightclub" and rec["fallback"] is True

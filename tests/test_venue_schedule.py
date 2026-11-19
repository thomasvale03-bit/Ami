"""Verified venue operating schedule: dark nights skip TAO and go to fallback."""
from datetime import date

from config import rules
from rules_engine import fallback_venues_for, venue_operates_that_night


def test_verified_open_days_and_hours():
    sunday, monday, wednesday, friday, saturday = (
        date(2026, 11, 15), date(2026, 11, 16), date(2026, 11, 25),
        date(2026, 11, 13), date(2026, 11, 14),
    )
    assert rules.venue_open_on("OMNIA Nightclub", sunday) is True
    assert rules.venue_open_on("OMNIA Nightclub", monday) is False
    assert rules.venue_open_on("Hakkasan Nightclub", wednesday) is True
    assert rules.venue_open_on("Hakkasan Nightclub", sunday) is False
    assert rules.venue_open_on("JEWEL Nightclub", friday) is True
    assert rules.venue_open_on("JEWEL Nightclub", sunday) is False
    assert rules.venue_open_on("JEWEL Nightclub", date(2026, 11, 21)) is True
    assert rules.venue_open_on("JEWEL Nightclub", date(2026, 11, 22)) is False
    assert rules.venue_open_on("JEWEL Nightclub", date(2026, 11, 23)) is False  # Monday after last night
    assert rules.venue_open_on("Marquee Nightclub", monday) is True
    assert rules.venue_open_on("Marquee Nightclub", sunday) is False
    assert rules.venue_open_on("TAO Nightclub", sunday) is True
    assert rules.venue_open_on("TAO Nightclub", monday) is False
    assert rules.venue_open_on("Marquee Dayclub", monday) is True
    assert rules.venue_open_on("LAVO Party Brunch", saturday) is True
    assert rules.venue_open_on("LAVO Party Brunch", friday) is False
    assert rules.venue_open_on("LAVO Party Brunch", date(2027, 4, 3)) is False  # out of Oct-Mar season
    assert rules.venue_open_on("Drai's", thursday := date(2026, 11, 19)) is True
    assert rules.venue_open_on("Drai's After Hours", monday) is False
    assert rules.VENUE_SCHEDULE["OMNIA Nightclub"]["doors"] == "10:30 PM"
    assert rules.VENUE_SCHEDULE["Marquee Dayclub"]["closes"] == "6:00 PM"
    assert rules.VENUE_SCHEDULE["LAVO Party Brunch"]["doors"] == "12:00 PM"
    assert rules.VENUE_SCHEDULE["Drai's After Hours"]["doors"] == "1:00 AM"


def test_tbd_venues_are_never_treated_as_closed():
    friday = date(2026, 11, 13)
    for name in ("XS Nightclub", "LIV Nightclub", "LIV Beach", "Encore Beach Club",
                 "TAO Beach Dayclub", "OMNIA Dayclub", "Palm Tree Beach Club",
                 "Liquid Pool Lounge"):
        entry = rules.venue_schedule(name)
        assert entry and entry["status"] == "tbd" and entry["days"] == []
        assert rules.venue_open_on(name, friday) is None


def test_operates_uses_schedule_and_tbd_keeps_heuristic():
    sunday = date(2026, 11, 15)
    monday = date(2026, 11, 16)
    assert venue_operates_that_night("Hakkasan Nightclub", sunday) is False
    assert venue_operates_that_night("Marquee Nightclub", monday) is True
    assert venue_operates_that_night("OMNIA Nightclub", monday) is False
    # tbd: never forced closed; XS is not on weekday routing, same as before
    assert venue_operates_that_night("XS Nightclub", date(2026, 11, 13)) is False


def test_fallback_list_skips_venues_that_do_not_operate():
    sunday = fallback_venues_for(date(2026, 11, 15))
    assert sunday[0] == "OMNIA Nightclub"
    assert "TAO Nightclub" in sunday
    assert "Hakkasan Nightclub" not in sunday
    assert "JEWEL Nightclub" not in sunday
    assert "Marquee Nightclub" not in sunday
    friday = fallback_venues_for(date(2026, 11, 13))
    assert "JEWEL Nightclub" in friday
    assert "Hakkasan Nightclub" in friday
    assert "Marquee Nightclub" in friday

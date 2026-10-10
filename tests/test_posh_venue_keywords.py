"""Branded Posh event names resolve to a venue by keyword (order 37448453)."""
from datetime import datetime, timezone

import posh
import posh_webhook


def test_branded_names_match_venue_keyword():
    cases = {
        "DESEO: OMNIA": "OMNIA Nightclub",
        "R&BAE | Hakkasan": "Hakkasan Nightclub",
        "Guestlist | TAO NC": "TAO Nightclub",
        "Saturdays at marquee": "Marquee Nightclub",
        "jewel fridays": "JEWEL Nightclub",
        "Liquid Sundays": "Liquid Pool Lounge",
        "LAVO Brunch": "LAVO Party Brunch",
        "OMNIA Dayclub Pool Party": "OMNIA Dayclub",
    }
    for name, venue in cases.items():
        assert posh.venue_from_event(name) == venue, name


def test_keyword_inside_other_word_does_not_match():
    assert posh.venue_from_event("Taotastic Night") is None
    assert posh.venue_from_event("Riot House | Guest List") is None


def test_deseo_resolves_to_sunday_nov_1():
    # Posh page JSON-LD: startDate 2026-11-01T10:30:00-08:00 (Nov 1 night, end 4 AM Nov 2)
    start = datetime(2026, 11, 1, 18, 30, tzinfo=timezone.utc)
    payload = {"event_id": "6aae53d9c6a734a4ef0cf3c9", "event_name": "DESEO: OMNIA",
               "event_start": "2026-09-27T22:30:00.000Z", "date_purchased": "2026-10-10T13:24:00.000Z"}
    night, how, local = posh_webhook.resolve_night(payload, lookup=lambda *a: start)
    assert (night.isoformat(), how) == ("2026-11-01", "posh_page")
    body = posh_webhook.to_signup_text(dict(payload, account_email="x@y.com", items=[]), night, how, local)
    assert posh.parse_signup("m", body)["start_date"] == "2026-11-01"


def test_after_midnight_start_belongs_to_previous_night():
    start = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)  # 1:30 AM PST Sun = Saturday night
    night, _, _ = posh_webhook.resolve_night({"event_id": "x"}, lookup=lambda *a: start)
    assert night.isoformat() == "2026-10-31"


import unittest  # noqa: E402


class VenueKeywordTests(unittest.TestCase):
    def test_all(self):
        for fn in (test_branded_names_match_venue_keyword, test_keyword_inside_other_word_does_not_match,
                   test_deseo_resolves_to_sunday_nov_1, test_after_midnight_start_belongs_to_previous_night):
            with self.subTest(fn.__name__):
                fn()

import unittest
from datetime import date

from rules_engine import (
    ActionNeeded, canonical_venue, normalize_guest_request, resolve_venue_for_date, routed_candidates,
)


def raw_request(**overrides):
    raw = {
        "source_message_id": "m1",
        "name": "Jane Doe",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "start_date": "2026-10-02",
        "end_date": "2026-10-03",
        "venues": [],
        "female_count": "2",
        "male_count": "1",
    }
    raw.update(overrides)
    return raw


def available(*venues):
    """Checker that reports a Passes listing only for the given venues."""
    def check(venue, date_obj):
        if venue in venues:
            return {"event": f"{venue} night", "listing_type": "Passes"}
        return None
    return check


FRIDAY = date(2026, 10, 2)
SATURDAY = date(2026, 10, 3)


class NormalizeTests(unittest.TestCase):
    def test_counts_are_coerced(self):
        req = normalize_guest_request(raw_request(female_count="3", male_count="x"))
        self.assertEqual((req["female_count"], req["male_count"]), (3, 0))

    def test_zero_guests_needs_action(self):
        with self.assertRaises(ActionNeeded):
            normalize_guest_request(raw_request(female_count="0", male_count="0"))

    def test_paused_venue_needs_action_regardless_of_case(self):
        with self.assertRaises(ActionNeeded):
            normalize_guest_request(raw_request(venues="drai's"))

    def test_end_before_start_needs_action(self):
        with self.assertRaises(ActionNeeded):
            normalize_guest_request(raw_request(start_date="2026-10-05", end_date="2026-10-03"))

    def test_fallback_phone_and_zip(self):
        req = normalize_guest_request(raw_request())
        self.assertEqual(req["phone"], "480-214-5268")
        self.assertEqual(req["billing_zip"], "85306")

    def test_venue_names_canonicalized(self):
        req = normalize_guest_request(raw_request(venues="omnia, TAO Beach Dayclub"))
        self.assertEqual(req["requested_venues"], ["OMNIA Nightclub", "TAO Beach Dayclub"])


class CanonicalVenueTests(unittest.TestCase):
    def test_ambiguous_prefix_left_alone(self):
        self.assertEqual(canonical_venue("Marquee"), "Marquee")

    def test_unknown_left_alone(self):
        self.assertEqual(canonical_venue(" Somewhere Else "), "Somewhere Else")


class RoutingTests(unittest.TestCase):
    def test_requested_venue_wins(self):
        req = normalize_guest_request(raw_request(venues=["OMNIA Nightclub"]))
        venue, _ = resolve_venue_for_date(req, FRIDAY, available("OMNIA Nightclub", "JEWEL Nightclub"))
        self.assertEqual(venue, "OMNIA Nightclub")

    def test_default_routing_when_requested_unavailable(self):
        req = normalize_guest_request(raw_request(venues=["OMNIA Nightclub"]))
        venue, _ = resolve_venue_for_date(req, FRIDAY, available("JEWEL Nightclub"))
        self.assertEqual(venue, "JEWEL Nightclub")

    def test_tickets_listing_is_not_eligible(self):
        req = normalize_guest_request(raw_request())
        venue, _ = resolve_venue_for_date(
            req, FRIDAY, lambda v, d: {"event": "x", "listing_type": "Tickets"})
        self.assertIsNone(venue)

    def test_saturday_after_jewel_friday_prefers_hakkasan(self):
        req = normalize_guest_request(raw_request())
        everything = available("JEWEL Nightclub", "Hakkasan Nightclub", "Marquee Nightclub")
        venue, _ = resolve_venue_for_date(req, SATURDAY, everything, previous_night_venue="JEWEL Nightclub")
        self.assertEqual(venue, "Hakkasan Nightclub")

    def test_saturday_never_repeats_routed_friday_venue(self):
        self.assertNotIn("Hakkasan Nightclub", routed_candidates(SATURDAY, "Hakkasan Nightclub"))

    def test_requested_venue_is_not_repeated_on_saturday(self):
        req = normalize_guest_request(raw_request(venues=["JEWEL Nightclub"]))
        venue, _ = resolve_venue_for_date(
            req, SATURDAY, available("JEWEL Nightclub", "Hakkasan Nightclub"),
            previous_night_venue="JEWEL Nightclub")
        self.assertEqual(venue, "Hakkasan Nightclub")

    def test_hakkasan_then_marquee_after_jewel_friday(self):
        req = normalize_guest_request(raw_request(venues=["OMNIA Nightclub"]))
        venue, _ = resolve_venue_for_date(
            req, SATURDAY, available("JEWEL Nightclub", "Marquee Nightclub"),
            previous_night_venue="JEWEL Nightclub")
        self.assertEqual(venue, "Marquee Nightclub")

    def test_last_resorts_cover_every_night(self):
        req = normalize_guest_request(raw_request(start_date="2026-10-05", end_date="2026-10-11"))
        from rules_engine import date_range
        for d in date_range("2026-10-05", "2026-10-11"):
            venue, _ = resolve_venue_for_date(req, d, available("Marquee Nightclub"))
            self.assertEqual(venue, "Marquee Nightclub", d)

    def test_jewel_is_last_resort(self):
        wednesday = date(2026, 10, 7)  # routing: Hakkasan, then Marquee
        req = normalize_guest_request(raw_request(
            start_date="2026-10-07", end_date="2026-10-07", venues=["OMNIA Nightclub"]))
        venue, _ = resolve_venue_for_date(req, wednesday, available("JEWEL Nightclub"))
        self.assertEqual(venue, "JEWEL Nightclub")

    def test_best_available_tries_other_nightclubs(self):
        monday = date(2026, 10, 5)
        req = normalize_guest_request(raw_request(start_date="2026-10-05", end_date="2026-10-05"))
        venue, _ = resolve_venue_for_date(req, monday, available("TAO Nightclub"))
        self.assertEqual(venue, "TAO Nightclub")

    def test_paused_venue_never_checked(self):
        checked = []
        req = normalize_guest_request(raw_request())
        req["requested_venues"] = ["Drai's"]
        resolve_venue_for_date(req, FRIDAY, lambda v, d: checked.append(v))
        self.assertNotIn("Drai's", checked)


if __name__ == "__main__":
    unittest.main()

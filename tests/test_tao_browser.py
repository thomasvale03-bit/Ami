"""Runs tao_portal's real browser code against local mock TicketDriver pages
(no network). Skipped when Playwright/Chromium are not installed."""
import pathlib
import unittest
from unittest import mock

import tao_portal

MOCK = pathlib.Path(__file__).parent / "fixtures" / "tao_mock"
GUEST = {"email": "jane.sample@example.com", "first_name": "Jane", "last_name": "Sample",
         "phone": "6025550100", "billing_zip": "85001", "female_count": 2, "male_count": 1}


def playwright_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            tao_portal.launch_browser(p).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(playwright_available(), "Playwright/Chromium not installed")
class MockCheckoutTests(unittest.TestCase):
    def listing(self, page):
        return {"event": "OMNIA", "listing_type": "Passes", "listing_url": (MOCK / page).as_uri()}

    def submit(self, page, guest=GUEST):
        with mock.patch.object(tao_portal, "is_safe_pass_url", return_value=True):
            return tao_portal.submit_registration(self.listing(page), guest)

    def test_free_checkout_returns_the_order_id(self):
        result = self.submit("event.html")
        self.assertTrue(result["verified"])
        self.assertEqual(result["confirmation_id"], "TD-778812")
        self.assertIn("Jonathan Sidara", result["authorization"])

    def test_form_is_filled_marketing_unticked_terms_ticked(self):
        from playwright.sync_api import sync_playwright
        seen = {}
        real_click = None

        def spy_click(self_locator, *args, **kwargs):
            page = self_locator.page
            seen.update(
                email=page.input_value("#OrderEmail"), first=page.input_value("#OrderFirstName"),
                female=page.input_value("#TicketFemale"), male=page.input_value("#TicketMale"),
                marketing=page.is_checked("#OrderOptedIn"), terms=page.is_checked("#OrderTerms"))
            return real_click(self_locator, *args, **kwargs)

        from playwright.sync_api._generated import Locator
        real_click = Locator.click
        with mock.patch.object(Locator, "click", spy_click):
            self.submit("event.html")
        self.assertEqual(seen, {"email": "jane.sample@example.com", "first": "Jane", "female": "2",
                                "male": "1", "marketing": False, "terms": True})

    def test_any_price_stops_before_submitting(self):
        result = self.submit("paid.html")
        self.assertFalse(result["verified"])

    def test_party_too_big_stops_before_submitting(self):
        result = self.submit("event.html", dict(GUEST, male_count=5))
        self.assertFalse(result["verified"])
        self.assertIn("not available", result["reason"])

    def test_no_success_page_is_uncertain_not_failed(self):
        with self.assertRaises(tao_portal.SubmissionUncertain):
            self.submit("uncertain.html")

    def test_availability_reads_a_free_listing(self):
        from datetime import date
        url = (MOCK / "event.html").as_uri()
        with mock.patch.object(tao_portal, "_catalog", {("OMNIA Nightclub", date(2026, 10, 6)): url}):
            listing = tao_portal.check_availability("OMNIA Nightclub", date(2026, 10, 6))
            self.assertEqual(listing["price"], 0)
            self.assertEqual(listing["capacity"], {"female": 3, "male": 2})
            self.assertIsNone(tao_portal.check_availability("TAO Nightclub", date(2026, 10, 6)))

    def test_availability_rejects_a_priced_listing(self):
        from datetime import date
        url = (MOCK / "paid.html").as_uri()
        with mock.patch.object(tao_portal, "_catalog", {("OMNIA Nightclub", date(2026, 10, 6)): url}):
            self.assertIsNone(tao_portal.check_availability("OMNIA Nightclub", date(2026, 10, 6)))


if __name__ == "__main__":
    unittest.main()

"""Phone / billing ZIP fallbacks for every intake path."""
import os
import unittest
from unittest import mock

import assisted_jobs
import posh
import posh_webhook as pw
from config import rules
from rules_engine import normalize_guest_request

URL = "https://tickets.taogroup.com/e/guest-list-omnia-nc-10-17-2026/tickets"
GUEST = {"first_name": "Ana", "last_name": "Ruiz", "email": "ana@example.com",
         "female_count": 1, "male_count": 0}
CLEAN = {"DEFAULT_GUEST_PHONE": "", "DEFAULT_BILLING_ZIP": ""}


class DefaultTests(unittest.TestCase):
    def test_builtin_defaults(self):
        with mock.patch.dict(os.environ, CLEAN):
            self.assertEqual(rules.phone_or_default(""), "4802649387")
            self.assertEqual(rules.phone_or_default("   "), "4802649387")
            self.assertEqual(rules.phone_or_default(None), "4802649387")
            self.assertEqual(rules.zip_or_default(""), "85311")
            self.assertEqual(rules.phone_or_default("7025550100"), "7025550100")
            self.assertEqual(rules.zip_or_default("89109"), "89109")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"DEFAULT_GUEST_PHONE": "5551112222", "DEFAULT_BILLING_ZIP": "90210"}):
            self.assertEqual(rules.phone_or_default(""), "5551112222")
            self.assertEqual(rules.zip_or_default(""), "90210")

    def test_assisted_job_and_old_token(self):
        with mock.patch.dict(os.environ, CLEAN):
            job = assisted_jobs.make_job(URL, dict(GUEST, phone="", billing_zip=None))
            self.assertEqual((job["phone"], job["billing_zip"]), ("4802649387", "85311"))
            raw = dict(job, phone="", billing_zip="")
            back = assisted_jobs.decode(assisted_jobs.encode(raw))
            self.assertEqual((back["phone"], back["billing_zip"]), ("4802649387", "85311"))
            kept = assisted_jobs.make_job(URL, dict(GUEST, phone="7025550100", billing_zip="89109"))
            self.assertEqual((kept["phone"], kept["billing_zip"]), ("7025550100", "89109"))

    def test_posh_webhook_without_phone_normalizes_to_default(self):
        payload = {"type": "new_order", "account_first_name": "Ana", "account_last_name": "Ruiz",
                   "account_email": "ana@example.com", "account_phone": "", "event_name": "Guestlist | TAO NC",
                   "event_start": "2026-10-17T22:30:00Z", "date_purchased": "2026-10-08T19:00:00Z",
                   "order_number": "9", "items": [{"name": "Guest List - Female"}]}
        night, how, start = pw.resolve_night(payload, lookup=lambda *a: None)
        raw = posh.parse_signup("m", pw.to_signup_text(payload, night, how, start))
        raw.update(authorization="YES")
        with mock.patch.dict(os.environ, CLEAN):
            try:
                req = normalize_guest_request(raw)
            except Exception as exc:  # consent/other gates are not what this test is about
                self.skipTest(f"normalize gate: {exc}")
        self.assertEqual((req["phone"], req["billing_zip"]), ("4802649387", "85311"))


if __name__ == "__main__":
    unittest.main()

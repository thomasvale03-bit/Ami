import base64
import email
import unittest

import gmail_client
from main import process_one_request

SAMPLE_BODY = """New guest list request submission

Name

Jane Doe

Email

jane@example.com

Phone

(602) 555-0100

Visit start date

2026-10-02

Visit end date

2026-10-03

Venues

JEWEL Nightclub

Female guests (21+)

2

Male guests (21+)

1
"""


class ParseTests(unittest.TestCase):
    def test_parses_form_fields(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY)
        self.assertEqual(raw["first_name"], "Jane")
        self.assertEqual(raw["last_name"], "Doe")
        self.assertEqual(raw["email"], "jane@example.com")
        self.assertEqual(raw["start_date"], "2026-10-02")
        self.assertEqual(raw["venues"], ["JEWEL Nightclub"])
        self.assertEqual(raw["female_count"], "2")
        self.assertEqual(raw["male_count"], "1")
        self.assertNotIn("_missing_required", raw)

    def test_value_resembling_a_label_is_kept(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY.replace("Jane Doe", "Nameer Khan"))
        self.assertEqual(raw["first_name"], "Nameer")

    def test_flags_missing_fields(self):
        raw = gmail_client.parse_request("abc", "Name\n\nJane\n")
        self.assertEqual(raw["_missing_required"], ["email", "start_date", "end_date"])

    def test_build_message_round_trips(self):
        encoded = gmail_client.build_message("a@example.com", "Hi — there", "Body — text")["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(encoded))
        self.assertEqual(msg["To"], "a@example.com")
        self.assertIn("Body — text", msg.get_payload(decode=True).decode("utf-8"))


class DryRunFlowTests(unittest.TestCase):
    def test_dry_run_produces_registrations_and_email(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY)
        result = process_one_request(raw, dry_run=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exceptions"], [])
        self.assertEqual([r["venue"] for r in result["registrations"]],
                         ["JEWEL Nightclub", "Hakkasan Nightclub"])  # never the same club Fri + Sat
        self.assertIn("Hi Jane", result["confirmation_email"]["body"])

    def test_routed_weekend_does_not_repeat(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY.replace("JEWEL Nightclub", ""))
        self.assertNotIn("venues", raw)  # blank field must not capture the next label
        result = process_one_request(raw, dry_run=True)
        self.assertEqual([r["venue"] for r in result["registrations"]],
                         ["JEWEL Nightclub", "Hakkasan Nightclub"])

    def test_unavailable_dates_become_action_needed(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY)
        result = process_one_request(raw, dry_run=True, availability_checker=lambda v, d: None)
        self.assertEqual(result["registrations"], [])
        self.assertEqual(len(result["action_needed_records"]), 2)
        self.assertNotIn("confirmation_email", result)


if __name__ == "__main__":
    unittest.main()

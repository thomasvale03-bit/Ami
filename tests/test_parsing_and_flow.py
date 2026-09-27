import base64
import email
import pathlib
import unittest
from datetime import date

import gmail_client
from main import process_one_request

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 27)

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


class RealEmailFormatTests(unittest.TestCase):
    """Built from a real GoDaddy/Airo notification (customer details replaced)."""

    def setUp(self):
        html = (FIXTURES / "godaddy_request.html").read_text()
        self.raw = gmail_client.parse_request("abc", gmail_client.html_to_text(html))

    def test_reads_fields_from_html_part(self):
        self.assertEqual(self.raw["first_name"], "Jane")
        self.assertEqual(self.raw["last_name"], "Sample Doe")
        self.assertEqual(self.raw["email"], "jane.sample@example.com")
        self.assertEqual((self.raw["start_date"], self.raw["end_date"]), ("2026-10-12", "2026-10-16"))
        self.assertEqual(self.raw["venues"], ["Omnia Nightclub", "Hakkasan Nightclub", "Marquee Nightclub",
                                              "TAO Nightclub", "Jewel Nightclub"])
        self.assertEqual((self.raw["female_count"], self.raw["male_count"]), ("5", "8"))
        self.assertNotIn("_missing_required", self.raw)

    def test_not_provided_promoter_is_empty(self):
        self.assertNotIn("promoter", self.raw)

    def test_promoter_names_combined(self):
        html = (FIXTURES / "godaddy_request.html").read_text()
        html = html.replace("first name</b><br/>\n        Not provided", "first name</b><br/>\n        Alex", 1)
        html = html.replace("last name</b><br/>\n        Not provided", "last name</b><br/>\n        Rivera", 1)
        raw = gmail_client.parse_request("abc", gmail_client.html_to_text(html))
        self.assertEqual(raw["promoter"], "Alex Rivera")

    def test_blank_male_count_is_skipped(self):
        html = (FIXTURES / "godaddy_request.html").read_text().replace(
            "12am)</b><br/>\n        8", "12am)</b><br/>\n        ")
        raw = gmail_client.parse_request("abc", gmail_client.html_to_text(html))
        self.assertNotIn("male_count", raw)
        self.assertEqual(raw["female_count"], "5")

    def test_plain_text_part_alone_is_missing_dates(self):
        raw = gmail_client.parse_request("abc", (FIXTURES / "godaddy_request.txt").read_text())
        self.assertEqual(raw["_missing_required"], ["start_date", "end_date"])

    def test_get_form_text_prefers_html(self):
        def b64(s):
            return base64.urlsafe_b64encode(s.encode()).decode()
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64((FIXTURES / "godaddy_request.txt").read_text())}},
            {"mimeType": "text/html", "body": {"data": b64((FIXTURES / "godaddy_request.html").read_text())}},
        ]}

        class FakeService:
            def users(self): return self
            def messages(self): return self
            def get(self, **kw): return self
            def execute(self): return {"payload": payload}

        _, text = gmail_client.get_form_text(FakeService(), "abc")
        self.assertIn("Visit start date", text)

    def test_past_nights_are_skipped(self):
        result = process_one_request(self.raw, dry_run=True, today=date(2026, 10, 15))
        self.assertEqual([r["date"] for r in result["registrations"]], ["2026-10-15", "2026-10-16"])


class DryRunFlowTests(unittest.TestCase):
    def test_dry_run_produces_registrations_and_email(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY)
        result = process_one_request(raw, dry_run=True, today=TODAY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exceptions"], [])
        self.assertEqual([r["venue"] for r in result["registrations"]],
                         ["JEWEL Nightclub", "Hakkasan Nightclub"])  # never the same club Fri + Sat
        self.assertIn("Hi Jane", result["confirmation_email"]["body"])

    def test_routed_weekend_does_not_repeat(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY.replace("JEWEL Nightclub", ""))
        self.assertNotIn("venues", raw)  # blank field must not capture the next label
        result = process_one_request(raw, dry_run=True, today=TODAY)
        self.assertEqual([r["venue"] for r in result["registrations"]],
                         ["JEWEL Nightclub", "Hakkasan Nightclub"])

    def test_tuesday_without_omnia_is_skipped_quietly(self):
        body = SAMPLE_BODY.replace("2026-10-02", "2026-10-06").replace("2026-10-03", "2026-10-06")
        raw = gmail_client.parse_request("abc", body)
        result = process_one_request(raw, dry_run=True, availability_checker=lambda v, d: None, today=TODAY)
        self.assertEqual(result["registrations"], [])
        self.assertEqual(result["exceptions"], [])
        self.assertNotIn("action_needed_records", result)

    def test_unavailable_dates_become_action_needed(self):
        raw = gmail_client.parse_request("abc", SAMPLE_BODY)
        result = process_one_request(raw, dry_run=True, availability_checker=lambda v, d: None, today=TODAY)
        self.assertEqual(result["registrations"], [])
        self.assertEqual(len(result["action_needed_records"]), 2)
        self.assertNotIn("confirmation_email", result)


if __name__ == "__main__":
    unittest.main()

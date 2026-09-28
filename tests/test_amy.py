import base64
import email
import pathlib
import unittest
from datetime import date
from unittest import mock

import gmail_client
import main
from rules_engine import ActionNeeded, candidate_venues, normalize_guest_request

FIXTURE = (pathlib.Path(__file__).parent / "fixtures" / "new_form.txt").read_text()
TODAY = date(2026, 9, 28)


def parsed(text=FIXTURE):
    return gmail_client.parse_request("msg-1", text)


class ParseTests(unittest.TestCase):
    """Layout copied from a real Sept 28 2026 notification (details replaced)."""

    def test_reads_every_field(self):
        raw = parsed()
        self.assertEqual((raw["first_name"], raw["last_name"]), ("Jane", "Sample"))
        self.assertEqual(raw["email"], "jane.sample@example.com")
        self.assertEqual(raw["phone"], "6025550100")
        self.assertEqual(raw["billing_zip"], "85001")
        self.assertEqual((raw["start_date"], raw["end_date"]), ("2026-10-30", "2026-10-31"))
        self.assertEqual(raw["venues"], ["OMNIA Nightclub"])
        self.assertEqual((raw["female_count"], raw["male_count"]), ("2", "1"))
        self.assertEqual(raw["authorization"], "YES")
        self.assertEqual(raw["submitted_at"], "09/28/2026, 03:47:46 AM PT")
        self.assertIsNone(raw["promoter"])
        self.assertNotIn("_missing_required", raw)

    def test_header_email_is_not_used_as_form_data(self):
        text = FIXTURE.replace("Email: jane.sample@example.com\n\nSubmitted", "Email: header@example.com\n\nSubmitted")
        self.assertEqual(parsed(text)["email"], "jane.sample@example.com")

    def test_missing_consent_needs_action(self):
        with self.assertRaises(ActionNeeded):
            normalize_guest_request(parsed(FIXTURE.replace("Authorization accepted: YES", "Authorization accepted: NO")))

    def test_blank_male_count_is_zero(self):
        request = normalize_guest_request(parsed(FIXTURE.replace("Male guests: 1", "Male guests:")))
        self.assertEqual((request["female_count"], request["male_count"]), (2, 0))


class RoutingTests(unittest.TestCase):
    def order(self, venues, day, prev=None):
        return candidate_venues({"requested_venues": venues}, day, prev)

    def test_several_picks_follow_the_nights_schedule(self):
        picks = ["OMNIA Nightclub", "Hakkasan Nightclub", "Marquee Nightclub", "TAO Nightclub", "JEWEL Nightclub"]
        firsts = [self.order(picks, date(2026, 10, d))[0] for d in (12, 13, 14, 15, 16)]
        self.assertEqual(firsts, ["Marquee Nightclub", "OMNIA Nightclub", "Hakkasan Nightclub",
                                  "Hakkasan Nightclub", "JEWEL Nightclub"])

    def test_single_pick_beats_the_schedule(self):
        self.assertEqual(self.order(["OMNIA Nightclub"], date(2026, 10, 12))[0], "OMNIA Nightclub")

    def test_extra_backups_except_tuesday(self):
        self.assertEqual(self.order([], date(2026, 10, 18)),
                         ["TAO Nightclub", "JEWEL Nightclub", "Hakkasan Nightclub", "Marquee Nightclub"])
        self.assertEqual(self.order([], date(2026, 10, 13)), ["OMNIA Nightclub", "TAO Nightclub"])

    def test_saturday_never_repeats_friday(self):
        order = self.order(["JEWEL Nightclub"], date(2026, 10, 17), prev="JEWEL Nightclub")
        self.assertNotIn("JEWEL Nightclub", order)
        self.assertEqual(order[0], "Hakkasan Nightclub")


class FlowTests(unittest.TestCase):
    def test_dry_run_plans_each_night(self):
        result = main.process_one_request(parsed(), dry_run=True, today=TODAY)
        self.assertEqual([(r["date"], r["venue"]) for r in result["registrations"]],
                         [("2026-10-30", "OMNIA Nightclub"),   # Friday: requested
                          ("2026-10-31", "JEWEL Nightclub")])  # Saturday: never repeat Friday
        self.assertIn("Hi Jane", result["confirmation_email"]["body"])

    def test_past_nights_are_skipped(self):
        result = main.process_one_request(parsed(), dry_run=True, today=date(2026, 10, 31))
        self.assertEqual([r["date"] for r in result["registrations"]], ["2026-10-31"])


class FakeGmail:
    """Stands in for the Gmail API: records sends and label changes."""

    def __init__(self, body, sent_ids=()):
        self.body, self.sent, self.labels, self.sent_ids = body, [], [], set(sent_ids)


def fake_module(fake):
    def get_plain_text_body(service, message_id):
        return {}, fake.body

    def send_once(service, message_id, to, subject, body, sender, cc=None):
        if message_id in fake.sent_ids:
            return False
        fake.sent_ids.add(message_id)
        fake.sent.append({"id": message_id, "to": to, "cc": cc, "subject": subject})
        return True

    def set_labels(service, message_id, label_ids, add=(), remove=()):
        fake.labels.append((list(add), list(remove)))

    return {"get_plain_text_body": get_plain_text_body, "send_once": send_once, "set_labels": set_labels}


class LiveFlowTests(unittest.TestCase):
    LABELS = {name: name for name in (gmail_client.PROCESSING_LABEL, gmail_client.PROCESSED_LABEL,
                                      gmail_client.EXCEPTION_LABEL)}

    def run_handle(self, fake, verified=True, today=TODAY):
        listing = {"event": "OMNIA Night", "listing_type": "Passes", "arrival_text": "Before 1 AM"}
        with mock.patch.multiple(gmail_client, **fake_module(fake)), \
             mock.patch.object(main.tao_portal, "check_availability", return_value=listing), \
             mock.patch.object(main.tao_portal, "submit_registration",
                               return_value={"confirmation_id": "ORD-1" if verified else None, "verified": verified}), \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value.date.return_value = today
            return main.handle_message(None, "msg-1", dry_run=False, labels=self.LABELS)

    def test_confirmed_request_emails_guest_with_team_cc_and_no_alert(self):
        fake = FakeGmail(FIXTURE)
        outcome = self.run_handle(fake)
        self.assertEqual(outcome, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["to"], "jane.sample@example.com")
        self.assertEqual(fake.sent[0]["cc"], "team@playmakerentertainment.com")

    def test_confirmation_is_not_resent_after_a_restart(self):
        fake = FakeGmail(FIXTURE, sent_ids={"amy-confirm-msg-1@playmakerentertainment.com"})
        self.run_handle(fake)
        self.assertEqual(fake.sent, [])

    def test_unverified_booking_alerts_team_and_sends_no_confirmation(self):
        fake = FakeGmail(FIXTURE)
        outcome = self.run_handle(fake, verified=False)
        self.assertEqual(outcome, gmail_client.EXCEPTION_LABEL)
        self.assertEqual([s["to"] for s in fake.sent], ["team@playmakerentertainment.com"])

    def test_missing_consent_alerts_team_only(self):
        fake = FakeGmail(FIXTURE.replace("Authorization accepted: YES", "Authorization accepted:"))
        outcome = self.run_handle(fake)
        self.assertEqual(outcome, gmail_client.EXCEPTION_LABEL)
        self.assertEqual([s["to"] for s in fake.sent], ["team@playmakerentertainment.com"])


class GmailHelperTests(unittest.TestCase):
    def test_message_has_brand_sender_cc_and_fixed_id(self):
        raw = gmail_client.build_message("a@example.com", "Hi — there", "Body", "valeconsultingaz@gmail.com",
                                         cc="team@playmakerentertainment.com", message_id="amy-1@x")["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(msg["From"], "Playmaker Entertainment <valeconsultingaz@gmail.com>")
        self.assertEqual(msg["Cc"], "team@playmakerentertainment.com")
        self.assertEqual(msg["Message-ID"], "<amy-1@x>")

    def test_query_skips_requests_before_go_live(self):
        self.assertTrue(gmail_client.build_query(1790600000).endswith("after:1790600000"))
        self.assertNotIn("is:unread", gmail_client.SEARCH_QUERY)


if __name__ == "__main__":
    unittest.main()

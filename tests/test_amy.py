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


def nightclubs_only(listing):
    """Availability stub: every nightclub has a free Pass, no dayclub does."""
    from config import rules
    return lambda venue, day: listing if venue in rules.NIGHTCLUBS else None


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
        nightclubs = [r for r in result["registrations"] if r["category"] == "nightclub"]
        self.assertEqual([(r["date"], r["venue"]) for r in nightclubs],
                         [("2026-10-30", "OMNIA Nightclub"),   # Friday: requested
                          ("2026-10-31", "JEWEL Nightclub")])  # Saturday: never repeat Friday
        self.assertIn("Hi Jane", result["confirmation_email"]["body"])

    def test_past_nights_are_skipped(self):
        result = main.process_one_request(parsed(), dry_run=True, today=date(2026, 10, 31))
        self.assertEqual({r["date"] for r in result["registrations"]}, {"2026-10-31"})


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
             mock.patch.object(main.tao_portal, "check_availability", side_effect=nightclubs_only(listing)), \
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


class TaoSafetyTests(unittest.TestCase):
    def test_only_https_guest_list_links_on_tao(self):
        import tao_portal as t
        self.assertTrue(t.is_safe_pass_url("https://tickets.taogroup.com/e/omnia-nc-guest-list/10-6-2026/tickets"))
        self.assertFalse(t.is_safe_pass_url("https://tickets.taogroup.com/e/omnia-nc-tickets/10-6-2026/tickets"))
        self.assertFalse(t.is_safe_pass_url("http://tickets.taogroup.com/e/omnia-guest-list/10-6-2026"))
        self.assertFalse(t.is_safe_pass_url("https://evil.example.com/guest-list"))

    def test_venue_and_date_from_link(self):
        import tao_portal as t
        url = "https://tickets.taogroup.com/e/omnia-nc-guest-list/10-6-2026/tickets"
        self.assertEqual(t.venue_from_url(url), "OMNIA Nightclub")
        self.assertEqual(t.date_from_url(url), date(2026, 10, 6))

    def test_any_positive_price_blocks(self):
        import tao_portal as t
        self.assertTrue(t.has_positive_price("Female GA $0.00 Male GA $20.00"))
        self.assertFalse(t.has_positive_price("Female GA $0.00 FREE"))
        self.assertTrue(t.has_free_evidence("Total: $0.00"))
        self.assertFalse(t.has_free_evidence("Total: $25.00"))

    def test_gender_and_capacity(self):
        import tao_portal as t
        self.assertEqual(t.gender_of("Ladies Guest List - FREE"), "female")
        self.assertEqual(t.gender_of("Men Guest List - FREE"), "male")
        self.assertEqual(t.max_quantity([{"value": "0"}, {"value": "1"}, {"value": "10"}]), 10)

    def test_authorization_is_recorded(self):
        from config import rules
        self.assertIn("Jonathan Sidara", rules.TAO_AUTOMATION_AUTHORIZATION)
        self.assertIn("utm_id=68d79ff587c84397b19f00330a1e6107", rules.TAO_PROMOTER_URL)


class UncertainSubmissionTests(unittest.TestCase):
    def test_uncertain_night_is_flagged_and_other_nights_continue(self):
        import tao_portal as t
        listing = {"event": "E", "listing_type": "Passes", "listing_url": "https://tickets.taogroup.com/e/guest-list/x"}
        calls = []

        def submit(listing, guest):
            calls.append(1)
            if len(calls) == 1:
                raise t.SubmissionUncertain("no order ID")
            return {"confirmation_id": "ORD-2", "verified": True}

        with mock.patch.object(t, "check_availability", side_effect=nightclubs_only(listing)), \
             mock.patch.object(t, "submit_registration", side_effect=submit):
            result = main.process_one_request(parsed(), dry_run=False, today=TODAY)
        self.assertEqual(len(calls), 2)  # never retried, next night still processed
        self.assertEqual([r["confirmation_id"] for r in result["registrations"]], ["ORD-2"])
        self.assertIn("Check TAO", result["exceptions"][0]["issue"])


class DayclubTests(unittest.TestCase):
    """Matches the owner's example confirmation for Oct 30-31."""

    def run_live(self, live_venues):
        booked = []

        def available(venue, day):
            if venue in live_venues:
                return {"event": f"{venue} event", "listing_type": "Passes",
                        "listing_url": "https://tickets.taogroup.com/e/guest-list/x"}
            return None

        def submit(listing, guest):
            booked.append(listing["event"])
            return {"confirmation_id": f"ORD-{len(booked)}", "verified": True}

        with mock.patch.object(main.tao_portal, "check_availability", side_effect=available), \
             mock.patch.object(main.tao_portal, "submit_registration", side_effect=submit):
            return main.process_one_request(parsed(), dry_run=False, today=TODAY)

    def test_live_dayclub_is_added_before_the_nightclub_each_day(self):
        result = self.run_live({"Marquee Dayclub", "OMNIA Nightclub", "JEWEL Nightclub"})
        self.assertEqual([(r["date"], r["venue"]) for r in result["registrations"]], [
            ("2026-10-30", "Marquee Dayclub"), ("2026-10-30", "OMNIA Nightclub"),
            ("2026-10-31", "Marquee Dayclub"), ("2026-10-31", "JEWEL Nightclub"),
        ])
        self.assertEqual(result["exceptions"], [])

    def test_dayclub_priority_and_no_problem_when_none_is_live(self):
        self.assertEqual(self.run_live({"Liquid Pool Lounge", "TAO Beach Dayclub", "OMNIA Nightclub",
                                        "JEWEL Nightclub"})["registrations"][0]["venue"], "TAO Beach Dayclub")
        result = self.run_live({"OMNIA Nightclub", "JEWEL Nightclub"})
        self.assertEqual({r["category"] for r in result["registrations"]}, {"nightclub"})
        self.assertEqual(result["exceptions"], [])

    def test_requested_dayclub_is_tried_first(self):
        from rules_engine import dayclub_candidates
        self.assertEqual(dayclub_candidates({"requested_venues": ["Liquid Pool Lounge", "OMNIA Nightclub"]})[0],
                         "Liquid Pool Lounge")


class ConfirmationEmailTests(unittest.TestCase):
    def test_owner_format(self):
        from templates.emails import consolidated_confirmation
        regs = [dict(date="2026-10-30", event_time="10:30 PM", venue="OMNIA Nightclub",
                     event="Tiësto – Halloween Weekend", confirmation_id="ORD-1", female_count=2, male_count=1),
                dict(date="2026-10-31", event_time=None, venue="JEWEL Nightclub", event="",
                     confirmation_id="ORD-2", female_count=2, male_count=1)]
        subject, body = consolidated_confirmation("Tom", "tom@example.com", "2026-10-30 to 2026-10-31", regs)
        self.assertIn("confirmed for 2 female guests and 1 male guest:", body)
        self.assertIn("Friday, October 30 at 10:30 PM — OMNIA Nightclub: Tiësto – Halloween Weekend\nOrder ID: ORD-1", body)
        self.assertIn("Saturday, October 31 — JEWEL Nightclub\nOrder ID: ORD-2", body)
        self.assertIn("same email address used for the guest list: tom@example.com", body)
        self.assertTrue(body.endswith("Enjoy Las Vegas!\n\nPlaymaker Entertainment"))

    def test_party_wording(self):
        from templates.emails import party_phrase
        self.assertEqual(party_phrase(1, 0), "1 female guest")
        self.assertEqual(party_phrase(0, 3), "3 male guests")

import base64
import os
import email
import pathlib
import re
import unittest
from datetime import date
from unittest import mock

import gmail_client
import main
import tao_portal
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

    def test_windows_line_endings(self):
        # Real notifications arrive with CRLF line endings.
        raw = parsed(FIXTURE.replace("\n", "\r\n"))
        self.assertEqual((raw["start_date"], raw["end_date"]), ("2026-10-30", "2026-10-31"))
        self.assertEqual(raw["email"], "jane.sample@example.com")
        self.assertEqual(normalize_guest_request(raw)["female_count"], 2)

    def test_header_email_is_not_used_as_form_data(self):
        text = FIXTURE.replace("Email: jane.sample@example.com\n\nSubmitted", "Email: header@example.com\n\nSubmitted")
        self.assertEqual(parsed(text)["email"], "jane.sample@example.com")

    def test_email_run_together_with_phone(self):
        # Seen Oct 3 2026: "Email: mailto:guest@icloud.com Phone: 7757221489"
        text = FIXTURE.replace("Email: jane.sample@example.com\nPhone: 6025550100\n",
                               "Email: mailto:jane.sample@example.com Phone: 6025550100\n")
        raw = parsed(text)
        self.assertEqual(raw["email"], "jane.sample@example.com")
        self.assertEqual(raw["phone"], "6025550100")

    def test_garbage_email_counts_as_missing(self):
        raw = parsed(FIXTURE.replace("Email: jane.sample@example.com\nPhone", "Email: not an address\nPhone"))
        self.assertIn("email", raw["_missing_required"])

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

    def send_once(service, message_id, to, subject, body, sender, cc=None, dedupe_query=None):
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


class LiveCatalogTests(unittest.TestCase):
    """Link and card formats copied from the live promoter page (2026-09-28)."""

    def test_builds_catalog_from_pass_links_and_cards(self):
        import tao_portal as t
        q = "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"
        links = [
            {"text": "Passes", "href": "https://tickets.taogroup.com/e/guest-list-omnia-las-vegas-10-27-2026/tickets" + q,
             "card": "Guest List - Alesso Tuesday, Oct 27, 2026 at 10:30 PM to Wednesday, Oct 28, 2026 "
                     "OMNIA Nightclub, Las Vegas, NV More details Passes"},
            {"text": "Passes", "href": "https://tickets.taogroup.com/e/guest-list-marquee-dc-las-vegas-10-30-2026/tickets" + q,
             "card": "Guest List - Dawn 2 Dusk – NOTD – Halloween Weekend Friday, Oct 30, 2026 at 11:00 AM "
                     "Marquee Dayclub, Las Vegas, NV More details Passes"},
            {"text": "Tickets", "href": "https://tickets.taogroup.com/e/tiesto-omnia-10-30-2026/tickets" + q, "card": ""},
        ]
        catalog = t.build_catalog(links)
        self.assertEqual(set(catalog), {("OMNIA Nightclub", date(2026, 10, 27)), ("Marquee Dayclub", date(2026, 10, 30))})
        omnia = catalog[("OMNIA Nightclub", date(2026, 10, 27))]
        self.assertEqual((omnia["event"], omnia["event_time"]), ("Alesso", "10:30 PM"))
        self.assertTrue(omnia["url"].endswith(q))  # promoter credit kept
        self.assertEqual(catalog[("Marquee Dayclub", date(2026, 10, 30))]["event"],
                         "Dawn 2 Dusk – NOTD – Halloween Weekend")

    def test_live_date_format(self):
        import tao_portal as t
        self.assertEqual(t.date_from_url("https://tickets.taogroup.com/e/guest-list-omnia-nc-9-29-2026/tickets?x=1"),
                         date(2026, 9, 29))


class TestModeTests(unittest.TestCase):
    """Test mode books only for allowlisted addresses; everyone else's
    request is left completely untouched (no labels, no email)."""

    LABELS = LiveFlowTests.LABELS

    def run_mode(self, body, allowlist):
        fake = FakeGmail(body)
        listing = {"event": "OMNIA Night", "listing_type": "Passes", "listing_url": "https://tickets.taogroup.com/e/guest-list/x"}
        with mock.patch.multiple(gmail_client, **fake_module(fake)), \
             mock.patch.object(main.tao_portal, "check_availability", side_effect=nightclubs_only(listing)), \
             mock.patch.object(main.tao_portal, "submit_registration",
                               return_value={"confirmation_id": "ORD-1", "verified": True}) as submit, \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value.date.return_value = TODAY
            outcome = main.handle_message(None, "msg-1", dry_run=False, labels=self.LABELS, allowlist=allowlist)
        return outcome, fake, submit

    def test_other_customers_are_untouched(self):
        outcome, fake, submit = self.run_mode(FIXTURE, {"valeconsultingaz@gmail.com"})
        self.assertIsNone(outcome)
        self.assertEqual((fake.labels, fake.sent), ([], []))
        submit.assert_not_called()

    def test_allowlisted_request_is_booked_and_confirmed(self):
        body = FIXTURE.replace("jane.sample@example.com", "valeconsultingaz@gmail.com")
        outcome, fake, submit = self.run_mode(body, {"valeconsultingaz@gmail.com"})
        self.assertEqual(outcome, gmail_client.PROCESSED_LABEL)
        self.assertTrue(submit.called)
        self.assertEqual(fake.sent[0]["to"], "valeconsultingaz@gmail.com")


class OrderIdTests(unittest.TestCase):
    """TAO's real order page: https://tickets.taogroup.com/orders/confirmation/<uuid>."""

    def test_from_confirmation_url(self):
        import tao_portal as t
        url = "https://tickets.taogroup.com/orders/confirmation/6aba738b-b670-47ec-b34b-54e80a1e60a9"
        self.assertEqual(t.order_id_from(url, "Thank you for your order!"), "6aba738b-b670-47ec-b34b-54e80a1e60a9")

    def test_from_page_text(self):
        import tao_portal as t
        self.assertEqual(t.order_id_from("https://tickets.taogroup.com/x",
                                         "Order Details\nOrder ID\n6aba738b-b670-47ec-b34b-54e80a1e60a9\nView"),
                         "6aba738b-b670-47ec-b34b-54e80a1e60a9")
        self.assertEqual(t.order_id_from("", "Order Number: TD-778812"), "TD-778812")

    def test_no_id_found(self):
        import tao_portal as t
        self.assertIsNone(t.order_id_from("https://tickets.taogroup.com/e/x/tickets", "Something went wrong"))


class StartedEventTests(unittest.TestCase):
    """Same-day requests: a dayclub that has already started is skipped."""

    def run_at(self, hour, minute):
        from datetime import datetime
        now = datetime(2026, 10, 30, hour, minute, tzinfo=main.VEGAS)
        booked = []

        def available(venue, day):
            times = {"TAO Beach Dayclub": "10:00 AM", "Marquee Dayclub": "11:00 AM",
                     "OMNIA Nightclub": "10:30 PM", "JEWEL Nightclub": "10:30 PM"}
            if venue in times:
                return {"event": venue, "event_time": times[venue], "listing_type": "Passes",
                        "listing_url": "https://tickets.taogroup.com/e/guest-list/x"}
            return None

        def submit(listing, guest):
            booked.append(listing["event"])
            return {"confirmation_id": f"ORD-{len(booked)}", "verified": True}

        raw = parsed(FIXTURE.replace("2026-10-31", "2026-10-30"))
        with mock.patch.object(main.tao_portal, "check_availability", side_effect=available), \
             mock.patch.object(main.tao_portal, "submit_registration", side_effect=submit):
            main.process_one_request(raw, dry_run=False, now=now)
        return booked

    def test_before_the_dayclub_starts_it_is_booked(self):
        self.assertEqual(self.run_at(9, 0), ["TAO Beach Dayclub", "OMNIA Nightclub"])

    def test_next_dayclub_is_tried_when_the_first_has_started(self):
        self.assertEqual(self.run_at(10, 30), ["Marquee Dayclub", "OMNIA Nightclub"])

    def test_after_all_dayclubs_started_only_the_nightclub(self):
        self.assertEqual(self.run_at(15, 0), ["OMNIA Nightclub"])

    def test_nightclub_is_still_booked_after_its_start(self):
        self.assertEqual(self.run_at(23, 0), ["OMNIA Nightclub"])

    def test_time_parsing(self):
        from datetime import datetime, date
        now = datetime(2026, 10, 30, 11, 0, tzinfo=main.VEGAS)
        self.assertTrue(main.event_has_started({"event_time": "11:00 AM"}, date(2026, 10, 30), now))
        self.assertFalse(main.event_has_started({"event_time": "11:30AM"}, date(2026, 10, 30), now))
        self.assertFalse(main.event_has_started({"event_time": "9:00 AM"}, date(2026, 10, 31), now))
        self.assertFalse(main.event_has_started({"event_time": None}, date(2026, 10, 30), now))


POSH = (pathlib.Path(__file__).parent / "fixtures" / "posh_signup.txt").read_text()


class PoshParseTests(unittest.TestCase):
    """Layout copied from the Zapier "NEW POSH SIGNUP" test email (details replaced)."""

    def test_recognised(self):
        import posh
        self.assertTrue(posh.is_posh_signup("NEW POSH SIGNUP", ""))
        self.assertTrue(posh.is_posh_signup("", POSH))
        self.assertFalse(posh.is_posh_signup("New contact form message", FIXTURE))

    def test_fields_club_night_and_gender(self):
        import posh
        raw = posh.parse_signup("m1", POSH)
        self.assertEqual((raw["first_name"], raw["last_name"], raw["email"]),
                         ("Jane", "Sample", "jane.sample@example.com"))
        self.assertEqual(raw["phone"], "+16025550100")
        # 05:30 UTC Wed = 10:30 PM Tuesday night in Las Vegas.
        self.assertEqual((raw["start_date"], raw["end_date"]), ("2026-10-06", "2026-10-06"))
        self.assertEqual(raw["venues"], ["OMNIA Nightclub"])
        self.assertEqual((raw["female_count"], raw["male_count"]), ("1", "0"))
        self.assertEqual(raw["posh"]["order_number"], "1001")
        self.assertEqual(raw["posh"]["promo_code"], "")
        self.assertNotIn("_missing_required", raw)

    def test_night_rollover_and_daytime(self):
        import posh
        from datetime import date
        # Posh sends local wall-clock time labelled "Z" (real order: 10:30 PM show -> T22:30:00.000Z).
        self.assertEqual(posh.night_of("2026-08-03T22:30:00.000Z"), date(2026, 8, 3))
        self.assertEqual(posh.night_of("2026-10-07T01:00:00.000Z"), date(2026, 10, 6))  # 1 AM -> previous night
        self.assertEqual(posh.night_of("2026-10-07T11:00:00.000Z"), date(2026, 10, 7))  # 11 AM dayclub
        self.assertIsNone(posh.night_of("not a date"))

    def test_venue_detection(self):
        import posh
        self.assertEqual(posh.venue_from_event("Dawn 2 Dusk at Marquee Dayclub"), "Marquee Dayclub")
        self.assertEqual(posh.venue_from_event("JEWEL Saturdays"), "JEWEL Nightclub")
        self.assertIsNone(posh.venue_from_event("Playmaker Halloween Party"))

    def test_gender_from_ticket(self):
        import posh
        self.assertEqual(posh.gender_from_ticket("Men's Guest List"), "male")
        self.assertEqual(posh.gender_from_ticket("Female - Free Entry"), "female")
        self.assertIsNone(posh.gender_from_ticket("General Admission"))

    def test_unrendered_zapier_placeholders_are_empty(self):
        import posh
        raw = posh.parse_signup("m1", POSH.replace("jane.sample@example.com",
                                                   '{{=gives["381629653"]["account_email"]}}'))
        self.assertIn("email", raw["_missing_required"])


class PoshFlowTests(unittest.TestCase):
    LABELS = LiveFlowTests.LABELS

    def run_posh(self, body, consent, already_handled=False):
        fake = FakeGmail(body)
        listing = {"event": "OMNIA Night", "event_time": "10:30 PM", "listing_type": "Passes",
                   "listing_url": "https://tickets.taogroup.com/e/guest-list/x"}
        with mock.patch.dict("os.environ", {"POSH_CONSENT_ON_FILE": "true" if consent else ""}), \
             mock.patch.multiple(gmail_client, **fake_module(fake)), \
             mock.patch.object(gmail_client, "posh_order_already_handled", return_value=already_handled), \
             mock.patch.object(main.tao_portal, "check_availability", side_effect=nightclubs_only(listing)), \
             mock.patch.object(main.tao_portal, "submit_registration",
                               return_value={"confirmation_id": "ORD-1", "verified": True}) as submit, \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value.date.return_value = TODAY
            outcome = main.handle_message(None, "msg-1", dry_run=False, labels=self.LABELS)
        return outcome, fake, submit

    def test_without_consent_setting_team_gets_the_plan_and_nothing_is_booked(self):
        outcome, fake, submit = self.run_posh(POSH, consent=False)
        self.assertEqual(outcome, gmail_client.EXCEPTION_LABEL)
        submit.assert_not_called()
        self.assertEqual([s["to"] for s in fake.sent], ["team@playmakerentertainment.com"])

    def test_with_consent_setting_it_is_booked_and_confirmed(self):
        outcome, fake, submit = self.run_posh(POSH, consent=True)
        self.assertEqual(outcome, gmail_client.PROCESSED_LABEL)
        self.assertTrue(submit.called)
        self.assertEqual(fake.sent[0]["to"], "jane.sample@example.com")
        self.assertEqual(fake.sent[0]["cc"], "team@playmakerentertainment.com")

    def test_same_posh_order_is_never_booked_twice(self):
        outcome, fake, submit = self.run_posh(POSH, consent=True, already_handled=True)
        self.assertEqual(outcome, gmail_client.PROCESSED_LABEL)
        submit.assert_not_called()
        self.assertEqual(fake.sent, [])

    def test_unknown_gender_goes_to_team(self):
        outcome, fake, submit = self.run_posh(POSH.replace("Ladies Guest List", "General Admission"), consent=True)
        self.assertEqual(outcome, gmail_client.EXCEPTION_LABEL)
        submit.assert_not_called()


class PoshSummaryTests(unittest.TestCase):
    def test_team_email_includes_order_and_plan(self):
        import posh
        text = main.posh_summary(posh.parse_signup("m1", POSH))
        self.assertIn("Event: Playmaker Tuesdays at OMNIA", text)
        self.assertIn("Order number: 1001", text)
        self.assertIn("Amy's nightclub plan for Tue Oct 06: OMNIA Nightclub", text)


class PoshSubjectOnlyTests(unittest.TestCase):
    def test_body_without_header_lines_is_recognised_by_subject(self):
        import posh
        body = "Name:TestUser\nEmail: t@example.com\nEvent:JEWEL Fridays\nEvent Date: 2026-10-10T05:30:00Z\nTicket: Men\n"
        msg = {"payload": {"headers": [{"name": "Subject", "value": "NEW POSH SIGNUP"}]}}
        self.assertTrue(posh.is_posh_signup(gmail_client.get_subject(msg), body))
        raw = posh.parse_signup("m", body)
        self.assertEqual((raw["start_date"], raw["venues"], raw["male_count"]), ("2026-10-09", ["JEWEL Nightclub"], "1"))


class PoshOrderLookupTests(unittest.TestCase):
    """posh_order_already_handled against a fake Gmail API."""

    def service(self, messages):
        class Api:
            def __init__(self, msgs): self.msgs, self.q = msgs, None
            def users(self): return self
            def messages(self): return self
            def list(self, userId, q, maxResults):
                self.q = q
                return self
            def execute(self): return {"messages": [{"id": i} for i in self.msgs]}
        return Api(messages)

    def lookup(self, others, order="1001"):
        api = self.service(list(others))
        bodies = dict(others)
        with mock.patch.object(gmail_client, "get_plain_text_body", lambda s, mid: ({}, bodies.get(mid, ""))):
            return gmail_client.posh_order_already_handled(api, order, "current"), api.q

    def test_found_when_another_handled_email_has_the_same_order(self):
        found, query = self.lookup({"old": POSH})
        self.assertTrue(found)
        self.assertIn('"1001"', query)
        self.assertIn("label:Amy/Processed", query)

    def test_not_found_for_a_different_order_or_itself(self):
        self.assertFalse(self.lookup({"old": POSH.replace("1001", "2002")})[0])
        self.assertFalse(self.lookup({"current": POSH})[0])

    def test_no_order_number_never_counts_as_duplicate(self):
        self.assertFalse(self.lookup({"old": POSH}, order="")[0])


REAL_POSH_TICKETS = "Guest List - Female - Free Before 1AM,Guest List - Male - Free Before 1AM"


class RealPoshOrderTests(unittest.TestCase):
    """From the first real Posh order (Sept 28 2026), details replaced."""

    def body(self, event_start="2026-10-05T22:30:00.000Z", tickets=REAL_POSH_TICKETS):
        return (POSH.replace("Playmaker Tuesdays at OMNIA", "Guest List | Marquee Night Club")
                    .replace("2026-10-07T05:30:00.000Z", event_start)
                    .replace("Ladies Guest List", tickets))

    def test_each_ticket_in_the_order_is_counted(self):
        import posh
        raw = posh.parse_signup("m", self.body())
        self.assertEqual((raw["female_count"], raw["male_count"]), ("1", "1"))
        self.assertNotIn("_action_needed", raw)
        self.assertEqual(raw["venues"], ["Marquee Nightclub"])
        self.assertEqual(raw["start_date"], "2026-10-05")

    def test_two_of_the_same_ticket(self):
        import posh
        self.assertEqual(posh.ticket_counts("Guest List - Female,Guest List - Female"), (2, 0, []))

    def test_an_unknown_ticket_goes_to_team(self):
        import posh
        raw = posh.parse_signup("m", self.body(tickets="Guest List - Female,VIP Table"))
        self.assertIn('"VIP Table"', raw["_action_needed"][0])

    def test_past_event_date_is_sent_to_team_not_silently_skipped(self):
        import posh
        raw = posh.parse_signup("m", self.body(event_start="2026-08-03T22:30:00.000Z"))
        with mock.patch.dict("os.environ", {"POSH_CONSENT_ON_FILE": "true"}):
            raw = posh.parse_signup("m", self.body(event_start="2026-08-03T22:30:00.000Z"))
        result = main.process_one_request(raw, dry_run=True, today=TODAY)
        self.assertEqual(result["status"], "action_needed")
        self.assertIn("recurring Posh event", result["issue"])

    def test_website_request_for_past_dates_also_goes_to_team(self):
        raw = parsed(FIXTURE.replace("2026-10-30", "2026-09-01").replace("2026-10-31", "2026-09-02"))
        result = main.process_one_request(raw, dry_run=True, today=TODAY)
        self.assertEqual(result["status"], "action_needed")


class ChallengeRetryTests(unittest.TestCase):
    """TAO's security check is intermittent; Amy waits and reloads, never fakes it."""

    class FakePage:
        def __init__(self, titles):
            self.titles_seq = list(titles)
            self.i = -1
            self.goto_calls = self.reload_calls = self.waits = 0
        def _advance(self):
            self.i = min(self.i + 1, len(self.titles_seq) - 1)
        def goto(self, url, **k): self.goto_calls += 1; self._advance()
        def reload(self, **k): self.reload_calls += 1; self._advance()
        def wait_for_timeout(self, ms): self.waits += ms
        def wait_for_load_state(self, *a, **k): pass
        def title(self): return self.titles_seq[self.i]
        class _Body:
            def inner_text(self): return ""
        def locator(self, *a): return self._Body()

    def setUp(self):
        self._settle = tao_portal._settle
        tao_portal._settle = lambda page: None
        self.addCleanup(setattr, tao_portal, "_settle", self._settle)

    def test_clears_after_a_couple_reloads(self):
        page = self.FakePage(["Just a moment...", "Just a moment...", "TAO Group Hospitality"])
        with mock.patch.dict(os.environ, {"TAO_CHALLENGE_WAIT_SECONDS": "3", "TAO_CHALLENGE_RETRIES": "4"}):
            tao_portal._open_promoter_page(page)  # does not raise
        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.reload_calls, 2)

    def test_gives_up_and_reports_blocked(self):
        page = self.FakePage(["Just a moment..."])
        with mock.patch.dict(os.environ, {"TAO_CHALLENGE_WAIT_SECONDS": "3", "TAO_CHALLENGE_RETRIES": "3"}):
            with self.assertRaises(tao_portal.TaoBlocked):
                tao_portal._open_promoter_page(page)


class BrowserProfileTests(unittest.TestCase):
    """AMY_BROWSER_PROFILE makes Amy reuse one profile so a cleared check sticks."""

    class FakeChromium:
        def __init__(self): self.persistent = self.ephemeral = None
        def launch_persistent_context(self, profile, **k):
            self.persistent = (profile, k)
            return mock.MagicMock(pages=[mock.MagicMock()])
        def launch(self, **k):
            self.ephemeral = k
            return mock.MagicMock()

    def _run(self, env):
        pw = mock.MagicMock(); pw.chromium = self.FakeChromium()
        with mock.patch.dict(os.environ, env, clear=True):
            tao_portal._browser_page(pw)
        return pw.chromium

    def test_persistent_when_profile_set(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        chromium = self._run({"AMY_BROWSER_PROFILE": d})
        self.assertIsNotNone(chromium.persistent)
        self.assertEqual(chromium.persistent[0], d)
        self.assertIsNone(chromium.ephemeral)

    def test_ephemeral_when_no_profile(self):
        chromium = self._run({})
        self.assertIsNone(chromium.persistent)
        self.assertIsNotNone(chromium.ephemeral)


class AccessHeaderTests(unittest.TestCase):
    """TAO's chosen method is allowlisting; Amy can send a token they allowlist."""

    def test_no_header_without_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tao_portal.access_headers(), {})

    def test_header_sent_when_token_set(self):
        with mock.patch.dict(os.environ, {"TAO_ACCESS_TOKEN": "secret123"}):
            self.assertEqual(tao_portal.access_headers(), {"X-Playmaker-Access": "secret123"})


class TaoBlockedTests(unittest.TestCase):
    """Oct 5 2026: TAO's site started showing a Cloudflare check to Amy."""

    def test_security_page_is_recognized(self):
        self.assertTrue(tao_portal.is_security_check(
            "Just a moment...", "Performing security verification\nThis website uses a security service"))
        self.assertFalse(tao_portal.is_security_check("TAO Group Hospitality", "Marquee Nightclub Passes"))

    def test_blocked_says_so_instead_of_no_guest_list(self):
        raw = parsed()
        with mock.patch.object(tao_portal, "check_availability",
                               side_effect=tao_portal.TaoBlocked("TAO's website showed a security check")), \
             mock.patch.object(tao_portal, "submit_registration") as submit:
            result = main.process_one_request(raw, dry_run=False, today=TODAY)
        self.assertEqual(result["status"], "action_needed")
        self.assertIn("security check", result["issue"])
        self.assertIn("Nothing was submitted", result["issue"])
        submit.assert_not_called()


class DraisTests(unittest.TestCase):
    """Drai's After Hours has no portal: the confirmation is the guest's pass."""

    def run_request(self, venues):
        raw = parsed(FIXTURE.replace("Requested venues: Omnia Nightclub", f"Requested venues: {venues}"))
        return main.process_one_request(raw, dry_run=True, today=TODAY)

    def test_drais_text_added_and_nightclub_still_booked(self):
        result = self.run_request("Drai's Nightclub, Hakkasan Nightclub")
        body = result["confirmation_email"]["body"]
        self.assertTrue(result["registrations"])
        self.assertNotIn("Drai's", [r["venue"] for r in result["registrations"]])
        self.assertIn("Order ID:", body)
        self.assertIn("show this email at the door", body)
        self.assertIn("Playmaker Entertainment’s\nGUESTLIST", body)
        self.assertIn("Name: Jane Sample\nParty: 2 female guests and 1 male guest", body)
        self.assertIn("Nights: Friday, October 30, Saturday, October 31", body)
        self.assertIn("Drai’s After Hours @ Vanderpump Hotel, opens at 1 AM", body)
        self.assertIn("@playmaker.entertainment", body)
        self.assertFalse(result.get("action_needed_records"))

    def test_no_drais_text_unless_requested(self):
        body = self.run_request("Hakkasan Nightclub")["confirmation_email"]["body"]
        self.assertNotIn("Drai", body)

    def test_drais_only_email_when_no_tao_booking(self):
        from templates.emails import consolidated_confirmation
        _, body = consolidated_confirmation("Jane", "j@x.com", "2026-10-30", [], drais={
            "name": "Jane Sample", "female_count": 2, "male_count": 0, "nights": ["2026-10-30"]})
        self.assertNotIn("TAO", body)
        self.assertIn("Night: Friday, October 30", body)
        self.assertIn("21+", body)


class FollowUpTests(unittest.TestCase):
    """One "see you next time" email, 7 days after the guest's last night."""

    CONFS = [
        {"id": "c1", "to": "Mia <mia@example.com>", "first_name": "Mia",
         "subject": "Playmaker Guest List Confirmation — 2026-09-28", "venues": ["TAO"]},
        {"id": "c2", "to": "tom@example.com", "first_name": "Tom",
         "subject": "Playmaker Guest List Confirmation — 2026-09-28 to 2026-10-02"},
    ]

    def run_on(self, day, mode="live", allowlist=None, opted_out=(), sent_ids=()):
        from datetime import date
        sent, done = [], set(sent_ids)

        def send_once(service, message_id, to, subject, body, sender, cc=None, dedupe_query=None):
            if message_id in done:
                return False
            done.add(message_id)
            sent.append((to, subject, body))
            return True

        with mock.patch.object(gmail_client, "recent_confirmations", return_value=self.CONFS), \
             mock.patch.object(gmail_client, "has_opted_out", side_effect=lambda s, a: a in opted_out), \
             mock.patch.object(gmail_client, "send_once", side_effect=send_once):
            main.send_follow_ups(None, mode, allowlist, today=date(2026, 10, day))
        return sent

    def test_sent_7_days_after_the_last_night_only(self):
        self.assertEqual(self.run_on(4), [])                              # 6 days after Sep 28
        sent = self.run_on(5)                                             # 7 days after Sep 28
        self.assertEqual([s[0] for s in sent], ["mia@example.com"])       # Tom's last night is Oct 2
        self.assertEqual([s[0] for s in self.run_on(9)], ["mia@example.com", "tom@example.com"])

    def test_wording(self):
        to, subject, body = self.run_on(5)[0]
        self.assertEqual(subject, "Hope you had fun in Vegas, Mia")
        self.assertTrue(body.startswith("Hi Mia,\n\nHope you had fun at the nightclub!"))
        self.assertIn("friends or family coming into town", body)
        self.assertIn("PlaymakerEntertainment.com", body)
        self.assertIn("@Playmaker.Entertainment", body)
        self.assertNotIn("702", body)
        self.assertIn("reply STOP", body)

    def test_mentions_dayclubs_only_when_they_went(self):
        from templates.emails import follow_up_email
        line = lambda v: follow_up_email("Mia", v)[1].split("\n\n")[1]
        self.assertEqual(line(["TAO"]), "Hope you had fun at the nightclub!")
        self.assertEqual(line(["JEWEL", "Hakkasan"]), "Hope you had fun at the nightclubs!")
        self.assertEqual(line(["Marquee", "TAO Beach Dayclub"]), "Hope you had fun at the nightclub and dayclub!")
        self.assertEqual(line(["Marquee Dayclub"]), "Hope you had fun at the dayclub!")

    def test_never_twice_and_respects_stop(self):
        self.assertEqual(self.run_on(5, sent_ids={"amy-followup-c1@playmakerentertainment.com"}), [])
        self.assertEqual(self.run_on(9, opted_out={"mia@example.com"})[0][0], "tom@example.com")

    def test_dry_run_and_test_mode(self):
        self.assertEqual(self.run_on(9, mode="dry-run"), [])
        self.assertEqual([s[0] for s in self.run_on(9, mode="test", allowlist={"tom@example.com"})],
                         ["tom@example.com"])

    def test_last_night_from_subject(self):
        from datetime import date
        self.assertEqual(main.last_night_from_subject("Playmaker Guest List Confirmation — 2026-09-28 to 2026-09-30"),
                         date(2026, 9, 30))
        self.assertIsNone(main.last_night_from_subject("Something else"))
        # Subjects from the previous process (real examples).
        self.assertEqual(main.last_night_from_subject("Your Playmaker Guest List Confirmation — September 26, 2026"),
                         date(2026, 9, 26))
        self.assertEqual(main.last_night_from_subject("Playmaker Guest List Confirmation — September 25–29, 2026"),
                         date(2026, 9, 29))
        self.assertEqual(main.last_night_from_subject("Guest List Confirmation — September 30–October 2, 2026"),
                         date(2026, 10, 2))
        self.assertIsNone(main.last_night_from_subject("Playmaker Guest List Confirmation"))


class FakeSentFolder:
    """Mimics the Gmail API as it really behaves: our Message-ID header is
    NOT kept, so only visible content (recipient, subject, body) can be
    searched. This is what let the follow-up repeat every hour."""

    def __init__(self):
        self.sent, self._q = [], None

    def users(self): return self
    def messages(self): return self

    def send(self, userId, body):
        import email as email_lib
        import email.policy
        msg = email_lib.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=email.policy.default)
        self.sent.append({"to": msg["To"], "subject": str(msg["Subject"]), "body": msg.get_content()})
        self._q = None
        return self

    def list(self, userId, q, maxResults):
        self._q = q
        return self

    def execute(self):
        if self._q is None:
            return {}
        q = self._q
        if "rfc822msgid:" in q:  # Gmail rewrote our Message-ID: never matches
            return {}
        def matches(m):
            to = re.search(r"to:(\S+)", q)
            subj = re.findall(r'subject:"([^"]+)"', q)
            phrases = re.findall(r'"([^"]+)"', re.sub(r'subject:"[^"]+"', "", q))
            return ((not to or to.group(1) in m["to"])
                    and (not subj or any(s in m["subject"] for s in subj))
                    and all(p in m["body"] for p in phrases))
        return {"messages": [{"id": str(i)} for i, m in enumerate(self.sent) if matches(m)]}


class NeverTwiceTests(unittest.TestCase):
    def test_follow_up_is_sent_once_even_though_gmail_drops_our_message_id(self):
        api = FakeSentFolder()
        from datetime import date
        confs = [{"id": "c1", "to": "mia@example.com", "first_name": "Mia",
                  "subject": "Playmaker Guest List Confirmation — 2026-09-28"}]
        with mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False):
            for _ in range(5):  # five hourly checks
                main.send_follow_ups(api, "live", today=date(2026, 10, 6))
        self.assertEqual(len(api.sent), 1)

    def test_customer_confirmation_and_team_alert_are_sent_once(self):
        api = FakeSentFolder()
        for _ in range(3):
            gmail_client.send_once(api, "x@y", "guest@example.com", "Playmaker Guest List Confirmation — 2026-10-05",
                                   "Hi", sender="valeconsultingaz@gmail.com")
            main.team_alert(api, "msg-42", {"name": "A"}, ["problem"], dry_run=False)
        self.assertEqual(len(api.sent), 2)

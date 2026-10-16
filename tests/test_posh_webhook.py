"""Posh webhook: auth, parsing, date logic, dedupe, HTTP server (no network beyond localhost)."""
import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import date
from unittest import mock

import posh
import posh_webhook as pw

BASE = {"type": "new_order", "account_first_name": "Test", "account_last_name": "User",
        "account_email": "test@example.com", "account_phone": "+15555555555",
        "event_name": "Guestlist | Marquee Night Club", "event_id": "600000000000000000000000",
        "event_start": "2026-10-10T22:30:00.000Z", "event_end": "2026-10-11T04:00:00.000Z",
        "items": [{"item_id": "a", "name": "Guest List - Female", "price": 0},
                  {"item_id": "b", "name": "Guest List - Female", "price": 0},
                  {"item_id": "c", "name": "Guest List - Male", "price": 0}],
        "date_purchased": "2026-10-08T19:00:00.000Z", "promo_code": "", "order_number": "77",
        "cancelled": False, "refunded": False, "disputed": False, "custom_fields": [],
        "isInPersonOrder": False}


NOLOOKUP = lambda *a, **k: None


def order(**kw):
    return dict(BASE, **kw)


class AuthTests(unittest.TestCase):
    def test_token_sources(self):
        with mock.patch.dict(os.environ, {"POSH_WEBHOOK_TOKEN": "s3cret"}):
            self.assertTrue(pw.token_ok("/webhooks/posh?token=s3cret", {}))
            self.assertTrue(pw.token_ok("/webhooks/posh", {"x-amy-token": "s3cret"}))
            self.assertTrue(pw.token_ok("/webhooks/posh", {"authorization": "Bearer s3cret"}))
            self.assertFalse(pw.token_ok("/webhooks/posh?token=nope", {}))
            self.assertFalse(pw.token_ok("/webhooks/posh", {}))

    def test_no_token_configured_rejects_all(self):
        with mock.patch.dict(os.environ, {"POSH_WEBHOOK_TOKEN": ""}):
            self.assertFalse(pw.token_ok("/webhooks/posh?token=", {}))

    def test_signature_header_names_only(self):
        names = pw.signature_header_names({"x-posh-signature": "abc", "content-type": "j", "x-amy-token": "t"})
        self.assertEqual(names, ["x-posh-signature"])


class NightTests(unittest.TestCase):
    def test_future_one_off_uses_event_start(self):
        self.assertEqual(pw.posh_night("2026-10-10T22:30:00.000Z", "2026-10-08T19:00:00Z"),
                         (date(2026, 10, 10), "event_start"))

    def test_after_midnight_belongs_to_previous_night(self):
        self.assertEqual(pw.posh_night("2026-10-11T00:30:00.000Z", "2026-10-08T19:00:00Z")[0], date(2026, 10, 10))

    def test_recurring_weekly_picks_next_occurrence(self):
        # Series anchored Sat 2026-07-18 10:30 PM, bought Thu 2026-10-08 -> Sat 2026-10-10.
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00.000Z", "2026-10-08T19:00:00Z"),
                         (date(2026, 10, 10), "recurring_next_occurrence"))

    def test_recurring_bought_same_day_before_start(self):
        # Bought Sat 3 PM Vegas (22:00Z) -> that night.
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00Z", "2026-10-10T22:00:00Z")[0], date(2026, 10, 10))

    def test_recurring_bought_during_event_counts_tonight(self):
        # Bought Sun 1 AM Vegas (08:00Z) while a 10:30 PM-4 AM Saturday show runs.
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00Z", "2026-10-11T08:00:00Z",
                                       "2026-07-19T04:00:00Z")[0], date(2026, 10, 10))

    def test_recurring_bought_after_event_rolls_to_next_week(self):
        # Bought Sun noon Vegas -> next Saturday.
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00Z", "2026-10-11T19:00:00Z",
                                       "2026-07-19T04:00:00Z")[0], date(2026, 10, 17))

    def test_recurring_after_midnight_series(self):
        # Drai's after-hours: Sat 1:00 AM starts (= Friday night). Bought Wed.
        self.assertEqual(pw.posh_night("2026-06-06T01:00:00Z", "2026-10-07T18:00:00Z")[0], date(2026, 10, 9))

    def test_purchase_converted_to_vegas(self):
        # 2026-10-11T03:00Z is Sat 8 PM in Vegas, before a Sat 10:30 PM start.
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00Z", "2026-10-11T03:00:00Z")[0], date(2026, 10, 10))

    def test_custom_field_date_wins(self):
        cf = [{"type": "input", "prompt": "Which night are you coming?", "answer": "10/16"}]
        self.assertEqual(pw.posh_night("2026-07-18T22:30:00Z", "2026-10-08T19:00:00Z", custom_fields=cf),
                         (date(2026, 10, 16), "custom_field"))

    def test_custom_field_without_date_prompt_ignored(self):
        cf = [{"type": "input", "prompt": "Instagram?", "answer": "10/16"}]
        self.assertEqual(pw.posh_night("2026-10-10T22:30:00Z", "2026-10-08T19:00:00Z", custom_fields=cf)[1],
                         "event_start")

    def test_utc_mode(self):
        with mock.patch.dict(os.environ, {"POSH_EVENT_START_IS_UTC": "true"}):
            # 05:30Z Oct 11 = 10:30 PM Oct 10 Vegas
            self.assertEqual(pw.posh_night("2026-10-11T05:30:00Z", "2026-10-08T19:00:00Z")[0], date(2026, 10, 10))

    def test_missing_start(self):
        self.assertEqual(pw.posh_night("", "2026-10-08T19:00:00Z"), (None, "no_event_start"))


class ParseTests(unittest.TestCase):
    def test_ignores(self):
        self.assertIsNone(pw.should_ignore(order()))
        for kw in ({"cancelled": True}, {"refunded": True}, {"isInPersonOrder": True},
                   {"type": "new_order_request"}, {"account_email": ""}):
            self.assertIsNotNone(pw.should_ignore(order(**kw)), kw)

    def test_signup_text_roundtrips_through_existing_parser(self):
        p = order(event_start="2026-07-18T22:30:00.000Z")
        night, how = pw.posh_night(p["event_start"], p["date_purchased"], p["event_end"])
        body = pw.to_signup_text(p, night, how)
        self.assertTrue(posh.is_posh_signup(pw.SUBJECT, body))
        self.assertTrue(pw.is_webhook_signup(body))
        raw = posh.parse_signup("m1", body)
        self.assertEqual(raw["start_date"], "2026-10-10")
        self.assertEqual((raw["female_count"], raw["male_count"]), ("2", "1"))
        self.assertEqual(raw["venues"], ["Marquee Nightclub"])
        self.assertEqual(raw["source"], "Posh")
        self.assertEqual(raw["posh"]["order_number"], "77")

    def test_after_midnight_roundtrip(self):
        p = order(event_name="Guestlist | Dria's After Hours", event_start="2026-06-06T01:00:00Z",
                  date_purchased="2026-10-07T18:00:00Z")
        night, how = pw.posh_night(p["event_start"], p["date_purchased"])
        raw = posh.parse_signup("m1", pw.to_signup_text(p, night, how))
        self.assertEqual(raw["start_date"], "2026-10-09")
        self.assertEqual(raw["venues"], ["Drai's"])

    def test_venue_mapping(self):
        for name, venue in (("Guestlist | TAO NC", "TAO Nightclub"), ("OMNIA DAYCLUB", "OMNIA Dayclub"),
                            ("Marquee Night Club", "Marquee Nightclub"), ("Hakkasan Fridays", "Hakkasan Nightclub"),
                            ("LAVO Party Brunch", "LAVO Party Brunch")):
            self.assertEqual(posh.venue_from_event(name), venue)

    def test_item_names_with_commas_dont_split(self):
        self.assertEqual(pw.ticket_names([{"name": "Guest List - Female, Free"}]), ["Guest List - Female Free"])


class FakeGmail:
    def __init__(self, existing=False):
        self.existing, self.inserted, self.queries = existing, [], []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return mock.Mock(execute=lambda: {"messages": [{"id": "x"}]} if self.existing else {})

    def insert(self, userId, body):
        self.inserted.append(body)
        return mock.Mock(execute=lambda: {"id": "new"})


class DedupeTests(unittest.TestCase):
    def test_inserts_once_then_dedupes_in_memory(self):
        g = FakeGmail()
        intake = pw.Intake(lambda: g, "intake@example.com", lookup=NOLOOKUP)
        self.assertEqual(intake.accept(order()), (200, "queued"))
        self.assertEqual(intake.accept(order()), (200, "duplicate (this run)"))
        self.assertEqual(len(g.inserted), 1)
        self.assertEqual(g.inserted[0]["labelIds"], ["INBOX", "UNREAD"])

    def test_gmail_dedupe_survives_restart(self):
        g = FakeGmail(existing=True)
        self.assertEqual(pw.Intake(lambda: g, "i@x.com", lookup=NOLOOKUP).accept(order()), (200, "duplicate"))
        self.assertEqual(g.inserted, [])
        self.assertIn('"Order Number: 77"', g.queries[0])
        self.assertIn('"600000000000000000000000" "test@example.com"', g.queries[0])

    def test_webhook_only_dedupe_ignores_zapier_copies(self):
        with mock.patch.dict(os.environ, {"POSH_WEBHOOK_ONLY": "true"}):
            self.assertIn(pw.WEBHOOK_MARKER, pw.dedupe_query(order()))

    def test_ignored_and_dry_run_insert_nothing(self):
        g = FakeGmail()
        self.assertEqual(pw.Intake(lambda: g, "i", lookup=NOLOOKUP).accept(order(refunded=True))[1], "ignored: order is refunded")
        self.assertEqual(pw.Intake(lambda: g, "i", dry_run=True, lookup=NOLOOKUP).accept(order())[1], "dry run")
        self.assertEqual(g.inserted, [])


class MainIntegrationTests(unittest.TestCase):
    def test_zapier_email_skipped_when_webhook_only(self):
        import main
        import gmail_client
        zap = "NEW POSH SIGNUP\nName: A B\nEmail: a@b.com\nEvent: Marquee\nEvent Date: 2026-07-18T22:30:00Z\nTicket: Guest List - Female\nSOURCE: POSH"
        with mock.patch.dict(os.environ, {"POSH_WEBHOOK_ONLY": "true"}), \
                mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, zap)), \
                mock.patch.object(gmail_client, "get_subject", return_value="NEW POSH SIGNUP"):
            self.assertEqual(main.handle_message(None, "m1", True, {}), gmail_client.PROCESSED_LABEL)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"POSH_WEBHOOK_TOKEN": "tok"})
        self.env.start()
        self.gmail = FakeGmail()
        self.server = pw.serve(pw.Intake(lambda: self.gmail, "i@x.com", lookup=NOLOOKUP), port=0, host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def post(self, path, payload, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_healthz(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_rejects_bad_token(self):
        self.assertEqual(self.post("/webhooks/posh?token=bad", order())[0], 401)
        self.assertEqual(self.gmail.inserted, [])

    def test_accepts_and_logs_signature_header_name(self):
        with self.assertLogs("amy.posh_webhook", "INFO") as logs:
            code, body = self.post("/webhooks/posh?token=tok", order(), {"X-Posh-Signature": "VALUE123"})
        self.assertEqual((code, body["result"]), (200, "queued"))
        text = "\n".join(logs.output)
        self.assertIn("x-posh-signature", text)
        self.assertNotIn("VALUE123", text)
        self.assertNotIn("tok", text.replace("token", ""))

    def test_bad_json(self):
        req = urllib.request.Request(self.base + "/webhooks/posh?token=tok", data=b"{nope")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

"""recent_confirmations unpacks get_plain_text_body's (msg, body) tuple;
send_follow_ups caps a cycle and skips stale backlog."""
import os
import unittest
from datetime import date
from unittest import mock

import gmail_client
import main


class FakeService:
    def __init__(self, ids):
        self.ids = ids

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return mock.Mock(execute=lambda: {"messages": [{"id": i} for i in self.ids]})

    def get(self, **kw):
        return mock.Mock(execute=lambda: {"internalDate": "1791500000000",
                                          "payload": {"headers": [{"name": "To", "value": "Mia <mia@example.com>"}]}})


BODY = "Hi Mia,\r\n\r\nSaturday, October 10 — OMNIA Nightclub\r\n"


class RecentConfirmationsTest(unittest.TestCase):
    def test_tuple_body_is_unpacked(self):
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, BODY)):
            out = gmail_client.recent_confirmations(FakeService(["a"]))
        self.assertEqual(out[0]["first_name"], "Mia")
        self.assertEqual(out[0]["last_night"], date(2026, 10, 10))
        self.assertIn("OMNIA Nightclub", out[0]["venues"])


class FollowUpThrottleTest(unittest.TestCase):
    def run_with(self, confs, today, env=None):
        sent = []
        with mock.patch.dict(os.environ, {"NEWSLETTER_LINK_SECRET": "s", **(env or {})}), \
             mock.patch.object(main, "marketing_blocked", return_value=False), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda s, mid, to, *a, **k: sent.append(to) or True):
            main.send_follow_ups(None, "live", None, today=today)
        return sent

    def test_cap_per_cycle(self):
        confs = [{"id": str(i), "to": f"g{i}@x.com", "first_name": "G", "venues": [],
                  "last_night": date(2026, 10, 1)} for i in range(12)]
        self.assertEqual(len(self.run_with(confs, date(2026, 10, 9), {"FOLLOW_UP_MAX_PER_CYCLE": "3"})), 3)

    def test_stale_backlog_skipped(self):
        confs = [{"id": "1", "to": "old@x.com", "first_name": "O", "venues": [], "last_night": date(2026, 9, 1)}]
        self.assertEqual(self.run_with(confs, date(2026, 10, 10)), [])


class ScottsdaleAndDedupeFollowUpTests(unittest.TestCase):
    def test_parses_almost_famous_hey_greeting_and_scottsdale_venue(self):
        body = (
            "YOU'RE ON THE LIST\n\n"
            "Hey Abby, get ready for an unforgettable night. Here are your details for Almost Famous.\n\n"
            "Date: Saturday, October 17\n"
            "Arrival Time: Arrive by 10:00 PM\n"
            "Almost Famous — 7419 E Indian Plaza, Scottsdale, AZ 85251\n"
        )
        first, venues, last_night = gmail_client._parse_confirmation(
            body, "You're on the list: Almost Famous — Sat Oct 17 — Playmaker Entertainment", date(2026, 10, 16)
        )
        self.assertEqual(first, "Abby")
        self.assertEqual(venues, ["Almost Famous"])
        self.assertEqual(last_night, date(2026, 10, 17))

    def test_parses_abbreviated_month_and_riot_house(self):
        body = (
            "Hi Carter,\n\n"
            "Fri, Oct 16 - Riot House\n"
        )
        first, venues, last_night = gmail_client._parse_confirmation(
            body, "Updated: You're on the list: Riot House — Fri Oct 16 — Playmaker Entertainment", date(2026, 10, 16)
        )
        self.assertEqual(first, "Carter")
        self.assertEqual(venues, ["Riot House"])
        self.assertEqual(last_night, date(2026, 10, 16))

    def test_scottsdale_follow_up_wording_plain_and_html(self):
        from templates import emails, html_emails
        subj, text = emails.follow_up_email("Abby", ["Almost Famous"])
        self.assertEqual(subj, "Hope you had fun at Almost Famous Abby")
        self.assertIn("Thanks for coming out to Almost Famous!", text)
        self.assertNotIn("Vegas trip", text)

        html = html_emails.render_follow_up("Abby", "https://example.com/in", "https://example.com/out",
                                            venues=["Almost Famous"])
        self.assertIn("we hope you enjoyed your night at Almost Famous in Scottsdale.", html)
        self.assertIn("Hope you had an amazing time at Almost Famous!", html)
        self.assertNotIn("Vegas treated", html)
        self.assertNotIn("Vegas trip", html)

    def test_send_follow_ups_uses_latest_night_across_multiple_confirmations(self):
        confs = [
            {"id": "c1", "to": "guest@example.com", "first_name": "Alex",
             "venues": ["Hakkasan Nightclub"], "last_night": date(2026, 10, 16)},
            {"id": "c2", "to": "guest@example.com", "first_name": "Alex",
             "venues": ["Hakkasan Nightclub", "JEWEL Nightclub"], "last_night": date(2026, 10, 18)},
        ]
        # On Oct 23 (7 days after Oct 16, but only 5 days after Oct 18), should NOT send yet.
        self.assertEqual(FollowUpThrottleTest().run_with(confs, date(2026, 10, 23)), [])
        # On Oct 25 (7 days after Oct 18), SHOULD send once.
        self.assertEqual(FollowUpThrottleTest().run_with(confs, date(2026, 10, 25)), ["guest@example.com"])

    def test_zoho_branded_followup_prevents_midnight_job_duplicate(self):
        import posh_webhook as pw
        pw._branded_times.clear()
        pw._zoho_sent.clear()
        svc = mock.MagicMock()
        svc.users().messages().list().execute.return_value = {"messages": []}
        payload = {
            "to": "newguest@example.com",
            "subject": "Hope you had fun at Almost Famous Newguest",
            "html": "<p>Hi</p>",
            "text": "Hi",
        }
        with mock.patch.dict(os.environ, {"ZOHO_SMTP_USER": "info@playmakerentertainment.com", "ZOHO_SMTP_PASSWORD": "p"}), \
             mock.patch.object(pw, "_zoho_send", return_value="z1"):
            code, res = pw.send_branded(svc, payload, _sleep=lambda s: None)
            self.assertEqual(code, 200)
            self.assertEqual(res["via"], "zoho")

        # Now Amy's midnight job checks already_sent with FOLLOW_UP_SEARCH -> True!
        self.assertTrue(
            gmail_client.already_sent(svc, f"to:newguest@example.com {main.FOLLOW_UP_SEARCH} newer_than:60d")
        )
        # And a second /send-branded call with a slightly different follow-up subject also dedupes!
        payload2 = dict(payload, subject="Hope you had fun in Vegas Newguest")
        self.assertEqual(pw.send_branded(svc, payload2, _sleep=lambda s: None), (200, "duplicate (already in Sent)"))

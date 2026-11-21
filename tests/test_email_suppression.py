"""Rejected / bounced addresses are recorded once and never retried."""
import os
import smtplib
import tempfile
import unittest
from datetime import date
from unittest import mock

import email_suppression
import gmail_client
import main
import posh_webhook as pw

from tests.test_branded_send import FakeGmail, PAYLOAD
from tests.test_follow_up_fix import FollowUpThrottleTest


DSN = """\
Address not found

Your message wasn't delivered to kadiance_olson@hotmail.com because the
address couldn't be found, or is unable to receive mail.

The email account that you tried to reach does not exist.

Final-Recipient: rfc822; kadiance_olson@hotmail.com
Action: failed
Status: 5.1.1
"""


class HardRejectDetection(unittest.TestCase):
    def test_smtp_550_and_user_unknown_are_hard(self):
        refused = smtplib.SMTPRecipientsRefused(
            {"bad@example.com": (550, b"5.1.1 User unknown")}
        )
        self.assertTrue(email_suppression.is_hard_reject(refused))
        self.assertTrue(email_suppression.is_hard_reject(550, "5.1.1 User unknown"))
        self.assertTrue(email_suppression.is_hard_reject(Exception("550 5.1.1 User unknown")))

    def test_transient_errors_are_not_hard(self):
        self.assertFalse(email_suppression.is_hard_reject(TimeoutError("t")))
        self.assertFalse(email_suppression.is_hard_reject(429, "try later"))
        self.assertFalse(email_suppression.is_hard_reject(502, "opt-out check failed"))
        greylist = smtplib.SMTPResponseException(451, b"Try again later")
        self.assertFalse(email_suppression.is_hard_reject(greylist))
        auth = smtplib.SMTPAuthenticationError(535, b"auth failed")
        self.assertFalse(email_suppression.is_hard_reject(auth))

    def test_successful_send_body_is_not_a_reject(self):
        self.assertFalse(email_suppression.is_hard_reject(
            200, '{"result": {"sent": true, "id": "abc"}}'))


class SuppressionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AMY_CACHE_DIR": self.tmp.name})
        self.env.start()
        email_suppression.reset_for_tests()

    def tearDown(self):
        self.env.stop()
        email_suppression.reset_for_tests()
        self.tmp.cleanup()

    def test_kadiance_is_seeded(self):
        self.assertTrue(email_suppression.is_suppressed("kadiance_olson@hotmail.com"))
        self.assertTrue(email_suppression.is_suppressed("Kadiance_Olson@Hotmail.com"))

    def test_record_round_trip(self):
        rec = email_suppression.record("bad@example.com", reason="rejected", source="test")
        self.assertEqual(rec["email"], "bad@example.com")
        email_suppression.reset_for_tests()
        self.assertTrue(email_suppression.is_suppressed("bad@example.com"))

    def test_does_not_suppress_internal_addresses(self):
        self.assertIsNone(email_suppression.record("team@playmakerentertainment.com", source="test"))
        self.assertFalse(email_suppression.is_suppressed("team@playmakerentertainment.com"))

    def test_bounce_recipients_from_dsn(self):
        self.assertEqual(
            email_suppression.bounce_recipients(DSN),
            ["kadiance_olson@hotmail.com"],
        )

    def test_ingest_bounces_records_failed_recipient(self):
        svc = mock.MagicMock()
        svc.users().messages().list().execute.return_value = {"messages": [{"id": "dsn1"}]}
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, DSN)):
            added = email_suppression.ingest_bounces(svc)
        self.assertGreaterEqual(added, 0)
        self.assertTrue(email_suppression.is_suppressed("kadiance_olson@hotmail.com"))


class SendBrandedHardReject(unittest.TestCase):
    def setUp(self):
        pw._branded_times.clear()
        pw._zoho_sent.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "AMY_CACHE_DIR": self.tmp.name,
            "ZOHO_SMTP_USER": "info@playmakerentertainment.com",
            "ZOHO_SMTP_PASSWORD": "p",
        })
        self.env.start()
        email_suppression.reset_for_tests()

    def tearDown(self):
        self.env.stop()
        email_suppression.reset_for_tests()
        self.tmp.cleanup()

    def test_zoho_hard_reject_does_not_fall_back_to_gmail(self):
        g = FakeGmail()
        refused = smtplib.SMTPRecipientsRefused(
            {"guest@example.com": (550, b"5.1.1 User unknown")}
        )
        with mock.patch.object(pw, "_zoho_send", side_effect=refused):
            code, res = pw.send_branded(g, dict(PAYLOAD), _sleep=lambda s: None)
        self.assertEqual(code, 422)
        self.assertTrue(res["rejected"])
        self.assertEqual(g.sent, [])
        self.assertTrue(email_suppression.is_suppressed("guest@example.com"))
        # A later attempt is skipped and still does not hit Gmail.
        g2 = FakeGmail()
        code2, res2 = pw.send_branded(g2, dict(PAYLOAD), _sleep=lambda s: None)
        self.assertEqual(code2, 422)
        self.assertEqual(g2.sent, [])

    def test_seeded_address_is_not_sent(self):
        g = FakeGmail()
        code, res = pw.send_branded(
            g, dict(PAYLOAD, to="kadiance_olson@hotmail.com"), _sleep=lambda s: None)
        self.assertEqual(code, 422)
        self.assertEqual(g.sent, [])

    def test_zoho_timeout_still_falls_back_to_gmail(self):
        g = FakeGmail()
        with mock.patch.object(pw, "_zoho_send", side_effect=TimeoutError("t")):
            code, r = pw.send_branded(g, dict(PAYLOAD), _sleep=lambda s: None)
        self.assertEqual((code, r["via"], r["id"]), (200, "gmail", "gm123"))
        self.assertEqual(len(g.sent), 1)
        self.assertFalse(email_suppression.is_suppressed("guest@example.com"))


class FollowUpSuppression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AMY_CACHE_DIR": self.tmp.name, "NEWSLETTER_LINK_SECRET": "s"})
        self.env.start()
        email_suppression.reset_for_tests()

    def tearDown(self):
        self.env.stop()
        email_suppression.reset_for_tests()
        self.tmp.cleanup()

    def test_send_follow_ups_skips_suppressed_address(self):
        confs = [{"id": "1", "to": "kadiance_olson@hotmail.com", "first_name": "Kadiance",
                  "venues": ["Almost Famous"], "last_night": date(2026, 11, 14)}]
        sent = FollowUpThrottleTest().run_with(confs, date(2026, 11, 21))
        self.assertEqual(sent, [])

    def test_send_follow_ups_records_reject_and_continues(self):
        confs = [
            {"id": "1", "to": "bad@example.com", "first_name": "Bad",
             "venues": ["Almost Famous"], "last_night": date(2026, 11, 14)},
            {"id": "2", "to": "good@example.com", "first_name": "Good",
             "venues": ["Almost Famous"], "last_night": date(2026, 11, 14)},
        ]
        sent = []
        refused = smtplib.SMTPRecipientsRefused(
            {"bad@example.com": (550, b"5.1.1 User unknown")}
        )

        def _send(s, mid, to, *a, **k):
            if to == "bad@example.com":
                raise refused
            sent.append(to)
            return True

        with mock.patch.dict(os.environ, {"NEWSLETTER_LINK_SECRET": "s", "AMY_CACHE_DIR": self.tmp.name}), \
             mock.patch.object(main, "marketing_blocked", return_value=False), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once", side_effect=_send), \
             mock.patch.object(email_suppression, "ingest_bounces", return_value=0):
            main.send_follow_ups(None, "live", None, today=date(2026, 11, 21))
        self.assertEqual(sent, ["good@example.com"])
        self.assertTrue(email_suppression.is_suppressed("bad@example.com"))
        # Next cycle must not call send_once for the rejected address.
        sent.clear()
        calls = []

        def _send2(s, mid, to, *a, **k):
            calls.append(to)
            sent.append(to)
            return True

        with mock.patch.dict(os.environ, {"NEWSLETTER_LINK_SECRET": "s", "AMY_CACHE_DIR": self.tmp.name}), \
             mock.patch.object(main, "marketing_blocked", return_value=False), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once", side_effect=_send2), \
             mock.patch.object(email_suppression, "ingest_bounces", return_value=0):
            main.send_follow_ups(None, "live", None, today=date(2026, 11, 21))
        self.assertEqual(calls, ["good@example.com"])

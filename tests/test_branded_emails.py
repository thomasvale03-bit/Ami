"""Branded HTML emails: multipart/alternative with plain-text fallback,
first name filled in, VIP block only for parties of 4+."""
import base64
import email
import unittest
from datetime import date
from email import policy
from unittest import mock

import gmail_client
import main
from config import rules
from templates import html_emails

VIP_URL = "https://booketing.com/microsite/pme/"


def parse(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]), policy=policy.default)


class RenderTest(unittest.TestCase):
    def test_follow_up_has_name_and_no_placeholders(self):
        out = html_emails.render_follow_up("Mia <b>")
        self.assertIn("Mia &lt;b&gt;", out)
        self.assertNotIn("{{", out)

    def _conf(self, size):
        nights = [{"date": "2026-10-10", "venue": "TAO Nightclub", "headliner": "DJ X"}]
        return html_emails.concierge_confirmation_html("Addison", nights, size)

    def test_vip_block_only_for_party_of_four_or_more(self):
        for size, expected in ((1, False), (3, False), (4, True), (8, True)):
            out = self._conf(size)
            self.assertEqual(VIP_URL in out, expected, size)
            self.assertNotIn("{{", out)
            self.assertIn("Addison", out)
            self.assertIn("TAO Nightclub", out)
            self.assertIn("Sat, Oct 10", out)

    def test_rerouted_note(self):
        nights = [{"date": "2026-10-10", "venue": "TAO Nightclub", "rerouted_from": "OMNIA Nightclub"}]
        self.assertIn("OMNIA Nightclub", html_emails.concierge_confirmation_html("A", nights, 2))

    def test_drais_falls_back_to_plain_text(self):
        nights = [{"date": "2026-10-10", "venue": "TAO Nightclub"}]
        self.assertIsNone(html_emails.concierge_confirmation_html("A", nights, 4, drais={"nights": []}))
        self.assertIsNone(html_emails.consolidated_confirmation_html("A", [], drais=True))

    def test_consolidated_party_size(self):
        regs = [{"date": "2026-10-10", "venue": "OMNIA", "event": "Gig", "female_count": 2, "male_count": 2}]
        self.assertIn(VIP_URL, html_emails.consolidated_confirmation_html("A", regs))
        regs[0]["male_count"] = 1
        self.assertNotIn(VIP_URL, html_emails.consolidated_confirmation_html("A", regs))


class BuildMessageTest(unittest.TestCase):
    def test_multipart_with_fallback_and_same_headers(self):
        msg = parse(gmail_client.build_message("g@example.com", "Subj", "plain body", "amy@x.com",
                                               cc="cc@x.com", html="<p>hi</p>"))
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertEqual(msg["From"], "Playmaker Entertainment <amy@x.com>")
        self.assertEqual(msg["To"], "g@example.com")
        self.assertEqual(msg["Cc"], "cc@x.com")
        self.assertIn("plain body", msg.get_body(("plain",)).get_content())
        self.assertIn("<p>hi</p>", msg.get_body(("html",)).get_content())

    def test_plain_only_without_html(self):
        msg = parse(gmail_client.build_message("g@example.com", "S", "plain", "a@x.com"))
        self.assertEqual(msg.get_content_type(), "text/plain")


class FollowUpSendTest(unittest.TestCase):
    def test_follow_up_sends_html(self):
        conf = {"id": "c1", "to": "Mia <mia@example.com>", "first_name": "Mia",
                "last_night": date(2026, 10, 1), "venues": ["OMNIA Nightclub"]}
        with mock.patch.object(gmail_client, "recent_confirmations", return_value=[conf]), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once", return_value=True) as send:
            main.send_follow_ups(object(), "live", today=date(2026, 10, 8))
        args, kw = send.call_args
        self.assertEqual(args[2], "mia@example.com")
        self.assertEqual(kw["sender"], rules.SENDER_EMAIL)
        self.assertIn("Mia", kw["html"])
        self.assertIn("Mia", args[4])  # plain-text fallback kept
        self.assertNotIn("cc", kw)


if __name__ == "__main__":
    unittest.main()

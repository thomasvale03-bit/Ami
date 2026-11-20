"""Automatic first-touch welcome for playmakerentertainment.com promoter applications."""
import base64
import email
from email.header import decode_header, make_header
import json
import os
import unittest
from unittest import mock

import posh_webhook as pw
from templates import html_emails


PAYLOAD_TEXT = (
    "Full name: Jane Applicant\n"
    "Email: jane.applicant@example.com\n"
    "Phone: 7025550100\n"
    "Instagram handle: @jane\n"
    "City: Las Vegas\n\n"
    "Reply to: jane.applicant@example.com\n"
    "Received: 11/20/2026 04:00:00 PM PT"
)



def decoded_header(msg, name):
    return str(make_header(decode_header(msg[name] or "")))

TEAM_NOTICE = {
    "to": "team@playmakerentertainment.com",
    "subject": "Promoter application - Jane Applicant",
    "text": PAYLOAD_TEXT,
    "html": "<pre>Full name: Jane Applicant\nEmail: jane.applicant@example.com</pre>",
}


class FakeGmail:
    def __init__(self, existing=None):
        self.existing = existing or set()
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        hit = any(key in q for key in self.existing)
        return mock.Mock(execute=lambda: {"messages": [{"id": "x"}]} if hit else {})

    def send(self, userId, body):
        self.sent.append(body)
        return mock.Mock(execute=lambda: {"id": f"gm{len(self.sent)}"})


class PromoterWelcomeTemplateTests(unittest.TestCase):
    def test_exact_wording_and_chrome(self):
        text = html_emails.promoter_welcome_text()
        html = html_emails.render_promoter_welcome()
        for part in html_emails.PROMOTER_WELCOME_PARAS:
            self.assertIn(part, text)
            self.assertIn(part, html)
        self.assertIn("https://playmakerentertainment.com/brand/logo-email.jpg", html)
        self.assertIn("#D4AF37", html)
        self.assertIn("info@playmakerentertainment.com", html)
        self.assertIn("playmakerentertainment.com", html)
        self.assertNotIn("TEST:", html_emails.PROMOTER_WELCOME_SUBJECT)
        self.assertNotIn(",", html_emails.PROMOTER_WELCOME_SUBJECT)
        self.assertNotIn(";", html_emails.PROMOTER_WELCOME_SUBJECT)
        # No phone numbers or pay figures
        self.assertNotIn("tel:", html)
        self.assertNotRegex(html, r"\$\d")
        self.assertNotRegex(text, r"\$\d")
        self.assertNotIn("7025550100", html)
        self.assertNotIn("payout", html.lower())

    def test_parses_applicant_email(self):
        self.assertEqual(
            pw.promoter_applicant_email(TEAM_NOTICE),
            "jane.applicant@example.com",
        )
        self.assertEqual(
            pw.promoter_applicant_email({"applicant_email": "Pat@Host.COM", "text": ""}),
            "pat@host.com",
        )
        self.assertEqual(pw.promoter_applicant_email({"text": "no email here"}), "")

    def test_only_promoter_applications_trigger(self):
        self.assertTrue(pw.should_send_promoter_welcome(TEAM_NOTICE))
        self.assertFalse(pw.should_send_promoter_welcome({
            "subject": "Sponsorship inquiry - Acme", "text": "Email: a@x.com",
        }))
        self.assertFalse(pw.should_send_promoter_welcome({
            "subject": html_emails.PROMOTER_WELCOME_SUBJECT,
            "text": "Email: a@x.com",
        }))
        self.assertFalse(pw.should_send_promoter_welcome({
            "subject": "TEST: Welcome to Playmaker Entertainment – Next Steps",
            "text": "Email: a@x.com",
        }))
        self.assertFalse(pw.should_send_promoter_welcome({
            **TEAM_NOTICE, "send_promoter_welcome": False,
        }))


class PromoterWelcomeSendTests(unittest.TestCase):
    def setUp(self):
        pw._branded_times.clear()
        pw._zoho_sent.clear()

    def test_send_welcome_from_info_bcc_team_dedupes(self):
        g = FakeGmail()
        code, res = pw.send_promoter_welcome_email(g, "jane.applicant@example.com")
        self.assertEqual(code, 200)
        self.assertEqual(res["from"], "info@playmakerentertainment.com")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(g.sent[0]["raw"]))
        self.assertEqual(msg["From"], "Playmaker Entertainment <info@playmakerentertainment.com>")
        self.assertEqual(msg["To"], "jane.applicant@example.com")
        self.assertEqual(msg["Bcc"], "team@playmakerentertainment.com")
        self.assertEqual(decoded_header(msg, "Subject"), html_emails.PROMOTER_WELCOME_SUBJECT)
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(g.sent[0]["raw"]))
        bodies = []
        for part in parsed.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True) or b""
                bodies.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        combined = "\n".join(bodies)
        self.assertIn("We’re so excited that you want to work with Playmaker Entertainment!", combined)
        self.assertIn("logo-email.jpg", combined)

        # Second send to the same applicant is a no-op (dedupe store).
        g2 = FakeGmail(existing={f'to:jane.applicant@example.com subject:"{html_emails.PROMOTER_WELCOME_SUBJECT}"'})
        code2, res2 = pw.send_promoter_welcome_email(g2, "jane.applicant@example.com")
        self.assertEqual(code2, 200)
        self.assertEqual(g2.sent, [])
        self.assertIn("duplicate", str(res2))

    def test_handler_sends_welcome_after_application_notice(self):
        g = FakeGmail()
        intake = mock.Mock(dry_run=False, service=lambda: g)
        handler = pw.make_handler(intake)
        inst = handler.__new__(handler)
        inst.headers = {"Authorization": "Bearer tok", "Content-Length": "1"}
        inst.rfile = mock.Mock(read=lambda n: json.dumps(TEAM_NOTICE).encode())
        replies = []
        inst._reply = lambda code, result: replies.append((code, result))
        with mock.patch.dict(os.environ, {"BRANDED_SEND_TOKEN": "tok"}):
            inst._send_branded()
        self.assertEqual(replies[0][0], 200)
        self.assertEqual(len(g.sent), 2)
        first = email.message_from_bytes(base64.urlsafe_b64decode(g.sent[0]["raw"]))
        second = email.message_from_bytes(base64.urlsafe_b64decode(g.sent[1]["raw"]))
        self.assertEqual(first["To"], "team@playmakerentertainment.com")
        self.assertEqual(first["Subject"], "Promoter application - Jane Applicant")
        self.assertEqual(second["To"], "jane.applicant@example.com")
        self.assertEqual(decoded_header(second, "Subject"), html_emails.PROMOTER_WELCOME_SUBJECT)
        self.assertIn("promoter_welcome", replies[0][1])

    def test_skips_yesso_fuentes_and_internal(self):
        g = FakeGmail()
        skipped = pw.maybe_send_promoter_welcome(g, {
            "subject": "Promoter application - X",
            "text": "Reply to: yesso_fuentes@gmail.com\n",
            "html": "<p>x</p>",
            "to": "team@playmakerentertainment.com",
        })
        self.assertIsNone(skipped)
        self.assertEqual(g.sent, [])
        skipped2 = pw.maybe_send_promoter_welcome(g, {
            "subject": "Promoter application - X",
            "text": "Reply to: info@playmakerentertainment.com\n",
            "html": "<p>x</p>",
            "to": "team@playmakerentertainment.com",
        })
        self.assertIsNone(skipped2)
        self.assertEqual(g.sent, [])

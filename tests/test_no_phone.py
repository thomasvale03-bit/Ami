"""No phone numbers in guest/marketing emails; info@ is the contact."""
import glob
import os
import re
import unittest

import newsletter
from templates import emails, html_emails

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHONE = re.compile(r"tel:|whatsapp|480[ .)-]*26|\(\d{3}\)\s*\d{3}-\d{4}|\+1[\s(.-]*\d{3}", re.I)


class NoPhoneTests(unittest.TestCase):
    def test_templates(self):
        for f in glob.glob(os.path.join(ROOT, "templates", "email", "**", "*.html"), recursive=True):
            with open(f, encoding="utf-8") as fh:
                self.assertIsNone(PHONE.search(fh.read()), f)

    def test_rendered(self):
        for out in (html_emails.render_follow_up("Mia"), emails.follow_up_email("Mia")[1],
                    newsletter.page("opted_in"), newsletter.page("opted_out"), newsletter.page("invalid")):
            self.assertIsNone(PHONE.search(out))
            self.assertIn("info@playmakerentertainment.com", out)
        self.assertIn('href="mailto:info@playmakerentertainment.com"', html_emails.render_follow_up("Mia"))


class CopyTests(unittest.TestCase):
    def test_follow_up_sections(self):
        out = html_emails.render_follow_up("Mia")
        self.assertNotIn("Book your next night", out)
        self.assertNotIn("Join the Guest List", out)
        self.assertNotIn("round two", out)
        text = emails.follow_up_email("Mia")[1]
        self.assertNotIn("VIP tables", text)
        self.assertNotIn("booketing", text)

    def test_confirmation_vip_block_unchanged(self):
        out = html_emails.render_confirmation("Mia", [("Sat, Oct 10", "TAO Nightclub", None)])
        self.assertIn("Want to take the night to the next level?", out)
        self.assertIn('href="https://booketing.com/microsite/pme/"', out)

    def test_follow_up_light_vip_line(self):
        out = html_emails.render_follow_up("Mia")
        self.assertNotIn("VIP tables are always an option", out)
        self.assertNotIn("booketing", out)
        self.assertNotIn("set up by a real host", out)
        self.assertNotIn("VIP Tables &#9733;", out)


class TeamEmailNoPhoneTests(unittest.TestCase):
    """Thomas: no phone numbers in any guest/team email; Drai's is email-only."""

    def test_drais_team_email(self):
        import worker_emails
        raw = {"phone": "+15625550123", "posh": {"order_number": "123"}}
        req = {"first_name": "Mia", "last_name": "Test", "email": "mia@example.com",
               "female_count": "3", "male_count": "1", "start_date": "2026-11-14", "end_date": "2026-11-14"}
        out = worker_emails.drais_team_email("ref1", raw, req, [{"date": "2026-11-14"}])
        for body in (out["text"], out["html"]):
            self.assertNotIn("Phone", body)
            self.assertNotIn("5625550123", body)
            self.assertNotIn("Added in Speakeasy", body)
        self.assertIn("(confirmation email sent, no Speakeasy signup)", out["text"])
        self.assertIn("2026-11-14", out["text"])

    def test_team_templates(self):
        req = {"first_name": "Mia", "last_name": "Test", "email": "mia@example.com", "phone": "6025550100",
               "female_count": "2", "male_count": "1", "promoter": "Tom",
               "start_date": "2026-11-20", "end_date": "2026-11-20"}
        _, body = emails.pending_signup_email(req, [{"date": "2026-11-20", "venue": "TAO Nightclub"}])
        self.assertNotIn("6025550100", body)
        self.assertNotIn("Phone", body)
        out = emails.manual_work_order(req, [{"date": "2026-11-20", "nightclubs": ["TAO Nightclub"], "dayclubs": []}],
                                       "https://example.com")
        text = out if isinstance(out, str) else "\n".join(x for x in out if isinstance(x, str))
        self.assertNotIn("6025550100", text)
        import main
        summary = main.posh_summary({"posh": {"phone": "+17025550111", "order_number": "9"}})
        self.assertNotIn("7025550111", summary)

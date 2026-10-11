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

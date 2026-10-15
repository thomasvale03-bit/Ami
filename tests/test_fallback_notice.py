import unittest
from rules_engine import is_fallback_venue
from templates import emails, html_emails

NOTICE = "Oh No, You Did Not Meet this Venue's GuestList Cut off. But we signed you up for another amazing venue!"
HTML_NOTICE = NOTICE.replace("'", "'")


class FallbackNotice(unittest.TestCase):
    def test_is_fallback_venue(self):
        self.assertTrue(is_fallback_venue({"requested_venues": ["OMNIA Nightclub"]}, "TAO Nightclub"))
        self.assertFalse(is_fallback_venue({"requested_venues": ["OMNIA Nightclub"]}, "OMNIA Nightclub"))
        self.assertFalse(is_fallback_venue({"requested_venues": []}, "TAO Nightclub"))

    def test_guest_confirmation_text_and_html(self):
        nights = [{"date": "2026-10-16", "venue": "JEWEL Nightclub"},
                  {"date": "2026-10-17", "venue": "Marquee Nightclub", "fallback": True}]
        _, body = emails.guest_confirmation("Amy", nights)
        self.assertEqual(body.count(NOTICE), 1)
        self.assertIn("October 17", body.split(NOTICE)[1].split("\n")[0])
        html = html_emails.concierge_confirmation_html("Amy", nights, 2)
        self.assertEqual(html.count("Oh No, You Did Not Meet"), 1)
        self.assertIn("Sat, Oct 17", html)
        self.assertLess(html.index("Oh No"), html.index("<!-- Nights -->"))

    def test_no_fallback_no_change(self):
        nights = [{"date": "2026-10-16", "venue": "JEWEL Nightclub"}]
        _, body = emails.guest_confirmation("Amy", nights)
        self.assertNotIn("Oh No", body)
        html = html_emails.concierge_confirmation_html("Amy", nights, 2)
        self.assertNotIn("Oh No", html)
        self.assertNotIn("{{fallback_note}}", html)

    def test_consolidated(self):
        regs = [{"venue": "TAO Nightclub", "date": "2026-10-18", "event": "", "female_count": 1,
                 "male_count": 0, "confirmation_id": "X1", "fallback": True}]
        _, body = emails.consolidated_confirmation("Amy", "a@b.c", "2026-10-18", regs)
        self.assertEqual(body.count(NOTICE), 1)
        self.assertIn("Oh No", html_emails.consolidated_confirmation_html("Amy", regs))


if __name__ == "__main__":
    unittest.main()

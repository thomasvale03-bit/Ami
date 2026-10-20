import os, unittest
from datetime import datetime, timedelta
from unittest import mock
import gmail_client, posh_webhook as pw


def payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
    p = {"source": "guestlistgo", "first_name": "TEST", "last_name": "Guest", "email": "test@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "venue": "OMNIA", "date": night,
         "women": 2, "men": 1, "age_21_confirmed": True, "ref": "gg-123"}
    p.update(kw)
    return p


class WebsiteForm(unittest.TestCase):
    def test_notice_parses_like_contact_form(self):
        clean, err = pw.validate_website(payload())
        self.assertIsNone(err)
        raw = gmail_client.parse_request("m1", pw.to_website_text(clean))
        self.assertEqual(raw["email"], "test@example.com")
        self.assertEqual(raw["venues"], ["OMNIA Nightclub"])
        self.assertEqual(raw["start_date"], clean["night"].isoformat())
        self.assertEqual((raw["female_count"], raw["male_count"]), ("2", "1"))
        self.assertEqual(raw["promoter"], "GuestlistGo")
        self.assertEqual(raw["source"], "guestlistgo")
        self.assertEqual(raw["brand"], "GuestlistGo")
        self.assertNotIn("_missing_required", raw)
        self.assertNotIn("Playmaker", pw.to_website_text(clean))

    def test_subject_matches_inbox_query(self):
        self.assertIn(f'subject:"{pw.WEBSITE_SUBJECT}"', gmail_client.SEARCH_QUERY)

    def test_playmaker_form_has_no_source(self):
        body = open(os.path.join(os.path.dirname(__file__), "fixtures", "new_form.txt")).read()
        self.assertNotIn("source", gmail_client.parse_request("m", body))

    def test_validation(self):
        self.assertEqual(pw.validate_website(payload(source="x"))[1], "unknown source")
        self.assertEqual(pw.validate_website(payload(age_21_confirmed=False))[1], "21+ not confirmed")
        self.assertEqual(pw.validate_website(payload(venue="Nope"))[1], "bad city/venue")

    def test_origins_env(self):
        with mock.patch.dict(os.environ, {"ALLOWED_ORIGINS": "https://a.example, https://b.example/"}):
            self.assertEqual(pw.website_origins(), {"https://a.example", "https://b.example"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pw.website_origins(), {"https://guestlistgo.me", "https://www.guestlistgo.me"})


if __name__ == "__main__":
    unittest.main()

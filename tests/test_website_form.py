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


class TeamNotice(unittest.TestCase):
    def test_accept_inserts_and_emails_team(self):
        from config import rules
        svc = mock.MagicMock()
        intake = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
        intake._lock = __import__("threading").Lock()
        intake.service.return_value = svc
        with mock.patch.object(gmail_client, "send_once", return_value=True) as so:
            self.assertEqual(pw.accept_website(intake, payload(notes="Birthday")), (200, "queued"))
        svc.users().messages().insert.assert_called_once()
        so.assert_called_once()
        a, k = so.call_args
        self.assertEqual(a[2], "team@playmakerentertainment.com")
        self.assertEqual(a[2], rules.PLAYMAKER_EMAIL)
        self.assertTrue(a[3].startswith("New guest list request submission - GuestlistGo OMNIA "))
        for line in ("Promoter first name: GuestlistGo", "Full name: TEST Guest", "Email: test@example.com",
                     "Phone: 6025550100", "Requested venues: OMNIA", "Female guests: 2", "Male guests: 1",
                     "Additional comments: Birthday", "Source: guestlistgo", "Ref: gg-123"):
            self.assertIn(line, a[4])
        self.assertEqual(k["sender"], rules.SENDER_EMAIL)

    def test_team_failure_does_not_break_intake(self):
        intake = mock.MagicMock(dry_run=False, intake_email="i@example.com", _seen=set())
        intake._lock = __import__("threading").Lock()
        with mock.patch.object(gmail_client, "send_once", side_effect=RuntimeError("boom")):
            self.assertEqual(pw.accept_website(intake, payload()), (200, "queued"))

    def test_dry_run_sends_nothing(self):
        intake = mock.MagicMock(dry_run=True, _seen=set())
        intake._lock = __import__("threading").Lock()
        with mock.patch.object(gmail_client, "send_once") as so:
            self.assertEqual(pw.accept_website(intake, payload()), (200, "dry run"))
        so.assert_not_called()


if __name__ == "__main__":
    unittest.main()

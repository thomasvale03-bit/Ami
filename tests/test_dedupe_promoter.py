import unittest
from datetime import datetime, timedelta
from unittest import mock
import gmail_client, main, assisted_jobs, posh_webhook as pw


def _msg(labels, to):
    return {"labelIds": labels, "payload": {"headers": [{"name": "To", "value": to},
                                                         {"name": "From", "value": "info@playmakerentertainment.com"}]}}


class TeamCopy(unittest.TestCase):
    def test_query_excludes_team_copy(self):
        self.assertIn("-(from:info@playmakerentertainment.com to:team@playmakerentertainment.com)", gmail_client.SEARCH_QUERY)
        self.assertIn("-(from:me to:team@playmakerentertainment.com)", gmail_client.SEARCH_QUERY)

    def test_own_team_copy_detected(self):
        self.assertTrue(gmail_client.is_own_team_copy(_msg(["SENT", "Label_11"], "team@playmakerentertainment.com")))

    def test_intake_insert_and_airo_notice_not_team_copy(self):
        # intake insert: SENT + INBOX (From = intake itself)
        self.assertFalse(gmail_client.is_own_team_copy(_msg(["SENT", "INBOX", "UNREAD"], "valeconsultingaz@gmail.com")))
        # GoDaddy Airo notice to Team@ arrives in INBOX
        self.assertFalse(gmail_client.is_own_team_copy(
            {"labelIds": ["INBOX", "UNREAD"], "payload": {"headers": [{"name": "To", "value": "Team@playmakerentertainment.com"}]}}))

    def test_team_copy_skipped_by_handler(self):
        body = pw.to_website_text(pw.validate_website(_payload())[0])
        with mock.patch.object(gmail_client, "get_plain_text_body",
                               return_value=(_msg(["SENT"], "team@playmakerentertainment.com"), body)), \
             mock.patch.object(gmail_client, "parse_request") as pr:
            self.assertEqual(main._handle_message(mock.MagicMock(), "m2", True, {}), gmail_client.PROCESSED_LABEL)
            pr.assert_not_called()


def _payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
    p = {"source": "playmaker", "first_name": "TEST", "last_name": "Guest", "email": "t@example.com",
         "phone": "6025550100", "city": "Las Vegas", "venue": "OMNIA", "date": night,
         "women": 0, "men": 1, "age_21_confirmed": True, "ref": "pm-dup-1", "promoter": "Test Promoter"}
    p.update(kw)
    return p


class RefDedupe(unittest.TestCase):
    def setUp(self):
        main._WEBSITE_REFS_SEEN.clear()

    def test_same_ref_already_processed_is_skipped(self):
        body = pw.to_website_text(pw.validate_website(_payload())[0])
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({"labelIds": ["INBOX"]}, body)), \
             mock.patch.object(gmail_client, "website_ref_already_handled", return_value=True) as h:
            self.assertEqual(main._handle_message(mock.MagicMock(), "m3", True, {}), gmail_client.PROCESSED_LABEL)
            h.assert_called_once()

    def test_same_ref_in_this_process_is_skipped(self):
        main._WEBSITE_REFS_SEEN.add("pm-dup-1")
        body = pw.to_website_text(pw.validate_website(_payload())[0])
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({"labelIds": ["INBOX"]}, body)), \
             mock.patch.object(gmail_client, "website_ref_already_handled", return_value=False):
            self.assertEqual(main._handle_message(mock.MagicMock(), "m4", True, {}), gmail_client.PROCESSED_LABEL)

    def test_ref_search_query(self):
        svc = mock.MagicMock()
        svc.users().messages().list().execute.return_value = {"messages": [{"id": "self"}, {"id": "other"}]}
        self.assertTrue(gmail_client.website_ref_already_handled(svc, "pm-x", "self"))
        svc.users().messages().list().execute.return_value = {"messages": [{"id": "self"}]}
        self.assertFalse(gmail_client.website_ref_already_handled(svc, "pm-x", "self"))
        self.assertFalse(gmail_client.website_ref_already_handled(svc, "", "self"))


class PromoterInJob(unittest.TestCase):
    def test_job_notes_and_email_credit_promoter(self):
        guest = {"first_name": "TEST", "last_name": "Guest", "email": "t@example.com", "phone": "6025550100",
                 "male_count": 1, "promoter": "Test Promoter"}
        job = assisted_jobs.make_job("https://tickets.taogroup.com/e/x", guest, venue="OMNIA Nightclub", date="2026-11-07")
        self.assertEqual(job["promoter"], "Test Promoter")
        self.assertTrue(job["notes"].startswith("Promoter: Test Promoter"))
        self.assertEqual(assisted_jobs.decode(assisted_jobs.encode(job))["promoter"], "Test Promoter")
        subject, body = assisted_jobs.job_email([job], "TEST Guest")
        self.assertIn("Promoter: Test Promoter", body)

    def test_no_promoter_unchanged(self):
        guest = {"first_name": "A", "last_name": "B", "email": "a@example.com", "male_count": 1}
        job = assisted_jobs.make_job("https://tickets.taogroup.com/e/x", guest, venue="OMNIA Nightclub", date="2026-11-07")
        self.assertEqual((job["notes"], job["promoter"]), ("", ""))
        self.assertNotIn("Promoter:", assisted_jobs.job_email([job], "A B")[1])


if __name__ == "__main__":
    unittest.main()

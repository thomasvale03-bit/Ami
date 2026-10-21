"""AMY_GUEST_CONFIRMATION (default off): no immediate guest email; team email
still goes out; follow-ups key off both confirmation subjects, one per guest."""
import os
import unittest
from datetime import date
from unittest import mock

import gmail_client
import main
import tests.test_amy as _amy_tests


class GuestConfirmationOffTests(unittest.TestCase):
    def _run(self, env):
        case = _amy_tests.ConciergeTests()
        case.guest_confirmation = env
        return case._handle()

    def test_default_off_only_team_email(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMY_GUEST_CONFIRMATION", None)
            out, sent, submit = self._run("")
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        submit.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(sent[0][0][2], "jane.sample@example.com")  # team email, not the guest

    def test_on_sends_guest_email(self):
        _out, sent, _ = self._run("true")
        self.assertEqual(len(sent), 2)


class FollowUpSourceTests(unittest.TestCase):
    def test_query_covers_both_subjects(self):
        svc = mock.MagicMock()
        svc.users().messages().list().execute.return_value = {"messages": []}
        gmail_client.recent_confirmations(svc)
        q = svc.users().messages().list.call_args.kwargs["q"]
        self.assertIn('subject:"You\'re on the list"', q)
        self.assertIn('subject:"You\'re on the Playmaker Entertainment guest list"', q)

    def test_one_follow_up_per_guest_with_both_confirmations(self):
        confs = [{"id": i, "to": "Mia <mia@example.com>", "first_name": "Mia", "venues": [],
                  "last_night": date(2026, 10, 1)} for i in ("a", "b")]
        sent = []
        with mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda s, mid, to, *a, **k: sent.append(to) or True):
            main.send_follow_ups(None, "live", None, today=date(2026, 10, 9))
        self.assertEqual(sent, ["mia@example.com"])

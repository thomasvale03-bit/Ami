"""Scottsdale venues (and unrecognized venues) never route to TAO Vegas."""
import os
import pathlib
import unittest
from unittest import mock

import assisted_jobs
import gmail_client
import main
import posh
import tao_portal
from config import rules
from rules_engine import ActionNeeded, normalize_guest_request

FIX = pathlib.Path(__file__).parent / "fixtures"
FORM = (FIX / "new_form.txt").read_text()
POSH = (FIX / "posh_signup.txt").read_text()
CATALOG = {("JEWEL Nightclub", __import__("datetime").date(2026, 10, 30)): {"url": "https://tickets.taogroup.com/e/jewel"},
           ("Marquee Dayclub", __import__("datetime").date(2026, 10, 30)): {"url": "https://tickets.taogroup.com/e/mdc"}}


def raw(venues):
    return {"authorization": "YES", "submitted_at": "2026-10-01", "female_count": "1",
            "male_count": "1", "email": "a@b.com", "start_date": "2026-10-16",
            "end_date": "2026-10-16", "venues": venues, "source_message_id": "m"}


class RulesTests(unittest.TestCase):
    def test_is_scottsdale(self):
        for v in ("Almost Famous", "Riot House Scottsdale", "RIOT HOUSE", "Scottsdale Saturdays"):
            self.assertTrue(rules.is_scottsdale(v), v)
        for v in ("JEWEL Nightclub", "Marquee Dayclub", "Drai's", ""):
            self.assertFalse(rules.is_scottsdale(v), v)

    def test_normalize_rejects_scottsdale(self):
        for v in ("Almost Famous", "Riot House Scottsdale"):
            with self.assertRaises(ActionNeeded) as cm:
                normalize_guest_request(raw([v]))
            self.assertIn("Scottsdale", cm.exception.issue)

    def test_normalize_rejects_unknown_venue(self):
        with self.assertRaises(ActionNeeded):
            normalize_guest_request(raw(["Some Random Club"]))

    def test_known_venue_still_ok(self):
        self.assertEqual(normalize_guest_request(raw(["jewel"]))["requested_venues"], ["JEWEL Nightclub"])

    def test_posh_scottsdale_event_not_mapped_to_vegas(self):
        self.assertIsNone(posh.venue_from_event("Almost Famous Scottsdale | Guestlist"))
        self.assertIsNone(posh.venue_from_event("Riot House Marquee Night"))
        body = POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Saturday Guestlist")
        self.assertEqual(posh.parse_signup("m", body)["venues"], ["Almost Famous Saturday Guestlist"])


class HandleMessageTests(unittest.TestCase):
    def _handle(self, text):
        sent, notes = [], []
        with mock.patch.dict(os.environ, {"AMY_CONCIERGE": "true", "AMY_GUEST_CONFIRMATION": "true",
                                          "ASSISTED_SIGNUP": "true", "POSH_CONSENT_ON_FILE": "true"}), \
             mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, text)), \
             mock.patch.object(gmail_client, "posh_order_already_handled", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda *a, **k: sent.append((a, k)) or True), \
             mock.patch.object(main.amyi, "notify", side_effect=lambda *a, **k: notes.append((a, k))), \
             mock.patch.object(assisted_jobs, "assisted_on", return_value=True), \
             mock.patch.object(tao_portal, "submit_registration") as submit, \
             mock.patch.object(tao_portal, "catalog_snapshot", return_value=CATALOG), \
             mock.patch.object(main, "send_assisted_jobs") as jobs, \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 1, 12, 0)
            out = main.handle_message(mock.MagicMock(), "m1", dry_run=False, labels={})
        return out, sent, notes, submit, jobs

    def _assert_no_tao(self, out, sent, submit, jobs, expected_label=gmail_client.PROCESSED_LABEL):
        self.assertEqual(out, expected_label)
        submit.assert_not_called()
        jobs.assert_not_called()
        for a, k in sent:
            self.assertNotIn("JEWEL", str(a))
            self.assertNotIn("Marquee", str(a))

    def test_contact_form_riot_house(self):
        out, sent, notes, submit, jobs = self._handle(
            FORM.replace("Requested venues: Omnia Nightclub", "Requested venues: Riot House Scottsdale"))
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)  # guest confirmation + website team summary
        self.assertIn("You're on the list: Riot House", sent[0][0][3])
        self.assertEqual(notes, [])  # mark_job suppresses Grok Bot contact_form wake

    def test_posh_almost_famous(self):
        out, sent, notes, submit, jobs = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Guestlist"))
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 1)  # Posh Almost Famous sends guest confirmation only (door list covers team)
        self.assertIn("You're on the list: Almost Famous", sent[0][0][3])
        self.assertEqual(notes, [])

    def test_unknown_venue_goes_to_team_not_default(self):
        out, sent, notes, submit, jobs = self._handle(
            FORM.replace("Requested venues: Omnia Nightclub", "Requested venues: Mystery Lounge"))
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.EXCEPTION_LABEL)
        self.assertEqual(sent, [])  # needs-attention is suppressed in concierge mode

    def test_send_scottsdale_door_lists_friday_evening(self):
        sent = []
        svc = mock.MagicMock()
        svc.users().messages().list().execute.return_value = {"messages": []}
        friday_evening = __import__("datetime").datetime(2026, 10, 23, 19, 45, tzinfo=main.PHOENIX)
        with mock.patch.object(gmail_client, "already_sent", return_value=False),              mock.patch.object(main, "_send_branded_or_gmail",
                               side_effect=lambda s, mid, to, subj, txt, html, **kw: sent.append((to, subj, txt)) or True):
            count = main.send_scottsdale_door_lists(svc, "live", now=friday_evening)
        self.assertEqual(count, 2)
        self.assertEqual(sent[0][1], "Riot House Guest List — Fri Oct 23")
        self.assertEqual(sent[1][1], "Almost Famous Guest List — Fri Oct 23")
        self.assertIn("No guests yet. Posh shows 0 orders for tonight.", sent[0][2])


if __name__ == "__main__":
    unittest.main()

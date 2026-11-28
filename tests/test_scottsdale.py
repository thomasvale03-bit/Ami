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
from templates import html_emails
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
    def _handle(self, text, queue_on=False, already_handled=False, already_confirmed=False,
                needs_review=False, env=None):
        sent, notes, enqueued = [], [], []
        environ = {"AMY_CONCIERGE": "true", "AMY_GUEST_CONFIRMATION": "true",
                   "ASSISTED_SIGNUP": "true", "POSH_CONSENT_ON_FILE": "true",
                   "AMY_QUEUE_ENABLED": "true" if queue_on else ""}
        if env:
            environ.update(env)
        with mock.patch.dict(os.environ, environ, clear=False), \
             mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, text)), \
             mock.patch.object(gmail_client, "posh_order_already_handled", return_value=already_handled), \
             mock.patch.object(gmail_client, "already_sent", return_value=already_confirmed), \
             mock.patch.object(gmail_client, "website_ref_already_handled", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda *a, **k: sent.append((a, k)) or True), \
             mock.patch.object(main.amyi, "notify", side_effect=lambda *a, **k: notes.append((a, k))), \
             mock.patch.object(assisted_jobs, "assisted_on", return_value=True), \
             mock.patch.object(tao_portal, "submit_registration") as submit, \
             mock.patch.object(tao_portal, "catalog_snapshot", return_value=CATALOG), \
             mock.patch.object(main, "send_assisted_jobs") as jobs, \
             mock.patch.object(main.posh_webhook, "needs_review", return_value=needs_review), \
             mock.patch.object(main.queue_service, "enqueue",
                               side_effect=lambda *a, **k: enqueued.append((a, k)) or {"state": "queued"}), \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 1, 12, 0)
            out = main.handle_message(mock.MagicMock(), "m1", dry_run=False, labels={})
        return out, sent, notes, submit, jobs, enqueued

    def _assert_no_tao(self, out, sent, submit, jobs, expected_label=gmail_client.PROCESSED_LABEL):
        self.assertEqual(out, expected_label)
        submit.assert_not_called()
        jobs.assert_not_called()
        for a, k in sent:
            self.assertNotIn("JEWEL", str(a))
            self.assertNotIn("Marquee", str(a))

    def test_contact_form_riot_house(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
            FORM.replace("Requested venues: Omnia Nightclub", "Requested venues: Riot House Scottsdale"))
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)  # guest confirmation + website team summary
        self.assertIn("You're on the list: Riot House", sent[0][0][3])
        self.assertEqual(notes, [])  # mark_job suppresses Grok Bot contact_form wake

    def test_posh_almost_famous(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Guestlist"))
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)  # guest confirmation + team summary (always, incl. Posh AF/Riot House)
        self.assertIn("You're on the list: Almost Famous", sent[0][0][3])
        self.assertTrue(sent[1][0][3].startswith("Signed up: "))
        self.assertEqual(sent[1][0][2], rules.PLAYMAKER_EMAIL)
        self.assertEqual(enqueued, [])
        self.assertEqual(notes, [])
        blob = "\n".join(str(a) + str(k) for a, k in sent)
        self.assertNotIn("+16025550100", blob)
        self.assertNotRegex(blob, r"(?i)\bphone\b")
        self.assertNotIn("jarvis", blob.lower())
        self.assertNotIn("infojarvis", blob.lower())


    def test_posh_almost_famous_sends_in_queue_mode(self):
        """Queue mode must not swallow Riot House / Almost Famous confirmations."""
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Guestlist"),
            queue_on=True)
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)
        self.assertIn("You're on the list: Almost Famous", sent[0][0][3])
        self.assertIn("Posh order: 1001", sent[0][0][4])
        self.assertTrue(sent[1][0][3].startswith("Signed up: "))
        self.assertEqual(sent[1][0][2], "team@playmakerentertainment.com")
        self.assertEqual(enqueued, [])
        self.assertEqual(notes, [])

    def test_posh_riot_house_sends_in_queue_mode(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Riot House | Guest List"),
            queue_on=True)
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)
        self.assertIn("You're on the list: Riot House", sent[0][0][3])
        self.assertTrue(sent[1][0][3].startswith("Signed up: "))
        self.assertEqual(enqueued, [])

    def test_posh_scottsdale_confirms_despite_recurring_date_review(self):
        """Wajhee-style recurring_guess_needs_review must not block the confirmation."""
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous"),
            queue_on=True, needs_review=True)
        self._assert_no_tao(out, sent, submit, jobs, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 2)
        self.assertIn("You're on the list: Almost Famous", sent[0][0][3])
        self.assertEqual(enqueued, [])

    def test_posh_order_deduped_to_one_confirmation(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Riot House | Guest List"),
            queue_on=True, already_handled=True)
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        self.assertEqual(sent, [])
        self.assertEqual(enqueued, [])
        submit.assert_not_called()

    def test_posh_order_already_confirmed_in_sent_is_deduped(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Guestlist"),
            queue_on=True, already_confirmed=True)
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        self.assertEqual(sent, [])
        self.assertEqual(enqueued, [])

    def test_vegas_posh_still_enqueues_in_queue_mode(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(POSH, queue_on=True)
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        self.assertEqual(sent, [])
        self.assertEqual(len(enqueued), 1)
        submit.assert_not_called()
        jobs.assert_not_called()

    def test_late_friday_signup_still_gets_confirmation(self):
        """Signups after the 7:43 PM door list still get a confirmation email."""
        out, sent, notes, submit, jobs, enqueued = self._handle(
            POSH.replace("Playmaker Tuesdays at OMNIA", "Riot House | Guest List"),
            queue_on=True)
        self.assertEqual(len(sent), 2)
        self.assertIn("You're on the list: Riot House", sent[0][0][3])
        # Door-list job is a separate scheduled send; this path must not emit one.
        for a, k in sent:
            self.assertNotIn("Guest List —", str(a[3]) if len(a) > 3 else "")

    def test_unknown_venue_goes_to_team_not_default(self):
        out, sent, notes, submit, jobs, enqueued = self._handle(
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
        self.assertEqual(
            sent[0][2],
            "Riot House – Fri 10/23\nTotal: 0 parties – 0 girls / 0 guys",
        )
        self.assertEqual(
            sent[1][2],
            "Almost Famous – Fri 10/23\nTotal: 0 parties – 0 girls / 0 guys",
        )

    def test_door_list_query_includes_sent_webhooks(self):
        q = main.scottsdale_door_list_query('"Almost Famous"')
        self.assertIn("newer_than:14d", q)
        self.assertIn('subject:"NEW POSH SIGNUP"', q)
        self.assertIn('"Almost Famous"', q)
        # Sent webhook mail is included; -in:sent is not applied to that branch.
        self.assertIn('OR (subject:"NEW POSH SIGNUP"', q)

    def test_send_scottsdale_door_lists_includes_sent_webhooks_and_dedupes(self):
        """Webhook self-mail (SENT+INBOX) is counted once per Posh order #."""
        wajhee = (
            "NEW POSH CLIENT\nCLIENT INFORMATION\nName: Wajhee Amer\n"
            "Email: wajhee.amer.24@gmail.com\nEVENT INFORMATION\n"
            "Event: Almost Famous\nEvent Date: 2026-11-14T02:00:00.000Z\n"
            "Ticket: Female Admission,Male Admission\nORDER INFORMATION\n"
            "Order Number: 40722378\nSOURCE: POSH WEBHOOK\n"
        )
        bodies = {"inbox-copy": wajhee, "sent-copy": wajhee}
        subjects = {
            "inbox-copy": "NEW POSH SIGNUP (webhook) #40722378 Almost Famous",
            "sent-copy": "NEW POSH SIGNUP (webhook) #40722378 Almost Famous",
        }
        current_mid = {"id": None}
        sent, queries = [], []

        def list_side_effect(*args, **kwargs):
            queries.append(kwargs.get("q") or "")
            listed = mock.MagicMock()
            if '"Almost Famous"' in (kwargs.get("q") or ""):
                listed.execute.return_value = {
                    "messages": [{"id": "inbox-copy"}, {"id": "sent-copy"}]
                }
            else:
                listed.execute.return_value = {"messages": []}
            return listed

        def get_body(service, mid):
            current_mid["id"] = mid
            return {}, bodies[mid]

        def get_subject(msg):
            return subjects[current_mid["id"]]

        svc = mock.MagicMock()
        svc.users().messages().list.side_effect = list_side_effect
        friday_evening = __import__("datetime").datetime(
            2026, 11, 13, 19, 45, tzinfo=main.PHOENIX
        )
        with mock.patch.object(gmail_client, "already_sent", return_value=False), \
             mock.patch.object(gmail_client, "get_plain_text_body", side_effect=get_body), \
             mock.patch.object(gmail_client, "get_subject", side_effect=get_subject), \
             mock.patch.object(main, "_send_branded_or_gmail",
                               side_effect=lambda s, mid, to, subj, txt, html, **kw: sent.append((to, subj, txt)) or True):
            count = main.send_scottsdale_door_lists(svc, "live", now=friday_evening)

        self.assertEqual(count, 2)
        self.assertTrue(any('subject:"NEW POSH SIGNUP"' in q for q in queries))
        self.assertEqual(sent[0][1], "Riot House Guest List — Fri Nov 13")
        self.assertEqual(
            sent[0][2],
            "Riot House – Fri 11/13\nTotal: 0 parties – 0 girls / 0 guys",
        )
        self.assertEqual(sent[1][1], "Almost Famous Guest List — Fri Nov 13")
        af = sent[1][2]
        self.assertEqual(
            af,
            "Almost Famous – Fri 11/13\n"
            "Wajhee Amer – 2 (1 girl / 1 guy)\n"
            "Total: 1 party – 1 girl / 1 guy",
        )
        self.assertEqual(af.count("Wajhee Amer"), 1)
        self.assertNotIn("#40722378", af)
        self.assertNotIn("wajhee.amer.24@gmail.com", af)
        self.assertNotIn("Female Admission", af)
        self.assertNotIn("10:00 PM", af)
        self.assertNotIn("Playmaker Entertainment", af)


class DoorShowBannerTests(unittest.TestCase):
    LINE = "Please show this email at the door for free entry before midnight."
    DISCLAIMER = "Does not guarantee expedited check-in."

    def _assert_banner(self, html, text):
        self.assertIn(self.LINE, html)
        self.assertIn(self.DISCLAIMER, html)
        # Gold high-contrast banner wraps the door-show line.
        self.assertIn('bgcolor="#D4AF37"', html)
        idx = html.index(self.LINE)
        self.assertLess(html.rfind('bgcolor="#D4AF37"', 0, idx), idx)
        self.assertLess(idx, html.index(self.DISCLAIMER))
        self.assertIn(self.LINE.upper(), text)
        self.assertIn(self.DISCLAIMER, text)
        # Banner sits near the top, before venue details.
        self.assertLess(html.index("<!-- Show this email at the door -->"), html.index("<!-- Event details -->") if "<!-- Event details -->" in html else html.index("<!-- Riot House guestlist details -->"))

    def test_riot_house_confirmation_has_door_banner(self):
        subj, text, html = html_emails.scottsdale_confirmation_email(
            "Selamawit", "Riot House", ["2026-11-28", "2026-11-29"])
        self._assert_banner(html, text)
        self.assertIn("Selamawit", html)
        self.assertIn("Riot House", html)

    def test_almost_famous_confirmation_has_door_banner(self):
        subj, text, html = html_emails.scottsdale_confirmation_email(
            "Sample Guest", "Almost Famous", ["2026-11-28"])
        self._assert_banner(html, text)
        self.assertIn("Sample Guest", html)
        self.assertIn("Almost Famous", html)

    def test_vegas_confirmation_does_not_get_the_banner(self):
        html = html_emails.render_confirmation(
            "Amy", [("Fri, Nov 27", "OMNIA Nightclub", None)], 8)
        self.assertNotIn(self.LINE, html)
        self.assertNotIn(self.DISCLAIMER, html)
        _, ev_text, ev_html = html_emails.scottsdale_confirmation_email(
            "Amy", "El Hefe", ["2026-11-28"])
        # Other Scottsdale venues (not Riot House / Almost Famous) stay unchanged.
        self.assertNotIn(self.LINE, ev_html)
        self.assertNotIn(self.LINE.upper(), ev_text)



if __name__ == "__main__":
    unittest.main()

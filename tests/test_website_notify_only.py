"""Notify-only website venues (Thomas, Nov 14 2026): ZOUK, Ghostbar, XS,
Encore Beach Club, LIV, LIV Beach. Accepted by intake; one branded team@ email
from info@; no Gmail intake insert, no queue receipt, no guest email, no phone."""
import threading, unittest
from datetime import datetime, timedelta
from unittest import mock
import gmail_client, posh_webhook as pw
import queue_service

NOTIFY = ("ZOUK", "Ghostbar", "XS", "Encore Beach Club", "LIV", "LIV Beach")


def payload(**kw):
    night = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
    p = {"source": "playmaker", "first_name": "TEST", "last_name": "Guest", "email": "test@example.com",
         "phone": "(602) 555-0100", "city": "Las Vegas", "venue": "ZOUK", "date": night,
         "women": 3, "men": 2, "age_21_confirmed": True, "ref": "pm-test1"}
    p.update(kw)
    return p


def intake(dry=False):
    i = mock.MagicMock(dry_run=dry, intake_email="intake@example.com", _seen=set())
    i._lock = threading.Lock()
    i.service.return_value = mock.MagicMock()
    return i


class NotifyOnly(unittest.TestCase):
    def test_all_six_validate(self):
        for v in NOTIFY:
            clean, err = pw.validate_website(payload(venue=v, ref="pm-" + v[:3]))
            self.assertIsNone(err, v)
            self.assertEqual(clean["venue"], v)
            self.assertTrue(pw.is_notify_only("Las Vegas", v))

    def test_site_form_names_and_aliases_map(self):
        for raw, want in (("ZOUK Nightclub", "ZOUK"), ("Ghost Bar", "Ghostbar"), ("XS Nightclub", "XS"),
                          ("EBC", "Encore Beach Club"), ("LIV Nightclub", "LIV"), ("LIV Beach", "LIV Beach")):
            clean, err = pw.validate_website(payload(venue=raw, city=""))
            self.assertIsNone(err, raw)
            self.assertEqual(clean["venue"], want, raw)

    def test_sends_one_team_email_only(self):
        from config import rules
        i = intake()
        with mock.patch.object(pw, "send_branded", return_value=(200, {"sent": True})) as sb, \
             mock.patch.object(gmail_client, "send_once") as so, \
             mock.patch.object(queue_service, "persist_intake") as pi, \
             mock.patch.object(pw, "_deliver_website") as dw:
            self.assertEqual(pw.accept_website(i, payload()), (200, "team notified"))
        sb.assert_called_once()
        so.assert_not_called()      # no legacy team/guest send path
        pi.assert_not_called()      # no queue receipt -> no booking job
        dw.assert_not_called()      # no intake insert -> no parse/booking/fallback/guest email
        i.service().users().messages().insert.assert_not_called()
        p = sb.call_args[0][1]
        self.assertEqual(p["to"], rules.PLAYMAKER_EMAIL)
        self.assertEqual(p["to"], "team@playmakerentertainment.com")
        self.assertNotIn("from", p)  # default branded sender (info@)
        for line in ("Guest: TEST Guest", "Email: test@example.com", "Venue: ZOUK", "Female guests: 3", "Male guests: 2"):
            self.assertIn(line, p["text"])
        night = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
        self.assertIn(f"Date: {night}", p["text"])
        for bad in ("602", "555", "0100", "Phone", "jarvis"):
            self.assertNotIn(bad, p["text"] + p["html"] + p["subject"])

    def test_subject_never_matches_intake_search(self):
        clean, _ = pw.validate_website(payload())
        subj = pw.notify_only_subject(clean).lower()
        for phrase in ("new guest list request submission", "guest list request", "new posh signup"):
            self.assertNotIn(phrase, subj)

    def test_dedupes_repeat(self):
        i = intake()
        with mock.patch.object(pw, "send_branded", return_value=(200, {"sent": True})) as sb:
            pw.accept_website(i, payload())
            self.assertEqual(pw.accept_website(i, payload()), (200, "duplicate (this run)"))
        sb.assert_called_once()

    def test_send_failure_returns_503_without_marking(self):
        i = intake()
        with mock.patch.object(pw, "send_branded", return_value=(429, "limit")):
            self.assertEqual(pw.accept_website(i, payload())[0], 503)
        with mock.patch.object(pw, "send_branded", return_value=(200, {"sent": True})) as sb:
            self.assertEqual(pw.accept_website(i, payload()), (200, "team notified"))
        sb.assert_called_once()

    def test_dry_run_sends_nothing(self):
        i = intake(dry=True)
        with mock.patch.object(pw, "send_branded") as sb:
            self.assertEqual(pw.accept_website(i, payload()), (200, "dry run (notify only)"))
        sb.assert_not_called()


class ExistingVenuesUnchanged(unittest.TestCase):
    def test_bookable_venue_still_inserted(self):
        i = intake()
        with mock.patch.object(gmail_client, "send_once", return_value=True), \
             mock.patch.object(pw, "send_branded") as sb:
            self.assertEqual(pw.accept_website(i, payload(venue="OMNIA", ref="pm-om")), (200, "queued"))
        i.service().users().messages().insert.assert_called_once()
        sb.assert_not_called()

    def test_mixed_text_prefers_bookable_venue(self):
        for raw in ("XS, OMNIA", "LIV or Hakkasan", "ZOUK then Marquee"):
            clean, err = pw.validate_website(payload(venue=raw, city=""))
            self.assertIsNone(err)
            self.assertFalse(pw.is_notify_only(clean["city"], clean["venue"]), raw)

    def test_unknown_still_rejected(self):
        self.assertEqual(pw.validate_website(payload(venue="Nope"))[1], "bad city/venue")
        self.assertEqual(pw.validate_website(payload(venue="ZOUK", city="Scottsdale"))[1], "bad city/venue")

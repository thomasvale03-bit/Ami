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

    def test_dayclubs_accepted_and_canonical(self):
        for v in ("Marquee Dayclub", "TAO Beach Dayclub", "OMNIA Dayclub", "Liquid Pool Lounge", "Palm Tree Beach Club"):
            clean, err = pw.validate_website(payload(venue=v))
            self.assertIsNone(err, v)
            raw = gmail_client.parse_request("m", pw.to_website_text(clean))
            self.assertEqual(raw["venues"], [v])

    def test_origins_env(self):
        with mock.patch.dict(os.environ, {"ALLOWED_ORIGINS": "https://a.example, https://b.example/"}):
            self.assertEqual(pw.website_origins(), {"https://a.example", "https://b.example"} | pw.PLAYMAKER_ORIGINS)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pw.website_origins(), {"https://guestlistgo.me", "https://www.guestlistgo.me"} | pw.PLAYMAKER_ORIGINS)


class PlaymakerSource(unittest.TestCase):
    def test_playmaker_parses_as_plain_playmaker_request(self):
        import main
        clean, err = pw.validate_website(payload(source="playmaker", ref="pm-1"))
        self.assertIsNone(err)
        text = pw.to_website_text(clean)
        raw = gmail_client.parse_request("m1", text)
        self.assertNotIn("source", raw)
        self.assertNotIn("brand", raw)
        self.assertIsNone(raw["promoter"])
        self.assertFalse(main.is_guestlistgo(raw))
        self.assertEqual(raw["venues"], ["OMNIA Nightclub"])
        self.assertEqual(raw["start_date"], raw["end_date"])
        self.assertNotIn("GuestlistGo", text)
        self.assertNotIn("guestlistgo", text.lower())

    def test_playmaker_date_range_and_lavo(self):
        d0 = datetime.now(pw.VEGAS).date() + timedelta(days=3)
        clean, err = pw.validate_website(payload(source="playmaker", venue="LAVO Party Brunch",
                                                 date=d0.isoformat(), end_date=(d0 + timedelta(days=2)).isoformat()))
        self.assertIsNone(err)
        raw = gmail_client.parse_request("m", pw.to_website_text(clean))
        self.assertEqual((raw["start_date"], raw["end_date"]), (d0.isoformat(), (d0 + timedelta(days=2)).isoformat()))
        self.assertEqual(raw["venues"], ["LAVO Party Brunch"])

    def test_playmaker_promoter_credited(self):
        clean, _ = pw.validate_website(payload(source="playmaker", promoter="  Jose   Rivera "))
        text = pw.to_website_text(clean)
        self.assertIn("Promoter first name: Jose", text)
        self.assertIn("Promoter last name: Rivera", text)
        raw = gmail_client.parse_request("m", text)
        self.assertEqual(raw["promoter"], "Jose Rivera")
        self.assertNotIn("brand", raw)
        clean, _ = pw.validate_website(payload(source="playmaker", promoter="Mike"))
        self.assertEqual(gmail_client.parse_request("m", pw.to_website_text(clean))["promoter"], "Mike")

    def test_playmaker_blank_or_guestlistgo_promoter_is_none(self):
        for pr in ("", "   ", "GuestlistGo", "guestlist go"):
            clean, _ = pw.validate_website(payload(source="playmaker", promoter=pr))
            raw = gmail_client.parse_request("m", pw.to_website_text(clean))
            self.assertIsNone(raw["promoter"], pr)
            self.assertNotIn("brand", raw)

    def test_guestlistgo_ignores_promoter_field(self):
        clean, _ = pw.validate_website(payload(promoter="Someone"))
        self.assertEqual(gmail_client.parse_request("m", pw.to_website_text(clean))["promoter"], "GuestlistGo")

    def test_bad_ranges(self):
        d0 = datetime.now(pw.VEGAS).date() + timedelta(days=3)
        for e in (d0 - timedelta(days=1), d0 + timedelta(days=14)):
            self.assertEqual(pw.validate_website(payload(source="playmaker", date=d0.isoformat(),
                                                         end_date=e.isoformat()))[1], "end date out of range")

    def test_playmaker_origins_always_allowed(self):
        with mock.patch.dict(os.environ, {"ALLOWED_ORIGINS": "https://guestlistgo.me"}):
            for o in ("https://playmaker-web-production.up.railway.app", "https://playmakerentertainment.com",
                      "https://www.playmakerentertainment.com", "https://guestlistgo.me"):
                self.assertIn(o, pw.website_origins())

    def test_playmaker_team_notice(self):
        svc = mock.MagicMock()
        intake = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
        intake._lock = __import__("threading").Lock()
        intake.service.return_value = svc
        with mock.patch.object(gmail_client, "send_once", return_value=True) as so:
            self.assertEqual(pw.accept_website(intake, payload(source="playmaker", ref="pm-9")), (200, "queued"))
        a, k = so.call_args
        self.assertTrue(a[1].endswith("@playmakerentertainment.com"))
        self.assertTrue(a[3].startswith("New guest list request submission - Playmaker OMNIA "))


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




class ServerToServerAndBrandedFallback(unittest.TestCase):
    def setUp(self):
        pw._web_times.clear()
        pw._branded_times.clear()
        pw._zoho_sent.clear()

    def test_server_to_server_flat_and_airo_payloads_validate(self):
        d0 = (datetime.now(pw.VEGAS).date() + timedelta(days=4)).isoformat()
        d1 = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
        # Flat camelCase payload
        clean, err = pw.validate_website({
            "firstName": "Gia",
            "lastName": "Lee",
            "email": "gia@example.com",
            "phone": "4802875168",
            "zip": "85281",
            "females": 3,
            "males": 1,
            "venues": "Priority: OMNIA (Fri 10/30). Fri 10/30 OMNIA, Sat 10/31 JEWEL, open to other venues",
            "dates": [d0, d1],
            "promoter": "Tom - Grok",
        })
        self.assertIsNone(err)
        self.assertEqual(clean["source"], "playmaker")
        self.assertEqual(clean["name"], "Gia Lee")
        self.assertEqual(clean["city"], "Las Vegas")
        self.assertEqual(clean["venue"], "OMNIA")
        self.assertEqual(clean["night"].isoformat(), d0)
        self.assertEqual(clean["end"].isoformat(), d1)
        self.assertEqual((clean["women"], clean["men"]), (3, 1))
        self.assertEqual(clean["promoter_name"], "Tom - Grok")
        self.assertIn("ZIP: 85281", clean["notes"])

        # PartyEveryDay Airo-shaped payload with Scottsdale open-to-all preference & single-word name
        clean2, err2 = pw.validate_website({
            "conversation": {
                "messages_attributes": [{"body": f"New guest list request submission\n\nFull name: Cher\nAdditional comments: Venues: [Scottsdale] Open to all venues on their dates. PartyEveryDay alert #99"}],
                "data": {
                    "Visit Start Date": d0,
                    "Visit End Date": d0,
                    "Venues": "[Scottsdale] Open to all venues on their dates",
                    "Female Guests": "2",
                    "Male Guests": "0",
                    "Phone": "6025550199",
                    "ZIP Code": "85251",
                    "Promoter First Name": "Tom - Grok",
                    "Promoter Last Name": "",
                    "Authorization Accepted": "YES",
                    "Originating Page": "PartyEveryDay instagram DM via @th3partyplug",
                },
            },
            "user": {"name": "Cher", "email": "cher@example.com"},
        })
        self.assertIsNone(err2)
        self.assertEqual(clean2["name"], "Cher")
        self.assertEqual(clean2["city"], "Scottsdale")
        self.assertEqual(clean2["venue"], "Almost Famous")
        self.assertEqual(clean2["ref"], "ped-99")

    def test_http_accepts_missing_origin_for_server_to_server_but_rejects_bad_origin(self):
        import json, urllib.request, urllib.error
        svc = mock.MagicMock()
        intake = mock.MagicMock(dry_run=True, intake_email="i@example.com", _seen=set())
        intake._lock = __import__("threading").Lock()
        intake.service.return_value = svc
        with mock.patch.dict(os.environ, {"WEBSITE_FORM_ENABLED": "1"}):
            srv = pw.serve(intake, port=0, host="127.0.0.1")
            base = f"http://127.0.0.1:{srv.server_address[1]}/webhooks/website"
            try:
                # Server-to-server (no Origin header) -> 200
                req = urllib.request.Request(base, data=json.dumps(payload(source="playmaker", ref="s2s-1")).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, 200)
                # Evil Origin header -> 403
                req_bad = urllib.request.Request(base, data=json.dumps(payload(source="playmaker", ref="s2s-2")).encode(),
                                                 headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req_bad, timeout=5)
                self.assertEqual(ctx.exception.code, 403)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_send_branded_static_guest_list_subject_does_not_drop_different_guests(self):
        queries = []
        svc = mock.MagicMock()
        def fake_list(userId, q, maxResults):
            queries.append(q)
            return mock.Mock(execute=lambda: {})
        svc.users().messages().list.side_effect = fake_list
        svc.users().messages().send.return_value = mock.Mock(execute=lambda: {"id": "m1"})
        body1 = "Message: New guest list request submission\n\nFull name: Alice Smith\nEmail: alice@example.com\nVisit start date: 2026-10-30\nRequested venues: Omnia Nightclub"
        body2 = "Message: New guest list request submission\n\nFull name: Bob Jones\nEmail: bob@example.com\nVisit start date: 2026-10-31\nRequested venues: TAO Nightclub"
        with mock.patch.object(pw, "zoho_configured", return_value=False):
            c1, r1 = pw.send_branded(svc, {"to": "team@playmakerentertainment.com", "subject": "New guest list request submission", "text": body1, "html": "<p>1</p>"})
            c2, r2 = pw.send_branded(svc, {"to": "team@playmakerentertainment.com", "subject": "New guest list request submission", "text": body2, "html": "<p>2</p>"})
        self.assertEqual((c1, r1.get("sent")), (200, True))
        self.assertEqual((c2, r2.get("sent")), (200, True))
        self.assertIn('"alice@example.com"', queries[0])
        self.assertIn('"bob@example.com"', queries[1])


if __name__ == "__main__":
    unittest.main()

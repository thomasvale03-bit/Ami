"""Assisted sign-up, TicketSauce API skeleton, and helper logic (no network, no browser)."""
import io
import json
import os
import sys
import pathlib
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import assisted_jobs
import main
import tao_portal
import ticketsauce_api
import assisted_signup

URL = "https://tickets.taogroup.com/e/guest-list-omnia-nc-10-17-2026/tickets"
GUEST = {"first_name": "Ana", "last_name": "Ruiz", "email": "ana@example.com", "phone": "7025550100",
         "female_count": 2, "male_count": 1, "billing_zip": "89109"}


class JobTests(unittest.TestCase):
    def test_roundtrip_and_promoter_tag(self):
        job = assisted_jobs.make_job(URL, GUEST, venue="OMNIA Nightclub", date="2026-10-17", ref="m1")
        self.assertIn("utm_source=promoter", job["url"])
        back = assisted_jobs.decode(assisted_jobs.encode(job))
        self.assertEqual(back["email"], "ana@example.com")
        self.assertEqual((back["female_count"], back["male_count"]), (2, 1))

    def test_command_is_one_line_and_decodable(self):
        job = assisted_jobs.make_job(URL, GUEST)
        cmd = assisted_jobs.command(job)
        self.assertNotIn("\n", cmd)
        self.assertTrue(cmd.startswith("python tools/assisted_signup.py --job "))
        self.assertEqual(assisted_jobs.decode(cmd.split("--job ")[1].strip("'"))["last_name"], "Ruiz")

    def test_validation(self):
        with self.assertRaises(ValueError):
            assisted_jobs.make_job(URL, dict(GUEST, email=""))
        with self.assertRaises(ValueError):
            assisted_jobs.make_job(URL, dict(GUEST, female_count=0, male_count=0))
        with self.assertRaises(ValueError):
            assisted_jobs.decode(assisted_jobs.encode(dict(assisted_jobs.make_job(URL, GUEST), v=99)))

    def test_mode_and_recipient(self):
        with mock.patch.dict(os.environ, {"AMY_SIGNUP_MODE": "Assisted", "ASSISTED_SIGNUP_TO": "t@x.com"}):
            self.assertTrue(assisted_jobs.assisted_on())
            self.assertEqual(assisted_jobs.recipient("d@x.com"), "t@x.com")
        with mock.patch.dict(os.environ, {"AMY_SIGNUP_MODE": "", "ASSISTED_SIGNUP_TO": ""}):
            self.assertFalse(assisted_jobs.assisted_on())
            self.assertEqual(assisted_jobs.recipient("d@x.com"), "d@x.com")

    def test_email_has_command_per_job(self):
        jobs = [assisted_jobs.make_job(URL, GUEST, venue="OMNIA Nightclub", date="2026-10-17")] * 2
        subject, body = assisted_jobs.job_email(jobs, "Ana Ruiz")
        self.assertIn("2 nights", subject)
        self.assertEqual(body.count("tools/assisted_signup.py --job"), 2)
        self.assertIn("Nothing has been submitted", body)


class SubmitRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"AMY_SIGNUP_MODE": "assisted", "TICKETSAUCE_CLIENT_ID": "",
                                                "TICKETSAUCE_CLIENT_SECRET": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_assisted_returns_job_without_browser(self):
        with mock.patch.object(tao_portal, "_catalog_urls", side_effect=AssertionError("no browser")):
            r = tao_portal.submit_registration({"listing_url": URL, "event": "GL"}, GUEST)
        self.assertFalse(r["verified"])
        self.assertEqual(r["assisted_job"]["email"], "ana@example.com")

    def test_unsafe_url_still_refused(self):
        r = tao_portal.submit_registration({"listing_url": "https://evil.com/guest-list"}, GUEST)
        self.assertNotIn("assisted_job", r)

    def test_not_assisted_falls_through(self):
        with mock.patch.dict(os.environ, {"AMY_SIGNUP_MODE": ""}):
            self.assertIsNone(tao_portal._api_or_assisted({"listing_url": URL}, GUEST))

    def test_api_preferred_when_creds_and_event_id(self):
        fake = mock.Mock()
        fake.create_registration.return_value = {"confirmation_id": "O1", "verified": True}
        with mock.patch.dict(os.environ, {"TICKETSAUCE_CLIENT_ID": "a", "TICKETSAUCE_CLIENT_SECRET": "b"}), \
                mock.patch.object(ticketsauce_api.Client, "from_env", return_value=fake):
            r = tao_portal.submit_registration({"listing_url": URL, "event_id": "E1"}, GUEST)
        self.assertEqual(r["confirmation_id"], "O1")

    def test_api_not_ready_falls_back_to_assisted(self):
        with mock.patch.dict(os.environ, {"TICKETSAUCE_CLIENT_ID": "a", "TICKETSAUCE_CLIENT_SECRET": "b",
                                          "TICKETSAUCE_REGISTRATION_VERIFIED": ""}):
            r = tao_portal.submit_registration({"listing_url": URL, "event_id": "E1"}, GUEST)
        self.assertIn("assisted_job", r)

    def test_process_request_collects_jobs(self):
        result = {"confirmation_id": None, "verified": False,
                  "assisted_job": assisted_jobs.make_job(URL, GUEST), "reason": "assisted"}
        with mock.patch.object(tao_portal, "submit_registration", return_value=result):
            book_calls = []
            def fake_book_nights(request, today, checker, dayclub_checker, book, exceptions):
                book_calls.append(book("OMNIA Nightclub", {"listing_url": URL}, today, "nightclub"))
            with mock.patch.object(main, "_book_nights", fake_book_nights), \
                    mock.patch.object(main, "normalize_guest_request", return_value=dict(
                        GUEST, start_date="2026-10-17", end_date="2026-10-17", drais=False,
                        requested_venues=[], promoter=None)):
                from datetime import date
                out = main.process_one_request({}, dry_run=False, today=date(2026, 10, 17))
        self.assertEqual(book_calls, [False])
        self.assertEqual(out["assisted_jobs"][0]["venue"], "OMNIA Nightclub")
        self.assertEqual(out["exceptions"], [])

    def test_send_assisted_jobs_dry_run(self):
        job = assisted_jobs.make_job(URL, GUEST, venue="OMNIA Nightclub", date="2026-10-17")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            main.send_assisted_jobs(None, "m1", GUEST, [job], dry_run=True)
        self.assertIn("--job", out.getvalue())

    def test_concierge_jobs_skip_nights_without_links(self):
        nights = [{"date": "2026-10-17", "venue": "OMNIA Nightclub", "signup_url": URL, "dayclub": None},
                  {"date": "2026-10-18", "venue": "TAO Nightclub", "signup_url": None, "dayclub": None}]
        jobs = main.concierge_jobs(GUEST, nights, "m1")
        self.assertEqual([j["date"] for j in jobs], ["2026-10-17"])


class ApiTests(unittest.TestCase):
    def _opener(self, payloads, seen):
        def opener(req, timeout=30):
            seen.append(req)
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps(payloads.pop(0)).encode()
            return resp
        return opener

    def test_token_cached(self):
        seen = []
        c = ticketsauce_api.Client("id", "sec", opener=self._opener([{"access_token": "T"}], seen), clock=lambda: 0)
        self.assertEqual(c.token(), "T")
        self.assertEqual(c.token(), "T")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].full_url.endswith("/v2/oauth/token"))
        self.assertIn(b"grant_type=client_credentials", seen[0].data)

    def test_registration_refused_until_verified(self):
        c = ticketsauce_api.Client("id", "sec", opener=lambda *a, **k: 1 / 0)
        with mock.patch.dict(os.environ, {"TICKETSAUCE_REGISTRATION_VERIFIED": ""}):
            with self.assertRaises(ticketsauce_api.ApiNotReady):
                c.create_registration("E1", GUEST)

    def test_registration_when_verified(self):
        seen = []
        c = ticketsauce_api.Client("id", "sec", opener=self._opener(
            [{"access_token": "T"}, {"order_id": "O9"}], seen), clock=lambda: 0)
        with mock.patch.dict(os.environ, {"TICKETSAUCE_REGISTRATION_VERIFIED": "true"}):
            r = c.create_registration("E1", GUEST)
        self.assertEqual(r["confirmation_id"], "O9")
        self.assertEqual(seen[1].headers["Authorization"], "Bearer T")

    def test_from_env_needs_creds(self):
        with mock.patch.dict(os.environ, {"TICKETSAUCE_CLIENT_ID": "", "TICKETSAUCE_CLIENT_SECRET": ""}):
            with self.assertRaises(ticketsauce_api.ApiNotReady):
                ticketsauce_api.Client.from_env()


class FakePage:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def title(self):
        return ""

    def locator(self, _):
        page = self
        class L:
            def inner_text(self, timeout=None):
                return page.bodies.pop(0) if len(page.bodies) > 1 else page.bodies[0]
        return L()


class HelperTests(unittest.TestCase):
    def test_waits_for_human_then_continues(self):
        page = FakePage(["Just a moment...", "Verify you are human", "Guest List - Female FREE"])
        t = [0]
        ok = assisted_signup.wait_for_human(page, timeout_s=60, clock=lambda: t[0],
                                            sleep=lambda s: t.__setitem__(0, t[0] + s), say=lambda *_: None)
        self.assertTrue(ok)

    def test_times_out_if_never_cleared(self):
        page = FakePage(["Just a moment..."])
        t = [0]
        ok = assisted_signup.wait_for_human(page, timeout_s=10, clock=lambda: t[0],
                                            sleep=lambda s: t.__setitem__(0, t[0] + s), say=lambda *_: None)
        self.assertFalse(ok)

    def test_no_challenge_returns_immediately(self):
        self.assertTrue(assisted_signup.wait_for_human(FakePage(["Form"]), sleep=lambda s: 1 / 0))

    def test_job_from_args(self):
        token = assisted_jobs.encode(assisted_jobs.make_job(URL, GUEST))
        self.assertEqual(assisted_signup.job_from_args(assisted_signup.parse_args(["--job", token]))["first_name"], "Ana")
        args = assisted_signup.parse_args(["--url", URL, "--first", "A", "--last", "B", "--email", "a@b.c", "--female", "1"])
        self.assertEqual(assisted_signup.job_from_args(args)["female_count"], 1)

    def test_show_refuses_foreign_url(self):
        token = assisted_jobs.encode(assisted_jobs.make_job("https://evil.com/guest-list", GUEST))
        with self.assertRaises(SystemExit):
            assisted_signup.main(["--job", token, "--show"])

    def test_no_stealth_code(self):
        src = (pathlib.Path(assisted_signup.__file__)).read_text().lower()
        for banned in ("playwright_stealth", "stealth_sync", "add_init_script", "turnstile", "navigator.", "scrapling",
                       "user_agent=", "--disable-blink-features"):
            self.assertNotIn(banned, src)
        self.assertIn("headless=false", src)


if __name__ == "__main__":
    unittest.main()

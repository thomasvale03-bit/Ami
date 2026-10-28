"""Posh child-event lookup (saved real page fixture, no network)."""
import os
import pathlib
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import main
import gmail_client
import posh
import posh_lookup as pl
import posh_webhook as pw

HTML = (pathlib.Path(__file__).parent / "fixtures" / "posh_event_tao_nc.html").read_text()
TARGET = "6a80e79648e2b4a1334418aa"
SLUG = "guestlist-tao-nc-2026-10-18-8-30"
NOT_FOUND = "<html><title>Posh</title></html>"


def fake_site(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        return pages.get(url.rsplit("/", 1)[1], NOT_FOUND)
    return fetch, calls


def series_payload(**kw):
    p = {"type": "new_order", "account_first_name": "Ana", "account_last_name": "Ruiz",
         "account_email": "ana@example.com", "event_name": "Guestlist | TAO NC", "event_id": TARGET,
         "event_start": "2026-08-15T22:30:00.000Z", "event_end": "2026-08-16T04:30:00.000Z",
         "date_purchased": "2026-10-08T19:00:00.000Z", "order_number": "500",
         "items": [{"name": "Guest List - Female"}]}
    p.update(kw)
    return p


class ParseTests(unittest.TestCase):
    def test_real_page(self):
        self.assertEqual(pl.page_start(HTML), datetime.fromisoformat("2026-10-17T19:30:00-07:00"))
        self.assertEqual(pl.page_slug(HTML), SLUG)
        sib = pl.siblings(HTML)
        self.assertEqual(sib[TARGET], SLUG)
        self.assertEqual(sib["6a80e79648e2b4a13344189e"], "guestlist-tao-nc-2026-10-11-8-30")
        self.assertIsNone(pl.page_start(NOT_FOUND))

    def test_slugify_and_candidates(self):
        self.assertEqual(pl.slugify("Guestlist | TAO NC"), "guestlist-tao-nc")
        pl.reset_cache()
        c = pl.candidate_slugs("Guestlist | TAO NC", datetime(2026, 11, 1, tzinfo=timezone.utc), 0, 0)
        self.assertEqual(c[:2], ["guestlist-tao-nc-2026-11-1-8-30", "guestlist-tao-nc-2026-11-1-9-30"])
        self.assertIn("guest-list-tao-nc-2026-11-1-8-30", c)

    def test_night_after_midnight(self):
        self.assertEqual(pl.night_from_start(datetime.fromisoformat("2026-10-18T01:00:00-07:00")), date(2026, 10, 17))
        self.assertEqual(pl.night_from_start(datetime.fromisoformat("2026-10-18T02:30:00+00:00")), date(2026, 10, 17))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        pl.reset_cache()

    def test_finds_target_via_sibling_and_caches(self):
        # Purchase Oct 8: guesses reach a sibling page (Oct 11 slug); it links to the target.
        sibling = HTML.replace(f'og:url" content="https://posh.vip/e/{SLUG}"',
                               'og:url" content="https://posh.vip/e/guestlist-tao-nc-2026-10-11-8-30"')
        fetch, calls = fake_site({"guestlist-tao-nc-2026-10-11-8-30": sibling, SLUG: HTML})
        bought = datetime(2026, 10, 8, 19, tzinfo=timezone.utc)
        start = pl.real_start(TARGET, "Guestlist | TAO NC", bought, fetch=fetch, sleep=lambda s: None)
        self.assertEqual(start, datetime.fromisoformat("2026-10-17T19:30:00-07:00"))
        self.assertTrue(calls[-1].endswith(SLUG))
        n = len(calls)
        self.assertEqual(pl.real_start(TARGET, "x", bought, fetch=fetch, sleep=lambda s: None), start)
        self.assertEqual(len(calls), n)  # cached

    def test_budget_and_failure(self):
        fetch, calls = fake_site({})
        self.assertIsNone(pl.real_start(TARGET, "Guestlist | TAO NC", datetime(2026, 10, 8, tzinfo=timezone.utc),
                                        fetch=fetch, sleep=lambda s: None, max_fetches=5))
        self.assertEqual(len(calls), 5)

    def test_unknown_id_not_on_page(self):
        fetch, _ = fake_site({SLUG: HTML})
        self.assertIsNone(pl.real_start("0" * 24, "Guestlist | TAO NC", datetime(2026, 10, 17, tzinfo=timezone.utc),
                                        fetch=fetch, sleep=lambda s: None, max_fetches=6))


class WebhookNightTests(unittest.TestCase):
    def test_lookup_gives_real_night(self):
        real = datetime.fromisoformat("2026-10-17T19:30:00-07:00")
        night, how, start = pw.resolve_night(series_payload(), lookup=lambda *a: real)
        self.assertEqual((night, how), (date(2026, 10, 17), "posh_page"))
        raw = posh.parse_signup("m", pw.to_signup_text(series_payload(), night, how, start))
        self.assertEqual(raw["start_date"], "2026-10-17")
        self.assertFalse(pw.needs_review(pw.to_signup_text(series_payload(), night, how, start)))

    def test_lookup_failure_on_series_anchor_needs_review(self):
        night, how, _ = pw.resolve_night(series_payload(), lookup=lambda *a: None)
        self.assertEqual(how, pw.REVIEW_RULE)
        self.assertTrue(pw.needs_review(pw.to_signup_text(series_payload(), night, how)))

    def test_future_one_off_without_lookup_is_not_review(self):
        _, how, _ = pw.resolve_night(series_payload(event_start="2026-10-17T22:30:00Z"), lookup=lambda *a: None)
        self.assertEqual(how, "event_start")

    def test_lookup_exception_falls_back(self):
        def boom(*a):
            raise OSError("down")
        self.assertEqual(pw.resolve_night(series_payload(), lookup=boom)[1], pw.REVIEW_RULE)

    def test_disabled_lookup(self):
        with mock.patch.dict(os.environ, {"POSH_LOOKUP_ENABLED": "false"}), \
                mock.patch.object(pl, "real_start", side_effect=AssertionError):
            self.assertEqual(pw.resolve_night(series_payload())[1], pw.REVIEW_RULE)


class MainReviewTests(unittest.TestCase):
    def test_review_order_goes_to_team_not_jobs(self):
        night, how, _ = pw.resolve_night(series_payload(), lookup=lambda *a: None)
        body = pw.to_signup_text(series_payload(), night, how)
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, body)), \
                mock.patch.object(gmail_client, "get_subject", return_value=pw.SUBJECT), \
                mock.patch.object(gmail_client, "posh_order_already_handled", return_value=False), \
                mock.patch.dict(os.environ, {"POSH_CONSENT_ON_FILE": "true", "AMY_CONCIERGE": "true",
                                             "AMY_SIGNUP_MODE": "assisted"}), \
                mock.patch.object(main, "team_alert") as alert, \
                mock.patch.object(main, "concierge_handoff", side_effect=AssertionError("no jobs")):
            self.assertEqual(main.handle_message(None, "m1", True, {}), gmail_client.EXCEPTION_LABEL)
        self.assertIn("could not be looked up", alert.call_args[0][3][0])


if __name__ == "__main__":
    unittest.main()


HAK_1010 = (pathlib.Path(__file__).parent / "fixtures" / "posh_event_hakkasan_1010.html").read_text()
HAK_1018 = (pathlib.Path(__file__).parent / "fixtures" / "posh_event_hakkasan_1018.html").read_text()
HAK_ID = "6a682ff9374ee034d5cc4b5e"


class HakkasanRegressionTests(unittest.TestCase):
    """Real order 37447941: 'Guest List | Hakkasan', series start 2026-07-29T22:30Z,
    bought 2026-10-10T13:05:22Z. Thomas confirmed the night: Sat Oct 17, 2026.
    Child slugs end in 11-30 (4:30 AM PDT), which the old 8-30/9-30 list missed."""

    def setUp(self):
        pl.reset_cache()

    def test_derived_end_time_from_series_start(self):
        self.assertIn("11-30", pl.derived_end_times("2026-07-29T22:30:00.000Z"))
        self.assertIn("11-30", pl.derived_end_times(None, "2026-07-30T04:30:00.000Z"))

    def test_name_bases(self):
        self.assertEqual(pl.name_bases("Guest List | Hakkasan"), ["guest-list-hakkasan", "guestlist-hakkasan"])
        self.assertEqual(pl.name_bases("Guestlist | TAO NC"), ["guestlist-tao-nc", "guest-list-tao-nc"])

    def test_on_hour_end_tries_both_forms(self):
        times = pl.derived_end_times(None, "2026-07-30T02:00:00Z")
        self.assertIn("9-00", times)
        self.assertIn("9-0", times)

    def test_finds_hakkasan_child_and_night(self):
        fetch, calls = fake_site({"guest-list-hakkasan-2026-10-10-11-30": HAK_1010,
                                  "guest-list-hakkasan-2026-10-18-11-30": HAK_1018})
        bought = datetime(2026, 10, 10, 13, 5, 22, tzinfo=timezone.utc)
        start = pl.real_start(HAK_ID, "Guest List | Hakkasan", bought, fetch=fetch, sleep=lambda s: None,
                              event_start="2026-07-29T22:30:00Z")
        self.assertEqual(start, datetime.fromisoformat("2026-10-17T22:30:00-07:00"))
        self.assertEqual(pl.night_from_start(start), date(2026, 10, 17))
        self.assertLessEqual(len(calls), 5)

    def test_webhook_end_to_end(self):
        fetch, _ = fake_site({"guest-list-hakkasan-2026-10-10-11-30": HAK_1010,
                              "guest-list-hakkasan-2026-10-18-11-30": HAK_1018})
        payload = series_payload(event_name="Guest List | Hakkasan", event_id=HAK_ID, order_number="37447941",
                                 event_start="2026-07-29T22:30:00Z", event_end=None,
                                 date_purchased="2026-10-10T13:05:22Z")
        look = lambda *a, **k: pl.real_start(*a, fetch=fetch, sleep=lambda s: None, **k)
        night, how, start = pw.resolve_night(payload, lookup=look)
        self.assertEqual((night, how), (date(2026, 10, 17), "posh_page"))
        raw = posh.parse_signup("m", pw.to_signup_text(payload, night, how, start))
        self.assertEqual(raw["start_date"], "2026-10-17")
        self.assertEqual(raw["venues"], ["Hakkasan Nightclub"])

    def test_old_tao_case_still_found_within_cap(self):
        sibling = HTML.replace(f'og:url" content="https://posh.vip/e/{SLUG}"',
                               'og:url" content="https://posh.vip/e/guestlist-tao-nc-2026-10-11-8-30"')
        fetch, calls = fake_site({"guestlist-tao-nc-2026-10-11-8-30": sibling, SLUG: HTML})
        start = pl.real_start(TARGET, "Guestlist | TAO NC", datetime(2026, 10, 8, 19, tzinfo=timezone.utc),
                              fetch=fetch, sleep=lambda s: None, event_start="2026-08-15T22:30:00.000Z",
                              event_end="2026-08-16T04:30:00.000Z")
        self.assertEqual(pl.night_from_start(start), date(2026, 10, 17))
        self.assertLessEqual(len(calls), 40)


HAK_1022 = (pathlib.Path(__file__).parent / "fixtures" / "posh_event_hakkasan_1022.html").read_text()
RBAE_ID = "6a682ff9374ee034d5cc4b61"


class RenamedChildTests(unittest.TestCase):
    """Real order 37448164: 'R&BAE | Hakkasan' is a renamed child of the
    'Guest List | Hakkasan' series; its slug keeps the series name
    (guest-list-hakkasan-2026-10-22-11-30). Thomas: night is Wed Oct 21."""

    def setUp(self):
        pl.reset_cache()

    def test_ampersand_variants(self):
        self.assertEqual(pl.name_bases("R&BAE | Hakkasan"),
                         ["r-bae-hakkasan", "rbae-hakkasan", "r-and-bae-hakkasan"])

    def test_series_key_and_seed(self):
        self.assertEqual(pl.series_key(RBAE_ID), pl.series_key(HAK_ID))
        self.assertEqual(pl.series_bases(RBAE_ID), ["guest-list-hakkasan"])
        self.assertEqual(pl.series_bases("ffffffffffffffffffffffff"), [])

    def test_env_seed(self):
        with mock.patch.dict(os.environ, {"POSH_SERIES_SEEDS": "abcdefabcdefabcdef12=marquee-gl:8-0/9-0"}):
            self.assertEqual(pl.series_bases("abcdefabcdefabcdef123456"), ["marquee-gl"])

    def test_learns_series_from_pages(self):
        pl._learn(pl.siblings(HAK_1010))
        self.assertIn("guest-list-hakkasan", pl._series[pl.series_key(HAK_ID)])

    def test_renamed_child_found_via_seed(self):
        fetch, calls = fake_site({"guest-list-hakkasan-2026-10-10-11-30": HAK_1010,
                                  "guest-list-hakkasan-2026-10-22-11-30": HAK_1022})
        start = pl.real_start(RBAE_ID, "R&BAE | Hakkasan", datetime(2026, 10, 10, 13, 13, 57, tzinfo=timezone.utc),
                              fetch=fetch, sleep=lambda s: None, event_start="2026-07-29T22:30:00Z")
        self.assertEqual(start, datetime.fromisoformat("2026-10-21T22:30:00-07:00"))
        self.assertEqual(pl.night_from_start(start), date(2026, 10, 21))
        self.assertLessEqual(len(calls), 6)
        self.assertFalse(any("r-bae" in c for c in calls))

    def test_renamed_child_without_seed_uses_learned_series(self):
        with mock.patch.object(pl, "seeds", return_value={}):
            pl._learn(pl.siblings(HAK_1010))  # an earlier Hakkasan order taught us the series
            fetch, _ = fake_site({"guest-list-hakkasan-2026-10-10-11-30": HAK_1010,
                                  "guest-list-hakkasan-2026-10-22-11-30": HAK_1022})
            pl._starts.clear()
            pl._slugs.clear()
            start = pl.real_start(RBAE_ID, "R&BAE | Hakkasan", datetime(2026, 10, 10, 13, 13, 57, tzinfo=timezone.utc),
                                  fetch=fetch, sleep=lambda s: None)
        self.assertEqual(pl.night_from_start(start), date(2026, 10, 21))

    def test_other_series_page_does_not_stop_search(self):
        # A TAO page found first (different series) must not end the search.
        fetch, _ = fake_site({"guest-list-hakkasan-2026-10-10-11-30": HTML,
                              "guest-list-hakkasan-2026-10-11-11-30": HAK_1010,
                              "guest-list-hakkasan-2026-10-22-11-30": HAK_1022})
        with mock.patch.object(pl, "seeds", return_value={}):
            start = pl.real_start(RBAE_ID, "Guest List | Hakkasan",
                                  datetime(2026, 10, 10, 13, tzinfo=timezone.utc), fetch=fetch, sleep=lambda s: None,
                                  event_start="2026-07-29T22:30:00Z")
        self.assertEqual(pl.night_from_start(start), date(2026, 10, 21))


class OmniaAndCarouselExtrapolationTests(unittest.TestCase):
    def setUp(self):
        pl.reset_cache()

    def test_omnia_series_seed_and_8_0_end_time(self):
        self.assertEqual(pl.series_bases("6acab072b53cd5b9c2c7882f"), ["guestlist-omnia"])
        self.assertIn("8-0", pl.DEFAULT_END_TIMES[:4])
        self.assertIn("9-0", pl.DEFAULT_END_TIMES[:4])

    def test_single_page_without_carousel_uses_event_id(self):
        lavo_html = (
            '<html><meta property="og:url" content="https://posh.vip/e/lavo-brunch-party-guest-list-2026-11-1-1-0"/>'
            '<script>{"eventId":"69733fedd067ffc440dc9359","startDate":"2026-10-31T12:00:00-07:00"}</script></html>'
        )
        sib = pl.siblings(lavo_html)
        self.assertEqual(sib.get("69733fedd067ffc440dc9359"), "lavo-brunch-party-guest-list-2026-11-1-1-0")

    def test_extrapolates_beyond_carousel(self):
        found = {
            "6acab072b53cd5b9c2c78832": "guestlist-omnia-2026-10-30-8-0",
            "6acab072b53cd5b9c2c78835": "guestlist-omnia-2026-10-31-8-0",
            "6acab072b53cd5b9c2c78838": "guestlist-omnia-2026-11-1-8-0",
            "6acab072b53cd5b9c2c7883b": "guestlist-omnia-2026-11-4-9-0",
            "6acab072b53cd5b9c2c78859": "guestlist-omnia-2026-11-21-9-0",
            "6acab072b53cd5b9c2c7885c": "guestlist-omnia-2026-11-22-9-0",
        }
        pl._learn(found)
        ext = pl._extrapolate_beyond_carousel(found, "6acab072b53cd5b9c2c78868", "6acab072b53cd5b9c2")
        self.assertIn("guestlist-omnia-2026-11-29-9-0", ext)

    def test_review_order_notifies_amyi_when_not_dry_run(self):
        import amyi
        night, how, _ = pw.resolve_night(series_payload(), lookup=lambda *a: None)
        body = pw.to_signup_text(series_payload(), night, how)
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, body)), \
                mock.patch.object(gmail_client, "get_subject", return_value=pw.SUBJECT), \
                mock.patch.object(gmail_client, "posh_order_already_handled", return_value=False), \
                mock.patch.dict(os.environ, {"POSH_CONSENT_ON_FILE": "true", "AMY_CONCIERGE": "true"}), \
                mock.patch.object(amyi, "notify") as notify_mock:
            self.assertEqual(main.handle_message(None, "m1", False, {}), gmail_client.EXCEPTION_LABEL)
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "posh_exception")

    def test_on_accept_callback_fires_on_posh_and_website_intake(self):
        import tempfile
        from datetime import datetime, timedelta
        called = []
        svc = mock.MagicMock()
        svc.users().messages().list.return_value = mock.Mock(execute=lambda: {})
        svc.users().messages().insert.return_value = mock.Mock(execute=lambda: {"id": "m1"})
        intake = pw.Intake(lambda: svc, "intake@example.com", lookup=lambda *a: None, on_accept=lambda: called.append("woke"))
        p = dict(series_payload(), event_start="2026-10-31T22:30:00.000Z", order_number="999999")
        self.assertEqual(intake.accept(p), (200, "queued"))
        self.assertEqual(called, ["woke"])
        # Duplicate does not re-trigger on_accept
        self.assertEqual(intake.accept(p), (200, "duplicate (this run)"))
        self.assertEqual(called, ["woke"])
        # Website accept also triggers on_accept
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AMY_PENDING_DIR": tmp}), \
                mock.patch.object(gmail_client, "send_once", return_value=True):
            night = (datetime.now(pw.VEGAS).date() + timedelta(days=5)).isoformat()
            web_p = {"source": "guestlistgo", "first_name": "Real", "last_name": "Guest",
                     "email": "real@gmail.com", "phone": "(602) 555-0100", "city": "Las Vegas",
                     "venue": "OMNIA", "date": night, "women": 2, "men": 1,
                     "age_21_confirmed": True, "ref": "gg-wake-1"}
            self.assertEqual(pw.accept_website(intake, web_p), (200, "queued"))
        self.assertEqual(called, ["woke", "woke"])

    def test_all_exception_branches_notify_amyi(self):
        import amyi
        svc = mock.MagicMock()
        # 1) _missing_required
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, "Empty body")), \
                mock.patch.object(gmail_client, "get_subject", return_value="New guest list request submission"), \
                mock.patch.object(gmail_client, "is_own_team_copy", return_value=False), \
                mock.patch.object(main, "team_alert"), \
                mock.patch.object(amyi, "notify") as notify_mock:
            self.assertEqual(main.handle_message(svc, "m_missing", False, {}), gmail_client.EXCEPTION_LABEL)
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "exception")

        # 2) venue_problem == "unknown"
        unknown_venue_raw = {
            "name": "Real Guest", "email": "real@gmail.com", "phone": "7025550199",
            "venues": ["Space Station Club"], "start_date": "2026-11-10", "end_date": "2026-11-10",
            "female_count": "2", "male_count": "0", "authorized": True,
        }
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, "body")), \
                mock.patch.object(gmail_client, "get_subject", return_value="Subj"), \
                mock.patch.object(gmail_client, "is_own_team_copy", return_value=False), \
                mock.patch.object(gmail_client, "parse_request", return_value=unknown_venue_raw), \
                mock.patch.object(main, "team_alert"), \
                mock.patch.object(amyi, "notify") as notify_mock:
            self.assertEqual(main.handle_message(svc, "m_unknown", False, {}), gmail_client.EXCEPTION_LABEL)
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "exception")

        # 3) process_one_request returns status == "action_needed"
        valid_raw = {
            "name": "Real Guest", "email": "real@gmail.com", "phone": "7025550199",
            "venues": ["OMNIA Nightclub"], "start_date": "2026-11-10", "end_date": "2026-11-10",
            "female_count": "2", "male_count": "0", "authorized": True,
        }
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, "body")), \
                mock.patch.object(gmail_client, "get_subject", return_value="Subj"), \
                mock.patch.object(gmail_client, "is_own_team_copy", return_value=False), \
                mock.patch.object(gmail_client, "parse_request", return_value=valid_raw), \
                mock.patch.object(gmail_client, "set_labels"), \
                mock.patch.object(main, "concierge_on", return_value=False), \
                mock.patch.object(main, "send_links_on", return_value=False), \
                mock.patch.object(main, "manual_booking_on", return_value=False), \
                mock.patch.object(main, "process_one_request", return_value={"status": "action_needed", "issue": "Bad date", "required_action": "Fix"}), \
                mock.patch.object(main, "team_alert"), \
                mock.patch.object(amyi, "notify") as notify_mock:
            self.assertEqual(main.handle_message(svc, "m_act", False, {}), gmail_client.EXCEPTION_LABEL)
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "exception")

        # 4) process_one_request returns action_needed_records
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, "body")), \
                mock.patch.object(gmail_client, "get_subject", return_value="Subj"), \
                mock.patch.object(gmail_client, "is_own_team_copy", return_value=False), \
                mock.patch.object(gmail_client, "parse_request", return_value=valid_raw), \
                mock.patch.object(gmail_client, "set_labels"), \
                mock.patch.object(main, "concierge_on", return_value=False), \
                mock.patch.object(main, "send_links_on", return_value=False), \
                mock.patch.object(main, "manual_booking_on", return_value=False), \
                mock.patch.object(main, "process_one_request", return_value={
                    "status": "ok",
                    "request": {"first_name": "Real", "last_name": "Guest", "email": "real@gmail.com"},
                    "action_needed_records": ["TAO error"],
                }), \
                mock.patch.object(main, "team_alert"), \
                mock.patch.object(amyi, "notify") as notify_mock:
            self.assertEqual(main.handle_message(svc, "m_rec", False, {}), gmail_client.EXCEPTION_LABEL)
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "exception")

        # 5) run_once uncaught exception branch
        with mock.patch.object(gmail_client, "list_pending_requests", return_value=[{"id": "m_crash"}]), \
                mock.patch.object(main, "handle_message", side_effect=RuntimeError("boom")), \
                mock.patch.object(gmail_client, "set_labels"), \
                mock.patch.object(main, "team_alert"), \
                mock.patch.object(amyi, "notify") as notify_mock:
            main.run_once(svc, "live", {})
            notify_mock.assert_called_once()
            self.assertEqual(notify_mock.call_args[0][0], "exception")

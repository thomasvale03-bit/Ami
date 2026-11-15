"""Posh lookup on the Nov 2026 page format (no sibling-date list) and the
queue-mode held-signup alert. No network."""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import main
import posh_lookup as pl

NOT_FOUND = "<html><title>Posh</title></html>"


def current_page(event_id, slug, start_iso):
    """Mimics today's posh.vip RSC page: escaped JSON-LD and eventId, og:url,
    and NO '"<id>",{"href":"/e/<slug>"' sibling carousel."""
    return (
        '<html><head><meta property="og:url" content="https://posh.vip/e/%s"/></head><body>'
        '<script>self.__next_f.push([1,"{\\"eventId\\":\\"%s\\",\\"start\\":\\"x\\"}"])</script>'
        '<script>self.__next_f.push([1,"4c:{\\n  \\"@type\\": \\"Event\\",\\n  \\"startDate\\": \\"%s\\",'
        '\\n  \\"endDate\\": \\"x\\"}"])</script></body></html>' % (slug, event_id, start_iso))


def fake_site(pages):
    calls = []

    def fetch(url):
        calls.append(url.rsplit("/", 1)[1])
        return pages.get(url.rsplit("/", 1)[1], NOT_FOUND)
    return fetch, calls


def run(event_id, name, bought, pages, **kw):
    fetch, calls = fake_site(pages)
    start = pl.real_start(event_id, name, bought, fetch=fetch, sleep=lambda s: None, **kw)
    return start, calls


DESEO_TARGET = "6aae53d9c6a734a4ef0cf3cf"
LAVO = "69733fedd067ffc440dc93"


def lavo_pages():
    # Saturday brunches: Nov 14 ...61, Nov 21 ...65, Nov 28 ...69, Dec 5 ...6d, Dec 12 ...71, Dec 19 ...75
    out = {}
    for i, (sun, sat) in enumerate([("11-15", "11-14"), ("11-22", "11-21"), ("11-29", "11-28"),
                                    ("12-6", "12-05"), ("12-13", "12-12"), ("12-20", "12-19")]):
        m, d = sun.split("-")
        slug = f"lavo-brunch-party-guest-list-2026-{int(m)}-{int(d)}-2-0"
        out[slug] = current_page(LAVO + "%02x" % (0x61 + 4 * i), slug, f"2026-{sat}T12:00:00-08:00")
    return out


class CurrentPageFormat(unittest.TestCase):
    def setUp(self):
        pl.reset_cache()

    def test_parser_reads_current_page(self):
        slug = "deseo-omnia-2026-11-16-12-0"
        html = current_page(DESEO_TARGET, slug, "2026-11-15T22:30:00-08:00")
        self.assertEqual(pl.page_event_id(html), DESEO_TARGET)
        self.assertEqual(pl.page_slug(html), slug)
        self.assertEqual(pl.page_start(html).isoformat(), "2026-11-15T22:30:00-08:00")
        self.assertEqual(pl.siblings(html), {DESEO_TARGET: slug})  # no carousel any more

    def test_deseo_12_0_url(self):
        slug = "deseo-omnia-2026-11-16-12-0"
        pages = {slug: current_page(DESEO_TARGET, slug, "2026-11-15T22:30:00-08:00")}
        bought = datetime(2026, 11, 15, 7, 16, tzinfo=timezone.utc)  # Posh #40822611
        start, calls = run(DESEO_TARGET, "DESEO: OMNIA", bought, pages,
                           event_start="2026-09-27T22:30:00.000Z", event_end="2026-09-28T04:00:00.000Z")
        self.assertEqual(pl.night_from_start(start).isoformat(), "2026-11-15")
        self.assertEqual(calls[0], slug)  # found on the first fetch

    def test_lavo_five_weeks_ahead(self):
        bought = datetime(2026, 11, 15, 6, 26, tzinfo=timezone.utc)  # Posh #40820856
        start, calls = run(LAVO + "75", "LAVO - Brunch Party - Guest List", bought, lavo_pages(),
                           event_start="2026-01-24T12:00:00.000Z", event_end="2026-01-24T18:00:00.000Z")
        self.assertEqual(pl.night_from_start(start).isoformat(), "2026-12-19")
        self.assertLessEqual(len(calls), 8)

    def test_lavo_three_weeks_ahead(self):
        bought = datetime(2026, 11, 13, 23, 40, tzinfo=timezone.utc)  # Posh #40718768
        start, _ = run(LAVO + "6d", "LAVO - Brunch Party - Guest List", bought, lavo_pages())
        self.assertEqual(pl.night_from_start(start).isoformat(), "2026-12-05")

    def test_weekly_probe_without_seeded_weekday(self):
        with mock.patch.dict(os.environ, {"POSH_SERIES_SEEDS": "aaaaaaaaaaaaaaaaaa=test-series:9-0"}):
            pages = {}
            for i, d in enumerate((14, 21, 28)):
                slug = f"test-series-2026-11-{d}-9-0"
                pages[slug] = current_page("aaaaaaaaaaaaaaaaaa%06x" % i, slug, f"2026-11-{d - 1}T22:00:00-08:00")
            start, _ = run("aaaaaaaaaaaaaaaaaa000002", "Something Renamed", datetime(2026, 11, 13, tzinfo=timezone.utc),
                           pages, max_fetches=1000)
            self.assertEqual(start.isoformat(), "2026-11-27T22:00:00-08:00")

    def test_unknown_child_gives_none(self):
        bought = datetime(2026, 11, 15, 6, 26, tzinfo=timezone.utc)
        start, calls = run(LAVO + "ff", "LAVO - Brunch Party - Guest List", bought, lavo_pages(), max_fetches=30)
        self.assertIsNone(start)
        self.assertLessEqual(len(calls), 30)


class HeldSignupAlert(unittest.TestCase):
    RAW = {"name": "Samantha Arreola", "first_name": "Samantha", "last_name": "Arreola",
           "email": "sam@example.com", "phone": "+14425550101", "venues": ["OMNIA Nightclub"],
           "start_date": "2026-11-15", "end_date": "2026-11-15", "female_count": "1", "male_count": "0",
           "source": "Posh", "posh": {"order_number": "40822611", "event_name": "DESEO: OMNIA",
                                      "phone": "+14425550101"},
           "_action_needed": ["Posh recurring event: the night is a guess. Phone +14425550101",
                              "Check the order in Posh."]}

    def send(self, row, **env):
        sent = []

        def fake(service, mid, to, subject, text, html, dedupe_query=None):
            sent.append({"to": to, "subject": subject, "text": text, "html": html, "dq": dedupe_query})
            return True
        with mock.patch.dict(os.environ, env), mock.patch.object(main, "_send_branded_or_gmail", fake):
            out = main.held_signup_alert(None, "1a1dref", dict(self.RAW), row, since=100.0)
        return out, sent

    def test_held_job_alerts_team_once_without_phone_even_in_concierge(self):
        row = {"state": "manual", "reason": "Source request requires review; do not guess", "created": 100.5}
        out, sent = self.send(row, AMY_CONCIERGE="true")
        self.assertTrue(out)
        self.assertEqual(len(sent), 1)
        m = sent[0]
        self.assertEqual(m["to"], "team@playmakerentertainment.com")
        self.assertNotIn("jarvis", (m["to"] + m["text"]).lower())
        for body in (m["text"], m["html"]):
            self.assertNotIn("5550101", body)
            self.assertNotIn("Phone", body)
        self.assertIn("AMY ref: 1a1dref", m["text"])
        self.assertIn("Posh order 40822611", m["text"])
        self.assertIn('"AMY ref: 1a1dref"', m["dq"])

    def test_queued_or_preexisting_or_legacy_jobs_do_not_alert(self):
        self.assertEqual(self.send({"state": "queued", "reason": "", "created": 101})[1], [])
        self.assertEqual(self.send({"state": "manual", "reason": "x", "created": 50})[1], [])
        self.assertEqual(self.send({"state": "manual", "reason": "Legacy job completed; do not rebook",
                                    "created": 101})[1], [])

    def test_queue_branch_sends_alert_for_held_signup(self):
        import time
        import gmail_client
        from tests.test_scottsdale import FORM
        sent = []
        row = {"state": "manual", "reason": "Past or invalid source date; never move to tonight",
               "created": time.time() + 5}
        with mock.patch.dict(os.environ, {"AMY_QUEUE_ENABLED": "true", "AMY_CONCIERGE": "true"}), \
             mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, FORM)), \
             mock.patch.object(gmail_client, "website_ref_already_handled", return_value=False), \
             mock.patch.object(main.queue_service, "enqueue", return_value=row), \
             mock.patch.object(main, "_send_branded_or_gmail",
                               side_effect=lambda *a, **k: sent.append((a, k)) or True):
            out = main.handle_message(mock.MagicMock(), "m-held", dry_run=False, labels={})
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        self.assertEqual(len(sent), 1)
        a, _k = sent[0]
        self.assertEqual(a[2], "team@playmakerentertainment.com")
        self.assertNotIn("Phone", a[4])
        self.assertIn("AMY ref: m-held", a[4])


if __name__ == "__main__":
    unittest.main()

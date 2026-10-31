"""Oct 30 2026 fixes: wake rule (only same Posh order dedupes), Karima's
after-hours night lookup, and the pending-website KeyError 'payload'."""
import json, logging, os, tempfile, unittest
from datetime import date
from unittest import mock

import amyi
import posh_webhook


class WakeRule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posts = []
        mock.patch.dict(os.environ, {"AMYI_DEDUPE_FILE": os.path.join(self.tmp, "s.json")}).start()
        mock.patch.object(amyi, "_sent", None).start()
        mock.patch.object(amyi, "disabled", return_value=False).start()
        mock.patch.object(amyi, "_post", side_effect=lambda pl: (self.posts.append(pl), amyi.record_sent(pl))).start()
        mock.patch("threading.Thread", side_effect=lambda target, args, daemon: mock.Mock(start=lambda: target(*args))).start()
        self.addCleanup(mock.patch.stopall)

    def ping(self, order, **kw):
        base = dict(name="Karima Hardwick", email="guest@gmail.com", venue="Drai's",
                    dates="2026-10-30", source="posh", ref=order, order=order)
        base.update(kw)
        return amyi.notify("posh_guest", **base)

    def test_two_orders_same_guest_both_wake(self):
        with self.assertLogs("amy.amyi", logging.INFO) as cm:
            self.assertTrue(self.ping("90000001"))
            self.assertTrue(self.ping("90000002"))
        self.assertEqual([p["order"] for p in self.posts], ["90000001", "90000002"])
        for line in cm.output:
            print(line)

    def test_same_order_retry_dedupes(self):
        self.assertTrue(self.ping("90000003"))
        self.assertFalse(self.ping("90000003"))
        self.assertEqual(len(self.posts), 1)

    def test_website_always_wakes(self):
        for _ in range(3):
            self.assertTrue(amyi.notify("contact_form", name="Ana Real", email="ana@gmail.com",
                                        venue="TAO", dates="2026-10-31", ref="pm-1", source="website"))
        self.assertEqual(len(self.posts), 3)


class KarimaNight(unittest.TestCase):
    def test_after_hours_utc_anchor_resolves_to_purchase_night(self):
        night, how = posh_webhook.posh_night("2026-10-29T23:30:00.000Z", "2026-10-30T23:38:00.000Z",
                                             event_name="Guestlist | Drai's After Hours")
        self.assertEqual(night, date(2026, 10, 30))
        self.assertNotEqual(how, posh_webhook.REVIEW_RULE)

    def test_resolve_night_not_review(self):
        p = {"event_start": "2026-10-29T23:30:00.000Z", "date_purchased": "2026-10-30T23:38:00.000Z",
             "event_name": "Guestlist | Drai's After Hours", "order_number": "39627027"}
        night, how, _ = posh_webhook.resolve_night(p, lookup=lambda *a, **k: None)
        self.assertEqual((night, how), (date(2026, 10, 30), "late_night_purchase_night"))

    def test_purchase_after_midnight_is_previous_night(self):
        night, _ = posh_webhook.posh_night("2026-10-29T23:30:00.000Z", "2026-10-31T09:00:00.000Z",
                                           event_name="Drai's After Hours")  # 2 AM PDT Sat
        self.assertEqual(night, date(2026, 10, 30))


class PendingKeyError(unittest.TestCase):
    def setUp(self):
        posh_webhook._warned_pending.clear()

    def test_jobs_ledger_and_bad_files_skipped_without_keyerror(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "jobs.json"), "w") as fh:
            json.dump({"r1": {"ref": "r1"}}, fh)
        with open(os.path.join(d, "x.json"), "w") as fh:
            json.dump({"key": ["a"]}, fh)
        intake = mock.Mock(_lock=mock.MagicMock(), _seen=set())
        with mock.patch.object(posh_webhook, "pending_dir", return_value=d), \
             self.assertLogs("amy", logging.WARNING) as cm:
            self.assertEqual(posh_webhook.retry_pending_website(intake), (0, 0))
            self.assertEqual(posh_webhook.retry_pending_website(intake), (0, 0))
        self.assertEqual(len(cm.output), 1)  # x.json warned once; jobs.json silent
        self.assertTrue(os.path.exists(os.path.join(d, "jobs.json")))
        self.assertTrue(os.path.exists(os.path.join(d, "x.json")))

    def test_restores_ledger_from_bad(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "jobs.json.bad"), "w") as fh:
            json.dump({"old": {"ref": "old"}, "r2": {"ref": "r2", "v": 1}}, fh)
        with open(os.path.join(d, "jobs.json"), "w") as fh:
            json.dump({"r2": {"ref": "r2", "v": 2}}, fh)
        with mock.patch.dict(os.environ, {"AMY_JOBS_FILE": os.path.join(d, "jobs.json")}):
            posh_webhook._restore_jobs_ledger(d)
        rows = json.load(open(os.path.join(d, "jobs.json")))
        self.assertEqual(set(rows), {"old", "r2"})
        self.assertEqual(rows["r2"]["v"], 2)
        self.assertFalse(os.path.exists(os.path.join(d, "jobs.json.bad")))

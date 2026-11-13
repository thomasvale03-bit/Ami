import os
import tempfile
import unittest
from unittest import mock

import amyi


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        amyi._sent = None
        self.env = mock.patch.dict(os.environ, {"AMYI_DEDUPE_FILE": os.path.join(self.tmp.name, "s.json"),
                                                "AMYI_WEBHOOK_URL": "http://127.0.0.1:9/never"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        amyi._sent = None
        self.tmp.cleanup()

    def test_disabled_under_tests(self):
        self.assertTrue(amyi.disabled())
        with mock.patch.object(amyi, "_post") as post:
            self.assertFalse(amyi.notify("contact_form", "Real Person", "real@gmail.com", ref="x"))
        post.assert_not_called()

    def _live(self, **kw):
        with mock.patch.object(amyi, "disabled", return_value=False), \
             mock.patch.object(amyi, "_post", side_effect=amyi.record_sent) as post:
            out = amyi.notify(**kw, wait=True)
        return out, post

    def test_test_traffic_skipped(self):
        for kw in ({"name": "Jane Sample", "email": "jane.sample@example.com"},
                   {"name": "Test User", "email": "test@example.com"},
                   {"name": "Bob", "email": ""}, {"name": "Bob", "email": "bob@foo.test"},
                   {"name": "Sample Guest", "email": "a@gmail.com"},
                   {"name": "Bob", "email": "bob@gmail.com", "test": True}):
            out, post = self._live(type="contact_form", ref="r", **kw)
            self.assertFalse(out, kw)
            post.assert_not_called()

    def test_dedupe_only_same_posh_order_persisted(self):
        kw = dict(type="posh_guest", name="Bob Smith", email="bob@gmail.com", venue="JEWEL", dates="2026-10-17")
        self.assertTrue(self._live(ref="77", order="77", **kw)[0])
        self.assertFalse(self._live(ref="77", order="77", **kw)[0])  # Posh retry
        amyi._sent = None  # reload from disk
        self.assertFalse(self._live(ref="77", order="77", **kw)[0])
        self.assertTrue(self._live(ref="78", order="78", **kw)[0])  # same guest, new order

    def test_dedupe_expires(self):
        p = amyi.build_payload("x", "Bob", "b@gmail.com", ref="1", order="1")
        self.assertTrue(amyi.claim(p, now=1000))
        self.assertFalse(amyi.claim(p, now=1000 + 3600))
        self.assertTrue(amyi.claim(p, now=1000 + amyi.DEDUPE_SECONDS + 1))

    def _jobs(self):
        import job_tracker
        os.environ["AMY_JOBS_FILE"] = os.path.join(self.tmp.name, "jobs.json")
        return job_tracker

    def test_retries_with_backoff_then_releases_claim(self):
        jt = self._jobs()
        jt.record("r9", guest="Bob", email="bob@gmail.com", dates=["2027-01-01"])
        p = amyi.build_payload("signup_job", "Bob", "bob@gmail.com", dates="2027-01-01", ref="r9")
        sleeps = []
        with mock.patch.object(amyi, "_post_once", return_value=(503, "busy")) as po:
            self.assertFalse(amyi.deliver(p, _sleep=sleeps.append))
        self.assertEqual(po.call_count, 4)
        self.assertEqual(sleeps, [5, 30, 120])
        self.assertFalse(amyi.is_duplicate(p))  # no claim after failure
        row = jt.get("r9")
        self.assertEqual((row["webhook_status"], row["attempts"], row["failures"]), (503, 4, 1))

    def test_claim_written_only_after_2xx(self):
        jt = self._jobs()
        jt.record("r10", guest="Bob", email="bob@gmail.com")
        p = amyi.build_payload("signup_job", "Bob", "bob@gmail.com", dates="2027-01-01", ref="r10", order="o10")
        with mock.patch.object(amyi, "_post_once", side_effect=[(None, "URLError"), (200, "ok")]):
            self.assertTrue(amyi.deliver(p, _sleep=lambda s: None))
        self.assertTrue(amyi.is_duplicate(p))
        self.assertEqual(jt.get("r10")["webhook_status"], 200)

    def test_logs_real_status_and_body(self):
        p = amyi.build_payload("x", "Bob", "b@gmail.com", ref="r11")
        with mock.patch.object(amyi, "_post_once", return_value=(201, "queued-abc")), \
             self.assertLogs("amy.amyi", "INFO") as cm:
            amyi.deliver(p, _sleep=lambda s: None)
        self.assertTrue(any("HTTP 201" in m and "queued-abc" in m for m in cm.output))

    def test_http_error_status_captured(self):
        import io, urllib.error
        err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"bad auth"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(amyi._post_once({"type": "x"}), (401, "bad auth"))


if __name__ == "__main__":
    unittest.main()

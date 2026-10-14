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
             mock.patch.object(amyi, "_post") as post:
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

    def test_dedupe_by_ref_and_event_persisted(self):
        kw = dict(type="posh_guest", name="Bob Smith", email="bob@gmail.com", venue="JEWEL", dates="2026-10-17")
        self.assertTrue(self._live(ref="77", **kw)[0])
        self.assertFalse(self._live(ref="77", **kw)[0])
        amyi._sent = None  # reload from disk
        self.assertFalse(self._live(ref="78", **kw)[0])  # same email+venue+date
        self.assertTrue(self._live(**dict(kw, dates="2026-10-18"), ref="79")[0])

    def test_dedupe_expires(self):
        p = amyi.build_payload("x", "Bob", "b@gmail.com", ref="1")
        self.assertTrue(amyi.claim(p, now=1000))
        self.assertFalse(amyi.claim(p, now=1000 + 3600))
        self.assertTrue(amyi.claim(p, now=1000 + amyi.DEDUPE_SECONDS + 1))

    def test_single_attempt(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError) as uo:
            amyi._post({"type": "x"})
        self.assertEqual(uo.call_count, 1)


if __name__ == "__main__":
    unittest.main()

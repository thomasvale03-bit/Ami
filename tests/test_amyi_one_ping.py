import os, tempfile, unittest
from unittest import mock
import amyi


class OnePingPerJob(unittest.TestCase):
    def setUp(self):
        amyi._sent = None
        self.f = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        os.unlink(self.f)
        self.env = mock.patch.dict(os.environ, {"AMYI_DEDUPE_FILE": self.f})
        self.env.start()
        self.posts = []
        self.p1 = mock.patch.object(amyi, "disabled", return_value=False)
        self.p2 = mock.patch.object(amyi, "_post", side_effect=lambda pl: (self.posts.append(pl), amyi.record_sent(pl)))
        self.p1.start(); self.p2.start()

    def tearDown(self):
        mock.patch.stopall(); amyi._sent = None

    def test_job_suppresses_contact_form(self):
        amyi.begin_message()
        amyi.defer_contact_form(name="Lesly S", email="l@gmail.com", venue=["OMNIA"], dates=["2026-11-06"], ref="r1")
        amyi.mark_job()
        amyi.notify("signup_job", name="Lesly S", email="l@gmail.com", venue=["OMNIA"], dates=["2026-11-06"], ref="r1", wait=True)
        amyi.end_message()
        self.assertEqual([p["type"] for p in self.posts], ["signup_job"])

    def test_contact_form_only_sends_once(self):
        for _ in range(2):
            amyi.begin_message()
            amyi.defer_contact_form(name="Ana B", email="a@gmail.com", ref="r2")
            amyi.end_message()
        import time; time.sleep(0.2)
        self.assertEqual([p["type"] for p in self.posts], ["contact_form", "contact_form"])

    def test_posh_then_job_same_guest_dates(self):
        amyi.notify("posh_guest", name="Ana B", email="a@gmail.com", venue="X", dates="2026-11-06", ref="o1", wait=True)
        amyi.notify("signup_job", name="Ana B", email="a@gmail.com", venue=["Y"], dates=["2026-11-06"], ref="m1", wait=True)
        self.assertEqual(len(self.posts), 2)

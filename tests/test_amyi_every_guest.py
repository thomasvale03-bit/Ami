import os, tempfile, unittest
from unittest import mock
import amyi, posh_webhook


class EveryGuestPings(unittest.TestCase):
    def setUp(self):
        amyi._sent = None
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        os.unlink(f)
        mock.patch.dict(os.environ, {"AMYI_DEDUPE_FILE": f}).start()
        self.posts = []
        mock.patch.object(amyi, "disabled", return_value=False).start()
        mock.patch.object(amyi, "_post", side_effect=self.posts.append).start()
        mock.patch.object(amyi.threading, "Thread", side_effect=lambda target, args, daemon: mock.Mock(
            start=lambda: target(*args), join=lambda t=None: None)).start()

    def tearDown(self):
        mock.patch.stopall(); amyi._sent = None

    def _order(self, event, qty=2, order="9001"):
        return {"type": "new_order", "account_first_name": "Ana", "account_last_name": "Bell",
                "account_email": "ana@gmail.com", "account_phone": "+17025550100",
                "event_name": event, "order_number": order, "event_id": "e1",
                "items": [{"name": "GA", "quantity": qty}]}

    def test_payload_has_all_fields(self):
        posh_webhook._ping_amyi(self._order("Almost Famous Saturdays"), ("9001", "e1", "ana@gmail.com"), "2026-10-17")
        p = self.posts[0]
        for k in ("name", "email", "phone", "event", "venue", "date", "party", "source", "ref"):
            self.assertTrue(p[k], k)
        self.assertEqual((p["party"], p["source"], p["event"]), ("2", "posh", "Almost Famous Saturdays"))

    def test_scottsdale_and_drais_events_ping(self):
        for i, ev in enumerate(("Riot House Scottsdale", "Almost Famous", "Drai's After Hours")):
            o = self._order(ev, order=str(i)); o["account_email"] = f"g{i}@gmail.com"
            posh_webhook._ping_amyi(o, (str(i), "e", o["account_email"]), "2026-10-17")
        self.assertEqual(len(self.posts), 3)

    def test_changed_party_repings_once(self):
        kw = dict(name="Ana Bell", email="ana@gmail.com", dates="2026-10-17", ref="r1", source="website")
        amyi.notify("contact_form", party=2, **kw)
        amyi.notify("contact_form", party=2, **kw)
        amyi.notify("contact_form", party=5, **kw)
        amyi.notify("contact_form", party=5, **kw)
        self.assertEqual([p["party"] for p in self.posts], ["2", "5"])

    def test_signup_job_without_party_still_suppressed(self):
        amyi.notify("posh_guest", name="Ana", email="ana@gmail.com", dates="2026-10-17", ref="o1", party=2)
        amyi.notify("signup_job", name="Ana", email="ana@gmail.com", dates="2026-10-17", ref="m1")
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()

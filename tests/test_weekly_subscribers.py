import unittest
from datetime import date
from unittest import mock
import main, newsletter
rules = main.rules


class Store:
    def __init__(self, recs=None, fail=False):
        self.recs, self.fail = recs or {}, fail
    def all(self, refresh=False):
        if self.fail:
            raise RuntimeError("boom")
        return self.recs


RECS = {"a@x.com": {"status": "opted_in"}, "B@x.com": {"status": "opted_in"},
        "b@x.com": {"status": "opted_in"}, "c@x.com": {"status": "opted_out"},
        "d@x.com": {"status": "unsubscribed"}, rules.NEWSLETTER_EMAIL: {"status": "opted_in"}}


class T(unittest.TestCase):
    def run_send(self, store, secret):
        env = {"NEWSLETTER_LINK_SECRET": secret} if secret else {}
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(main.gmail_client, "send_once", return_value=True) as so:
            if not secret:
                import os; os.environ.pop("NEWSLETTER_LINK_SECRET", None)
            n = main.send_weekly_to_subscribers(object(), "S", "t", "<body>h</body>", [], [],
                                                date(2026, 10, 14), rules.NEWSLETTER_EMAIL, store=store)
        return n, so

    def test_filter_dedupe(self):
        self.assertEqual(main.weekly_subscribers(Store(RECS), [rules.NEWSLETTER_EMAIL]),
                         ["a@x.com", "b@x.com"])

    def test_individual_with_unsub(self):
        n, so = self.run_send(Store(RECS), "k")
        self.assertEqual(n, 2)
        tos = [c.args[2] for c in so.call_args_list]
        self.assertEqual(tos, ["a@x.com", "b@x.com"])
        for c in so.call_args_list:
            self.assertIn("/unsubscribe?e=", c.args[4])
            self.assertIn("Unsubscribe", c.kwargs["html"])
            self.assertIn("List-Unsubscribe", c.kwargs["headers"])

    def test_bcc_chunks_without_secret(self):
        recs = {f"u{i}@x.com": {"status": "opted_in"} for i in range(120)}
        n, so = self.run_send(Store(recs), None)
        self.assertEqual(n, 120)
        self.assertEqual(len(so.call_args_list), 3)
        for c in so.call_args_list:
            self.assertEqual(c.args[2], rules.NEWSLETTER_EMAIL)
            self.assertLessEqual(len(c.kwargs["headers"]["Bcc"].split(", ")), 50)

    def test_store_failure(self):
        n, so = self.run_send(Store(fail=True), "k")
        self.assertEqual(n, 0)
        so.assert_not_called()


if __name__ == "__main__":
    unittest.main()

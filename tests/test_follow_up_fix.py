"""recent_confirmations unpacks get_plain_text_body's (msg, body) tuple;
send_follow_ups caps a cycle and skips stale backlog."""
import os
import unittest
from datetime import date
from unittest import mock

import gmail_client
import main


class FakeService:
    def __init__(self, ids):
        self.ids = ids

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return mock.Mock(execute=lambda: {"messages": [{"id": i} for i in self.ids]})

    def get(self, **kw):
        return mock.Mock(execute=lambda: {"internalDate": "1791500000000",
                                          "payload": {"headers": [{"name": "To", "value": "Mia <mia@example.com>"}]}})


BODY = "Hi Mia,\r\n\r\nSaturday, October 10 — OMNIA Nightclub\r\n"


class RecentConfirmationsTest(unittest.TestCase):
    def test_tuple_body_is_unpacked(self):
        with mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, BODY)):
            out = gmail_client.recent_confirmations(FakeService(["a"]))
        self.assertEqual(out[0]["first_name"], "Mia")
        self.assertEqual(out[0]["last_night"], date(2026, 10, 10))
        self.assertIn("OMNIA Nightclub", out[0]["venues"])


class FollowUpThrottleTest(unittest.TestCase):
    def run_with(self, confs, today, env=None):
        sent = []
        with mock.patch.dict(os.environ, env or {}), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda s, mid, to, *a, **k: sent.append(to) or True):
            main.send_follow_ups(None, "live", None, today=today)
        return sent

    def test_cap_per_cycle(self):
        confs = [{"id": str(i), "to": f"g{i}@x.com", "first_name": "G", "venues": [],
                  "last_night": date(2026, 10, 1)} for i in range(12)]
        self.assertEqual(len(self.run_with(confs, date(2026, 10, 9), {"FOLLOW_UP_MAX_PER_CYCLE": "3"})), 3)

    def test_stale_backlog_skipped(self):
        confs = [{"id": "1", "to": "old@x.com", "first_name": "O", "venues": [], "last_night": date(2026, 9, 1)}]
        self.assertEqual(self.run_with(confs, date(2026, 10, 10)), [])

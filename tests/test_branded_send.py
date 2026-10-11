"""POST /send-branded: token auth, validation, From info@, dedupe, rate-limit retry."""
import base64
import email
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest import mock

import posh_webhook as pw


class RateLimited(Exception):
    def __init__(self):
        super().__init__("rateLimitExceeded")
        self.resp = mock.Mock(status=429)


class FakeGmail:
    def __init__(self, existing=False, fail_first=0):
        self.existing, self.sent, self.fail_first = existing, [], fail_first

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        return mock.Mock(execute=lambda: {"messages": [{"id": "x"}]} if self.existing else {})

    def send(self, userId, body):
        def run():
            if self.fail_first:
                self.fail_first -= 1
                raise RateLimited()
            self.sent.append(body)
            return {"id": "gm123"}
        return mock.Mock(execute=run)


PAYLOAD = {"to": "guest@example.com", "subject": "You're on the list", "html": "<p>Hi</p>", "text": "Hi"}


class SendBrandedTests(unittest.TestCase):
    def setUp(self):
        pw._branded_times.clear()

    def test_sends_multipart_from_info(self):
        g = FakeGmail()
        code, res = pw.send_branded(g, PAYLOAD)
        self.assertEqual((code, res["id"]), (200, "gm123"))
        msg = email.message_from_bytes(base64.urlsafe_b64decode(g.sent[0]["raw"]))
        self.assertEqual(msg["From"], "Playmaker Entertainment <info@playmakerentertainment.com>")
        self.assertEqual(msg["To"], "guest@example.com")
        types = [p.get_content_type() for p in msg.walk()]
        self.assertIn("text/html", types)
        self.assertIn("text/plain", types)

    def test_dedupes_against_sent(self):
        g = FakeGmail(existing=True)
        self.assertEqual(pw.send_branded(g, PAYLOAD)[0], 200)
        self.assertEqual(g.sent, [])

    def test_validation(self):
        for bad in ({}, dict(PAYLOAD, text=""), dict(PAYLOAD, to="a@x.com,b@x.com"),
                    dict(PAYLOAD, subject="x\r\nBcc: a@x.com")):
            self.assertEqual(pw.send_branded(FakeGmail(), bad)[0], 400)

    def test_retries_rate_limit(self):
        g = FakeGmail(fail_first=2)
        self.assertEqual(pw.send_branded(g, PAYLOAD, _sleep=lambda s: None)[0], 200)
        self.assertEqual(len(g.sent), 1)

    def test_hourly_cap(self):
        for i in range(pw.BRANDED_MAX_PER_HOUR):
            pw.send_branded(FakeGmail(), dict(PAYLOAD, to=f"g{i}@example.com"))
        self.assertEqual(pw.send_branded(FakeGmail(), PAYLOAD)[0], 429)


class ServerTests(unittest.TestCase):
    def setUp(self):
        pw._branded_times.clear()
        self.env = mock.patch.dict(os.environ, {"BRANDED_SEND_TOKEN": "sekret", "POSH_WEBHOOK_TOKEN": "tok"})
        self.env.start()
        self.gmail = FakeGmail()
        self.server = pw.serve(pw.Intake(lambda: self.gmail, "i@x.com"), port=0, host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def post(self, headers):
        req = urllib.request.Request(self.base + "/send-branded", data=json.dumps(PAYLOAD).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.post({})[0], 401)
        self.assertEqual(self.post({"Authorization": "Bearer tok"})[0], 401)
        self.assertEqual(self.gmail.sent, [])

    def test_sends_with_bearer(self):
        code, body = self.post({"Authorization": "Bearer sekret"})
        self.assertEqual((code, body["result"]["id"]), (200, "gm123"))

"""Newsletter opt-in / unsubscribe: signed links, endpoints, export, follow-up exclusion."""
import email
import email.policy
import json
import base64
import os
import unittest
import urllib.error
import urllib.request
from datetime import date
from unittest import mock
from urllib.parse import urlparse, parse_qs

import gmail_client
import main
import newsletter
import posh_webhook as pw
from templates import html_emails


class MemStore:
    def __init__(self):
        self.recs = {}

    def record(self, email_, name, status, source):
        rec = {"email": email_.lower(), "name": name, "status": status,
               "timestamp": "2026-10-11T20:00:00+00:00", "source": source}
        self.recs[rec["email"]] = rec
        return rec

    def all(self, refresh=False):
        return dict(self.recs)

    def is_opted_out(self, e):
        r = self.recs.get(e.lower())
        return bool(r) and r["status"] == "opted_out"


ENV = {"NEWSLETTER_LINK_SECRET": "linksecret", "BRANDED_SEND_TOKEN": "sekret", "POSH_WEBHOOK_TOKEN": "tok"}


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_sign_verify(self):
        sig = newsletter.sign("Mia@X.com", "unsubscribe")
        self.assertTrue(newsletter.verify("mia@x.com", "unsubscribe", sig))
        self.assertFalse(newsletter.verify("mia@x.com", "optin", sig))  # action-bound
        self.assertFalse(newsletter.verify("other@x.com", "unsubscribe", sig))
        self.assertFalse(newsletter.verify("mia@x.com", "unsubscribe", "0" * 32))

    def test_no_secret_no_links(self):
        with mock.patch.dict(os.environ, {"NEWSLETTER_LINK_SECRET": ""}):
            self.assertIsNone(newsletter.follow_up_links("a@x.com"))
            self.assertFalse(newsletter.verify("a@x.com", "optin", "abc"))

    def test_links_and_header(self):
        optin, unsub = newsletter.follow_up_links("a@x.com", "Ann")
        self.assertTrue(optin.startswith("https://amy-production-1616.up.railway.app/newsletter/optin?"))
        self.assertIn("/unsubscribe?", unsub)
        h = newsletter.list_unsubscribe_headers(unsub)
        self.assertIn(f"<{unsub}>", h["List-Unsubscribe"])
        self.assertEqual(h["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_export_csv(self):
        ctype, body = newsletter.export({"a@x.com": {"email": "a@x.com", "name": "A", "status": "opted_in",
                                                     "timestamp": "t", "source": "s"}}, "csv")
        self.assertIn("text/csv", ctype)
        self.assertEqual(body.splitlines()[0], "email,name,status,timestamp,source")
        self.assertIn("a@x.com,A,opted_in,t,s", body)


class TemplateTests(unittest.TestCase):
    def test_follow_up_content(self):
        out = html_emails.render_follow_up("Mia", "https://h/optin?e=a&t=1", "https://h/unsubscribe?e=a&t=2")
        for s in ("Hope you had an amazing time", "enjoyed the night", "Remember us for your next Vegas trip",
                  "every 3 months", "Yes, sign me up", "Unsubscribe from Playmaker emails",
                  "Know someone heading to Vegas?", "Book a VIP Table", "Share Playmaker", "is not a secret &#9829;", "love to get referred",
                  'href="https://h/optin?e=a&amp;t=1"', 'href="https://h/unsubscribe?e=a&amp;t=2"'):
            self.assertIn(s, out)
        self.assertNotIn("{{", out)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.store = MemStore()
        self.server = pw.serve(pw.Intake(lambda: None, "i@x.com"), port=0, host="127.0.0.1", store=self.store)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def get(self, url, headers=None, data=None):
        req = urllib.request.Request(url if url.startswith("http") else self.base + url,
                                     headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode()

    def local(self, url):
        u = urlparse(url)
        return f"{u.path}?{u.query}"

    def test_optin_then_unsubscribe(self):
        optin, unsub = newsletter.follow_up_links("Mia@X.com", "Mia Lee")
        code, ctype, body = self.get(self.local(optin))
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("Thank you, Mia!", body)
        self.assertEqual(self.store.recs["mia@x.com"]["status"], "opted_in")
        self.assertEqual(self.store.recs["mia@x.com"]["name"], "Mia Lee")
        code, _, body = self.get(self.local(unsub))
        self.assertEqual(code, 200)
        self.assertIn("unsubscribed", body)
        self.assertTrue(self.store.is_opted_out("mia@x.com"))

    def test_bad_signature_rejected(self):
        code, _, body = self.get("/unsubscribe?e=mia@x.com&t=deadbeef")
        self.assertEqual(code, 400)
        self.assertEqual(self.store.recs, {})
        optin, _ = newsletter.follow_up_links("mia@x.com")
        q = parse_qs(urlparse(optin).query)  # optin sig can't unsubscribe
        self.assertEqual(self.get(f"/unsubscribe?e=mia@x.com&t={q['t'][0]}")[0], 400)

    def test_one_click_post(self):
        _, unsub = newsletter.follow_up_links("p@x.com")
        code, _, _ = self.get(self.local(unsub), data=b"List-Unsubscribe=One-Click")
        self.assertEqual(code, 200)
        self.assertEqual(self.store.recs["p@x.com"]["source"], "list_unsubscribe_one_click")

    def test_export_requires_token(self):
        self.store.record("a@x.com", "A", "opted_in", "follow_up_email")
        self.assertEqual(self.get("/newsletter/export")[0], 401)
        self.assertEqual(self.get("/newsletter/export?token=sekret")[0], 401)  # header only
        code, ctype, body = self.get("/newsletter/export", {"Authorization": "Bearer sekret"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["records"][0]["email"], "a@x.com")
        code, ctype, body = self.get("/newsletter/export?format=csv", {"X-Amy-Token": "sekret"})
        self.assertIn("text/csv", ctype)
        self.assertIn("a@x.com,A,opted_in", body)


class FollowUpExclusionTests(unittest.TestCase):
    def test_unsubscribed_excluded_and_header_added(self):
        store = MemStore()
        store.record("gone@x.com", "G", "opted_out", "follow_up_email")
        confs = [{"id": "1", "to": "gone@x.com", "first_name": "G", "venues": [], "last_night": date(2026, 10, 1)},
                 {"id": "2", "to": "Stay <stay@x.com>", "first_name": "S", "venues": [],
                  "last_night": date(2026, 10, 1)}]
        calls = []
        with mock.patch.dict(os.environ, ENV), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda s, mid, to, *a, **k: calls.append((to, k)) or True):
            main.send_follow_ups(None, "live", today=date(2026, 10, 8), store=store)
        self.assertEqual([c[0] for c in calls], ["stay@x.com"])
        kw = calls[0][1]
        self.assertIn("/unsubscribe?", kw["headers"]["List-Unsubscribe"])
        self.assertIn("Unsubscribe from Playmaker emails", kw["html"])

    def test_held_without_secret(self):
        confs = [{"id": "2", "to": "stay@x.com", "first_name": "S", "venues": [], "last_night": date(2026, 10, 1)}]
        sent = []
        with mock.patch.dict(os.environ, {"NEWSLETTER_LINK_SECRET": ""}), \
             mock.patch.object(gmail_client, "recent_confirmations", return_value=confs), \
             mock.patch.object(gmail_client, "has_opted_out", return_value=False), \
             mock.patch.object(gmail_client, "send_once", side_effect=lambda *a, **k: sent.append(a) or True):
            main.send_follow_ups(None, "live", today=date(2026, 10, 8), store=MemStore())
        self.assertEqual(sent, [])

    def test_build_message_headers(self):
        raw = gmail_client.build_message("a@x.com", "S", "b", "info@x.com",
                                         headers={"List-Unsubscribe": "<https://u>"})["raw"]
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(msg["List-Unsubscribe"], "<https://u>")


class GmailStoreTests(unittest.TestCase):
    def test_record_and_latest_wins(self):
        inserted = []

        class Svc:
            def users(s): return s
            def messages(s): return s
            def labels(s): return s
            def insert(s, userId, body):
                inserted.append(body)
                return mock.Mock(execute=lambda: {"id": str(len(inserted))})
            def list(s, **kw):
                if "q" not in kw:  # labels().list
                    return mock.Mock(execute=lambda: {"labels": [{"name": newsletter.LABEL, "id": "L1"}]})
                return mock.Mock(execute=lambda: {"messages": [{"id": str(i)} for i in range(len(inserted))]})

        bodies = []
        st = newsletter.GmailStore(lambda: Svc(), "i@x.com")
        st.record("A@x.com", "A", "opted_in", "s")
        st.record("a@x.com", "A", "opted_out", "s")
        for b in inserted:
            m = email.message_from_bytes(base64.urlsafe_b64decode(b["raw"]))
            bodies.append(m.get_payload(decode=True).decode())
            self.assertEqual(b["labelIds"], ["L1"])
            self.assertTrue(m["Subject"].startswith(newsletter.SUBJECT_PREFIX))
        recs = [json.loads(x) for x in bodies]
        recs[0]["timestamp"], recs[1]["timestamp"] = "2026-01-01", "2026-01-02"
        with mock.patch.object(gmail_client, "get_plain_text_body",
                               side_effect=lambda s, i: ({}, json.dumps(recs[int(i)]))):
            self.assertTrue(st.all(refresh=True)["a@x.com"]["status"] == "opted_out")
            self.assertTrue(st.is_opted_out("a@x.com"))


if __name__ == "__main__":
    unittest.main()

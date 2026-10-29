"""Pending jobs (no live TAO link), job ledger, AMYI watchdog, /jobs/<ref>/done,
and Scottsdale send checks."""
import json
import os
import pathlib
import unittest
import urllib.error
import urllib.request
from unittest import mock

import gmail_client
import job_tracker
import main
import posh_webhook as pw
import tao_portal

FIX = pathlib.Path(__file__).parent / "fixtures"
FORM = (FIX / "new_form.txt").read_text()
POSH = (FIX / "posh_signup.txt").read_text()


class PendingNoLiveLink(unittest.TestCase):
    def _run(self, catalog):
        sent, exc = [], []
        with mock.patch.dict(os.environ, {"AMY_CONCIERGE": "true", "AMY_GUEST_CONFIRMATION": "false"}), \
             mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, FORM)), \
             mock.patch.object(gmail_client, "send_once",
                               side_effect=lambda *a, **k: sent.append((a, k)) or True), \
             mock.patch.object(tao_portal, "catalog_snapshot", return_value=catalog), \
             mock.patch.object(main, "_notify_exception", wraps=main._notify_exception) as ne, \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 29, 12, 0)
            out = main.handle_message(mock.MagicMock(), "m1", dry_run=False, labels={})
        return out, sent, ne

    def test_not_posted_records_pending_job_and_alerts_amyi(self):
        from datetime import date
        # Master link readable, but the requested nights aren't posted yet.
        out, sent, ne = self._run({("TAO Nightclub", date(2026, 12, 31)): {"url": "x"}})
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        ne.assert_called_once()
        ref, raw, issue = ne.call_args[0][:3]
        self.assertEqual(ref, "m1:pending")
        self.assertIn("event not posted yet", issue)
        self.assertEqual(raw["email"], "jane.sample@example.com")
        self.assertEqual(raw["start_date"], "2026-10-30")
        row = job_tracker.get("m1:pending")
        self.assertEqual(row["status"], "pending_event")
        self.assertEqual(row["guest"], "Jane Sample")
        team = [a for a, k in sent if a[2] == main.rules.PLAYMAKER_EMAIL]
        self.assertEqual(len(team), 1)
        subj, body = team[0][3], team[0][4]
        self.assertTrue(subj.startswith("Pending: Jane Sample — "))
        self.assertTrue(subj.endswith("(event not posted on TAO yet; AMYI will book when live)"))
        for word in ("Jose", "tap each night", "promoter dashboard", "Mark done"):
            self.assertNotIn(word, subj + body)

    def test_templates_and_readme_have_no_jose(self):
        root = pathlib.Path(main.__file__).parent
        for f in [root / "README.md", root / "main.py", *(root / "templates").glob("*.py")]:
            self.assertNotIn("Jose", f.read_text(), f)


class Ledger(unittest.TestCase):
    def test_record_fields_and_done(self):
        row = job_tracker.record("r1", order_id="39318555", guest="Giovanni Price",
                                 email="g@gmail.com", venue=["JEWEL Nightclub"], dates=["2027-01-01"], now=100)
        for k in ("ref", "order_id", "guest", "venue", "dates", "queued_at",
                  "webhook_status", "attempts", "done_at"):
            self.assertIn(k, row)
        self.assertEqual(job_tracker.record("r1", guest="other")["guest"], "Giovanni Price")  # idempotent
        job_tracker.mark_done("r1", now=200)
        self.assertEqual(job_tracker.get("r1")["done_at"], 200)
        # Persisted under posh_webhook's pending store.
        self.assertTrue(os.path.exists(os.path.join(pw.pending_dir(), "jobs.json")))

    def test_watchdog_timeline(self):
        calls = []
        cb = {k: (lambda row, k=k: calls.append((k, row["ref"]))) for k in ("refire", "alert", "escalate")}
        job_tracker.record("w", guest="G", email="g@gmail.com", now=0)
        run = lambda t: job_tracker.watchdog(cb["refire"], cb["alert"], cb["escalate"], now=t)
        self.assertEqual(run(60), [])
        self.assertEqual(run(185), [("w", "refire")])
        self.assertEqual(run(200), [])  # re-fire only once
        self.assertEqual(run(305), [("w", "alert")])
        self.assertEqual(run(400), [])  # alert only once
        self.assertEqual(run(905), [("w", "manual")])
        self.assertEqual(job_tracker.get("w")["status"], "manual")
        self.assertEqual(run(2000), [])
        self.assertEqual([c[0] for c in calls], ["refire", "alert", "escalate"])

    def test_watchdog_three_failures_escalates_and_done_is_skipped(self):
        calls = []
        f = lambda row: calls.append(row["ref"])
        job_tracker.record("bad", email="b@gmail.com", now=0)
        job_tracker.record("ok", email="o@gmail.com", now=0)
        for _ in range(3):
            job_tracker.webhook_result("bad", 500, 4, False)
        job_tracker.mark_done("ok")
        self.assertEqual(job_tracker.watchdog(f, f, f, now=10), [("bad", "manual")])

    def test_pending_event_waits_for_posting(self):
        job_tracker.record("p", email="p@gmail.com", now=0)
        job_tracker.update("p", status="pending_event")
        self.assertEqual(job_tracker.watchdog(*(lambda r: None,) * 3, now=99999), [])
        self.assertFalse(job_tracker.has_open())

    def test_run_job_watchdog_alerts_amyi_and_team(self):
        job_tracker.record("m9", guest="Giovanni Price", email="g@gmail.com",
                           venue="JEWEL Nightclub", dates=["2027-01-01"], now=0)
        job_tracker.update("m9", refired_at=1)
        with mock.patch.object(main, "_notify_exception") as ne, \
             mock.patch.object(main.posh_webhook, "send_branded", return_value=(200, {"sent": True})) as sb:
            self.assertEqual(main.run_job_watchdog(mock.MagicMock(), now=301), [("m9", "alert")])
        self.assertEqual(ne.call_args[0][0], "m9:watchdog")
        self.assertFalse(ne.call_args[1]["track"])
        self.assertEqual(sb.call_args[0][1]["to"], main.rules.PLAYMAKER_EMAIL)
        with mock.patch.object(main, "_notify_exception") as ne, \
             mock.patch.object(main.posh_webhook, "send_branded", return_value=(200, {})):
            self.assertEqual(main.run_job_watchdog(mock.MagicMock(), now=901), [("m9", "manual")])
        self.assertIn("ESCALATED", ne.call_args[0][2])

    def test_refire_uses_stored_payload(self):
        job_tracker.record("rf", email="g@gmail.com", now=0)
        job_tracker.webhook_result("rf", None, 4, False, payload={"type": "signup_job", "ref": "rf"})
        with mock.patch.object(main.amyi, "refire") as rf, \
             mock.patch.object(main.posh_webhook, "send_branded", return_value=(200, {})):
            main.run_job_watchdog(mock.MagicMock(), now=181)
        rf.assert_called_once_with({"type": "signup_job", "ref": "rf"})


class JobDoneEndpoint(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"BRANDED_SEND_TOKEN": "s3cret"})
        self.env.start()
        self.server = pw.serve(pw.Intake(lambda: mock.MagicMock(), "i@x.com"), port=0, host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()

    def _post(self, ref, token=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        req = urllib.request.Request(f"{self.base}/jobs/{ref}/done", data=b"{}", headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_requires_token(self):
        job_tracker.record("abc", email="g@gmail.com")
        self.assertEqual(self._post("abc")[0], 401)
        self.assertEqual(self._post("abc", "wrong")[0], 401)
        self.assertIsNone(job_tracker.get("abc")["done_at"])

    def test_marks_done(self):
        job_tracker.record("abc:pending", email="g@gmail.com")
        code, res = self._post("abc%3Apending", "s3cret")
        self.assertEqual(code, 200)
        self.assertTrue(res["result"]["done"])
        self.assertIsNotNone(job_tracker.get("abc:pending")["done_at"])

    def test_unknown_ref(self):
        self.assertEqual(self._post("nope", "s3cret")[0], 404)


class ScottsdaleSendCheck(unittest.TestCase):
    def _handle(self, text, send_ok=True):
        sent = []
        def fake(service, mid, to, subj, txt, html, **kw):
            sent.append((to, subj, txt))
            return send_ok or to == main.rules.PLAYMAKER_EMAIL
        with mock.patch.dict(os.environ, {"AMY_CONCIERGE": "true", "POSH_CONSENT_ON_FILE": "true"}), \
             mock.patch.object(gmail_client, "get_plain_text_body", return_value=({}, text)), \
             mock.patch.object(gmail_client, "posh_order_already_handled", return_value=False), \
             mock.patch.object(main, "_send_branded_or_gmail", side_effect=fake), \
             mock.patch.object(main, "_notify_exception") as ne, \
             mock.patch.object(main, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 1, 12, 0)
            out = main.handle_message(mock.MagicMock(), "m1", dry_run=False, labels={})
        return out, sent, ne

    def test_riot_house_posh_success_sends_team_summary_and_marks_done(self):
        out, sent, ne = self._handle(POSH.replace("Playmaker Tuesdays at OMNIA", "Riot House Guestlist"))
        self.assertEqual(out, gmail_client.PROCESSED_LABEL)
        team = [s for s in sent if s[0] == main.rules.PLAYMAKER_EMAIL]
        self.assertEqual(len(team), 1)
        self.assertTrue(team[0][1].startswith("Signed up: "))
        self.assertIn("Riot House", team[0][1])
        self.assertIsNotNone(job_tracker.get("m1")["done_at"])
        ne.assert_not_called()

    def test_failed_send_alerts_and_exception_label(self):
        out, sent, ne = self._handle(POSH.replace("Playmaker Tuesdays at OMNIA", "Almost Famous Guestlist"),
                                     send_ok=False)
        self.assertEqual(out, gmail_client.EXCEPTION_LABEL)
        ne.assert_called_once()
        self.assertIn("did not send", ne.call_args[0][2])
        team = [s for s in sent if s[0] == main.rules.PLAYMAKER_EMAIL]
        self.assertTrue(team and team[0][1].startswith("SEND FAILED: "))
        self.assertIsNone(job_tracker.get("m1")["done_at"])


if __name__ == "__main__":
    unittest.main()

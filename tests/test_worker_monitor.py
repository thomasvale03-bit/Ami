"""In-worker sweep, self-health, stuck-sign-up audit and alert dedupe."""
import json
from datetime import datetime

import pytest

import amyi
import booking_worker as bw
import stall_watchdog
import worker_monitor as wm

PHX = wm.PHOENIX


def ts(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=PHX).timestamp()


NOW = ts(2026, 11, 16, 19, 45)


def job(ref, state, *, created=NOW - 600, updated=None, due=None, reason="", start="2026-11-16",
        end=None, venues=("Marquee Nightclub",), meta=None, lease=None):
    return {"ref": ref, "state": state, "created": created, "updated": updated or created,
            "due": created if due is None else due, "reason": reason, "lease_until": lease,
            "metadata": meta or {}, "attempts": 1,
            "raw": {"name": "Ana Mendoza", "start_date": start, "end_date": end or start,
                    "venues": list(venues), "female_count": 3, "male_count": 2}}


class FakeApi:
    queue_token = "x"

    def __init__(self, jobs=(), requeue=(200, {"state": "queued"})):
        self.all, self.requeue_reply, self.calls, self.requeued = list(jobs), requeue, [], []

    def jobs(self, state="queued", limit=100):
        self.calls.append(("jobs", state))
        return 200, {"jobs": [j for j in self.all if j["state"] == state]}

    def _call(self, method, path, body=None, token=None):
        self.calls.append((method, path))
        return 200, {"jobs": self.all}

    def stall_requeue(self, ref, reason=""):
        self.requeued.append(ref)
        return self.requeue_reply


class Browser:
    def __init__(self, states):
        self.states, self.ensured = list(states), 0

    def healthy(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def ensure(self):
        self.ensured += 1


def mon(tmp_path, api, sent=None, **kw):
    sent = sent if sent is not None else []
    kw.setdefault("browser_plain", False)
    return wm.Monitor(api, tmp_path, clock=lambda: NOW,
                      sender=lambda p: sent.append(p) or True, **kw), sent


# --- sweep ---------------------------------------------------------------------

def test_sweep_idle_is_two_cheap_calls_and_no_pickup(tmp_path):
    api = FakeApi([job("r1", "retry", due=NOW + 900), job("d1", "done")])
    m, _ = mon(tmp_path, api)
    runs = []
    assert m.sweep(runs.append, NOW) == {"status": "idle", "due": 0}
    assert runs == [] and api.calls == [("jobs", "queued"), ("jobs", "retry")]


def test_sweep_runs_pickup_for_due_queued_and_retry(tmp_path):
    api = FakeApi([job("q1", "queued", created=NOW - 120), job("r1", "retry", due=NOW - 5),
                   job("q2", "queued", due=NOW + 600)])
    m, _ = mon(tmp_path, api)
    runs = []
    out = m.sweep(lambda reason: runs.append(reason) or {"status": "run_done"}, NOW)
    assert runs == ["sweep"] and out["refs"] == ["q1", "r1"]


def test_tick_skips_while_a_run_is_in_progress(tmp_path):
    api = FakeApi([job("q1", "queued")])
    m, _ = mon(tmp_path, api)
    stall_watchdog.start_run(tmp_path, "2026-11-16T18")
    runs = []
    assert m.tick(runs.append, NOW) is None and runs == [] and api.calls == []
    stall_watchdog.end_run(tmp_path)
    m.tick(lambda r: runs.append(r) or {}, NOW)
    assert runs == ["sweep"]


def test_tick_cadence_sweep_15_audit_30(tmp_path):
    api = FakeApi([])
    m, _ = mon(tmp_path, api)
    assert set(m.tick(lambda r: {}, NOW)) == {"sweep", "audit"}
    assert m.tick(lambda r: {}, NOW + 600) is None
    assert set(m.tick(lambda r: {}, NOW + 900)) == {"sweep"}
    assert set(m.tick(lambda r: {}, NOW + 1800)) == {"sweep", "audit"}


def test_sweep_pickup_marks_heartbeat_and_pickup_time(tmp_path):
    class W:
        def run_pickup(self):
            assert wm.run_in_progress(tmp_path)
            return {"status": "run_done", "handled": 1}
    assert bw.sweep_pickup(W(), tmp_path)["status"] == "run_done"
    assert not wm.run_in_progress(tmp_path) and wm.read_last_pickup(tmp_path) > 0
    assert (tmp_path / "last_slot").exists() is False


def test_live_loop_calls_monitor_between_pickups(tmp_path):
    class W:
        def run_pickup(self):
            return {"status": "run_done", "handled": 0}
    ticks = []

    class M:
        sweep_every, audit_every = 900, 1800

        def tick(self, run_pickup, now=None):
            ticks.append(now)
    times = iter([ts(2026, 11, 16, 19, 40)] * 3)

    def clock():
        try:
            return next(times)
        except StopIteration:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        bw.live_loop(W(), tmp_path, clock=clock, sleep=lambda s: None, monitor=M(), heartbeat=10**9)
    assert ticks


# --- health --------------------------------------------------------------------

def test_dead_cdp_is_relaunched_without_alert(tmp_path):
    b = Browser([False, True])
    m, _ = mon(tmp_path, FakeApi([]), browser=b, browser_plain=True)
    m.sweep(lambda r: {}, NOW)
    assert b.ensured == 1 and m.health_items == []


def test_browser_that_wont_restart_is_flagged(tmp_path):
    b = Browser([False])
    m, sent = mon(tmp_path, FakeApi([]), browser=b, browser_plain=True)
    m.tick(lambda r: {}, NOW)
    assert b.ensured == 1
    assert [i["problem"] for i in sent[0]["items"]] == ["browser_down"]


def test_stale_claim_and_unrecovered_stall_flagged(tmp_path):
    api = FakeApi([job("c1", "assigned", updated=NOW - 3600, lease=NOW - 10)])
    stall_watchdog.record_stall(tmp_path, slot="2026-11-16T18", duration=1500, requeued=[],
                                clock=lambda: NOW - 600, recovery={"left_manual": ["c9"]}, chrome_ok=False)
    m, _ = mon(tmp_path, api)
    probs = [i["problem"] for i in m.audit(now=NOW)["items"]]
    assert "claim_stale" in probs and any(p.startswith("stall_unrecovered") for p in probs)


def test_clean_stall_event_not_flagged(tmp_path):
    stall_watchdog.record_stall(tmp_path, slot="s", duration=1500, requeued=["a"],
                                clock=lambda: NOW - 600, recovery={"requeued": ["a"]}, chrome_ok=True)
    m, sent = mon(tmp_path, FakeApi([]))
    assert m.audit(now=NOW)["flagged"] == 0 and sent == []


# --- audit ---------------------------------------------------------------------

def test_todays_bug_queued_after_pickup_or_45_min(tmp_path):
    """Ana (7:14 PM) / Kirk (7:27 PM) sat queued; flag once a pickup ran after
    creation or after 45 minutes."""
    ana = job("ana", "queued", created=ts(2026, 11, 16, 19, 14))
    kirk = job("kirk", "queued", created=ts(2026, 11, 16, 19, 27))
    m, sent = mon(tmp_path, FakeApi([ana, kirk]))
    out = m.audit(now=ts(2026, 11, 16, 19, 50))
    assert out["flagged"] == 0  # young, no pickup since
    out = m.audit(now=ts(2026, 11, 16, 20, 0))
    assert [i["ref"] for i in out["items"]] == ["ana"]  # 46 min
    wm.mark_pickup(tmp_path, ts(2026, 11, 16, 20, 1))
    out = m.audit(now=ts(2026, 11, 16, 20, 5))
    assert sorted(i["ref"] for i in out["items"]) == ["ana", "kirk"]
    item = [i for i in out["items"] if i["ref"] == "kirk"][0]
    assert item["female"] == 3 and item["male"] == 2 and item["venue"] == "Marquee Nightclub"
    assert item["minutes_sitting"] == 38 and item["guest"] == "Ana Mendoza"


def test_retry_with_future_due_not_flagged(tmp_path):
    m, _ = mon(tmp_path, FakeApi([job("r", "retry", created=NOW - 86400, due=NOW + 600,
                                      reason="TAO Nightclub 2026-12-31: guest list not posted on TAO yet")]))
    assert m.audit(now=NOW)["flagged"] == 0


@pytest.mark.parametrize("reason,venues,meta", [
    ("TAO Nightclub 2026-11-20: guest list not posted on TAO yet; nothing submitted", ("TAO Nightclub",), None),
    ("Hakkasan Nightclub 2026-11-22: no guest list posted on TAO yet (other nights already booked)", ("Hakkasan",), None),
    ("Past or invalid source date; never move to tonight", ("OMNIA Nightclub",), None),
    ("Notify only", ("Ghostbar",), None),
    ("Unspecified/unsupported venue; reviewed routing required", ("Riot House | Guest List",), None),
    ("Source request requires review; do not guess", ("Almost Famous",), None),
    ("Booked and emailed but completion refused (HTTP 409); DO NOT rebook", ("Hakkasan Nightclub",),
     {"resolution": {"evidence": {"bookings": [{"verified": True}]}}}),
    ("Duplicate sign-up: same email already booked", ("OMNIA Nightclub",), None),
    ("Owner stop (Thomas): wrong date", ("LAVO Party Brunch",), None),
])
def test_expected_holds_not_flagged(tmp_path, reason, venues, meta):
    m, _ = mon(tmp_path, FakeApi([job("h", "manual", reason=reason, venues=venues, meta=meta)]))
    assert m.audit(now=NOW)["flagged"] == 0


def test_held_zouk_job_is_flagged_now_that_zouk_is_confirmed_by_the_worker(tmp_path):
    # Nov 24 2026: ZOUK is no longer notify-only, so a stuck ZOUK job is real.
    m, _ = mon(tmp_path, FakeApi([job("z", "manual", reason="Source request malformed (x)",
                                      venues=("ZOUK Nightclub",))]))
    assert m.audit(now=NOW)["flagged"] == 1


def test_new_exceptions_for_today_or_later_flagged(tmp_path):
    jobs = [
        job("u1", "submission_uncertain", reason="a submit started with no confirmed result"),
        job("m1", "manual", reason="Source request requires review; do not guess", start="2026-11-17"),
        job("m2", "manual", reason="Booked and emailed but completion refused", meta={}),
        job("old", "manual", reason="Source request requires review", created=NOW - 27 * 3600),
        job("past", "manual", reason="Same-day OMNIA closed", start="2026-11-15"),
        job("nr", "needs_review", reason="recurring guess"),
    ]
    m, _ = mon(tmp_path, FakeApi(jobs))
    refs = sorted(i["ref"] for i in m.audit(now=NOW)["items"])
    assert refs == ["m1", "m2", "nr", "u1"]


def test_worker_error_requeued_once_then_flagged(tmp_path):
    bad = job("w1", "manual", reason="Worker error (TimeoutError: Timeout 14002ms exceeded.); not retried")
    api = FakeApi([bad])
    m, sent = mon(tmp_path, api)
    assert m.audit(now=NOW)["flagged"] == 0 and api.requeued == ["w1"]
    out = m.audit(now=NOW + 1800)  # failed again -> flag, no second requeue
    assert api.requeued == ["w1"] and [i["problem"] for i in out["items"]] == ["worker_error_again"]


def test_worker_error_requeue_refused_is_flagged(tmp_path):
    api = FakeApi([job("w2", "manual", reason="Worker error (Error: net::ERR_CONNECTION_RESET)")],
                  requeue=(409, {"error": "Job has submission progress; manual review required"}))
    m, _ = mon(tmp_path, api)
    out = m.audit(now=NOW)
    assert [i["problem"] for i in out["items"]] == ["worker_error_requeue_failed"]
    assert "409" in out["items"][0]["worker_tried"]


def test_uncertain_worker_error_never_requeued(tmp_path):
    api = FakeApi([job("w3", "submission_uncertain", reason="Worker error (TimeoutError: x); not retried")])
    m, _ = mon(tmp_path, api)
    assert [i["problem"] for i in m.audit(now=NOW)["items"]] == ["needs_attention:submission_uncertain"]
    assert api.requeued == []


def test_dry_run_writes_nothing_and_sends_nothing(tmp_path):
    api = FakeApi([job("w1", "manual", reason="Worker error (TimeoutError)"),
                   job("q", "queued", created=NOW - 3600)])
    sent = []
    m = wm.Monitor(api, tmp_path, clock=lambda: NOW, dry_run=True, sender=lambda p: sent.append(p),
                   browser_plain=False)
    out = m.audit(now=NOW)
    assert out["dry_run"] and out["flagged"] == 2 and sent == [] and api.requeued == []
    assert not (tmp_path / "alert_dedupe.json").exists() and not (tmp_path / "alerts.jsonl").exists()
    runs = []
    assert m.sweep(runs.append, NOW)["status"] == "would_pickup" and runs == []


# --- alerting + dedupe -----------------------------------------------------------

def test_one_batched_alert_and_6h_dedupe(tmp_path):
    jobs = [job("a", "queued", created=NOW - 3600), job("b", "queued", created=NOW - 3000)]
    m, sent = mon(tmp_path, FakeApi(jobs))
    assert m.audit(now=NOW)["sent"] == 2
    assert len(sent) == 1 and sent[0]["type"] == "worker_alert" and sent[0]["count"] == 2
    assert m.audit(now=NOW + 1800)["sent"] == 0 and len(sent) == 1
    assert m.audit(now=NOW + 6 * 3600 + 1)["sent"] == 2 and len(sent) == 2


def test_new_problem_same_ref_alerts_separately(tmp_path):
    d = wm.Dedupe(tmp_path / "d.json")
    a = {"ref": "x", "problem": "unbooked_queued"}
    d.mark([a], NOW)
    assert d.fresh([a, {"ref": "x", "problem": "worker_error_again"}], NOW + 60) == [
        {"ref": "x", "problem": "worker_error_again"}]


def test_no_webhook_writes_alerts_jsonl(tmp_path, caplog):
    m = wm.Monitor(FakeApi([job("a", "queued", created=NOW - 3600)]), tmp_path, clock=lambda: NOW,
                   sender=lambda p: None, browser_plain=False)
    with caplog.at_level("WARNING"):
        m.audit(now=NOW)
    rows = [json.loads(x) for x in (tmp_path / "alerts.jsonl").read_text().splitlines()]
    assert rows[0]["items"][0]["ref"] == "a" and rows[0]["delivered"] is False
    assert "NOT delivered" in caplog.text
    m.audit(now=NOW + 60)  # deduped, no second line
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 1


def test_failed_webhook_is_retried_next_audit(tmp_path):
    m = wm.Monitor(FakeApi([job("a", "queued", created=NOW - 3600)]), tmp_path, clock=lambda: NOW,
                   sender=lambda p: False, browser_plain=False)
    m.audit(now=NOW)
    assert m.dedupe.fresh([{"ref": "a", "problem": "unbooked_queued"}], NOW + 60)


def test_alert_env_file_parsing(tmp_path, monkeypatch):
    monkeypatch.delenv("AMYI_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AMYI_WEBHOOK_AUTH", raising=False)
    env = tmp_path / "alert.env"
    env.write_text("# c\nexport AMYI_WEBHOOK_URL='https://hook.example/x'\nAMYI_WEBHOOK_AUTH=Bearer\\ abc\n")
    assert wm.load_alert_env(env) == ("https://hook.example/x", "Bearer abc")
    assert wm.load_alert_env(tmp_path / "missing") == ("", "")


def test_post_alert_sends_auth_header_and_retries():
    seen = []

    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"ok"

    replies = iter([500, 200])

    def opener(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return Resp(next(replies))
    ok = amyi.post_alert({"type": "worker_alert", "count": 1}, url="https://hook/x", auth="Bearer s",
                         opener=opener, _sleep=lambda s: None)
    assert ok is True and len(seen) == 2 and seen[0][1] == "Bearer s"


def test_post_alert_never_sends_under_tests_without_opener():
    assert amyi.post_alert({"type": "worker_alert"}, url="https://hook/x") is None

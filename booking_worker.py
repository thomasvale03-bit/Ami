"""AMY Railway booking worker: books queued guest-list jobs with no desktop.

Runs as its own Railway service (same image, start command
`python booking_worker.py --loop`) and talks to the AMY service only over its
authenticated HTTPS API (/queue/* and /send-branded), so the queue database
never leaves the AMY volume.

Per job: claim one -> re-check source rules -> for each exact source
venue/night: skip if already booked (worker ledger or saved progress), read the
live TAO listing, fill the form through Playmaker's promoter link, submit once,
read the order ID + screenshot -> save progress -> send guest + team emails via
/send-branded -> mark complete with evidence. Drai's goes to SpeakeasyGo only
(name + party, no emails).

Anything unclear stops the job as manual/uncertain with a reason. Nothing is
retried in a loop and nothing is ever submitted twice:
  * a local ledger (AMY_WORKER_DIR, put it on a volume) records "submitting"
    BEFORE the final click and the order ID right after; a "submitting" entry
    with no order means the outcome is unknown -> uncertain, never resubmitted;
  * progress (order IDs, email receipt IDs) is also saved on the server job.

Modes (AMY_WORKER_MODE):
  dry-run  (default) read the soonest queued job WITHOUT claiming it, open the
           TAO form, fill it and stop before "Submit Order". No queue writes,
           no emails.
  live     claim, book, email, complete.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

log = logging.getLogger("amy.worker")

PHOENIX = ZoneInfo("America/Phoenix")
VEGAS = ZoneInfo("America/Los_Angeles")
DEFAULT_BASE_URL = "https://amy-production-1616.up.railway.app"


class Stop(Exception):
    """End this job now with `action` (manual|uncertain|retry) and a reason."""

    def __init__(self, action, reason, **extra):
        super().__init__(reason)
        self.action, self.reason, self.extra = action, reason, extra


# --- HTTP client ------------------------------------------------------------

class Api:
    """Minimal client for the AMY service. Tokens come from env, never logged."""

    def __init__(self, base_url, queue_token, send_token, timeout=90):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("AMY_BASE_URL must be https")
        self.base, self.queue_token, self.send_token, self.timeout = base_url.rstrip("/"), queue_token, send_token, timeout

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("AMY_BASE_URL", DEFAULT_BASE_URL),
                   os.environ.get("AMY_QUEUE_API_TOKEN", ""), os.environ.get("BRANDED_SEND_TOKEN", ""))

    def _call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode() or "null")
            except ValueError:
                payload = None
            return exc.code, payload

    def claim(self):
        return self._call("POST", "/queue/claim", {"limit": 1}, self.queue_token)

    def jobs(self, state="queued", limit=100):
        return self._call("GET", f"/queue/jobs?state={quote(state)}&limit={limit}", None, self.queue_token)

    def update(self, ref, token, action, metadata=None, reason=""):
        return self._call("POST", "/queue/jobs/" + quote(ref, safe=""), {
            "claim_token": token, "action": action, "metadata": metadata or {}, "reason": reason[:1000]},
            self.queue_token)

    def send_branded(self, payload):
        return self._call("POST", "/send-branded", payload, self.send_token)


# --- Local no-double-book ledger ---------------------------------------------

class Ledger:
    """JSON file keyed "ref|venue|date". Written atomically before the final
    click ("submitting") and right after ("booked")."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def get(self, key):
        return self._load().get(key)

    def put(self, key, value):
        data = self._load()
        data[key] = {**data.get(key, {}), **value, "at": time.time()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def night_key(ref, venue, night):
    return f"{ref}|{venue}|{night}"


# --- Vendors (thin wrappers so tests can swap them) ---------------------------

class TaoBooker:
    def __init__(self):
        import tao_portal
        self.tao = tao_portal

    def reset(self):
        self.tao.reset_catalog()

    def posted(self, venue, night):
        """True/False when the live feed is readable; None when unknown."""
        snap = self.tao.catalog_snapshot()
        if not snap:
            return None
        return (venue, night) in snap

    def listing(self, venue, night):
        return self.tao.check_availability(venue, night)

    def submit(self, listing, guest, *, rehearse, screenshot_path, before_submit):
        return self.tao.submit_registration(listing, guest, rehearse=rehearse,
                                            screenshot_path=screenshot_path, before_submit=before_submit)


class DraisBooker:
    """SpeakeasyGo manager: add name + party to the listing date, verify the
    row appears, never add a name that is already there."""

    LIST_URL = "https://manager.speakeasygo.com/guest-details?date={d}"

    def add(self, listing_date, first, last, male, female, *, rehearse, screenshot_path, before_submit):
        import speakeasy
        from playwright.sync_api import sync_playwright
        name = f"{first} {last}".strip()
        url = self.LIST_URL.format(d=listing_date)
        with sync_playwright() as p:
            browser = speakeasy._browser(p)
            page = browser.new_context(locale="en-US").new_page()
            page.set_default_timeout(20000)
            try:
                speakeasy._login(page)
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                before = page.locator("body").inner_text()
                if name.lower() in before.lower():
                    raise Stop("manual", f"{name} is already on the Drai's {listing_date} list; not adding twice")
                if rehearse:
                    return {"rehearsal": True, "screenshot": _shot(page, screenshot_path)}
                speakeasy.MANAGER_URL = url
                original_goto = speakeasy._goto_date
                speakeasy._goto_date = lambda _page, _target: True  # the URL already selects the date
                try:
                    before_submit()
                    speakeasy._add_one(page, listing_date, first, last, male, female)
                finally:
                    speakeasy._goto_date = original_goto
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                after = page.locator("body").inner_text()
                shot = _shot(page, screenshot_path)
                if name.lower() not in after.lower():
                    raise Stop("uncertain", f"Saved {name} on Speakeasy {listing_date} but the row is not visible")
                row = next((ln.strip() for ln in after.splitlines() if name.lower() in ln.lower()), name)
                return {"verified": True, "row": row[:200], "screenshot": shot}
            finally:
                browser.close()


def _shot(page, path):
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def _sha256(path):
    if not path or not os.path.exists(path):
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- Job processing -----------------------------------------------------------

def source_nights(raw):
    from config import rules
    from rules_engine import date_range
    venues = [rules.normalize_venue_name(v) for v in raw.get("venues") or []]
    nights = [d.isoformat() for d in date_range(raw["start_date"], raw["end_date"])]
    return [(v, n) for n in nights for v in venues]


def drais_offset():
    """Days between the guest's night and SpeakeasyGo's listing date. Must be
    set explicitly (DRAIS_SPEAKEASY_DAY_OFFSET); None => Drai's goes manual."""
    value = os.environ.get("DRAIS_SPEAKEASY_DAY_OFFSET", "").strip()
    try:
        return int(value) if value else None
    except ValueError:
        return None


class Worker:
    def __init__(self, api, ledger, tao=None, drais=None, *, mode="dry-run", evidence_dir=None,
                 now=None, sleep=time.sleep, email_wait_seconds=None, duplicate_check=None, alerts=None):
        self.api, self.ledger, self.mode = api, ledger, mode
        self.tao, self.drais = tao or TaoBooker(), drais or DraisBooker()
        self.evidence_dir = Path(evidence_dir or os.environ.get("AMY_WORKER_DIR", "/data/worker")) / "evidence"
        self.now, self.sleep = now or time.time, sleep
        self.email_wait = (int(os.environ.get("AMY_WORKER_EMAIL_WAIT_SECONDS", "900"))
                           if email_wait_seconds is None else email_wait_seconds)
        self.duplicate_check = (os.environ.get("AMY_WORKER_DUPLICATE_CHECK", "true").lower() == "true"
                                if duplicate_check is None else duplicate_check)
        self.alerts = (os.environ.get("AMY_WORKER_ALERTS", "").lower() == "true" if alerts is None else alerts)

    # -- one cycle --
    def run_once(self):
        if self.mode == "live":
            return self._live_once()
        return self._dry_run_once()

    def _dry_run_once(self):
        """Read-only: rehearse the soonest future queued job, stop before submit."""
        import queue_service
        code, body = self.api.jobs("queued", 100)
        if code != 200:
            return {"status": "error", "reason": f"queue read HTTP {code}"}
        today = datetime.fromtimestamp(self.now(), PHOENIX).date()
        jobs = sorted(body.get("jobs") or [], key=lambda j: ((j.get("raw") or {}).get("start_date") or "9999", j.get("created") or 0))
        for job in jobs:
            raw = job.get("raw") or {}
            if queue_service.source_policy(job["ref"], raw, today):
                continue
            try:
                result = self._book_all(job, raw, rehearse=True)
            except Stop as stop:
                return {"status": "rehearsal_stopped", "ref": job["ref"], "action": stop.action, "reason": stop.reason}
            return {"status": "rehearsed", "ref": job["ref"], "nights": result}
        return {"status": "idle"}

    def _live_once(self):
        code, body = self.api.claim()
        if code != 200:
            return {"status": "claim_refused", "http": code, "reason": (body or {}).get("error") if isinstance(body, dict) else body}
        jobs = body.get("jobs") or []
        if not jobs:
            return {"status": "idle"}
        job = jobs[0]
        if job.get("state") != "assigned" or not job.get("claim_token"):
            return {"status": "not_assigned", "ref": job.get("ref"), "state": job.get("state")}
        return self.process(job)

    # -- one job --
    def process(self, job):
        ref, token, raw = job["ref"], job["claim_token"], job.get("raw") or {}
        try:
            result = self._process(job, ref, token, raw)
            return result
        except Stop as stop:
            meta = {"worker": "railway", **stop.extra}
            if stop.action == "retry":
                meta["submission_attempted"] = False
            code, body = self.api.update(ref, token, stop.action, meta, stop.reason)
            log.warning("Job %s -> %s: %s (HTTP %s)", ref, stop.action, stop.reason, code)
            if stop.action in ("manual", "uncertain"):
                self._alert(ref, stop.action, stop.reason, raw)
            return {"status": stop.action, "ref": ref, "reason": stop.reason, "http": code}
        except Exception as exc:  # noqa: BLE001 - never loop on an unknown failure
            log.exception("Job %s failed unexpectedly", ref)
            attempted = any((self.ledger.get(night_key(ref, v, n)) or {}).get("state")
                            for v, n in self._safe_nights(raw))
            action = "uncertain" if attempted else "manual"
            reason = f"Worker error ({type(exc).__name__}: {str(exc)[:300]}); not retried"
            self.api.update(ref, token, action, {"worker": "railway"}, reason)
            self._alert(ref, action, reason, raw)
            return {"status": action, "ref": ref, "reason": reason}

    @staticmethod
    def _safe_nights(raw):
        try:
            return source_nights(raw)
        except Exception:  # noqa: BLE001
            return []

    def _process(self, job, ref, token, raw):
        import queue_service
        today = datetime.fromtimestamp(self.now(), PHOENIX).date()
        reason = queue_service.source_policy(ref, raw, today)
        if reason:
            raise Stop("manual", reason)
        if self.duplicate_check:
            self._check_duplicates(ref, raw)
        bookings = self._book_all(job, raw, rehearse=False, token=token)
        from config import rules
        drais = any(rules.is_drais(v) for v in raw.get("venues") or [])
        evidence = {"verified": True, "worker": "railway", "bookings": [b["record"] for b in bookings]}
        if drais:
            evidence["no_guest_or_team_email"] = True
        else:
            evidence["email_receipts"] = self._send_emails(job, ref, token, raw, bookings)
        code, body = self.api.update(ref, token, "complete", {"evidence": evidence}, "Booked by Railway worker")
        if code != 200:
            raise Stop("manual", f"Booked and emailed but completion refused (HTTP {code}: "
                       f"{(body or {}).get('error') if isinstance(body, dict) else body}); DO NOT rebook",
                       evidence=evidence)
        log.info("Job %s complete: %s", ref, [b["record"].get("order_id") or b["record"].get("entry_id") for b in bookings])
        return {"status": "done", "ref": ref, "evidence": evidence}

    def _check_duplicates(self, ref, raw):
        """Same guest email + venue + night already completed in the queue."""
        code, body = self.api.jobs("done", 100)
        if code != 200:
            return
        mine = set(self._safe_nights(raw))
        email = (raw.get("email") or "").strip().lower()
        for other in body.get("jobs") or []:
            oraw = other.get("raw") or {}
            if other.get("ref") == ref or (oraw.get("email") or "").strip().lower() != email:
                continue
            overlap = mine & set(self._safe_nights(oraw))
            if overlap:
                v, n = sorted(overlap)[0]
                raise Stop("manual", f"Duplicate suspicion: {email} already booked {v} on {n} (job {other['ref']})")

    def _book_all(self, job, raw, *, rehearse, token=None):
        from config import rules
        from rules_engine import ActionNeeded, normalize_guest_request
        try:
            request = normalize_guest_request(raw)
        except ActionNeeded as exc:
            raise Stop("manual", exc.issue)
        except (KeyError, ValueError, TypeError) as exc:
            raise Stop("manual", f"Source request malformed ({exc})")
        progress = ((job.get("metadata") or {}).get("progress") or {}).get("bookings") or {}
        out = []
        self.tao.reset()
        nights = source_nights(raw)
        for venue, night in nights:
            key = night_key(job["ref"], venue, night)
            saved = progress.get(f"{venue}|{night}") or (self.ledger.get(key) or {}).get("record")
            if saved and saved.get("verified"):
                out.append({"record": saved, "meta": (self.ledger.get(key) or {}).get("meta", {})})
                continue
            if not rehearse and (self.ledger.get(key) or {}).get("state") == "submitting":
                raise Stop("uncertain", f"{venue} {night}: a submit started earlier with no confirmed result; "
                           "check TAO/Speakeasy before any resubmit")
            if rules.is_drais(venue):
                item = self._book_drais(job["ref"], venue, night, request, rehearse, token)
            else:
                item = self._book_tao(job["ref"], venue, night, request, rehearse, token, partial=bool(out))
            out.append(item)
        return out

    def _shot_path(self, ref, venue, night, suffix):
        safe = "".join(c if c.isalnum() else "-" for c in venue)
        return str(self.evidence_dir / ref / f"{night}-{safe}-{suffix}.png")

    def _checkpoint(self, ref, token, key, entry):
        self.ledger.put(key, entry)
        if token:
            self.api.update(ref, token, "progress", {"progress": {"bookings": {key.split("|", 1)[1]: entry.get("record")}}}
                            if entry.get("record") else {"progress": {"submitting": {key.split("|", 1)[1]: True}}})

    def _book_tao(self, ref, venue, night, request, rehearse, token, partial):
        import tao_portal
        key = night_key(ref, venue, night)
        night_date = date.fromisoformat(night)
        posted = self.tao.posted(venue, night_date)
        if posted is False:
            if partial or rehearse:
                raise Stop("manual", f"{venue} {night}: no guest list posted on TAO yet (other nights already booked)")
            raise Stop("retry", f"{venue} {night}: guest list not posted on TAO yet; nothing submitted")
        try:
            listing = self.tao.listing(venue, night_date)
        except tao_portal.TaoBlocked as exc:
            raise Stop("manual", f"{venue} {night}: {exc}; nothing submitted (waited for Cloudflare to clear)")
        if not listing:
            raise Stop("manual", f"{venue} {night}: no live free guest list (closed, sold out or not free); nothing submitted")
        if listing.get("listing_type") != "Passes" or listing.get("price", 0) != 0:
            raise Stop("manual", f"{venue} {night}: listing is not a free guest-list pass")
        party = {"male_count": int(request["male_count"]), "female_count": int(request["female_count"])}
        shot = self._shot_path(ref, venue, night, "rehearsal" if rehearse else "confirmation")

        def before_submit():
            if not rehearse:
                self._checkpoint(ref, token, key, {"state": "submitting"})

        try:
            result = self.tao.submit(listing, request, rehearse=rehearse, screenshot_path=shot, before_submit=before_submit)
        except tao_portal.TaoBlocked as exc:
            raise Stop("manual", f"{venue} {night}: {exc}; nothing submitted")
        except tao_portal.SubmissionUncertain as exc:
            raise Stop("uncertain", f"{venue} {night}: submitted once but no confirmation ({exc}); check TAO, do not resubmit")
        if rehearse:
            if not result.get("rehearsal"):
                raise Stop("manual", f"{venue} {night}: rehearsal stopped: {result.get('reason')}")
            return {"venue": venue, "date": night, "rehearsal": True, "form_filled": True,
                    "event": listing.get("event"), "screenshot": result.get("screenshot"),
                    "submit_button": result.get("submit_button"), "page_url": result.get("page_url")}
        if result.get("assisted_job"):
            raise Stop("manual", f"{venue} {night}: assisted mode is on; worker never hands off. Nothing submitted")
        if not result.get("verified") or not result.get("confirmation_id"):
            raise Stop("manual", f"{venue} {night}: not submitted — {result.get('reason', 'could not be verified')}")
        record = {"verified": True, "venue": venue, "date": night, "order_id": result["confirmation_id"], "party": party}
        meta = {"event": result.get("event") or listing.get("event"), "event_time": listing.get("event_time"),
                "confirmation_url": result.get("confirmation_url"), "screenshot": result.get("screenshot"),
                "screenshot_sha256": _sha256(result.get("screenshot"))}
        self._checkpoint(ref, token, key, {"state": "booked", "record": record, "meta": meta})
        return {"record": record, "meta": meta}

    def _book_drais(self, ref, venue, night, request, rehearse, token):
        from datetime import timedelta
        if os.environ.get("AMY_WORKER_DRAIS", "").lower() != "true":
            raise Stop("manual", "Drai's: worker Speakeasy booking is off (AMY_WORKER_DRAIS); add on Speakeasy by hand")
        offset = drais_offset()
        if offset is None:
            raise Stop("manual", "Drai's: DRAIS_SPEAKEASY_DAY_OFFSET not configured; listing date would be a guess")
        listing_date = (date.fromisoformat(night) + timedelta(days=offset)).isoformat()
        key = night_key(ref, venue, night)
        first, last = request["first_name"].strip(), (request.get("last_name") or "").strip()
        male, female = int(request["male_count"]), int(request["female_count"])
        shot = self._shot_path(ref, "drais", night, "rehearsal" if rehearse else "after")
        result = self.drais.add(listing_date, first, last, male, female, rehearse=rehearse, screenshot_path=shot,
                                before_submit=lambda: self._checkpoint(ref, token, key, {"state": "submitting"}))
        if rehearse:
            return {"venue": venue, "date": night, "speakeasy_date": listing_date, "rehearsal": True,
                    "screenshot": result.get("screenshot")}
        record = {"verified": True, "venue": venue, "date": night, "system": "speakeasy",
                  "entry_id": f"speakeasy:{listing_date}:{result['row']}",
                  "party": {"male_count": male, "female_count": female},
                  "submitted_fields": ["first_name", "last_name", "male_count", "female_count"]}
        meta = {"speakeasy_date": listing_date, "screenshot": result.get("screenshot"),
                "screenshot_sha256": _sha256(result.get("screenshot"))}
        self._checkpoint(ref, token, key, {"state": "booked", "record": record, "meta": meta})
        return {"record": record, "meta": meta}

    def _send_emails(self, job, ref, token, raw, bookings):
        import worker_emails
        from rules_engine import normalize_guest_request
        request = normalize_guest_request(raw)
        rows = [{**b["record"], **(b.get("meta") or {})} for b in bookings]
        saved = ((job.get("metadata") or {}).get("progress") or {}).get("emails") or {}
        saved.update((self.ledger.get(f"{ref}|emails") or {}).get("receipts") or {})
        receipts = []
        for kind, payload in (("guest", worker_emails.guest_email(request, rows)),
                              ("team", worker_emails.team_email(ref, raw, request, rows))):
            if saved.get(kind):
                receipts.append({"kind": kind, "id": saved[kind]})
                continue
            if not worker_emails.allowed_recipient(payload["to"]):
                raise Stop("manual", f"{kind} email recipient blocked; bookings done, DO NOT rebook")
            message_id = self._send_one(kind, payload)
            saved[kind] = message_id
            self.ledger.put(f"{ref}|emails", {"receipts": dict(saved)})
            self.api.update(ref, token, "progress", {"progress": {"emails": {kind: message_id}}})
            receipts.append({"kind": kind, "id": message_id})
        return receipts

    def _send_one(self, kind, payload):
        deadline = self.now() + self.email_wait
        while True:
            code, body = self.api.send_branded(payload)
            result = (body or {}).get("result") if isinstance(body, dict) else None
            if code == 200 and isinstance(result, dict) and result.get("sent") and result.get("id"):
                return result["id"]
            if code == 200 and isinstance(result, str) and "duplicate" in result.lower():
                raise Stop("manual", f"{kind} email: /send-branded reports it was already sent; find the prior "
                           "message ID before completing. Bookings done, DO NOT rebook")
            if code == 429 and self.now() + 120 <= deadline:
                log.info("send-branded hourly cap reached; waiting 2 min before sending the %s email", kind)
                self.sleep(120)
                continue
            raise Stop("manual", f"{kind} email not sent (HTTP {code}: {str(result or body)[:200]}); "
                       "bookings done, DO NOT rebook")

    def _alert(self, ref, state, reason, raw):
        if not self.alerts:
            return
        import worker_emails
        payload = worker_emails.alert_email(ref, state, reason, raw)
        if worker_emails.allowed_recipient(payload["to"]):
            try:
                self.api.send_branded(payload)
            except Exception:  # noqa: BLE001
                log.exception("Exception alert email failed for %s", ref)


# --- Entry point -------------------------------------------------------------

def check_config(mode):
    problems = []
    if not os.environ.get("AMY_QUEUE_API_TOKEN"):
        problems.append("AMY_QUEUE_API_TOKEN is required")
    if mode == "live" and not os.environ.get("BRANDED_SEND_TOKEN"):
        problems.append("BRANDED_SEND_TOKEN is required in live mode")
    if os.environ.get("AMY_SIGNUP_MODE", "").strip().lower() == "assisted":
        problems.append("AMY_SIGNUP_MODE=assisted must be unset for the worker")
    if mode == "live" and os.environ.get("AMY_WORKER_LIVE_CONFIRM", "") != "yes":
        problems.append("live mode also needs AMY_WORKER_LIVE_CONFIRM=yes (explicit go-live switch)")
    return problems


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--loop", action="store_true", help="keep running")
    parser.add_argument("--mode", choices=("dry-run", "live"), default=None)
    args = parser.parse_args(argv)
    mode = args.mode or os.environ.get("AMY_WORKER_MODE", "dry-run")
    if mode not in ("dry-run", "live"):
        sys.exit("AMY_WORKER_MODE must be dry-run or live")
    # Cloudflare: wait it out (default ~5 min), never click or bypass it.
    os.environ.setdefault("TAO_CHALLENGE_RETRIES", "15")
    os.environ.setdefault("TAO_CHALLENGE_WAIT_SECONDS", "20")
    problems = check_config(mode)
    if problems:
        sys.exit("; ".join(problems))
    work_dir = Path(os.environ.get("AMY_WORKER_DIR", "/data/worker"))
    worker = Worker(Api.from_env(), Ledger(work_dir / "ledger.json"), mode=mode, evidence_dir=work_dir)
    idle = max(int(os.environ.get("AMY_WORKER_IDLE_SECONDS", "120")), 30)
    log.info("AMY booking worker started in %s mode", mode)
    while True:
        try:
            result = worker.run_once()
        except Exception:  # noqa: BLE001
            log.exception("Worker cycle failed")
            result = {"status": "error"}
        log.info("cycle: %s", json.dumps(result, default=str)[:2000])
        if not args.loop:
            return result
        # Busy -> go straight to the next job; idle/dry-run -> wait.
        time.sleep(5 if result.get("status") in ("done",) else idle if mode == "live" else max(idle, 1800))


if __name__ == "__main__":
    main()

"""Assisted sign-up jobs: Amy prepares, Thomas's own computer finishes.

TAO's guest-list pages sit behind a Cloudflare "are you human" check. Amy
(on Railway) never tries to get past it. In assisted mode
(AMY_SIGNUP_MODE=assisted) Amy instead packs each sign-up into a small
"job" and emails it to Thomas (ASSISTED_SIGNUP_TO). The email carries one
ready-to-paste command. Thomas runs it on his computer; a normal, visible
Chrome window opens, Thomas clicks the human check himself, and only then
does the helper (tools/assisted_signup.py) fill in and submit the form.

The job travels inside the command as base64url JSON, so there is no
server, queue, or shared file to keep in sync. Pure functions only; no
network, no browser.
"""
import base64
import json
import os
import shlex

JOB_VERSION = 1
FIELDS = ("url", "first_name", "last_name", "email", "phone", "female_count",
          "male_count", "billing_zip", "notes", "venue", "date", "event", "ref")


def signup_mode():
    return os.environ.get("AMY_SIGNUP_MODE", "").strip().lower()


def assisted_on():
    return signup_mode() == "assisted"


def recipient(default=None):
    return os.environ.get("ASSISTED_SIGNUP_TO", "").strip() or default


def make_job(url, guest, venue=None, date=None, event=None, ref=None, notes=None):
    """Build a job dict from a sign-up URL and a guest/request dict."""
    import ticketsauce
    job = {
        "v": JOB_VERSION,
        "url": ticketsauce.promoter_link(url),
        "first_name": guest.get("first_name", ""),
        "last_name": guest.get("last_name", ""),
        "email": guest.get("email", ""),
        "phone": guest.get("phone") or "",
        "female_count": int(guest.get("female_count") or 0),
        "male_count": int(guest.get("male_count") or 0),
        "billing_zip": guest.get("billing_zip") or "",
        "notes": notes or guest.get("notes") or "",
        "venue": venue or "", "date": str(date or ""), "event": event or "", "ref": ref or "",
    }
    validate(job)
    return job


def validate(job):
    missing = [k for k in ("url", "first_name", "last_name", "email") if not job.get(k)]
    if missing:
        raise ValueError(f"Sign-up job is missing: {', '.join(missing)}")
    if job["female_count"] + job["male_count"] < 1:
        raise ValueError("Sign-up job has a party size of 0")
    return job


def encode(job):
    raw = json.dumps({k: job.get(k) for k in ("v",) + FIELDS}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(token):
    token = token.strip()
    job = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode())
    if job.get("v") != JOB_VERSION:
        raise ValueError(f"Unsupported job version {job.get('v')}")
    return validate(job)


def command(job):
    """The exact line Thomas pastes into a terminal in his ami folder."""
    return f"python tools/assisted_signup.py --job {shlex.quote(encode(job))}"


def job_email(jobs, guest_name):
    """(subject, body) for the email that hands jobs to Thomas."""
    subject = f"Amy sign-up: {guest_name} ({len(jobs)} night{'s' if len(jobs) != 1 else ''})"
    lines = [
        "Run each command below in your ami folder. A Chrome window opens; if you see the",
        "Cloudflare check, click it yourself. Amy then fills in and submits the form.",
        "",
    ]
    for job in jobs:
        party = f"{job['female_count']} women, {job['male_count']} men"
        lines += [f"{job['date']} — {job['venue'] or job['event']} — {party}",
                  f"    {command(job)}", ""]
    lines.append("Nothing has been submitted yet.")
    return subject, "\n".join(lines)

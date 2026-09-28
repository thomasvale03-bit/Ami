"""
Amy — Playmaker Entertainment guest-list processing agent.

Run modes:
    python main.py --dry-run   # parse + decide + draft output, no live TAO
                                # submission, no email sent (safe to test with)
    python main.py --live      # actually submits to TAO and sends confirmations

Intended deployment: a scheduled job (cron / cloud scheduler) that runs this
every few minutes. See README.md for hosting options.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from rules_engine import (
    normalize_guest_request, ActionNeeded, date_range,
    resolve_dayclub_for_date, resolve_venue_for_date,
)
from config import rules
from templates.emails import consolidated_confirmation, internal_processed_record, internal_action_needed
import gmail_client
import posh
import tao_portal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amy")


VEGAS = ZoneInfo("America/Los_Angeles")


def event_has_started(listing, date_obj, now):
    """True when the listing is for today and its start time (e.g. "11:00 AM")
    is already past in Las Vegas. Unknown start times never count as started."""
    if not listing or date_obj != now.date() or not listing.get("event_time"):
        return False
    try:
        start = datetime.strptime(listing["event_time"].replace(" ", "").upper(), "%I:%M%p").time()
    except ValueError:
        return False
    return now.time() >= start


def process_one_request(raw, dry_run=True, today=None, now=None):
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        return {"status": "action_needed", "issue": e.issue, "required_action": e.required_action}

    registrations = []
    exceptions = []
    previous_night_venue = None

    def checker(venue, date_obj):
        if dry_run:
            # Simulated listing so routing logic can be exercised without
            # touching the live TAO site.
            return {"event": "[simulated]", "listing_type": "Passes"}
        return tao_portal.check_availability(venue, date_obj)

    def book(venue, listing, date_obj, category):
        """Register one venue/date. Returns True only for a verified booking."""
        if dry_run:
            registrations.append({
                "venue": venue, "category": category, "date": date_obj.isoformat(),
                "event": ("[only if TAO lists a free dayclub Pass that day]" if category == "dayclub"
                          else "[would be read from live listing]"),
                "event_time": None,
                "female_count": request["female_count"], "male_count": request["male_count"],
                "arrival_text": "[would be read from live listing]",
                "confirmation_id": "[DRY RUN — not submitted]",
            })
            return True
        try:
            result = tao_portal.submit_registration(listing, request)
        except tao_portal.SubmissionUncertain as exc:
            # The order may exist at TAO: flag it, never retry automatically.
            exceptions.append({"date": date_obj.isoformat(), "issue": (
                f"{venue}: submitted but success could not be confirmed ({exc}). "
                "Check TAO for this guest before re-submitting.")})
            return False
        if not result.get("verified"):
            exceptions.append({"date": date_obj.isoformat(), "issue": (
                f"{venue}: not submitted — {result.get('reason', 'could not be verified')}.")})
            return False
        log.info("Registered %s at %s on %s (order %s) under: %s", request["email"], venue,
                 date_obj, result["confirmation_id"], result.get("authorization"))
        registrations.append({
            "venue": venue, "category": category, "date": date_obj.isoformat(),
            "event": result.get("event") or listing.get("event", ""),
            "event_time": result.get("event_time") or listing.get("event_time"),
            "female_count": request["female_count"], "male_count": request["male_count"],
            "arrival_text": result.get("arrival_text") or listing.get("arrival_text", ""),
            "confirmation_id": result["confirmation_id"],
        })
        return True

    now = now or datetime.now(VEGAS)
    today = today or now.date()

    if request["end_date"] < today.isoformat():
        issue = f"Every requested night ({request['start_date']} to {request['end_date']}) has already passed."
        if raw.get("source") == "Posh":
            issue += (" For a Posh signup this usually means a recurring Posh event reporting its "
                      "first date, so the night the guest wants is unknown.")
        return {"status": "action_needed", "issue": issue,
                "required_action": "Confirm which night the guest wants and register manually."}

    def dayclub_checker(venue, date_obj):
        # A dayclub that has already started today is skipped (the next
        # dayclub is tried). Nightclubs are not: their guest lists stay
        # usable until the late arrival cutoff.
        listing = checker(venue, date_obj)
        if event_has_started(listing, date_obj, now):
            log.info("Skipping %s on %s: it started at %s", venue, date_obj, listing["event_time"])
            return None
        return listing
    for date_obj in date_range(request["start_date"], request["end_date"]):
        if date_obj < today:
            log.info("Skipping %s: that night has already passed", date_obj)
            continue

        # Dayclub: an extra registration when a free dayclub Pass is live.
        dayclub, day_listing = resolve_dayclub_for_date(request, date_obj, dayclub_checker)
        if dayclub:
            book(dayclub, day_listing, date_obj, "dayclub")

        venue, listing = resolve_venue_for_date(
            request, date_obj, live_availability_checker=checker,
            previous_night_venue=previous_night_venue,
        )
        if not venue:
            previous_night_venue = None
            exceptions.append({
                "date": date_obj.isoformat(),
                "issue": "No matching live Passes/Guest List event for requested or routed nightclubs.",
            })
            continue
        # Only a confirmed nightclub counts for the Fri/Sat no-repeat rule.
        previous_night_venue = venue if book(venue, listing, date_obj, "nightclub") else None

    output = {"request": request, "registrations": registrations, "exceptions": exceptions}

    if registrations:
        date_label = request["start_date"] if request["start_date"] == request["end_date"] \
            else f"{request['start_date']} to {request['end_date']}"
        subject, body = consolidated_confirmation(
            request["first_name"], request["email"], date_label, registrations
        )
        output["confirmation_email"] = {"subject": subject, "body": body}
        output["internal_log"] = internal_processed_record(
            f"{request['first_name']} {request['last_name']}", request["email"],
            registrations, request.get("promoter"), "Not sent (dry run)" if dry_run else "Pending send",
        )

    for exc in exceptions:
        output.setdefault("action_needed_records", []).append(
            internal_action_needed(
                f"{request['first_name']} {request['last_name']}", request["email"],
                venue=", ".join(request["requested_venues"]) or "(none specified)",
                date=exc["date"], issue=exc["issue"],
                required_action="Review live TAO availability manually or adjust routing.",
            )
        )

    return output


def team_alert(service, message_id, raw, problems, dry_run):
    """One "needs attention" email to the team per source message, only when
    a person has to look at something."""
    name = raw.get("name") or raw.get("email") or "Unknown guest"
    dates = f"{raw.get('start_date', '?')} to {raw.get('end_date', '?')}"
    body = "\n\n".join(problems) + f"\n\nGmail message ID: {message_id}"
    subject = f"Amy — needs attention: {name}, {dates}"
    if dry_run:
        log.info("[dry run] would email the team: %s\n%s", subject, body)
        return
    gmail_client.send_once(
        service, f"amy-alert-{message_id}@playmakerentertainment.com",
        rules.PLAYMAKER_EMAIL, subject, body, sender=rules.INTAKE_EMAIL,
    )


def posh_summary(raw):
    """Posh order details plus the nightclub Amy would book, for the team."""
    p = raw.get("posh", {})
    lines = ["", "", "POSH ORDER",
             f"Event: {p.get('event_name') or '(none)'}",
             f"Event start: {p.get('event_start') or '(none)'}",
             f"Ticket: {p.get('ticket') or '(none)'}",
             f"Order number: {p.get('order_number') or '(none)'}",
             f"Phone: {p.get('phone') or '(none)'}"]
    if raw.get("start_date"):
        from datetime import date
        from rules_engine import candidate_venues
        night = date.fromisoformat(raw["start_date"])
        plan = candidate_venues({"requested_venues": raw.get("venues") or []}, night)
        lines.append(f"Amy's nightclub plan for {night:%a %b %d}: {plan[0] if plan else '(none)'}"
                     f" (then {', '.join(plan[1:3])} if unavailable)")
    return "\n".join(lines)


def handle_message(service, message_id, dry_run, labels, allowlist=None):
    """Process one request email end to end. Returns the outcome label, or
    None when test mode skips a request that isn't from an allowlisted
    address (it is left completely untouched)."""
    msg, body = gmail_client.get_plain_text_body(service, message_id)
    if posh.is_posh_signup(gmail_client.get_subject(msg or {}), body):
        raw = posh.parse_signup(message_id, body)
        order = raw["posh"].get("order_number")
        log.info("%s: source POSH, order %s, event %r, ticket %r", message_id, order or "(none)",
                 raw["posh"].get("event_name"), raw["posh"].get("ticket"))
    else:
        raw = gmail_client.parse_request(message_id, body)

    if allowlist is not None:
        guest = (raw.get("email") or "").lower()
        if guest not in allowlist and not (raw.get("_missing_required")
                                           and any(a in body.lower() for a in allowlist)):
            log.info("[test mode] leaving %s untouched (not from an allowlisted address)", message_id)
            return None

    if raw.get("source") == "Posh":
        order = raw["posh"].get("order_number")
        if gmail_client.posh_order_already_handled(service, order, message_id):
            log.info("%s: POSH order %s was already processed; not booking it again", message_id, order)
            return gmail_client.PROCESSED_LABEL

    if raw.get("_missing_required"):
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"), venue="(n/a)", date="(n/a)",
            issue=f"Request is missing required fields: {', '.join(raw['_missing_required'])}.",
            required_action="Read the original email and process manually.",
        )], dry_run)
        return gmail_client.EXCEPTION_LABEL

    if raw.get("_action_needed") or (raw.get("source") == "Posh" and not posh.consent_on_file()):
        issue, action = raw.get("_action_needed") or (
            "Posh signup: no documented 21+ confirmation and guest-list authorization "
            "(required by the TAO authorization) — not booked automatically.",
            "Register manually if appropriate, or set POSH_CONSENT_ON_FILE=true once the "
            "Posh checkout collects that consent.")
        details = posh_summary(raw) if raw.get("source") == "Posh" else ""
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
            venue=", ".join(raw.get("venues") or []) or "(none specified)",
            date=raw.get("start_date", "(unknown)"), issue=issue, required_action=action,
        ) + details], dry_run)
        return gmail_client.EXCEPTION_LABEL

    if not dry_run:
        gmail_client.set_labels(service, message_id, labels, add=[gmail_client.PROCESSING_LABEL])

    result = process_one_request(raw, dry_run=dry_run)

    if result.get("status") == "action_needed":
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
            venue=", ".join(raw.get("venues") or []) or "(none specified)",
            date=f"{raw.get('start_date')} to {raw.get('end_date')}",
            issue=result["issue"], required_action=result["required_action"],
        )], dry_run)
        return gmail_client.EXCEPTION_LABEL

    request = result["request"]
    if dry_run:
        print(json.dumps({
            "guest": f"{request['first_name']} {request['last_name']} <{request['email']}>",
            "routing": [(r["date"], r["venue"]) for r in result.get("registrations", [])],
            "consent": request.get("consent"),
            "problems": result.get("action_needed_records"),
        }, indent=2, default=str))

    if "confirmation_email" in result:
        email = result["confirmation_email"]
        if dry_run:
            print(f"[dry run] would email {request['email']} (cc {rules.PLAYMAKER_EMAIL}):")
            print(email["subject"])
            print(email["body"])
        else:
            gmail_client.send_once(
                service, f"amy-confirm-{message_id}@playmakerentertainment.com",
                request["email"], email["subject"], email["body"],
                sender=rules.INTAKE_EMAIL, cc=rules.PLAYMAKER_EMAIL,
            )

    if result.get("action_needed_records"):
        team_alert(service, message_id, raw, result["action_needed_records"], dry_run)
        return gmail_client.EXCEPTION_LABEL
    return gmail_client.PROCESSED_LABEL


def run_once(service, mode, labels, allowlist=None, start_after=None):
    dry_run = mode == "dry-run"
    pending = gmail_client.list_pending_requests(service, start_after=start_after)
    log.info("Found %d pending guest-list request(s) [%s]", len(pending), mode)

    for msg_ref in reversed(pending):  # oldest first
        message_id = msg_ref["id"]
        try:
            outcome = handle_message(service, message_id, dry_run, labels, allowlist)
        except Exception:
            log.exception("Failed while processing %s", message_id)
            if dry_run:
                continue
            # A TAO submission may already have gone through, so never let
            # this message be picked up and resubmitted automatically.
            outcome = gmail_client.EXCEPTION_LABEL
            team_alert(service, message_id, {}, [
                "Amy hit an error partway through this request. A TAO registration may or may "
                "not have gone through — check TAO before re-submitting."
            ], dry_run)
        if outcome is None:
            continue
        log.info("%s -> %s", message_id, outcome)
        if not dry_run:
            gmail_client.set_labels(
                service, message_id, labels,
                add=[gmail_client.PROCESSED_LABEL] + ([outcome] if outcome == gmail_client.EXCEPTION_LABEL else []),
                remove=[gmail_client.PROCESSING_LABEL],
            )


def main():
    parser = argparse.ArgumentParser()
    mode_flags = parser.add_mutually_exclusive_group()
    mode_flags.add_argument("--dry-run", action="store_true", help="Read and decide only")
    mode_flags.add_argument("--test", action="store_true",
                            help="Live, but only for requests from TEST_GUEST_EMAIL_ALLOWLIST")
    mode_flags.add_argument("--live", action="store_true", help="Book and email for every new request")
    parser.add_argument("--loop", action="store_true",
                        help="Keep running, checking the inbox every AMY_POLL_SECONDS (default 120)")
    args = parser.parse_args()

    # Flags win; otherwise AMY_MODE (how the server is configured); default dry-run.
    mode = ("dry-run" if args.dry_run else "test" if args.test else "live" if args.live
            else os.environ.get("AMY_MODE", "dry-run"))
    if mode not in ("dry-run", "test", "live"):
        sys.exit(f"AMY_MODE must be dry-run, test or live (got {mode!r}).")

    if mode != "dry-run" and not tao_portal.READY:
        sys.exit("Booking is disabled until tao_portal.READY is True.")

    allowlist = None
    if mode == "test":
        allowlist = {a.strip().lower() for a in os.environ.get("TEST_GUEST_EMAIL_ALLOWLIST", "").split(",") if a.strip()}
        if not allowlist:
            sys.exit("Test mode needs TEST_GUEST_EMAIL_ALLOWLIST (e.g. valeconsultingaz@gmail.com).")

    # Unix seconds; requests received earlier were handled by the previous
    # process and must never be booked again. Required for live mode.
    # Required for test and live: requests the previous process may already
    # have booked (including earlier test submissions) are never re-booked.
    start_after = os.environ.get("AMY_START_AFTER")
    if mode != "dry-run" and not start_after:
        sys.exit(f"{mode} mode needs AMY_START_AFTER (Unix time to start from) so old requests are not re-booked.")

    service = gmail_client.get_service()
    account = gmail_client.get_account_email(service)
    if account.lower() != rules.INTAKE_EMAIL:
        sys.exit(f"Wrong Gmail account: signed in as {account}, expected {rules.INTAKE_EMAIL}.")
    log.info("Amy started as %s in %s mode%s", account, mode,
             f" (allowlist: {', '.join(sorted(allowlist))})" if allowlist else "")

    labels = {}
    if mode != "dry-run":
        for name in (gmail_client.PROCESSING_LABEL, gmail_client.PROCESSED_LABEL, gmail_client.EXCEPTION_LABEL):
            labels[name] = gmail_client.get_or_create_label(service, name)

    poll_seconds = max(int(os.environ.get("AMY_POLL_SECONDS", "120")), 30)
    while True:
        try:
            run_once(service, mode, labels, allowlist, start_after)
        except Exception:
            if not args.loop:
                raise
            log.exception("Inbox check failed; will retry next cycle")
        if not args.loop:
            break
        tao_portal.reset_catalog()  # fresh promoter-page listings every cycle
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()

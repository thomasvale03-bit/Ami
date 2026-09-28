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
from datetime import datetime
from zoneinfo import ZoneInfo

from rules_engine import (
    normalize_guest_request, ActionNeeded, date_range,
    resolve_dayclub_for_date, resolve_venue_for_date,
)
from config import rules
from templates.emails import consolidated_confirmation, internal_processed_record, internal_action_needed
import gmail_client
import tao_portal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amy")


VEGAS = ZoneInfo("America/Los_Angeles")


def process_one_request(raw, dry_run=True, today=None):
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

    today = today or datetime.now(VEGAS).date()
    for date_obj in date_range(request["start_date"], request["end_date"]):
        if date_obj < today:
            log.info("Skipping %s: that night has already passed", date_obj)
            continue

        # Dayclub: an extra registration when a free dayclub Pass is live.
        dayclub, day_listing = resolve_dayclub_for_date(request, date_obj, checker)
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


def handle_message(service, message_id, dry_run, labels):
    """Process one request email end to end. Returns the outcome label."""
    _, body = gmail_client.get_plain_text_body(service, message_id)
    raw = gmail_client.parse_request(message_id, body)

    if raw.get("_missing_required"):
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"), venue="(n/a)", date="(n/a)",
            issue=f"Request is missing required fields: {', '.join(raw['_missing_required'])}.",
            required_action="Read the original email and process manually.",
        )], dry_run)
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


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read and decide only (default)")
    mode.add_argument("--live", action="store_true", help="Actually submit + send email")
    args = parser.parse_args()
    dry_run = not args.live

    if not dry_run and not tao_portal.READY:
        sys.exit("--live is disabled until tao_portal.py is filled in from a recorded TAO session.")

    # Unix seconds; requests received earlier were handled by the previous
    # process and must never be booked again. Required for --live.
    start_after = os.environ.get("AMY_START_AFTER")
    if not dry_run and not start_after:
        sys.exit("--live needs AMY_START_AFTER (Unix time of go-live) so old requests are not re-booked.")

    service = gmail_client.get_service()
    account = gmail_client.get_account_email(service)
    if account.lower() != rules.INTAKE_EMAIL:
        sys.exit(f"Wrong Gmail account: signed in as {account}, expected {rules.INTAKE_EMAIL}.")

    pending = gmail_client.list_pending_requests(service, start_after=start_after)
    log.info("Found %d pending guest-list request(s)%s", len(pending), " [DRY RUN]" if dry_run else "")

    labels = {}
    if not dry_run:
        for name in (gmail_client.PROCESSING_LABEL, gmail_client.PROCESSED_LABEL, gmail_client.EXCEPTION_LABEL):
            labels[name] = gmail_client.get_or_create_label(service, name)

    for msg_ref in reversed(pending):  # oldest first
        message_id = msg_ref["id"]
        try:
            outcome = handle_message(service, message_id, dry_run, labels)
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
        log.info("%s -> %s", message_id, outcome)
        if not dry_run:
            gmail_client.set_labels(
                service, message_id, labels,
                add=[gmail_client.PROCESSED_LABEL] + ([outcome] if outcome == gmail_client.EXCEPTION_LABEL else []),
                remove=[gmail_client.PROCESSING_LABEL],
            )


if __name__ == "__main__":
    main()

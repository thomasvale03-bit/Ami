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
import traceback
from datetime import datetime, timedelta, timezone

from config import rules
from rules_engine import (
    normalize_guest_request, ActionNeeded, date_range,
    resolve_venue_for_date,
)
from templates.emails import consolidated_confirmation, internal_processed_record, internal_action_needed
import gmail_client
import tao_portal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amy")

RECORDS_PATH = "amy_records.jsonl"


def dry_run_availability(venue, date_obj):
    """Stand-in for tao_portal.check_availability in dry runs: treats every
    venue as having a live Passes listing so the routing decision is visible."""
    return {
        "event": "[would be read from live listing]",
        "listing_type": "Passes",
        "arrival_text": "[would be read from live listing]",
    }


def process_one_request(raw, dry_run=True, availability_checker=None):
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        return {"status": "action_needed", "issue": e.issue, "required_action": e.required_action}

    if availability_checker is None:
        availability_checker = dry_run_availability if dry_run else tao_portal.check_availability

    registrations = []
    exceptions = []
    venue_by_date = {}

    for date_obj in date_range(request["start_date"], request["end_date"]):
        venue, listing = resolve_venue_for_date(
            request, date_obj,
            live_availability_checker=availability_checker,
            previous_night_venue=venue_by_date.get(date_obj - timedelta(days=1)),
        )
        if not venue:
            exceptions.append({
                "date": date_obj.isoformat(),
                "issue": "No matching live Passes/Guest List event for requested or routed venues.",
            })
            continue

        if dry_run:
            venue_by_date[date_obj] = venue
            registrations.append({
                "venue": venue, "event": listing.get("event", ""),
                "date": date_obj.isoformat(),
                "female_count": request["female_count"], "male_count": request["male_count"],
                "arrival_text": listing.get("arrival_text", ""),
                "confirmation_id": "[DRY RUN — not submitted]",
            })
            continue

        result = tao_portal.submit_registration(listing, request)
        if not result.get("verified"):
            exceptions.append({"date": date_obj.isoformat(), "issue": "Submission could not be verified."})
            continue

        venue_by_date[date_obj] = venue
        registrations.append({
            "venue": venue, "event": listing.get("event", ""), "date": date_obj.isoformat(),
            "female_count": request["female_count"], "male_count": request["male_count"],
            "arrival_text": listing.get("arrival_text", ""),
            "confirmation_id": result["confirmation_id"],
        })

    output = {"status": "ok", "request": request, "registrations": registrations, "exceptions": exceptions}

    if registrations:
        date_label = request["start_date"] if request["start_date"] == request["end_date"] \
            else f"{request['start_date']} to {request['end_date']}"
        subject, body = consolidated_confirmation(
            request["first_name"], request["email"], date_label, registrations
        )
        output["confirmation_email"] = {"subject": subject, "body": body}

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


def processed_record(result, customer_email_status):
    request = result["request"]
    return internal_processed_record(
        f"{request['first_name']} {request['last_name']}", request["email"],
        result["registrations"], request.get("promoter"), customer_email_status,
    )


def append_record(message_id, kind, text):
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
        "kind": kind,
        "record": text,
    }
    with open(RECORDS_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def handle_message(service, msg_ref, dry_run, labels):
    """Process one inbox message. Returns the label name applied (or that
    would be applied in a dry run)."""
    message_id = msg_ref["id"]
    _, body = gmail_client.get_plain_text_body(service, message_id)
    raw = gmail_client.parse_request(message_id, body)

    if raw.get("_missing_required"):
        text = internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"), venue="(n/a)", date="(n/a)",
            issue=f"Request is missing required fields: {', '.join(raw['_missing_required'])}.",
            required_action="Read the original email and process manually.",
        )
        records = [("action_needed", text)]
        label = gmail_client.EXCEPTION_LABEL
    else:
        result = process_one_request(raw, dry_run=dry_run)
        records = []

        if result["status"] == "action_needed":
            text = internal_action_needed(
                raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
                venue=", ".join(raw.get("venues") or []) or "(none specified)",
                date=f"{raw.get('start_date')} to {raw.get('end_date')}",
                issue=result["issue"], required_action=result["required_action"],
            )
            records.append(("action_needed", text))
            label = gmail_client.EXCEPTION_LABEL
        else:
            if result.get("confirmation_email"):
                if dry_run:
                    email_status = "Not sent (dry run)"
                    log.info("Would send to %s:\n%s\n\n%s", result["request"]["email"],
                             result["confirmation_email"]["subject"], result["confirmation_email"]["body"])
                else:
                    gmail_client.send_email(
                        service, result["request"]["email"],
                        result["confirmation_email"]["subject"], result["confirmation_email"]["body"],
                        sender=rules.INTAKE_EMAIL,
                    )
                    email_status = "Sent"
                records.append(("processed", processed_record(result, email_status)))
            for text in result.get("action_needed_records", []):
                records.append(("action_needed", text))
            label = gmail_client.EXCEPTION_LABEL if result["exceptions"] else gmail_client.PROCESSED_LABEL

    for kind, text in records:
        log.info("%s record for %s:\n%s", kind, message_id, text)
        if not dry_run:
            append_record(message_id, kind, text)
            gmail_client.send_email(
                service, rules.TEAM_NOTIFICATION_EMAIL,
                f"Amy: {text.splitlines()[0]} — {raw.get('name') or raw.get('email') or message_id}",
                text, sender=rules.INTAKE_EMAIL,
            )

    if not dry_run:
        gmail_client.mark_processed(service, message_id, labels[label])
    return label


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Parse and decide only (default)")
    mode.add_argument("--live", action="store_true", help="Actually submit + send email")
    parser.add_argument("--token", default="token.json", help="Path to the Gmail OAuth token")
    args = parser.parse_args()
    dry_run = not args.live

    service = gmail_client.get_service(args.token)
    pending = gmail_client.list_pending_requests(service)
    log.info("Found %d pending guest-list request(s)%s", len(pending), " [DRY RUN]" if dry_run else "")

    labels = {}
    if not dry_run:
        for name in (gmail_client.PROCESSED_LABEL, gmail_client.EXCEPTION_LABEL):
            labels[name] = gmail_client.get_or_create_label(service, name)

    for msg_ref in pending:
        try:
            label = handle_message(service, msg_ref, dry_run, labels)
        except Exception:
            log.exception("Failed while processing %s", msg_ref["id"])
            if not dry_run:
                # A TAO submission may already have gone through, so never let
                # this message be picked up and resubmitted automatically.
                gmail_client.mark_processed(service, msg_ref["id"], labels[gmail_client.EXCEPTION_LABEL])
                gmail_client.send_email(
                    service, rules.TEAM_NOTIFICATION_EMAIL,
                    f"Amy: ACTION NEEDED — error processing message {msg_ref['id']}",
                    f"ACTION NEEDED\n\nAmy hit an error on Gmail message {msg_ref['id']} and did not finish it.\n"
                    "A TAO registration may or may not have gone through. Check TAO before re-submitting.\n\n"
                    f"Error:\n{traceback.format_exc()}",
                    sender=rules.INTAKE_EMAIL,
                )
            continue
        log.info("Processed %s -> %s", msg_ref["id"], label)


if __name__ == "__main__":
    main()

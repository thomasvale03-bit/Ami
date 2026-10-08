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
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rules_engine import (
    normalize_guest_request, ActionNeeded, date_range,
    resolve_dayclub_for_date, resolve_venue_for_date,
    candidate_venues, dayclub_candidates, is_dayclub_season, omnia_closed_for_same_day,
)
from config import rules
from templates.emails import (
    FOLLOW_UP_DELAY_DAYS, consolidated_confirmation, follow_up_email, internal_action_needed,
    internal_processed_record, manual_work_order, manual_confirmation_draft,
    guest_signup_links_email, guest_confirmation, jose_signup_order,
)
import gmail_client
import posh
import tao_portal
import headliners

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

    try:
        _book_nights(request, today, checker, dayclub_checker, book, exceptions)
    except tao_portal.TaoBlocked as exc:
        # Blocked before anything was read, so nothing was submitted.
        return {"status": "action_needed", "issue": f"Amy could not book: {exc}. Nothing was submitted.",
                "required_action": "Book this guest manually on TAO."}

    output = {"request": request, "registrations": registrations, "exceptions": exceptions}
    return _confirmation_and_records(output, request, today, dry_run)


def _book_nights(request, today, checker, dayclub_checker, book, exceptions):
    """Book each requested night (dayclub first, then the nightclub)."""
    previous_night_venue = None
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


def _confirmation_and_records(output, request, today, dry_run):
    registrations, exceptions = output["registrations"], output["exceptions"]
    drais_nights = [d.isoformat() for d in date_range(request["start_date"], request["end_date"])
                    if d >= today] if request.get("drais") else []
    if registrations or drais_nights:
        date_label = request["start_date"] if request["start_date"] == request["end_date"] \
            else f"{request['start_date']} to {request['end_date']}"
        subject, body = consolidated_confirmation(
            request["first_name"], request["email"], date_label, registrations,
            drais=drais_nights and {
                "name": f"{request['first_name']} {request['last_name']}".strip(),
                "female_count": request["female_count"], "male_count": request["male_count"],
                "nights": drais_nights,
            },
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
    a person has to look at something. Suppressed while concierge mode is on
    (the owner turned these off 2026-10-06)."""
    if concierge_on():
        log.info("[needs-attention off] not emailing the team about %s", message_id)
        return
    name = raw.get("name") or raw.get("email") or "Unknown guest"
    dates = f"{raw.get('start_date', '?')} to {raw.get('end_date', '?')}"
    body = "\n\n".join(problems) + f"\n\nGmail message ID: {message_id}"
    subject = f"Amy — needs attention: {name}, {dates}"
    if dry_run:
        log.info("[dry run] would email the team: %s\n%s", subject, body)
        return
    gmail_client.send_once(
        service, f"amy-alert-{message_id}@playmakerentertainment.com",
        rules.PLAYMAKER_EMAIL, subject, body, sender=rules.SENDER_EMAIL,
        dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} "Gmail message ID: {message_id}"',
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


def concierge_on():
    """AMY_CONCIERGE=true: Amy emails the guest a plain confirmation (no link,
    just the clubs they're set for) and a separate sign-up to-do to the team
    (Jose) to register them on TAO. Also turns off the old "needs attention"
    emails. The owner clicks nothing; the admin does the TAO signup."""
    return os.environ.get("AMY_CONCIERGE", "").strip().lower() in ("1", "true", "yes")


def concierge_handoff(service, message_id, raw, dry_run):
    """Guest confirmation + Jose sign-up order. No TAO, no needs-attention.
    Unprocessable or all-past requests are labeled and dropped quietly."""
    if raw.get("_missing_required"):
        log.info("%s: missing %s; concierge skip (no team alert)", message_id, raw["_missing_required"])
        return gmail_client.EXCEPTION_LABEL
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        log.info("%s: %s; concierge skip (no team alert)", message_id, e.issue)
        return gmail_client.EXCEPTION_LABEL

    today = datetime.now(VEGAS).date()
    # Read the master link (promoter page) to see what's actually live, so we
    # don't route to a club that's dark that night (e.g. JEWEL on Sunday) and
    # so we offer a dayclub when its free pass is live. {} if it can't be read.
    catalog = tao_portal.catalog_snapshot()
    live = bool(catalog)
    nights, prev = [], None
    for d in date_range(request["start_date"], request["end_date"]):
        if d < today:
            continue
        opts = candidate_venues(request, d, prev)
        rerouted_from = None
        if omnia_closed_for_same_day(d, today) and opts and opts[0] == "OMNIA Nightclub":
            rerouted_from = "OMNIA Nightclub"
            opts = [v for v in opts if v != "OMNIA Nightclub"]
        if not opts:
            continue
        # Prefer a club that shows a live guest list on the master link.
        chosen = next((v for v in opts if (v, d) in catalog), None) if live else None
        none_live = live and chosen is None
        if chosen is None:
            chosen = opts[0]
        # Dayclub: only when its free pass is actually live that day.
        dayclub = next((dv for dv in dayclub_candidates(request) if (dv, d) in catalog), None) if live else None
        nights.append({"date": d.isoformat(), "venue": chosen,
                       "backups": [v for v in opts if v != chosen][:3],
                       "dayclub": dayclub, "rerouted_from": rerouted_from,
                       "unverified": not live, "none_live": none_live,
                       "headliner": headliners.headliner(chosen, d)})
        prev = chosen

    drais_nights = [n["date"] for n in nights] if request.get("drais") else []
    if not nights and not drais_nights:
        log.info("%s: every requested night has passed; concierge skip", message_id)
        return gmail_client.EXCEPTION_LABEL

    drais = {"name": f"{request['first_name']} {request['last_name']}".strip(),
             "female_count": request["female_count"], "male_count": request["male_count"],
             "nights": drais_nights} if drais_nights else None
    gsub, gbody = guest_confirmation(request["first_name"], nights, drais=drais)
    tsub, tbody = jose_signup_order(request, nights, drais_nights)
    tbody += f"\n\nRef: {message_id}"

    if dry_run:
        print(f"[dry run] guest confirmation to {request['email']}:\n{gsub}\n{gbody}")
        print(f"[dry run] Jose sign-up to {rules.PLAYMAKER_EMAIL}:\n{tsub}\n{tbody}")
        return None

    gmail_client.send_once(
        service, f"amy-confirm-{message_id}@playmakerentertainment.com",
        request["email"], gsub, gbody, sender=rules.SENDER_EMAIL,
        dedupe_query=f'to:{request["email"]} subject:"{gsub}" newer_than:3d')
    gmail_client.send_once(
        service, f"amy-signup-{message_id}@playmakerentertainment.com",
        rules.PLAYMAKER_EMAIL, tsub, tbody, sender=rules.SENDER_EMAIL,
        dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} "Ref: {message_id}"')
    return gmail_client.PROCESSED_LABEL


def send_links_on():
    """AMY_SEND_LINKS=true: Amy emails the guest their direct free guest-list
    link for each night (picked by the rules). The guest taps it and signs
    themselves up, which clears TAO's check as a person and credits Playmaker."""
    return os.environ.get("AMY_SEND_LINKS", "").strip().lower() in ("1", "true", "yes")


def send_signup_links(service, message_id, raw, dry_run):
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
            venue=", ".join(raw.get("venues") or []) or "(none specified)",
            date=f"{raw.get('start_date')} to {raw.get('end_date')}",
            issue=e.issue, required_action=e.required_action)], dry_run)
        return gmail_client.EXCEPTION_LABEL

    today = datetime.now(VEGAS).date()
    if request["end_date"] < today.isoformat():
        team_alert(service, message_id, raw, [internal_action_needed(
            request["first_name"], request["email"], venue="(n/a)",
            date=f"{request['start_date']} to {request['end_date']}",
            issue="Every requested night has already passed.",
            required_action="Confirm the night with the guest and send the link manually.")], dry_run)
        return gmail_client.EXCEPTION_LABEL

    catalog = tao_portal.catalog_snapshot()  # {} if TAO's check is up
    nights, prev = [], None
    for d in date_range(request["start_date"], request["end_date"]):
        if d < today:
            continue
        options = candidate_venues(request, d, prev)
        chosen = url = None
        for venue in options:
            entry = catalog.get((venue, d))
            if entry:
                chosen, url = venue, entry["url"]
                break
        nights.append({"date": d.isoformat(), "venue": chosen or (options[0] if options else None),
                       "url": url, "options": options})
        prev = chosen or (options[0] if options else None)

    drais_nights = [n["date"] for n in nights] if request.get("drais") else []
    drais = {"name": f"{request['first_name']} {request['last_name']}".strip(),
             "female_count": request["female_count"], "male_count": request["male_count"],
             "nights": drais_nights} if drais_nights else None

    subject, body = guest_signup_links_email(
        request["first_name"], nights, rules.TAO_PROMOTER_URL, drais=drais)

    if dry_run:
        print(f"[dry run] guest links to {request['email']} (cc {rules.PLAYMAKER_EMAIL}):\n{subject}\n{body}")
        return None

    gmail_client.send_once(
        service, f"amy-links-{message_id}@playmakerentertainment.com",
        request["email"], subject, body, sender=rules.SENDER_EMAIL, cc=rules.PLAYMAKER_EMAIL)
    return gmail_client.PROCESSED_LABEL


def manual_booking_on():
    """AMY_MANUAL_BOOKING=true: Amy doesn't touch TAO. She emails the team a
    ready-to-book work order and saves a draft confirmation, and a person does
    the TAO booking (clearing the security check by hand). Flip it off once
    TAO lets Amy in directly."""
    return os.environ.get("AMY_MANUAL_BOOKING", "").strip().lower() in ("1", "true", "yes")


def manual_handoff(service, message_id, raw, dry_run):
    """Build and send the manual work order + draft confirmation for one
    request. No live TAO: the clubs are Amy's routed order, booker picks the
    first that's open."""
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
            venue=", ".join(raw.get("venues") or []) or "(none specified)",
            date=f"{raw.get('start_date')} to {raw.get('end_date')}",
            issue=e.issue, required_action=e.required_action)], dry_run)
        return gmail_client.EXCEPTION_LABEL

    today = datetime.now(VEGAS).date()
    if request["end_date"] < today.isoformat():
        team_alert(service, message_id, raw, [internal_action_needed(
            request["first_name"], request["email"], venue="(n/a)",
            date=f"{request['start_date']} to {request['end_date']}",
            issue="Every requested night has already passed.",
            required_action="Confirm the night with the guest and book manually.")], dry_run)
        return gmail_client.EXCEPTION_LABEL

    nights, prev = [], None
    for d in date_range(request["start_date"], request["end_date"]):
        if d < today:
            continue
        clubs = candidate_venues(request, d, prev)
        nights.append({"date": d.isoformat(), "nightclubs": clubs,
                       "dayclubs": dayclub_candidates(request) if is_dayclub_season(d) else []})
        prev = clubs[0] if clubs else None

    drais_nights = [n["date"] for n in nights] if request.get("drais") else []
    drais = {"name": f"{request['first_name']} {request['last_name']}".strip(),
             "female_count": request["female_count"], "male_count": request["male_count"],
             "nights": drais_nights} if drais_nights else None

    subject, body = manual_work_order(request, nights, rules.TAO_PROMOTER_URL, drais_nights)
    body += f"\n\nRef: {message_id}"
    csubject, cbody = manual_confirmation_draft(request, nights, drais=drais)

    if dry_run:
        print(f"[dry run] manual work order to {rules.PLAYMAKER_EMAIL}:\n{subject}\n{body}")
        print(f"[dry run] draft confirmation to {request['email']} (cc {rules.PLAYMAKER_EMAIL}):\n{csubject}")
        return None

    gmail_client.send_once(
        service, f"amy-manual-{message_id}@playmakerentertainment.com",
        rules.PLAYMAKER_EMAIL, subject, body, sender=rules.SENDER_EMAIL,
        dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} "Ref: {message_id}"')
    gmail_client.draft_once(
        service, dedupe_query=f'to:{request["email"]} subject:"{csubject}"',
        to=request["email"], subject=csubject, body=cbody,
        sender=rules.SENDER_EMAIL, cc=rules.PLAYMAKER_EMAIL)
    return gmail_client.PROCESSED_LABEL


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

    if concierge_on():
        return concierge_handoff(service, message_id, raw, dry_run)

    if send_links_on():
        return send_signup_links(service, message_id, raw, dry_run)

    if manual_booking_on():
        return manual_handoff(service, message_id, raw, dry_run)

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
                sender=rules.SENDER_EMAIL, cc=rules.PLAYMAKER_EMAIL,
            )

    if result.get("action_needed_records"):
        team_alert(service, message_id, raw, result["action_needed_records"], dry_run)
        return gmail_client.EXCEPTION_LABEL
    return gmail_client.PROCESSED_LABEL


def last_night_from_subject(subject):
    """The guest's last night, from a confirmation subject. Amy's own:
    "… — 2026-09-28" / "… — 2026-09-28 to 2026-09-30". The previous
    process's: "… — September 26, 2026" / "… — September 25–29, 2026"."""
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", subject or "")
    if dates:
        return datetime.fromisoformat(dates[-1]).date()
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})(?:\s*[–-]\s*(?:([A-Z][a-z]+)\s+)?(\d{1,2}))?,\s*(\d{4})",
                  subject or "")
    if not m:
        return None
    month = m.group(3) or m.group(1)
    day = m.group(4) or m.group(2)
    try:
        return datetime.strptime(f"{month} {day} {m.group(5)}", "%B %d %Y").date()
    except ValueError:
        return None


# Matches every follow-up wording used so far.
FOLLOW_UP_SEARCH = ('(subject:"Until next time" OR subject:"See you next time in Vegas" '
                    'OR subject:"Hope you had fun in Vegas")')


def send_follow_ups(service, mode, allowlist=None, today=None):
    """One "see you next time" email per confirmed visit, FOLLOW_UP_DELAY_DAYS
    after the guest's last night. Fixed Message-IDs mean never twice."""
    today = today or datetime.now(VEGAS).date()
    for conf in gmail_client.recent_confirmations(service):
        last_night = last_night_from_subject(conf["subject"])
        address = re.sub(r".*<([^>]+)>.*", r"\1", conf["to"]).strip().lower()
        if not last_night or not address or today < last_night + timedelta(days=FOLLOW_UP_DELAY_DAYS):
            continue
        if allowlist is not None and address not in allowlist:
            continue
        if gmail_client.has_opted_out(service, address):
            log.info("Follow-up skipped for %s: they asked to stop", address)
            continue
        subject, body = follow_up_email(conf["first_name"], conf.get("venues", []))
        if mode == "dry-run":
            log.info("[dry run] would send follow-up to %s (last night %s)", address, last_night)
            continue
        # Any follow-up to this guest in the last 60 days counts: never again.
        if gmail_client.send_once(service, f"amy-followup-{conf['id']}@playmakerentertainment.com",
                                  address, subject, body, sender=rules.SENDER_EMAIL,
                                  dedupe_query=f'to:{address} {FOLLOW_UP_SEARCH} newer_than:60d'):
            log.info("Follow-up sent to %s (last night %s)", address, last_night)


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


def outbound_ip():
    """Amy's public IP, for TAO to allowlist. Best-effort; never fatal."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return "unknown (check Railway's networking page)"


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
    # So TAO can allowlist Amy by IP: print the server's outbound address once.
    log.info("Amy outbound IP (give this to TAO to allowlist): %s", outbound_ip())
    if os.environ.get("TAO_ACCESS_TOKEN", "").strip():
        log.info("Sending TAO access token header on every request (TAO_ACCESS_TOKEN is set).")

    labels = {}
    if mode != "dry-run":
        for name in (gmail_client.PROCESSING_LABEL, gmail_client.PROCESSED_LABEL, gmail_client.EXCEPTION_LABEL):
            labels[name] = gmail_client.get_or_create_label(service, name)

    poll_seconds = max(int(os.environ.get("AMY_POLL_SECONDS", "120")), 30)
    last_follow_up_check = 0.0
    while True:
        try:
            run_once(service, mode, labels, allowlist, start_after)
            # Follow-ups are off unless FOLLOW_UPS_ENABLED=true (paused 2026-10-04
            # while the owner rewrites the wording).
            follow_ups_on = os.environ.get("FOLLOW_UPS_ENABLED", "").strip().lower() in ("1", "true", "yes")
            if follow_ups_on and time.time() - last_follow_up_check >= 3600:  # hourly is plenty
                send_follow_ups(service, mode, allowlist)
                last_follow_up_check = time.time()
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

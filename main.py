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
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rules_engine import (
    normalize_guest_request, ActionNeeded, date_range,
    resolve_dayclub_for_date, resolve_venue_for_date,
    candidate_venues, dayclub_candidates, is_closed_list_fallback, requested_nightclubs, is_dayclub_season, omnia_closed_for_same_day,
)
from config import rules
from templates import html_emails
from templates.emails import (
    FOLLOW_UP_DELAY_DAYS, consolidated_confirmation, follow_up_email, internal_action_needed,
    internal_processed_record, manual_work_order, manual_confirmation_draft,
    guest_signup_links_email, guest_confirmation, pending_signup_email, weekly_lineup_email,
)
import gmail_client
import newsletter
import posh
import posh_webhook
import tao_portal
import ticketsauce
import assisted_jobs
import amyi
import headliners
import speakeasy
import job_tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("amy")


VEGAS = ZoneInfo("America/Los_Angeles")

PHOENIX = ZoneInfo("America/Phoenix")


def _send_branded_or_gmail(service, message_id, to, subject, text, html, dedupe_query=None):
    """Send from info@playmakerentertainment.com via Zoho SMTP (send_branded)
    when Zoho is configured, falling back to gmail_client.send_once."""
    dq = dedupe_query or f'to:{to} subject:"{subject}" newer_than:7d'
    if posh_webhook.zoho_configured():
        code, res = posh_webhook.send_branded(service, {
            "to": to,
            "subject": subject,
            "text": text,
            "html": html,
            "dedupe_query": dq,
        })
        return code == 200 and isinstance(res, dict) and bool(res.get("sent"))
    return gmail_client.send_once(
        service, message_id, to, subject, text,
        sender=posh_webhook.branded_from(), html=html, dedupe_query=dq,
    )


def handle_scottsdale_request(service, message_id, raw, dry_run):
    """Automatically send the branded Scottsdale confirmation (Riot House,
    Almost Famous, El Hefe, Cake Nightclub, Maya Dayclub) without routing to
    TAO Vegas or waking Grok Bot."""
    email = (raw.get("email") or "").strip()
    full_name = (raw.get("name") or "").strip()
    first_name = (full_name.split()[0] if full_name else "there")
    try:
        f_cnt = int(raw.get("female_count") or 0)
    except (TypeError, ValueError):
        f_cnt = 0
    try:
        m_cnt = int(raw.get("male_count") or 0)
    except (TypeError, ValueError):
        m_cnt = 0
    party_str = html_emails.party_size(f_cnt, m_cnt)

    event_name = ((raw.get("posh") or {}).get("event_name") or "").strip()
    candidates = [v for v in ([event_name] + list(raw.get("venues") or [])) if v]
    canonical = rules.scottsdale_venue(candidates) or "Almost Famous"

    start_s = (raw.get("start_date") or "").strip()
    end_s = (raw.get("end_date") or start_s).strip()
    try:
        nights = [d.isoformat() for d in date_range(start_s, end_s)] if start_s else []
    except Exception:
        nights = [start_s] if start_s else []
    if not nights:
        nights = [datetime.now(PHOENIX).date().isoformat()]

    subject, text, html = html_emails.scottsdale_confirmation_email(
        first_name, canonical, nights
    )
    if dry_run:
        log.info("[dry run] would send Scottsdale confirmation to %s: %s", email, subject)
        return None
    if is_guestlistgo(raw):
        log.info("%s: GuestlistGo Scottsdale sign-up; Playmaker confirmation skipped", message_id)
        return gmail_client.PROCESSED_LABEL

    order_id = (raw.get("posh") or {}).get("order_number") or ""
    _track(message_id, name=full_name, email=email, venue=canonical, dates=nights, order_id=order_id)

    sent_ok = False
    if email:
        try:
            sent_ok = bool(_send_branded_or_gmail(
                service,
                f"amy-scottsdale-{message_id}@playmakerentertainment.com",
                email,
                subject,
                text,
                html,
                dedupe_query=f'to:{email} subject:"{subject}" newer_than:7d',
            ))
        except Exception:
            log.exception("%s: Scottsdale confirmation send raised", message_id)
            sent_ok = False
    if not sent_ok:
        issue = (f"Scottsdale confirmation to {email or '(no email)'} for {canonical} "
                 f"({', '.join(nights)}) did not send.")
        log.warning("%s: %s", message_id, issue)
        _notify_exception(f"{message_id}:send-failed", raw, issue, track=False)

    # Team summary always goes out (Riot House / Almost Famous Posh orders included).
    date_lbl = nights[0] if len(nights) == 1 else f"{nights[0]} to {nights[-1]}"
    status = "Signed up" if sent_ok else "SEND FAILED"
    tsub = f"{status}: {full_name or email} — {canonical} ({date_lbl})"
    tbody = (
        (f"Scottsdale guest list confirmation sent automatically by AMY.\n\n" if sent_ok else
         f"AMY could NOT send this guest's Scottsdale confirmation. AMYI has been alerted.\n\n")
        + f"Name: {full_name or '(none)'}\n"
        f"Email: {email}\n"
        f"Phone: {raw.get('phone') or (raw.get('posh') or {}).get('phone') or '(none)'}\n"
        f"Venue: {canonical}\n"
        f"Dates: {', '.join(nights)}\n"
        f"Party: {f_cnt}F / {m_cnt}M\n"
        + (f"Posh order: {order_id}\n" if order_id else "")
        + f"\nRef: {message_id}"
    )
    thtml = f'<html><body><pre style="font-family:inherit;white-space:pre-wrap">{tbody}</pre></body></html>'
    try:
        _send_branded_or_gmail(
            service,
            f"amy-scottsdale-team-{message_id}@playmakerentertainment.com",
            rules.PLAYMAKER_EMAIL,
            tsub,
            tbody,
            thtml,
            dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} "Ref: {message_id}"',
        )
    except Exception:
        log.exception("%s: Scottsdale team summary send failed", message_id)
    if not sent_ok:
        return gmail_client.EXCEPTION_LABEL
    job_tracker.mark_done(message_id)
    return gmail_client.PROCESSED_LABEL


def send_scottsdale_door_lists(service, mode, now=None):
    """On Friday and Saturday evenings at/after 7:43 PM America/Phoenix, send
    tonight's Riot House and Almost Famous guest-list rundown emails to
    team@playmakerentertainment.com (even when 0 orders)."""
    now = now or datetime.now(PHOENIX)
    if now.weekday() not in (4, 5):  # 4 = Friday, 5 = Saturday
        return 0
    if (now.hour, now.minute) < (19, 43):
        return 0
    tonight = now.date()
    day_short = f"{tonight.strftime('%a %b')} {tonight.day}"
    day_long = f"{tonight.strftime('%a, %b')} {tonight.day}, {tonight.year}"

    sent_count = 0
    for venue, header_title in (
        ("Riot House", f"Riot House | Guest List — {day_long} (10:00 PM - 2:00 AM)"),
        ("Almost Famous", f"Almost Famous — {day_long} (10:00 PM - 2:00 AM)"),
    ):
        subject = f"{venue} Guest List — {day_short}"
        dedupe_q = f'to:{rules.PLAYMAKER_EMAIL} subject:"{subject}" newer_than:2d'
        if mode != "dry-run" and gmail_client.already_sent(service, dedupe_q):
            continue

        q_term = '"Riot House" OR "riothouse"' if venue == "Riot House" else '"Almost Famous"'
        resp = service.users().messages().list(
            userId="me", q=f"({q_term}) newer_than:14d -in:sent", maxResults=100
        ).execute()
        orders = []
        seen_keys = set()
        for ref in reversed(resp.get("messages", [])):
            mid = ref["id"]
            msg, body = gmail_client.get_plain_text_body(service, mid)
            subj = gmail_client.get_subject(msg or {})
            if posh.is_posh_signup(subj, body):
                raw = posh.parse_signup(mid, body)
            elif any(m.lower() in (subj or "").lower() for m in rules.SUBJECT_MARKERS):
                raw = gmail_client.parse_request(mid, body)
            else:
                continue
            ev = ((raw.get("posh") or {}).get("event_name") or "").strip()
            if rules.scottsdale_venue([ev] + list(raw.get("venues") or [])) != venue:
                continue
            sd = (raw.get("start_date") or "").strip()
            ed = (raw.get("end_date") or sd).strip()
            if sd and not (sd <= tonight.isoformat() <= ed):
                continue
            order_no = ((raw.get("posh") or {}).get("order_number") or "").strip()
            guest_email = (raw.get("email") or "").strip().lower()
            guest_name = (raw.get("name") or "").strip() or guest_email
            dedupe_key = order_no or f"{guest_email}:{sd}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            try:
                w = int(raw.get("female_count") or 0)
            except (TypeError, ValueError):
                w = 0
            try:
                m = int(raw.get("male_count") or 0)
            except (TypeError, ValueError):
                m = 0
            ticket = ((raw.get("posh") or {}).get("ticket") or "").strip()
            orders.append({
                "name": guest_name,
                "email": guest_email,
                "women": w,
                "men": m,
                "ticket": ticket,
                "order_number": order_no,
            })

        if orders:
            lines = []
            tot_w = sum(o["women"] for o in orders)
            tot_m = sum(o["men"] for o in orders)
            tot_g = tot_w + tot_m
            for o in orders:
                w_lbl = "1 woman" if o["women"] == 1 else f"{o['women']} women"
                m_lbl = "1 man" if o["men"] == 1 else f"{o['men']} men"
                t_part = f" ({o['ticket']})" if o["ticket"] else ""
                ord_part = f" #{o['order_number']}" if o["order_number"] else ""
                email_part = f" ({o['email']})" if o["email"] and o["email"] not in o["name"].lower() else ""
                lines.append(f"{o['name']}{email_part} — {w_lbl}, {m_lbl}{t_part}{ord_part}")
            ord_word = "1 order" if len(orders) == 1 else f"{len(orders)} orders"
            g_word = "1 guest" if tot_g == 1 else f"{tot_g} guests"
            w_word = "1 woman" if tot_w == 1 else f"{tot_w} women"
            m_word = "1 man" if tot_m == 1 else f"{tot_m} men"
            text = (
                f"{header_title}\n\n"
                + "\n".join(lines)
                + f"\n\nTotals: {g_word} ({w_word}, {m_word}) across {ord_word}.\n\nPlaymaker Entertainment"
            )
        else:
            text = (
                f"{header_title}\n\n"
                "No guests yet. Posh shows 0 orders for tonight.\n\n"
                "Totals: 0 guests (0 women, 0 men).\n\nPlaymaker Entertainment"
            )
        html = f'<html><body><pre style="font-family:inherit;white-space:pre-wrap">{text}</pre></body></html>'
        if mode == "dry-run":
            log.info("[dry run] would send %s:\n%s", subject, text)
            continue
        if _send_branded_or_gmail(
            service,
            f"amy-doorlist-{venue.lower().replace(' ', '-')}-{tonight.isoformat()}@playmakerentertainment.com",
            rules.PLAYMAKER_EMAIL,
            subject,
            text,
            html,
            dedupe_query=dedupe_q,
        ):
            log.info("Sent Scottsdale door list: %s", subject)
            sent_count += 1
    return sent_count



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
    assisted = []

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
                "fallback": category == "nightclub" and bool((listing or {}).get("_requested_closed")),
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
        if result.get("assisted_job"):
            job = result["assisted_job"]
            job.update(venue=job.get("venue") or venue, date=job.get("date") or date_obj.isoformat())
            assisted.append(job)
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
            "fallback": category == "nightclub" and bool((listing or {}).get("_requested_closed")),
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
    if assisted:
        output["assisted_jobs"] = assisted
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

        closed = set()

        def tracking_checker(v, d, _closed=closed):
            # Record requested venues whose guest list is closed (no open Passes).
            res = checker(v, d)
            if v in requested_nightclubs(request) and not (res and res.get("listing_type") == "Passes"):
                _closed.add(v)
            return res

        venue, listing = resolve_venue_for_date(
            request, date_obj, live_availability_checker=tracking_checker,
            previous_night_venue=previous_night_venue,
        )
        if not venue:
            previous_night_venue = None
            exceptions.append({
                "date": date_obj.isoformat(),
                "issue": "No matching live Passes/Guest List event for requested or routed nightclubs.",
            })
            continue
        if is_closed_list_fallback(request, venue, closed):
            listing = dict(listing, _requested_closed=True)
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
        output["confirmation_email"] = {
            "subject": subject, "body": body,
            "html": html_emails.consolidated_confirmation_html(
                request["first_name"], registrations, drais=bool(drais_nights))}
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
    (AMYI books on TAO; nights not posted yet are recorded as pending jobs).
    Also turns off the old "needs attention" emails."""
    return os.environ.get("AMY_CONCIERGE", "").strip().lower() in ("1", "true", "yes")


def is_guestlistgo(raw):
    """GuestlistGo website sign-up (source guestlistgo / Promoter GuestlistGo).
    Guest-facing mail for these must be GuestlistGo-branded and never mention
    Playmaker; until confirmations@guestlistgo.me exists Amy sends the guest
    nothing (team/AMYI flow unchanged)."""
    return (raw or {}).get("source") == "guestlistgo"


def guest_confirmation_on():
    """AMY_GUEST_CONFIRMATION=true: also email the guest "You're on the list"
    right away, before any TAO sign-up. Off by default (some lists close);
    guests get AMYI's post-signup confirmation instead."""
    return os.environ.get("AMY_GUEST_CONFIRMATION", "").strip().lower() in ("1", "true", "yes")


def concierge_handoff(service, message_id, raw, dry_run):
    """Guest confirmation + AMYI jobs. Nights with a live TAO link become
    sign-up jobs; nights not posted yet become pending jobs (AMYI alerted).
    No manual team sign-up email, no needs-attention.
    Unprocessable or all-past requests are labeled and dropped quietly."""
    if raw.get("_missing_required"):
        issue = f"Request is missing required fields: {', '.join(raw['_missing_required'])}."
        log.info("%s: missing %s; concierge skip (no team alert)", message_id, raw["_missing_required"])
        _notify_exception(message_id, raw, issue, dry_run)
        return gmail_client.EXCEPTION_LABEL
    try:
        request = normalize_guest_request(raw)
    except ActionNeeded as e:
        log.info("%s: %s; concierge skip (no team alert)", message_id, e.issue)
        _notify_exception(message_id, raw, e.issue, dry_run)
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
        # Direct sign-up links for the chosen venue(s), for AMYI's job. Only
        # present when live on the feed.
        signup_url = ticketsauce.promoter_link((catalog.get((chosen, d)) or {}).get("url"))
        dayclub_url = ticketsauce.promoter_link((catalog.get((dayclub, d)) or {}).get("url")) if dayclub else None
        nights.append({"date": d.isoformat(), "venue": chosen,
                       "backups": [v for v in opts if v != chosen][:3],
                       "dayclub": dayclub, "rerouted_from": rerouted_from,
                       "unverified": not live, "none_live": none_live,
                       "fallback": is_closed_list_fallback(
                           request, chosen,
                           ({rerouted_from} if rerouted_from else set()) | (
                               {v for v in requested_nightclubs(request) if (v, d) not in catalog}
                               if live else set())),
                       "headliner": headliners.headliner(chosen, d),
                       "signup_url": signup_url, "dayclub_url": dayclub_url})
        prev = chosen

    drais_nights = [n["date"] for n in nights] if request.get("drais") else []
    if not nights and not drais_nights:
        log.info("%s: every requested night has passed; concierge skip", message_id)
        _notify_exception(message_id, raw, "Every requested night has already passed.", dry_run)
        return gmail_client.EXCEPTION_LABEL

    drais = {"name": f"{request['first_name']} {request['last_name']}".strip(),
             "female_count": request["female_count"], "male_count": request["male_count"],
             "nights": drais_nights} if drais_nights else None

    # Drai's runs on Playmaker's own SpeakeasyGo account (no bot-check). When
    # enabled, Amy adds the guest to the SpeakeasyGo list herself for each night
    # and reports the result in the team email, so success/failure is visible
    # and nobody is ever silently dropped. The guest's door text still goes out.
    drais_status = None
    if drais_nights and not dry_run and speakeasy.auto_on():
        guest_row = {"first": request["first_name"], "last": request["last_name"],
                     "male": request["male_count"], "female": request["female_count"],
                     "name": drais["name"]}
        lines = []
        for night in drais_nights:
            res = speakeasy.add_guests_for_night(night, [guest_row])[0]
            lines.append(f"  {night}: " + ("✓ added to SpeakeasyGo" if res["ok"]
                         else f"✗ auto-add failed ({res['error']}) — add manually"))
        drais_status = "Drai's SpeakeasyGo sign-up:\n" + "\n".join(lines)
        log.info("%s Drai's auto-add: %s", message_id, "; ".join(lines))

    gsub, gbody = guest_confirmation(request["first_name"], nights, drais=drais)
    ghtml = html_emails.concierge_confirmation_html(
        request["first_name"], nights,
        html_emails.party_size(request.get("female_count"), request.get("male_count")), drais=drais)
    jobs = concierge_jobs(request, nights, message_id)
    job_dates = {j.get("date") for j in jobs}
    pending = [n for n in nights if n["date"] not in job_dates]
    tsub = tbody = None
    if pending or drais_status:
        tsub, tbody = pending_signup_email(request, pending, drais_nights)
        if drais_status:
            tbody += f"\n\n{drais_status}"
        tbody += f"\n\nRef: {message_id}"

    if dry_run:
        print(f"[dry run] guest confirmation to {request['email']}:\n{gsub}\n{gbody}")
        if tsub:
            print(f"[dry run] team notice to {rules.PLAYMAKER_EMAIL}:\n{tsub}\n{tbody}")
        return None

    if is_guestlistgo(raw):
        log.info("%s: GuestlistGo sign-up; Playmaker guest confirmation not sent "
                 "(GuestlistGo-branded sender not set up yet)", message_id)
    elif guest_confirmation_on():
        gmail_client.send_once(
            service, f"amy-confirm-{message_id}@playmakerentertainment.com",
            request["email"], gsub, gbody, sender=rules.SENDER_EMAIL, html=ghtml,
            dedupe_query=f'to:{request["email"]} subject:"{gsub}" newer_than:3d')
    else:
        log.info("%s: immediate guest confirmation off (AMY_GUEST_CONFIRMATION); "
                 "guest hears from AMYI after sign-up", message_id)
    if jobs:
        send_assisted_jobs(service, message_id, request, jobs, dry_run=False)
    if pending:
        name = f"{request['first_name']} {request['last_name']}".strip()
        for n in pending:
            log.info("%s: %s %s has no live TAO link; pending job for AMYI (event not posted yet)",
                     message_id, n["venue"], n["date"])
        _notify_exception(
            f"{message_id}:pending",
            {**raw, "name": name, "email": request["email"], "phone": request.get("phone") or "",
             "venues": [n["venue"] for n in pending],
             "start_date": pending[0]["date"], "end_date": pending[-1]["date"]},
            "event not posted yet — " + "; ".join(
                f"{n['venue']} {n['date']}" + (f" (backups: {', '.join(n['backups'])})" if n.get("backups") else "")
                for n in pending) + ". AMYI: book when it's live on TAO.")
        job_tracker.update(f"{message_id}:pending", status="pending_event")
    if tsub:
        gmail_client.send_once(
            service, f"amy-pending-{message_id}@playmakerentertainment.com",
            rules.PLAYMAKER_EMAIL, tsub, tbody, sender=rules.SENDER_EMAIL,
            dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} "Ref: {message_id}"')
    return gmail_client.PROCESSED_LABEL


def concierge_jobs(request, nights, message_id):
    """Assisted jobs for every concierge night that has a live sign-up link
    (main venue and dayclub). Nights without a link yield no job."""
    jobs = []
    for n in nights:
        for venue, url in ((n["venue"], n.get("signup_url")), (n.get("dayclub"), n.get("dayclub_url"))):
            if url and tao_portal.is_safe_pass_url(url):
                jobs.append(assisted_jobs.make_job(url, request, venue=venue, date=n["date"], ref=message_id))
    return jobs


def send_assisted_jobs(service, message_id, request, jobs, dry_run):
    """Email the jobs to Thomas (ASSISTED_SIGNUP_TO, else the Playmaker inbox)."""
    to = assisted_jobs.recipient(rules.PLAYMAKER_EMAIL)
    name = f"{request['first_name']} {request['last_name']}".strip()
    subject, body = assisted_jobs.job_email(jobs, name)
    body += f"\n\nRef: {message_id}"
    if dry_run:
        print(f"[dry run] assisted sign-up jobs to {to}:\n{subject}\n{body}")
        return False
    sent = gmail_client.send_once(
        service, f"amy-assisted-{message_id}@playmakerentertainment.com", to, subject, body,
        sender=rules.SENDER_EMAIL, dedupe_query=f'to:{to} "Ref: {message_id}" subject:"Amy sign-up"')
    _track(message_id, name=name, email=request.get("email", ""),
           venue=sorted({j.get("venue", "") for j in jobs}),
           dates=sorted({j.get("date", "") for j in jobs}),
           order_id=((request.get("posh") or {}).get("order_number") or ""))
    amyi.notify("signup_job", name=name, email=request.get("email", ""),
                venue=sorted({j.get("venue", "") for j in jobs}),
                dates=sorted({j.get("date", "") for j in jobs}), ref=message_id)
    return sent


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
        _notify_exception(message_id, raw, e.issue, dry_run)
        return gmail_client.EXCEPTION_LABEL

    today = datetime.now(VEGAS).date()
    if request["end_date"] < today.isoformat():
        team_alert(service, message_id, raw, [internal_action_needed(
            request["first_name"], request["email"], venue="(n/a)",
            date=f"{request['start_date']} to {request['end_date']}",
            issue="Every requested night has already passed.",
            required_action="Confirm the night with the guest and send the link manually.")], dry_run)
        _notify_exception(message_id, raw, "Every requested night has already passed.", dry_run)
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

    if is_guestlistgo(raw):
        log.info("%s: GuestlistGo sign-up; Playmaker guest links email not sent", message_id)
        return gmail_client.PROCESSED_LABEL
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
        _notify_exception(message_id, raw, e.issue, dry_run)
        return gmail_client.EXCEPTION_LABEL

    today = datetime.now(VEGAS).date()
    if request["end_date"] < today.isoformat():
        team_alert(service, message_id, raw, [internal_action_needed(
            request["first_name"], request["email"], venue="(n/a)",
            date=f"{request['start_date']} to {request['end_date']}",
            issue="Every requested night has already passed.",
            required_action="Confirm the night with the guest and book manually.")], dry_run)
        _notify_exception(message_id, raw, "Every requested night has already passed.", dry_run)
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


def non_vegas_venue_problem(raw):
    """'scottsdale', 'unknown', or None. Scottsdale venues (and any venue we
    don't recognize) must never be routed to a TAO Vegas club."""
    venues = [
        rules.normalize_venue_name(v)
        for v in (raw.get("venues") or [])
        if v and not rules.is_open_venue_placeholder(v)
    ]
    event = (raw.get("posh") or {}).get("event_name") or ""
    if rules.is_scottsdale(event) or any(rules.is_scottsdale(v) for v in venues):
        return "scottsdale"
    if any(not rules.is_known_vegas_venue(v) for v in venues):
        return "unknown"
    return None


def _party(raw):
    try:
        return (int(raw.get("female_count") or 0) + int(raw.get("male_count") or 0)) or ""
    except (TypeError, ValueError):
        return ""


def _track(ref, name="", email="", venue="", dates=(), order_id=""):
    """Mark this message as having a job and record it in the job ledger."""
    amyi.mark_job()
    return job_tracker.record(ref, order_id=order_id, guest=name, email=email,
                              venue=venue, dates=dates)


def _notify_exception(message_id, raw, issue, dry_run=False, track=True):
    if dry_run:
        return
    raw = raw or {}
    p = raw.get("posh") or {}
    if track:
        _track(message_id, name=raw.get("name", ""), email=raw.get("email", ""),
               venue=raw.get("venues") or [],
               dates=[d for d in (raw.get("start_date"), raw.get("end_date")) if d],
               order_id=p.get("order_number") or "")
    else:
        amyi.mark_job()
    amyi.notify(
        "posh_exception" if raw.get("source") == "Posh" else "exception",
        name=raw.get("name", ""),
        email=raw.get("email", ""),
        phone=raw.get("phone", "") or p.get("phone", ""),
        event=p.get("event_name", "") or issue,
        venue=raw.get("venues") or [],
        dates=[d for d in (raw.get("start_date"), raw.get("end_date")) if d],
        party=_party(raw),
        source=(raw.get("source") or "posh").lower(),
        ref=message_id,
    )


def _job_raw(row):
    dates = row.get("dates") or []
    return {"name": row.get("guest", ""), "email": row.get("email", ""),
            "venues": [v.strip() for v in (row.get("venue") or "").split(",") if v.strip()],
            "start_date": dates[0] if dates else "", "end_date": dates[-1] if dates else "",
            "source": "watchdog"}


def _job_team_email(service, row, subject_prefix, note):
    body = (f"{note}\n\nGuest: {row.get('guest') or '(none)'}\nEmail: {row.get('email') or '(none)'}\n"
            f"Venue: {row.get('venue') or '(none)'}\nDates: {', '.join(row.get('dates') or [])}\n"
            f"Webhook status: {row.get('webhook_status')} (attempts {row.get('attempts')}, "
            f"failures {row.get('failures')})\n\nRef: {row['ref']}")
    html = f'<html><body><pre style="font-family:inherit;white-space:pre-wrap">{body}</pre></body></html>'
    code, res = posh_webhook.send_branded(service, {
        "to": rules.PLAYMAKER_EMAIL,
        "subject": f"{subject_prefix}: {row.get('guest') or row.get('email')} — {', '.join(row.get('dates') or [])}",
        "text": body, "html": html,
        "dedupe_query": f'to:{rules.PLAYMAKER_EMAIL} subject:"{subject_prefix}" "Ref: {row["ref"]}"'})
    return code == 200


def run_job_watchdog(service, now=None):
    """Re-fire at ~3 min, alert AMYI + team at 5 min, escalate/manual at 15 min
    or after 3 failed deliveries, for every job not marked done."""
    def refire(row):
        log.info("Job %s not done after 3 min; re-firing AMYI webhook", row["ref"])
        amyi.refire(row.get("payload"))

    def alert(row):
        log.warning("Job %s not done after 5 min; alerting AMYI + team", row["ref"])
        _notify_exception(f"{row['ref']}:watchdog", _job_raw(row),
                          "AMYI job not marked done after 5 minutes", track=False)
        _job_team_email(service, row, "AMYI job not done",
                        "AMY queued this job for AMYI 5+ minutes ago and it hasn't been marked done.")

    def escalate(row):
        log.error("Job %s escalated to manual (age/failures)", row["ref"])
        _notify_exception(f"{row['ref']}:manual", _job_raw(row),
                          "ESCALATED: AMYI job not done after 15 minutes or 3 failed deliveries; "
                          "marked manual", track=False)
        _job_team_email(service, row, "ESCALATED — manual",
                        "AMYI has not completed this job (15+ minutes or 3 failed webhook deliveries). "
                        "It is now marked manual.")

    return job_tracker.watchdog(refire, alert, escalate, now=now)


def handle_message(service, message_id, dry_run, labels, allowlist=None):
    amyi.begin_message()
    try:
        return _handle_message(service, message_id, dry_run, labels, allowlist)
    finally:
        amyi.end_message()


_WEBSITE_REFS_SEEN = set()  # website submission refs handled by this process


def _handle_message(service, message_id, dry_run, labels, allowlist=None):
    """Process one request email end to end. Returns the outcome label, or
    None when test mode skips a request that isn't from an allowlisted
    address (it is left completely untouched)."""
    msg, body = gmail_client.get_plain_text_body(service, message_id)
    if posh.is_posh_signup(gmail_client.get_subject(msg or {}), body):
        if posh_webhook.webhook_only() and not posh_webhook.is_webhook_signup(body):
            log.info("%s: Zapier Posh email skipped (POSH_WEBHOOK_ONLY; the webhook copy is used)", message_id)
            return gmail_client.PROCESSED_LABEL
        raw = posh.parse_signup(message_id, body)
        if posh_webhook.is_webhook_signup(body):
            raw["posh"]["via"] = "webhook"
            if posh_webhook.needs_review(body) and not raw.get("_action_needed"):
                raw["_action_needed"] = (
                    f"Posh recurring event: Posh only sent the series start "
                    f"({raw['posh'].get('event_start')}), and the real date of this order's event "
                    f"could not be looked up, so the night ({raw.get('start_date')}) is a guess. "
                    "Not booked automatically.",
                    "Check the order in Posh for the real night and register this guest manually.")
        order = raw["posh"].get("order_number")
        log.info("%s: source POSH, order %s, event %r, ticket %r", message_id, order or "(none)",
                 raw["posh"].get("event_name"), raw["posh"].get("ticket"))
    else:
        if gmail_client.is_own_team_copy(msg):
            log.info("%s: Amy's own team@ copy of a website notice; not a new request", message_id)
            return gmail_client.PROCESSED_LABEL
        raw = gmail_client.parse_request(message_id, body)
        ref = raw.get("form_ref")
        if ref:
            if ref in _WEBSITE_REFS_SEEN or gmail_client.website_ref_already_handled(service, ref, message_id):
                log.info("%s: website submission %s was already processed; not booking it again", message_id, ref)
                return gmail_client.PROCESSED_LABEL

    if allowlist is not None:
        guest = (raw.get("email") or "").lower()
        if guest not in allowlist and not (raw.get("_missing_required")
                                           and any(a in body.lower() for a in allowlist)):
            log.info("[test mode] leaving %s untouched (not from an allowlisted address)", message_id)
            return None

    if raw.get("form_ref") and raw.get("source") != "Posh":
        _WEBSITE_REFS_SEEN.add(raw["form_ref"])

    if raw.get("source") != "Posh" and not dry_run:
        amyi.defer_contact_form(name=raw.get("name", ""), email=raw.get("email", ""),
                    venue=raw.get("venues") or [], phone=raw.get("phone", ""),
                    event=raw.get("event", "") or ", ".join(raw.get("venues") or []),
                    party=_party(raw), source=raw.get("source") or "website",
                    dates=[d for d in (raw.get("start_date"), raw.get("end_date")) if d],
                    ref=message_id)

    if raw.get("source") == "Posh":
        order = raw["posh"].get("order_number")
        if gmail_client.posh_order_already_handled(service, order, message_id):
            log.info("%s: POSH order %s was already processed; not booking it again", message_id, order)
            return gmail_client.PROCESSED_LABEL

    if raw.get("_missing_required"):
        issue = f"Request is missing required fields: {', '.join(raw['_missing_required'])}."
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"), venue="(n/a)", date="(n/a)",
            issue=issue,
            required_action="Read the original email and process manually.",
        )], dry_run)
        _notify_exception(message_id, raw, issue, dry_run)
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
        _notify_exception(message_id, raw, issue, dry_run)
        return gmail_client.EXCEPTION_LABEL

    venue_problem = non_vegas_venue_problem(raw)
    if venue_problem == "scottsdale":
        log.info("%s: Scottsdale venue %s; sending Scottsdale confirmation (no TAO Vegas)", message_id, raw.get("venues"))
        return handle_scottsdale_request(service, message_id, raw, dry_run)
    if venue_problem == "unknown":
        issue = "Unrecognized venue; not routed to a default Vegas club."
        team_alert(service, message_id, raw, [internal_action_needed(
            raw.get("name", "(unknown)"), raw.get("email", "(unknown)"),
            venue=", ".join(raw.get("venues") or []), date=raw.get("start_date", "(unknown)"),
            issue=issue,
            required_action="Check the request and register the guest manually.",
        )], dry_run)
        _notify_exception(message_id, raw, issue, dry_run)
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
        _notify_exception(message_id, raw, result["issue"], dry_run)
        return gmail_client.EXCEPTION_LABEL

    request = result["request"]
    if dry_run:
        print(json.dumps({
            "guest": f"{request['first_name']} {request['last_name']} <{request['email']}>",
            "routing": [(r["date"], r["venue"]) for r in result.get("registrations", [])],
            "consent": request.get("consent"),
            "problems": result.get("action_needed_records"),
        }, indent=2, default=str))

    if result.get("assisted_jobs"):
        send_assisted_jobs(service, message_id, request, result["assisted_jobs"], dry_run)

    if "confirmation_email" in result:
        email = result["confirmation_email"]
        if dry_run:
            print(f"[dry run] would email {request['email']} (cc {rules.PLAYMAKER_EMAIL}):")
            print(email["subject"])
            print(email["body"])
        elif is_guestlistgo(raw):
            log.info("%s: GuestlistGo sign-up; Playmaker confirmation not sent", message_id)
        else:
            gmail_client.send_once(
                service, f"amy-confirm-{message_id}@playmakerentertainment.com",
                request["email"], email["subject"], email["body"],
                sender=rules.SENDER_EMAIL, cc=rules.PLAYMAKER_EMAIL, html=email.get("html"),
            )

    if result.get("action_needed_records"):
        team_alert(service, message_id, raw, result["action_needed_records"], dry_run)
        _notify_exception(message_id, raw, "; ".join(str(r) for r in result["action_needed_records"]), dry_run)
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


# Matches every follow-up wording used so far (Vegas and Scottsdale).
FOLLOW_UP_SEARCH = gmail_client.FOLLOW_UP_SEARCH


_NEWSLETTER_STORE = None


def newsletter_store():
    """One shared opt-in/opt-out store (own Gmail client, internally locked)."""
    global _NEWSLETTER_STORE
    if _NEWSLETTER_STORE is None:
        _NEWSLETTER_STORE = newsletter.GmailStore(gmail_client.get_service, rules.INTAKE_EMAIL)
    return _NEWSLETTER_STORE


def marketing_blocked(service, address, store=None):
    """True if a marketing email (follow-up, newsletter, promo) must NOT go to
    this guest: they unsubscribed via link or replied STOP. Transactional
    confirmations never call this."""
    store = store or newsletter_store()
    if store.is_opted_out(address):
        return True
    return gmail_client.has_opted_out(service, address)


def send_follow_ups(service, mode, allowlist=None, today=None, store=None):
    """One "see you next time" email per confirmed visit, FOLLOW_UP_DELAY_DAYS
    after the guest's last night. Fixed Message-IDs mean never twice."""
    today = today or datetime.now(VEGAS).date()
    cap = _int_env("FOLLOW_UP_MAX_PER_CYCLE", 5)
    late = _int_env("FOLLOW_UP_MAX_LATE_DAYS", 7)
    sent = deferred = 0
    merged = {}
    for conf in gmail_client.recent_confirmations(service):
        last_night = conf.get("last_night")
        address = re.sub(r".*<([^>]+)>.*", r"\1", conf["to"]).strip().lower()
        if not last_night or not address:
            continue
        if address not in merged:
            merged[address] = {
                "id": conf["id"],
                "to": conf["to"],
                "first_name": conf.get("first_name") or "",
                "venues": list(conf.get("venues") or []),
                "last_night": last_night,
            }
        else:
            cur = merged[address]
            if not cur["first_name"] and conf.get("first_name"):
                cur["first_name"] = conf["first_name"]
            for v in conf.get("venues") or []:
                if v not in cur["venues"]:
                    cur["venues"].append(v)
            if last_night > cur["last_night"]:
                cur["last_night"] = last_night
                cur["id"] = conf["id"]
                if conf.get("venues"):
                    cur["venues"] = list(conf["venues"]) + [v for v in cur["venues"] if v not in conf["venues"]]
    for address, conf in merged.items():
        last_night = conf["last_night"]
        if today < last_night + timedelta(days=FOLLOW_UP_DELAY_DAYS):
            continue
        if today > last_night + timedelta(days=FOLLOW_UP_DELAY_DAYS + late):
            continue  # too overdue: don't send a stale follow-up after an outage
        if allowlist is not None and address not in allowlist:
            continue
        try:
            blocked = marketing_blocked(service, address, store)
        except Exception:
            log.exception("Opt-out check failed for %s; skipping this cycle", address)
            continue  # never email someone we can't confirm is still subscribed
        if blocked:
            log.info("Follow-up skipped for %s: unsubscribed / asked to stop", address)
            continue
        if sent >= cap:
            deferred += 1
            continue
        subject, body = follow_up_email(conf["first_name"], conf.get("venues", []))
        links = newsletter.follow_up_links(address, conf["first_name"])
        if not links:
            log.warning("Follow-up held for %s: NEWSLETTER_LINK_SECRET not set (no unsubscribe link)", address)
            continue
        optin_url, unsub_url = links
        body += f"\n\nNewsletter (every 3 months, events and specials): {optin_url}\nUnsubscribe: {unsub_url}"
        if mode == "dry-run":
            log.info("[dry run] would send follow-up to %s (last night %s)", address, last_night)
            continue
        # Any follow-up to this guest in the last 60 days counts: never again.
        if gmail_client.send_once(service, f"amy-followup-{conf['id']}@playmakerentertainment.com",
                                  address, subject, body, sender=rules.SENDER_EMAIL,
                                  html=html_emails.render_follow_up(conf["first_name"], optin_url, unsub_url, venues=conf.get("venues", [])),
                                  headers=newsletter.list_unsubscribe_headers(unsub_url),
                                  dedupe_query=f'to:{address} {FOLLOW_UP_SEARCH} newer_than:60d'):
            log.info("Follow-up sent to %s (last night %s)", address, last_night)
            sent += 1
    if deferred:
        log.info("Follow-ups: %d sent this cycle, %d deferred to later cycles (cap %d)", sent, deferred, cap)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


FLYERS_DIR = os.path.join(os.path.dirname(__file__), "data", "flyers")


def _week_flyer_files(flyers_dir=FLYERS_DIR):
    """Current week's flyer images to attach, newest-looking order. The capture
    job writes this week's JPG/PNGs here; missing dir just means no attachments."""
    try:
        names = sorted(
            n for n in os.listdir(flyers_dir)
            if n.lower().endswith((".jpg", ".jpeg", ".png")))
    except OSError:
        return []
    return [os.path.join(flyers_dir, n) for n in names]


WEEKLY_LINEUP_WEEKDAY = 2  # Sent Wednesday (Mon=0). Covers the official Tue-Mon week (Tue before the send).
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


_last_flyer_scan_at = 0.0


def prep_weekly_lineup(today=None, data_dir=DATA_DIR, flyers_dir=FLYERS_DIR):
    """On Tuesday (>=10 AM Vegas) or Wednesday before send, pull this week's TAO
    Dropbox flyers and refresh headliners.json / top_picks.json / also_this_week.json
    if not already captured for the current Tue-Mon week."""
    global _last_flyer_scan_at
    from templates import weekly_newsletter
    now = datetime.now(VEGAS)
    today = today or now.date()
    if today.weekday() not in (1, WEEKLY_LINEUP_WEEKDAY):
        return None
    if today.weekday() == 1 and now.hour < int(os.environ.get("WEEKLY_PREP_HOUR", "10")):
        return None
    start, end = weekly_newsletter.week_window(today)
    marker = os.path.join(flyers_dir, ".week_start")
    both_lineups = all(
        os.path.isfile(os.path.join(flyers_dir, fn))
        for fn in ("weekly_nightclub.jpg", "weekly_dayclub.jpg")
    )
    try:
        with open(marker) as fh:
            if fh.read().strip() == start.isoformat() and both_lineups:
                return None
    except OSError:
        pass
    if time.time() - _last_flyer_scan_at < 3600 * 2:
        return None
    _last_flyer_scan_at = time.time()
    try:
        from tools import scan_flyers
        res = scan_flyers.refresh_week(data_dir, flyers_dir, start, end)
        headliners.reload_headliners()
        log.info("Weekly newsletter Dropbox prep finished for %s: %s", start.isoformat(), res)
        return res
    except Exception:
        log.exception("Weekly newsletter Dropbox prep failed for %s", start.isoformat())
        return None


def _build_weekly(today, flyers_dir):
    from templates import weekly_newsletter
    try:
        with open(os.path.join(DATA_DIR, "headliners.json")) as fh:
            hmap = json.load(fh)
    except (OSError, ValueError):
        hmap = {}
    return weekly_newsletter.build(today, DATA_DIR, flyers_dir, hmap)


def send_weekly_lineup(service, mode, today=None, flyers_dir=FLYERS_DIR, force=False):
    """Wednesday-morning (Vegas) HTML newsletter "This Week in Vegas" (Tue-Mon),
    sent once per week (deduped, so hourly checks are safe). Always carries the
    two TAO weekly lineup flyers (data/flyers/weekly_nightclub.jpg and
    weekly_dayclub.jpg) as CID inline images; if either is missing it does NOT
    send (fail closed) and logs an error. Top 3 picks come from
    data/top_picks.json when its week_start matches, else a default (biggest
    nightclub acts across venues, one JEWEL pick). WEEKLY_LINEUP_ENABLED=false
    pauses; WEEKLY_LINEUP_HOUR (default 9) sets the hour.

    WEEKLY_LINEUP_TEST_TO=<address> sends one "[TEST]" copy of this week's
    newsletter to that address only (any day/hour, deduped), for previews.
    A data/flyers/.send_now sentinel holding today's date forces one real send."""
    from templates import weekly_newsletter
    now = datetime.now(VEGAS)
    today = today or now.date()
    test_to = os.environ.get("WEEKLY_LINEUP_TEST_TO", "").strip()
    if test_to:
        # Preview the NEXT scheduled send: the Wednesday on/after today.
        preview_day = today + timedelta(days=(WEEKLY_LINEUP_WEEKDAY - today.weekday()) % 7)
        try:
            subject, text, html, inline, files = _build_weekly(preview_day, flyers_dir)
        except weekly_newsletter.MissingLineup as exc:
            log.error("Weekly newsletter TEST not sent: TAO lineup flyer missing (%s)", exc)
        else:
            subject = "[TEST] " + subject
            if mode == "dry-run":
                log.info("[dry run] would send weekly TEST to %s", test_to)
            elif gmail_client.send_once(service, None, test_to, subject, text, sender=rules.SENDER_EMAIL,
                                        html=html, inline_images=inline, attachments=files,
                                        dedupe_query=f'to:{test_to} subject:"{subject}" newer_than:6d'):
                log.info("Weekly newsletter TEST sent to %s (%d inline images)", test_to, len(inline))
    try:
        with open(os.path.join(flyers_dir, ".send_now")) as fh:
            sent_date = fh.read().strip()
        if sent_date in (today.isoformat(), (today - timedelta(days=1)).isoformat()):
            force = True
    except OSError:
        pass
    if not force:
        if today.weekday() != WEEKLY_LINEUP_WEEKDAY:
            return
        send_hour = int(os.environ.get("WEEKLY_LINEUP_HOUR", "9"))
        if now.hour < send_hour:
            return
    try:
        subject, text, html, inline, files = _build_weekly(today, flyers_dir)
    except weekly_newsletter.MissingLineup as exc:
        log.error("Weekly newsletter NOT sent: TAO weekly lineup flyer missing (%s). "
                  "Add data/flyers/weekly_nightclub.jpg and weekly_dayclub.jpg.", exc)
        return
    week_start = weekly_newsletter.week_window(today)[0]
    recipient = os.environ.get("WEEKLY_LINEUP_TO", "").strip() or rules.NEWSLETTER_EMAIL
    if mode == "dry-run":
        log.info("[dry run] would send weekly newsletter to %s (%d inline images)", recipient, len(inline))
        return
    if gmail_client.send_once(
            service, f"amy-lineup-{week_start.isoformat()}@playmakerentertainment.com",
            recipient, subject, text, sender=rules.SENDER_EMAIL, html=html, inline_images=inline,
            attachments=files, dedupe_query=f'to:{recipient} subject:"{subject}" newer_than:6d'):
        log.info("Weekly newsletter sent to %s (%d inline images, %d attachments)", recipient,
                 len(inline), len(files))
    send_weekly_to_subscribers(service, subject, text, html, inline, files, week_start, recipient)


WEEKLY_BCC_CHUNK = 50


def weekly_subscribers(store=None, exclude=()):
    """Deduped opted-in subscriber emails (newest record per email wins)."""
    skip = {newsletter.norm(e) for e in exclude}
    recs = (store or newsletter_store()).all(refresh=True)
    out = []
    for email, rec in sorted(recs.items()):
        e = newsletter.norm(email)
        if rec.get("status") == "opted_in" and e and "@" in e and e not in skip and e not in out:
            out.append(e)
    return sorted(out)


def _with_unsub(text, html, url):
    text = f"{text}\n\nUnsubscribe: {url}\n"
    foot = (f'<p style="font-size:12px;color:#888;text-align:center">'
            f'<a href="{url}" style="color:#888">Unsubscribe</a></p>')
    if html and "</body>" in html:
        html = html.replace("</body>", foot + "</body>", 1)
    elif html:
        html = html + foot
    return text, html


def send_weekly_to_subscribers(service, subject, text, html, inline, files, week_start, primary,
                               store=None):
    """Also send the weekly to every opted-in subscriber. Per-recipient
    unsubscribe links need individual sends (when NEWSLETTER_LINK_SECRET is
    set); otherwise BCC in chunks with To: newsletter@ and a mailto
    List-Unsubscribe. Each send is deduped via the Sent folder, so a capped run
    resumes on the next loop. Store failures are logged, never raised."""
    try:
        subs = weekly_subscribers(store, exclude=[primary, rules.NEWSLETTER_EMAIL])
    except Exception:  # noqa: BLE001
        log.exception("Weekly newsletter: subscriber store failed to load; sent to %s only", primary)
        return 0
    if not subs:
        return 0
    cap = int(os.environ.get("WEEKLY_SUBSCRIBER_MAX_PER_RUN", "400"))
    sent = 0
    if newsletter.secret():
        for addr in subs:
            if sent >= cap:
                log.info("Weekly subscriber send capped at %d this run; rest next run", cap)
                break
            url = newsletter.link(addr, "unsubscribe")
            t, h = _with_unsub(text, html, url)
            try:
                if gmail_client.send_once(
                        service, None, addr, subject, t, sender=rules.SENDER_EMAIL, html=h,
                        inline_images=inline, attachments=files,
                        headers=newsletter.list_unsubscribe_headers(url),
                        dedupe_query=f'to:{addr} subject:"{subject}" newer_than:6d'):
                    sent += 1
            except Exception:  # noqa: BLE001
                log.exception("Weekly newsletter send to subscriber %s failed", addr)
    else:
        mailto = "mailto:info@playmakerentertainment.com?subject=unsubscribe"
        t, h = _with_unsub(text, html, mailto)
        for i in range(0, len(subs), WEEKLY_BCC_CHUNK):
            chunk = subs[i:i + WEEKLY_BCC_CHUNK]
            mid = f"amy-lineup-{week_start.isoformat()}-bcc{i // WEEKLY_BCC_CHUNK}@playmakerentertainment.com"
            try:
                if gmail_client.send_once(
                        service, mid, rules.NEWSLETTER_EMAIL, subject, t, sender=rules.SENDER_EMAIL,
                        html=h, inline_images=inline, attachments=files,
                        headers={"Bcc": ", ".join(chunk),
                                 "List-Unsubscribe": f"<{mailto}>"},
                        dedupe_query=f"rfc822msgid:{mid}"):
                    sent += len(chunk)
            except Exception:  # noqa: BLE001
                log.exception("Weekly newsletter BCC chunk %d failed", i // WEEKLY_BCC_CHUNK)
    log.info("Weekly newsletter sent to %d subscribers", sent)
    return sent


def run_speakeasy_test(service):
    """One-shot SpeakeasyGo check: if data/speakeasy_test.json exists, add that
    one guest to SpeakeasyGo and email the result to the team, then it's safe to
    delete the file. Lets you verify the Drai's auto-add end to end (set the
    creds in Railway, deploy, check your email + the dashboard) before turning
    SPEAKEASY_AUTO on. No-op if the file isn't there."""
    path = os.path.join(os.path.dirname(__file__), "data", "speakeasy_test.json")
    try:
        with open(path) as fh:
            t = json.load(fh)
    except OSError:
        return
    if not (speakeasy.EMAIL and speakeasy.PASSWORD):
        log.info("speakeasy_test present but SPEAKEASY_EMAIL/PASSWORD not set; skipping")
        return
    guest = {"first": t.get("first", "Test"), "last": t.get("last", "Amy"),
             "male": t.get("male", 0), "female": t.get("female", 0),
             "name": f"{t.get('first','Test')} {t.get('last','Amy')}"}
    res = speakeasy.test_add_guest(t["date"], guest)
    status = "✓ added to SpeakeasyGo" if res["ok"] else f"✗ failed: {res['error']}"
    log.info("SpeakeasyGo test (%s %s, %s): %s", guest["first"], guest["last"], t["date"], status)
    body = (f"SpeakeasyGo auto-add test\n\nGuest: {guest['first']} {guest['last']}\n"
            f"Party: {guest['male']}M / {guest['female']}F\nNight: {t['date']}\n\n"
            f"Result: {status}\n\nScreenshot of the page attached. If ✓, check the SpeakeasyGo "
            f"dashboard for that night, then enable SPEAKEASY_AUTO. If ✗, send me the error + screenshot.")
    # Unique subject per run so nothing is deduped away during testing.
    import time as _t
    subject = f"Amy — SpeakeasyGo test {t['date']} {'OK' if res['ok'] else 'FAILED'} ({int(_t.time())})"
    attachments = [res["screenshot"]] if res.get("screenshot") and os.path.exists(res["screenshot"]) else None
    gmail_client.send_once(
        service, f"amy-speakeasy-test-{t['date']}@playmakerentertainment.com",
        rules.PLAYMAKER_EMAIL, subject, body, sender=rules.SENDER_EMAIL,
        attachments=attachments,
        dedupe_query=f'to:{rules.PLAYMAKER_EMAIL} subject:"{subject}"')


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
            issue = (
                "Amy hit an error partway through this request. A TAO registration may or may "
                "not have gone through — check TAO before re-submitting."
            )
            team_alert(service, message_id, {}, [issue], dry_run)
            _notify_exception(message_id, {}, issue, dry_run)
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

    if mode != "dry-run":
        try:
            run_speakeasy_test(service)  # one-shot, no-op unless the sentinel file is present
        except Exception:
            log.exception("SpeakeasyGo test run failed")

    wake = threading.Event()
    intake = None
    if args.loop or os.environ.get("PORT"):
        try:
            intake = posh_webhook.Intake(
                gmail_client.get_service, rules.INTAKE_EMAIL,
                dry_run=mode == "dry-run", on_accept=wake.set,
            )
            posh_webhook.serve(intake, store=newsletter_store())
        except Exception:
            log.exception("Posh webhook server failed to start; inbox loop continues")

    poll_seconds = max(int(os.environ.get("AMY_POLL_SECONDS", "120")), 30)
    follow_up_every = max(_int_env("FOLLOW_UP_INTERVAL_SECONDS", 600), 300)
    follow_up_backoff = max(_int_env("FOLLOW_UP_RATE_LIMIT_BACKOFF_SECONDS", 900), 300)
    next_follow_up = 0.0
    last_lineup_check = 0.0
    while True:
        if intake is not None:
            try:
                posh_webhook.retry_pending_website(intake)
            except Exception:
                log.exception("Pending website retry failed; will retry next cycle")
        try:
            run_once(service, mode, labels, allowlist, start_after)
        except Exception:
            if not args.loop:
                raise
            log.exception("Inbox check failed; will retry next cycle")
        # Follow-ups and the weekly lineup each get their own guard so a failure
        # in one never blocks the other (or the inbox) from running.
        try:
            # Wednesday-morning weekly newsletter to the team (dedupe makes the hourly
            # check safe — it sends once per week). On by default.
            lineup_on = os.environ.get("WEEKLY_LINEUP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
            if lineup_on and time.time() - last_lineup_check >= 3600:
                prep_weekly_lineup()
                send_weekly_lineup(service, mode)
                last_lineup_check = time.time()
        except Exception:
            if not args.loop:
                raise
            log.exception("Weekly lineup pass failed; will retry next cycle")
        try:
            # Follow-ups are ON by default now (wording approved, dedupe fixed,
            # sending from info@). Set FOLLOW_UPS_ENABLED=false in Railway to pause.
            follow_ups_on = os.environ.get("FOLLOW_UPS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
            if follow_ups_on and time.time() >= next_follow_up:  # every ~10 min
                next_follow_up = time.time() + follow_up_every
                send_follow_ups(service, mode, allowlist)
        except Exception as e:
            if gmail_client.is_rate_limited(e):
                next_follow_up = time.time() + follow_up_backoff
                log.warning("Follow-up pass rate limited by Gmail; backing off %ds", follow_up_backoff)
            elif not args.loop:
                raise
            else:
                log.exception("Follow-up pass failed; will retry next cycle")
        try:
            door_lists_on = os.environ.get("SCOTTSDALE_DOOR_LISTS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
            if door_lists_on:
                send_scottsdale_door_lists(service, mode)
        except Exception:
            if not args.loop:
                raise
            log.exception("Scottsdale door-list pass failed; will retry next cycle")
        if mode != "dry-run":
            try:
                run_job_watchdog(service)
            except Exception:
                if not args.loop:
                    raise
                log.exception("Job watchdog pass failed; will retry next cycle")
        if not args.loop:
            break
        tao_portal.reset_catalog()  # fresh promoter-page listings every cycle
        wake.wait(min(poll_seconds, 60) if job_tracker.has_open() else poll_seconds)
        wake.clear()


if __name__ == "__main__":
    main()

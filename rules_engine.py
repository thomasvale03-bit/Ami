"""
Decision logic for Amy. Pure functions where possible so this is easy to
unit-test independently of Gmail / TAO access.
"""
from datetime import datetime, timedelta
from config import rules


class ActionNeeded(Exception):
    """Raised whenever the operating instructions say 'stop and create an
    ACTION NEEDED exception' rather than guess."""
    def __init__(self, issue, required_action):
        self.issue = issue
        self.required_action = required_action
        super().__init__(issue)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_guest_request(raw):
    """
    raw: dict parsed from the incoming email (see gmail_client.parse_request).
    Applies the consent, guest-count, promoter and paused-venue rules.
    """
    # Consent gate (TAO authorization sec. 3-4): never accept terms for a
    # customer without documented consent from the intake form.
    if str(raw.get("authorization", "")).strip().upper() != "YES":
        raise ActionNeeded(
            issue="Customer authorization/21+ confirmation is missing or not YES.",
            required_action="Do not submit or accept terms. Get documented consent from the customer.",
        )
    if not raw.get("submitted_at"):
        raise ActionNeeded(
            issue="Submission timestamp missing, so consent cannot be documented.",
            required_action="Check the original form submission in the Airo inbox.",
        )

    female = _to_int(raw.get("female_count"))
    male = _to_int(raw.get("male_count"))
    if female == 0 and male == 0:
        raise ActionNeeded(
            issue="Both guest counts are zero — no party to register.",
            required_action="Confirm party size with the customer before processing.",
        )

    requested_venues = [rules.normalize_venue_name(v) for v in (raw.get("venues") or [])]
    drais = any(rules.is_drais(v) for v in requested_venues)
    requested_venues = [v for v in requested_venues if not rules.is_drais(v)]
    for v in requested_venues:
        if v in rules.PAUSED_VENUES:
            raise ActionNeeded(
                issue=f"{v} is paused and is not a fallback venue.",
                required_action="Do not submit. Wait for explicit reactivation.",
            )

    return {
        "first_name": raw.get("first_name") or (raw.get("name", "").split(" ")[0]),
        "last_name": raw.get("last_name", ""),
        "email": raw["email"],
        "phone": raw.get("phone") or rules.FALLBACK_PHONE,
        "billing_zip": raw.get("billing_zip") or rules.FALLBACK_BILLING_ZIP,
        "start_date": raw["start_date"],
        "end_date": raw["end_date"],
        "requested_venues": requested_venues,
        "drais": drais,
        "female_count": female,
        "male_count": male,
        "promoter": raw.get("promoter"),
        "source_message_id": raw["source_message_id"],
        "consent": {
            "authorization_accepted": True,
            "submitted_at": raw["submitted_at"],
            "originating_page": raw.get("originating_page"),
        },
    }


def date_range(start_date, end_date):
    d = datetime.fromisoformat(start_date).date()
    end = datetime.fromisoformat(end_date).date()
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def is_dayclub_season(date_obj):
    return date_obj.month < rules.DAYCLUB_SEASON_END_MONTH


def default_route_for_date(date_obj):
    return rules.DEFAULT_ROUTING[date_obj.strftime("%A")]


def candidate_venues(request, date_obj, previous_night_venue=None):
    """
    Ordered venue candidates for one date.
    1. Customer-requested venues first. When several were requested, the
       night's schedule decides which of them is tried first (owner decision
       2026-09-27); requested venues the schedule doesn't list follow in
       form order.
    2. Then the night's schedule: the weekday primary/fallbacks, then the
       extra backup nightclubs (not on Tuesday).
    Weekend rule: never repeat the previous night's nightclub on Fri->Sat;
    if Friday was JEWEL, prefer Hakkasan for Saturday.
    """
    day = date_obj.strftime("%A")
    route = default_route_for_date(date_obj)
    fallback = route["fallback"] if isinstance(route["fallback"], list) else [route["fallback"]]
    schedule = [route["primary"]] + fallback
    if day not in rules.NO_EXTRA_BACKUP_DAYS:
        schedule += rules.EXTRA_BACKUP_NIGHTCLUBS
    if day == "Saturday" and previous_night_venue == "JEWEL Nightclub":
        schedule.insert(0, "Hakkasan Nightclub")

    requested = [v for v in request["requested_venues"] if v in rules.NIGHTCLUBS]
    ordered = [v for v in schedule if v in requested] + requested + schedule

    seen, result = set(), []
    for v in ordered:
        if v == "best_available" or v in rules.PAUSED_VENUES or v in seen:
            continue
        if date_obj.strftime("%A") == "Saturday" and v == previous_night_venue:
            continue  # Fri+Sat duplicate-nightclub conflict
        seen.add(v)
        result.append(v)
    return result


def resolve_venue_for_date(request, date_obj, live_availability_checker, previous_night_venue=None):
    """
    live_availability_checker(venue, date_obj) -> dict or None
        {"event": str, "listing_type": "Passes"|"Tickets", ...}
        None means no live Passes/Guest List entry.
    Only a 'Passes' listing is ever used; paid 'Tickets' never are.
    """
    for venue in candidate_venues(request, date_obj, previous_night_venue):
        listing = live_availability_checker(venue, date_obj)
        if listing and listing.get("listing_type") == "Passes":
            return venue, listing
    return None, None


def dayclub_candidates(request):
    requested = [v for v in request["requested_venues"] if v in rules.DAYCLUB_PRIORITY]
    return list(dict.fromkeys(requested + rules.DAYCLUB_PRIORITY))


def resolve_dayclub_for_date(request, date_obj, live_availability_checker):
    """The one dayclub to add for this date, or (None, None) if none has a
    live free Pass. Missing a dayclub is normal, not an exception."""
    for venue in dayclub_candidates(request):
        listing = live_availability_checker(venue, date_obj)
        if listing and listing.get("listing_type") == "Passes":
            return venue, listing
    return None, None

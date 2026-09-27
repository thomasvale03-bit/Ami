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


def normalize_guest_request(raw):
    """
    raw: dict parsed from the incoming email (see gmail_client.parse_request).
    Applies the guest-count and promoter rules from section 3.
    """
    female = raw.get("female_count") or 0
    male = raw.get("male_count") or 0
    try:
        female = int(female)
    except (TypeError, ValueError):
        female = 0
    try:
        male = int(male)
    except (TypeError, ValueError):
        male = 0

    if female == 0 and male == 0:
        raise ActionNeeded(
            issue="Both guest counts are zero — no party to register.",
            required_action="Confirm party size with the customer before processing.",
        )

    requested_venues = raw.get("venues") or []
    if isinstance(requested_venues, str):
        requested_venues = requested_venues.split(",")
    requested_venues = [canonical_venue(v) for v in requested_venues if v and v.strip()]

    for v in requested_venues:
        if v in rules.PAUSED_VENUES:
            raise ActionNeeded(
                issue=f"{v} is paused and is not a fallback venue.",
                required_action="Do not submit. Wait for explicit reactivation.",
            )

    try:
        if date_range(raw["start_date"], raw["end_date"]) == []:
            raise ActionNeeded(
                issue=f"Visit end date {raw['end_date']} is before start date {raw['start_date']}.",
                required_action="Confirm the correct visit dates with the customer.",
            )
    except ValueError:
        raise ActionNeeded(
            issue=f"Unreadable visit dates: {raw['start_date']!r} to {raw['end_date']!r}.",
            required_action="Confirm the correct visit dates with the customer.",
        )

    return {
        "first_name": raw.get("first_name") or raw.get("name", "").split(" ")[0],
        "last_name": raw.get("last_name", ""),
        "email": raw["email"],
        "phone": raw.get("phone") or rules.FALLBACK_PHONE,
        "billing_zip": raw.get("billing_zip") or rules.FALLBACK_BILLING_ZIP,
        "start_date": raw["start_date"],
        "end_date": raw["end_date"],
        "requested_venues": requested_venues,
        "female_count": female,
        "male_count": male,
        "promoter": raw.get("promoter"),
        "source_message_id": raw["source_message_id"],
    }


def canonical_venue(name):
    """Map a submitted venue name onto the canonical name used in config/rules.py.

    Matches case-insensitively, and accepts a short form such as "OMNIA" or
    "Drai's" when it identifies exactly one known venue. Unknown names are
    returned stripped but otherwise unchanged.
    """
    name = name.strip()
    known = rules.ALL_AUTHORIZED_VENUES | rules.PAUSED_VENUES
    lowered = name.lower()
    for v in known:
        if v.lower() == lowered:
            return v
    prefix_matches = [v for v in known if v.lower().startswith(lowered)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return name


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
    day_name = date_obj.strftime("%A")
    return rules.DEFAULT_ROUTING[day_name]


def routed_candidates(date_obj, previous_night_venue=None):
    """Default-routing candidates for a date, in priority order.

    Applies the weekend rule: never route the same nightclub on Friday and
    Saturday, and prefer Hakkasan on Saturday when Friday was JEWEL.
    "best_available" expands to every other authorized nightclub.
    """
    route = default_route_for_date(date_obj)
    fallback = route["fallback"]
    fallback = fallback if isinstance(fallback, list) else [fallback]

    ordered = []
    for venue in [route["primary"]] + fallback:
        if venue == "best_available":
            ordered += rules.NIGHTCLUBS
        else:
            ordered.append(venue)

    if date_obj.strftime("%A") == "Saturday" and previous_night_venue:
        ordered = [v for v in ordered if v != previous_night_venue]
        if previous_night_venue == "JEWEL Nightclub":
            ordered.insert(0, "Hakkasan Nightclub")

    deduped = []
    for v in ordered:
        if v not in deduped:
            deduped.append(v)
    return deduped


def resolve_venue_for_date(request, date_obj, live_availability_checker,
                           previous_night_venue=None):
    """
    live_availability_checker(venue, date_obj) -> dict or None
        Expected dict shape: {"event": str, "listing_type": "Passes"|"Tickets",
                               "female_cutoff": str, "male_cutoff": str, ...}
        Must return None if there is no live Passes/Guest List entry.

    previous_night_venue: the venue already registered for the night before
        (used only for the Friday/Saturday no-repeat rule).

    Requested venue always has first priority (section 4), then the default
    routing table, then rules.LAST_RESORT_VENUES. The Fri/Sat no-repeat rule
    overrides everything, including a venue the guest explicitly requested.
    """
    route = default_route_for_date(date_obj)
    if route.get("only"):
        # This night has a single open venue; nothing else is tried.
        candidates = [route["primary"]]
    else:
        candidates = list(request["requested_venues"])
        for v in routed_candidates(date_obj, previous_night_venue) + rules.LAST_RESORT_VENUES:
            if v not in candidates:
                candidates.append(v)

    if date_obj.strftime("%A") == "Saturday" and previous_night_venue:
        candidates = [v for v in candidates if v != previous_night_venue]

    for venue in candidates:
        if venue in rules.PAUSED_VENUES:
            continue
        listing = live_availability_checker(venue, date_obj)
        if listing and listing.get("listing_type") == "Passes":
            return venue, listing

    return None, None

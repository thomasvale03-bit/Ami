"""
Playmaker Entertainment guest-list operating rules, encoded as data.
Source: Playmaker Guest-List Operating Instructions (v. Sept 2026).
Edit this file, not the logic in rules_engine.py, when a rule changes.
"""

# --- Scope ------------------------------------------------------------

NIGHTCLUBS = [
    "OMNIA Nightclub",
    "Hakkasan Nightclub",
    "JEWEL Nightclub",
    "Marquee Nightclub",
    "TAO Nightclub",
]

DAYLIFE_VENUES = [
    "TAO Beach Dayclub",
    "Marquee Dayclub",
    "OMNIA Dayclub",
    "Liquid Pool Lounge",
    "Palm Tree Beach Club",
    "LAVO Party Brunch",
]

PAUSED_VENUES = {"Drai's"}  # never submit unless explicitly reactivated

ALL_AUTHORIZED_VENUES = set(NIGHTCLUBS) | set(DAYLIFE_VENUES)

# --- Intake -------------------------------------------------------------

INTAKE_EMAIL = "valeconsultingaz@gmail.com"
PLAYMAKER_EMAIL = "team@playmakerentertainment.com"
SUBJECT_MARKERS = [
    "New guest list request submission",
    "New contact form message for Playmaker Entertainment via Guest List Request",
]

# Website/email venue names -> canonical names used in this config.
VENUE_ALIASES = {
    "omnia": "OMNIA Nightclub",
    "omnia nightclub": "OMNIA Nightclub",
    "hakkasan": "Hakkasan Nightclub",
    "hakkasan nightclub": "Hakkasan Nightclub",
    "jewel": "JEWEL Nightclub",
    "jewel nightclub": "JEWEL Nightclub",
    "marquee nightclub": "Marquee Nightclub",
    "marquee": "Marquee Nightclub",
    "marquee dayclub": "Marquee Dayclub",
    "omnia dayclub": "OMNIA Dayclub",
    "tao nightclub": "TAO Nightclub",
    "tao": "TAO Nightclub",
    "tao beach": "TAO Beach Dayclub",
    "tao beach dayclub": "TAO Beach Dayclub",
    "liquid pool lounge": "Liquid Pool Lounge",
    "liquid": "Liquid Pool Lounge",
    "palm tree beach club": "Palm Tree Beach Club",
    "palm tree beach": "Palm Tree Beach Club",
    "lavo party brunch": "LAVO Party Brunch",
    "lavo": "LAVO Party Brunch",
    "drai's": "Drai's",
    "drais": "Drai's",
}


def normalize_venue_name(name):
    key = name.strip().lower()
    return VENUE_ALIASES.get(key, name.strip())


# Fallback values ONLY used when TAO's form requires them and the guest
# submission did not supply a usable value.
FALLBACK_PHONE = "480-214-5268"
FALLBACK_BILLING_ZIP = "85306"

# --- Default nightclub routing (used only when no specific venue tied to
# a date, or requested venue is unavailable) ------------------------------

DEFAULT_ROUTING = {
    "Monday":    {"primary": "Marquee Nightclub", "fallback": "best_available"},
    "Tuesday":   {"primary": "OMNIA Nightclub", "fallback": ["TAO Nightclub", "best_available"]},
    "Wednesday": {"primary": "Hakkasan Nightclub", "fallback": "Marquee Nightclub"},
    "Thursday":  {"primary": "Hakkasan Nightclub", "fallback": "TAO Nightclub"},
    "Friday":    {"primary": "JEWEL Nightclub", "fallback": "Hakkasan Nightclub"},
    "Saturday":  {"primary": "JEWEL Nightclub", "fallback": "Marquee Nightclub"},
    "Sunday":    {"primary": "TAO Nightclub", "fallback": "best_available"},
}

# On any date with a live, free dayclub Pass, Amy books one dayclub IN
# ADDITION to the nightclub (never instead of it): a requested dayclub
# first, then this priority. Live TAO availability decides the season.
DAYCLUB_PRIORITY = [
    "TAO Beach Dayclub",
    "Marquee Dayclub",
    "OMNIA Dayclub",
    "Palm Tree Beach Club",
    "Liquid Pool Lounge",
]

# Tried after the weekday route, on every night except Tuesday, before giving
# up on a night (owner decision 2026-09-27).
EXTRA_BACKUP_NIGHTCLUBS = ["JEWEL Nightclub", "Hakkasan Nightclub", "Marquee Nightclub"]
NO_EXTRA_BACKUP_DAYS = {"Tuesday"}

# Never repeat the same nightclub Fri + Sat. If Friday resolves to JEWEL,
# prefer Hakkasan for Saturday when available (see rules_engine.apply_weekend_override).

# --- Per-venue qualification notes (reference values; the LIVE Passes/
# Guest List listing on tickets.taogroup.com always overrides these) ------

VENUE_RULES = {
    "OMNIA Nightclub": {
        "default_female_cutoff": "1:00 AM",
        "default_male_cutoff": "12:00 AM",
        "notes": "Verify every live listing; cutoffs have varied by event.",
    },
    "Hakkasan Nightclub": {
        "default_female_cutoff": "1:00 AM",
        "default_male_cutoff": "12:00 AM",
        "notes": "Some events cap complimentary male entry at first 300 arrivals.",
    },
    "JEWEL Nightclub": {
        "default_female_cutoff": "1:00 AM",
        "default_male_cutoff": "12:00 AM",
        "notes": "Some events use 1:30 AM instead — always confirm live.",
    },
    "Marquee Nightclub": {
        "default_female_cutoff": "1:00 AM",
        "default_male_cutoff": "12:00 AM",
        "notes": "Verify male eligibility and live cutoff each event.",
    },
    "TAO Nightclub": {
        "default_female_cutoff": "1:00 AM",
        "default_male_cutoff": "12:00 AM",
        "notes": "Ritual Sundays has used 1:30 AM — verify live.",
    },
    "TAO Beach Dayclub": {"default_arrival_cutoff": "3:00 PM", "notes": "Dayclub times change; verify live."},
    "Marquee Dayclub": {"notes": "No permanent cutoff; read live Passes listing."},
    "Liquid Pool Lounge": {"notes": "No permanent cutoff; read live Passes listing."},
    "Palm Tree Beach Club": {"notes": "No permanent cutoff; read live Passes listing."},
    "LAVO Party Brunch": {"notes": "No permanent cutoff; read live Passes listing."},
}

# Outdoor dayclubs stop being auto-scheduled from October 1 onward unless
# explicitly reactivated.
DAYCLUB_SEASON_END_MONTH = 10  # October
DAYLIFE_AUTO_VENUES_PAUSED_IN_OFFSEASON = {
    "TAO Beach Dayclub", "Marquee Dayclub", "Liquid Pool Lounge", "Palm Tree Beach Club"
}

# Exact promoter link named in the TAO authorization (section 5). Keep the
# utm_* parameters: they attribute bookings to Playmaker.
TAO_PROMOTER_URL = (
    "https://tickets.taogroup.com/promoter/68d79ff5-3d04-4198-83d7-00330a1e6107"
    "?utm_source=promoter&utm_id=68d79ff587c84397b19f00330a1e6107"
)

# Written provider authorization for automated $0 Guest List registrations.
# Signed copy: PME_POA.pdf (kept outside this repository). Scope: $0 Guest
# List/Pass options only, through the promoter link above; automated browser
# submission allowed; a Playmaker representative may accept the TicketDriver
# terms for a customer using the intake form's documented consent. TAO may
# revoke it with written notice to team@playmakerentertainment.com.
TAO_AUTOMATION_AUTHORIZATION = (
    "TAO Group Hospitality / TicketDriver authorization for automated $0 Guest "
    "List registrations, signed 2026-09-28 by Jonathan Sidara (Las Vegas "
    "Promotions Director, TAO Group Hospitality); acknowledged by Thomas Vale"
)

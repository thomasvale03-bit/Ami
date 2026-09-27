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

TAO_PROMOTER_URL = "https://tickets.taogroup.com/promoter/68d79ff5-3d04-4198-83d7-00330a1e6107"

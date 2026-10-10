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


def is_drais(name):
    """Drai's has no TAO portal: guests get the Drai's After Hours guest-list
    text in their confirmation instead (owner, Oct 4 2026)."""
    return "drai" in (name or "").lower().replace("’", "").replace("'", "")

ALL_AUTHORIZED_VENUES = set(NIGHTCLUBS) | set(DAYLIFE_VENUES)

# --- Intake -------------------------------------------------------------

INTAKE_EMAIL = "valeconsultingaz@gmail.com"
PLAYMAKER_EMAIL = "team@playmakerentertainment.com"

# Address Amy sends FROM. Defaults to the intake Gmail. Set AMY_SENDER_EMAIL
# to team@playmakerentertainment.com once that address is verified as a
# "Send mail as" alias (via Zoho SMTP) in the intake Gmail account, so
# confirmations are signed by the real domain and Apple/iCloud stop silently
# dropping them. Until then this stays the Gmail address so sends keep working.
import os as _os
SENDER_EMAIL = _os.environ.get("AMY_SENDER_EMAIL", "").strip() or INTAKE_EMAIL
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
    "drai’s": "Drai's",
    "dria's": "Drai's",  # common Posh typo ("Guestlist | Dria's After Hours")
    "drias": "Drai's",
}


def normalize_venue_name(name):
    key = name.strip().lower()
    return VENUE_ALIASES.get(key, name.strip())


# Fallback values used when a guest gave no phone / billing ZIP (website
# form, Posh webhook, assisted sign-up jobs). Override in Railway with
# DEFAULT_GUEST_PHONE / DEFAULT_BILLING_ZIP.
FALLBACK_PHONE = "4802649387"
FALLBACK_BILLING_ZIP = "85311"


def fallback_phone():
    import os
    return os.environ.get("DEFAULT_GUEST_PHONE", "").strip() or FALLBACK_PHONE


def fallback_billing_zip():
    import os
    return os.environ.get("DEFAULT_BILLING_ZIP", "").strip() or FALLBACK_BILLING_ZIP


def phone_or_default(value):
    return str(value or "").strip() or fallback_phone()


def zip_or_default(value):
    return str(value or "").strip() or fallback_billing_zip()

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

# JEWEL and Liquid are Playmaker's main clubs — always lead with them when
# they're live (owner rule 2026-10-10). JEWEL leads the nightclub order every
# day it's open; Liquid leads the dayclub order.
MAIN_NIGHTCLUB = "JEWEL Nightclub"
MAIN_DAYCLUB = "Liquid Pool Lounge"

# On any date with a live, free dayclub Pass, Amy books one dayclub IN
# ADDITION to the nightclub (different time slot — 11:30a–6p vs 10:30p–4a, so
# this is not "doubling" a night): a requested dayclub first, then Liquid (the
# main dayclub), then this priority. Live TAO availability decides the season.
DAYCLUB_PRIORITY = [
    "Liquid Pool Lounge",
    "TAO Beach Dayclub",
    "Marquee Dayclub",
    "OMNIA Dayclub",
    "Palm Tree Beach Club",
]

# Tried after the weekday route, on every night except Tuesday, before giving
# up on a night (owner decision 2026-09-27).
EXTRA_BACKUP_NIGHTCLUBS = ["JEWEL Nightclub", "Hakkasan Nightclub", "Marquee Nightclub"]
NO_EXTRA_BACKUP_DAYS = {"Tuesday"}

# Never repeat the same nightclub on back-to-back nights: candidate_venues moves
# last night's club to the end so a stay varies clubs and JEWEL gets pushed as
# the next night's lead (e.g. Hakkasan Fri -> JEWEL Sat).

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


# Posh recurring series: child events can be RENAMED ("R&BAE | Hakkasan"),
# but their slugs keep the series' original name. Known series, keyed by the
# first 18 hex chars of the child event ids (ids in one series share them),
# with their slug base and UTC end-time slug parts. Amy also learns new ones
# from every Posh page she reads. Extra entries: env POSH_SERIES_SEEDS as
# "prefix=base:8-30/9-30;prefix2=base2:11-30".
POSH_SERIES_SEEDS = {
    "6a682ff9374ee034d5": ("guest-list-hakkasan", ("11-30", "12-30")),
    "6a80e79648e2b4a133": ("guestlist-tao-nc", ("8-30", "9-30")),
}

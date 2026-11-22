"""Per-night guest-list terms for LIV / LIV Beach / XS confirmations (Thomas, Nov 21
2026: "I will provide the rule set of the night weekly").

XS terms live in a parallel file (XS_NIGHT_RULES env, default
/home/box/agent-data/shared/xs-night-rules.json, keys "XS Nightclub|YYYY-MM-DD",
same fields plus "drinks"). load() merges both files. A night whose terms say
"no list" for every gender in the party (or "no_list": true) is treated as a
closed night by the worker: nothing is submitted, XS/LIV fall back to OMNIA.

The file is read at send time, so editing it needs no restart or deploy.
Path: LIV_NIGHT_RULES env, default /home/box/agent-data/shared/liv-night-rules.json

Format (JSON). Only the "nights" object is real data; keys starting with "_"
are documentation and are never used:

    {
      "nights": {
        "LIV Nightclub|2026-11-28": {
          "event": "Dom Dolla",            # optional; defaults to the urvenue event title
          "men": "Comp",                   # M line
          "women": "Comp Open Bar",        # F line
          "cutoff": "1:30am",              # "Guestlist til <cutoff>"
          "notes": "Optional extra line"   # optional
        },
        "LIV Beach|2026-11-29": { ... }
      }
    }

Venue part of the key: "LIV Nightclub" (also accepted: "LIV") or "LIV Beach".
Date: the guest's night, YYYY-MM-DD. Never guessed: a night with no entry (or a
missing field) simply leaves that line out of the email.
"""
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("amy.liv_rules")

DEFAULT_PATH = "/home/box/agent-data/shared/liv-night-rules.json"
XS_DEFAULT_PATH = "/home/box/agent-data/shared/xs-night-rules.json"
FIELDS = ("event", "men", "women", "cutoff", "drinks", "notes")
VENUE_KEYS = {"LIV Nightclub": ("LIV Nightclub", "LIV"), "LIV Beach": ("LIV Beach", "LIV Dayclub"),
              "XS Nightclub": ("XS Nightclub", "XS")}
NO_LIST_RE = re.compile(r"^\s*no\s+(?:guest\s*)?list\b", re.I)

TEMPLATE = {
    "_format": ("Keys: '<venue>|YYYY-MM-DD' with venue 'LIV Nightclub' or 'LIV Beach'. Fields: event (optional, "
                "defaults to the urvenue event title), men, women, cutoff, notes (all optional). Only 'nights' is "
                "used; entries under '_example' are documentation from Tom's sample (Nov 21 2026), never real terms."),
    "_example": {
        "LIV Nightclub|YYYY-MM-DD (a Thursday)": {"event": "EXAMPLE", "men": "Comp", "women": "Comp Open Bar",
                                                  "cutoff": "2am"},
        "LIV Nightclub|YYYY-MM-DD (a Saturday)": {"event": "Dom Dolla", "men": "Comp", "women": "Comp Open Bar",
                                                  "cutoff": "1:30am"},
    },
    "nights": {},
}


def path():
    return Path(os.environ.get("LIV_NIGHT_RULES", "").strip() or DEFAULT_PATH)


def xs_path():
    return Path(os.environ.get("XS_NIGHT_RULES", "").strip() or XS_DEFAULT_PATH)


def _nights(p):
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("night rules file unreadable: %s", p)
        return {}
    nights = data.get("nights") if isinstance(data, dict) else None
    return nights if isinstance(nights, dict) else {}


def load():
    """{'<venue>|<date>': {...}} from the LIV and XS files' "nights" (merged);
    {} when missing or bad."""
    return {**_nights(path()), **_nights(xs_path())}


def for_night(venue, night, rules=None):
    """Clean rule dict for venue + YYYY-MM-DD, or {} (never invented)."""
    rules = load() if rules is None else rules
    for name in VENUE_KEYS.get(venue, (venue,)):
        entry = rules.get(f"{name}|{night}")
        if isinstance(entry, dict):
            out = {k: str(entry[k]).strip() for k in FIELDS if entry.get(k) not in (None, "")
                   and str(entry[k]).strip()}
            if entry.get("no_list") is True:
                out["no_list"] = True
            return out
    return {}


def is_no_list(rule, female=1, male=1):
    """True when the night's terms say there is no guest list for every gender
    in the party (or "no_list": true). Such a night is booked as closed."""
    if not rule:
        return False
    if rule.get("no_list") is True:
        return True
    genders = [g for g, n in (("women", female), ("men", male)) if int(n or 0) > 0] or ["women", "men"]
    return all(NO_LIST_RE.match(rule.get(g) or "") for g in genders)


def has_terms(rule):
    return bool(rule.get("men") or rule.get("women") or rule.get("cutoff"))


def ensure_file():
    """Create the rules file with documentation only (no real nights) if absent."""
    p = path()
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(TEMPLATE, indent=2, ensure_ascii=False) + "\n")
    return True

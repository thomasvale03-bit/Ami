"""Nightly plan for one multi-night, multi-venue guest-list request.

Thomas (Nov 19 2026): a request with a date range is ONE submission, ONE
booking job and ONE guest + ONE team confirmation covering every night, never
one submission/email per venue or night.

A plan maps each ISO date in the request to the venue(s) for that date, e.g.
{"2027-01-28": ["Hakkasan Nightclub", "Marquee Dayclub"], "2027-01-29": [...]}.
On the intake notice it is one line:

    Nightly plan: 2027-01-28 Hakkasan + Marquee Dayclub; 2027-01-29 Marquee + Marquee Dayclub

Without a plan, a job keeps the old meaning (every requested venue on every
night of the range). With a plan, only the planned venue/date pairs are
requested; dates in the range that are not in the plan request nothing.
Pure functions, no I/O.
"""
from datetime import date, timedelta
import re

PLAN_LABEL = "Nightly plan"
_ENTRY_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:\-]?\s*(.+?)\s*$")


def _norm(venue):
    from config import rules
    return rules.normalize_venue_name(str(venue).strip())


def _venue_list(value):
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"\s*\+\s*|\s*,\s*|\s+&\s+", str(value or ""))
    out = []
    for v in items:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def parse_plan(value, normalize=True):
    """Plan text / dict / list -> {iso: [venue, ...]} (dates sorted).

    Accepts the notice line ("2027-01-28 Hakkasan + Marquee Dayclub; ..."),
    a dict {iso: venue | [venues]} or a list of {"date", "venue"|"venues"}.
    Raises ValueError on anything malformed (never guesses)."""
    plan = {}

    def add(day, venues):
        day = str(day or "").strip()
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError(f"bad plan date {day!r}")
        names = _venue_list(venues)
        if not names:
            raise ValueError(f"no venue for {day}")
        for v in names:
            v = _norm(v) if normalize else v
            if v not in plan.setdefault(day, []):
                plan[day].append(v)

    if value is None or value == "" or value == {} or value == []:
        return {}
    if isinstance(value, dict):
        for day, venues in value.items():
            add(day, venues)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("plan entries must be objects")
            add(item.get("date"), item.get("venues") if item.get("venues") else item.get("venue"))
    else:
        for part in str(value).split(";"):
            if not part.strip():
                continue
            m = _ENTRY_RE.match(part)
            if not m:
                raise ValueError(f"bad plan entry {part.strip()!r}")
            add(m.group(1), m.group(2))
    return {d: plan[d] for d in sorted(plan)}


def format_plan(plan):
    """{iso: [venues]} -> 'iso A + B; iso C' (no commas, so it survives parsers)."""
    return "; ".join(f"{d} {' + '.join(vs)}" for d, vs in sorted((plan or {}).items()))


def plan_venues(plan):
    out = []
    for _d, vs in sorted((plan or {}).items()):
        for v in vs:
            if v not in out:
                out.append(v)
    return out


def _range(start, end):
    d, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    while d <= e:
        yield d
        d += timedelta(days=1)


def raw_plan(raw):
    """The normalized plan saved on a job (raw['itinerary']), or {}."""
    value = (raw or {}).get("itinerary")
    return parse_plan(value) if value else {}


def plan_problem(raw):
    """'' if the job's plan (if any) is usable, else a review reason."""
    try:
        plan = raw_plan(raw)
    except ValueError as exc:
        return f"Nightly plan unreadable ({exc}); manual review required"
    if not plan:
        return ""
    try:
        days = {d.isoformat() for d in _range(raw["start_date"], raw["end_date"])}
    except (KeyError, TypeError, ValueError):
        return "Nightly plan without a valid date range; manual review required"
    outside = sorted(set(plan) - days)
    if outside:
        return f"Nightly plan date(s) {', '.join(outside)} outside the request range; manual review required"
    requested = {_norm(v) for v in raw.get("venues") or []}
    extra = [v for v in plan_venues(plan) if v not in requested]
    if extra:
        return f"Nightly plan venue(s) {', '.join(extra)} not in the requested venues; manual review required"
    return ""


def requested_pairs(raw):
    """[(venue, iso)] the job asks for, date order then venue order.

    No plan: every requested venue on every night (unchanged behavior).
    Plan: exactly the planned venue/date pairs."""
    venues = [_norm(v) for v in (raw.get("venues") or [])]
    plan = raw_plan(raw)
    out = []
    for d in _range(raw["start_date"], raw["end_date"]):
        iso = d.isoformat()
        names = plan.get(iso, []) if plan else venues
        for v in names:
            if (v, iso) not in out:
                out.append((v, iso))
    return out

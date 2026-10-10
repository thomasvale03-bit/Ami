"""Who's playing each venue each night, read from the weekly flyers.

data/headliners.json is {date: {venue: artist}} and is refreshed from the
TAO Group weekly-assets Dropbox (tools/scan_flyers.py). Amy uses it to add
the headliner to a guest's confirmation ("OMNIA Nightclub (Steve Aoki)").
Purely cosmetic: a missing headliner just means no extra note.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "data", "headliners.json")
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with open(_PATH) as fh:
                _cache = json.load(fh)
        except Exception:  # noqa: BLE001 - no data file just means no headliners
            _cache = {}
    return _cache


def headliner(venue, day):
    """Artist headlining `venue` on `day` (a date or ISO string), or None."""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return (_load().get(iso) or {}).get(venue)


def reload_headliners():
    global _cache
    _cache = None

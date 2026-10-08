"""Read live events from TicketSauce's public events feed (the same data the
promoter "master link" widget shows), so Amy always knows what's actually open
without touching the Cloudflare-gated pages.

The feed is public and unauthenticated:
    https://events.ticketsauce.com/events/events_by_organization/<pid>

It lists every TAO event; the free guest lists are the ones whose name starts
with "Guest List -". Each carries the venue (`location`), local start time,
and the event URL. This is read-only; it cannot sign anyone up.
"""
import json
import logging
import os
import urllib.request
from datetime import datetime

log = logging.getLogger("amy.ticketsauce")

# TAO's TicketSauce organization id (the "pid" in the promoter widget).
ORG_PID = os.environ.get("TICKETSAUCE_PID", "61aa6d22-0a44-4f7b-9a62-5d970ad1213e")
FEED_URL = "https://events.ticketsauce.com/events/events_by_organization/"

GUEST_LIST_PREFIX = "guest list"


def _fetch():
    req = urllib.request.Request(
        FEED_URL + ORG_PID,
        headers={"User-Agent": "Mozilla/5.0 (PlaymakerAmy)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode())


def live_catalog():
    """{(venue, date): {"url", "event", "event_time"}} for live guest-list
    events. Returns {} on any failure (so callers degrade gracefully)."""
    try:
        payload = _fetch()
    except Exception as exc:  # noqa: BLE001 - a read failure means "unknown", not a crash
        log.info("TicketSauce feed read failed: %s", exc)
        return {}

    catalog = {}
    for wrapper in (payload.get("data") or {}).values():
        event = wrapper.get("Event") or {}
        name = (event.get("name") or "").strip()
        if not name.lower().startswith(GUEST_LIST_PREFIX):
            continue
        if event.get("tickets_active") is False:
            continue
        venue = (event.get("location") or "").strip()
        if not venue:
            continue
        try:
            start = datetime.strptime((event.get("start") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        catalog.setdefault((venue, start.date()), {
            "url": event.get("url") or event.get("tickets_url") or "",
            "event": name,
            "event_time": start.strftime("%I:%M %p").lstrip("0"),
        })
    return catalog

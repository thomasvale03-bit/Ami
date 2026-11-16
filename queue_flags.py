"""Venue changes and same-night double bookings, so they never hide in a queue
state (Thomas, Nov 16 2026: "I cant keep finding these issues").

Read-only projections over the queue plus ONE alert email per flagged job to
team@ via the branded sender (info@). Never cancels, rebooks or emails a guest:
orders are never canceled automatically.
"""
import html as _html
import logging
from datetime import date

log = logging.getLogger("amy.queue_flags")

# A night is a "night order" when it is a nightclub order with an order ID.
# The Marquee Dayclub add-on (same date, daytime) and Drai's email-only
# entries are not counted as a second order for the night.


def _requested(raw):
    from config import rules
    return {rules.normalize_venue_name(v) for v in raw.get('venues') or []}


def _requested_nights(raw):
    try:
        start, end = date.fromisoformat(raw['start_date']), date.fromisoformat(raw['end_date'])
    except (KeyError, TypeError, ValueError):
        return set()
    return {date.fromordinal(d).isoformat() for d in range(start.toordinal(), end.toordinal() + 1)}


def _guest(raw):
    name = raw.get('name') or ' '.join(x for x in (raw.get('first_name'), raw.get('last_name')) if x)
    return ' '.join(str(name or '').split())


def job_bookings(row):
    from worker_store import Store
    from config import rules
    out = []
    for rec in Store.saved_bookings(row.get('metadata') or {}):
        venue = rules.normalize_venue_name(str(rec.get('venue') or ''))
        out.append({**rec, 'venue': venue})
    return out


def job_venue_changes(row):
    """Booked records whose venue is not one the guest asked for (excluding
    the approved Marquee Dayclub add-on on a requested night)."""
    from queue_service import MDC_ADDON_VENUE
    raw = row.get('raw') or {}
    requested, nights = _requested(raw), _requested_nights(raw)
    changes = []
    for rec in job_bookings(row):
        if rec['venue'] in requested:
            continue
        if rec['venue'] == MDC_ADDON_VENUE and rec.get('date') in nights:
            continue
        changes.append({'ref': row['ref'], 'state': row['state'], 'guest': _guest(raw),
                        'email': (raw.get('email') or '').strip().lower(),
                        'requested_venues': sorted(requested), 'date': rec.get('date'),
                        'booked_venue': rec['venue'], 'order_id': rec.get('order_id')})
    return changes


def _night_orders(rows):
    """(email, night) -> [order dicts] over every job with verified bookings."""
    groups = {}
    for row in rows:
        raw = row.get('raw') or {}
        email = (raw.get('email') or '').strip().lower()
        if not email:
            continue
        for rec in job_bookings(row):
            if 'Nightclub' not in rec['venue'] or not rec.get('order_id'):
                continue
            item = {'ref': row['ref'], 'state': row['state'], 'venue': rec['venue'], 'order_id': rec['order_id'],
                    'requested_venues': sorted(_requested(raw)), 'guest': _guest(raw)}
            bucket = groups.setdefault((email, rec.get('date')), [])
            if all(o['order_id'] != item['order_id'] for o in bucket):
                bucket.append(item)
    return groups


def upcoming_flags(s, today):
    """Every venue change and same-night double booking on today or later."""
    today = today.isoformat() if isinstance(today, date) else str(today)
    rows = s.all_jobs()
    changes = [c for row in rows for c in job_venue_changes(row) if (c['date'] or '') >= today]
    doubles = []
    for (email, night), orders in sorted(_night_orders(rows).items(), key=lambda kv: (kv[0][1] or '', kv[0][0])):
        if (night or '') >= today and len(orders) >= 2:
            doubles.append({'guest': orders[0]['guest'], 'email': email, 'date': night,
                            'order_count': len(orders), 'orders': orders})
    changes.sort(key=lambda c: (c['date'] or '', c['guest']))
    return {'today': today, 'venue_changes': changes, 'double_bookings': doubles,
            'counts': {'venue_changes': len(changes), 'double_bookings': len(doubles)}}


def _long(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A %B')} {d.day}"


def _short(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a %b')} {d.day}"


def alert_payload(s, ref, today):
    """ONE team@ email for this job's flagged nights (today or later), or None.

    Guest name, requested venue/date, booked venue, order IDs. No phone numbers,
    no Jarvis. Subject has no commas/semicolons (send-branded rule)."""
    from config import rules
    today = today.isoformat() if isinstance(today, date) else str(today)
    rows = s.all_jobs()
    row = next((r for r in rows if r['ref'] == ref), None)
    if row is None:
        return None
    raw = row.get('raw') or {}
    email = (raw.get('email') or '').strip().lower()
    changes = [c for c in job_venue_changes(row) if (c['date'] or '') >= today]
    groups = _night_orders(rows)
    nights = sorted({rec.get('date') for rec in job_bookings(row) if (rec.get('date') or '') >= today})
    doubles = {n: groups.get((email, n)) for n in nights
               if len(groups.get((email, n)) or []) >= 2 and any(o['ref'] == ref for o in groups[(email, n)])}
    if not changes and not doubles:
        return None
    flagged_nights = sorted({c['date'] for c in changes} | set(doubles))
    kinds = []
    if changes:
        kinds.append('Venue change')
    if doubles:
        most = max(len(v) for v in doubles.values())
        kinds.append(f'Double booking ({most} orders)')
    guest = _guest(raw) or ref
    label = ' and '.join(_short(n) for n in flagged_nights)
    subject = f"AMY alert: {' + '.join(kinds)} - {guest} {label}"
    subject = ''.join(ch for ch in subject if ch not in ',;\r\n')
    lines = ["AMY flagged a booking for you to look at. Nothing was canceled or rebooked "
             "(orders are never canceled automatically).", "",
             f"Guest: {guest} ({email})",
             f"Requested: {', '.join(sorted(_requested(raw)))} on "
             f"{' and '.join(_long(n) for n in sorted(_requested_nights(raw)))} (Amy Ref {ref})", ""]
    for night in flagged_nights:
        lines.append(f"— {_long(night)} —")
        for c in [c for c in changes if c['date'] == night]:
            lines.append(f"Venue change: booked {c['booked_venue']} instead of "
                         f"{', '.join(c['requested_venues'])} (Order ID {c['order_id']})")
        if night in doubles:
            lines.append(f"Double booking: {len(doubles[night])} orders for this guest on this night:")
            for o in doubles[night]:
                lines.append(f"  • {o['venue']} — Order ID {o['order_id']} (requested "
                             f"{', '.join(o['requested_venues'])}; Amy Ref {o['ref']}; {o['state']})")
        lines.append("")
    lines.append("Full list: GET /queue/flags")
    text = "\n".join(lines)
    body = "<pre style=\"font-family:Helvetica,Arial,sans-serif;white-space:pre-wrap\">" + _html.escape(text) + "</pre>"
    return {'to': rules.PLAYMAKER_EMAIL, 'subject': subject, 'text': text, 'html': body}


def send_alert(s, ref, now, today, sender):
    """Build and send the alert for ref (if flagged); record the outcome on the
    job (metadata.flag_alert). /send-branded dedupes on recipient + subject, so
    the same flag is never emailed twice."""
    payload = alert_payload(s, ref, today)
    if payload is None:
        return None
    try:
        code, result = sender(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("Flag alert send failed for %s", ref)
        code, result = 0, f"error: {type(exc).__name__}"
    outcome = {'subject': payload['subject'], 'at': now, 'code': code,
               'result': result if isinstance(result, (str, dict)) else str(result)}
    try:
        s.set_meta(ref, 'flag_alert', outcome)
    except Exception:  # noqa: BLE001
        log.exception("Could not record flag alert for %s", ref)
    return outcome

"""Keyword commands (fallback when OPENAI_API_KEY is not set, and always
available as exact commands). Read-only."""
import re

from .views import ALL_STATES

HELP = """*AMY Worker* (read-only) \u2014 I can answer:
\u2022 `status` \u2014 queue counts for every state (queued, retry, claimed, running, submission_uncertain, needs_review, manual, done\u2026)
\u2022 `state <name>` \u2014 jobs in one state, e.g. `state manual`, `state queued`
\u2022 `guest <name>` \u2014 every job for a guest (venue, date, state, order ref)
\u2022 `tonight` / `tomorrow` / `date 11/21` \u2014 that night's bookings by venue
\u2022 `health` \u2014 booking worker last activity, in-flight jobs, missed pickups
\u2022 `help` \u2014 this list
I can't book, cancel, requeue or email from Slack."""

STATE_ALIASES = {'uncertain': 'submission_uncertain', 'review': 'needs_review', 'needs review': 'needs_review',
                 'booked': 'done', 'awaiting': 'awaiting_human_verification'}


def strip_mentions(text):
    return re.sub(r'<@[A-Z0-9]+>', ' ', text or '').strip()


def parse(text):
    """-> (command, arg) or (None, None) when it isn't a keyword command."""
    t = ' '.join(strip_mentions(text).split())
    low = t.lower().rstrip('?.! ')
    if low in ('', 'help', 'commands', '?', 'hi', 'hello'):
        return 'help', ''
    if low in ('status', 'queue', 'queue status', 'counts'):
        return 'status', ''
    if low in ('health', 'worker', 'worker health', 'heartbeat'):
        return 'health', ''
    if low in ('tonight', 'today', 'tomorrow'):
        return 'date', low
    m = re.fullmatch(r'(?:date|night|on)\s+(.+)', low)
    if m:
        return 'date', m.group(1)
    m = re.fullmatch(r'(?:guest|lookup|find)\s+(.+)', t, re.I)
    if m:
        return 'guest', m.group(1).strip()
    m = re.fullmatch(r'(?:state|list)\s+(.+)', low)
    if m:
        s = m.group(1).strip()
        return 'state', STATE_ALIASES.get(s, s.replace(' ', '_'))
    if low in ALL_STATES or low in STATE_ALIASES:
        return 'state', STATE_ALIASES.get(low, low)
    return None, None


def run(lookups, cmd, arg):
    if cmd == 'help':
        return HELP
    if cmd == 'status':
        return lookups.render_status(lookups.queue_status())
    if cmd == 'health':
        return lookups.render_health(lookups.worker_health())
    if cmd == 'date':
        return lookups.render_date(lookups.bookings_on(arg))
    if cmd == 'guest':
        return lookups.render_guest(lookups.guest(arg))
    if cmd == 'state':
        return lookups.render_state(lookups.jobs_in_state(arg))
    return HELP

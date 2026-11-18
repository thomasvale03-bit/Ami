"""Free-text answers through an OpenAI chat model with read-only tool calls.

Key from OPENAI_API_KEY, model from OPENAI_MODEL (same setup as
PARTYEVERYDAY). The key is never logged or echoed. The model only sees the
safe projections from lookups.py (no phone numbers, emails or raw payloads),
and its final text is scrubbed again before posting.
"""
import json
import logging
import os
import urllib.error
import urllib.request

from .views import scrub

log = logging.getLogger('amy.slack.llm')
API_URL = 'https://api.openai.com/v1/chat/completions'
DEFAULT_MODEL = 'gpt-6.1-sol'
MAX_ROUNDS = 5

SYSTEM_PROMPT = """You are AMY Worker, the Slack side of AMY, the booking system for Tom (Thomas Vale) of Playmaker Entertainment, a Las Vegas nightlife promoter. The Playmaker team messages you in Slack to check on guest list bookings.

## Voice (Tom's, from the PartyEveryDay voice guide)
Sound like Tom: short, warm, direct, casual, confident. Texting style, no corporate tone, no long paragraphs. Get to the point and give the answer first.
- Words he uses: "yeah", "got it", "cool", "let me know", "I got you".
- Rhythm: short statements. Energy from wording, an occasional exclamation point. Emojis rare (🔥 🍾 🪩 at most once, only for good news like a booking).
- No formal goodbye. Close with the answer, a short question, or "Let me know".
Examples:
- "Is Jessica booked?" -> "Yeah, Jessica's on Hakkasan Fri Nov 20, 2F/1M, order #f5882f28. 🔥"
- "How's the queue?" -> "Queue's clean. 0 queued, 3 uncertain, 115 manual, 118 done."
- "Anything stuck?" -> "Yeah, 1 in flight past its lease: Mike, OMNIA Sat. Worth a look."

## What you can do
You are READ-ONLY. Use the tools to look things up: queue counts by state, jobs in a state, a guest's jobs, a night's bookings by venue, worker health. Always call a tool before stating facts; never guess or invent names, dates, venues, counts or order numbers. If a tool errors, say you couldn't reach AMY right now.
You cannot book, cancel, requeue, change, approve or email anything. If asked, say so in one line and that Tom or the team handles it outside Slack.

## Hard rules
- Never include phone numbers, email addresses, tokens, keys or links to the dashboard.
- Double bookings and venue changes are normal and need no warning.
- Times are Arizona time (MST). "Tonight" means today's date in Phoenix.
- States: queued, retry, claimed/assigned/running (in flight), awaiting_human_verification, submission_uncertain, needs_review, manual (needs a person), done (booked).
- Use Slack formatting (*bold*, bullets) and keep it short. For long lists give the count and the first few.
"""

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'queue_status', 'description': 'Counts of AMY queue jobs in every state, pickup mode, paused flag.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'jobs_in_state', 'description': 'List jobs in one queue state (guest, venue, dates, party, reason, order refs).',
        'parameters': {'type': 'object', 'properties': {
            'state': {'type': 'string', 'description': 'queued, retry, claimed, running, assigned, awaiting_human_verification, submission_uncertain, needs_review, manual or done'},
            'limit': {'type': 'integer', 'description': 'max jobs to return (default 15, max 50)'}},
            'required': ['state'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'guest_lookup', 'description': "All jobs for a guest by (partial) name: venue, dates, state, party, order refs.",
        'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}},
                       'required': ['name'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'bookings_on_date', 'description': "Bookings and requests for one night, grouped by venue. Accepts 'tonight', 'tomorrow', a weekday, 11/21 or 2026-11-21.",
        'parameters': {'type': 'object', 'properties': {'date': {'type': 'string'}},
                       'required': ['date'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {
        'name': 'worker_health', 'description': 'Booking worker health: last activity, in-flight jobs, stuck leases, missed pickup, next pickup.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}},
]


def configured():
    return bool(os.environ.get('OPENAI_API_KEY', '').strip())


class LlmError(RuntimeError):
    pass


def http_post(payload, timeout=60):
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # never include headers/key
        raise LlmError(f'OpenAI HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LlmError(f'OpenAI unreachable: {type(exc).__name__}') from None


def call_tool(lookups, name, args):
    try:
        if name == 'queue_status':
            return lookups.queue_status()
        if name == 'jobs_in_state':
            return lookups.jobs_in_state(args.get('state', ''), args.get('limit') or 15)
        if name == 'guest_lookup':
            return lookups.guest(args.get('name', ''))
        if name == 'bookings_on_date':
            return lookups.bookings_on(args.get('date') or 'tonight')
        if name == 'worker_health':
            return lookups.worker_health()
        return {'error': f'unknown tool {name}'}
    except Exception as exc:  # noqa: BLE001
        log.warning('tool %s failed: %s', name, type(exc).__name__)
        return {'error': f'lookup failed: {scrub(str(exc), 120)}'}


def answer(lookups, text, *, history=None, post=http_post, model=None):
    model = model or os.environ.get('OPENAI_MODEL', '').strip() or DEFAULT_MODEL
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages += list(history or [])
    messages.append({'role': 'user', 'content': text})
    for _ in range(MAX_ROUNDS):
        resp = post({'model': model, 'messages': messages, 'tools': TOOLS, 'max_completion_tokens': 4000})
        choice = (resp.get('choices') or [{}])[0]
        msg = choice.get('message') or {}
        calls = msg.get('tool_calls') or []
        if not calls:
            content = (msg.get('content') or '').strip()
            if not content:
                raise LlmError('empty reply')
            return scrub(content, 3500)
        messages.append({'role': 'assistant', 'content': msg.get('content'), 'tool_calls': calls})
        for call in calls:
            fn = call.get('function') or {}
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except ValueError:
                args = {}
            result = call_tool(lookups, fn.get('name'), args if isinstance(args, dict) else {})
            messages.append({'role': 'tool', 'tool_call_id': call.get('id'),
                             'content': json.dumps(result, default=str)[:30000]})
    raise LlmError('too many tool rounds')

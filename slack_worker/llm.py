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
API_URL = 'https://api.openai.com/v1/chat/completions'  # plain self-check (same as PARTYEVERYDAY)
RESPONSES_URL = 'https://api.openai.com/v1/responses'  # function tools (required for gpt-6.1-sol)
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


def reasoning_effort():
    """gpt-6.1-sol rejects function tools on /v1/chat/completions (any
    reasoning_effort other than 'none', and 'none' is not supported), so tool
    calls go through /v1/responses. Default effort 'low' keeps Slack replies
    quick; OPENAI_REASONING_EFFORT overrides ('' omits the parameter)."""
    return os.environ.get('OPENAI_REASONING_EFFORT', 'low').strip()


def responses_tools():
    """Chat Completions tool specs -> Responses API function tools."""
    return [{'type': 'function', 'name': t['function']['name'], 'description': t['function']['description'],
             'parameters': t['function']['parameters']} for t in TOOLS]


def responses_payload(model, input_items, *, instructions=None, previous=None, max_tokens=4000):
    payload = {'model': model, 'input': input_items, 'tools': responses_tools(), 'max_output_tokens': max_tokens}
    if instructions:
        payload['instructions'] = instructions
    if previous:
        payload['previous_response_id'] = previous
    effort = reasoning_effort()
    if effort:
        payload['reasoning'] = {'effort': effort}
    return payload


def output_text(resp):
    if isinstance(resp.get('output_text'), str) and resp['output_text'].strip():
        return resp['output_text']
    parts = []
    for item in resp.get('output') or []:
        if item.get('type') == 'message':
            for c in item.get('content') or []:
                if c.get('type') in ('output_text', 'text') and c.get('text'):
                    parts.append(c['text'])
    return '\n'.join(parts)


def function_calls(resp):
    return [i for i in resp.get('output') or [] if i.get('type') == 'function_call']


def configured():
    return bool(os.environ.get('OPENAI_API_KEY', '').strip())


class LlmError(RuntimeError):
    """status + OpenAI's error type/code/param/message (scrubbed; never the key)."""

    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


def error_detail(body):
    """OpenAI error JSON -> safe dict. Messages are scrubbed (a 401 can echo a
    masked key fragment)."""
    try:
        err = json.loads(body).get('error') or {}
    except (ValueError, AttributeError):
        return {'message': scrub(str(body or '')[:300], 300)}
    if not isinstance(err, dict):
        return {'message': scrub(str(err), 300)}
    return {k: scrub(str(err.get(k)), 300) for k in ('type', 'code', 'param', 'message') if err.get(k) is not None}


def describe(exc):
    if isinstance(exc, LlmError) and exc.detail:
        bits = ' '.join(f'{k}={v!r}' for k, v in exc.detail.items())
        return f'{exc} ({bits})'
    return scrub(str(exc), 400)


def http_post(payload, timeout=60, url=API_URL):
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # never include headers/key
        try:
            body = exc.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            body = ''
        raise LlmError(f'OpenAI HTTP {exc.code}', status=exc.code, detail=error_detail(body)) from None
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


def responses_post(payload):
    return http_post(payload, url=RESPONSES_URL)


def answer(lookups, text, *, history=None, post=responses_post, model=None):
    """Responses API tool loop over the read-only lookups."""
    model = model or os.environ.get('OPENAI_MODEL', '').strip() or DEFAULT_MODEL
    items = [{'role': m['role'], 'content': m['content']} for m in (history or [])
             if m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)]
    items.append({'role': 'user', 'content': text})
    resp = post(responses_payload(model, items, instructions=SYSTEM_PROMPT))
    for _ in range(MAX_ROUNDS):
        calls = function_calls(resp)
        if not calls:
            content = output_text(resp).strip()
            if not content:
                raise LlmError('empty reply')
            return scrub(content, 3500)
        outputs = []
        for call in calls:
            try:
                args = json.loads(call.get('arguments') or '{}')
            except ValueError:
                args = {}
            result = call_tool(lookups, call.get('name'), args if isinstance(args, dict) else {})
            outputs.append({'type': 'function_call_output', 'call_id': call.get('call_id'),
                            'output': json.dumps(result, default=str)[:30000]})
        resp = post(responses_payload(model, outputs, instructions=SYSTEM_PROMPT, previous=resp.get('id')))
    raise LlmError('too many tool rounds')


def self_check(post=None, model=None, log_=None, tools_post=None):
    """Startup check: one plain Chat Completions request (PARTYEVERYDAY's path)
    and one Responses request with the read-only tools (the answer path). Logs
    ok/FAIL with OpenAI's error, never the key. Returns {'plain','tools'}."""
    post = post or http_post
    tools_post = tools_post or (post if post is not http_post else responses_post)
    log_ = log_ or log
    model = model or os.environ.get('OPENAI_MODEL', '').strip() or DEFAULT_MODEL
    results = {}
    checks = (
        ('plain', post, {'model': model, 'max_completion_tokens': 400, 'messages': [
            {'role': 'system', 'content': 'Reply with the single word ok.'}, {'role': 'user', 'content': 'ping'}]}),
        ('tools', tools_post, responses_payload(model, [{'role': 'user', 'content': 'ping'}],
                                                instructions='Reply with the single word ok.', max_tokens=400)),
    )
    for name, fn, payload in checks:
        try:
            resp = fn(payload)
            results[name] = 'ok'
            log_.info('OpenAI self-check (%s): ok, model=%s', name, scrub(str(resp.get('model') or model), 80))
        except Exception as exc:  # noqa: BLE001
            results[name] = describe(exc)
            log_.error('OpenAI self-check (%s): FAIL %s', name, describe(exc))
    return results

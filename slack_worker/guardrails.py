"""Last-line checks on every Slack reply, same idea as PartyEveryDay.

A blocked or unusable reply is swapped for a short safe line and flagged
so the logs show it happened. Raw model errors are never sent.
"""
import logging
import re

from .views import ASSIGNED_SECRET_RE, EMAIL_RE, PHONE_RE, SECRET_RE, SECRET_URL_RE, _not_phone

log = logging.getLogger('amy.slack.guardrails')

SAFE_FALLBACK = "Let me double check and get right back to you."
CONFIRM_FALLBACK = "I don't want to call that confirmed until AMY shows it as done. Want me to look the guest up?"

# Confirmation claims. Negated wording ("not booked", "isn't confirmed") is allowed.
CONFIRMATION_RES = [
    re.compile(r"\b(?:you(?:'re|’re| are)|they(?:'re|’re| are)|she(?:'s|’s| is)|he(?:'s|’s| is)|we got them|got (?:them|her|him|you))\s+(?:all\s+)?(?:set|good to go|confirmed|booked|on (?:the )?(?:guest\s?)?list)\b", re.I),
    re.compile(r"\b(?:spot|guest\s?list|reservation|booking|list)\s+(?:is\s+|has been\s+)?(?:confirmed|booked)\b", re.I),
    re.compile(r"\b(?:is|are|was|were)\s+(?:now\s+)?(?:confirmed|booked|on the (?:guest\s?)?list)\b", re.I),
    re.compile(r"\bbooked (?:at|on|for|into|with)\b", re.I),
    re.compile(r"\b(?:all set|good to go)\b", re.I),
]
NEGATION_RE = re.compile(
    r"(?i)(?:\bnot\b|\bnever\b|n['’]t|\bis not\b|\bare not\b|\bhas not\b|\bdo not\b)",
)


def looks_like_phone(text):
    return any(not _not_phone(m.group(0)) for m in PHONE_RE.finditer(text or ''))


def looks_like_secret(text):
    text = text or ''
    return bool(SECRET_RE.search(text) or SECRET_URL_RE.search(text) or ASSIGNED_SECRET_RE.search(text)
                or EMAIL_RE.search(text))


def looks_like_confirmation(text):
    text = text or ''
    if NEGATION_RE.search(text):
        return False
    return any(r.search(text) for r in CONFIRMATION_RES)


def tool_shows_done(results):
    """True when any lookup in this turn reported a job in state done."""
    stack = [results]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            state = str(cur.get('state') or '').lower()
            if state == 'done':
                return True
            # queue_status counts
            counts = cur.get('counts')
            if isinstance(counts, dict) and int(counts.get('done') or 0) > 0 and len(cur) <= 3:
                # a bare status payload is not evidence a *guest* is booked
                pass
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return False


def apply_guardrails(text, *, amy_confirmed=False, source='llm'):
    """-> (safe_text, flags). flags is empty when the original text is kept."""
    flags = []
    raw = (text or '').strip()
    if not raw:
        flags.append('empty_reply')
        log.warning('guardrails: empty_reply source=%s', source)
        return SAFE_FALLBACK, flags
    if looks_like_phone(raw):
        flags.append('phone_number_blocked')
    if looks_like_secret(raw):
        flags.append('secret_blocked')
    if flags:
        log.warning('guardrails: %s source=%s', ','.join(flags), source)
        return SAFE_FALLBACK, flags
    if not amy_confirmed and looks_like_confirmation(raw):
        flags.append('unconfirmed_spot_blocked')
        log.warning('guardrails: unconfirmed_spot_blocked source=%s', source)
        return CONFIRM_FALLBACK, flags
    return raw, flags

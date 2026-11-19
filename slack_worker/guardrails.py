"""Last-line checks on every Slack reply, same idea as PartyEveryDay.

A blocked or unusable reply is swapped for a short safe line and flagged
so the logs show it happened. Raw model errors are never sent.
"""
import logging
import re

from .views import ASSIGNED_SECRET_RE, EMAIL_RE, PHONE_RE, SECRET_RE, SECRET_URL_RE, _not_phone

log = logging.getLogger('amy.slack.guardrails')

SAFE_FALLBACK = "Let me double check and get right back to you."
CONFIRM_FALLBACK = "I don't want to call that confirmed until I check what we have. Send me the client's full name, the venue, and the date."

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


# Voice-guide banned phrases (rewrite out of the reply). "my guy" is in the
# guide with a soft exception, so it is not blocked in code.
BANNED_PHRASE_RES = [
    re.compile(r'\bfam\b', re.I),
    re.compile(r'\bvibes\b', re.I),
    re.compile(r'\brockstars?\b', re.I),
    re.compile(r'\bsynergy\b', re.I),
    re.compile(r"\blet'?s crush it\b!?", re.I),
    re.compile(r'\bamazing opportunity\b!?', re.I),
    re.compile(r'\bdear valued\b[\w\s]*', re.I),
    re.compile(r'\bwe sincerely appreciate your continued partnership\b', re.I),
]
# Unicode emoji + simple Slack :shortcodes:
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+"
)
SLACK_EMOJI_RE = re.compile(r':[a-z0-9_+-]{2,}:', re.I)

# Incoming messages that should be flagged to Tom (not "I want credit").
ESCALATION_RE = re.compile(
    r"(?i)\b("
    r"dispute|disputing|chargeback|refund|"
    r"payment(?:s)? issue|haven't been paid|have not been paid|not been paid|"
    r"unpaid|payout|paycheck|invoice|"
    r"special request|need(?:s)? (?:your |tom'?s |management )?approval|"
    r"manager(?:ial)? approval|talk to (?:a )?manager"
    r")\b"
)

ESCALATION_REPLY = "I'll get this over to Tom. Let me know if anything else comes up."


def needs_escalation(text):
    """True when the incoming message should be flagged for Tom."""
    return bool(ESCALATION_RE.search(text or ''))


def tidy_voice(text):
    """Strip banned phrases, emojis, and extra exclamation marks. -> (text, flags)."""
    flags = []
    out = text or ''
    for pat in BANNED_PHRASE_RES:
        if pat.search(out):
            flags.append('banned_phrase_rewritten')
            out = pat.sub('', out)
    if EMOJI_RE.search(out) or SLACK_EMOJI_RE.search(out):
        flags.append('emoji_stripped')
        out = EMOJI_RE.sub('', out)
        out = SLACK_EMOJI_RE.sub('', out)
    if re.search(r'!{2,}', out):
        flags.append('exclamation_trimmed')
        out = re.sub(r'!{2,}', '!', out)
    bangs = list(re.finditer(r'!', out))
    if len(bangs) > 1:
        flags.append('exclamation_trimmed')
        first = bangs[0].start()
        out = out[:first + 1] + out[first + 1:].replace('!', '.')
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" *\n *", "\n", out)
    out = re.sub(r" +([,.;:!?])", r"\1", out)
    out = re.sub(r"([,.;:]){2,}", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;")
    return out.strip(), flags


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
    cleaned, voice_flags = tidy_voice(raw)
    flags.extend(voice_flags)
    if voice_flags:
        log.info('guardrails: %s source=%s', ','.join(voice_flags), source)
    if not cleaned:
        flags.append('empty_after_rewrite')
        log.warning('guardrails: empty_after_rewrite source=%s', source)
        return SAFE_FALLBACK, flags
    return cleaned, flags

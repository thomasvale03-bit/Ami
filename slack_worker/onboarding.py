"""Jacob's new-hire onboarding coach: intro, reason capture, and homework.

State lives on the worker volume (next to SLACK_WORKER_STATE). Intros are
deduped so a person is never welcomed twice. Homework is taken only from
typical promoter activities in the knowledge base; nothing is invented.
"""
import json
import os
import re
import threading

TOM_USER_ID = 'U0CK42KJT7E'
NEWHIRE_CHANNEL_ID = 'C0CJZ69MPB7'
NEWHIRE_CHANNEL_NAMES = {'newhire', 'new-hire', 'new_hire', 'new-promoters', 'newpromoters'}

# Staged homework, 1–2 items assigned at a time. Each item is grounded in
# the KB (first-week plan, signup process, how to promote).
HOMEWORK = [
    {
        'id': 'learn-basics',
        'title': 'Learn how Playmaker works',
        'instruction': (
            "Review playmakerentertainment.com and make sure you understand guest "
            "lists vs tickets vs tables, and how promoter attribution works: you "
            "put your own first and last name in the Promoter fields on the same "
            "form as the client. Being counted for the guest list does not mean "
            "guaranteed check-in credit. When you're clear on that, tell me."
        ),
        'kb': 'First week Day 1; Q13 signup; Q20 guest lists/tickets/tables; credit policy',
    },
    {
        'id': 'venues-nights',
        'title': 'Study the venue lineup and weekly nights',
        'instruction': (
            "Review the current approved venues and the latest event calendar "
            "(check Slack / what management has shared). Don't assume an old "
            "weekly schedule is still current — availability has to be verified. "
            "Come back and tell me which venues you're starting with."
        ),
        'kb': 'First week Day 2; Q18–Q19 venues and nights',
    },
    {
        'id': 'practice-signup',
        'title': 'Practice understanding the signup form',
        'instruction': (
            "Go to playmakerentertainment.com and walk through the guest-list "
            "form so you know the fields: client info, venue, date, and your "
            "Promoter first and last name. Don't submit fake customer information. "
            "Tell me when you've walked through it."
        ),
        'kb': 'First week Day 2; Q13 and Q15 signup fields',
    },
    {
        'id': 'contact-list',
        'title': 'Build a list of contacts',
        'instruction': (
            "Put together a short list of people you'd actually reach out to — "
            "personal network, Instagram, friends heading to Vegas, or other "
            "people you already know. The goal is a consistent customer base, "
            "not spamming strangers. Send me a count or a general sense of the "
            "list (don't share anyone's private contact details)."
        ),
        'kb': 'Q23 where to find clients; First week Day 3',
    },
    {
        'id': 'first-outreach',
        'title': 'Practice a natural outreach',
        'instruction': (
            "Draft a natural first message — conversational, not a hard sell. "
            "If management has shared approved artwork or current calendar items "
            "in Slack, only use those. No outdated flyers, and don't promise "
            "free entry or discounts. Report back with what you drafted or posted."
        ),
        'kb': 'Q24–Q27 how to promote; First week Day 3–5',
    },
    {
        'id': 'follow-up',
        'title': 'Follow up and focus on actual attendance',
        'instruction': (
            "If you've signed anyone up, follow up before their night out and "
            "make sure they know the venue, the date, and to bring ID. A signup "
            "is only the start — attendance is what matters. Tell me how "
            "follow-ups are going. If you haven't signed anyone up yet, tell me "
            "what's getting in the way."
        ),
        'kb': 'Q2 promoter role; First week Day 4–7; day-one notes',
    },
]
HOMEWORK_BY_ID = {item['id']: item for item in HOMEWORK}
HOMEWORK_BATCH = 2

INTRO_TEXT = (
    "Hey, I'm Jacob, Playmaker's new-promoter coach. I help new hires get up "
    "to speed: how the program works, signing clients up on "
    "playmakerentertainment.com, venues and nights, how to promote, and any "
    "questions that come up. I'll also give you some homework as we go, the "
    "same things we typically do when someone starts. If you've got questions, "
    "ask me here. First I want to get to know you a bit — why do you want to "
    "be a promoter?"
)

QUESTION_RE = re.compile(
    r"(?i)^\s*(how|what|when|where|who|which|can|could|do|does|did|is|are|am|"
    r"should|will|would|may|any chance)\b"
)
REASON_HINT_RE = re.compile(
    r"(?i)\b(i want|i'd like|i would love|i love|i like|because|my goal|"
    r"i'm hoping|im hoping|i have a|my friends|friend group|make money|"
    r"side hustle|nightlife|clubs?|vegas|promote|following|network|"
    r"meet people|extra (?:cash|income|money))\b"
)
GREETING_RE = re.compile(
    r"(?i)^(hey|hi|hello|what's up|whats up|yo|thanks|thank you|ok|okay|sup)"
    r"[\s!.]*$"
)
PROGRESS_RE = re.compile(
    r"(?i)\b(done|finished|completed|i did(?: that)?|i walked|i reviewed|"
    r"i looked(?: through| over)?|i built|i posted|i sent|i made a list|"
    r"here's my list|here is my list|checked off|got through|"
    r"homework is done|finished the homework|I went through)\b"
)


def _owner_id():
    return (os.environ.get('SLACK_OWNER_USER_ID') or TOM_USER_ID).strip()


def newhire_channel_id():
    raw = (os.environ.get('SLACK_NEWHIRE_CHANNEL') or NEWHIRE_CHANNEL_ID).strip()
    return raw.lstrip('#')


def is_newhire_channel(channel, name=None):
    cid = (channel or '').strip()
    if cid == newhire_channel_id():
        return True
    label = (name or '').strip().lstrip('#').lower()
    return label in NEWHIRE_CHANNEL_NAMES


def first_name(user=None):
    user = user if isinstance(user, dict) else {}
    profile = user.get('profile') if isinstance(user.get('profile'), dict) else {}
    first = (profile.get('first_name') or '').strip()
    if not first:
        real = (user.get('real_name') or user.get('display_name')
                or profile.get('display_name') or '').strip()
        first = real.split()[0] if real else ''
    if first and first.lower() not in ('slackbot', 'jacob'):
        return first
    return ''


def intro_text(user=None):
    """Short intro in Tom's voice: who Jacob is, what he can help with, and why."""
    name = first_name(user)
    if name:
        return "Hey " + name + ", " + INTRO_TEXT[len("Hey, "):]
    return INTRO_TEXT


def should_intro(user):
    """Skip bots, empty ids, and Tom. Existing members are not backfilled."""
    user = user if isinstance(user, dict) else {'id': user}
    uid = str(user.get('id') or '')
    if not uid or user.get('is_bot') or user.get('is_app_user'):
        return False
    if uid == _owner_id() or uid == TOM_USER_ID:
        return False
    return True


def looks_like_question(text):
    text = (text or '').strip()
    if not text:
        return False
    if text.endswith('?'):
        return True
    return bool(QUESTION_RE.search(text))


def looks_like_reason(text):
    """True when a new hire is explaining why they want to be a promoter."""
    text = (text or '').strip()
    if len(text) < 8 or GREETING_RE.fullmatch(text):
        return False
    from .guardrails import needs_escalation
    if needs_escalation(text, persona='jacob'):
        return False
    if looks_like_question(text) and not REASON_HINT_RE.search(text):
        return False
    if REASON_HINT_RE.search(text):
        return True
    return (not looks_like_question(text)) and len(text.split()) >= 5


def looks_like_progress(text):
    return bool(PROGRESS_RE.search(text or ''))


def tailor_ack(reason):
    """Personal acknowledgment. Never promises pay, entry, or credit."""
    low = (reason or '').lower()
    if any(w in low for w in ('friend', 'people i know', 'network', 'circle',
                              'everyone i know', 'my group', 'friend group')):
        return ("That's a solid place to start. Your own circle is usually the "
                "best first list — we'll get you ready to talk to them the right way.")
    if any(w in low for w in ('instagram', 'tiktok', 'facebook', 'following',
                              'social', 'content', 'followers')):
        return ("Good. We'll keep the outreach natural and only use approved "
                "info — not spam, and no promises we can't keep.")
    if any(w in low for w in ('nightlife', 'club', 'vegas', 'scene', 'going out')):
        return ("Makes sense — if you already know the scene, the next step is "
                "learning how we actually sign people up and which venues we're "
                "pushing right now.")
    if any(w in low for w in ('money', 'income', 'paid', 'cash', 'hustle',
                              'commission', 'earn')):
        return ("Got it. I can't speak to rates or payouts — Tom still has to "
                "confirm that. What I can do is get you doing the work that "
                "actually counts: real signups and real attendance.")
    return "Appreciate you telling me that. We'll build from there."


def homework_blurb(items, *, first=False):
    if not items:
        return ("You've worked through the starting homework. Keep asking "
                "questions, keep signing people up the right way, and tell me "
                "how it's going. Let's get it.")
    lines = []
    if first:
        lines.append("Here's your first homework — two things, then report back:")
    else:
        lines.append("Next homework — take these and tell me when you're through them:")
    for item in items:
        lines.append(f"• *{item['title']}*. {item['instruction']}")
    return '\n'.join(lines)


def next_items(record, n=HOMEWORK_BATCH):
    done = set(record.get('homework_done') or [])
    assigned = set(record.get('homework_assigned') or [])
    pending = [item for item in HOMEWORK if item['id'] not in done and item['id'] not in assigned]
    return pending[:n]


def onboarding_note(record):
    """Context block for the model, not from the teammate."""
    if not record:
        return ''
    lines = ['[Onboarding - not from the teammate]']
    lines.append(f"Stage: {record.get('stage') or 'new'}")
    if record.get('reason'):
        lines.append(f"Their reason / goals: {record['reason']}")
    assigned = [HOMEWORK_BY_ID[i]['title'] for i in (record.get('homework_assigned') or []) if i in HOMEWORK_BY_ID]
    done = [HOMEWORK_BY_ID[i]['title'] for i in (record.get('homework_done') or []) if i in HOMEWORK_BY_ID]
    if assigned:
        lines.append('Assigned homework: ' + '; '.join(assigned))
    if done:
        lines.append('Completed homework: ' + '; '.join(done))
    lines.append(
        'If you assign more homework, only use items from the official list. '
        'Do not invent policies, rates, or venue availability. Ask them to report back.'
    )
    return '\n'.join(lines)


def _default_path():
    state = os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json')
    return os.path.join(os.path.dirname(state) or '.', 'jacob-onboarding.json')


def _welcomed_legacy_path(onboarding_path):
    return os.path.join(os.path.dirname(onboarding_path) or '.', 'jacob-welcomed.json')


class OnboardingStore:
    """Volume-backed per-user onboarding state (stage, reason, homework)."""

    def __init__(self, path=None):
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self._users = {}
        self._load()

    def _load(self):
        users = {}
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            raw = data.get('users', data) if isinstance(data, dict) else {}
            if isinstance(raw, dict):
                users = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            users = {}
        # People who already got the first welcome are not re-introduced.
        try:
            with open(_welcomed_legacy_path(self.path), encoding='utf-8') as fh:
                legacy = json.load(fh)
            ids = legacy if isinstance(legacy, list) else []
            for uid in ids:
                uid = str(uid or '')
                if uid and uid not in users:
                    users[uid] = {
                        'stage': 'awaiting_reason',
                        'intro_sent': True,
                        'source': 'migrated',
                        'reason': '',
                        'homework_assigned': [],
                        'homework_done': [],
                    }
        except (OSError, ValueError):
            pass
        self._users = users

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'users': self._users}, fh)
        os.replace(tmp, self.path)

    def _blank(self):
        return {
            'stage': 'new',
            'intro_sent': False,
            'reason': '',
            'homework_assigned': [],
            'homework_done': [],
        }

    def get(self, user_id):
        user_id = str(user_id or '')
        with self._lock:
            rec = self._users.get(user_id)
            return dict(rec) if rec else self._blank()

    def intro_sent(self, user_id):
        return bool(self.get(user_id).get('intro_sent'))

    def already(self, user_id):
        """Alias used by older welcome-dedupe tests and call sites."""
        return self.intro_sent(user_id)

    def _update(self, user_id, **fields):
        user_id = str(user_id or '')
        if not user_id:
            return self._blank()
        with self._lock:
            rec = dict(self._users.get(user_id) or self._blank())
            rec.update(fields)
            self._users[user_id] = rec
            try:
                self._save()
            except OSError:
                pass
            return dict(rec)

    def mark_intro_sent(self, user_id, source='dm'):
        rec = self.get(user_id)
        if rec.get('intro_sent'):
            return rec
        stage = rec.get('stage') if rec.get('stage') not in (None, 'new') else 'awaiting_reason'
        return self._update(user_id, intro_sent=True, stage=stage, source=source)

    def mark(self, user_id):
        """Back-compat with WelcomedSet.mark."""
        return self.mark_intro_sent(user_id)

    def set_reason(self, user_id, reason):
        reason = (reason or '').strip()
        rec = self.get(user_id)
        assigned = list(rec.get('homework_assigned') or [])
        items = next_items(rec, HOMEWORK_BATCH)
        for item in items:
            if item['id'] not in assigned:
                assigned.append(item['id'])
        return self._update(
            user_id,
            intro_sent=True,
            stage='homework' if assigned else 'complete',
            reason=reason,
            homework_assigned=assigned,
        )

    def complete_assigned(self, user_id):
        rec = self.get(user_id)
        done = list(rec.get('homework_done') or [])
        for hid in rec.get('homework_assigned') or []:
            if hid not in done:
                done.append(hid)
        rec_done = dict(rec)
        rec_done['homework_assigned'] = []
        rec_done['homework_done'] = done
        nxt = next_items(rec_done, HOMEWORK_BATCH)
        assigned = [item['id'] for item in nxt]
        stage = 'homework' if assigned else 'complete'
        return self._update(
            user_id,
            intro_sent=True,
            stage=stage,
            homework_assigned=assigned,
            homework_done=done,
        )


def plan_turn(store, user_id, text, *, user=None, history=None, is_im=False):
    """Decide the coach move for one incoming message. Deterministic.

    Returns a dict: mode, text, also_answer (whether to also run the KB model).
    Pay / escalation questions should be filtered by the caller first.
    """
    rec = store.get(user_id)
    text = text or ''
    history = history or []

    if not rec.get('intro_sent'):
        store.mark_intro_sent(user_id, source='dm' if is_im else 'message')
        intro = intro_text(user)
        # A first DM that's already a real question: intro, then also answer.
        if looks_like_question(text) or looks_like_reason(text):
            if looks_like_reason(text):
                rec = store.set_reason(user_id, text)
                items = [HOMEWORK_BY_ID[i] for i in rec.get('homework_assigned') or [] if i in HOMEWORK_BY_ID]
                body = intro + '\n\n' + tailor_ack(text) + '\n\n' + homework_blurb(items, first=True)
                return {'mode': 'intro', 'text': body, 'also_answer': False}
            return {'mode': 'intro', 'text': intro, 'also_answer': True}
        return {'mode': 'intro', 'text': intro, 'also_answer': False}

    if not rec.get('reason') and looks_like_reason(text):
        rec = store.set_reason(user_id, text)
        items = [HOMEWORK_BY_ID[i] for i in rec.get('homework_assigned') or [] if i in HOMEWORK_BY_ID]
        body = tailor_ack(text) + '\n\n' + homework_blurb(items, first=True)
        return {'mode': 'ack_reason', 'text': body, 'also_answer': False}

    if rec.get('stage') == 'homework' and looks_like_progress(text):
        rec = store.complete_assigned(user_id)
        items = [HOMEWORK_BY_ID[i] for i in rec.get('homework_assigned') or [] if i in HOMEWORK_BY_ID]
        first = False
        body = homework_blurb(items, first=first)
        if rec.get('stage') == 'complete':
            body = ("Good work. I appreciate you putting in the effort. " + body)
        else:
            body = "Good work.\n\n" + body
        return {'mode': 'progress', 'text': body, 'also_answer': False}

    suffix = ''
    if rec.get('intro_sent') and not rec.get('reason') and rec.get('stage') == 'awaiting_reason':
        suffix = " And when you have a second, tell me why you want to be a promoter."
    elif rec.get('stage') == 'homework' and rec.get('homework_assigned'):
        titles = [HOMEWORK_BY_ID[i]['title'] for i in rec['homework_assigned'] if i in HOMEWORK_BY_ID]
        if titles and not looks_like_question(text):
            suffix = ' How is that homework coming — ' + ' and '.join(titles) + '?'
        elif titles:
            suffix = " When you get a chance, report back on: " + ' and '.join(titles) + "."
    return {'mode': 'answer', 'text': suffix, 'also_answer': True}


def simulate_conversation(path=None):
    """A 5-turn intro → reason → homework → progress → pay-escalation script.

    Used by tests and the log-only startup check. Never posts to Slack.
    """
    from .guardrails import escalation_reply, needs_escalation

    store = OnboardingStore(path=path)
    uid = 'U_SIM'
    user = {'id': uid, 'real_name': 'Alex'}
    turns = []

    intro = intro_text(user)
    store.mark_intro_sent(uid, source='sim')
    turns.append(('jacob', intro))

    reason = ("I want to be a promoter because I have a big group of friends "
              "who go to Vegas and I can get them out.")
    turns.append(('alex', reason))
    planned = plan_turn(store, uid, reason, user=user, is_im=True)
    turns.append(('jacob', planned['text']))

    progress = "I reviewed the website and walked through the venues. I'm done with that homework."
    turns.append(('alex', progress))
    planned = plan_turn(store, uid, progress, user=user, is_im=True)
    turns.append(('jacob', planned['text']))

    pay = 'how often do we get paid?'
    turns.append(('alex', pay))
    assert needs_escalation(pay, persona='jacob')
    turns.append(('jacob', escalation_reply(pay, persona='jacob')))
    return turns, store.get(uid)

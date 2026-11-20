"""Shared Slack worker (Socket Mode, no public URL).

Persona is selected with SLACK_PERSONA (default amy):

* amy (AMY Worker / AMYI): posts booking updates to #amy-bookings (or Tom's
  DM), answers DMs and @mentions with read-only AMY queue tools.
* jacob (Jacob): new-hire promoter onboarding coach. No AMY queue tools or
  booking watcher. Intro DM on team_join, #newhire joins, and a first DM;
  captures why they want to be a promoter and assigns staged homework.
  Escalations are DMed to Tom.

Env: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_PERSONA, OPENAI_API_KEY,
OPENAI_MODEL, SLACK_OWNER_USER_ID (Tom), SLACK_ALLOWED_USERS,
SLACK_POLL_SECONDS, SLACK_WORKER_STATE. AMY_BASE_URL and
AMY_QUEUE_API_TOKEN are required for the amy persona only. Secrets are
never logged.
"""
import logging
import os
import threading
import time

from . import commands, flyers, llm, persona, user_identity
from .amy_api import AmyApi
from .catchup import Catchup, RepliedSet, should_answer_channel
from .daily_report import DailyReporter
from .guardrails import apply_guardrails, needs_escalation, escalation_reply, ESCALATION_REPLY
from .history import load_conversation
from .lookups import Lookups
from .views import scrub
from .watcher import Watcher
from .welcome import WelcomedSet
from . import onboarding as onboarding_mod
from .onboarding import OnboardingStore

log = logging.getLogger('amy.slack')
TOM_USER_ID = 'U0CK42KJT7E'


class CachedApi:
    """Read-only API with a short cache so one question = one fetch."""

    def __init__(self, api, ttl=20, clock=time.time):
        self.api, self.ttl, self.clock = api, ttl, clock
        self._cache = {}
        self._lock = threading.Lock()

    def _get(self, name, fn):
        with self._lock:
            hit = self._cache.get(name)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._cache[name] = (self.clock(), value)
        return value

    def status(self):
        return self._get('status', self.api.status)

    def jobs(self, limit=1000):
        return self._get('jobs', lambda: self.api.jobs(limit))


class Router:
    """Where updates go: the named channel if it exists (chat:write.public
    posts to public channels without joining), else create it if the app may,
    else Tom's DM. Re-checked hourly so a channel created later is picked up."""

    def __init__(self, client, channel=None, owner=None, clock=time.time):
        self.client = client
        self.name = (channel or os.environ.get('SLACK_UPDATES_CHANNEL') or 'amy-bookings').lstrip('#')
        self.owner = owner or os.environ.get('SLACK_OWNER_USER_ID') or TOM_USER_ID
        self.clock = clock
        self.target, self.kind, self.checked = None, None, 0.0
        self.note = ''

    def _find(self):
        if self.name.startswith(('C', 'G')) and self.name.isupper():
            return self.name
        cursor = None
        while True:
            resp = self.client.conversations_list(types='public_channel', exclude_archived=True,
                                                  limit=1000, cursor=cursor)
            for ch in resp.get('channels', []):
                if ch.get('name') == self.name:
                    return ch['id']
            cursor = (resp.get('response_metadata') or {}).get('next_cursor')
            if not cursor:
                return None

    def resolve(self, force=False):
        if self.target and not force and self.clock() - self.checked < 3600:
            return self.target
        self.checked = self.clock()
        previous = self.kind
        try:
            cid = self._find()
            if not cid:
                try:
                    cid = self.client.conversations_create(name=self.name)['channel']['id']
                    log.info('created #%s', self.name)
                except Exception as exc:  # noqa: BLE001  (missing_scope without channels:manage)
                    log.info('cannot create #%s: %s', self.name, getattr(getattr(exc, 'response', None), 'data', {}).get('error', type(exc).__name__))
            if cid:
                self.target, self.kind = cid, 'channel'
                self.note = f'#{self.name}'
            else:
                dm = self.client.conversations_open(users=self.owner)['channel']['id']
                self.target, self.kind = dm, 'dm'
                self.note = f"Tom's DM (#{self.name} doesn't exist and I can't create it)"
        except Exception as exc:  # noqa: BLE001
            log.error('could not resolve update channel: %s', type(exc).__name__)
        if self.kind == 'channel' and self.target and not self._noticed('hello-' + self.target):
            try:
                self.client.chat_postMessage(channel=self.target, text=(
                    "Hey team :wave: AMY Worker here. I'll post booking updates in this channel. "
                    "DM me or @mention me with `help` to see what I can look up."))
                self._mark_noticed('hello-' + self.target)
            except Exception:  # noqa: BLE001
                pass
        if self.kind == 'dm' and previous != 'dm' and self.target and not self._noticed():
            try:
                self.client.chat_postMessage(channel=self.target, text=(
                    f"Heads up: I'll post booking updates here because #{self.name} doesn't exist yet and I "
                    f"can't create channels. Create #{self.name} and I'll switch to it within the hour."))
                self._mark_noticed()
            except Exception:  # noqa: BLE001
                pass
        return self.target

    def _notice_path(self, name='dm-fallback-noticed'):
        state = os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json')
        return os.path.join(os.path.dirname(state), name)

    def _noticed(self, name='dm-fallback-noticed'):
        return os.path.exists(self._notice_path(name))

    def _mark_noticed(self, name='dm-fallback-noticed'):
        try:
            os.makedirs(os.path.dirname(self._notice_path(name)), exist_ok=True)
            open(self._notice_path(name), 'w').close()
        except OSError:
            pass

    def post(self, text):
        target = self.resolve()
        if not target:
            raise RuntimeError('no update channel')
        try:
            self.client.chat_postMessage(channel=target, text=text, unfurl_links=False, unfurl_media=False)
        except Exception:
            target = self.resolve(force=True)  # channel archived/renamed: re-resolve once
            self.client.chat_postMessage(channel=target, text=text, unfurl_links=False, unfurl_media=False)


def allowed(user):
    raw = os.environ.get('SLACK_ALLOWED_USERS', '').strip()
    return not raw or user in {u.strip() for u in raw.split(',') if u.strip()}


def slack_error(exc):
    data = getattr(getattr(exc, 'response', None), 'data', None) or {}
    if isinstance(data, dict):
        return data.get('error') or type(exc).__name__, data.get('needed')
    return type(exc).__name__, None


class FlagSink:
    """Posts escalation notes once a poster is attached.

    AMY attaches Router.post (#amy-bookings). Jacob attaches owner_poster
    (a DM to Tom).
    """

    def __init__(self):
        self.post = None

    def send(self, text):
        if not self.post:
            log.warning('escalation flagged but no poster is attached')
            return
        self.post(text)


def owner_poster(client, owner=None):
    """Return a post(text) function that DMs Tom. Used by Jacob only."""
    owner = owner or os.environ.get('SLACK_OWNER_USER_ID') or TOM_USER_ID

    def post(text):
        dm = client.conversations_open(users=owner)['channel']['id']
        client.chat_postMessage(channel=dm, text=text, unfurl_links=False, unfurl_media=False)

    return post


def escalation_note(event, reply, channel_label=None):
    user = event.get('user') or ''
    where = channel_label or event.get('channel') or 'Slack'
    snippet = scrub(event.get('text') or '', 200)
    answered = scrub(reply or '', 200)
    return (
        "Escalation \u2014 I'll get this to Tom.\n"
        f"From <@{user}> in {where}: {snippet}\n"
        f"Reply: {answered}"
    )


LLM_FAIL_FALLBACK = "Couldn't reach ChatGPT right now. Try me again in a minute?"
VAGUE_FALLBACK = "Not sure I follow — want the queue status, a guest lookup, or a night's bookings?"
JACOB_FAIL_FALLBACK = "Couldn't get back to you just now. Try me again in a minute?"
JACOB_VAGUE_FALLBACK = (
    "Not sure I follow — what do you want to know about getting started, "
    "signing clients up, or how things work here?"
)


def _call_answer(fn, lookups, text, history=None, context=None, meta=None):
    try:
        return fn(lookups, text, history=history, context=context, meta=meta)
    except TypeError:
        return fn(lookups, text)


def jacob_reply_text(lookups, text, *, use_llm=None, llm_answer=None, history=None,
                     context=None, user_id=None, onboarding=None, user=None, is_im=False):
    """Coach turn for Jacob: intro, reason, homework, or a KB answer."""
    use_llm = llm.configured() if use_llm is None else use_llm
    lead = ''
    suffix = ''
    if onboarding and user_id and not needs_escalation(text, persona='jacob'):
        planned = onboarding_mod.plan_turn(
            onboarding, user_id, text, user=user, history=history, is_im=is_im)
        if planned['mode'] in ('intro', 'ack_reason', 'progress') and not planned.get('also_answer'):
            safe, _flags = apply_guardrails(
                planned['text'], amy_confirmed=True, source='onboarding', persona='jacob')
            return safe
        if planned['mode'] == 'intro' and planned.get('also_answer'):
            lead = planned['text']
        elif planned['mode'] == 'answer':
            suffix = planned['text'] or ''
    extra = context
    if onboarding and user_id:
        note = onboarding_mod.onboarding_note(onboarding.get(user_id))
        if note:
            extra = (context + '\n' + note) if context else note
    try:
        if use_llm:
            try:
                meta = {}
                raw = _call_answer(llm_answer or llm.answer, lookups, commands.strip_mentions(text),
                                   history=history, context=extra, meta=meta)
                if lead:
                    raw = lead.rstrip() + '\n\n' + (raw or '')
                if suffix and suffix not in (raw or ''):
                    raw = (raw or '').rstrip() + suffix
                safe, _flags = apply_guardrails(
                    raw, amy_confirmed=False, source='llm', persona='jacob')
                return safe
            except Exception as exc:  # noqa: BLE001
                log.error('OpenAI answer failed: %s', llm.describe(exc))
                if lead:
                    return apply_guardrails(
                        lead, amy_confirmed=True, source='onboarding', persona='jacob')[0]
                return JACOB_FAIL_FALLBACK
        if lead:
            return apply_guardrails(
                lead, amy_confirmed=True, source='onboarding', persona='jacob')[0]
        vague = JACOB_VAGUE_FALLBACK + (suffix or '')
        return apply_guardrails(vague, amy_confirmed=True, source='onboarding', persona='jacob')[0]
    except Exception as exc:  # noqa: BLE001
        log.warning('jacob reply failed: %s', type(exc).__name__)
        return JACOB_FAIL_FALLBACK


def reply_text(lookups, text, *, use_llm=None, llm_answer=None, history=None, context=None,
               user_id=None, onboarding=None, user=None, is_im=False):
    """Answer one message. Exact keyword commands are answered directly; free
    text goes to the OpenAI model when configured, else a short clarifying
    question. Never dump the command list on a vague message. Guardrails run
    on every outgoing line (no phones, secrets, or unconfirmed 'booked').
    Jacob skips AMY keyword commands and has no queue lookups."""
    if persona.is_jacob():
        return jacob_reply_text(
            lookups, text, use_llm=use_llm, llm_answer=llm_answer,
            history=history, context=context, user_id=user_id,
            onboarding=onboarding, user=user, is_im=is_im)
    cmd, arg = commands.parse(text)
    use_llm = llm.configured() if use_llm is None else use_llm
    try:
        if cmd and (cmd != 'help' or not use_llm or not commands.strip_mentions(text)):
            out = commands.run(lookups, cmd, arg)
            safe, _flags = apply_guardrails(out, amy_confirmed=True, source='command')
            return safe
        if use_llm:
            try:
                meta = {}
                raw = _call_answer(llm_answer or llm.answer, lookups, commands.strip_mentions(text),
                                   history=history, context=context, meta=meta)
                safe, _flags = apply_guardrails(
                    raw, amy_confirmed=bool(meta.get('amy_confirmed')), source='llm')
                return safe
            except Exception as exc:  # noqa: BLE001
                log.error('OpenAI answer failed: %s', llm.describe(exc))
                if cmd:
                    out = commands.run(lookups, cmd, arg)
                    safe, _flags = apply_guardrails(out, amy_confirmed=True, source='command')
                    return safe
                return LLM_FAIL_FALLBACK
        return VAGUE_FALLBACK
    except Exception as exc:  # noqa: BLE001
        log.warning('lookup failed: %s', scrub(str(exc), 120))
        return "Couldn't reach AMY right now, try again in a minute."


def _jacob_startup_checks(lookups, answer=None):
    """Logged-only intro draft, coach simulation, and model tests. Never posts to Slack."""
    ok = True
    try:
        intro = onboarding_mod.intro_text({'id': 'U_SIM', 'real_name': 'Alex'})
        log.info('Jacob intro draft: %s', scrub(intro, 800))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            turns, _rec = onboarding_mod.simulate_conversation(
                path=os.path.join(tmp, 'jacob-coach-sim.json'))
        for i, (who, msg) in enumerate(turns, 1):
            log.info('Jacob coach-sim turn %d %s: %s', i, who, scrub(msg, 500))
    except Exception as exc:  # noqa: BLE001
        log.error('Jacob coach-sim: FAIL %s', type(exc).__name__)
        ok = False
    tests = (
        ('pay schedule', 'how often do we get paid?'),
        ('signup', 'how do I sign up a client?'),
    )
    for label, question in tests:
        try:
            raw = (answer or llm.answer)(lookups, question)
            draft, _flags = apply_guardrails(
                raw, amy_confirmed=False, source='voice-check', persona='jacob')
            outgoing = draft
            if needs_escalation(question, persona='jacob'):
                outgoing = apply_guardrails(
                    escalation_reply(question, persona='jacob'),
                    amy_confirmed=True, source='escalation', persona='jacob')[0]
                log.info('OpenAI voice-check (%s, model draft): %s', label, scrub(draft, 500))
            log.info('OpenAI voice-check (%s): %s', label, scrub(outgoing, 500))
        except Exception as exc:  # noqa: BLE001
            log.error('OpenAI voice-check (%s): FAIL %s', label, llm.describe(exc))
            ok = False
    return ok


def startup_check(lookups, answer=None, check=None):
    """OpenAI self-check plus persona-specific model tests. Logs ok/FAIL
    only; never the key, never posts to Slack."""
    (check or llm.self_check)()
    if persona.is_jacob():
        return _jacob_startup_checks(lookups, answer)
    try:
        text = (answer or llm.answer)(lookups, 'How many jobs are in done right now? Use the queue_status tool.')
        log.info('OpenAI self-check (round trip): ok, %d chars, has number=%s', len(text),
                 any(ch.isdigit() for ch in text))
        try:
            voice = (answer or llm.answer)(lookups, "I'm a promoter, i want to get credit")
            safe, _flags = apply_guardrails(voice, amy_confirmed=False, source='voice-check')
            log.info('OpenAI voice-check (promoter credit): %s', scrub(safe, 500))
        except Exception as exc:  # noqa: BLE001
            log.error('OpenAI voice-check (promoter credit): FAIL %s', llm.describe(exc))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error('OpenAI self-check (round trip): FAIL %s', llm.describe(exc))
        return False


def team_join_user(event):
    user = (event or {}).get('user')
    if isinstance(user, dict):
        return user
    if isinstance(user, str):
        return {'id': user}
    return {}


def should_welcome(user):
    """Skip bots, the app user, empty ids, and Tom."""
    return onboarding_mod.should_intro(user)


def build_app():
    from slack_bolt import App

    app = App(token=os.environ['SLACK_BOT_TOKEN'])
    if persona.is_jacob():
        api = lookups = None
    else:
        api = CachedApi(AmyApi())
        lookups = Lookups(api)

    replied = RepliedSet()
    flagsink = FlagSink()
    onboarding = OnboardingStore() if persona.is_jacob() else None
    posted_flyers = flyers.PostedSet() if persona.is_amy() else None
    welcomed = onboarding

    def handle(event, say, thread=None, *, client=None, self_user=None, identity_note='',
               download_token=None, poster=None):
        """Answer one message. client/self_user/identity_note select the identity
        (AMY Worker bot by default, AMYI's user account when given)."""
        client = client or app.client
        poster = poster or client
        user = event.get('user') or ''
        channel = event.get('channel') or ''
        ts = event.get('ts') or ''
        log.info('incoming type=%s channel=%s ts=%s user=%s thread=%s',
                 event.get('type') or event.get('channel_type') or '?',
                 channel, ts, user, thread or '')
        if ts and channel and not replied.claim(channel, ts):
            log.info('skip already-claimed channel=%s ts=%s', channel, ts)
            return
        try:
            if not allowed(user):
                say(text="Sorry, I only answer the Playmaker team.", thread_ts=thread)
                if channel and ts:
                    replied.mark(channel, ts)
                return
            is_im = event.get('channel_type') == 'im' or (channel or '').startswith('D')
            if persona.is_amy() and flyers.is_flyer_event(event):
                done, skipped, failed = flyers.share_flyers(
                    event, download_token=download_token or os.environ.get('SLACK_BOT_TOKEN', ''),
                    poster=poster, posted=posted_flyers)
                say(text=flyers.confirmation(done, skipped, failed), thread_ts=thread)
                if channel and ts:
                    replied.mark(channel, ts)
                return
            if self_user:
                history, context = load_conversation(client, event, thread, bot_user=self_user)
            else:
                history, context = load_conversation(client, event, thread)
            incoming = event.get('text') or ''
            if persona.is_amy():
                for note in (identity_note, user_identity.identity_context(user)):
                    if note:
                        context = (context + '\n' + note) if context else note
            if persona.is_jacob() and onboarding and user:
                note = onboarding_mod.onboarding_note(onboarding.get(user))
                if note:
                    context = (context + '\n' + note) if context else note
            outgoing = reply_text(
                lookups, incoming, history=history, context=context,
                user_id=user, onboarding=onboarding, user={'id': user}, is_im=is_im)
            who = persona.name()
            is_owner = persona.is_amy() and user == (os.environ.get('SLACK_OWNER_USER_ID') or TOM_USER_ID)
            escalate = needs_escalation(incoming, persona=who) and not is_owner
            if escalate:
                outgoing = apply_guardrails(
                    escalation_reply(incoming, persona=who),
                    amy_confirmed=True, source='escalation', persona=who)[0]
            say(text=outgoing, thread_ts=thread)
            if channel and ts:
                replied.mark(channel, ts)
            if escalate:
                log.warning('escalation flagged channel=%s ts=%s user=%s', channel, ts, user)
                try:
                    flagsink.send(escalation_note(event, outgoing, channel_label=event.get('channel_name')))
                except Exception as exc:  # noqa: BLE001
                    err, needed = slack_error(exc)
                    log.error('escalation post failed: %s needed=%s', err, needed)
        except Exception as exc:  # noqa: BLE001
            if channel and ts:
                replied.release(channel, ts)
            err, needed = slack_error(exc)
            log.error('reply failed type=%s channel=%s ts=%s: %s needed=%s',
                      event.get('type'), channel, ts, err, needed)
            try:
                say(text="Couldn't reply just now, try me again in a minute?", thread_ts=thread)
                if channel and ts:
                    replied.mark(channel, ts)
            except Exception as exc2:  # noqa: BLE001
                err2, needed2 = slack_error(exc2)
                log.error('fallback post failed: %s needed=%s', err2, needed2)

    def say_reply(event, thread):
        handle(event, lambda **kw: app.client.chat_postMessage(
            channel=event.get('channel'), unfurl_links=False, unfurl_media=False, **kw), thread)

    amyi = None
    if user_identity.enabled():
        try:
            amyi = user_identity.AmyiUser.from_env()
        except Exception as exc:  # noqa: BLE001
            log.error('AMYI user identity disabled: %s', type(exc).__name__)
            amyi = None
    app._amyi = amyi

    def amyi_reply(event, thread):
        """Answer as AMYI's own user account (user token)."""
        handle(event, lambda **kw: amyi.client.chat_postMessage(
            channel=event.get('channel'), unfurl_links=False, unfurl_media=False, **kw), thread,
            client=amyi.client, self_user=amyi.self_id, identity_note=user_identity.IDENTITY_NOTE,
            download_token=amyi.token, poster=amyi.client)
    app._amyi_reply = amyi_reply

    def maybe_amyi(event):
        """True if AMYI's user account handled (or owns) this message event."""
        if amyi is None:
            return False
        try:
            where = amyi.route(event)
        except Exception as exc:  # noqa: BLE001
            log.warning('AMYI route failed: %s', type(exc).__name__)
            return False
        if not where:
            return False
        if where == 'dm':
            amyi_reply(event, event.get('thread_ts'))
        else:
            amyi_reply(event, event.get('thread_ts') or event.get('ts'))
        return where == 'dm'

    @app.event('app_mention')
    def on_mention(event, say):
        handle(event, say, thread=event.get('thread_ts') or event.get('ts'))

    @app.event('message')
    def on_message(event, say):
        # Log every message event so we can tell whether Socket Mode is
        # delivering message.channels / message.im (even ones we ignore).
        log.info('incoming type=message subtype=%s channel=%s ts=%s user=%s thread=%s channel_type=%s',
                 event.get('subtype') or '-', event.get('channel') or '', event.get('ts') or '',
                 event.get('user') or event.get('bot_id') or '',
                 event.get('thread_ts') or '', event.get('channel_type') or '')
        if maybe_amyi(event):
            return  # a DM to AMYI's user account; the bot is not in it
        subtype = event.get('subtype')
        if event.get('bot_id') or (subtype and not (
                subtype == 'file_share' and event.get('channel_type') == 'im')):
            return
        if event.get('channel_type') == 'im':
            handle(event, say, thread=event.get('thread_ts'))
            return
        # Mentions (backup if app_mention did not fire) and untagged replies
        # in a thread the bot is already part of. Other channel messages stay quiet.
        if should_answer_channel(event, client=app.client):
            handle(event, say, thread=event.get('thread_ts') or event.get('ts'))
            return
        log.info('ignore untagged channel message channel=%s ts=%s',
                 event.get('channel') or '', event.get('ts') or '')

    def welcome_new_member(user, source='join'):
        user = user if isinstance(user, dict) else {'id': user}
        uid = user.get('id') or ''
        if not should_welcome(user):
            log.info('skip welcome user=%s', uid or '?')
            return
        if onboarding is None or onboarding.intro_sent(uid):
            log.info('skip already-welcomed user=%s', uid)
            return
        text = onboarding_mod.intro_text(user)
        text, _flags = apply_guardrails(
            text, amy_confirmed=True, source='welcome', persona='jacob')
        try:
            dm = app.client.conversations_open(users=uid)['channel']['id']
            app.client.chat_postMessage(
                channel=dm, text=text, unfurl_links=False, unfurl_media=False)
            onboarding.mark_intro_sent(uid, source=source)
            log.info('welcomed user=%s source=%s', uid, source)
        except Exception as exc:  # noqa: BLE001
            err, needed = slack_error(exc)
            log.error('welcome DM failed user=%s: %s needed=%s', uid, err, needed)

    if persona.is_jacob():
        @app.event('team_join')
        def on_team_join(event):
            user = team_join_user(event)
            log.info('incoming type=team_join user=%s', user.get('id') or '')
            welcome_new_member(user)

        @app.event('member_joined_channel')
        def on_member_joined_channel(event):
            user = event.get('user') or ''
            channel = event.get('channel') or ''
            log.info('incoming type=member_joined_channel user=%s channel=%s',
                     user if not isinstance(user, dict) else user.get('id'), channel)
            if onboarding_mod.is_newhire_channel(
                    channel, event.get('channel_name') or event.get('name')):
                # DM the intro; do not post in #newhire. Existing members are
                # not backfilled — only live join events after deploy.
                welcome_new_member(user, source='newhire')

    return app, api, lookups, replied, say_reply, flagsink


def main():
    import sys
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    required = ['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN']
    if not persona.is_jacob():
        required.append('AMY_QUEUE_API_TOKEN')
    for name in required:
        if not os.environ.get(name):
            raise SystemExit(f'{name} is not set')
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app, api, lookups, replied, say_reply, flagsink = build_app()
    if persona.is_jacob():
        flagsink.post = owner_poster(app.client)
        log.info('jacob onboarding coach; escalations DM Tom; answers via %s',
                 'OpenAI ' + (os.environ.get('OPENAI_MODEL') or llm.DEFAULT_MODEL) if llm.configured() else 'keyword fallback')
        if llm.configured():
            threading.Thread(target=startup_check, args=(lookups,), name='openai-self-check', daemon=True).start()
    else:
        router = Router(app.client)
        router.resolve()
        flagsink.post = router.post
        log.info('updates go to %s; answers via %s', router.note or '?',
                 'OpenAI ' + (os.environ.get('OPENAI_MODEL') or llm.DEFAULT_MODEL) if llm.configured() else 'keyword commands')
        if llm.configured():
            threading.Thread(target=startup_check, args=(Lookups(api),), name='openai-self-check', daemon=True).start()
        watcher = Watcher(AmyApi(), router.post)
        threading.Thread(target=watcher.run_forever, name='amy-watcher', daemon=True).start()
        reporter = DailyReporter(AmyApi(), router.post, channel=router.target or router.name,
                                 history_client=app.client)
        threading.Thread(target=reporter.run_forever, name='amy-daily-report', daemon=True).start()
    catchup = Catchup(app.client, say_reply, replied)
    threading.Thread(target=catchup.run_forever, name='amy-catchup', daemon=True).start()
    if getattr(app, '_amyi', None) is not None:
        amyi_catchup = user_identity.AmyiCatchup(app._amyi, app._amyi_reply, replied)
        threading.Thread(target=amyi_catchup.run_forever, name='amyi-catchup', daemon=True).start()
        log.info('AMYI user account replies on (user=%s)', app._amyi.self_id)
    elif persona.is_amy():
        log.info('AMYI user account replies off (SLACK_USER_TOKEN not set)')
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()


if __name__ == '__main__':
    main()

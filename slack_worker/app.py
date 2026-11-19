"""AMY Slack worker: Socket Mode bot (no public URL).

* Posts booking updates (done / manual / needs_review / submission_uncertain,
  stalls, API down) to #amy-bookings, or Tom's DM if that channel is missing.
* Answers DMs and @mentions read-only: OpenAI tool calls when OPENAI_API_KEY is
  set, keyword commands otherwise (and always for exact commands).

Env: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, AMY_BASE_URL, AMY_QUEUE_API_TOKEN,
OPENAI_API_KEY, OPENAI_MODEL, SLACK_UPDATES_CHANNEL (default amy-bookings),
SLACK_OWNER_USER_ID (Tom), SLACK_ALLOWED_USERS (optional comma list),
SLACK_POLL_SECONDS, SLACK_WORKER_STATE. Secrets are never logged.
"""
import logging
import os
import threading
import time

from . import commands, llm
from .amy_api import AmyApi
from .catchup import Catchup, RepliedSet, should_answer_channel
from .guardrails import apply_guardrails
from .history import load_conversation
from .lookups import Lookups
from .views import scrub
from .watcher import Watcher

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


LLM_FAIL_FALLBACK = "Couldn't reach ChatGPT right now. Try me again in a minute?"
VAGUE_FALLBACK = "Not sure I follow — want the queue status, a guest lookup, or a night's bookings?"


def _call_answer(fn, lookups, text, history=None, context=None, meta=None):
    try:
        return fn(lookups, text, history=history, context=context, meta=meta)
    except TypeError:
        return fn(lookups, text)


def reply_text(lookups, text, *, use_llm=None, llm_answer=None, history=None, context=None):
    """Answer one message. Exact keyword commands are answered directly; free
    text goes to the OpenAI model when configured, else a short clarifying
    question. Never dump the command list on a vague message. Guardrails run
    on every outgoing line (no phones, secrets, or unconfirmed 'booked')."""
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


def startup_check(lookups, answer=None, check=None):
    """OpenAI self-check plus one real read-only tool round trip (queue
    status). Logs ok/FAIL only; never the key, never posts to Slack."""
    (check or llm.self_check)()
    try:
        text = (answer or llm.answer)(lookups, 'How many jobs are in done right now? Use the queue_status tool.')
        log.info('OpenAI self-check (round trip): ok, %d chars, has number=%s', len(text),
                 any(ch.isdigit() for ch in text))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error('OpenAI self-check (round trip): FAIL %s', llm.describe(exc))
        return False


def build_app():
    from slack_bolt import App

    app = App(token=os.environ['SLACK_BOT_TOKEN'])
    api = CachedApi(AmyApi())
    lookups = Lookups(api)

    replied = RepliedSet()

    def handle(event, say, thread=None):
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
            history, context = load_conversation(app.client, event, thread)
            say(text=reply_text(lookups, event.get('text') or '', history=history, context=context),
                thread_ts=thread)
            if channel and ts:
                replied.mark(channel, ts)
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
        if event.get('subtype') or event.get('bot_id'):
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

    return app, api, lookups, replied, say_reply


def main():
    import sys
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'), stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    for name in ('SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'AMY_QUEUE_API_TOKEN'):
        if not os.environ.get(name):
            raise SystemExit(f'{name} is not set')
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app, api, lookups, replied, say_reply = build_app()
    router = Router(app.client)
    router.resolve()
    log.info('updates go to %s; answers via %s', router.note or '?',
             'OpenAI ' + (os.environ.get('OPENAI_MODEL') or llm.DEFAULT_MODEL) if llm.configured() else 'keyword commands')
    if llm.configured():
        threading.Thread(target=startup_check, args=(Lookups(api),), name='openai-self-check', daemon=True).start()
    watcher = Watcher(AmyApi(), router.post)
    threading.Thread(target=watcher.run_forever, name='amy-watcher', daemon=True).start()
    catchup = Catchup(app.client, say_reply, replied)
    threading.Thread(target=catchup.run_forever, name='amy-catchup', daemon=True).start()
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()


if __name__ == '__main__':
    main()

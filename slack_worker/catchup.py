"""Replay unanswered @mentions and DMs if Socket Mode missed the event.

Channel answers still require an @mention of this bot. DMs replay only when
the latest message is from a teammate (already-answered threads are skipped).
Processed (channel, ts) pairs are stored on the volume so a restart does not
double-reply. Live event handlers share the same claim/mark set.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger('amy.slack.catchup')
BOT_USER_ID = 'U0CKWBVA7BJ'
BOT_ID = 'B0CK3T1AH8R'
LOOKBACK_SECONDS = 6 * 3600
POLL_SECONDS = 45
HISTORY_LIMIT = 50


def mention_tokens(bot_user=BOT_USER_ID, bot_id=BOT_ID):
    tokens = []
    if bot_id:
        tokens.append(f'<@{bot_id}>')
    if bot_user:
        tokens.append(f'<@{bot_user}>')
    return tuple(tokens)


def is_mention(text, bot_user=BOT_USER_ID, bot_id=BOT_ID):
    text = text or ''
    return any(tok in text for tok in mention_tokens(bot_user, bot_id))


def _is_bot(msg, bot_user=BOT_USER_ID):
    if msg.get('bot_id') or msg.get('subtype') in ('bot_message',):
        return True
    return bool(bot_user and msg.get('user') == bot_user)


def _api_error(exc):
    data = getattr(getattr(exc, 'response', None), 'data', None) or {}
    if isinstance(data, dict):
        return data.get('error') or type(exc).__name__, data.get('needed')
    return type(exc).__name__, None


class RepliedSet:
    """In-memory claim plus a persisted set of successfully answered ts."""

    def __init__(self, path=None):
        self.path = path or os.path.join(
            os.path.dirname(os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json')),
            'replied.json')
        self._lock = threading.Lock()
        self._claimed = set()
        self._done = set()
        self._load()

    def _key(self, channel, ts):
        return f'{channel}:{ts}'

    def _load(self):
        try:
            with open(self.path) as fh:
                rows = json.load(fh)
            if isinstance(rows, list):
                self._done = {str(x) for x in rows}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._done = set()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(sorted(self._done), fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning('could not persist replied set: %s', type(exc).__name__)

    def already(self, channel, ts):
        key = self._key(channel, ts)
        with self._lock:
            return key in self._done or key in self._claimed

    def in_flight(self, channel, ts):
        """True only while a handler has claimed the ts and not yet marked or released it."""
        key = self._key(channel, ts)
        with self._lock:
            return key in self._claimed and key not in self._done

    def claim(self, channel, ts):
        """True if this process should answer this message (not already claimed)."""
        key = self._key(channel, ts)
        with self._lock:
            if key in self._done or key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def mark(self, channel, ts):
        key = self._key(channel, ts)
        with self._lock:
            self._done.add(key)
            self._claimed.discard(key)
            self._save()

    def release(self, channel, ts):
        """Allow a retry if we claimed but failed to post."""
        key = self._key(channel, ts)
        with self._lock:
            self._claimed.discard(key)

    def reopen(self, channel, ts):
        """Drop a persisted mark when Slack still has no bot reply."""
        key = self._key(channel, ts)
        with self._lock:
            self._done.discard(key)
            self._claimed.discard(key)
            self._save()


def _list_channels(client, types):
    out, cursor = [], None
    while True:
        resp = client.conversations_list(types=types, exclude_archived=True, limit=200, cursor=cursor)
        out.extend(resp.get('channels') or [])
        cursor = (resp.get('response_metadata') or {}).get('next_cursor')
        if not cursor:
            return out


def _history(client, channel, limit=HISTORY_LIMIT):
    resp = client.conversations_history(channel=channel, limit=limit)
    return list(resp.get('messages') or [])


def _replies(client, channel, ts, limit=HISTORY_LIMIT):
    resp = client.conversations_replies(channel=channel, ts=ts, limit=limit)
    return list(resp.get('messages') or [])


def _bot_already_replied(client, msg, channel, bot_user=BOT_USER_ID):
    if int(msg.get('reply_count') or 0) <= 0:
        return False
    try:
        replies = _replies(client, channel, msg.get('thread_ts') or msg.get('ts'))
    except Exception as exc:  # noqa: BLE001
        err, needed = _api_error(exc)
        log.warning('replies lookup failed: %s needed=%s', err, needed)
        return False
    mention_ts = msg.get('ts')
    for row in replies:
        if row.get('ts') == mention_ts:
            continue
        if _is_bot(row, bot_user):
            return True
    return False


def unanswered_channel_mentions(client, *, bot_user=BOT_USER_ID, bot_id=BOT_ID,
                                lookback=LOOKBACK_SECONDS, now=None, already=None):
    """Newest-first scan of public channels the bot is in."""
    now = time.time() if now is None else now
    cutoff = now - lookback
    found = []
    try:
        channels = _list_channels(client, 'public_channel')
    except Exception as exc:  # noqa: BLE001
        err, needed = _api_error(exc)
        log.warning('conversations.list failed: %s needed=%s', err, needed)
        return found
    for ch in channels:
        if ch.get('is_member') is False:
            continue
        cid = ch.get('id')
        if not cid:
            continue
        try:
            messages = _history(client, cid)
        except Exception as exc:  # noqa: BLE001
            err, needed = _api_error(exc)
            log.warning('history %s failed: %s needed=%s', cid, err, needed)
            continue
        for msg in messages:
            ts = msg.get('ts') or ''
            try:
                if float(ts) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            if _is_bot(msg, bot_user) or msg.get('subtype'):
                continue
            if not is_mention(msg.get('text') or '', bot_user, bot_id):
                continue
            if already and already(cid, ts):
                continue
            if _bot_already_replied(client, msg, cid, bot_user):
                continue
            event = {
                'type': 'app_mention',
                'channel': cid,
                'channel_type': 'channel',
                'channel_name': ch.get('name'),
                'user': msg.get('user') or '',
                'text': msg.get('text') or '',
                'ts': ts,
                'thread_ts': msg.get('thread_ts'),
            }
            found.append(event)
    return found


def unanswered_dms(client, *, bot_user=BOT_USER_ID, lookback=LOOKBACK_SECONDS, now=None, already=None):
    """If the latest DM message is from a teammate, treat it as unanswered."""
    now = time.time() if now is None else now
    cutoff = now - lookback
    found = []
    try:
        dms = _list_channels(client, 'im')
    except Exception as exc:  # noqa: BLE001
        err, needed = _api_error(exc)
        log.warning('im list failed: %s needed=%s', err, needed)
        return found
    for ch in dms:
        cid = ch.get('id')
        if not cid:
            continue
        try:
            messages = _history(client, cid)
        except Exception as exc:  # noqa: BLE001
            err, needed = _api_error(exc)
            log.warning('im history %s failed: %s needed=%s', cid, err, needed)
            continue
        # history is newest-first
        latest = None
        for msg in messages:
            if msg.get('subtype') in ('channel_join', 'channel_leave', 'message_changed', 'message_deleted'):
                continue
            if not (msg.get('text') or '').strip():
                continue
            latest = msg
            break
        if not latest or _is_bot(latest, bot_user):
            continue
        ts = latest.get('ts') or ''
        try:
            if float(ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        if already and already(cid, ts):
            continue
        found.append({
            'type': 'message',
            'channel': cid,
            'channel_type': 'im',
            'user': latest.get('user') or ch.get('user') or '',
            'text': latest.get('text') or '',
            'ts': ts,
            'thread_ts': latest.get('thread_ts'),
        })
    return found


class Catchup:
    def __init__(self, client, reply, replied=None, *, clock=time.time, sleeper=time.sleep,
                 lookback=LOOKBACK_SECONDS, poll=POLL_SECONDS, bot_user=BOT_USER_ID, bot_id=BOT_ID):
        self.client = client
        self.reply = reply
        self.replied = replied or RepliedSet()
        self.clock = clock
        self.sleeper = sleeper
        self.lookback = lookback
        self.poll = poll
        self.bot_user = bot_user
        self.bot_id = bot_id

    def pending(self):
        # Slack is the source of truth for whether we already replied. A persisted
        # mark must not hide an unanswered mention (a prior catch-up bug marked
        # before posting). Only skip ts the handler currently has in flight.
        already = self.replied.in_flight
        now = self.clock()
        mentions = unanswered_channel_mentions(
            self.client, bot_user=self.bot_user, bot_id=self.bot_id,
            lookback=self.lookback, now=now, already=already)
        dms = unanswered_dms(
            self.client, bot_user=self.bot_user, lookback=self.lookback, now=now, already=already)
        return mentions + dms

    def run_once(self):
        pending = self.pending()
        if pending:
            log.info('catch-up found %d unanswered message(s)', len(pending))
        for event in pending:
            channel, ts = event.get('channel'), event.get('ts')
            if self.replied.in_flight(channel, ts):
                continue
            thread = event.get('thread_ts') or (ts if event.get('channel_type') != 'im' else None)
            try:
                # handle() claims, posts, and marks. Do not claim here — that
                # made handle skip the post and still look like a success.
                # Reopen a stale disk mark so handle.claim() can succeed.
                self.replied.reopen(channel, ts)
                self.reply(event, thread)
                log.info('catch-up replied channel=%s ts=%s', channel, ts)
            except Exception as exc:  # noqa: BLE001
                err, needed = _api_error(exc)
                log.error('catch-up reply failed channel=%s ts=%s: %s needed=%s',
                          channel, ts, err, needed)

    def run_forever(self):
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.error('catch-up loop failed: %s', type(exc).__name__)
            self.sleeper(self.poll)

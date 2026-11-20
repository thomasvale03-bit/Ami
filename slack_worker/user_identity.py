"""AMYI's own Slack *user* account (infojarvis, display name AMYI) answering
as itself, through a user token (xoxp) from the AMY Worker app.

Enabled only for SLACK_PERSONA=amy when SLACK_USER_TOKEN is set. The app's
user event subscriptions (message.im, message.channels, message.groups)
deliver DMs to AMYI and channel messages where AMYI is a member over the
same Socket Mode connection as the bot events. This module decides which of
those AMYI answers:

* every teammate DM to AMYI (never AMYI's own messages, never bots);
* channel messages that @mention AMYI, and untagged replies in a thread
  AMYI already posted in or that started with an @mention of AMYI.

Replies use the same read-only AMY lookups, voice, guardrails and per-DM /
per-thread history (last ~30 messages) as AMY Worker, and are posted with
AMYI's user token. Flyer images from Tom are re-posted to #weeklyevents.
The token is never logged.
"""
import logging
import os
import threading
import time

from . import flyers

log = logging.getLogger('amy.slack.amyi')
AMYI_USER_ID = 'U0CJYJRMRL6'
AMY_WORKER_USER_ID = 'U0CKWBVA7BJ'
IM_REFRESH_SECONDS = 300
CATCHUP_POLL_SECONDS = 45
CATCHUP_GRACE_SECONDS = 120

IDENTITY_NOTE = (
    "You are replying as AMYI from AMYI's own Slack account (Tom's Director of Customer Relations "
    "for Playmaker Entertainment), not as the AMY Worker bot. Same rules and read-only tools.")
OWNER_NOTE = (
    "The person messaging is Tom, the owner and your boss, not a promoter. Talk to him like his assistant: "
    "direct, conversational, follow up on what he said earlier in this conversation. You still can't book, "
    "cancel, change or email anything from Slack; if he asks for that, say plainly you can't do it from here.")


def enabled():
    from . import persona
    return persona.is_amy() and bool((os.environ.get('SLACK_USER_TOKEN') or '').strip())


def mentions(text, user_id=AMYI_USER_ID):
    return f'<@{user_id}>' in (text or '')


def is_from_self_or_bot(msg, self_id=AMYI_USER_ID):
    if msg.get('bot_id') or msg.get('subtype') in ('bot_message',):
        return True
    return (msg.get('user') or '') in (self_id, AMY_WORKER_USER_ID)


ANSWERABLE_SUBTYPES = (None, '', 'file_share', 'thread_broadcast')


class AmyiUser:
    def __init__(self, client, *, self_id=None, token=None, clock=time.time):
        self.client = client
        self.token = token or os.environ.get('SLACK_USER_TOKEN', '').strip()
        self.self_id = self_id or AMYI_USER_ID
        self.clock = clock
        self._ims = {}
        self._ims_at = 0.0
        self._lock = threading.Lock()
        self.started = clock()

    @classmethod
    def from_env(cls):
        from slack_sdk import WebClient
        token = os.environ['SLACK_USER_TOKEN'].strip()
        client = WebClient(token=token)
        self_id = AMYI_USER_ID
        try:
            auth = client.auth_test()
            self_id = auth.get('user_id') or AMYI_USER_ID
            log.info('AMYI user token ok: user=%s team=%s', self_id, auth.get('team_id'))
        except Exception as exc:  # noqa: BLE001
            log.error('AMYI user token auth.test failed: %s', type(exc).__name__)
        return cls(client, self_id=self_id, token=token)

    # --- which DMs are AMYI's -------------------------------------------
    def refresh_ims(self, force=False):
        with self._lock:
            if not force and self._ims and self.clock() - self._ims_at < IM_REFRESH_SECONDS:
                return dict(self._ims)
        ims, cursor = {}, None
        try:
            while True:
                resp = self.client.conversations_list(types='im', limit=200, cursor=cursor)
                for ch in resp.get('channels') or []:
                    if ch.get('id'):
                        ims[ch['id']] = ch.get('user') or ''
                cursor = (resp.get('response_metadata') or {}).get('next_cursor')
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001
            data = getattr(getattr(exc, 'response', None), 'data', None) or {}
            log.warning('AMYI im list failed: %s needed=%s',
                        (data.get('error') if isinstance(data, dict) else None) or type(exc).__name__,
                        data.get('needed') if isinstance(data, dict) else None)
            return dict(self._ims)
        with self._lock:
            self._ims, self._ims_at = ims, self.clock()
        return dict(ims)

    def owns_dm(self, channel):
        if not channel or not channel.startswith('D'):
            return False
        if channel in self.refresh_ims():
            return True
        return channel in self.refresh_ims(force=self.clock() - self._ims_at > 10)

    # --- routing ----------------------------------------------------------
    def thread_is_ours(self, channel, thread_ts):
        try:
            resp = self.client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        except Exception as exc:  # noqa: BLE001
            log.info('AMYI thread lookup failed: %s', type(exc).__name__)
            return False
        msgs = resp.get('messages') or []
        if not msgs:
            return False
        if mentions(msgs[0].get('text'), self.self_id):
            return True
        return any((m.get('user') or '') == self.self_id for m in msgs)

    def route(self, event):
        """-> 'dm', 'channel' or None (not for AMYI)."""
        if event.get('subtype') not in ANSWERABLE_SUBTYPES:
            return None
        if is_from_self_or_bot(event, self.self_id):
            return None
        channel = event.get('channel') or ''
        if event.get('channel_type') == 'im' or channel.startswith('D'):
            return 'dm' if self.owns_dm(channel) else None
        if mentions(event.get('text'), self.self_id):
            return 'channel'
        thread = event.get('thread_ts')
        if thread and thread != event.get('ts') and self.thread_is_ours(channel, thread):
            return 'channel'
        return None


def identity_context(user, owner=None):
    owner = owner or flyers.owner_id()
    return OWNER_NOTE if user and user == owner else ''


class AmyiCatchup:
    """Replays DMs to AMYI that Socket Mode missed (reconnects/restarts).
    Only messages newer than process start minus a short grace window, so a
    deploy never answers old conversations."""

    def __init__(self, amyi, reply, replied, *, clock=time.time, sleeper=time.sleep,
                 poll=CATCHUP_POLL_SECONDS, grace=CATCHUP_GRACE_SECONDS):
        self.amyi, self.reply, self.replied = amyi, reply, replied
        self.clock, self.sleeper, self.poll = clock, sleeper, poll
        self.cutoff = amyi.started - grace

    def pending(self):
        out = []
        for channel, other in self.amyi.refresh_ims().items():
            if not other or other in ('USLACKBOT', AMY_WORKER_USER_ID, self.amyi.self_id):
                continue
            try:
                resp = self.amyi.client.conversations_history(channel=channel, limit=5, oldest=str(self.cutoff))
            except Exception as exc:  # noqa: BLE001
                log.info('AMYI catch-up history %s failed: %s', channel, type(exc).__name__)
                continue
            msgs = resp.get('messages') or []  # newest first
            if not msgs:
                continue
            newest = msgs[0]
            if is_from_self_or_bot(newest, self.amyi.self_id):
                continue
            if newest.get('subtype') not in ANSWERABLE_SUBTYPES:
                continue
            ts = newest.get('ts') or ''
            if not ts or float(ts) < self.cutoff or self.replied.already(channel, ts):
                continue
            if float(ts) > self.clock() - 20:
                continue  # give the live event a moment first
            event = dict(newest, channel=channel, channel_type='im')
            out.append(event)
        return out

    def run_once(self):
        n = 0
        for event in self.pending():
            log.info('AMYI catch-up reply channel=%s ts=%s', event.get('channel'), event.get('ts'))
            self.reply(event, event.get('thread_ts'))
            n += 1
        return n

    def run_forever(self):
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.warning('AMYI catch-up failed: %s', type(exc).__name__)
            self.sleeper(self.poll)

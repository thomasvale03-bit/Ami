"""Recent Slack DM or thread history plus a per-message context note.

Uses conversations.history / conversations.replies. The bot token already
has channels:history, groups:history, im:history, and users:read. A missing
scope is logged and we answer without history rather than failing the turn.
"""
import logging

from .prompt import context_note as render_context

log = logging.getLogger('amy.slack.history')
HISTORY_LIMIT = 30
BOT_USER_ID = 'U0CKWBVA7BJ'


def _api_error(exc):
    data = getattr(getattr(exc, 'response', None), 'data', None) or {}
    if isinstance(data, dict):
        return data.get('error') or type(exc).__name__, data.get('needed')
    return type(exc).__name__, None


def _is_bot(msg, bot_user=BOT_USER_ID):
    if msg.get('bot_id') or msg.get('subtype') in ('bot_message',):
        return True
    return bool(bot_user and msg.get('user') == bot_user)


def _plain(text):
    return ' '.join((text or '').split()).strip()


def channel_label(event):
    ctype = event.get('channel_type') or ''
    if ctype == 'im' or (event.get('channel') or '').startswith('D'):
        return 'DM'
    name = event.get('channel_name')
    if name:
        return f'#{name.lstrip("#")}'
    cid = event.get('channel') or ''
    if event.get('thread_ts'):
        return f'channel thread {cid}'.strip()
    return f'channel {cid}'.strip() if cid else 'Slack'


def asker_label(client, user_id, cache=None):
    if not user_id:
        return None
    if cache is not None and user_id in cache:
        return cache[user_id]
    label = user_id
    if user_id == 'U0CK42KJT7E':
        label = f'Tom ({user_id})'
    elif client is not None:
        try:
            info = (client.users_info(user=user_id) or {}).get('user') or {}
            name = info.get('real_name') or info.get('profile', {}).get('display_name') or info.get('name')
            label = f'{name} ({user_id})' if name else user_id
        except Exception as exc:  # noqa: BLE001
            err, needed = _api_error(exc)
            log.info('users_info failed: %s needed=%s', err, needed)
    if cache is not None:
        cache[user_id] = label
    return label


def to_api_messages(history, newest_text, newest_context=None):
    """History rows {role, content} + the new user text -> Responses API input.

    Consecutive same-role turns are merged; a leading assistant turn is
    dropped so the list starts with the user (PartyEveryDay's toApiMessages).
    """
    turns = []
    for row in history or []:
        role = row.get('role')
        text = row.get('content') if isinstance(row.get('content'), str) else ''
        if role not in ('user', 'assistant') or not text.strip():
            continue
        if turns and turns[-1]['role'] == role:
            turns[-1]['content'] += '\n' + text.strip()
        else:
            turns.append({'role': role, 'content': text.strip()})
    while turns and turns[0]['role'] == 'assistant':
        turns.pop(0)
    newest = (newest_text or '').strip()
    if turns and turns[-1]['role'] == 'user':
        # Current message may already be the last history row; don't duplicate.
        if turns[-1]['content'] != newest:
            turns.append({'role': 'user', 'content': newest})
    else:
        turns.append({'role': 'user', 'content': newest})
    if newest_context:
        turns[-1]['content'] = newest_context.rstrip() + '\n\n' + turns[-1]['content']
    return turns


def fetch_history(client, event, thread_ts=None, *, bot_user=BOT_USER_ID, limit=HISTORY_LIMIT):
    """Last ~30 messages of the DM or thread, oldest first, excluding the
    current event. Returns [] if Slack refuses (missing scope, etc.)."""
    if client is None:
        return []
    channel = event.get('channel')
    current_ts = event.get('ts')
    parent = thread_ts or event.get('thread_ts')
    try:
        if parent:
            resp = client.conversations_replies(channel=channel, ts=parent, limit=limit)
            messages = list(resp.get('messages') or [])
        else:
            resp = client.conversations_history(channel=channel, limit=limit)
            # history is newest-first
            messages = list(reversed(resp.get('messages') or []))
    except Exception as exc:  # noqa: BLE001
        err, needed = _api_error(exc)
        log.warning('history fetch failed: %s needed=%s (answering without history)', err, needed)
        return []
    out = []
    for msg in messages:
        if msg.get('ts') == current_ts:
            continue
        if msg.get('subtype') in ('channel_join', 'channel_leave', 'message_changed', 'message_deleted'):
            continue
        text = _plain(msg.get('text'))
        if not text:
            continue
        role = 'assistant' if _is_bot(msg, bot_user) else 'user'
        out.append({'role': role, 'content': text, 'user': msg.get('user'), 'ts': msg.get('ts')})
    return out[-limit:]


def load_conversation(client, event, thread_ts=None, *, now=None, bot_user=BOT_USER_ID):
    """-> (history_rows, context_note_text). Never raises."""
    history = fetch_history(client, event, thread_ts, bot_user=bot_user)
    asker = asker_label(client, event.get('user') or '')
    note = render_context(asker=asker, channel_label=channel_label(event), now=now)
    return history, note

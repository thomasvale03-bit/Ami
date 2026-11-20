"""Flyer images Tom sends in a DM (to AMY Worker or to AMYI's own user
account) are re-posted to #weeklyevents and confirmed back to him.

Only image files from the owner (SLACK_OWNER_USER_ID, Tom) count as flyers.
Each Slack file id is posted at most once (persisted next to the worker
state), and a file whose name and size already appear in the channel's
recent history is not posted again.
"""
import json
import logging
import os
import re
import threading
import urllib.request

log = logging.getLogger('amy.slack.flyers')
WEEKLYEVENTS_CHANNEL = 'C0CK42NLM9A'
TOM_USER_ID = 'U0CK42KJT7E'
MAX_BYTES = 25 * 1024 * 1024
# WhatsApp share boilerplate Tom's forwards carry; not a caption.
_BOILERPLATE = re.compile(
    r'(?:\u200e)?sent from\s*(?:\u200e)?whatsapp business|<?https?://wa\.me/[^\s>|]*(?:\|[^>]*)?>?|wa\.me/\+?\d+',
    re.I)


def channel_id():
    return (os.environ.get('SLACK_FLYER_CHANNEL') or WEEKLYEVENTS_CHANNEL).strip()


def owner_id():
    return (os.environ.get('SLACK_OWNER_USER_ID') or TOM_USER_ID).strip()


def image_files(event):
    return [f for f in (event.get('files') or [])
            if str(f.get('mimetype') or '').startswith('image/') or
            str(f.get('filetype') or '').lower() in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'heic')]


def is_flyer_event(event, owner=None):
    """An image shared by Tom in a DM."""
    owner = owner or owner_id()
    if (event.get('user') or '') != owner:
        return False
    is_im = event.get('channel_type') == 'im' or (event.get('channel') or '').startswith('D')
    return is_im and bool(image_files(event))


def caption(text):
    """Tom's own words from the message, minus WhatsApp boilerplate."""
    cleaned = _BOILERPLATE.sub(' ', text or '')
    cleaned = ' '.join(cleaned.replace('\u200e', ' ').split()).strip()
    return cleaned


class PostedSet:
    def __init__(self, path=None):
        self.path = path or os.path.join(
            os.path.dirname(os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json')),
            'flyers-posted.json')
        self._lock = threading.Lock()
        try:
            with open(self.path) as fh:
                self._ids = set(json.load(fh))
        except (OSError, ValueError, TypeError):
            self._ids = set()

    def has(self, file_id):
        with self._lock:
            return file_id in self._ids

    def add(self, file_id):
        with self._lock:
            self._ids.add(file_id)
            try:
                os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
                tmp = self.path + '.tmp'
                with open(tmp, 'w') as fh:
                    json.dump(sorted(self._ids), fh)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning('could not persist flyer ids: %s', type(exc).__name__)


def download(file_obj, token, opener=None):
    """Bytes of a Slack file via url_private (needs files:read). Token never logged."""
    url = file_obj.get('url_private_download') or file_obj.get('url_private')
    if not url:
        raise ValueError('file has no private url')
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with (opener or urllib.request.urlopen)(req, timeout=60) as resp:
        ctype = resp.headers.get('Content-Type', '') if hasattr(resp, 'headers') else ''
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('file too large')
    if ctype.startswith('text/html'):
        raise ValueError('got an HTML page instead of the file (missing files:read?)')
    return data


def already_in_channel(client, channel, file_obj, limit=100):
    """True if a file with the same name and size is already in the channel's recent history."""
    name, size = file_obj.get('name'), file_obj.get('size')
    if not name:
        return False
    try:
        resp = client.conversations_history(channel=channel, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.info('flyer channel history check failed: %s', type(exc).__name__)
        return False
    for msg in resp.get('messages') or []:
        for f in msg.get('files') or []:
            if f.get('id') == file_obj.get('id'):
                return True
            if f.get('name') == name and (not size or not f.get('size') or f.get('size') == size):
                return True
    return False


def share_flyers(event, *, download_token, poster, posted=None, fetch=None):
    """Post Tom's flyer images from this DM event to #weeklyevents.

    poster is a slack_sdk WebClient for the identity doing the post (AMY
    Worker bot or AMYI's user token). Returns (posted_count, skipped_count,
    failed_count).
    """
    channel = channel_id()
    posted = posted if posted is not None else PostedSet()
    fetch = fetch or download
    files = image_files(event)
    text = caption(event.get('text') or '')
    done = skipped = failed = 0
    uploads = []
    for f in files:
        fid = f.get('id') or ''
        if (fid and posted.has(fid)) or already_in_channel(poster, channel, f):
            skipped += 1
            if fid:
                posted.add(fid)
            continue
        try:
            uploads.append((f, fetch(f, download_token)))
        except Exception as exc:  # noqa: BLE001
            log.error('flyer download failed file=%s: %s', fid, type(exc).__name__)
            failed += 1
    if uploads:
        try:
            kwargs = {'channel': channel, 'file_uploads': [
                {'content': data, 'filename': f.get('name') or 'flyer.jpg', 'title': f.get('title') or f.get('name') or 'Flyer'}
                for f, data in uploads]}
            if text:
                kwargs['initial_comment'] = text
            poster.files_upload_v2(**kwargs)
            for f, _data in uploads:
                if f.get('id'):
                    posted.add(f['id'])
            done = len(uploads)
            log.info('flyers posted channel=%s count=%d', channel, done)
        except Exception as exc:  # noqa: BLE001
            data = getattr(getattr(exc, 'response', None), 'data', None) or {}
            log.error('flyer upload failed: %s needed=%s',
                      (data.get('error') if isinstance(data, dict) else None) or type(exc).__name__,
                      data.get('needed') if isinstance(data, dict) else None)
            failed += len(uploads)
    return done, skipped, failed


def confirmation(done, skipped, failed):
    where = '#weeklyevents'
    if failed and not done:
        return f"Couldn't post that flyer to {where} just now. I'll need you to drop it in there or send it again."
    parts = []
    if done:
        parts.append(f"Posted {'it' if done == 1 else f'all {done}'} to {where}")
    if skipped:
        parts.append(f"{'That one was' if skipped == 1 else f'{skipped} were'} already in {where}, so I didn't double post")
    if failed:
        parts.append(f"{failed} didn't go through, send {'it' if failed == 1 else 'those'} again?")
    out = '. '.join(parts)
    return out + (' :white_check_mark:' if done and not skipped and not failed else '.')

"""Persist which workspace members Jacob has already welcomed."""
import json
import os
import threading


def _default_path():
    state = os.environ.get('SLACK_WORKER_STATE', '/data/slack-worker/state.json')
    return os.path.join(os.path.dirname(state) or '.', 'jacob-welcomed.json')


class WelcomedSet:
    """Volume-backed set of Slack user IDs that already got the welcome DM.

    Only live team_join events trigger a welcome; this is not a backfill of
    existing members.
    """

    def __init__(self, path=None):
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self._ids = set()
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._ids = {str(x) for x in data if x}
        except (OSError, ValueError):
            self._ids = set()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(sorted(self._ids), fh)
        os.replace(tmp, self.path)

    def already(self, user_id):
        user_id = str(user_id or '')
        if not user_id:
            return True
        with self._lock:
            return user_id in self._ids

    def mark(self, user_id):
        user_id = str(user_id or '')
        if not user_id:
            return
        with self._lock:
            self._ids.add(user_id)
            try:
                self._save()
            except OSError:
                pass

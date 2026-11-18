"""Read-only client for the AMY queue API (GET only).

The Slack worker never claims, completes, requeues, books, cancels or emails.
It only reads /queue/status and /queue/jobs with AMY_QUEUE_API_TOKEN. The
token is never logged or included in an exception message.
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = 'https://amy-production-1616.up.railway.app'


class AmyApiError(RuntimeError):
    pass


class AmyApi:
    def __init__(self, base_url=None, token=None, timeout=20, opener=None):
        self.base = (base_url or os.environ.get('AMY_BASE_URL') or DEFAULT_BASE).rstrip('/')
        self._token = token if token is not None else os.environ.get('AMY_QUEUE_API_TOKEN', '')
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _get(self, path):
        req = urllib.request.Request(self.base + path, method='GET', headers={
            'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
            'User-Agent': 'amy-slack-worker/1'})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:  # message never carries the token
            raise AmyApiError(f'AMY API {path.split("?")[0]} returned HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise AmyApiError(f'AMY API {path.split("?")[0]} unreachable: {type(exc).__name__}') from None

    def status(self):
        return self._get('/queue/status')

    def jobs(self, limit=1000):
        return self._get(f'/queue/jobs?limit={int(limit)}').get('jobs') or []

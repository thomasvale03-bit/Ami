#!/usr/bin/env python3
"""Post one morning #generalchat report: Jarvis's text plus last night's venues.

AMYI runs this after Jarvis hands over the Daily Guestlist Report text, so
there is one post per morning instead of a separate 6:00 AM venue post.

  python3 tools/post_daily_report.py --report-file /path/to/jarvis-report.txt

Optional: --date YYYY-MM-DD (default: yesterday America/Phoenix), --channel,
--dry-run (print the composed message, do not post), --jobs-file (skip the
live queue). Uses the AMY Worker bot token; never prints it. Queue reads go
through the box queue client (GET /queue/jobs only).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from slack_worker.daily_report import (  # noqa: E402
    DEFAULT_CHANNEL, PHOENIX, merge_daily_report, previous_night, report_channel,
)

DEFAULT_QUEUE_CLIENT = '/home/box/agent-data/amy-queue-client/request_client.py'
DEFAULT_BOT_TOKEN_FILE = '/home/box/agent-data/slack-worker/bot-token'
SLACK_POST_URL = 'https://slack.com/api/chat.postMessage'


def _jobs_from_blob(blob):
    if not isinstance(blob, dict):
        raise SystemExit('jobs file must be a JSON object')
    if isinstance(blob.get('jobs'), list):
        return blob['jobs']
    body = blob.get('body')
    if isinstance(body, dict) and isinstance(body.get('jobs'), list):
        return body['jobs']
    raise SystemExit('jobs file has no jobs list')


def load_jobs(jobs_file=None, limit=1000):
    """Load queue jobs from a file, or live via the box queue client."""
    if jobs_file:
        return _jobs_from_blob(json.loads(Path(jobs_file).read_text()))
    client_path = Path(os.environ.get('AMY_QUEUE_CLIENT') or DEFAULT_QUEUE_CLIENT)
    if not client_path.is_file():
        raise SystemExit(f'queue client not found: {client_path}')
    spec = importlib.util.spec_from_file_location('amy_queue_request_client', client_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    status, data = mod.send('GET', f'/queue/jobs?limit={int(limit)}',
                            response_name='daily-report-jobs.json')
    if status != 200:
        raise SystemExit(f'queue /queue/jobs returned HTTP {status}')
    jobs = (data or {}).get('jobs') if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise SystemExit('queue /queue/jobs returned no jobs list')
    return jobs


def _bot_token(token_file=None):
    env = (os.environ.get('SLACK_BOT_TOKEN') or '').strip()
    if env:
        return env
    path = Path(token_file or os.environ.get('SLACK_BOT_TOKEN_FILE')
                or DEFAULT_BOT_TOKEN_FILE)
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise SystemExit(f'Slack bot token file is unreadable: {type(exc).__name__}') from None
    if not token:
        raise SystemExit('Slack bot token file is empty')
    return token


def post_channel(text, channel, token_file=None):
    """Post as the AMY Worker bot. Never logs or returns the token."""
    token = _bot_token(token_file)
    payload = json.dumps({
        'channel': channel,
        'text': text,
        'unfurl_links': False,
        'unfurl_media': False,
    }).encode()
    req = urllib.request.Request(SLACK_POST_URL, data=payload, method='POST', headers={
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json; charset=utf-8',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'Slack chat.postMessage HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SystemExit(f'Slack chat.postMessage failed: {type(exc).__name__}') from None
    if not data.get('ok'):
        raise SystemExit(f"Slack chat.postMessage error: {data.get('error') or 'unknown'}")
    return data.get('ts') or ''


def parse_night(value, now=None):
    if not value:
        return previous_night(now)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f'invalid --date {value!r}; use YYYY-MM-DD') from None


def compose(report_file, jobs, night=None, now=None):
    text = Path(report_file).read_text() if report_file else ''
    return merge_daily_report(text, jobs, night=night, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-file', required=True,
                        help="Path to Jarvis's Daily Guestlist Report text")
    parser.add_argument('--date', help='Night to count (YYYY-MM-DD). Default: yesterday MST')
    parser.add_argument('--channel', help='Slack channel id (default #generalchat)')
    parser.add_argument('--jobs-file', help='JSON jobs dump; skip the live queue')
    parser.add_argument('--token-file', help='Bot token file (never printed)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the composed message and do not post')
    args = parser.parse_args(argv)

    report_path = Path(args.report_file)
    if not report_path.is_file():
        raise SystemExit(f'report file not found: {report_path}')

    now = datetime.now(PHOENIX).timestamp()
    night = parse_night(args.date, now)
    jobs = load_jobs(args.jobs_file)
    text = compose(report_path, jobs, night=night, now=now)
    channel = (args.channel or report_channel() or DEFAULT_CHANNEL).strip()
    if args.dry_run:
        sys.stdout.write(text if text.endswith('\n') else text + '\n')
        return 0
    ts = post_channel(text, channel, token_file=args.token_file)
    print(f'posted {channel} ts={ts} night={night.isoformat()} ({len(text)} chars)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

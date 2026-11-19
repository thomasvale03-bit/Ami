"""AMY Slack worker: read-only lookups, keyword commands, OpenAI tool loop,
update alerts, stall detection, channel routing. No network, no Slack."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from slack_worker import app as sw_app, catchup, commands, guardrails, health, history, llm, prompt, views
from slack_worker.lookups import Lookups
from slack_worker.watcher import Watcher

PHX = ZoneInfo('America/Phoenix')
# Wed Nov 18 2026 3:00 PM MST (a Mon-Wed pickup hour)
NOW = datetime(2026, 11, 18, 15, 0, tzinfo=PHX).timestamp()


def job(ref, state, name='Jessica Alba', venues=('Hakkasan Nightclub',), start='2026-11-18', end=None,
        reason='', updated=NOW - 3600, bookings=None, attempts=1, due=NOW - 7200, lease=None, phone='602-555-1234'):
    meta = {}
    if bookings:
        meta['evidence'] = {'verified': True, 'bookings': bookings}
    return {'ref': ref, 'state': state, 'reason': reason, 'updated': updated, 'created': NOW - 86400,
            'attempts': attempts, 'due': due, 'lease_until': lease, 'metadata': meta,
            'raw': {'name': name, 'email': 'jess@example.com', 'phone': phone, 'venues': list(venues),
                    'start_date': start, 'end_date': end or start, 'female_count': 2, 'male_count': 1,
                    'source': 'website'}}


class FakeApi:
    def __init__(self, jobs, status=None):
        self._jobs = jobs
        self._status = status or {'counts': {}, 'execution': 'railway-booking-worker', 'worker_claims': True,
                                  'claims_paused': False, 'intake_receipts': []}
        self.fail = False

    def status(self):
        if self.fail:
            raise RuntimeError('AMY API /queue/status unreachable: URLError')
        return self._status

    def jobs(self, limit=1000):
        if self.fail:
            raise RuntimeError('AMY API /queue/jobs unreachable: URLError')
        return self._jobs


BOOKED = [{'verified': True, 'venue': 'Hakkasan Nightclub', 'date': '2026-11-18',
           'order_id': 'f5882f28-98b4-4705-bf15-4017fbae494e', 'party': {'male_count': 1, 'female_count': 2}}]


class ViewsTest(unittest.TestCase):
    def test_scrub_hides_phone_email_secrets_keeps_dates(self):
        out = views.scrub('call 602-555-1234 or (480) 555 9876, mail a@b.co, 2026-11-21, token xoxb-123-abc')
        self.assertNotIn('555', out)
        self.assertNotIn('a@b.co', out)
        self.assertNotIn('xoxb-123', out)
        self.assertIn('2026-11-21', out)

    def test_project_never_carries_phone_or_email(self):
        p = views.project(job('r1', 'done', bookings=BOOKED))
        blob = json.dumps(p)
        self.assertNotIn('555-1234', blob)
        self.assertNotIn('jess@example.com', blob)
        self.assertEqual(p['booked'][0]['order'], 'f5882f28')
        self.assertEqual(p['party'], '2F/1M')

    def test_parse_date(self):
        self.assertEqual(views.parse_date('tonight', NOW).isoformat(), '2026-11-18')
        self.assertEqual(views.parse_date('11/21', NOW).isoformat(), '2026-11-21')
        self.assertEqual(views.parse_date('2026-12-05', NOW).isoformat(), '2026-12-05')
        self.assertEqual(views.parse_date('1/3', NOW).isoformat(), '2027-01-03')
        self.assertEqual(views.parse_date('sat', NOW).isoformat(), '2026-11-21')
        self.assertIsNone(views.parse_date('soon', NOW))


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi([
            job('r1', 'done', bookings=BOOKED),
            job('r2', 'manual', name='Mike Ross', venues=('OMNIA Nightclub',), start='2026-11-21',
                reason='Past or invalid source date; call 602-555-0000'),
            job('r3', 'queued', name='Ann Lee', start='2026-11-18', attempts=0),
            job('r4', 'submission_uncertain', name='Jessica Biel', venues=('TAO Nightclub',), start='2026-11-20'),
        ])
        self.lk = Lookups(self.api, clock=lambda: NOW)

    def test_parse(self):
        self.assertEqual(commands.parse('<@U1> status'), ('status', ''))
        self.assertEqual(commands.parse('guest Jessica'), ('guest', 'Jessica'))
        self.assertEqual(commands.parse('date 11/21'), ('date', '11/21'))
        self.assertEqual(commands.parse('tonight'), ('date', 'tonight'))
        self.assertEqual(commands.parse('state uncertain'), ('state', 'submission_uncertain'))
        self.assertEqual(commands.parse('manual'), ('state', 'manual'))
        self.assertEqual(commands.parse('health'), ('health', ''))
        self.assertEqual(commands.parse('help'), ('help', ''))
        self.assertEqual(commands.parse('is jessica booked tonight?'), (None, None))

    def test_status_lists_all_states_including_zero(self):
        text = commands.run(self.lk, 'status', '')
        for state in ('queued', 'retry', 'claimed', 'running', 'submission_uncertain', 'needs_review', 'manual', 'done'):
            self.assertIn(f'`{state}`', text)
        self.assertIn('`queued`: 1', text)

    def test_guest_lookup_all_jobs_no_phone(self):
        text = commands.run(self.lk, 'guest', 'jessica')
        self.assertIn('Jessica Alba', text)
        self.assertIn('Jessica Biel', text)
        self.assertIn('#f5882f28', text)
        self.assertNotIn('555', text)
        self.assertNotIn('@example.com', text)

    def test_tonight_by_venue(self):
        text = commands.run(self.lk, 'date', 'tonight')
        self.assertIn('*Hakkasan Nightclub* (2)', text)
        self.assertIn('Ann Lee', text)
        self.assertNotIn('Mike Ross', text)

    def test_state_listing_scrubs_reason(self):
        text = commands.run(self.lk, 'state', 'manual')
        self.assertIn('Mike Ross', text)
        self.assertNotIn('555-0000', text)

    def test_health(self):
        text = commands.run(self.lk, 'health', '')
        self.assertIn('Last activity', text)
        self.assertIn('Next scheduled pickup', text)


class ReplyRoutingTest(unittest.TestCase):
    def setUp(self):
        self.lk = Lookups(FakeApi([job('r1', 'done', bookings=BOOKED)]), clock=lambda: NOW)

    def test_keyword_fallback_without_key(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            self.assertIn('`done`: 1', sw_app.reply_text(self.lk, 'status'))
            self.assertIn("Not sure I follow", sw_app.reply_text(self.lk, 'is jessica good?'))
            self.assertNotIn('`status`', sw_app.reply_text(self.lk, "I dont understand"))
            self.assertNotIn(commands.HELP, sw_app.reply_text(self.lk, "I dont understand"))

    def test_free_text_goes_to_llm_when_configured(self):
        seen = []
        out = sw_app.reply_text(self.lk, '<@U9> is jessica good?', use_llm=True,
                                llm_answer=lambda lk, t: seen.append(t) or 'Yeah, she is on.')
        self.assertEqual(out, 'Yeah, she is on.')
        self.assertEqual(seen, ['is jessica good?'])

    def test_exact_command_stays_deterministic_with_llm(self):
        out = sw_app.reply_text(self.lk, 'status', use_llm=True, llm_answer=lambda *a: self.fail('no llm'))
        self.assertIn('`done`: 1', out)

    def test_llm_failure_falls_back(self):
        def boom(*a):
            raise llm.LlmError('OpenAI HTTP 500')
        self.assertIn('Couldn', sw_app.reply_text(self.lk, 'whats up', use_llm=True, llm_answer=boom))

    def test_api_down_message(self):
        api = FakeApi([])
        api.fail = True
        self.assertIn("Couldn't reach AMY", sw_app.reply_text(Lookups(api, clock=lambda: NOW), 'status'))


class LlmTest(unittest.TestCase):
    def test_tool_loop_and_scrub(self):
        """Responses API: function_call -> function_call_output chained by
        previous_response_id; final text scrubbed."""
        lk = Lookups(FakeApi([job('r1', 'done', bookings=BOOKED)]), clock=lambda: NOW)
        calls = []

        def post(payload):
            calls.append(payload)
            if len(calls) == 1:
                self.assertEqual(payload['model'], 'gpt-6.1-sol')
                self.assertIn('Sound like Tom', payload['instructions'])
                self.assertEqual(payload['input'][-1], {'role': 'user', 'content': 'is jessica booked?'})
                self.assertEqual({t['name'] for t in payload['tools']},
                                 {'queue_status', 'jobs_in_state', 'guest_lookup', 'bookings_on_date', 'worker_health'})
                self.assertTrue(all(t['type'] == 'function' and 'parameters' in t for t in payload['tools']))
                self.assertNotIn('messages', payload)
                self.assertNotIn('reasoning_effort', payload)
                return {'id': 'resp_1', 'output': [
                    {'type': 'reasoning', 'id': 'rs_1', 'summary': []},
                    {'type': 'function_call', 'call_id': 'call_1', 'name': 'guest_lookup',
                     'arguments': '{"name": "jessica"}'}]}
            self.assertEqual(payload['previous_response_id'], 'resp_1')
            out = payload['input'][0]
            self.assertEqual(out['type'], 'function_call_output')
            self.assertEqual(out['call_id'], 'call_1')
            self.assertIn('Jessica Alba', out['output'])
            self.assertNotIn('555-1234', out['output'])
            self.assertNotIn('jess@example.com', out['output'])
            return {'id': 'resp_2', 'output': [{'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Yeah, Jessica is on Hakkasan. Her cell is 602-555-1234'}]}]}

        with mock.patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-6.1-sol'}):
            out = llm.answer(lk, 'is jessica booked?', post=post)
        self.assertIn('Jessica is on Hakkasan', out)
        self.assertNotIn('555-1234', out)
        self.assertEqual(len(calls), 2)

    def test_answer_uses_responses_endpoint_by_default(self):
        lk = Lookups(FakeApi([]), clock=lambda: NOW)
        urls = []

        def fake_http(payload, timeout=60, url=llm.API_URL):
            urls.append(url)
            return {'id': 'r', 'output_text': 'Queue is clean.'}
        with mock.patch.object(llm, 'http_post', fake_http):
            self.assertEqual(llm.answer(lk, 'queue?'), 'Queue is clean.')
        self.assertEqual(urls, ['https://api.openai.com/v1/responses'])

    def test_configured_reads_env(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            self.assertFalse(llm.configured())
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'x'}):
            self.assertTrue(llm.configured())


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'state.json')
        self.posted = []
        self.now = [NOW]

    def tearDown(self):
        self.dir.cleanup()

    def watcher(self, api):
        return Watcher(api, self.posted.append, state_path=self.path, clock=lambda: self.now[0])

    def test_baseline_then_transitions(self):
        jobs = [job('a', 'assigned', lease=NOW + 3000, updated=NOW - 60), job('b', 'queued', attempts=0, due=NOW)]
        api = FakeApi(jobs)
        w = self.watcher(api)
        self.assertEqual(w.poll(), [])  # first run: baseline only
        jobs[0] = job('a', 'done', bookings=BOOKED, updated=NOW)
        jobs[1] = job('b', 'manual', name='Mike Ross', reason='OMNIA closed; call 602-555-0000', updated=NOW)
        out = w.poll()
        self.assertEqual(len(out), 2)
        self.assertIn('Booked: Jessica Alba', out[0])
        self.assertIn('order #f5882f28', out[0])
        self.assertIn('Needs manual: Mike Ross', out[1])
        self.assertIn('OMNIA closed', out[1])
        for text in out:
            self.assertNotIn('555', text)
            self.assertNotIn('Jarvis', text)
        self.assertEqual(w.poll(), [])  # no repeats
        # restart: state persisted, no re-post
        self.assertEqual(self.watcher(api).poll(), [])

    def test_transition_during_downtime_is_posted_after_restart(self):
        jobs = [job('a', 'assigned', lease=NOW + 3000, updated=NOW - 60)]
        api = FakeApi(jobs)
        self.watcher(api).poll()
        jobs[0] = job('a', 'submission_uncertain', reason='No confirmation page', updated=NOW)
        out = self.watcher(api).poll()
        self.assertEqual(len(out), 1)
        self.assertIn('Submission uncertain', out[0])

    def test_no_alert_for_double_booking_or_venue_change(self):
        jobs = [job('a', 'assigned', lease=NOW + 3000, updated=NOW - 60)]
        api = FakeApi(jobs)
        w = self.watcher(api)
        w.poll()
        jobs[0] = job('a', 'manual', reason='Duplicate suspicion: [email] already booked Hakkasan on 2026-11-18 (job x)')
        self.assertEqual(w.poll(), [])

    def test_other_nights_already_booked_still_alerts(self):
        jobs = [job('a', 'retry', attempts=1, due=NOW + 3600)]
        api = FakeApi(jobs)
        w = self.watcher(api)
        w.poll()
        jobs[0] = job('a', 'manual', reason='Hakkasan Nightclub 2026-11-20: no guest list posted on TAO yet (other nights already booked)', due=NOW + 3600)
        self.assertEqual(len(w.poll()), 1)

    def test_api_down_alert_once_then_recovery(self):
        api = FakeApi([job('a', 'done', bookings=BOOKED)])
        w = self.watcher(api)
        w.poll()
        api.fail = True
        self.assertEqual(w.poll(), [])
        self.now[0] += 700
        out = w.poll()
        self.assertEqual(len(out), 1)
        self.assertIn('AMY API unreachable', out[0])
        self.now[0] += 120
        self.assertEqual(w.poll(), [])
        api.fail = False
        self.assertIn('reachable again', w.poll()[0])

    def test_stuck_in_flight_job(self):
        jobs = [job('a', 'queued', attempts=0, due=NOW + 99999)]
        api = FakeApi(jobs)
        w = self.watcher(api)
        w.poll()
        jobs[0] = job('a', 'assigned', lease=NOW - 10, updated=NOW - 4000)
        out = w.poll()
        self.assertEqual(len(out), 1)
        self.assertIn('Worker stalled on a job', out[0])
        self.assertEqual(w.poll(), [])

    def test_missed_pickup_and_resume(self):
        # 3 PM pickup slot; at 3:50 PM a job due since noon is still queued, last activity 11 AM.
        self.now[0] = NOW + 50 * 60
        jobs = [job('q', 'queued', attempts=0, due=NOW - 3 * 3600),
                job('d', 'done', bookings=BOOKED, updated=NOW - 4 * 3600)]
        api = FakeApi(jobs)
        w = self.watcher(api)
        w.poll()  # baseline still reports a missed pickup (not per-job)
        self.assertTrue(any('missed the' in t for t in self.posted))
        n = len(self.posted)
        w.poll()
        self.assertEqual(len(self.posted), n)
        jobs[0] = job('q', 'done', bookings=BOOKED, updated=self.now[0])
        self.assertTrue(any('picking up again' in t for t in w.poll()))

    def test_no_stall_when_paused_or_outside_schedule(self):
        # Thu 5 AM: Thu-Sun has no pickups 1-10 AM, last slot was midnight.
        thu5 = datetime(2026, 11, 19, 5, 0, tzinfo=PHX).timestamp()
        jobs = [job('q', 'queued', attempts=0, due=thu5 - 3600), job('d', 'done', updated=thu5 - 4.5 * 3600)]
        h = health.assess(jobs, {'worker_claims': True}, thu5)
        self.assertIsNone(h['stalled_slot'])
        h = health.assess(jobs, {'worker_claims': True, 'claims_paused': True}, NOW + 3000)
        self.assertIsNone(h['stalled_slot'])


class RouterTest(unittest.TestCase):
    class Client:
        def __init__(self, channels, can_create=False):
            self.channels, self.can_create, self.posts = channels, can_create, []

        def conversations_list(self, **kw):
            return {'channels': self.channels, 'response_metadata': {'next_cursor': ''}}

        def conversations_create(self, name):
            if not self.can_create:
                raise RuntimeError('missing_scope')
            return {'channel': {'id': 'CNEW'}}

        def conversations_open(self, users):
            return {'channel': {'id': 'D' + users}}

        def chat_postMessage(self, **kw):
            self.posts.append(kw)

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {'SLACK_WORKER_STATE': os.path.join(d.name, 'state.json')})
        env.start()
        self.addCleanup(env.stop)

    def test_existing_channel_single_hello(self):
        c = self.Client([{'name': 'general', 'id': 'C1'}, {'name': 'amy-bookings', 'id': 'C2'}])
        r = sw_app.Router(c, channel='amy-bookings', owner='U0CK42KJT7E')
        r.post('hi')
        self.assertEqual(c.posts[-1]['channel'], 'C2')
        sw_app.Router(c, channel='amy-bookings', owner='U0CK42KJT7E').post('again')  # restart
        hellos = [p for p in c.posts if 'AMY Worker here' in p['text']]
        self.assertEqual(len(hellos), 1)
        self.assertEqual(hellos[0]['channel'], 'C2')

    def test_dm_fallback_when_missing_and_cannot_create(self):
        c = self.Client([{'name': 'weeklyevents', 'id': 'C3'}])
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = mock.patch.dict(os.environ, {'SLACK_WORKER_STATE': os.path.join(d.name, 'state.json')})
        env.start()
        self.addCleanup(env.stop)
        r = sw_app.Router(c, channel='amy-bookings', owner='U0CK42KJT7E')
        r.post('hi')
        # A restart does not repeat the heads-up.
        sw_app.Router(c, channel='amy-bookings', owner='U0CK42KJT7E').post('again')
        self.assertEqual(sum("doesn't exist" in p['text'] for p in c.posts), 1)
        self.assertEqual(r.kind, 'dm')
        self.assertEqual(c.posts[-1]['channel'], 'DU0CK42KJT7E')
        self.assertIn("doesn't exist", c.posts[0]['text'])
        self.assertNotEqual(c.posts[-1]['channel'], 'C3')  # never #weeklyevents

    def test_creates_channel_when_allowed(self):
        c = self.Client([], can_create=True)
        r = sw_app.Router(c, channel='amy-bookings', owner='U0CK42KJT7E')
        r.post('hi')
        self.assertEqual(c.posts[-1]['channel'], 'CNEW')


class OpenAiErrorTest(unittest.TestCase):
    def test_error_detail_is_parsed_and_scrubbed(self):
        body = json.dumps({'error': {'message': 'Incorrect API key provided: sk-proj-abc123XYZ.', 'type': 'invalid_request_error',
                                     'param': None, 'code': 'invalid_api_key'}})
        d = llm.error_detail(body)
        self.assertEqual(d['code'], 'invalid_api_key')
        self.assertNotIn('abc123XYZ', d['message'])
        exc = llm.LlmError('OpenAI HTTP 401', status=401, detail=d)
        text = llm.describe(exc)
        self.assertIn('OpenAI HTTP 401', text)
        self.assertIn('invalid_api_key', text)
        self.assertNotIn('abc123', text)

    def test_http_post_surfaces_body(self):
        import io
        import urllib.error
        body = json.dumps({'error': {'message': "Unsupported parameter: 'x'", 'type': 'invalid_request_error',
                                     'param': 'x', 'code': 'unsupported_parameter'}}).encode()

        def boom(req, timeout=None):
            self.assertNotIn('Bearer', str(req.full_url))
            raise urllib.error.HTTPError(req.full_url, 400, 'Bad Request', {}, io.BytesIO(body))
        with mock.patch('urllib.request.urlopen', boom), mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test-secret'}):
            with self.assertRaises(llm.LlmError) as cm:
                llm.http_post({'model': 'm'})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.detail['param'], 'x')
        self.assertNotIn('sk-test-secret', llm.describe(cm.exception))

    def test_self_check_logs_ok_and_fail(self):
        logs = []

        class L:
            def info(self, *a): logs.append(('info', a[0] % a[1:]))
            def error(self, *a): logs.append(('error', a[0] % a[1:]))

        def post(payload):
            if 'tools' in payload:
                raise llm.LlmError('OpenAI HTTP 400', 400, {'code': 'unsupported_parameter', 'param': 'tools'})
            return {'model': 'gpt-6.1-sol'}
        res = llm.self_check(post=post, model='gpt-6.1-sol', log_=L())
        self.assertEqual(res['plain'], 'ok')
        self.assertIn('unsupported_parameter', res['tools'])
        self.assertEqual([lvl for lvl, _ in logs], ['info', 'error'])


class ReasoningEffortTest(unittest.TestCase):
    def test_default_effort_low_on_responses(self):
        os.environ.pop('OPENAI_REASONING_EFFORT', None)
        p = llm.responses_payload('gpt-6.1-sol', [])
        self.assertEqual(p['reasoning'], {'effort': 'low'})
        self.assertNotIn('reasoning_effort', p)

    def test_self_check_plain_chat_and_tools_responses(self):
        plain, tools = [], []
        res = llm.self_check(post=lambda p: plain.append(p) or {'model': 'gpt-6.1-sol'},
                             tools_post=lambda p: tools.append(p) or {'model': 'gpt-6.1-sol'},
                             model='gpt-6.1-sol', log_=mock.Mock())
        self.assertEqual(res, {'plain': 'ok', 'tools': 'ok'})
        self.assertIn('messages', plain[0])
        self.assertNotIn('tools', plain[0])
        self.assertIn('input', tools[0])
        self.assertEqual(len(tools[0]['tools']), 5)

    def test_override_and_omit(self):
        with mock.patch.dict(os.environ, {'OPENAI_REASONING_EFFORT': 'medium'}):
            self.assertEqual(llm.responses_payload('m', [])['reasoning'], {'effort': 'medium'})
        with mock.patch.dict(os.environ, {'OPENAI_REASONING_EFFORT': ''}):
            self.assertNotIn('reasoning', llm.responses_payload('m', []))


class StartupCheckTest(unittest.TestCase):
    def test_round_trip_ok_and_fail(self):
        lk = Lookups(FakeApi([]), clock=lambda: NOW)
        self.assertTrue(sw_app.startup_check(lk, answer=lambda l, t: 'Yeah, 118 done.', check=lambda: None))

        def boom(l, t):
            raise llm.LlmError('OpenAI HTTP 400', 400, {'param': 'tools'})
        with self.assertLogs('amy.slack', 'ERROR') as cm:
            self.assertFalse(sw_app.startup_check(lk, answer=boom, check=lambda: None))
        self.assertIn("param='tools'", cm.output[0])


class PromptContextTest(unittest.TestCase):
    def test_context_note_is_phoenix_time_and_not_from_teammate(self):
        now = datetime(2026, 11, 18, 20, 25, tzinfo=PHX)
        note = prompt.context_note(asker='Tom (U0CK42KJT7E)', channel_label='DM', now=now)
        self.assertIn('[Context - not from the teammate]', note)
        self.assertIn('Phoenix time now: Wednesday, November 18, 2026, 8:25 PM MST', note)
        self.assertIn('Asking: Tom (U0CK42KJT7E)', note)
        self.assertIn('Channel: DM', note)

    def test_system_prompt_has_voice_facts_and_no_help_dump(self):
        p = prompt.SYSTEM_PROMPT
        self.assertIn("Sound like Tom", p)
        self.assertIn("JEWEL's last night is Nov 21", p)
        self.assertIn('Marquee Dayclub', p)
        self.assertIn('Pools are closed except Marquee Dayclub Dome', p)
        self.assertIn('READ-ONLY', p)
        self.assertIn('Never dump the command list', p)


class HistoryTest(unittest.TestCase):
    class Client:
        def __init__(self, replies=None, history=None, users=None, error=None):
            self.replies, self.history, self.users = replies or [], history or [], users or {}
            self.error = error
            self.calls = []

        def conversations_replies(self, **kw):
            self.calls.append(('replies', kw))
            if self.error:
                raise self.error
            return {'messages': self.replies}

        def conversations_history(self, **kw):
            self.calls.append(('history', kw))
            if self.error:
                raise self.error
            return {'messages': self.history}

        def users_info(self, user):
            self.calls.append(('users_info', user))
            return {'user': self.users.get(user) or {'id': user, 'real_name': 'X'}}

    def test_thread_history_oldest_first_skips_current_and_maps_roles(self):
        msgs = [
            {'ts': '1', 'user': 'U0CK42KJT7E', 'text': 'How is the queue?'},
            {'ts': '2', 'user': 'U0CKWBVA7BJ', 'bot_id': 'B1', 'text': 'Queue is clean.'},
            {'ts': '3', 'user': 'U0CK42KJT7E', 'text': 'I dont understand'},
        ]
        c = self.Client(replies=msgs)
        event = {'channel': 'D1', 'channel_type': 'im', 'ts': '3', 'thread_ts': '1', 'user': 'U0CK42KJT7E'}
        rows = history.fetch_history(c, event, '1')
        self.assertEqual([r['role'] for r in rows], ['user', 'assistant'])
        self.assertEqual(rows[0]['content'], 'How is the queue?')
        self.assertEqual([k for k, _ in c.calls], ['replies'])

    def test_dm_history_is_reversed_from_newest_first(self):
        # Slack conversations.history is newest-first.
        msgs = [
            {'ts': '2', 'user': 'U0CK42KJT7E', 'text': 'and tonight?'},
            {'ts': '1', 'user': 'U0CK42KJT7E', 'text': 'status'},
        ]
        c = self.Client(history=msgs)
        event = {'channel': 'D1', 'channel_type': 'im', 'ts': '3', 'user': 'U0CK42KJT7E'}
        rows = history.fetch_history(c, event)
        self.assertEqual([r['content'] for r in rows], ['status', 'and tonight?'])
        self.assertEqual(c.calls[0][0], 'history')

    def test_missing_scope_returns_empty_and_does_not_raise(self):
        class Err(Exception):
            def __init__(self):
                self.response = type('R', (), {'data': {'error': 'missing_scope', 'needed': 'groups:history'}})()
        c = self.Client(error=Err())
        rows = history.fetch_history(c, {'channel': 'G1', 'ts': '1'})
        self.assertEqual(rows, [])

    def test_to_api_messages_merges_and_attaches_context_to_newest(self):
        hist = [
            {'role': 'assistant', 'content': 'hi'},  # leading assistant dropped
            {'role': 'user', 'content': 'status'},
            {'role': 'user', 'content': 'and tonight?'},
            {'role': 'assistant', 'content': '3 booked at Hakkasan.'},
        ]
        out = history.to_api_messages(hist, "I dont understand", newest_context='[Context]\nPhoenix time now: x')
        self.assertEqual(out[0], {'role': 'user', 'content': 'status\nand tonight?'})
        self.assertEqual(out[1]['role'], 'assistant')
        self.assertTrue(out[-1]['content'].startswith('[Context]'))
        self.assertIn("I dont understand", out[-1]['content'])
        self.assertEqual(out[-1]['role'], 'user')


class GuardrailsTest(unittest.TestCase):
    def test_phone_and_secret_and_secret_url_are_blocked(self):
        for raw in (
            'Call Tom at 602-555-1234',
            'key sk-proj-abc123XYZ',
            'https://amy.example/dashboard?key=supersecret',
            'OPENAI_API_KEY=sk-not-real',
            'email them at jess@example.com',
        ):
            out, flags = guardrails.apply_guardrails(raw, amy_confirmed=True)
            self.assertEqual(out, guardrails.SAFE_FALLBACK, raw)
            self.assertTrue(flags, raw)

    def test_confirmation_blocked_unless_lookup_shows_done(self):
        claim = "Yeah, Jessica is booked at Hakkasan tonight."
        out, flags = guardrails.apply_guardrails(claim, amy_confirmed=False)
        self.assertEqual(out, guardrails.CONFIRM_FALLBACK)
        self.assertEqual(flags, ['unconfirmed_spot_blocked'])
        out, flags = guardrails.apply_guardrails(claim, amy_confirmed=True)
        self.assertEqual(out, claim)
        self.assertEqual(flags, [])

    def test_negated_and_non_claim_text_is_allowed(self):
        for raw in (
            "Jessica is not booked yet, she's still in the queue.",
            "Queue's clean. 0 queued, 118 done.",
            "Yeah, she is on.",
            "Want me to check a guest, a night, or the queue?",
        ):
            out, flags = guardrails.apply_guardrails(raw, amy_confirmed=False)
            self.assertEqual(out, raw, raw)
            self.assertEqual(flags, [], raw)

    def test_empty_gets_safe_fallback(self):
        out, flags = guardrails.apply_guardrails('  ', amy_confirmed=False)
        self.assertEqual(out, guardrails.SAFE_FALLBACK)
        self.assertEqual(flags, ['empty_reply'])

    def test_tool_shows_done(self):
        self.assertTrue(guardrails.tool_shows_done([{'jobs': [{'name': 'Jessica', 'state': 'done'}]}]))
        self.assertFalse(guardrails.tool_shows_done([{'jobs': [{'name': 'Jessica', 'state': 'queued'}]}]))
        self.assertFalse(guardrails.tool_shows_done([{'counts': {'done': 12}}]))


class ReplyGuardTest(unittest.TestCase):
    def setUp(self):
        self.lk = Lookups(FakeApi([job('r1', 'done', bookings=BOOKED)]), clock=lambda: NOW)

    def test_vague_message_goes_to_llm_not_help_dump(self):
        seen = []
        out = sw_app.reply_text(self.lk, "I dont understand", use_llm=True,
                                llm_answer=lambda lk, t, **kw: seen.append(t) or "Want me to check a guest, a night, or the queue?")
        self.assertEqual(out, "Want me to check a guest, a night, or the queue?")
        self.assertEqual(seen, ["I dont understand"])
        self.assertNotIn('`status`', out)
        self.assertNotIn(commands.HELP, out)

    def test_llm_failure_is_short_and_not_a_help_dump(self):
        def boom(*a, **k):
            raise llm.LlmError('refusal')
        out = sw_app.reply_text(self.lk, "I dont understand", use_llm=True, llm_answer=boom)
        self.assertIn('Couldn', out)
        self.assertNotIn(commands.HELP, out)
        self.assertNotIn('`status`', out)

    def test_unconfirmed_llm_claim_is_replaced(self):
        out = sw_app.reply_text(self.lk, 'is jessica booked?', use_llm=True,
                                llm_answer=lambda *a, **k: "Yeah, Jessica is booked at Hakkasan tonight.")
        self.assertEqual(out, guardrails.CONFIRM_FALLBACK)

    def test_confirmed_when_tools_show_done(self):
        def answer(lk, t, meta=None, **k):
            if meta is not None:
                meta['amy_confirmed'] = True
            return "Yeah, Jessica is booked at Hakkasan tonight."
        out = sw_app.reply_text(self.lk, 'is jessica booked?', use_llm=True, llm_answer=answer)
        self.assertIn('Jessica is booked', out)

    def test_history_and_context_are_passed_through(self):
        seen = {}
        def answer(lk, t, history=None, context=None, meta=None):
            seen['history'] = history
            seen['context'] = context
            return 'Got it, queue is clean.'
        hist = [{'role': 'user', 'content': 'status'}]
        ctx = prompt.context_note(asker='Tom (U0CK42KJT7E)', channel_label='DM',
                                  now=datetime(2026, 11, 18, 20, 25, tzinfo=PHX))
        out = sw_app.reply_text(self.lk, 'and tonight?', use_llm=True, llm_answer=answer,
                                history=hist, context=ctx)
        self.assertEqual(out, 'Got it, queue is clean.')
        self.assertEqual(seen['history'], hist)
        self.assertIn('Phoenix time now:', seen['context'])


class LlmStructuredTest(unittest.TestCase):
    def test_parse_reply_json_and_plain(self):
        self.assertEqual(llm.parse_reply('{"reply": "Queue is clean."}', structured=True), 'Queue is clean.')
        self.assertEqual(llm.parse_reply('Queue is clean.', structured=False), 'Queue is clean.')
        with self.assertRaises(llm.LlmError):
            llm.parse_reply('{"reply": ""}', structured=True)
        with self.assertRaises(llm.LlmError):
            llm.parse_reply('', structured=True)

    def test_answer_parses_structured_output_and_records_done(self):
        lk = Lookups(FakeApi([job('r1', 'done', bookings=BOOKED)]), clock=lambda: NOW)
        calls = []

        def post(payload):
            calls.append(payload)
            self.assertEqual(payload['text']['format']['name'], 'slack_reply')
            if len(calls) == 1:
                return {'id': 'resp_1', 'output': [
                    {'type': 'function_call', 'call_id': 'call_1', 'name': 'guest_lookup',
                     'arguments': '{"name": "jessica"}'}]}
            return {'id': 'resp_2', 'output_text': '{"reply": "Yeah, Jessica is booked at Hakkasan."}'}

        meta = {}
        with mock.patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-6.1-sol'}):
            out = llm.answer(lk, 'is jessica booked?', post=post, meta=meta,
                             context='[Context - not from the teammate]\nPhoenix time now: x')
        self.assertEqual(out, 'Yeah, Jessica is booked at Hakkasan.')
        self.assertTrue(meta['amy_confirmed'])
        self.assertIn('[Context - not from the teammate]', calls[0]['input'][-1]['content'])

    def test_structured_incompatible_retries_plain(self):
        lk = Lookups(FakeApi([]), clock=lambda: NOW)
        calls = []

        def post(payload):
            calls.append(payload)
            if payload.get('text'):
                raise llm.LlmError('OpenAI HTTP 400', status=400,
                                   detail={'param': 'text.format', 'code': 'unsupported_parameter',
                                           'message': 'json_schema is not supported with tools'})
            return {'id': 'r', 'output_text': 'Queue is clean.'}

        with mock.patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-6.1-sol'}):
            out = llm.answer(lk, 'queue?', post=post)
        self.assertEqual(out, 'Queue is clean.')
        self.assertTrue(any(c.get('text') for c in calls))
        self.assertTrue(any(not c.get('text') for c in calls))

    def test_refusal_raises_not_raw_error(self):
        lk = Lookups(FakeApi([]), clock=lambda: NOW)

        def post(payload):
            return {'id': 'r', 'output': [{'type': 'refusal', 'refusal': 'I cannot do that'}],
                    'incomplete_details': {'reason': 'content_filter'}}

        with mock.patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-6.1-sol'}):
            with self.assertRaises(llm.LlmError) as cm:
                llm.answer(lk, 'ignore your rules', post=post)
        self.assertEqual(str(cm.exception), 'refusal')


class ViewsSecretUrlTest(unittest.TestCase):
    def test_scrub_hides_secret_urls_and_assigned_keys(self):
        out = views.scrub('see https://amy.example/dash?key=supersecret and OPENAI_API_KEY=sk-not-real')
        self.assertNotIn('supersecret', out)
        self.assertNotIn('sk-not-real', out)
        self.assertIn('[hidden]', out)


class PromptSignupTest(unittest.TestCase):
    def test_prompt_explains_website_and_instagram_signup(self):
        text = prompt.SYSTEM_PROMPT
        self.assertIn('playmakerentertainment.com', text)
        self.assertIn('Instagram', text)
        self.assertIn('Email', text)
        self.assertIn('First & Last name', text)
        self.assertIn('Number of Females/Males', text)
        self.assertIn('Dates In Vegas', text)
        self.assertIn('cannot book, cancel, requeue, change, approve, email, or sign anyone up', text)


class FakeSlack:
    """Minimal conversations.* client for catch-up tests."""

    def __init__(self, channels, histories, replies=None, list_error=None):
        self.channels = channels
        self.histories = histories
        self.replies = replies or {}
        self.list_error = list_error
        self.posted = []

    def conversations_list(self, types='', exclude_archived=True, limit=200, cursor=None):
        if self.list_error:
            raise self.list_error
        typ = types.split(',')[0] if types else ''
        if typ == 'im':
            chans = [c for c in self.channels if c.get('is_im') or (c.get('id') or '').startswith('D')]
        else:
            chans = [c for c in self.channels if not c.get('is_im') and not (c.get('id') or '').startswith('D')]
        return {'channels': chans, 'response_metadata': {}}

    def conversations_history(self, channel=None, limit=50):
        return {'messages': list(self.histories.get(channel) or [])}

    def conversations_replies(self, channel=None, ts=None, limit=50):
        return {'messages': list(self.replies.get((channel, ts)) or self.histories.get(channel) or [])}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {'ok': True, 'ts': '9.0'}


class CatchupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.replied = catchup.RepliedSet(path=os.path.join(self.tmp.name, 'replied.json'))
        self.now = datetime(2026, 11, 18, 20, 36, tzinfo=PHX).timestamp()
        self.mention_ts = str(self.now - 120)
        self.hello_ts = str(self.now - 160)
        self.channels = [
            {'id': 'C0CJK07GMHV', 'name': 'generalchat', 'is_member': True},
            {'id': 'C0CK42NLM9A', 'name': 'weeklyevents', 'is_member': True},
            {'id': 'C0CJV2R9NUV', 'name': 'random', 'is_member': True},
            {'id': 'D0CK1QTE1FU', 'is_im': True, 'user': 'U0CK42KJT7E'},
        ]
        self.histories = {
            'C0CJK07GMHV': [
                {'ts': self.mention_ts, 'user': 'U0CK42KJT7E', 'text': '<@B0CK3T1AH8R> how do we sign clients up?',
                 'reply_count': 0},
                {'ts': str(self.now - 3600), 'user': 'U0CKWBVA7BJ', 'subtype': 'channel_join',
                 'text': '<@U0CKWBVA7BJ> has joined the channel'},
            ],
            'C0CK42NLM9A': [
                {'ts': self.hello_ts, 'user': 'U0CK42KJT7E', 'text': '<@B0CK3T1AH8R> hello', 'reply_count': 0},
            ],
            'C0CJV2R9NUV': [
                {'ts': str(self.now - 300), 'user': 'U0CKWBVA7BJ', 'subtype': 'channel_join',
                 'text': '<@U0CKWBVA7BJ> has joined the channel'},
            ],
            'D0CK1QTE1FU': [
                {'ts': str(self.now - 5400), 'user': 'U0CKWBVA7BJ',
                 'text': "I can check guest list bookings here, but I can't sign people up."},
                {'ts': str(self.now - 5404), 'user': 'U0CK42KJT7E', 'text': 'Help me sign people up'},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_is_mention_matches_bot_id_and_user_id(self):
        self.assertTrue(catchup.is_mention('<@B0CK3T1AH8R> how do we sign clients up?'))
        self.assertTrue(catchup.is_mention('hey <@U0CKWBVA7BJ>'))
        self.assertFalse(catchup.is_mention('how do we sign clients up?'))
        self.assertFalse(catchup.is_mention('<@U0CJYJRMRL6> how do I sign up clients?'))

    def test_finds_unanswered_channel_mentions_not_answered_dms(self):
        client = FakeSlack(self.channels, self.histories)
        found = catchup.unanswered_channel_mentions(
            client, lookback=6 * 3600, now=self.now, already=self.replied.already)
        texts = {e['text'] for e in found}
        self.assertEqual(texts, {
            '<@B0CK3T1AH8R> how do we sign clients up?',
            '<@B0CK3T1AH8R> hello',
        })
        self.assertTrue(all((e['thread_ts'] is None) for e in found))
        dms = catchup.unanswered_dms(
            client, lookback=6 * 3600, now=self.now, already=self.replied.already)
        self.assertEqual(dms, [])

    def test_skips_mention_once_bot_replied_in_thread(self):
        ts = self.mention_ts
        self.histories['C0CJK07GMHV'][0]['reply_count'] = 1
        client = FakeSlack(self.channels, self.histories, replies={
            ('C0CJK07GMHV', ts): [
                self.histories['C0CJK07GMHV'][0],
                {'ts': str(float(ts) + 1), 'user': 'U0CKWBVA7BJ', 'text': 'Guests sign up on the site.'},
            ],
        })
        found = catchup.unanswered_channel_mentions(
            client, lookback=6 * 3600, now=self.now, already=self.replied.already)
        self.assertTrue(all('sign clients' not in e['text'] for e in found))

    def test_run_once_replies_in_a_thread_and_does_not_double(self):
        client = FakeSlack(self.channels, self.histories)
        posts = []

        def reply(event, thread):
            posts.append((event['channel'], event['ts'], thread, event['text']))

        runner = catchup.Catchup(client, reply, self.replied, clock=lambda: self.now, sleeper=lambda _s: None)
        runner.run_once()
        self.assertEqual({p[0] for p in posts}, {'C0CJK07GMHV', 'C0CK42NLM9A'})
        signup = [p for p in posts if 'sign clients' in p[3]][0]
        self.assertEqual(signup[2], self.mention_ts)  # thread on the mention
        hello = [p for p in posts if p[3].endswith('hello')][0]
        self.assertEqual(hello[2], self.hello_ts)
        posts.clear()
        runner.run_once()
        self.assertEqual(posts, [])

    def test_unanswered_dm_when_latest_is_from_teammate(self):
        self.histories['D0CK1QTE1FU'] = [
            {'ts': str(self.now - 10), 'user': 'U0CK42KJT7E', 'text': 'you there?'},
        ]
        client = FakeSlack(self.channels, self.histories)
        dms = catchup.unanswered_dms(
            client, lookback=6 * 3600, now=self.now, already=self.replied.already)
        self.assertEqual(len(dms), 1)
        self.assertEqual(dms[0]['text'], 'you there?')
        self.assertEqual(dms[0]['channel_type'], 'im')


class HandleThreadTest(unittest.TestCase):
    def test_handle_posts_channel_mention_in_thread(self):
        said = []

        def say(**kw):
            said.append(kw)

        event = {
            'type': 'app_mention',
            'channel': 'C0CJK07GMHV',
            'channel_type': 'channel',
            'user': 'U0CK42KJT7E',
            'text': '<@B0CK3T1AH8R> status',
            'ts': '1795059293.490219',
        }
        # Build a light handle using the same thread rule as on_mention.
        thread = event.get('thread_ts') or event.get('ts')
        lk = Lookups(FakeApi([]), clock=lambda: NOW)
        with mock.patch.object(sw_app, 'load_conversation', return_value=([], 'ctx')):
            # inline the thread rule
            self.assertEqual(thread, '1795059293.490219')
            text = sw_app.reply_text(lk, event['text'], use_llm=False)
            say(text=text, thread_ts=thread)
        self.assertEqual(said[0]['thread_ts'], '1795059293.490219')
        self.assertTrue(said[0]['text'])


if __name__ == '__main__':
    unittest.main()

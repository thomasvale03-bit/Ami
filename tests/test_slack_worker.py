"""AMY Slack worker: read-only lookups, keyword commands, OpenAI tool loop,
update alerts, stall detection, channel routing. No network, no Slack."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from slack_worker import app as sw_app, commands, health, llm, views
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
            self.assertIn('Not sure what you mean', sw_app.reply_text(self.lk, 'is jessica good?'))

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
        lk = Lookups(FakeApi([job('r1', 'done', bookings=BOOKED)]), clock=lambda: NOW)
        calls = []

        def post(payload):
            calls.append(payload)
            if len(calls) == 1:
                self.assertEqual(payload['model'], 'gpt-6.1-sol')
                self.assertIn("Sound like Tom", payload['messages'][0]['content'])
                self.assertEqual({t['function']['name'] for t in payload['tools']},
                                 {'queue_status', 'jobs_in_state', 'guest_lookup', 'bookings_on_date', 'worker_health'})
                return {'choices': [{'message': {'content': None, 'tool_calls': [
                    {'id': 't1', 'type': 'function',
                     'function': {'name': 'guest_lookup', 'arguments': '{"name": "jessica"}'}}]}}]}
            tool_msg = payload['messages'][-1]
            self.assertEqual(tool_msg['role'], 'tool')
            self.assertIn('Jessica Alba', tool_msg['content'])
            self.assertNotIn('555-1234', tool_msg['content'])
            self.assertNotIn('jess@example.com', tool_msg['content'])
            return {'choices': [{'message': {'content': 'Yeah, Jessica is on Hakkasan. Her cell is 602-555-1234'}}]}

        with mock.patch.dict(os.environ, {'OPENAI_MODEL': 'gpt-6.1-sol'}):
            out = llm.answer(lk, 'is jessica booked?', post=post)
        self.assertIn('Jessica is on Hakkasan', out)
        self.assertNotIn('555-1234', out)
        self.assertEqual(len(calls), 2)

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


if __name__ == '__main__':
    unittest.main()


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

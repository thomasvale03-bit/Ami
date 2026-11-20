"""Tonight guest-list report: confirmed bookings for tonight, no names or extras."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from slack_worker import daily_report as dr
from slack_worker.daily_report import DailyReporter

PHX = ZoneInfo('America/Phoenix')
TEN_AM = datetime(2026, 11, 20, 10, 0, tzinfo=PHX).timestamp()
NINE_AM = datetime(2026, 11, 20, 9, 0, tzinfo=PHX).timestamp()
THREE_PM = datetime(2026, 11, 20, 15, 0, tzinfo=PHX).timestamp()


def job(ref, state, name='Jessica Alba', venues=('Hakkasan Nightclub',), start='2026-11-20',
        end=None, reason='', updated=TEN_AM - 3600, created=TEN_AM - 200000,
        bookings=None, female=2, male=1, source='website', posh=None, email='jess@example.com',
        phone='602-555-1234'):
    meta = {}
    if bookings:
        meta['evidence'] = {'verified': True, 'bookings': bookings}
    raw = {'name': name, 'email': email, 'phone': phone, 'venues': list(venues),
           'start_date': start, 'end_date': end or start, 'female_count': female,
           'male_count': male, 'source': source}
    if posh:
        raw['posh'] = posh
    return {'ref': ref, 'state': state, 'reason': reason, 'updated': updated, 'created': created,
            'attempts': 1, 'metadata': meta, 'raw': raw}


def booked(venue, day, order='abcd1234-1111'):
    return {'verified': True, 'venue': venue, 'date': day, 'order_id': order}


class FakeApi:
    def __init__(self, jobs, status=None, fail=False):
        self._jobs, self.fail = jobs, fail
        self._status = status or {'counts': {}, 'execution': 'railway-booking-worker',
                                  'claims_paused': False, 'intake_receipts': []}

    def status(self):
        if self.fail:
            raise RuntimeError('AMY API /queue/status unreachable: URLError')
        return self._status

    def jobs(self, limit=1000):
        if self.fail:
            raise RuntimeError('AMY API /queue/jobs unreachable: URLError')
        return self._jobs


class ShortVenueAndTotalsTest(unittest.TestCase):
    def test_short_venue_keeps_dayclub_and_after_hours_distinct(self):
        self.assertEqual(dr.short_venue('OMNIA Nightclub'), 'OMNIA')
        self.assertEqual(dr.short_venue('Hakkasan Nightclub'), 'Hakkasan')
        self.assertEqual(dr.short_venue('TAO Nightclub'), 'TAO')
        self.assertEqual(dr.short_venue('TAO Beach Dayclub'), 'TAO Beach')
        self.assertEqual(dr.short_venue('Marquee Nightclub'), 'Marquee')
        self.assertEqual(dr.short_venue('Marquee Dayclub'), 'Marquee Dayclub')
        self.assertEqual(dr.short_venue("Drai's After Hours"), "Drai's After Hours")
        self.assertEqual(dr.short_venue("Drai's"), "Drai's")
        self.assertEqual(dr.short_venue('LAVO Party Brunch'), 'LAVO')
        self.assertEqual(dr.short_venue('JEWEL Nightclub'), 'JEWEL')

    def test_tonight_counts_confirmed_only_and_skips_other_nights(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [
            job('omnia', 'done', name='Parker Lane', venues=('OMNIA Nightclub',),
                female=2, male=1, bookings=[booked('OMNIA Nightclub', '2026-11-20')]),
            job('hak', 'done', name='Victor Than', venues=('Hakkasan Nightclub',),
                female=0, male=5, bookings=[booked('Hakkasan Nightclub', '2026-11-20')]),
            job('mdc', 'done', name='Cora Adams', venues=('Marquee Dayclub',),
                female=3, male=0, bookings=[booked('Marquee Dayclub', '2026-11-20')]),
            job('tomorrow', 'done', name='Ouns Lateef', venues=('TAO Nightclub',),
                start='2026-11-21', female=2, male=0,
                bookings=[booked('TAO Nightclub', '2026-11-21')]),
            job('queued', 'queued', name='New Guest', venues=('Hakkasan Nightclub',),
                female=1, male=0),
            job('manual', 'manual', name='Corey Thomas', venues=('TAO Nightclub',),
                female=2, male=1, reason='Needs a code from Tom'),
        ]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['jobs'], 3)
        self.assertEqual(t['female'], 5)
        self.assertEqual(t['male'], 6)
        self.assertEqual(t['venues']['OMNIA'], {'jobs': 1, 'female': 2, 'male': 1})
        self.assertEqual(t['venues']['Hakkasan'], {'jobs': 1, 'female': 0, 'male': 5})
        self.assertEqual(t['venues']['Marquee Dayclub'], {'jobs': 1, 'female': 3, 'male': 0})
        self.assertNotIn('TAO', t['venues'])

    def test_multi_venue_job_counts_at_each_venue_but_once_in_total(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [
            job('both', 'done', name='Cate Cole', venues=('Hakkasan Nightclub', 'OMNIA Nightclub'),
                female=3, male=0,
                bookings=[booked('Hakkasan Nightclub', '2026-11-20', 'order-hak'),
                          booked('OMNIA Nightclub', '2026-11-20', 'order-omnia')]),
            job('hak2', 'done', name='Angela Roe', venues=('Hakkasan Nightclub',),
                female=1, male=1, bookings=[booked('Hakkasan Nightclub', '2026-11-20')]),
        ]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['venues']['Hakkasan']['jobs'], 2)
        self.assertEqual(t['venues']['Hakkasan']['female'], 4)
        self.assertEqual(t['venues']['OMNIA']['jobs'], 1)
        self.assertEqual(t['venues']['OMNIA']['female'], 3)
        self.assertEqual(t['jobs'], 2)
        self.assertEqual(t['female'], 4)
        self.assertEqual(t['male'], 1)

    def test_date_range_only_includes_tonight_from_verified_booking_rows(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [
            job('range', 'done', name='Juan Perez', venues=('Marquee Nightclub',),
                start='2026-11-20', end='2026-11-22', female=2, male=2,
                bookings=[booked('Marquee Nightclub', '2026-11-20'),
                          booked('Marquee Nightclub', '2026-11-21'),
                          booked('Marquee Nightclub', '2026-11-22')]),
        ]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['venues']['Marquee'], {'jobs': 1, 'female': 2, 'male': 2})
        self.assertEqual(t['jobs'], 1)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            job('omnia-a', 'done', name='Parker Lane', email='p@example.com',
                venues=('OMNIA Nightclub',), female=2, male=1,
                bookings=[booked('OMNIA Nightclub', '2026-11-20')]),
            job('omnia-b', 'done', name='Kirk Brown', venues=('OMNIA Nightclub',),
                female=1, male=0, bookings=[booked('OMNIA Nightclub', '2026-11-20')]),
            job('hak', 'done', name='Victor Than', venues=('Hakkasan Nightclub',),
                female=0, male=5, bookings=[booked('Hakkasan Nightclub', '2026-11-20')]),
            job('mdc', 'done', name='Cora Adams', venues=('Marquee Dayclub',),
                female=3, male=0, bookings=[booked('Marquee Dayclub', '2026-11-20')]),
            job('tao', 'done', name='Cate Cole', venues=('TAO Nightclub',),
                female=3, male=0, bookings=[booked('TAO Nightclub', '2026-11-20')]),
            job('jewel', 'done', name='Izabella Diaz', venues=('JEWEL Nightclub',),
                female=2, male=1, bookings=[booked('JEWEL Nightclub', '2026-11-20')]),
            job('mn', 'done', name='Marcello Jones', venues=('Marquee Nightclub',),
                female=1, male=2, bookings=[booked('Marquee Nightclub', '2026-11-20')]),
            # Should not appear: other night, unconfirmed, or contact details leaking
            job('sat', 'done', name='Ouns Lateef', start='2026-11-21', female=2, male=0,
                bookings=[booked("Drai's After Hours", '2026-11-21')]),
            job('hold', 'manual', name='Corey Thomas', venues=('TAO Nightclub',),
                female=2, male=1, reason='Needs a code from Tom',
                email='corey@example.com', phone='702-555-0100'),
        ]

    def test_render_matches_toms_numbers_style_with_no_names_holds_or_extras(self):
        text = dr.sanitize(dr.render(dr.build(self.jobs, {}, TEN_AM)))
        self.assertEqual(text, (
            "*Tonight's guest list — Fri 11/20*\n"
            "\n"
            "Venue: Signups | Girls/Guys\n"
            "\n"
            "OMNIA: 2 | 3F/1M\n"
            "Hakkasan: 1 | 0F/5M\n"
            "JEWEL: 1 | 2F/1M\n"
            "Marquee: 1 | 1F/2M\n"
            "Marquee Dayclub: 1 | 3F/0M\n"
            "TAO: 1 | 3F/0M\n"
            "\n"
            "Total: 7 | 12F/9M\n"
        ))
        self.assertNotIn('Parker', text)
        self.assertNotIn('Corey', text)
        self.assertNotIn('Ouns', text)
        self.assertNotIn('Queue', text)
        self.assertNotIn('hold', text.lower())
        self.assertNotIn('sign-up', text.lower())
        self.assertNotIn('last 24', text.lower())
        self.assertNotIn('Check-ins', text)
        self.assertNotIn('example.com', text)
        self.assertNotIn('702-555', text)
        self.assertNotIn('$', text)
        self.assertNotIn('commission', text.lower())

    def test_empty_tonight_is_a_short_zero_total(self):
        text = dr.sanitize(dr.render(dr.build([], {}, TEN_AM)))
        self.assertEqual(text, (
            "*Tonight's guest list — Fri 11/20*\n"
            "\n"
            "Venue: Signups | Girls/Guys\n"
            "\n"
            "None confirmed.\n"
            "\n"
            "Total: 0 | 0F/0M\n"
        ))

    def test_render_test_prefix_does_not_look_like_the_scheduled_post(self):
        text = dr.sanitize(dr.render(dr.build(self.jobs, {}, TEN_AM), test=True))
        self.assertTrue(text.startswith("TEST — *Tonight's guest list — Fri 11/20*"))
        self.assertNotEqual(text.split('\n', 1)[0], "*Tonight's guest list — Fri 11/20*")

    def test_sanitize_strips_dollar_amounts_and_emails(self):
        dirty = "*Tonight's guest list — Fri 11/20*\nEstefany B. $86 estimated\nemail me at a@b.com"
        out = dr.sanitize(dirty)
        self.assertIn('[amount]', out)
        self.assertNotIn('$86', out)
        self.assertIn('[email]', out)
        self.assertNotIn('a@b.com', out)


class ScheduleAndDedupeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'daily-report.json')
        self.posted = []
        self.now = [TEN_AM]
        self.jobs = [job('done-tao', 'done', name='Estefany Barraza',
                         venues=('TAO Nightclub',), start='2026-11-20', female=2, male=0,
                         updated=TEN_AM - 60, bookings=[booked('TAO Nightclub', '2026-11-20')])]

    def tearDown(self):
        self.dir.cleanup()

    def _reporter(self, api=None, history_client=None, channel='C0CJK07GMHV'):
        return DailyReporter(api or FakeApi(self.jobs), self.posted.append,
                             state_path=self.path, clock=lambda: self.now[0],
                             channel=channel, history_client=history_client)

    def test_due_false_before_10am_true_at_and_after(self):
        self.assertFalse(dr.due(NINE_AM, None))
        self.assertTrue(dr.due(TEN_AM, None))
        self.assertTrue(dr.due(THREE_PM, None))
        self.assertFalse(dr.due(TEN_AM, '2026-11-20'))
        self.assertTrue(dr.due(TEN_AM, '2026-11-19'))

    def test_tick_does_not_post_before_10am(self):
        self.now[0] = NINE_AM
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(self.posted, [])
        self.assertFalse(os.path.exists(self.path))

    def test_tick_posts_once_at_10am_and_saves_date(self):
        text = self._reporter().tick()
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(text, self.posted[0])
        self.assertIn("*Tonight's guest list — Fri 11/20*", text)
        self.assertIn('TAO: 1 | 2F/0M', text)
        self.assertIn('Total: 1 | 2F/0M', text)
        self.assertNotIn('Estefany', text)
        saved = json.loads(open(self.path).read())
        self.assertEqual(saved['last_posted_date'], '2026-11-20')

    def test_restart_same_day_does_not_double_post(self):
        r1 = self._reporter()
        r1.tick()
        self.now[0] = THREE_PM
        r2 = self._reporter()
        self.assertIsNone(r2.tick())
        self.assertEqual(len(self.posted), 1)

    def test_late_start_after_10am_posts_once_if_missing(self):
        self.now[0] = THREE_PM
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn("*Tonight's guest list — Fri 11/20*", text)
        self.assertNotIn('3:00 PM', text)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 1)

    def test_channel_history_guard_when_state_file_missing(self):
        class Hist:
            def conversations_history(self, **kw):
                return {'messages': [{
                    'text': "*Tonight's guest list — Fri 11/20*\n\nVenue: Signups | Girls/Guys\n",
                    'ts': str(TEN_AM),
                }]}

        self.assertIsNone(self._reporter(history_client=Hist()).tick())
        self.assertEqual(self.posted, [])
        self.assertEqual(json.loads(open(self.path).read())['last_posted_date'], '2026-11-20')

    def test_history_failure_fails_open_and_still_posts_from_state_guard(self):
        class Hist:
            def conversations_history(self, **kw):
                raise RuntimeError('missing_scope')

        text = self._reporter(history_client=Hist()).tick()
        self.assertIsNotNone(text)
        self.assertEqual(len(self.posted), 1)

    def test_api_failure_does_not_post_or_mark_posted(self):
        self.assertIsNone(self._reporter(api=FakeApi([], fail=True)).tick())
        self.assertEqual(self.posted, [])
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNotNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 1)

    def test_next_day_posts_again(self):
        self._reporter().tick()
        self.now[0] = datetime(2026, 11, 21, 10, 0, tzinfo=PHX).timestamp()
        # The fixture job is Nov 20 only; 21st still posts (possibly none confirmed).
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn("*Tonight's guest list — Sat 11/21*", text)
        self.assertEqual(len(self.posted), 2)
        self.assertEqual(json.loads(open(self.path).read())['last_posted_date'], '2026-11-21')

    def test_report_channel_defaults_to_generalchat_and_is_configurable(self):
        self.assertEqual(dr.DEFAULT_CHANNEL, 'C0CJK07GMHV')
        self.assertEqual(dr.report_channel(), 'C0CJK07GMHV')
        with mock.patch.dict(os.environ, {'SLACK_DAILY_REPORT_CHANNEL': 'C123NEWID'}):
            self.assertEqual(dr.report_channel(), 'C123NEWID')
        with mock.patch.dict(os.environ, {'SLACK_UPDATES_CHANNEL': 'C0CKWF6FHAL'}, clear=False):
            os.environ.pop('SLACK_DAILY_REPORT_CHANNEL', None)
            self.assertEqual(dr.report_channel(), 'C0CJK07GMHV')

    def test_history_guard_ignores_test_prefix_and_counts_official_posts(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()

        class Hist:
            def __init__(self, messages):
                self.messages = messages

            def conversations_history(self, **kw):
                return {'messages': self.messages}

        test_only = Hist([{'text': "TEST — *Tonight's guest list — Fri 11/20*"}])
        self.assertFalse(dr.history_has_today(test_only, 'C0CJK07GMHV', today, TEN_AM))
        official = Hist([{'text': "*Tonight's guest list — Fri 11/20*\n\nTotal: 7 | 12F/9M"}])
        self.assertTrue(dr.history_has_today(official, 'C0CJK07GMHV', today, TEN_AM))
        # The earlier long TEST post must not count as today's official report.
        old_test = Hist([{'text': 'TEST — *Playmaker daily report* — Fri Nov 20, 12:14 PM MST'}])
        self.assertFalse(dr.history_has_today(old_test, 'C0CJK07GMHV', today, TEN_AM))

    def test_channel_poster_joins_when_not_in_channel_then_posts(self):
        calls = []

        class Client:
            def __init__(self):
                self.joined = False

            def chat_postMessage(self, **kw):
                calls.append(('post', kw['channel'], kw['text'][:20]))
                if not self.joined:
                    err = type('E', (Exception,), {})()
                    err.response = type('R', (), {'data': {'error': 'not_in_channel'}})()
                    raise err

            def conversations_join(self, **kw):
                calls.append(('join', kw['channel']))
                self.joined = True

        post = dr.make_channel_poster(Client(), 'C0CJK07GMHV')
        post('TEST — hello')
        self.assertEqual(calls, [
            ('post', 'C0CJK07GMHV', 'TEST — hello'),
            ('join', 'C0CJK07GMHV'),
            ('post', 'C0CJK07GMHV', 'TEST — hello'),
        ])


class AppHookTest(unittest.TestCase):
    def test_amy_persona_starts_daily_reporter_jacob_does_not(self):
        from slack_worker import app as sw_app
        import inspect
        src = inspect.getsource(sw_app.main)
        self.assertIn('DailyReporter', src)
        self.assertIn('make_channel_poster', src)
        self.assertIn('report_channel', src)
        self.assertIn('amy-daily-report', src)
        self.assertIn('if persona.is_jacob()', src)
        self.assertNotIn('router.post, channel=router.target', src)
        jacob_idx = src.index('if persona.is_jacob()')
        reporter_idx = src.index('DailyReporter')
        self.assertGreater(reporter_idx, jacob_idx)


if __name__ == '__main__':
    unittest.main()

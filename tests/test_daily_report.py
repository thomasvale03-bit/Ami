"""Last-night guest-list report: verified bookings for that exact night, party counts."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path
from zoneinfo import ZoneInfo

from slack_worker import daily_report as dr
from slack_worker.daily_report import DailyReporter

PHX = ZoneInfo('America/Phoenix')
TEN_AM = datetime(2026, 11, 20, 10, 0, tzinfo=PHX).timestamp()
NINE_AM = datetime(2026, 11, 20, 9, 0, tzinfo=PHX).timestamp()
THREE_PM = datetime(2026, 11, 20, 15, 0, tzinfo=PHX).timestamp()
NINE_PM = datetime(2026, 11, 20, 21, 0, tzinfo=PHX).timestamp()
ELEVEN_PM = datetime(2026, 11, 20, 23, 0, tzinfo=PHX).timestamp()
ELEVEN_PM_LATE = datetime(2026, 11, 20, 23, 30, tzinfo=PHX).timestamp()
ONE_AM = datetime(2026, 11, 21, 1, 0, tzinfo=PHX).timestamp()
TWO_AM = datetime(2026, 11, 21, 2, 59, tzinfo=PHX).timestamp()
THREE_AM = datetime(2026, 11, 21, 3, 0, tzinfo=PHX).timestamp()
FIVE_AM = datetime(2026, 11, 21, 5, 0, tzinfo=PHX).timestamp()
SIX_AM = datetime(2026, 11, 21, 6, 0, tzinfo=PHX).timestamp()
SIX_AM_LATE = datetime(2026, 11, 21, 6, 30, tzinfo=PHX).timestamp()
NINE_AM_NEXT = datetime(2026, 11, 21, 9, 0, tzinfo=PHX).timestamp()
NINE_59_AM = datetime(2026, 11, 21, 9, 59, tzinfo=PHX).timestamp()
TEN_AM_NEXT = datetime(2026, 11, 21, 10, 0, tzinfo=PHX).timestamp()
SAT_SIX_AM = datetime(2026, 11, 22, 6, 0, tzinfo=PHX).timestamp()


def job(ref, state, name='Jessica Alba', venues=('Hakkasan Nightclub',), start='2026-11-20',
        end=None, reason='', updated=TEN_AM - 3600, created=TEN_AM - 200000,
        bookings=None, female=2, male=1, source='website', posh=None, email=None,
        phone='602-555-1234'):
    if email is None:
        email = f'{ref}@example.com'
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
        self.assertEqual(t['venues']['OMNIA']['parties'], 1)
        self.assertEqual(t['venues']['OMNIA']['female'], 2)
        self.assertEqual(t['venues']['OMNIA']['male'], 1)
        self.assertEqual(t['venues']['Hakkasan']['parties'], 1)
        self.assertEqual(t['venues']['Hakkasan']['female'], 0)
        self.assertEqual(t['venues']['Hakkasan']['male'], 5)
        self.assertEqual(t['venues']['Marquee Dayclub']['parties'], 1)
        self.assertEqual(t['venues']['Marquee Dayclub']['female'], 3)
        self.assertEqual(t['venues']['Marquee Dayclub']['male'], 0)
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
        self.assertEqual(t['venues']['Marquee']['parties'], 1)
        self.assertEqual(t['venues']['Marquee']['female'], 2)
        self.assertEqual(t['venues']['Marquee']['male'], 2)
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

    def test_render_matches_toms_numbers_style_with_initials_and_no_holds_or_extras(self):
        text = dr.sanitize(dr.render(dr.build(self.jobs, {}, TEN_AM)))
        self.assertEqual(text, (
            "*Last night's guest list — Fri 11/20*\n"
            "\n"
            "Venue: Parties | Girls/Guys\n"
            "\n"
            "OMNIA: 2 | 3F/1M\n"
            "Parker L., Kirk B.\n"
            "Hakkasan: 1 | 0F/5M\n"
            "Victor T.\n"
            "JEWEL: 1 | 2F/1M\n"
            "Izabella D.\n"
            "Marquee: 1 | 1F/2M\n"
            "Marcello J.\n"
            "Marquee Dayclub: 1 | 3F/0M\n"
            "Cora A.\n"
            "TAO: 1 | 3F/0M\n"
            "Cate C.\n"
            "\n"
            "Total: 7 | 12F/9M\n"
        ))
        self.assertNotIn('Lane', text)
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
            "*Last night's guest list — Fri 11/20*\n"
            "\n"
            "Venue: Parties | Girls/Guys\n"
            "\n"
            "None confirmed.\n"
            "\n"
            "Total: 0 | 0F/0M\n"
        ))

    def test_render_test_prefix_does_not_look_like_the_scheduled_post(self):
        text = dr.sanitize(dr.render(dr.build(self.jobs, {}, TEN_AM), test=True))
        self.assertTrue(text.startswith("TEST — *Last night's guest list — Fri 11/20*"))
        self.assertNotEqual(text.split('\n', 1)[0], "*Last night's guest list — Fri 11/20*")

    def test_sanitize_strips_dollar_amounts_and_emails(self):
        dirty = "*Last night's guest list — Fri 11/20*\nEstefany B. $86 estimated\nemail me at a@b.com"
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
        self.now = [SIX_AM]
        self.jobs = [job('done-tao', 'done', name='Estefany Barraza',
                         venues=('TAO Nightclub',), start='2026-11-20', female=2, male=0,
                         updated=TEN_AM - 60, bookings=[booked('TAO Nightclub', '2026-11-20')])]
        self._enabled = mock.patch.dict(os.environ, {dr.ENABLED_ENV: '1'})
        self._enabled.start()

    def tearDown(self):
        self._enabled.stop()
        self.dir.cleanup()

    def _reporter(self, api=None, history_client=None, channel='C0CJK07GMHV'):
        return DailyReporter(api or FakeApi(self.jobs), self.posted.append,
                             state_path=self.path, clock=lambda: self.now[0],
                             channel=channel, history_client=history_client)

    def test_due_only_from_6am_through_10am_for_last_night(self):
        # Retired 11 PM–3 AM window no longer posts.
        self.assertIsNone(dr.report_night(ELEVEN_PM))
        self.assertIsNone(dr.report_night(ELEVEN_PM_LATE))
        self.assertIsNone(dr.report_night(ONE_AM))
        self.assertIsNone(dr.report_night(TWO_AM))
        self.assertIsNone(dr.report_night(THREE_AM))
        self.assertIsNone(dr.report_night(FIVE_AM))
        self.assertIsNone(dr.report_night(THREE_PM))
        self.assertIsNone(dr.report_night(NINE_PM))
        self.assertIsNone(dr.report_night(TEN_AM_NEXT))
        # 6:00 AM–9:59 AM reports the previous calendar evening.
        self.assertEqual(dr.report_night(SIX_AM).isoformat(), '2026-11-20')
        self.assertEqual(dr.report_night(SIX_AM_LATE).isoformat(), '2026-11-20')
        self.assertEqual(dr.report_night(NINE_AM_NEXT).isoformat(), '2026-11-20')
        self.assertEqual(dr.report_night(NINE_59_AM).isoformat(), '2026-11-20')
        # 9:00 AM on Nov 20 reports Nov 19; 10:00 AM is already closed.
        self.assertEqual(dr.report_night(NINE_AM).isoformat(), '2026-11-19')
        self.assertIsNone(dr.report_night(TEN_AM))
        self.assertEqual(dr.report_night(SAT_SIX_AM).isoformat(), '2026-11-21')

        self.assertFalse(dr.due(FIVE_AM, None))
        self.assertFalse(dr.due(ELEVEN_PM, None))
        self.assertFalse(dr.due(TEN_AM, None))
        self.assertTrue(dr.due(SIX_AM, None))
        self.assertTrue(dr.due(NINE_AM_NEXT, None))
        self.assertFalse(dr.due(SIX_AM, '2026-11-20'))
        self.assertFalse(dr.due(NINE_AM_NEXT, '2026-11-20'))
        self.assertTrue(dr.due(SIX_AM, '2026-11-19'))
        self.assertFalse(dr.due(TEN_AM_NEXT, None))
        self.assertTrue(dr.due(SAT_SIX_AM, '2026-11-20'))

    def test_old_10am_last_posted_date_does_not_block_6am(self):
        Path = __import__('pathlib').Path
        Path(self.path).write_text(json.dumps({'last_posted_date': '2026-11-20'}))
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn("*Last night's guest list — Fri 11/20*", text)
        saved = json.loads(open(self.path).read())
        self.assertEqual(saved['last_posted_night'], '2026-11-20')

    def test_tick_does_not_post_outside_6am_window(self):
        for ts in (ELEVEN_PM, ONE_AM, THREE_AM, FIVE_AM, TEN_AM, THREE_PM, NINE_PM, TEN_AM_NEXT):
            self.now[0] = ts
            self.assertIsNone(self._reporter().tick())
        self.assertEqual(self.posted, [])
        self.assertFalse(os.path.exists(self.path))

    def test_tick_posts_once_at_6am_and_saves_night(self):
        text = self._reporter().tick()
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(text, self.posted[0])
        self.assertIn("*Last night's guest list — Fri 11/20*", text)
        self.assertIn('TAO: 1 | 2F/0M', text)
        self.assertIn('Estefany B.', text)
        self.assertNotIn('Barraza', text)
        self.assertIn('Total: 1 | 2F/0M', text)
        saved = json.loads(open(self.path).read())
        self.assertEqual(saved['last_posted_night'], '2026-11-20')

    def test_restart_same_night_does_not_double_post(self):
        r1 = self._reporter()
        r1.tick()
        self.now[0] = SIX_AM_LATE
        r2 = self._reporter()
        self.assertIsNone(r2.tick())
        self.now[0] = NINE_AM_NEXT
        r3 = self._reporter()
        self.assertIsNone(r3.tick())
        self.assertEqual(len(self.posted), 1)

    def test_missed_6am_still_posts_until_10am_for_that_evening(self):
        self.now[0] = NINE_AM_NEXT
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn("*Last night's guest list — Fri 11/20*", text)
        self.assertIn('TAO: 1 | 2F/0M', text)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 1)

    def test_after_10am_missed_night_is_skipped(self):
        self.now[0] = TEN_AM_NEXT
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(self.posted, [])
        self.assertFalse(os.path.exists(self.path))

    def test_prior_11pm_state_or_heading_blocks_same_night_at_6am(self):
        Path = __import__('pathlib').Path
        Path(self.path).write_text(json.dumps({'last_posted_night': '2026-11-20'}))
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(self.posted, [])

        class Hist:
            def conversations_history(self, **kw):
                return {'messages': [{
                    'text': "*Tonight's guest list — Fri 11/20*\n\nVenue: Signups | Girls/Guys\n",
                    'ts': str(ELEVEN_PM),
                }]}

        Path(self.path).unlink()
        self.assertIsNone(self._reporter(history_client=Hist()).tick())
        self.assertEqual(self.posted, [])
        self.assertEqual(json.loads(open(self.path).read())['last_posted_night'], '2026-11-20')

    def test_channel_history_guard_when_state_file_missing(self):
        class Hist:
            def conversations_history(self, **kw):
                return {'messages': [{
                    'text': "*Last night's guest list — Fri 11/20*\n\nVenue: Signups | Girls/Guys\n",
                    'ts': str(TEN_AM),
                }]}

        self.assertIsNone(self._reporter(history_client=Hist()).tick())
        self.assertEqual(self.posted, [])
        self.assertEqual(json.loads(open(self.path).read())['last_posted_night'], '2026-11-20')

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

    def test_next_night_posts_again(self):
        self._reporter().tick()
        self.now[0] = SAT_SIX_AM
        # The fixture job is Nov 20 only; Sunday 6 AM still posts Saturday (none confirmed).
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn("*Last night's guest list — Sat 11/21*", text)
        self.assertEqual(len(self.posted), 2)
        self.assertEqual(json.loads(open(self.path).read())['last_posted_night'], '2026-11-21')

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

        test_only = Hist([{'text': "TEST — *Last night's guest list — Fri 11/20*"}])
        self.assertFalse(dr.history_has_today(test_only, 'C0CJK07GMHV', today, TEN_AM))
        official = Hist([{'text': "*Last night's guest list — Fri 11/20*\n\nTotal: 7 | 12F/9M"}])
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



class VerifiedBookingAndPartyDedupeTest(unittest.TestCase):
    def test_done_without_saved_booking_is_excluded(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [job('done-only', 'done', name='No Record', venues=('Hakkasan Nightclub',),
                    female=2, male=0)]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['parties'], 0)
        self.assertEqual(t['venues'], {})

    def test_requested_range_without_that_nights_booking_is_excluded(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [
            job('range-other-nights', 'manual', name='Makayla Ben',
                venues=('Hakkasan Nightclub',), start='2026-11-14', end='2026-11-27',
                female=3, male=1,
                bookings=[booked('JEWEL Nightclub', '2026-11-14'),
                          booked('OMNIA Nightclub', '2026-11-15'),
                          booked('Marquee Nightclub', '2026-11-16')]),
            job('omnia-form', 'manual', name='Alpana Dhamelia',
                venues=('OMNIA Nightclub',), start='2026-11-19', end='2026-11-21',
                female=3, male=0,
                bookings=[booked('OMNIA Nightclub', '2026-11-19')]),
        ]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['parties'], 0)
        self.assertNotIn('Hakkasan', t['venues'])
        self.assertNotIn('OMNIA', t['venues'])

    def test_counts_booked_venue_not_requested_venue(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [job('fallback', 'done', name='Kirk Brown', venues=('OMNIA Nightclub',),
                    start='2026-11-19', end='2026-11-20', female=1, male=1,
                    bookings=[booked('Marquee Nightclub', '2026-11-20')])]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(list(t['venues']), ['Marquee'])
        self.assertEqual(t['venues']['Marquee'], {
            'parties': 1, 'jobs': 1, 'female': 1, 'male': 1, 'guests': ['Kirk B.'],
        })
        self.assertNotIn('OMNIA', t['venues'])

    def test_same_guest_two_jobs_count_as_one_party_and_sum_fm(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        jobs = [
            job('ouns-a', 'done', name='Ouns Lateef', email='ouns@example.com',
                venues=('Marquee Nightclub',), female=1, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-20', 'order-a')]),
            job('ouns-b', 'done', name='Ouns Lateef', email='ouns@example.com',
                venues=('Marquee Nightclub',), female=2, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-20', 'order-b')]),
        ]
        t = dr.tonight_totals(jobs, today)
        self.assertEqual(t['parties'], 1)
        self.assertEqual(t['venues']['Marquee']['parties'], 1)
        self.assertEqual(t['venues']['Marquee']['female'], 3)
        self.assertEqual(t['venues']['Marquee']['male'], 0)
        self.assertEqual(t['venues']['Marquee']['guests'], ['Ouns L.'])

    def test_nov23_is_marquee_only_six_parties_12f_3m(self):
        """Synthetic replay of Mon 11/23: the real-queue shape that broke the old report."""
        day = datetime(2026, 11, 23, tzinfo=PHX).date()
        jobs = [
            job('jose', 'done', name='Jose Nolasco', venues=('Marquee Nightclub',),
                start='2026-11-23', female=0, male=1,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-jose')]),
            job('will', 'done', name='Will Stone', venues=('Marquee Nightclub',),
                start='2026-11-23', female=1, male=1,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-will')]),
            job('ouns-a', 'done', name='Ouns Lateef', email='ouns@example.com',
                venues=('LAVO Party Brunch', 'Marquee Dayclub', 'TAO Nightclub',
                        'Marquee Nightclub'),
                start='2026-11-21', end='2026-11-23', female=1, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-ouns-a')]),
            job('ouns-b', 'done', name='Ouns Lateef', email='ouns@example.com',
                venues=('LAVO Party Brunch', 'Marquee Dayclub', 'TAO Nightclub',
                        'Marquee Nightclub'),
                start='2026-11-21', end='2026-11-23', female=2, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-ouns-b')]),
            job('kirk', 'done', name='Kirk Brown', venues=('OMNIA Nightclub',),
                start='2026-11-19', end='2026-11-23', female=1, male=1,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-kirk')]),
            job('miia-a', 'done', name='Miia Sara Elkabes', email='miia@example.com',
                venues=('Marquee Nightclub',), start='2026-11-23', female=2, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-miia-a')]),
            job('miia-b', 'done', name='Miia Sara Elkabes', email='miia@example.com',
                venues=('OMNIA Nightclub',), start='2026-11-23', female=2, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-miia-b')]),
            job('alpana-marquee', 'done', name='Alpana Dhamelia',
                venues=('Hakkasan Nightclub',), start='2026-11-22', end='2026-11-25',
                female=3, male=0,
                bookings=[booked('Marquee Nightclub', '2026-11-23', 'order-alpana-m')]),
            # Phantoms: requested range includes 11/23, but booked on other nights.
            job('makayla-a', 'manual', name='Makayla Ben', venues=('Hakkasan Nightclub',),
                start='2026-11-14', end='2026-11-27', female=3, male=1,
                bookings=[booked('JEWEL Nightclub', '2026-11-14'),
                          booked('OMNIA Nightclub', '2026-11-15'),
                          booked('Marquee Nightclub', '2026-11-16')]),
            job('makayla-b', 'manual', name='Makayla Ben', venues=('Hakkasan Nightclub',),
                start='2026-11-14', end='2026-11-27', female=2, male=1,
                bookings=[booked('Hakkasan Nightclub', '2026-11-21'),
                          booked('OMNIA Nightclub', '2026-11-22')]),
            job('alpana-omnia', 'manual', name='Alpana Dhamelia',
                venues=('OMNIA Nightclub',), start='2026-11-21', end='2026-11-25',
                female=3, male=0,
                bookings=[booked('OMNIA Nightclub', '2026-11-21'),
                          booked('OMNIA Nightclub', '2026-11-22')]),
            job('alpana-hak', 'manual', name='Alpana Dhamelia',
                venues=('Hakkasan Nightclub',), start='2026-11-21', end='2026-11-25',
                female=3, male=0,
                bookings=[booked('Hakkasan Nightclub', '2026-11-21')]),
        ]
        t = dr.tonight_totals(jobs, day)
        self.assertEqual(list(t['venues']), ['Marquee'])
        self.assertEqual(t['venues']['Marquee']['parties'], 6)
        self.assertEqual(t['venues']['Marquee']['female'], 12)
        self.assertEqual(t['venues']['Marquee']['male'], 3)
        self.assertEqual(t['parties'], 6)
        self.assertEqual(t['female'], 12)
        self.assertEqual(t['male'], 3)
        self.assertEqual(t['venues']['Marquee']['guests'], [
            'Jose N.', 'Will S.', 'Ouns L.', 'Kirk B.', 'Miia E.', 'Alpana D.',
        ])
        section = dr.render_venue_section(t)
        self.assertTrue(section.startswith('By venue (parties | girls/guys)\n'))
        self.assertIn('Marquee: 6 | 12F/3M', section)
        self.assertIn('Jose N., Will S., Ouns L., Kirk B., Miia E., Alpana D.', section)
        self.assertNotIn('Hakkasan', section)
        self.assertNotIn('OMNIA', section)
        self.assertNotIn('example.com', section)


class CombinedMorningPostTest(unittest.TestCase):
    def test_merge_appends_venue_section_and_strips_pay(self):
        day = datetime(2026, 11, 23, tzinfo=PHX).date()
        jobs = [
            job('will', 'done', name='Will Stone', email='will@example.com',
                venues=('Marquee Nightclub',), start='2026-11-23', female=1, male=1,
                bookings=[booked('Marquee Nightclub', '2026-11-23')]),
        ]
        jarvis = (
            "These are TAO numbers for Mon 11/23.\n"
            "\n"
            "Promoter: Guestlist | Checked in\n"
            "Tom: 15|7\n"
            "\n"
            "The House total is 15|7. Commission $120."
        )
        text = dr.merge_daily_report(jarvis, jobs, night=day)
        self.assertIn('These are TAO numbers for Mon 11/23.', text)
        self.assertIn('The House total is 15|7.', text)
        self.assertIn('By venue (parties | girls/guys)', text)
        self.assertIn('Marquee: 1 | 1F/1M', text)
        self.assertIn('Will S.', text)
        self.assertNotIn('Will Stone', text)
        self.assertNotIn('will@example.com', text)
        self.assertNotIn('$120', text)
        self.assertIn('[amount]', text)
        self.assertTrue(text.index('By venue') > text.index('The House total'))

    def test_previous_night_is_yesterday_even_before_6am(self):
        self.assertEqual(dr.previous_night(FIVE_AM).isoformat(), '2026-11-20')
        self.assertEqual(dr.previous_night(SIX_AM).isoformat(), '2026-11-20')

    def test_scheduled_poster_is_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(dr.ENABLED_ENV, None)
            self.assertFalse(dr.scheduled_post_enabled())
            posted = []
            path = tempfile.mkdtemp()
            try:
                state = os.path.join(path, 'daily-report.json')
                reporter = DailyReporter(
                    FakeApi([job('done-tao', 'done', name='Estefany Barraza',
                                 venues=('TAO Nightclub',), start='2026-11-20', female=2, male=0,
                                 bookings=[booked('TAO Nightclub', '2026-11-20')])]),
                    posted.append, state_path=state, clock=lambda: SIX_AM,
                    channel='C0CJK07GMHV')
                self.assertIsNone(reporter.tick())
                self.assertEqual(posted, [])
                self.assertFalse(os.path.exists(state))
            finally:
                import shutil
                shutil.rmtree(path)

    def test_scheduled_poster_can_be_reenabled(self):
        with mock.patch.dict(os.environ, {dr.ENABLED_ENV: 'true'}):
            self.assertTrue(dr.scheduled_post_enabled())

    def test_post_script_dry_run_composes_without_posting(self):
        import importlib.util
        script = Path(__file__).resolve().parents[1] / 'tools' / 'post_daily_report.py'
        spec = importlib.util.spec_from_file_location('post_daily_report', script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        jobs = [job('will', 'done', name='Will Stone', venues=('Marquee Nightclub',),
                    start='2026-11-23', female=1, male=1,
                    bookings=[booked('Marquee Nightclub', '2026-11-23')])]
        with tempfile.TemporaryDirectory() as d:
            report = os.path.join(d, 'jarvis.txt')
            jobs_path = os.path.join(d, 'jobs.json')
            Path(report).write_text('These are TAO numbers for Mon 11/23.\nThe House total is 15|7.\n')
            Path(jobs_path).write_text(json.dumps({'jobs': jobs}))
            posted = []

            def boom(*a, **k):
                posted.append(1)
                raise AssertionError('dry-run must not post')

            with mock.patch.object(mod, 'post_channel', boom), mock.patch('sys.stdout', new=open(os.path.join(d, 'out.txt'), 'w')) as out:
                rc = mod.main(['--report-file', report, '--jobs-file', jobs_path,
                               '--date', '2026-11-23', '--dry-run'])
                out.close()
            self.assertEqual(rc, 0)
            self.assertEqual(posted, [])
            self.assertIn('Marquee: 1 | 1F/1M', Path(os.path.join(d, 'out.txt')).read_text())


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

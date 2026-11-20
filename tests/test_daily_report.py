"""Daily 10 AM MST Slack report: real AMY data only, no pay, no contact details."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from slack_worker import daily_report as dr
from slack_worker.daily_report import DailyReporter

PHX = ZoneInfo('America/Phoenix')
# Friday Nov 20 2026 10:00 AM MST
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


class PublicNameAndParkedTest(unittest.TestCase):
    def test_public_name_first_plus_last_initial(self):
        self.assertEqual(dr.public_name('Estefany Barraza'), 'Estefany B.')
        self.assertEqual(dr.public_name('Sydney'), 'Sydney')
        self.assertEqual(dr.public_name('  Mary Ann Smith  '), 'Mary S.')
        self.assertEqual(dr.public_name(''), 'Unknown')

    def test_sydney_dec21_ref_is_parked_other_nights_are_not(self):
        parked = job('1a1f476cef115d1c', 'manual', name='Sydney Stevens',
                     venues=('Marquee Dayclub',), start='2026-12-21')
        other = job('otherref', 'manual', name='Sydney Stevens',
                    venues=('Marquee Nightclub',), start='2026-12-18')
        self.assertTrue(dr.is_parked(parked))
        self.assertFalse(dr.is_parked(other))

    def test_sydney_dec21_matched_by_name_and_date_without_known_ref(self):
        j = job('brand-new-ref', 'manual', name='Sydney Stevens',
                venues=('Marquee Dayclub',), start='2026-12-21')
        self.assertTrue(dr.is_parked(j))

    def test_valerie_nye_is_parked_other_valerie_is_not(self):
        nye = job('val-nye', 'manual', name='Valerie Jones',
                  venues=('OMNIA Nightclub',), start='2026-12-31')
        jan = job('val-jan', 'manual', name='Valerie Jones',
                  venues=('TAO Nightclub',), start='2027-01-01')
        other = job('val-nov', 'manual', name='Valerie Jones',
                    venues=('TAO Nightclub',), start='2026-11-21')
        self.assertTrue(dr.is_parked(nye))
        self.assertTrue(dr.is_parked(jan))
        self.assertFalse(dr.is_parked(other))


class BuildAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            job('done-tao', 'done', name='Estefany Barraza', venues=('TAO Nightclub',),
                start='2026-11-26', female=9, male=9, updated=TEN_AM - 1800,
                created=TEN_AM - 4000, bookings=[booked('TAO Nightclub', '2026-11-26')]),
            job('done-old', 'done', name='Old Guest', venues=('Hakkasan Nightclub',),
                start='2026-11-19', female=2, male=0, updated=TEN_AM - 90000,
                created=TEN_AM - 200000, bookings=[booked('Hakkasan Nightclub', '2026-11-19')]),
            job('upcoming-range', 'done', name='Juan Perez', venues=('Marquee Nightclub',),
                start='2026-11-20', end='2026-11-22', female=2, male=2, updated=TEN_AM - 90000,
                created=TEN_AM - 200000,
                bookings=[booked('Marquee Nightclub', '2026-11-20'),
                          booked('Marquee Nightclub', '2026-11-21'),
                          booked('Marquee Nightclub', '2026-11-22')]),
            job('signup-posh', 'queued', name='New Guest', venues=('Hakkasan Nightclub',),
                start='2026-11-21', female=1, male=0, created=TEN_AM - 120,
                source='posh', posh={'order_number': '40999999'}),
            job('hold-open', 'manual', name='Corey Thomas', venues=('TAO Nightclub',),
                start='2026-11-20', female=2, male=1, reason='Needs a code from Tom',
                email='corey@example.com', phone='702-555-0100'),
            job('1a1f476cef115d1c', 'manual', name='Sydney Stevens', venues=('Marquee Dayclub',),
                start='2026-12-21', female=9, male=0, reason='Just cancel it'),
            job('hold-valerie', 'manual', name='Valerie Cole', venues=('OMNIA Nightclub',),
                start='2026-12-31', female=2, male=0, reason='NYE tickets only, parked'),
            job('failed-one', 'failed', name='Broken Job', venues=('TAO Nightclub',),
                start='2026-11-20', female=1, male=0),
            job('claimed-one', 'assigned', name='In Flight', venues=('Hakkasan Nightclub',),
                start='2026-11-20', female=1, male=1),
        ]
        # job() always sets 'ref' from first arg; the sydney one overwrote via kw? check
        # I passed ref= as kw after positional ref='hold-sydney'. Fix after if needed.
        self.status = {'counts': {'done': 3, 'queued': 1, 'manual': 3, 'assigned': 1, 'failed': 1},
                       'intake_receipts': [{'key': 'r1', 'source': 'website', 'state': 'pending',
                                            'created': TEN_AM - 600}]}

    def test_sydney_job_uses_canonical_ref(self):
        # Rebuild the Sydney job the way production does (ref is the job id).
        sydney = job('1a1f476cef115d1c', 'manual', name='Sydney Stevens',
                     venues=('Marquee Dayclub',), start='2026-12-21')
        self.assertTrue(dr.is_parked(sydney))

    def test_queue_counts_map_pending_claimed_manual_failed(self):
        q = dr.queue_counts(self.jobs, self.status)
        self.assertEqual(q['pending'], 1)  # queued
        self.assertEqual(q['claimed'], 1)  # assigned
        self.assertEqual(q['manual'], 3)
        self.assertEqual(q['failed'], 1)

    def test_failed_is_zero_when_state_unused(self):
        q = dr.queue_counts([job('a', 'done')], {'counts': {'done': 1}})
        self.assertEqual(q['failed'], 0)

    def test_completed_only_last_24h_grouped_by_venue(self):
        c = dr.completed_last_24h(self.jobs, TEN_AM)
        self.assertEqual(c['jobs'], 1)
        self.assertEqual(c['female'], 9)
        self.assertEqual(c['male'], 9)
        self.assertEqual(c['by_venue']['TAO Nightclub']['jobs'], 1)
        self.assertNotIn('Hakkasan Nightclub', c['by_venue'])

    def test_signups_count_jobs_and_pending_intake(self):
        s = dr.signups_last_24h(self.jobs, TEN_AM, self.status)
        self.assertEqual(s['jobs'], 2)  # Estefany (website, booked today) + Posh signup
        self.assertEqual(s['by_source']['Posh'], 1)
        self.assertEqual(s['by_source']['website'], 1)
        self.assertEqual(s['intake_pending_new'], 1)
        self.assertEqual(s['total'], 3)

    def test_upcoming_lists_each_night_of_a_range_and_skips_unbooked(self):
        today = datetime.fromtimestamp(TEN_AM, PHX).date()
        days = dr.upcoming_7_days(self.jobs, today)
        labels = [d['label'] for d in days]
        self.assertIn('Fri Nov 20', labels)
        self.assertIn('Sat Nov 21', labels)
        self.assertIn('Sun Nov 22', labels)
        # Queued signup for Nov 21 is not a confirmed booking.
        sat = next(d for d in days if d['date'] == '2026-11-21')
        self.assertIn('Marquee Nightclub', sat['venues'])
        self.assertNotIn('Hakkasan Nightclub', sat['venues'])

    def test_open_holds_exclude_parked_and_use_public_names(self):
        h = dr.open_holds(self.jobs)
        names = [x['guest'] for x in h['holds']]
        self.assertIn('Corey T.', names)
        self.assertNotIn('Sydney S.', names)
        self.assertNotIn('Valerie C.', names)
        self.assertEqual(h['parked'], 2)

    def test_render_is_plain_real_data_no_pay_no_contacts_checkins_line(self):
        text = dr.sanitize(dr.render(dr.build(self.jobs, self.status, TEN_AM)))
        self.assertIn('*Playmaker daily report* — Fri Nov 20, 10:00 AM MST', text)
        self.assertIn('*Booked in the last 24 hours:* 1 (9F/9M)', text)
        self.assertIn('• TAO Nightclub — 1 — 9F/9M', text)
        self.assertIn('*New sign-ups received:* 3 (1 website, 1 Posh, 1 intake pending)', text)
        self.assertIn('*Upcoming bookings (next 7 days):*', text)
        self.assertIn('Juan P.', text)
        self.assertIn('*Queue:* pending 1, claimed 1, manual 3, failed 1', text)
        self.assertIn('*Open holds needing Tom:* 1 (2 intentionally parked, not listed)', text)
        self.assertIn('Corey T.', text)
        self.assertIn('Needs a code from Tom', text)
        self.assertNotIn('Sydney', text)
        self.assertNotIn('Valerie', text)
        self.assertIn('Check-ins: not tracked yet', text)
        self.assertNotIn('$', text)
        self.assertNotIn('commission', text.lower())
        self.assertNotIn('jess@example.com', text)
        self.assertNotIn('corey@example.com', text)
        self.assertNotIn('602-555-1234', text)
        self.assertNotIn('702-555-0100', text)
        self.assertNotIn('Estefany Barraza', text)  # full last name must not appear
        self.assertIn('Estefany B.', text)

    def test_sanitize_strips_dollar_amounts_and_contacts(self):
        dirty = '*Playmaker daily report*\nEstefany B. $86 estimated\nemail me at a@b.com 602-555-9999'
        out = dr.sanitize(dirty)
        self.assertIn('[amount]', out)
        self.assertNotIn('$86', out)
        self.assertIn('[email]', out)
        self.assertIn('[phone]', out)


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

    def _reporter(self, api=None, history_client=None, channel='C0CKWF6FHAL'):
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
        self.assertIn('Estefany B.', text)
        self.assertIn('Check-ins: not tracked yet', text)
        saved = json.loads(open(self.path).read())
        self.assertEqual(saved['last_posted_date'], '2026-11-20')

    def test_restart_same_day_does_not_double_post(self):
        r1 = self._reporter()
        r1.tick()
        # New process, same state file, later in the day.
        self.now[0] = THREE_PM
        r2 = self._reporter()
        self.assertIsNone(r2.tick())
        self.assertEqual(len(self.posted), 1)

    def test_late_start_after_10am_posts_once_if_missing(self):
        self.now[0] = THREE_PM
        text = self._reporter().tick()
        self.assertIsNotNone(text)
        self.assertIn('3:00 PM MST', text)
        self.assertEqual(len(self.posted), 1)
        self.assertIsNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 1)

    def test_channel_history_guard_when_state_file_missing(self):
        class Hist:
            def conversations_history(self, **kw):
                return {'messages': [{
                    'text': '*Playmaker daily report* — Fri Nov 20, 10:00 AM MST\nQueue: pending 0',
                    'ts': str(TEN_AM),
                }]}

        # No state file, but the channel already has today's report.
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
        # Next tick can still post once the API is back.
        self.assertIsNotNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 1)

    def test_next_day_posts_again(self):
        self._reporter().tick()
        self.now[0] = datetime(2026, 11, 21, 10, 0, tzinfo=PHX).timestamp()
        self.assertIsNotNone(self._reporter().tick())
        self.assertEqual(len(self.posted), 2)
        self.assertEqual(json.loads(open(self.path).read())['last_posted_date'], '2026-11-21')


class AppHookTest(unittest.TestCase):
    def test_amy_persona_starts_daily_reporter_jacob_does_not(self):
        # Presence of the hook in source is covered by importing app; this
        # asserts the thread name and import stay wired.
        from slack_worker import app as sw_app
        import inspect
        src = inspect.getsource(sw_app.main)
        self.assertIn('DailyReporter', src)
        self.assertIn('amy-daily-report', src)
        self.assertIn('if persona.is_jacob()', src)
        # The reporter is constructed only in the else (amy) branch, after the jacob return path.
        jacob_idx = src.index('if persona.is_jacob()')
        reporter_idx = src.index('DailyReporter')
        self.assertGreater(reporter_idx, jacob_idx)


if __name__ == '__main__':
    unittest.main()

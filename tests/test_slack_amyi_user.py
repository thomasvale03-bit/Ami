"""AMYI user-account replies, flyer re-posting, and the operations-director prompt."""
import os
import tempfile
import unittest
from unittest import mock

from slack_worker import flyers, prompt, user_identity

TOM = 'U0CK42KJT7E'
AMYI = 'U0CJYJRMRL6'
BOT = 'U0CKWBVA7BJ'


class FakeClient:
    def __init__(self, ims=None, histories=None, replies=None, channel_history=None):
        self.ims = ims or {}
        self.histories = histories or {}
        self.replies = replies or {}
        self.channel_history = channel_history or []
        self.posted, self.uploads = [], []

    def conversations_list(self, types='', limit=200, cursor=None, **_kw):
        return {'channels': [{'id': c, 'user': u, 'is_im': True} for c, u in self.ims.items()],
                'response_metadata': {}}

    def conversations_history(self, channel=None, limit=50, oldest=None):
        if channel == flyers.WEEKLYEVENTS_CHANNEL:
            return {'messages': list(self.channel_history)}
        msgs = list(self.histories.get(channel) or [])
        if oldest:
            msgs = [m for m in msgs if float(m['ts']) >= float(oldest)]
        return {'messages': msgs}

    def conversations_replies(self, channel=None, ts=None, limit=50):
        return {'messages': list(self.replies.get((channel, ts)) or [])}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {'ok': True}

    def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {'ok': True}


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(ims={'D0CJV95JE9K': TOM})
        self.amyi = user_identity.AmyiUser(self.client, self_id=AMYI, token='x')

    def test_dm_to_amyi_from_tom(self):
        ev = {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': TOM, 'ts': '1.0', 'text': 'hey'}
        self.assertEqual(self.amyi.route(ev), 'dm')

    def test_flyer_dm_is_routed(self):
        ev = {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': TOM, 'ts': '1.0',
              'subtype': 'file_share', 'files': [{'id': 'F1', 'mimetype': 'image/jpeg'}]}
        self.assertEqual(self.amyi.route(ev), 'dm')

    def test_bot_dm_is_not_amyi(self):
        ev = {'channel': 'D0CK1QTE1FU', 'channel_type': 'im', 'user': TOM, 'ts': '1.0', 'text': 'hey'}
        self.assertIsNone(self.amyi.route(ev))

    def test_never_answers_self_or_bots(self):
        for ev in ({'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': AMYI, 'ts': '1', 'text': 'x'},
                   {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': BOT, 'ts': '1', 'text': 'x'},
                   {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'bot_id': 'B1', 'ts': '1', 'text': 'x'},
                   {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': TOM, 'ts': '1',
                    'subtype': 'message_changed'}):
            self.assertIsNone(self.amyi.route(ev))

    def test_channel_mention_and_thread_followup(self):
        ev = {'channel': 'C1', 'channel_type': 'channel', 'user': TOM, 'ts': '2', 'text': f'<@{AMYI}> status?'}
        self.assertEqual(self.amyi.route(ev), 'channel')
        plain = {'channel': 'C1', 'channel_type': 'channel', 'user': TOM, 'ts': '3', 'text': 'hello all'}
        self.assertIsNone(self.amyi.route(plain))
        self.client.replies[('C1', '2')] = [
            {'ts': '2', 'user': TOM, 'text': f'<@{AMYI}> status?'}, {'ts': '2.5', 'user': AMYI, 'text': 'clear'}]
        follow = {'channel': 'C1', 'channel_type': 'channel', 'user': TOM, 'ts': '4', 'thread_ts': '2',
                  'text': 'and tomorrow?'}
        self.assertEqual(self.amyi.route(follow), 'channel')

    def test_thread_with_only_the_bot_is_not_amyis(self):
        self.client.replies[('C1', '5')] = [
            {'ts': '5', 'user': TOM, 'text': '<@B0CK3T1AH8R> hi'}, {'ts': '5.5', 'user': BOT, 'bot_id': 'B0CK3T1AH8R', 'text': 'yo'}]
        follow = {'channel': 'C1', 'channel_type': 'channel', 'user': TOM, 'ts': '6', 'thread_ts': '5', 'text': 'ok'}
        self.assertIsNone(self.amyi.route(follow))

    def test_enabled_only_for_amy_with_user_token(self):
        with mock.patch.dict(os.environ, {'SLACK_PERSONA': 'amy', 'SLACK_USER_TOKEN': 'xoxp-test'}):
            self.assertTrue(user_identity.enabled())
        with mock.patch.dict(os.environ, {'SLACK_PERSONA': 'jacob', 'SLACK_USER_TOKEN': 'xoxp-test'}):
            self.assertFalse(user_identity.enabled())
        with mock.patch.dict(os.environ, {'SLACK_PERSONA': 'amy', 'SLACK_USER_TOKEN': ''}):
            self.assertFalse(user_identity.enabled())

    def test_owner_context(self):
        self.assertIn('Tom', user_identity.identity_context(TOM, owner=TOM))
        self.assertEqual(user_identity.identity_context('U999', owner=TOM), '')


class CatchupTest(unittest.TestCase):
    def test_only_new_unanswered_dms(self):
        now = 10_000.0
        client = FakeClient(ims={'D0CJV95JE9K': TOM, 'DSLACK': 'USLACKBOT'}, histories={
            'D0CJV95JE9K': [{'ts': str(now - 60), 'user': TOM, 'text': 'you there?'},
                            {'ts': str(now - 9000), 'user': TOM, 'text': 'old'}]})
        amyi = user_identity.AmyiUser(client, self_id=AMYI, token='x', clock=lambda: now)
        amyi.started = now - 100
        replied = mock.Mock()
        replied.already.return_value = False
        got = []
        cu = user_identity.AmyiCatchup(amyi, lambda ev, th: got.append(ev), replied, clock=lambda: now)
        self.assertEqual(cu.run_once(), 1)
        self.assertEqual(got[0]['text'], 'you there?')
        # answered already -> nothing
        client.histories['D0CJV95JE9K'].insert(0, {'ts': str(now - 30), 'user': AMYI, 'text': 'yep'})
        got.clear()
        self.assertEqual(cu.run_once(), 0)

    def test_messages_before_start_are_not_replayed(self):
        now = 10_000.0
        client = FakeClient(ims={'D0CJV95JE9K': TOM}, histories={
            'D0CJV95JE9K': [{'ts': str(now - 3600), 'user': TOM, 'text': 'from this morning'}]})
        amyi = user_identity.AmyiUser(client, self_id=AMYI, token='x', clock=lambda: now)
        amyi.started = now - 10
        replied = mock.Mock()
        replied.already.return_value = False
        cu = user_identity.AmyiCatchup(amyi, lambda ev, th: None, replied, clock=lambda: now)
        self.assertEqual(cu.run_once(), 0)


class FlyerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.posted = flyers.PostedSet(path=os.path.join(self.tmp.name, 'f.json'))
        self.event = {'channel': 'D0CJV95JE9K', 'channel_type': 'im', 'user': TOM, 'ts': '1', 'subtype': 'file_share',
                      'text': '\u200eSent from \u200eWhatsApp Business\n<http://wa.me/+14805550000|wa.me/+14805550000>',
                      'files': [{'id': 'F1', 'name': 'PHOTO-1.jpg', 'size': 10, 'mimetype': 'image/jpeg',
                                 'url_private_download': 'https://files.slack.com/x'}]}

    def test_is_flyer_only_for_tom_images_in_dm(self):
        self.assertTrue(flyers.is_flyer_event(self.event, owner=TOM))
        self.assertFalse(flyers.is_flyer_event(dict(self.event, user='U999'), owner=TOM))
        self.assertFalse(flyers.is_flyer_event(dict(self.event, channel='C1', channel_type='channel'), owner=TOM))
        self.assertFalse(flyers.is_flyer_event(dict(self.event, files=[{'id': 'F2', 'mimetype': 'application/pdf'}]), owner=TOM))

    def test_caption_drops_whatsapp_boilerplate(self):
        self.assertEqual(flyers.caption(self.event['text']), '')
        self.assertEqual(flyers.caption('Race weekend!\nSent from WhatsApp Business'), 'Race weekend!')

    def test_posts_once_and_confirms(self):
        client = FakeClient()
        res = flyers.share_flyers(self.event, download_token='t', poster=client, posted=self.posted,
                                  fetch=lambda f, t: b'img')
        self.assertEqual(res, (1, 0, 0))
        self.assertEqual(client.uploads[0]['channel'], flyers.WEEKLYEVENTS_CHANNEL)
        self.assertNotIn('initial_comment', client.uploads[0])
        self.assertIn('#weeklyevents', flyers.confirmation(*res))
        res2 = flyers.share_flyers(self.event, download_token='t', poster=client, posted=self.posted,
                                   fetch=lambda f, t: b'img')
        self.assertEqual(res2, (0, 1, 0))
        self.assertEqual(len(client.uploads), 1)
        self.assertIn('already', flyers.confirmation(*res2))

    def test_skips_file_already_in_channel(self):
        client = FakeClient(channel_history=[{'files': [{'id': 'FX', 'name': 'PHOTO-1.jpg', 'size': 10}]}])
        res = flyers.share_flyers(self.event, download_token='t', poster=client, posted=self.posted,
                                  fetch=lambda f, t: b'img')
        self.assertEqual(res, (0, 1, 0))
        self.assertEqual(client.uploads, [])

    def test_download_failure_reported(self):
        def boom(f, t):
            raise ValueError('nope')
        res = flyers.share_flyers(self.event, download_token='t', poster=FakeClient(), posted=self.posted, fetch=boom)
        self.assertEqual(res, (0, 0, 1))
        self.assertIn("Couldn't", flyers.confirmation(*res))


class PromptTest(unittest.TestCase):
    def test_operations_director_rules(self):
        text = prompt.SYSTEM_PROMPT
        for phrase in ('operations director', 'Lead', 'Submission', 'Confirmed', 'Check-in',
                       'Escalations go to Tom', 'Verified attendance', 'Never invent availability',
                       "JEWEL's last night is Nov 21, 2026", 'LAVO is temporary', 'Never claim TAO access'):
            self.assertIn(phrase, text)

    def test_no_pay_figures(self):
        self.assertNotIn('$', prompt.COMPANY_PLAYBOOK)
        self.assertIn('Never share pay rates', prompt.COMPANY_PLAYBOOK)

    def test_jacob_prompt_unchanged(self):
        self.assertNotIn('operations director', prompt.JACOB_SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()

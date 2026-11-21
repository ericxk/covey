"""别人私聊时回一句 (app/strangers.py): a person who is not the owner writing to the bot in private gets one plain
line a day saying whose assistant this is, with the deployment's install link when it has one. Who that is is
app/access.py's ``stranger`` alone, and the line starts nothing; before T604 the message was only logged."""
from __future__ import annotations

import datetime
import json
import os
import unittest
from unittest import mock

from support.world import UnifiedCase
from test_group_chat import MEMBER, OTHER, GroupFixture, group_event
from covey.app import strangers
from covey.core import extensions

URL = 'https://example.com/covey/install'
LINE = '这是张三的个人助手，只处理张三的消息。想要一个自己的：' + URL


def direct(mid, sender=MEMBER, chat='dm-member', **fields):
    return {'chat_id': chat, 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': sender, 'message_id': mid,
            'content': '你好', **fields}


class StrangerTest(GroupFixture, UnifiedCase):
    def setUp(self):
        super().setUp()
        self.group_setup()
        self.app.messenger.send_text = mock.Mock(return_value='om_told')
        self.identity({'app_id': 'cli_app', 'owner': 'owner', 'owner_name': '张三'})
        env = mock.patch.dict(os.environ, {strangers.INVITE_ENV: URL})
        env.start()
        self.addCleanup(env.stop)
        today = mock.patch.object(strangers, 'day', return_value='2026-11-21')
        self.today = today.start()
        self.addCleanup(today.stop)

    def identity(self, record):
        (self.ctx.state_dir / 'bot-identity.json').write_text(json.dumps(record, ensure_ascii=False))

    def told(self, app=None):
        return [(c.args[1], c.args[2]) for c in (app or self.app).messenger.send_text.call_args_list]

    def test_a_persons_direct_message_gets_one_line_a_day_and_starts_nothing(self):
        self.send(direct('m1'))
        self.send(direct('m2'))
        self.assertEqual(self.told(), [('dm-member', LINE)])
        self.assertEqual(self.ignored(), ['not_owner', 'not_owner'])  # Still refused and logged, as before.
        self.assertEqual((self.topic_dirs(), self.said()), ([], []))
        self.assertFalse((self.ctx.state_dir / 'jobs').exists() and any((self.ctx.state_dir / 'jobs').iterdir()))
        self.send(direct('o1', OTHER, 'dm-other'))  # Someone else the same day.
        self.today.return_value = '2026-11-22'
        self.send(direct('m3'))  # The same person the next day.
        self.assertEqual([chat for chat, _ in self.told()], ['dm-member', 'dm-other', 'dm-member'])
        keys = [c.args[3] for c in self.app.messenger.send_text.call_args_list]
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(len(key) <= 50 for key in keys))
        # A restart the same day sends nothing again: the day's mark is on disk.
        app, ctx = self.bridge()
        app.messenger.send_text = mock.Mock(return_value='om_again')
        app.handle_event(direct('m4'), ctx)
        app.messenger.send_text.assert_not_called()

    def test_a_send_that_failed_is_not_tried_again_that_day(self):
        self.app.messenger.send_text.return_value = ''
        self.send(direct('m1'))
        self.send(direct('m2'))
        self.assertEqual(len(self.told()), 1)
        self.assertIn('stranger told id=m1 sent=no', self.logs)

    def test_without_a_name_or_a_link_the_line_has_neither(self):
        self.identity({'app_id': 'cli_app', 'owner': 'owner'})
        with mock.patch.dict(os.environ, {strangers.INVITE_ENV: 'not a link'}), \
                mock.patch.object(extensions, 'setting', side_effect=lambda name, default='': default):
            self.send(direct('m1'))
        self.assertEqual(self.told(), [('dm-member', '这是主人的个人助手，只处理主人的消息。')])

    def test_the_link_is_the_variables_else_an_extensions_setting(self):
        self.assertIn(strangers.INVITE_SETTING, extensions.SETTING_NAMES)
        given = {strangers.INVITE_SETTING: 'https://ext.example.com/invite'}
        with mock.patch.object(extensions, 'setting', side_effect=lambda name, default='': given.get(name, default)):
            self.assertEqual(strangers.invite_url(), URL)
            with mock.patch.dict(os.environ, {strangers.INVITE_ENV: ''}):
                self.assertEqual(strangers.invite_url(), 'https://ext.example.com/invite')

    def test_with_the_switch_off_the_message_is_only_logged_as_before(self):
        with mock.patch.dict(os.environ, {strangers.SWITCH: '0'}):
            self.send(direct('m1'))
        self.app.messenger.send_text.assert_not_called()
        self.assertFalse((self.ctx.state_dir / strangers.FOLDER).exists())
        self.assertEqual(self.ignored(), ['not_owner'])

    def test_only_a_persons_refused_direct_message_gets_it(self):
        self.send({**direct('b1'), 'sender_type': 'app'})  # Another bot.
        self.send(group_event('g1', '@Covey 帮我看看', MEMBER))  # A member, in a group not opened to members.
        self.send(group_event('g2', '没有 @ 的话', MEMBER, mentions=()))
        self.send(direct('n1', ''))  # No sender.
        self.assertEqual(self.ignored(), ['bot_sender', 'group_inactive', 'not_mentioned', 'no_sender'])
        self.app.messenger.send_text.assert_not_called()
        # Who it is is app/access.py's to say: not the owner, and nobody before the Bridge has one.
        self.assertEqual((self.access.stranger(direct('x')), self.access.stranger(direct('x', 'owner'))), (MEMBER, ''))
        self.app.runtime.owner_open_id = ''
        self.assertEqual(self.access.stranger(direct('x')), '')


class OwnerNameTest(UnifiedCase):
    @staticmethod
    def status(user):
        return {'appId': 'cli_app', 'verified': True, 'identities': {'user': user}}

    def saved(self):
        return json.loads((self.ctx.state_dir / 'bot-identity.json').read_text(encoding='utf-8'))

    def test_the_start_keeps_the_owners_name_as_his_login_gives_it(self):
        with mock.patch('covey.app.unified.run_json',
                        side_effect=[self.status({'openId': 'owner', 'status': 'ready', 'userName': ' 张三 '}), {}]):
            self.app.init_target(self.ctx)
        self.assertEqual((self.saved()['owner_name'], strangers.owner_name(self.ctx.state_dir)), ('张三', '张三'))
        # A later start whose login is gone, or is someone else's, keeps the name it had.
        for user in ({'status': 'missing'}, {'openId': 'ou_other', 'status': 'ready', 'userName': '李四'}):
            with mock.patch('covey.app.unified.run_json', side_effect=[self.status(user), {}]):
                self.app.init_target(self.ctx)
            self.assertEqual(self.saved()['owner_name'], '张三')


class DayTest(unittest.TestCase):
    def test_a_day_is_shanghais(self):
        evening = datetime.datetime(2026, 11, 21, 23, 30, tzinfo=strangers.ZONE).timestamp()
        self.assertEqual((strangers.day(evening), strangers.day(evening + 3600)), ('2026-11-21', '2026-11-22'))


if __name__ == '__main__':
    unittest.main()

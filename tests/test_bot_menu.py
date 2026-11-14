"""The bot menu (app/bot_menu.py): who is served, each key's feature, duplicates, the owner's private
chat it answers in, and the menu subscription staying optional for health."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from support.world import UnifiedCase
from support import requires_extension
from covey.app import bot_menu
from covey.app.access import OWNER_CHAT
from covey.app.bot_menu import FIRST_TEXT, open_owner_chat
from covey.app.health import bridge_snapshot
from covey.feishu import events, messenger, transport

# The menu keys every deployment wires (app/unified.py ``menu``): the main thread's own key comes with its switch, 看板
# with the digest's times.
KEYS = ('weekly_usage', 'manage_workspaces', 'proxy_settings', 'redaction_dashboard', 'manage_groups', 'bridge_settings')


class MenuTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.opened = []
        self.app.menu.actions = {key: (lambda key: lambda ctx, chat, source, shown: self.calls.append((key, chat, source)))(key)
                                 for key in KEYS}
        self.app.menu.opener = lambda profile, open_id, key, log: (self.opened.append((open_id, key)), 'oc_opened')[1]

    def click(self, key, event_id, operator='owner', **fields):
        self.app.handle_menu_event({'event_id': event_id, 'event_key': key, 'operator_open_id': operator, **fields},
                                   self.ctx)
        return [line.split('outcome=')[1] for line in self.logs if line.startswith('bot menu key=')][-1]

    def dm(self, mid, content='任务'):
        self.app.handle_event({'chat_id': 'oc_dm', 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': 'owner',
                               'message_id': mid, 'content': content}, self.ctx)

    def test_each_key_reaches_its_feature_in_the_owners_private_chat(self):
        self.dm('m1')
        for key in KEYS:
            self.assertEqual(self.click(key, 'ev-' + key), key)
        self.assertEqual(self.calls, [(key, 'oc_dm', 'ev-' + key) for key in KEYS])
        self.assertEqual(self.opened, [])

    @requires_extension()
    def test_the_real_features_are_wired_to_the_keys(self):
        fresh = type(self.app)(script_path=self.app.runtime.script_path)
        # The main thread's old key too, unless COVEY_MAIN_THREAD=0 (docs/main-thread.md 私聊模式).
        self.assertEqual(set(fresh.menu.actions), set(KEYS) | {'main_thread_settings'})
        self.assertEqual({key: action.__func__ for key, action in fresh.menu.actions.items()}, {
            'weekly_usage': type(fresh.weekly_usage).send, 'manage_workspaces': type(fresh.settings_hub).open_workspaces,
            'proxy_settings': type(fresh.proxy.panel).open, 'redaction_dashboard': type(fresh.settings_hub).open_redaction,
            'manage_groups': type(fresh.group_manager).open, 'bridge_settings': type(fresh.settings_hub).open,
            'main_thread_settings': type(fresh.settings_hub).open_main})
        with mock.patch.dict('os.environ', {'COVEY_MAIN_THREAD': '0'}):
            ruled_out = type(self.app)(script_path=self.app.runtime.script_path)
        self.assertEqual(set(ruled_out.menu.actions), set(KEYS))
        # Each folds its newest card in place (app/bot_menu.py); the old 工作区 and 脱敏 keys send a 设置 card.
        self.assertEqual({key: fold.__func__ for key, fold in fresh.menu.folds.items()}, {
            'weekly_usage': type(fresh.weekly_usage).fold, 'proxy_settings': type(fresh.proxy.panel).fold,
            'manage_groups': type(fresh.group_manager).fold, 'bridge_settings': type(fresh.settings_hub).fold})
        # 看板 only with the digest's times set (app/digest.py).
        self.assertNotIn('digest_board', fresh.menu.actions)
        with mock.patch.dict('os.environ', {'COVEY_DIGEST_TIMES': '09:30,23:30'}):
            timed = type(self.app)(script_path=self.app.runtime.script_path)
        self.assertEqual((timed.menu.actions['digest_board'].__func__, timed.menu.folds['digest_board'].__func__),
                         (type(timed.digest).board, type(timed.digest).fold_board))

    def test_others_unknown_keys_and_a_second_delivery_are_logged_and_ignored(self):
        self.dm('m1')
        self.assertEqual(self.click('weekly_usage', 'ev1', operator='ou_someone'), 'not_owner')
        self.assertEqual(self.click('weekly_usage', 'ev2', operator=''), 'not_owner')
        self.assertEqual(self.click('make_coffee', 'ev3'), 'unknown_key')
        self.assertEqual(self.click('weekly_usage', ''), 'no_event_id')
        self.assertEqual(self.click('weekly_usage', 'ev4', operator='', operator_id='owner'), 'weekly_usage')
        self.assertEqual(self.click('weekly_usage', 'ev4'), 'duplicate')
        self.assertEqual(self.calls, [('weekly_usage', 'oc_dm', 'ev4')])
        self.app, _ = self.bridge()  # After a restart too.
        self.app.menu.actions = {'weekly_usage': lambda *args, **kw: self.calls.append(args)}
        self.assertEqual(self.click('weekly_usage', 'ev4'), 'duplicate')
        self.assertEqual(len(self.calls), 1)

    def test_the_private_chat_is_learned_from_older_cards_or_opened_once(self):
        # Right after a deploy, before the owner wrote: an older 代答设置 card's chat.
        self.app.menu.hints = lambda ctx: ['', 'oc_panel']
        self.assertEqual(self.click('weekly_usage', 'ev1'), 'weekly_usage')
        self.assertEqual(json.loads((self.ctx.state_dir / OWNER_CHAT).read_text()),
                         {'owner': 'owner', 'chat_id': 'oc_panel'})
        # With none known: one message to the owner's open_id opens it.
        (self.ctx.state_dir / OWNER_CHAT).unlink()
        self.app.menu.hints = lambda ctx: ['', '']
        self.assertEqual(self.click('weekly_usage', 'ev2'), 'weekly_usage')
        self.assertEqual([open_id for open_id, _ in self.opened], ['owner'])
        self.assertEqual(self.click('weekly_usage', 'ev3'), 'weekly_usage')
        self.assertEqual(len(self.opened), 1)  # Saved: not opened again.
        # The owner's next direct message names their chat: it is the one used from then on.
        self.dm('m1')
        self.assertEqual(self.click('weekly_usage', 'ev4'), 'weekly_usage')
        self.assertEqual([chat for _, chat, _ in self.calls], ['oc_panel', 'oc_opened', 'oc_opened', 'oc_dm'])
        # A chat saved for another owner (the bot paired again) is not used.
        self.app.runtime.owner_open_id = 'ou_new'
        self.app.menu.opener = lambda *args: ''
        self.assertEqual(self.click('weekly_usage', 'ev5', operator='ou_new'), 'no_chat')

    def test_a_saved_chat_that_is_not_an_object_is_unknown_and_the_owners_next_message_saves_it(self):
        # It raised AttributeError where every direct message of the owner is admitted (app/access.py).
        for index, broken in enumerate(('[]', '"oc_dm"', 'null')):
            with self.subTest(broken=broken):
                (self.ctx.state_dir / OWNER_CHAT).write_text(broken)
                self.assertEqual(self.app.runtime.access.owner_chat(), '')
                self.dm(f'm{index}')
                self.assertEqual(json.loads((self.ctx.state_dir / OWNER_CHAT).read_text()),
                                 {'owner': 'owner', 'chat_id': 'oc_dm'})

    def test_each_items_new_card_recalls_its_last_one_in_that_chat(self):
        self.dm('m1')
        sent = iter(['om_1', 'om_2', 'om_3', 'om_4', ''])
        self.app.menu.actions = {key: (lambda ctx, chat, source, shown: shown(next(sent))) for key in KEYS}
        recall = self.app.messenger.recall
        self.click('weekly_usage', 'ev1')
        self.click('manage_groups', 'ev2')  # Another item: its own card, nothing recalled.
        recall.assert_not_called()
        self.click('weekly_usage', 'ev3')
        recall.assert_called_once_with(self.ctx.profile, 'om_1')
        recall.return_value = False  # Past the recall window: one log line, the record moves on.
        self.click('weekly_usage', 'ev4')
        self.assertEqual(recall.call_args.args[1], 'om_3')
        self.assertIn('menu panel weekly_usage recalled=False old=om_3', self.logs)
        self.click('weekly_usage', 'ev5')  # Nothing sent: nothing recalled, the last card stays recorded.
        self.assertEqual(recall.call_count, 2)
        record = json.loads((self.ctx.state_dir / bot_menu.PANELS).read_text())
        self.assertEqual({key: value['message_id'] for key, value in record.items()},
                         {'weekly_usage': 'om_4', 'manage_groups': 'om_2'})

    def test_a_card_in_another_chat_or_a_broken_record_is_not_recalled(self):
        panels = bot_menu.Panels(self.app.runtime, self.app.messenger)
        panels.shown(self.ctx, 'weekly_usage', 'oc_a', 'om_1')
        panels.shown(self.ctx, 'weekly_usage', 'oc_b', 'om_2')  # The owner's chat changed: the old one stays.
        (self.ctx.state_dir / bot_menu.PANELS).write_text('not json')
        panels.shown(self.ctx, 'weekly_usage', 'oc_b', 'om_3')
        self.app.messenger.recall.assert_not_called()
        panels.shown(self.ctx, 'weekly_usage', 'oc_b', 'om_4')
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_3')

    def test_the_hints_are_the_panel_and_dashboard_chats(self):
        panels = self.ctx.state_dir / 'proxy' / 'panels'
        panels.mkdir(parents=True)
        (panels / 'tok.json').write_text(json.dumps({'chat_id': 'oc_panel'}))
        (panels / 'latest.json').write_text(json.dumps({'token': 'tok'}))
        self.assertEqual(bot_menu.known_chats(self.app.proxy.panel, self.ctx), ['oc_panel', ''])

    def test_opening_the_chat_reads_it_from_the_send_or_the_message(self):
        sent = {'ok': True, 'data': {'message_id': 'om_1', 'chat_id': 'oc_new'}}
        with mock.patch.object(messenger, 'call', return_value=sent) as call:
            self.assertEqual(open_owner_chat('bot', 'ou_owner', 'menu-chat-k', print), 'oc_new')
        argv = call.call_args.args[0]
        self.assertEqual(argv[argv.index('--user-id') + 1], 'ou_owner')
        self.assertEqual(argv[argv.index('--text') + 1], FIRST_TEXT)
        self.assertEqual(argv[argv.index('--idempotency-key') + 1], 'menu-chat-k')
        with mock.patch.object(messenger, 'call', return_value={'data': {'message_id': 'om_1'}}), \
                mock.patch.object(bot_menu, 'fetch_message_detail',
                                  return_value={'data': {'items': [{'message_id': 'om_1', 'chat_id': 'oc_read'}]}}):
            self.assertEqual(open_owner_chat('bot', 'ou_owner', 'k', print), 'oc_read')
        with mock.patch.object(messenger, 'call', return_value=None):
            self.assertEqual(open_owner_chat('bot', 'ou_owner', 'k', print), '')

    def test_opening_the_chat_again_under_its_key_reads_the_receipt_not_feishu(self):
        previous = transport.configure_receipts(self.ctx.state_dir)
        self.addCleanup(transport.configure_receipts, previous)
        sent = json.dumps({'ok': True, 'data': {'message_id': 'om_1', 'chat_id': 'oc_new'}})
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=sent, stderr=''))
        detail = {'data': {'items': [{'message_id': 'om_1', 'chat_id': 'oc_new'}]}}
        with mock.patch.object(transport.subprocess, 'run', run), \
                mock.patch.object(bot_menu, 'fetch_message_detail', return_value=detail) as fetch:
            self.assertEqual(open_owner_chat('bot', 'ou_owner', 'menu-chat-k', print), 'oc_new')
            self.assertEqual(open_owner_chat('bot', 'ou_owner', 'menu-chat-k', print), 'oc_new')
        self.assertEqual(run.call_count, 1)  # The receipt keeps the message id; its chat is read from it.
        fetch.assert_called_once_with('om_1', 'bot')

    def test_the_menu_consumer_is_a_worker_and_stops_with_the_others(self):
        self.assertIn('menu', dict(self.app.worker_targets()))
        with mock.patch('covey.app.unified.consume') as loop:
            self.app.menu_loop(self.ctx)
        self.assertEqual(loop.call_args.args[1], events.MENU_EVENT_KEY)
        self.assertEqual(loop.call_args.kwargs['handle'], self.app.handle_menu_event)
        with mock.patch('covey.app.unified.stop_processes') as stop:
            self.app.stop_children()
        self.assertIn('bot menu event consumer', [name for name, _ in stop.call_args.args[0]])


class SubscriptionTests(unittest.TestCase):
    def test_the_consumer_asks_for_the_menu_event_with_its_fields(self):
        with mock.patch.object(events, 'consume_event_stream') as stream:
            events.consume(mock.Mock(profile=None), events.MENU_EVENT_KEY, shutdown=mock.Mock(), set_process=print,
                           handle=print, log=print, retry_seconds=300)
        argv = stream.call_args.kwargs['argv']
        self.assertEqual(argv[:3], ['lark-cli', 'event', 'consume'])
        self.assertEqual(argv[3], 'application.bot.menu_v6')
        self.assertIn('event_key', argv[argv.index('--jq') + 1])
        self.assertEqual(stream.call_args.kwargs['restart_delay'](1), 300)

    def test_health_does_not_need_the_menu_subscription(self):
        ready = {'ready': True, 'handling_seconds': 0}
        runtime = mock.Mock(connection_health={}, availability_health={})
        runtime.shutdown.is_set.return_value = False
        runtime.jobs.queue.qsize.return_value = 0
        with mock.patch('covey.app.health.consumers_snapshot', return_value={
                'im.message.receive_v1': ready, 'card.action.trigger': ready,
                'application.bot.menu_v6': {'ready': False, 'handling_seconds': 0, 'state': 'retrying'}}):
            self.assertEqual(bridge_snapshot(runtime, {})['status'], 'ready')


if __name__ == '__main__':
    unittest.main()

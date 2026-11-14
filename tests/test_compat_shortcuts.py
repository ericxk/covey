from __future__ import annotations

import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.core.topics import read_topic, remember_topic_message


# Commands that retired single-harness bots registered; all must stay inert.
COMMANDS = ('menu', 'workspace', 'settings', 'status', 'history', 'stop', 'retry', 'cancel', 'steer', 'start', 'help')


class RetiredShortcutTests(UnifiedCase):

    def test_a_slash_message_is_an_ordinary_message(self):
        # 2026-10-26: the retired slash commands are no longer recognised (the owner's choice);
        # "/help" or "/mnt/data/x" starts a topic like any message.
        for name in (*COMMANDS, 'harness'):
            self.receive('cmd-' + name, '/' + name)
        app, _ = self.route('cmd-help')
        self.assertEqual(app.submission.selection_card.call_count, len(COMMANDS) + 1)

    def test_old_cards_and_native_menus_cannot_change_settings_or_create_topics(self):
        app, ctx = self.route('old')
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'old-card')
        before = read_topic(ctx)
        # Trace pages and artifact sends are task-card controls, not retired menus.
        for kind in ('bridge_shortcut', 'bridge_workspace', 'bridge_harness', 'bridge_model_settings',
                     'bridge_history', 'bridge_workspace_files'):
            with mock.patch('covey.app.unified.BridgeApp.handle_card_action_event', autospec=True) as handler, \
                    mock.patch('covey.feishu.message_read.fetch_message_detail') as fetch, \
                    mock.patch.object(self.app.topics, 'route') as route:
                for message_id in ('old-card', 'unknown-card'):
                    self.app.handle_card_action_event({'operator_id': 'owner', 'chat_id': 'chat',
                        'message_id': message_id, 'action_value': {'kind': kind, 'topic_id': ctx.topic_id}}, self.ctx)
                handler.assert_not_called()
                fetch.assert_not_called()
                route.assert_not_called()
        self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_a_slash_message_in_a_topic_is_an_ordinary_message(self):
        app, ctx = self.route('old')
        self.receive('settings', '/settings', root='old')
        self.assertIn('/settings', ctx.history_path.read_text())
        self.assertNotIn('命令入口已关闭', '\n'.join(self.said()))

    def test_stop_callback_is_scoped_to_source_topic(self):
        for root in ('one', 'two'):
            self.receive(root, 'work')
            app, ctx = self.route(root)
            app.submission.start_workspace(ctx, 'chat', self.work.name)
            remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', root + '-card')
        first, second = [self.app.runtime.jobs.queue.get_nowait() for _ in range(2)]
        click = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'one-card', 'event_id': 'stop',
                 'action_value': {'kind': 'bridge_job_stop', 'job_id': second.job_id, 'topic_id': first.topic_id}}
        self.app.handle_card_action_event(click, self.ctx)
        self.assertFalse(second.cancel_event.is_set())
        click['action_value']['job_id'] = first.job_id
        self.app.handle_card_action_event(click, self.ctx)
        self.assertTrue(first.cancel_event.is_set())
        self.assertFalse(second.cancel_event.is_set())


if __name__ == '__main__':
    unittest.main()

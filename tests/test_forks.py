"""Forks (the main thread's topic_fork) open a new topic bound to the source topic's settings; the 💬 侧聊,
🔀 分叉 and 📦 压缩上下文 buttons left the cards (2026-11-08), and a click on an older card only says so."""
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.fake_lark import FakeCardKit  # noqa: E402
from support.sessions import write_session_id  # noqa: E402
from support.world import UnifiedCase  # noqa: E402
from support.records import write_topic
from covey.app.card_actions import RETIRED_TEXT  # noqa: E402
from covey.core.harness import HarnessCapabilities  # noqa: E402
from covey.core.jobs import upsert_job_record  # noqa: E402
from covey.core.history_storage import append_history
from covey.core.topics import read_topic  # noqa: E402


class ForkTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.receive('src', 'first question')
        self.app_, self.source = self.route('src')
        data = read_topic(self.source)
        data.update(harness_id='codex', model='codex-model', effort='high', model_settings_snapshot=True,
                    workspace_locked=True, harness_locked=True)
        write_topic(self.source, data)
        self.app_, self.source = self.route('src')
        self.session = str(uuid.uuid4())
        write_session_id(self.source.session_path, self.session)
        append_history(self.source.history_path, 'user', 'remember the codeword', {})
        append_history(self.source.history_path, 'assistant', 'OK', {})
        self.detail = mock.patch('covey.feishu.message_read.fetch_message_detail',
                                 return_value={'data': {'items': [{'message_id': 'src', 'thread_id': 'omt_src'}]}})


    @staticmethod
    def forward():
        """CardKit while Feishu forwards the source thread as message om_forwarded."""
        return FakeCardKit({'data': {'message_id': 'om_forwarded'}})

    def fork(self, status='done', click='click-1'):
        record = {'job_id': 1, 'status': status, 'user_text': 'first question'}
        with self.forward() as lark, self.detail:
            forks = self.app_.forks.create_fork(self.source, 'chat', record, click)
        return forks, lark

    def test_fork_forwards_the_source_thread_and_binds_a_new_topic_once(self):
        forks, lark = self.fork()
        path, body = lark.last.path, lark.last.data
        self.assertEqual(path, '/open-apis/im/v1/threads/omt_src/forward')
        self.assertEqual(body, {'receive_id': 'chat'})
        self.assertEqual(lark.last.options['params']['receive_id_type'], 'chat_id')
        _, target = self.route_reply('om_forwarded')
        data = read_topic(target)
        self.assertEqual((data['harness_id'], data['model'], data['workdir']),
                         ('codex', 'codex-model', read_topic(self.source)['workdir']))
        self.assertTrue(data['workspace_locked'] and data['harness_locked'])
        self.assertEqual(data['forked_from']['session_id'], self.session)
        self.assertEqual(data['forked_from']['topic_id'], self.source.topic_id)
        header = self.app_.messenger.send_message.call_args
        self.assertEqual(header.kwargs['reply_to_message_id'], 'om_forwarded')
        self.assertIn('分叉', header.args[2])
        self.assertEqual([f['root_id'] for f in forks], ['om_forwarded'])
        again, lark = self.fork()
        self.assertEqual(lark.calls, [])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]['mode'], 'fork')

    def test_a_fork_keeps_redacting(self):
        # A topic chose 敏感词脱敏 for its life; its forks carry the same history, so they redact too.
        for n, redacts in enumerate((None, True)):
            with self.subTest(redacts=redacts):
                data = read_topic(self.source)
                if redacts:
                    data['redaction'] = True
                write_topic(self.source, data)
                record = {'job_id': 1, 'status': 'done', 'user_text': 'first question'}
                with FakeCardKit({'data': {'message_id': f'om_fork_{n}'}}), self.detail:
                    self.app_.forks.create_fork(self.source, 'chat', record, f'click-{n}')
                _, target = self.route_reply(f'om_fork_{n}')
                self.assertEqual(read_topic(target).get('redaction'), redacts)

    def route_reply(self, root):
        return self.app.topics.route(self.ctx, {'chat_id': 'chat', 'sender_id': 'owner', 'message_id': root + '-reply',
                                         'root_id': root, 'content': 'next'})

    def test_a_running_turn_is_not_forked(self):
        with self.assertRaisesRegex(ValueError, '本轮完成后才能分叉'):
            self.fork(status='running')
        self.assertNotIn('forks', read_topic(self.source))

    def test_a_topic_opened_as_a_side_chat_is_an_ordinary_topic_now(self):
        # Before 2026-11-08 every turn of one said 只读; its record keeps the mode, nothing reads it.
        self.fork()
        _, target = self.route_reply('om_forwarded')
        data = read_topic(target)
        data['forked_from']['mode'] = 'side'
        write_topic(target, data)
        app, target = self.route_reply('om_forwarded')
        ctx, history = app.forks.fork_start(target, [])
        self.assertEqual(ctx.options['fork_session'], self.session)

    def test_header_names_the_source_task_and_its_model(self):
        self.fork()
        header = self.app_.messenger.send_message.call_args.args[2]
        self.assertIn('任务 #1 之后', header)
        self.assertRegex(header, r'话题 T\d+\*\* · 来自上方转发的话题 T\d+，')
        self.assertNotIn('轮任务', header)
        # The model as the catalog names it, as on the receipt and the task card (T197 said Claude · opus).
        self.assertIn('沿用原话题配置：Codex · Codex Model · 思考预算 high', header)
        with mock.patch.object(self.app_.cards, 'model_label', return_value=''):
            self.assertEqual(self.app_.forks.model_name(self.source, 'codex-model'), 'Codex · codex-model')

    def test_first_run_forks_the_native_session_only_when_it_can(self):
        self.fork()
        app, target = self.route_reply('om_forwarded')
        ctx, history = app.forks.fork_start(target, [])
        self.assertEqual((ctx.options['fork_session'], history), (self.session, []))
        # The source is running another turn: start from its finished conversation.
        busy = SimpleNamespace(topic_id=self.source.topic_id, execution_started=True, job_id=99, message_id='m')
        self.app.runtime.jobs.by_id[99] = busy
        ctx, history = app.forks.fork_start(target, [])
        self.assertNotIn('fork_session', ctx.options)
        self.assertEqual([row['content'] for row in history][-2:], ['remember the codeword', 'OK'])
        del self.app.runtime.jobs.by_id[99]
        with mock.patch.object(type(app.backend), 'capabilities', return_value=HarnessCapabilities()):
            ctx, history = app.forks.fork_start(target, [])
        self.assertNotIn('fork_session', ctx.options)
        self.assertTrue(history)
        ctx, history = app.forks.fork_start(self.source, ['own'])
        self.assertEqual((ctx.options.get('fork_session'), history), (None, ['own']))

    def test_a_reply_in_the_new_topic_runs_without_a_launch_card(self):
        self.fork()
        before = {name: a.submission.selection_card.call_count for name, a in self.app.apps.items()}
        self.receive('om_forwarded-reply', 'continue here', root='om_forwarded')
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.user_text), ('codex', 'continue here'))
        self.assertEqual({name: a.submission.selection_card.call_count for name, a in self.app.apps.items()}, before)

    def test_a_thread_feishu_will_not_forward_starts_from_a_root_card(self):
        calls = []

        def api(profile, method, path, data, **kwargs):
            calls.append(path)
            return None if path.endswith('/forward') else {'data': {'card_id': 'card_root'}}
        record = {'job_id': 1, 'status': 'done', 'user_text': 'first question'}
        with FakeCardKit(side_effect=api, sent=('om_root', False)) as lark, self.detail, \
                mock.patch('covey.feishu.message_read.list_thread_messages', return_value=[]):
            forks = self.app_.forks.create_fork(self.source, 'chat', record, 'click-long')
        self.assertEqual(calls, ['/open-apis/im/v1/threads/omt_src/forward', '/open-apis/cardkit/v1/cards'])
        self.assertEqual(lark.send.call_args.args[:3], ('test-bot', 'chat', 'card_root'))
        self.assertNotIn('reply_to_message_id', lark.send.call_args.kwargs)  # a new root in the main chat
        self.assertEqual([f['root_id'] for f in forks], ['om_root'])
        _, target = self.route_reply('om_root')
        self.assertEqual(read_topic(target)['forked_from']['session_id'], self.session)
        header = self.app_.messenger.send_message.call_args
        self.assertEqual(header.kwargs['reply_to_message_id'], 'om_root')
        self.assertRegex(header.args[2], r'来自话题 T\d+，任务 #1 之后')
        with FakeCardKit(None), self.detail, mock.patch('covey.feishu.message_read.list_thread_messages', return_value=[]):
            with self.assertRaisesRegex(ValueError, '起始消息发送失败'):
                self.app_.forks.create_fork(self.source, 'chat', record, 'click-fails')

    def test_a_refused_thread_goes_as_a_merge_of_its_newest_messages(self):
        # 2026-11-02 and 11-04: two of four forks' thread forwards answered 230001 and started from a bare root card.
        calls = []
        merged = iter([None, {'data': {'message': {'message_id': 'om_merged'}}}])

        def api(profile, method, path, data, **kwargs):
            calls.append((path, data, kwargs.get('params')))
            if path.endswith('/forward'):
                return None
            if path.endswith('/merge_forward'):
                return next(merged)  # The first merge refused too: then only the people's messages.
            return {'data': {'card_id': 'card_root'}}
        thread = [{'message_id': 'om_src', 'msg_type': 'merge_forward'}, {'message_id': 'om_a', 'msg_type': 'text'},
                  {'message_id': 'om_card', 'msg_type': 'interactive'}, {'message_id': 'om_sys', 'msg_type': 'system'},
                  {'message_id': 'om_b', 'msg_type': 'post'}]
        record = {'job_id': 1, 'status': 'done', 'user_text': 'first question'}
        with FakeCardKit(side_effect=api), self.detail, \
                mock.patch('covey.feishu.message_read.list_thread_messages', return_value=thread) as listed:
            forks = self.app_.forks.create_fork(self.source, 'chat', record, 'click-merge')
        listed.assert_called_once_with('omt_src', self.source.profile, 50)
        merges = [(data, params) for path, data, params in calls if path.endswith('/merge_forward')]
        self.assertEqual([data['message_id_list'] for data, _ in merges], [['om_a', 'om_card', 'om_b'], ['om_a', 'om_b']])
        self.assertEqual({params['receive_id_type'] for _, params in merges}, {'chat_id'})
        self.assertEqual(len({params['uuid'] for _, params in merges}), 2)
        self.assertNotIn('/open-apis/cardkit/v1/cards', [path for path, _, _ in calls])  # No bare root card.
        self.assertEqual([f['root_id'] for f in forks], ['om_merged'])
        self.assertTrue(any('merged its newest 2 messages' in line for line in self.logs))

    def test_a_retired_button_on_an_older_card_only_says_so(self):
        from covey.core.topics import remember_topic_message
        remember_topic_message(self.source.state_dir, self.source.topic_id, 'chat', 'task-card')
        upsert_job_record(self.source.state_dir, 'job-1', job_id=1, status='done', chat_id='chat', backend='codex',
                          topic_id=self.source.topic_id, card_id='card-1', card_message_id='task-card',
                          user_text='first question')
        clicks = (('bridge_fork', {'mode': 'side'}), ('bridge_fork', {'mode': 'fork'}),
                  ('bridge_compact', {'harness_id': 'codex', 'binding_revision': 1, 'compact': True}))
        for n, (kind, value) in enumerate(clicks):
            with self.subTest(kind=kind, value=value):
                event = {**self.event('task-card'), 'operator_id': 'owner', 'event_id': f'ev-retired-{n}',
                         'action_value': {'kind': kind, 'topic_id': self.source.topic_id, 'job_id': 1, **value}}
                with FakeCardKit() as lark, self.detail, mock.patch.object(self.app_.messenger, 'reply') as reply:
                    self.app.handle_card_action_event(event, self.ctx)
                self.assertEqual(reply.call_args.args[2], RETIRED_TEXT[kind])
                self.assertEqual([c for c in lark.calls if c.method in ('PUT', 'POST')], [])
        self.assertNotIn('forks', read_topic(self.source))
        self.assertIsNone(self.app_.compaction.request(self.source))


class CompactionTests(UnifiedCase):
    """The main thread's compaction request (app/compaction.py): the next resumed turn takes it."""

    def setUp(self):
        super().setUp()
        self.receive('src', 'first question')
        _, source = self.route('src')
        data = read_topic(source)
        data.update(harness_id='claude', model='claude-model', effort='high', model_settings_snapshot=True,
                    workspace_locked=True, harness_locked=True)
        write_topic(source, data)
        self.app_, self.source = self.route('src')

    def test_a_request_is_taken_by_the_next_resumed_turn(self):
        self.app_.compaction.set_requested(self.source, 1)
        self.assertTrue((self.source.history_path.parent / 'compact-next.json').exists())
        job = SimpleNamespace(job_id=2, message_id='job-2')
        # A turn on a new session has nothing to compact and leaves the request.
        self.assertNotIn('compact_first', self.app_.compaction.turn_context(self.source, job, None).options)
        session = str(uuid.uuid4())
        ctx = self.app_.compaction.turn_context(self.source, job, session)
        self.assertTrue(ctx.options['compact_first'])
        self.assertFalse((self.source.history_path.parent / 'compact-next.json').exists())
        self.assertNotIn('compact_first', self.app_.compaction.turn_context(self.source, job, session).options)
        with mock.patch.object(type(self.app_.backend), 'capabilities', return_value=HarnessCapabilities()):
            self.app_.compaction.set_requested(self.source, 3)
            self.assertNotIn('compact_first', self.app_.compaction.turn_context(self.source, job, session).options)


if __name__ == '__main__':
    unittest.main()

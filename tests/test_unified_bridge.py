from __future__ import annotations

import importlib
import inspect
import json
import os
import unittest
from unittest import mock

from support.cards import stream_runtime
from support.sessions import write_session_id
from support.world import UnifiedCase
from support.records import write_topic
from support import requires_extension
from covey.app.app import BridgeApp
from covey.app.harnesses import components
from covey.core.ignored_env import IGNORED_ENV
from covey.core.roster import HARNESSES
from covey.core.jobs import find_job_record
from covey.core.model_settings import settings_path
from covey.core.topics import read_topic, remember_topic_message
from covey.service.pairing import issue
from types import SimpleNamespace


class UnifiedTests(UnifiedCase):
    def test_topics_get_short_numbers_in_creation_order_and_old_topics_on_first_show(self):
        from types import SimpleNamespace
        from covey.cards.streaming import build_stream_card
        from covey.core.topics import topic_label
        _, first = self.route('first')
        _, second = self.route('second')
        self.assertEqual((read_topic(first)['topic_number'], read_topic(second)['topic_number']), (1, 2))
        self.assertEqual(topic_label(self.ctx.state_dir, first.topic_id), 'T1')
        # A topic from before numbering is numbered when first shown, then keeps it.
        _, old = self.route('old')
        data = read_topic(old)
        data.pop('topic_number')
        write_topic(old, data)
        self.assertEqual(topic_label(self.ctx.state_dir, old.topic_id), 'T4')
        self.assertEqual(topic_label(self.ctx.state_dir, old.topic_id), 'T4')
        self.assertEqual(topic_label(self.ctx.state_dir, ''), '')
        runtime = stream_runtime()
        job = SimpleNamespace(job_id=196, user_text='看下这个转发', topic_label='T1', model='m', effort='', timeout=0)
        card = build_stream_card('处理中', job.user_text, runtime, job=job)
        self.assertEqual(card['header']['title']['content'], 'T1 · #196 · 看下这个转发')
        self.assertNotIn('任务编号', json.dumps(card, ensure_ascii=False))  # The title has both (2026-11-04 15:07).

    def test_every_card_click_is_logged_with_its_outcome(self):
        logs = []
        self.app.runtime.log_sink = logs.append
        self.app.handle_card_action_event({'operator_id': 'stranger', 'chat_id': 'chat', 'message_id': 'om_card_x',
                                           'event_id': 'ev_1', 'action_value': {'kind': 'bridge_job_stop'}}, self.ctx)
        self.app.handle_card_action_event({'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'om_card_x',
                                           'event_id': 'ev_2', 'action_value': {'kind': 'old_menu'}}, self.ctx)
        lines = [line for line in logs if line.startswith('card action')]
        self.assertIn('kind=bridge_job_stop', lines[0])
        self.assertIn('outcome=not_owner', lines[0])
        self.assertIn('outcome=retired', lines[1])
        self.assertTrue(all('form' not in line for line in lines))

    def test_a_card_outside_the_topic_aliases_is_looked_up_once(self):
        # The Bridge asks Feishu where the card is and hands the answer on with the click; routing records
        # the card's alias too, so the topic's app finds the topic without asking again.
        _, ctx = self.route('lookup-root')
        item = {'message_id': 'om_unaliased', 'root_id': 'lookup-root'}
        event = {**self.event('om_unaliased'), 'operator_id': 'owner', 'event_id': 'ev_lookup',
                 'action_value': {'kind': 'bridge_trace'}}
        with mock.patch('covey.feishu.message_read.fetch_message_detail',
                        return_value={'data': {'items': [item]}}) as fetch, \
                mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as action:
            self.app.handle_card_action_event(event, self.ctx)
        fetch.assert_called_once_with('om_unaliased', self.ctx.profile)
        self.assertEqual(action.call_args.args[2].topic_id, ctx.topic_id)
        self.assertEqual(action.call_args.kwargs, {'item': item})

    def test_the_app_takes_the_card_message_it_is_given_instead_of_asking_again(self):
        # Change (c), 2026-11-05: the item the Bridge already fetched; only a click without one is looked up.
        app, ctx = self.route('given-root')
        item = {'message_id': 'om_given', 'root_id': 'given-root'}
        event = {'chat_id': 'chat', 'operator_id': 'owner', 'message_id': 'om_given', 'event_id': 'ev_given',
                 'action_value': {'kind': 'bridge_trace'}}
        handler = mock.Mock(return_value=True)
        with mock.patch('covey.feishu.message_read.fetch_message_detail') as fetch, \
                mock.patch.object(BridgeApp, 'card_handler', return_value=handler):
            app.handle_card_action_event(event, self.ctx, item=item)
        fetch.assert_not_called()
        self.assertEqual(handler.call_args.args[0].topic_id, ctx.topic_id)

    def test_a_click_with_no_chat_id_anywhere_asks_feishu_once(self):
        # The one path that asked twice before change (c): with no chat_id routing cannot record the card's
        # alias, so the app did not know the card either. Through the real app, not a stand-in for it.
        item = {'message_id': 'om_no_chat', 'root_id': 'no-chat-root'}
        event = {'operator_id': 'owner', 'message_id': 'om_no_chat', 'event_id': 'ev_no_chat',
                 'action_value': {'kind': 'bridge_trace'}}
        handler = mock.Mock(return_value=True)
        with mock.patch('covey.feishu.message_read.fetch_message_detail',
                        return_value={'data': {'items': [item]}}) as fetch, \
                mock.patch.object(BridgeApp, 'card_handler', return_value=handler):
            self.app.handle_card_action_event(event, self.ctx)
        handler.assert_called_once()
        self.assertEqual(fetch.call_count, 1)

    def test_trace_navigation_is_scoped_and_does_not_reopen_global_menus(self):
        app, ctx = self.route('trace-task')
        # The panel has one page since 2026-11-04; a card sent with page controls still sends this route.
        route = {'kind': 'bridge_trace', 'topic_id': ctx.topic_id, 'job_id': 123}
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'trace-card')
        event = {**self.event('trace-card'), 'operator_id': 'owner'}
        event['action_value'] = {**route, 'page': 1}
        with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as action:
            self.app.handle_card_action_event(event, self.ctx)
            action.assert_called_once()
            action.reset_mock()
            event['action_value']['topic_id'] = 'other-topic'
            self.app.handle_card_action_event(event, self.ctx)
            action.assert_not_called()
            event['action_value'] = {'kind': 'bridge_history', 'topic_id': ctx.topic_id}
            self.app.handle_card_action_event(event, self.ctx)
            action.assert_not_called()

    def test_four_topics_keep_backend_workspace_and_queue_identity(self):
        jobs = []
        for index, name in enumerate(self.app.apps):
            mid = f'topic-{index}'
            self.receive(mid, 'initial')
            _, ctx = self.route(mid)
            self.app.topics.select_harness(ctx, 'chat', name)
            app, ctx = self.route(mid)
            app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
            job = self.app.runtime.jobs.queue.get_nowait()
            jobs.append(job)
            self.assertEqual(job.harness_id, name)
            self.assertEqual(job.workdir, self.work)
            self.assertEqual(find_job_record(self.ctx.state_dir, mid)['backend'], name)
            # This verifies routing; backend installation/auth belongs to preflight tests.
            with mock.patch('covey.app.job_dispatch.check_or_raise'), \
                 mock.patch.object(BridgeApp, 'process_reply_job', autospec=True) as process:
                self.app.process_reply_job(job, self.ctx)
                self.assertIs(process.call_args.args[0], app)
                self.assertEqual(process.call_args.args[2].backend_cmd, self.app.harnesses[name].context.backend_cmd)
        self.assertEqual(len({j.job_id for j in jobs}), len(self.app.apps))
        self.assertEqual(len({j.session_path for j in jobs}), len(self.app.apps))
        self.assertEqual(self.app.harnesses['codex'].backend.__class__.__name__, 'CodexBackend')

    def test_a_job_stopped_in_the_queue_gets_no_unavailable_notice_at_dequeue(self):
        self.receive('stopped', 'initial')
        _, ctx = self.route('stopped')
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        app, ctx = self.route('stopped')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        app.controls.cancel_reply_job_from_card(ctx, {'job_id': str(job.job_id)}, 'chat', 'stop-click')
        said = len(self.said())
        with mock.patch('covey.app.job_dispatch.check_or_raise', side_effect=RuntimeError('down')) as check, \
             mock.patch.object(app.controls, 'show_recovery_card') as recovery:
            self.app.process_reply_job(job, self.ctx)
        check.assert_not_called()
        recovery.assert_not_called()
        self.assertEqual(self.said()[said:], [])
        self.assertEqual(find_job_record(self.ctx.state_dir, 'stopped')['status'], 'cancelled')
        self.assertNotIn(job.job_id, self.app.runtime.jobs.by_id)

    def test_a_task_runner_does_not_repeat_its_bridges_backend_check(self):
        self.receive('checked', 'initial')
        _, ctx = self.route('checked')
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        app, ctx = self.route('checked')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        for runner, checks in ((None, 1), (object(), 0)):  # The Bridge checks it; its task runner then runs it unchecked.
            self.app.runtime.runner = runner
            with mock.patch('covey.app.job_dispatch.check_or_raise') as check, \
                 mock.patch.object(BridgeApp, 'process_reply_job', autospec=True) as process:
                self.app.process_reply_job(job, self.ctx)
            self.assertEqual((check.call_count, process.call_count), (checks, 1))
        self.app.runtime.runner = None

    def test_only_a_job_that_runs_next_has_its_task_runner_started_before_its_card(self):
        with mock.patch('covey.app.submission.prestart_runner') as prestart:
            self.receive('next', 'task')
            app, ctx = self.route('next')
            app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
            self.receive('next-2', '再补一句', root='next')  # Queued behind the first: no runner yet.
        self.assertEqual([call.args[1].message_id for call in prestart.call_args_list], ['next'])

    def test_existing_session_rejects_switch_without_rebinding_or_new_topic(self):
        app, ctx = self.route('old')
        write_session_id(ctx.session_path, '11111111-1111-1111-1111-111111111111')
        original = read_topic(ctx)
        target = self.app.apps['claude']
        with self.assertRaisesRegex(ValueError, '本话题已开始'):
            self.app.topics.select_harness(ctx, 'chat', 'claude')
        target.submission.selection_card.assert_not_called()
        self.assertEqual(read_topic(ctx), original)
        self.assertEqual(ctx.session_path.read_text().strip(), '11111111-1111-1111-1111-111111111111')

    def test_harness_settings_never_read_codex_defaults(self):
        (self.ctx.state_dir / 'model-settings').mkdir(exist_ok=True)
        (self.ctx.state_dir / 'model-settings' / 'codex.json').write_text(json.dumps({'model': 'codex-only', 'effort': 'high'}))
        (self.ctx.state_dir / 'model-settings.json').write_text(json.dumps({'model': 'old-file', 'effort': 'high'}))
        self.assertEqual(self.app.apps['codex'].models.configured_context(self.ctx).model, 'codex-only')
        for name in set(self.app.apps) - {'codex'}:
            app = self.app.apps[name]
            ctx = app.models.configured_context(self.app.harnesses[name].context)
            self.assertNotIn(ctx.model, ('codex-only', 'old-file'))
            self.assertEqual(settings_path(ctx), self.ctx.state_dir / 'model-settings' / (name + '.json'))

    def test_queued_task_cannot_switch_in_place(self):
        self.receive('queued', 'task')
        app, ctx = self.route('queued')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        original = read_topic(ctx)
        with self.assertRaisesRegex(ValueError, '本话题已开始'):
            self.app.topics.select_harness(ctx, 'chat', 'dsh')
        self.app.apps['dsh'].submission.selection_card.assert_not_called()
        self.assertEqual(job.harness_id, 'codex')
        self.assertEqual(read_topic(ctx), original)

    def test_task_card_controls_reach_the_topic_app(self):
        # Every button the task card renders must pass the closed allowlist.
        app, ctx = self.route('controls-task')
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'controls-card')
        event = {**self.event('controls-card'), 'operator_id': 'owner'}
        for kind in ('bridge_artifact_upload',):
            event['action_value'] = {'kind': kind, 'topic_id': ctx.topic_id, 'job_id': 1}
            with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as action:
                self.app.handle_card_action_event(event, self.ctx)
            action.assert_called_once()

    def test_old_selection_card_rejected_after_change(self):
        _, ctx = self.route('fresh')
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'old-card')
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as dispatch:
            self.app.handle_card_action_event({'chat_id': 'chat', 'operator_id': 'owner', 'message_id': 'old-card',
                'action_value': {'kind': 'bridge_workspace', 'harness_id': 'codex', 'binding_revision': 1}}, self.ctx)
            dispatch.assert_not_called()

    def test_unauthorized_sender_does_not_create_topics(self):
        self.app.handle_event({**self.event('foreign'), 'sender_id': 'other'}, self.ctx)
        self.assertFalse((self.ctx.state_dir / 'topics').exists())

    def test_a_message_delivered_again_is_skipped_even_after_a_restart(self):
        self.receive('twice', 'task')
        app, _ = self.route('twice')
        self.receive('twice', 'task')
        restarted, _ = self.bridge()
        with mock.patch('covey.app.app.enrich_incoming_event') as enrich:
            restarted.handle_event(self.event('twice'), self.ctx)
            enrich.assert_not_called()
        app.submission.selection_card.assert_called_once()
        self.assertEqual(sum('duplicate message id=twice' in line for line in self.logs), 2)

    def test_a_message_that_starts_with_a_path_is_a_task_not_a_command(self):
        # 2026-10-26: "/mnt/.../ 这个数据有多少条" was dropped as a retired command, with no topic.
        self.receive('path', '/mnt/data/run/\n这个数据有多少条')
        app, ctx = self.route('path')
        app.submission.selection_card.assert_called_once()
        self.receive('path-reply', '/mnt/data/run/b.jsonl 也看看', root='path')
        self.assertNotIn('命令入口已关闭', '\n'.join(self.said()))
        self.assertIn('/mnt/data/run/b.jsonl 也看看', ctx.history_path.read_text())
        cards = app.submission.selection_card.call_count  # Each message before 开始任务 gets its card.
        self.receive('slash', '/new')  # No slash command is recognised any more: a new topic.
        self.assertEqual(app.submission.selection_card.call_count, cards + 1)

    def test_legacy_topic_gets_original_backend_and_session(self):
        app, ctx = self.route('legacy')
        data = read_topic(ctx)
        data.pop('harness_id')
        data.pop('binding_revision')
        write_topic(ctx, data)
        write_session_id(ctx.session_path, '11111111-1111-1111-1111-111111111111')
        _, resumed = self.route('legacy')
        self.assertEqual(read_topic(resumed)['harness_id'], 'codex')
        self.assertEqual(resumed.session_path, ctx.session_path)

    def test_identity_rejects_other_apps_state(self):
        (self.ctx.state_dir / 'bot-identity.json').write_text(json.dumps({'app_id': 'old-app', 'owner': 'owner'}))
        with mock.patch('covey.app.unified.run_json', return_value={'appId': 'new-app', 'verified': True,
            'identities': {'user': {'openId': 'owner', 'status': 'ready'}}}):
            with self.assertRaisesRegex(ValueError, 'another Feishu application'):
                self.app.init_target(self.ctx)

    def test_binding_mismatch_prevents_backend_execution(self):
        self.receive('one', 'task')
        app, ctx = self.route('one')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        job.binding_revision += 1
        with mock.patch.object(BridgeApp, 'process_reply_job', autospec=True) as run:
            self.app.process_reply_job(job, self.ctx)
            run.assert_not_called()
        self.assertEqual(find_job_record(ctx.state_dir, 'one')['status'], 'failed')

    def test_owner_pairing_is_explicit_one_time_and_app_bound(self):
        code, path = issue(self.ctx.state_dir, 'new-app')
        self.app.pairing_file = path
        self.app.runtime.owner_open_id = ''
        with mock.patch('covey.app.unified.run_json', return_value={'appId': 'new-app', 'verified': True,
                        'identities': {'bot': {'status': 'ready'}, 'user': {'status': 'missing'}}}):
            self.app.init_target(self.ctx)
        self.app.handle_event(self.event('wrong', '/pair wrong'), self.ctx)
        self.assertFalse(self.app.runtime.owner_open_id)
        self.assertFalse(self.app.runtime.access.is_owner(''))
        self.assertFalse((self.ctx.state_dir / 'topics').exists())
        self.app.handle_event(self.event('pair', '/pair ' + code), self.ctx)
        self.assertEqual(self.app.runtime.owner_open_id, 'owner')
        self.assertFalse(path.exists())
        self.assertEqual(json.loads((self.ctx.state_dir / 'bot-identity.json').read_text())['owner'], 'owner')
        with self.assertRaisesRegex(ValueError, 'already has an owner'):
            issue(self.ctx.state_dir, 'new-app')

    @requires_extension()
    def test_different_workspaces_and_stop_are_scoped_to_topic(self):
        jobs = []
        contexts = []
        for name in ('codex', 'claude', 'dsh', 'maso'):
            project = self.root / name
            project.mkdir()
            self.receive(name, 'task')
            _, ctx = self.route(name)
            self.app.topics.select_harness(ctx, 'chat', name)
            app, ctx = self.route(name)
            app.submission.start_workspace(ctx, 'chat', str(project))
            job = self.app.runtime.jobs.queue.get_nowait()
            jobs.append(job)
            contexts.append(app.submission.event_context(self.app.harnesses[name].context, self.event(name)))
            self.assertEqual(job.workdir, project)
            self.assertEqual(contexts[-1].workdir, project)
            self.assertEqual(job.harness_id, name)
        other = self.app.apps['claude']
        other.controls.cancel_reply_job_from_card(contexts[1], {'job_id': str(jobs[0].job_id)}, 'chat', 'bad-stop')
        self.assertFalse(any(j.cancel_event.is_set() for j in jobs))
        other.controls.cancel_reply_job_from_card(contexts[1], {'job_id': str(jobs[1].job_id)}, 'chat', 'good-stop')
        self.assertTrue(jobs[1].cancel_event.is_set())
        self.assertFalse(any(j.cancel_event.is_set() for j in (jobs[0], jobs[2], jobs[3])))

    @requires_extension()
    def test_unversioned_card_cannot_change_rebound_topic(self):
        _, ctx = self.route('fresh')
        remember_topic_message(ctx.state_dir, ctx.topic_id, 'chat', 'legacy-card')
        self.app.topics.select_harness(ctx, 'chat', 'maso')
        with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True) as dispatch:
            self.app.handle_card_action_event({'chat_id': 'chat', 'operator_id': 'owner', 'message_id': 'legacy-card',
                'action_value': {'kind': 'bridge_workspace', 'topic_id': ctx.topic_id}}, self.ctx)
            dispatch.assert_not_called()

    def test_a_harness_picked_on_the_launch_card_keeps_the_expanded_message(self):
        # A forward, attachment or quote shows short on the cards; the model gets it expanded.
        expanded = '合并转发消息，共 2 条记录。\n\n[1] 我改不了\n\n[2] 居然提示我没权限'
        self.receive('forward', expanded, display='[合并转发 2 条]')
        _, ctx = self.route('forward')
        self.app.topics.select_harness(ctx, 'chat', 'claude')
        app, ctx = self.route('forward')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.user_text, job.prompt_text), ('claude', '[合并转发 2 条]', expanded))
        self.assertEqual(app.runs._turn_message(ctx, job, None)[1].count('居然提示我没权限'), 1)

    def test_binding_unstarted_topic_does_not_send_another_selection(self):
        _, ctx = self.route('pending')
        child = self.app.apps['claude']
        target = self.app.topics.select_harness(ctx, 'chat', 'claude')
        self.assertEqual(read_topic(ctx)['harness_id'], 'claude')
        self.assertEqual(target.topic_id, ctx.topic_id)
        child.submission.selection_card.assert_not_called()
        self.assertFalse(read_topic(ctx).get('workspace_locked'))


class IgnoredEnvTests(UnifiedCase):
    """Backend parameters the environment no longer sets: one line each at the Bridge's start (owner 2026-11-10)."""

    ENV = {"LARK_CLAUDE_BRIDGE_MODEL": "claude-x", "LARK_BRIDGE_CODEX_CMD": "codex",
           "LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "1", "LARK_CLAUDE_BRIDGE_LOGIN_SHELL": "1",
           "LARK_DSH_BRIDGE_CMD": "dsh", "LARK_MASO_BRIDGE_PYTHON": "/absolute/python"}

    def startup_lines(self, env):
        self.logs.clear()
        with mock.patch.dict(os.environ, env), mock.patch("covey.app.launch_models.start_catalog_refresh"), \
                mock.patch("covey.app.model_usage.start"):
            self.app.log_startup(self.ctx)
        return [line for line in self.logs if line.startswith("env ")]

    def test_the_start_names_the_extensions_it_runs_with(self):
        self.logs.clear()
        with mock.patch("covey.app.launch_models.start_catalog_refresh"), mock.patch("covey.app.model_usage.start"), \
                mock.patch("covey.core.extensions.manifests", return_value=(SimpleNamespace(package="pkg_a"),)):
            self.app.log_startup(self.ctx)
        self.assertIn("extensions loaded=pkg_a", self.logs)
        self.logs.clear()
        with mock.patch("covey.app.launch_models.start_catalog_refresh"), mock.patch("covey.app.model_usage.start"), \
                mock.patch("covey.core.extensions.manifests", return_value=()):
            self.app.log_startup(self.ctx)
        self.assertIn("extensions loaded=none", self.logs)

    def test_the_bridge_names_each_variable_that_is_set_once_at_startup(self):
        self.assertEqual(self.startup_lines(self.ENV), [
            "env LARK_BRIDGE_CODEX_CMD is no longer read; put --codex-cmd in the service command line",
            "env LARK_CLAUDE_BRIDGE_MODEL is no longer read; put --model in harnesses.json",
            "env LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS is no longer read; unset it: its value is the default",
            "env LARK_CLAUDE_BRIDGE_LOGIN_SHELL is no longer read; put --claude-login-shell in harnesses.json",
            "env LARK_DSH_BRIDGE_CMD is no longer read; put --dsh-cmd in harnesses.json"])
        # LARK_MASO_BRIDGE_PYTHON stays (setup_harness_bridge.sh sets it); the sandbox at its default only has to go.
        self.assertEqual(self.startup_lines({"LARK_BRIDGE_CODEX_SANDBOX": "danger-full-access"}), [
            "env LARK_BRIDGE_CODEX_SANDBOX is no longer read; put --sandbox in the service command line"])
        clean = {k: v for k, v in os.environ.items() if k not in IGNORED_ENV}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(self.startup_lines({}), [])

    def test_a_permission_the_variable_restricted_says_what_the_backend_now_does(self):
        self.assertEqual(self.startup_lines({"LARK_BRIDGE_CODEX_SANDBOX": "workspace-write",
                                             "LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "Off"}), [
            "env LARK_BRIDGE_CODEX_SANDBOX=workspace-write is no longer read, so Codex would run with "
            "danger-full-access; unset it and put --sandbox workspace-write in the service command line "
            "(covey_doctor fails until then)",
            "env LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS=Off is no longer read, so Claude would skip its permission "
            "prompts; unset it and put --no-skip-permissions in harnesses.json (covey_doctor fails until then)"])

    def test_a_task_runner_rebuilding_the_bridge_says_nothing(self):
        # The runner builds the Bridge's context from the handoff argv (app/runner.py) and never logs the startup.
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("covey.core.ignored_env.ignored_env") as ignored:
            self.bridge()
        ignored.assert_not_called()

    def test_no_variable_is_read_and_its_option_is_its_harness_s(self):
        for name, (harness, option) in IGNORED_ENV.items():
            with self.subTest(name=name):
                spec = components(harness)[0]
                self.assertNotIn(f'"{name}"', inspect.getsource(importlib.import_module(HARNESSES[harness].module)))
                parser = self.app.harnesses[harness].build_parser()
                for each in option if isinstance(option, tuple) else (option,):
                    if each:
                        self.assertIn(each, parser._option_string_actions, spec.name)


class TaskRunnerPartsTests(UnifiedCase):
    """A task runner builds the Bridge without the parts only the Bridge uses (UnifiedBridgeApp.PARTS), built on first use."""

    # What a runner's start must not import: the bot menu, the settings and other panels, 代答, the web dashboard,
    # the digest, the maintenance and service parts of the Bridge's main().
    BRIDGE_ONLY = ('covey.app.bot_menu', 'covey.app.proxy', 'covey.app.dashboard', 'covey.app.digest',
                   'covey.app.settings_hub', 'covey.app.overview', 'covey.app.group_manager', 'covey.app.group_menu',
                   'covey.app.group_boards', 'covey.app.weekly_usage', 'covey.app.workspace_manager',
                   'covey.app.butler_tools', 'covey.app.app_setup', 'covey.app.topic_seen', 'covey.app.health',
                   'covey.app.stale', 'covey.core.maintenance', 'covey.core.migrate', 'covey.service.manager',
                   'covey.service.boot', 'covey.feishu.connections', 'covey.core.serving', 'urllib.request')

    def runner_bridge(self):
        from covey.app.unified import UnifiedBridgeApp
        from support import REPO

        app = UnifiedBridgeApp(script_path=REPO / 'bin/covey_bridge.py', task_runner=True)
        args = app.build_parser().parse_args(['--workdir', str(self.work), '--state-dir', str(self.root / 'state'),
                                              '--profile', 'test-bot'])
        ctx = app.build_context(args)
        self.built.append(app)
        return app, ctx

    def test_a_task_runner_starts_without_the_parts_only_the_bridge_uses(self):
        import subprocess
        import sys
        from support import REPO

        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "import covey.app.runner\n"
            "from covey.app.unified import UnifiedBridgeApp\n"
            "app = UnifiedBridgeApp(script_path=Path(sys.argv[1]) / 'bin/covey_bridge.py', task_runner=True)\n"
            "ctx = app.build_context(app.build_parser().parse_args(['--workdir', sys.argv[2], '--state-dir', sys.argv[3],\n"
            "                                                       '--profile', 'test-bot']))\n"
            "print(' '.join(sorted(name for name in sys.modules if name in sys.argv[4:])))\n")
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'COVEY_EXTENSIONS': ''}
        done = subprocess.run([sys.executable, '-c', script, str(REPO), str(self.work), str(self.root / 'runner-state'),
                               *self.BRIDGE_ONLY], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip().splitlines()[-1] if done.stdout.strip() else '', '',
                         'a task runner imported what only the Bridge uses')

    def test_a_part_is_built_on_first_use_and_is_the_bridges_own(self):
        runner, ctx = self.runner_bridge()
        for name in runner.PARTS:
            if name != 'extension_features':
                self.assertNotIn(name, runner.__dict__, name)
        self.assertEqual(runner.butler_paths, (ctx.state_dir, ctx.profile or ''))
        before = set(runner.__dict__)
        menu = runner.menu  # One use builds them all, in the Bridge's order.
        self.assertEqual(set(runner.__dict__) - before - {'extension_features'}, set(runner.PARTS) - {'extension_features'},
                         'PARTS names exactly what _build_parts sets')
        self.assertEqual([type(f).__name__ for f in runner.features], [type(f).__name__ for f in self.app.features])
        self.assertEqual(sorted(menu.actions), sorted(self.app.menu.actions))
        self.assertEqual((runner.butler_env.state_dir, runner.butler_env.profile), (ctx.state_dir, ctx.profile or ''))
        self.assertIs(runner.menu, menu)  # Built once.
        # What build_context set before a part exists reaches it; after, it reaches the part itself.
        self.assertEqual(runner.titles, self.app.titles)
        with self.assertRaises(AttributeError):
            runner.no_such_part

    def test_a_click_in_a_task_runner_meets_the_same_controllers(self):
        runner, ctx = self.runner_bridge()
        runner.runtime.owner_open_id = 'owner'
        events = [{'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'om_card_x', 'event_id': 'ev_1',
                   'action_value': {'kind': 'old_menu'}}]
        self.assertEqual(runner._handle_card_action_event(dict(events[0]), ctx),
                         self.app._handle_card_action_event(dict(events[0]), self.ctx))

    def test_a_runner_builds_the_parts_once_the_job_is_under_way_unless_it_ends_first(self):
        import threading
        from covey.app.runner import warm_parts

        runner, _ = self.runner_bridge()
        stopped = threading.Event()
        stopped.set()
        warm_parts(runner, stopped, delay=0)  # The runner ends first: nothing is built.
        self.assertNotIn('features', runner.__dict__)
        warm_parts(runner, threading.Event(), delay=0)
        self.assertIn('features', runner.__dict__)
        warm_parts(object(), threading.Event(), delay=0)  # A failure is only a missed head start.


if __name__ == '__main__':
    unittest.main()

"""A dedicated bot uses its configured Harness for new topics, checks and runner callbacks."""
from __future__ import annotations

from unittest import mock

from support import requires_extension, REPO, quiet
from support.records import run_files, write_topic
from support.sessions import write_session_id
from support.world import UnifiedCase
from covey.app import availability, detached, runner_ipc
from covey.app.app import BridgeApp
from covey.app.harnesses import Harness
from covey.app.launch_models import available_models
from covey.app.topics import retired_notice
from covey.app.unified import UnifiedBridgeApp
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core import run_dir
from covey.core.state import write_json
from covey.core.topics import read_topic, remember_topic_message


class EnabledHarnessTests(UnifiedCase):
    def configured_bridge(self, names, **entries):
        config = self.root / 'harnesses.json'
        write_json(config, {'enabled_harnesses': names, 'harnesses': {
            'claude': {'args': ['--model', 'claude-model', '--effort', 'high',
                                '--claude-cmd', '/absolute/bin/claude']}, **entries}})
        app = UnifiedBridgeApp(script_path=REPO / 'bin/covey_bridge.py')
        quiet(app, self.logs)
        self.built.append(app)
        argv = ['--workdir', str(self.work), '--state-dir', str(self.root / 'state'),
                '--profile', 'test-bot', '--harness-config', str(config)]
        ctx = app.build_context(app.build_parser().parse_args(argv))
        app.runtime.owner_open_id = 'owner'
        app.messenger.send_message = mock.Mock(return_value=True)
        for child in app.apps.values():
            child.submission.selection_card = mock.Mock()
            child.cards.create_streaming_reply = mock.Mock(return_value=None)
            child.harness.started = True
            child.models.prepare_submission_context = lambda target: target
        return app, ctx, argv

    def enable(self, names=('claude',)):
        self.app, self.ctx, self.argv = self.configured_bridge(list(names))

    def queued(self, mid='task'):
        self.receive(mid, 'initial')
        child, target = self.route(mid)
        child.submission.start_workspace(target, 'chat', target.workspace_name)
        return child, target, self.app.runtime.jobs.queue.get_nowait()

    def test_only_enabled_harness_has_models_and_readiness_checks(self):
        self.enable()
        self.assertEqual(set(self.app.apps), {'claude'})
        self.assertEqual(set(self.app.harnesses), {'claude'})
        self.assertEqual((self.ctx.model, self.ctx.effort), ('claude-model', 'high'))
        self.assertEqual(self.ctx.backend_cmd, ['/absolute/bin/claude'])
        choices = available_models(self.app.topics, self.ctx)
        self.assertEqual({row['harness'] for row in choices.values()}, {'claude'})
        with mock.patch.object(availability, 'local_check', return_value={'status': 'configured'}) as check:
            availability.sample(self.app, self.ctx)
        self.assertEqual([call.args[0] for call in check.call_args_list], ['claude'])

    def test_first_enabled_harness_routes_new_topics_without_rebinding_old_ones(self):
        old_app, old = self.route('old-codex')
        self.enable(('claude', 'codex'))
        child, target, job = self.queued()
        self.assertEqual((child.spec.name, job.harness_id), ('claude', 'claude'))
        self.assertEqual(target.session_path.name, 'claude_session_id')
        self.assertEqual(self.route('old-codex')[1].session_path, old.session_path)
        self.assertEqual(self.route('old-codex')[0].spec.name, old_app.spec.name)
        with mock.patch('covey.app.job_dispatch.check_or_raise') as check, \
                mock.patch.object(BridgeApp, 'process_reply_job', autospec=True) as run:
            self.app.process_reply_job(job, self.ctx)
        self.assertIs(run.call_args.args[0], child)
        self.assertEqual(check.call_args.args[0], 'claude')

    def test_claude_topic_and_session_survive_a_bot_restart(self):
        self.enable()
        _, target = self.route('claude-topic')
        sid = '11111111-1111-4111-8111-111111111111'
        write_session_id(target.session_path, sid)
        target.history_path.write_text('saved conversation\n')
        self.enable()
        child, resumed = self.route('claude-topic')
        self.assertEqual(child.spec.name, 'claude')
        self.assertEqual((resumed.topic_id, resumed.session_path), (target.topic_id, target.session_path))
        self.assertEqual(resumed.session_path.read_text().strip(), sid)
        self.assertEqual(resumed.history_path.read_text(), 'saved conversation\n')

    def test_legacy_codex_topic_is_refused_instead_of_becoming_a_claude_session(self):
        _, old = self.route('legacy')
        data = read_topic(old)
        data.pop('harness_id')
        data.pop('binding_revision')
        write_topic(old, data)
        sid = '11111111-1111-4111-8111-111111111111'
        write_session_id(old.session_path, sid)
        self.enable()
        with self.assertRaises(ValueError):
            self.route('legacy')
        self.assertEqual(read_topic(old)['harness_id'], 'codex')
        self.assertEqual(old.session_path.read_text().strip(), sid)
        self.assertFalse((old.session_path.parent / 'claude_session_id').exists())

    def test_disabled_harness_cannot_run_from_a_saved_card_or_queued_job(self):
        _, target, job = self.queued('old-task')
        remember_topic_message(target.state_dir, target.topic_id, 'chat', 'old-card')
        self.enable()
        event = {'chat_id': 'chat', 'operator_id': 'owner', 'message_id': 'old-card', 'event_id': 'old-click',
                 'action_value': {'kind': 'bridge_job_retry', 'job_id': job.job_id, 'topic_id': target.topic_id}}
        with mock.patch.object(BridgeApp, 'handle_card_action_event') as click, \
                mock.patch.object(BridgeApp, 'process_reply_job') as run, \
                mock.patch.object(detached, 'run_detached') as start:
            self.app.handle_card_action_event(event, self.ctx)
            self.app.process_reply_job(job, self.ctx)
        click.assert_not_called()
        run.assert_not_called()
        start.assert_not_called()
        self.assertEqual(find_job_record(self.ctx.state_dir, 'old-task')['status'], 'interrupted')
        self.assertEqual(read_topic(target)['harness_id'], 'codex')
        self.assertTrue(any(retired_notice('codex') in text for text in self.said()))
        with self.assertRaises(ValueError):
            self.app.topics.select_harness(target, 'chat', 'codex')

    def test_startup_checks_the_configured_default_without_needing_codex(self):
        self.enable()
        app = UnifiedBridgeApp(script_path=REPO / 'bin/covey_bridge.py')
        quiet(app, self.logs)
        self.built.append(app)
        with mock.patch('covey.app.unified.instance_lock'), \
                mock.patch('covey.app.unified.threading.Thread'), \
                mock.patch('covey.app.unified.signal.signal'), \
                mock.patch('covey.feishu.sdk.lark_cli_direct'), \
                mock.patch('covey.feishu.sdk.enabled', return_value=False), \
                mock.patch.object(app, 'init_target'), mock.patch.object(app, 'log_startup'), \
                mock.patch.object(app, 'notify_stale_jobs'), \
                mock.patch.object(app, 'worker_targets', return_value=[('scheduler', mock.Mock())]), \
                mock.patch.object(app, 'consume_loop'), mock.patch.object(Harness, 'ensure_started', autospec=True) as start:
            self.assertEqual(app.main(self.argv), 0)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0].name, 'claude')
        self.assertEqual(start.call_args.args[1].backend_cmd, ['/absolute/bin/claude'])

    def test_card_returned_by_a_finished_runner_uses_the_claude_context(self):
        self.enable()
        child, target, job = self.queued()
        upsert_job_record(target.state_dir, job.message_id, status='running')
        upsert_job_record(target.state_dir, job.message_id, status='done')
        directory = run_files(target.state_dir, job.message_id, folders=(run_dir.INBOX, run_dir.ACKS))
        event = {'chat_id': 'chat', 'message_id': 'late-card', 'event_id': 'late-click'}
        runner_ipc.write_inbox(directory, {'kind': 'card_event', 'event': event})
        with mock.patch.object(self.app, 'handle_card_action_event') as handle:
            detached.finish_detached(self.app, child, job, target, directory)
        handle.assert_called_once_with(event, self.ctx)
        self.assertEqual(handle.call_args.args[1].options['harness_id'], 'claude')
        self.assertEqual(list((directory / run_dir.INBOX).glob('*.json')), [])

    @requires_extension()
    def test_a_harnesss_models_first_reaches_its_context_and_a_bad_one_is_rejected(self):
        app, _, _ = self.configured_bridge(['claude', 'maso'], maso={'args': [], 'models_first': ['gpt-6-astra', 'es1-grape-g61s']})
        self.assertEqual(app.harnesses['maso'].context.options['models_first'], ('gpt-6-astra', 'es1-grape-g61s'))
        self.assertNotIn('models_first', app.harnesses['claude'].context.options)
        for value in ('gpt-6-astra', [1], ['']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'models_first'):
                self.configured_bridge(['claude', 'maso'], maso={'models_first': value})

    def test_invalid_enabled_list_is_rejected_before_startup(self):
        for value in ([], None, 'claude', ['unknown'], ['claude', 'claude'], [1], [{}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.configured_bridge(value)

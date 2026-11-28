"""The butler's session (app/butler.py, docs/butler.md): which messages it takes, where its replies
go, supplements while it runs, its turn's message and tools, and compaction between its turns."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import UnifiedCase
from support.butler import switch_on
from covey.core import env as cenv
from covey.app import butler_harness, compact_runner, detached, idle_compaction, runner_spawn
from covey.app import butler as session
from covey.backends.claude.compaction import command as compaction_command
from covey.backends.claude.argv import stream_argv, turn_env
from covey.core import butler, butler_settings, principles
from covey.core.harness import HarnessCapabilities
from covey.core.checkpoint import CHECKPOINT_RULES, DELIVERY_RULE, IMAGES_RULE, STEPS_RULE, checkpoint_rules
from covey.core.harness import RunResult
from covey.core.jobs import job_key, job_path, read_job_record, upsert_job_record
from covey.core.process import JobCancelled, process_identity
from covey.core.session_state import capture_session_state, compare_and_set_session
from covey.core.steer import SteerRefused
from covey.core.topic_seen import SEEN_FILE
from covey.core.topics import remember_topic_message, route_message, topic_number
from covey.core.state import write_json
from covey.app.pins import NOTICES_KEPT, panel_message, record_path, remember_notice
from covey.core.workspaces import add_workspace
from covey.feishu import message_request

CHAT = 'oc_main'
BUTLER = butler.butler_topic_id(CHAT)


class Control:
    """A running task's backend control."""
    remote = False
    supports_steer = True

    def __init__(self, error=None):
        self.error, self.sent = error, []

    def steer(self, text, mid):
        if self.error:
            raise self.error
        self.sent.append((text, mid))


class ButlerUnifiedCase(UnifiedCase):
    switch = '1'

    def setUp(self):
        switch_on(self, self.switch)
        self.addCleanup(message_request.configure_top_level, None)
        super().setUp()
        self.app.runtime.state_dir = self.ctx.state_dir
        self.cards = iter(range(100))
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(
                side_effect=lambda *a, **k: SimpleNamespace(message_id=f'om_card{next(self.cards)}', card_id='c'))
            app.cards.set_task_element = mock.Mock(return_value=True)
        self.state = self.ctx.state_dir

    def event(self, mid, content='task', root='', **fields):
        return {'chat_id': CHAT, 'chat_type': 'p2p', 'sender_id': 'owner', 'message_id': mid, 'root_id': root,
                'content': content, **fields}

    def send(self, mid, content='task', root='', model_text=None, **fields):
        """The owner's message ``mid``; ``model_text``: what the model reads of it, when not ``content`` (the blocks
        enrich_incoming_event adds: attachments, the quoted message)."""
        event = self.event(mid, content, root, **fields)
        with mock.patch('covey.app.app.enrich_incoming_event') as enrich:
            # The event it was given, as enrich_incoming_event's (routing has named the quote parent_id by then).
            enrich.side_effect = lambda given, *args, **kwargs: SimpleNamespace(
                text=content if model_text is None else model_text, display_text=content, event=given)
            self.app.handle_event(event, self.ctx)

    def butler_jobs(self):
        return self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)

    def job_of(self, mid):
        return self.app.runtime.jobs.find(0, mid)

    def record(self, mid):
        """A job's record: one in a new topic waits for its launch card, not in the registry."""
        return read_job_record(job_path(self.state, mid))

    def settings(self, **changes):
        butler_settings.update(self.state, **changes)

    def topic(self):
        return json.loads((self.state / 'topics' / BUTLER / 'topic.json').read_text())

    def done(self, job):
        """The job's turn ended well, and it left the registry."""
        upsert_job_record(self.state, job.message_id, status='done', topic_id=job.topic_id, chat_id=CHAT, job_id=job.job_id)
        self.app.runtime.jobs.unregister(job)


class RoutingTest(ButlerUnifiedCase):
    def test_a_top_level_message_is_a_task_of_the_butler(self):
        self.send('om_1', 'hello')
        job = self.job_of('om_1')
        self.assertEqual((job.topic_id, job.harness_id, job.reply_to_message_id), (BUTLER, 'claude', 'om_1'))
        topic = self.topic()
        self.assertIs(topic['main_thread'], True)
        self.assertEqual((topic['harness_id'], topic['binding_revision'], topic['root_id']), ('claude', 1, ''))
        self.assertTrue(topic['workspace_locked'] and topic['harness_locked'])
        self.assertNotIn('topic_number', topic)

    def test_it_takes_no_topic_number(self):
        self.send('om_1')
        self.assertEqual(topic_number(self.state, BUTLER), 0)
        self.assertNotIn('topic_number', self.topic())
        self.assertFalse((self.state / 'topics' / 'counter.json').exists())
        self.assertIsNone(self.app.topics.target(self.state, BUTLER, CHAT))

    def open_topic(self, mid='om_t'):
        self.settings(enabled=False)
        self.send(mid)
        self.settings(enabled=True)
        return self.record(mid)['topic_id']

    def test_a_reply_in_a_topic_goes_to_that_topic(self):
        topic = self.open_topic()
        self.assertNotEqual(topic, BUTLER)
        self.send('om_2', root='om_t')
        self.assertEqual(self.record('om_2')['topic_id'], topic)
        self.assertEqual(self.record('om_2')['reply_to_message_id'], 'om_t')

    def test_a_reply_to_a_topics_card_goes_to_that_topic(self):
        topic = self.open_topic()
        remember_topic_message(self.state, topic, CHAT, 'om_topic_card')
        self.send('om_2', parent_id='om_topic_card')
        self.assertEqual(self.record('om_2')['topic_id'], topic)

    def test_a_quote_named_reply_to_reaches_the_topic_its_card_was_routed_to(self):
        # W0-3 (Neon B-P0-1): lark-cli and the WS intake name the quoted message reply_to and leave parent_id empty, and the
        # routes read parent_id alone: quoting the butler's hand-on card (in the owner's message's thread) stayed the butler's.
        topic = self.open_topic()
        self.send('om_1')
        self.done(self.job_of('om_1'))
        route_message(self.state, CHAT, 'om_card', topic)
        self.send('om_2', root='om_1', reply_to='om_card', parent_id=None)
        self.assertEqual(self.record('om_2')['topic_id'], topic)
        self.send('om_3', root='om_1', parent_id='om_card')  # Named parent_id: the same, as before.
        self.assertEqual(self.record('om_3')['topic_id'], topic)

    def test_a_reply_to_the_butlers_message_stays_with_it(self):
        self.send('om_1')
        self.done(self.job_of('om_1'))
        self.send('om_2', root='om_1', parent_id='om_1')
        self.assertEqual(self.job_of('om_2').topic_id, BUTLER)
        self.assertEqual(self.job_of('om_2').reply_to_message_id, 'om_2')

    def test_a_reply_to_a_message_no_topic_knows_opens_a_topic_as_ever(self):
        self.send('om_2', root='om_old', parent_id='om_old')
        self.assertNotEqual(self.record('om_2')['topic_id'], BUTLER)

    def test_a_reply_to_the_pinned_overview_or_a_digest_goes_to_the_butler(self):
        # No topic sent them (the Bridge's own cards): a reply to one is not a new topic on it (docs/ui.md 5.3).
        write_json(record_path(self.state, CHAT, 'pinned-overview.json'), {'chat_id': CHAT, 'message_id': 'om_board'})
        write_json(record_path(self.state, CHAT, 'digest.json'), {'message_id': 'om_d2', 'sent': {'om_d1': {}, 'om_d2': {}}})
        for parent in ('om_board', 'om_d1'):
            self.send('om_r' + parent, root=parent, parent_id=parent)
            self.assertEqual(self.job_of('om_r' + parent).topic_id, BUTLER, parent)
            self.done(self.job_of('om_r' + parent))

    def test_a_quote_named_reply_to_of_the_pinned_overview_stays_with_the_butler(self):
        # WAVE12 RT-7 ① (W1-B regression, W0-3 normalises it upstream: feishu/events.py with_parent in app/card_routing.py):
        # the WS intake's event names the quoted panel card reply_to with parent_id None; app/butler.py app_for reads
        # parent_id, so pins.panel_message sees the card and the message stays the butler's, not a new topic on it.
        write_json(record_path(self.state, CHAT, 'pinned-overview.json'), {'chat_id': CHAT, 'message_id': 'om_board'})
        self.send('om_rb', root='om_mine', reply_to='om_board', parent_id=None)
        self.assertEqual(self.job_of('om_rb').topic_id, BUTLER)

    def test_a_reply_to_an_alert_at_the_top_level_goes_to_the_butler(self):
        """2026-11-21 T622: a reply to a disk alert at the top level of the private chat opened a topic on the alert, whose task
        waited 21 minutes for 开始 on its launch card in the alert's thread. An alert the Bridge sent at the top level is one of
        its own cards, as 手上的事 is (app/pins.py remember_notice): a reply to it is the butler's, which starts on it as its
        settings say; a message no one kept is a topic's as ever."""
        self.assertTrue(remember_notice(self.state, CHAT, 'om_alert'))
        self.send('om_ra', root='om_alert', parent_id='om_alert')
        self.assertEqual(self.job_of('om_ra').topic_id, BUTLER)
        self.done(self.job_of('om_ra'))
        self.send('om_rb', root='om_other_alert', parent_id='om_other_alert')
        self.assertNotEqual(self.record('om_rb')['topic_id'], BUTLER)
        # The record keeps the newest NOTICES_KEPT; ids that are no message are not kept.
        for n in range(NOTICES_KEPT):
            remember_notice(self.state, CHAT, f'om_n{n}')
        self.assertFalse(panel_message(self.state, CHAT, 'om_alert'))
        self.assertTrue(panel_message(self.state, CHAT, f'om_n{NOTICES_KEPT - 1}'))
        self.assertFalse(remember_notice(self.state, CHAT, ''))
        self.assertFalse(remember_notice(self.state, CHAT, 'not-a-message'))

    def test_a_group_message_is_not_taken(self):
        self.send('om_1')  # The owner's private chat is known.
        event = {**self.event('om_g'), 'chat_type': 'group'}
        self.assertIsNone(self.app.butler.app_for(self.ctx, event, True))

    def test_another_chat_or_sender_is_not_taken(self):
        self.send('om_1')
        self.assertIsNone(self.app.butler.app_for(self.ctx, {**self.event('om_x'), 'chat_id': 'oc_other'}, True))
        self.assertIsNone(self.app.butler.app_for(self.ctx, self.event('om_x'), False))

    def test_settings_off_opens_a_topic_as_ever(self):
        self.settings(enabled=False)
        self.send('om_1')
        self.assertNotEqual(self.record('om_1')['topic_id'], BUTLER)
        self.assertFalse((self.state / 'topics' / BUTLER).exists())

    def test_claude_code_unavailable_opens_a_topic_as_ever(self):
        self.app.apps['claude'].harness.startup_failure = (time.monotonic(), 'down')
        self.assertIsNone(self.app.butler.app_for(self.ctx, self.event('om_1'), True))

    def test_the_topic_follows_the_settings(self):
        self.send('om_1')
        self.assertEqual((self.topic()['model'], self.topic()['effort']), ('', ''))
        self.done(self.job_of('om_1'))
        self.settings(model='claude-opus-5-5', effort='high')
        self.send('om_2')
        self.assertEqual((self.topic()['model'], self.topic()['effort']), ('claude-opus-5-5', 'high'))
        self.assertEqual(self.job_of('om_2').model, 'claude-opus-5-5')
        self.assertEqual(self.topic()['binding_revision'], 1)

    def test_a_budget_without_a_model_runs_on_claude_codes_default_model(self):
        """ACCEPT-325 问题 1: 私聊默认's 思考预算 with 模型 默认 goes into the topic.json, and the turn runs with it."""
        self.settings(effort='max')
        self.send('om_1')
        self.assertEqual((self.topic()['model'], self.topic()['effort']), ('', 'max'))
        job = self.job_of('om_1')
        self.assertEqual((job.model, job.effort), ('', 'max'))

    def another_workspace(self):
        other = self.root / 'other'
        other.mkdir()
        add_workspace(self.state, self.ctx.default_workdir, str(other))
        self.settings(workspace='other')
        return other

    def test_another_workspace_in_the_settings_never_moves_a_butler_that_runs(self):
        """主目录启动 (docs/configuration.md 主目录与仓库地图): no card sets the butler's workspace any more, and a butler that
        has run keeps its directory and its session whatever an older release saved; a new one starts there."""
        self.send('om_1')
        first = (self.topic()['workspace_name'], self.topic()['workdir'])
        pointer = self.state / 'topics' / BUTLER / self.app.apps['claude'].spec.session_filename
        pointer.write_text('11111111-1111-4111-8111-111111111111\n')
        other = self.another_workspace()
        self.done(self.job_of('om_1'))
        self.send('om_2')
        self.assertEqual((self.topic()['workspace_name'], self.topic()['workdir']), first)
        self.assertEqual(self.topic()['binding_revision'], 1)
        self.assertEqual(self.app.apps['claude'].sessions.read_session_id(pointer), '11111111-1111-4111-8111-111111111111')
        self.assertEqual(butler_harness.workspace_of(self.state, self.ctx.default_workdir, butler_settings.load(self.state)),
                         ('other', str(other.resolve())))  # Where a butler that never ran would start.

    def test_another_harness_waits_until_nothing_runs_and_starts_a_new_session(self):
        """T614: the butler's Harness is a setting. A turn under way keeps the one it has; once nothing runs the topic
        is bound to the new one, on a new session (a pointer it had from before is behind the conversation)."""
        self.send('om_1')
        self.assertEqual(self.topic()['harness_id'], 'claude')
        codex = self.app.apps['codex']
        stale = self.state / 'topics' / BUTLER / codex.spec.session_filename
        stale.write_text('01a1fcc9-38c1-7272-91fa-7be36d477776\n')
        self.settings(harness='codex', model='', effort='')
        self.assertEqual(self.app.butler.keep_topic(self.ctx, codex, CHAT), 'claude')  # A job is here.
        self.assertEqual(self.topic()['harness_id'], 'claude')
        self.done(self.job_of('om_1'))
        self.send('om_2')
        self.assertEqual((self.topic()['harness_id'], self.topic()['binding_revision']), ('codex', 2))
        job = self.job_of('om_2')
        self.assertEqual((job.harness_id, job.binding_revision), ('codex', 2))
        self.assertIsNone(codex.sessions.read_session_id(stale))
        self.done(job)
        self.settings(harness='')  # Back to Claude Code, the default.
        self.send('om_3')
        self.assertEqual((self.topic()['harness_id'], self.job_of('om_3').harness_id), ('claude', 'claude'))

    def test_a_harness_that_cannot_take_the_bridge_tools_leaves_the_messages_to_topics(self):
        self.send('om_0')  # The owner's private chat is known.
        self.settings(harness='codex')
        with mock.patch.object(type(self.app.apps['codex'].backend), 'capabilities',
                               return_value=HarnessCapabilities(bridge_tools=False)):
            self.assertIsNone(self.app.butler.app_for(self.ctx, self.event('om_1'), True))
        self.assertIs(self.app.butler.app_for(self.ctx, self.event('om_1'), True), self.app.apps['codex'])

    def test_an_old_redaction_flag_in_its_topic_json_is_left_alone(self):
        """敏感词脱敏 is retired (T947): the butler's topic.json is no longer written with or without ``redaction``; a file
        that still has it keeps it as it is (not read by anything)."""
        self.send('om_1')
        self.assertNotIn('redaction', self.topic())
        write_json(self.state / 'topics' / BUTLER / 'topic.json', {**self.topic(), 'redaction': True})
        self.app.butler.keep_topic(self.ctx, self.app.apps['claude'], CHAT)
        self.assertIs(self.topic()['redaction'], True)

    def test_the_overview_does_not_list_it(self):
        self.send('om_1')
        upsert_job_record(self.state, 'om_1', status='running', topic_id=BUTLER, chat_id=CHAT, job_id=1)
        upsert_job_record(self.state, 'om_o', status='running', topic_id='a' * 32, chat_id=CHAT, job_id=2)
        view = self.app.overview.view(self.ctx, CHAT, time.time())
        self.assertEqual([row['topic'] for row in view['running']], ['a' * 32])

    def test_its_cards_offer_no_rewind_or_switch(self):
        self.send('om_1')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event('om_1'))
        self.assertFalse(app.switch.can_rewind(target))
        job.progress = job.progress or SimpleNamespace(message_id='om_card')
        job.switch_controls_attached = job.topic_controls_attached = False
        app.cards.set_task_element = mock.Mock(return_value=True)
        app.switch.attach_switch_controls(target, job)
        app.cards.set_task_element.assert_not_called()
        # Nor is there a panel under a finished turn: the settings card holds the butler's 换配置, and it
        # cannot rewind.
        app.switch.attach_topic_controls(target, job)
        app.cards.set_task_element.assert_not_called()

    def test_a_switch_click_on_an_older_card_changes_nothing(self):
        # Cards sent before 换配置 left the butler's panel still have it: the click finds no topic it can
        # route (the butler has no root message) and does nothing.
        self.send('om_1')
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event('om_1'))
        path = self.ctx.state_dir / 'topics' / target.topic_id / 'topic.json'
        topic = json.loads(path.read_text())
        raw = {'kind': 'bridge_switch', 'topic_id': target.topic_id, 'job_id': 1, 'harness_id': topic['harness_id'],
               'binding_revision': topic.get('binding_revision', 1)}
        self.app.messenger.create_and_send_card = mock.Mock()
        self.app.harness_switch.open(self.ctx, {'chat_id': CHAT, 'event_id': 'ev1'}, raw)
        self.app.messenger.create_and_send_card.assert_not_called()
        self.assertEqual(json.loads(path.read_text()), topic)

    def test_the_topic_panel_can_leave_switch_out(self):
        from covey.cards.topic_actions import topic_panel
        self.assertIn('switch_action', json.dumps(topic_panel('t', 1, ('claude', 1), rewind=True)))
        panel = topic_panel('t', 1, ('claude', 1), rewind=True, switch=False)
        self.assertNotIn('switch_action', json.dumps(panel))
        self.assertIn('rewind_action', json.dumps(panel))
        self.assertIsNone(topic_panel('t', 1, ('claude', 1), switch=False))


class ReplyPlaceTest(ButlerUnifiedCase):
    def argv(self, reply_to, chat=CHAT):
        return message_request.message_argv('p', chat_id=chat, reply_to=reply_to, flag='--text', value='x', key='k')

    def test_a_reply_to_its_messages_is_in_their_thread(self):
        # 2026-11-08 (the owner): the butler's card goes in the thread of the owner's message, not beside it.
        self.send('om_1')
        argv = self.argv('om_1')
        self.assertIn('--reply-in-thread', argv)
        self.assertEqual(argv[argv.index('--message-id') + 1], 'om_1')
        self.assertTrue(message_request.from_argv(argv)[3]['reply_in_thread'])
        remember_topic_message(self.state, BUTLER, CHAT, 'om_card')
        self.assertIn('--reply-in-thread', self.argv('om_card'))

    def test_desk_topics_left_true_changes_nothing(self):
        # Retired on 2026-11-09: a main-thread.json that still says true is read as false.
        self.send('om_1')
        (self.state / butler_settings.FILE).write_text(json.dumps({'mode': 'agent', 'desk_topics': True}))
        self.assertIn('--reply-in-thread', self.argv('om_1'))
        remember_topic_message(self.state, BUTLER, CHAT, 'om_card')
        self.assertIn('--reply-in-thread', self.argv('om_card'))
        self.settings(compact_tokens=1000)  # A save keeps the value, for an older release.
        self.assertIs(json.loads((self.state / butler_settings.FILE).read_text())['desk_topics'], True)
        self.assertTrue(butler_settings.desk_topics_left(self.state))  # The Bridge's start warns once.
        self.assertTrue(butler_settings.DESK_TOPICS_RETIRED.startswith('warning: main-thread.json desk_topics'))
        (self.state / butler_settings.FILE).write_text(json.dumps({'mode': 'agent', 'desk_topics': False}))
        self.assertFalse(butler_settings.desk_topics_left(self.state))

    def test_a_reply_to_anything_else_is_in_its_thread(self):
        self.send('om_1')
        self.assertIn('--reply-in-thread', self.argv('om_other'))
        remember_topic_message(self.state, 'a' * 32, CHAT, 'om_topic')
        self.assertIn('--reply-in-thread', self.argv('om_topic'))
        self.assertTrue(message_request.from_argv(self.argv('om_topic'))[3]['reply_in_thread'])

    def test_a_task_runner_builds_the_same(self):
        self.send('om_1')
        message_request.configure_top_level(None)
        self.bridge()  # build_context, as app/runner.py does.
        self.assertIn('--reply-in-thread', self.argv('om_1'))


class SwitchOffTest(ButlerUnifiedCase):
    switch = ''

    def test_a_top_level_message_opens_a_topic_and_replies_are_in_threads(self):
        self.send('om_1')
        self.assertNotEqual(self.record('om_1')['topic_id'], BUTLER)
        self.assertFalse((self.state / 'topics' / BUTLER).exists())
        self.assertFalse((self.state / butler_settings.FILE).exists())
        argv = message_request.message_argv('p', chat_id=CHAT, reply_to='om_1', flag='--text', value='x', key='k')
        self.assertIn('--reply-in-thread', argv)

    def test_a_turn_is_unchanged(self):
        job = SimpleNamespace(topic_id=BUTLER, chat_id=CHAT)  # Even of the topic, should it exist.
        ctx, prompt = session.butler_turn(self.ctx, job, 'MSG', None, mock.Mock(), self.logs.append)
        self.assertIs(ctx, self.ctx)
        self.assertEqual(prompt, 'MSG')


class PrivateModeTest(ButlerUnifiedCase):
    """The private chat mode (docs/butler.md 私聊模式) with the switch allowing the butler, not saying 1."""
    switch = ''

    def setUp(self):
        super().setUp()
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        self.settings(enabled=True)  # 管家.

    def argv(self, reply_to):
        return message_request.message_argv('p', chat_id=CHAT, reply_to=reply_to, flag='--text', value='x', key='k')

    def test_personal_agent_needs_no_1(self):
        self.send('om_1')
        self.assertEqual(self.job_of('om_1').topic_id, BUTLER)
        self.assertIn('--reply-in-thread', self.argv('om_1'))  # The butler's card in the message's thread.

    def test_in_the_mode_topics_a_quote_of_a_butler_card_opens_its_own_topic(self):
        self.send('om_1')
        self.done(self.job_of('om_1'))
        remember_topic_message(self.state, BUTLER, CHAT, 'om_card')
        self.settings(enabled=False)
        self.send('om_2', root='om_1', parent_id='om_card')
        record = self.record('om_2')
        self.assertNotEqual(record['topic_id'], BUTLER)
        self.assertEqual(record['reply_to_message_id'], 'om_2')
        topic = json.loads((self.state / 'topics' / record['topic_id'] / 'topic.json').read_text())
        self.assertEqual((topic['root_id'], topic['thread_id']), ('om_2', ''))
        # The butler's messages stay its own; every reply is in its thread, as without the butler.
        self.assertEqual([session.message_topic(self.state, CHAT, m) for m in ('om_1', 'om_card')], [BUTLER, BUTLER])
        self.assertIn('--reply-in-thread', self.argv('om_card'))
        self.assertIn('--reply-in-thread', self.argv('om_2'))
        self.send('om_t', root='om_2')  # In that topic's thread: that topic's.
        self.assertEqual(self.record('om_t')['topic_id'], record['topic_id'])
        self.settings(enabled=True)  # Back: the same quote is the butler's again.
        self.send('om_3', root='om_1', parent_id='om_card')
        self.assertEqual(self.job_of('om_3').topic_id, BUTLER)

    def test_the_start_settles_the_mode_once_and_not_when_ruled_out(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ctx = SimpleNamespace(state_dir=state)
        with mock.patch.dict(os.environ, {butler.ENV: '0'}):
            session.Butler.settle(ctx, self.logs.append)
        self.assertFalse((state / butler_settings.FILE).exists())
        session.Butler.settle(ctx, self.logs.append)
        session.Butler.settle(ctx, self.logs.append)
        self.assertEqual([line for line in self.logs if 'mode' in line], ['main thread mode settled mode=agent'])
        with mock.patch.object(butler_settings, 'settle_mode', side_effect=OSError('disk')):
            session.Butler.settle(ctx, self.logs.append)
        self.assertEqual(self.logs[-1], 'main thread mode not settled: OSError: disk')


class SupplementTest(ButlerUnifiedCase):
    def running(self, control):
        self.send('om_1')
        job = self.job_of('om_1')
        job.execution_started = True
        job.backend_control = control
        return job

    def test_it_joins_the_running_turn_with_no_receipt(self):
        control = self.running(Control()).backend_control
        self.app.messenger.send_message.reset_mock()
        self.send('om_2', 'also this')
        self.assertEqual(control.sent, [('also this', 'om_2')])
        self.assertEqual(len(self.butler_jobs()), 1)
        self.app.messenger.send_message.assert_not_called()
        history = (self.state / 'topics' / BUTLER / 'history.jsonl').read_text()
        self.assertIn('also this', history)

    def test_while_it_starts_it_is_kept_for_the_turn(self):
        self.send('om_1')
        job = self.job_of('om_1')
        job.execution_started = True  # No backend control yet.
        with mock.patch.object(self.app.apps['claude'].steering, 'hold_steer') as hold:
            self.send('om_2', 'soon')
        hold.assert_called_once()
        self.assertEqual(len(self.butler_jobs()), 1)

    def test_one_the_turn_refuses_queues_behind_it_with_no_receipt(self):
        self.running(Control(SteerRefused('任务正在收尾')))
        self.app.messenger.send_message.reset_mock()
        self.send('om_2', 'next')
        self.assertEqual([job.message_id for job in self.butler_jobs()], ['om_1', 'om_2'])
        self.app.messenger.send_message.assert_not_called()

    def test_a_paused_turn_queues_it(self):
        control = Control()
        control.pause_for_question = 'q'
        self.running(control)
        self.send('om_2', 'next')
        self.assertEqual(control.sent, [])
        self.assertEqual([job.message_id for job in self.butler_jobs()], ['om_1', 'om_2'])

    def test_one_that_may_have_arrived_is_not_queued_or_sent_again(self):
        control = Control(RuntimeError('lost'))
        self.running(control)
        self.app.messenger.send_message.reset_mock()
        self.send('om_2', 'next')
        self.assertEqual(len(self.butler_jobs()), 1)
        self.app.messenger.send_message.assert_called_once()
        args, kwargs = self.app.messenger.send_message.call_args
        self.assertEqual(args[2], session.SUPPLEMENT_UNCERTAIN)
        self.assertEqual(kwargs['reply_to_message_id'], 'om_2')

    def test_a_queued_turn_takes_it_with_its_request(self):
        self.send('om_1', 'first')
        job = self.job_of('om_1')
        with mock.patch.object(self.app.apps['claude'].steering, 'note_latest_request'):
            self.send('om_2', 'second')
        self.assertEqual(len(self.butler_jobs()), 1)
        self.assertIn('second', job.prompt_text)

    def target(self, mid='om_2'):
        claude = self.app.apps['claude']
        return claude.submission.event_context(claude.harness.context, self.event(mid))

    def lines(self):
        """What the Bridge said under the owner's messages: (text, the message it quoted)."""
        return [(c.args[2], c.kwargs.get('reply_to_message_id')) for c in self.app.messenger.send_message.call_args_list]

    def delivery(self, mid):
        from covey.core.deliveries import delivery_path, read_delivery
        return read_delivery(delivery_path(self.state, BUTLER, mid))

    def held(self, *mids):
        """The turn om_1 is starting (no backend control yet): each of ``mids`` is kept for it."""
        self.send('om_1')
        job = self.job_of('om_1')
        job.execution_started = True
        steering = self.app.apps['claude'].steering

        def hold(ctx, job, text, mid, *, saved=None):
            job.held_steers[mid] = text
            saved()
        with mock.patch.object(steering, 'hold_steer', side_effect=hold):
            for mid in mids:
                self.send(mid, 'more ' + mid)
        return job

    def synchronous(self):
        return mock.patch('covey.app.steering.background.start', side_effect=lambda _, fn, *a: fn(*a))

    def test_one_held_when_the_turn_ends_becomes_a_turn_of_its_own(self):
        # ACCEPT-343-A s2: it was a SUPPLEMENT_LOST line in a thread under the message, and the message was lost.
        job = self.held('om_2')
        self.assertEqual(self.delivery('om_2')['status'], 'held')
        self.app.messenger.send_message.reset_mock()
        self.done(job)
        with self.synchronous():
            self.app.apps['claude'].steering.drop_held_steers(self.target('om_1'), job)
        again = self.job_of('om_2')
        self.assertIsNotNone(again)
        self.assertEqual((again.message_id, again.reply_to_message_id, again.topic_id), ('om_2', 'om_2', BUTLER))
        self.assertEqual((again.user_text, again.prompt_text, again.sender_id), ('more om_2', 'more om_2', 'owner'))
        self.assertEqual([j.message_id for j in self.butler_jobs()], ['om_2'])
        self.assertNotIn(session.SUPPLEMENT_LOST, [text for text, _ in self.lines()])
        # Placed as a new message is: no card yet in the 话题区 layout, OnIt on the owner's message itself.
        self.assertEqual((again.card_deferred, job.card_deferred), (True, True))
        self.assertIn(('om_2', 'OnIt'), [call.args[1:3] for call in self.app.messenger.react.call_args_list])
        record = self.delivery('om_2')
        self.assertEqual((record['status'], record['requeue_state'], record['requeued_job_id']),
                         ('refused', 'queued', again.job_id))

    def test_one_the_ended_turn_never_read_becomes_a_turn_of_its_own(self):
        job = self.running(Control())
        self.send('om_2', 'more')
        self.app.messenger.send_message.reset_mock()
        job.timings['backend_requested'] = 0
        with self.synchronous():
            self.app.apps['claude'].steering.end_unread_steers(self.target('om_1'), job)
        again = self.job_of('om_2')
        self.assertIsNotNone(again)
        self.assertEqual((again.reply_to_message_id, again.prompt_text), ('om_2', 'more'))
        self.assertEqual([j.message_id for j in self.butler_jobs()], ['om_1', 'om_2'])  # Behind the turn still here.
        self.assertEqual(self.lines(), [])
        self.assertTrue(self.delivery('om_2')['target_ended'])

    def test_each_is_queued_once_even_after_a_restart(self):
        job = self.held('om_2')
        self.done(job)
        steering = self.app.apps['claude'].steering
        with self.synchronous():
            steering.drop_held_steers(self.target('om_1'), job)
            again = self.job_of('om_2')
            self.app.runtime.jobs.unregister(again)  # A restart: the registry is empty, the records stay.
            self.assertEqual(steering.queue_lost(self.target('om_1'), job), [])
            detached.queue_lost(self.app.apps['claude'], job, self.target('om_1'))
            steering.settle_steer(self.target('om_1'), job, 'om_2', status='refused', error='again')
        self.assertEqual(self.butler_jobs(), [])
        self.assertEqual(self.delivery('om_2')['requeued_job_id'], again.job_id)
        self.assertEqual(self.lines(), [])

    def test_the_same_message_again_is_not_a_supplement_a_second_time(self):
        job = self.held('om_2')
        self.done(job)
        with self.synchronous():
            self.app.apps['claude'].steering.drop_held_steers(self.target('om_1'), job)
        again = self.job_of('om_2')
        again.execution_started, again.backend_control = True, Control()
        self.send('om_2', 'more om_2')  # Feishu sends the event again: its turn is already queued.
        self.assertEqual(again.backend_control.sent, [])
        self.assertEqual(self.butler_jobs(), [again])

    def test_several_queue_in_the_order_they_came(self):
        job = self.held('om_2', 'om_3')
        self.done(job)
        with self.synchronous():
            self.app.apps['claude'].steering.drop_held_steers(self.target('om_1'), job)
        jobs = sorted(self.butler_jobs(), key=lambda j: j.job_id)
        self.assertEqual([j.message_id for j in jobs], ['om_2', 'om_3'])

    def test_a_task_runner_marks_them_and_its_bridge_queues_them(self):
        job = self.held('om_2')
        self.done(job)
        claude = self.app.apps['claude']
        with self.synchronous():
            with mock.patch.object(type(self.app.runtime), 'in_runner', new_callable=mock.PropertyMock,
                                   return_value=True):
                claude.steering.drop_held_steers(self.target('om_1'), job)
                self.assertEqual(claude.steering.queue_lost(self.target('om_1'), job), [])
            self.assertIsNone(self.job_of('om_2'))
            self.assertTrue(self.delivery('om_2')['requeue_auto'])
            detached.queue_lost(claude, job, self.target('om_1'))  # The Bridge, once the runner is done.
        self.assertEqual(self.job_of('om_2').reply_to_message_id, 'om_2')
        self.assertEqual(self.lines(), [])

    def ended(self, job, *, runner=False):
        """``job``'s turn ends with ``om_2`` still held (it came after the result): its process_reply_job returns."""
        upsert_job_record(self.state, job.message_id, status='done', topic_id=job.topic_id, chat_id=CHAT,
                          job_id=job.job_id)
        claude = self.app.apps['claude']
        in_runner = mock.patch.object(type(self.app.runtime), 'in_runner', new_callable=mock.PropertyMock,
                                      return_value=runner)
        with self.synchronous(), in_runner, mock.patch('covey.app.app.process_bridge_reply_job'):
            claude.process_reply_job(job, claude.harness.context)

    def test_one_held_after_the_result_becomes_a_turn_as_the_job_leaves(self):
        # ACCEPT-344-A L7: held after the result, it stayed held once the runner exited; no turn, no word.
        job = self.held('om_2')
        self.app.messenger.send_message.reset_mock()
        self.ended(job)
        self.assertEqual(job.held_steers, {})
        record = self.delivery('om_2')
        self.assertEqual((record['status'], record['requeue_state'], record['error']),
                         ('refused', 'queued', '任务已经在收尾，这条补充没有发出'))
        self.assertEqual(self.job_of('om_2').reply_to_message_id, 'om_2')
        self.assertEqual(self.lines(), [])

    def test_one_held_at_the_end_of_a_stopped_turn_its_card_listed_gets_no_turn_and_no_line(self):
        job = self.held('om_2')
        job.cancel_event.set()
        job.supplements_listed = True  # app/butler_reply.py show_dropped listed it under the stopped card.
        self.app.messenger.send_message.reset_mock()
        self.ended(job)
        self.assertEqual(self.delivery('om_2')['status'], 'refused')
        self.assertIsNone(self.job_of('om_2'))
        self.assertEqual(self.lines(), [])

    def test_in_a_task_runner_one_held_at_the_end_is_marked_for_its_bridge(self):
        job = self.held('om_2')
        self.ended(job, runner=True)
        self.assertIsNone(self.job_of('om_2'))
        self.assertTrue(self.delivery('om_2')['requeue_auto'])
        with self.synchronous():
            detached.queue_lost(self.app.apps['claude'], job, self.target('om_1'))  # The Bridge, once it is done.
        self.assertEqual(self.job_of('om_2').reply_to_message_id, 'om_2')
        self.assertEqual(self.lines(), [])

    def test_marked_by_a_runner_gone_while_the_bridge_was_down_they_queue_at_its_start_once(self):
        # Review ra C1 (r4): the runner marked them and exited before a Bridge came back, so none adopted it.
        job = self.held('om_2', 'om_3')
        self.done(job)
        claude = self.app.apps['claude']
        with self.synchronous(), mock.patch.object(type(self.app.runtime), 'in_runner',
                                                   new_callable=mock.PropertyMock, return_value=True):
            claude.steering.drop_held_steers(self.target('om_1'), job)
        self.assertIsNone(self.job_of('om_2'))
        self.app.messenger.send_message.reset_mock()
        self.app.runtime.adopted_keys = {job.message_id}  # Its runner still there: queued once it ends, not now.
        detached.queue_stranded(self.app, self.ctx, claude)
        self.assertIsNone(self.job_of('om_2'))
        self.app.notify_stale_jobs(self.ctx)  # The next Bridge's start: no runner to adopt.
        jobs = sorted(self.butler_jobs(), key=lambda j: j.job_id)
        self.assertEqual([(j.message_id, j.reply_to_message_id) for j in jobs], [('om_2', 'om_2'), ('om_3', 'om_3')])
        self.assertEqual(jobs[0].prompt_text, 'more om_2')
        self.assertEqual(self.lines(), [])
        ids = {mid: self.delivery(mid)['requeued_job_id'] for mid in ('om_2', 'om_3')}
        self.assertEqual(ids, {'om_2': jobs[0].job_id, 'om_3': jobs[1].job_id})
        for again in jobs:
            self.app.runtime.jobs.unregister(again)
        with mock.patch.object(claude.steering, '_requeue') as requeue:
            self.app.notify_stale_jobs(self.ctx)  # Started once more: none queued a second time.
        requeue.assert_not_called()
        self.assertEqual({mid: self.delivery(mid)['requeued_job_id'] for mid in ('om_2', 'om_3')}, ids)

    def test_marked_in_a_bridge_that_stopped_before_queuing_them_they_queue_at_its_next_start(self):
        job = self.held('om_2')
        self.done(job)
        with mock.patch('covey.app.steering.background.start'):  # queue_lost never ran: the Bridge stopped.
            self.app.apps['claude'].steering.drop_held_steers(self.target('om_1'), job)
        self.assertIsNone(self.job_of('om_2'))
        self.assertTrue(self.delivery('om_2')['requeue_auto'])
        self.app.messenger.send_message.reset_mock()
        self.app.notify_stale_jobs(self.ctx)
        self.assertEqual(self.job_of('om_2').reply_to_message_id, 'om_2')
        self.assertEqual(self.lines(), [])

    def test_one_whose_queuing_failed_is_said_once_and_never_queued(self):
        # Review ra P1 (r4): it may be queued (rule 6), so not again; the owner hears so once.
        job = self.held('om_2')
        self.done(job)
        claude = self.app.apps['claude']
        self.app.messenger.send_message.reset_mock()
        with self.synchronous(), mock.patch.object(claude.steering, 'enqueue_chat_job', side_effect=RuntimeError('x')):
            claude.steering.drop_held_steers(self.target('om_1'), job)
        self.assertIsNone(self.job_of('om_2'))
        self.assertEqual(self.delivery('om_2')['requeue_state'], 'uncertain')
        self.assertEqual(self.lines(), [(session.SUPPLEMENT_UNCERTAIN, 'om_2')])
        with self.synchronous():
            detached.queue_lost(claude, job, self.target('om_1'))
            self.app.notify_stale_jobs(self.ctx)
        self.assertIsNone(self.job_of('om_2'))
        self.assertEqual(self.lines(), [(session.SUPPLEMENT_UNCERTAIN, 'om_2')])

    def test_one_queued_as_its_own_turn_is_not_asked_by_the_turn_that_ended(self):
        # Review ra P2 (r4): its own turn's "你问" has it; the ended turn's, read again later, does not.
        from covey.app.butler_handoff import asked
        job = self.running(Control())
        self.send('om_2', 'more')
        self.assertIn('more', [words for words, _ in asked(self.state, 'om_1').said])
        job.timings['backend_requested'] = 0
        with self.synchronous():
            self.app.apps['claude'].steering.end_unread_steers(self.target('om_1'), job)
        self.assertIsNotNone(self.job_of('om_2'))
        self.assertNotIn('more', [words for words, _ in asked(self.state, 'om_1').said])

    def test_one_that_may_have_arrived_is_said_not_queued(self):
        job = self.held('om_2')
        self.app.messenger.send_message.reset_mock()
        with self.synchronous():
            self.app.apps['claude'].steering.settle_steer(self.target('om_1'), job, 'om_2', status='uncertain',
                                                          error='后端没有确认收到')
        self.assertEqual(self.lines(), [(session.SUPPLEMENT_LOST, 'om_2')])
        self.assertIsNone(self.job_of('om_2'))

    def test_after_a_stop_it_is_said_not_queued(self):
        job = self.held('om_2')
        self.app.messenger.send_message.reset_mock()
        job.cancel_event.set()
        self.done(job)
        with self.synchronous():
            self.app.apps['claude'].steering.drop_held_steers(self.target('om_1'), job)
        self.assertEqual(self.lines(), [(session.SUPPLEMENT_LOST, 'om_2')])
        self.assertIsNone(self.job_of('om_2'))

    def test_a_record_from_before_is_said_as_before(self):
        job = self.running(Control())
        self.send('om_2', 'more')
        from covey.core.deliveries import delivery_path, read_delivery, save_delivery
        path = delivery_path(self.state, BUTLER, 'om_2')
        record = read_delivery(path)
        record.pop('requeue')  # Saved by a Bridge from before (no requeue for the butler's).
        save_delivery(path, record)
        self.app.messenger.send_message.reset_mock()
        job.timings['backend_requested'] = 0
        with self.synchronous():
            self.app.apps['claude'].steering.end_unread_steers(self.target('om_1'), job)
        self.assertEqual(self.lines(), [(session.SUPPLEMENT_LOST, 'om_2')])
        self.assertIsNone(self.job_of('om_2'))

    def test_a_supplement_its_stopped_card_listed_gets_no_line_of_its_own(self):
        job = self.running(Control())
        self.send('om_2', 'more')
        steering = self.app.apps['claude'].steering
        target = self.app.apps['claude'].submission.event_context(self.app.apps['claude'].harness.context, self.event('om_2'))
        self.app.messenger.send_message.reset_mock()
        with mock.patch('covey.app.steering.background.start', side_effect=lambda _, fn, *a: fn(*a)):
            job.timings['backend_requested'] = 0
            steering.end_unread_steers(target, job, listed=True)
        self.app.messenger.send_message.assert_not_called()
        self.assertIsNone(self.job_of('om_2'))  # The card said they stopped with it: no turn of their own either.


class HoldTest(ButlerUnifiedCase):
    """A message with no words of its own waits up to 30 s for the owner's words (REVIEW-1106 §1.1: a forward's
    留言 arrives as its own text 1-2 s later)."""

    FORWARD = dict(message_type='interactive')

    def setUp(self):
        super().setUp()
        self.notes = mock.patch.object(self.app.apps['claude'].steering, 'note_latest_request')
        self.notes.start()
        self.addCleanup(self.notes.stop)

    def held(self, job):
        return job.start_held_until - time.time()

    def test_what_has_no_words_of_its_own(self):
        bare = session.bare_message
        for kind, content in (('interactive', '{}'), ('merge_forward', 'x'), ('image', '{}'), ('file', '{}'),
                              ('media', '{}'), ('text', json.dumps({'text': 'https://a.example/x'})),
                              ('text', json.dumps({'text': 'https://a.example/x\n https://b.example/y'})),
                              ('post', json.dumps({'content': [[{'tag': 'img', 'image_key': 'k'}]]})),
                              ('post', json.dumps({'content': [[{'tag': 'a', 'text': 'https://q.example', 'href': 'https://q.example'}]]}))):
            self.assertTrue(bare({'message_type': kind, 'content': content}), kind + content)
        for kind, content in (('text', json.dumps({'text': '看看 https://a.example/x'})), ('text', json.dumps({'text': 'hi'})),
                              ('post', json.dumps({'content': [[{'tag': 'text', 'text': '做这个'}, {'tag': 'img', 'image_key': 'k'}]]})),
                              ('', 'x'), ('text', '')):
            self.assertFalse(bare({'message_type': kind, 'content': content}), kind + content)

    def test_a_forward_alone_is_queued_but_held_and_its_record_says_so(self):
        # The 话题区 layout sends no card yet (QuietTest): its record says why it waits.
        self.send('om_1', '{}', **self.FORWARD)
        job = self.job_of('om_1')
        self.assertGreater(self.held(job), butler.HOLD_SECONDS - 5)
        self.app.apps['claude'].cards.create_streaming_reply.assert_not_called()
        self.assertEqual(self.record('om_1')['status_message'], butler.HOLD_STATUS)
        self.assertEqual(butler.HOLD_STATUS, '收到，等你说要做什么（最多 30 秒）')

    def test_the_owners_words_join_its_request_and_start_it_at_once(self):
        self.send('om_1', '[转发的卡片]', **self.FORWARD)
        job = self.job_of('om_1')
        self.send('om_2', '帮我看看这张卡', message_type='text')
        self.assertEqual(self.butler_jobs(), [job])
        self.assertIn('帮我看看这张卡', job.prompt_text)
        self.assertEqual(job.start_held_until, 0)

    def test_the_you_asked_block_shows_the_words_the_owner_added(self):
        # S7 (T444, 2026-11-08): the block showed the link that started the turn, not what he asked.
        from covey.app import answer_cards
        from covey.app.runner_spawn import dump_job, load_job
        from covey.core.jobs import find_job_record
        self.send('om_1', json.dumps({'text': 'https://x.example/doc'}), message_type='text')
        job = self.job_of('om_1')
        self.assertGreater(self.held(job), 0)
        self.send('om_2', '帮我看看这个文档', message_type='text')
        state = self.ctx.state_dir
        with mock.patch.object(answer_cards, 'ask_link', return_value='https://link/om_1') as link:
            block = answer_cards.asked(state, job)
            record_block = answer_cards.asked(state, find_job_record(state, job.message_id))
        self.assertIn('帮我看看这个文档', str(block))
        self.assertNotIn('x.example', str(block))
        self.assertIn('帮我看看这个文档', str(record_block))  # What a relay of a topic it opened reads (ask_for).
        self.assertEqual(find_job_record(state, job.message_id)['asked_text'], '帮我看看这个文档')
        self.assertEqual(link.call_args.args[3], 'om_1')  # The link still opens the first message's thread.
        # A task runner's handoff carries it (an "extras" field: old runners set it and ignore it).
        self.assertEqual(load_job(json.loads(json.dumps(dump_job(job)))).asked_text, '帮我看看这个文档')

    def test_messages_without_words_add_none_to_the_you_asked_block(self):
        # S7 follow-up (T444, 2026-11-08): a second forward or an image the turn took in while it waited showed as
        # what the owner asked, the image's local path with it.
        from covey.app import answer_cards
        image = '收到的附件（已下载到本地）：\n- 图片 1: `/abs/media/om_3/x.png`\n请在需要理解图片或文件内容时读取上述本地路径。'
        self.send('om_1', '[转发的卡片]', **self.FORWARD)
        job = self.job_of('om_1')
        self.send('om_2', '[转发的第二张卡片]', **self.FORWARD)
        self.send('om_3', '附件：图片 1 张', model_text=image, message_type='image')
        self.assertEqual(job.asked_text, '')
        self.assertGreater(self.held(job), butler.HOLD_SECONDS - 5)  # Still waiting for his words.
        self.assertIn('/abs/media/om_3/x.png', job.prompt_text)  # The model still reads them all.
        self.send('om_4', '帮我看看这两张卡', message_type='text')
        self.assertEqual(job.asked_text, '帮我看看这两张卡')
        self.assertEqual(job.start_held_until, 0)
        with mock.patch.object(answer_cards, 'ask_link', return_value=''):
            block = str(answer_cards.asked(self.ctx.state_dir, job))
        self.assertIn('帮我看看这两张卡', block)
        self.assertNotIn('转发', block)
        self.assertNotIn('/abs/', block)

    def test_an_image_with_words_adds_only_its_words(self):
        post = json.dumps({'content': [[{'tag': 'text', 'text': '这张图里的报错'}, {'tag': 'img', 'image_key': 'k'}]]})
        model = ('这张图里的报错\n收到的附件（已下载到本地）：\n- 图片 1: `/abs/media/om_2/y.png`\n'
                 '请在需要理解图片或文件内容时读取上述本地路径。\n\n回复上下文：\n被回复消息 发送者：机器人\n旧的卡片')
        self.send('om_1', '[转发的卡片]', **self.FORWARD)
        job = self.job_of('om_1')
        self.send('om_2', post, model_text=model, message_type='post')
        self.assertEqual(job.asked_text, '这张图里的报错')
        self.assertEqual(job.start_held_until, 0)
        self.assertIn('/abs/media/om_2/y.png', job.prompt_text)

    def test_a_supplement_to_a_turn_that_did_not_wait_keeps_its_words(self):
        self.send('om_1', '做这个', message_type='text')
        job = self.job_of('om_1')
        self.send('om_2', '再加一点', message_type='text')
        self.assertEqual(getattr(job, 'asked_text', ''), '')

    def test_another_message_without_words_joins_it_and_holds_it_again(self):
        self.send('om_1', '[转发的卡片]', **self.FORWARD)
        job = self.job_of('om_1')
        job.start_held_until = time.time() + 1
        self.send('om_2', '[合并转发]', message_type='merge_forward')
        self.assertEqual(self.butler_jobs(), [job])
        self.assertIn('[合并转发]', job.prompt_text)
        self.assertGreater(self.held(job), butler.HOLD_SECONDS - 5)

    def test_a_message_with_words_starts_at_once(self):
        self.send('om_1', '做这个', message_type='text')
        self.assertEqual(self.job_of('om_1').start_held_until, 0)
        self.assertNotIn('等你说要做什么', self.record('om_1')['status_message'])

    def test_while_a_turn_runs_a_forward_is_its_supplement_as_before(self):
        self.send('om_1', '做这个', message_type='text')
        job = self.job_of('om_1')
        job.execution_started, job.backend_control = True, Control()
        self.send('om_2', '[转发的卡片]', **self.FORWARD)
        self.assertEqual(job.backend_control.sent, [('[转发的卡片]', 'om_2')])
        self.assertEqual(job.start_held_until, 0)

    def test_a_queued_turn_takes_a_forward_without_holding_it(self):
        self.send('om_1', '做这个', message_type='text')
        job = self.job_of('om_1')
        self.send('om_2', '[转发的卡片]', **self.FORWARD)
        self.assertEqual(self.butler_jobs(), [job])
        self.assertEqual(job.start_held_until, 0)

    def test_the_scheduler_starts_it_once_the_hold_ends(self):
        from covey.core.scheduler import WorkspaceScheduler
        scheduler = WorkspaceScheduler(None, None, None, None)
        job = SimpleNamespace(chat_id=CHAT, topic_id=BUTLER, start_held_until=time.time() + 30)
        other = SimpleNamespace(chat_id=CHAT, topic_id='t2')
        scheduler.pending = [job, other]
        self.assertFalse(scheduler.eligible(0))
        self.assertTrue(scheduler.eligible(1))  # Other topics go on.
        job.start_held_until = time.time() - 1
        self.assertTrue(scheduler.eligible(0))

    def test_a_restart_releases_it(self):
        # Never saved: neither the queued copy nor a task runner's handoff carries it, so a restart starts it.
        from covey.app.runner_spawn import dump_job, load_job
        self.send('om_1', '{}', **self.FORWARD)
        job = self.job_of('om_1')
        saved = dump_job(job)
        self.assertNotIn('start_held_until', saved['fields'])
        self.assertNotIn('start_held_until', saved['extras'])
        self.assertEqual(load_job(json.loads(json.dumps(saved))).start_held_until, 0)


class BridgeToolNudgeTest(ButlerUnifiedCase):
    """W2-A (G8 管家工具提醒): the butler's tool reminder on a Harness that does not send it by itself (core/harness.py
    nudges): the Bridge counts the main task's tool steps that are not the Bridge's and sends it once at ``after``, as an
    internal supplement that is never the owner's words."""

    def running(self, name, said='task', mid='om_1'):
        from covey.app.prompts import tool_nudge
        self.send(mid, said)
        job = self.job_of(mid)
        job.execution_started = True
        job.backend_control = Control()
        app = self.app.apps[name]
        ctx = app.submission.event_context(app.harness.context, self.event(mid))
        nudge = tool_nudge(said)
        if nudge:
            ctx = dataclasses.replace(ctx, options={**ctx.options, 'tool_nudge': nudge})
        return app, ctx, job

    def steps(self, app, ctx, job, steps, *, events=False):
        """Each of ``steps`` ((id, name, detail) or a dict) as the backend's tool step, counted as the event thread counts
        it (``events``: through the event's whole way, the task card's trace mocked), the reminder sent inline."""
        inline = SimpleNamespace(CARD_UPDATES='card-updates', start=lambda _, fn, *a: fn(*a))
        with mock.patch('covey.app.interaction.background', inline), mock.patch.object(app.cards, 'queue_trace'):
            for step in steps:
                data = step if isinstance(step, dict) else {'id': step[0], 'name': step[1], 'detail': step[2]}
                data = {'status': 'running', 'started_at': 1.0, **data}
                if events:
                    app.interaction.structured_harness_event(ctx, job, 'tool', data, None)
                else:
                    app.interaction.count_for_nudge(ctx, job, data)

    def delivery(self, job):
        from covey.core.deliveries import delivery_path, read_delivery
        return read_delivery(delivery_path(self.state, BUTLER, 'nudge_' + job_key(job.message_id, job.job_id)))

    def test_codex_takes_one_reminder_after_six_of_its_own_tool_calls(self):
        from covey.app.prompts import TOOL_NUDGE_TEXT
        app, ctx, job = self.running('codex')
        own = [(f'c{index}', 'bash', 'rg x repo') for index in range(5)]
        bridge = [('b1', '调用工具', 'topic_read'), ('b2', '调用工具', 'topics_list T7')]
        self.steps(app, ctx, job, own + bridge + [('c0', 'bash', 'rg x repo'), {'id': 'k1', 'name': 'bash', 'child_id': 'a'}],
                   events=True)
        self.assertEqual(job.backend_control.sent, [], "five of its own, two Bridge tools, an update and a subagent's")
        self.steps(app, ctx, job, [('c5', 'bash', 'ls'), ('c6', 'bash', 'ls'), ('c7', 'read', '/x')], events=True)
        self.assertEqual(job.backend_control.sent, [(TOOL_NUDGE_TEXT, 'nudge_' + job_key(job.message_id, job.job_id))])
        record = self.delivery(job)
        self.assertTrue(record['internal'])
        self.assertEqual(record['status'], 'sent')
        history = (self.state / 'topics' / BUTLER / 'history.jsonl').read_text()
        self.assertNotIn(TOOL_NUDGE_TEXT, history)
        self.assertTrue(any('butler tool reminder sent' in line for line in self.logs))

    def test_on_antigravity_its_mcp_names_are_the_bridges(self):
        app, ctx, job = self.running('agy')
        self.steps(app, ctx, job, [(f'b{index}', f'mcp_bridge_{name}', '') for index, name in
                                   enumerate(('topics_list', 'topic_read', 'topic_start', 'memory_search', 'repos_list', 'topics_search'))])
        self.assertEqual(job.backend_control.sent, [])
        self.steps(app, ctx, job, [(f's{index}', 'bash', 'ls') for index in range(6)])
        self.assertEqual(len(job.backend_control.sent), 1)

    def test_claude_code_reminds_by_itself_and_dsh_takes_no_supplement(self):
        for name in ('claude', 'dsh'):
            with self.subTest(harness=name):
                app, ctx, job = self.running(name, mid='om_' + name)
                self.steps(app, ctx, job, [(f'{name}{index}', 'bash', 'ls') for index in range(10)])
                self.assertEqual(job.backend_control.sent, [])
                self.assertIsNone(self.delivery(job))
                self.done(job)

    def test_not_when_the_owner_said_to_do_it_right_there(self):
        app, ctx, job = self.running('codex', said='这个你直接做')
        self.steps(app, ctx, job, [(f's{index}', 'bash', 'ls') for index in range(10)])
        self.assertEqual(job.backend_control.sent, [])

    def test_the_reminder_is_never_the_owners_words(self):
        """Not a turn of its own, not a line under his message, not what the butler hands on as his words, not on the
        stopped turn's card, not what waits behind a quiet turn."""
        from covey.app import butler_handoff, butler_reply, unstick
        from covey.core.deliveries import delivery_path, update_delivery
        app, ctx, job = self.running('codex')
        self.steps(app, ctx, job, [(f's{index}', 'bash', 'ls') for index in range(6)])
        path = delivery_path(self.state, BUTLER, 'nudge_' + job_key(job.message_id, job.job_id))
        update_delivery(path, requeue={'kind': 'chat'})  # Even with a requeue it is never queued.
        key = job_key(job.message_id, job.job_id)
        self.assertEqual(butler_handoff._supplements(self.state, {'topic_id': BUTLER}, job.message_id, 0), [])
        self.assertEqual(butler_reply.supplements(self.state, job), [])
        self.assertEqual(unstick.waiting_behind(self.state, job), [])
        self.app.messenger.send_message.reset_mock()
        steering = app.steering
        inline = SimpleNamespace(CARD_UPDATES='card-updates', start=lambda _, fn, *a: fn(*a))
        with mock.patch('covey.app.steering.background', inline), mock.patch.object(steering, 'reports_reads', return_value=True):
            steering.end_unread_steers(ctx, job)
            steering.tell_butler_lost(ctx, job, self.delivery(job))
            self.assertEqual(steering.requeue_lost(ctx, job, [self.delivery(job)]), [])
        self.app.messenger.send_message.assert_not_called()
        self.assertEqual(len(self.butler_jobs()), 1)
        self.assertNotIn(key, json.dumps([self.delivery(job).get('requeue_auto')]))

    def test_one_the_turn_cannot_take_now_is_dropped_never_held(self):
        from covey.core.steer import SteerNotReady
        app, ctx, job = self.running('codex')
        job.backend_control = Control(SteerNotReady('还没准备好'))
        with mock.patch.object(app.steering, 'hold_steer') as hold:
            self.steps(app, ctx, job, [(f's{index}', 'bash', 'ls') for index in range(6)])
        hold.assert_not_called()
        self.assertEqual(self.delivery(job)['status'], 'refused')
        self.assertTrue(any('butler tool reminder not sent' in line for line in self.logs))

    def test_one_that_may_have_arrived_says_nothing_under_the_owners_message(self):
        app, ctx, job = self.running('codex')
        job.backend_control = Control(RuntimeError('lost'))
        self.app.messenger.send_message.reset_mock()
        self.steps(app, ctx, job, [(f's{index}', 'bash', 'ls') for index in range(6)])
        self.assertEqual(self.delivery(job)['status'], 'uncertain')
        self.app.messenger.send_message.assert_not_called()


class SwitchOffHoldTest(ButlerUnifiedCase):
    switch = '0'

    def test_a_forward_opens_a_topic_as_ever(self):
        self.send('om_1', '{}', message_type='interactive')
        self.assertEqual(self.butler_jobs(), [])
        self.assertEqual(self.record('om_1')['status'], 'waiting_workspace')


class TurnTest(ButlerUnifiedCase):
    def turn(self, job, session_id=None, compaction=None):
        compaction = compaction or SimpleNamespace(request=lambda ctx: None, supported=lambda ctx: True)
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event(job.message_id))
        return session.butler_turn(target, job, 'THE MESSAGE', session_id, compaction, self.logs.append)

    def other_topic(self, topic, status, number=7, **fields):
        folder = self.state / 'topics' / topic
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'topic.json').write_text(json.dumps({'topic_id': topic, 'topic_number': number}))
        (folder / 'history.jsonl').write_text(json.dumps({'role': 'user', 'content': f'topic {number}'}) + '\n')
        upsert_job_record(self.state, f'om_{topic[:6]}', status=status, topic_id=topic, chat_id=CHAT, job_id=number,
                          **{'card_message_id': f'om_card_{topic[:6]}', **fields})

    def test_rules_and_the_whole_snapshot_open_a_new_sessions_message(self):
        self.other_topic('a' * 32, 'running')
        self.send('om_1')
        _, prompt = self.turn(self.job_of('om_1'))
        self.assertTrue(prompt.startswith('[管家规则]'))
        self.assertIn('topic_start', prompt)
        self.assertIn('不要在自己这一轮里做', prompt)
        for name in ('等你', '有结果', '在做', '管家开的话题'):
            self.assertIn(name, prompt)
        self.assertIn('T7 topic 7', prompt)
        self.assertIn('：全部]', prompt)
        self.assertTrue(prompt.endswith('THE MESSAGE'))

    def test_the_butlers_checkpoint_contract_is_the_part_every_turn_uses(self):
        """5.9 row 7: the butler answers and dispatches, so its rules carry no steps, delivery or show_images; its message
        gets show_images's rule when the owner speaks of a QR code or a screenshot. A topic's turn has all of it."""
        self.send('om_1')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target, _ = self.turn(job)
        rules = app.runs.bridge_rules(target, job)
        self.assertIn(CHECKPOINT_RULES, rules)
        for part in (STEPS_RULE, DELIVERY_RULE, IMAGES_RULE):
            self.assertNotIn(part, rules)
        self.assertTrue(app.runs.slim(target, job))
        self.assertEqual(app.runs._images_note(target, job), '')
        job.user_text = '登录要扫码，二维码发我'
        self.assertEqual(app.runs._images_note(target, job), '\n\n' + IMAGES_RULE.removeprefix('- '))
        self.assertEqual(app.runs.bridge_rules(target), rules.replace(CHECKPOINT_RULES, checkpoint_rules()))
        topic_job = SimpleNamespace(topic_id='a' * 32, chat_id=CHAT, user_text='二维码发我')
        self.assertFalse(app.runs.slim(target, topic_job))
        self.assertEqual(app.runs.checkpoint_rules(target, topic_job), checkpoint_rules())
        self.assertEqual(app.runs._images_note(target, topic_job), '')  # Its rules have it already.

    def test_the_butlers_turn_is_reminded_past_six_tool_calls_unless_the_owner_said_to_do_it_there(self):
        """#34: the reminder's option for backends/claude/hooks.py; none when the owner said 你直接做 for this turn."""
        from covey.app.prompts import TOOL_NUDGE_CALLS, TOOL_NUDGE_TEXT
        self.send('om_1')
        job = self.job_of('om_1')
        target, _ = self.turn(job)
        self.assertEqual(target.options['tool_nudge'], {'after': TOOL_NUDGE_CALLS, 'text': TOOL_NUDGE_TEXT})
        self.assertEqual(TOOL_NUDGE_CALLS, 6)
        for words in ('交给话题', 'topic_start', 'topic_send', '结束这一轮', '快做完了就做完再答'):
            self.assertIn(words, TOOL_NUDGE_TEXT)
        for said in ('这个你直接做', '别开话题，就在这儿做', '不用开话题'):
            job.user_text = said
            target, _ = self.turn(job)
            self.assertNotIn('tool_nudge', target.options, said)

    def test_the_butler_asks_the_way_its_harness_can(self):
        """W2-A (G8 提问方式): Claude Code's and Codex's question tools by name; Antigravity and dsh, without one, the
        checkpoint's questions, and no AskUserQuestion anywhere in their rules."""
        from covey.app.prompts import LATER_PICK
        self.send('om_1')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event(job.message_id))
        compaction = SimpleNamespace(request=lambda ctx: None, supported=lambda ctx: True)
        for name, tool in (('claude', 'AskUserQuestion'), ('codex', 'request_user_input'), ('agy', ''), ('dsh', '')):
            with self.subTest(harness=name):
                _, prompt = session.butler_turn(target, job, 'THE MESSAGE', None, compaction, self.logs.append,
                                                self.app.apps[name].harness)
                rules = prompt.split('\n\n')[0]
                if tool:
                    self.assertIn(f'用 {tool} 出一道单选题', rules)
                    self.assertIn(f'照下面说的用 {tool} 当场问', rules)
                else:
                    self.assertIn(LATER_PICK, rules)
                    self.assertNotIn('当场问', rules)
                for other in {'AskUserQuestion', 'request_user_input'} - {tool}:
                    self.assertNotIn(other, rules)

    def test_the_owners_principles_reach_the_butler_with_its_own_workspaces(self):
        """core/principles.py: the butler's turn reads 通用 and its own workspace's group, as a topic's does."""
        self.send('om_1')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target, _ = self.turn(job)
        self.assertTrue(target.workspace_name)
        principles.save(self.state, 'owner', '通用的原则')
        principles.save(self.state, 'workspace', '管家工作区的原则', workspace=target.workspace_name)
        principles.save(self.state, 'workspace', '别处的原则', workspace='elsewhere')
        rules = app.runs.bridge_rules(target, job)
        self.assertIn(f'通用：\n- 通用的原则\n工作区 {target.workspace_name}：\n- 管家工作区的原则', rules)
        self.assertNotIn('别处的原则', rules)
        self.assertIn('memory_save', self.turn(job)[1])  # The memory tool the butler is listed (T870).

    def test_the_next_turn_says_only_what_changed(self):
        self.other_topic('a' * 32, 'running')
        self.send('om_1')
        self.turn(self.job_of('om_1'))
        self.done(self.job_of('om_1'))
        self.send('om_2')
        _, same = self.turn(self.job_of('om_2'), 'sid')
        self.assertIn('和上一轮相比没有变化', same)
        self.assertNotIn('T7', same)
        self.done(self.job_of('om_2'))
        self.other_topic('b' * 32, 'running', number=8)
        self.send('om_3')
        _, changed = self.turn(self.job_of('om_3'), 'sid')
        self.assertIn('T8 topic 8', changed)
        self.assertNotIn('T7', changed)
        self.assertIn('只列和上一轮相比变了的', changed)

    def test_on_a_harness_the_bridge_does_not_compact_every_turn_has_the_whole_snapshot(self):
        """T614: Codex and agy compact on their own, which the Bridge does not see: a snapshot of only what changed
        could then stand on nothing, so each turn has all of it."""
        own = SimpleNamespace(request=lambda ctx: None, supported=lambda ctx: False)
        self.other_topic('a' * 32, 'running')
        self.send('om_1')
        self.turn(self.job_of('om_1'), compaction=own)
        self.done(self.job_of('om_1'))
        self.send('om_2')
        _, again = self.turn(self.job_of('om_2'), 'sid', own)
        self.assertNotIn('和上一轮相比没有变化', again)
        self.assertIn('：全部]', again)
        self.assertIn('T7', again)

    def test_a_topic_that_ended_moves_to_results(self):
        self.other_topic('a' * 32, 'running')
        self.send('om_1')
        self.turn(self.job_of('om_1'))
        self.done(self.job_of('om_1'))
        self.other_topic('a' * 32, 'done')
        self.send('om_2')
        _, prompt = self.turn(self.job_of('om_2'), 'sid')
        self.assertIn('不再在“在做”', prompt)
        self.assertIn('最近有结果', prompt)  # No read receipts in this state directory.

    def test_a_topic_taken_off_the_board_is_not_in_the_snapshot(self):
        from covey.app import pins
        from covey.core.topic_seen import PINNED_TOPICS_ENV
        self.other_topic('a' * 32, 'running', card_message_id='om_card_a')
        self.other_topic('b' * 32, 'running', number=8, card_message_id='om_card_b')
        pins.dismiss(self.state, CHAT, ['a' * 32], time.time())
        self.send('om_1')
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: '1', pins.OVERVIEW_ENV: '1'}):  # The board laid.
            groups = session.snapshot_groups(self.ctx, CHAT, time.time())
            _, prompt = self.turn(self.job_of('om_1'))
        self.assertEqual(list(groups['running']), ['b' * 32])
        self.assertNotIn('T7', prompt)
        self.assertIn('T8 topic 8', prompt)
        for off in ({PINNED_TOPICS_ENV: '0'}, {PINNED_TOPICS_ENV: '1', pins.OVERVIEW_ENV: '0'}):
            with self.subTest(off), mock.patch.dict(os.environ, off):  # The board off: its record is not read.
                self.assertEqual(set(session.snapshot_groups(self.ctx, CHAT, time.time())['running']),
                                 {'a' * 32, 'b' * 32})

    def test_a_running_topic_says_which_step_and_for_how_long(self):
        now = time.time()
        steps = [{'title': '读代码', 'state': 'done'}, {'title': '改卡片', 'state': 'doing'}, {'title': '写测试', 'state': 'todo'}]
        self.other_topic('a' * 32, 'running', phase='改卡片中', steps=steps, step_times={'改卡片': {'started': now - 2400}})
        self.other_topic('b' * 32, 'running', number=8, phase='排查')
        running = session.snapshot_groups(self.ctx, CHAT, now)['running']
        self.assertIn('第 2/3 步：改卡片 · 本步 40 分钟', running['a' * 32])
        self.assertNotIn('阶段：', running['a' * 32])  # The step under way stands for the phase.
        self.assertIn('阶段：排查', running['b' * 32])  # Without steps as before.
        self.assertNotIn('步', running['b' * 32])

    def test_a_result_the_owner_read_is_not_listed(self):
        self.other_topic('a' * 32, 'done')
        (self.state / SEEN_FILE).write_text(json.dumps({'a' * 32: time.time() + 60}))
        self.send('om_1')
        _, prompt = self.turn(self.job_of('om_1'))
        self.assertNotIn('T7', prompt)
        self.assertIn('有结果没看', prompt)

    def test_a_result_whose_conclusion_went_to_the_top_level_says_so(self):
        from covey.cards.relay_card import DONE
        self.other_topic('a' * 32, 'done')
        self.other_topic('b' * 32, 'done', number=8)
        (self.state / 'main-thread' / 'relays').mkdir(parents=True, exist_ok=True)
        (self.state / 'main-thread' / 'relays' / f"om_{'a' * 6}.json").write_text(
            json.dumps({'state': 'sent', 'outcome': DONE}))
        results = session.snapshot_groups(self.ctx, CHAT, time.time())['result']
        self.assertTrue(results['a' * 32].endswith('（结论已发到聊天区）'))
        self.assertNotIn('结论已发到聊天区', results['b' * 32])

    def test_after_a_compaction_the_whole_snapshot_again(self):
        self.send('om_1')
        self.turn(self.job_of('om_1'))
        self.done(self.job_of('om_1'))
        self.send('om_2')
        _, prompt = self.turn(self.job_of('om_2'), 'sid', SimpleNamespace(request=lambda ctx: {'at': 1}, supported=lambda ctx: True))
        self.assertIn('：全部]', prompt)

    def test_after_a_turn_that_ended_badly_the_whole_snapshot_again(self):
        self.send('om_1')
        self.turn(self.job_of('om_1'))
        upsert_job_record(self.state, 'om_1', status='failed')
        self.app.runtime.jobs.unregister(self.job_of('om_1'))
        self.send('om_2')
        _, prompt = self.turn(self.job_of('om_2'), 'sid')
        self.assertIn('：全部]', prompt)

    def test_topics_it_opened_are_listed(self):
        opened = self.state / 'main-thread' / 'opened'
        opened.mkdir(parents=True)
        (opened / 'r1.json').write_text(json.dumps({'topic_id': 'c' * 32, 'title': '查日志', 'at': 5}, ensure_ascii=False))
        self.send('om_1')
        _, prompt = self.turn(self.job_of('om_1'))
        self.assertIn('查日志（没在动）', prompt)

    def test_a_snapshot_it_cannot_read_does_not_stop_the_turn(self):
        self.send('om_1')
        with mock.patch.object(session, 'take_snapshot', side_effect=OSError):
            _, prompt = self.turn(self.job_of('om_1'))
        self.assertIn('THE MESSAGE', prompt)
        self.assertIn('快照暂时读不到', prompt)

    def test_only_its_jobs_run_with_the_bridge_tools(self):
        self.send('om_1')
        job = self.job_of('om_1')
        ctx, _ = self.turn(job)
        config = ctx.options['mcp_config']['mcpServers'][butler.MCP_SERVER]
        # The switches are handed on as they stand (the tests run them at 0, unset would mean on).
        switches = {name: cenv.get(name, '') for name in ('COVEY_KNOWLEDGE', 'COVEY_BUTLER_RULES', 'COVEY_PROJECTS', 'COVEY_PROJECT_TRIAGE', 'COVEY_PROJECT_TIDY')}
        self.assertEqual(config['env'], cenv.mirrored({butler.STATE_ENV: str(self.state), butler.JOB_ENV: 'om_1', **switches}))
        self.assertEqual(Path(config['args'][-1]), session.RELEASE / butler.MCP_ENTRY)
        self.settings(enabled=False)
        self.send('om_2')
        other = SimpleNamespace(topic_id=self.record('om_2')['topic_id'], chat_id=CHAT)
        ctx, prompt = session.butler_turn(self.ctx, other, 'MSG', None, mock.Mock(), self.logs.append)
        self.assertNotIn('mcp_config', ctx.options)
        self.assertEqual(prompt, 'MSG')

    def test_the_backend_gets_them_on_its_command_line(self):
        app = self.app.apps['claude']
        plain = stream_argv(app.backend, app.harness.context)
        self.assertNotIn('--mcp-config', plain)
        self.assertNotIn('--allowedTools', plain)
        config = butler.mcp_config(Path('/r'), Path('/s'), 'k', '/py')
        ctx = app.harness.context
        ctx = type(ctx)(**{**ctx.__dict__, 'options': {**ctx.options, 'mcp_config': config}})
        argv = stream_argv(app.backend, ctx)
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), config)
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'mcp__bridge')
        self.assertNotIn('--strict-mcp-config', argv)

    def test_only_the_butlers_turn_loads_its_tools_without_tool_search(self):
        plain = turn_env(SimpleNamespace(options={}), {'PATH': '/bin'})
        main = turn_env(SimpleNamespace(options={'mcp_config': {'mcpServers': {'bridge': {}}}}), {'PATH': '/bin'})
        self.assertNotIn('ENABLE_TOOL_SEARCH', plain)
        self.assertEqual(main['ENABLE_TOOL_SEARCH'], 'false')
        self.assertEqual(plain['CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS'], main['CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS'])

    def test_a_whole_turn_sends_the_rules_and_the_tools_to_the_backend(self):
        self.send('om_1', 'hello')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event('om_1'))
        with mock.patch.object(app.backend, 'run', return_value=RunResult('ok', 'sid', False)) as run, \
                mock.patch('covey.app.runs.stop_task_leftovers'):
            app.runs.generate_reply_for_worker(target, job, None, None)
        request = run.call_args.args[0]
        # The butler's rules at the system level with the bridge rules (app/rules_sent.py), the snapshot in the message.
        self.assertIn('[管家规则]', request.instructions)
        self.assertNotIn('[管家规则]', request.prompt)
        self.assertIn('[快照', request.prompt)
        self.assertIn('hello', request.prompt)
        self.assertIn(butler.MCP_SERVER, request.context.options['mcp_config']['mcpServers'])

    def test_a_butler_turn_runs_its_subagents_light_unless_the_setting_says_same(self):
        self.send('om_1', 'hello')
        job = self.job_of('om_1')
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event('om_1'))
        with mock.patch.object(app.backend, 'run', return_value=RunResult('ok', 'sid', False)) as run, \
                mock.patch('covey.app.runs.stop_task_leftovers'):
            app.runs.generate_reply_for_worker(target, job, None, None)
        ctx = run.call_args.args[0].context
        argv = stream_argv(app.backend, ctx)
        self.assertEqual(json.loads(argv[argv.index('--agents') + 1])['Explore']['model'], 'haiku')
        self.assertEqual(turn_env(ctx, {'PATH': '/bin'})['CLAUDE_CODE_SUBAGENT_MODEL'], 'sonnet')
        butler_settings.update(ctx.state_dir, subagents='same')
        self.assertNotIn('--agents', stream_argv(app.backend, ctx))
        self.assertNotIn('CLAUDE_CODE_SUBAGENT_MODEL', turn_env(ctx, {'PATH': '/bin'}))


class CompactionTest(ButlerUnifiedCase):
    def setUp(self):
        super().setUp()
        self.send('om_1')
        self.job = self.job_of('om_1')
        self.claude = self.app.apps['claude']
        self.target = self.claude.submission.event_context(self.claude.harness.context, self.event('om_1'))

    def after(self, tokens, job=None):
        (job or self.job).tool_trace = SimpleNamespace(context_tokens=tokens)
        session.compact_butler(self.claude.compaction, self.target, job or self.job)
        return self.claude.compaction.request(self.target)

    def test_past_the_threshold_the_next_turn_compacts_first(self):
        self.assertIsNone(self.after(199_999))
        self.assertIsNotNone(self.after(200_000))
        ctx = self.claude.compaction.turn_context(self.target, self.job, 'sid')
        self.assertTrue(ctx.options.get('compact_first'))

    def test_the_settings_threshold_counts_and_zero_never_compacts(self):
        self.settings(compact_tokens=1000)
        self.assertIsNotNone(self.after(1000))
        self.claude.compaction.path(self.target).unlink(missing_ok=True)
        self.settings(compact_tokens=0)
        self.assertIsNone(self.after(10_000_000))

    def test_the_tokens_are_read_from_the_record_when_the_trace_has_none(self):
        upsert_job_record(self.state, 'om_1', context_tokens=300_000)
        self.job.tool_trace = None
        session.compact_butler(self.claude.compaction, self.target, self.job)
        self.assertIsNotNone(self.claude.compaction.request(self.target))

    def test_a_topics_turn_is_left_alone(self):
        self.settings(enabled=False)
        self.send('om_2')
        job = SimpleNamespace(topic_id=self.record('om_2')['topic_id'], chat_id=CHAT, job_id=2,
                              tool_trace=SimpleNamespace(context_tokens=10_000_000))
        target = self.claude.submission.event_context(self.claude.harness.context, self.event('om_2'))
        session.compact_butler(self.claude.compaction, target, job)
        self.assertIsNone(self.claude.compaction.request(target))

    def test_its_compaction_asks_for_a_short_summary_and_a_topics_does_not(self):
        """backends/claude/compaction.py command: /compact plus the butler's COMPACT_INSTRUCTIONS."""
        self.assertIsNotNone(self.after(250_000))
        with mock.patch.object(self.claude.backend, 'run', return_value=RunResult('ok', 'sid', False)) as run, \
                mock.patch('covey.app.runs.stop_task_leftovers'):
            self.claude.runs.generate_reply_for_worker(self.target, self.job, 'sid', None)
        options = run.call_args.args[0].context.options
        self.assertTrue(options.get('compact_first'))
        self.assertEqual(options['compact_instructions'], session.COMPACT_INSTRUCTIONS)
        self.assertEqual(compaction_command(run.call_args.args[0].context),
                         '/compact ' + ' '.join(session.COMPACT_INSTRUCTIONS.split()))
        for text in ('2000 token', 'T 号', '主人', '偏好', '还没定的问题', '过程细节'):
            self.assertIn(text, session.COMPACT_INSTRUCTIONS)
        # A topic's turn, and the butler's own topic with the butler off: /compact as before.
        self.settings(enabled=False)
        self.send('om_2')
        topic = SimpleNamespace(topic_id=self.record('om_2')['topic_id'], chat_id=CHAT)
        for job in (topic, self.job):
            with self.subTest(topic=job.topic_id[:8]):
                ctx, _ = session.butler_turn(self.target, job, 'MSG', 'sid', self.claude.compaction, lambda line: None)
                self.assertNotIn('compact_instructions', ctx.options)
                self.assertEqual(compaction_command(ctx), '/compact')

    def test_a_delivered_turn_asks_for_it_through_the_job_state(self):
        self.job.tool_trace = SimpleNamespace(context_tokens=250_000)
        self.claude.record_job_state(self.target, self.job, 'done', '', delivery_ok=True)
        self.assertIsNotNone(self.claude.compaction.request(self.target))


SID = '0b0e3f4c-1d2e-4f50-9a6b-7c8d9e0f1a2b'


def until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.01)


class IdleCompactionTest(ButlerUnifiedCase):
    """app/idle_compaction.py: the butler's session compacted between its turns, not before the next one."""

    def setUp(self):
        super().setUp()
        self.send('om_1')
        self.job = self.job_of('om_1')
        self.claude = self.app.apps['claude']
        self.target = self.claude.submission.event_context(self.claude.harness.context, self.event('om_1'))
        self.lines = []
        self.app.runtime.log_sink = self.lines.append
        self.mark = idle_compaction.run_dir(self.state, BUTLER) / idle_compaction.MARK

    def ask(self, tokens=250_000, **extra):
        """The request a butler turn's task runner records past the threshold (200,000)."""
        self.claude.compaction.set_requested(self.target, self.job.job_id,
                                             **{'tokens': tokens, 'threshold': 200_000, 'idle': True, **extra})

    def request(self):
        return self.claude.compaction.request(self.target)

    def runner(self):
        """A stand-in compaction runner: a process leading its own group."""
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait()))
        until(lambda: process_identity(process.pid) is not None)
        return process

    def under_way(self, process):
        """A compaction the Bridge started: its request taken, its marker written."""
        request = self.request()
        self.mark.parent.mkdir(parents=True, exist_ok=True)
        self.mark.write_text(json.dumps({
            'topic_id': BUTLER, 'started_at': 1.0, 'request': request, 'process': process_identity(process.pid),
            'request_path': str(self.claude.compaction.path(self.target).absolute())}))
        self.claude.compaction.path(self.target).unlink()

    def launch(self):
        return runner_spawn.compaction_launch(self.app.runtime, self.claude, self.job, self.claude.harness.context)

    def start(self):
        return idle_compaction.start(self.app, self.claude, self.job, self.target, self.launch())

    def started(self, finish=None):
        """Start one with the runner's spawn stubbed; (spawn mock, its stand-in process)."""
        finish = finish or threading.Event()
        process = SimpleNamespace(pid=os.getpid(), wait=lambda: 0 if finish.wait(5) else 1)
        spawn = mock.patch('covey.app.runner_spawn._spawn', return_value=(process, Path('/r'))).start()
        self.addCleanup(mock.patch.stopall)
        return spawn, finish

    def live(self):
        """The live Bridge, whose jobs run detached (patched only around the exit: a turn here runs in this process)."""
        return mock.patch.object(self.app.runtime, 'detached', return_value=True)

    def test_a_turns_exit_compacts_once_the_topic_stayed_idle(self):
        self.ask()
        self.done(self.job)
        spawn, finish = self.started()
        with mock.patch.object(idle_compaction, 'IDLE_SECONDS', 0.3), self.live():
            idle_compaction.after_runner(self.app, self.claude, self.job, self.claude.harness.context, self.launch())
            self.assertFalse(spawn.called)  # It waits first.
            until(lambda: any('idle compaction started' in line for line in self.lines))
        self.assertEqual(spawn.call_args.args[1:], (self.mark.parent, '--compact-only'))
        marker = json.loads(self.mark.read_text())
        self.assertEqual(marker['process']['pid'], os.getpid())
        self.assertEqual(marker['request']['tokens'], 250_000)
        self.assertEqual(marker['handoff']['harness'], 'claude')
        self.assertEqual(marker['handoff']['job']['fields']['message_id'], 'om_1')
        self.assertIsNone(self.request())  # Taken: a turn now finds the marker instead.
        finish.set()
        until(lambda: any('idle compaction finished' in line for line in self.lines))
        until(lambda: not any(t.name == 'idle-compaction' for t in threading.enumerate()))

    def test_a_message_while_it_waits_starts_none(self):
        self.ask()
        self.done(self.job)
        spawn, _ = self.started()
        self.send('om_2')  # Queued, held or running: the topic is not idle.
        self.assertIsNone(self.start())
        self.assertFalse(spawn.called)
        self.assertIsNotNone(self.request())
        self.assertFalse(self.mark.exists())

    def test_a_turn_waiting_for_an_answer_or_a_failed_request_starts_none(self):
        self.ask()
        spawn, _ = self.started()
        upsert_job_record(self.state, 'om_1', status='waiting_user')
        self.app.runtime.jobs.unregister(self.job)
        self.assertIsNone(self.start())
        self.done(self.job)
        self.claude.compaction.path(self.target).unlink(missing_ok=True)
        self.ask(failed=True)
        self.assertIsNone(self.start())
        self.assertFalse(spawn.called)

    def test_with_the_butler_off_nothing_happens(self):
        self.ask()
        self.done(self.job)
        spawn, _ = self.started()
        self.settings(enabled=False)
        with self.live(), mock.patch('threading.Thread') as thread:
            idle_compaction.after_runner(self.app, self.claude, self.job, self.claude.harness.context, self.launch())
        self.assertFalse(thread.called)
        self.assertIsNone(self.start())
        self.assertFalse(spawn.called)
        # Nor in a task runner or a test app (no detached jobs).
        self.settings(enabled=True)
        with mock.patch('threading.Thread') as thread:
            idle_compaction.after_runner(self.app, self.claude, self.job, self.claude.harness.context, self.launch())
        self.assertFalse(thread.called)

    def test_a_turn_stops_a_compaction_under_way_and_runs_on_the_session_as_it_is(self):
        self.ask()
        self.done(self.job)
        process = self.runner()
        self.under_way(process)
        self.send('om_2')
        job = self.job_of('om_2')
        target = self.claude.submission.event_context(self.claude.harness.context, self.event('om_2'))
        logs = []
        session.butler_turn(target, job, 'MSG', SID, self.claude.compaction, logs.append)
        self.assertIsNotNone(process.poll())  # Its process group, stopped.
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.request()['tokens'], 250_000)  # Back, for this turn's end.
        self.assertTrue(any(f'idle compaction cancelled by job=#{job.job_id}' in line for line in logs))
        ctx = self.claude.compaction.turn_context(target, job, SID)
        self.assertNotIn('compact_first', ctx.options)
        self.assertIsNotNone(self.request())

    def test_a_turn_compacts_first_past_one_and_a_half_times_or_after_a_failure(self):
        for extra in ({'tokens': 300_000}, {'failed': True}, {'idle': False}):
            with self.subTest(**extra):
                self.claude.compaction.path(self.target).unlink(missing_ok=True)
                self.ask(**{'tokens': 250_000, **extra})
                ctx = self.claude.compaction.turn_context(self.target, self.job, SID)
                self.assertTrue(ctx.options.get('compact_first'))
                self.assertIsNone(self.request())
        # The request a runner records is idle, with the sizes; a repeated one keeps its time.
        self.app.runtime.runner = object()
        self.addCleanup(setattr, self.app.runtime, 'runner', None)
        self.job.tool_trace = SimpleNamespace(context_tokens=210_000)
        session.compact_butler(self.claude.compaction, self.target, self.job)
        first = self.request()
        self.job.tool_trace = SimpleNamespace(context_tokens=290_000)
        session.compact_butler(self.claude.compaction, self.target, self.job)
        self.assertEqual((self.request()['idle'], self.request()['tokens'], self.request()['threshold']),
                         (True, 290_000, 200_000))
        self.assertEqual(self.request()['at'], first['at'])

    def test_the_bridges_start_clears_what_the_last_one_left(self):
        self.ask()
        process = self.runner()
        self.under_way(process)
        session.Butler.settle(self.ctx, self.lines.append)
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.mark.exists())
        self.assertIsNotNone(self.request())
        # One whose runner is gone: only its marker and request.
        gone = self.runner()
        self.under_way(gone)
        gone.kill()
        gone.wait()
        idle_compaction.clear_left(self.state, self.lines.append)
        self.assertFalse(self.mark.exists())
        self.assertIsNotNone(self.request())
        self.assertEqual(sum('idle compaction cancelled at the Bridge' in line for line in self.lines), 2)

    def test_the_task_runners_entry_runs_it_with_no_job(self):
        from covey.app import runner
        with mock.patch.object(compact_runner, 'compact_main', return_value=0) as main:
            self.assertEqual(runner.runner_main(['--run-dir', str(self.mark.parent), '--compact-only']), 0)
        main.assert_called_once_with(self.mark.parent)

    def test_a_runners_exit_hands_the_turn_to_it(self):
        with mock.patch.object(idle_compaction, 'after_runner') as after:
            detached._compact_when_idle(self.app, self.claude, self.job, self.claude.harness.context)
        launch = after.call_args.args[4]
        self.assertEqual(launch.handoff()['job']['fields']['message_id'], 'om_1')
        with mock.patch.object(idle_compaction, 'after_runner', side_effect=OSError):
            detached._compact_when_idle(self.app, self.claude, self.job, self.claude.harness.context)
        self.assertTrue(any('idle compaction not scheduled' in line for line in self.lines))

    def compact(self, result, tools=False):
        """``compact_main`` on the marker a start wrote, its Bridge stubbed; (exit status, the backend's request).
        ``tools``: a backend that compacts with the butler's tools (``compact_with_tools``, agy's)."""
        self.ask()
        self.done(self.job)
        pointer = self.target.session_path
        revision, current = capture_session_state(pointer, self.claude.spec.session_pattern)
        compare_and_set_session(pointer, self.claude.spec.session_pattern, revision, current, SID)
        self.started()[1].set()
        self.assertEqual(self.start(), 0)
        backend = mock.Mock()
        if tools:
            backend.compact_with_tools = True
        if isinstance(result, BaseException):
            backend.compact_session.side_effect = result
        else:
            backend.compact_session.return_value = result
        backend.capabilities.return_value = HarnessCapabilities(system_instructions=True, system_refresh="compaction")
        runs = SimpleNamespace(bridge_rules=lambda ctx, job, system=False: "THE TURN'S RULES" if system else "")
        app = SimpleNamespace(harness=self.claude.harness, sessions=self.claude.sessions, backend=backend, runs=runs)
        gateway = SimpleNamespace(runtime=SimpleNamespace(runner=None, log=self.lines.append, owner_open_id=None),
                                  build_parser=lambda: SimpleNamespace(parse_args=lambda argv: argv),
                                  build_context=lambda args: None, apps={'claude': app})
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        with mock.patch.object(compact_runner, 'UnifiedBridgeApp', return_value=gateway):
            status = compact_runner.compact_main(self.mark.parent)
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.claude.sessions.read_session_id(pointer), SID)  # The session goes on under its ID.
        return status, backend.compact_session.call_args.args[0]

    def test_the_runner_compacts_the_topics_session_and_the_next_snapshot_is_whole(self):
        snapshot = self.state / 'topics' / BUTLER / session.SNAPSHOT
        key = job_key('om_1', self.job.job_id)
        status, request = self.compact(True)
        self.assertEqual(status, idle_compaction.DONE)
        self.assertEqual(request.session_id, SID)
        self.assertTrue(request.context.options['compact_first'])
        self.assertEqual(request.context.options['compact_instructions'], session.COMPACT_INSTRUCTIONS)
        self.assertNotIn('mcp_config', request.context.options)
        self.assertEqual(request.context.topic_id, BUTLER)
        # The rules at the system level as a turn has them: Claude Code renders them again from this process's.
        self.assertEqual(request.instructions, "THE TURN'S RULES")
        self.assertIsNone(self.request())
        at = idle_compaction.compacted_at(snapshot.parent)
        self.assertGreater(at, 0)
        for saved_at, whole in ((at - 10, True), (at + 10, False)):
            with self.subTest(saved_at=saved_at):
                snapshot.write_text(json.dumps({'job': key, 'at': saved_at, 'groups': session.snapshot_groups(
                    self.ctx, CHAT, time.time())}))
                text = session.take_snapshot(self.ctx, CHAT, job_key('om_9', 9), full=False)
                self.assertEqual('：全部]' in text, whole)

    def test_a_backend_that_compacts_with_the_butlers_tools_gets_them(self):
        # agy's compaction request reads the turn's prompt from the cache only with the same tools (backends/agy/compaction.py).
        status, request = self.compact(True, tools=True)
        self.assertEqual(status, idle_compaction.DONE)
        config = request.context.options['mcp_config']
        self.assertEqual(config, butler.mcp_config(session.RELEASE, self.state, job_key('om_1', self.job.job_id),
                                                        sys.executable))

    def test_how_long_a_topic_stays_idle_is_the_backends_when_it_says(self):
        self.assertEqual(idle_compaction.idle_seconds(SimpleNamespace(compact_idle_seconds=180.0)), 180.0)
        for backend in (SimpleNamespace(), mock.Mock(), SimpleNamespace(compact_idle_seconds=True),
                        SimpleNamespace(compact_idle_seconds=-1)):
            with self.subTest(backend=backend):
                self.assertEqual(idle_compaction.idle_seconds(backend), idle_compaction.IDLE_SECONDS)

    def test_a_failed_compaction_is_the_next_turns_and_a_stopped_one_comes_back_as_it_was(self):
        status, _ = self.compact(False)
        self.assertEqual(status, idle_compaction.NOT_DONE)
        self.assertTrue(self.request()['failed'])
        self.assertTrue(self.claude.compaction.turn_context(self.target, self.job, SID).options.get('compact_first'))
        status, _ = self.compact(JobCancelled('stopped'))
        self.assertEqual(status, idle_compaction.CANCELLED)
        self.assertNotIn('failed', self.request())
        self.assertNotIn('compact_first', self.claude.compaction.turn_context(self.target, self.job, SID).options)


if __name__ == '__main__':
    unittest.main()


class ActionRowTest(ButlerUnifiedCase):
    """A butler task card offers 补充 and 停止, no 换配置 (core/butler.py Traits.switch_config): the card
    held for the owner's words showed it, and 侧聊 (2026-11-06 DM acceptance; 侧聊 left every card 2026-11-08)."""

    def card(self, topic_id):
        from support.fake_lark import FakeCardKit
        from covey.app.cards import Cards
        from covey.core.jobs import BridgeReplyJob
        app = self.app.apps['claude']
        target = app.submission.event_context(app.harness.context, self.event('om_1'))
        job = BridgeReplyJob(job_id=9, user_text='task', chat_id=CHAT, message_id='om_run', timeout=0,
                             topic_id=topic_id, harness_id='claude', binding_revision=1)
        with FakeCardKit({'data': {'card_id': 'run-card'}}, sent=('run-msg', False)) as lark:
            Cards.create_streaming_reply(app.cards, target, job, 'queued')  # Not the fixture's stub.
        return json.dumps(lark.last.created, ensure_ascii=False)

    def test_a_queued_or_running_butler_card_has_no_switch(self):
        self.send('om_1', 'first')
        card = self.card(BUTLER)
        self.assertIn('job_stop_action', card)
        self.assertNotIn('running_switch', card)
        self.assertNotIn('side_chat_action', card)

    def test_a_topics_card_keeps_them(self):
        self.send('om_1', 'first')
        card = self.card('t' * 32)
        self.assertIn('running_switch', card)
        self.assertNotIn('side_chat_action', card)  # Off every card since 2026-11-08.


class HarnessNoteTest(unittest.TestCase):
    """The start says so when the private chat is 管家 but the butler's Harness is not enabled: the
    owner's messages then open topics, and nothing else told (acceptance on a test bot without Claude Code)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def thread(self, apps):
        return session.Butler(SimpleNamespace(runtime=SimpleNamespace(), apps=apps))

    @staticmethod
    def app(tools=True):
        backend = SimpleNamespace(capabilities=lambda ctx: HarnessCapabilities(bridge_tools=tools))
        return SimpleNamespace(backend=backend, harness=SimpleNamespace(context=None))

    def test_a_missing_harness_is_one_warning_line_and_an_enabled_one_none(self):
        with mock.patch.object(session.contract, "enabled", return_value=True):
            note = self.thread({}).harness_note(self.state)
            self.assertIn("no enabled Harness can take the Bridge tools", note)
            self.assertIn("纯话题", note)
            self.assertEqual(self.thread({session.HARNESS: self.app()}).harness_note(self.state), "")
            note = self.thread({"dsh": self.app(tools=False)}).harness_note(self.state)  # Enabled, but no Bridge tools.
            self.assertIn("no enabled Harness can take the Bridge tools", note)
        with mock.patch.object(session.contract, "enabled", return_value=False):
            self.assertEqual(self.thread({}).harness_note(self.state), "", "the main thread off: nothing to warn of")

    def test_with_none_named_the_butler_runs_on_claude_code_else_the_first_enabled_harness_that_takes_the_tools(self):
        """T664: a butler whose settings name no Harness follows what is enabled: Claude Code while it can run it (as
        before), else Codex, else Antigravity; never one without the Bridge's tools; nothing saved."""
        claude, codex, agy, dsh = self.app(), self.app(), self.app(), self.app(tools=False)
        with mock.patch.object(session.contract, "enabled", return_value=True):
            for apps, want in (({session.HARNESS: claude, "codex": codex, "agy": agy}, (session.HARNESS, claude)),
                               ({"codex": codex, "agy": agy}, ("codex", codex)),
                               ({"agy": agy, "dsh": dsh}, ("agy", agy)),
                               ({session.HARNESS: self.app(tools=False), "codex": codex}, ("codex", codex)),
                               ({"dsh": dsh}, (session.HARNESS, None))):
                with self.subTest(apps=sorted(apps)):
                    self.assertEqual(self.thread(apps).harness_app(self.state), want)
                    self.assertEqual(self.thread(apps).harness_note(self.state) == "", want[1] is not None)
        self.assertFalse(butler_settings.path(self.state).exists())  # Followed, never written.

    def test_the_settings_harness_counts_and_only_one_that_takes_the_bridge_tools(self):
        butler_settings.update(self.state, harness="agy")
        with mock.patch.object(session.contract, "enabled", return_value=True):
            claude_only = self.thread({session.HARNESS: self.app()})
            self.assertIn("Harness agy is not enabled", claude_only.harness_note(self.state))
            self.assertEqual(claude_only.harness_app(self.state), ("agy", None))
            toolless = self.thread({"agy": self.app(tools=False)})
            self.assertEqual(toolless.harness_app(self.state), ("agy", None))
            agy = self.app()
            self.assertEqual(self.thread({"agy": agy}).harness_app(self.state), ("agy", agy))
            self.assertEqual(self.thread({"agy": agy}).harness_note(self.state), "")

"""Changing a topic's Harness or model mid-topic (换配置): the switch card, what carries over
(workspace, conversation, budget), the handoff to the new Harness and its first prompt."""

from __future__ import annotations

import itertools
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.cards import page_options, settings_rows, walk_dicts, walk_elements as walk
from support.fake_backends import InstructedBackend
from support.git import commit_all, init_git_repo, run_git
from support.launch_fixtures import MASO_MENU, redacting, write_maso_catalog
from support.processes import leftover_check
from support.records import write_topic
from support.sessions import write_session_id
from support.world import UnifiedCase

from support import requires_extension
from covey.core.jobs import context_for_job
from covey.app import harness_switch as hs
from covey.app import switch_handoff
from covey.app import model_usage
from covey.cards import switch_form
from covey.app.topics import retired_notice
from covey.app.app import BridgeApp
from covey.app.launch_models import available_models, harness_rank
from covey.app.prompts import first_turn_message
from covey.app.sessions import started_with_system_rules
from covey.core.checkpoint import CHECKPOINT_RULES
from covey.core.harness import JobCancelled
from covey.core.session_guard import block_policy_session
from covey.app.recovery_cards import recovery_card
from covey.core.menu_choice import effort_options
from covey.cards.question_cards import question_card, question_element
from covey.app.task_card import card_look
from covey.cards.streaming import build_stop_action_row
from covey.core.jobs import BridgeReplyJob, find_job_record, job_path, list_job_records, upsert_job_record
from covey.core.roster import NAMES
from covey.core.state import write_private_text
from covey.core.history_storage import append_history
from covey.core.store import invalidate_directory
from covey.core.topics import read_topic, remember_topic_message
from covey.core.workspaces import load_config, resolve_selection

setUpModule, tearDownModule = leftover_check()


class HarnessSwitchTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.receive('src', 'first question')
        _, self.source = self.route('src')
        data = read_topic(self.source)
        data.update(harness_id='codex', model='codex-model', effort='high', model_settings_snapshot=True,
                    workspace_locked=True, harness_locked=True)
        write_topic(self.source, data)
        self.codex, self.source = self.route('src')
        self.session = str(uuid.uuid4())
        write_session_id(self.source.session_path, self.session)
        for n in range(1, 6):
            append_history(self.source.history_path, 'user', f'question {n}', {})
            append_history(self.source.history_path, 'assistant', f'answer {n}', {})
        for record in list_job_records(self.ctx.state_dir, limit=0):  # Only the jobs set up below exist.
            job_path(self.ctx.state_dir, record['key']).unlink()
        invalidate_directory(job_path(self.ctx.state_dir, 'x').parent)  # Removed by hand: the store lists again.
        self.app.runtime.jobs.by_id.clear()
        upsert_job_record(self.ctx.state_dir, 'job-1', topic_id=self.source.topic_id, chat_id='chat', job_id=1,
                          status='done', backend='codex', kind='chat', binding_revision=data.get('binding_revision', 1))
        self.switch = self.app.harness_switch

    def topic(self):
        return read_topic(self.source)

    @staticmethod
    def opening():
        """CardKit while a switch card goes out: created as card_1, sent as om_switch."""
        return FakeCardKit({'data': {'card_id': 'card_1'}}, sent=('om_switch', False))

    def open(self, click='click-open', **raw):
        topic = self.topic()
        value = {'kind': hs.OPEN, 'topic_id': self.source.topic_id, 'job_id': 1, 'harness_id': topic['harness_id'],
                 'binding_revision': topic.get('binding_revision', 1), **raw}
        with self.opening() as lark:
            self.switch.open(self.ctx, {'chat_id': 'chat', 'event_id': click, 'message_id': 'om_task'}, value)
        send = lark.send
        forms = sorted((self.ctx.state_dir / 'switch-forms').glob('*.json'), key=lambda p: p.stat().st_mtime)
        return (json.loads(forms[-1].read_text()), forms[-1], send) if forms else (None, None, send)

    def click(self, form, kind, option=None, tag=''):
        event = {'chat_id': 'chat', 'event_id': 'ev-' + kind + str(option) + tag, 'message_id': form['message_id'],
                 'action_value': {'kind': kind, 'token': form['token']}, 'option': option}
        with mock.patch.object(self.switch, 'submit', side_effect=lambda busy, handler, *args: handler(*args) or True), \
                FakeCardKit({'data': {}}):
            self.switch.dispatch(self.ctx, event)

    @staticmethod
    def notice(path):
        return json.loads(path.read_text()).get('notice', '')

    def switch_to(self, key='claude:claude-model', tag='', redaction=None):
        """Switch to ``key`` (and 敏感词脱敏 ``redaction`` 'on'/'off'); a second switch in one test gives its own
        ``tag``: the clicks are new events."""
        form, path, _ = self.open(click='click-open' + tag)
        self.click(form, hs.MODEL, key, tag)
        if redaction:
            self.click(json.loads(path.read_text()), hs.REDACTION, redaction, tag)
        self.click(json.loads(path.read_text()), hs.CONFIRM, tag=tag)
        return json.loads(path.read_text())

    def test_the_menu_lists_the_chats_recent_models_and_a_model_comes_with_its_usual_budget(self):
        now = time.time()
        model_usage.record(self.ctx.state_dir, 'chat', 'dsh:dsh-model', 'xhigh', '', 'launch', now=now - 2)
        model_usage.record(self.ctx.state_dir, 'chat', 'claude:claude-model', 'max', '', 'launch', now=now - 1)
        form, path, _ = self.open()
        # The topic's own model first and preselected with its budget, then the chat's latest.
        self.assertEqual(list(form['models'])[:3], ['codex:codex-model', 'claude:claude-model', 'dsh:dsh-model'])
        self.assertEqual((form['selected_model'], form['selected_effort']), ('codex:codex-model', 'high'))
        self.click(form, hs.MODEL, 'claude:claude-model')
        self.assertEqual(json.loads(path.read_text())['selected_effort'], 'max')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual((self.topic()['model'], self.topic()['effort']), ('claude-model', 'max'))
        latest = model_usage.recent(self.ctx.state_dir, 'chat')[-1]
        self.assertEqual((latest['model'], latest['effort'], latest['kind']), ('claude:claude-model', 'max', 'switch'))

    def test_a_message_routed_before_a_switch_goes_to_the_new_harness(self):
        """换配置 committed while the message was being read (its details, ~0.5 s): it is submitted to the
        topic's new Harness, and its history line written once."""
        event = self.event('m2', 'please continue', root='src')
        message = type('Message', (), {'text': 'please continue', 'display_text': 'please continue', 'event': event})()

        def enrich(*args, **kwargs):
            self.switch_to()
            return message
        with mock.patch('covey.app.app.enrich_incoming_event', side_effect=enrich):
            self.app.handle_event(event, self.ctx)
        jobs = [r for r in list_job_records(self.ctx.state_dir, limit=0) if r.get('message_id') == 'm2']
        self.assertEqual([(r['backend'], r['status']) for r in jobs], [('claude', 'queued')])
        lines = self.source.history_path.read_text().splitlines()
        self.assertEqual(sum('please continue' in line for line in lines), 1)

    def test_a_message_its_harness_moved_from_twice_is_answered(self):
        from covey.app.app import HARNESS_CHANGED_REPLY
        from covey.app.submission import HarnessChanged, Submission
        event = self.event('m3', 'please continue', root='src')
        message = type('Message', (), {'text': 'please continue', 'display_text': 'please continue', 'event': event})()
        with mock.patch('covey.app.app.enrich_incoming_event', return_value=message), \
                mock.patch.object(Submission, 'enqueue_chat_job', side_effect=HarnessChanged('moved')):
            self.app.handle_event(event, self.ctx)
        self.assertEqual(self.said()[-1], HARNESS_CHANGED_REPLY)

    def test_a_topic_on_a_removed_providers_model_is_refused_and_can_switch_to_a_live_one(self):
        # TRAE and 豆包 were removed for good (the owner, 2026-11-05): a topic still on one of their models gets a
        # notice naming the provider, no job, and 换配置 opens on it and moves it to a model that runs.
        del self.app.apps['claude'].models.prepare_submission_context  # The real one, as the Bridge runs it.
        for n, (model, name) in enumerate((("traex/meilin/claude-opus-5-5", "TRAE"), ("doubao/claude-opus-5-5", "豆包"))):
            with self.subTest(model=model):
                data = self.topic()
                data.update(harness_id='claude', model=model, effort='xhigh')
                write_topic(self.source, data)
                self.receive(f'gone-{n}', 'continue please', root='src')
                self.assertIn(f"已下线的供应商「{name}」", self.said()[-1])
                self.assertIn("换配置", self.said()[-1])
                self.assertTrue(self.app.runtime.jobs.queue.empty())
                form, path, _ = self.open(click=f'open-gone-{n}')
                self.assertEqual(form['current']['key'], 'claude:' + model)
                self.assertNotIn('claude:' + model, form['models'])
                self.assertIn('确认更换', json.dumps(switch_form.build_switch_card(form), ensure_ascii=False))
                self.click(form, hs.MODEL, 'codex:codex-model')
                self.click(json.loads(path.read_text()), hs.CONFIRM)
                self.assertEqual(json.loads(path.read_text())['status'], 'done')
                self.assertEqual((self.topic()['harness_id'], self.topic()['model']), ('codex', 'codex-model'))
        self.receive('live', 'continue please', root='src')
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.model), ('codex', 'codex-model'))

    def test_a_topic_of_the_doubao_harness_gets_the_retired_notice_and_nothing_runs(self):
        # 豆包 was a Harness until 2026-11-03; topics never moved to Claude Code keep harness_id "doubao". Its native
        # session is no Claude session of this release, so the topic is retired like any removed Harness's.
        data = self.topic()
        data.update(harness_id='doubao', model='claude-opus-5-5')
        write_topic(self.source, data)
        self.receive('old', 'continue please', root='src')
        self.assertEqual(self.said()[-1], retired_notice('doubao'))
        self.assertIn('豆包', self.said()[-1])
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        form, _, send = self.open(click='open-doubao')
        self.assertIsNone(form)  # No 换配置 card for a topic no Harness of this release can run.
        send.assert_not_called()

    def test_the_switch_card_says_to_pick_another_model_while_the_current_one_is_chosen(self):
        # T194: the current model is preselected and 确认更换 on it changed nothing, without a word on the card;
        # the 2026-11-03 client check: the button was blue over the note, now greyed out until a select changes.
        def confirm(card):
            return next(e for e in walk_dicts(card) if e.get('tag') == 'button' and e['text']['content'] == '确认更换')
        form, path, _ = self.open()
        self.assertEqual(form['current']['key'], 'codex:codex-model')
        self.assertIn(switch_form.UNCHANGED, json.dumps(switch_form.build_switch_card(form), ensure_ascii=False))
        self.assertIs(confirm(switch_form.build_switch_card(form)).get('disabled'), True)
        self.click(form, hs.MODEL, 'claude:claude-model')
        moved = json.loads(path.read_text())
        self.assertNotIn(switch_form.UNCHANGED, json.dumps(switch_form.build_switch_card(moved), ensure_ascii=False))
        self.assertNotIn('disabled', confirm(switch_form.build_switch_card(moved)))
        # A card opened before the current key was kept says nothing, as before.
        old = {**form, 'current': {k: v for k, v in form['current'].items() if k != 'key'}}
        self.assertNotIn(switch_form.UNCHANGED, json.dumps(switch_form.build_switch_card(old), ensure_ascii=False))

    def tap(self, value, message_id):
        """A tap on a row or button whose value is ``value``, as the client sends it (no select option): the
        card the redraw draws."""
        event = {'operator_id': 'owner', 'chat_id': 'chat', 'event_id': 'tap-' + json.dumps(value),
                 'message_id': message_id, 'action_value': value}
        with mock.patch.object(self.switch, 'submit', side_effect=lambda busy, handler, *args: handler(*args) or True), \
                mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace, FakeCardKit({'data': {}}):
            self.app.handle_card_action_event(event, self.ctx)
        return replace.call_args.args[4] if replace.call_args else None

    def test_a_rows_options_open_under_it_and_a_choice_marks_the_row(self):
        # No select on the phone (2026-11-04): as on the launch card, a row's options open under it in the client.
        form, path, _ = self.open()
        rows = settings_rows(switch_form.build_switch_card(form))
        self.assertFalse(any(row['open'] for row in rows.values()))
        other = next(value for value, _ in rows['effort']['options']
                     if value != f"{form['config_revision']}:{form['selected_effort']}")
        card = self.tap(rows['effort']['taps'][other], form['message_id'])
        record = json.loads(path.read_text())
        self.assertEqual((record['page'], record['selected_effort']), ('', other.split(':', 1)[1]))
        self.assertIn("**<font color='blue'>思考预算</font>**", json.dumps(card, ensure_ascii=False))
        # New row IDs on each redraw: the client keeps a fold's state by ID, and the chosen row should close.
        ids = lambda c: {e['element_id'] for e in walk_dicts(c) if str(e.get('element_id', '')).startswith('set_')}
        self.assertFalse(ids(card) & ids(switch_form.build_switch_card(form)))
        self.assertTrue(all(len(i) <= 20 for i in ids(card)))
        # A row's tap from a card sent with options pages draws that row open; a choice there, the current one too,
        # closes it.
        card = self.tap({'kind': switch_form.PAGE, 'token': form['token'], 'page': 'run_mode', 'current': 'direct'},
                        form['message_id'])
        self.assertEqual([key for key, row in settings_rows(card).items() if row['open']], ['run_mode'])
        current = settings_rows(card)['run_mode']['current']
        card = self.tap(settings_rows(card)['run_mode']['taps'][current], form['message_id'])
        record = json.loads(path.read_text())
        self.assertEqual((record['page'], record.get('selected_run_mode') or 'direct'), ('', current))
        self.assertFalse(any(row['open'] for row in settings_rows(card).values()))
        self.assertIn('确认更换', json.dumps(card, ensure_ascii=False))

    def test_a_budget_alone_redraws_the_card_marked_and_unlocks_confirm(self):
        # 469f135 greyed 确认更换 until a select changed, but a budget, work mode, workspace or run mode was only
        # recorded and the card kept the grey button (2026-11-03). Each choice now redraws it, its row marked blue.
        def confirm(card):
            return next(e for e in walk_dicts(card) if e.get('tag') == 'button' and e['text']['content'] == '确认更换')
        form, path, _ = self.open()
        other = next(value for value, _ in effort_options(form['models'][form['selected_model']])[0]
                     if value != form['selected_effort'])
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(form, hs.BUDGET, f"{form['config_revision']}:{other}")
        card = replace.call_args.args[4]
        text = json.dumps(card, ensure_ascii=False)
        self.assertNotIn('disabled', confirm(card))
        self.assertIn("**<font color='blue'>思考预算</font>**", text)
        self.assertIn("<font color='grey'>模型</font>", text)  # Unchanged rows stay grey.
        self.assertEqual(json.loads(path.read_text())['config_revision'], form['config_revision'] + 1)
        # Chosen back: nothing differs, the button is grey again and no row is marked.
        self.click(json.loads(path.read_text()), hs.BUDGET, f"{form['config_revision'] + 1}:{form['selected_effort']}")
        card = switch_form.build_switch_card(json.loads(path.read_text()))
        self.assertIs(confirm(card).get('disabled'), True)
        self.assertFalse(any("color='blue'" in e['header']['title']['content'] for e in walk_dicts(card)
                             if str(e.get('element_id', '')).startswith('set_')))  # No row's label is marked.

    def test_open_posts_a_switch_card_in_the_topic_with_every_harness(self):
        form, _, send = self.open()
        self.assertEqual((form['status'], form['message_id'], form['harness_id']), ('ready', 'om_switch', 'codex'))
        self.assertEqual(send.call_args.kwargs['reply_to_message_id'], self.source.reply_to_message_id)
        self.assertEqual(form['selected_model'], 'codex:codex-model')
        self.assertEqual(form['selected_effort'], 'high')
        self.assertIn('dsh:dsh-model', form['models'])
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        harnesses = page_options(switch_form.build_switch_card({**form, 'page': 'harness'}))
        self.assertIn(('claude', 'Claude Code'), harnesses)  # Every Harness, on the Harness row's page.
        for text in ('确认更换', '取消', 'Codex', 'Codex 自带', 'Codex Model', 'project', '原目录', '敏感词脱敏关',
                     "<font color='grey'>Harness</font>", "<font color='grey'>供应商</font>", "<font color='grey'>模型</font>",
                     "<font color='grey'>工作区</font>", "<font color='grey'>运行方式</font>", switch_form.SWITCH_NOTE):
            self.assertIn(text, card)
        self.assertNotIn(f'最近 {switch_handoff.HANDOFF_ROUNDS} 轮', card)  # The handoff's details are the model's.
        again, _, send = self.open()  # The same click redelivered.
        send.assert_not_called()

    def test_the_stop_first_paragraph_shows_only_while_the_topic_has_a_task(self):
        stop_first = '任务还在排队或执行时不能更换'
        form, path, _ = self.open(click='idle')
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        self.assertNotIn(stop_first, card)
        self.assertIn('当前：', card)
        self.assertIn('思考预算 high', card)  # One term, as on the select and the launch card.
        self.assertNotIn('推理强度', card)
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        form, path, _ = self.open(click='busy')
        self.assertIn(stop_first, json.dumps(switch_form.build_switch_card(form), ensure_ascii=False))
        upsert_job_record(self.ctx.state_dir, 'job-2', status='cancelled')
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(form, hs.MODEL, 'claude:claude-model')  # Redrawn as the topic is now.
        self.assertNotIn(stop_first, json.dumps(replace.call_args.args[4], ensure_ascii=False))

    def test_cancel_names_the_model_and_budget_the_topic_keeps(self):
        # S27: "已取消换配置，话题继续使用 MASO · GPT-5.6 Sol。" left out the budget the card's 当前 line shows.
        for click, current in (('keep', None), ('old', {'label': 'Codex · codex-model'})):  # One from before the budget.
            with self.subTest(click):
                form, path, _ = self.open(click=click)
                if current:
                    form['current'] = current
                    path.write_text(json.dumps(form, ensure_ascii=False))
                with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
                    self.click(form, hs.CANCEL)
                notice = json.dumps(replace.call_args.args[4], ensure_ascii=False)
                label = form['current']['label']
                self.assertIn(f"已取消换配置，话题继续使用 {label} · 思考预算 {'模型默认' if current else 'high'}。", notice)
                self.assertEqual(json.loads(path.read_text())['status'], 'cancelled')

    def test_confirm_rebinds_the_topic_and_keeps_its_workspace(self):
        workdir = self.topic()['workdir']
        revision = self.topic().get('binding_revision', 1)
        claude = self.app.apps['claude']
        write_session_id(self.source.history_path.parent / claude.spec.session_filename, str(uuid.uuid4()))
        done = self.switch_to()
        topic = self.topic()
        self.assertEqual(done['status'], 'done')
        self.assertEqual((topic['harness_id'], topic['model'], topic['effort']), ('claude', 'claude-model', 'xhigh'))
        self.assertEqual((topic['workdir'], topic['binding_revision']), (workdir, revision + 1))
        switch = topic['switches'][-1]
        self.assertEqual((switch['from']['harness_id'], switch['from']['session_id']), ('codex', self.session))
        self.assertEqual(switch['revision'], revision + 1)
        # The new Harness starts fresh, even with a pointer left from an earlier binding.
        app, ctx = self.route('src')
        self.assertIs(app, claude)
        self.assertIsNone(app.sessions.read_session_id(ctx.session_path))
        self.assertEqual((ctx.model, ctx.effort, str(ctx.workdir)), ('claude-model', 'xhigh', workdir))
        self.receive('next', 'continue please', root='src')
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.model), ('claude', 'claude-model'))

    @requires_extension()
    def test_a_redacting_topic_can_switch_harness_and_redaction_turns_off_with_notice(self):
        # The owner, 2026-11-04 17:13 (T178, redaction on): 当前切换不了Harness. Only Claude Code was listed, a tap on
        # it changed nothing, and the reason was small print at the bottom.
        redacting(self)
        data = self.topic()
        data.update(redaction=True)
        write_topic(self.source, data)
        form, path, _ = self.open()
        self.assertEqual((form['selected_model'], form['selected_redaction']), ('claude:claude-model', 'on'))
        self.assertEqual(len(page_options(switch_form.build_switch_card({**form, 'page': 'harness'}))), len(NAMES))
        self.assertNotIn(switch_form.AUTO_OFF_NOTE, json.dumps(switch_form.build_switch_card(form), ensure_ascii=False))
        # Another Harness: chosen, 脱敏 turned off on the card with the reason, and 确认更换 asks first.
        self.click(form, hs.HARNESS, 'dsh')
        form = json.loads(path.read_text())
        self.assertEqual((form['selected_model'], form['selected_redaction']), ('dsh:dsh-model', 'off'))
        card = switch_form.build_switch_card(form)
        self.assertIn(switch_form.AUTO_OFF_NOTE, json.dumps(card, ensure_ascii=False))
        self.assertIn('脱敏', switch_form.changes(form))
        confirm = next(e for e in walk(card['body']['elements']) if e.get('tag') == 'button' and '确认更换' in json.dumps(e, ensure_ascii=False))
        self.assertEqual(confirm['confirm']['title']['content'], switch_form.OFF_TITLE)
        # Back to Claude: 脱敏 on again, as the owner had it; set by hand, it stays as set.
        self.click(form, hs.HARNESS, 'claude')
        form = json.loads(path.read_text())
        self.assertEqual((form['selected_model'], form['selected_redaction']), ('claude:claude-model', 'on'))
        self.click(form, hs.REDACTION, 'off')
        self.click(json.loads(path.read_text()), hs.HARNESS, 'dsh')
        self.click(json.loads(path.read_text()), hs.HARNESS, 'claude')
        self.assertEqual(json.loads(path.read_text())['selected_redaction'], 'off')
        # A model that cannot redact with 脱敏 still on (a record forced so, an older card): refused on the card.
        forced = {**json.loads(path.read_text()), 'selected_model': 'dsh:dsh-model', 'selected_redaction': 'on'}
        path.write_text(json.dumps(forced, ensure_ascii=False))
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(forced, hs.CONFIRM)
        self.assertIn(hs.REDACTION_ONLY, self.notice(path))
        self.assertEqual(replace.call_args.args[4]['schema'], '2.0')  # The whole card, redrawn.
        self.assertEqual(self.topic()['harness_id'], 'codex')
        self.click(json.loads(path.read_text()), hs.HARNESS, 'dsh')  # Chosen again: 脱敏 follows it off.
        self.assertEqual(json.loads(path.read_text())['selected_redaction'], 'off')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        self.assertEqual(self.topic()['harness_id'], 'dsh')
        self.assertNotIn('redaction', self.topic())

    def test_redaction_alone_changes_the_topic_and_keeps_its_session(self):
        redacting(self, 'codex')
        form, path, _ = self.open()
        revision = self.topic().get('binding_revision', 1)
        self.click(form, hs.REDACTION, 'on')
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        topic = self.topic()
        self.assertEqual((topic['redaction'], topic.get('binding_revision', 1)), (True, revision))
        self.assertNotIn('switches', topic)
        self.assertEqual(self.codex.sessions.read_session_id(self.source.session_path), self.session)
        text = json.dumps(replace.call_args.args[4], ensure_ascii=False)
        self.assertIn('敏感词脱敏：关 → 开', text)
        self.assertIn('会话不变', text)

    def test_another_workspace_rebinds_the_topic_there_with_a_fresh_session(self):
        other = self.root / 'other'
        other.mkdir()
        resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, str(other))
        form, path, _ = self.open()
        self.assertEqual((form['selected_workspace'], form['selected_run_mode']), ('project', 'direct'))
        self.assertIn('other', form['workspaces'])
        self.click(form, hs.WORKSPACE, 'nowhere')  # Not offered: dropped.
        self.click(form, hs.WORKSPACE, 'other')
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        topic = self.topic()
        self.assertEqual((topic['workspace_name'], topic['workdir'], topic['harness_id']), ('other', str(other.resolve()), 'codex'))
        switch = topic['switches'][-1]
        self.assertEqual((switch['from']['workdir'], switch['to']['workdir']), (str(self.work), str(other.resolve())))
        self.assertIsNone(self.codex.sessions.read_session_id(self.source.session_path))  # A fresh session there.
        self.assertIn('工作区：project（原目录） → other（原目录）', json.dumps(replace.call_args.args[4], ensure_ascii=False))
        note = switch_handoff.handoff_note(topic, Path('/t.md'), 5)
        self.assertIn(f'工作目录从 {self.work} 换成了上面的 Current workdir', note)

    def test_a_harness_the_device_does_not_declare_is_refused_and_a_declared_one_switches(self):
        from support.fake_device import FakeDevice
        from covey.core import devices
        document = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-switch",
                    "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["claude"],
                                         "spaces": ["owner_private", "team"]}}}
        (self.root / "devices.json").write_text(json.dumps(document))
        patcher = mock.patch.dict(os.environ, {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.root / "devices.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(device.cleanup(), []))
        opened = mock.patch.object(devices, "open_transport", side_effect=lambda *a, **k: device.transport)
        opened.start()
        self.addCleanup(opened.stop)
        self.app.runtime.access.allows_device = lambda chat, alias: True
        value = "gpu1:%s" % device.home
        resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, value)
        name = next(n for n, v in load_config(self.ctx.state_dir, self.ctx.default_workdir)["workspaces"].items() if v == value)
        form, path, _ = self.open()
        self.click(form, hs.WORKSPACE, name)
        before = self.topic()
        self.click(json.loads(path.read_text()), hs.CONFIRM)  # The topic's Codex on a device that declares Claude only.
        record = json.loads(path.read_text())
        self.assertEqual(record["status"], "ready")
        self.assertIn("设备 gpu1 只声明了 claude（devices.json 的 harnesses），没有", record["notice"])
        self.assertIn("话题配置没有改变", record["notice"])
        self.assertEqual((self.topic()["harness_id"], self.topic()["workdir"]), (before["harness_id"], before["workdir"]))
        self.click(record, hs.MODEL, "claude:claude-model")
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())["status"], "done")
        self.assertEqual((self.topic()["harness_id"], self.topic()["workdir"]), ("claude", value))

    def test_a_worktree_is_made_from_the_workspaces_commit_and_kept_when_the_switch_is_refused(self):
        for where in ('covey.core.workspaces.worktree_root', 'covey.app.binding.worktree_root'):
            patcher = mock.patch(where, return_value=self.root / 'worktrees')
            patcher.start()
            self.addCleanup(patcher.stop)
        init_git_repo(self.work)
        (self.work / 'README.md').write_text('repo\n')
        commit_all(self.work, 'init')
        form, path, _ = self.open()
        self.click(form, hs.RUN_MODE, 'worktree')
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        record = json.loads(path.read_text())
        self.assertIn('还有任务在排队或执行', record['notice'])
        made = record['made_worktree']
        self.assertTrue(Path(made['workdir']).is_dir())
        self.assertEqual(self.topic()['workdir'], str(self.work))
        upsert_job_record(self.ctx.state_dir, 'job-2', status='cancelled')
        with mock.patch.object(hs, 'add_topic_worktree') as again:
            self.click(record, hs.CONFIRM)
        again.assert_not_called()  # The one made before.
        topic = self.topic()
        self.assertEqual((topic['workdir'], hs.run_mode_of(topic['workdir'])), (made['workdir'], 'worktree'))
        self.assertEqual(run_git(Path(made['workdir']), 'rev-parse', '--abbrev-ref', 'HEAD').strip(), made['branch'])
        self.assertTrue(made['workdir'].startswith(str(self.root)), made['workdir'])  # Never the live worktrees.

    def test_model_change_rebuilds_the_whole_card_with_that_models_budgets(self):
        form, path, _ = self.open()
        with mock.patch.object(self.switch, 'replace', return_value=True) as replace, \
                mock.patch.object(self.switch, 'submit', side_effect=lambda busy, handler, *args: handler(*args) or True):
            self.switch.dispatch(self.ctx, {'chat_id': 'chat', 'message_id': 'om_switch', 'option': 'dsh:dsh-model',
                                            'action_value': {'kind': hs.MODEL, 'token': form['token']}})
        card = replace.call_args.args[4]
        self.assertEqual(card['schema'], '2.0')  # A whole card, never one element.
        self.assertEqual(replace.call_args.args[3]['selected_model'], 'dsh:dsh-model')
        self.assertIn('2:top_minus_one', json.dumps(card))

    @requires_extension()
    def test_the_switch_card_lists_the_topics_harness_first_and_each_harness_its_models(self):
        write_maso_catalog(self.ctx.state_dir)
        form, path, _ = self.open()
        fixed = list(available_models(self.app.topics, self.ctx))
        self.assertEqual(list(form['models']), ['codex:codex-model'] + [key for key in fixed if key != 'codex:codex-model'])

        def options(name):  # What a row's options page lists.
            return page_options(switch_form.build_switch_card({**json.loads(path.read_text()), 'page': name}))
        self.assertEqual([v for v, _ in options('harness')], ['codex', 'claude', 'maso', 'dsh', 'pi', 'agy'])
        self.assertEqual([name for name, _ in itertools.groupby(key.split(':', 1)[0] for key in fixed)], sorted(NAMES, key=harness_rank))
        self.assertEqual(options('model'), [('codex:codex-model', 'Codex Model')])
        self.click(form, hs.HARNESS, 'maso')
        self.assertEqual(json.loads(path.read_text())['selected_model'], 'maso:seed-stable')
        self.assertEqual(options('provider'), [('maso', 'MASO 自带')])
        self.assertEqual([v for v, _ in options('model')], list(MASO_MENU))
        self.click(json.loads(path.read_text()), hs.HARNESS, 'codex')  # Back to the topic's own model and budget.
        record = json.loads(path.read_text())
        self.assertEqual((record['selected_model'], record['selected_effort']), ('codex:codex-model', 'high'))

    def test_a_provider_choice_offers_that_providers_models(self):
        rows = {'codex-model': {'display_name': 'codex model', 'levels': ['high'], 'default': 'high'},
                'acme/gpt-6-astra': {'display_name': 'gpt-6-astra (Acme)', 'levels': ['high'], 'default': 'high'}}
        write_private_text(self.ctx.state_dir / 'launch-models' / 'codex.json',
                           json.dumps({'updated_at': time.time(), 'models': rows}))
        form, path, _ = self.open()
        card = switch_form.build_switch_card(form)
        self.assertEqual(settings_rows(card)['provider']['current'], 'openai')
        self.assertEqual(page_options(switch_form.build_switch_card({**form, 'page': 'provider'})),
                         [('openai', 'Codex 自带'), ('acme', 'Acme')])
        self.click(form, hs.PROVIDER, 'acme')
        record = json.loads(path.read_text())
        self.assertEqual(record['selected_model'], 'codex:acme/gpt-6-astra')
        models = page_options(switch_form.build_switch_card({**record, 'page': 'model'}))
        self.assertEqual([text for _, text in models], ['GPT-6 Astra'])

    def bind_maso(self, model, effort):
        """The topic as a launch card from before one entry per model left it: bound to a MASO preset."""
        write_maso_catalog(self.ctx.state_dir)
        data = self.topic()
        data.update(harness_id='maso', model=model, effort=effort)
        write_topic(self.source, data)

    @requires_extension()
    def test_a_topic_on_a_maso_preset_shows_its_model_and_budget(self):
        self.bind_maso('gpt56_sol_reasoning_xhigh', 'xhigh')
        form, path, _ = self.open()
        self.assertEqual((form['selected_model'], form['selected_effort']), ('maso:gpt56_sol', 'xhigh'))
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        self.assertIn('当前：MASO · GPT-5.6 Sol · 思考预算 xhigh', card)
        self.assertEqual(settings_rows(switch_form.build_switch_card(form))['effort']['current'], '1:xhigh')
        self.click(form, hs.CONFIRM)  # The same model and budget.
        self.assertIn(switch_form.UNCHANGED, self.notice(path))
        self.assertEqual((self.topic()['model'], self.topic()['effort']), ('gpt56_sol_reasoning_xhigh', 'xhigh'))
        self.click(json.loads(path.read_text()), hs.BUDGET, '1:max')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        self.assertEqual((self.topic()['harness_id'], self.topic()['model'], self.topic()['effort']),
                         ('maso', 'gpt56_sol_reasoning_max', 'max'))  # The preset of that budget.

    @requires_extension()
    def test_a_maso_topic_changes_its_work_mode_through_a_switch(self):
        self.bind_maso('gpt56_sol_reasoning_xhigh', 'xhigh')
        form, path, _ = self.open()
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        self.assertEqual(settings_rows(switch_form.build_switch_card(form))['work_mode']['current'], 'default_v1')
        self.assertIn('Adaptive（派子 Agent）', json.dumps(switch_form.build_switch_card({**form, 'page': 'work_mode'}),
                                                         ensure_ascii=False))
        self.assertIn(switch_form.UNCHANGED, card)  # Unchanged so far; the mode is a change too.
        self.click(form, hs.WORK_MODE, 'orch_v2')  # Not offered: dropped.
        self.assertEqual(json.loads(path.read_text())['selected_work_mode'], '')
        self.click(form, hs.WORK_MODE, 'maso_adaptive')
        revision = self.topic().get('binding_revision', 1)
        self.click(json.loads(path.read_text()), hs.CONFIRM)  # The mode alone changed: a switch, a new session.
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        topic = self.topic()
        self.assertEqual((topic['model'], topic['effort'], topic['work_mode'], topic['binding_revision']),
                         ('gpt56_sol_reasoning_xhigh', 'xhigh', 'maso_adaptive', revision + 1))
        self.assertEqual((topic['switches'][-1]['from']['work_mode'], topic['switches'][-1]['to']['work_mode']),
                         ('', 'maso_adaptive'))
        form, path, _ = self.open(click='click-again')
        card = json.dumps(switch_form.build_switch_card(form), ensure_ascii=False)
        self.assertIn('当前：MASO · GPT-5.6 Sol · 思考预算 xhigh · Adaptive', card)
        self.assertEqual(settings_rows(switch_form.build_switch_card(form))['work_mode']['current'], 'maso_adaptive')
        self.click(form, hs.CONFIRM)
        self.assertIn(switch_form.UNCHANGED, self.notice(path))
        # Another Harness has no modes: its card has no mode select and the topic keeps none.
        self.click(json.loads(path.read_text()), hs.MODEL, 'claude:claude-model')
        self.assertNotIn('工作模式', json.dumps(switch_form.build_switch_card(json.loads(path.read_text())),
                                               ensure_ascii=False))
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(json.loads(path.read_text())['status'], 'done')
        self.assertNotIn('work_mode', self.topic())

    @requires_extension()
    def test_a_topic_on_a_plain_maso_preset_keeps_the_native_default(self):
        self.bind_maso('gpt56_sol', '')
        form, path, _ = self.open()
        self.assertEqual((form['selected_model'], form['selected_effort']), ('maso:gpt56_sol', 'default'))
        self.click(form, hs.CONFIRM)
        self.assertIn(switch_form.UNCHANGED, self.notice(path))

    def test_identical_choice_and_busy_topics_are_refused(self):
        form, path, _ = self.open()
        self.click(form, hs.CONFIRM)  # Still Codex · codex-model · high.
        self.assertIn(switch_form.UNCHANGED, self.notice(path))
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        self.click(json.loads(path.read_text()), hs.MODEL, 'claude:claude-model')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertIn('还有任务在排队或执行', self.notice(path))
        self.assertEqual(self.topic()['harness_id'], 'codex')
        form, _, send = self.open(click='click-busy')
        self.assertEqual(form['status'], 'ready')  # Choosing a model never waits for the old one.
        send.assert_called_once()
        self.assertEqual(self.topic()['harness_id'], 'codex')

    def test_a_failed_card_refresh_or_a_full_queue_still_answers(self):
        form, path, _ = self.open()
        with mock.patch.object(self.switch, 'replace', return_value=False), \
                mock.patch.object(self.switch, 'submit', side_effect=lambda busy, handler, *args: handler(*args) or True):
            self.switch.dispatch(self.ctx, {'chat_id': 'chat', 'message_id': 'om_switch', 'event_id': 'ev-model',
                                            'option': 'claude:claude-model',
                                            'action_value': {'kind': hs.MODEL, 'token': form['token']}})
        self.assertEqual(self.said()[-1], hs.REBUILD_FAILED)
        with mock.patch.object(self.switch, 'replace', return_value=False), \
                mock.patch.object(self.switch, 'submit', side_effect=lambda busy, handler, *args: handler(*args) or True):
            self.switch.dispatch(self.ctx, {'chat_id': 'chat', 'message_id': 'om_switch', 'event_id': 'ev-confirm',
                                            'action_value': {'kind': hs.CONFIRM, 'token': form['token']}})
        self.assertIn('请重新选一次模型再确认', self.said()[-1])  # The card still cannot be updated.
        self.assertEqual(self.topic()['harness_id'], 'codex')
        # Once the card updates again, the refusal redraws it with the chosen model's budgets.
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        record = json.loads(path.read_text())
        self.assertIn('请确认思考预算后再点确认更换', record['notice'])
        self.assertFalse(record['config_pending'])
        self.click(record, hs.CONFIRM)
        self.assertEqual(self.topic()['harness_id'], 'claude')
        topic = self.topic()
        with mock.patch.object(self.switch, 'submit', return_value=False):
            self.switch.dispatch(self.ctx, {'chat_id': 'chat', 'message_id': 'om_task', 'event_id': 'ev-full',
                                            'action_value': {'kind': hs.OPEN, 'topic_id': self.source.topic_id,
                                                             'harness_id': topic['harness_id'],
                                                             'binding_revision': topic.get('binding_revision', 1)}})
        self.assertIn('换配置卡片暂时没有打开（处理队列已满）', self.said()[-1])

    def test_a_switch_message_is_an_ordinary_message(self):
        # The task and question cards' 🔁 换配置 opens the switch card; the text does not (the bot menu, 2026-11-03).
        with mock.patch.object(self.switch, 'dispatch') as open_switch, \
                mock.patch('covey.app.unified.BridgeApp.handle_event', autospec=True) as model:
            for text in ('换配置', '切换配置'):
                self.receive('switch-' + text, text, root='src')
            self.receive('new-topic', '换配置')
        open_switch.assert_not_called()
        self.assertEqual(model.call_count, 3)

    def test_running_topic_can_choose_then_stop_and_confirm(self):
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        form, path, _ = self.open()
        self.click(form, hs.MODEL, 'claude:claude-model')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertIn('停止完成后再确认更换', self.notice(path))
        self.assertEqual(self.topic()['harness_id'], 'codex')
        upsert_job_record(self.ctx.state_dir, 'job-2', status='cancelled')
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(self.topic()['harness_id'], 'claude')

    def test_a_refusal_stays_on_the_switch_card_and_goes_with_the_switch(self):
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        form, path, _ = self.open()
        self.click(form, hs.MODEL, 'claude:claude-model')
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertEqual(self.said(), [])  # No topic message that would outlive the switch.
        card = json.dumps(replace.call_args.args[4], ensure_ascii=False)
        self.assertIn('还没有换', card)
        self.assertIn('停止完成后再确认更换', card)
        self.assertIn('确认更换', card)  # Still the form, with the choice kept.
        self.assertEqual(replace.call_args.args[3]['selected_model'], 'claude:claude-model')
        self.click(json.loads(path.read_text()), hs.MODEL, 'dsh:dsh-model')
        self.assertEqual(self.notice(path), '')  # A new choice drops the old answer.
        self.click(json.loads(path.read_text()), hs.CONFIRM)
        upsert_job_record(self.ctx.state_dir, 'job-2', status='cancelled')
        with mock.patch.object(self.switch, 'replace', wraps=self.switch.replace) as replace:
            self.click(json.loads(path.read_text()), hs.CONFIRM)
        done = json.dumps(replace.call_args.args[4], ensure_ascii=False)
        self.assertIn('已换配置', done)
        self.assertNotIn('还没有换', done)
        self.assertEqual((self.topic()['harness_id'], self.said()), ('dsh', []))

    def test_a_done_card_cardkit_did_not_confirm_keeps_its_sequence_reserved(self):
        # Hard rule 3: switch-done reserved the next sequence before its PUT; the record saved after an
        # unconfirmed PUT must hold it, or the next write to the card would reuse it.
        form, path, _ = self.open()
        self.click(form, hs.MODEL, 'claude:claude-model')
        form = json.loads(path.read_text())
        with mock.patch.object(self.switch.topics.messenger, 'put_card', return_value=None):
            self.click(form, hs.CONFIRM)
        saved = json.loads(path.read_text())
        self.assertEqual((saved['status'], saved['sequence']), ('done', form['sequence'] + 1))
        self.assertEqual(self.topic()['harness_id'], 'claude')

    def test_a_refusal_falls_back_to_a_topic_message_when_the_card_cannot_change(self):
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='running', backend='codex', kind='chat')
        form, path, _ = self.open()
        self.click(form, hs.MODEL, 'claude:claude-model')
        with mock.patch.object(self.switch, 'replace', return_value=False):
            self.click(json.loads(path.read_text()), hs.CONFIRM)
        self.assertIn('换配置未完成：本话题还有任务在排队或执行', self.said()[-1])
        self.assertEqual(self.notice(path), '')

    def test_opening_does_not_bypass_answers_still_being_delivered(self):
        question = self.ctx.state_dir / 'questions' / 'inflight.json'
        for status in ('submitting', 'uncertain'):
            with self.subTest(status=status):
                write_private_text(question, json.dumps({'status': status, 'topic_id': self.source.topic_id,
                                                        'chat_id': 'chat'}))
                form, path, _ = self.open(click='click-' + status)
                self.assertEqual(form['status'], 'ready')
                self.click(form, hs.MODEL, 'claude:claude-model')
                self.click(json.loads(path.read_text()), hs.CONFIRM)
                self.assertEqual(self.topic()['harness_id'], 'codex')
                self.assertIn('有回答正在提交或送达未确认', self.notice(path))
                self.assertEqual(json.loads(question.read_text())['status'], status)

    def test_failed_stopped_and_interrupted_tasks_keep_a_switch_control_without_a_reply(self):
        for status in ('failed', 'cancelled', 'interrupted'):
            with self.subTest(status=status):
                job = BridgeReplyJob(job_id=9, user_text='work', chat_id='chat', message_id='job-' + status,
                                     timeout=0, topic_id=self.source.topic_id)
                job.progress = SimpleNamespace(card_id='card-failed', message_id='message-failed', sequence=0)
                with mock.patch.object(self.codex.cards, 'set_task_element', return_value=True) as panel:
                    self.codex.record_job_state(self.source, job, status, delivery_ok=False)
                    self.codex.record_job_state(self.source, job, status, delivery_ok=True)
                panel.assert_called_once()
                self.assertIn(hs.OPEN, json.dumps(panel.call_args.args[2]))

    def test_active_question_and_recovery_cards_offer_the_same_scoped_control(self):
        record = {'token': 'q1', 'topic_id': self.source.topic_id, 'job_id': 1, 'harness_id': 'codex',
                  'binding_revision': self.topic()['binding_revision'], 'blocking': True,
                  'questions': [{'id': 'a', 'question': 'deploy?'}]}
        job = SimpleNamespace(**record)
        cards = [build_stop_action_row(job, switch=card_look(job).switch)]
        for status in ('pending', 'answered', 'cancelled'):
            # The task card's own 🔁 换配置 is the one (the 2026-10-27 UI review); since 2026-11-04 an open question
            # card has none either (the owner: 为啥要这么多选项).
            self.assertNotIn(hs.OPEN, json.dumps(question_card({**record, 'status': status, 'delivery_state': 'failed'})))
        for status in ('failed', 'cancelled', 'interrupted'):
            cards.append(recovery_card({**record, 'status': status,
                'result_retry_policy': 'saved_delivery_only', 'native_resume_allowed': False}))
        for card in cards:
            text = json.dumps(card)
            self.assertIn(hs.OPEN, text)
            self.assertIn(self.source.topic_id, text)
            self.assertIn('"binding_revision": ' + str(record['binding_revision']), text)
            # CardKit rejects the whole card for one invalid component ID.
            ids = []

            def check_ids(node):
                if isinstance(node, dict):
                    if 'element_id' in node:
                        element_id = node['element_id']
                        self.assertRegex(element_id, r'^[A-Za-z][A-Za-z0-9_]{0,19}$')
                        self.assertNotIn(element_id, ids)
                        ids.append(element_id)
                    for value in node.values():
                        check_ids(value)
                elif isinstance(node, list):
                    for value in node:
                        check_ids(value)

            check_ids(card)

    def test_cards_from_before_the_switch_view_but_never_start_jobs(self):
        old_revision = self.topic().get('binding_revision', 1)
        self.switch_to()
        form, _, send = self.open(click='click-stale', harness_id='codex', binding_revision=old_revision)
        self.assertIn(hs.STALE_CARD, self.said()[-1])
        remember_topic_message(self.ctx.state_dir, self.source.topic_id, 'chat', 'old-card')
        seen = []
        with mock.patch.object(BridgeApp, 'handle_card_action_event', autospec=True,
                               side_effect=lambda app, event, ctx, item=None: seen.append(app.spec.name)):
            for kind in ('bridge_trace', 'bridge_job_retry', 'bridge_steer_requeue'):
                event = {**self.event('old-card'), 'operator_id': 'owner', 'event_id': 'ev-' + kind,
                         'action_value': {'kind': kind, 'topic_id': self.source.topic_id, 'job_id': 1, 'mode': 'fork'}}
                self.app.handle_card_action_event(event, self.ctx)
        self.assertEqual(seen, ['codex'])  # The old job's process view still opens in its own Harness.
        self.assertIn(hs.STALE_CARD, self.said()[-1])

    def forms(self):
        return len(list((self.ctx.state_dir / 'switch-forms').glob('*.json')))

    def test_a_task_after_a_switch_draws_its_switch_control_under_its_own_binding(self):
        self.switch_to()
        revision = self.topic()['binding_revision']
        self.receive('next', 'continue please', root='src')
        job = self.app.runtime.jobs.queue.get_nowait()
        claude = self.app.apps['claude']
        # A task runner builds its context from the job, not from the topic.
        ctx = context_for_job(self.app.harnesses['claude'].context, job)
        self.assertEqual((ctx.options['harness_id'], ctx.options.get('binding_revision', 1)), ('claude', revision))
        job.progress = SimpleNamespace(card_id='card-next', message_id='message-next', sequence=0)
        with mock.patch.object(claude.cards, 'set_task_element', return_value=True) as panel:
            claude.record_job_state(ctx, job, 'cancelled', delivery_ok=True)

        def values(node):
            if isinstance(node, dict):
                if node.get('kind') == hs.OPEN:
                    yield node
                for item in node.values():
                    yield from values(item)
            elif isinstance(node, list):
                for item in node:
                    yield from values(item)
        value, = values(panel.call_args.args[2])
        self.assertEqual((value['harness_id'], value['binding_revision']), ('claude', revision))
        before = self.forms()
        self.open(click='click-after-stop', **value)
        self.assertEqual(self.forms(), before + 1)
        self.assertNotIn(hs.STALE_CARD, ' '.join(self.said()))

    def test_cards_drawn_with_revision_one_open_when_their_task_ran_under_the_current_binding(self):
        data = self.topic()
        data['binding_revision'] = 3
        write_topic(self.source, data)
        upsert_job_record(self.ctx.state_dir, 'job-1', binding_revision=3, harness_id='codex')
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='done', backend='codex', harness_id='codex', kind='chat', binding_revision=2)
        self.open(click='click-current', binding_revision=1)
        self.assertEqual(self.forms(), 1)
        self.assertNotIn(hs.STALE_CARD, ' '.join(self.said()))
        self.open(click='click-earlier', job_id=2, binding_revision=1)
        self.assertEqual(self.forms(), 1)
        self.assertIn(hs.STALE_CARD, self.said()[-1])

    def test_pending_questions_of_the_old_binding_are_cancelled(self):
        checkpoint = self.root / 'cp.json'
        checkpoint.write_text(json.dumps({'requires_user': True, 'question': 'deploy?'}))
        upsert_job_record(self.ctx.state_dir, 'job-wait', topic_id=self.source.topic_id, chat_id='chat', job_id=3,
                          status='waiting_user', backend='codex', kind='chat', checkpoint_path=str(checkpoint))
        question = self.ctx.state_dir / 'questions' / 'q1.json'
        write_private_text(question, json.dumps({'token': 'q1', 'status': 'pending', 'topic_id': self.source.topic_id,
                                                 'chat_id': 'chat', 'job_id': 3,
                                                 'questions': [{'id': 'a', 'question': 'deploy?'}]}))
        with mock.patch.object(type(self.codex.questions), 'paint_question_state') as paint:
            self.switch_to()
        record = json.loads(question.read_text())
        self.assertEqual((record['status'], record['cancel_reason']), ('cancelled', 'switched'))
        paint.assert_called_once()
        self.assertIn('话题已换配置', json.dumps(question_element(record), ensure_ascii=False))
        self.assertEqual(find_job_record(self.ctx.state_dir, 'job-wait')['status'], 'continued')
        self.assertFalse(json.loads(checkpoint.read_text())['requires_user'])

    def test_first_run_after_the_switch_gets_the_transcript_and_recent_rounds(self):
        rollout = self.root / 'codex-home/sessions/2026/10/22' / f'rollout-2026-10-22T10-00-00-{self.session}.jsonl'
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{}\n')
        self.switch_to()
        append_history(self.source.history_path, 'user', 'continue please', {})
        app, ctx = self.route('src')
        rows, note = app.switch.switch_handoff(ctx, ['snapshot'])
        self.assertEqual([r['content'] for r in rows],
                         ['question 3', 'answer 3', 'question 4', 'answer 4', 'question 5', 'answer 5', 'continue please'])
        path = Path(note.split('完整会话记录：')[1].split('（')[0])
        self.assertTrue(path.is_file())
        self.assertIn('Codex · codex-model（high）', note)
        self.assertIn('Claude Code · claude-model（xhigh）', note)
        transcript = path.read_text()
        self.assertIn('第 1 轮 · 用户', transcript)
        self.assertIn('answer 1', transcript)
        self.assertIn(str(rollout), transcript)
        self.assertIn('以下由 Claude Code · claude-model（xhigh） 接手', transcript)
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        # Other Harnesses and topics without a switch are untouched.
        codex_ctx = self.codex.submission.event_context(self.app.harnesses['codex'].context, self.event('src'))
        self.assertEqual(self.codex.switch.switch_handoff(codex_ctx, ['snapshot']), (['snapshot'], ''))

    def test_the_first_turn_after_a_switch_starts_a_session_with_the_rules_at_system_level(self):
        self.switch_to()
        app, ctx = self.route('src')
        self.assertIsNone(app.sessions.capture_session(ctx)[1])
        new = str(uuid.uuid4())
        backend = InstructedBackend(sid=new)
        job = BridgeReplyJob(job_id=2, user_text='go on', chat_id='chat', message_id='om_go', timeout=0)
        with mock.patch.object(app.harness, 'backend', backend):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        self.assertIn('本话题刚从', backend.prompts[-1])
        self.assertNotIn(CHECKPOINT_RULES, backend.prompts[-1])
        self.assertIn(CHECKPOINT_RULES, backend.instructions[-1])
        self.assertTrue(started_with_system_rules(ctx, 'claude', new))
        self.assertFalse(started_with_system_rules(ctx, 'codex', self.session))

    def test_the_prompt_of_a_fresh_session_carries_the_handoff(self):
        self.switch_to()
        app, ctx = self.route('src')
        rows, note = app.switch.switch_handoff(ctx, [])
        prompt = first_turn_message('go on', rows) + note
        self.assertIn('assistant: answer 5', prompt)
        self.assertNotIn('answer 1\n', prompt)
        self.assertIn('本话题刚从', prompt)

    def leave_claude(self, redaction=None, rewound=False):
        """The topic on Claude with a session and a long last answer, then switched to Codex (with 敏感词脱敏
        ``redaction``; ``rewound``: a ↩️ 回退 of the session pending): the old session ID."""
        if self.topic()['harness_id'] != 'claude':
            self.switch_to()
        claude, ctx = self.route('src')
        old = str(uuid.uuid4())
        write_session_id(ctx.session_path, old)
        if rewound:
            (ctx.history_path.parent / 'rewind-next.json').write_text(json.dumps(
                {'session_id': old, 'keep': 'entry-kept', 'job_id': 1, 'token': 'tok', 'at': 1.0}))
        append_history(self.source.history_path, 'user', 'question 6', {})
        append_history(self.source.history_path, 'assistant', 'L' * 3000, {})
        upsert_job_record(self.ctx.state_dir, 'job-1', backend='claude', harness_id='claude',
                          binding_revision=self.topic()['binding_revision'])
        self.switch_to('codex:codex-model', tag='-2', redaction=redaction)
        self.assertEqual(self.topic()['harness_id'], 'codex')
        self.assertIs(self.topic()['switches'][-1]['from']['redaction'], False)
        return old

    def summarized(self, *answers):
        """The Codex app's first prompt after leaving Claude, with Claude's handoff_summary answering ``answers``."""
        calls = []

        def summary(ctx, session_id, prompt, *, redact, cancel=None, run_id=''):
            calls.append({'model': ctx.model, 'effort': ctx.effort, 'workdir': str(ctx.workdir),
                          'session': session_id, 'redact': redact, 'prompt': prompt, 'cancel': cancel,
                          'run_id': run_id})
            return answers[len(calls) - 1]
        app, ctx = self.route('src')
        prompts = []
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=summary, create=True):
            for n in range(len(answers) or 1):
                backend = InstructedBackend(sid=str(uuid.uuid4()))
                job = BridgeReplyJob(job_id=2 + n, user_text='go on', chat_id='chat', message_id=f'om_go{n}', timeout=0)
                with mock.patch.object(app.harness, 'backend', backend):
                    app.runs.generate_reply_for_worker(ctx, job, None, None)
                prompts.append(backend.prompts[-1])
                ctx.session_path.unlink(missing_ok=True)  # Its first run again, as after a failed one.
        return calls, prompts, ctx

    def test_leaving_claude_the_old_model_hands_over_a_summary_and_the_last_round_cut_short(self):
        old = self.leave_claude()
        calls, prompts, ctx = self.summarized(('目标：修好 X。下一步：跑测试。', '完成'), ('另一份', '完成'))
        # Once per switch: the next attempt at the first run takes the saved summary.
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual((call['model'], call['effort'], call['session'], call['redact'], call['workdir']),
                         ('claude-model', 'xhigh', old, False, str(self.work)))
        # Its own row in the relay's counts, not one of a turn.
        self.assertEqual(call['run_id'], f"handoff-r{self.topic()['binding_revision']}")
        self.assertTrue(call['prompt'].startswith('[Bridge 交接请求]'))
        self.assertIsNotNone(call['cancel'])
        summary = ctx.history_path.parent / 'handoff' / f"summary-r{self.topic()['binding_revision']}.md"
        self.assertEqual(summary.read_text(), '目标：修好 X。下一步：跑测试。\n')
        for prompt in prompts:
            self.assertIn('交接摘要（Claude Code · claude-model（xhigh） 在更换前写的', prompt)
            self.assertIn('目标：修好 X。下一步：跑测试。', prompt)
            self.assertIn(f'原文在 {summary}', prompt)
            self.assertIn('完整会话记录：', prompt)
            self.assertIn('只有最近 1 轮原文（每条最多 2000 字）', prompt)
            # The last round only, each message cut.
            self.assertIn('question 6', prompt)
            self.assertNotIn('question 5', prompt)
            self.assertIn('L' * 2000 + switch_handoff.CUT, prompt)
            self.assertNotIn('L' * 2001, prompt)
        self.assertTrue(any('switch summary topic=' in line and '完成' in line for line in self.logs))

    def test_without_a_summary_the_three_rounds_come_cut_short(self):
        self.leave_claude()
        calls, prompts, _ = self.summarized(('', '超过 180 秒'))
        self.assertEqual(len(calls), 1)
        prompt = prompts[0]
        self.assertNotIn('交接摘要', prompt)
        self.assertIn('只有最近 3 轮原文', prompt)
        self.assertIn('question 4', prompt)
        self.assertIn('L' * 2000 + switch_handoff.CUT, prompt)
        self.assertNotIn('L' * 2001, prompt)
        self.assertTrue(any('switch summary topic=' in line and '超过 180 秒' in line for line in self.logs))

    def test_a_summary_that_raises_falls_back(self):
        self.leave_claude()
        app, ctx = self.route('src')
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=RuntimeError('x'),
                               create=True):
            rows, note = app.switch.switch_handoff(ctx, [])
        self.assertNotIn('交接摘要', note)
        self.assertEqual(sum(r['role'] == 'assistant' for r in rows), 3)

    def test_no_summary_without_a_claude_session_or_a_redaction_record(self):
        # From Codex (setUp's topic): its backend has no handoff_summary.
        self.switch_to()
        app, ctx = self.route('src')
        rows, note = app.switch.switch_handoff(ctx, [])
        self.assertNotIn('交接摘要', note)
        self.assertEqual(sum(r['role'] == 'assistant' for r in rows), 3)
        self.assertTrue(any('switch summary skipped' in line and 'not supported' in line for line in self.logs))
        # From Claude, but switched before the summary existed (no redaction in the record): nothing is sent.
        self.leave_claude()
        topic = self.topic()
        del topic['switches'][-1]['from']['redaction']
        write_topic(self.source, topic)
        calls, prompts, _ = self.summarized()
        self.assertEqual(calls, [])
        self.assertIn('只有最近 3 轮原文', prompts[0])

    def test_a_redacted_old_binding_summarizes_through_the_relay(self):
        self.leave_claude()
        topic = self.topic()
        topic['switches'][-1]['from']['redaction'] = True  # Its turns went through the term relay.
        write_topic(self.source, topic)
        calls, _, _ = self.summarized(('摘要', '完成'))
        self.assertIs(calls[0]['redact'], True)

    def handed_over(self):
        """(calls of Claude's handoff_summary, the note) of the switched topic's first run, its summary answered;
        a summary saved by an earlier call is removed first."""
        for kept in (self.source.history_path.parent / 'handoff').glob('summary-*'):
            kept.unlink()
        calls = []

        def summary(ctx, session_id, prompt, *, redact, cancel=None, run_id=''):
            calls.append({'session': session_id, 'redact': redact})
            return '摘要', '完成'
        app, ctx = self.route('src')
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=summary, create=True):
            _, note = app.switch.switch_handoff(ctx, [])
        return calls, note

    def test_redaction_turned_on_in_the_switch_or_after_it_sends_the_summary_through_the_relay(self):
        # The old turns went out as they were; the fork sends them again, now that the topic has 敏感词脱敏 on.
        redacting(self, 'codex')
        self.leave_claude(redaction='on')
        self.assertIs(self.topic()['redaction'], True)
        calls, note = self.handed_over()
        self.assertIs(calls[0]['redact'], True)
        self.assertIn('摘要', note)
        # Turned on after the switch (a redaction-only switch before the first run): the same.
        topic = self.topic()
        topic.pop('redaction')
        write_topic(self.source, topic)
        self.assertIs(self.handed_over()[0][0]['redact'], False)
        write_topic(self.source, {**topic, 'redaction': True})
        self.assertIs(self.handed_over()[0][0]['redact'], True)

    def test_no_summary_when_the_topics_redaction_cannot_be_read(self):
        self.leave_claude()
        with mock.patch.object(switch_handoff, 'topic_redaction', side_effect=ValueError('unreadable')):
            calls, note = self.handed_over()
        self.assertEqual(calls, [])
        self.assertNotIn('交接摘要', note)
        self.assertTrue(any('switch summary skipped' in line and 'redaction unknown' in line for line in self.logs))

    def test_a_turn_about_to_be_refused_does_not_wait_for_a_summary(self):
        # 敏感词脱敏 on while the new Harness cannot redact: generate_reply refuses the turn, so nothing is asked first.
        self.leave_claude()
        write_topic(self.source, {**self.topic(), 'redaction': True})
        app, ctx = self.route('src')
        backend = InstructedBackend(sid=str(uuid.uuid4()))
        job = BridgeReplyJob(job_id=2, user_text='go on', chat_id='chat', message_id='om_go', timeout=0)
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', create=True) as summary, \
                mock.patch.object(app.harness, 'backend', backend):
            result = app.runs.generate_reply_for_worker(ctx, job, None, None)
        summary.assert_not_called()
        self.assertEqual(backend.prompts, [])
        self.assertIn('不支持脱敏', result.failure_detail)
        self.assertTrue(any('switch summary skipped' in line and 'turn refused' in line for line in self.logs))

    def test_a_switch_with_a_rewind_pending_hands_over_no_summary_of_the_dropped_round(self):
        # The session's tip still has the round ↩️ 回退 dropped: a fork of it would summarize that round.
        self.leave_claude(rewound=True)
        self.assertEqual(self.topic()['switches'][-1]['from']['rewind_keep'], 'entry-kept')
        calls, prompts, _ = self.summarized()
        self.assertEqual(calls, [])
        self.assertNotIn('交接摘要', prompts[0])
        self.assertIn('只有最近 3 轮原文', prompts[0])
        self.assertTrue(any('switch summary skipped' in line and 'rewind pending' in line for line in self.logs))
        # Still none on a later attempt at the first run, the marker gone by then (undone, or dropped by a turn).
        (self.source.history_path.parent / 'rewind-next.json').unlink(missing_ok=True)
        self.assertEqual(self.handed_over()[0], [])

    def test_a_rewind_of_another_session_does_not_stop_the_summary(self):
        (self.source.history_path.parent / 'rewind-next.json').write_text(json.dumps(
            {'session_id': str(uuid.uuid4()), 'keep': 'x', 'token': 'tok'}))
        self.leave_claude()
        self.assertNotIn('rewind_keep', self.topic()['switches'][-1]['from'])
        self.assertEqual(len(self.handed_over()[0]), 1)

    def test_the_task_card_says_the_summary_is_being_written_and_how_it_went(self):
        self.leave_claude()
        app, ctx = self.route('src')
        for answer, last in ((('', '超过 180 秒'), '没拿到交接摘要（超过 180 秒），改为把最近 3 轮原文交给新模型。'),
                             (('摘要文字', '完成'), '交接摘要写好了（4 字），和最近 1 轮原文一起交给新模型。')):
            notes = []
            with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', return_value=answer,
                                   create=True):
                app.switch.switch_handoff(ctx, [], note=notes.append)
            self.assertEqual(notes, [switch_handoff.WAITING, last])
        # A card line that fails does not stop the turn; a saved summary asks nothing and says nothing.
        notes = []
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', create=True) as summary:
            _, note = app.switch.switch_handoff(ctx, [], note=notes.append)
        summary.assert_not_called()
        self.assertEqual(notes, [])
        self.assertIn('摘要文字', note)

    def test_the_turn_shows_the_summary_line_in_its_trace(self):
        self.leave_claude()
        app, ctx = self.route('src')
        job = BridgeReplyJob(job_id=7, user_text='go on', chat_id='chat', message_id='om_go', timeout=0)
        job.tool_trace = object()
        seen = []
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', return_value=('摘要', '完成'),
                               create=True), \
                mock.patch.object(app.interaction, 'structured_harness_event',
                                  side_effect=lambda c, j, kind, data, control, **kw: seen.append((kind, data))), \
                mock.patch.object(app.harness, 'backend', InstructedBackend(sid=str(uuid.uuid4()))):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        lines = [data for kind, data in seen if kind == 'progress_note' and data['id'] == 'bridge-handoff-7']
        self.assertEqual(len(lines), 2)
        self.assertIn(switch_handoff.WAITING, lines[0]['text'])
        self.assertIn('交接摘要写好了', lines[1]['text'])

    def test_a_stop_during_the_summary_starts_no_new_harness(self):
        # 停止 (or a SIGTERM) while the old model writes the summary: the turn ends there, nothing goes to Codex.
        self.leave_claude()
        app, ctx = self.route('src')
        job = BridgeReplyJob(job_id=7, user_text='go on', chat_id='chat', message_id='om_go', timeout=0)
        job.tool_trace = object()
        seen = []

        def stopped(ctx, session_id, prompt, *, redact, cancel=None, run_id=''):
            job.cancel_event.set()
            return '', '已停止'
        backend = InstructedBackend(sid=str(uuid.uuid4()))
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=stopped, create=True), \
                mock.patch.object(app.interaction, 'structured_harness_event',
                                  side_effect=lambda c, j, kind, data, control, **kw: seen.append((kind, data))), \
                mock.patch.object(app.harness, 'backend', backend), self.assertRaises(JobCancelled):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        self.assertEqual(backend.prompts, [])
        lines = [data['text'] for kind, data in seen if kind == 'progress_note' and data['id'] == 'bridge-handoff-7']
        self.assertTrue(lines[-1].endswith(switch_handoff.STOPPED))
        self.assertNotIn('原文交给新模型', lines[-1])

    def test_a_safety_refusal_retry_after_the_switch_starts_from_its_own_history(self):
        # The first turn on the new binding was refused; its retry (app/policy_retry.py) has the refused round
        # taken out of its snapshot: no handoff brings it back, nor the switch notice or a summary.
        self.leave_claude()
        app, ctx = self.route('src')
        job = BridgeReplyJob(job_id=8, user_text='scan it', chat_id='chat', message_id='policy-retry-8', timeout=0,
                             history_snapshot=[{'role': 'user', 'content': 'question 5'},
                                               {'role': 'assistant', 'content': 'answer 5'}])
        job.policy_retry = {'of': 'job-7', 'category': 'cyber', 'model': 'claude-opus-4-8'}
        append_history(self.source.history_path, 'user', 'scan it', {})
        append_history(self.source.history_path, 'assistant', 'REFUSED STOP TEXT', {})
        backend = InstructedBackend(sid=str(uuid.uuid4()))
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', create=True) as summary, \
                mock.patch.object(app.harness, 'backend', backend):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        summary.assert_not_called()
        prompt = backend.prompts[-1]
        self.assertNotIn('本话题刚从', prompt)
        self.assertNotIn('REFUSED STOP TEXT', prompt)
        self.assertIn('answer 5', prompt)

    def test_a_later_fresh_session_of_the_binding_gets_no_handoff(self):
        # Once a run of the new binding has finished with a session the topic may resume, a fresh session later
        # (a pointer cleared after a failed turn, a ↩️ 回退 of a session's first turn) is like any other.
        self.leave_claude()
        revision = self.topic()['binding_revision']
        app, ctx = self.route('src')
        # A first run that failed (its session not to be resumed) leaves the handoff for the next attempt.
        upsert_job_record(self.ctx.state_dir, 'job-2', topic_id=self.source.topic_id, chat_id='chat', job_id=2,
                          status='failed', binding_revision=revision, session_id=str(uuid.uuid4()),
                          native_resume_allowed=False)
        _, note = self.handed_over()
        self.assertIn('本话题刚从', note)
        # One that finished with its session recorded (the worker, as for any turn) ends it, for good.
        first = str(uuid.uuid4())
        self.receive('m3', 'next step', root='src')
        job = self.app.runtime.jobs.queue.get_nowait()
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', return_value=('摘要', '完成'),
                               create=True), mock.patch.object(app.harness, 'backend', InstructedBackend(sid=first)):
            app.process_reply_job(job, app.harness.context)
        record = find_job_record(self.ctx.state_dir, job.message_id)
        self.assertEqual((record['binding_revision'], record['session_id'], record['native_resume_allowed']),
                         (revision, first, True))
        calls, note = self.handed_over()
        self.assertEqual((calls, note), ([], ''))
        self.assertEqual(app.switch.switch_handoff(ctx, ['snapshot']), (['snapshot'], ''))
        job_path(self.ctx.state_dir, record['key']).unlink()
        invalidate_directory(job_path(self.ctx.state_dir, 'x').parent)
        self.assertEqual(app.switch.switch_handoff(ctx, ['snapshot']), (['snapshot'], ''))
        # A fresh turn's prompt then has the job's own snapshot, without the switch notice.
        backend = InstructedBackend(sid=str(uuid.uuid4()))
        job = BridgeReplyJob(job_id=9, user_text='go on', chat_id='chat', message_id='om_go9', timeout=0,
                             history_snapshot=[{'role': 'assistant', 'content': 'own snapshot'}])
        with mock.patch.object(app.harness, 'backend', backend):
            app.runs.generate_reply_for_worker(ctx, job, None, None)
        self.assertIn('own snapshot', backend.prompts[-1])
        self.assertNotIn('本话题刚从', backend.prompts[-1])
        self.assertTrue((ctx.history_path.parent / 'handoff' / f'settled-r{revision}').exists())

    def refused_summary(self, note_list):
        """(calls of Claude's handoff_summary, the note) of the switched topic's first run, card lines to ``note_list``."""
        calls = []

        def summary(ctx, session_id, prompt, *, redact, cancel=None, run_id=''):
            calls.append(session_id)
            return '摘要', '完成'
        app, ctx = self.route('src')
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=summary, create=True):
            rows, note = app.switch.switch_handoff(ctx, [], note=note_list.append)
        self.rows = rows
        return calls, note

    def test_no_summary_of_an_old_session_the_bridge_has_blocked(self):
        # A turn stopped by a refusal through the policy guard: the session is blocked, the pointer still names it.
        old = self.leave_claude()
        block_policy_session(self.source, 'claude', old, 'ev-1')
        notes = []
        calls, note = self.refused_summary(notes)
        self.assertEqual(calls, [])
        self.assertNotIn('交接摘要（', note)
        self.assertIn('只有最近 3 轮原文', note)
        self.assertEqual(sum(r['role'] == 'assistant' for r in self.rows), 3)
        self.assertEqual(notes, [switch_handoff.REFUSED['session blocked']])
        self.assertTrue(any('switch summary skipped' in line and 'session blocked' in line for line in self.logs))
        # A guard that cannot be read counts as blocked.
        with mock.patch.object(switch_handoff, 'session_is_blocked', side_effect=ValueError('bad')):
            self.assertEqual(self.refused_summary([])[0], [])

    def test_no_summary_of_a_session_whose_last_turn_was_a_safety_refusal_retry_or_stop(self):
        old = self.leave_claude()
        upsert_job_record(self.ctx.state_dir, 'job-r', topic_id=self.source.topic_id, chat_id='chat', job_id=5,
                          status='done', session_id=old, policy_retry={'of': 'job-4', 'category': 'cyber',
                                                                       'model': 'claude-opus-4-8'})
        notes = []
        calls, _ = self.refused_summary(notes)
        self.assertEqual(calls, [])
        # The retry itself was not stopped: the line says it was the retry.
        self.assertEqual(notes, [switch_handoff.REFUSED['policy retry']])
        self.assertIn('换旧模型的重试', notes[0])
        self.assertNotIn('停过', notes[0])
        self.assertTrue(any('switch summary skipped' in line and 'policy retry' in line for line in self.logs))
        # Stopped by a refusal (its block not written): the same, said as a stop.
        upsert_job_record(self.ctx.state_dir, 'job-s', topic_id=self.source.topic_id, chat_id='chat', job_id=6,
                          status='cancelled', session_id=old, policy_stop={'kind': 'refusal', 'category': 'cyber'})
        notes = []
        self.assertEqual(self.refused_summary(notes)[0], [])
        self.assertEqual(notes, [switch_handoff.REFUSED['policy refusal']])
        self.assertIn('安全拒绝停过', notes[0])
        # Stopped by an official model fallback: no 安全拒绝 in the line.
        upsert_job_record(self.ctx.state_dir, 'job-f', topic_id=self.source.topic_id, chat_id='chat', job_id=7,
                          status='cancelled', session_id=old, policy_stop={'kind': 'fallback', 'category': 'cyber'})
        notes = []
        self.assertEqual(self.refused_summary(notes)[0], [])
        self.assertEqual(notes, [switch_handoff.REFUSED['policy fallback']])
        self.assertIn('官方模型降级', notes[0])
        self.assertNotIn('安全拒绝', notes[0])
        # A later ordinary turn in that session is what the summary forks.
        upsert_job_record(self.ctx.state_dir, 'job-n', topic_id=self.source.topic_id, chat_id='chat', job_id=8,
                          status='done', session_id=old)
        self.assertEqual(self.refused_summary([])[0], [old])

    def test_a_turn_about_to_be_refused_says_nothing_of_rounds_for_a_blocked_old_session(self):
        # Blocked old session and 敏感词脱敏 on while the new Harness cannot redact: the turn is refused, so no
        # card line may say the old rounds go to the new model.
        old = self.leave_claude()
        block_policy_session(self.source, 'claude', old, 'ev-1')
        write_topic(self.source, {**self.topic(), 'redaction': True})
        app, ctx = self.route('src')
        backend = InstructedBackend(sid=str(uuid.uuid4()))
        job = BridgeReplyJob(job_id=2, user_text='go on', chat_id='chat', message_id='om_go', timeout=0)
        notes = []
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', create=True) as summary, \
                mock.patch.object(app.harness, 'backend', backend), \
                mock.patch.object(app.runs, '_bridge_note', side_effect=lambda ctx, job, key, text: notes.append(text)):
            result = app.runs.generate_reply_for_worker(ctx, job, None, None)
        summary.assert_not_called()
        self.assertEqual(backend.prompts, [])
        self.assertIn('不支持脱敏', result.failure_detail)
        self.assertEqual(notes, [])
        self.assertTrue(any('switch summary skipped' in line and 'turn refused' in line for line in self.logs))

    def test_a_summary_file_that_is_not_utf8_is_made_again(self):
        self.leave_claude()
        kept = self.source.history_path.parent / 'handoff' / f"summary-r{self.topic()['binding_revision']}.md"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b'\xff\xfe\xfa')
        calls, note = self.handed_over_keeping()
        self.assertEqual(len(calls), 1)
        self.assertIn('摘要', note)

    def handed_over_keeping(self):
        """``handed_over`` without removing a saved summary first."""
        calls = []

        def summary(ctx, session_id, prompt, *, redact, cancel=None, run_id=''):
            calls.append(session_id)
            return '摘要', '完成'
        app, ctx = self.route('src')
        with mock.patch.object(self.app.apps['claude'].backend, 'handoff_summary', side_effect=summary, create=True):
            _, note = app.switch.switch_handoff(ctx, [])
        return calls, note

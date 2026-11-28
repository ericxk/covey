"""Starting a task from the launch card, and a topic's own Harness and model settings.

- The launch card: one compact card whose selects record model, budget, workspace and run
  mode; 开始任务 starts exactly one task, in the original directory or a new worktree.
- Model changes replace the whole card; stale, replaced, undelivered and retired forms
  cannot start anything; old finished forms keep their answers.
- Topic-only settings: each topic keeps its own Harness, model and budget.

The launch card's drawing, clicks and dead forms are test_launch_card; what it preselects and offers
(model order, MASO, redaction, budgets) is test_launch_models. Changing the Harness or model mid-topic (换配置)
is test_harness_switch.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import subprocess
import threading
import time
from unittest import mock

from support.fake_lark import FakeCardKit
from support.cards import (card_selects, current_token, page_options, selects, settings_rows)
from support.git import commit_all, init_git_repo, run_git
from support.processes import leftover_check
from support.sessions import write_session_id
from support.world import LaunchCase

from support import requires_extension
from covey.app.card_lanes import CardLanes
from covey.app.job_dispatch import BACKEND_UNAVAILABLE
from covey.app.harnesses import STARTUP_RETRY_SECONDS
from covey.app.form_cards import save_record
from covey.app.launch import build_launch_card, card_token, chat_defaults_path
from covey.app.launch_submit import RESTART_ABANDONED, RUN_MODE_INVALID, submit_launch_form
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.app.launch_models import available_models
from covey.app.recovery_cards import recovery_card
from covey.core.menu_choice import effort_options
from covey.cards.streaming import build_stream_card
from covey.core.backup import create_backup, verify_backup
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.state import write_private_text
from covey.core.topics import read_topic, remember_topic_message
from covey.core.workspaces import config_path, load_config, resolve_selection

setUpModule, tearDownModule = leftover_check()


class LaunchTests(LaunchCase):
    def test_choices_are_local_and_submit_has_all_fields(self):
        for index in range(9):
            directory = self.root / str(index)
            directory.mkdir()
            resolve_selection(self.ctx.state_dir, self.work, str(directory))
        card, event = self.form('form')
        selections = card_selects(card)
        # Harness and provider above the model (2026-11-03).
        # No 工作区 or 运行方式: a topic starts in the home (no device on, so no 执行设备 row either).
        self.assertEqual([e['name'] for e in selections], ['harness', 'provider', 'model', 'effort'])
        # Each row's options open under it (no select since 2026-11-04); a Harness each.
        self.assertEqual(len(page_options(card, 'harness')), len(self.app.apps))
        self.assertNotIn('工作区', json.dumps(card, ensure_ascii=False))
        self.assertNotIn('运行方式', json.dumps(card, ensure_ascii=False))
        # The form holds only the buttons (no path input since 2026-11-03).
        form = next(e for e in card['body']['elements'] if e['tag'] == 'form')
        self.assertEqual([e['tag'] for e in form['elements']], ['column_set'])
        buttons = [c['elements'][0] for c in form['elements'][0]['columns']]
        self.assertEqual([b['form_action_type'] for b in buttons], ['submit'])  # 开始任务 alone: no 其他目录….
        self.assertTrue(event['action_name'].startswith(SUBMIT_PREFIX))

    def test_single_submit_routes_four_harnesses_without_replacement_selection(self):
        for name in self.app.apps:
            self.receive(name, 'first')
            self.receive(name + '-second', 'second', root=name)
            _, event = self.form(name)
            directory = self.root / ('project ' + name)
            directory.mkdir()
            event['form_value'].update(model=name + ':' + name + '-model', path=str(directory))
            before = {k: a.submission.selection_card.call_count for k, a in self.app.apps.items()}
            self.submit(event)
            jobs = [self.app.runtime.jobs.queue.get_nowait() for _ in range(2)]
            self.assertEqual([j.user_text for j in jobs], ['first', 'second'])
            self.assertTrue(all(j.harness_id == name and j.workdir == directory for j in jobs))
            self.assertEqual({k: a.submission.selection_card.call_count for k, a in self.app.apps.items()}, before)
            self.assertEqual(read_topic(self.route(name)[1])['harness_id'], name)
            self.assertEqual(find_job_record(self.ctx.state_dir, name)['status'], 'queued')

    def test_duplicate_submits_and_restarted_app_do_not_reenqueue(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        event = self.choose_model(event)
        self.submit(event)
        self.submit({**event, 'event_id': 'another-delivery'})
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        restarted, ctx = self.bridge()
        restarted.handle_card_action_event(event, ctx)
        self.assertTrue(restarted.runtime.jobs.queue.empty())

    def test_concurrent_double_click_enqueues_once(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        event = self.choose_model(event)  # Both clicks come from the card shown after it.
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(self.submit, [event, {**event, 'event_id': 'double'}]))
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)

    def test_other_topic_or_owner_cannot_submit_form(self):
        self.receive('one', 'first')
        self.receive('two', 'second')
        _, event = self.form('one')
        _, other = self.form('two')
        self.submit({**event, 'operator_id': 'foreign'})
        self.submit({**event, 'message_id': other['message_id']})
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(read_topic(self.route('one')[1])['harness_id'], 'codex')

    def test_invalid_path_or_form_does_not_change_binding(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        app, ctx = self.route('task')
        before = read_topic(ctx)
        invalid = [{'model': 'maso:maso-model', 'effort': 'high', 'path': str(self.root / 'missing')},
                   {'model': 'maso:maso-model', 'effort': 'high', 'path': []}, '{bad json']
        # An empty form is valid on a compact card: its selects were recorded and the path is optional.
        for fields in invalid:
            with self.assertRaises(ValueError):
                submit_launch_form(app, ctx, {**event, 'form_value': fields})
            self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_unknown_run_mode_is_refused(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        app, ctx = self.route('task')
        before = read_topic(ctx)
        path = ctx.state_dir / 'launch-forms' / (event['action_name'][len(SUBMIT_PREFIX):] + '.json')
        self.app.handle_card_action_event({**event, 'action_name': '', 'event_id': 'mode',
            'action_value': {'kind': 'bridge_launch_mode', 'token': path.stem}, 'option': 'copy'}, self.ctx)
        self.assertEqual(json.loads(path.read_text())['form_defaults'].get('run_mode', 'direct'), 'direct')
        for index, mode in enumerate(('copy', ['direct'])):
            # Only a forged form could carry it; it is refused, never defaulted.
            self.app.handle_card_action_event({**event, 'event_id': f'click-{index}',
                                               'form_value': {'path': '', 'run_mode': mode}}, self.ctx)
            self.assertIn(RUN_MODE_INVALID, '\n'.join(self.said()))
        self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_card_without_run_mode_runs_in_the_selected_directory(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        fields = {k: v for k, v in event['form_value'].items() if k != 'run_mode'}
        self.submit({**event, 'form_value': fields})
        job = self.app.runtime.jobs.queue.get_nowait()
        record = next(r for r in (json.loads(f.read_text()) for f in (self.ctx.state_dir / 'launch-forms').glob('*.json'))
                      if r.get('status') == 'done')
        self.assertEqual(record['selection']['run_mode'], 'direct')
        self.assertEqual(record['selection']['branch'], '')
        self.assertNotIn('worktrees', str(job.workdir))

    def test_a_card_of_the_home_start_starts_in_the_services_workdir_with_no_workspace_picked(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        self.assertEqual(set(event['form_value']), {'model', 'effort', 'path'})  # Nothing of a workspace or run mode.
        self.submit(event)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.workdir, self.work.resolve())
        record = next(r for r in (json.loads(f.read_text()) for f in (self.ctx.state_dir / 'launch-forms').glob('*.json'))
                      if r.get('status') == 'done')
        self.assertEqual((record['selection']['run_mode'], record['selection']['branch']), ('direct', ''))
        self.assertEqual(record['selection']['workdir'], str(self.work.resolve()))
        topic = read_topic(self.route('task')[1])
        self.assertEqual(topic['workdir'], str(self.work.resolve()))
        # A home start (T870, core/topics.py mark_home): marked, with its start and slug, as the butler's topics are.
        self.assertEqual((topic['home_start'], type(topic['started_at']), topic['slug']), (True, float, 'hello'))  # Its first line.

    def test_a_home_start_neither_looks_up_nor_rewrites_the_workspace_list(self):
        # T930 F0 R1, R2: the home is named after its directory, not looked up in workspace-config.json (here it lists
        # only another directory) and no ``recent`` is written: the file is left as it was, its mtime too.
        other = self.root / 'other'
        other.mkdir()
        path = config_path(self.ctx.state_dir)
        path.write_text(json.dumps({'default': 'other', 'workspaces': {'other': str(other)}}))
        os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        before = (path.stat().st_mtime_ns, path.read_bytes())
        self.receive('task', 'hello')
        self.submit(self.form('task')[1])
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().workdir, self.work.resolve())
        topic = read_topic(self.route('task')[1])
        self.assertEqual((topic['workspace_name'], topic['workdir'], topic['home_start']),
                         (self.work.resolve().name, str(self.work.resolve()), True))
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)

    def test_a_card_sent_before_the_home_start_still_starts_where_its_record_says(self):
        # Rule 10: a launch card already sent (a message waiting_workspace for it) names a workspace in its record.
        other = self.root / 'other'
        other.mkdir()
        name, _ = resolve_selection(self.ctx.state_dir, self.work, str(other))
        self.receive('task', 'hello')
        card, event = self.form('task')
        token = card_token(card)
        path = self.ctx.state_dir / 'launch-forms' / (token + '.json')
        record = json.loads(path.read_text())
        record['form_defaults'] = {'workspace': name, 'path': '', 'run_mode': 'direct'}
        path.write_text(json.dumps(record))
        self.assertEqual(find_job_record(self.ctx.state_dir, 'task')['status'], 'waiting_workspace')
        self.app.handle_card_action_event({**event, 'form_value': {'path': ''}}, self.ctx)  # Nothing posted: the record's.
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.workdir, other.resolve())
        self.assertNotIn('home_start', read_topic(self.route('task')[1]))  # A workspace's topic, as before.
        self.assertEqual(self.launch_form(token)['selection']['workspace'], name)

    def test_a_card_that_names_a_workspace_still_asks_for_a_worktree(self):
        repo = self.make_repo()
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value'].update(run_mode='worktree', path=str(repo / 'pkg'))
        background = []
        with self.worktree_patches(background):
            self.app.handle_card_action_event(self.choose_model(event), self.ctx)
        self.assertEqual(len(background), 1)  # The checkout is made off the click, as before.
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_a_typed_path_is_the_owners_alone(self):
        from covey.app.launch_submit import OWNER_ONLY_PATH
        self.receive('task', 'hello')
        _, event = self.form('task')
        app, ctx = self.route('task')
        app.runtime.access.is_owner = lambda operator: operator == 'owner'
        typed = self.root / 'typed'
        typed.mkdir()
        before = read_topic(ctx)
        with self.assertRaisesRegex(ValueError, OWNER_ONLY_PATH):
            submit_launch_form(app, ctx, {**event, 'operator_id': 'member', 'form_value': {'path': str(typed)}})
        self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def make_repo(self):
        repo = self.root / 'repo'
        (repo / 'pkg').mkdir(parents=True)
        (repo / 'pkg' / 'a.txt').write_text('a')
        init_git_repo(repo)
        commit_all(repo)
        self.git = lambda *args, cwd=repo: run_git(cwd, *args)
        (repo / 'pkg' / 'dirty.txt').write_text('not committed')
        return repo

    def worktree_patches(self, background=None):
        """Worktrees under the test root; the background step runs inline unless captured."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch('covey.core.workspaces.worktree_root', return_value=self.root / 'worktrees'))
        stack.enter_context(mock.patch('covey.app.launch_submit.run_in_background',
                                       side_effect=(lambda *a: background.append(a)) if background is not None
                                       else (lambda target, *a: target(*a))))
        self.cardkit = stack.enter_context(FakeCardKit())
        return stack

    def test_worktree_mode_runs_the_topic_on_a_new_branch_checkout(self):
        repo = self.make_repo()
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value'].update(run_mode='worktree', path=str(repo / 'pkg'))
        with self.worktree_patches():
            self.submit(event)
        job = self.app.runtime.jobs.queue.get_nowait()
        checkout = job.workdir.parent
        self.assertEqual(checkout.parent, self.root / 'worktrees')
        self.assertEqual(job.workdir.name, 'pkg')
        self.assertTrue((job.workdir / 'a.txt').exists())
        self.assertFalse((job.workdir / 'dirty.txt').exists())
        self.assertTrue(self.git('branch', '--show-current', cwd=checkout).strip().startswith('bridge/'))
        self.assertEqual(self.git('branch', '--show-current').strip() in ('master', 'main'), True)
        record = next(r for r in (json.loads(f.read_text()) for f in (self.ctx.state_dir / 'launch-forms').glob('*.json'))
                      if r.get('status') == 'done')
        self.assertEqual(record['selection']['run_mode'], 'worktree')
        self.assertTrue(record['receipt_published'])
        preference = json.loads((self.ctx.state_dir / 'launch-defaults.json').read_text())
        self.assertEqual(preference['workdir'], str((repo / 'pkg').resolve()))

    def test_worktree_click_returns_before_the_checkout_and_starts_once(self):
        repo = self.make_repo()
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value'].update(run_mode='worktree', path=str(repo / 'pkg'))
        app, ctx = self.route('task')
        background = []
        cards_before = sum(a.submission.selection_card.call_count for a in self.app.apps.values())
        with self.worktree_patches(background):
            click = self.choose_model(event)
            self.app.handle_card_action_event(click, self.ctx)
            # Clicks while the checkout runs neither create nor report anything.
            self.app.handle_card_action_event({**click, 'event_id': 'again'}, self.ctx)
            self.app.handle_card_action_event({**click, 'event_id': 'third'}, self.ctx)
            # A message sent meanwhile waits for this launch instead of opening another card.
            self.receive('more', 'and this', root='task')
            # Another launch card of the topic cannot start a second worktree.
            _, other = self.form('task')
            self.app.handle_card_action_event({**other, 'event_id': 'other', 'form_value': {'path': ''}}, self.ctx)
        token = click['action_name'][len(SUBMIT_PREFIX):]
        self.assertEqual(self.launch_form(token)['status'], 'creating_worktree')
        self.assertEqual(len(background), 1)
        self.assertFalse((self.root / 'worktrees').exists())
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(sum(a.submission.selection_card.call_count for a in self.app.apps.values()), cards_before)
        self.assertIn('独立 worktree 正在创建', '\n'.join(self.said()))
        self.assertIn('本话题正在创建独立 worktree', '\n'.join(self.said()))
        self.assertNotIn('again', '\n'.join(self.said()))
        # The whole card becomes a note with nothing to click.
        self.assertEqual(self.cardkit.last.path, '/open-apis/cardkit/v1/cards/card-id-task')
        note = json.dumps(self.cardkit.last.card, ensure_ascii=False)
        self.assertIn('正在创建独立 worktree', note)
        self.assertNotIn('button', note)
        self.assertEqual(read_topic(ctx)['launching'], token)
        with self.worktree_patches():
            target, *args = background[0]
            target(*args)
        jobs = [self.app.runtime.jobs.queue.get_nowait() for _ in range(self.app.runtime.jobs.queue.qsize())]
        self.assertEqual([j.message_id for j in jobs], ['task', 'more'])
        self.assertEqual({j.workdir for j in jobs}, {jobs[0].workdir})
        self.assertEqual(jobs[0].workdir.parent.parent, self.root / 'worktrees')
        record = self.launch_form(token)
        self.assertEqual((record['status'], record['receipt_published']), ('done', True))
        self.assertNotIn('launching', read_topic(ctx))
        self.assertEqual(len(self.git('branch', '--list', 'bridge/*').splitlines()), 1)

    @requires_extension()
    def test_a_failed_checkout_gives_the_card_back_with_its_choices(self):
        repo = self.make_repo()
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value'].update(run_mode='worktree', path=str(repo / 'pkg'), effort='low')
        app, ctx = self.route('task')
        with self.worktree_patches(), mock.patch('covey.app.launch_submit.add_topic_worktree',
                                                 side_effect=ValueError('创建 worktree 失败：disk full')):
            click = self.choose_model(event)
            self.app.handle_card_action_event(click, self.ctx)
        token = click['action_name'][len(SUBMIT_PREFIX):]
        self.assertEqual(self.launch_form(token)['status'], 'worktree_failed')
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertNotIn('launching', read_topic(ctx))
        self.assertIn('独立 worktree 未就绪，任务尚未启动：创建 worktree 失败：disk full', '\n'.join(self.said()))
        self.assertIn('配置卡已恢复', '\n'.join(self.said()))
        card = self.cardkit.last.card
        self.assertEqual(card_token(card), current_token(self.ctx.state_dir, card_token(card)))
        self.assertIn('任务未启动：创建 worktree 失败', json.dumps(card, ensure_ascii=False))
        options = {e['name']: e['initial_option'] for e in card_selects(card)}
        name, _ = resolve_selection(self.ctx.state_dir, self.work, str(repo / 'pkg'))
        self.assertEqual((options['model'], options['workspace'], options['run_mode']), ('maso:maso-model', name, 'worktree'))
        self.assertTrue(options['effort'].endswith(':low'))
        # The old form is closed; the restored one starts in the original directory.
        self.app.handle_card_action_event({**click, 'event_id': 'old'}, self.ctx)
        self.assertIn('最新选择表单', '\n'.join(self.said()))
        new = card_token(card)
        self.app.handle_card_action_event({**click, 'action_name': '', 'event_id': 'mode-direct',
            'action_value': {'kind': 'bridge_launch_mode', 'token': new}, 'option': 'direct'}, self.ctx)
        self.app.handle_card_action_event({**click, 'event_id': 'retry', 'action_name': SUBMIT_PREFIX + new}, self.ctx)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.workdir, job.effort), ((repo / 'pkg').resolve(), 'low'))

    def test_a_restart_reads_the_launch_forms_together_and_leaves_those_not_creating_a_worktree(self):
        # The forms are read many at a time at a start (700 one by one took 1.6 s on NAS); one it cannot read is logged.
        from covey.app import launch_submit
        forms = self.ctx.state_dir / 'launch-forms'
        forms.mkdir(exist_ok=True)
        for number in range(30):
            (forms / f'ready{number:02}.json').write_text(json.dumps({'status': 'ready', 'topic_id': 'gone'}))
        (forms / 'torn.json').write_text('{')
        with mock.patch.object(launch_submit, 'abandon_worktree_launch') as abandon, \
                mock.patch.object(self.app.topics.runtime, 'log') as log:
            launch_submit.recover_worktree_launches(self.app.topics, self.ctx)
        abandon.assert_not_called()
        self.assertEqual([c.args[0] for c in log.call_args_list], ['launch worktree recovery failed: JSONDecodeError'])

    def test_a_restart_during_the_checkout_gives_the_card_back(self):
        repo = self.make_repo()
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value'].update(run_mode='worktree', path=str(repo / 'pkg'))
        app, ctx = self.route('task')
        background = []
        with self.worktree_patches(background):
            click = self.choose_model(event)
            self.app.handle_card_action_event(click, self.ctx)
            self.app.notify_stale_jobs(self.ctx)  # What a restarted bridge runs at startup.
        token = click['action_name'][len(SUBMIT_PREFIX):]
        self.assertEqual(self.launch_form(token)['status'], 'worktree_failed')
        self.assertIn(RESTART_ABANDONED, '\n'.join(self.said()))
        self.assertNotIn('launching', read_topic(ctx))
        card = self.cardkit.last.card
        self.assertEqual(self.launch_form(card_token(card))['status'], 'ready')
        # A checkout that outlived its form never starts the topic.
        with self.worktree_patches():
            target, *args = background[0]
            target(*args)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(self.launch_form(token)['status'], 'worktree_failed')

    def test_worktree_mode_needs_a_git_repository(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        app, ctx = self.route('task')
        before = read_topic(ctx)
        plain = self.root / 'plain'
        plain.mkdir()
        self.submit({**event, 'form_value': {**event['form_value'], 'run_mode': 'worktree', 'path': str(plain)}})
        self.assertIn('不在 git 仓库中', '\n'.join(self.said()))
        self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_stale_form_cannot_rebind_changed_or_started_topic(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        app, ctx = self.route('task')
        self.app.topics.select_harness(ctx, 'chat', 'dsh')
        self.submit(event)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        _, new_event = self.form('task')
        app, ctx = self.route('task')
        app.submission.start_workspace(ctx, 'chat', ctx.workspace_name)
        self.submit(new_event)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(read_topic(ctx)['harness_id'], 'dsh')

    def test_standalone_new_topic_is_rejected_and_session_unchanged(self):
        app, ctx = self.route('old')
        write_session_id(ctx.session_path, 'original-session')
        with self.assertRaises(ValueError):
            build_launch_card(app.topics, ctx, new_topic=True)
        self.assertEqual(ctx.session_path.read_text().strip(), 'original-session')
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    @requires_extension()
    def test_last_combination_prefills_new_form_without_rebinding(self):
        self.receive('one', 'hello')
        _, event = self.form('one')
        directory = self.root / 'other project'
        directory.mkdir()
        event['form_value']['path'] = str(directory)
        self.submit(event)
        config = load_config(self.ctx.state_dir, self.work)
        old_name = next(name for name, path in config['workspaces'].items() if path == str(directory))
        config['workspaces']['renamed'] = config['workspaces'].pop(old_name)
        write_private_text(config_path(self.ctx.state_dir), json.dumps(config))
        _, ctx = self.route('fresh')
        before = read_topic(ctx)
        card, _ = self.form('fresh')
        selects = [e for e in card_selects(card) if e['name'] not in ('run_mode', 'harness', 'provider', 'model', 'work_mode')]
        # The last launch's model with its budget (S28: no longer always 推荐); no 工作区 row on a new card (主目录启动).
        self.assertEqual([e['initial_option'] for e in selects], ['1:high'])
        self.assertEqual(read_topic(ctx), before)

    def test_whitespace_path_uses_saved_workspace(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        event['form_value']['path'] = '  '
        event['form_value']['effort'] = '1:high'
        event['form_value'] = json.dumps(event['form_value'])
        self.submit(event)
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().workdir, self.work)

    def test_empty_topic_cannot_start_without_user_message(self):
        _, event = self.form('empty')
        self.submit(event)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertIn('没有待启动', self.app.messenger.send_message.call_args.args[2])
        self.assertFalse(read_topic(self.route('empty')[1]).get('workspace_locked'))

    def test_preference_write_failure_does_not_report_or_replay_failed_task(self):
        self.receive('task', 'hello')
        _, event = self.form('task')
        def save(path, value):
            if path.name == 'launch-defaults.json' or path.parent.name == 'launch-defaults':
                raise OSError('disk unavailable')
            return save_record(path, value)
        with mock.patch('covey.app.launch_submit.save_record', side_effect=save):
            self.submit(event)
            self.submit(event)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.notices(), [])

    def test_explicit_selection_replaces_waiting_snapshot_for_same_harness(self):
        self.receive('task', 'hello')
        before = find_job_record(self.ctx.state_dir, 'task')
        write_private_text(self.ctx.state_dir / 'model-settings/codex.json', json.dumps({'model': 'new', 'effort': 'high'}))
        _, event = self.form('task')
        event['form_value'].update(model='codex:codex-model', effort='low')
        self.submit(event)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.model, job.effort), ('codex-model', 'low'))
        self.assertNotEqual(job.model, before['model'])


class InChat:
    """Messages from ``self.chat`` ('chat' until a test switches it): each chat keeps its own launch defaults."""
    chat = 'chat'

    def event(self, mid, content='task', root=''):
        return {**super().event(mid, content, root), 'chat_id': self.chat}


class ChatLaunchCase(InChat, LaunchCase):
    """Launches in several chats, and what a new topic's card in a chat then shows."""

    def form(self, mid, **kwargs):
        card, event = super().form(mid, **kwargs)
        _, ctx = self.route(mid)
        remember_topic_message(ctx.state_dir, ctx.topic_id, self.chat, 'card-' + mid)
        return card, {**event, 'chat_id': self.chat}

    def launch(self, chat, mid, model, effort, directory):
        self.chat = chat
        self.receive(mid, 'hello')
        _, event = self.form(mid)
        directory.mkdir(exist_ok=True)
        event['form_value'].update(model=model, effort=effort, path=str(directory))
        self.submit(event)
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().workdir, directory.resolve())

    def preselected(self, chat, mid):
        """(model, budget) a new topic's card in ``chat`` preselects."""
        self.chat = chat
        chosen = selects(self.form(mid)[0])
        self.assertNotIn('workspace', chosen)
        return chosen['model']['initial_option'], chosen['effort']['initial_option']


class ChatLaunchDefaultsTests(ChatLaunchCase):
    """S29: a launch card preselects its chat's last launch; a chat without one starts from the most
    recent launch anywhere, launch-defaults.json, which older releases read and write as before."""

    @requires_extension()
    def test_a_launch_in_one_chat_leaves_another_chats_preselection(self):
        direct, group = (self.root / name for name in ('direct', 'group'))
        self.launch('chat', 'direct-1', 'maso:maso-model', 'high', direct)
        self.launch('oc_group', 'group-1', 'codex:codex-model', 'low', group)
        self.assertEqual(self.preselected('chat', 'direct-2'), ('maso:maso-model', '1:high'))
        self.assertEqual(self.preselected('oc_group', 'group-2'), ('codex:codex-model', '1:low'))
        # Each record names its chat; launch-defaults.json keeps the most recent launch anywhere.
        self.assertEqual(json.loads(chat_defaults_path(self.ctx.state_dir, 'oc_group').read_text())['chat_id'], 'oc_group')
        self.assertEqual(json.loads((self.ctx.state_dir / 'launch-defaults.json').read_text()),
                         {'model_choice': 'codex:codex-model', 'effort': 'low', 'workdir': str(group.resolve())})

    def test_a_new_chat_starts_from_the_most_recent_launch_anywhere(self):
        direct, group, new = (self.root / name for name in ('direct', 'group', 'new'))
        self.launch('chat', 'direct-1', 'maso:maso-model', 'high', direct)
        self.launch('oc_group', 'group-1', 'codex:codex-model', 'low', group)
        self.assertEqual(self.preselected('oc_new', 'new-1'), ('codex:codex-model', '1:low'))
        # It follows the latest launch until it has launched itself; chats that launched keep their own.
        self.launch('chat', 'direct-2', 'claude:claude-model', 'max', direct)
        self.assertEqual(self.preselected('oc_new', 'new-2')[0], 'claude:claude-model')
        self.assertEqual(self.preselected('oc_group', 'group-2')[0], 'codex:codex-model')
        self.launch('oc_new', 'new-3', 'dsh:dsh-model', 'xhigh', new)
        self.launch('chat', 'direct-3', 'maso:maso-model', 'high', direct)
        self.assertEqual(self.preselected('oc_new', 'new-4'), ('dsh:dsh-model', '1:xhigh'))

    @requires_extension()
    def test_launch_defaults_json_from_an_older_release_still_preselects(self):
        older = self.root / 'older'
        older.mkdir()
        resolve_selection(self.ctx.state_dir, self.work, str(older))
        save_record(self.ctx.state_dir / 'launch-defaults.json',
              {'model_choice': 'codex:codex-model', 'effort': 'low', 'workdir': str(older.resolve())})
        self.assertEqual(self.preselected('chat', 'first'), ('codex:codex-model', '1:low'))
        self.assertFalse((self.ctx.state_dir / 'launch-defaults').exists())
        # A chat record that cannot be read, or that names another chat, counts as none.
        path = chat_defaults_path(self.ctx.state_dir, 'chat')
        for n, broken in enumerate(('{', json.dumps({'model_choice': 'maso:maso-model', 'effort': 'high', 'chat_id': 'x'}))):
            write_private_text(path, broken)
            self.assertEqual(self.preselected('chat', f'broken-{n}')[0], 'codex:codex-model')
        path.unlink()
        # A launch rewrites the file with the keys older releases read, beside the chat's own record.
        direct = self.root / 'direct'
        self.launch('chat', 'task', 'maso:maso-model', 'high', direct)
        self.assertEqual(json.loads((self.ctx.state_dir / 'launch-defaults.json').read_text()),
                         {'model_choice': 'maso:maso-model', 'effort': 'high', 'workdir': str(direct.resolve())})
        self.assertEqual(json.loads(path.read_text())['chat_id'], 'chat')

    def test_a_backup_keeps_each_chats_defaults(self):
        self.launch('oc_group', 'group-1', 'codex:codex-model', 'low', self.root / 'group')
        manifest = verify_backup(create_backup(self.ctx.state_dir))
        self.assertIn('launch-defaults.json', manifest)
        self.assertIn('launch-defaults/' + chat_defaults_path(self.ctx.state_dir, 'oc_group').name, manifest)
        self.assertIn('model-usage.json', manifest)  # The menus' order too (app/model_usage.py).


class RecentUseTests(ChatLaunchCase):
    """The owner, 2026-11-02: a chat's menu lists its last launch's model first (preselected), then the ones
    it chose most, each with the budget it usually gives it; a launch and a 换配置 are each one choice
    (app/model_usage.py)."""

    def menu(self, chat, mid):
        """The model keys a new topic's card in ``chat`` lists, its selects, and the click that starts it."""
        self.chat = chat
        card, event = self.form(mid)
        chosen = selects(card)
        # The menu's order is the record's: the model select lists the chosen Harness's models only.
        return list(self.launch_form(event['action_name'][len(SUBMIT_PREFIX):])['models']), chosen, event

    @requires_extension()
    def test_the_last_used_model_comes_first_with_its_usual_budget(self):
        fixed = list(available_models(self.app.topics, self.ctx))
        for n, (model, effort) in enumerate((('codex:codex-model', 'low'), ('claude:claude-model', 'max'),
                                             ('codex:codex-model', 'low'), ('codex:codex-model', 'high'),
                                             ('dsh:dsh-model', 'xhigh'))):
            self.launch('chat', f'm{n}', model, effort, self.root / 'work-a')
        order, chosen, event = self.menu('chat', 'next')
        self.assertEqual(order[:3], ['dsh:dsh-model', 'codex:codex-model', 'claude:claude-model'])
        self.assertEqual(order[3:], [key for key in fixed if key not in order[:3]])  # Not used lately: as before.
        self.assertEqual((chosen['model']['initial_option'], chosen['effort']['initial_option']), ('dsh:dsh-model', '1:xhigh'))
        # Chosen on the card, Codex comes with low (2 of its 3 launches), not 推荐; a model never used with the level 推荐
        # names: the card lists no 推荐 (docs/ui.md 13 U5).
        moved = self.choose_model({**event, 'form_value': {'model': 'codex:codex-model'}})
        record = self.launch_form(moved['action_name'][len(SUBMIT_PREFIX):])
        self.assertEqual((record['selected_model'], record['selected_effort']), ('codex:codex-model', 'low'))
        self.assertEqual(list(record['models']), order)  # The new card lists them as the first did.
        moved = self.choose_model({**moved, 'form_value': {'model': 'maso:maso-model'}})
        record = self.launch_form(moved['action_name'][len(SUBMIT_PREFIX):])
        self.assertEqual(effort_options(record['models']['maso:maso-model'])[1], 'top_minus_one')
        self.assertEqual(record['selected_effort'], 'xhigh')

    def test_each_chat_lists_its_own_and_a_new_chat_everyones(self):
        self.launch('chat', 'direct-1', 'claude:claude-model', 'max', self.root / 'direct')
        self.launch('oc_accept', 'accept-1', 'dsh:dsh-model', 'xhigh', self.root / 'accept')
        self.launch('oc_accept', 'accept-2', 'codex:codex-model', 'low', self.root / 'accept')
        self.assertEqual(self.menu('chat', 'direct-2')[0][0], 'claude:claude-model')  # Another chat's tests don't count.
        self.assertEqual(self.menu('oc_accept', 'accept-3')[0][:2], ['codex:codex-model', 'dsh:dsh-model'])
        self.assertEqual(self.menu('oc_new', 'new-1')[0][:3], ['codex:codex-model', 'dsh:dsh-model', 'claude:claude-model'])


def actions(value):
    if isinstance(value, dict):
        if 'kind' in value:
            yield value['kind']
        for child in value.values():
            yield from actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from actions(child)


class TopicOnlyTests(LaunchCase):

    def start(self, root, model='codex:codex-model', effort='low'):
        self.receive(root, 'original ' + root)
        _, event = self.form(root)
        event['form_value'].update(model=model, effort=effort)
        self.submit(event)
        return self.app.runtime.jobs.queue.get_nowait()

    def test_two_topics_freeze_independent_settings_across_restart_and_global_edit(self):
        one, two = self.start('one'), self.start('two', effort='high')
        for job in (one, two):
            self.app.apps[job.harness_id].runtime.unregister_reply_job(job)
            upsert_job_record(self.ctx.state_dir, job.message_id, status='done')
        write_private_text(self.ctx.state_dir / 'model-settings/codex.json', json.dumps({'model': 'unrelated', 'effort': 'max'}))
        self.receive('one-followup', 'continue', root='one')
        followup = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((followup.model, followup.effort, followup.session_path),
                         (one.model, 'low', one.session_path))
        restarted, base = self.bridge()
        self.assertEqual(restarted.topics.route(base, self.event('two'))[1].effort, 'high')
        self.assertEqual(restarted.topics.route(base, self.event('one'))[1].effort, 'low')

    def test_a_topic_without_a_snapshot_runs_on_current_settings_and_keeps_its_session(self):
        for name in self.app.apps:
            self.receive(name, 'legacy task')
            app, ctx = self.route(name)
            self.app.topics.select_harness(ctx, 'chat', name)
            app, ctx = self.route(name)
            upsert_job_record(ctx.state_dir, name, model='original-' + name, effort='high', status='done')
            write_session_id(ctx.session_path, 'original-session-' + name)
            before = (ctx.session_path.read_bytes(), ctx.history_path.read_bytes())
            write_private_text(ctx.state_dir / 'model-settings' / (name + '.json'),
                               json.dumps({'model': 'changed', 'effort': 'low'}))
            topic = read_topic(ctx)
            current = self.route(name)[1]
            self.assertEqual((current.model, current.effort), ('changed', 'low'))
            self.assertEqual((current.session_path.read_bytes(), current.history_path.read_bytes()), before)
            self.assertEqual(read_topic(current), topic)  # Nothing is frozen into the topic.

    def test_incompatible_effort_rejected_before_workspace_or_binding_changes(self):
        self.receive('task', 'work')
        _, event = self.form('task')
        app, ctx = self.route('task')
        before = read_topic(ctx)
        path = ctx.state_dir / 'launch-forms' / (event['action_name'][len(SUBMIT_PREFIX):] + '.json')
        # The budget select only records options the card offered.
        self.app.handle_card_action_event({**event, 'action_name': '', 'event_id': 'budget',
            'action_value': {'kind': 'bridge_launch_budget', 'token': path.stem}, 'option': '1:ultra'}, self.ctx)
        self.assertNotEqual(json.loads(path.read_text())['selected_effort'], 'ultra')
        # A recorded budget the model no longer supports is refused at start, not downgraded.
        record = json.loads(path.read_text())
        record['selected_effort'] = 'ultra'
        write_private_text(path, json.dumps(record))
        with self.assertRaisesRegex(ValueError, '不支持'):
            submit_launch_form(app, ctx, {**event, 'form_value': {'path': ''}})
        self.assertEqual(read_topic(ctx), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        event['form_value']['effort'] = 'default'
        self.submit(event)
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().effort, '')

    def test_local_cards_and_catalog_do_not_probe_native_processes_or_offer_menus(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('not local')):
            choices = available_models(self.app.topics, self.ctx)
            card, _ = self.form('task')
        self.assertEqual(len(choices), len(self.app.apps))
        # Option lines record their choice; only the form's buttons submit.
        self.assertEqual(set(actions(card)), {'bridge_launch_harness', 'bridge_launch_provider', 'bridge_launch_model',
                                              'bridge_launch_budget'})
        job = self.start('started')
        running = build_stream_card('progress', 'task', self.app.apps['codex'].cards.stream, job=job, include_stop_action=True)
        self.assertLessEqual(set(actions(running)), {'bridge_job_stop', 'bridge_steer', 'bridge_fork', 'bridge_switch_open'})
        self.assertNotIn('command_shortcuts', json.dumps(running))
        recovery = recovery_card({'job_id': job.job_id, 'topic_id': job.topic_id, 'topic_only': True})
        self.assertEqual(set(actions(recovery)), {'bridge_job_retry'})

    def test_plain_answer_still_uses_pending_question_in_its_topic(self):
        app, ctx = self.route('answer')
        with mock.patch.object(app.interaction, 'interactive_command', return_value=True) as answer:
            self.receive('response', 'yes', root='answer')
        self.assertEqual(answer.call_args.args[0].topic_id, ctx.topic_id)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    @requires_extension()
    def test_selected_backend_initializes_in_worker_not_launch_callback(self):
        child = self.app.apps['maso']
        child.harness.started = False
        with mock.patch.object(child.backend, 'on_startup') as startup, \
             mock.patch('covey.app.job_dispatch.check_or_raise'):
            job = self.start('selected', model='maso:maso-model')
            startup.assert_not_called()
            with mock.patch('covey.app.unified.BridgeApp.process_reply_job', autospec=True) as process:
                self.app.process_reply_job(job, self.ctx)
            self.assertEqual(startup.call_args.args[0].workdir, job.workdir)
            self.assertIs(process.call_args.args[0], child)

    def test_the_launch_card_shows_its_receipt_before_the_task_card_is_sent(self):
        """S19 review item 6: 开始任务 changed its card only after the task card was created and sent."""
        forms, published = self.ctx.state_dir / 'launch-forms', []
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(side_effect=lambda *args: published.append(
                any(json.loads(path.read_text()).get('receipt_published') for path in forms.glob('*.json'))))
        self.start('receipt-first')
        self.assertEqual(published, [True])

    def restarted_maso_topic(self):
        """A started MASO topic after a Bridge restart: its startup check has not run yet."""
        child = self.app.apps['maso']
        first = self.start('maso-topic', model='maso:maso-model')
        child.runtime.unregister_reply_job(first)
        upsert_job_record(self.ctx.state_dir, first.message_id, status='done')
        child.harness.started = False
        del child.models.prepare_submission_context  # The real one, as the Bridge runs it.
        child.backend.preset_for_effort = lambda ctx, model, effort: (model, effort)  # No catalog helper.
        return child

    def run_worker(self, job):
        with mock.patch('covey.app.job_dispatch.check_or_raise'), \
             mock.patch('covey.app.unified.BridgeApp.process_reply_job', autospec=True) as process:
            self.app.process_reply_job(job, self.ctx)
        return process

    @requires_extension()
    def test_a_slow_startup_check_runs_on_the_worker_and_holds_up_no_message(self):
        """S19 review item 10: MASO's --check-runtime (up to 60 s) ran on the message consumer."""
        child = self.restarted_maso_topic()
        checking, release = threading.Event(), threading.Event()
        child.backend.on_startup = mock.Mock(side_effect=lambda ctx: (checking.set(), release.wait(10)))
        self.receive('next-1', 'first after restart', root='maso-topic')
        job = self.app.runtime.jobs.queue.get_nowait()
        child.backend.on_startup.assert_not_called()  # Queued without waiting for the check.
        result = []
        worker = threading.Thread(daemon=True, target=lambda: result.append(self.run_worker(job)))
        worker.start()
        try:
            self.assertTrue(checking.wait(5))
            self.receive('next-2', 'while it checks', root='maso-topic')
            self.assertEqual(self.app.runtime.jobs.queue.get_nowait().message_id, 'next-2')
        finally:
            release.set()
            worker.join(10)
        self.assertIs(result[0].call_args.args[0], child)
        self.assertEqual(child.backend.on_startup.call_count, 1)
        self.assertTrue(child.harness.started)

    @requires_extension()
    def test_a_failed_check_is_told_and_retried_only_after_the_back_off(self):
        child = self.restarted_maso_topic()
        child.backend.on_startup = mock.Mock(side_effect=subprocess.TimeoutExpired(['maso'], 60))
        self.receive('next-1', 'first after restart', root='maso-topic')
        job = self.app.runtime.jobs.queue.get_nowait()
        with mock.patch.object(child.controls, 'show_recovery_card') as recovery:
            process = self.run_worker(job)  # A timeout used to escape the worker's handler: no reply at all.
        process.assert_not_called()
        recovery.assert_called_once()
        self.assertIn(BACKEND_UNAVAILABLE, self.said())
        self.receive('next-2', 'again', root='maso-topic')
        self.assertTrue(self.app.runtime.jobs.queue.empty())  # Refused at once, without a second check.
        self.assertIn('任务未提交：模型配置无法确认。MASO 启动检查超时', self.said())
        self.assertEqual(child.backend.on_startup.call_count, 1)
        at, why = child.harness.startup_failure
        child.harness.startup_failure = (at - STARTUP_RETRY_SECONDS, why)
        child.backend.on_startup.side_effect = None
        self.receive('next-3', 'later', root='maso-topic')
        self.run_worker(self.app.runtime.jobs.queue.get_nowait()).assert_called_once()
        self.assertEqual(child.backend.on_startup.call_count, 2)
        self.assertTrue(child.harness.started)

    def test_failed_text_fallback_has_recovery_in_original_topic(self):
        job = self.start('fallback')
        upsert_job_record(self.ctx.state_dir, job.message_id, status='failed')
        with mock.patch('covey.app.unified.BridgeApp.process_reply_job', autospec=True), \
             mock.patch.object(self.app.apps[job.harness_id].controls, 'show_recovery_card') as recovery:
            self.app.process_reply_job(job, self.ctx)
        self.assertEqual(recovery.call_args.args[0].topic_id, job.topic_id)
        self.assertEqual(recovery.call_args.args[1]['key'], job.message_id)


class LaunchFollowUpTests(LaunchCase):
    """REVIEW2 item 9: 开始任务 took 2.6 s of Feishu's 3 s callback window, sending each task card inside
    the click. The click now ends at once; the receipt follows beside the task cards (S36), which
    follow in the topic's follow-up lane before their jobs are queued, and a message sent meanwhile
    still runs after them."""

    def setUp(self):
        super().setUp()
        self.shutdown = threading.Event()
        self.addCleanup(self.shutdown.set)
        self.lanes = CardLanes(self.app.handle_card_action_event, self.app.card_lane_key, self.app.runtime.log,
                               self.shutdown)
        self.receive('om_l', 'original')
        _, event = self.form('om_l')
        event['form_value'].update(model='codex:codex-model', effort='low')
        self.submit_event = self.choose_model(event)
        self.token = self.submit_event['action_name'][len(SUBMIT_PREFIX):]

    def wait(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not predicate():
            time.sleep(0.01)
        self.assertTrue(predicate())

    def queued(self):
        jobs = []
        while not self.app.runtime.jobs.queue.empty():
            jobs.append(self.app.runtime.jobs.queue.get_nowait().message_id)
        return jobs

    def acknowledged(self):
        return [line for line in self.logs if line.startswith('card action kind=') and 'outcome=handled' in line]

    def test_the_click_ends_with_the_receipt_and_the_task_card_follows(self):
        making, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(side_effect=lambda *a: (making.set(), release.wait(30), None)[-1])
        self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
        self.assertTrue(making.wait(5))
        self.wait(lambda: self.acknowledged())  # Acknowledged while the task card is still being made,
        self.wait(lambda: self.launch_form(self.token).get('receipt_published'))  # and the receipt is written.
        self.assertEqual(self.queued(), [])  # The job waits for its card.
        release.set()
        self.wait(lambda: not self.app.runtime.jobs.queue.empty())
        self.assertEqual(self.queued(), ['om_l'])
        # 开始任务 is a form button with no kind: both lines name it (they said kind=unknown and the form's token).
        self.wait(lambda: any(line.startswith('card follow-up kind=bridge_launch_submit ') for line in self.logs))
        self.assertTrue(self.acknowledged()[0].startswith('card action kind=bridge_launch_submit '))

    def test_the_task_card_does_not_wait_for_the_receipt(self):
        # S35: the receipt (0.4 s) was written inside the click, and the task card only after it.
        from covey.feishu.messenger import Messenger
        writing, release, made = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        put = Messenger.put_card

        def slow_put(messenger, *args, **kwargs):
            writing.set()
            release.wait(30)
            return put(messenger, *args, **kwargs)
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(side_effect=lambda *a: (made.set(), None)[-1])
        with mock.patch.object(Messenger, 'put_card', slow_put):
            self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
            self.assertTrue(writing.wait(5))
            self.assertTrue(made.wait(5))  # Made while the receipt is still being written.
            self.wait(lambda: not self.app.runtime.jobs.queue.empty())
            self.assertEqual(self.queued(), ['om_l'])
            self.assertFalse(self.launch_form(self.token).get('receipt_published'))
            release.set()
            self.wait(lambda: self.launch_form(self.token).get('receipt_published'))
            self.wait(lambda: not self.lanes.lanes and self.lanes.unsettled == 0)

    def test_a_message_sent_before_the_follow_up_runs_after_the_launch_message(self):
        started, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)
        codex = self.app.apps['codex'].submission
        release = codex.release_waiting
        codex.release_waiting = lambda *a: (started.set(), go.wait(30), release(*a))
        self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
        self.assertTrue(started.wait(5))
        self.receive('om_more', 'also check the logs', root='om_l')  # Takes the launch message along first.
        self.assertEqual(self.queued(), ['om_l', 'om_more'])
        go.set()
        self.wait(lambda: not self.lanes.lanes)
        self.assertEqual(self.queued(), [])  # The follow-up found nothing left to queue.

    def test_a_repeated_start_click_queues_the_message_once(self):
        self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
        self.lanes.submit({**self.submit_event, 'event_id': 'again'}, self.ctx)
        self.wait(lambda: len(self.acknowledged()) == 2 and not self.lanes.lanes)
        self.assertEqual(self.queued(), ['om_l'])

    def test_a_follow_up_failure_is_told_and_clicking_again_queues_the_message(self):
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(side_effect=OSError('disk'))
        self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
        self.wait(lambda: any(text.startswith('开始任务暂未完成，请稍后重试') for text in self.said()))
        self.assertEqual(self.queued(), [])
        for app in self.app.apps.values():
            app.cards.create_streaming_reply = mock.Mock(return_value=None)
        self.lanes.submit({**self.submit_event, 'event_id': 'retry'}, self.ctx)
        self.wait(lambda: not self.app.runtime.jobs.queue.empty())
        self.assertEqual(self.queued(), ['om_l'])

    def test_a_restart_before_the_follow_up_queues_the_started_topics_messages(self):
        codex = self.app.apps['codex'].submission
        codex.release_waiting = mock.Mock()  # The follow-up never ran: the Bridge stopped first.
        self.lanes.submit({**self.submit_event, 'event_id': 'start'}, self.ctx)
        self.wait(lambda: codex.release_waiting.called and not self.lanes.lanes)
        self.assertEqual(self.queued(), [])
        restarted, base = self.bridge()
        for app in restarted.apps.values():
            app.cards.create_streaming_reply = mock.Mock(return_value=None)
        restarted.messenger.send_message = mock.Mock(return_value=True)
        restarted.release_started_launches(base)
        self.assertEqual([j.message_id for j in [restarted.runtime.jobs.queue.get_nowait()]], ['om_l'])
        self.assertIn('released 1 waiting messages of started topic=', ' '.join(self.logs))
        restarted.release_started_launches(base)  # Nothing is left waiting: nothing more is queued.
        self.assertTrue(restarted.runtime.jobs.queue.empty())


class DeviceHarnessLaunchTests(LaunchCase):
    """A device workspace starts from a Harness the device declares (devices.json ``harnesses``), and a Harness it does
    not declare is refused at 开始任务 with the reason (A-10)."""

    def setUp(self):
        super().setUp()
        from support.fake_device import FakeDevice
        from covey.core import devices
        document = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-launch",
                    "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["codex"],
                                         "spaces": ["owner_private", "team"]}}}
        (self.root / 'devices.json').write_text(json.dumps(document))
        patch = mock.patch.dict('os.environ', {'COVEY_DEVICES': '1', 'COVEY_DEVICES_FILE': str(self.root / 'devices.json')})
        patch.start()
        self.addCleanup(patch.stop)
        self.device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(self.device.cleanup(), []))
        opened = mock.patch.object(devices, 'open_transport', side_effect=lambda *a, **k: self.device.transport)
        opened.start()
        self.addCleanup(opened.stop)
        self.value = 'gpu1:%s' % self.device.home
        self.app.runtime.access.allows_device = lambda chat, device: True

    def use_device_workspace(self, *, default=False):
        config = load_config(self.ctx.state_dir, self.root)
        config['workspaces']['gpu1-proj'] = self.value
        if default:
            config['default'] = 'gpu1-proj'
        config_path(self.ctx.state_dir).write_text(json.dumps(config))

    def pick(self, event, kind, option):
        """One select callback on the card the event belongs to; the event of the card it redraws."""
        token = event['action_name'][len(SUBMIT_PREFIX):]
        with FakeCardKit():
            self.app.handle_card_action_event({**event, 'action_name': '', 'event_id': kind + '-' + str(option),
                'action_value': {'kind': kind, 'token': token}, 'option': option}, self.ctx)
            self.app.launch_model_picker.queue.join()
        return {**event, 'action_name': SUBMIT_PREFIX + current_token(self.ctx.state_dir, token), 'form_value': {'path': ''}}

    def selected(self, event):
        return self.launch_form(event['action_name'][len(SUBMIT_PREFIX):])

    def test_a_card_has_an_execution_device_row_only_with_devices_on(self):
        self.receive('task', 'hello')
        card, event = self.form('task')
        rows = settings_rows(card)
        self.assertEqual([value for value, _ in rows['device']['options']], ['', 'gpu1'])
        self.assertEqual(rows['device']['current'], '')
        self.assertNotIn('workspace', rows)
        self.assertNotIn('run_mode', rows)
        self.assertEqual(self.launch_form(card_token(card))['form_defaults'], {'device': ''})
        with mock.patch.dict('os.environ', {'COVEY_DEVICES': ''}):
            self.receive('task2', 'hello')
            self.assertNotIn('device', settings_rows(self.form('task2')[0]))

    def test_choosing_the_device_moves_the_selection_and_the_topic_starts_on_it(self):
        self.use_device_workspace()  # The device's saved workspace: where its topics start.
        self.receive('task', 'hello')
        card, event = self.form('task')
        event = self.pick(event, 'bridge_launch_model', 'claude:claude-model')
        event = self.pick(event, 'bridge_launch_device', 'gpu1')
        record = self.selected(event)
        self.assertEqual((record['selected_model'], record['form_defaults']['device']), ('codex:codex-model', 'gpu1'))
        self.pick(event, 'bridge_launch_device', 'nowhere')  # Not a device: dropped.
        self.app.handle_card_action_event(event, self.ctx)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, read_topic(self.route('task')[1])['workspace_name']), ('codex', 'gpu1-proj'))
        # Back to 本机 on another topic: the model stays where it is.
        self.receive('task-2', 'hello')
        card, event = self.form('task-2')
        event = self.pick(event, 'bridge_launch_device', 'gpu1')
        event = self.pick(event, 'bridge_launch_device', '')
        self.assertEqual(self.selected(event)['form_defaults']['device'], '')

    def test_a_device_that_declares_no_harness_the_card_uses_is_refused_on_start(self):
        self.use_device_workspace()
        self.receive('task', 'hello')
        card, event = self.form('task')
        event = self.pick(event, 'bridge_launch_device', 'gpu1')
        event = self.pick(event, 'bridge_launch_model', 'claude:claude-model')
        before = read_topic(self.route('task')[1])
        self.app.handle_card_action_event(event, self.ctx)
        self.assertIn('设备 gpu1 只声明了 codex（devices.json 的 harnesses）', '\n'.join(self.said()))
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(read_topic(self.route('task')[1]), before)

    def test_a_card_for_a_device_workspace_of_an_older_card_starts_from_the_devices_harness(self):
        self.use_device_workspace(default=True)
        self.receive('task', 'hello')
        card, event = self.form('task', draft={'workspace': 'gpu1-proj'})
        record = self.launch_form(card_token(card))
        self.assertEqual(record['models'][record['selected_model']]['harness'], 'codex')
        self.assertEqual(next(iter(record['models'])), record['selected_model'])
        self.assertEqual(record['form_defaults']['workspace'], 'gpu1-proj')

    def test_choosing_the_device_workspace_moves_the_selection_and_choosing_a_local_one_leaves_it(self):
        self.use_device_workspace()
        self.receive('task', 'hello')
        card, event = self.form('task')
        event = self.pick(event, 'bridge_launch_model', 'claude:claude-model')
        self.assertEqual(self.selected(event)['selected_model'], 'claude:claude-model')
        event = self.pick(event, 'bridge_launch_workspace', 'gpu1-proj')
        record = self.selected(event)
        self.assertEqual(record['selected_model'], 'codex:codex-model')
        self.assertEqual(record['form_defaults']['workspace'], 'gpu1-proj')
        # Back to a local workspace: the model stays where it is.
        event = self.pick(event, 'bridge_launch_workspace', load_config(self.ctx.state_dir, self.root)['default'])
        self.assertEqual(self.selected(event)['selected_model'], 'codex:codex-model')

    def test_a_model_chosen_by_hand_that_the_device_lacks_is_refused_with_the_reason(self):
        self.use_device_workspace()
        self.receive('task', 'hello')
        card, event = self.form('task')
        event = self.pick(event, 'bridge_launch_workspace', 'gpu1-proj')
        event = self.pick(event, 'bridge_launch_model', 'claude:claude-model')
        self.assertEqual(self.selected(event)['selected_model'], 'claude:claude-model')  # The owner's choice stays.
        before = read_topic(self.route('task')[1])
        self.app.handle_card_action_event(event, self.ctx)
        text = '\n'.join(self.said())
        self.assertIn('设备 gpu1 只声明了 codex（devices.json 的 harnesses）', text)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(read_topic(self.route('task')[1]), before)

    def test_the_devices_own_harness_starts(self):
        self.use_device_workspace()
        self.receive('task', 'hello')
        card, event = self.form('task')
        event = self.pick(event, 'bridge_launch_workspace', 'gpu1-proj')
        self.assertEqual(self.selected(event)['selected_model'].split(':')[0], 'codex')
        self.app.handle_card_action_event(event, self.ctx)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.user_text), ('codex', 'hello'))


class WebLaunchTests(LaunchCase):
    """The page's launch (app/web_launch.py, data contract v43): no workspace or run mode to pick, a topic starts in the home, on a
    device if the chat has devices on; an older page's payload that names them is checked as before."""

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        self.env = SimpleNamespace(topics=self.app.topics, runtime=self.app.runtime, state_dir=self.ctx.state_dir,
                                   profile=self.ctx.profile)
        self.web_ctx = SimpleNamespace(state_dir=self.ctx.state_dir, profile=self.ctx.profile, default_workdir=self.work)
        self.app.runtime.access.allows_device = lambda chat, alias: True

    def catalog(self):
        from covey.app import web_launch
        return web_launch.catalog(self.env, self.web_ctx, 'chat')

    def plan(self, **payload):
        from covey.app import web_launch
        model = self.catalog()['models'][0]
        return web_launch.plan_of(self.env, self.web_ctx, 'chat', {'model': model['key'], 'effort': model['start_budget'], **payload})

    def with_devices(self):
        from support.fake_device import FakeDevice
        from covey.core import devices
        document = {'schema': 1, 'control_dir': '/dev/shm/covey-ssh-weblaunch',
                    'devices': {'gpu1': {'ssh': {'host': 'gpu1.example'}, 'harnesses': ['codex'], 'spaces': ['owner_private', 'team']}}}
        (self.root / 'devices.json').write_text(json.dumps(document))
        patch = mock.patch.dict('os.environ', {'COVEY_DEVICES': '1', 'COVEY_DEVICES_FILE': str(self.root / 'devices.json')})
        patch.start()
        self.addCleanup(patch.stop)
        device = FakeDevice()
        self.addCleanup(lambda: self.assertEqual(device.cleanup(), []))
        opened = mock.patch.object(devices, 'open_transport', side_effect=lambda *a, **k: device.transport)
        opened.start()
        self.addCleanup(opened.stop)
        value = 'gpu1:%s' % device.home
        resolve_selection(self.ctx.state_dir, self.work, value)  # The device's saved workspace: where its topics start.
        return value

    def test_the_catalog_has_no_workspaces_or_run_modes_and_no_devices_without_them(self):
        data = self.catalog()
        for key in ('workspaces', 'run_modes', 'devices'):
            self.assertNotIn(key, data)
        self.assertNotIn('workspace', data['preselect'])
        self.assertNotIn('run_mode', data['preselect'])
        self.assertTrue(data['models'])

    def test_the_catalog_lists_the_devices_only_with_devices_on(self):
        """本机 first, as the launch card's 执行设备 row has it (T879: the page showed the first device for a topic of this
        machine and could not choose 本机), then each device the chat may use; none at all without such a device."""
        self.with_devices()
        devices = self.catalog()['devices']
        self.assertEqual([(d['name'], d['title'], d['harnesses'], d.get('local', False)) for d in devices],
                         [('', '本机', [], True), ('gpu1', 'gpu1', ['codex'], False)])
        self.assertTrue(all(d['dot'] for d in devices))  # The node panel's word, "gray" where there is none.
        self.assertNotIn('device', self.catalog()['preselect'])  # No device named: 本机, the default.
        self.app.runtime.access.allows_device = lambda chat, alias: False
        self.assertNotIn('devices', self.catalog())

    def test_a_payload_naming_no_device_or_this_machine_starts_here(self):
        """The page's 本机 is no ``device`` (or ""): the topic starts in this machine's home, a device on or off."""
        self.with_devices()
        for payload in ({}, {'device': ''}):
            plan = self.plan(**payload)
            self.assertEqual((plan['workdir'], plan.get('home_start')), (str(self.work.resolve()), True), payload)

    def test_a_payload_without_workspace_or_run_mode_starts_in_the_home(self):
        plan = self.plan()
        self.assertEqual((plan['workdir'], plan['run_mode'], plan['start'], plan['home_start']),
                         (str(self.work.resolve()), 'direct', True, True))
        self.assertEqual(plan['workspace'], self.work.resolve().name)  # The home's directory, not a workspace list entry.
        self.assertNotIn('repos', plan)

    def test_a_payload_naming_a_device_starts_there(self):
        value = self.with_devices()
        models = self.catalog()['models']
        codex, other = (next(m for m in models if (m['harness'] == 'codex') == want) for want in (True, False))
        plan = self.plan(device='gpu1', model=codex['key'], effort=codex['start_budget'])
        # On a device: not a home start (T870), as the butler's topic_start and the launch card have it.
        self.assertEqual((plan['workdir'], plan['run_mode'], plan['start'], 'home_start' in plan), (value, 'direct', True, False))
        with self.assertRaisesRegex(ValueError, '没有设备 nowhere'):
            self.plan(device='nowhere', model=codex['key'], effort=codex['start_budget'])
        with self.assertRaisesRegex(ValueError, '只声明了 codex'):
            self.plan(device='gpu1', model=other['key'], effort=other['start_budget'])

    def test_a_payload_that_names_a_workspace_or_run_mode_is_checked_as_before(self):
        name = load_config(self.ctx.state_dir, self.work)['default']
        plan = self.plan(workspace=name, run_mode='direct')
        self.assertEqual((plan['workspace'], plan['start'], 'home_start' in plan), (name, True, False))
        plan = self.plan(workspace=name, run_mode='worktree')  # Only the launch card makes one: the topic waits for it.
        self.assertEqual((plan['run_mode'], plan['start']), ('worktree', False))
        with self.assertRaisesRegex(ValueError, '没有登记工作区「elsewhere」'):
            self.plan(workspace='elsewhere')
        with self.assertRaisesRegex(ValueError, '运行方式只能是'):
            self.plan(run_mode='copy')
        self.assertEqual(self.plan(run_mode='worktree')['workspace'], name)  # A run mode alone: in the home.

    def test_the_declared_repos_go_into_the_plan_checked(self):
        from covey.app import butler_topics
        seen = []
        with mock.patch.object(butler_topics, 'declared_repos', create=True,
                               side_effect=lambda state, value: seen.append(value) or [{'name': 'covey', 'mode': 'read', 'path': '/x'}]):
            plan = self.plan(repos=[{'name': 'covey', 'mode': 'read'}])
        self.assertEqual((plan['repos'], seen), ([{'name': 'covey', 'mode': 'read', 'path': '/x'}], [[{'name': 'covey', 'mode': 'read'}]]))
        with self.assertRaisesRegex(ValueError, '最多 10 个'):
            self.plan(repos=[{'name': f'r{n}', 'mode': 'read'} for n in range(11)])
        with mock.patch.object(butler_topics, 'declared_repos', create=True, side_effect=ValueError('没有这个仓库')):
            with self.assertRaisesRegex(ValueError, '没有这个仓库'):
                self.plan(repos=[{'name': 'nope', 'mode': 'write'}])

"""Where a task runs: workspaces, the workspace manager and the directory picker.

- A topic's workspace is chosen once and kept; the scheduler runs one task per workspace.
- 管理工作区 (from the launch card or the main chat) lists, keeps and forgets workspaces.
- The directory picker on a launch card browses, caches and picks a directory; stale
  views and old cards cannot apply.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.cards import page_options, selects, walk_dicts, walk_elements, walk_elements as walk
from support.fake_lark import FakeCardKit
from support.sessions import write_session_id
from support.world import LaunchCardCase, PickerCase, TopicFixture, build_claude_app

from support import requires_extension
from covey.app import directory_picker
from covey.app.directory_picker import DirectoryCache, DirectoryPicker, scan_directory, search_directories
from covey.app.launch import SUBMIT_PREFIX, card_token, submit_launch_form
from covey.cards.launch_config import PICKER_PREFIX
from covey.app import workspace_manager
from covey.app.workspace_manager import END_STATUS, MANAGING
from covey.cards.directory import LAUNCH, SEARCH_PLACEHOLDER, directory_card
from covey.core.text import literal
from covey.cards.launch_config import MANAGE_OPTION
from covey.cards import workspace_manager as manager_cards
from covey.cards.workspace_manager import (BROWSE_HOST, BUSY_NOTE, MANAGER_PREFIX, PAGE_SIZE, QUERY_PLACEHOLDER,
                                                   REMOVED_TITLE, manager_card, row_key)
from covey.core import locks, topics, workspaces
from covey.core.jobs import list_job_records
from covey.core.scheduler import WorkspaceScheduler
from covey.core.state import write_private_text
from covey.core.topics import read_topic, remember_topic_message
from covey.core.workspaces import config_path, load_config, remove_workspaces, resolve_selection



def open_standalone(manager, ctx, chat_id, source):
    """Queue what the bot menu's 工作区 did before 2026-11-09 (its key opens the 设置 card now): a manager card of its own
    in ``chat_id``, keyed by the click ``source``, with no topic, job or launch card."""
    def draw(token, record):
        config = load_config(ctx.state_dir, ctx.default_workdir)
        return manager_card(token, record['manager'], config, manager.status(config), standalone=True)

    def start(ctx, event):
        manager.menu.open(ctx, chat_id, source, draw, '工作区卡未能发送，请稍后再点机器人菜单「工作区」。')

    manager.dispatch(ctx, {'chat_id': chat_id, 'source': source}, start)

class WorkspaceSelectionTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "workspace_selection": True})
        self.app.submission.selection_card = mock.Mock()
        self.other = self.root / "other"
        self.other.mkdir()
        config = load_config(self.ctx.state_dir, self.ctx.default_workdir)
        config["workspaces"]["另一个仓库"] = str(self.other)
        write_private_text(config_path(self.ctx.state_dir), json.dumps(config))

    def test_first_message_waits_and_choice_binds_all_followups(self):
        self.receive("om_a", "first")
        self.receive("om_a2", "second", root="om_a")
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        ctx = self.topic("om_a")
        self.assertEqual(len(self.app.harness.topic_records(ctx, "oc_chat")), 2)
        self.app.submission.start_workspace(ctx, "oc_chat", "另一个仓库")
        jobs = list(self.app.runtime.jobs.queue.queue)
        self.assertEqual([j.user_text for j in jobs], ["first", "second"])
        self.assertTrue(all(j.workdir == self.other for j in jobs))
        self.assertNotEqual(jobs[0].history_snapshot, jobs[1].history_snapshot)
        again = self.topic("om_a")
        self.assertEqual(again.workdir, self.other)
        self.assertTrue(read_topic(again)["workspace_locked"])
        self.app.submission.start_workspace(again, "oc_chat", self.ctx.default_workdir.name)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 2)
        self.assertEqual(self.topic("om_a").workdir, self.other)

    def test_default_change_only_affects_new_topics(self):
        self.receive("om_a", "first")
        previous = self.topic("om_a")
        config = load_config(self.ctx.state_dir, self.ctx.default_workdir)
        config["default"] = "另一个仓库"
        write_private_text(config_path(self.ctx.state_dir), json.dumps(config))
        self.receive("om_b", "new")
        self.assertEqual(self.topic("om_a").workdir, previous.workdir)
        self.assertEqual(self.topic("om_b").workdir, self.other)

    def test_waiting_jobs_survive_restart_without_rebinding_session(self):
        self.receive("om_a", "first")
        self.app = build_claude_app(self.backend)
        self.app.cards.streaming = False
        self.app.messenger.send_message = mock.Mock(return_value=True)
        self.app.submission.start_workspace(self.topic("om_a"), "oc_chat", "另一个仓库")
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.user_text, "first")
        self.assertEqual(job.workdir, self.other)
        self.assertIsNone(self.app.sessions.read_session_id(job.session_path))

    def test_missing_default_directory_does_not_break_existing_topic(self):
        self.receive("om_a", "existing")
        config = load_config(self.ctx.state_dir, self.ctx.default_workdir)
        config["workspaces"]["missing"] = str(self.root / "missing")
        config["default"] = "missing"
        write_private_text(config_path(self.ctx.state_dir), json.dumps(config))
        self.receive("om_a2", "followup", root="om_a")
        self.assertEqual(len(self.app.harness.topic_records(self.topic("om_a"), "oc_chat")), 2)

    def test_absolute_path_registers_and_binds_waiting_job(self):
        directory = self.root / "directory with spaces"
        directory.mkdir()
        self.receive("om_path", "task")
        self.app.submission.start_workspace(self.topic("om_path"), "oc_chat", str(directory))
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.workdir, directory)
        saved = load_config(self.ctx.state_dir, self.ctx.default_workdir)
        self.assertIn(str(directory), saved["workspaces"].values())

    def test_path_alias_collision_and_invalid_directory(self):
        first = self.root / "one" / "repo"
        second = self.root / "two" / "repo"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        a, _ = resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, str(first))
        b, _ = resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, str(second))
        self.assertNotEqual(a, b)
        self.assertEqual(resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, str(first))[0], a)
        with self.assertRaises(ValueError):
            resolve_selection(self.ctx.state_dir, self.ctx.default_workdir, str(self.root / "missing"))


class SubmissionLockTest(TopicFixture, unittest.TestCase):
    """A Feishu call stuck while one topic submits holds up that topic only, never LOCK."""

    def setUp(self):
        super().setUp()
        self.release, self.sending = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)

        def card(ctx, job, content):
            if job.message_id == "om_a":
                self.sending.set()
                self.release.wait(10)
            if job.message_id == "om_c":
                raise RuntimeError("synthetic card failure")
            return None
        self.app.cards.create_streaming_reply = card

    def background(self, *args, **kwargs):
        thread = threading.Thread(target=self.receive, args=args, kwargs=kwargs, daemon=True)
        thread.start()
        return thread

    def test_card_stuck_in_one_topic_leaves_other_topics_and_lock_free(self):
        first = self.background("om_a", "first")
        self.assertTrue(self.sending.wait(5))
        # Registered before its card exists: a binding switch or maintenance sees the topic busy.
        self.assertEqual([j.message_id for j in self.app.runtime.jobs.by_id.values()], ["om_a"])
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertTrue(locks.LOCK.acquire(timeout=1))
        locks.LOCK.release()
        other = self.background("om_b", "another topic")
        other.join(5)
        self.assertFalse(other.is_alive())
        same = self.background("om_a2", "same topic", root="om_a")
        same.join(0.3)
        self.assertTrue(same.is_alive())  # Behind the stuck card, so the topic keeps its order.
        self.release.set()
        first.join(5)
        same.join(5)
        self.assertEqual([j.message_id for j in self.app.runtime.jobs.queue.queue], ["om_b", "om_a", "om_a2"])

    def test_card_failure_leaves_the_topic_free(self):
        with self.assertRaises(RuntimeError):
            self.receive("om_c", "fails")
        self.assertEqual(self.app.runtime.jobs.by_id, {})
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_topic_lock_is_never_taken_inside_lock(self):
        with locks.LOCK, self.assertRaises(RuntimeError):
            with locks.topic_lock(self.topic("om_d")):
                pass
        with locks.topic_lock(self.topic("om_d")), locks.LOCK, locks.topic_lock(self.topic("om_d")):
            pass  # Reentry by the holder is fine.


class LockHomeTest(unittest.TestCase):
    def test_workspaces_still_names_the_moved_lock_and_topic_helpers(self):
        # Round 2 D3 moved them to core/locks.py and core/topics.py: one RLock, whichever module a caller
        # (an old release's script, a deployer's branch) imports it from.
        self.assertIs(workspaces.LOCK, locks.LOCK)
        self.assertIsInstance(locks.LOCK, type(threading.RLock()))
        self.assertIs(workspaces.topic_lock, locks.topic_lock)
        for name in ("read_topic", "proxy_topic", "workspace_locked", "bind_topic"):
            self.assertIs(getattr(workspaces, name), getattr(topics, name), name)


class WorkspaceSchedulerTest(unittest.TestCase):
    def test_same_topic_fifo_and_same_directory_topics_parallel(self):
        shutdown = threading.Event()
        incoming = queue.Queue()
        release = threading.Event()
        a_started = threading.Event()
        b_started = threading.Event()
        second = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(shutdown.set)

        def process(job):
            if job.message_id == "a1":
                a_started.set()
                release.wait(2)
            elif job.message_id == "b":
                b_started.set()
                release.wait(2)
            else:
                second.set()

        for name, directory in [("a1", "/tmp/work-a"), ("a2", "/tmp/work-a"), ("b", "/tmp/work-a")]:
            incoming.put(
                SimpleNamespace(
                    message_id=name,
                    chat_id="chat",
                    topic_id="a" if name.startswith("a") else "b",
                    workdir=Path(directory),
                    cancel_event=threading.Event(),
                )
            )
        scheduler = WorkspaceScheduler(shutdown, incoming, process, lambda _: None)
        thread = threading.Thread(target=scheduler.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(a_started.wait(1))
            self.assertTrue(b_started.wait(1))
            self.assertFalse(second.is_set())
            release.set()
            self.assertTrue(second.wait(1))
        finally:
            release.set()
            shutdown.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_ten_task_limit_and_eleventh_waits(self):
        shutdown, release = threading.Event(), threading.Event()
        ten, eleven = threading.Event(), threading.Event()
        incoming, started = queue.Queue(), []
        lock = threading.Lock()

        def process(job):
            with lock:
                started.append(job.message_id)
                if len(started) == 10:
                    ten.set()
                if len(started) == 11:
                    eleven.set()
            release.wait(5)

        for i in range(11):
            incoming.put(
                SimpleNamespace(
                    message_id=str(i),
                    chat_id="chat",
                    topic_id=str(i),
                    workdir=Path("/tmp/same"),
                    cancel_event=threading.Event(),
                )
            )
        scheduler = WorkspaceScheduler(shutdown, incoming, process, lambda _: None)
        thread = threading.Thread(target=scheduler.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(ten.wait(3))
            self.assertFalse(eleven.wait(0.3))
            release.set()
            self.assertTrue(eleven.wait(2))
        finally:
            release.set()
            shutdown.set()
            thread.join(3)

    def test_overlapping_directories_do_not_block_different_topics(self):
        scheduler = WorkspaceScheduler(None, None, None, None)
        scheduler.active = {"first": ("chat", "a")}
        scheduler.pending = [SimpleNamespace(chat_id="chat", topic_id="b", workdir=Path("/tmp/code/sub"))]
        self.assertTrue(scheduler.eligible(0))


def find(card, tag, name=None):
    return next(e for e in walk(card['body']['elements']) if e.get('tag') == tag and (name is None or e.get('name') == name))


def buttons(card):
    return {e['name'].rsplit(':', 1)[1]: e for e in walk(card['body']['elements']) if e.get('tag') == 'button'}


def values(element):
    return [o['value'] for o in element['options']]


def listed(card):
    """The names of the manager's list rows, top to bottom: a row's first column holds its name and its path."""
    return [html.unescape(re.match(r'\*\*(.+?)\*\*', line['columns'][0]['elements'][0]['content'])[1])
            for line in find(card, 'form')['elements']
            if line.get('tag') == 'column_set' and len(line['columns'][0]['elements']) == 2]


def entry_buttons(card, name):
    """The verbs of the list row of ``name``: remove and default, without the key."""
    key = row_key(name)
    return {verb.split('-')[0] for verb in buttons(card) if verb.endswith('-' + key)}


def row_buttons(card, name):
    """The buttons of the browser row whose text names the directory ``name`` (进入, then 使用/添加)."""
    for line in walk(card['body']['elements']):
        if line.get('tag') == 'column_set' and any(
                e.get('tag') == 'markdown' and f'**{literal(name)}**' in e['content'] for e in walk(line['columns'])):
            return [e for e in walk(line['columns']) if e.get('tag') == 'button']
    return []


class WorkspaceManagerTests(LaunchCardCase):

    def workspaces(self):
        self.dirs = {}
        for label in ('kept', 'stale', 'gone'):
            directory = self.root / label
            directory.mkdir()
            self.dirs[label] = resolve_selection(self.ctx.state_dir, self.work, str(directory))[0]
        self.manager = self.app.workspace_manager
        self.manager.started = True  # The tests run each queued request themselves.
        self.lark = FakeCardKit().start(self)
        self.api = self.lark.api

    def prepare(self):
        self.prepare_card()
        self.workspaces()
        self.message = 'mid'

    def menu(self, event_id='wsm'):
        """A standalone manager card, as the bot menu's 工作区 sent before 2026-11-09 (its key opens the 设置 card on
        its 工作区 tab now: test_settings_hub), in the main chat as the owner's private chat."""
        self.app.runtime.access.remember_owner_chat('chat')
        open_standalone(self.manager, self.ctx, 'chat', event_id)

    def standalone(self):
        """Open the manager from the bot menu and run the queued request."""
        self.menu()
        handler, ctx, event = self.manager.queue.get_nowait()
        with FakeCardKit({'data': {'card_id': 'scid'}}, sent=('smid', False)) as post:
            handler(ctx, event)
        self.path = next((self.ctx.state_dir / 'workspace-managers').glob('*.json'))
        self.message = 'smid'
        self.sent = post.last.created
        return post, post.send

    def config(self):
        return load_config(self.ctx.state_dir, self.work)

    def updates(self):
        return [call.card for call in self.lark.calls if call.method == 'PUT']

    def shown(self):
        """The card as the chat shows it: the last update, else the card sent."""
        updates = self.updates()
        return updates[-1] if updates else self.sent

    def act(self, verb, name, **fields):
        """A click of 移除 (remove) or 设为默认 (default) on the row of ``name``, drawn on the card shown."""
        self.assertIn(verb, entry_buttons(self.shown(), name))
        self.click(f'{verb}-{row_key(name)}', **fields)

    def text(self):
        return json.dumps(self.shown(), ensure_ascii=False)

    def open(self):
        event = {**self.callback, 'action_value': {'kind': 'bridge_launch_workspace', 'token': self.path.stem},
                 'option': MANAGE_OPTION}
        self.assertTrue(self.manager.matches(event))
        self.assertEqual(self.app._handle_card_action_event(event, self.ctx), 'workspace_manager')
        self.manager.queue.get_nowait()
        self.manager.process(self.ctx, event)

    def choose(self, kind, option, token=None):
        """As LaunchCardCase.choose; the manager then opens from the card shown now (each choice redraws it)."""
        token = super().choose(kind, option, token)
        self.path = self.path.parent / (token + '.json')
        return token

    def click(self, verb, *names, revision=None, **fields):
        revision = revision or self.record()['manager']['revision']
        event = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': self.message,
                 'action_name': f'{MANAGER_PREFIX}{self.path.stem}:{revision}:{verb}',
                 'form_value': {'names': list(names), **fields}}
        self.assertEqual(self.app._handle_card_action_event(event, self.ctx), 'workspace_manager')
        self.manager.queue.get_nowait()
        self.manager.process(self.ctx, event)

    def record(self, token=None):
        folder = 'workspace-managers' if self.path.parent.name == 'workspace-managers' and not token else 'launch-forms'
        return json.loads((self.ctx.state_dir / folder / ((token or self.path.stem) + '.json')).read_text())

    def use(self, **days):
        config = self.config()
        config['recent'] = {self.dirs[label]: time.time() - ago * 86400 for label, ago in days.items()}
        write_private_text(self.ctx.state_dir / 'workspace-config.json', json.dumps(config))

    def test_menu_entry_opens_one_list_merged_with_the_selection(self):
        self.prepare()
        workspace = selects(self.card)['workspace']
        self.assertEqual(page_options(self.card, 'workspace')[-1][0], MANAGE_OPTION)  # The workspace row ends with it.
        self.assertNotEqual(workspace['initial_option'], MANAGE_OPTION)
        (self.root / 'gone').rmdir()
        self.open()
        self.assertEqual((self.lark.last.method, self.lark.last.path), ('PUT', '/open-apis/cardkit/v1/cards/cid'))
        self.assertEqual(self.record()['status'], MANAGING)
        card = self.shown()
        elements = card['body']['elements']
        # In the launch card's place too, under the title bar the launch card and the browser have.
        head = card['header']
        self.assertEqual((head['title']['content'], head['subtitle']['content'], head['text_tag_list'][0]['text']['content']),
                         ('工作区', '共 4 个 · 默认 project', '1 个目录不存在'))
        self.assertEqual([e['tag'] for e in elements], ['form'])
        # A row a workspace, the default first: its name and tags, its path and last use below.
        self.assertEqual(listed(card)[0], 'project')
        self.assertEqual(set(listed(card)), set(self.config()['workspaces']))
        gone = next(r for r in find(card, 'form')['elements'] if f"**{self.dirs['gone']}**" in json.dumps(r))
        self.assertEqual([e['content'] for e in gone['columns'][0]['elements']],
                         ["**gone** <text_tag color='red'>目录不存在</text_tag>",
                          f"<font color='grey'>{literal(self.root / 'gone')} · 未用于任务</font>"])
        self.assertIn("**project** <text_tag color='blue'>默认</text_tag>", self.text())
        # The default has no buttons; a missing directory can be removed, not made the default.
        self.assertEqual(entry_buttons(card, 'project'), set())
        self.assertEqual(entry_buttons(card, self.dirs['gone']), {'remove'})
        self.assertEqual(entry_buttons(card, self.dirs['kept']), {'remove', 'default'})
        kept = buttons(card)['remove-' + row_key(self.dirs['kept'])]
        self.assertEqual((kept['text']['content'], kept['type'], kept['form_action_type']), ('移除', 'danger_text', 'submit'))
        self.assertEqual(buttons(card)['default-' + row_key(self.dirs['kept'])]['type'], 'primary_text')
        self.assertEqual(find(card, 'input', 'query')['placeholder']['content'], QUERY_PLACEHOLDER)
        self.assertEqual(values(find(card, 'select_static', 'filter')), ['all', 'missing', 'stale', 'unused'])
        labels = {verb: b['text']['content'] for verb, b in buttons(card).items() if '-' not in verb}
        self.assertEqual(labels, {'browse': '＋ 添加工作区…', 'filter': '筛选', 'prune': '移除不存在的（1）',
                                  'refresh': '刷新', 'back': '返回启动卡', 'end': '结束，不启动任务'})
        self.assertNotIn('第 1/', self.text())  # One page.
        text = self.text()
        self.assertIn('不删除目录', text)
        self.assertNotIn('失效', text)
        # 开始任务 on the replaced view cannot start, and the menu choice was not recorded as a workspace.
        self.assertNotEqual(self.record()['form_defaults']['workspace'], MANAGE_OPTION)
        with self.assertRaisesRegex(ValueError, '最新选择表单'):
            submit_launch_form(self.child, self.target, self.submit)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_standalone_entry_leaves_no_topic_job_or_launch_card(self):
        self.workspaces()
        self.receive('thread-reply', '管理工作区', root='elsewhere')  # Inside a thread it is an ordinary message.
        self.assertTrue(self.manager.queue.empty())
        topics = set((self.ctx.state_dir / 'topics').iterdir())
        jobs = list_job_records(self.ctx.state_dir, limit=0)
        post, send = self.standalone()
        self.assertEqual(set((self.ctx.state_dir / 'topics').iterdir()), topics)
        self.assertEqual(list_job_records(self.ctx.state_dir, limit=0), jobs)
        self.assertFalse((self.ctx.state_dir / 'launch-forms').exists())
        self.assertEqual((post.last.method, post.last.path), ('POST', '/open-apis/cardkit/v1/cards'))
        self.assertEqual(send.call_args.args[2], 'scid')
        self.assertEqual(send.call_args.args[1], 'chat')
        self.assertEqual(send.call_args.kwargs.get('reply_to_message_id', ''), '')  # A new message in the chat.
        record = self.record()
        self.assertEqual((record['status'], record['card_id'], record['message_id'], record['source_event_id']),
                         ('open', 'scid', 'smid', 'wsm'))
        card = post.last.created
        self.assertEqual({v: b['text']['content'] for v, b in buttons(card).items() if v in ('refresh', 'done')},
                         {'refresh': '刷新', 'done': '完成管理'})
        self.assertNotIn('back', buttons(card))
        # The bot menu's panels' look: a white title bar with the blue folder icon and the count.
        self.assertEqual((card['header']['title']['content'], card['header']['subtitle']['content'],
                          card['header']['icon']['token'], card['config']['summary']['content']),
                         ('工作区', '共 4 个 · 默认 project', 'folder_outlined', '工作区'))
        self.assertNotIn('text_tag_list', card['header'])  # Nothing is missing.
        # The same click again reaches the manager, which sends no second card. The old text is an ordinary message.
        open_standalone(self.manager, self.ctx, 'chat', 'wsm')
        handler, ctx, event = self.manager.queue.get_nowait()
        with FakeCardKit() as again:
            handler(ctx, event)
        self.assertEqual(again.calls, [])
        self.app.handle_event(self.event('wsm2', '管理工作区'), self.ctx)
        self.assertTrue(self.manager.queue.empty())
        # Clicks work without a topic.
        self.act('remove', self.dirs['stale'])
        self.assertNotIn(self.dirs['stale'], self.config()['workspaces'])
        self.assertEqual(self.lark.last.path, '/open-apis/cardkit/v1/cards/scid')

    def test_buttons_keep_their_width_and_wrap_on_narrow_screens(self):
        # On a phone, 筛选 / 返回启动卡 / 结束，不启动任务 were cut to "…" in weighted columns; stacked, the card was
        # 14 controls tall in the topic side panel, and two to a line cut 结束，不启动任务 again (2026-11-02 review).
        for standalone in (True, False):
            card = manager_card("a" * 24, {"revision": 1}, self.config(), {}, standalone=standalone)
            rows = [e for e in walk_dicts(card) if e.get("tag") == "column_set"]
            self.assertEqual({e.get("flex_mode") for e in rows}, {"flow", "none"})
            for column in (c for r in rows for c in r["columns"]):
                if column["elements"][0].get("tag") == "button":
                    self.assertEqual(column["width"], "auto")
            ends = next(r for r in rows if "refresh" in json.dumps(r))
            self.assertEqual(ends["flex_mode"], "flow")

    def test_standalone_failed_delivery_says_so(self):
        self.workspaces()
        self.menu()
        handler, ctx, event = self.manager.queue.get_nowait()
        with FakeCardKit(None):
            handler(ctx, event)
        self.assertIn('未能发送', self.app.messenger.send_message.call_args.args[2])
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs.get('reply_to_message_id', ''), '')  # The main chat.

    def test_done_replaces_the_standalone_card_with_a_closed_summary(self):
        self.workspaces()
        self.standalone()
        self.click('remove', self.dirs['stale'])
        self.click('done')
        card = self.shown()
        self.assertEqual(card['body']['elements'],
                         [{'tag': 'markdown', 'content': '**工作区管理已完成** · 本次移除 1 项 · 默认工作区：project'}])
        self.assertEqual(self.record()['status'], 'closed')
        calls = self.api.call_count
        self.click('remove', self.dirs['kept'], revision=self.record()['manager']['revision'] - 1)
        self.assertEqual(self.api.call_count, calls)
        self.assertIn(self.dirs['kept'], self.config()['workspaces'])
        # A card left open (its update failed) shows the closed state again on 刷新.
        self.click('refresh', revision=1)
        self.assertEqual(self.shown(), card)

    @requires_extension()
    def test_end_cancels_the_waiting_message_and_its_launch_form_never_starts(self):
        self.prepare()
        old = self.path.stem
        _, token = self.change_model()  # The first card was replaced by a model switch.
        self.path = self.path.parent / (token + '.json')
        self.open()
        self.click('remove', self.dirs['gone'])
        self.click('end')
        jobs = [r for r in list_job_records(self.ctx.state_dir, limit=0) if r.get('topic_id') == self.target.topic_id]
        self.assertTrue(jobs)
        self.assertEqual({(r['status'], r['status_message'], r.get('never_started')) for r in jobs},
                         {('cancelled', END_STATUS, True)})
        self.assertEqual(self.record()['status'], 'closed')
        self.assertEqual(self.shown()['body']['elements'][0]['content'],
                         '**已结束** · 本话题未启动任务；之后在主聊天发送新消息开始任务\n本次移除 1 项 · 默认工作区：project')
        self.assertEqual(buttons(self.shown()), {})
        for start in (token, old):
            with self.assertRaisesRegex(ValueError, '已结束'):
                submit_launch_form(self.child, self.target, {**self.submit, 'action_name': SUBMIT_PREFIX + start})
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        calls = self.api.call_count
        self.click('back', revision=self.record()['manager']['revision'])  # Same view: shows the end again.
        self.assertEqual(self.api.call_count, calls + 1)
        self.assertEqual(self.record()['status'], 'closed')

    def test_search_and_every_filter(self):
        self.workspaces()
        self.use(kept=1, stale=40)
        (self.root / 'gone').rmdir()
        self.standalone()

        def drawn():
            return set(listed(self.shown()))

        self.click('filter', query='KEP')
        self.assertEqual(drawn(), {self.dirs['kept']})
        self.assertEqual(find(self.shown(), 'input', 'query')['default_value'], 'KEP')
        self.assertIn('搜索“KEP” · 全部 1 个', self.text())
        self.click('filter', query=str(self.root / 'STA')[-8:])  # Part of the full path, any case.
        self.assertEqual(drawn(), {self.dirs['stale']})
        expected = {'missing': {self.dirs['gone']}, 'stale': {self.dirs['stale']}, 'unused': {self.dirs['gone'], 'project'},
                    'all': {self.dirs['kept'], self.dirs['stale'], self.dirs['gone'], 'project'}}
        for kind, names in expected.items():
            self.click('filter', query='', filter=kind)
            self.assertEqual(drawn(), names, kind)
            self.assertEqual(find(self.shown(), 'select_static', 'filter')['initial_option'], kind)
        self.click('filter', filter='unused')
        self.assertEqual(listed(self.shown()), ['project', self.dirs['gone']])  # The default first, without buttons.
        self.assertEqual(entry_buttons(self.shown(), 'project'), set())
        self.click('filter', query='nothing-matches', filter='all')
        self.assertIn('没有符合条件的工作区', self.text())
        self.assertEqual(listed(self.shown()), [])
        # A row's small print shows where it is and its last use.
        self.click('filter', query='', filter='all')
        kept = next(r for r in find(self.shown(), 'form')['elements'] if f"**{self.dirs['kept']}**" in json.dumps(r))
        self.assertRegex(kept['columns'][0]['elements'][1]['content'], r' · 最近 \d\d-\d\d</font>$')

    def test_removal_shows_a_short_result_with_details(self):
        self.prepare()
        self.open()
        # A card sent before the rows (2026-11-03) removes what its multi-select names.
        self.click('remove', self.dirs['stale'], self.dirs['kept'])
        self.assertNotIn(self.dirs['stale'], self.config()['workspaces'])
        self.assertTrue((self.root / 'stale').is_dir())
        card = self.shown()
        elements = card['body']['elements']
        self.assertEqual(elements[0]['content'], '✓ 已从列表移除 2 项，磁盘目录未删除')
        self.assertEqual(elements[1]['header']['title']['content'], REMOVED_TITLE)
        self.assertFalse(elements[1]['expanded'])
        self.assertIn(literal(self.root / 'stale'), elements[1]['elements'][0]['content'])
        self.assertEqual(listed(card), ['project', self.dirs['gone']])
        size = len(elements)
        self.click('default', default=self.dirs['gone'])
        self.assertEqual(self.config()['default'], self.dirs['gone'])
        text = self.text()
        self.assertIn('✓ 已把 gone 设为默认工作区', text)
        self.assertNotIn('已从列表移除', text)  # Each notice replaces the previous one.
        self.assertEqual(len(self.shown()['body']['elements']), size - 1)
        self.assertEqual(self.record()['manager']['removed'], 2)

    def test_a_change_shows_a_processing_card_before_it_is_applied(self):
        self.prepare()
        self.open()
        seen = []
        self.api.side_effect = lambda *a, **k: seen.append(self.dirs['kept'] in self.config()['workspaces']) or {'code': 0}
        self.act('remove', self.dirs['kept'], revision=1)
        processing, result = self.updates()[-2:]
        self.assertEqual(seen[-2:], [True, False])
        self.assertIn(BUSY_NOTE, json.dumps(processing, ensure_ascii=False))
        self.assertEqual({v for v, b in buttons(processing).items() if not b['disabled']}, {'refresh'})
        self.assertTrue(all(b['name'].split(':')[1] == '2' for b in buttons(processing).values()))
        self.assertTrue(all(b['name'].split(':')[1] == '3' for b in buttons(result).values()))
        self.assertNotIn('正在处理', json.dumps(result, ensure_ascii=False))
        self.assertNotIn(self.dirs['kept'], listed(result))
        # A click that changes nothing gets no processing step: a row already gone, an old card's empty selection.
        calls = self.api.call_count
        self.click('remove-' + row_key(self.dirs['kept']))
        self.assertEqual(self.api.call_count, calls + 1)
        self.assertIn('该工作区已不在列表中', self.text())
        self.click('remove')
        self.assertEqual(self.api.call_count, calls + 2)
        self.assertIn('请先在下拉框中选择', self.text())

    def test_a_failed_final_update_reports_success_and_refresh_recovers(self):
        self.workspaces()
        self.standalone()
        self.api.side_effect = [{'code': 0}, None, None]
        self.act('remove', self.dirs['stale'], revision=1)
        self.assertNotIn(self.dirs['stale'], self.config()['workspaces'])
        text, key = self.app.messenger.send_message.call_args.args[2:4]
        self.assertEqual(text, '操作已完成：已从列表移除 1 项，磁盘目录未删除。卡片未能刷新，请点卡片上的“刷新”。')
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs['reply_to_message_id'], '')  # The main chat, like the card.
        self.assertEqual(self.record()['manager']['revision'], 3)
        # The card still shows the processing view (revision 2): only 刷新 is accepted from it.
        self.api.side_effect = None
        calls = self.api.call_count
        self.click('remove-' + row_key(self.dirs['kept']), revision=2)
        self.assertEqual(self.api.call_count, calls)
        self.click('refresh', revision=2)
        self.assertIn('✓ 已从列表移除 1 项', self.text())
        self.assertFalse(buttons(self.shown())['remove-' + row_key(self.dirs['kept'])]['disabled'])
        self.assertEqual(self.record()['manager']['revision'], 4)

    def test_a_failed_update_in_a_topic_replies_there(self):
        self.prepare()
        self.open()
        self.api.side_effect = [{'code': 0}, None, None]
        self.act('default', self.dirs['kept'])
        self.assertEqual(self.config()['default'], self.dirs['kept'])
        self.assertIn('操作已完成：已把 kept 设为默认工作区', self.app.messenger.send_message.call_args.args[2])
        self.assertEqual(self.app.messenger.send_message.call_args.kwargs['reply_to_message_id'], 'task')

    def test_stale_and_repeated_clicks_are_ignored(self):
        self.prepare()
        self.open()
        self.act('remove', self.dirs['stale'])
        self.assertEqual(self.record()['manager']['revision'], 3)
        calls = self.api.call_count
        gone = 'remove-' + row_key(self.dirs['gone'])
        self.click(gone, revision=1)  # A repeated click from the first view.
        self.click(gone, revision=2)  # From the processing view.
        self.click('remove', self.dirs['gone'], revision=1)  # An old card's.
        self.click('page1', revision=1)
        self.assertEqual(self.api.call_count, calls)
        self.assertIn(self.dirs['gone'], self.config()['workspaces'])

    def test_prune_removes_only_confirmed_missing_directories(self):
        self.prepare()
        (self.root / 'gone').rmdir()
        self.open()
        unanswered = str(self.root / 'stale')
        real = workspaces.directory_status

        def status(paths):
            return {**real(paths), unanswered: None}
        # The manager's card and the prune it runs each look the status up in their own module.
        with mock.patch.object(workspaces, 'directory_status', status), \
                mock.patch.object(workspace_manager, 'directory_status', status):
            self.click('prune')
        listed = self.config()['workspaces']
        self.assertNotIn(self.dirs['gone'], listed)
        self.assertIn(self.dirs['stale'], listed)
        self.assertIn(self.dirs['kept'], listed)
        self.assertIn('✓ 已从列表移除 1 项', self.text())
        self.assertNotIn('prune', buttons(self.shown()))  # Shown only while some are missing.
        self.click('prune')
        self.assertIn('没有目录不存在的工作区', self.text())

    def test_the_default_row_has_no_buttons_and_cannot_be_removed(self):
        self.prepare()
        self.open()
        self.assertEqual(entry_buttons(self.shown(), 'project'), set())
        calls = self.api.call_count
        self.click('default-' + row_key('project'))  # Not offered; forged ones are refused.
        self.assertEqual(self.api.call_count, calls + 1)
        self.assertIn('已是默认工作区', self.text())
        self.click('default', default='project')  # An old card's select.
        self.assertIn('已是默认工作区', self.text())
        (self.root / 'gone').rmdir()
        self.click('refresh')
        self.assertEqual(entry_buttons(self.shown(), self.dirs['gone']), {'remove'})
        self.click('default-' + row_key(self.dirs['gone']))
        self.assertEqual(self.config()['default'], self.work.name)
        self.assertIn('⚠️ 工作区目录不存在', self.text())
        for forged in (('remove-' + row_key(self.work.name),), ('remove', self.work.name)):
            self.click(*forged)
            self.assertIn(self.work.name, self.config()['workspaces'])
            self.assertIn('不能移除', self.text())
        self.act('default', self.dirs['kept'])
        self.assertEqual(self.config()['default'], self.dirs['kept'])
        self.assertEqual(listed(self.shown())[0], self.dirs['kept'])
        self.assertEqual(entry_buttons(self.shown(), 'project'), {'remove', 'default'})
        self.act('remove', self.work.name)
        self.assertNotIn(self.work.name, self.config()['workspaces'])

    def test_back_rebuilds_the_launch_card_with_its_choices_and_starts_once(self):
        self.prepare()
        self.choose('bridge_launch_workspace', self.dirs['stale'])
        self.choose('bridge_launch_mode', 'worktree')
        self.choose('bridge_launch_budget', '1:low')
        self.open()
        self.click('remove', self.dirs['gone'])
        self.click('back')
        card = self.shown()
        token = card_token(card)
        self.assertEqual(self.record()['status'], 'replaced')
        self.assertEqual(self.record()['replaced_by'], token)
        options = selects(card)
        self.assertEqual(options['workspace']['initial_option'], self.dirs['stale'])
        self.assertEqual(options['run_mode']['initial_option'], 'worktree')
        self.assertEqual(options['effort']['initial_option'], '1:low')
        self.assertNotIn(self.dirs['gone'], dict(page_options(card, 'workspace')))
        self.assertEqual(self.record(token)['card_id'], 'cid')
        self.choose('bridge_launch_mode', 'direct', token)
        submit = {**self.submit, 'action_name': SUBMIT_PREFIX + token}
        with FakeCardKit():
            submit_launch_form(self.child, self.target, submit)
            submit_launch_form(self.child, self.target, submit)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().workdir, self.root / 'stale')

    def row(self, name, verb='pick'):
        """The browser's button of the row named ``name``: (its verb with the row index, the button)."""
        for found in row_buttons(self.shown(), name):
            if found['name'].rsplit(':', 1)[1].startswith(f'dir-{verb}'):
                return found['name'].rsplit(':', 1)[1], found
        raise AssertionError(f'no row {name}')

    def test_add_browses_down_from_the_shared_parent_and_back_chooses_the_last(self):
        self.prepare()
        self.open()
        (self.root / 'fresh').mkdir()
        (self.root / 'outer' / 'inner').mkdir(parents=True)
        self.click('browse', query='kept')  # The list's search is not the browser's.
        card = self.shown()
        self.assertEqual(card['header']['title']['content'], '添加工作区')
        form = card['body']['elements'][0]['elements']
        self.assertEqual(form[0]['content'], f'📂 **{literal(self.root.name)}**')
        self.assertEqual(form[1]['content'], literal(self.root))  # Where project, kept, stale and gone are.
        self.assertEqual(find(card, 'input', 'query')['placeholder']['content'], SEARCH_PLACEHOLDER)
        self.assertEqual(find(card, 'input', 'query')['default_value'], '')
        verb, kept = self.row('kept')
        self.assertEqual((kept['text']['content'], kept['disabled']), ('已添加', True))
        verb, fresh = self.row('fresh')
        self.assertEqual((fresh['text']['content'], fresh['disabled']), ('添加', False))
        self.assertEqual({b['text']['content'] for b in buttons(card).values()} >= {
            '⬆️ 上一级', '🔄 刷新', '搜索', '返回管理页', '添加此目录'}, True)
        self.click(verb)
        self.assertEqual(self.config()['workspaces']['fresh'], str((self.root / 'fresh').resolve()))
        self.assertEqual(self.shown()['header']['title']['content'], '添加工作区')  # Stays to add more.
        self.assertIn('✓ 已添加 fresh', self.text())
        self.assertEqual(self.row('fresh')[1]['text']['content'], '已添加')
        self.click(self.row('outer', 'enter')[0])
        self.click(self.row('inner')[0])
        self.assertIn('inner', self.config()['workspaces'])
        self.click('dir-use')  # 添加此目录: outer itself.
        self.assertIn('✓ 已添加 outer', self.text())
        use = buttons(self.shown())['dir-use']
        self.assertEqual((use['text']['content'], use['disabled']), ('此目录已在列表', True))
        self.assertEqual(self.shown()['body']['elements'][0]['elements'][0]['content'],
                         "📂 **outer** <font color='grey'>· 已在列表</font>")  # Listed under its own name.
        self.click('dir-back')
        self.assertIn('✓ 本次添加 3 项', self.text())
        self.assertEqual(self.shown()['header']['title']['content'], '工作区')
        self.assertEqual(find(self.shown(), 'input', 'query')['default_value'], 'kept')
        self.click('back')
        self.assertEqual(selects(self.shown())['workspace']['initial_option'], 'outer')
        self.assertIn('已选择工作区：outer', self.text())

    def test_a_card_sent_with_the_path_row_still_adds(self):
        self.prepare()
        self.open()
        fresh = self.root / 'fresh'
        fresh.mkdir()
        self.click('add', add_path=f'  {fresh}  ')
        self.assertEqual(self.config()['workspaces']['fresh'], str(fresh.resolve()))
        self.assertIn('✓ 已添加工作区 fresh', self.text())
        self.click('add', add_path=f'{fresh}/')
        self.assertIn('该目录已在列表中：fresh', self.text())
        for typed, reason in (('fresh', '绝对路径'), (str(self.root / 'nowhere'), '目录不存在'), (' ', '请先填写')):
            before = self.config()
            self.click('add', add_path=typed)
            self.assertIn(reason, self.text())
            self.assertEqual(self.config(), before)
        self.assertNotIn('add_path', self.text())  # Today's card has no path row.

    def test_browser_search_paste_paging_and_a_missing_directory(self):
        self.workspaces()
        self.standalone()
        for i in range(25):
            (self.root / 'many' / f'sub-{i:02}').mkdir(parents=True)
        (self.root / 'many' / 'sub-03' / 'deep-sub').mkdir()
        self.click('browse')
        self.click('dir-search', query=str(self.root / 'many'))
        self.assertEqual(self.record()['manager']['browse']['path'], str(self.root / 'many'))
        self.assertIn('共 25 个子目录', self.text())
        self.assertIn('第 1/2 页', self.text())
        self.assertNotIn(literal('sub-20'), self.text())
        self.click('dir-page1')
        self.assertIn(literal('sub-24'), self.text())
        self.assertIn('第 2/2 页', self.text())
        self.click('dir-search', query='DEEP')
        self.assertIn('搜索“DEEP”：1 个结果', self.text())
        self.assertEqual([e['name'] for e in self.record()['manager']['browse']['entries']], ['sub-03/deep-sub'])
        self.assertIn('清除搜索', self.text())
        self.click(self.row('deep-sub')[0])
        self.assertIn('deep-sub', self.config()['workspaces'])
        self.click('dir-clear')
        self.assertNotIn('清除搜索', self.text())
        self.click('dir-search', query=str(self.root / 'nowhere'))
        self.assertIn('路径不存在', self.text())
        self.assertTrue(buttons(self.shown())['dir-use']['disabled'])
        self.assertFalse(buttons(self.shown())['dir-up']['disabled'])
        self.click('dir-search', query='~no-such-user-here')
        self.assertIn('请输入绝对路径', self.text())
        self.click('dir-up')
        self.assertEqual(self.record()['manager']['browse']['path'], str(self.root))
        self.click('dir-back')
        self.click('done')
        self.assertIn('本次添加 1 项', self.text())

    def test_browser_stale_clicks_group_members_and_old_verbs_change_nothing(self):
        self.workspaces()
        self.standalone()
        (self.root / 'fresh').mkdir()
        self.click('browse')
        revision = self.record()['manager']['revision']
        verb = self.row('fresh')[0]
        before = self.config()
        self.click('dir-refresh')
        self.click(verb, revision=revision)  # Sent before the refresh.
        self.click('remove', self.dirs['stale'])  # The list's buttons wait for 返回管理页.
        self.click('dir-pick9999')
        self.assertEqual(self.config(), before)
        event = {'operator_id': 'other', 'chat_id': 'chat', 'message_id': self.message,
                 'action_name': f"{MANAGER_PREFIX}{self.path.stem}:{self.record()['manager']['revision']}:{verb}",
                 'form_value': {}}
        puts = len(self.updates())
        self.manager.process(self.ctx, event)  # Someone else in the owner's private chat: not served.
        self.assertEqual((self.config(), len(self.updates())), (before, puts))
        # Digits only follow the browser's verbs and page, a key only remove and default.
        key = row_key(self.dirs['kept'])
        for verb in ('remove3', 'page', 'page-' + key, 'filter-' + key, 'dir-remove-' + key, 'remove-' + key[:-1],
                     'remove3-' + key):
            self.assertIsNone(self.manager._click({**event, 'action_name': f'{MANAGER_PREFIX}{self.path.stem}:1:{verb}'}))
        self.assertEqual(self.manager._click({**event, 'action_name': f'{MANAGER_PREFIX}{self.path.stem}:1:default-{key}'}),
                         (self.path.stem, 1, 'default', key))
        self.assertEqual(self.manager._click({**event, 'action_name': f'{MANAGER_PREFIX}{self.path.stem}:1:page12'}),
                         (self.path.stem, 1, 'page', 12))

    def test_browser_starts_at_the_parent_most_workspaces_share(self):
        start = workspace_manager.start_directory
        self.assertEqual(start({'default': 'a', 'workspaces': {'a': '/x/a', 'b': '/y/b', 'c': '/y/c'}}), '/y')
        self.assertEqual(start({'default': 'a', 'workspaces': {'a': '/x/a', 'b': '/y/b'}}), '/x')
        self.assertEqual(start({'default': 'a', 'workspaces': {'a': '/x/a', 'b': '/x/b', 'c': '/y/c', 'd': '/y/d'}}), '/x')
        self.assertEqual(start({'default': 'a', 'workspaces': {'a': '/'}}), '/')

    def test_standalone_add_is_counted_when_done(self):
        self.workspaces()
        self.standalone()
        (self.root / 'extra').mkdir()
        self.click('browse')
        self.click(self.row('extra')[0])
        self.assertIn('extra', self.config()['workspaces'])
        self.click('dir-back')
        self.click('done')
        self.assertEqual(self.shown()['body']['elements'][0]['content'],
                         '**工作区管理已完成** · 本次添加 1 项、移除 0 项 · 默认工作区：project')

    def test_back_after_removing_the_chosen_workspace_says_so(self):
        self.prepare()
        self.choose('bridge_launch_workspace', self.dirs['stale'])
        self.open()
        self.click('remove', self.dirs['stale'])
        self.click('back')
        self.assertIn('原先选择的工作区已移除', self.text())
        self.assertIn(selects(self.shown())['workspace']['initial_option'], self.config()['workspaces'])

    def test_a_card_sent_before_a_removal_cannot_start_in_another_workspace(self):
        self.prepare()
        self.open()
        self.click('remove', self.dirs['stale'])
        self.click('back')
        token = card_token(self.shown())
        # A second card still lists the removed entry.
        self.choose('bridge_launch_workspace', self.dirs['stale'], token)
        with self.assertRaisesRegex(ValueError, '已从列表移除'):
            submit_launch_form(self.child, self.target, {**self.submit, 'action_name': SUBMIT_PREFIX + token})
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_removal_leaves_started_topics_in_their_directory(self):
        self.prepare()
        self.choose('bridge_launch_workspace', self.dirs['stale'])
        with FakeCardKit():
            submit_launch_form(self.child, self.target, self.submit)
        self.app.runtime.jobs.queue.get_nowait()
        remove_workspaces(self.ctx.state_dir, self.work, [self.dirs['stale']])
        self.assertEqual(read_topic(self.target)['workdir'], str(self.root / 'stale'))
        _, again = self.route('task')
        self.assertEqual(again.workdir, self.root / 'stale')
        # Its launch card is done; the manager no longer opens from it.
        self.api.reset_mock()
        self.manager.process(self.ctx, {**self.callback, 'action_value': {
            'kind': 'bridge_launch_workspace', 'token': self.path.stem}, 'option': MANAGE_OPTION})
        self.api.assert_not_called()

    def test_wrong_owner_message_or_failed_open_change_nothing(self):
        self.prepare()
        event = {**self.callback, 'action_value': {'kind': 'bridge_launch_workspace', 'token': self.path.stem},
                 'option': MANAGE_OPTION}
        for change in ({'operator_id': 'other'}, {'message_id': 'other'}, {'chat_id': 'other'},
                       {'action_value': {'kind': 'bridge_launch_workspace', 'token': '../x'}}):
            self.manager.process(self.ctx, {**event, **change})
        self.manager.process(self.ctx, {**event, 'action_name': f'{MANAGER_PREFIX}{self.path.stem}:1:open'})
        self.api.assert_not_called()
        self.api.return_value = None
        self.manager.process(self.ctx, event)
        self.assertEqual(self.record()['status'], 'ready')
        self.assertIn('未能打开', self.app.messenger.send_message.call_args.args[2])
        self.api.return_value = {'code': 0}
        self.manager.process(self.ctx, event)
        self.assertEqual(self.record()['status'], MANAGING)
        self.assertEqual(self.lark.calls[-1].data['sequence'], self.lark.calls[-2].data['sequence'] + 1)

    def test_a_long_list_is_paged_within_the_card_limits(self):
        root = '/mnt/a-rather-long-network-share/with/several/levels/of/project-directories' * 3
        config = {'default': 'w0', 'workspaces': {f'w{i}-' + '名' * 80: f'{root}/项目-{i}' for i in range(150)},
                  'recent': {}}
        config['workspaces']['w0'] = root + '/default'
        status = {path: i % 3 != 0 for i, path in enumerate(config['workspaces'].values())}
        view = {'revision': 7, 'notice': '✓ 已从列表移除 2 项，磁盘目录未删除', 'details': '**a** · <font color=\'grey\'>/x</font>'}
        for standalone in (True, False):
            card = manager_card('0' * 24, view, config, status, standalone=standalone)
            data = json.dumps(card, ensure_ascii=False)
            self.assertLess(len(data.encode('utf-8')), 30000)
            self.assertLess(data.count('"tag"'), 200)
            self.assertEqual(len(listed(card)), PAGE_SIZE)
            self.assertIn(f'第 1/{-(-151 // PAGE_SIZE)} 页', data)
            self.assertIn("<font color='grey'>…" + literal((root + '/default')[-119:]) + ' · ', data)  # A long path keeps its end.
            self.assertIn('移除不存在的（50）', data)
            self.assertNotIn('本页只列出', data)
        # Were a page too large for a card, its last rows are left out and the card says so.
        with mock.patch.object(manager_cards, 'PAGE_SIZE', 40):
            card = manager_card('0' * 24, view, config, status, standalone=True)
        data = json.dumps(card, ensure_ascii=False)
        self.assertLess(data.count('"tag"'), 200)
        self.assertIn(f'本页只列出前 {len(listed(card))} 个', data)
        self.assertLess(len(listed(card)), 40)

    def test_rows_page_and_a_click_acts_on_its_own_workspace(self):
        self.workspaces()
        for i in range(20):
            (self.root / f'ws-{i:02}').mkdir()
            resolve_selection(self.ctx.state_dir, self.work, str(self.root / f'ws-{i:02}'))
        self.standalone()
        first = listed(self.shown())
        self.assertEqual(len(first), PAGE_SIZE)
        pages = -(-24 // PAGE_SIZE)
        self.assertIn(f'第 1/{pages} 页', self.text())
        self.assertTrue(buttons(self.shown())['page0']['disabled'])  # 上一页 on the first page.
        self.click('page1')
        second = listed(self.shown())
        self.assertIn(f'第 2/{pages} 页', self.text())
        self.assertFalse(set(first) & set(second))
        self.assertEqual(self.record()['manager']['page'], 1)
        # The list moves (a task starts in another workspace) before the click: the row's key still names it.
        target = second[0]
        self.use(kept=0)
        self.act('remove', target)
        self.assertNotIn(target, self.config()['workspaces'])
        self.assertEqual(len(self.config()['workspaces']), 23)
        self.assertIn(f'第 2/{pages} 页', self.text())  # The page is kept.
        # A new search starts at its first page, also when it comes with a page button.
        self.click(f'page{pages - 1}', query='ws-1')
        self.assertEqual(self.record()['manager']['page'], 0)
        self.assertEqual(listed(self.shown()), [f'ws-{i}' for i in range(10, 18)])
        self.click('page1', query='ws-1')
        self.assertEqual(self.record()['manager']['page'], 1)
        self.assertEqual(listed(self.shown()), ['ws-18', 'ws-19'])
        self.click('filter', query='ws-1')
        self.assertEqual(self.record()['manager']['page'], 0)
        # Past the last page (rows removed meanwhile) the card shows the last one.
        self.click('page99', query='ws-1')
        self.assertIn('第 2/2 页', self.text())


class WorkspaceCliTests(unittest.TestCase):
    def test_remove_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, work = root / 'state', root / 'work'
            work.mkdir()
            for name in ('a', 'b'):
                (root / name).mkdir()
                resolve_selection(state, work, str(root / name))
            (root / 'b').rmdir()
            script = Path(__file__).parents[1] / 'bin/covey_workspaces.py'

            def run(*args):
                return subprocess.run([sys.executable, str(script), '--state-dir', str(state), *args],
                                      cwd=work, capture_output=True, text=True)
            self.assertEqual(json.loads(run('prune').stdout)['workspaces'].keys(), {'work', 'a'})
            self.assertEqual(json.loads(run('remove', 'a').stdout)['workspaces'].keys(), {'work'})
            self.assertIn('不能移除', run('remove', 'work').stderr)
            self.assertIn('unknown workspace', run('remove', 'missing').stderr)

    def test_add_names_a_new_directory_and_never_waits_on_a_silent_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, work = root / 'state', root / 'work'
            (root / 'a' / 'work').mkdir(parents=True)
            work.mkdir()
            self.assertEqual(workspaces.add_workspace(state, work, str(root / 'a' / 'work')), ('work-2', True))
            self.assertEqual(workspaces.add_workspace(state, work, str(work)), ('work', False))
            # "~name" of no user: said like any path that is not absolute (pathlib raised RuntimeError, 2026-11-11).
            for typed in ('~$预算表', '~nosuchuser/work'):
                with self.assertRaisesRegex(ValueError, '绝对路径'):
                    workspaces.add_workspace(state, work, typed)
                with self.assertRaisesRegex(ValueError, '绝对目录路径'):
                    workspaces.resolve_selection(state, work, typed)
            release = threading.Event()
            resolve = Path.resolve
            with mock.patch.object(Path, 'resolve', lambda path, *a, **k: release.wait(5) and resolve(path)):
                started = time.monotonic()
                with self.assertRaisesRegex(ValueError, '没有答复'):
                    workspaces.add_workspace(state, work, str(root / 'slow'), timeout=.05)
                self.assertLess(time.monotonic() - started, 2)
                release.set()
            self.assertEqual(set(load_config(state, work)['workspaces']), {'work', 'work-2'})


class DirectoryReadTests(unittest.TestCase):
    def test_shallow_scan_hidden_links_and_bounded_listing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / '.hidden').mkdir()
            (root / 'directory').mkdir()
            (root / 'directory/nested').mkdir()
            (root / 'file').write_text('not read')
            (root / 'link').symlink_to(root / 'absent')
            result = scan_directory(root)
            self.assertEqual([e['name'] for e in result['entries']], ['.hidden', 'directory', 'link'])
            self.assertTrue(result['entries'][-1]['link'])
            self.assertFalse(result['error'])
            self.assertTrue(scan_directory(root / 'link')['error'])
            self.assertTrue(scan_directory(root / 'file')['error'])
            with mock.patch('covey.app.directory_picker.SCAN_LIMIT', 1):
                self.assertTrue(scan_directory(root)['truncated'])
            with mock.patch('covey.app.directory_picker.os.scandir', side_effect=PermissionError):
                self.assertIn('权限', scan_directory(root)['error'])

    def test_search_reads_within_its_directory_and_time_budgets(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for i in range(5):
                (root / f'd{i}' / f'match-{i}').mkdir(parents=True)
            (root / 'match-top').mkdir()
            found = search_directories(root, 'MATCH')
            self.assertEqual([e['name'] for e in found['entries']], ['match-top'] + [f'd{i}/match-{i}' for i in range(5)])
            self.assertEqual((found['truncated'], found['scanned']), (False, 12))
            with mock.patch.object(directory_picker, 'SEARCH_DIRECTORIES', 3):
                found = search_directories(root, 'match')
            self.assertEqual((found['truncated'], found['scanned']), (True, 3))
            self.assertEqual([e['name'] for e in found['entries']], ['match-top', 'd0/match-0', 'd1/match-1'])
            view = {'path': str(root), 'revision': 1, 'query': 'match', 'results': True, **found}
            self.assertIn('只查了前 3 个目录，可进入子目录再搜', json.dumps(directory_card('a' * 24, view), ensure_ascii=False))
            with mock.patch.object(directory_picker, 'SEARCH_SECONDS', -1):
                self.assertTrue(search_directories(root, 'match')['truncated'])
            self.assertIn('不存在', search_directories(root / 'absent', 'x')['error'])
            with mock.patch.object(directory_picker, 'SEARCH_RESULTS', 2):
                found = search_directories(root, 'match')
            self.assertEqual((len(found['entries']), found['more']), (2, True))
            # A search is cached apart from the listing of the same directory.
            cache = DirectoryCache()
            self.assertEqual(len(cache.request(str(root)).result(2)['entries']), 6)
            self.assertEqual(len(cache.request(str(root), query='match-1').result(2)['entries']), 1)
            self.assertEqual(set(cache.cache), {str(root), (str(root), 'match-1')})

    def test_cache_refresh_expiry_and_capacity(self):
        cache = DirectoryCache(ttl=10, capacity=2, stale_ttl=0)
        with mock.patch('covey.app.directory_picker.scan_directory', return_value={'entries': []}) as scan:
            first = cache.request('/a').result(2)
            self.assertIs(cache.request('/a').result(), first)
            self.assertEqual(scan.call_count, 1)
            cache.request('/a', refresh=True).result(2)
            self.assertEqual(scan.call_count, 2)
            cache.ttl = -1
            cache.request('/a').result(2)
            self.assertEqual(scan.call_count, 3)
            cache.request('/b').result(2)
            cache.request('/c').result(2)
            self.assertEqual(len(cache.cache), 2)
            self.assertNotIn('/a', cache.cache)

    def test_recent_listing_returns_immediately_while_refresh_is_bounded(self):
        cache = DirectoryCache(ttl=0, stale_ttl=120)
        first = {'entries': [{'name': 'old', 'link': False}], 'error': ''}
        cache.cache['/a'] = (time.monotonic() - 21, first)
        release, entered = threading.Event(), threading.Event()
        def scan(path):
            entered.set()
            release.wait(3)
            return {'entries': [{'name': 'new', 'link': False}], 'error': ''}
        with mock.patch('covey.app.directory_picker.scan_directory', side_effect=scan) as read:
            try:
                result = cache.request('/a').result(.05)
                self.assertTrue(result['stale'])
                self.assertEqual(result['entries'], first['entries'])
                self.assertTrue(entered.wait(1))
                self.assertTrue(cache.request('/a').result(.05)['stale'])
                self.assertEqual(read.call_count, 1)
                # User-requested freshness waits for the existing reader.
                refresh = cache.request('/a', refresh=True)
                self.assertFalse(refresh.done())
            finally:
                release.set()
            self.assertEqual(refresh.result(2)['entries'][0]['name'], 'new')
            self.assertFalse(refresh.result()['stale'])

    def test_old_listing_and_cached_errors_are_never_served_as_recent(self):
        cache = DirectoryCache(ttl=0, stale_ttl=120)
        for value, age in [({'entries': [], 'error': ''}, 121),
                           ({'entries': [], 'error': 'permission denied'}, 21)]:
            cache.cache['/a'] = (time.monotonic() - age, value)
            with mock.patch('covey.app.directory_picker.scan_directory', return_value={'entries': [], 'error': 'fresh'}) as read:
                result = cache.request('/a').result(2)
                self.assertEqual(result['error'], 'fresh')
                self.assertFalse(result['stale'])
                read.assert_called_once()

    def test_stalled_reads_are_bounded_and_same_path_coalesces(self):
        release = threading.Event()
        cache = DirectoryCache()
        def stalled(path):
            release.wait(3)
            return {'entries': [], 'error': ''}
        with mock.patch('covey.app.directory_picker.scan_directory', side_effect=stalled) as scan:
            try:
                first = cache.request('/a')
                second = cache.request('/b')
                self.assertIs(cache.request('/a'), first)
                self.assertIn('读取', cache.request('/c').result(.1)['error'])
                self.assertLessEqual(scan.call_count, 2)
            finally:
                release.set()
                first.result(2)
                second.result(2)


class PickerTests(PickerCase):
    def enter(self, name):
        index = next(i for i, e in enumerate(self.record()['picker']['entries']) if e['name'] == name)
        self.click('enter', index=index)

    def press(self, verb, **fields):
        self.picker.process(self.ctx, {**self.open_event, 'action_name':
                            f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:{verb}",
                            'form_value': {'query': '', **fields}})

    def row(self, name, verb='pick'):
        """The verb of the button for the row named ``name``: enter<i> or pick<i>."""
        for found in row_buttons(self.current_card(), name):
            if found['name'].rsplit(':', 1)[1].startswith(verb):
                return found['name'].rsplit(':', 1)[1]
        raise AssertionError(f'no row {name}')

    def text(self):
        return json.dumps(self.current_card(), ensure_ascii=False)

    def test_rows_enter_page_and_use_a_row_return_to_the_launch_card(self):
        self.prepare()
        for i in range(45):
            (self.work / f'child-{i:02}').mkdir()
        self.picker.process(self.ctx, self.open_event)
        card = self.current_card()
        self.assertEqual(card['header']['title']['content'], '选择工作区目录')
        form = card['body']['elements'][0]['elements']
        self.assertEqual(form[0]['content'], "📂 **project** <font color='grey'>· 已在列表</font>")  # Listed as project.
        self.assertEqual(form[1]['content'], literal(self.work))
        self.assertEqual(find(card, 'input', 'query')['placeholder']['content'], SEARCH_PLACEHOLDER)
        labels = [b['text']['content'] for b in walk(form) if b.get('tag') == 'button']
        self.assertEqual(labels[:3], ['⬆️ 上一级', '🔄 刷新', '搜索'])
        self.assertEqual(labels[3:5], ['进入', '使用'])
        self.assertEqual([b['name'].rsplit(':', 1)[1] for b in row_buttons(card, 'child-00')], ['enter0', 'pick0'])
        self.assertEqual(labels[-4:], ['上一页', '下一页', '返回', '使用此目录'])
        self.assertEqual(labels.count('使用'), 15)
        self.assertIn('共 45 个子目录', self.text())
        self.assertIn('第 1/3 页', self.text())
        self.assertNotIn(literal('child-20'), self.text())
        self.assertTrue({e.get('flex_mode') for e in walk_dicts(card) if e.get('tag') == 'column_set'} == {None})
        self.press('page2')
        self.assertIn(literal('child-44'), self.text())
        self.assertIn('第 3/3 页', self.text())
        self.assertTrue(buttons(self.current_card())['page2']['disabled'])  # 下一页 on the last page.
        self.press(self.row('child-41', 'enter'))
        self.assertEqual(self.record()['picker']['path'], str(self.work / 'child-41'))
        self.assertIn('没有子目录', self.text())
        self.press('up')
        self.assertEqual(self.record()['picker']['path'], str(self.work))
        self.assertIn('第 1/3 页', self.text())
        self.press(self.row('child-07'))
        self.assertEqual(self.record()['status'], 'replaced')
        self.assertIn('已选择工作区：child&#45;07', self.text())
        self.assertEqual(selects(self.current_card())['workspace']['initial_option'], 'child-07')
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_search_this_level_first_then_deeper_and_paste_a_path(self):
        self.prepare()
        for relative in ('Alpha', 'x/alpha-deep', 'x/y/alpha3', 'x/y/z/alpha4', '.hid/alpha', 'node_modules/alpha'):
            (self.work / relative).mkdir(parents=True)
        (self.work / 'alpha-link').symlink_to(self.work / 'x')
        self.picker.process(self.ctx, self.open_event)
        self.press('search', query=' ALPHA ')
        self.assertEqual([e['name'] for e in self.record()['picker']['entries']],
                         ['Alpha', 'alpha-link', 'x/alpha-deep', 'x/y/alpha3'])
        self.assertIn('搜索“ALPHA”：4 个结果', self.text())
        self.assertEqual(find(self.current_card(), 'input', 'query')['default_value'], 'ALPHA')
        self.assertIn(f'🔗 **{literal("alpha-link")}**', self.text())
        self.assertIn("📁 **alpha3**\\n<font color='grey'>x/y</font>", self.text())  # Where a deeper result is.
        self.assertIn('清除搜索', self.text())
        self.press('search', query='.hid')
        self.assertEqual([e['name'] for e in self.record()['picker']['entries']], ['.hid'])
        self.press('search', query='nothing-like-it')
        self.assertIn('没有匹配的目录', self.text())
        self.press('search', query='alpha')
        self.press(self.row('alpha-deep', 'enter'))
        self.assertEqual(self.record()['picker']['path'], str(self.work / 'x/alpha-deep'))
        self.assertEqual(self.record()['picker']['query'], '')
        self.press('search', query=f'{self.root}/')
        self.assertEqual(self.record()['picker']['path'], str(self.root))
        with mock.patch.dict(os.environ, {'HOME': str(self.work)}):
            self.press('search', query='~/x')
        self.assertEqual(self.record()['picker']['path'], str(self.work / 'x'))
        self.press('search', query=str(self.root / 'nowhere'))
        self.assertIn('路径不存在', self.text())
        self.assertTrue(buttons(self.current_card())['use']['disabled'])
        self.press('up')
        self.press('clear')
        self.assertEqual(self.record()['picker']['path'], str(self.root))
        self.press('use')
        self.assertEqual(selects(self.current_card())['workspace']['initial_option'], self.root.name[:32])

    def test_browser_ignores_stale_rows_and_other_people(self):
        self.prepare()
        (self.work / 'one').mkdir()
        self.picker.process(self.ctx, self.open_event)
        old = {**self.open_event, 'form_value': {'query': ''},
               'action_name': f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:{self.row('one')}"}
        self.press('refresh')
        calls = self.api.call_count
        before = load_config(self.ctx.state_dir, self.work)
        for event in (old, {**old, 'action_name': f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:pick99"},
                      {**old, 'action_name': f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:use",
                       'operator_id': 'other'}):
            self.picker.process(self.ctx, event)
        self.assertEqual((self.api.call_count, load_config(self.ctx.state_dir, self.work)), (calls, before))
        self.assertEqual(self.record()['status'], 'ready')

    def test_root_down_up_and_near_only_update_original_card(self):
        self.prepare().process(self.ctx, self.open_event)
        self.assertEqual(self.record()['picker']['path'], str(self.work))
        self.click('root')
        child = self.root.parts[1]
        self.enter(child)
        self.assertEqual(self.record()['picker']['path'], '/' + child)
        self.click('up')
        self.assertEqual(self.record()['picker']['path'], '/')
        self.click('near')
        self.assertEqual(self.record()['picker']['path'], str(self.root))
        self.enter(self.work.name)
        self.assertEqual(self.record()['picker']['path'], str(self.work))
        self.click('root')
        self.assertTrue(all(call.args[1:3] == ('PUT', '/open-apis/cardkit/v1/cards/existing-card')
                            for call in self.api.call_args_list))
        self.assertEqual([call.args[3]['sequence'] for call in self.api.call_args_list], list(range(5, 12)))
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_callbacks_from_cards_sent_before_the_browser_still_page_and_show_hidden(self):
        self.prepare()
        for i in range(14):
            (self.work / f'child-{i:02}').mkdir()
        (self.work / '.hidden').mkdir()
        self.picker.process(self.ctx, self.open_event)
        self.click('near')
        self.enter(self.work.name)
        card = json.dumps(self.current_card(), ensure_ascii=False)
        self.assertIn(literal('child-00'), card)
        self.assertIn(literal('child-13'), card)
        self.assertNotIn('下一页', card)
        self.assertNotIn(literal('.hidden'), card)
        self.click('page', page=1)
        self.assertIn(literal('child-13'), json.dumps(self.current_card()))
        self.click('hidden')
        self.assertIn(literal('.hidden'), json.dumps(self.current_card()))
        view = {**self.record()['picker'], 'path': '/<at id=all>*abc*'}
        self.assertNotIn('<at', directory_card(self.token, view)['body']['elements'][0]['elements'][0]['content'])

    def test_a_card_sent_before_the_browser_still_filters_and_selects(self):
        self.prepare()
        for i in range(140):
            (self.work / f'child-{i:03}').mkdir()
        self.picker.process(self.ctx, self.open_event)
        def form(verb, **fields):
            return {**self.open_event, 'action_name':
                    f"bridge_dir_{self.token}:{self.record()['picker']['revision']}:{verb}",
                    'form_value': {'location': str(self.work), 'query': '', 'directory': 'current', **fields}}
        self.picker.process(self.ctx, form('filter', query='139'))
        self.assertIn(literal('child-139'), json.dumps(self.current_card()))
        self.assertNotIn(literal('child-001'), json.dumps(self.current_card()))
        index = next(i for i, e in enumerate(self.record()['picker']['entries']) if e['name'] == 'child-139')
        self.picker.process(self.ctx, form('select', directory=str(index), query='139'))
        self.assertEqual(self.record()['status'], 'replaced')
        self.assertEqual(self.api.call_count, 3)  # Open, filter, select; no enter/extra page.
        workspaces = load_config(self.ctx.state_dir, self.work)['workspaces']
        self.assertIn(str(self.work / 'child-139'), workspaces.values())

    def test_card_is_bounded_for_long_unicode_directory_names(self):
        view = {'path': '/' + '😀' * 999, 'revision': 1, 'entries': [
            {'name': str(i) + '😀中文' * 90, 'link': False} for i in range(4000)]}
        for host, listed in ((LAUNCH, {}), (BROWSE_HOST, {'/' + '😀' * 999: 'x' * 32})):
            card = directory_card('a' * 24, view, host, listed)
            self.assertLess(len(json.dumps(card, ensure_ascii=False).encode()), 30000)
            self.assertLessEqual(json.dumps(card).count('"tag"'), 200)
            self.assertIn('第 1/267 页', json.dumps(card, ensure_ascii=False))  # 4000 directories, 15 a page.
        many = {**view, 'path': '/' + 'p' * 900, 'entries': [{'name': '名' * 255, 'link': True}] * 20,
                'notice': '✓ 已添加 ' + 'n' * 32}
        card = json.dumps(directory_card('a' * 24, many, BROWSE_HOST), ensure_ascii=False)
        self.assertLess(len(card.encode()), 30000)
        self.assertLessEqual(card.count('"tag"'), 200)

    @requires_extension()
    def test_select_registers_path_preserves_draft_without_binding(self):
        self.prepare()
        selected = self.root / 'another project'
        selected.mkdir()
        before = read_topic(self.target)
        self.picker.process(self.ctx, self.open_event)
        self.click('near')
        self.enter(selected.name)
        self.click('select')
        self.assertEqual(self.record()['status'], 'replaced')
        fields = list(walk_elements(self.current_card()['body']['elements']))
        shown = selects(self.current_card())
        self.assertEqual([shown['effort']['initial_option'], shown['workspace']['initial_option']], ['1:high', selected.name])
        self.assertEqual(shown['model']['initial_option'], 'maso:maso-model')
        self.assertNotIn('path', [e.get('name') for e in fields])  # No path input since 2026-11-03.
        config = load_config(self.ctx.state_dir, self.work)
        self.assertEqual(config['workspaces'][selected.name], str(selected))
        self.assertEqual(read_topic(self.target), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        replacement = card_token(self.current_card())
        record = json.loads((self.record_path.parent / (replacement + '.json')).read_text())
        self.assertEqual(record['card_id'], 'existing-card')
        self.assertEqual(record['sequence'], self.record()['sequence'])
        self.assertFalse(record['new_topic'])

    @requires_extension()
    def test_cancel_restores_all_draft_fields_without_adding_workspace(self):
        self.prepare()
        self.open_event['form_value'] = json.dumps(self.open_event['form_value'])
        before = load_config(self.ctx.state_dir, self.work)
        self.picker.process(self.ctx, self.open_event)
        self.click('cancel')
        fields = list(walk_elements(self.current_card()['body']['elements']))
        self.assertNotIn('path', [e.get('name') for e in fields])  # No path input since 2026-11-03.
        self.assertEqual(self.record()['draft']['path'], 'draft path')  # A path an older card sent is kept.
        self.assertEqual(selects(self.current_card())['model']['initial_option'], 'maso:maso-model')
        self.assertEqual(load_config(self.ctx.state_dir, self.work), before)

    @requires_extension()
    def test_returned_form_starts_waiting_task_in_selected_harness_and_directory_once(self):
        self.prepare()
        self.receive('source', 'queued request')
        remember_topic_message(self.ctx.state_dir, self.target.topic_id, 'chat', 'card-message')
        selected = self.root / 'chosen'
        selected.mkdir()
        self.picker.process(self.ctx, self.open_event)
        self.click('near')
        self.enter(selected.name)
        self.click('select')
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        event = {**self.open_event, 'action_name': SUBMIT_PREFIX + card_token(self.current_card()), 'form_value': {
            'model': 'maso:maso-model', 'effort': '1:high', 'workspace': selected.name, 'path': '',
            'run_mode': 'direct'}}
        self.app.handle_card_action_event(event, self.ctx)
        self.app.handle_card_action_event(event, self.ctx)
        self.assertEqual(self.app.runtime.jobs.queue.qsize(), 1)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.workdir, job.user_text), ('maso', selected, 'queued request'))

    def test_wrong_owner_chat_source_and_changed_binding_are_rejected(self):
        self.prepare()
        for change in ({'operator_id': 'other'}, {'chat_id': 'other'}, {'message_id': 'other'},
                       {'action_name': PICKER_PREFIX + '../other'}):
            self.picker.process(self.ctx, {**self.open_event, **change})
        self.api.assert_not_called()
        self.app.topics.select_harness(self.target, 'chat', 'claude')
        self.picker.process(self.ctx, self.open_event)
        self.api.assert_not_called()

    def test_duplicates_old_views_and_old_start_cannot_apply(self):
        self.prepare().process(self.ctx, self.open_event)
        self.picker.process(self.ctx, self.open_event)
        self.assertEqual(self.api.call_count, 1)
        old = self.action('near')
        self.picker.process(self.ctx, old)
        self.picker.process(self.ctx, old)
        self.assertEqual(self.api.call_count, 2)
        with self.assertRaisesRegex(ValueError, '最新选择表单'):
            submit_launch_form(self.child, self.target, {**self.open_event, 'action_name': SUBMIT_PREFIX + self.token})

    def test_failed_put_keeps_previous_view_and_retry_uses_new_sequence(self):
        self.prepare().process(self.ctx, self.open_event)
        before = self.record()['picker']
        action = self.action('near')
        self.api.return_value = None
        self.picker.process(self.ctx, action)
        self.assertEqual(self.record()['picker'], before)
        self.api.return_value = {'code': 0}
        self.picker.process(self.ctx, action)
        self.assertEqual(self.record()['picker']['path'], str(self.root))
        self.assertEqual([c.args[3]['sequence'] for c in self.api.call_args_list], [5, 6, 7])

    def test_slow_scan_shows_loading_and_late_result_cannot_overwrite_navigation(self):
        self.prepare()
        slow = Future()
        ready = Future()
        ready.set_result({'entries': [], 'error': ''})
        self.picker.started = True  # The test runs the queued completion itself.
        with mock.patch.object(self.picker.cache, 'request', side_effect=[slow, ready]):
            started = time.monotonic()
            self.picker.process(self.ctx, self.open_event)
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(self.record()['picker']['loading'])
            self.assertIn('正在读取', json.dumps(self.current_card(), ensure_ascii=False))
            self.click('near')
            slow.set_result({'entries': [{'name': 'late', 'link': False}], 'error': ''})
            _, ctx, event = self.picker.queue.get_nowait()
            self.picker.process(ctx, event)
            self.assertEqual(self.record()['picker']['path'], str(self.root))
            self.assertEqual(self.api.call_count, 2)

    def test_async_completion_and_restart_refresh(self):
        self.prepare()
        slow = Future()
        self.picker.started = True  # The test runs the queued completion itself.
        with mock.patch.object(self.picker.cache, 'request', return_value=slow):
            self.picker.process(self.ctx, self.open_event)
            slow.set_result({'entries': [], 'error': ''})
            _, ctx, event = self.picker.queue.get_nowait()
            self.picker.process(ctx, event)
        self.assertFalse(self.record()['picker']['loading'])
        self.picker = DirectoryPicker(self.app)
        self.click('refresh')
        self.assertFalse(self.record()['picker']['error'])

    def test_symlink_file_and_removed_directory_are_recoverable(self):
        self.prepare()
        (self.work / 'link').symlink_to(self.work / 'missing')
        self.picker.process(self.ctx, self.open_event)
        self.click('near')
        self.enter(self.work.name)
        self.enter('link')
        self.assertTrue(self.record()['picker']['error'])
        count = self.api.call_count
        self.click('select')
        self.assertEqual(self.api.call_count, count)
        self.click('up')
        (self.work / 'link').unlink()
        self.work.rmdir()
        self.click('select')
        self.assertIn('不可用', self.record()['picker']['notice'])
        self.assertIn('不可用', json.dumps(self.current_card(), ensure_ascii=False))

    def test_dispatch_does_not_block_message_consumer(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        def slow(ctx, event):
            entered.set()
            release.wait(3)
        with mock.patch.object(self.picker, 'process', side_effect=slow):
            try:
                started = time.monotonic()
                self.app.handle_card_action_event(self.open_event, self.ctx)
                self.assertLess(time.monotonic() - started, .2)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
                self.picker.queue.join()
                self.app.runtime.shutdown.set()

    def test_picker_issued_before_legacy_session_cannot_change_its_workspace(self):
        self.prepare()
        write_session_id(self.target.session_path, 'preserved-session')
        before = read_topic(self.target)
        self.picker.process(self.ctx, self.open_event)
        self.api.assert_not_called()
        self.assertEqual(read_topic(self.target), before)
        self.assertEqual(self.target.session_path.read_text().strip(), 'preserved-session')

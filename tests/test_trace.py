"""The process trace of a task card.

Backend events feed one ToolTrace; notes, tools and child agents render as bounded,
paged panels; live updates touch only the elements that changed; page clicks repaint
the original card in place.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from support import make_job
from support.cards import figures_line, md_text, walk_dicts, panel_title, split_panel, trace_panel
from support.fake_lark import FakeCardKit
from support.world import QuestionFixture, build_claude_app

from covey.app.recovery_cards import recovery_card
from covey.backends.codex.appserver import dispatch_structured_event
from covey.backends.codex.events import CodexEventAdapter, apply_agent_event
from covey.cards.streaming import task_details_panel, task_details_text
from covey.cards.task_figures import figure_cells
from covey.cards.trace import cut_escaped
from covey.cards.trace_updates import incremental_actions, panel_json
from covey.cards.trace_view import (
    FOLD_CHARS, TRACE_ELEMENTS, TraceView, element_count, json_size as json_bytes, text_chunks)
from covey.core.text import safe_text, short_count
from covey.core.jobs import job_path, read_job_record
from covey.core.maintenance import dry_run_report, maintain
from covey.core.migrate import MIGRATIONS, migrate, pending
from covey.core.trace import TRACE_BYTES, ToolTrace, task_trace
from covey.core.trace_store import compress_trace, load_trace, save_embedded, save_trace, trace_path
from covey.feishu.events import CARD_ACTION_EVENT_JQ


def codex_trace():
    """A trace fed the way a live Codex turn feeds it: App Server items through the adapter."""
    trace = ToolTrace()
    return trace, CodexEventAdapter(trace.accept)


class TraceTest(unittest.TestCase):
    def test_a_long_detail_is_never_cut_inside_an_escape(self):
        detail = safe_text("x" * 150 + " p = Path('a') and [b]", None)
        self.assertEqual(cut_escaped(detail, 160), "x" * 150 + " p = Path(…")  # Was "Path(&#x2…" (S25).
        self.assertEqual(cut_escaped(safe_text("y" * 159 + "[", None), 160), "y" * 159 + "…")
        self.assertEqual(cut_escaped("short", 160), "short")

    def test_command_lifecycle_and_replayed_start(self):
        trace, codex = codex_trace()
        item = {"id": "a", "type": "commandExecution", "command": "python -m unittest"}
        codex.item("item/started", item)
        codex.item("item/completed", {**item, "exitCode": 1})
        codex.item("item/started", item)
        self.assertEqual(trace.total, 1)
        self.assertEqual(trace.steps['a']['status'], 'failed')
        self.assertFalse(trace_panel(trace, False)['expanded'])

    def test_bounded_trace_preserves_active_step(self):
        trace, codex = codex_trace()
        codex.item('item/started', {'id': 'active', 'type': 'mcpToolCall'})
        for n in range(60):
            codex.item('item/completed', {'id': str(n), 'type': 'fileChange', 'changes': [{'path': 'a.py'}]})
        self.assertEqual(len(trace.steps), 61)
        self.assertIn('active', trace.steps)
        self.assertEqual(trace.total, 61)
        trace.finish()
        self.assertEqual(trace.steps['active']['status'], 'unconfirmed')

    def test_reasoning_and_tool_output_never_rendered(self):
        trace, codex = codex_trace()
        codex.item('item/completed', {'id':'r', 'type':'reasoning', 'text':'private'})
        self.assertEqual(trace.total, 0)
        codex.item('item/completed', {'id':'x', 'type':'commandExecution', 'command':'echo ok', 'aggregatedOutput':'private'})
        self.assertNotIn('private', str(trace_panel(trace)))
        self.assertNotIn('abc123', safe_text('token=abc123'))
        self.assertNotIn('<img', safe_text('<img>'))

    def test_public_notes_and_plan_survive_tool_completion(self):
        trace, codex = codex_trace()
        trace.note('note-1', '先检查目录，再等待。')
        trace.note('plan-1', '检查目录；等待', '任务计划')
        codex.item('item/started', {'id':'cmd', 'type':'commandExecution', 'command':"python3 - <<'PY'\nprint('ok')"})
        codex.item('item/completed', {'id':'cmd', 'type':'commandExecution', 'command':"python3 - <<'PY'\nprint('ok')", 'exitCode':0})
        trace.note('plan-1', '检查目录已完成；等待已完成', '任务计划')
        trace.finish()
        panel = trace_panel(trace, False)
        self.assertEqual(trace.total, 3)
        self.assertIn('先检查目录', str(panel))
        self.assertIn('检查目录已完成', str(panel))
        self.assertIn("print(&#x27;ok&#x27;)", str(panel))
        # A 进展说明: under a blue heading on a light blue block (the owner, 2026-11-04 18:05: 蓝底), as small as the
        # operations: at 14 px among the 12 px rows it read too large (22:53; 23:05 picked 12 号常规).
        note = panel['elements'][0]
        self.assertEqual((note['tag'], note['background_style']), ('column_set', 'blue-50'))
        self.assertEqual(next(e for e in walk_dicts(note) if e.get('tag') == 'markdown')['text_size'], 'notation')
        self.assertTrue(md_text(note).startswith("<font color='blue'>") and md_text(note).endswith('先检查目录，再等待。'))
        self.assertEqual(panel['header']['title']['tag'], 'markdown')  # The grey collapsible's title.

    def test_backend_duration_and_received_time_survive_ui_delay(self):
        trace, codex = codex_trace()
        item = {'id': 'cmd', 'type': 'commandExecution', 'command': 'sleep 10'}
        codex.item('item/completed', {**item, 'exitCode': 0, 'durationMs': 10040}, received_at=1012)
        self.assertEqual(trace.steps['cmd']['elapsed'], 10.0)
        self.assertEqual(trace.steps['cmd']['ended'], 1012)
        trace.finish()
        text = str(trace_panel(trace, False))
        self.assertIn('sleep 10', text)
        self.assertNotIn('退出码', text)  # ✓ says it ran well; a failure's code is written (2026-11-04 17:38).

    def test_completion_without_command_preserves_started_details(self):
        trace, codex = codex_trace()
        codex.item('item/started', {'id':'cmd', 'type':'commandExecution', 'command':'cat README.md'})
        codex.item('item/completed', {'id':'cmd', 'type':'commandExecution', 'exitCode':0})
        self.assertIn('README.md', str(trace_panel(trace, False)))

    def test_child_lifecycle_and_tools_share_parent_timeline(self):
        trace, codex = codex_trace()
        trace.note('intro', '检查项目')
        codex.agents({'type': 'collabAgentToolCall', 'tool': 'spawnAgent', 'receiverThreadIds': ['child'],
                      'agentPath': '检查配置', 'prompt': '读取配置', 'agentsStates': {'child': {'status': 'running'}}}, 1000)
        codex.child('item/completed', {'threadId': 'child', 'item': {'id':'tool','type':'commandExecution',
                    'command':'cat config.json','exitCode':0,'durationMs':2000}})
        codex.agents({'id':'wait1', 'tool':'wait','receiverThreadIds':['child']})
        panel = trace_panel(trace)
        self.assertEqual(panel['element_id'], 'process_panel')
        nested = next(e for e in panel['elements'] if e['tag']=='collapsible_panel')
        self.assertIn('检查配置', str(nested))
        self.assertIn('执行中', nested['header']['title']['content'])
        self.assertIn('cat config.json', str(panel))
        self.assertIn('子任务 · 检查配置', str(panel))
        self.assertIn('等待子任务', str(panel))
        codex.child('turn/completed', {'threadId': 'child', 'turn': {'status': 'completed'}}, 1002)
        trace.finish()
        nested = next(e for e in trace_panel(trace, False)['elements'] if e['tag']=='collapsible_panel')
        self.assertIn('已完成', nested['header']['title']['content'])
        self.assertFalse(nested['expanded'])

    def test_web_search_with_null_action_starts_then_completes(self):
        trace, codex = codex_trace()
        codex.item('item/started', {'id':'search', 'type':'webSearch', 'action':None})
        codex.item('item/completed', {'id':'search', 'type':'webSearch', 'action':{'query':'GQA'}})
        self.assertEqual(trace.total, 1)
        self.assertEqual(trace.steps['search']['status'], 'done')
        self.assertIn('GQA', str(trace_panel(trace, False)))

    def test_a_bridge_note_keeps_its_title(self):
        trace = ToolTrace()
        self.assertTrue(trace.accept('progress_note', {'id': 'bridge-restart-1', 'title': '机器人',
                                                       'text': '机器人 10:00 重启过，任务未中断。'}))
        self.assertEqual(trace.steps['bridge-restart-1']['title'], '机器人')
        trace.accept('progress_note', {'id': 'n', 'text': '下一步'})
        self.assertEqual(trace.steps['n']['title'], '进展说明')

    def test_progress_failure_isolated_but_input_errors_propagate(self):
        def broken(*args):
            raise AttributeError("invalid event")
        for kind in ('tool','progress_note','agent_state','answer_snapshot','trace_finished'):
            with self.assertLogs('covey.backends.codex.appserver', level='WARNING') as logs:
                dispatch_structured_event(broken, kind, {}, None)
            self.assertIn('AttributeError', logs.output[0])
            self.assertNotIn('invalid event', logs.output[0])
        with self.assertRaises(AttributeError):
            dispatch_structured_event(broken, 'question', {}, None)

    def test_a_note_longer_than_a_card_keeps_its_end_whole_and_its_start_in_the_fold(self):
        trace = ToolTrace()
        trace.route = {'kind':'bridge_trace','topic_id':'topic','job_id':1}
        note = '第一段\n' + '长说明内容。' * 3000 + '说明结尾'
        trace.note('note', note)
        CodexEventAdapter(trace.accept).item('item/completed', {'id':'cmd','type':'commandExecution','command':'prefix ' + 'x'*1000})
        self.assertEqual(trace.steps['note']['detail'], note)
        self.assertGreater(len(trace.steps['cmd']['detail']), 180)
        self.assertNotIn('x'*200, str(trace_panel(trace, False)))
        # One page (2026-11-04): the note's newest chunks whole, its start one line in the fold.
        panel = trace_panel(trace, False)
        body = ''.join(md_text(e) for e in panel['elements'] if e.get('tag') in ('markdown', 'column_set'))
        self.assertIn('长说明内容。' * 100 + '说明结尾', body)
        self.assertIn('第一段', str(next(e for e in panel['elements'] if e.get('element_id') == 'trace_older')))
        wrapped = split_panel(panel)
        self.assertLessEqual(json_bytes(wrapped), TRACE_BYTES)
        self.assertLessEqual(element_count(wrapped), TRACE_ELEMENTS)
        trace.output_tokens=1234
        # No tokens under the title since 2026-11-04 15:07 (the token tile and 任务详情 have them).
        self.assertNotIn('1,234', panel_title(trace_panel(trace, True)))

    def test_note_chunks_fit_their_serialized_size_and_end_at_a_break(self):
        # Escapes and multi-byte characters count as their JSON UTF-8 bytes; a chunk holds at least one
        # character. One that could take another character ends at a line break, a sentence end or a
        # space instead of mid-word.
        text = '说明 "引号" \\ 换行\n😀 ascii\x01' * 300
        for limit in (1, 7, 400, 4000):
            chunks = list(text_chunks(text, limit))
            self.assertEqual(''.join(chunks), text, limit)
            for chunk in chunks:
                self.assertTrue(len(chunk) == 1 or json_bytes(chunk) - 2 <= limit, (limit, chunk))
            for chunk, following in zip(chunks, chunks[1:]):
                if json_bytes(chunk + following[0]) - 2 <= limit:
                    self.assertRegex(chunk, r'[\n 。！？；.!?;:]$', (limit, chunk))

    def test_a_long_note_is_cut_between_words_not_inside_one(self):
        # T182 #1111 read "Not verified in the client (unit te" | "…（续）" | "sts only)".
        sentence = 'Not verified in the client (unit tests only) and the rest of the report follows here. '
        text = sentence * 80 + 'Short tail without a stop ' * 40
        chunks = list(text_chunks(text, 1000))
        self.assertGreater(len(chunks), 2)
        self.assertEqual(''.join(chunks), text)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(('. ', ' ')), chunk[-20:])
        self.assertTrue(chunks[0].endswith('follows here. '))  # A sentence end before a mere space.
        lines = ''.join(f'第 {i} 行，说明内容比较长一些。\n' for i in range(200))
        self.assertTrue(all(chunk.endswith('\n') for chunk in list(text_chunks(lines, 1500))[:-1]))
        # Without a break in reach it still cuts by size, never inside an entity.
        dense = ('x' * 37 + '&amp;') * 60
        for chunk in list(text_chunks(dense, 100))[:-1]:
            self.assertNotRegex(chunk, r'&[a-z]*$', chunk)
        self.assertEqual(list(text_chunks('', 400)), [])


class CompactionStepTest(unittest.TestCase):
    """A context compaction (backends/claude/compaction.py, or Claude Code's own) in 执行过程."""

    def test_it_is_a_line_of_its_own_and_the_status_line_names_it(self):
        from covey.core.status_line import trace_progress
        trace, now = task_trace(), time.time()
        step = {"id": "compact-before-turn", "name": "context_compaction", "status": "running", "started_at": now - 50,
                "detail": "按任务卡上的安排 · 压缩前上下文 62.3 万 token", "public_progress": True}
        trace.accept("tool", step)
        self.assertEqual(trace_progress(trace, now)[0], "压缩上下文 · 按任务卡上的安排 · 压缩前上下文 62.3 万 token")
        trace.accept("tool", {**step, "status": "done", "ended_at": now - 2,
                              "detail": "按任务卡上的安排 · 压缩前 62.3 万 token → 摘要 9,800 token"})
        for i in range(2):
            trace.accept("tool", {"id": f"b{i}", "name": "bash", "status": "done", "started_at": now, "ended_at": now,
                                  "detail": "ls"})
        text = json.dumps(trace_panel(trace, False), ensure_ascii=False)
        self.assertIn("压缩上下文 · 已完成 · 用时 48 秒\\n按任务卡上的安排 · 压缩前 62.3 万 token → 摘要 9,800 token", text)
        self.assertEqual(text.count("**执行命令**"), 2)  # The two commands after it, in full, where they ran.
        self.assertNotIn("最后几项操作", text)  # No block of its own since 2026-11-04 15:07.

    def test_the_context_survives_a_restore(self):
        trace = task_trace()
        trace.accept("token_usage", {"output_tokens": 5, "context_tokens": 623456, "context_window": 1000000})
        restored = ToolTrace.restore(trace.snapshot())
        self.assertEqual((restored.context_tokens, restored.context_window), (623456, 1000000))
        self.assertNotIn("context_tokens", task_trace().snapshot())  # An older snapshot's shape, unchanged.


class CheckpointStepTest(unittest.TestCase):
    """S29 #867: Codex wrote the bridge checkpoint with python heredocs, shown as raw commands right
    under a prompt saying "不要修改任何文件". A step naming this task's checkpoint reads 更新检查点."""
    CP = "/state/harness-cli_x/checkpoints/om_a.json"
    WRITE = ("/bin/bash -lc \"python - <<'PY'\nimport json, os, tempfile\nfrom pathlib import Path\n"
             f"p = Path('{CP}')\nos.replace(tmp, p)\nPY\"")

    def trace(self):
        trace = ToolTrace()
        trace.checkpoint = self.CP
        return trace

    def titles(self, trace):
        return {key: (step["title"], step["detail"]) for key, step in trace.steps.items()}

    def test_codex_commands_and_file_changes(self):
        trace = self.trace()
        codex = CodexEventAdapter(trace.accept)
        rows = [{"id": "w", "type": "commandExecution", "command": self.WRITE},
                {"id": "o", "type": "commandExecution", "command": "cat /state/harness-cli_x/checkpoints/om_ab.json"},
                {"id": "s", "type": "commandExecution", "command": "/bin/bash -lc 'sleep 60'"},
                {"id": "f", "type": "fileChange", "changes": [{"path": self.CP}]},
                {"id": "g", "type": "fileChange", "changes": [{"path": self.CP}, {"path": "/repo/a.py"}]}]
        for row in rows:
            codex.item("item/started", row)
            codex.item("item/completed", {**row, "exitCode": 0})
        titles = self.titles(trace)
        self.assertEqual(titles["w"][0], "更新检查点")
        self.assertIn("os.replace", titles["w"][1])  # A command keeps its body: it could do more than that.
        self.assertEqual(titles["f"], ("更新检查点", ""))
        self.assertEqual(titles["o"][0], "执行命令")  # Another task's checkpoint: not this one.
        self.assertEqual(titles["s"], ("执行命令", safe_text("/bin/bash -lc 'sleep 60'", None)))
        self.assertEqual(titles["g"][0], "修改文件")  # Several files: they stay listed.
        panel = json.dumps(trace_panel(trace, False), ensure_ascii=False)
        self.assertIn("更新检查点", panel)
        restored = ToolTrace.restore(json.loads(json.dumps(trace.snapshot())))
        self.assertEqual(self.titles(restored)["w"][0], "更新检查点")  # Saved records keep it.

    def test_claude_write_edit_and_bash(self):
        from covey.backends.claude.events import ClaudeEventAdapter
        trace = self.trace()
        claude = ClaudeEventAdapter(trace.accept)
        blocks = [{"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": self.CP, "content": "{}"}},
                  {"type": "tool_use", "id": "e", "name": "Edit", "input": {"file_path": self.CP}},
                  {"type": "tool_use", "id": "b", "name": "Bash",
                   "input": {"command": f"cat > {self.CP} <<'EOF'\n{{}}\nEOF", "description": "写入检查点"}},
                  {"type": "tool_use", "id": "r", "name": "Write", "input": {"file_path": "/repo/notes.md"}}]
        claude.accept({"type": "assistant", "message": {"content": blocks}})
        titles = self.titles(trace)
        for key in ("w", "e"):
            self.assertEqual(titles[key], ("更新检查点", ""), key)
        self.assertEqual(titles["b"][0], "更新检查点")
        self.assertIn(self.CP.split("/")[-1].replace("_", "\\_"), titles["b"][1])  # Its command stays visible.
        self.assertEqual(titles["r"], ("写入文件", "/repo/notes.md"))

    def test_the_status_line_names_it_too(self):
        from covey.core.text import step_event, step_label
        trace = self.trace()
        CodexEventAdapter(trace.accept).item("item/started", {"id": "w", "type": "commandExecution", "command": self.WRITE})
        self.assertEqual((step_label(trace.steps["w"]), step_event(trace.steps["w"])), ("更新检查点", "正在更新检查点"))

    def test_without_a_checkpoint_steps_stay_as_they_were(self):
        trace, codex = codex_trace()
        codex.item("item/started", {"id": "w", "type": "commandExecution", "command": self.WRITE})
        self.assertEqual(trace.steps["w"]["title"], "执行命令")


class NestedLifecycleTest(unittest.TestCase):
    def test_late_running_event_cannot_reopen_completed_grandchild(self):
        trace = ToolTrace()
        trace.accept('agent_state', {'id':'a','status':'running','name':'A','started_at':1})
        completed = {'id':'b','parent_id':'a','status':'completed','name':'B','started_at':2,'ended_at':3}
        trace.accept('agent_state', completed)
        self.assertFalse(trace.accept('agent_state', {**completed,'status':'running'}))
        self.assertEqual(trace.children['a'].agents['b']['status'],'completed')

    def test_a_closed_subtask_ends_its_steps(self):
        # Codex closeAgent: the subtask read 已关闭, its operation 执行中 until the turn ended.
        trace = ToolTrace()
        trace.accept("agent_state", {"id": "a", "name": "核对配置", "status": "running", "started_at": 100})
        trace.accept("tool", {"id": "ls", "child_id": "a", "name": "bash", "status": "running", "detail": "ls",
                              "started_at": 101})
        closed = {"id": "a", "name": "核对配置", "status": "shutdown", "started_at": 100, "ended_at": 103}
        trace.accept("agent_state", closed)
        text = str(trace_panel(trace, True))
        self.assertIn("核对配置 · 已关闭", text)
        self.assertNotIn("执行中", text)
        self.assertEqual(trace.children["a"].steps["ls"]["status"], "unconfirmed")
        self.assertFalse(trace.accept("agent_state", {**closed, "status": "running"}))  # Late, not a new run.
        self.assertEqual(trace.agents["a"]["status"], "shutdown")


class DeepTraceTest(unittest.TestCase):
    def test_deep_legacy_trace_render_is_bounded_and_navigable(self):
        trace = ToolTrace()
        parent = None
        for n in range(35):
            sid = str(n)
            trace.accept('agent_state', {'id': sid, 'parent_id': parent, 'name': sid,
                                        'status': 'running', 'started_at': 1000})
            parent = sid
        trace.route = {'kind': 'bridge_trace', 'topic_id': 'topic', 'job_id': 5}
        calls = []
        original = TraceView.panel_model
        def render(view, *args, **kwargs):
            calls.append(view.depth)
            return original(view, *args, **kwargs)
        with patch.object(TraceView, 'panel_model', render):
            panel = trace_panel(trace)
        self.assertLessEqual(len(calls), 2)
        self.assertNotIn('child_id', str(panel))
        for element in panel['elements']:
            if element['tag'] == 'collapsible_panel':
                self.assertFalse(any(e['tag'] == 'button' for e in element['elements']))

class AgentStateTest(unittest.TestCase):
    def test_spawn_tool_completion_is_not_agent_completion(self):
        agents = {}
        apply_agent_event(
            agents,
            {
                "type": "collabAgentToolCall",
                "status": "completed",
                "tool": "spawnAgent",
                "receiverThreadIds": ["child"],
                "agentsStates": {"child": {"status": "running"}},
            },
            now=1,
        )
        self.assertEqual(agents["child"]["status"], "running")
        apply_agent_event(
            agents,
            {
                "type": "collabAgentToolCall",
                "receiverThreadIds": ["child"],
                "agentsStates": {"child": {"status": "completed", "message": "verified"}},
            },
            now=3,
        )
        self.assertEqual(agents["child"]["ended_at"], 3)
        self.assertEqual(agents["child"]["summary"], "verified")

    def test_a_resumed_subtask_runs_again(self):
        # Codex resumeAgent: the subtask's row read 已完成 while it ran again.
        trace = ToolTrace()
        adapter = CodexEventAdapter(lambda kind, data: trace.accept(kind, data))
        collab = {"type": "collabAgentToolCall", "receiverThreadIds": ["a"]}
        adapter.agents({**collab, "tool": "spawnAgent", "agentPath": "/root/a",
                        "agentsStates": {"a": {"status": "running"}}}, 100)
        adapter.child("turn/completed", {"threadId": "a", "turn": {"status": "completed"}}, 102)
        self.assertIn("/root/a · 已完成", str(trace_panel(trace, True)))
        adapter.agents({**collab, "tool": "resumeAgent", "agentsStates": {"a": {"status": "running"}}}, 105)
        self.assertIn("/root/a · 执行中", str(trace_panel(trace, True)))
        self.assertEqual(trace.steps["agent-a"]["started"], 105)
        adapter.child("turn/completed", {"threadId": "a", "turn": {"status": "completed"}}, 107)
        self.assertIn("/root/a · 已完成 · 2.0 秒", str(trace_panel(trace, True)))


class SubtaskLoopTest(unittest.TestCase):
    """T272 #2611 (2026-11-03): Codex named the main thread (/root) a subtask of each subtask that messaged it, and
    subtasks parents of each other; once two held each other every snapshot, so every card update, failed with
    RecursionError (40+ runner logs since 10-31)."""

    def test_the_main_thread_is_no_subtask_and_a_subtask_keeps_the_parent_it_was_first_seen_with(self):
        trace = ToolTrace()
        adapter = CodexEventAdapter(lambda kind, data: trace.accept(kind, data))
        adapter.main_thread = "main"
        spawn = {"type": "collabAgentToolCall", "tool": "spawnAgent"}
        adapter.agents({**spawn, "receiverThreadIds": ["a"], "agentPath": "/root/a", "agentsStates": {"a": {"status": "running"}}}, 1)
        adapter.agents({**spawn, "receiverThreadIds": ["b"], "agentPath": "/root/b", "agentsStates": {"b": {"status": "running"}}}, 2)
        adapter.agents({"type": "collabAgentToolCall", "tool": "sendInput", "receiverThreadIds": ["main"],
                        "agentPath": "/root"}, 3, "a")  # a messages the main thread,
        adapter.agents({"type": "collabAgentToolCall", "tool": "wait", "receiverThreadIds": ["a"]}, 4, "b")  # b waits on a,
        adapter.agents({"type": "collabAgentToolCall", "tool": "wait", "receiverThreadIds": ["b"]}, 5, "a")  # a on b,
        adapter.agents({**spawn, "receiverThreadIds": ["c"], "agentPath": "/root/a/c"}, 6, "a")  # a spawns c.
        self.assertNotIn("main", adapter.agent_states)
        self.assertEqual({sid: state.get("parent_id") for sid, state in adapter.agent_states.items()},
                         {"a": None, "b": None, "c": "a"})
        snapshot = trace.snapshot()
        self.assertEqual(set(snapshot["agents"]), {"a", "b"})
        self.assertEqual(set(snapshot["children"]["a"]["agents"]), {"c"})
        self.assertNotIn("/root", json.dumps(snapshot, ensure_ascii=False).replace("/root/", ""))

    def test_a_link_that_would_make_a_loop_is_left_out(self):
        trace = ToolTrace()
        for sid, parent in (("a", None), ("b", "a"), ("c", "b"), ("a", "c")):  # The last would put a under its own c.
            trace.accept("agent_state", {"id": sid, "name": sid, "status": "running", "started_at": 1,
                                         **({"parent_id": parent} if parent else {})})
        restored = ToolTrace.restore(json.loads(json.dumps(trace.snapshot())))
        self.assertEqual(set(restored.snapshot()["children"]["a"]["children"]["b"]["children"]), {"c"})
        trace_panel(trace, True)  # Draws, as every card update does.
        self.assertTrue(trace.children["a"].holds(trace.children["c"]))
        self.assertFalse(trace.children["c"].holds(trace.children["a"]))


class StoppedStepsTest(unittest.TestCase):
    """S28 #850: after 停止, the command still running read "? … · 执行命令 · 结束状态未确认"."""

    def trace(self, *, public=False):
        trace = ToolTrace()
        trace.accept("tool", {"id": "sleep", "name": "bash", "status": "running", "detail": "sleep 60",
                              "started_at": 1000, **({"public_progress": True} if public else {})})
        trace.accept("tool", {"id": "ls", "name": "bash", "status": "done", "detail": "ls", "started_at": 1001,
                              "ended_at": 1002})
        trace.accept("agent_state", {"id": "child", "name": "核对配置", "status": "running", "started_at": 995})
        return trace

    def test_open_steps_of_a_stopped_task_read_stopped(self):
        trace = self.trace()
        trace.finish(stopped=True)
        text = str(trace_panel(trace, False))
        for shown in ("⏹ ", "**执行命令** <font color='grey'>· 已停止", "核对配置 · 已停止"):
            self.assertIn(shown, text)
        self.assertNotIn("未确认", text)
        self.assertEqual(trace.steps["sleep"]["status"], "unconfirmed")  # The stored status earlier releases draw.
        self.assertEqual(str(trace_panel(ToolTrace.restore(json.loads(json.dumps(trace.snapshot()))), False)), text)

    def test_open_steps_without_a_stop_stay_unconfirmed(self):
        trace = self.trace()
        trace.finish()
        text = str(trace_panel(trace, False))
        self.assertIn("**执行命令** <font color='grey'>· 结束状态未确认", text)
        self.assertNotIn("已停止", text)

    def test_a_stopped_card_s_last_operations_say_stopped(self):
        trace = self.trace(public=True)
        self.assertIn("**执行命令** <font color='grey'>· 执行中", str(trace_panel(trace, True)))
        trace.finish(stopped=True)
        text = str(trace_panel(trace, False))
        self.assertNotIn("最后几项操作", text)
        self.assertIn("**执行命令** <font color='grey'>· 已停止", text)

    def test_a_finished_turn_leaves_no_subtask_step_running(self):
        # A subtask's own steps, and a subtask under it, were left 执行中 on the ended card and in traces/.
        for stopped, shown in ((False, "结束状态未确认"), (True, "已停止")):
            trace = self.trace()
            trace.accept("tool", {"id": "grep", "child_id": "child", "name": "grep", "status": "running",
                                  "detail": "TODO", "started_at": 1003})
            trace.accept("agent_state", {"id": "grandchild", "parent_id": "child", "name": "查日志",
                                         "status": "running", "started_at": 1004})
            trace.accept("tool", {"id": "tail", "child_id": "grandchild", "name": "bash", "status": "running",
                                  "detail": "tail log", "started_at": 1005})
            if stopped:
                trace.finish(stopped=True)
            else:
                trace.accept("trace_finished", {})
            snapshot = json.loads(json.dumps(trace.snapshot()))
            for text in (str(trace_panel(trace, False)), str(trace_panel(ToolTrace.restore(snapshot), False))):
                self.assertNotIn("执行中", text)
                for name in ("搜索文件", "tail log", "查日志"):  # Each drawn.
                    self.assertIn(name, text)
                self.assertIn(shown, text)
            child = snapshot["children"]["child"]
            self.assertEqual(child["steps"]["grep"]["status"], "unconfirmed")
            self.assertEqual(child["children"]["grandchild"]["steps"]["tail"]["status"], "unconfirmed")
            self.assertEqual(child["ended_at"], snapshot["ended_at"])
            self.assertEqual(child["agents"]["grandchild"]["status"], "running")  # Agents keep their states.

    def test_a_record_says_whether_its_task_was_stopped(self):
        # Also traces saved before this release: the record's status says how the task ended.
        from covey.core.trace import finish_record_trace
        snapshot = self.trace().snapshot()
        for status, shown in (("cancelled", "执行命令** <font color='grey'>· 已停止"), ("interrupted", "执行命令** <font color='grey'>· 结束状态未确认")):
            restored = finish_record_trace(ToolTrace.restore(snapshot), {"status": status, "elapsed_seconds": 5})
            self.assertIn(shown, str(trace_panel(restored, False)))

    def test_a_continued_task_s_trace_is_finished_when_paged(self):
        # continued (a waiting_user task its answer went on from) has no later status: like waiting_user,
        # its pages never show a step still 执行中.
        from covey.app.interaction import Interaction
        record, shown = {"job_id": 7, "status": "continued", "elapsed_seconds": 5}, Mock()
        app = SimpleNamespace(harness=SimpleNamespace(topic_record=lambda *a: record),
                              cards=SimpleNamespace(update_record_panel=shown), runtime=SimpleNamespace(log=Mock()))
        ctx = SimpleNamespace(state_dir=Path("unused"), topic_id="topic")
        with patch("covey.app.interaction.load_trace", return_value=self.trace().snapshot()):
            Interaction.show_trace_page(app, ctx, "chat", "mid", {}, {"job_id": 7}, {})
        text = str(shown.call_args.args[5])
        self.assertIn("执行命令** <font color='grey'>· 结束状态未确认", text)
        self.assertNotIn("执行中", text)

    def test_a_page_that_does_not_exist_is_noted_above_the_records(self):
        from covey.app.interaction import Interaction
        record, shown = {"job_id": 7, "status": "done", "elapsed_seconds": 5}, Mock()
        app = SimpleNamespace(harness=SimpleNamespace(topic_record=lambda *a: record),
                              cards=SimpleNamespace(update_record_panel=shown), runtime=SimpleNamespace(log=Mock()))
        ctx = SimpleNamespace(state_dir=Path("unused"), topic_id="topic")
        with patch("covey.app.interaction.load_trace", return_value=self.trace().snapshot()):
            Interaction.show_trace_page(app, ctx, "chat", "mid", {"input_value": "9"},
                                        {"job_id": 7, "verb": "jump", "page_count": 3}, {})
        panel = shown.call_args.args[5]
        self.assertEqual(panel["element_id"], "process_panel")
        details = panel["columns"][0]["elements"][0]
        self.assertEqual([e["element_id"] for e in details["elements"]], ["trace_page_notice", "trace_body"])
        self.assertIn("不是有效页码", details["elements"][0]["content"])

    def test_a_turn_closed_by_a_stop_finishes_its_trace_stopped(self):
        # Codex and dsh send trace_finished as the turn closes, before the worker has the outcome.
        from covey.app.interaction import Interaction
        from support import make_job
        for cancelled in (True, False):
            job = make_job(tool_trace=self.trace())
            if cancelled:
                job.cancel_event.set()
            app = SimpleNamespace(cards=SimpleNamespace(queue_trace=lambda *a: None))
            Interaction._dispatch_structured_event(app, SimpleNamespace(), job, "trace_finished", {}, None)
            self.assertIs(job.tool_trace.stopped, cancelled)


class OperationGroupsTest(unittest.TestCase):
    def test_public_note_markdown_is_readable_without_active_html_or_secrets(self):
        trace = ToolTrace()
        trace.note('public', '**Evidence** in `trial.py` <at id=all> token=TEST_SECRET')
        detail = trace.steps['public']['detail']
        self.assertIn('**Evidence** in `trial.py`', detail)
        self.assertNotIn('<at', detail)
        self.assertNotIn('TEST_SECRET', detail)

    def tool(self, trace, key, status="done", **extra):
        trace.accept("tool", {"id": key, "name": "bash", "status": status,
                              "detail": f"echo {key}", "started_at": 1000,
                              **({"ended_at": 1002, "exit_code": 0} if status == "done" else {}), **extra})

    def groups(self, panel):
        return [e for e in walk_dicts(panel) if e.get("element_id", "").startswith("ops_")]

    def test_a_command_reads_as_its_purpose_or_without_its_wrapper_trimming_and_path_heads(self):
        # The 2026-11-02 review: "R=/data/…/03-t290.png | tail -1 | cut -c1-60", "/bin/bash -lc 'timeout …".
        trace = task_trace()
        trace.public_progress = True
        claude = "截图话题 · R=/data/runs/x/shots/03.png | tail -1 | cut -c1-60"
        codex = "/bin/bash -lc 'timeout 125s python3 -B /data/repo/scripts/run.py --x 1 2>&1 | tail -20'"
        for key, detail in (("c", claude), ("x", codex), ("u", "curl -s https://code.example.com/a/b/c/d/e")):
            trace.accept("tool", {"id": key, "name": "bash", "status": "running", "detail": detail, "started_at": 1000,
                                  "public_progress": True})
        text = json.dumps(trace_panel(trace, True), ensure_ascii=False)
        self.assertIn("<font color='grey'>截图话题", text)
        self.assertIn("timeout 125s python3 -B …/scripts/run.py --x 1", text)
        self.assertIn("https://code.example.com/a/b/c/d/e", text)
        for gone in ("/data", "tail -", "cut -c", "bash -lc", "2&gt;&amp;1"):
            self.assertNotIn(gone, text)
        # The step keeps the whole command: core/text.py command_kind reads its kind from it.
        self.assertIn("cut -c1-60", trace.steps["c"]["detail"])

    def test_an_ended_turn_without_records_has_nothing_to_unfold(self):
        # T320: "执行过程 · 无执行记录 · 用时 6 秒" over an empty 查看过程记录.
        running, ended = task_trace(), task_trace()
        ended.finish()
        live, done = split_panel(trace_panel(running, True)), split_panel(trace_panel(ended, False))
        details = done["columns"][0]["elements"][0]  # Its title and figures as a line, nothing to unfold.
        self.assertEqual((details["tag"], details["element_id"]), ("markdown", "trace_details"))
        self.assertIn("无执行记录", details["content"])
        actions = incremental_actions(live, done)
        self.assertEqual([a["params"]["element_id"] for a in actions], ["trace_details"])  # The figures are inside it.
        self.assertEqual(incremental_actions(done, done), [])

    def test_the_turn_duration_appears_only_once_the_turn_ends(self):
        trace = ToolTrace()
        self.tool(trace, "a")
        # While running, the status line's 已运行 is the one duration on the card.
        self.assertNotIn("用时", panel_title(trace_panel(trace)).split("\n")[0])
        self.assertNotIn("用时", panel_title(trace_panel(ToolTrace())))
        trace.finish()
        # Ended, the status line says 用时 (2026-11-02 review: the title said it again); an interrupted task's
        # card, without that line, asks for it (``timed``).
        self.assertNotIn("用时", panel_title(trace_panel(trace, False)))
        self.assertIn("用时", panel_title(trace_panel(trace, False, timed=True)).split("\n")[0])
        self.assertIn("用时", panel_title(trace_panel(ToolTrace(), False, timed=True)))

    def test_stats_name_total_time_and_model_generation_time_separately(self):
        trace = ToolTrace()
        self.tool(trace, "a")
        trace.generation_seconds, trace.measured_output_tokens, trace.output_tokens = 12.5, 500, 500
        trace.finish()
        # Ended: the title is the rows; the token tile has the tokens, 任务详情 the speed and the other figures
        # (2026-11-04 15:07: the token line was in several places), the status line and the 用时 tile the time.
        first = panel_title(trace_panel(trace, False))
        self.assertEqual(first, "执行过程 · 1 步")
        second = figures_line(trace.snapshot())
        self.assertIn("模型生成 12.5 秒", second)
        self.assertIn("40.0 token/s", second)

    def test_the_tokens_and_speed_are_in_the_details_not_under_the_title(self):
        # T288: under every finished card, 输入 … · 输出 … · 模型生成 … · 平均 … · TTFT … was most of what it said;
        # since 2026-11-04 15:07 (the owner: 输入 … token（缓存命中 …） · 输出 … · token/s，在好几个地方都有) the title
        # has none of it: the token tile has the total, 任务详情 the exact counts on one line and the speed.
        trace = task_trace()
        self.tool(trace, "a")
        trace.accept("token_usage", {"input_tokens": 73800, "cache_read_tokens": 50000, "cache_write_tokens": 0,
                                     "output_tokens": 436, "measured_output_tokens": 436, "generation_seconds": 7.2})
        trace.accept("first_token", {"ttft_seconds": 1.5, "ttft_source": "codex_delta"})
        running = panel_title(trace_panel(trace, True)).split("\n")
        self.assertEqual(running, ["执行过程 · 1 步"])
        trace.finish()
        self.assertEqual(panel_title(trace_panel(trace, False)).split("\n"), ["执行过程 · 1 步"])
        # Since 2026-11-09 (the owner: 和上面重复了，只保留上面的) the counts, 模型生成 and token/s are the 输入/输出
        # box's alone: 任务详情 keeps the TTFT, which the box lacks.
        details = task_details_panel(SimpleNamespace(job_id=1, model="m", timeout=0), trace.snapshot())
        self.assertEqual(details["elements"][0]["content"], "TTFT 1.50 秒")
        # With subtasks the figures are the main task's, and say so; an older snapshot still renders.
        trace.accept("subtask_usage", {"agents": {"a": {"name": "子", "level": 1, "input_tokens": 1,
                                                        "output_tokens": 1}}, "unreported": 0, "pending": 0})
        self.assertEqual(figures_line(trace.snapshot()), "主任务 模型生成 7.2 秒 · 平均 60.6 token/s · TTFT 1.50 秒")
        old = trace.snapshot()
        for key in ("subtask_usage", "generation_seconds", "measured_output_tokens", "ttft_seconds"):
            old.pop(key, None)
        self.assertIsNone(task_details_panel(SimpleNamespace(job_id=1, model="m", timeout=0), old))
        self.assertEqual(task_details_text(SimpleNamespace(job_id=1, model="m", timeout=0), trace.snapshot()).split("\n")[-1],
                         "主任务 TTFT 1.50 秒")

    def test_notes_stay_whole_and_a_short_run_shows_every_operation_where_it_ran(self):
        # The owner, 2026-11-04 15:07: 正常的进展说明和工具命令详情…最后几项操作就不用单列.
        trace = ToolTrace()
        trace.note("intro", "先核验\n然后运行", received_at=999)
        for n in range(4):
            self.tool(trace, f"a{n}", "failed" if n == 1 else "done", started_at=1000 + n,
                      **({"exit_code": 1} if n == 1 else {}))
        trace.note("next", "继续核验", received_at=1010)
        for n in range(5):
            self.tool(trace, f"b{n}", "running" if n == 4 else "done", started_at=1011 + n)
        elements = trace_panel(trace)["elements"]
        self.assertTrue(md_text(elements[0]).endswith("先核验\n然后运行"))
        # a0–a3 in full between the notes, b0–b4 at the end, each where it ran: no 最近操作 of its own.
        self.assertEqual([e["tag"] for e in elements], ["column_set", "markdown", "column_set", "markdown"])
        self.assertEqual([elements[1]["content"].count(f"echo a{n}") for n in range(4)], [1] * 4)
        self.assertIn("<font color='red'>✕</font> **执行命令**", elements[1]["content"])
        self.assertTrue(md_text(elements[2]).endswith("继续核验"))
        recent = elements[3]["content"]
        self.assertEqual([recent.count(f"echo b{n}") for n in range(5)], [1] * 5)
        self.assertNotIn("最近操作", str(elements))
        self.assertIn("<font color='blue'>●</font> **执行命令** <font color='grey'>· 执行中 · ", recent)
        self.assertRegex(recent, r"\*\*执行命令\*\* <font color='grey'>· 执行中 · (\d\d-\d\d )?\d\d:\d\d:\d\d 开始</font>")
        self.assertNotIn("→", recent)  # One time a row: its end, or a running one's start (2026-11-04).

    def test_a_checkpoint_write_shows_no_command_and_a_run_of_eight_is_not_folded(self):
        # The owner, 2026-11-04 17:38: 前 3 项操作：执行命令 3 — 这个是干啥的 (a run of 6 folded 3).
        trace = ToolTrace()
        trace.checkpoint = "/state/checkpoints/cp.json"
        trace.note("intro", "开始", received_at=999)
        trace.accept("tool", {"id": "cp", "name": "bash", "status": "done", "started_at": 1000, "ended_at": 1001,
                              "exit_code": 0, "detail": "timeout 10s python3 - <<'PY' open('/state/checkpoints/cp.json') PY"})
        trace.accept("tool", {"id": "cp2", "name": "bash", "status": "done", "started_at": 1001, "ended_at": 1001.5,
                              "detail": "Save evidence and checkpoint · cat > /state/checkpoints/cp.json"})
        for n in range(6):
            self.tool(trace, f"d{n}", started_at=1002 + n)
        elements = trace_panel(trace)["elements"]
        self.assertEqual([e["tag"] for e in elements], ["column_set", "markdown"])  # Eight rows, no fold.
        rows = elements[1]["content"]
        self.assertEqual(rows.count("**更新检查点**"), 2)
        self.assertNotIn("python3", rows)
        self.assertNotIn("cat &gt;", rows)
        self.assertIn("Save evidence and checkpoint", rows)  # What else it did, in Claude's words.
        self.assertNotIn("退出码", rows)

    def test_a_long_run_shows_its_last_three_and_folds_the_others(self):
        trace = ToolTrace()
        trace.note("intro", "开始", received_at=999)
        for n in range(9):
            self.tool(trace, f"c{n}", "failed" if n == 2 else "done", started_at=1000 + n)
        elements = trace_panel(trace)["elements"]
        fold = elements[1]
        self.assertEqual((fold["tag"], fold["expanded"]), ("collapsible_panel", False))
        self.assertTrue(fold["element_id"].startswith("ops_"))
        self.assertEqual(fold["header"]["title"]["content"], "<font color='grey'>收起了更早的 6 项操作：执行命令 6，1 项失败</font>")
        inside = "".join(e["content"] for e in fold["elements"])
        self.assertEqual([inside.count(f"echo c{n}") for n in range(9)], [1] * 6 + [0] * 3)
        self.assertEqual([elements[2]["content"].count(f"echo c{n}") for n in range(9)], [0] * 6 + [1] * 3)
        # The fold's ID comes from the run's first operation: it stays open while the run grows.
        self.tool(trace, "c9", started_at=1009)
        self.assertEqual(trace_panel(trace)["elements"][1]["element_id"], fold["element_id"])

    def test_a_sub_task_s_operations_say_whose_and_come_in_time_order(self):
        trace = ToolTrace()
        self.tool(trace, "main")
        trace.accept("agent_state", {"id": "child", "name": "核验链接", "status": "running", "started_at": 1001})
        self.tool(trace, "child-tool", child_id="child", started_at=1002)
        trace.accept("progress_note", {"id": "n", "child_id": "child", "text": "子任务进展全文", "received_at": 1003})
        self.tool(trace, "main-after", started_at=1004)
        panel = trace_panel(trace)
        texts = [json.dumps(e, ensure_ascii=False) for e in panel["elements"]]
        order = [next(i for i, t in enumerate(texts) if key in t) for key in
                 ("echo main", "核验链接", "echo child-tool", "子任务进展全文", "echo main-after")]
        self.assertEqual(order, sorted(order))  # The newest of either at the bottom (the owner: SubAgent得兼顾).
        child_row = next(e["content"] for e in panel["elements"] if "echo child-tool" in e.get("content", ""))
        self.assertIn("子任务 · 核验链接 · ", child_row)
        self.assertNotIn("子任务", panel["elements"][-1]["content"])

    def test_many_operations_before_a_plan_fold_and_the_plan_stays_whole(self):
        trace = ToolTrace()
        trace.route = {"kind": "bridge_trace", "topic_id": "topic", "job_id": 1}
        for n in range(100):
            self.tool(trace, f"tool-{n:03}", started_at=1000 + n)
        plan = "阶段说明\n" * 600
        trace.note("plan", plan, "任务计划", received_at=1105)
        trace.output_tokens = 1234
        trace.finish()
        panel = trace_panel(trace, False)
        self.assertNotIn("1,234", panel_title(panel))  # The token tile and 任务详情 have it (2026-11-04 15:07).
        body = "".join(e.get("content", "") for e in walk_dicts(panel) if e.get("tag") == "markdown")
        # No room for 97 operations' details next to the plan: their run keeps its count and its last three.
        self.assertIn("· 更早的 97 项操作：执行命令 97", body)
        self.assertEqual(sum(body.count(f"echo tool-{n:03}") for n in range(100)), 3)
        self.assertIn(plan, body)

    def test_the_oldest_runs_lose_their_details_first(self):
        trace = ToolTrace()
        trace.route = {"kind": "bridge_trace", "topic_id": "topic", "job_id": 1}
        for phase in range(12):
            trace.note(f"n{phase}", f"阶段 {phase} 开始", received_at=1000 + phase * 100)
            for n in range(30):
                trace.accept("tool", {"id": f"t{phase}-{n}", "name": "bash", "status": "done", "started_at": 1001 + phase * 100 + n,
                                      "ended_at": 1002 + phase * 100 + n, "detail": f"run phase {phase} step {n} " + "x" * 60})
        panel = trace_panel(trace, False)
        text = json.dumps(panel, ensure_ascii=False)
        self.assertLessEqual(json_bytes(split_panel(panel)), TRACE_BYTES)
        self.assertNotIn("run phase 0 step 0 ", text)  # The oldest run: its count line only.
        self.assertIn("30 项操作：执行命令 30", text)
        self.assertIn("run phase 11 step 29 ", text)  # The newest run's last ones in full, where they ran,
        self.assertIn("更早的 27 项操作：执行命令 27", text)  # its earlier ones as their count, the run shrunk last,
        self.assertNotIn("最近操作", text)  # and no block of the newest operations of its own.
        self.assertEqual([f"阶段 {p} 开始" in text for p in range(12)], [True] * 12)  # Every note kept.

    def test_the_panel_is_one_boxed_collapsible_whose_count_is_inside_while_running(self):
        # The A 仪表盘 sample (2026-11-04): 执行过程 · N 步 on a grey block. A sent collapsible's title changes only
        # with the whole collapsible, which would undo the user's open or closed: running, it says 进行中.
        trace = ToolTrace()
        trace.accept("tool", {"id": "ls", "name": "bash", "status": "done", "detail": "ls -l", "started_at": 1000,
                              "ended_at": 1001.2})
        trace.accept("tool", {"id": "wc", "name": "bash", "status": "running", "detail": "wc -l a.py", "started_at": 1002})
        trace.output_tokens = 2184
        live = split_panel(trace_panel(trace, True))
        (details,) = live["columns"][0]["elements"]
        self.assertEqual((details["element_id"], "background_color" in details, details["border"]),
                         ("trace_details", False, {"color": "grey", "corner_radius": "8px"}))
        self.assertEqual(details["header"]["title"]["content"], "**执行过程**　<font color='grey'>进行中</font>")
        self.assertEqual(details["elements"][0]["element_id"], "trace_metrics")
        self.assertEqual(details["elements"][0]["content"], "<font color='grey'>2 步</font>")  # No token line.
        text = json.dumps(details, ensure_ascii=False)
        self.assertIn("<font color='green'>✓</font> **执行命令** <font color='grey'>· 1.2 秒 · ", text)
        self.assertIn("<font color='blue'>●</font> **执行命令** <font color='grey'>· 执行中 · ", text)
        self.assertIn("<font color='grey'>wc -l a.py</font>", text)
        self.assertNotIn("最近操作", text)  # The newest operations are at the end where they ran (2026-11-04 15:07).
        trace.accept("tool", {"id": "ls2", "name": "bash", "status": "done", "detail": "ls", "started_at": 1003, "ended_at": 1004})
        later = split_panel(trace_panel(trace, True))
        # The count and the steps change by their IDs; the collapsible, which holds the user's open or closed, never.
        self.assertNotIn("trace_details", [a["params"].get("element_id") for a in incremental_actions(live, later)])
        trace.finish()
        ended = split_panel(trace_panel(trace, False))["columns"][0]["elements"][0]
        self.assertEqual((ended["header"]["title"]["content"], ended["expanded"]), ("**执行过程**　<font color='grey'>3 步</font>", False))

    def test_an_operation_s_row_follows_its_completion(self):
        trace = ToolTrace()
        self.tool(trace, "a", "running")
        self.assertIn("<font color='blue'>●</font>", trace_panel(trace)["elements"][-1]["content"])
        self.tool(trace, "a", "done")
        recent = trace_panel(trace)["elements"][-1]["content"]
        self.assertIn("<font color='green'>✓</font>", recent)
        self.assertEqual(recent.count("echo a"), 1)


class ReadableRecordTest(unittest.TestCase):
    """The 2026-10-27 UI review of T182 #1111's process record (P2, P6, P7)."""

    def texts(self, panel):
        return [e["content"] for e in walk_dicts(panel) if e.get("tag") == "markdown"]

    def subtask(self, notes, name="Build trial card", started=None):
        trace = task_trace()
        at = time.time() - 60 if started is None else started
        trace.accept("tool", {"id": "a1", "name": "agent", "detail": "Agent", "status": "running", "started_at": at})
        trace.accept("agent_state", {"id": "a1", "name": name, "status": "running", "started_at": at,
                                     "parent_id": None, "background": False})
        for i, text in enumerate(notes):
            if text.startswith("$ "):
                trace.accept("tool", {"id": f"c{i}", "name": "bash", "detail": text[2:], "status": "done",
                                      "child_id": "a1", "started_at": at + i, "ended_at": at + i + 1})
            else:
                trace.accept("progress_note", {"id": f"n{i}", "text": text, "child_id": "a1", "received_at": at + i})
        return trace

    def test_code_in_a_note_reads_as_written(self):
        # T182 #1111 read "It is &lt;state&gt;/card-style.json … such as &quot;T182&quot;".
        trace = ToolTrace()
        note = ('It is `<state>/card-style.json`, such as `"T182"` and `a & b`; outside: <b>x</b> & "q".\n'
                '```json\n{"all": true, "tag": "<font color=\'red\'>x</font>"}\n```\n`<at id=all>`')
        trace.note("n", note)
        self.assertIn("&lt;state&gt;", trace.steps["n"]["detail"])  # Stored as before: older releases read it.
        text = "".join(self.texts(trace_panel(trace, False)))
        self.assertIn("`<\u200bstate>/card-style.json`", text)
        self.assertIn('`"T182"`', text)
        self.assertIn("`a & b`", text)
        self.assertIn('{"all": true, "tag": "<\u200bfont color=\'red\'>x<\u200b/font>"}', text)
        self.assertIn("outside: &lt;b&gt;x&lt;/b&gt; &amp; &quot;q&quot;", text)  # Not code: escaped as before.
        self.assertNotRegex(re.sub(r"</?font[^>]*>", "", text), r"<(?!\u200b)")  # No tag but the panel's own grey can start, in code or not.
        self.assertNotIn("&quot;T182", text)

    def test_a_sub_task_s_long_last_note_shows_its_start_and_folds_the_rest(self):
        # T182 #1111: a final report of thousands of characters took about 2,000 px of the card.
        report = "\n".join(f"Line {i}: the trial card shows a narrative of the task." for i in range(120))
        trace = self.subtask(["short interim note", report])
        panel = trace_panel(trace, False)
        texts = self.texts(panel)
        self.assertTrue(any(t.endswith("\nshort interim note") for t in texts))
        head = next(t for t in texts if "Line 0:" in t).split("\n", 1)[1]  # Under its heading line.
        self.assertLessEqual(len(head), FOLD_CHARS)
        self.assertTrue(head.endswith("\n"))  # Cut at a line break.
        rest = [e for e in walk_dicts(panel) if e.get("element_id", "").startswith("rest_")]
        self.assertTrue(rest)
        self.assertFalse(rest[0]["expanded"])
        self.assertRegex(rest[0]["header"]["title"]["content"], r"^进展说明其余部分 · 约 [\d,]+ 字$")
        self.assertEqual(head + "".join(e["elements"][0]["content"] for e in rest), report)
        main = ToolTrace()  # The main task's notes keep their full text, folded or not.
        main.note("m", report)
        self.assertFalse([e for e in walk_dicts(trace_panel(main, False)) if e.get("element_id", "").startswith("rest_")])
        earlier = self.subtask([report, "the last one"])  # Only the last note of a sub-task folds.
        self.assertFalse([e for e in walk_dicts(trace_panel(earlier, False)) if e.get("element_id", "").startswith("rest_")])

    def test_every_sibling_sub_task_s_long_last_note_folds(self):
        # The last-note lookup was keyed by id() of a sub-task's view, made during the walk and freed after
        # it: a later sibling's view at a reused address read the freed one's key (26 of 60 did not fold).
        now = time.time()
        agents, spawns, children = {}, {}, {}
        for i in range(60):
            sid = f"k{i}"
            agents[sid] = {"name": f"子{i}", "status": "completed", "started_at": now - 500 + i}
            spawns[f"spawn{i}"] = {"title": "派生", "status": "done", "started": now - 500 + i, "detail": "",
                                   "agent_id": sid}
            children[sid] = {"steps": {
                f"first{i}": {"title": "进展说明", "status": "note", "started": now - 499 + i, "detail": "开始"},
                f"last{i}": {"title": "子任务说明", "status": "note", "started": now - 498 + i, "detail": "报告。" * 300}},
                "started_at": now - 600, "ended_at": None, "agents": {}, "children": {}}
        root = {"steps": spawns, "started_at": now - 600, "ended_at": None, "agents": agents, "children": children}
        _, _, notes, _ = TraceView(root)._trace_lines()
        self.assertEqual([n["fold"] for n in notes.values() if n["title"] == "子任务说明"], [True] * 60)
        # Which addresses get reused depends on the allocator; every view at one address is the worst case.
        same = lambda obj: 1 if isinstance(obj, TraceView) else id(obj)  # noqa: E731
        with patch("covey.cards.trace_view.id", same, create=True):
            _, _, notes, _ = TraceView(root)._trace_lines()
        self.assertEqual([n["fold"] for n in notes.values() if n["title"] == "子任务说明"], [True] * 60)

    def test_a_sub_task_is_named_once_for_its_consecutive_entries(self):
        # At midday today: now - 60 s is yesterday until 00:01, and an entry of another day shows its date.
        noon = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
        trace = self.subtask(["first", "second", "$ ls"], name="CUA review of bot card UI", started=noon)
        panel = trace_panel(trace, False)
        headings = [t.split("\n")[0] for t in self.texts(panel) if t.split("\n")[0].endswith("进展说明</font>")]
        self.assertEqual(headings[0], "<font color='blue'>子任务 · CUA review of bot card UI · 12:00 · 进展说明</font>")
        self.assertEqual(headings[1], "<font color='blue'>子任务 · 12:00 · 进展说明</font>")
        # Its operation names it in full: the rows of a run may be of several owners.
        self.assertIn("子任务 · CUA review of bot card UI · ", next(t for t in self.texts(panel) if "echo" in t or "ls" in t.split("\n")[-1]))
        older = self.subtask(["yesterday"], started=time.time() - 3 * 86400)  # Another day: with its date.
        self.assertRegex(next(t.split("\n")[0] for t in self.texts(trace_panel(older, False)) if "进展说明</font>" in t),
                         r" · \d\d-\d\d \d\d:\d\d · 进展说明</font>$")

    def test_the_first_entry_after_the_fold_names_its_sub_task(self):
        trace = self.subtask(["说明内容。" * 900, "下一条。" * 900, "再下一条。" * 900], name="长子任务")
        trace.route = {"kind": "bridge_trace", "topic_id": "topic", "job_id": 1}
        elements = trace_panel(trace, False)["elements"]
        self.assertEqual(elements[0]["element_id"], "trace_older")
        self.assertIn("子任务 · 长子任务 · ", md_text(elements[1]))
        # After it, the sub-task's entries say only 子任务.
        self.assertFalse([e for e in elements[2:-3] if "长子任务" in json.dumps(e, ensure_ascii=False)])

    def test_a_bridge_restart_is_a_line_of_its_own_not_an_operation(self):
        # T182 #1111: "22:03:16 · 操作记录 · 5 项" counted "机器人 22:03 重启过，任务未中断。" twice.
        trace = ToolTrace()
        for key in ("a", "b"):
            trace.accept("tool", {"id": key, "name": "bash", "detail": f"echo {key}", "status": "done",
                                  "started_at": 1000, "ended_at": 1001})
        trace.accept("progress_note", {"id": "bridge-restart-1", "title": "机器人", "text": "机器人 22:03 重启过，任务未中断。"})
        trace.accept("tool", {"id": "c", "name": "bash", "detail": "echo c", "status": "done", "started_at": 1002,
                              "ended_at": 1003})
        panel = trace_panel(trace, False)
        rows = next(e["content"] for e in panel["elements"] if "echo a" in e.get("content", ""))
        self.assertEqual([rows.count(f"echo {key}") for key in "abc"], [1, 1, 1])
        self.assertNotIn("重启过", rows)
        line = next(e for e in panel["elements"] if e.get("tag") == "markdown" and "重启过" in e["content"])
        self.assertEqual(line["content"], "机器人 22:03 重启过，任务未中断。")

    def test_figures_read_in_hours_and_ten_thousands_the_exact_ones_are_in_the_details(self):
        # T182 #1111: "总耗时 6460.3 秒", "输入 141,137,149 token（含缓存）" (T173: the owner misread the cache).
        trace = task_trace()
        trace.accept("token_usage", {"input_tokens": 141137149, "cache_read_tokens": 139000000,
                                     "cache_write_tokens": 1000000, "output_tokens": 329638})
        trace.started_at = time.time() - 6460.3
        trace.finish()
        trace.route = {"kind": "bridge_trace", "topic_id": "topic", "job_id": 1}
        first, second = panel_title(trace_panel(trace, False)), figure_cells(trace.snapshot())[1]
        self.assertEqual(first, "执行过程 · 无执行记录")  # The rounded tokens are the token tile's.
        self.assertEqual(panel_title(trace_panel(trace, False, timed=True)).split("\n")[0], "执行过程 · 无执行记录 · 用时 1小时47分")
        self.assertEqual(second, ("token", "1.4亿", ""))  # 1.41 亿 in and 33.0 万 out, rounded in the tile.
        # No 总耗时 (the 用时 tile, 2026-11-04 15:07), nor the exact counts (the 输入/输出 box's, 2026-11-09).
        self.assertIsNone(task_details_panel(SimpleNamespace(job_id=1111, model="m", timeout=0), trace.snapshot()))
        self.assertEqual([short_count(n) for n in (9999, 10000, 26204, 4146596, 99994999, 99995000)],
                         ["9,999", "1.00 万", "2.62 万", "415 万", "9999 万", "1.00 亿"])

    def test_the_figures_do_not_say_a_page(self):
        trace = ToolTrace()
        trace.route = {"kind": "bridge_trace", "topic_id": "topic", "job_id": 1}
        trace.note("n", "进展全文" * 20000)
        trace.finish()
        panel = trace_panel(trace, False)
        self.assertNotIn("页", panel_title(panel))


class IncrementalTests(unittest.TestCase):

    def trace(self):
        t=ToolTrace();t.route={'kind':'bridge_trace','topic_id':'t','job_id':1}
        for i in range(24):t.note(str(i),'中文🙂 detailed public progress '*120)
        return t

    def test_live_updates_do_not_replace_picker_or_parent(self):
        t=self.trace();before=split_panel(trace_panel(t, True));t.note('extra','new event')
        after=split_panel(trace_panel(t, True));actions=incremental_actions(before,after)
        self.assertTrue(actions)
        self.assertFalse(any(a['params'].get('element_id') in ('process_panel','trace_details','trace_page_picker') for a in actions))
        self.assertFalse(any(a['action']=='partial_update_element' for a in actions))

    def test_the_end_of_a_task_folds_the_process_panel(self):
        t=self.trace();before=split_panel(trace_panel(t, True));t.finish()
        after=split_panel(trace_panel(t, False));actions=incremental_actions(before,after)
        details=[json.loads(a['params']['element']) for a in actions if a['params'].get('element_id')=='trace_details']
        self.assertEqual([d['expanded'] for d in details],[False])
        self.assertTrue(all(a['action']=='update_element' for a in actions))
        self.assertEqual(incremental_actions(after,split_panel(trace_panel(t, False))),[])

    def test_a_running_panel_starts_folded_and_the_owner_s_open_survives_until_the_end(self):
        # #16: the status line says 现在：…, so 执行过程 starts folded; the owner opens it, live updates leave the
        # collapsible (its title and open state) alone, the end replaces it once, folded, its title N 步.
        t = self.trace()
        live = split_panel(trace_panel(t, True))
        details = live["columns"][0]["elements"][0]
        self.assertEqual((details["element_id"], details["expanded"]), ("trace_details", False))
        opened = TraceView(t.snapshot()).panel_model(True, opened=True)  # An old card's page redraw: drawn open.
        self.assertTrue(opened.expanded)
        t.note("extra", "new event")
        later = split_panel(trace_panel(t, True))
        for before in (live, panel_json(opened)):
            actions = incremental_actions(before, later)
            self.assertTrue(actions)
            self.assertFalse(any(a["params"].get("element_id") in ("process_panel", "trace_details") for a in actions))
        t.finish()
        ended = split_panel(trace_panel(t, False))
        actions = incremental_actions(later, ended)
        details = [json.loads(a["params"]["element"]) for a in actions if a["params"].get("element_id") == "trace_details"]
        self.assertEqual([(d["expanded"], d["header"]["title"]["content"]) for d in details],
                         [(False, "**执行过程**　<font color='grey'>25 步</font>")])

    def test_metrics_only_update_cannot_reset_open_or_collapsed_process(self):
        t=self.trace();before=split_panel(trace_panel(t, True));t.input_tokens=123456
        after=split_panel(trace_panel(t, True))
        self.assertEqual(incremental_actions(before,after),[])  # Tokens are not in the panel (2026-11-04 15:07).
        t.note('extra','one more');after=split_panel(trace_panel(t, True));actions=incremental_actions(before,after)
        self.assertIn('trace_metrics',[a['params']['element_id'] for a in actions])
        self.assertFalse(any(a['params'].get('element_id') in ('process_panel','trace_details') for a in actions))

    def test_body_changes_leave_selector_identity_and_options_untouched(self):
        t=self.trace();before=split_panel(trace_panel(t, True));t.note('extra','fresh public event')
        after=split_panel(trace_panel(t, True));actions=incremental_actions(before,after)
        self.assertTrue(any(a['params'].get('element_id')=='trace_body' for a in actions))
        self.assertFalse(any(a['params'].get('element_id')=='trace_page_picker' for a in actions))

    def test_all_transport_pages_fit_card_budget(self):
        wrapped=split_panel(trace_panel(self.trace(), False))
        self.assertLessEqual(json_bytes(wrapped),24000)
        self.assertLessEqual(element_count(wrapped),120)

    def test_a_long_record_folds_its_oldest_entries_and_adds_no_picker(self):
        t=ToolTrace();old=split_panel(trace_panel(t));t.route={'kind':'bridge_trace','job_id':1};t.note('n','progress '*9000)
        new=split_panel(trace_panel(t))
        self.assertNotIn('select_static', json.dumps(new))
        self.assertIn('trace_older', json.dumps(new))
        self.assertTrue(incremental_actions(old,new))


class EmbeddedTraceMigrationTest(unittest.TestCase):
    """core/migrate.py writes the process snapshot an older job record embeds to its traces/ file once."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        # This step alone: the others (desk-retire, tests/test_compat_desk_topics.py) have their own tests.
        from covey.core import migrate as steps
        self.enterContext(patch.dict(steps.STEPS, {steps.EMBEDDED_TRACE: steps.STEPS[steps.EMBEDDED_TRACE]},
                                          clear=True))

    def embed(self, key, note, **fields):
        trace = ToolTrace()
        trace.started_at, trace.ended_at, trace.output_tokens = 1000, 1030, 77
        trace.note('b', note)
        trace.note('a', 'first ' + note)
        snapshot = trace.snapshot()
        snapshot['step_order'] = ['a', 'b']  # A record's order may differ from its steps' key order.
        path = job_path(self.state, key)
        path.write_text(json.dumps({'key': key, 'job_id': 1, 'trace_snapshot': snapshot, **fields}, indent=2))
        return path, snapshot

    def test_each_embedded_snapshot_is_written_once_and_reads_as_it_did(self):
        one, embedded = self.embed('one', 'alpha')
        two, _ = self.embed('two', 'beta')
        none = job_path(self.state, 'none')
        none.write_text(json.dumps({'key': 'none', 'job_id': 3}))
        save_trace(self.state, 'two', {'steps': {'x': {'title': 'newer', 'started': 1}}})
        saved = trace_path(self.state, 'two')
        records = {path: path.read_bytes() for path in (one, two, none)}
        self.assertIsNone(load_trace(self.state, read_job_record(one)))
        lines = []
        self.assertEqual(migrate(self.state, log=lines.append), {'embedded-trace': {'written': 1, 'failed': 0}})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('record migrations embedded-trace:written=1,failed=0 elapsed_ms='))
        self.assertEqual({path: path.read_bytes() for path in records}, records)  # The records are not touched.
        self.assertEqual(ToolTrace.restore(load_trace(self.state, read_job_record(one))).snapshot(),
                         ToolTrace.restore(embedded).snapshot())
        self.assertEqual(list(load_trace(self.state, read_job_record(one))['steps']), ['a', 'b'])
        self.assertEqual(list(load_trace(self.state, read_job_record(two))['steps']), ['x'])  # Its own file stays.
        self.assertFalse(trace_path(self.state, 'none').exists())
        files = {path: path.read_bytes() for path in (trace_path(self.state, 'one'), saved)}
        self.assertTrue(json.loads((self.state / MIGRATIONS).read_text())['embedded-trace']['done'])
        self.assertEqual(migrate(self.state, log=lines.append), {})  # Done: one read of migrations.json.
        (self.state / MIGRATIONS).unlink()
        self.assertEqual(migrate(self.state), {'embedded-trace': {'written': 0, 'failed': 0}})  # Idempotent anyway.
        self.assertEqual(({path: path.read_bytes() for path in files}, len(lines)), (files, 1))

    def test_a_gzipped_file_counts_and_a_run_cut_short_resumes(self):
        self.embed('a', 'alpha')
        self.embed('b', 'beta')
        self.embed('c', 'gamma')
        save_trace(self.state, 'c', {'steps': {'x': {'title': 'own', 'started': 1}}})
        compress_trace(self.state, 'c', older_than=time.time() + 1, finished=lambda: True)
        self.assertTrue(trace_path(self.state, 'c').with_suffix('.jsonl.gz').exists())
        real = save_embedded

        def cut(state, key, snapshot):
            if key == 'b':
                raise OSError('disk')
            return real(state, key, snapshot)
        with patch('covey.core.migrate.save_embedded', side_effect=cut):
            self.assertEqual(migrate(self.state), {'embedded-trace': {'written': 1, 'failed': 1}})
        self.assertFalse((self.state / MIGRATIONS).exists())  # Not done: the next run goes on.
        first = trace_path(self.state, 'a').read_bytes()
        self.assertEqual(migrate(self.state), {'embedded-trace': {'written': 1, 'failed': 0}})
        self.assertEqual(trace_path(self.state, 'a').read_bytes(), first)
        self.assertIn('beta', json.dumps(load_trace(self.state, {'key': 'b'})))
        self.assertEqual(list(load_trace(self.state, {'key': 'c'})['steps']), ['x'])
        self.assertFalse(trace_path(self.state, 'c').exists())

    def test_maintenance_apply_migrates_too(self):
        self.embed('one', 'alpha')
        plan = maintain(self.state, apply=True, backup=False)
        self.assertEqual(plan['migrated'], {'embedded-trace': {'written': 1, 'failed': 0}})
        self.assertIn('alpha', json.dumps(load_trace(self.state, {'key': 'one'})))

    def test_records_it_cannot_write_are_named_and_do_not_stop_the_start(self):
        self.embed('fine', 'alpha')
        self.embed('blocked', 'beta')
        trace_path(self.state, 'blocked').mkdir(parents=True)  # Its file cannot be written.
        # By hand: a record from before the migration, its snapshot malformed.
        job_path(self.state, 'odd').write_text(json.dumps({'key': 'odd', 'trace_snapshot': {'children': {'c': 'x'}}}))
        lines = []
        with patch('covey.core.migrate.FAILED_KEYS', 1):
            self.assertEqual(migrate(self.state, log=lines.append), {'embedded-trace': {'written': 1, 'failed': 2}})
        self.assertRegex(lines[0], r'^record migrations embedded-trace:written=1,failed=2 elapsed_ms=\d+ '
                                   r'embedded-trace:failed_keys=(blocked|odd),\+1$')
        self.assertFalse((self.state / MIGRATIONS).exists())  # Not done: the next start tries them again.
        self.assertEqual(pending(self.state), {'embedded-trace': 2})

    def test_the_dry_run_lists_the_migration_until_it_is_done(self):
        self.embed('one', 'alpha')
        last = lambda: dry_run_report(self.state).splitlines()[-1]
        self.assertEqual(last(), '旧记录迁移（core/migrate.py，--apply 和 Bridge 启动时先做）：embedded-trace 还没做完，'
                                 '内嵌执行过程待写成 traces/ 文件的记录 1 条')
        self.assertFalse(trace_path(self.state, 'one').exists())  # The dry run writes nothing.
        migrate(self.state)
        self.assertEqual(last(), '旧记录迁移（core/migrate.py，--apply 和 Bridge 启动时先做）：已做完')


class TracePaginationTest(unittest.TestCase):
    """A page picked on a card sent with page controls (before 2026-11-04) redraws the one page."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = SimpleNamespace(topic_id='topic', state_dir=Path(self.temp.name), profile='neeko')
        trace = ToolTrace()
        trace.note('n', '进展全文' * 15000)
        self.record = dict(job_id=5, key='job', card_message_id='original', card_id='card', trace_snapshot=trace.snapshot())
        save_embedded(self.ctx.state_dir, 'job', self.record['trace_snapshot'])  # As core/migrate.py writes it.
        self.app = build_claude_app()
        self.app.harness.topic_records = lambda *_: [self.record]
        self.app.harness.topic_record = lambda *_: self.record
        self.app.messenger.publish_card = Mock()
        self.event = dict(message_id='original', chat_id='chat', event_id='click',
                          action_value=dict(kind='bridge_trace', topic_id='topic', job_id=5, page=1))

    def test_first_click_only_updates_process_element_on_original_card(self):
        with FakeCardKit({}) as lark:
            self.assertTrue(self.app.interaction.interaction_action(self.ctx, self.event))
        self.app.messenger.publish_card.assert_not_called()
        self.assertEqual((lark.last.method, lark.last.path), ('PUT', '/open-apis/cardkit/v1/cards/card/elements/process_panel'))
        element = json.loads(lark.last.data['element'])
        self.assertFalse([e for e in walk_dicts(element) if e.get('tag') == 'select_static'])
        self.assertIn('trace_older', json.dumps(element))
        self.assertIn('执行过程', next(e for e in walk_dicts(element) if e.get('element_id')=='trace_details')['header']['title']['content'])
        self.assertNotIn('trace_part_', json.dumps(element))

    def test_a_picked_page_is_kept_as_the_newest_and_a_recovery_card_draws_the_one_page(self):
        with FakeCardKit({}):
            self.app.interaction.interaction_action(self.ctx, self.event)
        record = {**self.record, 'trace_page': 1, 'topic_id': 'topic', 'status': 'interrupted'}
        card = recovery_card(record)
        panel = next(e for e in card['body']['elements'] if e.get('element_id') == 'process_panel')
        self.assertFalse([e for e in walk_dicts(panel) if e.get('tag') == 'select_static'])

    def test_an_interrupted_card_draws_its_record_from_the_process_file(self):
        snapshot = self.record.pop('trace_snapshot')
        save_trace(self.ctx.state_dir, 'job', snapshot)
        self.record.update(topic_id='topic', status='interrupted')
        card = recovery_card(self.record, state_dir=self.ctx.state_dir)
        self.assertIn('进展全文', json.dumps(card, ensure_ascii=False))
        self.assertFalse([e for e in walk_dicts(card) if e.get('tag') == 'select_static'])
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx, self.event)
        self.assertIn('进展全文', lark.last.data['element'])

    def test_a_process_file_gzipped_by_maintenance_still_draws_and_rebuilds(self):
        snapshot = self.record.pop('trace_snapshot')
        save_trace(self.ctx.state_dir, 'job', snapshot)
        plain = trace_path(self.ctx.state_dir, 'job')
        original = load_trace(self.ctx.state_dir, self.record)
        self.assertEqual(compress_trace(self.ctx.state_dir, 'job', older_than=time.time() + 1, finished=lambda: False), 0)
        self.assertGreater(compress_trace(self.ctx.state_dir, 'job', older_than=time.time() + 1, finished=lambda: True), 0)
        self.assertEqual(sorted(p.name for p in plain.parent.iterdir()), ['job.jsonl.gz'])  # No temporary left.
        self.assertEqual(load_trace(self.ctx.state_dir, self.record), original)
        # The retry card and a paging click on the old card read it (task_controls, interaction).
        self.record.update(topic_id='topic', status='interrupted')
        card = recovery_card(self.record, state_dir=self.ctx.state_dir)
        self.assertIn('进展全文', json.dumps(card, ensure_ascii=False))
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx, self.event)
        self.assertIn('进展全文', lark.last.data['element'])
        # A later save in this process starts a new plain file with the whole snapshot, read first.
        trace = ToolTrace.restore(original)
        trace.note('later', 'after the gzip')
        save_trace(self.ctx.state_dir, 'job', trace.snapshot())
        self.assertIn('"full": true', plain.read_text())
        self.assertEqual(load_trace(self.ctx.state_dir, self.record), load_trace(self.ctx.state_dir, {'key': 'job'}))
        self.assertIn('later', load_trace(self.ctx.state_dir, self.record)['steps'])

    def test_a_recent_or_changed_process_file_is_not_gzipped(self):
        save_trace(self.ctx.state_dir, 'job', self.record['trace_snapshot'])
        plain = trace_path(self.ctx.state_dir, 'job')
        self.assertEqual(compress_trace(self.ctx.state_dir, 'job', older_than=time.time() - 60, finished=lambda: True), 0)

        def append_meanwhile():  # Another process appends while the gzip is written.
            with plain.open('a') as handle:
                handle.write('{"v": 1, "set": []}\n')
            return True
        self.assertEqual(compress_trace(self.ctx.state_dir, 'job', older_than=time.time() + 1, finished=append_meanwhile), 0)
        self.assertEqual(sorted(p.name for p in plain.parent.iterdir()), ['job.jsonl'])

    def test_existing_browse_reply_is_updated_in_place(self):
        folder = self.ctx.state_dir/'browse-cards';folder.mkdir()
        (folder/'browse.json').write_text(json.dumps(dict(topic_id='topic', chat_id='chat', card_id='browse-card', sequence=1)))
        self.event['message_id']='browse'
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx,self.event)
        self.assertIn('/browse-card/elements/process_panel',lark.last.path)
        self.app.messenger.publish_card.assert_not_called()

    def test_update_failure_never_falls_back_to_new_reply(self):
        with FakeCardKit(None):
            with self.assertRaisesRegex(ValueError,'翻页更新失败'):
                self.app.interaction.interaction_action(self.ctx,self.event)
        self.app.messenger.publish_card.assert_not_called()

    def test_jump_uses_selected_page_and_does_not_leak_jump_into_buttons(self):
        self.event['action_value'].update(verb='jump', page_count=10)
        self.event['option'] = '3'
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx, self.event)
        panel = json.loads(lark.last.data['element'])
        for e in walk_dicts(panel):
            if e.get('tag') == 'button':
                self.assertNotIn('verb', e['behaviors'][0]['value'])
        self.assertFalse([e for e in walk_dicts(panel) if e.get('tag') == 'select_static'])

    def test_invalid_jump_does_not_update_card(self):
        self.event['action_value'].update(verb='jump', page_count=10)
        for value in ('', '0', '-1', '11', '1.5', 'abc'):
            self.event['form_value'] = {'trace_page': value}
            with FakeCardKit() as lark:
                with self.assertRaisesRegex(ValueError, '整数页码'):
                    self.app.interaction.interaction_action(self.ctx, self.event)
                self.assertEqual(lark.calls, [])

    def test_event_consumer_preserves_selected_option(self):
        fields = [field.strip() for field in CARD_ACTION_EVENT_JQ.strip('{}').split(',')]
        self.assertIn('option', fields)
        self.assertIn('input_value', fields)  # The page-number box.

    def test_a_record_from_before_process_files_renders_once_migrated(self):
        # Older job records embed the process (trace_snapshot, tool_steps and child_steps beside it) and have no
        # traces/ file until core/migrate.py writes one.
        trace = ToolTrace()
        trace.steps = {'n1': {'title': '进展说明', 'detail': '先读取目录结构', 'status': 'note', 'started': 1000},
                       'agent-c1': {'title': '子任务', 'detail': '核对配置', 'status': 'done', 'started': 1001,
                                    'agent_id': 'c1'}}
        trace.agents = {'c1': {'name': '核对配置', 'status': 'completed', 'started_at': 1001, 'ended_at': 1010,
                               'background': False, 'parent_id': None}}
        trace.children['c1'] = ToolTrace()
        trace.children['c1'].steps = {'m1': {'title': '进展说明', 'detail': '配置一致', 'status': 'note', 'started': 1002}}
        self.record = dict(job_id=5, key='old-job', card_message_id='original', card_id='card', status='done',
                           elapsed_seconds=30, trace_snapshot=trace.snapshot(), tool_steps=trace.steps,
                           child_steps={'c1': trace.children['c1'].steps}, agents=trace.agents)
        job_path(self.ctx.state_dir, 'old-job').write_text(json.dumps(self.record))  # By hand: from before the migration.
        migrate(self.ctx.state_dir)
        with FakeCardKit({}) as lark:
            self.assertTrue(self.app.interaction.interaction_action(self.ctx, self.event))
        element = json.loads(lark.last.data['element'])
        self.assertIn('先读取目录结构', json.dumps(element, ensure_ascii=False))
        self.assertIn('核对配置', json.dumps(element, ensure_ascii=False))

    def test_paused_question_keeps_live_card_sequence_in_sync(self):
        progress = SimpleNamespace(card_id='card', sequence=10)
        job = SimpleNamespace(job_id=5, topic_id='topic', chat_id='chat', progress=progress,
                              backend_control=SimpleNamespace(pause_for_question='pending'))
        self.app.runtime.jobs.by_id[5] = job
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx,self.event)
        self.assertEqual(progress.sequence,lark.last.data['sequence'])

    def test_a_running_card_draws_the_one_page(self):
        trace = ToolTrace()
        trace.note('n', '进展全文' * 15000)
        job = make_job(message_id='job', progress=SimpleNamespace(card_id='card', sequence=1, disabled=False))
        job.tool_trace = trace
        self.app.cards.details.set_process_panel = Mock()

        def drawn():
            job.trace_card_updates = None
            self.app.cards.queue_trace(self.ctx, job, True)
            job.trace_card_updates.close()
            return self.app.cards.details.set_process_panel.call_args.args[2]
        first = drawn()
        self.assertFalse([e for e in walk_dicts(first) if e.get('tag') == 'select_static'])
        self.assertEqual(drawn(), first)

    def test_latest_selection_keeps_following_latest_for_live_writer(self):
        progress = SimpleNamespace(card_id='card', sequence=10)
        job = SimpleNamespace(job_id=5, topic_id='topic', chat_id='chat', progress=progress)
        self.app.runtime.jobs.by_id[5] = job
        self.event['action_value'].update(verb='latest')
        with FakeCardKit({}) as lark:
            self.app.interaction.interaction_action(self.ctx,self.event)
        self.assertNotIn('select_static', lark.last.data['element'])

    def test_normal_long_trace_is_continuous_without_navigation_or_duplicate_metrics(self):
        trace=ToolTrace()
        trace.route={'kind':'bridge_trace','topic_id':'topic','job_id':1}
        for i in range(12):
            trace.note(str(i), 'phase-'+str(i)+' '+('public progress ' * 25), received_at=1000+i)
            trace.accept('tool', {'id':'t'+str(i),'name':'bash','detail':'printf marker','status':'done',
                                  'started_at':1000.5+i,'ended_at':1000.5+i,'exit_code':0})
        before=trace.snapshot()
        panel=trace_panel(trace, False)
        text=json.dumps(panel)
        self.assertNotIn('trace_part_', text)
        self.assertNotIn('behaviors', text)
        self.assertNotIn('select_static', text)
        self.assertNotIn('output_rate_note', text)
        # 12 notes, each followed by its command in full (2026-11-04 15:07).
        self.assertEqual(len(panel['elements']),24)
        self.assertIn("**执行命令**", panel['elements'][1]['content'])
        self.assertIn("printf marker", panel['elements'][1]['content'])
        self.assertEqual(before,trace.snapshot())

    def test_a_very_large_record_is_one_page_within_the_card_budget(self):
        trace=ToolTrace()
        trace.route={'kind':'bridge_trace','topic_id':'topic','job_id':1}
        note='中文🙂长记录\n'*9000
        trace.note('long',note)
        panel=trace_panel(trace, False)
        self.assertNotIn('select_static',json.dumps(panel))
        self.assertLessEqual(len(json.dumps(split_panel(panel),ensure_ascii=False).encode()),24000)
        self.assertLessEqual(element_count(split_panel(panel)),120)
        self.assertTrue(md_text(panel['elements'][-1]).endswith('中文🙂长记录\n'*20))  # Its end, whole.


class LiveEventTest(QuestionFixture, unittest.TestCase):

    def test_nested_agents_appear_under_parent_without_duplicate_root_entry(self):
        now=time.time()
        self.app.interaction.structured_harness_event(self.ctx_topic,self.job,'agent_state',{'id':'A','name':'A','status':'running','started_at':now},self.rpc)
        self.app.interaction.structured_harness_event(self.ctx_topic,self.job,'agent_state',{'id':'B','parent_id':'A','name':'B','status':'completed','started_at':now,'ended_at':now+1},self.rpc)
        trace=self.job.tool_trace
        self.assertIn('A',trace.agents);self.assertNotIn('B',trace.agents)
        self.assertIn('B',trace.children['A'].agents)
        self.assertIsNotNone(trace.children['B'].ended_at)
        self.job.trace_card_updates.close()

    def test_stream_flush_never_overwrites_final(self):
        received=[]
        self.job.progress=SimpleNamespace(card_id='card')
        def update(ctx, progress, text, force=False):
            received.append(text)
        self.app.cards.update_streaming_reply=update
        self.app.interaction.structured_harness_event(self.ctx_topic,self.job,'answer_snapshot',{'text':'partial'},self.rpc)
        self.assertTrue(self.job.card_updates.close(timeout=2))
        self.assertEqual(received,['partial'])
        self.job.answer_finalized=True
        self.app.interaction.structured_harness_event(self.ctx_topic,self.job,'answer_snapshot',{'text':'stale'},self.rpc)
        self.assertEqual(received,['partial'])

    def test_final_process_retains_note_tool_and_timing_in_same_element(self):
        self.app.interaction.structured_harness_event(self.ctx_topic, self.job, "progress_note", {"id":"note", "text":"先读取目录"}, self.rpc)
        tool = {"id":"cmd", "name":"bash", "detail":"pwd && sleep 10", "started_at":1000}
        self.app.interaction.structured_harness_event(self.ctx_topic, self.job, "tool", {**tool, "status":"running"}, self.rpc)
        self.app.interaction.structured_harness_event(self.ctx_topic, self.job, "tool", {**tool, "status":"done", "ended_at":1010, "exit_code":0}, self.rpc)
        self.app.cards.finalize_trace(self.ctx_topic, self.job)
        panel = next(c.args[-1] for c in reversed(self.app.cards.set_task_element.call_args_list)
                     if c.args[-1]['element_id'] == 'process_panel')
        self.assertEqual(panel['element_id'], 'process_panel')
        self.assertFalse(panel['columns'][0]['elements'][0]['expanded'])  # The split panel (panel_json).
        self.assertIn('先读取目录', str(panel))
        self.assertIn('sleep 10', str(panel))
        self.assertIn('10 秒', str(panel))
        self.assertNotIn('退出码 0', str(panel))  # ✓ says it (2026-11-04 17:38).

    def add_second_question(self):
        self.record["questions"].append({"id": "order", "question": "顺序？", "options": []})
        self.path.write_text(json.dumps(self.record))


class PanelModelTest(unittest.TestCase):
    """TraceView.panel_model: the process panel as data; panel_json draws the split panel from it."""

    def test_the_model_says_the_state_and_panel_json_draws_it(self):
        from covey.cards.elements import grey
        from covey.cards.trace_updates import panel_json
        trace = ToolTrace()
        model = TraceView(trace.snapshot()).panel_model(True)
        self.assertEqual((model.state, model.live, model.expanded, model.rows), ("等待进展", True, False, 0))
        details = panel_json(model)["columns"][0]["elements"][0]
        self.assertEqual(details["header"]["title"]["content"], "**执行过程**　" + grey("进行中"))
        self.assertEqual(details["elements"][0], {"tag": "markdown", "element_id": "trace_metrics",
                                                  "text_size": "notation", "content": grey("等待进展")})
        trace.note("n", "先读目录")
        trace.finish()
        model = TraceView(trace.snapshot()).panel_model(False, timed=True)
        self.assertEqual((model.live, model.expanded, model.rows), (False, False, 1))
        self.assertRegex(model.state, r"^1 步 · 用时 \d+秒$")
        details = panel_json(model)["columns"][0]["elements"][0]
        self.assertEqual(details["header"]["title"]["content"], "**执行过程**　" + grey(model.state))
        self.assertEqual([e["element_id"] for e in details["elements"]], ["trace_body"])

    def test_an_ended_turn_without_records_is_one_line(self):
        from covey.cards.elements import grey
        from covey.cards.trace_updates import panel_json
        trace = ToolTrace()
        trace.finish()
        self.assertEqual(panel_json(TraceView(trace.snapshot()).panel_model(False))["columns"][0]["elements"],
                         [{"tag": "markdown", "element_id": "trace_details",
                           "content": "**执行过程**　" + grey("无执行记录")}])

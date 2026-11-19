"""A project's lead runs (docs/architecture.md 专项, app/project_lead.py, app/project_tools.py): its rules in layers and the
state pack, the inbox, when its session is built again, its turn (tools, the built-in ones narrowed, the message), its
tools as the MCP server lists them and the Bridge runs them, the line its card says, and that with COVEY_PROJECTS off
none of it changes anything."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import enter_context
from support.fake_backends import codex_context
from support.records import state_dir, write_job, write_topic

from covey.app import dispatch_cards, butler_handoff, butler_reply, butler as session, butler_tools, project_lead, project_tools
from covey.app import projects as projects_app
from covey.app.butler_tools import ButlerTools
from covey.backends.claude.argv import stream_argv
from covey.backends.claude.bridge import ClaudeBackend
from covey.core import butler_mcp, butler, butler_tool_specs, project_memory, project_proposals, projects
from covey.core import butler_settings as settings
from covey.core.jobs import job_key
from covey.core.butler_requests import Request
from covey.core.state import write_json

from test_butler_topics import ButlerTopicsCase

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
LEAD = projects.lead_topic_id(CHAT, PID)
ON = {projects.ENV: "1"}
NOW = 1_800_000_000.0
LEAD_NAMES = ["topics_list", "topics_search", "topic_read", "workspaces_list",
              "project_topic_start", "project_topic_send", "project_topic_stop", "project_note", "project_goal", "project_remember",
              "project_style", "project_finish"]


class Case(unittest.TestCase):
    """A state directory with the butler on (mode agent) and a project with its lead topic."""

    def setUp(self):
        self.state = state_dir(self)
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
        enter_context(self, mock.patch.dict(os.environ, {**env, **ON}, clear=True))
        write_json(self.state / settings.FILE, {"mode": "agent"})
        butler.forget_mode()
        self.record = projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", goal="把账单迁到新库，对账一致",
                                                                      project_id=PID, now=NOW))[0]
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, harness_id="claude",
                    project=projects.project_field(PID, projects.LEAD))
        self.logs = []

    def thread(self, topic_id, title, number, **job):
        write_topic(self.state, {"topic_id": topic_id}, topic_id=topic_id, chat_id=CHAT, harness_id="claude",
                    topic_number=number, origin={"title": title}, project=projects.project_field(PID, projects.MEMBER))
        projects.add_member(self.state, PID, topic_id)
        if job:
            write_job(self.state, f"k-{topic_id[:4]}", topic_id=topic_id, chat_id=CHAT, job_id=number, **job)

    def lead_job(self, key="lead-1", job_id=1, **fields):
        return write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=job_id, **fields)

    def reload(self):
        return projects.read_record(self.state, PID)


class RulesTest(Case):
    def test_the_rules_are_layered_and_the_practice_layer_is_empty_until_phase_3(self):
        base = project_lead.lead_rules()
        self.assertTrue(base.startswith("[负责人规则]"))
        self.assertIn("Read、Grep、Glob", base)
        self.assertIn("project_topic_start", base)
        self.assertNotIn("管家规则", base)
        self.assertEqual(project_lead.PRACTICE, "")
        self.assertIn("专员做法层", project_lead.lead_rules(practice="专员做法层"))
        self.assertNotIn("专员做法层", base)

    def test_the_rules_say_the_style_the_record_has(self):
        rules = project_lead.lead_rules({"report": "ask_only", "spawn": "保守"})  # An older record's spawn is read past.
        self.assertIn("只在需要主人定的时候说", rules)
        self.assertIn("同时在做的话题不设上限", rules)
        for words in ("同时最多", "派活“", "保守", "多开几个话题"):
            self.assertNotIn(words, rules)
        self.assertIn("只报里程碑和完成", project_lead.lead_rules())

    def test_a_bad_style_reads_as_the_default_and_the_filter_follows_it(self):
        self.assertEqual(project_lead.style_of({"style": {"report": "everything", "detail": "全"}}),
                         {"report": "milestones", "detail": "全"})
        self.assertEqual(project_lead.style_of({"style": {"spawn": "积极", "report": "verbose"}}),
                         {"report": "verbose", "detail": "简"})  # An older record's spawn is no error and not read.
        self.assertEqual(project_lead.style_of({}), project_lead.STYLE_DEFAULT)
        table = {"ask_only": (), "milestones": ("milestone", "finish"), "each_thread": ("milestone", "finish", "thread"),
                 "verbose": ("milestone", "finish", "thread", "note")}
        for report, kinds in table.items():
            for kind in ("milestone", "finish", "thread", "note"):
                self.assertIs(project_lead.reportable({"report": report}, kind), kind in kinds, (report, kind))


class PackTest(Case):
    def test_a_new_session_gets_the_whole_pack_in_order(self):
        self.thread("ab" * 16, "导出旧账单", 41, status="running", phase="导出第 3 批")
        self.thread("cd" * 16, "写对账脚本", 42, status="done", final_text="脚本写好了\n放在 reconcile.py")
        projects.update(self.state, PID, lambda r: r.update(
            memory=[{"id": "m1", "kind": "pitfall", "text": "旧库时区是 UTC+0", "at": NOW}],
            decisions=[{"at": NOW, "text": "先迁 2024 年的账"}]))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True)
        order = [pack.index(part) for part in ("[专项状态：迁移账单]", "目标和完成标准", "话题（2 个，1 个没结束）",
                                               "专项记忆", "风格：", "近 1 条决定", "自上次唤醒以来的收件箱")]
        self.assertEqual(order, sorted(order))
        self.assertIn("把账单迁到新库，对账一致", pack)
        self.assertIn("T41 · 导出旧账单 · 在做 · 导出第 3 批", pack)
        self.assertIn("T42 · 写对账脚本 · 完成 · 放在 reconcile.py", pack)
        self.assertIn("[constraint] 旧库时区是 UTC+0", pack)  # An older release's entry in the record, under the kind's new name.
        self.assertIn("风格：report=milestones；detail=简\n", pack)
        self.assertIn("没有新的。", pack)

    def test_a_resumed_session_gets_only_what_changes(self):
        self.thread("ab" * 16, "导出旧账单", 41, status="running")
        projects.update(self.state, PID, lambda r: r.update(memory=[{"id": "m1", "kind": "fact", "text": "记忆里的话", "at": NOW}]))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=False)
        self.assertNotIn("目标和完成标准", pack)
        self.assertNotIn("记忆里的话", pack)
        self.assertIn("T41", pack)
        self.assertIn("风格：", pack)

    def test_the_decision_log_is_the_last_ten_and_a_thread_row_names_its_deliverables(self):
        checkpoint = self.state / "cp.json"
        write_json(checkpoint, {"artifacts": ["a.csv", "b.csv", "c.csv", "d.csv"]})
        self.thread("ab" * 16, "导出", 41, status="done", final_text="完成", checkpoint_path=str(checkpoint))
        projects.update(self.state, PID, lambda r: r.update(decisions=[{"at": NOW, "text": f"决定{n}"} for n in range(15)]))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True)
        self.assertIn("近 10 条决定", pack)
        self.assertIn("决定14", pack)
        self.assertIn("决定5", pack)
        self.assertNotIn("决定4\n", pack + "\n")
        self.assertIn("交付：a.csv；b.csv；c.csv", pack)
        self.assertNotIn("d.csv", pack)

    def test_a_project_without_threads_or_a_goal_says_so(self):
        projects.update(self.state, PID, lambda r: r.update(goal=""))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True)
        self.assertIn("话题（还没有）", pack)
        self.assertIn("主人没有写目标", pack)
        self.assertEqual(project_lead.library_index(self.state, PID), [])  # Nothing without COVEY_PROJECT_MEMORY=1.


class InboxTest(Case):
    def test_a_turn_takes_the_receipts_once_and_gets_them_again_when_it_is_the_same_turn(self):
        one = project_lead.add_receipt(self.state, PID, "thread_done", "T41 做完了", thread="ab" * 16, at=NOW)
        two = project_lead.add_receipt(self.state, PID, "owner_said", "主人说先停一下", at=NOW + 5)
        taken = project_lead.take_receipts(self.state, PID, "lead-1")
        self.assertEqual([item["id"] for item in taken], [one["id"], two["id"]])
        self.assertEqual([item["taken_by"] for item in taken], ["lead-1", "lead-1"])
        self.assertEqual(len(project_lead.take_receipts(self.state, PID, "lead-1")), 2)  # A retry of the same turn.
        self.lead_job("lead-1", status="done")
        self.assertEqual(project_lead.take_receipts(self.state, PID, "lead-2"), [])
        pack = project_lead.state_pack(self.state, self.reload(), "lead-3", full=False)
        self.assertIn("没有新的。", pack)

    def test_receipts_a_turn_that_failed_took_are_given_to_the_next(self):
        project_lead.add_receipt(self.state, PID, "thread_done", "T41 做完了", at=NOW)
        project_lead.take_receipts(self.state, PID, "lead-1")
        self.lead_job("lead-1", status="failed")
        taken = project_lead.take_receipts(self.state, PID, "lead-2")
        self.assertEqual([item["text"] for item in taken], ["T41 做完了"])
        pack = project_lead.state_pack(self.state, self.reload(), "lead-2", full=False)
        self.assertIn("T41 做完了", pack)
        self.assertIn("thread_done", pack)

    def test_a_turn_still_running_keeps_what_it_took(self):
        project_lead.add_receipt(self.state, PID, "thread_done", "x", at=NOW)
        project_lead.take_receipts(self.state, PID, "lead-1")
        self.lead_job("lead-1", status="running")
        self.assertEqual(project_lead.take_receipts(self.state, PID, "lead-2"), [])


class RebuildTest(Case):
    def last(self, **fields):
        self.lead_job("lead-1", status="done", model="claude-opus-5", timings={"execution_finished": NOW - 60}, **fields)

    def reason(self, model="claude-opus-5", now=NOW):
        return project_lead.rebuild_reason(self.state, LEAD, "lead-2", model, now)

    def test_a_first_turn_and_a_recent_small_one_on_the_same_model_go_on(self):
        self.assertEqual(self.reason(), "")
        self.last(input_tokens=149_999)
        self.assertEqual(self.reason(), "")

    def test_over_150_thousand_tokens_idle_over_a_day_or_another_model_each_rebuild(self):
        self.last(input_tokens=150_001)
        self.assertIn("150001", self.reason())
        self.assertEqual(self.reason(now=NOW + 24 * 3600 - 60), "上一轮读了 150001 token")

    def test_idle_and_model(self):
        self.last(input_tokens=1000)
        self.assertEqual(self.reason(now=NOW - 60 + 24 * 3600), "")
        self.assertEqual(self.reason(now=NOW - 60 + 24 * 3600 + 1), "空闲超过 24 小时")
        self.assertIn("换了模型", self.reason(model="claude-sonnet-5"))
        self.assertEqual(self.reason(model=""), "")

    def test_the_context_size_stands_in_when_a_record_has_no_input_tokens(self):
        self.last(context_tokens=200_000)
        self.assertIn("200000", self.reason())

    def test_settle_session_clears_the_pointer_and_logs_why(self):
        self.last(input_tokens=200_000)
        job = SimpleNamespace(topic_id=LEAD, chat_id=CHAT, message_id="lead-2", job_id=2, model="claude-opus-5")
        calls = []
        sessions = SimpleNamespace(clear_session=lambda ctx, revision, sid: calls.append((revision, sid)) or True,
                                   capture_session=lambda ctx: (8, None))
        runtime = SimpleNamespace(session_lock=__import__("threading").RLock(), log=self.logs.append)
        ctx = SimpleNamespace(state_dir=self.state, model="claude-opus-5")
        self.assertEqual(project_lead.settle_session(ctx, job, sessions, runtime, 7, "sid-1"), (8, None))
        self.assertEqual(calls, [(7, "sid-1")])
        self.assertTrue(any("lead session rebuilt" in line and "上一轮读了 200000 token" in line for line in self.logs))

    def test_a_session_that_may_go_on_and_other_topics_are_not_touched(self):
        self.last(input_tokens=10)
        job = SimpleNamespace(topic_id=LEAD, chat_id=CHAT, message_id="lead-2", job_id=2, model="claude-opus-5")
        sessions = SimpleNamespace(clear_session=mock.Mock(), capture_session=mock.Mock())
        runtime = SimpleNamespace(session_lock=__import__("threading").RLock(), log=self.logs.append)
        ctx = SimpleNamespace(state_dir=self.state, model="claude-opus-5")
        self.assertEqual(project_lead.settle_session(ctx, job, sessions, runtime, 7, "sid-1"), (7, "sid-1"))
        self.assertEqual(project_lead.settle_session(ctx, job, sessions, runtime, 7, None), (7, None))
        other = SimpleNamespace(topic_id="ee" * 16, chat_id=CHAT, message_id="x", job_id=3, model="m")
        self.lead_job("lead-9", job_id=9, status="done", input_tokens=900_000)
        self.assertEqual(project_lead.settle_session(ctx, other, sessions, runtime, 7, "sid-1"), (7, "sid-1"))
        sessions.clear_session.assert_not_called()

    def test_with_the_switch_off_a_lead_topic_is_an_ordinary_one(self):
        self.last(input_tokens=900_000)
        job = SimpleNamespace(topic_id=LEAD, chat_id=CHAT, message_id="lead-2", job_id=2, model="m")
        sessions = SimpleNamespace(clear_session=mock.Mock(), capture_session=mock.Mock())
        ctx = SimpleNamespace(state_dir=self.state, model="m")
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            self.assertEqual(project_lead.settle_session(ctx, job, sessions, SimpleNamespace(), 7, "sid"), (7, "sid"))


class SessionPointerTest(unittest.TestCase):
    def test_clear_session_is_a_compare_and_set_of_the_pointer(self):
        import re
        import threading

        from covey.app.sessions import Sessions
        from covey.core.session_state import capture_session_state

        root = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, root, True)
        sid = "11111111-1111-4111-8111-111111111111"
        ctx = SimpleNamespace(session_path=root / "session")
        pattern = re.compile(r"[0-9a-f-]{36}")
        sessions = Sessions(SimpleNamespace(spec=SimpleNamespace(session_pattern=pattern)),
                            runtime=SimpleNamespace(session_lock=threading.RLock()))
        ctx.session_path.write_text(sid)
        revision, found = capture_session_state(ctx.session_path, pattern)
        self.assertFalse(sessions.clear_session(ctx, revision + 5, sid))  # Another revision: left as it is.
        self.assertFalse(sessions.clear_session(ctx, revision, "22222222-2222-4222-8222-222222222222"))
        self.assertTrue(ctx.session_path.exists())
        self.assertTrue(sessions.clear_session(ctx, revision, found))
        self.assertFalse(ctx.session_path.exists())


class StartTurnTest(Case):
    """app/worker.py ``_start_turn``: the lead's session pointer is dropped before the turn takes it."""

    def start(self, topic_id, sid):
        import re
        import threading

        from covey.app import worker
        from covey.app.sessions import Sessions

        pattern = re.compile(r"[0-9a-f-]{36}")
        ctx = SimpleNamespace(state_dir=self.state, session_path=self.state / "session", model="claude-opus-5", topic_id=topic_id)
        ctx.session_path.write_text(sid)
        runtime = SimpleNamespace(session_lock=threading.RLock(), log=self.logs.append)
        sessions = Sessions(SimpleNamespace(spec=SimpleNamespace(session_pattern=pattern, name="claude")), runtime=runtime)
        job = SimpleNamespace(topic_id=topic_id, chat_id=CHAT, message_id="lead-2", job_id=2, model="claude-opus-5")
        with mock.patch.object(worker, "timing"):
            return worker._start_turn(ctx, job, sessions=sessions, runtime=runtime, record_job_state=lambda *a, **k: None), job

    def test_a_big_previous_turn_starts_the_lead_on_a_new_session_and_a_topic_keeps_its_own(self):
        sid = "11111111-1111-4111-8111-111111111111"
        self.lead_job("lead-1", status="done", model="claude-opus-5", input_tokens=200_000)
        write_job(self.state, "top-1", topic_id="ee" * 16, chat_id=CHAT, job_id=8, status="done", input_tokens=900_000)
        (_, session_id), job = self.start(LEAD, sid)
        self.assertIsNone(session_id)
        self.assertIsNone(job.session_id)
        self.assertFalse((self.state / "session").exists())
        (_, session_id), job = self.start("ee" * 16, sid)
        self.assertEqual((session_id, job.session_id), (sid, sid))

    def test_a_small_recent_one_resumes(self):
        sid = "11111111-1111-4111-8111-111111111111"
        self.lead_job("lead-1", status="done", model="claude-opus-5", input_tokens=1000,
                      timings={"execution_finished": time.time() - 60})
        (_, session_id), _ = self.start(LEAD, sid)
        self.assertEqual(session_id, sid)


class TurnTest(Case):
    def context(self, **options):
        return dataclasses.replace(codex_context(self.state, "stream-json"), state_dir=self.state, options=options)

    def job(self, **fields):
        return SimpleNamespace(topic_id=LEAD, chat_id=CHAT, message_id="lead-1", job_id=1, kind="chat", **fields)

    def run_turn(self, session_id=None, **fields):
        return session.butler_turn(self.context(), self.job(**fields), "叫醒", session_id, mock.Mock(), self.logs.append)

    def test_a_lead_turn_has_its_tools_narrowed_and_its_message_headed_by_rules_and_pack(self):
        ctx, message = self.run_turn()
        self.assertEqual(ctx.options["tools"], ("Read", "Grep", "Glob"))
        server = ctx.options["mcp_config"]["mcpServers"][butler.MCP_SERVER]
        self.assertEqual(server["env"][butler.LEAD_ENV], PID)
        self.assertEqual(server["env"][butler.JOB_ENV], job_key("lead-1", 1))
        self.assertTrue(message.startswith("[负责人规则]"))
        self.assertLess(message.index("[专项状态：迁移账单]"), message.rindex("叫醒"))
        self.assertTrue(message.endswith("叫醒"))
        self.assertNotIn("[管家规则]", message)
        self.assertNotIn("compact_instructions", ctx.options)  # No compaction: the session is rebuilt from the disk.
        self.assertIn("把账单迁到新库", message)

    def test_a_resumed_lead_gets_the_changing_parts_only(self):
        _, message = self.run_turn(session_id="sid")
        self.assertNotIn("把账单迁到新库", message)
        self.assertIn("话题（还没有）", message)

    def test_a_lead_whose_record_is_gone_goes_on_unchanged_and_says_so(self):
        projects.record_path(self.state, PID).unlink()
        ctx, message = self.run_turn()
        self.assertEqual(message, "叫醒")
        self.assertNotIn("tools", ctx.options)
        self.assertTrue(any("without a project record" in line for line in self.logs))

    def test_the_butler_turn_has_no_project_tools(self):
        main = SimpleNamespace(topic_id=butler.butler_topic_id(CHAT), chat_id=CHAT, message_id="om_1", job_id=3, kind="chat")
        with mock.patch.object(session, "take_snapshot", return_value="SNAP"):
            ctx, message = session.butler_turn(self.context(), main, "你好", None, mock.Mock(), self.logs.append)
        server = ctx.options["mcp_config"]["mcpServers"][butler.MCP_SERVER]
        self.assertNotIn(butler.LEAD_ENV, server["env"])
        self.assertNotIn("tools", ctx.options)
        self.assertIn("compact_instructions", ctx.options)
        self.assertTrue(message.startswith("[管家规则]"))

    def test_a_lead_turn_of_a_stopped_butler_is_not_one(self):
        write_json(self.state / settings.FILE, {"mode": "topics"})
        butler.forget_mode()
        ctx, message = self.run_turn()
        self.assertEqual(message, "叫醒")
        self.assertNotIn("tools", ctx.options)

    def test_the_lead_has_no_compaction_request(self):
        compaction = mock.Mock()
        job = self.job(tool_trace=SimpleNamespace(context_tokens=10 ** 7))
        session.compact_butler(compaction, self.context(), job)
        compaction.set_requested.assert_not_called()


class ConfigTest(Case):
    def test_the_lead_environment_is_handed_on_only_with_the_switch_on_and_a_lead(self):
        plain = butler.mcp_config(Path("/r"), self.state, "k", sys.executable)
        self.assertNotIn(butler.LEAD_ENV, plain["mcpServers"]["bridge"]["env"])
        lead = butler.mcp_config(Path("/r"), self.state, "k", sys.executable, lead=PID)
        self.assertEqual(lead["mcpServers"]["bridge"]["env"][butler.LEAD_ENV], PID)
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            off = butler.mcp_config(Path("/r"), self.state, "k", sys.executable, lead=PID)
            plain_off = butler.mcp_config(Path("/r"), self.state, "k", sys.executable)
        self.assertEqual(off, plain_off)  # With the switch off the lead's variable and the butler's are not handed on.
        self.assertEqual(plain["mcpServers"]["bridge"]["env"][projects.ENV], "1")
        self.assertNotIn(projects.ENV, plain_off["mcpServers"]["bridge"]["env"])

    def test_the_server_lists_the_leads_tools_when_told_and_the_butlers_otherwise(self):
        names = lambda env: [tool["name"] for tool in butler_mcp.tool_list(env)]  # noqa: E731
        self.assertEqual(names({butler.LEAD_ENV: PID}), LEAD_NAMES)
        self.assertEqual(names({}), list(butler_tool_specs.NAMES))
        self.assertFalse(set(butler_tool_specs.PROJECT_NAMES) & set(names({"COVEY_KNOWLEDGE": "1", "COVEY_BUTLER_RULES": "1"})))
        self.assertNotIn("project_answer", names({butler.LEAD_ENV: PID}))  # Phase 2's triage: defined, not listed.
        self.assertNotIn("project_escalate", names({butler.LEAD_ENV: PID}))
        self.assertEqual(butler_tool_specs.listed({}), butler_tool_specs.TOOLS)

    def test_the_lead_has_none_of_the_tools_that_start_topics_for_the_owner_or_change_settings(self):
        listed = set(names["name"] for names in butler_tool_specs.lead_tools())
        self.assertFalse(listed & {"topic_start", "topic_send", "topic_fork", "question_answer", "principle_save",
                                   "settings_panel", "topic_priority"})

    def test_a_call_to_a_project_tool_outside_a_lead_session_is_refused_by_the_server(self):
        server = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "k"}, submit=mock.Mock())
        answer = server.call({"name": "project_note", "arguments": {"text": "x"}})
        self.assertTrue(answer["isError"])
        self.assertIn("没有开 project_note", answer["content"][0]["text"])
        server.submit.assert_not_called()
        lead = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "k", butler.LEAD_ENV: PID},
                               submit=mock.Mock(return_value={"ok": True, "text": "好"}))
        self.assertFalse(lead.call({"name": "project_note", "arguments": {"text": "x"}})["isError"])
        self.assertTrue(lead.call({"name": "project_answer", "arguments": {}})["isError"])  # Not listed: not callable.


class ArgvTest(Case):
    def argv(self, **options):
        ctx = dataclasses.replace(codex_context(self.state, "stream-json"), options=options)
        return stream_argv(ClaudeBackend(), ctx)

    def test_tools_in_the_options_become_a_tools_whitelist_beside_the_mcp_tools(self):
        config = {"mcpServers": {"bridge": {}}}
        argv = self.argv(mcp_config=config, tools=("Read", "Grep", "Glob"))
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__bridge")
        self.assertIn("--mcp-config", argv)

    def test_without_the_option_there_is_no_tools_flag_and_the_butlers_argv_is_as_it_was(self):
        self.assertNotIn("--tools", self.argv())
        self.assertNotIn("--tools", self.argv(mcp_config={"mcpServers": {"bridge": {}}}))


class ToolsCase(ButlerTopicsCase):
    """The handlers against a live Bridge world: a lead's job, its project, the topics it opens."""

    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        enter_context(self, mock.patch.dict(os.environ, ON))
        settings.update(self.state, enabled=True)
        butler.forget_mode()
        self.record = projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", goal="目标",
                                                                      project_id=PID, now=NOW))[0]
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, harness_id="claude",
                    project=projects.project_field(PID, projects.LEAD))
        write_job(self.state, "lead-job", topic_id=LEAD, chat_id=CHAT, job_id=1, status="running",
                  message_id="lead-pid-1", user_text="收件箱：T41 做完了")

    def tool(self, name, job="lead-job", request_id=None, **args):
        request = Request(request_id or f"r{next(self.ids)}", name, args, job, 0.0)
        return butler_tools.TOOLS[name](self.env, request)

    def refused(self, name, **args):
        answer = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("rx", name, args, "lead-job", 0.0))
        self.assertFalse(answer["ok"], answer)
        return answer["text"]

    def reload(self):
        return projects.read_record(self.state, PID)


class ToolNamesTest(ToolsCase):
    """project_thread_start and project_thread_send became project_topic_start and project_topic_send (2026-11-16): the list
    names only the new ones, a call under an old name still runs (a session that began earlier, an older server)."""

    def test_the_list_has_only_the_new_names_and_a_call_under_an_old_one_runs_as_the_new(self):
        listed = [tool["name"] for tool in butler_mcp.tool_list({butler.LEAD_ENV: PID})]
        self.assertIn("project_topic_start", listed)
        self.assertIn("project_topic_send", listed)
        self.assertFalse({"project_thread_start", "project_thread_send"} & set(listed))
        sent = []
        server = butler_mcp.Server({butler.STATE_ENV: "/s", butler.JOB_ENV: "k", butler.LEAD_ENV: PID},
                                   submit=lambda state, name, args, job_key: sent.append(name) or {"ok": True, "text": "好"})
        for old, new in (("project_thread_start", "project_topic_start"), ("project_thread_send", "project_topic_send")):
            answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": old, "arguments": {}}})
            self.assertFalse(answer["result"]["isError"], answer)
            self.assertEqual(sent[-1], new)
        self.assertEqual(butler_tool_specs.current_name("topic_start"), "topic_start")

    def test_a_request_file_an_older_server_wrote_under_an_old_name_is_read_under_the_new(self):
        from covey.core import butler_requests
        for old, new in (("project_thread_start", "project_topic_start"), ("project_thread_send", "project_topic_send")):
            path = butler_requests.folder(self.state) / f"{old}.json"
            butler_requests._write(path, {"id": old, "tool": old, "args": {}, "job_key": "lead-job", "created_at": time.time()})
            self.assertEqual(butler_requests._request(path).tool, new)
            self.assertIn(new, butler_tools.TOOLS)
            self.assertNotIn(old, butler_tools.TOOLS)


class MemberToolsTest(ToolsCase):
    def start(self, request_id=None, **args):
        fields = dict(title="导出旧账单", prompt="把旧库 2024 年账单导成 csv", workspace="project", readonly=True, why="脚本在这个仓库")
        fields.update(args)
        return self.tool("project_topic_start", request_id=request_id, **fields)

    def test_a_thread_is_opened_marked_as_the_projects_and_recorded_before_its_first_turn(self):
        answer = self.start("r1")
        self.assertTrue(answer["ok"], answer)
        topic = answer["data"]["topic_id"]
        data = json.loads((self.state / "topics" / topic / "topic.json").read_text())
        self.assertEqual(data["project"], {"id": PID, "role": "thread"})
        self.assertEqual(self.reload()["threads"], [topic])
        self.assertTrue(projects.is_member_topic(self.state, topic))
        # The first turn waits in the queue: the thread's marker was written with its binding, not after.
        job = self.job("r1")
        self.assertEqual(job.topic_id, topic)
        self.assertEqual(projects.cells(self.state, CHAT), {topic: LEAD})

    def test_the_wake_up_message_is_not_quoted_to_the_thread_as_the_owners_words(self):
        self.assertTrue(self.start("r1")["ok"])
        job = self.job("r1")
        self.assertEqual(job.user_text, "把旧库 2024 年账单导成 csv")  # The card shows the task, not 收件箱：T41 做完了.
        self.assertIn("这是专项「迁移账单」的一个话题", job.prompt_text)
        self.assertNotIn("主人原话", job.prompt_text)
        self.assertNotIn("收件箱", job.prompt_text)
        self.assertNotIn("管家", job.prompt_text)

    def test_the_same_request_again_opens_nothing_new(self):
        first = self.start("r1")
        second = self.start("r1")
        self.assertEqual(first["data"]["topic_id"], second["data"]["topic_id"])
        self.assertEqual(len(self.reload()["threads"]), 1)

    def test_no_dispatch_card_is_made_for_a_lead(self):
        with mock.patch.dict(os.environ, {dispatch_cards.ENV: "1"}), \
                mock.patch.object(dispatch_cards, "created") as created:
            self.start("r1")
        created.assert_not_called()

    def test_the_butlers_own_topic_start_is_still_no_thread(self):
        answer = self.call("topic_start", "m1", title="查日志", prompt="看看", workspace="project", readonly=True,
                           user_requested=False, why="x")
        self.assertTrue(answer["ok"], answer)
        data = json.loads((self.state / "topics" / answer["data"]["topic_id"] / "topic.json").read_text())
        self.assertNotIn("project", data)

    def test_more_than_four_threads_work_at_once_whatever_an_older_style_said(self):
        projects.update(self.state, PID, lambda r: r.update(style={"spawn": "保守"}))  # An older record's 派活.
        for n in range(1, 7):
            answer = self.start(f"r{n}", title=f"第 {n} 件")
            self.assertTrue(answer["ok"], answer)
            write_job(self.state, "main-" + butler_topics_token(f"r{n}"), status="running", phase="做事")
        record = self.reload()
        self.assertEqual(len(record["threads"]), 6)
        self.assertEqual(len(project_lead.live_members(self.state, record)), 6)

    def test_finishing_counts_a_topic_waiting_for_the_owner_as_live(self):
        self.start("r1")
        write_job(self.state, "main-" + butler_topics_token("r1"), status="waiting_user", status_message="等你回答")
        self.start("r2", title="另一件")
        self.assertEqual(len(project_lead.live_members(self.state, self.reload())), 2)

    def test_a_failed_start_says_so_and_records_nothing(self):
        answer = self.start("r1", workspace="nowhere")
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["text"].startswith("没有开出话题："))
        self.assertEqual(self.reload()["threads"], [])

    def test_a_paused_or_finished_project_opens_no_thread_and_says_what_comes_next(self):
        projects.set_state(self.state, PID, projects.PAUSED)
        self.assertIn("专项现在是停住，不能开新话题", self.refused("project_topic_start", title="x", prompt="y", workspace="project",
                                                       readonly=True, why="z"))
        projects.set_state(self.state, PID, projects.DONE)
        words = self.refused("project_topic_start", title="x", prompt="y", workspace="project", readonly=True, why="z")
        self.assertIn("做完（已收尾）", words)
        self.assertIn("自动重开", words)


def butler_topics_token(request_id):
    from covey.app import butler_topics

    return butler_topics.request_token(SimpleNamespace(id=request_id))


class SendToolsTest(ToolsCase):
    def opened(self):
        data = self.tool("project_topic_start", request_id="open", title="导出", prompt="导出账单", workspace="project",
                         readonly=True, why="x")["data"]
        return data, self.job("open")

    def test_a_stopped_thread_gets_its_next_turn_from_the_lead(self):
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)
        write_job(self.state, job.message_id, status="done")
        answer = self.tool("project_topic_send", request_id="s1", thread=data["label"], text="再核对一遍金额", note="对账时发现的")
        self.assertTrue(answer["ok"], answer)
        nxt = self.job("s1")
        self.assertEqual((nxt.user_text, nxt.topic_id), ("再核对一遍金额", data["topic_id"]))
        self.assertIn("负责人把下一件事交给你", nxt.prompt_text)
        self.assertNotIn("管家转来你的", nxt.prompt_text)

    def test_a_running_thread_takes_it_as_a_supplement(self):
        data, job = self.opened()
        job.execution_started = True
        job.backend_control = SimpleNamespace(steer=mock.Mock(return_value=None), remote=False)
        answer = self.tool("project_topic_send", thread=data["topic_id"], text="顺便导出 2023 年的")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["data"].get("steered"), answer)
        sent = job.backend_control.steer.call_args.args[0]
        self.assertIn("专项「迁移账单」的负责人给", sent)
        self.assertNotIn("管家转来", sent)

    def ordinary(self, root, number, **fields):
        """An ordinary topic of the owner's private chat whose root message is ``root``: its id as the Bridge derives it."""
        from covey.app.topics import topic_target

        topic = topic_target(self.app.topics.apps["claude"], CHAT, {"root_id": root}).topic_id  # Writes its topic.json.
        path = self.state / "topics" / topic / "topic.json"
        write_topic(self.state, json.loads(path.read_text()), topic_id=topic, harness_id="claude", topic_number=number,
                    origin={"title": "别的"}, **fields)
        return topic

    def test_an_ordinary_topic_the_lead_writes_to_joins_the_project_first(self):
        """The owner, 2026-11-19: the lead opened new topics for two bugs T519 already covered, since it could not write to a
        topic outside the project. Now project_topic_send takes it in (join_topic, by the lead), then sends."""
        self.opened()
        other = self.ordinary("om_other", 77)
        answer = self.tool("project_topic_send", request_id="s77", thread="T77", text="顺便看下这个")
        found = projects.topic_project(self.state, other)
        self.assertIsNotNone(found, answer)
        self.assertEqual((found.id, found.role), (PID, projects.MEMBER))
        record = projects.read_record(self.state, PID)
        self.assertIn(other, record["threads"])
        self.assertEqual([(row["topic"], row["by"]) for row in record["joins"]][-1], (other, "lead"))

    def test_another_projects_topic_a_topic_taken_out_and_the_lead_are_refused(self):
        self.opened()
        other = self.ordinary("om_other", 77, project={"id": "0" * 12, "role": "thread"})
        answer = self.tool("project_topic_send", thread="T77", text="顺便看下这个")
        self.assertFalse(answer["ok"])
        self.assertNotIn(other, projects.read_record(self.state, PID)["threads"])
        kept = self.ordinary("om_kept", 78)
        project_proposals.change_topic(self.state, kept, lambda flags: flags.update(never_auto=True))
        answer = self.tool("project_topic_send", thread="T78", text="x")
        self.assertFalse(answer["ok"])
        self.assertIn("移出过", answer["text"])
        self.assertIsNone(projects.topic_project(self.state, kept))
        self.assertFalse(self.tool("project_topic_send", thread="T9999", text="x")["ok"])
        self.assertFalse(self.tool("project_topic_send", thread=LEAD, text="x")["ok"])  # The lead itself is no thread.


class StopToolTest(ToolsCase):
    """project_topic_stop: the lead stops one member topic for the owner (acceptance 2026-11-17: a topic waiting for an answer
    could not be stopped, and the project never wound up)."""

    def opened(self):
        data = self.tool("project_topic_start", request_id="open", title="导出", prompt="导出账单", workspace="project",
                         readonly=True, why="x")["data"]
        return data, self.job("open")

    def ask(self, data, job, token="q1"):
        """The job waits on a question, as a checkpoint or native question leaves it."""
        write_job(self.state, job.message_id, status="waiting_user", topic_id=data["topic_id"], chat_id=CHAT)
        write_json(self.state / "questions" / (token + ".json"), {
            "token": token, "topic_id": data["topic_id"], "chat_id": CHAT, "job_id": job.job_id, "job_key": job.message_id,
            "status": "pending", "created_at": 1.0, "questions": [{"question": "用哪个库？"}]})

    def question(self, token="q1"):
        return json.loads((self.state / "questions" / (token + ".json")).read_text())

    def test_it_is_listed_to_the_lead_and_not_to_the_butler(self):
        self.assertIn("project_topic_stop", [tool["name"] for tool in butler_tool_specs.lead_tools()])
        self.assertNotIn("project_topic_stop", butler_tool_specs.NAMES)

    def test_a_queued_task_is_stopped_and_the_topic_stays_in_the_project(self):
        data, job = self.opened()
        answer = self.tool("project_topic_stop", thread=data["label"], why="主人不要这个了")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["stopped"], 1)
        self.assertTrue(job.cancel_event.is_set())
        self.assertEqual(self.reload()["threads"], [data["topic_id"]])

    def test_a_topic_waiting_for_the_owners_answer_is_stopped_its_question_cancelled_for_the_reason(self):
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)  # Waiting for the answer: no task runs (or its runner is gone, as after a restart).
        self.ask(data, job)
        answer = self.tool("project_topic_stop", thread=data["topic_id"])
        self.assertTrue(answer["ok"], answer)
        question = self.question()
        self.assertEqual((question["status"], question["cancel_reason"]), ("cancelled", "lead_stop"))
        record = json.loads((self.state / "jobs" / (job.message_id + ".json")).read_text())
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("负责人按主人的意思停了这个话题", record["status_message"])
        # It counts as ended now: the project can wind up.
        self.assertEqual(project_lead.live_members(self.state, self.reload()), [])

    def test_a_launch_card_not_started_is_closed_and_waits_no_more(self):
        """T561, T562 (2026-11-19): opened twice by mistake, never started (their launch cards waited for a workspace), stopped
        twice by the lead with 请 0 个任务停下; the cards could still start them and kept them under 等我 and the web page's 待办."""
        from covey.core import board_read
        data, job = self.opened()
        self.app.runtime.unregister_reply_job(job)
        from covey.core.jobs import read_job_record, job_path
        write_job(self.state, "k-launch", topic_id=data["topic_id"], chat_id=CHAT, job_id=999, status="waiting_workspace")
        form = self.state / "launch-forms" / "f1.json"
        write_json(form, {"topic_id": data["topic_id"], "chat_id": CHAT, "status": "ready", "message_id": "om_launch",
                          "harness_id": "claude", "schema_version": 3})
        self.assertTrue(board_read.launch_card(self.state, data["topic_id"], CHAT))
        answer = self.tool("project_topic_stop", thread=data["label"])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["launches"], 1)
        self.assertIn("启动卡", answer["text"])
        self.assertEqual(json.loads(form.read_text())["status"], "closed")
        record = read_job_record(job_path(self.state, "k-launch"))
        self.assertEqual((record["status"], record.get("never_started")), ("cancelled", True))
        self.assertEqual(board_read.launch_card(self.state, data["topic_id"], CHAT), "")
        self.assertNotIn(data["topic_id"], [wait.topic for wait in board_read.waits(self.state, CHAT, time.time()).all])

    def test_only_the_projects_own_topics_can_be_stopped(self):
        data, job = self.opened()
        write_topic(self.state, {"topic_id": "ef" * 16}, topic_id="ef" * 16, chat_id=CHAT, harness_id="claude",
                    topic_number=77, origin={"title": "别的"}, root_id="om_other")
        answer = self.tool("project_topic_stop", thread="T77")
        self.assertFalse(answer["ok"])
        self.assertIn("不是这个专项的话题", answer["text"])
        self.assertFalse(job.cancel_event.is_set())
        self.assertFalse(self.tool("project_topic_stop", thread=LEAD)["ok"])
        self.assertIn("没有执行", self.refused_as_butler())  # Not a lead's job.

    def refused_as_butler(self):
        answer = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("rb", "project_topic_stop", {"thread": "T1"}, "other-job", 0.0))
        self.assertFalse(answer["ok"])
        return answer["text"]

    def test_a_finished_project_refuses(self):
        data, job = self.opened()
        projects.set_state(self.state, PID, projects.DONE)
        self.assertIn("做完（已收尾）", self.refused("project_topic_stop", thread=data["label"]))

    def test_the_lead_rules_and_the_butlers_say_how_to_stop_a_topic(self):
        from covey.app import prompts
        self.assertIn("project_topic_stop", project_lead.lead_rules())
        self.assertIn("project_send", prompts.PROJECT_RULE)
        self.assertIn("停", prompts.PROJECT_RULE)


class RecordToolsTest(ToolsCase):
    def test_a_note_goes_on_the_card_and_a_decision_into_the_log(self):
        one = self.tool("project_note", text="导出完成，开始对账", report=True, kind="milestone")
        self.assertTrue(one["ok"])
        self.assertIn("会按风格告诉主人", one["text"])
        quiet = self.tool("project_note", text="换了批量大小", kind="note", report=True)
        self.assertIn("按风格不会打扰主人", quiet["text"])
        decided = self.tool("project_note", text="先迁 2024 年", decision=True)
        self.assertIn("决定日志", decided["text"])
        record = self.reload()
        self.assertEqual([(n["text"], n["report"], n["kind"], n["job"]) for n in record["notes"]],
                         [("导出完成，开始对账", True, "milestone", "lead-job"), ("换了批量大小", True, "note", "lead-job"),
                          ("先迁 2024 年", False, "note", "lead-job")])
        self.assertEqual([d["text"] for d in record["decisions"]], ["先迁 2024 年"])

    def test_the_goal_the_lead_writes_is_its_own_and_the_opening_goal_stays(self):
        """T550 (2026-11-19): a project's goal was the words it was opened with (the work it took over), shown as its goal on
        the web page. The lead writes one with project_goal; the record's ``goal`` stays, and the state pack asks for one
        until it is written."""
        self.assertIn("还没用 project_goal", project_lead.state_pack(self.state, self.reload(), "lead-job", full=False, receipts=[]))
        answer = self.tool("project_goal", text="账单全部迁到新库， 对账一致后收尾")
        self.assertTrue(answer["ok"], answer)
        record = self.reload()
        self.assertEqual((record["lead_goal"]["text"], record["lead_goal"]["job"], record["goal"]),
                         ("账单全部迁到新库， 对账一致后收尾", "lead-job", "目标"))
        self.assertEqual(project_lead.lead_goal(record), "账单全部迁到新库， 对账一致后收尾")
        pack = project_lead.state_pack(self.state, record, "lead-job", full=True, receipts=[])
        self.assertIn("你写下的（project_goal）：账单全部迁到新库", pack)
        self.assertNotIn("还没用 project_goal", pack)
        self.assertIn("text 不能为空", self.refused("project_goal", text=" "))

    def test_the_lists_are_capped(self):
        for number in range(project_lead.NOTES_KEPT + 5):
            self.tool("project_note", text=f"n{number}")
        notes = self.reload()["notes"]
        self.assertEqual((len(notes), notes[0]["text"], notes[-1]["text"]),
                         (project_lead.NOTES_KEPT, "n5", f"n{project_lead.NOTES_KEPT + 4}"))
        self.assertIn("text 不能为空", self.refused("project_note", text="  "))

    def test_a_memory_entry_is_kept_once_and_a_bad_kind_is_refused(self):
        first = self.tool("project_remember", kind="pitfall", text="旧库时区是 UTC+0")
        again = self.tool("project_remember", kind="pitfall", text="旧库时区是 UTC+0")
        self.assertEqual((first["data"]["new"], again["data"]["new"], first["data"]["id"]), (True, False, again["data"]["id"]))
        record = self.reload()
        self.assertEqual([(row["kind"], row["text"], row["source"]) for row in project_memory.live(self.state, record)],
                         [("constraint", "旧库时区是 UTC+0", "lead")])
        self.assertNotIn("memory", record)  # Not in the record any more: the card reads the file.
        self.assertTrue(project_memory.memory_path(self.state, PID).exists())
        self.assertIn("kind 要是", self.refused("project_remember", kind="style", text="x"))

    def test_a_memory_the_owner_deleted_is_not_noted_again_and_the_pack_lists_it(self):
        entry = self.tool("project_remember", kind="decision", text="先迁 2024 年")["data"]["id"]
        self.assertTrue(project_memory.delete(self.state, PID, entry))
        self.assertIn("删掉过", self.refused("project_remember", kind="decision", text="先迁 2024 年"))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True)
        self.assertIn("主人删掉的记忆", pack)
        self.assertIn("[decision] 先迁 2024 年", pack)
        self.assertNotIn("专项记忆：", pack)

    def test_the_style_changes_by_the_items_given_and_leaves_one_line_in_the_memory(self):
        shown = self.tool("project_style")
        self.assertEqual(shown["data"]["style"], project_lead.STYLE_DEFAULT)
        self.tool("project_style", report="ask_only")
        self.tool("project_style", detail="全", spawn="积极")  # spawn is no style item any more: not kept.
        record = self.reload()
        self.assertEqual(record["style"], {"report": "ask_only", "detail": "全"})
        rows = project_memory.live(self.state, record)
        self.assertEqual([m["kind"] for m in rows], ["style"])
        self.assertEqual(rows[0]["text"], "汇报风格：report=ask_only；detail=全")
        self.assertIn("detail", self.refused("project_style", detail="疯狂"))
        spec = next(tool for tool in butler_tool_specs.PROJECT_TOOLS if tool["name"] == "project_style")
        self.assertEqual(set(spec["input_schema"]["properties"]), {"report", "detail"})
        self.assertNotIn("派活", spec["description"])

    def test_the_style_is_kept_for_the_next_project_only_with_the_switch(self):
        self.tool("project_style", report="ask_only")
        self.assertFalse((self.state / project_lead.STYLE_FILE).exists())  # Off: nothing is kept.
        with mock.patch.dict(os.environ, {projects.STYLE_KEEP_ENV: "1"}):
            self.tool("project_style", detail="全")
            self.assertEqual(project_lead.last_style(self.state), {"report": "ask_only", "detail": "全"})
            self.tool("project_style")  # Only reading it changes nothing.
        self.assertEqual(json.loads((self.state / project_lead.STYLE_FILE).read_text())["detail"], "全")
        (self.state / project_lead.STYLE_FILE).write_text('{"report": "bogus", "detail": "简", "spawn": "积极"}')
        self.assertEqual(project_lead.last_style(self.state), {"detail": "简"})  # Each choice is checked.
        (self.state / project_lead.STYLE_FILE).write_text("[1]")
        self.assertEqual(project_lead.last_style(self.state), {})
        self.assertEqual(project_lead.last_style(self.state / "nowhere"), {})

    def test_finishing_needs_the_threads_to_have_ended_and_ends_the_project_once(self):
        thread = self.tool("project_topic_start", request_id="open", title="导出", prompt="导出账单", workspace="project",
                           readonly=True, why="x")["data"]["topic_id"]
        write_job(self.state, "main-" + butler_topics_token("open"), status="running")
        blocked = self.tool("project_finish", summary="都做完了")
        self.assertFalse(blocked["ok"])
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        write_job(self.state, "main-" + butler_topics_token("open"), status="done", final_text="完成")
        done = self.tool("project_finish", summary="账单迁完，对账一致")
        self.assertTrue(done["ok"], done)
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"], record["state_job"]), ("done", "账单迁完，对账一致", "lead-job"))
        self.assertEqual(record["notes"][-1]["kind"], "finish")
        self.assertIn("做完（已收尾）", self.refused("project_finish", summary="再来一次"))
        self.assertTrue(thread)

    def test_a_project_tidy_gathered_is_not_finished_before_the_owner_speaks_or_the_lead_hands_out_work(self):
        projects.update(self.state, PID, lambda record: record.update(origin={"kind": "tidy", "proposal": "p"}))
        blocked = self.tool("project_finish", summary="收进来的话题都做完了")
        self.assertFalse(blocked["ok"])
        self.assertIn("收进来的话题做完不等于专项完成", blocked["text"])
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        self.assertIn("不要收尾", projects_app.Projects.gathered(self.state, self.reload()))
        for opened in ({"owner_at": NOW}, {"acted": [{"job": "lead-job", "what": "start", "at": NOW}]}):
            with self.subTest(opened=opened):
                projects.update(self.state, PID, lambda record: (record.pop("owner_at", None), record.pop("acted", None),
                                                                 record.update(opened)))
                self.assertEqual(project_tools.finish_gate(self.reload()), "")
        for kind in ("direct", "upgrade"):  # The owner's own words began these.
            projects.update(self.state, PID, lambda record: (record.pop("acted", None), record.update(origin={"kind": kind})))
            self.assertEqual(project_tools.finish_gate(self.reload()), "")

    def test_the_triage_tools_say_it_is_off_without_the_switch(self):
        for name, args in (("project_answer", {"thread": "T1", "answers": {}}), ("project_escalate", {"thread": "T1", "why": "x"})):
            answer = self.tool(name, **args)
            self.assertFalse(answer["ok"])
            self.assertIn("问题分流没有开", answer["text"])


class RefusalTest(ToolsCase):
    def test_only_a_leads_job_may_call_them(self):
        butler_job = write_job(self.state, "main-job-2", topic_id=butler.butler_topic_id(CHAT), chat_id=CHAT, job_id=5, status="running")
        self.assertTrue(butler_job)
        request = Request("q1", "project_note", {"text": "x"}, "main-job-2", 0.0)
        answer = ButlerTools(self.env, butler_tools.TOOLS).execute(request)
        self.assertFalse(answer["ok"])
        self.assertIn("只有专项的负责人", answer["text"])
        write_job(self.state, "thread-job", topic_id="ab" * 16, chat_id=CHAT, job_id=6, status="running")
        gone = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("q2", "project_note", {"text": "x"}, "thread-job", 0.0))
        self.assertFalse(gone["ok"])

    def test_with_the_switch_off_they_are_refused(self):
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            answer = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("q3", "project_note", {"text": "x"}, "lead-job", 0.0))
        self.assertFalse(answer["ok"])
        self.assertEqual(self.reload().get("notes"), None)

    def test_the_loop_runs_a_lead_job_and_not_a_finished_one(self):
        ok = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("q4", "project_note", {"text": "一句"}, "lead-job", 0.0))
        self.assertTrue(ok["ok"], ok)
        write_job(self.state, "lead-job", status="done")
        late = ButlerTools(self.env, butler_tools.TOOLS).execute(Request("q5", "project_note", {"text": "二句"}, "lead-job", 0.0))
        self.assertFalse(late["ok"])


class OutcomeTest(ToolsCase):
    def line(self, text="", job="lead-job"):
        return butler_reply.outcome_words(self.state, job, text, lead=True)

    def test_a_turn_that_opened_a_thread_says_so_as_the_butler_would(self):
        self.tool("project_topic_start", request_id="r1", title="导出旧账单", prompt="导出", workspace="project",
                  readonly=True, why="x")
        self.assertRegex(self.line(), r"^↪ 开了 T\d+「导出旧账单」")

    def test_else_the_note_it_recorded_the_finish_or_the_reply(self):
        self.assertEqual(self.line("好的，我先看一下情况再决定"), "✅ 已答：好的，我先看一下情况再决定")
        self.tool("project_note", text="导出完成，开始对账")
        self.assertEqual(self.line("好的"), "📌 记进展：导出完成，开始对账")
        self.tool("project_finish", summary="账单迁完")
        self.assertEqual(self.line("好的"), "🏁 专项完成：账单迁完")

    def test_the_butlers_line_never_looks_for_a_project(self):
        self.tool("project_note", text="一句")
        self.assertEqual(butler_reply.outcome_words(self.state, "lead-job", "好的"), "✅ 已答：好的")

    def test_say_outcome_takes_the_lead_role_and_not_a_topic(self):
        said = []
        cards = SimpleNamespace(say_outcome=lambda ctx, job, line: said.append(line) or True)
        turn = SimpleNamespace(status="done", verified_delivery=None, verified_outcome=None)
        job = SimpleNamespace(progress=object(), topic_id=LEAD, chat_id=CHAT, message_id="lead-pid-1", job_id=1)
        self.tool("project_note", text="一句")
        delivery = SimpleNamespace(job=job, ctx=self.ctx, turn=turn, cards=cards, runtime=self.app.runtime)
        with mock.patch.object(butler_reply.placement, "layout", return_value=butler_reply.placement.AREA), \
                mock.patch.object(butler_reply, "job_key", return_value="lead-job"):
            self.assertTrue(butler_reply.say_outcome(delivery))
        self.assertEqual(said, ["📌 记进展：一句"])
        job.topic_id = "ab" * 16
        with mock.patch.object(butler_reply.placement, "layout", return_value=butler_reply.placement.AREA):
            self.assertFalse(butler_reply.say_outcome(delivery))


class HandoffTest(unittest.TestCase):
    def test_a_lead_hands_work_on_in_its_own_words_not_as_the_butler_for_the_owner(self):
        lead = butler_handoff.Asked(lead="迁移账单")
        for kind in (butler_handoff.START, butler_handoff.NEXT, butler_handoff.STEER):
            text = butler_handoff.note(kind, label="T41", title="导出", owner=lead)
            self.assertIn("迁移账单", text)
            self.assertNotIn("管家", text)
            self.assertNotIn("主人在私聊", text)
        self.assertEqual(butler_handoff.thread_head(butler_handoff.NEXT, lead), "负责人转来消息")
        self.assertEqual(butler_handoff.thread_head(butler_handoff.STEER, lead), "负责人转来补充")
        self.assertEqual(butler_handoff.shown(lead, "做这个", ()), "做这个")
        prompt = butler_handoff.handoff_prompt(butler_handoff.START, label="T41", title="导出", task="做这个", owner=lead)
        self.assertNotIn("主人原话", prompt)

    def test_the_butlers_notes_are_as_they_were(self):
        owner = butler_handoff.Asked()
        self.assertIn("管家", butler_handoff.note(butler_handoff.START, label="T41", title="导出", owner=owner))
        self.assertEqual(butler_handoff.thread_head(butler_handoff.NEXT, owner), "管家转来你的消息")


class OffTest(Case):
    def test_with_the_switch_off_the_turn_and_the_tool_list_are_the_butlers_as_before(self):
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            job = SimpleNamespace(topic_id=LEAD, chat_id=CHAT, message_id="lead-1", job_id=1, kind="chat")
            ctx = dataclasses.replace(codex_context(self.state, "stream-json"), state_dir=self.state)
            out, message = session.butler_turn(ctx, job, "叫醒", None, mock.Mock(), self.logs.append)
            self.assertIs(out, ctx)
            self.assertEqual(message, "叫醒")
            self.assertEqual([tool["name"] for tool in butler_mcp.tool_list({})], list(butler_tool_specs.NAMES))
            self.assertEqual(project_lead.settle_session(ctx, job, SimpleNamespace(), SimpleNamespace(), 1, "s"), (1, "s"))
            self.assertFalse(butler.is_lead(LEAD, CHAT))


if __name__ == "__main__":
    unittest.main()

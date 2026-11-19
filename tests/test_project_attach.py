"""A new matter goes into the project it belongs to (COVEY_PROJECTS=1): the butler's snapshot lists the projects on and folded
away, its rules and tool descriptions say to hand the matter to that project's lead (project_send, which opens a folded one
again), and with the switch off the rules and the snapshot are what they were."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import butler as session
from covey.app import prompts
from covey.core import butler_tool_specs, projects

CHAT = "oc_owner_dm"
GROUP = ("waiting", "result", "running", "opened")


class AttachTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {projects.ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, pid, name, goal="", chat=CHAT, **fields):
        record = projects.new_record(chat_id=chat, name=name, goal=goal, project_id=pid, now=1000.0)
        record.update(fields)
        projects.create(self.state, record)

    def groups(self):
        return session.snapshot_groups(SimpleNamespace(state_dir=self.state), CHAT, time.time())

    def test_the_snapshot_lists_the_projects_on_and_folded_away_with_a_goal_and_no_others(self):
        self.make("aaa111", "迁移订单库", "把订单库迁到新集群，周末前要有结果\n第二行不要")
        self.make("bbb222", "整理周报", "每周一出周报", state="paused", state_reason=projects.IDLE)
        self.make("ccc333", "停住的", "x", state="paused", state_reason="stalled")
        self.make("ddd444", "做完很久的", "x", state="done")  # Finished 1000 s after the epoch: long over DONE_SHOWN.
        self.make("ddd555", "刚做完的", "x", state="done", updated_at=time.time() - 3600)
        self.make("eee555", "取消的", "x", state="cancelled")
        self.make("fff666", "别的聊天", "x", chat="oc_other")
        rows = self.groups()["projects"]
        self.assertEqual(list(rows), ["aaa111", "bbb222", "ccc333", "ddd555"])
        self.assertIn("做完，主人再说话会重开", rows["ddd555"])  # The owner's words for it still reach its lead (T574).
        self.assertEqual(rows["aaa111"], "aaa111 · 迁移订单库 · 在做 · 目标：把订单库迁到新集群，周末前要有结果")
        self.assertIn("收起", rows["bbb222"])
        self.assertIn("停住", rows["ccc333"])

    def test_it_reads_as_a_section_and_only_the_change_shows_next_time(self):
        self.make("aaa111", "迁移订单库", "迁库")
        groups = self.groups()
        full = session.render_snapshot(groups, {}, full=True, at="11-17 10:00", receipts=True)
        self.assertIn("专项：\n- aaa111 · 迁移订单库 · 在做 · 目标：迁库", full)
        projects.update(self.state, "aaa111", lambda record: record.update(state="paused", state_reason=projects.IDLE))
        later = session.render_snapshot(self.groups(), groups, full=False, at="11-17 11:00", receipts=True)
        self.assertIn("专项：\n- aaa111 · 迁移订单库 · 收起", later)
        same = session.render_snapshot(self.groups(), self.groups(), full=False, at="11-17 12:00", receipts=True)
        self.assertNotIn("专项", same)
        projects.update(self.state, "aaa111", lambda record: record.update(state="cancelled"))
        gone = session.render_snapshot(self.groups(), self.groups() | {"projects": {"aaa111": "x"}}, full=False, at="t", receipts=True)
        self.assertIn("不再在“专项”：", gone)

    def test_no_project_is_no_section_on_a_full_snapshot(self):
        text = session.render_snapshot(self.groups(), {}, full=True, at="t", receipts=True)
        self.assertNotIn("专项", text)

    def test_off_the_snapshot_has_no_projects_group_and_reads_as_before(self):
        self.make("aaa111", "迁移订单库", "迁库")
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            groups = self.groups()
            self.assertEqual(set(groups), set(GROUP))
            self.assertNotIn("专项", session.render_snapshot(groups, {}, full=True, at="t", receipts=True))

    def test_the_rule_is_there_only_with_the_projects_and_the_rules_around_it_do_not_change(self):
        on = prompts.butler_rules()
        with mock.patch.dict(os.environ, {projects.ENV: ""}):
            off = prompts.butler_rules()
            self.assertEqual(prompts.project_rules(), ())
        self.assertNotIn(prompts.PROJECT_RULE, off)
        self.assertEqual(on.split("\n"), [*off.split("\n")[:-1], prompts.PROJECT_RULE, off.split("\n")[-1]])

    def test_the_rule_and_the_tool_words_say_what_the_owner_decided_and_use_the_terms_list(self):
        rule = prompts.PROJECT_RULE
        for word in ("project_send", "同一件事", "重新展开", "拿不准", "普通话题", "这是 X 的事"):
            self.assertIn(word, rule)
        self.assertEqual(rule.count("\n"), 0)
        tools = {tool["name"]: tool for tool in butler_tool_specs.BUTLER_PROJECT_TOOLS}
        self.assertIn("重新展开", tools["project_send"]["description"])
        self.assertIn("拿不准", tools["project_send"]["description"])
        self.assertIn("收起", tools["projects_list"]["description"])
        for text in (rule, *(tool["description"] for tool in tools.values())):
            for old in ("方向", "专员", "长活", "快事", "线程"):
                self.assertNotIn(old, text)

    def test_no_prompt_tool_or_card_text_says_方向_or_专员_as_a_noun(self):
        import json
        import lzma

        golden = Path(__file__).resolve().parent / "golden" / "cards.json.xz"
        self.assertNotIn("方向", lzma.decompress(golden.read_bytes()).decode("utf-8"))
        texts = [prompts.butler_rules(), json.dumps(butler_tool_specs.listed({"COVEY_PROJECTS": "1", "COVEY_BUTLER_RULES": "1"}),
                                                   ensure_ascii=False)]
        for text in texts:
            for word in ("方向", "专员"):
                self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()

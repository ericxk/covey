"""A new matter goes into the project it belongs to (COVEY_PROJECTS=1): the butler's snapshot lists the projects on and folded
away, its rules and tool descriptions say to hand the matter to that project's lead (project_send, which opens a folded one
again), and with the switch off the rules and the snapshot are what they were."""
from __future__ import annotations

import json
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
        # The ones on first, then stopped, folded away and finished (the order a long list is cut in).
        self.assertEqual(list(rows), ["aaa111", "ccc333", "bbb222", "ddd555"])
        self.assertIn("做完，主人再说话会重开", rows["ddd555"])  # The owner's words for it still reach its lead (T574).
        self.assertEqual(rows["aaa111"], "aaa111 · 迁移订单库 · 在做 · 目标：把订单库迁到新集群，周末前要有结果")
        self.assertIn("收起", rows["bbb222"])
        self.assertIn("停住", rows["ccc333"])

    def test_a_project_row_reads_the_goal_the_lead_wrote_before_the_old_field(self):
        self.make("aaa111", "迁移订单库", "老目标", lead_goal={"text": "负责人定的目标\n第二行", "at": 1.0})
        self.make("bbb222", "整理周报", "每周一出周报")
        rows = self.groups()["projects"]
        self.assertTrue(rows["aaa111"].endswith("目标：负责人定的目标 第二行"), rows["aaa111"])
        self.assertTrue(rows["bbb222"].endswith("目标：每周一出周报"), rows["bbb222"])

    def test_the_rule_names_the_days_a_finished_project_stays_from_the_constant(self):
        self.assertIn(f"{projects.DONE_SHOWN // 86400} 天内还在快照里", prompts.PROJECT_RULE)
        self.assertIs(session.DONE_SHOWN, projects.DONE_SHOWN)
        self.assertEqual(projects.DONE_SHOWN, 14 * 86400)

    def test_a_long_list_shows_the_newest_on_first_and_points_to_projects_list_for_the_rest(self):
        for number in range(session.PROJECT_ROWS + 2):
            self.make(f"p{number:05d}", f"专项{number}", updated_at=1000.0 + number)
        self.make("zzz999", "收起的", state="paused", state_reason=projects.IDLE, updated_at=5000.0)
        groups = self.groups()
        self.assertEqual(list(groups["projects"])[:2], [f"p{session.PROJECT_ROWS + 1:05d}", f"p{session.PROJECT_ROWS:05d}"])
        full = session.render_snapshot(groups, {}, full=True, at="11-17 10:00", receipts=True)
        self.assertIn("- 另有 3 个，用 projects_list 看", full)
        self.assertNotIn("收起的", full)  # Folded away after the ones on: past the rows shown.

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
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            groups = self.groups()
            self.assertEqual(set(groups), set(GROUP))
            self.assertNotIn("专项", session.render_snapshot(groups, {}, full=True, at="t", receipts=True))

    def test_the_rule_is_there_only_with_the_projects_and_the_rules_around_it_do_not_change(self):
        on = prompts.butler_rules()
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            off = prompts.butler_rules()
            self.assertEqual(prompts.project_rules(), ())
        self.assertNotIn(prompts.PROJECT_RULE, off)
        self.assertNotIn(prompts.ASKED_RULE, off)
        self.assertEqual(on.split("\n"), [*off.split("\n")[:-1], prompts.PROJECT_RULE, prompts.ASKED_RULE, off.split("\n")[-1]])

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



class BoardTest(AttachTest):
    """The 看板's 专项 part as projects grow (core/projects.py MAX_ACTIVE 50, folded ones piling up): what asks for him first,
    a block each for the first SHOWN on, the rest and the folded ones a line each in one fold."""

    def view(self):
        from covey.app.digest import Digest

        fake = SimpleNamespace(where=lambda chat: "", urls=lambda ctx, pairs: {}, pending=lambda ctx, chat: [])
        return Digest.projects_view(fake, SimpleNamespace(state_dir=self.state), CHAT,
                                    {"waiting": [{"topic": "w" * 32}], "running": [], "done": []})

    def test_waiting_ones_first_folded_ones_last_and_the_rest_by_their_last_change(self):
        self.make("aaa111", "老的", updated_at=1000.0)
        self.make("bbb222", "新的", updated_at=3000.0)
        self.make("ccc333", "等你的", threads=["w" * 32], updated_at=500.0)
        self.make("ddd444", "收起的", state="paused", state_reason=projects.IDLE, updated_at=9000.0)
        found = self.view()["projects"]
        self.assertEqual([entry["name"] for entry in found], ["等你的", "新的", "老的", "收起的"])
        self.assertEqual([bool(entry.get("folded")) for entry in found], [False, False, False, True])
        self.assertNotIn("_at", found[0])

    def test_past_the_blocks_and_folded_away_each_is_a_line_in_one_fold(self):
        from covey.cards import board
        from covey.cards.elements import find_element

        live = [{"name": f"在做{n}", "goal": "", "topics": [], "waiting": 0, "running": 1, "stopped": 0, "url": ""}
                for n in range(board.SHOWN + 2)]
        folded = [{"name": "收起1", "goal": "", "topics": [], "waiting": 0, "running": 0, "stopped": 0, "folded": True,
                   "url": "https://applink.example/p"}]
        view = {"waiting": [], "running": [], "done": [], "stale": [], "stamp": "08:00", "projects": live + folded}
        card = board.board_card(view)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn(f"在做{board.SHOWN - 1}", text)
        fold = find_element(card, "board_projects")
        self.assertEqual(fold["header"]["title"]["content"], "另有 2 个专项 · 收起的专项 1 个")
        lines = [element["content"] for element in fold["elements"]]
        self.assertEqual(len(lines), 3)
        self.assertIn(f"在做{board.SHOWN}", lines[0])
        self.assertIn("[收起1](https://applink.example/p)", lines[2])
        self.assertIn("收起", lines[2])
        self.assertIsNone(find_element(board.board_card({**view, "projects": live[:2]}), "board_projects"))


if __name__ == "__main__":
    unittest.main()

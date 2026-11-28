"""The lead answers for the project's goal (docs/projects.md 负责人对照目标跟进 and 分流的依据, COVEY_COLLAB=1; T634 包 D).

- L1: a 目标检查表 the lead keeps (core/project_checklist.py, project_checklist) and project_finish's gate on an item not met
  or a 协作 gap not dealt with: two topics say they are done while the baseline check of the goal is missing, the finish is
  refused naming it, the lead arranges it, and only then may it finish.
- L2: ordinary receipts that come close together wake the lead once, a start, a choice, a question or a gap at once, a
  consult's receipt never; a wake-up turn that read only results the owner was sent and consults' receipts does not retell
  them; a consult's tokens count in its project's budget once.
- M5: project_answer stands on grounds the Bridge checks: the lead's notes (待核对参考), a line voided, done, superseded or
  only for that once, a line with no origin and a made-up one are refused and the lead is told to escalate; the owner's own
  line that still holds, or his own message in the project, answers, and the grounds are on the answer and its row.

- 简报 (docs/projects.md 项目简报): the lead writes the project's background and the actions its goal is split into
  (core/project_actions.py, project_goal's ``background``, project_checklist's ``actions``), opens topics on an action or an
  item, and reads it as a short block of its pack; an action's state is read off its items and topics, never kept.

With COVEY_COLLAB off none of it changes anything: each part says so here, and the older tests of each part still pass."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.records import write_job, write_topic

from covey.app import project_facts, project_lead, project_loop, triage as flow
from covey.core import butler, butler_mcp, butler_tool_specs, collab, collab_records, project_library, project_memory
from covey.core import projects, triage, turn_source
from covey.core.jobs import job_key
from covey.core.state import read_object, write_json
from covey.core.turn_words import RESULTS

from test_project_lead import CHAT, LEAD, LEAD_NAMES, NOW, PID, Case, ToolsCase, butler_topics_token
from test_project_loop import Loop
from test_project_triage import CHOICE, TOKEN, question

THREAD = "b" * 32
COLLAB = {turn_source.SWITCH: "1"}
# A 目标检查表 as project_checklist leaves it in the record (core/project_checklist.py): c1 not met, delivered by THREAD; c2 met
# while it needs c1.
CHECKLIST = {"items": [
    {"id": "c1", "criterion": "导出 2024 年账单", "topic": THREAD, "needs": [], "evidence": "", "gap": "", "state": "open",
     "at": NOW, "job": "lead-1"},
    {"id": "c2", "criterion": "对账一致", "topic": "", "needs": ["c1"], "evidence": "对账报告", "gap": "", "state": "done",
     "at": NOW, "job": "lead-1"}], "closed": {}, "next": "c3", "at": NOW, "job": "lead-1"}
TRIAGE = {triage.ENV: "1"}


def checklist():
    """core/project_checklist.py, imported where a test uses it (scripts/test_map.py prove runs this module on the code
    before it, where the other tests fail on what they check, not on an import)."""
    from covey.core import project_checklist
    return project_checklist


def actions():
    """core/project_actions.py, imported where a test uses it (see ``checklist``)."""
    from covey.core import project_actions
    return project_actions


def label_of(state, topic) -> str:
    return "T" + str(json.loads((Path(state) / "topics" / topic / "topic.json").read_text())["topic_number"])


class ChecklistTest(unittest.TestCase):
    """core/project_checklist.py on a record of its own: what an item is, what a change may do, the gaps, what is not met."""

    def setUp(self):
        self.pc = checklist()

    def test_items_get_ids_in_order_and_a_change_replaces_only_what_it_names(self):
        record = {"id": PID}
        touched = self.pc.apply(record, [{"criterion": "导出 2024 年账单"}, {"criterion": "对账一致", "needs": ["c1"]}],
                                          job="j1", now=NOW)
        self.assertEqual(touched, ["c1", "c2"])
        self.assertEqual([(item["id"], item["state"], item["needs"], item["job"]) for item in self.pc.items(record)],
                         [("c1", "open", [], "j1"), ("c2", "open", ["c1"], "j1")])
        self.pc.apply(record, [{"id": "c1", "state": "done", "evidence": "提交 abc123"}], job="j2", now=NOW + 1)
        first, second = self.pc.items(record)
        self.assertEqual((first["state"], first["evidence"], first["criterion"], first["job"], second["state"]),
                         ("done", "提交 abc123", "导出 2024 年账单", "j2", "open"))
        self.assertEqual(record["checklist"]["next"], "c3")
        self.assertTrue(self.pc.kept(record))
        self.assertFalse(self.pc.kept({"id": PID}))

    def test_what_does_not_fit_is_refused_and_nothing_changes(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "导出"}], now=NOW)
        before = copy.deepcopy(record)
        for rows, words in (([{"id": "c1", "state": "done"}], "要写 evidence"),
                            ([{"id": "c1", "state": "blocked"}], "要写卡在哪"),
                            ([{"id": "c1", "state": "dropped"}], "为什么不做了"),
                            ([{"id": "c9", "gap": "x"}], "没有条目 c9"),
                            ([{"criterion": "对账", "needs": ["c7"]}], "c7 不在检查表里"),
                            ([{"id": "c1", "needs": ["c1"]}], "不能依赖自己"),
                            ([{"gap": "没写标准"}], "要写 criterion"),
                            ([{"id": "c1", "state": "finished"}], "state 要是"),
                            ([{"criterion": f"第 {n} 条"} for n in range(self.pc.MAX_ITEMS)], "最多 20 条"),
                            ([{"criterion": "好的一条"}, {"id": "c1", "state": "done"}], "要写 evidence")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                self.pc.apply(record, rows, now=NOW + 1)
            self.assertEqual(record, before)  # The good row of a refused call is not written either.

    def test_only_the_items_not_met_count_against_the_limit_and_the_whole_list_has_a_ceiling(self):
        """W2-C: twenty items done leave room for twenty more not met; the 21st not met is refused, and so is the 101st item."""
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": f"做完的 {n}", "state": "done", "evidence": f"提交 {n}"}
                               for n in range(self.pc.MAX_ITEMS)], now=NOW)
        self.pc.apply(record, [{"criterion": f"没做的 {n}"} for n in range(self.pc.MAX_ITEMS)], now=NOW + 1)
        self.assertEqual(len(self.pc.items(record)), 2 * self.pc.MAX_ITEMS)
        with self.assertRaisesRegex(ValueError, "没满足的条目最多 20 条，这次会有 21 条"):
            self.pc.apply(record, [{"criterion": "再一条"}], now=NOW + 2)
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": f"第 {n} 条", "state": "done", "evidence": "x"} for n in range(self.pc.MAX_TOTAL)],
                      now=NOW)
        with self.assertRaisesRegex(ValueError, "一共最多 100 条，这次会有 101 条"):
            self.pc.apply(record, [{"criterion": "多一条", "state": "done", "evidence": "x"}], now=NOW + 1)

    def test_an_item_turned_dropped_names_one_of_the_owners_words_the_bridge_found(self):
        """W2-C: ``bases`` (what the lead's tool checked) holds the owner's words; a drop names one of them in its evidence. One
        dropped before is left alone when it is changed again; with ``bases`` None (no tool) nothing is checked."""
        self.assertEqual(self.pc.bases_of("主人说不做了，依据msg:om_abc1、ask:0123abcd；ask:pause-1700000000 x:no"),
                         ["msg:om_abc1", "ask:0123abcd", "ask:pause-1700000000"])
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "导出"}, {"criterion": "旧格式", "state": "dropped", "gap": "主人说不要了"}], now=NOW)
        for evidence in ("", "主人说过", "msg:om_other"):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, "c1 标 dropped 要带主人的依据"):
                self.pc.apply(record, [{"id": "c1", "state": "dropped", "gap": "主人说不做", "evidence": evidence}],
                              bases={"msg:om_said"}, now=NOW + 1)
        self.assertEqual(self.pc.items(record)[0]["state"], "open")
        self.pc.apply(record, [{"id": "c1", "state": "dropped", "gap": "主人说不做", "evidence": "msg:om_said"}],
                      bases={"msg:om_said"}, now=NOW + 2)
        self.assertEqual(self.pc.items(record)[0]["state"], "dropped")
        # Dropped before (c2, with no basis at all): a later change of its words is not checked again.
        self.pc.apply(record, [{"id": "c2", "gap": "主人说这期不要了"}], bases=set(), now=NOW + 3)
        self.pc.apply(record, [{"criterion": "新的", "state": "dropped", "gap": "主人说不做", "evidence": "ask:0123abcd"}],
                      bases={"ask:0123abcd"}, now=NOW + 4)
        self.assertEqual([item["state"] for item in self.pc.items(record)], ["dropped", "dropped", "dropped"])

    def test_a_gap_is_a_collab_gap_receipt_until_it_is_closed_with_how(self):
        receipts = [{"id": "0001-aa", "kind": collab.RECEIPT_GAP, "thread": THREAD, "text": "缺原版基线数据", "at": NOW},
                    {"id": "0002-bb", "kind": "thread_end", "text": "T41 做完了", "at": NOW},
                    {"id": "0003-cc", "kind": collab.RECEIPT_GAP, "thread": THREAD, "text": "两份结果冲突", "at": NOW + 1}]
        record = {"id": PID}
        gaps = self.pc.gaps(record, receipts)
        self.assertEqual([gap["text"] for gap in gaps], ["缺原版基线数据", "两份结果冲突"])
        gap = gaps[0]["id"]
        self.assertRegex(gap, r"^g[0-9a-f]{6}$")
        self.assertEqual(gap, self.pc.gap_id("0001-aa"))
        with self.assertRaisesRegex(ValueError, "要写 how"):
            self.pc.apply(record, close=[{"gap": gap}], receipts=receipts, now=NOW)
        with self.assertRaisesRegex(ValueError, "没有没处理的缺口 g000000"):
            self.pc.apply(record, close=[{"gap": "g000000", "how": "补了"}], receipts=receipts, now=NOW)
        self.assertEqual(self.pc.apply(record, close=[{"gap": gap, "how": "T7 补了基线，提交 d4e5"}], receipts=receipts,
                                                 now=NOW), [gap])
        self.assertEqual([row["text"] for row in self.pc.gaps(record, receipts)], ["两份结果冲突"])
        self.assertEqual(record["checklist"]["closed"][gap]["how"], "T7 补了基线，提交 d4e5")
        items, left = self.pc.unmet(record, receipts)
        self.assertEqual((items, [row["id"] for row in left]), ([], [self.pc.gap_id("0003-cc")]))

    def test_an_item_met_while_what_it_needs_is_not_is_a_conflict_and_what_is_not_met_is_listed(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "原版基线"},
                                         {"criterion": "对比报告", "needs": ["c1"], "state": "done", "evidence": "报告"},
                                         {"criterion": "旧格式", "state": "dropped", "gap": "主人说不要了"}], now=NOW)
        [(item, waiting)] = self.pc.conflicts(record)
        self.assertEqual((item["id"], waiting), ("c2", ["c1"]))
        self.assertEqual([row["id"] for row in self.pc.unmet(record, [])[0]], ["c1"])  # dropped counts as met.

    def test_a_record_without_one_or_with_bad_rows_reads_what_it_can(self):
        self.assertEqual(self.pc.items({}), [])
        rows = [{"id": "x1", "criterion": "不是我们的 id"}, {"id": "c1"}, "c2", {"id": "c3", "criterion": "好", "state": "weird",
                                                                               "needs": ["c1", 7, "zz"]}]
        [item] = self.pc.items({"checklist": {"items": rows}})
        self.assertEqual((item["id"], item["state"], item["needs"]), ("c3", "open", ["c1"]))


class ChecklistItemTest(unittest.TestCase):
    """The newer fields of an item (title, waiting_on, done_at, topics) and what is read off ``needs`` and the fields
    (core/project_checklist.py): the same on a record from before, which has none of them."""

    def setUp(self):
        self.pc = checklist()

    def test_done_at_is_written_when_an_item_turns_done_kept_while_it_stays_and_cleared_when_it_turns_back(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "导出"}, {"criterion": "对账", "state": "done", "evidence": "报告"}], now=NOW)
        first, second = self.pc.items(record)
        self.assertEqual((first["done_at"], second["done_at"]), (0, NOW))
        self.assertNotIn("done_at", record["checklist"]["items"][0])  # Not kept for an item that has none.
        self.pc.apply(record, [{"id": "c1", "state": "done", "evidence": "提交 abc"}], now=NOW + 5)
        self.pc.apply(record, [{"id": "c1", "gap": "补一句"}, {"id": "c2", "evidence": "新报告"}], now=NOW + 9)
        first, second = self.pc.items(record)
        self.assertEqual((first["done_at"], first["at"], second["done_at"], second["at"]), (NOW + 5, NOW + 9, NOW, NOW + 9))
        self.pc.apply(record, [{"id": "c1", "state": "open"}, {"id": "c2", "state": "dropped", "gap": "主人说不要了"}], now=NOW + 20)
        self.assertEqual([item["done_at"] for item in self.pc.items(record)], [0, 0])
        self.assertFalse(any("done_at" in row for row in record["checklist"]["items"]))

    def test_waiting_on_is_the_owner_or_not_and_the_owner_needs_a_gap_to_say_what_he_is_to_do(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "上线", "gap": "只差主人点头"}, {"criterion": "对账"}], now=NOW)
        before = copy.deepcopy(record)
        for rows, words in (([{"id": "c1", "waiting_on": "me"}], "waiting_on 要是 owner"),
                            ([{"id": "c2", "waiting_on": "owner"}], "要在 gap 里写要主人做什么")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                self.pc.apply(record, rows, now=NOW + 1)
            self.assertEqual(record, before)
        first, second = self.pc.items(record)
        self.assertEqual((first["waiting_on"], self.pc.owners(first)), (None, (True, True)))  # Not written: the gap's words are read.
        self.assertEqual(self.pc.owners(second), (False, False))
        self.pc.apply(record, [{"id": "c1", "waiting_on": ""}, {"id": "c2", "waiting_on": "owner", "gap": "要主人给账号"}], now=NOW + 2)
        first, second = self.pc.items(record)
        self.assertEqual((self.pc.owners(first), self.pc.owners(second)), ((False, False), (True, False)))  # Written: not guessed.
        self.assertEqual(record["checklist"]["items"][0]["waiting_on"], "")  # '' is written: it says not the owner's.
        self.pc.apply(record, [{"criterion": "迁移", "waiting_on": "none"}], now=NOW + 2)  # What the tool says now (T839).
        self.assertEqual((record["checklist"]["items"][2]["waiting_on"], self.pc.owners(self.pc.items(record)[2])), ("", (False, False)))
        self.pc.apply(record, [{"id": "c2", "state": "done", "evidence": "账号给了"}], now=NOW + 3)
        self.assertEqual(self.pc.owners(self.pc.items(record)[1]), (False, False))  # Met: nobody's.

    def test_a_title_is_cut_and_topics_are_a_group_beside_the_main_topic_replaced_whole(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "导出", "title": "标" * 30, "topic": "t1", "topics": ["t2", "t3", "t2", ""]}], now=NOW)
        [item] = self.pc.items(record)
        self.assertEqual((item["title"], item["topics"], self.pc.topics_of(item)), ("标" * 24, ["t2", "t3"], ["t1", "t2", "t3"]))
        self.pc.apply(record, [{"id": "c1", "topics": ["t1", "t4"]}], now=NOW + 1)
        [item] = self.pc.items(record)
        self.assertEqual((item["topics"], self.pc.topics_of(item)), (["t1", "t4"], ["t1", "t4"]))  # The main one counts once.
        self.pc.apply(record, [{"id": "c1", "topics": []}], now=NOW + 2)
        self.assertEqual(self.pc.items(record)[0]["topics"], [])
        self.assertNotIn("topics", record["checklist"]["items"][0])
        before = copy.deepcopy(record)
        with self.assertRaisesRegex(ValueError, "c1 的 topics 最多 6 个"):
            self.pc.apply(record, [{"id": "c1", "topics": [f"t{n}" for n in range(7)]}], now=NOW + 3)
        self.assertEqual(record, before)
        self.assertEqual(self.pc.topics_of({"topic": "", "topics": []}), [])

    def test_a_record_from_before_reads_with_none_of_them_and_is_written_back_as_it_was(self):
        old = {"id": "c1", "criterion": "导出", "topic": "t1", "needs": [], "evidence": "", "gap": "", "state": "open", "at": NOW, "job": "j"}
        record = {"id": PID, "checklist": {"items": [old], "closed": {}, "next": "c2", "at": NOW, "job": "j"}}
        [item] = self.pc.items(record)
        self.assertEqual((item["title"], item["waiting_on"], item["done_at"], item["topics"]), ("", None, 0, []))
        self.pc.apply(record, [{"criterion": "对账"}], now=NOW + 1)
        self.assertEqual(record["checklist"]["items"][0], old)
        self.assertEqual(set(record["checklist"]["items"][1]), set(old))

    def test_what_waits_on_an_item_directly_is_read_from_needs(self):
        record = {"id": PID}
        self.pc.apply(record, [{"criterion": "A"}, {"criterion": "B", "needs": ["c1"]}, {"criterion": "C", "needs": ["c1", "c4"]},
                               {"criterion": "D"}, {"criterion": "E", "needs": ["c1"], "state": "done", "evidence": "x"},
                               {"criterion": "F", "needs": ["c2"]}], now=NOW)
        entries = self.pc.items(record)
        self.assertEqual(self.pc.rev(entries)["c1"], ["c2", "c3", "c5"])
        # c5 is met already and c6 lies further on (it waits on c2): only c2 and c3 begin or wait for what c1 gives.
        self.assertEqual(self.pc.unlocks(entries, ["c1"]), [("c2", []), ("c3", ["c4"])])
        self.assertEqual(self.pc.unlocks(entries, ["c1", "c4"]), [("c2", []), ("c3", [])])
        self.assertEqual(self.pc.unlocks(entries, ["c6"]), [])

    def test_the_column_an_item_is_in_and_the_count_of_each(self):
        record = {"id": PID}
        self.pc.apply(record, [
            {"criterion": "A", "state": "done", "evidence": "x"},
            {"criterion": "B", "gap": "只差主人点头"},
            {"criterion": "C", "gap": "只差主人点头", "waiting_on": "", "topic": "t1"},
            {"criterion": "D", "needs": ["c5"]},
            {"criterion": "E", "topics": ["t2"]},
            {"criterion": "F", "state": "blocked", "gap": "卡着", "topic": "t1"},
            {"criterion": "G"},
            {"criterion": "H", "state": "dropped", "gap": "主人说不要了"}], now=NOW)
        entries = self.pc.items(record)
        by_id = {item["id"]: item for item in entries}
        self.assertEqual([self.pc.column(item, by_id) for item in entries],
                         ["done", "owner", "doing", "next", "doing", "next", "next", "done"])
        self.assertEqual(self.pc.columns(record), {"done": 2, "doing": 2, "next": 3, "owner": 1, "total": 8})
        self.assertEqual(self.pc.columns({}), {"done": 0, "doing": 0, "next": 0, "owner": 0, "total": 0})


class ActionsTest(unittest.TestCase):
    """core/project_actions.py on a record of its own: what an action is, what a change may do, the state read off its items
    and topics, the topics no item names."""

    def setUp(self):
        self.pa, self.pc = actions(), checklist()
        self.record = {"id": PID, "threads": ["t1", "t2", "t3"]}
        self.pc.apply(self.record, [{"criterion": "导出", "topic": "t1"}, {"criterion": "对账", "needs": ["c1"], "topic": "t2"},
                                    {"criterion": "上线"}], now=NOW)

    def test_actions_get_ids_in_order_and_a_change_replaces_only_what_it_names_and_its_groups_whole(self):
        touched = self.pa.apply(self.record, [{"title": "导出和对账", "note": "在做", "items": ["c1", "c2"], "topics": ["t1"]},
                                              {"title": "上线"}], job="j1", now=NOW)
        self.assertEqual(touched, ["a1", "a2"])
        first, second = self.pa.items(self.record)
        self.assertEqual((first["id"], first["title"], first["note"], first["items"], first["topics"], first["state"], first["job"]),
                         ("a1", "导出和对账", "在做", ["c1", "c2"], ["t1"], "open", "j1"))
        self.assertEqual((second["items"], second["topics"], second["note"]), ([], [], ""))
        self.assertEqual(self.record["actions"]["next"], "a3")
        self.pa.apply(self.record, [{"id": "a1", "note": "做完了"}], job="j2", now=NOW + 1)
        first = self.pa.items(self.record)[0]
        self.assertEqual((first["note"], first["items"], first["topics"], first["title"], first["at"], first["job"]),
                         ("做完了", ["c1", "c2"], ["t1"], "导出和对账", NOW + 1, "j2"))
        self.pa.apply(self.record, [{"id": "a1", "items": ["c2"], "topics": ["t2", "t3"]}], now=NOW + 2)
        first = self.pa.items(self.record)[0]
        self.assertEqual((first["items"], first["topics"]), (["c2"], ["t2", "t3"]))  # The group is replaced, not added to.
        self.assertTrue(self.pa.kept(self.record))
        self.assertFalse(self.pa.kept({"id": PID}))
        self.pa.apply(self.record, [{"id": "a1", "title": "标" * 40, "note": "写" * 100}], now=NOW + 3)
        first = self.pa.items(self.record)[0]
        self.assertEqual((len(first["title"]), len(first["note"])), (self.pa.TITLE, self.pa.NOTE))

    def test_what_does_not_fit_is_refused_and_nothing_changes(self):
        self.pa.apply(self.record, [{"title": "导出", "items": ["c1"]}], now=NOW)
        before = copy.deepcopy(self.record)
        for rows, words in (([{"note": "没有名字"}], "新加的动作要写 title"),
                            ([{"id": "a9", "note": "x"}], "没有动作 a9"),
                            ([{"title": "对账", "items": ["c1", "c7"]}], "条目 c7 不在目标检查表里"),
                            ([{"title": "对账", "topics": ["t9"]}], "不是这个专项的话题"),
                            ([{"id": "a1", "state": "finished"}], "state 要是 open、dropped 之一"),
                            ([{"id": "a1", "title": ""}], "title 不能为空"),
                            (["a1"], "要是一个对象"),
                            ([{"title": f"第 {n} 个"} for n in range(self.pa.MAX_OPEN)], "最多 10 个"),
                            ([{"title": "好的一个"}, {"id": "a9"}], "没有动作 a9")):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                self.pa.apply(self.record, rows, now=NOW + 1)
            self.assertEqual(self.record, before)  # The good row of a refused call is not written either.

    def test_a_dropped_action_stays_with_its_id_and_does_not_count_against_the_limit(self):
        self.pa.apply(self.record, [{"title": f"第 {n} 个"} for n in range(1, 11)], now=NOW)
        with self.assertRaisesRegex(ValueError, "最多 10 个"):
            self.pa.apply(self.record, [{"title": "多一个"}], now=NOW + 1)
        self.pa.apply(self.record, [{"id": "a3", "state": "dropped", "note": "主人说不做了"}], now=NOW + 2)
        self.assertEqual(self.pa.apply(self.record, [{"title": "替它的"}], now=NOW + 3), ["a11"])  # Not a3 again.
        rows = self.pa.items(self.record)
        self.assertEqual((len(rows), rows[2]["id"], rows[2]["state"], rows[-1]["id"]), (11, "a3", "dropped", "a11"))
        with self.assertRaisesRegex(ValueError, "最多 10 个"):
            self.pa.apply(self.record, [{"id": "a3", "state": "open"}], now=NOW + 4)

    def test_a_topic_is_hung_on_an_action_and_an_item_and_what_is_full_is_left_alone(self):
        self.pa.apply(self.record, [{"title": "导出", "items": ["c1"]}, {"title": "也含 c1", "items": ["c1", "c2"]},
                                    {"title": "不做的", "items": ["c1"], "state": "dropped"}], now=NOW)
        for words, args in (("没有动作 a9", {"action": "a9"}), ("没有条目 c9", {"item": "c9"}), ("已经不做了", {"action": "a3"})):
            with self.subTest(words=words), self.assertRaisesRegex(ValueError, words):
                self.pa.check(self.record, **args)
        self.pa.check(self.record, "a1", "c2")
        self.pa.check(self.record)
        self.assertEqual(self.pa.attach(self.record, "t3", item="c1", job="j", now=NOW + 1), (["a1", "a2", "c1"], []))
        self.assertEqual({row["id"]: row["topics"] for row in self.pa.items(self.record)}, {"a1": ["t3"], "a2": ["t3"], "a3": []})
        self.assertEqual(self.pc.items(self.record)[0]["topics"], ["t3"])
        self.assertEqual(self.pa.attach(self.record, "t3", "a1", "c1", now=NOW + 2), ([], []))  # Already there: nothing new.
        # Naming the action hangs it on that one alone; the item's own main topic is not put in its group.
        self.assertEqual(self.pa.attach(self.record, "t1", action="a2", item="c1", now=NOW + 3), (["a2"], []))
        self.assertEqual({row["id"]: row["topics"] for row in self.pa.items(self.record)}, {"a1": ["t3"], "a2": ["t3", "t1"], "a3": []})
        self.assertEqual(self.pc.items(self.record)[0]["topics"], ["t3"])
        # Full: an item has six beside its main topic, an action TOPICS.
        self.pc.apply(self.record, [{"id": "c3", "topics": [f"x{n}" for n in range(6)]}], now=NOW + 4)
        self.assertEqual(self.pa.attach(self.record, "t3", item="c3", now=NOW + 5), ([], ["c3 已有 6 个参与的话题"]))
        self.record["threads"] = [f"m{n}" for n in range(self.pa.TOPICS)]
        self.pa.apply(self.record, [{"id": "a1", "topics": self.record["threads"]}], now=NOW + 6)
        self.assertEqual(self.pa.attach(self.record, "t3", action="a1", now=NOW + 7), ([], [f"a1 的话题已满 {self.pa.TOPICS} 个"]))

    def test_what_an_action_reads_as_from_its_items_and_the_states_of_its_topics(self):
        pa, pc = self.pa, self.pc
        pc.apply(self.record, [{"id": "c1", "state": "done", "evidence": "x"}, {"id": "c3", "gap": "只差主人点头"}], now=NOW)
        entries = pc.items(self.record)

        def action(items, topics):
            return {"id": "a1", "title": "x", "note": "", "items": items, "topics": topics, "state": "open", "at": 0, "job": ""}
        run, wait, end = pa.RUNNING, pa.WAITING, pa.ENDED
        for items, topics, found, words in (
                (["c1"], [], {}, pa.DONE),  # Its items met, no topic.
                (["c1", "c2"], [], {}, pa.NEXT),  # Not met, no topic yet.
                (["c1", "c2"], ["t1"], {"t1": end}, pa.CHECK),  # Not met, its topics ended: what they say is a claim, the lead checks.
                (["c1", "c2"], ["t1", "t2"], {"t1": end, "t2": run}, pa.DOING),
                (["c1"], ["t1"], {"t1": run}, pa.DOING),  # Running beats done.
                (["c1"], ["t1"], {"t1": end}, pa.DONE),
                (["c2"], ["t1", "t2"], {"t1": run, "t2": wait}, pa.OWNER),  # A topic waiting for the owner beats one running.
                (["c3"], ["t1"], {"t1": run}, pa.OWNER),  # An item whose next step is the owner's (read off its gap).
                (["c3"], [], {}, pa.OWNER),
                ([], [], {}, pa.NEXT),  # With no item it is never done.
                ([], ["t1"], {"t1": end}, pa.CHECK),
                (["c1", "c9"], ["t9"], {"t1": run}, pa.DONE)):  # An item gone and a topic not counted are left out.
            with self.subTest(items=items, topics=topics, found=found):
                self.assertEqual(pa.state(action(items, topics), entries, found), words)
        pc.apply(self.record, [{"id": "c3", "waiting_on": ""}], now=NOW + 1)  # The lead says it is not the owner's.
        self.assertEqual(pa.state(action(["c3"], ["t1"]), pc.items(self.record), {"t1": end}), pa.CHECK)
        self.assertEqual(pa.STATE_WORDS, {"owner": "等你", "doing": "在做", "done": "做完", "check": "待核对", "next": "将要做"})

    def test_the_member_topics_no_item_names_are_unmapped(self):
        self.assertEqual(self.pa.unmapped(self.record), ["t3"])  # c1 has t1, c2 has t2.
        self.pc.apply(self.record, [{"id": "c3", "topics": ["t3"]}], now=NOW + 1)
        self.assertEqual(self.pa.unmapped(self.record), [])
        self.assertEqual(self.pa.unmapped({"id": PID}), [])

    def test_a_record_with_bad_rows_reads_what_it_can(self):
        self.assertEqual(self.pa.items({}), [])
        rows = [{"id": "x1", "title": "不是我们的 id"}, {"id": "a1"}, "a2",
                {"id": "a3", "title": "好", "state": "weird", "items": ["c1", 7, "zz"], "topics": ["t1", 5, "t1"]}]
        [action] = self.pa.items({"actions": {"items": rows}})
        self.assertEqual((action["id"], action["state"], action["items"], action["topics"]), ("a3", "open", ["c1"], ["t1"]))


class ChecklistToolTest(ToolsCase):
    """project_checklist and project_finish's gate as the lead calls them, 协作 on."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)

    def member(self, request, title):
        topic = self.tool("project_topic_start", request_id=request, title=title, prompt=title, workspace="project",
                          readonly=True, why="x")["data"]["topic_id"]
        return topic, label_of(self.state, topic)

    def ended(self, request, text="做完了"):
        write_job(self.state, "main-" + butler_topics_token(request), status="done", final_text=text)

    def test_two_topics_say_done_but_the_baseline_check_is_missing_so_the_finish_waits_until_it_is_met(self):
        """L1: both members claim done; the goal also asked for a check against the original baseline, which nobody did."""
        _, first = self.member("a", "导出新版结果")
        _, second = self.member("b", "跑新版对比")
        self.ended("a")
        self.ended("b")
        answer = self.tool("project_checklist", items=[
            {"criterion": "新版结果导出", "topic": first, "state": "done", "evidence": "/absolute/out.csv"},
            {"criterion": "新版对比报告", "topic": second, "state": "done", "evidence": "提交 abc123"},
            {"criterion": "和原版基线对照", "gap": "还没有话题做"}])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["data"]["unmet"], ["c3"])
        self.assertIn("- c3 · 没满足 · 和原版基线对照 · 还没有话题 · 缺口：还没有话题做", answer["text"])
        refused = self.tool("project_finish", summary="两个话题都做完了")
        self.assertFalse(refused["ok"])
        self.assertEqual((refused["data"]["gate"], refused["data"]["unmet"], refused["data"]["gaps"]), ("checklist", ["c3"], []))
        for words in ("c3「和原版基线对照」没满足", "project_topic_send 让原话题返工", "project_topic_start 开新话题"):
            self.assertIn(words, refused["text"])
        self.assertEqual(self.reload()["state"], projects.ACTIVE)
        # The lead arranges it: a topic for the baseline, named on the item.
        _, third = self.member("c", "跑原版基线")
        self.assertTrue(self.tool("project_checklist", items=[{"id": "c3", "topic": third, "gap": f"{third} 在跑"}])["ok"])
        self.ended("c", "基线跑完了")
        pack = project_lead.state_pack(self.state, self.reload(), "lead-job", full=False, receipts=[])
        self.assertIn(f"⚠️ {third} 已经结束（{RESULTS['done']}），它交付的 c3 还没满足", pack)  # Its claim, to check.
        self.assertFalse(self.tool("project_finish", summary="都做完了")["ok"])  # Ended is not met.
        self.assertTrue(self.tool("project_checklist", items=[{"id": "c3", "state": "done", "evidence": "/absolute/base.md"}])["ok"])
        done = self.tool("project_finish", summary="新版对比做完，含原版基线对照")
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.reload()["state"], projects.DONE)

    def test_an_item_not_met_in_the_record_holds_the_finish(self):
        projects.update(self.state, PID, lambda record: record.update(checklist=CHECKLIST))
        refused = self.tool("project_finish", summary="做完了")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["data"]["unmet"], ["c1"])  # c2 is met (its own need not being so is the pack's warning).
        self.assertEqual(self.reload()["state"], projects.ACTIVE)

    def test_a_collab_gap_holds_the_finish_until_the_lead_says_how_it_was_dealt_with(self):
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_GAP, "T3 协作里补不上的缺口：缺原版基线数据", thread=THREAD)
        project_lead.take_receipts(self.state, PID, "lead-job")  # Read already: still not dealt with.
        refused = self.tool("project_finish", summary="做完了")
        self.assertFalse(refused["ok"])
        [gap] = refused["data"]["gaps"]
        self.assertIn("协作里还有没处理的缺口", refused["text"])
        self.assertIn("缺原版基线数据", refused["text"])
        pack = project_lead.state_pack(self.state, self.reload(), "lead-job", full=False, receipts=[])
        self.assertIn(project_lead.GAPS_HEAD, pack)
        self.assertIn(f"- {gap} · ", pack)
        self.assertIn("要写 how", self.refused("project_checklist", close_gaps=[{"gap": gap}]))
        closed = self.tool("project_checklist", close_gaps=[{"gap": gap, "how": "主人说这次不用补"}])
        self.assertTrue(closed["ok"], closed)
        self.assertEqual(closed["data"]["gaps"], [])
        self.assertTrue(self.tool("project_finish", summary="做完了")["ok"])

    def test_the_topic_of_an_item_is_one_of_the_projects_and_reading_changes_nothing(self):
        outside = "a" * 32  # An ordinary topic of the owner's, in no project.
        write_topic(self.state, {"topic_id": outside}, topic_id=outside, chat_id=CHAT, harness_id="claude", topic_number=88,
                    root_id="om_root88")
        refused = self.tool("project_checklist", items=[{"criterion": "导出", "topic": "T88"}])
        self.assertFalse(refused["ok"])
        self.assertIn("不是这个专项的话题", refused["text"])
        self.assertNotIn("checklist", self.reload())
        self.assertIn("没有找到话题 T999", self.tool("project_checklist", items=[{"criterion": "导出", "topic": "T999"}])["text"])
        before = self.reload()
        shown = self.tool("project_checklist")
        self.assertTrue(shown["ok"])
        self.assertIn(project_lead.CHECKLIST_EMPTY, shown["text"])
        self.assertEqual(self.reload(), before)

    def test_a_refused_change_writes_nothing_and_says_why(self):
        self.tool("project_checklist", items=[{"criterion": "导出"}])
        before = self.reload()
        answer = self.tool("project_checklist", items=[{"criterion": "对账"}, {"id": "c1", "state": "done"}])
        self.assertFalse(answer["ok"])
        self.assertIn("目标检查表没有改：c1 标 done 要写 evidence", answer["text"])
        self.assertEqual(self.reload()["checklist"], before["checklist"])

    def test_what_the_owner_let_go_is_named_when_the_project_is_finished(self):
        write_job(self.state, "om_said1", topic_id=LEAD, chat_id=CHAT, job_id=9, message_id="om_said1", status="done",
                  user_text="原版基线这次不做了")
        written = self.tool("project_checklist", items=[{"criterion": "导出", "state": "done", "evidence": "/absolute/x.csv"},
                                                        {"criterion": "原版基线", "state": "dropped", "gap": "主人说这次不做",
                                                         "evidence": "msg:om_said1"}])
        self.assertTrue(written["ok"], written)
        done = self.tool("project_finish", summary="导出做完")
        self.assertTrue(done["ok"], done)
        self.assertIn("主人说不做的：c2「原版基线」（主人说这次不做）", done["text"])

    def owner_question(self, token, *, party="owner", topic=LEAD, status="answered", origin="project_ask"):
        write_json(self.state / "questions" / f"{token}.json",
                   {"token": token, "topic_id": topic, "chat_id": CHAT, "job_id": 2, "status": status, "origin": origin,
                    "questions": CHOICE, "answers": {"0": {"answers": ["不做了"]}},
                    "answer_by": {"party": party, "channel": "card"}}, ensure_ascii=False)

    def test_a_drop_stands_only_on_the_owners_own_word_in_the_project(self):
        """W2-C: an item turned dropped names his message in the project (msg:) or a question of the project he answered himself
        (ask:), which the Bridge checks; anything else, or nothing, is refused and nothing is written."""
        self.tool("project_checklist", items=[{"criterion": "导出"}, {"criterion": "原版基线"}])
        write_job(self.state, "om_them", topic_id=LEAD, chat_id=CHAT, job_id=10, message_id="om_them", status="done",
                  user_text="不做了", sender_id="ou_someone_else")
        write_job(self.state, "om_mine", topic_id=LEAD, chat_id=CHAT, job_id=11, message_id="om_mine", status="done",
                  user_text="基线不做了")
        self.owner_question("a1b2c3d4e5f60001")
        self.owner_question("a1b2c3d4e5f60002", party="lead")  # The lead answered it in his place.
        self.owner_question("a1b2c3d4e5f60003", status="pending")  # Not answered yet.
        self.owner_question("a1b2c3d4e5f60004", topic="a" * 32)  # A topic of no project's.
        before = self.reload()["checklist"]
        for evidence in ("", "主人说的", "msg:om_them", "msg:nothing1", "ask:a1b2c3d4e5f60002", "ask:a1b2c3d4e5f60003",
                         "ask:a1b2c3d4e5f60004", "ask:pause-1700000000"):
            with self.subTest(evidence=evidence):
                answer = self.tool("project_checklist", items=[{"id": "c2", "state": "dropped", "gap": "主人说不做",
                                                                "evidence": evidence}])
                self.assertFalse(answer["ok"], answer)
                self.assertIn("c2 标 dropped 要带主人的依据", answer["text"])
                if evidence.startswith(("msg:", "ask:")):
                    self.assertIn(f"Bridge 核对不上（不是主人在这个专项里说的话或他本人答过的题）：{evidence}", answer["text"])
                self.assertEqual(self.reload()["checklist"], before)
        for row, evidence in (("c2", "msg:om_mine"), ("c1", "主人在卡上答了 ask:a1b2c3d4e5f60001")):
            answer = self.tool("project_checklist", items=[{"id": row, "state": "dropped", "gap": "主人说不做", "evidence": evidence}])
            self.assertTrue(answer["ok"], answer)
        self.assertEqual([item["state"] for item in checklist().items(self.reload())], ["dropped", "dropped"])

    def test_the_owners_finish_on_the_pause_card_is_the_ground_its_receipt_names(self):
        self.tool("project_checklist", items=[{"criterion": "导出"}])
        ask = {"reason": "wakes", "why": "叫醒太多", "at": NOW + 5, "choice": "finish", "chosen_at": NOW + 9}
        projects.update(self.state, PID, lambda data: data.update(pause_ask=ask))
        project_loop.pause_receipt(self.state, PID, "finish", ask)
        [receipt] = [item for item in project_lead.inbox_items(self.state, PID) if item["kind"] == "owner_choice"]
        ref = f"ask:pause-{int(NOW + 5)}"
        self.assertIn(f"evidence 写 {ref}", receipt["text"])
        answer = self.tool("project_checklist", items=[{"id": "c1", "state": "dropped", "gap": "主人选了收尾，没做完：导出",
                                                        "evidence": ref}])
        self.assertTrue(answer["ok"], answer)
        projects.update(self.state, PID, lambda data: data["pause_ask"].update(choice="continue"))
        self.tool("project_checklist", items=[{"criterion": "再一条"}])
        refused = self.tool("project_checklist", items=[{"id": "c2", "state": "dropped", "gap": "x", "evidence": ref}])
        self.assertFalse(refused["ok"])  # He chose to go on: no ground for a drop.

    def test_the_owners_answer_to_a_leads_question_names_itself_as_a_ground(self):
        from covey.app.questions import with_approvals

        record = {"token": "a1b2c3d4e5f60001", "topic_id": LEAD, "origin": "project_ask",
                  "answer_by": {"party": "owner", "channel": "card"}}
        self.assertIn("evidence 写 ask:a1b2c3d4e5f60001", with_approvals(self.state, record, "用户补充回答"))
        for other in ({**record, "answer_by": {"party": "butler", "channel": "tool"}}, {**record, "origin": ""}):
            self.assertEqual(with_approvals(self.state, other, "用户补充回答"), "用户补充回答")
        os.environ.pop(turn_source.SWITCH)
        self.assertEqual(with_approvals(self.state, record, "用户补充回答"), "用户补充回答")

    def test_a_finished_project_cannot_have_its_checklist_changed_but_it_can_be_read(self):
        self.assertTrue(self.tool("project_finish", summary="做完")["ok"])
        self.assertIn("不能改目标检查表", self.refused("project_checklist", items=[{"criterion": "x"}]))
        self.assertTrue(self.tool("project_checklist")["ok"])

    def test_off_there_is_no_checklist_and_the_finish_is_as_it_was(self):
        os.environ.pop(turn_source.SWITCH)
        off = self.tool("project_checklist", items=[{"criterion": "导出"}])
        self.assertFalse(off["ok"])
        self.assertIn("协作没有开", off["text"])
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_GAP, "缺口", thread=THREAD)
        projects.update(self.state, PID, lambda record: record.update(
            checklist={"items": [{"id": "c1", "criterion": "导出", "state": "open"},
                                 {"id": "c2", "criterion": "基线", "state": "dropped", "gap": "不做"}]}))
        done = self.tool("project_finish", summary="账单迁完")
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["text"], "专项已记为完成：账单迁完")


class BriefToolTest(ToolsCase):
    """The 简报 as the lead writes it: project_goal's background, project_checklist's actions with the items they name in the same
    call, and a topic opened or fed on an action or an item. 协作 on; off, the new arguments are refused."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)

    def member(self, request, title, **args):
        answer = self.tool("project_topic_start", request_id=request, title=title, prompt=title, workspace="project",
                           readonly=True, why="x", **args)
        self.assertTrue(answer["ok"], answer)
        topic = answer["data"]["topic_id"]
        return topic, label_of(self.state, topic)

    def running(self, request, **fields):
        write_job(self.state, "main-" + butler_topics_token(request), status="running", phase="做事", **fields)

    def test_the_goal_and_the_background_are_written_apart_and_one_of_them_at_least(self):
        self.assertEqual(self.tool("project_goal", text="把账单迁到新库")["text"], "已记下专项的目标：把账单迁到新库")  # As before.
        answer = self.tool("project_goal", background="旧库在老机房，下月到期。\n新库已建好。")
        self.assertEqual(answer["text"], "已记下专项的背景：旧库在老机房，下月到期。 新库已建好。")
        record = self.reload()
        self.assertEqual((record["lead_goal"]["text"], project_lead.lead_background(record)), ("把账单迁到新库", "旧库在老机房，下月到期。 新库已建好。"))
        self.assertEqual(set(record["lead_background"]), {"text", "at", "job"})
        both = self.tool("project_goal", text="迁完", background="背" * 400)
        self.assertEqual(both["text"].count("已记下专项的"), 2)
        record = self.reload()
        self.assertEqual((record["lead_goal"]["text"], len(record["lead_background"]["text"])), ("迁完", 300))
        self.assertIn("text（目标）和 background（背景）至少给一个", self.refused("project_goal"))
        self.assertIn("text（目标）和 background（背景）至少给一个", self.refused("project_goal", text=" ", background=""))

    def test_actions_are_written_with_the_items_they_name_and_topics_by_t_number_all_or_nothing(self):
        first, first_label = self.member("a", "导出新版结果")
        second, second_label = self.member("b", "跑新版对比")
        self.running("a")
        answer = self.tool("project_checklist",
                           items=[{"criterion": "新版结果导出", "title": "导出", "topic": first_label},
                                  {"criterion": "对比报告", "topic": second_label, "topics": [first_label], "needs": ["c1"]}],
                           actions=[{"title": "新版导出和对比", "note": "在做导出", "items": ["c1", "c2"], "topics": [first_label, second]}])
        self.assertTrue(answer["ok"], answer)
        record = self.reload()
        [action] = actions().items(record)
        self.assertEqual((action["id"], action["items"], action["topics"], action["note"]), ("a1", ["c1", "c2"], [first, second], "在做导出"))
        self.assertEqual([item["topics"] for item in checklist().items(record)], [[], [first]])  # Topic ids, not T numbers.
        self.assertTrue(answer["text"].startswith("已更新目标检查表和简报（c1、c2、a1）。\n简报（1 个动作）："))
        self.assertIn("- a1 新版导出和对比 · 在做 · 没满足 c1、c2 · 现状：在做导出", answer["text"])
        self.assertEqual(answer["data"]["unmet"], ["c1", "c2"])
        # One bad row refuses the whole call, the items of the same call with it.
        before = self.reload()
        for args, words in (({"items": [{"criterion": "新条目"}], "actions": [{"title": "x", "items": ["c9"]}]}, "c9 不在目标检查表里"),
                            ({"items": [{"criterion": "新条目"}], "actions": [{"id": "a7", "note": "x"}]}, "没有动作 a7"),
                            ({"actions": [{"title": "x", "topics": ["T9999"]}]}, "没有找到话题 T9999")):
            with self.subTest(words=words):
                refused = self.tool("project_checklist", **args)
                self.assertFalse(refused["ok"])
                self.assertIn(words, refused["text"])
                self.assertEqual(self.reload(), before)
        # A topic that is not the project's is no topic of an action, nor of an item's group.
        outside = "a" * 32
        write_topic(self.state, {"topic_id": outside}, topic_id=outside, chat_id=CHAT, harness_id="claude", topic_number=88, root_id="om_root88")
        for args in ({"actions": [{"title": "x", "topics": ["T88"]}]}, {"items": [{"id": "c1", "topics": ["T88"]}]}):
            self.assertIn("不是这个专项的话题", self.tool("project_checklist", **args)["text"])
        self.assertEqual(self.reload(), before)

    def test_an_action_changed_alone_and_dropped_and_reading_shows_the_brief_with_its_groups(self):
        topic, label = self.member("a", "导出")
        self.assertTrue(self.tool("project_checklist", items=[{"criterion": "导出"}], actions=[{"title": "导出", "items": ["c1"], "topics": [label]},
                                                                                           {"title": "试试看"}])["ok"])
        before = self.reload()["checklist"]
        answer = self.tool("project_checklist", actions=[{"id": "a2", "state": "dropped", "note": "主人说不做了"}])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.reload()["checklist"], before)  # Actions alone leave the items as they were.
        self.assertTrue(answer["text"].startswith("已更新简报（a2）。\n简报（1 个动作，另有 1 个不做了）："), answer["text"])
        shown = self.tool("project_checklist")["text"]
        self.assertIn(f"- a1 导出 · 在做 · 没满足 c1 · 条目 c1 · 话题 {label}", shown)  # The group a change would replace.
        self.assertNotIn("a2", shown)
        self.assertIn("目标检查表（1 条", shown)
        self.assertEqual(self.reload()["actions"]["items"][0]["topics"], [topic])

    def test_a_topic_opened_on_an_action_or_an_item_is_hung_on_them_once_it_is_a_member(self):
        self.tool("project_checklist", items=[{"criterion": "导出"}, {"criterion": "对账", "needs": ["c1"]}],
                  actions=[{"title": "导出和对账", "items": ["c1", "c2"]}, {"title": "对账再核", "items": ["c2"]}])
        topic, _ = self.member("s1", "导出", action="a1", item="c1")
        answer = self.tool("project_topic_start", request_id="s1", title="导出", prompt="导出", workspace="project", readonly=True, why="x",
                           action="a1", item="c1")
        self.assertEqual(answer["data"]["topic_id"], topic)  # The same request again opens nothing new, and hangs nothing twice.
        self.assertNotIn("已挂到", answer["text"])
        record = self.reload()
        self.assertEqual({row["id"]: row["topics"] for row in actions().items(record)}, {"a1": [topic], "a2": []})
        self.assertEqual([item["topics"] for item in checklist().items(record)], [[topic], []])
        self.assertEqual(record["threads"], [topic])
        second, _ = self.member("s2", "对账", item="c2")  # An item alone: every action that has it.
        record = self.reload()
        self.assertEqual({row["id"]: row["topics"] for row in actions().items(record)}, {"a1": [topic, second], "a2": [second]})
        self.assertIn("已挂到 a1、a2、c2", self.tool("project_topic_start", request_id="s3", title="再对一次", prompt="x", workspace="project",
                                                       readonly=True, why="x", item="c2")["text"])
        self.assertEqual(self.tool("project_topic_start", request_id="s4", title="只挂条目", prompt="x", workspace="project",
                                   readonly=True, why="x", action="a2")["text"].count("已挂到 a2"), 1)

    def test_ids_that_are_not_the_records_refuse_the_start_before_a_topic_is_opened(self):
        self.tool("project_checklist", items=[{"criterion": "导出"}], actions=[{"title": "导出", "items": ["c1"]}, {"title": "弃", "state": "dropped"}])
        for fields, words in (({"action": "a9"}, "没有动作 a9"), ({"item": "c9"}, "没有条目 c9"), ({"action": "a2"}, "已经不做了"),
                              ({"action": "a1", "item": "c9"}, "没有条目 c9")):
            with self.subTest(words=words):
                answer = self.refused("project_topic_start", title="x", prompt="y", workspace="project", readonly=True, why="z", **fields)
                self.assertIn(words, answer)
                self.assertEqual(self.reload()["threads"], [])
                self.assertIsNone(self.job("rx"))
        topic, label = self.member("s1", "导出")
        self.assertIn("没有动作 a9", self.refused("project_topic_send", thread=label, text="再看", action="a9"))
        self.assertIsNone(self.job("rx"))
        self.assertEqual([row["topics"] for row in actions().items(self.reload())], [[], []])

    def test_a_topic_fed_with_an_action_or_an_item_is_hung_on_them_and_a_refused_send_hangs_nothing(self):
        topic, label = self.member("open", "导出")
        job = self.job("open")
        self.app.runtime.unregister_reply_job(job)
        write_job(self.state, job.message_id, status="done")
        self.tool("project_checklist", items=[{"criterion": "导出"}], actions=[{"title": "导出", "items": ["c1"]}, {"title": "另一个"}])
        answer = self.tool("project_topic_send", request_id="s1", thread=label, text="再核对一遍金额", action="a2", item="c1")
        self.assertTrue(answer["ok"], answer)
        self.assertIn("已挂到 a2、c1", answer["text"])
        record = self.reload()
        self.assertEqual([row["topics"] for row in actions().items(record)], [[], [topic]])
        self.assertEqual(checklist().items(record)[0]["topics"], [topic])
        self.assertEqual(self.job("s1").user_text, "再核对一遍金额")  # The topic's own words carry none of it.
        self.assertNotIn("a2", self.job("s1").prompt_text)

    def test_off_the_brief_arguments_are_refused_and_the_calls_without_them_are_as_before(self):
        os.environ.pop(turn_source.SWITCH)
        self.assertIn("没有简报", self.refused("project_goal", background="x"))
        self.assertIn("没有简报", self.refused("project_topic_start", title="x", prompt="y", workspace="project", readonly=True, why="z", action="a1"))
        self.assertIn("没有简报", self.refused("project_topic_start", title="x", prompt="y", workspace="project", readonly=True, why="z", item="c1"))
        self.assertEqual(self.reload()["threads"], [])
        self.assertIn("text 不能为空", self.refused("project_goal"))
        self.assertEqual(self.tool("project_goal", text="迁完")["text"], "已记下专项的目标：迁完")
        self.assertNotIn("lead_background", self.reload())
        self.assertTrue(self.tool("project_topic_start", request_id="plain", title="x", prompt="y", workspace="project", readonly=True, why="z")["ok"])
        self.assertNotIn("actions", self.reload())


class PackTest(Case):
    """What the lead's rules and pack say, 协作 on and off."""

    def receipts(self):
        made = [project_lead.add_receipt(self.state, PID, kind, text, thread=THREAD, at=NOW + n) for n, (kind, text) in enumerate((
            ("thread_end", "T41 的任务 #41 做完：导出好了"),
            (collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：一"),
            (collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：二"),
            ("stalled", "专项还有要做的事"),
            (collab.RECEIPT_ANSWERED, "T41 请 T56 协助：已答复：三"),
            (collab.RECEIPT_GAP, "T41 协作里补不上的缺口：缺原版基线"),
            (collab.RECEIPT_ANSWERED, "T41 请 T57 协助：未接手：在等主人回答"),
            ("start", "接手一个已在进行的事")))]
        return made

    def world(self):
        self.thread(THREAD, "导出旧账单", 41, status="done", final_text="做完了")
        entry, _ = project_memory.add(self.state, PID, "先迁 2024 年", "decision", "lead")
        projects.update(self.state, PID, lambda record: record.update(checklist=CHECKLIST))
        write_job(self.state, "om_own1", topic_id=LEAD, chat_id=CHAT, job_id=7, message_id="om_own1", status="done",
                  user_text="先迁 2024 年的，其余下周再说")
        write_job(self.state, f"lead-{PID}-1", topic_id=LEAD, chat_id=CHAT, job_id=8, message_id=f"lead-{PID}-1", status="done",
                  user_text=project_loop.WAKE_TEXT)
        return entry, self.receipts()

    def test_off_the_rules_and_the_pack_are_as_they_were(self):
        _, receipts = self.world()
        record = self.reload()
        pack = project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=receipts)
        rules = project_lead.lead_rules()
        with mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}):
            self.assertEqual(project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=receipts), pack)
            self.assertEqual(project_lead.lead_rules(), rules)
        with mock.patch.dict(os.environ, TRIAGE):
            triaged = project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=receipts)
            triaged_rules = project_lead.lead_rules()
        for words in ("目标检查表", "先看：", "没处理的协作缺口", "简报"):
            self.assertNotIn(words, pack)
            self.assertNotIn(words, triaged)
        self.assertIn("自上次唤醒以来的收件箱：\n" + "\n".join(map(project_lead.receipt_line, receipts)), pack)
        for words in ("project_checklist", "目标检查表", "collab_answered"):
            self.assertNotIn(words, rules)
        # With the triage on, project_answer names its grounds with 协作 off too (W0-2): the owner's words and the rule for them.
        self.assertIn(project_lead.OWNER_WORDS_HEAD, triaged)
        self.assertIn("project_answer 必须写 evidence", triaged_rules)
        with mock.patch.dict(os.environ, {"COVEY_PROJECT_TRIAGE": "0"}):
            self.assertNotIn("evidence", project_lead.lead_rules())
            self.assertNotIn(project_lead.OWNER_WORDS_HEAD, project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=receipts))

    def test_on_the_pack_shows_the_checklist_the_gaps_the_ids_the_owners_words_and_the_inbox_in_groups(self):
        mock.patch.dict(os.environ, {**COLLAB, **TRIAGE}).start()
        self.addCleanup(mock.patch.stopall)
        entry, receipts = self.world()
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True, receipts=receipts)
        self.assertIn("目标检查表（2 条：1 条满足或不做了，1 条没满足）：", pack)
        self.assertIn(f"- c1 · 没满足 · 导出 2024 年账单 · T41（{RESULTS['done']}）", pack)
        self.assertIn("- c2 · 满足 · 对账一致 · 还没有话题 · 依赖 c1（没满足） · 证据：对账报告", pack)
        self.assertIn("⚠️ c2 标了满足，但它依赖的 c1 还没满足", pack)
        self.assertIn(f"⚠️ T41 已经结束（{RESULTS['done']}），它交付的 c1 还没满足", pack)
        self.assertIn(project_lead.GAPS_HEAD, pack)
        self.assertIn(f"〔{project_lead.memory_codes(self.state, PID)[entry['id']]}〕先迁 2024 年", pack)  # Its short code.
        self.assertIn(project_lead.OWNER_WORDS_HEAD + "\n- msg:om_own1 · ", pack)
        self.assertIn("先迁 2024 年的，其余下周再说", pack)
        self.assertNotIn(f"msg:lead-{PID}-1", pack)  # A wake-up is no word of the owner's.
        inbox = pack[pack.index("自上次唤醒以来的收件箱："):]
        heads = ["先看：", "话题的交付和结束（1 条", "其他：", "协作咨询的回执（4 条"]
        self.assertEqual([inbox.index(head) for head in heads], sorted(inbox.index(head) for head in heads))
        first = inbox[inbox.index("先看："):inbox.index("话题的交付和结束")]
        self.assertIn("- ⚠️ ", first)
        self.assertIn("collab_gap（T41）", first)
        self.assertIn("start（T41）", first)
        self.assertIn("（它交付的 c1 还没满足）", inbox)
        self.assertIn("另有 1 条更早的", inbox)
        self.assertNotIn("已答复：一", inbox)  # Folded: the newest three are shown.
        self.assertIn("未接手：在等主人回答", inbox)
        rules = project_lead.lead_rules()
        for words in ("project_checklist", "collab_gap", "不要把话题或汇总方的结论换个说法再发一遍", "project_answer 必须写 evidence"):
            self.assertIn(words, rules)
        consult = project_lead.read_only_pack(self.state, self.reload())
        self.assertIn("目标检查表（2 条", consult)
        self.assertIn(project_lead.GAPS_HEAD, consult)

    def test_without_the_triage_the_owners_words_are_not_listed_and_the_rules_ask_no_evidence(self):
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        _, receipts = self.world()
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True, receipts=receipts)
        self.assertNotIn("主人在专项里说过的话", pack)
        self.assertIn("目标检查表（2 条", pack)
        self.assertNotIn("project_answer 必须写 evidence", project_lead.lead_rules())
        self.assertIn("project_checklist", project_lead.lead_rules())

    def test_the_new_words_retire_nothing_the_owner_retired(self):
        from test_glossary import RETIRED, strings
        mock.patch.dict(os.environ, {**COLLAB, **TRIAGE}).start()
        self.addCleanup(mock.patch.stopall)
        texts = [*project_lead.COLLAB_RULES, project_lead.EVIDENCE_RULE, project_lead.CHECKLIST_EMPTY, project_lead.GAPS_HEAD,
                 project_lead.OWNER_WORDS_HEAD, project_lead.BRIEF_EMPTY, project_loop.CHOICE_CHECKLIST,
                 *strings(butler_tool_specs.lead_tools({**COLLAB, **TRIAGE}))]
        self.assertEqual([text[:40] for text in texts if RETIRED.search(text)], [])

    def brief(self, background="主人用 Covey 跑本机飞书机器人，要迁账单到新库。"):
        """A project with a 目标检查表, a background and three actions: 导出 met and its topic ended, 对账 running, 下线旧库 for the owner."""
        done, running, loose = "1" * 32, "2" * 32, "3" * 32
        self.thread(done, "导出账单", 41, status="done", final_text="做完了")
        self.thread(running, "对账", 42, status="running", phase="核对金额")
        self.thread(loose, "旧库迁移", 43, status="done", final_text="做完了")
        pc, pa = checklist(), actions()

        def change(record):
            pc.apply(record, [{"criterion": "导出 2024 年账单", "topic": done, "state": "done", "evidence": "/absolute/out.csv"},
                              {"criterion": "对账一致", "topic": running, "needs": ["c1"]},
                              {"criterion": "旧库下线", "needs": ["c1"], "gap": "等主人点头", "waiting_on": "owner"}], now=NOW)
            pa.apply(record, [{"title": "导出", "items": ["c1"], "topics": [done], "note": "做完了"},
                              {"title": "对账", "items": ["c2"], "topics": [running], "note": "核对中：金额"},
                              {"title": "下线旧库", "items": ["c3"], "note": "等你定日期"},
                              {"title": "不做的", "state": "dropped"}], now=NOW)
            if background:
                record["lead_background"] = {"text": background, "at": NOW, "job": "lead-1"}
        projects.update(self.state, PID, change)
        return done, running, loose

    def test_the_pack_has_a_short_brief_before_the_checklist_and_only_with_collab_on(self):
        self.brief()
        record = self.reload()
        full = project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=[])
        self.assertNotIn("简报", full)  # Off: the pack is as it was.
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        full = project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=[])
        block = full[full.index("简报（"):full.index("目标检查表（")]
        self.assertEqual(block.strip().splitlines(), [
            "简报（3 个动作，另有 1 个不做了）：",
            "背景：主人用 Covey 跑本机飞书机器人，要迁账单到新库。",
            "- a1 导出 · 做完",
            "- a2 对账 · 在做 · 没满足 c2 · 现状：核对中：金额",
            "- a3 下线旧库 · 等你 · 没满足 c3 · 现状：等你定日期",
            "还没挂到条目上的话题：T43"])
        self.assertLess(full.index("话题（3 个"), full.index("简报（"))
        resumed = project_lead.state_pack(self.state, record, "lead-1", full=False, receipts=[])
        self.assertNotIn("背景：", resumed)  # The background is said once, in a new session.
        self.assertIn("- a3 下线旧库 · 等你", resumed)
        self.assertIn("简报（3 个动作", project_lead.read_only_pack(self.state, record))

    def test_the_background_is_cut_in_the_pack_the_unmapped_topics_are_eight_and_a_count_and_no_brief_is_one_line(self):
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        empty = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True, receipts=[])
        self.assertIn(project_lead.BRIEF_EMPTY, empty)
        self.assertEqual(empty.count("简报"), 1)  # One sentence and nothing else.
        for number in range(10):
            self.thread(f"{number:x}" * 32, f"话题 {number}", 50 + number, status="done", final_text="好")
        projects.update(self.state, PID, lambda record: record.update(lead_background={"text": "背" * 200, "at": NOW, "job": "j"}))
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=True, receipts=[])
        self.assertIn("背景：" + "背" * 119 + "…\n", pack)
        self.assertIn("动作：还没写（project_checklist 的 actions 拆 3–8 个）", pack)
        self.assertIn("还没挂到条目上的话题：T50、T51、T52、T53、T54、T55、T56、T57 等 10 个", pack)
        self.assertNotIn(project_lead.BRIEF_EMPTY, pack)
        # Actions and no background: the lead is asked for one in a new session, not in a resumed one.
        projects.update(self.state, PID, lambda record: (record.pop("lead_background"), actions().apply(record, [{"title": "导出"}], now=NOW)))
        self.assertIn("背景：还没写（project_goal 的 background）", project_lead.state_pack(self.state, self.reload(), "lead-1", full=True, receipts=[]))
        self.assertNotIn("背景：", project_lead.state_pack(self.state, self.reload(), "lead-1", full=False, receipts=[]))

    def test_a_resumed_session_folds_the_items_met_into_one_line(self):
        # W2-C: a resumed session has had them whole; a new one, a consult and the tool's answer still get each line.
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        self.world()
        record = self.reload()
        resumed = project_lead.state_pack(self.state, record, "lead-1", full=False, receipts=[])
        self.assertIn("目标检查表（2 条：1 条满足或不做了，1 条没满足）：\n- 满足或不做了：c2（续接的会话里只列 id）", resumed)
        self.assertNotIn("- c2 · 满足 · 对账一致", resumed)
        self.assertIn("- c1 · 没满足 · 导出 2024 年账单", resumed)
        self.assertIn("⚠️ c2 标了满足，但它依赖的 c1 还没满足", resumed)  # What to look into stays.
        whole = project_lead.state_pack(self.state, record, "lead-1", full=True, receipts=[])
        self.assertIn("- c2 · 满足 · 对账一致", whole)
        self.assertNotIn("满足或不做了：c2", whole)
        rows = project_lead.member_rows(self.state, record)
        self.assertIn("- c2 · 满足 · 对账一致", project_lead.checklist_block(self.state, record, rows, detail=True))

    def test_an_item_says_the_topics_that_took_part_when_the_tool_shows_the_checklist_in_detail(self):
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        done, running, _ = self.brief()
        projects.update(self.state, PID, lambda record: checklist().apply(record, [{"id": "c2", "topics": [done]}], now=NOW + 1))
        record = self.reload()
        rows = project_lead.member_rows(self.state, record)
        self.assertNotIn("参与的话题", project_lead.checklist_block(self.state, record, rows))
        self.assertIn("- c2 · 没满足 · 对账一致 · T42（在做） · 参与的话题 T41 · 依赖 c1（满足）",
                      project_lead.checklist_block(self.state, record, rows, detail=True))
        # An item with no main topic names the first that took part, and still says the others.
        projects.update(self.state, PID, lambda record: checklist().apply(record, [{"id": "c3", "topics": [done, running]}], now=NOW + 2))
        record = self.reload()
        self.assertIn("- c3 · 没满足 · 旧库下线 · T41（做完） · 参与的话题 T42", project_lead.checklist_block(self.state, record, rows, detail=True))
        self.assertIn("- c3 · 没满足 · 旧库下线 · T41（做完） · 依赖 c1", project_lead.checklist_block(self.state, record, rows))

    def test_a_delivery_that_ends_says_what_its_item_unlocks_and_what_those_still_wait_on(self):
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        checkpoint = "c" * 32
        projects.update(self.state, PID, lambda record: checklist().apply(record, [
            {"criterion": "导出", "topic": THREAD}, {"criterion": "对账", "needs": ["c1"]}, {"criterion": "上线", "needs": ["c1", "c4"]},
            {"criterion": "审批"}, {"criterion": "归档", "needs": ["c2"]}, {"criterion": "已做", "needs": ["c1"], "state": "done", "evidence": "x"},
            {"criterion": "另一个话题的", "topics": [checkpoint]}], now=NOW))
        receipts = [project_lead.add_receipt(self.state, PID, "thread_end", "T41 的任务 #41 做完：导出好了", thread=THREAD),
                    project_lead.add_receipt(self.state, PID, "thread_end", "T9 做完", thread=checkpoint)]
        inbox = project_lead.inbox_block(self.state, receipts, self.reload())
        self.assertIn("（它交付的 c1 还没满足；满足后 c2 可开始、c3 还在等 c4）", inbox)  # c6 is met, c5 lies further on.
        self.assertIn("（它交付的 c7 还没满足）", inbox)  # Nothing waits on c7: as before.
        projects.update(self.state, PID, lambda record: checklist().apply(record, [{"id": "c1", "state": "done", "evidence": "y"}], now=NOW + 1))
        self.assertNotIn("它交付的 c1", project_lead.inbox_block(self.state, receipts, self.reload()))  # Met: no hint.
        mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}).start()
        self.assertEqual(project_lead.inbox_block(self.state, receipts, self.reload()),
                         "自上次唤醒以来的收件箱：\n" + "\n".join(map(project_lead.receipt_line, receipts)))  # Off: a line each, as before.

    def test_the_rules_say_the_brief_in_two_sentences_with_collab_on_only(self):
        rules = project_lead.lead_rules()
        for words in ("简报", "background", "actions", "action 或 item"):
            self.assertNotIn(words, rules)
        mock.patch.dict(os.environ, COLLAB).start()
        self.addCleanup(mock.patch.stopall)
        on = project_lead.lead_rules()
        self.assertEqual([line for line in on.splitlines() if line in rules.splitlines()], rules.splitlines())  # What was there stays.
        self.assertEqual(project_lead.COLLAB_RULES[-1][:5], "- 简报：")
        added = [rule for rule in project_lead.COLLAB_RULES if rule.startswith("- 简报：")]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].count("。"), 2)
        for words in ("project_goal 的 background", "project_checklist 的 actions", "3–8 个动作", "状态由 Bridge 算", "action 或 item"):
            self.assertIn(words, added[0])


class BatchTest(Loop):
    """L2: what wakes the lead, 协作 on (COVEY_PROJECT_AUTOWAKE=1, so every ordinary receipt may)."""

    def setUp(self):
        super().setUp()
        self.autowake()
        mock.patch.dict(os.environ, COLLAB).start()

    def reset(self):
        shutil.rmtree(project_lead.inbox_dir(self.state, PID), ignore_errors=True)
        self.queued.clear()

    def test_topics_that_end_together_wake_the_lead_once(self):
        self.receipt(at=NOW)
        self.receipt(text="T42 做完了", at=NOW + 5)
        self.loop.tick(NOW + 10)
        self.assertEqual(self.queued, [])
        self.loop.tick(NOW + 5 + 19)
        self.assertEqual(self.queued, [])
        self.loop.tick(NOW + 5 + 20)  # COVEY_PROJECT_BATCH_SECONDS unset: 20.
        self.assertEqual([message for message, _ in self.queued], [f"lead-{PID}-1"])
        self.assertEqual(len(project_lead.take_receipts(self.state, PID, f"lead-{PID}-1")), 2)  # One turn reads both.

    def test_a_steady_trickle_waits_no_longer_than_the_stretch(self):
        for n in range(6):  # One every 15 s: each comes within the 20 s window of the one before.
            self.receipt(text=f"T4{n} 做完了", at=NOW + 15 * n)
            self.loop.tick(NOW + 15 * n + 1)
        self.assertEqual(self.queued, [])
        self.loop.tick(NOW + 20 * 4)  # The newest came 5 s ago; the oldest waited four windows.
        self.assertEqual(len(self.queued), 1)

    def test_a_start_or_a_choice_wakes_at_once_a_question_or_a_gap_after_a_moment(self):
        # W2-C: questions held and 协作 gaps that come together wake once, QUICK_SECONDS after the newest.
        for kind, wait in (("start", 0), ("owner_choice", 0), ("question", project_loop.QUICK_SECONDS),
                           (collab.RECEIPT_GAP, project_loop.QUICK_SECONDS)):
            with self.subTest(kind=kind):
                self.reset()
                self.receipt(at=NOW)
                self.receipt(kind, "要看的", at=NOW)
                self.loop.tick(NOW + 1)
                self.assertEqual(len(self.queued), 0 if wait else 1)
                if wait:
                    self.loop.tick(NOW + wait)
                    self.assertEqual(len(self.queued), 1)  # Not the ordinary window: the urgent one's.

    def test_a_consults_receipt_wakes_nobody_and_does_not_hold_the_others_back(self):
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：好", thread=THREAD, at=NOW)
        self.loop.tick(NOW + 300)
        self.assertEqual(self.queued, [])
        self.receipt(at=NOW + 300)
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_ANSWERED, "T41 请 T56 协助：已答复：好", thread=THREAD, at=NOW + 315)
        self.loop.tick(NOW + 300 + 20)
        self.assertEqual(len(self.queued), 1)

    def test_the_window_can_be_set_and_zero_or_the_switch_off_wakes_at_the_next_look(self):
        for value, seconds in (("60", 60.0), ("0", 0.0), ("2.5", 2.5), ("x", 20.0), ("-1", 20.0), ("nan", 20.0), ("", 20.0)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {project_loop.BATCH_ENV: value}):
                self.assertEqual(project_loop.batch_seconds(), seconds)
        for environ in ({project_loop.BATCH_ENV: "0"}, {turn_source.SWITCH: "0"}):
            with self.subTest(environ=environ), mock.patch.dict(os.environ, environ):
                self.reset()
                self.receipt(at=NOW)
                self.loop.tick(NOW + 1)
                self.assertEqual(len(self.queued), 1)
        with mock.patch.dict(os.environ, {turn_source.SWITCH: "0"}):
            self.assertEqual(project_loop.batch_seconds(), 0.0)


class RetellTest(Case):
    """L2: one result, told once, with consults in the inbox."""

    def wake_reported(self, kind="thread", acted=False):
        wake = f"lead-{PID}-1"
        project_lead.take_receipts(self.state, PID, wake)
        note = {"id": "n1", "at": NOW, "text": "T41 出了报告，T55 也确认了", "report": True, "kind": kind, "job": wake}
        projects.update(self.state, PID, lambda record: record.update(
            style={"report": "verbose"}, notes=[note], **({"acted": [{"job": wake, "what": "send", "at": NOW}]} if acted else {})))
        return project_loop.reported(self.state, LEAD, wake)

    def test_a_wake_that_read_a_result_the_owner_was_sent_and_a_consults_receipt_does_not_retell_them(self):
        project_lead.add_receipt(self.state, PID, "thread_end", "T41 的任务 #41 做完：报告写好了", thread=THREAD, relayed=True)
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：一致", thread=THREAD)
        self.assertFalse(self.wake_reported("thread"))
        self.assertTrue(project_loop.told_already(self.state, PID, f"lead-{PID}-1"))
        self.assertFalse(self.wake_reported("note"))
        self.assertTrue(self.wake_reported("milestone"))  # Its own judgment goes.
        # Work handed out no longer sends anything of its own (W1-A C4: report notes and questions are the channels).
        self.assertFalse(self.wake_reported("thread", acted=True))

    def test_a_wake_that_read_only_consults_receipts_retells_none_of_them(self):
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：一致", thread=THREAD)
        self.assertFalse(self.wake_reported("thread"))

    def test_a_result_the_owner_was_not_sent_beside_a_consults_receipt_is_the_leads_to_tell(self):
        project_lead.add_receipt(self.state, PID, "thread_end", "T41 的任务 #41 做完：报告写好了", thread=THREAD)
        project_lead.add_receipt(self.state, PID, collab.RECEIPT_ANSWERED, "T41 请 T55 协助：已答复：一致", thread=THREAD)
        self.assertTrue(self.wake_reported("thread"))


class ConsultTokensTest(Case):
    """L2: a consult's turn spends its project's budget (package C's handoff 7.1)."""

    def setUp(self):
        super().setUp()
        self.thread(THREAD, "汇总", 41)
        self.outside = "f" * 32
        write_topic(self.state, topic_id=self.outside, chat_id=CHAT, harness_id="claude", topic_number=55)

    def consult(self, integrator, helper, collab_id="c0ffee12", status="done"):
        channel = collab.channel_id(collab_id, helper)
        ask = collab_records.new_ask(collab_id, to=helper, frm=integrator, number=1, text="基线是哪版？", channel=channel, now=NOW)
        record = collab_records.new_record(collab_id, CHAT, root={"message_id": "om_root", "job_key": "om_root", "job_id": 40,
                                                                  "topic": integrator},
                                           integrator=collab_records.participant(integrator, collab.TOPIC),
                                           helpers=[collab_records.participant(helper, collab.TOPIC)], goal_rev=40, source={}, now=NOW)
        collab_records.change(self.state, collab_id, lambda current: {**record, "asks": [ask]})
        write_topic(self.state, topic_id=channel, chat_id=CHAT, harness_id="claude",
                    collab_channel={"collab": collab_id, "helper": helper, "integrator": integrator})
        message = collab.ask_message(ask["id"])
        source = turn_source.make(turn_source.ASK, asserted_by=turn_source.PARTY_TOPIC, origin={"topic": integrator},
                                  collab=collab_id, ask=ask["id"])
        write_job(self.state, job_key(message, 77), topic_id=channel, chat_id=CHAT, job_id=77, message_id=message, status=status,
                  input_tokens=2000, output_tokens=100, turn_source=source, final_text="基线是 v1.8")
        return make_job(job_id=77, chat_id=CHAT, message_id=message, topic_id=channel)

    def after(self, job):
        project_loop.after_runner(self.logs.append, self.state, job)

    def test_a_consults_tokens_count_once_in_its_integrators_project_and_leave_no_receipt(self):
        job = self.consult(THREAD, self.outside)
        self.after(job)
        self.after(job)
        self.assertEqual(self.reload()["tokens_used"], 2100)
        self.assertEqual(project_loop.unread(self.state, PID), [])
        self.assertEqual(project_library.entries(self.state, PID), [])

    def test_an_integrator_in_no_project_charges_the_helpers(self):
        self.after(self.consult(self.outside, THREAD))
        self.assertEqual(self.reload()["tokens_used"], 2100)

    def test_neither_in_a_project_a_turn_not_ended_or_a_record_gone_counts_nothing(self):
        self.after(self.consult(self.outside, "e" * 32))
        self.assertNotIn("tokens_used", self.reload())
        self.after(self.consult(THREAD, self.outside, collab_id="c0ffee13", status="running"))
        self.assertNotIn("tokens_used", self.reload())
        job = self.consult(THREAD, self.outside, collab_id="c0ffee14")
        (self.state / "collab" / "c0ffee14.json").unlink()
        self.after(job)
        self.assertNotIn("tokens_used", self.reload())

    def test_the_budget_counts_them(self):
        with mock.patch.dict(os.environ, {projects.BUDGET_ENV: "2000"}):
            self.after(self.consult(THREAD, self.outside))
            self.assertEqual(project_loop.hold(self.reload(), NOW), "tokens")


class GroundsTest(ToolsCase):
    """M5: project_answer's grounds, 协作 and the triage on; a member topic's choice question held for the lead."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, {**COLLAB, **TRIAGE}).start()
        self.addCleanup(mock.patch.stopall)
        self.topic = self.tool("project_topic_start", request_id="m1", title="导出旧账单", prompt="把旧库账单导出", workspace="project",
                               readonly=True, why="脚本在这个仓库")["data"]["topic_id"]
        self.path = self.state / "questions" / (TOKEN + ".json")
        write_json(self.path, question(topic=self.topic, job_key="x-1"), ensure_ascii=False)
        # Asked while the lead was idle (W1-A C4 ③: a busy lead's question goes to the owner at once); the wake-up's turn,
        # ``lead-job``, answers it.
        mock.patch.object(project_loop, "lead_busy", return_value=False).start()
        flow.Triage(runtime=SimpleNamespace(log=print), cards=mock.Mock())._route(SimpleNamespace(state_dir=self.state),
                                                                                 read_object(self.path))
        self.label = label_of(self.state, self.topic)
        app = self.env.topics.target(self.state, self.topic, CHAT)[0]
        self.submit = mock.patch.object(app.questions, "submit_answer", side_effect=self.landed).start()

    def landed(self, ctx, chat, token, fields, event_id, *, by=None):
        path = self.state / "questions" / (token + ".json")
        write_json(path, {**read_object(path), "status": "answered", "answers": {"0": {"answers": ["旧版"]}}}, ensure_ascii=False)

    def hold(self, token):
        """Another choice question of the topic, held for the lead."""
        path = self.state / "questions" / (token + ".json")
        write_json(path, question(topic=self.topic, job_key="x-" + token[:4], token=token), ensure_ascii=False)
        flow.Triage(runtime=SimpleNamespace(log=print), cards=mock.Mock())._route(SimpleNamespace(state_dir=self.state),
                                                                                 read_object(path))
        return path

    def answer(self, evidence=None):
        args = {"thread": self.label, "answers": {"0": "旧版"}}
        if evidence is not None:
            args["evidence"] = evidence
        return self.tool("project_answer", **args)

    def owner_answer(self, token="d" * 16, text="主人答：用哪个库版本？→旧版") -> str:
        """A line of the owner's answer to an earlier question card: his, with its origin (level owner), a 当前决定."""
        record = {"token": token, "topic_id": self.topic, "job_id": 3, "questions": CHOICE, "answers": {"0": {"answers": ["旧版"]}}}
        [(entry, _)] = project_memory.note_answers(self.state, PID, record, [{"index": 1, "item": "0", "text": text}])
        return entry["id"]

    def meta(self, entry, **fields):
        project_memory._edit(self.state, PID, lambda rows, meta: meta.__setitem__(entry, {**meta.get(entry, {}), **fields}))

    def still_held(self):
        self.submit.assert_not_called()
        self.assertEqual(triage.state(read_object(self.path)), triage.HELD)

    def test_without_evidence_nothing_is_answered_and_the_question_stays_the_leads(self):
        answer = self.answer()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["refused"], "evidence")
        self.assertIn("要写 evidence", answer["text"])
        self.still_held()

    def test_the_leads_own_notes_alone_are_refused_and_it_is_told_to_escalate(self):
        lead, _ = project_memory.add(self.state, PID, "这个专项一律用旧版", "decision", "lead")
        answer = self.answer([lead["id"]])
        self.assertFalse(answer["ok"])
        for words in ("没有替主人答", "待核对参考", "project_escalate"):
            self.assertIn(words, answer["text"])
        self.still_held()

    def test_an_owners_older_line_with_no_origin_is_no_ground_alone(self):
        old, _ = project_memory.add(self.state, PID, "以后都用旧版", "constraint", "owner")  # No origin: level agent.
        answer = self.answer([old["id"]])
        self.assertFalse(answer["ok"])
        self.assertIn("无出处", answer["text"])
        self.still_held()

    def test_a_voided_done_superseded_once_only_or_made_up_line_is_refused(self):
        voided, done, superseded, once, good = (self.owner_answer(token * 16) for token in "12345")
        project_memory.delete(self.state, PID, voided)
        self.meta(done, valid="done")
        self.meta(superseded, valid="superseded", superseded_by=good)
        project_memory.fix(self.state, PID, once, "once", origin={"message_id": "om_fix1"})
        for evidence, words in (([voided], "已作废"), ([done], "进了历史记录"), ([superseded], "进了历史记录"),
                                ([once], "只限当次"), (["0123456789abcdef-9"], "不在这个专项的记忆里"),
                                ([good, "0123456789abcdef-9"], "不在这个专项的记忆里"), ([good, voided], "已作废")):
            with self.subTest(evidence=evidence):
                answer = self.answer(evidence)
                self.assertFalse(answer["ok"])
                self.assertIn(words, answer["text"])
                self.assertIn("project_escalate", answer["text"])
                self.still_held()

    def test_the_owners_line_that_holds_answers_and_the_grounds_are_on_the_answer_and_its_row(self):
        owner = self.owner_answer()
        lead, _ = project_memory.add(self.state, PID, "旧版兼容现有脚本", "fact", "lead")
        answer = self.answer([f"〔{owner}〕", lead["id"]])  # Copied from the pack with its brackets.
        self.assertTrue(answer["ok"], answer)
        note = self.submit.call_args.args[3]["supplement"]
        self.assertTrue(note.startswith("（负责人替主人答的，依据："), note)
        self.assertIn(f"{owner} 你定的·决定「主人答：用哪个库版本？→旧版」", note)
        self.assertIn(f"{lead['id']} 负责人记的·资料", note)
        row = self.reload()["triage"][-1]
        self.assertTrue(row["text"].startswith("答了（依据 你定的·决定、负责人记的·资料）："), row["text"])
        self.assertEqual([(item["ref"], item["level"]) for item in row["evidence"]],
                         [(owner, turn_source.LEVEL_OWNER), (lead["id"], turn_source.LEVEL_AGENT)])
        self.assertEqual(project_facts.triage_lines(self.reload())[0][1], TOKEN)  # The 不对 button as before.
        # A 规矩 of the owner's (his own words, the message they came in kept as its origin) answers alone.
        rule, _ = project_memory.add(self.state, PID, "以后版本都选旧版，兼容现有脚本", "constraint", "owner",
                                     origin={"message_id": "om_rule1"})
        second = self.hold("e" * 16)
        answer = self.answer([rule["id"]])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(triage.state(read_object(second)), triage.ANSWERED)
        self.assertIn(f"{rule['id']} 你定的·规矩「以后版本都选旧版", self.submit.call_args.args[3]["supplement"])
        self.assertTrue(self.reload()["triage"][-1]["text"].startswith("答了（依据 你定的·规矩）："))

    def test_the_owners_own_message_in_the_project_is_a_ground(self):
        write_job(self.state, "om_said1", topic_id=LEAD, chat_id=CHAT, job_id=9, message_id="om_said1", status="done",
                  user_text="这一批都用旧版")
        for message, why in ((f"lead-{PID}-3", "a wake-up"), ("om_other", "another topic's"), ("om_them", "someone else's")):
            topic = self.outside_topic() if why == "another topic's" else LEAD
            write_job(self.state, message, topic_id=topic, chat_id=CHAT, job_id=10, message_id=message, status="done",
                      user_text="用旧版", **({"sender_id": "ou_someone_else"} if why == "someone else's" else {}))
            with self.subTest(why=why):
                answer = self.answer([f"msg:{message}"])
                self.assertFalse(answer["ok"], answer)
                self.assertIn("不是主人在这个专项里说的话", answer["text"])
                self.still_held()
        answer = self.answer(["msg:om_said1"])
        self.assertTrue(answer["ok"], answer)
        self.assertIn("msg:om_said1 主人原话「这一批都用旧版」", self.submit.call_args.args[3]["supplement"])
        self.assertTrue(self.reload()["triage"][-1]["text"].startswith("答了（依据 主人原话）："))

    def outside_topic(self):
        topic = "a" * 32
        write_topic(self.state, {"topic_id": topic}, topic_id=topic, chat_id=CHAT, harness_id="claude", topic_number=77)
        return topic

    def test_off_and_without_evidence_nothing_is_answered_either(self):
        # W0-2 (T892 P1-5): with 协作 off the answer went through on nothing; the grounds are checked whatever 协作 says.
        os.environ.pop(turn_source.SWITCH)
        answer = self.answer()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["data"]["refused"], "evidence")
        self.still_held()
        self.assertTrue(self.answer([self.owner_answer()])["ok"])  # On the owner's grounds it goes, as with 协作 on.

    def test_off_evidence_given_is_still_checked(self):
        os.environ.pop(turn_source.SWITCH)
        answer = self.answer(["0123456789abcdef-9"])
        self.assertFalse(answer["ok"])
        self.assertIn("不在这个专项的记忆里", answer["text"])
        self.still_held()


class ListingTest(Case):
    """What the lead's MCP server lists, 协作 on and off."""

    def test_off_the_leads_tools_are_as_they_were(self):
        self.assertEqual([tool["name"] for tool in butler_tool_specs.lead_tools({"COVEY_PROJECT_TRIAGE": "0"})], LEAD_NAMES)
        # project_answer asks for its evidence with 协作 off too (W0-2).
        self.assertEqual(butler_tool_specs.lead_tools({**TRIAGE})[-2:], tuple(map(butler_tool_specs.with_evidence, butler_tool_specs.TRIAGE_TOOLS)))
        self.assertEqual(butler_tool_specs.lead_tools({turn_source.SWITCH: "0", **TRIAGE}), butler_tool_specs.lead_tools(TRIAGE))
        self.assertNotIn("project_checklist", [tool["name"] for tool in butler_mcp.tool_list({butler.LEAD_ENV: PID})])
        self.assertIn("project_answer", [tool["name"] for tool in butler_tool_specs.lead_tools({})])  # Triage unset is on.

    def test_on_the_lead_has_project_checklist_and_project_answer_asks_for_evidence(self):
        tools = butler_tool_specs.lead_tools({**COLLAB, **TRIAGE})
        self.assertEqual([tool["name"] for tool in tools], [*LEAD_NAMES, "project_checklist", "project_answer", "project_escalate"])
        answer = next(tool for tool in tools if tool["name"] == "project_answer")
        self.assertEqual(answer["input_schema"]["required"], ["thread", "answers", "evidence"])
        self.assertIn("evidence", answer["input_schema"]["properties"])
        self.assertIn(butler_tool_specs.EVIDENCE_NOTE, answer["description"])
        self.assertNotIn("evidence", butler_tool_specs.TRIAGE_TOOLS[0]["input_schema"]["properties"])  # Not changed in place.
        self.assertEqual([tool["name"] for tool in butler_tool_specs.lead_tools({**COLLAB, "COVEY_PROJECT_TRIAGE": "0"})], [*LEAD_NAMES, "project_checklist"])
        listed = [tool["name"] for tool in butler_mcp.tool_list({butler.LEAD_ENV: PID, **COLLAB})]
        self.assertIn("project_checklist", listed)
        self.assertNotIn("project_checklist", [tool["name"] for tool in butler_tool_specs.listed({**COLLAB, projects.ENV: "1"})])

    def test_the_brief_arguments_are_listed_with_collab_on_only_and_the_shared_specs_are_not_changed(self):
        woken = {butler_tool_specs.AUTOWAKE_ENV: "1"}
        off = {tool["name"]: tool for tool in butler_tool_specs.lead_tools(woken)}
        for tool in butler_tool_specs.PROJECT_TOOLS:
            self.assertEqual(off[tool["name"]], tool)  # Off: as they were.
        on = {tool["name"]: tool for tool in butler_tool_specs.lead_tools({**COLLAB, **woken})}
        for name in ("project_topic_start", "project_topic_send"):
            self.assertEqual(sorted(set(on[name]["input_schema"]["properties"]) - set(off[name]["input_schema"]["properties"])), ["action", "item"])
            self.assertEqual(on[name]["input_schema"]["required"], off[name]["input_schema"]["required"])
            self.assertEqual(on[name]["description"], off[name]["description"])
        goal = on["project_goal"]["input_schema"]
        self.assertEqual((sorted(goal["properties"]), "required" in goal, off["project_goal"]["input_schema"]["required"]),
                         (["background", "text"], False, ["text"]))
        checklist_schema = on["project_checklist"]["input_schema"]["properties"]
        self.assertEqual(sorted(checklist_schema), ["actions", "close_gaps", "items"])
        self.assertEqual(sorted(checklist_schema["actions"]["items"]["properties"]), ["id", "items", "note", "state", "title", "topics"])
        self.assertTrue({"title", "topics", "waiting_on"} <= set(checklist_schema["items"]["items"]["properties"]))
        self.assertEqual(checklist_schema["items"]["items"]["properties"]["waiting_on"]["enum"], ["owner", "none"])  # No '' (T839).
        for tool in butler_tool_specs.PROJECT_TOOLS:  # Not changed in place.
            self.assertFalse({"action", "item", "background"} & set(tool["input_schema"]["properties"]))

    def test_a_call_to_project_checklist_on_a_server_without_the_switch_is_refused(self):
        server = butler_mcp.Server({butler.STATE_ENV: str(self.state), butler.JOB_ENV: "k", butler.LEAD_ENV: PID},
                                   submit=mock.Mock())
        answer = server.call({"name": "project_checklist", "arguments": {}})
        self.assertTrue(answer["isError"])
        self.assertIn("没有开 project_checklist", answer["content"][0]["text"])
        server.submit.assert_not_called()

    def test_the_leads_server_is_handed_the_switch_and_the_butlers_is_not(self):
        with mock.patch.dict(os.environ, COLLAB):
            lead = butler.mcp_config(Path("/r"), self.state, "k", sys.executable, lead=PID)
            plain = butler.mcp_config(Path("/r"), self.state, "k", sys.executable)
        self.assertEqual(lead["mcpServers"][butler.MCP_SERVER]["env"][turn_source.SWITCH], "1")
        self.assertNotIn(turn_source.SWITCH, plain["mcpServers"][butler.MCP_SERVER]["env"])
        off = butler.mcp_config(Path("/r"), self.state, "k", sys.executable, lead=PID)
        self.assertNotIn(turn_source.SWITCH, off["mcpServers"][butler.MCP_SERVER]["env"])


if __name__ == "__main__":
    unittest.main()

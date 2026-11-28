"""记忆自己长, a turn at a time (COVEY_MEMORY_LEARN=1, app/memory_learn.py, app/memory_turn.py, core/memory.py drafts and
``seen``, core/memory_usage.py): the owner's own words of an ended turn read by the Bridge and written as 你说的 where they
belong, the lines a turn used noted, the candidates carried as 它总结的, the block's lookup line, and that with the switch off
everything is as it was."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import UNSET_OFF
from support import make_context
from support.records import write_job, write_topic

from covey.app import memory_collect, memory_learn, memory_turn
from covey.app.answers import FEEDBACK_START
from covey.cards.answer_card import memory_element
from covey.core import memory, memory_review, memory_usage, memstore, principles, project_memory, projects, topic_memory
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import find_job_record
from covey.core.state import write_json

ON = {memory.ENV: "1", memory.LEARN_ENV: "1", projects.ENV: "1", projects.MEMORY_ENV: "1"}
OWNER, DM = "ou_owner", "oc_dm"
PID, CHAT = "a1b2c3d4e5f6", "oc_owner_dm"
NOW = 1_800_000_000.0


def envelope(value) -> str:
    """The small model's answer as Claude Code's JSON envelope (app/track_judge.py answer_text)."""
    return json.dumps({"result": json.dumps(value, ensure_ascii=False)})


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env(**ON)
        self.logs = []
        self.runtime = mock.Mock(log=self.logs.append, access=mock.Mock(owner_turn=lambda chat, sender: sender == OWNER))
        self.messenger = mock.Mock()
        self.messenger.patch_card.return_value = True
        self.ctx = make_context(Path(tmp.name))
        self.state = self.ctx.state_dir
        write_topic(self.state, topic_id="t1", topic_number=7, origin={"title": "选型"})
        self.ctx.topic_id = "t1"
        self.count = 4
        self.reviewed = []  # The review queue (core/memory_review.py add) is package M2's other half: a stand-in keeps the items.

        def add(state_dir, item):  # (its key, whether it is new): a proposal there already says no 待审核 again.
            same = next((n for n, old in enumerate(self.reviewed) if (old.type, old.text, old.suggest) == (item.type, item.text, item.suggest)), None)
            if same is not None:
                return f"review:learn:{same + 1:06x}", False
            self.reviewed.append(item)
            return f"review:learn:{len(self.reviewed):06x}", True
        patcher = mock.patch.object(memory_review, "add", side_effect=add)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def turn(self, user_text="以后汇报都先给结论，再给表", *, status="done", sender=OWNER, key="", checkpoint=None, **record):
        self.count += 1
        key = key or f"om_{self.count}"  # A message id of the owner's the Bridge took in (core/turn_source.py DIRECT).
        path = self.state / f"cp-{key}.json"
        write_json(path, checkpoint or {"phase": "完成"})
        write_job(self.state, key, job_id=self.count, topic_id=record.pop("topic", "t1"), chat_id=DM, status=status,
                  user_text=user_text, message_id=record.pop("message_id", key), workspace_name="ws", checkpoint_path=str(path), sender_id=sender,
                  final_text=record.pop("final_text", "结论：可以。"), **record)
        return ReplyJob(job_id=self.count, user_text=user_text, chat_id=DM, message_id=key, timeout=0, sender_id=sender,
                        topic_id="t1")

    def learn(self, job, answer):
        record = find_job_record(self.state, job.message_id)
        asked = []

        def ask(system, data, **kwargs):
            asked.append(json.loads(data))
            return envelope(answer)
        notes = memory_learn.learn(self.runtime, self.messenger, self.ctx, record, None, ask=ask)
        return notes, asked, find_job_record(self.state, job.message_id)


class SwitchTest(unittest.TestCase):
    def test_the_old_names_turn_the_memory_and_its_learning_on(self):
        # A Manager from before the rename starts the Bridge with the manifest's LARK_BRIDGE_ names only (T778: a colleague's
        # page had no 记忆 and no turn carried the memory although his manifest set LARK_BRIDGE_MEMORY=1).
        self.assertTrue(memory.enabled({"LARK_BRIDGE_MEMORY": "1"}))
        self.assertTrue(memory.learning({"LARK_BRIDGE_MEMORY": "1", "LARK_BRIDGE_MEMORY_LEARN": "1"}))
        self.assertTrue(memory.learning({"COVEY_MEMORY": "1", "LARK_BRIDGE_MEMORY_LEARN": "1"}))
        self.assertFalse(memory.enabled({"COVEY_MEMORY": "0", "LARK_BRIDGE_MEMORY": "1"}))  # The new name, when set, decides.
        self.assertFalse(memory.learning({"COVEY_MEMORY": "0", "LARK_BRIDGE_MEMORY_LEARN": "1"}))  # Learning needs the memory.
        self.assertFalse(memory.learning({"LARK_BRIDGE_MEMORY": "0", "LARK_BRIDGE_MEMORY_LEARN": "1"}))
        self.assertFalse(memory.enabled({"LARK_BRIDGE_MEMORY": "0"}))
        self.assertTrue(memory.enabled({}))  # Unset is on.
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_MEMORY": "1"}, clear=True):
            self.assertTrue(memory.enabled())


class OwnerWordsTest(Case):
    def test_an_answers_turn_is_what_he_answered_and_added_never_the_question(self):
        said = "用户补充回答：\n要不要再单独加一个「整理」？\n不加，看板卡里有就够（推荐）\n\n用户补充说明：\n以后这类按推荐走"
        words = memory_turn.owner_words({"user_text": said, "message_id": "answer-0123456789abcdef"}, self.state)
        self.assertNotIn("要不要再", words)
        self.assertIn("不加，看板卡里有就够", words)
        self.assertIn("以后这类按推荐走", words)

    def test_a_feedback_is_the_feedback_and_a_message_is_itself(self):
        self.assertEqual(memory_turn.owner_words({"user_text": FEEDBACK_START + "别问了"}), "别问了")
        self.assertEqual(memory_turn.owner_words({"user_text": "以后都这样"}), "以后都这样")

    def test_the_old_paths_trigger_and_quote_check_take_his_words_alone(self):
        self.env(**{memory.LEARN_ENV: ""})
        said = "用户补充回答：\n底部菜单要不要再加一个按钮？\n不加"
        job = self.turn(said, key="answer-0123456789abcdef",
                        checkpoint={"memory": [{"quote": "要不要再加一个按钮", "text": "不要再加按钮"}]})
        memory_collect.collect(self.runtime, self.messenger, job, self.ctx)
        self.assertNotIn("memory_notes", find_job_record(self.state, job.message_id))  # 要不要再 was the question's.
        self.assertFalse(memory_turn.fix_rule(self.ctx, job, lambda: True))


class LearnTest(Case):
    def test_a_rule_for_every_topic_holds_where_it_was_said_and_waits_for_global_and_a_workspace_one_is_the_topics(self):
        # T908 M3a: 处处 → in force where it was said at once, and 待审核 asks whether it is to hold 全局 (its line in items).
        job = self.turn("以后汇报都先给结论，再给表；这个仓库别再跑全量测试")
        answer = {"remember": [{"quote": "以后汇报都先给结论，再给表", "text": "汇报先给结论再给表", "scope": "global"},
                               {"quote": "这个仓库别再跑全量测试", "text": "这个仓库不跑全量测试", "scope": "workspace"}],
                  "void": []}
        notes, asked, record = self.learn(job, answer)
        # The workspace is no scope of the memory any more: that line is the topic's, written at once.
        self.assertEqual([(note["kind"], note["text"], note["scope"]) for note in notes],
                         [("remembered", "汇报先给结论再给表", "管这个话题"),
                          ("pending", "汇报先给结论再给表", memory_collect.WIDER_WORDS),
                          ("remembered", "这个仓库不跑全量测试", "管这个话题")])
        self.assertEqual(memstore.entries(self.state, scope="global"), [])  # 全局 only with his accept.
        self.assertEqual([row["text"] for row in topic_memory.live(self.state, "t1")], ["汇报先给结论再给表", "这个仓库不跑全量测试"])
        self.assertEqual([(item.type, item.text, item.suggest) for item in self.reviewed],
                         [("learn", "汇报先给结论再给表", {"kind": "rule", "scope": "global"})])
        self.assertEqual(self.reviewed[0].items, (notes[0]["ref"],))
        self.assertEqual(self.reviewed[0].quote, "以后汇报都先给结论，再给表")
        self.assertEqual(self.reviewed[0].origin, {"topic_key": "t1"})
        self.assertEqual(record["memory_notes"], notes)
        self.assertEqual(record["memory_learned"]["state"], "done")
        self.assertEqual(asked[0]["said"], "以后汇报都先给结论，再给表；这个仓库别再跑全量测试")
        self.assertEqual(asked[0]["topic"], "选型")
        self.assertNotIn("project", asked[0])
        self.assertFalse(any("汇报" in line for line in self.logs))  # His words never in a log line.
        card = json.dumps(memory_element(notes, "t1", "4"), ensure_ascii=False)
        self.assertIn("待审核：汇报先给结论再给表 · " + memory_collect.WIDER_WORDS, card)
        self.assertEqual(card.count("bridge_memory_wrong"), 3)  # 不对 on each: on 待审核 it turns the proposal down.
        with mock.patch.object(memstore, "review_set") as rejected:
            after = memory_collect.take_back(self.state, "t1", record, notes[1]["entry_id"])
        self.assertEqual(after[1]["kind"], "undone")
        rejected.assert_called_once_with(self.state, notes[1]["entry_id"], "rejected", by="owner")

    def test_a_project_line_is_the_projects_at_once_and_a_note_says_it_is_carried_when_related(self):
        # T908 M3a: 这个专项 → the project's, no 待审核 in a project's topic any more; 记一下 → a note (资料).
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.MEMBER))
        job = self.turn("这个专项以后都先跑冒烟；记一下：账单表按日期分区；这件事别再先问")
        notes, asked, _ = self.learn(job, {"remember": [
            {"quote": "这个专项以后都先跑冒烟", "text": "合并前先跑冒烟测试", "kind": "rule", "scope": "project"},
            {"quote": "记一下：账单表按日期分区", "text": "账单表按日期分区", "kind": "note", "scope": "project"}]})
        self.assertEqual(asked[0]["project"], "迁移账单")
        self.assertEqual([(note["kind"], note["scope"], note.get("entry_kind")) for note in notes],
                         [("remembered", "管这个专项", None), ("remembered", "管这个专项", "note")])
        self.assertEqual(self.reviewed, [])
        found = {entry.text: entry for entry in memstore.entries(self.state, scope="project", owner_key=PID)}
        self.assertEqual((found["合并前先跑冒烟测试"].kind, found["合并前先跑冒烟测试"].said), ("rule", "owner"))
        self.assertEqual((found["账单表按日期分区"].kind, found["账单表按日期分区"].said), ("note", "owner"))
        self.assertEqual(found["合并前先跑冒烟测试"].origin.get("message_id"), job.message_id)
        self.assertEqual(found["合并前先跑冒烟测试"].origin.get("quote"), "这个专项以后都先跑冒烟")
        card = json.dumps(memory_element(notes, "t1", "4"), ensure_ascii=False)
        self.assertIn("记下了：账单表按日期分区（相关时才带） · 管这个专项", card)
        self.assertNotIn("合并前先跑冒烟测试（相关时才带）", card)

    def test_a_learned_rule_that_would_let_a_turn_skip_asking_waits_and_holds_only_once_accepted(self):
        # T908 M3a: the five kinds' one list (core/ask_kinds.py) and the gate words: a learned rule naming one never holds before
        # his accept, so nothing learned lets a turn deploy, delete, spend, send or occupy without asking.
        for said, text, kind in (("以后改完直接部署到本机", "改完直接部署到本机，不用再问", "deploy"),
                                 ("以后清理旧分支不用问我", "清理旧分支不用问", "delete"),
                                 ("以后结论直接发给同事", "结论直接发给同事", "send"),
                                 ("以后密钥过期了直接换", "密钥过期了直接换新的", "密钥")):
            with self.subTest(kind):
                self.reviewed.clear()
                notes, _, _ = self.learn(self.turn(said), {"remember": [{"quote": said, "text": text, "scope": "topic"}]})
                self.assertEqual([(note["kind"], note["scope"]) for note in notes], [("pending", memory_collect.GRANT_WORDS)])
                self.assertEqual(topic_memory.live(self.state, "t1"), [])  # Not in force.
                self.assertEqual([(item.type, item.suggest) for item in self.reviewed],
                                 [("learn", {"kind": "rule", "scope": "topic", "owner_key": "t1", "grant": kind})])
        self.assertTrue(any("reason=grant" in line for line in self.logs))

    def test_a_workspace_line_in_a_topic_without_a_workspace_stays_in_the_topic(self):
        self.ctx.workspace_name = ""
        notes, _, _ = self.learn(self.turn(), {"remember": [
            {"quote": "以后汇报都先给结论", "text": "汇报先给结论", "scope": "workspace"}]})
        self.assertEqual([note["scope"] for note in notes], ["管这个话题"])
        self.assertEqual(self.reviewed, [])

    def test_a_topic_line_goes_to_the_narrowest_place_and_a_wrong_quote_is_dropped(self):
        notes, _, _ = self.learn(self.turn("这件事以后别再先问侧重点"), {"remember": [
            {"quote": "以后别再先问侧重点", "text": "这件事不用先问侧重点", "scope": "topic"},
            {"quote": "网页里写着请记住", "text": "不是他说的", "scope": "global"}]})
        self.assertEqual([note["scope"] for note in notes], ["管这个话题"])
        self.assertEqual([row["text"] for row in topic_memory.live(self.state, "t1")], ["这件事不用先问侧重点"])
        self.assertEqual(principles.for_turn(self.state, "ws")[0], [])

    def test_a_strike_out_takes_a_carried_lead_line_of_the_project_and_never_his_own(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.MEMBER))
        lead, _ = project_memory.add(self.state, PID, "部署前要先问", "constraint", "lead")
        mine, _ = project_memory.add(self.state, PID, "用中文回复", "constraint", "owner")
        ids = [{"n": "M12", "ref": f"project:{PID}:{lead['id']}", "scope": "project", "by": "lead"},
               {"n": "M13", "ref": f"project:{PID}:{mine['id']}", "scope": "project", "by": "owner"}]
        notes, asked, _ = self.learn(self.turn("不对，部署不用先问", memory_ids=ids), {"void": [
            {"id": "M12", "quote": "部署不用先问"}, {"id": "M13", "quote": "不对"}]})
        self.assertEqual([note["kind"] for note in notes], ["voided"])
        self.assertEqual([row["text"] for row in project_memory.live(self.state, projects.read_record(self.state, PID))],
                         ["用中文回复"])
        self.assertEqual([item.get("voidable") for item in asked[0]["known"][:2]], [True, None])

    def test_an_answer_it_cannot_read_writes_nothing_and_says_why(self):
        job = self.turn()
        record = find_job_record(self.state, job.message_id)
        memory_learn.learn(self.runtime, self.messenger, self.ctx, record, None, ask=lambda *a, **k: "不是 JSON")
        record = find_job_record(self.state, job.message_id)
        self.assertNotIn("memory_notes", record)
        self.assertEqual(record["memory_learned"]["state"], "failed")


class ProposedOnceTest(Case):
    def test_a_line_waiting_already_is_not_said_to_wait_again(self):
        said = "以后汇报都先给结论，再给表"
        answer = {"remember": [{"quote": said, "text": "汇报先给结论再给表", "scope": "global"}], "void": []}
        first, _, _ = self.learn(self.turn(said), answer)
        self.assertEqual([note["kind"] for note in first], ["remembered", "pending"])
        self.count += 1
        again, _, record = self.learn(self.turn(said), answer)
        self.assertEqual(again, [])  # T914 M1: the line and the item are the ones there already: the card says nothing again.
        self.assertEqual(len(self.reviewed), 1)


class RefusalTest(Case):
    """T852: what the learner must not keep, even when the model says it should."""

    def test_a_bug_report_with_a_time_in_it_is_not_a_workspace_rule(self):
        said = "还是展开了这一轮中途的汇报，这块Covey不是上午刚优化了么"
        notes, _, record = self.learn(self.turn(said), {"remember": [
            {"quote": said, "text": "汇报按上午已优化的方式处理，不展开中途的汇报", "scope": "workspace"}]})
        self.assertEqual(notes, [])
        self.assertEqual(memstore.review_items(self.state), [])
        self.assertEqual(principles.for_turn(self.state, "ws"), ([], []))
        self.assertTrue(any("reason=relative-time" in line for line in self.logs), self.logs)
        self.assertFalse(any("展开" in line for line in self.logs))

    def test_a_complaint_or_a_plain_question_without_from_now_on_is_refused_but_a_rule_with_a_time_that_comes_round_is_not(self):
        for text, quote, why in (("汇报不要展开中途的过程", "又报错了，汇报怎么还是展开了中途的过程总是不对", "complaint"),
                                 ("回复前先查日志", "为什么回复前要先查日志呢？", "question"),
                                 ("这类问题不再用上次的办法", "这类问题上次的办法不要了", "relative-time")):
            with self.subTest(why):
                self.assertEqual(memory_learn.refusal(text, quote), why)
        self.assertEqual(memory_learn.refusal("每天上午发汇总", "以后每天上午发汇总"), "")
        self.assertEqual(memory_learn.refusal("报错时先贴日志", "以后报错了先贴日志"), "")

    def test_the_prompt_says_what_is_not_remembered(self):
        for words in ("报 Bug", "纯提问", "上午", "这一轮", "这批", "这类", "上次", "今天", "supersede"):
            self.assertIn(words, memory_learn.SYSTEM)


class SupersedeTest(Case):
    def project(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.MEMBER))

    def test_a_project_line_he_contradicts_is_put_away_with_the_new_one_and_a_receipt_that_can_be_taken_back(self):
        self.project()
        old, _ = project_memory.add(self.state, PID, "汇报先给结论", "constraint", "owner")
        ids = [{"n": "M12", "ref": f"project:{PID}:{old['id']}", "scope": "project", "by": "owner"}]
        job = self.turn("汇报不用先给结论了，直接按时间线写", memory_ids=ids)
        notes, asked, record = self.learn(job, {"supersede": [
            {"id": "M12", "quote": "汇报不用先给结论了", "text": "汇报按时间线写"}]})
        self.assertEqual([note["kind"] for note in notes], ["remembered", "superseded"])
        rows = {row["id"]: row for row in project_memory.stored(self.state, PID)}
        self.assertEqual(rows[old["id"]]["valid"], "superseded")
        self.assertEqual(rows[old["id"]]["superseded_by"], notes[0]["entry_id"])
        self.assertEqual(rows[notes[0]["entry_id"]]["text"], "汇报按时间线写")
        self.assertIn("M12：汇报先给结论", notes[1]["text"])
        self.assertIn("因为你这轮说“汇报不用先给结论了”", notes[1]["text"])
        card = json.dumps(memory_element(notes, "t1", "4"), ensure_ascii=False)
        self.assertIn("收起了：M12：汇报先给结论", card)
        self.assertEqual(card.count("bridge_memory_wrong"), 2)
        memory_collect.take_back(self.state, "t1", record, old["id"])  # 不对 on the receipt.
        again = {row["id"]: row for row in project_memory.stored(self.state, PID)}
        self.assertNotIn("valid", again[old["id"]])
        self.assertEqual(find_job_record(self.state, job.message_id)["memory_notes"][1]["kind"], "undone")

    def test_a_topic_line_he_contradicts_is_deleted_and_comes_back_with_不对(self):
        entry, _ = topic_memory.add(self.state, "t1", "这件事先问侧重点")
        ids = [{"n": "M30", "ref": f"topic:{entry['id']}", "scope": "topic", "by": "owner"}]
        job = self.turn("这件事不用先问侧重点了，直接做", memory_ids=ids)
        notes, _, record = self.learn(job, {"supersede": [{"id": "M30", "quote": "不用先问侧重点了", "text": ""}]})
        self.assertEqual([note["kind"] for note in notes], ["superseded"])
        self.assertEqual(topic_memory.live(self.state, "t1"), [])
        memory_collect.take_back(self.state, "t1", record, entry["id"])
        self.assertEqual([row["text"] for row in topic_memory.live(self.state, "t1")], ["这件事先问侧重点"])

    def test_an_entry_of_global_is_never_touched_what_he_says_now_waits_and_the_receipt_says_which(self):
        old = memstore.add(self.state, kind="rule", scope="global", text="回复用中文")
        ids = [{"n": "M01", "ref": old.ref, "scope": "global", "by": "owner"}]
        job = self.turn("以后这些回复用英文，不要用中文", memory_ids=ids)
        notes, _, _ = self.learn(job, {"supersede": [{"id": "M01", "quote": "以后这些回复用英文", "text": "回复用英文"}]})
        # T908 M3a: what he says now holds where he said it; 待审核 asks whether it is to hold 全局.
        self.assertEqual([note["kind"] for note in notes], ["remembered", "pending"])
        self.assertIn("和 M01：回复用中文 矛盾，那条没动", notes[1]["text"])
        self.assertEqual([entry.text for entry in memstore.entries(self.state, scope="global")], ["回复用中文"])
        self.assertEqual([row["text"] for row in topic_memory.live(self.state, "t1")], ["回复用英文"])
        self.assertEqual([item.text for item in self.reviewed], ["回复用英文"])

    def test_a_quote_that_is_not_his_or_an_id_the_turn_did_not_carry_changes_nothing(self):
        entry, _ = topic_memory.add(self.state, "t1", "这件事先问侧重点")
        ids = [{"n": "M30", "ref": f"topic:{entry['id']}", "scope": "topic", "by": "owner"}]
        job = self.turn("以后都这样", memory_ids=ids)
        notes, _, _ = self.learn(job, {"supersede": [{"id": "M30", "quote": "他没说过的话", "text": ""},
                                                     {"id": "M99", "quote": "以后都这样", "text": ""}]})
        self.assertEqual(notes, [])
        self.assertEqual(len(topic_memory.live(self.state, "t1")), 1)


class LeadAndQuestionTest(Case):
    def lead(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.LEAD))

    def test_the_owners_words_to_a_lead_are_read_but_the_bridges_wake_up_of_it_never(self):
        self.lead()
        read = self.turn("以后汇报都先给结论")
        wake = self.turn("[Bridge 唤醒] 专项状态里的收件箱有新的回执，主人不在场：以后每次先查状态")
        again = self.turn("你总是先问，说过别这样")  # One set of trigger words, a lead's topic too (core/memory.py CUES).
        loose = self.turn("先看看，别动")
        reasons = [memory_learn.reason_to_skip(self.runtime, self.ctx, job, find_job_record(self.state, job.message_id), {})
                   for job in (read, wake, again, loose)]
        self.assertEqual(reasons, ["", "not-owner-words", "", "no-cue"])

    def test_in_a_lead_topic_his_words_are_the_projects_as_his(self):
        # T908 M3a: a lead's topic has no line of its own; what he says there is the project's, his own (the lead notes only its
        # own summaries, app/project_lead.py), and 处处 waits for 全局 in 待审核 while it holds in the project.
        self.lead()
        job = self.turn("以后汇报都先给结论；这件事别再先问")
        notes, asked, _ = self.learn(job, {"remember": [
            {"quote": "以后汇报都先给结论", "text": "汇报先给结论", "scope": "global"},
            {"quote": "这件事别再先问", "text": "这件事不先问", "scope": "topic"}]})
        self.assertEqual([(note["kind"], note["text"], note["scope"]) for note in notes],
                         [("remembered", "汇报先给结论", "管这个专项"), ("pending", "汇报先给结论", memory_collect.WIDER_WORDS),
                          ("remembered", "这件事不先问", "管这个专项")])
        self.assertEqual(asked[0]["project"], "迁移")
        self.assertEqual(topic_memory.live(self.state, "t1"), [])
        found = memstore.entries(self.state, scope="project", owner_key=PID)
        self.assertEqual(sorted((entry.text, entry.said, entry.origin.get("message_id")) for entry in found),
                         [("汇报先给结论", "owner", job.message_id), ("这件事不先问", "owner", job.message_id)])
        self.assertEqual([(item.suggest, item.items) for item in self.reviewed],
                         [({"kind": "rule", "scope": "global"}, (notes[0]["ref"],))])

    def test_the_lead_notes_only_its_own_summaries_while_the_bridge_notes_his_words(self):
        # T908 M3a: never the same words twice: with 记忆自己长 reading the lead's topic the lead's rules say project_remember is
        # for its own summaries; without it, as before, the lead notes his words.
        from covey.app import project_lead
        noted, before = project_lead.lead_rules(noted=True), project_lead.lead_rules()
        self.assertIn(project_lead.LEAD_REMEMBER, noted)
        self.assertNotIn(project_lead.OWNER_REMEMBER, noted)
        self.assertIn(project_lead.OWNER_REMEMBER, before)
        self.assertNotIn(project_lead.LEAD_REMEMBER, before)
        self.lead()
        self.assertIn(project_lead.LEAD_REMEMBER, project_lead.rules_of(self.ctx, self.turn()))
        self.env(**{memory.LEARN_ENV: ""})
        self.assertIn(project_lead.OWNER_REMEMBER, project_lead.rules_of(self.ctx, self.turn()))

    def question(self, token="0123456789abcdef", text="是否删掉记忆中这一部分？"):
        write_json(self.state / "questions" / f"{token}.json", {"questions": [
            {"id": "q1", "question": text, "options": [{"label": "删掉", "description": "从记忆里拿掉"},
                                                       {"label": "留着"}]}]})
        return self.turn(f"用户补充回答：\n{text}\n删掉这部分", key=f"answer-{token}")

    def test_an_answer_to_a_question_card_goes_to_the_model_with_the_cards_words_and_options(self):
        job = self.question()
        record = find_job_record(self.state, job.message_id)
        self.assertEqual(memory_learn.reason_to_skip(self.runtime, self.ctx, job, record, {}), "")  # The card speaks of 记忆.
        notes, asked, _ = self.learn(job, {"remember": []})
        self.assertEqual(asked[0]["said"], "删掉这部分")
        self.assertEqual(asked[0]["asked"], [{"question": "是否删掉记忆中这一部分？",
                                              "options": ["删掉 从记忆里拿掉", "留着"]}])
        self.assertIn("题面不是他说的", memory_learn.SYSTEM)

    def test_a_card_that_does_not_speak_of_memory_and_an_answer_with_no_cue_are_not_read(self):
        job = self.question(token="fedcba9876543210", text="要不要加一个按钮？")
        record = find_job_record(self.state, job.message_id)
        self.assertEqual(memory_learn.reason_to_skip(self.runtime, self.ctx, job, record, {}), "no-cue")
        _, asked, _ = self.learn(self.turn("帮我看看", key="om90"), {"remember": []})
        self.assertNotIn("asked", asked[0])

    def test_the_question_cards_words_are_never_a_quote_of_his(self):
        job = self.question()
        notes, _, _ = self.learn(job, {"remember": [{"quote": "是否删掉记忆中这一部分", "text": "删掉记忆", "scope": "topic"}]})
        self.assertEqual(notes, [])


class AfterTurnTest(Case):
    def gateway(self):
        return mock.Mock(runtime=self.runtime, topics=mock.Mock(apps={}))

    def test_which_turns_are_read(self):
        cases = {"not-finished": dict(status="failed"), "not-owner": dict(sender="ou_other"),
                 "handed-on": dict(key="main-tok9"), "no-cue": dict(user_text="帮我看看这个"),
                 "collected": dict(checkpoint={"memory": [{"quote": "以后", "text": "x"}]}),
                 # T852: the Bridge's own turns are never his words (the source decides, the text is a last resort).
                 "not-owner-words": dict(key="revisit-abcdef-170-0", user_text="[Bridge 叫醒] 以后每次都先查看当前状态"),
                 "not-owner-words ": dict(user_text="[Bridge 叫醒] 到了回看时间：以后每次都先查看当前状态"),
                 "not-owner-words  ": dict(user_text="[Bridge 说明] 负责人转来：以后每次开工前先查看当前状态"),
                 "not-owner-words   ": dict(turn_source={"v": 1, "kind": "collab_reply", "asserted_by": "owner"}),
                 "not-owner-words    ": dict(turn_source={"v": 1, "kind": "handover", "asserted_by": "butler"}),
                 # The everyday words are no cue any more.
                 "no-cue ": dict(user_text="先看看日志，别动配置，不要删文件，不是这个，应该改成 B")}
        for reason, fields in cases.items():
            reason = reason.strip()
            with self.subTest(reason, **fields):
                job = self.turn(**fields)
                record = find_job_record(self.state, job.message_id)
                self.assertEqual(memory_learn.reason_to_skip(self.runtime, self.ctx, job, record, memory_learn._checkpoint(record)),
                                 reason)
        job = self.turn()
        record = find_job_record(self.state, job.message_id)
        self.assertEqual(memory_learn.reason_to_skip(self.runtime, self.ctx, job, record, {}), "")
        with mock.patch("covey.app.memory_learn.topic_redaction", return_value=True):
            self.assertEqual(memory_learn.reason_to_skip(self.runtime, self.ctx, job, record, {}), "redacted")

    def test_a_turn_is_handed_to_the_learner_once(self):
        job = self.turn()
        started = []
        routed = memory_learn.assist.Route(memory_learn.assist.Choice("claude"))
        with mock.patch.object(memory_learn.assist, "route", return_value=routed), \
                mock.patch.object(memory_learn, "start", side_effect=lambda work: started.append(work) or True):
            memory_learn.after_turn(self.gateway(), mock.Mock(messenger=self.messenger), job, self.ctx)
            memory_learn.after_turn(self.gateway(), mock.Mock(messenger=self.messenger), job, self.ctx)
        self.assertEqual(len(started), 1)
        self.assertEqual(find_job_record(self.state, job.message_id)["memory_learned"]["state"], "asked")

    def test_off_without_the_switch(self):
        self.env(**{memory.LEARN_ENV: ""})
        job = self.turn(memory_ids=[{"n": "M12", "ref": "principle:p1", "scope": "global", "by": "owner"}],
                        final_text="照 M12 做了")
        with mock.patch.object(memory_learn, "start") as start:
            memory_learn.after_turn(self.gateway(), mock.Mock(), job, self.ctx)
        start.assert_not_called()
        record = find_job_record(self.state, job.message_id)
        self.assertNotIn("memory_used", record)
        self.assertNotIn("memory_learned", record)
        self.assertEqual(memory_usage.read(self.state), {})

    def test_the_lines_a_turn_used_are_counted_by_the_observer_not_here(self):
        ids = [{"n": "M12", "ref": "principle:p1", "scope": "global", "by": "owner"},
               {"n": "M40", "ref": "candidate:c0a1b2c3d", "scope": "global", "by": "lead"}]
        job = self.turn("帮我看看", memory_ids=ids, final_text="按（M12）做了，没再问", checkpoint={"memory_used": ["M40", "M99"]})
        with mock.patch.object(memory_learn, "start"):
            memory_learn.after_turn(self.gateway(), mock.Mock(), job, self.ctx)
        self.assertNotIn("memory_used", find_job_record(self.state, job.message_id))
        self.assertEqual(memory_usage.read(self.state), {})


class TurnBlockTest(Case):
    def job(self, owner=True):
        return ReplyJob(job_id=1, user_text="看看", chat_id=DM, message_id="om1", timeout=0,
                        sender_id=OWNER if owner else "ou_other", topic_id="t1")

    def carried(self, text="汇报先给结论", workspace=""):
        write_json(Path(self.state) / "memory-candidates.json", {"schema_version": 1, "rows": [  # An older release's.
            {"id": "c0a1b2c3d", "text": text, "topics": [], "labels": [], "workspace": workspace, "slot": NOW, "at": NOW,
             "state": "open", "carried": True, "source": "repeat"}], "declined": [], "ran": []})

    def test_a_candidate_is_never_carried_and_the_rules_say_how_to_look_the_memory_up(self):
        self.carried()
        memstore.add(self.state, kind="rule", scope="global", text="用中文回复")
        block = memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True)
        self.assertNotIn("汇报先给结论", block)  # It waits in 待审核, not in the turn.
        self.assertIn("用中文回复", block)
        self.assertIn("memory_used", block)
        self.assertIn("covey_memory.py search", block)
        self.assertIn("--owner", block)
        self.assertNotIn("汇报先给结论", memory_turn.tail(self.ctx, self.job(), lambda: True))

    def test_a_workspace_candidate_is_not_read_on_anybodys_turn(self):
        self.carried(workspace="ws")
        self.assertEqual(memory_turn.block(self.ctx, self.job(False), owner_turn=lambda: False), "")
        self.assertEqual(memory_turn.tail(self.ctx, self.job(False), lambda: False), "")

    def test_off_the_block_is_the_same_and_the_correction_rule_comes_back(self):
        memstore.add(self.state, kind="rule", scope="global", text="用中文回复")
        on = memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True)
        memory_turn._plans.clear()
        self.env(**{memory.LEARN_ENV: ""})
        self.assertEqual(memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True), on)
        self.assertTrue(memory_turn.fix_rule(self.ctx, ReplyJob(job_id=2, user_text="以后都这样", chat_id=DM, message_id="om2",
                                                                timeout=0, sender_id=OWNER, topic_id="t1"), lambda: True))

    def test_with_the_switch_the_correction_rule_is_never_carried(self):
        self.assertFalse(memory_turn.fix_rule(self.ctx, ReplyJob(job_id=2, user_text="以后都这样", chat_id=DM, message_id="om2",
                                                                 timeout=0, sender_id=OWNER, topic_id="t1"), lambda: True))

    def test_with_the_switch_and_no_model_for_the_learner_the_correction_rule_comes_back(self):
        job = ReplyJob(job_id=2, user_text="以后都这样", chat_id=DM, message_id="om2", timeout=0, sender_id=OWNER, topic_id="t1")
        for off, expected in ((True, True), (False, False)):
            settings = mock.Mock(assist=mock.Mock(off=off))
            with mock.patch.object(memory_turn.assist, "settings_of", return_value=settings):
                self.assertIs(memory_turn.fix_rule(self.ctx, job, lambda: True), expected)  # 不用模型: no learner reads it.

    def test_a_device_turn_has_no_lookup_line(self):
        with mock.patch("covey.app.memory_turn.device_of", return_value={"alias": "gpu1"}):
            self.assertEqual(memory_turn.search_command(self.ctx, self.job(), True), "")

    def test_the_line_used_last_comes_first_when_a_scope_is_full(self):
        rows = [memory.make(f"principle:p{i}", memory.GLOBAL, memory.OWNER, "字" * 100, at=NOW + i) for i in range(20)]
        newest = {item["ref"] for item in memory.select([dict(row) for row in rows])[0]}
        used = {"principle:p0": NOW + 100}
        found = memory.items(principles=([{"id": f"p{i}", "text": "字" * 100, "at": NOW + i} for i in range(20)], []),
                             used=used)
        kept = {item["ref"] for item in memory.select(found)[0]}
        self.assertIn("principle:p0", kept)
        self.assertNotIn("principle:p0", newest)


if __name__ == "__main__":
    unittest.main()

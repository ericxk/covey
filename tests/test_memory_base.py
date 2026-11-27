"""专项记忆的底座 (docs/projects.md 专项记忆与资料库; core/project_memory.py, core/memory.py, app/project_context.py,
app/project_lead.py, app/web_collect.py): three faults it once had and must not have again, and the file older releases read.

- An answered question card was one line, each question cut to 70 characters and each answer to 50, the whole cut to 200: the
  conditions in a question were lost, the last questions' answers too, and nothing led back to the question. Now each question
  answered is an entry of its own, whole, that names its question record and question, and the record is kept whole beside it.
- A member topic's turn carried 2000 characters of the project's memory, the owner's words first and the newest first: a few new
  answers pushed out what the lead had noted for good. Now 长期约定, 当前决定 and 待核对参考 are chosen each in its own room, and
  a full file lets answers go first, into memory-archive.json, not into nothing.
- The lead's pack read the last 20 lines as "[kind] text", and a resumed lead turn read none: a line deleted in between was
  still the lead's to follow. Now the lead's pack (every turn), a member topic's turn and the web page take one read
  (core/memory.py project_view), with the same version, and a resumed turn of either is told what was voided since its previous
  turn."""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF
from support.records import state_dir, write_job, write_topic

from covey.app import memory_turn, project_context, project_lead, project_tools
from covey.app.prompts import PROJECT_PART_HEADS, VOIDED_HEAD
from covey.app.web_collect import Collector
from covey.core import butler
from covey.core import memory as core_memory
from covey.core import memstore, project_memory, projects
from covey.core.board_read import previous_start

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
LEAD = projects.lead_topic_id(CHAT, PID)
MEMBER = "ab" * 16
TOKEN = "0123456789abcdef"
NOW = 1_800_000_000.0
ON = {projects.ENV: "1", projects.MEMORY_ENV: "1"}

# A question card of three questions whose conditions are in the questions and the options, long enough that the old line lost
# them, the last question's answer with them.
FIRST = ("第一批迁哪些表？前提：只在周六的发布窗口里迁，迁之前先停掉旧库的写入，回滚脚本要先在预发环境演练一遍；"
         "账单表约 4000 万行，流水表约 1.2 亿行，两张一起迁要多占一个窗口。")
SECOND = "迁完以后用哪几项核对？多选；核对不过就整批回滚，不做局部修补。"
THIRD = ("最后一题：旧库保留多久？到期前要再问一次主人；保留期间只读，不再接新写入，账单服务的配置里旧库地址留作备用。"
         "保留到期后由谁下线、下线前要不要导出一份冷备份？")
QUESTIONS = [
    {"id": "scope", "header": "迁移范围", "question": FIRST, "context": "上周演练时流水表迁了 7 小时",
     "options": [{"label": "只迁账单表", "description": "一个窗口内做完"}, {"label": "账单和流水一起", "description": "要两个窗口"}]},
    {"id": "check", "header": "核对", "question": SECOND, "multiple": True,
     "options": [{"label": "对行数", "description": ""}, {"label": "对金额合计", "description": "按天汇总"}, {"label": "抽样比对", "description": ""}]},
    {"id": "keep", "header": "旧库", "question": THIRD,
     "options": [{"label": "保留 30 天", "description": ""}, {"label": "保留 90 天", "description": "到期前再问"}]},
]
ANSWERS = {"scope": {"answers": ["账单和流水一起"]}, "check": {"answers": ["对行数", "对金额合计"]},
           "keep": {"answers": ["保留 90 天，到期由我来下线，下线前导出冷备份"]}}


def question_record(token=TOKEN, questions=QUESTIONS, answers=ANSWERS, **fields):
    return {"token": token, "topic_id": MEMBER, "chat_id": CHAT, "job_id": 12, "job_key": "om_q12", "questions": questions,
            "answers": answers, "supplement": "周六前把核对结果给我", "status": "submitting", **fields}


def legacy_answer(state, topic, record):
    """An answer as releases before the decision log noted it (core/project_memory.py note_answers, a line of memory.json each):
    the rows the readers of the project's memory read from older releases."""
    found = projects.topic_project(state, topic)
    lines = [{key: line[key] for key in ("index", "item", "text")} for line in project_context.answered(record)]
    if project_memory.TOKEN.fullmatch(str(record.get("token") or "")):
        project_memory.note_answers(state, found.id, {**record, "topic_id": record.get("topic_id") or topic}, lines)
        return
    for line in lines:
        project_memory.add(state, found.id, line["text"], "decision", "owner_answer",
                           origin={"item": line["item"], "index": line["index"], "topic": topic})


def environment(test, **extra):
    env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
    patch = mock.patch.dict(os.environ, {**env, **UNSET_OFF, **extra}, clear=True)
    patch.start()
    test.addCleanup(patch.stop)


class Case(unittest.TestCase):
    """A state directory with a project, its lead topic and one member topic; the switches on."""

    switches = ON

    def setUp(self):
        self.state = state_dir(self)
        environment(self, **self.switches)
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="账单迁移", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, project=projects.project_field(PID, projects.LEAD))
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))

    def record(self):
        return projects.read_record(self.state, PID)

    def ctx(self, topic=MEMBER):
        return SimpleNamespace(state_dir=self.state, topic_id=topic, workspace_name="ws",
                               history_path=self.state / "topics" / topic / "history.jsonl")

    def job(self, topic=MEMBER, job_id=0):
        return SimpleNamespace(topic_id=topic, chat_id=CHAT, job_id=job_id, message_id=f"om{job_id}")

    def live(self):
        return project_memory.live(self.state, self.record())

    def stored(self):
        return json.loads(project_memory.memory_path(self.state, PID).read_text())


class AnswerTest(Case):
    """① An answered question card: a whole decision for each question (core/memstore.py decisions.jsonl, not a line of
    memory.json any more), the record kept, nothing noted twice or brought back."""

    def decisions(self):
        return memstore.decisions(self.state, "project", PID, statuses=None)

    def test_each_question_is_a_decision_of_its_own_whole_with_where_it_came_from(self):
        project_context.remember_answer(self.state, MEMBER, question_record(), owners=True)
        rows = self.decisions()
        self.assertEqual([row.origin["question"] for row in rows], [f"{TOKEN}-1", f"{TOKEN}-2", f"{TOKEN}-3"])
        self.assertEqual({(row.kind, row.said, row.by, row.status) for row in rows}, {("decision", "owner", "owner", "active")})
        self.assertEqual([row.text for row in rows], [f"主人答：{FIRST}→账单和流水一起", f"主人答：{SECOND}→对行数；对金额合计",
                                                      f"主人答：{THIRD}→保留 90 天，到期由我来下线，下线前导出冷备份"])
        self.assertTrue(all("…" not in row.text for row in rows))  # Nothing cut: the conditions and the last answer are there.
        self.assertEqual([{key: row.origin[key] for key in ("item", "index", "topic_key", "job")} for row in rows], [
            {"item": item, "index": index, "topic_key": MEMBER, "job": "om_q12"} for index, item in enumerate(("scope", "check", "keep"), 1)])
        self.assertTrue(all(memstore.is_code(row.code) and row.code.startswith("D") for row in rows))
        self.assertFalse(project_memory.memory_path(self.state, PID).exists())  # Not a line of the 120 any more.

    def test_the_question_record_is_kept_whole_beside_the_memory(self):
        project_context.remember_answer(self.state, MEMBER, question_record(), owners=True)
        kept = project_memory.answer_record(self.state, PID, TOKEN)
        self.assertEqual((kept["questions"], kept["answers"], kept["supplement"], kept["job_key"]),
                         (QUESTIONS, ANSWERS, "周六前把核对结果给我", "om_q12"))  # Options, their descriptions and the context too.
        self.assertNotIn("chat_id", kept)
        row = self.decisions()[-1]
        token, _, index = row.origin["question"].partition("-")
        source = project_memory.answer_record(self.state, PID, token)["questions"][int(index) - 1]
        self.assertEqual(source["id"], row.origin["item"])  # From the decision back to its question.
        self.assertEqual(project_memory.answer_record(self.state, PID, "f" * 16), {})

    def test_the_same_answer_is_noted_once_and_one_the_owner_voided_is_not_noted_again(self):
        project_context.remember_answer(self.state, MEMBER, question_record(), owners=True)
        memstore.void(self.state, self.decisions()[1].ref, who="owner")
        project_context.remember_answer(self.state, MEMBER, question_record(), owners=True)  # Submitted again after it was reconciled.
        self.assertEqual([row.status for row in self.decisions()], ["active", "void", "active"])

    def test_a_question_too_long_for_a_line_is_cut_there_and_whole_in_the_record(self):
        long = "条件" * 350
        project_context.remember_answer(self.state, MEMBER, question_record(questions=[{"id": "q", "question": long}],
                                                                            answers={"q": {"answers": ["照第二种做"]}}), owners=True)
        (row,) = self.decisions()
        self.assertEqual(len(row.text), memstore.DECISION_CHARS)
        self.assertTrue(row.text.endswith("…→照第二种做"))  # The answer whole, the question cut before it.
        self.assertEqual(project_memory.answer_record(self.state, PID, TOKEN)["questions"][0]["question"], long)

    def test_skipped_questions_and_feedback_note_nothing_and_a_record_without_a_token_notes_each_question(self):
        project_context.remember_answer(self.state, MEMBER, question_record(answers={
            "scope": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}, "check": {"answers": []}, "keep": {"answers": ["保留 30 天"]}}), owners=True)
        self.assertEqual([row.origin["question"] for row in self.decisions()], [f"{TOKEN}-3"])
        project_context.remember_answer(self.state, MEMBER, {**question_record("1" * 16), "response_kind": "feedback"}, owners=True)
        bare = question_record()
        del bare["token"]
        project_context.remember_answer(self.state, MEMBER, bare, owners=True)
        loose = [row for row in self.decisions() if not str(row.origin.get("question") or "").startswith(TOKEN)]
        self.assertEqual([(row.origin["item"], row.origin["index"]) for row in loose], [("scope", 1), ("check", 2), ("keep", 3)])
        self.assertFalse(any(row.origin.get("question") for row in loose))

    def test_every_reader_of_the_project_memory_has_the_decisions(self):
        project_context.remember_answer(self.state, MEMBER, question_record(
            questions=[{"id": "q", "question": "第一批迁哪些表？"}], answers={"q": {"answers": ["账单和流水一起"]}}), owners=True)
        (entry,) = self.decisions()
        pack = project_lead.state_pack(self.state, self.record(), "lead-1", full=True, receipts=[])
        self.assertIn(f"〔{entry.code}〕主人答：第一批迁哪些表？→账单和流水一起", pack)
        view = project_context.memory_view(self.ctx(), self.job(job_id=2))
        self.assertIn("主人答：第一批迁哪些表？→账单和流水一起", [item["text"] for item in view["items"]])
        memstore.settle(self.state, entry.ref, who="owner")
        view = project_context.memory_view(self.ctx(), self.job(job_id=3))
        self.assertNotIn("主人答：第一批迁哪些表？→账单和流水一起", [item["text"] for item in view["kept"]])  # Done: history.


class CompatTest(Case):
    """The file as older releases read and write it: their keys and types, an origin their rewrite dropped read back from the id,
    a merged line of theirs read as it was."""

    def test_every_entry_has_the_keys_and_types_an_older_release_reads_and_nothing_else(self):
        project_memory.add(self.state, PID, "窗口在周六", "constraint", "lead", now=NOW, origin={"job": "lead-1"})
        legacy_answer(self.state, MEMBER, question_record())
        data = self.stored()
        self.assertEqual(data["schema_version"], 1)
        for row in data["entries"]:
            self.assertLessEqual(set(row), {"id", "text", "kind", "source", "at", "deleted", "redacted"})  # The rest is in the meta.
            self.assertIsInstance(row["id"], str)
            self.assertIsInstance(row["text"], str)
            self.assertIn(row["kind"], project_memory.KINDS)
            self.assertLessEqual(len(row["source"]), 30)
            self.assertIsInstance(row["at"], float)
            self.assertEqual(row["deleted"], 0)

    def test_an_older_releases_rewrite_keeps_the_meta_and_an_answers_origin_is_read_from_its_id_without_one(self):
        legacy_answer(self.state, MEMBER, question_record())
        data = self.stored()
        old = [{key: row[key] for key in ("id", "text", "kind", "source", "at", "deleted")} | {"text": row["text"][:200]}
               for row in data["entries"]]  # What an older release writes: its keys, its 200 characters.
        project_memory.memory_path(self.state, PID).write_text(json.dumps({"schema_version": 1, "entries": old}, ensure_ascii=False))
        self.assertEqual([row["origin"]["item"] for row in self.live()], ["scope", "check", "keep"])  # The meta is untouched.
        project_memory.meta_path(self.state, PID).unlink()
        self.assertEqual([row["origin"] for row in self.live()], [{"question": TOKEN, "index": n} for n in (1, 2, 3)])
        self.assertTrue(project_memory.answer_record(self.state, PID, TOKEN)["questions"])  # The record is still whole.
        self.assertEqual({core_memory.describe(row)["level"] for row in self.live()}, {"owner"})

    def test_a_merged_answer_line_from_before_is_read_as_it_was_a_decision_of_the_owner(self):
        line = {"id": "a1b2c3", "text": "主人答：按日期还是按租户分区？→按日期；先迁哪年？→2023", "kind": "decision",
                "source": "owner_answer", "at": NOW, "deleted": 0}
        project_memory.memory_path(self.state, PID).parent.mkdir(parents=True, exist_ok=True)
        project_memory.memory_path(self.state, PID).write_text(json.dumps({"schema_version": 1, "entries": [line]}, ensure_ascii=False))
        (row,) = self.live()
        self.assertEqual((row["text"], "origin" in row), (line["text"], False))
        (item,) = core_memory.items(project=[row], project_id=PID)
        # The owner's words, a 当前决定; with no origin to show, the agent's level (core/turn_source.py provenance), said so.
        self.assertEqual((item["by"], item["asserted_by"], item["recorded_by"], item["layer"], item["part"], item["level"]),
                         (core_memory.OWNER, "owner", "bridge", "decision", "decision", "agent"))
        environment(self, **ON, **{core_memory.ENV: "1"})
        job = self.job(job_id=2)
        self.assertNotIn(line["text"], memory_turn.block(self.ctx(), job, owner_turn=lambda: True))  # A decision: the tail's.
        self.assertIn(line["text"], memory_turn.tail(self.ctx(), job, lambda: True))


# The sizes of a project's 13 lines when the old choice was measured (1956 of 2000 characters), and a 200-character answer after
# them: who wrote each and how long it was, the oldest first. The words are made up.
THIRTEEN = (("owner_answer", 85), ("owner_answer", 183), ("owner_answer", 200), ("owner_answer", 42), ("lead", 114), ("lead", 44),
            ("owner_answer", 191), ("owner_answer", 200), ("owner_answer", 163), ("lead", 100), ("lead", 153), ("lead", 60),
            ("lead", 109))


def made_up(number, size):
    return (f"第{number:02d}条：" + "核对口径和分工的约定" * 30)[:size]


class BudgetTest(Case):
    """② What holds for good is not pushed out by new answers, in what a turn carries and in what the file keeps."""

    def thirteen_and_one(self):
        for number, (source, size) in enumerate(THIRTEEN):
            project_memory.add(self.state, PID, made_up(number, size), "constraint" if source == "lead" else "decision", source,
                               now=NOW + number)
        old = sum(len(row["text"]) + 24 for row in self.live())
        self.assertGreater(old, 1900)  # Near the old 2000.
        project_memory.add(self.state, PID, made_up(13, 200), "decision", "owner_answer", now=NOW + 13)

    def test_a_new_answer_does_not_push_out_the_leads_lines_a_turn_carries(self):
        self.thirteen_and_one()
        lead = [row["text"] for row in self.live() if row["source"] == "lead"]
        environment(self, **ON, **{core_memory.ENV: "1"})
        job = self.job(job_id=5)
        block = memory_turn.block(self.ctx(), job, owner_turn=lambda: True)
        tail = memory_turn.tail(self.ctx(), job, lambda: True)
        for text in lead:  # The lead's lines are notes, carried when a turn's words find them: no answer takes their room.
            self.assertNotIn(text, block)
        self.assertNotIn(made_up(13, 200), block)  # Answers are never in the rules.
        self.assertIn(made_up(13, 200), tail)  # The newest answer is carried at the tail, the newest first.
        view = core_memory.project_view(project_memory.entries(self.state, self.record()), PID)
        self.assertEqual(view["parts"][core_memory.REFERENCE]["left"], [])

    def test_with_the_room_of_the_rules_used_new_answers_go_without_and_say_so_and_the_leads_lines_stay(self):
        for number in range(6):  # The owner's rules fill their own part: no room is lent.
            project_memory.add(self.state, PID, made_up(40 + number, 135), "constraint", "owner", now=NOW - 100 + number,
                               origin={"message_id": f"om_{number}"})
        self.thirteen_and_one()
        environment(self, **ON, **{core_memory.ENV: "1"})
        job = self.job(job_id=5)
        block = memory_turn.block(self.ctx(), job, owner_turn=lambda: True)
        tail = memory_turn.tail(self.ctx(), job, lambda: True)
        for number in range(6):  # The owner's rules: all of them, whatever the answers.
            self.assertIn(made_up(40 + number, 135), block)
        self.assertNotIn("放不下", block)
        self.assertIn(made_up(13, 200), tail)
        listed = tail.split("相关但没带的：", 1)[1]
        self.assertIn("第00条", listed)  # The older answers past the decisions' room: their titles, still in the memory.
        view = core_memory.project_view(project_memory.entries(self.state, self.record()), PID)
        self.assertEqual((len(view["parts"][core_memory.RULE]["kept"]), view["parts"][core_memory.REFERENCE]["left"]), (6, []))

    def test_with_the_memory_switch_off_a_member_topic_reads_the_same_three_layers(self):
        self.thirteen_and_one()
        block = project_context.block(self.ctx(), self.job(job_id=5), resumed=True)
        for text in [row["text"] for row in self.live() if row["source"] == "lead"]:
            self.assertIn(f"- [constraint] {text}\n", block)
        heads = [block.index(PROJECT_PART_HEADS[layer]) for layer in (core_memory.DECISION, core_memory.REFERENCE)]
        self.assertEqual(heads, sorted(heads))
        self.assertIn(made_up(13, 200), block)

    def test_more_answers_never_take_the_room_of_the_owners_rules(self):
        for number in range(6):
            project_memory.add(self.state, PID, made_up(number, 120), "constraint", "owner", now=NOW + number)
        for number in range(6, 30):
            project_memory.add(self.state, PID, made_up(number, 190), "decision", "owner_answer", now=NOW + number)
        view = core_memory.project_view(project_memory.entries(self.state, self.record()), PID)
        self.assertEqual(len(view["parts"][core_memory.RULE]["kept"]), 6)
        self.assertTrue(view["parts"][core_memory.DECISION]["left"])

    def test_a_full_memory_lets_answers_go_first_and_keeps_what_went(self):
        earlier = time.time() - 100  # Before the answers, which are noted at the time they come.
        project_memory.add(self.state, PID, "上线前要有回滚演练", "constraint", "owner", now=earlier)
        project_memory.add(self.state, PID, "对账一致才算完", "constraint", "lead", now=earlier + 1)
        project_memory.add(self.state, PID, "新库在二号机房", "fact", "lead", now=earlier + 2)
        with mock.patch.object(project_memory, "MAX_ENTRIES", 5):
            for number in range(4):
                token = f"{number:x}" * 16
                legacy_answer(self.state, MEMBER, question_record(  # As an older release noted it: a line of the 120.
                    token, questions=[{"id": "q", "question": f"第 {number} 批开跑吗？"}], answers={"q": {"answers": ["开跑"]}}))
        texts = [row["text"] for row in self.live()]
        self.assertEqual(texts[:3], ["上线前要有回滚演练", "对账一致才算完", "新库在二号机房"])
        self.assertEqual(texts[3:], ["主人答：第 2 批开跑吗？→开跑", "主人答：第 3 批开跑吗？→开跑"])
        gone = project_memory.archived(self.state, PID)
        self.assertEqual([row["text"] for row in gone], ["主人答：第 0 批开跑吗？→开跑", "主人答：第 1 批开跑吗？→开跑"])
        self.assertTrue(all(row["archived"] for row in gone))
        self.assertTrue(project_memory.answer_record(self.state, PID, "0" * 16))  # Its question record stays.

    def test_past_the_answers_the_leads_facts_go_then_its_rules_and_the_owners_words_last(self):
        project_memory.add(self.state, PID, "主人：发布只在周六", "constraint", "owner", now=NOW)
        project_memory.add(self.state, PID, "负责人：对账一致才算完", "constraint", "lead", now=NOW + 1)
        project_memory.add(self.state, PID, "负责人：新库在二号机房", "fact", "lead", now=NOW + 2)
        with mock.patch.object(project_memory, "MAX_ENTRIES", 3):
            project_memory.add(self.state, PID, "负责人：迁移分两批", "decision", "lead", now=NOW + 3)
            self.assertNotIn("负责人：新库在二号机房", [row["text"] for row in self.live()])
            project_memory.add(self.state, PID, "负责人：先迁账单", "decision", "lead", now=NOW + 4)
            project_memory.add(self.state, PID, "负责人：再迁流水", "decision", "lead", now=NOW + 5)
        self.assertEqual([row["text"] for row in self.live()], ["主人：发布只在周六", "负责人：先迁账单", "负责人：再迁流水"])
        self.assertEqual(len(project_memory.archived(self.state, PID)), 3)

    def test_an_archive_that_cannot_be_read_leaves_the_memory_as_it_was(self):
        project_memory.add(self.state, PID, "最早的一条", "fact", "lead", now=NOW)
        project_memory.archive_path(self.state, PID).mkdir()  # Not a file: reading it fails.
        before = self.stored()
        with mock.patch.object(project_memory, "MAX_ENTRIES", 1), self.assertRaises(OSError):
            project_memory.add(self.state, PID, "挤掉它的一条", "fact", "lead", now=NOW + 1)
        self.assertEqual(self.stored(), before)


class ReadCase(Case):
    """A rule the owner said in a member topic's turn, a note of the lead's and an answer; a turn of the member topic and of the
    lead before them."""

    def setUp(self):
        super().setUp()
        environment(self, **ON, **{core_memory.ENV: "1"})
        self.rule, _ = project_memory.add(self.state, PID, "发布只在周六", "constraint", "owner", now=NOW,
                                          origin={"message_id": "om_rule", "topic": MEMBER, "quote": "以后发布只在周六"})
        self.note, _ = project_memory.add(self.state, PID, "对账一致才算完", "constraint", "lead", now=NOW + 1)
        legacy_answer(self.state, MEMBER, question_record(  # An older release's answer line: what the page reads too.
            questions=[{"id": "q", "question": "第一批迁哪些表？"}], answers={"q": {"answers": ["账单和流水一起"]}}))
        write_job(self.state, "om1", topic_id=MEMBER, chat_id=CHAT, job_id=1, status="done", timings={"backend_started": NOW + 10})
        write_job(self.state, "om3", topic_id=LEAD, chat_id=CHAT, job_id=3, status="done", timings={"backend_started": NOW + 10})

    def member(self):
        memory_turn._plans.clear()
        return memory_turn.block(self.ctx(), self.job(job_id=2), owner_turn=lambda: True)

    def member_tail(self, words=""):
        memory_turn._plans.clear()
        job = self.job(job_id=2)
        job.user_text = words
        return memory_turn.tail(self.ctx(), job, lambda: True)

    def lead(self, full):
        pack = project_lead.state_pack(self.state, self.record(), "lead-4", full=full, receipts=[],
                                       since=None if full else previous_start(self.state, LEAD, 4))
        return re.sub(r"〔[0-9A-Za-z-]+〕", "", pack)  # The lead's lines start with their short codes (project_memory_revise takes them).

    def web(self):
        (item,) = Collector(SimpleNamespace(state_dir=self.state)).project_items(self.state, CHAT)
        return item["data"]


class OneReadTest(ReadCase):
    """③ The lead (a new session and a resumed one), a member topic and the web page read the same memory, version and
    speakers, and a line voided between two turns is said to be voided at the next turn of both."""

    def test_the_lead_a_member_and_the_page_read_one_version_with_the_same_speakers(self):
        version = project_memory.version(project_memory.entries(self.state, self.record()))
        member, fresh, resumed, page = self.member(), self.lead(True), self.lead(False), self.web()
        for text in (fresh, resumed):
            self.assertIn(f"专项记忆版本 {version}。", text)
        self.assertEqual(page["memory_version"], version)
        self.assertRegex(member, r"- R[0-9A-Z]{4,6} 发布只在周六\n")  # The member's rules carry the rules alone.
        tail = self.member_tail("对账一致怎么算完")
        self.assertRegex(tail, r"- D[0-9A-Z]{4,6} 主人答：第一批迁哪些表？→账单和流水一起（T\d+ · \d\d-\d\d）")
        self.assertRegex(tail, r"- N[0-9A-Z]{4,6} 对账一致才算完（负责人记的）")
        for pack in (fresh, resumed):
            for layer, text in ((core_memory.RULE, "- [constraint] 发布只在周六"), (core_memory.DECISION, "- [decision] 主人答：第一批迁哪些表？"),
                                (core_memory.REFERENCE, "- [constraint] 对账一致才算完")):
                self.assertLess(pack.index(PROJECT_PART_HEADS[layer]), pack.index(text))
        rows = {row["text"]: row for row in page["memory"]}
        self.assertEqual({text: tuple(row[key] for key in ("asserted_by", "recorded_by", "level", "layer", "part", "valid", "carried",
                                                          "said_by")) for text, row in rows.items()}, {
            "发布只在周六": ("owner", "bridge", "owner", "rule", "rule", "active", True, "你说的"),
            "对账一致才算完": ("lead", "lead", "agent", "rule", "reference", "active", True, "它总结的"),
            "主人答：第一批迁哪些表？→账单和流水一起": ("owner", "bridge", "owner", "decision", "decision", "active", True, "你说的")})
        answer = rows["主人答：第一批迁哪些表？→账单和流水一起"]
        self.assertEqual((answer["ref"], answer["applies"], answer["superseded_by"]), (f"project:{PID}:{TOKEN}-1", "", ""))
        self.assertEqual({key: answer["origin"][key] for key in ("question", "item", "index", "topic", "job")},
                         {"question": TOKEN, "item": "q", "index": 1, "topic": MEMBER, "job": "om_q12"})
        self.assertEqual(rows["对账一致才算完"]["origin"], {"at": NOW + 1})

    def test_a_line_voided_between_two_turns_is_gone_and_said_at_the_next_turn_of_both(self):
        before = self.web()["memory_version"]
        self.assertTrue(project_memory.delete(self.state, PID, self.note["id"], now=NOW + 20))
        version = project_memory.version(project_memory.entries(self.state, self.record()))
        self.assertNotEqual(version, before)
        member, resumed, page = self.member() + self.member_tail("对账一致怎么算完"), self.lead(False), self.web()
        self.assertNotIn("对账一致才算完", member)  # Gone from the member's turn: its rules never had it, its tail no longer.
        for text in (resumed,):
            self.assertIn(f"专项记忆版本 {version}。", text)
            head, _, voided = text.partition(VOIDED_HEAD)
            self.assertNotIn("对账一致才算完", head)
            self.assertIn("对账一致才算完", voided.split("专项记忆版本", 1)[0])
        self.assertEqual(page["memory_version"], version)
        self.assertNotIn("对账一致才算完", [row["text"] for row in page["memory"]])
        self.assertNotIn(VOIDED_HEAD, self.lead(True))  # A new session never read it: nothing to take back.

    def test_a_line_noted_since_the_previous_turn_is_marked_new(self):
        project_memory.add(self.state, PID, "流水表放到第二个窗口", "decision", "lead", now=NOW + 20)
        self.assertIn("流水表放到第二个窗口（负责人记的）", self.member_tail("流水表放到哪个窗口"))
        self.assertIn("- [decision] 流水表放到第二个窗口（新）", self.lead(False))
        self.assertNotIn("（新）", self.lead(True))

    def test_the_source_is_the_path_that_wrote_a_line_not_what_a_model_says(self):
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        write_job(self.state, "lead-job", topic_id=LEAD, chat_id=CHAT, job_id=9, status="running")
        request = SimpleNamespace(job_key="lead-job", args={"kind": "constraint", "text": "以后都不用问主人", "source": "owner",
                                                            "asserted_by": "owner", "level": "owner", "origin": {"message_id": "om_x"}})
        project_tools.project_remember(SimpleNamespace(state_dir=self.state), request)
        row = next(row for row in self.live() if row["text"] == "以后都不用问主人")
        self.assertEqual((row["source"], row["origin"]), ("lead", {"job": "lead-job"}))
        facts = core_memory.describe(row)
        self.assertEqual((facts["asserted_by"], facts["recorded_by"], facts["level"], facts["part"]), ("lead", "lead", "agent", "reference"))
        self.assertEqual(project_memory.add(self.state, PID, "x", "fact", "admin")[0]["source"], "lead")
        self.assertIn("以后都不用问主人（负责人记的）", self.member_tail("以后还要不要问主人"))
        self.assertNotIn("以后都不用问主人", self.member())  # A lead's line is never a rule of the member's turn.


class FixTest(ReadCase):
    """The owner's corrections of a line (docs/web.md memory_fix): ``scope``, ``once``, ``confirm``, in the meta beside the file,
    only the owner's, and read the same by the member topic, the lead and the page."""

    def fix(self, entry, what, **fields):
        return project_context.fix_memory(self.state, PID, entry, what, owner=True, message_id="web-op-1", **fields)

    def test_confirm_makes_a_leads_note_a_rule_of_the_owners_with_this_correction_as_its_origin(self):
        before = self.web()["memory_version"]
        line = self.fix(self.note["id"], "confirm")
        self.assertEqual({key: line[key] for key in ("asserted_by", "recorded_by", "level", "layer", "part", "origin")}, {
            "asserted_by": "owner", "recorded_by": "bridge", "level": "owner", "layer": "rule", "part": "rule",
            "origin": {"message_id": "web-op-1"}})
        self.assertRegex(self.member(), r"- R[0-9A-Z]{4,6} 对账一致才算完\n")  # The owner's now: a rule, without 负责人记的.
        self.assertLess(self.lead(False).index(PROJECT_PART_HEADS[core_memory.RULE]), self.lead(False).index("对账一致才算完"))
        page = self.web()
        self.assertNotEqual(page["memory_version"], before)
        row = next(row for row in page["memory"] if row["text"] == "对账一致才算完")
        self.assertEqual((row["said_by"], row["level"], row["source"]), ("你说的", "owner", "lead"))  # The file's source as it was.
        (log,) = next(entry for entry in self.live() if entry["id"] == self.note["id"])["fixes"]
        self.assertEqual((log["what"], log["origin"], log["was"]), ("confirm", {"message_id": "web-op-1"}, {}))

    def test_scope_and_once_set_the_conditions_and_once_makes_a_rule_a_decision_of_that_time(self):
        self.fix(self.note["id"], "scope", applies="只管迁移批次，不管日常对账")
        line = self.fix(self.rule["id"], "once")
        self.assertEqual((line["layer"], line["part"], line["applies"]), ("decision", "decision", project_memory.ONCE))
        self.assertNotIn("发布只在周六", self.member())  # Of that time only: no longer a rule of the member's turns,
        self.assertIn("发布只在周六", self.member_tail())  # a decision in progress at their tail.

    def test_only_the_owner_corrects_with_an_origin_and_a_correction_needs_what_it_says(self):
        with self.assertRaises(ValueError):
            project_context.fix_memory(self.state, PID, self.note["id"], "confirm", owner=False, message_id="web-op-1")
        with self.assertRaises(ValueError):
            project_memory.fix(self.state, PID, self.note["id"], "confirm", origin={})  # No origin: no owner's level.
        with self.assertRaises(ValueError):
            self.fix(self.note["id"], "scope")  # Nothing to scope with.
        with self.assertRaises(ValueError):
            self.fix(self.note["id"], "promote")
        project_memory.delete(self.state, PID, self.note["id"])
        with self.assertRaises(ValueError):
            self.fix(self.note["id"], "confirm")  # A tombstone is not brought back.
        self.assertEqual(core_memory.describe(self.note)["level"], "agent")

    def test_an_older_releases_rewrite_keeps_a_confirmation(self):
        self.fix(self.note["id"], "confirm")
        data = self.stored()
        project_memory.memory_path(self.state, PID).write_text(json.dumps(
            {"schema_version": 1, "entries": [{key: row[key] for key in ("id", "text", "kind", "source", "at", "deleted")}
                                              for row in data["entries"]]}, ensure_ascii=False))
        row = next(row for row in self.live() if row["id"] == self.note["id"])
        self.assertEqual(core_memory.describe(row)["level"], "owner")

    def test_a_line_done_or_superseded_is_history_and_not_carried(self):
        project_memory.meta_path(self.state, PID).write_text(json.dumps({"schema_version": 1, "entries": {
            self.note["id"]: {"valid": "superseded", "superseded_by": self.rule["id"]}}}))
        row = next(row for row in self.web()["memory"] if row["text"] == "对账一致才算完")
        self.assertEqual((row["layer"], row["part"], row["valid"], row["superseded_by"], row["carried"]),
                         ("history", None, "superseded", self.rule["id"], False))
        self.assertNotIn("对账一致才算完", self.member())


if __name__ == "__main__":
    unittest.main()

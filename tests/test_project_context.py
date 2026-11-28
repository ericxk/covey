"""What a project's memory and library do in the Bridge (app/project_context.py, core/project_library.py, COVEY_PROJECT_MEMORY=1):
the block a topic of the project starts with, an answer to a question card noted as a decision, the owner's files and the
topics' artifacts in the library, the lead's pack and the card's rows, the maintenance rules, and that with the switch off
none of it does anything."""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import UNSET_OFF
from support import enter_context, make_context, quiet
from support.fake_backends import FakeBackend
from support.records import state_dir, write_job, write_topic
from support.world import QuestionFixture, harness_app

from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_rules
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.app import project_context, project_facts, project_lead, project_loop
from covey.app.prompts import PROJECT_CHARS, bridge_rules, project_block
from covey.core import backup, memstore, project_library as library, project_memory as memory, projects
from covey.core import memory as core_memory
from covey.core.maintenance import maintain, plan_maintenance
from covey.core.state import DAY, read_object, write_json

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
LEAD = projects.lead_topic_id(CHAT, PID)
MEMBER = "ab" * 16
NOW = 1_800_000_000.0
ON = {projects.ENV: "1", projects.MEMORY_ENV: "1"}


def environment(test, **extra):
    env = {name: value for name, value in os.environ.items() if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}
    enter_context(test, mock.patch.dict(os.environ, {**env, **UNSET_OFF, **extra}, clear=True))


class Case(unittest.TestCase):
    """A state directory with a project, its lead topic and one member topic; the switches on."""

    switches = ON

    def setUp(self):
        self.state = state_dir(self)
        environment(self, **self.switches)
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=LEAD, chat_id=CHAT, project=projects.project_field(PID, projects.LEAD))
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        self.media = self.state / "topics" / LEAD / "incoming_media"
        self.media.mkdir(parents=True)
        self.logs = []

    def ctx(self, topic=MEMBER):
        return SimpleNamespace(state_dir=self.state, topic_id=topic, history_path=self.state / "topics" / topic / "history.jsonl")

    def job(self, topic=MEMBER):
        return SimpleNamespace(topic_id=topic, chat_id=CHAT)

    def record(self):
        return projects.read_record(self.state, PID)

    def attachment(self, name, data=b"x"):
        path = self.media / name
        path.write_bytes(data)
        return path

    def message(self, *paths):
        return "看一下这个\n收到的附件（已下载到本地）：\n" + "\n".join(f"- 文件 {i + 1}（{p.name}）: `{p}`" for i, p in enumerate(paths))


class BlockTest(Case):
    def test_a_member_topic_reads_the_memory_and_a_new_session_the_library_too(self):
        memory.add(self.state, PID, "窗口在周六", "constraint", "lead", now=NOW)
        memory.set_style(self.state, PID, "汇报风格：x", now=NOW)
        library.add_ref(self.state, PID, "path", "/data/out/bill.csv", source="artifact", topic_id=MEMBER, now=NOW)
        fresh = project_context.block(self.ctx(), self.job(), resumed=False)
        self.assertIn("属于专项「迁移账单」", fresh)
        self.assertIn("- [constraint] 窗口在周六", fresh)
        self.assertNotIn("汇报风格", fresh)
        self.assertIn("专项资料库", fresh)
        self.assertIn("/data/out/bill.csv", fresh)
        resumed = project_context.block(self.ctx(), self.job(), resumed=True)
        self.assertIn("窗口在周六", resumed)
        self.assertNotIn("专项资料库", resumed)

    def test_the_lead_an_ordinary_topic_a_cancelled_project_and_an_empty_one_read_nothing(self):
        memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        write_topic(self.state, topic_id="cd" * 16, chat_id=CHAT)
        self.assertEqual(project_context.block(self.ctx(LEAD), self.job(LEAD), resumed=False), "")
        self.assertEqual(project_context.block(self.ctx("cd" * 16), self.job("cd" * 16), resumed=False), "")
        projects.set_state(self.state, PID, projects.CANCELLED, "downgraded")
        self.assertEqual(project_context.block(self.ctx(), self.job(), resumed=False), "")
        other = state_dir(self)
        self.assertEqual(project_context.block(SimpleNamespace(state_dir=other, topic_id=MEMBER, history_path=other / "h"),
                                               self.job(), resumed=False), "")

    def test_a_deleted_entry_is_not_read(self):
        item, _ = memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        memory.delete(self.state, PID, item["id"])
        self.assertEqual(project_context.block(self.ctx(), self.job(), resumed=False), "")

    def test_the_block_is_cut_at_its_limit_oldest_first_and_goes_into_the_rules_after_the_principles(self):
        lines = [f"- [fact] {n:03d}" + "字" * 90 for n in range(60)]
        block = project_block("迁移账单", lines, ["- 方案.md"])
        self.assertLessEqual(len(block.split("\n\n")[0]), PROJECT_CHARS + 200)
        self.assertIn("059", block)
        self.assertNotIn("[fact] 000", block)
        self.assertIn("条较早的记忆未列出", block)
        self.assertTrue(block.endswith("- 方案.md"))
        self.assertEqual(project_block("x"), "")
        rules = bridge_rules(backend_name="Claude Code", workspace_name="w", workdir=Path("/w"), principles="原则块", project="专项块")
        self.assertLess(rules.index("原则块"), rules.index("专项块"))
        self.assertEqual(bridge_rules(backend_name="Claude Code", workspace_name="w", workdir=Path("/w")),
                         bridge_rules(backend_name="Claude Code", workspace_name="w", workdir=Path("/w"), project=""))


class RunsTest(unittest.TestCase):
    """app/runs.py: the block is in a new session's rules and a resumed session's reminder after the principles, and with the
    switch off the rules are word for word what they were."""

    def setUp(self):
        self.root = state_dir(self)
        environment(self, **ON)
        self.ctx = make_context(self.root)
        self.state = self.ctx.state_dir
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        memory.add(self.state, PID, "窗口在周六", "constraint", now=NOW)
        library.add_ref(self.state, PID, "path", "/data/out/bill.csv", now=NOW)
        app = harness_app(CLAUDE_SPEC, FakeBackend(), ClaudeSessionStore())
        quiet(app)
        app.runtime.state_dir = self.state
        self.runs = app.runs
        self.job = ReplyJob(job_id=5, user_text="接着做", chat_id=CHAT, message_id="om5", timeout=0, topic_id=MEMBER)
        self.ctx = dataclasses.replace(self.ctx, topic_id=MEMBER)

    def test_a_new_session_reads_memory_and_library_a_resumed_one_the_memory(self):
        rules = self.runs.bridge_rules(self.ctx, self.job)
        self.assertIn("窗口在周六", rules)
        self.assertIn("/data/out/bill.csv", rules)
        self.assertLess(rules.index("窗口在周六"), rules.index(CHECKPOINT_RULES))
        resumed = self.runs._with_rules(self.ctx, self.job, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertIn("窗口在周六", resumed)
        self.assertNotIn("/data/out/bill.csv", resumed)
        self.assertTrue(resumed.endswith(checkpoint_rules(images=False)))

    def test_with_the_switch_off_the_rules_have_no_project_block(self):
        os.environ[projects.MEMORY_ENV] = "0"
        rules = self.runs.bridge_rules(self.ctx, self.job)
        self.assertNotIn("专项", rules)
        resumed = self.runs._with_rules(self.ctx, self.job, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertNotIn("窗口在周六", resumed)


class SwitchOffTest(Case):
    """COVEY_PROJECTS on, COVEY_PROJECT_MEMORY unset: the memory is one place, nothing else happens."""
    switches = {projects.ENV: "1"}

    def test_nothing_is_put_noted_or_copied(self):
        memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        library.add_ref(self.state, PID, "path", "/data/out/bill.csv", now=NOW)  # On disk from an earlier time.
        self.assertFalse(projects.memory_enabled())
        self.assertEqual(project_context.block(self.ctx(), self.job(), resumed=False), "")
        self.assertEqual(project_lead.library_index(self.state, PID), [])
        record = {"questions": [{"id": "q", "question": "用哪个？"}], "answers": {"q": {"answers": ["新库"]}}}
        project_context.remember_answer(self.state, MEMBER, record, owners=True)
        self.assertEqual([row["source"] for row in memory.live(self.state, self.record())], ["lead"])
        path = self.attachment("方案.md")
        project_context.owner_files(self.state, LEAD, CHAT, self.message(path), self.media)
        self.assertEqual(len(library.entries(self.state, PID)), 1)
        write_job(self.state, "k1", topic_id=MEMBER, chat_id=CHAT, status="done", message_id="m")
        cp = self.state / "cp.json"
        write_json(cp, {"artifacts": ["/data/out/new.csv"]})
        project_context.turn_to_library(self.state, PID, MEMBER, {"status": "done", "checkpoint_path": str(cp)})
        self.assertEqual(len(library.entries(self.state, PID)), 1)

    def test_without_the_projects_switch_either(self):
        environment(self)
        self.assertFalse(projects.memory_enabled())
        os.environ[projects.MEMORY_ENV] = "1"
        self.assertFalse(projects.memory_enabled())


class AnswerTest(Case):
    def decisions(self):
        return memstore.decisions(self.state, "project", PID, statuses=None)

    def test_each_answer_is_a_decision_of_its_own_with_the_owner_as_its_source(self):
        record = {"token": "0123456789abcdef", "questions": [{"id": "a", "question": "按日期还是按租户分区？"}, {"id": "b", "question": "先迁哪年？"},
                                {"id": "c", "question": "要不要停机？"}],
                  "answers": {"a": {"answers": ["按日期"]}, "b": {"answers": ["2023", "2024"]}, "c": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}}}
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        rows = self.decisions()
        self.assertEqual([(row.kind, row.said, row.by) for row in rows], [("decision", "owner", "owner")] * 2)
        self.assertEqual([row.text for row in rows], ["主人答：按日期还是按租户分区？→按日期", "主人答：先迁哪年？→2023；2024"])
        project_context.remember_answer(self.state, MEMBER, record, owners=True)  # The same answer is not noted twice.
        self.assertEqual(len(self.decisions()), 2)
        self.assertEqual(memory.live(self.state, self.record()), [])  # decisions.jsonl, not the memory's 120 lines.

    def test_the_lead_counts_feedback_and_a_plain_topic_do_not(self):
        record = {"questions": [{"id": "a", "question": "用哪个？"}], "answers": {"a": {"answers": ["新库"]}}}
        project_context.remember_answer(self.state, LEAD, record, owners=True)
        self.assertEqual(len(self.decisions()), 1)
        project_context.remember_answer(self.state, MEMBER, {**record, "response_kind": "feedback", "feedback": "不对"}, owners=True)
        project_context.remember_answer(self.state, "cd" * 16, record, owners=True)  # No such topic: nothing written for it.
        project_context.remember_answer(self.state, MEMBER, {"questions": [{"id": "a", "question": "q"}], "answers": {"a": {"answers": []}}}, owners=True)
        self.assertEqual(len(self.decisions()), 1)
        self.assertFalse((self.state / "topics" / ("cd" * 16)).exists())
        project_context.remember_answer(self.state, MEMBER, {"answers": "broken"}, self.logs.append, owners=True)
        self.assertEqual(self.logs, [])  # Nothing to note, nothing raised.

    def test_a_record_that_is_gone_is_logged_not_raised(self):
        record = {"questions": [{"id": "a", "question": "用哪个？"}], "answers": {"a": {"answers": ["新库"]}}}
        os.unlink(projects.record_path(self.state, PID))
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        self.assertEqual(len(self.logs), 1)

    def test_a_question_about_another_projects_memory_is_history_from_the_start(self):
        # A topic of this project asked, on one card, how each line of another project's memory counts (a migration); the
        # answers became this project's 当前决定 and its turns carried them.
        other = "f6e5d4c3b2a1"
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="回放", project_id=other, now=NOW))
        theirs = memory.add(self.state, other, "每批回放都要带上旧版一起跑", "decision", "lead")[0]["id"]
        mine = memory.add(self.state, PID, "账单表按日期分区", "decision", "lead")[0]["id"]
        token = "0123456789abcdef"
        questions = [{"id": f"memory-{theirs}", "header": f"记忆 {theirs}",
                      "question": f"回放专项的记忆〔{theirs}〕「每批回放都要带上旧版一起跑」这条以后怎么算？"},
                     {"id": "pilot", "question": f"第一次真实试点选哪个？（回放专项 {other}）"},
                     {"id": "own", "question": f"这个专项的记忆〔{mine}〕以后怎么算？"}]
        record = {"token": token, "questions": questions,
                  "answers": {question["id"]: {"answers": ["长期都照这条做"]} for question in questions}}
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        status = {row.origin["question"]: row.status for row in self.decisions()}
        self.assertEqual(status, {f"{token}-1": "done", f"{token}-2": "active", f"{token}-3": "active"})  # Naming a project is not it.
        self.assertEqual(self.logs, [f"project memory answer 1 noted as history: it names project {other}'s lines"])
        rows = memory.entries(self.state, self.record())
        carried = core_memory.project_view(rows + project_context.decision_rows(self.state, PID, rows), PID)["kept"]
        self.assertEqual(len([item for item in carried if item["text"].startswith("主人答：")]), 2)

    def test_only_a_word_like_an_id_reads_the_other_projects_and_a_failed_read_notes_as_before(self):
        plain = {"token": "0123456789abcdef", "questions": [{"id": "a", "question": "按日期还是按租户分区？"}],
                 "answers": {"a": {"answers": ["按日期"]}}}
        with mock.patch.object(memory, "across", side_effect=AssertionError("read")):
            project_context.remember_answer(self.state, MEMBER, plain, self.logs.append, owners=True)
        with mock.patch.object(memory, "across", side_effect=OSError("nas")):
            project_context.remember_answer(self.state, MEMBER, {**plain, "token": "fedcba9876543210", "questions": [
                {"id": "a", "question": "〔abc123〕这条还要吗？"}]}, self.logs.append, owners=True)
        self.assertEqual([row.status for row in self.decisions()], ["active"] * 2)
        self.assertEqual(self.logs, ["project memory answers not read against the other projects: OSError"])


class RealAnswerTest(Case):
    """T908 4.2: only a real answer is a decision, by rules alone (no model). Three live ones were not: a remark typed in the
    topic while a card waited ("这个你能用CUA方式来弄么"), and two that asked back or held on."""

    OPTIONS = {"id": "pick", "question": "自动学习要不要对所有部署默认打开？",
               "options": [{"label": "对所有部署默认打开（推荐）"}, {"label": "保持默认关"}]}
    FREE = {"id": "free", "question": "93 条迁移来的负责人约束怎么处理？"}

    def note(self, question, said, channel, token):
        record = {"token": token, "questions": [question], "answers": {question["id"]: {"answers": [said]}},
                  "answer_by": {"party": "owner", "channel": channel}}
        before = len(memstore.decisions(self.state, "project", PID, statuses=None))
        project_context.remember_answer(self.state, MEMBER, record, self.logs.append, owners=True)
        return len(memstore.decisions(self.state, "project", PID, statuses=None)) > before

    def test_a_pick_and_an_options_words_typed_are_answers_other_words_typed_in_the_topic_are_not(self):
        cases = [(self.OPTIONS, "对所有部署默认打开（推荐）", "card", True),
                 (self.OPTIONS, " 对所有部署默认打开 。", "message", True),  # Its words, the 推荐 mark and the marks aside.
                 (self.OPTIONS, "保持默认关", "message", True),
                 (self.OPTIONS, "这个你能用CUA方式来弄么", "message", False),
                 (self.OPTIONS, "我觉得都行，你看着办", "message", False),  # Typed in the topic, none of the options.
                 (self.OPTIONS, "两个都先保持现状", "card", True),  # Written in the card's own box: his answer.
                 (self.OPTIONS, "这两个有什么区别？", "card", False)]
        for n, (question, said, channel, noted) in enumerate(cases):
            self.assertEqual(self.note(question, said, channel, f"{n:016x}"), noted, said)
        self.assertIn("question answers not noted as decisions: 1 of 1 not an answer", self.logs)
        self.assertFalse([line for line in self.logs if "CUA" in line])  # Never his words in a log line.

    def test_a_free_question_answered_by_asking_back_or_holding_on_is_not_an_answer(self):
        cases = [("问几个问题，这个包级满意率是啥意思？", False), ("这个你能用CUA方式来弄么", False),
                 ("等等你不能用无头浏览器登录，帮我建么", False), ("先别动，我再看看", False), ("这样可以吗 。", False),
                 ("你先说说有几种做法呢", False), ("Is it safe?", False),
                 ("先都不动，我自己逐个看", True), ("照 11-27 的选择做", True), ("要", True)]
        for n, (said, noted) in enumerate(cases):
            for m, channel in enumerate(("message", "card")):
                self.assertEqual(self.note(self.FREE, said, channel, f"{n:08x}{m:08x}"), noted, (said, channel))

    def test_the_rule_reads_each_part_and_never_a_skipped_one(self):
        multi = {**self.OPTIONS, "multiple": True}
        self.assertTrue(project_context.real(multi, ["对所有部署默认打开（推荐）", "保持默认关"], "card"))
        self.assertFalse(project_context.real(self.FREE, ["好的", "这样行吗？"], "card"))
        self.assertTrue(project_context.asks_back("真的么"))
        self.assertFalse(project_context.asks_back("先都不动，我自己逐个看"))
        record = {"token": "fedcba9876543210", "questions": [self.FREE],
                  "answers": {"free": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}}, "answer_by": {"channel": "card"}}
        self.assertEqual(project_context.answered(record), [])


class SubmittedAnswerTest(QuestionFixture, unittest.TestCase):
    """The click itself: the Bridge's own submit_answer notes the decision when the topic belongs to a project."""

    def setUp(self):
        super().setUp()
        environment(self, **ON)
        state = self.ctx_topic.state_dir
        projects.create(state, projects.new_record(chat_id="oc_chat", name="迁移账单", project_id=PID, now=NOW))
        topic = state / "topics" / self.job.topic_id / "topic.json"
        data = read_object(topic)
        write_json(topic, {**data, "project": projects.project_field(PID, projects.MEMBER)}, ensure_ascii=False, indent=2)
        self.state = state

    def test_a_submitted_answer_in_a_project_topic_is_noted(self):
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0"}, "answer-1",
                                         by=self.app.runtime.owner_open_id)
        rows = memstore.decisions(self.state, "project", PID)
        self.assertEqual([(row.kind, row.said, row.text) for row in rows], [("decision", "owner", "主人答：范围？→全部")])

    def test_an_answer_not_known_to_be_the_owners_is_not_noted_as_his(self):
        # W0-1 (T892 P0-1): words a lead or the butler sent the topic, or a group member's answer, are no decision of the owner's.
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0"}, "answer-1", by="ou_member")
        self.assertEqual(memstore.decisions(self.state, "project", PID), [])
        self.assertEqual(json.loads(self.path.read_text())["status"], "answered")  # The answer itself went through.

    def test_an_answer_by_of_the_owners_click_is_noted_as_his_string_was(self):
        # WAVE12 C2 (W1-0): submit_answer takes an AnswerBy beside the open_id; its open_id is what decides, as before.
        from covey.app.answers import AnswerBy

        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0"}, "answer-1",
                                         by=AnswerBy("owner", "card", open_id=self.app.runtime.owner_open_id))
        rows = memstore.decisions(self.state, "project", PID)
        self.assertEqual([(row.kind, row.said, row.text) for row in rows], [("decision", "owner", "主人答：范围？→全部")])

    def test_an_answer_by_without_the_owners_open_id_is_not_noted_as_his(self):
        from covey.app.answers import AnswerBy

        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0"}, "answer-1",
                                         by=AnswerBy("owner", "card", open_id="ou_member"))
        self.assertEqual(memstore.decisions(self.state, "project", PID), [])
        self.assertEqual(json.loads(self.path.read_text())["status"], "answered")

    def test_with_the_switch_off_it_is_not(self):
        os.environ[projects.MEMORY_ENV] = "0"
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0"}, "answer-1")
        self.assertEqual(memory.live(self.state, projects.read_record(self.state, PID)), [])
        self.assertEqual(memstore.decisions(self.state, "project", PID), [])


class FilesTest(Case):
    def test_a_file_to_the_lead_is_copied_with_its_hash_and_the_same_content_once(self):
        first = self.attachment("方案.md", b"plan v1")
        project_context.owner_files(self.state, LEAD, CHAT, self.message(first), self.media, self.logs.append)
        (row,) = library.entries(self.state, PID)
        self.assertEqual((row["kind"], row["name"], row["source"], row["size"]), ("file", "方案.md", "owner_file", 7))
        self.assertEqual(len(row["sha256"]), 64)
        copy = library.library_dir(self.state, PID) / row["ref"]
        self.assertEqual(copy.read_bytes(), b"plan v1")
        again = self.attachment("方案-又发.md", b"plan v1")
        project_context.owner_files(self.state, LEAD, CHAT, self.message(again), self.media)
        self.assertEqual(len(library.entries(self.state, PID)), 1)
        other = self.attachment("方案.md", b"plan v2")  # Same name, other content: a name of its own.
        project_context.owner_files(self.state, LEAD, CHAT, self.message(other), self.media)
        names = sorted(path.name for path in library.library_dir(self.state, PID).iterdir())
        self.assertEqual(len(names), 2)
        self.assertEqual(self.logs, [])

    def test_a_member_topic_a_text_without_files_and_a_path_outside_the_topics_media_copy_nothing(self):
        path = self.attachment("方案.md")
        project_context.owner_files(self.state, MEMBER, CHAT, self.message(path), self.media)
        project_context.owner_files(self.state, LEAD, CHAT, "只有文字", self.media)
        outside = self.state / "secret.txt"
        outside.write_text("no")
        project_context.owner_files(self.state, LEAD, CHAT, self.message(outside), self.media)
        self.assertEqual(library.entries(self.state, PID), [])

    def test_a_large_file_is_noted_by_its_path_not_copied(self):
        path = self.attachment("big.bin", b"0123456789")
        with mock.patch.object(library, "FILE_LIMIT", 5):
            row, new = library.add_file(self.state, PID, path, source="owner_file", now=NOW)
        self.assertEqual((new, row["kind"], row["ref"], row["size"]), (True, "path", str(path), 10))
        self.assertFalse(library.library_dir(self.state, PID).exists())

    def test_the_projects_files_stay_under_the_limit(self):
        with mock.patch.object(library, "PROJECT_LIMIT", 10):
            first, _ = library.add_file(self.state, PID, self.attachment("a.bin", b"123456"), now=NOW)
            second, _ = library.add_file(self.state, PID, self.attachment("b.bin", b"abcdef"), now=NOW + 1)
        self.assertEqual((first["kind"], second["kind"]), ("file", "path"))

    def test_names_cannot_leave_the_folder(self):
        self.assertEqual(library.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(library.safe_name(".hidden"), "hidden")
        self.assertEqual(library.safe_name(""), "file")
        link = self.media / "link"
        link.symlink_to(self.attachment("real.txt"))
        with self.assertRaises(ValueError):
            library.add_file(self.state, PID, link)

    def test_delete_takes_the_entry_and_its_copy(self):
        row, _ = library.add_file(self.state, PID, self.attachment("方案.md"), now=NOW)
        self.assertTrue(library.delete(self.state, PID, row["id"]))
        self.assertFalse(library.delete(self.state, PID, row["id"]))
        self.assertEqual((library.entries(self.state, PID), list(library.library_dir(self.state, PID).iterdir())), ([], []))


class TurnTest(Case):
    def finished(self, **checkpoint):
        cp = self.state / "cp.json"
        write_json(cp, checkpoint)
        return {"status": "done", "checkpoint_path": str(cp), "job_id": 3}

    def test_artifacts_and_a_delivery_are_noted_as_paths_links_and_a_line(self):
        record = self.finished(artifacts=["/data/out/bill.csv", "https://example.com/doc", ""],
                               delivery={"commit": "a6bc148", "deployed": "第 12 代", "verified": "测试群点过"})
        project_context.turn_to_library(self.state, PID, MEMBER, record, self.logs.append)
        project_context.turn_to_library(self.state, PID, MEMBER, record)  # Once.
        rows = library.entries(self.state, PID)
        self.assertEqual([(row["kind"], row["ref"], row["source"]) for row in rows],
                         [("path", "/data/out/bill.csv", "artifact"), ("link", "https://example.com/doc", "artifact"),
                          ("delivery", "提交 a6bc148；部署 第 12 代；验收 测试群点过", "delivery")])
        self.assertFalse(library.library_dir(self.state, PID).exists())  # Nothing copied.

    def test_a_turn_that_did_not_end_well_or_has_nothing_adds_nothing(self):
        record = self.finished(artifacts=["/data/out/bill.csv"])
        project_context.turn_to_library(self.state, PID, MEMBER, {**record, "status": "failed"})
        project_context.turn_to_library(self.state, PID, MEMBER, {"status": "done"})
        project_context.turn_to_library(self.state, PID, MEMBER, {"status": "done", "checkpoint_path": str(self.state / "none.json")})
        self.assertEqual(library.entries(self.state, PID), [])

    def test_after_runner_notes_a_members_turn_and_not_the_leads(self):
        cp = self.state / "cp.json"
        write_json(cp, {"artifacts": ["/data/out/bill.csv"]})
        for topic, key in ((MEMBER, "m-1"), (LEAD, "m-2")):
            write_job(self.state, key, topic_id=topic, chat_id=CHAT, status="done", message_id="m", checkpoint_path=str(cp), job_id=1)
        for topic, key in ((MEMBER, "m-1"), (LEAD, "m-2")):
            job = SimpleNamespace(topic_id=topic, chat_id=CHAT, message_id=key.split("-")[0], job_id=int(key.split("-")[1]))
            with mock.patch.object(project_loop, "job_key", return_value=key):
                project_loop.after_runner(self.logs.append, self.state, job)
        self.assertEqual([row["topic_id"] for row in library.entries(self.state, PID)], [MEMBER])


class ResultTest(Case):
    """C7: every member turn that ended keeps its whole reply in result-files/<key>.md, noted in the library."""

    def setUp(self):
        super().setUp()
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, topic_number=12, project=projects.project_field(PID, projects.MEMBER))

    def end(self, key, text, job_id=41, topic=MEMBER, **fields):
        write_job(self.state, key, topic_id=topic, chat_id=CHAT, status="done", message_id=key, job_id=job_id, final_text=text, **fields)
        project_loop.after_runner(self.logs.append, self.state, SimpleNamespace(topic_id=topic, chat_id=CHAT, message_id=key, job_id=job_id))
        return self.state / "result-files" / f"{key}.md"

    def results(self):
        return [row for row in library.entries(self.state, PID) if row["source"] == "result"]

    def test_a_short_reply_is_kept_whole_and_noted_once(self):
        short = "导出好了，十二张表对账一致，差异为零。" * 6  # 114 characters: never a file to send.
        path = self.end("om_r1", short)
        self.assertEqual(path.read_text(), short)
        saved = read_object(self.state / "jobs" / "om_r1.json")
        self.assertEqual((saved["result_file_path"], saved["result_file_kept"]), (str(path), True))
        [row] = self.results()
        self.assertEqual((row["kind"], row["ref"], row["name"], row["topic_id"], row["redacted"]),
                         ("path", str(path), "T12 #41 结论全文", MEMBER, False))
        project_loop.after_runner(self.logs.append, self.state, SimpleNamespace(topic_id=MEMBER, chat_id=CHAT, message_id="om_r1", job_id=41))
        self.assertEqual(len(self.results()), 1)
        self.assertEqual(self.logs, [])

    def test_the_card_and_the_web_page_do_not_list_the_whole_replies(self):
        """They are the lead's and the topics' to read, one a member turn: the 专项卡's 资料库 and the web page's would
        fill with them and push the owner's files out."""
        from covey.app import project_facts
        self.end("om_r9", "结论")
        self.assertEqual(len(self.results()), 1)
        self.assertEqual([row for row in project_facts.library_rows(self.state, PID) if "结论全文" in str(row[0])], [])

    def test_a_long_reply_its_runner_sent_as_a_file_is_not_written_over(self):
        path = self.state / "result-files" / "om_r2.md"
        path.parent.mkdir(parents=True)
        path.write_text("长文全文" * 2000)
        self.end("om_r2", "卡片上的那段", result_file_path=str(path))
        self.assertEqual(path.read_text(), "长文全文" * 2000)
        self.assertNotIn("result_file_kept", read_object(self.state / "jobs" / "om_r2.json"))
        self.assertEqual([row["ref"] for row in self.results()], [str(path)])

    def test_no_reply_keeps_nothing_and_the_lead_keeps_none(self):
        self.end("om_r3", "")
        self.end("om_r4", "负责人的回答", topic=LEAD)
        self.assertFalse((self.state / "result-files").exists())
        self.assertEqual(self.results(), [])

    def test_a_redacted_topics_reply_is_listed_only_for_a_reader_with_redaction_on(self):
        write_topic(self.state, topic_id=MEMBER, chat_id=CHAT, topic_number=12, redaction=True,
                    project=projects.project_field(PID, projects.MEMBER))
        path = self.end("om_r5", "含敏感词的结论")
        [row] = self.results()
        self.assertTrue(row["redacted"])
        self.assertNotIn(str(path), "\n".join(library.index_lines(self.state, PID)))
        self.assertIn(str(path), "\n".join(library.index_lines(self.state, PID, redacted_reader=True)))
        self.assertIn(str(path), project_context.block(self.ctx(), self.job(), resumed=False))  # Its own topic: redaction on.
        plain = "cd" * 16
        write_topic(self.state, topic_id=plain, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        self.assertNotIn(str(path), project_context.block(self.ctx(plain), self.job(plain), resumed=False))
        (self.state / "topics" / plain / "topic.json").write_text("{")
        self.assertTrue(project_context.source_redacted(self.state, plain))  # Its setting cannot be read: redacted.
        self.assertFalse(project_context.marked(self.state, plain))  # Nor does it read the redacted ones.

    def test_a_delivery_line_is_cut_field_by_field_and_a_members_line_names_its_topic(self):
        library.add_ref(self.state, PID, "delivery", "提交 a6bc1481234567 9d3aecd 51c2e07 1111111 2222222；部署 " + "第 12 代" * 20
                        + "；验收 " + "测" * 100, name="交付", source="delivery", topic_id=MEMBER, now=NOW)
        [line] = library.index_lines(self.state, PID)
        self.assertIn("交付（话题的交付，", line)
        self.assertIn("，T12）：", line)
        self.assertIn("提交 a6bc148 9d3aecd 51c2e07 +2；", line)
        deployed, verified = line.split("；部署 ")[1].split("；验收 ")
        self.assertEqual((len(deployed), deployed[-1]), (40, "…"))
        self.assertEqual(verified, "测" * 59 + "…")
        self.assertEqual(library.delivery_words("提交 a6bc148；部署 第 12 代；验收 测试群点过"), "提交 a6bc148；部署 第 12 代；验收 测试群点过")
        self.assertEqual(library.delivery_words("随手写的一行"), "随手写的一行")
        path = self.end("om_r6", "好了")
        [line] = [line for line in library.index_lines(self.state, PID) if "结论全文" in line]
        self.assertTrue(line.startswith("T12 #41 结论全文（话题的结论，"))  # Its name has the number: not said twice.
        self.assertNotIn("，T12）", line)
        self.assertTrue(line.endswith(f"）：{path}"))

    def test_the_index_lists_the_newest_replies_only_and_keeps_the_owners_file(self):
        library.add_ref(self.state, PID, "path", "/data/in/brief.pdf", source="owner_file", now=NOW)
        for number in range(5):
            library.add_ref(self.state, PID, "path", f"/state/result-files/om_{number}.md", name=f"T12 #{number} 结论全文",
                            source="result", topic_id=MEMBER, now=NOW + 1 + number, redacted=False)
        lines = library.index_lines(self.state, PID)
        self.assertEqual([line.rsplit("/", 1)[-1] for line in lines[:4]], ["om_4.md", "om_3.md", "om_2.md", "brief.pdf"])
        self.assertEqual(lines[-1], "另有 2 条较早的未列出")

    def test_the_summary_is_the_checkpoints_phase_and_delivery_else_the_replys_opening_within_300(self):
        from covey.core.checkpoint import turn_summary

        record = {"final_text": "## 结论\n\n**要点**\n已迁完\n全部 12 张表对账一致\n\n第二段", "phase": "处理本轮请求"}
        self.assertEqual(turn_summary(record, {}), "已迁完 全部 12 张表对账一致")
        self.assertEqual(turn_summary(record, {"phase": "迁移完成", "delivery": {"commit": ["a6bc1481234"], "deployed": "第 12 代"}}),
                         "迁移完成；提交 a6bc148；部署 第 12 代")
        self.assertEqual(turn_summary({**record, "phase": "核对中", "delivery": {"verified": "抽查三张"}}), "核对中；验收 抽查三张")
        long = turn_summary({"final_text": "字" * 1000})
        self.assertEqual((len(long), long[-1]), (300, "…"))
        self.assertEqual(turn_summary({}, None), "")


class IndexTest(Case):
    def test_the_index_is_newest_first_within_2_kb_and_says_how_many_did_not_fit(self):
        for number in range(40):
            library.add_ref(self.state, PID, "path", f"/data/out/{number:02d}" + "x" * 150, source="artifact", topic_id=MEMBER, now=NOW + number)
        lines = library.index_lines(self.state, PID)
        self.assertLessEqual(sum(len(line.encode()) + 3 for line in lines), library.INDEX_BYTES)
        self.assertIn("/data/out/39", lines[0])
        self.assertRegex(lines[-1], r"^另有 \d+ 条较早的未列出$")
        self.assertEqual(library.index_lines(self.state, "ffffffffffff"), [])

    def test_the_lead_pack_lists_it_with_the_topics_label_and_not_without_the_switch(self):
        library.add_ref(self.state, PID, "delivery", "提交 a6bc148", name="交付", source="delivery", topic_id=MEMBER, now=NOW)
        pack = project_lead.state_pack(self.state, self.record(), "lead-1", full=True)
        self.assertIn("资料库：\n- 交付（话题的交付，", pack)
        self.assertIn("提交 a6bc148", pack)
        os.environ[projects.MEMORY_ENV] = "0"
        self.assertNotIn("资料库", project_lead.state_pack(self.state, self.record(), "lead-1", full=True))

    def test_the_entries_are_capped_and_a_dropped_ones_copy_goes(self):
        first, _ = library.add_file(self.state, PID, self.attachment("a.txt", b"a"), source="artifact", now=NOW - 10)
        with mock.patch.object(library, "MAX_ENTRIES", 3):
            for number in range(3):
                library.add_ref(self.state, PID, "path", f"/p/{number}", now=NOW + number)
        self.assertEqual(len(library.entries(self.state, PID)), 3)
        self.assertEqual(list(library.library_dir(self.state, PID).iterdir()), [])


class LibraryReviewTest(Case):
    """s2/review: what a full library drops first, the copy that is judged, and an index that cannot be read."""

    def test_a_full_index_drops_what_the_topics_made_before_the_owners_files(self):
        mine, _ = library.add_file(self.state, PID, self.attachment("资料.md", b"mine"), now=NOW - 100)  # The oldest of all.
        with mock.patch.object(library, "MAX_ENTRIES", 3):
            for number in range(4):
                library.add_ref(self.state, PID, "path", f"/p/{number}", source="artifact", now=NOW + number)
        rows = library.entries(self.state, PID)
        self.assertEqual(len(rows), 3)
        self.assertIn(mine["id"], [row["id"] for row in rows])
        self.assertTrue((library.library_dir(self.state, PID) / mine["ref"]).exists())
        self.assertEqual(sorted(row["ref"] for row in rows if row["kind"] == "path"), ["/p/2", "/p/3"])

    def test_with_only_the_owners_files_left_the_oldest_of_them_goes(self):
        first, _ = library.add_file(self.state, PID, self.attachment("a.md", b"a"), now=NOW)
        library.add_file(self.state, PID, self.attachment("b.md", b"b"), now=NOW + 1)
        with mock.patch.object(library, "MAX_ENTRIES", 1):
            library.add_file(self.state, PID, self.attachment("c.md", b"c"), now=NOW + 2)
        self.assertEqual([row["name"] for row in library.entries(self.state, PID)], ["c.md"])
        self.assertEqual(sorted(p.name for p in library.library_dir(self.state, PID).iterdir()), ["c.md"])

    def test_trim_takes_the_topics_files_before_the_owners(self):
        library.add_file(self.state, PID, self.attachment("资料.md", b"mine"), now=NOW - 100)
        library.add_file(self.state, PID, self.attachment("out.csv", b"theirs"), source="artifact", now=NOW)
        self.assertEqual(library.trim(self.state, PID, limit=4), 1)
        self.assertEqual(sorted(p.name for p in library.library_dir(self.state, PID).iterdir()), ["资料.md"])

    def test_the_copy_is_what_is_hashed_and_judged(self):
        source = self.attachment("a.bin", b"abc")
        row, new = library.add_file(self.state, PID, source, now=NOW)
        copy = library.library_dir(self.state, PID) / row["ref"]
        self.assertEqual(library.digest(copy), (row["sha256"], row["size"]))
        self.assertEqual([p.name for p in library.library_dir(self.state, PID).iterdir()], ["a.bin"])  # No temporary file left.

    def test_a_source_that_grows_while_it_is_copied_is_judged_by_what_was_copied(self):
        source = self.attachment("g.bin", b"x" * 10)
        with mock.patch.object(library, "FILE_LIMIT", 20):
            reads = []
            real_open = open

            def growing(path, mode="r", *args, **kwargs):
                handle = real_open(path, mode, *args, **kwargs)
                if Path(path) == source and not reads:
                    reads.append(1)
                    source.write_bytes(b"y" * 50)  # Grew after the size was looked at.
                    handle.close()
                    handle = real_open(path, mode, *args, **kwargs)
                return handle
            with mock.patch("builtins.open", growing):
                row, new = library.add_file(self.state, PID, source, now=NOW)
        self.assertEqual((row["kind"], new), ("path", True))
        self.assertEqual(list(library.library_dir(self.state, PID).iterdir()), [])

    def test_the_project_total_is_judged_with_the_copy_inside_the_lock(self):
        with mock.patch.object(library, "PROJECT_LIMIT", 10):
            first, _ = library.add_file(self.state, PID, self.attachment("a.bin", b"a" * 6), now=NOW)
            second, _ = library.add_file(self.state, PID, self.attachment("b.bin", b"b" * 6), now=NOW + 1)
        self.assertEqual((first["kind"], second["kind"]), ("file", "path"))
        self.assertEqual(sorted(p.name for p in library.library_dir(self.state, PID).iterdir()), ["a.bin"])

    def test_an_index_that_cannot_be_read_is_not_written_over(self):
        library.add_ref(self.state, PID, "path", "/p/1", source="artifact", now=NOW)
        path = library.index_path(self.state, PID)
        with mock.patch.object(library, "read_json", side_effect=PermissionError("denied")):
            self.assertEqual(library.entries(self.state, PID), [])  # A reader sees nothing...
            with self.assertRaises(PermissionError):
                library.add_ref(self.state, PID, "path", "/p/2", source="artifact", now=NOW + 1)  # ...a writer does not go on.
        self.assertEqual(len(library.entries(self.state, PID)), 1)
        self.assertTrue(path.exists())

    def test_an_index_that_cannot_be_parsed_is_kept_aside_and_a_new_one_started(self):
        library.add_ref(self.state, PID, "path", "/p/1", source="artifact", now=NOW)
        path = library.index_path(self.state, PID)
        path.write_text('{"entries": [')
        library.add_ref(self.state, PID, "path", "/p/2", source="artifact", now=NOW + 1)
        kept = [p for p in path.parent.iterdir() if p.name.startswith("library.json.bad-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), '{"entries": [')
        self.assertEqual([row["ref"] for row in library.entries(self.state, PID)], ["/p/2"])


class CardTest(Case):
    def test_the_card_lists_the_index_then_the_files_it_does_not_name_and_delete_takes_either(self):
        row, _ = library.add_file(self.state, PID, self.attachment("方案.md"), now=NOW)
        library.add_ref(self.state, PID, "delivery", "提交 a6bc148", name="交付", source="delivery", now=NOW + 1)
        (library.library_dir(self.state, PID) / "手放的.csv").write_text("x")
        rows = project_facts.library_rows(self.state, PID)
        self.assertEqual([entry[0] for entry in rows], ["交付：提交 a6bc148", "方案.md（主人发的）", "手放的.csv"])
        self.assertEqual(rows[1][1], row["id"])
        self.assertEqual(len(rows[2]), 1)
        self.assertTrue(library.delete(self.state, PID, row["id"]))
        self.assertEqual([entry[0] for entry in project_facts.library_rows(self.state, PID)], ["交付：提交 a6bc148", "手放的.csv"])

    def test_the_memory_rows_have_the_ids_the_delete_click_carries(self):
        item, _ = memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        memory.set_style(self.state, PID, "汇报风格：x", now=NOW)
        self.assertEqual(project_facts.memory_rows(self.state, self.record()), ((item["id"], "窗口在周六"),))


class MaintenanceTest(Case):
    def put_files(self):
        library.add_file(self.state, PID, self.attachment("方案.md", b"plan"), now=NOW)
        return library.library_dir(self.state, PID)

    def test_the_files_of_a_project_done_30_days_go_and_the_index_stays(self):
        folder = self.put_files()
        projects.set_state(self.state, PID, projects.DONE, "做完")
        record = projects.record_path(self.state, PID)
        later = time.time() + 31 * DAY
        self.assertEqual(plan_maintenance(self.state, now=time.time())["purge_project_files"], [])
        self.assertEqual(plan_maintenance(self.state, now=later)["purge_project_files"], [str(folder)])
        os.utime(record, (later - 40 * DAY, later - 40 * DAY))
        maintain(self.state, apply=True, now=later, backup=False)
        (row,) = library.entries(self.state, PID)
        self.assertTrue(row["purged"] and len(row["sha256"]) == 64)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertIn("文件已清理", library.index_lines(self.state, PID)[0])

    def test_an_active_project_keeps_its_files_unless_it_is_over_200_mb(self):
        folder = self.put_files()
        later = time.time() + 400 * DAY
        self.assertEqual(plan_maintenance(self.state, now=later)["purge_project_files"], [])
        with mock.patch.object(library, "PROJECT_LIMIT", 2):
            self.assertEqual(plan_maintenance(self.state, now=later)["purge_project_files"], [str(folder)])
            maintain(self.state, apply=True, now=later, backup=False)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(len(library.entries(self.state, PID)), 1)

    def test_memory_entries_go_after_180_days_and_the_style_line_stays(self):
        old = time.time() - 200 * DAY
        memory.add(self.state, PID, "很老的", "fact", now=old)
        memory.set_style(self.state, PID, "汇报风格：x", now=old)
        memory.add(self.state, PID, "新的", "fact", now=time.time())
        path = memory.memory_path(self.state, PID)
        self.assertEqual(plan_maintenance(self.state, now=time.time())["trim_project_memory"], [])  # At work: kept, however old.
        projects.set_state(self.state, PID, projects.DONE, "做完")
        self.assertEqual(plan_maintenance(self.state, now=time.time())["trim_project_memory"], [str(path)])
        maintain(self.state, apply=True, now=time.time(), backup=False)
        self.assertEqual(sorted(row["text"] for row in memory.stored(self.state, PID)), ["新的", "汇报风格：x"])
        self.assertEqual(plan_maintenance(self.state, now=time.time())["trim_project_memory"], [])

    def test_a_project_at_work_or_paused_keeps_its_old_memory_a_cancelled_one_loses_it(self):
        memory.add(self.state, PID, "很老的", "decision", now=time.time() - 400 * DAY)
        for state in (projects.ACTIVE, projects.PAUSED):
            projects.set_state(self.state, PID, state, "x")
            self.assertEqual(plan_maintenance(self.state, now=time.time())["trim_project_memory"], [])
            maintain(self.state, apply=True, now=time.time(), backup=False)
            self.assertEqual(len(memory.stored(self.state, PID)), 1)
        projects.set_state(self.state, PID, projects.CANCELLED, "不做了")
        maintain(self.state, apply=True, now=time.time(), backup=False)
        self.assertEqual(memory.stored(self.state, PID), [])

    def test_a_backup_holds_a_projects_record_and_memory_but_not_its_librarys_files(self):
        # Projects were out of every backup (2026-11-16) for the files a library copies, up to PROJECT_LIMIT each;
        # T604 keeps the rest, the owner's projects and their memory.
        self.put_files()
        memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        held = [path.relative_to(self.state).as_posix() for path in backup.backup_paths(self.state)
                if "projects" in path.parts]
        self.assertIn(f"projects/{PID}/memory.json", held)
        self.assertIn(f"projects/{PID}/{library.FILE}", held)
        self.assertEqual([name for name in held if f"/{library.FOLDER}/" in name], [])


if __name__ == "__main__":
    unittest.main()

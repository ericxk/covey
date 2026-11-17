"""记忆 (COVEY_MEMORY=1, core/memory.py, app/memory_turn.py, app/memory_collect.py, cards/answer_card.py memory_element): the one
block of a turn's rules, who said each line, the numbers a turn carried, the owner's words that become memory after the turn
and a 不对 that takes one back; and that with the switch off the rules, the reminder and the cards are word for word as they were."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_context, quiet
from support.fake_backends import FakeBackend
from support.records import write_job, write_topic
from support.world import harness_app

from covey.app import answer_cards, memory_collect, memory_turn
from covey.app.card_actions import ACTIONS
from covey.app.prompts import MEMORY_FIX_RULE, MEMORY_HEAD, MEMORY_RULES, RESUME_REMINDERS, memory_block
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.cards import answer_card as cards
from covey.core import knowledge, memory, principles, project_memory, projects, topic_memory
from covey.core.checkpoint import CHECKPOINT_RULES
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import find_job_record
from covey.core.state import write_json

ON = {memory.ENV: "1"}
OWNER, DM = "ou_owner", "oc_dm"
PID, CHAT, TOPIC = "a1b2c3d4e5f6", "oc_owner_dm", "ab" * 16
NOW = 1_800_000_000.0


def item(ref, scope, by, text, at=0):
    return memory.make(ref, scope, by, text, at)


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)


class SelectTest(Case):
    def test_each_scope_has_its_limit_and_an_entry_is_whole_or_not_there(self):
        rows = [item(f"p:{i}", memory.GLOBAL, memory.OWNER, "字" * 100, at=i) for i in range(20)]
        kept, left = memory.select(rows)
        self.assertEqual(len(kept), 1200 // (100 + 24))
        self.assertEqual(len(kept) + left, 20)
        self.assertEqual({row["text"] for row in kept}, {"字" * 100})  # None cut in the middle.
        self.assertEqual(sorted(row["ref"] for row in kept), sorted(f"p:{i}" for i in range(20 - len(kept), 20)))  # The newest.

    def test_the_owners_words_come_first_and_a_lead_line_gives_way_when_full(self):
        rows = [item("project:x:l1", memory.PROJECT, memory.LEAD, "总" * 1900, at=9),
                item("project:x:o1", memory.PROJECT, memory.OWNER, "说" * 100, at=1)]
        kept, left = memory.select(rows)
        self.assertEqual([row["ref"] for row in kept], ["project:x:o1"])
        self.assertEqual(left, 1)
        both = memory.select([rows[0] | {"text": "总" * 50}, rows[1]])[0]
        self.assertEqual([row["by"] for row in both], [memory.OWNER, memory.LEAD])  # Listed: 你说的 first.

    def test_numbers_are_stable_and_never_shared(self):
        rows = [item(f"knowledge:k{i:06d}", memory.GLOBAL, memory.OWNER, "x", at=i) for i in range(60)]
        first = {row["ref"]: row["n"] for row in memory.select(rows)[0]}
        again = {row["ref"]: row["n"] for row in memory.select(list(reversed(rows)))[0]}
        self.assertEqual(first, again)
        self.assertEqual((len(first), len(set(first.values()))), (48, 48))
        self.assertRegex(next(iter(first.values())), r"^M\d{2,3}$")
        self.assertNotEqual(memory.number("a", {memory.number("a", set())}), memory.number("a", set()))

    def test_sources_and_redaction(self):
        self.assertEqual([memory.by_source(s) for s in ("owner", "owner_answer", "owner_fix", "lead", "", "x")],
                         ["owner", "owner", "owner", "lead", "lead", "lead"])
        marked = {"id": "a", "text": "脱敏话题里说的", "source": "owner", "at": 1, "redacted": True}
        plain = {"id": "b", "text": "普通的", "source": "owner", "at": 1}
        self.assertEqual([row["text"] for row in memory.items(topic=[marked, plain])], ["普通的"])
        self.assertEqual(len(memory.items(topic=[marked, plain], redacted=True)), 2)

    def test_the_block_says_number_scope_and_speaker_then_the_four_rules(self):
        kept, left = memory.select([item("project:x:o1", memory.PROJECT, memory.OWNER, "先做迁移"),
                                    item("topic:t1", memory.TOPIC, memory.LEAD, "产物放 out/")])
        block = memory_block(kept, left)
        lines = block.splitlines()
        self.assertEqual(lines[0], MEMORY_HEAD)
        self.assertRegex(lines[1], r"^- M\d+ · 管这个专项 · 你说的：先做迁移$")
        self.assertRegex(lines[2], r"^- M\d+ · 管这个话题 · 它总结的：产物放 out/$")
        self.assertEqual(lines[3:], list(MEMORY_RULES))
        self.assertEqual(len(MEMORY_RULES), 4)
        self.assertEqual(memory_block([], 0), "")

    def test_the_words_are_in_the_glossary(self):
        text = "\n".join((MEMORY_HEAD, *MEMORY_RULES, MEMORY_FIX_RULE))
        for retired in ("主线程", "长活", "快事", "方向", "专员", "线程"):
            self.assertNotIn(retired, text)


class TopicMemoryTest(Case):
    def test_add_read_delete_and_the_same_text_once(self):
        write_topic(self.state, topic_id="t1")
        row, new = topic_memory.add(self.state, "t1", " 不用 Redis  ")
        self.assertEqual((new, row["text"], row["source"]), (True, "不用 Redis", "owner"))
        self.assertEqual(topic_memory.add(self.state, "t1", "不用 Redis")[1], False)
        self.assertEqual([r["id"] for r in topic_memory.live(self.state, "t1")], [row["id"]])
        self.assertTrue(topic_memory.delete(self.state, "t1", row["id"]))
        self.assertFalse(topic_memory.delete(self.state, "t1", row["id"]))
        self.assertEqual(topic_memory.live(self.state, "t1"), [])
        kept = json.loads(topic_memory.memory_path(self.state, "t1").read_text())["entries"]
        self.assertEqual((len(kept), kept[0]["deleted"] > 0), (1, True))  # A tombstone: the text stays.

    def test_a_redacted_line_has_its_mark_and_an_ordinary_one_has_no_key(self):
        write_topic(self.state, topic_id="t1")
        topic_memory.add(self.state, "t1", "甲", redacted=True)
        topic_memory.add(self.state, "t1", "乙")
        marks = {r["text"]: r.get("redacted") for r in topic_memory.live(self.state, "t1")}
        self.assertEqual(marks, {"甲": True, "乙": None})

    def test_the_oldest_goes_past_the_limit(self):
        write_topic(self.state, topic_id="t1")
        for index in range(topic_memory.MAX_ENTRIES + 3):
            topic_memory.add(self.state, "t1", f"条 {index}", now=NOW + index)
        live = topic_memory.live(self.state, "t1")
        self.assertEqual((len(live), live[0]["text"]), (topic_memory.MAX_ENTRIES, "条 3"))
        self.assertEqual(topic_memory.live(self.state, ""), [])


class TurnCase(Case):
    def setUp(self):
        super().setUp()
        self.ctx = make_context(self.state)
        self.state = self.ctx.state_dir

    def app(self):
        app = harness_app(CLAUDE_SPEC, FakeBackend(), ClaudeSessionStore())
        quiet(app)
        app.runtime.state_dir, app.runtime.owner_open_id = self.state, OWNER
        app.runtime.access.remember_owner_chat(DM)
        return app

    def job(self, text="帮我检查代码", job_id=5, topic="", **fields):
        return ReplyJob(job_id=job_id, user_text=text, chat_id=DM, message_id=f"om{job_id}", timeout=0, sender_id=OWNER,
                        topic_id=topic, **fields)

    def seed(self):
        principles.save(self.state, "owner", "发版前先跑全量")
        principles.save(self.state, "workspace", "ws 里别用 rg 扫 NAS", workspace=self.ctx.workspace_name)
        knowledge.save(self.state, "owner", "decision", "文档写中文")


class TurnTest(TurnCase):
    def test_switch_off_the_rules_the_reminder_and_the_cards_are_what_they_were(self):
        self.env(**{knowledge.ENV: "1"})
        self.seed()
        runs = self.app().runs
        job = self.job()
        rules = runs.bridge_rules(self.ctx, job)
        self.assertNotIn(MEMORY_HEAD, rules)
        self.assertNotIn(MEMORY_FIX_RULE, rules)
        self.assertIn("主人的原则", rules)
        self.assertIn("主人的知识条目", rules)
        resumed = runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertNotIn(MEMORY_HEAD, resumed)
        self.assertIsNone(find_job_record(self.state, "om5"))  # Nothing written either.
        self.assertFalse(any("memory" in name for name in os.listdir(self.state)))

    def test_one_block_stands_in_for_the_principles_and_the_entries_in_rules_and_reminder(self):
        self.env(**ON, **{knowledge.ENV: "1"})
        self.seed()
        runs = self.app().runs
        job = self.job()
        rules = runs.bridge_rules(self.ctx, job)
        self.assertEqual(rules.count(MEMORY_HEAD), 1)
        for text in ("发版前先跑全量", "ws 里别用 rg 扫 NAS", "文档写中文"):
            self.assertEqual(rules.count(text), 1, text)
        self.assertNotIn("主人的原则", rules)
        self.assertNotIn("主人的知识条目", rules)
        self.assertRegex(rules, r"- M\d+ · 管全局 · 你说的：决定：文档写中文")
        self.assertRegex(rules, r"- M\d+ · 管这个工作区 · 你说的：ws 里别用 rg 扫 NAS")
        self.assertLess(rules.index(MEMORY_HEAD), rules.index(CHECKPOINT_RULES))
        resumed = runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertEqual(resumed.count(MEMORY_HEAD), 1)
        self.assertEqual(resumed.count("发版前先跑全量"), 1)
        self.assertIn(RESUME_REMINDERS, resumed)

    def test_the_numbers_a_turn_carried_are_in_its_record(self):
        self.env(**ON)
        self.seed()
        write_job(self.state, "om5", job_id=5, topic_id="", chat_id=DM, status="running")
        runs = self.app().runs
        job = self.job()
        runs._with_rules(self.ctx, job, "MSG", runs.bridge_rules(self.ctx, job), system=False, fresh=True, rules_session=False)
        ids = find_job_record(self.state, "om5")["memory_ids"]
        self.assertEqual(sorted(row["ref"].split(":")[0] for row in ids), ["principle", "principle"])
        self.assertTrue(all(row["by"] == "owner" and row["n"].startswith("M") for row in ids))

    def test_project_lines_are_marked_by_who_wrote_them(self):
        self.env(**ON, **{projects.ENV: "1", projects.MEMORY_ENV: "1"})
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        project_memory.add(self.state, PID, "先迁 2024 年", "decision", "owner_answer")
        project_memory.add(self.state, PID, "接口叫 bill_v2", "fact", "lead")
        topic_memory.add(self.state, TOPIC, "这个话题只看报错")
        self.ctx.topic_id = TOPIC
        text = memory_turn.block(self.ctx, self.job(topic=TOPIC), owner_turn=lambda: True)
        self.assertRegex(text, r"- M\d+ · 管这个专项 · 你说的：先迁 2024 年")
        self.assertRegex(text, r"- M\d+ · 管这个专项 · 它总结的：接口叫 bill_v2")
        self.assertRegex(text, r"- M\d+ · 管这个话题 · 你说的：这个话题只看报错")
        self.assertLess(text.index("先迁 2024 年"), text.index("接口叫 bill_v2"))

    def test_a_colleague_does_not_read_the_owners_global_lines_and_a_proxy_topic_nothing(self):
        self.env(**ON)
        self.seed()
        mine = memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True)
        theirs = memory_turn.block(self.ctx, self.job(), owner_turn=lambda: False)
        self.assertIn("发版前先跑全量", mine)
        self.assertNotIn("发版前先跑全量", theirs)
        self.assertIn("ws 里别用 rg 扫 NAS", theirs)
        with mock.patch.object(memory_turn, "proxy_topic", return_value=True):
            self.assertEqual(memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True), "")

    def test_the_fix_rule_comes_only_with_the_owners_words(self):
        self.env(**ON)
        runs = self.app().runs
        quiet_job, said = self.job("帮我检查代码"), self.job("以后别再先问我侧重点了")
        self.assertNotIn(MEMORY_FIX_RULE, runs.bridge_rules(self.ctx, quiet_job))
        rules = runs.bridge_rules(self.ctx, said)
        self.assertTrue(rules.endswith(CHECKPOINT_RULES + "\n\n" + MEMORY_FIX_RULE))
        resumed = runs._with_rules(self.ctx, said, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertTrue(resumed.endswith(MEMORY_FIX_RULE))
        butler_job = self.job("以后别再问", topic="butler-x")
        with mock.patch.object(memory_turn, "is_butler_topic", return_value=True):
            self.assertFalse(memory_turn.fix_rule(self.ctx, butler_job, lambda: True))
        self.assertFalse(memory_turn.fix_rule(self.ctx, said, lambda: False))
        self.assertFalse(memory_turn.fix_rule(self.ctx, self.job("以后别再问", prompt_text="管家转来的"), lambda: True))

    def test_knowledge_keeps_its_own_remember_words(self):
        self.env(**ON, **{knowledge.ENV: "1"})
        runs = self.app().runs
        self.assertFalse(memory_turn.fix_rule(self.ctx, self.job("记住这个坑"), lambda: True))
        self.assertTrue(memory_turn.fix_rule(self.ctx, self.job("记住，以后都这样"), lambda: True))
        self.env(**{knowledge.ENV: ""})
        self.assertTrue(memory_turn.fix_rule(self.ctx, self.job("记住这个坑"), lambda: True))
        self.assertIsNotNone(runs)


class CollectCase(Case):
    def setUp(self):
        super().setUp()
        self.env(**ON, **{projects.ENV: "1", projects.MEMORY_ENV: "1"})
        self.logs = []
        owner_turn = lambda chat, sender: sender == OWNER  # noqa: E731
        self.runtime = mock.Mock(log=self.logs.append, access=mock.Mock(owner_turn=owner_turn))
        self.messenger = mock.Mock()
        self.messenger.patch_card.return_value = True
        self.ctx = make_context(self.state)
        self.state = self.ctx.state_dir
        write_topic(self.state, topic_id="t1", topic_number=7)
        self.ctx.topic_id = "t1"
        self.count = 4

    def turn(self, *, memory_lines=None, void=None, user_text="以后别再先问侧重点，直接开工", status="done", sender=OWNER,
             prompt_text="", topic="t1", ids=(), **record):
        self.count += 1
        key = f"om{self.count}"
        path = self.state / f"cp-{key}.json"
        data = {"phase": "完成"}
        if memory_lines is not None:
            data["memory"] = memory_lines
        if void is not None:
            data["memory_void"] = void
        write_json(path, data)
        write_job(self.state, key, job_id=self.count, topic_id=topic, chat_id=DM, status=status, user_text=user_text,
                  workspace_name="bridge", checkpoint_path=str(path), prompt_text=prompt_text, sender_id=sender,
                  memory_ids=list(ids), **record)
        return ReplyJob(job_id=self.count, user_text=user_text, chat_id=DM, message_id=key, timeout=0, sender_id=sender,
                        topic_id=topic, prompt_text=prompt_text or None)

    def collect(self, job):
        memory_collect.collect(self.runtime, self.messenger, job, self.ctx)
        return find_job_record(self.state, job.message_id)

    LINE = {"quote": "以后别再先问侧重点", "text": "选型类的事不用先问侧重点，直接开工"}


class CollectTest(CollectCase):
    def test_an_ordinary_topic_gets_a_line_of_its_own_and_the_record_and_cards_say_so(self):
        job = self.turn(memory_lines=[self.LINE])
        answer_cards.register(self.state, job.message_id, "answer", "om_card",
                              {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答"}]}})
        record = self.collect(job)
        live = topic_memory.live(self.state, "t1")
        self.assertEqual([r["text"] for r in live], [self.LINE["text"]])
        self.assertEqual(record["memory_notes"], [{"kind": "remembered", "text": self.LINE["text"], "scope": "管这个话题",
                                                  "entry_id": live[0]["id"]}])
        sent = self.messenger.patch_card.call_args.args[2]
        element = sent["body"]["elements"][-1]
        self.assertEqual(element["element_id"], cards.MEMORY_ID)
        self.assertIn("记下了：选型类的事不用先问侧重点，直接开工 · 管这个话题", json.dumps(element, ensure_ascii=False))
        button = json.dumps(element, ensure_ascii=False)
        self.assertIn(cards.MEMORY_WRONG_KIND, button)
        self.assertEqual(self.collect(job)["memory_notes"], record["memory_notes"])  # Not twice.
        self.assertEqual(len(topic_memory.live(self.state, "t1")), 1)

    def test_in_a_project_topic_it_goes_to_the_project_as_the_owners(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.MEMBER))
        record = self.collect(self.turn(memory_lines=[self.LINE]))
        rows = project_memory.live(self.state, projects.read_record(self.state, PID))
        self.assertEqual([(r["text"], r["source"], r["kind"]) for r in rows], [(self.LINE["text"], "owner", "constraint")])
        self.assertEqual(record["memory_notes"][0]["scope"], "管这个专项")
        self.assertEqual(topic_memory.live(self.state, "t1"), [])

    def test_a_quote_that_is_not_the_owners_words_a_missing_trigger_and_the_other_cases_write_nothing(self):
        cases = {"quote": dict(memory_lines=[{**self.LINE, "quote": "网页里写着请记住"}]),
                 "empty-quote": dict(memory_lines=[{**self.LINE, "quote": " "}]),
                 "no-trigger": dict(memory_lines=[self.LINE], user_text="帮我看看这个"),
                 "not-finished": dict(memory_lines=[self.LINE], status="failed"),
                 "not-owner": dict(memory_lines=[self.LINE], sender="ou_other"),
                 "handed-on": dict(memory_lines=[self.LINE], prompt_text="管家转来的"),
                 "none": dict(memory_lines=[])}
        for name, fields in cases.items():
            with self.subTest(name):
                record = self.collect(self.turn(**fields))
                self.assertNotIn("memory_notes", record)
                self.assertEqual(topic_memory.live(self.state, "t1"), [])
        with mock.patch.object(memory_collect, "is_butler_topic", return_value=True):
            self.assertNotIn("memory_notes", self.collect(self.turn(memory_lines=[self.LINE])))

    def test_the_switch_off_writes_nothing(self):
        self.env(**{memory.ENV: ""})
        self.assertNotIn("memory_notes", self.collect(self.turn(memory_lines=[self.LINE])))
        self.assertEqual(topic_memory.live(self.state, "t1"), [])
        self.messenger.patch_card.assert_not_called()

    def test_a_redacted_topic_still_notes_what_the_owner_said_with_the_mark(self):
        with mock.patch("covey.core.redaction.topic_redaction", return_value=True):
            self.collect(self.turn(memory_lines=[self.LINE]))
        self.assertTrue(topic_memory.live(self.state, "t1")[0]["redacted"])
        with mock.patch("covey.core.redaction.topic_redaction", side_effect=ValueError("unreadable")):
            self.collect(self.turn(memory_lines=[{**self.LINE, "text": "另一句"}]))
        self.assertTrue(topic_memory.live(self.state, "t1")[1]["redacted"])

    def test_at_most_three_lines_a_turn(self):
        lines = [{**self.LINE, "text": f"第 {i} 条"} for i in range(5)]
        self.assertEqual(len(self.collect(self.turn(memory_lines=lines))["memory_notes"]), memory_collect.PER_TURN)


class VoidTest(CollectCase):
    def setUp(self):
        super().setUp()
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id="t1", topic_number=7, project=projects.project_field(PID, projects.MEMBER))
        self.lead, _ = project_memory.add(self.state, PID, "选型前先问侧重点", "constraint", "lead")
        self.said, _ = project_memory.add(self.state, PID, "报告写中文", "constraint", "owner")
        self.ids = [{"n": "M12", "ref": f"project:{PID}:{self.lead['id']}", "scope": "project", "by": "lead"},
                    {"n": "M13", "ref": f"project:{PID}:{self.said['id']}", "scope": "project", "by": "owner"}]

    def live(self):
        return [r["text"] for r in project_memory.live(self.state, projects.read_record(self.state, PID))]

    def test_a_line_of_the_leads_the_turn_carried_is_voided_and_the_text_stays(self):
        record = self.collect(self.turn(void=[{"id": "M12", "quote": "别再先问侧重点"}], ids=self.ids))
        self.assertEqual(record["memory_notes"], [{"kind": "voided", "text": "选型前先问侧重点", "scope": "管这个专项",
                                                  "entry_id": self.lead["id"]}])
        self.assertEqual(self.live(), ["报告写中文"])
        self.assertIn("选型前先问侧重点", [r["text"] for r in project_memory.tombstones(
            self.state, projects.read_record(self.state, PID))])

    def test_what_the_owner_said_a_number_not_carried_and_a_bad_quote_are_not_voided(self):
        for name, void in (("owner's", {"id": "M13", "quote": "别再先问侧重点"}),
                           ("not carried", {"id": "M99", "quote": "别再先问侧重点"}),
                           ("bad quote", {"id": "M12", "quote": "我编的一句"})):
            with self.subTest(name):
                self.assertNotIn("memory_notes", self.collect(self.turn(void=[void], ids=self.ids)))
                self.assertEqual(self.live(), ["选型前先问侧重点", "报告写中文"])

    def test_the_undo_button_takes_a_line_back_and_the_note_says_so(self):
        job = self.turn(memory_lines=[self.LINE])
        answer_cards.register(self.state, job.message_id, "answer", "om_card", {"body": {"elements": []}})
        record = self.collect(job)
        entry = record["memory_notes"][0]["entry_id"]
        self.assertEqual(project_memory.live(self.state, projects.read_record(self.state, PID))[-1]["id"], entry)
        self.ctx.topic_id = "t1"
        value = {"kind": cards.MEMORY_WRONG_KIND, "topic_id": "t1", "job_id": record["job_id"], "entry_id": entry}
        event = {"action_value": value}
        host = mock.Mock(messenger=self.messenger, runtime=self.runtime)
        self.assertTrue(memory_collect.undo(host, self.ctx, event))
        self.assertNotIn(entry, [r["id"] for r in project_memory.live(self.state, projects.read_record(self.state, PID))])
        notes = find_job_record(self.state, job.message_id)["memory_notes"]
        self.assertEqual([note["kind"] for note in notes], ["undone"])
        self.assertIn("已撤销：", json.dumps(self.messenger.patch_card.call_args.args[2], ensure_ascii=False))
        self.assertTrue(memory_collect.undo(host, self.ctx, event))  # A second click does nothing more.
        self.assertEqual(self.messenger.patch_card.call_count, 2)
        with self.assertRaises(ValueError):
            memory_collect.undo(host, self.ctx, {"action_value": {**value, "job_id": "99999"}})


class CardTest(Case):
    def test_the_lines_and_the_button(self):
        notes = [{"kind": "remembered", "text": "不用 Redis", "scope": "管这个专项", "entry_id": "ab12"},
                 {"kind": "voided", "text": "先问侧重点", "scope": "管这个专项", "entry_id": "cd34"},
                 {"kind": "undone", "text": "旧的", "scope": "管这个话题", "entry_id": "ef56"}]
        element = cards.memory_element(notes, "t1", 7)
        text = json.dumps(element, ensure_ascii=False)
        for words in ("记下了：不用 Redis · 管这个专项", "作废了：先问侧重点", "已撤销：旧的"):
            self.assertIn(words, text)
        self.assertEqual(text.count('"不对"'), 1)
        self.assertIn('"entry_id": "ab12"', text)
        self.assertIsNone(cards.memory_element([]))
        self.assertIsNone(cards.memory_element([{"kind": "other", "text": "x"}]))

    def test_the_button_is_in_the_table_without_bot_only(self):
        action = ACTIONS[cards.MEMORY_WRONG_KIND]
        self.assertEqual((action.handler, action.bot_only), ("controls.memory_wrong", False))


if __name__ == "__main__":
    unittest.main()

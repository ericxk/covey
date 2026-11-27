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

from support import UNSET_OFF
from support import make_context, quiet
from support.fake_backends import FakeBackend
from support.records import write_job, write_topic
from support.world import harness_app

from covey.app import answer_cards, memory_collect, memory_turn
from covey.app.card_actions import ACTIONS
from covey.app.prompts import (MEMORY_FIX_RULE, MEMORY_HEAD, MEMORY_LAYER_RULES, MEMORY_RULES, RULES_HEAD, memory_block,
                                resume_reminders)
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.cards import answer_card as cards
from covey.core import knowledge, memory, memstore, principles, project_memory, projects, topic_memory, turn_source
from covey.core.checkpoint import CHECKPOINT_RULES, checkpoint_rules
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
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF, clear=True)
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
        rows = [item("project:x:l1", memory.PROJECT, memory.LEAD, "总" * 2700, at=9),  # Past the project's room, lent room and all.
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

    def test_the_block_says_number_scope_layer_and_speaker_then_the_rules(self):
        kept, left = memory.select([item("project:x:o1", memory.PROJECT, memory.OWNER, "先做迁移"),
                                    item("topic:t1", memory.TOPIC, memory.LEAD, "产物放 out/")])
        block = memory_block(kept, left)
        lines = block.splitlines()
        self.assertEqual(lines[0], MEMORY_HEAD)
        self.assertRegex(lines[1], r"^- M\d+ · 管这个专项 · 长期约定 · 你说的：先做迁移$")  # A project's line says its layer.
        self.assertRegex(lines[2], r"^- M\d+ · 管这个话题 · 它总结的：产物放 out/$")
        self.assertEqual(lines[3:], [*MEMORY_RULES, *MEMORY_LAYER_RULES])  # With a project's lines, what its layers are.
        self.assertEqual(len(MEMORY_RULES), 5)  # The fifth: what the owner allowed is not asked again.
        self.assertEqual(memory_block([], 0), "")
        alone = memory_block(*memory.select([item("topic:t1", memory.TOPIC, memory.OWNER, "只看报错")])).splitlines()
        self.assertEqual(alone[2:], list(MEMORY_RULES))  # No project, no layers.

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

    def job(self, text="帮我检查代码", job_id=5, topic="", message_id="", **fields):
        return ReplyJob(job_id=job_id, user_text=text, chat_id=DM, message_id=message_id or f"om{job_id}", timeout=0, sender_id=OWNER,
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

    def seed_store(self):
        memstore.add(self.state, kind="rule", scope="global", text="发版前先跑全量")
        memstore.add(self.state, kind="note", scope="global", text="文档写中文，术语照名词表")

    def test_the_rules_carry_the_rules_of_the_store_once_in_rules_and_reminder(self):
        self.env(**ON, **{knowledge.ENV: "1"})
        self.seed()  # The older stores: only the migration brings them into the one store.
        self.seed_store()
        runs = self.app().runs
        job = self.job()
        rules = runs.bridge_rules(self.ctx, job)
        self.assertEqual(rules.count(RULES_HEAD), 1)
        self.assertEqual(rules.count("发版前先跑全量"), 1)
        self.assertNotIn("文档写中文", rules)  # A note is the tail's when the turn's words find it.
        self.assertNotIn("主人的原则", rules)
        self.assertNotIn("主人的知识条目", rules)
        self.assertNotIn(MEMORY_HEAD, rules)
        self.assertRegex(rules, r"- R[0-9A-Z]{4,6} 发版前先跑全量")
        self.assertLess(rules.index(RULES_HEAD), rules.index(CHECKPOINT_RULES))
        resumed = runs._with_rules(self.ctx, job, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertEqual(resumed.count(RULES_HEAD), 1)
        self.assertEqual(resumed.count("发版前先跑全量"), 1)
        self.assertIn(resume_reminders(), resumed)

    def test_the_codes_a_turn_carried_are_in_its_record(self):
        self.env(**ON)
        self.seed_store()
        write_job(self.state, "om5", job_id=5, topic_id="", chat_id=DM, status="running")
        runs = self.app().runs
        job = self.job()
        runs._with_rules(self.ctx, job, "MSG", runs.bridge_rules(self.ctx, job), system=False, fresh=True, rules_session=False)
        record = find_job_record(self.state, "om5")
        self.assertEqual([row["ref"].split(":")[0] for row in record["memory_ids"]], ["global"])
        self.assertTrue(all(row["by"] == "owner" and memstore.CODE_RE.match(row["n"]) for row in record["memory_ids"]))
        self.assertEqual(record["memory_listed"], [])

    def test_project_lines_say_who_wrote_them_and_a_decision_is_at_the_tail(self):
        self.env(**ON, **{projects.ENV: "1", projects.MEMORY_ENV: "1"})
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, project=projects.project_field(PID, projects.MEMBER))
        project_memory.add(self.state, PID, "先迁 2024 年", "decision", "owner_answer", origin={"question": "0123456789abcdef"})
        project_memory.add(self.state, PID, "接口叫 bill_v2，别改名", "constraint", "owner")
        project_memory.add(self.state, PID, "账单接口按月分表", "fact", "lead")
        topic_memory.add(self.state, TOPIC, "这个话题只看报错")
        self.ctx.topic_id = TOPIC
        job = self.job(text="账单接口怎么分表的", topic=TOPIC)
        text = memory_turn.block(self.ctx, job, owner_turn=lambda: True)
        self.assertRegex(text, r"本专项「迁移账单」\n- R[0-9A-Z]{4,6} 接口叫 bill_v2，别改名\n")
        self.assertRegex(text, r"本话题\n- R[0-9A-Z]{4,6} 这个话题只看报错")
        self.assertNotIn("先迁 2024 年", text)
        tail = memory_turn.tail(self.ctx, job, lambda: True)
        self.assertRegex(tail, r"- N[0-9A-Z]{4,6} 账单接口按月分表（负责人记的）")

    def test_a_colleague_does_not_read_the_owners_global_lines_and_a_proxy_topic_nothing(self):
        self.env(**ON)
        self.seed_store()
        mine = memory_turn.block(self.ctx, self.job(), owner_turn=lambda: True)
        theirs = memory_turn.block(self.ctx, self.job(job_id=6), owner_turn=lambda: False)
        self.assertIn("发版前先跑全量", mine)
        self.assertNotIn("发版前先跑全量", theirs)
        with mock.patch.object(memory_turn, "proxy_topic", return_value=True):
            self.assertEqual(memory_turn.block(self.ctx, self.job(job_id=7), owner_turn=lambda: True), "")

    def test_the_fix_rule_comes_only_with_the_owners_words(self):
        self.env(**ON)
        runs = self.app().runs
        quiet_job, said = self.job("帮我检查代码"), self.job("以后别再先问我侧重点了")
        self.assertNotIn(MEMORY_FIX_RULE, runs.bridge_rules(self.ctx, quiet_job))
        rules = runs.bridge_rules(self.ctx, said)
        self.assertTrue(rules.endswith(checkpoint_rules() + "\n\n" + MEMORY_FIX_RULE))
        resumed = runs._with_rules(self.ctx, said, "MSG", rules, system=False, fresh=False, rules_session=False)
        self.assertTrue(resumed.endswith(MEMORY_FIX_RULE))
        butler_job = self.job("以后别再问", topic="butler-x")
        with mock.patch.object(memory_turn, "is_butler_topic", return_value=True):
            self.assertFalse(memory_turn.fix_rule(self.ctx, butler_job, lambda: True))
        self.assertFalse(memory_turn.fix_rule(self.ctx, said, lambda: False))
        self.assertFalse(memory_turn.fix_rule(self.ctx, self.job("以后别再问", message_id="main-tok1", prompt_text="管家转来的"), lambda: True))
        # A hand-on that carries his message id (stamped) is not his words, whatever its id begins with (W1-B).
        for made in (turn_source.make("relay", asserted_by="owner", origin={"message_id": "om_1"}),
                     turn_source.make("lead", asserted_by="lead", origin={"topic": "t1"})):
            handed = self.job("以后别再问", message_id="om_77")
            handed.turn_source = made
            self.assertFalse(memory_turn.fix_rule(self.ctx, handed, lambda: True))
        # A page's words and a reply in a topic's thread carry prompt_text too: still the owner's own turn.
        self.assertTrue(memory_turn.fix_rule(self.ctx, self.job("以后别再问", message_id="web-op1", prompt_text="以后别再问"), lambda: True))
        self.assertTrue(memory_turn.fix_rule(self.ctx, self.job("以后别再问", prompt_text="话题起始消息：…\n\n以后别再问"), lambda: True))

    def test_knowledge_keeps_its_own_remember_words(self):
        self.env(**ON, **{knowledge.ENV: "1"})
        runs = self.app().runs
        self.assertFalse(memory_turn.fix_rule(self.ctx, self.job("记住这个坑"), lambda: True))
        self.assertTrue(memory_turn.fix_rule(self.ctx, self.job("记住，以后都这样"), lambda: True))
        self.env(**{knowledge.ENV: "0"})
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
             prompt_text="", topic="t1", ids=(), key="", **record):
        self.count += 1
        key = key or f"om{self.count}"
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
                                                  "entry_id": live[0]["id"], "ref": f"topic:t1:{live[0]['id']}"}])
        sent = self.messenger.patch_card.call_args.args[2]
        element = sent["body"]["elements"][-1]
        self.assertEqual(element["element_id"], cards.MEMORY_ID)
        self.assertIn("记下了：选型类的事不用先问侧重点，直接开工 · 管这个话题", json.dumps(element, ensure_ascii=False))
        button = json.dumps(element, ensure_ascii=False)
        self.assertIn(cards.MEMORY_WRONG_KIND, button)
        self.assertEqual(self.collect(job)["memory_notes"], record["memory_notes"])  # Not twice.
        self.assertEqual(len(topic_memory.live(self.state, "t1")), 1)

    def test_words_from_the_page_or_a_reply_in_the_thread_are_taken_too(self):
        # Both fill prompt_text (the page's words; a reply carries the thread's first message as background): not handed on.
        for n, (key, prompt) in enumerate((("web-op7", "以后别再先问侧重点，直接开工"),
                                           ("om_reply", "话题起始消息：…\n\n以后别再先问侧重点，直接开工"))):
            with self.subTest(key):
                line = {**self.LINE, "text": f"{self.LINE['text']}（第 {n + 1} 次）"}
                record = self.collect(self.turn(memory_lines=[line], prompt_text=prompt, key=key))
                self.assertEqual([note["kind"] for note in record["memory_notes"]], ["remembered"])

    def test_an_answer_card_without_files_gets_the_lines_too(self):
        # Not listed in the answer-cards record (it shows no 产物): written from the card as it went out.
        job = self.turn(memory_lines=[self.LINE], answer_message_id="om_answer")
        sent = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答"}]}}
        write_json(answer_cards.record_path(self.state, job.message_id), {"schema_version": 1, "job_key": job.message_id, "cards": [],
                                                                           "outgoing": {"key": "k", "answer": sent}})
        self.collect(job)
        profile, message_id, built = self.messenger.patch_card.call_args.args[:3]
        self.assertEqual(message_id, "om_answer")
        self.assertEqual(built["body"]["elements"][0], sent["body"]["elements"][0])
        self.assertEqual(built["body"]["elements"][-1]["element_id"], cards.MEMORY_ID)
        self.assertIn("记下了：", json.dumps(built["body"]["elements"][-1], ensure_ascii=False))
        self.assertEqual(sent["body"]["elements"], [{"tag": "markdown", "content": "答"}])  # The kept card is not changed.

    def test_on_a_relay_the_lines_go_over_its_last_line_where_a_reply_goes(self):
        # T647 P1-A3: a 做完了 relay ends with 回复这张卡，就是接着跟 T12 说 (cards/relay_card.py reply_hint); it stays last.
        from covey.cards.relay_card import HINT_ID, reply_hint
        job = self.turn(memory_lines=[self.LINE])
        relay = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "答"}, reply_hint("T12")]}}
        write_json(answer_cards.record_path(self.state, job.message_id), {"schema_version": 1, "job_key": job.message_id,
                                                                           "cards": [{"where": "relay", "message_id": "om_relay",
                                                                                      "card": relay}]})
        self.collect(job)
        _, message_id, built = self.messenger.patch_card.call_args.args[:3]
        self.assertEqual(message_id, "om_relay")
        self.assertEqual([e.get("element_id") for e in built["body"]["elements"]][1:], [cards.MEMORY_ID, HINT_ID])

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
                 "handed-on": dict(memory_lines=[self.LINE], prompt_text="管家转来的", key="main-tok9"),
                 "none": dict(memory_lines=[])}
        for name, fields in cases.items():
            with self.subTest(name):
                record = self.collect(self.turn(**fields))
                self.assertNotIn("memory_notes", record)
                self.assertEqual(topic_memory.live(self.state, "t1"), [])
        with mock.patch.object(memory_collect, "is_butler_topic", return_value=True):
            self.assertNotIn("memory_notes", self.collect(self.turn(memory_lines=[self.LINE])))

    def test_the_switch_off_writes_nothing(self):
        self.env(**{memory.ENV: "0"})
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
                                                  "entry_id": self.lead["id"], "ref": f"project:{PID}:{self.lead['id']}"}])
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


class PromoteTest(CollectCase):
    """升为全局 (T647 E4): what the owner said in one topic held only there; one click on its "记下了" line makes it one of his
    principles for every topic, the topic's entry a tombstone, and 不对 takes the principle back."""

    def setUp(self):
        super().setUp()
        self.host = mock.Mock(messenger=self.messenger, runtime=self.runtime)

    def remembered(self, **record):
        job = self.turn(memory_lines=[self.LINE], **record)
        answer_cards.register(self.state, job.message_id, "answer", "om_card", {"body": {"elements": []}})
        return job, self.collect(job)

    def click(self, kind, record, entry):
        value = {"kind": kind, "topic_id": "t1", "job_id": record["job_id"], "entry_id": entry}
        return {"action_value": value}

    def test_a_topic_s_line_becomes_a_principle_for_every_topic_and_not_twice(self):
        job, record = self.remembered()
        entry = record["memory_notes"][0]["entry_id"]
        self.assertTrue(memory_collect.promote(self.host, self.ctx, self.click(cards.MEMORY_PROMOTE_KIND, record, entry)))
        [principle] = memstore.entries(self.state, scope="global")
        self.assertEqual((principle.text, principle.said, principle.kind), (self.LINE["text"], "owner", "rule"))
        self.assertEqual(memstore.entries(self.state, scope="topic", owner_key="t1"), [])  # Carried once, from 全局.
        [note] = find_job_record(self.state, job.message_id)["memory_notes"]
        self.assertEqual((note["kind"], note["scope"], note["ref"]), ("remembered", "管全局", principle.ref))
        card = json.dumps(self.messenger.patch_card.call_args.args[2], ensure_ascii=False)
        self.assertIn("记下了：选型类的事不用先问侧重点，直接开工 · 管全局", card)
        self.assertNotIn(cards.MEMORY_PROMOTE_KIND, card)  # Only 不对 is left on it.
        self.assertTrue(memory_collect.promote(self.host, self.ctx, self.click(cards.MEMORY_PROMOTE_KIND, record, entry)))
        self.assertEqual(len(memstore.entries(self.state, scope="global")), 1)  # A second click does nothing more.
        # 不对 now takes the global entry back.
        self.assertTrue(memory_collect.undo(self.host, self.ctx, self.click(cards.MEMORY_WRONG_KIND, record, note["entry_id"])))
        self.assertEqual(memstore.entries(self.state, scope="global"), [])
        self.assertEqual(find_job_record(self.state, job.message_id)["memory_notes"][0]["kind"], "undone")

    def test_a_line_from_a_topic_with_redaction_on_stays_there(self):
        with mock.patch.object(memory_collect, "_topic_marked", return_value=True):
            _, record = self.remembered()
        note = record["memory_notes"][0]
        self.assertTrue(note["redacted"])
        self.assertNotIn(cards.MEMORY_PROMOTE_KIND, json.dumps(cards.memory_element([note], "t1", 5), ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "脱敏"):
            memory_collect.promote(self.host, self.ctx, self.click(cards.MEMORY_PROMOTE_KIND, record, note["entry_id"]))
        self.assertEqual(memstore.entries(self.state, scope="global"), [])
        self.assertEqual(len(topic_memory.live(self.state, "t1")), 1)

    def test_a_line_deleted_since_is_not_promoted(self):
        _, record = self.remembered()
        entry = record["memory_notes"][0]["entry_id"]
        topic_memory.delete(self.state, "t1", entry)
        with self.assertRaisesRegex(ValueError, "不在了"):
            memory_collect.promote(self.host, self.ctx, self.click(cards.MEMORY_PROMOTE_KIND, record, entry))
        self.assertEqual(memstore.entries(self.state, scope="global"), [])


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
        self.assertEqual(text.count('"升为全局"'), 0)  # Moving to a larger scope is the 网页记忆中心's now.
        self.assertIn('"entry_id": "ab12"', text)
        for scope, redacted in (("管全局", False), ("管这个话题", True)):
            line = cards.memory_element([{**notes[0], "scope": scope, **({"redacted": True} if redacted else {})}], "t1", 7)
            self.assertNotIn("升为全局", json.dumps(line, ensure_ascii=False), scope)
        self.assertIsNone(cards.memory_element([]))
        self.assertIsNone(cards.memory_element([{"kind": "other", "text": "x"}]))

    def test_the_button_is_in_the_table_without_bot_only(self):
        action = ACTIONS[cards.MEMORY_WRONG_KIND]
        self.assertEqual((action.handler, action.bot_only), ("controls.memory_wrong", False))
        action = ACTIONS[cards.MEMORY_PROMOTE_KIND]
        self.assertEqual((action.handler, action.bot_only), ("controls.memory_promote", False))


if __name__ == "__main__":
    unittest.main()

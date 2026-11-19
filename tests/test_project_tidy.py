"""整理专项 (app/project_tidy.py, app/project_sorter.py, core/project_proposals.py, core/topic_gist.py; docs/butler.md 整理专项): a
topic's 话题要点 kept from its checkpoint, the moments of a real-time 整理 (its essence changed: a topic that began as A and became
B), a sure join done and its 移出 keeping the topic out for good, the proposal card's buttons, the 整理专项 card with a topic the
model will not read left out alone, and with the switch off nothing of it."""
from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from test_dispatch_cards import CHAT
from test_project_upgrade import UpCase

from covey.app import project_sorter as sorter
from covey.app import project_tidy as tidy_module
from covey.app import projects as upgrades
from covey.app.project_tidy import ProjectTidy
from covey.app.track_standby import Failed
from covey.app.web_ops import Ops
from covey.cards import project_proposal as cards
from covey.app import project_lead
from covey.core import butler_tool_specs, projects, topic_gist
from covey.core import project_proposals as proposals
from covey.core.checkpoint import TOPIC_GIST_RULE
from covey.core.jobs import upsert_job_record
from covey.core.state import read_object, write_json

A = "修 064 采购审批题包"
B = "优化 EnvReplay 的 Prompt"


def iso(seconds_ago=0.0) -> str:
    return datetime.datetime.fromtimestamp(time.time() - seconds_ago).astimezone().isoformat()


def envelope(value) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(value, ensure_ascii=False)}, ensure_ascii=False)


def text(card) -> str:
    return json.dumps(card, ensure_ascii=False)


class TidyCase(UpCase):
    """A Bridge with 专项 and 整理专项 (suggest) on, a fake small model and message sends that hand back a card's message id."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {proposals.ENV: "suggest"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tidy = ProjectTidy(self.app.runtime, self.app.messenger, self.app.voice, self.env)
        self.env.tidy = self.tidy
        self.messages = iter(f"om_tidy{n}" for n in range(1, 1000))
        self.app.messenger.send_message = mock.Mock(side_effect=self.send_message)
        self.app.messenger.patch_card = mock.Mock(return_value=True)
        self.asked = []  # (system, data) of each model call
        self.verdicts = []  # What the judge answers, in turn ({"suggestions": [...]} each).
        self.refuse = set()  # Titles whose backfill the model refuses.
        ask = mock.patch.object(sorter, "ask", side_effect=self.ask)
        ask.start()
        self.addCleanup(ask.stop)
        self.numbers = iter(range(400, 1000))
        self.jobs = iter(range(5000, 9000))
        self.tidy.link = lambda state, topic, **kw: f"https://applink.example/{topic[-6:]}"

    def send_message(self, profile, chat, words, key, *, voice, card=None, on_card=None, reply_to_message_id="", **kwargs):
        message = next(self.messages)
        if card is not None and on_card is not None:
            on_card(message)
        return True

    def ask(self, system, data, backend, ctx, limit):
        given = json.loads(data)
        self.asked.append((system, given))
        if system == sorter.GIST_SYSTEM:
            if given["title"] in self.refuse:
                raise Failed("model_error")
            return envelope({"essence": "本质：" + given["title"], "goal": "做完", "involves": "", "related": [], "changed": "没变"})
        return envelope(self.verdicts.pop(0) if self.verdicts else {"suggestions": []})

    def judged(self):
        return [given for system, given in self.asked if system == sorter.JUDGE_SYSTEM]

    def topic(self, title, *, words=(), gist=None, days_ago=0.0, project=None):
        """An ordinary topic of the owner's private chat with one done turn per word (its checkpoint's topic_gist ``gist``)."""
        topic = f"{next(self.numbers):x}".rjust(32, "a")
        fields = {"project": projects.project_field(project, projects.MEMBER)} if project else {}
        write_topic(self.state, topic_id=topic, chat_id=CHAT, harness_id="claude", topic_number=int(topic[-3:], 16) % 900 + 100,
                    origin={"title": title}, workspace_name="project", **fields)
        for word in words or (title,):
            self.turn(topic, word, gist=gist, created=iso(days_ago * 86400))
        return topic

    def turn(self, topic, words, *, gist=None, created=None):
        number = next(self.jobs)
        checkpoint = self.state / "checkpoints" / f"job-{number}.json"
        write_json(checkpoint, {"phase": "做完", **({"topic_gist": gist} if gist else {})}, ensure_ascii=False)
        upsert_job_record(self.state, f"om_{number}", message_id=f"om_{number}", job_id=number, chat_id=CHAT, topic_id=topic,
                          status="done", user_text=words, final_text=f"结论：{words}\n\n细节", checkpoint_path=str(checkpoint),
                          **({"created_at": created} if created else {}))

    def project(self, name, topics=()):
        record = projects.new_record(chat_id=CHAT, name=name, goal=name + "做完")
        projects.create(self.state, record)
        for topic in topics:
            projects.add_member(self.state, record["id"], topic)
            data = read_object(self.state / "topics" / topic / "topic.json")
            write_json(self.state / "topics" / topic / "topic.json",
                       {**data, "project": projects.project_field(record["id"], projects.MEMBER)}, ensure_ascii=False)
        return record["id"]

    def filler(self, count=5):
        return [self.topic(f"杂事 {n}") for n in range(count)]

    def click(self, value, message, **form):
        event = {"event_id": f"ev-{next(self.jobs)}", "message_id": message, "chat_id": CHAT, "operator_id": "ou_owner",
                 "action_value": value, **({"form_value": form} if form else {})}
        return self.tidy.claim(self.ctx, event, value, True)

    def sent_cards(self):
        return [call.kwargs.get("card") for call in self.app.messenger.send_message.call_args_list if call.kwargs.get("card")]

    def patched(self):
        return self.app.messenger.patch_card.call_args.args[2]


class GistTest(TidyCase):
    def test_a_turns_topic_gist_is_kept_and_an_essence_that_changed_calls_for_a_realtime_tidy(self):
        topic = self.topic(A, words=("先修一下 064 题包",), gist={"essence": A})
        self.tidy.tick(self.ctx)  # The first look only sets the cursor: the turn before it is not read.
        self.assertEqual(topic_gist.read(self.state, topic), {})
        self.turn(topic, "这题包的问题其实在 Prompt", gist={"本质": A, "现在的目标": "复现"})
        self.assertEqual(self.tidy.scan(self.state, CHAT, time.time()), [(topic, "turn")])  # The first essence is no change.
        self.assertEqual(topic_gist.read(self.state, topic)["essence"], A)
        self.turn(topic, "那就改 Prompt 吧", gist="本质：" + B + "\n和上次比变了什么：从修一个题包变成改 Prompt")
        fired = self.tidy.scan(self.state, CHAT, time.time())
        self.assertIn((topic, "essence"), fired)
        saved = topic_gist.read(self.state, topic)
        self.assertEqual((saved["essence"], [item["text"] for item in saved["essences"]]), (B, [A, B]))
        self.turn(topic, "接着改", gist={"essence": B + "（064 是第一个例子）"})
        self.assertNotIn((topic, "essence"), self.tidy.scan(self.state, CHAT, time.time()))  # Reworded, not changed.

    def test_the_changed_essence_is_judged_for_that_topic_and_nothing_with_fewer_than_five_topics(self):
        topic = self.topic(A, gist={"essence": A})
        self.tidy.tick(self.ctx)
        self.turn(topic, "改 Prompt", gist={"essence": B})
        self.tidy.tick(self.ctx)
        self.assertEqual(self.judged(), [])  # Two topics in the chat: too few.
        self.filler()
        self.tidy.counted = (0.0, 0)
        self.turn(topic, "再改", gist={"essence": A})
        self.tidy.tick(self.ctx)
        focus = self.judged()[-1]
        self.assertEqual(focus["topics"][focus["focus"] - 1]["gist"]["essence"], A)

    def test_a_new_topic_its_third_day_and_its_fifteenth_word_each_call_once(self):
        self.tidy.tick(self.ctx)
        fresh = self.topic("新开的话题")
        old = self.topic("老话题", days_ago=4)
        talky = self.topic("聊得多的话题", words=[f"第 {n} 句" for n in range(15)], days_ago=4)
        reasons = {}
        for topic, why in self.tidy.scan(self.state, CHAT, time.time()):
            reasons.setdefault(topic, set()).add(why)
        self.assertEqual(reasons[fresh], {"new", "turn"})
        self.assertEqual(reasons[old], {"age", "turn"})
        self.assertEqual(reasons[talky], {"age", "words", "turn"})
        self.turn(old, "又来一轮")
        self.assertEqual(self.tidy.scan(self.state, CHAT, time.time()), [(old, "turn")])  # Each turn the owner started.

    def test_the_turns_message_carries_the_rule_and_the_current_gist_only_with_the_switch_on(self):
        from covey.app.runs import Runs

        topic = self.topic(A)
        topic_gist.save(self.state, topic, {"essence": A, "goal": "复现"})
        ctx = SimpleNamespace(state_dir=self.state, topic_id=topic)
        job = SimpleNamespace(topic_id=topic, chat_id=CHAT)
        note = Runs._gist_note(ctx, job)
        self.assertIn(TOPIC_GIST_RULE, note)
        self.assertIn("本质：" + A, note)
        butler_job = SimpleNamespace(topic_id=upgrades.butler.butler_topic_id(CHAT), chat_id=CHAT)
        self.assertEqual(Runs._gist_note(ctx, butler_job), "")
        with mock.patch.dict(os.environ, {proposals.ENV: ""}):
            self.assertEqual(Runs._gist_note(ctx, job), "")


class JoinTest(TidyCase):
    def test_a_sure_join_makes_the_topic_the_projects_tells_the_lead_and_marks_it_and_after_leave_never_again(self):
        member = self.topic("EnvReplay Prompt 第一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        self.filler()
        topic = self.topic(A, gist={"essence": B})
        write_json(self.state / "topics" / topic / "topic.json",
                   {**read_object(self.state / "topics" / topic / "topic.json"), "root_id": "om_root"}, ensure_ascii=False)
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.95}]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "essence", time.time()), "joined")
        self.assertEqual(projects.topic_project(self.state, topic).id, pid)
        self.assertIn(topic, projects.read_record(self.state, pid)["threads"])
        self.assertEqual(projects.cells(self.state, CHAT)[topic], projects.lead_topic_id(CHAT, pid))
        receipts = [read_object(path) for path in project_lead.inbox_dir(self.state, pid).glob("*.json")]
        self.assertEqual([item["kind"] for item in receipts], [upgrades.JOINED])
        # Neither a topic card nor a status card: the line goes into the topic's thread as a card of its own.
        sent = self.app.messenger.send_message.call_args
        self.assertEqual(sent.kwargs["reply_to_message_id"], "om_root")
        self.assertIn("已归进专项「EnvReplay Prompt 优化」", text(sent.kwargs["card"]))
        self.assertIn(cards.LEAVE_KIND, text(sent.kwargs["card"]))
        self.click({"kind": cards.LEAVE_KIND, "topic_id": topic, "project_id": pid}, "om_topic_card")
        self.assertIsNone(projects.topic_project(self.state, topic))
        self.assertNotIn(topic, projects.read_record(self.state, pid)["threads"])
        self.assertTrue(proposals.never_auto(self.state, topic))
        self.assertIn(upgrades.LEFT, [read_object(path)["kind"] for path in project_lead.inbox_dir(self.state, pid).glob("*.json")])
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.99}]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "words", time.time()), "none")
        self.assertIsNone(projects.topic_project(self.state, topic))

    def test_the_status_card_gets_the_line_with_its_leave_button(self):
        from covey.app import status_cards

        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        topic = self.topic(A)
        write_json(status_cards.record_path(self.state, topic), {"card_id": "card-s", "message_id": "om_status", "sequence": 1,
                                                                   "shown": ["T1 · " + A, "做完", "13:01", "结论：好"]})
        self.assertTrue(self.tidy.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid), CHAT))
        card = self.put.call_args.args[2]
        self.assertIn("已归进专项「EnvReplay Prompt 优化」", text(card))
        self.assertIn(cards.LEAVE_KIND, text(card))
        self.tidy.leave(self.ctx, CHAT, topic, pid)
        self.assertNotIn("已归进", text(self.put.call_args.args[2]))

    def test_a_low_confidence_join_is_not_done(self):
        member = self.topic("一轮")
        self.project("EnvReplay Prompt 优化", [member])
        self.filler()
        topic = self.topic(A)
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "像", "confidence": 0.6}]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "new", time.time()), "none")
        self.assertIsNone(projects.topic_project(self.state, topic))


class AutoJoinTest(TidyCase):
    """自动收话题 (the owner, 2026-11-19): a project takes in the topics that belong to it as they go on, without a click — by
    the 话题要点's related topics, after each turn he started, and when the project is made; one 移出 keeps a topic out."""

    def number(self, topic):
        return read_object(self.state / "topics" / topic / "topic.json")["topic_number"]

    def gisted(self, title, gist):
        """A topic whose 话题要点 is ``gist`` already (as a scan of its turn keeps it)."""
        topic = self.topic(title)
        topic_gist.save(self.state, topic, gist, source="turn", job="x")
        return topic

    def test_a_topic_whose_gist_names_a_projects_topic_joins_without_the_model(self):
        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        topic = self.gisted(A, {"essence": B, "related": [f"T{self.number(member)}"]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "joined")
        self.assertEqual(self.judged(), [])  # The rule: no judge call, and not held back by the chat having few topics.
        self.assertEqual(projects.topic_project(self.state, topic).id, pid)
        joins = projects.read_record(self.state, pid)["joins"]
        self.assertEqual([(row["topic"], row["by"]) for row in joins], [(topic, "rule")])
        self.assertIn(f"T{self.number(member)}", joins[0]["why"])

    def test_each_owner_turn_is_judged_against_the_projects_once_a_gist_and_a_sure_join_is_done(self):
        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        topic = self.gisted(A, {"essence": A})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "none")
        self.assertEqual(len(self.judged()), 1)
        given = self.judged()[0]
        self.assertEqual([row["index"] for row in given["topics"]], [1])  # This topic alone, against the live projects.
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "same")  # Gist unchanged: no call.
        self.assertEqual(len(self.judged()), 1)
        topic_gist.save(self.state, topic, {"essence": B}, source="turn", job="x")
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.9}]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "joined")
        self.assertEqual(projects.topic_project(self.state, topic).id, pid)
        self.assertEqual(projects.read_record(self.state, pid)["joins"][-1]["by"], "tidy")

    def test_a_topic_taken_out_is_not_joined_by_the_rule_nor_after_its_turns(self):
        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        topic = self.gisted(A, {"essence": B, "related": [f"T{self.number(member)}"]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "joined")
        self.tidy.leave(self.ctx, CHAT, topic, pid)
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, topic, "turn", time.time()), "kept_out")
        self.assertIsNone(projects.topic_project(self.state, topic))
        self.assertEqual(self.judged(), [])
        record = projects.read_record(self.state, pid)
        self.assertTrue(record["joins"][-1]["left_at"])

    def test_a_project_just_made_takes_in_the_recent_topics_the_judge_is_sure_of(self):
        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        other = self.project("别的专项", [self.topic("别的一轮")])
        sure, unsure = self.gisted(A, {"essence": B}), self.gisted("杂事", {"essence": "杂事"})
        self.verdicts.append({"suggestions": [
            {"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.95},
            {"kind": "join", "topic": 2, "project": 1, "why": "沾边", "confidence": 0.5}]})
        items = []
        original = sorter.material
        with mock.patch.object(sorter, "material", side_effect=lambda *a, **k: items.append(a[2]) or original(*a, **k)):
            proposals.queue_sweep(self.state, pid)
            self.tidy.drain(self.ctx, CHAT, time.time())
        given = self.judged()[-1]
        self.assertEqual([row["name"] for row in given["projects"]], ["EnvReplay Prompt 优化"])  # This project alone.
        self.assertNotIn(member, items)  # Topics already in a project are not read.
        self.assertEqual(set(items), {unsure, sure})
        self.assertEqual(projects.topic_project(self.state, sure).id, pid)
        self.assertIsNone(projects.topic_project(self.state, unsure))  # Not sure enough: left alone, nothing proposed.
        self.assertNotIn(other, [row["name"] for row in given["projects"]])

    def test_the_projects_card_lists_who_took_each_topic_in_with_a_leave_button_while_it_is_in(self):
        from covey.app import project_cards

        member = self.topic("一轮")
        pid = self.project("EnvReplay Prompt 优化", [member])
        topic = self.topic(A)
        self.assertTrue(self.tidy.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid, why="同一件事"), CHAT))
        lines = project_cards.join_lines(self.state, projects.read_record(self.state, pid))
        self.assertEqual(len(lines), 1)
        self.assertIn("整理专项归入", lines[0][0])
        self.assertIn("同一件事", lines[0][0])
        self.assertEqual(lines[0][1], topic)
        self.tidy.leave(self.ctx, CHAT, topic, pid)
        lines = project_cards.join_lines(self.state, projects.read_record(self.state, pid))
        self.assertIn("已移出", lines[0][0])
        self.assertEqual(lines[0][1], "")


class ProposalTest(TidyCase):
    def propose(self, topics, name="EnvReplay Prompt 优化"):
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": list(range(1, len(topics) + 1)), "name": name,
                                               "goal": "新旧 Prompt 对率可比", "why": "都在改 Prompt", "confidence": 0.9}]})
        return self.tidy.realtime(self.ctx, CHAT, topics[0], "new", time.time())

    def setUp(self):
        super().setUp()
        self.filler()
        self.pair = [self.topic(A, gist={"essence": B}), self.topic("优化 Bootstrap 下一环节", gist={"essence": B})]
        self.tidy.recent_topics = lambda state, chat, now, **kw: list(self.pair)  # The judge sees the two in this order.

    def card_and_record(self):
        record = proposals.proposals(self.state, None)[-1]
        return record, record["card"]["message_id"]

    def test_the_first_proposal_card_says_what_a_project_is_and_has_the_four_buttons(self):
        self.assertEqual(self.propose(self.pair), "proposed")
        card = text(self.sent_cards()[-1])
        for word in ("EnvReplay Prompt 优化", cards.INTRO, cards.AFTER, "建专项", "不管了", "tidy_name"):
            self.assertIn(word, card)
        for word in ("改个名再建", "以后别提议"):  # The owner, 2026-11-18: 4 个按钮太冗余了.
            self.assertNotIn(word, card)
        self.assertEqual(self.propose(self.pair), "open")  # One open proposal about a topic at a time.
        self.assertTrue(proposals.prefs(self.state)["first_sent"])

    def test_create_gathers_the_topics_into_a_project_and_its_lead_gets_them(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, message), "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual((made["name"], made["origin"]["kind"], made["threads"]), ("EnvReplay Prompt 优化", "tidy", self.pair))
        self.assertTrue(all(name in made["steps"] for name in upgrades.GATHER_STEPS))
        start = [read_object(path) for path in project_lead.inbox_dir(self.state, made["id"]).glob("*.json")]
        self.assertEqual(start[0]["kind"], "start")
        self.assertIn("主人同意把这几个已有的话题建成专项", start[0]["text"])
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.DONE)
        self.assertIn("已建专项「EnvReplay Prompt 优化」", text(self.patched()))
        self.assertEqual(proposals.prefs(self.state)["accepted_in_row"], 1)

    def test_rename_then_create_keeps_the_owners_name_out_of_reach_of_the_tidy(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.RENAME_KIND, "proposal": record["id"]}, message), "tidy_rename")
        self.assertIn("tidy_rename", text(self.patched()))
        self.assertEqual(self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, message,
                                    name="Prompt 提效 2026-11-18", goal="对率可比"), "tidy_named")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual((made["name"], made["name_by"]), ("Prompt 提效", "owner"))
        self.assertEqual(proposals.prefs(self.state)["accepted_in_row"], 0)
        rename = {"suggestions": [{"kind": "rename", "project": 1, "name": "别的名字", "why": "", "confidence": 0.9}]}
        self.assertEqual(sorter.parse(json.dumps(rename), [made], []), [])

    def test_the_proposal_card_links_each_topic_and_says_what_it_is_about(self):
        """The owner, 2026-11-18: 我都不知道 T252 在哪，这里就写了一个 19天118轮…这个是啥 — each topic opens from the card and
        reads as its 本质, with how long it has gone on."""
        for topic in self.pair:
            topic_gist.save(self.state, topic, topic_gist.parse({"essence": B}), source="turn", job="", now=time.time())
        self.propose(self.pair)
        card = text(self.sent_cards()[-1])
        record, _ = self.card_and_record()
        for topic in self.pair:
            self.assertIn(f"https://applink.example/{topic[-6:]}", card)
        self.assertIn("这 2 个话题像是一件事", card)
        self.assertIn(B, card)  # Their 本质, not their first titles' words alone.
        self.assertIn("为什么提：都在改 Prompt", card)
        self.assertEqual([row["essence"] for row in record["view"]["rows"]], [B, B])

    def test_one_long_topic_without_a_gist_is_read_whole_before_it_is_judged_and_alone_it_is_no_project(self):
        """A topic grown long (reason age) with no 话题要点 yet is read whole first: the judge sees its 本质 and so does the
        card, which speaks of one topic, not of 这几个."""
        alone = self.topic("阅读这个session，继续我们未完成的事情", words=[f"第 {n} 句" for n in range(3)], days_ago=19)
        self.tidy.recent_topics = lambda state, chat, now, **kw: [alone]
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": [1], "name": "账号额度运行台", "goal": "额度看得准",
                                               "why": "一直在改运行台", "confidence": 0.8}]})
        # Read whole first, so the judge sees what it is about; and a project of one topic is no project (the owner,
        # 2026-11-18: 只有一个话题的不提专项), so nothing is proposed.
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, alone, "age", time.time()), "none")
        systems = [system for system, _ in self.asked]
        self.assertEqual(systems[:2], [sorter.GIST_SYSTEM, sorter.JUDGE_SYSTEM])
        self.assertEqual(self.judged()[0]["topics"][0]["gist"]["essence"], "阅读这个session，继续我们未完成的事情")
        self.assertEqual(self.sent_cards(), [])

    def test_a_kept_one_topic_view_still_draws(self):
        """A proposal card of one topic sent before 2026-11-18 is redrawn on a click as before."""
        row = {"label": "T252", "title": "阅读这个session", "link": "", "essence": "运行台", "now": "做完", "days": 19, "turns": 3}
        card = text(cards.proposal_card({"proposal": "1" * 16, "rows": [row], "name": "运行台", "state": "declined"}))
        self.assertIn("这个话题推了很久", card)
        self.assertIn("19 天 · 3 轮", card)

    def test_the_owner_unticks_a_topic_adds_one_by_its_number_and_renames_before_building(self):
        """The owner, 2026-11-18: 在页面增删一下话题…让人微操一波 — the card's select and T number box decide the topics."""
        third = self.topic("顺手查的一件事")
        self.tidy.recent_topics = lambda state, chat, now, **kw: [*self.pair, third]
        self.propose(self.pair)
        record, message = self.card_and_record()
        choices = record["view"]["choices"]
        self.assertEqual([choice["topic"] for choice in choices][:3], [*self.pair, third])
        self.assertEqual([choice["chosen"] for choice in choices][:3], [True, True, False])
        number = read_object(self.state / "topics" / third / "topic.json")["topic_number"]
        with mock.patch.object(tidy_module.topic_links, "topic_numbers", return_value={number: third}):
            self.assertEqual(self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, message,
                                        topics=["0"], more=f"T{number} T99999", name="Prompt 提效"), "tidy_named")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual((made["name"], made["threads"]), ("Prompt 提效", [self.pair[0], third]))
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], [self.pair[0], third])
        self.assertIn("T99999 不是你最近的话题", text(self.patched()))

    def test_the_name_left_as_it_was_counts_as_a_plain_build_and_none_ticked_builds_nothing(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, message, topics=[], more="",
                                    name="EnvReplay Prompt 优化"), "tidy_no_topics")
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.OPEN)
        self.assertEqual(self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, message, topics=["0", "1"],
                                    name="EnvReplay Prompt 优化"), "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertNotEqual(made.get("name_by"), "owner")
        self.assertEqual(proposals.prefs(self.state)["accepted_in_row"], 1)

    def test_decline_keeps_the_same_proposal_away_until_two_more_topics_and_never_stops_them(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.DECLINE_KIND, "proposal": record["id"]}, message), "tidy_decline")
        self.assertIn("tidy_lesson", text(self.patched()))  # The reason form under it.
        self.assertEqual(self.click({"kind": cards.LESSON_KIND, "proposal": record["id"]}, message,
                                    why="题包和 Prompt 是两件事"), "tidy_lesson")
        self.assertEqual(proposals.lessons(self.state), ["题包和 Prompt 是两件事"])
        self.assertEqual(self.propose(self.pair), "none")
        self.assertEqual(self.judged()[-1]["lessons"], ["题包和 Prompt 是两件事"])
        self.pair += [self.topic("Prompt 第三轮", gist={"essence": B}), self.topic("Prompt 第四轮", gist={"essence": B})]
        self.assertEqual(self.propose(self.pair), "proposed")
        record, message = self.card_and_record()
        self.click({"kind": cards.NEVER_KIND, "proposal": record["id"]}, message)
        self.assertTrue(proposals.prefs(self.state)["no_proposals"])
        self.pair.append(self.topic("Prompt 第五轮"))
        self.pair.append(self.topic("Prompt 第六轮"))
        self.assertEqual(self.propose(self.pair), "none")

    def test_five_creates_in_a_row_ask_once_whether_to_create_directly_from_then_on(self):
        proposals.set_pref(self.state, accepted_in_row=5)
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertIn(cards.ASK_AUTO.format(5), text(self.sent_cards()[-1]))
        self.assertEqual(self.click({"kind": cards.AUTO_KIND, "proposal": record["id"]}, message), "tidy_auto")
        self.assertTrue(proposals.prefs(self.state)["auto_create"])
        others = [self.topic("又一件"), self.topic("又一件的下一步")]
        self.pair = others
        self.assertEqual(self.propose(others, name="又一件事"), "created")
        self.assertEqual(len([record for record in projects.records(self.state) if (record.get("origin") or {}).get("kind") == "tidy"]), 1)


class TidyCardTest(TidyCase):
    def test_a_whole_tidy_reads_the_topics_without_a_gist_skips_the_one_refused_and_lists_its_suggestions(self):
        member = self.topic("回流周报 第 1 周")
        pid = self.project("回流周报", [member])
        topics = [self.topic(f"老话题 {n}") for n in range(5)]
        self.refuse.add("老话题 2")
        self.tidy.recent_topics = lambda state, chat, now, **kw: [*topics, member]  # The judge's indexes: these, in this order.
        self.verdicts.append({"suggestions": [
            {"kind": "create", "topics": [1, 2], "name": "推理网关稳定", "goal": "不失联", "why": "同一个网关", "confidence": 0.8},
            {"kind": "fold", "project": 1, "why": "本周的已交", "confidence": 0.9},
            {"kind": "rename", "project": 1, "name": "回流周报汇报", "why": "", "confidence": 0.7}]})
        self.tidy.start(self.ctx, origin="test", wait=True)
        backfills = [given for system, given in self.asked if system == sorter.GIST_SYSTEM]
        self.assertEqual(len(backfills), 6)  # One call a topic.
        self.assertEqual(topic_gist.read(self.state, topics[2])["unreadable"]["reason"], "model_error")
        judged = self.judged()[-1]
        self.assertEqual(len(judged["topics"]), 5)  # The refused one sat out, the others went on.
        view = read_object(next((proposals.folder(self.state) / "runs").glob("*.json")))["view"]
        self.assertEqual(view["state"], "done")
        # Which one and why (the owner, 2026-11-18: “读不了”的话题，要写清是哪几个、为什么读不了).
        self.assertEqual(len(view["skipped"]), 1)
        self.assertIn("1 个话题读不了，没参与这次整理：", view["skipped"][0])
        self.assertIn("老话题 2（模型调用出错）", view["skipped"][0])
        self.assertIn(f"https://applink.example/{topics[2][-6:]}", view["skipped"][0])
        self.assertTrue(any("已收起「回流周报」" in line for line in view["did"]))
        self.assertTrue(projects.folded(projects.read_record(self.state, pid)))
        self.assertEqual([row["state"] for row in view["rows"]], ["open"])  # The rename; the new project has its own card.
        self.assertEqual(view["proposed"], ["推理网关稳定"])
        proposal = text(self.sent_cards()[-1])
        for topic in topics[:2]:
            self.assertIn(f"https://applink.example/{topic[-6:]}", proposal)
        self.assertIn("建专项", proposal)
        card = text(cards.tidy_card(view))
        for word in ("整理专项", "新专项的提议", "「推理网关稳定」", "好", "不用", "已经做了", "最近 7 天动过的话题"):
            self.assertIn(word, card)
        self.assertNotIn("全部照办", card)
        self.assertIn("正在读", text(self.app.messenger.patch_card.call_args_list[0].args[2]))

    def test_rows_list_every_topic_and_are_confirmed_one_by_one_and_no_project_of_one_topic_or_no_end(self):
        """The owner, 2026-11-18: 不准用“等”字省略; 只有一个话题的不提专项; 去掉「全部照办」，每条逐条确认."""
        self.filler(3)
        one, two, three = self.topic("网关失联"), self.topic("网关扩容"), self.topic("头像")
        members = [self.topic(f"周报 {n}") for n in range(8)]
        pid = self.project("回流周报", members)
        self.verdicts.append({"suggestions": [
            {"kind": "create", "topics": [1, 2], "name": "推理网关稳定", "goal": "一周不失联", "why": "", "confidence": 0.8},
            {"kind": "create", "topics": [3], "name": "做头像", "goal": "换上新头像", "why": "", "confidence": 0.9},
            {"kind": "create", "topics": [1, 3], "name": "值守", "goal": "", "why": "", "confidence": 0.9},
            {"kind": "rename", "project": 1, "name": "回流周报汇报", "why": "", "confidence": 0.7}]})
        self.tidy.recent_topics = lambda state, chat, now, **kw: [one, two, three, *members]
        record = self.tidy.whole(self.ctx, CHAT, "run-1", "test", "")
        view = record["view"]
        self.assertEqual(view["proposed"], ["推理网关稳定"])  # One topic, or no end to reach: no project.
        self.assertEqual(len(view["rows"]), 1)
        line = view["rows"][0]["line"]
        self.assertNotIn("等", line)
        card = text(cards.tidy_card(view))
        self.assertNotIn("全部照办", card)
        self.assertEqual(self.click({"kind": cards.OK_KIND, "run": "run-1", "proposal": view["rows"][0]["proposal"]},
                                    record["message_id"]), "tidy_ok")
        self.assertEqual(projects.read_record(self.state, pid)["name"], "回流周报汇报")

    def test_a_card_sent_before_keeps_its_all_button_working(self):
        self.filler(3)
        one, two = self.topic("网关失联"), self.topic("网关扩容")
        pid = self.project("回流周报", [self.topic("周报 1")])
        self.verdicts.append({"suggestions": [{"kind": "rename", "project": 1, "name": "回流周报汇报", "why": "", "confidence": 0.7}]})
        self.tidy.recent_topics = lambda state, chat, now, **kw: [one, two]
        record = self.tidy.whole(self.ctx, CHAT, "run-2", "test", "")
        self.assertEqual(self.click({"kind": cards.ALL_KIND, "run": "run-2"}, record["message_id"]), "tidy_all")
        self.assertEqual(projects.read_record(self.state, pid)["name"], "回流周报汇报")

    def test_one_tidy_at_a_time_and_the_tool_and_the_web_page_say_where_it_is(self):
        self.filler()
        self.tidy.busy.acquire()
        self.addCleanup(self.tidy.busy.release)
        self.tidy.progress = {"done": 12, "total": 40}
        answer = upgrades.projects_tidy(self.env, SimpleNamespace(args={}, id="r9", job_key="main-job"))
        self.assertEqual(answer["text"], "正在整理（12/40）")
        self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o1", "payload": {"scope": ""}}), ("done", "正在整理（12/40）"))
        self.assertIn("projects_tidy", [tool["name"] for tool in butler_tool_specs.listed({"COVEY_PROJECTS": "1", proposals.ENV: "suggest"})])


class PendingTest(TidyCase):
    """The owner, 2026-11-18 23:17-23:19: 怎么可能没有专项呢 / 为啥现在我点了整理专项说不需要呢 — a proposal still waiting is
    said so, decided right on the 看板, and the three places say the same."""

    def setUp(self):
        super().setUp()
        self.filler(3)
        self.one, self.two = self.topic("修 064 采购审批题包"), self.topic("优化 Bootstrap 下一环节")
        self.tidy.recent_topics = lambda state, chat, now, **kw: [self.one, self.two]
        self.suggest = {"suggestions": [{"kind": "create", "topics": [1, 2], "name": "题包提示词优化", "goal": "合入 !248",
                                         "why": "同一条流水线", "confidence": 0.8}]}

    def test_a_suggestion_already_waiting_is_named_not_called_nothing_to_change(self):
        self.verdicts.append(self.suggest)
        first = self.tidy.whole(self.ctx, CHAT, "run-a", "test", "")
        self.assertEqual(first["view"]["proposed"], ["题包提示词优化"])
        self.verdicts.append(self.suggest)
        second = self.tidy.whole(self.ctx, CHAT, "run-b", "test", "")
        self.assertEqual(second["view"].get("pending"), ["题包提示词优化"])
        card = text(cards.tidy_card(second["view"]))
        self.assertIn("还在等你定", card)
        self.assertNotIn("没有要改的", card)

    def test_a_proposal_of_one_topic_left_from_before_expires_at_the_next_tidy(self):
        alone = proposals.new_proposal(proposals.CREATE, topics=[self.one], name="单独一个", goal="做完", source="realtime")
        proposals.put_proposal(self.state, alone)
        self.tidy.whole(self.ctx, CHAT, "run-c", "test", "")
        self.assertEqual(proposals.read_proposal(self.state, alone["id"])["state"], proposals.EXPIRED)

    def test_build_on_the_board_creates_it_redraws_its_own_card_and_the_board(self):
        self.verdicts.append(self.suggest)
        self.tidy.whole(self.ctx, CHAT, "run-d", "test", "")
        record = [r for r in proposals.proposals(self.state, [proposals.OPEN]) if r.get("name") == "题包提示词优化"][0]
        own = record["card"]["message_id"]
        redrawn = []
        self.tidy.handed["board"] = lambda ctx, message: redrawn.append(message)
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"], "from": "board"}, "om_board9"),
                         "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual(made["threads"], [self.one, self.two])
        self.assertEqual(redrawn, ["om_board9"])
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], own)  # Its own card, not the board.
        answer = upgrades.projects_list(self.env, SimpleNamespace(args={}, id="r1", job_key="main-job"))
        self.assertIn("题包提示词优化", answer["text"])

    def test_the_board_lists_the_waiting_proposals_with_build_and_not_now(self):
        from covey.cards.board import board_card
        self.verdicts.append(self.suggest)
        self.tidy.whole(self.ctx, CHAT, "run-e", "test", "")
        record = [r for r in proposals.proposals(self.state, [proposals.OPEN]) if r.get("name") == "题包提示词优化"][0]
        pending = [{"id": record["id"], "name": record["name"], "goal": record["goal"], "why": "",
                    "rows": [{"topic": self.one, "label": "T1", "title": "修 064", "url": "https://applink.example/1"},
                             {"topic": self.two, "label": "T2", "title": "Bootstrap", "url": ""}]}]
        card = text(board_card({"waiting": [], "running": [], "done": [], "stale": [], "tidy": {}, "projects": [],
                                "pending": pending, "proposals": 1}))
        for word in ("专项", "0 个", "1 个提议等你定", "「题包提示词优化」", "https://applink.example/1", "建专项", "不管了",
                     cards.CREATE_KIND, cards.DECLINE_KIND, '"from": "board"'):
            self.assertIn(word, card)
        self.assertNotIn("往上翻", card)


class FollowTest(TidyCase):
    """The owner, 2026-11-19: 已经归进专项的话题，提议卡不会失效 — four cards stayed on the 看板 after T519、T525、T531 joined a project,
    two of them nearly the same. A proposal follows the joins, however they happen, and a new one does not repeat a waiting one."""

    def setUp(self):
        super().setUp()
        self.filler(3)
        self.trio = [self.topic("C队重启后自启"), self.topic("开发机重启"), self.topic("自启脚本")]
        self.tidy.recent_topics = lambda state, chat, now, **kw: list(self.trio)
        self.pid = self.project("Covey 体验与节点改进", [self.topic("节点第一期")])

    def suggest(self, picks, name):
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": picks, "name": name, "goal": "自启恢复",
                                               "why": "都在修重启", "confidence": 0.9}]})

    def open_card(self, picks=(1, 2, 3), name="C队重启后自启恢复"):
        self.suggest(list(picks), name)
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, self.trio[0], "new", time.time()), "proposed")
        return proposals.waiting(self.state)[0]

    def test_a_topic_the_lead_takes_in_leaves_the_card_with_the_rest_and_the_card_says_where_it_went(self):
        record = self.open_card()
        upgrades.join_topic(self.state, self.pid, self.trio[2], by="lead")
        self.assertEqual(self.tidy.follow(self.ctx, CHAT), 1)
        now = proposals.read_proposal(self.state, record["id"])
        self.assertEqual((now["state"], now["topics"]), (proposals.OPEN, self.trio[:2]))
        card = text(self.patched())
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], record["card"]["message_id"])
        for word in ("已归进专项「Covey 体验与节点改进」", "只剩下面 2 个话题", "建专项"):
            self.assertIn(word, card)
        self.assertEqual(self.tidy.follow(self.ctx, CHAT), 0)  # Nothing new: the card is not written again.

    def test_too_few_left_ends_it_off_the_board_the_list_and_the_tidy_card(self):
        record = self.open_card((1, 2), "C队开发机重启后自启修复")
        upgrades.join_topic(self.state, self.pid, self.trio[1], by="tidy")
        self.assertEqual(self.tidy.follow(self.ctx, CHAT), 1)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.EXPIRED)
        card = text(self.patched())
        for word in ("已过时", "已归进专项「Covey 体验与节点改进」", "成不了专项", "这条不再提"):
            self.assertIn(word, card)
        self.assertNotIn(cards.CREATE_KIND, card)
        self.assertEqual(proposals.waiting(self.state), [])
        answer = upgrades.projects_list(self.env, SimpleNamespace(args={}, id="r1", job_key="main-job"))
        self.assertNotIn("C队开发机重启后自启修复", answer["text"])
        self.assertNotIn("等主人定", answer["text"])
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, record["card"]["message_id"]),
                         "tidy_decided")  # A click on the card from before builds nothing.

    def test_a_new_proposal_inside_a_waiting_one_is_not_sent_and_one_with_more_goes_onto_its_card(self):
        record = self.open_card((1, 2))
        sent = len(self.sent_cards())
        self.assertEqual(self.tidy.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio[1::-1], name="C队开发机重启后自启修复")), "")
        self.assertEqual(self.tidy.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio, name="C队重启后自启恢复")), "merged")
        self.assertEqual(len(self.sent_cards()), sent)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio)
        self.assertEqual(len(proposals.waiting(self.state)), 1)
        self.assertIn("也是这件事，补进来了", text(self.patched()))

    def test_two_cards_left_with_the_same_topics_keep_only_the_newer(self):
        """The owner's four cards: 「C队重启后自启恢复」 (T525、T526、T524) and the newer 「C队开发机重启后自启修复」 (T524、T526)."""
        older = proposals.new_proposal(proposals.CREATE, topics=self.trio, name="C队重启后自启恢复", now=time.time() - 60)
        newer = proposals.new_proposal(proposals.CREATE, topics=self.trio[:2], name="C队开发机重启后自启修复")
        for record, message in ((older, "om_old"), (newer, "om_new")):
            proposals.put_proposal(self.state, {**record, "card": {"message_id": message, "chat_id": CHAT}})
        upgrades.join_topic(self.state, self.pid, self.trio[2], by="lead")
        self.assertEqual(self.tidy.follow(self.ctx, CHAT), 2)
        self.assertEqual(proposals.read_proposal(self.state, older["id"])["state"], proposals.EXPIRED)
        self.assertEqual(proposals.read_proposal(self.state, newer["id"])["state"], proposals.OPEN)
        self.assertEqual([record["id"] for record in proposals.waiting(self.state)], [newer["id"]])
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], "om_old")
        self.assertIn("和提议「C队开发机重启后自启修复」是同一件事", text(self.patched()))

    def test_a_proposal_mostly_of_a_projects_topics_is_not_made(self):
        for topic in self.trio[1:]:
            upgrades.join_topic(self.state, self.pid, topic, by="rule")
        self.assertEqual(self.tidy.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio, name="C队重启后自启恢复")), "")
        self.assertEqual(proposals.proposals(self.state, [proposals.OPEN]), [])

    def test_each_tick_follows_and_a_whole_tidy_names_the_one_it_merged_into_as_waiting(self):
        record = self.open_card((1, 2))
        self.suggest([1, 2, 3], "C队重启后自启恢复")
        view = self.tidy.whole(self.ctx, CHAT, "run-f", "test", "")["view"]
        self.assertEqual(view.get("pending"), ["C队重启后自启恢复"])
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio)
        upgrades.join_topic(self.state, self.pid, self.trio[0], by="lead")
        with mock.patch.object(self.tidy, "scan", return_value=[]), mock.patch.object(self.tidy, "scheduled"):
            self.tidy.tick(self.ctx)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio[1:])


class WhyUnreadTest(UpCase):
    def test_a_refusal_and_a_topic_too_long_are_told_apart_from_other_exits(self):
        """The owner, 2026-11-18: “读不了”的话题，要写清为什么 — Claude Code says a refusal only on stdout."""
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 stop_reason=refusal")), "refused")
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 Prompt is too long")), "too_long")
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 boom")), "exit_nonzero")
        self.assertEqual(sorter.why_failed(Failed("timeout")), "timeout")
        for reason in ("refused", "too_long", "exit_nonzero", "timeout", "model_error", "empty", "redacted"):
            self.assertIn(reason, tidy_module.UNREADABLE_WORDS)


class MomentTest(TidyCase):
    def test_the_digest_times_start_one_whole_tidy_each(self):
        self.filler()
        self.tidy.tick(self.ctx)  # The cursor.
        clock = time.localtime(time.time() - 60)
        with mock.patch.dict(os.environ, {"COVEY_DIGEST_TIMES": f"{clock.tm_hour:02d}:{clock.tm_min:02d}"}):
            self.tidy.tick(self.ctx)
            self.tidy.tick(self.ctx)
        runs = list((proposals.folder(self.state) / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(read_object(runs[0])["origin"], "scheduled")
        self.assertFalse(self.tidy.busy.locked())

    def test_the_butlers_topic_card_is_drawn_again_with_the_line(self):
        from covey.app import butler_topics

        topic = self.topic(A)
        request = "req-1"
        data = read_object(self.state / "topics" / topic / "topic.json")
        write_json(self.state / "topics" / topic / "topic.json", {**data, "origin": {"title": A, "request": request}},
                   ensure_ascii=False)
        path = butler_topics.record_path(self.state, SimpleNamespace(id=request))
        write_json(path, {"topic_id": topic, "card_id": "card-topic", "title": A, "sequence": 2}, ensure_ascii=False)
        pid = self.project("EnvReplay Prompt 优化", [self.topic("一轮")])
        self.tidy.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid), CHAT)
        call = next(call for call in self.put.call_args_list if call.args[1] == "card-topic")
        self.assertIn("已归进专项「EnvReplay Prompt 优化」", text(call.args[2]))
        self.assertEqual(call.kwargs["sequence"], 3)

    def test_a_gist_copied_with_its_labels_reads_the_same(self):
        self.assertEqual(topic_gist.parse({"essence": "本质：" + B, "goal": "现在的目标：跑 90 次"})["goal"], "跑 90 次")
        self.assertEqual(topic_gist.parse({"本质": "本质：" + B})["essence"], B)


class OffTest(TidyCase):
    def test_with_the_switch_off_nothing_is_read_judged_listed_or_pushed(self):
        topic = self.topic(A, gist={"essence": A})
        for environ in ({proposals.ENV: ""}, {proposals.ENV: "suggest", projects.ENV: ""}, {proposals.ENV: "yes"}):
            with mock.patch.dict(os.environ, environ):
                self.assertFalse(proposals.enabled())
                self.assertEqual(self.tidy.threads(), ())
                self.tidy.tick(self.ctx)
                self.assertEqual(proposals.load(self.state).get("cursor"), None)
                self.assertEqual(self.tidy.start(self.ctx), tidy_module.OFF_WORDS)
                self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o2", "payload": {}})[0], "failed")
                self.assertNotIn("projects_tidy", [tool["name"] for tool in butler_tool_specs.listed(dict(os.environ))])
                from covey.app.web_sync import Collector
                self.assertIsNone(Collector.gist(self.state, topic))
        self.assertEqual(self.asked, [])

    def test_the_code_default_is_off_and_install_writes_suggest(self):
        from covey.service import installer

        with mock.patch.dict(os.environ, {proposals.ENV: "", projects.ENV: ""}):
            self.assertEqual(proposals.mode(), proposals.OFF)
        ins = SimpleNamespace(env={"COVEY_PROJECT_TIDY": "off"})
        installer.Installer.default_switches(ins)
        self.assertEqual(ins.env, {**installer.INSTALL_SWITCHES, "COVEY_PROJECT_TIDY": "off"})
        ins = SimpleNamespace(env={})
        installer.Installer.default_switches(ins)
        self.assertEqual(ins.env, installer.INSTALL_SWITCHES)

    def test_the_patrol_reads_the_tidys_proposals_instead_of_its_own_word_match(self):
        from covey.app import patrol_rules

        one, two = self.topic("网关失联"), self.topic("网关失联又来")
        made = proposals.new_proposal("create", topics=[one, two], name="推理网关稳定")
        proposals.put_proposal(self.state, made)
        found = patrol_rules.find(patrol_rules.look(self.state, CHAT, time.time()))
        groups = [item for item in found if item.kind == "group"]
        self.assertEqual([item.key for item in groups], [f"group:{made['id'][:10]}"])
        with mock.patch.dict(os.environ, {proposals.ENV: ""}):
            found = patrol_rules.find(patrol_rules.look(self.state, CHAT, time.time()))
            self.assertFalse(any(item.key == f"group:{made['id'][:10]}" for item in found))


class NameTest(TidyCase):
    def test_a_name_is_the_essence_without_numbers_or_dates_at_most_twelve(self):
        self.assertEqual(proposals.clean_name("T488 Bootstrap 题包提效 2026-11-18"), "Bootstrap 题包提效")
        self.assertLessEqual(proposals.name_width(proposals.clean_name("一个非常非常非常非常长的专项名字超过十二个字")), 12)
        self.assertEqual(Path(proposals.state_path(self.state)).parent.name, "tidy")


class TidyModelTest(UpCase):
    """整理 reads whole conversations and names what a matter really is: Sonnet by default, not the small judge's Haiku."""

    def test_the_model_is_sonnet_unless_set(self):
        from covey.core import project_proposals as proposals
        self.assertEqual("sonnet", proposals.model({}))
        self.assertEqual("opus", proposals.model({"COVEY_PROJECT_TIDY_MODEL": "opus"}))
        self.assertEqual("sonnet", proposals.model({"COVEY_PROJECT_TIDY_MODEL": "bad model; rm"}))

    def test_the_call_asks_that_model(self):
        seen = {}

        def argv(backend, ctx, flag, **kwargs):
            seen.update(kwargs)
            return ["claude"]

        ctx = SimpleNamespace(state_dir=self.state)
        with mock.patch.dict(os.environ, {"COVEY_PROJECT_TIDY_MODEL": "opus"}), \
                mock.patch.object(sorter.track_judge, "claude_argv", argv), \
                mock.patch.object(sorter.track_judge, "run", return_value="{}"):
            sorter.ask("system", "data", SimpleNamespace(), ctx, 5)
        self.assertEqual("opus", seen.get("model"))

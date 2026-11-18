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
        self.assertEqual(self.tidy.scan(self.state, CHAT, time.time()), [])  # The first essence is no change.
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
        self.assertEqual(reasons[fresh], {"new"})
        self.assertEqual(reasons[old], {"age"})
        self.assertEqual(reasons[talky], {"age", "words"})
        self.turn(old, "又来一轮")
        self.assertEqual(self.tidy.scan(self.state, CHAT, time.time()), [])

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
        for word in ("EnvReplay Prompt 优化", cards.INTRO, cards.AFTER, "建专项", "暂不考虑", "tidy_name"):
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

    def test_one_long_topic_without_a_gist_is_read_whole_before_it_is_judged_and_its_card_says_so(self):
        """A topic grown long (reason age) with no 话题要点 yet is read whole first: the judge sees its 本质 and so does the
        card, which speaks of one topic, not of 这几个."""
        alone = self.topic("阅读这个session，继续我们未完成的事情", words=[f"第 {n} 句" for n in range(3)], days_ago=19)
        self.tidy.recent_topics = lambda state, chat, now, **kw: [alone]
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": [1], "name": "账号额度运行台", "goal": "额度看得准",
                                               "why": "一直在改运行台", "confidence": 0.8}]})
        self.assertEqual(self.tidy.realtime(self.ctx, CHAT, alone, "age", time.time()), "proposed")
        systems = [system for system, _ in self.asked]
        self.assertEqual(systems[:2], [sorter.GIST_SYSTEM, sorter.JUDGE_SYSTEM])
        self.assertEqual(self.judged()[0]["topics"][0]["gist"]["essence"], "阅读这个session，继续我们未完成的事情")
        card = text(self.sent_cards()[-1])
        self.assertIn("这个话题推了很久", card)
        self.assertIn("19 天 · 3 轮", card)
        self.assertIn("在做：阅读这个session", card)
        self.assertIn("现在：做完", card)  # Its newest checkpoint's phase.
        self.assertNotIn("这几个话题", card)

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
        self.assertIn("1 个话题读不了，没参与这次整理", view["skipped"])
        self.assertTrue(any("已收起「回流周报」" in line for line in view["did"]))
        self.assertTrue(projects.folded(projects.read_record(self.state, pid)))
        self.assertEqual([row["state"] for row in view["rows"]], ["open", "open"])
        card = text(cards.tidy_card(view))
        for word in ("整理专项", "新建「推理网关稳定」", "好", "不用", "全部照办", "已经做了"):
            self.assertIn(word, card)
        self.assertIn("正在读", text(self.app.messenger.patch_card.call_args_list[0].args[2]))

    def test_ok_no_and_all_on_the_card(self):
        self.filler(3)
        one, two = self.topic("网关失联"), self.topic("网关扩容")
        three = self.topic("头像")
        self.verdicts.append({"suggestions": [
            {"kind": "create", "topics": [1, 2], "name": "推理网关稳定", "goal": "", "why": "", "confidence": 0.8},
            {"kind": "create", "topics": [3], "name": "做头像", "goal": "", "why": "", "confidence": 0.5}]})
        self.tidy.recent_topics = lambda state, chat, now, **kw: [one, two, three]
        record = self.tidy.whole(self.ctx, CHAT, "run-1", "test", "")
        rows = record["view"]["rows"]
        self.assertEqual(self.click({"kind": cards.DECLINE_KIND, "run": "run-1", "proposal": rows[1]["proposal"]},
                                    record["message_id"]), "tidy_row_decline")
        self.assertEqual(self.click({"kind": cards.ALL_KIND, "run": "run-1"}, record["message_id"]), "tidy_all")
        view = read_object(self.tidy.run_path(self.state, "run-1"))["view"]
        self.assertEqual([row["state"] for row in view["rows"]], ["done", "declined"])
        self.assertEqual(projects.read_record(self.state, upgrades.project_id("tidy", rows[0]["proposal"]))["threads"], [one, two])
        self.assertEqual(self.click({"kind": cards.OK_KIND, "run": "run-1", "proposal": rows[1]["proposal"]},
                                    record["message_id"]), "tidy_decided")

    def test_one_tidy_at_a_time_and_the_tool_and_the_web_page_say_where_it_is(self):
        self.filler()
        self.tidy.busy.acquire()
        self.addCleanup(self.tidy.busy.release)
        self.tidy.progress = {"done": 12, "total": 40}
        answer = upgrades.projects_tidy(self.env, SimpleNamespace(args={}, id="r9", job_key="main-job"))
        self.assertEqual(answer["text"], "正在整理（12/40）")
        self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o1", "payload": {"scope": ""}}), ("done", "正在整理（12/40）"))
        self.assertIn("projects_tidy", [tool["name"] for tool in butler_tool_specs.listed({"COVEY_PROJECTS": "1", proposals.ENV: "suggest"})])


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

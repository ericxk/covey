"""整理专项 (app/project_tidy.py, app/project_sorter.py, core/project_proposals.py, core/topic_gist.py; docs/projects.md 整理专项): a
topic's 话题要点 kept from its checkpoint, a sure join done by a 整理 and its 移出 keeping the topic out for good, the proposal
card's buttons, the 整理专项 card with a topic the model will not read left out alone, and with the switch off nothing of it.
There is no real-time 整理: the model judges only in a whole one (scheduled or asked for)."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from test_dispatch_cards import CHAT
from support.assist import claude_topics
from test_project_upgrade import UpCase

from covey.app import project_sorter as sorter
from covey.app import tidy_facts as facts, tidy_run, topic_links
from covey.app import butler_projects, project_members as members, projects as upgrades
from covey.app.project_tidy import ProjectTidy
from covey.app.track_judge import Failed
from covey.app.web_ops import Ops
from covey.cards import project_proposal as cards
from covey.app import digest, project_lead
from covey.core import butler_tool_specs, projects, topic_gist
from covey.core import env as cenv
from covey.core import project_proposals as proposals
from covey.core.checkpoint import GIST_BEFORE, GIST_NONE, TOPIC_GIST_RULE, gist_note
from covey.core.jobs import upsert_job_record
from covey.core.state import read_object, write_json
from covey.core.topics import label_title, topic_file

A = "修 064 采购审批数据包"
B = "优化 DataLab 的 Prompt"


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
        self.tidy.words.link = lambda state, topic, **kw: f"https://applink.example/{topic[-6:]}"

    def see(self, topics):
        """The topics a 整理 looks at (app/tidy_facts.py recent_topics) are what ``topics()`` says, in that order."""
        patcher = mock.patch.object(facts, "recent_topics", lambda state, chat, now, **kw: topics())
        patcher.start()
        self.addCleanup(patcher.stop)

    def send_message(self, profile, chat, words, key, *, voice, card=None, on_card=None, reply_to_message_id="", **kwargs):
        message = next(self.messages)
        if card is not None and on_card is not None:
            on_card(message)
        return True

    def ask(self, system, data, where, limit):
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
    def test_a_turns_topic_gist_is_kept_and_a_turn_without_one_keeps_the_last(self):
        topic = self.topic(A, words=("先修一下 064 数据包",), gist={"essence": A})
        self.tidy.tick(self.ctx)  # The first look only sets the cursor: the turn before it is not read.
        self.assertEqual(topic_gist.read(self.state, topic), {})
        self.turn(topic, "这数据包的问题其实在 Prompt", gist={"本质": A, "现在的目标": "复现"})
        self.assertGreaterEqual(self.tidy.scan(self.state, CHAT, time.time()), 1)
        self.assertEqual(topic_gist.read(self.state, topic)["essence"], A)
        self.turn(topic, "那就改 Prompt 吧", gist="本质：" + B + "\n和上次比变了什么：从修一个数据包变成改 Prompt")
        self.assertGreaterEqual(self.tidy.scan(self.state, CHAT, time.time()), 1)
        saved = topic_gist.read(self.state, topic)
        self.assertEqual((saved["essence"], [item["text"] for item in saved["essences"]]), (B, [A, B]))
        self.turn(topic, "接着说")  # No topic_gist in its checkpoint: the last one stays.
        self.assertGreaterEqual(self.tidy.scan(self.state, CHAT, time.time()), 1)
        self.assertEqual(topic_gist.read(self.state, topic), saved)
        self.assertEqual(self.tidy.scan(self.state, CHAT, time.time()), 0)  # Each turn is read once.
        self.assertEqual(self.asked, [])  # Reading a turn never asks the model.

    def test_a_queue_an_older_release_left_in_the_state_is_dropped(self):
        self.tidy.tick(self.ctx)
        proposals.change(self.state, lambda data: data.update(queue=[{"project": "p" * 16, "at": 1}]))
        self.assertIn("queue", proposals.load(self.state))
        self.tidy.scan(self.state, CHAT, time.time())
        self.assertNotIn("queue", proposals.load(self.state))

    def test_the_judge_reads_the_repositories_a_topic_lists_and_no_workspace_for_a_home_topic(self):
        plain = self.topic("旧话题")
        home = self.topic("主目录开的话题")
        data = read_object(topic_file(self.state, home))
        data.update(home_start=True, repos=[{"name": "covey", "path": "/absolute/covey", "mode": "write"},
                                            {"name": "covey", "path": "/absolute/covey-wt", "mode": "read"}])
        write_json(topic_file(self.state, home), data)
        old, new = sorter.material(self.state, CHAT, plain, 0), sorter.material(self.state, CHAT, home, 0)
        self.assertEqual((old.repos, old.workspace), ((), "project"))
        self.assertEqual((new.repos, new.workspace), (("covey",), ""))
        self.assertNotIn("repos", sorter.topic_row(1, old, {}))
        self.assertEqual(sorter.topic_row(1, new, {})["repos"], ["covey"])

    def test_a_gist_is_written_only_when_the_goal_or_scope_moved_and_a_turn_without_one_keeps_the_last(self):
        # #31: "短任务、问答也写" cost a follow-up question a checkpoint write of its own. Now the rule asks for one only for
        # a topic without a gist or whose goal or scope moved; the Bridge keeps the last one when a turn writes none.
        self.assertNotIn("短任务、问答也写", TOPIC_GIST_RULE)
        self.assertNotIn("每一轮结束前都", TOPIC_GIST_RULE)
        for words in ("还没有要点，或这一轮让它的目标或涉及的范围变了，才在检查点里写 topic_gist", "没变就不用写，Bridge 沿用上一份"):
            self.assertIn(words, TOPIC_GIST_RULE)
        self.assertEqual(gist_note(""), TOPIC_GIST_RULE + "\n" + GIST_NONE)
        self.assertEqual(gist_note("本质：x"), TOPIC_GIST_RULE + "\n" + GIST_BEFORE + "\n本质：x")
        topic = self.topic(A)
        self.tidy.tick(self.ctx)
        self.assertTrue(sorter.needs_gist(self.state, topic, "om_x"))  # Never wrote one: 整理专项 still reads it whole.
        self.turn(topic, "复现一下", gist={"essence": A, "goal": "复现"})
        self.tidy.scan(self.state, CHAT, time.time())
        kept = topic_gist.read(self.state, topic)
        self.assertEqual((kept["essence"], kept["goal"], kept["source"]), (A, "复现", "turn"))
        self.turn(topic, "那个日志在哪")  # A follow-up: no topic_gist in its checkpoint.
        self.assertGreaterEqual(self.tidy.scan(self.state, CHAT, time.time()), 1)
        self.assertEqual(topic_gist.read(self.state, topic), kept)
        self.assertFalse(sorter.needs_gist(self.state, topic, "om_x"))

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
        with mock.patch.dict(os.environ, {proposals.ENV: "off"}):
            self.assertEqual(Runs._gist_note(ctx, job), "")


class JoinTest(TidyCase):
    def whole(self, run):
        return self.tidy.runs.whole(self.ctx, CHAT, run, "test", "")

    def test_a_sure_join_makes_the_topic_the_projects_tells_the_lead_and_marks_it_and_after_leave_never_again(self):
        member = self.topic("DataLab Prompt 第一轮")
        pid = self.project("DataLab Prompt 优化", [member])
        self.filler()
        topic = self.topic(A, gist={"essence": B})
        self.see(lambda: [topic])
        write_json(self.state / "topics" / topic / "topic.json",
                   {**read_object(self.state / "topics" / topic / "topic.json"), "root_id": "om_root"}, ensure_ascii=False)
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.95}]})
        view = self.whole("run-j1")["view"]
        self.assertTrue(any("已把" in line and "「DataLab Prompt 优化」" in line for line in view["did"]))
        self.assertEqual(projects.topic_project(self.state, topic).id, pid)
        self.assertIn(topic, projects.read_record(self.state, pid)["threads"])
        self.assertEqual(projects.cells(self.state, CHAT)[topic], projects.lead_topic_id(CHAT, pid))
        receipts = [read_object(path) for path in project_lead.inbox_dir(self.state, pid).glob("*.json")]
        self.assertEqual([item["kind"] for item in receipts], [members.JOINED])
        self.assertEqual(projects.read_record(self.state, pid)["joins"][-1]["by"], "tidy")
        # Neither a topic card nor a status card: the line goes into the topic's thread as a card of its own.
        sent = next(call for call in self.app.messenger.send_message.call_args_list if call.kwargs.get("reply_to_message_id") == "om_root")
        self.assertIn("已归进专项「DataLab Prompt 优化」", text(sent.kwargs["card"]))
        self.assertIn(cards.LEAVE_KIND, text(sent.kwargs["card"]))
        self.click({"kind": cards.LEAVE_KIND, "topic_id": topic, "project_id": pid}, "om_topic_card")
        self.assertIsNone(projects.topic_project(self.state, topic))
        self.assertNotIn(topic, projects.read_record(self.state, pid)["threads"])
        self.assertTrue(proposals.never_auto(self.state, topic))
        self.assertIn(members.LEFT, [read_object(path)["kind"] for path in project_lead.inbox_dir(self.state, pid).glob("*.json")])
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "同一件事", "confidence": 0.99}]})
        self.assertEqual(self.whole("run-j2")["view"]["did"], [])  # The next 整理 does not take it in again.
        self.assertIsNone(projects.topic_project(self.state, topic))
        self.assertTrue(projects.read_record(self.state, pid)["joins"][-1]["left_at"])

    def test_the_status_card_gets_the_line_with_its_leave_button(self):
        from covey.app import status_cards

        member = self.topic("一轮")
        pid = self.project("DataLab Prompt 优化", [member])
        topic = self.topic(A)
        write_json(status_cards.record_path(self.state, topic), {"card_id": "card-s", "message_id": "om_status", "sequence": 1,
                                                                   "shown": ["T1 · " + A, "做完", "13:01", "结论：好"]})
        self.assertTrue(self.tidy.applier.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid), CHAT))
        card = self.put.call_args.args[2]
        self.assertIn("已归进专项「DataLab Prompt 优化」", text(card))
        self.assertIn(cards.LEAVE_KIND, text(card))
        self.tidy.applier.leave(self.ctx, CHAT, topic, pid)
        self.assertNotIn("已归进", text(self.put.call_args.args[2]))

    def test_a_low_confidence_join_is_not_done(self):
        member = self.topic("一轮")
        self.project("DataLab Prompt 优化", [member])
        self.filler()
        topic = self.topic(A)
        self.see(lambda: [topic])
        self.verdicts.append({"suggestions": [{"kind": "join", "topic": 1, "project": 1, "why": "像", "confidence": 0.6}]})
        view = self.whole("run-j3")["view"]
        self.assertEqual((view["did"], view["rows"]), ([], []))  # Not sure enough to do, not worth asking.
        self.assertIsNone(projects.topic_project(self.state, topic))

    def test_the_projects_card_lists_who_took_each_topic_in_with_a_leave_button_while_it_is_in(self):
        from covey.app import project_facts

        member = self.topic("一轮")
        pid = self.project("DataLab Prompt 优化", [member])
        topic = self.topic(A)
        self.assertTrue(self.tidy.applier.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid, why="同一件事"), CHAT))
        lines = project_facts.join_lines(self.state, projects.read_record(self.state, pid))
        self.assertEqual(len(lines), 1)
        self.assertIn("整理专项归入", lines[0][0])
        self.assertIn("同一件事", lines[0][0])
        self.assertEqual(lines[0][1], topic)
        self.tidy.applier.leave(self.ctx, CHAT, topic, pid)
        lines = project_facts.join_lines(self.state, projects.read_record(self.state, pid))
        self.assertIn("已移出", lines[0][0])
        self.assertEqual(lines[0][1], "")



class ProposalTest(TidyCase):
    def propose(self, topics, name="DataLab Prompt 优化"):
        """What a whole 整理 hands the proposal cards for a new project the judge saw (its run id is on it)."""
        made = proposals.new_proposal(proposals.CREATE, topics=topics, name=name, goal="新旧 Prompt 对率可比", why="都在改 Prompt",
                                      confidence=0.9, run="run-p")
        return self.tidy.proposal_cards.propose(self.ctx, CHAT, made)

    def setUp(self):
        super().setUp()
        self.filler()
        self.pair = [self.topic(A, gist={"essence": B}), self.topic("优化 Bootstrap 下一环节", gist={"essence": B})]
        self.see(lambda: list(self.pair))  # The judge sees the two in this order.

    def card_and_record(self):
        record = proposals.proposals(self.state, None)[-1]
        return record, record["card"]["message_id"]

    def test_the_first_proposal_card_says_what_a_project_is_and_has_the_four_buttons(self):
        self.assertEqual(self.propose(self.pair), "proposed")
        card = text(self.sent_cards()[-1])
        for word in ("DataLab Prompt 优化", cards.INTRO, cards.AFTER, "建专项", "不管了", "tidy_name"):
            self.assertIn(word, card)
        for word in ("改个名再建", "以后别提议"):  # The owner, 2026-11-18: 4 个按钮太冗余了.
            self.assertNotIn(word, card)
        self.assertEqual(self.propose(self.pair), "")  # One open proposal about a topic at a time.
        self.assertEqual(len(self.sent_cards()), 1)
        self.assertTrue(proposals.prefs(self.state)["first_sent"])

    def test_create_gathers_the_topics_into_a_project_and_its_lead_gets_them(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, message), "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual((made["name"], made["origin"]["kind"], made["threads"]), ("DataLab Prompt 优化", "tidy", self.pair))
        self.assertTrue(all(name in made["steps"] for name in upgrades.GATHER_STEPS))
        start = [read_object(path) for path in project_lead.inbox_dir(self.state, made["id"]).glob("*.json")]
        self.assertEqual(start[0]["kind"], "start")
        self.assertIn("主人同意把这几个已有的话题建成专项", start[0]["text"])
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.DONE)
        self.assertIn("已建专项「DataLab Prompt 优化」", text(self.patched()))
        self.assertEqual(proposals.prefs(self.state)["accepted_in_row"], 1)

    def test_a_new_project_leaves_no_queue_in_the_state(self):
        # There is no sweep of the recent topics after a project is made any more (it was the real-time 整理's).
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, message), "tidy_create")
        self.assertIsNotNone(projects.read_record(self.state, upgrades.project_id("tidy", record["id"])))
        self.assertNotIn("queue", proposals.load(self.state))
        self.tidy.tick(self.ctx)
        self.assertNotIn("queue", proposals.load(self.state))

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

    def test_a_kept_one_topic_view_still_draws(self):
        """A proposal card of one topic sent before 2026-11-18 is redrawn on a click as before."""
        row = {"label": "T252", "title": "阅读这个session", "link": "", "essence": "运行台", "now": "做完", "days": 19, "turns": 3}
        card = text(cards.proposal_card({"proposal": "1" * 16, "rows": [row], "name": "运行台", "state": "declined"}))
        self.assertIn("这个话题推了很久", card)
        self.assertIn("19 天 · 3 轮", card)

    def test_the_owner_unticks_a_topic_adds_one_by_its_number_and_renames_before_building(self):
        """The owner, 2026-11-18: 在页面增删一下话题…让人微操一波 — the card's select and T number box decide the topics."""
        third = self.topic("顺手查的一件事")
        self.see(lambda: [*self.pair, third])
        self.propose(self.pair)
        record, message = self.card_and_record()
        choices = record["view"]["choices"]
        self.assertEqual([choice["topic"] for choice in choices][:3], [*self.pair, third])
        self.assertEqual([choice["chosen"] for choice in choices][:3], [True, True, False])
        number = read_object(self.state / "topics" / third / "topic.json")["topic_number"]
        with mock.patch.object(topic_links, "topic_numbers", return_value={number: third}):
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
                                    name="DataLab Prompt 优化"), "tidy_no_topics")
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.OPEN)
        self.assertEqual(self.click({"kind": cards.NAMED_KIND, "proposal": record["id"]}, message, topics=["0", "1"],
                                    name="DataLab Prompt 优化"), "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertNotEqual(made.get("name_by"), "owner")
        self.assertEqual(proposals.prefs(self.state)["accepted_in_row"], 1)

    def test_decline_keeps_the_same_proposal_away_until_two_more_topics_and_never_stops_them(self):
        self.propose(self.pair)
        record, message = self.card_and_record()
        self.assertEqual(self.click({"kind": cards.DECLINE_KIND, "proposal": record["id"]}, message), "tidy_decline")
        self.assertIn("tidy_lesson", text(self.patched()))  # The reason form under it.
        self.assertEqual(self.click({"kind": cards.LESSON_KIND, "proposal": record["id"]}, message,
                                    why="数据包和 Prompt 是两件事"), "tidy_lesson")
        self.assertEqual(proposals.lessons(self.state), ["数据包和 Prompt 是两件事"])
        self.assertEqual(self.propose(self.pair), "")
        self.pair += [self.topic("Prompt 第三轮", gist={"essence": B}), self.topic("Prompt 第四轮", gist={"essence": B})]
        self.assertEqual(self.propose(self.pair), "proposed")
        record, message = self.card_and_record()
        self.click({"kind": cards.NEVER_KIND, "proposal": record["id"]}, message)
        self.assertTrue(proposals.prefs(self.state)["no_proposals"])
        self.pair.append(self.topic("Prompt 第五轮"))
        self.pair.append(self.topic("Prompt 第六轮"))
        self.assertEqual(self.propose(self.pair), "")

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
        self.see(lambda: [*topics, member])  # The judge's indexes: these, in this order.
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
        self.assertIn("老话题 2（模型返回了错误）", view["skipped"][0])
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
        for word in ("整理专项", "新专项的提议", "「推理网关稳定」", "改名", "不用", "已经做了", "最近 7 天动过的话题"):
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
        self.see(lambda: [one, two, three, *members])
        record = self.tidy.runs.whole(self.ctx, CHAT, "run-1", "test", "")
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
        self.see(lambda: [one, two])
        record = self.tidy.runs.whole(self.ctx, CHAT, "run-2", "test", "")
        self.assertEqual(self.click({"kind": cards.ALL_KIND, "run": "run-2"}, record["message_id"]), "tidy_all")
        self.assertEqual(projects.read_record(self.state, pid)["name"], "回流周报汇报")

    def test_one_tidy_at_a_time_and_the_tool_and_the_web_page_say_where_it_is(self):
        self.filler()
        self.tidy.shared.busy.acquire()
        self.addCleanup(self.tidy.shared.busy.release)
        self.tidy.shared.progress = {"done": 12, "total": 40}
        answer = butler_projects.projects_tidy(self.env, SimpleNamespace(args={}, id="r9", job_key="main-job"))
        self.assertEqual(answer["text"], "正在整理（12/40）")
        self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o1", "payload": {"scope": ""}}), ("done", "正在整理（12/40）"))
        self.assertIn("projects_tidy", [tool["name"] for tool in butler_tool_specs.listed({"COVEY_PROJECTS": "1", proposals.ENV: "suggest"})])


class SuggestionLayoutTest(TidyCase):
    """2026-11-19 T572, the owner's pick A 一条一块: each suggestion a block (what it does in bold, where from, 原因, its topics, its
    verb and 不用 under them), the same suggestions on the web page's 看板, decided there through the card's own click."""

    def split_run(self, run="run-s"):
        self.filler(3)
        members = [self.topic(f"评审改动 {n}") for n in range(6)] + [self.topic("体验 1"), self.topic("体验 2")]
        pid = self.project("体验改进", members)
        self.verdicts.append({"suggestions": [{"kind": "split", "project": 1, "topics": [1, 2, 3, 4, 5, 6], "name": "评审改动上线",
                                               "goal": "评审的改动都上线", "why": "都是同一份评审报告拆出的改动", "confidence": 0.8}]})
        self.see(lambda: members)
        return self.tidy.runs.whole(self.ctx, CHAT, run, "test", ""), pid, members

    def test_a_suggestion_is_a_block_its_topics_listed_then_folded_and_a_row_kept_before_draws_as_before(self):
        record, pid, members = self.split_run()
        row = record["view"]["rows"][0]
        self.assertEqual(row["item"]["kind"], "split")
        self.assertEqual(row["item"]["project"], "体验改进")
        self.assertEqual(len(row["item"]["topics"]), 6)
        card = text(cards.tidy_card(record["view"]))
        for word in ("要你定 · 整理专项", "条建议", "拆出", "拆出新专项「评审改动上线」", "从「体验改进」移过去 6 个话题",
                     "原因：都是同一份评审报告拆出的改动", "目标：评审的改动都上线", "还有 3 个话题", "拆出来", "不用", "band-line"):
            self.assertIn(word, card)
        self.assertNotIn("等", card.replace("等你", ""))  # Never “T550 等 11 个” (the owner, 2026-11-18).
        for topic in members[:6]:
            self.assertIn(f"https://applink.example/{topic[-6:]}", card)
        old = {**record["view"], "rows": [{key: value for key, value in row.items() if key != "item"}]}
        drawn = text(cards.tidy_card(old))
        self.assertIn("要你定的（1 条）", drawn)
        self.assertIn('"好"', drawn)
        self.assertNotIn("band-line", drawn)

    def test_the_cards_tag_counts_what_still_waits_and_says_done_once_it_is_all_decided(self):
        # T647 B7: the tag counted the decided rows too: "1 条建议" in the waiting colour stayed on a card with nothing left
        # to decide, under a band saying 这次的建议都定好了.
        record, pid, members = self.split_run()
        tags = lambda card: [(t["text"]["content"], t["color"]) for t in card["header"]["text_tag_list"]]  # noqa: E731
        self.assertEqual(tags(cards.tidy_card(record["view"])), [("1 条建议", "orange")])
        row = record["view"]["rows"][0]
        self.assertEqual(self.click({"kind": cards.DECLINE_KIND, "run": "run-s", "proposal": row["proposal"]},
                                    record["message_id"]), "tidy_row_decline")
        drawn = self.patched()
        self.assertEqual(tags(drawn), [("整理好了", "green")])
        self.assertIn("这次的建议都定好了", text(drawn))
        self.assertNotIn("条建议", text(drawn))
        # A row kept before ``item``, drawn as before: its heading counts what is left too.
        kept = [{**{key: value for key, value in row.items() if key != "item"}, "state": "declined"},
                {"proposal": "f" * 16, "line": "改名", "state": "open"}]
        view = {**record["view"], "rows": kept}
        self.assertIn("要你定的（1 条）", text(cards.tidy_card(view)))
        self.assertEqual(tags(cards.tidy_card(view)), [("1 条建议", "orange")])
        view["rows"] = [{**each, "state": "done"} for each in kept]
        self.assertIn("这次的建议都定好了（2 条）", text(cards.tidy_card(view)))
        self.assertEqual(tags(cards.tidy_card(view)), [("整理好了", "green")])

    def test_the_web_page_lists_the_suggestion_and_decides_it_through_the_cards_click(self):
        record, pid, members = self.split_run()
        shown = self.tidy.web_view()["proposals"]
        self.assertEqual([(each["kind"], each["tag"], each["verb"], each["state"]) for each in shown], [("split", "拆出", "拆出来", "open")])
        self.assertEqual(shown[0]["headline"], "拆出新专项「评审改动上线」")
        self.assertEqual([each["label"] for each in shown[0]["topics"]][:1], [label_title(self.state, members[0])[0]])
        self.assertNotIn("link", shown[0]["topics"][0])
        self.assertEqual(shown[0]["project"], "")  # 去专项 only once it is done (v28).
        before = self.tidy.web_stamps()
        state, words = Ops(self.env).run({"kind": "tidy_decide", "op_id": "o1", "payload": {"proposal": shown[0]["id"], "choice": "ok"}})
        self.assertEqual(state, "done", words)
        self.assertEqual(proposals.read_proposal(self.state, shown[0]["id"])["state"], proposals.DONE)
        self.assertNotEqual(self.tidy.web_stamps(), before)
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], record["message_id"])  # The card redrawn the same.
        self.assertNotIn("拆出来", text(self.patched()))
        after = self.tidy.web_view()["proposals"][0]
        self.assertEqual((after["state"], after["note"]), ("done", words))
        self.assertEqual(projects.read_record(self.state, after["project"])["name"], "评审改动上线")  # The new one, not ``pid``.
        again = Ops(self.env).run({"kind": "tidy_decide", "op_id": "o2", "payload": {"proposal": shown[0]["id"], "choice": "decline"}})
        self.assertEqual(again[0], "failed")
        self.assertIn("已经定过了", again[1])
        self.assertEqual(Ops(self.env).run({"kind": "tidy_decide", "op_id": "o3", "payload": {"proposal": "nope", "choice": "maybe"}})[0],
                         "failed")

    def test_a_click_on_the_card_and_one_on_the_page_carry_a_suggestion_out_once(self):
        record, pid, members = self.split_run()
        pid_ = record["view"]["rows"][0]["proposal"]
        self.assertEqual(self.click({"kind": cards.DECLINE_KIND, "run": "run-s", "proposal": pid_}, record["message_id"]),
                         "tidy_row_decline")
        self.assertEqual(proposals.read_proposal(self.state, pid_)["state"], proposals.DECLINED)
        state, words = Ops(self.env).run({"kind": "tidy_decide", "op_id": "o1", "payload": {"proposal": pid_, "choice": "ok"}})
        self.assertEqual(state, "failed")
        self.assertEqual(proposals.read_proposal(self.state, pid_)["state"], proposals.DECLINED)
        self.assertEqual(self.tidy.web_view()["proposals"][0]["state"], "declined")

    def test_a_new_project_waiting_on_its_own_card_is_declined_from_the_page(self):
        self.filler(3)
        one, two = self.topic("网关失联"), self.topic("网关扩容")
        self.verdicts.append({"suggestions": [
            {"kind": "create", "topics": [1, 2], "name": "推理网关稳定", "goal": "一周不失联", "why": "同一个网关", "confidence": 0.8}]})
        self.see(lambda: [one, two])
        self.tidy.runs.whole(self.ctx, CHAT, "run-c", "test", "")
        shown = self.tidy.web_view()["proposals"]
        self.assertEqual([(each["kind"], each["headline"], each["run"]) for each in shown], [("create", "新建专项「推理网关稳定」", "")])
        state, words = Ops(self.env).run({"kind": "tidy_decide", "op_id": "o1", "payload": {"proposal": shown[0]["id"], "choice": "decline"}})
        self.assertEqual(state, "done", words)
        self.assertEqual(proposals.read_proposal(self.state, shown[0]["id"])["state"], proposals.DECLINED)
        self.assertIn("不管了", text(self.patched()))
        self.assertEqual([(each["state"], each["project"]) for each in self.tidy.web_view()["proposals"]], [("declined", "")])

    def test_a_new_project_made_from_the_page_is_the_one_its_row_opens(self):
        self.filler(3)
        one, two = self.topic("网关失联"), self.topic("网关扩容")
        self.verdicts.append({"suggestions": [
            {"kind": "create", "topics": [1, 2], "name": "推理网关稳定", "goal": "一周不失联", "why": "同一个网关", "confidence": 0.8}]})
        self.see(lambda: [one, two])
        self.tidy.runs.whole(self.ctx, CHAT, "run-c", "test", "")
        pid = self.tidy.web_view()["proposals"][0]["id"]
        state, words = Ops(self.env).run({"kind": "tidy_decide", "op_id": "o1", "payload": {"proposal": pid, "choice": "ok"}})
        self.assertEqual(state, "done", words)
        made = proposals.read_proposal(self.state, pid)["project_made"]
        self.assertEqual(projects.read_record(self.state, made)["name"], "推理网关稳定")
        self.assertEqual([(each["state"], each["project"]) for each in self.tidy.web_view()["proposals"]], [("done", made)])
        projects.record_path(self.state, made).unlink()  # Gone: no 去专项.
        self.assertEqual(self.tidy.web_view()["proposals"][0]["project"], "")


class PendingTest(TidyCase):
    """The owner, 2026-11-18 23:17-23:19: 怎么可能没有专项呢 / 为啥现在我点了整理专项说不需要呢 — a proposal still waiting is
    said so, decided right on the 看板, and the three places say the same."""

    def setUp(self):
        super().setUp()
        self.filler(3)
        self.one, self.two = self.topic("修 064 采购审批数据包"), self.topic("优化 Bootstrap 下一环节")
        self.see(lambda: [self.one, self.two])
        self.suggest = {"suggestions": [{"kind": "create", "topics": [1, 2], "name": "数据包提示词优化", "goal": "合入 !248",
                                         "why": "同一条流水线", "confidence": 0.8}]}

    def test_a_suggestion_already_waiting_is_named_not_called_nothing_to_change(self):
        self.verdicts.append(self.suggest)
        first = self.tidy.runs.whole(self.ctx, CHAT, "run-a", "test", "")
        self.assertEqual(first["view"]["proposed"], ["数据包提示词优化"])
        self.verdicts.append(self.suggest)
        second = self.tidy.runs.whole(self.ctx, CHAT, "run-b", "test", "")
        self.assertEqual(second["view"].get("pending"), ["数据包提示词优化"])
        card = text(cards.tidy_card(second["view"]))
        self.assertIn("还在等你定", card)
        self.assertNotIn("没有要改的", card)

    def test_a_proposal_of_one_topic_left_from_before_expires_at_the_next_tidy(self):
        alone = proposals.new_proposal(proposals.CREATE, topics=[self.one], name="单独一个", goal="做完", source="realtime")
        proposals.put_proposal(self.state, alone)
        self.tidy.runs.whole(self.ctx, CHAT, "run-c", "test", "")
        self.assertEqual(proposals.read_proposal(self.state, alone["id"])["state"], proposals.EXPIRED)

    def test_build_on_the_board_creates_it_redraws_its_own_card_and_the_board(self):
        self.verdicts.append(self.suggest)
        self.tidy.runs.whole(self.ctx, CHAT, "run-d", "test", "")
        record = [r for r in proposals.proposals(self.state, [proposals.OPEN]) if r.get("name") == "数据包提示词优化"][0]
        own = record["card"]["message_id"]
        redrawn = []
        self.tidy.handed["board"] = lambda ctx, message: redrawn.append(message)
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"], "from": "board"}, "om_board9"),
                         "tidy_create")
        made = projects.read_record(self.state, upgrades.project_id("tidy", record["id"]))
        self.assertEqual(made["threads"], [self.one, self.two])
        self.assertEqual(redrawn, ["om_board9"])
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], own)  # Its own card, not the board.
        answer = butler_projects.projects_list(self.env, SimpleNamespace(args={}, id="r1", job_key="main-job"))
        self.assertIn("数据包提示词优化", answer["text"])

    def test_the_board_lists_the_waiting_proposals_with_build_and_not_now(self):
        from covey.cards.board import board_card
        self.verdicts.append(self.suggest)
        self.tidy.runs.whole(self.ctx, CHAT, "run-e", "test", "")
        record = [r for r in proposals.proposals(self.state, [proposals.OPEN]) if r.get("name") == "数据包提示词优化"][0]
        pending = [{"id": record["id"], "name": record["name"], "goal": record["goal"], "why": "",
                    "rows": [{"topic": self.one, "label": "T1", "title": "修 064", "url": "https://applink.example/1"},
                             {"topic": self.two, "label": "T2", "title": "Bootstrap", "url": ""}]}]
        card = text(board_card({"waiting": [], "running": [], "done": [], "stale": [], "tidy": {}, "projects": [],
                                "pending": pending, "proposals": 1}))
        # Under 等你 (T657 B3: 等你定 is 等你), counted among 要你处理: the 专项 part says how many projects there are.
        for word in ("专项", "0 个", "**等你**  <font color='grey'>1</font>", "**1**</font> **件事**", "「数据包提示词优化」",
                     "https://applink.example/1", "建专项", "不管了", cards.CREATE_KIND, cards.DECLINE_KIND, '"from": "board"'):
            self.assertIn(word, card)
        self.assertNotIn("个提议等你定", card)
        self.assertNotIn("往上翻", card)


class FollowTest(TidyCase):
    """The owner, 2026-11-19: 已经归进专项的话题，提议卡不会失效 — four cards stayed on the 看板 after T519、T525、T531 joined a project,
    two of them nearly the same. A proposal follows the joins, however they happen, and a new one does not repeat a waiting one."""

    def setUp(self):
        super().setUp()
        self.filler(3)
        self.trio = [self.topic("Covey重启后自启"), self.topic("开发机重启"), self.topic("自启脚本")]
        self.see(lambda: list(self.trio))
        self.pid = self.project("Covey 体验与节点改进", [self.topic("节点第一期")])

    def suggest(self, picks, name):
        self.verdicts.append({"suggestions": [{"kind": "create", "topics": picks, "name": name, "goal": "自启恢复",
                                               "why": "都在修重启", "confidence": 0.9}]})

    def open_card(self, picks=(1, 2, 3), name="Covey重启后自启恢复"):
        made = proposals.new_proposal(proposals.CREATE, topics=[self.trio[pick - 1] for pick in picks], name=name, goal="自启恢复",
                                      why="都在修重启", confidence=0.9, run="run-f0")
        self.assertEqual(self.tidy.proposal_cards.propose(self.ctx, CHAT, made), "proposed")
        return proposals.waiting(self.state)[0]

    def test_a_topic_the_lead_takes_in_leaves_the_card_with_the_rest_and_the_card_says_where_it_went(self):
        record = self.open_card()
        members.join_topic(self.state, self.pid, self.trio[2], by="lead")
        self.assertEqual(self.tidy.proposal_cards.follow(self.ctx, CHAT), 1)
        now = proposals.read_proposal(self.state, record["id"])
        self.assertEqual((now["state"], now["topics"]), (proposals.OPEN, self.trio[:2]))
        card = text(self.patched())
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], record["card"]["message_id"])
        for word in ("已归进专项「Covey 体验与节点改进」", "只剩下面 2 个话题", "建专项"):
            self.assertIn(word, card)
        self.assertEqual(self.tidy.proposal_cards.follow(self.ctx, CHAT), 0)  # Nothing new: the card is not written again.

    def test_a_topic_that_joins_a_project_loses_its_revisit_and_the_log_says_so(self):
        """T675, 2026-11-23: 约好的回看留在已归进专项的话题上，页面上一直挂着。"""
        from covey.core import revisits as book
        said = []
        with mock.patch.dict(os.environ, {book.SWITCH: "1"}):
            book.change(self.state, self.trio[2], lambda data: book.armed(self.trio[2], CHAT, {"minutes": 10, "look": "看 CI"},
                                                                        by="job", round_=1))
            members.join_topic(self.state, self.pid, self.trio[2], by="lead", log=said.append)
            self.assertEqual(book.read(self.state, self.trio[2]), {})
            self.assertEqual(len(said), 1)
            self.assertIn("joined a project", said[0])
            members.join_topic(self.state, self.pid, self.trio[2], by="lead", log=said.append)  # Again: nothing to remove, nothing said.
            self.assertEqual(len(said), 1)

    def test_too_few_left_ends_it_off_the_board_the_list_and_the_tidy_card(self):
        record = self.open_card((1, 2), "Covey开发机重启后自启修复")
        members.join_topic(self.state, self.pid, self.trio[1], by="tidy")
        self.assertEqual(self.tidy.proposal_cards.follow(self.ctx, CHAT), 1)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["state"], proposals.EXPIRED)
        card = text(self.patched())
        for word in ("已过时", "已归进专项「Covey 体验与节点改进」", "成不了专项", "这条不再提"):
            self.assertIn(word, card)
        self.assertNotIn(cards.CREATE_KIND, card)
        self.assertEqual(proposals.waiting(self.state), [])
        answer = butler_projects.projects_list(self.env, SimpleNamespace(args={}, id="r1", job_key="main-job"))
        self.assertNotIn("Covey开发机重启后自启修复", answer["text"])
        self.assertNotIn("等主人定", answer["text"])
        self.assertEqual(self.click({"kind": cards.CREATE_KIND, "proposal": record["id"]}, record["card"]["message_id"]),
                         "tidy_decided")  # A click on the card from before builds nothing.

    def test_a_new_proposal_inside_a_waiting_one_is_not_sent_and_one_with_more_goes_onto_its_card(self):
        record = self.open_card((1, 2))
        sent = len(self.sent_cards())
        self.assertEqual(self.tidy.proposal_cards.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio[1::-1], name="Covey开发机重启后自启修复")), "")
        self.assertEqual(self.tidy.proposal_cards.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio, name="Covey重启后自启恢复")), "merged")
        self.assertEqual(len(self.sent_cards()), sent)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio)
        self.assertEqual(len(proposals.waiting(self.state)), 1)
        self.assertIn("也是这件事，补进来了", text(self.patched()))

    def test_two_cards_left_with_the_same_topics_keep_only_the_newer(self):
        """The owner's four cards: 「Covey重启后自启恢复」 (T525、T526、T524) and the newer 「Covey开发机重启后自启修复」 (T524、T526)."""
        older = proposals.new_proposal(proposals.CREATE, topics=self.trio, name="Covey重启后自启恢复", now=time.time() - 60)
        newer = proposals.new_proposal(proposals.CREATE, topics=self.trio[:2], name="Covey开发机重启后自启修复")
        for record, message in ((older, "om_old"), (newer, "om_new")):
            proposals.put_proposal(self.state, {**record, "card": {"message_id": message, "chat_id": CHAT}})
        members.join_topic(self.state, self.pid, self.trio[2], by="lead")
        self.assertEqual(self.tidy.proposal_cards.follow(self.ctx, CHAT), 2)
        self.assertEqual(proposals.read_proposal(self.state, older["id"])["state"], proposals.EXPIRED)
        self.assertEqual(proposals.read_proposal(self.state, newer["id"])["state"], proposals.OPEN)
        self.assertEqual([record["id"] for record in proposals.waiting(self.state)], [newer["id"]])
        self.assertEqual(self.app.messenger.patch_card.call_args.args[1], "om_old")
        self.assertIn("和提议「Covey开发机重启后自启修复」是同一件事", text(self.patched()))

    def test_a_proposal_mostly_of_a_projects_topics_is_not_made(self):
        for topic in self.trio[1:]:
            members.join_topic(self.state, self.pid, topic, by="rule")
        self.assertEqual(self.tidy.proposal_cards.propose(self.ctx, CHAT, proposals.new_proposal(
            proposals.CREATE, topics=self.trio, name="Covey重启后自启恢复")), "")
        self.assertEqual(proposals.proposals(self.state, [proposals.OPEN]), [])

    def test_each_tick_follows_and_a_whole_tidy_names_the_one_it_merged_into_as_waiting(self):
        record = self.open_card((1, 2))
        self.suggest([1, 2, 3], "Covey重启后自启恢复")
        view = self.tidy.runs.whole(self.ctx, CHAT, "run-f", "test", "")["view"]
        self.assertEqual(view.get("pending"), ["Covey重启后自启恢复"])
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio)
        members.join_topic(self.state, self.pid, self.trio[0], by="lead")
        with mock.patch.object(self.tidy, "scan", return_value=0), mock.patch.object(self.tidy.runs, "scheduled"):
            self.tidy.tick(self.ctx)
        self.assertEqual(proposals.read_proposal(self.state, record["id"])["topics"], self.trio[1:])


class JudgeInputTest(TidyCase):
    """T924: the judge got 75k characters for 40 topics, 62% of them the owner's words of topics that had a gist, and 31 of
    its 40 places were the projects' own topics. A topic with a gist is sent as its gist (the T numbers his words name, the
    opening of its latest conclusion), the words only for one without or a read in full; the topics in no project come
    first."""

    GIST = {"essence": "推理网关稳定", "goal": "不失联", "involves": "网关", "related": ["T88"], "changed": "从排查改成修"}

    def test_a_topic_with_a_gist_is_sent_as_its_gist_and_one_without_with_its_words(self):
        said = self.topic("网关", words=["先看 T123 的网关" + "很长的话" * 400, "再对一下 T88 和 T77"])
        plain = self.topic("没要点", words=["一些话"])
        topic_gist.save(self.state, said, self.GIST, source="turn")
        item = sorter.material(self.state, CHAT, said, time.time())
        row = sorter.topic_row(1, item, {})
        self.assertNotIn("owner", row)
        self.assertNotIn("conclusions", row)
        self.assertEqual(row["gist"], {key: self.GIST[key] for key in sorter.BRIEF})  # Without ``changed``.
        self.assertEqual(row["refs"], ["T77", "T123"])  # The latest first, T88 the gist relates already.
        self.assertTrue(row["now"].startswith("结论：再对一下"))
        self.assertLessEqual(len(row["now"]), sorter.NOW_CHARS)
        bare = sorter.topic_row(2, sorter.material(self.state, CHAT, plain, time.time()), {})
        self.assertEqual((bare["owner"], bare["gist"]), (["一些话"], {}))
        full = sorter.topic_row(1, sorter.in_full(self.state, item), {}, full=True)
        self.assertTrue(full["full"])
        self.assertLessEqual(sum(map(len, full["owner"])), sorter.FULL_CHARS + 20)
        self.assertIn("refs", sorter.JUDGE_SYSTEM)

    def test_a_whole_tidy_sends_the_gists_it_backfilled_and_the_words_of_the_topic_it_reads_in_full(self):
        topics = [self.topic(f"老话题 {n}", words=[f"老话题 {n} 要做的事" * 50]) for n in range(5)]
        self.see(lambda: topics)
        self.verdicts.extend([{"read": [2]}, {"suggestions": []}])
        self.tidy.start(self.ctx, origin="test", wait=True)
        first, second = self.judged()[-2:]
        self.assertTrue(all("owner" not in row and row["gist"]["essence"] for row in first["topics"]))
        self.assertEqual(second["full"], [2])
        self.assertIn("owner", second["topics"][1])
        self.assertNotIn("owner", second["topics"][0])

    def test_the_topics_in_no_project_come_first_and_the_projects_own_take_their_places_at_most(self):
        inside = [self.topic(f"专项里的 {n}") for n in range(5)]
        self.project("网关", inside)
        others = [self.topic(f"游离的 {n}") for n in range(6)]
        records = facts.live_projects(self.state, CHAT)
        with mock.patch.object(sorter, "MAX_TOPICS", 6), mock.patch.object(sorter, "MEMBER_TOPICS", 2):
            chosen = facts.recent_topics(self.state, CHAT, time.time(), records=records)
            self.assertEqual((len(chosen), len(set(chosen) & set(inside))), (6, 2))
            few = facts.recent_topics(self.state, CHAT, time.time(), records=records, scope="游离的 0")
            self.assertEqual(few, [others[0]])  # A 整理 of one word: as they come.
        with mock.patch.object(sorter, "MAX_TOPICS", 7), mock.patch.object(sorter, "MEMBER_TOPICS", 2):
            for topic in others[1:]:
                projects.add_member(self.state, records[0]["id"], topic)
            records = facts.live_projects(self.state, CHAT)
            chosen = facts.recent_topics(self.state, CHAT, time.time(), records=records)
            self.assertEqual((len(chosen), len(set(chosen) - set(inside) - set(others[1:]))), (7, 1))  # Not enough others.


class ReadAtOnceTest(TidyCase):
    """T924: a read on the followed budget takes a minute or more, so a 整理 reads READS_AT_ONCE topics at a time; once one
    says no model can take any (a spent quota), no other starts and the run says why."""

    def test_the_topics_are_read_a_few_at_a_time(self):
        topics = [self.topic(f"老话题 {n}") for n in range(6)]
        self.see(lambda: topics)
        lock, now_, most = threading.Lock(), [0], [0]
        plain = self.ask

        def ask(system, data, where, limit):
            if system != sorter.GIST_SYSTEM:
                return plain(system, data, where, limit)
            with lock:
                now_[0] += 1
                most[0] = max(most[0], now_[0])
            time.sleep(0.05)
            with lock:
                now_[0] -= 1
            return plain(system, data, where, limit)
        with mock.patch.object(sorter, "ask", side_effect=ask):
            self.tidy.start(self.ctx, origin="test", wait=True)
        self.assertEqual(sum(1 for topic in topics if topic_gist.read(self.state, topic).get("essence")), 6)
        self.assertEqual(most[0], tidy_run.READS_AT_ONCE)
        self.assertEqual(len(self.judged()[-1]["topics"]), 6)

    def test_a_read_that_finds_no_model_stops_the_others_from_starting(self):
        topics = [self.topic(f"老话题 {n}") for n in range(8)]
        self.see(lambda: topics)
        calls = []

        def ask(system, data, where, limit):
            calls.append(system)
            raise Failed("quota_exhausted", "quota exceeded")
        with mock.patch.object(sorter, "ask", side_effect=ask):
            self.tidy.start(self.ctx, origin="test", wait=True)
        self.assertLessEqual(len(calls), tidy_run.READS_AT_ONCE)
        self.assertNotIn(sorter.JUDGE_SYSTEM, calls)
        view = read_object(next((proposals.folder(self.state) / "runs").glob("*.json")))["view"]
        self.assertEqual((view["state"], view["why_code"]), ("failed", "quota_exhausted"))
        self.assertFalse(any(topic_gist.read(self.state, topic).get("unreadable") for topic in topics))


class WhyUnreadTest(UpCase):
    def test_a_refusal_and_a_topic_too_long_are_told_apart_from_other_exits(self):
        """The owner, 2026-11-18: “读不了”的话题，要写清为什么 — Claude Code says a refusal only on stdout."""
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 stop_reason=refusal")), "policy_refusal")
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 Prompt is too long")), "too_long")
        self.assertEqual(sorter.why_failed(Failed("exit_nonzero", "code=1 boom")), "exit_nonzero")
        self.assertEqual(sorter.why_failed(Failed("timeout")), "timeout")
        for reason in ("refused", "policy_refusal", "too_long", "exit_nonzero", "timeout", "model_error", "empty", "redacted"):
            self.assertIn(reason, tidy_run.UNREADABLE_WORDS)


class MomentTest(TidyCase):
    def test_the_digest_times_start_one_whole_tidy_each(self):
        self.filler()
        self.tidy.tick(self.ctx)  # The cursor.
        clock = datetime.datetime.fromtimestamp(time.time() - 60, digest.ZONE)  # The digest times are Asia/Shanghai's.
        with mock.patch.dict(os.environ, {"COVEY_DIGEST_TIMES": f"{clock.hour:02d}:{clock.minute:02d}"}):
            self.tidy.tick(self.ctx)
            self.tidy.tick(self.ctx)
        runs = list((proposals.folder(self.state) / "runs").glob("*.json"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(read_object(runs[0])["origin"], "scheduled")
        self.assertFalse(self.tidy.shared.busy.locked())

    def test_with_the_assist_model_off_the_digest_times_start_no_tidy_and_are_not_tried_again_until_it_is_back(self):
        from covey.app import assist
        from covey.core import butler_settings

        self.filler()
        self.tidy.tick(self.ctx)  # The cursor.
        self.addCleanup(assist.forget)
        butler_settings.update(self.state, assist=butler_settings.AssistChoice(off=True))
        assist.forget()
        now = time.time()
        clock = datetime.datetime.fromtimestamp(now - 60, digest.ZONE)
        runs = proposals.folder(self.state) / "runs"
        with mock.patch.dict(os.environ, {"COVEY_DIGEST_TIMES": f"{clock.hour:02d}:{clock.minute:02d}"}), \
                mock.patch.object(self.tidy.runs, "start", wraps=self.tidy.runs.start) as start, \
                mock.patch.object(self.tidy.runtime, "log") as log:
            self.tidy.tick(self.ctx, now)
            self.tidy.tick(self.ctx, now + 20)
            start.assert_not_called()
            self.assertEqual(list(runs.glob("*.json")) if runs.exists() else [], [])
            self.assertEqual(self.sent_cards(), [])
            self.assertEqual(self.asked, [])
            slot = proposals.load(self.state)["last_slot"]  # Marked once: no second try at the same time.
            self.assertEqual([call.args[0] for call in log.call_args_list if "scheduled skipped" in call.args[0]],
                             [f"project tidy scheduled skipped slot={slot} reason=none"])
            butler_settings.update(self.state, assist=butler_settings.AssistChoice())
            assist.forget()
            self.tidy.tick(self.ctx, now + 40)  # Back on, but this time was given up.
            start.assert_not_called()
            self.tidy.tick(self.ctx, now + 86400)  # The next day's time runs as always.
            start.assert_called_once()
            self.assertEqual(start.call_args.kwargs["origin"], "scheduled")
        (run,) = runs.glob("*.json")
        self.assertEqual(read_object(run)["origin"], "scheduled")
        self.assertFalse(self.tidy.shared.busy.locked())

    def test_the_butlers_topic_card_is_drawn_again_with_the_line(self):
        from covey.core import butler_opened

        topic = self.topic(A)
        request = "req-1"
        data = read_object(self.state / "topics" / topic / "topic.json")
        write_json(self.state / "topics" / topic / "topic.json", {**data, "origin": {"title": A, "request": request}},
                   ensure_ascii=False)
        path = butler_opened.record_path(self.state, SimpleNamespace(id=request))
        write_json(path, {"topic_id": topic, "card_id": "card-topic", "title": A, "sequence": 2}, ensure_ascii=False)
        pid = self.project("DataLab Prompt 优化", [self.topic("一轮")])
        self.tidy.applier.join(self.ctx, proposals.new_proposal("join", topics=[topic], project=pid), CHAT)
        call = next(call for call in self.put.call_args_list if call.args[1] == "card-topic")
        self.assertIn("已归进专项「DataLab Prompt 优化」", text(call.args[2]))
        self.assertEqual(call.kwargs["sequence"], 3)

    def test_a_gist_copied_with_its_labels_reads_the_same(self):
        self.assertEqual(topic_gist.parse({"essence": "本质：" + B, "goal": "现在的目标：跑 90 次"})["goal"], "跑 90 次")
        self.assertEqual(topic_gist.parse({"本质": "本质：" + B})["essence"], B)


class OffTest(TidyCase):
    def test_with_the_switch_off_nothing_is_read_judged_listed_or_pushed(self):
        topic = self.topic(A, gist={"essence": A})
        for environ in ({proposals.ENV: "off"}, {proposals.ENV: "suggest", projects.ENV: "0"}, {proposals.ENV: "yes"}):
            with mock.patch.dict(os.environ, environ):
                self.assertFalse(proposals.enabled())
                self.assertEqual(self.tidy.threads(), ())
                self.tidy.tick(self.ctx)
                self.assertEqual(proposals.load(self.state).get("cursor"), None)
                self.assertEqual(self.tidy.start(self.ctx), tidy_run.OFF_WORDS)
                self.assertEqual(Ops(self.env).run({"kind": "tidy", "op_id": "o2", "payload": {}})[0], "failed")
                self.assertNotIn("projects_tidy", [tool["name"] for tool in butler_tool_specs.listed(dict(os.environ))])
                from covey.app.web_collect import Collector
                self.assertIsNone(Collector.gist(self.state, topic))
        self.assertEqual(self.asked, [])

    def test_the_code_default_is_suggest_and_install_writes_suggest(self):
        from covey.service import installer

        with mock.patch.dict(os.environ, {proposals.ENV: "off", projects.ENV: "1"}):
            self.assertEqual(proposals.mode(), proposals.OFF)
        with mock.patch.dict(os.environ, {proposals.ENV: "", projects.ENV: "0"}):
            self.assertEqual(proposals.mode(), proposals.OFF)  # 专项 off turns it off too.
        clean = {name: value for name, value in os.environ.items() if name not in cenv.spellings(proposals.ENV) + cenv.spellings(projects.ENV)}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(proposals.mode(), proposals.SUGGEST)  # Unset is suggest.
        with mock.patch.dict(os.environ, {proposals.ENV: "", projects.ENV: "1"}):
            self.assertEqual(proposals.mode(), proposals.SUGGEST)
        ins = SimpleNamespace(env={"COVEY_PROJECT_TIDY": "off"}, feishu_sdk=lambda: True)
        installer.Installer.default_switches(ins)
        self.assertEqual(ins.env, {**installer.INSTALL_SWITCHES, "COVEY_PROJECT_TIDY": "off"})
        ins = SimpleNamespace(env={}, feishu_sdk=lambda: True)
        installer.Installer.default_switches(ins)
        self.assertEqual(ins.env, installer.INSTALL_SWITCHES)

    def test_install_keeps_a_switch_turned_off_in_the_shell(self):
        # The installer's environment is canonical (core/env.py): `export COVEY_PROJECTS=0` reaches it as
        # LARK_BRIDGE_PROJECTS, and the default must not win over it in the saved manifest.
        from covey.service import installer
        from covey.service.manager import runtime_environment

        shell = {"COVEY_PROJECTS": "0", "COVEY_PROJECT_TIDY": "off"}
        ins = SimpleNamespace(env=cenv.canonical(shell), feishu_sdk=lambda: True)
        installer.Installer.default_switches(ins)
        saved = runtime_environment(ins.env)
        self.assertEqual(saved["LARK_BRIDGE_PROJECTS"], "0")
        self.assertEqual(saved["LARK_BRIDGE_PROJECT_TIDY"], "off")


class NameTest(TidyCase):
    def test_a_name_is_the_essence_without_numbers_or_dates_at_most_twelve(self):
        self.assertEqual(proposals.clean_name("T488 Bootstrap 数据包提效 2026-11-18"), "Bootstrap 数据包提效")
        self.assertLessEqual(proposals.name_width(proposals.clean_name("一个非常非常非常非常长的专项名字超过十二个字")), 12)
        self.assertEqual(Path(proposals.state_path(self.state)).parent.name, "tidy")


class TidyModelTest(UpCase):
    """整理 reads whole conversations and names what a matter really is: Sonnet by default, not the small judge's Haiku."""

    def test_the_limits_leave_room_for_the_followed_budget(self):
        """T924: on the butler's budget a judgment took 12 minutes (gemini-4-neon, xhigh) and a gist up to 2: the limits
        guard against a hang, they are no deadline for a slow model."""
        self.assertGreaterEqual(proposals.number("judge_seconds", {}), 900)
        self.assertGreaterEqual(proposals.number("read_seconds", {}), 240)
        self.assertEqual(proposals.number("judge_seconds", {"COVEY_PROJECT_TIDY_JUDGE_SECONDS": "90"}), 90)

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

        ctx = SimpleNamespace(state_dir=self.state, backend_cmd=["claude"], options={})
        topics = claude_topics(self.state, SimpleNamespace(), ctx)
        with mock.patch.dict(os.environ, {"COVEY_PROJECT_TIDY_MODEL": "opus"}), \
                mock.patch.object(sorter.track_judge, "claude_argv", argv), \
                mock.patch.object(sorter.track_judge, "run", return_value=envelope({"suggestions": []})):
            sorter.ask("system", "data", sorter.Where(topics), 5)
        self.assertEqual("opus", seen.get("model"))

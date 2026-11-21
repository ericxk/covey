"""话题区 (docs/butler.md 话题, the owner's choice of 2026-11-07 noon): a topic the butler opens hangs its
thread under the owner's question; a second one on the same question gets a topic card at the top level as its root;
the relay is a message of its own at the top level and is the topic's; quoting the card handed on to one topic alone goes to that topic. main-thread.json's
desk_topics, retired on 2026-11-09, opens no front-desk topic."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_job
from test_compat_desk_topics import OWNER, DeskCase
from test_butler_topics import CHAT

from covey.app import answer_cards, butler_relay, relay_targets
from covey.cards import relay_card
from covey.app.artifacts import answer_files, artifact_key
from covey.cards.answer_card import ARTIFACTS_ID, tail
from covey.cards.review import answer_card
from covey.app.question_display import standalone_card
from covey.app.prompts import BUTLER_RULES_TEXT, butler_message
from covey.core import butler, butler_opened, butler_settings
from covey.core.jobs import job_key, upsert_job_record
from covey.core.butler import TOPIC
from covey.core.state import read_object, write_json
from covey.app.butler import Butler
from covey.core.topics import (context_for_topic, load_topic, message_topic, remember_topic_message, route_message,
                                        routed_topic, topic_key)
from covey.feishu.message_request import reply_in_thread

BUTLER_CARD = "om_main_card"  # The butler job's card, quoting the owner's question at the top level.


class AreaCase(DeskCase):
    def setUp(self):
        super().setUp()
        upsert_job_record(self.ctx.state_dir, "main-job", card_id="card-main", card_message_id=BUTLER_CARD)


class OpenTest(AreaCase):
    def test_the_thread_hangs_under_the_owners_question(self):
        data, job = self.opened()
        self.app.messenger.create_and_send_card.assert_not_called()  # No topic card.
        self.assertEqual(data["root_message_id"], OWNER)
        self.assertEqual(data["topic_id"], topic_key(CHAT, OWNER))
        self.assertIs(butler.traits(data["topic_id"], CHAT), TOPIC)
        topic = load_topic(self.ctx.state_dir, data["topic_id"])
        self.assertEqual(topic["root_id"], OWNER)
        self.assertNotIn("quote", topic)
        # The question is the topic's now: replies to it go in its thread, quotes of it to the topic.
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, OWNER), data["topic_id"])
        self.assertEqual((job.topic_id, job.reply_to_message_id), (data["topic_id"], OWNER))
        self.assertTrue(reply_in_thread(CHAT, OWNER))
        self.assertIn("话题串挂在主人这条提问下面", self.start("open")["text"])

    def test_a_second_topic_on_the_same_question_gets_a_card_root(self):
        first, _ = self.opened("one")
        answer = self.start("two", title="改看板")
        self.assertTrue(answer["ok"], answer)
        self.app.messenger.create_and_send_card.assert_called_once()  # A topic card at the top level.
        self.assertTrue(answer["data"]["root_message_id"].startswith("om_root"))
        self.assertNotEqual(answer["data"]["topic_id"], first["topic_id"])
        self.assertIn("根是顶层一张小卡", answer["text"])
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, OWNER), first["topic_id"])

    def test_the_place_is_decided_once(self):
        record = butler_opened.record_path(self.ctx.state_dir, SimpleNamespace(id="once"))
        self.opened("once")
        saved = read_object(record)
        self.assertEqual((saved["area_root"], saved["area_taken"]), (OWNER, False))
        self.assertNotIn("desk", saved)

    def test_desk_topics_left_true_opens_a_topic_area_all_the_same(self):
        # Retired on 2026-11-09 (the owner: 下线): front-desk topics opened as before no more.
        (self.ctx.state_dir / butler_settings.FILE).write_text(json.dumps({"mode": "agent", "desk_topics": True}))
        data, _ = self.opened("desk")
        self.assertIs(butler.traits(data["topic_id"], CHAT), TOPIC)
        self.assertNotIn("desk", load_topic(self.ctx.state_dir, data["topic_id"])["origin"])
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, OWNER), data["topic_id"])
        self.assertFalse((self.ctx.state_dir / "main-thread" / "desk").exists())  # No registry entry either.


class RelayTest(AreaCase):
    def opened_turn(self, request="open", **timings):
        data, job = self.opened(request)
        token = butler_opened.request_token(SimpleNamespace(id=request))
        turn = make_job(job_id=job.job_id, chat_id=CHAT, message_id="main-" + token, topic_id=data["topic_id"],
                        timings=timings)
        return data, turn

    def test_it_quotes_the_main_card_and_is_the_topics(self):
        data, turn = self.opened_turn()
        target = relay_targets.relay_target(self.ctx.state_dir, turn)
        self.assertEqual((target["reply_to"], target["topic_id"], target["main_key"]), (BUTLER_CARD, data["topic_id"],
                                                                                         "main-job"))

    def test_the_second_topic_of_a_message_links_its_you_asked_to_its_own_root(self):
        # S6 (T444, 2026-11-08): both relays linked to the question's thread, which is the first topic's.
        upsert_job_record(self.ctx.state_dir, "main-job", user_text="A 和 B 两件事")
        first, first_turn = self.opened_turn("one")
        second, second_turn = self.opened_turn("two")
        self.assertTrue(second["root_message_id"].startswith("om_root"))
        links = []
        with mock.patch("covey.app.topic_links.topic_thread_link",
                        side_effect=lambda state, topic, **kwargs: links.append(topic) or ("https://link", "thread")):
            for turn in (first_turn, second_turn):
                block = relay_targets.ask_for(self.ctx.state_dir, turn, relay_targets.relay_target(self.ctx.state_dir, turn))
                self.assertIn("A 和 B 两件事", str(block))  # The words are the owner's question either way.
        # Each relay opens its own topic's thread (2026-11-10: 都能跳转到话题上); the second topic's root is its own.
        self.assertEqual(links, [first["topic_id"], second["topic_id"]])

    def test_threaded_a_quick_answer_is_a_message_of_its_own_quoting_no_card(self):
        _, turn = self.opened_turn(backend_requested=100.0, execution_finished=130.0)
        send = mock.Mock(side_effect=lambda chat, text, key, reply_to, card, on_sent: on_sent("om_relay") or True)
        self.assertEqual("sent", butler_relay.relay(self.ctx.state_dir, turn, relay_card.DONE, send=send, text="答案",
                                                  at=130.0, took=30.0, artifacts=0, inputs=["Claude Code"], outputs=[]))
        self.assertEqual(send.call_args.args[3], "")


class SettleTest(AreaCase):
    """after_runner: the butler's card handed on to one topic alone routes quotes to it."""

    def test_a_card_handed_on_to_one_topic_routes_quotes_to_it(self):
        data, _ = self.opened()
        upsert_job_record(self.ctx.state_dir, "main-job", status="done")
        gateway = SimpleNamespace(apps=self.app.topics.apps, runtime=self.app.runtime)
        butler_job = SimpleNamespace(message_id="main-job", job_id=1, chat_id=CHAT, topic_id=self.main)
        messenger = self.app.topics.apps["claude"].messenger
        with mock.patch.object(messenger, "send_message") as send:
            butler_relay.after_runner(gateway, butler_job, self.ctx)
        send.assert_not_called()
        # Quoting it goes there, a reply to it stays the butler's.
        self.assertEqual(routed_topic(self.ctx.state_dir, CHAT, BUTLER_CARD), data["topic_id"])
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, BUTLER_CARD), self.main)
        quote = {"chat_id": CHAT, "message_id": "om_quote", "root_id": OWNER, "parent_id": BUTLER_CARD}
        self.assertEqual(context_for_topic(self.ctx, quote, lambda w: w).topic_id, data["topic_id"])

    def test_a_topic_turn_routes_nothing(self):
        data, job = self.opened()
        turn = make_job(job_id=job.job_id, chat_id=CHAT, message_id="om_next", topic_id=data["topic_id"])
        gateway = SimpleNamespace(apps=self.app.topics.apps, runtime=self.app.runtime)
        butler_relay.after_runner(gateway, turn, self.ctx)
        self.assertEqual(routed_topic(self.ctx.state_dir, CHAT, BUTLER_CARD), "")

    def test_in_the_mode_topics_a_turn_is_not_relayed(self):
        data, job = self.opened()
        butler_settings.update(self.ctx.state_dir, enabled=False)
        turn = make_job(job_id=job.job_id, chat_id=CHAT, message_id="om_next", topic_id=data["topic_id"],
                        timings={"backend_requested": 1.0, "execution_finished": 200.0})
        send = mock.Mock()
        self.assertIsNone(butler_relay.relay(self.ctx.state_dir, turn, relay_card.DONE, send=send, text="答案", at=200.0,
                                           took=199.0, artifacts=0))
        send.assert_not_called()
        self.assertTrue(reply_in_thread(CHAT, OWNER))  # Replies go in threads again, the butler's too.

    def test_a_quick_relay_is_held_no_more(self):
        # The desk_topics layout's hold (a quick turn the card handed on to alone, written on it) went with it.
        data, job = self.opened()
        token = butler_opened.request_token(SimpleNamespace(id="open"))
        quick = make_job(job_id=job.job_id, chat_id=CHAT, message_id="main-" + token, topic_id=data["topic_id"],
                         timings={"backend_requested": 100.0, "execution_finished": 110.0})
        upsert_job_record(self.ctx.state_dir, "main-job", status="running")
        send = mock.Mock(side_effect=lambda chat, text, key, reply_to, card, on_sent: on_sent("om_quick") or True)
        self.assertEqual("sent", butler_relay.relay(self.ctx.state_dir, quick, relay_card.DONE, send=send, text="快",
                                                  at=110.0, took=10.0, artifacts=0))
        self.assertEqual(send.call_args.args[3], "")

    def test_a_card_handed_on_to_two_topics_stays_the_butlers(self):
        self.opened("one")
        self.opened("two")
        upsert_job_record(self.ctx.state_dir, "main-job", status="done")
        gateway = SimpleNamespace(apps=self.app.topics.apps, runtime=self.app.runtime)
        butler_relay.after_runner(gateway, SimpleNamespace(message_id="main-job", job_id=1, chat_id=CHAT, topic_id=self.main),
                                self.ctx)
        self.assertEqual(routed_topic(self.ctx.state_dir, CHAT, BUTLER_CARD), "")


class QuoteTest(AreaCase):
    def test_quoting_a_card_routed_to_an_appended_area_goes_there_and_its_card_goes_in_its_thread(self):
        data, _ = self.opened()
        # A later question the butler appended to this area (topic_send): its card is routed, the question is
        # the butler's.
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, "om_q2")
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, "om_card2")
        route_message(self.ctx.state_dir, CHAT, "om_card2", data["topic_id"], holder=self.main)
        event = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_follow", "root_id": "om_q2",
                 "parent_id": "om_card2"}
        thread = Butler(self.app.topics)
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIsNone(thread.app_for(self.ctx, event, True))  # Not the butler's.
        ctx = context_for_topic(self.ctx, event, lambda w: w)
        self.assertEqual((ctx.topic_id, ctx.reply_to_message_id), (data["topic_id"], OWNER))  # In its thread.
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "om_q2"), self.main)
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "om_card2"), self.main)
        # A message in its thread is the area's, as ever.
        inside = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_in", "root_id": OWNER, "parent_id": OWNER,
                  "thread_id": "omt_1"}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIsNone(thread.app_for(self.ctx, inside, True))
        self.assertEqual(context_for_topic(self.ctx, inside, lambda w: w).topic_id, data["topic_id"])


    def test_quoting_the_main_card_in_a_thread_goes_to_the_topic_it_handed_on_to(self):
        # S8 (T444, 2026-11-08): the butler's card is in the owner's message's thread, so a quote of it carries
        # a thread_id; it went to the butler, while the rest of that thread went to the topic.
        data, _ = self.opened()
        thread = Butler(self.app.topics)
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, BUTLER_CARD)
        route_message(self.ctx.state_dir, CHAT, BUTLER_CARD, data["topic_id"], holder=self.main)
        # In the area's own thread (its root is the owner's question).
        event = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_q_in", "root_id": OWNER,
                 "parent_id": BUTLER_CARD, "thread_id": "omt_1"}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIsNone(thread.app_for(self.ctx, event, True))
        ctx = context_for_topic(self.ctx, event, lambda w: w)
        self.assertEqual((ctx.topic_id, ctx.reply_to_message_id), (data["topic_id"], OWNER))
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, BUTLER_CARD), self.main)  # Still the butler's.
        # In the thread of a later question the butler handed on to this area (topic_send): the topic's, its
        # reply in the topic's own thread; the question and its thread stay the butler's.
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, "om_q2")
        remember_topic_message(self.ctx.state_dir, self.main, CHAT, "om_card2")
        route_message(self.ctx.state_dir, CHAT, "om_card2", data["topic_id"], holder=self.main)
        handed = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_q2_in", "root_id": "om_q2",
                  "parent_id": "om_card2", "thread_id": "omt_2"}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIsNone(thread.app_for(self.ctx, handed, True))
        ctx = context_for_topic(self.ctx, handed, lambda w: w)
        self.assertEqual((ctx.topic_id, ctx.reply_to_message_id), (data["topic_id"], OWNER))
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "om_q2_in"), data["topic_id"])
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "om_q2"), self.main)
        self.assertEqual(message_topic(self.ctx.state_dir, CHAT, "omt_2"), "")
        # The mode 纯话题: a thread stays its root's, as before.
        with mock.patch.object(butler, "enabled", return_value=False):
            off = context_for_topic(self.ctx, {**handed, "message_id": "om_q2_off"}, lambda w: w)
        self.assertEqual(off.topic_id, self.main)
        # Not quoting anything there is the butler's, as before (left to the owner, VERIFY-edges S8 point 2).
        plain = {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_q2_plain", "root_id": "om_q2",
                 "parent_id": "om_q2", "thread_id": "omt_2"}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertIsNotNone(thread.app_for(self.ctx, plain, True))


class EveryTurnTest(AreaCase):
    def test_a_turn_no_request_started_relays_through_the_latest_request_that_touched_its_area(self):
        data, job = self.opened()
        # The owner answered its question (or wrote in its thread): that turn is no request's.
        answered = make_job(job_id=job.job_id + 1, chat_id=CHAT, message_id="answer-tok", topic_id=data["topic_id"],
                            timings={"backend_requested": 1.0, "execution_finished": 5.0})
        target = relay_targets.relay_target(self.ctx.state_dir, answered)
        self.assertEqual((target["kind"], target["reply_to"], target["main_key"]), ("follow", BUTLER_CARD, "main-job"))
        # A topic no request touched is not relayed.
        self.assertIsNone(relay_targets.area_target(self.ctx.state_dir, "f" * 64, CHAT))


class MirrorCase(AreaCase):
    def setUp(self):
        super().setUp()
        self.data, self.job = self.opened()
        self.topic = self.data["topic_id"]
        harness = load_topic(self.ctx.state_dir, self.topic)["harness_id"]
        self.app_ = self.app.topics.apps[harness]
        self.tctx = context_for_topic(self.app_.harness.context, {"chat_id": CHAT, "message_id": OWNER,
                                                                  "root_id": OWNER}, lambda w: w)
        self.path = self.ctx.state_dir / "questions" / "qtok.json"
        self.record = {"token": "qtok", "topic_id": self.topic, "chat_id": CHAT, "job_id": self.job.job_id,
                       "status": "pending", "blocking": True, "created_at": 1.0,
                       "questions": [{"id": "q1", "question": "用哪个做法？", "options": [{"label": "A"}, {"label": "B"}]}]}
        write_json(self.path, self.record)


class MirrorTest(MirrorCase):
    def test_the_question_goes_up_once_with_the_output_and_folds_with_the_one_in_the_topic(self):
        display = self.app_.questions.display
        sends = self.app.messenger.create_and_send_card
        sends.reset_mock()
        with mock.patch.object(display.runtime, "log") as log:
            mid = display.mirror(self.tctx, self.record, "**结论**：A 比 B 快。")
        self.assertTrue(mid)
        self.assertEqual([c.args[0] for c in log.call_args_list if c.args[0].startswith("main relay")],
                         [f"main relay state=sent outcome=question job=#{self.job.job_id} kind=follow token=qtok"
                          f" message={mid[-8:]}"])
        self.assertEqual(sends.call_args.kwargs["reply_to_message_id"], "")  # Its own message at the top level.
        card = json.dumps(sends.call_args.args[2], ensure_ascii=False)
        self.assertIn("A 比 B 快", card)
        self.assertIn("用哪个做法", card)
        # One single-choice question: its options are buttons, as on a front-desk card, and on the one in the topic
        # (T604); only where to reply differs.
        self.assertIn('"pick": 0', card)
        self.assertIn("引用这张卡直接回复", card)
        topic_card = json.dumps(standalone_card(self.tctx, self.record), ensure_ascii=False)
        self.assertIn('"pick": 0', topic_card)
        self.assertIn("直接在话题里回复", topic_card)
        saved = read_object(self.path)
        self.assertEqual((saved["mirror_message_id"], saved["mirror_state"]), (mid, "sent"))
        self.assertEqual(routed_topic(self.ctx.state_dir, CHAT, mid), self.topic)  # Quoting it answers there.
        self.assertEqual(display.mirror(self.tctx, saved, "again"), "")  # Once.
        self.assertEqual(sends.call_count, 1)
        # Answered: its repaint repaints the copy too.
        self.app.messenger.put_card.reset_mock()
        display.paint_mirror(self.tctx, {**saved, "status": "answered"})
        self.assertEqual(self.app.messenger.put_card.call_count, 1, [line for line in self.logs if "repaint" in line])
        self.assertIn("A 比 B 快", json.dumps(self.app.messenger.put_card.call_args.args[2], ensure_ascii=False))

    def test_the_copy_s_you_asked_is_the_owner_s_question_linking_the_second_topic_to_its_own_root(self):
        # B1 review of S6: the copy took the topic's latest request as a turn the owner started there: the turn's
        # own words, and a link to its main- id, which has none.
        upsert_job_record(self.ctx.state_dir, "main-job", user_text="A 和 B 两件事")
        answer = self.start("two")
        second, job = answer["data"], type(self).job(self, "two")  # setUp's self.job is the first topic's.
        self.assertTrue(second["root_message_id"].startswith("om_root"))
        token = butler_opened.request_token(SimpleNamespace(id="two"))
        turn = make_job(job_id=job.job_id, chat_id=CHAT, message_id="main-" + token, topic_id=second["topic_id"],
                        user_text="去做 B")
        record = {**self.record, "token": "q2", "topic_id": second["topic_id"], "job_id": job.job_id}
        write_json(self.ctx.state_dir / "questions" / "q2.json", record)
        links = []
        with mock.patch("covey.app.topic_links.topic_thread_link",
                        side_effect=lambda state, topic, **kwargs: links.append(topic) or ("https://link", "thread")):
            self.assertTrue(self.app_.questions.display.mirror(self.tctx, record, "输出", job=turn))
        ask = json.dumps(read_object(self.ctx.state_dir / "questions" / "q2.json")["mirror_ask"], ensure_ascii=False)
        self.assertIn("A 和 B 两件事", ask)
        self.assertNotIn("去做 B", ask)
        self.assertEqual(links, [second["topic_id"]])  # Its own topic's thread.
        self.assertIn("https://link", ask)

    def test_a_long_output_goes_on_its_own_first_and_a_topic_without_a_request_gets_none(self):
        display = self.app_.questions.display
        with mock.patch.object(self.app.messenger, "send_message", return_value=True) as send:
            mid = display.mirror(self.tctx, self.record, "长" * 5000)
        self.assertTrue(mid)
        self.assertEqual(send.call_args.kwargs["reply_to_message_id"], "")
        self.assertEqual(read_object(self.path)["mirror_output"], "")
        other = {**self.record, "token": "q2", "topic_id": "f" * 64}
        self.assertEqual(display.mirror(self.tctx, other, "x"), "")

    def test_a_project_topic_s_question_goes_up_though_no_request_of_the_butler_opened_it(self):
        """The owner, 2026-11-19: a question the lead handed on to him (T538's, 09:59) went to the topic's thread alone,
        and so had every question of a project's topic: they are opened by the lead, never by a request of the butler, so
        there was no 话题区 target and no copy at the top level (core/placement.py says TOP for them)."""
        from covey.core import projects
        from covey.core.topics import topic_file

        display = self.app_.questions.display
        member = "e" * 64
        write_json(topic_file(self.ctx.state_dir, member), {
            "topic_id": member, "chat_id": CHAT, "topic_number": 538, "harness_id": "claude",
            "project": {"id": "1a5e5c7675eb", "role": projects.MEMBER}})
        record = {**self.record, "token": "q-member", "topic_id": member}
        write_json(self.ctx.state_dir / "questions" / "q-member.json", record)
        self.assertEqual(display.mirror(self.tctx, record, "x"), "")  # Without COVEY_PROJECTS: a plain topic, none.
        sends = self.app.messenger.create_and_send_card
        sends.reset_mock()
        turn = make_job(job_id=self.job.job_id, chat_id=CHAT, message_id="lead-1a5e5c7675eb-3", topic_id=member,
                        user_text="[Bridge 说明] 负责人派的活")
        with mock.patch.dict("os.environ", {projects.ENV: "1"}):
            mid = display.mirror(self.tctx, record, "三张样卡", job=turn)
        self.assertTrue(mid)
        self.assertEqual(sends.call_args.kwargs["reply_to_message_id"], "")  # At the top level.
        card = json.dumps(sends.call_args.args[2], ensure_ascii=False)
        self.assertIn("用哪个做法", card)
        self.assertNotIn("负责人派的活", card)  # No "你问" block: the lead asked for this turn, not the owner.
        # Who asks all the same (样卡 B, docs/ui.md 5.3): the topic, for work its lead handed it.
        self.assertIn("**T538", card)
        self.assertIn("办负责人派的事，要你定", card)
        self.assertEqual(read_object(self.ctx.state_dir / "questions" / "q-member.json")["mirror_message_id"], mid)

    def test_a_running_turn_s_output_is_its_notes_and_the_answer_so_far(self):
        from covey.app.native_questions import turn_output
        from covey.core.trace import task_trace
        trace = task_trace()
        trace.note("n1", "A：改看板。\nB：改卡片。")  # Text before a tool: a 进展说明.
        trace.steps["t1"] = {"title": "Bash", "detail": "ls", "status": "done"}
        trace.note("agent-x", "子任务", "子任务")
        job = SimpleNamespace(tool_trace=trace, progress=SimpleNamespace(last_content=" "))
        self.assertEqual(turn_output(job), "A：改看板。\nB：改卡片。")
        job.progress.last_content = "所以先问你。"
        self.assertEqual(turn_output(job), "A：改看板。\nB：改卡片。\n\n所以先问你。")
        self.assertEqual(turn_output(SimpleNamespace(tool_trace=None, progress=None)), "")

    def test_a_waiting_turn_sends_its_questions_up_instead_of_a_line(self):
        delivery = SimpleNamespace(job=self.job, ctx=self.tctx, questions=self.app_.questions, retry=None,
                                   runtime=self.app.runtime,
                                   turn=SimpleNamespace(status="waiting_user", verified_delivery=SimpleNamespace(text="输出"),
                                                        status_message=""))
        send = mock.Mock()
        with mock.patch.object(self.app.messenger, "send_message", send):
            state = butler_relay.after_turn(delivery, True, "waiting_user", messenger=self.app.messenger,
                                          profile="p", voice=self.app_.voice)
        self.assertEqual(state, "mirrored")
        send.assert_not_called()
        saved = read_object(self.path)
        self.assertTrue(saved.get("mirror_message_id"))
        self.assertEqual(saved["mirror_output"], "输出")



class MirrorFilesTest(MirrorCase):
    """The copy shows the asking turn's 产物 as its answer card in the topic does (the owner, 2026-11-09 14:57: 选哪张图
    at the top level had no pictures), brought up to date by the upload's end and a 重新上传 through CardKit."""

    def setUp(self):
        super().setUp()
        import tempfile
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        work = Path(temp.name)
        (work / "a.png").write_bytes(b"png")
        point = work / "checkpoint.json"
        point.write_text(json.dumps({"artifacts": [str(work / "a.png")]}))
        self.key = job_key(self.job.message_id, self.job.job_id)
        self.jrecord = upsert_job_record(self.ctx.state_dir, self.key, job_id=self.job.job_id, topic_id=self.topic,
                                         chat_id=CHAT, message_id=self.job.message_id, checkpoint_path=str(point),
                                         workdir=str(work))
        self.record = {**self.record, "job_key": self.key}
        write_json(self.path, self.record)
        self.file_key = artifact_key(str(work / "a.png"))

    def ledger(self, status):
        files = {self.file_key: {"name": "a.png", "size": 3, "url": "https://drive.invalid/a.png"}} if status == "done" else {}
        write_json(self.ctx.state_dir / "artifact-drive" / (self.app_.voice.safe_key(self.key) + ".json"),
                   {"status": status, "files": files, "folder_url": "https://drive.invalid/folder"})

    def answer_card(self):
        panel = answer_files(self.ctx.state_dir, self.jrecord, self.topic, self.app_.voice)
        answer_cards.register(self.ctx.state_dir, self.key, "answer", "om_answer", answer_card("好了", "s", tail(panel, ["a"], [])),
                              "uploading")

    def test_the_copy_shows_the_turn_s_files_under_its_output(self):
        self.ledger("done")
        self.answer_card()
        sends = self.app.messenger.create_and_send_card
        mid = self.app_.questions.display.mirror(self.tctx, self.record, "选哪张图？")
        built = sends.call_args.args[2]
        elements = built["body"]["elements"]
        self.assertEqual([e.get("element_id") for e in elements][1], ARTIFACTS_ID)  # Right under the output.
        self.assertIn("https://drive.invalid/a.png", json.dumps(built, ensure_ascii=False))
        self.assertTrue(answer_cards.clicked(self.ctx.state_dir, self.key, mid))  # Its 重新上传 is the answer cards'.
        self.assertFalse(answer_cards.holds_files(self.ctx.state_dir, "other"))

    def test_a_long_output_s_copy_keeps_the_files(self):
        self.ledger("done")
        self.answer_card()
        with mock.patch.object(self.app.messenger, "send_message", return_value=True):
            self.app_.questions.display.mirror(self.tctx, self.record, "长" * 5000)
        built = self.app.messenger.create_and_send_card.call_args.args[2]
        self.assertEqual(built["body"]["elements"][0].get("element_id"), ARTIFACTS_ID)

    def test_none_without_an_answer_card_showing_them_nor_at_the_top_level_itself(self):
        self.ledger("done")
        self.app_.questions.display.mirror(self.tctx, self.record, "选哪张图？")  # A question mid-turn: no answer yet.
        self.assertNotIn(ARTIFACTS_ID, json.dumps(self.app.messenger.create_and_send_card.call_args.args[2]))
        self.answer_card()
        quiet = {**self.record, "at_top": True, "mirror_state": "at_top", "question_message_id": "om_q"}
        self.assertEqual(self.app_.questions.display.copy_files(self.tctx, quiet), (None, None))
        self.assertNotIn(ARTIFACTS_ID, json.dumps(standalone_card(self.tctx, quiet)))
        old = {key: value for key, value in self.record.items() if key != "job_key"}  # A record from before.
        self.assertEqual(self.app_.questions.display.copy_files(self.tctx, old), (None, None))

    def test_the_upload_s_end_repaints_the_copy_whole_and_an_answered_form_stays_folded(self):
        self.ledger("uploading")
        self.answer_card()
        display = self.app_.questions.display
        mid = display.mirror(self.tctx, self.record, "选哪张图？")
        self.assertIn("上传中", json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False))
        answered = {**read_object(self.path), "status": "answered", "continuation_job_id": 99}
        write_json(self.path, answered)
        put = self.app.messenger.put_card
        put.reset_mock()
        self.app.messenger.patch_card = mock.Mock(return_value=True)
        self.ledger("done")
        self.app_.artifacts.refresh_answer_cards(self.tctx, self.jrecord)
        self.assertEqual(put.call_count, 1)
        painted = put.call_args.args[2]
        text = json.dumps(painted, ensure_ascii=False)
        self.assertIn("https://drive.invalid/a.png", text)
        self.assertNotIn("上传中", text)
        self.assertNotIn('"pick"', text)  # Answered: folded as the card in the topic.
        self.assertEqual(json.dumps(painted["body"]["elements"][-1]),
                         json.dumps(standalone_card(self.tctx, answered)["body"]["elements"][-1]))
        self.assertEqual([c.args[1] for c in self.app.messenger.patch_card.call_args_list], ["om_answer"])
        self.assertEqual(answer_cards.copies(self.ctx.state_dir, self.key, "done"), [])
        self.app_.artifacts.refresh_answer_cards(self.tctx, self.jrecord)
        self.assertEqual(put.call_count, 1)  # Already up to date.
        self.app_.artifacts.refresh_answer_cards(self.tctx, self.jrecord, force=True)
        self.assertEqual(put.call_count, 2)
        self.assertTrue(mid)

    def test_an_upload_ending_while_the_copy_goes_out_is_painted_once_it_is_out(self):
        self.ledger("uploading")
        self.answer_card()
        sent = self.app.messenger.create_and_send_card.side_effect

        def racing(*args, **kwargs):
            out = sent(*args, **kwargs)
            self.ledger("done")
            self.app_.artifacts.refresh_answer_cards(self.tctx, self.jrecord)  # Before the copy is listed.
            return out
        self.app.messenger.create_and_send_card.side_effect = racing
        self.app.messenger.patch_card = mock.Mock(return_value=True)
        put = self.app.messenger.put_card
        put.reset_mock()
        self.app_.questions.display.mirror(self.tctx, self.record, "选哪张图？")
        self.assertEqual(put.call_count, 1)
        self.assertNotIn("上传中", json.dumps(put.call_args.args[2], ensure_ascii=False))

    def test_retry_on_the_copy_uploads_again_and_never_touches_the_task_card(self):
        self.ledger("failed")
        self.answer_card()
        mid = self.app_.questions.display.mirror(self.tctx, self.record, "选哪张图？")
        self.assertIn("重新上传", json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False))
        artifacts = self.app_.artifacts
        event = {"chat_id": CHAT, "message_id": mid, "event_id": "ev1",
                 "action_value": {"kind": "bridge_artifact_upload", "topic_id": self.topic, "job_id": self.job.job_id}}
        with mock.patch.object(artifacts, "in_background") as background, \
                mock.patch.object(artifacts.cards, "update_record_panel") as task_card, \
                mock.patch.object(artifacts.harness, "topic_records", return_value=[self.jrecord]):
            self.assertTrue(artifacts.artifact_action(self.tctx, event))
        task_card.assert_not_called()
        again = background.call_args.args[0]
        put = self.app.messenger.put_card
        put.reset_mock()
        self.app.messenger.patch_card = mock.Mock(return_value=True)

        def upload(ctx, record):
            self.ledger("done")
        with mock.patch.object(artifacts, "upload_artifacts", side_effect=upload):
            again()
        self.assertEqual(put.call_count, 1)
        self.assertIn("https://drive.invalid/a.png", json.dumps(put.call_args.args[2], ensure_ascii=False))


    def test_an_answer_on_the_copy_repaints_the_copy_first(self):
        """The owner sees the card he clicked change one CardKit write (~0.3 s) sooner; any other click, or a paint
        off a click, keeps the order: the card in the topic first."""
        from covey.app import card_lanes
        display, order = self.app_.questions.display, []
        record = {**self.record, "mirror_message_id": "om_copy", "question_message_id": "om_form"}
        with mock.patch.object(display, "paint_mirror", side_effect=lambda ctx, r: order.append("copy")), \
                mock.patch.object(display.questions, "paint_standalone_question",
                                  side_effect=lambda ctx, r: order.append("topic")):
            for clicked, expected in (("om_copy", ["copy", "topic"]), ("om_form", ["topic", "copy"]),
                                      ("", ["topic", "copy"])):
                order.clear()
                card_lanes._click.message = clicked
                try:
                    display.paint(self.tctx, record)
                finally:
                    card_lanes._click.message = ""
                self.assertEqual(order, expected, clicked)

    def test_clicks_redrawing_the_copy_keep_its_files(self):
        """An empty 只发补充说明 (refused on the card while the question waits), 查看提交状态 and a click on a folded
        question each redraw the clicked copy: the pictures stay on it."""
        from covey.cards.question_cards import FOLDED, SKIP_ALL_KIND
        self.ledger("done")
        self.answer_card()
        token = "0123456789abcdef"  # A click's token is checked.
        self.path = self.ctx.state_dir / "questions" / (token + ".json")
        write_json(self.path, {**self.record, "token": token})
        mid = self.app_.questions.display.mirror(self.tctx, read_object(self.path), "选哪张图？")
        interaction = self.app_.interaction

        def click(kind, fields=None):
            return interaction.interaction_action(self.tctx, {
                "chat_id": CHAT, "event_id": "ev-" + kind, "message_id": mid, "form_value": fields or {},
                "action_value": {"kind": kind, "topic_id": self.topic, "token": token}})
        with mock.patch.object(interaction.cards, "repaint_browse_card", return_value=True) as repaint:
            click(SKIP_ALL_KIND, {"supplement": " "})
            refused = json.dumps(repaint.call_args.args[4], ensure_ascii=False)
            self.assertIn("补充说明是空的", refused)
            self.assertIn("https://drive.invalid/a.png", refused)
            self.assertEqual(read_object(self.path)["status"], "pending")
            write_json(self.path, {**read_object(self.path), "status": "superseded", "superseded_reason": FOLDED,
                                   "superseded_by_job": 7})
            click("bridge_question", {"option_0": "0"})
            self.assertIn("#7 又提了问题", json.dumps(repaint.call_args.args[4], ensure_ascii=False))
            self.assertIn(ARTIFACTS_ID, json.dumps(repaint.call_args.args[4]))
        with mock.patch.object(interaction.messenger, "publish_card") as publish:
            click("bridge_question_status")
        self.assertIn(ARTIFACTS_ID, json.dumps(publish.call_args.args[3]))

    def test_the_butler_s_own_question_s_copy_leaves_them_to_its_answer_card(self):
        """The butler's own turn answers at the top level, its 产物 there: its question's copy has none."""
        self.ledger("done")
        self.answer_card()
        own = {**self.record, "topic_id": butler.butler_topic_id(CHAT)}
        self.assertEqual(self.app_.questions.display.copy_files(self.tctx, own), (None, None))
        self.assertIsNotNone(self.app_.questions.display.copy_files(self.tctx, self.record)[0])

    def test_a_repaint_lists_the_copy_again_only_when_it_changed(self):
        answer_cards.register_copy(self.ctx.state_dir, self.key, "om_copy", "qtok", "uploading")
        with mock.patch.object(answer_cards, "write_json") as write:
            answer_cards.register_copy(self.ctx.state_dir, self.key, "om_copy", "qtok", "uploading")
            write.assert_not_called()
            answer_cards.register_copy(self.ctx.state_dir, self.key, "om_copy", "qtok", "done")
            write.assert_called_once()


class RulesTest(AreaCase):
    def test_the_rules_say_by_which_matter(self):
        self.assertIn("放哪儿按“是哪件事”分，不按它会跑多久", BUTLER_RULES_TEXT)
        self.assertIn("AskUserQuestion", BUTLER_RULES_TEXT)
        self.assertIn("放到 T432「标题」里做", butler_message("快照", "消息"))

    def test_the_opening_says_the_chat_area_and_no_rule_says_the_top_level_or_one_card(self):
        """T444 S2: the 话题区 rules say the chat area and a new message."""
        rules = butler_message("快照", "消息").split("\n\n")[0]
        for stale in ("顶层", "一分钟内", "一张回复卡", "写在你这张卡上"):
            self.assertNotIn(stale, rules)
        self.assertIn("主人在私聊聊天区发的每条消息是你的一轮", rules)
        self.assertIn("Bridge 把它作为聊天区的一条新消息发出", rules)
        self.assertNotIn("显示在你这张卡上", rules)
        self.assertIn("主人原话和附件 Bridge 会自动附上", rules)

    def test_tool_notes_are_in_the_owners_language_everywhere(self):
        """T444 V11: commands' and subtasks' descriptions show on the card's 执行过程."""
        from covey.app.prompts import RESUME_REMINDERS, constraints
        for text in (constraints("Claude"), RESUME_REMINDERS, butler_message("快照", "消息")):
            self.assertIn("工具调用里写给人看的说明（命令和子任务的 description、待办条目）也用用户提问的语言写", text)

if __name__ == "__main__":
    import unittest
    unittest.main()

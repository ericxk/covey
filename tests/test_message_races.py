"""并发时机与消息归属 (T957): an owner's message at the top level of the private chat meets a turn the Bridge started for
him (话题转交管家, "↪ T12 转给管家"), a turn queued while the message's attachments were read, or several messages in one
butler turn; it goes where the owner meant it, and what a topic gets of such a turn is the message it is for.

The incident (2026-11-28 22:05): a topic (T18 here) handed a request to the butler ("↪ T18 转给管家", a handover turn queued at
22:05:41.19); the owner's unrelated message with three images arrived at 22:05:41.01, the router saw nothing live and took
the usual path, the images took three seconds to read, and by then the handover turn was there and took the message as
its 补充; the topic the butler then opened for the images hung under "↪ T18 转给管家"."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from support.butler import switch_on
from support.world import UnifiedCase
from test_butler_topics import CHAT as TOPIC_CHAT, ButlerTopicsCase, HandoffTest
from test_butler_tracks_runtime import BUTLER, CHAT, SLOT1, TracksCase

from covey.app import butler as session
from covey.app import butler_handoff, butler_projects, butler_topics
from covey.core import butler, turn_source
from covey.core.butler_requests import Request
from covey.core.deliveries import delivery_path, read_delivery, save_delivery
from covey.core.jobs import upsert_job_record
from covey.core.state import epoch_seconds
from covey.core.topics import load_topic, message_topic, remember_topic_message

HANDED = "↪ T18 转给管家\n你在 T18 说：这台机器先不升级"
PDF = "将上述谱子整理成pdf发我"


class RacesCase(TracksCase):
    def handover(self, mid="om_hand", text=HANDED):
        """话题转交管家's turn of the butler (app/butler_asks.py queue_turn): the bot's message at the top level, the turn
        queued on it with the source ``handover``."""
        self.main.keep_topic(self.ctx, self.claude, CHAT)
        remember_topic_message(self.state, BUTLER, CHAT, mid)
        target = self.claude.submission.event_context(self.claude.harness.context, {"chat_id": CHAT, "message_id": mid})
        source = turn_source.make(turn_source.HANDOVER, asserted_by=turn_source.PARTY_OWNER,
                                  origin={"topic": "t818", "message_id": "answer-q1", "chat": CHAT, "question": "q1"})
        return self.claude.submission.enqueue_chat_job(target, text, CHAT, mid, prompt_content=text, sender_id="owner",
                                                       source=source)

    def send_while(self, mid, content, during, **fields):
        """The owner's message ``mid``, ``during()`` run while its attachments are read (after the router looked)."""
        event = {"chat_id": CHAT, "chat_type": "p2p", "sender_id": "owner", "message_id": mid, "root_id": "",
                 "content": content, **fields}

        def reading(raw, *args, **kwargs):
            during()
            return SimpleNamespace(text=content, display_text=content, event=raw)
        with mock.patch("covey.app.app.enrich_incoming_event", side_effect=reading):
            self.app.handle_event(event, self.ctx)

    def jobs_in(self, topic):
        return [job.message_id for job in self.app.runtime.jobs.topic_jobs(topic, CHAT)]

    def delivered(self, topic, mid):
        return read_delivery(delivery_path(self.state, topic, mid))


class HandoverTurnTest(RacesCase):
    """多路并行 on: the router never gives a message that quotes nothing to a turn the Bridge started."""

    def test_an_unrelated_message_while_a_handover_runs_is_a_track_of_its_own(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_pdf", PDF)
        self.judge.assert_not_called()  # Nothing it may join: no model asked.
        self.assertEqual(control.sent, [])
        self.assertEqual(self.slot_of("om_pdf"), 1)
        self.assertEqual(self.jobs_in(BUTLER), ["om_hand"])
        self.assertIsNone(self.delivered(BUTLER, "om_pdf"))

    def test_a_correction_word_does_not_join_a_handover(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_more", "另外把第二页也加上")  # Rule 8 within a minute of the handover's start.
        self.assertEqual(control.sent, [])
        self.assertEqual(self.slot_of("om_more"), 1)

    def test_images_alone_do_not_join_a_handover(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_img", "{}", message_type="image")  # Rule 7 would merge it into the newest track.
        self.assertEqual(control.sent, [])
        self.assertEqual(self.slot_of("om_img"), 1)

    def test_the_words_after_images_go_with_the_images_not_the_handover(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_img", "{}", message_type="image")
        self.send("om_words", PDF)
        self.assertEqual(control.sent, [])
        self.assertIsNone(self.job_of("om_words"))
        self.assertIn(PDF, self.job_of("om_img").prompt_text)
        self.assertEqual(self.slot_of("om_img"), 1)

    def test_quoting_the_handover_message_or_its_card_joins_it(self):
        self.handover()
        job = self.run_job("om_hand")
        job.progress = SimpleNamespace(message_id="om_hand_card")  # Its task card at the top level.
        remember_topic_message(self.state, BUTLER, CHAT, "om_hand_card")  # As the card's send records it.
        self.send("om_q1", "专项名字改成 Covey 升级", parent_id="om_hand")
        self.send("om_q2", "负责人用 Codex", parent_id="om_hand_card")
        self.assertEqual([mid for _, mid in job.backend_control.sent], ["om_q1", "om_q2"])

    def test_a_message_while_a_handover_waits_behind_the_owners_turn_goes_to_his_turn(self):
        self.send("om_1", "帮我查一下上周所有失败任务的原因，并且整理成一份报告")
        control = self.run_job("om_1").backend_control
        self.handover()  # Queued behind om_1 on the butler's own slot.
        self.assertEqual(self.jobs_in(BUTLER), ["om_1", "om_hand"])
        from covey.app import track_judge
        self.verdicts.append(track_judge.Verdict("merge", 0, 0.9))
        self.send("om_2", "报告里加上每个失败的时间")
        listed = self.judge.call_args.args[0].live
        self.assertEqual([job.job_id for job in listed], [str(self.job_of("om_1").job_id)])  # The judge sees no handover.
        self.assertEqual(control.sent, [("报告里加上每个失败的时间", "om_2")])
        self.assertEqual(self.job_of("om_hand").prompt_text, HANDED)  # Its request untouched.


class AttachmentRaceTest(RacesCase):
    """The router decides on what is live when the message comes; a turn queued while its attachments are read is none it
    chose, and the message never becomes that turn's 补充."""

    def test_a_handover_queued_while_the_images_were_read_never_takes_the_message(self):
        self.main.keep_topic(self.ctx, self.claude, CHAT)
        self.send_while("om_pdf", PDF, self.handover, message_type="post")
        hand = self.job_of("om_hand")
        self.assertEqual(hand.prompt_text, HANDED)
        self.assertIsNone(self.delivered(BUTLER, "om_pdf"))
        self.assertEqual(self.slot_of("om_pdf"), 1)
        self.assertEqual(message_topic(self.state, CHAT, "om_pdf"), SLOT1)
        self.assertEqual(self.jobs_in(BUTLER), ["om_hand"])

    def test_an_owners_turn_queued_meanwhile_does_not_take_it_either(self):
        # An answer's continuation (or a web page's message) queued on another thread as this one's files were read.
        self.main.keep_topic(self.ctx, self.claude, CHAT)

        def other():
            remember_topic_message(self.state, BUTLER, CHAT, "web-1")
            target = self.claude.submission.event_context(self.claude.harness.context,
                                                          {"chat_id": CHAT, "message_id": "web-1"})
            self.claude.submission.enqueue_chat_job(target, "网页上说的另一件事", CHAT, "web-1", sender_id="owner")
        self.send_while("om_pdf", PDF, other)
        self.assertNotIn(PDF, self.job_of("web-1").prompt_text or "")
        self.assertEqual(self.slot_of("om_pdf"), 1)

    def test_a_free_track_a_turn_took_meanwhile_is_left_to_it(self):
        self.send("om_1", "帮我查一下上周所有失败任务的原因，并且整理成一份报告")
        self.run_job("om_1")

        def taken():  # Slot 1 was free when the router looked; a turn took it before the message was queued.
            self.main.keep_track_topic(self.ctx, self.claude, CHAT, 1)
            remember_topic_message(self.state, SLOT1, CHAT, "om_x")
            target = self.claude.submission.event_context(self.claude.harness.context,
                                                          {"chat_id": CHAT, "message_id": "om_x"})
            self.claude.submission.enqueue_chat_job(target, "另一件事", CHAT, "om_x", sender_id="owner")
        self.send_while("om_2", "明天北京天气怎么样，要不要带伞呢", taken)
        self.assertEqual(self.slot_of("om_2"), 2)
        self.assertEqual(self.jobs_in(SLOT1), ["om_x"])
        self.assertNotIn("北京", self.job_of("om_x").prompt_text or "")

    def test_with_nothing_queued_meanwhile_the_usual_path_is_as_before(self):
        self.send_while("om_1", PDF, lambda: None)
        self.assertEqual(self.slot_of("om_1"), 0)
        self.judge.assert_not_called()


class TracksOffTest(RacesCase):
    """多路并行 off: a message while a turn runs is its 补充, as ever, unless that turn is one the Bridge started."""

    tracks = "0"

    def test_a_message_while_a_handover_runs_queues_as_a_turn_of_its_own(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_pdf", PDF)
        self.assertEqual(control.sent, [])
        self.assertEqual(self.jobs_in(BUTLER), ["om_hand", "om_pdf"])
        self.assertIsNone(self.delivered(BUTLER, "om_pdf"))

    def test_a_message_while_a_handover_is_queued_never_joins_its_request(self):
        self.handover()  # Not started: a message would have gone into its request (merge_before_start).
        self.send("om_pdf", PDF)
        self.assertEqual(self.job_of("om_hand").prompt_text, HANDED)
        self.assertEqual(self.jobs_in(BUTLER), ["om_hand", "om_pdf"])

    def test_the_owners_running_turn_takes_it_past_a_handover_queued_behind(self):
        self.send("om_1", "帮我查一下上周所有失败任务的原因，并且整理成一份报告")
        control = self.run_job("om_1").backend_control
        self.handover()
        self.send("om_2", "报告里加上每个失败的时间")
        self.assertEqual(control.sent, [("报告里加上每个失败的时间", "om_2")])
        self.assertEqual(self.job_of("om_hand").prompt_text, HANDED)

    def test_quoting_the_handover_joins_it(self):
        self.handover()
        control = self.run_job("om_hand").backend_control
        self.send("om_q", "专项名字改成 Covey 升级", parent_id="om_hand")
        self.assertEqual(control.sent, [("专项名字改成 Covey 升级", "om_q")])

    def test_two_messages_of_the_owner_still_go_together(self):
        self.send("om_1", "帮我查一下上周所有失败任务的原因，并且整理成一份报告")
        control = self.run_job("om_1").backend_control
        self.send("om_2", "顺便把这周的也查一下")
        self.assertEqual(control.sent, [("顺便把这周的也查一下", "om_2")])


class JoinableKindsTest(UnifiedCase):
    def test_only_a_turn_on_the_owners_own_words_takes_his_unquoted_message(self):
        table = {turn_source.LEGACY: True, turn_source.OWNER: True, turn_source.ANSWER: True, turn_source.RELAY: True,
                 turn_source.LEAD: True, turn_source.HANDOVER: False, turn_source.WAKE: False,
                 turn_source.ANSWER_FOR: False, turn_source.ASK: False, turn_source.REPLY: False}
        for kind, joins in table.items():
            with self.subTest(kind=kind):
                self.assertIs(turn_source.BEHAVIOR[kind].joinable, joins)
        self.assertFalse(turn_source.NOTHING.joinable)
        self.assertTrue(turn_source.behavior({}).joinable)
        job = SimpleNamespace(turn_source={"v": 1, "kind": "handover"}, message_id="om_h", reply_to_message_id="om_h",
                              progress=SimpleNamespace(message_id="om_card"))
        self.assertFalse(session.joins(job))
        self.assertTrue(session.joins(job, "om_card"))
        self.assertFalse(session.joins(job, "om_other"))


class TurnMessagesTest(ButlerTopicsCase):
    """Several messages in one butler turn (the one it started on, one merged before it started, a supplement while it ran):
    a call that names one (``message``) hands on that message's words and files alone, its topic hangs under that message,
    and its relay, recall and edit follow that message (T957)."""

    WORDS, AT = HandoffTest.WORDS, HandoffTest.AT
    supplement = HandoffTest.supplement

    def setUp(self):
        super().setUp()
        switch_on(self, "1")
        from covey.core.attachments import ATTACHMENTS_HEADER
        media = self.ctx.state_dir / "topics" / butler.butler_topic_id(TOPIC_CHAT) / "incoming_media"
        media.mkdir(parents=True)
        self.image = media / "om_owner-1.png"
        self.image.write_bytes(b"\x89PNG fake")
        self.doc = media / "om_owner-2-notes.txt"
        self.doc.write_text("x")
        prompt = "\n\n".join((self.WORDS, ATTACHMENTS_HEADER, f"- 图片 1: `{self.image}`",
                               f"- 文件 1（notes.txt）: `{self.doc}`", "请在需要理解图片或文件内容时读取上述本地路径。"))
        upsert_job_record(self.ctx.state_dir, "main-job", job_id=7, user_text=self.WORDS + "\n附件：图片 1 张、文件 1 个（notes.txt）",
                          prompt_text=prompt, created_at=self.AT, chat_id=TOPIC_CHAT)
        later = self.image.with_name("om_owner-3.png")
        later.write_bytes(b"\x89PNG later")
        self.later = later
        remember_topic_message(self.ctx.state_dir, butler.butler_topic_id(TOPIC_CHAT), TOPIC_CHAT, "om_b")
        self.supplement("om_b", PDF, image=later)

    def test_the_turns_messages_each_on_its_own(self):
        owner = butler_handoff.asked(self.ctx.state_dir, "main-job")
        self.assertEqual([part.id for part in owner.parts], ["main-job", "om_b"])
        self.assertEqual([part.words for part in owner.parts], [self.WORDS, PDF])
        self.assertEqual([[item.path for item in part.attachments] for part in owner.parts],
                         [[str(self.image), str(self.doc)], [str(self.later)]])
        two = owner.only(2)
        self.assertEqual((two.words, two.message_id, two.ids), (PDF, "om_b", ("om_b",)))
        self.assertIs(owner.only(3), owner)
        self.assertEqual(butler_handoff.message_error(3, owner), "message 是这一轮主人的第几条消息：1 到 2（开头那条是 1，补充的接着数）。")
        self.assertEqual(butler_handoff.message_error("2", owner), "")

    def test_messages_merged_before_the_turn_started_count_one_by_one(self):
        upsert_job_record(self.ctx.state_dir, "main-job-2", job_id=8, user_text="附件：图片 1 张", chat_id=TOPIC_CHAT,
                          message_id="om_first", created_at=self.AT,
                          prompt_text=f"收到的附件（已下载到本地）：\n\n- 图片 1: `{self.image}`"
                                      + butler_handoff.MERGED + "把这张图里的报错查一下")
        save_delivery(delivery_path(self.ctx.state_dir, butler.butler_topic_id(TOPIC_CHAT), "om_words"),
                      {"status": "sent", "merged": True, "text": "把这张图里的报错查一下", "job_key": "main-job-2",
                       "topic_id": butler.butler_topic_id(TOPIC_CHAT), "chat_id": TOPIC_CHAT, "mid": "om_words",
                       "saved_at": epoch_seconds(self.AT) + 2})
        owner = butler_handoff.asked(self.ctx.state_dir, "main-job-2")
        self.assertEqual([(part.id, part.words) for part in owner.parts],
                         [("om_first", ""), ("om_words", "把这张图里的报错查一下")])
        self.assertEqual(owner.words, "把这张图里的报错查一下")  # The whole turn's, as before.

    def test_a_topic_for_the_second_message_hangs_under_it_with_its_words_and_images_alone(self):
        data = self.start("m2", message=2, title="谱子整理成 PDF", prompt=PDF)["data"]
        job = self.job("m2")
        self.assertEqual(data["root_message_id"], "om_b")  # Not the turn's first message.
        self.assertIn(f"> {PDF}", job.prompt_text)
        self.assertNotIn(self.WORDS, job.prompt_text)
        self.assertIn(self.later.name, job.prompt_text)
        self.assertNotIn(self.image.name, job.prompt_text)
        self.assertEqual(load_topic(self.ctx.state_dir, data["topic_id"])["origin"]["message_id"], "om_b")
        self.assertEqual(turn_source.origin(job)["message_id"], "om_b")

    def test_without_message_it_is_the_whole_turn_as_before(self):
        self.start("m0", attachments="none")
        sent = self.job("m0").prompt_text
        self.assertIn(self.WORDS, sent)
        self.assertIn(PDF, sent)

    def test_a_message_the_turn_does_not_have_opens_nothing(self):
        answer = self.start("m9", message=5)
        self.assertFalse(answer["ok"])
        self.assertIn("1 到 2", answer["text"])
        self.assertIsNone(self.job("m9"))
        self.app.messenger.create_and_send_card.assert_not_called()

    def test_numbers_given_still_count_the_whole_turn(self):
        self.start("m3", message=2, attachments=["图片 1"], title="谱子", prompt=PDF)
        sent = self.job("m3").prompt_text
        self.assertIn(self.image.name, sent)  # 图片 1 of the whole turn: the first message's.
        self.assertNotIn(self.later.name, sent)

    def test_a_project_hears_the_second_message_alone(self):
        owner, items = butler_handoff.chosen(butler_handoff.asked(self.ctx.state_dir, "main-job"), 2, None)
        self.assertEqual((owner.words, owner.message_id), (PDF, "om_b"))
        self.assertEqual([item.path for item in items], [str(self.later)])
        refused = butler_projects.project_send(self.env, Request("p1", "project_send", {"pid": "x", "text": PDF,
                                                                                       "message": 7}, "main-job", 0.0))
        self.assertFalse(refused["ok"])

    def test_the_dispatch_card_and_said_message_follow_the_message(self):
        from covey.app import relay_targets
        upsert_job_record(self.ctx.state_dir, "main-job", message_id="om_first", reply_to_message_id="om_first")
        self.assertEqual(relay_targets.said_message(self.ctx.state_dir, "main-job"), "om_first")
        self.assertEqual(relay_targets.said_message(self.ctx.state_dir, "main-job", 2), "om_b")
        self.assertEqual(relay_targets.said_message(self.ctx.state_dir, "main-job", 1), "om_first")
        request = Request("d1", "topic_send", {"message": 2}, "main-job", 0.0)
        self.assertEqual(butler_topics.said_message(self.env, request), "om_b")

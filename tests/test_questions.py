"""Questions a task asks and the answers that go back to it.

Question cards (layout, skip, supplement box, answer forms), answers and feedback through
the card or a reply, standalone question cards after the task ended, duplicate checkpoint
forms, folding older questions, recovery of question delivery and the question records.
What follows an answer and held messages are in test_question_followup, Codex native
questions in test_native_questions.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.cards import form_button, remember_browse_card, text_of, walk_dicts
from support.fake_lark import FakeCardKit
from support.world import QuestionFixture, TopicFixture

from covey.app.questions import QuestionFolded, fold_older_questions, recover_question_delivery
from covey.app.artifact_policy import artifact_key
from covey.core.checkpoint import artifact_paths
from covey.cards.artifact_panels import artifact_panel
from covey.cards.held_note import ANSWER_REQUIRED, note_card, note_parts
from covey.app.answers import (AnswerIncomplete, SKIPPED, TITLE_CHARS, UNANSWERED_NOTE, answer_title, form_answers,
                                        picked_fields)
from covey.app.questions import scoped_question
from covey.app.task_card import StreamingCard
from covey.cards.question_cards import (PICK, SKIP_ALL_KIND, SKIP_ALL_LABEL, SKIP_LABEL, STATUS_LABEL, TOP_COPY,
                                                 question_card,
                                                 question_element)
from covey.cards.review import question_preview, recent_user_questions
from covey.app.interaction import _cancel_live_question
from covey.cards.streaming import build_stream_card
from covey.core.checkpoint import release_user_wait
from covey.core.jobs import find_job_record, job_path, upsert_job_record
from covey.core.longtask import TaskMonitor
from covey.core.store import transition_question, update_question
from covey.core.state import write_private_text
from covey.feishu.events import REPLY_CONTEXT
from covey.feishu.media import enrich_incoming_event


class QuestionTest(QuestionFixture, unittest.TestCase):
    def test_an_answer_refused_by_a_stop_repaints_its_card(self):
        """停止 clicked, its backend not yet closing its questions: the answer is refused and the card,
        painted 正在发送 when it was saved, says cancelled. cancel_questions then finds nothing pending."""
        painted = []
        self.app.questions.paint_question_state = lambda ctx, record: painted.append(record["status"])
        self.job.cancel_event.set()
        with self.assertRaisesRegex(ValueError, "任务已停止"):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0": "全部"}, "late")
        self.assertEqual(json.loads(self.path.read_text())["status"], "cancelled")
        self.rpc.answer.assert_not_called()
        self.app.questions.dispatch_question_event(self.ctx_topic, self.job, "cancel_questions", {}, None)
        self.assertEqual(painted, ["submitting", "cancelled"])

    def test_multiple_selection_and_freeform_are_preserved(self):
        self.record['questions'][0]['multiple'] = True
        self.record['questions'][0]['options'] = [{'label':'A'},{'label':'B'},{'label':'C'}]
        self.path.write_text(json.dumps(self.record))
        self.assertIn('multi_select_static', str(question_element(self.record)))
        self.app.questions.submit_answer(self.ctx_topic,'oc_chat',self.token,{'option_0':['0','2'],'supplement':'补充\n内容'},'multi')
        self.rpc.answer.assert_called_once_with(17, {'scope':{'answers':['A','C','补充说明：补充\n内容']}})

    def test_every_question_can_be_skipped_and_the_card_has_one_supplement_box(self):
        self.add_second_question()
        elements = question_element(self.record)["elements"]
        dropdown = next(e for e in elements if e.get("name") == "option_0")
        self.assertEqual(dropdown["options"][-1], {"text": {"tag": "plain_text", "content": SKIP_LABEL}, "value": "skip"})
        checkers = [e["name"] for e in elements if e.get("tag") == "checker"]
        self.assertEqual(checkers, ["skip_1"])  # Only the question without a dropdown gets a checkbox.
        inputs = [e["name"] for e in elements if e.get("tag") == "input"]
        self.assertEqual(inputs, ["answer_0", "answer_1", "supplement"])  # Answers per question, one note per card.
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token,
                               {"option_0": "skip", "skip_1": True, "supplement": "涉及个人信息"}, "skip")
        self.rpc.answer.assert_called_once_with(17, {
            "scope": {"answers": [SKIPPED]}, "order": {"answers": [SKIPPED, UNANSWERED_NOTE + "涉及个人信息"]}})
        record = json.loads(self.path.read_text())
        self.assertEqual(record["supplement"], "涉及个人信息")
        summary = question_element(record)["elements"][0]["content"]
        self.assertTrue(summary.endswith("补充说明：\n涉及个人信息"))  # The card shows it plainly; the model's text says more.

    def test_a_written_answer_wins_over_what_was_picked(self):
        # 2026-10-27, the owner: a picked option cannot be cleared on Feishu, and refusing the written answer
        # left no way to answer once the option turned out wrong. The box now wins, 不回答此题 included.
        self.add_second_question()
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token,
                                         {"option_0": "0", "answer_0": "自己写的范围", "skip_1": "true", "answer_1": "先检查"}, "own")
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": ["自己写的范围"]}, "order": {"answers": ["先检查"]}})
        self.assertEqual(form_answers([{"id": "q", "question": "Q", "options": [{"label": "A"}]}],
                                      {"option_0": "skip", "answer_0": "改主意了"}), {"q": {"answers": ["改主意了"]}})

    def test_a_skip_cannot_be_mixed_with_options(self):
        self.record["questions"][0].update(multiple=True, options=[{"label": "A"}, {"label": "B"}])
        self.path.write_text(json.dumps(self.record))
        before = self.path.read_text()
        with self.assertRaisesRegex(AnswerIncomplete, "不能再选其他选项"):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": ["0", "skip"]}, "mixed")
        self.assertEqual(self.path.read_text(), before)
        self.rpc.answer.assert_not_called()

    def test_an_image_sent_as_the_answer_reaches_the_model_as_its_path(self):
        self.backend.declare(questions=True, steering=True)
        attachments = "收到的附件（已下载到本地）：\n- 图片 1: `/media/a.png`"
        self.app.interaction.interactive_command(self.ctx_topic, "看这张\n附件：图片 1 张", "oc_chat", "om_img", attachments)
        sent = self.rpc.answer.call_args.args[1]["scope"]["answers"]
        self.assertEqual(sent[0], "看这张\n附件：图片 1 张")
        self.assertIn("`/media/a.png`", "\n".join(sent))
        # The answered card shows what was sent, never the server path or the reading instruction.
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["answers"]["scope"]["answers"], ["看这张\n附件：图片 1 张"])
        card = json.dumps(question_element(saved), ensure_ascii=False)
        self.assertIn("附件：图片 1 张", card)
        self.assertNotIn("/media/a.png", card)

    def test_an_image_answer_after_the_task_ended_shows_no_path_on_the_new_task_card(self):
        self.backend.declare(questions=True, steering=True)
        self.job.backend_control = None
        attachments = "收到的附件（已下载到本地）：\n- 图片 1: `/media/a.png`\n\n请在需要理解图片或文件内容时读取上述本地路径。"
        self.app.interaction.interactive_command(self.ctx_topic, "附件：图片 1 张", "oc_chat", "om_img", attachments)
        queued = self.app.runtime.jobs.queue.get_nowait()
        self.assertIn("附件：图片 1 张", queued.user_text)
        self.assertNotIn("/media/a.png", queued.user_text)
        self.assertNotIn("读取上述本地路径", queued.user_text)
        self.assertIn("`/media/a.png`", queued.prompt_text)
        self.assertNotIn("/media/a.png", queued.title)

    def test_restart_with_unacknowledged_saved_answer_never_replays(self):
        self.record.update(status='answered',delivery_state='awaiting_ack',answers={'scope':{'answers':['A']}})
        self.path.write_text(json.dumps(self.record))
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        restored=json.loads(self.path.read_text())
        self.assertEqual(restored['status'],'uncertain')
        self.assertEqual(restored['answers']['scope']['answers'],['A'])
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(self.ctx_topic,'oc_chat',self.token,{'answer_0':'A'},'retry')
        self.rpc.answer.assert_not_called()

    def test_saved_answer_recovery_requires_explicit_action_and_is_not_duplicated(self):
        self.job.backend_control=None
        self.record.update(status='uncertain',answers={'scope':{'answers':['A','C']}})
        self.path.write_text(json.dumps(self.record))
        action={'kind':'bridge_question_resume','topic_id':self.ctx_topic.topic_id,'token':self.token}
        self.app.interaction.interaction_action(self.ctx_topic,{'chat_id':'oc_chat','action_value':action,'event_id':'recover'})
        job=self.app.runtime.jobs.queue.get_nowait()
        self.assertIn('先核查',job.prompt_text)
        self.assertNotIn('先核查',job.user_text)  # An instruction for the model, not something the user wrote.
        self.assertEqual(json.loads(self.path.read_text())['answers']['scope']['answers'],['A','C'])
        with self.assertRaises(ValueError):
            self.app.interaction.interaction_action(self.ctx_topic,{'chat_id':'oc_chat','action_value':action,'event_id':'again'})
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_saved_answer_of_a_live_question_is_refused_without_holding_the_topic(self):
        self.job.backend_control=None
        self.record.update(status='uncertain',answers={'scope':{'answers':['A']}},native_live_question=True)
        self.path.write_text(json.dumps(self.record))
        action={'kind':'bridge_question_resume','topic_id':self.ctx_topic.topic_id,'token':self.token}
        with self.assertRaisesRegex(ValueError,'失去原执行上下文'):
            self.app.interaction.interaction_action(self.ctx_topic,{'chat_id':'oc_chat','action_value':action,'event_id':'recover'})
        saved=json.loads(self.path.read_text())
        self.assertEqual((saved['status'],saved.get('reconcile_saved')),('uncertain',None))
        self.assertFalse(self.app.questions.pending_questions(self.ctx_topic,'oc_chat'))
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_question_card_address_never_overwrites_an_answer_given_while_it_was_sent(self):
        def publish(ctx, chat, mid, card, on_sent=None, **kw):
            remember_browse_card(self.app, ctx, 'om_q', 'card_q')
            answered = {**json.loads(self.path.read_text()), 'status': 'submitting'}
            self.path.write_text(json.dumps(answered))  # The user answered while the card was on its way.
            on_sent('om_q')
            return True
        with mock.patch.object(self.app.messenger, 'publish_card', side_effect=publish):
            self.app.questions.publish_question_card(self.ctx_topic, dict(self.record))
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved['status'], saved['question_message_id'], saved['card_id']), ('submitting', 'om_q', 'card_q'))

    def test_repainting_a_browse_card_with_no_saved_address_reports_false(self):
        self.app.runtime.log_sink = mock.Mock()
        self.assertFalse(self.app.cards.repaint_browse_card(self.ctx_topic, 'oc_chat', 'mid', 'om_unknown', {}))

    def test_answer_saved_then_acknowledged_without_resending(self):
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0":"确认"}, "submit")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["delivery_state"], "awaiting_ack")
        self.assertIn(STATUS_LABEL, str(question_element(saved)))
        self.app.interaction.structured_harness_event(self.ctx_topic, self.job, "question_resolved", {"requestId":17}, self.rpc)
        self.assertEqual(json.loads(self.path.read_text())["delivery_state"], "delivered")
        self.job.card_updates.close()
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0":"确认"}, "again")
        self.rpc.answer.assert_called_once()

    def test_ambiguous_answer_failure_keeps_input_and_blocks_replay(self):
        self.rpc.answer.side_effect = OSError("connection closed")
        with self.assertRaises(OSError):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0":"我的回答"}, "submit")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["status"], "uncertain")
        self.assertEqual(saved["answers"]["scope"]["answers"], ["我的回答"])
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0":"我的回答"}, "again")
        self.rpc.answer.assert_called_once()

    def test_form_string_round_trip_multiline_and_duplicate_submit(self):
        event = {
            "chat_id": "oc_chat",
            "event_id": "answer",
            "action_value": json.dumps(form_button(question_element(self.record), "submit")["behaviors"][0]["value"]),
            "form_value": json.dumps({"option_0": "0", "supplement": "补充第一行\n第二行"}),
        }
        self.assertTrue(self.app.interaction.interaction_action(self.ctx_topic, event))
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": ["全部", "补充说明：补充第一行\n第二行"]}})
        self.assertFalse(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])
        with self.assertRaises(ValueError):
            self.app.interaction.interaction_action(self.ctx_topic, event)
        self.assertEqual(self.rpc.answer.call_count, 1)

    def test_a_form_value_that_is_not_json_is_refused_as_invalid(self):
        value = json.dumps(form_button(question_element(self.record), "submit")["behaviors"][0]["value"])
        for n, form in enumerate(['{"option_0": "0"', '["option_0"]']):  # Not JSON; JSON but not an object.
            with self.subTest(form=form):
                self.app.messenger.send_message.reset_mock()
                self.logs.clear()
                self.app.handle_card_action_event({"operator_id": "ou_user", "chat_id": "oc_chat", "event_id": f"answer{n}",
                                                   "action_value": value, "form_value": form}, self.ctx_topic)
                # Was the decoder's own text, or for a non-object the generic 暂未完成 reply and a log line.
                self.assertEqual([c.args[2] for c in self.app.messenger.send_message.call_args_list],
                                 ["提交回答未完成：表单数据无效，请重新提交"])
                self.assertFalse([line for line in self.logs if "card action failed" in line])
                self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
                self.rpc.answer.assert_not_called()

    def test_foreign_topic_cannot_answer(self):
        other = self.topic("om_other")
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(other, "oc_chat", self.token, {"answer_0": "no"}, "id")
        self.rpc.answer.assert_not_called()

    def test_unanswered_option_is_never_implicitly_selected(self):
        with self.assertRaises(ValueError):
            self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {}, "id")
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        self.rpc.answer.assert_not_called()

    def test_single_question_accepts_a_direct_long_reply_without_a_form(self):
        self.backend.declare(questions=True, steering=True)
        answer = "直接回复的长回答\n" * 150
        self.receive("om_direct_answer", answer, root="om_question")
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": [answer.strip()]}})
        self.assertEqual(json.loads(self.path.read_text())["status"], "answered")
        self.assertFalse(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertFalse((self.ctx_topic.state_dir / "held-notes").exists())
        self.assertIn("也可以直接在话题里回复", json.dumps(question_element(self.record), ensure_ascii=False))

    def add_second_question(self):
        self.record["questions"].append({"id": "order", "question": "顺序？", "options": []})
        self.path.write_text(json.dumps(self.record))

    def test_multiple_questions_keep_waiting_until_every_answer_is_submitted(self):
        self.add_second_question()
        before = self.path.read_text()
        for fields in ({"option_0": "0"}, {"answer_0": "全部", "answer_1": " \n "},
                       {"answer_1": "先检查"}, {"option_0": "99", "answer_1": "先检查"}):
            with self.subTest(fields=fields), self.assertRaises(AnswerIncomplete):
                self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, fields, "incomplete")
            self.assertEqual(self.path.read_text(), before)
            self.assertTrue(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])
        self.rpc.answer.assert_not_called()
        self.rpc.steer.assert_not_called()
        self.assertTrue(self.app.runtime.jobs.queue.empty())

        # A chosen option answers the first question; its free-text field is optional.
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token,
                               {"option_0": "0", "answer_1": "先检查"}, "complete")
        self.rpc.answer.assert_called_once_with(17, {
            "scope": {"answers": ["全部"]}, "order": {"answers": ["先检查"]}})
        self.assertFalse(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])
        self.assertEqual(json.loads(self.path.read_text())["status"], "answered")

    def test_direct_reply_to_multiple_questions_is_only_a_supplement(self):
        self.backend.declare(questions=True, steering=True)
        self.add_second_question()
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        self.receive("om_multi_note", "都按建议，另外看日志", root="om_question")
        self.rpc.answer.assert_not_called()
        self.rpc.steer.assert_not_called()
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        self.assertTrue(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])
        notice = json.dumps(self.app.messenger.publish_card.call_args.args[3], ensure_ascii=False)
        self.assertIn(ANSWER_REQUIRED, notice)
        notes = self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held")
        self.assertEqual([note["text"] for note in notes], ["都按建议，另外看日志"])

        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token,
                               {"option_0": "0", "answer_1": "先检查"}, "complete")
        self.rpc.answer.assert_called_once_with(17, {
            "scope": {"answers": ["全部"]},
            "order": {"answers": ["先检查", "另外补充：都按建议，另外看日志"]}})
        self.assertFalse(self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held"))

    def test_an_image_held_for_several_questions_keeps_its_path_for_the_model(self):
        self.backend.declare(questions=True, steering=True)
        self.add_second_question()
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        self.app.interaction.interactive_command(self.ctx_topic, "附件：图片 1 张", "oc_chat", "om_img",
                                     "收到的附件（已下载到本地）：\n- 图片 1: `/media/a.png`")
        note, = self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held")
        self.assertEqual(note["text"], "附件：图片 1 张")
        self.assertIn("`/media/a.png`", note["attachments"])
        card = json.dumps(self.app.messenger.publish_card.call_args.args[3], ensure_ascii=False)
        self.assertIn("附件：图片 1 张", card)
        self.assertNotIn("/media/a.png", card)  # The card shows what was sent, not the local path.
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0", "answer_1": "先检查"}, "done")
        self.assertIn("`/media/a.png`", self.rpc.answer.call_args.args[1]["order"]["answers"][-1])
        self.assertNotIn("/media/a.png", self.path.read_text())

    def receive_in_topic(self, mid, content, details, **event):
        """A topic reply through handle_event and the real enrichment, quoted messages read from ``details``."""
        def fetch(message_id, _profile=None):
            return details.get(message_id, {"ok": False, "error": "not found"})
        enrich = functools.partial(enrich_incoming_event, message_detail_fetcher=fetch)
        event = {"chat_id": "oc_chat", "sender_id": "ou_user", "message_id": mid, "content": content,
                 "message_type": "text", "root_id": "om_question", "thread_id": "omt_q", REPLY_CONTEXT: True, **event}
        with mock.patch("covey.app.app.enrich_incoming_event", enrich):
            self.app.handle_event(event, self.ctx)

    @staticmethod
    def detail(mid, msg_type, content):
        return {"data": {"items": [{"message_id": mid, "msg_type": msg_type, "create_time": "1700000000",
                                    "body": {"content": json.dumps(content, ensure_ascii=False)},
                                    "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}}}]}}

    def topic_root(self):
        return {"om_question": self.detail("om_question", "text", {"text": "话题起始的正文"})}

    def test_an_answer_quoting_a_message_gives_the_model_the_quote_and_shows_only_what_was_written(self):
        self.backend.declare(questions=True, steering=True)
        details = {**self.topic_root(), "om_other": self.detail("om_other", "text", {"text": "被引用的正文"})}
        self.receive_in_topic("om_ans", json.dumps({"text": "按这条做"}, ensure_ascii=False), details,
                              parent_id="om_other")
        sent = "\n".join(self.rpc.answer.call_args.args[1]["scope"]["answers"])
        self.assertIn("被引用的正文", sent)
        self.assertNotIn("话题起始的正文", sent)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["answers"]["scope"]["answers"], ["按这条做"])
        self.assertNotIn("被引用的正文", json.dumps(question_element(saved), ensure_ascii=False))

    def test_a_plain_topic_reply_answers_without_repeating_the_topic_root(self):
        self.backend.declare(questions=True, steering=True)
        self.receive_in_topic("om_ans", json.dumps({"text": "全部"}, ensure_ascii=False), self.topic_root(),
                              parent_id="om_question",
                              mentions=[{"id": "ou_x", "key": "@_user_1", "name": "某人"}])
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": ["全部"]}})
        self.assertNotIn("attachments", json.loads(self.path.read_text()))

    def test_a_merge_forward_held_for_several_questions_reaches_the_model_and_not_the_cards(self):
        self.backend.declare(questions=True, steering=True)
        self.add_second_question()
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        forward = {"data": {"items": [
            {"message_id": "om_fwd", "msg_type": "merge_forward", "body": {"content": "{}"},
             "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}}, "create_time": "1700000000"},
            {"message_id": "om_inner", "upper_message_id": "om_fwd", "msg_type": "text",
             "body": {"content": json.dumps({"text": "转发的正文"}, ensure_ascii=False)},
             "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}}, "create_time": "1700000000"}]}}
        self.receive_in_topic("om_fwd", '"Merged and Forwarded Message"', {**self.topic_root(), "om_fwd": forward},
                              message_type="merge_forward", parent_id="om_question")
        note, = self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held")
        self.assertTrue(note["text"].startswith("[合并转发 "))
        card = json.dumps(self.app.messenger.publish_card.call_args.args[3], ensure_ascii=False)
        self.assertNotIn("转发的正文", card)
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"option_0": "0", "answer_1": "先检查"}, "done")
        rider = self.rpc.answer.call_args.args[1]["order"]["answers"][-1]
        self.assertIn("转发的正文", rider)
        self.assertEqual(rider.count("[合并转发 "), 1)  # The summary line once, then the forward itself.

    def test_a_note_saved_with_its_paths_in_the_text_still_shows_only_what_was_sent(self):
        old = {"text": "看这张\n附件：图片 1 张\n收到的附件（已下载到本地）：\n- 图片 1: `/media/a.png`",
               "display": "看这张\n附件：图片 1 张", "status": "held", "topic_id": "t", "token": "n"}
        self.assertEqual(note_parts(old), ("看这张\n附件：图片 1 张", "收到的附件（已下载到本地）：\n- 图片 1: `/media/a.png`"))
        self.assertNotIn("/media/a.png", json.dumps(note_card(old), ensure_ascii=False))

    def test_multiple_question_guidance_requires_all_answers_and_respects_blocking(self):
        self.add_second_question()
        for blocking in (True, False):
            with self.subTest(blocking=blocking):
                card = question_element({**self.record, "blocking": blocking})
                text = json.dumps(card, ensure_ascii=False)
                self.assertIn("共 2 道题：逐题选择或填写，最后点“提交回答”。", text)
                self.assertIn("直接在话题里回复会先保存为补充", text)
                self.assertNotIn("问题不合适 / 修改要求", text)  # Folded into 只发补充说明 (2026-11-04 18:27).
                self.assertIn(f"点“{SKIP_ALL_LABEL}”", text)
                self.assertNotIn("也可以直接在话题里回复", text)
                self.assertIn(f"不想回答的题选“{SKIP_LABEL}”", text)
                self.assertEqual("每题都处理后提交才能继续" in text, blocking)
                for element in card["elements"]:
                    self.assertNotIn("initial_option", element)
                    self.assertNotIn("initial_options", element)

    def test_feedback_form_can_reopen_discussion_without_answering_the_questions(self):
        self.add_second_question()
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        # A card sent before 2026-11-04 has 问题不合适 / 修改要求; its click still opens the feedback card.
        self.assertFalse([e for e in walk_dicts(question_element(self.record)) if e.get("name") == "feedback"])
        old = {"kind": "bridge_question", "topic_id": self.record["topic_id"], "token": self.token, "mode": "feedback"}
        event = {"chat_id": "oc_chat", "event_id": "open-feedback", "action_value": old, "form_value": {}}
        self.app.interaction.interaction_action(self.ctx_topic, event)
        self.rpc.answer.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        form = self.app.messenger.publish_card.call_args.args[3]["body"]["elements"][0]
        submit = form["elements"][-1]["behaviors"][0]["value"]
        self.assertEqual(submit["mode"], "feedback_submit")
        event.update(event_id="send-feedback", action_value=submit, form_value={"feedback": "不用处理全部数据，请先解释为什么需要这两个决定"})
        self.app.interaction.interaction_action(self.ctx_topic, event)
        answers = self.rpc.answer.call_args.args[1]
        self.assertEqual(set(answers), {"scope", "order"})
        for answer in answers.values():
            self.assertIn("未回答本题", answer["answers"][0])
            self.assertIn("不表示同意或授权", answer["answers"][0])
        self.assertIn("请先解释", answers["order"]["answers"][0])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["response_kind"], "feedback")
        rendered = json.dumps(question_element(saved), ensure_ascii=False)
        self.assertIn("你提了反馈", rendered)
        self.assertIn("原问题没有被回答，也不表示同意", rendered)
        self.assertIn("原问题：\\n- 范围？\\n- 顺序？\\n\\n你的反馈：\\n不用处理全部数据", rendered)
        self.assertNotIn("答：", rendered)
        with self.assertRaises(ValueError):
            self.app.interaction.interaction_action(self.ctx_topic, event)
        self.assertEqual(self.rpc.answer.call_count, 1)

    def test_empty_feedback_does_not_release_questions_or_select_defaults(self):
        self.add_second_question()
        before = self.path.read_text()
        for text in (None, "", " \n "):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "先说明"):
                self.app.questions.submit_question_feedback(self.ctx_topic, "oc_chat", self.token, text, "empty-feedback")
        self.rpc.answer.assert_not_called()
        self.assertEqual(self.path.read_text(), before)
        self.assertTrue(json.loads(Path(self.job.checkpoint_path).read_text())["requires_user"])

    def test_held_reply_becomes_feedback_only_after_the_explicit_choice(self):
        self.backend.declare(questions=True, steering=True)
        self.add_second_question()
        self.app.messenger.publish_card = mock.Mock(return_value=True)
        feedback = "这两个问题前提不对，先重新分析"
        self.receive("om_feedback_note", feedback, root="om_question")
        self.rpc.answer.assert_not_called()
        note = self.app.questions.notes.held_notes(self.ctx_topic, "oc_chat", "held")[0]
        self.app.questions.note_action(self.ctx_topic, "oc_chat",
                             {"token": note["token"], "verb": "feedback"}, "explicit-feedback")
        self.rpc.answer.assert_called_once()
        sent = json.dumps(self.rpc.answer.call_args.args[1], ensure_ascii=False)
        self.assertEqual(sent.count(feedback), 1)
        self.assertIn("不表示同意或授权", sent)
        saved_note = json.loads(self.app.questions.notes.note_path(self.ctx_topic, note["token"]).read_text())
        self.assertEqual((saved_note["status"], saved_note["response_kind"]), ("attached", "feedback"))
        self.app.questions.note_action(self.ctx_topic, "oc_chat",
                             {"token": note["token"], "verb": "feedback"}, "duplicate-feedback")
        self.assertEqual(self.rpc.answer.call_count, 1)

    def test_feedback_after_restart_queues_reconsideration_without_claiming_answers(self):
        self.add_second_question()
        self.job.backend_control = None
        self.app.questions.submit_question_feedback(self.ctx_topic, "oc_chat", self.token, "先别执行，问题问错了", "feedback")
        queued = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(queued.topic_id, self.job.topic_id)
        self.assertIn("未回答原问题，不表示同意或授权", queued.user_text)
        self.assertIn("先别执行", queued.user_text)
        self.assertNotIn("用户补充回答：", queued.user_text)
        self.rpc.answer.assert_not_called()

    def test_response_to_expired_rpc_steers_live_turn(self):
        self.rpc.user_requests.clear()
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0": "later"}, "id")
        self.rpc.answer.assert_not_called()
        self.rpc.steer.assert_called_once()

    def test_answer_after_restart_enqueues_only_in_original_topic(self):
        self.job.backend_control = None
        self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0": "later"}, "id")
        queued = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(queued.topic_id, self.job.topic_id)
        self.assertIn("later", queued.user_text)
        self.rpc.answer.assert_not_called()

    def test_an_answer_that_would_run_on_a_removed_providers_model_is_refused_before_it_is_saved(self):
        # Job 2279 of the live data: waiting on a question, on a TRAE model. The answer would start a task
        # there; it is refused as certain, not saved and left 送达状态未确认.
        self.job.backend_control = None
        real = self.app.harness.topic_record
        retired = lambda *args: {**(real(*args) or {}), "model": "traex/huaxingyao/claude-opus-5-5"}  # noqa: E731
        with mock.patch.object(self.app.harness, "topic_record", side_effect=retired):
            with self.assertRaisesRegex(ValueError, "已下线的供应商「TRAE」.*换配置"):
                self.app.questions.submit_answer(self.ctx_topic, "oc_chat", self.token, {"answer_0": "later"}, "id")
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["status"], saved.get("answers")), ("pending", None))
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_question_card_has_no_default_answer_and_supports_multiline(self):
        # Two questions: the dropdown (one single-choice question has a button per option, TopLevelPickTest).
        element = question_element({**self.record, "questions": self.record["questions"] + [
            {"id": "b", "question": "顺序？", "options": [{"label": "先后"}]}]})
        selector = next(e for e in element["elements"] if e["tag"] == "select_static")
        self.assertNotIn("initial_option", selector)
        field = next(e for e in element["elements"] if e["tag"] == "input")
        self.assertEqual(field["input_type"], "multiline_text")
        self.assertEqual(form_button(element, "submit")["form_action_type"], "submit")


class TurnEndCheckpointQuestionTest(QuestionFixture, unittest.TestCase):
    """The turn-end checkpoint question and the native questions the same turn asked (T444 #3288: two
    answered AskUserQuestion forms kept the checkpoint's question from ever showing, without a log line)."""

    def setUp(self):
        super().setUp()
        Path(self.job.checkpoint_path).write_text(json.dumps({
            "phase": "等确认", "requires_user": True, "question": "按方案 A 做？",
            "questions": [{"id": "plan", "question": "按方案 A 做？", "options": [{"label": "是"}]}]}))
        self.logged = []
        self.app.runtime.log = self.logged.append

    def leave(self):
        with mock.patch.object(self.app.runs.questions, "defer_checkpoint_question") as defer:
            self.app.runs._leave_checkpoint_question(self.ctx_topic, self.job,
                                                     mock.Mock(outcome=None, is_error=False, text="方案如下"))
        return defer

    def test_an_answered_native_question_leaves_the_checkpoint_question_asked(self):
        write_private_text(self.path, json.dumps({**self.record, "status": "answered", "native_live_question": True}))
        defer = self.leave()
        defer.assert_called_once()
        self.assertEqual(defer.call_args.args[2][0]["id"], "plan")
        self.assertTrue(self.job.checkpoint_question_deferred)
        record = find_job_record(self.ctx_topic.state_dir, self.job.message_id)
        self.assertEqual((record["checkpoint_question_deferred"], record["execution_status"]), (True, "waiting_user"))
        self.assertFalse(any("checkpoint question skipped" in line for line in self.logged))

    def test_a_native_question_still_waiting_holds_it_back_and_says_so(self):
        for status in ("pending", "submitting", "uncertain"):
            with self.subTest(status=status):
                write_private_text(self.path, json.dumps({**self.record, "status": status}))
                self.logged.clear()
                self.leave().assert_not_called()
                self.assertIn(f"checkpoint question skipped job=#{self.job.job_id}: native question still open",
                              self.logged)

    def test_only_native_records_count_as_open(self):
        write_private_text(self.path, json.dumps({**self.record, "status": "cancelled"}))
        checkpoint = {**self.record, "token": "c" * 16, "request_id": "checkpoint-" + self.job.message_id}
        write_private_text(self.path.with_name("c" * 16 + ".json"), json.dumps(checkpoint))
        self.assertFalse(self.app.questions.has_open_native_questions(self.ctx_topic, self.job.job_id))
        self.assertTrue(self.app.questions.has_native_questions(self.ctx_topic, self.job.job_id))
        self.leave().assert_called_once()


class QuestionCardSendTest(QuestionFixture, unittest.TestCase):
    """The cross-process record lock is free while a question form is being sent."""

    def test_record_lock_is_free_during_the_send_and_a_cancel_meanwhile_is_repainted(self):
        self.record.update(body_delivery_required=True, display_pending=True)
        self.path.write_text(json.dumps(self.record))
        self.job.progress = SimpleNamespace(card_id="card-1")
        release, sending = threading.Event(), threading.Event()

        def send(ctx, job, element):
            sending.set()
            release.wait(10)
            return True
        self.app.cards.set_task_element = send
        self.app.questions.checkpoints.result_delivered = lambda ctx, record: True
        painted = []
        self.app.questions.paint_question_state = lambda ctx, record: painted.append(record["status"])
        result, cancelled = [], []
        worker = threading.Thread(daemon=True, target=lambda: result.append(
            self.app.questions.deliver_checkpoint_questions(self.ctx_topic, self.job, True)))
        worker.start()
        try:
            self.assertTrue(sending.wait(5))
            other = threading.Thread(daemon=True, target=lambda: cancelled.append(
                transition_question(self.path, {"pending"}, status="cancelled")))
            other.start()
            other.join(5)
            self.assertTrue(cancelled and cancelled[0])
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(result, [True])
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["status"], saved["display_pending"], saved["card_id"]), ("cancelled", False, "card-1"))
        self.assertEqual(painted, ["cancelled"])


def two_questions(**fields):
    """T191 #1084's shape: two questions with headers, options with descriptions, one generic header."""
    return {"token": "b" * 16, "job_id": 1084, "topic_id": "t" * 64, "chat_id": "oc_chat", "status": "pending",
            "blocking": True, "harness_id": "claude", "binding_revision": 2, "created_at": 1,
            "questions": [{"id": "ws", "header": "/workspace 口径", "question": "按什么口径建 workspace？",
                           "options": [{"label": "按会话单独建（推荐）", "description": "每个会话自成一个"},
                                       {"label": "维持排除", "description": "暂不放开"}]},
                          {"id": "more", "header": "补充信息", "question": "还有什么要说明的？"}],
            **fields}


class QuestionCardLookTest(unittest.TestCase):
    """The 2026-10-27 UI review, P3 and P9: what a question card says, folded and open."""

    def test_a_folded_question_says_whose_it_is_and_where_it_went(self):
        # #1127: the owner asked "这个是啥东西？" of one that read only 回答已送达 · 后续任务已完成.
        record = two_questions(status="answered", delivery_state="done", continuation_job_id=1111,
                               answers={"ws": {"answers": ["维持排除"]}, "more": {"answers": ["没有"]}})
        single = {**record, "questions": record["questions"][:1]}
        self.assertEqual(question_card(single, "T182")["config"]["summary"]["content"],
                         "❓ #1084 的提问 · 已回答 → #1111 有结果")
        cases = {
            "❓ #1084 的 2 道题 · 你提了反馈 → #1125 又提了问题": {"response_kind": "feedback", "feedback": "意思不大",
                                                     "delivery_state": "waiting_user", "continuation_job_id": 1125},
            "❓ #1084 的 2 道题 · 已回答 → #1111 停住了": {"delivery_state": "failed"},
            "❓ #1084 的 2 道题 · 回答已保存，送达未确认，请先核查，不会自动重复发送": {"status": "uncertain"},
            "❓ #1084 的 2 道题 · 已取消 · 话题已换配置，直接在话题里回复新模型即可": {"status": "cancelled",
                                                                 "cancel_reason": "switched"},
        }
        for title, fields in cases.items():
            with self.subTest(title):
                element = question_element({**record, **fields})
                self.assertEqual(element["header"]["title"]["content"], title)
        # The panel: each question with its number and header, and its answer; feedback keeps the questions.
        panel = question_element(record)["elements"][0]["content"]
        self.assertIn("**1/2 · /workspace 口径**\n按什么口径建 workspace？\n答：维持排除", panel)
        self.assertIn("**2/2**\n还有什么要说明的？\n答：没有", panel)  # A header that names nothing is left out.
        feedback = question_element({**record, **cases["❓ #1084 的 2 道题 · 你提了反馈 → #1125 又提了问题"]})
        self.assertIn("原问题：\n- 按什么口径建 workspace？\n- 还有什么要说明的？\n\n你的反馈：\n意思不大",
                      feedback["elements"][0]["content"])

    def test_an_answered_card_offers_only_what_is_still_useful(self):
        record = two_questions(status="answered", delivery_state="done", continuation_job_id=1111, answers={})
        done = text_of(question_card(record))
        self.assertNotIn(STATUS_LABEL, done)  # The title already says where it went.
        self.assertNotIn("bridge_switch_open", done)  # Its task card has 🔁 换配置.
        for fields in ({"status": "uncertain"}, {"status": "submitting"}, {"delivery_state": "awaiting_ack"}):
            with self.subTest(fields):
                self.assertIn(STATUS_LABEL, text_of(question_card({**record, **fields})))
        self.assertNotIn("bridge_switch_open", text_of(question_card(two_questions())))  # Open too: the task card has it.

    def test_an_open_form_numbers_its_questions_and_folds_its_rules(self):
        # T191 #1084: ten lines of rules before the first question, no numbers, options in the questions' type.
        card = question_card(two_questions(), "T191")
        # No title bar: a conclusion band names the topic and the task, what the owner decides and what the turn waits for.
        self.assertNotIn("header", card)
        band = json.dumps(card["body"]["elements"][0], ensure_ascii=False)
        for words in ("等你 · T191 &#35;1084", "要你定：2 件事", "等你回答后继续"):
            self.assertIn(words, band)
        form = card["body"]["elements"][1]
        texts = [e.get("content", "") for e in form["elements"] if e.get("tag") == "markdown"]
        self.assertEqual(texts[0], "共 2 道题：逐题选择或填写，最后点“提交回答”。")  # The band says what it waits for.
        self.assertEqual(texts[1], "**1/2 · /workspace 口径**\n按什么口径建 workspace？")
        self.assertEqual(texts[2], "- **按会话单独建（推荐）**：每个会话自成一个\n- **维持排除**：暂不放开")
        self.assertEqual(texts[3], "**2/2**\n还有什么要说明的？")
        # On the task card the form says whose it is itself; once settled the card has no bar.
        self.assertEqual(question_element(two_questions())["elements"][0]["content"], "**等你 · 等你回答后继续**")
        self.assertNotIn("header", question_card({**two_questions(), "status": "answered"}, "T191"))
        help_panel = next(e for e in form["elements"] if e.get("tag") == "collapsible_panel")
        self.assertEqual(help_panel["header"]["title"]["content"], "怎么回答")
        rules = help_panel["elements"][0]["content"]
        for rule in (SKIP_LABEL, "写了回答就以你写的为准", "补充说明", "问题不合适", SKIP_ALL_LABEL):
            self.assertIn(rule, rules)
        self.assertNotIn("每个输入框最多", text_of(card))
        # The form sends what it always sent: the same field names and values.
        fields = {e["name"]: e for e in walk_dicts(form["elements"]) if e.get("name")}
        # 先问一句 under 提交回答 (T604, test_ask_first) is a submit of its own name; the fields are unchanged.
        self.assertEqual(set(fields), {"option_0", "answer_0", "answer_1", "skip_1", "supplement", "skip_all", "submit",
                                       "ask_first"})
        self.assertEqual([o["value"] for o in fields["option_0"]["options"]], ["0", "1", "skip"])
        self.assertEqual(question_element(two_questions())["elements"][0]["content"], "**等你 · 等你回答后继续**")


class QuestionContextTest(unittest.TestCase):
    """A checkpoint's question may carry one line of background (``context``, T604 #14: on a phone a question alone did
    not say what it would change): small grey print under the question; without one the card is as before."""

    def test_the_background_is_one_grey_line_under_its_question(self):
        record = two_questions()
        record["questions"][0]["context"] = "看板要从外网打开\n  8080 已被网关占用"
        form = question_element(record)
        texts = [e.get("content", "") for e in form["elements"] if e.get("tag") == "markdown"]
        at = texts.index("**1/2 · /workspace 口径**\n按什么口径建 workspace？")
        self.assertEqual(texts[at + 1], "<font color='grey'>背景：看板要从外网打开 8080 已被网关占用</font>")
        line = next(e for e in form["elements"] if "背景：" in e.get("content", ""))
        self.assertEqual(line["text_size"], "notation")
        self.assertEqual(texts[at + 2], "- **按会话单独建（推荐）**：每个会话自成一个\n- **维持排除**：暂不放开")
        self.assertEqual(sum("背景：" in text for text in texts), 1)  # The second question has none.
        # Without text the card is as before.
        plain_card = question_card(two_questions(), "T191")
        for context in (None, "", "   ", 3, ["a"], {"a": 1}):
            with self.subTest(repr(context)):
                record = two_questions()
                record["questions"][0]["context"] = context
                self.assertEqual(question_card(record, "T191"), plain_card)

    def test_the_line_is_cut_and_shown_as_written(self):
        record = two_questions()
        record["questions"][0]["context"] = "# 标题 [链接](x) " + "长" * 300
        line = next(e["content"] for e in question_element(record)["elements"] if "背景：" in e.get("content", ""))
        self.assertTrue(line.startswith("<font color='grey'>背景：标题 链接 长"))  # Plain words: no heading, no link.
        self.assertTrue(line.endswith("…</font>"))
        self.assertLessEqual(line.count("长"), 120)


class QuestionStatusClickTest(QuestionFixture, unittest.TestCase):
    def test_an_old_cards_status_button_still_shows_the_answer(self):
        # Cards sent before kept 查看提交状态 after the answer went through; its click still works.
        self.path.write_text(json.dumps({**self.record, "status": "answered", "delivery_state": "done",
                                         "continuation_job_id": 9, "answers": {"scope": {"answers": ["全部"]}}}))
        event = {"chat_id": "oc_chat", "event_id": "status", "message_id": "om_old_card",
                 "action_value": {"kind": "bridge_question_status", "topic_id": self.ctx_topic.topic_id,
                                  "token": self.token}}
        with mock.patch.object(self.app.messenger, "publish_card", return_value=True) as publish:
            self.app.interaction.interaction_action(self.ctx_topic, event)
        card = publish.call_args.args[3]
        self.assertEqual(publish.call_args.kwargs["source_message"], "om_old_card")
        self.assertIn("已回答 → #9 有结果", text_of(card))


class SkipAllTest(QuestionFixture, unittest.TestCase):
    """“这些题先不答，只发补充说明” (repo review U2, the owner's choice): every question 不回答此题, the 补充说明
    sent and required. T191 answered five cards in a row with 不回答此题 per question and a 补充说明 that was
    the new instruction; #1180's 提交回答 was refused first."""

    def click(self, fields, message_id=""):
        return self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": "skip-all", "message_id": message_id,
            "action_value": {"kind": SKIP_ALL_KIND, "topic_id": self.ctx_topic.topic_id, "token": self.token},
            "form_value": fields})

    def test_every_question_card_has_the_button_beside_submit(self):
        for questions in (self.record["questions"], self.record["questions"] + [{"id": "b", "question": "顺序？"}]):
            with self.subTest(len(questions)):
                form = question_element({**self.record, "questions": questions})
                # Two buttons on one row, 提交回答 first and blue (the owner, 2026-11-04 18:27); the row under it has
                # the lesser ones (先问一句, test_ask_first).
                row = form["elements"][-2]
                buttons = [c["elements"][0] for c in row["columns"]]
                self.assertEqual([b["text"]["content"] for b in buttons], ["提交回答", SKIP_ALL_LABEL])
                self.assertEqual([b["type"] for b in buttons], ["primary_filled", "default"])
                skip = buttons[1]
                self.assertEqual((skip["form_action_type"], skip["name"]), ("submit", "skip_all"))
                self.assertEqual(skip["behaviors"][0]["value"],
                                 {"kind": SKIP_ALL_KIND, "topic_id": self.record["topic_id"], "token": self.token})
                # Besides them only the option buttons of a single-choice question (TopLevelPickTest) and 先问一句.
                self.assertEqual([e["name"] for e in walk_dicts(form) if e.get("tag") == "button"
                                  and not e["name"].startswith("pick_")], ["submit", "skip_all", "ask_first"])
                self.assertIn("**补充说明**（可选）", text_of(form))

    def test_whatever_was_picked_every_question_is_skipped_and_the_supplement_goes(self):
        # Picked and written fields are dropped: the owner said not to answer these questions.
        self.click({"option_0": "0", "answer_0": "全部", "supplement": "先帮我整理到一个飞书文档里"})
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": [SKIPPED, UNANSWERED_NOTE + "先帮我整理到一个飞书文档里"]}})
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["status"], saved["answers"], saved["supplement"]),
                         ("answered", {"scope": {"answers": [SKIPPED]}}, "先帮我整理到一个飞书文档里"))

    def test_an_empty_supplement_is_refused_on_the_card_and_nothing_is_saved(self):
        self.path.write_text(json.dumps({**self.record, "question_message_id": "om_qcard", "card_id": "card_q"}))
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.app.questions.show_answer_notice = mock.Mock()
        self.app.messenger.send_message.reset_mock()  # The task's own queued notice.
        for supplement in ("", "  \n "):
            with self.subTest(repr(supplement)):
                self.click({"option_0": "0", "supplement": supplement}, "om_qcard")
                _, chat, _, source, card = self.app.cards.repaint_browse_card.call_args.args
                self.assertEqual((chat, source), ("oc_chat", "om_qcard"))
                self.assertEqual(card["body"]["elements"][2]["content"],
                                 f"⚠️ 补充说明是空的，这次没有提交。点“{SKIP_ALL_LABEL}”前，先在最下面的“补充说明”里写上你想说的")
                self.assertIn('"tag": "form"', json.dumps(card))  # Still open: written and sent again.
        self.rpc.answer.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")
        self.app.questions.show_answer_notice.assert_not_called()
        self.app.messenger.send_message.assert_not_called()
        # A form on a task card is not redrawn alone: the small notice card, as for 提交回答.
        self.click({"supplement": ""}, "om_task_card")
        self.assertIn("补充说明是空的", self.app.questions.show_answer_notice.call_args.args[3])
        self.rpc.answer.assert_not_called()

    def test_the_answer_takes_the_path_of_submit_and_reads_the_same_to_any_runner(self):
        with mock.patch.object(self.app.questions, "submit_answer") as submit:
            self.click({"option_0": "0", "supplement": "换个方向"})
        self.assertEqual(submit.call_args.args[2:4], (self.token, {"skip_0": True, "supplement": "换个方向"}))


class TopLevelPickTest(QuestionFixture, unittest.TestCase):
    """A question with options, one picked (cards/question_cards.py picks): each option a button that answers at once,
    on a top-level copy (batch 5, 2026-11-07: the dropdown took four steps, and iOS's picker could not be cancelled)
    and on the topic's own card (T604: colleagues answer in the topic). Several questions and multiple choice keep the
    dropdown."""

    def setUp(self):
        super().setUp()
        self.record["questions"][0]["options"].append({"label": "只要新的"})
        self.path.write_text(json.dumps(self.record))
        self.app.questions.show_answer_notice = mock.Mock()

    def click(self, pick, fields):
        value = {"kind": "bridge_question", "topic_id": self.ctx_topic.topic_id, "token": self.token, PICK: pick}
        return self.app.interaction.interaction_action(self.ctx_topic, {
            "chat_id": "oc_chat", "event_id": f"pick-{pick}", "action_value": value, "form_value": fields})

    def test_a_top_level_copy_shows_option_buttons_and_no_dropdown(self):
        form = question_element({**self.record, TOP_COPY: True})
        self.assertFalse([e for e in walk_dicts(form) if str(e.get("tag")).endswith("select_static")])
        self.assertEqual([form_button(form, f"pick_{j}")["text"]["content"] for j in (0, 1)], ["全部", "只要新的"])
        row = next(e for e in walk_dicts(form) if e.get("tag") == "column_set" and e.get("flex_mode") == "flow")
        buttons = [c["elements"][0] for c in row["columns"]]
        self.assertEqual([b["name"] for b in buttons], ["pick_0", "pick_1", "pick_skip"])
        self.assertTrue(all(b["form_action_type"] == "submit" and len(b["name"]) <= 20 for b in buttons))
        self.assertEqual(buttons[1]["behaviors"][0]["value"], {"kind": "bridge_question", "topic_id": self.record["topic_id"],
                                                               "token": self.token, PICK: 1})
        self.assertIn("- **全部**：处理全部数据", text_of(form))  # The options' notes stay above the buttons.
        self.assertIn("点选项就提交；都不合适时写在回答框再点“提交回答”，也可以引用这张卡直接回复。", text_of(form))
        for name in ("submit", "skip_all"):  # 提交回答 and 只发补充说明 stay, with the answer box.
            form_button(form, name)
        self.assertTrue([e for e in walk_dicts(form) if e.get("name") == "answer_0"])

    def test_a_topic_card_shows_option_buttons_too(self):
        form = question_element(self.record)
        self.assertFalse([e for e in walk_dicts(form) if str(e.get("tag")).endswith("select_static")])
        row = next(e for e in walk_dicts(form) if e.get("tag") == "column_set" and e.get("flex_mode") == "flow")
        self.assertEqual([c["elements"][0]["name"] for c in row["columns"]], ["pick_0", "pick_1", "pick_skip"])
        self.assertIn("点选项就提交；都不合适时写在回答框再点“提交回答”，也可以直接在话题里回复。", text_of(form))
        self.assertIn("也可以直接在话题里回复，当作这道题的回答。", text_of(form))
        for name in ("submit", "skip_all"):
            form_button(form, name)

    def test_other_cards_keep_the_dropdown(self):
        other = {"id": "b", "question": "顺序？", "options": [{"label": "先后"}]}
        cases = {"two questions": {**self.record, "questions": self.record["questions"] + [other]},
                 "multiple": {**self.record, "questions": [{**self.record["questions"][0], "multiple": True}]},
                 "no options": {**self.record, "questions": [{"id": "c", "question": "说说？"}]}}
        for name, record in cases.items():
            with self.subTest(name):
                form = question_element(record)
                self.assertFalse([e for e in walk_dicts(form) if e.get("name", "").startswith("pick_")])
                self.assertNotIn("点选项就提交", text_of(form))
                if name != "no options":
                    self.assertTrue([e for e in walk_dicts(form) if str(e.get("tag")).endswith("select_static")])

    def test_a_pick_answers_with_its_option_and_the_supplement_goes_with_it(self):
        # What the dropdown or the answer box held does not count.
        self.click(1, {"option_0": "0", "answer_0": "写了别的", "supplement": "顺便看下日志"})
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": ["只要新的", "补充说明：顺便看下日志"]}})
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["status"], saved["answers"]), ("answered", {"scope": {"answers": ["只要新的"]}}))
        with self.assertRaises(ValueError):  # A second click: already answered, nothing sent again.
            self.click(0, {})
        self.assertEqual(self.rpc.answer.call_count, 1)

    def test_the_skip_button_answers_skipped(self):
        self.click("skip", {})
        self.rpc.answer.assert_called_once_with(17, {"scope": {"answers": [SKIPPED]}})

    def test_a_pick_that_names_no_option_submits_nothing(self):
        for pick in (2, -1, "0", True, None, "x"):
            with self.subTest(pick=pick):
                self.click(pick, {"answer_0": "写了别的"})
                self.assertIn("对不上", self.app.questions.show_answer_notice.call_args.args[3])
        self.rpc.answer.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "pending")

    def test_a_pick_on_questions_it_was_not_drawn_for_submits_nothing(self):
        questions = self.record["questions"]
        for name, changed in {"two": questions + [{"id": "b", "question": "顺序？"}],
                              "multiple": [{**questions[0], "multiple": True}],
                              "no options": [{"id": "c", "question": "说说？"}]}.items():
            with self.subTest(name), self.assertRaisesRegex(AnswerIncomplete, "对不上"):
                picked_fields(changed, {"supplement": "补"}, 0)
        self.assertEqual(picked_fields(questions, {"supplement": "补", "option_0": "1", "answer_0": "x"}, 0),
                         {"supplement": "补", "option_0": "0"})


class AnswerTitleTest(unittest.TestCase):
    record = {"questions": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}

    def test_a_supplement_titles_skipped_questions_and_follows_answers_when_it_fits(self):
        record = {"questions": [{"id": "a", "header": "范围", "question": "范围？"}], "supplement": "先做 ptl eval"}
        self.assertEqual(answer_title(record, {"a": {"answers": [SKIPPED]}}), "补充：先做 ptl eval")
        self.assertEqual(answer_title(record, {"a": {"answers": ["全部"]}}), "范围 → 全部；补充：先做 ptl eval")
        long = answer_title(record, {"a": {"answers": ["全" * 26]}})
        self.assertNotIn("补充", long)  # No room left: the answer alone, as before.
        self.assertLessEqual(len(long), TITLE_CHARS)
        cut = answer_title({**record, "supplement": "很长的补充" * 10}, {"a": {"answers": ["全部"]}})
        self.assertLessEqual(len(cut), TITLE_CHARS)
        self.assertTrue(cut.startswith("范围 → 全部；补充：很长的补充") and cut.endswith("…"))

    def test_the_follow_up_title_names_each_question_and_its_answer(self):
        # T182 #1111: "回答：可以做一版本 然后找个…" named no question. S25 #828: a skip read in full
        # ("用户选择不回答此题（不表示同意或授权）").
        record = {"questions": [{"id": "a", "header": "迁移方式", "question": "迁移怎么做？"},
                                {"id": "b", "header": "补充信息", "question": "上线时间怎么定？"}]}
        title = answer_title(record, {"a": {"answers": ["A2"]}, "b": {"answers": [SKIPPED]}})
        # A header that names no topic gives way to the question.
        self.assertEqual(title, "迁移方式 → A2；上线时间怎么定？ → 不回答")
        long = "先在测试环境验证迁移脚本，\n确认无误后再上线"
        title = answer_title({"questions": [{"id": "a", "header": "第一题", "question": "**要不要**把任务卡改成近似美临的样式"}]},
                             {"a": {"answers": [long, "选项二"]}})
        self.assertLessEqual(len(title), TITLE_CHARS)
        self.assertEqual(title, "要不要把任务卡改成近似… → 先在测试环境验证迁移脚本，…")
        self.assertNotIn("\n", title)
        # Without anything to name a question by, the answers alone.
        self.assertEqual(answer_title({"questions": [{"id": "a"}, {"id": "b"}]},
                                      {"a": {"answers": ["A2"]}, "b": {"answers": [SKIPPED]}}), "回答：A2；不回答")
        skipped = {k: {"answers": [SKIPPED]} for k in "abc"}
        self.assertEqual(answer_title(self.record, skipped, "问题本身不对，" * 10)[:9], "问题反馈：问题本身")
        self.assertLessEqual(len(answer_title(self.record, skipped, "问题本身不对，" * 10)), TITLE_CHARS)
        self.assertTrue(answer_title(self.record, skipped, "问题本身不对，" * 10).endswith("…"))


class QuestionLayoutTest(unittest.TestCase):
    def test_long_multiline_questions_have_bounded_independent_previews(self):
        preview = question_preview("第一行\n\n第三行\n第四行\n最后一行", max_lines=4, max_chars=240)
        self.assertIn("> 第一行  \n> \u3000  \n> 第三行", preview)  # A bare ">" let Feishu join the lines (S25).
        self.assertNotIn("最后一行", preview)
        self.assertIn("完整内容见原消息", preview)
        preview = question_preview("长" * 1000, max_lines=8, max_chars=600)
        self.assertIn("完整内容见原消息", preview)
        self.assertNotIn("长" * 601, preview)

    def test_user_markdown_cannot_create_card_headings_or_code_fences(self):
        preview = question_preview("# 标题\n```\n1. 步骤\n<at id=all>", max_lines=4, max_chars=240)
        self.assertNotIn("> # 标题", preview)
        self.assertNotIn("```", preview)
        self.assertNotIn("<at", preview)
        self.assertIn("&lt;at", preview)

    def test_questions_with_same_long_prefix_remain_distinct(self):
        first, current = "很长" * 200 + "甲", "很长" * 200 + "乙"
        rows = [{"role": "user", "content": first}, {"role": "user", "content": current}]
        self.assertEqual(recent_user_questions(rows, current), [first])

    def test_card_has_independent_collapsed_question_panels(self):

        runtime = StreamingCard("test", "test", "answer", lambda **kw: kw, lambda _: None, lambda s: s,
                                lambda *a: True, lambda s, n: s[:n], lambda *a, **k: None,
                                lambda *a, **k: (None, False), update_interval=0, replacement_limit=0)
        job = SimpleNamespace(user_text="当前第一行\n当前第二行", recent_questions=["历史第一行\n历史第二行"])
        elements = build_stream_card("处理中", "test", runtime, job=job)["body"]["elements"]
        panels = [e for e in elements if e["tag"] == "collapsible_panel"]
        self.assertEqual([p["element_id"] for p in panels], ["current_question"])  # No 任务详情 without words.
        self.assertTrue(all(p["expanded"] is False for p in panels))
        self.assertIn("当前第二行", panels[0]["elements"][0]["content"])
        answer = next(e for e in elements if e["element_id"] == "answer")
        self.assertEqual(answer["content"], "处理中")
        self.assertEqual(elements[0]["element_id"], "activity")  # The status line, the 输入/输出 box under it.


class QuestionCardLifecycleTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.topic('question-root')
        self.record = dict(token='a'*16, job_id=1, topic_id=self.ctx.topic_id, chat_id='oc_chat',
                           status='pending', blocking=True, questions=[dict(id='language', question='报告语言？')])
        self.path = self.ctx.state_dir / 'questions' / ('a'*16 + '.json')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.record))

    def publish(self):
        # Exercise the actual Cards.finish_result_controls and persisted address; only network is mocked.
        with FakeCardKit({'data': {'card_id': 'question-card'}}, sent=('question-message', None)):
            self.assertTrue(self.app.questions.publish_question_card(self.ctx, self.record))
        return json.loads(self.path.read_text())

    def test_finished_job_answer_replaces_original_form_with_monotonic_updates(self):
        record = self.publish()
        self.assertEqual(record['question_message_id'], 'question-message')
        self.assertFalse(self.app.runtime.jobs.by_id)
        record.update(status='answered', delivery_state='queued', answers={'language': {'answers': ['英文\nUTF-8']}})
        with FakeCardKit({}) as put:
            self.app.questions.paint_question_state(self.ctx, record)
            record['delivery_state'] = 'done'
            self.app.questions.paint_question_state(self.ctx, record)
        calls = put.calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].path, '/open-apis/cardkit/v1/cards/question-card')
        self.assertGreater(calls[1].data['sequence'], calls[0].data['sequence'])
        card = calls[1].card
        self.assertNotIn('提交回答', json.dumps(card, ensure_ascii=False))
        self.assertIn('已回答 → 后续任务 有结果', json.dumps(card, ensure_ascii=False))
        self.assertIn('英文\nUTF-8', card['body']['elements'][0]['elements'][0]['content'])

    def test_a_question_left_a_week_is_closed_once_with_a_note_on_its_card(self):
        from covey.app.stale import close_stale_records
        from covey.core.stale import STALE_NOTE
        self.record['harness_id'] = 'claude'
        self.path.write_text(json.dumps(self.record))
        record = self.publish()
        gateway = SimpleNamespace(apps={'claude': self.app}, runtime=self.app.runtime)
        plan = {'stale_questions': [record['token']], 'stale_jobs': []}
        with FakeCardKit({}) as put, \
                mock.patch('covey.app.stale._topic_context', return_value=self.ctx):
            self.assertEqual(close_stale_records(gateway, self.ctx, plan), ['question of job #1'])
            self.assertEqual(close_stale_records(gateway, self.ctx, plan), [])  # Closed once.
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved['status'], saved['cancel_reason']), ('cancelled', 'stale'))
        self.assertEqual(len(put.calls), 1)
        self.assertEqual(put.last.path, '/open-apis/cardkit/v1/cards/question-card')  # The whole card.
        card = json.dumps(put.last.card, ensure_ascii=False)
        self.assertIn(STALE_NOTE, card)
        self.assertNotIn('提交回答', card)

    def test_an_answer_whose_delivery_is_unconfirmed_is_never_closed_as_unhandled(self):
        from covey.app.stale import close_stale_records
        self.record['harness_id'] = 'claude'
        self.path.write_text(json.dumps(self.record))
        record = self.publish()
        record.update(status='uncertain', answers={})
        self.path.write_text(json.dumps(record))
        gateway = SimpleNamespace(apps={'claude': self.app}, runtime=self.app.runtime)
        with FakeCardKit({}) as put, \
                mock.patch('covey.app.stale._topic_context', return_value=self.ctx):
            self.assertEqual(close_stale_records(gateway, self.ctx, {'stale_questions': [record['token']],
                                                                     'stale_jobs': []}), [])
        self.assertEqual((json.loads(self.path.read_text())['status'], put.calls), ('uncertain', []))

    def test_continuation_updates_saved_question_once_and_rejects_other_topic(self):
        record = self.publish()
        record.update(status='answered', delivery_state='queued', continuation_job_id=2, answers={})
        self.path.write_text(json.dumps(record))
        job = SimpleNamespace(message_id='answer-' + record['token'], job_id=2, chat_id='oc_chat')
        with FakeCardKit({}) as put:
            self.app.questions.update_continuation_card(self.ctx, job, 'done')
            self.app.questions.update_continuation_card(self.ctx, job, 'done')
        self.assertEqual(len(put.calls), 1)
        self.assertEqual(json.loads(self.path.read_text())['delivery_state'], 'done')
        with self.assertRaises(ValueError):
            scoped_question(self.ctx, 'other-chat', record['token'])

    def test_failed_update_does_not_repeat_answer_or_reuse_sequence(self):
        record = self.publish()
        record.update(status='answered', answers={}, delivery_state='queued')
        with FakeCardKit(side_effect=[None, {}]) as put:
            self.app.questions.paint_question_state(self.ctx, record)
            self.app.questions.paint_question_state(self.ctx, record)
        self.assertGreater(put.calls[1].data['sequence'], put.calls[0].data['sequence'])
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_steer_form_replaces_controls_and_duplicate_callbacks_never_resend(self):
        self.publish()
        control = SimpleNamespace(steer=mock.Mock(), supports_steer=True)
        job = make_job(job_id=7, message_id='', topic_id=self.ctx.topic_id, chat_id='oc_chat', backend_control=control)
        self.app.runtime.jobs.by_id[7] = job
        event = {'message_id': 'question-message', 'event_id': 'click-1', 'chat_id': 'oc_chat',
                 'action_value': {'kind': 'bridge_steer', 'topic_id': self.ctx.topic_id, 'job_id': 7},
                 'form_value': {'supplement': '口令 902'}}
        with FakeCardKit({}) as put:
            self.app.interaction.interaction_action(self.ctx, event)
            self.app.runtime.jobs.by_id.clear()  # A late repeated callback after the task finished.
            self.app.interaction.interaction_action(self.ctx, {**event, 'event_id': 'click-2'})
        control.steer.assert_called_once()
        cards = [call.card for call in put.calls]
        self.assertEqual(len(cards), 2)
        self.assertNotIn('"tag": "form"', json.dumps(cards))
        self.assertIn('已补充到 #7', json.dumps(cards, ensure_ascii=False))
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_artifact_panel_attaches_once_and_pages_only_registered_paths(self):
        cp = self.root / 'artifacts.json'
        files = [str(self.root / f'report-{i}.csv') for i in range(7)]
        cp.write_text(json.dumps({'artifacts': files}))
        record = {'key':'artifact-job', 'job_id':9, 'checkpoint_path':str(cp), 'workdir':str(self.root),
                  'topic_id':self.ctx.topic_id, 'chat_id':'oc_chat'}
        upsert_job_record(self.ctx.state_dir, 'artifact-job', **{k:v for k,v in record.items() if k != 'key'})
        job = make_job(message_id='artifact-job', progress=SimpleNamespace(card_id='artifact-card',
                              sequence=0, element_id='answer'))
        self.app.artifacts.in_background = mock.Mock()
        with FakeCardKit({}) as put:
            self.app.artifacts.attach_artifact_controls(self.ctx, job)
            self.app.artifacts.attach_artifact_controls(self.ctx, job)
        self.assertEqual(len(put.calls), 1)
        first = json.loads(put.last.data['elements'])[0]
        self.assertIn('正在上传到云空间', str(first))
        self.assertNotIn('下一页', str(first))
        self.app.artifacts.in_background.assert_called_once_with(self.app.artifacts.publish_artifacts, self.ctx, job, mock.ANY)
        # Cards published before Drive links keep their paged send buttons.
        second = artifact_panel(artifact_paths(record), record, self.ctx.topic_id, 1)
        buttons = [e for e in second['elements'] if e['tag'] == 'button']
        send = [b['behaviors'][0]['value'] for b in buttons if b['behaviors'][0]['value']['kind'] == 'bridge_artifact_send']
        self.assertEqual([v['artifact_key'] for v in send], [artifact_key(files[5]),artifact_key(files[6])])
        self.assertTrue(all(v['topic_id'] == self.ctx.topic_id and v['job_id']==9 for v in send))
        self.assertNotIn('command_shortcuts', str(second))

    def test_retry_status_is_explicit_and_clears_after_real_progress(self):
        job = make_job(backend_retry={'attempt': 3, 'next': (time.time()+10)*1000},
                              tool_trace=object())
        rendered = []
        monitor = TaskMonitor(job, self.root/'cp.json', rendered.append, lambda **kw: None,
                              lambda *a: None, lambda *a: None, lambda: None)
        monitor.tick()
        self.assertIn('模型请求重试第 3 次', rendered[-1])
        self.assertIn('停止按钮', rendered[-1])
        self.assertNotIn('上游返回', rendered[-1])
        job.backend_retry = {**job.backend_retry, 'reason': 'Internal Server Error: unknown error'}
        monitor.tick()
        self.assertIn('上游返回：Internal Server Error: unknown error', rendered[-1])
        self.assertNotIn('已持续', rendered[-1])
        # Acme's relay tries again at once and says since when and for how long at most.
        job.backend_retry = {'attempt': 41, 'since': (time.time() - 125) * 1000, 'limit': 3600}
        monitor.tick()
        self.assertIn('模型请求重试第 41 次', rendered[-1])
        self.assertIn('正在再次尝试（已持续 2 分 0', rendered[-1])
        self.assertIn('，最长 60 分钟）', rendered[-1])
        job.backend_retry = {}
        monitor.tick()
        self.assertNotIn('重试中', rendered[-1])


class DuplicateCheckpointQuestionTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.topic('duplicate-root')
        self.app.questions.paint_question_state = mock.Mock()
        self.app.cards.update_record_panel = mock.Mock()
        self.old_token, self.new_token = 'a' * 16, 'b' * 16
        self.scope = dict(topic_id=self.ctx.topic_id, chat_id='oc_chat', backend='claude',
                          harness_id='claude', session_id='native-session', workdir=str(self.root),
                          binding_revision=2)
        self.questions = [dict(id='deploy', question='When to deploy?', options=[dict(label='Now')]),
                          dict(id='plan', question='Use this plan?', options=[dict(label='Yes')])]
        self.old = self.form(self.old_token, 'old', 95)
        self.new = self.form(self.new_token, 'new', 96)
        self.new.update(status='answered', delivery_state='done', continuation_job_id=97,
                        answers={'deploy': {'answers': ['Now']}, 'plan': {'answers': ['Yes']}})
        self.save(self.old)
        self.save(self.new)
        for key, jid in [('old', 95), ('new', 96)]:
            upsert_job_record(self.ctx.state_dir, key, **self.scope, job_id=jid,
                message_id=key, status='waiting_user' if key == 'old' else 'continued',
                execution_status='waiting_user', delivery_ok=True, card_id=key + '-card',
                card_message_id=key + '-message')
        self.continuation_key = 'answer-' + self.new_token
        upsert_job_record(self.ctx.state_dir, self.continuation_key, **self.scope, job_id=97,
                          message_id=self.continuation_key, status='done')

    def form(self, token, key, jid):
        return dict(token=token, job_key=key, job_id=jid, topic_id=self.ctx.topic_id, chat_id='oc_chat',
                    request_id='checkpoint-' + key, body_delivery_required=True,
                    body_delivery_confirmed=True, display_pending=False, blocking=True,
                    status='pending', questions=copy.deepcopy(self.questions))

    def path(self, token):
        return self.ctx.state_dir / 'questions' / (token + '.json')

    def save(self, record):
        write_private_text(self.path(record['token']), json.dumps(record))

    def test_restart_closes_only_proven_duplicate_without_replaying_answers(self):
        recover_question_delivery(self.ctx, [self.app.questions], self.app.runtime.log)
        retired = json.loads(self.path(self.old_token).read_text())
        self.assertEqual(retired['status'], 'superseded')
        self.assertEqual(retired['superseded_by'], self.new_token)
        self.assertNotIn('answers', retired)
        self.assertEqual(json.loads(self.path(self.new_token).read_text()), self.new)
        self.assertEqual(find_job_record(self.ctx.state_dir, 'old')['status'], 'continued')
        self.assertEqual(self.app.questions.pending_questions(self.ctx, 'oc_chat'), [])
        self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
        self.app.questions.paint_question_state.assert_called_once()
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        self.assertFalse(self.backend.calls)
        rendered = json.dumps(question_card(retired), ensure_ascii=False)
        self.assertIn('后续任务 #97', rendered)
        self.assertNotIn('提交回答', rendered)
        self.assertNotIn('"tag": "form"', rendered)
        with self.assertRaisesRegex(ValueError, '已经提交'):
            self.app.questions.submit_answer(self.ctx, 'oc_chat', self.old_token,
                                   {'option_0': '0', 'option_1': '0'}, 'late-click')

    def test_each_harness_retires_only_its_own_duplicates_at_startup(self):
        self.app.questions.reconcile_checkpoint_questions(self.ctx, backend='codex')
        self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'pending')
        self.app.questions.reconcile_checkpoint_questions(self.ctx, backend='claude')
        self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'superseded')

    def test_a_message_reads_no_job_records_while_no_form_is_answered(self):
        # reconcile runs for every message: with no answered form nothing can be retired (S31).
        from covey.app import checkpoint_questions
        read, real = [], checkpoint_questions.directory_store
        self.save({**self.new, 'status': 'pending'})
        with mock.patch.object(checkpoint_questions, 'directory_store', lambda path: read.append(path.name) or real(path)):
            self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
            self.assertEqual(read, ['questions'])
            self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'pending')
            self.save(self.new)  # Answered: the same call reads the jobs and retires the older form.
            self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
        self.assertEqual(read, ['questions', 'questions', 'jobs'])
        self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'superseded')

    def test_normal_reply_reconciles_before_pending_question_guard(self):
        self.assertFalse(self.app.interaction._interactive_command(self.ctx, '部署结果', 'oc_chat', 'reply'))
        self.app.messenger.send_message.assert_not_called()
        self.assertEqual(self.app.questions.pending_questions(self.ctx, 'oc_chat'), [])
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_submitting_newer_form_closes_old_one_after_enqueue(self):
        self.save(self.form(self.new_token, 'new', 96))
        job_path(self.ctx.state_dir, self.continuation_key).unlink()
        upsert_job_record(self.ctx.state_dir, 'new', model='claude-model', effort='max')

        def enqueue(ctx, text, chat, mid, **kwargs):
            self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'pending')
            upsert_job_record(ctx.state_dir, mid, **self.scope, job_id=97, message_id=mid, status='queued')
            return SimpleNamespace(job_id=97)

        with mock.patch.object(self.app.submission, 'enqueue_chat_job', side_effect=enqueue) as queued:
            self.app.questions.submit_answer(self.ctx, 'oc_chat', self.new_token,
                                   {'option_0': '0', 'option_1': '0'}, 'submit')
        queued.assert_called_once()
        self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'superseded')
        self.assertEqual(json.loads(self.path(self.new_token).read_text())['answers'], self.new['answers'])

    def test_restart_finishes_interrupted_question_to_job_reconciliation(self):
        self.save({**self.old, 'status': 'superseded', 'superseded_by': self.new_token,
                   'continuation_job_id': 97})
        recover_question_delivery(self.ctx, [self.app.questions], self.app.runtime.log)
        self.assertEqual(find_job_record(self.ctx.state_dir, 'old')['status'], 'continued')
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_different_scope_or_incomplete_delivery_never_discards_question(self):
        cases = [('source', key, 'different') for key in
                 ('session_id', 'workdir', 'topic_id', 'chat_id', 'backend', 'harness_id', 'binding_revision')]
        cases += [('continuation', 'status', value) for value in ('failed', 'cancelled', 'interrupted')]
        cases += [('continuation', key, 'different') for key in ('topic_id', 'chat_id', 'backend', 'workdir', 'message_id')]
        cases += [('question', 'status', value) for value in ('pending', 'submitting', 'uncertain')]
        cases += [('question', 'delivery_state', 'awaiting_ack'), ('question', 'continuation_job_id', 999),
                  ('question', 'answers', {}), ('question', 'native_live_question', True),
                  ('question', 'response_kind', 'feedback'),
                  ('question', 'questions', [dict(id='deploy', question='Different plan?')]),
                  ('source', 'job_id', 94)]
        for target, field, value in cases:
            with self.subTest(target=target, field=field, value=value):
                key = 'new' if target == 'source' else self.continuation_key
                if target == 'question':
                    self.save({**self.new, field: value})
                else:
                    before = find_job_record(self.ctx.state_dir, key)
                    # By hand: one field changed and nothing else (upsert_job_record would stamp it too).
                    write_private_text(job_path(self.ctx.state_dir, key), json.dumps({**before, field: value}))
                self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
                self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'pending')
                if target == 'question':
                    self.save(self.new)
                else:
                    # Restore exact fixture, bypassing terminal status protection.
                    write_private_text(job_path(self.ctx.state_dir, key), json.dumps(before))
        self.app.questions.paint_question_state.assert_not_called()
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_pending_native_question_and_inflight_old_answer_are_not_retired(self):
        for changes in ({'native_live_question': True}, {'status': 'submitting'}, {'status': 'uncertain'}):
            with self.subTest(changes=changes):
                self.save({**self.old, **changes})
                self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
                self.assertEqual(json.loads(self.path(self.old_token).read_text()), {**self.old, **changes})
        self.app.questions.paint_question_state.assert_not_called()

    def test_missing_continuation_does_not_retire_pending_question(self):
        job_path(self.ctx.state_dir, self.continuation_key).unlink()
        self.app.questions.reconcile_checkpoint_questions(self.ctx, 'oc_chat')
        self.assertEqual(json.loads(self.path(self.old_token).read_text())['status'], 'pending')


class FoldOlderQuestionTest(TopicFixture, unittest.TestCase):
    """A newer question card shown in a topic folds the older questions still waiting there. T182 (2026-10-27):
    #1105's question stayed open after #1106 asked two more; the owner chose 自动收起."""

    def setUp(self):
        super().setUp()
        self.base = self.ctx  # The Bridge's own context, as the outbox loop and startup have it.
        self.ctx = self.topic('fold-root')
        self.old_token, self.new_token = 'c' * 16, 'd' * 16
        asked = time.mktime(time.localtime()[:3] + (20, 21, 0, 0, 0, -1))
        self.old = self.form(self.old_token, 'om_1105', 1105, asked,
                             [dict(id='order', header='顺序', question='先做哪部分？')])
        self.new = self.form(self.new_token, 'om_1106', 1106, asked + 180,
                             [dict(id='a', question='范围？'), dict(id='b', question='口径？')])
        self.save(self.old)
        self.save(self.new)
        for key, jid, status in (('om_1105', 1105, 'continued'), ('om_1106', 1106, 'waiting_user')):
            upsert_job_record(self.ctx.state_dir, key, topic_id=self.ctx.topic_id, chat_id='oc_chat', backend='claude',
                              job_id=jid, message_id=key, status=status, execution_status='waiting_user',
                              delivery_ok=True, card_id=key + '-card', card_message_id=key + '-message')
        self.painted = []
        self.app.questions.paint_standalone_question = mock.Mock(side_effect=lambda ctx, r: self.painted.append(r))
        self.app.cards.update_record_panel = mock.Mock()

    def form(self, token, key, jid, asked, questions):
        return dict(token=token, job_key=key, job_id=jid, topic_id=self.ctx.topic_id, chat_id='oc_chat',
                    harness_id='claude', request_id='checkpoint-' + key, body_delivery_required=True,
                    body_delivery_confirmed=True, display_pending=False, blocking=True, status='pending',
                    question_message_id='om_card_' + key, card_id='card-' + key, created_at=asked,
                    questions=questions)

    def path(self, token):
        return self.ctx.state_dir / 'questions' / (token + '.json')

    def save(self, record):
        write_private_text(self.path(record['token']), json.dumps(record))

    def now(self, token):
        return json.loads(self.path(token).read_text())

    def fold(self, *harnesses):
        fold_older_questions(self.base, list(harnesses) or [self.app.questions])

    def test_an_unconfirmed_answer_being_resubmitted_is_never_folded(self):
        """核查已保存回答并继续 writes it back to pending, then submits it: the outbox loop's fold, on its
        own thread, waits for both steps and never folds an answer that may have been delivered."""
        job_file = job_path(self.ctx.state_dir, 'om_1105')
        # By hand: the status set back and nothing else, past upsert_job_record's transitions and stamps.
        write_private_text(job_file, json.dumps({**json.loads(job_file.read_text()), 'status': 'waiting_user'}))
        self.save({**self.old, 'status': 'uncertain', 'answers': {'order': {'answers': ['先后端']}}})
        self.app.questions.enqueue_continuation = mock.Mock(return_value=SimpleNamespace(job_id=1107))
        real, folding = self.app.questions.submit_answer, []

        def submit(*args, **kwargs):  # The outbox loop's fold lands between the two steps.
            folding.append(threading.Thread(target=self.fold))
            folding[0].start()
            folding[0].join(0.3)
            return real(*args, **kwargs)
        self.app.questions.submit_answer = submit
        action = {'kind': 'bridge_question_resume', 'topic_id': self.ctx.topic_id, 'token': self.old_token}
        with mock.patch("covey.core.model_settings.strict_recorded_model_settings", return_value=("m", "e")):
            self.app.interaction.interaction_action(self.ctx, {'chat_id': 'oc_chat', 'action_value': action,
                                                               'event_id': 'recover'})
        folding[0].join(5)
        self.assertNotEqual(self.now(self.old_token)['status'], 'superseded')
        self.assertEqual(find_job_record(self.ctx.state_dir, 'om_1105')['status'], 'waiting_user')

    def test_a_newer_question_card_folds_the_older_one_and_points_to_it(self):
        self.fold()
        folded = self.now(self.old_token)
        self.assertEqual({k: folded.get(k) for k in ('status', 'superseded_by', 'superseded_reason', 'superseded_by_job')},
                         {'status': 'superseded', 'superseded_by': self.new_token,
                          'superseded_reason': 'newer_question', 'superseded_by_job': 1106})
        self.assertNotIn('answers', folded)
        self.assertEqual(self.now(self.new_token), self.new)
        self.assertEqual([r['token'] for r in self.painted], [self.old_token])  # Its own card, redrawn whole.
        self.assertEqual([r['token'] for r in self.app.questions.pending_questions(self.ctx, 'oc_chat')], [self.new_token])
        # #1105 had gone on as continued already: its task is left as it is.
        self.assertEqual(find_job_record(self.ctx.state_dir, 'om_1105')['status'], 'continued')
        self.app.cards.update_record_panel.assert_not_called()
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        card = question_card(folded, 'T182')
        self.assertEqual(card['config']['summary']['content'], '❓ #1105 的提问 · 已收起 → #1106 又提了问题')
        [panel] = card['body']['elements']  # No form, no buttons, no 🔁 换配置.
        self.assertEqual((panel['tag'], panel['expanded'], panel['header']['title']['content']),
                         ('collapsible_panel', False, '❓ #1105 的提问 · 已收起 → #1106 又提了问题'))
        self.assertEqual(panel['elements'][0]['content'],
                         '**顺序**\n先做哪部分？\n\n这组问题没有回答。要补充的，写在 #1106 的问题卡上。')
        self.assertNotIn('behaviors', json.dumps(card))
        self.fold()  # Folded once.
        self.assertEqual(len(self.painted), 1)
        self.assertIn('question folded token=' + self.old_token, '\n'.join(self.logs))

    def test_a_task_still_waiting_for_its_answer_goes_on_as_continued(self):
        job_file = job_path(self.ctx.state_dir, 'om_1105')
        # By hand: the status set back and nothing else, past upsert_job_record's transitions and stamps.
        write_private_text(job_file, json.dumps({**json.loads(job_file.read_text()), 'status': 'waiting_user'}))
        self.fold()
        job = find_job_record(self.ctx.state_dir, 'om_1105')
        self.assertEqual((job['status'], job['status_message'], job['superseded_question_by']),
                         ('continued', '这组问题已收起：后面 #1106 又提了问题', self.new_token))
        args = self.app.cards.update_record_panel.call_args.args
        self.assertEqual((args[3], args[5]), ({'message_id': 'om_1105-message'}, {
            'tag': 'markdown', 'element_id': self.app.cards.stream.element_id,
            'content': '这组问题已收起：后面 #1106 又提了问题。'}))

    def test_a_question_its_task_may_wait_on_or_being_answered_stays(self):
        live = make_job(message_id='om_1105', job_id=1105, topic_id=self.ctx.topic_id, chat_id='oc_chat')
        job_file = job_path(self.ctx.state_dir, 'om_1105')
        # By hand: the status set back and the record restored byte for byte, past upsert_job_record's transitions and stamps.
        saved_job = job_file.read_text()
        cases = {
            # Its task is still on: a live native question folded would leave it waiting for ever.
            'running task': lambda: write_private_text(job_file, json.dumps({**json.loads(saved_job), 'status': 'running'})),
            'registered task': lambda: self.app.runtime.jobs.register(live),
            'live task runner': lambda: self.runner_alive.configure_mock(return_value=True),
            'answer being sent': lambda: self.save({**self.old, 'status': 'submitting'}),
            'answer unconfirmed': lambda: self.save({**self.old, 'status': 'uncertain'}),
            'newer not shown yet': lambda: self.save({**self.new, 'display_pending': True}),
            'newer result not delivered': lambda: self.save({**self.new, 'body_delivery_confirmed': False}),
            'newer cancelled': lambda: self.save({**self.new, 'status': 'cancelled'}),
            'newer in another topic': lambda: self.save({**self.new, 'topic_id': 'x' * 64}),
            'in a task runner': lambda: setattr(self.app.runtime, 'runner', object()),
        }
        for name, change in cases.items():
            with self.subTest(name), mock.patch('covey.app.questions.runner_alive',
                                                return_value=False) as self.runner_alive:
                change()
                before = self.now(self.old_token)
                self.fold()
                self.assertEqual(self.now(self.old_token), before)
            self.save(self.old)
            self.save(self.new)
            write_private_text(job_file, saved_job)
            self.app.runtime.jobs.unregister(live)
            self.app.runtime.runner = None
        self.assertEqual(self.painted, [])
        self.fold()  # With all of it undone, it folds.
        self.assertEqual(self.now(self.old_token)['status'], 'superseded')

    def test_a_question_never_shown_folds_without_a_card(self):
        self.save({**self.old, 'display_pending': True, 'body_delivery_confirmed': False})
        with mock.patch.object(self.app.messenger, 'publish_card') as publish:
            self.fold()
        self.assertEqual(self.now(self.old_token)['status'], 'superseded')
        self.assertEqual(self.painted, [])
        publish.assert_not_called()

    def test_startup_and_the_outbox_poll_fold_too(self):
        from covey.app.unified import OUTBOX_STEPS, fold_questions
        self.assertIn(fold_questions, OUTBOX_STEPS)  # Every 5 s in the Bridge: within a poll of the new card.
        recover_question_delivery(self.base, [self.app.questions], self.app.runtime.log)
        self.assertEqual(self.now(self.old_token)['status'], 'superseded')
        self.save(self.old)
        fold_questions(SimpleNamespace(apps={'claude': self.app}), self.base)
        self.assertEqual(self.now(self.old_token)['status'], 'superseded')
        self.assertEqual(len(self.painted), 2)

    def test_a_fold_that_fails_never_stops_startup_or_a_message(self):
        self.backend.declare(questions=True, steering=True)
        with mock.patch.object(self.app.questions, 'fold_question', side_effect=OSError('nas')), \
                mock.patch.object(self.app.questions, 'hold_note') as hold:
            recover_question_delivery(self.base, [self.app.questions], self.app.runtime.log)
            self.app.interaction._interactive_command(self.ctx, '另外', 'oc_chat', 'om_reply')
        hold.assert_called_once()  # The message went on as before: held for the questions still open.
        self.app.messenger.send_message.assert_not_called()
        self.assertEqual(sum('question fold failed' in line for line in self.logs), 2)

    def test_each_harness_folds_only_the_questions_of_its_own_tasks(self):
        upsert_job_record(self.ctx.state_dir, 'om_1105', backend='codex')
        self.fold()
        self.assertEqual(self.now(self.old_token)['status'], 'pending')  # No Codex here to draw its card.
        codex = SimpleNamespace(harness=SimpleNamespace(name='codex'), runtime=self.app.runtime,
                                fold_question=mock.Mock(return_value=True), notes=mock.Mock())
        self.fold(self.app.questions, codex)
        ctx, path, newer, job = codex.fold_question.call_args.args
        self.assertEqual((ctx.topic_id, path, newer['token'], job['key']),
                         (self.ctx.topic_id, self.path(self.old_token), self.new_token, 'om_1105'))
        codex.notes.wait_instead.assert_called_once_with(ctx, 'oc_chat', {self.old_token: 1105})
        self.assertEqual(self.painted, [])

    def test_a_message_answers_the_newest_card_once_the_older_one_is_folded(self):
        # Before it, two questions waited: the message was held for both instead of answering #1106's.
        self.backend.declare(questions=True, steering=True)
        self.save({**self.new, 'questions': self.new['questions'][:1]})
        with mock.patch.object(self.app.questions, 'submit_answer') as submit, \
                mock.patch.object(self.app.questions, 'hold_note') as hold:
            self.assertTrue(self.app.interaction._interactive_command(self.ctx, '全部', 'oc_chat', 'om_reply'))
        self.assertEqual(submit.call_args.args[:4], (self.ctx, 'oc_chat', self.new_token, {'answer_0': '全部'}))
        hold.assert_not_called()
        self.assertEqual(self.now(self.old_token)['status'], 'superseded')

    def click(self, message, token=None, **value):
        return self.app.interaction.interaction_action(self.ctx, {
            'chat_id': 'oc_chat', 'event_id': 'late-' + message, 'message_id': message,
            'form_value': {'option_0': '0', 'answer_0': '后来补的', 'feedback': '不合适'},
            'action_value': {'kind': 'bridge_question', 'topic_id': self.ctx.topic_id,
                             'token': token or self.old_token, **value}})

    def test_a_click_on_the_folded_form_is_refused_on_its_card(self):
        # The client still showed #1105's form: 提交回答, 问题不合适, and 提交反馈 on its feedback card.
        self.fold()
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.app.questions.enqueue_continuation = mock.Mock()
        before = self.now(self.old_token)
        for message, value in (('om_card_om_1105', {}), ('om_card_om_1105', {'mode': 'feedback'}),
                               ('om_feedback_card', {'mode': 'feedback_submit'})):
            with self.subTest(value), mock.patch.object(self.app.messenger, 'publish_card') as publish:
                self.assertTrue(self.click(message, **value))
                _, chat, _, source, card = self.app.cards.repaint_browse_card.call_args.args
                self.assertEqual((chat, source), ('oc_chat', message))
                self.assertEqual(card['body']['elements'][-1]['content'],
                                 '⚠️ 这组问题已收起，#1106 又提了问题，请在新卡上回答')
                self.assertIn('已收起 → #1106 又提了问题', text_of(card))
                self.assertNotIn('"tag": "form"', json.dumps(card))
                publish.assert_not_called()  # No feedback card opened.
        self.app.messenger.send_message.assert_not_called()  # Said on the card, not in the topic.
        self.app.questions.enqueue_continuation.assert_not_called()
        self.assertEqual(self.now(self.old_token), before)
        # A form on a task card cannot be redrawn by itself: then the topic is told.
        self.app.cards.repaint_browse_card.return_value = False
        with self.assertRaisesRegex(ValueError, '^这组问题已收起，#1106 又提了问题，请在新卡上回答$'):
            self.click('om_1105-message')
        with self.assertRaises(QuestionFolded):
            self.app.questions.submit_answer(self.ctx, 'oc_chat', self.old_token, {'option_0': '0'}, 'typed')

    def test_a_question_folded_while_its_click_was_read_is_refused_the_same_way(self):
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        real = self.app.questions.submit_answer

        def racing(*args):
            self.fold()  # The outbox poll, between the click reading the form and the answer's own check.
            return real(*args)
        for message, value in (('om_card_om_1105', {}), ('om_feedback_card', {'mode': 'feedback_submit'})):
            with self.subTest(value), mock.patch.object(self.app.questions, 'submit_answer', side_effect=racing):
                self.save(self.old)
                self.assertTrue(self.click(message, **value))
                _, _, _, source, card = self.app.cards.repaint_browse_card.call_args.args
                self.assertEqual(source, message)
                self.assertIn('⚠️ 这组问题已收起', text_of(card))
                self.assertEqual(self.now(self.old_token)['status'], 'superseded')
        self.app.messenger.send_message.assert_not_called()

    def test_a_note_waiting_for_the_folded_question_waits_for_the_newer_one(self):
        from covey.cards.trace_view import clock
        self.app.messenger.publish_card = mock.Mock(side_effect=lambda *a, on_sent=None, **k: (
            on_sent and on_sent('om_note_card'), True)[-1])
        self.app.cards.repaint_browse_card = mock.Mock(return_value=True)
        self.app.questions.hold_note(self.ctx, 'oc_chat', '另外看看日志', 'om_note', [self.old])
        self.fold()
        [note] = [json.loads(p.read_text()) for p in (self.ctx.state_dir / 'held-notes').glob('*.json')]
        self.assertEqual((note['status'], note['question_tokens'], note['waiting'], note['folded']), (
            'held', [self.new_token], [{'job_id': 1106, 'created_at': self.new['created_at'], 'count': 2}],
            [{'job_id': 1105, 'created_at': self.old['created_at'], 'count': 1}]))
        _, _, _, source, card = self.app.cards.repaint_browse_card.call_args.args
        self.assertEqual(source, 'om_note_card')
        new, old = clock(self.new['created_at']), clock(self.old['created_at'])
        self.assertEqual([e['content'] for e in card['body']['elements'][:2]], [
            f'📝 **这条消息还没发给模型**：#1106（{new}）的 2 道题还没答，提交回答时会一起带上',
            f'#1105（{old}）的提问已收起，改等 #1106（{new}）的 2 道题。'])
        # Its buttons act on #1106's questions: 作为问题反馈发送 sends it as their feedback.
        with mock.patch.object(self.app.questions, 'submit_question_feedback') as feedback:
            self.app.questions.note_action(self.ctx, 'oc_chat', {'token': note['token'], 'verb': 'feedback'}, 'om_fb')
        self.assertEqual(feedback.call_args.args[:4], (self.ctx, 'oc_chat', self.new_token, '另外看看日志'))


class QuestionRecoveryTest(QuestionFixture, unittest.TestCase):
    """recover_question_delivery at startup: it writes a question record only when it changes it, so
    an untouched record keeps its mtime (maintenance's last activity of the topic)."""

    def recover(self):
        recover_question_delivery(self.ctx, [self.app.questions], self.app.runtime.log)
        return json.loads(self.path.read_text())

    def test_an_unchanged_question_is_not_rewritten_by_a_restart(self):
        self.assertTrue(self.recover()["restarted"])
        old = time.time() - 9 * 86400
        os.utime(self.path, (old, old))
        # As the file system keeps it: the NAS keeps whole microseconds, so st_mtime is seldom the float set.
        stamped, text = self.path.stat().st_mtime_ns, self.path.read_text()
        self.recover()
        self.maxDiff = None
        self.assertEqual(self.path.read_text(), text)  # Rewritten with a change: the whole diff.
        self.assertEqual(self.path.stat().st_mtime_ns, stamped)

    def test_an_answered_question_shows_its_follow_ups_last_known_state(self):
        from covey.core.jobs import job_path, upsert_job_record
        self.path.write_text(json.dumps({**self.record, "status": "answered", "delivery_state": "waiting_user"}))
        follow_up = dict(message_id="answer-" + self.token, topic_id=self.job.topic_id, chat_id="oc_chat", job_id=99)
        upsert_job_record(self.ctx.state_dir, "answer-" + self.token, status="continued", **follow_up)
        self.assertEqual(self.recover()["delivery_state"], "waiting_user")  # Was overwritten with "continued".
        job_path(self.ctx.state_dir, "answer-" + self.token).unlink()  # continued is final: a new follow-up.
        upsert_job_record(self.ctx.state_dir, "answer-" + self.token, status="interrupted", **follow_up)
        record = self.recover()
        self.assertEqual(record["delivery_state"], "interrupted")
        self.assertIn("已回答 → #99 停住了", json.dumps(question_card(record), ensure_ascii=False))

    def test_a_cancelled_question_stays_cancelled_through_a_restart(self):
        # A live native question the backend withdrew after 提交回答 keeps the submit's awaiting_ack.
        self.path.write_text(json.dumps({**self.record, "status": "cancelled", "delivery_state": "awaiting_ack"}))
        text = self.path.read_text()
        self.assertEqual(self.recover()["status"], "cancelled")  # Was uncertain, with a 核查 that must fail.
        self.assertEqual(self.path.read_text(), text)


class QuestionRecordTest(unittest.TestCase):
    """update_question, the one way a saved question record changes, and release_user_wait."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "questions" / "a.json"
        self.path.parent.mkdir()
        self.save({"token": "a", "status": "pending", "schema_version": 1, "revision": 3})

    def save(self, record):
        self.path.write_text(json.dumps(record))
        os.utime(self.path, ns=(1, 1))

    def saved(self):
        return json.loads(self.path.read_text())

    def untouched(self):
        return self.path.stat().st_mtime_ns == 1

    def test_a_change_is_saved_with_the_next_revision(self):
        record = update_question(self.path, lambda current: {**current, "notes": ["x"]})
        self.assertEqual(record, {"token": "a", "status": "pending", "schema_version": 1, "revision": 4, "notes": ["x"]})
        self.assertEqual(self.saved(), record)
        self.assertEqual(list(self.saved()), ["token", "status", "schema_version", "revision", "notes"])

    def test_a_record_from_before_revisions_gets_one_from_a_transition_only(self):
        self.save({"token": "a", "status": "pending"})
        update_question(self.path, lambda current: {**current, "notes": 1})
        self.assertEqual(list(self.saved().items()), [("token", "a"), ("status", "pending"), ("notes", 1)])
        transition_question(self.path, {"pending"}, task_finished=True)
        self.assertEqual(list(self.saved().items()), [("token", "a"), ("status", "pending"), ("notes", 1),
                                                      ("task_finished", True), ("schema_version", 1), ("revision", 1)])

    def test_nothing_changed_writes_nothing(self):
        for change in (lambda current: current, lambda current: {**current, "status": "pending"}):
            self.assertEqual(update_question(self.path, change)["revision"], 3)
            self.assertTrue(self.untouched())
        self.assertEqual(transition_question(self.path, {"pending"}, status="pending")["revision"], 3)
        self.assertTrue(self.untouched())

    def test_a_change_inside_a_field_is_a_change(self):
        self.save({"token": "a", "status": "pending", "answers": {"q": 1}})
        def change(current):
            current["answers"]["q"] = 2
            return current
        self.assertEqual(update_question(self.path, change)["answers"], {"q": 2})
        self.assertFalse(self.untouched())

    def test_a_change_may_leave_the_record(self):
        self.assertIsNone(update_question(self.path, lambda current: None))
        self.assertTrue(self.untouched())

    def test_another_status_than_expected_runs_no_change(self):
        called = []
        self.assertIsNone(update_question(self.path, called.append, expected={"answered"}))
        self.assertEqual(called, [])
        self.assertIsNone(transition_question(self.path, {"answered"}, status="uncertain"))
        self.assertTrue(self.untouched())

    def test_a_status_moves_only_along_the_transitions(self):
        with self.assertRaisesRegex(ValueError, "无效的问题状态转换：pending → answered"):
            update_question(self.path, lambda current: {**current, "status": "answered"})
        self.assertTrue(self.untouched())
        self.assertEqual(update_question(self.path, lambda current: {**current, "status": "submitting"})["status"],
                         "submitting")

    def test_a_delivered_answer_does_not_go_back_to_awaiting_its_ack(self):
        self.save({"token": "a", "status": "submitting", "delivery_state": "delivered", "schema_version": 1, "revision": 3})
        record = transition_question(self.path, {"submitting"}, delivery_state="awaiting_ack")
        self.assertEqual(record["delivery_state"], "delivered")
        self.assertTrue(self.untouched())

    def test_withdrawing_the_last_live_question_releases_the_checkpoint(self):
        state = self.path.parent.parent
        checkpoint = state / "checkpoint.json"
        checkpoint.write_text(json.dumps({"requires_user": True, "question": "哪个？"}))
        live = {"topic_id": "topic", "chat_id": "chat", "job_id": 7, "native_live_question": True, "status": "pending",
                "schema_version": 1, "revision": 1}
        self.save({**live, "token": "a", "request_id": "r1"})
        (self.path.parent / "b.json").write_text(json.dumps({**live, "token": "b", "request_id": "r2"}))
        painted = []
        questions = SimpleNamespace(paint_question_state=lambda _, record: painted.append(record["token"]))
        job = SimpleNamespace(job_id=7, topic_id="topic", chat_id="chat", checkpoint_path=str(checkpoint))
        _cancel_live_question(questions, SimpleNamespace(state_dir=state), job, {"requestId": "r1"})
        self.assertEqual(self.saved()["status"], "cancelled")
        self.assertEqual(painted, ["a"])
        self.assertTrue(json.loads(checkpoint.read_text())["requires_user"])  # b still waits.
        _cancel_live_question(questions, SimpleNamespace(state_dir=state), job, {"requestId": "r2"})
        self.assertEqual(json.loads(checkpoint.read_text()), {"requires_user": False, "question": ""})

    def test_release_user_wait(self):
        checkpoint = self.path.parent / "checkpoint.json"
        waiting = {"phase": "等", "requires_user": True, "question": "哪个？", "questions": [{"id": "q"}]}
        checkpoint.write_text(json.dumps(waiting))
        release_user_wait(checkpoint)
        self.assertEqual(checkpoint.read_text(), json.dumps({**waiting, "requires_user": False, "question": ""},
                                                            ensure_ascii=False))
        release_user_wait(checkpoint, drop_questions=True)
        self.assertEqual(json.loads(checkpoint.read_text()), {"phase": "等", "requires_user": False, "question": ""})


class SettledStampTest(unittest.TestCase):
    """core/store.py SETTLED_STAMPS: a question record says when it stopped waiting. 2026-11-19 T550: it said nothing, so the
    web page's 专项报告 drew every answered or cancelled question as waiting until now."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "q.json"
        write_private_text(self.path, json.dumps({"token": "q", "status": "pending", "created_at": 1.0}))

    def test_an_answer_is_stamped_when_submitted_and_keeps_it(self):
        before = time.time()
        submitted = transition_question(self.path, {"pending"}, status="submitting")
        self.assertGreaterEqual(submitted["answered_at"], before)
        answered = transition_question(self.path, {"submitting"}, status="answered")
        self.assertEqual(answered["answered_at"], submitted["answered_at"])
        self.assertNotIn("cancelled_at", answered)

    def test_a_cancellation_and_a_fold_are_stamped_unless_the_change_wrote_its_own(self):
        cancelled = transition_question(self.path, {"pending"}, status="cancelled")
        self.assertGreater(cancelled["cancelled_at"], 1.0)
        write_private_text(self.path, json.dumps({"token": "q", "status": "pending"}))
        self.assertEqual(transition_question(self.path, {"pending"}, status="cancelled", cancelled_at=5.0)["cancelled_at"], 5.0)
        write_private_text(self.path, json.dumps({"token": "q", "status": "pending"}))
        self.assertIn("cancelled_at", transition_question(self.path, {"pending"}, status="superseded"))

    def test_a_write_that_does_not_move_the_status_stamps_nothing(self):
        record = update_question(self.path, lambda row: {**row, "mirror_state": "sent"})
        self.assertEqual(set(record) - {"mirror_state"}, {"token", "status", "created_at"})

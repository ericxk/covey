"""Synthetic app-to-worker delivery regressions; every transport is in-memory."""
from __future__ import annotations

import contextlib
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_job
from support.worker_seams import leave_out_side_work
from support.fake_lark import MemoryTransport, install_card_transport

from covey.app.questions import recover_question_delivery
from covey.core.checkpoint import CHECKPOINT_RULES
from covey.core.harness import RunResult
from covey.cards.question_cards import question_element
from covey.core.jobs import BridgeReplyJob, find_job_record, job_path, upsert_job_record
from covey.core.longtask import TaskMonitor
from covey.app.prompts import RESUME_REMINDERS, bridge_rules
from covey.core.state import write_private_text
from covey.core.history_storage import recent_history
from covey.app.task_card import StreamingReply
from covey.app.outbox import AUDIT_REFUSED_NOTE, REDELIVER_LABEL, retry_label
from covey.app.recovery_cards import recovery_card
from covey.app.redelivery import REDELIVERED_STATUS
from covey.core.checkpoint import QUESTION_ONLY_REPLY, checkpoint_reply_display
from support import make_context
from support.world import build_claude_app

QUESTION = "是否按以上计划继续？"
BODY = "# 方案首段\n已核实的约束与方案如下。\n\n" + ("逐项说明执行顺序、验证依据和停止条件。\n" * 230) + "\n# 方案中段\n保留不同选项的决策背景。\n\n# 方案尾段\n等待你的选择。"


class PublicBackend:
    def __init__(self, text, events, *, native=False, questions=None):
        self.text, self.events, self.native = text, events, native
        self.questions = questions
        self.calls = 0

    def prompt_runtime_note(self, ctx):
        return "synthetic backend; no external operations"

    def run(self, request):
        self.calls += 1
        checkpoint = {"requires_user": True, "question": QUESTION, "phase": "awaiting-decision", "artifacts": []}
        if self.questions is not None:
            checkpoint["questions"] = self.questions
        write_private_text(Path(request.inputs.checkpoint_path), json.dumps(checkpoint))
        if self.native:
            control = SimpleNamespace(native_blocking_questions=True, thread_id="synthetic-session",
                wait_for_answer=mock.Mock(), reject_question=mock.Mock())
            request.callbacks.event("question", {"request_id": "native-request", "isBlocking": True,
                "questions": [{"id": "answer", "question": QUESTION}]}, control)
            control.wait_for_answer.assert_not_called()
        self.events.append(("backend_return", self.text))
        return RunResult(self.text, request.session_id)


class ReplyPreservationTest(unittest.TestCase):
    def case(self, *, body=BODY, with_card=True, limit=20000, native=False, backend_name="maso", questions=None, question_store_error=False, **transport):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        tx = MemoryTransport(**transport)
        tx.question_store_error = question_store_error
        backend = PublicBackend(body, tx.events, native=native, questions=questions)
        app = build_claude_app(backend)
        app.harness.spec = dataclasses.replace(app.spec, name=backend_name)
        ctx = dataclasses.replace(make_context(root), topic_id="synthetic-topic", reply_to_message_id="om-root")
        stack.enter_context(mock.patch("covey.app.worker.context_for_job", lambda _, job: ctx))
        app.sessions.capture_session = lambda _: (0, None)
        app.sessions.record_session_from_worker = mock.Mock()
        app.runtime.log_sink = mock.Mock()
        app.messenger.send_message = tx.send_text
        app.artifacts.send_result_file = tx.send_file
        app.cards.finish_result_controls = mock.Mock()
        app.cards.streaming = False
        app.cards.stream = dataclasses.replace(app.cards.stream, card_limit=limit,
            replacement_limit=0, send_text_message=tx.send_text)
        install_card_transport(stack, tx)
        def save_question(path, text):
            if Path(path).parent.name == "questions" and tx.question_store_error:
                raise OSError("synthetic question storage failure")
            return write_private_text(path, text)
        stack.enter_context(mock.patch("covey.app.questions.write_private_text", side_effect=save_question))
        stack.enter_context(mock.patch("covey.app.native_questions.write_private_text", side_effect=save_question))
        stack.enter_context(mock.patch("covey.app.question_display.write_private_text", side_effect=save_question))
        leave_out_side_work(stack)
        job = BridgeReplyJob(job_id=8, user_text="请给出方案及待确认问题", chat_id="oc-unit", message_id="om-job",
            topic_id=ctx.topic_id, reply_to_message_id="om-root", timeout=0, model="saved-model", effort="high",
            model_settings_snapshot=True, progress=StreamingReply("result-card", "om-result") if with_card else None)
        app.process_reply_job(job, ctx)
        record = find_job_record(ctx.state_dir, job.message_id)
        questions = [json.loads(p.read_text()) for p in (ctx.state_dir / "questions").glob("*.json")]
        self.assertEqual(backend.calls, 1)
        self.assertEqual(record["final_text"], body)
        if body:
            self.assertEqual(recent_history(ctx.history_path)[-1]["content"], body)
        return SimpleNamespace(app=app, ctx=ctx, job=job, tx=tx, backend=backend, record=record, question=questions[0] if questions else None)

    def assert_body_before_question(self, c, kind):
        body = next(i for i, e in enumerate(c.tx.events) if e[0] == kind and e[2] and "方案尾段" in e[1])
        forms = [i for i, e in enumerate(c.tx.events) if e[0] in {"question_insert", "question_message"} and e[2]]
        self.assertEqual(len(forms), 1)
        self.assertLess(body, forms[0])
        self.assertTrue(c.record["delivery_ok"])
        self.assertTrue(c.record["question_delivery_ok"])
        self.assertEqual(c.record["status"], "waiting_user")
        self.assertFalse(c.question["display_pending"])

    def test_multisection_body_stays_above_single_form_on_same_card(self):
        c = self.case()
        self.assert_body_before_question(c, "body_card")
        elements = c.tx.cards["result-card"]
        self.assertIn("方案首段", elements[0]["content"])
        self.assertIn("方案中段", elements[0]["content"])
        self.assertIn("方案尾段", elements[0]["content"])
        self.assertEqual(elements[1]["tag"], "form")
        self.assertEqual(json.dumps(elements, ensure_ascii=False).count(QUESTION), 1)

    def test_codex_uses_same_complete_body_path(self):
        c = self.case(backend_name="codex")
        self.assert_body_before_question(c, "body_card")

    def test_no_card_delivers_text_before_standalone_question(self):
        self.assert_body_before_question(self.case(with_card=False), "body_text")

    def test_failed_card_does_not_put_form_back_on_placeholder(self):
        c = self.case(card_ok=False)
        self.assert_body_before_question(c, "body_text")
        self.assertFalse(any(e[0] == "question_insert" for e in c.tx.events))

    def test_oversize_card_sends_complete_file_before_new_question(self):
        c = self.case(limit=300)
        self.assert_body_before_question(c, "body_file")
        self.assertFalse(any(e[0] == "question_insert" for e in c.tx.events))
        self.assertEqual(next(e[1] for e in c.tx.events if e[0] == "body_file"), BODY)

    def test_file_failure_can_fall_back_to_complete_text(self):
        self.assert_body_before_question(self.case(limit=300, file_ok=False), "body_text")

    def test_file_exception_does_not_treat_card_preview_as_full_delivery(self):
        c = self.case(limit=300, file_error=True)
        self.assertFalse(c.record["delivery_ok"])
        self.assertEqual(c.record["status"], "failed")
        self.assertFalse(any(e[0].startswith("question_") for e in c.tx.events))

    def test_failed_body_blocks_form_callback_restart_and_plain_answer(self):
        c = self.case(limit=300, text_ok=False, file_ok=False)
        self.assertFalse(c.record["delivery_ok"])
        self.assertEqual(c.record["status"], "failed")
        self.assertEqual(c.app.cards.finish_result_controls.call_args.args[-1], "failed")
        self.assertIn("完整结果尚未成功发送", c.tx.cards["result-card"][0]["content"])
        self.assertEqual(c.record["execution_status"], "waiting_user")
        self.assertFalse(any(e[0].startswith("question_") for e in c.tx.events))
        self.assertEqual(c.app.questions.pending_questions(c.ctx, "oc-unit"), [])
        self.assertNotIn("form", json.dumps(question_element(c.question)))
        with self.assertRaisesRegex(ValueError, "不能提前确认"):
            c.app.questions.submit_answer(c.ctx, "oc-unit", c.question["token"], {"answer_0": "认可"}, "event-answer")
        before = len(c.tx.events)
        recover_question_delivery(c.ctx, [c.app.questions], c.app.runtime.log)
        self.assertEqual(len(c.tx.events), before)
        self.assertEqual(c.backend.calls, 1)
        c.tx.file_ok = True
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-event"))
        self.assertEqual(c.backend.calls, 1)
        self.assert_body_before_question(SimpleNamespace(**{**vars(c), "record": find_job_record(c.ctx.state_dir, c.job.message_id),
            "question": json.loads((c.ctx.state_dir / "questions" / (c.question["token"] + ".json")).read_text())}), "body_file")
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["model"], "saved-model")
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["effort"], "high")

    def test_receive_does_not_enqueue_plain_text_before_full_delivery(self):
        c = self.case(with_card=False, text_ok=False, file_ok=False)
        c.app.runtime.owner_open_id = "ou-unit"
        c.app.submission.event_context = lambda ctx, event: c.ctx
        c.app.handle_event({"sender_id": "ou-unit", "chat_id": "oc-unit", "message_id": "om-early-answer",
                             "content": "认可"}, c.ctx, fetch_message_details=False)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertEqual(c.backend.calls, 1)
        self.assertTrue(any("本条没有续接或排队" in str(e[1]) for e in c.tx.events))
        # A slash message is ordinary text now: it waits for the redelivery like any other.
        self.assertTrue(c.app.interaction._interactive_command(c.ctx, "/retry", "oc-unit", "command-event"))
        self.assertFalse(c.app.interaction._interactive_command(c.ctx, "新消息", "oc-other", "other-chat"))

    def test_empty_original_reply_can_be_redelivered_without_model_run(self):
        c = self.case(body="", with_card=False, text_ok=False, file_ok=False)
        c.tx.text_ok = True
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "empty-retry"))
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["status"], "waiting_user")
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["final_text"], "")
        self.assertEqual(c.backend.calls, 1)

    def test_native_blocking_question_remains_immediate(self):
        c = self.case(native=True, backend_name="codex")
        question_index = next(i for i, e in enumerate(c.tx.events) if e[0] == "question_insert")
        return_index = next(i for i, e in enumerate(c.tx.events) if e[0] == "backend_return")
        self.assertLess(question_index, return_index)
        self.assertNotIn("body_delivery_required", c.question)
        self.assertEqual(len(c.app.questions.pending_questions(c.ctx, "oc-unit")), 1)

    def test_question_failure_retries_form_without_rerunning_or_resending_body(self):
        c = self.case(question_ok=False)
        self.assertTrue(c.record["delivery_ok"])
        self.assertFalse(c.record["question_delivery_ok"])
        self.assertEqual(c.record["status"], "failed")
        self.assertEqual(c.app.cards.finish_result_controls.call_args.args[-1], "failed")
        before = sum(e[0].startswith("body_") for e in c.tx.events)
        c.tx.question_ok = True
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-question"))
        self.assertEqual(sum(e[0].startswith("body_") for e in c.tx.events), before)
        self.assertEqual(c.backend.calls, 1)
        count = len(c.tx.events)
        self.assertTrue(c.app.questions.deliver_checkpoint_questions(c.ctx, c.job, False))
        self.assertEqual(len(c.tx.events), count)

    def test_empty_or_exact_question_is_honest_without_changing_saved_reply(self):
        for body in ("", QUESTION):
            with self.subTest(body=body):
                c = self.case(body=body, with_card=False)
                text = next(e[1] for e in c.tx.events if e[0] == "body_text")
                self.assertEqual(text, QUESTION_ONLY_REPLY)
                self.assertEqual(c.record["final_text"], body)
                self.assertEqual(sum(e[0] == "question_message" and e[2] for e in c.tx.events), 1)
        different = "这段决策背景很重要。" + QUESTION
        self.assertEqual(checkpoint_reply_display(different, QUESTION, True), different)
        self.assertEqual(checkpoint_reply_display("用另一种措辞询问", QUESTION, True), "用另一种措辞询问")

    def test_exact_trailing_question_is_display_only_deduplicated(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original)
        self.assertEqual(c.record["final_text"], original)
        self.assertEqual(json.dumps(c.tx.cards["result-card"], ensure_ascii=False).count(QUESTION), 1)
        self.assertNotIn(QUESTION, c.tx.cards["result-card"][0]["content"])
        self.assertIn("方案尾段", c.tx.cards["result-card"][0]["content"])

    def test_failed_form_restores_the_only_question_from_canonical_reply(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original, question_ok=False)
        self.assertEqual(c.record["status"], "failed")
        self.assertEqual(c.record["final_text"], original)
        self.assertIn(original, c.tx.cards["result-card"][0]["content"])
        self.assertFalse(c.record["question_delivery_ok"])
        c.tx.question_ok = True
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-form"))
        self.assertEqual(c.backend.calls, 1)

    def test_invalid_checkpoint_question_does_not_replace_successful_body(self):
        c = self.case(questions=["not-a-question-object"])
        self.assertEqual(c.record["status"], "failed")
        self.assertEqual(c.record["execution_status"], "waiting_user")
        self.assertTrue(c.record["delivery_ok"])
        self.assertFalse(c.record["question_delivery_ok"])
        self.assertIn("登记失败", c.record["checkpoint_question_error"])
        self.assertIn(BODY, c.tx.cards["result-card"][0]["content"])
        self.assertIsNone(c.question)
        self.assertNotIn("调用失败", c.record["final_text"])

    def test_a_checkpoint_question_marked_multiple_gets_a_multi_select(self):
        # T290 2026-11-03: "可以多选" in the question text alone gave a single select.
        options = [{"label": "工作区"}, {"label": "运行方式"}, {"label": "不加"}]
        c = self.case(questions=[{"id": "extras", "question": "还要能换哪些？", "options": options, "multiple": True},
                                 {"id": "layout", "question": "卡片样式？", "options": options[:2], "multiSelect": True},
                                 {"id": "scope", "question": "范围？", "options": options[:2]}])
        selects = [e for e in json.loads(json.dumps(c.tx.cards["result-card"]))[1]["elements"]
                   if str(e.get("name", "")).startswith("option_")]
        self.assertEqual([e["tag"] for e in selects], ["multi_select_static", "multi_select_static", "select_static"])
        self.assertEqual([q["multiple"] for q in c.question["questions"]], [True, True, False])
        self.assertIn('"multiple":true', CHECKPOINT_RULES)

    def test_question_storage_failure_keeps_body_and_can_retry_registration(self):
        c = self.case(question_store_error=True)
        self.assertEqual(c.record["status"], "failed")
        self.assertEqual(c.record["execution_status"], "waiting_user")
        self.assertTrue(c.record["delivery_ok"])
        self.assertIn(BODY, c.tx.cards["result-card"][0]["content"])
        c.tx.question_store_error = False
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-register"))
        self.assertEqual(c.backend.calls, 1)
        current = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertEqual(current["final_text"], BODY)
        self.assertTrue(current["question_delivery_ok"])

    def test_registration_and_body_failure_block_receive_until_stop(self):
        c = self.case(with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        self.assertIsNone(c.question)
        self.assertFalse(c.record["delivery_ok"])
        c.app.runtime.owner_open_id = "ou-unit"
        c.app.submission.event_context = lambda ctx, event: c.ctx
        c.app.handle_event({"sender_id": "ou-unit", "chat_id": "oc-unit", "message_id": "om-no-record-answer",
                             "content": "认可"}, c.ctx, fetch_message_details=False)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertEqual(c.backend.calls, 1)
        self.assertTrue(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))

    def test_a_later_started_job_of_the_topic_lifts_the_delivery_gate(self):
        # T252 (2026-10-31): #1812's reply never arrived, #1813 queued behind it ran, and #1812 still held every message.
        c = self.case(with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        later = {"topic_id": c.ctx.topic_id, "chat_id": "oc-unit", "job_id": c.job.job_id + 1}
        upsert_job_record(c.ctx.state_dir, "om-later-queued", status="queued", **later)
        self.assertTrue(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))  # Not started yet.
        upsert_job_record(c.ctx.state_dir, "om-other-topic", status="done", **{**later, "topic_id": "other"})
        self.assertTrue(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))
        upsert_job_record(c.ctx.state_dir, "om-later-queued", status="done")
        self.assertFalse(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))

    def test_a_result_feishu_audit_refused_does_not_hold_the_topic_or_offer_redelivery(self):
        c = self.case(with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        self.assertEqual(retry_label(find_job_record(c.ctx.state_dir, c.job.message_id)), REDELIVER_LABEL)
        upsert_job_record(c.ctx.state_dir, c.job.message_id, delivery_error="audit_rejected")
        self.assertFalse(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertEqual(retry_label(record), "")
        self.assertIn(AUDIT_REFUSED_NOTE, json.dumps(recovery_card(record), ensure_ascii=False))

    def test_retry_repairs_registration_and_body_failure_then_removes_gate(self):
        c = self.case(with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        self.assertTrue(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))
        c.tx.text_ok = True
        c.tx.question_store_error = False
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "repair-both"))
        self.assertFalse(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))
        self.assertEqual(c.backend.calls, 1)
        self.assertEqual(len(c.app.questions.pending_questions(c.ctx, "oc-unit")), 1)

    def test_retry_keeps_unique_question_visible_when_form_delivery_fails(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original, with_card=False, text_ok=False, file_ok=False)
        c.tx.text_ok = True
        c.tx.question_ok = False
        self.assertIn("问题控件仍未能发送", c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-full-original"))
        self.assertTrue(any(e[0] == "body_text" and e[1] == original and e[2] for e in c.tx.events))
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["final_text"], original)
        self.assertEqual(c.backend.calls, 1)

    def test_successful_retry_shows_exact_trailing_question_once(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original, with_card=False, text_ok=False, file_ok=False)
        before = len(c.tx.events)
        c.tx.text_ok = True
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-once"))
        visible = [e[1] for e in c.tx.events[before:] if e[0] in {"body_text", "question_message"} and e[2]]
        self.assertEqual(json.dumps(visible, ensure_ascii=False).count(QUESTION), 1)
        self.assertTrue(any(e[0] == "body_text" and e[1] == BODY and e[2] for e in c.tx.events[before:]))
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["final_text"], original)
        self.assertEqual(recent_history(c.ctx.history_path)[-1]["content"], original)
        self.assertEqual(c.backend.calls, 1)

    def test_retry_original_restore_failure_keeps_delivery_gate_closed(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original, with_card=False, text_ok=False, file_ok=False)
        c.tx.question_ok = False
        def only_display_succeeds(profile, chat, text, key, **kwargs):
            ok = text != original
            c.tx.events.append(("body_text", text, ok))
            return ok
        c.app.messenger.send_message = only_display_succeeds
        self.assertIn("原始回复仍未能完整发送", c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-restore-fails"))
        current = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertFalse(current["delivery_ok"])
        self.assertFalse(current["question_delivery_ok"])
        self.assertEqual(current["final_text"], original)
        self.assertTrue(c.app.questions.undelivered_checkpoint_jobs(c.ctx, "oc-unit"))
        with self.assertRaisesRegex(ValueError, "不能提前确认"):
            c.app.questions.submit_answer(c.ctx, "oc-unit", c.question["token"], {"answer_0": "认可"}, "blocked")
        self.assertEqual(c.backend.calls, 1)

    def test_retry_registration_failure_restores_original_question(self):
        original = BODY + "\n\n" + QUESTION
        c = self.case(body=original, with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        c.tx.text_ok = True
        self.assertIn("问题仍未能登记", c.app.controls.retry_topic(c.ctx, "oc-unit", "retry-registration-fails"))
        self.assertTrue(any(e[0] == "body_text" and e[1] == original and e[2] for e in c.tx.events))
        self.assertEqual(find_job_record(c.ctx.state_dir, c.job.message_id)["final_text"], original)
        self.assertEqual(c.backend.calls, 1)

    def test_restart_recovers_saved_registration_intent_without_question_record(self):
        c = self.case(with_card=False, question_store_error=True, text_ok=False, file_ok=False)
        self.assertTrue(c.record["checkpoint_question_registration_pending"])
        self.assertIsNone(c.question)
        interrupted = {**c.record, "status": "running", "checkpoint_question_error": ""}
        # By hand: the record a Bridge stopped mid-run left, back to running past upsert_job_record's transitions.
        write_private_text(job_path(c.ctx.state_dir, c.record["key"]), json.dumps(interrupted))
        before = len(c.tx.events)
        recover_question_delivery(c.ctx, [c.app.questions], c.app.runtime.log)
        c.app.notify_stale_jobs(c.ctx)
        self.assertEqual(len(c.tx.events), before)
        recovered = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["final_text"], BODY)
        self.assertIn("登记尚未完成", recovered["checkpoint_question_error"])
        c.tx.text_ok = True
        c.tx.question_store_error = False
        self.assertIn(REDELIVERED_STATUS, c.app.controls.retry_topic(c.ctx, "oc-unit", "repair-registration-intent"))
        current = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertFalse(current["checkpoint_question_registration_pending"])
        self.assertEqual(c.backend.calls, 1)

    def test_legacy_pending_question_is_not_migrated_on_restart(self):
        # MASO and Claude now keep native questions live in the task;
        # Codex still records the earlier plain pending-question shape.
        c = self.case(native=True, backend_name="codex")
        before = dict(c.question)
        recover_question_delivery(c.ctx, [c.app.questions], c.app.runtime.log)
        current = json.loads((c.ctx.state_dir / "questions" / (before["token"] + ".json")).read_text())
        for key in ("token", "request_id", "questions", "backend_session_id"):
            self.assertEqual(current[key], before[key])
        self.assertNotIn("body_delivery_required", current)
        self.assertEqual(len(c.app.questions.pending_questions(c.ctx, "oc-unit")), 1)

    def test_checkpoint_monitor_does_not_request_approval_before_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "checkpoint.json"
            write_private_text(p, json.dumps({"requires_user": True, "question": QUESTION}))
            job = make_job()
            updates, states = [], []
            monitor = TaskMonitor(job, p, updates.append, lambda **x: states.append(x), mock.Mock(), mock.Mock(), mock.Mock())
            monitor.tick()
            self.assertNotIn(QUESTION, updates[-1])
            self.assertNotIn(QUESTION, states[-1]["long_task_summary"])
            job.native_question_active = True
            monitor.last_ui = float("-inf")
            monitor.tick()
            self.assertIn(QUESTION, updates[-1])

    def test_late_delivery_update_cannot_undo_continued_or_cancelled(self):
        from covey.core.statuses import merge_job_record
        for status in ("continued", "cancelled"):
            current = {"status": status, "execution_status": "waiting_user", "final_text": BODY,
                       "checkpoint_question_deferred": True}
            for change in ({"status": "waiting_user", "delivery_ok": True},
                           {"status": "failed", "question_delivery_ok": False}):
                self.assertEqual(merge_job_record(current, change)["status"], status)

    def test_prompt_contract_preserves_plan_before_question(self):
        self.assertIn("最终回复先交代已形成的方案", CHECKPOINT_RULES)
        self.assertIn("不得声称已有方案或编造内容", CHECKPOINT_RULES)
        self.assertIn("写它不算修改工作区文件；用户要求不改文件时也照常写检查点", CHECKPOINT_RULES)
        rules = bridge_rules(backend_name="unit", workspace_name="unit", workdir=Path("."))
        self.assertNotIn("Keep replies concise and useful", rules)
        self.assertIn("用户要的完整方案、证据和决策背景都写全", rules)
        self.assertIn(CHECKPOINT_RULES, rules)
        self.assertIn("要确认之前，先把用户要的方案和决策背景写完整", RESUME_REMINDERS)


if __name__ == "__main__":
    unittest.main()

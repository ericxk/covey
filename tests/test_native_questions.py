"""Codex native questions: delivery, the shared delivery gates and the paused-question proof."""

from __future__ import annotations

import json
import queue
import time
from types import SimpleNamespace
from unittest import mock

from support.codex_questions import ANSWER_TEXT, CodexNativeQuestionCase, QUESTION
from support.fake_backends import CodexRunCase, FakeRpc, OTHER, RUN, SECRET, SID, TURN, agent, finished, turn, codex_server

import covey.backends.codex.appserver as appserver
import covey.core.session_guard as guard
from covey.app.outbox import tick
from covey.backends.codex.rpc import RpcError
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.reply_state import failure_text
from covey.core.saved_results import load_authorized_reply
from covey.core.session_guard import session_is_blocked


class CodexNativeQuestionDeliveryTests(CodexNativeQuestionCase):
    def test_outbox_restores_paused_question_without_resuming_the_model(self):
        c = self.prepare(body_ok=False)
        self.start_question(c)
        # Simulate restart after the durable pause, before the first delivery
        # attempt was recorded. The worker may only publish the proven question.
        upsert_job_record(c.ctx.state_dir, c.job.message_id, outbox_state='pending', outbox_next_at=0)
        c.tx.text_ok = c.tx.file_ok = c.tx.question_ok = True
        c.app.harness.context = c.ctx
        tick(SimpleNamespace(runtime=c.app.runtime, apps={c.app.spec.name: c.app}), c.ctx, now=time.time() + 1000)
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertTrue(record['delivery_ok'])
        self.assertTrue(record['question_delivery_ok'])
        self.assertEqual(record['status'], 'waiting_user')
        self.assertEqual(record['outbox_state'], 'delivered')
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertTrue(session_is_blocked(c.ctx, 'codex', SID))
        self.answer_and_execute(c)  # Only the user's answer permits continuation.

    def test_native_question_body_failure_retry_then_answer(self):
        c = self.prepare(body_ok=False, form_ok=True)
        record = self.start_question(c)
        self.assertFalse(record["delivery_ok"])
        before = self.snapshot(c)
        c.tx.text_ok = c.tx.file_ok = True
        response = c.app.controls.retry_topic(c.ctx, c.job.chat_id, "native-body-retry", job_id=c.job.job_id)
        self.evidence(c, before_retry=before, retry_response=response)
        self.assertIn("重新投递", response)
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertTrue(find_job_record(c.ctx.state_dir, c.job.message_id)["delivery_ok"])
        self.answer_and_execute(c)
        self.evidence(c, before_retry=before, retry_response=response, answer_continued=True)

    def test_native_question_form_failure_retry_then_answer(self):
        c = self.prepare(body_ok=True, form_ok=False)
        self.start_question(c)
        self.assertTrue(any(e[0] == "question_message" and not e[2] for e in c.tx.events))
        self.assertFalse(any(e[0] == "question_message" and e[2] for e in c.tx.events))
        before = self.snapshot(c)
        before_events = len(c.tx.events)
        c.tx.question_ok = True
        response = c.app.controls.retry_topic(c.ctx, c.job.chat_id, "native-form-retry", job_id=c.job.job_id)
        self.evidence(c, before_retry=before, retry_response=response)
        self.assertTrue(any(e[0] == "question_message" and e[2] for e in c.tx.events[before_events:]))
        self.assertEqual(c.backend_run.call_count, 1)
        self.answer_and_execute(c)
        self.evidence(c, before_retry=before, retry_response=response, answer_continued=True)

    def test_native_question_successful_delivery_allows_answer_resume(self):
        c = self.prepare(body_ok=True, form_ok=True)
        self.start_question(c)
        self.assertTrue(any(e[0] == "question_message" and e[2] for e in c.tx.events))
        self.assertTrue(find_job_record(c.ctx.state_dir, c.job.message_id)["delivery_ok"])
        self.evidence(c, before_answer=True)
        self.answer_and_execute(c)
        self.evidence(c, answer_continued=True)


class CodexNativeQuestionGateTests(CodexNativeQuestionCase):
    def submit(self, c, event="edge-answer"):
        question = json.loads(c.question_path.read_text())
        return c.app.questions.submit_answer(c.ctx, c.job.chat_id, question["token"], {"answer_0": "Approved"}, event)

    def assert_blocked(self, c):
        before = json.loads(c.question_path.read_text())["status"]
        with self.assertRaises(ValueError):
            self.submit(c)
        self.assertEqual(json.loads(c.question_path.read_text())["status"], before)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertEqual(c.backend_run.call_count, 1)
        c.app.runtime.owner_open_id = "ou-unit"
        c.app.submission.event_context = lambda *_: c.ctx
        c.app.handle_event({"sender_id": "ou-unit", "chat_id": c.job.chat_id,
            "message_id": "om-ordinary-before-delivery", "content": "Approved"}, c.ctx, fetch_message_details=False)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertEqual(json.loads(c.question_path.read_text())["status"], before)

    def test_body_failure_blocks_answer_then_retry_delivers_body_and_form(self):
        c = self.prepare(body_ok=False)
        self.start_question(c)
        self.assertFalse(any(e[0] == "question_message" and e[2] for e in c.tx.events))
        self.assert_blocked(c)
        c.tx.text_ok = c.tx.file_ok = True
        before = len(c.tx.events)
        response = c.app.controls.retry_topic(c.ctx, c.job.chat_id, "edge-body-retry", job_id=c.job.job_id)
        self.assertIn("重新投递", response)
        self.assertTrue(any(e[0] in {"body_text", "body_file"} and e[2] for e in c.tx.events[before:]))
        self.assertEqual(sum(e[0] == "question_message" and e[2] for e in c.tx.events[before:]), 1)
        self.assertEqual(c.backend_run.call_count, 1)
        self.answer_and_execute(c)
        self.evidence(c, answer_was_blocked_before_retry=True, answer_continued=True)

    def test_form_failure_blocks_answer_and_retry_does_not_repeat_body(self):
        c = self.prepare(form_ok=False)
        self.start_question(c)
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertTrue(record["delivery_ok"])
        self.assertFalse(record["question_delivery_ok"])
        self.assert_blocked(c)
        prior_bodies = {e[1] for e in c.tx.events if e[0] == "body_text" and e[2]}
        before = len(c.tx.events)
        c.tx.question_ok = True
        response = c.app.controls.retry_topic(c.ctx, c.job.chat_id, "edge-form-retry", job_id=c.job.job_id)
        self.assertIn("重新投递", response)
        later = c.tx.events[before:]
        self.assertEqual(sum(e[0] == "question_message" and e[2] for e in later), 1)
        self.assertFalse(any(e[0] == "body_text" and e[1] in prior_bodies for e in later))
        self.assertFalse(any(e[0] == "body_file" for e in later))
        self.assertEqual(c.backend_run.call_count, 1)
        self.answer_and_execute(c)
        self.evidence(c, answer_was_blocked_before_retry=True, body_not_repeated=True, answer_continued=True)

    def test_duplicate_answers_before_execution_queue_and_execute_once(self):
        c = self.prepare()
        self.start_question(c)
        self.submit(c, "first-answer")
        self.assertEqual(c.app.runtime.jobs.queue.qsize(), 1)
        with self.assertRaises(ValueError):
            self.submit(c, "second-answer")
        self.assertEqual(c.app.runtime.jobs.queue.qsize(), 1)
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertTrue(session_is_blocked(c.ctx, "codex", SID))
        self.assertIsNone(c.app.sessions.capture_session(c.ctx)[1])
        self.assertIsNone(json.loads(c.question_path.read_text()).get("native_answer_run_id"))
        job = c.app.runtime.jobs.queue.get_nowait()
        transfers, backend_entries = [], []
        original_transfer = guard.transfer_question_session
        def transfer(ctx, backend, sid, source_run_id, answer_run_id, token):
            path = guard._path(ctx, backend, sid)
            before = json.loads(path.read_text())
            self.assertEqual(before["state"], "blocked")
            self.assertEqual(before["run_id"], c.job.bridge_run_id)
            result = original_transfer(ctx, backend, sid, source_run_id, answer_run_id, token)
            after = json.loads(path.read_text())
            self.assertEqual(after["state"], "active")
            self.assertEqual(after["run_id"], job.bridge_run_id)
            self.assertEqual(after["question_token"], token)
            self.assertNotEqual(source_run_id, answer_run_id)
            transfers.append({"source_run_id": source_run_id, "answer_run_id": answer_run_id,
                              "guard_present_before_and_after": True})
            return result
        self.stack.enter_context(mock.patch.object(guard, "transfer_question_session", side_effect=transfer))
        original_run = c.backend_run._mock_wraps
        def continued_run(request):
            saved_question = json.loads(c.question_path.read_text())
            active = json.loads(guard._path(c.ctx, "codex", SID).read_text())
            self.assertEqual(request.session_id, SID)
            self.assertEqual(request.inputs.run_id, job.bridge_run_id)
            self.assertEqual(saved_question["native_answer_run_id"], request.inputs.run_id)
            self.assertEqual(active["run_id"], request.inputs.run_id)
            self.assertEqual(active["state"], "active")
            self.assertIsNone(c.app.sessions.read_session_id(c.ctx.session_path))
            backend_entries.append({"token_consumed_for_this_run": True, "generic_sid_absent": True})
            return original_run(request)
        c.backend_run.side_effect = continued_run
        c.app.process_reply_job(job, c.ctx)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(len(backend_entries), 1)
        self.assertEqual(c.app.sessions.capture_session(c.ctx)[1], SID)
        self.assertFalse(session_is_blocked(c.ctx, "codex", SID))
        self.assertEqual(c.backend_run.call_count, 2)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertEqual(find_job_record(c.ctx.state_dir, job.message_id)["execution_status"], "done")
        with self.assertRaises(ValueError):
            self.submit(c, "third-answer")
        self.assertEqual(c.backend_run.call_count, 2)
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.evidence(c, duplicate_answer_rejected_before_and_after_execution=True,
            transfers=transfers, backend_entries=backend_entries, generic_sid_released_only_after_root=True)

    def failed_native_question(self, status="failed", *, cancel_on_close=False):
        c = self.prepare()
        real_results = []
        run = c.backend_run._mock_wraps
        def observed_run(*args, **kwargs):
            result = run(*args, **kwargs)
            real_results.append(result)
            return result
        c.backend_run.side_effect = observed_run
        record_session = self.stack.enter_context(mock.patch.object(c.app.sessions, "record_session_from_worker",
            wraps=c.app.sessions.record_session_from_worker))
        original = appserver.AppServer.side_effect
        def factory(*args, **kwargs):
            rpc = original(*args, **kwargs)
            first = rpc.events.get_nowait()
            rpc.events = queue.Queue()
            rpc.events.put(first)
            rpc.events.put(finished(turn([agent("not-authorized", ANSWER_TEXT)], status=status)))
            rpc.events.put({"method": "bridge/closed"})
            if cancel_on_close:
                close = rpc.close
                def cancelled_close():
                    result = close()
                    c.job.cancel_event.set()
                    return result
                rpc.close = cancelled_close
            return rpc
        self.stack.enter_context(codex_server(factory=factory))
        c.app.process_reply_job(c.job, c.ctx)
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        paths = list((c.ctx.state_dir / "questions").glob("*.json"))
        self.assertEqual(len(paths), 1)
        c.question_path = paths[0]
        self.assertIsNone(c.rpc_instances[0].paused_question_proof)
        self.assertIn(record["execution_status"], {"failed", "cancelled"})
        self.assertIsNot(record.get("native_resume_allowed"), True)
        self.assertFalse(record.get("verified_reply_authorized", False))
        self.assertEqual(len(real_results), 1)
        self.assertIsNone(real_results[0].public_reply)
        self.assertEqual(real_results[0].outcome.reply_fingerprint, "")
        if status != "future":
            self.assertTrue(real_results[0].outcome.delivery_authorized)
        elif real_results[0].outcome.status == "unknown":
            self.assertFalse(real_results[0].outcome.delivery_authorized)
        with self.assertRaises(ValueError):
            load_authorized_reply(c.ctx, record, "codex", c.job.chat_id)
        self.assertNotIn(ANSWER_TEXT, record["final_text"])
        self.assertTrue(any(e[0] == "body_text" and e[1] and e[2] for e in c.tx.events))
        outbound = json.dumps(c.tx.events, ensure_ascii=False)
        self.assertNotIn(ANSWER_TEXT, outbound)
        self.assertNotIn(QUESTION, outbound)
        self.assertTrue(session_is_blocked(c.ctx, "codex", SID))
        self.assertTrue(c.finalize_session.call_count)
        finalize_permissions = [call.kwargs.get("allow_resume", call.args[5] if len(call.args) > 5 else None)
                                for call in c.finalize_session.call_args_list]
        self.assertTrue(all(value is False for value in finalize_permissions))
        self.assertEqual(record_session.call_count, 0)
        sid_writes = [call.kwargs.get("new_session_id", call.args[4] if len(call.args) > 4 else None)
                      for call in c.cas.call_args_list]
        self.assertNotIn(SID, sid_writes)
        self.assertIsNone(c.app.sessions.capture_session(c.ctx)[1])
        self.assert_blocked(c)
        before = len(c.tx.events)
        response = c.app.controls.retry_topic(c.ctx, c.job.chat_id, "edge-failed-retry", job_id=c.job.job_id)
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertFalse(any(e[0] == "question_message" and e[2] for e in c.tx.events[before:]))
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        self.assertNotIn(ANSWER_TEXT, json.dumps(c.tx.events, ensure_ascii=False))
        self.assertTrue(session_is_blocked(c.ctx, "codex", SID))
        self.assertIsNone(c.app.sessions.capture_session(c.ctx)[1])
        finalize_permissions = [call.kwargs.get("allow_resume", call.args[5] if len(call.args) > 5 else None)
                                for call in c.finalize_session.call_args_list]
        sid_writes = [call.kwargs.get("new_session_id", call.args[4] if len(call.args) > 4 else None)
                      for call in c.cas.call_args_list]
        self.assertTrue(all(value is False for value in finalize_permissions))
        self.assertEqual(record_session.call_count, 0)
        self.assertNotIn(SID, sid_writes)
        self.evidence(c, protocol_terminal_status=status, user_cancelled=cancel_on_close,
            actual_outcome_status=real_results[0].outcome.status,
            retry_response=response, native_exception_rejected=True, answer_blocked=True,
            actual_outcome_delivery_authorized=real_results[0].outcome.delivery_authorized,
            public_reply_absent=real_results[0].public_reply is None,
            reply_fingerprint=real_results[0].outcome.reply_fingerprint,
            actual_shared_load_rejected=True, unauthorized_text_not_sent=True,
            finalize_allow_resume=finalize_permissions, record_session_calls=record_session.call_count,
            cas_new_session_ids=sid_writes)

    def test_failed_terminal_with_question_fields_cannot_use_native_exception(self):
        self.failed_native_question()

    def test_unknown_protocol_terminal_with_question_fields_cannot_use_native_exception(self):
        self.failed_native_question("future")

    def test_user_cancelled_question_cannot_use_native_exception(self):
        self.failed_native_question("interrupted", cancel_on_close=True)


def question(rid=7, **scope):
    return {"id": rid, "method": "item/tool/requestUserInput", "params": {
        "threadId": SID, "turnId": TURN,
        "questions": [{"id": "q", "question": "Continue?"}], **scope}}


class CodexPausedQuestionProofTests(CodexRunCase):
    def run_case(self, *, rows=None, after_pause=None, after_close=None, control=None, **rpc_kwargs):
        rpc = FakeRpc(rows if rows is not None else [question(),
            finished(turn([], status="interrupted"))], **rpc_kwargs)
        close = rpc.close
        def close_checked():
            self.assertIsNone(rpc.paused_question_proof)
            value = close()
            if after_close:
                override = after_close(rpc)
                if override is not None:
                    value = override
            return value
        rpc.close = close_checked
        def event(kind, data, owner):
            if kind == "question":
                self.assertIsNone(owner.paused_question_proof)
                owner.wait_for_answer("Continue?")
                self.assertIsNone(owner.paused_question_proof)
                if after_pause:
                    after_pause(owner)
        result = self.run_rpc(rpc, event_hook=event, control_hook=control)
        self.assertIsNone(result.public_reply)
        self.assertTrue(all(value is None for value in self.saved))
        return rpc, result

    def assert_rejected(self, rpc, result):
        self.assertIsNone(rpc.paused_question_proof)
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.outcome)
        self.assertFalse(result.outcome.resume_allowed)
        self.assertEqual(result.outcome.reply_fingerprint, "")
        self.assertNotEqual(result.outcome.status, "completed")

    def test_pause_proof_is_bound_and_never_authorizes_root(self):
        rpc, result = self.run_case()
        self.assertFalse(result.is_error)
        self.assertIsNone(result.outcome)
        self.assertEqual(rpc.paused_question_proof, {
            "version": 1, "backend": "codex", "run_id": RUN, "generation": 2,
            "session_id": SID, "turn_id": TURN, "request_ids": [7],
            "terminal_status": "interrupted", "pause_confirmed": True,
            "input_closed": True, "cleanup_confirmed": True})
        self.assertFalse(rpc.accepting_input)
        self.assertEqual(rpc.turn_id, "")

    def test_resolved_request_retains_original_string_id_after_cleanup(self):
        rid = "native-request-7"
        rpc, result = self.run_case(rows=[question(rid),
            {"method": "serverRequest/resolved", "params": {"requestId": rid}},
            finished(turn([], status="interrupted"))])
        self.assertFalse(result.is_error)
        self.assertEqual(rpc.user_requests, set())
        self.assertEqual(rpc.paused_question_proof["request_ids"], [rid])

    def test_failed_turn_has_no_pause_exception(self):
        self.assert_rejected(*self.run_case(rows=[question(), finished(turn([], status="failed"))]))

    def test_unknown_terminal_has_no_pause_exception(self):
        self.assert_rejected(*self.run_case(rows=[question(), finished(turn([], status="future"))]))

    def test_interrupted_error_is_not_a_confirmed_pause(self):
        self.assert_rejected(*self.run_case(rows=[question(), finished(
            turn([], status="interrupted", error={"message": SECRET}))]))

    def test_wrong_terminal_turn_does_not_confirm_pause(self):
        self.assert_rejected(*self.run_case(rows=[question(), finished(
            turn([], id="another-turn", status="interrupted"))]))

    def test_foreign_question_cannot_support_pause(self):
        self.assert_rejected(*self.run_case(rows=[question(threadId=OTHER),
            finished(turn([], status="interrupted"))]))

    def test_plain_pause_marker_without_wait_for_answer_is_rejected(self):
        def control(rpc):
            if rpc is not None:
                rpc.pause_for_question = "Continue?"
        self.assert_rejected(*self.run_case(rows=[finished(turn([], status="interrupted"))], control=control))

    def test_generation_change_after_pause_revokes_proof(self):
        self.assert_rejected(*self.run_case(after_pause=lambda rpc: rpc.reply_state.invalidate()))

    def test_run_change_after_pause_revokes_proof(self):
        self.assert_rejected(*self.run_case(after_close=lambda rpc: setattr(rpc.reply_state, "run_id", OTHER)))

    def test_cleanup_exception_has_no_proof(self):
        self.assert_rejected(*self.run_case(close_error=True))

    def test_cleanup_false_has_no_proof(self):
        self.assert_rejected(*self.run_case(after_close=lambda rpc: False))

    def test_reader_cleanup_unconfirmed_has_no_proof(self):
        self.assert_rejected(*self.run_case(after_close=lambda rpc: setattr(rpc, "cleanup_confirmed", False)))

    def test_process_failure_has_no_proof(self):
        self.assert_rejected(*self.run_case(exit_code=2))

    def test_late_control_failure_has_no_proof(self):
        self.assert_rejected(*self.run_case(after_close=lambda rpc: setattr(rpc, "control_error", True)))

    def test_cancel_during_cleanup_has_no_proof(self):
        self.assert_rejected(*self.run_case(after_close=lambda rpc: self.cancel.set()))

    def test_timeout_during_cleanup_has_no_proof(self):
        clock = [0.0]
        def late(rpc):
            clock[0] = 20.0
        with mock.patch("covey.backends.codex.appserver.time.monotonic", side_effect=lambda: clock[0]):
            self.assert_rejected(*self.run_case(after_close=late))

    def test_cleanup_without_root_does_not_claim_saved_text(self):
        rpc, result = self.run_case(close_error=True)
        self.assert_rejected(rpc, result)
        self.assertEqual(result.text, failure_text("cleanup_unconfirmed"))
        self.assertNotIn("已保留", result.text)
        self.assertEqual(self.saved, [None])

    def test_cleanup_with_root_keeps_candidate_but_uses_neutral_notice(self):
        rpc = FakeRpc([finished()], close_error=True)
        result = self.run_rpc(rpc)
        self.assertEqual(result.text, failure_text("cleanup_unconfirmed"))
        self.assertNotIn("已保留", result.text)
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.public_reply)
        self.assertIs(result.public_reply, self.saved[-1])
        self.assertEqual(result.outcome.reply_fingerprint, result.public_reply.fingerprint)
        self.assertFalse(result.outcome.cleanup_confirmed)
        self.assertFalse(result.outcome.delivery_authorized)
        self.assertFalse(result.outcome.resume_allowed)

    def test_interrupt_write_failure_has_no_proof(self):
        original = FakeRpc.request
        def request(rpc, method, params, timeout=30):
            if method == "turn/interrupt":
                raise RpcError("failed interrupt")
            return original(rpc, method, params, timeout)
        with mock.patch.object(FakeRpc, "request", new=request):
            self.assert_rejected(*self.run_case())

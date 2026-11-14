"""A Codex task that pauses on a native blocking question, then continues on the answer.

Built on ``CodexChainCase``: the shared worker, question store and session
guard are real; the App Server RPC and outbound transport are simulated.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from unittest import mock

from support.codex_chain import CodexChainCase
from support.fake_backends import SID, TURN, FakeRpc, agent, finished, turn, codex_server
from support.fake_lark import MemoryTransport, install_card_transport
from covey.core.jobs import find_job_record
from covey.core.saved_results import load_authorized_reply
from covey.core.session_guard import session_is_blocked
from covey.core.topics import bind_topic, context_for_topic
from covey.core.workspaces import load_config

QUESTION = "Approve the next synthetic step?"
NEXT_TURN = "turn-after-native-answer"
ANSWER_TEXT = "Completed after the explicit native answer."


class CodexNativeQuestionCase(CodexChainCase):
    def prepare(self, *, body_ok=True, form_ok=True):
        import covey.core.saved_results as saved
        original_observe = saved.observe_verified_reply
        c = self.prepare_case(send_ok=body_ok)
        c.ctx = context_for_topic(dataclasses.replace(c.ctx, workspace_name=c.root.name),
            {"chat_id": c.job.chat_id, "message_id": "om-native-root"}, c.app.store.sessions_dir)
        load_config(c.ctx.state_dir, c.ctx.default_workdir)
        bind_topic(c.ctx, c.root.name, c.root)
        c.job.topic_id = c.ctx.topic_id
        c.job.reply_to_message_id = c.ctx.reply_to_message_id
        c.app.cards.streaming = False
        c.tx = MemoryTransport(text_ok=body_ok, file_ok=body_ok, question_ok=form_ok)
        c.app.messenger.send_message = c.tx.send_text
        c.app.artifacts.send_result_file = c.tx.send_file
        c.app.cards.stream = dataclasses.replace(c.app.cards.stream, send_text_message=c.tx.send_text)
        install_card_transport(self.stack, c.tx)
        c.saved_callbacks = []
        def observe(ctx, job, backend, reply):
            result = original_observe(ctx, job, backend, reply)
            c.saved_callbacks.append({"job_id": job.job_id, "none": reply is None,
                "generation": getattr(reply, "generation", None)})
            return result
        c.observe = self.stack.enter_context(mock.patch("covey.app.runs.observe_verified_reply", side_effect=observe))
        def rpc_factory(argv, cwd, env):
            if not c.rpc_instances:
                rows = [{"id": 7, "method": "item/tool/requestUserInput", "params": {
                    "threadId": SID, "turnId": TURN, "isBlocking": True,
                    "questions": [{"id": "answer", "header": "Decision", "question": QUESTION}]}},
                    finished(turn([], status="interrupted"))]
                turn_id = TURN
            else:
                rows = [finished(turn([agent("answer-result", ANSWER_TEXT)], id=NEXT_TURN))]
                turn_id = NEXT_TURN
            rpc = FakeRpc(rows)
            original = rpc.request
            def request(method, params, timeout=30):
                value = original(method, params, timeout)
                if method == "turn/start":
                    return {"turn": {"id": turn_id}}
                return value
            rpc.request = request
            c.rpc_instances.append(rpc)
            return rpc
        self.stack.enter_context(codex_server(factory=rpc_factory))
        return c

    def snapshot(self, c):
        record = find_job_record(c.ctx.state_dir, c.job.message_id) or {}
        paths = list((c.ctx.state_dir / "questions").glob("*.json"))
        questions = [json.loads(path.read_text()) for path in paths]
        keys = ("status", "execution_status", "delivery_ok", "question_delivery_ok",
                "checkpoint_question_deferred", "verified_reply_state", "verified_reply_generation",
                "verified_reply_authorized", "native_resume_allowed", "result_retry_policy", "final_text")
        return {"job": {key: record.get(key) for key in keys},
            "questions": [{key: q.get(key) for key in ("status", "blocking", "body_delivery_required",
                "display_pending", "body_delivery_confirmed", "backend_session_id")} for q in questions],
            "saved_callbacks": list(c.saved_callbacks), "backend_calls": c.backend_run.call_count,
            "rpc_instances": len(c.rpc_instances), "rpc_methods": [[name for name, _ in rpc.requests] for rpc in c.rpc_instances],
            "session_pointer": c.app.sessions.read_session_id(c.ctx.session_path),
            "sid_blocked": session_is_blocked(c.ctx, "codex", SID),
            "outbound_kinds_and_success": [{"kind": e[0], "ok": e[2]} for e in c.tx.events]}

    def evidence(self, c, **extra):
        data = {**self.snapshot(c), **extra, "shared_products_unchanged": True,
            "cli_rpc_and_outbound_are_simulated": True, "prebuilt_run_result": False}
        config = os.environ.get("MASO_TEST_SANDBOX_CONFIG")
        if config:
            result_dir = Path(json.loads(Path(config).read_text())["results"])
            (result_dir / (self._testMethodName + "-facts.json")).write_text(json.dumps(data, indent=2) + "\n")

    def start_question(self, c):
        c.app.process_reply_job(c.job, c.ctx)
        self.assertEqual(c.backend_run.call_count, 1)
        self.assertEqual(len(c.rpc_instances), 1)
        self.assertIn("turn/interrupt", [name for name, _ in c.rpc_instances[0].requests])
        self.assertTrue(c.rpc_instances[0].closed)
        record = find_job_record(c.ctx.state_dir, c.job.message_id)
        self.assertEqual(record["execution_status"], "waiting_user")
        self.assertEqual(record["final_text"], QUESTION)
        self.assertFalse(record.get("verified_reply_authorized", False))
        self.assertTrue(c.saved_callbacks and c.saved_callbacks[0]["none"])
        self.assertTrue(all(row["none"] for row in c.saved_callbacks))
        self.assertIsNone(c.app.sessions.capture_session(c.ctx)[1])
        self.assertTrue(session_is_blocked(c.ctx, "codex", SID))
        self.assertFalse(record["native_resume_allowed"])
        self.assertTrue(record["native_question_answer_allowed"])
        paths = list((c.ctx.state_dir / "questions").glob("*.json"))
        self.assertEqual(len(paths), 1)
        c.question_path = paths[0]
        return record

    def answer_and_execute(self, c):
        question = json.loads(c.question_path.read_text())
        c.app.questions.submit_answer(c.ctx, c.job.chat_id, question["token"], {"answer_0": "Approved"}, "native-answer-event")
        self.assertEqual(c.app.runtime.jobs.queue.qsize(), 1)
        followup = c.app.runtime.jobs.queue.get_nowait()
        self.assertNotEqual(followup.message_id, c.job.message_id)
        self.assertEqual(json.loads(c.question_path.read_text())["status"], "answered")
        c.app.process_reply_job(followup, c.ctx)
        record = find_job_record(c.ctx.state_dir, followup.message_id)
        self.assertEqual(record["execution_status"], "done")
        self.assertEqual(record["final_text"], ANSWER_TEXT)
        self.assertTrue(record["verified_reply_authorized"])
        self.assertEqual(c.backend_run.call_count, 2)
        self.assertEqual(c.backend_run.call_args.args[0].session_id, SID)
        self.assertEqual(len(c.rpc_instances), 2)
        self.assertIn("thread/resume", [name for name, _ in c.rpc_instances[1].requests])
        self.assertEqual(followup.native_session_claims, {SID})
        self.assertEqual(c.app.sessions.capture_session(c.ctx)[1], SID)
        self.assertFalse(session_is_blocked(c.ctx, "codex", SID))
        self.assertFalse(c.app.questions.pending_questions(c.ctx, c.job.chat_id))
        self.assertTrue(c.app.runtime.jobs.queue.empty())
        reply, outcome = load_authorized_reply(c.ctx, record, "codex", c.job.chat_id)
        self.assertEqual(reply.boundary[:3], ("app-server", SID, NEXT_TURN))
        self.assertTrue(outcome.resume_allowed)
        return followup

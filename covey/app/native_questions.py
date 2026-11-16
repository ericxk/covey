"""A backend's question as it arrives: saved once as a record with how it waits, then shown there
- a live native form on its own card, a paused turn, or the task card's question element - and the
check that a live form still belongs to the running task (``live_control``). A checkpoint's question
is registered here too (``defer_question``); checkpoint_questions.py shows it after the result."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import upsert_job_record
from covey.core.locks import resource_lock
from covey.core.proxy import NO_QUESTION
from covey.core.roster import LIVE_QUESTIONS
from covey.cards.question_cards import question_element
from covey.app import butler_relay
from covey.app.triage import LEAD
from covey.app.question_display import question_card_lock
from covey.core.state import write_private_text
from covey.core.store import directory_store, transition_question
from covey.core.topics import read_topic


def turn_output(job):
    """What the turn wrote before it asked, in order: the text before each tool call (its 进展说明 in the trace,
    core/trace.py note; the backends move it there when a tool starts, core/steps.py AnswerNotes) and the answer
    so far. The caller holds the job's trace lock (app/interaction.py structured_harness_event)."""
    trace = getattr(job, "tool_trace", None)
    parts = [str(step.get("detail") or "") for step in (trace.steps.values() if trace is not None else ())
             if step.get("status") == "note" and step.get("title") == "进展说明"]
    parts.append(str(getattr(job.progress, "last_content", "") or "") if job.progress else "")
    return "\n\n".join(part.strip() for part in parts if part.strip())


class NativeQuestions:
    """A Harness's native questions. Questions methods it uses are called through ``questions`` (the
    facade), so an override on the Questions instance still intercepts them."""

    def __init__(self, harness, *, runtime, cards, questions) -> None:
        self.harness = harness
        self.runtime = runtime
        self.cards = cards
        self.questions = questions

    def dispatch(self, ctx, job, kind, data, rpc, *, defer_question=False):
        """A backend question, or the end of the turn or task that its pending questions depend on."""
        if kind == "question":
            if not defer_question and rpc is not None and self.refused_in_proxy_topic(ctx, job, data, rpc):
                return
            registered = self.register(ctx, job, data, rpc, defer_question=defer_question)
            if registered is not None and not defer_question:
                self.show(ctx, job, rpc, *registered)
                self.mirror(ctx, job, registered[0])
            return
        if kind == "turn_finished":
            for record in self.questions.pending_questions(ctx, job.chat_id):
                if record["job_id"] == job.job_id and not record["blocking"]:
                    record = transition_question(ctx.state_dir / "questions" / (record["token"] + ".json"), {"pending"}, task_finished=True)
                    if record:
                        self.cards.set_task_element(ctx, job, question_element(record))
            return
        if kind == "cancel_questions":
            for record in self.questions.pending_questions(ctx, job.chat_id):
                if record["job_id"] == job.job_id:
                    record = transition_question(ctx.state_dir / "questions" / (record["token"] + ".json"), {"pending"}, status="cancelled")
                    if record:
                        self.questions.paint_question_state(ctx, record)
            return

    def mirror(self, ctx, job, path):
        """A 话题区's pending question at the private chat's top level too, with what the turn has written so far
        above it (app/question_display.py mirror); never fails the question."""
        try:
            record = json.loads(Path(path).read_text())
            if self.questions.display.mirror(ctx, record, turn_output(job), job=job):
                return
            if record.get("status") == "pending" and not json.loads(Path(path).read_text()).get("mirror_state"):
                # Not copied (the card not shown, no butler card to quote): the question as a line, once.
                butler_relay.question_asked(ctx, job, record, messenger=self.cards.messenger, voice=self.harness.voice,
                                          log=self.runtime.log)
        except Exception as exc:  # noqa: BLE001 - the question stands in the topic
            self.runtime.log(f"question top-level copy of job #{job.job_id} failed: {type(exc).__name__}")

    def refused_in_proxy_topic(self, ctx, job, data, rpc):
        """A 代答 topic (app/proxy.py) never asks the owner: its native question is answered at once with
        NO_QUESTION and the turn goes on to its reply. Not a Harness that cannot refuse one (``refuses_questions``)."""
        if not self.harness.spec.refuses_questions:
            return False
        try:
            if not read_topic(ctx).get("proxy"):
                return False
        except (OSError, ValueError, AttributeError):
            return False
        rpc.reject_question(data["request_id"], NO_QUESTION)
        self.runtime.log(f"proxy job=#{job.job_id} native question refused")
        return True

    def register(self, ctx, job, data, rpc, *, defer_question):
        """Save a backend's question as a record (once: a redelivered event finds it saved), with
        how it waits: a live native form, a paused turn, or a form shown after the result.
        None when it cannot be asked here."""
        if not data.get("questions") or any(q.get("isSecret") for q in data["questions"]):
            if rpc is not None:
                rpc.reject_question(data["request_id"], "Secret or empty questions cannot be collected in Feishu")
            return None
        token = hashlib.sha256(f"{job.message_id}-{data['request_id']}".encode()).hexdigest()[:16]
        record = {
            "token": token,
            "topic_id": job.topic_id,
            "chat_id": job.chat_id,
            "job_id": job.job_id,
            "job_key": job.message_id,
            "harness_id": ctx.options.get("harness_id", ""),
            "binding_revision": ctx.options.get("binding_revision", 1),
            "card_id": job.progress.card_id if job.progress else "",
            "questions": data["questions"],
            "blocking": data.get("isBlocking", True)
            or read_checkpoint(Path(job.checkpoint_path)).get("requires_user") is True,
            "request_id": data["request_id"],
            "backend_session_id": getattr(rpc, "session_id", "") or getattr(rpc, "thread_id", ""),
            "status": "pending",
            "created_at": time.time(),
        }
        pause_question = (not defer_question and record["blocking"] and rpc is not None
            and not getattr(rpc, "native_blocking_questions", False)
            and bool(job.bridge_run_id))
        live_question = (not defer_question and record["blocking"] and rpc is not None
            and getattr(rpc, "native_blocking_questions", False) is True and self.harness.spec.name in LIVE_QUESTIONS
            and bool(job.bridge_run_id))
        if live_question:
            record.update(native_live_question=True, display_pending=True,
                          live_question_proof=data.get("live_question_proof"))
        if defer_question or pause_question:
            record.update(body_delivery_required=True, body_delivery_confirmed=False, display_pending=True)
        if pause_question:
            record.update(native_paused_question=True, result_run_id=job.bridge_run_id,
                result_generation=getattr(getattr(rpc, "reply_state", None), "generation", None),
                backend_turn_id=getattr(rpc, "turn_id", ""))
            job.checkpoint_question_deferred = True
            job.native_question_deferred = True
            # Before the pause withdraws the candidate: a record so left is redelivered, never run again
            # (saved_results.reruns_after_withdrawal), even if its runner dies first.
            upsert_job_record(ctx.state_dir, job.message_id, native_question_deferred=True)
        if not defer_question and record["blocking"] and rpc is not None:
            job.native_question_active = True
        path = ctx.state_dir / "questions" / (token + ".json")
        with resource_lock("question", path):
            if path.exists():
                record = json.loads(path.read_text())
            else:
                record.update(schema_version=1, revision=1)
                write_private_text(path, json.dumps(record, ensure_ascii=False))
        return path, record, pause_question, live_question

    def show(self, ctx, job, rpc, path, record, pause_question, live_question):
        """Show a saved, pending question where it waits for its answer."""
        if record["status"] != "pending":
            self.questions.paint_question_state(ctx, record)
            return
        if record["blocking"]:
            cp = read_checkpoint(Path(job.checkpoint_path))
            cp.update(requires_user=True, question="\n".join(q["question"] for q in record["questions"]))
            write_private_text(Path(job.checkpoint_path), json.dumps(cp, ensure_ascii=False))
        if live_question and self.questions.triage.route(ctx, job, record) == LEAD:
            self.hold(ctx, job, record)
            return
        if live_question:
            self.cards.close_streaming_reply(ctx, job.progress) if job.progress else None
            with question_card_lock(path):
                with resource_lock("question", path):
                    record = json.loads(path.read_text())
                    with rpc.control_lock:
                        self.live_control(ctx, job, record)
                visible = {**record, "display_pending": False}
                # Feishu can clear unsent form values when any sibling
                # task-card element refreshes. Keep live forms on their
                # own stable card; activity and process pages stay live.
                shown = bool(self.questions.publish_question_card(ctx, record, visible=visible))
                record = self.questions.display.commit_display(ctx, path, shown and not job.cancel_event.is_set())
                upsert_job_record(ctx.state_dir, job.message_id, question_delivery_ok=not record["display_pending"])
            return
        if pause_question:
            rpc.wait_for_answer("\n".join(q["question"] for q in record["questions"]))
            return
        self.cards.close_streaming_reply(ctx, job.progress) if job.progress else None
        if not self.cards.set_task_element(ctx, job, question_element(record)):
            self.questions.publish_question_card(ctx, record)
        if record["blocking"] and rpc is not None and not getattr(rpc, "native_blocking_questions", False):
            rpc.wait_for_answer("\n".join(q["question"] for q in record["questions"]))

    def hold(self, ctx, job, record):
        """A live question the project's lead has (app/triage.py): no card; the task's record says its question is delivered
        (nothing offers a retry for it) and its card says where it is. The turn goes on waiting for the answer."""
        upsert_job_record(ctx.state_dir, job.message_id, question_delivery_ok=True)
        self.questions.triage.note(ctx, job, record)

    def live_control(self, ctx, job, record):
        proof = record.get("live_question_proof")
        keys = {"version", "backend", "run_id", "generation", "session_id", "request_id",
                "request_session_id", "message_id", "call_id", "questions_sha256"}
        control = job.backend_control if job is not None else None
        if (job is None or self.runtime.jobs.by_id.get(job.job_id) is not job or job.cancel_event.is_set()
                or job.topic_id != ctx.topic_id or job.chat_id != record.get("chat_id")
                or not isinstance(proof, dict) or set(proof) != keys
                or type(proof["version"]) is not int or proof["version"] != 1
                or self.harness.spec.name not in LIVE_QUESTIONS or proof["backend"] != self.harness.spec.name
                or proof["run_id"] != job.bridge_run_id or type(proof["generation"]) is not int
                or proof["request_id"] != record.get("request_id")
                or proof["session_id"] != record.get("backend_session_id")
                or hashlib.sha256(json.dumps(record["questions"], ensure_ascii=False, sort_keys=True).encode()).hexdigest() != proof["questions_sha256"]
                or control is None or getattr(control, "native_blocking_questions", False) is not True
                or not callable(getattr(control, "verify_question_request", None))
                or not control.verify_question_request(proof)):
            raise ValueError("原生问题已失效或失去当前执行上下文")
        return control

    def retry_live(self, ctx, chat_id, job):
        rows = directory_store(ctx.state_dir / "questions").query(job_id=job.job_id, topic_id=ctx.topic_id,
                                                                  chat_id=chat_id, status="pending")
        live = [(path, row) for path, row in rows if row.get("native_live_question") is True]
        if not live:
            return None
        try:
            for path, _ in live:
                with question_card_lock(path):
                    with resource_lock("question", path):
                        record = json.loads(path.read_text())
                        if record.get("status") != "pending":
                            raise ValueError("原生问题已经解决")
                        control = job.backend_control
                        with control.control_lock:
                            self.live_control(ctx, job, record)
                    if not record.get("display_pending"):
                        continue
                    visible = {**record, "display_pending": False}
                    shown = bool(self.questions.publish_question_card(ctx, record, visible=visible))
                    if not shown or job.cancel_event.is_set():
                        upsert_job_record(ctx.state_dir, job.message_id, question_delivery_ok=False)
                        return "原生问题控件仍未送达；没有发送答案或重新执行任务。"
                    self.questions.display.commit_display(ctx, path, True)
            upsert_job_record(ctx.state_dir, job.message_id, question_delivery_ok=True)
            return "原生问题控件已重新投递；仍由原任务等待回答，没有重新执行。"
        except Exception:
            return "原生问题已失效或投递状态未确认；没有发送答案或重新执行任务。"

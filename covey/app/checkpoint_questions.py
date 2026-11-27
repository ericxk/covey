"""A question that comes with a checkpoint result: deferred until the complete result is delivered,
published after it, an older duplicate retired once a later identical form has an accepted answer,
and the jobs whose result or form still needs redelivery."""

from __future__ import annotations

import dataclasses
import json

from covey.core import ui_samples
from covey.core.checkpoint import normalize_questions
from covey.core.jobs import upsert_job_record
from covey.core.locks import resource_lock
from covey.cards.elements import md
from covey.cards.question_cards import question_element
from covey.app import question_dedup
from covey.app.question_display import question_card_lock
from covey.app.triage import LEAD
from covey.core.store import directory_store, transition_question, update_question


def _checkpoint_source(record, jobs):
    """The job a checkpoint question came from, when the record and that job still agree."""
    job = jobs.get(record.get("job_key"), {})
    if (not record.get("body_delivery_required") or record.get("native_live_question")
            or record.get("request_id") != "checkpoint-" + job.get("key", "")
            or any(record.get(k) != job.get(k) for k in ("job_id", "topic_id", "chat_id"))
            or not job.get("session_id") or not job.get("workdir")):
        return None
    return job


def _accepted_answer(record, jobs):
    """(source job, continuation job) of a checkpoint question whose full answer was accepted
    and carried on by a continuation in the same scope; None otherwise."""
    job = _checkpoint_source(record, jobs)
    continuation = jobs.get("answer-" + record["token"], {})
    if (job is None or record.get("status") != "answered" or record.get("response_kind") == "feedback"
            or record.get("delivery_state") in {None, "awaiting_ack"}
            or continuation.get("job_id") != record.get("continuation_job_id")
            or continuation.get("message_id") != "answer-" + record["token"]
            or any(continuation.get(k) != job.get(k) for k in ("topic_id", "chat_id", "backend", "workdir"))
            or continuation.get("status") not in {"queued", "running", "done", "waiting_user", "continued"}
            or not record.get("questions")
            or any(not record.get("answers", {}).get(q["id"], {}).get("answers") for q in record["questions"])):
        return None
    return job, continuation


def _open_job_keys(rows):
    """The job keys of the open questions (pending, superseded) of ``rows``: the only jobs _older_duplicate looks at. A
    key it cannot look up by (unhashable) is left out: its own lookup raises on it as before."""
    keys = set()
    for _, record in rows:
        if record.get("status") in {"pending", "superseded"}:
            try:
                keys.add(record.get("job_key"))
            except TypeError:
                pass
    return keys


def _older_duplicate(old, jobs, backend):
    """The job of the open checkpoint question ``old`` when a later identical form's accepted answer may retire it:
    the job still waits for the answer (and is of ``backend``, when given). None otherwise."""
    previous = _checkpoint_source(old, jobs)
    if (previous is None or old.get("status") not in {"pending", "superseded"}
            or previous.get("status") != "waiting_user"
            or (backend is not None and previous.get("backend") != backend)):
        return None
    return previous


def _answers_same_question(old, previous, newer, newer_job):
    """Whether the later accepted answer (``newer``, of ``newer_job``) answers the same form as
    the older question ``old`` of job ``previous``, in the same session and binding."""
    scope = ("topic_id", "chat_id", "backend", "harness_id", "session_id", "workdir", "binding_revision")
    return not (newer_job["job_id"] <= previous["job_id"]
                or any(newer_job.get(k) != previous.get(k) for k in scope)
                or (old.get("status") == "superseded" and old.get("superseded_by") != newer["token"])
                or newer["questions"] != old.get("questions"))


class CheckpointQuestions:
    """A Harness's checkpoint questions. Questions methods it uses are called through ``questions``
    (the facade), so an override on the Questions instance still intercepts them."""

    def __init__(self, *, runtime, cards, questions) -> None:
        self.runtime = runtime
        self.cards = cards
        self.questions = questions

    def reconcile(self, ctx, chat=None, *, backend=None):
        """Retire an older duplicate only after a later identical form has an accepted answer.
        ``chat`` limits it to ctx's topic; ``backend`` to the questions of that Harness's jobs."""
        filters = {"topic_id": ctx.topic_id, "chat_id": chat} if chat is not None else {}
        questions = directory_store(ctx.state_dir / "questions")
        # It runs for every message: without an open form and an answered one there is nothing to
        # retire, and the job records are not read. At the Bridge's start it runs once per Harness: only
        # the open questions and their jobs are copied out of the stores unless one of them may be retired
        # (a copy of every record, 1500 questions and 5900 jobs, per Harness made most of a start, T648).
        opened = questions.query(status=frozenset({"pending", "superseded"}), **filters)
        if not (opened and questions.count(status="answered", **filters)):
            return
        store = directory_store(ctx.state_dir / "jobs")
        wanted = _open_job_keys(opened)
        jobs = {row["key"]: row for _, row in store.query(lambda row: row.get("key") in wanted, **filters)
                if row.get("key")}
        if not any(_older_duplicate(old, jobs, backend) for _, old in opened):
            return
        rows = questions.query(**filters)  # Then all of them, as before.
        jobs = {row["key"]: row for _, row in store.query(**filters) if row.get("key")}
        for path, old in rows:
            previous = _older_duplicate(old, jobs, backend)
            if previous is None:
                continue
            for newer_path, newer in rows:
                proof = _accepted_answer(newer, jobs)
                if proof is None:
                    continue
                newer_job, continuation = proof
                if not _answers_same_question(old, previous, newer, newer_job):
                    continue
                # Never overwrite a concurrent submission or infer an answer from model prose.
                with resource_lock("question", path):
                    current = json.loads(path.read_text())
                    fresh = json.loads(newer_path.read_text())
                    if current != old or fresh != newer:
                        continue
                    retired = current if current["status"] == "superseded" else transition_question(
                        path, {"pending"}, status="superseded", superseded_by=newer["token"],
                        continuation_job_id=continuation["job_id"])
                if retired:
                    upsert_job_record(ctx.state_dir, previous["key"], status="continued",
                        status_message="相同问题已在后续卡片回答，无需重复提交",
                        resumed_by=continuation["message_id"], superseded_question_by=newer["token"])
                    scoped = dataclasses.replace(ctx, topic_id=old["topic_id"])
                    self.questions.paint_question_state(scoped, retired)
                    if previous.get("card_id"):
                        try:
                            self.cards.update_record_panel(scoped, old["chat_id"], "question-superseded-" + old["token"],
                                {"message_id": previous.get("card_message_id", "")}, previous,
                                md(f"相同问题已在后续卡片回答 · 后续任务 #{continuation['job_id']}。无需重复提交。",
                                   element_id=self.cards.stream.element_id))
                        except Exception as exc:
                            self.runtime.log(f"Superseded question task display failed: {type(exc).__name__}")
                break

    def defer(self, ctx, job, questions):
        """Only the already-returned checkpoint path may defer a question. ``questions`` as the turn wrote them, made into
        what a card takes first (core/checkpoint.py normalize_questions: a number id, a string option, ``header`` for
        empty words…); ValueError when nothing is left to ask."""
        questions = normalize_questions(questions, "")
        # A multi-select question says "multiple" (core/checkpoint.py); Claude's own question tool's
        # "multiSelect" is taken too. Without either the card had a single select, though the question
        # said 可多选.
        questions = [{**q, "multiple": q.get("multiple") is True or q.get("multiSelect") is True} for q in questions]
        # 样张 (core/ui_samples.py): each option's file read once, now, and what its card draws kept in the record.
        questions = ui_samples.carry(questions, device=bool(getattr(ctx, "device", "")))
        job.checkpoint_question_deferred = True
        upsert_job_record(ctx.state_dir, job.message_id, checkpoint_question_deferred=True)
        with job.trace_lock:
            return self.questions.native.dispatch(ctx, job, "question",
                {"request_id": "checkpoint-" + job.message_id, "isBlocking": True, "questions": questions},
                None, defer_question=True)

    def result_delivered(self, ctx, record):
        if not record.get("body_delivery_required"):
            return True  # Legacy and native questions retain their existing contract.
        rows = directory_store(ctx.state_dir / "jobs").query(job_id=record["job_id"])
        return any(job.get("topic_id") == record["topic_id"] and job.get("chat_id") == record["chat_id"]
            and job.get("key") == record["job_key"] and job.get("delivery_ok") is True
            and job.get("execution_status") == "waiting_user" for _, job in rows)

    def deliver(self, ctx, job, use_result_card=False):
        """Publish saved forms only after their complete public result is delivered."""
        if job.checkpoint_question_error:
            return False
        ok = True

        def confirmed(record):
            nonlocal ok
            if not record.get("display_pending"):
                return None
            if not self.result_delivered(ctx, record):
                ok = False
                return None
            return {**record, "body_delivery_confirmed": True}
        for path, saved in directory_store(ctx.state_dir / "questions").query(job_id=job.job_id):
            if (not saved.get("body_delivery_required") or saved.get("topic_id") != job.topic_id
                    or saved.get("chat_id") != job.chat_id):
                continue
            with question_card_lock(path):
                record = update_question(path, confirmed, expected={"pending"})
                if record is None:
                    continue
                if self.questions.triage.route(ctx, job, record) == LEAD:
                    # The project's lead has it: no card, and not a failure (the task's question_delivery_ok stays true).
                    self.questions.triage.note(ctx, job, record)
                    continue
                visible = question_dedup.drawn({**record, "display_pending": False})  # Its grey lines and extra question.
                # A question with samples goes on a card of its own: the task card is no place for two or three cards.
                on_result_card = bool(use_result_card and not ui_samples.any_of(record.get("questions"))
                                      and self.cards.set_task_element(ctx, job, question_element(visible)))
                shown = on_result_card
                if not shown:
                    shown = bool(self.questions.publish_question_card(ctx, record, visible=visible))
                if shown:
                    self.questions.display.commit_display(ctx, path, True,
                                                          **({"card_id": job.progress.card_id} if on_result_card else {}))
                else:
                    ok = False
        return ok

    def undelivered_jobs(self, ctx, chat):
        """The jobs whose result or question form the topic must get again before it takes a message.
        Not one a later job of the topic has started after, nor one Feishu's audit refused (sending it
        again sends the same text): a reply the audit refused for an email address must not hold every later
        message of the topic while the jobs behind it run."""
        rows = [record for _, record in directory_store(ctx.state_dir / "jobs").query(topic_id=ctx.topic_id, chat_id=chat)]
        started = [int(record.get("job_id") or 0) for record in rows
                   if (record.get("timings") or {}).get("backend_started")
                   or record.get("status") in {"running", "waiting_user", "continued", "done"}]
        return [record for record in rows if record.get("checkpoint_question_deferred") is True
            and not record.get("checkpoint_question_stopped")
            and record.get("status") not in {"continued", "cancelled", "done"}
            and record.get("delivery_error") != "audit_rejected"
            and not any(job_id > int(record.get("job_id") or 0) for job_id in started)
            and (record.get("delivery_ok") is False or record.get("question_delivery_ok") is False
                 or record.get("checkpoint_question_error") or record.get("checkpoint_question_registration_pending"))]

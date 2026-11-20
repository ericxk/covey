"""Private saved replies. A candidate is never a delivery authorization."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import threading

from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.locks import shared_file_lock
from covey.core.file_lock import file_lock
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.roster import HARNESSES, PAUSED_QUESTIONS
from covey.core.state import write_private_text
from covey.core.harness import JobCancelled
from covey.core.store import transition_question, directory_store

_LOCK = threading.RLock()
RETRY_POLICY = "saved_delivery_only"
# Harnesses whose withdrawn candidate is gone for good (Claude, dsh). Codex also keeps a
# withdrawn generation for a native question it paused on: that turn is not run again (reruns_after_withdrawal).
RERUN_AFTER_WITHDRAWAL = frozenset(name for name, entry in HARNESSES.items() if not entry.paused_questions)


def reruns_after_withdrawal(backend, paused_on_question):
    """Whether a turn of ``backend`` whose candidates were all withdrawn runs again: on every Harness but for
    a turn that paused on a native question (Codex; its record says ``native_question_deferred`` from when
    the question was saved, before the pause withdrew the candidate). Any other Codex turn that wrote a
    message and went on, then failed or was interrupted, did: such a turn would get
    neither 检查并继续 nor the automatic continue after a restart."""
    return backend in RERUN_AFTER_WITHDRAWAL or not paused_on_question


def _scope(ctx, job, backend):
    if type(job.job_id) is not int or job.job_id < 1:
        raise ValueError("保存结果的任务标识无效")
    return {"backend": backend, "run_id": job.bridge_run_id,
            "job_key": job_key(job.message_id, job.job_id), "job_id": job.job_id,
            "chat_id": job.chat_id, "topic_id": job.topic_id, "workdir": str(Path(ctx.workdir).resolve())}


def _folder(state):
    folder = Path(state) / "verified-results"
    if folder.is_symlink():
        raise ValueError("保存结果目录无效")
    return folder


@contextmanager
def _locked_results(ctx, *, create=False):
    with _LOCK:
        folder = _folder(ctx.state_dir)
        if create:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(folder / ".lock", create=create):
            yield


def _path(state, key):
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("保存结果标识无效")
    path = _folder(state) / (key + ".json")
    if path.is_symlink():
        raise ValueError("保存结果路径无效")
    return path


def _index(state, key):
    # A durable no-execution marker survives an interrupted job-record update.
    name = "index-" + hashlib.sha256(str(key).encode()).hexdigest() + ".json"
    path = _folder(state) / name
    if path.is_symlink():
        raise ValueError("保存结果索引无效")
    return path


def withdrawn_unauthorized(record):
    """The turn's candidates were all withdrawn (``observe_verified_reply(None)``) before one was
    authorized: nothing was saved to send, so 检查并继续 and the automatic retry run the turn again,
    as after a turn that never had a candidate. Records already like this (Claude woken by a
    background task, then rate-limited or incomplete) read the same; an authorization once written
    stays in the record. Not a paused native question (reruns_after_withdrawal): it is kept as an
    invalidated generation too, and is redelivered, never run again."""
    return (record.get("verified_reply_state") == "invalidated" and not record.get("verified_authorization")
            and reruns_after_withdrawal(record.get("backend"), record.get("native_question_deferred"))
            and record.get("paused_question_delivery") is None)


def has_saved_reply(ctx, record):
    if withdrawn_unauthorized(record):
        return False
    return record.get("result_retry_policy") == RETRY_POLICY or _index(ctx.state_dir, record.get("key")).exists()


def _authorization(saved):
    fields = {k: saved[k] for k in ("scope", "fingerprint", "generation", "outcome", "delivery_kind")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _generation(ctx, scope):
    index = _index(ctx.state_dir, scope["job_key"])
    if not index.exists():
        return 1
    saved = json.loads(index.read_text())
    generation = saved.get("generation")
    if saved.get("run_id") != scope["run_id"] or type(generation) is not int or generation < 1:
        raise ValueError("保存回复的周期记录无效")
    return generation


def observe_verified_reply(ctx, job, backend, reply):
    """None persistently advances the generation, including before any candidate."""
    with _locked_results(ctx, create=True):
        scope = _scope(ctx, job, backend)
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        generation = _generation(ctx, scope)
        path = _path(ctx.state_dir, key)
        if reply is None:
            generation += 1
            saved = json.loads(path.read_text()) if path.exists() else {"version": 1, "scope": scope}
            saved.update(state="invalidated", generation=generation)
            saved.pop("outcome", None)
            saved.pop("authorization", None)
            write_private_text(_index(ctx.state_dir, scope["job_key"]), json.dumps(
                {"result_key": key, "generation": generation, "run_id": scope["run_id"]}))
            write_private_text(path, json.dumps(saved, ensure_ascii=False))
            upsert_job_record(ctx.state_dir, job.message_id, verified_reply_state="invalidated",
                              verified_reply_authorized=False, verified_reply_generation=generation,
                              result_retry_policy=RETRY_POLICY, verified_result_key=key, result_run_id=scope["run_id"])
            job.verified_reply, job.verified_result_key = None, key
            return
        if (not isinstance(reply, VerifiedRootReply) or reply.backend != backend or reply.run_id != job.bridge_run_id
                or reply.generation != generation):
            raise ValueError("公开回复与当前运行或周期不匹配")
        if job.verified_reply == reply and job.verified_result_key == key:
            return
        saved = {"version": 1, "scope": scope, "state": "candidate", "reply": reply.to_dict(),
                 "fingerprint": reply.fingerprint, "generation": generation}
        write_private_text(_index(ctx.state_dir, scope["job_key"]), json.dumps(
            {"result_key": key, "generation": generation, "run_id": scope["run_id"]}))
        write_private_text(path, json.dumps(saved, ensure_ascii=False))
        upsert_job_record(ctx.state_dir, job.message_id, verified_result_key=key,
                          verified_reply_fingerprint=reply.fingerprint, verified_reply_state="candidate",
                          verified_reply_authorized=False, result_run_id=reply.run_id,
                          verified_reply_generation=generation, result_retry_policy=RETRY_POLICY)
        job.verified_reply, job.verified_result_key = reply, key


def authorize_verified_reply(ctx, job, backend, reply, outcome):
    """Called only after backend and shared session-protection finalization."""
    if (not isinstance(reply, VerifiedRootReply) or not isinstance(outcome, ExecutionOutcome)
            or not outcome.delivery_authorized or outcome.reply_fingerprint != reply.fingerprint):
        return False
    with _locked_results(ctx):
        if job.verified_reply != reply:
            return False
        path = _path(ctx.state_dir, job.verified_result_key)
        saved = json.loads(path.read_text())
        if (type(saved.get("version")) is not int or saved["version"] != 1
                or saved.get("state") != "candidate" or saved.get("scope") != _scope(ctx, job, backend)
                or type(saved.get("scope", {}).get("job_id")) is not int
                or type(saved.get("generation")) is not int or saved["generation"] != reply.generation
                or _generation(ctx, saved["scope"]) != reply.generation
                or saved.get("fingerprint") != reply.fingerprint
                or VerifiedRootReply.from_dict(saved.get("reply")) != reply):
            return False
        saved.update(state="authorized", outcome=outcome.to_dict(), delivery_kind=saved_delivery_kind(reply, outcome))
        saved["authorization"] = _authorization(saved)
        write_private_text(path, json.dumps(saved, ensure_ascii=False))
        upsert_job_record(ctx.state_dir, job.message_id, verified_reply_state="authorized",
                          verified_reply_authorized=True, verified_authorization=saved["authorization"],
                          verified_outcome=outcome.to_dict(), execution_status=outcome.job_status,
                          native_resume_allowed=outcome.resume_allowed, result_retry_policy=RETRY_POLICY,
                          final_text=reply.text, verified_delivery_kind=saved["delivery_kind"], delivery_ok=False,
                          outbox_version=1, outbox_state='pending',
                          outbox_attempts=0, outbox_next_at=0)
        return True


def _load_authorized(ctx, record, backend, chat_id):
    if (record.get("result_retry_policy") != RETRY_POLICY
            or record.get("verified_reply_authorized") is not True
            or record.get("verified_reply_state") != "authorized"):
        raise ValueError("回复仅已保全，尚未满足发送条件；不会重新执行任务")
    saved = json.loads(_path(ctx.state_dir, record.get("verified_result_key")).read_text())
    expected = {"backend": backend, "run_id": record.get("result_run_id"), "job_key": record.get("key"),
                "job_id": record.get("job_id"), "chat_id": chat_id, "topic_id": ctx.topic_id,
                "workdir": str(Path(ctx.workdir).resolve())}
    if (type(saved.get("version")) is not int or saved["version"] != 1 or saved.get("state") != "authorized"
            or type(record.get("job_id")) is not int or saved.get("scope") != expected
            or type(saved.get("scope", {}).get("job_id")) is not int):
        raise ValueError("保存回复与当前话题或运行不匹配")
    reply = VerifiedRootReply.from_dict(saved.get("reply"))
    outcome = ExecutionOutcome(**saved["outcome"])
    if (not outcome.delivery_authorized or outcome.reply_fingerprint != reply.fingerprint
            or reply.backend != backend or reply.run_id != expected["run_id"]
            or type(saved.get("generation")) is not int or saved["generation"] != reply.generation
            or type(record.get("verified_reply_generation")) is not int or record["verified_reply_generation"] != reply.generation
            or _generation(ctx, expected) != reply.generation
            or reply.fingerprint != saved.get("fingerprint")
            or reply.fingerprint != record.get("verified_reply_fingerprint")
            or _authorization(saved) != saved.get("authorization")
            or saved["authorization"] != record.get("verified_authorization")
            or saved["outcome"] != record.get("verified_outcome")
            or saved.get("delivery_kind") != saved_delivery_kind(reply, outcome)
            or record.get("verified_delivery_kind") != saved["delivery_kind"]
            or not (outcome.job_status == record.get("execution_status") or
                    (outcome.job_status == 'done' and outcome.resume_allowed
                     and record.get('execution_status') == 'waiting_user'))
            or record.get("native_resume_allowed") is not outcome.resume_allowed):
        raise ValueError("保存回复未通过完整性检查")
    return reply, outcome


def _paused_question_marker(ctx, scope, proof, session_id, statuses=("pending",)):
    if type(scope.get("job_id")) is not int or scope["job_id"] < 1:
        raise ValueError("原生问答任务标识无效")
    keys = {"version", "backend", "run_id", "generation", "session_id", "turn_id", "request_ids",
            "terminal_status", "pause_confirmed", "input_closed", "cleanup_confirmed"}
    identifier = lambda value: isinstance(value, str) and 0 < len(value) <= 256 and not any(c.isspace() for c in value)
    if (not isinstance(proof, dict) or set(proof) != keys or type(proof["version"]) is not int
            or proof["version"] != 1 or proof["backend"] not in PAUSED_QUESTIONS or proof["backend"] != scope["backend"]
            or proof["run_id"] != scope["run_id"] or type(proof["generation"]) is not int
            or proof["generation"] < 1 or proof["session_id"] != session_id
            or not identifier(session_id) or not identifier(proof["turn_id"])
            or proof["terminal_status"] != "interrupted"
            or any(proof[key] is not True for key in ("pause_confirmed", "input_closed", "cleanup_confirmed"))):
        raise ValueError("原生问答缺少当前运行的暂停与清理证明")
    ids = proof["request_ids"]
    if (not isinstance(ids, list) or not 0 < len(ids) <= 128
            or any(not (identifier(value) or (type(value) is int and 0 <= value < 2**63)) for value in ids)
            or len({(type(value), value) for value in ids}) != len(ids)):
        raise ValueError("原生提问标识无效")
    key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    path = _path(ctx.state_dir, key)
    saved = json.loads(path.read_text())
    if (not isinstance(saved, dict) or type(saved.get("version")) is not int or saved["version"] != 1
            or saved.get("scope") != scope or saved.get("state") != "invalidated"
            or type(saved.get("scope", {}).get("job_id")) is not int
            or saved.get("reply") is not None or "authorization" in saved or "outcome" in saved
            or type(saved.get("generation")) is not int or saved["generation"] != proof["generation"]
            or _generation(ctx, scope) != proof["generation"]):
        raise ValueError("原生问答与当前保存代际不匹配")
    requests = []
    for _, question in directory_store(ctx.state_dir / "questions").query(job_id=scope["job_id"]):
        if question.get("topic_id") != scope["topic_id"] or question.get("chat_id") != scope["chat_id"]:
            continue
        if (question.get("native_paused_question") is not True or question.get("body_delivery_required") is not True
                or question.get("result_run_id") != scope["run_id"]
                or type(question.get("result_generation")) is not int
                or question["result_generation"] != proof["generation"]
                or question.get("backend_session_id") != session_id or question.get("backend_turn_id") != proof["turn_id"]
                or question.get("status") not in statuses):
            raise ValueError("原生问题记录与暂停证明不匹配")
        requests.append(question.get("request_id"))
    if sorted(map(lambda value: (type(value).__name__, str(value)), requests)) != sorted(
            map(lambda value: (type(value).__name__, str(value)), ids)):
        raise ValueError("原生问题请求集合不匹配")
    return path, saved


def mark_paused_question_delivery(ctx, job, backend, session_id, control, text):
    """Record a proven native pause; this never authorizes root public content."""
    if not job.verified_result_key:
        raise ValueError("原生问答缺少当前代际记录")
    proof = getattr(control, "paused_question_proof", None)
    state = getattr(control, "reply_state", None)
    process = getattr(control, "process", None)
    if (getattr(control, "accepting_input", None) is not False
            or getattr(control, "cleanup_confirmed", None) is not True
            or getattr(control, "closed", None) is not True or getattr(control, "control_error", True) is not False
            or getattr(state, "run_id", None) != job.bridge_run_id or type(getattr(state, "generation", None)) is not int
            or not isinstance(text, str) or not text.strip() or text != getattr(control, "pause_for_question", None)
            or not isinstance(proof, dict) or getattr(state, "generation", None) != proof.get("generation")
            or getattr(control, "thread_id", None) != session_id
            or process is None or type(process.poll()) is not int or process.poll() != 0):
        raise ValueError("原生问答后端未确认暂停和清理")
    with _locked_results(ctx):
        path, saved = _paused_question_marker(ctx, _scope(ctx, job, backend), proof, session_id)
        saved["paused_question_delivery"] = proof
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        saved["paused_question_text_sha256"] = digest
        write_private_text(path, json.dumps(saved, ensure_ascii=False))
        upsert_job_record(ctx.state_dir, job.message_id, paused_question_delivery=proof,
                          paused_question_text_sha256=digest, native_question_answer_allowed=True,
                          outbox_version=1, outbox_state='pending', outbox_attempts=0, outbox_next_at=0)
        job.paused_question_delivery = proof


def _load_paused_question_delivery(ctx, record, backend, chat_id, statuses=("pending",)):
    if (record.get("execution_status") != "waiting_user" or record.get("native_resume_allowed") is not False
            or record.get("native_question_answer_allowed") is not True
            or record.get("checkpoint_question_deferred") is not True
            or record.get("checkpoint_question_stopped") is True):
        raise ValueError("本轮未处于可恢复投递的正常问答状态")
    scope = {"backend": backend, "run_id": record.get("result_run_id"), "job_key": record.get("key"),
             "job_id": record.get("job_id"), "chat_id": chat_id, "topic_id": ctx.topic_id,
             "workdir": str(Path(ctx.workdir).resolve())}
    _, saved = _paused_question_marker(ctx, scope, record.get("paused_question_delivery"), record.get("session_id"), statuses)
    text = record.get("final_text")
    if (not isinstance(text, str) or not text.strip()
            or saved.get("paused_question_delivery") != record.get("paused_question_delivery")
            or saved.get("paused_question_text_sha256") != record.get("paused_question_text_sha256")
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != saved.get("paused_question_text_sha256")):
        raise ValueError("原生问答投递证明或正文不匹配")


def _redelivery_lock(ctx, record):
    """One job's redeliveries, one at a time, in any process: its own lock file, held across the send.
    Only redeliveries of that job wait for it; no task runner takes it."""
    name = "redelivery-" + hashlib.sha256(str(record.get("key")).encode()).hexdigest()[:32]
    return shared_file_lock(_folder(ctx.state_dir) / name)


@contextmanager
def paused_question_delivery(ctx, record, backend, chat_id):
    """As authorized_delivery, for a paused native question's body and forms."""
    with _redelivery_lock(ctx, record):
        with _locked_results(ctx):
            current = find_job_record(ctx.state_dir, record.get("key"))
            if not current or current.get("paused_question_delivery") != record.get("paused_question_delivery"):
                raise ValueError("原生问答运行已变化")
            _load_paused_question_delivery(ctx, current, backend, chat_id)
        yield current


def consume_paused_question_answer(ctx, job, backend, current_session):
    grant = (job.recovery_context or {}).get("native_question_continuation")
    if not isinstance(grant, dict) or set(grant) != {"token", "source_key"}:
        raise ValueError("原生问答续答凭据无效")
    token = grant["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{16}", token) or job.message_id != "answer-" + token:
        raise ValueError("原生问答续答任务不匹配")
    from covey.core.session_guard import transfer_question_session
    with _locked_results(ctx):
        if job.cancel_event.is_set():
            raise JobCancelled("回答任务已停止")
        original = find_job_record(ctx.state_dir, grant["source_key"])
        if not original or original.get("delivery_ok") is not True or original.get("question_delivery_ok") is not True:
            raise ValueError("原生问题尚未完整送达")
        _load_paused_question_delivery(ctx, original, backend, job.chat_id, ("pending", "submitting", "answered"))
        path = ctx.state_dir / "questions" / (token + ".json")
        question = json.loads(path.read_text())
        sid = original["session_id"]
        if (current_session not in (None, sid) or question.get("job_id") != original["job_id"]
                or question.get("status") not in {"submitting", "answered"}
                or question.get("body_delivery_confirmed") is not True or question.get("display_pending") is not False
                or not question.get("answers") or not question.get("answer_event")
                or question.get("native_answer_run_id") is not None):
            raise ValueError("原生回答已消费或上下文已改变")
        # A failure after this durable claim must not create a second model run.
        upsert_job_record(ctx.state_dir, job.message_id, result_retry_policy="native_answer_once")
        claimed = transition_question(path, {"submitting", "answered"}, native_answer_run_id=job.bridge_run_id)
        if not claimed:
            raise ValueError("原生回答状态已改变")
        transfer_question_session(ctx, backend, sid, original["result_run_id"], job.bridge_run_id, token)
        if job.cancel_event.is_set():
            raise JobCancelled("回答任务已停止")
        return sid


def paused_question_delivery_key(prefix, key, proof):
    return f"{prefix}-native-question-{key}-{proof['run_id']}-{proof['generation']}"


def load_authorized_reply(ctx, record, backend, chat_id):
    """Read only; missing directories are not created by loading."""
    with _locked_results(ctx):
        return _load_authorized(ctx, record, backend, chat_id)


@contextmanager
def authorized_delivery(ctx, record, backend, chat_id):
    """Serialize one job's redeliveries and reread its persistent ledger under the verified-results
    lock. That lock is let go before the caller sends: every task runner takes it for each candidate,
    each turn's end and each answer's start, so it is never held across a Feishu call (rule 1). The
    saved reply of an ended job no longer changes, and its send keys are fixed, so a send after the
    check sends what was checked."""
    with _redelivery_lock(ctx, record):
        with _locked_results(ctx):
            current = find_job_record(ctx.state_dir, record.get("key"))
            identity = ("job_id", "verified_result_key", "result_run_id", "verified_reply_generation",
                        "verified_reply_fingerprint")
            if not current or any(current.get(key) != record.get(key) for key in identity):
                raise ValueError("重投请求与当前保存结果不匹配")
            reply, outcome = _load_authorized(ctx, current, backend, chat_id)
        yield reply, outcome, current


FILE_BYTES = 20000  # A saved reply or a question's words longer than this (UTF-8) goes out as a file.


def saved_delivery_kind(reply, outcome):
    return "file" if len(display_saved_reply(reply, outcome).encode("utf-8")) > FILE_BYTES else "message"


def saved_delivery_key(prefix, key, reply):
    return f"{prefix}-verified-result-{key}-{reply.fingerprint}"


def display_saved_reply(reply, outcome):
    notice = outcome.notice()
    return notice + "\n\n" + reply.text if notice else reply.text

"""What a failed, interrupted or stopped task's record allows next, decided in one place: the recovery card's
state and button (app/recovery_cards.py recovery_card, app/outbox.py retry_label) and what 检查并继续 or
重新投递 does with it (task_controls._retry_topic). Pure: whether a reply is saved (its index file) and whether
the task's native question is still live (the question store) are read by app/ and passed in; the question
store only when the decision gets that far, as before.

Four known divergences between the button (``_label``) and the click (``_kind``) are kept as they were:
- the button asks ``saved_reply_undelivered`` (record fields only) while the click asks ``saved`` (the
  index file too);
- a saved_delivery_only reply whose delivery needs reconciling (``reconciliation_required``,
  ``idempotency_conflict``) gets no button, but a click on an older card, or an automatic retry, still
  redelivers it (with the same saved delivery key, so Feishu's idempotency keeps it one message);
- a policy_stop record with ``question_delivery_ok`` False and a ``final_text`` shows 重新投递结果, but the
  click refuses to rerun, since it only checks ``delivery_ok`` is False;
- a ``paused_question_delivery`` of ``{}`` counts as absent for the button, but the click (``is not None``)
  redelivers the paused question."""
from __future__ import annotations

import dataclasses

from covey.core.saved_results import withdrawn_unauthorized

RETRY_LABEL = "检查并继续"
REDELIVER_LABEL = "重新投递结果"
REDELIVER_QUESTION_LABEL = "重新投递问题"

# Recovery.card: what the recovery card says became of the task.
SAVED_FAILED = "saved_failed"  # 执行失败: no reply authorized for publishing
SAVED_DELIVERED = "saved_delivered"  # 结果已送达
SAVED_PENDING = "saved_pending"  # 结果投递待处理
START_FAILED = "start_failed"  # 任务未能启动
POLICY_STOP = "policy_stop"  # 任务已中断, by an official refusal
INTERRUPTED = "interrupted"  # 任务已中断

# Recovery.kind: what 检查并继续 / 重新投递 does.
GUARDED = "guarded"  # the policy notifier owns it: refuse
POLICY_STOPPED = "policy_stopped"  # refuse to rerun
REDELIVER_FAILED = "redeliver_failed"  # send the record's final text again
NATIVE_LOST = "native_lost"  # the native question lost its run: refuse
REDELIVER_QUESTION = "redeliver_question"  # send the paused native question again
ANSWERED_ONCE = "answered_once"  # the answer was submitted already: refuse
REDELIVER_SAVED = "redeliver_saved"  # send the saved, authorized reply again
SAVED_QUESTION = "saved_question"  # check the saved reply's question may resume, then ``after``
WAIT_CANCELLED = "wait_cancelled"  # the checkpoint question's wait was cancelled: refuse
REQUEUE = "requeue"  # run the task again from its checkpoint


@dataclasses.dataclass(frozen=True)
class Recovery:
    kind: str  # Recovery.kind above; "" when classify was not told ``saved``
    label: str  # the recovery button, "" for none
    card: str  # Recovery.card above
    after: str = ""  # SAVED_QUESTION: the kind once the question is authorized


def classify(record, *, saved=None, native_live_question=False):
    """``saved``: the record has a saved reply (saved_results.has_saved_reply); ``native_live_question``: a
    native question of its task is still marked live. Either may be a function returning that, called only
    when the decision reaches it (``saved`` at most once, after the policy checks, as before). The label and
    card never read either; ``kind`` is "" when ``saved`` is None."""
    kind, after = ("", "") if saved is None else _kind(record, _once(saved), native_live_question)
    return Recovery(kind, _label(record), _card(record), after)


def _once(value):
    if not callable(value):
        return lambda: value
    memo = []
    return lambda: memo[0] if memo else memo.append(value()) or memo[0]


def saved_reply_undelivered(record):
    """A reply the task finished but the topic never got: retry_topic resends it without running the task."""
    return ((record.get('delivery_ok') is False or record.get('question_delivery_ok') is False)
            and isinstance(record.get('final_text'), str)
            and not (record.get('checkpoint_question_deferred') and record.get('checkpoint_question_stopped')))


def needs_question(record):
    return record.get('execution_status') == 'waiting_user' and record.get('question_delivery_ok') is not True


def saved_delivery(record):
    """Only the saved reply may be sent for this record; it is never run again."""
    return record.get('result_retry_policy') == 'saved_delivery_only' and not withdrawn_unauthorized(record)


def authorized(record):
    return record.get('verified_reply_authorized') is True and record.get('verified_reply_state') == 'authorized'


def _label(record):
    if record.get('policy_guard_requested'):
        return ''  # Dedicated durable policy notifier; never rerun/re-send partial results.
    if record.get('delivery_error') == 'audit_rejected' and not record.get('delivery_ok'):
        return ''  # Feishu's audit refused it even masked: another send is the same text.
    if record.get('policy_stop'):
        return REDELIVER_LABEL if saved_reply_undelivered(record) else ''
    if saved_delivery(record):
        if record.get('paused_question_delivery'):
            return REDELIVER_QUESTION_LABEL if not record.get('question_delivery_ok') else ''
        if not authorized(record) or (record.get('delivery_ok') and not needs_question(record)):
            return ''
        if record.get('delivery_error') in {'reconciliation_required', 'idempotency_conflict'}:
            return ''
        return REDELIVER_LABEL
    if record.get('result_retry_policy') == 'native_answer_once':
        return ''
    if saved_reply_undelivered(record):
        return REDELIVER_LABEL
    return RETRY_LABEL


def _card(record):
    if saved_delivery(record):
        return SAVED_FAILED if not authorized(record) else SAVED_DELIVERED if record.get('delivery_ok') else SAVED_PENDING
    if record.get("runner_start_failed") is True:
        return START_FAILED
    return POLICY_STOP if record.get("policy_stop") else INTERRUPTED


def _kind(record, saved, native_live_question):
    if record.get("policy_guard_requested"):
        return GUARDED, ""
    if record.get("policy_stop"):
        undelivered = record.get("delivery_ok") is False and isinstance(record.get("final_text"), str)
        return (REDELIVER_FAILED if undelivered else POLICY_STOPPED), ""
    if not saved() and (native_live_question() if callable(native_live_question) else native_live_question):
        return NATIVE_LOST, ""
    if record.get("paused_question_delivery") is not None:
        return REDELIVER_QUESTION, ""
    if record.get("result_retry_policy") == "native_answer_once":
        return ANSWERED_ONCE, ""
    if saved():
        # Workflow waiting_user is separate from the completed native turn.
        ordinary_question = (record.get("execution_status") == "waiting_user"
            and record.get("checkpoint_question_deferred") is True and record.get("native_resume_allowed") is True)
        if not ordinary_question:
            return REDELIVER_SAVED, ""
        return SAVED_QUESTION, _otherwise(record)
    return _otherwise(record), ""


def _otherwise(record):
    if record.get("checkpoint_question_deferred") and record.get("checkpoint_question_stopped"):
        return WAIT_CANCELLED
    if ((record.get("delivery_ok") is False or record.get("question_delivery_ok") is False)
            and isinstance(record.get("final_text"), str)):
        return REDELIVER_FAILED
    return REQUEUE

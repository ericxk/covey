"""Resume delivery of authorized saved replies only; never run a Harness."""
from __future__ import annotations

import time

from covey.core.jobs import context_for_record, find_job_record, select_job_records, upsert_job_record
from covey.core.loops import every
from covey.core import recovery
from covey.core.recovery import classify, needs_question
from covey.core.topics import read_topic


RETRY_NOTE = "检查并继续：新开一个任务，带上本轮的检查点和结果，先核查已完成的工作再继续，避免重复提交外部任务。"
REDELIVER_NOTE = "重新投递结果：只重新发送已保存的回复，不重新执行任务。"
REDELIVER_LABEL = recovery.REDELIVER_LABEL
AUDIT_REFUSED_NOTE = "飞书内容审核拒收了这条结果，打码后仍未通过；原文保存在本地任务记录，请在本话题继续说明需求"


def retry_label(record):
    """The recovery button for ``record``, "" for none (core/recovery.py)."""
    return classify(record).label


def record_attempt(state, key, failure, *, delivered=False, now=None):
    now = time.time() if now is None else now
    record = find_job_record(state, key) or {}
    attempts = int(record.get('outbox_attempts') or 0) + 1
    retryable = failure is not None and failure.retryable
    status = 'delivered' if delivered else ('pending' if retryable and attempts < 12 else 'paused')
    delay = max(min(300, 5 * 2 ** min(attempts - 1, 6)), failure.retry_after if failure else 0)
    upsert_job_record(state, key, outbox_state=status, outbox_attempts=attempts,
                      outbox_next_at=now + delay,
                      delivery_error='' if delivered else (failure.category if failure else 'delivery_unclassified'),
                      **({'timings': {'delivered': now}} if delivered else {}))


def due(record, now):
    """A saved result the outbox sends now: authorized (or a paused question), not delivered, its retry time reached."""
    paused = bool(record.get('paused_question_delivery'))
    if record.get('outbox_version') != 1 or (record.get('delivery_ok') is True and not needs_question(record)):
        return False
    return not ((not paused and (record.get('verified_reply_authorized') is not True
                                 or record.get('verified_reply_state') != 'authorized'))
                or record.get('outbox_state') in {'paused', 'delivered'}
                or record.get('outbox_next_at', 0) > now)


def tick(gateway, ctx, *, now=None):
    now = time.time() if now is None else now
    for record in select_job_records(ctx.state_dir, lambda record: due(record, now), log=gateway.runtime.log):
        paused = bool(record.get('paused_question_delivery'))
        if gateway.runtime.jobs.settling(record.get('message_id')):
            continue  # The first sender still owns this result.
        backend = record.get('backend')
        owner = gateway.apps.get(backend)
        if owner is None:
            continue  # A Harness no longer in the roster: its saved reply is left as it is, unread.
        try:
            target = context_for_record(owner.harness.context, record)
            topic = read_topic(target)
            if topic.get('chat_id') != record['chat_id'] or (topic.get('harness_id') and topic['harness_id'] != backend):
                raise ValueError('saved topic mismatch')
            if paused:
                owner.controls.redeliver_paused_question(target, record['chat_id'], record)
            else:
                owner.controls.redeliver_verified_result(target, record['chat_id'], record)
            current = find_job_record(ctx.state_dir, record['key'])
            if (not paused and current and current.get('delivery_ok') and current.get('card_id')
                    and current.get('execution_status') != 'waiting_user'):
                # Update existing status card; no new menu, topic or model run.
                owner.controls.repaint_recovery_card(target, current)
        except (OSError, ValueError, KeyError, TypeError):
            upsert_job_record(ctx.state_dir, record['key'], outbox_state='paused',
                              delivery_error='reconciliation_required')


def run(gateway, ctx, *also):
    """Every 5 s: resume saved deliveries, then each of ``also`` (same arguments), such as the
    automatic retries; one step's failure does not stop the others."""
    every(gateway.runtime, 5, tick, *also, args=(gateway, ctx), isolate="outbox")

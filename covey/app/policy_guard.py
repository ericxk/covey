"""Independent policy stop consumer: covers adopted runners from older releases.

    policy-source.json: {"command": [absolute_executable, ...]}
The read-only adapter accepts a JSON cursor on stdin and returns {cursor, events,
health}. No model or Feishu call in adapters. Intent and delivery are durable;
only the Bridge writes the card, after the task runner has stopped.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import time

from covey.core.jobs import context_for_record, find_job_record, select_job_records, upsert_job_record
from covey.core.loops import every
from covey.app import policy_retry
from covey.core.butler import worded
from covey.core.model_policy import policy_summary
from covey.core.policy_feed import match_task, valid_observation
from covey.app import devices as device_turns
from covey.core import device_reap
from covey.core.policy_process import stop_tree
from covey.core.run_dir import run_dir, runner_alive, runner_info
from covey.core.state import read_json, write_json
from covey.feishu.transport import last_failure, set_failure


def intent_path(state, key):
    return Path(state) / 'policy-stops' / (hashlib.sha256(key.encode()).hexdigest() + '.json')


def requested(state, key):
    return intent_path(state, key).exists()


def ingest(state, packet):
    events = packet.get('events')
    if (not isinstance(events, list) or len(events) > 2000
            or not all(valid_observation(row) for row in events)):
        raise ValueError('invalid_policy_source_packet')
    for row in events:
        name = hashlib.sha256(row['id'].encode()).hexdigest() + '.json'
        path = Path(state) / 'policy-inbox' / name
        if not path.exists():
            write_json(path, {'observation': row, 'status': 'pending', 'received': time.time()})
    # Advance only after every event is durable; a crash before this replays identical IDs.
    write_json(Path(state) / 'policy-source-state.json', {
        'cursor': packet.get('cursor', {}), 'health': packet.get('health', {}),
        'last_success': time.time(), 'error': ''})


class SourceKilled(RuntimeError):
    """The source subprocess was stopped by SIGTERM, SIGINT or SIGHUP, as on Bridge shutdown: not a source failure."""


# Return codes of a subprocess ended by SIGTERM, SIGINT or SIGHUP. Other signals
# (SIGKILL from the OOM killer, SIGSEGV) are still source failures.
STOP_SIGNALS = frozenset({-15, -2, -1})


def fetch(state):
    config = read_json(Path(state) / 'policy-source.json', {})
    command = config.get('command')
    if not command:
        return False
    if (not isinstance(command, list) or not all(isinstance(x, str) for x in command)
            or not Path(command[0]).is_absolute()):
        raise ValueError('invalid_policy_source_command')
    before = read_json(Path(state) / 'policy-source-state.json', {})
    result = subprocess.run(command, input=json.dumps(before.get('cursor', {})),
                            capture_output=True, text=True, timeout=25)
    if result.returncode in STOP_SIGNALS:
        raise SourceKilled('policy_source_killed')
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise RuntimeError('policy_source_unavailable')
    ingest(state, json.loads(result.stdout))
    return True


def claim(state, row, record):
    path = intent_path(state, record['key'])
    if path.exists() or record.get('policy_stop'):
        return 'already_stopped'
    directory = run_dir(state, record['key'])
    identity = runner_info(directory)
    if (not identity or identity.get('key') != record['key']
            or identity.get('job_id') != record['job_id'] or not runner_alive(directory)):
        return 'runner_identity_unconfirmed'
    # 'fallback_retry': this release claimed it, so its stop may be retried on an older model (app/policy_retry.py).
    # An older release's intent has none: finish, which runs again for every notice not yet sent, never schedules
    # a retry for a stop recorded before the feature.
    write_json(path, {'key': record['key'], 'job_id': record['job_id'], 'observation': row,
                      'runner': identity, 'phase': 'stop_requested', 'notice': 'pending',
                      'attempts': 0, 'next_attempt': 0, 'card_updated': False, 'fallback_retry': True})
    upsert_job_record(state, record['key'], policy_stop=row['event'],
                      policy_guard_requested=True, auto_retry_state='cancelled', native_resume_allowed=False)
    return 'stop_requested'


def finish(gateway, ctx, path):
    """One task's pending stop; reentrant across Bridge restarts, never reruns a task."""
    intent = read_json(path, {})
    if not intent:
        return
    key = intent['key']
    record = find_job_record(ctx.state_dir, key)
    if not record or record['job_id'] != intent['job_id']:
        raise ValueError('policy_intent_task_mismatch')
    if intent['phase'] == 'stop_requested':
        def checkpoint_tree(rows):
            intent['cleanup'] = {'processes': rows, 'confirmed': False}
            write_json(path, intent)
        cleanup = stop_tree(intent['runner'], before_kill=checkpoint_tree)
        # A previously killed runner after a crash is gone; saved tree identities below
        # remain authoritative. Never signal a replacement runner.
        if not cleanup['confirmed']:
            from covey.core.process import process_identity
            previous = intent.get('cleanup', {}).get('processes', [])
            if previous and all(process_identity(p['pid']) != p for p in previous):
                cleanup = {**cleanup, 'confirmed': True, 'reason': 'previous_tree_gone'}
        # stop_tree stops the runner's tree; an Agent on a device is not in it (its supervisor runs there): it is reaped
        # through the device, and the stop is not confirmed until the device says nothing of it is left.
        if cleanup['confirmed'] and not device_turns.reap_run(ctx.state_dir, run_dir(ctx.state_dir, key), record,
                                                              device_reap.POLICY):
            cleanup = {**cleanup, 'confirmed': False, 'reason': 'device_reap_pending'}
        intent['cleanup'] = cleanup
        intent['phase'] = 'stopped' if cleanup['confirmed'] else 'cleanup_unconfirmed'
        write_json(path, intent)
    elif (intent['phase'] == 'cleanup_unconfirmed' and intent.get('cleanup', {}).get('reason') == 'device_reap_pending'
          and device_turns.reap_run(ctx.state_dir, run_dir(ctx.state_dir, key), record, device_reap.POLICY)):
        intent['cleanup'] = {**intent['cleanup'], 'confirmed': True, 'reason': 'device_reaped'}
        intent['phase'] = 'stopped'
        write_json(path, intent)
    if intent['phase'] == 'cleanup_unconfirmed':
        # Do not claim success or automatically kill a new PID. Keep the stop latched.
        cleanup_ok = False
    else:
        cleanup_ok = True
    event = intent['observation']['event']
    # A refusal of cyber or bio, its tree stopped, claimed by this release: one retry on an older model
    # (app/policy_retry.py), said here.
    found = policy_retry.schedule(ctx.state_dir, key, event) if cleanup_ok and intent.get('fallback_retry') else None
    # In the butler's words (T3027's card said 再在本话题明确提出): the record keeps the text a 重新投递 sends.
    text = policy_retry.stop_text(policy_summary(event, cleanup=cleanup_ok), event, found, record)
    record = upsert_job_record(ctx.state_dir, key, status='cancelled', execution_status='cancelled',
        status_message=('官方拒绝／降级，任务已停止' + ('；' + policy_retry.will_retry(found) if found else '')
                        if cleanup_ok else '官方拒绝／降级，进程清理待确认'),
        policy_stop=intent['observation']['event'], policy_guard_finalized=True,
        # A retry scheduled by the task runner (app/policy_retry.py at_stop) waits for this and does not start
        # while it says unconfirmed (policy_retry.tick).
        policy_guard_cleanup='confirmed' if cleanup_ok else 'unconfirmed',
        auto_retry_state='cancelled', native_resume_allowed=False, verified_reply_authorized=False,
        verified_reply_state='revoked', final_text=text, result_retry_policy='policy_notice_only',
        **({'delivery_ok': False} if intent['notice'] != 'sent' else {}))
    owner = gateway.apps.get(record.get('backend'))
    if owner is None:
        return
    target = context_for_record(owner.harness.context, record, record.get('reply_to_message_id') or '')
    if not intent.get('session_blocked'):
        from covey.core.session_guard import block_policy_session
        block_policy_session(target, record['backend'], record.get('session_id'), intent['observation']['id'])
        intent['session_blocked'] = True
        write_json(path, intent)
    if cleanup_ok and not intent.get('questions_closed'):
        from covey.core.jobs import saved_job
        job = saved_job(record, record['chat_id'], record['topic_id'], target.reply_to_message_id)
        owner.questions.native.dispatch(target, job, 'cancel_questions', {}, None)
        intent['questions_closed'] = True
        write_json(path, intent)
    if cleanup_ok and not intent['card_updated']:
        # Old runner can no longer overwrite this card. All card writes go through Cards.
        retry = record.get('policy_retry_state')
        try:
            if not record.get('card_id'):
                intent['card_updated'] = True
                intent['card_note'] = 'no_existing_card; topic_notice_required'
            elif record.get('retried_by') or retry == 'queued':
                # The retry on an older model replaced this card with where the task went on (app/policy_retry.py,
                # after CARD_WAIT): a repaint from the record would take that header away.
                intent['card_updated'] = True
                intent['card_note'] = 'moved_by_policy_retry'
            elif retry != 'submitting':  # One being queued now: the next round sees how it went.
                intent['card_updated'] = bool(owner.controls.repaint_recovery_card(target, record))
        except Exception as exc:
            gateway.runtime.log('policy card update failed: ' + type(exc).__name__)
        write_json(path, intent)
        if intent['card_updated']:  # A retry waits for it (app/policy_retry.py guard_done).
            upsert_job_record(ctx.state_dir, key, policy_card_updated=True)
    if intent['notice'] in {'sent', 'paused'} or intent['next_attempt'] > time.time():
        return
    set_failure(None)
    # The record's text: the first finish's, or the one policy_retry.unpromise left when the retry did not run.
    body, send_key = notice_send(intent, worded(record.get('final_text') or text, record),
                                 owner.voice.key('policy-stop', key))
    intent.update(notice_text=body, notice_key=send_key)
    sent = owner.messenger.reply(target, record['chat_id'], body, send_key, voice=owner.voice)
    failure = last_failure()
    intent['notice_ambiguous'] = bool(getattr(failure, 'ambiguous', False))
    intent['attempts'] += 1
    intent['notice'] = 'sent' if sent else ('pending' if failure and failure.retryable and intent['attempts'] < 12 else 'paused')
    intent['next_attempt'] = time.time() + max(5, min(300, 5 * 2 ** min(intent['attempts'], 6)),
                                             failure.retry_after if failure else 0)
    write_json(path, intent)
    upsert_job_record(ctx.state_dir, key, delivery_ok=bool(sent), policy_notice_state=intent['notice'],
                      policy_notice_at=time.time())


def notice_send(intent, text, base_key):
    """(text, idempotency key) for the stop notice's next attempt. A key never carries two texts (the transport
    refuses it as idempotency_conflict, feishu/transport.py receipt). The record's text can change between attempts:
    policy_retry.unpromise takes the retry's promise back while a notice is pending. If the last attempt surely did
    not land (its failure was not ambiguous), the new text goes under a key of its own; if it may have landed, the
    same text again under the same key, which the transport reconciles, never a second notice. An intent of an older
    release (no notice_text) sends as it did: the record's text under the stop's key."""
    last, last_key = intent.get('notice_text'), intent.get('notice_key') or base_key
    if last is None or last == text:
        return text, last_key
    if intent.get('notice_ambiguous'):
        return last, last_key
    return text, base_key + '-' + hashlib.sha256(text.encode()).hexdigest()[:12]


def tick(gateway, ctx):
    state = ctx.state_dir
    # Resume interrupted stops before fetching, even if VPS is temporarily unreachable.
    for path in sorted((Path(state) / 'policy-stops').glob('*.json')):
        try:
            saved = read_json(path, {})
            if saved.get('notice') != 'sent' or not saved.get('card_updated'):
                finish(gateway, ctx, path)
        except Exception as exc:
            gateway.runtime.log('policy stop pending: ' + type(exc).__name__)
    try:
        enabled = fetch(state)
    except SourceKilled:
        enabled = True
    except Exception as exc:
        if gateway.runtime.shutdown.is_set():
            return
        status_path = Path(state) / 'policy-source-state.json'
        write_json(status_path, {**read_json(status_path, {}), 'error': type(exc).__name__, 'last_failure': time.time()})
        gateway.runtime.log('policy source unavailable: ' + type(exc).__name__)
        enabled = True
    if not enabled:
        return
    records = select_job_records(state, lambda r: r.get('backend') == 'claude'
                                 and r.get('status') in {'running', 'cancel_requested'})
    for path in sorted((Path(state) / 'policy-inbox').glob('*.json')):
        saved = read_json(path, {})
        if saved.get('status') != 'pending':
            continue
        row = saved['observation']
        record, reason = match_task(row, records, time.time())
        if record:
            reason = claim(state, row, record)
        # A session may not yet be written by the runner: retain for a bounded retry.
        if reason in {'no_active_task_interval', 'runner_identity_unconfirmed'} and time.time() - saved['received'] < 120:
            continue
        write_json(path, {**saved, 'status': reason, 'job_id': record.get('job_id') if record else None})
        if reason == 'stop_requested':
            try:
                finish(gateway, ctx, intent_path(state, record['key']))
            except Exception as exc:
                gateway.runtime.log('policy stop pending: ' + type(exc).__name__)


def run(gateway, ctx):
    every(gateway.runtime, 2, tick, args=(gateway, ctx))

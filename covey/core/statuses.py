"""The status machines of the records: question and job transitions, the job status groups, the form
records' statuses, and how a job record merges a late write. Pure data and functions (no I/O): the
record stores (core/store.py, core/jobs.py) enforce them."""
from __future__ import annotations


QUESTION_TRANSITIONS = {
    'pending': {'submitting', 'cancelled', 'superseded', 'set_aside'},
    'submitting': {'answered', 'uncertain', 'cancelled'},
    'answered': {'uncertain'},
    'uncertain': {'pending', 'answered', 'cancelled'},
    # 不处理 (T604 #15, app/questions.py set_aside): off the 等你 lists, each question 不回答此题, nothing sent yet. Back to
    # pending on 撤销, until the topic's next turn takes it as its answer (take_set_aside), or cancelled with its binding.
    'set_aside': {'pending', 'answered', 'cancelled'},
    'cancelled': set(),
    'superseded': set(),
}
# Questions still waiting on the user or on their answer's delivery: answered, cancelled and
# superseded ones are settled (an answered one turns uncertain only when its delivery is checked again).
OPEN_QUESTIONS = frozenset({'pending', 'submitting', 'uncertain'})


# How a run ends.
TERMINAL = frozenset({'done', 'failed', 'cancelled', 'interrupted'})


JOB_TRANSITIONS = {
    'waiting_workspace': {'queued', 'cancelled', 'failed'},
    'queued': {'running', 'waiting_user', 'done', 'cancel_requested', 'cancelled', 'interrupted', 'failed'},
    'running': {'done', 'failed', 'waiting_user', 'cancel_requested', 'cancelled', 'interrupted'},
    'cancel_requested': {'cancelled', 'interrupted', 'failed', 'done'},
    'waiting_user': {'continued', 'running', 'cancelled', 'interrupted'},
    'continued': set(),
    **{state: set() for state in TERMINAL},
}
# Not finished: some later status can still follow.
NOT_ENDED = frozenset(state for state, following in JOB_TRANSITIONS.items() if following)
# Finished: no later status can follow (TERMINAL, and continued: answered, its follow-up is a new job).
ENDED = frozenset(state for state, following in JOB_TRANSITIONS.items() if not following)
# Queued or executing, in this process or a task runner.
RUNNING = frozenset({'queued', 'running', 'cancel_requested'})
# Not finished, nothing executing: waiting for a workspace or for the user's answer.
WAITING = NOT_ENDED - RUNNING

# The statuses a form record (app/form_cards.py) moves through, by its folder in the state directory; '' is no
# record yet. Only the tests check it (tests/support/form_transitions.py): no writer reads it yet.
FORM_TRANSITIONS = {
    # build_launch_card saves each new form ready, then publish_launch_cards publishes the topic's first
    # (publish_form); a rebuilt card's form stays ready on the card it replaced, or unpublished.
    'launch-forms': {
        '': {'ready'},
        'ready': {'publishing', 'replaced', 'unpublished', 'managing_workspaces', 'creating_worktree', 'done',
                  'closed'},
        'publishing': {'ready', 'delivery_failed'},
        'managing_workspaces': {'replaced', 'closed'},
        'creating_worktree': {'done', 'worktree_failed'},
        **{state: set() for state in ('replaced', 'unpublished', 'done', 'closed', 'delivery_failed',
                                      'worktree_failed')},
    },
    'switch-forms': {
        '': {'publishing'},
        'publishing': {'ready', 'delivery_failed'},
        'ready': {'cancelled', 'done'},
        **{state: set() for state in ('cancelled', 'done', 'delivery_failed')},
    },
    'rewind-forms': {
        '': {'publishing'},
        'publishing': {'ready', 'delivery_failed'},
        'ready': {'cancelled', 'done'},
        'done': {'undone'},
        **{state: set() for state in ('cancelled', 'undone', 'delivery_failed')},
    },
}
# Launch-form statuses besides FORM_TRANSITIONS' plain ones: a launch card that became the workspace
# manager (app/workspace_manager.py), and one being turned into a worktree (app/launch.py).
MANAGING = 'managing_workspaces'
WORKTREE_STATUS = 'creating_worktree'
# Launch forms the stale closer closes (core/stale.py, app/stale.py); one in WORKTREE_STATUS keeps
# its topic open.
OPEN_FORMS = frozenset({'ready', MANAGING})


def merge_job_record(current, fields):
    """Terminal execution state cannot be reverted by a late heartbeat."""
    result = {**current, **fields}
    recovery = fields.get('status') == 'continued' and bool(fields.get('resumed_by'))
    saved_reply = bool(current.get('final_text')) or (
        current.get('checkpoint_question_deferred') is True and current.get('final_text') == '')
    delivery_repaired = (current.get('status') in {'failed', 'interrupted'} and fields.get('delivery_ok') is True
        and saved_reply and fields.get('status') == current.get('execution_status', 'done'))
    deferred_delivery_failed = (current.get('status') in {'running', 'waiting_user'}
        and fields.get('status') == 'failed' and fields.get('question_delivery_ok') is False
        and current.get('checkpoint_question_deferred') is True and current.get('execution_status') == 'waiting_user')
    old, new = current.get('status'), fields.get('status', current.get('status'))
    if old in JOB_TRANSITIONS and new != old and new not in JOB_TRANSITIONS[old] and not (recovery or delivery_repaired or deferred_delivery_failed):
        result['status'] = current['status']
        result['status_message'] = current.get('status_message', '')
    if current.get('policy_guard_finalized') or fields.get('policy_guard_finalized'):
        result.update(status='cancelled', execution_status='cancelled', native_resume_allowed=False,
                      verified_reply_authorized=False, verified_reply_state='revoked', auto_retry_state='cancelled')
        if current.get('policy_guard_finalized'):
            # Only app/policy_retry.py unpromise, taking a retry's promise out (it names the reason), changes the
            # text and status line the guard wrote.
            words = () if fields.get('policy_retry_unpromised') else ('final_text', 'status_message')
            for key in ('policy_stop', 'result_retry_policy', *words):
                result[key] = current.get(key)
    result['schema_version'] = 1
    result['revision'] = int(current.get('revision', 0)) + 1
    return result

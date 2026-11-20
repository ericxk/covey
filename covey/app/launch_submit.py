"""A launch form's submission: ``submit_launch_form`` checks the 开始任务 click (model, effort, workspace, run mode),
starts the topic and remembers the choice (``remember_launch``); a worktree launch creates its checkout in the
background (``finish_worktree_launch``) and is finished or given back after a restart (``recover_worktree_launches``).
The card's own building, rebuilding and closing stay in app/launch.py."""
from __future__ import annotations


import functools
import re
import threading
import time
from pathlib import Path

from covey.core import devices, timing
from covey.core.state import read_json
from covey.core.locks import LOCK, resource_lock, topic_lock
from covey.core.workspaces import RUN_MODES, add_topic_worktree, plan_topic_worktree, resolve_binding


from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.cards.launch_receipt import build_preparing_card
from covey.app.card_actions import form_fields
from covey.app.card_lanes import after_ack
from covey.app.form_cards import LAUNCH_FORM_LOCK, replace_form_card, save_record
from covey.app.launch_receipt import publish_receipt
from covey.app.launch_models import available_models, catalog_changed, run_choice
from covey.core.menu_choice import budget_effort, chosen_work_mode
from covey.app import model_usage
from covey.app.topics import route_locked, topic_target
from covey.core.jobs import topic_job_records, upsert_job_record
from covey.core.topics import load_topic, read_topic, topic_label, workspace_locked
from covey.core.statuses import WORKTREE_STATUS
from covey.app.launch import (CLOSED, LATEST_DEFAULTS, RETIRED_TEXT, chat_defaults_path, form_draft, rebuild_launch_card,
                              retired_form, selected_effort)

OWNER_ONLY_PATH = '仅机器人主人可以填写其他目录，请选择已保存的工作区'  # A group member's, said on the card (app/access.py).
RUN_MODE_INVALID = '运行方式只能是原目录或独立 worktree'
RESTART_ABANDONED = '服务重启时 worktree 尚未创建完成'


def run_in_background(target, *args):
    threading.Thread(target=target, args=args, daemon=True, name='launch-worktree').start()


SWITCH_WAIT = 3.0


def follow_model_switch(ctx, event):
    """A 开始任务 click on the form a model switch replaces counts for its replacement.

    The client can show the old card for a few seconds after the switch, and a
    click can land while the replacement is still being published. The user has
    already picked the model, so wait briefly for the switch and start from the
    form it produced. Directory-picker replacements are not followed."""
    action = event.get('action_name')
    if not isinstance(action, str) or not action.startswith(SUBMIT_PREFIX):
        return event
    token, hops = action[len(SUBMIT_PREFIX):], 0
    deadline = time.monotonic() + SWITCH_WAIT
    while hops < 8 and re.fullmatch('[0-9a-f]{24}', token):
        try:
            record = read_json(ctx.state_dir / 'launch-forms' / (token + '.json'))
        except (OSError, ValueError):
            break
        if record.get('status') == 'replaced' and isinstance(record.get('replaced_by'), str):
            token, hops = record['replaced_by'], hops + 1
        elif record.get('status') == 'ready' and record.get('config_pending') and time.monotonic() < deadline:
            time.sleep(0.1)
        else:
            break
    return {**event, 'action_name': SUBMIT_PREFIX + token}


def submit_launch_form(app, ctx, event):
    event = follow_model_switch(ctx, event)
    outcome = _submit_launch_form(app, ctx, event)
    if not outcome:
        return False
    path = ctx.state_dir / 'launch-forms' / (event['action_name'][len(SUBMIT_PREFIX):] + '.json')
    if outcome == 'worktree':
        # A checkout takes seconds to tens of seconds on the NAS. One consumer
        # handles every card click, so the click only switches this card.
        run_in_background(finish_worktree_launch, app, ctx, path)
        publish_preparing(app, ctx, path)
    elif outcome in (True, 'started'):
        # Once more after the bound topic's own: this retries a receipt that did not go out (a
        # repeated click included); nothing when it did. Queue persistence is authoritative; a
        # cosmetic update cannot undo it.
        after_ack(functools.partial(publish_receipt, app, ctx, path), beside=True)
    if outcome == 'started':
        # A click again on a started topic's card also queues what its first click's follow-up left
        # waiting (it failed, or a restart came first): "请稍后重试" then means what it says.
        after_ack(functools.partial(app.submission.release_waiting, ctx, event['chat_id']))
    return True


def launch_in_progress(ctx):
    """The token of this topic's launch that is still creating its worktree, or ''."""
    token = read_topic(ctx).get('launching') or ''
    try:
        record = read_json(ctx.state_dir / 'launch-forms' / (token + '.json')) if token else {}
    except (OSError, ValueError):
        return ''
    return token if record.get('status') == WORKTREE_STATUS else ''


def _mark_launching(ctx, token):
    with LOCK:
        topic = read_topic(ctx)
        if token:
            topic['launching'] = token
        else:
            topic.pop('launching', None)
        save_record(ctx.history_path.parent / 'topic.json', topic)


def _submit_launch_form(app, ctx, event):
    action = event.get('action_name') or ''
    if not isinstance(action, str) or not action.startswith(SUBMIT_PREFIX):
        return False
    token = action[len(SUBMIT_PREFIX):]
    if not re.fullmatch('[0-9a-f]{24}', token):
        raise ValueError('选择卡无效，请在主聊天重新发送任务')
    path = ctx.state_dir / 'launch-forms' / (token + '.json')
    started = time.monotonic()
    submitted_at = time.time()
    # The topic lock orders this start with the topic's other submissions; LOCK is taken only
    # for each rewrite, so publishing cards here never holds up other topics.
    with resource_lock(LAUNCH_FORM_LOCK, path), topic_lock(ctx):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            raise ValueError('选择卡已失效，请在主聊天重新发送任务') from None
        started_already = _launch_record_state(ctx, event, record)
        if started_already:
            return started_already
        fields = _launch_fields(event)
        model_key, choice = _launch_model(app, ctx, event, record, token)
        effort, preference_effort = _launch_effort(record, choice, fields)
        name, (model, effort) = choice['harness'], run_choice(choice, effort)
        work_mode = chosen_work_mode(choice, record.get('selected_work_mode'))
        selection, run_mode = _launch_workspace(record, fields)
        if str(fields.get('path') or '').strip() and not app.runtime.access.is_owner(event.get('operator_id', '')):
            raise ValueError(OWNER_ONLY_PATH)  # A typed path is added to the saved workspaces.
        # Validate all user choices before changing a route or queue.
        if record['new_topic'] or route_locked(ctx) or workspace_locked(ctx):
            raise ValueError('本话题配置已固定；新任务请在主聊天发送消息')
        if not _waiting_jobs(ctx, event['chat_id']):
            raise ValueError('本话题没有待启动的消息，请先发送任务')
        timing.mark('checked')
        workspace, binding = resolve_binding(ctx.state_dir, ctx.default_workdir, selection)
        source = binding.workdir
        if binding.device:
            if not app.runtime.access.allows_device(event['chat_id'], binding.device):
                raise ValueError(f'设备 {binding.device} 不允许在这个聊天里使用（devices.json 的 spaces）')
            refused = devices.undeclared_refusal(binding.device, name, choice.get('title') or name)
            if refused:
                raise ValueError(refused)
            if run_mode == 'worktree':
                raise ValueError('设备上的工作区只能用原目录运行')
        plan = {'harness': name, 'model': model, 'effort': effort, 'label': choice['label'], 'work_mode': work_mode,
                'model_key': model_key, 'preference_effort': preference_effort,
                'workspace': workspace, 'source': binding.value if binding.device else str(source), 'run_mode': run_mode,
                **({'device': binding.device} if binding.device else {}),
                'chat_id': event['chat_id'], 'submitted_at': submitted_at,
                'topic': topic_label(ctx.state_dir, ctx.topic_id)}
        # Only a model whose card showed the select can turn it on; a form from before it has no choice. Such a
        # model's receipt says 开 or 关, another's nothing.
        if choice.get('redaction'):
            plan['redaction'] = record.get('selected_redaction') == 'on'
        if choice.get('work_modes'):
            plan['work_modes'] = True  # Its receipt names the mode, the default too (Focus).
        if run_mode == 'worktree':
            # Only the checkout is slow; a directory outside git is refused now.
            plan['worktree'] = plan_topic_worktree(source, ctx.topic_id)
            record.update(status=WORKTREE_STATUS, pending=plan)
            save_record(path, record)
            _mark_launching(ctx, token)
            app.runtime.log(f'launch form accepted harness={name} run_mode=worktree '
                    f'elapsed_ms={(time.monotonic() - started) * 1000:.0f}')
            return 'worktree'
        _start_topic(app, ctx, path, record, plan, source, '')
    app.runtime.log(f'launch form submitted harness={name} new_topic={record["new_topic"]} '
            f'elapsed_ms={(time.monotonic() - started) * 1000:.0f}')
    return True


def _launch_record_state(ctx, event, record):
    """'started' or 'creating' when an earlier click already started this form (a repeated callback
    must never create or enqueue twice); None when this click may start it; ValueError otherwise."""
    if record.get('topic_id') != ctx.topic_id or record.get('chat_id') != event.get('chat_id'):
        raise ValueError('选择卡不属于当前话题')
    if record.get('message_id') and record['message_id'] != event.get('message_id'):
        raise ValueError('选择卡来源不匹配')
    if record.get('status') == 'done':
        return 'started'
    if record.get('status') == WORKTREE_STATUS:
        return 'creating'  # The first click is still creating the worktree.
    if record.get('status') == 'publishing':
        raise ValueError('配置正在准备，请稍后点击开始；任务尚未启动')
    if record.get('status') == CLOSED:
        raise ValueError('本话题已结束，未启动任务；新任务请在主聊天发送消息')
    if retired_form(record):
        raise ValueError(RETIRED_TEXT.rstrip('。'))
    if record.get('status') != 'ready' or record.get('picker'):
        raise ValueError('请在最新选择表单中开始任务；选目录时先点击“使用所选目录”或“返回”。')
    current = read_topic(ctx)
    if (record.get('binding_revision') != current.get('binding_revision', 1)
            or record.get('harness_id') != current.get('harness_id')):
        raise ValueError('选择卡已过期，请使用本话题最新的选择卡')
    if launch_in_progress(ctx):
        raise ValueError('本话题正在创建独立 worktree，完成后任务会自动开始')
    return None


def _launch_fields(event):
    fields = form_fields(event)
    if fields is None:
        raise ValueError('表单内容无效，请重新选择')
    return fields


def _launch_model(app, ctx, event, record, token):
    """(model key, catalogue entry) the form selected, still the same in the live catalogue; a
    form whose catalogue was refreshed gets a new selection card."""
    model_key = record.get('selected_model')
    choice = record['models'].get(model_key)
    if not choice:
        app.submission.selection_card(ctx, event['chat_id'], 'refresh-' + token)
        raise ValueError('模型目录已刷新，请在新选择卡中选择模型')
    # A form sent before one entry per model names a preset the menu no longer lists alone.
    if catalog_changed(choice, available_models(app.topics, ctx).get(model_key)):
        raise ValueError('模型或预算目录已更新，请重新打开配置后选择')
    return model_key, choice


def _launch_effort(record, choice, fields):
    """(effort to run with, effort to remember as the preference) of the form's selection."""
    preference_effort = selected_effort(record, fields.get('effort'))
    effort = budget_effort(choice, preference_effort)  # 'default': the selected backend's native default.
    if not isinstance(effort, str) or (effort and effort not in choice['levels']):
        raise ValueError('该模型不支持此推理强度，可选：模型默认' + ''.join(' / ' + e for e in choice['levels']))
    return effort, preference_effort


def _launch_workspace(record, fields):
    """(workspace selection, run mode) the form submitted."""
    # A card sent before the path input went still submits one.
    custom_path = fields.get('path', '')
    if not isinstance(custom_path, str):
        raise ValueError('路径必须是文本')
    # A compact card records its selects as they change; the form holds no select.
    defaults = record.get('form_defaults', {}) if record.get('compact') else {}
    selection = custom_path.strip() or fields.get('workspace') or defaults.get('workspace')
    if not isinstance(selection, str) or not selection.strip():
        raise ValueError('请选择工作区')
    # Cards published before the field existed submit without it.
    run_mode = fields.get('run_mode') or defaults.get('run_mode') or 'direct'
    if not isinstance(run_mode, str) or run_mode not in RUN_MODES:
        raise ValueError(RUN_MODE_INVALID)
    return selection, run_mode


def _waiting_jobs(ctx, chat_id):
    return topic_job_records(ctx.state_dir, ctx.topic_id, chat_id=chat_id, status='waiting_workspace')


def _start_topic(app, ctx, path, record, plan, directory, branch):
    """Bind the topic and queue its waiting messages; the caller holds the form lock and the topic lock."""
    chat_id, name = plan['chat_id'], plan['harness']
    # Messages sent while a worktree was being created wait with the first one.
    pending = _waiting_jobs(ctx, chat_id)
    if not pending:
        raise ValueError('本话题没有待启动的消息，请先发送任务')
    target_app = app.topics.apps[name]
    target = app.topics.select_harness(ctx, chat_id, name)
    # '' for the default mode, so a default-mode or no-mode binding is written as before work modes.
    mode = {'work_mode': plan['work_mode']} if plan.get('work_mode') else {}
    # One topic is the authority for future messages, including after restart.
    with LOCK:
        topic = read_topic(target)
        topic.pop('launching', None)
        topic.pop('work_mode', None)
        topic.update(model=plan['model'], effort=plan['effort'], model_settings_snapshot=True, **mode)
        # Fixed for the topic's life from here: forks copy it, a model switch keeps it.
        if plan.get('redaction'):
            topic[REDACTION_KEY] = True
        else:
            topic.pop(REDACTION_KEY, None)
        save_record(target.history_path.parent / 'topic.json', topic)
    for job in pending:
        upsert_job_record(ctx.state_dir, job['key'], backend=name, harness_id=name,
                          timings={'submitted': plan['submitted_at']},
                          binding_revision=topic['binding_revision'], model=plan['model'], effort=plan['effort'],
                          model_settings_snapshot=True, **mode)

    def bound():
        # The binding decides it: the form is done, and a click on it again only repaints the
        # receipt. The receipt is written beside the task cards, not before them: first, it held them
        # 0.4 s (S35). A worktree's launch, off the click, still writes it first.
        timing.mark('bound')
        record.pop('pending', None)
        record.update(status='done', selection={
            'topic': plan['topic'], 'model_label': plan['label'], 'effort': plan['effort'] or '模型默认', **mode,
            'workspace': plan['workspace'], 'workdir': plan['source'] if plan.get('device') else str(directory), 'run_mode': plan['run_mode'], 'branch': branch,
            **({'redaction': plan['redaction']} if 'redaction' in plan else {}),
            **({'work_modes': True} if plan.get('work_modes') else {}),
        })
        save_record(path, record)
        after_ack(functools.partial(publish_receipt, app, ctx, path), beside=True)
    result = target_app.submission.start_workspace(target_app.models.configured_context(target), chat_id, plan['workspace'],
                                        directory=directory, on_bound=bound)
    if result:
        raise ValueError(result)
    remember_launch(ctx.state_dir, chat_id, {**plan, 'workdir': plan['source']}, form=path.stem, log=app.runtime.log)


def remember_launch(state_dir, chat_id, plan, *, form, log):
    """A launch's choices kept for the next one: the chat's usage (app/model_usage.py) and its last 开始任务
    (``launch_defaults``, and the Bridge's latest). The launch card's 开始任务 and the web page's launch (app/web_launch.py)
    both keep them here; a failure to save is logged, the launch is done already."""
    mode = {'work_mode': plan['work_mode']} if plan.get('work_mode') else {}
    try:
        model_usage.record(state_dir, chat_id, plan['model_key'], plan['preference_effort'], plan.get('work_mode') or '',
                           'launch', form=form)
    except OSError as exc:
        log(f'model usage save failed: {type(exc).__name__}; submission already completed')
    # Each is replaced whole: concurrent launches leave the latest, and nothing is read back first.
    defaults = {'model_choice': plan['model_key'], 'effort': plan['preference_effort'], 'workdir': plan['workdir'], **mode}
    for path, value in ((chat_defaults_path(state_dir, chat_id), {**defaults, 'chat_id': chat_id}),
                        (Path(state_dir) / LATEST_DEFAULTS, defaults)):
        try:
            save_record(path, value)
        except OSError as exc:
            log(f'launch preference save failed: {type(exc).__name__}; submission already completed')


def publish_preparing(app, ctx, path):
    """Replace the whole launch card with a note while its worktree is created."""
    try:
        with resource_lock(LAUNCH_FORM_LOCK, path):
            record = read_json(path)
            if record.get('status') != WORKTREE_STATUS or not record.get('card_id'):
                return  # Already started or failed: that card replaces this one.
            replace_form_card(ctx, path, record, dict(record), build_preparing_card(record['pending']),
                              app.messenger, 'launch-preparing')
    except Exception as exc:
        app.runtime.log(f'launch preparing card unavailable: {type(exc).__name__}')


def finish_worktree_launch(app, ctx, path):
    started = time.monotonic()
    created = None
    try:
        plan = read_json(path)['pending']
        directory, branch = add_topic_worktree(plan['worktree'])
        created = plan['worktree']
        with resource_lock(LAUNCH_FORM_LOCK, path), topic_lock(ctx):
            record = read_json(path)
            if record.get('status') != WORKTREE_STATUS:
                app.runtime.log(f'launch worktree created after its form closed status={record.get("status")}')
                return
            _start_topic(app, ctx, path, record, plan, directory, branch)
    except Exception as exc:  # noqa: BLE001 - every failure must give the card back
        app.runtime.log(f'launch worktree failed: {type(exc).__name__} elapsed_ms={(time.monotonic() - started) * 1000:.0f}')
        abandon_worktree_launch(app, ctx, path, str(exc) if isinstance(exc, ValueError) else
                                f'内部错误（{type(exc).__name__}）', created)
        return
    app.runtime.log(f'launch form submitted harness={plan["harness"]} run_mode=worktree '
            f'elapsed_ms={(time.monotonic() - started) * 1000:.0f}')
    publish_receipt(app, ctx, path)


def abandon_worktree_launch(app, ctx, path, reason, created=None):
    """Nothing was queued: give the topic a fresh launch card that keeps its choices."""
    kept = f'；已创建的 worktree 保留在 {created["dest"]}（分支 {created["branch"]}），可手动删除' if created else ''
    with resource_lock(LAUNCH_FORM_LOCK, path):
        with LOCK:
            record = read_json(path)
            if record.get('status') != WORKTREE_STATUS:
                return
            record['status'] = 'worktree_failed'
            save_record(path, record)
            if read_topic(ctx).get('launching') == path.stem:
                _mark_launching(ctx, '')
        restored = False
        try:
            # The workspace that was resolved, including a path typed into the form.
            draft = form_draft(record)
            draft['workspace'] = (record.get('pending') or {}).get('workspace') or draft.get('workspace')
            draft['expanded'] = True  # Unfolded: retrying likely means another run mode or workspace.
            restored = rebuild_launch_card(app.topics, ctx, path, record, draft, kind='launch-retry',
                                           notice=f'⚠️ 任务未启动：{reason}{kept}', harness_id=read_topic(ctx)['harness_id'],
                                           status=None, follow=False)
        except Exception as exc:  # noqa: BLE001
            app.runtime.log(f'launch card not restored: {type(exc).__name__}')
    app.messenger.reply(ctx, record['chat_id'],
        f'独立 worktree 未就绪，任务尚未启动：{reason}{kept}。'
        + ('配置卡已恢复，可重试或改用原目录。' if restored else '在本话题回复任意消息可重新显示配置卡。'),
        app.key('launch-worktree-failed', path.stem), voice=app.voice)


def recover_worktree_launches(topics, ctx):
    """A restart stopped these checkouts; none queued a task, so each topic gets its card back."""
    folder = ctx.state_dir / 'launch-forms'
    for path in sorted(folder.glob('*.json')) if folder.exists() else []:
        try:
            record = read_json(path)
            if record.get('status') != WORKTREE_STATUS:
                continue
            topic = load_topic(ctx.state_dir, record['topic_id'])
            app = topics.apps[topic['harness_id']]
            target = topic_target(app, record['chat_id'], topic)
            plan = record.get('pending') or {}
            left = plan.get('worktree') if Path(plan.get('worktree', {}).get('dest', '/nonexistent')).exists() else None
            abandon_worktree_launch(app, target, path, RESTART_ABANDONED, left)
        except Exception as exc:  # noqa: BLE001 - one record never blocks startup
            topics.runtime.log(f'launch worktree recovery failed: {type(exc).__name__}')

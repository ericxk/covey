"""One local form selection and one server submission for unified topics."""
from __future__ import annotations

import functools
import hashlib
import re
import secrets
import threading
import time
from pathlib import Path

from covey.core import main_thread_settings, timing
from covey.core.state import read_json
from covey.core.locks import LOCK, resource_lock, topic_lock
from covey.core.workspaces import RUN_MODES, add_topic_worktree, load_config, plan_topic_worktree, resolve_selection


from covey.core.redaction import TOPIC_KEY as REDACTION_KEY, default_on as redaction_default_on
from covey.cards.launch_config import PAGES, SUBMIT_PREFIX, launch_form_card
from covey.cards.launch_receipt import build_preparing_card
from covey.app.card_actions import form_fields
from covey.app.card_lanes import after_ack
from covey.app.form_cards import LAUNCH_FORM_LOCK, publish_form, replace_form_card, save_record
from covey.app.launch_receipt import publish_receipt
from covey.app.launch_models import available_models, catalog_changed, run_choice
from covey.core.menu_choice import budget_effort, chosen_work_mode
from covey.app.preselection import launch_selection
from covey.app import model_usage
from covey.app.topics import route_locked, topic_target
from covey.core.jobs import job_key, topic_job_records, transition_job_record, upsert_job_record
from covey.core.topics import load_topic, read_topic, topic_label, workspace_locked
from covey.core.statuses import WORKTREE_STATUS
from covey.feishu.transport import last_failure

# A waiting message whose launch card was not delivered (publish_launch_cards).
WAITING_STATUS = '等待选择工作区，尚未执行'  # app/submission.py
UNDELIVERED_STATUS = '启动卡未送达，尚未执行；在本话题再发一条消息会重新显示启动卡'
RUN_MODE_INVALID = '运行方式只能是原目录或独立 worktree'
# A group member's click on a control only the owner has (app/access.py), said on the card.
OWNER_ONLY_MANAGER = '⚠️ 仅机器人主人可以管理工作区列表'
OWNER_ONLY_DIRECTORY = '⚠️ 仅机器人主人可以选择其他目录'
OWNER_ONLY_PATH = '仅机器人主人可以填写其他目录，请选择已保存的工作区'
RESTART_ABANDONED = '服务重启时 worktree 尚未创建完成'


def card_token(card):
    """The form token of a launch card, from its 开始任务 button wherever it sits in the form."""
    def walk(elements):
        for element in elements:
            if str(element.get('name', '')).startswith(SUBMIT_PREFIX):
                yield element['name'][len(SUBMIT_PREFIX):]
            for column in element.get('columns', []):
                yield from walk(column.get('elements', []))
    form = next(e for e in card['body']['elements'] if e.get('tag') == 'form')
    return next(walk(form['elements']))


def selected_effort(record, value, *, allow_empty=False):
    if record.get('config_pending'):
        raise ValueError('模型配置正在更新，请等预算选项更新后再开始')
    if record.get('compact'):
        value = record.get('selected_effort', '')
        if not value and not allow_empty:
            raise ValueError('请选择当前模型的思考预算')
        return value
    if value in (None, ''):
        if allow_empty:
            return ''
        raise ValueError('请选择当前模型的思考预算')
    if not isinstance(value, str) or not value.startswith(str(record['config_revision']) + ':'):
        raise ValueError('预算选项已更新，请按当前模型重新选择预算')
    return value.split(':', 1)[1]


def form_draft(record):
    """What a launch form's card is rebuilt from: its recorded selects."""
    return {**record.get('form_defaults', {}), 'model': record['selected_model'],
            'effort': record.get('selected_effort', ''), 'work_mode': record.get('selected_work_mode', ''),
            'redaction': record.get('selected_redaction', '')}


# The last launch's model_choice, effort, work_mode (only when not the default) and workdir: one record
# per chat (with its chat_id), and launch-defaults.json for the most recent launch anywhere, which older
# releases read and write too.
LATEST_DEFAULTS = 'launch-defaults.json'
CHAT_DEFAULTS = 'launch-defaults'


def chat_defaults_path(state_dir, chat_id):
    return state_dir / CHAT_DEFAULTS / (hashlib.sha256(chat_id.encode()).hexdigest() + '.json')


def launch_defaults(state_dir, chat_id):
    """The chat's own last launch; a chat without one starts from the most recent launch anywhere."""
    try:
        saved = read_json(chat_defaults_path(state_dir, chat_id))
        if isinstance(saved, dict) and saved.get('chat_id') == chat_id:
            return saved
    except (OSError, ValueError):
        pass
    return read_json(state_dir / LATEST_DEFAULTS)


def topic_budget(state_dir):
    """(Harness, budget) of the 设置卡's 新话题默认 (core/main_thread_settings.py TopicDefaults), which the launch
    card's budget row preselects (app/preselection.py launch_selection); ('', '') where it says none or can't be
    read."""
    try:
        defaults = main_thread_settings.load(state_dir).topic_defaults
    except (OSError, ValueError):
        return '', ''
    return (defaults.harness.lower(), defaults.effort) if defaults.effort else ('', '')


def build_launch_card(topics, ctx, *, new_topic=False, draft=None, notice=''):
    if new_topic:
        raise ValueError('请在主聊天发送任务，在该消息下选择配置')
    if route_locked(ctx):
        raise ValueError('本话题已开始，配置保持不变；新任务请在主聊天发送消息')
    config = load_config(ctx.state_dir, ctx.default_workdir)
    topic = read_topic(ctx)
    chat_id = topic['chat_id']
    if draft is None:
        # A topic the main thread opened shows what it chose (app/main_topics.py launch_draft).
        draft = (topic.get('origin') or {}).get('launch') or None
    # The chat's recent launches and switches order the menu and give each model its usual budget.
    usage = model_usage.recent(ctx.state_dir, chat_id)
    fresh = ctx.options.get('binding_revision', 1) == 1
    try:
        saved = launch_defaults(ctx.state_dir, chat_id) if fresh else None
    except (OSError, ValueError):
        saved = None
    selection = launch_selection(available_models(topics, ctx), usage, config,
                                 model=ctx.options['harness_id'] + ':' + ctx.model, workspace=ctx.workspace_name,
                                 work_mode=ctx.work_mode, fresh=fresh, saved=saved, draft=draft, notice=notice,
                                 topic_budget=topic_budget(ctx.state_dir))
    draft = draft or {}
    # 敏感词脱敏 keeps the card's choice across rebuilds; a new card starts from the config's default.
    redaction = draft.get('redaction') if draft.get('redaction') in ('on', 'off') else (
        'on' if redaction_default_on(ctx.state_dir) else 'off')
    token = secrets.token_hex(12)
    run_mode = draft.get('run_mode') if draft.get('run_mode') in RUN_MODES else 'direct'
    page = draft.get('page') if draft.get('page') in PAGES else ''  # The row drawn open.
    # Budget, work mode, workspace and run mode are selects outside the form: each choice is
    # recorded when made, so a model change (a whole new card) can keep them.
    save_record(ctx.state_dir / 'launch-forms' / (token + '.json'), {
        'topic_id': ctx.topic_id, 'chat_id': chat_id,
        'binding_revision': ctx.options.get('binding_revision', 1),
        'harness_id': ctx.options['harness_id'], 'new_topic': False, 'status': 'ready',
        'schema_version': 3, 'compact': True, 'models': selection.choices, 'selected_model': selection.model,
        'config_revision': 1, 'selected_effort': selection.effort, 'selected_work_mode': selection.work_mode,
        'selected_redaction': redaction,
        'form_defaults': {'workspace': selection.workspace, 'path': draft.get('path') or '', 'run_mode': run_mode},
        **({'page': page} if page else {}),
    })
    return launch_form_card(token, model=selection.model, choices=selection.choices, effort=selection.effort,
                            workspace=selection.workspace, names=selection.names, run_mode=run_mode,
                            notice=selection.notice, work_mode=selection.work_mode, redaction=redaction,
                            topic_tag=topic_label(ctx.state_dir, ctx.topic_id), expanded=bool(draft.get('expanded')),
                            page=page)


def remember_launch_card(ctx, card, card_id, message_id, *, sequence=0):
    path = ctx.state_dir / 'launch-forms' / (card_token(card) + '.json')
    record = read_json(path)
    record.update(card_id=card_id, message_id=message_id, sequence=sequence)
    save_record(path, record)


def publish_launch_cards(topics, voice, ctx, chat_id, message_id, *, new_topic=False):
    card = build_launch_card(topics, ctx, new_topic=new_topic)
    token = card_token(card)
    path = ctx.state_dir / 'launch-forms' / (token + '.json')

    def send():
        timing.mark('build')
        return topics.messenger.create_and_send_card(ctx.profile, chat_id, card, voice.key('launch-card', message_id),
                                                     reply_to_message_id=ctx.reply_to_message_id, voice=voice)
    _, mid = publish_form(path, read_json(path), send, topic=(ctx.state_dir, ctx.topic_id, chat_id))
    if mid:
        timing.mark('saved')
        timing.outcome('launch_card')
        # The topic's earlier messages whose card failed can start from this one: they are no longer undelivered.
        for job in topic_job_records(ctx.state_dir, ctx.topic_id, chat_id=chat_id, status='waiting_workspace'):
            if job.get('launch_delivery_error'):
                transition_job_record(ctx.state_dir, job['key'], {'waiting_workspace'},
                                      status_message=WAITING_STATUS, launch_delivery_error='')
        return
    failure = last_failure()  # Read now: the notice below replaces it.
    error = ' '.join(filter(None, [failure.code or failure.category, failure.detail[:80]])) if failure else 'unknown'
    timing.outcome('launch_card_failed')
    told = topics.messenger.reply(ctx, chat_id, '配置卡暂未送达，请在本话题回复消息以重新显示；任务尚未启动。',
                                  voice.key('launch-delivery-failed', message_id), voice=voice)
    # The topic's next message gets its own launch card; until then the record says why nothing shows,
    # and maintenance closes it after UNDELIVERED_LAUNCH_DAYS (core/stale.py).
    job = transition_job_record(ctx.state_dir, job_key(message_id, ''), {'waiting_workspace'},
                                status_message=UNDELIVERED_STATUS, launch_delivery_error=error)
    topics.messenger.log(f'launch card not delivered form={token} job=#{(job or {}).get("job_id", "-")} '
                         f'error={error} notice={"sent" if told else "failed"}')


def rebuild_launch_card(topics, ctx, path, record, draft, *, kind, notice='', harness_id='',
                        status='replaced', follow=True, fail_closed=True):
    """Replace a launch form's whole card, in place, with a new form built from `draft`.

    Never one element inside the form: the Lark client then cannot submit it,
    with no callback and no error. The new form is registered on the same card
    before the PUT, so a click or a restart right after it already finds its
    record. Once CardKit confirms, the old record takes `status` (None keeps its
    own) and, with `follow`, names the new form, so a 开始任务 still sent from
    the old card starts it. Unconfirmed, a `fail_closed` new form is marked
    unpublished and never starts. Returns whether CardKit confirmed."""
    topic = load_topic(ctx.state_dir, record['topic_id'])
    app = topics.apps[harness_id or record['harness_id']]
    target = topic_target(app, record['chat_id'], topic)
    card = build_launch_card(topics, target, new_topic=record.get('new_topic', False), draft=draft, notice=notice)
    token = card_token(card)
    candidate = {**record, **({'status': status} if status else {}), **({'replaced_by': token} if follow else {})}
    # The card's lock from registration to confirmation: a click on the new form waits for this update.
    with resource_lock('card', record['card_id']):
        # Registered with the sequence the update below reserves, so its own next write follows it.
        remember_launch_card(target, card, record['card_id'], record['message_id'], sequence=record.get('sequence', 0) + 1)
        if replace_form_card(ctx, path, record, candidate, card, topics.messenger, kind):
            return True
    if fail_closed:
        replacement = path.parent / (token + '.json')
        save_record(replacement, {**read_json(replacement), 'status': 'unpublished'})
    return False


CLOSED = 'closed'  # Ended from the workspace manager without a task; never starts.


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
        workspace, source = resolve_selection(ctx.state_dir, ctx.default_workdir, selection)
        plan = {'harness': name, 'model': model, 'effort': effort, 'label': choice['label'], 'work_mode': work_mode,
                'model_key': model_key, 'preference_effort': preference_effort,
                'workspace': workspace, 'source': str(source), 'run_mode': run_mode,
                'chat_id': event['chat_id'], 'submitted_at': submitted_at,
                'topic': topic_label(ctx.state_dir, ctx.topic_id)}
        # Only a model whose card showed the select can turn it on; a form from before it has no choice. Such a
        # model's receipt says 开 or 关 (the owner, 2026-11-04 16:45: 连是否敏感都不知道), another's nothing.
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
    # A card sent before the path input went (2026-11-03) still submits one.
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
            'workspace': plan['workspace'], 'workdir': str(directory), 'run_mode': plan['run_mode'], 'branch': branch,
            **({'redaction': plan['redaction']} if 'redaction' in plan else {}),
            **({'work_modes': True} if plan.get('work_modes') else {}),
        })
        save_record(path, record)
        after_ack(functools.partial(publish_receipt, app, ctx, path), beside=True)
    result = target_app.submission.start_workspace(target_app.models.configured_context(target), chat_id, plan['workspace'],
                                        directory=directory, on_bound=bound)
    if result:
        raise ValueError(result)
    try:
        model_usage.record(ctx.state_dir, chat_id, plan['model_key'], plan['preference_effort'], plan.get('work_mode') or '',
                           'launch', form=path.stem)
    except OSError as exc:
        app.runtime.log(f'model usage save failed: {type(exc).__name__}; submission already completed')
    # Each is replaced whole: concurrent launches leave the latest, and nothing is read back first.
    defaults = {'model_choice': plan['model_key'], 'effort': plan['preference_effort'], 'workdir': plan['source'], **mode}
    for path, value in ((chat_defaults_path(ctx.state_dir, chat_id), {**defaults, 'chat_id': chat_id}),
                        (ctx.state_dir / LATEST_DEFAULTS, defaults)):
        try:
            save_record(path, value)
        except OSError as exc:
            app.runtime.log(f'launch preference save failed: {type(exc).__name__}; submission already completed')


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


def close_launch_form(ctx, path, record, text, reason):
    """End a launch form without a task; the caller holds the form's lock.

    Closed before its card changes, so this form's 开始任务 can never start anything.
    The topic's messages still waiting for a launch are cancelled."""
    with LOCK:
        record.update(status=CLOSED, closed_text=text)
        save_record(path, record)
        for job in topic_job_records(ctx.state_dir, record['topic_id'], chat_id=record['chat_id'],
                                     status='waiting_workspace'):
            upsert_job_record(ctx.state_dir, job['key'], status='cancelled', status_message=reason, never_started=True)


RETIRED_TEXT = '此启动卡已失效，请在主聊天重新发送消息开始任务。'


# compat(3bfb9be): a launch form published as two cards or before schema 3 only says it is retired;
# retire when no launch-forms record is one (compat_report: retired-launch-form)
def retired_form(record):
    """A form this release no longer starts: published as two cards, or before schema 3."""
    return record.get('schema_version') != 3 or bool(record.get('split_config'))


def retired_form_at(path, event):
    """The record of a retired form this click in its own chat names, else None. Retirement never
    changes, so this reads without the form's lock."""
    try:
        record = read_json(path)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and retired_form(record) and record.get('chat_id') == event.get('chat_id') else None


def refuse_on_launch_card(topics, ctx, path, record, notice):
    """A click on a ready launch card that only the owner may make: the card, rebuilt with its
    choices, says so; a 开始任务 from the old card follows it."""
    rebuild_launch_card(topics, ctx, path, record, form_draft(record), kind='owner-only', notice=notice)


def form_topic_root(ctx, record):
    """The root message of a launch form's topic, for a reply in its thread; '' when unknown."""
    try:
        topic = load_topic(ctx.state_dir, record['topic_id'])
        return topic['root_id']
    except (OSError, ValueError, KeyError):
        return ''


def refuse_retired_form(topics, ctx, record, event):
    """Answer a click on a retired form in its topic; the form and its waiting messages stay as they are."""
    topics.messenger.send_message(ctx.profile, record['chat_id'], RETIRED_TEXT,
                                   topics.voice.key('launch-retired', str(event.get('event_id') or event.get('message_id'))),
                                   reply_to_message_id=form_topic_root(ctx, record), voice=topics.voice)

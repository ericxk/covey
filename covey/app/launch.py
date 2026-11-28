"""The launch card of unified topics: its form, defaults and rebuilds, and closing or retiring a launch form (the submission is app/launch_submit.py)."""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from types import SimpleNamespace

from covey.core import devices, butler_settings, timing
from covey.core.state import read_json
from covey.core.locks import LOCK, resource_lock, topic_lock
from covey.core.workspaces import RUN_MODES, load_config, visible_workspaces


from covey.cards.launch_config import PAGES, SUBMIT_PREFIX, launch_form_card
from covey.app.form_cards import LAUNCH_FORM_LOCK, publish_form, replace_form_card, save_record
from covey.app.launch_models import available_models
from covey.app.preselection import launch_selection
from covey.app import model_usage
from covey.app.topics import route_locked, topic_target
from covey.core.jobs import job_key, topic_job_records, transition_job_record, upsert_job_record
from covey.core.topics import load_topic, read_topic, topic_label
from covey.feishu.transport import last_failure

# A waiting message whose launch card was not delivered (publish_launch_cards).
WAITING_STATUS = '等待选择工作区，尚未执行'  # app/submission.py
UNDELIVERED_STATUS = '启动卡未送达，尚未执行；在本话题再发一条消息会重新显示启动卡'
# A group member's click on a control only the owner has (app/access.py), said on the card.
OWNER_ONLY_MANAGER = '⚠️ 仅机器人主人可以管理工作区列表'
OWNER_ONLY_DIRECTORY = '⚠️ 仅机器人主人可以选择其他目录'


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
            'effort': record.get('selected_effort', ''), 'work_mode': record.get('selected_work_mode', '')}


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


def topic_defaults(state_dir):
    """The 设置 card's 新话题 page (core/butler_settings.py TopicDefaults), which the launch card preselects; its
    defaults where it can't be read."""
    try:
        return butler_settings.load(state_dir).topic_defaults
    except (OSError, ValueError):
        return butler_settings.TopicDefaults()


def topic_budget(state_dir):
    """(Harness, budget) of the 新话题 page (``topic_defaults``), which the launch card's budget row preselects
    (app/preselection.py launch_selection); ('', '') where it says none."""
    defaults = topic_defaults(state_dir)
    return (defaults.harness.lower(), defaults.effort) if defaults.effort else ('', '')


def device_titles(config):
    """name -> the device's title for each device workspace of ``config`` (empty unless devices are on): the launch
    card's device label."""
    found = {}
    for name, value in config['workspaces'].items():
        if devices.is_device_value(value):
            alias = value.partition(':')[0]
            try:
                found[name] = devices.current().device(alias).title
            except (devices.UnknownDevice, devices.DeviceConfigError):
                found[name] = alias
    return found


def device_harnesses(config):
    """name -> the Harnesses its device declares (devices.json ``harnesses``) for each device workspace of ``config``
    (empty unless devices are on): what a card preselects a model from (app/preselection.py device_fit)."""
    found = {}
    for name, value in config['workspaces'].items():
        if devices.is_device_value(value):
            try:
                found[name] = devices.current().device(value.partition(':')[0]).harnesses
            except (devices.UnknownDevice, devices.DeviceConfigError):
                continue
    return found


def alias_harnesses():
    """alias -> the Harnesses its device declares (devices.json ``harnesses``), for each device (empty unless COVEY_DEVICES=1
    with devices): what the 执行设备 row's card preselects a model from (app/preselection.py device_fit)."""
    if not devices.enabled():
        return {}
    try:
        return {alias: tuple(device.harnesses) for alias, device in devices.current().devices.items()}
    except (devices.DeviceConfigError, AttributeError):
        return {}


def legacy_place(config, draft, notice):
    """(workspace, names, device alias, notice) of a card rebuilt from an older card's choices (``draft['workspace']``: a
    butler's worktree launch, a card sent before the home start): the 工作区 row's as the card drew it, the workspace of a
    device the alias of that device (app/preselection.py device_fit)."""
    names = sorted(config['workspaces'], key=lambda name: -config.get('recent', {}).get(name, 0))
    workspace = draft['workspace']
    if workspace not in config['workspaces']:
        # Chosen on a card sent before its removal: it stays chosen, so 开始任务 reports it instead of starting
        # elsewhere, and the card, redrawn with it, says so.
        names = [*names, workspace]
        notice = '\n'.join(filter(None, [notice, f'⚠️ 工作区「{workspace}」已从列表移除，请换一个工作区。']))
    value = config['workspaces'].get(workspace, '')
    return workspace, names, value.partition(':')[0] if devices.is_device_value(value) else '', notice


def build_launch_card(topics, ctx, *, new_topic=False, draft=None, notice=''):
    if new_topic:
        raise ValueError('请在主聊天发送任务，在该消息下选择配置')
    if route_locked(ctx):
        raise ValueError('本话题已开始，配置保持不变；新任务请在主聊天发送消息')
    topic = read_topic(ctx)
    chat_id = topic['chat_id']
    if draft is None:
        # A topic the butler opened shows what it chose (app/butler_topics.py launch_draft).
        draft = (topic.get('origin') or {}).get('launch') or None
    draft = dict(draft or {})
    # No workspace to choose: a topic starts in the home. A draft that names one is an older card's (a butler's worktree
    # launch, a card sent before the home start): its card keeps the 工作区 and 运行方式 rows and 其他目录….
    legacy = isinstance(draft.get('workspace'), str) and bool(draft['workspace'])
    # No 执行设备 row and no device's workspace (主人 D2Y4T): a new topic starts here; it uses a device from the home
    # (``covey device``). An older card's device choice is refused by 开始任务 (app/launch_submit.py).
    place = None
    if legacy:
        config = visible_workspaces(load_config(ctx.state_dir, ctx.default_workdir), lambda alias: False)
        place = legacy_place(config, draft, notice)
        notice = place[3]
    if draft.get('device'):
        notice = notice or devices.START_RETIRED  # An older card's 执行设备 choice, redrawn without it: says why.
    draft['device'] = ''
    # The chat's recent launches and switches order the menu and give each model its usual budget.
    usage = model_usage.recent(ctx.state_dir, chat_id)
    defaults = topic_defaults(ctx.state_dir)
    fresh = ctx.options.get('binding_revision', 1) == 1
    try:
        saved = launch_defaults(ctx.state_dir, chat_id) if fresh else None
    except (OSError, ValueError):
        saved = None
    selection = launch_selection(available_models(topics, ctx), usage, device_harnesses=alias_harnesses(),
                                 model=ctx.options['harness_id'] + ':' + ctx.model, work_mode=ctx.work_mode, fresh=fresh,
                                 saved=saved, draft=draft, notice=notice, topic_budget=topic_budget(ctx.state_dir),
                                 topic_model=(defaults.harness.lower(), defaults.model))
    # 运行方式 keeps the card's choice across rebuilds; a new card starts from the 新话题 page's.
    token = secrets.token_hex(12)
    page = draft.get('page') if draft.get('page') in PAGES else ''  # The row drawn open.
    run_mode = draft.get('run_mode') if draft.get('run_mode') in RUN_MODES else (
        defaults.run_mode if defaults.run_mode in RUN_MODES else 'direct')
    # Budget, work mode, device (and an older card's workspace and run mode) are rows outside the form: each choice is
    # recorded when made, so a model change (a whole new card) can keep them.
    save_record(ctx.state_dir / 'launch-forms' / (token + '.json'), {
        'topic_id': ctx.topic_id, 'chat_id': chat_id,
        'binding_revision': ctx.options.get('binding_revision', 1),
        'harness_id': ctx.options['harness_id'], 'new_topic': False, 'status': 'ready',
        'schema_version': 3, 'compact': True, 'models': selection.choices, 'selected_model': selection.model,
        'config_revision': 1, 'selected_effort': selection.effort, 'selected_work_mode': selection.work_mode,
        'form_defaults': ({'workspace': place[0], 'path': draft.get('path') or '', 'run_mode': run_mode} if legacy
                          else {'device': selection.device}),
        **({'page': page} if page else {}),
    })
    config = load_config(ctx.state_dir, ctx.default_workdir) if legacy else {'workspaces': {}}
    return launch_form_card(token, model=selection.model, choices=selection.choices, effort=selection.effort,
                            **({'workspace': place[0], 'names': place[1], 'run_mode': run_mode} if legacy else {}),
                            notice=selection.notice, work_mode=selection.work_mode,
                            topic_tag=topic_label(ctx.state_dir, ctx.topic_id), expanded=bool(draft.get('expanded')),
                            page=page, devices=device_titles(config), device=selection.device)


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


def close_open_launches(ctx, chat_id, messenger, text, title='已取消'):
    """Close the launch cards of ``ctx``'s topic in ``chat_id`` that can still start it (OPEN_FORMS) as a stop of the topic:
    the form is closed and its waiting messages cancelled first (``close_launch_form``, so 开始任务 can never start them),
    then the card says ``text`` instead of the form; how many were closed. Its lock order is a 开始任务 click's (the form's,
    then the topic's), as the stale closer's (app/stale.py). Otherwise a topic stopped before it started has no task to stop (请 0 个任务停下),
    and its launch card keeps it under 等我 and the web page's 待办."""
    from covey.cards.elements import notice_card
    from covey.core.statuses import OPEN_FORMS
    from covey.core.store import directory_store

    closed = 0
    state = Path(ctx.state_dir)
    rows = directory_store(state / 'launch-forms').query(topic_id=ctx.topic_id, chat_id=chat_id)
    # The topic's lock by its history's folder, as the stale closer takes it for a topic without its context.
    topic = SimpleNamespace(history_path=state / 'topics' / str(ctx.topic_id) / 'history.jsonl')
    for path, row in sorted(rows, key=lambda pair: str(pair[0])):
        if row.get('status') not in OPEN_FORMS:
            continue
        with resource_lock(LAUNCH_FORM_LOCK, path), topic_lock(topic):
            record = read_json(path)
            if record.get('status') not in OPEN_FORMS:
                continue  # Started or closed meanwhile.
            close_launch_form(ctx, path, record, text, text)
            if record.get('card_id') and messenger is not None:
                replace_form_card(ctx, path, record, record, notice_card(text, title), messenger, 'stop-launch')
        closed += 1
    return closed


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


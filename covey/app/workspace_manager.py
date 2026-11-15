"""Manage the saved workspace list.

The bot menu's 工作区 sent the owner's private chat a standalone card that
belongs to no topic and leaves no job, until 2026-11-09 (one sent before still acts). The launch card's workspace menu
replaces the whole launch card with the same view; 返回启动卡 rebuilds it with
the choices it had (a workspace added there becomes the choice), 结束，不启动任务
closes that launch. ＋ 添加工作区… turns the card into the directory browser of
app/directory_picker.py: 添加 lists a directory and stays there, 返回管理页 comes back. Removing
an entry never deletes its directory, and topics already bound to it keep running
there: a topic stores its own path when it starts. The card itself is cards/workspace_manager.py.

The 设置 card's 工作区 tab (app/settings_hub.py) is the same manager on the 设置 card's record
(<state>/settings-cards/<token>.json, its view in ``view`` with ``hub``; changed under that record's one lock and only
while the card shows this tab): the tab row on top, no 完成管理, and with the main thread's settings each row's 用途
and 记住的工作区 under the list (their buttons are app/main_thread_panel.py's, which draws this tab through
``tab_card``). The bot menu's 工作区 key (MENU_KEY) opens the 设置 card on this tab since 2026-11-09; a standalone card
sent before still acts.
"""
from __future__ import annotations

import os
import re
from collections import Counter

from covey.app.card_queue import CardActionQueue
from covey.app.form_cards import LAUNCH_FORM_LOCK, replace_form_card, save_record
from covey.app.launch import (CLOSED, OWNER_ONLY_MANAGER, close_launch_form, rebuild_launch_card,
                                       refuse_on_launch_card)
from covey.app.directory_picker import (DeviceGate, DirectoryCache, listed_paths, read_into, step, take_listing,
                                                 when_listed)
from covey.app.menu_panel import FOLDED, HUB_FOLDER, OPEN, MenuPanel, hub_top
from covey.cards.directory import directory_card
from covey.core.text import literal
from covey.cards.main_thread_panel import WORKSPACE_NOTE, notes_button, trusted_section
from covey.cards.settings_hub import WORKSPACE
from covey.cards.launch_config import MANAGE_OPTION, WORKSPACE_ACTION
from covey.cards.elements import note
from covey.cards.workspace_manager import (BROWSE_HOST, BROWSE_VERBS, FILTERS, MANAGER_PREFIX, ROW_VERBS,
                                                   closed_card, manager_card, markdown_lines, row_key)
from covey.core import devices, main_thread, main_thread_settings
from covey.core.workspaces import (add_workspace, directory_status, load_config, missing_workspaces,
                                           remove_workspaces, set_default_workspace)
from covey.app.card_actions import action_value, form_fields
from covey.core.state import read_json
from covey.core.statuses import MANAGING  # A launch card that became the manager (a standalone one: OPEN).
from covey.core.topics import load_topic

CHANGES = ('add', 'remove', 'prune', 'default')  # add: the absolute-path row of cards sent before the browser.
END_STATUS = '用户结束工作区管理，未启动任务'
MENU_KEY = 'manage_workspaces'  # The bot menu's 工作区 before 2026-11-09: the 设置 card on this tab now.


def is_hub(path):
    """Whether the record at ``path`` is a 设置 card's (its view in ``view``), not a manager card's own."""
    return path.parent.name == HUB_FOLDER


def view_field(path):
    return 'view' if is_hub(path) else 'manager'


def start_directory(config):
    """Where 添加工作区 starts browsing: the one parent most saved workspaces share, else the default's."""
    parents = Counter(os.path.dirname(os.path.normpath(path)) for path in config['workspaces'].values())
    most = max(parents.values())
    shared = [parent for parent, count in parents.items() if count == most]
    if most > 1 and len(shared) == 1:
        return shared[0]
    return os.path.dirname(os.path.normpath(config['workspaces'][config['default']]))


class WorkspaceManager(CardActionQueue):
    def __init__(self, topics, cache=None):
        super().__init__(topics, name='workspace-manager', workers=2, label='workspace manager')
        self.checked = {}  # The latest directory answers: the processing card needs no NAS wait.
        self.cache = cache or DirectoryCache()  # The launch card's: its readers bound both.
        self.menu = MenuPanel(topics, 'workspace-manager', 'workspace-managers', LAUNCH_FORM_LOCK, 'manager',
                              tab=WORKSPACE)

    action = staticmethod(action_value)

    def matches(self, event):
        name = event.get('action_name')
        if isinstance(name, str) and name.startswith(MANAGER_PREFIX):
            return True
        return self.action(event).get('kind') == WORKSPACE_ACTION and event.get('option') == MANAGE_OPTION

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is the manager's, else None (app/unified.py EARLY); the
        processing refuses anyone but the owner on the card."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return 'workspace_manager' if owner else 'owner_only'

    def dispatch(self, ctx, event, handler=None):
        # Directory checks can wait on the NAS; message and card ingestion stay free.
        self.submit('request dropped', handler or self.process, ctx, event)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    # The 设置 card's 工作区 tab (app/settings_hub.py): its first view, its record's fields, its card, once shown.
    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record, fresh=True):
        """The tab for the 设置 card's ``record``: the list, or the directory browser it shows."""
        view = record['view']
        config = load_config(ctx.state_dir, ctx.default_workdir)
        top = hub_top(WORKSPACE, token, view)
        if view.get('browse'):
            return directory_card(token, {**view['browse'], 'revision': view['revision']}, BROWSE_HOST,
                                  listed_paths(config, view['browse'].get('device', '')), top=top)
        notes, after = None, ()
        if main_thread.available():
            settings = main_thread_settings.load(ctx.state_dir)

            def value(kind, **more):
                return {'kind': kind, 'token': token, 'revision': view['revision'], **more}

            notes = dict(settings.workspace_notes), lambda names: notes_button(value, names)
            after = [*trusted_section(settings, value), note(WORKSPACE_NOTE)]
        return manager_card(token, view, config, self.status(config, fresh), standalone=True, hub=True, top=top,
                            notes=notes, after=after, device_lines=self.device_lines(ctx, record.get('chat_id', '')))

    def device_lines(self, ctx, chat_id):
        """A line on each device this chat may use: its title and alias, and what its last self-check said (read from
        ``profile.json``: no call to the device while a card is drawn). Nothing when devices are off."""
        access = self.topics.runtime.access
        try:
            listed = devices.current().devices.items()
        except devices.DeviceConfigError:
            return []
        return [f'设备 {device.title}（{alias}）：{devices.status_text(ctx.state_dir, alias)}'
                for alias, device in listed if access.allows_device(chat_id, alias)]

    def tab_shown(self, ctx, token):
        self.topics.runtime.log(f'workspace manager shown on the settings card token={token[:8]}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY_MANAGER on the 设置 card ``event`` clicked while it shows this tab; whether it did."""
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)
            if record is None or not is_hub(path):
                return False
            view = {**record['view'], 'notice': OWNER_ONLY_MANAGER, 'revision': record['view']['revision'] + 1}
            self._publish(ctx, path, record, view, self.tab_card(ctx, path.stem, {**record, 'view': view}, False))
            return True

    def status(self, config, fresh=True):
        paths = list(dict.fromkeys(config['workspaces'].values()))
        if fresh:
            self.checked.update(directory_status(paths))
        return {path: self.checked.get(path) for path in paths}

    def process(self, ctx, event):
        access = self.topics.runtime.access
        if not access.allows(event.get('operator_id', ''), event.get('chat_id', '')):
            return
        owner = access.is_owner(event.get('operator_id', ''))
        click = self._click(event)
        if click is None:
            return
        token, revision, verb, arg = click
        path = self.menu.path(ctx, token)
        standalone = verb != 'open' and path.exists()
        if not standalone:
            path = ctx.state_dir / 'launch-forms' / (token + '.json')
        hub, field = is_hub(path), view_field(path)
        with self.menu.lock(path):
            try:
                record = read_json(path)
                topic = None if standalone else load_topic(ctx.state_dir, record['topic_id'])
            except (OSError, ValueError, KeyError):
                return
            if not self._card_matches(ctx, event, record, topic, standalone):
                return
            if hub and (not self.menu.on_tab(record) or record.get(FOLDED)):
                return  # The 设置 card shows another tab now, or is folded: a stale click.
            if not owner:
                self._refuse(ctx, path, record, standalone, verb)
                return
            if verb == 'open':
                self._open(ctx, path, record, token, topic)
                return
            view = dict(record.get(field) or {})
            if not self._current(ctx, path, record, view, standalone, verb, revision):
                return
            fields = form_fields(event)
            if fields is None:
                return
            if verb.startswith(BROWSE_VERBS) or view.get('browse'):
                self._browse_click(ctx, path, record, view, standalone, verb, arg, fields, topic, event)
                return
            self._act(ctx, path, record, view, standalone, hub, verb, arg, fields, topic, event)

    def _current(self, ctx, path, record, view, standalone, verb, revision):
        """Whether a click acts on the card as it is now: open (a launch card's: managing) at ``revision``
        (刷新 at any). On a closed card 刷新, or a click at its revision, draws the closed card again."""
        if record.get('status') == CLOSED:
            if verb == 'refresh' or revision == view.get('revision'):
                self._publish(ctx, path, record, view, closed_card(record.get('closed_text') or '**工作区管理已结束**'))
            return False
        if record.get('status') != (OPEN if standalone else MANAGING) or not isinstance(view.get('revision'), int):
            return False
        if verb not in ('refresh', BROWSE_VERBS + 'refresh') and view['revision'] != revision:
            return False  # A repeated or stale click.
        return True

    def _browse_click(self, ctx, path, record, view, standalone, verb, arg, fields, topic, event):
        """A click while the directory browser is open, or on it: the browser's own (its search box is not
        the list's), or 刷新 of the card."""
        if verb.startswith(BROWSE_VERBS) and view.get('browse'):
            self._browse(ctx, path, record, view, verb[len(BROWSE_VERBS):], arg, fields, topic, event)
        elif verb == 'refresh':
            view.update(revision=view['revision'] + 1)
            self._render(ctx, path, record, view, standalone, fresh=False)

    @staticmethod
    def _take_search(view, fields):
        """The search, filter and typed path the form shows into ``view`` (every button submits the whole
        form: the search shown is the search in use); whether the search or the filter changed."""
        search = view.get('query', ''), view.get('filter', 'all')
        query = fields.get('query', view.get('query', ''))
        if isinstance(query, str) and len(query) <= 100:
            view['query'] = query.strip()
        typed = fields.get('add_path', view.get('add_path', ''))
        if isinstance(typed, str) and len(typed) <= 1000:
            view['add_path'] = typed.strip()
        if fields.get('filter') in FILTERS:
            view['filter'] = fields['filter']
        return (view.get('query', ''), view.get('filter', 'all')) != search

    def _act(self, ctx, path, record, view, standalone, hub, verb, arg, fields, topic, event):
        """A click on the list: redrawn (刷新, 筛选, a page), a change, closed, back to the launch card, or the
        directory browser opened."""
        moved = self._take_search(view, fields)
        if moved or verb == 'filter':
            view['page'] = 0  # A new search starts at its first page.
        if verb in ('refresh', 'filter', 'page'):
            if verb == 'page' and not moved:
                view['page'] = arg
            view.update(revision=view['revision'] + 1, processing=False,
                        **({'notice': '', 'details': ''} if verb != 'refresh' else {}))
            self._render(ctx, path, record, view, standalone, fresh=verb != 'page')
        elif verb in CHANGES:
            self._change(ctx, path, record, view, verb, fields, topic, arg)
        elif verb == ('done' if standalone else 'end') and not hub:
            self._close(ctx, path, record, view, topic)
        elif verb == 'back' and not standalone:
            self._return_form(ctx, path, record, topic)
        elif verb == 'browse':
            view.update(notice='', details='', processing=False)
            self._browse(ctx, path, record, view, 'open', None, fields, topic, event)

    def _refuse(self, ctx, path, record, standalone, verb):
        """A group member's click: the launch card, or the manager it became, says only the owner
        manages the list. The caller holds the card's lock."""
        if verb == 'open':
            if record.get('status') == 'ready':
                refuse_on_launch_card(self.topics, ctx, path, record, OWNER_ONLY_MANAGER)
            return
        view = dict(record.get(view_field(path)) or {})
        if record.get('status') == (OPEN if standalone else MANAGING) and isinstance(view.get('revision'), int):
            if view.get('browse'):
                view['browse'] = {**view['browse'], 'notice': OWNER_ONLY_MANAGER}
            view.update(revision=view['revision'] + 1, notice=OWNER_ONLY_MANAGER, details='', processing=False)
            self._render(ctx, path, record, view, standalone, fresh=False)

    def _click(self, event):
        """(token, revision, verb, arg) of a click on a manager card, or of 管理工作区 on a launch card
        (verb 'open', no revision); None for anything else. The browser's verbs (dir-…) and page carry
        a number, the row or page a button stands for; a row's remove- and default- the workspace's
        row_key. Without a key, remove and default are a card's sent before the rows (2026-11-03): its
        selects name the workspaces."""
        name = event.get('action_name') or ''
        arg = None
        if isinstance(name, str) and name.startswith(MANAGER_PREFIX):
            match = re.fullmatch(r'bridge_wsm_([0-9a-f]{24}):(\d+):((?:dir-)?[a-z]+)(\d{0,5})(?:-([0-9a-f]{12}))?', name)
            if not match or (match[4] and not (match[3].startswith(BROWSE_VERBS) or match[3] == 'page')) or (
                    match[3] == 'page' and not match[4]) or (match[5] and match[3] not in ROW_VERBS):
                return None
            token, revision, verb = match[1], int(match[2]), match[3]
            arg = int(match[4]) if match[4] else match[5]
            if verb == 'open':
                return None
        else:
            token, revision, verb = self.action(event).get('token', ''), None, 'open'
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token):
            return None
        return token, revision, verb, arg

    @staticmethod
    def _card_matches(ctx, event, record, topic, standalone):
        """Whether the clicked card is this record's, and a launch card may still manage workspaces:
        its topic not bound to a workspace, Harness or session yet."""
        if (not record.get('card_id') or record.get('message_id') != event.get('message_id')
                or record.get('chat_id') != event.get('chat_id')):
            return False
        if standalone and record.get('schema_version') != 1:
            return False
        if not standalone and (
                record.get('schema_version') != 3 or not record.get('compact') or record.get('new_topic')
                or record.get('picker') or topic.get('chat_id') != event.get('chat_id')
                or record.get('harness_id') != topic.get('harness_id')
                or record.get('binding_revision') != topic.get('binding_revision', 1)
                or topic.get('workspace_locked') or topic.get('harness_locked')
                or any((ctx.state_dir / 'topics' / record['topic_id']).glob('*_session_id'))):
            return False
        return True

    def _open(self, ctx, path, record, token, topic):
        """管理工作区 on a ready launch card: the card becomes the manager."""
        if record.get('status') != 'ready':
            return
        view = {'revision': 1}
        config = load_config(ctx.state_dir, ctx.default_workdir)
        if not replace_form_card(ctx, path, record, {**record, 'status': MANAGING, 'manager': view},
                                 manager_card(token, view, config, self.status(config), standalone=False,
                                              device_lines=self.device_lines(ctx, record.get('chat_id', ''))),
                                 self.topics.messenger, 'workspace-manager'):
            self.topics.messenger.send_message(ctx.profile, record['chat_id'],
                '工作区管理未能打开；启动卡未改动，可重新选择“添加或管理工作区…”。',
                self.topics.voice.key('workspace-manager-failed', token + '-' + str(record.get('sequence', 0))),
                reply_to_message_id=topic['root_id'], voice=self.topics.voice)

    def _publish(self, ctx, path, record, view, card):
        return self.menu.publish(ctx, path, record, view, card)  # The state moves on regardless; 刷新 shows it.

    def _render(self, ctx, path, record, view, standalone, fresh=True):
        if is_hub(path):
            return self._publish(ctx, path, record, view, self.tab_card(ctx, path.stem, {**record, 'view': view}, fresh))
        config = load_config(ctx.state_dir, ctx.default_workdir)
        if view.get('browse'):
            card = directory_card(path.stem, {**view['browse'], 'revision': view['revision']}, BROWSE_HOST,
                                  listed_paths(config, view['browse'].get('device', '')))
        else:
            card = manager_card(path.stem, view, config, self.status(config, fresh), standalone=standalone,
                                device_lines=self.device_lines(ctx, record.get('chat_id', '')))
        return self._publish(ctx, path, record, view, card)

    def _browse(self, ctx, path, record, view, verb, arg, fields, topic, event):
        """A click of ＋ 添加工作区… ('open') or on the browser it shows. Moving around is published only
        when CardKit confirms; an addition is kept regardless, like the list's other changes."""
        standalone = topic is None
        gate = DeviceGate(self.topics.runtime.access, record.get('chat_id', ''))
        if verb == 'open':
            browse, outcome = {'path': start_directory(load_config(ctx.state_dir, ctx.default_workdir)),
                               'query': '', 'adds': 0, **({'offer': True} if gate.offers() else {})}, 'read'
        else:
            browse = dict(view['browse'])
            if verb == 'loaded':
                if not take_listing(browse, event):
                    return
                outcome = 'show'
            else:
                if verb == 'search':
                    arg = fields.get('query', '')
                outcome = step(browse, verb, arg, allow=gate)
            if outcome is None:
                return
        if outcome == 'back':
            adds = browse.get('adds', 0)
            view.pop('browse')
            view.update(revision=view['revision'] + 1, notice=f'✓ 本次添加 {adds} 项' if adds else '', details='')
            self._render(ctx, path, record, view, standalone)
            return
        added = False
        if outcome[0] == 'choose':
            try:
                name, added = add_workspace(ctx.state_dir, ctx.default_workdir, outcome[1])
                browse['notice'] = (f'✓ 已添加 {literal(name)}' if added else f'该目录已在列表中：{literal(name)}')
                view.update(added=name, adds=view.get('adds', 0) + added)
                browse['adds'] = browse.get('adds', 0) + added
            except ValueError as exc:
                browse['notice'] = '⚠️ ' + literal(exc)
            outcome = 'show'
        view = {**view, 'browse': browse, 'revision': view['revision'] + 1}
        future = (read_into(self.cache, browse, refresh=outcome == 'refresh', state_dir=ctx.state_dir)
                  if outcome != 'show' else None)
        config = load_config(ctx.state_dir, ctx.default_workdir)
        field = view_field(path)
        card = directory_card(path.stem, {**browse, 'revision': view['revision']}, BROWSE_HOST,
                              listed_paths(config, browse.get('device', '')),
                              top=hub_top(WORKSPACE, path.stem, view) if is_hub(path) else ())
        if not replace_form_card(ctx, path, record, {**record, field: view}, card, self.topics.messenger,
                                 'workspace-manager'):
            if added:
                save_record(path, {**record, field: view})
                self._done_message(ctx, record, topic, view, f'已添加工作区 {name}')
            return
        if future is not None:
            revision = view['revision']
            when_listed(future, lambda listing: self.dispatch(ctx, {
                **event, 'form_value': {}, '_listing': listing,
                'action_name': f'{MANAGER_PREFIX}{path.stem}:{revision}:{BROWSE_VERBS}loaded'}))

    def _done_message(self, ctx, record, topic, view, summary, tag='workspace-manager-done'):
        """The change is applied but the card still shows the previous view: say it succeeded."""
        self.topics.messenger.send_message(ctx.profile, record['chat_id'], f'操作已完成：{summary}。卡片未能刷新，请点卡片上的“刷新”。',
            self.topics.voice.key(tag, f"{record.get('card_id', '')}-{view['revision']}"),
            reply_to_message_id='' if topic is None else topic['root_id'], voice=self.topics.voice)  # Standalone: the main chat.

    def _change(self, ctx, path, record, view, verb, fields, topic, key=None):
        """``key``: the row_key of the row whose button it was; None, the selects of an older card."""
        state, fallback, standalone = ctx.state_dir, ctx.default_workdir, topic is None
        config = load_config(state, fallback)
        if key is None:
            selected, choice = fields.get('names'), fields.get('default')
            selected = [selected] if isinstance(selected, str) else selected or []
            if not isinstance(selected, list) or not all(isinstance(n, str) for n in selected):
                return
        else:
            selected = [name for name in config['workspaces'] if row_key(name) == key][:1]
            choice = selected[0] if selected else None
        # A click that cannot change anything is answered at once.
        refusal = ('请先填写要添加的目录的绝对路径。' if verb == 'add' and not view.get('add_path') else
                   '该工作区已不在列表中。' if key is not None and not selected else
                   '请先在下拉框中选择要移除的工作区。' if verb == 'remove' and not selected else
                   '请选择要设为默认的工作区。' if verb == 'default' and (
                       not isinstance(choice, str) or choice not in config['workspaces']) else
                   f'{literal(choice)} 已是默认工作区。' if verb == 'default' and choice == config['default'] else '')
        if refusal:
            view.update(revision=view['revision'] + 1, notice=refusal, details='', processing=False)
            self._render(ctx, path, record, view, standalone)
            return
        view.update(revision=view['revision'] + 1, processing=True)
        self._render(ctx, path, record, view, standalone, fresh=False)
        notice, details, removed, summary = '', '', [], ''
        try:
            if verb == 'add':
                name, added = add_workspace(state, fallback, view['add_path'])
                shown = literal(load_config(state, fallback)['workspaces'][name])
                notice = (f'✓ 已添加工作区 {literal(name)}' if added else f'该目录已在列表中：{literal(name)}'
                          ) + f"\n<font color='grey'>{shown}</font>"
                summary = f'已添加工作区 {name}' if added else ''
                view.update(add_path='', added=name, adds=view.get('adds', 0) + added)
            elif verb == 'default':
                set_default_workspace(state, fallback, choice)
                notice, summary = f'✓ 已把 {literal(choice)} 设为默认工作区', f'已把 {choice} 设为默认工作区'
            else:
                config = load_config(state, fallback)
                removed = remove_workspaces(state, fallback, selected if verb == 'remove' else missing_workspaces(config))
                if removed:
                    notice = f'✓ 已从列表移除 {len(removed)} 项，磁盘目录未删除'
                    summary = f'已从列表移除 {len(removed)} 项，磁盘目录未删除'
                    details = markdown_lines([f"**{literal(n)}** · <font color='grey'>{literal(config['workspaces'][n])}</font>"
                                              for n in removed], 3000)
                else:
                    notice = '所选工作区已不在列表中。' if verb == 'remove' else '没有目录不存在的工作区。'
        except ValueError as exc:
            notice = '⚠️ ' + literal(exc)
        view.update(revision=view['revision'] + 1, processing=False, notice=notice, details=details,
                    removed=view.get('removed', 0) + len(removed))
        if not (self._render(ctx, path, record, view, standalone)
                or self._render(ctx, path, record, view, standalone, fresh=False)) and summary:
            self._done_message(ctx, record, topic, view, summary)

    def _close(self, ctx, path, record, view, topic):
        config = load_config(ctx.state_dir, ctx.default_workdir)
        count = (f"本次添加 {view['adds']} 项、移除 {view.get('removed', 0)} 项" if view.get('adds')
                 else f"本次移除 {view.get('removed', 0)} 项")
        changes = f"{count} · 默认工作区：{literal(config['default'])}"
        if topic is None:
            text = '**工作区管理已完成** · ' + changes
            summary = f"工作区管理已完成，{count}"
            record.update(status=CLOSED, closed_text=text)
            save_record(path, record)
        else:
            text = '**已结束** · 本话题未启动任务；之后在主聊天发送新消息开始任务\n' + changes
            summary = f"已结束，本话题未启动任务，{count}"
            close_launch_form(ctx, path, record, text, END_STATUS)
        card = closed_card(text)
        if not (self._publish(ctx, path, record, view, card) or self._publish(ctx, path, record, view, card)):
            self._done_message(ctx, record, topic, view, summary, 'workspace-manager-closed')

    def _return_form(self, ctx, path, record, topic):
        draft = {**record.get('form_defaults', {}), 'model': record['selected_model'],
                 'effort': record.get('selected_effort', ''), 'redaction': record.get('selected_redaction', '')}
        config = load_config(ctx.state_dir, ctx.default_workdir)
        added = (record.get('manager') or {}).get('added')
        if added in config['workspaces']:  # Added here to be used: the latest one becomes the choice.
            draft.update(workspace=added, path='')
            notice = '已选择工作区：' + literal(added)
        else:
            notice = ('原先选择的工作区已移除，请确认工作区。' if draft.get('workspace') not in config['workspaces'] else '')
            if notice:  # Removed here: the card falls back to the default (a stale card's choice would stay).
                draft.pop('workspace', None)
        rebuild_launch_card(self.topics, ctx, path, record, draft, kind='workspace-manager', notice=notice)

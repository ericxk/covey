"""Bounded directory browsing: one listing or name search at a time, never on the event threads.

The navigation here (``step``, ``read_into``) and the card in cards/directory.py serve two hosts: the
launch card's 选择其他目录 (``DirectoryPicker`` below, the view in a launch form's ``picker``) and the
workspace manager's ＋ 添加工作区… (app/workspace_manager.py, the view in ``manager.browse``).

A view with a ``device`` (an alias of devices.json) browses that device: the listing and the name search are done by
the device (core/devices.py ``list_dirs``, ``search_dirs``), ``~`` is the device's home (never expanded here), the cache
key carries the device, and a directory chosen is ``alias:path``. An owner gets there by typing ``alias:path`` in the
search box, where the chat's ``spaces`` allow it (``DeviceGate``); the hub looks at no device path itself."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import posixpath
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError

from covey.app.card_queue import CardActionQueue
from covey.app.form_cards import LAUNCH_FORM_LOCK, replace_form_card
from covey.app.launch import (OWNER_ONLY_DIRECTORY, rebuild_launch_card, refuse_on_launch_card,
                                       refuse_retired_form, retired_form, retired_form_at, selected_effort)
from covey.cards.launch_config import PICKER_PREFIX
from covey.cards.directory import DIRECTORY_FORM_PREFIX, directory_card, visible_entries
from covey.core import devices
from covey.core.text import literal
from covey.app.menu_panel import valid_token
from covey.core.locks import resource_lock
from covey.core.workspaces import add_workspace
from covey.app.card_actions import action_value, card_object, form_fields
from covey.core.workspaces import load_config
from covey.core.state import read_json
from covey.core.topics import load_topic

SCAN_LIMIT = 4000
FAST_SCAN_SECONDS = .15
# A name search reads at most this many directories, this many levels down, in this long.
SEARCH_DEPTH, SEARCH_DIRECTORIES, SEARCH_SECONDS, SEARCH_RESULTS = 3, 5000, 6, 200
# A longer search shows 正在搜索… first; its result replaces the card when it is done.
SEARCH_WAIT = 1.8
SKIPPED = frozenset({'node_modules', '__pycache__'})
RETRY = '可点“刷新”重新读取'


def _read_error(exc):
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return '路径不存在或不是目录，请返回上一级。'
    if isinstance(exc, PermissionError):
        return '没有权限读取此目录，请返回上一级。'
    return f'读取失败，{RETRY}，或返回上一级。'


def device_error(code, device):
    """The card's words for a device listing that failed (core/devices.py ``list_dirs`` codes)."""
    if code in ('not_found', 'not_dir'):
        return _read_error(FileNotFoundError())
    if code == 'permission':
        return _read_error(PermissionError())
    if code == 'offline':
        return f'设备 {device} 没有答复，{RETRY}，或返回上一级。'
    return f'读取失败，{RETRY}，或返回上一级。'


def read_device(device, path, query, state_dir):
    """A listing (``query`` empty) or a name search of ``path`` on ``device``, in the shape of scan_directory's and
    search_directories's, plus the ``path`` the device resolved (``~`` expanded there)."""
    if state_dir is None:
        return {'entries': [], 'error': device_error('other', device)}
    found = (devices.search_dirs(state_dir, device, path, query) if query
             else devices.list_dirs(state_dir, device, path))
    if found.get('code'):
        return {'entries': [], 'error': device_error(found['code'], device)}
    return {**{k: found[k] for k in ('entries', 'truncated', 'more', 'scanned') if k in found},
            'path': found.get('path', path), 'error': ''}


class DeviceGate:
    """Which devices a chat's browser may go to: ``gate(alias)`` is '' when allowed, else the reason (an alias
    devices.json does not list, or one the chat's ``spaces`` refuse: app/access.py ``allows_device``)."""

    def __init__(self, access, chat_id):
        self.access, self.chat_id = access, chat_id

    def __call__(self, alias):
        try:
            devices.current().device(alias)
        except devices.UnknownDevice:
            return f'设备 {alias} 不在 devices.json 里'
        if not self.access.allows_device(self.chat_id, alias):
            return f'设备 {alias} 不允许在这个聊天里使用（devices.json 的 spaces）'
        return ''

    def offers(self):
        """Whether this chat may browse some device: the browser then says so in its search box."""
        try:
            return any(not self(alias) for alias in devices.current().devices)
        except devices.DeviceConfigError:
            return False


def device_hop(text):
    """(alias, path) of a typed ``alias:path`` with devices on (``~`` for an empty path), else None."""
    if not devices.is_device_value(text):
        return None
    alias, _, rest = text.strip().partition(':')
    return alias, devices.normalize_remote(rest)


def qualified(view, path):
    """``path`` as the workspace value to add: ``alias:path`` in a device's view."""
    return f"{view['device']}:{path}" if view.get('device') else path


def scan_directory(path):
    """Never recurse or stat link targets. Links are inspected only when entered."""
    started = time.monotonic()
    entries, truncated = [], False
    try:
        with os.scandir(path) as iterator:
            for count, entry in enumerate(iterator):
                if count >= SCAN_LIMIT or time.monotonic() - started > .25:
                    truncated = True
                    break
                link = entry.is_symlink()
                if link or entry.is_dir(follow_symlinks=False):
                    entries.append({'name': entry.name, 'link': link})
    except OSError as exc:
        return {'entries': [], 'error': _read_error(exc)}
    entries.sort(key=lambda item: (item['name'].casefold(), item['name']))
    return {'entries': entries, 'truncated': truncated, 'error': ''}


def search_directories(path, query):
    """Directories below ``path`` whose name contains ``query`` (any case), by relative path: the
    direct children first, then breadth first down to SEARCH_DEPTH levels. Reads at most
    SEARCH_DIRECTORIES directories within SEARCH_SECONDS, follows no link, and skips hidden
    directories unless the query starts with '.', and node_modules and __pycache__ always."""
    started = time.monotonic()
    needle = query.strip().casefold()
    hidden = needle.startswith('.')
    found, level, scanned, truncated = [], [''], 0, False
    for _ in range(SEARCH_DEPTH):
        below = []
        for relative in level:
            if scanned >= SEARCH_DIRECTORIES or time.monotonic() - started > SEARCH_SECONDS:
                truncated = True
                break
            scanned += 1
            names = []
            try:
                with os.scandir(os.path.join(path, relative) if relative else path) as iterator:
                    for count, entry in enumerate(iterator):
                        if count >= SCAN_LIMIT or time.monotonic() - started > SEARCH_SECONDS:
                            truncated = True
                            break
                        link = entry.is_symlink()
                        if link or entry.is_dir(follow_symlinks=False):
                            names.append((entry.name, link))
            except OSError as exc:
                if not relative:
                    return {'entries': [], 'error': _read_error(exc)}
                continue
            for name, link in sorted(names, key=lambda item: (item[0].casefold(), item[0])):
                if not hidden and name.startswith('.'):
                    continue
                child = f'{relative}/{name}' if relative else name
                if needle in name.casefold():
                    found.append({'name': child, 'link': link})
                if not link and name not in SKIPPED:
                    below.append(child)
        if truncated:
            break
        level = below
    return {'entries': found[:SEARCH_RESULTS], 'more': len(found) > SEARCH_RESULTS, 'error': '',
            'truncated': truncated, 'scanned': scanned}


def parse_click(event):
    """(action, name, fields, opening, token) of a picker click, or None for one that is not a
    well-formed picker click (it is ignored). A form button's row index or page is action['arg']."""
    action = card_object(event, 'action_value')
    if action is None:
        return
    name = event.get('action_name') or ''
    if isinstance(name, str) and name.startswith(DIRECTORY_FORM_PREFIX):
        match = re.fullmatch(r'bridge_dir_([0-9a-f]{24}):(\d+):([a-z]+)(\d{0,5})', name)
        if not match:
            return
        action = {'token': match[1], 'revision': int(match[2]), 'verb': match[3],
                  'arg': int(match[4]) if match[4] else None}
    fields = form_fields(event)
    if fields is None:
        return
    opening = isinstance(name, str) and name.startswith(PICKER_PREFIX)
    token = name[len(PICKER_PREFIX):] if opening else action.get('token', '')
    if not valid_token(token):
        return
    return action, name, fields, opening, token


def legacy_form(view, verb, fields):
    """(verb, arg) of a click on a picker card sent before the browser (path box, 筛选, a select of
    subdirectories and 进入所选目录/使用所选目录); None when it cannot apply."""
    location, query, selected = (fields.get(k, '') for k in ('location', 'query', 'directory'))
    if not all(isinstance(v, str) for v in (location, query, selected)):
        return
    if verb == 'filter':
        return 'search', query
    if verb == 'cancel':
        return 'back', None
    if verb not in ('enter', 'select', 'jump'):
        return
    target = location.strip() or view['path']
    if target == view['path'] and selected != 'current' and verb != 'jump':
        try:
            return ('enter' if verb == 'enter' else 'pick'), int(selected)
        except ValueError:
            return
    return ('jump' if verb != 'select' else 'use'), target


def step(view, verb, arg, *, near=None, allow=None):
    """Apply one click to ``view`` in place: 'read' to list (or search) what it shows next, 'refresh'
    to read it again past the cache, 'show' to render it as it is, ('choose', path), 'back', or None
    for a click that does not apply (it is ignored). ``allow`` (a DeviceGate) says which devices a typed
    ``alias:path`` may go to; none given, none."""
    view.pop('notice', None)
    entries = view.get('entries', [])
    ready = not (view.get('loading') or view.get('error'))

    def row(index):
        if not ready or type(index) is not int or index not in {i for i, _ in visible_entries(view)}:
            return None
        return os.path.join(view['path'], entries[index]['name'])

    def move(target, outcome='read'):
        view.update(path=target, query='', results=False, page=0)
        return outcome

    if verb in ('back', 'cancel'):
        return 'back'
    if verb in ('enter', 'pick'):
        target = row(arg)
        if target is None:
            return None
        return move(target) if verb == 'enter' else ('choose', qualified(view, target))
    if verb in ('use', 'select'):
        if not ready:
            return None
        return 'choose', qualified(view, arg if isinstance(arg, str) and arg else view['path'])
    if verb in ('search', 'filter', 'jump'):
        if not isinstance(arg, str) or len(arg) > 1000:
            return None
        text = arg.strip()
        if (hop := device_hop(text)) is not None:
            refusal = allow(hop[0]) if allow is not None else 'devices.json 里没有可用的设备'
            if refusal:
                view['notice'] = '⚠️ ' + refusal
                return 'show'
            view.update(device=hop[0], device_title=device_title(hop[0]))
            return move(hop[1], 'refresh')
        if view.get('device') and (verb == 'jump' or text.startswith(('/', '~'))):
            # A path typed in a device's view is the device's: no expansion and no absolute-path check here.
            target = devices.normalize_remote(text)
            if not target.startswith(('/', '~')):
                view['notice'] = '⚠️ 请输入绝对路径，例如 /home/me/project'
                return 'show'
            return move(target, 'refresh')
        if verb == 'jump' or text.startswith(('/', '~')):
            target = os.path.expanduser(text)
            if not os.path.isabs(target):
                view['notice'] = '⚠️ 请输入绝对路径，例如 /home/me/project'
                return 'show'
            return move(os.path.normpath(target), 'refresh')
        view.update(query=text, results=bool(text), page=0)
        return 'read'
    if verb == 'clear':
        return move(view['path'])
    if verb == 'up':
        if view.get('device'):
            parent = posixpath.dirname(view['path'])
            return move(parent if view['path'] not in ('~', '/') and parent else '/')
        return move(str(Path(view['path']).parent))
    if verb == 'root':
        return move('/')
    if verb == 'near' and near and not view.get('device') and not devices.is_device_value(near):
        return move(str(Path(near).parent))
    if verb == 'hidden':
        view['hidden'] = not view.get('hidden')
        return 'read'
    if verb == 'page':
        if type(arg) is not int or arg < 0:
            return None
        view['page'] = arg
        return 'show'
    if verb == 'refresh':
        return 'refresh'
    return None


def device_title(alias):
    try:
        return devices.current().device(alias).title
    except (devices.UnknownDevice, devices.DeviceConfigError):
        return alias


def read_into(cache, view, *, refresh=False, state_dir=None):
    """Fill ``view`` with its listing, or its search results, when they come within a moment; else
    mark it loading and return the future that brings them. A device view needs the ``state_dir``."""
    query = view.get('query', '') if view.get('results') else ''
    future = cache.request(view['path'], refresh=refresh, query=query, device=view.get('device', ''),
                           state_dir=state_dir)
    try:
        view.update(future.result(timeout=SEARCH_WAIT if query else FAST_SCAN_SECONDS), loading=False)
    except TimeoutError:
        view.update(entries=[], error='', truncated=False, loading=True)
        return future
    return None


def take_listing(view, event):
    """A 'loaded' click (the listing read_into's future brought, queued again by when_listed) applied to ``view``:
    whether the view still waited for it."""
    if '_listing' not in event or not view.get('loading'):
        return False
    view.update(event['_listing'], loading=False)
    return True


def when_listed(future, redispatch):
    """Once read_into's ``future`` brings the listing, ``redispatch(listing)``: the host queues it as a 'loaded' click
    on the card (take_listing), which redraws it."""
    future.add_done_callback(lambda completed: redispatch(completed.result()))


def listed_paths(config, device=''):
    """Saved workspaces by normalized path, for the browser's 已在列表 marks; reads no directory. A device's view
    marks the workspaces on that device (by their path there), a local view those here."""
    if device:
        prefix = device + ':'
        return {posixpath.normpath(path[len(prefix):] or '~'): name for name, path in config['workspaces'].items()
                if path.startswith(prefix) and devices.is_device_value(path)}
    return {os.path.normpath(path): name for name, path in config['workspaces'].items()
            if not devices.is_device_value(path)}


def open_view(ctx, event, record, topic, candidate, allow=None):
    """The first view of a picker opened from the launch form: the draft the form had is kept in
    ``candidate`` for its return. None when the click cannot open it."""
    if record.get('picker'):
        return
    fields = form_fields(event)
    if fields is None or any(not isinstance(fields.get(k, ''), str)
                             for k in ('model', 'effort', 'workspace', 'path', 'run_mode')):
        return
    if record.get('compact'):
        # Its selects were recorded as chosen; the form sends only the path.
        recorded = record.get('form_defaults', {})
        fields = {**fields, 'workspace': fields.get('workspace') or recorded.get('workspace', ''),
                  'run_mode': fields.get('run_mode') or recorded.get('run_mode', '')}
    try:
        effort = selected_effort(record, fields.get('effort'), allow_empty=True)
    except ValueError:
        return
    candidate['draft'] = {k: fields.get(k, '') for k in ('model', 'effort', 'workspace', 'path', 'run_mode')}
    candidate['draft'].update(model=record.get('selected_model') or fields.get('model', ''), effort=effort,
                              work_mode=record.get('selected_work_mode', ''),
                              redaction=record.get('selected_redaction', ''))
    config = load_config(ctx.state_dir, ctx.default_workdir)
    initial = config['workspaces'].get(fields.get('workspace'), topic['workdir'])
    custom = fields.get('path', '').strip()
    if custom and (Path(custom).is_absolute() or devices.is_device_value(custom)):
        initial = custom
    view = {'path': initial, 'hidden': False, 'query': '', 'revision': 1}
    if (hop := device_hop(initial)) is not None:
        # Browsing starts on the device the chosen workspace is on, where this chat may.
        if allow is not None and not allow(hop[0]):  # '' is allowed.
            view.update(path=hop[1], device=hop[0], device_title=device_title(hop[0]))
        else:
            view['path'] = str(ctx.default_workdir)
    if allow is not None and allow.offers():
        view['offer'] = True  # The search box says a device's path can be typed.
    return view


class DirectoryCache:
    """At most two disk readers, including hung NAS calls; no executor exit wait. A listing is
    cached by its path, a search by (path, query); a device's by ``('dev', device, path[, query])``."""

    def __init__(self, *, ttl=20, capacity=64, stale_ttl=120):
        self.ttl, self.capacity, self.stale_ttl = ttl, capacity, stale_ttl
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.pending = {}
        self.slots = threading.BoundedSemaphore(2)

    def request(self, path, *, refresh=False, query='', device='', state_dir=None):
        text = query.strip().casefold()
        key = (('dev', device, path, text) if text else ('dev', device, path)) if device else (
            (path, text) if text else path)
        stale = None
        with self.lock:
            if refresh:
                self.cache.pop(key, None)
            cached = self.cache.get(key)
            age = time.monotonic() - cached[0] if cached else float('inf')
            if cached and age < self.ttl:
                self.cache.move_to_end(key)
                future = Future()
                future.set_result(cached[1])
                return future
            if cached and not cached[1].get('error') and age < self.stale_ttl:
                # A recent listing can render immediately while one bounded
                # reader refreshes it. Explicit Refresh never takes this path.
                stale = Future()
                stale.set_result({**cached[1], 'stale': True})
                self.cache.move_to_end(key)
            if key in self.pending:
                return stale or self.pending[key]
            future = Future()
            if not self.slots.acquire(blocking=False):
                future.set_result({'entries': [], 'error': f'目录仍在读取，请稍后{RETRY}；也可以返回。'})
                return stale or future
            self.pending[key] = future

        def read():
            try:
                found = (read_device(device, path, query.strip(), state_dir) if device else
                         search_directories(path, query) if text else scan_directory(path))
                result = {**found, 'stale': False}
            except Exception:
                result = {'entries': [], 'error': f'读取失败，{RETRY}。', 'stale': False}
            with self.lock:
                self.pending.pop(key, None)
                self.cache[key] = (time.monotonic(), result)
                self.cache.move_to_end(key)
                while len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)
            self.slots.release()
            future.set_result(result)

        threading.Thread(target=read, name='directory-scan', daemon=True).start()
        return stale or future


class DirectoryPicker(CardActionQueue):
    """The launch host: the browser replaces the launch card, 使用 returns to it with the directory
    chosen, 返回 with nothing changed."""

    def __init__(self, topics):
        super().__init__(topics, name='directory-picker', workers=3, label='directory picker')
        self.cache = DirectoryCache()

    @staticmethod
    def matches(event):
        name = event.get('action_name')
        return ((isinstance(name, str) and name.startswith((PICKER_PREFIX, DIRECTORY_FORM_PREFIX)))
                or action_value(event).get('kind') == 'bridge_directory')

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is the browser's, else None (app/unified.py EARLY); the
        processing refuses anyone but the owner on the card."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return 'directory' if owner else 'owner_only'

    def dispatch(self, ctx, event):
        # Callback ingestion stays free for Stop, messages, and other cards.
        self._enqueue(ctx, event)

    def _enqueue(self, ctx, event):
        self.submit('callback dropped, refresh remains available', self.process, ctx, event)

    def _publish(self, ctx, path, record, candidate):
        config = load_config(ctx.state_dir, ctx.default_workdir)
        card = directory_card(path.stem, candidate['picker'], listed=listed_paths(config, candidate['picker'].get('device', '')))
        return replace_form_card(ctx, path, record, candidate, card, self.topics.messenger, 'directory')

    def process(self, ctx, event):
        access = self.topics.runtime.access
        if not access.allows(event.get('operator_id', ''), event.get('chat_id', '')):
            return
        owner = access.is_owner(event.get('operator_id', ''))
        click = parse_click(event)
        if click is None:
            return
        action, name, fields, opening, token = click
        path = ctx.state_dir / 'launch-forms' / (token + '.json')
        if (retired := retired_form_at(path, event)) is not None:
            refuse_retired_form(self.topics, ctx, retired, event)
            return
        with resource_lock(LAUNCH_FORM_LOCK, path):
            try:
                record = read_json(path)
                topic = load_topic(ctx.state_dir, record['topic_id'])
            except (OSError, ValueError, KeyError):
                return
            if not self._card_current(ctx, event, record, topic):
                return
            if not owner:
                # A group member: the launch card says so; an open picker (the owner's) stays as it is.
                if opening and not record.get('picker'):
                    refuse_on_launch_card(self.topics, ctx, path, record, OWNER_ONLY_DIRECTORY)
                return
            candidate = copy.deepcopy(record)
            if opening:
                view = open_view(ctx, event, record, topic, candidate,
                                 allow=DeviceGate(self.topics.runtime.access, event.get('chat_id', '')))
                if view is None:
                    return
                outcome = 'read'
            else:
                stepped = self._step(ctx, path, record, candidate, topic, action, name, fields, event)
                if stepped is None:
                    return
                view, outcome = stepped
            candidate['picker'] = view
            self._show(ctx, path, record, candidate, outcome, event, token)

    @staticmethod
    def _card_current(ctx, event, record, topic):
        """Whether the click is on the launch card as it is now, of a topic that has not started: the picker
        is only for a ready launch card, bound as when it was drawn."""
        return not (retired_form(record) or record.get('new_topic')
                    or topic.get('workspace_locked') or topic.get('harness_locked')
                    or any((ctx.state_dir / 'topics' / record['topic_id']).glob('*_session_id'))
                    or record.get('status') != 'ready' or not record.get('card_id')
                    or record.get('message_id') != event.get('message_id')
                    or record.get('chat_id') != event.get('chat_id')
                    or topic.get('chat_id') != event.get('chat_id')
                    or record.get('harness_id') != topic.get('harness_id')
                    or record.get('binding_revision') != topic.get('binding_revision', 1))

    def _step(self, ctx, path, record, candidate, topic, action, name, fields, event):
        """A click on the open picker (``candidate``'s): (its view, the outcome) to show; None when nothing is
        left to show here: a stale click, a listing that came in (shown already), back to the launch card."""
        view = candidate.get('picker')
        if not view or action.get('revision') != view.get('revision'):
            return None
        verb = action.get('verb')
        if verb == 'loaded':
            if not take_listing(view, event):
                return None
            view['revision'] += 1
            self._publish(ctx, path, record, candidate)
            return None
        arg = action.get('arg', action.get('page', action.get('index')))
        if name.startswith(DIRECTORY_FORM_PREFIX):
            if verb == 'search':
                arg = fields.get('query', '')
            elif arg is None and verb in ('enter', 'select', 'jump', 'filter', 'cancel'):
                if (legacy := legacy_form(view, verb, fields)) is None:
                    return None
                verb, arg = legacy
        outcome = step(view, verb, arg, near=topic['workdir'],
                       allow=DeviceGate(self.topics.runtime.access, event.get('chat_id', '')))
        if outcome is None:
            return None
        if outcome == 'back' or outcome[0] == 'choose':
            self._return_form(ctx, path, candidate, topic, None if outcome == 'back' else outcome[1])
            return None
        view['revision'] += 1
        return view, outcome

    def _show(self, ctx, path, record, candidate, outcome, event, token):
        """``candidate``'s picker on the card, its directory read (unless only shown) and, once listed, the
        listing posted back as a ``loaded`` click."""
        view = candidate['picker']
        future = (read_into(self.cache, view, refresh=outcome == 'refresh', state_dir=ctx.state_dir)
                  if outcome != 'show' else None)
        if self._publish(ctx, path, record, candidate) and future is not None:
            revision = view['revision']
            when_listed(future, lambda listing: self._enqueue(ctx, {**event, 'action_name': '', 'action_value': {
                'kind': 'bridge_directory', 'token': token, 'revision': revision, 'verb': 'loaded'},
                '_listing': listing}))

    def _return_form(self, ctx, path, record, topic, target):
        """Back to the launch card: with ``target`` listed and chosen, or (None) unchanged."""
        draft = dict(record.get('draft', {}))
        notice = ''
        if target is not None:
            try:
                # Checked within a bounded wait, outside the workspace lock.
                name, _ = add_workspace(ctx.state_dir, ctx.default_workdir, target)
            except ValueError as exc:
                candidate = copy.deepcopy(record)
                candidate['picker'].update(notice='⚠️ 目录不可用：' + literal(exc), loading=False)
                candidate['picker']['revision'] += 1
                self._publish(ctx, path, record, candidate)
                return
            draft.update(workspace=name, path='')
            notice = '已选择工作区：' + literal(name)
        # Not followed by a 开始任务 from the old card, and left startable if the update is unconfirmed:
        # the new form starts with the model and budget the old one had.
        rebuild_launch_card(self.topics, ctx, path, record, draft, kind='directory', notice=notice,
                            follow=False, fail_closed=False)

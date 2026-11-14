"""Bounded directory browsing: one listing or name search at a time, never on the event threads.

The navigation here (``step``, ``read_into``) and the card in cards/directory.py serve two hosts: the
launch card's 选择其他目录 (``DirectoryPicker`` below, the view in a launch form's ``picker``) and the
workspace manager's ＋ 添加工作区… (app/workspace_manager.py, the view in ``manager.browse``)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
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
from covey.core.text import literal
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
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token):
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


def step(view, verb, arg, *, near=None):
    """Apply one click to ``view`` in place: 'read' to list (or search) what it shows next, 'refresh'
    to read it again past the cache, 'show' to render it as it is, ('choose', path), 'back', or None
    for a click that does not apply (it is ignored)."""
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
        return move(target) if verb == 'enter' else ('choose', target)
    if verb in ('use', 'select'):
        if not ready:
            return None
        return 'choose', arg if isinstance(arg, str) and arg else view['path']
    if verb in ('search', 'filter', 'jump'):
        if not isinstance(arg, str) or len(arg) > 1000:
            return None
        text = arg.strip()
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
        return move(str(Path(view['path']).parent))
    if verb == 'root':
        return move('/')
    if verb == 'near' and near:
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


def read_into(cache, view, *, refresh=False):
    """Fill ``view`` with its listing, or its search results, when they come within a moment; else
    mark it loading and return the future that brings them."""
    query = view.get('query', '') if view.get('results') else ''
    future = cache.request(view['path'], refresh=refresh, query=query)
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


def listed_paths(config):
    """Saved workspaces by normalized path, for the browser's 已在列表 marks; reads no directory."""
    return {os.path.normpath(path): name for name, path in config['workspaces'].items()}


def open_view(ctx, event, record, topic, candidate):
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
    if custom and Path(custom).is_absolute() :
        initial = custom
    view = {'path': initial, 'hidden': False, 'query': '', 'revision': 1}
    return view


class DirectoryCache:
    """At most two disk readers, including hung NAS calls; no executor exit wait. A listing is
    cached by its path, a search by (path, query)."""

    def __init__(self, *, ttl=20, capacity=64, stale_ttl=120):
        self.ttl, self.capacity, self.stale_ttl = ttl, capacity, stale_ttl
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.pending = {}
        self.slots = threading.BoundedSemaphore(2)

    def request(self, path, *, refresh=False, query=''):
        key = (path, query.strip().casefold()) if query.strip() else path
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
                found = search_directories(path, query) if key != path else scan_directory(path)
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
        card = directory_card(path.stem, candidate['picker'], listed=listed_paths(config))
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
                view = open_view(ctx, event, record, topic, candidate)
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
        outcome = step(view, verb, arg, near=topic['workdir'])
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
        future = read_into(self.cache, view, refresh=outcome == 'refresh') if outcome != 'show' else None
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

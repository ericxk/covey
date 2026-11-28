"""The 设置 card's 仓库 tab, and the retired 工作区 manager card.

The tab (cards/settings_hub.py REPOS, in the 工作区 tab's place) is read only: the home (``default_workdir``), how many repositories
the map has and, with the web page up (core/web_sync.py ``page_url``), the link to its 仓库 page; without the page the
repositories' names, states and 用途. The map is ``<state>/repos.json`` (core/repomap.py, read without a lock, rule 2). Before it exists
``repo_entries`` reads the old workspace list (workspace-config.json) and the butler's 用途 for each (core/butler_settings.py
``workspace_notes``) as a read-only view, and writes nothing: the page's first ``repos_*`` change makes the file (app/web_settings.py).
The bot menu's older 工作区 key is gone from the menu (core/app_manifest.py STALE_MENU_KEYS; app/bot_menu.py answers a cached copy).

The manager card itself is retired: no entry sends it, the launch card's ⚙️ 添加或管理工作区… is gone with the 工作区 row, and a
click on a card sent before (the manager's own buttons, or that option of a launch card) gets RETIRED_TEXT as a message and changes
nothing. Its builders stay in cards/workspace_manager.py for the cards already sent.
"""
from __future__ import annotations

import os
from pathlib import Path

from covey.app.card_actions import action_value
from covey.app.card_queue import CardActionQueue
from covey.app.menu_panel import HUB_FOLDER, MenuPanel, hub_top
from covey.cards.launch_config import MANAGE_OPTION, WORKSPACE_ACTION
from covey.cards.settings_hub import REPOS, WORKSPACE
from covey.cards.workspace_manager import MANAGER_PREFIX, repos_card, tail
from covey.core import butler_settings, devices, repomap, web_sync
from covey.core.state import read_object
from covey.core import statuses
from covey.core.workspaces import config_path, directory_status

MANAGING = statuses.MANAGING  # A launch card that became the manager: cards sent before still name it.
LOCK = 'workspace-manager'
RETIRED_TEXT = '这个选项已下线：新话题一律从主目录开始，仓库在网页设置的「常用路径」页管。'
OWNER_ONLY = '⚠️ 只有机器人主人能改设置。'
HOME_SIZE = 80  # Characters of the home's path the tab shows (its tail).


def is_hub(path):
    """Whether the record at ``path`` is a 设置 card's (its view in ``view``)."""
    return path.parent.name == HUB_FOLDER


def _real(path) -> str:
    try:
        return os.path.realpath(str(path)).rstrip('/') if path else ''
    except (OSError, ValueError):
        return str(path or '').rstrip('/')


def _checkout(path) -> bool:
    """Whether ``path`` may be a repository: a git checkout (its ``.git`` a directory, or a worktree's file), or a directory
    not there now (the page says it is missing); False for a directory that is there and no checkout."""
    try:
        where = Path(path)
        return (where / '.git').exists() or not where.is_dir()
    except (OSError, ValueError):
        return True


def derived_repos(state_dir, home=None):
    """The repositories the old workspace list and the butler's 用途 say, as ``Repo``s (added_by ``migrate``), for a deployment without
    ``repos.json``: read only, nothing is written. The home itself (its real path too), a directory there that is no git checkout, a device's
    workspace and a name without an absolute path are left out; ``last_used`` is the list's last use; the way a topic writes to it is the one the 新话题 default had (``run_mode``): ``worktree``
    for an own worktree, else ``inplace``, as every topic edited it before."""
    config = read_object(config_path(Path(state_dir)))
    found = config.get('workspaces') if isinstance(config.get('workspaces'), dict) else {}
    recent = config.get('recent') if isinstance(config.get('recent'), dict) else {}
    try:
        settings = butler_settings.load(Path(state_dir))
        notes, mode = dict(settings.workspace_notes), settings.topic_defaults.run_mode
    except (OSError, ValueError, AttributeError):
        notes, mode = {}, 'direct'
    home = str(home or '').rstrip('/')
    homes = {home, _real(home)} - {''}
    out = []
    for name, value in found.items():
        value = str(value)
        if not isinstance(name, str) or not value.startswith('/') or devices.is_device_value(value) or value.rstrip('/') in homes:
            continue
        if _real(value) in homes or not _checkout(value):
            continue  # The home under another name, or a directory a topic once ran in that is there and no repository.
        used = recent.get(name)
        out.append(repomap.Repo(name=name, path=value, note=str(notes.get(name) or ''), status='active',
                                write_mode='worktree' if mode == 'worktree' else 'inplace', added_by='migrate',
                                last_used=float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 0.0))
    return out


def repo_entries(state_dir, home=None):
    """(the repositories, whether ``repos.json`` holds them): the map when its file exists, else ``derived_repos``. Never raises."""
    try:
        if repomap.path(state_dir).exists():
            return repomap.read(state_dir), True
    except OSError:
        return [], True  # The map is there but cannot be asked now: the page says none rather than a stand-in.
    return derived_repos(state_dir, home), False


class WorkspaceManager(CardActionQueue):
    def __init__(self, topics, cache=None):
        """``cache`` (the launch card's DirectoryCache) is kept for the caller; nothing here browses a directory any more."""
        super().__init__(topics, name='workspace-manager', workers=2, label='workspace manager')
        self.checked = {}  # The latest directory answers (``status``): the web page's old 工作区 page needs no NAS wait.
        self.cache = cache
        self.menu = MenuPanel(topics, 'workspace-manager', 'workspace-managers', LOCK, 'manager', tab=(REPOS, WORKSPACE))

    action = staticmethod(action_value)

    def matches(self, event):
        name = event.get('action_name')
        if isinstance(name, str) and name.startswith(MANAGER_PREFIX):
            return True
        return self.action(event).get('kind') == WORKSPACE_ACTION and event.get('option') == MANAGE_OPTION

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a retired manager card's (or the launch card's option for it), else None (app/unified.py
        EARLY); the processing says RETIRED_TEXT to the owner and nothing else."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return 'workspace_manager' if owner else 'owner_only'

    def dispatch(self, ctx, event, handler=None):
        self.submit('request dropped', handler or self.process, ctx, event)

    def process(self, ctx, event):
        """A click on a retired manager card: RETIRED_TEXT, a message of its own at the chat's top level, to the owner; no record or
        card changes."""
        access = self.topics.runtime.access
        chat_id, operator = str(event.get('chat_id') or ''), event.get('operator_id', '')
        if not chat_id or not access.allows(operator, chat_id) or not access.is_owner(operator):
            return
        source = str(event.get('event_id') or event.get('message_id') or '')
        self.topics.messenger.send_message(ctx.profile, chat_id, RETIRED_TEXT, self.topics.voice.key('workspace-retired', source),
                                           voice=self.topics.voice)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    # The 设置 card's 仓库 tab (app/settings_hub.py): its first view, its record's fields, its card, once shown.
    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record, fresh=True):
        """The tab for the 设置 card's ``record``: the home, the count and the link, or the repositories themselves."""
        repos, _ = repo_entries(ctx.state_dir, ctx.default_workdir)
        link = web_sync.page_url()
        return repos_card(token, record['view'], home=tail(str(ctx.default_workdir or ''), HOME_SIZE),
                          items=[{'name': repo.name, 'status': repo.status, 'note': repo.note} for repo in repos],
                          link=link.rstrip('/') + '/settings/repos' if link else '', top=hub_top(REPOS, token, record['view']))

    def tab_shown(self, ctx, token):
        self.topics.runtime.log(f'repos tab shown on the settings card token={token[:8]}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the 设置 card ``event`` clicked while it shows this tab; whether it did."""
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)
            if record is None or not is_hub(path):
                return False
            view = {**record['view'], 'notice': OWNER_ONLY, 'revision': record['view']['revision'] + 1}
            self.menu.publish(ctx, path, record, view, self.tab_card(ctx, path.stem, {**record, 'view': view}))
            return True

    def status(self, config, fresh=True):
        """{path: True, False or None (no answer in time)} of the workspace list's directories, for the web page's old 工作区 page."""
        paths = list(dict.fromkeys(config['workspaces'].values()))
        if fresh:
            self.checked.update(directory_status(paths))
        return {path: self.checked.get(path) for path in paths}


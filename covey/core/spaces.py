"""Spaces: the chats the Bridge works in, and what each is for. Standard library only; the decisions stay with
app/access.py (rule 11), which reads these settings.

- ``personal``: only the owner (and the owner's other accounts). A private chat with the bot, and each group listed
  in ``personal_groups`` (a project group of the owner's own).
- ``team``: a group that is active (app/access.py): colleagues @ the bot to start topics.
- ``proxy``: the group the proxy made (or a whitelisted one) for the contacts' topics (app/proxy.py).
- ``''``: not bound to anything (a group the bot is in that the owner never @-ed it in).

Settings come from the ``spaces`` section of harnesses.json (docs/configuration.md); a file without it is read as the
defaults, which behave as before spaces existed. The NAS profile's older keys are read as aliases (``ALIASES``) and
each is noted once at startup; the keys of its features that need the personal-space butler, menu card and
boards (``LATER``) are read and noted: personal groups have them without a switch.

Records (``<state>/spaces/<sha256(chat_id)>.json``): each bound group's ``kind`` and ``title``, written where the Bridge
learns them (app/group_menu.py, app/group_manager.py) and shown on the 设置 card's 群聊 tab. Decisions never read them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

from covey.core.locks import resource_lock
from covey.core.state import read_object, utc_now_iso, write_json

PERSONAL, TEAM, PROXY = 'personal', 'team', 'proxy'
KINDS = (PERSONAL, TEAM, PROXY)
CHAT_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
OPEN_ID = re.compile(r'ou_[A-Za-z0-9_-]{1,120}')
# old key -> the ``spaces`` field it stands for.
ALIASES = {'owner_only': 'allow_team_spaces', 'owner_aliases': 'owner_accounts', 'home_chat': 'notify_space',
           'agent_groups': 'personal_groups'}
# Read and noted, with no effect of their own: a personal group has its menu card, its boards and the status-line
# heads on answer cards, the butler's besides (app/group_menu.py, app/group_boards.py, app/saved_send.py).
LATER = ('group_menu', 'group_boards', 'owner_headed')


@dataclasses.dataclass(frozen=True)
class Settings:
    owner_accounts: frozenset = frozenset()  # The owner's other open_ids in this app: each is the owner.
    notify_space: str = ''  # The chat notices go to; '' the owner's private chat.
    allow_team_spaces: bool = True  # False: no group is ever active; only personal spaces are served.
    personal_groups: frozenset = frozenset()  # Groups that are the owner's personal spaces.


DEFAULT = Settings()


def _list(value, pattern, what):
    if not isinstance(value, list) or not all(isinstance(v, str) and pattern.fullmatch(v) for v in value):
        raise ValueError(f'{what} must be a list of {"open_ids" if pattern is OPEN_ID else "chat_ids"}')
    return frozenset(value)


def parse(config) -> tuple[Settings, list[str]]:
    """(the settings, one note per deprecated or not-yet-effective key) of a harnesses.json object. A ``spaces``
    field wins over the old key naming the same thing. A wrong type raises ValueError."""
    config = config if isinstance(config, dict) else {}
    section = config.get('spaces', {})
    if not isinstance(section, dict):
        raise ValueError('spaces must be an object')
    notes, found = [], dict(section)
    for old, new in ALIASES.items():
        if old in config:
            notes.append(f'harnesses.json key {old} is deprecated: write spaces.{new}'
                         + (' (false)' if old == 'owner_only' else ''))
            if new not in found:
                value = config[old]
                if old == 'owner_only':
                    if not isinstance(value, bool):
                        raise ValueError('owner_only must be true or false')
                    value = not value
                found[new] = value
    for key in LATER:
        if key in config:
            notes.append(f'harnesses.json key {key} is deprecated and has no effect of its own: a group in '
                         'spaces.personal_groups has it')
    allow = found.get('allow_team_spaces', True)
    if not isinstance(allow, bool):
        raise ValueError('allow_team_spaces must be true or false')
    notify = found.get('notify_space', '')
    if not isinstance(notify, str) or notify and not CHAT_ID.fullmatch(notify):
        raise ValueError('notify_space must be a chat_id')
    return Settings(owner_accounts=_list(found.get('owner_accounts', []), OPEN_ID, 'owner_accounts'),
                    notify_space=notify, allow_team_spaces=allow,
                    personal_groups=_list(found.get('personal_groups', []), CHAT_ID, 'personal_groups')), notes


def space_of(settings: Settings, chat_id, *, private=False, proxy=False, active=False) -> str:
    """The kind of chat ``chat_id`` (KINDS, '' when unbound). ``private``: it is the owner's direct chat with the bot;
    ``proxy``: the proxy uses it; ``active``: it is an active group. Settings first, so a listed group is personal
    whatever else is true of it."""
    if chat_id and chat_id in settings.personal_groups:
        return PERSONAL
    if proxy:
        return PROXY
    if private:
        return PERSONAL
    return TEAM if active and settings.allow_team_spaces else ''


SPACES_DIR = 'spaces'


def space_path(state_dir, chat_id):
    return state_dir / SPACES_DIR / (hashlib.sha256(chat_id.encode()).hexdigest() + '.json')


def read_space(state_dir, chat_id) -> dict:
    """The record of ``chat_id`` (``chat_id``, ``kind``, ``title``, ``noted_at``); {} when none."""
    if not chat_id or state_dir is None:
        return {}
    record = read_object(space_path(state_dir, str(chat_id)))
    return record if record.get('chat_id') == chat_id else {}


def save_space(state_dir, chat_id, kind, title='') -> dict:
    """``chat_id`` noted as a ``kind`` space (a ``title`` '' keeps the one noted); written only when it changed, under the
    record's lock. The record as it is now."""
    path = space_path(state_dir, chat_id)
    with resource_lock('space', str(path)):
        record = read_space(state_dir, chat_id)
        new = {'chat_id': chat_id, 'kind': kind, 'title': title or record.get('title', ''),
               'noted_at': record.get('noted_at') or utc_now_iso()}
        if (new['kind'], new['title']) != (record.get('kind'), record.get('title')):
            write_json(path, new, ensure_ascii=False, indent=2)
            return new
        return record


def list_spaces(state_dir) -> list[dict]:
    """Every record, as ``read_space`` reads it."""
    if state_dir is None:
        return []
    found = []
    for path in sorted((state_dir / SPACES_DIR).glob('*.json')):
        record = read_object(path)
        if isinstance(record.get('chat_id'), str) and path == space_path(state_dir, record['chat_id']):
            found.append(record)
    return found

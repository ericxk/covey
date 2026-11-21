"""The group records (app/access.py says what they decide): one per group, ``<state>/groups/<sha256(chat_id)>.json``,
with ``chat_id``, ``active``, ``external`` and the fields app/access.py lists. Read and written here only; a change
happens under the record's lock and makes no Feishu call (the chat lookup runs before it is taken)."""
from __future__ import annotations

import hashlib

from covey.core.locks import resource_lock
from covey.core.state import read_object, utc_now_iso, write_json

GROUPS_DIR = 'groups'
# A group waiting for the owner's word (``pending``) and its confirmation card going out: both end with any decision.
WAITING = ('pending_at', 'pending_by', 'asking_at')


def group_path(state_dir, chat_id):
    return state_dir / GROUPS_DIR / (hashlib.sha256(chat_id.encode()).hexdigest() + '.json')


def read_group(state_dir, chat_id) -> dict:
    """The group's record; {} for a direct chat or a group the owner has not @-ed the bot in."""
    if not chat_id or state_dir is None:
        return {}
    record = read_object(group_path(state_dir, str(chat_id)))
    return record if record.get('chat_id') == chat_id else {}


def save_group(state_dir, chat_id, change) -> dict:
    """Apply ``change(record)`` to the group's record under its lock (a new group's: inactive, external unknown) and
    write it when it changed; the record as it is now. ``change`` returns None to leave it."""
    path = group_path(state_dir, chat_id)
    with resource_lock('group', path):
        record = read_group(state_dir, chat_id) or {'chat_id': chat_id, 'active': False, 'external': None}
        updated = change(dict(record))
        if updated is not None and updated != record:
            write_json(path, updated, ensure_ascii=False, indent=2)
        return updated or record


def list_groups(state_dir) -> list[dict]:
    """Every group record, as ``read_group`` reads it."""
    if state_dir is None:
        return []
    found = []
    for path in sorted((state_dir / GROUPS_DIR).glob('*.json')):
        record = read_object(path)
        if isinstance(record.get('chat_id'), str) and path == group_path(state_dir, record['chat_id']):
            found.append(record)
    return found


def pending(record) -> bool:
    """Whether the group waits for the owner's word (app/access.py): known not to be external, never turned on or off
    since his first @ marked it (``pending_at``). A release before it reads such a record as an inactive group."""
    return (bool(record.get('pending_at')) and record.get('external') is False and record.get('active') is not True
            and not record.get('deactivated_at'))


def decided(record):
    """``record`` no longer waiting for the owner's word (WAITING dropped)."""
    for key in WAITING:
        record.pop(key, None)
    return record


def activated(record, owner, external):
    """``record`` as the lookup's ``external`` decides it: active unless external, and then activated by ``owner``."""
    record.update(external=external, active=not external)
    if not external:
        record.update(activated_by=owner, activated_at=utc_now_iso())
        record.pop('deactivated_at', None)
        record.pop('deactivated_by', None)
        decided(record)
    return record

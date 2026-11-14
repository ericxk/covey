"""Who the Bridge serves: the one place that decides it.

- A direct chat: the owner only (``Runtime.owner_open_id``). Pairing happens only there.
- A group: Feishu delivers only the messages that @ this bot (the app has
  ``im:message.group_at_msg``, not ``group_msg``), and a message counts only when its
  mentions name this bot (its open_id, or in a fetched message detail its app ID):
  @所有人 does not. The owner is always served; any other user once the group is active.
- 免@ (only with FREE_ENV=1, a deployment whose app also has ``im:message.group_msg``, so Feishu sends every
  message of the groups the bot is in): the owner picks on the 群聊 card who in a group is served without the @
  (``free_talk``: FREE_OWNER the owner, FREE_SOME the owner and ``free_talk_members``, FREE_ALL everyone).
  It never widens who is served: members only while the group is active, never in an external one. A message
  without the bot's @ that mentions anyone else (a person, another bot, @所有人) is someone else's; the proxy's
  chats never talk freely (the proxy sends as the owner there, ``proxy_chat``). Without the switch the field is
  read as absent and every group message needs the @, as before; with it, the messages nobody talks freely in
  are dropped without a log line (Feishu now sends all of them).
- A group becomes active the first time the owner @s the bot there, unless Feishu says it
  is external: it then stays the owner's only. The owner switches it on the 群管理 card
  (app/group_manager.py, opened from the bot menu). A chat lookup that fails activates
  nothing; the owner's next @ tries again.
- A bot's message is never served.
- Card clicks: the owner, or anyone in an active group. The workspace manager, the
  directory picker (and a typed launch path) and pairing stay the owner's everywhere
  (``is_owner``); their controllers refuse others on the card.

- Spaces (core/spaces.py, the ``spaces`` section of harnesses.json): ``owner_accounts`` are more open_ids that are the
  owner wherever ``is_owner`` decides (the records keep the one ``owner_open_id``; only that account's direct message
  names the owner's private chat); ``allow_team_spaces`` false activates no group, so only the owner is served;
  a group in ``personal_groups`` is the owner's own space: members are not served there even where one is active;
  ``notify_space`` is where ``notify_chat`` points. ``space_of`` tells what a chat is.

Everything refused is silent apart from one log line (``ignored message … reason=…`` here,
``card action … outcome=…`` for clicks). Each group is one record (read and written by core/groups.py),
``groups/<sha256(chat_id)>.json``: ``chat_id``, ``active``, ``activated_by``,
``activated_at``, ``external`` (None until a lookup answered), and ``deactivated_at`` /
``deactivated_by`` after 停用; ``free_talk`` (absent: off), ``free_talk_members`` (open_ids, kept while another
mode is on), ``free_talk_by`` / ``free_talk_at`` once set. The chat lookup runs before the record's lock is taken.

The owner's private chat with the bot, where the bot menu's answers go (app/bot_menu.py), is
``owner-chat.json``: ``owner`` and ``chat_id``, written when the owner's direct message names
another chat than the one saved.
"""
from __future__ import annotations

import dataclasses
import json
import re

from covey.core import env as cenv
from covey.core.locks import resource_lock  # owner-chats.json
from covey.core.groups import activated, list_groups, read_group, save_group
from covey.core.proxy import read_contacts, read_settings
from covey.core import spaces
from covey.core.spaces import CHAT_ID, OPEN_ID
from covey.core.state import read_object, utc_now_iso, write_json
from covey.feishu.mentions import drop_mentions, mention_id, quote_label_lines
from covey.feishu.transport import call, lark_argv

OWNER_CHAT = 'owner-chat.json'
OWNER_CHATS = 'owner-chats.json'
FREE_ENV = 'COVEY_GROUP_ALL_MESSAGES'
FREE_OWNER, FREE_SOME, FREE_ALL = 'owner', 'some', 'all'
FREE_MODES = (FREE_OWNER, FREE_SOME, FREE_ALL)
MEMBER_PAGES = 5  # Pages of 100 a member list reads: a group of more than 500 lists its first 500.


def free_talk_on() -> bool:
    """Whether this deployment offers 免@ (its app receives every group message): FREE_ENV is 1."""
    return cenv.get(FREE_ENV, '').strip() == '1'


@dataclasses.dataclass(frozen=True)
class Admission:
    """A message the Bridge serves: from the owner or not, in a group or a direct chat."""
    owner: bool
    group: bool


def chat_info(profile, chat_id, log=lambda _: None):
    """Feishu's ``im/v1/chats/<chat_id>`` data, or None when it cannot be read."""
    if not CHAT_ID.fullmatch(chat_id):
        return None
    result = call(lark_argv(profile, 'api', 'GET', f'/open-apis/im/v1/chats/{chat_id}', '--as', 'bot',
                            '--format', 'json'), timeout=20, log=log)
    data = (result or {}).get('data')
    return data if isinstance(data, dict) else None


def chat_members(profile, chat_id, log=lambda _: None):
    """The users of group ``chat_id`` as [{'id': open_id, 'name'}], at most MEMBER_PAGES pages; None when Feishu did
    not list them (the app needs ``im:chat.members:read``)."""
    if not CHAT_ID.fullmatch(chat_id):
        return None
    found, token = [], ''
    for _ in range(MEMBER_PAGES):
        params = {'member_id_type': 'open_id', 'page_size': '100', **({'page_token': token} if token else {})}
        result = call(lark_argv(profile, 'api', 'GET', f'/open-apis/im/v1/chats/{chat_id}/members', '--as', 'bot',
                                '--params', json.dumps(params), '--format', 'json'), timeout=20, log=log)
        data = (result or {}).get('data')
        if not isinstance(data, dict):
            return None if not found else found
        for entry in data.get('items') or []:
            member = str((entry or {}).get('member_id') or '') if isinstance(entry, dict) else ''
            if OPEN_ID.fullmatch(member):
                found.append({'id': member, 'name': str(entry.get('name') or '')})
        token = str(data.get('page_token') or '')
        if not data.get('has_more') or not token:
            break
    return found


class Access:
    """The policy above over one Bridge's state. ``lookup(profile, chat_id)`` reads a chat
    (``chat_info``; tests replace it), ``members`` its users; ``identify`` gives it this bot's open_id and app ID."""

    def __init__(self, runtime, lookup=None, members=None) -> None:
        self.runtime = runtime
        self.bot_open_id = ''
        self.app_id = ''
        self.spaces = spaces.DEFAULT  # core/spaces.py Settings; app/unified.py sets them from harnesses.json.
        self.lookup = lookup or (lambda profile, chat_id: chat_info(profile, chat_id, self.runtime.log))
        # ``members(profile, chat_id)``: a group's users (``chat_members``; tests replace it).
        self.members = members or (lambda profile, chat_id: chat_members(profile, chat_id, self.runtime.log))

    def identify(self, *, app_id='', bot_open_id=''):
        self.app_id, self.bot_open_id = str(app_id or ''), str(bot_open_id or '')

    def own_ids(self):
        """The IDs a mention of this bot carries: open_id in an event, app ID in a message detail."""
        return frozenset(i for i in (self.bot_open_id, self.app_id) if i)

    def is_owner(self, user_id) -> bool:
        owner = self.runtime.owner_open_id
        return bool(user_id and owner and (user_id == owner or user_id in self.spaces.owner_accounts))

    def group(self, chat_id) -> dict:
        """The group's record (core/groups.py); {} for a direct chat or a group the owner has not @-ed the bot in."""
        return read_group(self.runtime.state_dir, chat_id)

    def team_allowed(self, chat_id) -> bool:
        """Whether group ``chat_id`` can be a team space: not with ``allow_team_spaces`` false, not a personal group."""
        return self.spaces.allow_team_spaces and chat_id not in self.spaces.personal_groups

    def active(self, chat_id) -> bool:
        if not self.team_allowed(chat_id):
            return False
        record = self.group(chat_id)
        return record.get('active') is True and record.get('external') is False

    def allows(self, user_id, chat_id) -> bool:
        """A click or a routed message from ``user_id`` in ``chat_id``: the owner, or an active group."""
        return self.is_owner(user_id) or bool(user_id and self.active(chat_id))

    def ignore(self, event, reason):
        message = str(event.get('message_id') or event.get('id') or '')
        self.runtime.log(f'ignored message id={message[-10:]} reason={reason}')

    def mentions_bot(self, mentions) -> bool:
        own = self.own_ids()
        return bool(own) and isinstance(mentions, list) and any(
            isinstance(m, dict) and mention_id(m) in own for m in mentions)

    def admit(self, event):
        """The Admission of a message from Feishu, or None (logged) when it is not served."""
        sender = str(event.get('sender_id') or '')
        group = event.get('chat_type') == 'group'
        owner = self.is_owner(sender)
        chat = str(event.get('chat_id') or '')
        reason = ('bot_sender' if event.get('sender_type') not in (None, '', 'user') else
                  'no_sender' if not sender else
                  'not_owner' if not group and not owner else
                  'bot_identity_unknown' if group and not self.bot_open_id else
                  self.unmentioned(event, chat, sender) if group and not self.mentions_bot(event.get('mentions')) else
                  'group_inactive' if group and not owner and not self.active(chat) else '')
        if reason:
            if reason != 'not_mentioned' or not free_talk_on():  # With 免@ on, Feishu sends every group message.
                self.ignore(event, reason)
            return None
        if owner and not group:
            if sender == self.runtime.owner_open_id:  # Not another account's chat.
                self.remember_owner_chat(chat)
            else:
                self.remember_account_chat(sender, chat)
        return Admission(owner=owner, group=group)

    def unmentioned(self, event, chat_id, sender) -> str:
        """Why a group message without the bot's @ is not served; '' when its sender talks freely there (免@)."""
        if not self.talks_freely(sender, chat_id):
            return 'not_mentioned'
        mentions = event.get('mentions')
        if isinstance(mentions, list) and mentions or '@_all' in str(event.get('content') or ''):
            return 'for_others'  # It @s someone else: said to them.
        return 'proxy_chat' if self.proxy_chat(chat_id) else ''

    def free_talk(self, chat_id) -> str:
        """Group ``chat_id``'s 免@ mode as set (FREE_MODES); '' when off, unset or the deployment has no 免@."""
        mode = self.group(chat_id).get('free_talk') if free_talk_on() else ''
        return mode if mode in FREE_MODES else ''

    def free_members(self, chat_id) -> list[str]:
        """The open_ids FREE_SOME names in group ``chat_id`` (kept while another mode is on)."""
        members = self.group(chat_id).get('free_talk_members')
        return [m for m in members if isinstance(m, str)] if isinstance(members, list) else []

    def talks_freely(self, user_id, chat_id) -> bool:
        """Whether ``user_id``'s messages in group ``chat_id`` need no @: the owner in any 免@ mode; a member only
        in an active group set to FREE_ALL, or to FREE_SOME naming them. The owner in a personal group always."""
        if user_id and chat_id in self.spaces.personal_groups and self.is_owner(user_id):
            return True  # The owner's own space: no @ needed (it takes effect where Feishu sends every group message).
        mode = self.free_talk(chat_id)
        if not mode or not user_id:
            return False
        if self.is_owner(user_id):
            return True
        return self.active(chat_id) and (mode == FREE_ALL or mode == FREE_SOME and user_id in self.free_members(chat_id))

    def proxy_chat(self, chat_id) -> bool:
        """Whether the proxy uses group ``chat_id`` (its own group or a whitelisted one): it sends as the owner there,
        so 免@ would take its replies for the owner's words."""
        state = self.runtime.state_dir
        if not chat_id or state is None:
            return False
        if read_settings(state).get('group_chat_id') == chat_id:
            return True
        return any(c.get('kind') == 'group' and chat_id in (c.get('chat_id'), c.get('target')) for c in read_contacts(state))

    def set_free_talk(self, chat_id, mode) -> dict:
        """The owner set group ``chat_id``'s 免@ to ``mode`` (FREE_MODES, '' off); its record."""
        owner = self.runtime.owner_open_id

        def change(record):
            if mode:
                record['free_talk'] = mode
            else:
                record.pop('free_talk', None)
            record.update(free_talk_by=owner, free_talk_at=utc_now_iso())
            return record
        record = self._save(chat_id, change)
        self.runtime.log(f"group chat={chat_id[-8:]} free_talk={mode or 'off'}")
        return record

    def pick_member(self, chat_id, member, on) -> dict:
        """The owner added ``member`` (an open_id) to group ``chat_id``'s FREE_SOME list, or took them off it; its
        record."""
        owner = self.runtime.owner_open_id

        def change(record):
            members = [m for m in record.get('free_talk_members') or [] if isinstance(m, str) and m != member]
            record.update(free_talk_members=members + ([member] if on else []), free_talk_by=owner,
                          free_talk_at=utc_now_iso())
            return record
        return self._save(chat_id, change)

    def owner_turn(self, chat_id, sender_id) -> bool:
        """Whether a turn is the owner's own, so it reads the owner's 通用 principles (app/runs.py): when the message
        that started it is the owner's (``is_owner``), and any turn in the owner's private chat, where only the owner
        is served. A job that kept no sender (older records, continuations, retries) elsewhere is not."""
        if sender_id:
            return self.is_owner(sender_id)
        chat = self.owner_chat()
        return bool(chat and chat_id == chat)

    def owner_chat(self) -> str:
        """The owner's private chat with the bot as last seen; '' when unknown or saved for another owner."""
        try:
            saved = read_object(self.runtime.state_dir / OWNER_CHAT)
        except TypeError:  # No state_dir.
            return ''
        chat_id, owner = saved.get('chat_id'), self.runtime.owner_open_id
        return chat_id if owner and saved.get('owner') == owner and isinstance(chat_id, str) \
            and CHAT_ID.fullmatch(chat_id) else ''

    def notify_chat(self) -> str:
        """Where the Bridge's own notices go (the early/late digest, 手上的事 and its board, alerts, the app setup card):
        ``spaces.notify_space`` when set, else the owner's private chat as last seen (``owner_chat``)."""
        return self.spaces.notify_space or self.owner_chat()

    def space_of(self, chat_id) -> str:
        """The space ``chat_id`` is (core/spaces.py KINDS; '' unbound): the settings' personal groups, the proxy's
        groups, the owner's private chat, an active group."""
        chat_id = str(chat_id or '')
        return spaces.space_of(self.spaces, chat_id,
                               private=bool(chat_id) and (chat_id == self.owner_chat()
                                                          or chat_id in self.account_chats().values()),
                               proxy=self.proxy_chat(chat_id), active=self.active(chat_id))

    def account_chats(self) -> dict:
        """The private chat of each of the owner's other accounts (``owner_accounts``) with the bot, as last seen:
        open_id -> chat_id (``owner-chats.json``); {} when none is saved or they were saved for another owner."""
        if not self.spaces.owner_accounts:
            return {}
        try:
            saved = read_object(self.runtime.state_dir / OWNER_CHATS)
        except TypeError:  # No state_dir.
            return {}
        chats = saved.get('chats') if isinstance(saved.get('chats'), dict) else {}
        owner = self.runtime.owner_open_id
        if not owner or saved.get('owner') != owner:
            return {}
        return {user: chat for user, chat in chats.items()
                if user in self.spaces.owner_accounts and isinstance(chat, str) and CHAT_ID.fullmatch(chat)}

    def account_chat(self, user_id) -> str:
        """The private chat of the owner's other account ``user_id`` with the bot as last seen; '' when unknown."""
        return self.account_chats().get(str(user_id or ''), '')

    def remember_account_chat(self, user_id, chat_id) -> None:
        """``chat_id`` is the private chat of the owner's other account ``user_id``: saved when it is not the one saved."""
        owner = self.runtime.owner_open_id
        if not owner or user_id not in self.spaces.owner_accounts or not CHAT_ID.fullmatch(chat_id or '') \
                or self.runtime.state_dir is None or self.account_chat(user_id) == chat_id:
            return
        path = self.runtime.state_dir / OWNER_CHATS
        try:
            with resource_lock('owner-chats', str(path)):
                chats = self.account_chats()
                chats[user_id] = chat_id
                write_json(path, {'owner': owner, 'chats': chats})
        except OSError as exc:
            self.runtime.log(f'owner account chat not saved ({type(exc).__name__})')

    @property
    def agent_groups(self) -> frozenset:
        """The personal groups (``spaces.personal_groups``): each has a main thread, a menu card and a board of its own."""
        return self.spaces.personal_groups

    def board_groups(self) -> frozenset:
        """The personal groups with a board record of their own: not the notify space, whose board is the default one."""
        return self.spaces.personal_groups - {self.spaces.notify_space}

    def agent_chat(self, chat_id, group=None) -> bool:
        """Whether the owner's top-level messages in ``chat_id`` go to its main thread (app/main_thread.py): a personal
        group, the owner's private chat as last seen, or the private chat of one of the owner's other accounts.
        ``group`` None: either; True: only a group; False: only a private chat."""
        chat_id = str(chat_id or '')
        if not chat_id:
            return False
        if chat_id in self.spaces.personal_groups:
            return group is not False
        return group is not True and (chat_id == self.owner_chat() or chat_id in self.account_chats().values())

    def remember_owner_chat(self, chat_id) -> None:
        """``chat_id`` is the owner's private chat: saved when it is not the one saved."""
        owner = self.runtime.owner_open_id
        if not owner or not CHAT_ID.fullmatch(chat_id or '') or self.runtime.state_dir is None \
                or self.owner_chat() == chat_id:
            return
        try:
            write_json(self.runtime.state_dir / OWNER_CHAT, {'owner': owner, 'chat_id': chat_id})
        except OSError as exc:
            self.runtime.log(f'owner chat not saved ({type(exc).__name__})')

    def strip(self, event) -> dict:
        """``event`` without this bot's own @: its ``@名字`` (or placeholder key) in the text and
        its entry in the mentions. Other mentions stay, and so does the bot's name typed as text."""
        mentions = event.get('mentions') if isinstance(event.get('mentions'), list) else []
        own = self.own_ids()

        def tokens(entries):
            return {t if t.startswith('@') else '@' + t for m in entries if isinstance(m, dict)
                    for t in (str(m.get('key') or ''), str(m.get('name') or '')) if t}
        mine = [m for m in mentions if isinstance(m, dict) and mention_id(m) in own]
        removed = tokens(mine)
        if not removed:
            return event
        content = str(event.get('content') or '')
        # Every mention's text is matched, longest first: a bot "C队" leaves "@C队长" alone.
        pattern = '|'.join(re.escape(t) for t in sorted(tokens(mentions), key=len, reverse=True))
        hits = [m for m in re.finditer(pattern, content) if m.group(0) in removed]
        # Those a space, @ or the end follows; with none, as many as the bot was mentioned. The same
        # name typed as plain text ("问问@C队长怎么看") stays.
        bounded = [m for m in hits if content[m.end():m.end() + 1] in ('', ' ', '\u00a0', '\n', '@')]
        chosen = bounded or hits[:len(mine)]
        for m in sorted(chosen, key=lambda m: m.start(), reverse=True):
            end = m.end() + (content[m.end():m.end() + 1] in (' ', '\u00a0'))
            content = content[:m.start()] + content[end:]
        return {**event, 'content': content.strip(), 'mentions': drop_mentions(mentions, own)}

    def member_text(self, event) -> dict:
        """A member's message with its lines that read like the Bridge's own labels (发送者：…,
        被回复消息：…) quoted, so only the Bridge's first line names the sender."""
        return {**event, 'content': quote_label_lines(str(event.get('content') or ''))}

    def sender_label(self, event) -> str:
        """Who sent a group message, as the line its prompt starts with."""
        sender = str(event.get('sender_id') or '')
        if self.is_owner(sender):
            return '发送者：主人'
        mentions = event.get('mentions') if isinstance(event.get('mentions'), list) else []
        name = next((str(m.get('name') or '') for m in mentions
                     if isinstance(m, dict) and mention_id(m) == sender and m.get('name')), '')
        return '发送者：群成员 ' + (name or sender[-8:])

    def _save(self, chat_id, change):
        """Apply ``change(record)`` to the group's record under its lock; no Feishu call inside."""
        return save_group(self.runtime.state_dir, chat_id, change)

    def _external(self, ctx, chat_id):
        """True or False as Feishu says; None when the lookup failed (logged)."""
        data = self.lookup(ctx.profile, chat_id)
        external = (data or {}).get('external')
        if not isinstance(external, bool):
            self.runtime.log(f'group lookup failed chat={chat_id[-8:]}; not activated')
            return None
        return external

    def _activate(self, record, owner, external, chat_id):
        """core/groups.py ``activated``, except where the group cannot be a team space (``team_allowed``): the lookup is
        kept, the group stays inactive."""
        if self.team_allowed(chat_id):
            return activated(record, owner, external)
        record.update(external=external, active=False)
        return record

    def owner_message(self, ctx, chat_id, owner):
        """The owner @-ed the bot in group ``chat_id``: activate it the first time, and after a
        failed lookup, unless the owner turned it off."""
        record = self.group(chat_id)
        if record and (record.get('external') is not None or record.get('deactivated_at')):
            return
        external = self._external(ctx, chat_id)
        if external is None:
            # Still known as a group (its topics offer no fork); the owner's next @ looks again.
            self._save(chat_id, lambda current: {**current, 'lookup_failed_at': utc_now_iso()})
            return

        def decide(current):
            if current.get('external') is not None or current.get('deactivated_at'):
                return None  # Decided while the lookup ran.
            return self._activate(current, owner, external, chat_id)
        record = self._save(chat_id, decide)
        self.runtime.log(f"group chat={chat_id[-8:]} external={external} active={record.get('active')}")

    def groups(self) -> list[dict]:
        """Every group record (core/groups.py list_groups)."""
        return list_groups(self.runtime.state_dir)

    def switch(self, ctx, chat_id, on) -> str:
        """停用 / 启用 of a group from the owner (the 群管理 card); what happened, to follow the group's name."""
        owner = self.runtime.owner_open_id
        if not on:
            def off(record):
                record.update(active=False, deactivated_at=utc_now_iso(), deactivated_by=owner)
                return record
            self._save(chat_id, off)
            self.runtime.log(f'group chat={chat_id[-8:]} turned off')
            return '已停用：只处理主人的消息和操作。'
        if not self.team_allowed(chat_id):
            return '这个群是主人的个人空间，或部署设置不允许团队空间，不能启用。'
        external = self.group(chat_id).get('external')
        if external is None:
            external = self._external(ctx, chat_id)
        if external is None:
            return '暂时无法确认这个群的信息，没有启用，请稍后再试。'
        record = self._save(chat_id, lambda current: self._activate(current, owner, external, chat_id))
        self.runtime.log(f"group chat={chat_id[-8:]} external={external} active={record.get('active')}")
        return '是外部群，只处理主人的消息，没有启用。' if external else '已启用：群成员 @ 机器人即可发起任务。'

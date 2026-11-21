"""The 群聊 card: the owner turns groups on and off in the private chat (the bot menu's 群聊, app/bot_menu.py).

One standalone card a click: no topic, no job. It lists every group record (Access.groups) with the
name Feishu gave when the card was sent (looked up before it, all at once; saved on the card's record).
停用 / 启用 go through Access.switch, outside the card's lock (enabling may ask Feishu whether the group
is external); then the whole card is drawn again with what happened. The buttons name the state they
set, so a click on an older card or revision still does what it says. Only the owner acts; anyone
else's click is refused on the card. On the bot menu's 设置 card it is the 群聊 tab (app/settings_hub.py).

免@ (app/access.py FREE_ENV): a row's 免@ opens that group's page in the same card (the view's ``free``); opening it
reads the group's members (Access.members, before the lock) into the view, for 你和选中的人. A mode or a member
goes through Access.set_free_talk / pick_member. Without the switch these clicks change nothing and say so.

The card's record is <state>/group-managers/<token>.json; the card is cards/group_manager.py.

The confirmation card (``ask``): the owner's first @ in a group that can be a team space (Access.owner_message) has
one sent to his private chat, as a message of its own (its Markdown when Feishu takes no card) under a key of the
group and its wait; the group's record learns whether it went out (Access.asked), so a card that did not go out is
sent again at his next @ there. Its 启用 and 先不启用 (CONFIRM, ``confirm``) are Access.switch's 启用 and 停用 while
the group waits; once it is decided, here or on the 群聊 card (where a waiting group has the same two buttons), a
click on it changes nothing. Either way the card is then drawn whole with what holds (``redraw``, after the click is
answered: the card has no record, patch_card replaces it).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from covey.app.access import FREE_MODES, FREE_SOME, free_talk_on
from covey.app.card_lanes import after_ack
from covey.app.menu_panel import MenuPanel, hub_top
from covey.core import spaces
from covey.core.groups import pending
from covey.cards.group_manager import (ASKING, CONFIRM, DECIDED, EXTERNAL_ONLY, FREE, FREE_CHOICES, INACTIVE_ONLY,
                                                KINDS, MEMBER, MISSING, OFF, ON, PAGE, PROXY_BLOCKED, SWITCH, VIEW,
                                                ask_card, ask_text, folded_card, free_limit, group_manager_card,
                                                name_of)
from covey.cards.settings_hub import GROUPS

FOLDER = 'group-managers'
OWNER_ONLY = '⚠️ 仅机器人主人可以管理群。'
GONE = '⚠️ 没有这个群的记录了，卡片已按最新记录重画。'
FREE_OFF = '⚠️ 这个部署没有打开免@，卡片已按最新记录重画。'
LOOKUPS = 8  # Group names read at the same time.


class GroupManager:
    def __init__(self, runtime, messenger, voice):
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.menu = MenuPanel(self, 'group-manager', FOLDER, 'group-manager', tab=GROUPS)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a 群管理 card's or a confirmation card's, else None (app/unified.py PANELS):
        the owner's private chat."""
        if not self.matches(raw):
            return None
        return self.confirm(ctx, event, raw, owner) if raw['kind'] == CONFIRM else self.click(ctx, event, raw, owner)

    def names(self, ctx, groups):
        """{chat_id: name} of the groups Feishu named."""
        def lookup(chat_id):
            try:
                data = self.runtime.access.lookup(ctx.profile, chat_id) or {}
            except Exception:  # noqa: BLE001 - a group without its name is still listed
                return ''
            return str(data.get('name') or '') if isinstance(data, dict) else ''
        chats = [g['chat_id'] for g in groups]
        if not chats:
            return {}
        with ThreadPoolExecutor(max_workers=min(LOOKUPS, len(chats)), thread_name_prefix='group-name') as pool:
            found = list(pool.map(lookup, chats))
        return {chat: name for chat, name in zip(chats, found) if name}

    def listed(self):
        """(the group records to list, {chat_id: space type} or None): with the settings' ``spaces`` section the personal
        groups are listed too (a record of their own, core/spaces.py) and each row says its space type; without, the
        group records as ever."""
        access = self.runtime.access
        groups = access.groups()
        if access.spaces == spaces.DEFAULT:
            return groups, None
        known = {g['chat_id'] for g in groups}
        groups = groups + [{'chat_id': chat, 'active': None, 'external': None} for chat in sorted(access.agent_groups)
                           if chat not in known]
        return groups, {g['chat_id']: access.space_of(g['chat_id']) for g in groups}

    def card(self, view, names):
        access = self.runtime.access
        groups, kinds = self.listed()
        free = {'blocked': {g['chat_id'] for g in groups if access.proxy_chat(g['chat_id'])}} if free_talk_on() else None
        return group_manager_card(view['token'], view, groups, names, top=hub_top(GROUPS, view['token'], view),
                                  free=free, kinds=kinds)

    def members(self, ctx, chat_id):
        """Group ``chat_id``'s users but the owner, as the 免@ page lists them; None when Feishu did not list them."""
        try:
            found = self.runtime.access.members(ctx.profile, chat_id)
        except Exception:  # noqa: BLE001 - the page says it could not read them
            found = None
        if not isinstance(found, list):
            return None
        return [m for m in found if isinstance(m, dict) and m.get('id') and not self.runtime.access.is_owner(m['id'])]

    def lookup(self, ctx):
        """The card record's names, read before its lock (Feishu calls)."""
        groups, kinds = self.listed()
        names = self.names(ctx, groups)
        for chat, kind in (kinds or {}).items():  # The records the 群聊 tab shows (core/spaces.py).
            if kind:
                try:
                    spaces.save_space(ctx.state_dir, chat, kind, names.get(chat, ''))
                except OSError as exc:
                    self.runtime.log(f'space record not saved chat={chat[-8:]} ({type(exc).__name__})')
        return {'names': names}

    def menu_items(self):
        """The bot menu's 群聊 (app/unified.py menu)."""
        return {"manage_groups": self.open}

    def menu_folds(self):
        """A second 群聊 on its card, the newest in the chat (app/bot_menu.py)."""
        return {"manage_groups": self.fold}

    def fold(self, ctx, chat_id, source, message_id):
        """The card the click ``source`` sent folded, or drawn again with every group's name of now."""
        return self.menu.toggle(ctx, chat_id, source, message_id,
                                lambda token, record: self.card(record['view'], record['names']),
                                lambda token, record: folded_card(),
                                view=lambda token: {'token': token, 'revision': 1, 'page': 0},
                                fields=lambda: self.lookup(ctx))

    def open(self, ctx, chat_id, source, shown=None):
        """Send a new 群聊 card to ``chat_id`` (the owner's private chat); ``source``, the menu click's
        event_id, keys it; ``shown`` hears its message id."""
        self.menu.open(ctx, chat_id, source, lambda token, record: self.card(record['view'], record['names']),
                       '群聊卡未能发送，请稍后再点机器人菜单「群聊」。',
                       view=lambda token: {'token': token, 'revision': 1, 'page': 0},
                       fields=lambda: self.lookup(ctx),
                       opened=lambda token: self.runtime.log(
                           f'group manager opened token={token[:8]} groups={len(self.runtime.access.groups())}'),
                       shown=shown)

    # The 设置 card's 群聊 tab (app/settings_hub.py): its first view, its record's fields (every group's name, read
    # before the record's lock), its card, once shown.
    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'page': 0, 'hub': True}

    def tab_fields(self, ctx):
        return self.lookup(ctx)

    def tab_card(self, ctx, token, record):
        return self.card(record['view'], record['names'] if isinstance(record.get('names'), dict) else {})

    def tab_shown(self, ctx, token):
        self.runtime.log(f'group manager shown token={token[:8]} groups={len(self.runtime.access.groups())}')

    def refuse(self, ctx, path, event):
        """OWNER_ONLY on the card ``event`` clicked; whether it was a 群聊 card's."""
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.card(
            view, record['names'] if isinstance(record.get('names'), dict) else {}))

    def click(self, ctx, event, value, owner):
        """A click on a 群聊 card; the outcome its ``card action`` line records."""
        entry = self.menu.entry(ctx, event, value)
        if entry is None:
            return 'group_unknown'
        token, path, record = entry
        names = record.get('names') if isinstance(record.get('names'), dict) else {}
        outcome, changes = 'group_manager', {}
        if not owner:
            outcome, changes['notice'] = 'owner_only', OWNER_ONLY
        elif value['kind'] == SWITCH:
            changes['notice'] = self.switch(ctx, value.get('chat'), value.get('on'), names)
        elif value['kind'] == PAGE:
            page = value.get('page')
            key = 'member_page' if value.get('members') else 'page'
            changes = {key: max(0, page), 'notice': ''} if isinstance(page, int) and not isinstance(page, bool) else {}
        elif not free_talk_on():
            changes = {'free': '', 'members': None, 'notice': FREE_OFF}
        elif value['kind'] == VIEW:
            changes = self.view(ctx, value.get('chat'))
        elif value['kind'] == FREE:
            changes = self.free(ctx, value.get('chat'), value.get('mode'), names, record['view'])
        elif value['kind'] == MEMBER:
            changes = self.pick(value.get('chat'), value.get('member'), value.get('on'), record['view'])
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)  # Read again: another click may have drawn it since.
            if record is None:
                return 'group_unknown'
            view = {**record['view'], **changes, 'token': token}
            view['revision'] += 1
            if not self.menu.publish(ctx, path, record, view, self.card(view, names)) and changes.get('notice'):
                self.menu.unsaved(ctx, record, changes['notice'] + '\n群聊卡未能刷新，请重新打开。', f"{token}-{view['revision']}")
        return outcome

    def view(self, ctx, chat_id):
        """The view's changes for 免@ of group ``chat_id`` (its page, with its members read) or '' (the list)."""
        if not chat_id:
            return {'free': '', 'members': None, 'member_page': 0, 'notice': ''}
        if not isinstance(chat_id, str) or not self.runtime.access.group(chat_id):
            return {'free': '', 'members': None, 'notice': GONE}
        return {'free': chat_id, 'members': self.members(ctx, chat_id), 'member_page': 0, 'notice': ''}

    def free(self, ctx, chat_id, mode, names, view):
        """Group ``chat_id``'s 免@ set to ``mode``; the view's changes with the line the card shows."""
        access = self.runtime.access
        record = access.group(chat_id) if isinstance(chat_id, str) else {}
        if not record:
            return {'free': '', 'members': None, 'notice': GONE}
        if mode not in ('', *FREE_MODES):
            return {}
        limit = free_limit(record, access.proxy_chat(chat_id))
        if mode and (limit == PROXY_BLOCKED or limit in (EXTERNAL_ONLY, INACTIVE_ONLY) and mode != FREE_MODES[0]):
            return {'free': chat_id, 'notice': '⚠️ ' + limit}
        access.set_free_talk(chat_id, mode)
        changes = {'free': chat_id, 'notice': f"✓ 群「{name_of(record, names)}」免@ 改成：{FREE_CHOICES[mode][0]}"}
        if mode == FREE_SOME and (view.get('free') != chat_id or view.get('members') is None):
            changes.update(members=self.members(ctx, chat_id), member_page=0)
        return changes

    def pick(self, chat_id, member, on, view):
        """``member`` picked for or dropped from group ``chat_id``'s 你和选中的人; the view's changes."""
        access = self.runtime.access
        if not isinstance(chat_id, str) or not access.group(chat_id):
            return {'free': '', 'members': None, 'notice': GONE}
        if not isinstance(member, str) or not member.startswith('ou_') or not isinstance(on, bool):
            return {}
        if on == (member in access.free_members(chat_id)):
            return {'free': chat_id, 'notice': ''}  # Done already: an older card's button.
        access.pick_member(chat_id, member, on)
        listed = {m.get('id'): m.get('name') for m in view.get('members') or [] if isinstance(m, dict)}
        who = listed.get(member) or '…' + member[-6:]
        return {'free': chat_id, 'notice': f"✓ {'选上了' if on else '取消了'} {who}"}

    def switch(self, ctx, chat_id, on, names):
        """停用 or 启用 of one group (a waiting group's 先不启用 is its 停用); the line the card shows."""
        access = self.runtime.access
        if not isinstance(chat_id, str) or not isinstance(on, bool) or not access.group(chat_id):
            return GONE
        record = access.group(chat_id)
        name = name_of(record, names)
        if on == (record.get('active') is True) and (on or not pending(record)):
            return f"群「{name}」已经是{'启用' if on else '停用'}的了。"
        reply = access.switch(ctx, chat_id, on)
        return ('✓ ' if reply.startswith('已') else '⚠️ ') + f'群「{name}」' + reply

    # The confirmation card ------------------------------------------------------------------------------------------

    def ask(self, ctx, wanted):
        """The confirmation card ``wanted`` (an Ask of Access.owner_message) sent to the owner's private chat; the
        group's record learns whether it went out (Access.asked). A send that raises went nowhere: the owner's message
        it came with is served all the same."""
        access, tail = self.runtime.access, wanted.chat_id[-8:]
        chat, cards, sent = access.owner_chat(), [], False
        try:
            if chat:  # An @ after a card that did not go out ran no lookup: the name is read now.
                name = wanted.name or self.names(ctx, [{'chat_id': wanted.chat_id}]).get(wanted.chat_id, '')
                view = {'chat': wanted.chat_id, 'name': name, 'state': ASKING}
                key = self.voice.safe_key(f'group-ask-{wanted.chat_id}-{wanted.since}')  # One card a wait.
                sent = self.messenger.send_message(ctx.profile, chat, ask_text(view), key, voice=self.voice,
                                                   card=ask_card(view), on_card=cards.append)
        except Exception as exc:  # noqa: BLE001 - the card is the one thing missing; his next @ asks again
            self.runtime.log(f'group chat={tail} confirmation card failed: {type(exc).__name__}')
        access.asked(wanted, cards[0] if cards else '', sent)
        if sent:
            self.runtime.log(f"group chat={tail} confirmation card sent card={'yes' if cards else 'no'}")
        else:
            why = 'delivery failed' if chat else 'no private chat with the owner yet'
            self.runtime.log(f"group chat={tail} confirmation card not sent ({why}); the owner's next @ asks again")

    def confirm(self, ctx, event, value, owner):
        """A click on a confirmation card: 启用 or 先不启用 of its group while it waits for the owner's word, else
        nothing; the card is drawn again after the click is answered. The outcome its ``card action`` line records."""
        chat, on = value.get('chat'), value.get('on')
        if not owner:
            return 'owner_only'
        if not isinstance(chat, str) or not chat or not isinstance(on, bool) or not event.get('message_id'):
            return 'group_unknown'
        access = self.runtime.access
        view = {'chat': chat, 'name': value.get('name') if isinstance(value.get('name'), str) else '', 'notice': ''}
        if not access.group(chat):
            view['state'], outcome = MISSING, 'group_unknown'
        else:
            if access.pending(chat):
                reply = access.switch(ctx, chat, on)  # No lookup: a waiting group is known not to be external.
                outcome = 'group_confirm_on' if on else 'group_confirm_off'
                if not reply.startswith('已'):
                    view['notice'] = '⚠️ ' + reply
            else:
                view['notice'], outcome = DECIDED, 'group_confirm_decided'
            view['state'] = ON if access.active(chat) else OFF
        after_ack(lambda: self.redraw(ctx, event, view))
        return outcome

    def redraw(self, ctx, event, view):
        """The confirmation card ``event`` clicked drawn whole for ``view``; when Feishu does not take it, what holds is
        said in the chat instead."""
        message, chat = str(event.get('message_id') or ''), str(event.get('chat_id') or '')
        if self.messenger.patch_card(ctx.profile, message, ask_card(view)):
            return
        self.runtime.log(f"group chat={view['chat'][-8:]} confirmation card not redrawn message={message[-8:]}")
        if chat:
            key = self.voice.safe_key(f"group-asked-{message}-{event.get('event_id') or ''}")
            self.messenger.send_message(ctx.profile, chat, ask_text(view), key, voice=self.voice)

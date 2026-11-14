"""The menu card of a personal group (core/spaces.py, ``spaces.personal_groups``).

Feishu shows the bot menu only in a private chat, and a group's own menu (群菜单, im/v1/chats/:chat_id/menu_tree)
only opens links: a click there never reaches the bot. So each personal group gets one
card of the menu's items as buttons, sent and pinned by this feature's thread (``group-menu.json``: chat_id ->
``message_id``, ``version``, ``pinned_at``; a card of an older VERSION is taken off the pins and replaced). A click
is the owner's only and runs the item as a menu click does (BotMenu.run), its card in that group, once the click is
acknowledged. The group's kind and title are noted on its space record (core/spaces.py ``save_space``) when the card
is looked after.
"""
from __future__ import annotations

import hashlib
import time

from covey.app.card_lanes import after_ack
from covey.cards.elements import callback_button, note, panel_header, row, styled_panel
from covey.core.locks import resource_lock
from covey.core.loops import every
from covey.core.spaces import PERSONAL, read_space, save_space
from covey.core.state import claim_once, read_object, write_json

KIND = 'bridge_group_menu'
RECORD = 'group-menu.json'
VERSION = 1
TICK = 600  # Seconds between looks: a card missing or not pinned is tried again.
ITEMS = (('weekly_usage', '用量'), ('digest_board', '看板'), ('bridge_settings', '设置'))


def group_menu_card(items):
    """The card: one button for each (event key, label) of ``items``."""
    buttons = [(callback_button(label, {'kind': KIND, 'key': key}, width='fill'), 1) for key, label in items]
    return styled_panel(panel_header('waiting_outlined', '菜单'),
                        [row(*buttons, flex='none'), note('点了在这个群里出卡 · 私聊里用输入框上方的菜单')],
                        '菜单 · ' + ' · '.join(label for _, label in items))


class GroupMenu:
    """``menu``: the Bridge's BotMenu (its ``actions``, ``run`` and ``inbound``)."""

    def __init__(self, runtime, messenger, voice, menu):
        self.runtime, self.messenger, self.voice, self.menu = runtime, messenger, voice, menu

    def items(self):
        return tuple((key, label) for key, label in ITEMS if key in self.menu.actions)

    def threads(self):
        """Its worker, only with personal groups (app/unified.py worker_targets, after the harness config is read)."""
        return (('group-menu', lambda ctx: every(self.runtime, TICK, self.tick, args=(ctx,), isolate='group-menu')),
                ) if self.runtime.access.agent_groups else ()

    def tick(self, ctx):
        items = self.items()
        for chat in sorted(self.runtime.access.agent_groups):
            self.note(ctx, chat)
            if items:
                self.keep(ctx, chat, items)

    def note(self, ctx, chat):
        """``chat``'s space record (kind personal, its title from Feishu while it has none); a failure is a log line."""
        try:
            title = read_space(ctx.state_dir, chat).get('title') or ''
            if not title:
                data = self.runtime.access.lookup(ctx.profile, chat)
                title = str((data or {}).get('name') or '') if isinstance(data, dict) else ''
            save_space(ctx.state_dir, chat, PERSONAL, title)
        except Exception as exc:  # noqa: BLE001 - the card goes on without a record
            self.runtime.log(f"space record not saved chat={chat[-8:]} ({type(exc).__name__})")

    def keep(self, ctx, chat, items):
        """``chat``'s card out and pinned; one of an older VERSION taken off the pins first."""
        path = ctx.state_dir / RECORD
        entry = read_object(path).get(chat)
        entry = entry if isinstance(entry, dict) else {}
        if entry.get('message_id') and entry.get('version') != VERSION:
            unpinned = self.messenger.unpin(ctx.profile, entry['message_id']) if entry.get('pinned_at') else None
            self.runtime.log(f"group menu retired chat={chat[-8:]} unpinned={unpinned}")
            entry = {}
        if not entry.get('message_id'):
            key = self.voice.key('group-menu', f"{chat}-{VERSION}")
            _, mid = self.messenger.create_and_send_card(ctx.profile, chat, group_menu_card(items), key, voice=self.voice)
            if not mid:
                self.runtime.log(f"group menu not sent chat={chat[-8:]}")
                return
            entry = {'message_id': mid, 'version': VERSION, 'sent_at': time.time()}
            self.save(path, chat, entry)
            self.runtime.log(f"group menu sent chat={chat[-8:]} message={mid[-8:]}")
        if not entry.get('pinned_at'):
            if not self.messenger.pin(ctx.profile, entry['message_id']):
                self.runtime.log(f"group menu pin failed chat={chat[-8:]}")
                return
            self.save(path, chat, {**entry, 'pinned_at': time.time()})
            self.runtime.log(f"group menu pinned chat={chat[-8:]} message={entry['message_id'][-8:]}")

    @staticmethod
    def save(path, chat, entry):
        with resource_lock('group-menu', str(path)):
            record = read_object(path)
            record[chat] = entry
            write_json(path, record)

    @staticmethod
    def matches(raw):
        return isinstance(raw, dict) and raw.get('kind') == KIND

    def claim(self, ctx, event, raw, owner):
        """A click on the card (app/unified.py PANELS), else None: the item's card in the group, after the ack."""
        if not self.matches(raw):
            return None
        if not owner:
            return 'owner_only'
        chat, key = str(event.get('chat_id') or ''), str(raw.get('key') or '')
        event_id = str(event.get('event_id') or '')
        if chat not in self.runtime.access.agent_groups or key not in dict(self.items()) or not event_id:
            return 'group_menu_ignored'
        if not claim_once(ctx.state_dir / self.menu.inbound,
                          hashlib.sha256(('group-menu:' + event_id).encode()).hexdigest()):
            return 'duplicate'
        after_ack(lambda: self.menu.run(ctx, key, chat, event_id))
        return 'group_menu'

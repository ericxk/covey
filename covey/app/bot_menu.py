"""The bot's custom menu (机器人自定义菜单): the owner's commands, since typed ones misfired.

Each menu item is set to 推送事件 in the Feishu console with its event key (the keys of app/unified.py's
``menu`` table, from each feature's ``menu_items()``); a click arrives as ``application.bot.menu_v6``
(feishu/events.py consume) with the clicker's open_id and no chat. Only the owner is served
(``Runtime.access.is_owner``); anyone else, and a key not in that table, is one log line. Feishu delivers at least once: an ``event_id`` seen before is skipped, the
markers beside the messages' (card_routing.py INBOUND_DIR).

The answer is a new message in the owner's private chat with the bot, keyed by the ``event_id``. That
chat is the one Access saved from the owner's last direct message (``owner-chat.json``); before any,
the one an older 代答 card or 敏感词看板 link went to; before those, one message sent to the owner's open_id
(FIRST_TEXT) opens it and its chat is saved. A click by an owner account of a space setting's (app/access.py
``owner_accounts``) answers in that account's own private chat (``Access.account_chat``), opened the same way when
not known yet; the menu card of a personal group (app/group_menu.py) answers in that group (``run``).

Each item's card replaces the one it sent before: the action calls
``shown(message_id)`` once its card is out, and Panels recalls the item's previous card in that chat,
so the chat keeps one card of each.

点一下弹出，再点一下收回: when the item's last card is still the newest top-level message in
the chat, a click folds it in place to one line, and the next unfolds it with fresh figures; no new message, no
recall. The item's ``fold(ctx, chat_id, source, message_id)`` (``menu_folds()`` of its feature) does that through the
card's own record and says 'fold' or 'unfold'; '' (a card from before it had a record, or closed) sends a new card as
before. "Still the newest" is what this process knows (Panels ``newest``): the card was sent after this process
started, after the owner's last top-level message there (``owner_said``, kept in memory only) and after every other
item's last card there. Anything else, and anything unsure, sends a new card.
"""
from __future__ import annotations

import functools
import hashlib
import time

from covey.core import app_manifest, timing
from covey.core.locks import resource_lock
from covey.core.state import claim_once, read_object, utc_now_iso, write_json
from covey.feishu.message_parts import collect_message_parts, text_message_texts
from covey.feishu.message_read import fetch_message_detail, message_item
from covey.feishu.messenger import Messenger

FIRST_TEXT = '菜单的结果都发在这个私聊里。'
PANELS = 'menu-panels.json'  # event_key -> the chat and message of the card it last sent.
# Personal groups open only these three (same as app/group_menu.py ITEMS); 代答 stays in the owner's private chat.
GROUP_SLASH_KEYS = frozenset({'weekly_usage', 'digest_board', 'bridge_settings'})
SLASH_MESSAGE_TYPES = frozenset({'', 'text', 'post'})
MOVED_TEXT = '「{name}」已移到「设置」卡的「{tab}」页签：发 /设置 或点菜单的「设置」打开。'  # An older menu item cached by a client.
# A stale item whose page went with its feature: 敏感词脱敏 was taken out (T947, docs/legacy.md).
GONE_TEXT = {'redaction_dashboard': '敏感词脱敏已下线，菜单里的「脱敏」没有用了。'}


def slash_key(event):
    """The bot menu event key a top-level slash command message names (/settings, /设置, …), else ''.

    Never matches replies inside a topic thread, quoted replies, messages with images/files, or text with extra words."""
    if any(str(event.get(field) or '') for field in ('thread_id', 'root_id', 'reply_to', 'parent_id')):
        return ''
    if str(event.get('message_type') or '') not in SLASH_MESSAGE_TYPES:
        return ''
    raw = str(event.get('content') or '')
    parts, images, files = collect_message_parts(raw)
    if not parts and not images and not files:
        texts = text_message_texts(raw)
        if texts:
            parts, images, files = collect_message_parts('\n'.join(texts))
    if images or files or len(parts) != 1:
        return ''
    return app_manifest.slash_key(parts[0])


class Panels:
    """The card each menu item last sent; a newer one recalls it. ``messenger.recall`` is a Feishu call,
    so it happens after the record is written and its lock let go; a refusal (past the tenant's
    recall window, already gone) is one log line and the old card stays."""

    def __init__(self, runtime, messenger):
        self.runtime, self.messenger = runtime, messenger

    def shown(self, ctx, key, chat_id, message_id, source=''):
        """``key``'s new card ``message_id`` in ``chat_id``, opened by the click ``source`` (which keys its record), is
        out: recorded with when (``sent``, epoch seconds), and the card before it recalled."""
        if not message_id:
            return
        path = ctx.state_dir / PANELS
        with resource_lock('menu-panels', str(path)):
            record = read_object(path)
            before = record.get(key) if isinstance(record.get(key), dict) else {}
            record[key] = {'chat_id': chat_id, 'message_id': message_id, 'at': utc_now_iso(), 'sent': time.time(),
                           **({'source': source} if source else {})}
            write_json(path, record)
        old = str(before.get('message_id') or '')
        if old and old != message_id and before.get('chat_id') == chat_id:
            done = self.messenger.recall(ctx.profile, old)
            self.runtime.log(f'menu panel {key} recalled={done} old={old[-8:]}')

    @staticmethod
    def newest(ctx, key, chat_id, since):
        """``key``'s last card in ``chat_id`` when it was sent after ``since`` (epoch seconds) and after every other
        item's last card there, with the click that opened it: its entry, else None. An entry from before ``sent``
        and ``source`` were kept is never the newest."""
        record = read_object(ctx.state_dir / PANELS)
        entry = record.get(key)
        if not isinstance(entry, dict) or entry.get('chat_id') != chat_id:
            return None
        sent, message_id, source = entry.get('sent'), entry.get('message_id'), entry.get('source')
        if not isinstance(sent, (int, float)) or sent <= since or not message_id or not source:
            return None
        for other, value in record.items():
            if other != key and isinstance(value, dict) and value.get('chat_id') == chat_id \
                    and isinstance(value.get('sent'), (int, float)) and value['sent'] >= sent:
                return None
        return entry


def click_time(event):
    """When the menu click ``event`` was made, epoch seconds, from Feishu's stamp on it (milliseconds, or finer: the unit
    is told by size, as core/timing.py ``delay_ms``); None without one."""
    for name in ('menu_timestamp', 'timestamp'):
        try:
            value = int(str(event.get(name)))
        except (TypeError, ValueError):
            continue
        while value >= 10 ** 11:  # Epoch seconds stay below this until the year 5138.
            value /= 1000
        if value > 0:
            return float(value)
    return None


def open_owner_chat(profile, open_id, key, log):
    """Send FIRST_TEXT to ``open_id``: the private chat it went to, '' when unknown."""
    result = Messenger(log).send_to_user(profile, open_id, FIRST_TEXT, key)
    data = (result or {}).get('data') if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    chat_id = str(data.get('chat_id') or '')
    if not chat_id and data.get('message_id'):
        item = message_item(fetch_message_detail(str(data['message_id']), profile), str(data['message_id'])) or {}
        chat_id = str(item.get('chat_id') or '')
    return chat_id


class BotMenu:
    """``actions``: event key -> ``action(ctx, chat_id, source, shown=...)``, ``source`` the click's
    event_id, ``shown(message_id)`` to call with the card it sent (Panels). ``folds``: event key ->
    ``fold(ctx, chat_id, source, message_id)`` (the module docstring). ``hints(ctx)``: private chats
    with the owner known from before owner-chat.json; ``opener`` as open_owner_chat (tests replace it)."""

    def __init__(self, runtime, inbound, actions, panels, hints=lambda ctx: (), opener=open_owner_chat, folds=None):
        self.runtime, self.inbound, self.actions, self.panels = runtime, inbound, actions, panels
        self.hints, self.opener, self.folds = hints, opener, dict(folds or {})
        self.actions = {**actions, **{key: functools.partial(self.moved, key)  # Items taken off the app's menu:
                                      for key in app_manifest.STALE_MENU_KEYS}}  # a cached copy still answers.
        self.started = time.time()  # A card sent before is not known to be the newest.
        self.said = {}  # chat_id -> when the owner's last top-level message there came (time.time()).

    def moved(self, key, ctx, chat_id, source, shown=None):
        """The answer to a stale menu item (core/app_manifest.py STALE_MENU_KEYS): where its card went, or that it went."""
        name = app_manifest.STALE_MENU_KEYS[key]
        self.panels.messenger.send_text(ctx.profile, chat_id, GONE_TEXT.get(key) or MOVED_TEXT.format(name=name, tab=name),
                                        'menu-moved-' + hashlib.sha256(source.encode()).hexdigest()[:24])

    def owner_said(self, chat_id):
        """The owner wrote at the top level of ``chat_id`` (not in a thread): the menu's cards there are older."""
        if chat_id:
            self.said[chat_id] = time.time()

    def shown(self, ctx, key, chat_id, source=''):
        """What an action calls with the message id of the card it sent for ``key`` to ``chat_id`` for the click
        ``source``."""
        return functools.partial(self.panels.shown, ctx, key, chat_id, source=source)

    def handle(self, event, ctx):
        """One menu click; the outcome its log line records."""
        operator = str(event.get('operator_open_id') or event.get('operator_id') or '')
        key, event_id = str(event.get('event_key') or ''), str(event.get('event_id') or '')
        if not self.runtime.access.is_owner(operator):
            return 'not_owner'
        action = self.actions.get(key)
        if action is None:
            return 'unknown_key'
        if not event_id:
            return 'no_event_id'
        if not claim_once(ctx.state_dir / self.inbound, hashlib.sha256(('menu:' + event_id).encode()).hexdigest()):
            return 'duplicate'
        timing.mark('dedup')
        chat_id = self.owner_chat(ctx, event_id, operator)
        timing.mark('chat')
        if not chat_id:
            return 'no_chat'
        how = self.run(ctx, key, chat_id, event_id, clicked=click_time(event))
        return 'twice' if how == 'twice' else key

    def handle_slash(self, ctx, event, admitted, *, personal_group=False):
        """A top-level slash command message (/usage, /board, /proxy, /settings or their Chinese aliases) from the
        owner in a private chat or personal group: opens the menu card in that chat without starting a topic.
        Returns True when handled, False when the message should continue to normal routing."""
        if not getattr(admitted, 'owner', False):
            return False
        if admitted.group and not personal_group:
            return False
        key = slash_key(event)
        if not key:
            return False
        if admitted.group and key not in GROUP_SLASH_KEYS:
            return False
        if key not in self.actions:
            return False
        chat_id = str(event.get('chat_id') or '')
        source = str(event.get('message_id') or event.get('event_id') or '')
        if not chat_id or not source:
            return False
        how = self.run(ctx, key, chat_id, source)
        timing.outcome(f'slash:{key}')
        self.runtime.log(f'slash menu key={key} message={source[-10:]} chat={chat_id[-8:]} outcome={how}')
        return True

    def run(self, ctx, key, chat_id, source, clicked=None):
        """Item ``key``'s card in ``chat_id`` for the click ``source``: its last card there folded or unfolded while it
        is the newest, else a new one. A click made (``clicked``, epoch seconds) before that card was out is the owner
        clicking again while it was on its way (a cold 看板 took seven seconds): 'twice', nothing done, where it used to
        fold the card he was waiting for. What it did is the return value."""
        entry = self.panels.newest(ctx, key, chat_id, max(self.started, self.said.get(chat_id, 0)))
        if entry is not None and clicked is not None and entry['sent'] > clicked:
            how = 'twice'
        else:
            how = self.fold(ctx, key, chat_id, entry)
        if not how:
            how = 'new'
            self.actions[key](ctx, chat_id, source, shown=self.shown(ctx, key, chat_id, source))
        self.runtime.log(f'menu panel {key} {how}')
        return how

    def fold(self, ctx, key, chat_id, entry):
        """'fold' or 'unfold' when ``key``'s last card (``entry``, Panels ``newest``) was the newest in ``chat_id`` and
        its feature turned it; '' when a new card is to be sent."""
        fold = self.folds.get(key)
        if fold is None or entry is None:
            return ''
        return fold(ctx, chat_id, entry['source'], entry['message_id']) or ''

    def owner_chat(self, ctx, event_id, operator=''):
        access = self.runtime.access
        if operator and operator != self.runtime.owner_open_id and access.is_owner(operator):
            return self.account_chat(ctx, event_id, operator)
        chat_id = access.owner_chat()
        if chat_id:
            return chat_id
        chat_id = next((c for c in self.hints(ctx) if c), '') or self.opener(
            ctx.profile, self.runtime.owner_open_id, 'menu-chat-' + hashlib.sha256(event_id.encode()).hexdigest()[:24],
            self.runtime.log)
        if chat_id:
            access.remember_owner_chat(chat_id)
        return access.owner_chat() or chat_id

    def account_chat(self, ctx, event_id, operator):
        """Owner account ``operator``'s own private chat (Access.account_chat), opened by FIRST_TEXT when not known."""
        access = self.runtime.access
        chat_id = access.account_chat(operator)
        if chat_id:
            return chat_id
        chat_id = self.opener(ctx.profile, operator,
                              'menu-chat-' + hashlib.sha256((operator + event_id).encode()).hexdigest()[:24],
                              self.runtime.log)
        access.remember_account_chat(operator, chat_id)
        return chat_id


def known_chats(proxy_panel, ctx):
    """The private chats an older 代答 card and 敏感词看板 link were sent to (each only opens there). The link's is in the
    record the web dashboard kept (taken out, and 敏感词脱敏 with it), read while a state directory still has it."""
    return [proxy_panel.owner_chat(ctx), str(read_object(ctx.state_dir / 'redaction' / 'dashboard-entry.json').get('chat_id') or '')]

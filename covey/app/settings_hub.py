"""The 设置 card: the bot menu's 设置 (event key MENU_KEY) puts the butler's settings in two pages, 新话题 and 管家
(app/butler_panel.py), the workspace list with each workspace's 用途 (app/workspace_manager.py), the redaction term
list (app/redaction_panel.py), the 群聊 (app/group_manager.py) and, with nodes, 设备 (app/node_panel.py) panels on one
card in the owner's private chat, a tab row on top (cards/settings_hub.py: 新话题 · 管家 · 工作区 · 脱敏 · 群聊 · 设备;
the owner, 2026-11-19). 代答 is the bot menu's own item again; a record an older card left on its tab is still drawn
there (app/proxy_panel.py). A tab turns the same card to the other panel in place: no new message, no recall. The page 其他 is
retired (2026-11-09): an older card's tab click on it, or a record still on it, lands on 工作区 (cards/settings_hub.py
RETIRED), its buttons on the page that has their row now (app/butler_panel.py PAGE_OF).

One record owns the card: <state>/settings-cards/<token>.json (app/menu_panel.py HUB_FOLDER), with its card_id,
message_id and sequence, the tab it shows (``tab``) and that tab's view (``view``, ``hub`` in it: the panel draws the
tab row above its body). The panels only draw their tab (``tab_view``, ``tab_fields``, ``tab_card``, ``tab_shown``)
and handle their own buttons on it: the buttons carry this record's token, a panel finds the record by it and acts
only while the card shows its tab, under the record's one lock (MenuPanel ``lock``, ``clicked``). Every write takes
its sequence from this record (rule 3). A tab click, the owner's only:

1. under the record's lock, the click is checked to be on this card (its chat and message, the card open, or 代答's
   expired card); Feishu gets its answer at once;
2. after the acknowledgement (card_lanes.after_ack: Feishu wants the answer within 3 s, and the 群聊 tab reads every
   group's name before it draws) the same record turns to the tab, with a fresh view of it, and the whole card is
   drawn. The click's event_id is kept (``turned``): the same click delivered again draws nothing more.

点一下弹出，再点一下收回 (the owner, 2026-11-06): a click on the tab the card shows folds it in place to that panel's title
bar and the tab row (cards/settings_hub.py folded_hub; the record keeps FOLDED), and the next unfolds it with a fresh
view of the tab, as steps 1 and 2 above. Another tab turns the card to it unfolded. The bot menu's 设置 again, while
this card is the newest top-level message in the chat (app/bot_menu.py), folds and unfolds it the same way (``fold``).

A card sent before this record existed (gen 286) is held by its panel's own record; its buttons still act there. Its
tab click moves that record here under the same token (``takeover``: its sequence, view and fields go on, the old
record is MOVED) and then turns as above. A card whose record was MOVED by the scheme before finds nothing.

The menu opens it on 新话题 (``first_tab``), in either private chat mode and with COVEY_BUTLER=0 too. 管家 (BUTLER,
the key of the one 管家 tab before the pages: an older card's tab click there lands on it) is a tab unless
COVEY_BUTLER=0 rules the butler out, in the mode 纯话题 too (its row of the mode turns the butler on). The butler's menu
key (app/butler_panel.py MENU_KEY) and its tool settings_panel send it on 管家; the bot menu's keys before 2026-11-09, 工作区
(app/workspace_manager.py MENU_KEY) and 脱敏 (REDACTION_KEY), on their tabs (``opens``: the console's
menu shows them until the owner publishes the new one). The chat keeps one 设置 card (app/bot_menu.py Panels, key
MENU_KEY). The bot menu's 代答 and 群聊 keys send their own cards, without tabs.
"""
from __future__ import annotations

import functools
import re

from covey.app import butler_panel, workspace_manager
from covey.app.bot_menu import Panels
from covey.app.card_lanes import after_ack
from covey.app.form_cards import save_record, settle
from covey.app.menu_panel import FOLDED, HUB_FOLDER, HUB_LOCK, MOVED, OPEN, MenuPanel, hub_tabs
from covey.app.proxy_panel import EXPIRED
from covey.cards.settings_hub import (GROUPS, KINDS, LABELS, BUTLER, BUTLER_PAGES, NODES, PROXY, REDACTION, WORKSPACE,
                                               folded_hub, successor)
from covey.core import butler
from covey.core.state import read_json

MENU_KEY = 'bridge_settings'
REDACTION_KEY = 'redaction_dashboard'  # The bot menu's 脱敏 before 2026-11-09 (core/app_manifest.py OLD_MENU_KEYS).
KIND = 'settings-card'
UNSAVED = '⚠️ 设置卡未能切换到「{}」，请重新点机器人菜单「设置」。'
UNFOLDED = '⚠️ 设置卡未能{}，请再点一次「{}」。'
FAILED = '设置卡未能发送，请稍后再点机器人菜单「设置」。'
HELD = (OPEN, EXPIRED)  # The record's statuses in which the card shows the tab row (EXPIRED: 代答's, app/proxy_panel.py).
TURNS = 8  # The last tab clicks' event_ids the record keeps.


class SettingsHub:
    def __init__(self, runtime, messenger, voice, main, proxy, groups, workspaces, redaction, nodes=None):
        """``main``, ``workspaces``, ``redaction``, ``proxy``, ``groups``: the ButlerPanel (its two pages),
        WorkspaceManager, RedactionPanel, ProxyPanel and GroupManager the tabs show. The butler's buttons on the 工作区
        tab redraw it through the WorkspaceManager (ButlerPanel.pages)."""
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.panels = {**dict.fromkeys(BUTLER_PAGES, main), WORKSPACE: workspaces, REDACTION: redaction, PROXY: proxy,
                       GROUPS: groups, **({NODES: nodes} if nodes is not None else {})}
        main.pages[WORKSPACE] = workspaces.tab_card
        self.redaction = redaction
        self.recalls = Panels(runtime, messenger)
        self.menu = MenuPanel(self, KIND, HUB_FOLDER, HUB_LOCK)

    def panel(self, tab):
        return self.panels[tab]

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    def token(self, chat_id, source):
        """The token of the card opened for ``source``."""
        return self.menu.token(chat_id, source)

    def menu_items(self):
        """The bot menu's 设置; the keys of the menu before 2026-11-09, 工作区 and 脱敏, on their tabs; unless
        COVEY_BUTLER=0 also the butler's own key, on its tab."""
        items = {MENU_KEY: self.open, workspace_manager.MENU_KEY: self.open_workspaces,
                 REDACTION_KEY: self.open_redaction}
        if butler.available():
            items[butler_panel.MENU_KEY] = self.open_main
        return items

    def menu_folds(self):
        """The bot menu's 设置 again on its card, the newest in the chat (app/bot_menu.py)."""
        return {MENU_KEY: self.fold}

    @staticmethod
    def first_tab():
        """新话题, always the first tab (app/menu_panel.py hub_tabs)."""
        return hub_tabs()[0]

    def open(self, ctx, chat_id, source, shown=None, tab='', quote=''):
        """Send a new 设置 card to ``chat_id`` (the owner's private chat) on ``tab`` (first_tab by default);
        ``source`` keys it, ``shown`` hears its message id, ``quote`` (管家 only) the message it quotes."""
        tab = tab or self.first_tab()
        panel = self.panels[tab]
        self.menu.open(ctx, chat_id, source, lambda token, record: panel.tab_card(ctx, token, record),
                       FAILED, view=panel.tab_view,
                       fields=lambda: {'tab': tab, **panel.tab_fields(ctx)},
                       opened=lambda token: panel.tab_shown(ctx, token), shown=shown, quote=quote if tab == BUTLER else '')

    def open_main(self, ctx, chat_id, source, shown=None, quote=''):
        """The card on 管家, replacing the 设置 card before (not the item's own: there is one 设置 card)."""
        self.open_on(ctx, chat_id, source, BUTLER, quote=quote)

    def open_workspaces(self, ctx, chat_id, source, shown=None):
        """The bot menu's old 工作区: the card on 工作区, as ``open_main``."""
        self.open_on(ctx, chat_id, source, WORKSPACE)

    def open_redaction(self, ctx, chat_id, source, shown=None):
        """The bot menu's old 脱敏: the card on 脱敏, as ``open_main``."""
        self.open_on(ctx, chat_id, source, REDACTION)

    def open_on(self, ctx, chat_id, source, tab, quote=''):
        """The card on ``tab``, recorded as the 设置 card (it recalls the one before)."""
        self.open(ctx, chat_id, source, shown=functools.partial(self.recalls.shown, ctx, MENU_KEY, chat_id,
                                                                source=source), tab=tab, quote=quote)

    def sent(self, ctx, chat_id, source):
        """Whether the card opened for ``source`` went out."""
        try:
            return read_json(self.path(ctx, self.token(chat_id, source))).get('status') in HELD
        except (OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is a tab's, else None (app/unified.py PANELS)."""
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    def click(self, ctx, event, value, owner):
        """A tab click; the outcome its ``card action`` line records."""
        tab, here, token = value.get('tab'), value.get('from'), value.get('token')
        tab, here = successor(tab), successor(here)  # An older card's 其他 is 工作区 now.
        if tab not in self.panels or here not in self.panels or not isinstance(token, str) or not re.fullmatch('[0-9a-f]{24}', token):
            return 'settings_unknown'
        path = self.path(ctx, token)
        if not path.exists() and not self.takeover(ctx, event, here, token, owner):
            # Not an older card's either; the one refused on it is said there.
            return 'owner_only' if not owner and self.panels[here].refuse(ctx, self.panels[here].path(ctx, token), event) \
                else 'settings_unknown'
        with self.menu.lock(path):
            record = self.menu.clicked(path, event, HELD)
        shown = successor((record or {}).get('tab'))
        if shown not in self.panels:
            return 'settings_unknown'
        if not owner:
            # Refused on the card as each panel refuses its own buttons.
            return 'owner_only' if self.panels[shown].refuse(ctx, path, event) else 'settings_unknown'
        chat_id, event_id = str(event.get('chat_id') or ''), str(event.get('event_id') or '')
        if not chat_id or not event_id:
            return 'settings_unknown'
        if tab == shown and record['status'] == OPEN:
            # The tab shown: the card folds, or unfolds when folded.
            after_ack(functools.partial(self.flip, ctx, path, lambda: self.menu.clicked(path, event), event_id))
            return 'settings_unfold' if record.get(FOLDED) else 'settings_fold'
        after_ack(functools.partial(self.turn, ctx, path, event, tab))
        return 'settings_tab'

    def fold(self, ctx, chat_id, source, message_id):
        """The bot menu's 设置 on the card the click ``source`` sent, the newest in the chat: 'fold' or 'unfold'; ''
        when it has no open record (the menu sends a new card)."""
        path = self.path(ctx, self.token(chat_id, source))
        return self.flip(ctx, path, lambda: self.menu.held(path, chat_id, message_id))

    def flip(self, ctx, path, find, event_id=''):
        """The card of the record ``find()`` reads folded to its tab row, or unfolded on its tab with a fresh view (the
        tab's fields read before the lock); ``event_id``: a tab click's, kept as ``turned`` (a click delivered again
        does nothing more). 'fold' or 'unfold'; '' when ``find()`` finds nothing."""
        record = find()
        tab = successor((record or {}).get('tab'))
        if tab not in self.panels:
            return ''
        panel, token = self.panels[tab], path.stem
        fields = panel.tab_fields(ctx) if record.get(FOLDED) else {}  # Before the lock: 群聊 asks Feishu for names.
        with self.menu.lock(path):
            record = find()
            if record is None or successor(record.get('tab')) != tab or (
                    event_id and event_id in (record.get('turned') or ())):
                return ''  # Gone or turned since, or this click did it already (delivered again).
            more = {'turned': [*(record.get('turned') or ())][-(TURNS - 1):] + [event_id]} if event_id else {}
            more['tab'] = tab  # A record on a retired tab is on its successor from now on.
            outcome, confirmed = self.menu.flip(
                ctx, path, record, lambda token, changed: panel.tab_card(ctx, token, changed),
                lambda token, changed: folded_hub(tab, token, hub_tabs()), view=panel.tab_view, fields=fields, more=more)
            if outcome == 'unfold':
                panel.tab_shown(ctx, token)
        self.runtime.log(f'settings card {outcome} tab={tab} token={token[:8]} confirmed={confirmed}')
        if not confirmed and event_id:
            self.menu.unsaved(ctx, record, UNFOLDED.format('收起' if outcome == 'fold' else '展开', LABELS[tab]), event_id)
        return outcome

    def turn(self, ctx, path, event, tab):
        """Step 2 of a tab click, after its acknowledgement: the same record and card turned to ``tab``."""
        panel, event_id = self.panels[tab], str(event['event_id'])
        fields = panel.tab_fields(ctx)  # Before the lock: the 群聊 tab asks Feishu for the groups' names.
        token = path.stem
        with self.menu.lock(path):
            record = self.menu.clicked(path, event, HELD)
            if record is None or event_id in (record.get('turned') or ()):
                return  # Gone since, or this click turned it already (delivered again).
            here = record.get('tab')
            view = {**panel.tab_view(token), 'revision': record['view']['revision'] + 1}
            turned = [*(record.get('turned') or ())][-(TURNS - 1):] + [event_id]
            changed = {**record, **fields, 'status': OPEN, 'tab': tab, 'view': view, 'turned': turned}
            changed.pop(FOLDED, None)  # Another tab shows unfolded.
            confirmed = settle(ctx, path, record, changed, panel.tab_card(ctx, token, changed), self.messenger, KIND)
            panel.tab_shown(ctx, token)
        self.runtime.log(f'settings card tab {here}->{tab} token={token[:8]} confirmed={confirmed}')
        if not confirmed:
            self.menu.unsaved(ctx, record, UNSAVED.format(LABELS[tab]), event_id)

    def takeover(self, ctx, event, here, token, owner):
        """A tab click on a card sent before the 设置 card had one record: the panel ``here`` names held it in its
        own record. That record, open on this card and the owner's click, moves to this one under the same token
        (its sequence, view and fields go on; it is MOVED, so its clicks find nothing from then on). Whether the
        card's record is here now."""
        old = self.panels.get(here)
        if old is None:
            return False
        old_path = old.path(ctx, token)
        if not owner or old_path.parent.name == HUB_FOLDER:
            return False
        path = self.path(ctx, token)
        with old.menu.lock(old_path):
            record = old.menu.clicked(old_path, event, (OPEN, EXPIRED) if here == PROXY else (OPEN,))
            if record is None or not record['view'].get('hub'):
                return path.exists()
            with self.menu.lock(path):
                if not path.exists():
                    save_record(path, {**record, 'tab': here, 'taken_from': str(old_path.parent.name)})
            save_record(old_path, {**record, 'status': MOVED, 'moved_to': {'tab': here, 'token': token}})
        self.runtime.log(f'settings card taken over tab={here} token={token[:8]}')
        return True


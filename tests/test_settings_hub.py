"""The 设置 card (app/settings_hub.py, cards/settings_hub.py): the bot menu's one card for the butler's two pages
(新话题, 管家), 仓库 and 群聊, a tab row on top (配置 · 仓库 · 群聊 · ..., T546; 脱敏 went with 敏感词脱敏, T947), held by one record;
a tab turns the same record's card to another panel in place, the panels' own buttons keep the row and act only on
their tab, a card sent under the scheme before (a record each panel) is taken over or finds nothing, 代答 is no tab
but a record an older card left on it is still drawn there, and the standalone 代答 and 群聊 cards stay as they were."""
from __future__ import annotations

import json
import os
from unittest import mock

from support.proxy import ProxyCase
from support import requires_extension
from covey.app import workspace_manager
from covey.app.butler_panel import OWNER_ONLY, TOOLS
from covey.app.menu_panel import MOVED
from covey.app.settings_hub import MENU_KEY, SettingsHub
from covey.cards import butler_panel as butler_card
from covey.cards import proxy_panel as proxy_card
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.group_manager import PAGE
from covey.cards.settings_hub import BUTLER_PAGES, TAB, tab_row
from covey.core import butler
from covey.core import butler_settings as settings
from covey.core import proxy as records
from covey.core.jobs import upsert_job_record
from covey.core.butler_requests import Request
from covey.core.groups import group_path
from covey.core.state import read_json, write_json

MEMBER = 'ou_member0012345678'
LABELS = ['配置', '仓库', '群聊', '诊断']  # 记忆 only with COVEY_MEMORY=1, 设备 with nodes.


def row_of(current):
    """The tab row's (label, type) with ``current`` the tab shown; 代答, no tab, is added last when it is the one shown
    (an older card's record)."""
    return [(label, 'primary_filled' if label == current else 'text') for label in LABELS + (
        ['代答'] if current == '代答' else [])]


def values(card, kind=None):
    found = []

    def walk(value):
        if isinstance(value, dict):
            for behavior in value.get('behaviors') or ():
                if behavior.get('type') == 'callback' and (kind is None or behavior['value'].get('kind') == kind):
                    found.append((value, behavior['value']))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(card)
    return found


class HubCase(ProxyCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_BUTLER': '1'}))  # The menu is built with it.
        super().setUp()
        self.access = self.app.runtime.access
        self.access.remember_owner_chat('dm')
        self.access.lookup = lambda profile, chat_id: {'external': False, 'name': '群' + chat_id[-2:]}
        for i in range(12):  # Two pages of the 群聊 panel.
            write_json(group_path(self.ctx.state_dir, f'oc_g{i:02d}'),
                       {'chat_id': f'oc_g{i:02d}', 'active': True, 'external': False})
        self.sent, self.puts = iter(f'om_s{i}' for i in range(1, 20)), []
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            self.puts.append(('new', card, 0)), ('hubcard', next(self.sent)))[1])
        messenger.put_card = mock.Mock(side_effect=lambda profile, card_id, card, **kw: (
            self.puts.append((card_id, card, kw['sequence'])), {})[1])
        self.hub = self.app.settings_hub

    def open(self, event_id='ev-menu', key=MENU_KEY):
        self.app.handle_menu_event({'event_id': event_id, 'event_key': key, 'operator_open_id': 'owner'}, self.ctx)

    def card(self):
        return self.puts[-1][1]

    def title(self):
        return self.card()['header']['title']['content']

    def tabs(self, card=None):
        """(label, button type) of the tab row's buttons, in order."""
        return [(button['text']['content'], button['type']) for button, _ in values(card or self.card(), TAB)]

    def tab(self, label):
        return next(value for button, value in values(self.card(), TAB) if button['text']['content'] == label)

    def press(self, value, event_id, operator='owner', message='om_s1', **more):
        return self.app._handle_card_action_event({'event_id': event_id, 'operator_id': operator, 'chat_id': 'dm',
                                                   'message_id': message, 'action_value': value, **more}, self.ctx)

    def option(self, kind, option, card=None):
        return next(v for _, v in values(card or self.card(), kind) if v.get('option') == option)

    def open_part(self, name, event_id):
        """配置's section ``name`` opened by its 展开 (cards/butler_panel.py SECTIONS); the click's outcome."""
        return self.press(next(v for _, v in values(self.card(), butler_card.SECTION) if v.get('open') == name), event_id)

    def record(self, tab, token):
        return read_json(self.hub.panel(tab).path(self.ctx, token))

    def hub_record(self, token=None):
        return read_json(self.hub.path(self.ctx, token or self.hub.token('dm', 'ev-menu')))

class SettingsHubTests(HubCase):
    @requires_extension()
    def test_the_menu_opens_on_config_with_or_without_the_butler_recalling_the_card_before(self):
        self.open('ev1')
        self.assertEqual(self.title(), '配置')  # T619: the butler's settings on one page, the first tab.
        self.assertEqual(self.tabs(), row_of('配置'))
        self.assertNotIn('代答', [label for label, _ in self.tabs()])
        self.assertEqual(self.card()['body']['elements'][0]['flex_mode'], 'flow')  # The row on top, labels never cut.
        with mock.patch.dict(os.environ, {'COVEY_BUTLER': '0'}):  # The deployment rules it out.
            self.app.menu.owner_said('dm')  # The owner wrote since: a new card (app/bot_menu.py).
            self.open('ev2')
            self.assertEqual(self.title(), '配置')  # Still the first tab: it preselects the launch card too.
            self.assertEqual(self.tabs(), [t for t in row_of('配置') if t[0] != '管家'])
            self.assertNotIn('**管家**', json.dumps(self.card(), ensure_ascii=False))  # No butler: no section of it.
            self.assertEqual({k: a.__func__ for k, a in self.hub.menu_items().items()}, {MENU_KEY: SettingsHub.open})
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_s1')
        self.assertEqual({k: a.__func__ for k, a in self.hub.menu_items().items()}, {
            MENU_KEY: SettingsHub.open, 'main_thread_settings': SettingsHub.open_main})

    @requires_extension()
    def test_a_tab_turns_the_same_records_card_once_a_click(self):
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        first = self.hub_record()
        self.assertEqual((first['tab'], first['status'], first['view']), ('config', 'open', {
            'token': token, 'revision': 1, 'hub': True}))
        self.assertEqual(self.press(self.tab('仓库'), 'ev-t0'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('仓库', 'repos'))
        # 代答 is no tab, but an older card's click on it (values as they were) still turns this card to it.
        click = {'kind': TAB, 'tab': 'proxy', 'from': 'main', 'token': token}
        self.assertEqual(self.press(click, 'ev-t1'), 'settings_tab')
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)  # No new message, nothing recalled.
        self.app.messenger.recall.assert_not_called()
        card_id, card, sequence = self.puts[-1]
        self.assertEqual((card_id, self.title()), ('hubcard', '代答'))
        self.assertEqual(self.tabs(), row_of('代答'))
        record = self.hub_record()
        self.assertEqual((record['tab'], record['status'], record['card_id'], record['message_id'], record['sequence'],
                          record['view'], record['turned']), ('proxy', 'open', 'hubcard', 'om_s1', sequence,
                                                              {'revision': 3, 'hub': True}, ['ev-t0', 'ev-t1']))
        self.assertEqual(read_json(records.proxy_dir(self.state) / 'panels' / 'latest.json'), {'token': token})
        self.assertEqual([p.name for p in (self.state / 'settings-cards').iterdir()], [token + '.json'])
        self.assertFalse((self.state / 'main-thread-panels').exists() or (records.proxy_dir(self.state) / 'panels' /
                                                                            (token + '.json')).exists())
        # The same click delivered again: nothing written; the main tab's buttons, gone from the card, find nothing.
        drawn = len(self.puts)
        self.assertEqual(self.press(click, 'ev-t1'), 'settings_fold')  # On the tab shown now; this click turned it.
        compact = {'kind': butler_card.COMPACT, 'token': token, 'revision': 1, 'option': '0'}
        self.assertEqual(self.press(compact, 'ev-c1'), 'main_panel_unknown')
        self.assertEqual(len(self.puts), drawn)
        self.assertEqual(settings.load(self.state).compact_tokens, settings.Settings().compact_tokens)
        # On to 群聊 and back to 管家: one record, each write the next sequence of it (rule 3).
        self.assertEqual(self.press(self.tab('群聊'), 'ev-t3'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('群聊', 'groups'))
        drawn = len(self.puts)  # The 代答 click's turn run again late (delivered again): it turned the card already.
        self.hub.turn(self.ctx, self.hub.path(self.ctx, token), {'event_id': 'ev-t1', 'chat_id': 'dm',
                                                                 'message_id': 'om_s1'}, 'proxy')
        self.assertEqual((len(self.puts), self.hub_record()['tab']), (drawn, 'groups'))
        self.assertEqual(self.press(self.tab('配置'), 'ev-t4'), 'settings_tab')
        self.assertEqual(self.title(), '配置')
        self.assertEqual(self.open_part('butler', 'ev-p1'), 'main_panel')  # 配置 opens on 新话题.
        self.assertEqual(self.press(self.option(butler_card.COMPACT, '0'), 'ev-c2'), 'main_panel')
        self.assertEqual([s for _, _, s in self.puts[1:]], list(range(1, len(self.puts))))
        self.assertEqual(self.hub_record()['sequence'], len(self.puts) - 1)
        self.assertEqual(self.hub_record()['view']['revision'], 7)

    @requires_extension()
    def test_the_retired_mode_buttons_of_an_older_card_change_nothing(self):
        """私聊顶层的消息 is retired (T946): the 管家 part has no mode row; an older card's row, its 确定改 and the older
        开启/关闭 redraw 配置 with MODE_RETIRED, the butler stays on and nothing is saved."""
        self.addCleanup(butler.configure_state, butler.configure_state(self.state))
        self.open()
        self.assertEqual(self.open_part('butler', 'ev-t1'), 'main_panel')
        self.assertTrue(values(self.card(), butler_card.COMPACT))
        self.assertEqual([values(self.card(), kind) for kind in butler_card.MODE_KINDS], [[], []])
        before = settings.load(self.state)
        token = self.hub.token('dm', 'ev-menu')
        for n, value in enumerate(({'kind': butler_card.MODE, 'option': 'topics'}, {'kind': butler_card.MODE, 'option': 'agent'},
                                   {'kind': butler_card.ENABLE, 'on': False}, {'kind': butler_card.ENABLE, 'on': True})):
            revision = self.hub_record()['view']['revision']
            self.assertEqual(self.press({**value, 'token': token, 'revision': revision}, f'ev-m{n}'), 'main_panel')
            self.assertEqual(settings.load(self.state), before)
            self.assertTrue(butler.enabled())
            self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
            self.assertIn(butler_card.MODE_RETIRED, json.dumps(self.card(), ensure_ascii=False))
        self.assertFalse(settings.path(self.state).exists())

    def test_a_slow_proxy_result_after_the_card_turned_writes_nothing(self):
        with mock.patch.dict(os.environ, {'COVEY_BUTLER': ''}):
            self.open()
        token = self.hub.token('dm', 'ev-menu')
        proxy = self.hub.panel('proxy')
        self.assertEqual(self.press(self.tab('群聊'), 'ev-t1'), 'settings_tab')
        drawn = len(self.puts)
        path = proxy.path(self.ctx, token)
        self.assertEqual(path, self.state / 'settings-cards' / (token + '.json'))
        proxy.redraw(self.ctx, path)  # Its busy redraw.
        proxy.finish(self.ctx, path, proxy_card.FIND, lambda: {'notice': 'found'})  # Its result.
        self.assertEqual((len(self.puts), self.title()), (drawn, '群聊'))
        self.assertNotIn('found', json.dumps(self.hub_record(), ensure_ascii=False))

    def test_a_card_sent_before_one_record_is_taken_over_on_a_tab_click(self):
        """Gen 286 cards: the panel's own record holds the card (its view has ``hub``); a moved one finds nothing."""
        main = self.hub.panel('main')
        old = 'd' * 24
        folder = self.state / 'main-thread-panels'
        record = {'schema_version': 1, 'chat_id': 'dm', 'source_event_id': 'ev-old', 'status': 'open',
                  'card_id': 'oldcard', 'message_id': 'om_old', 'sequence': 1_762_400_000,
                  'view': {'token': old, 'revision': 3, 'hub': True}}
        write_json(folder / (old + '.json'), record)
        write_json(folder / ('e' * 24 + '.json'), {**record, 'status': MOVED, 'message_id': 'om_moved',
                                                    'view': {'token': 'e' * 24, 'revision': 1, 'hub': True}})
        compact = {'kind': butler_card.COMPACT, 'token': old, 'revision': 3, 'option': '0'}
        # Its own buttons act on its own record, as before.
        self.assertEqual(self.press(compact, 'ev-c1', message='om_old'), 'main_panel')
        self.assertEqual((self.puts[-1][0], self.puts[-1][2]), ('oldcard', 1_762_400_001))
        self.assertFalse((self.state / 'settings-cards').exists())
        click = {'kind': TAB, 'tab': 'groups', 'from': 'main', 'token': old}
        event = {'event_id': 'ev-x', 'chat_id': 'dm', 'message_id': 'om_old'}
        self.assertEqual(self.hub.click(self.ctx, event, click, False), 'owner_only')  # Refused on the card.
        self.assertIn(OWNER_ONLY, json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual(self.press(click, 'ev-t1', message='om_old'), 'settings_tab')
        self.assertEqual((self.puts[-1][0], self.puts[-1][2], self.title()), ('oldcard', 1_762_400_003, '群聊'))
        taken = self.hub_record(old)
        self.assertEqual((taken['tab'], taken['card_id'], taken['sequence'], taken['view']['revision']),
                         ('groups', 'oldcard', 1_762_400_003, 6))
        self.assertEqual(read_json(folder / (old + '.json'))['status'], MOVED)
        self.assertEqual(self.press(compact, 'ev-c2', message='om_old'), 'main_panel_unknown')  # Not on 管家 now.
        self.assertEqual(main.path(self.ctx, old), self.state / 'settings-cards' / (old + '.json'))
        self.assertEqual(self.press(self.tab('配置'), 'ev-t2', message='om_old'), 'settings_tab')
        self.assertEqual(self.press({**compact, 'revision': 7, 'option': '100000'}, 'ev-c3', message='om_old'),
                         'main_panel')
        self.assertEqual(settings.load(self.state).compact_tokens, 100_000)
        # A card the scheme before had moved on: neither its buttons nor its tabs do anything.
        drawn = len(self.puts)
        moved = {'kind': TAB, 'tab': 'topic', 'from': 'main', 'token': 'e' * 24}
        self.assertEqual(self.press(moved, 'ev-t3', message='om_moved'), 'settings_unknown')
        self.assertEqual(self.press({**compact, 'token': 'e' * 24}, 'ev-c4', message='om_moved'), 'main_panel_unknown')
        self.assertEqual(len(self.puts), drawn)
        self.assertFalse((self.state / 'settings-cards' / ('e' * 24 + '.json')).exists())

    def test_only_the_owner_turns_tabs(self):
        self.open()
        click = self.tab('群聊')
        self.assertEqual(self.press(click, 'ev-t1', operator=MEMBER), 'not_owner')  # The owner's private chat.
        event = {'event_id': 'ev-t2', 'chat_id': 'dm', 'message_id': 'om_s1'}
        self.assertEqual(self.hub.click(self.ctx, event, click, False), 'owner_only')
        self.assertIn(OWNER_ONLY, json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual((self.title(), self.hub_record()['tab'], self.hub_record()['status']), ('配置', 'config', 'open'))
        self.assertEqual(self.tabs()[0], ('配置', 'primary_filled'))
        self.assertEqual(self.press({**click, 'token': '0' * 24}, 'ev-t3'), 'settings_unknown')
        self.assertEqual(self.press({**click, 'tab': 'usage'}, 'ev-t4'), 'settings_unknown')
        self.assertEqual(self.press(click, 'ev-t5', message='om_other'), 'settings_unknown')
        self.assertEqual(self.title(), '配置')

    @requires_extension()
    def test_the_panels_own_buttons_keep_the_tab_row(self):
        self.open()
        self.open_part('butler', 'ev-t0')
        compact = self.option(butler_card.COMPACT, '0')
        self.assertEqual(self.press(compact, 'ev-c1'), 'main_panel')
        self.assertEqual((settings.load(self.state).compact_tokens, self.tabs()[0]), (0, ('配置', 'primary_filled')))
        self.press(self.tab('群聊'), 'ev-t1')
        turn = next(v for _, v in values(self.card(), PAGE) if v['page'] == 1)
        self.assertEqual(self.press(turn, 'ev-p1'), 'group_manager')
        self.assertIn('第 2/2 页', json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual(self.tabs()[2], ('群聊', 'primary_filled'))
        # An older card on 代答 keeps its row too (代答 added after the tabs).
        self.press({'kind': TAB, 'tab': 'proxy', 'from': 'groups', 'token': self.hub.token('dm', 'ev-menu')}, 'ev-t2')
        refresh = values(self.card(), proxy_card.REFRESH)[0][1]
        self.assertEqual(self.press(refresh, 'ev-r1'), 'proxy_panel')
        self.assertEqual((self.title(), self.tabs()), ('代答', row_of('代答')))

    @requires_extension()
    def test_the_standalone_cards_have_no_tabs_and_only_the_newest_proxy_panel_acts(self):
        self.open('ev-proxy', 'proxy_settings')
        standalone = self.hub.panel('proxy').menu.token('dm', 'ev-proxy')
        self.assertEqual((self.title(), self.tabs()), ('代答', []))
        self.open('ev-menu')  # om_s2: the 群聊 menu item that came between is gone (T898).
        hub = self.hub.token('dm', 'ev-menu')

        def to_proxy(event_id, current):  # No 代答 tab on the card: the click of an older one (values as they were).
            return self.press({'kind': TAB, 'tab': 'proxy', 'from': current, 'token': hub}, event_id, message='om_s2')
        to_proxy('ev-t1', 'topic')
        refresh = values(self.puts[0][1], proxy_card.REFRESH)[0][1]
        self.assertEqual(self.press(refresh, 'ev-r1', message='om_s1'), 'proxy_expired')
        self.assertEqual(self.record('proxy', standalone)['status'], 'expired')
        # The other way: a standalone card opened since expires the tab, whose card keeps its row to turn on.
        self.open('ev-proxy2', 'proxy_settings')
        on_hub = {'kind': proxy_card.REFRESH, 'token': hub, 'revision': 2}
        self.assertEqual(self.press(on_hub, 'ev-r2', message='om_s2'), 'proxy_expired')
        self.assertIn('已过期', self.title() + json.dumps(self.card()['header'], ensure_ascii=False))
        self.assertEqual(self.tabs(), row_of('代答'))  # Its row keeps 代答 though it is no tab, to turn from.
        self.assertEqual(self.press(self.tab('群聊'), 'ev-t2', message='om_s2'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['status']), ('群聊', 'open'))
        # Back on 代答, the card is the newest panel again and acts.
        self.assertEqual(to_proxy('ev-t3', 'groups'), 'settings_tab')
        self.assertEqual(self.press({**on_hub, 'revision': 4}, 'ev-r3', message='om_s2'), 'proxy_panel')

    @requires_extension()
    def test_the_tool_and_the_butlers_menu_key_send_the_settings_card_on_the_butler_page(self):
        upsert_job_record(self.state, 'main-job', chat_id='dm', message_id='om_owner')
        answer = TOOLS['settings_panel'](self.app.butler_env, Request('r1', 'settings_panel', {}, 'main-job', 0.0))
        self.assertTrue(answer['ok'], answer)
        send = self.app.messenger.create_and_send_card
        self.assertEqual((send.call_args.kwargs['reply_to_message_id'], send.call_args.kwargs['in_thread']),
                         ('om_owner', False))
        self.assertEqual((self.title(), self.tabs()[0]), ('配置', ('配置', 'primary_filled')))
        self.open('ev-main', 'main_thread_settings')
        self.assertEqual((self.title(), self.tabs()), ('配置', row_of('配置')))
        self.assertTrue(values(self.card(), butler_card.COMPACT))  # 管家 open: the butler's key and tool.
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_s1')  # One 设置 card in the chat.
        self.app.menu.owner_said('dm')  # The owner wrote since: a new card (app/bot_menu.py).
        self.open('ev-menu')
        self.assertEqual(self.app.messenger.recall.call_args.args[1], 'om_s2')

    def test_the_butlers_page_folds_and_an_older_cards_tab_still_turns(self):
        """T619 (the owner, 2026-11-21: 模型、新话题、管家 合成一个): the butler's settings are one tab, 配置, folding on a
        second click, a section open at a time; a card drawn before (its tabs 管家 · 代答 · 群聊, or 新话题 · 管家, or 模型)
        still turns: each of those lands on 配置, its 代答 still draws the 代答 panel."""
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        self.assertEqual((self.title(), self.hub_record()['tab'], self.tabs()[0]),
                         ('配置', 'config', ('配置', 'primary_filled')))  # The menu opens on it, 新话题 open.
        self.assertTrue(values(self.card(), butler_card.TOPIC_HARNESS))
        self.assertFalse(values(self.card(), butler_card.HARNESS))
        # 直接开工 is retired: an older card's row still turns the card and changes nothing.
        before = settings.load(self.state)
        self.assertEqual(self.press({'kind': butler_card.START_ROW, 'token': token, 'revision': 1, 'option': 'safe'}, 'ev-s1'),
                         'main_panel')
        self.assertEqual(settings.load(self.state), before)
        self.assertEqual(self.open_part('butler', 'ev-s2'), 'main_panel')
        self.assertTrue(values(self.card(), butler_card.HARNESS))
        self.assertFalse(values(self.card(), butler_card.TOPIC_HARNESS))
        self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        self.assertEqual(self.press(self.tab('配置'), 'ev-t3'), 'settings_fold')
        self.assertEqual(self.title(), '配置')
        self.assertEqual(self.press(self.tab('配置'), 'ev-t4'), 'settings_unfold')
        # The older cards' tab rows, values as they were.
        for n, (tab, here) in enumerate((('topic', 'main'), ('models', 'topic'))):
            self.press(self.tab('群聊'), f'ev-g{n}')
            self.assertEqual(self.press({'kind': TAB, 'tab': tab, 'from': here, 'token': token}, f'ev-o{n}'),
                             'settings_tab')
            self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        self.assertEqual(self.press({'kind': TAB, 'tab': 'proxy', 'from': 'main', 'token': token}, 'ev-t5'),
                         'settings_tab')
        self.assertEqual(self.title(), '代答')
        self.assertEqual(self.press({'kind': TAB, 'tab': 'main', 'from': 'proxy', 'token': token}, 'ev-t6'),
                         'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        self.assertEqual(self.press({'kind': TAB, 'tab': 'main', 'from': 'groups', 'token': token}, 'ev-t7'),
                         'settings_fold')  # The tab shown (管家 is 配置 now): folds, as before.

    @requires_extension()
    def test_with_the_tab_row_a_full_card_keeps_within_the_budget(self):
        tabs = ['config', 'workspace', 'groups']
        choices = {f'codex:m{i}': {'harness': 'codex', 'model': f'm{i}', 'title': 'Codex', 'display_name': f'M{i}',
                                   'levels': ['low', 'high', 'xhigh']} for i in range(40)}
        choices.update({f'claude:c{i}': {'harness': 'claude', 'model': f'c{i}', 'title': 'Claude Code',
                                         'display_name': f'C{i}', 'levels': ['high', 'max']} for i in range(10)})
        current = settings.Settings(model='c9', topic_defaults=settings.TopicDefaults(harness='codex', model='m39'))
        for page in BUTLER_PAGES:  # Each page of the butler's.
            row = [tab_row(page, 'a' * 24, tabs)]
            card = butler_card.butler_panel_card('a' * 24, {'revision': 1, 'hub': True}, current, choices,
                                                    [f'w{i}' for i in range(30)], top=row, page=page)
            self.assertEqual(card['body']['elements'][0], row[0])
            self.assertLessEqual(element_count(card), CARD_NODES)
            self.assertLessEqual(json_size(card), CARD_BYTES)
        for i in range(60):  # A long whitelist on the 代答 tab.
            records.write_contact(self.state, {**dict(key=f'p2p:ou_{i:03d}', kind='p2p', target=f'ou_{i:03d}',
                                                      name=f'联系人{i:03d}', chat_id=f'oc_p{i:03d}'),
                                               'paused': False, 'since': 0, 'added_at': i, 'seen': []})
        with mock.patch.dict(os.environ, {'COVEY_BUTLER': '0'}):
            self.open()
            self.press({'kind': TAB, 'tab': 'proxy', 'from': 'topic', 'token': self.hub.token('dm', 'ev-menu')}, 'ev-t1')
        self.assertEqual(self.tabs(), [t for t in row_of('代答') if t[0] != '管家'])  # No 管家 without the butler; 代答 only an older card's.
        self.assertLessEqual(element_count(self.card()), CARD_NODES)
        self.assertLessEqual(json_size(self.card()), CARD_BYTES)


class SixTabsTests(HubCase):
    """工作区 and 脱敏 on the 设置 card (the owner, 2026-11-09: 菜单 用量 · 看板 · 设置; the tabs now 新话题 · 管家 · 工作区 · 脱敏 · 群聊, T546); the page 其他 retired,
    an older card's tab and buttons on it still land; the old menu keys open the card on their tabs."""

    def setUp(self):
        super().setUp()
        from covey.core.workspaces import add_workspace
        for name in ('alpha', 'beta'):
            (self.root / name / '.git').mkdir(parents=True)  # Checkouts: the 仓库 tab lists no plain directory (T870 Wave 3.5).
            add_workspace(self.state, self.ctx.default_workdir, str(self.root / name))
        self.manager = self.app.workspace_manager
        self.manager.started = True  # The tests run each queued request themselves.

    def run_queued(self):
        handler, ctx, event = self.manager.queue.get_nowait()
        handler(ctx, event)

    @requires_extension()
    def test_the_old_keys_say_where_their_pages_went_and_open_no_card(self):
        # T898: 仓库, 脱敏 and 群聊 are off the app's menu (core/app_manifest.py STALE_MENU_KEYS); a client's cached copy of one
        # is answered with one line (app/bot_menu.py), not a card.
        for event_id, key, name, tab in (('ev-w', 'manage_workspaces', '仓库', '仓库'), ('ev-g', 'manage_groups', '群聊', '群聊')):
            self.open(event_id, key)
            text = self.app.messenger.send_text.call_args.args[2]
            self.assertIn(f'「{name}」已移到「设置」卡的「{tab}」页签', text)
        self.open('ev-r', 'redaction_dashboard')  # 敏感词脱敏 went with its tab (T947).
        self.assertEqual(self.app.messenger.send_text.call_args.args[2], '敏感词脱敏已下线，菜单里的「脱敏」没有用了。')
        self.assertFalse(self.puts)  # No 设置 card was sent.
        self.assertFalse((self.state / 'workspace-managers').exists())
        self.assertFalse((self.state / 'dashboard-links').exists())

    @requires_extension()
    def test_the_repos_tab_is_the_home_and_the_repositories_read_only(self):
        settings.update(self.state, workspace_notes=(('alpha', '甲'),))
        self.open()
        self.assertNotIn('工作区', [label for label, _ in self.tabs()])  # The 工作区 tab is replaced.
        self.assertEqual(self.press(self.tab('仓库'), 'ev-t1'), 'settings_tab')
        text = json.dumps(self.card(), ensure_ascii=False)
        # Without the web page: the home, the count, each repository with its state and 用途 (the old list read as a map).
        for part in ('主目录', str(self.ctx.default_workdir.name), '2 个', '**alpha**', '**beta**', '甲', '可写', '没开网页'):
            self.assertIn(part, text)
        for part in ('＋ 添加工作区…', '改用途', '保存用途', '记住的工作区', '设为默认', '"note_0"', '完成管理'):
            self.assertNotIn(part, text)
        self.assertEqual({v['kind'] for _, v in values(self.card())}, {TAB})  # No button but the tab row's: read only.
        self.assertFalse((self.state / 'repos.json').exists())  # Drawing writes nothing.
        self.assertEqual([r for r in (self.state).glob('workspace-managers/*')], [])
        # With the web page: the home, the count and the link; no list.
        with mock.patch('covey.core.web_sync.page_url', return_value='https://web.example/'):
            self.assertEqual(self.press(self.tab('群聊'), 'ev-t2'), 'settings_tab')
            self.assertEqual(self.press(self.tab('仓库'), 'ev-t3'), 'settings_tab')
            text = json.dumps(self.card(), ensure_ascii=False)
        self.assertIn('(https://web.example/settings/repos)', text)
        for part in ('**alpha**', '甲', '改状态'):
            self.assertNotIn(part, text)
        self.assertIn('2 个', text)

    @requires_extension()
    def test_the_map_file_is_what_the_tab_shows_once_it_exists_and_the_butler_does_not_change_it(self):
        write_json(self.state / 'repos.json', {'version': 1, 'repos': [
            {'name': 'covey', 'path': '/srv/covey', 'note': '核心', 'status': 'frozen'},
            {'name': 'web', 'path': '/srv/web', 'status': 'readonly'}]})
        self.open()
        with mock.patch.dict(os.environ, {'COVEY_BUTLER': '0'}):
            self.assertEqual(self.press(self.tab('仓库'), 'ev-t1'), 'settings_tab')
            text = json.dumps(self.card(), ensure_ascii=False)
        for part in ('**covey**', '已冻结', '核心', '**web**', '只读'):
            self.assertIn(part, text)
        self.assertNotIn('**alpha**', text)
        self.assertEqual(read_json(self.state / 'repos.json')['repos'][0]['status'], 'frozen')

    @requires_extension()
    def test_a_click_on_the_retired_manager_card_says_it_is_gone_and_changes_nothing(self):
        self.open()
        self.assertEqual(self.press(self.tab('仓库'), 'ev-t1'), 'settings_tab')
        token = self.hub.token('dm', 'ev-menu')
        puts, record = len(self.puts), self.hub_record()
        from covey.cards.workspace_manager import MANAGER_PREFIX, row_key
        sent = self.app.messenger.send_message = mock.Mock()
        for n, verb in enumerate(('refresh', 'notes', f'remove-{row_key("alpha")}', 'add', 'dir-open0')):
            name = f'{MANAGER_PREFIX}{token}:{record["view"]["revision"]}:{verb}'
            self.assertEqual(self.press({}, f'ev-d{n}', action_name=name, form_value={}), 'workspace_manager')
            self.run_queued()
            self.assertEqual(sent.call_args.args[2], workspace_manager.RETIRED_TEXT)
        self.assertEqual(sent.call_count, 5)
        # The launch card's 管理工作区 option, on a card sent before.
        from covey.cards import launch_config
        self.assertEqual(self.press({'kind': launch_config.WORKSPACE_ACTION, 'token': token}, 'ev-l1',
                                    option=launch_config.MANAGE_OPTION), 'workspace_manager')
        self.run_queued()
        self.assertEqual(sent.call_count, 6)
        self.assertEqual((len(self.puts), self.hub_record()), (puts, record))  # No card or record written.
        self.assertEqual(self.press({}, 'ev-d9', operator=MEMBER, action_name=f'{MANAGER_PREFIX}{token}:1:refresh',
                                    form_value={}), 'not_owner')
        self.assertEqual(sent.call_count, 6)  # Not the owner: nothing said.

    @requires_extension()
    def test_the_butlers_workspace_buttons_of_an_older_card_say_offline_and_change_nothing(self):
        """The 工作区 and 运行方式 rows and 保存用途 are off the 配置 page: an older card's button (its record on the retired 工作区 tab too)
        is answered on the card, 配置 drawn, no setting touched, the 工作区 setting the butler runs in kept."""
        settings.update(self.state, workspace='alpha')
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        path = self.hub.path(self.ctx, token)
        write_json(path, {**read_json(path), 'tab': 'workspace'})  # An older card, shown on 工作区.
        before = settings.load(self.state)
        for n, (kind, more) in enumerate(((butler_card.TOPIC_WORKSPACE, {'option': 'beta'}), (butler_card.TOPIC_RUN_MODE, {'option': 'worktree'}),
                                          (butler_card.SPACE, {'option': 'beta'}), (butler_card.NOTES, {'workspaces': ['alpha']}))):
            write_json(path, {**read_json(path), 'tab': 'workspace'})
            value = {'kind': kind, 'token': token, 'revision': self.hub_record()['view']['revision'], **more}
            self.assertEqual(self.press(value, f'ev-o{n}', form_value={'note_0': 'x'}), 'main_panel')
            self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
            self.assertIn(butler_card.OFFLINE_NOTICE, json.dumps(self.card(), ensure_ascii=False))
            self.assertEqual(settings.load(self.state), before)
        self.assertEqual(settings.load(self.state).workspace, 'alpha')
        # The page itself has none of the rows or buttons.
        text = json.dumps(self.card(), ensure_ascii=False)
        for kind in butler_card.OFFLINE_KINDS:
            self.assertEqual(values(self.card(), kind), [], kind)
        for part in ('工作区', '运行方式', '保存用途'):
            self.assertNotIn(part, text.replace(butler_card.OFFLINE_NOTICE, ''))
        self.assertTrue(values(self.card(), butler_card.HARNESS))  # The butler's section is the one the last click opened: no 工作区 row.
        self.assertNotIn('默认工作区', json.dumps(self.card(), ensure_ascii=False))

    def test_an_older_cards_redaction_tab_lands_on_config(self):
        # 敏感词脱敏 was taken out (T947): its tab is gone, and a card drawn before turns to 配置 on it (its buttons only
        # get one line under the card, tests/test_redaction_retired.py).
        self.open()
        self.assertNotIn('脱敏', [label for label, _ in self.tabs()])
        self.press(self.tab('群聊'), 'ev-t1')
        token = self.hub.token('dm', 'ev-menu')
        self.assertEqual(self.press({'kind': TAB, 'tab': 'redaction', 'from': 'groups', 'token': token}, 'ev-t2'),
                         'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        path = self.hub.path(self.ctx, token)
        write_json(path, {**read_json(path), 'tab': 'redaction'})  # A record still on it.
        self.assertEqual(self.press({'kind': TAB, 'tab': 'groups', 'from': 'redaction', 'token': token}, 'ev-t3'),
                         'settings_tab')
        self.assertEqual(self.title(), '群聊')

    def test_an_older_card_on_the_retired_other_page_still_acts(self):
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        path = self.hub.path(self.ctx, token)
        write_json(path, {**read_json(path), 'tab': 'other'})  # Shown on 其他, before it was retired.
        # Its 直接开工 (retired) lands on 配置 and changes nothing.
        start = {'kind': butler_card.START, 'token': token, 'revision': 1, 'tier': 'readonly', 'on': True}
        self.assertEqual(self.press(start, 'ev-s1'), 'main_panel')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        # Its 其他 tab (clicked on another page) lands on 仓库, and a tab click from 其他 turns as any.
        self.assertEqual(self.press({'kind': TAB, 'tab': 'other', 'from': 'main', 'token': token}, 'ev-t1'),
                         'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('仓库', 'repos'))
        write_json(path, {**read_json(path), 'tab': 'other'})
        self.assertEqual(self.press({'kind': TAB, 'tab': 'proxy', 'from': 'other', 'token': token}, 'ev-t2'),
                         'settings_tab')
        self.assertEqual(self.title(), '代答')  # An older card's 代答 click from 其他: the panel is still drawn.
        # 记住的工作区 (retired) removed from it lands on 配置 as well; the menu's 设置 folds a record on 其他 (or the old 工作区) as 仓库.
        write_json(path, {**read_json(path), 'tab': 'other'})
        untrust = {'kind': butler_card.UNTRUST, 'token': token, 'revision': 1, 'key': 'alpha|read'}
        self.assertEqual(self.press(untrust, 'ev-u1'), 'main_panel')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('配置', 'config'))
        write_json(path, {**read_json(path), 'tab': 'other'})
        write_json(path, {**read_json(path), 'tab': 'other'})
        self.assertEqual(self.hub.fold(self.ctx, 'dm', 'ev-menu', 'om_s1'), 'fold')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('仓库', 'repos'))
        self.assertEqual(self.hub.fold(self.ctx, 'dm', 'ev-menu', 'om_s1'), 'unfold')
        self.assertIn('**alpha**', json.dumps(self.card(), ensure_ascii=False))
        write_json(path, {**read_json(path), 'tab': 'workspace'})  # An older card shown on 工作区 turns the same way.
        self.assertEqual(self.press({'kind': TAB, 'tab': 'groups', 'from': 'workspace', 'token': token}, 'ev-t8'), 'settings_tab')
        self.assertEqual(self.title(), '群聊')

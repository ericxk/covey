"""The 设置 card (app/settings_hub.py, cards/settings_hub.py): the bot menu's one card for the main thread's two pages
(私聊默认, 新话题默认), 工作区, 脱敏, 代答 and 群聊, a tab row on top, held by one record; a tab turns the same record's card to another panel in place, the panels' own
buttons keep the row and act only on their tab, a card sent under the scheme before (a record each panel) is taken
over or finds nothing, and the standalone 代答 and 群聊 cards stay as they were."""
from __future__ import annotations

import json
import os
from unittest import mock

from support.proxy import ProxyCase
from support import requires_extension
from covey.app import main_thread_panel
from covey.app.main_thread_panel import OWNER_ONLY, TOOLS
from covey.app.menu_panel import MOVED
from covey.app.settings_hub import MENU_KEY, SettingsHub
from covey.cards import main_thread_panel as main_card
from covey.cards import proxy_panel as proxy_card
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.group_manager import PAGE
from covey.cards.settings_hub import TAB, tab_row
from covey.core import main_thread
from covey.core import main_thread_settings as settings
from covey.core import proxy as records
from covey.core.jobs import upsert_job_record
from covey.core.main_requests import Request
from covey.core.groups import group_path
from covey.core.state import read_json, write_json

MEMBER = 'ou_member0012345678'
LABELS = ['私聊', '新话题', '工作区', '脱敏', '代答', '群聊']


def row_of(current):
    """The tab row's (label, type) with ``current`` the tab shown."""
    return [(label, 'primary_filled' if label == current else 'text') for label in LABELS]


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
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': '1'}))  # The menu is built with it.
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

    def record(self, tab, token):
        return read_json(self.hub.panel(tab).path(self.ctx, token))

    def hub_record(self, token=None):
        return read_json(self.hub.path(self.ctx, token or self.hub.token('dm', 'ev-menu')))

class SettingsHubTests(HubCase):
    @requires_extension()
    def test_the_menu_opens_on_main_and_without_the_switch_on_proxy_recalling_the_card_before(self):
        self.open('ev1')
        self.assertEqual(self.title(), '私聊默认')
        self.assertEqual(self.tabs(), row_of('私聊'))
        self.assertEqual(self.card()['body']['elements'][0]['flex_mode'], 'flow')  # The row on top, labels never cut.
        with mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': '0'}):  # The deployment rules it out.
            self.app.menu.owner_said('dm')  # The owner wrote since: a new card (app/bot_menu.py).
            self.open('ev2')
            self.assertEqual(self.title(), '工作区')
            self.assertEqual(self.tabs(), row_of('工作区')[2:])
            self.assertEqual({k: a.__func__ for k, a in self.hub.menu_items().items()}, {
                MENU_KEY: SettingsHub.open, 'manage_workspaces': SettingsHub.open_workspaces,
                'redaction_dashboard': SettingsHub.open_redaction})
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_s1')
        self.assertEqual({k: a.__func__ for k, a in self.hub.menu_items().items()}, {
            MENU_KEY: SettingsHub.open, 'manage_workspaces': SettingsHub.open_workspaces,
            'redaction_dashboard': SettingsHub.open_redaction, 'main_thread_settings': SettingsHub.open_main})

    @requires_extension()
    def test_a_tab_turns_the_same_records_card_once_a_click(self):
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        first = self.hub_record()
        self.assertEqual((first['tab'], first['status'], first['view']), ('main', 'open', {
            'token': token, 'revision': 1, 'hub': True}))
        click = self.tab('代答')
        self.assertEqual(click, {'kind': TAB, 'tab': 'proxy', 'from': 'main', 'token': token})
        self.assertEqual(self.press(click, 'ev-t1'), 'settings_tab')
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)  # No new message, nothing recalled.
        self.app.messenger.recall.assert_not_called()
        card_id, card, sequence = self.puts[-1]
        self.assertEqual((card_id, self.title()), ('hubcard', '代答'))
        self.assertEqual(self.tabs(), row_of('代答'))
        record = self.hub_record()
        self.assertEqual((record['tab'], record['status'], record['card_id'], record['message_id'], record['sequence'],
                          record['view'], record['turned']), ('proxy', 'open', 'hubcard', 'om_s1', sequence,
                                                              {'revision': 2, 'hub': True}, ['ev-t1']))
        self.assertEqual(read_json(records.proxy_dir(self.state) / 'panels' / 'latest.json'), {'token': token})
        self.assertEqual([p.name for p in (self.state / 'settings-cards').iterdir()], [token + '.json'])
        self.assertFalse((self.state / 'main-thread-panels').exists() or (records.proxy_dir(self.state) / 'panels' /
                                                                            (token + '.json')).exists())
        # The same click delivered again: nothing written; the main tab's buttons, gone from the card, find nothing.
        drawn = len(self.puts)
        self.assertEqual(self.press(click, 'ev-t1'), 'settings_fold')  # On the tab shown now; this click turned it.
        compact = next(v for _, v in values(self.puts[0][1], main_card.COMPACT) if v.get('option') == '0')
        self.assertEqual(self.press(compact, 'ev-c1'), 'main_panel_unknown')
        self.assertEqual(len(self.puts), drawn)
        self.assertEqual(settings.load(self.state).compact_tokens, settings.Settings().compact_tokens)
        # On to 群聊 and back to 私聊默认: one record, each write the next sequence of it (rule 3).
        self.assertEqual(self.press(self.tab('群聊'), 'ev-t3'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('群聊', 'groups'))
        drawn = len(self.puts)  # The 代答 click's turn run again late (delivered again): it turned the card already.
        self.hub.turn(self.ctx, self.hub.path(self.ctx, token), {'event_id': 'ev-t1', 'chat_id': 'dm',
                                                                 'message_id': 'om_s1'}, 'proxy')
        self.assertEqual((len(self.puts), self.hub_record()['tab']), (drawn, 'groups'))
        self.assertEqual(self.press(self.tab('私聊'), 'ev-t4'), 'settings_tab')
        self.assertEqual(self.title(), '私聊默认')
        self.assertEqual(self.press(next(v for _, v in values(self.card(), main_card.COMPACT) if v.get('option') == '0'),
                                    'ev-c2'), 'main_panel')
        self.assertEqual([s for _, _, s in self.puts[1:]], list(range(1, len(self.puts))))
        self.assertEqual(self.hub_record()['sequence'], len(self.puts) - 1)
        self.assertEqual(self.hub_record()['view']['revision'], 5)

    @requires_extension()
    def test_in_the_mode_topics_the_menu_opens_on_main_whose_first_row_turns_personal_agent_on(self):
        """The menu opens on 私聊默认 in either mode (T444, the owner 2026-11-08); in 纯话题 its first row turns it on."""
        settings.update(self.state, enabled=False)
        self.addCleanup(main_thread.configure_state, main_thread.configure_state(self.state))
        with mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': ''}):  # Allowed: the mode decides.
            self.assertFalse(main_thread.enabled())
            self.open('ev1')
            self.assertEqual((self.title(), self.tabs()[0]), ('私聊默认', ('私聊', 'primary_filled')))
            self.assertEqual(self.tabs(), row_of('私聊'))  # 私聊 turns the mode back.
            self.assertIn(main_thread_panel.MENU_KEY, self.hub.menu_items())
            switch, = [v for _, v in values(self.card(), main_card.ENABLE)]
            self.assertIs(switch['on'], True)
            self.assertEqual(self.press(switch, 'ev-e1'), 'main_panel')
            self.assertTrue(main_thread.enabled())
            self.assertIn('已改成 Personal Agent', json.dumps(self.card(), ensure_ascii=False))

    def test_a_slow_proxy_result_after_the_card_turned_writes_nothing(self):
        with mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': ''}):
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
        compact = {'kind': main_card.COMPACT, 'token': old, 'revision': 3, 'option': '0'}
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
        self.assertEqual(self.press(compact, 'ev-c2', message='om_old'), 'main_panel_unknown')  # Not on 主线程 now.
        self.assertEqual(main.path(self.ctx, old), self.state / 'settings-cards' / (old + '.json'))
        self.assertEqual(self.press(self.tab('私聊'), 'ev-t2', message='om_old'), 'settings_tab')
        self.assertEqual(self.press({**compact, 'revision': 7, 'option': '100000'}, 'ev-c3', message='om_old'),
                         'main_panel')
        self.assertEqual(settings.load(self.state).compact_tokens, 100_000)
        # A card the scheme before had moved on: neither its buttons nor its tabs do anything.
        drawn = len(self.puts)
        moved = {'kind': TAB, 'tab': 'proxy', 'from': 'main', 'token': 'e' * 24}
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
        self.assertEqual((self.title(), self.hub_record()['tab'], self.hub_record()['status']), ('私聊默认', 'main', 'open'))
        self.assertEqual(self.tabs()[0], ('私聊', 'primary_filled'))
        self.assertEqual(self.press({**click, 'token': '0' * 24}, 'ev-t3'), 'settings_unknown')
        self.assertEqual(self.press({**click, 'tab': 'usage'}, 'ev-t4'), 'settings_unknown')
        self.assertEqual(self.press(click, 'ev-t5', message='om_other'), 'settings_unknown')
        self.assertEqual(self.title(), '私聊默认')

    @requires_extension()
    def test_the_panels_own_buttons_keep_the_tab_row(self):
        self.open()
        compact = next(v for _, v in values(self.card(), main_card.COMPACT) if v.get('option') == '0')
        self.assertEqual(self.press(compact, 'ev-c1'), 'main_panel')
        self.assertEqual((settings.load(self.state).compact_tokens, self.tabs()[0]), (0, ('私聊', 'primary_filled')))
        self.press(self.tab('群聊'), 'ev-t1')
        turn = next(v for _, v in values(self.card(), PAGE) if v['page'] == 1)
        self.assertEqual(self.press(turn, 'ev-p1'), 'group_manager')
        self.assertIn('第 2/2 页', json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual(self.tabs()[5], ('群聊', 'primary_filled'))
        self.press(self.tab('代答'), 'ev-t2')
        refresh = values(self.card(), proxy_card.REFRESH)[0][1]
        self.assertEqual(self.press(refresh, 'ev-r1'), 'proxy_panel')
        self.assertEqual((self.title(), self.tabs()[4]), ('代答', ('代答', 'primary_filled')))

    @requires_extension()
    def test_the_standalone_cards_have_no_tabs_and_only_the_newest_proxy_panel_acts(self):
        self.open('ev-proxy', 'proxy_settings')
        standalone = self.hub.panel('proxy').menu.token('dm', 'ev-proxy')
        self.assertEqual((self.title(), self.tabs()), ('代答', []))
        self.open('ev-groups', 'manage_groups')
        self.assertEqual((self.title(), self.tabs()), ('群聊', []))
        self.open('ev-menu')
        self.press(self.tab('代答'), 'ev-t1', message='om_s3')
        refresh = values(self.puts[0][1], proxy_card.REFRESH)[0][1]
        self.assertEqual(self.press(refresh, 'ev-r1', message='om_s1'), 'proxy_expired')
        self.assertEqual(self.record('proxy', standalone)['status'], 'expired')
        # The other way: a standalone card opened since expires the tab, whose card keeps its row to turn on.
        self.open('ev-proxy2', 'proxy_settings')
        on_hub = {'kind': proxy_card.REFRESH, 'token': self.hub.token('dm', 'ev-menu'), 'revision': 2}
        self.assertEqual(self.press(on_hub, 'ev-r2', message='om_s3'), 'proxy_expired')
        self.assertIn('已过期', self.title() + json.dumps(self.card()['header'], ensure_ascii=False))
        self.assertEqual(self.tabs()[4], ('代答', 'primary_filled'))
        self.assertEqual(self.press(self.tab('群聊'), 'ev-t2', message='om_s3'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['status']), ('群聊', 'open'))
        # Back on 代答, the card is the newest panel again and acts.
        self.assertEqual(self.press(self.tab('代答'), 'ev-t3', message='om_s3'), 'settings_tab')
        self.assertEqual(self.press({**on_hub, 'revision': 4}, 'ev-r3', message='om_s3'), 'proxy_panel')

    @requires_extension()
    def test_the_tool_and_the_main_threads_menu_key_send_the_settings_card_on_main(self):
        upsert_job_record(self.state, 'main-job', chat_id='dm', message_id='om_owner')
        answer = TOOLS['settings_panel'](self.app.main_env, Request('r1', 'settings_panel', {}, 'main-job', 0.0))
        self.assertTrue(answer['ok'], answer)
        send = self.app.messenger.create_and_send_card
        self.assertEqual((send.call_args.kwargs['reply_to_message_id'], send.call_args.kwargs['in_thread']),
                         ('om_owner', False))
        self.assertEqual((self.title(), self.tabs()[0]), ('私聊默认', ('私聊', 'primary_filled')))
        self.open('ev-main', 'main_thread_settings')
        self.assertEqual((self.title(), len(self.tabs())), ('私聊默认', 6))
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_s1')  # One 设置 card in the chat.
        self.app.menu.owner_said('dm')  # The owner wrote since: a new card (app/bot_menu.py).
        self.open('ev-menu')
        self.assertEqual(self.app.messenger.recall.call_args.args[1], 'om_s2')

    def test_the_main_threads_pages_are_two_tabs_and_an_older_cards_tab_still_turns(self):
        """T444 (the owner, 2026-11-08: 一页是新话题默认，一页是私聊默认): the main thread's pages are tabs of one record,
        each folding on a second click; 直接开工 is on 新话题 since 其他 was retired (2026-11-09); a card drawn before
        them, its tabs 主线程 · 代答 · 群聊, still turns: its 主线程 (key main) is 私聊默认."""
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        self.assertEqual(self.press(self.tab('新话题'), 'ev-t1'), 'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab'], self.tabs()[1]),
                         ('新话题默认', 'topic', ('新话题', 'primary_filled')))
        self.assertTrue(values(self.card(), main_card.TOPIC_HARNESS))
        self.assertEqual(len(values(self.card(), main_card.START)), 3)
        start = next(v for _, v in values(self.card(), main_card.START) if v['tier'] == 'readonly')
        self.assertEqual(self.press(start, 'ev-s1'), 'main_panel')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('新话题默认', 'topic'))
        self.assertEqual(self.press(self.tab('新话题'), 'ev-t3'), 'settings_fold')
        self.assertEqual(self.title(), '新话题默认')
        self.assertEqual(self.press(self.tab('新话题'), 'ev-t4'), 'settings_unfold')
        # The older card's tab row: 主线程 · 代答 · 群聊, values as they were.
        self.assertEqual(self.press({'kind': TAB, 'tab': 'proxy', 'from': 'main', 'token': token}, 'ev-t5'),
                         'settings_tab')
        self.assertEqual(self.title(), '代答')
        self.assertEqual(self.press({'kind': TAB, 'tab': 'main', 'from': 'proxy', 'token': token}, 'ev-t6'),
                         'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('私聊默认', 'main'))
        self.assertEqual(self.press({'kind': TAB, 'tab': 'main', 'from': 'groups', 'token': token}, 'ev-t7'),
                         'settings_fold')  # The tab shown: folds, as before.

    @requires_extension()
    def test_with_the_tab_row_a_full_card_keeps_within_the_budget(self):
        tabs = ['main', 'topic', 'workspace', 'redaction', 'proxy', 'groups']
        choices = {f'codex:m{i}': {'harness': 'codex', 'model': f'm{i}', 'title': 'Codex', 'display_name': f'M{i}',
                                   'levels': ['low', 'high', 'xhigh']} for i in range(40)}
        choices.update({f'claude:c{i}': {'harness': 'claude', 'model': f'c{i}', 'title': 'Claude Code',
                                         'display_name': f'C{i}', 'levels': ['high', 'max']} for i in range(10)})
        current = settings.Settings(model='c9', topic_defaults=settings.TopicDefaults(harness='codex', model='m39'),
                                    trusted=tuple(f'w{i}|edit' for i in range(8)))
        for page in main_card.PAGES:  # Each page of the main thread's.
            row = [tab_row(page, 'a' * 24, tabs)]
            card = main_card.main_thread_panel_card('a' * 24, {'revision': 1, 'hub': True}, current, choices,
                                                    [f'w{i}' for i in range(30)], top=row, page=page)
            self.assertEqual(card['body']['elements'][0], row[0])
            self.assertLessEqual(element_count(card), CARD_NODES)
            self.assertLessEqual(json_size(card), CARD_BYTES)
        for i in range(60):  # A long whitelist on the 代答 tab.
            records.write_contact(self.state, {**dict(key=f'p2p:ou_{i:03d}', kind='p2p', target=f'ou_{i:03d}',
                                                      name=f'联系人{i:03d}', chat_id=f'oc_p{i:03d}'),
                                               'paused': False, 'since': 0, 'added_at': i, 'seen': []})
        with mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': '0'}):
            self.open()
            self.press(self.tab('代答'), 'ev-t1')
        self.assertEqual(self.tabs()[2], ('代答', 'primary_filled'))
        self.assertLessEqual(element_count(self.card()), CARD_NODES)
        self.assertLessEqual(json_size(self.card()), CARD_BYTES)


class SixTabsTests(HubCase):
    """工作区 and 脱敏 on the 设置 card (the owner, 2026-11-09: 菜单 用量 · 看板 · 设置, 设置六页); the page 其他 retired,
    an older card's tab and buttons on it still land; the old menu keys open the card on their tabs."""

    def setUp(self):
        super().setUp()
        from covey.app.dashboard_entry import DASHBOARD_ENV
        self.enterContext(mock.patch.dict(os.environ, {DASHBOARD_ENV: '127.0.0.1:10601', 'COVEY_DASHBOARD_ENTRY': ''}))
        from covey.core.workspaces import add_workspace
        for name in ('alpha', 'beta'):
            (self.root / name).mkdir()
            add_workspace(self.state, self.ctx.default_workdir, str(self.root / name))
        self.manager = self.app.workspace_manager
        self.manager.started = True  # The tests run each queued request themselves.

    def run_queued(self):
        handler, ctx, event = self.manager.queue.get_nowait()
        handler(ctx, event)

    @requires_extension()
    def test_the_old_keys_open_the_card_on_their_tabs_as_the_one_settings_card(self):
        self.open('ev-w', 'manage_workspaces')
        self.assertEqual((self.title(), self.tabs()), ('工作区', row_of('工作区')))
        self.assertEqual(self.hub_record(self.hub.token('dm', 'ev-w'))['tab'], 'workspace')
        self.assertFalse((self.state / 'workspace-managers').exists())  # No card of its own.
        self.app.menu.owner_said('dm')
        self.open('ev-r', 'redaction_dashboard')
        self.assertEqual((self.title(), self.tabs()), ('脱敏看板', row_of('脱敏')))
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_s1')  # One 设置 card in the chat.
        self.assertFalse((self.state / 'dashboard-links').exists())
        rotate, = [v for _, v in values(self.card(), 'bridge_dashboard_rotate')]
        self.assertEqual(rotate, {'kind': 'bridge_dashboard_rotate', 'token': self.hub.token('dm', 'ev-r')})

    @requires_extension()
    def test_the_workspace_tab_is_the_manager_with_the_main_threads_notes_and_trusted(self):
        settings.update(self.state, trusted=('alpha|read',), workspace_notes=(('alpha', '甲'),))
        self.open()
        self.assertEqual(self.press(self.tab('工作区'), 'ev-t1'), 'settings_tab')
        text = json.dumps(self.card(), ensure_ascii=False)
        for part in ('**alpha**', '**beta**', '"default_value": "甲"', '保存用途', '记住的工作区（1）', '**alpha** · 只读',
                     '＋ 添加工作区…'):
            self.assertIn(part, text)
        self.assertNotIn('完成管理', text)  # The 设置 card is not closed from one tab.
        # A manager button (its name carries the 设置 card's token): acts on the same record, the row kept on top.
        token = self.hub.token('dm', 'ev-menu')
        revision = self.hub_record()['view']['revision']
        from covey.cards.workspace_manager import MANAGER_PREFIX, row_key
        name = f'{MANAGER_PREFIX}{token}:{revision}:default-{row_key("beta")}'
        self.assertEqual(self.press({}, 'ev-d1', action_name=name, form_value={}), 'workspace_manager')
        self.run_queued()
        from covey.core.workspaces import load_config
        self.assertEqual(load_config(self.state, self.ctx.default_workdir)['default'], 'beta')
        self.assertEqual((self.title(), self.tabs()[2], self.puts[-1][0]), ('工作区', ('工作区', 'primary_filled'), 'hubcard'))
        self.assertIn('已把 beta 设为默认工作区', json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual([s for _, _, s in self.puts[1:]], list(range(1, len(self.puts))))  # One record's sequence.
        # The main thread's buttons on it: 删除 a trusted one, 保存用途; the tab stays.
        untrust, = [v for _, v in values(self.card(), main_card.UNTRUST)]
        self.assertEqual(self.press(untrust, 'ev-u1'), 'main_panel')
        self.assertEqual((settings.load(self.state).trusted, self.title()), ((), '工作区'))
        save, = [v for _, v in values(self.card(), main_card.NOTES)]
        typed = {'beta': '乙'}
        self.assertEqual(self.press(save, 'ev-n1', form_value={f'note_{i}': typed.get(n, '') for i, n in
                                                              enumerate(save['workspaces'])}), 'main_panel')
        self.assertEqual(settings.load(self.state).workspace_notes, (('beta', '乙'),))
        self.assertEqual((self.title(), self.hub_record()['tab']), ('工作区', 'workspace'))
        # Turned away, the manager's buttons find nothing.
        self.press(self.tab('代答'), 'ev-t2')
        drawn = len(self.puts)
        name = f'{MANAGER_PREFIX}{token}:{self.hub_record()["view"]["revision"]}:refresh'
        self.press({}, 'ev-d2', action_name=name, form_value={})
        self.run_queued()
        self.assertEqual(len(self.puts), drawn)

    @requires_extension()
    def test_without_the_main_thread_the_workspace_tab_is_the_list_alone(self):
        with mock.patch.dict(os.environ, {'COVEY_MAIN_THREAD': '0'}):
            self.open()
            self.assertEqual((self.title(), self.tabs()), ('工作区', row_of('工作区')[2:]))
            text = json.dumps(self.card(), ensure_ascii=False)
        self.assertIn('**alpha**', text)
        for part in ('保存用途', '记住的工作区', '"note_0"'):
            self.assertNotIn(part, text)

    def test_rotate_on_the_redaction_tab_redraws_the_same_card(self):
        from covey.app.dashboard_http import dashboard_token
        self.open()
        self.press(self.tab('脱敏'), 'ev-t1')
        before = dashboard_token(self.state)
        self.assertIn(f'/?t={before}', json.dumps(self.card()))
        rotate, = [v for _, v in values(self.card(), 'bridge_dashboard_rotate')]
        self.assertEqual(self.press(rotate, 'ev-x1'), 'dashboard_rotate')
        self.assertEqual(self.press(rotate, 'ev-x1'), 'duplicate')
        after = dashboard_token(self.state)
        self.assertNotEqual(after, before)
        self.assertEqual((self.puts[-1][0], self.title(), self.app.messenger.create_and_send_card.call_count),
                         ('hubcard', '脱敏看板', 1))
        text = json.dumps(self.card(), ensure_ascii=False)
        self.assertIn(f'/?t={after}', text)
        self.assertIn('口令已换', text)
        self.assertEqual(self.press(rotate, 'ev-x2', operator=MEMBER), 'not_owner')
        self.assertEqual(dashboard_token(self.state), after)
        # Turned away, 换口令 from the card drawn before changes nothing.
        self.press(self.tab('群聊'), 'ev-t2')
        self.assertEqual(self.press(rotate, 'ev-x3'), 'dashboard_rotate')
        self.assertEqual((dashboard_token(self.state), self.title()), (after, '群聊'))

    def test_an_older_card_on_the_retired_other_page_still_acts(self):
        self.open()
        token = self.hub.token('dm', 'ev-menu')
        path = self.hub.path(self.ctx, token)
        write_json(path, {**read_json(path), 'tab': 'other'})  # Shown on 其他, before it was retired.
        # Its 直接开工 lands on 新话题.
        start = {'kind': main_card.START, 'token': token, 'revision': 1, 'tier': 'readonly', 'on': True}
        self.assertEqual(self.press(start, 'ev-s1'), 'main_panel')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('新话题默认', 'topic'))
        # Its 其他 tab (clicked on another page) lands on 工作区, and a tab click from 其他 turns as any.
        self.assertEqual(self.press({'kind': TAB, 'tab': 'other', 'from': 'main', 'token': token}, 'ev-t1'),
                         'settings_tab')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('工作区', 'workspace'))
        write_json(path, {**read_json(path), 'tab': 'other'})
        self.assertEqual(self.press({'kind': TAB, 'tab': 'proxy', 'from': 'other', 'token': token}, 'ev-t2'),
                         'settings_tab')
        self.assertEqual(self.title(), '代答')
        # 记住的工作区 removed from it lands on 工作区; the menu's 设置 folds a record on 其他 as 工作区.
        write_json(path, {**read_json(path), 'tab': 'other'})
        untrust = {'kind': main_card.UNTRUST, 'token': token, 'revision': 1, 'key': 'alpha|read'}
        self.assertEqual(self.press(untrust, 'ev-u1'), 'main_panel')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('工作区', 'workspace'))
        write_json(path, {**read_json(path), 'tab': 'other'})
        self.assertEqual(self.hub.fold(self.ctx, 'dm', 'ev-menu', 'om_s1'), 'fold')
        self.assertEqual((self.title(), self.hub_record()['tab']), ('工作区', 'workspace'))
        self.assertEqual(self.hub.fold(self.ctx, 'dm', 'ev-menu', 'om_s1'), 'unfold')
        self.assertIn('**alpha**', json.dumps(self.card(), ensure_ascii=False))

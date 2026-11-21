"""点一下弹出，再点一下收回 (app/bot_menu.py, app/menu_panel.py toggle, app/settings_hub.py): a menu item clicked again
while its card is the newest top-level message in the private chat folds that card in place, the next click unfolds
it; anything newer (the owner's message, another item's card, a restart) or a card without a record gets a new card as
before. The 设置 card's tab shown folds and unfolds it too; every write takes the record's next sequence."""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from support.proxy import ProxyCase
from support import requires_extension
from covey.app import bot_menu
from covey.app.menu_panel import FOLDED
from covey.app.settings_hub import MENU_KEY
from covey.app.weekly_usage import FOLDER as USAGE_FOLDER
from covey.cards.settings_hub import TAB
from covey.core.groups import group_path
from covey.core.state import read_json, write_json

# Each item: its title on the card unfolded, the menu word its folded line names.
ITEMS = {'weekly_usage': ('本周用量', '用量'), 'proxy_settings': ('代答', '代答'), 'manage_groups': ('群聊', '群聊'),
         MENU_KEY: ('配置', '设置')}  # 设置 opens on its first tab, 配置 (T619; 新话题 since T546).


def tab_values(card):
    found = []

    def walk(value):
        if isinstance(value, dict):
            found.extend(b['value'] for b in value.get('behaviors') or () if b['value'].get('kind') == TAB)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(card)
    return found


class MenuFoldTests(ProxyCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_BUTLER': '1'}))
        super().setUp()
        self.access = self.app.runtime.access
        self.access.remember_owner_chat('dm')
        self.access.lookup = lambda profile, chat_id: {'external': False, 'name': '群' + chat_id[-2:]}
        write_json(group_path(self.ctx.state_dir, 'oc_g01'), {'chat_id': 'oc_g01', 'active': True, 'external': False})
        self.count, self.puts = 0, []
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=self.send)
        messenger.put_card = mock.Mock(side_effect=lambda profile, card_id, card, **kw: (
            self.puts.append((card_id, card, kw['sequence'])), {})[1])

    def send(self, profile, chat, card, key, **kw):
        self.count += 1
        self.puts.append(('new', card, 0))
        return f'cid{self.count}', f'om_{self.count}'

    def click(self, key, event_id, operator='owner'):
        """The menu click's outcome and how it went (fold, unfold, new; '' when it went nowhere)."""
        self.app.handle_menu_event({'event_id': event_id, 'event_key': key, 'operator_open_id': operator}, self.ctx)
        self.app.workspace_manager.queue.join()  # 工作区 opens on its queue.
        outcome = [line.split('outcome=')[1] for line in self.logs if line.startswith('bot menu key=')][-1]
        hows = [line.split()[-1] for line in self.logs
                if line.startswith(f'menu panel {key} ') and line.split()[-1] in ('fold', 'unfold', 'new')]
        return outcome, (hows[-1] if hows else '')

    def card(self):
        return self.puts[-1][1]

    def text(self, card=None):
        return json.dumps(card or self.card(), ensure_ascii=False)

    def entry(self, key):
        return read_json(self.ctx.state_dir / bot_menu.PANELS)[key]

    def press(self, value, event_id, message, operator='owner'):
        return self.app._handle_card_action_event({'event_id': event_id, 'operator_id': operator, 'chat_id': 'dm',
                                                   'message_id': message, 'action_value': value}, self.ctx)

    # The rule: when the card is the newest -------------------------------------------------------

    @requires_extension()
    def test_each_items_newest_card_folds_and_unfolds_in_place_with_increasing_sequences(self):
        for n, (key, (title, word)) in enumerate(ITEMS.items()):
            with self.subTest(key=key):
                self.assertEqual(self.click(key, f'{key}-1'), (key, 'new'))
                first = self.card()
                self.assertEqual(self.count, n + 1)
                cid, mid = f'cid{n + 1}', f'om_{n + 1}'
                self.assertEqual(self.entry(key)['message_id'], mid)
                self.assertEqual(self.click(key, f'{key}-2'), (key, 'fold'))
                card_id, folded, sequence = self.puts[-1]
                self.assertEqual((card_id, sequence, folded['header']['title']['content']), (cid, 1, title))
                lines = folded['body']['elements']
                self.assertEqual(lines[-1]['content'], f"<font color='grey'>已收起 · 再点"
                                 f"{'「配置」或' if key == MENU_KEY else ''}菜单「{word}」展开</font>")
                self.assertEqual(len(lines), 2 if key == MENU_KEY else 1)  # 设置: the tab row above the line.
                self.assertEqual(self.click(key, f'{key}-3'), (key, 'unfold'))
                card_id, whole, sequence = self.puts[-1]
                self.assertEqual((card_id, sequence, whole['header']['title']['content']), (cid, 2, title))
                self.assertNotIn('已收起', self.text(whole))
                self.assertEqual(len(whole['body']['elements']), len(first['body']['elements']))  # Drawn anew.
                self.assertEqual(self.click(key, f'{key}-4'), (key, 'fold'))  # Clicks alternate.
                self.assertEqual(self.puts[-1][::2], (cid, 3))
                self.assertEqual(self.count, n + 1)  # No new message, nothing recalled.
        self.app.messenger.recall.assert_not_called()
        self.assertEqual(self.app.messenger.send_message.call_count, 0)

    def test_a_click_made_before_the_card_was_out_is_clicking_twice_not_folding(self):
        # 2026-11-20 00:32: a cold 看板 took 5.7 s; the owner clicked again meanwhile and that click folded the new card.
        made = int(time.time() * 1000)
        self.assertEqual(self.click('weekly_usage', 'ev1'), ('weekly_usage', 'new'))
        self.app.handle_menu_event({'event_id': 'ev2', 'event_key': 'weekly_usage', 'operator_open_id': 'owner',
                                    'timestamp': str(made)}, self.ctx)
        self.assertIn('bot menu key=weekly_usage event=ev2 outcome=twice', self.logs)
        self.assertIn('menu panel weekly_usage twice', self.logs)
        self.assertEqual((self.count, len(self.puts)), (1, 1))  # Neither folded nor sent again.
        later = str(int((self.entry('weekly_usage')['sent'] + 1) * 1000))
        self.app.handle_menu_event({'event_id': 'ev3', 'event_key': 'weekly_usage', 'operator_open_id': 'owner',
                                    'menu_timestamp': later}, self.ctx)
        self.assertEqual(self.puts[-1][::2], ('cid1', 1))  # Made after the card was out: folds as before.
        self.assertIn('menu panel weekly_usage fold', self.logs)

    def test_click_time_reads_feishus_stamp_in_any_unit(self):
        for stamp in ('1763569966123', '1763569966123456', 1763569966, '1763569966123000000'):
            self.assertAlmostEqual(bot_menu.click_time({'timestamp': stamp}), 1763569966, delta=1)
        self.assertEqual(bot_menu.click_time({'menu_timestamp': '1763569900000', 'timestamp': '1763569966000'}),
                         1763569900.0)
        for event in ({}, {'timestamp': ''}, {'timestamp': 'x'}, {'timestamp': '0'}):
            self.assertIsNone(bot_menu.click_time(event))

    def test_an_owners_top_level_message_after_the_card_means_a_new_card(self):
        handled = []
        self.app.butler.handle_event = lambda ctx, event, owner: handled.append(event['message_id']) or True
        self.click('weekly_usage', 'ev1')
        self.app.handle_event({'chat_id': 'dm', 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': 'owner',
                               'message_id': 'm-thread', 'thread_id': 'omt_1', 'content': '话题里'}, self.ctx)
        self.assertEqual(self.click('weekly_usage', 'ev2'), ('weekly_usage', 'fold'))  # A reply in a thread is not.
        self.app.handle_event({'chat_id': 'dm', 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': 'owner',
                               'message_id': 'm-top', 'content': '顶层'}, self.ctx)
        self.assertEqual(handled, ['m-thread', 'm-top'])
        self.assertEqual(self.click('weekly_usage', 'ev3'), ('weekly_usage', 'new'))
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_1')
        self.assertEqual(self.click('weekly_usage', 'ev4'), ('weekly_usage', 'fold'))  # The new one is the newest.
        self.assertEqual(self.puts[-1][0], 'cid2')

    def test_another_items_card_after_it_means_a_new_card(self):
        self.click('weekly_usage', 'ev1')
        self.click('manage_groups', 'ev2')
        self.assertEqual(self.click('weekly_usage', 'ev3'), ('weekly_usage', 'new'))
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_1')
        self.assertEqual(self.click('manage_groups', 'ev4'), ('manage_groups', 'new'))  # 用量's is newer now.
        # Folding one does not move it: the other stays older.
        self.assertEqual(self.click('manage_groups', 'ev5'), ('manage_groups', 'fold'))
        self.assertEqual(self.click('weekly_usage', 'ev6'), ('weekly_usage', 'new'))

    def test_a_card_sent_before_this_process_started_means_a_new_card(self):
        self.click('weekly_usage', 'ev1')
        self.app.menu.started = time.time() + 1  # As after a restart: the card is from before it.
        self.assertEqual(self.click('weekly_usage', 'ev2'), ('weekly_usage', 'new'))
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_1')
        app, _ = self.bridge()  # A real restart: its own start.
        self.assertGreater(app.menu.started, self.entry('weekly_usage')['sent'])

    def test_a_card_from_before_records_or_menu_entries_from_before_send_a_new_card(self):
        self.click('weekly_usage', 'ev1')
        path = self.ctx.state_dir / bot_menu.PANELS
        entry = self.entry('weekly_usage')
        self.assertEqual(set(entry), {'chat_id', 'message_id', 'at', 'sent', 'source'})
        # An entry written before this change (no ``sent``, no ``source``): a new card, the old one recalled.
        write_json(path, {'weekly_usage': {'chat_id': 'dm', 'message_id': 'om_1', 'at': entry['at']}})
        self.assertEqual(self.click('weekly_usage', 'ev2'), ('weekly_usage', 'new'))
        self.app.messenger.recall.assert_called_with(self.ctx.profile, 'om_1')
        # A plain card from before it had a record: the entry is the newest, but there is nothing to fold it by.
        for record in (self.ctx.state_dir / USAGE_FOLDER).glob('*.json'):
            record.unlink()
        self.assertEqual(self.click('weekly_usage', 'ev3'), ('weekly_usage', 'new'))
        self.app.messenger.recall.assert_called_with(self.ctx.profile, 'om_2')
        self.assertEqual(self.count, 3)
        self.assertTrue(all(cid == 'new' for cid, _, _ in self.puts))

    def test_only_the_owner_folds(self):
        self.click('weekly_usage', 'ev1')
        self.assertEqual(self.click('weekly_usage', 'ev2', operator='ou_someone'), ('not_owner', 'new'))
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.click('weekly_usage', 'ev3'), ('weekly_usage', 'fold'))

    def test_a_panels_own_redraw_leaves_it_unfolded(self):
        self.click('manage_groups', 'ev1')
        self.click('manage_groups', 'ev2')
        path = next((self.ctx.state_dir / 'group-managers').glob('*.json'))
        self.assertTrue(read_json(path)[FOLDED])
        record = read_json(path)
        self.app.group_manager.menu.publish(self.ctx, path, record, {**record['view'], 'revision': 9}, {'body': {}})
        self.assertNotIn(FOLDED, read_json(path))
        self.assertEqual(self.click('manage_groups', 'ev3'), ('manage_groups', 'fold'))

    # The record 用量 has now ---------------------------------------------------------

    def test_usage_has_a_record(self):
        for key, folder in (('weekly_usage', USAGE_FOLDER),):
            with self.subTest(key=key):
                self.click(key, key + '-1')
                [path] = (self.ctx.state_dir / folder).glob('*.json')
                record = read_json(path)
                self.assertEqual((record['status'], record['chat_id'], record['card_id'], record['message_id'],
                                  record['sequence'], record['view'], record['source_event_id']),
                                 ('open', 'dm', f'cid{self.count}', f'om_{self.count}', 0, {'revision': 1}, key + '-1'))
                self.click(key, key + '-2')
                self.assertEqual((read_json(path)[FOLDED], read_json(path)['sequence']), (True, 1))
                self.click(key, key + '-3')
                self.assertEqual((FOLDED in read_json(path), read_json(path)['sequence'], read_json(path)['view']),
                                 (False, 2, {'revision': 3}))

    def test_a_card_that_did_not_go_out_sends_its_text_as_before(self):
        self.app.messenger.create_and_send_card = mock.Mock(return_value=('', ''))
        self.click('weekly_usage', 'ev1')
        self.assertIn('本周用量', self.app.messenger.send_message.call_args.args[2])
        self.app.messenger.recall.assert_not_called()

    # The 设置 card's tabs ----------------------------------------------------------------------------

    @requires_extension()
    def test_the_tab_shown_folds_and_unfolds_another_tab_turns_unfolded(self):
        self.click(MENU_KEY, 'ev1')
        tabs = tab_values(self.card())
        main = next(v for v in tabs if v['tab'] == 'config')  # The tab shown (T619: the first, 配置).
        path = self.app.settings_hub.path(self.ctx, main['token'])
        self.assertEqual(self.press(main, 'ev-t1', 'om_1'), 'settings_fold')
        card_id, folded, sequence = self.puts[-1]
        self.assertEqual((card_id, sequence, folded['header']['title']['content']), ('cid1', 1, '配置'))
        self.assertEqual([v['tab'] for v in tab_values(folded)],
                         ['config', 'workspace', 'redaction', 'groups', 'about', 'doctor'])  # 能做什么, 诊断: always.
        self.assertEqual(len(folded['body']['elements']), 2)  # The tab row and the line, none of the panel.
        record = read_json(path)
        self.assertEqual((record[FOLDED], record['tab'], record['turned']), (True, 'config', ['ev-t1']))
        self.assertEqual(self.press(main, 'ev-t1', 'om_1'), 'settings_unfold')  # Delivered again: nothing more.
        self.assertEqual(len(self.puts), 2)
        self.assertEqual(self.press(main, 'ev-t2', 'om_1'), 'settings_unfold')
        self.assertEqual((self.puts[-1][0], self.puts[-1][2]), ('cid1', 2))
        self.assertNotIn('已收起', self.text())
        self.assertNotIn(FOLDED, read_json(path))
        self.assertEqual(self.press(main, 'ev-t3', 'om_1'), 'settings_fold')
        # Another tab, from the folded card: that tab, unfolded.
        groups = next(v for v in tab_values(self.card()) if v['tab'] == 'groups')
        self.assertEqual(self.press(groups, 'ev-t4', 'om_1'), 'settings_tab')
        self.assertEqual((self.puts[-1][0], self.puts[-1][2], self.card()['header']['title']['content']),
                         ('cid1', 4, '群聊'))
        self.assertNotIn('已收起', self.text())
        record = read_json(path)
        self.assertEqual((FOLDED in record, record['tab'], record['sequence']), (False, 'groups', 4))
        # The menu's 设置 folds it on the tab it shows; the tab unfolds it.
        self.assertEqual(self.click(MENU_KEY, 'ev2'), (MENU_KEY, 'fold'))
        self.assertEqual((self.card()['header']['title']['content'], self.puts[-1][2]), ('群聊', 5))
        self.assertIn('再点「群聊」或菜单「设置」展开', self.text())
        self.assertEqual(self.press(groups, 'ev-t5', 'om_1'), 'settings_unfold')
        self.assertEqual(self.puts[-1][2], 6)
        self.assertEqual(self.count, 1)
        self.app.messenger.recall.assert_not_called()

    def test_only_the_owner_folds_by_the_tab(self):
        self.click(MENU_KEY, 'ev1')
        main = next(v for v in tab_values(self.card()) if v['tab'] == 'config')
        self.assertEqual(self.press(main, 'ev-t1', 'om_1', operator='ou_member0012345678'), 'not_owner')
        self.assertNotIn(FOLDED, read_json(self.app.settings_hub.path(self.ctx, main['token'])))
        self.assertNotIn('已收起', self.text())

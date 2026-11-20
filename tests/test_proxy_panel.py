"""Proxy replies (代答) on cards: the bot menu's 代答设置 card, the 重新发送 card of a reply that never went out,
and the web login's caches (each case builds on the batches and controls of support/proxy.py)."""
import json
import re
import threading
import time
import unittest
from collections import Counter
from unittest import mock

from support import require_extension
require_extension()
from support.fake_lark import FakeCardKit
from support.proxy import GROUP, OWNER, PERSON, ProxyCase, controls, item, lark, owner_cli, user, walk
from covey.app.card_actions import ACTIONS, BOT_ONLY
from covey.app.card_lanes import CardLanes
from covey.app.proxy_panel import GONE, OWNER_ONLY, STALE
from covey.cards import proxy_panel as panel
from covey.cards.elements import element_count, tag
from covey.core import proxy as records


def whitelist_folds(card):
    """The folded panels on a 代答 card but the model rows' (set_p_*)."""
    return [e for e in walk(card) if e.get('tag') == 'collapsible_panel' and not str(e.get('element_id')).startswith('set_p_')]


class PanelTests(ProxyCase):
    """The bot menu's 代答设置 sends a card to the owner's private chat where everything 代答 has is a control."""

    def setUp(self):
        super().setUp()
        self.lark = FakeCardKit().start(self)
        self.app.messenger.create_and_send_card = mock.Mock(return_value=('pcid', 'om_panel'))

    def open(self, event_id='ev-panel'):
        self.app.messenger.create_and_send_card.reset_mock()
        self.app.runtime.access.remember_owner_chat('chat')
        self.app.menu.owner_said('chat')  # The owner wrote since: a new card, not the last one folded (app/bot_menu.py).
        self.app.handle_menu_event({'event_id': event_id, 'event_key': 'proxy_settings', 'operator_open_id': 'owner'},
                                   self.ctx)
        self.app.messenger.create_and_send_card.assert_called_once()
        return self.app.messenger.create_and_send_card.call_args

    def token(self):
        return json.loads((records.proxy_dir(self.state) / 'panels' / 'latest.json').read_text())['token']

    def record(self, token=None):
        return json.loads((records.proxy_dir(self.state) / 'panels' / f'{token or self.token()}.json').read_text())

    def click_event(self, kind, *, token=None, revision=None, operator='owner', message='om_panel', option=None, query=None,
                    **value):
        token = token or self.token()
        revision = self.record(token)['view']['revision'] if revision is None else revision
        event = {'operator_id': operator, 'chat_id': 'chat', 'message_id': message, 'event_id': 'ev-' + kind[-6:],
                 'action_value': {'kind': kind, 'token': token, 'revision': revision, **value}}
        if option is not None:
            event['option'] = option
        if query is not None:
            event['form_value'] = json.dumps({'query': query}, ensure_ascii=False)  # The client sends a string.
        return event

    def click(self, kind, **options):
        return self.app._handle_card_action_event(self.click_event(kind, **options), self.ctx)

    def tap(self, kind, option):
        """A tap on a settings row's option (cards/launch_config.py setting_row): the option is in the value."""
        event = self.click_event(kind)
        event['action_value']['option'] = option
        return self.app._handle_card_action_event(event, self.ctx)

    def puts(self):
        return [call for call in self.lark.calls if call.method == 'PUT']

    def shown(self):
        return self.puts()[-1].card

    def text(self):
        return json.dumps(self.shown(), ensure_ascii=False)

    def contact(self, key='p2p:ou_zhang'):
        return next((c for c in records.read_contacts(self.state) if c['key'] == key), None)

    def search(self, users=(), chats=(), **answers):
        return owner_cli(side_effect=lark(users, chats, **answers))

    def buttons(self, kind, card=None):
        """(label, value, disabled) of each control of ``kind`` on the card shown last."""
        return [(label, value, element.get('disabled')) for k, label, value, element in controls(card or self.shown())
                if k == kind]

    def lookup_id(self):
        return self.record()['view']['lookup']['id']

    def test_the_menu_opens_one_card_in_the_private_chat_with_every_control(self):
        topics = set((self.state / 'topics').iterdir()) if (self.state / 'topics').exists() else set()
        call = self.open()
        self.app.messenger.send_message.assert_not_called()  # The card is the answer; no status text.
        self.assertEqual(call.args[1], 'chat')
        self.assertNotIn('reply_to_message_id', call.kwargs)  # A new message: the menu click has none to reply to.
        card = call.args[2]
        head = card['header']
        self.assertEqual((head['title']['content'], head['subtitle']['content'], head['text_tag_list'][0]['text']['content']),
                         ('代答', '群「代答」', '已开启'))
        # The status line first (no tiles), the model rows last under a heading that names the model.
        lines = [e['content'] for e in walk(card['body']['elements'][0]) if e.get('tag') == 'markdown']
        self.assertEqual(lines[0], "**代答开着** <font color='grey'>｜</font> 白名单 1 个")
        self.assertNotIn('tile', json.dumps(card['body']['elements'][:2]))
        order = [e['content'] for e in card['body']['elements'] if e.get('tag') == 'markdown' and 'icon' in e]
        self.assertEqual([c.split('**')[1].split(' · ')[0].split('（')[0] for c in order], ['白名单', '添加', '代答话题的模型'])
        self.assertIn('代答话题的模型 · Codex Model · 思考 high', order[-1])
        kinds = {(kind, label) for kind, label, _, _ in controls(card)}
        rows = {panel.HARNESS, panel.PROVIDER, panel.TIER_MODEL, panel.TIER_BUDGET}
        self.assertEqual(kinds, {(panel.DISABLE, '关闭代答'), (panel.LOGIN, '去登录'), (panel.REFRESH, '刷新'),
                                 *((kind, 'interactive_container') for kind in rows),
                                 (panel.PAUSE, '暂停'), (panel.REMOVE, '删除'), (panel.FIND, '查找')})
        token = self.token()
        self.assertTrue(all({k: v for k, v in value.items() if k != 'option'} == {
            'token': token, 'revision': 1, **({'contact': 'p2p:ou_zhang'} if kind in (panel.PAUSE, panel.REMOVE) else {})}
            for kind, _, value, _ in controls(card)))
        self.assertEqual(self.chosen(card), {'Harness': 'Codex', '供应商': 'Codex 自带', '模型': 'Codex Model', '思考预算': 'high'})
        self.assertIn(f"**张三** {tag('私聊', 'wathet')} {tag('运行中', 'green')}", json.dumps(card, ensure_ascii=False))
        self.assertIn(panel.MODEL_NOTE, json.dumps(card, ensure_ascii=False))
        # Only 查找 is in the form; every other control is outside it. The box takes several names.
        [form] = [e for e in walk(card) if e.get('tag') == 'form']
        self.assertEqual([k for k, *_ in controls(form)], [panel.FIND])
        [box] = [e for e in walk(form) if e.get('tag') == 'input']
        self.assertEqual((box['max_length'], box['input_type']), (1000, 'multiline_text'))
        self.assertEqual(self.record()['status'], 'open')
        self.assertEqual(set((self.state / 'topics').iterdir()) if (self.state / 'topics').exists() else set(), topics)
        self.assertNotIn('文字命令', json.dumps(card, ensure_ascii=False))
        self.open('ev-panel2')  # Each click its own card.
        self.assertNotEqual(self.record()['source_event_id'], 'ev-panel')

    def test_a_groups_members_are_removed_one_at_a_time_under_逐个管理(self):
        source = {'chat_id': 'oc_team', 'name': '项目群'}
        for index, name in enumerate(['甲', '乙']):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_m{index}', 'target': f'ou_m{index}', 'name': name,
                                               'added_at': 10 + index, 'source': source})
        call = self.open()
        [(label, value, _)] = self.buttons(panel.SOURCE_OPEN, call.args[2])
        self.assertEqual((label, value['source']), ('逐个管理', 'oc_team'))
        self.assertEqual(len(self.buttons(panel.REMOVE, call.args[2])), 1)  # 张三's: the group's members fold.
        self.click(panel.SOURCE_OPEN, source='oc_team')
        self.assertEqual(self.buttons(panel.SOURCE_OPEN)[0][0], '收起')
        self.assertIn(f"甲 {tag('运行中', 'green')}", self.text())
        removes = [v['contact'] for _, v, _ in self.buttons(panel.REMOVE)]
        self.assertEqual(removes, ['p2p:ou_zhang', 'p2p:ou_m0', 'p2p:ou_m1'])
        self.click(panel.REMOVE, contact='p2p:ou_m0')
        self.assertIn('已移出白名单：甲', self.text())
        self.assertEqual([c['key'] for c in records.read_contacts(self.state)], ['p2p:ou_zhang', 'p2p:ou_m1'])
        self.assertIn(f"**群「项目群」的成员** {tag('1 人', 'violet')}", self.text())  # Still open after the removal.
        self.click(panel.SOURCE_OPEN, source='oc_team')
        self.assertEqual(self.buttons(panel.SOURCE_OPEN)[0][0], '逐个管理')
        self.assertNotIn(f"乙 {tag('运行中', 'green')}", self.text())
        self.click(panel.SOURCE_OPEN, source='oc_gone')
        self.assertIn('白名单里已经没有这一项', self.text())

    def test_a_login_seen_good_is_a_grey_line_not_a_button_and_closing_is_no_primary_button(self):
        self.app.proxy.web_login.login_seen = (True, 1)
        card = self.open().args[2]
        self.assertNotIn(panel.LOGIN, {k for k, *_ in controls(card)})
        self.assertIn("<font color='grey'>网页登录正常</font>", json.dumps(card, ensure_ascii=False))
        [(_, _, _, close)] = [c for c in controls(card) if c[0] == panel.DISABLE]
        self.assertEqual(close['type'], 'text')
        self.app.proxy.web_login.login_seen = (False, 1)
        self.click(panel.REFRESH)
        self.assertIn(panel.LOGIN, {k for k, *_ in controls(self.shown())})
        self.assertNotIn('网页登录正常', json.dumps(self.shown(), ensure_ascii=False))

    def test_switching_off_and_on_redraws_the_card_and_on_works_after_the_click_returns(self):
        self.open()
        self.assertEqual(self.click(panel.DISABLE), 'proxy_panel')
        self.assertFalse(records.read_settings(self.state)['enabled'])
        self.assertIn('代答已关闭', self.text())
        self.assertEqual(self.shown()['header']['text_tag_list'][0]['text']['content'], '已关闭')
        self.assertIn(panel.ENABLE, {k for k, *_ in controls(self.shown())})
        # 开启 reads the owner as the owner's profile: after the click returned, on a real lane, while
        # the busy card is drawn; the click itself writes no card.
        seen = []
        me = {'data': {'users': [{'open_id': OWNER, 'localized_name': '王小明'}]}}

        def identity(*args, **kwargs):
            seen.append([line for line in self.logs if line.startswith('card action')])
            return me
        stop = threading.Event()
        self.addCleanup(stop.set)
        lanes = CardLanes(self.app.handle_card_action_event, self.app.card_lane_key, self.app.runtime.log, stop)
        with owner_cli(side_effect=identity):
            lanes.run(self.click_event(panel.ENABLE), self.ctx)
        [acknowledged] = seen
        self.assertRegex(acknowledged[-1], r'^card action kind=bridge_proxy_enable .* outcome=proxy_panel ')
        busy, result = self.puts()[1].card, self.puts()[2].card
        self.assertIn('⏳ 正在开启代答…', json.dumps(busy, ensure_ascii=False))
        self.assertEqual({k for k, _, _, e in controls(busy) if e['tag'] == 'button' and not e['disabled']}, {panel.REFRESH})
        self.assertTrue(records.read_settings(self.state)['enabled'])
        self.assertIn('代答已开启', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('⏳', self.text())
        self.assertEqual([call.data['sequence'] for call in self.puts()], [1, 2, 3])
        self.assertEqual(len({call.data['uuid'] for call in self.puts()}), 3)
        self.assertEqual(self.proxy.panel.working, {})

    def chosen(self, card=None):
        """{row label: its choice} of the model rows on the card shown last: the ✓ option under each fold."""
        rows = {}
        for element in walk(card or self.shown()):
            if element.get('tag') == 'collapsible_panel' and str(element.get('element_id', '')).startswith('set_p_'):
                label = re.sub(r'<[^>]+>|\*', '', element['header']['title']['content']).split('　')[0]
                [mark] = [e['content'] for e in walk(element) if e.get('tag') == 'markdown' and e['content'].startswith('✓')]
                rows[label] = re.sub(r'<[^>]+>|\*|✓ ', '', mark)
        return rows

    def test_the_model_is_chosen_on_the_launch_cards_rows_harness_provider_model_budget(self):
        # The owner, 2026-11-10: 代答's model as the launch card's settings rows, not a select of every model.
        menu = {'codex:gpt-own': {'harness': 'codex', 'title': 'Codex', 'model': 'gpt-own', 'provider': 'openai',
                                  'provider_title': '自带', 'display_name': 'gpt-own', 'label': 'Codex · gpt-own', 'levels': ['low', 'high']},
                'codex:acme/gpt-6-astra': {'harness': 'codex', 'title': 'Codex', 'model': 'acme/gpt-6-astra',
                                             'provider': 'acme', 'provider_title': 'Acme',
                                             'display_name': 'gpt-6-astra', 'label': 'Codex · Acme · gpt-6-astra', 'levels': ['low', 'medium', 'high', 'xhigh']},
                'codex:acme/other': {'harness': 'codex', 'title': 'Codex', 'model': 'acme/other', 'provider': 'acme',
                                       'provider_title': 'Acme', 'display_name': 'other', 'label': 'Codex · Acme · other', 'levels': ['low', 'high']},
                'claude:claude-model': {'harness': 'claude', 'title': 'Claude Code', 'model': 'claude-model',
                                        'provider': 'anthropic', 'display_name': 'claude model',
                                        'label': 'Claude Code · claude model', 'levels': ['low', 'high', 'xhigh', 'max']}}
        records.write_settings(self.state, {**records.read_settings(self.state), 'model': 'acme/gpt-6-astra'})
        self.enterContext(mock.patch('covey.app.proxy_panel.available_models', return_value=menu))
        card = self.open().args[2]
        self.assertNotIn('select_static', json.dumps(card))
        self.assertEqual(self.chosen(card), {'Harness': 'Codex', '供应商': 'Acme', '模型': 'gpt-6-astra', '思考预算': 'high'})
        options = {}
        for kind, _, value, _ in controls(card):
            options.setdefault(kind, []).append(value.get('option'))
        self.assertEqual(options[panel.HARNESS], ['codex', 'claude'])
        self.assertEqual(options[panel.PROVIDER], ['openai', 'acme'])
        self.assertEqual(options[panel.TIER_MODEL], ['acme/gpt-6-astra', 'acme/other'])  # That provider's only.
        self.assertEqual(options[panel.TIER_BUDGET], ['xhigh', 'high', 'medium', 'low', 'default'])

        def settings():
            saved = records.read_settings(self.state)
            return saved['harness'], saved['model'], saved['effort'], saved.get('model_changed_at')

        # The same choice on each row (the client acceptance's step): the card is drawn again, nothing changes.
        before = settings()
        for kind, option in [(panel.HARNESS, 'codex'), (panel.PROVIDER, 'acme'), (panel.TIER_MODEL, 'acme/gpt-6-astra'),
                             (panel.TIER_BUDGET, 'high')]:
            puts = len(self.puts())
            self.assertEqual(self.tap(kind, option), 'proxy_panel')
            self.assertEqual(len(self.puts()), puts + 1)
            self.assertEqual(settings(), before)
            self.assertIn('代答话题模型没有变', self.text())
        self.assertIsNone(before[3])
        # Each row's choice is saved; the change is marked, so Proxy.sync_topics moves the idle contact topics over.
        steps = [(panel.TIER_BUDGET, 'xhigh', ('codex', 'acme/gpt-6-astra', 'xhigh')),
                 (panel.TIER_MODEL, 'acme/other', ('codex', 'acme/other', 'high')),  # Its own default budget.
                 (panel.PROVIDER, 'openai', ('codex', 'gpt-own', 'high')),
                 (panel.HARNESS, 'claude', ('claude', 'claude-model', 'xhigh'))]
        for kind, option, want in steps:
            marked = settings()[3] or 0
            time.sleep(0.01)
            self.tap(kind, option)
            self.assertEqual(settings()[:3], want, kind)
            self.assertGreater(settings()[3], marked)
            self.assertIn('已有联系人话题空闲时也会换过去', self.text())
        self.assertEqual(self.chosen(), {'Harness': 'Claude Code', '供应商': 'anthropic', '模型': 'claude model',
                                         '思考预算': 'xhigh'})
        # Gone from the menu, or a budget the model lacks: refused on the card, nothing saved.
        for kind, option, why in [(panel.HARNESS, 'gone', '这个 Harness 已不在'), (panel.PROVIDER, 'acme', '这个供应商已不在'),
                                  (panel.TIER_MODEL, 'gone', '这个模型已不在'), (panel.TIER_BUDGET, 'ultra', '没有这一档')]:
            self.tap(kind, option)
            self.assertIn(why, self.text())
            self.assertEqual(settings()[:3], want)
        with mock.patch.object(self.proxy.contact_topics, 'sync_topics') as sync:
            self.proxy.tick(self.ctx, self.proxy.settings(self.ctx))
        sync.assert_called_once()
        self.assertEqual(sync.call_args.args[1]['model'], 'claude-model')

    def test_an_older_cards_model_and_budget_selects_still_act(self):
        # A card drawn before the rows (2026-11-10) has selects whose click carries the option beside the value.
        self.open()
        self.assertEqual(self.click(panel.MODEL, option='claude:claude-model'), 'proxy_panel')
        settings = records.read_settings(self.state)
        self.assertEqual((settings['harness'], settings['model'], settings['effort']), ('claude', 'claude-model', 'xhigh'))
        self.assertTrue(settings['model_changed_at'])
        self.assertEqual(self.chosen(), {'Harness': 'Claude Code', '供应商': 'Claude 自带', '模型': 'claude model',
                                         '思考预算': 'xhigh'})
        self.assertNotIn('select_static', self.text())
        self.assertIn('已有话题空闲时换过去', self.text())
        self.assertEqual(self.click(panel.BUDGET, option='max'), 'proxy_panel')
        self.assertEqual(records.read_settings(self.state)['effort'], 'max')
        self.click(panel.BUDGET, option='ultra')
        self.assertEqual(records.read_settings(self.state)['effort'], 'max')
        self.assertIn('没有这一档思考预算', self.text())
        self.click(panel.MODEL, option='gone:model')
        self.assertIn('已不在可选目录中', self.text())

    def test_a_unique_match_waits_for_add_all_and_an_ambiguous_name_for_its_candidates_add(self):
        self.open()
        users = {'李四': [user('ou_li4', '李四四'), user('ou_li', '李四', p2p_chat_id='oc_li', department='数据')],
                 '王五': [user('ou_a', '王五', department='甲'), user('ou_b', '王五', department='乙')]}
        with self.search(users, {'李四': [{'chat_id': 'oc_g9', 'name': '李四的群'}]}):
            self.assertEqual(self.click(panel.FIND, query=' 李四 '), 'proxy_panel')
        self.assertEqual(len(records.read_contacts(self.state)), 1)  # A unique exact match is not added by itself.
        [item] = self.record()['view']['lookup']['items']
        self.assertEqual((item['state'], item['candidates'][0]['target']), ('unique', 'ou_li'))
        self.assertIn(f"**李四** · 数据 · 私聊 {tag('待添加', 'blue')}", self.text())
        self.assertEqual(self.buttons(panel.ADD_ALL), [('全部添加（1）', {'token': self.token(), 'revision': 3,
                                                                       'lookup': self.lookup_id()}, False)])
        self.assertEqual(self.click(panel.ADD_ALL, lookup=self.lookup_id()), 'proxy_panel')
        added = self.contact('p2p:ou_li')
        self.assertEqual((added['chat_id'], added['name']), ('oc_li', '李四'))
        self.assertIn('已加入白名单 1 个：李四', self.text())
        # Added: the result leaves the card (UI review P12: it stayed under the new row); the query stays.
        self.assertNotIn('查找结果', self.text())
        self.assertNotIn('待添加', self.text())
        self.assertEqual(self.buttons(panel.ADD_ALL), [])
        self.assertIn('"default_value": "李四"', self.text())
        with self.search(users):
            self.click(panel.FIND, query='王五')
        adds = [(label, value['candidate']) for label, value, _ in self.buttons(panel.ADD)]
        self.assertEqual(adds, [('添加', 'p2p:ou_a'), ('添加', 'p2p:ou_b')])
        self.assertIn(f"**“王五”** {tag('2 个匹配', 'orange')} 在下面选", self.text())
        result, drawn = self.lookup_id(), self.record()['view']['revision']
        self.assertEqual(self.click(panel.ADD, candidate='p2p:ou_b', lookup=result), 'proxy_panel')
        self.assertEqual(self.contact('p2p:ou_b')['name'], '王五')
        self.assertEqual(self.buttons(panel.ADD), [])
        self.assertIsNone(self.record()['view']['lookup'])
        # A second 添加 on the card as it was drawn before still adds (a quick second click is not lost).
        self.assertEqual(self.click(panel.ADD, candidate='p2p:ou_a', lookup=result, revision=drawn), 'proxy_panel')
        self.assertEqual(self.contact('p2p:ou_a')['name'], '王五')
        self.click(panel.ADD, candidate='p2p:ou_x', lookup=result)
        self.assertIn('查找结果已失效', self.text())
        self.click(panel.ADD, index=7)  # A card from before several names.
        self.assertIn('查找结果已失效', self.text())

    def test_several_names_are_looked_up_at_once_each_with_its_own_result(self):
        self.open()
        users = {'李四': [user('ou_li', '李四')], '张三': [user('ou_zhang', '张三')],
                 '王五': [user('ou_a', '王五'), user('ou_b', '王五')]}
        started = time.monotonic()
        with self.search(users, delay=.3) as cli:
            self.click(panel.FIND, query='张三，李四 王五、没这个人')
        elapsed = time.monotonic() - started
        searched = cli.side_effect.calls
        self.assertEqual(len(searched), 10)  # People and groups of each name, and of "李四 王五" whole.
        self.assertLess(elapsed, 1.6)  # 3 s one after another.
        lines = [e['content'] for e in walk(self.shown()) if e.get('tag') == 'markdown']
        self.assertIn("<font color='grey'>查找结果（4 个名字）：</font>", lines)
        self.assertIn(f"**张三** · 人 · 私聊 {tag('已在白名单', 'green')}\n**李四** · 人 · 私聊 {tag('待添加', 'blue')}\n"
                      f"**“王五”** {tag('2 个匹配', 'orange')} 在下面选\n**“没这个人”** {tag('没找到', 'neutral')}", lines)
        self.assertEqual([label for label, _, _ in self.buttons(panel.ADD_ALL)], ['全部添加（1）'])
        self.assertEqual(len(self.buttons(panel.ADD)), 2)

    def test_a_group_name_with_a_space_is_one_name_and_offers_its_members(self):
        self.open()
        chats = {'Covey 验收测试': [{'chat_id': 'oc_acc', 'name': 'Covey 验收测试'}],
                 '验收测试': [{'chat_id': 'oc_acc', 'name': 'Covey 验收测试'}]}
        with self.search((), chats, info={'oc_acc': {'name': 'Covey 验收测试', 'user_count': '1'}}):
            self.click(panel.FIND, query='Covey 验收测试')
        [item] = self.record()['view']['lookup']['items']
        self.assertEqual((item['state'], item['candidates'][0]['members']), ('unique', 1))
        self.assertIn(f"**Covey 验收测试** · 群 · 群 @我 {tag('待添加', 'blue')}", self.text())
        self.assertEqual([label for label, _, _ in self.buttons(panel.ADD)], ['群里@我'])
        self.assertEqual(self.buttons(panel.MEMBERS)[0][:2], ('全员 1 人', {
            'token': self.token(), 'revision': 3, 'group': 'oc_acc', 'name': 'Covey 验收测试'}))
        self.click(panel.ADD_ALL, lookup=self.lookup_id())
        self.assertEqual(self.contact('group:oc_acc')['kind'], 'group')
        self.assertIn('已加入白名单 1 个：Covey 验收测试（群里 @你的消息）', self.text())

    def test_a_search_without_a_query_or_a_result_says_so_on_the_card(self):
        self.open()
        with owner_cli() as cli:
            self.click(panel.FIND, query='  ')
        cli.assert_not_called()
        self.assertIn('请先在输入框里填', self.text())
        with self.search():
            self.click(panel.FIND, query='没这个人')
        self.assertIn(f"**“没这个人”** {tag('没找到', 'neutral')}", self.text())
        with self.search(fail=RuntimeError('token expired')):
            self.click(panel.FIND, query='张三')
        self.assertIn('⚠️ 查找联系人未完成：token expired', self.text())
        self.assertNotIn('⏳', self.text())

    def members(self, users, bots=1, total=None):
        return {'oc_team': {'users': [{'member_id': u, 'name': name} for u, name in users], 'bots': [{}] * bots,
                            'user_total': total if total is not None else len(users), 'has_more': False}}

    def test_a_groups_members_are_added_after_a_confirmation_and_fold_into_one_row(self):
        self.open()
        team = [(OWNER, '王小明'), ('ou_zhang', '张三'), ('ou_a', '甲'), ('ou_b', '乙')]
        with self.search((), (), info={'oc_team': {'name': '项目群', 'user_count': '4'}},
                         members=self.members(team)) as cli:
            self.click(panel.FIND, query='oc_team')
            self.assertEqual(self.buttons(panel.MEMBERS)[0][0], '全员 4 人')
            self.assertEqual(self.click(panel.MEMBERS, group='oc_team', name='项目群'), 'proxy_panel')
        listing = [c for c in cli.side_effect.calls if c[1] == '+chat-members-list'][0]
        self.assertEqual(listing[listing.index('--page-size') + 1], '100')
        self.assertIn('**加入群「项目群」的全体成员**', self.text())
        self.assertIn('共 4 人（另有机器人 1 个）；不加你自己和已在白名单的 2 人。将以私聊代答加入 **2 人**', self.text())
        self.assertIn('甲、乙', self.text())
        pending = self.record()['view']['members']['id']
        self.assertEqual([label for label, _, _ in self.buttons(panel.MEMBERS_ADD)], ['确认添加 2 人'])
        self.assertEqual(self.click(panel.MEMBERS_CANCEL, pending=pending), 'proxy_panel')
        self.assertIn('一个也没有加', self.text())
        self.assertEqual(len(records.read_contacts(self.state)), 1)
        with self.search(members=self.members(team)):
            self.click(panel.MEMBERS, group='oc_team', name='项目群')
        pending = self.record()['view']['members']['id']
        self.assertEqual(self.click(panel.MEMBERS_ADD, pending=pending), 'proxy_panel')
        added = [c for c in records.read_contacts(self.state) if records.source_of(c)]
        self.assertEqual([(c['target'], c['kind'], c['source']) for c in added],
                         [('ou_a', 'p2p', {'chat_id': 'oc_team', 'name': '项目群'}),
                          ('ou_b', 'p2p', {'chat_id': 'oc_team', 'name': '项目群'})])
        self.assertIn('已把群「项目群」的 2 人加入白名单', self.text())
        self.assertIn(f"**群「项目群」的成员** {tag('2 人', 'violet')} {tag('运行中 2', 'green')}", self.text())
        # The search the group came from left the card with the add: its 全员 button with it.
        self.assertEqual([label for kind in (panel.SOURCE_PAUSE, panel.SOURCE_REMOVE, panel.MEMBERS)
                          for label, _, _ in self.buttons(kind)], ['暂停全部', '删除全部', '同步成员'])
        self.assertNotIn('查找结果', self.text())
        self.assertNotIn('甲', self.text().split('**添加**')[0])  # The members are not rows of their own.
        # A second 确认 (a click from the card before) has nothing left to confirm.
        self.assertEqual(self.click(panel.MEMBERS_ADD, pending=pending, revision=3), 'proxy_stale')
        self.assertIn(STALE + '这个确认已失效', self.text())
        self.assertEqual(len(records.read_contacts(self.state)), 3)

    def test_a_group_with_nobody_to_add_or_too_many_says_so_without_a_confirmation(self):
        self.open()
        with self.search(members=self.members([(OWNER, '王小明')])):
            self.click(panel.MEMBERS, group='oc_team', name='Covey 验收测试')
        self.assertIn('⚠️ 群「Covey 验收测试」里没有可以加入的人：共 1 人，除去你自己和已在白名单的 1 人后为 0（机器人 1 个不加）。',
                      self.text())
        self.assertEqual(self.buttons(panel.MEMBERS_ADD), [])
        with self.search(members=self.members([('ou_a', '甲')], total=records.MEMBER_LIMIT + 1)):
            self.click(panel.MEMBERS, group='oc_team', name='大群')
        self.assertIn(f'有 {records.MEMBER_LIMIT + 1} 人，超过一次最多加入的 {records.MEMBER_LIMIT} 人', self.text())
        self.assertEqual(self.buttons(panel.MEMBERS_ADD), [])

    def test_a_groups_members_are_paused_resumed_synced_and_removed_together(self):
        source = {'chat_id': 'oc_team', 'name': '项目群'}
        for index in range(3):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_m{index}', 'target': f'ou_m{index}',
                                               'name': f'成员{index}', 'added_at': 10 + index, 'source': source})
        self.open()
        self.assertEqual(self.click(panel.SOURCE_PAUSE, source='oc_team'), 'proxy_panel')
        self.assertTrue(all(c['paused'] for c in records.read_contacts(self.state) if records.source_of(c)))
        self.assertFalse(self.contact()['paused'])
        self.assertIn('群「项目群」的成员：3 人已暂停代答。', self.text())
        self.assertIn(f"{tag('3 人', 'violet')} {tag('已暂停 3', 'neutral')}", self.text())  # Nobody 运行中.
        self.assertEqual([label for label, _, _ in self.buttons(panel.SOURCE_RESUME)], ['恢复全部'])
        self.assertEqual(self.buttons(panel.SOURCE_PAUSE), [])
        self.click(panel.SOURCE_RESUME, source='oc_team')
        self.assertIn('3 人已恢复代答', self.text())
        # 同步成员: only who is not on the whitelist yet is offered.
        team = [('ou_m0', '成员0'), ('ou_m1', '成员1'), ('ou_m2', '成员2'), ('ou_new', '新人')]
        with self.search(members=self.members(team)):
            self.click(panel.MEMBERS, group='oc_team', name='项目群')
        self.assertEqual(self.record()['view']['members']['add'], [{'open_id': 'ou_new', 'name': '新人'}])
        self.click(panel.MEMBERS_ADD, pending=self.record()['view']['members']['id'])
        self.assertIn(f"**群「项目群」的成员** {tag('4 人', 'violet')}", self.text())
        self.assertEqual(self.click(panel.SOURCE_REMOVE, source='oc_team'), 'proxy_panel')
        self.assertEqual([c['key'] for c in records.read_contacts(self.state)], ['p2p:ou_zhang'])
        self.assertIn('已把群「项目群」的 4 位成员移出白名单', self.text())
        self.click(panel.SOURCE_PAUSE, source='oc_team')
        self.assertIn(GONE, self.text())

    def test_three_hundred_on_the_whitelist_fit_the_card_and_turn_pages(self):
        for index in range(250):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_{index:03}', 'target': f'ou_{index:03}',
                                               'name': f'很长很长的名字{index:03}', 'added_at': 10 + index})
        for group in range(5):
            for index in range(10):
                records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_g{group}{index}', 'target': f'ou_g{group}{index}',
                                                   'name': f'成员{index}', 'added_at': 300 + group * 10 + index,
                                                   'source': {'chat_id': f'oc_g{group}', 'name': f'一个名字很长的项目群{group}'}})
        self.open()
        names = '、'.join(f'同名的人{i}' for i in range(records.NAMES))
        people = {f'同名的人{i}': [user(f'ou_s{i}{j}', f'同名的人{i}', department='一个很长的部门名称', email=f'same{i}{j}@corp.com')
                                for j in range(6)] for i in range(records.NAMES)}
        with self.search(people):
            self.click(panel.FIND, query=names + '、多出来的')
        team = [(f'ou_t{i}', f'群成员{i}') for i in range(300)]
        with self.search(members=self.members(team)):
            self.click(panel.MEMBERS, group='oc_team', name='三百人的群')
        card = self.shown()
        self.assertLessEqual(element_count(card), 200)  # CardKit's limit (300305).
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 30000)
        self.assertIn('确认添加 300 人', self.text())
        self.assertIn('候选太多，卡片放不下全部', self.text())
        self.assertIn('后面 1 个没有查', self.text())
        self.assertRegex(self.text(), r'第 1/\d+ 页')
        pages = int(re.search(r'第 1/(\d+) 页', self.text()).group(1))
        self.assertGreater(pages, 10)
        self.assertEqual([(label, disabled) for label, _, disabled in self.buttons(panel.PAGE)], [('上一页', True), ('下一页', False)])
        self.assertIn('很长很长的名字000', self.text())
        self.click(panel.PAGE, page=pages - 1)
        self.assertIn(f'第 {pages}/{pages} 页', self.text())
        self.assertIn(f"**群「一个名字很长的项目群4」的成员** {tag('10 人', 'violet')}", self.text())
        self.assertNotIn('很长很长的名字000', self.text())
        self.assertLessEqual(element_count(self.shown()), 200)
        self.click(panel.PAGE, page=pages + 5)  # Past the end: the last page.
        self.assertIn(f'第 {pages}/{pages} 页', self.text())
        # A model menu far larger than the live one (34 on 2026-10-27; each option is a node): still fits.
        menu = {f'codex:m{i}': {'label': f'Codex · 一个很长的模型名字 {i}', 'harness': 'codex', 'model': f'm{i}',
                                'levels': ['low', 'medium', 'high', 'xhigh', 'max']} for i in range(70)}
        for page in (0, pages - 1):
            card = panel.proxy_panel_card(self.token(), {**self.record()['view'], 'page': page},
                                          settings=self.settings(), contacts=records.read_contacts(self.state), batches=[],
                                          choices=menu, current=('codex:m0', 'high'), busy='⏳ 正在查找…')
            self.assertLessEqual(element_count(card), 200)
            self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 30000)
            self.assertIn('确认添加 300 人', json.dumps(card, ensure_ascii=False))

    def live_sized(self, names, matches=21):
        """A card the size of the owner's on 2026-10-27: 8 rows on the whitelist, a 34-model menu, and a
        search of ``names`` each with ``matches`` people of that name (CHOICES_EACH of them listed)."""
        for index in range(7):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_w{index}', 'target': f'ou_w{index}',
                                               'name': f'白名单里的人{index}', 'added_at': 10 + index})
        menu = {f'codex:m{i}': {'label': f'Codex Acme · 模型名字 {i}', 'harness': 'codex', 'model': f'm{i}',
                                'levels': ['low', 'medium', 'high', 'xhigh']} for i in range(34)}
        items = [{'query': name, 'state': 'choices', 'more': matches - 4, 'candidates': [
            {'kind': 'p2p', 'target': f'ou_{name}{j}', 'key': f'p2p:ou_{name}{j}', 'name': f'{name}{j}',
             'chat_id': '', 'detail': '数据组-平台-研究', 'email': f'{name}{j}@example.com'}
            for j in range(4)]} for name in names]
        view = {'revision': 3, 'lookup': {'id': 'look', 'text': ' '.join(names), 'items': items, 'left': 0}}
        return panel.proxy_panel_card('t' * 24, view, settings=self.settings(), contacts=records.read_contacts(self.state),
                                      batches=[], choices=menu, current=('codex:m0', 'high'))

    def test_when_not_every_candidate_fits_each_name_still_gets_one_or_two(self):
        # 2026-10-27 16:25: “zengduju” had 21 matches and the card listed none of them, only “候选太多”.
        names = ['jinyimu', 'zengduju', 'lisi', 'wangwu', 'zhaoliu']
        card = self.live_sized(names)
        self.assertLessEqual(element_count(card), 200)
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 30000)
        listed = Counter(re.sub(r'\d$', '', value['candidate']) for kind, _, value, _ in controls(card) if kind == panel.ADD)
        self.assertEqual(set(listed), {f'p2p:ou_{name}' for name in names})
        self.assertTrue(all(count >= panel.FIRST_CHOICES for count in listed.values()), listed)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('第 1/', text)  # The whitelist gave way: it turns pages while the search is on the card.
        for name in names:  # Each heading counts what it does not list.
            hidden = 21 - listed[f'p2p:ou_{name}']
            self.assertIn(f'“{name}”的匹配（另有 {hidden} 个没列出，请写得更具体或用 ID）：', text)
        self.assertIn('候选太多，卡片放不下全部', text)

    def test_result_buttons_are_as_wide_as_their_short_labels(self):
        # At about 420 px the weighted columns cut them: “已在白…”, “群里 …”, “加入全体成员…” (UI review P12).
        records.write_contact(self.state, dict(GROUP))
        candidates = [{'kind': 'p2p', 'target': 'ou_zhang', 'key': 'p2p:ou_zhang', 'name': '张三', 'chat_id': '',
                       'detail': '数据', 'email': ''},
                      {'kind': 'group', 'target': 'oc_g', 'key': 'group:oc_g', 'name': '项目群', 'chat_id': 'oc_g',
                       'detail': '群', 'email': '', 'members': 12},
                      {'kind': 'group', 'target': 'oc_h', 'key': 'group:oc_h', 'name': '另一个群', 'chat_id': 'oc_h',
                       'detail': '群', 'email': ''}]
        view = {'revision': 2, 'lookup': {'id': 'look', 'text': 'x', 'left': 0, 'items': [
            {'query': 'x', 'state': 'choices', 'candidates': candidates}]}}
        card = panel.proxy_panel_card('t' * 24, view, settings=self.settings(), contacts=records.read_contacts(self.state),
                                      batches=[], choices={}, current=None)
        shown = [(label, element['width']) for kind, label, _, element in controls(card) if kind in (panel.ADD, panel.MEMBERS)]
        self.assertEqual(shown, [('已添加', 'default'), ('已添加', 'default'), ('全员 12 人', 'default'),
                                 ('群里@我', 'default'), ('全员', 'default')])
        rows = [e for e in walk(card) if e.get('tag') == 'column_set'
                and any(c.get('kind') in (panel.ADD, panel.MEMBERS) for c in walk(e) if isinstance(c, dict))]
        for row in rows:
            self.assertEqual([c['width'] for c in row['columns']][0], 'weighted')
            self.assertEqual({c['width'] for c in row['columns'][1:]}, {'auto'})

    def test_a_whitelist_over_five_rows_folds_and_opens_after_a_click_on_a_row(self):
        opened = self.open().args[2]
        self.assertEqual(whitelist_folds(opened), [])  # One row: as before.
        self.assertIn('**白名单（1）**', json.dumps(opened, ensure_ascii=False))
        for index in range(5):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_w{index}', 'target': f'ou_w{index}',
                                               'name': f'人{index}', 'added_at': 10 + index, 'paused': index == 0})

        def folded():
            panels = whitelist_folds(self.shown())
            self.assertEqual(len(panels), 1)
            return panels[0]
        self.click(panel.REFRESH)
        self.assertEqual((folded()['expanded'], folded()['header']['title']['content']),
                         (False, '全部 6 个 · 已暂停 1 · 点开管理'))
        self.assertIn(f"**人4** {tag('私聊', 'wathet')} {tag('运行中', 'green')}", json.dumps(folded(), ensure_ascii=False))
        self.assertIn('**白名单（6）**', self.text())  # The heading stays above the folded rows.
        self.click(panel.PAUSE, contact='p2p:ou_w1')  # A click on a row: drawn open, the row in sight.
        self.assertTrue(folded()['expanded'])
        self.click(panel.REFRESH)
        self.assertTrue(folded()['expanded'])
        self.click(panel.DISABLE)  # Anything else: folded again.
        self.assertFalse(folded()['expanded'])

    def test_pause_resume_and_remove_act_on_their_row(self):
        self.open()
        self.click(panel.PAUSE, contact='p2p:ou_zhang')
        self.assertTrue(self.contact()['paused'])
        self.assertIn(f"**张三** {tag('私聊', 'wathet')} {tag('已暂停', 'neutral')}", self.text())
        self.assertEqual([label for kind, label, *_ in controls(self.shown()) if kind == panel.RESUME], ['恢复'])
        self.click(panel.RESUME, contact='p2p:ou_zhang')
        self.assertFalse(self.contact()['paused'])
        self.assertGreater(self.contact()['since'], 0)
        self.click(panel.REMOVE, contact='p2p:ou_zhang')
        self.assertIsNone(self.contact())
        self.assertIn(panel.EMPTY, self.text())
        self.click(panel.PAUSE, contact='p2p:ou_zhang')
        self.assertIn(GONE, self.text())

    def test_login_replies_under_the_card(self):
        self.open()
        started = threading.Event()

        def login(ctx, settings, chat_id, message_id, key):
            started.args = chat_id, message_id, key
            self.proxy.web_lock.release()
            started.set()
        with mock.patch.object(self.proxy.web_login, 'login', side_effect=login):
            self.assertEqual(self.click(panel.LOGIN), 'proxy_panel')
            self.assertTrue(started.wait(5))
        self.assertEqual(started.args[:2], ('chat', 'om_panel'))
        self.assertIn('登录二维码，马上发到这里', self.text())

    def test_refresh_shows_what_changed_elsewhere_from_any_revision(self):
        self.open()
        self.click(panel.DISABLE)
        records.write_contact(self.state, dict(GROUP))
        self.assertEqual(self.click(panel.REFRESH, revision=1), 'proxy_panel')
        self.assertIn('白名单 2 个', self.text())
        self.assertNotIn('代答已关闭', self.text())  # 刷新 clears the last notice.

    def test_a_click_from_before_the_last_redraw_acts_while_it_still_applies(self):
        self.open()
        self.click(panel.PAUSE, contact='p2p:ou_zhang')
        # Drawn at revision 1: the contact is still there, so it is removed.
        self.assertEqual(self.click(panel.REMOVE, revision=1, contact='p2p:ou_zhang'), 'proxy_panel')
        self.assertIsNone(self.contact())
        self.assertEqual(self.click(panel.PAUSE, revision=1, contact='p2p:ou_zhang'), 'proxy_stale')
        self.assertIn(STALE + GONE, self.text())
        self.assertEqual(self.click(panel.DISABLE, revision=1), 'proxy_panel')
        self.assertEqual(self.click(panel.DISABLE, revision=1), 'proxy_stale')  # Off already.
        self.assertIn(STALE + '代答已经是关闭的。', self.text())
        self.assertEqual(self.click(panel.ENABLE, revision=1), 'proxy_panel')  # Still applies: it is off.
        # A search from an older revision runs; 添加 and 全部添加 of a result a newer search replaced do not.
        with self.search({'李四': [user('ou_li', '李四')]}):
            self.click(panel.FIND, query='李四', revision=1)
        first = self.lookup_id()
        with self.search({'王五': [user('ou_w', '王五')]}):
            self.click(panel.FIND, query='王五', revision=2)
        self.assertEqual(self.click(panel.ADD_ALL, lookup=first, revision=3), 'proxy_stale')
        self.assertEqual(self.click(panel.ADD, candidate='p2p:ou_li', lookup=first, revision=3), 'proxy_stale')
        self.assertIn(STALE + '这个查找结果已被新的查找替换', self.text())
        self.assertIsNone(self.contact('p2p:ou_li'))
        self.assertEqual(self.click(panel.ADD_ALL, lookup=self.lookup_id(), revision=3), 'proxy_panel')
        self.assertIsNotNone(self.contact('p2p:ou_w'))

    def test_a_card_drawn_by_the_release_before_still_adds_its_candidates(self):
        self.open()
        path = records.proxy_dir(self.state) / 'panels' / f'{self.token()}.json'
        record = self.record()
        record['view'] = {'revision': 2, 'query': '李四', 'busy': '', 'found': {'query': '李四', 'candidates': [
            {'kind': 'p2p', 'target': 'ou_li', 'name': '李四', 'chat_id': '', 'detail': '数据', 'email': '',
             'key': 'p2p:ou_li'}]}}
        path.write_text(json.dumps(record, ensure_ascii=False))
        self.assertEqual(self.click(panel.ADD, index=0, revision=2), 'proxy_panel')
        self.assertEqual(self.contact('p2p:ou_li')['name'], '李四')
        self.assertEqual(self.buttons(panel.ADD), [])  # Added: the result left the card.
        self.assertIsNone(self.record()['view']['found'])

    def test_an_older_panel_is_expired_on_its_own_card(self):
        self.open('ev-old')
        old = self.token()
        self.app.messenger.create_and_send_card.return_value = ('pcid2', 'om_panel2')
        self.open('ev-new')
        self.assertNotEqual(self.token(), old)
        self.assertEqual(self.click(panel.DISABLE, token=old), 'proxy_expired')
        self.assertTrue(records.read_settings(self.state)['enabled'])
        self.assertEqual(self.puts()[-1].path, '/open-apis/cardkit/v1/cards/pcid')
        self.assertIn('这张卡已过期', self.text())
        self.assertEqual(controls(self.shown()), [])
        self.assertEqual(self.record(old)['status'], 'expired')
        self.assertEqual(self.click(panel.REFRESH, token=old), 'proxy_expired')
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(self.click(panel.DISABLE, message='om_panel2'), 'proxy_panel')

    def test_only_the_owner_acts_and_a_refusal_is_written_on_the_card(self):
        self.open()
        self.assertEqual(self.click(panel.DISABLE, operator='stranger'), 'not_owner')  # Access.allows: not served.
        self.assertEqual(self.puts(), [])
        self.assertEqual(self.proxy.panel.click(self.ctx, self.click_event(panel.DISABLE), self.click_event(panel.DISABLE)['action_value'],
                                                False), 'owner_only')
        self.assertTrue(records.read_settings(self.state)['enabled'])
        self.assertIn(OWNER_ONLY, self.text())
        self.assertEqual(self.click(panel.DISABLE, message='om_other'), 'proxy_unknown')
        self.assertEqual(self.click(panel.DISABLE, token='f' * 24, revision=1), 'proxy_unknown')

    def test_a_change_the_card_could_not_show_is_said_under_it(self):
        self.open()
        self.lark.api.return_value = None
        self.click(panel.PAUSE, contact='p2p:ou_zhang')
        self.assertTrue(self.contact()['paused'])
        text = self.app.messenger.send_message.call_args
        self.assertIn('代答卡未能刷新', text.args[2])
        self.assertEqual(text.kwargs['reply_to_message_id'], 'om_panel')
        self.lark.api.return_value = {'code': 0}
        # The card still shows revision 1: its next click acts on what is there now and redraws.
        self.assertEqual(self.click(panel.RESUME, revision=1, contact='p2p:ou_zhang'), 'proxy_panel')
        self.assertFalse(self.contact()['paused'])
        self.assertIn('已恢复代答', self.text())

    def test_slow_clicks_return_before_any_lookup_or_card_write(self):
        self.open()
        seen = []

        def search(profile, *args, **kwargs):
            seen.append((len(self.puts()), any(l.startswith('card action kind=bridge_proxy_find') for l in self.logs)))
            return lark()(profile, *args, **kwargs)
        stop = threading.Event()
        self.addCleanup(stop.set)
        lanes = CardLanes(self.app.handle_card_action_event, self.app.card_lane_key, self.app.runtime.log, stop)
        with owner_cli(side_effect=search):
            lanes.run(self.click_event(panel.FIND, query='张三'), self.ctx)
        self.assertTrue(seen and all(acknowledged for _, acknowledged in seen))
        [line] = [l for l in self.logs if l.startswith('card action kind=bridge_proxy_find')]
        self.assertLess(int(line.rsplit('elapsed_ms=', 1)[1]), 500)
        self.assertIn('正在查找“张三”', json.dumps(self.puts()[0].card, ensure_ascii=False))
        self.assertIn(f"**“张三”** {tag('没找到', 'neutral')}", self.text())
        self.assertEqual([call.data['sequence'] for call in self.puts()], [1, 2])

    def test_the_kinds_are_accepted_card_actions_that_start_no_task(self):
        self.assertLessEqual(panel.KINDS, set(ACTIONS))
        self.assertFalse(panel.KINDS & BOT_ONLY)


class WebCacheTests(ProxyCase):
    """Repo review E6: the web login (the owner's only kept one) loses only its caches, past their cap, and only
    while no batch is sending and no browser has it; web_lock is held throughout, so no send or login starts."""

    def setUp(self):
        super().setUp()
        self.profile = self.root / 'web' / 'profile'
        self.cache = self.profile / 'Default' / 'Cache' / 'Cache_Data' / 'f1'
        for path in (self.profile / 'Default' / 'Cookies', self.profile / 'Default' / 'IndexedDB' / 'a.ldb', self.cache):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x' * 100)
        for patch in (mock.patch('covey_internal.proxy_login.WEB_CACHE_BYTES', 100),
                      mock.patch('covey_internal.proxy_login.browser_on', return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.logs = []
        self.app.runtime.log = self.logs.append

    def test_the_caches_go_with_the_browser_held_and_the_login_stays(self):
        held = []
        real = __import__('covey.core.web_profile', fromlist=['x']).prune_web_caches
        with mock.patch('covey_internal.proxy_login.prune_web_caches',
                        side_effect=lambda *a: held.append(self.proxy.web_lock.locked()) or real(*a)):
            self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertEqual(held, [True])
        self.assertFalse(self.cache.parent.parent.exists())
        self.assertTrue((self.profile / 'Default' / 'Cookies').exists() and (self.profile / 'Default' / 'IndexedDB').exists())
        self.assertFalse(self.proxy.web_lock.locked())
        self.assertEqual(self.logs, ['proxy web login caches removed: 0 MiB; the login and the rest stay'])

    def test_nothing_goes_while_a_batch_is_sending_or_the_browser_is_in_use_or_below_the_cap(self):
        records.write_batch(self.state, {'id': 'b1', 'status': 'sending', 'created_at': 1, 'history': []})
        self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertTrue(self.cache.exists())
        records.write_batch(self.state, {'id': 'b1', 'status': 'sent', 'created_at': 1, 'history': []})
        with self.proxy.web_lock:  # A send or a login has the browser.
            self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertTrue(self.cache.exists())
        with mock.patch('covey_internal.proxy_login.browser_on', return_value=True):
            self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertTrue(self.cache.exists())
        with mock.patch('covey_internal.proxy_login.WEB_CACHE_BYTES', 101):
            self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.logs, [])
        self.assertFalse(self.proxy.web_lock.locked())
        self.proxy.web_login.prune_web_caches(self.ctx)
        self.assertFalse(self.cache.exists())

    def test_the_bridges_maintenance_bounds_them_each_hour(self):
        [target] = [target for name, target in self.app.worker_targets() if name == 'maintenance']
        with mock.patch('covey.core.maintenance.maintenance_loop') as loop, \
                mock.patch.object(self.proxy.web_login, 'prune_web_caches') as prune:
            target(self.ctx)
            loop.call_args.kwargs['prune_web']()
        prune.assert_called_once_with(self.ctx)


class ResendTests(ProxyCase):
    """A reply that never went out gets a 重新发送 card in its topic; the button sends the saved reply
    again on the next poll, and the card shows what came of it (the owner, 2026-10-27)."""

    FAILURE = {'ok': False, 'step': 'check', 'submitted': False, 'error': '会话最新一条消息的位置对不上：网页端 57，接口 62'}

    def failed(self):
        """A batch whose sends never pressed Enter on any try, its card out."""
        self.submitted()
        for attempt in range(records.SEND_ATTEMPTS):
            self.run_tick('收到 哥', self.FAILURE)
        [batch] = self.batches()
        self.assertEqual((batch['status'], batch['resend_card']), ('held', {'card_id': 'rcid', 'message_id': 'om_resend'}))
        return batch

    def event(self, batch, *, operator='owner', message='om_resend', reply=None, kind=None):
        from covey.cards.proxy_resend import RESEND, reply_digest
        return {'operator_id': operator, 'chat_id': 'oc_proxy', 'message_id': message, 'event_id': 'ev-resend',
                'action_value': {'kind': kind or RESEND, 'batch': batch['id'],
                                 'reply': reply or reply_digest(batch['reply'])}}

    DECLINE = '[不回复]\n他问能不能批 3 万预算，这要你定。\n建议回复：“我看下预算再回你”'

    def declined(self):
        """A batch whose turn would not answer for the owner and suggested a reply."""
        batch = self.submitted()
        self.run_tick(self.DECLINE, {'ok': True}).assert_not_called()
        [held] = self.batches()
        self.assertEqual((held['status'], held['reply'], held['reply_job_id']), ('held', '我看下预算再回你', batch['job_id']))
        return held

    def test_a_declined_round_says_it_once_and_offers_its_suggestion(self):
        # Repo review U5 (T186, 10-28 09:45): the reason and the suggested reply came 3–4 times a round, and to send
        # it the owner had to @ the bot and type it.
        from covey.cards.proxy_resend import SUGGEST
        batch = self.declined()
        self.assertFalse(any('没有代你回复' in note for note in self.notes()))  # The card, not a line of text.
        [create] = [call for call in self.lark.calls if call.method == 'POST']
        text = json.dumps(create.created, ensure_ascii=False)
        for said in ('⏸ 没有代你回复', f"原因和建议见上面 #{batch['reply_job_id']} 的回答", '建议回复：\\n我看下预算再回你'):
            self.assertIn(said, text)
        self.assertNotIn('这要你定', text)  # The reason stays in the answer above.
        [(kind, label, _, button)] = controls(create.created)
        self.assertEqual((kind, label, button.get('disabled')), (SUGGEST, '按建议回复', False))
        self.assertIn(SUGGEST, ACTIONS)
        self.assertNotIn(SUGGEST, BOT_ONLY)
        self.run_tick(self.DECLINE, {'ok': True, 'submitted': True}).assert_not_called()  # Never sent by itself.
        self.assertEqual(self.now(batch)['status'], 'held')

    def test_the_owners_click_sends_the_suggestion_as_written_and_the_card_says_what_came_of_it(self):
        from covey.cards.proxy_resend import SUGGEST
        batch = self.declined()
        self.assertEqual(self.click(batch, kind=SUGGEST), 'proxy_resend')
        self.assertIn('⏳ 正在发送…', self.shown())
        self.assertEqual(self.buttons(), [('按建议回复', True)])
        self.assertIn(f"proxy resend requested batch={batch['id'][:8]} (the suggested reply)", self.logs)
        sent = self.run_tick('模型没有再跑', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_args.args[4], '我看下预算再回你')  # The existing send: page, check, read-back.
        self.assertEqual(self.now(batch)['status'], 'sent')
        self.assertIn('✅ 已发出', self.shown())
        self.assertEqual(self.buttons(), [])

    def test_a_suggestion_only_the_owner_sends_and_one_overtaken_is_void(self):
        from covey.app.proxy_resend import SUGGEST_OWNER_ONLY
        from covey.cards.proxy_resend import SUGGEST
        batch = self.declined()
        event = self.event(batch, operator='member', kind=SUGGEST)
        self.assertEqual(self.proxy.resend.click(self.ctx, event, event['action_value'], False), 'owner_only')
        self.assertIn(SUGGEST_OWNER_ONLY, self.shown())
        self.assertIsNone(self.now(batch).get('resend_requested'))
        self.proxy.commands.set_paused(self.ctx, '张三', True)
        self.assertEqual(self.click(batch, kind=SUGGEST), 'proxy_resend_refused')
        self.assertIn('⚠️ 没有发出：已暂停对 张三 的代答。恢复代答后再点“按建议回复”。', self.shown())
        self.assertEqual(self.buttons(), [('按建议回复', False)])
        self.proxy.commands.set_paused(self.ctx, '张三', False)
        self.take_in(item('m2', '算了不用了', position='9'))  # Newer messages: this suggestion is void.
        self.assertEqual(self.now(batch)['status'], 'superseded')
        self.assertIn('对方有新消息，这条已作废', self.shown())
        self.assertEqual(self.click(batch, kind=SUGGEST), 'proxy_stale')

    def click(self, batch, **options):
        return self.app._handle_card_action_event(self.event(batch, **options), self.ctx)

    def now(self, batch):
        return records.read_batch(self.state, batch['id'])

    def puts(self):
        return [call for call in self.lark.calls if call.method == 'PUT']

    def shown(self):
        return json.dumps(self.puts()[-1].card, ensure_ascii=False)

    def buttons(self):
        return [(label, element.get('disabled')) for _, label, _, element in controls(self.puts()[-1].card)]

    def test_the_card_is_the_bridges_own_accepted_kind_that_starts_no_task(self):
        from covey.cards.proxy_resend import RESEND
        self.assertIn(RESEND, ACTIONS)
        self.assertNotIn(RESEND, BOT_ONLY)

    def test_the_card_says_why_in_words_once_and_folds_the_positions(self):
        from covey.cards.proxy_resend import resend_card
        batch = {'id': 'b' * 32, 'reply': '嗯嗯', 'reason': '2 个名为 钱七 的会话都对不上：会话最新一条消息的位置对不上：'
                 '网页端 14708，接口 14709；会话最新一条消息的位置对不上：网页端 14708，接口 14709'}
        card = resend_card({'state': 'failed'}, batch, '钱七')
        first = card['body']['elements'][0]
        self.assertEqual(first['content'], '**⚠️ 代答没有发出**\n没发出：网页端的「钱七」会话和接口对不上（试了 2 个同名会话、3 次），钱七没收到。')
        [folded] = [e for e in walk(card) if e.get('tag') == 'collapsible_panel']
        self.assertEqual((folded['expanded'], folded['header']['title']['content']), (False, '详情'))
        self.assertEqual(json.dumps(card, ensure_ascii=False).count('14708'), 1)  # Only in the details.
        self.assertIn('网页端 14708，接口 14709（2 个会话都是）', json.dumps(folded, ensure_ascii=False))
        # A group: the chat is the group, the reply is for the person who @-ed the owner.
        again = resend_card({'state': 'again'}, {**batch, 'reason': '会话里找不到原消息（位置 9）'}, '张三', '项目群')
        self.assertIn('还是没发出：网页端的「项目群」会话里找不到要回的那条消息（又试了 3 次），张三没收到。', json.dumps(again, ensure_ascii=False))
        plain = resend_card({'state': 'failed'}, {**batch, 'reason': '网页端未登录'}, '张三')
        self.assertEqual([e for e in walk(plain) if e.get('tag') == 'collapsible_panel'], [])  # Nothing more to say.

    def test_a_click_sends_the_saved_reply_on_the_next_poll_also_past_the_late_limit(self):
        batch = self.failed()
        # Three hours on, a batch's own reply no longer goes out by itself; the owner asked for it.
        late = time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - records.STALE_AFTER - 3600))
        records.write_batch(self.state, {**batch, 'messages': [{**batch['messages'][0], 'time': late}]})
        self.assertEqual(self.click(batch), 'proxy_resend')  # Before topic routing: never a task runner's.
        marked = self.now(batch)
        self.assertEqual((marked['attempts'], marked['send_failed'], marked['resend_by']), (0, True, 'owner'))
        self.assertIn('正在重新发送', self.shown())
        self.assertEqual(self.buttons(), [('重新发送', True)])
        before = len(self.notes())
        sent = self.run_tick('模型没有再跑', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_args.args[4], '收到 哥')  # The saved reply, as it is.
        self.assertEqual((self.now(batch)['status'], self.now(batch)['sent_message_id']), ('sent', 'om_sent'))
        self.assertIn('✅ 已发出', self.shown())
        self.assertIn('收到 哥', self.shown())
        self.assertEqual(self.buttons(), [])
        self.assertEqual(len(self.notes()), before)  # Said on the card the owner clicked.
        # Each write of the card: a sequence of its own from reserve, and a key made from it.
        self.assertEqual([call.data['sequence'] for call in self.puts()], [1, 2])
        self.assertEqual(len({call.data['uuid'] for call in self.puts()}), 2)
        self.assertEqual(json.loads((records.proxy_dir(self.state) / 'resend' / f"{batch['id']}.json").read_text())['sequence'], 2)
        self.assertIn('proxy resend requested batch=' + batch['id'][:8], self.logs)

    def test_a_resend_that_fails_again_says_so_and_gives_the_button_back(self):
        batch = self.failed()
        self.click(batch)
        for attempt in range(records.SEND_ATTEMPTS + 1):
            sent = self.run_tick('收到 哥', self.FAILURE)
        sent.assert_not_called()  # Its own SEND_ATTEMPTS, then it waits for the button again.
        again = self.now(batch)
        self.assertEqual((again['status'], again['resend_requested'], again['attempts']), ('held', None, 3))
        self.assertIn('还是没发出', self.shown())
        self.assertIn('网页端 57，接口 62', self.shown())
        self.assertEqual(self.buttons(), [('重新发送', False)])
        self.assertFalse(any('代答没有发出' in note for note in self.notes()))
        self.assertEqual(self.click(batch), 'proxy_resend')

    def test_a_resend_that_may_have_gone_out_shows_no_button_and_is_never_sent_again(self):
        batch = self.failed()
        self.click(batch)
        self.run_tick('收到 哥', {'ok': False, 'submitted': True, 'error': '网页端发送超时'}, read_back={})
        self.assertEqual(self.now(batch)['status'], 'uncertain')
        self.assertIn('可能已发出，没能确认', self.shown())
        self.assertIn('网页端发送超时', self.shown())
        self.assertEqual(self.buttons(), [])
        self.assertEqual(self.click(batch), 'proxy_stale')
        self.run_tick('收到 哥', {'ok': True, 'submitted': True}).assert_not_called()

    def test_a_click_that_may_not_send_says_why_on_the_card_and_changes_nothing(self):
        from covey.app.proxy_resend import CHANGED, OWNER_ONLY
        batch = self.failed()
        before = self.now(batch)
        self.assertEqual(self.click(batch, operator='stranger'), 'not_owner')  # Access.allows: not served.
        self.assertEqual(self.puts(), [])
        event = self.event(batch, operator='member')
        self.assertEqual(self.proxy.resend.click(self.ctx, event, event['action_value'], False), 'owner_only')
        self.assertIn(OWNER_ONLY, self.shown())
        self.proxy.commands.set_paused(self.ctx, '张三', True)
        self.assertEqual(self.click(batch), 'proxy_resend_refused')
        self.assertIn('⚠️ 没有重新发送：已暂停对 张三 的代答。恢复代答后再点“重新发送”。', self.shown())
        self.assertEqual(self.buttons(), [('重新发送', False)])
        self.proxy.commands.set_paused(self.ctx, '张三', False)
        self.proxy.commands.cmd_disable(self.ctx, '', '', '')
        self.assertEqual(self.click(batch), 'proxy_resend_refused')
        self.assertIn('没有重新发送：代答已关闭。开启代答后再点', self.shown())
        records.write_settings(self.state, {**records.read_settings(self.state), 'enabled': True})
        for i in range(records.HOURLY_LIMIT):
            records.write_batch(self.state, {'id': f'{i:032x}', 'contact': PERSON['key'], 'status': 'sent', 'created_at': 0,
                                             'send_started_at': time.time() - 60, 'messages': []})
        self.assertEqual(self.click(batch), 'proxy_resend_refused')
        self.assertIn(f'一小时内已给对方代答 {records.HOURLY_LIMIT} 条', self.shown())
        for i in range(records.HOURLY_LIMIT):
            records.batch_path(self.state, f'{i:032x}').unlink()
        self.assertEqual(self.click(batch, reply='0' * 12), 'proxy_stale')  # Drawn with another reply.
        self.assertIn(CHANGED, self.shown())
        self.assertEqual(self.click(batch, message='om_other'), 'proxy_unknown')
        self.assertEqual(self.click({'id': 'not-a-batch', 'reply': ''}), 'proxy_unknown')
        self.assertEqual(self.now(batch), before)
        self.run_tick('收到 哥', {'ok': True, 'submitted': True}).assert_not_called()

    def test_a_click_after_newer_messages_says_this_one_is_void(self):
        batch = self.failed()
        self.take_in(item('m9', '算了，我问别人', position='9'))
        self.assertEqual(self.now(batch)['status'], 'superseded')
        self.assertEqual(self.click(batch), 'proxy_stale')
        self.assertIn('对方有新消息，这条已作废', self.shown())
        self.assertEqual(self.buttons(), [])

    def test_a_batch_that_ends_another_way_after_the_click_redraws_its_card(self):
        # Repo review D7: the card said 正在重新发送… with its button off for good once the batch ended before
        # the next poll sent it.
        for case in ('newer messages', 'removed', 'nothing to say back'):
            with self.subTest(case=case):
                for path in records.proxy_dir(self.state).glob('*/*.json'):
                    path.unlink()
                records.write_contact(self.state, dict(PERSON))
                self.cards = iter(['om_root', 'om_mirror'])
                batch = self.failed()
                self.assertEqual(self.click(batch), 'proxy_resend')
                self.assertIn('正在重新发送', self.shown())
                if case == 'newer messages':
                    self.take_in(item('m9', '算了，我问别人', position='9'))  # The next poll's take-in.
                    said, status = '对方有新消息，这条已作废', 'superseded'
                elif case == 'removed':
                    self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
                    self.run_tick('好的', {'ok': True, 'submitted': True}).assert_not_called()
                    said, status = '联系人已移出白名单，不再发', 'skipped'
                else:  # The owner's turn there: nothing to say back.
                    records.write_batch(self.state, {**self.now(batch), 'resend_requested': None, 'send_failed': False})
                    self.owner_turn(batch)
                    self.run_tick('[无需回复]\n他说不用了', {'ok': True}).assert_not_called()
                    said, status = '不用回：他说不用了，不再发', 'skipped'
                self.assertEqual(self.now(batch)['status'], status)
                self.assertNotIn('正在重新发送', self.shown())
                self.assertIn(said, self.shown())
                self.assertEqual(self.buttons(), [])

    def test_a_click_for_a_removed_contact_sends_nothing(self):
        batch = self.failed()
        self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
        self.assertEqual(self.click(batch), 'proxy_resend_refused')
        self.assertIn('联系人已移出白名单，不再发', self.shown())
        self.assertEqual(self.buttons(), [])
        self.assertFalse(self.now(batch).get('resend_requested'))

    def test_a_resend_stopped_before_the_poll_is_said_on_the_card_and_waits_for_another_click(self):
        batch = self.failed()
        self.click(batch)
        self.proxy.commands.cmd_disable(self.ctx, '', '', '')
        before = len(self.notes())
        for attempt in range(2):  # The next poll does not try it by itself either.
            self.run_tick('收到 哥', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertIn('⚠️ 没有重新发送：代答已关闭。开启代答后再点“重新发送”。', self.shown())
        self.assertEqual(self.buttons(), [('重新发送', False)])
        self.assertEqual(len(self.notes()), before)
        records.write_settings(self.state, {**records.read_settings(self.state), 'enabled': True})
        self.assertEqual(self.click(batch), 'proxy_resend')
        self.run_tick('收到 哥', {'ok': True, 'submitted': True}).assert_called_once()

    def test_a_turn_whose_reply_also_fails_redraws_the_card_and_says_so_below(self):
        batch = self.failed()
        self.owner_turn(batch)
        for attempt in range(records.SEND_ATTEMPTS):
            self.run_tick('明天给你', self.FAILURE)
        self.assertEqual(self.now(batch)['reply'], '明天给你')
        self.assertIn('明天给你', self.shown())
        self.assertEqual(self.buttons(), [('重新发送', False)])
        # Why is on the card, once; the line under it only says this turn's reply failed too (UI review P11).
        self.assertEqual(self.notes()[-1], '⚠️ 这一轮的回复也没发出，原因和要发的原文在上面的卡片上，可以点“重新发送”。')
        self.assertIn('没发出：网页端的「张三」会话和接口对不上（试了 3 次），张三没收到。', self.shown())
        self.assertEqual(len([call for call in self.lark.calls if call.method == 'POST']), 1)  # One card a batch.

    def test_without_the_card_a_line_says_it_and_the_next_poll_tries_the_card_again(self):
        self.lark.api.return_value = {'code': 0}  # Created without an id.
        self.submitted()
        for attempt in range(records.SEND_ATTEMPTS):
            self.run_tick('好的', self.FAILURE)
        self.assertIn('⚠️ 代答没有发出：网页端的「张三」会话和接口对不上（试了 3 次），张三没收到。\n', self.notes()[-1])
        self.assertNotIn('57', self.notes()[-1])  # Positions only in the card's folded details.
        self.assertIn('在本话题 @机器人 说“重新回复”', self.notes()[-1])
        self.lark.send.assert_not_called()
        self.lark.api.return_value = {'code': 0, 'data': {'card_id': 'rcid'}}
        self.run_tick('好的', self.FAILURE).assert_not_called()
        self.assertEqual(self.batches()[0]['resend_card'], {'card_id': 'rcid', 'message_id': 'om_resend'})

    def test_a_batch_an_older_release_held_gets_its_card_and_a_failed_one_is_left_as_it_is(self):
        # 赵六 8fb90f36 (2026-10-27): held after three sends that never went out, said in a line of text.
        batch = self.submitted()
        sending = records.move_batch(self.state, batch, 'sending', reply='收到 哥', reply_job_id=batch['job_id'],
                                     send_started_at=time.time(), attempts=3)
        records.move_batch(self.state, sending, 'held', reason='网页端未登录', send_failed=True)
        old = {**sending, 'id': 'f' * 32, 'status': 'failed', 'created_at': 1, 'send_failed': True}
        records.write_batch(self.state, old)
        for attempt in range(2):
            self.run_tick('收到 哥', self.FAILURE).assert_not_called()
        self.assertEqual(self.now(batch)['resend_card'], {'card_id': 'rcid', 'message_id': 'om_resend'})
        self.assertEqual(len([call for call in self.lark.calls if call.method == 'POST']), 1)
        self.assertIn('没发出：网页端未登录', json.dumps(self.lark.calls[0].created, ensure_ascii=False))
        self.assertEqual(records.read_batch(self.state, 'f' * 32), old)
        self.assertFalse((records.proxy_dir(self.state) / 'resend' / f"{'f' * 32}.json").exists())

    def test_a_poll_writes_over_the_batch_as_it_is_now_and_never_over_a_newer_status(self):
        batch = self.failed()
        read_by_the_poll = dict(batch)
        self.click(batch)  # Written after the poll read it.
        merged = self.proxy.update(self.ctx, read_by_the_poll, considered_job_id=99)
        self.assertEqual((merged['considered_job_id'], merged['resend_by']), (99, 'owner'))
        records.move_batch(self.state, self.now(batch), 'superseded')
        self.assertIsNone(self.proxy.update(self.ctx, read_by_the_poll, considered_job_id=100))
        self.assertEqual((self.now(batch)['status'], self.now(batch)['considered_job_id']), ('superseded', 99))

    def test_a_poll_that_holds_or_moves_its_older_copy_keeps_the_click_and_never_a_newer_status(self):
        # Hard rules 1 and 4: move_batch and stop's held write wrote the poll's copy whole, the click's
        # 重新发送 lost; and a move outside Proxy.lock could land between the click's read and its write.
        batch = self.failed()
        read_by_the_poll = dict(batch)
        self.click(batch)  # Written after the poll read it.
        self.proxy.stop(self.ctx, self.proxy.settings(self.ctx), read_by_the_poll, batch['reply_job_id'],
                        '代答已关闭', '开启代答后')
        held = self.now(batch)
        self.assertEqual((held['status'], held['reason'], held['resend_by']), ('held', '代答已关闭', 'owner'))
        with self.proxy.lock:  # A click reads and writes the batch under it: a move waits.
            moving = threading.Thread(target=records.move_batch, args=(self.state, read_by_the_poll, 'superseded'))
            moving.start()
            moving.join(0.2)
            self.assertTrue(moving.is_alive())
        moving.join()
        self.assertEqual((self.now(batch)['status'], self.now(batch)['resend_by']), ('superseded', 'owner'))
        self.assertIsNone(records.move_batch(self.state, read_by_the_poll, 'sending', reply='收到 哥'))
        self.assertEqual(self.now(batch)['status'], 'superseded')

    def test_a_click_while_a_poll_takes_an_owners_turn_is_kept_and_sent_next(self):
        from covey.app import proxy as worker
        batch = self.failed()
        self.owner_turn(batch)
        turns = worker.topic_job_records

        def clicking(*args):
            self.click(batch)  # While this poll, which read the batch before, looks at the topic's turns.
            return turns(*args)
        with mock.patch.object(worker, 'topic_job_records', side_effect=clicking):
            self.run_tick('[不回复]\n先别回', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertTrue(self.now(batch)['resend_requested'])
        sent = self.run_tick('[不回复]', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_args.args[4], '收到 哥')

    def test_a_resend_the_owner_answered_themselves_meanwhile_ends_on_the_card(self):
        batch = self.failed()
        self.click(batch)
        before = len(self.notes())
        self.run_tick('收到 哥', {'ok': True, 'submitted': True}, owner_replied=True).assert_not_called()
        self.assertEqual(self.now(batch)['status'], 'skipped')
        self.assertIn('你已亲自回复，不再发', self.shown())
        self.assertEqual(self.buttons(), [])
        self.assertEqual(len(self.notes()), before)


if __name__ == '__main__':
    unittest.main()

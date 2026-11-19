"""The 设置 card's 脱敏 tab (app/redaction_panel.py, cards/redaction_panel.py) and the list's edits it makes
(core/redaction.py): the four pages, each button's change of the list, refusals in words that name no term, the
web dashboard's part only where one is served, and no term on a card, in a card's record or in a log line."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.records import write_topic
from test_settings_hub import MEMBER, HubCase, values
from covey.app.redaction_panel import CASE_CLASH, GONE, UNREADABLE, card_key, row_key
from covey.cards import redaction_panel as cards
from covey.cards.elements import walk
from covey.cards.redaction_link import ROTATE_KIND
from covey.core.redaction import (Refused, auto_stand_in, config_path, load_config, masked, record_turn,
                                  row_problem, save_config, term_lines, update_config, upstream_problem)
from covey.core.state import write_json
from covey.core.text import literal

SECRET = ('机密项目甲乙', 'ProjectZeta', '张三丰')  # No card, record or log line may hold these.


def rows(*terms, **stand_ins):
    return [{'term': term, 'stand_in': stand_ins.get(f't{i}', ''), 'anywhere': False} for i, term in enumerate(terms)]


class ListEditTests(unittest.TestCase):
    """core/redaction.py: how a card names a term, the 加入词表 box's lines, why a row cannot join the list, and the
    list changed one change at a time."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_a_term_is_named_by_its_first_character_and_length(self):
        self.assertEqual(masked('机密项目甲乙'), '机··（6 字）')
        self.assertEqual(masked('ab'), 'a··（2 字）')

    def test_each_non_empty_line_is_a_term_with_its_number(self):
        self.assertEqual(term_lines('甲乙丙\n\n  丁戊 \n'), [(1, '甲乙丙'), (3, '丁戊')])
        self.assertEqual(term_lines(None), [])

    def test_a_new_stand_in_or_anywhere_is_refused_and_an_old_one_kept(self):
        save_config(self.state, {'terms': [{'term': '旧词旧', 'stand_in': '代号甲', 'anywhere': True},
                                           {'term': 'abc词', 'stand_in': '', 'anywhere': False}]})

        def give(config):
            config['terms'][1]['stand_in'] = '代号乙'

        def widen(config):
            config['terms'][1]['anywhere'] = True

        def append(config):
            config['terms'].append({'term': '新词新', 'stand_in': '代号丙', 'anywhere': False})

        def delete(config):
            return config['terms'].pop(0)

        for change in (give, widen, append):
            with self.assertRaises(Refused) as caught:
                update_config(self.state, change)
            self.assertIn('自动生成', str(caught.exception))
        self.assertEqual(load_config(self.state)['terms'][0]['stand_in'], '代号甲')  # Unchanged rows keep working.
        row = update_config(self.state, delete)  # A delete is no new stand-in.
        with self.assertRaises(Refused):
            update_config(self.state, lambda config: config['terms'].insert(0, dict(row)))
        update_config(self.state, lambda config: config['terms'].insert(0, dict(row)), allow=[row])  # Put back whole.
        self.assertEqual(load_config(self.state)['terms'][0], row)

    def test_why_a_row_cannot_join_names_no_term(self):
        terms = rows('机密项目', t0='代号甲')
        cases = {('机', ''): '至少要 2 个字', ('机密项目', ''): '已经有', ('另一个词', '含机密项目的'): '替身词里含有',
                 ('代号', ''): '别的词的替身词里', ('x' * 201, ''): '不能超过', ('甲\t乙', ''): '控制字符',
                 ('自己的词', '自己的词二'): '替身词里含有'}
        for (term, stand_in), words in cases.items():
            with self.subTest(term=term[:6]):
                why = row_problem(terms, term, stand_in, ignore_case=True)
                self.assertIn(words, why)
                self.assertNotIn('机密项目', why)
        self.assertEqual(row_problem(terms, '新词', '', ignore_case=True), '')
        self.assertIn('已经有', row_problem(rows('Mimi'), 'MIMI', '', ignore_case=True))
        self.assertEqual(row_problem(rows('Mimi'), 'MIMI', '', ignore_case=False), '')
        self.assertEqual(row_problem(terms, '机密项目', '代号乙', ignore_case=True, skip=0), '')  # Its own row.
        self.assertIn('最多 1000 个词', row_problem(rows(*(f'词{i:04d}' for i in range(1000))), '新词', '',
                                                 ignore_case=True))

    def test_a_change_is_saved_and_a_refused_one_leaves_the_file(self):
        save_config(self.state, {'terms': rows('机密项目')})
        before = config_path(self.state).read_text()

        def refuse(config):
            raise Refused('不改')

        with self.assertRaises(Refused):
            update_config(self.state, refuse)
        self.assertEqual(config_path(self.state).read_text(), before)

        def add(config):
            config['terms'].append({'term': '张三丰', 'stand_in': '', 'anywhere': False})
            return 'added'

        self.assertEqual(update_config(self.state, add), 'added')
        self.assertEqual([r['term'] for r in load_config(self.state)['terms']], ['机密项目', '张三丰'])
        with self.assertRaises(Refused) as caught:  # What normalize refuses is said without the term.
            update_config(self.state, add)
        self.assertNotIn('张三丰', str(caught.exception))

    def test_the_upstream_address(self):
        self.assertEqual(upstream_problem(''), '')
        self.assertEqual(upstream_problem('https://gw.example.com/v1'), '')
        self.assertIn('http://', upstream_problem('ftp://gw.example.com'))


class RedactionTabTests(HubCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        for name in ('COVEY_DASHBOARD', 'LARK_BRIDGE_DASHBOARD'):  # No web dashboard unless a test serves one.
            os.environ.pop(name, None)
        self.panel = self.app.redaction_panel

    def show(self):
        self.open()
        self.assertEqual(self.press(self.tab('脱敏'), 'ev-tab'), 'settings_tab')
        self.assertEqual(self.title(), '脱敏看板')

    def button(self, kind, **match):
        """The value of the card's button of ``kind`` whose value holds ``match``."""
        return next(v for _, v in values(self.card(), kind) if all(v.get(k) == x for k, x in match.items()))

    def text(self):
        return json.dumps(self.card(), ensure_ascii=False)

    def key(self, term):
        return row_key(card_key(self.state), term)

    def terms(self):
        return [row['term'] for row in load_config(self.state)['terms']]

    def assertNoTerm(self, *, automatic=True):
        """No term on the card, in its record, in a log line or in a message; nor an automatic stand-in."""
        seen = [self.text(), json.dumps(self.hub_record(), ensure_ascii=False), '\n'.join(map(str, self.logs)),
                str(self.app.messenger.send_message.call_args_list)]
        for term in SECRET + (tuple(auto_stand_in(t) for t in SECRET) if automatic else ()):
            for where in seen:
                self.assertNotIn(term, where)

    def test_the_tab_is_one_layer_the_terms_one_line_each_masked_newest_first(self):
        save_config(self.state, {'terms': rows(*SECRET, t2='代号丙')})
        self.show()
        text = self.text()
        self.assertLess(text.index('张··（3 字）'), text.index('P··（11 字）'))
        self.assertLess(text.index('P··（11 字）'), text.index('机··（6 字）'))
        self.assertIn('3 个词 · 近 7 天替换 0 次 · 0 个话题开着', text)
        self.assertIn('自定义替身', text)  # An older version's stand-in: said, not shown.
        self.assertNotIn('代号丙', text)
        self.assertEqual(text.count('近 7 天 0 次'), 3)
        self.assertEqual(values(self.card(), cards.PAGE), [])  # No second layer of tabs.
        self.assertEqual([v['op'] for _, v in values(self.card(), cards.TERM)], ['delete'] * 3)
        folds = [e['element_id'] for _, _, e in self.walk() if e.get('tag') == 'collapsible_panel']
        self.assertEqual(folds, ['rx_switches', 'rx_records'])
        for _, _, panel in self.walk():  # The client refuses a form in a fold (10002), as it did live.
            if panel.get('tag') == 'collapsible_panel':
                self.assertNotIn('"tag": "form"', json.dumps(panel))
        self.assertEqual(values(self.card(), cards.PREVIEW), [])  # 试一下 is shut until opened.
        self.assertNotIn('"expanded": true', text)
        self.assertNoTerm()

    def walk(self):
        return list(walk(self.card()['body']['elements'] if 'body' in self.card() else self.card()['elements']))

    def test_terms_are_added_from_the_box_and_the_lines_left_out_said_by_number(self):
        self.show()
        add = self.button(cards.ADD)
        lines = f'{SECRET[0]}\n\n{SECRET[1]} => 代号乙\n甲\n{SECRET[0]}\n{SECRET[1]}'
        self.assertEqual(self.press(add, 'ev-a1', form_value={cards.LINES: lines}), 'redaction_tab')
        self.assertEqual([(r['term'], r['stand_in']) for r in load_config(self.state)['terms']],
                         [(SECRET[0], ''), (SECRET[1], '')])
        text = self.text()
        for words in ('加入了 「机··（6 字）」、「P··（11 字）」', '第 3 行没加（替身词自动生成，不用写 =>）',
                      '第 4 行没加（原词至少要 2 个字）', '第 5 行没加（词表里已经有这个词）'):
            self.assertIn(words, text)
        self.assertNoTerm()
        self.assertEqual(self.press(add, 'ev-a1', form_value={cards.LINES: lines}), 'duplicate')  # Delivered again.
        before = config_path(self.state).read_text()
        self.press(self.button(cards.ADD), 'ev-a2', form_value={cards.LINES: SECRET[0]})
        self.assertEqual(config_path(self.state).read_text(), before)  # Nothing could go in: nothing saved.
        self.assertIn('一个也没加：第 1 行没加（词表里已经有这个词）', self.text())
        self.press(self.button(cards.ADD), 'ev-a3', form_value={})
        self.assertIn('先在框里填词', self.text())

    def test_a_search_keeps_the_keys_it_found_not_what_was_typed(self):
        save_config(self.state, {'terms': rows(*SECRET)})
        self.show()
        self.press(self.button(cards.FIND), 'ev-f1', form_value={cards.QUERY: '密项目甲'})
        view = self.hub_record()['view']
        self.assertEqual(view['found'], [self.key(SECRET[0])])
        self.assertNotIn('密项目甲', json.dumps(self.hub_record(), ensure_ascii=False))
        self.assertIn('找到 1 个', self.text())
        self.assertEqual(self.text().count('近 7 天 0 次'), 1)
        self.assertEqual(len(values(self.card(), cards.FIND)), 1)  # The search box stays.
        self.press(self.button(cards.LIST, clear=True), 'ev-f2')
        self.assertIsNone(self.hub_record()['view']['found'])
        self.assertEqual(self.text().count('近 7 天 0 次'), 3)
        self.press(self.button(cards.FIND), 'ev-f3', form_value={cards.QUERY: 'projectz'})  # 不区分大小写 is on.
        self.assertEqual(self.hub_record()['view']['found'], [self.key(SECRET[1])])
        self.press(self.button(cards.LIST, clear=True), 'ev-f4')
        self.press(self.button(cards.FIND), 'ev-f5', form_value={cards.QUERY: '没有这个'})
        self.assertIn('没有找到', self.text())
        self.assertNoTerm()

    def test_a_term_deleted_comes_back_with_undo_once(self):
        save_config(self.state, {'terms': rows(*SECRET)})
        self.show()
        delete = self.button(cards.TERM, key=self.key(SECRET[1]), op='delete')
        self.assertEqual(self.press(delete, 'ev-d1'), 'redaction_tab')
        self.assertEqual(self.terms(), [SECRET[0], SECRET[2]])
        self.assertIn('已删除「P··（11 字）」', self.text())
        undo = self.button(cards.UNDO)
        self.press(undo, 'ev-u1')
        self.assertEqual(self.terms(), list(SECRET))  # Back where it was.
        self.assertIn('放回了「P··（11 字）」', self.text())
        self.assertEqual(values(self.card(), cards.UNDO), [])
        self.press(undo, 'ev-u2')
        self.assertIn('没有能撤销的删除了', self.text())
        save_config(self.state, {'terms': rows(SECRET[0])})  # Changed elsewhere: the row is gone.
        self.press(delete, 'ev-d2')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.terms(), [SECRET[0]])
        self.assertNoTerm()

    def test_a_button_of_the_older_layout_draws_the_card_again_and_changes_nothing(self):
        save_config(self.state, {'terms': rows(SECRET[0], SECRET[1])})
        self.show()
        before = config_path(self.state).read_text()
        zeta = self.key(SECRET[1])
        old = {'token': self.hub.token('dm', 'ev-menu'), 'revision': 1}
        for n, value in enumerate(({'kind': cards.PAGE, 'page': 'switches', 'at': 0},
                                   {'kind': cards.TERM, 'key': zeta, 'op': 'edit'},
                                   {'kind': cards.EDIT, 'key': zeta, 'op': 'stand_in'},
                                   {'kind': cards.EDIT, 'key': zeta, 'op': 'anywhere', 'on': True},
                                   {'kind': cards.EDIT, 'key': zeta, 'op': 'done'})):
            with self.subTest(value=value):
                self.assertEqual(self.press({**old, **value}, f'ev-e{n}', form_value={'stand_in': '代号乙'}),
                                 'redaction_tab')
                self.assertIn('这个按钮已更新', self.text())
                self.assertEqual(config_path(self.state).read_text(), before)
        self.assertEqual(values(self.card(), cards.EDIT), [])
        self.assertNoTerm()

    def test_the_switches_and_a_case_clash_refused(self):
        save_config(self.state, {'ignore_case': False, 'terms': rows('Mimi', 'MIMI')})
        self.show()
        self.press(self.button(cards.SWITCH, name='default_on'), 'ev-s1')
        self.assertTrue(load_config(self.state)['default_on'])
        self.assertIn('「新话题默认开」打开了', self.text())
        self.press(self.button(cards.SWITCH, name='ignore_case'), 'ev-s2')
        self.assertFalse(load_config(self.state)['ignore_case'])
        self.assertIn(CASE_CLASH, self.text())
        self.press({**self.button(cards.SWITCH, name='restore'), 'on': True}, 'ev-s3')
        self.assertIn('「回复还原」已经是开的了', self.text())

    def test_the_upstream_address_and_probe(self):
        self.show()
        self.assertEqual(values(self.card(), cards.UPSTREAM), [])
        self.press(self.button(cards.OPEN, name='upstream'), 'ev-p0')
        save = self.button(cards.UPSTREAM, op='save')
        self.press(save, 'ev-p1', form_value={cards.UPSTREAM_FIELD: 'ftp://nope'})
        self.assertIn('上游地址必须是', self.text())
        self.press(save, 'ev-p2', form_value={cards.UPSTREAM_FIELD: 'https://gw.example.com/v1/'})
        self.assertEqual(load_config(self.state)['upstream'], 'https://gw.example.com/v1')
        self.assertEqual(self.hub_record()['view']['open'], [])  # Saved: the box closes.
        self.press(self.button(cards.UPSTREAM, op='clear'), 'ev-p3')
        self.assertEqual(load_config(self.state)['upstream'], '')
        held = []
        self.panel.start = held.append  # 探测 runs off the click: held here, run below.
        self.panel.dashboard.http.detect_now = mock.Mock(return_value=(200, {'upstream': 'https://api.example.com',
                                                                             'source': '默认'}))
        probe = self.button(cards.PROBE)
        self.press(probe, 'ev-p4')
        self.assertIn('正在探测', self.text())
        self.press(probe, 'ev-p5')
        self.assertIn('上一次探测还没结束', self.text())
        self.assertEqual(len(held), 1)
        held[0]()
        self.assertIn(f"探测结果：{literal('https://api.example.com')}（默认）", self.text())
        self.assertFalse(self.hub_record()['view']['probing'])
        self.panel.dashboard.http.detect_now.return_value = (504, {'error': '探测超时，稍后再试'})
        self.press(self.button(cards.PROBE), 'ev-p6')
        held[-1]()
        self.assertIn('没探测到：探测超时', self.text())

    def test_a_try_out_shows_what_goes_out_and_not_the_text_typed(self):
        save_config(self.state, {'terms': rows(SECRET[0])})
        self.show()
        self.press(self.button(cards.OPEN, name='try'), 'ev-v0')
        self.press(self.button(cards.PREVIEW), 'ev-v1', form_value={cards.TEXT: f'我们的{SECRET[0]}\n上线了'})
        text = self.text()
        self.assertIn('替换了 1 处（1 个词）', text)
        self.assertIn('还原后与原文一致 ✓', text)
        self.assertIn(f'我们的{auto_stand_in(SECRET[0])}\\n上线了', text)  # What the vendor would see.
        self.assertNoTerm(automatic=False)
        self.press(self.button(cards.FIND), 'ev-v2', form_value={cards.QUERY: 'x'})
        self.assertIsNone(self.hub_record()['view']['preview'])  # The next click drops it.
        self.assertNoTerm()

    def test_the_records_page_names_topics_and_turns_by_their_counts(self):
        write_topic(self.state, {'topic_number': 7, 'redaction': True, 'workspace_name': 'ws', 'root_id': 'm1'},
                    topic_id='a' * 64)
        record_turn(self.state, {'topic_id': 'a' * 64, 'harness': 'claude', 'model': 'opus', 'requests': 4,
                                 'hits': {SECRET[0]: 2}})
        self.show()
        self.assertIn('⚠️ 词表是空的：开着脱敏的 1 个话题现在什么都不替换。', self.text())
        text = self.text()
        for words in ('T7', '1 轮 · 替换 2 处', '请求 4 次 · 替换 2 处'):
            self.assertIn(words, text)
        self.assertNoTerm()

    def test_a_list_that_cannot_be_read_is_said_without_its_words(self):
        write_json(config_path(self.state), {'terms': rows(SECRET[0], SECRET[0])})  # normalize quotes the term.
        self.show()
        self.assertIn(UNREADABLE, self.text())
        self.assertNoTerm()

    def test_only_the_owner_on_the_tab_it_shows(self):
        self.show()
        add = self.button(cards.ADD)
        self.assertEqual(self.press(add, 'ev-o1', operator=MEMBER, form_value={cards.LINES: '新的词语'}), 'not_owner')
        self.assertEqual(self.terms(), [])
        self.press(self.tab('群聊'), 'ev-o2')  # Turned away: the tab's buttons find nothing.
        self.assertEqual(self.press(add, 'ev-o3', form_value={cards.LINES: '新的词语'}), 'redaction_unknown')
        self.assertEqual(self.terms(), [])

    def test_the_web_dashboard_part_only_where_one_is_served(self):
        self.show()
        self.assertNotIn('网页看板**', self.text())
        self.assertEqual(values(self.card(), ROTATE_KIND), [])
        os.environ['COVEY_DASHBOARD'] = '127.0.0.1:10601'
        self.press(self.button(cards.FIND), 'ev-w1', form_value={cards.QUERY: 'x'})
        text = self.text()
        self.assertIn('打开网页看板', text)
        self.assertIn('没有在监听', text)  # The worker does not run in the tests.
        rotate, = [v for _, v in values(self.card(), ROTATE_KIND)]
        self.assertEqual(rotate, {'kind': ROTATE_KIND, 'token': self.hub.token('dm', 'ev-menu')})


if __name__ == '__main__':
    unittest.main()

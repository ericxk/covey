"""The 设置 card's 脱敏 tab (app/redaction_panel.py, cards/redaction_panel.py) and the list's edits it makes
(core/redaction.py): the four pages, each button's change of the list, refusals in words that name no term, 探测, the
retired web dashboard's 换口令 on cards sent before, and no term on a card, in a card's record or in a log line."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_topic
from test_settings_hub import MEMBER, HubCase, values
from covey.app import redaction_panel
from covey.app.redaction_panel import (CASE_CLASH, GONE, PAGE_GONE, UNREADABLE, RedactionPanel, card_key, detect_upstream,
                                       row_key)
from covey.backends.claude.redaction import Unavailable
from covey.cards import redaction_panel as cards
from covey.cards.elements import walk
from covey.cards.redaction_panel import ROTATE as ROTATE_KIND
from covey.core import migrate
from covey.core.redaction import (ALREADY, Refused, TermEdit, auto_stand_in, config_path, edit_terms, load_config, masked,
                                  put_back, record_turn, save_config, term_lines, term_problem, update_config,
                                  upstream_problem)
from covey.core.state import write_json
from covey.core.text import literal

SECRET = ('机密项目甲乙', 'ProjectZeta', '张三丰')  # No card, record or log line may hold these.


def rows(*terms, anywhere=()):
    """List rows of ``terms``; those at the indexes ``anywhere`` carry an older version's ``anywhere``."""
    return [{'term': term, 'anywhere': i in anywhere} for i, term in enumerate(terms)]


def saved(state):
    return json.loads(config_path(state).read_text(encoding='utf-8'))


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
        self.assertEqual(term_lines(['Project Atlas ', '', '鹰眼']), [(1, 'Project Atlas'), (3, '鹰眼')])  # A list's own lines.

    def test_a_stand_in_is_never_kept_and_a_new_anywhere_is_refused_an_old_one_kept(self):
        # The owner names no stand-in (T566): one an older version saved is dropped on reading and on writing; an older
        # version's ``anywhere`` keeps working (dropping it would let the term out inside a word), a new one is refused.
        save_config(self.state, {'terms': [{'term': '旧词旧', 'stand_in': '代号甲', 'anywhere': True},
                                           {'term': 'abc词', 'stand_in': '代号乙', 'anywhere': False}]})
        self.assertEqual(saved(self.state)['terms'], [{'term': '旧词旧', 'anywhere': True}, {'term': 'abc词'}])
        self.assertEqual(load_config(self.state)['terms'], rows('旧词旧', 'abc词', anywhere=(0,)))

        def widen(config):
            config['terms'][1]['anywhere'] = True

        def append(config):
            config['terms'].append({'term': '新词新', 'anywhere': True})

        def delete(config):
            return config['terms'].pop(0)

        for change in (widen, append):
            with self.assertRaises(Refused) as caught:
                update_config(self.state, change)
            self.assertIn('旧版本', str(caught.exception))
        row = update_config(self.state, delete)
        with self.assertRaises(Refused):
            update_config(self.state, lambda config: config['terms'].insert(0, dict(row)))
        update_config(self.state, lambda config: config['terms'].insert(0, dict(row)), allow=[row])  # Put back whole.
        self.assertEqual(load_config(self.state)['terms'][0], row)

    def test_why_a_term_cannot_join_names_no_term(self):
        terms = rows('机密项目')
        cases = {'机': '至少要 2 个字', '机密项目': ALREADY, 'x' * 201: '不能超过', '甲\t乙': '控制字符'}
        for term, words in cases.items():
            with self.subTest(term=term[:6]):
                why = term_problem(terms, term, ignore_case=True)
                self.assertIn(words, why)
                self.assertNotIn('机密项目', why)
        self.assertEqual(term_problem(terms, '新词', ignore_case=True), '')
        self.assertEqual(term_problem(terms, '机密', ignore_case=True), '')  # Inside a term is no stand-in's business now.
        self.assertEqual(term_problem(rows('Mimi'), 'MIMI', ignore_case=True), ALREADY)
        self.assertEqual(term_problem(rows('Mimi'), 'MIMI', ignore_case=False), '')
        self.assertIn('最多 1000 个词', term_problem(rows(*(f'词{i:04d}' for i in range(1000))), '新词', ignore_case=True))

    def test_terms_are_added_and_removed_many_at_once(self):
        save_config(self.state, {'terms': rows('机密项目', 'Project Zeta', '张三丰', anywhere=(2,))})
        edit = edit_terms(self.state, add='新词一\n\nproject zeta\n新词一\n甲\n旧 => 新\n带 空格 的词',
                          remove=['PROJECT ZETA', '没有的词', '', '张三丰'])
        self.assertEqual(edit, TermEdit(added=('新词一', 'project zeta', '带 空格 的词'),
                                        removed=(({'term': 'Project Zeta', 'anywhere': False}, 1),
                                                 ({'term': '张三丰', 'anywhere': True}, 2)),
                                        already=(4,), missing=(2,),
                                        skipped=((5, '原词至少要 2 个字'), (6, '替身词自动生成，不用写 =>'))))
        # Removes went first: a term in both is written anew, at the end, with its automatic stand-in.
        self.assertEqual(saved(self.state)['terms'],
                         [{'term': t} for t in ('机密项目', '新词一', 'project zeta', '带 空格 的词')])
        back, left = put_back(self.state, edit.removed)
        self.assertEqual((back, left), ([{'term': '张三丰', 'anywhere': True}], [({'term': 'Project Zeta', 'anywhere': False},
                                                                                ALREADY)]))
        self.assertEqual([r['term'] for r in load_config(self.state)['terms']],
                         ['机密项目', '新词一', '张三丰', 'project zeta', '带 空格 的词'])
        self.assertTrue(load_config(self.state)['terms'][2]['anywhere'])  # Back whole.

    def test_an_edit_that_changes_nothing_saves_nothing_and_whole_saves_all_or_nothing(self):
        save_config(self.state, {'terms': rows('机密项目')})
        before = config_path(self.state).read_text()
        edit = edit_terms(self.state, add=['机密项目'], remove=['没有的词'])
        self.assertEqual((edit.changed, edit.already, edit.missing), (False, (1,), (1,)))
        self.assertEqual(config_path(self.state).read_text(), before)
        with self.assertRaises(Refused) as caught:
            edit_terms(self.state, add=['新词', '机'], whole=True)
        self.assertEqual(str(caught.exception), '没有保存：第 2 行（原词至少要 2 个字）')
        self.assertEqual(config_path(self.state).read_text(), before)
        self.assertEqual(put_back(self.state, [({'term': '机密项目', 'anywhere': False}, 0)]),
                         ([], [({'term': '机密项目', 'anywhere': False}, ALREADY)]))
        self.assertEqual(config_path(self.state).read_text(), before)

    def test_the_migration_drops_the_stand_ins_of_a_list_once(self):
        path = config_path(self.state)
        write_json(path, {'ignore_case': True, 'terms': [{'term': '旧词旧', 'stand_in': '代号甲', 'anywhere': True},
                                                         {'term': 'abc词', 'stand_in': '', 'anywhere': False}]})
        self.assertEqual(migrate.pending(self.state)[migrate.REDACTION_STAND_IN], 1)
        self.assertEqual(migrate.migrate(self.state)[migrate.REDACTION_STAND_IN], {'written': 1, 'failed': 0})
        self.assertEqual(saved(self.state)['terms'], [{'term': '旧词旧', 'anywhere': True}, {'term': 'abc词'}])
        self.assertIn(migrate.REDACTION_STAND_IN, migrate.done(self.state))
        self.assertEqual(migrate.migrate(self.state), {})

    def test_a_change_is_saved_and_a_refused_one_leaves_the_file(self):
        save_config(self.state, {'terms': rows('机密项目')})
        before = config_path(self.state).read_text()

        def refuse(config):
            raise Refused('不改')

        with self.assertRaises(Refused):
            update_config(self.state, refuse)
        self.assertEqual(config_path(self.state).read_text(), before)

        def add(config):
            config['terms'].append({'term': '张三丰', 'anywhere': False})
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
        save_config(self.state, {'terms': rows(*SECRET, anywhere=(2,))})
        self.show()
        text = self.text()
        self.assertLess(text.index('张··（3 字）'), text.index('P··（11 字）'))
        self.assertLess(text.index('P··（11 字）'), text.index('机··（6 字）'))
        self.assertIn('3 个词 · 近 7 天替换 0 次 · 0 个话题开着', text)
        self.assertIn('任意位置匹配', text)  # An older version's flag: said.
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
        self.assertEqual(self.terms(), [SECRET[0], SECRET[1]])
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

    def test_terms_pasted_in_the_box_are_deleted_at_once_and_undo_puts_them_all_back(self):
        save_config(self.state, {'terms': rows(*SECRET, '另一个词')})
        self.show()
        remove = self.button(cards.REMOVE)  # The same box as 加入词表: a line a term.
        lines = f'{SECRET[2]}\n\nprojectzeta\n没有的词'
        self.assertEqual(self.press(remove, 'ev-r1', form_value={cards.LINES: lines}), 'redaction_tab')
        self.assertEqual(self.terms(), [SECRET[0], '另一个词'])  # 不区分大小写 is on.
        self.assertIn('已删除「P··（11 字）」、「张··（3 字）」；第 4 行没删（词表里没有）。', self.text())
        self.press(self.button(cards.UNDO), 'ev-r2')
        self.assertEqual(self.terms(), [*SECRET, '另一个词'])  # Each back where it was.
        self.assertIn('放回了「P··（11 字）」、「张··（3 字）」', self.text())
        self.press(self.button(cards.REMOVE), 'ev-r3', form_value={cards.LINES: '没有的词\n机密项目'})
        self.assertIn('一个也没删：第 1、2 行没删（词表里没有）', self.text())  # Only a whole term is deleted.
        self.assertEqual(self.terms(), [*SECRET, '另一个词'])
        self.press(self.button(cards.REMOVE), 'ev-r4', form_value={})
        self.assertIn('先在框里填要删的词', self.text())
        self.assertNoTerm()

    def test_a_click_is_answered_before_the_card_is_drawn_and_quick_clicks_draw_it_once(self):
        # The spinner lasted until the whole card was drawn, about a second a click (T566): the list changes and the
        # record keeps what happened before the answer; the card is drawn after it, once for the clicks before it.
        from covey.app import card_lanes
        save_config(self.state, {'terms': rows(*SECRET)})
        self.show()
        drawn = len(self.puts)
        card_lanes._click.pending = []
        try:
            for n, term in enumerate(SECRET[:2]):
                self.press(self.button(cards.TERM, key=self.key(term)), f'ev-q{n}')
            follow_ups = card_lanes._click.pending
        finally:
            card_lanes._click.pending = None
        self.assertEqual(self.terms(), [SECRET[2]])
        self.assertEqual(len(self.puts), drawn)  # Nothing drawn before the answer.
        self.assertEqual(self.hub_record()['view']['seen'][-2:], ['ev-q0', 'ev-q1'])
        for work, _, _ in follow_ups:
            work()
        self.assertEqual(len(self.puts), drawn + 1)
        self.assertIn('已删除「P··（11 字）」', self.text())
        self.assertEqual(self.text().count('近 7 天 0 次'), 1)
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
        # 新话题默认开 is the 新话题 page's 脱敏 row now (T546): not on this tab; an older card's switch says where it is.
        self.assertNotIn('新话题默认开', self.text())
        token = self.button(cards.SWITCH, name='restore')['token']
        self.press({**self.button(cards.SWITCH, name='restore'), 'name': 'default_on', 'on': True, 'token': token}, 'ev-s1')
        self.assertFalse(load_config(self.state)['default_on'])
        self.assertIn('挪到了设置卡「新话题」页', self.text())
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
        self.panel.detect_now = mock.Mock(return_value=(200, {'upstream': 'https://api.example.com', 'source': '默认'}))
        probe = self.button(cards.PROBE)
        self.press(probe, 'ev-p4')
        self.assertIn('正在探测', self.text())
        self.press(probe, 'ev-p5')
        self.assertIn('上一次探测还没结束', self.text())
        self.assertEqual(len(held), 1)
        held[0]()
        self.assertIn(f"探测结果：{literal('https://api.example.com')}（默认）", self.text())
        self.assertFalse(self.hub_record()['view']['probing'])
        self.panel.detect_now.return_value = (504, {'error': '探测超时，稍后再试'})
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

    def test_an_old_link_cards_rotate_says_the_page_went(self):
        # 换口令 on a link card the web dashboard sent (taken out 2026-11-19): no token of a card, one line under it.
        reply = self.app.messenger.reply = mock.Mock()
        self.assertEqual(self.press({'kind': ROTATE_KIND}, 'ev-l1', message='om_link'), 'dashboard_gone')
        (ctx, chat, text, key), kwargs = reply.call_args
        self.assertEqual((ctx.reply_to_message_id, chat, text), ('om_link', 'dm', PAGE_GONE))
        self.assertFalse((self.state / 'redaction' / 'dashboard-token').exists())


class DetectTests(unittest.TestCase):
    """探测: where a Claude account's requests would go (``detect_upstream``), bounded and off the click (``detect_now``)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.panel = RedactionPanel(mock.Mock(), mock.Mock(), mock.Mock(),
                                    lambda: {'upstream': 'http://127.0.0.1:9', 'source': 'test'})

    def test_detect_answers_and_says_why_it_failed(self):
        self.assertEqual(self.panel.detect_now(), (200, {'upstream': 'http://127.0.0.1:9', 'source': 'test'}))
        self.assertFalse(self.panel.detecting.locked())

        def fail():
            raise Unavailable('个人网关没有就绪')
        self.panel.detect = fail
        self.assertEqual(self.panel.detect_now(), (502, {'error': '个人网关没有就绪'}))

        def crash():
            raise RuntimeError('secret token sk-123')
        self.panel.detect = crash
        self.assertEqual(self.panel.detect_now(), (502, {'error': '探测失败（RuntimeError）'}))
        self.panel.detect = None
        self.assertEqual(self.panel.detect_now()[0], 503)

    def test_one_detection_at_a_time_and_bounded(self):
        self.panel.detecting.acquire()
        self.assertEqual(self.panel.detect_now(), (409, {'error': '上一次探测还没结束'}))
        self.panel.detecting.release()
        release = threading.Event()
        self.addCleanup(release.set)
        self.panel.detect = release.wait
        with mock.patch.object(redaction_panel, 'DETECT_TIMEOUT', 0.05):
            self.assertEqual(self.panel.detect_now(), (504, {'error': '探测超时，稍后再试'}))

    def test_detect_upstream_names_its_source(self):
        ctx = SimpleNamespace(state_dir=self.state, backend_cmd=['claude'], workdir=self.state)
        env = {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8080/'}
        harness = SimpleNamespace(context=ctx, backend=SimpleNamespace(child_env=lambda c: dict(env)))
        self.assertEqual(detect_upstream(harness), {'upstream': 'http://127.0.0.1:8080',
                                                    'source': 'Bridge 环境里的 ANTHROPIC_BASE_URL'})
        env.clear()
        env['HOME'] = str(self.state)
        with mock.patch.object(redaction_panel, 'gateway_status', return_value='http://[::1]:7000') as status:
            self.assertEqual(detect_upstream(harness)['upstream'], 'http://[::1]:7000')
        self.assertIs(status.call_args.args[0], ctx)
        with mock.patch.object(redaction_panel, 'gateway_status', return_value=''):
            self.assertEqual(detect_upstream(harness)['source'], '默认（Anthropic 官方地址）')
        self.assertRaises(ValueError, detect_upstream, SimpleNamespace(context=None))

if __name__ == '__main__':
    unittest.main()

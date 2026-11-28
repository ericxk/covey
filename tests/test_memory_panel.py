"""The 设置 card's 记忆 tab (app/memory_panel.py, cards/memory_panel.py, COVEY_MEMORY=1): the counts, 待审核 to 采纳 or 不要, the way
to the 网页记忆中心 and, without the web page, the rules to 收起; the kinds of the older tab only say they are offline. Also 常驻
principles (core/principles.py pin) and the 能做什么 part of the 诊断 tab (app/about_panel.py, cards/about_panel.py; #62): what this deployment can
do and what it lacks, read only."""
from __future__ import annotations

import json
import os
from unittest import mock

from test_settings_hub import HubCase, values
from covey.app import about_panel
from covey.app.menu_panel import hub_tabs
from covey.app.prompts import PRINCIPLES_HEAD, principles_block
from covey.cards import memory_panel as cards
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.settings_hub import ABOUT, DOCTOR, MEMORY, TAB
from covey.core import memstore, principles
from covey.core.state import write_json
from covey.core.workspaces import config_path

TOPIC = 'ab' * 8


def text_of(card):
    return json.dumps(card, ensure_ascii=False)


def contents(card):
    """Every markdown element's content, in order."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get('tag') == 'markdown':
                found.append(value.get('content') or '')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(card)
    return found


def seed(state, owner=(), workspaces=None):
    """principles.json with ``owner`` (texts, the first the oldest: a minute apart) and ``workspaces`` {name: texts}."""
    def group(texts, start):
        return [{"id": f"p{start + i:06x}", "text": text, "at": f"2026-11-0{1 + (start + i) // 1440}T"
                 f"{(start + i) // 60 % 24:02d}:{(start + i) % 60:02d}:00Z", "source": {}} for i, text in enumerate(texts)]
    write_json(principles.path(state), {"version": 1, "owner": group(owner, 0), "workspaces": {
        name: group(texts, 100 * (n + 1)) for n, (name, texts) in enumerate((workspaces or {}).items())}})


URL = 'https://covey.example.test'


def rule(state, title, scope='global', owner=''):
    return memstore.add(state, kind='rule', scope=scope, owner_key=owner, title=title, text=title + '，照这个做')


def propose(state, title, scope='global', kind='rule', text=''):
    """An open item of 待审核 (core/memstore.py review_put): [its key]."""
    return memstore.review_put(state, {'type': 'learn', 'title': title, 'text': text or title + '，以后都这样',
                                       'suggest': {'kind': kind, 'scope': scope}}, now=1_800_000_000.0)[0]


class MemoryCase(HubCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_MEMORY': '1'}))
        super().setUp()
        write_json(config_path(self.state), {'default': 'project', 'workspaces': {
            'project': str(self.work), 'docs': str(self.root)}})

    def web(self, url=URL):
        """The web page on (``url``) or off (''): the address the cards link to."""
        self.enterContext(mock.patch('covey.core.web_sync.page_url', return_value=url))

    def memory_tab(self):
        self.open()
        self.assertEqual(self.press(self.tab('记忆'), 'ev-tab'), 'settings_tab')
        self.assertEqual(self.title(), '记忆')

    def click(self, kind, event_id, **more):
        value = next(v for _, v in values(self.card(), kind) if all(v.get(k) == w for k, w in more.items()))
        return self.press(value, event_id)

    def urls(self):
        found = []

        def walk(value):
            if isinstance(value, dict):
                for behavior in value.get('behaviors') or ():
                    if behavior.get('type') == 'open_url':
                        found.append((value['text']['content'], behavior['default_url']))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(self.card())
        return found


class MemoryTabTests(MemoryCase):
    def test_the_tab_is_there_with_the_switch_only_and_doctor_always(self):
        self.assertIn(MEMORY, hub_tabs())
        self.assertEqual(hub_tabs()[-1:], [DOCTOR])  # 诊断 (app/doctor_panel.py), 能做什么 under it (T898).
        self.assertNotIn(ABOUT, hub_tabs())
        with mock.patch.dict(os.environ, {'COVEY_MEMORY': '0'}):
            self.assertNotIn(MEMORY, hub_tabs())
            self.assertIn(DOCTOR, hub_tabs())

    def test_with_the_web_page_it_is_counts_and_links_only(self):
        self.web()
        rule(self.state, '回答先给结论')
        memstore.add(self.state, kind='note', scope='global', title='网关端口', text='网关走 8443')
        propose(self.state, '发布前跑冒烟')
        self.memory_tab()
        text = text_of(self.card())
        self.assertEqual(self.card()['header']['subtitle']['content'], '规矩：全局 1 · 专项 0 · 话题 0 · 待审核 1')
        self.assertEqual(text.count('规矩：全局 1'), 2)  # The subtitle and the card's summary only: one line of counts.
        self.assertIn('发布前跑冒烟', text)  # 待审核 is listed, with what it is and what the buttons do.
        self.assertIn(cards.REVIEW_WORDS[('learn', 'global')], text)
        self.assertNotIn('回答先给结论', text)  # The rules are the web page's.
        self.assertEqual(self.urls(), [(cards.URL_LABEL, URL + '/memory')])
        # The URL buttons carry no kind; the older tab's parts are gone.
        for gone in ('下一轮带上什么', '常驻', '升为全局', '工作区 ', '看原文'):
            self.assertNotIn(gone, text)
        self.assertEqual({v['kind'] for _, v in values(self.card())} - {TAB}, {cards.ACCEPT, cards.DELETE})

    def test_without_the_web_page_the_rules_are_listed_and_can_be_put_away_in_two_steps(self):
        self.web('')
        keep = rule(self.state, '回答先给结论')
        gone = rule(self.state, '不要用表情')
        self.memory_tab()
        text = text_of(self.card())
        self.assertEqual(self.urls(), [])
        for words in ('规矩（2）', '不要用表情', '回答先给结论'):
            self.assertIn(words, text)
        for code in (gone.code, keep.code):  # T969: the owner reads titles; a code is the tools' and the page's.
            self.assertNotIn(code, text)
        self.assertEqual(self.click(cards.DELETE, 'ev-d1', key=gone.ref, op='ask'), 'memory_tab')
        self.assertIn('确认收起', text_of(self.card()))
        self.assertEqual(memstore.find(self.state, gone.ref).status, 'active')  # Not yet.
        self.assertEqual(self.click(cards.DELETE, 'ev-d0', key=gone.ref, op='cancel'), 'memory_tab')
        self.assertNotIn('确认收起', text_of(self.card()))
        self.click(cards.DELETE, 'ev-d1b', key=gone.ref, op='ask')
        self.assertEqual(self.click(cards.DELETE, 'ev-d2', key=gone.ref, op='confirm'), 'memory_tab')
        self.assertEqual(memstore.find(self.state, gone.ref).status, 'archived')
        self.assertEqual(memstore.find(self.state, keep.ref).status, 'active')
        text = text_of(self.card())
        self.assertIn('已收起：不要用表情', text)
        self.assertNotIn(gone.code, text)
        self.assertIn('规矩（1）', text)
        # The same click delivered again acts once; an older card's button for it finds it gone.
        drawn = len(self.puts)
        value = {'kind': cards.DELETE, 'token': self.hub.token('dm', 'ev-menu'), 'revision': 1, 'key': gone.ref, 'op': 'confirm'}
        self.assertEqual(self.press(value, 'ev-d2'), 'duplicate')
        self.assertEqual(len(self.puts), drawn)
        self.assertEqual(self.press(value, 'ev-d3'), 'memory_tab')
        self.assertIn('这条已经不在了', text_of(self.card()))

    def test_an_entry_that_is_not_a_rule_in_use_is_never_put_away_by_a_click(self):
        self.web('')
        note = memstore.add(self.state, kind='note', scope='global', title='网关端口', text='网关走 8443')
        self.memory_tab()
        value = {'kind': cards.DELETE, 'token': self.hub.token('dm', 'ev-menu'), 'revision': 1, 'key': note.ref, 'op': 'confirm'}
        self.assertEqual(self.press(value, 'ev-n1'), 'memory_tab')
        self.assertEqual(memstore.find(self.state, note.ref).status, 'active')

    def test_accept_and_reject_go_through_review_decide(self):
        self.web()
        yes, no = propose(self.state, '发布前跑冒烟'), propose(self.state, '别用表情', kind='rule')
        self.memory_tab()
        self.assertEqual(self.click(cards.ACCEPT, 'ev-a1', key=yes, op='accept'), 'memory_tab')
        self.assertIn('已采纳：发布前跑冒烟', text_of(self.card()))
        self.assertEqual([e.title for e in memstore.entries(self.state, scope='global')], ['发布前跑冒烟'])
        self.assertEqual({row['key'] for row in memstore.review_items(self.state)}, {no})
        self.assertEqual(self.click(cards.DELETE, 'ev-r1', key=no, op='reject'), 'memory_tab')
        self.assertIn('已选「不要」：别用表情', text_of(self.card()))
        self.assertEqual(len(memstore.entries(self.state, scope='global')), 1)  # Nothing written for a 不要.
        self.assertEqual(memstore.review_items(self.state), [])
        self.assertIn('没有待审核的', text_of(self.card()))
        # An older card's button for what was decided: the reason, not a crash, and nothing written.
        self.assertEqual(self.press({'kind': cards.ACCEPT, 'token': self.hub.token('dm', 'ev-menu'), 'revision': 1,
                                     'key': yes, 'op': 'accept'}, 'ev-a2'), 'memory_tab')
        self.assertIn('⚠️', text_of(self.card()))
        self.assertEqual(len(memstore.entries(self.state, scope='global')), 1)

    def test_the_review_list_pages(self):
        self.web()
        keys = [propose(self.state, f'第 {i} 条') for i in range(cards.REVIEW_PAGE + 2)]
        self.memory_tab()
        self.assertIn('第 1/2 页', text_of(self.card()))
        self.assertEqual(self.click(cards.OPEN, 'ev-p1', scope=cards.REVIEW, page=1), 'memory_tab')
        text = text_of(self.card())
        self.assertIn('第 2/2 页', text)
        self.assertEqual(len({v['key'] for _, v in values(self.card(), cards.ACCEPT)}), 2)
        self.assertEqual(len(keys), cards.REVIEW_PAGE + 2)

    def test_the_kinds_of_the_older_tab_say_offline_and_change_nothing(self):
        self.web('')
        old = rule(self.state, '回答先给结论')
        principle, _ = principles.save(self.state, principles.OWNER, '不要用表情')
        self.memory_tab()
        before = [(e.ref, e.status) for e in memstore.entries(self.state, statuses=None)]
        token = self.hub.token('dm', 'ev-menu')
        for n, value in enumerate(({'kind': cards.PIN, 'key': f'principle:{principle["id"]}', 'on': True},
                                   {'kind': cards.DELETE, 'key': f'principle:{principle["id"]}', 'op': 'confirm'})):
            self.assertEqual(self.press({**value, 'token': token, 'revision': 1}, f'ev-old{n}'), 'memory_tab')
            text = text_of(self.card())
            self.assertTrue(cards.OFFLINE in text or '这条已经不在了' in text, value['kind'])
        self.assertEqual([(e.ref, e.status) for e in memstore.entries(self.state, statuses=None)], before)
        self.assertEqual([p['id'] for p in principles.entries(self.state)], [principle['id']])
        self.assertEqual(memstore.find(self.state, old.ref).status, 'active')

    def test_only_the_owner_acts(self):
        self.web()
        key = propose(self.state, '发布前跑冒烟')
        self.memory_tab()
        value = next(v for _, v in values(self.card(), cards.ACCEPT))
        event = {'event_id': 'ev-x', 'chat_id': 'dm', 'message_id': 'om_s1'}  # Access lets no one else click here at all.
        self.assertEqual(self.hub.memory.click(self.ctx, event, value, False), 'owner_only')
        self.assertEqual([row['key'] for row in memstore.review_items(self.state)], [key])
        self.assertIn('仅机器人主人可以改记忆', text_of(self.card()))

    def test_a_memory_that_cannot_be_read_is_a_line_and_the_card_still_draws(self):
        self.web()
        with mock.patch('covey.app.web_memory.overview', side_effect=OSError('nfs')):
            self.memory_tab()
        self.assertIn('记忆读不出来', text_of(self.card()))

    def test_the_counts_are_one_line_of_rules_by_scope_and_review_however_many_projects(self):
        scopes = [{'key': 'global', 'title': '全局', 'counts': {}}, {'key': 'topics', 'title': '话题', 'counts': {}},
                  *({'key': f'p:{i}', 'title': f'专项{i}', 'counts': {'rule': i}} for i in range(9))]
        data = {'counts': {'rule': 9, 'pending': 2}, 'rules_by': {'global': 1, 'project': 36, 'topic': 3}, 'scopes': scopes,
                'review': [], 'rules': [], 'url': URL}
        card = cards.memory_card('c' * 24, {'revision': 1}, data)
        self.assertEqual(card['header']['subtitle']['content'], '规矩：全局 1 · 专项 36 · 话题 3 · 待审核 2')
        text = text_of(card)
        self.assertNotIn('专项4', text)  # No line per scope any more.
        self.assertNotIn('决定', text)

    def test_each_item_of_review_says_what_it_is_and_what_the_buttons_do(self):
        rows = [{'key': 'review:learn:a', 'type': 'learn', 'title': '甲', 'suggest': {'scope': 'global'}},
                {'key': 'review:learn:b', 'type': 'learn', 'title': '乙', 'suggest': {'scope': 'project'}},
                {'key': 'review:migrate:c', 'type': 'migrate', 'title': '丙', 'suggest': {'scope': 'project'}},
                {'key': 'review:other:d', 'type': 'other', 'title': '丁', 'suggest': {}}]
        self.assertEqual([cards.review_words(row) for row in rows],
                         [cards.REVIEW_WORDS[('learn', 'global')], cards.REVIEW_WORDS[('learn', '')],
                          cards.REVIEW_WORDS[('migrate', 'project')], cards.REVIEW_OTHER])
        text = text_of(cards.memory_card('c' * 24, {'revision': 1}, {'counts': {'pending': 4}, 'review': rows, 'url': ''}))
        for row in rows:
            self.assertIn(cards.review_words(row), text)

    def test_the_two_kinds_of_review_say_what_not_wanting_them_leaves(self):
        """T908 M3b: a rule held where it was said says 不要 leaves it there, one that grants says it holds only once accepted;
        the kinds older releases put here are not listed."""
        self.web()
        here = memstore.add(self.state, kind='rule', scope='topic', owner_key=TOPIC, text='回复都用中文')
        memstore.review_put(self.state, {'type': 'learn', 'title': '回复都用中文', 'text': here.text, 'items': [here.ref],
                                         'suggest': {'kind': 'rule', 'scope': 'global'}}, now=1_800_000_000.0)
        memstore.review_put(self.state, {'type': 'learn', 'title': '改完直接部署', 'text': '改完直接部署到本机',
                                         'suggest': {'kind': 'rule', 'scope': 'global', 'grant': 'deploy'}}, now=1_800_000_001.0)
        self.memory_tab()
        text = text_of(self.card())
        self.assertIn(cards.WIDER_WORDS, text)
        self.assertIn('不要就留在原处', text)
        self.assertIn(cards.GRANT_WORDS, text)
        self.assertNotIn('只关掉这一项', text)
        self.assertNotIn('改成资料', text)

    def test_accept_on_an_answer_cards_line_is_the_tabs_kind_and_acts_on_that_line(self):
        value = {'kind': cards.ACCEPT, 'topic_id': TOPIC, 'job_id': '7', 'entry_id': 'review:learn:aaaaaa'}
        event = {'event_id': 'ev-x', 'chat_id': 'dm', 'message_id': 'om_s1', 'action_value': value}
        with mock.patch('covey.app.memory_collect.accept_click', return_value='memory_accept') as accept:
            self.assertEqual(self.hub.memory.click(self.ctx, event, value, False), 'owner_only')
            self.assertEqual(self.hub.memory.click(self.ctx, event, value, True), 'memory_accept')
        accept.assert_called_once()
        with mock.patch('covey.app.memory_collect.accept_click', side_effect=ValueError('这条待审核已经处理过了')), \
                mock.patch.object(self.hub.memory.messenger, 'reply') as reply:
            self.assertEqual(self.hub.memory.click(self.ctx, event, value, True), 'memory_accept_refused')
        self.assertIn('这条待审核已经处理过了', reply.call_args.args[2])

    def test_without_the_web_page_a_topics_rules_are_listed_too(self):
        rules = [{'key': 'topic:t1:r1', 'code': 'R2N6W', 'title': '这个话题只读', 'scope': 'topic', 'over_cap': True, 'topic_number': 12}]
        text = text_of(cards.memory_card('c' * 24, {'revision': 1}, {'counts': {}, 'rules_by': {'topic': 1}, 'review': [],
                                                                      'rules': rules, 'url': ''}))
        self.assertIn('这个话题只读', text)
        self.assertIn('话题 T12', text)
        self.assertIn('只带标题', text)
        self.assertNotIn('R2N6W', text)



class WorstCaseTests(HubCase):
    def test_the_longest_memory_still_fits_one_card(self):
        from covey.cards.settings_hub import tab_row
        review = [{'key': f'review:learn:{i:06x}', 'title': '题' * 80, 'text': '长' * 400} for i in range(40)]
        rules = [{'key': f'global:p{i:06x}', 'code': 'R12345', 'title': '规' * 80, 'scope': 'project', 'over_cap': True,
                  'project_key': f'{i:012x}'} for i in range(40)]
        scopes = [{'key': 'global', 'title': '全局', 'counts': {'rule': 99, 'decision': 99, 'note': 99, 'pending': 99}},
                  {'key': 'topics', 'title': '话题', 'counts': {}},
                  *({'key': f'p:{i:012x}', 'title': '名' * 60, 'counts': {'rule': 99, 'decision': 99, 'note': 99, 'pending': 99}}
                    for i in range(40))]
        top = [tab_row(MEMORY, 'c' * 24, ['topic', 'main', 'workspace', 'groups', 'memory', 'nodes', 'doctor'])]
        for url in ('', URL):
            data = {'counts': {'rule': 99, 'decision': 99, 'note': 99, 'pending': 40}, 'scopes': scopes, 'review': review,
                    'rules': rules, 'url': url}
            for page in (0, 3):
                view = {'revision': 99999, 'hub': True, 'review_page': page, 'rules_page': page,
                        'confirm': rules[0]['key'], 'notice': '已收起：' + '长' * 24}
                card = cards.memory_card('c' * 24, view, data, top=top)
                self.assertLessEqual(element_count(card['body']['elements']), CARD_NODES, (url, page))
                self.assertLessEqual(json_size(card), CARD_BYTES, (url, page))


class PinTests(HubCase):
    def test_principles_block_keeps_the_pinned_ones_past_its_limit(self):
        old = {'text': '最早的', 'at': '2026-01-01T00:00:00Z'}
        rows = [old, *({'text': f'第 {i} 条' + '庚' * 40, 'at': f'2026-02-{i + 1:02d}T00:00:00Z'} for i in range(5))]
        self.assertNotIn('- 最早的', principles_block(rows, limit=120))
        block = principles_block([{**old, 'pinned': True}, *rows[1:]], limit=120)
        self.assertTrue(block.startswith(PRINCIPLES_HEAD))
        self.assertIn('- 最早的', block)
        self.assertIn('条较早的原则未列出', block)
        # Nothing pinned: the block as it was.
        self.assertEqual(principles_block(rows, limit=4000), principles_block([dict(row) for row in rows], limit=4000))


class AboutTabTests(HubCase):
    def about(self):
        self.open()
        self.hub.panels[DOCTOR].report = lambda: {}  # No lark-cli or service root here: the report is empty.
        self.assertEqual(self.press(self.tab('诊断'), 'ev-about'), 'settings_tab')
        self.assertEqual(self.title(), '诊断')
        return text_of(self.card())

    def test_it_says_what_is_on_and_how_to_get_what_is_not(self):
        with mock.patch.dict(os.environ, {'COVEY_DIGEST_TIMES': '21:30,09:30', 'COVEY_MEMORY': '0', 'COVEY_KNOWLEDGE': '0'}):
            text = self.about()
        # Three groups by what the owner does with them (T647 D6), each line what it does for him first.
        work, safe, see = (text.index(title + '（开着 ') for title in ('交给它办事', '不断、不丢', '看得全、记得住'))
        self.assertLess(work, safe)
        self.assertLess(safe, see)
        self.assertLess(see, text.index('到点在私聊发一张汇总卡（每天 09:30、21:30）'))
        self.assertLess(see, text.index('没开：设 COVEY&#95;MEMORY=1 和 COVEY&#95;KNOWLEDGE=1'))
        self.assertIn('Harness：', text)
        self.assertIn('没有启用扩展', text) if not about_panel.extensions.enabled() else None
        self.assertIn('--state-dir ' + str(self.state).replace('_', '&#95;').replace('.', '&#46;'), text.replace('&#45;', '-'))
        self.assertNotIn('我本来想找你的事', text)  # 巡逻 is gone (T898).
        # Read only: the tab row's buttons and none of its own.
        self.assertEqual({v['kind'] for _, v in values(self.card())}, {TAB})

    def test_it_lists_what_every_deployment_has_and_the_new_install_s_defaults(self):
        """T647 D6: 重启不打断, 换配置, 回退 and 限流后自动继续 were not listed; the 看板 ./install turns on read as off (only the
        digest's switch was read); 知识条目 was a feature of its own beside 记忆 (docs/glossary.md: one word, 记忆)."""
        env = {'COVEY_DIGEST_TIMES': 'off', 'COVEY_REVISIT': '1', 'COVEY_MEMORY': '1',
               'COVEY_KNOWLEDGE': '1'}
        with mock.patch.dict(os.environ, env):
            rows = {row['name']: row for row in about_panel.features(about_panel.Probe(self.state, self.app.topics.apps,
                                                                                        self.app.runtime))}
        for name in ('重启不打断', '换配置不断上下文', '回退一步', '限流后自动继续', '撤回和编辑跟着变', '在跑时补一句', '看板',
                     '到点回来看', '记忆'):
            self.assertTrue(rows[name]['on'], name)
        self.assertNotIn('独立复核', rows)  # Taken out (T937).
        self.assertFalse(rows['早晚汇总']['on'])
        self.assertNotIn('知识条目', rows)
        self.assertEqual({row['group'] for row in rows.values()}, {about_panel.WORK, about_panel.SAFE, about_panel.SEE})
        for row in rows.values():
            self.assertTrue(row['good'], row['name'])  # What it does for him, on or off.
        with mock.patch.dict(os.environ, {'COVEY_MEMORY': '1', 'COVEY_KNOWLEDGE': '0'}):
            half = about_panel._memory(None)
        self.assertEqual(half[:2], (True, '“记一下”的事还没开：设 COVEY_KNOWLEDGE=1'))

    def test_without_claude_code_the_butler_runs_on_codex_and_without_any_it_says_how_to_get_one(self):
        """T664: no Harness named, no Claude Code: the butler runs on Codex (app/butler_harness.py); only with no Harness
        that takes the Bridge's tools is there none, and the row says how to get one. 回退 still needs Claude Code."""
        def row(apps, name):
            rows = about_panel.features(about_panel.Probe(self.state, apps, self.app.runtime))
            return next(row for row in rows if row['name'] == name)
        apps = {name: app for name, app in self.app.topics.apps.items() if name != 'claude'}
        self.assertTrue(row(apps, '管家')['on'])
        self.assertFalse(row(apps, '回退一步')['on'])
        self.assertFalse(any(row['name'] == '敏感词脱敏' for row in about_panel.features(
            about_panel.Probe(self.state, apps, self.app.runtime))))  # 敏感词脱敏 was taken out (T947).
        # DeepSeek Harness takes the Bridge's tools too since T700: with it alone the butler runs.
        self.assertTrue(row({name: app for name, app in apps.items() if name == 'dsh'}, '管家')['on'])
        from covey.app.butler_harness import takes_tools  # An enabled extension's that takes them too (MASO) goes as well.
        none = {name: app for name, app in apps.items() if not takes_tools(app)}
        butler = row(none, '管家')
        self.assertFalse(butler['on'])
        self.assertIn('装好并登录 Claude Code、Codex 或 Antigravity 中的一个', butler['line'])

    def test_a_check_that_fails_says_so_and_the_page_stays(self):
        def broken(probe):
            raise RuntimeError('boom')
        with mock.patch.object(about_panel, 'FEATURES', (('坏的', about_panel.WORK, '做点事', broken), *about_panel.FEATURES[:1])):
            rows = about_panel.features(about_panel.Probe(self.state, self.app.topics.apps, self.app.runtime))
        self.assertEqual(rows[0], {'name': '坏的', 'group': about_panel.WORK, 'good': '做点事', 'on': False,
                                   'line': '读不出来（RuntimeError）', 'switch': ''})
        self.assertEqual(len(rows), 2)

    def test_someone_else_is_refused_on_the_card(self):
        self.about()
        event = {'event_id': 'ev-o', 'chat_id': 'dm', 'message_id': 'om_s1'}
        self.assertEqual(self.hub.click(self.ctx, event, self.tab('配置'), False), 'owner_only')
        self.assertIn('仅机器人主人可以看诊断', text_of(self.card()))

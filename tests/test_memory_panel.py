"""The 设置 card's 记忆 tab (app/memory_panel.py, cards/memory_panel.py, COVEY_MEMORY=1; T604 #29) with 常驻 principles
and the room each scope has left (core/principles.py pin, core/memory.py fill; #27), and its 能做什么 tab (app/about_panel.py,
cards/about_panel.py; #62): what this deployment can do and what it lacks, read only."""
from __future__ import annotations

import json
import os
from unittest import mock

from test_settings_hub import HubCase, values
from covey.app import about_panel, memory_turn
from covey.app.menu_panel import hub_tabs
from covey.app.prompts import PRINCIPLES_HEAD, memory_block, principles_block
from covey.cards import memory_panel as cards
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.settings_hub import ABOUT, DOCTOR, MEMORY, TAB
from covey.core import memory, principles, topic_memory
from covey.core.jobs import upsert_job_record
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


class MemoryCase(HubCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_MEMORY': '1'}))
        super().setUp()
        write_json(config_path(self.state), {'default': 'project', 'workspaces': {
            'project': str(self.work), 'docs': str(self.root)}})

    def memory_tab(self):
        self.open()
        self.assertEqual(self.press(self.tab('记忆'), 'ev-tab'), 'settings_tab')
        self.assertEqual(self.title(), '记忆')

    def click(self, kind, event_id, **more):
        value = next(v for _, v in values(self.card(), kind) if all(v.get(k) == w for k, w in more.items()))
        return self.press(value, event_id)


class MemoryTabTests(MemoryCase):
    def test_the_tab_is_there_with_the_switch_only_and_about_always(self):
        self.assertIn(MEMORY, hub_tabs())
        self.assertEqual(hub_tabs()[-2:], [ABOUT, DOCTOR])  # 能做什么, then the 诊断 tab (app/doctor_panel.py).
        with mock.patch.dict(os.environ, {'COVEY_MEMORY': ''}):
            self.assertNotIn(MEMORY, hub_tabs())
            self.assertIn(ABOUT, hub_tabs())

    def test_it_lists_each_scope_and_the_block_the_next_turn_carries(self):
        principles.save(self.state, principles.OWNER, '回答先给结论')
        principles.save(self.state, principles.WORKSPACE, '测试通过就合并', workspace='project')
        principles.save(self.state, principles.WORKSPACE, '文档用中文写', workspace='docs')
        topic_memory.add(self.state, TOPIC, '这个话题不要再问部署')
        upsert_job_record(self.state, 'job-1', job_id=1, topic_id=TOPIC, chat_id='dm', status='done',
                          memory_notes=[{'kind': 'remembered', 'text': '这个话题不要再问部署', 'scope': '管这个话题',
                                         'entry_id': 'x'}])
        self.memory_tab()
        text = text_of(self.card())
        for words in ('全局（1）', '工作区 project（1）', '话题（1）', '回答先给结论', '测试通过就合并', '这个话题不要再问部署',
                      '其他工作区：docs 1 条'):
            self.assertIn(words, text)
        self.assertNotIn('文档用中文写', text)  # Another workspace's entries: only counted.
        # The block is the one a turn builds (app/memory_turn.py): an ordinary new topic in project, the owner's own turn.
        ctx = mock.Mock(state_dir=self.state, workspace_name='project', topic_id='', device='')
        job = mock.Mock(topic_id='', chat_id='dm', message_id='m1')
        with mock.patch('covey.app.project_context.member_record', return_value=None):
            block = memory_block(*memory_turn.build(ctx, job, lambda: True))
        self.assertTrue(block)
        shown = next(content for content in contents(self.card()) if content.startswith('```text'))
        self.assertEqual(shown, '```text\n' + block + '\n```')
        self.assertIn('带上 **2 条**', text)

    def test_the_workspace_row_turns_the_preview_and_the_workspace_part(self):
        principles.save(self.state, principles.WORKSPACE, '文档用中文写', workspace='docs')
        self.memory_tab()
        self.assertNotIn('文档用中文写', text_of(self.card()))
        self.assertEqual(self.click(cards.WORKSPACE, 'ev-w', option='docs'), 'memory_tab')
        text = text_of(self.card())
        self.assertIn('工作区 docs（1）', text)
        self.assertIn('文档用中文写', text)
        self.assertEqual(self.hub_record()['view']['workspace'], 'docs')
        # A workspace no longer in the list: the default one.
        self.assertEqual(self.click(cards.WORKSPACE, 'ev-w2', option='project'), 'memory_tab')
        self.assertEqual(self.press({**self.option(cards.WORKSPACE, 'docs'), 'option': 'gone'}, 'ev-w3'), 'memory_tab')
        self.assertEqual(self.hub_record()['view']['workspace'], 'project')

    def test_delete_asks_first_then_goes_through_the_store(self):
        kept, _ = principles.save(self.state, principles.OWNER, '回答先给结论')
        gone, _ = principles.save(self.state, principles.OWNER, '不要用表情')
        self.memory_tab()
        key = f'principle:{gone["id"]}'
        self.assertEqual(self.click(cards.DELETE, 'ev-d1', key=key, op='ask'), 'memory_tab')
        self.assertIn('确认删除', text_of(self.card()))
        self.assertEqual([p['id'] for p in principles.entries(self.state)], [kept['id'], gone['id']])  # Not yet.
        self.assertEqual(self.click(cards.DELETE, 'ev-d2', key=key, op='confirm'), 'memory_tab')
        self.assertEqual([p['id'] for p in principles.entries(self.state)], [kept['id']])
        text = text_of(self.card())
        self.assertIn('已删掉：不要用表情', text)
        self.assertNotIn('确认删除', text)
        # The same click delivered again acts once; an older card's button for it finds it gone.
        drawn = len(self.puts)
        self.assertEqual(self.press({'kind': cards.DELETE, 'token': self.hub.token('dm', 'ev-menu'),
                                     'revision': 1, 'key': key, 'op': 'confirm'}, 'ev-d2'), 'duplicate')
        self.assertEqual(len(self.puts), drawn)
        self.assertEqual(self.press({'kind': cards.DELETE, 'token': self.hub.token('dm', 'ev-menu'),
                                     'revision': 1, 'key': key, 'op': 'confirm'}, 'ev-d3'), 'memory_tab')
        self.assertIn('这条已经不在了', text_of(self.card()))

    def test_a_topics_entry_is_deleted_as_its_answer_cards_不对_does(self):
        entry, _ = topic_memory.add(self.state, TOPIC, '这个话题不要再问部署')
        upsert_job_record(self.state, 'job-1', job_id=1, topic_id=TOPIC, chat_id='dm', status='done',
                          memory_notes=[{'kind': 'remembered', 'text': entry['text'], 'scope': '管这个话题',
                                         'entry_id': entry['id']}])
        self.memory_tab()
        key = f'topic:{TOPIC}:{entry["id"]}'
        self.click(cards.DELETE, 'ev-d1', key=key, op='ask')
        self.click(cards.DELETE, 'ev-d2', key=key, op='confirm')
        self.assertEqual(topic_memory.live(self.state, TOPIC), [])
        self.assertTrue(topic_memory.memory_path(self.state, TOPIC).exists())  # A tombstone, as 不对 leaves.
        self.assertIn('话题（0）', text_of(self.card()))

    def test_only_the_owner_acts(self):
        gone, _ = principles.save(self.state, principles.OWNER, '不要用表情')
        self.memory_tab()
        value = next(v for _, v in values(self.card(), cards.DELETE))
        event = {'event_id': 'ev-x', 'chat_id': 'dm', 'message_id': 'om_s1'}  # Access lets no one else click here at all.
        self.assertEqual(self.hub.memory.click(self.ctx, event, {**value, 'op': 'confirm'}, False), 'owner_only')
        self.assertEqual(len(principles.entries(self.state)), 1)
        self.assertIn('仅机器人主人可以改记忆', text_of(self.card()))

    def test_a_pinned_principle_is_never_crowded_out_and_the_card_says_what_was(self):
        seed(self.state, ['最早的一条：' + '甲' * 150, *(f'第 {i} 条：' + '乙' * 150 for i in range(8))])
        oldest = principles.entries(self.state)[0]
        self.memory_tab()
        text = text_of(self.card())
        self.assertIn('⚠️ 全局放不下 3 条', text)
        self.assertNotIn('这次带不上', text)  # The newest first: the ones left out are below the fold.
        self.click(cards.OPEN, 'ev-o', scope=memory.GLOBAL, page=0)
        self.assertEqual(text_of(self.card()).count('这次带不上'), 2)  # The newest 8 of 9: two of the three left out.
        key = f'principle:{oldest["id"]}'
        self.click(cards.OPEN, 'ev-o2', scope=memory.GLOBAL, page=1)
        self.assertEqual(self.click(cards.PIN, 'ev-p1', key=key, on=True), 'memory_tab')
        self.assertTrue(principles.entries(self.state)[0].get('pinned'))
        self.assertIn('已标常驻', text_of(self.card()))
        found = memory.items(principles=principles.for_turn(self.state, 'project'))
        kept, left = memory.select(found)
        self.assertIn(f'principle:{oldest["id"]}', [item['ref'] for item in kept])
        self.assertEqual(left, 3)
        self.assertEqual(self.click(cards.PIN, 'ev-p2', key=key, on=False), 'memory_tab')
        self.assertNotIn('pinned', principles.entries(self.state)[0])

    def test_near_its_limit_a_scope_says_so(self):
        seed(self.state, [f'第 {i} 条：' + '丙' * 190 for i in range(5)])  # 1,100 of 1,200 characters.
        self.memory_tab()
        self.assertIn('全局快满了', text_of(self.card()))

    def test_a_full_memory_fits_one_card(self):
        for i in range(30):
            principles.save(self.state, principles.OWNER, f'第 {i} 条：' + '丁' * 190)
            principles.save(self.state, principles.WORKSPACE, f'第 {i} 条：' + '戊' * 190, workspace='project')
        for i in range(20):
            topic = f'{i:02d}' * 8
            topic_memory.add(self.state, topic, f'话题的第 {i} 条：' + '己' * 190)
            upsert_job_record(self.state, f'job-{i}', job_id=i + 1, topic_id=topic, chat_id='dm', status='done',
                              memory_notes=[{'kind': 'remembered', 'text': 'x', 'scope': '管这个话题', 'entry_id': 'x'}])
        self.memory_tab()
        for scope in (memory.GLOBAL, memory.WORKSPACE, memory.TOPIC, ''):
            if scope:
                self.click(cards.OPEN, f'ev-o-{scope}', scope=scope, page=0)
            else:
                self.click(cards.OPEN, 'ev-o-close', scope='')
            card = self.card()
            self.assertLessEqual(element_count(card['body']['elements']), CARD_NODES, scope)
            self.assertLessEqual(json_size(card), CARD_BYTES, scope)
        self.click(cards.OPEN, 'ev-o-g', scope=memory.GLOBAL, page=0)
        self.assertEqual(self.click(cards.OPEN, 'ev-o-g2', scope=memory.GLOBAL, page=1), 'memory_tab')
        self.assertIn('第 2/4 页', text_of(self.card()))


class WorstCaseTests(HubCase):
    def test_the_longest_memory_still_fits_one_card(self):
        from covey.cards.settings_hub import tab_row
        names = [f"工作区{i:02d}" + "名" * 40 for i in range(12)]
        row = {"key": "principle:p000001", "text": "长" * 200, "by": "你说的", "pinned": True, "pinnable": True,
               "where": "专项「" + "名" * 60 + "」", "out": True, "at": 1}
        parts = [{"scope": scope, "title": "工作区 " + names[0] if scope == memory.WORKSPACE else "全局",
                  "rows": [{**row, "key": f"principle:p{i:06x}"} for i in range(30)],
                  **({"others": "其他工作区：" + "、".join(names)} if scope == memory.WORKSPACE else {})}
                 for scope in memory.SCOPES]
        data = {"preview": {"workspace": names[0], "workspaces": names, "count": 30, "chars": 6000, "left": 9,
                            "rooms": [["全局", 1200, 1200], ["工作区 " + names[0], 800, 800]],
                            "warnings": ["⚠️ " + "满" * 60] * 4, "text": "块" * 6000}, "parts": parts}
        top = [tab_row(MEMORY, "c" * 24, ["topic", "main", "workspace", "redaction", "groups", "memory", "nodes", "about"])]
        for scope in ("", *memory.SCOPES):
            view = {"revision": 99999, "hub": True, "open": scope, "page": 0, "confirm": "principle:p000001",
                    "notice": "已删掉：" + "长" * 24}
            card = cards.memory_card("c" * 24, view, data, top=top)
            self.assertLessEqual(element_count(card["body"]["elements"]), CARD_NODES, scope)
            self.assertLessEqual(json_size(card), CARD_BYTES, scope)

    def test_the_lines_of_full_scopes_are_a_few_and_one_for_the_rest(self):
        from covey.app import memory_panel
        self.enterContext(mock.patch.dict(os.environ, {'COVEY_MEMORY': '1'}))
        for i in range(8):
            topic = f'{i:02d}' * 8
            for n in range(4):
                topic_memory.add(self.state, topic, f'话题 {i} 的第 {n} 条：' + '满' * 190)
            upsert_job_record(self.state, f'job-{i}', job_id=i + 1, topic_id=topic, chat_id='dm', status='done',
                              memory_notes=[{'kind': 'remembered', 'text': 'x', 'scope': '管这个话题', 'entry_id': 'x'}])
        warnings = memory_panel.facts(self.state, ['project'], 'project')['preview']['warnings']
        self.assertEqual(len(warnings), memory_panel.WARNINGS)
        self.assertIn('另有 5 个专项或话题快满了或放不下', warnings[-1])


class PinTests(HubCase):
    def test_pin_holds_a_group_to_its_limit_and_reads_back(self):
        ids = [principles.save(self.state, principles.OWNER, f'第 {i} 条')[0]['id'] for i in range(principles.PINNED_LIMIT + 1)]
        for pid in ids[:-1]:
            self.assertTrue(principles.pin(self.state, pid, True)['pinned'])
        with self.assertRaisesRegex(ValueError, '常驻已有'):
            principles.pin(self.state, ids[-1], True)
        self.assertIsNone(principles.pin(self.state, 'p000000', True))
        self.assertEqual(sum(1 for row in principles.entries(self.state) if row.get('pinned')), principles.PINNED_LIMIT)
        principles.pin(self.state, ids[0], False)
        self.assertNotIn('pinned', principles.entries(self.state)[0])

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
        self.assertEqual(self.press(self.tab('能做什么'), 'ev-about'), 'settings_tab')
        self.assertEqual(self.title(), '能做什么')
        return text_of(self.card())

    def test_it_says_what_is_on_and_how_to_get_what_is_not(self):
        with mock.patch.dict(os.environ, {'COVEY_DIGEST_TIMES': '21:30,09:30', 'COVEY_MEMORY': ''}):
            text = self.about()
        on, off = text.index('能做的（'), text.index('还没开的（')
        self.assertLess(on, text.index('每天 09:30、21:30 在私聊发汇总卡'))
        self.assertLess(off, text.index('COVEY&#95;MEMORY=1'))
        self.assertIn('Harness：', text)
        self.assertIn('没有启用扩展', text) if not about_panel.extensions.enabled() else None
        # Read only: the tab row's buttons and none of its own.
        self.assertEqual({v['kind'] for _, v in values(self.card())}, {TAB})

    def test_without_claude_code_there_is_no_butler_and_it_says_how_to_get_one(self):
        apps = {name: app for name, app in self.app.topics.apps.items() if name != 'claude'}
        rows = about_panel.features(about_panel.Probe(self.state, apps, self.app.runtime))
        butler = next(row for row in rows if row['name'] == '管家')
        self.assertFalse(butler['on'])
        self.assertIn('装好并登录 Claude Code', butler['line'])
        redaction = next(row for row in rows if row['name'] == '敏感词脱敏')
        self.assertFalse(redaction['on'])

    def test_a_check_that_fails_says_so_and_the_page_stays(self):
        def broken(probe):
            raise RuntimeError('boom')
        with mock.patch.object(about_panel, 'FEATURES', (('坏的', broken), *about_panel.FEATURES[:1])):
            rows = about_panel.features(about_panel.Probe(self.state, self.app.topics.apps, self.app.runtime))
        self.assertEqual(rows[0], {'name': '坏的', 'on': False, 'line': '读不出来（RuntimeError）', 'switch': ''})
        self.assertEqual(len(rows), 2)

    def test_someone_else_is_refused_on_the_card(self):
        self.about()
        event = {'event_id': 'ev-o', 'chat_id': 'dm', 'message_id': 'om_s1'}
        self.assertEqual(self.hub.click(self.ctx, event, self.tab('新话题'), False), 'owner_only')
        self.assertIn('仅机器人主人可以看这一页', text_of(self.card()))

"""The 管家 settings card (app/main_thread_panel.py, cards/main_thread_panel.py): what its three pages (私聊默认,
新话题默认, 其他) show, each click writing its field and redrawing the whole page, the owner alone acting, and the tool
settings_panel sending it."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from support.launch_fixtures import write_maso_catalog
from support.world import UnifiedCase
from support import requires_extension
from covey.app.launch_models import available_models
from covey.app.main_topics import resolve
from covey.app.main_env import MainEnv
from covey.app import main_thread_panel as panel
from covey.app.main_thread_panel import GONE, OWNER_ONLY, TOOLS, settings_panel
from covey.cards import main_thread_panel as panel_card
from covey.cards.settings_hub import TAB, TITLES as HUB_TITLES
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.launch_config import (BUDGET_ACTION, HARNESS_ACTION, MODEL_ACTION, PROVIDER_ACTION,
                                                WORK_MODE_ACTION, model_settings)
from covey.core.defaults import RECOMMENDED
from covey.core import main_thread, main_thread_settings as settings
from covey.core.jobs import upsert_job_record
from covey.core.main_requests import Request
from covey.core.state import write_json
from covey.core.workspaces import config_path
from covey.feishu.message_request import from_argv, message_argv

MEMBER = 'ou_member0012345678'


def walk(value, found):
    if isinstance(value, dict):
        for behavior in value.get('behaviors') or ():
            found.append((value, behavior['value']))
        for child in value.values():
            walk(child, found)
    elif isinstance(value, list):
        for child in value:
            walk(child, found)
    return found


class MainThreadPanelTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.app.runtime.access.remember_owner_chat('dm')
        write_json(config_path(self.ctx.state_dir), {'default': 'project', 'workspaces': {
            'project': str(self.work), 'docs': str(self.root)}})
        upsert_job_record(self.ctx.state_dir, 'main-job', chat_id='dm', message_id='om_owner')
        settings.update(self.ctx.state_dir, enabled=True)  # The mode Personal Agent, as a new deployment starts.
        self.cards, self.sent = [], iter(['om_panel1', 'om_panel2', 'om_panel3'])
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            self.cards.append(card), ('card1', next(self.sent)))[1])
        messenger.put_card = mock.Mock(side_effect=lambda profile, card_id, card, **kw: (self.cards.append(card), {})[1])
        self.env = MainEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=messenger, voice=self.app.voice,
                           state_dir=self.ctx.state_dir, profile=self.ctx.profile, settings=self.app.settings_hub)

    def ask(self, request_id='r1'):
        return TOOLS['settings_panel'](self.env, Request(request_id, 'settings_panel', {}, 'main-job', 0.0))

    def settings(self):
        return settings.load(self.ctx.state_dir)

    def text(self):
        return json.dumps(self.cards[-1], ensure_ascii=False)

    def values(self, kind):
        return [value for _, value in walk(self.cards[-1], []) if value['kind'] == kind]

    def option(self, kind, option):
        """The value of the row option ``option`` of ``kind`` on the latest card: it must be there."""
        found = [value for value in self.values(kind) if value.get('option') == option]
        self.assertEqual(len(found), 1, (kind, option, self.values(kind)))
        return found[0]

    def click(self, value, operator='owner', event_id='c1', message='om_panel1', **fields):
        drawn = len(self.cards)
        self.app.handle_card_action_event({'event_id': event_id, 'operator_id': operator, 'chat_id': 'dm',
                                           'message_id': message, 'action_value': value, **fields}, self.ctx)
        return len(self.cards) - drawn

    def outcomes(self):
        """The rows' clicks' outcomes, not the tabs'."""
        return [outcome for outcome in (line.split('outcome=')[1].split()[0] for line in self.logs
                                        if line.startswith('card action')) if not outcome.startswith('settings_')]

    def page(self, tab):
        """The 设置 card turned to the main thread's page ``tab`` (panel_card.PAGES), or 工作区, by its tab."""
        self.turns = getattr(self, 'turns', 0) + 1
        value, = [v for v in self.values(TAB) if v['tab'] == tab]
        # On the newest card: each ask sends one, om_panel1, om_panel2, ...
        self.click(value, event_id=f'tab{self.turns}', message=f'om_panel{self.app.messenger.create_and_send_card.call_count}')
        self.assertEqual(self.cards[-1]['header']['title']['content'], HUB_TITLES[tab])

    def test_the_tool_sends_the_card_quoting_the_owner_at_the_top_level_once_a_request(self):
        self.assertIs(TOOLS['settings_panel'], settings_panel)
        answer = self.ask()
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(self.ask(), answer)  # The same request again: one card.
        send = self.app.messenger.create_and_send_card
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], 'dm')
        self.assertEqual((send.call_args.kwargs['reply_to_message_id'], send.call_args.kwargs['in_thread']),
                         ('om_owner', False))

    def test_a_quote_at_the_top_level_is_not_sent_in_the_thread(self):
        def body(**reply):
            return from_argv(message_argv('bot', reply_to='om_owner', flag='--text', value='x', key='k', **reply))[3]
        self.assertTrue(body()['reply_in_thread'])
        self.assertNotIn('reply_in_thread', body(in_thread=False))

    def test_a_newer_card_recalls_the_one_before_and_without_a_chat_none_goes(self):
        self.ask('r1')
        self.ask('r2')
        self.app.messenger.recall.assert_called_once_with(self.ctx.profile, 'om_panel1')
        self.app.runtime.access.owner_chat = lambda: ''
        answer = self.ask('r3')
        self.assertFalse(answer['ok'])
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 2)

    def test_the_default_settings_card(self):
        self.ask()
        header = self.cards[-1]['header']
        self.assertEqual((header['title']['content'], header['subtitle']['content'], header['icon']['token']),
                         ('私聊默认', '管家 · 默认模型 · 压缩 20 万', 'robot_outlined'))
        text = self.text()
        for part in ('**私聊模式**', "<text_tag color='green'>管家</text_tag> 聊天区的消息交给管家",
                     '改成纯话题', "<font color='grey'>Harness</font>　Claude Code", panel_card.HARNESS_NOTE,
                     "<font color='grey'>供应商</font>　Claude 自带", '压缩阈值', '20 万'):
            self.assertIn(part, text)
        for part in ('**新话题默认**', '**直接开工**', '由管家判断'):  # Other pages'.
            self.assertNotIn(part, text)
        self.assertNotIn('select_static', text)
        # ACCEPT-325 问题 3: each page's last line says what its settings are for.
        self.assertIn(panel_card.NOTES_OF[panel_card.MAIN], text)
        self.assertNotIn(panel_card.NOTE, text)
        # No model chosen: no budget row; Claude Code's one provider and its models are offered for the main thread,
        # its Harness is no choice.
        self.assertEqual(self.values(panel_card.EFFORT), [])
        self.assertEqual([v['option'] for v in self.values(panel_card.PROVIDER)], ['anthropic'])
        self.assertEqual([v['option'] for v in self.values(panel_card.MODEL)], ['', 'claude-model'])
        self.assertEqual([v['option'] for v in self.values(panel_card.REDACTION)], ['off', 'on'])
        self.assertEqual([v['option'] for v in self.values(panel_card.COMPACT)], ['0', '100000', '200000', '300000'])
        self.assertEqual(self.values(panel_card.TOPIC_HARNESS), [])
        self.page(panel_card.TOPIC)
        text = self.text()
        for part in ('由管家判断', '原目录', panel_card.TOPIC_NOTE, panel_card.NOTES_OF[panel_card.TOPIC]):
            self.assertIn(part, text)
        self.assertEqual(self.values(panel_card.TOPIC_MODEL), [])
        self.assertIn('codex', [v['option'] for v in self.values(panel_card.TOPIC_HARNESS)])
        self.assertEqual(self.values(panel_card.MODEL), [])
        # 直接开工 is 新话题默认's last part since 其他 was retired (2026-11-09).
        self.assertIn('**直接开工**', text)
        self.assertEqual([v['tier'] for v in self.values(panel_card.START)], list(settings.TIERS))
        self.assertTrue(all(v['on'] is False for v in self.values(panel_card.START)))
        # 记住的工作区 and each workspace's 用途 on 工作区, in the workspace list's rows.
        self.page(panel_card.WORKSPACE)
        text = self.text()
        for part in ('**记住的工作区**', panel_card.TRUSTED_EMPTY, '**project**', '**docs**', panel_card.WORKSPACE_NOTE,
                     '保存用途', '"note_0"', '"note_1"'):
            self.assertIn(part, text)
        self.assertNotIn('**工作区用途**', text)  # Not a second list of the workspaces.

    def test_an_empty_topic_default_says_what_it_resolves_to(self):
        from covey.app.launch import chat_defaults_path

        def row(label):
            return [p for p in walk_panels(self.cards[-1]) if f">{label}</font>" in json.dumps(p['header'], ensure_ascii=False)]

        def shown(label):
            title = json.dumps(row(label)[-1]['header'], ensure_ascii=False)  # 新话题默认 comes after the main thread's
            return title.split('</font>　', 1)[1].split('"', 1)[0]

        self.sent = iter(f'om_panel{i}' for i in range(1, 9))  # A new card each ask.
        claude = self.app.apps['claude'].models.configured_context(self.app.apps['claude'].harness.context)
        default = self.app.apps[self.app.topics.default]
        base = default.models.configured_context(default.harness.context)
        self.ask('r1')
        self.page(panel_card.TOPIC)
        self.assertEqual(shown('Harness'), f"Bridge 默认：{default.spec.title} · {base.model} · {base.effort}")
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'codex:codex-model',
                                                                   'effort': 'xhigh'})
        self.ask('r2')
        self.page(panel_card.TOPIC)
        self.assertEqual(shown('Harness'), '跟随上次：Codex · Codex Model · xhigh')
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='codex'))
        self.ask('r3')
        self.page(panel_card.TOPIC)
        self.assertEqual((shown('Harness'), shown('模型')), ('Codex', '跟随上次：Codex Model'))
        # ACCEPT-325 问题 2: each empty option says what a tap sets, the row and those under it emptied, not the value
        # chosen now; the 模型 row's says no 默认 beside the catalog's "Default (recommended)".
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='codex', model='codex-model',
                                                                                  effort='low'))
        self.ask('r5')
        self.page(panel_card.TOPIC)
        self.assertEqual(self.rows(panel_card.TOPIC_HARNESS)[0], ('', '跟随上次：Codex · Codex Model · xhigh'))
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL)[0], ('', '跟随上次：Codex Model'))
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[0], ('', '跟随上次：xhigh'))
        self.assertEqual(shown('Harness'), 'Codex')
        self.click(self.option(panel_card.TOPIC_MODEL, ''), event_id='c1',
                   message=f'om_panel{self.app.messenger.create_and_send_card.call_count}')
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='codex'))
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'claude:claude-model',
                                                                   'effort': 'xhigh'})
        self.ask('r6')
        self.page(panel_card.TOPIC)  # Another Harness's last launch: the Bridge's model, said without 默认.
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL)[0][1].partition('：')[0], '跟随 Bridge')
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'codex:codex-model',
                                                                   'effort': 'xhigh'})
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='claude', model='claude-model'))
        self.ask('r4')
        self.page(panel_card.TOPIC)  # Another Harness's launch: the Bridge's budget.
        self.assertEqual(shown('思考预算'), f"Bridge 默认：{claude.effort or '模型默认'}")

    def test_a_changed_settings_card(self):
        settings.update(self.ctx.state_dir, enabled=False, model='claude-model', effort='high', compact_tokens=0,
                        topic_defaults=settings.TopicDefaults(harness='codex', model='codex-model', effort='xhigh',
                                                              run_mode='worktree', workspace='docs'),
                        start=('readonly',), trusted=('project|edit', 'docs|read'),
                        workspace_notes=(('project', 'Bridge 本体'),))
        self.ask()
        header = self.cards[-1]['header']
        self.assertEqual(header['subtitle']['content'], '纯话题 · claude model · 压缩 关')
        text = self.text()
        for part in ("<text_tag color='neutral'>纯话题</text_tag> 私聊顶层一条消息开一个话题", '改成管家',
                     "✓ **<font color='blue'>high</font>**", '推荐 xhigh',
                     *(f"<font color='grey'>{label}</font>　{choice}" for label, choice in (
                         ('思考预算', 'high'), ('压缩阈值', '关')))):
            self.assertIn(part, text)
        self.page(panel_card.TOPIC)
        self.assertEqual(self.cards[-1]['header']['subtitle']['content'], 'Codex · Codex Model · 独立 worktree · 直接开工 1/3 类')
        text = self.text()
        for part in (f"<font color='grey'>{label}</font>　{choice}" for label, choice in (
                ('Harness', 'Codex'), ('运行方式', '独立 worktree'), ('工作区', 'docs'))):
            self.assertIn(part, text)
        self.assertIn("<text_tag color='green'>直接开工</text_tag>", text)
        self.assertEqual([(v['tier'], v['on']) for v in self.values(panel_card.START)],
                         [('readonly', False), ('requested', True), ('trusted', True)])
        self.page(panel_card.WORKSPACE)
        text = self.text()
        for part in ('**project** · 改文件', '**docs** · 只读', '记住的工作区（2）', '"default_value": "Bridge 本体"'):
            self.assertIn(part, text)
        self.page(panel_card.MAIN)
        self.assertEqual([v['on'] for v in self.values(panel_card.ENABLE)], [True])

    def test_each_row_writes_its_field_and_redraws_the_whole_card(self):
        self.ask()
        steps = [
            ((panel_card.MODEL, 'claude-model'), lambda s: (s.model, s.effort), ('claude-model', '')),
            ((panel_card.EFFORT, 'xhigh'), lambda s: s.effort, 'xhigh'),
            ((panel_card.COMPACT, '100000'), lambda s: s.compact_tokens, 100000),
            ((panel_card.REDACTION, 'on'), lambda s: s.redaction, True),
            panel_card.TOPIC,  # The next page, by its tab.
            ((panel_card.TOPIC_HARNESS, 'codex'), lambda s: s.topic_defaults.harness, 'codex'),
            ((panel_card.TOPIC_MODEL, 'codex-model'), lambda s: s.topic_defaults.model, 'codex-model'),
            ((panel_card.TOPIC_EFFORT, 'low'), lambda s: s.topic_defaults.effort, 'low'),
            ((panel_card.TOPIC_RUN_MODE, 'worktree'), lambda s: s.topic_defaults.run_mode, 'worktree'),
            ((panel_card.TOPIC_WORKSPACE, 'docs'), lambda s: s.topic_defaults.workspace, 'docs'),
            ((panel_card.TOPIC_HARNESS, 'claude'), lambda s: (s.topic_defaults.harness, s.topic_defaults.model,
                                                              s.topic_defaults.effort), ('claude', '', '')),
            ((panel_card.TOPIC_REDACTION, 'on'), lambda s: s.topic_defaults.redaction, True),
        ]
        for n, step in enumerate(steps):
            if isinstance(step, str):
                self.page(step)
                continue
            (kind, option), field, expected = step
            self.assertEqual(self.click(self.option(kind, option), event_id=f'c{n}'), 1, kind)
            self.assertEqual(field(self.settings()), expected, kind)
            self.assertIn('header', self.cards[-1])  # The whole card, put again.
        self.assertEqual(self.app.messenger.put_card.call_count, len(steps))
        self.assertEqual([c.kwargs['sequence'] for c in self.app.messenger.put_card.call_args_list],
                         list(range(1, len(steps) + 1)))
        self.assertEqual(self.outcomes(), ['main_panel'] * (len(steps) - 1))
        # The rows' fold IDs take the revision, so a redraw closes the row just chosen.
        folds = [p['element_id'] for p in walk_panels(self.cards[-1])]
        self.assertTrue(all(f.endswith(f'_r{len(steps) + 1}') for f in folds if f.startswith('set_')), folds)

    def test_the_subagents_row_on_the_new_topic_page_toggles_the_bot_wide_setting(self):
        self.assertEqual(self.settings().subagents, 'light')  # No field yet: the default.
        self.ask()
        self.page(panel_card.TOPIC)
        text = self.text()
        self.assertIn("<font color='grey'>子 Agent</font>　轻量（Sonnet，搜索用 Haiku）", text)
        self.assertIn(panel_card.SUBAGENT_NOTE, text)
        self.assertEqual(self.click(self.option(panel_card.SUBAGENTS, 'same'), event_id='s1'), 1)
        self.assertEqual(self.settings().subagents, 'same')
        self.assertEqual(json.loads(settings.path(self.ctx.state_dir).read_text())['subagents'], 'same')
        self.assertEqual(self.cards[-1]['header']['title']['content'], HUB_TITLES[panel_card.TOPIC])
        self.assertIn("<font color='grey'>子 Agent</font>　跟主模型", self.text())
        self.assertEqual(self.click(self.option(panel_card.SUBAGENTS, 'light'), event_id='s2'), 1)
        self.assertEqual(self.settings().subagents, 'light')
        # An option no release offers is not written.
        self.click({**self.option(panel_card.SUBAGENTS, 'same'), 'option': 'big'}, event_id='s3')
        self.assertEqual(self.settings().subagents, 'light')
        self.assertIn(GONE, self.text())
        self.page(panel_card.MAIN)
        self.assertNotIn('子 Agent', self.text())

    def test_a_bad_or_missing_subagents_field_reads_as_light(self):
        for data in ({}, {'subagents': 'huge'}, {'subagents': 3}):
            self.assertEqual(settings.from_json(data).subagents, 'light', data)
        self.assertEqual(settings.from_json({'subagents': 'same'}).subagents, 'same')

    def test_every_element_id_is_short_enough_for_cardkit_at_any_revision(self):
        current = settings.Settings(model='claude-model', topic_defaults=settings.TopicDefaults(
            harness='claude', model='claude-model'))
        card = panel_card.main_thread_panel_card('a' * 24, {'revision': 9999}, current,
                                                 available_models(self.app.topics, self.ctx), ['project'])
        folds = [p['element_id'] for p in walk_panels(card)]
        self.assertEqual(len(folds), 12, folds)  # Every row there is: 5 of the main thread's, 7 of a new topic's.
        self.assertEqual([f for f in folds if len(f) > 20], [])

    def test_switches_tiers_trusted_and_notes(self):
        settings.update(self.ctx.state_dir, trusted=('project|edit', 'docs|read'),
                        workspace_notes=(('docs', '文档'), ('gone', '已删的工作区')))
        self.addCleanup(main_thread.configure_state, main_thread.configure_state(self.ctx.state_dir))
        self.assertTrue(main_thread.enabled())
        self.ask()
        self.click(self.values(panel_card.ENABLE)[0], event_id='c1')
        self.assertFalse(self.settings().enabled)
        self.assertFalse(main_thread.enabled())  # At once, for this process too (core/main_thread.py forget_mode).
        self.assertIn(panel.MODE_NOTICES[False], self.text())
        self.assertEqual(json.loads((self.ctx.state_dir / settings.FILE).read_text())['mode'], 'topics')
        self.click(self.values(panel_card.ENABLE)[0], event_id='c2')
        self.assertTrue(self.settings().enabled)
        self.assertTrue(main_thread.enabled())
        self.assertIn(panel.MODE_NOTICES[True], self.text())
        self.page(panel_card.TOPIC)
        readonly, = [v for v in self.values(panel_card.START) if v['tier'] == 'readonly']
        self.click(readonly, event_id='c3')
        self.assertEqual(self.settings().start, ('requested', 'trusted'))
        self.click(readonly, event_id='c4')  # From the card drawn before: still turns it off.
        self.assertEqual(self.settings().start, ('requested', 'trusted'))
        self.page(panel_card.WORKSPACE)
        removed, = [v for v in self.values(panel_card.UNTRUST) if v['key'] == 'project|edit']
        self.click(removed, event_id='c5')
        self.assertEqual(self.settings().trusted, ('docs|read',))
        self.assertNotIn('**project** · 改文件', self.text())
        save, = self.values(panel_card.NOTES)
        self.assertEqual(sorted(save['workspaces']), ['docs', 'project'])
        typed = {'project': '  Bridge\n本体 ', 'docs': ''}
        self.click(save, event_id='c6', form_value={f'note_{i}': typed[name] for i, name in enumerate(save['workspaces'])})
        self.assertEqual(self.settings().workspace_notes, (('gone', '已删的工作区'), ('project', 'Bridge 本体')))
        self.assertEqual(self.cards[-1]['header']['title']['content'], '工作区')  # The tab it was clicked on.
        self.assertIn('Bridge 本体', self.text())
        self.assertEqual(self.outcomes(), ['main_panel'] * 6)

    def test_only_the_owner_changes_and_an_option_gone_changes_nothing(self):
        self.ask()
        drawn = len(self.cards)
        value = self.values(panel_card.ENABLE)[0]
        self.click(value, operator=MEMBER)  # Not the owner, in the owner's private chat.
        self.click(value, message='om_other', event_id='c2')  # Not the card the record names.
        self.click({**value, 'token': '0' * 24}, event_id='c3')
        self.assertEqual(len(self.cards), drawn)
        self.assertEqual(self.outcomes(), ['not_owner', 'main_panel_unknown', 'main_panel_unknown'])
        panel = self.app.main_thread_panel
        self.assertEqual(panel.click(self.ctx, {'chat_id': 'dm', 'message_id': 'om_panel1'}, value, False), 'owner_only')
        self.assertIn(OWNER_ONLY, self.text())
        self.assertTrue(self.settings().enabled)
        self.click({**self.option(panel_card.COMPACT, '0'), 'option': '-5'}, event_id='c5')
        self.assertIn(GONE, self.text())
        self.click({**self.option(panel_card.REDACTION, 'off'), 'option': 'maybe'}, event_id='c6')
        self.page(panel_card.TOPIC)
        self.click({**self.option(panel_card.TOPIC_HARNESS, 'codex'), 'option': 'retired'}, event_id='c4')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.settings(), settings.Settings())

    def launch_rows(self, key):
        """{row key: its option values and texts} of the launch card's settings list around menu entry ``key``."""
        rows = model_settings(available_models(self.app.topics, self.ctx), key, revision=1, effort='', kinds=(
            HARNESS_ACTION, PROVIDER_ACTION, MODEL_ACTION, BUDGET_ACTION, WORK_MODE_ACTION))
        return {row.key: [(value.partition(':')[2] if row.key in ('model', 'effort') else value, text)
                          for value, text in row.options] for row in rows}

    def rows(self, kind):
        """[(option, text)] of the latest card's row ``kind``."""
        return [(value['option'], container['elements'][0]['content'].lstrip('\u3000 ✓').strip('*')
                 .removeprefix("<font color='blue'>").removesuffix('</font>'))
                for container, value in walk(self.cards[-1], []) if value['kind'] == kind]

    @requires_extension()
    def test_new_topic_defaults_offer_the_launch_cards_providers_models_and_budgets(self):
        """T444 (the owner, 2026-11-08 22:1x): 新话题默认 had no 供应商, Codex's Acme models were cut off and MASO's
        budgets were not the launch card's. Its rows now come from the launch card's tiers and budgets."""
        from covey.core.state import write_private_text
        write_private_text(self.ctx.state_dir / 'launch-models' / 'codex.json', json.dumps({
            'updated_at': time.time(), 'models': {
                'codex-model': {'display_name': 'codex model', 'levels': ['low', 'high', 'xhigh', 'max']},
                'acme/gpt-6-astra': {'display_name': 'gpt-6-astra (Acme)', 'levels': ['low', 'high', 'xhigh', 'max']},
                'acme/gpt-6.1-sol': {'display_name': 'gpt-6.1-sol (Acme)', 'levels': ['high', 'xhigh', 'max']}}}))
        write_maso_catalog(self.ctx.state_dir)
        self.sent = iter(f'om_panel{i}' for i in range(1, 9))
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='codex'))
        self.ask()
        self.page(panel_card.TOPIC)
        acme = self.launch_rows('codex:acme/gpt-6-astra')
        # Two providers: the provider first, its models after it is chosen.
        self.assertEqual([o for o, _ in self.rows(panel_card.TOPIC_PROVIDER)], ['', 'openai', 'acme'])
        self.assertEqual(self.rows(panel_card.TOPIC_PROVIDER)[1:], acme['provider'])
        self.assertEqual(self.values(panel_card.TOPIC_MODEL), [])
        self.click(self.option(panel_card.TOPIC_PROVIDER, 'acme'), event_id='c1')
        self.assertEqual(self.settings().topic_defaults.model, 'acme/gpt-6-astra')  # Its first model, as launching.
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL), acme['model'])
        self.assertEqual([text for _, text in acme['model']], ['GPT-6 Astra', 'GPT-6.1 Sol'])
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], acme['effort'])
        self.click(self.option(panel_card.TOPIC_EFFORT, RECOMMENDED), event_id='c2')
        self.click(self.option(panel_card.TOPIC_MODEL, 'acme/gpt-6.1-sol'), event_id='c3')
        self.assertEqual((self.settings().topic_defaults.model, self.settings().topic_defaults.effort),
                         ('acme/gpt-6.1-sol', RECOMMENDED))
        self.click(self.option(panel_card.TOPIC_MODEL, 'acme/gpt-6-astra'), event_id='c4')
        # topic_start opens with the provider's model and the budget 推荐 names.
        found = resolve(self.app.topics, self.ctx.state_dir, 'dm', self.settings().topic_defaults)
        self.assertEqual((found['model_key'], found['model'], found['effort']),
                         ('codex:acme/gpt-6-astra', 'acme/gpt-6-astra', 'xhigh'))
        self.click(self.option(panel_card.TOPIC_PROVIDER, ''), event_id='c5')
        self.assertEqual((self.settings().topic_defaults.model, self.settings().topic_defaults.effort), ('', ''))
        # MASO: one provider, its models one entry each with the launch card's budgets (推荐, presets, 原生默认).
        self.click(self.option(panel_card.TOPIC_HARNESS, 'maso'), event_id='c6')
        maso = self.launch_rows('maso:gpt56_sol')
        self.assertEqual(self.rows(panel_card.TOPIC_PROVIDER), maso['provider'])
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL)[1:], maso['model'])
        self.click(self.option(panel_card.TOPIC_MODEL, 'gpt56_sol'), event_id='c7')
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], maso['effort'])
        self.assertIn(RECOMMENDED, [o for o, _ in self.rows(panel_card.TOPIC_EFFORT)])
        self.assertEqual(self.outcomes(), ['main_panel'] * 7)

    def test_the_main_threads_model_rows_are_the_launch_cards_and_old_defaults_still_read(self):
        # A main-thread.json from before 供应商 (the live file's shape, 2026-11-08): read as it was.
        (self.ctx.state_dir / settings.FILE).write_text(json.dumps({
            'enabled': True, 'mode': 'agent', 'model': '', 'effort': '', 'compact_tokens': 300000, 'schema_version': 1,
            'topic_defaults': {'harness': 'codex', 'model': 'codex-model', 'effort': 'xhigh', 'run_mode': 'direct',
                               'redaction': False, 'workspace': ''}}))
        main_thread.forget_mode()
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='codex', model='codex-model',
                                                                                effort='xhigh'))
        self.ask()
        claude = self.launch_rows('claude:claude-model')
        self.assertEqual(self.rows(panel_card.PROVIDER), claude['provider'])
        self.assertEqual(self.rows(panel_card.MODEL)[1:], claude['model'])
        self.click(self.option(panel_card.MODEL, 'claude-model'), event_id='c1')
        # The main thread's budgets are the launch card's but 原生默认, which 默认 is; 推荐 saves the level it names.
        self.assertEqual(self.rows(panel_card.EFFORT)[1:], [o for o in claude['effort'] if o[0] != 'default'])
        self.click(self.option(panel_card.EFFORT, RECOMMENDED), event_id='c2')
        self.assertEqual(claude['effort'][0], (RECOMMENDED, '推荐 xhigh'))
        self.assertEqual(self.settings().effort, 'xhigh')
        self.page(panel_card.TOPIC)
        self.assertEqual([o for o, _ in self.rows(panel_card.TOPIC_PROVIDER)], ['openai'])
        self.assertEqual(self.outcomes(), ['main_panel'] * 2)

    def test_with_the_default_model_both_pages_offer_its_budgets(self):
        """ACCEPT-325 问题 1 (the owner): 私聊默认 had no 思考预算 while its 模型 was 默认. The main thread then runs
        Claude Code's default model ("Default (recommended)", no --model), so the row offers that entry's budgets; a
        new topic's empty 模型 runs the last launch's model, so 新话题默认 offers that one's."""
        from covey.app.launch import chat_defaults_path
        from covey.core.state import write_private_text
        write_private_text(self.ctx.state_dir / 'launch-models' / 'claude.json', json.dumps({
            'updated_at': time.time(), 'models': {
                'default': {'display_name': 'Default (recommended)', 'levels': ['low', 'high', 'max']},
                'claude-model': {'display_name': 'claude model', 'levels': ['low', 'medium', 'high', 'xhigh']}}}))
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'claude:claude-model',
                                                                   'effort': 'xhigh'})
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='claude'))
        self.sent = iter(f'om_panel{i}' for i in range(1, 9))
        self.ask()
        self.assertEqual(self.settings().model, '')
        native = self.launch_rows('claude:default')['effort']
        self.assertEqual(self.rows(panel_card.EFFORT)[1:], [o for o in native if o[0] != 'default'])
        self.assertEqual(self.rows(panel_card.EFFORT)[0], ('', panel_card.DEFAULT))
        self.click(self.option(panel_card.EFFORT, 'max'), event_id='c1')
        self.assertEqual((self.settings().model, self.settings().effort), ('', 'max'))
        self.assertNotIn(GONE, self.text())
        self.page(panel_card.TOPIC)
        last = self.launch_rows('claude:claude-model')['effort']
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], last)
        self.click(self.option(panel_card.TOPIC_EFFORT, 'medium'), event_id='c2')
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='claude', effort='medium'))
        self.assertEqual(resolve(self.app.topics, self.ctx.state_dir, 'dm', self.settings().topic_defaults)['effort'],
                         'medium')
        # A budget the default model does not offer is no choice: an older card's click changes nothing.
        self.click({**self.option(panel_card.TOPIC_EFFORT, 'medium'), 'kind': panel_card.EFFORT}, event_id='c3')
        self.assertEqual(self.settings().effort, 'max')
        self.assertEqual(self.outcomes(), ['main_panel'] * 3)

    def test_the_main_threads_provider_and_redaction_rows(self):
        """T444 (the owner, 2026-11-08): 私聊默认 has what a topic's settings have: Harness (Claude Code, no choice:
        HARNESS_NOTE), 供应商 before 模型 as 新话题默认's, and 脱敏. An old main-thread.json has none: 脱敏 off."""
        self.assertFalse(settings.from_json({'model': 'x'}).redaction)
        levels = ['low', 'high', 'xhigh', 'max']
        choices = {'claude:a1': {'harness': 'claude', 'model': 'a1', 'title': 'Claude Code', 'display_name': 'A1',
                                 'provider': 'anthropic', 'provider_title': 'Claude 自带', 'levels': levels,
                                 'redaction': True},
                   'claude:k1': {'harness': 'claude', 'model': 'k1', 'title': 'Claude Code', 'display_name': 'K1',
                                 'provider': 'kiro', 'provider_title': 'Kiro', 'levels': levels, 'redaction': True},
                   'claude:k2': {'harness': 'claude', 'model': 'k2', 'title': 'Claude Code', 'display_name': 'K2',
                                 'provider': 'kiro', 'provider_title': 'Kiro', 'levels': levels, 'redaction': True}}
        self.enterContext(mock.patch.object(panel, 'available_models', lambda topics, ctx: choices))
        self.ask()
        self.assertIn("<font color='grey'>Harness</font>　Claude Code", self.text())
        self.assertEqual(self.rows(panel_card.PROVIDER), [('', '默认'), ('anthropic', 'Claude 自带'), ('kiro', 'Kiro')])
        self.assertEqual(self.values(panel_card.MODEL), [])  # Several providers: a provider first.
        self.click(self.option(panel_card.PROVIDER, 'kiro'), event_id='c1')
        self.assertEqual((self.settings().model, self.settings().effort), ('k1', ''))
        self.assertEqual([o for o, _ in self.rows(panel_card.MODEL)], ['k1', 'k2'])
        self.click(self.option(panel_card.EFFORT, 'max'), event_id='c2')
        self.click(self.option(panel_card.PROVIDER, 'anthropic'), event_id='c3')
        self.assertEqual((self.settings().model, self.settings().effort), ('a1', 'max'))  # The budget it offers too.
        self.click(self.option(panel_card.PROVIDER, ''), event_id='c4')
        self.assertEqual((self.settings().model, self.settings().effort), ('', ''))
        self.click(self.option(panel_card.REDACTION, 'on'), event_id='c5')
        self.assertTrue(self.settings().redaction)
        self.assertEqual(self.cards[-1]['header']['subtitle']['content'], '管家 · 默认模型 · 脱敏 · 压缩 20 万')
        self.click({**self.option(panel_card.PROVIDER, 'kiro'), 'option': 'gone'}, event_id='c6')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.outcomes(), ['main_panel'] * 6)
        # Without a model that can redact, 脱敏 cannot be turned on (it stays shown while on, to turn it off).
        for entry in choices.values():
            entry['redaction'] = False
        self.click(self.option(panel_card.REDACTION, 'off'), event_id='c7')
        self.assertFalse(self.settings().redaction)
        self.assertEqual(self.values(panel_card.REDACTION), [])

    def test_a_row_of_a_card_drawn_before_the_pages_lands_on_its_page(self):
        """A 设置 card sent before the pages showed every row on its one 管家 tab (key MAIN): a click on a 新话题默认
        or 其他 row there redraws the page it belongs to now (直接开工 on 新话题默认 since 其他 was retired), and the
        record's tab follows."""
        self.ask()
        token = self.values(panel_card.ENABLE)[0]['token']
        hub = self.app.settings_hub
        old = {'kind': panel_card.TOPIC_RUN_MODE, 'token': token, 'revision': 1, 'option': 'worktree'}
        self.assertEqual(self.click(old, event_id='c1'), 1)
        self.assertEqual(self.settings().topic_defaults.run_mode, 'worktree')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '新话题默认')
        record = json.loads(hub.path(self.ctx, token).read_text())
        self.assertEqual(record['tab'], panel_card.TOPIC)
        self.assertEqual([(value['tab'], button['type']) for button, value in walk(self.cards[-1], [])
                          if value['kind'] == TAB][:3], [('main', 'text'), ('topic', 'primary_filled'), ('workspace', 'text')])
        self.click({'kind': panel_card.START, 'token': token, 'revision': 1, 'tier': 'readonly', 'on': False},
                   event_id='c2')
        self.assertEqual(self.settings().start, ('requested', 'trusted'))
        self.assertEqual(self.cards[-1]['header']['title']['content'], '新话题默认')
        self.click({'kind': panel_card.UNTRUST, 'token': token, 'revision': 1, 'key': 'x|read'}, event_id='c2b')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '工作区')
        self.click({'kind': panel_card.COMPACT, 'token': token, 'revision': 1, 'option': '0'}, event_id='c3')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '私聊默认')
        self.assertEqual(self.outcomes(), ['main_panel'] * 4)

    def test_a_new_topics_whole_model_list_comes_before_the_workspaces_lines(self):
        """MASO's 13 models (2026-11-08) all listed, as on the launch card: workspaces' lines give way first."""
        levels = ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode']
        choices = {f'claude:c{i}': {'harness': 'claude', 'model': f'c{i}', 'title': 'Claude Code', 'display_name': f'C{i}',
                                    'provider': 'anthropic', 'provider_title': 'Claude 自带', 'levels': levels}
                   for i in range(5)}
        choices.update({f'maso:m{i}': {'harness': 'maso', 'model': f'm{i}', 'title': 'MASO', 'display_name': f'M{i}',
                                       'provider': 'maso', 'provider_title': 'MASO 自带', 'levels': ['high', 'xhigh', 'max']}
                        for i in range(13)})
        names = ['bridge', 'docs', 'scratch', 'project-x']
        current = settings.Settings(topic_defaults=settings.TopicDefaults(harness='maso', model='m0', effort='high'),
                                    workspace_notes=tuple((name, '用途') for name in names))
        card = panel_card.main_thread_panel_card('a' * 24, {'revision': 1}, current, choices, names)
        self.assertLessEqual(element_count(card), CARD_NODES)
        shown = [value['option'] for _, value in walk(card, []) if value['kind'] == panel_card.TOPIC_MODEL]
        self.assertEqual(shown, [''] + [f'm{i}' for i in range(13)])

    def test_a_large_catalog_and_many_workspaces_keep_within_the_card_budget(self):
        choices = {f'codex:m{i}': {'harness': 'codex', 'model': f'm{i}', 'title': 'Codex', 'display_name': f'M{i}',
                                   'levels': ['low', 'high', 'xhigh']} for i in range(40)}
        choices.update({f'claude:c{i}': {'harness': 'claude', 'model': f'c{i}', 'title': 'Claude Code',
                                         'display_name': f'C{i}', 'levels': ['high', 'max']} for i in range(10)})
        names = [f'w{i}' for i in range(30)]
        current = settings.Settings(model='c9', topic_defaults=settings.TopicDefaults(harness='codex', model='m39'),
                                    trusted=tuple(f'w{i}|edit' for i in range(8)))
        card = panel_card.main_thread_panel_card('a' * 24, {'revision': 1}, current, choices, names)
        self.assertLessEqual(element_count(card), CARD_NODES)
        self.assertLessEqual(json_size(card), CARD_BYTES)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("<font color='grey'>模型</font>　M39", text)  # The chosen model stays listed when the options are cut.
        self.assertIn('只列出前', text)


def walk_panels(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        if value.get('tag') == 'collapsible_panel':
            found.append(value)
        for child in value.values():
            walk_panels(child, found)
    elif isinstance(value, list):
        for child in value:
            walk_panels(child, found)
    return found


if __name__ == '__main__':
    unittest.main()

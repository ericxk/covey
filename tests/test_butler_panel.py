"""The 管家 settings card (app/butler_panel.py, cards/butler_panel.py): what its three pages (私聊默认,
新话题默认, 其他) show, each click writing its field and redrawing the whole page, the owner alone acting, and the tool
settings_panel sending it."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import unittest
from unittest import mock

from support.launch_fixtures import write_maso_catalog
from support.world import UnifiedCase
from support import requires_extension
from covey.app.launch_models import available_models
from covey.app.butler_model_choice import resolve
from covey.app.butler_env import ButlerEnv
from covey.app import butler_panel as panel
from covey.app.butler_panel import GONE, OWNER_ONLY, TOOLS, settings_panel
from covey.cards import butler_panel as panel_card
from covey.core.roster import CORE_HARNESSES
from covey.cards.settings_hub import REPOS, TAB, TITLES as HUB_TITLES, successor
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.launch_config import (BUDGET_ACTION, HARNESS_ACTION, MODEL_ACTION, PROVIDER_ACTION,
                                                WORK_MODE_ACTION, model_settings)
from covey.core.defaults import RECOMMENDED
NAMELESS = (RECOMMENDED, 'default')  # The budgets that do not say what they run: off the 设置 card (T615).
from covey.core import butler, butler_settings as settings
from covey.core.jobs import upsert_job_record
from covey.core.butler_requests import Request
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


class ButlerPanelTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.app.runtime.access.remember_owner_chat('dm')
        write_json(config_path(self.ctx.state_dir), {'default': 'project', 'workspaces': {
            'project': str(self.work), 'docs': str(self.root)}})
        upsert_job_record(self.ctx.state_dir, 'main-job', chat_id='dm', message_id='om_owner')
        settings.update(self.ctx.state_dir, enabled=True)  # The mode 管家, as a new deployment starts.
        self.cards, self.sent = [], iter(['om_panel1', 'om_panel2', 'om_panel3'])
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            self.cards.append(card), ('card1', next(self.sent)))[1])
        messenger.put_card = mock.Mock(side_effect=lambda profile, card_id, card, **kw: (self.cards.append(card), {})[1])
        self.env = ButlerEnv(runtime=self.app.runtime, topics=self.app.topics, messenger=messenger, voice=self.app.voice,
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

    def part(self, name):
        """配置 with its section ``name`` (panel_card.SECTIONS) open, by its tab and its 展开; nothing when it is open
        already."""
        self.page(panel_card.CONFIG)
        found = [v for v in self.values(panel_card.SECTION) if v.get('open') == name]
        if found:
            self.parts = getattr(self, 'parts', 0) + 1
            self.assertEqual(self.click(found[0], event_id=f'part{self.parts}',
                                        message=f'om_panel{self.app.messenger.create_and_send_card.call_count}'), 1)

    def page(self, tab):
        """The 设置 card turned to ``tab``, 配置 (a retired page of the butler's as it) or 工作区, by its tab; nothing when
        it shows that tab already."""
        tab = successor(tab)
        if self.cards[-1]['header']['title']['content'] == HUB_TITLES[tab]:
            return
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
        self.ask()  # The butler's settings_panel: 配置 with its 管家 part open.
        header = self.cards[-1]['header']
        self.assertEqual((header['title']['content'], header['subtitle']['content'], header['icon']['token']),
                         ('配置', '新话题 跟上次一样 · 管家 Claude Code', 'robot_outlined'))
        text = self.text()
        # T619: one page, a section a use (新话题, 管家; 独立复核 only with COVEY_REVIEW=1), one open at a time (CardKit has
        # room for one's options), the others a heading, 展开 and a grey line of what they hold; 更多 always. T546: the
        # private chat mode is a row of the butler's, not a red button on top. T614: the butler's Harness is a row of its own
        # (its 工作区 row went with the 仓库 page, v43).
        for part in ('**新话题**', '**管家**', '**更多**', panel_card.SUBAGENT_NOTE, panel_card.LOCKED,
                     "<font color='grey'>私聊顶层的消息</font>\u3000交给管家", "<font color='grey'>Harness</font>\u3000Claude Code",
                     "<font color='grey'>供应商</font>\u3000Claude 自带",
                     '压缩阈值', '20 万', '展开'):
            self.assertIn(part, text)
        for part in ('**新话题默认**', '**直接开工**', '改成纯话题', '**私聊模式**', '**独立复核**', '记住的工作区',
                     panel_card.TOPIC_NOTE, '工作区', '运行方式'):  # v43: no 工作区 or 运行方式 row, on the page or the closed line.
            self.assertNotIn(part, text)
        self.assertEqual([self.values(kind) for kind in (panel_card.SPACE, panel_card.TOPIC_WORKSPACE, panel_card.TOPIC_RUN_MODE)],
                         [[], [], []])
        self.assertLess(text.index('**新话题**'), text.index('**管家**'))
        self.assertLess(text.index('**管家**'), text.index('压缩阈值'))
        self.assertLess(text.index('压缩阈值'), text.index('私聊顶层的消息'))
        self.assertLess(text.index('私聊顶层的消息'), text.index('**更多**'))
        self.assertNotIn('select_static', text)
        self.assertNotIn(panel_card.NOTE, text)
        # T884: 辅助模型 a section of its own, closed while another one is open.
        self.assertEqual([v['open'] for v in self.values(panel_card.SECTION)], ['topic', 'assist'])
        self.assertEqual([v['option'] for v in self.values(panel_card.MODE)], ['agent', 'topics'])
        self.assertEqual(self.values(panel_card.ENABLE), [])
        self.assertEqual([v['option'] for v in self.values(panel_card.REDACTION)], ['off', 'on'])
        self.assertEqual([v['option'] for v in self.values(panel_card.COMPACT)], ['0', '100000', '200000', '300000'])
        # No model chosen: no budget row; Claude Code's one provider and its models are offered for the butler.
        self.assertEqual(self.values(panel_card.EFFORT), [])
        self.assertEqual([v['option'] for v in self.values(panel_card.PROVIDER)], ['anthropic'])
        self.assertEqual([v['option'] for v in self.values(panel_card.MODEL)], ['claude-model'])  # T615: no 默认.
        self.assertIn("<font color='grey'>模型</font>\u3000未选", text)
        self.assertEqual(self.values(panel_card.TOPIC_HARNESS), [])  # 新话题 closed: what it holds in a grey line.
        self.assertNotIn('原目录', text)  # No 运行方式 or 工作区 in its line any more.
        # 新话题 opened: its rows; 管家 closed.
        self.part('topic')
        text = self.text()
        self.assertIn(panel_card.TOPIC_NOTE, text)
        self.assertNotIn('直接开工', text)
        self.assertEqual([v['open'] for v in self.values(panel_card.SECTION)], ['butler', 'assist'])
        self.assertEqual(self.values(panel_card.MODEL), [])
        self.assertEqual(self.values(panel_card.TOPIC_MODEL), [])
        self.assertIn('codex', [v['option'] for v in self.values(panel_card.TOPIC_HARNESS)])
        self.assertNotIn('交给管家', text)  # The closed 管家's line leaves out the usual mode (USUAL).
        # 直接开工 and 记住的工作区 are retired: no row, no button.
        for kind in panel_card.RETIRED_KINDS:
            self.assertEqual(self.values(kind), [])
        self.assertNotIn('记住的工作区', text)
        # The 工作区 tab is the 仓库 tab (v43): read only, no 用途 inputs, no 保存用途.
        self.page(REPOS)
        text = self.text()
        self.assertEqual(self.cards[-1]['header']['title']['content'], '仓库')
        for part in ('**记住的工作区**', '"note_0"', '保存用途', '设为默认', '**工作区用途**', panel_card.WORKSPACE_NOTE):
            self.assertNotIn(part, text)
        self.assertEqual(self.values(panel_card.NOTES), [])

    def test_an_empty_topic_default_says_what_it_resolves_to(self):
        from covey.app.launch import chat_defaults_path

        def row(label):
            return [p for p in walk_panels(self.cards[-1]) if f">{label}</font>" in json.dumps(p['header'], ensure_ascii=False)]

        def shown(label):
            title = json.dumps(row(label)[0]['header'], ensure_ascii=False)  # 模型: 新话题 before 管家
            return re.split(r'</font>(?:\\n|　)', title, maxsplit=1)[1].split('"', 1)[0]  # A cell or a row alone.

        self.sent = iter(f'om_panel{i}' for i in range(1, 9))  # A new card each ask.
        claude = self.app.apps['claude'].models.configured_context(self.app.apps['claude'].harness.context)
        default = self.app.apps[self.app.topics.default]
        base = default.models.configured_context(default.harness.context)
        self.ask('r1')
        self.part('topic')
        self.assertEqual(shown('Harness'), f"系统默认：{default.spec.title} · {base.model} · {base.effort}")
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'codex:codex-model',
                                                                   'effort': 'xhigh'})
        self.ask('r2')
        self.part('topic')
        self.assertEqual(shown('Harness'), '跟上次一样：Codex · Codex Model · xhigh')
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='codex'))
        self.ask('r3')
        self.part('topic')
        self.assertEqual((shown('Harness'), shown('模型')), ('Codex', '跟上次一样：Codex Model'))
        # ACCEPT-325 问题 2: each empty option says what a tap sets, the row and those under it emptied, not the value
        # chosen now; the 模型 row's says no 默认 beside the catalog's "Default (recommended)".
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='codex', model='codex-model',
                                                                                  effort='low'))
        self.ask('r5')
        self.part('topic')
        self.assertEqual(self.rows(panel_card.TOPIC_HARNESS)[0], ('', '跟上次一样：Codex · Codex Model · xhigh'))
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL)[0], ('', '跟上次一样：Codex Model'))
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[0], ('', '跟上次一样：xhigh'))
        self.assertEqual(shown('Harness'), 'Codex')
        self.click(self.option(panel_card.TOPIC_MODEL, ''), event_id='c1',
                   message=f'om_panel{self.app.messenger.create_and_send_card.call_count}')
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='codex'))
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'claude:claude-model',
                                                                   'effort': 'xhigh'})
        self.ask('r6')
        self.part('topic')  # Another Harness's last launch: the Bridge's model, said without 默认.
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL)[0][1].partition('：')[0], '系统默认')
        write_json(chat_defaults_path(self.ctx.state_dir, 'dm'), {'chat_id': 'dm', 'model_choice': 'codex:codex-model',
                                                                   'effort': 'xhigh'})
        settings.update(self.ctx.state_dir, topic_defaults=settings.TopicDefaults(harness='claude', model='claude-model'))
        self.ask('r4')
        self.part('topic')  # Another Harness's launch: the Bridge's budget.
        self.assertEqual(shown('思考预算'), f"系统默认：{claude.effort or '模型默认'}")

    def test_a_changed_settings_card(self):
        settings.update(self.ctx.state_dir, enabled=False, model='claude-model', effort='high', compact_tokens=0,
                        topic_defaults=settings.TopicDefaults(harness='codex', model='codex-model', effort='xhigh',
                                                              run_mode='worktree', workspace='docs'),  # Kept, shown nowhere.
                        workspace_notes=(('project', 'Bridge 本体'),))
        self.ask()
        self.assertEqual(self.cards[-1]['header']['subtitle']['content'], '新话题 Codex Model · 管家没开')
        text = self.text()  # 纯话题: the butler's section is the mode's row alone.
        for part in ("<font color='grey'>私聊顶层的消息</font>\u3000每条直接开一个话题", panel_card.MODE_OFF, '**管家**',
                     'Codex · Codex Model · xhigh'):  # The closed 新话题's line: no 工作区, no 运行方式 any more.
            self.assertIn(part, text)
        for part in ('压缩阈值', '改成管家', panel_card.LOCKED, 'docs', '独立 worktree'):
            self.assertNotIn(part, text)
        self.assertEqual(self.values(panel_card.MODEL), [])
        self.part('topic')
        text = self.text()
        self.assertIn("<font color='grey'>Harness</font>\u3000Codex", text)
        for part in ('运行方式', '工作区', 'docs', '独立 worktree'):  # What a new topic ran in before is not a row.
            self.assertNotIn(part, text)
        settings.update(self.ctx.state_dir, enabled=True)
        self.part('butler')
        self.assertIn("✓ **<font color='blue'>high</font>**", self.text())

    def test_each_row_writes_its_field_and_redraws_the_whole_card(self):
        self.ask()  # T619: one page, 配置; the butler's tool opens it with 管家 open.
        steps = [
            # T615: a model saves a budget by name with it (its 推荐 level), no empty one.
            ((panel_card.MODEL, 'claude-model'), lambda s: (s.model, s.effort), ('claude-model', 'xhigh')),
            ((panel_card.EFFORT, 'high'), lambda s: s.effort, 'high'),
            ((panel_card.EFFORT, 'xhigh'), lambda s: s.effort, 'xhigh'),
            ((panel_card.COMPACT, '100000'), lambda s: s.compact_tokens, 100000),
            ((panel_card.REDACTION, 'on'), lambda s: s.redaction, True),
            'topic',  # 新话题 opened by its 展开: a click and a put of its own.
            ((panel_card.TOPIC_HARNESS, 'codex'), lambda s: s.topic_defaults.harness, 'codex'),
            ((panel_card.TOPIC_MODEL, 'codex-model'), lambda s: s.topic_defaults.model, 'codex-model'),
            ((panel_card.TOPIC_EFFORT, 'low'), lambda s: s.topic_defaults.effort, 'low'),
            ((panel_card.TOPIC_HARNESS, 'claude'), lambda s: (s.topic_defaults.harness, s.topic_defaults.model,
                                                              s.topic_defaults.effort), ('claude', '', '')),
            ((panel_card.TOPIC_REDACTION, 'on'), lambda s: s.topic_defaults.redaction, True),
        ]
        for n, step in enumerate(steps):
            if isinstance(step, str):
                self.part(step)
                continue
            (kind, option), field, expected = step
            self.assertEqual(self.click(self.option(kind, option), event_id=f'c{n}'), 1, kind)
            self.assertEqual(field(self.settings()), expected, kind)
            self.assertIn('header', self.cards[-1])  # The whole card, put again.
        clicks = len(steps)
        self.assertEqual(self.app.messenger.put_card.call_count, clicks)
        self.assertEqual([c.kwargs['sequence'] for c in self.app.messenger.put_card.call_args_list],
                         list(range(1, clicks + 1)))
        self.assertEqual(self.outcomes(), ['main_panel'] * clicks)
        # The rows' fold IDs take the revision, so a redraw closes the row just chosen.
        folds = [p['element_id'] for p in walk_panels(self.cards[-1])]
        self.assertTrue(all(f.endswith(f'_r{clicks + 1}') for f in folds if f.startswith('set_')), folds)

    def test_the_subagents_row_under_more_toggles_the_bot_wide_setting(self):
        self.assertEqual(self.settings().subagents, 'light')  # No field yet: the default.
        self.ask()
        self.part('topic')
        text = self.text()
        self.assertIn("<font color='grey'>子 Agent</font>\u3000轻量（Sonnet，搜索用 Haiku）", text)
        self.assertIn(panel_card.SUBAGENT_NOTE, text)
        self.assertEqual(self.click(self.option(panel_card.SUBAGENTS, 'same'), event_id='s1'), 1)
        self.assertEqual(self.settings().subagents, 'same')
        self.assertEqual(json.loads(settings.path(self.ctx.state_dir).read_text())['subagents'], 'same')
        self.assertEqual(self.cards[-1]['header']['title']['content'], HUB_TITLES[panel_card.CONFIG])
        self.assertIn("<font color='grey'>子 Agent</font>\u3000跟主模型", self.text())
        self.assertEqual(self.click(self.option(panel_card.SUBAGENTS, 'light'), event_id='s2'), 1)
        self.assertEqual(self.settings().subagents, 'light')
        # An option no release offers is not written.
        self.click({**self.option(panel_card.SUBAGENTS, 'same'), 'option': 'big'}, event_id='s3')
        self.assertEqual(self.settings().subagents, 'light')
        self.assertIn(GONE, self.text())
        self.page(REPOS)
        self.assertNotIn('子 Agent', self.text())

    def test_a_bad_or_missing_subagents_field_reads_as_light(self):
        for data in ({}, {'subagents': 'huge'}, {'subagents': 3}):
            self.assertEqual(settings.from_json(data).subagents, 'light', data)
        self.assertEqual(settings.from_json({'subagents': 'same'}).subagents, 'same')

    def test_every_element_id_is_short_enough_for_cardkit_at_any_revision(self):
        current = settings.Settings(model='claude-model', topic_defaults=settings.TopicDefaults(
            harness='claude', model='claude-model'))
        card = panel_card.butler_panel_card('a' * 24, {'revision': 9999}, current,
                                                 available_models(self.app.topics, self.ctx), ['project'])
        folds = [p['element_id'] for p in walk_panels(card)]
        self.assertEqual(len(folds), 10, folds)  # Every row there is: 5 of the butler's, 5 of a new topic's.
        self.assertEqual([f for f in folds if len(f) > 20], [])
        # The 设置 card's 配置 (T619), 管家 (a retired page) drawn as it, each of its sections open in turn: a new topic's
        # rows, the butler's (its Harness and 工作区 too, T614) and the mode, 独立复核's; 子 Agent always.
        current = settings.Settings(model='claude-model', topic_defaults=current.topic_defaults,
                                    review=settings.ReviewModel(harness='claude', model='claude-model'))
        current = dataclasses.replace(current, lead=settings.LeadModel(harness='codex', model='codex-model'))
        keys = {'topic': ('t_harness', 't_provider', 't_model', 't_effort', 't_redact'),
                'butler': ('harness', 'provider', 'model', 'effort', 'redact', 'compact', 'mode'),
                'review': ('r_harness', 'r_provider', 'r_model', 'r_effort'),
                'lead': ('l_harness', 'l_provider', 'l_model', 'l_effort')}
        for page in (panel_card.CONFIG, 'main'):
            for part, shown in keys.items():
                card = panel_card.butler_panel_card('a' * 24, {'revision': 9999, 'open': part}, current,
                                                         available_models(self.app.topics, self.ctx), ["project"],
                                                         page=page, review_on=True, lead_on=True)
                folds = [p['element_id'] for p in walk_panels(card)]
                # Those nobody changes in 更多 at the section's end (U4, T898), its fold before them.
                usual = [key for key in shown if key not in panel_card.FOLDED]
                rare = [key for key in shown if key in panel_card.FOLDED]
                self.assertEqual(folds, [f'set_{key}_r9999' for key in usual] + [f'more_{part}_r9999']
                                 + [f'set_{key}_r9999' for key in (*rare, 'subagent')])
                self.assertEqual([f for f in folds if len(f) > 20], [])

    def test_the_lead_section_with_projects_on_saves_what_a_new_lead_runs_on(self):
        """T700: 专项负责人 is a section of 配置 only with COVEY_PROJECTS=1; its Harness row offers 跟管家一样 and the Harnesses
        that take the Bridge's tools; a choice saves the settings' ``lead`` (its first model and that model's budget), and ''
        follows the butler again. A card drawn while projects were on changes nothing once they are off."""
        self.ask()
        self.assertNotIn('**专项负责人**', self.text())
        self.assertEqual(self.values(panel_card.LEAD_HARNESS), [])
        with mock.patch.dict(os.environ, {'COVEY_PROJECTS': '1'}):
            self.ask('r2')
            self.part('lead')
            text = self.text()
            self.assertIn('**专项负责人**', text)
            self.assertIn(panel_card.LEAD_NOTE, text)
            self.assertIn('负责人 跟管家', self.cards[-1]['header']['subtitle']['content'])
            # Every core Harness takes the Bridge's tools (DeepSeek Harness too since T700's second round).
            # An enabled extension's Harness that takes them too (covey_internal's MASO) is listed among them.
            self.assertEqual([v['option'] for v in self.values(panel_card.LEAD_HARNESS) if v['option'] in CORE_HARNESSES
                              or not v['option']], ['', 'claude', 'codex', 'dsh', 'agy'])
            self.click(self.option(panel_card.LEAD_HARNESS, 'codex'), event_id='lead1',
                       message=f'om_panel{self.app.messenger.create_and_send_card.call_count}')
            lead = self.settings().lead
            self.assertEqual((lead.harness, lead.model), ('codex', 'codex-model'))
            self.assertIn(panel.SAVED[panel.LEAD], self.text())
            self.assertEqual([v['option'] for v in self.values(panel_card.LEAD_MODEL)], ['codex-model'])
            self.click(self.option(panel_card.LEAD_HARNESS, ''), event_id='lead2',
                       message=f'om_panel{self.app.messenger.create_and_send_card.call_count}')
            self.assertEqual(self.settings().lead, settings.LeadModel())
            lead_card = self.cards[-1]
        self.assertIsNone(panel.ButlerPanel(self.app.topics).changes(
            self.ctx, {}, {'kind': panel_card.LEAD_HARNESS, 'option': 'codex', 'token': 't', 'revision': 1}))
        self.assertIn('**专项负责人**', json.dumps(lead_card, ensure_ascii=False))

    def test_the_lead_section_lists_only_harnesses_that_redact_while_the_butler_redacts(self):
        settings.update(self.ctx.state_dir, redaction=True)
        rows = panel_card.lead_setting_list(self.settings(), available_models(self.app.topics, self.ctx), 12)
        self.assertEqual([option for option, _ in rows[0].options], ['', 'claude'])
        rows = panel.Rows('', '', '', False, lambda: None, lambda entry, option: option, lambda **fields: fields)
        choices = available_models(self.app.topics, self.ctx)
        self.assertIsNone(panel.pick_lead_harness(rows, choices, 'codex', self.ctx.state_dir))
        self.assertEqual(panel.pick_lead_harness(rows, choices, 'claude', self.ctx.state_dir)['harness'], 'claude')

    def test_switches_the_mode_and_notes_and_the_retired_buttons_change_nothing(self):
        settings.update(self.ctx.state_dir, workspace_notes=(('docs', '文档'), ('gone', '已删的工作区')))
        self.addCleanup(butler.configure_state, butler.configure_state(self.ctx.state_dir))
        self.assertTrue(butler.enabled())
        self.ask()
        # 每条直接开一个话题 asks first (T546): nothing is saved until 确定改; 取消 closes the question.
        self.click(self.option(panel_card.MODE, 'topics'), event_id='c1')
        self.assertTrue(self.settings().enabled)
        self.assertIn(panel_card.MODE_CONFIRM, self.text())
        cancel, = [v for v in self.values(panel_card.MODE) if 'revision' in v]  # The question's 取消.
        self.assertEqual(cancel['option'], 'agent')
        self.click(cancel, event_id='c1b')
        self.assertTrue(self.settings().enabled)
        self.assertNotIn(panel_card.MODE_CONFIRM, self.text())
        self.click(self.option(panel_card.MODE, 'topics'), event_id='c1c')
        sure, = self.values(panel_card.ENABLE)
        self.assertIs(sure['on'], False)
        self.click(sure, event_id='c1d')
        self.assertFalse(self.settings().enabled)
        self.assertFalse(butler.enabled())  # At once, for this process too (core/butler.py forget_mode).
        self.assertIn(panel.MODE_NOTICES[False], self.text())
        self.assertNotIn(panel_card.MODE_CONFIRM, self.text())
        self.assertEqual(json.loads((self.ctx.state_dir / settings.FILE).read_text())['mode'], 'topics')
        self.click(self.option(panel_card.MODE, 'agent'), event_id='c2')  # Back to the butler: no question.
        self.assertTrue(self.settings().enabled)
        self.assertTrue(butler.enabled())
        self.assertIn(panel.MODE_NOTICES[True], self.text())
        self.part('topic')
        # 直接开工 and 记住的工作区 are retired: an older card's buttons say so and change nothing.
        token = self.values(panel_card.SECTION)[0]['token']
        before = self.settings()
        for n, value in enumerate(({'kind': panel_card.START_ROW, 'option': 'safe'},
                                   {'kind': panel_card.START, 'tier': 'trusted', 'on': False},
                                   {'kind': panel_card.UNTRUST, 'key': 'project|edit'})):
            self.click({**value, 'token': token}, event_id=f'c3{n}')
            self.assertEqual(self.settings(), before)
            self.assertIn(panel_card.RETIRED_NOTICE, self.text())
            self.assertEqual(self.cards[-1]['header']['title']['content'], HUB_TITLES[panel_card.CONFIG])
        # The 工作区 and 运行方式 rows and 保存用途 are off the page (v43): an older card's buttons say so, change nothing, and the
        # butler's own workspace setting stays as it was (it keeps an older deployment's butler where it works).
        for n, value in enumerate(({'kind': panel_card.TOPIC_WORKSPACE, 'option': 'docs'},
                                   {'kind': panel_card.TOPIC_RUN_MODE, 'option': 'worktree'},
                                   {'kind': panel_card.SPACE, 'option': 'docs'},
                                   {'kind': panel_card.NOTES, 'workspaces': ['docs', 'project']})):
            self.click({**value, 'token': token}, event_id=f'c4{n}', form_value={'note_0': 'x', 'note_1': 'y'})
            self.assertEqual(self.settings(), before)
            self.assertIn(panel_card.OFFLINE_NOTICE, self.text())
            self.assertEqual(self.cards[-1]['header']['title']['content'], HUB_TITLES[panel_card.CONFIG])
        self.assertEqual(self.outcomes(), ['main_panel'] * 13)  # 新话题's 展开 among them.

    def test_only_the_owner_changes_and_an_option_gone_changes_nothing(self):
        self.ask()
        drawn = len(self.cards)
        value = self.option(panel_card.MODE, 'topics')
        self.click(value, operator=MEMBER)  # Not the owner, in the owner's private chat.
        self.click(value, message='om_other', event_id='c2')  # Not the card the record names.
        self.click({**value, 'token': '0' * 24}, event_id='c3')
        self.assertEqual(len(self.cards), drawn)
        self.assertEqual(self.outcomes(), ['not_owner', 'main_panel_unknown', 'main_panel_unknown'])
        panel = self.app.butler_panel
        self.assertEqual(panel.click(self.ctx, {'chat_id': 'dm', 'message_id': 'om_panel1'}, value, False), 'owner_only')
        self.assertIn(OWNER_ONLY, self.text())
        self.assertTrue(self.settings().enabled)
        self.click({**self.option(panel_card.COMPACT, '0'), 'option': '-5'}, event_id='c5')
        self.assertIn(GONE, self.text())
        self.click({**self.option(panel_card.REDACTION, 'off'), 'option': 'maybe'}, event_id='c6')
        self.part('topic')
        self.click({**self.option(panel_card.TOPIC_HARNESS, 'codex'), 'option': 'retired'}, event_id='c4')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.settings(), settings.Settings())

    def test_a_settings_file_that_cannot_be_read_now_is_not_reset_and_the_card_says_so(self):
        """W1-F (RT-6): read as {} a moment, a click on one row wrote the defaults over every other setting."""
        settings.update(self.ctx.state_dir, compact_tokens=0, harness='codex')
        self.ask()
        self.part('topic')
        path = settings.path(self.ctx.state_dir)
        body = path.read_bytes()
        path.write_bytes(body[: len(body) // 2])
        torn = path.read_bytes()
        self.click(self.option(panel_card.SUBAGENTS, 'same'), event_id='c7')
        self.assertEqual(path.read_bytes(), torn)
        self.assertIn(settings.UNREADABLE, self.text())
        self.assertTrue(any('main thread settings not saved' in line for line in self.logs))
        path.write_bytes(body)
        self.click(self.option(panel_card.SUBAGENTS, 'same'), event_id='c8')
        self.assertEqual((self.settings().subagents, self.settings().compact_tokens, self.settings().harness), ('same', 0, 'codex'))

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
        self.part('topic')
        acme = self.launch_rows('codex:acme/gpt-6-astra')
        # Two providers: the provider first, its models after it is chosen.
        self.assertEqual([o for o, _ in self.rows(panel_card.TOPIC_PROVIDER)], ['', 'openai', 'acme'])
        self.assertEqual(self.rows(panel_card.TOPIC_PROVIDER)[1:], acme['provider'])
        self.assertEqual(self.values(panel_card.TOPIC_MODEL), [])
        self.click(self.option(panel_card.TOPIC_PROVIDER, 'acme'), event_id='c1')
        self.assertEqual(self.settings().topic_defaults.model, 'acme/gpt-6-astra')  # Its first model, as launching.
        self.assertEqual(self.rows(panel_card.TOPIC_MODEL), acme['model'])
        self.assertEqual([text for _, text in acme['model']], ['GPT-6 Astra', 'GPT-6.1 Sol'])
        # The launch card's levels by name, no 推荐 or 原生默认 (T615).
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], [o for o in acme['effort'] if o[0] not in NAMELESS])
        self.click(self.option(panel_card.TOPIC_EFFORT, 'xhigh'), event_id='c2')
        self.click(self.option(panel_card.TOPIC_MODEL, 'acme/gpt-6.1-sol'), event_id='c3')
        self.assertEqual((self.settings().topic_defaults.model, self.settings().topic_defaults.effort),
                         ('acme/gpt-6.1-sol', 'xhigh'))
        self.click(self.option(panel_card.TOPIC_MODEL, 'acme/gpt-6-astra'), event_id='c4')
        # topic_start opens with the provider's model and the budget chosen.
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
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], [o for o in maso['effort'] if o[0] not in NAMELESS])
        self.assertNotIn(RECOMMENDED, [o for o, _ in self.rows(panel_card.TOPIC_EFFORT)])
        self.assertEqual(self.outcomes(), ['main_panel'] * 8)  # 新话题's 展开 first.

    def test_the_butlers_model_rows_are_the_launch_cards_and_old_defaults_still_read(self):
        # A main-thread.json from before 供应商 (the live file's shape, 2026-11-08): read as it was.
        (self.ctx.state_dir / settings.FILE).write_text(json.dumps({
            'enabled': True, 'mode': 'agent', 'model': '', 'effort': '', 'compact_tokens': 300000, 'schema_version': 1,
            'topic_defaults': {'harness': 'codex', 'model': 'codex-model', 'effort': 'xhigh', 'run_mode': 'direct',
                               'redaction': False, 'workspace': ''}}))
        butler.forget_mode()
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='codex', model='codex-model',
                                                                                effort='xhigh'))
        self.ask()
        claude = self.launch_rows('claude:claude-model')
        self.assertEqual(self.rows(panel_card.PROVIDER), claude['provider'])
        self.assertEqual(self.rows(panel_card.MODEL), claude['model'])  # T615: no 默认.
        self.click(self.option(panel_card.MODEL, 'claude-model'), event_id='c1')
        # The butler's budgets are the launch card's levels by name (T615; the launch card's too since, docs/ui.md 13 U5):
        # no 推荐, 原生默认 or 默认; the model came with the level 推荐 names.
        self.assertEqual(self.rows(panel_card.EFFORT), claude['effort'])
        self.assertFalse([o for o in claude['effort'] if o[0] in NAMELESS])
        self.assertEqual(self.settings().effort, 'xhigh')
        # 推荐 on a card drawn before is no choice.
        self.click(self.option(panel_card.EFFORT, 'low'), event_id='c2')
        self.click({**self.option(panel_card.EFFORT, 'low'), 'option': RECOMMENDED}, event_id='c2b')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.settings().effort, 'low')
        self.part('topic')
        self.assertEqual([o for o, _ in self.rows(panel_card.TOPIC_PROVIDER)], ['openai'])  # The same page.
        self.assertEqual(self.outcomes(), ['main_panel'] * 4)

    def test_with_the_default_model_both_pages_offer_its_budgets(self):
        """ACCEPT-325 问题 1 (the owner): 私聊默认 had no 思考预算 while its 模型 was 默认. The butler then runs
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
        # T615: its levels by name; no 默认, 推荐 or 原生默认, and "Default (recommended)" is no 模型 choice.
        self.assertEqual(self.rows(panel_card.EFFORT), [o for o in native if o[0] not in NAMELESS])
        self.assertEqual([o for o, _ in self.rows(panel_card.MODEL)], ['claude-model'])
        self.assertIn("<font color='grey'>模型</font>\u3000未选", self.text())
        self.click(self.option(panel_card.EFFORT, 'max'), event_id='c1')
        self.assertEqual((self.settings().model, self.settings().effort), ('', 'max'))
        self.assertNotIn(GONE, self.text())
        last = self.launch_rows('claude:claude-model')['effort']  # The same page.
        self.part('topic')
        self.assertEqual(self.rows(panel_card.TOPIC_EFFORT)[1:], [o for o in last if o[0] not in NAMELESS])
        self.assertNotIn('default', [o for o, _ in self.rows(panel_card.TOPIC_MODEL)])
        self.click(self.option(panel_card.TOPIC_EFFORT, 'medium'), event_id='c2')
        self.assertEqual(self.settings().topic_defaults, settings.TopicDefaults(harness='claude', effort='medium'))
        self.assertEqual(resolve(self.app.topics, self.ctx.state_dir, 'dm', self.settings().topic_defaults)['effort'],
                         'medium')
        # A budget the default model does not offer is no choice: an older card's click changes nothing.
        self.click({**self.option(panel_card.TOPIC_EFFORT, 'medium'), 'kind': panel_card.EFFORT}, event_id='c3')
        self.assertEqual(self.settings().effort, 'max')
        self.assertEqual(self.outcomes(), ['main_panel'] * 4)

    def test_the_butlers_provider_and_redaction_rows(self):
        """T444 (the owner, 2026-11-08): 管家 has what a topic's settings have: Harness (Claude Code, no choice: said in
        the title bar since T546), 供应商 before 模型 as 新话题's, and 脱敏. An old main-thread.json has none: 脱敏 off."""
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
        self.assertTrue(self.cards[-1]['header']['subtitle']['content'].endswith('管家 Claude Code'))
        self.assertEqual(self.rows(panel_card.PROVIDER), [('anthropic', 'Claude 自带'), ('kiro', 'Kiro')])  # No 默认.
        self.assertEqual(self.values(panel_card.MODEL), [])  # Several providers: a provider first.
        self.click(self.option(panel_card.PROVIDER, 'kiro'), event_id='c1')
        self.assertEqual((self.settings().model, self.settings().effort), ('k1', 'xhigh'))
        self.assertEqual([o for o, _ in self.rows(panel_card.MODEL)], ['k1', 'k2'])
        self.click(self.option(panel_card.EFFORT, 'max'), event_id='c2')
        self.click(self.option(panel_card.PROVIDER, 'anthropic'), event_id='c3')
        self.assertEqual((self.settings().model, self.settings().effort), ('a1', 'max'))  # The budget it offers too.
        self.click({**self.option(panel_card.PROVIDER, 'kiro'), 'option': ''}, event_id='c4')  # A card drawn before's 默认.
        self.assertIn(GONE, self.text())
        self.assertEqual((self.settings().model, self.settings().effort), ('a1', 'max'))
        self.click(self.option(panel_card.REDACTION, 'on'), event_id='c5')
        self.assertTrue(self.settings().redaction)
        self.assertTrue(self.cards[-1]['header']['subtitle']['content'].endswith('管家 A1'))
        self.click({**self.option(panel_card.PROVIDER, 'kiro'), 'option': 'gone'}, event_id='c6')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.outcomes(), ['main_panel'] * 6)
        # Without a model that can redact, 脱敏 cannot be turned on (it stays shown while on, to turn it off).
        for entry in choices.values():
            entry['redaction'] = False
        self.click(self.option(panel_card.REDACTION, 'off'), event_id='c7')
        self.assertFalse(self.settings().redaction)
        self.assertEqual(self.values(panel_card.REDACTION), [])

    def test_without_claude_code_the_rows_show_the_harness_it_runs_on_and_a_model_picked_saves_it(self):
        """T664: no Harness named and no Claude Code: the card draws the butler on Codex with Codex's own model (a model
        saved with no Harness named is Claude Code's); a model picked there saves Codex with it."""
        def entry(harness, title, model):
            return {'harness': harness, 'model': model, 'title': title, 'display_name': model.upper(),
                    'levels': ['low', 'high'], 'bridge_tools': True}
        choices = {'codex:g1': entry('codex', 'Codex', 'g1'), 'codex:g2': entry('codex', 'Codex', 'g2'),
                   'agy:n1': entry('agy', 'Antigravity', 'n1')}
        self.enterContext(mock.patch.object(panel, 'available_models', lambda topics, ctx: choices))
        apps = self.app.topics.apps
        self.enterContext(mock.patch.dict(apps, {name: app for name, app in apps.items() if name != 'claude'}, clear=True))
        settings.update(self.ctx.state_dir, model='claude-opus-x', effort='max')
        self.ask()
        self.assertEqual([o for o, _ in self.rows(panel_card.MODEL)], ['g1', 'g2'])
        subtitle = self.cards[-1]['header']['subtitle']['content']
        self.assertIn('管家 Codex', subtitle)  # Codex's own default, not Claude Code's model.
        self.assertNotIn('claude-opus-x', json.dumps(self.cards[-1]).lower())
        self.click(self.option(panel_card.MODEL, 'g2'), event_id='m1')
        current = self.settings()
        self.assertEqual((current.harness, current.model), ('codex', 'g2'))

    def test_the_butlers_harness_and_workspace_rows(self):
        """T614 (the owner, 2026-11-21): 管家 chooses its Harness as a new topic does, among those that can run it
        (agy, Codex beside Claude Code); another Harness takes its first model and a budget by name (T615). With 脱敏 on, one that cannot
        redact is not offered and the page says to turn 脱敏 off first. Its workspace is a row too."""
        def entry(harness, title, model, **more):
            return {'harness': harness, 'model': model, 'title': title, 'display_name': model.upper(),
                    'levels': ['low', 'high'], 'bridge_tools': True, **more}
        choices = {'claude:a1': entry('claude', 'Claude Code', 'a1', redaction=True, compaction=True),
                   'codex:g1': entry('codex', 'Codex', 'g1'), 'agy:n1': entry('agy', 'Antigravity', 'n1'),
                   'dsh:p1': {**entry('dsh', 'DeepSeek Harness', 'p1'), 'bridge_tools': False}}
        self.enterContext(mock.patch.object(panel, 'available_models', lambda topics, ctx: choices))
        settings.update(self.ctx.state_dir, model='a1', effort='high')
        self.ask()
        self.assertIn('压缩阈值', self.text())
        self.assertEqual(self.rows(panel_card.HARNESS), [('claude', 'Claude Code'), ('codex', 'Codex'), ('agy', 'Antigravity')])
        self.click(self.option(panel_card.HARNESS, 'agy'), event_id='h1')
        current = self.settings()
        # No 推荐 level (high is not xhigh or max): the highest.
        self.assertEqual((current.harness, current.butler_harness, current.model, current.effort), ('agy', 'agy', 'n1', 'high'))
        self.assertIn(panel.SAVED_NEW_SESSION, self.text())
        self.assertIn('管家 N1', self.cards[-1]['header']['subtitle']['content'])
        self.assertEqual([o for o, _ in self.rows(panel_card.MODEL)], ['n1'])
        self.click(self.option(panel_card.MODEL, 'n1'), event_id='h2')
        self.assertEqual(self.settings().model, 'n1')
        self.assertNotIn('压缩阈值', self.text())  # The Bridge does not compact agy's session.
        self.assertEqual(self.values(panel_card.REDACTION), [])  # agy cannot redact.
        self.click(self.option(panel_card.HARNESS, 'claude'), event_id='h3')
        self.assertEqual((self.settings().harness, self.settings().butler_harness), ('', 'claude'))
        # 脱敏 on: only a Harness that can redact, and both pages say why.
        self.click(self.option(panel_card.REDACTION, 'on'), event_id='h4')
        self.assertIn(panel_card.REDACT_FIRST, self.text())
        self.assertEqual(self.rows(panel_card.HARNESS), [('claude', 'Claude Code')])
        self.assertIn(panel_card.REDACT_FIRST, self.text())
        self.click({**self.option(panel_card.HARNESS, 'claude'), 'option': 'codex'}, event_id='h5')
        self.assertIn(GONE, self.text())
        self.assertEqual(self.settings().butler_harness, 'claude')
        self.assertEqual(self.values(panel_card.SPACE), [])  # Its workspace is no row of 管家 any more (v43).

    def test_a_click_in_更多_keeps_it_open_and_a_click_elsewhere_closes_it(self):
        """U4 (T898, docs/ui.md U15): the rows nobody changes are in their section's 更多, closed; a click on one of them
        redraws it open, a click on another row closed again; 脱敏 on stays out of it, in sight."""
        self.ask()
        token = self.values(panel_card.MODE)[0]['token']

        def folds():
            found = []

            def look(node):
                if isinstance(node, dict):
                    if node.get('tag') == 'collapsible_panel' and str(node.get('element_id', '')).startswith('more_'):
                        found.append((node['header']['title']['content'], node['expanded'],
                                      {v['kind'] for _, v in walk(node, [])}))
                    for child in node.values():
                        look(child)
                elif isinstance(node, list):
                    for child in node:
                        look(child)
            look(self.cards[-1])
            return found

        self.click({'kind': panel_card.MODE, 'token': token, 'revision': 1, 'option': 'agent'}, event_id='m1')
        (title, expanded, kinds), = folds()
        self.assertEqual((title, expanded), ('更多：供应商、脱敏、私聊顶层的消息', True))
        self.assertEqual(kinds, {panel_card.PROVIDER, panel_card.REDACTION, panel_card.MODE})
        self.click({'kind': panel_card.COMPACT, 'token': token, 'revision': 2, 'option': '0'}, event_id='m2')
        self.assertEqual([(t, e) for t, e, _ in folds()], [('更多：供应商、脱敏、私聊顶层的消息', False)])
        self.click({'kind': panel_card.REDACTION, 'token': token, 'revision': 3, 'option': 'on'}, event_id='m3')
        self.assertTrue(self.settings().redaction)
        (title, expanded, kinds), = folds()
        self.assertEqual((title, expanded, kinds), ('更多：供应商、私聊顶层的消息', True, {panel_card.PROVIDER, panel_card.MODE}))
        self.assertTrue(self.values(panel_card.REDACTION))  # 脱敏 on: a row of its own, in sight.

    def test_a_row_of_a_card_drawn_before_the_pages_lands_on_its_page(self):
        """A 设置 card drawn before 配置 (T619: its pages 管家, 新话题 and 模型, or the one 管家 tab before them, key BUTLER):
        a click on any of its rows redraws 配置 with that row's section open, and the record's tab
        follows."""
        self.ask()
        token = self.values(panel_card.MODE)[0]['token']
        hub = self.app.settings_hub
        path = hub.path(self.ctx, token)
        write_json(path, {**json.loads(path.read_text()), 'tab': 'topic'})  # Shown on 新话题, before 配置.
        old = {'kind': panel_card.TOPIC_REDACTION, 'token': token, 'revision': 1, 'option': 'on'}
        self.assertEqual(self.click(old, event_id='c1'), 1)
        self.assertTrue(self.settings().topic_defaults.redaction)
        self.assertEqual(self.cards[-1]['header']['title']['content'], '配置')
        record = json.loads(path.read_text())
        self.assertEqual((record['tab'], record['view']['open']), (panel_card.CONFIG, 'topic'))
        self.assertEqual([(value['tab'], button['type']) for button, value in walk(self.cards[-1], [])
                          if value['kind'] == TAB][:2], [('config', 'primary_filled'), ('repos', 'text')])
        self.click({'kind': panel_card.START, 'token': token, 'revision': 1, 'tier': 'readonly', 'on': False},
                   event_id='c2')
        self.assertTrue(self.settings().topic_defaults.redaction)  # Retired: changes nothing more.
        self.click({'kind': panel_card.UNTRUST, 'token': token, 'revision': 1, 'key': 'x|read'}, event_id='c2b')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '配置')
        self.click({'kind': panel_card.NOTES, 'token': token, 'revision': 1, 'workspaces': []}, event_id='c2c')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '配置')  # 保存用途 is offline: 配置, with its notice.
        self.assertIn(panel_card.OFFLINE_NOTICE, self.text())
        write_json(path, {**json.loads(path.read_text()), 'tab': 'main'})  # Shown on 管家, before 配置.
        self.click({'kind': panel_card.COMPACT, 'token': token, 'revision': 1, 'option': '0'}, event_id='c3')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '配置')
        self.assertEqual(json.loads(path.read_text())['view']['open'], 'butler')
        self.assertTrue(self.values(panel_card.COMPACT))
        write_json(path, {**json.loads(path.read_text()), 'tab': 'models'})  # Shown on 模型, before 配置.
        self.click({'kind': panel_card.TOPIC_HARNESS, 'token': token, 'revision': 1, 'option': 'codex'}, event_id='c4')
        self.assertEqual(self.settings().topic_defaults.harness, 'codex')
        self.assertEqual(self.cards[-1]['header']['title']['content'], '配置')
        self.assertEqual(json.loads(path.read_text())['tab'], panel_card.CONFIG)
        self.assertTrue(self.values(panel_card.TOPIC_HARNESS))
        # 展开 of a section no release has changes nothing.
        self.click({'kind': panel_card.SECTION, 'token': token, 'revision': 1, 'open': 'gone'}, event_id='c5')
        self.assertIn(GONE, self.text())
        self.assertTrue(self.values(panel_card.TOPIC_HARNESS))
        self.assertEqual(self.outcomes(), ['main_panel'] * 7)

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
        card = panel_card.butler_panel_card('a' * 24, {'revision': 1}, current, choices, names)
        self.assertLessEqual(element_count(card), CARD_NODES)
        shown = [value['option'] for _, value in walk(card, []) if value['kind'] == panel_card.TOPIC_MODEL]
        self.assertEqual(shown, [''] + [f'm{i}' for i in range(13)])

    def test_a_large_catalog_and_many_workspaces_keep_within_the_card_budget(self):
        choices = {f'codex:m{i}': {'harness': 'codex', 'model': f'm{i}', 'title': 'Codex', 'display_name': f'M{i}',
                                   'levels': ['low', 'high', 'xhigh']} for i in range(40)}
        choices.update({f'claude:c{i}': {'harness': 'claude', 'model': f'c{i}', 'title': 'Claude Code',
                                         'display_name': f'C{i}', 'levels': ['high', 'max']} for i in range(10)})
        names = [f'w{i}' for i in range(30)]
        current = settings.Settings(model='c9', topic_defaults=settings.TopicDefaults(harness='codex', model='m39'))
        card = panel_card.butler_panel_card('a' * 24, {'revision': 1}, current, choices, names)
        self.assertLessEqual(element_count(card), CARD_NODES)
        self.assertLessEqual(json_size(card), CARD_BYTES)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("<font color='grey'>模型</font>\u3000M39", text)  # The chosen model stays listed when the options are cut.
        self.assertNotIn('只列出前', text)  # The workspaces' lines are gone: nothing else gives way.


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

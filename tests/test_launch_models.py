"""What the launch card preselects and offers (app/preselection.py, app/model_usage.py, app/launch_models.py): model
order, MASO models, budgets and a topic's model settings. Split from test_launch."""

from __future__ import annotations

import dataclasses
import itertools
import json
import time
import unittest
from unittest import mock

from support import make_job
from support.fake_lark import FakeCardKit
from support.cards import (current_token, page_options, selects, settings_rows)
from support.git import commit_all, init_git_repo
from support import requires_extension
from support.launch_fixtures import MASO_MENU, write_maso_catalog
from support.processes import leftover_check
from support.world import TopicFixture, UnifiedCase

from covey.core import butler_settings
from covey.core.jobs import context_for_job
from covey.app.form_cards import save_record
from covey.app.launch import build_launch_card, card_token, chat_defaults_path, topic_budget
from covey.app.launch_submit import submit_launch_form
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.app.launch_models import available_models, harness_rank, run_choice
from test_launch import InChat
from covey.cards.launch_config import (BUDGET_ACTION, MODEL_ACTION, WORK_MODE_ACTION, launch_form_card)
from covey.core.menu_choice import AUTO_EFFORT_LABEL, effort_options, model_tiers, parse_budget, tier_pick
from covey.cards.launch_receipt import build_receipt_card
from covey.core.defaults import recommended_effort
from covey.core.jobs import find_job_record
from covey.core.model_settings import settings
from covey.core.roster import NAMES
from covey.core.state import write_private_text
from covey.core.topics import read_topic
from covey.core.workspaces import load_config

setUpModule, tearDownModule = leftover_check()


# gpt56_sol's presets in support/launch_fixtures.py's MASO_PROVIDERS, by budget.
GPT56_SOL = {'': 'gpt56_sol', 'high': 'gpt56_sol_reasoning_high', 'xhigh': 'gpt56_sol_reasoning_xhigh',
             'max': 'gpt56_sol_reasoning_max'}


class ModelOrderTests(UnifiedCase):
    """Every model select: a block per Harness in MENU_ORDER (Claude first), each in its catalog's order,
    after the preselected model and the ones the chat chose lately (RecentUseTests)."""

    @requires_extension()
    def test_the_tiers_list_the_harnesses_the_chosen_ones_providers_and_that_providers_models(self):
        # The owner, 2026-11-03: the launch card in tiers like 换配置, Harness → 供应商 → 模型.
        def entry(harness, title, provider, provider_title, name):
            return {'harness': harness, 'title': title, 'provider': provider, 'provider_title': provider_title,
                    'display_name': name}
        choices = {'claude:opus': entry('claude', 'Claude', '', 'Claude 自带', 'Opus 5.5'),
                   'claude:claude/claude-opus-5': entry('claude', 'Claude', '', 'Claude 自带', 'Opus 5'),
                   'codex:gpt-6-astra': entry('codex', 'Codex', '', 'Codex 自带', 'GPT-6 Astra'),
                   'codex:acme/gpt-6-astra': entry('codex', 'Codex', 'acme', 'Acme', 'GPT-6 Astra'),
                   'maso:old': {'label': 'MASO · Old'}}  # Saved before the Harness and provider fields.
        self.assertEqual(model_tiers(choices, 'claude:opus'), (
            'claude', '', {'claude': 'Claude', 'codex': 'Codex', 'maso': 'maso'}, {'': 'Claude 自带'},
            {'claude:opus': 'Opus 5.5', 'claude:claude/claude-opus-5': 'Opus 5'}))
        self.assertEqual(model_tiers(choices, 'codex:gpt-6-astra')[3:], (
            {'': 'Codex 自带', 'acme': 'Acme'}, {'codex:gpt-6-astra': 'GPT-6 Astra'}))
        self.assertEqual(model_tiers(choices, 'maso:old')[3:], ({'': '自带'}, {'maso:old': 'MASO · Old'}))
        self.assertEqual(tier_pick(choices, 'claude:opus', harness='codex'), 'codex:gpt-6-astra')
        self.assertEqual(tier_pick(choices, 'claude:opus', harness='maso'), 'maso:old')
        self.assertIsNone(tier_pick(choices, 'claude:opus', harness='claude'))  # Its own Harness: no change.
        self.assertEqual(tier_pick(choices, 'codex:gpt-6-astra', provider='acme'), 'codex:acme/gpt-6-astra')
        self.assertEqual(tier_pick(choices, 'codex:acme/gpt-6-astra', provider=''), 'codex:gpt-6-astra')
        self.assertIsNone(tier_pick(choices, 'claude:opus', harness='gone'))
        # 换配置 prefers the topic's own model where the choice leaves it, else the first; None when that is selected.
        self.assertEqual(tier_pick(choices, 'claude:opus', harness='claude', prefer='claude:claude/claude-opus-5'),
                         'claude:claude/claude-opus-5')
        self.assertIsNone(tier_pick(choices, 'claude:opus', harness='claude', prefer='codex:gpt-6-astra'))
        self.assertEqual(tier_pick(choices, 'claude:opus', harness='codex', prefer='claude:opus'), 'codex:gpt-6-astra')
        # A 思考预算 row's value is <revision>:<budget option>.
        budgets = {'levels': ['high', 'low']}
        self.assertEqual([parse_budget(option, 2, budgets) for option in ('2:high', '2:default', '1:high', '2:max')],
                         ['high', 'default', None, None])

    def setUp(self):
        super().setUp()
        claude = {'default': 'Default (recommended)', 'opus': 'Opus 5.5', 'fable': 'Fable 5.1'}
        write_private_text(self.ctx.state_dir / 'launch-models' / 'claude.json', json.dumps({
            'updated_at': time.time(), 'models': {m: {'display_name': n, 'levels': ['high', 'max'], 'default': ''}
                                                  for m, n in claude.items()}}))
        self.catalog = write_maso_catalog(self.ctx.state_dir)

    def model_select(self, saved=None):
        if saved:
            save_record(self.ctx.state_dir / 'launch-defaults.json', {'model_choice': saved, 'effort': 'top_minus_one'})
        card = build_launch_card(self.child.topics, self.target)
        # The model row, with the options its page lists.
        page = build_launch_card(self.child.topics, self.target, draft={'page': 'model'})
        element = {**selects(card)['model'], 'options': [{'value': value} for value, _ in page_options(page)]}
        return element, self.launch_form(card_token(card))

    @requires_extension()
    def test_blocks_keep_a_fixed_order_before_any_recorded_choice(self):
        self.remembered_launch_card()
        fixed = list(available_models(self.app.topics, self.ctx))
        for saved in ('claude:fable', 'maso:gpt55', 'dsh:dsh-model'):
            element, record = self.model_select(saved)
            self.assertEqual(element['initial_option'], saved)  # The last launch is still preselected,
            self.assertEqual(record['selected_model'], saved)
            listed = list(record['models'])
            self.assertEqual(listed, [saved] + [key for key in fixed if key != saved])  # listed first, the rest fixed.
            # The model select: the saved model's Harness and provider only, in that order; Claude Code's "Default
            # (recommended)" names no model, so the card lists it no more (docs/ui.md 13 U5), though the menu keeps it.
            same = [key for key in listed if key.split(':')[0] == saved.split(':')[0]
                    and record['models'][key].get('provider', '') == record['models'][saved].get('provider', '')
                    and key != 'claude:default']
            self.assertEqual([o['value'] for o in element['options']], same)
        harnesses = [key.split(':', 1)[0] for key in fixed]
        self.assertEqual([name for name, _ in itertools.groupby(harnesses)], sorted(NAMES, key=harness_rank))  # One block each.
        self.assertEqual([key for key in fixed if key.startswith('claude:')],
                         ['claude:default', 'claude:opus', 'claude:fable'])  # The catalog's order.

    @requires_extension()
    def test_the_new_topic_default_budget_is_preselected(self):
        # The 设置卡's 新话题默认 budget comes before the chat's last launch's (owner 2026-11-10), for its Harness only.
        self.remembered_launch_card()
        maso_before = self.model_select('maso:gpt55')[1]['selected_effort']
        self.assertNotEqual(self.model_select('claude:fable')[1]['selected_effort'], 'max')  # Its last launch's.
        # A Harness and a budget alone: the last launch's model of that Harness, with that budget.
        butler_settings.update(self.ctx.state_dir, topic_defaults=butler_settings.TopicDefaults(
            harness='claude', effort='max'))
        record = self.model_select('claude:fable')[1]
        self.assertEqual((record['selected_model'], record['selected_effort']), ('claude:fable', 'max'))
        # The last launch on another Harness: the page's Harness wins (T546), the budget stays its own elsewhere.
        self.assertEqual(self.model_select('maso:gpt55')[1]['selected_model'].split(':')[0], 'claude')
        butler_settings.update(self.ctx.state_dir, topic_defaults=butler_settings.TopicDefaults())
        self.assertEqual(self.model_select('maso:gpt55')[1]['selected_effort'], maso_before)
        # A model named on the page wins over the last launch (T546).
        butler_settings.update(self.ctx.state_dir, topic_defaults=butler_settings.TopicDefaults(
            harness='claude', model='opus', effort='max'))
        record = self.model_select('claude:fable')[1]
        self.assertEqual((record['selected_model'], record['selected_effort']), ('claude:opus', 'max'))
        self.assertEqual(topic_budget(self.ctx.state_dir), ('claude', 'max'))
        (self.ctx.state_dir / 'main-thread.json').write_text('{')  # Unreadable: as before.
        self.assertEqual(topic_budget(self.ctx.state_dir), ('', ''))

    @requires_extension()
    def test_a_saved_launch_naming_a_removed_providers_model_is_skipped(self):
        self.remembered_launch_card()
        for saved in ('claude:traex/claude-opus-5-5', 'claude:doubao/claude-opus-5-5', 'doubao:claude-opus-5-5'):
            with self.subTest(saved=saved):
                element, record = self.model_select(saved)
                self.assertIn(record['selected_model'], record['models'])
                self.assertNotEqual(record['selected_model'], saved)
                self.assertFalse([key for key in record['models'] if 'traex/' in key or 'doubao' in key])

    @requires_extension()
    def test_each_maso_model_is_listed_once_with_its_budgets(self):
        from covey_internal.maso.models import normalize_selection
        self.remembered_launch_card()
        element, record = self.model_select()
        maso = [(key, entry['label']) for key, entry in record['models'].items() if key.startswith('maso:')]
        self.assertEqual(maso, list(MASO_MENU.items()))
        sol = record['models']['maso:gpt56_sol']
        self.assertEqual((sol['model'], sol['levels'], sol['presets']), ('gpt56_sol', ['high', 'xhigh', 'max'], GPT56_SOL))

        def budgets(key):
            options, default = effort_options(record['models'][key])
            return [label for _, label in options], default
        self.assertEqual(budgets('maso:gpt56_sol'),
                         (['推荐 · xhigh', 'max', 'xhigh', 'high', '模型原生默认（不指定）'], 'top_minus_one'))
        self.assertEqual(budgets('maso:seed-stable'), (['high', 'medium', '模型原生默认（不指定）'], ''))
        self.assertEqual(budgets('maso:es1-grape-g61s_reasoning_xhigh'), (['推荐 · xhigh', 'max', 'xhigh'], 'top_minus_one'))
        self.assertEqual(budgets('maso:gpt-6-astra_reasoning_max'), ([AUTO_EFFORT_LABEL], 'default'))
        # Each budget runs the preset MASO itself maps it to (preset_for_effort), from any preset of the model.
        for key, entry in record['models'].items():
            for budget, preset in entry.get('presets', {}).items():
                self.assertEqual(run_choice(entry, budget), (preset, budget))
                for member in (p for b, p in entry['presets'].items() if b and budget):
                    self.assertEqual(normalize_selection(self.catalog, member, budget), (preset, budget), (key, member))
        with self.assertRaisesRegex(ValueError, '不支持'):
            run_choice(record['models']['maso:es1-grape-g61s_reasoning_xhigh'], '')  # No plain preset to run.

    @requires_extension()
    def test_a_saved_maso_preset_preselects_its_model_with_its_budget(self):
        self.remembered_launch_card()
        for saved, model, budget in (('maso:gpt56_sol_reasoning_xhigh', 'maso:gpt56_sol', 'xhigh'),
                                     ('maso:gpt56_sol_reasoning_max', 'maso:gpt56_sol', 'max'),
                                     ('maso:es1-grape-g61s_reasoning_max', 'maso:es1-grape-g61s_reasoning_xhigh', 'max'),
                                     # A model: the level its 推荐 names (docs/ui.md 13 U5).
                                     ('maso:gpt56_sol', 'maso:gpt56_sol', 'xhigh')):
            with self.subTest(saved):
                save_record(self.ctx.state_dir / 'launch-defaults.json', {'model_choice': saved, 'effort': 'top_minus_one'})
                card = build_launch_card(self.child.topics, self.target)
                self.assertEqual(selects(card)['model']['initial_option'], model)
                self.assertEqual(selects(card)['effort']['initial_option'], '1:' + budget)
                self.assertEqual(self.launch_form(card_token(card))['selected_effort'], budget)

    @requires_extension()
    def test_a_catalog_written_before_families_lists_each_preset(self):
        self.remembered_launch_card()
        write_maso_catalog(self.ctx.state_dir, families=False)  # Until the first refresh after the update.
        choices = available_models(self.app.topics, self.ctx)
        self.assertEqual(choices['maso:gpt56_sol_reasoning_xhigh']['label'], 'MASO · GPT-5.6 Sol · xhigh')
        self.assertNotIn('presets', choices['maso:gpt56_sol'])

    @requires_extension()
    def test_a_claude_gateway_model_reads_like_claudes_own_and_a_removed_providers_is_not_offered(self):
        write_private_text(self.ctx.state_dir / 'launch-models' / 'claude.json', json.dumps({'updated_at': time.time(), 'models': {
            'opus': {'display_name': 'Opus 5.5', 'levels': ['high'], 'default': ''},
            'claude/claude-opus-5': {'display_name': 'Claude Opus 5', 'levels': ['high'], 'default': ''},
            'claude/claude-fable-5': {'display_name': 'Claude Fable 5', 'levels': [], 'default': ''},
            # A catalog saved while TRAE and 豆包 were providers (removed 2026-11-05) still lists theirs.
            'traex/claude-opus-5-5': {'display_name': 'Opus 5.5', 'levels': ['xhigh'], 'default': 'xhigh'},
            'traex/hxy/claude-opus-5-5': {'display_name': '华星遥 · Opus 5.5', 'levels': ['xhigh'], 'default': 'xhigh'},
            'doubao/claude-opus-5-5': {'display_name': 'Opus 5.5', 'levels': ['xhigh'], 'default': 'xhigh'}}}))
        choices = available_models(self.app.topics, self.ctx)
        self.assertEqual({key: choice['label'] for key, choice in choices.items() if key.startswith('claude:')},
                         {'claude:opus': 'Claude Code · Opus 5.5', 'claude:claude/claude-opus-5': 'Claude Code · Opus 5',
                          'claude:claude/claude-fable-5': 'Claude Code · Fable 5'})
        self.assertEqual(choices['claude:claude/claude-opus-5']['model'], 'claude/claude-opus-5')  # Runs by its id.

    @requires_extension()
    @requires_extension()
    @requires_extension()
    def test_a_maso_display_name_of_its_own_is_kept(self):
        write_private_text(self.ctx.state_dir / 'launch-models' / 'maso.json', json.dumps({'updated_at': time.time(), 'models': {
            'gpt55_reasoning_high': {'display_name': 'GPT 5.5 高', 'levels': ['high'], 'default': 'high'}}}))
        self.assertEqual(available_models(self.app.topics, self.ctx)['maso:gpt55_reasoning_high']['label'],
                         'MASO · GPT 5.5 高')

    @requires_extension()
    def test_task_details_name_the_model_as_the_model_select_does(self):
        # S26 client run: 任务详情 said "模型：gpt-6-astra · 推理强度：max" under a receipt saying Codex · GPT-6-Astra.
        cards = self.app.apps['claude'].cards
        self.assertEqual(cards.model_label(self.ctx, 'retired'), '')  # Not listed: the card shows the id.
        cards.streaming = True
        job = make_job(model='fable', effort='high')
        with FakeCardKit({'code': 0, 'data': {'card_id': 'card-t'}}) as lark:
            type(cards).create_streaming_reply(cards, self.ctx, job, '已入队')
        self.assertEqual(job.model_label, 'Claude Code · Fable 5.1')  # Carried to the task runner with the job.
        card = json.loads(lark.calls[0].data['data'])
        # The 输入/输出 box's 输入 names it, as the select does (the "E" sample, 2026-11-08; the title bar's subtitle
        # did before, and the card has no title bar since 2026-11-22); 任务详情 no longer repeats it (2026-11-04 15:07).
        self.assertEqual(job.input_parts[:4], ['Claude Code', 'Claude 自带', 'Fable 5.1', '思考 high'])
        box = next(e for e in card['body']['elements'] if e.get('element_id') == 'task_io')
        words = json.dumps(box, ensure_ascii=False).replace('\\u2060', '').replace('\u2060', '').replace('\u00a0', ' ')
        self.assertIn('Claude Code', words)
        self.assertIn('Fable 5.1', words)
        self.assertNotIn('header', card)  # Its title row says the topic and the turn's title, not what it runs with.
        # Nor any 任务详情 with nothing else to say (the owner, 2026-11-09).
        self.assertFalse(any(e.get('element_id') == 'task_details' for e in card['body']['elements']))
        # A MASO budget preset is named by its model's one entry (S27: one entry per model).
        write_maso_catalog(self.ctx.state_dir)
        maso = self.app.apps['maso'].cards
        self.assertEqual(maso.model_label(self.ctx, 'gpt56_sol_reasoning_xhigh'), 'MASO · GPT-5.6 Sol')
        self.assertEqual(maso.model_label(self.ctx, 'gpt56_sol'), 'MASO · GPT-5.6 Sol')

    @requires_extension()
    def test_names_that_only_spell_out_the_id_read_like_the_other_entries(self):
        models = {'gpt-6-astra': 'gpt 6 astra', 'gpt-5.6-sol': 'GPT 5.6 sol', 'gpt-5.5-2026-04-24': 'GPT 5.5 2026-04-24',
                  'ali-deepseek-v4-pro': 'DeepSeek V4 Pro', 'ali-deepseek-v4.1-flash': 'ali-deepseek-v4.1-flash'}
        write_private_text(self.ctx.state_dir / 'launch-models' / 'dsh.json', json.dumps({
            'updated_at': time.time(), 'models': {m: {'display_name': n, 'levels': [], 'default': ''} for m, n in models.items()}}))
        choices = available_models(self.app.topics, self.ctx)
        self.assertEqual({key: entry['label'] for key, entry in choices.items() if key.startswith('dsh:')}, {
            'dsh:gpt-6-astra': 'DeepSeek Harness · GPT-6 Astra', 'dsh:gpt-5.6-sol': 'DeepSeek Harness · GPT-5.6 Sol',
            'dsh:gpt-5.5-2026-04-24': 'DeepSeek Harness · GPT-5.5 (2026-04-24)',
            'dsh:ali-deepseek-v4-pro': 'DeepSeek Harness · DeepSeek V4 Pro',  # A name of its own.
            'dsh:ali-deepseek-v4.1-flash': 'DeepSeek Harness · DeepSeek V4.1 Flash'})
        self.assertEqual(choices['claude:default']['label'], 'Claude Code · Default (recommended)')  # Claude's own text.


def save_defaults(state_dir, value, chat='chat'):
    """What a launch in ``chat`` leaves: the chat's own record and the most recent launch anywhere."""
    save_record(chat_defaults_path(state_dir, chat), {**value, 'chat_id': chat})
    save_record(state_dir / 'launch-defaults.json', value)


def settle(picker):
    """Run the card redraws launch card choices queued (model_picker.process), as the picker's workers would."""
    with FakeCardKit():
        while not picker.queue.empty():
            handler, *args = picker.queue.get_nowait()
            handler(*args)


class MasoLaunchTests(InChat, UnifiedCase):
    """开始任务 on a MASO model runs the preset of the chosen budget, with that budget; a card sent
    before one entry per model never starts another budget."""

    def setUp(self):
        super().setUp()
        self.picker = self.app.launch_model_picker
        self.picker.started = True  # Run queued work deterministically.

    def card(self, mid, saved, *, families=True):
        write_maso_catalog(self.ctx.state_dir, families=families)
        save_defaults(self.ctx.state_dir, {'model_choice': saved, 'effort': 'top_minus_one'}, self.chat)
        return self.remembered_launch_card(mid)[1]

    def click(self, token, kind, option, *, redraw=True):
        """A choice on the card the topic shows now; with ``redraw`` its redraw is run (it moves the card to a
        new token), else left queued."""
        self.picker.dispatch(self.ctx, {'operator_id': 'owner', 'chat_id': self.chat, 'message_id': 'mid',
                                        'action_value': {'kind': kind, 'token': current_token(self.ctx.state_dir, token)},
                                        'option': option})
        if redraw:
            settle(self.picker)

    def submit(self, token):
        submit_launch_form(self.child, self.target, {'chat_id': self.chat, 'message_id': 'mid',
                                                     'action_name': SUBMIT_PREFIX + token, 'form_value': {'path': ''}})

    def start(self, token):
        with FakeCardKit():
            self.submit(token)
        return self.app.runtime.jobs.queue.get_nowait()

    @requires_extension()
    def test_each_budget_starts_the_preset_of_that_budget(self):
        for n, (saved, option, pair) in enumerate((
                ('maso:gpt56_sol', None, ('gpt56_sol_reasoning_xhigh', 'xhigh')),  # 推荐 · xhigh.
                ('maso:gpt56_sol', '1:max', ('gpt56_sol_reasoning_max', 'max')),
                ('maso:gpt56_sol', '1:default', ('gpt56_sol', '')),  # The plain preset, as it is.
                ('maso:es1-grape-g61s_reasoning_xhigh', '1:max', ('es1-grape-g61s_reasoning_max', 'max')))):
            with self.subTest(saved=saved, option=option):
                token = self.card(f'task-{n}', saved)
                if option:
                    self.click(token, BUDGET_ACTION, option)
                job = self.start(token)
                self.assertEqual((job.harness_id, job.model, job.effort), ('maso', *pair))
                self.assertEqual((read_topic(self.target)['model'], read_topic(self.target)['effort']), pair)
                self.assertEqual(self.launch_form(current_token(self.ctx.state_dir, token))['selection']['model_label'], MASO_MENU[saved])
                self.assertEqual(json.loads((self.ctx.state_dir / 'launch-defaults.json').read_text())['model_choice'], saved)

    @requires_extension()
    def test_the_next_card_keeps_the_last_budget_with_the_last_model(self):
        # S27: after GPT-5.6 Sol at max, the next card kept the model but preselected 推荐 · xhigh.
        token = self.card('first', 'maso:gpt56_sol')
        self.click(token, BUDGET_ACTION, '1:max')
        self.start(token)
        card, token, _ = self.remembered_launch_card('next')
        self.assertEqual((selects(card)['model']['initial_option'], selects(card)['effort']['initial_option']),
                         ('maso:gpt56_sol', '1:max'))
        self.assertEqual(self.launch_form(token)['selected_effort'], 'max')
        self.assertEqual(self.start(token).model, 'gpt56_sol_reasoning_max')
        # Another model chosen on the card starts from the level its 推荐 names (docs/ui.md 13 U5).
        card, token, _ = self.remembered_launch_card('other')
        self.click(token, MODEL_ACTION, 'maso:es1-grape-g61s_reasoning_xhigh', redraw=False)
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            self.picker.process(ctx, path, revision)
        self.assertEqual(selects(lark.last.card)['effort']['initial_option'], '1:xhigh')
        # A saved budget the model doesn't offer, or a malformed one, falls back to that level.
        for n, last in enumerate(('default', ['max'], None)):
            with self.subTest(last=last):
                save_defaults(self.ctx.state_dir, {'model_choice': 'maso:es1-grape-g61s_reasoning_xhigh', 'effort': last})
                card = self.remembered_launch_card(f'fallback-{n}')[0]
                self.assertEqual(selects(card)['effort']['initial_option'], '1:xhigh')

    @requires_extension()
    def test_each_chat_keeps_its_own_last_budget(self):
        # S29: the budget comes with the model from the chat's own last launch.
        token = self.card('direct', 'maso:gpt56_sol')
        self.click(token, BUDGET_ACTION, '1:max')
        self.start(token)
        self.chat = 'oc_group'
        card, token, _ = self.remembered_launch_card('group')
        self.assertEqual(selects(card)['effort']['initial_option'], '1:max')  # The most recent launch anywhere.
        self.click(token, BUDGET_ACTION, '1:default')
        self.assertEqual(self.start(token).model, 'gpt56_sol')
        # 模型原生默认, tapped on a card sent before, still starts the model as it is; the next cards draw it as the level
        # 推荐 names, since they list no 模型原生默认 (docs/ui.md 13 U5).
        for chat, budget in (('chat', '1:max'), ('oc_group', '1:xhigh'), ('oc_new', '1:xhigh')):
            with self.subTest(chat=chat):
                self.chat = chat
                card, token, _ = self.remembered_launch_card(chat + '-next')
                self.assertEqual((selects(card)['model']['initial_option'], selects(card)['effort']['initial_option']),
                                 ('maso:gpt56_sol', budget))
                self.assertEqual(self.launch_form(token)['selected_effort'], budget[2:])

    MODE_LABELS = ['Focus（推荐）', 'Adaptive（派子 Agent）']  # The settings list's label says 工作模式.

    @requires_extension()
    def test_a_maso_model_offers_focus_or_adaptive_and_the_choice_reaches_the_job(self):
        token = self.card('focus', 'maso:gpt56_sol')
        card, token, _ = self.remembered_launch_card('focus-card')
        page = build_launch_card(self.child.topics, self.target, draft={'page': 'work_mode'})
        self.assertEqual((selects(card)['work_mode']['initial_option'], [text for _, text in page_options(page)]),
                         ('default_v1', self.MODE_LABELS))
        job = self.start(token)
        # Focus is written as before work modes: no field on the topic, the record or the defaults.
        self.assertEqual(job.work_mode, '')
        self.assertNotIn('work_mode', read_topic(self.target))
        self.assertNotIn('work_mode', find_job_record(self.ctx.state_dir, job.message_id))
        self.assertNotIn('work_mode', json.loads((self.ctx.state_dir / 'launch-defaults.json').read_text()))
        self.assertNotIn('Adaptive', self.launch_form(current_token(self.ctx.state_dir, token))['selection'].get('work_mode', ''))
        card, token, _ = self.remembered_launch_card('adaptive')
        for option in ('orch_v1', 'minimal', ''):  # MASO's other modes are not offered; a click with one is dropped.
            self.click(token, WORK_MODE_ACTION, option)
            self.assertEqual(self.launch_form(token)['selected_work_mode'], 'default_v1')
        self.click(token, WORK_MODE_ACTION, 'maso_adaptive')
        job = self.start(token)
        self.assertEqual(job.work_mode, 'maso_adaptive')
        self.assertEqual(context_for_job(self.target, job).work_mode, 'maso_adaptive')
        self.assertEqual(read_topic(self.target)['work_mode'], 'maso_adaptive')
        self.assertEqual(self.models_for(self.target).work_mode, 'maso_adaptive')
        self.assertEqual(find_job_record(self.ctx.state_dir, job.message_id)['work_mode'], 'maso_adaptive')
        receipt = json.dumps(build_receipt_card(self.launch_form(current_token(self.ctx.state_dir, token))['selection']), ensure_ascii=False)
        self.assertIn('MASO · GPT-5.6 Sol · 思考预算 xhigh · 工作模式 Adaptive', receipt)  # As the launch card names them.
        # The next card starts from the last mode; a model change (the whole card rebuilt) keeps it.
        card, token, _ = self.remembered_launch_card('next')
        self.assertEqual(selects(card)['work_mode']['initial_option'], 'maso_adaptive')
        self.click(token, MODEL_ACTION, 'maso:es1-grape-g61s_reasoning_xhigh', redraw=False)
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            self.picker.process(ctx, path, revision)
        self.assertEqual(selects(lark.last.card)['work_mode']['initial_option'], 'maso_adaptive')

    @requires_extension()
    def test_a_model_without_work_modes_shows_no_mode_select(self):
        choices = {'claude:m': {'label': 'Claude · M', 'levels': ['high', 'xhigh', 'max']},
                   'maso:m': {'label': 'MASO · M', 'levels': [], 'work_modes': ['default_v1', 'maso_adaptive']}}
        build = lambda model: launch_form_card('0' * 24, model=model, choices=choices, effort='', workspace='ws',
                                               names=['ws'], run_mode='direct', work_mode='maso_adaptive')
        self.assertNotIn('work_mode', selects(build('claude:m')))
        self.assertEqual(selects(build('maso:m'))['work_mode']['initial_option'], 'maso_adaptive')

    def models_for(self, target):
        """The context a new message of the topic is submitted with: the topic's model settings."""
        return self.app.topics.apps['maso'].models.configured_context(target)

    @requires_extension()
    def test_a_model_with_budget_presets_only_offers_no_native_default(self):
        token = self.card('grape', 'maso:es1-grape-g61s_reasoning_xhigh')
        self.click(token, BUDGET_ACTION, '1:default')
        self.assertEqual(self.launch_form(token)['selected_effort'], 'xhigh')  # 推荐's level by name (docs/ui.md 13 U5).

    @requires_extension()
    def test_a_card_sent_before_the_update_never_starts_another_budget(self):
        token = self.card('old', 'maso:gpt56_sol_reasoning_xhigh', families=False)
        record = self.launch_form(token)
        self.assertEqual((record['selected_model'], record['selected_effort']), ('maso:gpt56_sol_reasoning_xhigh', 'xhigh'))
        write_maso_catalog(self.ctx.state_dir)  # The update: one entry per model.
        before = read_topic(self.target)
        with FakeCardKit(), self.assertRaisesRegex(ValueError, '目录已更新'):
            self.submit(token)
        self.assertEqual(read_topic(self.target), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        # Its preset chosen again on that card: the card becomes the model's, with the preset's budget.
        self.click(token, MODEL_ACTION, 'maso:gpt56_sol_reasoning_xhigh', redraw=False)
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            self.picker.process(ctx, path, revision)
        self.assertEqual((selects(lark.last.card)['model']['initial_option'], selects(lark.last.card)['effort']['initial_option']),
                         ('maso:gpt56_sol', '1:xhigh'))
        job = self.start(token)  # Still from the old card: it starts its replacement.
        self.assertEqual((job.model, job.effort), ('gpt56_sol_reasoning_xhigh', 'xhigh'))


class RedactionLaunchTests(InChat, UnifiedCase):
    """敏感词脱敏 is retired (T947): the launch card has no 脱敏 row, and an older card's or form's ``redaction`` is ignored."""

    def setUp(self):
        super().setUp()
        self.picker = self.app.launch_model_picker
        self.picker.started = True

    def card(self, mid, model='claude:claude-model'):
        save_defaults(self.ctx.state_dir, {'model_choice': model, 'effort': 'high'}, self.chat)
        return self.remembered_launch_card(mid)

    def click(self, token, kind, option, *, redraw=True):
        """A choice on the card the topic shows now; with ``redraw`` its redraw is run (it moves the card to a
        new token), else left queued."""
        self.picker.dispatch(self.ctx, {'operator_id': 'owner', 'chat_id': self.chat, 'message_id': 'mid',
                                        'action_value': {'kind': kind, 'token': current_token(self.ctx.state_dir, token)},
                                        'option': option})
        if redraw:
            settle(self.picker)

    def rebuild(self, token, model):
        self.picker.dispatch(self.ctx, {'operator_id': 'owner', 'chat_id': self.chat, 'message_id': 'mid',
                                        'action_value': {'kind': MODEL_ACTION, 'token': current_token(self.ctx.state_dir, token)},
                                        'option': model})
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            self.picker.process(ctx, path, revision)
        return lark.last.card, card_token(lark.last.card)

    def start(self, token):
        with FakeCardKit():
            submit_launch_form(self.child, self.target, {'chat_id': self.chat, 'message_id': 'mid',
                                                         'action_name': SUBMIT_PREFIX + token, 'form_value': {'path': ''}})
        return self.app.runtime.jobs.queue.get_nowait()

    def test_the_new_topic_pages_run_mode_no_longer_reaches_the_card(self):
        # The home start (docs/configuration.md): no 工作区 or 运行方式 row, whatever the 新话题 page keeps; the topic starts in the home.
        butler_settings.update(self.ctx.state_dir, topic_defaults=butler_settings.TopicDefaults(run_mode='worktree'))
        card, token, _ = self.card('worktree')
        self.assertNotIn('run_mode', settings_rows(card))
        self.assertNotIn('workspace', settings_rows(card))
        self.assertNotIn('run_mode', self.launch_form(token)['form_defaults'])

    def submit(self, token):
        """开始任务 on the form ``token``; the cards CardKit was sent meanwhile."""
        with FakeCardKit() as lark:
            submit_launch_form(self.child, self.target, {'chat_id': self.chat, 'message_id': 'mid',
                                                         'action_name': SUBMIT_PREFIX + token, 'form_value': {'path': ''}})
        return lark

    def test_the_card_has_no_redaction_row_and_an_older_forms_choice_is_ignored(self):
        butler_settings.update(self.ctx.state_dir, topic_defaults=butler_settings.TopicDefaults(workspace=''))
        for mid, model in (('claude', 'claude:claude-model'), ('codex', 'codex:codex-model')):
            with self.subTest(mid):
                card, token, path = self.card(mid, model)
                self.assertNotIn('redaction', settings_rows(card))
                self.assertNotIn('脱敏', json.dumps(card, ensure_ascii=False))
                saved = self.launch_form(token)
                self.assertNotIn('selected_redaction', saved)
                # A card drawn before (it recorded the choice, and for a model that cannot redact the question): started as any.
                saved.update(selected_redaction='on', redaction_asked=model)
                path.write_text(json.dumps(saved))
                self.start(token)
                self.assertNotIn('redaction', read_topic(self.target))
                form = self.launch_form(current_token(self.ctx.state_dir, token))
                self.assertNotIn('redaction', form['selection'])
                self.assertNotIn('敏感词脱敏', json.dumps(build_receipt_card(form['selection']), ensure_ascii=False))

    def test_a_worktree_launch_keeps_its_place_in_its_pending_plan(self):
        init_git_repo(self.work)
        (self.work / 'README').write_text('x')
        commit_all(self.work, 'init')
        _, token, path = self.card('tree')
        # A card that names a workspace is an older card's (a butler's worktree launch): only it can ask for a worktree.
        self.click(token, 'bridge_launch_workspace', load_config(self.ctx.state_dir, self.work)['default'])
        self.click(token, 'bridge_launch_mode', 'worktree')
        with FakeCardKit(), mock.patch('covey.app.launch_submit.run_in_background'):
            submit_launch_form(self.child, self.target, {'chat_id': self.chat, 'message_id': 'mid',
                                                         'action_name': SUBMIT_PREFIX + token, 'form_value': {'path': ''}})
        self.assertEqual(self.launch_form(current_token(self.ctx.state_dir, token))['pending']['run_mode'], 'worktree')


class BudgetTests(unittest.TestCase):
    def test_rank_uses_intensity_not_provider_order(self):
        self.assertEqual(recommended_effort(["max", "low", "xhigh", "high", "medium"]), "xhigh")

    def test_an_orchestration_mode_on_top_is_not_counted(self):
        # Codex's ultra and Claude Code's ultracode are xhigh plus orchestration, not a deeper budget.
        self.assertEqual(recommended_effort(["ultra", "low", "max", "high", "xhigh", "medium"]), "xhigh")
        self.assertEqual(recommended_effort(["low", "xhigh", "max", "ultracode"]), "xhigh")
        for levels in (["max", "ultra"], ["high", "xhigh", "ultracode"]):
            with self.subTest(levels=levels), self.assertRaises(ValueError):
                recommended_effort(levels)

    def test_inadequate_or_unknown_tiers_fail_without_downgrade(self):
        for levels in ([], ["max"], ["high", "xhigh"], ["medium", "high"], ["low", "max"], ["xhigh", "max", "future"]):
            with self.subTest(levels=levels), self.assertRaises(ValueError):
                recommended_effort(levels)


CATALOG = {
    "model-a": {
        "display_name": "Model A",
        "default_reasoning_level": "high",
        "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}],
    },
    "model-b": {
        "display_name": "Model B",
        "default_reasoning_level": "low",
        "supported_reasoning_levels": [{"effort": "low"}],
    },
}


class ModelSettingsTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.harness.spec = dataclasses.replace(self.app.spec, name="codex")
        self.ctx = dataclasses.replace(self.ctx, options={**self.ctx.options, "harness_id": "codex"})
        self.patch = mock.patch("covey.core.model_settings.model_catalog", return_value=CATALOG)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def change(self, ctx, **values):
        # Settings cards are retired; existing saved settings are still honored.
        path = self.app.harness.model_settings_path(ctx.state_dir)
        saved = json.loads(path.read_text()) if path.exists() else {"model": ctx.model, "effort": ctx.effort}
        write_private_text(path, json.dumps({**saved, **values}))

    def test_settings_only_change_future_jobs_and_survive_reload(self):
        self.receive("om_a", "first")
        first = self.app.runtime.jobs.queue.queue[0]
        old = (first.model, first.effort)
        ctx = self.topic("om_a")
        self.change(ctx, model="model-a")
        self.change(ctx, effort="xhigh")
        self.receive("om_a2", "second", root="om_a")
        second = self.app.runtime.jobs.queue.queue[1]
        self.assertEqual((first.model, first.effort), old)
        self.assertEqual((second.model, second.effort), ("model-a", "xhigh"))
        effective = context_for_job(ctx, second)
        self.assertEqual((effective.model, effective.effort), ("model-a", "xhigh"))
        self.assertEqual(settings(self.app.models.configured_context(ctx)), ("model-a", "xhigh"))
        records = self.app.harness.topic_records(ctx, "oc_chat")
        saved = next(r for r in records if r["message_id"] == "om_a2")
        self.assertEqual(saved["model"], "model-a")
        restored = self.app.submission.job_from_record(ctx, saved)
        self.assertEqual(restored.effort, "xhigh")

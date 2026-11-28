"""The launch card itself (cards/launch_config.py, app/launch.py): how it is drawn, its picker clicks and redraws,
the receipt it becomes, and the forms that can no longer start anything (stale, replaced, never started, queued
clicks). Split from test_launch, which keeps starting a task and the per-chat defaults."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from support.cards import (current_token, form_fields, page_options, selects, settings_rows,
                           walk_elements as walk)
from support.processes import leftover_check
from support.world import LaunchCardCase, PickerCase, UnifiedCase

from support import requires_extension
from covey.cards.task_status import status_markdown
from covey.app.launch import (
    RETIRED_TEXT,
    card_token,
    publish_launch_cards,
    UNDELIVERED_STATUS,
    rebuild_launch_card,
    selected_effort,
)
from covey.app.launch_submit import submit_launch_form
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.app import launch_models
from covey.app.launch_models import available_models, refresh_catalog, refresh_once
from support.records import write_topic
from covey.cards.launch_config import PICKER_PREFIX
from covey.cards.launch_config import (HARNESS_ACTION, PAGE_ACTION,
                                                model_settings)
from covey.core.menu_choice import AUTO_EFFORT_LABEL, budget_options, effort_options, models_of
from covey.cards.launch_receipt import build_receipt_card
from covey.cards.elements import CARD_BYTES, CARD_NODES, element_count, json_size
from covey.cards.launch_config import SHORTENED_NOTE, launch_form_card
from covey.cards.switch_form import build_switch_card
from covey.core.jobs import BridgeReplyJob, find_job_record, never_started, upsert_job_record
from covey.core.topics import read_topic
from covey.core.workspaces import resolve_selection

setUpModule, tearDownModule = leftover_check()


class LaunchCardTests(LaunchCardCase):
    def test_one_compact_card_records_its_selects_and_submits_only_its_buttons(self):
        self.prepare_card()
        self.assertEqual(len(self.cards),1)
        record=self.record()
        self.assertEqual((record['status'],record['card_id'],record['message_id'],record['compact']),('ready','cid','mid',True))
        self.assertEqual(self.card['header']['icon']['token'],'start_outlined')  # The menu panels' title bar.
        # The summary, the settings list folded under 调整模型和预算 (no 工作区 or 运行方式: a topic starts in the home), then
        # the one button (no 其他目录… either).
        summary,adjust,form=self.card['body']['elements']
        self.assertIn('思考预算',json.dumps(summary,ensure_ascii=False))
        self.assertEqual((adjust['tag'],adjust['expanded'],adjust['header']['title']['content']),
                         ('collapsible_panel',False,"<font color='grey'>调整模型和预算</font>"))
        # A fold each, its options under it, all closed (no select: iOS's picker, 2026-11-04).
        self.assertEqual({(e['tag'],e['expanded']) for e in adjust['elements']},{('collapsible_panel',False)})
        rows=settings_rows(self.card)
        self.assertEqual(list(rows),['harness','provider','model','effort'])
        self.assertEqual({tap['token'] for row in rows.values() for tap in row['taps'].values()},{self.path.stem})
        self.assertNotIn('工作区',json.dumps(self.card,ensure_ascii=False))
        self.assertNotIn('运行方式',json.dumps(self.card,ensure_ascii=False))
        self.assertEqual(self.record()['form_defaults'],{'device':''})  # No workspace, no run mode: the home.
        self.assertNotIn('select_static',json.dumps(self.card))
        self.assertEqual(set(form_fields(self.card)),set())  # No path input: 其他目录… takes a pasted path.
        self.assertEqual(form['tag'],'form')
        self.assertEqual(self.card['header']['subtitle']['content'][:4],'话题 T')
        buttons=[e for e in walk(self.card['body']['elements']) if e.get('tag')=='button']
        self.assertEqual([(b['name'],b['text']['content']) for b in buttons],[(SUBMIT_PREFIX+self.path.stem,'开始任务')])

    def test_a_card_that_names_a_workspace_is_an_older_cards_and_keeps_its_rows(self):
        # What a butler's worktree launch and a card sent before the home start still are: the 工作区 and 运行方式 rows, 其他目录….
        self.prepare_card()
        other=self.root/'other'
        other.mkdir()
        name,_=resolve_selection(self.ctx.state_dir,self.work,str(other))
        self.choose('bridge_launch_workspace',name)
        token=current_token(self.ctx.state_dir,self.path.stem)
        card=self.tap({'kind':PAGE_ACTION,'token':token,'page':'run_mode','current':'direct'})
        rows=settings_rows(card)
        self.assertEqual(list(rows),['harness','provider','model','effort','workspace','run_mode'])
        self.assertEqual(rows['workspace']['current'],name)
        self.assertEqual(rows['run_mode']['current'],'direct')
        self.assertEqual({tap['kind'] for tap in rows['run_mode']['taps'].values()},{'bridge_launch_mode'})
        buttons=[e for e in walk(card['body']['elements']) if e.get('tag')=='button']
        self.assertEqual([b['text']['content'] for b in buttons],['其他目录…','开始任务'])

    def test_each_choice_is_recorded_and_redraws_the_card_unfolded_with_its_summary(self):
        self.prepare_card()
        other=self.root/'other'
        other.mkdir()
        name,_=resolve_selection(self.ctx.state_dir,self.work,str(other))
        self.choose('bridge_launch_workspace',name)
        self.choose('bridge_launch_mode','worktree')
        token=self.choose('bridge_launch_budget','1:low')
        self.assertEqual(self.choose('bridge_launch_mode','copy'),token)  # Not an option: nothing changes.
        record=self.record(token)
        self.assertEqual(record['form_defaults']['workspace'],name)
        self.assertEqual(record['form_defaults']['run_mode'],'worktree')
        self.assertEqual(selected_effort(record,None),'low')
        self.picker.dispatch(self.ctx,{**self.callback,'event_id':'budget-high',
                                       'action_value':{'kind':'bridge_launch_budget','token':token},'option':'1:high'})
        handler,*args=self.picker.queue.get_nowait()
        with FakeCardKit() as lark:
            handler(*args)
        token=current_token(self.ctx.state_dir,token)
        card=lark.last.card
        self.assertEqual(lark.last.path,'/open-apis/cardkit/v1/cards/cid')  # The whole card, again.
        summary,adjust,_=card['body']['elements']
        self.assertTrue(adjust['expanded'])  # Unfolded where the choice was made.
        self.assertIn(f'工作区 {name} · 独立 worktree',json.dumps(summary,ensure_ascii=False))
        self.assertIn('思考预算 high',json.dumps(summary,ensure_ascii=False))
        # A name gone from the list (still shown on an older card) is kept, so 开始任务 reports it.
        token=self.choose('bridge_launch_workspace','not-a-workspace',token)
        self.assertEqual(self.record(token)['form_defaults']['workspace'],'not-a-workspace')
        with self.assertRaisesRegex(ValueError,'已从列表移除'):
            submit_launch_form(self.child,self.target,self.submit)  # The first card's click starts the latest.
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def tap(self, value):
        """A tap on a row or button whose value is ``value``, as the client sends it (no select option); the
        card the redraw puts in its place."""
        record = self.record(value['token'])
        with FakeCardKit() as lark:
            self.app.handle_card_action_event({'operator_id': 'owner', 'chat_id': record['chat_id'],
                                               'message_id': record['message_id'], 'event_id': json.dumps(value),
                                               'action_value': value}, self.ctx)
            while not self.picker.queue.empty():
                handler, *args = self.picker.queue.get_nowait()
                handler(*args)
        return lark.last.card if lark.calls else None

    def test_a_rows_options_open_under_it_and_a_choice_redraws_once(self):
        # On iOS Feishu a select opens a full-screen picker whose 取消 lies under the status bar: the only way out
        # was the option already ticked (the owner's phone, 2026-11-03 23:37). A row's options open under it in the
        # client; an options page (84caa8a) cost two redraws a change, about a second each (2026-11-04 09:37).
        self.prepare_card()
        rows = settings_rows(self.card)
        current = self.record()['selected_effort']
        self.assertEqual(rows['effort']['current'], '1:' + current)
        # A choice records it and redraws the card once, unfolded, every row closed and showing its choice.
        card = self.tap(rows['effort']['taps']['1:max'])
        token = current_token(self.ctx.state_dir, self.path.stem)
        self.assertEqual(self.record(token)['selected_effort'], 'max')
        summary, adjust, _ = card['body']['elements']
        self.assertTrue(adjust['expanded'])
        self.assertFalse(any(row['open'] for row in settings_rows(card).values()))
        self.assertEqual(settings_rows(card)['effort']['current'], '1:max')
        self.assertIn('思考预算 max', json.dumps(summary, ensure_ascii=False))
        # The option already ticked changes nothing and draws nothing.
        self.assertIsNone(self.tap(settings_rows(card)['effort']['taps']['1:max']))
        # A row's tap from a card sent with options pages redraws this card with that row open; its ‹ 返回 closes it,
        # and a choice there, the current one too, closes it.
        card = self.tap({'kind': PAGE_ACTION, 'token': token, 'page': 'effort', 'current': '1:max'})
        token = current_token(self.ctx.state_dir, token)
        self.assertEqual([key for key, row in settings_rows(card).items() if row['open']], ['effort'])
        card = self.tap(settings_rows(card)['effort']['taps']['1:max'])
        token = current_token(self.ctx.state_dir, token)
        self.assertFalse(any(row['open'] for row in settings_rows(card).values()))
        self.tap({'kind': PAGE_ACTION, 'token': token, 'page': 'harness', 'current': ''})
        token = current_token(self.ctx.state_dir, token)
        card = self.tap({'kind': PAGE_ACTION, 'token': token, 'page': ''})
        self.assertFalse(any(row['open'] for row in settings_rows(card).values()))
        # 开始任务 on the first card starts the latest choice.
        submit_launch_form(self.child, self.target, {**self.submit, 'event_id': 'start'})
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual(job.effort, 'max')

    @requires_extension()
    def test_model_change_keeps_workspace_and_run_mode_and_resets_only_the_budget(self):
        self.prepare_card()
        other=self.root/'other'
        other.mkdir()
        name,_=resolve_selection(self.ctx.state_dir,self.work,str(other))
        self.choose('bridge_launch_workspace',name)
        self.choose('bridge_launch_mode','worktree')
        lark,token=self.change_model()
        # The whole card, never one element inside its form: Lark cannot submit that form.
        self.assertEqual(lark.last.path,'/open-apis/cardkit/v1/cards/cid')
        card=lark.last.card
        self.assertEqual(card_token(card),token)
        options=selects(card)
        self.assertEqual(options['model']['initial_option'],'maso:maso-model')
        self.assertEqual(options['workspace']['initial_option'],name)
        self.assertEqual(options['run_mode']['initial_option'],'worktree')
        self.assertTrue(options['effort']['initial_option'].endswith(':xhigh'))  # 推荐's level by name (docs/ui.md 13 U5).
        # A click the client still sends from the previous card starts its replacement, as recorded there.
        with self.assertRaisesRegex(ValueError,'不在 git 仓库'):
            submit_launch_form(self.child,self.target,self.submit)
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        token=self.choose('bridge_launch_budget','1:high',token)
        submit={**self.submit,'event_id':'again','action_name':SUBMIT_PREFIX+token}
        # The recorded run mode is what starts: a worktree needs a git repository.
        with self.assertRaisesRegex(ValueError,'不在 git 仓库'):
            submit_launch_form(self.child,self.target,submit)
        token=self.choose('bridge_launch_mode','direct',token)
        with FakeCardKit() as receipt:
            submit_launch_form(self.child,self.target,submit)
        job=self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id,job.effort,job.workdir),('maso','high',other.resolve()))
        self.assertEqual(len(receipt.calls),1)  # One card, one receipt.
        self.assertEqual(self.record(token)['selection']['workspace'],name)
        self.assertTrue(self.record(token)['receipt_published'])

    @requires_extension()
    def test_directory_return_keeps_the_chosen_model_and_budget(self):
        self.prepare_card()
        _,token=self.change_model()
        token=self.choose('bridge_launch_budget','1:high',token)
        token=self.choose('bridge_launch_mode','worktree',token)
        directory=self.app.directory_picker
        with FakeCardKit() as lark:
            directory.process(self.ctx,{**self.submit,'action_name':PICKER_PREFIX+token,'form_value':{'path':''}})
            directory.process(self.ctx,{**self.submit,'action_name':'','action_value':{
                'kind':'bridge_directory','token':token,'revision':self.record(token)['picker']['revision'],'verb':'cancel'}})
        card=lark.last.card
        options=selects(card)
        self.assertEqual(options['model']['initial_option'],'maso:maso-model')
        self.assertNotIn('run_mode',options)  # A new card has no 运行方式 row (a topic starts in the home).
        self.assertTrue(options['effort']['initial_option'].endswith(':high'))
        self.assertIn('开始任务',json.dumps(card,ensure_ascii=False))

    def test_receipt_is_one_line_with_the_full_path_folded(self):
        card=build_receipt_card({'model_label':'Claude · default','effort':'xhigh','workdir':str(self.work),
                                 'run_mode':'worktree','branch':'bridge/1-abc','topic':'T7'})
        head=card['header']
        self.assertEqual((head['title']['content'],head['subtitle']['content'],head['icon']['token']),
                         ('已提交','话题 T7 · Claude · default · 思考预算 xhigh','yes_outlined'))
        self.assertEqual([tag['text']['content'] for tag in head['text_tag_list']],['独立 worktree'])
        self.assertEqual(card['body']['elements'][0]['content'],'工作区 **project** · 分支 bridge/1-abc')
        self.assertTrue(card['body']['elements'][1]['elements'][0]['content'].endswith('/project'))  # Folded full path.
        self.assertLessEqual(len(card['body']['elements']),2)
        # The chat list's preview of every topic's first reply said 已提交 · 本话题配置已固定 (UI review P13).
        self.assertEqual(card['config']['summary']['content'],'已提交 · Claude default · 思考预算 xhigh · project')
        lazy=build_receipt_card({'model_label':'Codex · Acme · GPT-6 Astra','effort':'','workdir':'/','run_mode':'direct'})
        self.assertEqual(lazy['config']['summary']['content'],'已提交 · Codex Acme GPT-6 Astra · 思考预算 模型默认 · /')

    @requires_extension()
    def test_the_receipt_says_what_the_launch_card_said(self):
        # The owner, 2026-11-04 16:45 (T384): 已提交的也不全，连是否敏感都不知道 — only 开 was shown, so 关 read as unknown.
        base={'model_label':'Claude Code · Opus 5.5','effort':'max','workdir':str(self.work),'run_mode':'direct','topic':'T384'}
        def tags(selection):
            return [tag['text']['content'] for tag in build_receipt_card(selection)['header']['text_tag_list']]
        self.assertEqual(tags(base),['原目录'])
        # 敏感词脱敏 is retired (T947): a receipt from before that says 开 or 关 draws no tag for it.
        self.assertEqual(tags({**base,'redaction':True}),['原目录'])
        self.assertEqual(tags({**base,'redaction':False}),['原目录'])
        maso=build_receipt_card({**base,'model_label':'MASO · GPT-6 Astra','work_modes':True})
        self.assertEqual(maso['header']['subtitle']['content'],'话题 T384 · MASO · GPT-6 Astra · 思考预算 max · 工作模式 Focus')
        adaptive=build_receipt_card({**base,'work_mode':'maso_adaptive','work_modes':True})
        self.assertTrue(adaptive['header']['subtitle']['content'].endswith('思考预算 max · 工作模式 Adaptive'))

    def test_a_start_before_the_card_is_registered_is_refused(self):
        self.prepare_card()
        record=self.record();record['status']='publishing';self.path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'配置正在准备'):
            submit_launch_form(self.child,self.target,self.submit)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_undelivered_card_cannot_start(self):
        self.receive('task','pending');child,target=self.route('task')
        with FakeCardKit(None) as lark:
            publish_launch_cards(child.topics, child.voice, target,'chat','task')
        lark.send.assert_not_called()
        path=next((self.ctx.state_dir/'launch-forms').glob('*.json'))
        self.assertEqual(json.loads(path.read_text())['status'],'delivery_failed')
        self.assertTrue(self.app.runtime.jobs.queue.empty())
        # The record says why nothing shows (live #1774 kept 等待选择工作区 for a card nobody saw).
        job=find_job_record(self.ctx.state_dir,'task')
        self.assertEqual((job['status'],job['status_message']),('waiting_workspace',UNDELIVERED_STATUS))
        self.assertTrue(job['launch_delivery_error'])
        self.assertTrue(any('launch card not delivered' in line for line in self.logs))
        # The topic's next message shows a launch card again.
        shown=child.submission.selection_card.call_count
        self.receive('later','more',root='task')
        self.assertEqual(child.submission.selection_card.call_count,shown+1)
        # Once a card arrives, the earlier message can start from it: no longer marked undelivered.
        with FakeCardKit({'data':{'card_id':'c2'}},sent=('om_later',False)):
            publish_launch_cards(child.topics, child.voice, target,'chat','later')
        job=find_job_record(self.ctx.state_dir,'task')
        self.assertEqual((job['status'],job['status_message'],job['launch_delivery_error']),
                         ('waiting_workspace','等待选择工作区，尚未执行',''))

    def retire(self, **changes):
        """Turn the published form into one an older release left behind: two cards, or before schema 3."""
        record=self.record()
        record.pop('compact',None)
        record.update(**changes)
        self.path.write_text(json.dumps(record))
        return record

    def test_every_control_on_a_retired_form_is_refused_and_changes_nothing(self):
        self.prepare_card()
        directory=self.app.directory_picker
        directory.started=True  # The test runs each queued click itself.
        original=self.path.read_text()
        for n,changes in enumerate(({'split_config':True,'config_card_id':'config-cid','config_message_id':'config-mid'},
                                    {'schema_version':2})):
            with self.subTest(changes=changes):
                self.path.write_text(original)
                before=self.retire(**changes)
                self.app.messenger.send_message.reset_mock()
                budget={**self.callback,'event_id':f'b{n}','option':'1:low',
                        'action_value':{'kind':'bridge_launch_budget','token':self.path.stem}}
                with FakeCardKit() as lark:
                    for controller,event in ((self.picker,budget),(self.picker,{**self.callback,'event_id':f'm{n}'}),
                                             (directory,{**self.submit,'event_id':f'd{n}','action_name':PICKER_PREFIX+self.path.stem})):
                        controller.dispatch(self.ctx,event)
                        handler,*args=controller.queue.get_nowait()
                        handler(*args)
                    with self.assertRaisesRegex(ValueError,RETIRED_TEXT.rstrip('。')):
                        submit_launch_form(self.child,self.target,{**self.submit,'event_id':f's{n}'})
                self.assertEqual(lark.calls, [])
                replies=self.app.messenger.send_message.call_args_list
                self.assertEqual([(c.args[2],c.kwargs['reply_to_message_id']) for c in replies],[(RETIRED_TEXT,'task')]*3)
                self.assertEqual(len({c.args[3] for c in replies}),3)  # One reply per click.
                self.assertEqual(self.record(),before)
                self.assertEqual(find_job_record(self.ctx.state_dir,'task')['status'],'waiting_workspace')
                self.assertTrue(self.app.runtime.jobs.queue.empty())
                self.assertTrue(self.picker.queue.empty() and directory.queue.empty())

    def test_finished_forms_from_before_schema_3_keep_their_answers(self):
        self.prepare_card()
        record=self.retire(schema_version=2)
        # A repeated 开始任务 on a finished form stays a no-op, a replaced one is refused.
        record['status']='done'
        self.path.write_text(json.dumps(record))
        self.assertTrue(submit_launch_form(self.child,self.target,{**self.submit,'event_id':'e3'}))
        record['status']='replaced'
        self.path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,RETIRED_TEXT.rstrip('。')):
            submit_launch_form(self.child,self.target,{**self.submit,'event_id':'e4'})
        self.assertTrue(self.app.runtime.jobs.queue.empty())


def budget_setting(choice, revision, effort=''):
    """The 思考预算 row a model's choice gives, values ``revision``:budget as on the card."""
    settings = model_settings({'m:x': choice}, 'm:x', kinds=('h', 'p', 'm', 'b', 'w'), revision=revision, effort=effort)
    return next(setting for setting in settings if setting.key == 'effort')


class LaunchConfigTests(UnifiedCase):

    def prepare(self):
        self.card, self.token, self.path = self.remembered_launch_card()
        self.picker = self.app.launch_model_picker
        self.picker.started = True  # Run queued work deterministically.
        self.click = {'operator_id': 'owner', 'chat_id': 'chat', 'message_id': 'mid',
                      'action_value': {'kind': 'bridge_launch_model', 'token': self.token},
                      'option': 'maso:maso-model'}
        self.submit = {'chat_id': 'chat', 'message_id': 'mid', 'action_name': 'bridge_launch_' + self.token,
                       'form_value': {'workspace': self.work.name, 'effort': '1:top_minus_one', 'path': '', 'run_mode': 'direct'}}

    def record(self):
        return self.launch_form(self.token)

    def process(self, result={'code': 0}):
        _, ctx, path, revision = self.picker.queue.get_nowait()
        with FakeCardKit(result) as lark:
            self.picker.process(ctx, path, revision)
        return lark

    def current(self):
        token = current_token(self.ctx.state_dir, self.token)
        return token, self.launch_form(token)

    @requires_extension()
    def test_model_change_replaces_the_whole_card_and_the_old_form_starts_the_replacement(self):
        self.prepare()
        before = read_topic(self.target)
        self.picker.dispatch(self.ctx, self.click)
        self.assertTrue(self.record()['config_pending'])
        with mock.patch('covey.app.launch_submit.SWITCH_WAIT', 0.2), self.assertRaisesRegex(ValueError, '正在更新'):
            submit_launch_form(self.child, self.target, self.submit)  # The switch never finished.
        lark = self.process()
        # The whole card, never one element inside its form: Lark cannot submit that form.
        self.assertEqual(lark.last.path, '/open-apis/cardkit/v1/cards/cid')
        card = lark.last.card
        self.assertEqual(selects(card)['model']['initial_option'], 'maso:maso-model')
        self.assertEqual(read_topic(self.target), before)
        self.assertEqual(self.record()['status'], 'replaced')
        token, record = self.current()
        self.assertNotEqual(token, self.token)
        self.assertEqual(self.record()['replaced_by'], token)
        self.assertEqual((record['status'], record['selected_model'], record['card_id'], record['message_id']),
                         ('ready', 'maso:maso-model', 'cid', 'mid'))
        # The client still shows the old card: its 开始任务 starts the replacement, with the new model.
        with FakeCardKit():
            submit_launch_form(self.child, self.target, self.submit)
        job = self.app.runtime.jobs.queue.get_nowait()
        self.assertEqual((job.harness_id, job.effort), ('maso', 'xhigh'))
        self.assertEqual(json.loads((self.ctx.state_dir / 'launch-forms' / (token + '.json')).read_text())['status'], 'done')
        with FakeCardKit():
            submit_launch_form(self.child, self.target, self.submit)  # A repeated click starts nothing more.
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    @requires_extension()
    def test_a_harness_choice_opens_its_first_model_and_the_model_select_lists_only_its_models(self):
        self.prepare()
        click = {**self.click, 'action_value': {'kind': HARNESS_ACTION, 'token': self.token}, 'option': 'maso'}
        self.picker.dispatch(self.ctx, click)
        card = self.process().last.card
        chosen = selects(card)
        self.assertEqual((chosen['harness']['initial_option'], chosen['model']['initial_option']), ('maso', 'maso:maso-model'))
        token, record = self.current()
        self.assertEqual(record['selected_model'], 'maso:maso-model')
        # The Harness it shows chosen again changes nothing.
        self.picker.dispatch(self.ctx, {**click, 'action_value': {'kind': HARNESS_ACTION, 'token': token}})
        self.assertTrue(self.picker.queue.empty())
        # Its model page lists that Harness's models only.
        models = page_options(card, 'model')
        self.assertTrue(models and all(value.startswith('maso:') for value, _ in models))

    @requires_extension()
    def test_a_click_during_the_switch_waits_for_it_and_starts_the_new_model(self):
        self.prepare()
        self.picker.dispatch(self.ctx, self.click)
        finisher = threading.Timer(0.3, self.process)
        finisher.start()
        with FakeCardKit():
            submit_launch_form(self.child, self.target, self.submit)
        finisher.join()
        self.assertEqual(self.app.runtime.jobs.queue.get_nowait().harness_id, 'maso')

    @requires_extension()
    def test_fast_changes_coalesce_old_work_and_keep_latest_model(self):
        self.prepare()
        self.picker.dispatch(self.ctx, self.click)
        self.picker.dispatch(self.ctx, {**self.click, 'option': 'claude:claude-model'})
        self.assertEqual(self.process().calls, [])
        self.assertEqual(len(self.process().calls), 1)
        _, record = self.current()
        self.assertEqual(record['selected_model'], 'claude:claude-model')
        self.assertEqual(selected_effort(record, '1:top_minus_one'), 'xhigh')  # The level 推荐 names (docs/ui.md 13 U5).

    def test_unknown_source_owner_model_and_started_topics_cannot_mutate(self):
        self.prepare()
        before = self.record()
        for change in ({'operator_id': 'stranger'}, {'chat_id': 'other'}, {'message_id': 'other'},
                       {'option': 'maso:gone'}):
            self.picker.dispatch(self.ctx, {**self.click, **change})
            self.assertEqual(self.record(), before)
        topic = read_topic(self.target)
        topic['workspace_locked'] = True
        write_topic(self.target, topic)
        self.picker.dispatch(self.ctx, self.click)
        self.assertEqual(self.record(), before)

    @requires_extension()
    def test_uncertain_write_stays_pending_until_explicit_reselection(self):
        self.prepare()
        self.picker.dispatch(self.ctx, self.click)
        self.process(None)
        self.assertEqual(self.record()['status'], 'ready')
        with self.assertRaises(ValueError):
            selected_effort(self.record(), '2:top_minus_one')
        self.picker.dispatch(self.ctx, self.click)
        self.process()
        self.assertEqual(self.record()['sequence'], 2)
        self.assertEqual(self.record()['status'], 'replaced')
        _, record = self.current()
        self.assertEqual((record['sequence'], record['config_pending'] if 'config_pending' in record else False), (2, False))
        statuses = sorted(json.loads(p.read_text())['status'] for p in (self.ctx.state_dir / 'launch-forms').glob('*.json'))
        self.assertEqual(statuses, ['ready', 'replaced', 'unpublished'])  # The failed replacement cannot start.

    def test_removed_model_and_changed_levels_fail_before_route_or_queue(self):
        self.prepare()
        before = read_topic(self.target)
        name, model = self.record()['selected_model'].split(':', 1)  # Whichever model the card preselected.
        path = self.ctx.state_dir / 'launch-models' / (name + '.json')
        cached = json.loads(path.read_text())
        for models in ({}, {model: {'display_name': 'C', 'levels': ['low'], 'default': ''}}):
            path.write_text(json.dumps({**cached, 'models': models}))
            with self.assertRaisesRegex(ValueError, '目录已更新'):
                submit_launch_form(self.child, self.target, self.submit)
            self.assertEqual(read_topic(self.target), before)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_an_old_card_on_a_removed_providers_model_starts_nothing(self):
        # A card sent while TRAE and 豆包 were providers (removed 2026-11-05), or while 豆包 was a Harness (its
        # "doubao:" route key), never starts a task, on that model or silently on another.
        self.prepare()
        before = read_topic(self.target)
        path = self.ctx.state_dir / 'launch-forms' / (self.token + '.json')
        for key in ('claude:traex/claude-opus-5-5', 'claude:doubao/claude-opus-5-5', 'doubao:claude-opus-5-5'):
            with self.subTest(key=key):
                record = self.record()
                record['models'][key] = {'harness': key.split(':')[0], 'model': key.split(':', 1)[1], 'label': 'Opus 5.5',
                                         'levels': ['xhigh', 'max'], 'default': 'xhigh'}
                path.write_text(json.dumps({**record, 'selected_model': key}))
                with self.assertRaisesRegex(ValueError, '目录已更新'):
                    submit_launch_form(self.child, self.target, self.submit)
                self.assertEqual(read_topic(self.target), before)
                self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_expired_catalog_does_not_resurrect_native_static_cache(self):
        for path in (self.ctx.state_dir / 'launch-models').glob('*.json'):
            data = json.loads(path.read_text())
            data['updated_at'] = time.time() - 901
            path.write_text(json.dumps(data))
        self.assertEqual(available_models(self.app.topics, self.ctx), {})

    def test_a_refresh_without_an_answer_keeps_the_last_catalog_an_hour(self):
        # 2026-11-03 20:26–20:42 at load ~420: the Claude probe and the MASO helper timed out three times running and
        # both Harnesses dropped off the launch card.
        path = self.ctx.state_dir / 'launch-models' / 'claude.json'
        claude = lambda: {k for k in available_models(self.app.topics, self.ctx) if k.startswith('claude:')}
        offered, app = claude(), self.app.topics.apps['claude']
        self.assertTrue(offered)
        path.write_text(json.dumps({**json.loads(path.read_text()), 'updated_at': time.time() - 901}))
        with mock.patch.object(launch_models, 'refresh_catalog', side_effect=ValueError('模型目录暂不可用')):
            refresh_once(self.app.topics, app, self.ctx)
        self.assertFalse(claude())  # An answer without models removes them, as before.
        with mock.patch.object(launch_models, 'refresh_catalog', side_effect=RuntimeError('Claude 模型目录查询超时')):
            refresh_once(self.app.topics, app, self.ctx)
        self.assertEqual(claude(), offered)
        self.assertTrue(any('error=RuntimeError detail=Claude 模型目录查询超时' in line for line in self.logs))
        path.write_text(json.dumps({**json.loads(path.read_text()), 'updated_at': time.time() - 3601}))
        self.assertFalse(claude())

    def test_an_unexpected_refresh_error_is_logged_and_the_refresh_goes_on(self):
        # Anything else used to end the refresh thread: the Harness left the launch card until a restart.
        app = self.app.topics.apps['codex']
        error = AttributeError("'list' object has no attribute 'get'")
        with mock.patch.object(launch_models, 'refresh_catalog', side_effect=error):
            refresh_once(self.app.topics, app, self.ctx)
        self.assertTrue(any('launch catalog unavailable harness=codex error=AttributeError '
                            "detail='list' object has no attribute 'get'" in line for line in self.logs), self.logs)

    def test_a_failure_out_of_refresh_once_ends_that_thread_without_a_retry(self):
        # refresh_once's handler can raise: noting the cached catalog writes to disk (a full disk, a NAS fault). The
        # catalog threads are not workers: no "worker ... failed" line and no refresh again 5 s later.
        ended, logged, died = threading.Event(), [], []

        def log(line):
            logged.append(line)
            ended.set()

        def hook(args):
            died.append((args.exc_type, args.thread.name))
            ended.set()
        runtime = SimpleNamespace(shutdown=threading.Event(), log=log)
        self.addCleanup(runtime.shutdown.set)
        topics = SimpleNamespace(runtime=runtime, apps={'codex': self.app.topics.apps['codex']})
        with mock.patch.object(launch_models, 'refresh_catalog', side_effect=RuntimeError('no answer')) as refresh, \
                mock.patch.object(launch_models, 'note_unanswered', side_effect=OSError('disk full')), \
                mock.patch.object(threading, 'excepthook', hook):
            launch_models.start_catalog_refresh(topics)
            self.assertTrue(ended.wait(60))
        self.assertEqual(logged, [])
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(len(died), 1)
        self.assertIs(died[0][0], OSError)
        self.assertRegex(died[0][1], r'^Thread-\d+ \(refresh\)$')

    def test_a_codex_models_cache_of_another_shape_is_no_catalog(self):
        from covey.core.model_settings import model_catalog
        home = self.root / 'codex-home'
        home.mkdir()
        row = {'slug': 'gpt-x', 'visibility': 'list'}
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(home)}):
            odd = {'slug': ['gpt-x'], 'visibility': 'list'}
            for cache in ([row], {'models': {'gpt-x': row}}, {'models': [row['slug'], None, odd]}):
                (home / 'models_cache.json').write_text(json.dumps(cache))
                self.assertEqual(model_catalog(), {}, cache)
            (home / 'models_cache.json').write_text(json.dumps({'models': [row, 'gpt-y']}))
            self.assertEqual(model_catalog(), {'gpt-x': row})

    def test_account_catalog_uses_codex_home_without_reading_config_or_auth(self):
        # Moved from test_magpie.py when the Magpie provider went (2026-11-10).
        from covey.core.model_settings import model_catalog
        home = self.root / 'account-home'
        configured = home / 'configured'
        configured.mkdir(parents=True)
        (home / '.codex').mkdir()
        for directory, slug in ((configured, 'configured-model'), (home / '.codex', 'wrong-home-model')):
            (directory / 'models_cache.json').write_text(json.dumps({'models': [
                {'slug': slug, 'visibility': 'list'}, {'slug': 'hidden', 'visibility': 'hide'}]}))
        with mock.patch.dict(os.environ, {'HOME': str(home), 'CODEX_HOME': str(configured)}):
            self.assertEqual(set(model_catalog()), {'configured-model'})
        with mock.patch.dict(os.environ, {'HOME': str(home), 'CODEX_HOME': ''}):
            self.assertEqual(set(model_catalog()), {'wrong-home-model'})

    @requires_extension()
    def test_orange_models_are_not_offered_on_any_route(self):
        row = {'display_name': 'M', 'levels': [], 'default': ''}
        lists = {'maso': ['es1_orange_o48_thinking_max', 'gpt-6-astra']}
        for name, models in lists.items():
            (self.ctx.state_dir / 'launch-models' / (name + '.json')).write_text(
                json.dumps({'updated_at': time.time(), 'models': {m: row for m in models}}))
        choices = set(available_models(self.app.topics, self.ctx))
        self.assertIn('maso:gpt-6-astra', choices)
        self.assertNotIn('maso:es1_orange_o48_thinking_max', choices)

    def test_claude_offers_opus_5_and_up_and_sonnet_and_haiku_by_their_aliases(self):
        row = {'display_name': 'M', 'levels': [], 'default': ''}
        # A gateway's own ids (AsterGate, 2026-11-06) and prefixed ones (claude/…) are filtered alike.
        kept = ['default', 'opus', 'fable', 'claude-fable-5', 'sonnet', 'haiku', 'claude-opus-5', 'claude-opus-5-5',
                'claude/claude-opus-5']
        hidden = ['claude-sonnet-5', 'claude-sonnet-5-5', 'claude-opus-4-8', 'claude-sonnet-4-6',
                  'claude-opus-4-5-20251101', 'claude-haiku-4-5-20251001', 'claude-haiku-5-5',
                  'claude/claude-sonnet-5', 'claude/claude-opus-4-8', 'claude/claude-haiku-4-5']
        (self.ctx.state_dir / 'launch-models' / 'claude.json').write_text(
            json.dumps({'updated_at': time.time(), 'models': {m: row for m in kept + hidden}}))
        choices = [key for key in available_models(self.app.topics, self.ctx) if key.startswith('claude:')]
        self.assertEqual(choices, ['claude:' + m for m in kept])

    def test_the_launch_menu_lists_sonnet_and_haiku_5_5_after_opus_and_fable_with_their_budgets(self):
        # The live catalog's order (2026-11-10): the aliases, then the gateway's ids.
        levels = ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode']
        names = {'default': 'Default (recommended)', 'opus': 'Opus 5.5', 'fable': 'Fable 5.1', 'sonnet': 'Sonnet 5.5',
                 'haiku': 'Haiku 5.5', 'claude-haiku-4-5-20251001': 'Haiku 4.5', 'claude-opus-4-8': 'Opus 4.8',
                 'claude-opus-5': 'Opus 5', 'claude-sonnet-4-6': 'Sonnet 4.6', 'claude-sonnet-5': 'Sonnet 5'}
        (self.ctx.state_dir / 'launch-models' / 'claude.json').write_text(json.dumps({'updated_at': time.time(), 'models': {
            model: {'display_name': name, 'levels': levels, 'default': ''} for model, name in names.items()}}))
        choices = {key: row for key, row in available_models(self.app.topics, self.ctx).items()
                   if key.startswith('claude:')}
        self.assertEqual([row['display_name'] for row in choices.values()],
                         ['Default (recommended)', 'Opus 5.5', 'Fable 5.1', 'Sonnet 5.5', 'Haiku 5.5', 'Opus 5'])
        self.assertEqual(choices['claude:haiku']['levels'], levels)
        self.assertIn('max', dict(budget_options(choices['claude:haiku'])[0]))  # Its 思考预算 row offers them.
        # The 设置 card's model rows reuse the launch card's: they list them too.
        self.assertEqual(list(models_of(available_models(self.app.topics, self.ctx), 'claude').values()),
                         ['Default (recommended)', 'Opus 5.5', 'Fable 5.1', 'Sonnet 5.5', 'Haiku 5.5', 'Opus 5'])

    @requires_extension()
    def test_selecting_removed_model_cannot_start_the_previous_model(self):
        self.prepare()
        path = self.ctx.state_dir / 'launch-models/maso.json'
        path.write_text(json.dumps({'updated_at': time.time(), 'models': {}}))
        self.picker.dispatch(self.ctx, self.click)
        self.assertTrue(self.record()['config_pending'])
        with mock.patch('covey.app.launch_submit.SWITCH_WAIT', 0.2), self.assertRaises(ValueError):
            submit_launch_form(self.child, self.target, self.submit)
        self.assertTrue(self.app.runtime.jobs.queue.empty())

    def test_model_specific_options_and_explicit_low_tier_override(self):
        self.assertEqual(effort_options({'levels': []}), ([('default', AUTO_EFFORT_LABEL)], 'default'))
        for levels in (['low', 'medium', 'high'], ['high', 'max']):
            setting = budget_setting({'levels': levels}, 7)
            self.assertEqual((setting.current, setting.text), ('7:', '未选'))
            self.assertNotIn('7:top_minus_one', dict(setting.options))
        # The levels by name, the one 推荐 names preselected; no 推荐 or 模型原生默认 (docs/ui.md 13 U5).
        setting = budget_setting({'levels': ['max', 'xhigh', 'high', 'medium', 'low']}, 7)
        self.assertEqual(setting.current, '7:xhigh')
        self.assertEqual([value for value, _ in setting.options], ['7:max', '7:xhigh', '7:high', '7:medium', '7:low'])
        # 推荐 kept by a topic or tapped on a card sent before stays, drawn as it was.
        setting = budget_setting({'levels': ['max', 'xhigh', 'high', 'medium', 'low']}, 7, effort='top_minus_one')
        self.assertEqual((setting.current, setting.options[-1]), ('7:top_minus_one', ('7:top_minus_one', '推荐 xhigh')))

    def test_a_single_budget_is_preselected(self):
        setting = budget_setting({'levels': ['max']}, 7)
        self.assertEqual(setting.current, '7:max')
        self.assertEqual([value for value, _ in setting.options], ['7:max'])  # No 模型原生默认 (docs/ui.md 13 U5).

    def test_maso_presets_offer_only_their_own_budget(self):

        def levels(*providers):
            return [{'effort': p.rsplit('_', 1)[1], 'provider': p} for p in providers]
        family = ('grape_high', 'grape_xhigh', 'grape_max')
        catalog = {p: {'display_name': p, 'supported_reasoning_levels': levels(*family),
                       'default_reasoning_level': p.rsplit('_', 1)[1]} for p in family}
        # Budgets without a provider are a model's own (Codex, Claude): all are kept.
        catalog['native'] = {'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}]}
        backend = SimpleNamespace(refresh_model_catalog=lambda ctx: catalog,
                                  recommended_reasoning_effort=lambda ctx, model, levels: '')
        clean = refresh_catalog(SimpleNamespace(backend=backend, spec=SimpleNamespace(name='maso')), self.ctx)
        self.assertEqual({p: clean[p]['levels'] for p in family},
                         {'grape_high': ['high'], 'grape_xhigh': ['xhigh'], 'grape_max': ['max']})
        self.assertEqual(clean['native']['levels'], ['high', 'max'])
        self.assertEqual(effort_options(clean['grape_max'])[1], 'max')


class RebuildLaunchCardTests(UnifiedCase):

    def prepare(self):
        _, self.token, self.path = self.remembered_launch_card(sequence=3)

    def record(self, token=None):
        return self.launch_form(token or self.token)

    def rebuild(self, result, **options):
        seen = {}

        def put(profile, method, path, data, **kwargs):
            # The new form is already registered on the card when the update goes out.
            token = card_token(json.loads(data['card']['data']))
            seen.update(token=token, method=method, path=path, data=data, registered=self.record(token))
            return result

        with FakeCardKit(side_effect=put):
            ok = rebuild_launch_card(self.app.topics, self.ctx, self.path, self.record(),
                                     {'model': 'maso:maso-model', 'workspace': self.work.name}, kind='test', **options)
        return ok, seen

    @requires_extension()
    def test_the_whole_card_is_replaced_and_the_old_form_follows_the_new_one(self):
        self.prepare()
        ok, seen = self.rebuild({'code': 0}, notice='已选择工作区')
        self.assertTrue(ok)
        self.assertEqual((seen['method'], seen['path']), ('PUT', '/open-apis/cardkit/v1/cards/cid'))
        self.assertNotIn('element', seen['data'])  # Never one element inside the form.
        self.assertEqual((seen['registered']['status'], seen['registered']['card_id'], seen['registered']['message_id'],
                          seen['registered']['sequence']), ('ready', 'cid', 'mid', 4))
        self.assertEqual(seen['data']['sequence'], 4)
        self.assertIn('已选择工作区', seen['data']['card']['data'])
        old = self.record()
        self.assertEqual((old['status'], old['replaced_by'], old['sequence']), ('replaced', seen['token'], 4))
        self.assertEqual(self.record(seen['token'])['selected_model'], 'maso:maso-model')

    def test_an_unconfirmed_update_fails_closed_and_never_reuses_its_sequence(self):
        self.prepare()
        ok, seen = self.rebuild(None)
        self.assertFalse(ok)
        self.assertEqual(self.record(seen['token'])['status'], 'unpublished')
        old = self.record()
        self.assertEqual((old['status'], old['sequence']), ('ready', 4))
        self.assertNotIn('replaced_by', old)
        _, again = self.rebuild({'code': 0})
        self.assertEqual(again['data']['sequence'], 5)

    def test_options_keep_the_old_status_and_leave_an_unconfirmed_form_startable(self):
        self.prepare()
        record = self.record()
        record['status'] = 'worktree_failed'
        self.path.write_text(json.dumps(record))
        ok, seen = self.rebuild({'code': 0}, status=None, follow=False)
        self.assertTrue(ok)
        self.assertEqual(self.record()['status'], 'worktree_failed')
        self.assertNotIn('replaced_by', self.record())
        ok, seen = self.rebuild(None, follow=False, fail_closed=False)
        self.assertFalse(ok)
        self.assertEqual(self.record(seen['token'])['status'], 'ready')


class StaleLaunchTests(UnifiedCase):
    """Daily maintenance closes a launch card whose messages all waited a week (app/stale.py)."""

    def close(self, *keys):
        from covey.app.stale import close_stale_records
        with FakeCardKit() as put:
            closed = close_stale_records(self.app, self.ctx, {'stale_jobs': list(keys), 'stale_questions': []})
        return closed, put

    def test_the_whole_card_shows_the_note_and_its_messages_never_start(self):
        from covey.core.stale import STALE_NOTE
        _, token, _ = self.remembered_launch_card(sequence=3)
        closed, put = self.close('task')
        self.assertEqual(closed, ['launch card of 1 waiting job(s)'])
        method, path, data = put.last.method, put.last.path, put.last.data
        self.assertEqual((method, path, data['sequence']), ('PUT', '/open-apis/cardkit/v1/cards/cid', 4))
        self.assertIn(STALE_NOTE, data['card']['data'])
        self.assertEqual(self.launch_form(token)['status'], 'closed')  # 开始任务 on it starts nothing.
        job = find_job_record(self.ctx.state_dir, 'task')
        self.assertEqual((job['status'], job['status_message']), ('cancelled', STALE_NOTE))
        self.assertIs(job['never_started'], True)
        self.assertEqual(self.close('task')[0], [])  # Once.

    def test_a_message_whose_launch_card_was_not_delivered_is_closed_with_its_own_note(self):
        from covey.core.stale import UNDELIVERED_LAUNCH_NOTE
        upsert_job_record(self.ctx.state_dir, 'lost', job_id=8, status='waiting_workspace', topic_id='t',
                          chat_id='chat', backend='codex', launch_delivery_error='2200 Something went wrong')
        self.assertEqual(self.close('lost')[0], ['job #8 waiting_workspace'])
        job = find_job_record(self.ctx.state_dir, 'lost')
        self.assertEqual((job['status'], job['status_message']), ('cancelled', UNDELIVERED_LAUNCH_NOTE))
        # Live #1774 predates launch_delivery_error: maintenance's 1-day rule picked it from its form.
        upsert_job_record(self.ctx.state_dir, 'old', job_id=9, status='waiting_workspace', topic_id='t2',
                          chat_id='chat', backend='codex')
        from covey.app.stale import close_stale_records
        with FakeCardKit():
            closed = close_stale_records(self.app, self.ctx, {'stale_jobs': ['old'], 'stale_questions': [],
                                                              'stale_undelivered': ['old']})
        self.assertEqual(closed, ['job #9 waiting_workspace'])
        self.assertEqual(find_job_record(self.ctx.state_dir, 'old')['status_message'], UNDELIVERED_LAUNCH_NOTE)

    def test_a_waiting_message_without_a_launch_card_is_closed_as_never_started(self):
        from covey.core.stale import STALE_NOTE
        upsert_job_record(self.ctx.state_dir, 'lone', job_id=7, status='waiting_workspace', topic_id='t',
                          chat_id='chat', backend='codex', timings={'received': 1.0})
        self.assertEqual(self.close('lone')[0], ['job #7 waiting_workspace'])
        job = find_job_record(self.ctx.state_dir, 'lone')
        self.assertEqual((job['status'], job['status_message'], job['never_started']), ('cancelled', STALE_NOTE, True))

    def test_a_waiting_job_registered_with_the_bridge_is_never_closed(self):
        from covey.core.stale import STALE_NOTE
        upsert_job_record(self.ctx.state_dir, 'asked', job_id=9, status='waiting_user', topic_id='t', chat_id='chat')
        job = BridgeReplyJob(job_id=9, user_text='x', chat_id='chat', message_id='asked', timeout=0)
        self.app.runtime.jobs.register(job)
        self.assertEqual(self.close('asked')[0], [])
        self.assertEqual(find_job_record(self.ctx.state_dir, 'asked')['status'], 'waiting_user')
        self.app.runtime.jobs.unregister(job)
        self.assertEqual(self.close('asked')[0], ['job #9 waiting_user'])
        self.assertEqual(find_job_record(self.ctx.state_dir, 'asked')['status_message'], STALE_NOTE)

    def test_a_newer_message_waiting_on_the_card_keeps_the_topic_open(self):
        _, token, _ = self.remembered_launch_card()
        self.receive('later', 'more', root='task')
        closed, put = self.close('task')
        self.assertEqual((closed, len(put.calls)), ([], 0))
        self.assertEqual(self.launch_form(token)['status'], 'ready')
        self.assertEqual(find_job_record(self.ctx.state_dir, 'task')['status'], 'waiting_workspace')

    def test_a_message_its_start_click_is_submitting_is_never_closed(self):
        """开始任务 marks its form done, then submits the waiting messages, under the topic's lock;
        the closer finds no open form then and waits for that lock (S21 a3/repro_launch_fallback.py)."""
        from covey.core.locks import topic_lock
        _, token, path = self.remembered_launch_card()
        held, release = threading.Event(), threading.Event()

        def click():
            with topic_lock(self.target):
                path.write_text(json.dumps({**self.launch_form(token), 'status': 'done'}))  # on_bound
                held.set()
                release.wait(10)
                upsert_job_record(self.ctx.state_dir, 'task', status='queued', status_message='已入队')

        clicker = threading.Thread(target=click)
        clicker.start()
        held.wait(5)
        result = {}
        closer = threading.Thread(target=lambda: result.update(closed=self.close('task')[0]))
        closer.start()
        closer.join(0.5)
        self.assertTrue(closer.is_alive())  # Waits for the click.
        release.set()
        clicker.join(5)
        closer.join(5)
        self.assertEqual(result['closed'], [])
        self.assertEqual(find_job_record(self.ctx.state_dir, 'task')['status'], 'queued')

    def test_a_job_that_asked_in_its_reply_gets_the_note_on_its_task_card(self):
        from covey.core.stale import STALE_NOTE
        app, target = self.route('root')
        name = next(key for key, value in self.app.apps.items() if value is app)
        upsert_job_record(self.ctx.state_dir, 'asked', job_id=9, status='waiting_user', topic_id=target.topic_id,
                          chat_id='chat', harness_id=name, card_id='tc')
        closed, put = self.close('asked')
        self.assertEqual(closed, ['job #9 waiting_user'])
        self.assertEqual((put.last.method, put.last.path), ('PATCH', '/open-apis/cardkit/v1/cards/tc/elements/activity'))
        self.assertEqual(json.loads(put.last.data['partial_element']), {'content': status_markdown(STALE_NOTE)})
        self.assertEqual(find_job_record(self.ctx.state_dir, 'asked')['status'], 'cancelled')

    def test_the_closer_knows_the_launch_form_states(self):
        from covey.core.statuses import WORKTREE_STATUS
        from covey.app.workspace_manager import MANAGING
        from covey.core import statuses
        self.assertEqual((statuses.OPEN_FORMS, statuses.WORKTREE_STATUS), ({'ready', MANAGING}, WORKTREE_STATUS))


class NeverStartedTests(unittest.TestCase):
    """Launch leftovers carry the default route's backend though nothing ran: per-backend counts skip
    them (REVIEW2: 22 such records were counted as codex cancellations). Shapes as on the live state."""

    def test_records_closed_before_a_run(self):
        launch = {'status': 'cancelled', 'backend': 'codex', 'timings': {'received': 1.0}, 'session_id': ''}
        self.assertTrue(never_started(launch))  # Before the marker existed.
        self.assertTrue(never_started({**launch, 'never_started': True}))
        self.assertTrue(never_started({**launch, 'status': 'waiting_workspace'}))
        merged = {'status': 'cancelled', 'timings': dict.fromkeys(('received', 'submitted', 'queued'), 1.0),
                  'merged_into_job_id': 3}
        self.assertTrue(never_started(merged))
        # Stopped while the worker prepared it (dispatched, backend never asked): nothing ran.
        self.assertTrue(never_started({**merged, 'timings': {**merged['timings'], 'dispatched': 2.0}}))

    def test_records_of_a_run_count(self):
        stages = ('received', 'submitted', 'queued', 'dispatched', 'backend_requested', 'backend_started')
        stopped = {'status': 'cancelled', 'timings': dict.fromkeys(stages, 1.0), 'execution_status': 'cancelled',
                   'session_id': 's'}
        self.assertFalse(never_started(stopped))
        self.assertFalse(never_started({'status': 'cancelled', 'timings': {'received': 1.0, 'backend_requested': 2.0}}))
        self.assertFalse(never_started({'status': 'cancelled', 'timings': {'received': 1.0}, 'session_id': 's'}))
        self.assertFalse(never_started({'status': 'cancelled', 'timings': {}, 'execution_status': 'done'}))
        for status in ('done', 'failed', 'interrupted', 'queued', 'running'):
            self.assertFalse(never_started({'status': status, 'timings': {'received': 1.0}, 'never_started': True}))


def run_queued(controller):
    """Run every queued click here, in order, as a worker would."""
    while not controller.queue.empty():
        handler, *args = controller.queue.get_nowait()
        handler(*args)
        controller.queue.task_done()


class QueuedClickTests(PickerCase):

    def test_a_click_delivered_twice_through_the_queue_updates_the_card_once(self):
        self.prepare()
        self.picker.started = True  # The test runs the queued clicks itself.
        self.assertEqual(self.app._handle_card_action_event(self.open_event, self.ctx), 'directory')
        run_queued(self.picker)
        calls = self.api.call_count
        click = self.action('up')
        for _ in range(2):
            self.assertEqual(self.app._handle_card_action_event(click, self.ctx), 'directory')
        run_queued(self.picker)
        self.assertEqual(self.api.call_count, calls + 1)
        self.assertEqual(self.record()['picker']['path'], str(self.root))


def dicts(value):
    """Every dict in a card, at any depth."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from dicts(child)


class CatalogTooLongTests(unittest.TestCase):
    """A Harness whose provider serves more models than one card holds (a gateway's dozens of routes as Codex's own):
    the launch and 换配置 cards cut their 模型 row to fit instead of CardKit refusing them (300305)."""

    @staticmethod
    def choices(count):
        codex = {f'codex:route/m{i:02d}': {'harness': 'codex', 'title': 'Codex', 'provider': 'openai',
                                           'provider_title': 'Codex 自带', 'model': f'route/m{i:02d}',
                                           'display_name': f'Route Model {i:02d} · Gateway', 'label': f'Codex · m{i:02d}',
                                           'levels': ['low', 'medium', 'high', 'xhigh'], 'default': 'high'}
                 for i in range(count)}
        return {'claude:opus': {'harness': 'claude', 'title': 'Claude', 'provider': 'anthropic',
                                'provider_title': 'Claude 自带', 'model': 'opus', 'display_name': 'Opus 5.5',
                                'label': 'Claude · Opus 5.5', 'levels': ['high', 'xhigh']}, **codex}

    def form(self, count, selected, current):
        return {'token': 'f' * 24, 'topic': 'T9', 'workspace': 'project', 'config_revision': 3,
                'models': self.choices(count), 'selected_model': selected, 'selected_effort': 'high',
                'current': {'label': 'Codex · current', 'effort': 'high', 'key': current, 'budget': 'high',
                            'workspace': 'project', 'run_mode': 'direct'},
                'workspaces': ['project'], 'selected_workspace': 'project', 'selected_run_mode': 'direct'}

    @staticmethod
    def model_options(card):
        return [behavior['value']['option'] for item in dicts(card) if item.get('tag') == 'interactive_container'
                for behavior in item.get('behaviors', ())
                if behavior.get('value', {}).get('kind') in ('bridge_switch_model', 'bridge_launch_model')]

    def assert_fits(self, card):
        self.assertLessEqual(element_count(card), CARD_NODES)
        self.assertLessEqual(json_size(card), CARD_BYTES)

    def test_the_switch_card_of_a_long_catalog_fits_and_keeps_the_chosen_model_and_the_topics_own(self):
        card = build_switch_card(self.form(70, 'codex:route/m69', 'codex:route/m68'))
        self.assert_fits(card)
        options = self.model_options(card)
        self.assertLess(len(options), 70)
        self.assertEqual(options[:3], ['codex:route/m00', 'codex:route/m01', 'codex:route/m02'])
        self.assertIn('codex:route/m69', options)
        self.assertIn('codex:route/m68', options)
        self.assertIn(SHORTENED_NOTE.format(shown=len(options), total=70), json.dumps(card, ensure_ascii=False))

    def test_the_launch_card_of_a_long_catalog_fits_and_keeps_the_chosen_model(self):
        card = launch_form_card('l' * 24, model='codex:route/m69', choices=self.choices(70), effort='high',
                                workspace='project', names=['project'], run_mode='direct')
        self.assert_fits(card)
        options = self.model_options(card)
        self.assertLess(len(options), 70)
        self.assertIn('codex:route/m69', options)
        self.assertIn(SHORTENED_NOTE.format(shown=len(options), total=70), json.dumps(card, ensure_ascii=False))

    def test_a_catalog_that_fits_lists_every_model_and_says_nothing(self):
        card = build_switch_card(self.form(12, 'codex:route/m03', 'codex:route/m03'))
        self.assertEqual(len(self.model_options(card)), 12)
        self.assertNotIn('一张卡放不下', json.dumps(card, ensure_ascii=False))
        launch = launch_form_card('l' * 24, model='codex:route/m03', choices=self.choices(12), effort='high',
                                  workspace='project', names=['project'], run_mode='direct')
        self.assertEqual(len(self.model_options(launch)), 12)
        self.assertNotIn('一张卡放不下', json.dumps(launch, ensure_ascii=False))

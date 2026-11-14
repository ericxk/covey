"""The model selects in use order (app/model_usage.py): a chat's recent launches and switches put its last
used model first and its most used next, and give each model the budget it usually runs on."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import requires_extension
from covey.app import model_usage
from covey.app.model_usage import (WINDOW_DAYS, ensure, last_selection, ranked, recent, record, usage_path,
                                            usual_effort)
from covey.core.state import write_json

SOL = {'': 'gpt56_sol', 'high': 'gpt56_sol_reasoning_high', 'max': 'gpt56_sol_reasoning_max'}
# available_models' fixed order: Claude (its own models, then its gateway's), Codex, MASO, DeepSeek Harness.
CHOICES = {key: {'harness': key.split(':', 1)[0], 'model': key.split(':', 1)[1], 'label': key} for key in (
    'claude:opus', 'claude:fable', 'claude:claude/claude-opus-5', 'codex:gpt-6-astra', 'codex:acme/gpt-6-astra')}
CHOICES['maso:gpt56_sol'] = {'harness': 'maso', 'model': 'gpt56_sol', 'label': 'MASO · GPT-5.6 Sol',
                             'levels': ['high', 'max'], 'presets': SOL}
CHOICES['dsh:flash'] = {'harness': 'dsh', 'model': 'flash', 'label': 'DeepSeek Harness · Flash'}
DAY = 86400


def events(*selections, chat='chat', start=1000.0):
    """One selection a second from ``start``: (model, effort) pairs."""
    return [{'at': start + n, 'chat': chat, 'model': model, 'effort': effort, 'work_mode': '', 'kind': 'launch'}
            for n, (model, effort) in enumerate(selections)]


class OrderTests(unittest.TestCase):
    def test_a_selection_of_a_removed_providers_model_is_skipped(self):
        # TRAE and 豆包 (removed 2026-11-05; 豆包 also a Harness until 2026-11-03, "doubao:" keys) are on no menu.
        used = events(('codex:gpt-6-astra', 'max'), ('claude:traex/claude-opus-5-5', 'xhigh'),
                      ('doubao:claude-opus-5-5', 'xhigh'), ('claude:doubao/claude-opus-5-5', 'max'))
        self.assertEqual(last_selection(CHOICES, used), ('codex:gpt-6-astra', '', ''))
        self.assertEqual(list(ranked(CHOICES, used)), ['codex:gpt-6-astra'] + [k for k in CHOICES if k != 'codex:gpt-6-astra'])

    def test_without_selections_the_menu_keeps_its_fixed_order(self):
        self.assertEqual(list(ranked(CHOICES, [])), list(CHOICES))
        self.assertIsNone(last_selection(CHOICES, []))

    def test_the_preselected_comes_first_then_the_most_used_then_the_rest_in_fixed_order(self):
        used = events(('claude:opus', 'top_minus_one'), ('codex:gpt-6-astra', 'max'), ('claude:opus', 'max'),
                      ('codex:gpt-6-astra', 'max'), ('dsh:flash', ''), ('claude:opus', 'top_minus_one'),
                      ('codex:acme/gpt-6-astra', 'ultra'))
        menu = ranked(CHOICES, used, 'dsh:flash')
        self.assertEqual(list(menu), ['dsh:flash', 'claude:opus', 'codex:gpt-6-astra', 'codex:acme/gpt-6-astra',
                                      'claude:fable', 'claude:claude/claude-opus-5', 'maso:gpt56_sol'])
        self.assertEqual(menu['claude:opus'], CHOICES['claude:opus'])  # The entries themselves are untouched.
        # Nothing preselected: by use alone (a tie: the more recent); a preselected model nobody chose leads too.
        self.assertEqual(list(ranked(CHOICES, used))[:4],
                         ['claude:opus', 'codex:gpt-6-astra', 'codex:acme/gpt-6-astra', 'dsh:flash'])
        self.assertEqual(list(ranked(CHOICES, used, 'claude:fable'))[:2], ['claude:fable', 'claude:opus'])
        self.assertEqual(last_selection(CHOICES, used), ('codex:acme/gpt-6-astra', '', ''))

    def test_a_tie_goes_to_the_more_recent(self):
        used = events(('claude:fable', 'max'), ('dsh:flash', 'max'), ('claude:opus', 'max'))
        self.assertEqual(list(ranked(CHOICES, used))[:3], ['claude:opus', 'dsh:flash', 'claude:fable'])

    def test_a_model_no_longer_on_the_menu_is_passed_over(self):
        used = events(('claude:opus', 'max'), ('gone:claude-opus-5-5', 'max'), ('gone:claude-opus-5-5', 'max'))
        self.assertEqual(list(ranked(CHOICES, used))[0], 'claude:opus')
        self.assertEqual(last_selection(CHOICES, used), ('claude:opus', '', ''))

    def test_a_maso_preset_chosen_before_one_entry_per_model_counts_for_its_model(self):
        used = events(('claude:opus', 'max'), ('maso:gpt56_sol_reasoning_high', ''))
        self.assertEqual(list(ranked(CHOICES, used))[:2], ['maso:gpt56_sol', 'claude:opus'])
        self.assertEqual(last_selection(CHOICES, used), ('maso:gpt56_sol', 'high', ''))

    def test_the_last_selection_brings_its_work_mode(self):
        used = [{**events(('maso:gpt56_sol', 'high'))[0], 'work_mode': 'maso_adaptive'}]
        self.assertEqual(last_selection(CHOICES, used), ('maso:gpt56_sol', '', 'maso_adaptive'))


class BudgetTests(unittest.TestCase):
    OPTIONS = {'top_minus_one': '推荐 · xhigh', 'max': 'max', 'xhigh': 'xhigh', 'high': 'high', 'default': '默认'}

    def test_the_budget_chosen_most_often_with_the_model(self):
        used = events(('claude:opus', 'max'), ('claude:opus', 'top_minus_one'), ('claude:opus', 'max'),
                      ('codex:gpt-6-astra', 'high'), ('codex:gpt-6-astra', 'high'))
        self.assertEqual(usual_effort(used, 'claude:opus', self.OPTIONS), 'max')
        self.assertEqual(usual_effort(used, 'codex:gpt-6-astra', self.OPTIONS), 'high')
        self.assertEqual(usual_effort(used, 'claude:fable', self.OPTIONS), '')  # Never chosen: the card's default.

    def test_a_tie_goes_to_the_latest(self):
        used = events(('claude:opus', 'max'), ('claude:opus', 'top_minus_one'))
        self.assertEqual(usual_effort(used, 'claude:opus', self.OPTIONS), 'top_minus_one')

    def test_only_the_last_selections_count_and_only_budgets_the_model_still_offers(self):
        used = events(*[('claude:opus', 'high')] * 5 + [('claude:opus', 'max')] * 4
                      + [('claude:opus', 'top_minus_one')] * 2 + [('claude:opus', 'ultracode')] * 5)
        # Without ultracode, the last ten are high 4, max 4, 推荐 2: max is the later (all eleven: high 5).
        self.assertEqual(usual_effort(used, 'claude:opus', self.OPTIONS), 'max')
        self.assertEqual(usual_effort(used, 'claude:opus', {**self.OPTIONS, 'ultracode': 'ultracode'}), 'ultracode')


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        write_json(usage_path(self.state), {'events': []})  # No forms to read first.

    def saved(self):
        return json.loads(usage_path(self.state).read_text())['events']

    def test_each_chat_reads_its_own_and_a_chat_without_any_reads_every_chats(self):
        now = time.time()
        record(self.state, 'oc_private', 'claude:opus', 'max', '', 'launch', now=now - 2)
        record(self.state, 'oc_accept', 'dsh:flash', 'high', '', 'launch', now=now - 1)
        record(self.state, 'oc_private', 'codex:gpt-6-astra', 'top_minus_one', '', 'switch', now=now)
        self.assertEqual([e['model'] for e in recent(self.state, 'oc_private', now=now)], ['claude:opus', 'codex:gpt-6-astra'])
        self.assertEqual([e['model'] for e in recent(self.state, 'oc_accept', now=now)], ['dsh:flash'])
        self.assertEqual([e['model'] for e in recent(self.state, 'oc_new', now=now)],
                         ['claude:opus', 'dsh:flash', 'codex:gpt-6-astra'])
        self.assertEqual(self.saved()[-1], {'at': now, 'chat': 'oc_private', 'model': 'codex:gpt-6-astra',
                                            'effort': 'top_minus_one', 'work_mode': '', 'kind': 'switch', 'form': ''})

    def test_selections_older_than_the_window_are_dropped(self):
        now = time.time()
        record(self.state, 'chat', 'claude:opus', 'max', '', 'launch', now=now - WINDOW_DAYS * DAY - 60)
        record(self.state, 'chat', 'dsh:flash', 'high', '', 'launch', now=now - WINDOW_DAYS * DAY + 60)
        self.assertEqual([e['model'] for e in recent(self.state, 'chat', now=now)], ['dsh:flash'])
        record(self.state, 'chat', 'claude:fable', 'max', '', 'launch', now=now)
        self.assertEqual([e['model'] for e in self.saved()], ['dsh:flash', 'claude:fable'])

    def test_the_file_keeps_the_newest_selections(self):
        now = time.time()
        with mock.patch.object(model_usage, 'KEEP', 3):
            for n in range(5):
                record(self.state, 'chat', f'claude:m{n}', 'max', '', 'launch', now=now + n)
        self.assertEqual([e['model'] for e in self.saved()], ['claude:m2', 'claude:m3', 'claude:m4'])

    def test_a_damaged_file_reads_as_no_selections(self):
        now = time.time()
        usage_path(self.state).write_text(json.dumps({'events': [
            {'at': 'x', 'model': 'claude:opus'}, 3, {'at': now, 'model': None},
            {'at': now, 'chat': 'chat', 'model': 'claude:opus', 'effort': []}]}))
        self.assertEqual(recent(self.state, 'chat', now=now), [])
        usage_path(self.state).write_text(json.dumps({'events': {'at': now}}))
        self.assertEqual(recent(self.state, 'chat'), [])
        usage_path(self.state).write_text('not json')
        self.assertEqual(recent(self.state, 'chat'), [])


class FormHistoryTests(unittest.TestCase):
    """The release that brings model-usage.json starts from the launch and switch forms of the window."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def form(self, folder, name, at, **record):
        path = self.state / folder / (name + '.json')
        write_json(path, record)
        os.utime(path, (at, at))

    @requires_extension()
    def test_the_done_forms_of_the_window_in_the_order_they_finished(self):
        now = time.time()
        self.form('launch-forms', 'a', now - 30, status='done', chat_id='chat', selected_model='claude:opus',
                  selected_effort='max', selected_work_mode='')
        self.form('switch-forms', 'b', now - 20, status='done', chat_id='chat', selected_model='maso:gpt56_sol',
                  selected_effort='high', selected_work_mode='maso_adaptive')
        self.form('launch-forms', 'c', now - 10, status='done', chat_id='oc_group', selected_model='dsh:flash',
                  selected_effort='top_minus_one', selected_work_mode='default_v1')
        self.form('launch-forms', 'ready', now - 5, status='ready', chat_id='chat', selected_model='claude:fable')
        self.form('switch-forms', 'cancelled', now - 5, status='cancelled', chat_id='chat', selected_model='claude:fable')
        self.form('launch-forms', 'old', now - WINDOW_DAYS * DAY - 60, status='done', chat_id='chat',
                  selected_model='claude:fable')
        (self.state / 'launch-forms' / 'broken.json').write_text('{')
        self.assertTrue(ensure(self.state, now=now))
        saved = json.loads(usage_path(self.state).read_text())['events']
        self.assertEqual([(e['chat'], e['model'], e['effort'], e['work_mode'], e['kind']) for e in saved], [
            ('chat', 'claude:opus', 'max', '', 'launch'), ('chat', 'maso:gpt56_sol', 'high', 'maso_adaptive', 'switch'),
            ('oc_group', 'dsh:flash', 'top_minus_one', '', 'launch')])  # Focus is stored as "", like a topic's.
        self.assertFalse(ensure(self.state, now=now))  # Once: the file is the record from here.

    def test_a_selection_before_the_start_finished_reading_the_forms_keeps_them_and_counts_once(self):
        now = time.time()
        self.form('launch-forms', 'a', now - 30, status='done', chat_id='chat', selected_model='claude:opus',
                  selected_effort='max')
        self.form('launch-forms', 'b', now - 1, status='done', chat_id='chat', selected_model='dsh:flash',
                  selected_effort='high')
        # The launch of form b, recorded after it was done: the forms are read first, b counted once.
        record(self.state, 'chat', 'dsh:flash', 'high', '', 'launch', form='b', now=now)
        self.assertEqual([(e['model'], e['form'], e['at']) for e in recent(self.state, 'chat', now=now)],
                         [('claude:opus', 'a', now - 30), ('dsh:flash', 'b', now)])

    def test_start_reads_them_off_the_starting_thread_and_logs_it(self):
        self.form('launch-forms', 'a', time.time() - 30, status='done', chat_id='chat', selected_model='claude:opus')
        lines = []
        with mock.patch.object(model_usage.threading, 'Thread') as thread:
            model_usage.start(self.state, lines.append)
        thread.return_value.start.assert_called_once_with()
        thread.call_args.kwargs['target']()
        self.assertEqual(lines, ['model usage written from the forms: 1 selections'])
        thread.call_args.kwargs['target']()
        self.assertEqual(len(lines), 1)  # Written already: nothing to say.


if __name__ == '__main__':
    unittest.main()

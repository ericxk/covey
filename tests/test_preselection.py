"""What the launch card and the 换配置 card preselect (app/preselection.py), case by case: the binding, the
chat's last launch, its latest and usual selections, a rebuilt card's draft, and what the menu still offers."""
import unittest

from covey.app.preselection import launch_selection, switch_selection
from covey.core.defaults import RECOMMENDED

SOL = {'': 'gpt56_sol', 'high': 'gpt56_sol_reasoning_high', 'max': 'gpt56_sol_reasoning_max'}
# available_models' fixed order. Budget options: Opus top_minus_one/max/xhigh/high/default (default top_minus_one);
# GPT top_minus_one/high/low/default (top_minus_one); Sol max/high/default (none: ''); Flash default only.
CHOICES = {
    'claude:opus': {'harness': 'claude', 'model': 'opus', 'levels': ['high', 'xhigh', 'max'], 'recommended': 'xhigh',
                    'redaction': True},
    'codex:gpt': {'harness': 'codex', 'model': 'gpt', 'levels': ['low', 'high'], 'recommended': 'high'},
    'maso:gpt56_sol': {'harness': 'maso', 'model': 'gpt56_sol', 'levels': ['high', 'max'], 'recommended': '',
                       'presets': SOL, 'work_modes': ['focus', 'adaptive']},
    'dsh:flash': {'harness': 'dsh', 'model': 'flash'},
}
CONFIG = {'default': 'project', 'workspaces': {'project': '/absolute/project', 'other': '/absolute/other'},
          'recent': {'other': 5}}
NAMES = ['other', 'project']
WARNING = '⚠️ 工作区「gone」已从列表移除，请换一个工作区。'


def used(*selections, chat='chat'):
    """(model, effort, work_mode) selections, one a second, oldest first."""
    return [{'at': 1000.0 + n, 'chat': chat, 'model': model, 'effort': effort, 'work_mode': mode, 'kind': 'launch'}
            for n, (model, effort, mode) in enumerate(selections)]


class LaunchSelectionTests(unittest.TestCase):
    def check(self, cases):
        for name, kwargs, expected in cases:
            with self.subTest(name):
                args = {'model': 'codex:gpt', 'workspace': 'project', 'work_mode': '', 'fresh': False, **kwargs}
                choices, usage = args.pop('choices', CHOICES), args.pop('usage', [])
                got = launch_selection(choices, usage, CONFIG, **args)
                self.assertEqual({k: getattr(got, k) for k in expected if k != 'first'},
                                 {k: v for k, v in expected.items() if k != 'first'})
                if 'first' in expected:
                    self.assertEqual(next(iter(got.choices), None), expected['first'])
                self.assertEqual(set(got.choices), set(choices))

    def test_the_binding_and_the_chats_preferences(self):
        last = {'model_choice': 'maso:gpt56_sol', 'effort': 'max', 'work_mode': 'adaptive', 'workdir': '/absolute/other'}
        self.check([
            ('the binding alone', {},
             {'model': 'codex:gpt', 'effort': RECOMMENDED, 'work_mode': '', 'workspace': 'project', 'names': NAMES,
              'notice': '', 'first': 'claude:opus'}),
            ('a MASO preset binding is its model with that budget', {'model': 'maso:gpt56_sol_reasoning_max'},
             {'model': 'maso:gpt56_sol', 'effort': 'max', 'work_mode': 'focus'}),
            ('the last launch prefills a first binding', {'fresh': True, 'saved': last},
             {'model': 'maso:gpt56_sol', 'effort': 'max', 'work_mode': 'adaptive', 'workspace': 'other',
              'first': 'maso:gpt56_sol'}),
            ('a later binding keeps its own', {'saved': last, 'usage': used(('claude:opus', 'max', ''))},
             {'model': 'codex:gpt', 'effort': RECOMMENDED, 'workspace': 'project', 'first': 'claude:opus'}),
            ('a saved preset key brings its budget', {'fresh': True, 'saved': {'model_choice': 'maso:gpt56_sol_reasoning_high'}},
             {'model': 'maso:gpt56_sol', 'effort': 'high', 'work_mode': 'focus'}),
            ('a saved launch that is no dict is none', {'fresh': True, 'saved': ['maso:gpt56_sol']},
             {'model': 'codex:gpt', 'effort': RECOMMENDED, 'workspace': 'project', 'first': 'claude:opus'}),
            ('without a saved launch the latest selection', {'fresh': True, 'usage': used(
                ('codex:gpt', 'low', ''), ('maso:gpt56_sol_reasoning_high', 'high', 'adaptive'))},
             {'model': 'maso:gpt56_sol', 'effort': 'high', 'work_mode': 'adaptive', 'first': 'maso:gpt56_sol'}),
            ('a saved model off the menu still gives its workspace', {'fresh': True, 'usage': used(('claude:opus', 'max', '')),
                                                                       'saved': {'model_choice': 'codex:gone', 'workdir': '/absolute/other'}},
             {'model': 'claude:opus', 'effort': 'max', 'workspace': 'other', 'first': 'claude:opus'}),
            ('the usual budget before the last launch', {'fresh': True, 'saved': {**last, 'model_choice': 'codex:gpt'},
                                                         'usage': used(('codex:gpt', 'low', ''))},
             {'model': 'codex:gpt', 'effort': 'low', 'work_mode': ''}),
            ('the last launch budget when the chat has none', {'fresh': True, 'saved': {'model_choice': 'codex:gpt', 'effort': 'low'}},
             {'model': 'codex:gpt', 'effort': 'low'}),
            ('a budget the model does not offer falls to its default', {'fresh': True,
                                                                        'saved': {'model_choice': 'maso:gpt56_sol', 'effort': 'low'}},
             {'model': 'maso:gpt56_sol', 'effort': ''}),
        ])

    def test_a_rebuilt_cards_draft(self):
        self.check([
            ('the draft model and budget', {'draft': {'model': 'claude:opus', 'effort': 'high'}},
             {'model': 'claude:opus', 'effort': 'high', 'first': 'claude:opus'}),
            ('a draft preset key keeps its own budget', {'draft': {'model': 'maso:gpt56_sol_reasoning_max', 'effort': 'high'}},
             {'model': 'maso:gpt56_sol', 'effort': 'max'}),
            ('a draft budget alone', {'draft': {'effort': 'low'}}, {'model': 'codex:gpt', 'effort': 'low'}),
            ('a draft budget the model lacks', {'draft': {'effort': 'max'}}, {'model': 'codex:gpt', 'effort': RECOMMENDED}),
            ('the draft work mode', {'model': 'maso:gpt56_sol', 'draft': {'work_mode': 'adaptive'}},
             {'model': 'maso:gpt56_sol', 'work_mode': 'adaptive', 'effort': ''}),
            ('a work mode the model lacks is its first', {'model': 'maso:gpt56_sol', 'work_mode': 'turbo'},
             {'work_mode': 'focus'}),
            ('the draft workspace', {'draft': {'workspace': 'other'}}, {'workspace': 'other', 'names': NAMES, 'notice': ''}),
            ('a removed workspace stays chosen and is told', {'draft': {'workspace': 'gone'}, 'notice': '已更新'},
             {'workspace': 'gone', 'names': [*NAMES, 'gone'], 'notice': '已更新\n' + WARNING}),
            ('a binding workspace off the list is the default', {'workspace': 'gone'}, {'workspace': 'project', 'notice': ''}),
        ])

    def test_what_the_menu_still_offers(self):
        self.check([
            ('a model off the menu gives way to the first', {'model': 'codex:gone'}, {'model': 'claude:opus', 'effort': RECOMMENDED}),
            ('a model off the menu gives way to the chats most used', {'model': 'codex:gone',
                                                                       'usage': used(('dsh:flash', 'default', ''))},
             {'model': 'dsh:flash', 'effort': 'default', 'first': 'dsh:flash'}),
            ('an empty menu', {'choices': {}}, {'model': 'unavailable', 'effort': 'default', 'work_mode': '', 'first': None}),
        ])

    def test_a_device_workspace_starts_from_a_harness_the_device_declares(self):
        # A-10: testbox declared only Codex, yet the card started from the hub's default Claude Code.
        device = {'device_harnesses': {'project': ('codex',), 'other': ('dsh', 'codex')}}
        usual = used(('codex:gpt', 'low', ''), ('codex:gpt', 'low', ''))
        self.check([
            ('a Harness the device lacks gives way to its first declared one', {**device, 'model': 'claude:opus'},
             {'model': 'codex:gpt', 'effort': RECOMMENDED, 'workspace': 'project', 'first': 'codex:gpt'}),
            ('with that models usual budget', {**device, 'model': 'claude:opus', 'usage': usual},
             {'model': 'codex:gpt', 'effort': 'low', 'first': 'codex:gpt'}),
            ('a declared Harness stays', {**device, 'model': 'codex:gpt'}, {'model': 'codex:gpt', 'first': 'claude:opus'}),
            ('the first declared one the hub offers', {'device_harnesses': {'project': ('maso', 'dsh', 'codex')},
                                                       'model': 'claude:opus'},
             {'model': 'maso:gpt56_sol', 'effort': '', 'work_mode': 'focus'}),
            ('none of them offered: the model stays and 开始任务 refuses it', {'device_harnesses': {'project': ('pi',)},
                                                                              'model': 'claude:opus'},
             {'model': 'claude:opus'}),
            ('a workspace that is not on a device', {'device_harnesses': {'project': ('codex',)}, 'model': 'claude:opus',
                                                     'workspace': 'other', 'draft': {'workspace': 'other'}},
             {'model': 'claude:opus', 'workspace': 'other'}),
            ('no device workspaces at all', {'model': 'claude:opus'}, {'model': 'claude:opus'}),
            ('the draft workspace is the one that counts', {'device_harnesses': {'other': ('codex',)}, 'model': 'claude:opus',
                                                            'draft': {'workspace': 'other'}},
             {'model': 'codex:gpt', 'workspace': 'other'}),
            ('a model the owner chose stays', {**device, 'model': 'claude:opus', 'draft': {'model': 'claude:opus'}},
             {'model': 'claude:opus'}),
            ('the last launch is fitted too', {**device, 'model': 'claude:opus', 'fresh': True,
                                               'saved': {'model_choice': 'claude:opus', 'effort': 'max'}},
             {'model': 'codex:gpt', 'effort': RECOMMENDED}),
        ])

    def test_the_new_topic_default_budget(self):
        # The 设置卡's 新话题默认 budget (owner 2026-11-10): before the chat's usual one and its last launch's, after the
        # card's own and a MASO preset's; only for its Harness and where the model offers it.
        usual = used(('claude:opus', 'xhigh', ''), ('claude:opus', 'xhigh', ''))
        opus = {'model': 'claude:opus', 'topic_budget': ('claude', 'high')}
        self.check([
            ('it replaces the recommended one', opus, {'model': 'claude:opus', 'effort': 'high'}),
            ('it comes before the usual one', {**opus, 'usage': usual}, {'effort': 'high'}),
            ('and before the last launch', {**opus, 'fresh': True, 'saved': {'model_choice': 'claude:opus', 'effort': 'max'}},
             {'model': 'claude:opus', 'effort': 'high'}),
            ('the card keeps its own', {**opus, 'draft': {'model': 'claude:opus', 'effort': 'max'}}, {'effort': 'max'}),
            ('another Harness keeps its own', {'topic_budget': ('claude', 'high')}, {'model': 'codex:gpt', 'effort': RECOMMENDED}),
            ('a Harness of any', {'topic_budget': ('', 'high')}, {'model': 'codex:gpt', 'effort': 'high'}),
            ('a budget the model lacks', {**opus, 'topic_budget': ('claude', 'low'), 'usage': usual}, {'effort': 'xhigh'}),
            ('a MASO preset keeps its own', {'model': 'maso:gpt56_sol_reasoning_max', 'topic_budget': ('', 'high')},
             {'model': 'maso:gpt56_sol', 'effort': 'max'}),
            ('none set', {**opus, 'topic_budget': ('', ''), 'usage': usual}, {'effort': 'xhigh'}),
        ])


    def test_the_new_topic_page_names_a_model_and_a_workspace(self):
        # T546 (the owner, 2026-11-19): the 设置 card's 新话题 page preselects every new topic, the launch card's too;
        # an empty row (跟上次一样) keeps the last launch's. Only a first binding is prefilled; a card's own draft wins.
        last = {'model_choice': 'codex:gpt', 'effort': 'low', 'workdir': '/absolute/other'}
        fresh = {'fresh': True, 'saved': last}
        self.check([
            ('its model over the last launch', {**fresh, 'topic_model': ('claude', 'opus')},
             {'model': 'claude:opus', 'effort': RECOMMENDED, 'workspace': 'other', 'first': 'claude:opus'}),
            ('with its budget', {**fresh, 'topic_model': ('claude', 'opus'), 'topic_budget': ('claude', 'max')},
             {'model': 'claude:opus', 'effort': 'max'}),
            ('a Harness alone keeps the last model where it is that Harness', {**fresh, 'topic_model': ('codex', '')},
             {'model': 'codex:gpt', 'effort': 'low'}),
            ('a Harness alone, the last launch another: its first model', {**fresh, 'topic_model': ('maso', '')},
             {'model': 'maso:gpt56_sol', 'work_mode': 'focus', 'first': 'maso:gpt56_sol'}),
            ('a model gone from the menu: its Harness', {**fresh, 'topic_model': ('claude', 'gone')},
             {'model': 'claude:opus'}),
            ('a Harness off the menu: the last launch', {**fresh, 'topic_model': ('pi', '')}, {'model': 'codex:gpt'}),
            ('its workspace over the last launch', {**fresh, 'topic_workspace': 'project'}, {'workspace': 'project'}),
            ('a workspace off the list: the last launch', {**fresh, 'topic_workspace': 'gone'}, {'workspace': 'other'}),
            ('a later binding keeps its own', {'saved': last, 'topic_model': ('claude', 'opus'), 'topic_workspace': 'other'},
             {'model': 'codex:gpt', 'workspace': 'project'}),
            ('the card keeps its own', {**fresh, 'topic_model': ('claude', 'opus'),
                                        'draft': {'model': 'codex:gpt', 'workspace': 'other'}},
             {'model': 'codex:gpt', 'workspace': 'other'}),
        ])


class SwitchSelectionTests(unittest.TestCase):
    def check(self, cases):
        for name, topic, usage, expected in cases:
            with self.subTest(name):
                topic = {'harness_id': 'codex', 'model': 'gpt', 'effort': 'low', 'workspace_name': 'project', **topic}
                got = switch_selection(CHOICES, usage, topic, NAMES)
                self.assertEqual({k: getattr(got, k) for k in expected if k != 'first'},
                                 {k: v for k, v in expected.items() if k != 'first'})
                if 'first' in expected:
                    self.assertEqual(next(iter(got.choices)), expected['first'])
                self.assertEqual(set(got.choices), set(CHOICES))

    def test_the_topics_binding(self):
        self.check([
            ('the topic model and budget', {}, [],
             {'key': 'codex:gpt', 'selected': 'codex:gpt', 'effort': 'low', 'usual': {}, 'redacting': False,
              'workspace': 'project', 'names': NAMES, 'first': 'codex:gpt'}),
            ('a MASO preset is its model with that budget', {'harness_id': 'maso', 'model': 'gpt56_sol_reasoning_high', 'effort': 'high'},
             [], {'key': 'maso:gpt56_sol', 'selected': 'maso:gpt56_sol', 'effort': 'high', 'first': 'maso:gpt56_sol'}),
            ('a plain MASO preset has the native default', {'harness_id': 'maso', 'model': 'gpt56_sol', 'effort': ''}, [],
             {'key': 'maso:gpt56_sol', 'effort': 'default'}),
            ('a budget not offered: the usual one', {'effort': 'max'}, used(('codex:gpt', 'high', ''), ('codex:gpt', 'high', '')),
             {'effort': 'high', 'usual': {'codex:gpt': 'high'}}),
            ('a budget not offered: the default', {'effort': 'max'}, [], {'effort': RECOMMENDED}),
            ('a model off the menu', {'model': 'gone'}, used(('dsh:flash', 'default', '')),
             {'key': 'codex:gone', 'selected': 'dsh:flash', 'effort': 'default', 'usual': {'dsh:flash': 'default'},
              'first': 'dsh:flash'}),
        ])

    def test_redaction_and_the_workspace(self):
        self.check([
            ('a redacting topic preselects a model that redacts', {'redaction': True}, used(('claude:opus', 'max', '')),
             {'key': 'codex:gpt', 'selected': 'claude:opus', 'effort': 'max', 'redacting': True, 'first': 'codex:gpt'}),
            ('a redacting topic on a redacting model keeps it', {'harness_id': 'claude', 'model': 'opus', 'effort': 'high',
                                                                 'redaction': True}, [],
             {'selected': 'claude:opus', 'effort': 'high', 'redacting': True}),
            ('only True is redacting', {'redaction': 'yes'}, [], {'selected': 'codex:gpt', 'redacting': False}),
            ('a workspace off the list comes first', {'workspace_name': 'gone'}, [],
             {'workspace': 'gone', 'names': ['gone', *NAMES]}),
            ('without a name the workdir', {'workspace_name': '', 'workdir': '/absolute/worktrees/t1'}, [],
             {'workspace': '/absolute/worktrees/t1', 'names': ['/absolute/worktrees/t1', *NAMES]}),
        ])

    def test_a_redacting_topic_when_no_model_redacts_keeps_its_own(self):
        choices = {key: {k: v for k, v in entry.items() if k != 'redaction'} for key, entry in CHOICES.items()}
        got = switch_selection(choices, [], {'harness_id': 'codex', 'model': 'gpt', 'redaction': True}, [])
        self.assertEqual((got.selected, got.effort, got.workspace, got.names), ('codex:gpt', RECOMMENDED, '', ['']))


if __name__ == '__main__':
    unittest.main()

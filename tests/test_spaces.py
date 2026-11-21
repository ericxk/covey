"""Spaces (core/spaces.py, app/access.py): the ``spaces`` section of harnesses.json, the NAS profile's older keys as
aliases, who is served where, and where the Bridge's own notices go."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support.world import UnifiedCase
from test_group_chat import MEMBER, GroupFixture, group_event
from covey.app.unified import UnifiedBridgeApp
from covey.core import spaces
from covey.core.proxy import write_settings

ALIAS = 'ou_alias0012345678'


def direct(mid, sender, chat='dm-alias'):
    return {'chat_id': chat, 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': sender, 'message_id': mid,
            'content': '帮我看看'}


class ParseTests(unittest.TestCase):
    def test_no_section_is_the_defaults(self):
        for config in ({}, {'spaces': {}}, None):
            self.assertEqual(spaces.parse(config), (spaces.DEFAULT, []))
        self.assertEqual((spaces.DEFAULT.owner_accounts, spaces.DEFAULT.notify_space,
                          spaces.DEFAULT.allow_team_spaces, spaces.DEFAULT.personal_groups),
                         (frozenset(), '', True, frozenset()))

    def test_the_section(self):
        found, notes = spaces.parse({'spaces': {'owner_accounts': [ALIAS], 'notify_space': 'oc_home',
                                                'allow_team_spaces': False, 'personal_groups': ['oc_room']}})
        self.assertEqual((found, notes), (spaces.Settings(frozenset({ALIAS}), 'oc_home', False,
                                                           frozenset({'oc_room'})), []))

    def test_old_keys_are_aliases_and_each_is_noted(self):
        found, notes = spaces.parse({'owner_only': True, 'owner_aliases': [ALIAS], 'home_chat': 'oc_home',
                                     'agent_groups': ['oc_room'], 'group_menu': True, 'group_boards': True,
                                     'owner_headed': True})
        self.assertEqual(found, spaces.Settings(frozenset({ALIAS}), 'oc_home', False, frozenset({'oc_room'})))
        self.assertEqual(len(notes), 7)
        for key in ('owner_only', 'owner_aliases', 'home_chat', 'agent_groups'):
            self.assertTrue(any(key in n and 'deprecated' in n for n in notes), key)
        for key in ('group_menu', 'group_boards', 'owner_headed'):
            self.assertTrue(any(key in n and 'personal_groups' in n for n in notes), key)

    def test_the_section_wins_over_an_old_key(self):
        found, _ = spaces.parse({'home_chat': 'oc_old', 'spaces': {'notify_space': 'oc_new'}, 'owner_only': True,
                                 })
        self.assertEqual((found.notify_space, found.allow_team_spaces), ('oc_new', False))
        found, _ = spaces.parse({'owner_only': True, 'spaces': {'allow_team_spaces': True}})
        self.assertTrue(found.allow_team_spaces)

    def test_bad_values_raise(self):
        for bad in ({'spaces': []}, {'spaces': {'owner_accounts': ALIAS}}, {'spaces': {'owner_accounts': ['x']}},
                    {'spaces': {'notify_space': 'a b'}}, {'spaces': {'notify_space': 5}},
                    {'spaces': {'allow_team_spaces': 'no'}}, {'spaces': {'personal_groups': ['a b']}},
                    {'owner_only': 'yes'}, {'owner_aliases': ['not-an-open-id']}, {'home_chat': 5},
                    {'agent_groups': 'oc_room'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                spaces.parse(bad)

    def test_space_of(self):
        settings = spaces.Settings(personal_groups=frozenset({'oc_room'}))
        self.assertEqual(spaces.space_of(settings, 'oc_room', active=True), spaces.PERSONAL)
        self.assertEqual(spaces.space_of(settings, 'oc_dm', private=True), spaces.PERSONAL)
        self.assertEqual(spaces.space_of(settings, 'oc_p', proxy=True, active=True), spaces.PROXY)
        self.assertEqual(spaces.space_of(settings, 'oc_g', active=True), spaces.TEAM)
        self.assertEqual(spaces.space_of(settings, 'oc_g'), '')
        self.assertEqual(spaces.space_of(spaces.Settings(allow_team_spaces=False), 'oc_g', active=True), '')


class SpacesAccessTests(GroupFixture, UnifiedCase):
    def setUp(self):
        super().setUp()
        self.group_setup()

    def test_default_serves_as_before(self):
        self.assertEqual(self.access.spaces, spaces.DEFAULT)
        self.assertFalse(self.access.is_owner(ALIAS))
        self.send(group_event('o1', '@Covey 第一个任务'))
        self.assertEqual((self.access.pending('group'), self.access.space_of('group')), (True, ''))  # His word first.
        self.activate('group')
        self.assertEqual(self.access.space_of('group'), spaces.TEAM)
        self.assertEqual(self.access.notify_chat(), self.access.owner_chat())

    def test_no_team_spaces_never_activates_a_group(self):
        self.access.spaces = spaces.Settings(allow_team_spaces=False)
        self.send(group_event('o1', '@Covey 第一个任务'))
        self.assertFalse(self.access.group('group').get('active'))
        self.assertFalse(self.access.pending('group'))  # Nothing to ask the owner.
        self.send(group_event('m1', '@Covey 帮我看看', MEMBER))
        self.assertEqual(self.ignored(), ['group_inactive'])
        self.assertEqual(len(self.topic_dirs()), 1)
        self.assertFalse(self.access.allows(MEMBER, 'group'))
        self.assertTrue(self.access.allows('owner', 'group'))
        self.assertEqual(self.access.space_of('group'), '')

    def test_the_group_card_cannot_switch_one_on_and_old_records_stay_off(self):
        self.access.spaces = spaces.Settings(allow_team_spaces=False)
        self.assertIn('不能启用', self.access.switch(self.ctx, 'group', True))
        self.assertFalse(self.access.group('group').get('active'))
        self.access.spaces = spaces.DEFAULT
        self.activate('group')
        self.access.spaces = spaces.Settings(allow_team_spaces=False)
        self.assertFalse(self.access.allows(MEMBER, 'group'))

    def test_a_personal_group_serves_only_the_owner_and_never_activates(self):
        self.access.spaces = spaces.Settings(personal_groups=frozenset({'group'}), owner_accounts=frozenset({ALIAS}))
        self.send(group_event('o1', '@Covey 第一个任务'))
        self.send(group_event('a1', '@Covey 另一个账号', ALIAS))
        self.send(group_event('m1', '@Covey 帮我看看', MEMBER))
        self.assertEqual(self.ignored(), ['group_inactive'])
        self.assertEqual(len(self.topic_dirs()), 2)
        self.assertEqual((self.access.group('group').get('active'), self.access.pending('group')), (False, False))
        self.assertEqual(self.access.space_of('group'), spaces.PERSONAL)
        self.assertEqual(self.access.space_of('other'), '')
        # Other groups still become team spaces, on the owner's word.
        self.send(group_event('o2', '@Covey 这个群', chat='group2'))
        self.assertTrue(self.access.pending('group2'))
        self.activate('group2')
        self.assertEqual(self.access.space_of('group2'), spaces.TEAM)

    def test_the_alias_is_the_owner_and_members_are_not(self):
        self.access.spaces = spaces.Settings(owner_accounts=frozenset({ALIAS}))
        self.assertTrue(self.access.is_owner(ALIAS))
        self.assertTrue(self.access.allows(ALIAS, 'group'))
        self.assertFalse(self.access.is_owner(MEMBER))
        self.send(direct('a1', ALIAS))
        self.send(direct('m1', MEMBER, 'dm-member'))
        self.assertEqual(self.ignored(), ['not_owner'])
        self.assertEqual(len(self.topic_dirs()), 1)
        self.send(group_event('a2', '@Covey 群里也行', ALIAS))
        self.assertEqual(len(self.topic_dirs()), 2)
        self.assertEqual(self.access.sender_label({'sender_id': ALIAS}), '发送者：主人')

    def test_an_alias_private_chat_is_not_the_owners_chat(self):
        self.access.spaces = spaces.Settings(owner_accounts=frozenset({ALIAS}))
        self.send(direct('o1', 'owner', 'dm-owner'))
        self.send(direct('a1', ALIAS, 'dm-alias'))
        self.assertEqual(self.access.owner_chat(), 'dm-owner')
        self.assertEqual(self.access.space_of('dm-owner'), spaces.PERSONAL)

    def test_aliases_count_only_with_an_owner(self):
        self.access.spaces = spaces.Settings(owner_accounts=frozenset({ALIAS}))
        self.app.runtime.owner_open_id = None
        self.assertFalse(self.access.is_owner(ALIAS))

    def test_the_proxy_group_is_a_proxy_space(self):
        write_settings(self.ctx.state_dir, {'group_chat_id': 'oc_proxy'})
        self.assertEqual(self.access.space_of('oc_proxy'), spaces.PROXY)

    def test_notices_go_to_the_notify_space_else_the_private_chat(self):
        self.access.remember_owner_chat('dm-owner')
        self.assertEqual(self.access.notify_chat(), 'dm-owner')
        self.access.spaces = spaces.Settings(notify_space='oc_home')
        self.assertEqual(self.access.notify_chat(), 'oc_home')
        self.assertEqual(self.access.owner_chat(), 'dm-owner')  # The private chat itself does not move.
        self.send(direct('o1', 'owner', 'dm-other'))
        self.assertEqual(self.access.notify_chat(), 'oc_home')


class ConfigTests(unittest.TestCase):
    def build(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'harnesses.json'
            path.write_text(json.dumps(config))
            app = UnifiedBridgeApp(script_path=Path(tmp) / 'bridge.py')
            logs = []
            app.runtime.log = logs.append
            args = app.build_parser().parse_args(['--workdir', tmp, '--state-dir', str(Path(tmp) / 'state'),
                                                  '--profile', 'test', '--harness-config', str(path)])
            app.build_context(args)
            return app.runtime.access, logs

    def test_the_section_is_read_at_start(self):
        access, logs = self.build({'spaces': {'owner_accounts': [ALIAS], 'notify_space': 'oc_home',
                                              'personal_groups': ['oc_room']}})
        self.assertEqual(access.spaces, spaces.Settings(frozenset({ALIAS}), 'oc_home', True, frozenset({'oc_room'})))
        self.assertFalse([line for line in logs if 'deprecated' in line])

    def test_old_keys_log_one_line_each(self):
        access, logs = self.build({'owner_only': True, 'owner_aliases': [ALIAS], 'home_chat': 'oc_home',
                                   'agent_groups': ['oc_room'], 'group_menu': True})
        self.assertEqual((access.spaces.allow_team_spaces, access.spaces.notify_space),
                         (False, 'oc_home'))
        self.assertEqual(len([line for line in logs if 'deprecated' in line]), 5)
        self.assertEqual(len([line for line in logs if 'no effect of its own' in line]), 1)

    def test_default_and_bad_values(self):
        access, logs = self.build({})
        self.assertEqual(access.spaces, spaces.DEFAULT)
        with self.assertRaises(ValueError):
            self.build({'spaces': {'allow_team_spaces': 'no'}})


if __name__ == '__main__':
    unittest.main()

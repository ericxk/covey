"""Personal spaces (core/spaces.py, app/access.py ``agent_chat``): a butler of its own in each personal group and in
each private chat of the owner's other accounts, the menu card pinned in a personal group (app/group_menu.py), a board
and 早晚汇总 of its own there (app/group_boards.py), the space records, and the 群聊 tab's space types."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import unittest
from unittest import mock

from types import SimpleNamespace

from support.world import UnifiedCase
from test_group_chat import MEMBER, GroupFixture, group_event
from test_butler import ButlerUnifiedCase
from test_pins import NOW, ON, FakePins
from support.records import write_topic
from covey.app import digest, group_menu, pins, saved_send
from covey.app.overview import RECORD as OVERVIEW_RECORD
from covey.cards.overview import DISMISS
from covey.core import butler, placement, spaces
from covey.core.jobs import upsert_job_record
from covey.core.state import write_json

ALIAS = 'ou_alias0012345678'


def setspaces(access, **fields):
    access.spaces = dataclasses.replace(access.spaces, **fields)


class AgentChatTests(ButlerUnifiedCase):
    """Personal groups and the private chats of the owner's other accounts: each chat has a butler of its own, and
    a tool call runs for the chat of the butler that made it."""

    def setUp(self):
        super().setUp()
        self.access = self.app.runtime.access
        setspaces(self.access, owner_accounts=frozenset({ALIAS}))
        setspaces(self.access, notify_space='oc_home')
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))

    def test_each_owner_private_chat_and_an_agent_group_have_a_butler_of_their_own(self):
        self.send('om_1')
        self.send('om_2', chat_id='oc_alias', sender_id=ALIAS)
        self.assertEqual(self.job_of('om_1').topic_id, butler.butler_topic_id('oc_main'))
        self.assertEqual(self.job_of('om_2').topic_id, butler.butler_topic_id('oc_alias'))
        room = {**self.event('om_3'), 'chat_id': 'oc_room', 'chat_type': 'group'}
        self.assertIsNotNone(self.app.butler.app_for(self.ctx, room, True))
        self.assertIsNone(self.app.butler.app_for(self.ctx, {**room, 'chat_id': 'oc_other'}, True))
        self.assertIsNone(self.app.butler.app_for(self.ctx, room, False))
        self.assertIsNone(self.app.butler.app_for(self.ctx, {**room, 'chat_type': 'p2p'}, True))

    def test_a_tool_call_runs_for_the_chat_of_its_butler(self):
        self.send('om_1')
        self.send('om_2', chat_id='oc_alias', sender_id=ALIAS)
        tools, env = self.app.butler_tools, self.app.butler_env
        self.assertEqual(tools.request_chat(SimpleNamespace(job_key='om_2')), 'oc_alias')
        self.assertEqual(tools.request_chat(SimpleNamespace(job_key='om_none')), '')
        seen = []
        with mock.patch.dict(tools.handlers, {'probe': lambda env, request: (seen.append(env.chat()),
                                                                              {'ok': True, 'text': ''})[1]}):
            request = SimpleNamespace(id='r1', tool='probe', job_key='om_2')
            self.assertTrue(tools.execute(request)['ok'])
        self.assertEqual((seen, env.chat()), (['oc_alias'], 'oc_main'))


class GroupMenuTests(GroupFixture, UnifiedCase):
    """app/group_menu.py: the menu card pinned in a personal group, whose buttons run the menu's items there."""

    def setUp(self):
        super().setUp()
        self.group_setup()
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))
        self.opened = []
        actions = {key: (lambda key: lambda ctx, chat, source, shown: self.opened.append((key, chat)))(key)
                   for key, _ in group_menu.ITEMS}
        for patch in (mock.patch.dict(self.app.menu.actions, actions), mock.patch.dict(self.app.menu.folds, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.messenger = self.app.messenger
        self.messenger.create_and_send_card = mock.Mock(side_effect=lambda *a, **k: ('c', f'om_menu{len(self.sent())}'))
        self.messenger.pin = mock.Mock(return_value=True)
        self.messenger.unpin = mock.Mock(return_value=True)

    def sent(self):
        return self.messenger.create_and_send_card.call_args_list

    def test_one_card_is_sent_and_pinned_in_each_personal_group(self):
        menu = self.app.group_menu
        menu.tick(self.ctx)
        menu.tick(self.ctx)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.sent()[0].args[1], 'oc_room')
        card = json.dumps(self.sent()[0].args[2], ensure_ascii=False)
        self.assertEqual([label for _, label in group_menu.ITEMS if f'"{label}"' in card], ['用量', '看板', '设置'])
        self.messenger.pin.assert_called_once_with(self.ctx.profile, 'om_menu1')
        with mock.patch.object(group_menu, 'VERSION', 2):
            menu.tick(self.ctx)
        self.messenger.unpin.assert_called_once_with(self.ctx.profile, 'om_menu1')
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual([name for name, _ in menu.threads()], ['group-menu'])
        setspaces(self.access, personal_groups=frozenset())
        menu.tick(self.ctx)
        self.assertEqual((len(self.sent()), menu.threads()), (2, ()))

    def click(self, event_id, key='digest_board', operator='owner', chat='oc_room'):
        self.app.handle_card_action_event({'operator_id': operator, 'chat_id': chat, 'message_id': 'om_menu1',
                                           'event_id': event_id, 'action_value': {'kind': group_menu.KIND, 'key': key}},
                                          self.ctx)

    def test_a_click_runs_the_item_in_that_group_for_the_owner_only(self):
        self.click('e1')
        self.click('e1')
        self.click('e2', operator=MEMBER)
        self.click('e3', chat='oc_other')
        self.click('e4', key='nope')
        self.click('e5', key='bridge_settings')
        self.assertEqual(self.opened, [('digest_board', 'oc_room'), ('bridge_settings', 'oc_room')])
        self.assertEqual(self.outcomes(), ['group_menu', 'duplicate', 'not_owner', 'group_menu_ignored',
                                           'group_menu_ignored', 'group_menu'])


class GroupBoardTests(GroupFixture, UnifiedCase):
    """app/group_boards.py: each personal group has its own 手上的事 and pinned board, in records of their own, beside the
    private chat's; the owner's message there reads it."""

    def setUp(self):
        super().setUp()
        self.group_setup()
        self.state = self.ctx.state_dir
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))
        pins.configure_boards({'oc_room'})
        self.addCleanup(pins.configure_boards, ())
        self.access.remember_owner_chat('oc_dm')
        self.feishu = FakePins()
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            'card-' + chat, 'om_index_' + chat))
        messenger.put_card = mock.Mock(return_value={'code': 0})
        messenger.pin, messenger.unpin, messenger.pin_info = self.feishu.pin, self.feishu.unpin, self.feishu.pin_info
        for number, topic in enumerate(('t1', 't2', 't3'), 1):
            write_topic(self.state, {'topic_number': number}, topic_id=topic)
        for chat in ('oc_room', 'oc_dm'):  # Boards running for a week (tests/test_pins.py RoundTests).
            write_json(pins.record_path(self.state, chat), {'schema_version': 1, 'since': NOW - 7 * 86400})
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)

    def job(self, key, job_id, chat, topic, status='running', **fields):
        upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id=chat, topic_id=topic,
                          card_message_id=f'om_{key}', harness_id='claude', **fields)

    def record(self, chat, name=pins.RECORD):
        return json.loads(pins.record_path(self.state, chat, name).read_text())

    def tick(self, now):
        self.app.overview.tick(self.ctx, now)
        self.app.group_boards.boards['oc_room'].tick(self.ctx, now)

    def test_each_chat_keeps_its_own_record(self):
        self.assertEqual(pins.record_path(self.state, 'oc_dm'), self.state / pins.RECORD)
        self.assertEqual(pins.record_path(self.state, 'oc_room').name, 'pinned-topics.oc_room.json')
        self.assertEqual(pins.record_path(self.state, 'oc_room', OVERVIEW_RECORD).name,
                         'pinned-overview.oc_room.json')

    def test_the_group_has_its_own_index_and_board_and_the_private_chat_keeps_its_own(self):
        self.assertEqual([name for name, _ in self.app.group_boards.threads()], ['overview-oc_room'])
        self.job('room_run', 1, 'oc_room', 't1', timings={'backend_started': NOW - 60})
        self.job('dm_res', 2, 'oc_dm', 't2', status='done', timings={'delivered': NOW - 30})
        self.tick(NOW)
        room, dm = self.record('oc_room'), self.record('oc_dm')
        self.assertEqual((room['chat_id'], room['index_message_id'], [pin['message_id'] for pin in room['pins']]),
                         ('oc_room', 'om_index_oc_room', ['om_room_run']))
        self.assertEqual((dm['chat_id'], dm['index_message_id'], [pin['message_id'] for pin in dm['pins']]),
                         ('oc_dm', 'om_index_oc_dm', ['om_dm_res']))
        self.assertEqual(self.record('oc_room', OVERVIEW_RECORD)['chat_id'], 'oc_room')
        self.assertEqual(self.record('oc_dm', OVERVIEW_RECORD)['chat_id'], 'oc_dm')
        cards = {call.args[1]: json.dumps(call.args[2], ensure_ascii=False)
                 for call in self.app.messenger.create_and_send_card.call_args_list}
        self.assertIn('只列这个群里的任务', cards['oc_room'])
        self.assertIn('只列这个私聊里的任务', cards['oc_dm'])

    def test_without_personal_groups_or_the_overview_no_thread(self):
        with mock.patch.dict(os.environ, {'COVEY_PINNED_OVERVIEW': ''}):
            self.assertEqual(self.app.group_boards.threads(), ())
        setspaces(self.access, personal_groups=frozenset())
        self.assertEqual(self.app.group_boards.threads(), ())

    def test_the_owners_message_in_the_group_reads_its_results(self):
        self.app.group_boards.threads()
        self.job('room_res', 1, 'oc_room', 't1', status='done', timings={'delivered': NOW - 30})
        self.job('dm_res', 2, 'oc_dm', 't2', status='done', timings={'delivered': NOW - 30})
        self.tick(NOW)
        self.assertEqual([pin['message_id'] for pin in self.record('oc_room')['pins']], ['om_room_res'])
        self.assertEqual(self.app.group_boards.owner_active(self.ctx, 'oc_dm', NOW), {})
        self.assertEqual(self.app.group_boards.owner_active(self.ctx, 'oc_room', NOW), {'t1': NOW})
        self.tick(NOW + pins.GAP)
        self.assertEqual(self.record('oc_room')['pins'], [])
        self.assertEqual([pin['message_id'] for pin in self.record('oc_dm')['pins']], ['om_dm_res'])
        with mock.patch.dict(os.environ, {'COVEY_PINNED_TOPICS': ''}):
            self.assertEqual(self.app.group_boards.owner_active(self.ctx, 'oc_room', NOW + 1), {})

    def test_a_group_message_of_the_owner_reaches_owner_active(self):
        setspaces(self.access, personal_groups=frozenset({'group'}))
        with mock.patch.object(self.app.group_boards, 'owner_active') as active:
            self.send(group_event('o1', '@Covey 看看', 'owner'))
            self.send(group_event('m1', '@Covey 我也看看', MEMBER))
        self.assertEqual([call.args[1] for call in active.call_args_list], ['group'])

    def test_a_dismiss_click_on_the_groups_card_takes_off_its_own_board(self):
        self.app.group_boards.threads()
        self.job('room_q', 1, 'oc_room', 't1', status='done', timings={'delivered': NOW - 30})
        self.tick(NOW)
        with mock.patch('covey.app.overview.time', SimpleNamespace(time=lambda: NOW + 10)):
            outcome = self.app.overview.claim(self.ctx, {'chat_id': 'oc_room', 'message_id': 'om_index_oc_room'},
                                              {'kind': DISMISS, 'topics': ['t1']}, True)
        self.assertEqual(outcome, 'overview_dismiss')
        self.assertIn('t1', self.record('oc_room')['dismissed'])
        self.assertNotIn('t1', self.record('oc_dm').get('dismissed') or {})


class AgentChatAnswerHeadTests(UnifiedCase):
    """saved_send.owner_headed: a 话题区's answer card leads with "✅ 做完 · T11 · 标题" in every chat with a butler
    (personal groups, the other accounts' private chats), not only the owner's private chat."""

    def test_agent_chats_are_headed_and_other_groups_are_not(self):
        access = self.app.runtime.access
        setspaces(access, owner_accounts=frozenset({ALIAS}))
        access.remember_owner_chat('oc_home')
        setspaces(access, personal_groups=frozenset({'oc_room'}))
        access.remember_account_chat(ALIAS, 'oc_alias')
        row = placement.TABLE[(placement.AREA, placement.TOPIC_ROLE)]
        headed = {chat: saved_send.owner_headed(row, self.app.runtime, chat)
                  for chat in ('oc_home', 'oc_room', 'oc_alias', 'oc_group', '')}
        self.assertEqual(headed, {'oc_home': True, 'oc_room': True, 'oc_alias': True, 'oc_group': False, '': False})
        self.assertFalse(saved_send.owner_headed(placement.TABLE[(placement.AREA, placement.BUTLER_ROLE)],
                                                 self.app.runtime, 'oc_room'))


class GroupDigestTests(unittest.TestCase):
    """app/group_boards.py's Digest: 早晚汇总 of a personal group's topics, sent there, in a record of its own; the
    private chat's goes on as before."""

    def setUp(self):
        from support.records import state_dir
        from test_digest import FakeMessenger
        from covey.feishu.messenger import Voice
        self.state = state_dir(self)
        pins.configure_boards({'oc_room'})
        self.addCleanup(pins.configure_boards, ())
        self.ctx = SimpleNamespace(state_dir=self.state, profile='test-bot')
        self.runtime = SimpleNamespace(log=lambda line: None, shutdown=threading.Event(),
                                       access=SimpleNamespace(owner_chat=lambda: 'oc_dm', notify_chat=lambda: 'oc_dm',
                                                              agent_groups=frozenset({'oc_room'})))
        self.messenger = FakeMessenger()
        voice = Voice('lark-bridge', 'LARK_BRIDGE')
        self.dm = digest.Digest(self.runtime, self.messenger, voice)
        self.room = digest.Digest(self.runtime, self.messenger, voice, chat='oc_room', name='digest-oc_room')
        for feature in (self.dm, self.room):
            feature.link = lambda ctx, chat, text, cards=None: text
        self.now = time.time()
        local = digest.datetime.datetime.fromtimestamp(self.now, digest.ZONE)
        self.slot = local.replace(second=0, microsecond=0).timestamp()
        self.times = [local.hour * 60 + local.minute]
        for number, (topic, chat) in enumerate((('t_room', 'oc_room'), ('t_dm', 'oc_dm')), 401):
            write_topic(self.state, {'topic_number': number}, topic_id=topic)
            upsert_job_record(self.state, 'j-' + topic, job_id=number, status='running', chat_id=chat, topic_id=topic,
                              card_message_id='om_' + topic, harness_id='claude',
                              timings={'backend_started': self.now - 300})

    def test_each_chat_gets_its_own_topics_in_its_own_record(self):
        for feature in (self.room, self.dm):
            feature.tick(self.ctx, self.times, now=self.slot + 5)
        sent = {send['chat']: send for send in self.messenger.sends}
        self.assertEqual(sorted(sent), ['oc_dm', 'oc_room'])
        room, dm = (json.dumps(sent[chat]['card'], ensure_ascii=False) for chat in ('oc_room', 'oc_dm'))
        self.assertIn('T401', room)
        self.assertNotIn('T402', room)
        self.assertIn('T402', dm)
        self.assertIn('只列这个群里的话题', room)
        self.assertIn('只列这个私聊里的话题', dm)
        self.assertNotEqual(sent['oc_room']['key'], sent['oc_dm']['key'])
        self.assertEqual(json.loads((self.state / 'digest.oc_room.json').read_text())['last_slot'], self.slot)
        self.assertEqual(json.loads((self.state / digest.RECORD).read_text())['last_slot'], self.slot)

    def test_the_board_on_the_group_menu_says_group(self):
        view = self.dm.board_view(self.ctx, 'oc_room', self.now)
        self.assertEqual(view.get('where'), '群')
        self.assertNotIn('where', self.dm.board_view(self.ctx, 'oc_dm', self.now))


class GroupBoardsDigestThreadTests(GroupFixture, UnifiedCase):
    def test_a_digest_thread_for_each_agent_group_handed_its_dismissals(self):
        self.group_setup()
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))
        with mock.patch.dict(os.environ, {digest.ENV: '09:30,23:30', 'COVEY_PINNED_OVERVIEW': '1'}):
            names = [name for name, _ in self.app.group_boards.threads()]
        self.assertEqual(names, ['overview-oc_room', 'digest-oc_room'])
        room = self.app.group_boards.boards['oc_room']
        self.assertEqual(room.handed[digest.Digest.origin], self.app.group_boards.digests['oc_room'].dismissed)


class SettingsCardChatTests(UnifiedCase):
    """The 设置 card's 新话题 defaults follow the last launch of the chat the card is in."""

    def test_the_card_resolves_with_its_own_chat(self):
        panel = self.app.butler_panel
        with mock.patch.object(panel, 'card', return_value={}) as card:
            panel.tab_card(self.ctx, 'tok', {'view': {'token': 'tok'}, 'tab': 'topic', 'chat_id': 'oc_room'})
        self.assertEqual(card.call_args.args[3], 'oc_room')
        seen = []
        with mock.patch('covey.app.butler_panel.resolve',
                        side_effect=lambda topics, state, chat, defaults, **kw: (seen.append(chat), 'no')[1]):
            from covey.core.butler_settings import load
            panel.follows(self.ctx, load(self.ctx.state_dir).topic_defaults, {}, 'oc_room')
            panel.follows(self.ctx, load(self.ctx.state_dir).topic_defaults, {})
        self.assertEqual(set(seen[:3]), {'oc_room'})
        self.assertEqual(set(seen[3:]), {self.app.runtime.access.owner_chat()})


class SpaceRecordTests(GroupFixture, UnifiedCase):
    """<state>/spaces/: each bound group's kind and title, for the 设置 card's 群聊 tab."""

    def setUp(self):
        super().setUp()
        self.group_setup()
        self.state = self.ctx.state_dir

    def test_a_record_keeps_its_title_and_is_written_only_when_it_changes(self):
        first = spaces.save_space(self.state, 'oc_room', spaces.PERSONAL, '项目群')
        self.assertEqual((first['kind'], first['title']), ('personal', '项目群'))
        path = spaces.space_path(self.state, 'oc_room')
        stamp = path.stat().st_mtime_ns
        self.assertEqual(spaces.save_space(self.state, 'oc_room', spaces.PERSONAL), first)  # '' keeps the title.
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        spaces.save_space(self.state, 'oc_room', spaces.TEAM)
        self.assertEqual(spaces.read_space(self.state, 'oc_room')['kind'], 'team')
        self.assertEqual([r['chat_id'] for r in spaces.list_spaces(self.state)], ['oc_room'])
        self.assertEqual(spaces.read_space(self.state, 'oc_none'), {})

    def test_the_menu_thread_notes_each_personal_group_with_its_title_and_none_by_default(self):
        self.access.lookup = mock.Mock(return_value={'name': '项目群'})
        self.app.group_menu.tick(self.ctx)
        self.assertEqual(spaces.list_spaces(self.state), [])
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))
        self.app.messenger.create_and_send_card = mock.Mock(return_value=('c', 'om_menu'))
        self.app.messenger.pin = mock.Mock(return_value=True)
        self.app.group_menu.tick(self.ctx)
        self.app.group_menu.tick(self.ctx)
        self.assertEqual([(r['chat_id'], r['kind'], r['title']) for r in spaces.list_spaces(self.state)],
                         [('oc_room', 'personal', '项目群')])
        self.assertEqual(self.access.lookup.call_count, 1)

    def test_the_group_tab_says_each_groups_space_type_only_with_a_spaces_section(self):
        manager = self.app.group_manager
        self.activate('group')  # A team space: the owner's @ there, then his word (app/group_manager.py ask).
        plain = json.dumps(manager.card({'token': 't' * 24, 'revision': 1, 'page': 0}, {}), ensure_ascii=False)
        self.assertNotIn('空间', plain)
        self.assertEqual(manager.listed()[1], None)
        setspaces(self.access, personal_groups=frozenset({'oc_room'}))
        groups, kinds = manager.listed()
        self.assertEqual(kinds, {'group': 'team', 'oc_room': 'personal'})
        card = json.dumps(manager.card({'token': 't' * 24, 'revision': 1, 'page': 0}, {'oc_room': '项目群'}),
                          ensure_ascii=False)
        self.assertIn('个人空间', card)
        self.assertIn('团队空间', card)
        self.assertIn('项目群', card)
        names = manager.lookup(self.ctx)['names']
        self.assertTrue(all(isinstance(v, str) for v in names.values()))
        self.assertEqual({r['chat_id']: r['kind'] for r in spaces.list_spaces(self.state)},
                         {'group': 'team', 'oc_room': 'personal'}) if names else None


class PersonalGroupAccessTests(GroupFixture, UnifiedCase):
    def setUp(self):
        super().setUp()
        self.group_setup()
        setspaces(self.access, personal_groups=frozenset({'group'}))

    def test_the_owner_talks_freely_in_a_personal_group_and_a_member_is_not_served(self):
        self.assertTrue(self.access.talks_freely('owner', 'group'))
        self.assertFalse(self.access.talks_freely(MEMBER, 'group'))
        self.assertFalse(self.access.talks_freely('owner', 'oc_other'))

    def test_without_the_section_the_owner_needs_the_at_as_before(self):
        setspaces(self.access, personal_groups=frozenset())
        self.assertFalse(self.access.talks_freely('owner', 'group'))

    def test_a_notify_space_group_has_no_board_of_its_own(self):
        setspaces(self.access, personal_groups=frozenset({'group', 'oc_room'}), notify_space='oc_room')
        self.assertEqual(self.access.board_groups(), frozenset({'group'}))
        self.assertEqual(self.access.agent_chat('oc_room', True), True)


class ButlerSpacesTests(ButlerUnifiedCase):
    def test_a_private_chat_of_another_account_is_saved_once_and_not_the_owners(self):
        self.access = self.app.runtime.access
        setspaces(self.access, owner_accounts=frozenset({ALIAS}))
        self.send('om_1')
        self.send('om_2', chat_id='oc_alias', sender_id=ALIAS)
        self.assertEqual(self.access.owner_chat(), 'oc_main')
        self.assertEqual(self.access.account_chats(), {ALIAS: 'oc_alias'})
        self.assertEqual(self.access.space_of('oc_alias'), spaces.PERSONAL)

    def test_without_other_accounts_no_chat_is_saved_for_them(self):
        access = self.app.runtime.access
        self.send('om_1')
        self.assertEqual((access.account_chats(), access.agent_chat('oc_alias')), ({}, False))
        self.assertTrue(access.agent_chat('oc_main'))

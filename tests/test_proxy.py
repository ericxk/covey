"""Proxy replies (代答): the records, ordinary messages that used to be commands, taking in the whitelisted
chats' messages, one topic per chat, and sending each answer as the owner exactly once. The 代答设置 card and the
重新发送 card are in test_proxy_panel, the send tool's side in test_proxy_send."""
import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import require_extension  # noqa: E402
require_extension()  # The tests run the company's sender (core/extensions.py hook proxy_sender); test_proxy_nosender is the core's.
from support.fake_lark import FakeCardKit  # noqa: E402
from support.proxy import (  # noqa: E402
    GROUP, OWNER, PERSON, RECEIPT, SENT, ProxyCase, controls, failure, item, owner_cli)
from support.records import write_topic
from covey.app import proxy as proxy_module  # noqa: E402
from covey.app import proxy_intake as intake_module  # noqa: E402
from covey.core import proxy as records  # noqa: E402
from covey.core.jobs import find_job_record, upsert_job_record  # noqa: E402
from covey.core.topics import read_topic  # noqa: E402
from covey_internal import proxy_sender as proxy_send  # noqa: E402


class RecordTests(unittest.TestCase):
    def test_a_declined_reply_suggests_what_its_last_suggestion_line_says(self):
        self.assertEqual(records.suggested_reply('他问预算，要你定。\n建议回复：“我看下预算再回你”'), '我看下预算再回你')
        self.assertEqual(records.suggested_reply('原因\n建议回： 好的\n明天给你'), '好的\n明天给你')  # How it wrote one before.
        self.assertEqual(records.suggested_reply('建议回复：先别回\n理由\n建议的回复：好'), '好')  # The last one.
        self.assertEqual(records.suggested_reply('涉及审批，留给主人'), '')
        batch = {'messages': [{'time': '2026-10-28 10:00', 'content': '能批吗', 'sender_name': '张三', 'chat_id': 'oc_p2p',
                               'id': 'm1'}]}
        for prompt in (records.batch_prompt(PERSON, batch, {}), records.NO_QUESTION):
            self.assertIn('“建议回复：”', prompt)

    def test_a_group_message_counts_only_when_it_ats_the_owner(self):
        person = records.incoming(item('m1', 'hi', chat_type='p2p'))
        self.assertTrue(records.addressed(PERSON, person, OWNER))
        self.assertFalse(records.addressed(PERSON, records.incoming(item('m2', 'hi', sender=OWNER, chat_type='p2p')), OWNER))
        at_me = records.incoming(item('m3', '@胥可 看下', chat='oc_g', mentions=[OWNER], chat_type='group'))
        at_all = records.incoming(item('m4', '@所有人 看下', chat='oc_g', mentions=['all'], chat_type='group'))
        self.assertTrue(records.addressed(GROUP, at_me, OWNER))
        self.assertFalse(records.addressed(GROUP, at_all, OWNER))
        # T452: what a whitelisted person says in a group is the group's, never their private chat's, @ or not.
        for said in (records.incoming(item('m5', '这是 ai 画的吗', chat='oc_g', chat_type='group')), at_me,
                     records.incoming(item('m6', 'hi', chat='oc_p2p'))):  # Of no known chat: not taken either.
            self.assertFalse(records.addressed(PERSON, said, OWNER))

    def test_skip_mark_and_empty_replies_are_not_sent(self):
        self.assertIsNone(records.skip_reason('好的，明天给你'))
        self.assertEqual(records.skip_reason('[不回复]\n涉及审批'), '涉及审批')
        self.assertEqual(records.skip_reason('  '), '没有回复内容')

    def test_batches_move_only_forward(self):
        batch = records.new_batch(PERSON, [records.incoming(item('m1', 'hi'))])
        with mock.patch.object(records, 'write_batch'), \
                mock.patch.object(records, 'read_batch', return_value={**batch, 'status': 'sending'}):
            sent = records.move_batch(Path('/unused'), {**batch, 'status': 'sending'}, 'sent')
            for status in ('sending', 'held', 'received'):
                with self.assertRaises(ValueError):
                    records.move_batch(Path('/unused'), sent, status)

    def test_prompt_names_the_sender_and_how_to_decline(self):
        batch = records.new_batch(PERSON, [records.incoming(item('m1', '接口文档在哪'))])
        prompt = records.batch_prompt(PERSON, batch, {'owner_name': '胥可', 'user_profile': 'me'})
        self.assertIn('张三 在私聊发来', prompt)
        self.assertIn('接口文档在哪', prompt)
        self.assertIn(records.SKIP_MARK, prompt)
        self.assertIn(records.NO_REPLY_MARK, prompt)  # Wrap-ups need no reply (2026-10-27).
        self.assertIn('lark-cli --profile me', prompt)
        self.assertIn('requires_user 写 false', prompt)  # Never waits for the owner (2026-10-27).
        self.assertTrue(records.batch_prompt(PERSON, batch, {}, '主人风格：很短').endswith('主人风格：很短'))

    def test_search_times_carry_a_colon_offset_and_cli_errors_are_one_line(self):
        from covey.app import proxy_send as proxy
        self.assertRegex(proxy.iso(1793076000), r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$')
        failed = mock.Mock(returncode=1, stderr='', stdout='{\n  "ok": false,\n  "error": {\n    "message": "--start: cannot parse"\n  }\n}\n')
        # Through transport.cli_json (repo review R7): its subprocess.run, which user_cli passes on.
        with mock.patch('covey.app.proxy_send.subprocess.run', return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, '^--start: cannot parse$'):
                proxy.user_cli('me', 'im', '+messages-search')
        run.assert_called_once()

    def test_names_are_split_by_spaces_commas_and_ats_and_a_phrase_is_kept_whole(self):
        self.assertEqual(records.parse_names('苏越、张信宇 不存在的人'),
                         [('', ['苏越']), ('张信宇 不存在的人', ['张信宇', '不存在的人'])])
        self.assertEqual(records.parse_names('@张三 @李四 @王五'), [('', ['张三', '李四', '王五'])])
        self.assertEqual(records.parse_names('C队 验收测试'), [('C队 验收测试', ['C队', '验收测试'])])
        self.assertEqual(records.parse_names('zhang.san@example.com，ou_x;\noc_y；张三, 张三'),
                         [('', ['zhang.san@example.com']), ('', ['ou_x']), ('', ['oc_y']), ('', ['张三'])])
        self.assertEqual(records.parse_names(' ,， \n'), [])
        names = ' '.join(f'人{i}' for i in range(records.NAMES + 3))
        kept, left = records.first_names(records.parse_names(names))
        self.assertEqual((sum(len(w) for _, w in kept), left, kept[0][0]), (records.NAMES, 3, ''))

    def test_a_contact_folder_is_read_again_only_when_it_changed_and_each_reader_gets_a_copy(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = Path(folder.name)
        records.write_contact(state, dict(PERSON))
        first = records.read_contacts(state)
        first[0]['name'] = 'changed by a caller'
        self.assertEqual(records.read_contacts(state)[0]['name'], '张三')
        with mock.patch.object(records, 'read_json', side_effect=AssertionError('read again')):
            self.assertEqual(len(records.read_contacts(state)), 1)  # Unchanged: from the cache.
        # Written by another writer (not write_contact): the folder's mtime tells, once the file
        # system's clock has ticked (tmpfs: every few milliseconds).
        from covey.core.state import write_json
        time.sleep(.05)
        write_json(records.contact_path(state, GROUP['key']), dict(GROUP))
        self.assertEqual([c['key'] for c in records.read_contacts(state)], ['p2p:ou_zhang', 'group:oc_g'])
        records.remove_contact(state, GROUP['key'])
        self.assertEqual(len(records.read_contacts(state)), 1)


class TextTests(ProxyCase):
    """There are no 代答 text commands (the bot menu, 2026-11-03): each is an ordinary message."""

    def test_adding_by_message_is_an_ordinary_message(self):
        # 2026-10-27: the owner dropped adding from a message (代答 添加 …, @s included); it is a normal task.
        with owner_cli() as cli:
            self.receive('m-add', '代答 添加 李四')
        cli.assert_not_called()
        app, _ = self.route('m-add')
        app.submission.selection_card.assert_called_once()  # A new topic's launch card: a task, not a command.
        self.assertEqual([c['target'] for c in records.read_contacts(self.state)], ['ou_zhang'])

    def test_the_old_commands_are_tasks(self):
        for index, text in enumerate(('代答', '代答 暂停 张三', '代答 删除 1', '代答 关闭')):
            self.receive(f'm-old{index}', text)
            app, _ = self.route(f'm-old{index}')
            app.submission.selection_card.assert_called()
        self.app.messenger.send_message.assert_not_called()
        self.assertEqual([c.get('paused') for c in records.read_contacts(self.state)], [False])
        self.assertTrue(records.read_settings(self.state).get('enabled'))

    def test_enable_records_the_group_lark_cli_prints_over_many_lines(self):
        records.write_settings(self.state, {'user_profile': 'me', 'harness': 'codex', 'workspace': 'project',
                                            'workdir': str(self.work)})
        me = {'data': {'users': [{'open_id': OWNER, 'localized_name': '胥可'}]}}
        created = mock.Mock(returncode=0, stdout='{\n  "ok": true,\n  "data": {\n    "chat_id": "oc_new"\n  }\n}\n')
        with owner_cli(return_value=me), \
             mock.patch('covey.app.proxy_commands.run_cli', return_value=created):
            reply = self.proxy.commands.cmd_enable(self.ctx, '', '', '')
        self.assertIn('代答已开启', reply)
        settings = records.read_settings(self.state)
        self.assertEqual((settings['group_chat_id'], settings['enabled'], settings['owner_user_id']), ('oc_new', True, OWNER))

    def enable_with(self, chats, created=None):
        """开启代答 once: the owner's chats named the group's name are ``chats``; lark-cli's +chat-create
        answers ``created`` (a value, or an exception to raise)."""
        me = {'data': {'users': [{'open_id': OWNER, 'localized_name': '胥可'}]}}

        def cli(profile, *args, timeout=60):
            return {'data': {'chats': chats}} if '+chat-search' in args else me
        create = (mock.Mock(side_effect=created) if isinstance(created, BaseException)
                  else mock.Mock(return_value=created))
        with owner_cli(side_effect=cli), mock.patch('covey.app.proxy_commands.run_cli', create):
            try:
                return self.proxy.commands.cmd_enable(self.ctx, '', '', ''), create
            except RuntimeError as exc:
                return exc, create

    def test_a_group_creation_of_unknown_outcome_is_found_by_name_and_never_made_twice(self):
        # Hard rule 1: a second 开启代答 after a timed-out +chat-create made a second 「C队 代答」.
        records.write_settings(self.state, {'user_profile': 'me', 'harness': 'codex', 'workspace': 'project',
                                            'workdir': str(self.work)})
        said, create = self.enable_with([], subprocess.TimeoutExpired(['lark-cli'], 60))
        self.assertIsInstance(said, RuntimeError)
        self.assertIn('按群名', str(said))
        self.assertEqual(create.call_count, 1)
        self.assertIn('group_creating', records.read_settings(self.state))
        said, create = self.enable_with([])
        self.assertIn('没找到', str(said))  # Said on the card; nothing is created.
        create.assert_not_called()
        self.assertNotIn('group_chat_id', records.read_settings(self.state))
        said, create = self.enable_with([{'chat_id': 'oc_made', 'name': records.GROUP_NAME},
                                         {'chat_id': 'oc_old', 'name': records.GROUP_NAME + '（旧）'}])
        self.assertIn('代答已开启', said)
        create.assert_not_called()
        settings = records.read_settings(self.state)
        self.assertEqual((settings['group_chat_id'], settings['enabled']), ('oc_made', True))
        self.assertNotIn('group_creating', settings)

    def test_a_group_creation_feishu_refused_is_tried_again_on_the_next_click(self):
        records.write_settings(self.state, {'user_profile': 'me', 'harness': 'codex', 'workspace': 'project',
                                            'workdir': str(self.work)})
        refused = mock.Mock(returncode=1, stdout='', stderr=json.dumps(
            {'ok': False, 'error': {'type': 'api', 'code': 232043, 'message': 'no permission'}}))
        said, _ = self.enable_with([], refused)
        self.assertEqual(str(said), '建群失败：no permission')
        self.assertNotIn('group_creating', records.read_settings(self.state))
        said, create = self.enable_with([], mock.Mock(returncode=0, stdout='{"data": {"chat_id": "oc_new"}}'))
        self.assertIn('代答已开启', said)
        create.assert_called_once()
        self.assertEqual(records.read_settings(self.state)['group_chat_id'], 'oc_new')

    def test_a_group_creation_whose_lark_cli_never_started_is_tried_again_on_the_next_click(self):
        # An OSError from subprocess.run (lark-cli not found, no memory to fork) means nothing reached Feishu.
        records.write_settings(self.state, {'user_profile': 'me', 'harness': 'codex', 'workspace': 'project',
                                            'workdir': str(self.work)})
        said, _ = self.enable_with([], BlockingIOError(11, 'Resource temporarily unavailable'))
        self.assertEqual(str(said), '建群失败：BlockingIOError')
        self.assertNotIn('group_creating', records.read_settings(self.state))
        said, create = self.enable_with([], mock.Mock(returncode=0, stdout='{"data": {"chat_id": "oc_new"}}'))
        self.assertIn('代答已开启', said)
        create.assert_called_once()
        self.assertEqual(records.read_settings(self.state)['group_chat_id'], 'oc_new')

    def test_other_messages_still_start_tasks(self):
        self.receive('m-task', '代答功能有个 bug')
        app, _ = self.route('m-task')
        app.submission.selection_card.assert_called_once()  # A new topic's launch card: a task, not a command.


class FlowTests(ProxyCase):
    def test_new_messages_become_one_batch_and_are_taken_once(self):
        self.take_in(item('m1', '在吗', position='7'), item('m2', '接口文档在哪', position='8'))
        self.take_in(item('m1', '在吗', position='7'))
        [batch] = self.batches()
        self.assertEqual([m['id'] for m in batch['messages']], ['m1', 'm2'])
        self.assertEqual(records.read_contacts(self.state)[0]['seen'], ['m1', 'm2'])

    def test_private_messages_are_one_search_whatever_the_whitelist_and_kept_only_from_it(self):
        for index in range(300):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_{index}', 'target': f'ou_{index}',
                                               'name': f'人{index}', 'added_at': 10 + index})
        records.write_contact(self.state, dict(GROUP))
        payload = {'data': {'has_more': True, 'messages': [
            item('m1', '在吗'), item('m2', '我自己发的', sender=OWNER), item('m3', '不在白名单', sender='ou_stranger'),
            item('m4', '第 7 个人', sender='ou_7', chat='oc_7')]}}
        with owner_cli(side_effect=lambda profile, *args: (
                {'data': {'messages': []}} if '--is-at-me' in args else payload)) as cli:
            self.search_in()
        searches = [c.args[1:] for c in cli.call_args_list]
        self.assertEqual(len(searches), 2)  # One for the groups' @s, one for every private chat.
        [p2p] = [s for s in searches if '--chat-type' in s]
        self.assertNotIn('--sender', p2p)
        self.assertEqual(p2p[p2p.index('--sender-type') + 1], 'user')
        self.assertIn('--page-all', p2p)
        self.assertEqual(sorted((b['contact'], [m['id'] for m in b['messages']]) for b in self.batches()),
                         [('p2p:ou_7', ['m4']), ('p2p:ou_zhang', ['m1'])])
        self.assertTrue(any(line.startswith('proxy search truncated') for line in self.logs))

    def test_a_batch_runs_in_its_contacts_bound_topic_without_a_launch_card(self):
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        [batch] = self.batches()
        self.assertEqual(batch['status'], 'submitted')
        contact = records.read_contacts(self.state)[0]
        self.assertEqual(contact['root_id'], 'om_root')
        _, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, 'om_mirror')
        data = read_topic(target)
        self.assertEqual((data['harness_id'], data['model'], data['proxy']['contact']), ('codex', 'codex-model', 'p2p:ou_zhang'))
        self.assertTrue(data['workspace_locked'])
        job = self.app.runtime.jobs.by_id[batch['job_id']]
        self.assertEqual((job.message_id, job.chat_id), ('om_mirror', 'oc_proxy'))
        self.assertIn('接口文档在哪', job.prompt_text)
        self.app.apps['codex'].submission.selection_card.assert_not_called()

    def test_a_job_carries_the_owners_style(self):
        (records.proxy_dir(self.state) / records.STYLE_FILE).write_text('主人风格：很短，不寒暄\n', encoding='utf-8')
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        [batch] = self.batches()
        self.assertTrue(self.app.runtime.jobs.by_id[batch['job_id']].prompt_text.endswith('主人风格：很短，不寒暄'))

    def test_a_proxy_topics_checkpoint_question_is_dropped_not_asked(self):
        # T185/T186: the model asked the owner whether to send its draft and the reply waited for them.
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        contact = records.read_contacts(self.state)[0]
        app, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, 'om_mirror')
        checkpoint = self.root / 'checkpoint.json'
        checkpoint.write_text(json.dumps({'phase': '草稿', 'requires_user': True, 'question': '是否按草稿回复？',
                                          'questions': [{'id': 'q', 'question': '是否按草稿回复？'}]}))
        job = mock.Mock(checkpoint_path=str(checkpoint), job_id=7, message_id='om_mirror')
        with mock.patch.object(app.runs.questions, 'defer_checkpoint_question') as defer:
            app.runs._leave_checkpoint_question(target, job, mock.Mock(outcome=None, is_error=False, text='好的'))
        defer.assert_not_called()
        saved = json.loads(checkpoint.read_text())
        self.assertEqual((saved['requires_user'], saved['question'], 'questions' in saved), (False, '', False))
        from covey.app.runs import proxy_topic
        self.assertEqual((proxy_topic(target), proxy_topic(self.route('m-plain')[1])), (True, False))

    def test_a_proxy_tasks_artifacts_leave_out_its_contacts_notes(self):
        # #1060, #1070, #1121 (2026-10-27): each 代答 card offered the contact's README.md, uploaded again each
        # turn; since the move to Codex the run data beside it too (#1151: six chat dumps).
        from covey.app.artifacts import drive_paths
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        _, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), records.read_contacts(self.state)[0], 'om_mirror')
        notes = self.work / 'misc-tasks' / 'sessions' / '20261027-proxy-p2p-cbfe23dc'
        runs = self.work / 'misc-tasks' / 'runs' / '20261027-proxy-p2p-cbfe23dc' / '20261028-013342-9ffa' / 'inputs'
        report = self.work / 'misc-tasks' / 'runs' / '20261021-rl-trial-status-32280270' / 'rl-trial-links.md'
        for path in (notes / 'README.md', runs / 'latest_chat.json', report):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x')
        checkpoint = self.root / 'artifacts-checkpoint.json'
        checkpoint.write_text(json.dumps({'artifacts': [str(notes / 'README.md'), str(runs / 'latest_chat.json'), str(report)]}))
        record = {'job_id': 1, 'workdir': str(self.work), 'checkpoint_path': str(checkpoint),
                  'history_path': str(target.history_path)}
        self.assertEqual(drive_paths(record), [str(report)])
        plain = self.route('m-plain')[1]  # Any other task keeps them.
        self.assertEqual(len(drive_paths({**record, 'history_path': str(plain.history_path)})), 3)

    def test_a_proxy_topic_s_cards_offer_the_model_switch_before_its_group_was_ever_activated(self):
        # #1060 (2026-10-27 16:4x) had 🔀 分叉 and 💬 侧聊 (off every card since 2026-11-08): the proxy group got its
        # group record only at 17:15.
        from covey.app.cards import Cards
        from covey.core.jobs import BridgeReplyJob
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        app, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), records.read_contacts(self.state)[0], 'om_mirror')
        self.assertFalse(app.runtime.access.group('oc_proxy'))
        job = BridgeReplyJob(job_id=7, user_text='task', chat_id='oc_proxy', message_id='om_run', timeout=0,
                             topic_id=target.topic_id, harness_id='codex', binding_revision=1)
        with FakeCardKit({'data': {'card_id': 'run-card'}}, sent=('run-msg', False)) as lark:
            Cards.create_streaming_reply(app.cards, target, job, 'queued')
        running = json.dumps(lark.last.created, ensure_ascii=False)
        self.assertIn('running_switch', running)
        self.assertNotIn('side_chat_action', running)
        with mock.patch.object(app.cards, 'set_task_element', return_value=True) as drawn:
            app.switch.attach_topic_controls(target, SimpleNamespace(progress=object(), topic_controls_attached=False,
                                                                     job_id=7, chat_id='oc_proxy'))
        finished = json.dumps(drawn.call_args.args[2], ensure_ascii=False)
        self.assertIn('bridge_switch_open', finished)
        self.assertNotIn('rewind_action', finished)  # Its replies went to the contact.

    def test_an_answer_is_sent_once_and_shown_in_the_topic(self):
        self.submitted()
        sent = self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_args.args[4], '文档在 wiki 首页')
        [batch] = self.batches(statuses=None)
        self.assertEqual((batch['status'], batch['sent_message_id']), ('sent', 'om_sent'))
        # The reply is the answer just above it in the topic; the line says when it went out (UI review P8).
        self.assertEqual(self.notes()[-1], '✅ 已代你回复（上一条，09:41 发出）')
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()

    def test_a_wrap_up_needs_no_reply_and_closes_without_waiting_for_the_owner(self):
        # 2026-10-27, the owner: “一会有个会” got “行 那你先忙…”; a chat need not end with them.
        self.submitted()
        self.run_tick('[无需回复]\n对方只是说要去开会', {'ok': True}).assert_not_called()
        [batch] = self.batches(statuses=None)
        self.assertEqual((batch['status'], batch['reason']), ('skipped', '不用回：对方只是说要去开会'))
        # Repo review U5: the reason is in the turn's answer just above, said once.
        self.assertEqual(self.notes()[-1], f"· 不用回（原因见上面 #{batch['job_id']} 的回答）")
        self.assertIsNone(records.no_reply_needed('好的，明天给你'))
        self.assertEqual(records.no_reply_needed('[无需回复]'), '')

    def test_a_declined_answer_waits_for_the_owner_then_their_turn_is_sent(self):
        batch = self.submitted()
        self.run_tick('[不回复]\n涉及排期承诺', {'ok': True}).assert_not_called()
        self.assertEqual(self.batches()[0]['status'], 'held')
        # Repo review U5: the reason is in the turn's answer just above; the line points there.
        self.assertEqual(self.notes()[-1], f"⏸ 没有代你回复（原因见上面 #{batch['job_id']} 的回答）\n"
                                           "要回的话，在本话题 @机器人 告诉我怎么回。")
        upsert_job_record(self.state, 'owner-turn', status='done', job_id=batch['job_id'] + 1,
                          topic_id=batch['topic_id'], message_id='om_owner', chat_id='oc_proxy')
        self.run_tick('下周三前给你', {'ok': True, 'submitted': True})
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'sent')

    def test_a_question_answered_on_its_card_waits_quietly_for_the_follow_up(self):
        # T186 (2026-10-27): the model asked the owner, who answered; the batch said it would not reply.
        batch = self.submitted(status='waiting_user')
        self.run_tick(None, {'ok': True}).assert_not_called()  # Still asking: the batch waits.
        self.assertEqual(self.batches()[0]['status'], 'submitted')
        upsert_job_record(self.state, batch['job_key'], status='continued')
        before = len(self.notes())
        self.run_tick(None, {'ok': True}).assert_not_called()
        self.assertEqual(self.batches()[0]['status'], 'held')
        self.assertEqual(len(self.notes()), before)
        upsert_job_record(self.state, 'answer-turn', status='done', job_id=batch['job_id'] + 1,
                          topic_id=batch['topic_id'], message_id='om_answer', chat_id='oc_proxy')
        self.run_tick('没听说这事，具体啥问题？', {'ok': True, 'submitted': True})
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'sent')

    def test_a_task_the_owner_stopped_on_its_card_holds_the_batch_without_a_note(self):
        # 苏越 17:31: the owner stopped #1071; "⏸ 没有代你回复：任务没有可用的回复（cancelled）" followed.
        batch = self.submitted(status='cancelled')
        upsert_job_record(self.state, batch['job_key'], cancel_requested_by='card')
        before = len(self.notes())
        self.run_tick(None, {'ok': True}).assert_not_called()
        self.assertEqual((self.batches()[0]['status'], self.batches()[0]['reason']), ('held', '你停止了任务'))
        self.assertEqual(len(self.notes()), before)

    def test_a_task_cancelled_some_other_way_is_still_said(self):
        self.submitted(status='cancelled')
        self.run_tick(None, {'ok': True}).assert_not_called()
        self.assertIn('任务没有可用的回复（cancelled）', self.notes()[-1])

    def test_a_held_batch_gives_way_to_a_newer_one_so_one_reply_is_not_sent_twice(self):
        # 苏越 16:24/16:25: a batch taken in while the first still ran; both held, both waiting for one turn.
        first = self.submitted()
        self.take_in(item('m2', '还有个事', position='9'))
        # The first gives way at once and says no ⏸ line: the newer batch, which carries its messages, says what
        # comes of them (repo review U5: T186 #1176 and #1177 each said the same ⏸ line, reason and suggestion).
        self.assertEqual(sorted(b['status'] for b in self.batches(statuses=None)), ['received', 'superseded'])
        self.run_tick('[不回复]\n你还在聊', {'ok': True})
        self.assertFalse(any('没有代你回复' in note for note in self.notes()))
        statuses = {b['id'] == first['id']: b['status'] for b in self.batches(statuses=None)}
        self.assertEqual(statuses, {True: 'superseded', False: 'submitted'})

    def test_a_send_that_never_pressed_enter_is_tried_again_then_waits_for_the_owners_turn(self):
        # 张信宇 T190 (2026-10-27): three sends never pressed Enter; the batch ended as failed, and the owner's
        # 重新回复 in the topic then sent nothing. It now stays held and the owner's next turn is sent.
        batch = self.submitted()
        failure = {'ok': False, 'submitted': False, 'error': '网页端未登录'}
        for attempt in range(records.SEND_ATTEMPTS + 1):  # The last poll waits for the owner: no fourth try.
            sent = self.run_tick('好的', failure)
        sent.assert_not_called()
        [held] = self.batches(statuses=None)
        self.assertEqual((held['status'], held['attempts'], held['send_failed']), ('held', records.SEND_ATTEMPTS, True))
        # Said on a 重新发送 card under the batch's messages (2026-10-27, the owner), not in a line of text.
        self.assertFalse(any('代答没有发出' in note for note in self.notes()))
        [create] = [call for call in self.lark.calls if call.method == 'POST']
        text = json.dumps(create.created, ensure_ascii=False)
        for said in ('代答没有发出', '没发出：网页端未登录（试了 3 次），张三没收到。', '好的', '重新发送',
                     '在本话题 @机器人 说“重新回复”', '在代答卡上点“网页端登录”'):
            self.assertIn(said, text)
        self.assertEqual(self.lark.send.call_args.kwargs['reply_to_message_id'], 'om_mirror')
        self.assertEqual(held['resend_card'], {'card_id': 'rcid', 'message_id': 'om_resend'})
        self.assertIn('上一条回复没有发出去（网页端未登录）', records.owner_turn_note(self.state, PERSON['key'], 'om_owner'))
        self.assertIn('好的', records.owner_turn_note(self.state, PERSON['key'], 'om_owner'))
        upsert_job_record(self.state, 'owner-turn', status='done', job_id=batch['job_id'] + 1,
                          topic_id=batch['topic_id'], message_id='om_owner', chat_id='oc_proxy')
        sent = self.run_tick('好的', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_count, 1)
        [done] = self.batches(statuses=None)
        self.assertEqual((done['status'], done['attempts'], done['reply_job_id']), ('sent', 1, batch['job_id'] + 1))
        # The owner's turn sent it: said in the topic as always, and the card no longer offers its button.
        self.assertIn('✅ 已代你回复', self.notes()[-1])
        put = [call for call in self.lark.calls if call.method == 'PUT'][-1]
        self.assertIn('✅ 已发出', json.dumps(put.card, ensure_ascii=False))
        self.assertEqual(controls(put.card), [])

    def test_a_model_chosen_on_the_card_moves_idle_contact_topics_over(self):
        # 2026-10-27, the owner: one model for all of 代答, existing contact topics included.
        batch = self.submitted()
        contact = records.read_contacts(self.state)[0]
        _, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, contact['root_id'])
        self.proxy.commands.set_model(self.ctx, 'claude', 'claude-model', 'high')
        self.run_tick('好的', {'ok': True, 'submitted': True})  # Its batch was under way: the topic waits.
        self.assertEqual(read_topic(target)['harness_id'], 'codex')
        self.app.runtime.jobs.unregister(self.app.runtime.jobs.by_id[batch['job_id']])
        self.run_tick(None, {'ok': True})  # Idle now: moved over, the way 换配置 does, and said so.
        data = read_topic(target)
        self.assertEqual((data['harness_id'], data['model'], data['effort']), ('claude', 'claude-model', 'high'))
        self.assertEqual((data['switches'][-1]['from']['harness_id'], data['binding_revision']), ('codex', 2))
        self.assertIn('代答模型已换成 claude / claude-model / high', self.notes()[-1])
        self.assertTrue(records.read_contacts(self.state)[0]['model_synced_at'])
        # A topic switched by hand afterwards keeps its own choice, until the card changes the model again.
        data.update(harness_id='codex', model='codex-model', effort='high')
        write_topic(target, data)
        self.run_tick(None, {'ok': True})
        self.assertEqual(read_topic(target)['harness_id'], 'codex')
        self.proxy.commands.set_model(self.ctx, 'claude', 'claude-model', 'high')  # Same model again: no change.
        self.run_tick(None, {'ok': True})
        self.assertEqual(read_topic(target)['harness_id'], 'codex')

    def test_a_send_that_may_have_gone_out_is_never_sent_again(self):
        self.submitted()
        self.run_tick('好的', {'ok': False, 'submitted': True, 'error': '超时'}, read_back={})
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'uncertain')
        self.assertIn('不会自动重发', self.notes()[-1])
        self.run_tick('好的', {'ok': True, 'submitted': True}).assert_not_called()

    def test_the_owners_own_reply_cancels_the_send(self):
        self.submitted()
        self.run_tick('好的', {'ok': True}, owner_replied=True).assert_not_called()
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'skipped')

    def test_a_send_interrupted_by_a_restart_is_only_read_back(self):
        batch = self.submitted()
        records.move_batch(self.state, batch, 'sending', reply='好的', send_started_at=time.time())
        self.run_tick('好的', {'ok': True}, read_back={}).assert_not_called()
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'uncertain')

    def test_new_messages_replace_a_held_batch(self):
        self.submitted()
        self.run_tick('[不回复]', {'ok': True})
        self.take_in(item('m9', '算了，我问别人', position='9'))
        statuses = sorted(b['status'] for b in self.batches(statuses=None))
        self.assertEqual(statuses, ['received', 'superseded'])

    def test_a_paused_contacts_finished_reply_is_held_not_sent_and_resuming_sends_nothing_by_itself(self):
        batch = self.submitted()
        self.proxy.commands.set_paused(self.ctx, '张三', True)
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()
        [held] = self.batches()
        self.assertEqual((held['status'], held['reason']), ('held', '已暂停对 张三 的代答'))
        self.assertIn('⏸ 没有代你回复：已暂停对 张三 的代答。\n要回的话，恢复代答后在本话题', self.notes()[-1])
        self.proxy.commands.set_paused(self.ctx, '张三', False)
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()
        self.owner_turn(batch)  # The owner says how to answer it.
        self.run_tick('下周给你', {'ok': True, 'submitted': True}).assert_called_once()

    def test_switched_off_open_batches_move_on_without_a_send_and_a_new_one_is_dropped(self):
        first = self.submitted()
        records.write_batch(self.state, {**first, 'restarts': 2})  # MAX_RESTARTS stopped in a row: runs to its end.
        self.take_in(item('m2', '还在吗', position='9'))  # Waits behind the submitted one.
        self.proxy.commands.cmd_disable(self.ctx, '', '', '')
        with mock.patch.object(self.proxy.intake, 'search') as search:
            self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()
        search.assert_not_called()
        self.assertEqual(sorted((b['status'], b.get('reason')) for b in self.batches(statuses=None)),
                         [('held', '代答已关闭'), ('skipped', '代答已关闭')])
        self.assertIn('开启代答后在本话题', self.notes()[-1])
        # Off, the worker still polls while a batch is open; with none open it does nothing.
        with mock.patch('covey.app.proxy.POLL_SECONDS', 0), \
             mock.patch.object(self.proxy, 'tick', side_effect=lambda *a: self.app.runtime.shutdown.set()) as tick:
            self.proxy.run(self.ctx)
        tick.assert_called_once()
        self.app.runtime.shutdown.clear()

    def test_a_removed_contacts_reply_is_dropped_and_not_sent_when_it_is_added_again(self):
        self.submitted()
        self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertEqual([(b['status'], b['reason']) for b in self.batches(statuses=None)], [('skipped', records.REMOVED)])
        records.write_contact(self.state, dict(PERSON))
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()

    def readd(self):
        """张三 removed on the settings card and added again before the next poll: a new record, with
        no topic_id or root_id yet."""
        self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
        self.proxy.commands.add_contacts(self.ctx, self.settings(),
                                [{'kind': 'p2p', 'target': 'ou_zhang', 'name': '张三', 'chat_id': 'oc_p2p'}])

    def li_waits(self):
        """李四, added after 张三, has a message waiting: his batch comes after 张三's."""
        self.cards = iter(['om_root_li', 'om_mirror_li', 'om_x'])
        records.write_contact(self.state, {**PERSON, 'key': 'p2p:ou_li', 'target': 'ou_li', 'name': '李四',
                                           'chat_id': 'oc_li', 'added_at': 5})
        self.take_in(item('m-li', '在吗', sender='ou_li', chat='oc_li', position='3', name='李四'))

    def li(self):
        return [b['status'] for b in self.batches(statuses=None) if b['contact'] == 'p2p:ou_li']

    def test_a_contact_added_back_before_the_next_poll_drops_its_old_batches_and_holds_up_no_other(self):
        for status in ('held', 'submitted'):
            with self.subTest(status=status):
                for path in records.proxy_dir(self.state).glob('*/*.json'):
                    path.unlink()  # Each case from the start: 张三 with one batch.
                records.write_contact(self.state, dict(PERSON))
                self.cards = iter(['om_root', 'om_mirror'])
                self.submitted()
                if status == 'held':
                    self.run_tick('[不回复]\n涉及审批', {'ok': True})
                [batch] = self.batches()
                self.assertEqual(batch['status'], status)
                self.readd()
                self.li_waits()
                sent = self.run_tick('好的', {'ok': True, 'submitted': True})
                self.assertEqual((records.read_batch(self.state, batch['id'])['status'],
                                  records.read_batch(self.state, batch['id'])['reason']), ('skipped', records.REMOVED))
                self.assertEqual(self.li(), ['submitted'])
                sent.assert_not_called()  # The old batch's reply is never sent to the contact added again.

    def test_a_batch_whose_step_fails_holds_up_no_other(self):
        self.submitted()
        self.run_tick('[不回复]\n涉及审批', {'ok': True})
        self.li_waits()
        with mock.patch.object(self.proxy, 'check_held', side_effect=KeyError('topic_id')):
            self.run_tick('好的', {'ok': True, 'submitted': True})
        self.assertEqual(self.li(), ['submitted'])
        self.assertTrue(any("proxy batch=" in line and "KeyError" in line for line in self.logs))

    def test_a_pause_while_the_send_is_being_checked_stops_it(self):
        self.submitted()

        def pausing(*_):
            self.proxy.commands.set_paused(self.ctx, '张三', True)  # On the card, while the chat is being read.
            return False
        with mock.patch.object(self.proxy, 'reply_of', return_value='好的'), \
             mock.patch.object(self.proxy.web, 'owner_replied', side_effect=pausing), \
             mock.patch.object(self.proxy.web, 'web_send', return_value={'ok': True, 'submitted': True}) as sent, \
             mock.patch.object(self.proxy.web, 'read_back', return_value=SENT):
            self.proxy.tick(self.ctx, self.settings())
        sent.assert_not_called()
        self.assertEqual(self.batches()[0]['status'], 'held')

    def test_a_late_reply_of_the_batchs_own_job_is_held_but_the_owners_turn_still_sends(self):
        batch = self.submitted(at=time.time() - records.STALE_AFTER - 3600)
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertEqual(self.batches()[0]['status'], 'held')
        self.assertIn('小时前的，这么晚的回复不自动发', self.notes()[-1])
        self.owner_turn(batch)
        self.run_tick('文档在 wiki 首页', {'ok': True, 'submitted': True}).assert_called_once()

    def test_a_batch_the_owner_answered_in_the_chat_runs_no_job_and_shows_nothing(self):
        self.take_in(item('m1', '接口文档在哪', position='7'))
        [batch] = self.batches()
        chat = {'data': {'messages': [item('m-own', '在 wiki', sender=OWNER, position='8')]}}
        with owner_cli(return_value=chat):
            self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        self.assertEqual([(b['status'], b['reason']) for b in self.batches(statuses=None)], [('skipped', '你已亲自回复')])
        self.app.messenger.create_and_send_card.assert_not_called()
        self.assertEqual(self.app.runtime.jobs.by_id, {})

    def test_a_reply_that_may_have_gone_out_is_not_taken_for_the_owners_own(self):
        first = self.submitted()
        self.run_tick('文档在 wiki 首页', {'ok': False, 'submitted': True, 'error': '超时'}, read_back={})
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'uncertain')
        self.take_in(item('m2', '还有个事', position='9'))
        later = next(b for b in self.batches() if b['id'] != first['id'])
        chat = {'data': {'messages': [item('m-out', '文档在 wiki 首页', sender=OWNER, position='10')]}}
        with owner_cli(return_value=chat):
            self.assertFalse(self.proxy.web.owner_replied(self.ctx, self.settings(), records.read_contacts(self.state)[0], later))
            chat['data']['messages'].append(item('m-own', '我晚点看', sender=OWNER, position='11'))
            self.assertTrue(self.proxy.web.owner_replied(self.ctx, self.settings(), records.read_contacts(self.state)[0], later))

    def test_a_contact_added_back_goes_on_in_its_earlier_topic_on_that_topics_harness(self):
        # 2026-10-27 17:44: C队 验收测试 added back; its topic root's key was used at 13:07, the transport
        # refused it again with another card, and every poll failed on that batch.
        self.cards = iter(['om_root', 'om_mirror', 'om_card3', 'om_card4'])
        first = self.submitted()
        old = records.read_contacts(self.state)[0]
        self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
        self.proxy.commands.set_model(self.ctx, 'claude', 'claude-model', 'high')
        records.write_contact(self.state, {**PERSON, 'added_at': time.time(), 'since': time.time()})
        self.take_in(item('m2', '又来问了', position='9'))
        [batch] = self.batches(statuses=('received',))
        self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
        contact = records.read_contacts(self.state)[0]
        self.assertEqual((contact['root_id'], contact['topic_id']), (old['root_id'], old['topic_id']))
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 3)  # Root, mirror, mirror: no new root.
        [batch] = [b for b in self.batches(statuses=('submitted',)) if b['id'] != first['id']]
        self.assertEqual(batch['topic_id'], first['topic_id'])
        app, _, _ = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, batch['mirror_id'])
        self.assertEqual(app.harness.spec.name, 'codex')  # Bound when it opened; the new model binds new topics.
        self.assertIn('proxy topic found again', ' '.join(self.logs))

    def test_a_batch_that_cannot_move_on_holds_up_no_other(self):
        records.write_contact(self.state, {**PERSON, 'key': 'p2p:ou_li', 'target': 'ou_li', 'name': '李四',
                                           'chat_id': 'oc_li', 'added_at': 5})
        self.take_in(item('m1', '在吗', position='7'), item('m2', '在吗', sender='ou_li', chat='oc_li', position='3'))
        roots = iter([('', ''), ('card', 'om_root_li'), ('card', 'om_mirror_li')])  # 张三's topic will not open.
        self.app.messenger.create_and_send_card.side_effect = lambda *a, **k: next(roots)
        with mock.patch.object(self.proxy.intake, 'search', return_value=[]):
            self.proxy.tick(self.ctx, self.settings())
        self.assertEqual(sorted((b['contact'], b['status']) for b in self.batches()),
                         [('p2p:ou_li', 'submitted'), ('p2p:ou_zhang', 'received')])
        self.assertTrue(any(line.startswith('proxy batch=') and '代答话题的起始消息发送失败' in line for line in self.logs))

    def test_a_contact_removed_while_its_topic_opens_is_not_written_back(self):
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()

        def removing(*args, **kwargs):
            self.proxy.commands.cmd_remove(self.ctx, '张三', '', '')
            return 'card', 'om_root'
        self.app.messenger.create_and_send_card.side_effect = removing
        self.proxy.submit(self.ctx, self.settings(), dict(PERSON), batch)
        self.assertEqual(records.read_contacts(self.state), [])
        self.assertEqual(self.batches()[0]['status'], 'received')

    def test_a_job_that_could_not_be_submitted_is_not_tried_every_poll(self):
        self.take_in(item('m1', '接口文档在哪'))
        [batch] = self.batches()
        with mock.patch.object(self.app.apps['codex'].submission, 'enqueue_chat_job', return_value=None) as enqueue:
            self.proxy.submit(self.ctx, self.settings(), records.read_contacts(self.state)[0], batch)
            self.run_tick('好的', {'ok': True})
        enqueue.assert_called_once()
        self.assertEqual([(b['status'], b['reason']) for b in self.batches(statuses=None)], [('skipped', '任务未提交')])

    def test_a_private_reply_goes_to_the_chat_a_groups_quotes_its_message_and_a_threads_stays_in_it(self):
        # STATE3 (2026-10-28): sent by the sender tool; a group's @ inside a thread is answered inside it.
        self.take_in(item('m1', '在吗', position='7'))
        [batch] = self.batches()
        group = {**batch, 'messages': [records.incoming(item('m3', '@胥可 看下', chat='oc_g', position='12', mentions=[OWNER]))]}
        thread = {**batch, 'messages': [records.incoming(item('m4', '@胥可 看下', chat='oc_g', position='-3', thread='omt_1',
                                                              mentions=[OWNER]))]}
        unknown = {**batch, 'messages': [{**batch['messages'][0], 'chat_id': ''}]}
        patch, runs = self.tool_runs(*[RECEIPT] * 4)
        with patch:
            result = self.proxy.web.web_send(self.ctx, self.settings(), dict(PERSON), batch, ' 收到 \n')
            self.proxy.web.web_send(self.ctx, self.settings(), {**PERSON, 'chat_id': ''}, unknown, '收到')
            self.proxy.web.web_send(self.ctx, self.settings(), GROUP, group, '我看看')
            self.proxy.web.web_send(self.ctx, self.settings(), GROUP, thread, '我看看')
        self.assertEqual((result['ok'], result['message_id']), (True, 'om_tool'))
        key = proxy_send.idempotency_key(batch, '收到')
        self.assertEqual(runs[0].args, ['+messages-send', '--chat-id', 'oc_p2p', '--text', '@reply.txt',
                                        '--idempotency-key', key, '--no-login'])
        # The text from a file of its own: "-" or "@a.txt" as an argument would be read as one.
        self.assertEqual((runs[0].text, runs[0].timeout), ('收到', proxy_send.SEND_TIMEOUT))
        self.assertEqual(runs[1].args[:3], ['+messages-send', '--user-id', 'ou_zhang'])  # Its chat not known yet.
        self.assertEqual(runs[2].args[:3], ['+messages-reply', '--message-id', 'm3'])
        self.assertNotIn('--reply-in-thread', runs[2].args)
        self.assertEqual(runs[3].args[:4], ['+messages-reply', '--message-id', 'm4', '--reply-in-thread'])
        self.assertFalse(any(Path(run.cwd).exists() for run in runs))
        self.assertIn(f"proxy send batch={batch['id'][:8]} ok=True type=None submitted=True message_id=om_tool", self.logs)

    def test_an_at_tag_in_a_reply_goes_out_as_plain_text(self):
        # Repo review F1: the tool turns <at user_id="all">所有人</at> into a real @所有人, as the owner.
        cases = {'<at user_id="all">所有人</at> 请看': '@所有人 请看',
                 '找 <at user_id="ou_3a6b">张三</at>！': '找 @张三！',
                 '<at user_id="7012345678">李四</at>': '@李四',
                 "<AT User_Id = '123' >@王五</At >": '@王五',
                 '< at\nuser_id="all" >\n所有人\n< / at >': '@所有人',
                 '<at user_id="all"/>和<at user_id="ou_x" />': '@all和@ou_x',
                 '<at user_id="1"><at user_id="all">所有人</at></at>': '@@所有人',
                 'a < b > c，<atom>x</atom>，user_id="all"': 'a < b > c，<atom>x</atom>，user_id="all"'}
        for text, plain in cases.items():
            self.assertEqual(proxy_send.plain_mentions(text), plain, text)
        self.take_in(item('m1', '在吗', position='7'))
        [batch] = self.batches()
        patch, runs = self.tool_runs(RECEIPT)
        with patch:
            self.proxy.web.web_send(self.ctx, self.settings(), dict(PERSON), batch, '<at user_id="all">所有人</at> 收到')
        self.assertEqual(runs[0].text, '@所有人 收到')

    def test_a_reply_is_saved_and_sent_with_its_at_tags_as_plain_text(self):
        # Read back and the owner's own replies are told apart by the saved text: it is the text that went out.
        batch = self.submitted()
        sent = self.run_tick('<at user_id="all">所有人</at> 文档在 wiki 首页', {'ok': True, 'submitted': True})
        self.assertEqual(sent.call_args.args[4], '@所有人 文档在 wiki 首页')
        done = records.read_batch(self.state, batch['id'])
        self.assertEqual((done['status'], done['reply']), ('sent', '@所有人 文档在 wiki 首页'))

    def test_the_key_is_the_batch_and_its_reply(self):
        batch = {'id': 'f' * 64}
        key = proxy_send.idempotency_key(batch, '好的')
        self.assertLessEqual(len(key), 50)
        self.assertEqual(key, proxy_send.idempotency_key(batch, ' 好的\n'))  # Tried again: the tool's record decides.
        self.assertNotEqual(key, proxy_send.idempotency_key(batch, '好的，明天给你'))  # The owner's new reply: another send.
        self.assertNotEqual(key, proxy_send.idempotency_key({'id': 'e' * 64}, '好的'))

    def test_an_at_inside_a_groups_thread_is_answered_in_the_thread(self):
        # It was left to the owner (Feishu web quoted only the main list); the tool replies inside the thread.
        records.write_contact(self.state, dict(GROUP))
        records.remove_contact(self.state, PERSON['key'])
        group = records.read_contacts(self.state)[0]
        self.take_in(item('m1', '@胥可 看下', chat='oc_g', position='-3', thread='omt_1', mentions=[OWNER]))
        self.proxy.submit(self.ctx, self.settings(), group, records.read_batches(self.state)[0])
        [batch] = self.batches()
        upsert_job_record(self.state, batch['job_key'], status='done', job_id=batch['job_id'], topic_id=batch['topic_id'],
                          message_id=batch['mirror_id'], chat_id='oc_proxy')
        self.run_tick('我看看', {'ok': True, 'submitted': True, 'message_id': 'om_tool'}).assert_called_once()
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'sent')
        self.assertFalse(any('话题回复里' in note for note in self.notes()))

    def test_a_receipt_is_sent_by_its_own_id_without_a_read_back(self):
        self.submitted()
        self.run_tick('好的', {'ok': True, 'submitted': True, 'message_id': 'om_tool',
                             'create_time': str(int(time.mktime(time.strptime(time.strftime('%Y-%m-%d 09:41'), '%Y-%m-%d %H:%M'))) * 1000)},
                      read_back={})
        [batch] = self.batches(statuses=None)
        self.assertEqual((batch['status'], batch['sent_message_id']), ('sent', 'om_tool'))
        self.assertEqual(self.notes()[-1], '✅ 已代你回复（上一条，09:41 发出）')

    def test_a_receipt_the_owners_chat_does_not_show_is_still_sent_and_not_the_owners_answer(self):
        batch = self.submitted()
        self.run_tick('好的', {'ok': True, 'submitted': True, 'message_id': ''}, read_back={})
        self.assertEqual([(b['status'], b['sent_message_id']) for b in self.batches(statuses=None)], [('sent', '')])
        later = {**batch, 'id': 'b-later', 'messages': [records.incoming(item('m9', '在吗', position='5'))]}
        with owner_cli(return_value={'data': {'messages': [item('m-own', '好的', sender=OWNER, position='8')]}}):
            self.assertFalse(self.proxy.web.owner_replied(self.ctx, self.settings(), records.read_contacts(self.state)[0], later))

    def test_a_receipt_without_its_om_is_read_once_and_on_later_polls_never_waited_for(self):
        # Repo review F5: four reads 15 s apart held every other batch up for 45 s.
        self.submitted()
        with mock.patch.object(self.proxy, 'reply_of', return_value='好的'), \
             mock.patch.object(self.proxy.web, 'owner_replied', return_value=False), \
             mock.patch.object(self.proxy.web, 'web_send', return_value={'ok': True, 'submitted': True, 'message_id': ''}), \
             mock.patch.object(self.proxy.runtime.shutdown, 'wait', return_value=False) as waited, \
             owner_cli(return_value={'data': {'messages': []}}) as cli:
            self.proxy.tick(self.ctx, self.settings())
        waited.assert_not_called()
        self.assertEqual(cli.call_count, 1)
        self.assertEqual([(b['status'], b['sent_message_id']) for b in self.batches(statuses=None)], [('sent', '')])
        # A later poll reads it again and keeps its om_; then no more reads.
        mine = item('m-sent', '好的', sender=OWNER, position='9')
        with owner_cli(return_value={'data': {'messages': [mine]}}) as cli:
            self.proxy.tick(self.ctx, self.settings())
            self.proxy.tick(self.ctx, self.settings())
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(self.batches(statuses=None)[0]['sent_message_id'], 'm-sent')
        # A thread's reply is not in the chat's list: not read at all.
        records.write_contact(self.state, dict(GROUP))
        thread = {'id': 'b-thread', 'contact': GROUP['key'], 'status': 'sending', 'reply': '我看看', 'created_at': time.time(),
                  'send_started_at': time.time(), 'history': [],
                  'messages': [records.incoming(item('m5', '@胥可 看下', chat='oc_g', position='-3', mentions=[OWNER]))]}
        records.write_batch(self.state, thread)
        with owner_cli(return_value={'data': {'messages': []}}) as cli:
            self.proxy.finish_send(self.ctx, self.settings(), GROUP, thread, {'ok': True, 'submitted': True, 'message_id': ''})
        cli.assert_not_called()
        self.assertEqual((records.read_batch(self.state, 'b-thread')['status'], self.proxy.web.reads), ('sent', {}))

    def test_a_slow_search_holds_up_no_chats_list(self):
        # Repo review F7: the search's two calls, up to 60 s each, ran before the next round's lists.
        release, searches = threading.Event(), []

        def cli(profile, *args, timeout=60):
            if '+chat-messages-list' in args:
                return {'data': {'messages': [item('m1', '在吗', position='7')]}}
            searches.append(args)
            release.wait(10)
            return {'data': {'messages': [item('m2', '接口文档在哪', position='8')]}}
        with owner_cli(side_effect=cli):
            try:
                round_ = threading.Thread(target=self.proxy.intake.listen_once, args=(self.ctx, self.settings()),
                                          kwargs={'search': True}, daemon=True)
                round_.start()
                round_.join(5)
                self.assertFalse(round_.is_alive())  # The list's round is done while the search waits.
                self.assertEqual([m['id'] for m in self.proxy.intake.inbox[PERSON['key']]], ['m1'])
                self.proxy.intake.listen_once(self.ctx, self.settings(), search=True)  # The last search still runs: no other.
                deadline = time.monotonic() + 5
                while not searches and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(searches), 1)
            finally:
                release.set()
            self.proxy.intake.searching.join(10)
        self.assertEqual([m['id'] for m in self.proxy.intake.inbox[PERSON['key']]], ['m1', 'm2'])

    def test_a_group_reply_is_read_back_by_the_message_it_quoted(self):
        records.write_contact(self.state, dict(GROUP))
        batch = {'id': 'b-group', 'contact': GROUP['key'], 'reply': '我看看', 'send_started_at': time.time(),
                 'messages': [records.incoming(item('m1', '@胥可 看下这个', chat='oc_g', position='5', mentions=[OWNER])),
                              records.incoming(item('m2', '', chat='oc_g', position='6', kind='image', mentions=[OWNER]))]}
        chat = {'data': {'messages': [{**item('m-sent', '我看看', chat='oc_g', sender=OWNER, position='7'), 'reply_to': 'm1'}]}}
        with owner_cli(return_value=chat):
            self.assertEqual(self.proxy.web.read_back(self.settings(), GROUP, batch)['id'], 'm-sent')

    def test_the_owners_own_turn_is_told_its_reply_goes_to_the_contact_while_a_batch_waits(self):
        batch = self.submitted()
        contact = records.read_contacts(self.state)[0]
        app, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, 'om_owner')

        def prompt(message_id):
            job = mock.Mock(kind='chat', prompt_text='为什么这么回', user_text='为什么这么回', message_id=message_id)
            return app.runs._turn_message(target, job, 'session-1')[1]
        self.run_tick('[不回复]\n涉及排期', {'ok': True})  # Held: the owner's next turn answers it.
        self.assertIn('这一轮的最终回复会原样以主人的身份发给张三', prompt('om_owner'))
        self.assertIn('第一行只写 [不回复]', prompt('om_owner'))
        self.assertNotIn('（代答：', prompt(batch['mirror_id']))  # A batch's own job has the rules in its prompt.
        self.take_in(item('m2', '算了', position='9'))
        self.run_tick('[不回复]', {'ok': True})  # The held one gives way to the new one, now submitted.
        [waiting] = self.batches(statuses=records.OPEN)
        self.assertIn('（代答：', prompt('om_owner'))  # Should that one be held, this turn's reply is its reply.
        records.move_batch(self.state, waiting, 'skipped')
        self.assertNotIn('（代答：', prompt('om_owner'))  # Nothing waits: the turn's reply goes nowhere.

    def test_a_native_question_in_a_proxy_topic_is_refused_at_once_except_where_refusing_ends_the_run(self):
        from covey.app.native_questions import NativeQuestions
        self.submitted()
        contact = records.read_contacts(self.state)[0]
        app, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, 'om_mirror')
        rpc = mock.Mock()
        job = mock.Mock(job_id=9, message_id='om_mirror', checkpoint_path='')
        with mock.patch.object(app.runs.questions.native, 'register', return_value=None) as register:
            app.runs.questions.native.dispatch(target, job, 'question', {'request_id': 'r1', 'questions': [{}]}, rpc)
        register.assert_not_called()
        rpc.reject_question.assert_called_once_with('r1', records.NO_QUESTION)
        self.assertIn('proxy job=#9 native question refused', self.logs)
        strict = NativeQuestions(SimpleNamespace(spec=SimpleNamespace(name='strict', refuses_questions=False)),
                                 runtime=self.app.runtime, cards=None, questions=None)
        self.assertFalse(strict.refused_in_proxy_topic(target, job, {'request_id': 'r2'}, rpc))  # Refusing ends its run.
        plain = self.route('m-plain')[1]
        self.assertFalse(app.runs.questions.native.refused_in_proxy_topic(plain, job, {'request_id': 'r3'}, rpc))
        rpc.reject_question.assert_called_once()

    def turn(self, batch, step, status):
        """The owner's turn ``step`` after the batch's own job in its topic, now ``status``."""
        upsert_job_record(self.state, f'owner-turn-{step}', status=status, job_id=batch['job_id'] + step,
                          topic_id=batch['topic_id'], message_id=f'om_owner{step}', chat_id='oc_proxy')

    def poll(self, replies, web=None):
        """A poll where each job's saved reply is ``replies`` {its step after the batch's own job: reply}."""
        first = min(int(r['job_id']) for r in records.read_batches(self.state, statuses=None) if r.get('job_id'))
        with mock.patch.object(self.proxy, 'reply_of', side_effect=lambda c, s, k, r: replies[int(r['job_id']) - first]), \
             mock.patch.object(self.proxy.web, 'owner_replied', return_value=False), \
             mock.patch.object(self.proxy.web, 'web_send', return_value=web or {'ok': True, 'submitted': True}) as sent, \
             mock.patch.object(self.proxy.web, 'read_back', return_value=SENT):
            self.proxy.tick(self.ctx, self.settings())
        return [call.args[4] for call in sent.call_args_list]

    def test_the_owners_turn_while_the_batchs_own_job_runs_is_the_reply_that_goes_out(self):
        # Repo review D2, the owner's choice (2026-10-28): their newer turn wins. They say “回他：…” in the topic
        # while the batch's own job runs; that turn queues behind it and is told its reply goes to 张三. The
        # batch's own reply went out instead, and the turn's never did, unsaid.
        batch = self.submitted(status='running')
        contact = records.read_contacts(self.state)[0]
        app, _, target = self.proxy.contact_topics.topic_context(self.ctx, self.settings(), contact, 'om_owner1')
        job = mock.Mock(kind='chat', prompt_text='回他：明天下午给你', user_text='回他：明天下午给你', message_id='om_owner1')
        self.assertIn('这一轮的最终回复会原样以主人的身份发给张三', app.runs._turn_message(target, job, 'session-1')[1])
        self.turn(batch, 1, 'queued')
        upsert_job_record(self.state, batch['job_key'], status='done')  # Its own reply saved: “好的我看下”.
        replies = {0: '好的我看下', 1: '明天下午给你'}
        self.assertEqual(self.poll(replies), [])
        held = records.read_batch(self.state, batch['id'])
        self.assertEqual((held['status'], held['reason'], held['considered_job_id']), ('held', records.LATER_TURN, batch['job_id']))
        self.assertIn(f"proxy passed over batch={batch['id'][:8]} job=#{batch['job_id']}: "
                      f"the later turn #{batch['job_id'] + 1} in its topic answers it", self.logs)
        self.turn(batch, 1, 'running')
        self.assertEqual(self.poll(replies), [])  # Waits for that turn.
        self.turn(batch, 1, 'done')
        before = len(self.notes())
        self.assertEqual(self.poll(replies), ['明天下午给你'])
        done = records.read_batch(self.state, batch['id'])
        self.assertEqual((done['status'], done['reply_job_id']), ('sent', batch['job_id'] + 1))
        self.assertEqual(self.notes()[before:], ['✅ 已代你回复（上一条，09:41 发出）'])  # Nothing else said.

    def test_an_owners_turn_that_sends_nothing_leaves_the_batch_held_and_says_so_once(self):
        # D2, where the owner only asked something (“为什么查这么久”): the rules as for any held batch, and the
        # reply passed over is said not to have gone out.
        batch = self.submitted(status='running')
        self.turn(batch, 1, 'done')
        upsert_job_record(self.state, batch['job_key'], status='done')
        replies = {0: '好的我看下', 1: '[不回复]\n在查 wiki', 2: '[不回复]\n还是先别回', 3: '我看下再回你'}
        before = len(self.notes())
        for poll in range(2):
            self.assertEqual(self.poll(replies), [])
        self.assertEqual(self.notes()[before:], [
            f"⏸ 没有代你回复：#{batch['job_id']} 的回复没有发（之后你在本话题又说了话），#{batch['job_id'] + 1} "
            "这一轮也没有要发给对方的回复。\n要回的话，在本话题 @机器人 告诉我怎么回。"])
        self.turn(batch, 2, 'done')
        self.assertEqual(self.poll(replies), [])
        self.assertEqual(len(self.notes()), before + 1)  # Once: a turn that sends nothing is quiet as before.
        self.turn(batch, 3, 'done')
        self.assertEqual(self.poll(replies), ['我看下再回你'])

    def test_a_batchs_own_job_that_declines_while_the_owner_spoke_is_said_as_before(self):
        batch = self.submitted(status='running')
        self.turn(batch, 1, 'queued')
        upsert_job_record(self.state, batch['job_key'], status='done')
        replies = {0: '[不回复]\n涉及审批', 1: '下周给你'}
        self.assertEqual(self.poll(replies), [])
        self.assertIn(f"⏸ 没有代你回复（原因见上面 #{batch['job_id']} 的回答）", self.notes()[-1])
        self.turn(batch, 1, 'done')
        self.assertEqual(self.poll(replies), ['下周给你'])

    def test_of_two_owner_turns_while_a_batch_waits_the_newer_ones_reply_goes_out(self):
        # D2 between the owner's own turns: each was told its reply goes out; the later one is the owner's last word.
        batch = self.submitted()
        self.assertEqual(self.poll({0: '[不回复]\n涉及审批'}), [])  # Held for the owner.
        self.turn(batch, 1, 'done')
        self.turn(batch, 2, 'running')
        replies = {0: '[不回复]', 1: '明天给你', 2: '明天下午三点前给你'}
        self.assertEqual(self.poll(replies), [])
        self.assertIn(f"proxy passed over batch={batch['id'][:8]} job=#{batch['job_id'] + 1}: "
                      f"the later turn #{batch['job_id'] + 2} in its topic answers it", self.logs)
        self.turn(batch, 2, 'done')
        self.assertEqual(self.poll(replies), ['明天下午三点前给你'])

    def test_an_owners_turn_past_the_hourly_limit_goes_out_once_the_hour_allows(self):
        # Repo review D14: past HOURLY_LIMIT the turn was marked considered and its reply dropped, unsaid.
        batch = self.submitted()
        self.assertEqual(self.poll({0: '[不回复]\n涉及审批'}), [])  # Held for the owner.
        now = time.time()
        for i in range(records.HOURLY_LIMIT):  # The oldest leaves the hour in about ten minutes.
            records.write_batch(self.state, {'id': f'{i:032x}', 'contact': PERSON['key'], 'status': 'sent', 'created_at': 0,
                                             'send_started_at': now - 3000 + i * 60, 'messages': []})
        self.turn(batch, 1, 'done')
        replies = {0: '[不回复]', 1: '明天给你'}
        before = len(self.notes())
        for poll in range(2):
            self.assertEqual(self.poll(replies), [])
        held = records.read_batch(self.state, batch['id'])
        self.assertEqual((held['status'], held['considered_job_id']), ('held', batch['job_id']))  # Not past the turn.
        at = time.strftime('%H:%M', time.localtime(now - 3000 + 3600))
        self.assertEqual(self.notes()[before:], [f'⏸ 一小时内已给对方代答 {records.HOURLY_LIMIT} 条，这条回复 {at} 后自动发出。'])
        oldest = records.read_batch(self.state, f'{0:032x}')
        records.write_batch(self.state, {**oldest, 'send_started_at': now - 3700})  # The hour has moved on.
        self.assertEqual(self.poll(replies), ['明天给你'])
        self.assertEqual(records.read_batch(self.state, batch['id'])['status'], 'sent')
        self.assertEqual(records.next_send_at([], PERSON['key'], now), now)

    def test_an_owners_turn_while_the_tools_login_runs_goes_out_after_it(self):
        # Repo review F2: the login held web_lock, the turn was marked considered and its reply dropped, unsaid.
        batch = self.submitted()
        self.assertEqual(self.poll({0: '[不回复]\n涉及审批'}), [])  # Held for the owner.
        self.turn(batch, 1, 'done')
        replies = {0: '[不回复]', 1: '明天给你'}
        with self.proxy.web_lock:  # As cmd_login holds it.
            for poll in range(2):
                self.assertEqual(self.poll(replies), [])
        held = records.read_batch(self.state, batch['id'])
        self.assertEqual((held['status'], held['considered_job_id']), ('held', batch['job_id']))  # Not past the turn.
        self.assertEqual(self.poll(replies), ['明天给你'])
        self.assertEqual(records.read_batch(self.state, batch['id'])['status'], 'sent')

    def test_a_search_that_fails_still_lets_the_batches_move_on(self):
        # Repo review D13: 2026-10-28 03:29 “proxy tick failed: RuntimeError: lark-cli rc=-15”, and the whole
        # poll went: no reply ready was sent, no 重新发送 asked for, no send read back. The reads are listen's
        # now (T2 A): a poll reads nothing as the owner, and a failed read is logged there.
        self.submitted()
        with owner_cli(side_effect=RuntimeError('lark-cli rc=-15')) as cli, \
             mock.patch.object(self.proxy, 'reply_of', return_value='文档在 wiki 首页'), \
             mock.patch.object(self.proxy.web, 'owner_replied', return_value=False), \
             mock.patch.object(self.proxy.web, 'web_send', return_value={'ok': True, 'submitted': True}) as sent, \
             mock.patch.object(self.proxy.web, 'read_back', return_value=SENT):
            self.proxy.tick(self.ctx, self.settings())
            cli.assert_not_called()
            self.listen_round(search=True)
        sent.assert_called_once()
        self.assertEqual(self.batches(statuses=None)[0]['status'], 'sent')
        self.assertIn('proxy take-in failed source=search: RuntimeError: lark-cli rc=-15; tried again in 10s, '
                      'longer while it fails', self.logs)

    def listing(self, by_chat, searched=()):
        """The owner's lark-cli: +chat-messages-list of a chat answers ``by_chat[chat_id]``, a search ``searched``."""
        def cli(profile, *args, timeout=60):
            if '+chat-messages-list' in args:
                return {'data': {'messages': [dict(m) for m in by_chat.get(args[args.index('--chat-id') + 1], [])]}}
            return {'data': {'messages': [dict(m) for m in searched]}}
        return owner_cli(side_effect=cli)

    def inbox(self):
        return {key: [m['id'] for m in messages] for key, messages in self.proxy.intake.inbox.items()}

    def test_known_chats_are_listed_every_round_the_rest_searched_and_only_the_poll_writes_batches(self):
        # T2 A (2026-10-28): the search showed a message 17–19 s late, every 30 s plus the sends in that round;
        # a chat's list shows it at once. Read on listen's thread, written by the poll's (the only batch writer).
        records.write_contact(self.state, dict(GROUP))
        records.write_contact(self.state, {**PERSON, 'key': 'p2p:ou_li', 'target': 'ou_li', 'name': '李四', 'chat_id': '',
                                           'added_at': 3})
        listed = {'oc_p2p': [item('m0', '我自己说的', sender=OWNER, position='6'), item('m1', '在吗', position='7')],
                  'oc_g': [item('g1', '@胥可 看下', chat='', sender='ou_w', position='4', mentions=[OWNER]),
                           item('g2', '大家好', chat='', sender='ou_w', position='5')]}  # The list gives no chat_id.
        with self.listing(listed) as cli:
            self.proxy.intake.listen_once(self.ctx, self.settings(), search=False)
        self.assertTrue(all('+chat-messages-list' in c.args for c in cli.call_args_list))
        self.assertEqual(sorted(c.args[c.args.index('--chat-id') + 1] for c in cli.call_args_list), ['oc_g', 'oc_p2p'])
        self.assertEqual((self.inbox(), self.batches()), ({'p2p:ou_zhang': ['m1'], 'group:oc_g': ['g1']}, []))
        self.assertTrue(self.proxy.wake.is_set())
        with self.listing(listed):
            self.proxy.intake.listen_once(self.ctx, self.settings(), search=False)  # Found again before the poll: once.
        self.assertEqual(self.inbox(), {'p2p:ou_zhang': ['m1'], 'group:oc_g': ['g1']})
        with owner_cli() as cli, mock.patch.object(self.proxy, 'submit'):
            self.proxy.tick(self.ctx, self.settings())
        cli.assert_not_called()  # A poll reads nothing as the owner.
        self.assertEqual(sorted((b['contact'], [m['id'] for m in b['messages']]) for b in self.batches()),
                         [('group:oc_g', ['g1']), ('p2p:ou_zhang', ['m1'])])
        self.assertEqual(self.proxy.intake.inbox, {})
        # The search: 李四's chat is not known yet, and a reply inside a thread is in no list.
        searched = [item('l1', '你好', sender='ou_li', chat='oc_li', position='2'), item('m1', '在吗', position='7'),
                    item('g3', '@胥可 这里', chat='oc_g', sender='ou_w', position='-3', thread='omt_1', mentions=[OWNER])]
        with self.listing(listed, searched) as cli:
            self.listen_round(search=True)
        self.assertEqual(sum('+messages-search' in c.args for c in cli.call_args_list), 2)
        self.assertEqual(self.inbox(), {'p2p:ou_li': ['l1'], 'group:oc_g': ['g3']})
        # A message taken in already and found again by a read that started before it was: not taken twice.
        self.proxy.intake.inbox['p2p:ou_zhang'] = [records.incoming(item('m1', '在吗', position='7'))]
        with mock.patch.object(self.proxy, 'submit'):
            self.proxy.tick(self.ctx, self.settings())
        self.assertEqual([m['id'] for b in self.batches() if b['contact'] == 'p2p:ou_zhang' for m in b['messages']], ['m1'])
        # Off: nothing is listed, and what was found is not taken in.
        self.proxy.intake.inbox['p2p:ou_zhang'] = [records.incoming(item('m5', '还在吗', position='9'))]
        self.proxy.commands.cmd_disable(self.ctx, '', '', '')
        with owner_cli() as cli, mock.patch.object(self.proxy.runtime.shutdown, 'wait', return_value=True):
            self.proxy.intake.listen(self.ctx)
            self.proxy.tick(self.ctx, self.settings())
        cli.assert_not_called()
        self.assertNotIn('m5', records.read_contacts(self.state)[0]['seen'])

    def test_a_whitelisted_person_speaking_in_a_whitelisted_group_is_the_groups_only(self):
        # T452 (2026-11-09): 张三 is whitelisted for their private chat and is in a whitelisted group. Matched by
        # sender alone, what they said in the group without an @ went into their private batch, and its reply
        # out into the group (as the last message's chat: 10-29, a question to someone else answered there).
        records.write_contact(self.state, dict(GROUP))
        records.write_contact(self.state, {**PERSON, 'key': 'p2p:ou_li', 'target': 'ou_li', 'name': '李四', 'chat_id': '',
                                           'added_at': 3})
        listed = {'oc_p2p': [item('m1', '在吗', chat='', position='7')],
                  'oc_g': [item('g1', '这是 ai 画的吗', chat='', position='4'),
                           item('g2', '@胥可 看下', chat='', position='5', mentions=[OWNER]),
                           item('g3', '李四在群里说', chat='', sender='ou_li', name='李四', position='6')]}
        with self.listing(listed):
            self.proxy.intake.listen_once(self.ctx, self.settings(), search=False)
        self.assertEqual(self.inbox(), {'p2p:ou_zhang': ['m1'], 'group:oc_g': ['g2']})
        # The searches: the groups' @s (a reply in a thread, which no list shows) and the private chats.
        searched = [item('g4', '@胥可 这里', chat='oc_g', position='-3', thread='omt_1', mentions=[OWNER], chat_type='group'),
                    item('g5', '李四 @胥可', chat='oc_g', sender='ou_li', name='李四', position='8', mentions=[OWNER],
                         chat_type='group')]
        with self.listing({}, searched):
            self.listen_round(search=True)
        self.assertEqual(self.inbox(), {'p2p:ou_zhang': ['m1'], 'group:oc_g': ['g4', 'g2', 'g5']})  # In position order.
        with mock.patch.object(self.proxy, 'submit'):
            self.proxy.tick(self.ctx, self.settings())
        contacts = {c['key']: c for c in records.read_contacts(self.state)}
        self.assertEqual(contacts['p2p:ou_li']['chat_id'], '')  # Never the group's: a send goes where it says.
        self.assertEqual({m['chat_type'] for b in self.batches() for m in b['messages'] if b['contact'] == 'group:oc_g'},
                         {'group'})

    def test_what_was_found_just_before_proxy_went_off_is_dropped_with_nothing_open(self):
        self.proxy.intake.inbox['p2p:ou_zhang'] = [records.incoming(item('m5', '还在吗', position='9'))]
        self.proxy.commands.cmd_disable(self.ctx, '', '', '')
        with mock.patch.object(self.proxy, 'rest', side_effect=[False, True]), \
             mock.patch.object(self.proxy, 'tick') as tick:
            self.proxy.run(self.ctx)
        tick.assert_not_called()
        self.assertEqual(self.proxy.intake.inbox, {})

    def test_listen_searches_on_its_first_round_and_then_once_a_minute(self):
        rounds = []
        stop = iter([False, False, True])
        with mock.patch.object(self.proxy.intake, 'listen_once', side_effect=lambda ctx, s, search: rounds.append(search)), \
             mock.patch.object(self.proxy.runtime.shutdown, 'wait', side_effect=lambda seconds: next(stop)), \
             mock.patch.object(intake_module, 'time', SimpleNamespace(time=time.time, monotonic=mock.Mock(
                 side_effect=[100, 100, 130, 130, 161, 161]))):
            self.proxy.intake.listen(self.ctx)
        self.assertEqual(rounds, [True, False, True])

    def test_a_read_that_keeps_failing_backs_off_and_is_logged_once(self):
        with owner_cli(side_effect=RuntimeError('rate limited 99991400')) as cli:
            self.listen_round(search=True)
            self.assertEqual(cli.call_count, 2)  # The chat's list and the search.
            self.listen_round(search=True)
            self.assertEqual(cli.call_count, 2)  # Both wait.
            self.proxy.intake.failures = {source: (count, 0.0) for source, (count, _) in self.proxy.intake.failures.items()}
            self.proxy.intake.listed_at = {}  # Due again.
            self.listen_round(search=True)
            self.assertEqual(cli.call_count, 4)
        self.assertEqual(sorted(count for count, _ in self.proxy.intake.failures.values()), [2, 2])
        self.assertEqual(sum('proxy take-in failed' in line for line in self.logs), 2)  # Once a source.
        self.proxy.intake.failures = {source: (count, 0.0) for source, (count, _) in self.proxy.intake.failures.items()}
        self.proxy.intake.listed_at = {}
        with self.listing({'oc_p2p': [item('m1', '在吗')]}):
            self.listen_round(search=True)
        self.assertEqual((self.proxy.intake.failures, self.inbox()), ({}, {'p2p:ou_zhang': ['m1']}))
        self.assertEqual(sum('proxy take-in reads again' in line for line in self.logs), 2)
        # A source fails at most BACKOFF_LIMIT seconds' wait.
        self.proxy.intake.failures = {'search': (20, 0.0)}
        with owner_cli(side_effect=RuntimeError('rc=1')), \
             mock.patch.object(intake_module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: 0)):
            self.proxy.intake.guarded('search', lambda: intake_module.user_cli('me'), [])
        self.assertEqual(self.proxy.intake.failures['search'], (21, intake_module.BACKOFF_LIMIT))

    def test_a_read_after_failures_reaches_back_to_its_last_good_one(self):
        # 2026-11-03: four chats' lists failed for about 25 minutes, past LOOKBACK; only the search had them.
        clock = [100000.0]
        with mock.patch.object(intake_module, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])):
            self.assertEqual(self.proxy.intake.reach('list x'), clock[0] - intake_module.LOOKBACK)  # Nothing read yet.
            self.proxy.intake.guarded('list x', lambda: [], [])
            clock[0] += 1800
            self.assertEqual(self.proxy.intake.reach('list x'), 100000.0 - 60)
            clock[0] += 3 * intake_module.CATCH_UP
            self.assertEqual(self.proxy.intake.reach('list x'), clock[0] - intake_module.CATCH_UP)
            self.proxy.intake.guarded('list x', lambda: [], [])
            self.assertEqual(self.proxy.intake.reach('list x'), clock[0] - intake_module.LOOKBACK)

    def test_a_read_the_bridges_stop_ended_is_no_failure(self):
        # Every restart logged "proxy take-in failed … rc=-15" for the lists under way (44 lines over 11-03 and 04).
        with mock.patch.object(self.proxy.runtime.shutdown, 'is_set', return_value=True):
            read = mock.Mock(side_effect=RuntimeError('lark-cli rc=-15'))
            self.assertEqual(self.proxy.intake.guarded('search', read, []), [])
        self.assertEqual(self.proxy.intake.failures, {})
        self.assertFalse(any('proxy take-in failed' in line for line in self.logs))

    def rounds(self, seconds, by_chat=None, fail=()):
        """``listen_once`` once a second of a fake clock for ``seconds`` seconds: [(second, chat listed)]. A chat
        in ``fail`` answers a rate limit, the rest ``by_chat[chat_id]``."""
        calls, clock = [], [1000.0]

        def cli(profile, *args, timeout=60):
            chat = args[args.index('--chat-id') + 1]
            calls.append((int(clock[0] - 1000), chat))
            if chat in fail:
                raise RuntimeError('HTTP 429: 99991400 request trigger frequency limit')
            return {'data': {'messages': [dict(m) for m in (by_chat or {}).get(chat, [])]}}
        with owner_cli(side_effect=cli), \
             mock.patch.object(intake_module, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0])):
            for _ in range(seconds):
                self.proxy.intake.listen_once(self.ctx, self.settings(), search=False)
                clock[0] += 1
        return calls

    def whitelist(self, size):
        """Known chats oc_00… up to ``size`` with PERSON's."""
        for index in range(size - 1):
            records.write_contact(self.state, {**PERSON, 'key': f'p2p:ou_{index:02}', 'target': f'ou_{index:02}',
                                               'chat_id': f'oc_{index:02}', 'added_at': 10 + index})

    def test_a_known_chat_is_listed_every_3_seconds(self):
        # The owner (2026-10-28): why does it read only every 10 s. A round each second lists the chats due.
        self.assertEqual(self.rounds(10), [(0, 'oc_p2p'), (3, 'oc_p2p'), (6, 'oc_p2p'), (9, 'oc_p2p')])

    def test_a_chat_with_a_message_or_a_batch_in_the_last_5_minutes_is_listed_every_2_seconds(self):
        records.write_contact(self.state, dict(GROUP))
        calls = self.rounds(7, {'oc_p2p': [item('m1', '在吗')]})
        self.assertEqual([t for t, chat in calls if chat == 'oc_p2p'], [0, 2, 4, 6])  # A message taken in.
        self.assertEqual([t for t, chat in calls if chat == 'oc_g'], [0, 3, 6])
        # Five minutes on, every 3 s again; a batch under way (seen by the poll) makes the group active.
        self.proxy.intake.active['p2p:ou_zhang'] = time.time() - 301
        self.proxy.intake.inbox.clear()
        message = records.incoming(item('g1', '@胥可 看下', chat='oc_g', sender='ou_w', mentions=[OWNER]))
        records.write_batch(self.state, records.new_batch(GROUP, [message]))
        with mock.patch.object(self.proxy, 'submit'):
            self.proxy.tick(self.ctx, self.settings())
        self.proxy.intake.listed_at = {}
        calls = self.rounds(7)
        self.assertEqual([t for t, chat in calls if chat == 'oc_p2p'], [0, 3, 6])
        self.assertEqual([t for t, chat in calls if chat == 'oc_g'], [0, 2, 4, 6])

    def test_lists_stay_within_a_quarter_of_the_rate_limit_whatever_the_whitelist(self):
        # GET /open-apis/im/v1/messages allows 1000 a minute per app, shared with the owner's other uses: at most
        # 4 lists a round, 240 a minute; a longer whitelist takes turns, the longest waiting first.
        self.whitelist(8)
        self.assertEqual(len(self.rounds(60)), 160)  # 8 chats, each every 3 s.
        for size in (16, 41):
            self.whitelist(size)
            self.proxy.intake.listed_at = {}
            calls = self.rounds(60)
            per_round = {t: sum(1 for s, _ in calls if s == t) for t, _ in calls}
            self.assertEqual((len(calls), max(per_round.values())), (240, 4))
            times = {}
            for t, chat in calls:
                times.setdefault(chat, []).append(t)
            gaps = [b - a for listed in times.values() for a, b in zip(listed, listed[1:])]
            self.assertEqual((len(times), max(gaps)), (size, -(-size // 4)))  # Every chat, in turn.

    def test_a_chat_whose_list_is_rate_limited_backs_off_without_holding_up_the_others(self):
        records.write_contact(self.state, dict(GROUP))
        calls = self.rounds(31, fail={'oc_g'})
        self.assertEqual([t for t, chat in calls if chat == 'oc_g'], [0, 10, 30])  # 10 s, then 20 s.
        self.assertEqual([t for t, chat in calls if chat == 'oc_p2p'], list(range(0, 31, 3)))
        self.assertEqual(sorted(count for count, _ in self.proxy.intake.failures.values()), [3])
        self.assertEqual(sum('proxy take-in failed' in line for line in self.logs), 1)

    def test_a_job_of_a_proxy_topic_that_ends_wakes_the_poll_at_once(self):
        # T2 B: check_job ran only on the next poll, 0–30 s after the job ended.
        self.app.runtime.unregister_reply_job(SimpleNamespace(job_id=9, message_id='', chat_id='oc_other'))
        self.assertFalse(self.proxy.wake.is_set())
        polls = []

        def tick(ctx, settings):
            polls.append(time.monotonic())
            self.app.runtime.shutdown.set()
        self.addCleanup(self.app.runtime.shutdown.clear)
        self.addCleanup(self.app.runtime.shutdown.set)
        with mock.patch.object(self.proxy, 'tick', side_effect=tick):
            worker = threading.Thread(target=self.proxy.run, args=(self.ctx,), daemon=True)
            started = time.monotonic()
            worker.start()
            self.app.runtime.unregister_reply_job(SimpleNamespace(job_id=10, message_id='', chat_id='oc_proxy'))
            worker.join(5)
        self.assertEqual(len(polls), 1)
        self.assertLess(polls[0] - started, 5)  # Not POLL_SECONDS later.
        self.assertFalse(self.proxy.wake.is_set())
        # A listener that fails does not keep the job from ending.
        self.app.runtime.job_ended.insert(0, mock.Mock(side_effect=ValueError('x')))
        self.app.runtime.unregister_reply_job(SimpleNamespace(job_id=11, message_id='', chat_id='oc_proxy'))
        self.assertTrue(self.proxy.wake.is_set())
        self.assertIn('job end listener failed job=#11: ValueError: x', self.logs)

    def test_an_owners_message_before_a_batch_of_thread_replies_is_not_their_answer(self):
        # Repo review D9: a private batch of thread replies (position -3) took any message of the owner's from two
        # minutes before it for their answer, and was skipped unsaid.
        now = time.time()
        self.take_in(item('m1', '这个我看了，有个问题', position='-3', thread='omt_1', at=now))
        [batch] = self.batches()
        contact = records.read_contacts(self.state)[0]

        def replied(*messages):
            with owner_cli(return_value={'data': {'messages': list(messages)}}):
                return self.proxy.web.owner_replied(self.ctx, self.settings(), contact, batch)
        self.assertFalse(replied(item('m-own', '你看下这个方案', sender=OWNER, position='5', at=now - 60)))
        self.assertTrue(replied(item('m-own', '我晚点回你', sender=OWNER, position='6', at=now + 60)))
        self.assertTrue(replied(item('m-own', '收到', sender=OWNER, position='-3', thread='omt_1', at=now + 60)))
        # In the main list both: by position, as before.
        listed = {**batch, 'messages': [records.incoming(item('m2', '在吗', position='7', at=now))]}
        with owner_cli(return_value={'data': {'messages': [
                item('m-own', '在', sender=OWNER, position='8', at=now)]}}):
            self.assertTrue(self.proxy.web.owner_replied(self.ctx, self.settings(), contact, listed))

    def test_a_send_waits_while_the_login_runs(self):
        self.submitted()
        with self.proxy.web_lock:  # As cmd_login holds it.
            self.run_tick('好的', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertEqual(self.batches()[0]['status'], 'submitted')
        self.assertIn(f"proxy batch={self.batches()[0]['id'][:8]} waits: the send tool's login is under way", self.logs)
        self.run_tick('好的', {'ok': True, 'submitted': True}).assert_called_once()

    def test_each_answer_of_the_tool_is_sent_may_have_been_or_certainly_not(self):
        batch = self.submitted()
        contact = records.read_contacts(self.state)[0]

        def send(*answers, run=None):
            patch, runs = self.tool_runs(*answers)
            with (mock.patch.object(self.proxy.web, 'run_tool', **run) if run else patch):
                return self.proxy.web.web_send(self.ctx, self.settings(), contact, records.read_batch(self.state, batch['id']), '好的'), runs
        for kind in ('validation', 'unsupported', 'auth', 'failed'):
            result, _ = send(failure(kind))
            self.assertEqual((result['ok'], result['submitted'], result['type'], result['error']), (False, False, kind, '原因'))
        for kind in ('unknown_result', 'a_type_of_later'):
            self.assertTrue(send(failure(kind))[0]['submitted'])
        # No answer at all (repo review F6: killed after sending, or uv could not fetch its packages): the same
        # command with the same key once more, the tool's own record answers; no answer again may have been sent.
        silent = ('', ['Traceback (most recent call last):\n', 'MemoryError\n'])
        replayed = (RECEIPT[0].replace('"om_tool"', '"om_tool", "replayed": true'), [])
        result, runs = send(silent, replayed)
        self.assertEqual((result['ok'], result['message_id'], result['replayed'], runs[0].args), (True, 'om_tool', True, runs[1].args))
        result, runs = send(silent, failure('unknown_result'))
        self.assertEqual((result['submitted'], result['type'], len(runs)), (True, 'unknown_result', 2))
        result, runs = send(silent, failure('validation'))  # Its record has no send of the key: certainly not.
        self.assertEqual((result['submitted'], result['type']), (False, 'validation'))
        result, runs = send(silent, ('', ['error: Failed to download `requests==2.32.5`\n']))
        self.assertEqual((result['submitted'], result['type'], len(runs)), (True, 'no_result', 2))
        self.assertIn('Failed to download', result['error'])
        # Sent, not read back: the same command with the same key only reads back; still not, sent all the same.
        result, runs = send(failure('readback_failed', '消息已发出，回读核验未完成'), RECEIPT)
        self.assertEqual((result['ok'], result['message_id'], runs[0].args), (True, 'om_tool', runs[1].args))
        result, _ = send(failure('readback_failed', '消息已发出，回读核验未完成'), failure('readback_failed'))
        self.assertEqual((result['ok'], result['message_id'], result['note']), (True, '', '消息已发出，回读核验未完成'))
        timed, _ = send(run={'side_effect': TimeoutError('发送工具 300 秒没有结果')})
        self.assertEqual((timed['submitted'], timed['type']), (True, 'timeout'))
        self.assertFalse(send(run={'side_effect': FileNotFoundError(2, 'No such file', 'uv')})[0]['submitted'])
        # The whole way: certainly not sent is held and tried again on the next poll.
        self.run_tick('好的', {'ok': False, 'submitted': False, 'type': 'failed', 'error': '当前用户无发言权限'})
        held = records.read_batch(self.state, batch['id'])
        self.assertEqual((held['status'], held['send_failed'], held['attempts']), ('held', True, 1))
        self.assertEqual(self.run_tick('好的', {'ok': True, 'submitted': True}).call_count, 1)
        # A tool file changed since it was installed: not run at all.
        (Path(self.settings()['send_tool']) / 'scripts' / 'send-as-user.py').write_text('# changed\n')
        with mock.patch.object(self.proxy.web, 'run_tool') as run:
            result = self.proxy.web.web_send(self.ctx, self.settings(), contact, held, '好的')
        run.assert_not_called()
        self.assertEqual((result['submitted'], result['error']), (False, '代答发送工具的文件和安装时不同：./scripts/send-as-user.py'))

    def test_every_reason_a_batch_was_given_stays_in_its_history(self):
        # Repo review E3: 苏越's d40626116d was held three times by a failed send, and kept only the reason of
        # the move after them, “对方又发来新消息”.
        batch = self.submitted()
        mismatch = '会话最新一条消息的位置对不上：网页端 57，接口 62'
        self.run_tick('好的', {'ok': False, 'step': 'check', 'submitted': False, 'error': mismatch})
        # Tried again with 代答 turned off meanwhile: held still, now for that.
        records.write_settings(self.state, {**records.read_settings(self.state), 'enabled': False})
        self.run_tick('好的', {'ok': True, 'submitted': True}).assert_not_called()
        records.write_settings(self.state, {**records.read_settings(self.state), 'enabled': True})
        self.take_in(item('m9', '算了', position='63'))
        gone = records.read_batch(self.state, batch['id'])
        self.assertEqual(gone['reason'], '对方又发来新消息')
        self.assertEqual([entry[0::2] for entry in gone['history']],
                         [['received'], ['submitted'], ['sending'], ['held', mismatch], ['held', '代答已关闭'],
                          ['superseded', '对方又发来新消息']])
        self.assertTrue(all(isinstance(entry[1], int) for entry in gone['history']))


    # Overtaken rounds (repo review D, 2026-10-29: a round took 6-8 minutes, and newer messages waited for it to
    # end and then for a round of their own, while its reply, which never saw them, went out).

    @contextlib.contextmanager
    def stops(self, *, forwarded=False):
        """The Bridge's stop of a job (the card's 停止) faked: each call's (action, chat, event id), checked to be
        outside Proxy.lock; ``forwarded`` says a live task runner took the click instead."""
        calls, app = [], self.proxy.contact_topics.topic_context(self.ctx, self.settings(), records.read_contacts(self.state)[0],
                                                  'om_mirror')[0]

        def stop(target, action, chat, event_id):
            self.assertFalse(self.proxy.lock.locked())
            calls.append((action, chat, event_id))
            return True
        with mock.patch.object(app.controls, 'cancel_reply_job_from_card', side_effect=stop), \
             mock.patch.object(self.app.runtime.jobs, 'find', return_value=object()), \
             mock.patch('covey.app.runner_ipc.forward_card_action', return_value=forwarded) as forward:
            yield calls, forward

    def running(self, batch):
        """``batch``'s own job running, by its record."""
        batch = records.read_batch(self.state, batch['id'])
        upsert_job_record(self.state, batch['job_key'], status='running', job_id=batch['job_id'],
                          topic_id=batch['topic_id'], message_id=batch['mirror_id'], chat_id='oc_proxy')
        return batch

    def open_batch(self, status):
        return next(b for b in self.batches() if b['status'] == status)

    def test_newer_messages_stop_a_running_round_and_the_next_one_answers_both(self):
        first = self.submitted(status='running')
        with self.stops() as (calls, forward):
            self.take_in(item('m2', '还有个事', position='9'))
        gone = records.read_batch(self.state, first['id'])
        self.assertEqual((gone['status'], gone['reason']), ('superseded', '对方又发来新消息，这一轮已停下'))
        carrier = self.open_batch('received')
        self.assertEqual(([m['id'] for m in carrier['messages']], carrier['restarts'], 'stop_job' in carrier),
                         (['m1', 'm2'], 1, False))
        # Its job asked to stop the way its card's 停止 does: the runner first, else the Bridge.
        # ``by``: who stopped it (core/jobs.py STOP_SOURCES), so the record says 代答 replaced the batch, not the owner.
        action = {'kind': 'bridge_job_stop', 'job_id': str(first['job_id']), 'message_id': 'om_mirror', 'by': 'proxy'}
        self.assertEqual(calls, [(action, 'oc_proxy', f"proxy-stop-{first['id']}")])
        event = forward.call_args.args[2]
        self.assertEqual((event['message_id'], event['action_value'], forward.call_args.args[3]), ('om_mirror', action, action))
        self.assertIn('· 对方又发来新消息，这一轮停下，和新消息一起回', self.notes())
        self.assertIn(f"proxy stop job=#{first['job_id']} batch={first['id'][:8]}: asked in the Bridge", self.logs)
        self.assertTrue(any(line.startswith(f"proxy overtaken batch={first['id'][:8]}") for line in self.logs))
        # The next round's prompt says so, and has both messages in order.
        contact = records.read_contacts(self.state)[0]
        prompt = records.batch_prompt(contact, carrier, self.settings())
        self.assertIn('那一轮已停下，没有发出', prompt)
        self.assertLess(prompt.index('接口文档在哪'), prompt.index('还有个事'))
        self.assertNotIn('那一轮已停下', records.batch_prompt(contact, first, self.settings()))
        # The stopped job ends (cancelled, or done after all): its reply is never sent; the next round runs.
        for status in ('cancelled', 'done'):
            upsert_job_record(self.state, first['job_key'], status=status)
            self.run_tick('旧的回复', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertEqual(records.read_batch(self.state, first['id'])['status'], 'superseded')
        self.assertEqual(records.read_batch(self.state, carrier['id'])['status'], 'submitted')

    def test_a_round_that_ended_but_was_not_yet_decided_is_not_sent(self):
        first = self.submitted(status='done')  # Its job ended; the poll that sends its reply has not come.
        with self.stops() as (calls, _):
            self.take_in(item('m2', '还有个事', position='9'))
            sent = self.run_tick('旧的回复', {'ok': True, 'submitted': True})
        sent.assert_not_called()
        self.assertEqual(calls, [])  # Nothing to stop.
        self.assertEqual(records.read_batch(self.state, first['id'])['status'], 'superseded')
        carrier = self.open_batch('submitted')
        self.assertEqual([m['id'] for m in carrier['messages']], ['m1', 'm2'])
        self.assertIn(f"proxy stop job=#{first['job_id']} batch={first['id'][:8]}: nothing to stop (done)", self.logs)

    def test_a_send_under_way_and_the_owners_later_turn_are_never_stopped(self):
        first = self.submitted()
        sending = records.move_batch(self.state, first, 'sending', reply='好的', reply_job_id=first['job_id'])
        with self.stops() as (calls, _):
            self.take_in(item('m2', '还有个事', position='9'))
            self.assertEqual(records.read_batch(self.state, first['id'])['status'], 'sending')
            self.assertEqual([m['id'] for m in self.open_batch('received')['messages']], ['m2'])
            records.move_batch(self.state, sending, 'sent')
            self.run_tick('好的', {'ok': True, 'submitted': True})
            second = self.running(self.open_batch('submitted'))
            # The owner spoke in the topic while it ran: their turn is the one that answers (repo review D2).
            upsert_job_record(self.state, 'owner-turn', status='queued', job_id=second['job_id'] + 1,
                              topic_id=second['topic_id'], message_id='om_owner', chat_id='oc_proxy')
            self.take_in(item('m3', '在吗', position='11'))
            self.assertEqual(records.read_batch(self.state, second['id'])['status'], 'submitted')
            self.assertEqual(calls, [])
            self.assertIn(f"proxy batch={second['id'][:8]} job=#{second['job_id']} runs on: the owner's later turn "
                          "in its topic", self.logs)

    def test_a_batch_waits_for_its_chat_to_settle_but_not_past_the_limit(self):
        self.assertEqual((proxy_module.SETTLE_LIMIT, records.MAX_RESTARTS), (45, 2))
        with mock.patch.object(proxy_module, 'SETTLE_SECONDS', 10):
            self.take_in(item('m1', '在吗'))
            self.run_tick('好的', {'ok': True})
            batch = self.open_batch('received')
            self.assertTrue(9 < self.proxy.soon <= 10)  # ``run`` goes round as it settles, not in POLL_SECONDS.
            now = time.time()
            records.write_batch(self.state, {**batch, 'created_at': now - 30, 'last_in_at': now - 5})
            self.take_in(item('m2', '问你个事', position='9'))  # A message more: it waits again from this one.
            self.run_tick('好的', {'ok': True})
            batch = self.open_batch('received')
            self.assertTrue(9 < self.proxy.soon <= 10)
            records.write_batch(self.state, {**batch, 'created_at': now - 40, 'last_in_at': now - 1})
            self.run_tick('好的', {'ok': True})
            self.assertTrue(4 < self.proxy.soon <= 5)  # SETTLE_LIMIT after it opened, not 10 s after the last.
            records.write_batch(self.state, {**batch, 'created_at': now - 46, 'last_in_at': now - 1})
            self.run_tick('好的', {'ok': True})
            self.assertEqual(self.open_batch('submitted')['id'], batch['id'])
            self.assertIsNone(self.proxy.soon)
        rests = []
        self.proxy.soon = 3
        with mock.patch.object(self.proxy, 'rest', side_effect=lambda s: rests.append(s) or True):
            self.proxy.run(self.ctx)
        self.assertEqual(rests, [3.1])

    def test_at_most_two_rounds_in_a_row_are_stopped_and_a_sent_reply_starts_the_count_again(self):
        batch = self.running(self.submitted(status='running'))
        self.cards = iter(f'om_mirror{n}' for n in range(2, 9))  # A mirror card for each round.
        with self.stops() as (calls, _):
            for mid, position in (('m2', '9'), ('m3', '11')):
                self.take_in(item(mid, '还有' + mid, position=position))
                self.run_tick(None, {'ok': True})
                batch = self.running(self.open_batch('submitted'))
            self.assertEqual((len(calls), batch['restarts'], [m['id'] for m in batch['messages']]), (2, 2, ['m1', 'm2', 'm3']))
            self.take_in(item('m4', '再问一下', position='13'))  # The third in a row runs to its end.
            self.assertEqual((records.read_batch(self.state, batch['id'])['status'], len(calls)), ('submitted', 2))
            waiting = self.open_batch('received')
            self.assertEqual(([m['id'] for m in waiting['messages']], waiting.get('restarts', 0)), (['m4'], 0))
            self.assertIn(f"proxy batch={batch['id'][:8]} job=#{batch['job_id']} runs on: 2 rounds stopped in a row", self.logs)
            upsert_job_record(self.state, batch['job_key'], status='done')
            sent = self.run_tick('都在 wiki 上', {'ok': True, 'submitted': True})
            self.assertEqual(sent.call_args.args[4], '都在 wiki 上')  # Sent as before.
            self.assertEqual(records.read_batch(self.state, batch['id'])['status'], 'sent')
            self.run_tick(None, {'ok': True})
            fresh = self.running(self.open_batch('submitted'))
            self.take_in(item('m5', '还有', position='15'))  # Counted afresh: stopped again.
            self.assertEqual((records.read_batch(self.state, fresh['id'])['status'], len(calls)), ('superseded', 3))

    def test_a_restart_before_the_stop_still_stops_the_job_and_never_sends_its_reply(self):
        first = self.submitted(status='running')
        # The Bridge stopped between add_messages's two writes: the new batch says what to stop, the old one
        # is still submitted.
        with mock.patch.object(self.proxy, 'stop_overtaken', create=True):
            self.take_in(item('m2', '还有个事', position='9'))
        records.write_batch(self.state, {**records.read_batch(self.state, first['id']), 'status': 'submitted'})
        carrier = self.open_batch('received')
        records.write_batch(self.state, {**carrier, 'stop_job': first['id']})
        upsert_job_record(self.state, first['job_key'], status='running')
        with self.stops(forwarded=True) as (calls, forward):
            self.run_tick('旧的回复', {'ok': True, 'submitted': True}).assert_not_called()
        forward.assert_called_once()
        self.assertEqual(calls, [])  # The live runner took the click: it stops its own job (rule 8).
        self.assertEqual(records.read_batch(self.state, first['id'])['status'], 'superseded')
        self.assertNotIn('stop_job', records.read_batch(self.state, carrier['id']))
        self.assertIn(f"proxy stop job=#{first['job_id']} batch={first['id'][:8]}: sent to its task runner", self.logs)
        upsert_job_record(self.state, first['job_key'], status='done')
        with self.stops() as (calls, forward):
            self.run_tick('旧的回复', {'ok': True, 'submitted': True}).assert_not_called()
        self.assertEqual(calls, [])

    def test_the_stop_a_task_runner_is_sent_is_its_cards_stop_click(self):
        # A runner routes the click it is sent like any click on its card (UnifiedBridgeApp.handle_card_action_event).
        first = self.submitted(status='running')
        self.app.runtime.owner_open_id = OWNER
        with mock.patch('covey.app.runner_ipc.forward_card_action', return_value=True) as forward:
            self.take_in(item('m2', '还有个事', position='9'))
        with FakeCardKit():
            self.app.handle_card_action_event(forward.call_args.args[2], self.ctx)
        self.assertIn('outcome=handled', next(line for line in self.logs if 'card action kind=bridge_job_stop' in line))
        self.assertEqual(find_job_record(self.state, first['job_key'])['status'], 'cancelled')  # Queued: never runs.


if __name__ == '__main__':
    unittest.main()

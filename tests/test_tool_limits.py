"""What a turn may do (app/tool_limits.py): 群成员只读 (app/access.py read_only and member_mode, the 群聊 card and the
confirmation card), the butler's read-only first turn (LOOK), a 代答 topic's turn (PROXY), and how Claude Code and Codex
are held to them (backends/claude/argv.py, backends/codex/appserver.py); the 代答 turn's PreToolUse hook
(backends/claude/tool_guard.py); who sent a task, carried through its waits, retries and continuations."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context, make_job
from support.world import UnifiedCase
from covey.app import tool_limits
from covey.app.access import MEMBERS_ENV, Access
from covey.backends.claude import argv as claude_argv
from covey.backends.claude import redaction, tool_guard
from covey.backends.claude.bridge import ClaudeBackend
from covey.backends.codex import appserver
from covey.cards.group_manager import ask_card, ask_text, group_manager_card
from covey.core import look_write, topic_repos
from covey.core.harness import (CONSULT, LOOK, LOOK_ROOTS, PROXY, READ, TOOL_LIMIT, TOOL_NOTES, HarnessCapabilities,
                                JobCancelled, backend_capabilities)
from covey.core.groups import save_group
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.proxy import write_contact
from covey.core.state import write_json

OWNER, MEMBER = 'ou_owner0001', 'ou_member0001'


def active(record):
    return {**record, 'active': True, 'external': False}


class AccessReadOnlyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.logs = []
        self.access = Access(SimpleNamespace(state_dir=self.state, owner_open_id=OWNER, log=self.logs.append))
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop(MEMBERS_ENV, None)
        os.environ.pop('LARK_BRIDGE_GROUP_MEMBERS', None)

    def test_a_members_turn_is_read_only_where_the_owner_set_the_group_so(self):
        save_group(self.state, 'oc_g', active)
        self.assertEqual(self.access.member_mode('oc_g'), 'full')  # Unset: as before.
        self.assertFalse(self.access.read_only('oc_g', MEMBER))
        record = self.access.set_member_mode('oc_g', 'read')
        self.assertEqual((record['member_mode'], record['member_mode_by']), ('read', OWNER))
        self.assertIn('group chat=oc_g member_mode=read', self.logs[-1])
        self.assertTrue(self.access.read_only('oc_g', MEMBER))
        self.assertFalse(self.access.read_only('oc_g', OWNER))  # His own message.
        self.assertTrue(self.access.read_only('oc_g', ''))  # Kept no sender in an active group: a member's.
        save_group(self.state, 'oc_g', lambda r: {**r, 'active': False})
        self.assertFalse(self.access.read_only('oc_g', ''))  # Only the owner is served there: his.
        self.assertTrue(self.access.read_only('oc_g', MEMBER))  # Queued while it was open: still a member's.
        self.access.set_member_mode('oc_g', 'full')
        self.assertFalse(self.access.read_only('oc_g', MEMBER))

    def test_never_in_a_direct_chat_a_personal_group_or_a_chat_without_a_record(self):
        os.environ[MEMBERS_ENV] = 'read'
        self.assertFalse(self.access.read_only('oc_dm', ''))
        self.assertFalse(self.access.read_only('oc_unknown', MEMBER))
        save_group(self.state, 'oc_mine', active)
        from covey.core import spaces
        self.access.spaces = dataclasses.replace(spaces.DEFAULT, personal_groups=frozenset({'oc_mine'}))
        self.assertFalse(self.access.read_only('oc_mine', ''))

    def test_the_deployments_default_holds_for_groups_never_chosen_for(self):
        save_group(self.state, 'oc_g', active)
        save_group(self.state, 'oc_h', active)
        os.environ[MEMBERS_ENV] = 'read'
        self.assertEqual(self.access.member_mode('oc_g'), 'read')
        self.assertTrue(self.access.read_only('oc_g', MEMBER))
        self.access.set_member_mode('oc_h', 'full')  # Chosen on the card: the card wins.
        self.assertFalse(self.access.read_only('oc_h', MEMBER))
        os.environ[MEMBERS_ENV] = 'yes'
        self.assertEqual(self.access.member_mode('oc_g'), 'full')


class LimitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = make_context(self.root)
        self.ctx.state_dir.mkdir()
        self.access = Access(SimpleNamespace(state_dir=self.ctx.state_dir, owner_open_id=OWNER, log=lambda _: None))
        save_group(self.ctx.state_dir, 'oc_g', lambda r: {**active(r), 'member_mode': 'read'})

    def test_a_member_in_a_read_only_group_a_read_only_start_and_a_proxy_topic(self):
        member = make_job(chat_id='oc_g', sender_id=MEMBER)
        self.assertEqual(tool_limits.limit(self.access, self.ctx, member), READ)
        self.assertEqual(tool_limits.limit(self.access, self.ctx, make_job(chat_id='oc_g', sender_id=OWNER)), '')
        self.assertEqual(tool_limits.limit(self.access, self.ctx, make_job(chat_id='oc_dm', read_only=True)), LOOK)
        self.assertEqual(tool_limits.limit(self.access, self.ctx, None), '')
        limited = tool_limits.limited(self.access, self.ctx, member)
        self.assertEqual(limited.options[TOOL_LIMIT], READ)
        self.assertTrue(tool_limits.note(limited).endswith(tool_limits.MEMBER_NOTE))
        self.assertIs(tool_limits.limited(self.access, self.ctx, make_job(chat_id='oc_dm')), self.ctx)
        self.assertEqual(tool_limits.note(self.ctx), '')
        # A 代答 topic, whoever speaks: its contact's notes go with it.
        write_contact(self.ctx.state_dir, {'key': 'p2p:ou_zhang', 'kind': 'p2p', 'target': 'ou_zhang',
                                           'session_dir': '/abs/notes/zhang'})
        write_json(self.ctx.history_path.parent / 'topic.json', {'proxy': {'contact': 'p2p:ou_zhang'}})
        limited = tool_limits.limited(self.access, self.ctx, member)
        self.assertEqual((limited.options[TOOL_LIMIT], limited.options[TOOL_NOTES]), (PROXY, '/abs/notes/zhang'))
        self.assertEqual(tool_limits.note(limited), '')

    def test_a_look_turn_gets_its_checkpoint_and_outputs_directory_and_ends_with_where_it_may_write(self):
        """W1-C (C6): a LOOK turn may write its own checkpoint and the topic's outputs directory (made before it runs);
        its words end its message, by whether its Harness holds that."""
        job = make_job(chat_id='oc_dm', read_only=True, checkpoint_path=str(self.ctx.state_dir / 'checkpoints' / 'k.json'))
        held = tool_limits.limited(self.access, self.ctx, job, held=True)
        roots = held.options[LOOK_ROOTS]
        folder = self.ctx.state_dir / 'topics' / (self.ctx.topic_id or 'topic') / 'outputs'
        self.assertEqual(roots, {'file': job.checkpoint_path, 'dir': str(folder), 'held': True})
        self.assertTrue(folder.is_dir())
        words = tool_limits.note(held)
        self.assertTrue(words.startswith('\n\n[Bridge 说明] 这一轮只读'))
        self.assertIn(f'能写的只有本轮检查点 {job.checkpoint_path} 和本话题产物目录 {folder}', words)
        self.assertIn('别处的写入会被拒绝', words)
        self.assertNotIn('检查点也不用写', words)
        loose = tool_limits.limited(self.access, self.ctx, job)  # A Harness without look_gate (a device, dsh, MASO).
        self.assertFalse(loose.options[LOOK_ROOTS]['held'])
        self.assertIn('写不了就略过', tool_limits.note(loose))
        # No checkpoint file to name: nothing to hold the writes to.
        self.assertFalse(tool_limits.look_roots(self.ctx, make_job(read_only=True), held=True)['held'])

    def test_only_a_read_turn_on_a_harness_that_cannot_hold_it_is_refused(self):
        read = dataclasses.replace(self.ctx, options={TOOL_LIMIT: READ})
        self.assertIn('这一轮没有启动', tool_limits.refusal(read, HarnessCapabilities(), 'Later'))
        self.assertIn('Later 做不到只读', tool_limits.refusal(read, HarnessCapabilities(), 'Later'))
        self.assertEqual(tool_limits.refusal(read, HarnessCapabilities(read_only=True), 'Codex'), '')
        look = dataclasses.replace(self.ctx, options={TOOL_LIMIT: LOOK})
        self.assertEqual(tool_limits.refusal(look, HarnessCapabilities(), 'Later'), '')  # The words alone.
        self.assertTrue(backend_capabilities(ClaudeBackend(), self.ctx).read_only)


class ClaudeArgvTests(unittest.TestCase):
    def argv(self, **options):
        # A turn in a repository under the home (/x): the home-search hook of a turn started from the home itself is
        # tests/test_home_search.py's.
        ctx = make_context(Path('/x'), workdir=Path('/x/repo'), backend_cmd=['claude'], options=options)
        return claude_argv.stream_argv(ClaudeBackend(), ctx)

    @staticmethod
    def value(argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_a_read_turn_has_read_grep_and_glob_and_no_mcp_server(self):
        argv = self.argv(**{TOOL_LIMIT: READ})
        self.assertEqual(self.value(argv, '--tools'), 'Read,Grep,Glob')
        self.assertEqual(self.value(argv, '--mcp-config'), '{"mcpServers":{}}')
        self.assertIn('--strict-mcp-config', argv)
        self.assertNotIn('--disallowedTools', argv)
        self.assertNotIn('--settings', argv)

    def test_a_look_turn_whose_writes_are_not_held_goes_without_the_tools_that_change_files(self):
        for options in ({TOOL_LIMIT: LOOK}, {TOOL_LIMIT: LOOK, LOOK_ROOTS: {'file': '/s/c.json', 'dir': '/s/o', 'held': False}}):
            argv = self.argv(**options)
            self.assertEqual(self.value(argv, '--disallowedTools'), 'Edit,Write,NotebookEdit')
            self.assertNotIn('--tools', argv)
            self.assertNotIn('--strict-mcp-config', argv)
        # On a device the hook script is not there: its file tools go, held or not.
        ctx = make_context(Path('/x'), workdir=Path('/x/repo'), backend_cmd=['claude'], device='gpu1', remote_workdir='/p',
                           options={TOOL_LIMIT: LOOK, LOOK_ROOTS: {'file': '/s/c.json', 'dir': '/s/o', 'held': True}})
        self.assertEqual(self.value(claude_argv.stream_argv(ClaudeBackend(), ctx), '--disallowedTools'),
                         'Edit,Write,NotebookEdit')

    def test_a_held_look_turn_keeps_its_tools_and_has_the_look_hook_beside_the_others(self):
        """W1-C (C6): on this machine a LOOK turn keeps Edit and Write (no --disallowedTools) and the look hook checks
        where they and its shell write, merged with the extensions' hooks into the one --settings."""
        ext = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/abs/hook'}]}]}
        argv = self.argv(**{TOOL_LIMIT: LOOK, LOOK_ROOTS: {'file': '/s/c.json', 'dir': '/s/o', 'held': True},
                            claude_argv.TURN_HOOKS: ext})
        self.assertNotIn('--disallowedTools', argv)
        self.assertNotIn('--tools', argv)
        self.assertEqual(argv.count('--settings'), 1)
        groups = json.loads(self.value(argv, '--settings'))['hooks']['PreToolUse']
        self.assertEqual(len(groups), 2)
        look, other = groups
        self.assertEqual(look['matcher'], 'Bash|Edit|Write|MultiEdit|NotebookEdit')
        command = look['hooks'][0]['command']
        self.assertIn('covey_tool_guard.py', command)
        self.assertTrue(command.endswith('--mode look --allow-file /s/c.json --allow-dir /s/o'), command)
        self.assertEqual(other, ext['PreToolUse'][0])

    def test_the_extensions_hooks_go_into_the_one_settings_but_not_a_proxy_or_consult_turn(self):
        """backends/claude/argv.py TURN_HOOKS, after Wyu981011/covey 50fc367e: an extension adds Claude Code hooks to a
        topic's turns without touching the user's or the project's settings."""
        hooks = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/abs/hook'}]}],
                 'SessionStart': [{'hooks': [{'type': 'command', 'command': '/abs/start'}]}]}
        for options in ({}, {TOOL_LIMIT: LOOK}, {TOOL_LIMIT: READ}):
            argv = self.argv(**options, **{claude_argv.TURN_HOOKS: hooks, redaction.SETTINGS_ENV: {'A': 'b'}})
            self.assertEqual(argv.count('--settings'), 1)
            self.assertEqual(json.loads(self.value(argv, '--settings')), {'env': {'A': 'b'}, 'hooks': hooks})
        settings = json.loads(self.value(self.argv(**{TOOL_LIMIT: PROXY, claude_argv.TURN_HOOKS: hooks}), '--settings'))
        self.assertEqual(list(settings['hooks']), ['PreToolUse'])  # Its tool guard's alone.
        self.assertIn('covey_tool_guard.py', settings['hooks']['PreToolUse'][0]['hooks'][0]['command'])
        self.assertNotIn('--settings', self.argv(**{TOOL_LIMIT: CONSULT, claude_argv.TURN_HOOKS: hooks}))
        self.assertNotIn('--settings', self.argv(**{claude_argv.TURN_HOOKS: {}}))

    def test_hooks_merge_by_event_and_a_table_of_another_shape_is_refused(self):
        first = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/a'}]}]}
        second = {'PreToolUse': [{'matcher': 'Edit', 'hooks': [{'type': 'command', 'command': '/b'}]}],
                  'Stop': [{'hooks': [{'type': 'command', 'command': '/c'}]}]}
        self.assertEqual(claude_argv.merge_hooks(first, None, second, {}),
                         {'PreToolUse': first['PreToolUse'] + second['PreToolUse'], 'Stop': second['Stop']})
        self.assertEqual(first['PreToolUse'][0]['matcher'], 'Bash')  # The tables given are not changed.
        self.assertEqual(len(first['PreToolUse']), 1)
        for bad in (['PreToolUse'], {'PreToolUse': {'matcher': 'Bash'}}, {1: []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                claude_argv.merge_hooks(bad)

    def test_a_proxy_turn_has_its_hook_beside_the_relay_in_one_settings(self):
        argv = self.argv(**{TOOL_LIMIT: PROXY, TOOL_NOTES: '/abs/notes', redaction.SETTINGS_ENV: {'A': 'b'}})
        self.assertEqual(self.value(argv, '--tools'), 'Read,Grep,Glob,Bash,Edit,Write')
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(argv.count('--settings'), 1)
        settings = json.loads(self.value(argv, '--settings'))
        self.assertEqual(settings['env'], {'A': 'b'})
        [entry] = settings['hooks']['PreToolUse']
        self.assertEqual(entry['matcher'], 'Bash|Edit|Write|MultiEdit|NotebookEdit')
        [hook] = entry['hooks']
        self.assertEqual(hook['type'], 'command')
        self.assertIn('covey_tool_guard.py', hook['command'])
        self.assertTrue(hook['command'].endswith('--notes /abs/notes'))

    def test_a_turn_without_a_limit_is_as_before(self):
        argv = self.argv()
        for flag in ('--tools', '--disallowedTools', '--strict-mcp-config', '--settings'):
            self.assertNotIn(flag, argv)
        self.assertEqual(json.loads(self.value(self.argv(**{redaction.SETTINGS_ENV: {'A': 'b'}}), '--settings')),
                         {'env': {'A': 'b'}})
        self.assertNotIn('--tools', self.argv(tools=('Read',)))  # A lead's list of old: LEAD holds it now (ArgvTest).


class CodexSandboxTests(unittest.TestCase):
    def ctx(self, **options):
        return make_context(Path('/x'), options=options)

    def test_a_read_or_look_turn_runs_in_the_read_only_sandbox_for_the_thread_and_the_turn(self):
        for limit in (READ, LOOK):
            params, _ = appserver.thread_params(self.ctx(**{TOOL_LIMIT: limit}), None)
            self.assertEqual(params['sandbox'], 'read-only')
            self.assertEqual(self.turn(**{TOOL_LIMIT: limit})['sandboxPolicy'], {'type': 'readOnly'})
        # A 代答 turn and any other keep the Harness's sandbox, and the turn says nothing of it, as before.
        for options in ({TOOL_LIMIT: PROXY}, {}, {'sandbox': 'workspace-write'}):
            params, _ = appserver.thread_params(self.ctx(**options), None)
            self.assertEqual(params['sandbox'], options.get('sandbox', 'danger-full-access'))
            self.assertNotIn('sandboxPolicy', self.turn(**options))

    def turn(self, **options):
        sent = {}

        def request(method, params, timeout=None):
            sent.update(params)
            return {'turn': {'id': 'turn-1'}}
        rpc = SimpleNamespace(thread_id='thread-1', request=request)
        appserver.start_turn(rpc, SimpleNamespace(inputs=SimpleNamespace(attachments=())), self.ctx(**options),
                             'hi', None)
        return sent


class GuardTests(unittest.TestCase):
    READS = ('lark-cli --profile me im +chat-messages-list --chat-id oc_x --as user',
             'lark-cli im +messages-search --query "接口 (文档)" --as user --format json',
             'timeout --kill-after=2s 30s lark-cli docs +fetch https://x.feishu.cn/docx/abc',
             'lark-cli contact +search-user --query 张三', 'lark-cli --profile me api GET /open-apis/im/v1/chats/oc_x',
             'lark-cli schema im.messages.list', 'lark-cli im +messages-send --help', '/usr/bin/lark-cli im +messages-mget',
             'lark-cli im +chat-messages-list --chat-id oc_x 2>&1 | head -50', "lark-cli im +messages-search --query a | jq '.data'",
             'lark-cli im +messages-mget --ids om_1 2>/dev/null', 'lark-cli --as user wiki spaces get_node --token wk',
             'lark-cli im chats list --page-size 20')
    REFUSED = ('lark-cli im +messages-send --chat-id oc_x --text hi --as user', 'lark-cli api POST /open-apis/im/v1/messages',
               'lark-cli im +chat-messages-list --chat-id oc_x | tee /tmp/x', 'lark-cli im +chat-messages-list; rm -rf /x',
               'lark-cli im +chat-messages-list --chat-id "$(cat ~/.ssh/id_rsa)"', 'lark-cli docs +fetch x --output /tmp/a',
               'lark-cli im +messages-resources-download --message-id om_x', 'rm -rf /x', 'cat ~/.ssh/id_rsa',
               'FOO=1 lark-cli im +chat-messages-list', 'lark-cli im +chat-messages-list > /tmp/x', 'lark-cli im chats create',
               'lark-cli im messages delete --message-id om_x', 'lark-cli auth login', 'lark-cli im chats',
               'timeout 30s rm -rf /x', 'lark-cli im +chat-messages-list\nrm -rf /x', 'echo `id`', '',
               'lark-cli im +chat-messages-list | tee /tmp/x', 'lark-cli im +chat-messages-list | sh', 'lark-cli im +x-list |',
               'lark-cli im +chat-messages-list 2>&1 > /tmp/x', 'lark-cli im +chat-messages-list && lark-cli im +messages-send')

    def test_the_shell_runs_only_one_lark_cli_command_that_reads(self):
        for command in self.READS:
            self.assertEqual(tool_guard.command_refusal(command), '', command)
        for command in self.REFUSED:
            self.assertEqual(tool_guard.command_refusal(command), tool_guard.BASH_WORDS, command)

    def test_the_edit_tools_write_only_in_the_contacts_notes(self):
        with tempfile.TemporaryDirectory() as root:
            notes = Path(root) / 'notes'
            notes.mkdir()
            (Path(root) / 'escape').symlink_to('/etc')
            self.assertEqual(tool_guard.path_refusal('Write', str(notes / 'log.md'), str(notes)), '')
            self.assertEqual(tool_guard.path_refusal('Edit', 'log.md', str(notes), cwd=str(notes)), '')
            for given in ('/etc/passwd', str(notes) + '-other/x.md', str(notes / '..' / 'x.md'),
                          str(Path(root) / 'escape' / 'x')):
                self.assertIn('只能改联系人笔记目录', tool_guard.path_refusal('Write', given, str(notes)), given)
            self.assertIn('没有笔记目录', tool_guard.path_refusal('Write', str(notes / 'log.md'), ''))
        call = {'tool_name': 'NotebookEdit', 'tool_input': {'notebook_path': '/etc/x.ipynb'}}
        self.assertIn('NotebookEdit', tool_guard.refusal(call, '/abs/notes'))
        self.assertEqual(tool_guard.refusal({'tool_name': 'Read', 'tool_input': {'file_path': '/etc/passwd'}}, ''), '')

    def test_the_hook_exits_2_with_the_reason_and_refuses_what_it_cannot_read(self):
        def run(text, *args):
            err = io.StringIO()
            with mock.patch('sys.stderr', err):
                return tool_guard.main(list(args), stdin=io.StringIO(text)), err.getvalue()
        allowed = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': self.READS[0]}})
        self.assertEqual(run(allowed, '--notes', '/abs/notes'), (0, ''))
        code, said = run(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'rm -rf /'}}))
        self.assertEqual((code, said.strip()), (2, tool_guard.BASH_WORDS))
        code, said = run('not json')
        self.assertEqual(code, 2)
        self.assertIn('工具检查出错', said)


class LookGateTests(unittest.TestCase):
    """W1-C (C6): the one check every LOOK path gate shares (core/look_write.py refusal), its write tool, and Claude
    Code's look hook (backends/claude/tool_guard.py --mode look)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / 'data' / 'state'
        self.file = self.state / 'checkpoints' / 'om_1-7.json'
        self.dir = self.state / 'topics' / 't1' / 'outputs'
        self.repo = self.root / 'home' / 'covey'
        for folder in (self.file.parent, self.dir, self.repo / '.git', self.repo / 'app'):
            folder.mkdir(parents=True)
        (self.repo / 'app' / 'app.py').write_text('old\n')
        (self.repo / '.git' / 'config').write_text('[core]\n')
        self.read = self.root / 'data' / 'worktrees' / topic_repos.READ_DIR / 'covey' / 'b4d687c49c3a'
        self.read.mkdir(parents=True)

    def refusal(self, target, cwd=''):
        return look_write.refusal(target, allow_file=str(self.file), allow_dir=str(self.dir), cwd=cwd)

    def test_only_the_checkpoint_and_the_outputs_directory_may_be_written(self):
        for target in (self.file, self.dir / 'report.md', self.dir / 'sub' / 'deeper' / 'x.json', '/dev/null'):
            self.assertEqual(self.refusal(str(target)), '', target)
        self.assertEqual(self.refusal('report.md', cwd=str(self.dir)), '')
        (self.dir / 'link-to-git').symlink_to(self.repo / '.git')
        (self.dir / 'link-to-repo').symlink_to(self.repo / 'app' / 'app.py')
        (self.dir / 'parent-link').symlink_to(self.repo)
        os.link(self.repo / 'app' / 'app.py', self.dir / 'hard.py')
        refused = [self.repo / 'app' / 'app.py', self.root / 'home' / 'x.md', self.root / 'home',
                   self.state / 'checkpoints' / 'om_2-8.json', self.file.parent / '.hidden', self.read / 'app.py',
                   self.dir / '..' / 'escape.md', self.dir / 'link-to-git' / 'config', self.dir / 'link-to-repo',
                   self.dir / 'parent-link' / 'app' / 'app.py', self.dir / '.git' / 'config', self.dir / 'hard.py',
                   self.repo / '.git' / 'config', '/etc/passwd', '']
        for target in refused:
            self.assertTrue(self.refusal(str(target)), target)
        self.assertIn('.git', self.refusal(str(self.dir / 'link-to-git' / 'config')))
        self.assertIn('只读 worktree', self.refusal(str(self.read / 'app.py')))
        self.assertTrue(self.refusal('x.md', cwd=str(self.repo)))  # Relative to the call's directory.
        # An outputs directory inside a read worktree or a .git is none at all.
        self.assertTrue(look_write.refusal(str(self.read / 'r.md'), allow_dir=str(self.read)))
        # The read worktree's place is core/topic_repos.py's.
        self.assertEqual(topic_repos.READ_DIR, look_write.READ_PARTS[1])
        # Anything that goes wrong refuses.
        with mock.patch.object(look_write.os.path, 'realpath', side_effect=RuntimeError('boom')):
            self.assertIn('出错', self.refusal(str(self.file)))

    def test_the_write_tool_writes_there_and_nowhere_else(self):
        made = look_write.call('workspace_write_file', {'path': str(self.dir / 'a' / 'r.md'), 'content': '结论\n'},
                               allow_file=str(self.file), allow_dir=str(self.dir))
        self.assertEqual(((self.dir / 'a' / 'r.md').read_text(), made['bytes']), ('结论\n', len('结论\n'.encode())))
        look_write.call('workspace_write_file', {'path': str(self.dir / 'a' / 'r.md'), 'content': '再一行\n', 'append': True},
                        allow_file=str(self.file), allow_dir=str(self.dir))
        self.assertEqual((self.dir / 'a' / 'r.md').read_text(), '结论\n再一行\n')
        look_write.call('workspace_write_file', {'path': str(self.file), 'content': '{"phase": "x"}'},
                        allow_file=str(self.file), allow_dir=str(self.dir))
        self.assertEqual(json.loads(self.file.read_text()), {'phase': 'x'})
        (self.dir / 'link').symlink_to(self.repo / '.git' / 'config')
        for target in (self.repo / 'app' / 'app.py', self.dir / 'link', self.read / 'x', self.repo / '.git' / 'config'):
            with self.assertRaises(ValueError):
                look_write.call('workspace_write_file', {'path': str(target), 'content': 'x'},
                                allow_file=str(self.file), allow_dir=str(self.dir))
        self.assertEqual(((self.repo / 'app' / 'app.py').read_text(), (self.repo / '.git' / 'config').read_text()),
                         ('old\n', '[core]\n'))
        self.assertEqual(sorted(os.listdir(self.read)), [])
        with self.assertRaises(ValueError):  # No other tool, no other argument.
            look_write.call('workspace_write_file', {'path': str(self.file), 'content': 'x', 'mode': 'w'})

    def test_the_mcp_server_lists_and_calls_the_write_tool_and_says_why_it_refused(self):
        rows = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                    'name': 'workspace_write_file', 'arguments': {'path': str(self.dir / 'r.md'), 'content': 'ok'}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
                    'name': 'workspace_write_file', 'arguments': {'path': str(self.repo / 'app' / 'app.py'), 'content': 'x'}}}]
        out = io.StringIO()
        look_write.serve(str(self.file), str(self.dir), io.StringIO(''.join(json.dumps(r) + '\n' for r in rows)), out)
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([tool['name'] for tool in answers[1]['result']['tools']], ['workspace_write_file'])
        self.assertFalse(answers[2]['result']['isError'])
        self.assertTrue(answers[3]['result']['isError'])
        self.assertIn('不在可写的地方', answers[3]['result']['content'][0]['text'])
        self.assertEqual(((self.dir / 'r.md').read_text(), (self.repo / 'app' / 'app.py').read_text()), ('ok', 'old\n'))

    # A LOOK turn's shell: what it obviously writes elsewhere is refused, what only reads runs.
    SHELL_READS = ('rg -n "def main" covey', 'git log --oneline -20', 'git status --short', 'git diff HEAD~1 -- app',
                   'git -C /r show HEAD:app/app.py', 'git fetch origin', 'git branch -a', 'git stash list',
                   'git worktree list', 'git config --get remote.origin.url', 'covey repo use covey',
                   'covey repo use covey-internal --json', 'python3 -B scripts/stats.py --since 7d',
                   'rg -l foo | xargs wc -l', 'find . -maxdepth 2 -name "*.py" | head', 'timeout 30s rg x 2>/dev/null',
                   "awk '$1 > 5' data.txt", 'python3 -c "print(1 > 0)"', 'wc -l a b > /dev/null 2>&1',
                   'cd {dir} && echo x > r.md', 'echo x | tee -a {file}', 'cp /r/a.py {dir}/a.py',
                   "cat > {dir}/notes.md <<'EOF'\na > b\nEOF", 'mkdir -p {dir}/sub', 'ls -la | head -5')
    SHELL_WRITES = ('echo x >> /r/a.py', 'echo x > a.py', 'git commit -am x', 'git -C /r checkout main', 'git reset --hard',
                    'git push origin HEAD', 'git clean -fd', 'git stash', 'git branch -D topic', 'git worktree add /tmp/w',
                    'covey repo use covey --write', 'sed -i s/a/b/ /r/a.py', 'perl -pi -e s/a/b/ a.py', 'tee /r/a.py',
                    'rm -rf /r/build', 'touch /r/x', 'mv /r/a.py {dir}/a.py', 'cp a.py /r/b.py', 'ln -s x /r/y',
                    'dd if=/dev/zero of=/r/z', 'curl -o /r/x https://e.com', 'find . -delete', 'find . -exec rm {{}} +',
                    'ls | xargs rm', "bash -c 'echo x > /r/a'", 'echo x > $OUT/r', 'cd /r && echo x > r.md',
                    'echo x > {dir}/link/.git/config', 'echo x > "{read}/a.py"')

    def test_the_look_hooks_shell_check_refuses_obvious_writes_elsewhere_and_lets_reads_run(self):
        fill = {'dir': self.dir, 'file': self.file, 'read': self.read}
        for command in self.SHELL_READS:
            command = command.format(**fill)
            self.assertEqual(tool_guard.look_bash_refusal(command, '/r', str(self.file), str(self.dir)), '', command)
        for command in self.SHELL_WRITES:
            command = command.format(**fill)
            self.assertIn('这一轮只读', tool_guard.look_bash_refusal(command, '/r', str(self.file), str(self.dir)), command)

    def test_the_look_hook_exits_2_with_the_reason_and_refuses_what_it_cannot_read(self):
        def run(call):
            err = io.StringIO()
            with mock.patch('sys.stderr', err):
                code = tool_guard.main(['--mode', 'look', '--allow-file', str(self.file), '--allow-dir', str(self.dir)],
                                       stdin=io.StringIO(call if isinstance(call, str) else json.dumps(call)))
            return code, err.getvalue()
        self.assertEqual(run({'tool_name': 'Write', 'tool_input': {'file_path': str(self.dir / 'r.md')}}), (0, ''))
        self.assertEqual(run({'tool_name': 'Read', 'tool_input': {'file_path': str(self.repo / 'app' / 'app.py')}}), (0, ''))
        self.assertEqual(run({'tool_name': 'Bash', 'tool_input': {'command': 'git log -3'}, 'cwd': str(self.repo)}), (0, ''))
        for call in ({'tool_name': 'Edit', 'tool_input': {'file_path': str(self.repo / 'app' / 'app.py')}},
                     {'tool_name': 'MultiEdit', 'tool_input': {'file_path': str(self.read / 'x')}},
                     {'tool_name': 'NotebookEdit', 'tool_input': {'notebook_path': str(self.repo / 'a.ipynb')}},
                     {'tool_name': 'Bash', 'tool_input': {'command': 'git commit -m x'}, 'cwd': str(self.repo)}):
            code, said = run(call)
            self.assertEqual(code, 2, call)
            self.assertIn('这一轮只读', said)
        for broken in ('not json', json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'echo "open'}})):
            code, said = run(broken)
            self.assertEqual(code, 2, broken)
            self.assertIn('出错', said)


class RunsLimitTests(UnifiedCase):
    """A turn's limit reaches its backend before anything of the turn runs; a member's turn of a read-only group never
    runs on a Harness that cannot hold it."""

    def setUp(self):
        super().setUp()
        self.access = self.app.runtime.access
        save_group(self.ctx.state_dir, 'oc_g', lambda r: {**active(r), 'member_mode': 'read'})
        self.codex, self.target = self.app.topics.route(self.ctx, {'chat_id': 'oc_g', 'message_id': 'm1'})

    def turn(self, sender, backend_caps=None):
        job = make_job(job_id=7, chat_id='oc_g', message_id='m1', sender_id=sender, user_text='看下日志',
                       prompt_text='发送者：群成员 1\n\n看下日志', topic_id=self.target.topic_id,
                       workdir=self.target.workdir, history_path=self.target.history_path)
        seen = []

        def run(request):
            seen.append(request)
            raise JobCancelled('stop here')
        caps = (mock.patch('covey.app.runs.backend_capabilities', return_value=backend_caps) if backend_caps
                else contextlib.nullcontext())
        with caps, mock.patch.object(self.codex.backend, 'run', side_effect=run):
            try:
                result = self.codex.runs.generate_reply_for_worker(self.target, job, None, None)
            except JobCancelled:
                result = None
        return seen, result

    def test_a_members_turn_runs_read_only_and_says_so(self):
        [request], _ = self.turn(MEMBER)
        self.assertEqual(request.context.options[TOOL_LIMIT], READ)
        self.assertTrue(request.prompt.endswith(tool_limits.MEMBER_NOTE))
        self.assertIn('limit=read', ' '.join(self.logs))
        [request], _ = self.turn('owner')  # The owner's own: as before.
        self.assertNotIn(TOOL_LIMIT, request.context.options)
        self.assertNotIn('这一轮只读', request.prompt)

    def test_a_harness_that_cannot_hold_it_does_not_run_it(self):
        seen, result = self.turn(MEMBER, HarnessCapabilities())
        self.assertEqual(seen, [])
        self.assertTrue(result.is_error)
        self.assertEqual(result.outcome.error_code, 'read_only_refused')
        self.assertIn('做不到只读', result.failure_detail)


class SenderCarriedTests(UnifiedCase):
    """Who sent a task stays with it: its wait for a launch card, a retry, a continuation's request."""

    def test_a_waiting_record_a_retry_and_a_continuation_keep_their_sender(self):
        codex, target = self.app.topics.route(self.ctx, {'chat_id': 'oc_g', 'message_id': 'm1'})
        upsert_job_record(self.ctx.state_dir, 'm1', job_id=3, message_id='m1', chat_id='oc_g', user_text='看下日志',
                          sender_id=MEMBER, read_only=True, status='waiting_workspace')
        restored = codex.submission.job_from_record(target, find_job_record(self.ctx.state_dir, 'm1'))
        self.assertEqual((restored.sender_id, restored.read_only), (MEMBER, True))
        from covey.app.runner_ipc import RunnerLink
        posted = []
        with mock.patch('covey.app.runner_ipc.post', side_effect=lambda folder, payload: posted.append(payload) or 'op'), \
                mock.patch('covey.app.runner_ipc.await_reply', return_value={'ok': True, 'job_id': 9}):
            RunnerLink(self.root).enqueue('codex', target, 'answer', 'oc_g', 'answer-x', None, sender_id=MEMBER)
            RunnerLink(self.root).enqueue('codex', target, 'answer', 'oc_g', 'answer-y', None)
        self.assertEqual(posted[0]['sender_id'], MEMBER)
        self.assertNotIn('sender_id', posted[1])  # None known: none sent, as before.


class GroupCardTests(unittest.TestCase):
    """The 群聊 card's 设为只读 / 取消只读 and the confirmation card's words."""

    def buttons(self, card):
        found = []

        def walk(value):
            if isinstance(value, dict):
                if value.get('tag') == 'button':
                    found.append((value['text']['content'], value['behaviors'][0]['value']))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(card)
        return found

    def test_an_active_rows_button_names_the_mode_it_sets_and_its_tag_the_mode_it_has(self):
        groups = [{'chat_id': 'oc_a1', 'active': True, 'external': False},
                  {'chat_id': 'oc_a2', 'active': True, 'external': False, 'member_mode': 'read'},
                  {'chat_id': 'oc_a3', 'active': False, 'external': False}]
        card = group_manager_card('t' * 24, {'revision': 1, 'page': 0}, groups, {})
        labels = [(label, value.get('chat'), value.get('mode')) for label, value in self.buttons(card)]
        self.assertIn(('设为只读', 'oc_a1', 'read'), labels)
        self.assertIn(('取消只读', 'oc_a2', 'full'), labels)
        self.assertNotIn('oc_a3', [chat for label, chat, mode in labels if mode])  # Not active: nothing to set.
        text = json.dumps(card, ensure_ascii=False)
        self.assertEqual(text.count('群成员只读</text_tag>'), 1)
        # The deployment's default read: a group never chosen for is read-only on the card too.
        card = group_manager_card('t' * 24, {'revision': 1, 'page': 0}, groups[:1], {}, members='read')
        self.assertEqual([label for label, _ in self.buttons(card)][0], '取消只读')

    def test_the_confirmation_card_says_what_members_could_do(self):
        full = json.dumps(ask_card({'chat': 'oc_g', 'name': '项目群'}), ensure_ascii=False)
        self.assertIn('用你的全部权限和凭据', full)
        self.assertIn('设为只读', full)
        read = json.dumps(ask_card({'chat': 'oc_g', 'name': '项目群', 'members': 'read'}), ensure_ascii=False)
        self.assertNotIn('用你的全部权限和凭据', read)
        self.assertIn('只能读文件和搜索', read)
        self.assertIn('读得到你机器上的文件', ask_text({'chat': 'oc_g', 'members': 'read'}))


if __name__ == '__main__':
    unittest.main()

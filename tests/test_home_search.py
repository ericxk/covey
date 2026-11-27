"""The home-search hook (backends/claude/tool_guard.py ``--mode home-search``): a turn started from the home has no Bash,
Grep or Glob call that walks the whole home unbounded, and nothing else of its work is held up. Which turns get it is
backends/claude/argv.py ``home_hooks``; what it refuses and lets through is ``search_refusal``; whatever goes wrong in it
lets the call run (fail-open)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import exec_tempdir, make_context
from covey.backends.claude import argv as claude_argv
from covey.backends.claude import tool_guard
from covey.backends.claude.bridge import ClaudeBackend
from covey.core import home as covey_home
from covey.core.harness import CONSULT, LEAD, LOOK, PROXY, READ, TOOL_LIMIT, TOOL_NOTES


def settings_of(argv):
    return json.loads(argv[argv.index('--settings') + 1]) if '--settings' in argv else None


class HomeHooksArgvTests(unittest.TestCase):
    """Which Claude Code turns carry the hook, and how it sits in the turn's one --settings."""

    def argv(self, workdir=Path('/x'), **fields):
        options = fields.pop('options', {})
        ctx = make_context(Path('/x'), workdir=workdir, backend_cmd=['claude'], options=options, **fields)
        return claude_argv.stream_argv(ClaudeBackend(), ctx)

    def assert_home_hook(self, hooks, home='/x'):
        [entry] = hooks['PreToolUse']
        self.assertEqual(entry['matcher'], 'Bash|Grep|Glob')  # Never Read, Edit or Write.
        for tool in ('Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
            self.assertNotIn(tool, entry['matcher'].split('|'))
        [hook] = entry['hooks']
        self.assertEqual(hook['type'], 'command')
        words = shlex.split(hook['command'])
        self.assertTrue(words[2].endswith('bin/covey_tool_guard.py'), words)
        self.assertEqual(words[3:], ['--mode', 'home-search', '--home', home])

    def test_a_turn_started_from_the_home_has_the_hook_whatever_else_it_is_held_to(self):
        for options in ({}, {TOOL_LIMIT: LOOK}, {TOOL_LIMIT: READ}, {TOOL_LIMIT: LEAD}):
            with self.subTest(options=options):
                settings = settings_of(self.argv(options=options))
                self.assertEqual(list(settings), ['hooks'])
                self.assert_home_hook(settings['hooks'])

    def test_a_turn_in_a_repository_a_device_a_proxy_or_a_consult_turn_has_none(self):
        self.assertIsNone(settings_of(self.argv(workdir=Path('/x/repo'))))  # Started in a repository: as before.
        self.assertIsNone(settings_of(self.argv(device='box', remote_workdir='~/x')))  # The hook would run on the device.
        self.assertIsNone(settings_of(self.argv(options={TOOL_LIMIT: CONSULT})))
        proxy = settings_of(self.argv(options={TOOL_LIMIT: PROXY, TOOL_NOTES: '/abs/notes'}))
        [entry] = proxy['hooks']['PreToolUse']  # Its 代答 guard alone.
        self.assertEqual(entry['matcher'], 'Bash|Edit|Write|MultiEdit|NotebookEdit')
        self.assertNotIn('home-search', entry['hooks'][0]['command'])

    def test_the_home_through_a_link_is_the_home(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'home'
            home.mkdir()
            (Path(root) / 'link').symlink_to(home)
            ctx = make_context(home, workdir=Path(root) / 'link', backend_cmd=['claude'])
            self.assert_home_hook(claude_argv.home_hooks(ctx), str(home))

    def test_the_extensions_hooks_come_after_it_in_the_one_settings(self):
        hooks = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/abs/hook'}]}],
                 'SessionStart': [{'hooks': [{'type': 'command', 'command': '/abs/start'}]}]}
        argv = self.argv(options={claude_argv.TURN_HOOKS: hooks})
        self.assertEqual(argv.count('--settings'), 1)
        settings = settings_of(argv)
        self.assertEqual(settings['hooks']['SessionStart'], hooks['SessionStart'])
        home, extension = settings['hooks']['PreToolUse']
        self.assert_home_hook({'PreToolUse': [home]})
        self.assertEqual(extension, hooks['PreToolUse'][0])


class HomeSearchTests(unittest.TestCase):
    """search_refusal on a home with a repository (covey/) and a data directory (data/runs/), as the hook reads a call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(os.path.join(tmp.name, 'home'))
        for sub in ('covey/x', 'data/runs'):
            os.makedirs(os.path.join(self.home, sub))
        Path(self.home, 'AGENTS.md').write_text('# home\n')

    def bash(self, command, cwd=None):
        call = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': cwd or self.home}
        return tool_guard.search_refusal(call, self.home)

    def tool(self, name, cwd=None, **given):
        return tool_guard.search_refusal({'tool_name': name, 'tool_input': given, 'cwd': cwd or self.home}, self.home)

    # A recursive search with no depth limit whose target is the home (or above it).
    REFUSED = (
        'rg foo', 'rg --files', 'rg -n "a b" .', 'rg -uuu foo', 'rg foo *', 'rg -g "*.py" foo', 'ripgrep foo',
        'grep -rn foo .', 'grep --directories=recurse foo', 'grep -d recurse foo', 'grep -r foo', 'grep -R --include=*.py foo', 'egrep -Ri x', 'cat x | grep -r foo',
        'find .', 'find', 'find . -name "*.py"', 'find -L . -type f', 'ls -R', 'ls -laR .', 'ls --recursive',
        'du -sh .', 'du -sh *', 'du -d 1', 'du --max-depth=1 .', 'tree', 'tree -a', 'fd py', 'fd -e py', 'fdfind x',
        'rg foo $COVEY_HOME', 'rg foo "${COVEY_HOME}"', 'find / -name libfoo.so', 'rg foo ..',
        'timeout --kill-after=2s 30s rg foo', 'FOO=1 nice -n 5 rg foo', 'env A=1 rg foo', 'cd covey && cd .. && rg foo',
        'echo $(find . -name x)', 'rg foo 2>/dev/null | head -5', 'ls; rg foo', 'true\nrg foo', 'bash -lc "rg foo ."',
        "sh -c 'find . -name x'", 'cd covey && rg x; cd .. && du -sh .',
        "python3 -c \"import os\nfor r, d, f in os.walk('.'):\n    pass\"",
        "python3 - <<'EOF'\nimport os\nfor r, d, f in os.walk(os.getcwd()):\n    print(r)\nEOF",
        "python3 -c \"from pathlib import Path; print(list(Path('.').rglob('*.py')))\"",
        "python3 -c \"from pathlib import Path; print(list(Path().rglob('*.py')))\"",
        "python3 -c \"import glob; glob.glob('**/*', recursive=True)\"",
        "python3 -c \"from pathlib import Path; Path.cwd().glob('**/*.md')\"",
        "python3 -B -c \"import os; os.walk(os.environ['COVEY_HOME'])\"",
    )
    # A concrete repository or subdirectory, a depth limit, a data subdirectory, input from a pipe, a single file.
    ALLOWED = (
        'rg foo covey', 'rg foo covey/x data/runs', 'cd covey && rg foo', 'cd covey; rg --files', 'cd covey && find .',
        'pushd covey && grep -rn foo .', 'find . -maxdepth 2 -name "*.md"', 'find covey -name x', 'rg --max-depth 2 foo',
        'rg -d 1 foo', 'fd -d 2 py', 'fd py covey', 'tree -L 2', 'ls', 'ls -la', 'ls -R covey', 'du -sh covey',
        'du -sh covey/*', 'grep foo AGENTS.md', 'grep -rn foo covey/', 'cat x | rg foo', 'git log | rg foo',
        'rg foo < AGENTS.md', 'cat data/runs/x.json', 'rg -n foo data/runs', 'find data -maxdepth 3 -name x',
        'find data/runs -name "*.json"', 'ls -R data/runs', 'rg --files data/runs', 'rg foo /tmp', 'find /tmp -name x',
        "python3 -c \"import os; os.walk('covey')\"", "python3 -c \"import glob; glob.glob('**/*')\"",
        "python3 -c \"import glob; glob.glob('covey/**/*.py', recursive=True)\"", 'python3 script.py',
        "cat <<'EOF' > notes.md\nfind . -name x\nrg foo\nEOF", 'rg --version', 'rg --type-list', 'grep -c x a.txt',
        'echo rg foo', 'git grep foo', 'ls | xargs rg foo',
        # What it cannot tell runs: another variable, a directory it cannot follow, an open quote.
        'rg foo $SOMEWHERE', 'cd - && rg foo', 'cd "$DIR" && rg foo', 'echo "unterminated',
    )

    def test_a_recursive_search_over_the_whole_home_is_refused_with_what_to_do(self):
        for command in self.REFUSED:
            with self.subTest(command=command):
                reason = self.bash(command)
                self.assertIn(self.home, reason)
                self.assertIn('没有执行', reason)
        self.assertIn('cd <仓库> && rg', self.bash('rg foo'))
        self.assertIn('Python', self.bash("python3 -c \"import os; os.walk('.')\""))
        self.assertIn(self.home, self.bash(f'rg foo {self.home}'))  # The home by its absolute path, from anywhere.
        self.assertIn(self.home, self.bash(f'find {self.home} -name x', cwd='/tmp'))
        self.assertIn(self.home, self.bash('rg foo ..', cwd=os.path.join(self.home, 'covey')))

    def test_a_search_in_a_repository_a_subdirectory_or_with_a_depth_limit_runs(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), '')
        covey = os.path.join(self.home, 'covey')
        self.assertEqual(self.bash('rg foo', cwd=covey), '')  # The session's cwd already in a repository.
        self.assertEqual(self.bash('find . -name x', cwd=covey), '')
        self.assertEqual(self.bash(f'rg foo {self.home}/data/runs', cwd='/tmp'), '')

    def test_the_grep_tool_with_the_home_as_its_path_is_refused(self):
        self.assertIn('Grep', self.tool('Grep', pattern='x'))  # No path: the cwd, the home.
        self.assertIn('Grep', self.tool('Grep', pattern='x', path=self.home))
        self.assertIn('Grep', self.tool('Grep', pattern='x', path='.', glob='covey/**'))  # Its glob filters, not narrows.
        self.assertEqual(self.tool('Grep', pattern='x', path='covey'), '')
        self.assertEqual(self.tool('Grep', pattern='x', path=os.path.join(self.home, 'data', 'runs')), '')
        self.assertEqual(self.tool('Grep', pattern='x', path='AGENTS.md'), '')
        self.assertEqual(self.tool('Grep', pattern='x', cwd=os.path.join(self.home, 'covey')), '')

    def test_the_glob_tool_with_double_star_from_the_home_is_refused(self):
        self.assertIn('Glob', self.tool('Glob', pattern='**/*.py'))
        self.assertIn('Glob', self.tool('Glob', pattern='**/*', path=self.home))
        self.assertIn('Glob', self.tool('Glob', pattern=self.home + '/**/*.md'))
        self.assertEqual(self.tool('Glob', pattern='*.md'), '')  # One level.
        self.assertEqual(self.tool('Glob', pattern='*/*.md'), '')
        self.assertEqual(self.tool('Glob', pattern='covey/**/*.py'), '')  # Starts at a repository.
        self.assertEqual(self.tool('Glob', pattern='**/*', path=os.path.join(self.home, 'covey')), '')
        self.assertEqual(self.tool('Glob', pattern=self.home + '/data/runs/**/*.json'), '')

    def test_read_edit_write_and_other_tools_are_never_looked_at(self):
        for name, given in (('Read', {'file_path': self.home}), ('Edit', {'file_path': self.home + '/AGENTS.md'}),
                            ('Write', {'file_path': self.home + '/x'}), ('WebFetch', {'url': 'rg foo'}),
                            ('Task', {'prompt': 'rg foo'})):
            self.assertEqual(self.tool(name, **given), '', name)

    def test_it_lets_the_call_run_when_anything_goes_wrong(self):
        self.assertEqual(tool_guard.search_refusal({'tool_name': 'Bash', 'tool_input': 'rg foo'}, self.home), '')
        self.assertEqual(tool_guard.search_refusal({'tool_name': 'Bash', 'tool_input': {'command': 'rg foo'}}, ''), '')
        self.assertEqual(tool_guard.search_refusal({'tool_name': 'Grep', 'tool_input': {'path': 3}}, self.home), '')
        with mock.patch.object(tool_guard, 'bash_refusal', side_effect=RuntimeError('boom')):
            self.assertEqual(self.bash('rg foo'), '')
        with mock.patch.object(tool_guard, 'covers_home', side_effect=OSError('nas')):
            self.assertEqual(self.tool('Grep', pattern='x'), '')
        with mock.patch.object(tool_guard.shlex, 'shlex', side_effect=ValueError('No closing quotation')):
            self.assertEqual(self.bash('rg foo'), '')


class HomeSearchHookTests(unittest.TestCase):
    """main in its home-search mode: exit 2 with the reason for a refused call, 0 for anything else, its own faults
    included; the 代答 mode is as it was."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(self.home, 'covey'))

    def run_main(self, text, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tool_guard.main(list(args), stdin=io.StringIO(text))
        return code, err.getvalue()

    def call(self, command, cwd=None):
        return json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': cwd or self.home})

    def test_a_refused_call_exits_2_with_the_reason_and_another_exits_0(self):
        code, said = self.run_main(self.call('rg foo'), '--mode', 'home-search', '--home', self.home)
        self.assertEqual(code, 2)
        self.assertIn('没有执行', said)
        self.assertEqual(self.run_main(self.call('rg foo covey'), '--mode', 'home-search', '--home', self.home), (0, ''))
        self.assertEqual(self.run_main(self.call('rg foo'), '--mode=home-search', '--home', self.home)[0], 2)

    def test_what_it_cannot_read_lets_the_call_run(self):
        for text in ('not json', '[]', '{"tool_name": "Bash"}', '', json.dumps({'tool_name': 'Bash', 'tool_input': None})):
            with self.subTest(text=text):
                self.assertEqual(self.run_main(text, '--mode', 'home-search', '--home', self.home), (0, ''))
        self.assertEqual(self.run_main(self.call('rg foo'), '--mode', 'home-search')[0], 0)  # No home given.
        code, _ = self.run_main(self.call('rg foo'), '--mode', 'home-search', '--home', self.home, '--bogus')
        self.assertEqual(code, 0)  # An argument it does not take.
        with mock.patch.object(tool_guard, 'search_refusal', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_main(self.call('rg foo'), '--mode', 'home-search', '--home', self.home), (0, ''))

    def test_the_proxy_mode_still_refuses_what_it_cannot_read(self):
        self.assertEqual(self.run_main('not json', '--notes', '/abs/notes')[0], 2)
        self.assertEqual(self.run_main('not json', '--mode', 'proxy')[0], 2)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                tool_guard.main(['--bogus'], stdin=io.StringIO('{}'))

    def test_the_hook_command_argv_writes_runs_as_claude_code_would_run_it(self):
        ctx = make_context(Path(self.home), backend_cmd=['claude'])
        [entry] = claude_argv.home_hooks(ctx)['PreToolUse']
        command = shlex.split(entry['hooks'][0]['command'])
        for given, expected in (('rg foo', 2), ('rg foo covey', 0), ('find . -maxdepth 2', 0)):
            with self.subTest(command=given):
                done = subprocess.run(command, input=self.call(given), capture_output=True, text=True, timeout=60,
                                      cwd=self.home)
                self.assertEqual(done.returncode, expected, done.stderr)
                self.assertEqual(bool(done.stderr.strip()), expected == 2)
        done = subprocess.run(command, input='garbage', capture_output=True, text=True, timeout=60, cwd=self.home)
        self.assertEqual(done.returncode, 0)


SHIM_BIN = Path(__file__).resolve().parents[1] / 'bin'


class SearchShimTests(unittest.TestCase):
    """The search shims in <数据根>/bin (core/home.py ``ensure_shim``, ``SEARCH_SHIM_TEXT``) run for real, in front of fake
    programs of the same names: the home and the fake home are made in a directory that may be executed."""

    def setUp(self):
        tmp = exec_tempdir()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        self.home, self.data, self.real = root / 'home', root / 'data', root / 'real'
        for folder in (self.home / 'repo' / 'sub', self.home / 'data-dir', self.real, root / 'elsewhere'):
            folder.mkdir(parents=True)
        (self.home / 'repo' / 'a.txt').write_text('x\n')
        for name in covey_home.SEARCH_SHIMS:
            fake = self.real / name
            fake.write_text(f'#!/bin/sh\necho "REAL {name} $*"\n')
            fake.chmod(0o755)
        covey_home.ensure_shim(self.data, tool_bin=SHIM_BIN)
        self.bin = self.data / 'bin'
        self.path = os.pathsep.join([str(self.bin), str(self.real), '/usr/bin', '/bin'])

    def shim(self, command, cwd=None, *, env=None, stdin=None, path=None):
        """``command`` (one shim name and its words) in a sh, as a Harness's shell would run it."""
        env = {'PATH': path or self.path, 'COVEY_HOME': str(self.home), **(env or {})}
        env = {key: value for key, value in env.items() if value is not None}
        return subprocess.run(['/bin/sh', '-c', command], cwd=str(cwd or self.home), env=env, timeout=60,
                              stdin=stdin if stdin is not None else subprocess.DEVNULL,
                              capture_output=True, text=True)

    def assertRefused(self, done):
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stdout, '')
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn('递归扫整个主目录', done.stderr)

    def assertRan(self, done, name):
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(f'REAL {name}'), done.stdout)

    def test_the_shims_are_written_for_the_search_commands_and_are_valid_sh(self):
        self.assertEqual(sorted(path.name for path in self.bin.iterdir()), sorted(['covey', *covey_home.SEARCH_SHIMS]))
        for name in covey_home.SEARCH_SHIMS:
            shim = self.bin / name
            self.assertTrue(shim.stat().st_mode & 0o100, name)
            self.assertEqual(subprocess.run(['/bin/sh', '-n', str(shim)], capture_output=True).returncode, 0)
            self.assertIn(f'"{SHIM_BIN}/covey_tool_guard.py"', shim.read_text())
            self.assertIn(covey_home.SHIM_MARK, shim.read_text())
        bash = subprocess.run(['bash', '-n', str(self.bin / 'rg')], capture_output=True)
        self.assertEqual(bash.returncode, 0, bash.stderr)

    def test_a_search_of_the_whole_home_is_refused_with_one_line_and_exit_2(self):
        for command in ('rg x', 'rg x .', f'rg x {self.home}', 'rg --files', 'find', f'find {self.home}', 'find . -name x',
                        f'du -sh {self.home}', 'du -sh .', 'tree', 'fd x', 'ls -R', f'ls -lR {self.home}',
                        'ls --recursive', 'grep -r x .', 'grep -rn x', f'grep -R x {self.home}', 'grep -d recurse x',
                        'grep --directories=recurse x', 'rg x ..'):
            with self.subTest(command=command):
                self.assertRefused(self.shim(command))
        self.assertRefused(self.shim('cd .. && rg x', cwd=self.home / 'repo'))
        self.assertRefused(self.shim(f'rg x {self.home.parent}', cwd=self.home / 'repo'))
        self.assertRefused(self.shim('cd ' + str(self.home) + ' && rg x'))

    def test_a_search_of_a_repository_a_depth_limit_or_not_a_search_runs_the_real_program(self):
        for command, name in (('rg x repo', 'rg'), (f'rg x {self.home}/repo', 'rg'), ('find repo -name x', 'find'),
                              ('find . -maxdepth 2', 'find'), ('rg --max-depth 2 x', 'rg'), ('tree -L 2', 'tree'),
                              ('fd -d 2 x', 'fd'), ('ls', 'ls'), ('ls -la', 'ls'), ('ls -R repo', 'ls'),
                              ('grep x a.txt', 'grep'), ('grep -c x a.txt', 'grep'), ('du -sh repo', 'du'),
                              ('rg --version', 'rg'), ('grep -r x repo', 'grep')):
            with self.subTest(command=command):
                self.assertRan(self.shim(command), name)
        for command, name in (('rg x', 'rg'), ('rg x .', 'rg'), ('find . -name x', 'find'), ('grep -rn x', 'grep'),
                              ('ls -R', 'ls'), ('du -sh .', 'du'), (f'rg x {self.home}/repo/sub', 'rg')):
            with self.subTest(command=command, cwd='repo'):
                self.assertRan(self.shim(command, cwd=self.home / 'repo'), name)
        self.assertRan(self.shim('rg x', cwd=self.data), 'rg')  # Another directory entirely.
        self.assertRan(self.shim('rg x', cwd=self.home / 'data-dir'), 'rg')

    def test_the_real_program_gets_its_arguments_untouched(self):
        done = self.shim("rg 'a b' 'c d' --max-depth 2")
        self.assertEqual(done.stdout.strip(), 'REAL rg a b c d --max-depth 2')

    def test_what_a_pipe_or_a_file_feeds_to_rg_is_not_a_search_of_the_home(self):
        done = self.shim('rg x', stdin=subprocess.PIPE)  # Overridden below: a real pipe with data.
        done = subprocess.run(['/bin/sh', '-c', 'echo hello | rg hello'], cwd=str(self.home), timeout=60, text=True,
                              env={'PATH': self.path, 'COVEY_HOME': str(self.home)}, capture_output=True)
        self.assertRan(done, 'rg')
        done = subprocess.run(['/bin/sh', '-c', 'rg hello < repo/a.txt'], cwd=str(self.home), timeout=60, text=True,
                              env={'PATH': self.path, 'COVEY_HOME': str(self.home)}, capture_output=True)
        self.assertRan(done, 'rg')
        self.assertRefused(self.shim('rg hello'))  # No stdin data (/dev/null): it walks the directory.

    def test_without_covey_home_every_command_runs_as_it_is(self):
        for command, name in (('rg x', 'rg'), ('find .', 'find'), ('ls -R', 'ls'), ('grep -r x .', 'grep')):
            with self.subTest(command=command):
                self.assertRan(self.shim(command, env={'COVEY_HOME': None}), name)
                self.assertRan(self.shim(command, env={'COVEY_HOME': ''}), name)

    def test_a_fault_in_the_shim_lets_the_real_program_run(self):
        fake = self.data / 'release' / 'bin'
        fake.mkdir(parents=True)
        for body in ('raise SystemExit(2)\n', 'raise RuntimeError("boom")\n', 'import sys; sys.exit(1)\n', ''):
            with self.subTest(guard=body):
                (fake / 'covey_tool_guard.py').write_text(body)
                covey_home.ensure_shim(self.data, tool_bin=fake, force=True)
                self.assertRan(self.shim('rg x'), 'rg')
                self.assertRan(self.shim('find .'), 'find')
        (fake / 'covey_tool_guard.py').unlink()  # The release's bin/ is gone.
        self.assertRan(self.shim('rg x'), 'rg')
        covey_home.ensure_shim(self.data, tool_bin=SHIM_BIN, force=True)
        text = (self.bin / 'rg').read_text().replace(sys.executable, str(self.data / 'no-such-python'))
        (self.bin / 'rg').write_text(text)  # An interpreter that does not start.
        self.assertRan(self.shim('rg x'), 'rg')
        self.assertEqual(self.shim('rg x', path=str(self.bin)).returncode, 127)  # Nothing real to run.

    def test_the_real_program_is_looked_for_past_the_shim_directory_however_it_is_in_path(self):
        link = self.data / 'link-bin'
        link.symlink_to(self.bin)
        twice = os.pathsep.join([str(self.bin), str(link), str(self.bin) + '/', '', str(self.real), '/bin'])
        for path in (twice, os.pathsep.join([str(self.real), str(self.bin)]), str(link) + os.pathsep + str(self.real)):
            with self.subTest(path=path):
                self.assertRan(self.shim('rg x repo', path=path), 'rg')
        done = self.shim('rg x repo', path=os.pathsep.join([str(self.bin), str(link)]))
        self.assertEqual(done.returncode, 127, done.stderr)  # No loop: the shim only.
        self.assertRan(subprocess.run([str(link / 'rg'), 'x', 'repo'], cwd=str(self.home), timeout=60, text=True,
                                      capture_output=True, env={'PATH': twice, 'COVEY_HOME': str(self.home)}), 'rg')

    def test_the_shim_dir_not_in_path_still_finds_the_real_program(self):
        done = subprocess.run([str(self.bin / 'rg'), 'x', 'repo'], cwd=str(self.home), timeout=60, text=True,
                              capture_output=True, env={'PATH': f'{self.real}:/bin', 'COVEY_HOME': str(self.home)})
        self.assertRan(done, 'rg')

    def test_a_deploy_rewrites_the_shims_and_a_foreign_file_is_left_alone(self):
        foreign = self.bin / 'tree'
        foreign.write_text('#!/bin/sh\necho mine\n')
        old = self.data / 'rel1' / 'bin'
        old.mkdir(parents=True)
        (old / 'covey_tool_guard.py').write_text('')
        covey_home.ensure_shim(self.data, tool_bin=old, force=True)
        self.assertIn(f'"{old}/covey_tool_guard.py"', (self.bin / 'rg').read_text())
        self.assertEqual(foreign.read_text(), '#!/bin/sh\necho mine\n')
        (self.bin / 'rg').unlink()
        covey_home.ensure_shim(self.data, tool_bin=SHIM_BIN)  # A missing one is written again, the others kept.
        self.assertIn(f'"{SHIM_BIN}/covey_tool_guard.py"', (self.bin / 'rg').read_text())
        self.assertIn(f'"{old}/covey_tool_guard.py"', (self.bin / 'find').read_text())
        (old / 'covey_tool_guard.py').unlink()
        covey_home.ensure_shim(self.data, tool_bin=SHIM_BIN)  # Its release is gone: rewritten.
        self.assertIn(f'"{SHIM_BIN}/covey_tool_guard.py"', (self.bin / 'find').read_text())


class ShimModeTests(unittest.TestCase):
    """main in its shim mode (backends/claude/tool_guard.py ``shim_main``): the rule is the home-search one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(self.home, 'repo'))

    def run_shim(self, *words, cwd=None, home=None, stdin='/dev/null'):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'COVEY_HOME': self.home if home is None else home}), \
                mock.patch('os.getcwd', return_value=cwd or self.home), contextlib.redirect_stderr(err), \
                mock.patch('os.fstat', return_value=os.stat(stdin)):
            code = tool_guard.main(['--mode', 'shim', *words])
        return code, err.getvalue()

    def test_it_refuses_with_the_code_the_shim_turns_into_2_and_one_line(self):
        for words in (('rg', 'x'), ('find',), ('du', '-sh'), ('ls', '-R'), ('grep', '-rn', 'x'), ('fd', 'x'), ('tree',)):
            with self.subTest(words=words):
                code, said = self.run_shim(*words)
                self.assertEqual(code, tool_guard.SHIM_REFUSED)
                self.assertEqual(len(said.strip().splitlines()), 1)
        self.assertEqual(self.run_shim('rg', 'x', cwd=os.path.join(self.home, 'repo'))[0], 0)
        self.assertEqual(self.run_shim('find', '.', '-maxdepth', '2')[0], 0)
        self.assertEqual(self.run_shim('ls')[0], 0)
        self.assertEqual(self.run_shim('rg', 'x', home='')[0], 0)
        self.assertEqual(self.run_shim('rg', 'x', stdin=self.home)[0], tool_guard.SHIM_REFUSED)  # A directory is no input.
        self.assertEqual(self.run_shim('rg', 'x', stdin=os.path.join(self.home, '..'))[0], tool_guard.SHIM_REFUSED)
        os.mkfifo(os.path.join(self.home, 'fifo'))
        self.assertEqual(self.run_shim('rg', 'x', stdin=os.path.join(self.home, 'fifo'))[0], 0)  # A pipe is: ``… | rg x``.

    def test_what_goes_wrong_lets_it_run(self):
        with mock.patch.object(tool_guard, 'searcher_targets', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_shim('rg', 'x'), (0, ''))
        with mock.patch.object(tool_guard, 'shim_refusal', side_effect=RuntimeError('boom')):
            self.assertEqual(self.run_shim('rg', 'x'), (0, ''))
        self.assertEqual(self.run_shim(), (0, ''))
        self.assertEqual(self.run_shim('cat', 'x'), (0, ''))


if __name__ == '__main__':
    unittest.main()

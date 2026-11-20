"""scripts/deploy_bridge.sh (arguments and pre-checks, --check-only on a fake state directory and
service root; nothing is packed or bootstrapped) and scripts/cardkit_check.py (cards from samples and
files, measured; CardKit's create faked, no other call), scripts/golden_trace.py (a throwaway test module
recorded three times and a sleeping one terminated, one Python child each; compare on hand-made recordings)
and scripts/cross_release.py (its output checks and compare). Each deploy run is one short-lived bash and
Python child under TMPDIR."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import REPO, exec_tempdir

from covey.service.manager import runtime_environment

_spec = importlib.util.spec_from_file_location("cardkit_check_script", REPO / "scripts" / "cardkit_check.py")
cardkit_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cardkit_check)
_spec = importlib.util.spec_from_file_location("golden_trace_script", REPO / "scripts" / "golden_trace.py")
golden_trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden_trace)

_spec = importlib.util.spec_from_file_location("cross_release_script", REPO / "scripts" / "cross_release.py")
cross_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cross_release)

DEPLOY = REPO / "scripts" / "deploy_bridge.sh"
APP = "cli_test"


class DeployFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data, self.services, self.workdir = self.root / "data", self.root / "services", self.root / "work"
        self.state = self.data / f"harness-{APP}"
        for path in (self.state, self.services / "services", self.workdir):
            path.mkdir(parents=True)
        self.env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(self.root)), "LANG": "C.UTF-8",
                    "TMPDIR": str(self.root), "COVEY_DATA_HOME": str(self.data),
                    "COVEY_SERVICE_ROOT": str(self.services), "APP_ID": APP}
        self.args = ["--harness", "unified", "--state-dir", str(self.state), "--workdir", str(self.workdir),
                     "--profile", "bot", "--app-id", APP, "--sandbox", "danger-full-access",
                     "--harness-config", str(self.data / "harnesses.json"), "--sandbox", "danger-full-access"]
        self.manifest(runtime_environment(self.env), self.args)

    def manifest(self, environment, args):
        python = shutil.which("python3", path=self.env["PATH"])
        (self.services / "services" / "a.json").write_text(json.dumps({
            "state_dir": str(self.state.resolve()), "environment": environment,
            "command": [python, "-B", "/releases/old/bin/covey_bridge.py", *args]}))

    def deploy(self, *args, env=None):
        child = subprocess.run(["bash", str(DEPLOY), *args], env=env or self.env, capture_output=True, text=True,
                               timeout=120)
        lines = child.stdout.splitlines()
        self.assertEqual(len(lines), 1, child.stdout + child.stderr)
        return child.returncode, json.loads(lines[0])


class DeployScriptTests(DeployFixture):
    def test_check_only_takes_the_names_of_the_operations_guide_and_deploys_nothing(self):
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--",
                                "--sandbox", "danger-full-access")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["ok"], out["note"], out["state_dir"]), (True, "check only, nothing deployed", str(self.state)))
        self.assertEqual(out["bootstrap"], ["--workdir", str(self.workdir), "--profile", "bot", "--app-id", APP,
                                            "--state-dir", str(self.state), "--harness-config",
                                            str(self.data / "harnesses.json"), "--sandbox", "danger-full-access"])
        self.assertEqual(out["commit"], subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                                                       text=True).stdout.strip())
        self.assertEqual({key: value for key, value in out["pre"].items() if value},
                         {"env_names": len(runtime_environment(self.env))})
        self.assertTrue(out["run_dir"].startswith(str(self.root / "bridge-deploy-")))
        self.assertFalse((self.data / "releases").exists())

    def test_the_live_manifests_old_names_for_the_butler_switches_are_no_difference_under_the_new_ones(self):
        # 2026-11-16: COVEY_MAIN_THREAD and the others became COVEY_BUTLER...; a manifest keeps what older releases read.
        live = {"LARK_BRIDGE_MAIN_THREAD": "1", "LARK_BRIDGE_MAIN_TRACKS": "1", "LARK_BRIDGE_MAIN_DISPATCH_CARD": "1",
                "LARK_BRIDGE_MAIN_BUTLER": "1"}
        shells = {"the old names": live,
                  "the middle names": {"COVEY_MAIN_THREAD": "1", "COVEY_MAIN_TRACKS": "1", "COVEY_MAIN_DISPATCH_CARD": "1",
                                       "COVEY_MAIN_BUTLER": "1"},
                  "the new names": {"COVEY_BUTLER": "1", "COVEY_BUTLER_TRACKS": "1", "COVEY_DISPATCH_CARD": "1",
                                    "COVEY_BUTLER_RULES": "1"}}
        for what, shell in shells.items():
            self.manifest({**runtime_environment(self.env), **live}, self.args)
            code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--",
                                    "--sandbox", "danger-full-access", env={**self.env, **shell})
            self.assertEqual(code, 0, (what, out))
            pre = out["pre"]
            self.assertEqual((pre["env_missing"], pre["env_extra"], pre["env_value_diff"]), ([], [], []), what)
            self.assertEqual(pre["env_names"], len(runtime_environment(self.env)) + 4, what)
        # A manifest holding the new names (written by hand) is compared as the old ones.
        self.manifest({**runtime_environment(self.env), **shells["the new names"]}, self.args)
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--",
                                "--sandbox", "danger-full-access", env={**self.env, **live})
        self.assertEqual(code, 0, out)
        self.assertEqual((out["pre"]["env_missing"], out["pre"]["env_extra"], out["pre"]["env_value_diff"]), ([], [], []))

    def test_each_pre_check_blocks_and_a_command_change_needs_leave(self):
        (self.state / "proxy" / "batches").mkdir(parents=True)
        (self.state / "proxy" / "batches" / "b1.json").write_text('{"status": "sending"}')
        (self.state / "proxy" / "batches" / "b2.json").write_text('{"status": "sent"}')
        (self.state / "launch-forms").mkdir()
        (self.state / "launch-forms" / "f.json").write_text('{"status": "creating_worktree"}')
        self.manifest({**runtime_environment(self.env), "LARK_BRIDGE_DATA_HOME": "/elsewhere",
                       "LARK_BRIDGE_PROXY_PYTHON": "/p"}, self.args)
        env = {**self.env, "COVEY_CARD_TRANSPORT": "cardkit"}  # The shell's new spelling is compared as the old one.
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "other", env=env)
        self.assertEqual((code, out["ok"]), (1, False))
        pre = out["pre"]
        self.assertEqual((pre["proxy_sending"], pre["creating_worktree"]), (["b1"], ["f.json"]))
        self.assertEqual((pre["env_missing"], pre["env_extra"], pre["env_value_diff"]),
                         (["LARK_BRIDGE_PROXY_PYTHON"], ["LARK_BRIDGE_CARD_TRANSPORT"], ["LARK_BRIDGE_DATA_HOME"]))
        self.assertEqual(pre["command_diff"]["live"][2:], self.args)
        self.assertIn("other", pre["command_diff"]["planned"])
        self.assertEqual(out["error"], "pre-checks failed: ['creating_worktree', 'proxy_sending', 'env_missing', "
                                       "'env_extra', 'env_value_diff', 'command_diff']")
        self.manifest(runtime_environment(self.env), self.args)
        shutil.rmtree(self.state / "proxy")
        shutil.rmtree(self.state / "launch-forms")
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "other",
                                "--allow-command-change", "--", "--sandbox", "danger-full-access")
        self.assertEqual((code, out["ok"]), (0, True), out)
        self.assertTrue(out["pre"]["command_diff"])

    def test_a_permission_variable_no_longer_read_blocks_in_the_manifest_or_the_shell(self):
        # core/ignored_env.py: these restricted the backends, which now run with their open defaults.
        sandbox = {"LARK_BRIDGE_CODEX_SANDBOX": "workspace-write"}
        cases = (("manifest and shell", sandbox, sandbox, ["LARK_BRIDGE_CODEX_SANDBOX=workspace-write"]),
                 ("manifest only", {"LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "0"}, {},
                  ["LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS=0"]),
                 ("shell only", {}, {"LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "false"},
                  ["LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS=false"]),
                 ("at the defaults", {"LARK_BRIDGE_CODEX_SANDBOX": "danger-full-access",
                                      "LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "1"},
                  {"LARK_BRIDGE_CODEX_SANDBOX": "danger-full-access", "LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS": "1"}, []))
        for what, kept, shell, flagged in cases:
            with self.subTest(what):
                env = {**self.env, **shell}
                self.manifest({**runtime_environment(self.env), **kept}, self.args)
                code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot",
                                        "--", "--sandbox", "danger-full-access", env=env)
                self.assertEqual([line.split(" ", 1)[0] for line in out["pre"]["permission_env"]], flagged)
                if flagged:
                    self.assertEqual((code, out["ok"]), (1, False))
                    self.assertIn("'permission_env'", out["error"])
                    self.assertIn("unset it and put --", out["pre"]["permission_env"][0])
                else:
                    self.assertEqual((code, out["ok"]), (0, True), out)

    def test_another_running_service_s_permission_variable_blocks_a_stopped_one_s_does_not(self):
        # r4 review C1: covey_doctor --runtime-only after the deploy fails on every running service's; the pre-check
        # looks at the same ones and names them.
        loose = {"LARK_BRIDGE_CODEX_SANDBOX": "workspace-write"}
        for state, flagged in (("stopped", []), ("running", ["service other: LARK_BRIDGE_CODEX_SANDBOX=workspace-write"])):
            with self.subTest(state):
                (self.services / "services" / "b.json").write_text(json.dumps({
                    "id": "other", "state_dir": str(self.root / "other-state"), "desired_state": state,
                    "environment": loose, "command": []}))
                code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot",
                                        "--", "--sandbox", "danger-full-access")
                self.assertEqual([line.split(" is no longer read", 1)[0] for line in out["pre"]["permission_env"]],
                                 flagged)
                self.assertEqual((code, out["ok"]), (1, False) if flagged else (0, True), out)

    def test_missing_settings_are_named_and_stop_before_any_check(self):
        cases = [((), {}, "missing --workdir, --profile"),
                 (("--workdir", str(self.workdir), "--profile", "bot"), {"APP_ID": ""}, "missing --app-id or APP_ID"),
                 (("--workdir", str(self.root / "nowhere"), "--profile", "bot"), {}, "is not a directory"),
                 (("--workdir", str(self.workdir), "--profile", "bot"), {"COVEY_DATA_HOME": ""},
                  "set COVEY_DATA_HOME"),
                 (("--workdir", str(self.workdir), "--profile", "bot", "--commit", "no-such-rev"), {},
                  "no-such-rev is not a commit")]
        for args, env, error in cases:
            with self.subTest(error=error):
                code, out = self.deploy("--check-only", *args, env={**self.env, **env})
                self.assertEqual(code, 2)
                self.assertIn(error, out["error"])
                self.assertNotIn("pre", out)
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot",
                                "--state-dir", str(self.root / "other-state"))
        self.assertEqual((code, out["state_dir"]), (1, str(self.root / "other-state")))
        self.assertIn("expected one manifest", out["error"])



class DeployQueueTests(DeployFixture):
    """One deploy at a time (<service root>/deploy.lock) and no rollback of the live release (pre.rollback)."""

    def hold_lock(self, said):
        import fcntl

        fd = os.open(self.services / "deploy.lock", os.O_RDWR | os.O_CREAT, 0o644)
        os.write(fd, json.dumps(said).encode())
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def test_a_deploy_while_another_runs_exits_3_at_once_and_a_check_only_reports_it(self):
        holder = {"pid": 4242, "commit": "f" * 40, "task": "/absolute/runs/x"}
        fd = self.hold_lock(holder)
        try:
            code, out = self.deploy("--workdir", str(self.workdir), "--profile", "bot", "--", "--sandbox", "danger-full-access")
            self.assertEqual((code, out["ok"], out["deploy_running"]), (3, False, holder), out)
            self.assertIn("another deploy is running", out["error"])
            self.assertNotIn("pre", out)  # Nothing was checked, packed or bootstrapped.
            self.assertFalse((self.data / "releases").exists())
            code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--",
                                    "--sandbox", "danger-full-access")
            self.assertEqual((code, out["ok"], out["pre"]["deploy_running"]), (0, True, holder), out)
        finally:
            os.close(fd)
        code, out = self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--",
                                "--sandbox", "danger-full-access")
        self.assertEqual((code, out["pre"]["deploy_running"]), (0, None), out)

    def test_a_deploy_holds_the_lock_and_says_who_it_is_until_it_exits(self):
        import fcntl

        # The bootstrap stub of a release that exists already fails at once; while it runs the lock is held.
        spare = exec_tempdir()
        self.addCleanup(spare.cleanup)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        release = fake_release(Path(spare.name) / "release")
        (self.data / "releases").mkdir(parents=True)
        (self.data / "releases" / head).symlink_to(release)
        probe, seen_file = self.root / "probe.py", self.root / "probe.json"
        probe.write_text("import fcntl, json, os, sys\n"
                         "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                         "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    held = False\n"
                         "except BlockingIOError:\n    held = True\n"
                         "json.dump({'held': held, 'said': json.loads(os.pread(fd, 4096, 0))}, open(sys.argv[2], 'w'))\n")
        stub = release / "scripts" / "bootstrap_harness_bridge.sh"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(f"#!/bin/bash\n'{sys.executable}' -I '{probe}' '{self.services / 'deploy.lock'}' '{seen_file}'\nexit 1\n")
        stub.chmod(0o755)
        code, out = self.deploy("--workdir", str(self.workdir), "--profile", "bot", "--", "--sandbox", "danger-full-access")
        self.assertEqual((code, out["error"]), (1, "bootstrap failed, see bootstrap.log"), out)
        seen = json.loads(seen_file.read_text())
        self.assertTrue(seen["held"])  # The bootstrap (and the Bridge it starts) does not inherit the lock.
        self.assertEqual((seen["said"]["commit"], seen["said"]["run_dir"]), (head, out["run_dir"]))
        fd = os.open(self.services / "deploy.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Released when the deploy exited.
        finally:
            os.close(fd)

    def objects(self):
        """A git environment whose new objects go to a temporary store over this checkout's (no history needed)."""
        store = self.root / "objects"
        store.mkdir()
        mine = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        return {**self.env, "GIT_OBJECT_DIRECTORY": str(store), "GIT_ALTERNATE_OBJECT_DIRECTORIES": mine,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@localhost"}

    def live(self, release_id):
        python = shutil.which("python3", path=self.env["PATH"])
        entry = self.data / "releases" / release_id / "bin" / "covey_bridge.py"
        (self.services / "services" / "a.json").write_text(json.dumps({
            "state_dir": str(self.state.resolve()), "environment": runtime_environment(self.env),
            "command": [python, "-B", str(entry), *self.args]}))

    def check(self, env, *args):
        return self.deploy("--check-only", "--workdir", str(self.workdir), "--profile", "bot", *args, "--",
                           "--sandbox", "danger-full-access", env=env)

    def test_an_older_commit_than_the_live_release_is_refused_unless_asked_for(self):
        env = self.objects()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, env=env, capture_output=True, text=True).stdout.strip()
        newer = subprocess.run(["git", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "newer"], cwd=REPO, env=env,
                               capture_output=True, text=True).stdout.strip()
        self.assertTrue(newer)
        self.live(newer)  # Live is a commit after HEAD: deploying HEAD would go back.
        code, out = self.check(env)
        self.assertEqual((code, out["ok"], out["pre"]["live_release"]), (1, False, newer), out)
        self.assertEqual(out["pre"]["rollback"], [f"core: {head[:8]} does not contain the live {newer[:8]} (an older or another line)"])
        self.assertIn("'rollback'", out["error"])
        code, out = self.check(env, "--allow-rollback")
        self.assertEqual((code, out["ok"]), (0, True), out)
        self.live(head)  # Live is HEAD: its child, and HEAD again, go forward.
        for rev in (newer, "HEAD"):
            code, out = self.check(env, "--commit", rev)
            self.assertEqual((code, out["ok"], out["pre"]["rollback"], out["pre"]["live_release"]), (0, True, [], head), out)

    def test_the_live_release_manifest_names_its_commits_and_an_unknown_one_needs_a_fetch(self):
        env = self.objects()
        newer = subprocess.run(["git", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "newer"], cwd=REPO, env=env,
                               capture_output=True, text=True).stdout.strip()
        rid = newer[:8] + "-0123abcd"
        (self.data / "releases" / rid).mkdir(parents=True)
        (self.data / "releases" / rid / "RELEASE_MANIFEST").write_text(f"core {newer}\nextension {'0' * 40} covey_internal\n")
        self.live(rid)
        code, out = self.check(env)  # No --ext-repo: only the core is compared, from the manifest's full commit.
        self.assertEqual(out["pre"]["rollback"], [f"core: {subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True).stdout.strip()[:8]} does not contain the live {newer[:8]} (an older or another line)"])
        self.live("0123456789abcdef0123456789abcdef01234567")
        code, out = self.check(env)
        self.assertEqual(code, 1, out)
        self.assertEqual(len(out["pre"]["rollback"]), 1)
        self.assertIn("core: the live 0123456789ab is not in", out["pre"]["rollback"][0])
        self.assertIn("fetch it first", out["pre"]["rollback"][0])


def make_repo(path, files, message="init"):
    """A git repository at path with the given files ({relative path: text}, one commit); returns its commit."""
    for name, text in files.items():
        target = Path(path) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}
    for cmd in (["init", "-q", "-b", "master"], ["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(["git", *cmd], cwd=path, env=env, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()


ASSEMBLE = REPO / "scripts" / "assemble_release.py"
CORE_FILES = {"README.md": "core\n", "covey/__init__.py": "", "bin/covey_maso_runtime.py": "# forwarder\n",
              "bin/covey_bridge.py": "# bridge\n", "docs/a.md": "a\n"}
EXT_FILES = {"covey_internal/manifest.py": "HARNESSES = {}\n", "covey_internal/__init__.py": "",
             "covey_internal/bin/covey_maso_runtime.py": "# the real one\n",
             "covey_internal/standalone/core.lock": "x\n", "covey_internal/tests/test_x.py": "# t\n",
             "README.md": "ext\n", "core.lock": "# comment\n{core}\n", "dev/test.sh": "#!/bin/sh\n"}
SECOND_FILES = {"covey_second/manifest.py": "SETTINGS = {}\n", "covey_second/bin/covey_second_tool.py": "# tool\n"}


def tree(root):
    root = Path(root)
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


class AssembleReleaseTests(unittest.TestCase):
    """scripts/assemble_release.py on small fake repositories: the release has the shape of the old single repository."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.core = self.root / "core"
        self.core_sha = make_repo(self.core, CORE_FILES)
        self.ext = self.root / "ext"
        self.ext_sha = make_repo(self.ext, {**EXT_FILES, "core.lock": f"# c\n{self.core_sha[:8]}\n"})
        self.second = self.root / "second"
        self.second_sha = make_repo(self.second, SECOND_FILES)

    def assemble(self, out, *args):
        child = subprocess.run([sys.executable, "-B", "-I", str(ASSEMBLE), "--core", str(self.core), "--out", str(out),
                                *args], capture_output=True, text=True, timeout=60)
        return child.returncode, child.stdout, child.stderr

    def test_without_extensions_the_release_is_the_cores_git_archive(self):
        code, out, err = self.assemble(self.root / "r0")
        self.assertEqual(code, 0, err)
        self.assertEqual(tree(self.root / "r0"), CORE_FILES)
        self.assertEqual(json.loads(out)["extensions"], [])

    def test_one_extension_gives_the_single_repository_shape(self):
        if not (REPO / "extensions" / "enabled").exists():
            self.skipTest("the single-repository shape needs extensions/enabled; the public core has none")
        # What the single repository would have: the core's files, extensions/<package>/ (no standalone/), the
        # package's bin/ over the core's forwarders, extensions/enabled (the repository's own text for covey_internal).
        single = {**CORE_FILES, "bin/covey_maso_runtime.py": "# the real one\n",
                  "extensions/enabled": (REPO / "extensions" / "enabled").read_text()}
        for name, text in EXT_FILES.items():
            if name.startswith("covey_internal/") and "/standalone/" not in name:
                single["extensions/" + name] = text
        code, out, err = self.assemble(self.root / "r1", "--ext", str(self.ext))
        self.assertEqual(code, 0, err)
        got = tree(self.root / "r1")
        manifest = got.pop("RELEASE_MANIFEST")
        self.assertEqual(got, single)
        self.assertEqual(manifest, f"core {self.core_sha}\nextension {self.ext_sha} covey_internal\n")
        self.assertEqual(json.loads(out)["extensions"][0]["packages"], ["covey_internal"])

    def test_two_extensions_are_enabled_in_order_and_a_core_carrying_one_loses_to_the_checkout(self):
        core2 = self.root / "core2"
        sha = make_repo(core2, {**CORE_FILES, "extensions/covey_internal/manifest.py": "OLD = 1\n",
                                "extensions/covey_internal/old_only.py": "", "extensions/enabled": "covey_internal\n"})
        out = self.root / "r2"
        child = subprocess.run([sys.executable, "-B", "-I", str(ASSEMBLE), "--core", str(core2), "--out", str(out),
                                "--ext", str(self.second), "--ext", str(self.ext), "--ext-ref", self.second_sha],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr)
        got = tree(out)
        self.assertNotIn("extensions/covey_internal/old_only.py", got)
        self.assertEqual(got["extensions/covey_internal/manifest.py"], "HARNESSES = {}\n")
        self.assertEqual([ln for ln in got["extensions/enabled"].splitlines() if not ln.startswith("#")],
                         ["covey_second", "covey_internal"])
        self.assertEqual(got["bin/covey_second_tool.py"], "# tool\n")
        self.assertEqual(got["RELEASE_MANIFEST"].splitlines()[0], f"core {sha}")

    def test_a_root_enabled_file_picks_the_packages_turned_on_by_default(self):
        both = self.root / "both"
        make_repo(both, {**EXT_FILES, "covey_second/manifest.py": "HARNESSES = {}\n", "enabled": "# only the first\ncovey_internal\n"})
        code, out, err = self.assemble(self.root / "r4", "--ext", str(both))
        self.assertEqual(code, 0, err)
        got = tree(self.root / "r4")
        self.assertIn("extensions/covey_second/manifest.py", got)
        self.assertEqual([ln for ln in got["extensions/enabled"].splitlines() if not ln.startswith("#")], ["covey_internal"])
        bad = self.root / "bad"
        make_repo(bad, {**EXT_FILES, "enabled": "covey_nope\n"})
        code, _, err = self.assemble(self.root / "r5", "--ext", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("covey_nope", err)

    def test_a_non_empty_target_or_a_checkout_without_a_package_is_refused(self):
        (self.root / "full").mkdir()
        (self.root / "full" / "x").write_text("x")
        self.assertEqual(self.assemble(self.root / "full", "--ext", str(self.ext))[0], 1)
        bare = self.root / "bare"
        make_repo(bare, {"README.md": "no package\n"})
        code, _, err = self.assemble(self.root / "r3", "--ext", str(bare))
        self.assertEqual(code, 1)
        self.assertIn("no extension package", err)


class DeployExtensionTests(DeployFixture):
    """deploy_bridge.sh --ext-repo: release id, commits in the result, core.lock and environment pre-checks
    (--check-only on the fake state directory of DeployScriptTests; the core is this checkout)."""

    def setUp(self):
        super().setUp()
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        self.ext = self.root / "ext"
        self.ext_sha = make_repo(self.ext, {**EXT_FILES, "core.lock": f"# c\n{self.head[:8]}\n"})
        self.base = ["--check-only", "--workdir", str(self.workdir), "--profile", "bot", "--"]
        self.tail = ["--sandbox", "danger-full-access"]

    def check(self, *extra, env=None):
        return self.deploy(*self.base[:-1], *extra, "--", *self.tail, env=env)

    def test_without_an_extension_the_result_has_no_extension_fields(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn("release_id", out)
        self.assertNotIn("ext_commits", out)
        self.assertNotIn("core_lock_mismatch", out["pre"])
        self.assertEqual(out["release"], str(self.data / "releases" / self.head))

    def test_an_extension_names_the_release_after_both_commits_and_keeps_the_core_commit(self):
        code, out = self.check("--ext-repo", str(self.ext))
        self.assertEqual(code, 0, out)
        self.assertEqual((out["commit"], out["ext_commits"], out["release_id"]),
                         (self.head, [self.ext_sha], f"{self.head[:8]}-{self.ext_sha[:8]}"))
        self.assertEqual(out["release"], str(self.data / "releases" / out["release_id"]))
        self.assertFalse((self.data / "releases").exists())
        second = self.root / "ext2"
        sha2 = make_repo(second, {**EXT_FILES, "core.lock": f"{self.head[:8]}\n"})
        code, out = self.check("--ext-repo", str(self.ext), "--ext-commit", "HEAD", "--ext-repo", str(second))
        self.assertEqual(out["release_id"], f"{self.head[:8]}-{self.ext_sha[:8]}-{sha2[:8]}")

    def test_a_core_lock_naming_another_commit_blocks_unless_allowed(self):
        other = self.root / "other"
        make_repo(other, {**EXT_FILES, "core.lock": "deadbeef\n"})
        code, out = self.check("--ext-repo", str(other))
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertEqual(len(out["pre"]["core_lock_mismatch"]), 1)
        self.assertIn("core_lock_mismatch", out["error"])
        code, out = self.check("--ext-repo", str(other), "--allow-core-mismatch")
        self.assertEqual((code, out["ok"]), (0, True), out)
        self.assertTrue(out["pre"]["core_lock_mismatch"])

    def test_the_extension_variables_in_the_manifest_or_the_shell_block(self):
        for what, kept, shell in (("manifest", {"LARK_BRIDGE_EXTENSION_PATH": "/x"}, {}),
                                  ("shell", {}, {"COVEY_EXTENSIONS": ""})):
            with self.subTest(what):
                env = {**self.env, **shell}
                self.manifest({**runtime_environment(env), **kept}, self.args)
                code, out = self.check("--ext-repo", str(self.ext), env=env)
                self.assertEqual((code, out["ok"]), (1, False), out)
                self.assertIn("extension_env", out["error"])
                self.assertIn("EXTENSION", " ".join(out["pre"]["extension_env"]))

    def test_a_missing_checkout_or_commit_or_an_orphan_ext_commit_is_a_usage_error(self):
        code, out = self.check("--ext-repo", str(self.root / "nowhere"))
        self.assertEqual(code, 2)
        self.assertIn("not a git checkout", out["error"])
        code, out = self.check("--ext-repo", str(self.ext), "--ext-commit", "nosuchrev")
        self.assertEqual(code, 2)
        self.assertIn("not a commit", out["error"])
        code, out = self.check("--ext-commit", "HEAD")
        self.assertEqual(code, 2)


PRECOMPILE = REPO / "scripts" / "precompile_release.py"


def fake_release(root, files=None):
    """A release-shaped directory (code only) with a test module and a dev script that must stay uncompiled."""
    for name, text in (files or {"covey/__init__.py": "", "covey/app/x.py": "X = 1\n", "covey/tests/t.py": "T = 1\n",
                                 "extensions/pkg/manifest.py": "M = 1\n", "extensions/pkg/tests/t.py": "T = 1\n",
                                 "harness_bridge.py": "H = 1\n", "bin/covey_bridge.py": "# entry\n"}).items():
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return Path(root)


def pyc_names(root):
    return sorted(str(p.relative_to(root)).replace(sys.implementation.cache_tag, "TAG")
                  for p in Path(root).rglob("*.pyc"))


class PrecompileReleaseTests(unittest.TestCase):
    """scripts/precompile_release.py, assemble_release.py --precompile and deploy_bridge.sh writing a release's .pyc
    files (the service runs with -B and never would)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_script(self, release):
        child = subprocess.run([sys.executable, "-B", "-I", str(PRECOMPILE), str(release)], capture_output=True,
                               text=True, timeout=60)
        return child.returncode, child.stderr, json.loads(child.stdout.strip().splitlines()[-1]) if child.stdout else None

    def test_code_is_compiled_tests_and_entry_scripts_are_not_and_a_second_run_leaves_the_files_alone(self):
        release = fake_release(self.root / "r")
        code, err, info = self.run_script(release)
        self.assertEqual((code, err, info["ok"], info["failed"]), (0, "", True, []))
        self.assertEqual(pyc_names(release), ["__pycache__/harness_bridge.TAG.pyc", "covey/__pycache__/__init__.TAG.pyc",
                                              "covey/app/__pycache__/x.TAG.pyc",
                                              "extensions/pkg/__pycache__/manifest.TAG.pyc"])
        before = {p: p.stat().st_mtime_ns for p in release.rglob("*.pyc")}
        self.assertEqual(self.run_script(release)[0], 0)
        self.assertEqual({p: p.stat().st_mtime_ns for p in release.rglob("*.pyc")}, before)

    def test_a_file_that_does_not_compile_is_a_warning_and_the_rest_is_still_compiled(self):
        release = fake_release(self.root / "r")
        (release / "covey" / "app" / "broken.py").write_text("def (:\n")
        code, err, info = self.run_script(release)
        self.assertEqual((code, info["ok"], info["failed"]), (0, False, ["covey"]))
        self.assertIn("warning", err)
        self.assertIn("covey/app/__pycache__/x.TAG.pyc", pyc_names(release))
        self.assertEqual(subprocess.run([sys.executable, "-B", "-I", str(PRECOMPILE), str(self.root / "nowhere")],
                                        capture_output=True, text=True, timeout=60).returncode, 2)

    def test_assemble_writes_pyc_only_when_asked_and_a_wrong_interpreter_only_warns(self):
        core = self.root / "core"
        make_repo(core, {"covey/__init__.py": "", "covey/a.py": "A = 1\n", "bin/covey_bridge.py": "# b\n"})
        run = lambda out, *a: subprocess.run([sys.executable, "-B", "-I", str(ASSEMBLE), "--core", str(core), "--out",
                                              str(self.root / out), *a], capture_output=True, text=True, timeout=60)
        plain = run("plain")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual((pyc_names(self.root / "plain"), "precompile" in json.loads(plain.stdout)), ([], False))
        done = run("with", "--precompile", sys.executable)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(pyc_names(self.root / "with"), ["covey/__pycache__/__init__.TAG.pyc", "covey/__pycache__/a.TAG.pyc"])
        self.assertTrue(json.loads(done.stdout)["precompile"]["ok"])
        bad = run("bad", "--precompile", str(self.root / "no-python"))
        self.assertEqual(bad.returncode, 0, bad.stderr)
        self.assertIn("warning", bad.stderr)
        self.assertFalse(json.loads(bad.stdout)["precompile"]["ok"])
        self.assertTrue((self.root / "bad" / "covey" / "a.py").exists())


class DeployPrecompileTests(DeployFixture):
    """deploy_bridge.sh compiles the release it bootstraps from, also one that already exists, and a compile failure
    does not stop it (the stub bootstrap fails on purpose, so nothing is registered)."""

    def deploy_into_existing_release(self, files=None):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        # The stub bootstrap is executed, and the test temp dir (/dev/shm under test_map.py) is noexec: link one that is not.
        spare = exec_tempdir()
        self.addCleanup(spare.cleanup)
        release = fake_release(Path(spare.name) / "release", files)
        (self.data / "releases").mkdir(parents=True)
        (self.data / "releases" / head).symlink_to(release)
        stub = release / "scripts" / "bootstrap_harness_bridge.sh"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("#!/bin/bash\nexit 1\n")
        stub.chmod(0o755)
        code, out = self.deploy("--workdir", str(self.workdir), "--profile", "bot", "--", "--sandbox", "danger-full-access")
        self.assertEqual((code, out["error"]), (1, "bootstrap failed, see bootstrap.log"), out)
        return release, out

    def test_an_existing_release_is_compiled_before_bootstrap(self):
        release, out = self.deploy_into_existing_release()
        self.assertTrue(out["precompile"]["ok"], out)
        self.assertEqual(out["precompile"]["failed"], [])
        self.assertIn("covey/app/__pycache__/x.TAG.pyc", pyc_names(release))
        self.assertTrue((Path(out["run_dir"]) / "precompile.log").is_file())

    def test_a_compile_failure_is_reported_and_the_deploy_goes_on_to_bootstrap(self):
        _, out = self.deploy_into_existing_release({"covey/__init__.py": "", "covey/bad.py": "def (:\n",
                                                    "bin/covey_bridge.py": "# entry\n"})
        self.assertFalse(out["precompile"]["ok"])
        self.assertEqual(out["bootstrap_exit"], 1)


class CardkitCheckTests(unittest.TestCase):
    CARD = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "t"}},
            "body": {"elements": [{"tag": "markdown", "content": "x"}]}}

    def check(self, *argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = cardkit_check.main(list(argv))
        lines = out.getvalue().splitlines()
        return code, [json.loads(line) for line in lines[:-1]], (lines[-1] if lines else err.getvalue())

    def test_a_dry_run_measures_samples_and_files_and_calls_nothing(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(cardkit_check, "create", side_effect=AssertionError("called")):
            one, many, out = Path(root) / "one.json", Path(root) / "many.json", Path(root) / "out"
            one.write_text(json.dumps(self.CARD))
            many.write_text(json.dumps({"a": self.CARD, "b": {**self.CARD, "body": {"elements": [
                {"tag": "markdown", "content": "y"}] * 201}}}))
            code, rows, summary = self.check("--dry-run", "--out", str(out), "task_plain", str(one), str(many))
            self.assertEqual(code, 1)
            self.assertEqual([row["card"] for row in rows], ["task_plain", "one", "a", "b"])
            self.assertEqual([row["ok"] for row in rows], [True, True, True, False])
            self.assertEqual(rows[1], {"card": "one", "bytes": len(json.dumps(self.CARD).encode()), "nodes": 2,
                                       "within_limits": True, "ok": True})
            self.assertEqual(summary, "3/4 within limits")
            self.assertEqual(json.loads((out / "one.json").read_text()), self.CARD)
            self.assertEqual(json.loads((out / "results.json").read_text()), rows)
            with redirect_stdout(io.StringIO()) as listed:
                self.assertEqual(cardkit_check.main(["--list"]), 0)
        self.assertIn("task_plain", listed.getvalue().splitlines())
        self.assertIn("proxy_panel", listed.getvalue().splitlines())

    def test_cards_are_only_created_through_cardkit_and_a_refusal_is_reported(self):
        calls = []

        def api(profile, method, path, data, *, log):
            calls.append((profile, method, path, json.loads(data["data"])))
            return {"code": 0, "data": {"card_id": "c1"}} if len(calls) == 1 else {"code": 200830, "msg": "bad card"}

        with tempfile.TemporaryDirectory() as root, \
                mock.patch("covey.feishu.cardkit.cardkit_api", side_effect=api):
            path = Path(root) / "cards.json"
            path.write_text(json.dumps({"good": self.CARD, "bad": {**self.CARD, "schema": "9"}}))
            code, rows, summary = self.check("--profile", "bot", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(calls, [("bot", "POST", "/open-apis/cardkit/v1/cards", self.CARD),
                                 ("bot", "POST", "/open-apis/cardkit/v1/cards", {**self.CARD, "schema": "9"})])
        self.assertEqual((rows[0]["ok"], rows[0]["card_id"]), (True, "c1"))
        self.assertEqual(rows[1]["ok"], False)
        self.assertIn("bad card", rows[1]["error"])
        self.assertEqual(summary, "1/2 created")

    def test_an_unknown_card_or_a_missing_profile_stops_before_anything(self):
        with mock.patch.object(cardkit_check, "create", side_effect=AssertionError("called")):
            code, _, err = self.check("--profile", "bot", "no_such_card")
            self.assertEqual(code, 2)
            self.assertIn("unknown card 'no_such_card'", err)
            for argv in (("task_plain",), ()):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
                    cardkit_check.main(list(argv))
                self.assertEqual(stop.exception.code, 2)


class GoldenTraceTests(unittest.TestCase):
    FAKE = """\
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from covey.core.history_storage import append_history
from covey.core.state import write_json
from covey.feishu.messenger import Messenger, Voice


class Fake(unittest.TestCase):
    def test_a_dirs(self):
        for _ in range(DIRS):
            with tempfile.TemporaryDirectory():
                pass

    def test_send(self):
        with tempfile.TemporaryDirectory() as root, \\
                mock.patch("covey.feishu.messenger.send_message_with_fallback", return_value=True):
            write_json(Path(root) / "runs" / "k" / "x.json", {"a": 1})
            append_history(Path(root) / "topics" / "t1" / "history.jsonl", "assistant", TEXT)
            Messenger(print).send_message("p", "oc_fake", TEXT, "key-1", voice=Voice("t", "T"))
"""

    def main(self, *argv):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = golden_trace.main(list(argv))
        return code, out.getvalue()

    @unittest.skipIf(sys.version_info < (3, 11), "Path.replace on 3.10 calls the os.replace saved at import, which the trace hook cannot see")
    def test_two_recordings_agree_and_compare_reports_a_changed_message_text(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for version, text, dirs in (("v1", "hello", 1), ("v2", "hello!", 2)):
            (root / version).mkdir()
            (root / version / "test_golden_fake.py").write_text(f"TEXT = {text!r}\nDIRS = {dirs}\n" + self.FAKE)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        with mock.patch.object(golden_trace, "FORBIDDEN", ()):  # TMPDIR may be under /tmp here.
            for out, version in (("a", "v1"), ("b", "v1"), ("c", "v2")):
                os.chdir(root if out == "b" else here)  # A relative path is the caller's, not the repo's.
                module = f"{version}/test_golden_fake.py" if out == "b" else str(root / version / "test_golden_fake.py")
                code, _ = self.main("record", str(root / out), "--jobs", "1", "--scratch", str(root / "scratch"), module)
                self.assertEqual(code, 0, (root / out / "test_golden_fake.log").read_text())
        recorded = (root / "a" / "test_golden_fake.json").read_text()
        self.assertEqual(recorded, (root / "b" / "test_golden_fake.json").read_text())
        test = "test_golden_fake.Fake.test_send"
        events = json.loads(recorded)["events"][test]
        self.assertEqual([event[1] for event in events if event[1] in ("write", "feishu", "tree", "written")],
                         ["write", "feishu", "tree", "written"])
        folder = f"<S>/tmp{hashlib.sha256(test.encode()).hexdigest()[:6]}w1"
        self.assertIn(["MainThread", "write", f"{folder}/runs/k/x.json", "0o600", {"a": 1}], events)
        written = [event for event in events if event[1] == "written"]  # An append, not an os.replace.
        self.assertEqual(written[0][2:4], [f"{folder}/topics/t1/history.jsonl", "0o600"])
        self.assertEqual(written[0][4]["content"], "hello")
        self.assertEqual(self.main("compare", str(root / "a"), str(root / "b")),
                         (0, f"modules 1 tests 2 events {sum(map(len, json.loads(recorded)['events'].values()))}\n"
                             "real differences: 0\n"))
        changed = json.loads((root / "c" / "test_golden_fake.json").read_text())["events"]
        self.assertEqual(json.loads(json.dumps(changed[test]).replace("hello!", "hello")), events,
                         "one more temp dir in an earlier test leaves this test's paths as they were")
        code, out = self.main("compare", str(root / "a"), str(root / "c"))
        self.assertEqual(code, 1)
        self.assertIn(f"CONTENT {test}", out)
        self.assertIn('"text": "hello!"', out)
        self.assertIn('"content": "hello!"', out)
        self.assertEqual(self.main("compare", str(root / "a"), str(root / "c"), "--noise", str(root / "c"))[0], 0)
        self.assertFalse((root / "scratch").exists() and any((root / "scratch").glob("*/*")))

    def recordings(self, root, **runs):
        """Hand-made recordings: run name -> {test: events}, one module m."""
        for name, tests in runs.items():
            (root / name).mkdir()
            (root / name / "m.json").write_text(json.dumps({"outcome": {test: "ok" for test in tests}, "events": tests}))

    def test_compare_counts_a_reordering_within_a_thread_and_another_thread_but_not_an_interleaving(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        intent, send = ["write", "jobs/7.json", {"state": "submitting"}], ["feishu", "send_message", {"text": "hi"}]
        log, other = ["log", "queued"], ["log", "lane"]
        base = {"own": [["MainThread", *intent], ["MainThread", *send]],
                "moved": [["MainThread", *intent], ["MainThread", *send]],
                "two": [["MainThread", *intent], ["task-monitor", *log], ["MainThread", *send]],
                "pool": [["card-lane-N", *log], ["card-lane-N@2", *other]],
                "noisy": [["MainThread", *intent], ["task-monitor", *log]]}
        self.recordings(root, base=base, after={
            "own": [["MainThread", *send], ["MainThread", *intent]],  # The intent stored after the send.
            "moved": [["MainThread", *intent], ["task-monitor", *send]],
            "two": [["task-monitor", *log], ["MainThread", *intent], ["MainThread", *send]],
            "pool": [["card-lane-N", *other], ["card-lane-N@2", *log]],  # The other lane took it.
            "noisy": [["task-monitor", *log], ["MainThread", *intent], ["MainThread", *send]]},
            noise={**base, "noisy": [["task-monitor", *log], ["MainThread", *intent]]})
        code, out = self.main("compare", str(root / "base"), str(root / "after"), "--noise", str(root / "noise"))
        self.assertEqual(code, 1)
        kinds = {line.split()[1]: line.split()[0] for line in out.splitlines() if line.split()[1:2] and "outcome" in line}
        self.assertEqual(kinds, {"own": "SEQUENCE", "moved": "THREADS", "two": "ORDER", "pool": "ORDER",
                                 "noisy": "CONTENT"})  # An interleaving in the noise run excuses no new event.
        self.assertIn("real differences: 3", out)

    def test_record_refuses_the_repo_and_home_and_a_file_named_like_a_tests_module(self):
        for place in (REPO / "golden-out", Path.home() / "golden-out"):
            with self.subTest(place=place):
                self.assertEqual(self.main("record", str(place), "test_text")[0], 2)
                self.assertFalse(place.exists())
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "test_text.py").write_text("")
        with mock.patch.object(golden_trace, "FORBIDDEN", ()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(golden_trace.main(["record", str(root / "out"), str(root / "test_text.py")]), 2)
        self.assertIn("has the name of", err.getvalue())

    @unittest.skipUnless(threading.current_thread() is threading.main_thread(), "signal handlers need the main thread")
    def test_a_terminated_record_kills_the_module_it_was_waiting_for(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pid_file = root / "pid"
        (root / "test_golden_slow.py").write_text(
            "import os, time, unittest\nfrom pathlib import Path\n\n\nclass Slow(unittest.TestCase):\n"
            f"    def test_sleep(self):\n        Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "        time.sleep(30)\n")
        handled = []

        def terminate():  # SIGTERM only when record handles it: it would end this test process otherwise.
            deadline = time.time() + 60
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.1)
            handled.append(signal.getsignal(signal.SIGTERM) is golden_trace.interrupted)
            os.kill(os.getpid(), signal.SIGTERM if handled[0] else signal.SIGINT)

        killer = threading.Thread(target=terminate, daemon=True)
        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(golden_trace, "FORBIDDEN", ()), self.assertRaises(KeyboardInterrupt):
            killer.start()
            self.main("record", str(root / "out"), "--jobs", "1", "--scratch", str(root / "scratch"),
                      str(root / "test_golden_slow.py"))
        killer.join()
        self.assertEqual(handled, [True])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse(golden_trace.alive(int(pid_file.read_text())))
        self.assertFalse(any((root / "scratch").glob("*/*")))


class CrossReleaseTests(unittest.TestCase):
    """scripts/cross_release.py: where it may write, and what compare calls equal (no process is started)."""

    def test_the_output_is_refused_inside_the_checkout_home_tmp_or_a_foreign_directory(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"HOME": str(Path(root) / "home")}), \
                mock.patch.object(cross_release, "TMP", Path(root) / "tmp"):
            foreign = Path(root) / "foreign"
            foreign.mkdir()
            (foreign / "notes.txt").write_text("x")
            for out, why in ((REPO / "out", "inside this checkout"), (REPO, "inside this checkout"),
                             (Path(root) / "home" / "out", "inside $HOME"), (Path(root) / "tmp" / "cross", "inside /tmp"),
                             (foreign, "holds no earlier run")):
                self.assertIn(why, cross_release.refused_out(out))
            self.assertEqual(cross_release.refused_out(Path(root) / "new"), "")
            for other in ("{}", "[1]", "{", json.dumps({"rows": 3, "split": "train"})):  # Someone else's meta.json.
                (foreign / "meta.json").write_text(other)
                self.assertIn("holds no earlier run", cross_release.refused_out(foreign))
            for earlier in ({"tool": "cross_release"}, {"old_commit": "a" * 40, "new_head": "b" * 40, "trees": {}}):
                (foreign / "meta.json").write_text(json.dumps(earlier))
                self.assertEqual(cross_release.refused_out(foreign), "")  # An earlier run of its own: run again.
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cross_release.main(["run", "--old", "HEAD", "--out", str(REPO / "out")]), 2)
            self.assertIn("refused", err.getvalue())
            self.assertFalse((REPO / "out").exists())

    def test_compare_is_equal_only_when_every_cross_run_equals_its_same_release_runs(self):
        trees = {"new": "/absolute/new-tree", "old": "/absolute/old-tree"}

        def run(scenario, **facts):
            bridge, runner, _ = (trees[name] for name in cross_release.SCENARIOS[scenario])
            events = [{"what": "backend.cancelled"}, {"what": "feishu.send_message", "text": "已停止"}]
            runner_log = ["[2026-11-05T10:00:00] timing flow=runner journal=jobs:3", "[2026-11-05T10:00:01] job ended"]
            start = {"code": bridge, "log": ["task runner done job=#7", "timing flow=x"], "feishu": [],
                     "facts": {"runner_json": {"release": runner, "pid": 1}, "exit": {"status": "finished"},
                               "runner_events": events, "runner_log": runner_log, **facts}}
            return {"scenario": scenario, "variant": "plain", "ok": True, "start": start, "stamp": "s1"}

        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            (out / "runs").mkdir()
            (out / "meta.json").write_text(json.dumps({
                "tool": "cross_release", "stamp": "s1", "old_ref": "v1", "old_commit": "a" * 40, "new_head": "b" * 40,
                "new_dirty": False, "trees": trees, "scenarios": ["base-old", "base-new", "new-bridge-old-runner"],
                "variants": ["plain"]}))
            for scenario in ("base-old", "base-new"):
                (out / "runs" / f"{scenario}-plain.json").write_text(json.dumps(run(scenario)))
            # Two threads' events in the other order, a timing line's counts: not the protocol.
            (out / "runs" / "new-bridge-old-runner-plain.json").write_text(json.dumps(run(
                "new-bridge-old-runner", runner_events=[{"what": "feishu.send_message", "text": "已停止"},
                                                        {"what": "backend.cancelled"}],
                runner_log=["[2026-11-05T10:00:02] job ended", "[2026-11-05T10:00:03] timing flow=runner journal=jobs:4"])))
            args = cross_release.argparse.Namespace(dir=str(out), show=5)
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cross_release.compare(args), 0)
            self.assertEqual(printed.getvalue().splitlines()[-1], "all equal")
            self.assertIn("new-bridge-old-runner vs base-old [plain]: 0 differences", printed.getvalue())
            (out / "runs" / "new-bridge-old-runner-plain.json").write_text(
                json.dumps(run("new-bridge-old-runner", exit={"status": "interrupted"})))
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cross_release.compare(args), 1)
            self.assertIn("new-bridge-old-runner vs base-new [plain]: 1 differences", printed.getvalue())
            self.assertTrue(printed.getvalue().splitlines()[-1].startswith("NOT EQUAL"))
            # A run whose runner was not the release it names is never equal.
            wrong = run("new-bridge-old-runner")
            wrong["start"]["facts"]["runner_json"]["release"] = trees["new"]
            (out / "runs" / "new-bridge-old-runner-plain.json").write_text(json.dumps(wrong))
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cross_release.compare(args), 1)
            self.assertIn("not the release they name: ['new-bridge-old-runner plain']", printed.getvalue())

    def test_compare_is_not_equal_when_a_run_asked_for_is_missing_or_left_by_another_run(self):
        trees = {"new": "/absolute/new-tree", "old": "/absolute/old-tree"}

        def run(scenario, stamp="s2"):
            bridge, runner, _ = (trees[name] for name in cross_release.SCENARIOS[scenario])
            start = {"code": bridge, "log": [], "feishu": [], "facts": {"runner_json": {"release": runner}}}
            return {"scenario": scenario, "variant": "plain", "ok": True, "start": start, "stamp": stamp}

        def verdict(out):
            with redirect_stdout(io.StringIO()) as printed:
                code = cross_release.compare(cross_release.argparse.Namespace(dir=str(out), show=5))
            return code, printed.getvalue()

        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            (out / "runs").mkdir()
            meta = {"tool": "cross_release", "stamp": "s2", "old_ref": "v1", "old_commit": "a" * 40, "new_head": "b" * 40,
                    "new_dirty": False, "trees": trees, "scenarios": ["base-old", "base-new", "new-bridge-old-runner"],
                    "variants": ["plain"]}
            (out / "meta.json").write_text(json.dumps(meta))
            for scenario in ("base-old", "base-new", "new-bridge-old-runner"):
                (out / "runs" / f"{scenario}-plain.json").write_text(json.dumps(run(scenario)))
            self.assertEqual(verdict(out), (0, mock.ANY))
            # A baseline that never wrote its JSON: the pair is not compared, and that is not a pass.
            (out / "runs" / "base-old-plain.json").unlink()
            code, printed = verdict(out)
            self.assertEqual(code, 1)
            self.assertIn("runs missing: ['base-old plain']", printed)
            self.assertTrue(printed.splitlines()[-1].startswith("NOT EQUAL: 0 failed, 1 missing"))
            # A result an earlier run left (another stamp) is not this run's.
            (out / "runs" / "base-old-plain.json").write_text(json.dumps(run("base-old", stamp="s1")))
            code, printed = verdict(out)
            self.assertEqual(code, 1)
            self.assertIn("not this meta.json's stamp): ['base-old-plain.json']", printed)
            # Only one scenario asked for: nothing to compare it with, never "all equal".
            meta["scenarios"] = ["base-new"]
            (out / "meta.json").write_text(json.dumps(meta))
            shutil.rmtree(out / "runs")
            (out / "runs").mkdir()
            (out / "runs" / "base-new-plain.json").write_text(json.dumps(run("base-new")))
            code, printed = verdict(out)
            self.assertEqual(code, 1)
            self.assertIn("0 of 0 pairs", printed)
            # A meta.json of someone else's: no run here.
            (out / "meta.json").write_text("{}")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(verdict(out)[0], 2)

    def test_a_run_that_breaks_still_writes_its_result_as_failed(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            with mock.patch.object(cross_release.subprocess, "run", side_effect=OSError("no python")), \
                    mock.patch.object(cross_release, "kill_all"):
                line = cross_release.run_one(out, {"new": "/absolute/new", "old": "/absolute/old"}, "s3", "base-new", "plain")
            self.assertEqual(line, "base-new plain: FAILED")
            saved = json.loads((out / "runs" / "base-new-plain.json").read_text())
            self.assertEqual((saved["ok"], saved["stamp"], saved["error"]), (False, "s3", "OSError: no python"))


if __name__ == "__main__":
    unittest.main()

"""Workspace/storage boundaries and upgrade compatibility, without real services."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support.world import harness_app
from support import requires_extension
from covey.core.paths import component_dir, data_home, service_root


class StoragePathsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "tool checkout"
        self.repo.mkdir()
        patch = mock.patch.dict(os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_new_install_uses_sibling_storage_without_creating_it(self):
        for name in ("codex-lark-bridge", "claude-lark-bridge", "maso-lark-bridge"):
            target = component_dir(name, repo_root=self.repo)
            self.assertEqual(target, self.root / "tool checkout-data" / name)
            self.assertFalse(target.exists())
        self.assertEqual(list(self.repo.iterdir()), [])

    def test_data_home_is_shared_across_execution_directories(self):
        target = self.root / "persistent data"
        with mock.patch.dict(os.environ, {"COVEY_DATA_HOME": str(target)}):
            self.assertEqual(data_home(self.repo), target)
            self.assertEqual(component_dir("maso-lark-bridge", repo_root=self.repo), target / "maso-lark-bridge")
        self.assertFalse(target.exists())

    def test_upgrade_keeps_old_topic_and_session_even_with_new_data_home(self):
        old = self.repo / ".local-state" / "maso-lark-bridge"
        old.mkdir(parents=True)
        session = old / "maso_session_id"
        session.write_text("existing-session")
        with mock.patch.dict(os.environ, {"COVEY_DATA_HOME": str(self.root / "new data")}):
            self.assertEqual(component_dir("maso-lark-bridge", repo_root=self.repo), old)
            self.assertEqual(session.read_text(), "existing-session")
            self.assertEqual(component_dir("maso-lark-bridge", repo_root=self.repo, preserve_legacy=False),
                             self.root / "new data" / "maso-lark-bridge")
        self.assertFalse((self.root / "new data").exists())

    def test_a_resolved_legacy_path_logs_one_warning_line(self):
        import contextlib
        import io
        old = self.repo / ".local-state" / "maso-lark-bridge"
        old.mkdir(parents=True)
        for keep, expected in ((True, old), (False, self.root / "tool checkout-data" / "maso-lark-bridge")):
            with self.subTest(preserve_legacy=keep), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(component_dir("maso-lark-bridge", repo_root=self.repo, preserve_legacy=keep), expected)
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), int(keep))
            if keep:
                self.assertIn(f"legacy .local-state path resolved: {old}", lines[0])
                self.assertIn(str(self.root / "tool checkout-data"), lines[0])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            component_dir("claude-lark-bridge", repo_root=self.repo)  # Not under .local-state: nothing.
        self.assertEqual(err.getvalue(), "")

    def test_review_new_root_does_not_reuse_tool_repository(self):
        old = self.repo / ".local-state" / "example-component"
        old.mkdir(parents=True)
        self.assertEqual(component_dir("example-component", repo_root=self.repo, preserve_legacy=False),
                         self.root / "tool checkout-data" / "example-component")
        self.assertTrue(old.exists())

    def test_component_cannot_escape_data_home(self):
        for name in ("", ".", "..", "../escape", "/absolute"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                component_dir(name, repo_root=self.repo)

    def test_dangling_legacy_link_does_not_select_a_new_namespace(self):
        legacy = self.repo / ".local-state/codex-lark-bridge"
        legacy.parent.mkdir()
        legacy.symlink_to(self.root / "unavailable persistent store")
        self.assertEqual(component_dir("codex-lark-bridge", repo_root=self.repo),
                         self.root / "unavailable persistent store")
        self.assertFalse((self.root / "unavailable persistent store").exists())
        self.assertFalse(data_home(self.repo).exists())

    def test_service_root_is_the_managers_and_skips_legacy_state(self):
        (self.repo / ".local-state" / "bridge-services").mkdir(parents=True)
        self.assertEqual(service_root(self.repo), self.root / "tool checkout-data" / "bridge-services")
        with mock.patch.dict(os.environ, {"COVEY_SERVICE_ROOT": str(self.root / "services")}):
            self.assertEqual(service_root(self.repo), self.root / "services")
        script = Path(__file__).resolve().parents[1] / "bin" / "covey_paths.py"
        for env, expected in (({"COVEY_DATA_HOME": str(self.root / "data")}, self.root / "data" / "bridge-services"),
                              ({"COVEY_SERVICE_ROOT": str(self.root / "services")}, self.root / "services")):
            with self.subTest(env=env):
                shown = subprocess.run([sys.executable, "-B", str(script), "service-root"], env=env,
                                       capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(Path(shown), expected)
        self.assertFalse((self.root / "data").exists())

    def test_child_environment_tracks_workspace_without_changing_native_homes(self):
        from covey.core.process_env import child_process_env

        workspace = self.root / "selected workspace"
        workspace.mkdir()
        inherited = {"PWD": str(self.repo), "HOME": str(self.root / "user home"),
                     "CODEX_HOME": str(self.root / "codex home"),
                     "CLAUDE_CONFIG_DIR": str(self.root / "claude home"),
                     "TMPDIR": str(self.root / "configured NAS temporary directory"),
                     "COVEY_DATA_HOME": str(self.root / "service data")}
        with mock.patch.dict(os.environ, inherited):
            env = child_process_env(workspace)
            result = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import json,os; from pathlib import Path; Path('artifact.txt').write_text('ok'); "
                 "print(json.dumps([os.getcwd(), os.environ['PWD']]))"],
                cwd=workspace, env=env, capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(result.stdout), [str(workspace), str(workspace)])
            self.assertEqual(os.environ["PWD"], str(self.repo))
        for name in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
            self.assertEqual(env[name], inherited[name])
        self.assertNotIn("REVIEW_LOOP_STATE_DIR", env)
        self.assertEqual((workspace / "artifact.txt").read_text(), "ok")
        self.assertFalse((self.repo / "artifact.txt").exists())

    def test_native_session_readers_honor_harness_configured_homes(self):
        from covey.backends.codex.sessions import rollout_root
        from covey.backends.claude.sessions import claude_projects_root

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex config"),
                                          "CLAUDE_CONFIG_DIR": str(self.root / "claude config")}):
            self.assertEqual(rollout_root(), self.root / "codex config/sessions")
            self.assertEqual(claude_projects_root(), self.root / "claude config/projects")
        with mock.patch("pathlib.Path.home", return_value=self.root / "home"):
            self.assertEqual(rollout_root(), self.root / "home/.codex/sessions")
            self.assertEqual(claude_projects_root(), self.root / "home/.claude/projects")

    @requires_extension()
    def test_maso_context_uses_explicit_state_for_session_store(self):
        from covey_internal.maso.bridge import MASO_SPEC, MasoBackend
        from covey_internal.maso.sessions import MasoSessionStore

        store = MasoSessionStore()
        app = harness_app(MASO_SPEC, MasoBackend(), store)
        state = self.root / "personal maso state"
        args = app.harness.build_parser().parse_args(["--workdir", str(self.repo), "--state-dir", str(state)])
        ctx = app.harness.build_context(args)
        self.assertEqual(ctx.sessions_dir, state / "maso-sessions")
        self.assertEqual(store.sessions_dir(self.root / "another workspace"), state / "maso-sessions")

    @requires_extension()
    def test_only_the_default_harness_reads_its_state_dir_variable(self):
        # The others always get the Bridge's --state-dir (app/unified.py), so their variable is not read.
        from covey.backends.codex.bridge import CODEX_SPEC
        from covey.backends.claude.bridge import CLAUDE_SPEC
        from covey_internal.maso.bridge import MASO_SPEC

        for spec in (CODEX_SPEC, CLAUDE_SPEC, MASO_SPEC):
            with self.subTest(backend=spec.name):
                app = harness_app(spec, object(), object())
                state = self.root / (spec.name + " explicit")
                with mock.patch.dict(os.environ, {spec.env("STATE_DIR"): str(state)}):
                    self.assertEqual(app.harness.default_state_dir() == state, spec is CODEX_SPEC)

    def test_fresh_resumed_and_non_chat_jobs_receive_current_storage_contract(self):
        from support import make_context
        from support.fake_backends import FakeBackend
        from support.world import build_claude_app
        from covey.core.jobs import BridgeReplyJob

        for index, (kind, sid) in enumerate((("chat", None), ("chat", "11111111-1111-4111-8111-111111111111"),
                                           ("review", None))):
            with self.subTest(kind=kind, resumed=bool(sid)):
                backend = FakeBackend()
                app = build_claude_app(backend)
                ctx = make_context(self.root / str(index))
                ctx.workdir = self.repo
                job = BridgeReplyJob(job_id=index, user_text="continue", chat_id="chat", message_id=str(index),
                                     timeout=0, kind=kind, history_snapshot=[])
                app.runs.generate_reply_for_worker(ctx, job, sid, None)
                prompt = backend.prompts[-1]
                self.assertIn(f"Current workdir: {self.repo.resolve()}", prompt)
                self.assertIn("AGENTS.md / CLAUDE.md", prompt)
                self.assertIn("do not determine the project output root", prompt)
                self.assertIn("checkpoint path is for that checkpoint only", prompt)


if __name__ == "__main__":
    unittest.main()

"""One command changes one setting of a service (bin/covey_service.py set / unset, docs/configuration.md 改开关三步)."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.service import manager
from covey.service.manager import change_settings, main, parse_settings, read_json, register, service_key, set_desired

PROXY = "http://proxy.example.invalid:3128"


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.services = self.root / "services"
        self.environment = {"PATH": "/absolute/bin:/usr/bin", "HTTPS_PROXY": PROXY, "LARK_BRIDGE_DATA_HOME": "/absolute/data",
                            "LARK_BRIDGE_DIGEST_TIMES": "09:30", "LARK_BRIDGE_MAIN_TRACKS": "1"}
        self.command = [sys.executable, "-B", "/absolute/release/bin/covey_bridge.py", "--harness", "unified"]
        self.first = register(self.services, self.state, self.command, workdir=self.root, profile="bot",
                              environment=dict(self.environment), health_timeout=90)
        started = mock.patch.object(manager, "ensure_manager")
        self.started = started.start()
        self.addCleanup(started.stop)
        # The Manager running now has none of these in its own environment.
        inherited = mock.patch.object(manager, "manager_environment", return_value={"PATH": "/usr/bin"})
        self.inherited = inherited.start()
        self.addCleanup(inherited.stop)

    def manifest(self):
        return read_json(self.services / "services" / (service_key(self.state) + ".json"))

    def change(self, words, unset=False, **kwargs):
        return change_settings(self.services, self.state, parse_settings(words, unset=unset), **kwargs)

    def test_one_setting_changes_and_everything_else_stays(self):
        report = self.change(["COVEY_DIGEST_TIMES=09:30,21:30", "COVEY_DOWN_NOTICE=0"])
        after = self.manifest()
        self.assertEqual(report["changed"], {"LARK_BRIDGE_DIGEST_TIMES": {"from": "09:30", "to": "09:30,21:30"},
                                             "LARK_BRIDGE_DOWN_NOTICE": {"from": None, "to": "0"}})
        self.assertEqual(report["generation"], self.first["generation"] + 1)
        self.assertEqual(after["environment"], {**self.environment, "LARK_BRIDGE_DIGEST_TIMES": "09:30,21:30",
                                                "LARK_BRIDGE_DOWN_NOTICE": "0"})
        for field in ("command", "workdir", "profile", "log", "health_timeout", "state_dir", "desired_state"):
            self.assertEqual(after[field], self.first[field], field)
        self.assertEqual(after["generation"], self.first["generation"] + 1)
        self.started.assert_called_once_with(self.services)
        # A renamed switch is kept under the name every release reads (core/env.py canonical).
        self.change(["COVEY_BUTLER_TRACKS=0"])
        self.assertEqual(self.manifest()["environment"]["LARK_BRIDGE_MAIN_TRACKS"], "0")
        self.assertNotIn("COVEY_BUTLER_TRACKS", self.manifest()["environment"])

    def test_a_dry_run_and_a_value_already_set_change_nothing(self):
        report = self.change(["COVEY_DIGEST_TIMES=21:30"], dry_run=True)
        self.assertEqual(report["changed"], {"LARK_BRIDGE_DIGEST_TIMES": {"from": "09:30", "to": "21:30"}})
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.manifest(), self.first)
        report = self.change(["LARK_BRIDGE_DIGEST_TIMES=09:30"])
        self.assertEqual(report["changed"], {})
        self.assertEqual(self.manifest(), self.first)
        self.started.assert_not_called()

    def test_unset_removes_it_unless_the_managers_own_environment_would_put_it_back(self):
        report = self.change(["COVEY_DIGEST_TIMES"], unset=True)
        self.assertEqual(report["changed"], {"LARK_BRIDGE_DIGEST_TIMES": {"from": "09:30", "to": None}})
        self.assertNotIn("LARK_BRIDGE_DIGEST_TIMES", self.manifest()["environment"])
        self.inherited.return_value = {"COVEY_BUTLER_TRACKS": "1"}
        with self.assertRaisesRegex(ValueError, "COVEY_BUTLER_TRACKS is also in the Manager's own environment.*"
                                                "set COVEY_BUTLER_TRACKS=0"):
            self.change(["COVEY_BUTLER_TRACKS"], unset=True)
        self.assertEqual(self.manifest()["environment"]["LARK_BRIDGE_MAIN_TRACKS"], "1")
        self.assertEqual(self.change(["COVEY_NOT_THERE"], unset=True)["changed"], {})

    def test_credentials_and_what_the_manifest_does_not_keep_are_refused(self):
        for word, why in (("COVEY_WEB_TOKEN=abc", "credential"), ("LARK_BRIDGE_API_KEY=abc", "credential"),
                          ("COVEY_MANAGED=1", "sets it itself"), ("COVEY_REQUIRED_MOUNT=/x", "sets it itself"),
                          ("COVEY_TASK_DIR=/x", "sets it itself"), ("EDITOR=vim", "only COVEY_")):
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, why):
                self.change([word])
        with self.assertRaisesRegex(ValueError, "only non-secret proxy URLs"):
            self.change(["HTTPS_PROXY=http://user:secret@proxy.example.invalid:3128"])  # privacy: allow a fake proxy password to be refused
        with self.assertRaisesRegex(ValueError, "only explicit non-secret"):
            self.change(["COVEY_DIGEST_TIMES=09:30\n21:30"])
        self.assertEqual(self.manifest(), self.first)

    def test_what_the_deploy_pre_check_stops_is_refused(self):
        with self.assertRaisesRegex(ValueError, "LARK_BRIDGE_CODEX_SANDBOX=read-only is no longer read"):
            self.change(["LARK_BRIDGE_CODEX_SANDBOX=read-only"])
        with mock.patch("covey.core.run_dir.restart_blockers", return_value=[{"key": "k", "job_id": 12, "status": "running"}]):
            with self.assertRaisesRegex(ValueError, r"job #12 \(running\) would be interrupted"):
                self.change(["COVEY_DIGEST_TIMES=21:30"])
        forms = self.state / "launch-forms"
        forms.mkdir()
        (forms / "abc.json").write_text(json.dumps({"status": "creating_worktree"}))
        with self.assertRaisesRegex(ValueError, "a launch form is creating its worktree"):
            self.change(["COVEY_DIGEST_TIMES=21:30"])
        (forms / "abc.json").unlink()
        held = os.open(self.services / "deploy.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # As scripts/deploy_bridge.sh holds it, and says who.
        os.ftruncate(held, 0)
        os.pwrite(held, json.dumps({"pid": 4242, "commit": "abcdef"}).encode(), 0)
        with self.assertRaisesRegex(ValueError, 'a deploy is running .*"pid": 4242'):
            self.change(["COVEY_DIGEST_TIMES=21:30"])
        fcntl.flock(held, fcntl.LOCK_UN)
        self.assertEqual(self.manifest(), self.first)
        self.assertEqual(self.change(["COVEY_DIGEST_TIMES=21:30"])["changed"]["LARK_BRIDGE_DIGEST_TIMES"]["to"], "21:30")

    def test_a_stopped_or_unregistered_service_is_refused(self):
        set_desired(self.services, self.state, "stopped")
        with self.assertRaisesRegex(ValueError, "the service is stopped: start it first"):
            self.change(["COVEY_DIGEST_TIMES=21:30"])
        with self.assertRaisesRegex(ValueError, "no service is registered"):
            change_settings(self.services, self.root / "other", {"COVEY_DIGEST_TIMES": "21:30"})

    def test_the_words_of_the_command(self):
        self.assertEqual(parse_settings(["COVEY_A=1", "COVEY_B="]), {"COVEY_A": "1", "COVEY_B": ""})
        self.assertEqual(parse_settings(["COVEY_A"], unset=True), {"COVEY_A": None})
        for words, unset in ((["COVEY_A"], False), (["COVEY_A=1"], True), (["=1"], False),
                             (["COVEY_DIGEST_TIMES=1", "LARK_BRIDGE_DIGEST_TIMES=2"], False)):
            with self.subTest(words=words), self.assertRaises(ValueError):
                parse_settings(words, unset=unset)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.assertEqual(main(["--root", str(self.services), "set", "--state-dir", str(self.state), "--dry-run",
                                   "COVEY_DIGEST_TIMES=21:30"]), 0)
        self.assertEqual(json.loads(said.getvalue())["changed"]["LARK_BRIDGE_DIGEST_TIMES"]["to"], "21:30")
        self.assertEqual(self.manifest(), self.first)


class ManagerEnvironmentTests(unittest.TestCase):
    def test_without_a_running_manager_it_is_this_processs_own(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"COVEY_PROBE_SETTING": "1"}):
                self.assertEqual(manager.manager_environment(Path(root)).get("COVEY_PROBE_SETTING"), "1")


if __name__ == "__main__":
    unittest.main()

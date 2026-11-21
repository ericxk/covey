"""Claude Code deletes session transcripts after ``cleanupPeriodDays`` (default 30): the doctor's 诊断 tab and ./install say so
when the user's settings keep few days, and change nothing (core/claude_retention.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_doctor_panel import NOW, report
from test_installer import InstallerCase
from covey.app.doctor_panel import summarize
from covey.cards.doctor_card import doctor_rows
from covey.core import claude_retention as retention
from covey.core.process import service_key
from covey.service import doctor as doctor_module
from covey.service import installer


class Config(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = {"CLAUDE_CONFIG_DIR": str(self.home)}

    def write(self, text):
        (self.home / "settings.json").write_text(text)


class CheckTest(Config):
    def test_a_missing_file_or_setting_is_the_default_and_a_low_one_is_said(self):
        found = retention.check(self.env)
        self.assertEqual((found["state"], found["days"]), (retention.MISSING, 30))
        self.write("{}")
        self.assertEqual(retention.check(self.env)["state"], retention.MISSING)
        self.write(json.dumps({"cleanupPeriodDays": 60}))
        found = retention.check(self.env)
        self.assertEqual((found["state"], found["days"]), (retention.LOW, 60))
        self.assertIn("最近 60 天", retention.hint(found))
        self.assertIn('"cleanupPeriodDays": 3650', retention.hint(found))
        self.assertIn(str(self.home / "settings.json"), retention.hint(found))

    def test_ninety_days_or_more_says_nothing(self):
        for days in (90, 3650):
            self.write(json.dumps({"cleanupPeriodDays": days}))
            found = retention.check(self.env)
            self.assertEqual(found["state"], retention.OK)
            self.assertEqual(retention.hint(found), "")

    def test_a_value_claude_code_would_not_take_counts_as_the_default(self):
        for value in ("90", 0, -5, True, 12.5, None):
            self.write(json.dumps({"cleanupPeriodDays": value}))
            self.assertEqual(retention.check(self.env)["state"], retention.MISSING, value)

    def test_a_file_that_is_not_json_is_unreadable_and_says_no_fix(self):
        self.write("{not json")
        found = retention.check(self.env)
        self.assertEqual((found["state"], found["days"]), (retention.UNREADABLE, None))
        self.assertEqual(retention.hint(found), "")

    def test_the_config_dir_else_home_decides_the_file_and_nothing_is_written(self):
        self.assertEqual(retention.settings_path({"CLAUDE_CONFIG_DIR": "/absolute/c"}), Path("/absolute/c/settings.json"))
        self.assertEqual(retention.settings_path({"HOME": "/absolute/h"}), Path("/absolute/h/.claude/settings.json"))
        retention.check(self.env)
        self.assertEqual(list(self.home.iterdir()), [])


class DoctorTest(Config):
    def harness_file(self, names):
        path = self.home / "harnesses.json"
        path.write_text(json.dumps({"enabled_harnesses": names} if names else {}))
        return path

    def manifest(self, names=None):
        return {"command": ["/absolute/python", "bridge", "--harness-config", str(self.harness_file(names))],
                "environment": dict(self.env)}

    def test_the_row_is_there_only_with_the_claude_harness(self):
        self.write(json.dumps({"cleanupPeriodDays": 30}))
        row = doctor_module.retention_row(self.manifest(["claude", "codex"]))
        self.assertEqual((row["state"], row["days"]), ("low", 30))
        self.assertIn("cleanupPeriodDays", row["hint"])
        self.assertIsNotNone(doctor_module.retention_row(self.manifest()))  # No list: all enabled.
        self.assertIsNone(doctor_module.retention_row(self.manifest(["codex"])))

    def test_the_tab_says_it_with_the_fix_and_stays_quiet_when_it_is_long_or_off(self):
        low = {"state": "missing", "days": 30, "path": "/absolute/settings.json", "hint": "要改成 3650"}
        rows = doctor_rows(summarize(report("k1", service={"claude_cleanup": low}), "k1", NOW))
        self.assertEqual(rows[3][:2], (None, "Claude Code 只保留最近 30 天的会话"))
        self.assertEqual(rows[3][2], "要改成 3650")
        ok = doctor_rows(summarize(report("k1", service={"claude_cleanup": {"state": "ok", "days": 3650}}), "k1", NOW))
        self.assertEqual(ok[3][:2], (True, "Claude Code 会话保留 3650 天"))
        unreadable = doctor_rows(summarize(report("k1", service={"claude_cleanup": {"state": "unreadable", "path": "/a/b"}}),
                                           "k1", NOW))
        self.assertEqual(unreadable[3][0], "info")
        self.assertNotIn("会话", " ".join(row[1] for row in doctor_rows(summarize(report("k1"), "k1", NOW))))

    def test_diagnose_adds_it_to_the_service_and_does_not_degrade_it(self):
        from covey.service.manager import process_identity, register, save
        self.write("{}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            config = self.harness_file(["claude"])
            register(root, state, [sys.executable, "-c", "pass", "--harness-config", str(config)], workdir=root,
                     environment=dict(self.env))
            identity = process_identity(os.getpid())
            manifest = json.loads(next((root / "services").glob("*.json")).read_text())
            save(root / "status" / (service_key(state) + ".json"), {"process": identity, "generation": manifest["generation"]})
            with mock.patch("covey.service.doctor.manager_health", return_value={"process": identity, "lag": 0}), \
                 mock.patch("covey.service.doctor.query", return_value={"process": identity, "status": "ready"}):
                found = doctor_module.diagnose(root, check_events=False)
        [service] = found["services"]
        self.assertEqual(service["claude_cleanup"]["state"], "missing")
        self.assertEqual(service["status"], "ready")


class InstallHintTest(InstallerCase):
    def run_install(self, settings, working=("claude",)):
        home = self.root / "claude-home"
        home.mkdir(exist_ok=True)
        if settings is not None:
            (home / "settings.json").write_text(json.dumps(settings))
        ins = self.make(env=self.env(CLAUDE_CONFIG_DIR=str(home)))
        ins.working = list(working)
        ins.retention_hint()
        return [line for line in self.lines if "cleanupPeriodDays" in line]

    def test_one_line_when_few_days_are_kept_and_nothing_written(self):
        said = self.run_install({"cleanupPeriodDays": 30})
        self.assertEqual(len(said), 1)
        self.assertIn("提示：", said[0])
        self.assertEqual(json.loads((self.root / "claude-home" / "settings.json").read_text()), {"cleanupPeriodDays": 30})

    def test_nothing_with_a_long_setting_or_without_claude(self):
        self.assertEqual(self.run_install({"cleanupPeriodDays": 3650}), [])
        self.assertEqual(self.run_install({}, working=("codex",)), [])
        self.assertEqual(installer.claude_retention.hint({"state": "unreadable"}), "")


if __name__ == "__main__":
    unittest.main()

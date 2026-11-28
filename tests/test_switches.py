"""The optional switches a service has set, said to whoever helps with it (core/switches.py): in the 复制诊断 block
(app/diag_summary.py) and on the 设置 card's 诊断 tab (service/doctor.py, app/doctor_panel.py, cards/doctor_card.py). Only
known names, never a credential-like name or value."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_doctor_panel import NOW, report
from test_structure import inventory_rows
from covey.app.diag_summary import summary
from covey.app.doctor_panel import summarize
from covey.cards.doctor_card import doctor_card, doctor_rows
from covey.core import switches

REPO = Path(__file__).resolve().parents[1]


class SwitchesTests(unittest.TestCase):
    def test_known_names_are_the_ones_the_switch_table_names_and_each_has_its_inventory_row(self):
        lines = (REPO / "docs" / "configuration.md").read_text().splitlines()
        table = lines[lines.index("## 开关速查"):lines.index("### 改开关三步")]
        named = list(dict.fromkeys(re.findall(r"`(COVEY_[A-Z0-9_]+)", "\n".join(table))))
        self.assertEqual(list(switches.KNOWN), named)
        rows = inventory_rows()
        self.assertEqual([name for name in switches.KNOWN if name not in rows], [])
        self.assertEqual([name for name in switches.KNOWN if switches.SECRET_NAME.search(name)], [])

    def test_each_switch_on_unset_is_on_where_its_feature_reads_it_and_off_at_0(self):
        """T813: the default-off switches the owner turned on are on unset, each read where its feature is; DEFAULTS
        says the same, and 0 turns each off."""
        from covey.app import dispatch_cards, prompts, workspace_gists
        from covey.app import projects as upgrade
        from covey.app.overview import Overview
        from covey.core import butler_tracks, knowledge, memory, project_proposals, projects, quiet_hours, revisits
        from covey.core import env as cenv, topic_seen, triage
        readers = {
            "COVEY_PROJECTS": projects.enabled, "COVEY_PROJECT_AUTOWAKE": projects.autowake_enabled,
            "COVEY_PROJECT_ASK": projects.ask_enabled, "COVEY_PROJECT_MEMORY": projects.memory_enabled,
            "COVEY_PROJECT_TRIAGE": triage.enabled, "COVEY_PROJECT_TIDY": project_proposals.mode,
            "COVEY_PROJECT_UPGRADE": upgrade.mode, "COVEY_REVISIT": revisits.enabled,
            "COVEY_MEMORY": memory.enabled, "COVEY_MEMORY_LEARN": memory.learning, "COVEY_KNOWLEDGE": knowledge.enabled,
            "COVEY_PINNED_OVERVIEW": Overview.enabled,
            "COVEY_PINNED_TOPICS": topic_seen.enabled, "COVEY_DIGEST_TIMES": quiet_hours.digest_times,
            "COVEY_DISPATCH_CARD": dispatch_cards.enabled, "COVEY_BUTLER_TRACKS": butler_tracks.enabled,
            "COVEY_BUTLER_RULES": prompts.butler_on, "COVEY_WORKSPACE_GISTS": workspace_gists.count}
        self.assertEqual(set(readers), set(switches.DEFAULTS))
        spelled = {spelling for name in readers for spelling in cenv.spellings(name)}
        clean = {key: value for key, value in os.environ.items() if key not in spelled}
        with mock.patch.dict(os.environ, clean, clear=True):
            for name, read in readers.items():
                with self.subTest(name=name):
                    held = read()
                    self.assertTrue(held)
                    if not isinstance(held, bool):
                        self.assertEqual(str(held), switches.DEFAULTS[name])
                    with mock.patch.dict(os.environ, {name: "0"}):
                        self.assertIn(read(), (False, 0, "", "off"))
            self.assertTrue(switches.is_on("COVEY_PROJECTS", {}))
            self.assertFalse(switches.is_on("COVEY_PROJECTS", {"COVEY_PROJECTS": "0"}))
            self.assertTrue(switches.is_on("COVEY_REVISIT", {"COVEY_REVISIT": switches.EMPTY}))  # Empty is unset.
            self.assertFalse(switches.is_on("COVEY_COLLAB", {}))  # Not one of DEFAULTS: off unset.

    def test_a_manifest_environment_in_its_stored_spellings_gives_names_and_values(self):
        found = switches.shown({"LARK_BRIDGE_MAIN_THREAD": "1", "COVEY_REVISIT": "1", "COVEY_DIGEST_TIMES": "09:30,21:30",
                                "COVEY_PROJECT_TIDY": "", "PATH": "/usr/bin", "COVEY_NOT_A_SWITCH": "1"})
        self.assertEqual(found, [("COVEY_BUTLER", "1"), ("COVEY_PROJECT_TIDY", switches.EMPTY), ("COVEY_REVISIT", "1"),
                                 ("COVEY_DIGEST_TIMES", "09:30,21:30")])
        self.assertEqual(switches.words(found[:2]), "COVEY_BUTLER=1；COVEY_PROJECT_TIDY=空")

    def test_install_and_showcase_name_known_switches_and_the_showcase_holds_the_install_set(self):
        self.assertEqual(switches.INSTALL, {"COVEY_EVENT_TRANSPORT": "sdk", "COVEY_CARD_TRANSPORT": "sdk"})
        self.assertEqual(switches.SHOWCASE, {**switches.INSTALL, "COVEY_COLLAB": "1"})
        self.assertEqual([name for name in switches.DEFAULTS if name not in switches.KNOWN], [])
        self.assertEqual({k: v for k, v in switches.SHOWCASE.items() if k in switches.INSTALL}, switches.INSTALL)
        self.assertEqual([name for name in switches.SHOWCASE if name not in switches.KNOWN], [])
        lines = (REPO / "docs" / "configuration.md").read_text().splitlines()
        showcase = "\n".join(lines[lines.index("### 演示套餐"):lines.index("### 改开关三步")])
        extra = sorted(set(switches.SHOWCASE) - set(switches.INSTALL))
        self.assertEqual([name for name in extra if f"`{name}=" not in showcase], [])  # Its table names each one.

    def test_a_preset_turns_on_what_is_unset_or_off_whatever_the_spelling_and_keeps_other_values(self):
        environ = {"LARK_BRIDGE_EVENT_TRANSPORT": "cli", "LARK_BRIDGE_CARD_TRANSPORT": "off", "COVEY_COLLAB": "No"}
        found = switches.preset_changes("showcase", environ)
        self.assertEqual(found, {"COVEY_CARD_TRANSPORT": "sdk", "COVEY_COLLAB": "1"})  # cli is a value of its own: kept.
        self.assertEqual(switches.preset_changes("showcase", {}), switches.SHOWCASE)
        self.assertEqual(switches.preset_changes("showcase", dict(switches.SHOWCASE)), {})
        with self.assertRaises(KeyError):
            switches.preset_changes("other", {})

    def test_a_path_an_address_a_long_or_secret_looking_value_is_not_shown(self):
        for value in ("/absolute/path/x.json", "https://example.com/hook", "a" * 40,
                      "sk-live-1234567890abcdef",  # privacy: allow a fake secret-shaped value that must stay hidden
                      "token=abc12345", "x y"):
            with self.subTest(value=value):
                self.assertEqual(switches.shown_value(value), switches.HIDDEN)
        self.assertEqual(switches.shown({"COVEY_WEB_CONFIG": "/absolute/web.json"}), [("COVEY_WEB_CONFIG", switches.HIDDEN)])

    def test_a_credential_like_name_is_never_read_even_if_it_were_listed(self):
        with mock.patch.object(switches, "KNOWN", ("COVEY_API_TOKEN", "COVEY_SERVICE_KEY", "COVEY_REVISIT")):
            found = switches.shown({"COVEY_API_TOKEN": "1", "COVEY_SERVICE_KEY": "1", "COVEY_REVISIT": "1"})
        self.assertEqual(found, [("COVEY_REVISIT", "1")])

    def test_the_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_API_TOKEN": "x"}):
            self.assertIn(("COVEY_MEMORY", "1"), switches.shown())
            self.assertNotIn("COVEY_API_TOKEN", dict(switches.shown()))


class WhereTheyAreSaidTests(unittest.TestCase):
    def test_the_diagnosis_block_says_the_bridges_switches(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"COVEY_REVISIT": "1", "COVEY_WEB_CONFIG": "/a/b"}):
            block = summary(Path(tmp), {"key": "k", "job_id": 7, "status": "interrupted"}, bridge_release="covey")
        self.assertIn("开关：", block)
        self.assertIn("COVEY_REVISIT=1", block)
        self.assertIn("COVEY_WEB_CONFIG=已设", block)
        self.assertNotIn("/a/b", block)

    def test_the_tab_row_lists_the_switches_the_manifest_has_and_says_so_when_there_is_none(self):
        data = summarize(report("k1", service={"switches": [["COVEY_REVISIT", "1"], ["COVEY_DIGEST_TIMES", "09:30,21:30"]]}),
                         "k1", NOW)
        rows = doctor_rows(data)
        self.assertEqual(rows[2], ("info", "设了 2 个开关", "COVEY_REVISIT=1｜COVEY_DIGEST_TIMES=09:30,21:30"))
        self.assertIn("COVEY_REVISIT=1", json.dumps(doctor_card("t" * 24, {"revision": 1, "hub": True}, data), ensure_ascii=False))
        self.assertEqual(doctor_rows(summarize(report("k1"), "k1", NOW))[2][:2], ("info", "没设可选开关"))
        self.assertNotIn("设了", " ".join(row[1] for row in doctor_rows(summarize(report("k1"), "other", NOW))))

    def test_the_doctor_reads_them_from_the_manifest(self):
        from covey.core.process import service_key
        from covey.service.doctor import diagnose
        from covey.service.manager import process_identity, register, save
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            register(root, state, [sys.executable, "-c", "pass"], workdir=root,
                     environment={"COVEY_REVISIT": "1", "COVEY_WEB_CONFIG": "/a/b"})
            identity = process_identity(os.getpid())
            save(root / "status" / (service_key(state) + ".json"), {"process": identity, "generation": 0})
            with mock.patch("covey.service.doctor.manager_health", return_value={"process": identity, "lag": 0}), \
                 mock.patch("covey.service.doctor.query", return_value={"process": identity, "status": "ready"}):
                found = diagnose(root, check_events=False)
        [service] = found["services"]
        self.assertEqual(service["switches"], [["COVEY_REVISIT", "1"], ["COVEY_WEB_CONFIG", "已设"]])


if __name__ == "__main__":
    unittest.main()

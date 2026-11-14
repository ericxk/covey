"""COVEY_<X> is the name of a setting, LARK_BRIDGE_<X> the old one that keeps working (covey/core/env.py)."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from covey.core import env as cenv
from covey.core.file_lock import runtime_locks_enabled
from covey.core.task_processes import OLD_TASK_DIR_ENV, TASK_DIR_ENV
from covey.service.manager import persistable, runtime_environment, service_environment


class NamesTest(unittest.TestCase):
    def test_either_spelling_is_read_and_the_new_one_wins(self):
        self.assertEqual(cenv.get("COVEY_X", None, {"LARK_BRIDGE_X": "old"}), "old")
        self.assertEqual(cenv.get("LARK_BRIDGE_X", None, {"COVEY_X": "new"}), "new")
        self.assertEqual(cenv.get("COVEY_X", None, {"LARK_BRIDGE_X": "old", "COVEY_X": "new"}), "new")
        self.assertEqual(cenv.get("COVEY_X", "d", {}), "d")
        self.assertEqual(cenv.get("HOME", None, {"HOME": "/h"}), "/h")  # Another variable is read as it is.
        self.assertTrue(cenv.has("COVEY_X", {"LARK_BRIDGE_X": ""}))  # Set to empty is set.

    def test_reading_only_the_old_name_of_the_process_environment_says_so_once(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_ONLY_OLD": "1"}), contextlib.redirect_stderr(err):
            cenv._warned.clear()
            self.assertEqual(cenv.get("COVEY_ONLY_OLD"), "1")
            self.assertEqual(cenv.get("COVEY_ONLY_OLD"), "1")
        self.assertEqual(err.getvalue().count("LARK_BRIDGE_ONLY_OLD is the old name of COVEY_ONLY_OLD"), 1, err.getvalue())

    def test_mirrored_and_canonical_forms(self):
        self.assertEqual(cenv.mirrored({"LARK_BRIDGE_A": "1", "COVEY_B": "2", "COVEY_C": "n", "LARK_BRIDGE_C": "o", "PATH": "/b"}),
                         {"LARK_BRIDGE_A": "1", "COVEY_A": "1", "COVEY_B": "2", "LARK_BRIDGE_B": "2",
                          "COVEY_C": "n", "LARK_BRIDGE_C": "n", "PATH": "/b"})
        self.assertEqual(cenv.canonical({"COVEY_A": "1", "LARK_BRIDGE_B": "2", "PATH": "/b"}),
                         {"LARK_BRIDGE_A": "1", "LARK_BRIDGE_B": "2", "PATH": "/b"})
        child = {}
        cenv.put(child, "COVEY_TASK_DIR", "/run")
        self.assertEqual(child, {"COVEY_TASK_DIR": "/run", "LARK_BRIDGE_TASK_DIR": "/run"})

    def test_manifests_keep_the_old_spelling_so_an_upgrade_or_rollback_changes_nothing(self):
        self.assertTrue(persistable("COVEY_PINNED_TOPICS") and persistable("LARK_BRIDGE_PINNED_TOPICS"))
        self.assertFalse(persistable("COVEY_API_TOKEN"))
        self.assertFalse(persistable("COVEY_MANAGED") or persistable("LARK_BRIDGE_MANAGED"))
        self.assertFalse(persistable(TASK_DIR_ENV) or persistable(OLD_TASK_DIR_ENV))
        self.assertEqual(runtime_environment({"COVEY_PINNED_TOPICS": "1", "LARK_BRIDGE_DIGEST_TIMES": "09:30", "PATH": "/b"}),
                         {"LARK_BRIDGE_PINNED_TOPICS": "1", "LARK_BRIDGE_DIGEST_TIMES": "09:30", "PATH": "/b"})
        # The same settings under either name register the same environment: no deploy comparison difference.
        self.assertEqual(runtime_environment({"COVEY_PINNED_TOPICS": "1"}), runtime_environment({"LARK_BRIDGE_PINNED_TOPICS": "1"}))

    def test_a_setting_under_the_old_name_still_switches_a_feature_on(self):
        with mock.patch.dict(os.environ, {"LARK_BRIDGE_LOCK_ROOT": "/x"}), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(runtime_locks_enabled())

    def test_a_service_gets_both_spellings_and_its_manifest_beats_the_managers_own_environment(self):
        env = service_environment({"COVEY_PINNED_TOPICS": "0", "PATH": "/b", "COVEY_ONLY_HERE": "h"},
                                  {"LARK_BRIDGE_PINNED_TOPICS": "1"})
        self.assertEqual((env["COVEY_PINNED_TOPICS"], env["LARK_BRIDGE_PINNED_TOPICS"]), ("1", "1"))
        self.assertEqual((env["COVEY_ONLY_HERE"], env["LARK_BRIDGE_ONLY_HERE"], env["PATH"]), ("h", "h", "/b"))
        self.assertEqual((env["COVEY_MANAGED"], env["LARK_BRIDGE_MANAGED"]), ("1", "1"))


if __name__ == "__main__":
    unittest.main()

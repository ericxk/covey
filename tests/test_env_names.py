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

    def test_a_setting_born_after_the_rename_has_no_old_name_to_warn_about(self):
        """The manifest stores COVEY_PATROL as LARK_BRIDGE_PATROL; reading it that way must not claim an old name."""
        err = io.StringIO()
        env = {"LARK_BRIDGE_PATROL": "shadow", "LARK_BRIDGE_PROJECTS": "1", "LARK_BRIDGE_TRACK_JUDGE": "1", "LARK_BRIDGE_DEVICES": "1"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            cenv._warned.clear()
            for name in ("COVEY_PATROL", "COVEY_PROJECTS", "COVEY_PROJECT_BUDGET", "COVEY_TRACK_JUDGE", "COVEY_DEVICES"):
                cenv.get(name)
            self.assertEqual(cenv.get("COVEY_PATROL"), "shadow")  # Still read.
        self.assertEqual(err.getvalue(), "")

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


# The names the main thread's variables had before it became the butler (2026-11-16): (new tail, old tail).
RENAMED = (("BUTLER", "MAIN_THREAD"), ("BUTLER_RULES", "MAIN_BUTLER"), ("BUTLER_TRACKS", "MAIN_TRACKS"),
           ("BUTLER_TRACKS_MAX", "MAIN_TRACKS_MAX"), ("DISPATCH_CARD", "MAIN_DISPATCH_CARD"), ("BUTLER_JOB", "MAIN_JOB"),
           ("BUTLER_STATE", "MAIN_STATE"))


class RenamedTest(unittest.TestCase):
    """Seven variables changed their name as well as their prefix: the new name wins, then COVEY_<old>, then
    LARK_BRIDGE_<old>; manifests keep LARK_BRIDGE_<old>, the name every older release reads."""

    def test_the_new_name_wins_then_the_old_name_under_either_prefix(self):
        for new, old in RENAMED:
            for asked in (f"COVEY_{new}", f"COVEY_{old}", f"LARK_BRIDGE_{old}"):  # Asked under any of its names.
                self.assertEqual(cenv.get(asked, None, {f"COVEY_{new}": "n", f"COVEY_{old}": "m", f"LARK_BRIDGE_{old}": "l"}), "n")
                self.assertEqual(cenv.get(asked, None, {f"COVEY_{old}": "m", f"LARK_BRIDGE_{old}": "l"}), "m")
                self.assertEqual(cenv.get(asked, None, {f"LARK_BRIDGE_{old}": "l"}), "l")
                self.assertIsNone(cenv.get(asked, None, {}))
        self.assertTrue(cenv.has("COVEY_BUTLER", {"LARK_BRIDGE_MAIN_THREAD": ""}))  # Set to empty is set.

    def test_reading_an_old_name_from_the_process_environment_points_to_the_new_one(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"COVEY_MAIN_TRACKS": "1", "LARK_BRIDGE_MAIN_DISPATCH_CARD": "1"}), \
                contextlib.redirect_stderr(err):
            cenv._warned.clear()
            self.assertEqual(cenv.get("COVEY_BUTLER_TRACKS"), "1")
            self.assertEqual(cenv.get("COVEY_DISPATCH_CARD"), "1")
            self.assertEqual(cenv.get("COVEY_BUTLER_TRACKS"), "1")
        lines = err.getvalue().splitlines()
        self.assertEqual(lines, ["COVEY_MAIN_TRACKS is the old name of COVEY_BUTLER_TRACKS: set COVEY_BUTLER_TRACKS instead "
                                 "(the old name still works)",
                                 "LARK_BRIDGE_MAIN_DISPATCH_CARD is the old name of COVEY_DISPATCH_CARD: set COVEY_DISPATCH_CARD "
                                 "instead (the old name still works)"])

    def test_canonical_mirrored_and_put_use_every_spelling(self):
        self.assertEqual(cenv.canonical({"COVEY_BUTLER": "1", "COVEY_MAIN_TRACKS": "1", "LARK_BRIDGE_MAIN_JOB": "j", "PATH": "/b"}),
                         {"LARK_BRIDGE_MAIN_THREAD": "1", "LARK_BRIDGE_MAIN_TRACKS": "1", "LARK_BRIDGE_MAIN_JOB": "j", "PATH": "/b"})
        # The new name beats an old one, whatever the order they came in.
        for order in ({"LARK_BRIDGE_MAIN_THREAD": "old", "COVEY_BUTLER": "new"}, {"COVEY_BUTLER": "new", "LARK_BRIDGE_MAIN_THREAD": "old"},
                      {"COVEY_MAIN_THREAD": "mid", "COVEY_BUTLER": "new", "LARK_BRIDGE_MAIN_THREAD": "old"}):
            self.assertEqual(cenv.canonical(order), {"LARK_BRIDGE_MAIN_THREAD": "new"})
        self.assertEqual(cenv.canonical({"COVEY_MAIN_THREAD": "mid", "LARK_BRIDGE_MAIN_THREAD": "old"}), {"LARK_BRIDGE_MAIN_THREAD": "mid"})
        self.assertEqual(cenv.mirrored({"LARK_BRIDGE_MAIN_STATE": "/s"}),
                         {"LARK_BRIDGE_MAIN_STATE": "/s", "COVEY_MAIN_STATE": "/s", "COVEY_BUTLER_STATE": "/s"})
        self.assertEqual(cenv.mirrored({"COVEY_BUTLER_STATE": "/new", "LARK_BRIDGE_MAIN_STATE": "/old"}),
                         dict.fromkeys(("COVEY_BUTLER_STATE", "COVEY_MAIN_STATE", "LARK_BRIDGE_MAIN_STATE"), "/new"))
        child = {}
        cenv.put(child, "COVEY_BUTLER_JOB", "k")
        self.assertEqual(child, {"COVEY_BUTLER_JOB": "k", "COVEY_MAIN_JOB": "k", "LARK_BRIDGE_MAIN_JOB": "k"})

    def test_the_manifest_of_the_live_deployment_means_the_same_under_the_new_names(self):
        live = {"LARK_BRIDGE_MAIN_THREAD": "1", "LARK_BRIDGE_MAIN_TRACKS": "1", "LARK_BRIDGE_MAIN_DISPATCH_CARD": "1",
                "LARK_BRIDGE_MAIN_BUTLER": "1", "LARK_BRIDGE_PINNED_TOPICS": "1"}
        for name in live:
            self.assertTrue(persistable(name), name)
        self.assertEqual(runtime_environment(live), live)
        # The shell says the same under the new names, or the middle ones: the deploy comparison finds no difference.
        new = {"COVEY_BUTLER": "1", "COVEY_BUTLER_TRACKS": "1", "COVEY_DISPATCH_CARD": "1", "COVEY_BUTLER_RULES": "1",
               "COVEY_PINNED_TOPICS": "1"}
        middle = {"COVEY_MAIN_THREAD": "1", "COVEY_MAIN_TRACKS": "1", "COVEY_MAIN_DISPATCH_CARD": "1", "COVEY_MAIN_BUTLER": "1",
                  "COVEY_PINNED_TOPICS": "1"}
        self.assertTrue(all(persistable(name) for name in (*new, *middle)))
        self.assertEqual(runtime_environment(new), live)
        self.assertEqual(runtime_environment(middle), live)
        # The service started from that manifest has every spelling, so the code reads the new name and a task runner of
        # an older release the old one; its own environment's spelling of the same setting does not survive.
        env = service_environment({"COVEY_BUTLER": "0", "PATH": "/b"}, live)
        for new_name, old_name in (("COVEY_BUTLER", "MAIN_THREAD"), ("COVEY_BUTLER_TRACKS", "MAIN_TRACKS"),
                                   ("COVEY_DISPATCH_CARD", "MAIN_DISPATCH_CARD"), ("COVEY_BUTLER_RULES", "MAIN_BUTLER")):
            self.assertEqual((env[new_name], env["COVEY_" + old_name], env["LARK_BRIDGE_" + old_name]), ("1", "1", "1"), new_name)
        self.assertEqual(cenv.get("COVEY_BUTLER", None, env), "1")
        self.assertEqual(env["PATH"], "/b")


if __name__ == "__main__":
    unittest.main()

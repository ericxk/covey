"""covey/service/lark_cli_upgrade.py (docs/deploy.md lark-cli 跟着升级): the machine's lark-cli brought along by an upgrade,
against a fake npm global lark-cli (support/lark_cli_fake.py), so neither the machine's lark-cli nor the network is touched.

Only npm's global one is updated, and only by ``update``: ``note`` (the read-only modes) runs ``--version`` and ``npm root -g``
and nothing else; a copy in front of PATH is said and left alone; an update that does not go through keeps the old one and
says why; one that leaves no lark-cli that runs is BROKEN; one that never ends is stopped with its process group."""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path

from support import exec_tempdir
from support import lark_cli_fake

from covey.service import lark_cli_upgrade as lcu


class LarkCliUpgradeTest(unittest.TestCase):
    def setUp(self):
        folder = exec_tempdir()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.fake = lark_cli_fake.install(self.tmp / "npm")
        self.data = self.tmp / "data"
        self.said = []
        self.env = {"PATH": os.pathsep.join([self.fake["bin"], "/usr/bin", "/bin"]), "HOME": str(self.tmp)}

    def version(self):
        return lcu.version(lcu.find(self.env), self.env)

    def test_npm_s_global_one_is_found_and_a_copy_in_front_of_path_is_not(self):
        found = lcu.find(self.env)
        self.assertTrue(found["global"], found)
        self.assertEqual(found["path"], os.path.join(self.fake["bin"], "lark-cli"))
        self.assertTrue(found["real"].endswith(os.path.join("@larksuite", "cli", "scripts", "run.js")))
        self.assertEqual(self.version(), "1.0.99")
        local = lark_cli_fake.local_copy(self.tmp / "console" / "lark-cli-1.0.100")
        env = {**self.env, "PATH": os.pathsep.join([local, self.env["PATH"]])}
        found = lcu.find(env)
        self.assertFalse(found["global"])
        self.assertIn("npm root -g", found["why"])
        self.assertEqual(lcu.version(found, env), "1.0.100")
        no_npm = lcu.find({"PATH": local})
        self.assertFalse(no_npm["global"])
        self.assertIn("没有 npm", no_npm["why"])
        self.assertEqual(lcu.find({"PATH": "/nonexistent"}), {})

    def test_the_read_only_note_never_updates(self):
        self.assertEqual(lcu.note(self.env, self.said.append), lcu.PLANNED)
        self.assertIn("1.0.99", self.said[-1])
        self.assertIn("升级时会运行 lark-cli update", self.said[-1])
        local = lark_cli_fake.local_copy(self.tmp / "console" / "lark-cli-1.0.100")
        self.assertEqual(lcu.note({**self.env, "PATH": local + os.pathsep + self.env["PATH"]}, self.said.append), lcu.SKIPPED)
        self.assertIn("升级时不动它", self.said[-1])
        self.assertEqual(lcu.note({"PATH": "/nonexistent"}, self.said.append), lcu.SKIPPED)
        self.assertEqual(lark_cli_fake.calls(self.fake), [])
        self.assertEqual(self.version(), "1.0.99")
        self.assertFalse((self.data / "deploy-runs").exists())

    def test_an_update_says_the_versions_and_a_second_is_current(self):
        self.assertEqual(lcu.update(self.data, self.env, self.said.append), lcu.DONE)
        self.assertIn("更新好了：1.0.99 → 1.0.100，lark Skills 也同步了", self.said[-1])
        [call] = lark_cli_fake.calls(self.fake)
        self.assertEqual(call["argv"], ["update", "--json"])
        self.assertEqual(call["path"], self.env["PATH"])  # The service's own PATH, so its npm and proxy.
        [run] = (self.data / "deploy-runs").iterdir()
        self.assertTrue(run.name.endswith("-lark-cli"))
        log = (run / "lark-cli.log").read_text()
        self.assertIn("before=1.0.99", log)
        self.assertIn("after=1.0.100", log)
        self.assertEqual(lcu.update(self.data, self.env, self.said.append), lcu.CURRENT)
        self.assertIn("已经是最新（1.0.100）", self.said[-1])

    def test_an_update_that_does_not_go_through_keeps_the_old_one(self):
        for mode, words in (("eacces", "Permission denied"), ("network", "network"), ("lie", "说更新好了")):
            with self.subTest(mode=mode):
                self.assertEqual(lcu.update(self.data, {**self.env, "FAKE_LARK_MODE": mode}, self.said.append), lcu.FAILED)
                self.assertIn("没更新上，还用原来的 1.0.99（不影响这次升级）", self.said[-1])
                self.assertIn(words, self.said[-1])
                self.assertIn("单独重跑：", self.said[-1])
                self.assertEqual(self.version(), "1.0.99")

    def test_one_that_no_longer_runs_is_broken(self):
        self.assertEqual(lcu.update(self.data, {**self.env, "FAKE_LARK_MODE": "broken"}, self.said.append), lcu.BROKEN)
        self.assertIn("更新后跑不起来了（更新前是 1.0.99）", self.said[-1])
        self.assertIn("npm install -g @larksuite/cli@1.0.99", self.said[-1])

    def test_a_copy_in_front_of_path_is_left_alone(self):
        local = lark_cli_fake.local_copy(self.tmp / "console" / "lark-cli-1.0.100")
        env = {**self.env, "PATH": os.pathsep.join([local, self.env["PATH"]])}
        self.assertEqual(lcu.update(self.data, env, self.said.append), lcu.SKIPPED)
        self.assertIn(os.path.join(local, "lark-cli"), self.said[-1])
        self.assertIn("没有替你更新", self.said[-1])
        self.assertEqual(lark_cli_fake.calls(self.fake), [])
        self.assertEqual(lcu.update(self.data, {"PATH": "/nonexistent"}, self.said.append), lcu.SKIPPED)

    def test_an_update_that_never_ends_is_stopped_with_its_children(self):
        started = time.monotonic()
        outcome = lcu.update(self.data, {**self.env, "FAKE_LARK_MODE": "hang"}, self.said.append, seconds=3)
        self.assertEqual(outcome, lcu.FAILED)
        self.assertLess(time.monotonic() - started, 60)
        self.assertIn("超过 3 秒没结束", self.said[-1])
        pid = int((self.tmp / "npm" / "hang.pid").read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the update's child was left running")


if __name__ == "__main__":
    unittest.main()

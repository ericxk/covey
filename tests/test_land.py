"""scripts/land.py (批量合入) on throwaway repositories: a bare origin, a clone and its worktrees as the topics'.
The clone's scripts/test_map.py is a stand-in: a file breaks_<x> at the root fails test_x, flaky_<x> fails it in a
full run only, a file STALE makes a full run refuse until `build` rewrote the map."""

import fcntl
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import REPO

spec = importlib.util.spec_from_file_location("land", REPO / "scripts" / "land.py")
land = importlib.util.module_from_spec(spec)
spec.loader.exec_module(land)

FAKE_TEST_MAP = '''\
import sys
from pathlib import Path

args = sys.argv[1:]
root = Path(".")
rebuilt = "rebuilt" in (root / "tests" / "test_map.json").read_text()
if args[0] == "run" and "--full" in args and "--force" not in args and (root / "STALE").exists() and not rebuilt:
    print("refused: a full run on a stale map (test ids)", file=sys.stderr)
    sys.exit(2)
if args[0] == "build":
    (root / "tests" / "test_map.json").write_text('{"rebuilt": true}\\n')
paths = [arg for arg in args[1:] if arg.startswith("tests/")]
failed = [f"test_{path.name[len('breaks_'):]}" for path in sorted(root.glob("breaks_*"))]
failed = [module for module in failed if not paths or f"tests/{module}.py" in paths]
if not paths:  # flaky_<x>: test_x fails in a full run only.
    failed += [f"test_{path.name[len('flaky_'):]}" for path in sorted(root.glob("flaky_*"))]
for module in failed:
    print(f"FAIL: test_y ({module}.C.test_y)", file=sys.stderr)
sys.exit(1 if failed else 0)
'''


def run(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class LandTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="land-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)])
        origin, self.clone = self.root / "origin.git", self.root / "clone"
        run("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.root)
        run("init", "-q", "-b", "main", str(self.clone), cwd=self.root)
        for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            run("config", key, value, cwd=self.clone)
        (self.clone / "scripts").mkdir()
        (self.clone / "tests").mkdir()
        (self.clone / "scripts" / "test_map.py").write_text(FAKE_TEST_MAP)
        (self.clone / "tests" / "test_map.json").write_text("{}\n")
        (self.clone / "a.txt").write_text("one\ntwo\n")
        self.commit(self.clone, "start")
        run("remote", "add", "origin", str(origin), cwd=self.clone)
        run("push", "-q", "origin", "main", cwd=self.clone)
        run("fetch", "-q", "origin", cwd=self.clone)

    def commit(self, where, message, **files):
        for name, text in files.items():
            path = where / name.replace("__", "/")
            if text is None:
                path.unlink()
            else:
                path.write_text(text)
        run("add", "-A", cwd=where)
        run("commit", "-q", "-m", message, cwd=where)

    def topic(self, name, **files):
        """A worktree of the clone on branch topic/<name>, from origin/main, with one commit of ``files``."""
        where = self.root / name
        run("worktree", "add", "-q", "-b", f"topic/{name}", str(where), "origin/main", cwd=self.clone)
        if files:
            self.commit(where, f"{name}'s change", **files)
        return where

    def hand_in(self, where):
        self.assertEqual(land.submit("why", 0, cwd=where, spawn=False), 3)  # Queued, nobody drives yet.

    def main_log(self):
        run("fetch", "-q", "origin", cwd=self.clone)
        return run("log", "--format=%s", "origin/main", cwd=self.clone).split("\n")[:-1]

    def result(self, name):
        return land.read(land.home(self.clone) / "results" / f"topic-{name}.json")

    def test_branches_handed_in_land_in_one_batch_in_their_order(self):
        first, second = self.topic("a", **{"x.txt": "x\n"}), self.topic("b", **{"y.txt": "y\n"})
        self.hand_in(first)
        self.hand_in(second)
        self.assertEqual(land.drive(cwd=self.clone), 0)
        self.assertEqual(self.main_log(), ["b's change", "a's change", "start"])
        self.assertEqual((self.result("a")["state"], self.result("b")["state"]), ("merged", "merged"))
        self.assertEqual(self.result("b")["commit"], run("rev-parse", "origin/main", cwd=self.clone).strip())
        self.assertEqual(land.queued(land.home(self.clone)), [])
        self.assertEqual(run("for-each-ref", "refs/land", cwd=self.clone), "")

    def test_a_branch_that_conflicts_goes_back_and_the_others_land(self):
        first, second = self.topic("a", **{"a.txt": "one\nTWO\n"}), self.topic("b", **{"a.txt": "one\n2\n"})
        self.hand_in(first)
        self.hand_in(second)
        land.drive(cwd=self.clone)
        self.assertEqual(self.main_log(), ["a's change", "start"])
        kicked = self.result("b")
        self.assertEqual((kicked["state"], kicked["reason"], kicked["files"]), ("kicked", "conflict", ["a.txt"]))

    def test_the_branch_that_breaks_a_test_is_found_by_halves_and_sent_back(self):
        names = ("a", "b", "c")
        topics = [self.topic("a", **{"x.txt": "x\n"}), self.topic("b", breaks_foo=""), self.topic("c", **{"z.txt": "z\n"})]
        for where in topics:
            self.hand_in(where)
        land.drive(cwd=self.clone)
        self.assertEqual([self.result(name)["state"] for name in names], ["merged", "kicked", "merged"])
        self.assertEqual(self.result("b")["failed"], ["test_foo.C.test_y"])
        self.assertEqual(self.main_log(), ["c's change", "a's change", "start"])

    def test_what_fails_on_main_alone_does_not_hold_the_batch(self):
        self.commit(self.clone, "main breaks bar", breaks_bar="")
        run("push", "-q", "origin", "main", cwd=self.clone)
        self.hand_in(self.topic("a", **{"x.txt": "x\n"}))
        land.drive(cwd=self.clone)
        merged = self.result("a")
        self.assertEqual((merged["state"], merged["mains_own"]), ("merged", ["test_bar.C.test_y"]))
        self.assertEqual(self.main_log()[0], "a's change")

    def test_a_failure_that_does_not_come_back_on_the_branch_is_chance_and_the_batch_lands(self):
        self.hand_in(self.topic("a", flaky_baz=""))
        land.drive(cwd=self.clone)
        merged = self.result("a")
        self.assertEqual((merged["state"], merged["flaky"]), ("merged", ["test_baz.C.test_y"]))

    def test_map_only_commits_stay_out_and_a_stale_map_is_rebuilt_once_for_the_batch(self):
        self.commit(self.clone, "stale", STALE="")
        run("push", "-q", "origin", "main", cwd=self.clone)
        where = self.topic("a", **{"x.txt": "x\n"})
        self.commit(where, "Test map rebuilt for a", **{"tests__test_map.json": '{"a": 1}\n'})
        self.hand_in(where)
        land.drive(cwd=self.clone)
        self.assertEqual(self.main_log()[:2], ["Test map rebuilt for the batch of topic-a", "a's change"])
        self.assertEqual(run("show", "origin/main:tests/test_map.json", cwd=self.clone), '{"rebuilt": true}\n')

    def test_hand_in_refuses_uncommitted_changes_a_merge_and_an_empty_branch(self):
        where = self.topic("a", **{"x.txt": "x\n"})
        (where / "tests" / "test_map.json").write_text('{"local": 1}\n')  # A map built here is left out, not refused.
        (where / "x.txt").write_text("changed\n")
        with self.assertRaisesRegex(land.LandError, "uncommitted changes"):
            land.submit("why", 0, cwd=where, spawn=False)
        run("checkout", "-q", "--", "x.txt", cwd=where)
        empty = self.topic("e")
        with self.assertRaisesRegex(land.LandError, "nothing to land"):
            land.submit("why", 0, cwd=empty, spawn=False)
        other = self.topic("o", **{"o.txt": "o\n"})
        run("merge", "-q", "--no-ff", "-m", "merge", "topic/a", cwd=other)
        with self.assertRaisesRegex(land.LandError, "merge commits"):
            land.submit("why", 0, cwd=other, spawn=False)

    def test_a_second_driver_leaves_the_queue_to_the_one_that_runs(self):
        self.hand_in(self.topic("a", **{"x.txt": "x\n"}))
        state = land.home(self.clone)
        with open(state / "lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertTrue(land.driver_alive(state))
            self.assertEqual(land.drive(cwd=self.clone), 0)
        self.assertIsNone(self.result("a"))  # Still queued for the driver that holds the lock.
        self.assertFalse(land.driver_alive(state))
        self.assertEqual(land.wait("topic-a", 0, cwd=self.clone), 3)


if __name__ == "__main__":
    unittest.main()

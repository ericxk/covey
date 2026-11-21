"""A topic hears that another topic works in the same git repository (app/repo_peers.py)."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import UnifiedCase
from test_auto_retry import Failing
from support.records import write_job, write_topic

from covey.app import repo_peers


class Case(unittest.TestCase):
    def setUp(self):
        repo_peers._cache.clear()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "pkg" / "sub").mkdir(parents=True)
        self.now = time.time()

    def topic(self, name, number, title):
        write_topic(self.state, topic_id=name, topic_number=number, origin={"title": title}, chat_id="oc_chat")

    def job(self, name, workdir, *, status="running", **more):
        # The record's own time is now: the store stamps ``updated_at`` on every write.
        write_job(self.state, f"om_{name}", topic_id=name, chat_id="oc_chat", job_id=abs(hash(name)) % 100000,
                  kind="chat", status=status, workdir=str(workdir), **more)

    def ctx(self, workdir, **more):
        return SimpleNamespace(state_dir=self.state, topic_id="mine", workdir=workdir, device="", **more)

    def note(self, workdir=None, **more):
        job = SimpleNamespace(topic_id="mine", chat_id="oc_chat", read_only=False)
        return repo_peers.note(self.ctx(workdir or self.repo / "pkg", **more), job, self.now)


class RepoTest(Case):
    def test_the_nearest_ancestor_with_a_git_directory(self):
        self.assertEqual(repo_peers.repo_of(self.repo / "pkg" / "sub"), str(self.repo / ".git"))
        self.assertEqual(repo_peers.repo_of(self.root / "elsewhere"), "")
        self.assertEqual(repo_peers.repo_of("relative/dir"), "")
        self.assertEqual(repo_peers.repo_of(""), "")

    def test_a_linked_worktree_is_its_main_repository(self):
        tree = self.root / "tree"
        tree.mkdir()
        (self.repo / ".git" / "worktrees" / "tree").mkdir(parents=True)
        (tree / ".git").write_text(f"gitdir: {self.repo}/.git/worktrees/tree\n")
        self.assertEqual(repo_peers.repo_of(tree), str(self.repo / ".git"))
        (tree / ".git").write_text("garbage")
        repo_peers._cache.clear()
        self.assertEqual(repo_peers.repo_of(tree), "")

    def test_it_is_cached_and_bounded(self):
        self.assertTrue(repo_peers.repo_of(self.repo))
        (self.repo / ".git").rmdir()
        self.assertTrue(repo_peers.repo_of(self.repo))  # Cached.
        self.assertEqual(repo_peers.repo_of(self.repo, self.now + repo_peers.TTL + 1), "")
        deep = self.root
        for n in range(repo_peers.DEPTH + 3):
            deep = deep / f"d{n}"
        deep.mkdir(parents=True)
        (self.root / ".git").mkdir()
        self.assertEqual(repo_peers.repo_of(deep), "")  # Too far below its repository.


class NoteTest(Case):
    def test_a_running_topic_in_the_same_repository_is_named_with_the_rule(self):
        self.topic("other", 12, "修登录")
        self.job("other", self.repo / "pkg" / "sub")
        line = self.note()
        self.assertTrue(line.startswith("同一个仓库里还有话题在做：T12《修登录》。"), line)
        for words in ("git fetch", "rebase", "只提交自己改的文件", "git diff origin/main...HEAD --stat"):
            self.assertIn(words, line)

    def test_a_queued_one_counts_and_a_long_list_is_cut(self):
        for n in range(5):
            self.topic(f"o{n}", 20 + n, f"话题{n}")
            self.job(f"o{n}", self.repo, status="queued" if n == 0 else "running")
        line = self.note()
        self.assertEqual(line.count("《"), repo_peers.SHOWN)
        self.assertIn("等 5 个话题", line)

    def test_no_overlap_no_line(self):
        self.topic("elsewhere", 30, "别处")
        other = self.root / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.job("elsewhere", other)
        self.topic("done", 32, "做完了")
        self.job("done", self.repo, status="done")
        self.topic("dev", 33, "设备上")
        self.job("dev", self.repo, device="box")
        self.topic("mine", 1, "自己")
        self.job("mine", self.repo)
        self.assertEqual(self.note(), "")  # Itself, and these: another repository, finished, on a device.

    def test_a_record_not_touched_for_half_an_hour_is_not_working_now(self):
        self.topic("other", 12, "修登录")
        self.job("other", self.repo)
        self.assertTrue(self.note())
        job = SimpleNamespace(topic_id="mine", chat_id="oc_chat", read_only=False)
        self.assertEqual(repo_peers.note(self.ctx(self.repo), job, time.time() + repo_peers.WINDOW + 60), "")

    def test_no_repository_a_device_or_a_read_only_turn_asks_nothing(self):
        self.topic("other", 12, "修登录")
        self.job("other", self.repo)
        self.assertEqual(self.note(self.root / "plain"), "")
        self.assertEqual(repo_peers.note(self.ctx(self.repo, ), SimpleNamespace(topic_id="mine", chat_id="x", read_only=True), self.now), "")
        with mock.patch.object(repo_peers, "others", side_effect=OSError("nas")):
            self.assertEqual(self.note(), "")
        ctx = self.ctx(self.repo)
        ctx.device = "box"
        self.assertEqual(repo_peers.note(ctx, SimpleNamespace(topic_id="mine", chat_id="oc_chat", read_only=False), self.now), "")

    def test_a_long_title_is_cut(self):
        self.topic("other", 12, "很长" * 30)
        self.job("other", self.repo)
        self.assertIn("…》", self.note())


class DuplicatesTest(Case):
    def test_only_a_title_like_it_in_the_same_repository(self):
        self.topic("a", 12, "查昨晚的失败日志")
        self.job("a", self.repo)
        self.topic("b", 13, "部署网关新版本")
        self.job("b", self.repo)
        found = repo_peers.duplicates(self.state, "mine", self.repo, "查失败日志", self.now)
        self.assertEqual(found, ["T12《查昨晚的失败日志》"])
        self.assertEqual(repo_peers.duplicates(self.state, "mine", self.root / "plain", "查失败日志", self.now), [])
        self.assertEqual(repo_peers.duplicates(self.state, "a", self.repo, "查失败日志", self.now), [])  # Not itself.
        self.assertGreater(repo_peers.similar("查失败日志", "查昨晚的失败日志"), repo_peers.SIMILAR)
        self.assertEqual(repo_peers.similar("", "x"), 0.0)


class WiredTest(UnifiedCase):
    def test_the_line_is_in_the_turn_s_message_only_when_there_is_one(self):
        claude = self.app.apps["claude"]
        claude.harness.backend = Failing()
        self.receive("task", "initial")
        _, ctx = self.route("task")
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, ctx = self.route("task")
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        live = SimpleNamespace(ctx=ctx, rules_session=True)
        with mock.patch.object(repo_peers, "note", return_value="同一个仓库里还有话题在做：T9。"):
            self.assertIn("\n\n同一个仓库里还有话题在做：T9。", claude.runs._prompt(live, job, None)[0])
        with mock.patch.object(repo_peers, "note", return_value=""):
            self.assertNotIn("同一个仓库", claude.runs._prompt(live, job, None)[0])


if __name__ == "__main__":
    unittest.main()

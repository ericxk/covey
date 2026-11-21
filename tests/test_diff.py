from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.git import commit_all, init_git_repo
from covey.core.diff import (
    DiffLimits,
    append_diff_summary_note,
    capture_job_diff_snapshot,
    finalize_job_diff,
    missing_commits,
    render_diff_summary_note,
)


def output(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


def make_job() -> SimpleNamespace:
    return SimpleNamespace(job_id=7, message_id="om_unit")


class BridgeDiffTest(unittest.TestCase):
    def test_clean_file_change_renders_inline_diff_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "app.py").write_text("print('old')\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            (root / "app.py").write_text("print('new')\n", encoding="utf-8")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertIn("-print('old')", result.files[0].diff_text)
            self.assertIn("+print('new')", result.files[0].diff_text)
            self.assertTrue(Path(result.artifact_path).exists())
            # The diff text is stored once, in diff.md; diff.json keeps the per-file stats.
            self.assertIn("+print('new')", Path(result.artifact_path).read_text())
            index = json.loads(Path(result.json_path).read_text())
            self.assertEqual([(f["path"], f["additions"], f["deletions"]) for f in index["files"]], [("app.py", 1, 1)])
            self.assertNotIn("print(", Path(result.json_path).read_text())
            # The limits it was taken with, none of them an inline limit nothing read (the owner, 2026-11-05).
            self.assertEqual(index["limits"], {"max_snapshot_file_bytes": DiffLimits().max_snapshot_file_bytes,
                                               "max_snapshot_total_bytes": DiffLimits().max_snapshot_total_bytes,
                                               "max_git_diff_capture_chars": DiffLimits().max_git_diff_capture_chars})
            note = render_diff_summary_note(result)
            self.assertIn("本次修改 Diff", note)
            self.assertIn("M app.py", note)
            self.assertNotIn("本地 Markdown", note)
            self.assertNotIn("本地 JSON", note)
            self.assertNotIn("```diff", note)
            self.assertEqual(append_diff_summary_note("最终答案", result), "最终答案\n\n" + note)
            self.assertEqual(append_diff_summary_note("最终答案", None), "最终答案")

    def test_committed_file_change_still_renders_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            (root / "app.py").write_text("new\n", encoding="utf-8")
            commit_all(root, "change")
            self.assertEqual(output(["git", "status", "--porcelain"], root), "")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertIn("-old", result.files[0].diff_text)
            self.assertIn("+new", result.files[0].diff_text)

    def test_committed_then_dirty_change_uses_job_start_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            (root / "app.py").write_text("committed\n", encoding="utf-8")
            commit_all(root, "change")
            (root / "app.py").write_text("dirty\n", encoding="utf-8")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertIn("-old", result.files[0].diff_text)
            self.assertIn("+dirty", result.files[0].diff_text)
            self.assertNotIn("+committed", result.files[0].diff_text)

    def test_predirty_file_uses_job_start_content_as_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "app.py").write_text("base\n", encoding="utf-8")
            commit_all(root)
            (root / "app.py").write_text("before job\n", encoding="utf-8")

            snapshot = capture_job_diff_snapshot(root)
            (root / "app.py").write_text("after job\n", encoding="utf-8")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertIn("-before job", result.files[0].diff_text)
            self.assertIn("+after job", result.files[0].diff_text)
            self.assertNotIn("-base", result.files[0].diff_text)

    def test_file_count_limit_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            for idx in range(12):
                (root / f"f{idx}.txt").write_text("old\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            for idx in range(12):
                (root / f"f{idx}.txt").write_text("new\n", encoding="utf-8")
            result = finalize_job_diff(
                snapshot,
                root / ".state",
                make_job(),
            )

            self.assertEqual(len(result.files), 12)
            summary = render_diff_summary_note(result)
            self.assertIn("本次修改 Diff", summary)
            self.assertIn("另有 2 个文件", summary)

    def test_character_limit_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / "large.txt").write_text("old\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            (root / "large.txt").write_text("new\n" * 200, encoding="utf-8")
            result = finalize_job_diff(
                snapshot,
                root / ".state",
                make_job(),
            )

            self.assertEqual(result.truncated_file_count, 0)
            note = render_diff_summary_note(result)
            self.assertIn("large.txt", note)
            self.assertNotIn("```diff", note)

    def test_sensitive_file_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / ".env").write_text("TOKEN=old\n", encoding="utf-8")
            commit_all(root)

            snapshot = capture_job_diff_snapshot(root)
            (root / ".env").write_text("TOKEN=new\n", encoding="utf-8")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertEqual(result.hidden_file_count, 1)
            self.assertIn("敏感文件", result.files[0].hidden_reason)

    def test_symlink_to_outside_workspace_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            init_git_repo(root)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            commit_all(root)
            outside = base / "outside-data.txt"
            outside.write_text("HOST_SECRET\n", encoding="utf-8")

            snapshot = capture_job_diff_snapshot(root)
            os.symlink(outside, root / "linked-data.txt")
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(len(result.files), 1)
            self.assertEqual(result.hidden_file_count, 1)
            self.assertIn("符号链接", result.files[0].hidden_reason)
            self.assertNotIn("HOST_SECRET", result.files[0].diff_text)

    def test_predirty_skipped_file_without_status_change_is_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            (root / ".env").write_text("TOKEN=base\n", encoding="utf-8")
            commit_all(root)
            (root / ".env").write_text("TOKEN=dirty-before-job\n", encoding="utf-8")

            snapshot = capture_job_diff_snapshot(root)
            result = finalize_job_diff(snapshot, root / ".state", make_job())

            self.assertEqual(result.files, [])


class MissingCommitsTest(unittest.TestCase):
    """#18: the commits a turn's delivery names, checked against the repository of its workdir (git, no model)."""

    def repo(self, tmp):
        root = Path(tmp) / "repo"
        (root / "sub").mkdir(parents=True)
        init_git_repo(root)
        (root / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
        commit_all(root)
        head = output(["git", "rev-parse", "HEAD"], root).strip()
        tree = output(["git", "rev-parse", "HEAD^{tree}"], root).strip()
        return root, head, tree

    def test_a_commit_the_repository_lacks_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, head, tree = self.repo(tmp)
            # Full or short, from a subdirectory too; a tree's id is no commit; each asked once, in order.
            self.assertEqual(missing_commits(root / "sub", [head, head[:7], "deadbee", tree, "deadbee"]),
                             ["deadbee", tree])
            self.assertEqual(missing_commits(root, [head]), [])
            # Words that are not ids are never given to git.
            self.assertEqual(missing_commits(root, ["bridge/x 分支", "--all", "abc"]), [])

    def test_it_says_nothing_when_it_cannot_be_told(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, head, _ = self.repo(tmp)
            plain = Path(tmp) / "plain"
            plain.mkdir()
            with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": tmp}):
                self.assertIsNone(missing_commits(plain, ["deadbee"]))  # Not a repository.
            self.assertIsNone(missing_commits(Path(tmp) / "gone", ["deadbee"]))
            with mock.patch("covey.core.diff.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)) as run:
                self.assertIsNone(missing_commits(root, ["deadbee"]))  # Too slow.
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            with mock.patch("covey.core.diff.subprocess.run", side_effect=FileNotFoundError("git")):
                self.assertIsNone(missing_commits(root, ["deadbee"]))  # No git.


if __name__ == "__main__":
    unittest.main()

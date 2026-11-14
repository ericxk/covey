"""scripts/compat_report.py (repo review R2): the # compat(...) comments and its checks stay in step, each
check counts what still needs its code, and the report only reads."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("compat_report_script", REPO / "scripts" / "compat_report.py")
compat = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = compat  # Its dataclass looks the module up.
_spec.loader.exec_module(compat)

MARK = re.compile(r"# compat\((?P<commit>[0-9a-f]{7,40})\): (?P<body>(?:[^\n]*\n[ \t]*# )*?[^\n]*?)"
                  r"\(compat_report: (?P<name>[\w-]+)\)")


def marks():
    """(check name, commit, file relative to covey/) of every # compat(...) comment."""
    root = REPO / "covey"
    found = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        for match in MARK.finditer(text):
            assert "; retire when " in " ".join(match["body"].replace("#", " ").split()), (path, match["body"])
            found.add((match["name"], match["commit"], str(path.relative_to(root))))
        assert text.count("# compat(") == len(MARK.findall(text)), f"{path}: a # compat( comment without its check"
    return found


class CommentTests(unittest.TestCase):
    def test_each_compat_comment_names_a_check_with_its_commit_and_each_check_has_its_comments(self):
        found = marks()
        listed = {(c.name, c.commit, f) for c in compat.CHECKS for f in c.files}
        self.assertEqual(found, listed)

    def test_the_fields_maintenance_drops_as_unread_are_read_nowhere(self):
        # Repo review R2: no live release named these; only the list that drops them does.
        from covey.core.jobs import FINISHED_UNREAD_FIELDS
        dead = FINISHED_UNREAD_FIELDS[3:]
        self.assertEqual(dead, ("diff_doc_error", "diff_doc_id", "diff_doc_url", "diff_status", "topic_only",
                                "card_style", "input_tokens_source"))
        users = [f"{path.relative_to(REPO)}:{name}" for path in sorted([*(REPO / "covey").rglob("*.py"),
                                                                         *(REPO / "bin").glob("*.py")])
                 if path.name != "jobs.py" for name in dead if name in path.read_text()]
        self.assertEqual(users, [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "data" / "harness-app"
        self.state.mkdir(parents=True)
        self.services = Path(self.temp.name) / "data" / "bridge-services"
        self.alive = set()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        # Records as older releases left them (fields no writer of this release makes, no updated_at, runs/
        # files of old runners), byte for byte: by hand, not through support/records.py.
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def register(self, command):
        """The service's manifest, as the Manager keeps it, with this command line."""
        path = self.services / "services" / (compat.service_key(self.state) + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": compat.service_key(self.state), "state_dir": str(self.state),
                                    "command": command}))

    def run_report(self, *args):
        out = io.StringIO()
        with mock.patch.object(compat, "runner_alive", lambda run: run.name in self.alive), redirect_stdout(out):
            code = compat.main(["--state", str(self.state), "--service-root", str(self.services), "--json", *args])
        return code, {row["check"]: row for row in json.loads(out.getvalue())}

    def test_an_empty_state_needs_none_of_them(self):
        self.write("../harnesses.json", {"claude": []})
        self.register(["/absolute/python3", "/absolute/bin/lark_bridge.py", "--state-dir", str(self.state)])
        code, rows = self.run_report()
        self.assertEqual(code, 0)
        self.assertEqual({name: row["verdict"] for name, row in rows.items()},
                         {**{c.name: "可删" for c in compat.CHECKS}, "lock-recheck": "已删"})

    def test_a_harness_config_it_cannot_read_leaves_its_checks_unknown(self):
        # The service's --harness-config can be anywhere: none next to the state directory is not none at all.
        _, rows = self.run_report()
        self.assertEqual({name: (row["verdict"], row["count"]) for name, row in rows.items()
                          if name in ("removed-options", "local-state")},
                         {"removed-options": ("不知道", None), "local-state": ("不知道", None)})
        config = self.write("../elsewhere/bridge.json", {"claude": ["--traex-site", "i18n-tt"]})
        _, rows = self.run_report("--harness-config", str(config))
        self.assertEqual((rows["removed-options"]["verdict"], rows["removed-options"]["count"]), ("还要留", 1))
        self.assertEqual(rows["local-state"]["verdict"], "可删")

    def test_codex_transport_is_counted_where_codex_options_are_parsed(self):
        # Codex is the default Harness: its options come from the service's command line, copied into each task
        # runner's handoff; harnesses.json's "codex" entry is never read, so it does not keep the option.
        self.write("../harnesses.json", {"codex": ["--codex-transport", "app-server"]})
        row = self.run_report()[1]["codex-transport"]
        self.assertEqual((row["verdict"], row["count"]), ("不知道", None))
        self.register(["/absolute/python3", "/absolute/bin/lark_bridge.py"])
        row = self.run_report()[1]["codex-transport"]
        self.assertEqual((row["verdict"], row["count"]), ("可删", 0))
        self.register(["/absolute/python3", "/absolute/bin/lark_bridge.py", "--codex-transport=app-server"])
        self.write("runs/old/handoff.json", {"argv": ["--codex-transport", "app-server", "--state-dir", "/absolute/state"]})
        self.write("runs/old/runner.json", {"release": "/absolute/releases/old"})
        self.write("runs/new/handoff.json", {"argv": ["--state-dir", "/absolute/state"]})
        self.write("runs/new/runner.json", {"release": "/absolute/releases/new"})
        self.write("runs/ended/handoff.json", {"argv": ["--codex-transport", "app-server"]})
        self.alive = {"old", "new"}
        row = self.run_report()[1]["codex-transport"]
        self.assertEqual((row["verdict"], row["count"], row["detail"]), ("还要留", 2, "服务命令行、任务进程 old"))

    def test_each_check_counts_the_records_that_need_it(self):
        self.write("topics/t1/topic.json", {"harness_id": "neeko"})
        self.write("topics/t2/topic.json", {"harness_id": "codex-w"})
        self.write("topics/t3/topic.json", {"harness_id": "claude"})
        self.write("topics/t4/topic.json", {"harness_id": "doubao"})  # A Harness, then a provider; both removed.
        self.write("topics/t5/topic.json", {"harness_id": "claude", "model": "traex/meilin/claude-opus-5-5"})
        self.write("topics/t6/topic.json", {"harness_id": "claude", "model": "claude-opus-5-5"})
        # A "doubao:" key of the Harness is on no menu, so skipped without any code.
        self.write("launch-defaults/chat.json", {"model_choice": "doubao:claude-opus-5-5"})
        self.write("launch-defaults/other.json", {"model_choice": "claude:doubao/claude-opus-5-5"})
        self.write("model-usage.json", {"events": [{"model": "doubao:claude-opus-5-5"}, {"model": "claude:opus"},
                                                   {"model": "claude:kiro-shared/claude-opus-5-5"}]})
        self.write("launch-forms/old.json", {"schema_version": 2})
        self.write("launch-forms/split.json", {"schema_version": 3, "split_config": True})
        self.write("launch-forms/new.json", {"schema_version": 3})
        self.write("jobs/a.json", {"key": "a", "tool_steps": {"s": {"started": 1}}, "updated_at": "2026-10-19T01"})
        self.write("jobs/b.json", {"key": "b", "tool_steps": {"s": {"started": 1}}})
        self.write("traces/b.jsonl", "")
        self.write("jobs/c.json", {"key": "c", "trace_snapshot": {"steps": {}}})
        self.write("traces/c.jsonl.gz", "")
        self.write("jobs/d.json", {"key": "d", "card_id": "card", "updated_at": "2026-10-21T02"})
        self.write("jobs/e.json", {"key": "e", "card_id": "card", "card_sequence": 7})
        self.write("jobs/f.json", {"key": "f", "card_id": "card", "card_sequence": 1, "created_at": "2026-11-03T10:00Z"})
        self.write("jobs/g.json", {"key": "g", "card_id": "card", "card_sequence": 1, "created_at": "2026-11-05T01:00Z"})
        self.write("jobs/h.json", {"key": "h", "backend": "doubao"})
        self.write("proxy/panels/old.json", {"view": {"found": {"query": "q"}, "lookup": None}})
        self.write("proxy/panels/new.json", {"view": {"found": None, "lookup": {"id": "x"}}})
        self.write("proxy/panels/latest.json", {"token": "new"})
        self.write("../harnesses.json", {"maso": ["--maso-python", "/code/.local-state/maso-runtime/venv/bin/python"],
                                         "claude": ["--claude-transport", "stream-json", "--traex-ppe-env", "ppe_trae_eval",
                                                    "--traex-site=i18n-tt"],
                                         "codex": ["--codex-transport", "app-server"], "dsh": ["--dsh-cmd", "dsh"]})
        self.write("jobs/l.json", {"key": "l", "auto_retry_state": "pending"})
        self.write("jobs/m.json", {"key": "m", "auto_retry_state": "pending", "auto_retry_kind": "incomplete"})
        # A policy stop writes "cancelled" without a kind today, and kind_of never reads it.
        self.write("jobs/n.json", {"key": "n", "status": "cancelled", "policy_guard_finalized": True,
                                   "auto_retry_state": "cancelled", "session_id": "s"})
        self.write("job-diffs/x/diff.json", {"files": [{"path": "a", "diff_text": "+a"}]})
        self.write("job-diffs/y/diff.json", {"files": [{"path": "a"}]})
        self.write("switch-forms/old.json", {"token": "old", "selected_model": "claude:opus"})
        self.write("switch-forms/new.json", {"token": "new", "selected_workspace": "w", "selected_redaction": "off"})
        _, rows = self.run_report()
        counts = {name: (row["verdict"], row["count"]) for name, row in rows.items()}
        self.assertEqual(counts["retired-harness"], ("还要留", 3))
        self.assertEqual(rows["retired-harness"]["detail"], "codex-w 1、doubao 1、neeko 1")
        self.assertEqual(counts["retired-launch-form"], ("还要留", 2))
        self.assertEqual(counts["embedded-trace"], ("还要留", 1))
        self.assertEqual(rows["embedded-trace"]["detail"], "最新一条 2026-10-19")
        self.assertEqual(counts["card-sequence-floor"], ("还要留", 1))
        self.assertEqual(counts["local-state"], ("还要留", 1))
        self.assertEqual(counts["panel-lookup"], ("还要留", 1))
        self.assertEqual(rows["panel-lookup"]["detail"], "old.json")
        self.assertEqual(counts["trace-pages"], ("还要留", 3))  # d and e have no created_at: older.
        self.assertEqual(rows["trace-pages"]["detail"], "最新一条 2026-11-03")
        self.assertEqual(counts["retired-provider"], ("还要留", 3))
        self.assertEqual(rows["retired-provider"]["detail"], "话题 1、启动默认 1、最近选择 1")
        # Claude's options of removed code still passed: its transport's (ab0ce25) and the TRAE provider's; the
        # "codex" entry is never read, so it is not counted.
        self.assertEqual(counts["removed-options"], ("还要留", 3))
        self.assertEqual(rows["removed-options"]["detail"], "harnesses.json 里 --claude-transport 1、--traex-* 2")
        self.assertEqual(counts["doubao-jobs"], ("还要留", 1))
        self.assertEqual(counts["auto-retry-kind"], ("还要留", 1))
        self.assertEqual(counts["fat-diff-index"], ("还要留", 1))
        self.assertEqual(counts["switch-form-fields"], ("还要留", 1))
        self.assertEqual(rows["switch-form-fields"]["detail"], "old.json")

    def test_a_live_runner_of_a_release_before_a_commit_keeps_its_code(self):
        try:
            new = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True, timeout=30).stdout.strip()
            old = subprocess.run(["git", "-C", str(REPO), "rev-parse", "b20cfed^"], capture_output=True, text=True,
                                 check=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            self.skipTest("needs the repository's history")
        releases = Path(self.temp.name) / "releases"
        for commit in (new, old):
            locks = releases / commit / "covey" / "core" / "locks.py"
            locks.parent.mkdir(parents=True)
            locks.write_text((REPO / "covey" / "core" / "locks.py").read_text())
        self.write("runs/new/runner.json", {"release": str(releases / new)})
        self.write("runs/ended/runner.json", {"release": str(releases / old)})
        # Real git for the check this test reads, each pair asked once over the three reports; the other
        # checks' commits are taken as present (each git call is about 0.15 s, and they ask 4 more per release).
        real, asked = compat.Facts.contains, {}

        def contains(facts, release, commit):
            if commit != "b20cfed":
                return True
            key = (str(release), commit)
            if key not in asked:
                asked[key] = real(facts, release, commit)
            return asked[key]
        self.enterContext(mock.patch.object(compat.Facts, "contains", contains))
        self.alive = {"new"}
        self.assertEqual(self.run_report()[1]["steer-refused"]["verdict"], "可删")
        self.write("runs/old/runner.json", {"release": str(releases / old)})
        self.alive = {"new", "old"}
        row = self.run_report()[1]["steer-refused"]
        self.assertEqual((row["verdict"], row["count"], row["detail"]), ("还要留", 1, "任务进程 old"))
        self.write("runs/odd/runner.json", {"release": "/somewhere/a-worktree"})
        self.alive.add("odd")
        self.assertEqual(self.run_report()[1]["steer-refused"]["verdict"], "不知道")

    def test_a_live_runner_of_a_removed_harness_keeps_its_adoption_skip(self):
        self.write("runs/gone/handoff.json", {"harness": "neeko"})
        self.write("runs/here/handoff.json", {"harness": "claude"})
        self.write("runs/ended/handoff.json", {"harness": "neeko"})
        self.alive = {"gone", "here"}
        row = self.run_report()[1]["removed-harness-runners"]
        self.assertEqual((row["verdict"], row["count"], row["detail"]), ("还要留", 1, "gone"))

    def test_a_runner_without_the_lock_recheck_fails_the_report(self):
        # The condition the lock re-check's removal rests on (repo review R2): no such runner alive.
        release = Path(self.temp.name) / "releases" / "af89642"
        (release / "covey" / "core").mkdir(parents=True)
        (release / "covey" / "core" / "locks.py").write_text("def _open_locked(self): pass\n")
        self.write("runs/k/runner.json", {"release": str(release)})
        self.alive = {"k"}
        code, rows = self.run_report()
        self.assertEqual((code, rows["lock-recheck"]["verdict"], rows["lock-recheck"]["count"]),
                         (1, "已删但还有数据要它", 1))

    def test_the_report_only_reads(self):
        self.write("topics/t1/topic.json", {"harness_id": "neeko"})
        self.write("jobs/a.json", {"key": "a", "tool_steps": {}, "card_id": "c"})
        self.write("runs/k/runner.json", {"release": "/nowhere"})
        self.alive = {"k"}

        def snapshot():
            root = self.state.parent
            return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in root.rglob("*"))
        before = snapshot()
        out = io.StringIO()
        with mock.patch.object(compat, "runner_alive", lambda run: True), redirect_stdout(out):
            compat.main(["--state", str(self.state), "--service-root", str(self.services)])
        self.assertEqual(snapshot(), before)
        self.assertIn("还要留\tretired-harness\tapp/topics.py (d3aaeb6)\t1\tneeko 1", out.getvalue())
        self.assertEqual(os.listdir(self.state / "jobs"), ["a.json"])


if __name__ == "__main__":
    unittest.main()

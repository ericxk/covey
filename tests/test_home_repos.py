"""The home (core/home.py), the repo map and the ``covey repo`` command (core/repomap.py), the repositories a topic uses
(core/topic_repos.py, app/repos_turn.py), the turn's home block and environment (app/home_turn.py), a diff across
repositories (core/diff.py) and a Codex turn's writable roots (backends/codex/appserver.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support import make_context
from support.git import commit_all, init_git_repo, run_git
from covey.app import home_turn, repos_turn, runner
from covey.backends.codex import appserver
from covey.core import home, repomap, topic_repos
from covey.core.diff import NO_BASE, capture_job_diff_snapshot, finalize_job_diff, missing_in_repos
from covey.core.task_processes import TASK_DIR_ENV
from covey.core.trace import ToolTrace

TOPIC = "abcdef1234567890"


def make_repo(root: Path, rules="# 规范\n只在 worktree 里改。\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    init_git_repo(root)
    (root / "AGENTS.md").write_text(rules, encoding="utf-8")
    (root / "app.py").write_text("print('old')\n", encoding="utf-8")
    commit_all(root)
    run_git(root, "branch", "-M", "main")
    return root


def trace_of(*steps) -> dict:
    return {"steps": {str(i): {"title": title, "detail": detail, "status": "done"} for i, (title, detail) in enumerate(steps)},
            "children": {}}


class Sandbox(unittest.TestCase):
    """A data root, a state directory and a home of their own; COVEY_* of this process cleared."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data, self.state, self.home = self.root / "data", self.root / "state", self.root / "home"
        for folder in (self.data, self.state, self.home):
            folder.mkdir()
        patcher = mock.patch.dict(os.environ, {**{k: v for k, v in os.environ.items()
                                                  if not k.startswith(("COVEY_", "LARK_BRIDGE_"))},
                                               "COVEY_DATA_HOME": str(self.data)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        topic_repos.cache.clear()

    def topic(self, **fields) -> Path:
        folder = self.state / "topics" / TOPIC
        folder.mkdir(parents=True, exist_ok=True)
        data = {"topic_id": TOPIC, "chat_id": "oc_dm", "topic_number": 7, "workspace_name": "ws", "workdir": str(self.home),
                **fields}
        (folder / "topic.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return folder / "topic.json"

    def ctx(self, **overrides):
        fields = dict(default_workdir=self.home, workdir=self.home, state_dir=self.state,
                      history_path=self.state / "topics" / TOPIC / "history.jsonl", topic_id=TOPIC)
        return make_context(self.root, **{**fields, **overrides})

    def topic_data(self) -> dict:
        return json.loads((self.state / "topics" / TOPIC / "topic.json").read_text(encoding="utf-8"))


class HomeTest(Sandbox):
    def test_a_new_home_gets_its_few_files_and_a_taken_over_one_none(self):
        fresh = self.root / "fresh"
        made = home.seed_home(fresh)
        self.assertLessEqual({"AGENTS.md", "CLAUDE.md", ".ignore", home.SEED_MARK, ".skills/", "tasks/runs/",
                              ".claude/skills", ".agents/skills"}, set(made))
        self.assertEqual((fresh / "AGENTS.md").read_text(), (fresh / "CLAUDE.md").read_text())
        self.assertLessEqual(len((fresh / "AGENTS.md").read_text().splitlines()), 60)
        self.assertEqual(os.readlink(fresh / ".claude" / "skills"), "../.skills")
        self.assertTrue(home.seeded(fresh))
        self.assertEqual(home.seed_home(fresh), [])  # Again: everything is there.
        (self.home / "notes.txt").write_text("mine")
        self.assertEqual(home.seed_home(self.home), [])  # Taken over: nothing written into it.
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["notes.txt"])

    def test_home_json_is_written_only_for_a_home_covey_made(self):
        self.assertIn("taken-over", home.migrate_v1(self.state, self.home))
        self.assertFalse(home.configured(self.state))
        fresh = self.root / "fresh"
        home.seed_home(fresh)
        self.assertIn("defaults", home.migrate_v1(self.state, fresh))
        self.assertEqual(home.read(self.state), home.HomeConfig())
        self.assertIn("already", home.migrate_v1(self.state, fresh))

    def test_the_environment_names_the_home_topic_and_state_with_covey_first_on_path(self):
        ctx = self.ctx()
        with mock.patch.dict(os.environ, {"PATH": f"/usr/bin{os.pathsep}{self.data / 'bin'}"}):
            found = home.env(ctx)
        self.assertEqual((found["COVEY_HOME"], found["COVEY_TOPIC"], found["COVEY_STATE"]),
                         (str(self.home), TOPIC, str(self.state)))
        self.assertEqual(found["PATH"].split(os.pathsep), [str(self.data / "bin"), "/usr/bin"])
        self.assertEqual(home.env(self.ctx(device="gpu1")), {})  # A device's process gets the device's names only.

    def test_the_outputs_directory_renders_the_templates(self):
        config = home.HomeConfig(runs_template="{home}/misc/runs/{date}-{slug}-{topic8}/{run}", run_scope="turn")
        started = time.mktime((2026, 11, 27, 3, 0, 0, 0, 0, -1))
        self.assertEqual(home.topic_dir(config, "/h", number=7, topic_id=TOPIC, title="Fix the Diff 基线", started=started),
                         "/h/misc/runs/20261127-fix-the-diff-abcdef12")
        run = home.run_dir_for(config, "/h", number=7, topic_id=TOPIC, title="中文", started=started, task_key="om_x1y2",
                               now=started)
        self.assertEqual(run, "/h/misc/runs/20261127-topic-abcdef12/20261127-0300-omx1")
        self.assertEqual(home.run_dir_for(home.HomeConfig(), "/h", number=7, topic_id=TOPIC, started=started, now=started),
                         "/h/tasks/runs/20261127-T7-topic/20261127")
        cache = home.cache_env(self.root / "run")
        self.assertEqual(cache["TMPDIR"], str(self.root / "run" / "tmp"))
        self.assertTrue((self.root / "run" / "cache").is_dir())

    def test_the_shim_is_written_and_written_again_when_its_release_is_gone(self):
        old, new = self.root / "rel1" / "bin", self.root / "rel2" / "bin"
        for folder in (old, new):
            folder.mkdir(parents=True)
            (folder / "covey_repo.py").write_text("")
        shim = home.ensure_shim(tool_bin=old)
        self.assertEqual(shim, self.data / "bin" / "covey")
        self.assertIn(f'"{old}/covey_repo.py"', shim.read_text())
        self.assertTrue(shim.stat().st_mode & 0o100)  # Executable (os.access says no on a noexec test directory).
        home.ensure_shim(tool_bin=new)
        self.assertIn(str(old), shim.read_text())  # Its release is there: kept.
        (old / "covey_repo.py").unlink()
        home.ensure_shim(tool_bin=new)
        self.assertIn(f'"{new}/covey_repo.py"', shim.read_text())
        home.ensure_shim(tool_bin=old.parent / "x", force=True)  # A deploy points it at its release.
        self.assertIn("/rel1/x/covey_repo.py", shim.read_text())


class RepoMapTest(Sandbox):
    def test_the_map_is_made_once_from_the_workspaces_and_the_repositories_under_the_home(self):
        core = make_repo(self.home / "core")
        make_repo(self.home / "old")
        make_repo(self.home / "lib")
        make_repo(self.home / "x-data")  # Excluded (*-data/).
        run_git(core, "worktree", "add", str(self.home / "core-wt"), "-b", "w")  # A worktree: not listed.
        (self.home / "plain").mkdir()
        (self.state / "workspace-config.json").write_text(json.dumps({"default": "core", "workspaces": {
            "core": str(core), "old": str(self.home / "old"), "plain": str(self.home / "plain"), "gpu": "gpu1:/srv/x"}}))
        (self.state / "main-thread.json").write_text(json.dumps({"workspace_notes": {"core": "核心", "old": "旧仓库（已冻结）"}},
                                                                ensure_ascii=False))
        self.assertIn("3 repositories (worktree)", repomap.migrate_v1(self.state, self.home))
        repos = {repo.name: repo for repo in repomap.read(self.state)}
        self.assertEqual(sorted(repos), ["core", "lib", "old"])
        self.assertEqual((repos["core"].note, repos["core"].status, repos["core"].added_by), ("核心", "active", "migrate"))
        self.assertEqual(repos["old"].status, "frozen")
        self.assertEqual((repos["lib"].added_by, repos["lib"].write_mode), ("discover", "worktree"))
        rendered = (self.state / "repos.md").read_text()  # A taken-over home: the map beside the state, not in the home.
        self.assertIn("| core |", rendered)
        self.assertFalse((self.home / "repos.md").exists())
        self.assertIn("already", repomap.migrate_v1(self.state, self.home))

    def test_a_home_covey_made_writes_in_place_and_has_its_map_in_it(self):
        fresh = self.root / "fresh"
        home.seed_home(fresh)
        make_repo(fresh / "app")
        repomap.migrate_v1(self.state, fresh)
        (repo,) = repomap.read(self.state)
        self.assertEqual(repo.write_mode, "inplace")
        self.assertIn("| app |", (fresh / "repos.md").read_text())

    def test_the_rules_head_is_cut_at_a_line_within_its_bytes(self):
        repo = self.root / "r"
        repo.mkdir()
        (repo / "CLAUDE.md").write_text("第一行\n" + "长" * 2000 + "\n尾巴\n", encoding="utf-8")
        name, text, cut = repomap.rules_head(repo, ("AGENTS.md",), limit=4096)
        self.assertEqual((name, cut), ("CLAUDE.md", True))
        self.assertLessEqual(len(text.encode()), 4096)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(repomap.rules_head(self.root / "none"), ("", "", False))

    def test_a_used_repository_is_taken_in_and_its_last_use_kept(self):
        repomap.write(self.state, [repomap.Repo(name="a", path="/absolute/a")])
        used = [topic_repos.TopicRepo(name="a", path="/absolute/a"), topic_repos.TopicRepo(name="a", path="/absolute/other/a")]
        repomap.note_used(self.state, used, now=100.0)
        repos = repomap.read(self.state)
        self.assertEqual([(r.name, r.path, r.added_by, r.last_used) for r in repos],
                         [("a", "/absolute/a", "owner", 100.0), ("a-1", "/absolute/other/a", "touched", 100.0)])


class MapWriterTest(Sandbox):
    """core/repomap.py ``update`` is the map's one writer (T870): what a newer release wrote kept, a file that cannot be read
    never written over, and a missing one made from ``start`` only when asked."""

    def test_update_keeps_the_files_other_keys_and_the_entries_read_passes_over(self):
        (self.state / "repos.json").write_text(json.dumps({"version": 1, "exclude": ["node_modules/"], "future": {"k": 1}, "repos": [
            {"name": "a", "path": "/absolute/a", "later": True}, {"name": "gpu", "path": "gpu1:/srv/x"}]}))
        repomap.update(self.state, lambda repos: [*repos, repomap.Repo(name="b", path="/absolute/b")])
        raw = json.loads((self.state / "repos.json").read_text())
        self.assertEqual((raw["exclude"], raw["future"]), (["node_modules/"], {"k": 1}))
        self.assertEqual([item["name"] for item in raw["repos"]], ["a", "b", "gpu"])
        self.assertTrue(raw["repos"][0]["later"])

    def test_a_file_that_cannot_be_read_raises_and_stays(self):
        for text, why in (("{oops", "read"), ("[1]", "format")):
            (self.state / "repos.json").write_text(text)
            with self.assertRaises(repomap.Unreadable) as raised:
                repomap.update(self.state, lambda repos: [repomap.Repo(name="b", path="/absolute/b")])
            self.assertEqual((str(raised.exception), (self.state / "repos.json").read_text()), (why, text))
            repomap.note_used(self.state, [topic_repos.TopicRepo(name="b", path="/absolute/b")])  # A turn's settle: no error.
            self.assertEqual((self.state / "repos.json").read_text(), text)

    def test_a_status_or_write_mode_a_newer_release_wrote_is_written_back_as_it_was(self):
        (self.state / "repos.json").write_text(json.dumps({"version": 1, "repos": [
            {"name": "a", "path": "/absolute/a", "status": "archived", "write_mode": "sparse"}]}))
        (repo,) = repomap.read(self.state)
        self.assertEqual((repo.status, repo.write_mode), ("active", "worktree"))  # Read as the defaults here.
        repomap.note_used(self.state, [topic_repos.TopicRepo(name="a", path="/absolute/a")], now=5.0)
        (item,) = json.loads((self.state / "repos.json").read_text())["repos"]
        self.assertEqual((item["status"], item["write_mode"], item["last_used"]), ("archived", "sparse", 5.0))
        repomap.update(self.state, lambda repos: [repomap.replace(repos[0], status="frozen")])
        (item,) = json.loads((self.state / "repos.json").read_text())["repos"]
        self.assertEqual((item["status"], item["write_mode"]), ("frozen", "sparse"))

    def test_a_missing_file_is_made_from_start_only(self):
        repomap.update(self.state, lambda repos: repos)
        self.assertFalse((self.state / "repos.json").exists())
        repomap.update(self.state, lambda repos: repos, start=lambda: [repomap.Repo(name="a", path="/absolute/a")])
        self.assertEqual([repo.name for repo in repomap.read(self.state)], ["a"])


class MigrateStepsTest(Sandbox):
    """W1's migrations in core/migrate.py (T870): home_v1 and repos_v1 run at the Bridge's start (it passes the home), once;
    maintenance --apply, without the home, leaves them pending; a step that raised runs again; an older release's marks and
    files are left as they were."""

    def migrate(self, **kwargs):
        from covey.core import migrate
        return migrate.migrate(self.state, **kwargs)

    def test_the_bridges_start_writes_home_json_and_the_map_once(self):
        from covey.core import migrate
        seeded = self.root / "seeded"
        home.seed_home(seeded)
        make_repo(seeded / "app")
        self.assertFalse({"home_v1", "repos_v1"} & set(self.migrate()))  # Maintenance --apply: no home, the home's steps wait.
        self.assertFalse(home.configured(self.state) or (self.state / "repos.json").exists())
        self.assertFalse({"home_v1", "repos_v1"} & set(migrate.pending(self.state)))  # Not maintenance's to list.
        summary = self.migrate(workdir=str(seeded))
        self.assertEqual((summary["home_v1"], summary["repos_v1"]), ({"written": 1, "failed": 0}, {"written": 1, "failed": 0}))
        self.assertTrue(home.configured(self.state))
        self.assertEqual([(repo.name, repo.write_mode) for repo in repomap.read(self.state)], [("app", "inplace")])
        self.assertIn("| app |", (seeded / "repos.md").read_text())
        marks = json.loads((self.state / migrate.MIGRATIONS).read_text())
        self.assertTrue(marks["home_v1"]["done"] and marks["repos_v1"]["done"])
        before = (self.state / "repos.json").read_text()
        self.assertEqual(self.migrate(workdir=str(seeded)), {})  # Once: one read of migrations.json from then on.
        self.assertEqual((self.state / "repos.json").read_text(), before)

    def test_a_taken_over_home_gets_no_home_json_and_a_map_already_there_is_kept(self):
        make_repo(self.home / "core")
        repomap.write(self.state, [repomap.Repo(name="mine", path="/absolute/mine", note="主人写的")])
        before = (self.state / "repos.json").read_text()
        summary = self.migrate(workdir=str(self.home))
        self.assertEqual((summary["home_v1"]["written"], summary["repos_v1"]["written"]), (0, 0))
        self.assertFalse(home.configured(self.state))  # Its own AGENTS.md says where outputs go.
        self.assertEqual((self.state / "repos.json").read_text(), before)  # A map an earlier run (or the owner) made stays.
        self.assertIn("| mine |", (self.state / "repos.md").read_text())

    def test_home_repos_and_memory_steps_together_once_and_again_after_an_older_release_wrote(self):
        """T870 Wave 2B: home_v1, repos_v1 and M1's memory_v2 in one start; the next start runs none; an older release's
        principle written after a rollback is taken by memory_v2 alone (RECURRING), the home's two stay done and untouched."""
        from covey.core import memstore
        seeded = self.root / "seeded"
        home.seed_home(seeded)
        make_repo(seeded / "app")
        from covey.core import principles
        principles.save(self.state, "owner", "回复先给结论", now=1.0)  # As every release writes principles.json.
        first = self.migrate(workdir=str(seeded))
        self.assertTrue({"home_v1", "repos_v1", "memory_v2"} <= set(first), first)
        self.assertEqual([e.text for e in memstore.entries(self.state, scope="global")], ["回复先给结论"])
        files = {name: (self.state / name).read_text() for name in ("home.json", "repos.json")}
        self.assertEqual(self.migrate(workdir=str(seeded)), {})
        principles.save(self.state, "owner", "不在主目录根递归搜索", now=2.0)  # An older release's, after a rollback.
        again = self.migrate(workdir=str(seeded))
        self.assertEqual(set(again), {"memory_v2"})
        self.assertEqual(sorted(e.text for e in memstore.entries(self.state, scope="global")), ["不在主目录根递归搜索", "回复先给结论"])
        self.assertEqual({name: (self.state / name).read_text() for name in files}, files)
        self.assertEqual(self.migrate(workdir=str(seeded)), {})

    def test_a_step_that_raised_runs_again_and_an_older_releases_marks_stay(self):
        from covey.core import migrate
        (self.state / migrate.MIGRATIONS).write_text(json.dumps({"older-step": {"done": True, "at": 1}}))
        with mock.patch.object(repomap, "migrate_v1", side_effect=OSError("disk")):
            summary = self.migrate(workdir=str(self.home))
        self.assertEqual(summary["repos_v1"], {"written": 0, "failed": 1})
        self.assertNotIn("repos_v1", migrate.done(self.state))
        self.assertEqual(self.migrate(workdir=str(self.home))["repos_v1"]["failed"], 0)
        marks = json.loads((self.state / migrate.MIGRATIONS).read_text())
        self.assertEqual(marks["older-step"], {"done": True, "at": 1})  # A mark this release does not know is kept as it was.
        self.assertTrue(marks["repos_v1"]["done"])

class CommandTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home / "core")
        repomap.write(self.state, [repomap.Repo(name="core", path=str(self.repo), note="核心")])
        self.topic()
        self.task = self.state / "runs" / "om_1"
        self.task.mkdir(parents=True)
        os.environ.update(COVEY_STATE=str(self.state), COVEY_HOME=str(self.home), COVEY_TOPIC=TOPIC,
                          **{TASK_DIR_ENV: str(self.task)})

    def run_main(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = repomap.main(list(argv))
        return code, out.getvalue()

    def test_use_prints_the_rules_and_writes_only_the_tasks_request(self):
        code, out = self.run_main("use", "core")
        self.assertEqual(code, 0)
        self.assertIn("只在 worktree 里改", out)
        (request,) = topic_repos.requests(self.task)
        self.assertEqual((request["name"], request["mode"], request["rules_file"]), ("core", "read", "AGENTS.md"))
        self.assertNotIn("repos", self.topic_data())  # The topic is the Bridge's to write, when the turn ends.

    def test_use_write_makes_the_topics_worktree_once_from_the_main_branch(self):
        code, out = self.run_main("--json", "use", "core", "--write")
        self.assertEqual(code, 0, out)
        made = json.loads(out)
        expect = self.data / "worktrees" / "T7-abcdef12" / "core"
        self.assertEqual((made["worktree"], made["branch"], made["created"]), (str(expect), "topic/T7-abcdef12", True))
        self.assertEqual(made["base_oid"], run_git(self.repo, "rev-parse", "main").strip())
        self.assertEqual((expect / "app.py").read_text(), "print('old')\n")
        self.assertIn("只在 worktree 里改", made["rules"]["text"])
        code, again = self.run_main("use", "core", "--write", "--json")
        self.assertEqual((code, json.loads(again)["created"], json.loads(again)["worktree"]), (0, False, str(expect)))
        self.assertEqual([r["mode"] for r in topic_repos.requests(self.task)], ["write", "write"])

    def test_a_frozen_or_readonly_repository_is_never_written(self):
        repomap.write(self.state, [repomap.Repo(name="core", path=str(self.repo), status="frozen")])
        code, out = self.run_main("use", "core", "--write")
        self.assertEqual(code, 3)
        self.assertIn("已冻结", out)
        self.assertFalse((self.data / "worktrees").exists())
        self.assertEqual(topic_repos.requests(self.task), [])
        self.assertEqual(self.run_main("use", "core")[0], 0)  # Reading it is fine.

    def test_an_inplace_repository_is_written_where_it_is(self):
        repomap.write(self.state, [repomap.Repo(name="core", path=str(self.repo), write_mode="inplace")])
        code, out = self.run_main("--json", "use", "core", "--write")
        made = json.loads(out)
        self.assertEqual((code, made["worktree"], made["branch"]), (0, str(self.repo), ""))
        self.assertFalse((self.data / "worktrees").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes through a read-only directory")
    def test_a_worktree_add_that_fails_takes_back_the_branch_it_made(self):
        folder = self.data / "worktrees" / "T7-abcdef12"
        folder.mkdir(parents=True)
        (folder / ".core.lock").write_text("")
        folder.chmod(0o555)  # git makes the branch, then cannot make the worktree's directory.
        self.addCleanup(folder.chmod, 0o755)
        code, out = self.run_main("--json", "use", "core", "--write")
        self.assertEqual(code, 0, out)
        made = json.loads(out)
        self.assertEqual(made["worktree"], "")
        self.assertIn("下一轮", made["note"])
        self.assertEqual(run_git(self.repo, "branch", "--list", "topic/*").strip(), "")
        self.assertTrue(topic_repos.requests(self.task)[0]["pending"])

    def test_an_unknown_name_says_what_the_map_has(self):
        code, out = self.run_main("use", "nope")
        self.assertEqual(code, 2)
        self.assertIn("core", out)

    def test_status_list_and_a_prune_plan_that_runs_nothing(self):
        self.run_main("use", "core", "--write")
        tree = self.data / "worktrees" / "T7-abcdef12" / "core"
        (tree / "new.py").write_text("x\n")
        code, out = self.run_main("status", "--json")
        (row,) = json.loads(out)
        self.assertEqual((code, row["mode"], row["worktree"], row["changes"]), (0, "write", str(tree), 1))
        code, out = self.run_main("list")
        self.assertIn("core\t可用", out)
        (tree / "new.py").unlink()
        old = time.time() - 3 * 86400
        for path in (tree, tree / ".git"):
            os.utime(path, (old, old))
        code, out = self.run_main("prune-plan", "--json")
        (row,) = json.loads(out)
        self.assertTrue(row["prunable"], row)
        self.assertEqual(row["commands"][0], f"git -C {self.repo} worktree remove {tree}")
        self.assertTrue(tree.is_dir())  # Only listed.
        legacy = self.root / "legacy"
        legacy.mkdir()
        run_git(self.repo, "worktree", "add", str(legacy / "core-old"), "-b", "old")
        (legacy / "core-old" / "dirty.txt").write_text("x")
        rows = json.loads(self.run_main("prune-plan", "--legacy", str(legacy), "--json")[1])
        (left,) = [r for r in rows if r["legacy"]]
        self.assertEqual((left["prunable"], left["changes"]), (False, 1))


class TouchedTest(Sandbox):
    def test_a_turns_steps_say_which_repositories_were_read_written_and_whose_rules_were_read(self):
        core = make_repo(self.home / "core")
        lib = make_repo(self.home / "lib")
        other = make_repo(self.home / "other")
        tree = self.data / "worktrees" / "T7-abcdef12" / "core"
        run_git(core, "worktree", "add", str(tree), "-b", "topic/T7-abcdef12")
        make_repo(self.data / "inside")  # Under the data root: never taken in.
        repos = [repomap.Repo(name="core", path=str(core)), repomap.Repo(name="lib", path=str(lib))]
        trace = trace_of(("读取文件", str(core / "AGENTS.md")), ("修改文件", f"{tree / 'app.py'}, {tree / 'b.py'}"),
                         ("执行命令", f"提交 · cd {lib} && git commit -am x"), ("执行命令", "cat other/README"),
                         ("执行命令", f"ls {self.data / 'inside'}"), ("搜索文件", "core"))
        found = {(r.name, r.worktree): r for r in topic_repos.touched(trace, repos, workdir=self.home, home=self.home,
                                                                       skip=(self.data,), now=5.0)}
        self.assertEqual(sorted(found), [("core", ""), ("core", str(tree)), ("lib", ""), ("other", "")])
        self.assertEqual((found[("core", "")].mode, found[("core", "")].rules_read_at), ("read", 5.0))
        self.assertEqual((found[("core", str(tree))].mode, found[("core", str(tree))].path), ("write", str(core)))
        self.assertEqual(found[("lib", "")].mode, "write")
        self.assertEqual((found[("other", "")].path, found[("other", "")].source), (str(other), "touched"))

    def test_a_quoted_comparison_is_no_write_and_a_linked_repository_is_found_by_its_real_path(self):
        core = make_repo(self.home / "core")
        (self.root / "link").symlink_to(core)
        repos = [repomap.Repo(name="core", path=str(self.root / "link"))]
        tree = self.data / "worktrees" / "T7-abcdef12" / "core"
        run_git(core, "worktree", "add", str(tree), "-b", "topic/T7-abcdef12")
        trace = trace_of(("执行命令", f"cd {core} && python -c \"if 2 > 1: print(1)\" && awk '$1 > 5' app.py"),
                         ("修改文件", str(tree / "app.py")))
        found = {(r.name, r.worktree): r.mode for r in topic_repos.touched(trace, repos, workdir=self.home, home=self.home)}
        self.assertEqual(found, {("core", ""): "read", ("core", str(tree)): "write"})

    def test_a_codex_turn_may_write_the_run_directory_the_topics_worktrees_and_its_repositories(self):
        core, lib = make_repo(self.home / "core"), make_repo(self.home / "lib")
        data = {"repos": [{"name": "core", "path": str(core), "mode": "write", "worktree": "/absolute/wt/core"},
                          {"name": "lib", "path": str(lib), "mode": "write", "worktree": str(lib)},
                          {"name": "ro", "path": "/absolute/ro", "mode": "read"},
                          {"name": "old", "path": "/absolute/old", "mode": "write"}]}
        folder = self.data / "worktrees" / "T7-abcdef12"
        roots = topic_repos.writable_roots(data, task_dir="/absolute/runs/om_1", worktrees=folder, frozen=["/absolute/old"])
        self.assertEqual(roots, ["/absolute/runs/om_1", str(folder), str(core / ".git"), "/absolute/wt/core",
                                 str(lib / ".git"), str(lib)])
        self.assertTrue(folder.is_dir())  # Made before the turn: a worktree made in it is writable from the first turn.
        pending = {"repos": [{"name": "core", "path": str(core), "mode": "write"}]}  # Its worktree not made yet.
        self.assertEqual(topic_repos.writable_roots(pending), [str(core / ".git")])  # Never its main checkout.
        self.assertEqual(topic_repos.writable_roots(pending, inplace=[str(core)]), [str(core / ".git"), str(core)])


class ReposTurnTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home / "core")
        repomap.write(self.state, [repomap.Repo(name="core", path=str(self.repo))])
        self.job = SimpleNamespace(topic_id=TOPIC, job_id=3, message_id="om_3", tool_trace=None)

    def test_a_declared_repositorys_rules_are_inlined_until_a_turn_has_seen_them(self):
        self.topic(home_start=True, repos=[{"name": "core", "path": str(self.repo), "mode": "write", "source": "declared"}])
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("本话题用到的仓库：\n- core（写，worktree 还没建：先 covey repo use core --write", block)
        self.assertIn("规范 AGENTS.md 未读", block)
        self.assertIn("core 的规范（AGENTS.md 开头；这个仓库是派活时点名的，先照它做）：\n<<<\n# 规范", block)
        repos_turn.settle(self.ctx(), self.job, None)
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("规范 AGENTS.md ✓已读", block)
        self.assertNotIn("<<<", block)

    def test_a_write_without_the_rules_inlines_them_next_turn_and_a_main_checkout_write_is_said(self):
        self.topic(home_start=True)
        self.job.tool_trace = trace_of(("修改文件", str(self.repo / "app.py")))
        repos_turn.settle(self.ctx(), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["mode"], entry["source"], entry["rules_miss"], entry[repos_turn.CHECKOUT_WRITES]),
                         ("write", "touched", 1, 1))
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("上一轮改到了 core 的主检出", block)
        self.assertIn("上一轮改了它却没读它的规范", block)
        self.assertIn("- core（写，worktree 还没建：先 covey repo use core --write", block)
        self.job.tool_trace = trace_of(("读取文件", str(self.repo / "AGENTS.md")))
        repos_turn.settle(self.ctx(), self.job, None)
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertNotIn("主检出", block)
        self.assertNotIn("<<<", block)
        self.assertEqual(repomap.read(self.state)[0].last_used > 0, True)

    def test_the_map_is_noted_later_and_a_turn_that_changes_nothing_takes_no_lock(self):
        """T870 Wave 3.5 (Fable P1-12): the end of every turn read topic.json under LOCK and took the map's file lock before
        its answer went out. A turn that changes nothing of the topic's repos takes no lock; the map is noted after."""
        import threading
        from covey.core import topics as core_topics
        self.topic(home_start=True)
        self.job.tool_trace = trace_of(("读取文件", str(self.repo / "AGENTS.md")))
        note = repos_turn.settle(self.ctx(), self.job, None, later=True)
        self.assertTrue(callable(note))
        self.assertEqual(repomap.read(self.state)[0].last_used, 0.0)  # Not yet: after the answer.
        note()
        self.assertGreater(repomap.read(self.state)[0].last_used, 0)
        held, done = threading.Event(), threading.Event()

        def hold():
            with core_topics.LOCK:
                held.set()
                done.wait(5)
        other = threading.Thread(target=hold)
        other.start()
        self.addCleanup(other.join)
        held.wait(5)
        try:
            started = time.monotonic()
            core_topics.update_repos(self.state, TOPIC, lambda entries: entries)  # Nothing changes: no LOCK.
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            done.set()

    def test_an_old_topics_repository_written_without_its_rules_is_counted_once(self):
        """T870 Wave 3.5 review: update_repos may call ``change`` twice (read without LOCK, then under it); the old topic's
        own repository was the same dict both times, its rules_miss counted twice."""
        self.topic(workdir=str(self.repo))  # From before the home: no repos yet, its workdir the repository.
        self.job.tool_trace = trace_of(("修改文件", str(self.repo / "app.py")))
        repos_turn.settle(self.ctx(workdir=self.repo), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["name"], entry["mode"], entry.get("rules_miss", 0)), ("core", "write", 0))

    def test_a_head_cut_short_is_never_read_and_a_write_without_the_rules_brings_it_again(self):
        """T900 W0-4: rules inlined only in part (their first RULES_INLINE_BYTES of 89 KB) counted as read: the row said
        ✓已读 and a turn that then wrote the repository without reading them got no rules_miss."""
        (self.repo / "AGENTS.md").write_text("# 规范\n" + "".join(f"第 {i} 条：只在 worktree 里改。\n" for i in range(400)))
        self.topic(home_start=True, repos=[{"name": "core", "path": str(self.repo), "mode": "write", "source": "declared"}])
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("core 的规范（AGENTS.md 开头，后面还有，动手前读全文；这个仓库是派活时点名的，先照它做）", block)
        repos_turn.settle(self.ctx(), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertNotIn("rules_read_at", entry)
        self.assertGreater(entry["rules_inlined_at"], 0)
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("规范 AGENTS.md 未读，动它之前先读", block)
        self.assertNotIn("<<<", block)  # Shown once: not the same 4 KB every turn.
        self.job.tool_trace = trace_of(("修改文件", str(self.repo / "app.py")))  # Written without reading them.
        repos_turn.settle(self.ctx(), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["rules_miss"], "rules_inlined_at" in entry), (1, False))
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("上一轮改了它却没读它的规范", block)
        self.assertIn("<<<\n# 规范", block)
        self.job.tool_trace = trace_of(("读取文件", str(self.repo / "AGENTS.md")))
        repos_turn.settle(self.ctx(), self.job, None)
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn("规范 AGENTS.md ✓已读", block)
        self.assertNotIn("<<<", block)

    def test_covey_repo_use_printing_a_head_cut_short_is_no_reading(self):
        """T900 W0-4: ``covey repo use`` prints at most 4 KB of the rules; a request saying so (``rules_cut``) leaves them
        unread. One of an older release (no ``rules_cut``) counts as read, as before."""
        self.topic(home_start=True)
        task = self.state / "runs" / "om_3"
        topic_repos.add_request(task, {"at": 9.0, "name": "core", "path": str(self.repo), "mode": "read",
                                       "rules_file": "AGENTS.md", "rules_cut": True})
        repos_turn.settle(self.ctx(), self.job, task)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry.get("rules_read_at"), entry["rules_inlined_at"]), (None, 9.0))
        self.assertIn("规范 AGENTS.md 未读", repos_turn.tail(self.ctx(), self.job))
        self.job.tool_trace = trace_of(("修改文件", str(self.repo / "app.py")))
        repos_turn.settle(self.ctx(), self.job, None)
        self.assertIn("上一轮改了它却没读它的规范", repos_turn.tail(self.ctx(), self.job))

    def test_the_tasks_requests_are_folded_in_with_their_worktree_and_base(self):
        self.topic(home_start=True)
        task = self.state / "runs" / "om_3"
        topic_repos.add_request(task, {"at": 9.0, "name": "core", "path": str(self.repo), "mode": "write",
                                       "worktree": "/absolute/wt/core", "branch": "topic/T7-abcdef12", "base": "origin/main@1234",
                                       "base_oid": "1" * 40, "rules_file": "AGENTS.md"})
        repos_turn.settle(self.ctx(), self.job, task)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual({k: entry[k] for k in ("mode", "worktree", "branch", "base_oid", "source", "rules_read_at")},
                         {"mode": "write", "worktree": "/absolute/wt/core", "branch": "topic/T7-abcdef12",
                          "base_oid": "1" * 40, "source": "cli", "rules_read_at": 9.0})
        self.assertIn("- core（写，worktree /absolute/wt/core，分支 topic/T7-abcdef12；规范 AGENTS.md", repos_turn.tail(self.ctx(), self.job))
        self.assertEqual(repos_turn.write_dirs(self.ctx(), self.job), [("core", "/absolute/wt/core")])

    def test_another_topics_worktree_is_never_taken_for_this_topics(self):
        self.topic(home_start=True)
        theirs = self.data / "worktrees" / "T12-ffffffff" / "core"
        run_git(self.repo, "worktree", "add", str(theirs), "-b", "topic/T12-ffffffff")
        self.job.tool_trace = trace_of(("读取文件", str(theirs / "app.py")))
        repos_turn.settle(self.ctx(), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertNotIn("worktree", entry)
        self.assertEqual(repos_turn.write_dirs(self.ctx(), self.job), [])

    def test_a_topic_from_before_the_home_gets_its_workdirs_repository_and_no_block(self):
        self.topic(workdir=str(self.repo))
        repos_turn.settle(self.ctx(workdir=self.repo), self.job, None)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["name"], entry["mode"], bool(entry["rules_read_at"])), ("core", "read", True))
        self.assertEqual(repos_turn.tail(self.ctx(workdir=self.repo), self.job), "")
        self.job.tool_trace = trace_of(("修改文件", str(self.repo / "app.py")))
        repos_turn.settle(self.ctx(workdir=self.repo), self.job, None)
        self.assertNotIn(repos_turn.CHECKOUT_WRITES, self.topic_data()["repos"][0])  # Its own checkout: its way of working.

    def test_the_bridge_tool_lists_the_map_and_what_the_topic_uses(self):
        self.topic(home_start=True, repos=[{"name": "core", "path": str(self.repo), "mode": "write", "worktree": "/absolute/wt"},
                                            {"name": "x", "path": "/absolute/x", "mode": "read"}])
        found = repos_turn.repos_list(self.ctx())
        self.assertEqual(found["home"], str(self.home))
        self.assertEqual(found["repos"][0]["used"], "write")
        self.assertEqual(found["unmapped"], [{"name": "x", "path": "/absolute/x", "used": "read", "worktree": ""}])


class ReadWorktreeTest(Sandbox):
    """T900 W0-5: ``covey repo use`` without ``--write`` read the repository's main checkout, 87 commits behind; it reads a
    detached worktree of the latest main branch now, says which commit and when, and its rules come from it."""

    def setUp(self):
        super().setUp()
        self.upstream = make_repo(self.root / "upstream", rules="# 规范\n旧规范。\n")
        run_git(self.root, "clone", "-q", str(self.upstream), str(self.home / "core"))
        self.repo = self.home / "core"
        (self.upstream / "AGENTS.md").write_text("# 规范\n新规范：只读最新主干。\n", encoding="utf-8")
        commit_all(self.upstream, "rules")  # The upstream moves on: the main checkout is behind.
        self.latest = run_git(self.upstream, "rev-parse", "main").strip()
        repomap.write(self.state, [repomap.Repo(name="core", path=str(self.repo))])
        self.topic(home_start=True)
        self.task = self.state / "runs" / "om_1"
        self.task.mkdir(parents=True)
        os.environ.update(COVEY_STATE=str(self.state), COVEY_HOME=str(self.home), COVEY_TOPIC=TOPIC,
                          **{TASK_DIR_ENV: str(self.task)})
        self.job = SimpleNamespace(topic_id=TOPIC, job_id=3, message_id="om_1", tool_trace=None)

    def use(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = repomap.main(["use", "core", *argv])
        return code, out.getvalue()

    def test_reading_is_in_the_latest_main_branch_with_its_commit_and_time_and_its_rules(self):
        self.assertNotEqual(run_git(self.repo, "rev-parse", "HEAD").strip(), self.latest)
        code, out = self.use("--json")
        made = json.loads(out)
        tree = self.data / "worktrees" / "_read" / "core" / self.latest[:12]
        self.assertEqual((code, made["worktree"], made["base_oid"], made["base"]),
                         (0, str(tree), self.latest, f"origin/main@{self.latest[:8]}"))
        self.assertEqual(run_git(tree, "rev-parse", "HEAD").strip(), self.latest)
        self.assertEqual(run_git(self.repo, "rev-parse", "origin/main").strip(), self.latest)  # Fetched.
        self.assertIn("新规范", made["rules"]["text"])  # The rules of the latest main branch, not the checkout's.
        self.assertEqual((made["note"], made["stale"]), ("", 0))
        code, text = self.use()
        at = repomap.when(made["base_at"])
        self.assertTrue(at)
        self.assertIn(f"在这里读：{tree}（最新主干的只读 worktree，origin/main 的提交 {self.latest[:8]}，提交于 {at}", text)
        self.assertNotIn("可能落后", text)
        self.assertEqual(json.loads(self.use("--json")[1])["created"], False)  # The same commit: used again.
        repos_turn.settle(self.ctx(), self.job, self.task)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["mode"], entry["worktree"], entry["base_oid"]), ("read", str(tree), self.latest))
        block = repos_turn.tail(self.ctx(), self.job)
        self.assertIn(f"- core（读，最新主干的只读 worktree {tree}，origin/main 的提交 {self.latest[:8]}，提交于 {at}；规范 AGENTS.md", block)
        self.assertEqual(repos_turn._rules(topic_repos.of_topic(self.topic_data())[0], {}, 4096)[1],
                         "# 规范\n新规范：只读最新主干。\n")
        # Writing it later is in the topic's own worktree, never the shared read one.
        made = json.loads(self.use("--write", "--json")[1])
        self.assertEqual(made["worktree"], str(self.data / "worktrees" / "T7-abcdef12" / "core"))
        repos_turn.settle(self.ctx(), self.job, self.task)
        self.use("--json")  # A read after it keeps the topic's own worktree.
        repos_turn.settle(self.ctx(), self.job, self.task)
        (entry,) = self.topic_data()["repos"]
        self.assertEqual((entry["mode"], entry["worktree"]), ("write", made["worktree"]))
        self.assertNotIn("base_at", entry)

    def test_a_fetch_that_fails_says_it_may_be_behind(self):
        run_git(self.repo, "fetch", "-q", "origin")
        run_git(self.repo, "remote", "set-url", "origin", str(self.root / "gone.git"))
        (self.upstream / "app.py").write_text("print('newer')\n")
        commit_all(self.upstream, "newer")  # Not fetched: the remote branch as last fetched is a commit behind.
        code, out = self.use("--json")
        made = json.loads(out)
        self.assertEqual((code, made["base_oid"], made["stale"]), (0, self.latest, 1))
        self.assertIn("git fetch 没成功", made["note"])
        self.assertIn("可能落后，未能确认是最新主干", made["note"])
        self.assertNotIn(str(self.root / "gone.git"), made["note"])  # The exit status only, not git's words.
        repos_turn.settle(self.ctx(), self.job, self.task)
        self.assertIn("当时 fetch 没成功，可能落后", repos_turn.tail(self.ctx(), self.job))

    def test_without_a_read_worktree_it_reads_the_checkout_and_says_so(self):
        with mock.patch.object(repomap, "read_worktree", side_effect=repomap.RepoError("建不了", later=True)):
            code, out = self.use("--json")
        made = json.loads(out)
        self.assertEqual((code, made["worktree"], made["stale"]), (0, "", 1))
        self.assertIn("读的是仓库目录本身，可能落后，未能确认是最新主干", made["note"])
        self.assertIn("旧规范", made["rules"]["text"])
        self.assertFalse(topic_repos.requests(self.task)[0]["pending"])  # A read is never pending a write.
        # A repository without a remote has no main branch to be behind of: read where it is, as before.
        run_git(self.repo, "remote", "remove", "origin")
        made = json.loads(self.use("--json")[1])
        self.assertEqual((made["worktree"], made["note"]), ("", ""))

    def test_read_worktrees_no_turn_used_for_a_day_are_removed_and_a_turn_keeps_its_own(self):
        first = json.loads(self.use("--json")[1])["worktree"]
        repos_turn.settle(self.ctx(), self.job, self.task)
        old = time.time() - repomap.READ_IDLE - 60
        marker = Path(first + repomap.READ_USED)
        os.utime(marker, (old, old))
        note = repos_turn.settle(self.ctx(), self.job, None, later=True)  # A turn of the topic reading it keeps it.
        note()
        self.assertGreater(marker.stat().st_mtime, old + 60)
        os.utime(marker, (old, old))
        (self.upstream / "app.py").write_text("print('next')\n")
        commit_all(self.upstream, "next")
        second = json.loads(self.use("--json")[1])["worktree"]
        self.assertNotEqual(second, first)
        self.assertFalse(Path(first).exists())  # Clean and idle a day: removed with the commit's marker.
        self.assertFalse(marker.exists())
        self.assertNotIn(first, run_git(self.repo, "worktree", "list"))
        self.assertIn("之前的只读 worktree 已清理", repos_turn.tail(self.ctx(), self.job))
        # One with changes stays, however idle.
        (Path(second) / "scratch.txt").write_text("x")
        os.utime(Path(second + repomap.READ_USED), (old, old))
        (self.upstream / "app.py").write_text("print('last')\n")
        commit_all(self.upstream, "last")
        self.use("--json")
        self.assertTrue(Path(second).is_dir())


class HomeBlockTest(Sandbox):
    def test_a_home_topic_has_the_home_block_and_an_older_one_none(self):
        self.topic()
        self.assertEqual(home_turn.static_block(self.ctx()), "")
        self.topic(home_start=True, started_at=time.mktime((2026, 11, 27, 3, 0, 0, 0, 0, -1)), slug="fix-diff")
        block = home_turn.static_block(self.ctx())
        self.assertIn(f"主目录：{self.home}", block)
        self.assertIn(f"仓库地图：{self.state / 'repos.md'}", block)
        self.assertIn("covey repo use <仓库> --write", block)
        self.assertNotIn("产物目录", block)  # No home.json: the taken-over home's AGENTS.md says where outputs go.
        self.assertEqual(block, home_turn.static_block(self.ctx()))  # The same every turn: one cacheable prefix.
        home.write(self.state, home.HomeConfig(cache_env=True))
        block = home_turn.static_block(self.ctx())
        self.assertIn(f"本话题产物目录：{self.home}/tasks/runs/20261127-T7-fix-diff", block)
        self.assertEqual(home_turn.static_block(self.ctx(device="gpu1")), "")
        environment = home_turn.env(self.ctx())
        self.assertTrue(environment["TMPDIR"].startswith(f"{self.home}/tasks/runs/20261127-T7-fix-diff/"))

    def test_the_task_runner_puts_the_home_in_its_environment(self):
        runner.export_home(self.ctx())
        self.assertEqual((os.environ["COVEY_HOME"], os.environ["COVEY_TOPIC"], os.environ["COVEY_STATE"]),
                         (str(self.home), TOPIC, str(self.state)))
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(self.data / "bin"))


class MultiRepoDiffTest(Sandbox):
    def test_a_worktree_made_in_the_turn_has_its_diff_from_the_commit_it_started_from(self):
        repo = make_repo(self.home / "core")
        job = SimpleNamespace(job_id=4, message_id="om_4", tool_trace=None)
        snapshot = capture_job_diff_snapshot(self.home)  # The home is no git repository.
        tree = self.data / "worktrees" / "T7-abcdef12" / "core"
        base = run_git(repo, "rev-parse", "HEAD").strip()
        run_git(repo, "worktree", "add", str(tree), "-b", "topic/T7-abcdef12", base)
        topic_repos.add_request(self.state / "runs" / "om_4", {"name": "core", "path": str(repo), "mode": "write",
                                                               "worktree": str(tree), "base_oid": base, "created": True})
        (tree / "app.py").write_text("print('new')\n")
        commit_all(tree, "change")
        (tree / "extra.py").write_text("x = 1\n")
        result = finalize_job_diff(snapshot, self.state, job)
        self.assertEqual([(f.path, f.status) for f in result.files], [("core/app.py", "M"), ("core/extra.py", "A")])
        self.assertIn("+print('new')", result.files[0].diff_text)
        again = self.state / "runs" / "om_9"  # Used again, not made in the turn: what it held before is not this turn's.
        topic_repos.add_request(again, {"name": "core", "path": str(repo), "mode": "write", "worktree": str(tree),
                                        "base_oid": base, "created": False})
        self.assertEqual(finalize_job_diff(snapshot, self.state, SimpleNamespace(job_id=9, message_id="om_9", tool_trace=None)).files, [])

    def test_a_repository_written_without_a_base_lists_its_files_by_name_only(self):
        repo = make_repo(self.home / "lib")
        job = SimpleNamespace(job_id=5, message_id="om_5", tool_trace=ToolTrace.restore(trace_of(("修改文件", str(repo / "app.py")))))
        snapshot = capture_job_diff_snapshot(self.home)
        (repo / "app.py").write_text("print('new')\n")
        result = finalize_job_diff(snapshot, self.state, job)
        self.assertEqual([(f.path, f.hidden_reason, f.diff_text) for f in result.files], [("lib/app.py", NO_BASE, "")])

    def test_a_commit_any_repository_of_the_turn_has_is_not_missing(self):
        one, two = make_repo(self.home / "one"), make_repo(self.home / "two")
        head = run_git(two, "rev-parse", "HEAD").strip()
        self.assertEqual(missing_in_repos([one, two], [head, "f" * 40]), ["f" * 40])
        self.assertIsNone(missing_in_repos([self.home / "none"], [head]))

    def test_the_topics_other_repositories_are_snapshotted_before_the_turn(self):
        main, other = make_repo(self.home / "main"), make_repo(self.home / "other")
        job = SimpleNamespace(job_id=6, message_id="om_6", tool_trace=None)
        snapshot = capture_job_diff_snapshot(main, repos=[("other", str(other)), ("main-again", str(main))])
        self.assertEqual([part.label for part in snapshot.others], ["other"])
        (main / "app.py").write_text("print('a')\n")
        (other / "app.py").write_text("print('b')\n")
        result = finalize_job_diff(snapshot, self.state, job)
        self.assertEqual([f.path for f in result.files], ["app.py", "other/app.py"])


class CodexRootsTest(Sandbox):
    def params(self, sandbox):
        ctx = self.ctx(options={"sandbox": sandbox})
        return appserver.thread_params(ctx, None)[0]["config"]["sandbox_workspace_write.writable_roots"]

    def test_workspace_write_gets_the_repositories_roots_and_full_access_does_not_need_them(self):
        repo = make_repo(self.home / "core")
        self.topic(home_start=True, repos=[{"name": "core", "path": str(repo), "mode": "write",
                                            "worktree": str(self.data / "worktrees" / "T7-abcdef12" / "core")}])
        repomap.write(self.state, [repomap.Repo(name="core", path=str(repo))])
        os.environ[TASK_DIR_ENV] = str(self.state / "runs" / "om_1")
        folder = self.data / "worktrees" / "T7-abcdef12"
        self.assertEqual(self.params("workspace-write"), [str(self.state / "checkpoints"), str(self.state / "runs" / "om_1"),
                                                          str(folder), str(repo / ".git")])
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.params("danger-full-access"), [str(self.state / "checkpoints")])
        self.topic()  # A topic from before the home, no repositories: no worktrees directory made for it.
        os.rmdir(folder)
        self.assertEqual(self.params("workspace-write"), [str(self.state / "checkpoints"), str(self.state / "runs" / "om_1")])
        self.assertFalse(folder.exists())
        repomap.write(self.state, [repomap.Repo(name="core", path=str(repo), status="frozen")])
        self.assertNotIn(str(repo / ".git"), self.params("workspace-write"))


if __name__ == "__main__":
    unittest.main()

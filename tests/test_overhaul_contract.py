"""The overhaul's frozen contract (A0, FINAL-SPEC): the web's v43 memory keys on both sides and their golden samples, the
absolute similarity and the ranking, the new store's short codes, and the prompt's hooks that change nothing until a
package fills them (a turn's rules and message as before; a resumed session's turn records the memory it carries)."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import UNSET_OFF, make_context, quiet
from support.fake_backends import FakeBackend
from support.world import harness_app
from covey.app import home_turn, memory_turn, question_dedup, repos_turn, web_memory
from covey.app.prompts import turn_tail
from covey.backends.claude.bridge import CLAUDE_SPEC
from covey.backends.claude.sessions import ClaudeSessionStore
from covey.core import home, memory_rank, memstore, repomap, run_dir, topic_repos, web_sync
from covey.core.jobs import BridgeReplyJob as ReplyJob

REPO = Path(__file__).resolve().parents[1]
API = REPO / "web" / "shared" / "api.interface.ts"
GOLDEN = REPO / "tests" / "golden" / "web_memory_v43.json"
ENTRY_FIELDS = {"key", "code", "kind", "scope", "title", "text", "status", "said", "by", "origin", "rev"}


def ts_array(name: str) -> list[str]:
    found = re.search(rf"export const {name} = \[(.*?)\] as const;", API.read_text(encoding="utf-8"), re.S)
    return re.findall(r"'([^']+)'", found.group(1))


class WebContractTest(unittest.TestCase):
    def test_both_sides_have_the_seven_keys_and_the_same_memory_key_pattern(self):
        self.assertEqual(ts_array("SETTINGS_KEYS"), list(web_sync.SETTINGS_KEYS))
        self.assertIn("quota", web_sync.SETTINGS_KEYS)
        literal = re.search(r"export const MEMORY_KEY_RE = /(.*)/;", API.read_text(encoding="utf-8")).group(1)
        self.assertEqual(literal, web_sync.MEMORY_KEY_RE.pattern)
        for bad in ("memory:t:" + "a" * 32, "memory:p:AB12CD", "memory:p:abc", "memory:", "memory:import:1000", "memory:index2"):
            self.assertIsNone(web_sync.MEMORY_KEY_RE.match(bad), bad)
        for name in ("memory:p:" + "z" * 32, "memory:import:999"):
            self.assertLessEqual(len(name), 64)  # sync_state.name is varchar(64).

    def test_the_golden_samples_have_every_key_and_entries_of_the_frozen_shape(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(golden["version"], 45)  # v45: the one-page 记忆 (no previews, imports, 体检, rooms).
        keys = set(golden["keys"])
        self.assertTrue(all(web_sync.MEMORY_KEY_RE.match(key) for key in keys), keys)
        for kind in ("index", "global", "topics"):
            self.assertIn(f"memory:{kind}", keys)
        self.assertTrue(any(key.startswith("memory:p:") for key in keys))
        self.assertFalse(any(key == "memory:previews" or key.startswith("memory:import:") for key in keys))
        self.assertEqual(golden["keys"]["memory:index"]["version"], web_memory.V45)
        self.assertFalse({"health", "imports"} & set(golden["keys"]["memory:index"]))
        entries = [*golden["keys"]["memory:global"]["entries"],
                   *(e for topic in golden["keys"]["memory:topics"]["topics"] for e in topic["entries"]),
                   *next(v for k, v in golden["keys"].items() if k.startswith("memory:p:"))["entries"]]
        self.assertTrue(entries)
        for entry in entries:
            self.assertLessEqual(ENTRY_FIELDS, set(entry), entry.get("key"))
            self.assertIn(entry["kind"], memstore.KINDS)
            self.assertIn(entry["scope"], memstore.SCOPES)
            self.assertIn(entry["status"], memstore.STATUSES)
            self.assertIn(entry["by"], memstore.BY)
            self.assertTrue(memstore.is_code(entry["code"]), entry["code"])
            self.assertLessEqual(len(entry["title"]), memstore.TITLE_CHARS)
            limit = memstore.DECISION_CHARS if entry["kind"] == "decision" else memstore.TEXT_CHARS
            self.assertLessEqual(len(entry["text"]), limit)
        actions = {edit["action"] for edit in golden["edits"]}
        self.assertEqual(actions, set(ts_array("MEMORY_EDIT_ACTIONS_V43")))
        self.assertEqual(list(web_memory.ACTIONS_V43), ts_array("MEMORY_EDIT_ACTIONS_V43"))  # The Bridge does each of them (M4).
        self.assertTrue(all(edit.get("v") == 43 for edit in golden["edits"]))
        self.assertLessEqual({"repos_set", "repos_add", "repos_remove", "worktrees_prune"}, set(ts_array("SETTINGS_WHATS")))
        self.assertNotIn("/home/", GOLDEN.read_text(encoding="utf-8"))


class RankTest(unittest.TestCase):
    def test_similarity_is_absolute_and_a_duplicate_needs_both_directions(self):
        self.assertEqual(memory_rank.similarity(0.05, 4.0), 0.0125)  # A weak lone hit stays weak (no rel = final / max).
        self.assertEqual(memory_rank.similarity(9.0, 4.0), 1.0)
        self.assertEqual(memory_rank.similarity(1.0, 0.0), 1.0)
        self.assertEqual(memory_rank.mutual(4.0, 4.0, 1.0, 8.0), 0.125)
        self.assertFalse(memory_rank.covered({"部署"}, {"部署": 1.0}))
        self.assertTrue(memory_rank.covered({"部署", "网页"}, {}))
        self.assertTrue(memory_rank.covered({"ep-xyz"}, {"ep-xyz": 4.2}))

    def test_the_ranking_has_a_bounded_usefulness_and_a_bonus_only_for_the_never_carried(self):
        self.assertEqual(memory_rank.usefulness(0, 0), 0.25)
        self.assertEqual(memory_rank.usefulness(50, 1), 1.0)
        self.assertAlmostEqual(memory_rank.rank(0.6, 0, 0), 0.6 * (0.85 + 0.3 * 0.25) + 0.1)
        self.assertAlmostEqual(memory_rank.rank(0.4, 0, 0), 0.4 * (0.85 + 0.3 * 0.25))
        self.assertAlmostEqual(memory_rank.rank(0.6, 0, 6), 0.6 * (0.85 + 0.3 * 0.1))
        now = 1_800_000_000.0
        self.assertEqual(memory_rank.freshness("note", now - 61 * 86400, now), memory_rank.STALE_FRESHNESS)
        self.assertEqual(memory_rank.freshness("note", 0, now), 1.0)
        self.assertEqual(memory_rank.freshness("rule", now - 400 * 86400, now), 1.0)
        self.assertEqual(memory_rank.SCOPE_WEIGHTS, {"topic": 1.2, "project": 1.1, "global": 1.0, "other_project": 0.85, "done": 0.6})


class StoreShapeTest(unittest.TestCase):
    def test_codes_old_ids_and_the_files_beside_what_an_older_release_rewrites(self):
        self.assertTrue(memstore.is_code("R7K2Q"))
        self.assertFalse(memstore.is_code("RIL0O"))  # Crockford: no I, L, O, U.
        self.assertTrue(all(memstore.is_legacy(old) for old in ("p7dcce3", "k48ccd6", "M12")))
        state = Path("/absolute/state")
        self.assertEqual(memstore.sidecar_path(state, "project", "p1").as_posix(), "/absolute/state/projects/p1/memory-v2.json")
        self.assertEqual(memstore.usage_path(state).as_posix(), "/absolute/state/memory/usage.json")
        self.assertEqual(memstore.decisions_path(state, "topic", "t1").as_posix(), "/absolute/state/topics/t1/decisions.jsonl")
        with self.assertRaises(ValueError):
            memstore.sidecar_path(state, "global", "")
        self.assertEqual(run_dir.REPOS, "repos.json")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(home.read(tmp), home.HomeConfig())
            self.assertEqual(repomap.read(tmp), [])
            self.assertEqual(topic_repos.requests(tmp), [])
            memstore.touch_rev(tmp, now=1_800_000_000.0)
            self.assertEqual(memstore.rev_path(tmp).stat().st_mtime, 1_800_000_000.0)
            Path(tmp, "repos.json").write_text(json.dumps({"repos": [
                {"name": "core", "path": "/absolute/core", "status": "frozen", "write_mode": "inplace", "future": 1},
                {"name": "", "path": "/absolute/x"}, {"name": "rel", "path": "relative"}]}))
            (repo,) = repomap.read(tmp)
            self.assertEqual((repo.status, repo.write_mode, repo.extra), ("frozen", "inplace", {"future": 1}))
            self.assertIs(repomap.find([repo], "/absolute/core/"), repo)
        self.assertEqual(home.render("{home}/tasks/{date}-T{no}", home="/h", date="20261127", no=7), "/h/tasks/20261127-T7")
        self.assertEqual(question_dedup.q_hash({"question": " 部署吗 ", "options": [{"label": "是"}]}),
                         question_dedup.q_hash({"question": "部署吗", "options": [{"label": "是"}]}))


class HookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = make_context(Path(tmp.name))
        app = harness_app(CLAUDE_SPEC, FakeBackend(), ClaudeSessionStore())
        quiet(app)
        app.runtime.state_dir = self.ctx.state_dir
        self.runs = app.runs
        self.job = ReplyJob(job_id=5, user_text="看一下", chat_id="oc_dm", message_id="om5", timeout=0, sender_id="ou_owner")

    def test_the_empty_hooks_leave_the_rules_and_the_message_as_they_were(self):
        self.assertEqual(turn_tail("", ""), "")
        self.assertEqual(turn_tail("A", "", "B"), "\n\nA\n\nB")
        self.assertEqual(home_turn.static_block(self.ctx), "")  # A topic that did not start in the home keeps its workspace block.
        environment = home_turn.env(self.ctx)  # W1: every Harness process gets the home, the topic and the state, covey on PATH.
        self.assertEqual({k: environment[k] for k in ("COVEY_HOME", "COVEY_TOPIC", "COVEY_STATE")},
                         {"COVEY_HOME": str(self.ctx.default_workdir), "COVEY_TOPIC": self.ctx.topic_id,
                          "COVEY_STATE": str(self.ctx.state_dir)})
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(home.bin_dir()))
        self.assertEqual(repos_turn.tail(self.ctx, self.job), "")
        self.assertEqual(memory_turn.tail(self.ctx, self.job, lambda: True), "")
        self.assertEqual(self.runs._turn_tail(self.ctx, self.job), "")
        rules = self.runs.bridge_rules(self.ctx, self.job)
        self.assertIn(str(self.ctx.workdir), rules)  # The workspace block, as before (home_section without a home).
        with mock.patch.object(home_turn, "static_block", return_value="主目录：/absolute/home"):
            homed = self.runs.bridge_rules(self.ctx, self.job)
        self.assertIn("主目录：/absolute/home", homed)
        with mock.patch.object(repos_turn, "tail", return_value="仓库块"), \
                mock.patch.object(memory_turn, "tail", return_value="记忆块"):
            self.assertEqual(self.runs._turn_tail(self.ctx, self.job), "\n\n记忆块\n\n仓库块")

    def test_a_resumed_session_with_the_rules_at_system_level_records_the_memory_it_carries(self):
        with mock.patch.object(memory_turn, "record") as record:
            out = self.runs._with_rules(self.ctx, self.job, "MSG", "RULES", system=True, fresh=False, rules_session=True)
        self.assertEqual(out, "MSG")  # The message itself is as before.
        record.assert_called_once()
        with mock.patch.object(memory_turn, "record") as record, \
                mock.patch("covey.app.runs.proxy_topic", return_value=True):
            self.runs._with_rules(self.ctx, self.job, "MSG", "RULES", system=True, fresh=False, rules_session=True)
        record.assert_not_called()  # A 代答 topic's resumed turn records nothing, as before.


if __name__ == "__main__":
    unittest.main()

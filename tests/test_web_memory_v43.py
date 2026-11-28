"""The page's 记忆 keys (data contract v45, app/web_memory.py Center, app/web_sync.py push_memory_center): every memory:* key
built from the one store has the fields and the types of its golden sample (tests/golden/web_memory_v43.json, v45's shape),
key by key, and nothing of 体检, the previews, the imports or the rooms; the change sentinel is the one file stat'ed; a key
whose rev did not change is not sent again, and what a turn did with an entry never moves one; a key gone is taken back
(SyncDeleted), an older release's previews and import pages too; the old ``memory`` key (v37) still goes alone; the log
leaves out the Bridge's own notes of the owner's answers; a decision says the T number it was asked in; and the 15 actions
of a ``memory_edit`` with ``v: 43`` (app/web_ops.py) each change the store as the receipt says, with the operation's id as
the undo token (import and ignore refused). Package M2's functions (core/memory_review.py decide/decide_all) are taken when
they have the agreed signatures: stand-ins of the same signatures show it."""
from __future__ import annotations

import json
import os
import pathlib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import state_dir, write_job, write_topic
from test_web_sync import WebCase

from covey.app import web_memory
from covey.app import web_sync as web_sync_mod
from covey.app.web_ops import Ops
from covey.core import memory_review, memory_usage, memstore, principles, projects, web_sync

GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "tests" / "golden" / "web_memory_v43.json").read_text(encoding="utf-8"))
CHAT = "oc_owner_dm"
PID = "demo01"
TID = "t001"
NOW = time.time()
# The optional fields the contract (web/shared/api.interface.ts) allows beside the golden's, per shape (a list's element).
EXTRA = {"entry": {"project_key", "topic_key", "topic_number", "why", "repos", "device", "verified_at", "over_cap",
                   "superseded_by", "edited", "in_progress"},
         "origin": {"topic_key", "topic_number", "quote", "message_id", "question", "file", "harness"},
         "review": {"quote", "from", "conflict_with", "items", "flags"}}


def kind_of(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return {str: "str", list: "list", dict: "dict", type(None): "null"}[type(value)]


def schema(samples) -> dict:
    """The shape the golden ``samples`` of one place have: their type; of dicts each field's schema (from every sample that has
    it) and the fields every sample has (required); of lists their elements' schema. A map (``revs``, ``health``) is open."""
    samples = list(samples)
    kinds = {kind_of(value) for value in samples}
    kind = "number" if kinds == {"number"} else kinds.pop() if len(kinds) == 1 else "any"
    out = {"kind": kind}
    if kind == "dict":
        names = [name for sample in samples for name in sample]
        out["fields"] = {name: schema([sample[name] for sample in samples if name in sample]) for name in dict.fromkeys(names)}
        out["required"] = set.intersection(*(set(sample) for sample in samples))
        values = [value for sample in samples for value in sample.values()]
        out["values"] = schema(values) if values and len({kind_of(v) for v in values}) == 1 else {"kind": "any"}
    elif kind == "list":
        out["items"] = schema([item for sample in samples for item in sample]) if any(samples) else None
    return out


OPEN = ("revs",)  # Maps: any key, the golden's values' type.


def check(test, shape, ours, where, seen, extra_of):
    """``ours`` has ``shape``: the same type; a dict every required field and no field the contract does not know, each field
    checked; a list each element. ``seen`` collects the paths of the golden fields our data had (the coverage check)."""
    if shape["kind"] == "any":
        return
    test.assertEqual(kind_of(ours), shape["kind"], where)
    if shape["kind"] == "dict":
        is_open = where.rsplit(".", 1)[-1] in OPEN
        if not is_open:
            for name in shape["required"] - extra_of(where):  # What the contract makes optional the golden may still show.
                test.assertIn(name, ours, f"{where}.{name}")
        for name, value in ours.items():
            field = shape["values"] if is_open else shape["fields"].get(name)
            if field is None:
                test.assertIn(name, extra_of(where), f"{where}.{name} is not in the contract")
                continue
            path = f"{where}.{name}" if not is_open else where
            seen.add(f"{where}.{name}" if not is_open else where)
            check(test, field, value, path, seen, extra_of)
    elif shape["kind"] == "list" and shape["items"] is not None:
        for value in ours:
            check(test, shape["items"], value, f"{where}[]", seen, extra_of)


def golden_paths(shape, where, out) -> set:
    """Every path of a field the golden samples have (an open map's own path only)."""
    if shape["kind"] == "dict":
        is_open = where.rsplit(".", 1)[-1] in OPEN
        for name, field in shape["fields"].items():
            if is_open:
                out.add(where)
                continue
            out.add(f"{where}.{name}")
            golden_paths(field, f"{where}.{name}", out)
    elif shape["kind"] == "list" and shape.get("items"):
        golden_paths(shape["items"], f"{where}[]", out)
    return out


def extra_fields(path) -> set:
    tail = path.rsplit(".", 1)[-1]
    if tail in ("entries[]", "items[]") and "review[]" not in path:
        return EXTRA["entry"]
    if tail == "origin":
        return EXTRA["origin"]
    if tail == "review[]":
        return EXTRA["review"]
    if tail == "items[]" and "review[]" in path:
        return EXTRA["entry"]
    if tail == "topics[]":
        return {"topic_number", "project_key"}
    if tail == "log[]":
        return {"because", "undo_token"}
    if tail == "because":
        return {"topic_number", "quote"}
    if tail == "from":
        return {"topic_key", "topic_number"}
    if tail == "suggest":
        return {"project_key"}
    if tail == "counts":
        return {"migrate"}  # T870 Wave 3.5: the items memory_v2 carried over, of ``pending`` (MemoryCounts.migrate).
    return set()


class Case(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_KNOWLEDGE": "0", "COVEY_MEMORY_LEARN": "0"}))
        self.state = state_dir(self)
        self.home = self.state / "home"
        memstore._cache.clear()
        self.addCleanup(memstore._cache.clear)
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="演示专项", project_id=PID, now=NOW))

    def seed(self):
        """A store with every part the golden samples show: 全局 rules and notes (two saying the same), a topic's rule and note,
        a project's decision, 待审核 of each kind the golden has, the log."""
        s = self.state
        self.rule = memstore.add(s, kind="rule", scope="global", text="提交代码之前先跑相关测试，失败就不提交。", title="提交前先跑测试",
                                 why="曾经把没跑过的改动合进了主干", origin={"topic_number": 12, "quote": "提交代码之前先跑相关测试"},
                                 token="u-0001", now=NOW - 100)
        self.note = memstore.add(s, kind="note", scope="global", text="演示环境的网页服务监听在 8080 端口。", title="部署机用的端口",
                                 said="summary", by="learn", verified_at=NOW - 90 * 86400, now=NOW - 90 * 86400)
        data = json.loads(memstore.global_path(s).read_text())  # The same words twice: written as an older store would.
        twin = dict(data["entries"][0], id="g9z9z9", code="")
        data["entries"].append(twin)
        memstore.global_path(s).write_text(json.dumps(data, ensure_ascii=False))
        write_topic(s, topic_id=TID, chat_id=CHAT, topic_number=12, title="演示话题", project={"id": PID, "role": "member"})
        self.trule = memstore.add(s, kind="rule", scope="topic", owner_key=TID, text="这个话题里不要修改 demo-repo，只读它的代码。",
                                  title="这个话题只读 demo-repo", repos=("demo-repo",), now=NOW - 50,
                                  origin={"topic_number": 12, "quote": "这个话题里不要修改 demo-repo，"})
        memstore.edit(s, self.trule.ref, why="主人说的")
        self.tnote = memstore.add(s, kind="note", scope="topic", owner_key=TID, text="日志里的超时来自上游接口，和本地代码无关。",
                                  title="排查结论", now=NOW - 40)
        for n in range(6):  # Past the topic's room: the last ones are listed by title only.
            memstore.add(s, kind="rule", scope="topic", owner_key=TID, text=f"第 {n} 条话题规矩" + "很长" * 60, now=NOW - 30 + n)
        self.decision = memstore.add_decision(s, scope="project", owner_key=PID, question="记忆中心先上线什么？", answer="只读页",
                                              said="summary", by="lead", origin={"topic_number": 12, "quote": "记忆中心第一版先只做查看"},
                                              now=NOW - 20)
        self.learn, _ = memstore.review_put(s, {"type": "learn", "title": "回复先说结论", "text": "回复时先给结论，再展开原因。",
                                                "quote": "你先说结论吧", "origin": {"topic_key": TID, "topic_number": 12},
                                                "suggest": {"kind": "rule", "scope": "global"}, "flags": ["repeat"]})
        self.conflict, _ = memstore.review_put(s, {"type": "conflict", "title": "部署端口改为 9090", "text": "演示环境的网页服务改到 9090 端口。",
                                                   "suggest": {"kind": "note", "scope": "global"}, "conflict_with": self.note.ref,
                                                   "flags": ["dynamic"], "origin": {"topic_key": "t002", "topic_number": 15}})
        # The two kinds the learning proposes (T908 M3b): a rule held where it was said that may be 全局 (its line in items),
        # and one that grants, in force only once accepted.
        self.wider, _ = memstore.review_put(s, {"type": "learn", "title": "只读 demo-repo 管全局？", "text": self.trule.text,
                                                "quote": "这个话题里不要修改 demo-repo，", "origin": {"topic_key": TID, "topic_number": 12},
                                                "suggest": {"kind": "rule", "scope": "global"}, "items": [self.trule.ref]})
        self.project_item, _ = memstore.review_put(s, {"type": "learn", "title": "改完直接部署到演示机",
                                                       "text": "演示专项里改完直接部署到演示机，不用再问。", "quote": "以后改完直接部署到演示机",
                                                       "origin": {"topic_key": TID, "topic_number": 12},
                                                       "suggest": {"kind": "rule", "scope": "project", "project_key": PID,
                                                                   "grant": "deploy"}})
        memstore.log_change(s, who="owner", action="accept", refs=[self.rule.ref], title="提交前先跑测试",
                            because={"topic_number": 12, "quote": "提交之前先跑测试"}, token="u-0002", now=NOW - 10)
        write_job(s, "k-t001", topic_id=TID, chat_id=CHAT, job_id=12, status="done", updated_at=NOW - 5)

    def build(self, now=None):
        return web_memory.Center(self.state).build(now=now or NOW)


class GoldenShapeTest(Case):
    def test_every_key_has_the_golden_fields_and_types_and_covers_them(self):
        self.seed()
        built = self.build()
        golden = GOLDEN["keys"]
        names = {"memory:p:demo01": f"memory:p:{PID}"}
        self.assertEqual({web_sync.MEMORY_KEY_RE.match(key) is not None for key in built}, {True})
        for key in golden:
            self.assertIn(names.get(key, key), built)
        seen, wanted = set(), set()
        for key, sample in golden.items():
            shape = schema([sample])
            golden_paths(shape, key, wanted)
            with self.subTest(key=key):
                check(self, shape, built[names.get(key, key)], key, seen, extra_fields)
        # The notes a preview finds relevant are package M2's search (app/memory_turn.py preview): PackageM2Test checks their shape.
        # A topic's line keeps no quote of where it came from (core/memstore.py topic_add): its origin is the topic itself.
        wanted = {path for path in wanted if ".notes[]" not in path and path != "memory:topics.topics[].entries[].origin.quote"}
        self.assertEqual(sorted(wanted - seen), [], "golden fields our data never had")
        index = built["memory:index"]
        self.assertEqual(index["revs"]["memory:global"], web_sync.rev(built["memory:global"]))
        self.assertEqual(set(index["revs"]), set(built))
        self.assertEqual(index["counts"]["pending"], 4)
        self.assertEqual(index["counts"]["migrate"], 0)
        self.assertEqual(index["version"], 45)
        for gone in ("health", "imports"):  # v45: no 体检, no import.
            self.assertNotIn(gone, index)
        self.assertEqual([sorted(row) for row in index["scopes"]], [["counts", "key", "rev", "title"]] * len(index["scopes"]))
        self.assertFalse([key for key in built if key == "memory:previews" or key.startswith("memory:import:")])
        self.assertNotIn("carried_next", json.dumps(built, ensure_ascii=False))
        for key, data in built.items():
            self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), web_memory.KEY_BYTES, key)

    def test_the_migrated_items_are_counted_apart_for_the_badge(self):
        """T870 Wave 3.5 (P1-6): what memory_v2 carried over waits in 待审核 as type migrate; the index and each scope say
        how many, the page's badge and first tab leave them out."""
        self.seed()
        for n in range(3):
            memstore.review_put(self.state, {"type": "migrate", "title": f"迁移约束{n}", "text": f"负责人记的第{n}条约束",
                                             "items": [f"project:{PID}:m{n}"],
                                             "suggest": {"kind": "rule", "scope": "project", "project_key": PID}})
        index = self.build()["memory:index"]
        self.assertEqual((index["counts"]["pending"], index["counts"]["migrate"]), (7, 3))
        scoped = {row["key"]: row["counts"] for row in index["scopes"]}
        self.assertEqual(scoped[f"p:{PID}"]["migrate"], 3)
        self.assertEqual(scoped["global"]["migrate"], 0)

    def test_entries_carry_no_use_counts_and_a_turns_use_moves_no_key(self):
        self.seed()
        before = self.build()
        memory_usage.carried(self.state, [self.rule.ref, self.note.ref, self.trule.ref])
        memory_usage.signal(self.state, [self.rule.ref], "memory_used")
        after = self.build()
        for key in before:
            if key != "memory:index":
                self.assertEqual(web_sync.rev(before[key]), web_sync.rev(after[key]), key)
        self.assertEqual(before["memory:index"], after["memory:index"])
        text = json.dumps(after["memory:global"], ensure_ascii=False)
        for word in ('"e"', '"t"', '"h"', '"n"', '"last"', "usage"):
            self.assertNotIn(word + ":", text)

    def test_the_global_key_past_its_size_loses_log_rows_never_review(self):
        self.seed()
        for n in range(400):
            memstore.log_change(self.state, who="owner", action="edit", refs=[self.rule.ref], title="改" * 40, token=f"u-{n}", now=NOW)
        with mock.patch.object(web_memory, "KEY_BYTES", 6000), mock.patch.object(web_memory, "LOG_SHOWN", 400):
            data = self.build()["memory:global"]
            self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), 6000 + 4000)
        self.assertEqual(len(data["review"]), 4)
        self.assertLess(len(data["log"]), 400)

    def test_titles_are_short_and_at_most_the_table(self):
        self.seed()
        with mock.patch.object(web_memory, "TITLES", 3):
            titles = self.build()["memory:index"]["titles"]
        self.assertEqual(len(titles), 3)
        for code, title, key in titles:
            self.assertTrue(memstore.is_code(code))
            self.assertLessEqual(len(title), 40)
            self.assertIsNotNone(memstore.find(self.state, key))

    def test_rules_past_the_room_are_flagged_not_dropped(self):
        self.seed()
        topic = self.build()["memory:topics"]["topics"][0]
        rules = [e for e in topic["entries"] if e["kind"] == "rule" and e["status"] == "active"]
        self.assertEqual(len(rules), 7)
        over = [e for e in rules if e.get("over_cap")]
        self.assertTrue(over)
        self.assertLess(len(over), len(rules))  # The page says “N 条只带标题” of these.
        self.assertEqual(topic["entries"][0]["topic_number"], 12)

    def test_a_decision_says_the_topic_it_was_asked_in(self):
        self.seed()
        asked = memstore.add_decision(self.state, scope="project", owner_key=PID, question="要不要发版？", answer="先不发",
                                      origin={"topic_key": TID}, now=NOW - 5)
        rows = {row["key"]: row for row in self.build()["memory:p:" + PID]["entries"]}
        self.assertEqual(rows[asked.ref]["origin"]["topic_number"], 12)  # 上次你答过：…（T12 · 日期）on the page's search.
        self.assertEqual(rows[self.decision.ref]["origin"]["topic_number"], 12)  # Said in its own origin: kept.

    def test_the_log_leaves_out_the_bridges_own_notes_of_the_owners_answers(self):
        self.seed()
        answered = memstore.add_decision(self.state, scope="project", owner_key=PID, question="先做哪个？", answer="先做 A",
                                         origin={"topic_key": TID}, who="owner", now=NOW - 3)
        web = memstore.add_decision(self.state, scope="project", owner_key=PID, text="网页上记的决定", said="owner", by="web",
                                    who="owner", token="w-op-d", now=NOW - 2)
        refs = [ref for row in self.build()["memory:global"]["log"] for ref in row["refs"]]
        self.assertNotIn(answered.ref, refs)
        self.assertIn(web.ref, refs)  # The owner's own, from the page: in 记录.
        self.assertIn(self.rule.ref, refs)

    def test_topics_older_than_thirty_days_are_not_read(self):
        self.seed()
        built = self.build(now=NOW + 40 * 86400)
        self.assertEqual(built["memory:topics"], {"topics": []})


class StampTest(Case):
    def test_stamps_stat_the_one_sentinel_and_walk_nothing(self):
        self.seed()
        web_memory.stamps(self.state)
        statted = []
        real = pathlib.Path.stat

        def stat(path, *args, **kwargs):
            statted.append(str(path))
            return real(path, *args, **kwargs)

        def walk(*args, **kwargs):
            raise AssertionError("stamps walked a directory")

        with mock.patch.object(pathlib.Path, "stat", stat), mock.patch.object(pathlib.Path, "iterdir", walk), \
                mock.patch.object(pathlib.Path, "glob", walk), mock.patch.object(os, "scandir", walk), \
                mock.patch.object(os, "listdir", walk):
            first = web_memory.stamps(self.state)
        self.assertEqual(statted, [str(memstore.rev_path(self.state))])
        self.assertEqual(web_memory.stamps(self.state), first)
        memstore.archive(self.state, self.note.ref)
        self.assertNotEqual(web_memory.stamps(self.state), first)

    def test_an_older_stores_write_moves_the_sentinel_once_it_was_planted(self):
        self.assertEqual(memstore.rev_path(self.state).exists(), False)
        first = web_memory.stamps(self.state)
        self.assertTrue(memstore.rev_path(self.state).exists())
        principles.save(self.state, principles.OWNER, "先给结论")  # core/memory_rev.py touch(create=False)
        self.assertNotEqual(web_memory.stamps(self.state), first)


class ActionsTest(Case):
    """The 15 actions, each through app/web_ops.py as the page sends them, with the operation's id as the undo token."""

    def setUp(self):
        super().setUp()
        self.seed()
        runtime = SimpleNamespace(owner_open_id="ou_owner", access=SimpleNamespace(is_owner=lambda open_id: True), log=lambda line: None)
        self.ops = SimpleNamespace(runtime=runtime)

    def run_op(self, op_id, action, keys=(), **payload):
        row = {"op_id": op_id, "kind": "memory_edit"}
        return Ops.memory_edit(self.ops, self.state, CHAT, row, {"v": 43, "action": action, "keys": list(keys), **payload})

    def logged(self, token):
        return [row for row in memstore.log_read(self.state, limit=500) if row.get("undo_token") == token]

    def test_add_edit_move_promote_archive_restore_void(self):
        said = self.run_op("op-1", "add", title="先只读后编辑", text="第一版先做只读页", kind="decision", scope="project", project_key=PID)
        self.assertTrue(said.startswith("已记下"), said)
        decision = next(e for e in memstore.decisions(self.state, "project", PID) if e.title == "先只读后编辑")
        self.assertEqual((decision.by, decision.said), ("web", "owner"))
        self.assertTrue(self.logged("w-op-1"))
        self.assertTrue(self.run_op("op-2", "add", title="一条", text="提交前先跑测试", kind="rule", scope="global").startswith("已记下"))
        self.assertTrue(self.run_op("op-2b", "add", title="一条", text="提交前先跑测试", kind="rule", scope="global").startswith("已有"))
        rule = memstore.find(self.state, self.rule.ref)
        with self.assertRaisesRegex(ValueError, "刚在别处改过"):
            self.run_op("op-3", "edit", [rule.ref], text="新的话", base="nope")
        self.assertTrue(self.run_op("op-3", "edit", [rule.ref], text="提交前先跑全部相关测试", kind="rule", base=memstore.rev_of(rule)).startswith("已保存"))
        self.assertEqual(memstore.find(self.state, rule.ref).text, "提交前先跑全部相关测试")
        self.assertEqual(self.run_op("op-3b", "edit", [rule.ref], text="提交前先跑全部相关测试"), "没有变化")
        moved = self.run_op("op-4", "move", [self.trule.ref], scope="project", project_key=PID,
                            base=memstore.rev_of(memstore.find(self.state, self.trule.ref)))
        self.assertTrue(moved.startswith("已换范围"), moved)
        self.assertEqual(memstore.find(self.state, self.trule.ref).status, "archived")
        promoted = self.run_op("op-5", "promote", [self.tnote.ref], scope="project", project_key=PID, repos=["demo-repo"])
        self.assertTrue(promoted.startswith("已提到"), promoted)
        new = memstore.find(self.state, memstore.find(self.state, self.tnote.ref).superseded_by)
        self.assertEqual((new.scope, new.repos), ("project", ("demo-repo",)))
        self.assertTrue(self.run_op("op-6", "archive", [self.note.ref]).startswith("已归档"))
        self.assertEqual(self.run_op("op-6b", "archive", [self.note.ref]), "这条已经归档了")
        self.assertTrue(self.run_op("op-7", "restore", [self.note.ref]).startswith("已放回"))
        self.assertTrue(self.run_op("op-8", "void", [decision.ref], why="决定已作废").startswith("已作废"))
        self.assertEqual(memstore.find(self.state, decision.ref).status, "void")
        with self.assertRaisesRegex(ValueError, "已经不在了"):
            self.run_op("op-9", "archive", ["global:gnothere"])

    def test_accept_reject_and_their_all_forms(self):
        said = self.run_op("op-a", "accept", [self.learn], kind="rule", scope="global", title="回复先给结论")
        self.assertEqual(said, "已采纳")
        made = next(e for e in memstore.entries(self.state, scope="global") if e.title == "回复先给结论")
        self.assertEqual((made.kind, made.said, made.by), ("rule", "owner", "learn"))
        self.assertEqual(memstore.review_items(self.state, states=("accepted",))[0]["key"], self.learn)
        self.assertEqual(self.run_op("op-r", "reject", [self.project_item]), "不要了")
        with self.assertRaisesRegex(ValueError, "已经处理过了"):
            self.run_op("op-r2", "reject", [self.project_item])
        self.assertEqual(self.run_op("op-all", "accept_all", [self.wider]), "全部采纳 1 条")
        # 推断全局: its line held in the topic moves to 全局 with its code, not made again (core/memory_review.py).
        moved = memstore.find(self.state, self.trule.code)
        self.assertEqual((moved.scope, moved.text), ("global", self.trule.text))
        self.assertEqual(memstore.find(self.state, self.trule.ref).status, "archived")
        self.assertEqual(self.run_op("op-none", "reject_all", []), "全部不要 1 条")  # The conflict, the last one open.
        self.assertEqual(memstore.review_items(self.state), [])
        with self.assertRaisesRegex(ValueError, "没有要处理的待审核"):
            self.run_op("op-none2", "accept_all", [])

    def test_keep_left_right_both(self):
        self.assertEqual(self.run_op("op-l", "keep_left", [self.note.ref, self.conflict]), "留了原来的")
        self.assertEqual(memstore.find(self.state, self.note.ref).text, "演示环境的网页服务监听在 8080 端口。")
        memstore.review_set(self.state, self.conflict, "open")
        self.assertEqual(self.run_op("op-rt", "keep_right", [self.note.ref, self.conflict]), "换成了新的")
        self.assertEqual(memstore.find(self.state, self.note.ref).text, "演示环境的网页服务改到 9090 端口。")
        other, _ = memstore.review_put(self.state, {"type": "conflict", "title": "备用端口", "text": "演示环境另开了 7070 端口。",
                                                    "suggest": {"kind": "note", "scope": "global"}, "conflict_with": self.note.ref})
        before = len(memstore.entries(self.state, scope="global"))
        self.assertEqual(self.run_op("op-b", "keep_both", [self.note.ref, other]), "两条都留了")
        self.assertEqual(len(memstore.entries(self.state, scope="global")), before + 1)

    def test_import_and_ignore_are_gone_and_undo_takes_a_change_back(self):
        for action in ("import", "ignore"):
            with self.assertRaisesRegex(ValueError, "不认识的记忆操作"):
                self.run_op("op-" + action, action, ["fp1"], kind="rule", scope="global")
        self.assertTrue(self.run_op("op-i", "add", title="新的一条", text="测试要用真实数据库。", kind="rule", scope="global").startswith("已记下"))
        made = next(e for e in memstore.entries(self.state, scope="global") if e.text == "测试要用真实数据库。")
        self.assertTrue(self.run_op("op-u", "undo", undo_token="w-op-i").startswith("已撤销"))
        self.assertEqual(memstore.find(self.state, made.ref).status, "void")
        with self.assertRaisesRegex(ValueError, "撤销过了"):
            self.run_op("op-u2", "undo", undo_token="w-op-i")

    def test_undo_of_a_decision_of_review_opens_it_again(self):
        self.run_op("op-a", "accept", [self.learn], kind="rule", scope="global")
        self.run_op("op-r", "reject", [self.project_item])
        self.assertIn("回到待审核", self.run_op("op-u", "undo", undo_token="w-op-a"))
        self.assertIn(self.learn, [item["key"] for item in memstore.review_items(self.state)])
        self.assertIn("回到待审核", self.run_op("op-u2", "undo", undo_token="w-op-r"))
        self.assertIn(self.project_item, [item["key"] for item in memstore.review_items(self.state)])

    def test_every_golden_action_is_known_and_the_old_payload_is_refused(self):
        self.assertEqual(set(web_memory.ACTIONS_V43), {edit["action"] for edit in GOLDEN["edits"]})
        with self.assertRaisesRegex(ValueError, "页面版本太旧"):  # v37's memory_edit, without v: gone (T908 M3b).
            Ops.memory_edit(self.ops, self.state, CHAT, {"op_id": "op-old"},
                            {"action": "add", "what": "principle", "scope": "global", "text": "先给结论"})
        self.assertEqual(principles.entries(self.state), [])
        with self.assertRaisesRegex(ValueError, "不认识的记忆操作"):
            self.run_op("op-x", "pin", [self.rule.ref])

    def test_the_log_gives_the_page_the_undo_token_of_its_operation(self):
        self.run_op("op-6", "archive", [self.note.ref])
        log = self.build()["memory:global"]["log"]
        row = next(item for item in log if item.get("undo_token") == "w-op-6")
        self.assertIn(self.note.ref, row["refs"])
        self.run_op("op-u", "undo", undo_token="w-op-6")
        log = self.build()["memory:global"]["log"]
        self.assertFalse([item for item in log if item.get("undo_token") == "w-op-6"])  # Taken back: no 撤销 any more.


    def test_the_page_reads_what_kind_of_item_it_is_and_the_log_of_its_changes(self):
        """推断全局 carries the line it holds (``items``), 放行类 what it grants (``suggest.grant``); a change of an item the owner
        decided is in the log with its 撤销, one the Bridge closed (its line gone) without."""
        self.seed()
        review = {row["key"]: row for row in self.build()["memory:global"]["review"]}
        self.assertEqual([item["key"] for item in review[self.wider]["items"]], [self.trule.ref])
        self.assertEqual(review[self.project_item]["suggest"], {"kind": "rule", "scope": "project", "project_key": PID, "grant": "deploy"})
        self.assertNotIn("grant", review[self.learn]["suggest"])
        self.run_op("op-r", "reject", [self.project_item])
        memstore.void(self.state, self.trule.ref, who="owner", token="w-op-v")
        log = self.build()["memory:global"]["log"]
        rejected = next(row for row in log if row["refs"] == [self.project_item])
        closed = next(row for row in log if row["refs"] == [self.wider])
        self.assertEqual((rejected["action"], rejected.get("undo_token")), ("reject", "w-op-r"))
        self.assertEqual((closed["action"], closed.get("undo_token")), ("archive", None))
        self.assertIn("回到待审核", self.run_op("op-u", "undo", undo_token="w-op-v"))  # The void's undo opens it again.
        self.assertIn(self.wider, [item["key"] for item in memstore.review_items(self.state)])


class PackageM2Test(Case):
    """M2's functions of the agreed signatures are the ones taken; A0's shapes are not."""

    def test_review_decisions_go_through_m2_with_the_owners_edits_on_the_item(self):
        self.seed()
        calls = []

        def decide(state_dir, key, action, *, who="owner", token="", now=None):
            calls.append((key, action, who, token, next(i for i in memstore.review_items(state_dir) if i["key"] == key)["title"]))
            memstore.review_set(state_dir, key, "accepted")
            return {"refs": ["global:gm2"]}

        def decide_all(state_dir, action, *, keys=None, who="owner", token="", now=None):
            calls.append(("all", action, tuple(keys or ())))
            return {"refs": []}

        with mock.patch.object(memory_review, "decide", decide, create=True), \
                mock.patch.object(memory_review, "decide_all", decide_all, create=True):
            found = web_memory.review_decide(self.state, [self.learn], "accept", edits={"title": "改过的标题"}, token="w-1")
            web_memory.review_decide(self.state, [], "reject_all", token="w-2")
        self.assertEqual(found["refs"], ["global:gm2"])
        self.assertEqual(calls[0], (self.learn, "accept", "owner", "w-1", "改过的标题"))
        self.assertEqual(calls[1][:2], ("all", "reject_all"))
        self.assertEqual(set(calls[1][2]), {self.conflict, self.wider, self.project_item})



class IntegratedM2Test(Case):
    """Seam M4 ↔ M2 (T870 Wave 2B): with package M2 in, its real functions are the ones taken (no stand-in, no silent fall back
    to this module's baseline), and 在办 is app/memory_turn.py's rule (FINAL-SPEC 修订 5 as docs/butler.md 记忆 says it)."""

    def test_review_goes_through_memory_review_alone(self):
        self.seed()
        self.assertFalse([name for name in ("_m2", "_baseline_decide", "_write_item", "BY_OF") if hasattr(web_memory, name)])
        found = web_memory.review_decide(self.state, [self.learn], "accept", token="w-m2-1")
        web_memory.review_decide(self.state, [self.project_item], "reject", token="w-m2-2")
        self.assertTrue(found["refs"], found)
        states = {item["key"]: item["state"] for item in memstore.review_items(self.state, states=None)}
        self.assertEqual((states[self.learn], states[self.project_item]), ("accepted", "rejected"))
        self.assertIsNone(next((e for e in memstore.entries(self.state) if "部署到演示机" in e.text), None))  # 不要: not written.

    def test_a_turns_memory_reads_m2s_memory_ids(self):
        """A turn's ``memory`` on the page (app/web_turns.py memory_of): app/memory_turn.py writes ``memory_ids`` as
        {n: the short code, ref, scope, kind, by}; their codes are the turn's, not the dicts' str (T870, CUA C13)."""
        from covey.app import web_turns
        self.seed()
        rule = memstore.find(self.state, self.rule.ref)
        record = {"memory_ids": [{"n": rule.code, "ref": rule.ref, "scope": "global", "kind": "rule", "by": "owner"},
                                 {"n": "", "ref": self.decision.ref, "scope": "project", "kind": "decision", "by": "lead"}],
                  "memory_used": [rule.code]}
        found = web_turns.memory_of(self.state, record)
        self.assertEqual((found["count"], found["used"], found["codes"]), (2, [{"code": rule.code, "title": rule.title}],
                                                                           [rule.code]))
        self.assertIsNone(web_turns.memory_of(self.state, {}))

    def test_what_a_turn_used_names_its_own_code_not_the_carried_one_at_its_place(self):
        """T870 Wave 3.5 (both reviews' P0, CUA C13): ``used`` and ``codes`` were two lists the page paired by position; a
        turn that used the last entry it carried showed the first one's code. Each used one names its own code now."""
        from covey.app import web_turns
        made = [memstore.add(self.state, kind="rule", scope="global", text=f"第{n}条：提交前先跑测试{n}", title=f"规矩{n}")
                for n in range(3)]
        record = {"memory_ids": [{"n": e.code, "ref": e.ref, "scope": "global", "kind": "rule", "by": "owner"} for e in made],
                  "memory_used": [made[2].code, "M9"]}
        found = web_turns.memory_of(self.state, record)
        self.assertEqual(found["codes"], [e.code for e in made])
        self.assertEqual(found["used"], [{"code": made[2].code, "title": "规矩2"}, {"code": "", "title": "M9"}])
        carried_only = web_turns.memory_of(self.state, {"memory_ids": record["memory_ids"]})
        self.assertEqual((carried_only["count"], carried_only["used"]), (3, []))  # Still drawn, open-able, by its codes.

    def test_a_decision_is_in_progress_by_m2s_rule(self):
        self.seed()
        old = memstore.add_decision(self.state, scope="project", owner_key=PID, question="三周前问的事？", answer="先不做",
                                    origin={"topic_key": "t-gone"}, now=NOW - 21 * 86400)
        quiet = memstore.add_decision(self.state, scope="project", owner_key=PID, question="四天前问的另一件事？", answer="照旧",
                                      origin={"topic_key": "t-quiet"}, now=NOW - 4 * 86400)
        write_job(self.state, "k-quiet", topic_id="t-quiet", chat_id=CHAT, job_id=3, status="done",
                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 4 * 86400 - 60)))
        asked = memstore.add_decision(self.state, scope="project", owner_key=PID, question="上线前还要做什么？", answer="跑全量",
                                      origin={"topic_key": TID}, now=NOW - 3 * 86400)
        rows = {row["key"]: row for row in self.build()["memory:p:" + PID]["entries"]}
        self.assertTrue(rows[self.decision.ref]["in_progress"])  # Decided an hour ago.
        self.assertFalse(rows[old.ref]["in_progress"])  # Past 14 days: the project's other topics carry it no more.
        self.assertFalse(rows[quiet.ref]["in_progress"])  # 4 days old, and its topic's last turn was before it.
        self.assertTrue(rows[asked.ref]["in_progress"])  # 3 days old, but its topic had a turn within 48 hours (seed's job).
        rows = {row["key"]: row for row in self.build()["memory:p:" + PID]["entries"]}
        write_job(self.state, "k-t001-3", topic_id=TID, chat_id=CHAT, job_id=14, status="done", updated_at=NOW - 1800,
                  delivery={"commit": "abc1234"})
        rows = {row["key"]: row for row in self.build()["memory:p:" + PID]["entries"]}
        self.assertFalse(rows[asked.ref]["in_progress"])  # A delivered commit ends it for the project.


class CenterPushTest(WebCase):
    """push_memory_center: the data keys, then memory:index; nothing again while the revs are the same; a key gone taken back;
    the old ``memory`` key in its own request as before; off says so and takes the rest back."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_KNOWLEDGE": "0"}))
        memstore._cache.clear()

    def keys_sent(self):
        return [each["key"] for batch in self.web.upserts for each in batch["items"]]

    def deleted(self):
        return [row for batch in self.web.upserts for row in batch["deleted"]]

    def test_the_index_goes_last_and_an_unchanged_rev_is_not_sent_again(self):
        memstore.add(self.state, kind="rule", scope="global", text="提交前先跑测试")
        sent = self.sync.push_memory_center()
        keys = self.keys_sent()
        self.assertEqual(keys[-1], "memory:index")
        self.assertEqual(sent, len(keys))
        self.assertEqual({key for key in keys if not key.startswith("memory:p:")},
                         {"memory:index", "memory:global", "memory:topics"})
        self.assertEqual(self.sync.push_memory_center(), 0)  # The sentinel did not move.
        self.assertEqual(self.sync.push_memory_center(force=True), 0)  # Built again: the same revs.
        memory_usage.carried(self.state, [e.ref for e in memstore.entries(self.state)])
        self.assertEqual(self.sync.push_memory_center(force=True), 0)  # A turn's use moves no key.
        memstore.add(self.state, kind="note", scope="global", text="测试机在 3 楼")
        self.assertEqual(self.sync.push_memory_center(), 0)  # Within CENTER_REST of the page's push: the pass waits.
        self.sync.center_rest = 0.0
        self.assertEqual(self.sync.push_memory_center(), 2)  # memory:global and memory:index.
        self.assertEqual(self.keys_sent()[-2:], ["memory:global", "memory:index"])

    def test_a_project_cancelled_is_taken_back(self):
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="演示专项", project_id=PID, now=NOW))
        memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="专项里先写测试")
        self.sync.push_memory_center()
        self.assertIn(f"memory:p:{PID}", self.keys_sent())
        projects.set_state(self.state, PID, projects.CANCELLED)
        memstore.touch_rev(self.state)
        self.sync.push_memory_center()
        self.assertIn({"type": "settings", "key": f"memory:p:{PID}"}, self.deleted())
        self.assertNotIn(("settings", f"memory:p:{PID}"), self.sync.sent)

    def test_the_previews_and_import_pages_an_older_release_pushed_are_taken_back(self):
        memstore.add(self.state, kind="rule", scope="global", text="提交前先跑测试")
        for key in ("memory:previews", "memory:import:0", "memory:import:1"):
            self.sync.sent[("settings", key)] = "old-rev"
        self.sync.push_memory_center()
        self.assertEqual({row["key"] for row in self.deleted()}, {"memory:previews", "memory:import:0", "memory:import:1"})
        self.assertFalse([key for kind, key in self.sync.sent if key == "memory:previews" or key.startswith("memory:import:")])

    def test_the_old_memory_key_still_goes_alone_and_off_takes_the_center_back(self):
        principles.save(self.state, principles.OWNER, "先给结论")
        self.assertEqual(self.sync.push_memory(), 1)
        self.assertEqual([[each["key"] for each in batch["items"]] for batch in self.web.upserts], [["memory"]])
        self.sync.push_memory_center()
        self.assertTrue(all(key == "memory" or key.startswith("memory:") for key in self.keys_sent()))
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "0"}):
            self.sync.push_memory_center()
            index = [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "memory:index"][-1]
            self.assertFalse(index["data"]["on"])
            self.assertEqual({row["key"] for row in self.deleted() if not row["key"].startswith("memory:p:")},
                             {"memory:global", "memory:topics"})
            self.assertEqual(self.sync.push_memory_center(full=True), 0)

    def test_never_on_never_pushed(self):
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "0"}):
            self.assertEqual(self.sync.push_memory_center(full=True), 0)
        self.assertEqual(self.web.upserts, [])

    def test_a_refused_post_waits_longer_each_time(self):
        memstore.add(self.state, kind="rule", scope="global", text="提交前先跑测试")
        with mock.patch.object(self.sync.client, "request", side_effect=web_sync_error()):
            self.assertEqual(self.sync.push_memory_center(), 0)
            self.assertEqual(self.sync.push_memory_center(full=True), 0)  # Within the wait: not even built.
        self.assertEqual(self.sync.center_failed, 1)
        self.assertGreater(self.sync.center_wait, time.monotonic())
        self.assertEqual(self.sync.push_memory_center(force=True) > 0, True)  # The page's own change goes at once.

    def test_a_change_from_the_page_is_pushed_before_the_ack(self):
        self.sync.push_memory_center()
        self.web.outbox.append({"seq": 1, "op_id": "op-m1", "kind": "memory_edit", "topic_key": "", "project_key": "", "payload": {
            "v": 43, "action": "add", "keys": [], "title": "先给结论", "text": "回复先给结论", "kind": "rule", "scope": "global"}})
        with mock.patch.object(self.runtime, "access", SimpleNamespace(owner_chat=lambda: "oc_chat", is_owner=lambda open_id: True)):
            self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual(self.web.acks[-1]["state"], "done")
        glob = [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "memory:global"][-1]
        self.assertEqual([e["title"] for e in glob["data"]["entries"]], ["先给结论"])
        self.assertEqual(glob["data"]["log"][0]["undo_token"], "w-op-m1")


class CenterRaceTest(WebCase):
    """T870 Wave 3.5 (both reviews' P0): the page's next change within the undo bar's 5 s is pushed before its ack. After a
    quick build the pass builds it all again only CENTER_REST later; a push before an ack waits for a build in the making
    (CENTER_LOCK_WAIT); a build that raises is tried again later and later, logged the first time and every tenth."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_MEMORY": "1", "COVEY_KNOWLEDGE": "0"}))
        memstore._cache.clear()
        memstore.add(self.state, kind="rule", scope="global", text="提交前先跑测试")
        self.sync.push_memory_center()

    def builds(self):
        made = []
        real = web_memory.Center.build

        def build(center, *args, **kw):
            made.append(kw.get("quick", False))
            return real(center, *args, **kw)
        self.enterContext(mock.patch.object(web_memory.Center, "build", build))
        return made

    def test_no_full_build_right_after_the_pages_push(self):
        made = self.builds()
        memstore.add(self.state, kind="note", scope="global", text="测试机在 3 楼")
        self.assertGreater(self.sync.push_memory_center(force=True), 0)
        self.assertEqual(made, [True])
        self.assertGreater(self.sync.center_rest, time.monotonic() + web_sync_mod.CENTER_REST - 5)
        self.assertEqual(self.sync.push_memory_center(full=True), 0)  # The pass's next steps, within the rest: no build.
        self.assertEqual(self.sync.push_memory_center(), 0)
        self.assertEqual(made, [True])
        memstore.add(self.state, kind="note", scope="global", text="撤销也要马上看到")
        self.assertGreater(self.sync.push_memory_center(force=True), 0)  # The undo within the 5 s: built and pushed at once.
        self.assertEqual(made, [True, True])
        self.sync.center_rest = 0.0
        self.sync.push_memory_center()
        self.assertEqual(made, [True, True, False])  # Then a pass's own build.

    def test_the_push_before_the_ack_waits_for_the_build_in_the_making(self):
        import threading
        held, release = threading.Event(), threading.Event()

        def build_holding():
            with self.sync.center_lock:
                held.set()
                release.wait(5)
        other = threading.Thread(target=build_holding)
        other.start()
        self.addCleanup(other.join)
        held.wait(5)
        memstore.add(self.state, kind="note", scope="global", text="测试机在 3 楼")
        threading.Timer(0.3, release.set).start()
        started = time.monotonic()
        self.assertGreater(self.sync.push_memory_center(force=True), 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        glob = [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "memory:global"][-1]
        self.assertIn("测试机在 3 楼", json.dumps(glob["data"], ensure_ascii=False))

    def test_a_build_that_raises_waits_longer_each_time_and_logs_once(self):
        logged = []
        self.enterContext(mock.patch.object(self.runtime, "log", logged.append))
        self.sync.memory_center = None
        self.sync.center_stamps = None
        with mock.patch.object(web_memory.Center, "build", side_effect=OSError("nas")) as build:
            self.assertEqual(self.sync.push_memory_center(full=True), 0)
            self.assertEqual(self.sync.push_memory_center(full=True), 0)
            self.assertEqual(self.sync.push_memory_center(), 0)
        self.assertEqual(build.call_count, 1)  # Within the wait: not built again each pass step.
        self.assertEqual(len([line for line in logged if "not built" in line]), 1)
        self.assertGreater(self.sync.center_wait, time.monotonic() + web_sync_mod.CENTER_WAIT - 5)
        self.sync.center_wait = 0.0
        memstore.add(self.state, kind="note", scope="global", text="测试机在 3 楼")
        self.assertGreater(self.sync.push_memory_center(full=True), 0)  # Readable again: built and pushed.
        self.assertEqual(self.sync.center_broken, 0)


def web_sync_error():
    from covey.app.web_push_client import WebError
    return WebError("refused")


if __name__ == "__main__":
    unittest.main()

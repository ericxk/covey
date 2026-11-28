"""The one memory store (core/memstore.py, docs/operations.md 记忆的存储与迁移): an old entry of every store mapped to the
three kinds, short codes given once and kept (lazily, never by walking the topics), the public writes every package builds
on (add, edit, move, status, kind, decisions) with their log and undo, 待审核 and the import states, and nothing new ever
written into the files an older release rewrites with its own keys."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from support.records import state_dir

from covey.core import knowledge, memory_usage, memstore, principles, project_memory, projects, topic_memory, turn_source

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
TID = "f" * 64
NOW = 1_800_000_000.0


def rev_mtime(state):
    try:
        return memstore.rev_path(state).stat().st_mtime_ns
    except OSError:
        return 0


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        memstore._cache.clear()
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="记忆专项", project_id=PID, now=NOW))


class KindMapTest(unittest.TestCase):
    """One case a row of the mapping table (docs/operations.md 记忆的存储与迁移)."""

    def test_every_old_store_maps_to_one_of_the_three_kinds(self):
        k = memstore.kind3_of
        self.assertEqual(k({"id": "p1a2b3c", "text": "x"}, source="principle"), ("rule", "owner", "active"))
        self.assertEqual(k({"kind": "decision", "state": "current"}, source="knowledge"), ("rule", "owner", "active"))
        self.assertEqual(k({"kind": "practice", "state": "current"}, source="knowledge"), ("rule", "owner", "active"))
        self.assertEqual(k({"kind": "pitfall", "state": "current"}, source="knowledge"), ("note", "owner", "active"))
        self.assertEqual(k({"kind": "fact", "state": "superseded"}, source="knowledge"), ("note", "owner", "archived"))
        self.assertEqual(k({"kind": "question", "state": "dropped"}), ("note", "owner", "void"))  # Guessed: a knowledge entry.
        self.assertEqual(k({"source": "owner", "kind": "constraint", "deleted": 0}), ("rule", "owner", "active"))
        self.assertEqual(k({"source": "owner", "kind": "fact"}, {"layer": "decision"}), ("decision", "owner", "active"))
        self.assertEqual(k({"source": "lead", "kind": "style"}), ("note", "summary", "active"))  # Never a rule (T908 M3b),
        self.assertEqual(k({"source": "owner_answer", "kind": "decision"}), ("decision", "owner", "active"))
        self.assertEqual(k({"source": "owner_answer", "kind": "decision"}, {"valid": "done"}), ("decision", "owner", "done"))
        self.assertEqual(k({"source": "owner_fix", "kind": "decision"}), ("decision", "owner", "active"))
        confirmed = {"asserted_by": turn_source.PARTY_OWNER}
        self.assertEqual(k({"source": "lead", "kind": "constraint"}, confirmed), ("rule", "owner", "active"))
        self.assertEqual(k({"source": "lead", "kind": "constraint"}), ("note", "summary", "active"))
        self.assertEqual(k({"source": "lead", "kind": "constraint"}, {"carried": True}), ("rule", "summary", "active"))
        self.assertEqual(k({"source": "lead", "kind": "decision"}, {"carried": True}), ("rule", "summary", "active"))
        self.assertEqual(k({"source": "lead", "kind": "fact"}, {"carried": True}), ("note", "summary", "active"))
        self.assertEqual(k({"source": "lead", "kind": "contact"}), ("note", "summary", "active"))
        self.assertEqual(k({"source": "lead", "kind": "constraint"}, {"valid": "superseded"}), ("note", "summary", "archived"))
        self.assertEqual(k({"source": "lead", "kind": "fact", "deleted": NOW}), ("note", "summary", "void"))
        self.assertEqual(k({"source": "owner", "kind": "constraint"}, source="topic"), ("rule", "owner", "active"))
        self.assertEqual(k({"source": "owner", "kind": "constraint", "deleted": NOW}, source="topic"), ("rule", "owner", "void"))
        self.assertEqual(k({"id": "c1", "text": "x"}, source="candidate"), ("rule", "summary", "pending"))


class StyleLineTest(Case):
    def test_the_style_line_is_no_entry_of_the_store(self):
        """project_style's line (汇报风格：…) stays the lead's own setting, read where it always was: no rule, no code, no entry
        of the store (T908 M3b), the project's other lines as before."""
        project_memory.add(self.state, PID, "汇报风格：只报里程碑和收尾", "style", "lead", now=NOW)
        rule = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="改完先跑全量", now=NOW)
        self.assertEqual([e.text for e in memstore.entries(self.state, scope="project", owner_key=PID, statuses=None)], [rule.text])
        self.assertEqual([row["kind"] for row in project_memory.stored(self.state, PID)], ["style", "constraint"])


class CodeTest(Case):
    def test_codes_are_crockford_unique_lengthened_when_taken_and_kept_after_a_restart(self):
        refs = [(f"project:{PID}:{index:06x}", ("rule", "note", "decision")[index % 3]) for index in range(600)]
        given = memstore.assign(self.state, refs)
        self.assertEqual(len(set(given.values())), 600)
        self.assertTrue(all(memstore.is_code(code) for code in given.values()))
        self.assertEqual({code[0] for ref, code in given.items() if ref.endswith("000")}, {"R"})
        memstore._cache.clear()
        self.assertEqual(memstore.assign(self.state, refs), given)  # Read back, the same.
        # A ref whose four-letter code another ref holds gets five letters.
        ref = "topic:t1:abc"
        four = next(memstore._candidates("rule", ref))
        data = json.loads(memstore.codes_path(self.state).read_text())
        data["codes"][four] = "global:gtaken"
        memstore.codes_path(self.state).write_text(json.dumps(data))
        code = memstore.assign(self.state, [(ref, "rule")])[ref]
        self.assertEqual(len(code), 6)
        self.assertTrue(code.startswith(four))
        reserved = memstore.code_for(self.state, "note")
        self.assertTrue(memstore.is_code(reserved) and reserved.startswith("N"))
        self.assertNotIn(reserved, given.values())
        with self.assertRaises(ValueError):
            memstore.code_for(self.state, "fact")

    def test_find_by_ref_code_old_id_and_a_turns_memory_number(self):
        entry = memstore.add(self.state, kind="rule", scope="global", text="提交前先跑测试", aliases=("principle:p1a2b3c",),
                             now=NOW)
        self.assertEqual(memstore.find(self.state, entry.ref), entry)
        self.assertEqual(memstore.find(self.state, entry.code), entry)
        self.assertEqual(memstore.find(self.state, entry.code.lower()), entry)
        self.assertEqual(memstore.find(self.state, "p1a2b3c"), entry)
        self.assertEqual(memstore.find(self.state, "principle:p1a2b3c"), entry)
        self.assertIsNone(memstore.find(self.state, "M12"))  # A memory number names nothing without its turn.
        self.assertEqual(memstore.find_in_turn(self.state, "M12", [{"n": "M12", "ref": entry.ref}]), entry)
        self.assertIsNone(memstore.find(self.state, "RZZZZ"))
        self.assertIsNone(memstore.find(self.state, ""))


class LazyTest(Case):
    def test_a_topic_is_read_only_when_named_or_indexed_and_its_codes_are_given_then(self):
        path = memstore.topic_memory_path(self.state, TID)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"entries": [{"id": "aa0001", "text": "这个话题只读 demo", "kind": "constraint",
                                                 "source": "owner", "at": NOW, "deleted": 0}]}))  # An older release's write.
        self.assertEqual([e for e in memstore.entries(self.state) if e.scope == "topic"], [])
        self.assertFalse(memstore.codes_path(self.state).exists())  # Nothing read, nothing given.
        (entry,) = memstore.entries(self.state, scope="topic", owner_key=TID)
        self.assertEqual((entry.ref, entry.kind, entry.said), (f"topic:{TID}:aa0001", "rule", "owner"))
        self.assertTrue(memstore.is_code(entry.code))
        memstore._cache.clear()
        self.assertEqual(memstore.find(self.state, entry.code), entry)
        topic_memory.add(self.state, TID, "第二条")  # A write lists the topic.
        self.assertIn(TID, memstore.topics(self.state))
        self.assertEqual(len([e for e in memstore.entries(self.state) if e.scope == "topic"]), 2)

    def test_reading_everything_never_lists_the_topic_directories(self):
        for index in range(30):
            folder = Path(self.state, "topics", f"{index:064x}")
            folder.mkdir(parents=True)
            (folder / "memory.json").write_text(json.dumps({"entries": []}))
        real = os.scandir

        def guard(path="."):
            if Path(path) == Path(self.state, "topics"):
                raise AssertionError("walked the topics")
            return real(path)
        with mock.patch("os.scandir", guard):
            memstore.entries(self.state, statuses=None)
        self.assertEqual(memstore.seed_topics(self.state, now=NOW), 30)  # The maintenance's one walk.


class WriteTest(Case):
    def test_add_in_every_scope_lands_in_the_store_every_release_reads_with_the_new_fields_beside(self):
        before = rev_mtime(self.state)
        g = memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文", title="用中文", why="主人说的", now=NOW)
        self.assertGreater(rev_mtime(self.state), before)
        p = memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="网页服务在 8080", said="summary",
                         by="lead", repos=("demo-repo",), now=NOW)
        t = memstore.add(self.state, kind="rule", scope="topic", owner_key=TID, text="这个话题不改 demo-repo", now=NOW)
        self.assertEqual((g.title, g.why, g.scope, g.status), ("用中文", "主人说的", "global", "active"))
        self.assertEqual((p.kind, p.said, p.by, p.repos), ("note", "summary", "lead", ("demo-repo",)))
        (line,) = project_memory.live(self.state, projects.read_record(self.state, PID))
        self.assertEqual((line["text"], line["kind"], line["source"]), ("网页服务在 8080", "fact", "lead"))
        self.assertEqual(topic_memory.live(self.state, TID)[0]["text"], "这个话题不改 demo-repo")
        meta = project_memory.meta_path(self.state, PID)
        self.assertFalse(meta.exists() and set().union(*(json.loads(meta.read_text())["entries"].values()))
                         - set(project_memory.META_KEYS))  # Nothing new in the file an older release rewrites.
        self.assertIn(p.ref.rsplit(":", 1)[1], memstore.sidecar(self.state, "project", PID))
        self.assertEqual(memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文。", now=NOW), g)  # Same.
        self.assertEqual({row["action"] for row in memstore.log_read(self.state)}, {"add"})
        self.assertIn(t.ref, {e.ref for e in memstore.entries(self.state)})
        with self.assertRaises(ValueError):
            memstore.add(self.state, kind="rule", scope="global", text="x" * 201)
        with self.assertRaises(ValueError):
            memstore.add(self.state, kind="decision", scope="global", text="走 add_decision")
        with self.assertRaises(ValueError):
            memstore.add(self.state, kind="rule", scope="project", owner_key="zzzzzz", text="没有这个专项")

    def test_edit_status_kind_move_and_undo(self):
        p = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="先跑测试再提交", now=NOW)
        with self.assertRaises(ValueError):
            memstore.edit(self.state, p.ref, text="改了", base="00000000")
        edited = memstore.edit(self.state, p.ref, text="先跑相关测试再提交", title="先测再提", base=memstore.rev_of(p),
                               token="u-edit", now=NOW + 1)
        self.assertEqual((edited.text, edited.title, edited.code), ("先跑相关测试再提交", "先测再提", p.code))
        archived = memstore.archive(self.state, p.ref, now=NOW + 2)
        self.assertEqual(archived.status, "archived")
        self.assertEqual(project_memory.stored(self.state, PID)[0]["valid"], "done")  # An older release reads it as history.
        self.assertEqual(memstore.restore(self.state, p.ref).status, "active")
        self.assertEqual(memstore.void(self.state, p.ref).status, "void")
        self.assertTrue(project_memory.stored(self.state, PID)[0]["deleted"])  # A tombstone for an older release.
        self.assertEqual(memstore.restore(self.state, p.ref).status, "active")
        note = memstore.set_kind(self.state, p.ref, "note", said="summary")
        self.assertEqual((note.kind, note.said, note.code), ("note", "summary", p.code))  # The code never changes.
        self.assertEqual(memstore.confirm(self.state, p.ref).said, "owner")
        # A topic's line moved to the project keeps its code; the old ref is an alias and archived.
        t = memstore.add(self.state, kind="note", scope="topic", owner_key=TID, text="上游接口超时", now=NOW)
        moved = memstore.move(self.state, t.ref, scope="project", owner_key=PID, token="u-move", now=NOW + 3)
        self.assertEqual((moved.code, moved.scope, moved.kind), (t.code, "project", "note"))
        self.assertIn(t.ref, moved.aliases)
        self.assertEqual(memstore.find(self.state, t.code), moved)
        old = memstore.find(self.state, t.ref)  # Its own ref still names it, archived.
        self.assertEqual((old.ref, old.status, old.superseded_by), (t.ref, "archived", moved.ref))
        self.assertEqual(memory_usage.counts(self.state), {})
        self.assertEqual(memstore.undo(self.state, "u-move"), [t.ref, moved.ref])
        (undone,) = [row for row in memstore.log_read(self.state) if row["action"] == "undo"]
        self.assertEqual(undone.get("title"), moved.title)  # 记录页写撤销了哪条（CUA 复验：原来是「」）。
        self.assertEqual(memstore.find(self.state, moved.ref).status, "void")
        restored = memstore.entries(self.state, scope="topic", owner_key=TID)[0]
        self.assertEqual((restored.status, restored.superseded_by), ("active", ""))
        with self.assertRaises(ValueError):
            memstore.undo(self.state, "u-move")  # Once.
        with self.assertRaises(ValueError):
            memstore.undo(self.state, "u-edit")  # The line changed after it.
        with self.assertRaises(ValueError):
            memstore.undo(self.state, "u-none")

    def test_undo_puts_back_a_global_edit_and_voids_what_an_add_made(self):
        g = memstore.add(self.state, kind="note", scope="global", text="演示环境监听 8080", token="u-add", now=NOW)
        memstore.edit(self.state, g.ref, text="演示环境监听 9090", token="u-e", now=NOW + 1)
        self.assertEqual(memstore.undo(self.state, "u-e"), [g.ref])
        self.assertEqual(memstore.find(self.state, g.ref).text, "演示环境监听 8080")
        self.assertEqual(memstore.undo(self.state, "u-add"), [g.ref])
        self.assertEqual(memstore.find(self.state, g.ref).status, "void")
        self.assertEqual([row.get("undid") for row in memstore.log_read(self.state)][:2], ["u-add", "u-e"])

    def test_supersede_and_promote(self):
        a = memstore.add(self.state, kind="rule", scope="global", text="部署前先问", now=NOW)
        b = memstore.add(self.state, kind="rule", scope="global", text="部署前先跑预检再问", now=NOW)
        old = memstore.supersede(self.state, a.ref, b.ref)
        self.assertEqual((old.status, old.superseded_by), ("archived", b.ref))
        with self.assertRaises(ValueError):
            memstore.supersede(self.state, b.ref, b.ref)
        t2 = memstore.add(self.state, kind="note", scope="topic", owner_key=TID, text="构建要先装依赖", now=NOW)
        up = memstore.promote(self.state, t2.ref, scope="project", owner_key=PID, repos=["demo-repo"])
        self.assertEqual((up.scope, up.repos, up.code), ("project", ("demo-repo",), t2.code))

    def test_old_writers_touch_the_sentinel_too_once_the_memory_folder_is_there(self):
        principles.save(self.state, principles.OWNER, "迁移前记的")
        self.assertFalse(memstore.memory_dir(self.state).exists())  # Never migrated, or the memory off: no new folder.
        memstore.touch_rev(self.state)
        for write in (lambda: principles.save(self.state, principles.OWNER, "以后先给结论"),
                      lambda: knowledge.save(self.state, knowledge.OWNER, "fact", "端口 8080"),
                      lambda: project_memory.add(self.state, PID, "负责人记的", "fact"),
                      lambda: topic_memory.add(self.state, TID, "话题里的")):
            path = memstore.rev_path(self.state)
            if path.exists():
                os.utime(path, (1, 1))
            write()
            self.assertGreater(path.stat().st_mtime, 1)


class ConfirmLegacyTest(Case):
    """T870 Wave 3.5 (Fable P1-2): a project's rule of the lead's the owner confirms (待审核 采纳) is his for the paths that read
    the old store too: the lead's project_memory_revise refuses it, core/memory.py describe says the owner asserted it, and the
    store maps it back to 你定的."""

    def test_a_confirmed_leads_rule_is_the_owners_on_the_old_paths(self):
        from covey.app import project_tools
        from covey.core import memory
        made = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="发网关镜像都先发预发", said="summary",
                            by="lead", now=NOW)
        row = lambda: next(r for r in project_memory.entries(self.state, projects.read_record(self.state, PID))  # noqa: E731
                           if r["text"] == "发网关镜像都先发预发")
        self.assertEqual(project_tools.revise_refusal("update", row()), "")  # The lead's own: the lead may change it.
        memstore.confirm(self.state, made.ref, now=NOW + 1)
        self.assertEqual(memory.describe(row())["asserted_by"], turn_source.PARTY_OWNER)
        self.assertIn("主人定的规矩", project_tools.revise_refusal("update", row()))
        memstore._cache.clear()
        again = memstore.find(self.state, made.ref)
        self.assertEqual((again.said, again.kind, again.status), ("owner", "rule", "active"))
        (logged,) = [row for row in memstore.log_read(self.state) if row["action"] == "confirm"]
        memstore.undo(self.state, logged["undo_token"], now=NOW + 2)  # The confirmation undone: the lead's again everywhere.
        memstore._cache.clear()
        self.assertEqual(memstore.find(self.state, made.ref).said, "summary")
        self.assertNotEqual(memory.describe(row())["asserted_by"], turn_source.PARTY_OWNER)
        self.assertEqual(project_tools.revise_refusal("update", row()), "")


class UnreadableTest(Case):
    """T870 Wave 3.5 (Fable P1-4): a memory file that is there but cannot be read (a network disk's passing error, a torn
    file) refuses the write; it was read as {} and written over whole with only the new entry."""

    def test_an_unreadable_file_is_not_written_over(self):
        memstore.add(self.state, kind="rule", scope="global", text="回复主人用中文", now=NOW)
        path = memstore.global_path(self.state)
        good = path.read_text()
        for broken in ('{"entries": [', "[1, 2]"):
            path.write_text(broken)
            memstore._cache.clear()
            with self.assertRaises(OSError):
                memstore.add(self.state, kind="rule", scope="global", text="部署前先跑预检", now=NOW + 1)
            self.assertEqual(path.read_text(), broken)
        path.write_text(good)
        memstore._cache.clear()
        memstore.add(self.state, kind="rule", scope="global", text="部署前先跑预检", now=NOW + 2)
        self.assertEqual(len(memstore.entries(self.state, scope="global")), 2)

    def test_the_codes_review_and_sidecar_files_too(self):
        made = memstore.add(self.state, kind="note", scope="project", owner_key=PID, text="网页服务在 8080", now=NOW)
        for path, write in ((memstore.codes_path(self.state), lambda: memstore.add(self.state, kind="note", scope="global",
                                                                                  text="测试机在 3 楼", now=NOW + 1)),
                            (memstore.review_path(self.state), lambda: memstore.review_put(self.state, {
                                "type": "learn", "title": "先给结论", "text": "回复先给结论"})),
                            (memstore.sidecar_path(self.state, "project", PID), lambda: memstore.edit(
                                self.state, made.ref, title="网页端口", now=NOW + 2))):
            with self.subTest(path=path.name):
                before = path.read_text() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{")
                memstore._cache.clear()
                try:
                    write()  # The codes are given lazily: an entry may go without one for now, the registry untouched.
                except OSError:
                    pass
                self.assertEqual(path.read_text(), "{")
                if before is None:
                    path.unlink()
                else:
                    path.write_text(before)
                memstore._cache.clear()

    def test_no_file_yet_is_an_empty_one(self):
        self.assertEqual(memstore.read_for_write(self.state / "memory" / "nothing.json"), {})


class AutoTitleTest(unittest.TestCase):
    def test_half_width_bang_and_semicolon_do_not_end_a_sentence_and_a_short_one_takes_the_next(self):
        """T870 Wave 3.5 (Fable P1-7): ``主人定（11-23）：!248 合进…`` was titled 主人定（11-23）; the title is what a rule past
        its room carries and what the page lists."""
        self.assertEqual(memstore.auto_title("主人定（11-23）：!248 合进主干后再部署。另外一句"), "主人定（11-23）：!248 合进主干后再部署")
        self.assertEqual(memstore.auto_title("a; b; c 都要先跑测试。再说"), "a; b; c 都要先跑测试")
        self.assertEqual(memstore.auto_title("短。也短。第三句话够长了。"), "短。也短。第三句话够长了")
        self.assertEqual(memstore.auto_title("部署前先在预发环境验证。别的"), "部署前先在预发环境验证")
        self.assertEqual(memstore.auto_title("主人答：现在部署吗？→部署（推荐）"), "现在部署吗？")
        self.assertLessEqual(len(memstore.auto_title("很长" * 60)), memstore.TITLE_CHARS)


class DecisionTest(Case):
    def test_old_answers_and_new_decisions_read_as_one(self):
        record = {"token": "0123456789abcdef", "topic_id": TID, "job_id": 3, "questions": [], "answers": {}}
        project_memory.note_answers(self.state, PID, record, [{"index": 1, "item": "deploy", "text": "主人答：现在部署吗？→先不"}],
                                    now=NOW)
        new = memstore.add_decision(self.state, scope="project", owner_key=PID, question="合入 main 吗？", answer="合入",
                                    question_ref="fedcba9876543210-1", topic_key=TID, now=NOW + 1)
        again = memstore.add_decision(self.state, scope="project", owner_key=PID, question="合入 main 吗？", answer="合入",
                                      question_ref="fedcba9876543210-1", now=NOW + 2)
        self.assertEqual(again.ref, new.ref)
        found = memstore.decisions(self.state, "project", PID)
        self.assertEqual(len(found), 2)
        old = next(e for e in found if e.ref != new.ref)
        self.assertEqual((old.ref, old.title, old.said), (f"decision:{PID}:0123456789abcdef-1", "现在部署吗？", "owner"))
        self.assertEqual(memstore.find(self.state, f"project:{PID}:0123456789abcdef-1"), old)  # The ref a turn named it by.
        self.assertEqual((new.title, new.text, new.origin["question"]), ("合入 main 吗？", "合入 main 吗？→合入",
                                                                         "fedcba9876543210-1"))
        done = memstore.settle(self.state, new.ref, now=NOW + 3)
        self.assertEqual(done.status, "done")
        self.assertEqual(memstore.settle(self.state, old.ref).status, "done")
        self.assertEqual(project_memory.stored(self.state, PID)[0]["valid"], "done")
        topic = memstore.add_decision(self.state, scope="topic", owner_key=TID, question="先写测试？", answer="是", now=NOW)
        self.assertEqual(memstore.find(self.state, topic.code), topic)
        self.assertIn(TID, memstore.topics(self.state))
        self.assertTrue(memstore.decisions_path(self.state, "topic", TID).is_file())


class QueueTest(Case):
    def test_review_items_once_each_and_their_states(self):
        item = {"type": "learn", "text": "回复先给结论", "suggest": {"kind": "rule", "scope": "global"}, "quote": "先说结论"}
        key, new = memstore.review_put(self.state, item, now=NOW)
        self.assertTrue(new and key.startswith("review:learn:"))
        self.assertEqual(memstore.review_put(self.state, {**item, "text": "回复先给结论。"}, now=NOW)[0], key)
        self.assertEqual(memstore.review_items(self.state)[0]["title"], "回复先给结论")
        done = memstore.review_set(self.state, key, "accepted", by="owner", refs=["global:g1"], now=NOW)
        self.assertEqual((done["state"], done["decided_by"], done["refs"]), ("accepted", "owner", ["global:g1"]))
        self.assertEqual(memstore.review_items(self.state), [])
        self.assertIsNone(memstore.review_set(self.state, "review:learn:none", "rejected"))
        with self.assertRaises(ValueError):
            memstore.review_put(self.state, {"type": "other", "text": "x"})

    def test_an_expired_item_lets_the_same_proposal_in_again_and_expiring_is_one_write(self):
        item = {"type": "learn", "text": "回复先给结论", "suggest": {"kind": "rule", "scope": "global"}}
        old, _ = memstore.review_put(self.state, {**item, "created_at": NOW - 15 * 86400}, now=NOW)
        kept, _ = memstore.review_put(self.state, {**item, "text": "新的一条", "created_at": NOW - 86400}, now=NOW)
        with mock.patch.object(memstore, "write_json", wraps=memstore.write_json) as wrote:
            self.assertEqual(memstore.review_expire(self.state, now=NOW), 1)
        self.assertEqual(wrote.call_count, 1)
        self.assertEqual(memstore.review_expire(self.state, now=NOW), 0)
        again, new = memstore.review_put(self.state, item, now=NOW)
        self.assertTrue(new)
        self.assertNotEqual(again, old)
        self.assertEqual({row["key"] for row in memstore.review_items(self.state)}, {again, kept})



class UpkeepTest(Case):
    def test_the_daily_upkeep_trims_the_log_compacts_decisions_and_lists_the_topics(self):
        old = memstore.add_decision(self.state, scope="project", owner_key=PID, question="旧题？", answer="作废了", now=NOW)
        memstore.void(self.state, old.ref, now=NOW)
        kept = memstore.add_decision(self.state, scope="project", owner_key=PID, question="新题？", answer="照做", now=NOW)
        memstore.settle(self.state, kept.ref, now=NOW + 1)
        memstore.edit(self.state, kept.ref, title="新题", now=NOW + 2)
        path = memstore.decisions_path(self.state, "project", PID)
        self.assertEqual(len(path.read_text().splitlines()), 5)
        topic_memory.add(self.state, TID, "话题里的")
        memstore.topics_path(self.state).unlink()  # Lost: the walk lists it again.
        out = memstore.upkeep(self.state, now=NOW + 100 * 86400)
        self.assertEqual(out, {"log": 5, "decisions": 4, "topics": 1, "expired": 0})
        (line,) = [json.loads(text) for text in path.read_text().splitlines()]
        self.assertEqual((line["op"], line["title"], line["status"]), ("add", "新题", "done"))
        self.assertEqual(memstore.find(self.state, kept.code).title, "新题")
        self.assertIsNone(memstore.find(self.state, old.ref))
        self.assertIn(TID, memstore.topics(self.state))
        self.assertEqual(memstore.log_read(self.state), [])
        self.assertEqual(memstore.upkeep(self.state, now=NOW + 100 * 86400), {"log": 0, "decisions": 0, "topics": 1, "expired": 0})


class UsageTest(Case):
    def test_new_counts_lie_beside_the_old_file_and_an_old_ref_counts_on_its_entry(self):
        memory_usage.note(self.state, ["principle:p1a2b3c"], now=NOW)
        g = memstore.add(self.state, kind="rule", scope="global", text="先给结论", aliases=("principle:p1a2b3c",), now=NOW)
        memory_usage.carried(self.state, [g.ref])
        memory_usage.carried(self.state, [g.ref])
        memory_usage.listed(self.state, [g.ref])
        memory_usage.signal(self.state, [g.ref], "memory_used")
        memory_usage.signal(self.state, [g.ref], "show")
        with self.assertRaises(ValueError):
            memory_usage.signal(self.state, [g.ref], "retell")
        row = memory_usage.counts(self.state)[g.ref]
        self.assertEqual((row["e"], row["t"], row["h"], row["n"], row["last"]), (2, 1, 1.5, 1, NOW))
        self.assertEqual(set(json.loads(memory_usage.path(self.state).read_text())["refs"]), {"principle:p1a2b3c"})


if __name__ == "__main__":
    unittest.main()

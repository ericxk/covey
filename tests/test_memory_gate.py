"""The write gate of a memory entry (core/memory_gate.py): time words and a live state refused or rewritten, vague pointers
only flagged, a decision never refused. Its write and revise (WAVE12 C8, W1-0) pass through to core/memstore.py: the files
they write are byte for byte the same call's there; and app/memory_proof.py, the only maker of a Proven."""
from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from covey.core.memory_gate import accept, check


class GateTest(unittest.TestCase):
    def test_live_state_is_refused_with_no_rewrite(self):
        verdict = check("现网第 459 代", kind="note")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.text, "")
        self.assertIn("dynamic", verdict.flags)
        self.assertTrue(verdict.reason)

    def test_leading_time_word_is_rewritten(self):
        for text, want in (("从今天起回复用中文", "回复用中文"), ("今天开始部署要先问", "部署要先问"), ("目前只用中文回复", "只用中文回复"),
                           ("现在，先问再做", "先问再做")):
            verdict = check(text, kind="rule")
            self.assertFalse(verdict.ok, text)
            self.assertEqual(verdict.text, want)

    def test_time_word_that_cannot_go_is_refused_with_a_reason(self):
        for text in ("今天要先问", "现在的流程是A", "刚才说的那样办"):
            verdict = check(text, kind="rule")
            self.assertFalse(verdict.ok, text)
            self.assertEqual(verdict.text, "")
            self.assertIn("会过时", verdict.reason)

    def test_words_not_in_the_list_pass(self):
        # "上午" is not one of TIME_WORDS and nothing else matches: it passes (the gate keeps to the two hard cases).
        verdict = check("按上午已优化的方式处理", kind="rule")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.flags, ())

    def test_commit_and_clock_time_are_not_refused_or_flagged(self):
        verdict = check("11-26 15:21 授权推送 a1b2c3d", kind="note")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.flags, ())

    def test_vague_pointer_is_flagged_only(self):
        verdict = check("这个专项的部署要先问", kind="rule")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.flags, ("vague",))

    def test_clear_rule_passes_clean(self):
        verdict = check("回复主人用中文", kind="rule")
        self.assertTrue(verdict.ok)
        self.assertEqual((verdict.text, verdict.flags), ("回复主人用中文", ()))

    def test_an_automatic_write_keeps_the_rewrite_or_nothing(self):
        self.assertEqual(accept("从今天起回复主人用中文", kind="rule")[0], "回复主人用中文")
        self.assertEqual(accept("现网第 459 代是稳定版", kind="rule")[0], "")
        self.assertEqual(accept("回复主人用中文", kind="rule")[0], "回复主人用中文")

    def test_decision_is_never_refused(self):
        verdict = check("今天部署吗？现网第 459 代，这个先不动", kind="decision")
        self.assertTrue(verdict.ok)
        self.assertEqual(set(verdict.flags), {"vague", "dynamic"})



NOW = 1_800_000_000.0
PID, TOPIC = "p" + "1" * 11, "t" * 64


def files(state: Path) -> dict:
    return {path.relative_to(state).as_posix(): path.read_bytes() for path in sorted(state.rglob("*")) if path.is_file()}


class PassThroughTest(unittest.TestCase):
    """W1-0: write and revise are core/memstore.py's writes under another name; nothing calls them yet (W1-D will)."""

    def pinned(self):
        counter = itertools.count(1)
        return (mock.patch("secrets.token_hex", side_effect=lambda n=32: f"{next(counter):0{2 * n}x}"),
                mock.patch("secrets.token_bytes", side_effect=lambda n=32: next(counter).to_bytes(n, "big")),
                mock.patch("time.time", return_value=NOW))

    def same(self, before, through, direct):
        """The files ``through`` writes (after ``before``) in one state directory, and ``direct`` in another: equal, byte for
        byte. ``through``'s result."""
        results, trees = [], []
        for act in (through, direct):
            with tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                patches = self.pinned()
                with patches[0], patches[1], patches[2]:
                    made = before(state)
                    results.append(act(state, made))
                trees.append(files(state))
        self.assertEqual(sorted(trees[0]), sorted(trees[1]))
        for name in trees[0]:
            with self.subTest(file=name):
                self.assertEqual(trees[0][name], trees[1][name])
        self.assertGreater(len(trees[0]), 0)
        return results[0]

    @staticmethod
    def project(state):
        from covey.core import projects

        projects.create(state, projects.new_record(chat_id="oc_chat", name="迁移账单", project_id=PID, now=NOW))
        return None

    def test_a_write_is_memstores_add_or_add_decision(self):
        from covey.core import memory_gate, memstore, turn_source
        from covey.core.memory_gate import Proof, Proven

        owner = Proven(Proof(message_id="om_1", quote="以后部署都先问我一句", via="butler"), turn_source.LEVEL_OWNER)
        cases = (
            # The owner's words, proven: his. Global, through the butler.
            (dict(kind="rule", scope="global", text="部署前先问主人", title="部署先问", why="主人说的", repos=("covey",), proof=owner,
                  by="butler", source="memory_save", place="butler"),
             lambda state: memstore.add(state, kind="rule", scope="global", text="部署前先问主人", title="部署先问", why="主人说的",
                                        said="owner", by="butler", repos=("covey",), who="butler",
                                        origin={"message_id": "om_1", "quote": "以后部署都先问我一句", "via": "butler"},
                                        because={"source": "memory_save", "place": "butler"}, now=NOW)),
            # No proof: a summary of the lead's, in the project.
            (dict(kind="note", scope="project", owner_key=PID, text="账单表按日期分区", proof=None, by="lead",
                  source="project_remember", place="lead"),
             lambda state: memstore.add(state, kind="note", scope="project", owner_key=PID, text="账单表按日期分区",
                                        said="summary", by="lead", origin={}, who="lead",
                                        because={"source": "project_remember", "place": "lead"}, now=NOW)),
            # An agent's level is no proof either; a topic's rule, redacted.
            (dict(kind="rule", scope="topic", owner_key=TOPIC, text="只改测试目录", proof=Proven(Proof(message_id="om_2"), "agent"),
                  by="lead", source="x", place="topic", redacted=True),
             lambda state: memstore.add(state, kind="rule", scope="topic", owner_key=TOPIC, text="只改测试目录", said="summary",
                                        by="lead", origin={"message_id": "om_2"}, who="lead", redacted=True,
                                        because={"source": "x", "place": "topic"}, now=NOW)),
            # A decision with its fields.
            (dict(kind="decision", scope="project", owner_key=PID, text="主人答：范围？→全部", proof=owner, by="owner",
                  source="answer", place="topic", decision={"question": "范围？", "answer": "全部", "question_ref": "ab" * 8 + "-1",
                                                            "category": "other", "ignored": "x"}),
             lambda state: memstore.add_decision(state, scope="project", owner_key=PID, text="主人答：范围？→全部", said="owner",
                                                 by="owner", question="范围？", answer="全部", question_ref="ab" * 8 + "-1",
                                                 category="other", who="owner", because={"source": "answer", "place": "topic"},
                                                 origin={"message_id": "om_1", "quote": "以后部署都先问我一句", "via": "butler"},
                                                 now=NOW)),
        )
        for given, direct in cases:
            with self.subTest(kind=given["kind"], scope=given["scope"]):
                written = self.same(self.project, lambda state, _: memory_gate.write(state, now=NOW, **given),
                                    lambda state, _: direct(state))
                self.assertEqual((written.said, written.scope, written.review, written.reason),
                                 ("owner" if given["proof"] is owner else "summary", given["scope"], "", ""))
                self.assertEqual(written.entry.text, given["text"])

    def test_a_write_that_supersedes_is_add_then_supersede(self):
        from covey.core import memory_gate, memstore

        def before(state):
            self.project(state)
            return memstore.add(state, kind="rule", scope="project", owner_key=PID, text="旧的约定", said="summary", by="lead",
                                now=NOW).ref

        def direct(state, old):
            new = memstore.add(state, kind="rule", scope="project", owner_key=PID, text="新的约定", said="summary", by="lead",
                               origin={}, who="lead", because={"source": "s", "place": "lead"}, now=NOW)
            memstore.supersede(state, old, new.ref, who="lead", because={"source": "s", "place": "lead"}, now=NOW)

        self.same(before, lambda state, old: memory_gate.write(state, kind="rule", scope="project", owner_key=PID, text="新的约定",
                                                               proof=None, by="lead", source="s", place="lead", supersedes=old,
                                                               now=NOW), direct)

    def test_a_revision_is_memstores_edit_supersede_settle_archive_or_void(self):
        from covey.core import memory_gate, memstore

        def before(state):
            self.project(state)
            rule = memstore.add(state, kind="rule", scope="project", owner_key=PID, text="账单表按日期分区", said="summary",
                                by="lead", now=NOW).ref
            other = memstore.add(state, kind="rule", scope="project", owner_key=PID, text="账单表按租户分区", said="summary",
                                 by="lead", now=NOW).ref
            decision = memstore.add_decision(state, scope="project", owner_key=PID, text="主人答：范围？→全部", now=NOW).ref
            return rule, other, decision

        because = {"source": "project_memory_revise"}
        cases = (
            ("update", 0, dict(text="账单表按月分区", why="改了"),
             lambda state, refs: memstore.edit(state, refs[0], text="账单表按月分区", why="改了", who="lead", because=because)),
            ("supersede", 0, dict(by_ref=None),
             lambda state, refs: memstore.supersede(state, refs[0], refs[1], who="lead", because=because)),
            ("settle", 2, {}, lambda state, refs: memstore.settle(state, refs[2], who="lead", because=because)),
            ("archive", 0, {}, lambda state, refs: memstore.archive(state, refs[0], who="lead", because=because)),
            ("void", 0, {}, lambda state, refs: memstore.void(state, refs[0], who="lead", because=because)),
            ("retire", 2, {}, lambda state, refs: memstore.settle(state, refs[2], who="lead", because=because)),
            ("retire", 0, {}, lambda state, refs: memstore.archive(state, refs[0], who="lead", because=because)),
        )
        for action, which, extra, direct in cases:
            with self.subTest(action=action, which=which):
                def through(state, refs, action=action, which=which, extra=extra):
                    extra = {**extra, "by_ref": refs[1]} if "by_ref" in extra else extra
                    return memory_gate.revise(state, refs[which], action, proof=None, by="lead",
                                              source="project_memory_revise", **extra)

                written = self.same(before, through, direct)
                self.assertIsNotNone(written.entry)

    def test_a_supersede_with_words_writes_the_new_entry_first(self):
        from covey.core import memory_gate, memstore

        def before(state):
            self.project(state)
            return memstore.add(state, kind="note", scope="project", owner_key=PID, text="旧资料", said="summary", by="lead",
                                now=NOW).ref

        def direct(state, old):
            new = memstore.add(state, kind="note", scope="project", owner_key=PID, text="新资料", said="summary", by="lead",
                               origin={}, who="lead", because={"source": "s"}, now=None)
            memstore.supersede(state, old, new.ref, who="lead", because={"source": "s"})

        written = self.same(before, lambda state, old: memory_gate.revise(state, old, "supersede", proof=None, by="lead",
                                                                          source="s", text="新资料"), direct)
        self.assertEqual(written.entry.text, "新资料")

    def test_an_action_it_does_not_know_writes_nothing(self):
        from covey.core import memory_gate

        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            memory_gate.revise(Path(temp), "global:g1", "delete", proof=None, by="lead", source="s")
        self.assertEqual(set(memory_gate.REVISIONS), {"update", "supersede", "settle", "archive", "void"})


class MemoryProofTest(unittest.TestCase):
    class Access:
        def __init__(self, owner="ou_owner", fails=False):
            self.owner, self.fails = owner, fails

        def is_owner(self, open_id):
            if self.fails:
                raise RuntimeError("cannot tell")
            return open_id == self.owner

    def test_the_owners_click_proves_itself_and_nobody_elses_does(self):
        from covey.app import memory_proof
        from covey.core import turn_source
        from covey.core.memory_gate import Proof, Proven

        self.assertEqual(memory_proof.by_click(self.Access(), "ou_owner", "card"),
                         Proven(Proof(via="card"), turn_source.LEVEL_OWNER))
        for access, open_id in ((self.Access(), "ou_member"), (self.Access(), ""), (self.Access(fails=True), "ou_owner")):
            with self.subTest(open_id=open_id):
                self.assertIsNone(memory_proof.by_click(access, open_id, "card"))

    def test_a_proof_of_words_proves_nothing_yet(self):
        from covey.app import memory_proof
        from covey.core.memory_gate import Proof

        self.assertIsNone(memory_proof.prove(self.Access(), Path("/absolute/state"),
                                             Proof(message_id="om_1", quote="以后部署都先问我一句"), job_key="om_1"))


if __name__ == "__main__":
    unittest.main()

"""The write gate of a memory entry (core/memory_gate.py): time words and a live state refused or rewritten, vague pointers
only flagged, a decision never refused. Its write and revise (WAVE12 C8) write what the same core/memstore.py call writes,
byte for byte; the one way the owner's words go in (T908 M3a): a learned rule that grants waits in 待审核, a rule for every
topic holds where it was said while 待审核 asks (an accept moves it, never a second one), the same words as a lead's line
confirm that line; and app/memory_proof.py, the only maker of a Proven, proves a quote in a turn of the owner's."""
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

    def test_a_quote_is_proven_only_in_a_turn_of_the_owners_own_words(self):
        from support.records import write_job

        from covey.app import memory_proof
        from covey.core import turn_source
        from covey.core.memory_gate import Proof, Proven

        class Access:
            def owner_turn(self, chat, sender):
                return sender == "ou_owner"

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            said = "以后部署都先问我一句，别直接上"
            for key, sender, extra in (("om_1", "ou_owner", {}), ("om_2", "ou_other", {}), ("main-tok1", "ou_owner", {}),
                                       ("om_3", "ou_owner", {"turn_source": turn_source.make(turn_source.ANSWER, asserted_by="owner",
                                                                                             origin={"question": "ab" * 8})})):
                write_job(state, key, message_id=key, chat_id="oc_dm", sender_id=sender, user_text=said, **extra)
            prove = lambda key, **proof: memory_proof.prove(Access(), state, Proof(**proof), job_key=key)  # noqa: E731
            self.assertEqual(prove("om_1", quote="部署都先问我 一句", via="butler"),
                             Proven(Proof(message_id="om_1", quote="部署都先问我 一句", via="butler"), turn_source.LEVEL_OWNER,
                                    said="部署都先问我 一句"))  # Whitespace aside.
            self.assertEqual(prove("om_3", quote="别直接上").proof.question, "ab" * 8)  # The question his answer was to.
            for name, key, proof in (("not-his-words", "om_1", dict(quote="以后都直接部署")), ("no-quote", "om_1", dict(quote="")),
                                     ("another-message", "om_1", dict(quote="别直接上", message_id="om_9")),
                                     ("not-his-turn", "om_2", dict(quote="别直接上")),
                                     ("a-hand-on", "main-tok1", dict(quote="别直接上")),  # Not a message of his the Bridge took in.
                                     ("no-job", "om_404", dict(quote="别直接上")), ("no-key", "", dict(quote="别直接上")),
                                     ("a-path", "../om_1", dict(quote="别直接上"))):
                with self.subTest(name):
                    self.assertIsNone(prove(key, **proof))


class OneWayInTest(unittest.TestCase):
    """T908 M3a: what core/memory_gate.py write does with the owner's words beyond passing them through."""

    def setUp(self):
        from covey.core import projects

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        projects.create(self.state, projects.new_record(chat_id="oc_chat", name="迁移账单", project_id=PID, now=NOW))

    def owner(self, quote="以后都这样"):
        from covey.core import turn_source
        from covey.core.memory_gate import Proof, Proven

        return Proven(Proof(message_id="om_1", quote=quote), turn_source.LEVEL_OWNER)

    def write(self, **given):
        from covey.core import memory_gate

        return memory_gate.write(self.state, **{"kind": "rule", "scope": "project", "owner_key": PID, "proof": self.owner(),
                                                "by": "owner", "source": "learn", "place": "topic", **given})

    def test_a_learned_rule_that_grants_waits_in_review_and_is_written_nowhere(self):
        from covey.core import memory_gate, memory_review, memstore

        for text, grant in (("改完直接部署到本机", "deploy"), ("旧分支直接删掉", "delete"), ("额度不够直接扣共享池", "额度")):
            with self.subTest(grant):
                written = self.write(text=text, learned=True, origin={"topic_key": "t1"})
                self.assertEqual((written.entry, written.reason, written.grant, written.fresh), (None, memory_gate.GRANT, grant, True))
                item = next(item for item in memory_review.items(self.state) if item.key == written.review)
                self.assertEqual((item.type, item.suggest, item.quote, item.origin),
                                 ("learn", {"kind": "rule", "scope": "project", "project_key": PID, "grant": grant}, "以后都这样",
                                  {"topic_key": "t1"}))
        self.assertEqual(memstore.entries(self.state), [])
        # Not learned (the butler's, the owner's click), or a note: written as it is.
        self.assertIsNotNone(self.write(text="改完直接部署到本机").entry)
        self.assertIsNotNone(self.write(kind="note", text="部署脚本在 scripts 下", learned=True).entry)
        # Accepted, it holds, his: the one way a learned rule that grants comes into force.
        key = next(item.key for item in memory_review.items(self.state) if item.suggest.get("grant") == "delete")
        done = memory_review.decide(self.state, key, "accept")
        found = memstore.find(self.state, done["refs"][0])
        self.assertEqual((found.text, found.said, found.scope, found.status), ("旧分支直接删掉", "owner", "project", "active"))

    def test_a_rule_for_every_topic_holds_where_it_was_said_and_an_accept_moves_that_line(self):
        from covey.core import memory_review, memstore

        written = self.write(text="回复都用中文", wider="global")
        self.assertEqual((written.entry.scope, written.entry.said, written.fresh), ("project", "owner", True))
        [item] = memory_review.items(self.state)
        self.assertEqual((item.key, item.suggest, item.items), (written.review, {"kind": "rule", "scope": "global"},
                                                                (written.entry.ref,)))
        self.assertEqual(self.write(text="回复都用中文", wider="global").fresh, False)  # Asked once.
        done = memory_review.decide(self.state, written.review, "accept")
        [moved] = memstore.entries(self.state, scope="global")
        self.assertEqual((done["refs"], moved.code, moved.text), ([moved.ref], written.entry.code, "回复都用中文"))  # Moved: one line.
        self.assertEqual(memstore.find(self.state, written.entry.ref).status, "archived")
        self.assertEqual([entry.text for entry in memstore.entries(self.state, scope="project", owner_key=PID)], [])
        # Never for a line from a topic with redaction on.
        marked = self.write(text="回复先给结论", wider="global", redacted=True)
        self.assertEqual((marked.review, len(memory_review.items(self.state))), ("", 0))

    def test_a_refused_text_is_rewritten_unless_strict_and_pending_writes_nothing(self):
        from covey.core import memory_gate, memory_review, memstore

        self.assertEqual(self.write(text="从今天起回复用中文").entry.text, "回复用中文")
        strict = self.write(text="今天起汇报先给结论", strict=True)
        self.assertEqual((strict.entry, strict.rewrite), (None, "汇报先给结论"))
        self.assertIn("会过时", strict.reason)
        refused = self.write(text="现网第 9 代")
        self.assertEqual((refused.entry, refused.rewrite), (None, ""))
        held = self.write(scope="global", owner_key="", text="回复先给结论", by="butler", pending=True)
        self.assertEqual((held.entry, held.reason), (None, memory_gate.PENDING))
        self.assertEqual([item.suggest for item in memory_review.items(self.state)], [{"kind": "rule", "scope": "global"}])
        self.assertEqual([entry.text for entry in memstore.entries(self.state)], ["回复用中文"])

    def test_the_owners_words_the_same_as_a_summary_confirm_it_where_it_is(self):
        from covey.core import memstore

        for scope, owner in (("global", ""), ("project", PID), ("topic", "t1")):
            with self.subTest(scope):
                summed = memstore.add(self.state, kind="rule", scope=scope, owner_key=owner, text=f"合并前先跑冒烟（{scope}）",
                                      said="summary", by="lead")
                again, how = memstore.put(self.state, kind="rule", scope=scope, owner_key=owner, text=f"合并前先跑冒烟（{scope}）",
                                          said="summary", by="lead")
                self.assertEqual((again.ref, again.said, how), (summed.ref, "summary", memstore.SAME))
                written = self.write(scope=scope, owner_key=owner, text=f"合并前先跑冒烟（{scope}）")
                self.assertEqual((written.entry.ref, written.entry.code, written.entry.said, written.done),
                                 (summed.ref, summed.code, "owner", memstore.CONFIRMED))
                if scope != "topic":  # A topic's line keeps no origin of its own.
                    self.assertEqual(written.entry.origin.get("message_id"), "om_1")
                self.assertEqual(memstore.log_read(self.state)[0]["action"], "confirm")
                self.assertEqual(self.write(scope=scope, owner_key=owner, text=f"合并前先跑冒烟（{scope}）").done, memstore.SAME)
                self.assertEqual(len(memstore.entries(self.state, scope=scope, owner_key=owner)), 1)


if __name__ == "__main__":
    unittest.main()

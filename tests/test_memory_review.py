"""待审核 (core/memory_review.py): what each kind of item writes when the owner decides it, one undo token over a whole
decision, whether a proposal is new, and the 14 days (closed by the daily upkeep)."""
from __future__ import annotations

import unittest

from support.records import state_dir

from covey.core import memory_review, memstore, projects
from covey.core.memory_review import ReviewItem

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
NOW = 1_800_000_000.0
DAY = 86400


def add(state, row) -> str:
    """The key of ``row`` put in 待审核 (core/memory_review.py add gives (key, new))."""
    return memory_review.add(state, row)[0]


def item(kind="learn", text="回复先给结论", **fields):
    suggest = fields.pop("suggest", {"kind": "rule", "scope": "global"})
    return ReviewItem(key="", type=kind, title=fields.pop("title", ""), text=text, suggest=suggest,
                      created_at=fields.pop("created_at", NOW), **fields)


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        memstore._cache.clear()
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="记忆专项", project_id=PID, now=NOW))

    def lead_rule(self, text="部署前先问"):
        return memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text=text, said="summary", by="lead", now=NOW)


class DecideTest(Case):
    def test_add_does_not_repeat_a_proposal_and_says_whether_it_is_new(self):
        one, new = memory_review.add(self.state, item())
        self.assertTrue(new)
        self.assertEqual(memory_review.add(self.state, item()), (one, False))
        self.assertEqual([row.key for row in memory_review.items(self.state)], [one])

    def test_a_proposal_turned_down_or_accepted_stays_out_but_an_expired_one_may_come_again(self):
        down = add(self.state, item("learn", "被拒的"))
        memory_review.decide(self.state, down, "reject", now=NOW)
        self.assertEqual(memory_review.add(self.state, item("learn", "被拒的")), (down, False))
        taken = add(self.state, item("learn", "采纳的"))
        memory_review.decide(self.state, taken, "accept", now=NOW)
        self.assertEqual(memory_review.add(self.state, item("learn", "采纳的")), (taken, False))
        old = add(self.state, item("learn", "没人理的", created_at=NOW - 15 * DAY))
        memory_review.expire(self.state, now=NOW)
        again, new = memory_review.add(self.state, item("learn", "没人理的"))
        self.assertTrue(new)
        self.assertNotEqual(again, old)
        self.assertEqual([row.key for row in memory_review.items(self.state)], [again])

    def test_migrate_accept_makes_it_the_owners_and_reject_makes_it_a_note(self):
        a, b = self.lead_rule("部署前先问"), self.lead_rule("发布走灰度")
        suggest = {"kind": "rule", "scope": "project", "project_key": PID}
        ka = add(self.state, item("migrate", a.text, suggest=suggest, items=(a.ref,)))
        kb = add(self.state, item("migrate", b.text, suggest=suggest, items=(b.ref,)))
        done = memory_review.decide(self.state, ka, "accept", now=NOW)
        self.assertEqual((done["state"], done["refs"]), ("accepted", [a.ref]))
        self.assertEqual(memstore.find(self.state, a.ref).said, "owner")
        self.assertEqual(memory_review.decide(self.state, kb, "reject", now=NOW)["state"], "rejected")
        self.assertEqual(memstore.find(self.state, b.ref).kind, "note")
        self.assertEqual(memory_review.items(self.state), [])

    def test_learn_accept_adds_an_entry_the_owner_said_and_reject_writes_nothing(self):
        key = add(self.state, item("learn", "回复先给结论", suggest={"kind": "rule", "scope": "project", "project_key": PID}))
        done = memory_review.decide(self.state, key, "accept", now=NOW)
        entry = memstore.find(self.state, done["refs"][0])
        self.assertEqual((entry.said, entry.by, entry.scope, entry.owner_key, entry.text), ("owner", "learn", "project", PID, "回复先给结论"))
        other = add(self.state, item("lead", "不要擅自推送"))
        self.assertEqual(memory_review.decide(self.state, other, "reject", now=NOW)["refs"], [])
        self.assertIsNone(next((e for e in memstore.entries(self.state) if e.text == "不要擅自推送"), None))

    def conflict(self, action, new):
        old = memstore.add(self.state, kind="rule", scope="global", text="旧写法", now=NOW)
        key = add(self.state, item("conflict", new, conflict_with=old.ref))
        memory_review.decide(self.state, key, action, now=NOW)
        texts = [e.text for e in memstore.entries(self.state)]
        return memstore.find(self.state, old.ref).text, texts

    def test_conflict_keep_left_drops_the_new_words(self):
        old, texts = self.conflict("keep_left", "新写法甲")
        self.assertEqual((old, texts), ("旧写法", ["旧写法"]))

    def test_conflict_keep_right_takes_the_new_words_in_the_old_entry(self):
        old, texts = self.conflict("keep_right", "新写法乙")
        self.assertEqual((old, texts), ("新写法乙", ["新写法乙"]))

    def test_conflict_keep_both_makes_the_new_words_an_entry(self):
        old, texts = self.conflict("keep_both", "新写法丙")
        self.assertEqual((old, sorted(texts)), ("旧写法", sorted(["旧写法", "新写法丙"])))

    def test_tidy_reject_puts_the_lines_back(self):
        one = memstore.add(self.state, kind="note", scope="global", text="某条资料", now=NOW)
        memstore.archive(self.state, one.ref, now=NOW)
        key = add(self.state, item("tidy", "收起了一条", items=(one.ref,)))
        self.assertEqual(memory_review.decide(self.state, key, "reject", now=NOW)["refs"], [one.ref])
        self.assertEqual(memstore.find(self.state, one.ref).status, "active")
        again = memstore.add(self.state, kind="note", scope="global", text="另一条资料", now=NOW)
        memstore.archive(self.state, again.ref, now=NOW)
        key = add(self.state, item("tidy", "又收起一条", items=(again.ref,)))
        memory_review.decide(self.state, key, "accept", now=NOW)
        self.assertEqual(memstore.find(self.state, again.ref).status, "archived")

    def test_an_import_an_older_release_left_is_written_as_it_reads_or_let_go(self):
        a = add(self.state, item("import", "导入甲", items=("fp1",)))
        b = add(self.state, item("import", "导入乙", items=("fp2",)))
        done = memory_review.decide(self.state, a, "accept", who="owner", token="t1", now=NOW)
        self.assertEqual(memstore.find(self.state, done["refs"][0]).text, "导入甲")
        self.assertEqual(memory_review.decide(self.state, b, "reject", now=NOW)["refs"], [])

    def test_decided_again_or_unknown_raises(self):
        key = add(self.state, item())
        memory_review.decide(self.state, key, "reject", now=NOW)
        with self.assertRaises(ValueError):
            memory_review.decide(self.state, key, "accept", now=NOW)
        with self.assertRaises(ValueError):
            memory_review.decide(self.state, "review:learn:nope00", "accept", now=NOW)

    def test_accept_counts_as_a_strong_sign(self):
        key = add(self.state, item())
        ref = memory_review.decide(self.state, key, "accept", now=NOW)["refs"][0]
        self.assertGreater(memory_review.memory_usage.counts(self.state)[ref]["h"], 0)

    def test_decide_all_takes_every_open_item_under_one_token_and_undoes_at_once(self):
        keys = [add(self.state, item("learn", text)) for text in ("规矩甲", "规矩乙", "规矩丙")]
        done = memory_review.decide_all(self.state, "accept_all", now=NOW)
        self.assertEqual(sorted(done["decided"]), sorted(keys))
        self.assertEqual((done["failed"], len(done["refs"])), ([], 3))
        self.assertEqual(memory_review.items(self.state), [])
        memstore.undo(self.state, done["undo_token"], now=NOW + 1)
        self.assertEqual([e.text for e in memstore.entries(self.state)], [])

    def test_decide_all_reports_the_ones_that_failed(self):
        key = add(self.state, item())
        done = memory_review.decide_all(self.state, "reject_all", keys=[key, "review:learn:nope00"], token="tk", now=NOW)
        self.assertEqual((done["decided"], done["undo_token"]), ([key], "tk"))
        self.assertEqual([row["key"] for row in done["failed"]], ["review:learn:nope00"])


class ExpireTest(Case):
    def test_expire_marks_the_old_open_ones_only(self):
        old = add(self.state, item("learn", "旧的", created_at=NOW - 15 * DAY))
        new = add(self.state, item("learn", "新的", created_at=NOW - 13 * DAY))
        self.assertEqual(memory_review.expire(self.state, now=NOW), 1)
        self.assertEqual([row.key for row in memory_review.items(self.state)], [new])
        self.assertEqual(memstore.review_items(self.state, states=("expired",))[0]["key"], old)

    def test_the_daily_upkeep_closes_what_waited_fourteen_days(self):
        old = add(self.state, item("migrate", "迁移来的", created_at=NOW - 15 * DAY))
        new = add(self.state, item("learn", "这周的", created_at=NOW - DAY))
        self.assertEqual(memstore.upkeep(self.state, now=NOW)["expired"], 1)
        self.assertEqual([row.key for row in memory_review.items(self.state)], [new])
        self.assertEqual([row["key"] for row in memstore.review_items(self.state, states=("expired",))], [old])
        self.assertEqual(memstore.upkeep(self.state, now=NOW + 1)["expired"], 0)
        self.assertFalse(hasattr(memory_review, "health"))


if __name__ == "__main__":
    unittest.main()

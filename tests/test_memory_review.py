"""待审核 (core/memory_review.py): the two kinds of item the learning proposes (推断全局, held where it was said, and 放行类),
what each writes when the owner decides it (不要 always leaves things as they are), the one-time sources, one undo token over a
whole decision, whether a proposal is new, the items older releases left (read as expired), the 记录 of each change of state,
an item closed when its line is gone, and the 14 days (closed by the daily upkeep)."""
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

    def test_migrate_accept_makes_it_the_owners_and_reject_leaves_it_as_it_is(self):
        a, b = self.lead_rule("部署前先问"), self.lead_rule("发布走灰度")
        suggest = {"kind": "rule", "scope": "project", "project_key": PID}
        ka = add(self.state, item("migrate", a.text, suggest=suggest, items=(a.ref,)))
        kb = add(self.state, item("migrate", b.text, suggest=suggest, items=(b.ref,)))
        done = memory_review.decide(self.state, ka, "accept", now=NOW)
        self.assertEqual((done["state"], done["refs"]), ("accepted", [a.ref]))
        self.assertEqual(memstore.find(self.state, a.ref).said, "owner")
        down = memory_review.decide(self.state, kb, "reject", now=NOW)
        self.assertEqual((down["state"], down["refs"]), ("rejected", []))
        kept = memstore.find(self.state, b.ref)
        self.assertEqual((kept.kind, kept.said, kept.status), ("rule", "summary", "active"))  # 不要: no longer made a note.
        self.assertEqual(memory_review.items(self.state), [])

    def test_learn_accept_adds_an_entry_the_owner_said_and_reject_writes_nothing(self):
        key = add(self.state, item("learn", "回复先给结论", suggest={"kind": "rule", "scope": "project", "project_key": PID}))
        done = memory_review.decide(self.state, key, "accept", now=NOW)
        entry = memstore.find(self.state, done["refs"][0])
        self.assertEqual((entry.said, entry.by, entry.scope, entry.owner_key, entry.text), ("owner", "learn", "project", PID, "回复先给结论"))
        other = add(self.state, item("learn", "不要擅自推送", suggest={"kind": "rule", "scope": "global", "grant": "deploy"}))
        self.assertEqual(memory_review.decide(self.state, other, "reject", now=NOW)["refs"], [])
        self.assertIsNone(next((e for e in memstore.entries(self.state) if e.text == "不要擅自推送"), None))

    def test_a_rule_held_where_it_was_said_moves_to_global_on_accept_and_stays_on_reject(self):
        here = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="回复都用中文", now=NOW)
        key = add(self.state, item("learn", here.text, items=(here.ref,)))
        done = memory_review.decide(self.state, key, "accept", now=NOW)
        moved = memstore.find(self.state, done["refs"][0])
        self.assertEqual((moved.scope, moved.code, moved.text), ("global", here.code, "回复都用中文"))  # Moved, not made again.
        self.assertEqual(memstore.find(self.state, here.ref).status, "archived")
        self.assertEqual([e.text for e in memstore.entries(self.state, kinds=("rule",))], ["回复都用中文"])
        other = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="改完先跑全量", now=NOW)
        key = add(self.state, item("learn", other.text, items=(other.ref,)))
        self.assertEqual(memory_review.decide(self.state, key, "reject", now=NOW)["refs"], [])
        stays = memstore.find(self.state, other.ref)
        self.assertEqual((stays.scope, stays.status), ("project", "active"))  # 不要: left where it is.

    def test_a_grant_rule_holds_only_once_accepted_as_the_owners(self):
        key = add(self.state, item("learn", "改完直接部署到本机", suggest={"kind": "rule", "scope": "project", "project_key": PID,
                                                                    "grant": "deploy"}))
        self.assertEqual(memstore.entries(self.state, kinds=("rule",)), [])
        entry = memstore.find(self.state, memory_review.decide(self.state, key, "accept", now=NOW)["refs"][0])
        self.assertEqual((entry.said, entry.scope, entry.status), ("owner", "project", "active"))

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

    def test_the_kinds_older_releases_put_here_read_as_expired_and_are_not_put_again(self):
        path = memstore.review_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        old = [{"key": f"review:{kind}:aaaaa{n}", "type": kind, "title": kind, "text": kind, "suggest": {"kind": "rule", "scope": "global"},
                "created_at": NOW, "state": "open", "fp": f"fp{n}"} for n, kind in enumerate(("lead", "candidate", "import", "tidy", "class3"))]
        memstore.write_json(path, {"version": 1, "items": old})
        self.assertEqual(memory_review.items(self.state), [])
        self.assertEqual({row["state"] for row in memstore.review_items(self.state, states=None)}, {"expired"})
        with self.assertRaises(ValueError):
            memory_review.decide(self.state, "review:lead:aaaaa0", "accept", now=NOW)
        for kind in ("lead", "candidate", "import", "tidy", "class3"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                memory_review.add(self.state, item(kind, "旧版的一项"))
        self.assertEqual(memory_review.TYPES, ("learn", "conflict", "migrate"))
        self.assertEqual(memstore.upkeep(self.state, now=NOW)["expired"], 0)  # Read as expired; nothing written.
        self.assertEqual([row.get("state") for row in memstore.read_object(path)["items"]], ["open"] * 5)

    def test_each_change_of_state_is_in_the_log_and_an_undo_passes_over_it(self):
        key = add(self.state, item("learn", "规矩甲"))
        done = memory_review.decide(self.state, key, "accept", token="tk1", now=NOW)
        rows = [row for row in memstore.log_read(self.state) if row.get("undo_token") == "tk1"]
        self.assertEqual(sorted(row["action"] for row in rows), ["accept", "add"])
        review = next(row for row in rows if row["action"] == "accept")
        self.assertEqual((review["refs"], review["title"], review.get("after")), ([key], "规矩甲", {}))
        self.assertEqual(memstore.undo(self.state, "tk1", now=NOW + 1), done["refs"])
        down = add(self.state, item("learn", "规矩乙"))
        memory_review.decide(self.state, down, "reject", token="tk2", now=NOW)
        self.assertEqual([row["action"] for row in memstore.log_read(self.state) if row.get("undo_token") == "tk2"], ["reject"])

    def test_an_item_closes_when_its_line_is_gone_and_the_undo_of_that_opens_it_again(self):
        from covey.app import web_memory
        here = memstore.add(self.state, kind="rule", scope="project", owner_key=PID, text="回复都用中文", now=NOW)
        key = add(self.state, item("learn", here.text, items=(here.ref,)))
        memstore.void(self.state, here.ref, who="owner", token="tv", now=NOW)
        self.assertEqual(memory_review.items(self.state), [])
        closed = next(row for row in memstore.review_items(self.state, states=None) if row["key"] == key)
        self.assertEqual((closed["state"], closed["decided_by"], closed["token"]), ("expired", "owner", "tv"))
        again, new = memory_review.add(self.state, item("learn", here.text, items=(here.ref,)))
        self.assertTrue(new)  # Expired: the same may be proposed again.
        self.assertNotEqual(again, key)
        memstore.review_set(self.state, again, "expired", by="bridge", now=NOW)
        memstore.undo(self.state, "tv", now=NOW + 1)
        self.assertEqual(web_memory.reopen(self.state, "tv", now=NOW + 1), [key])
        self.assertEqual([row.key for row in memory_review.items(self.state)], [key])

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

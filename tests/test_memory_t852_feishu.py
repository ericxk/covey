"""T852 飞书与核心包: the quota a project's parts lend each other (core/memory.py SPARE_ORDER, REFERENCE_CAP), the project card's
count and the one choice of a turn and the card (app/project_facts.py, core/memory.py project_view ``used``), the lookup's label,
the use that goes with a line made global (core/memory_usage.py carry) and the answer card's 记下了 line out of the fold
(cards/answer_card.py tail). The 记忆 tab's workspaces, 常驻 hint and 升为全局 are gone (M4: tests/test_memory_panel.py)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import UNSET_OFF, make_context

from covey.app import memory_candidates, project_facts
from covey.cards import answer_card as cards
from covey.cards.project_card import row_fold
from covey.core import memory, memory_search, memory_usage, project_memory, projects

ON = {memory.ENV: "1", memory.LEARN_ENV: "1", projects.ENV: "1", projects.MEMORY_ENV: "1"}
PID, CHAT = "a1b2c3d4e5f6", "oc_owner_dm"
NOW = 1_800_000_000.0


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                               if not name.startswith(("COVEY_", "LARK_BRIDGE_"))} | UNSET_OFF | ON, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = make_context(Path(tmp.name)).state_dir
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="网关", goal="发出去", project_id=PID, now=NOW))

    def line(self, text, source="owner", kind="decision", at=NOW):
        return project_memory.add(self.state, PID, text, kind, source, now=at)[0]["id"]

    def record(self):
        return projects.read_record(self.state, PID)


def part_item(ref, part, size, at):
    """A project line of ``part`` of exactly ``size`` characters in the room (core/memory.py line_size)."""
    by = memory.OWNER if part != memory.REFERENCE else memory.LEAD
    item = memory.make(ref, memory.PROJECT, by, "字" * (size - 24 - len(memory.PART_WORDS[part]) - 3 - (8 if part == memory.DECISION else 0)), at)
    item.update(part=part, layer=part)
    assert memory.line_size(item) == size
    return item


class QuotaTest(Case):
    def test_the_spare_goes_to_the_owners_decisions_before_the_reference_and_the_reference_is_capped(self):
        self.assertEqual(memory.SPARE_ORDER, (memory.RULE, memory.DECISION, memory.REFERENCE))
        self.assertEqual(memory.REFERENCE_CAP, 1000)
        found = [part_item(f"project:p:d{i}", memory.DECISION, 100, NOW - i) for i in range(15)]  # 10 fit, 5 left out.
        found += [part_item(f"project:p:r{i}", memory.REFERENCE, 100, NOW - i) for i in range(18)]  # 8 fit, 10 left out.
        parts = memory.fill(found)[memory.PROJECT]["parts"]  # No rule: its 1000 is spare.
        self.assertEqual(len(parts[memory.DECISION]["kept"]), 15)  # All the decisions, before the reference.
        self.assertEqual(parts[memory.REFERENCE]["used"], 1000)  # 800 and the 200 it may borrow, not what was left of the rest.
        self.assertEqual(len(parts[memory.REFERENCE]["kept"]), 10)

    def test_the_reference_never_takes_more_than_its_cap_with_all_the_spare_there(self):
        found = [part_item(f"project:p:r{i}", memory.REFERENCE, 100, NOW - i) for i in range(30)]
        room = memory.fill(found)[memory.PROJECT]
        self.assertEqual(room["parts"][memory.REFERENCE]["used"], 1000)
        self.assertEqual(len(room["left"]), 20)


class ProjectMemoryLineTest(Case):
    def project_rows(self):
        """(entry id, text) of the project's lines as the card's older rows listed them (``memory_listing``)."""
        return dict(project_facts.memory_rows(self.state, self.record()))

    def test_history_is_not_listed(self):
        rule = self.line("发布前跑一遍冒烟", kind="constraint")
        answer = self.line("主人答：用蓝绿发布", source="owner_answer")
        old = self.line("旧的约定", kind="constraint")
        lead_old = self.line("负责人记的旧事", source="lead", kind="fact")
        project_memory.settle(self.state, PID, [{"id": old, "valid": "done"}, {"id": lead_old, "valid": "superseded"}])
        self.assertEqual(set(self.project_rows()), {rule, answer})


class UsageCarryTest(Case):
    def test_the_later_time_and_the_counts_added(self):
        memory_usage.note(self.state, ["a", "b"], now=100.0)
        memory_usage.note(self.state, ["a"], now=300.0)
        memory_usage.note(self.state, ["b"], now=200.0)
        self.assertTrue(memory_usage.carry(self.state, "a", "b"))
        self.assertEqual(memory_usage.read(self.state), {"b": {"n": 4, "last": 300.0}})
        self.assertFalse(memory_usage.carry(self.state, "gone", "b"))
        self.assertEqual(memory_usage.read(self.state), {"b": {"n": 4, "last": 300.0}})

    def test_a_candidate_made_a_principle_keeps_its_use(self):
        row = {"id": "c0a1b2c3d", "text": "回复一律先说结论", "topics": [], "labels": [], "workspace": "", "slot": "slot", "at": NOW,
               "state": memory_candidates.OPEN, "source": "harvest"}  # A row an older release wrote.
        memory_candidates.save(self.state, {"schema_version": 1, "rows": [row], "declined": [], "ran": []})
        memory_usage.note(self.state, [f"candidate:{row['id']}"], now=NOW)
        done = memory_candidates.decide(self.state, row["id"], "save")
        self.assertEqual(done["state"], memory_candidates.SAVED)
        self.assertEqual(memory_usage.last_used(self.state), {f"principle:{done['principle']}": NOW})


class ProjectCardTest(Case):
    def test_the_count_is_of_all_the_lines_and_history_is_not_in_it(self):
        ids = [self.line(f"约定 {i}", kind="constraint", at=NOW + i) for i in range(60)]
        project_memory.settle(self.state, PID, [{"id": ids[0], "valid": "done"}])
        rows, total = project_facts.memory_listing(self.state, self.record())
        self.assertEqual((len(rows), total), (project_facts.LIST_CAP, 59))
        fold = row_fold("pj_mem", "专项记忆", rows, 10, total)
        self.assertIn("专项记忆 · 59", str(fold))
        self.assertIn("另有 49 条", str(fold))
        self.assertNotIn(ids[0], [entry for entry, _ in rows])

    def test_the_view_a_turn_and_a_card_take_chooses_as_the_turn_does_with_the_use(self):
        ids = [self.line("规" * 150 + str(i), kind="constraint", at=NOW + i) for i in range(20)]
        used = {f"project:{PID}:{ids[0]}": NOW + 1000}  # The oldest, used last.
        rows = project_memory.entries(self.state, self.record())
        mine = memory.items(project=[row for row in rows if not row["deleted"]], project_id=PID, used=used)
        want = {item["ref"] for item in memory.select(mine)[0]}
        view = memory.project_view(rows, PID, used=used)
        self.assertEqual({item["ref"] for item in view["kept"]}, want)
        self.assertIn(f"project:{PID}:{ids[0]}", want)
        self.assertNotEqual({item["ref"] for item in memory.project_view(rows, PID)["kept"]}, want)


class SearchLabelTest(Case):
    def test_a_leads_note_is_a_reference_to_check_not_a_long_term_rule(self):
        self.line("负责人总结：网关走 8443", source="lead", kind="fact")
        self.line("主人定的：别动 8443", source="owner", kind="constraint")
        found = {hit["text"]: (hit["kind"], hit["said"]) for hit in memory_search.eval_search(
            self.state, "网关走 8443，别动 8443", {"project": PID, "tau": 0.0}, limit=8)}
        self.assertEqual(found["负责人总结：网关走 8443"], ("note", "summary"))  # 资料, 别人总结的: not a rule.
        self.assertEqual(found["主人定的：别动 8443"], ("rule", "owner"))


class AnswerCardTest(unittest.TestCase):
    def test_the_lines_of_what_was_noted_are_out_of_the_fold_and_first(self):
        element = cards.memory_element([{"kind": "remembered", "text": "以后先说结论", "scope": "管这个专项", "entry_id": "e1"}], "t1", "1")
        files = {"tag": "markdown", "content": "产物", "element_id": cards.ARTIFACTS_ID}
        notes = [{"tag": "markdown", "content": "验收", "element_id": "answer_acceptance"}]
        out = cards.tail(files, notes=notes, buttons=[element])
        self.assertEqual(out[0].get("element_id"), cards.MEMORY_ID)
        fold = next(part for part in out if part.get("element_id") == cards.DETAILS_ID)
        self.assertNotIn("answer_memory", str(fold))
        self.assertNotIn("记下了", str(fold))
        self.assertIn("记下了", str(out[0]))


if __name__ == "__main__":
    unittest.main()

"""A project's memory (core/project_memory.py, docs/projects.md 专项记忆): entries in projects/<id>/memory.json, a delete as a
tombstone, the limits, an older release's entries in the record read and moved on the next write, and writers that
do not overwrite one another."""
from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from support.records import state_dir

from covey.core import project_memory as memory, projects

CHAT = "oc_owner_dm"
PID = "a1b2c3d4e5f6"
NOW = 1_800_000_000.0
DAY = 86400


class Case(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        projects.create(self.state, projects.new_record(chat_id=CHAT, name="迁移账单", project_id=PID, now=NOW))

    def record(self):
        return projects.read_record(self.state, PID)

    def stored(self):
        return json.loads(memory.memory_path(self.state, PID).read_text())["entries"]


class WriteTest(Case):
    def test_an_entry_has_the_fields_and_the_same_text_is_kept_once(self):
        item, new = memory.add(self.state, PID, "  先迁\n2024 年  ", "decision", "owner_answer", now=NOW)
        self.assertTrue(new)
        self.assertEqual({key: item[key] for key in ("text", "kind", "source", "at", "deleted")},
                         {"text": "先迁 2024 年", "kind": "decision", "source": "owner_answer", "at": NOW, "deleted": 0})
        again, new = memory.add(self.state, PID, "先迁 2024 年", "decision", "lead", now=NOW + 1)
        self.assertEqual((new, again["id"]), (False, item["id"]))
        self.assertEqual(len(self.stored()), 1)
        memory.add(self.state, PID, "先迁 2024 年", "fact", "lead", now=NOW + 2)  # Another kind is another entry.
        self.assertEqual(len(self.stored()), 2)

    def test_a_text_is_cut_at_200_a_kind_is_one_of_five_and_nothing_is_nothing(self):
        item, _ = memory.add(self.state, PID, "长" * 300, "practice", now=NOW)
        self.assertEqual((len(item["text"]), item["kind"]), (200, "constraint"))  # practice: the older name.
        self.assertEqual(memory.add(self.state, PID, "x", "nonsense", now=NOW)[0]["kind"], "fact")
        self.assertEqual(memory.kind_of("pitfall"), "constraint")
        with self.assertRaises(ValueError):
            memory.add(self.state, PID, "  ", "fact")

    def test_the_record_is_not_written_by_a_plain_add(self):
        before = self.record()
        memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        self.assertEqual(self.record(), before)

    def test_no_record_no_memory(self):
        with self.assertRaises(KeyError):
            memory.add(self.state, "ffffffffffff", "x", "fact")
        self.assertEqual(memory.forget(self.state, "ffffffffffff", NOW), 0)

    def test_the_style_line_is_one_and_changes_the_record_in_the_same_write(self):
        memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        memory.set_style(self.state, PID, "汇报风格：report=a", now=NOW + 1, also=lambda r: r.update(style={"report": "a"}))
        memory.set_style(self.state, PID, "汇报风格：report=b", now=NOW + 2, also=lambda r: r.update(style={"report": "b"}))
        rows = memory.live(self.state, self.record())
        self.assertEqual([(row["kind"], row["text"]) for row in rows], [("fact", "窗口在周六"), ("style", "汇报风格：report=b")])
        self.assertEqual(self.record()["style"], {"report": "b"})
        self.assertEqual([row["text"] for row in memory.live(self.state, self.record(), style=False)], ["窗口在周六"])


class DeleteTest(Case):
    def test_a_delete_is_a_tombstone_that_keeps_the_text_for_30_days(self):
        item, _ = memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        keep, _ = memory.add(self.state, PID, "只读到周日", "fact", now=NOW + 1)
        self.assertTrue(memory.delete(self.state, PID, item["id"], now=NOW + 10))
        self.assertFalse(memory.delete(self.state, PID, item["id"], now=NOW + 11))
        self.assertFalse(memory.delete(self.state, PID, "nope", now=NOW + 11))
        self.assertEqual([row["id"] for row in memory.live(self.state, self.record())], [keep["id"]])
        struck = memory.tombstones(self.state, self.record(), now=NOW + 20 * DAY)
        self.assertEqual([(row["text"], row["deleted"]) for row in struck], [("窗口在周六", NOW + 10)])
        self.assertEqual(memory.tombstones(self.state, self.record(), now=NOW + 41 * DAY), [])
        self.assertTrue(any(row["text"] == "窗口在周六" and row["deleted"] for row in self.stored()))

    def test_a_tombstone_past_30_days_goes_at_the_next_write(self):
        item, _ = memory.add(self.state, PID, "旧的", "fact", now=NOW)
        memory.delete(self.state, PID, item["id"], now=NOW)
        memory.add(self.state, PID, "新的", "fact", now=NOW + 31 * DAY)
        self.assertEqual([row["text"] for row in self.stored()], ["新的"])

    def test_the_text_of_a_deleted_entry_can_be_noted_again_as_a_new_one(self):
        item, _ = memory.add(self.state, PID, "窗口在周六", "fact", now=NOW)
        memory.delete(self.state, PID, item["id"], now=NOW + 1)
        again, new = memory.add(self.state, PID, "窗口在周六", "fact", now=NOW + 2)
        self.assertEqual((new, again["id"] == item["id"]), (True, False))


class LimitTest(Case):
    def test_past_40_entries_the_oldest_goes_but_the_style_line_stays(self):
        memory.set_style(self.state, PID, "汇报风格：x", now=NOW - 5)
        for number in range(45):
            memory.add(self.state, PID, f"条目{number}", "fact", now=NOW + number)
        rows = self.stored()
        self.assertEqual(len([row for row in rows if not row["deleted"]]), 40)  # The style line is one of the 40.
        texts = [row["text"] for row in rows]
        self.assertIn("汇报风格：x", texts)
        self.assertNotIn("条目0", texts)
        self.assertIn("条目44", texts)

    def test_past_6_kb_the_oldest_goes_first_and_a_tombstone_before_a_live_entry(self):
        gone, _ = memory.add(self.state, PID, "老的", "fact", now=NOW)
        memory.add(self.state, PID, "最老的活条目", "fact", now=NOW - 100)
        memory.delete(self.state, PID, gone["id"], now=NOW + 1)
        for number in range(30):
            memory.add(self.state, PID, f"{number:02d}" + "字" * 190, "fact", now=NOW + 10 + number)
        raw = memory.memory_path(self.state, PID).read_text()
        self.assertLessEqual(len(raw.encode()), memory.MAX_BYTES)
        texts = [row["text"] for row in self.stored()]
        self.assertNotIn("老的", texts)  # The tombstone went first.
        self.assertNotIn("最老的活条目", texts)
        self.assertTrue(texts[-1].startswith("29"))

    def test_forget_drops_what_is_older_than_the_cut_and_keeps_style_and_untimed(self):
        memory.add(self.state, PID, "很老", "fact", now=NOW - 200 * DAY)
        memory.add(self.state, PID, "不老", "fact", now=NOW - 10 * DAY)
        memory.set_style(self.state, PID, "汇报风格：x", now=NOW - 300 * DAY)
        self.assertEqual(memory.forget(self.state, PID, NOW - 180 * DAY), 1)
        self.assertEqual(sorted(row["text"] for row in self.stored()), ["不老", "汇报风格：x"])


class ReviewLimitTest(Case):
    """s2/review: what is pushed out of a full memory, and a file that cannot be read."""

    def test_the_answers_notes_go_before_what_the_lead_noted(self):
        memory.add(self.state, PID, "只能在周六发布", "constraint", "lead", now=NOW)  # The oldest, and the lead's.
        for number in range(memory.MAX_ENTRIES):
            memory.add(self.state, PID, f"主人答：{number}", "decision", "owner_answer", now=NOW + 1 + number)
        texts = [row["text"] for row in memory.live(self.state, self.record())]
        self.assertLessEqual(len(texts), memory.MAX_ENTRIES)
        self.assertIn("只能在周六发布", texts)
        self.assertNotIn("主人答：0", texts)

    def test_with_no_answers_left_the_oldest_other_entry_goes_and_the_style_stays(self):
        memory.set_style(self.state, PID, "汇报风格：简短", now=NOW - 1)
        for number in range(memory.MAX_ENTRIES + 1):
            memory.add(self.state, PID, f"事实 {number}", "fact", "lead", now=NOW + number)
        texts = [row["text"] for row in memory.live(self.state, self.record())]
        self.assertIn("汇报风格：简短", texts)
        self.assertNotIn("事实 0", texts)

    def test_a_tombstone_still_goes_first(self):
        old, _ = memory.add(self.state, PID, "要删的", "fact", "lead", now=NOW)
        memory.add(self.state, PID, "主人答：甲", "decision", "owner_answer", now=NOW + 1)
        memory.delete(self.state, PID, old["id"], now=NOW + 2)
        with mock.patch.object(memory, "MAX_ENTRIES", 1):
            fitted = memory._fit(memory.entries(self.state, self.record()), NOW + 3)
        self.assertEqual([row["text"] for row in fitted if not row["deleted"]], ["主人答：甲"])

    def test_a_file_that_cannot_be_read_is_not_written_over(self):
        memory.add(self.state, PID, "窗口在周六", "fact", "lead", now=NOW)
        with mock.patch.object(memory, "read_json", side_effect=PermissionError("denied")):
            self.assertEqual(memory.stored(self.state, PID), [])
            with self.assertRaises(PermissionError):
                memory.add(self.state, PID, "另一条", "fact", "lead", now=NOW + 1)
        self.assertEqual([row["text"] for row in self.stored()], ["窗口在周六"])

    def test_a_file_that_cannot_be_parsed_is_kept_aside_with_a_log_line(self):
        memory.add(self.state, PID, "窗口在周六", "fact", "lead", now=NOW)
        path = memory.memory_path(self.state, PID)
        path.write_text('{"entries": [{"text"')
        with mock.patch("sys.stderr") as err:
            memory.add(self.state, PID, "另一条", "fact", "lead", now=NOW + 1)
        kept = [p for p in path.parent.iterdir() if p.name.startswith("memory.json.bad-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), '{"entries": [{"text"')
        self.assertEqual([row["text"] for row in self.stored()], ["另一条"])
        self.assertTrue(err.write.called)

    def test_a_missing_file_is_just_empty(self):
        self.assertEqual(memory.stored(self.state, PID), [])
        memory.add(self.state, PID, "第一条", "fact", "lead", now=NOW)
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual([p.name for p in memory.memory_path(self.state, PID).parent.iterdir() if ".bad-" in p.name], [])


class LegacyTest(Case):
    def put_legacy(self):
        projects.update(self.state, PID, lambda r: r.update(memory=[
            {"id": "m1", "kind": "pitfall", "text": "旧库时区是 UTC+0", "at": NOW - 5},
            {"id": "m2", "kind": "decision", "text": "先迁 2024 年", "at": NOW - 4}]))

    def test_the_records_entries_are_read_and_merged_with_the_file(self):
        self.put_legacy()
        memory.memory_path(self.state, PID).parent.mkdir(parents=True, exist_ok=True)
        memory.memory_path(self.state, PID).write_text(json.dumps({"entries": [{"id": "f1", "text": "文件里的", "kind": "fact", "at": NOW}]}))
        rows = memory.live(self.state, self.record())
        self.assertEqual([(row["id"], row["kind"]) for row in rows], [("m1", "constraint"), ("m2", "decision"), ("f1", "fact")])

    def test_the_next_write_moves_them_into_the_file_and_the_record_loses_the_field(self):
        self.put_legacy()
        memory.add(self.state, PID, "新的", "fact", now=NOW)
        self.assertNotIn("memory", self.record())
        self.assertEqual([row["id"] for row in self.stored()][:2], ["m1", "m2"])
        self.assertEqual(len(memory.live(self.state, self.record())), 3)

    def test_a_delete_of_an_entry_that_is_only_in_the_record_works_and_moves_the_rest(self):
        self.put_legacy()
        self.assertTrue(memory.delete(self.state, PID, "m1", now=NOW))
        self.assertNotIn("memory", self.record())
        self.assertEqual([row["id"] for row in memory.live(self.state, self.record())], ["m2"])

    def test_a_file_in_the_cards_older_shapes_is_read(self):
        folder = memory.memory_path(self.state, PID).parent
        folder.mkdir(parents=True, exist_ok=True)
        memory.memory_path(self.state, PID).write_text(json.dumps(["窗口在周六", {"id": "x", "text": "只读到周日"}, 7, {"text": ""}]))
        self.assertEqual([row["text"] for row in memory.live(self.state, self.record())], ["窗口在周六", "只读到周日"])
        memory.memory_path(self.state, PID).write_text("not json")
        self.assertEqual(memory.entries(self.state, self.record()), [])

    def test_a_record_with_nothing_wrong_is_not_rewritten_by_a_delete_of_nothing(self):
        before = memory.memory_path(self.state, PID)
        self.assertFalse(memory.delete(self.state, PID, "x"))
        self.assertFalse(before.exists())


class ConcurrencyTest(Case):
    def test_writers_in_threads_lose_none_of_their_entries(self):
        def writer(start):
            for number in range(start, start + 6):
                memory.add(self.state, PID, f"第{number}条", "fact", now=NOW + number)
        threads = [threading.Thread(target=writer, args=(start,)) for start in (0, 10, 20, 30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(memory.live(self.state, self.record())), 24)


if __name__ == "__main__":
    unittest.main()

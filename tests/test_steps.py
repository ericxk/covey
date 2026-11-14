"""core/steps.py: one step lifecycle and held answer notes for every adapter."""
import unittest

from covey.core.steps import AnswerNotes, Steps


def recorder():
    events = []
    return events, lambda kind, data: events.append((kind, data))


class StepsTests(unittest.TestCase):
    def test_a_step_runs_then_ends_and_never_runs_again(self):
        events, emit = recorder()
        steps = Steps(emit)
        steps.start("t", "bash", "ls", 1.0, child_id=None)
        self.assertTrue(steps.update("t", name="background"))
        self.assertTrue(steps.finish("t", False, 2.0))
        self.assertIsNone(steps.start("t", "bash", "ls", 3.0))
        self.assertFalse(steps.update("t", name="bash"))
        self.assertEqual(events, [
            ("tool", {"public_progress": True, "id": "t", "name": "bash", "detail": "ls", "status": "running",
                      "started_at": 1.0, "child_id": None}),
            ("tool", {"public_progress": True, "id": "t", "name": "background", "detail": "ls", "status": "running",
                      "started_at": 1.0, "child_id": None}),
            ("tool", {"public_progress": True, "id": "t", "name": "background", "detail": "ls", "status": "failed",
                      "started_at": 1.0, "child_id": None, "ended_at": 2.0})])
        self.assertEqual(steps.running(), [])

    def test_repeats_publish_again_as_the_adapters_did_and_unknown_keys_change_nothing(self):
        events, emit = recorder()
        steps = Steps(emit)
        steps.start("t", "read", None, 1.0)
        steps.start("t", "read", None, 2.0)  # A repeat while it runs: published afresh.
        self.assertEqual(steps.running(), ["t"])
        steps.finish("t", True, 3.0)
        steps.finish("t", True, 4.0, detail="x")  # A repeated end publishes the later one.
        self.assertFalse(steps.finish("nope", True, 5.0))
        self.assertFalse(steps.update("nope", name="x"))
        self.assertEqual([(d["status"], d["started_at"], d.get("ended_at")) for _, d in events],
                         [("running", 1.0, None), ("running", 2.0, None), ("done", 2.0, 3.0), ("done", 2.0, 4.0)])
        self.assertNotIn("detail", events[0][1])
        self.assertEqual(list(events[-1][1]), ["public_progress", "id", "name", "status", "started_at", "ended_at",
                                               "detail"])

    def test_published_steps_are_copies(self):
        events, emit = recorder()
        steps = Steps(emit)
        steps.start("t", "bash", "ls", 1.0)
        steps.finish("t", True, 2.0)
        self.assertEqual(events[0][1]["status"], "running")
        self.assertIsNot(events[1][1], steps.get("t"))
        self.assertIn("t", steps)

    def test_a_slot_has_one_step_open_at_a_time_numbered_in_turn(self):
        events, emit = recorder()
        steps = Steps(emit)
        first = steps.open_slot("compaction", "context_compaction", 1.0)
        self.assertIs(steps.open_slot("compaction", "context_compaction", 2.0), first)  # A keepalive.
        self.assertTrue(steps.close_slot("compaction", True, 3.0))
        self.assertFalse(steps.close_slot("compaction", True, 4.0))  # None open: ignored.
        steps.open_slot("compaction", "context_compaction", 5.0, "自动压缩")
        steps.close_slot("compaction", False, 6.0, "没有成功")
        self.assertEqual([(d["id"], d["status"], d.get("detail")) for _, d in events], [
            ("compaction-1", "running", None), ("compaction-1", "done", None),
            ("compaction-2", "running", "自动压缩"), ("compaction-2", "failed", "没有成功")])
        self.assertEqual(list(events[1][1]), ["public_progress", "id", "name", "status", "started_at", "ended_at"])

    def test_a_close_with_a_name_and_none_open_opens_and_ends_one_step(self):
        events, emit = recorder()
        steps = Steps(emit)
        steps.open_slot("compact-auto", "context_compaction", 1.0, "a")
        steps.close_slot("compact-auto", True, 2.0, "b", name="context_compaction")
        steps.close_slot("compact-auto", True, 3.0, "c", name="context_compaction")
        self.assertEqual(events[-1], ("tool", {"public_progress": True, "id": "compact-auto-2",
                                               "name": "context_compaction", "started_at": 3.0, "status": "done",
                                               "ended_at": 3.0, "detail": "c"}))
        self.assertEqual(len(events), 3)

    def test_put_guards_ended_steps_and_drops_unchanged_ones(self):
        events, emit = recorder()
        steps = Steps(emit)
        running = {"id": "c", "name": "bash", "status": "running", "started_at": 1.0}
        self.assertTrue(steps.put(running))
        self.assertFalse(steps.put(dict(running)))  # Unchanged.
        done = {**running, "status": "done", "ended_at": 2.0}
        self.assertTrue(steps.put(done))
        self.assertFalse(steps.put(dict(running)))  # Ended: never running again.
        self.assertTrue(steps.put({**done, "status": "failed"}))
        self.assertEqual([d["status"] for _, d in events], ["running", "done", "failed"])
        self.assertIs(events[0][1], running)  # The event itself, as Codex published it.
        self.assertIsNot(steps.get("c"), events[-1][1])


class AnswerNotesTests(unittest.TestCase):
    def test_held_texts_show_as_the_answer_then_become_notes(self):
        events, emit = recorder()
        notes = AnswerNotes(emit)
        notes.text("n1", "先看看")
        notes.text("n2", "再想想")
        notes.to_notes()
        notes.to_notes(5.0)
        self.assertEqual(events, [
            ("answer_snapshot", {"text": "先看看"}), ("answer_snapshot", {"text": "再想想"}),
            ("progress_note", {"id": "n1", "text": "先看看"}), ("progress_note", {"id": "n2", "text": "再想想"}),
            ("answer_snapshot", {"text": ""}), ("answer_snapshot", {"text": ""})])

    def test_received_at_and_child_only_when_given_and_clear_drops_held_texts(self):
        events, emit = recorder()
        notes = AnswerNotes(emit)
        notes.text("text-1", "草稿", show=False)
        notes.to_notes(7.0, "a1")
        notes.text("text-2", "丢掉")
        notes.clear()
        notes.to_notes(8.0)
        self.assertEqual(events, [
            ("progress_note", {"id": "text-1", "text": "草稿", "received_at": 7.0, "child_id": "a1"}),
            ("answer_snapshot", {"text": ""}), ("answer_snapshot", {"text": "丢掉"}), ("answer_snapshot", {"text": ""})])


if __name__ == "__main__":
    unittest.main()

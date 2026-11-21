"""A resumed topic turn whose previous turn left the context over 60% of its window is told to update its checkpoint
first (app/prompts.py context_full, app/runs.py _context_note)."""
from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from support.world import UnifiedCase
from test_auto_retry import Failing

from covey.app.prompts import CONTEXT_FULL, context_full
from covey.core.jobs import find_job_record, upsert_job_record


class ContextFullTest(unittest.TestCase):
    def test_over_sixty_percent_of_a_known_window(self):
        self.assertEqual(context_full({"context_tokens": 600_000, "context_window": 1_000_000}), CONTEXT_FULL)
        self.assertEqual(context_full({"context_tokens": 130_000, "context_window": 200_000}), CONTEXT_FULL)
        self.assertEqual(context_full({"context_tokens": 599_999, "context_window": 1_000_000}), "")

    def test_no_window_or_no_count_is_no_line(self):
        for record in (None, {}, {"context_tokens": 900_000}, {"context_window": 1_000_000},
                       {"context_tokens": 900_000, "context_window": 0}, {"context_tokens": "9", "context_window": 10},
                       {"context_tokens": 9.5, "context_window": 10}):
            self.assertEqual(context_full(record), "", record)


class WiredTest(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.claude = self.app.apps["claude"]
        self.claude.harness.backend = Failing()

    def started(self, **fields):
        """A topic's first task ended with ``fields`` on its record: (its context, a next job of the topic)."""
        self.receive("task", "initial")
        _, ctx = self.route("task")
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, ctx = self.route("task")
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        app.process_reply_job(job, app.harness.context)
        record = find_job_record(self.ctx.state_dir, job.message_id)
        upsert_job_record(self.ctx.state_dir, "om_done", topic_id=record["topic_id"], chat_id=record["chat_id"], job_id=2,
                          kind="chat", backend=record["backend"], status="done", created_at="2099-01-01T00:00:00Z",
                          updated_at="2099-01-01T00:00:00Z", **fields)
        nxt = dataclasses.replace(job, job_id=3, message_id="next", recovery_context=None)
        nxt.checkpoint_path = str(Path(self.root) / "cp.json")
        return ctx, nxt

    def test_a_resumed_turn_after_a_full_context_is_told_first(self):
        ctx, job = self.started(context_tokens=700_000, context_window=1_000_000)
        note = self.claude.runs._context_note(ctx, job, "sid")
        self.assertEqual(note, "\n\n" + CONTEXT_FULL)
        self.assertIn(CONTEXT_FULL, self.claude.runs._prompt(type("L", (), {"ctx": ctx, "rules_session": True})(), job, "sid")[0])

    def test_a_new_session_gets_no_line(self):
        ctx, job = self.started(context_tokens=700_000, context_window=1_000_000)
        self.assertEqual(self.claude.runs._context_note(ctx, job, None), "")

    def test_a_small_context_gets_no_line(self):
        ctx, job = self.started(context_tokens=100_000, context_window=1_000_000)
        self.assertEqual(self.claude.runs._context_note(ctx, job, "sid"), "")

    def test_an_unknown_window_gets_no_line(self):
        ctx, job = self.started(context_tokens=900_000)
        self.assertEqual(self.claude.runs._context_note(ctx, job, "sid"), "")

    def test_the_butler_gets_none(self):
        from unittest import mock
        ctx, job = self.started(context_tokens=700_000, context_window=1_000_000)
        with mock.patch("covey.app.runs.is_butler_topic", return_value=True):
            self.assertEqual(self.claude.runs._context_note(ctx, job, "sid"), "")


if __name__ == "__main__":
    unittest.main()

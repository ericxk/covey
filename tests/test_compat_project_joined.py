"""core/migrate.py project-joined (T552, 2026-11-19): a project's record from before ``joined`` gets, for each member topic,
the start of its first task the 专项卡 counted (``token_jobs``), or the project's start when none was; a member that has its
time keeps it. The web page's 专项报告 then counts a member's turns from the same time as the 专项卡's token."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support.records import write_job

from covey.core import migrate, projects
from covey.core.state import epoch_seconds

CHAT, PID = "oc_dm", "a1b2c3d4e5f6"
EARLY, LATE, QUIET, KEPT = "ab" * 16, "cd" * 16, "ef" * 16, "12" * 16


class ProjectJoinedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        record = projects.new_record(chat_id=CHAT, name="n", project_id=PID, now=1000.0)
        record.update(threads=[EARLY, LATE, QUIET, KEPT], joined={KEPT: 1500.0},
                      token_jobs=["main-late-2", "main-late-1", "main-early-1"])
        projects.create(self.state, record)
        write_job(self.state, "main-early-1", topic_id=EARLY, chat_id=CHAT, job_id=1, status="done",
                  created_at="2026-11-19T00:10:00Z")
        write_job(self.state, "main-late-1", topic_id=LATE, chat_id=CHAT, job_id=2, status="done",
                  created_at="2026-11-19T00:27:00Z")
        write_job(self.state, "main-late-2", topic_id=LATE, chat_id=CHAT, job_id=3, status="done",
                  created_at="2026-11-19T01:00:00Z")

    def test_each_member_gets_its_first_counted_task_or_the_time_of_the_migration(self):
        """QUIET had no task counted: none of its tasks ended since it came in, so all it did came before (T519's 10M)."""
        self.assertEqual(migrate.pending(self.state)[migrate.PROJECT_JOINED], 1)
        with mock.patch.object(migrate.time, "time", return_value=5000.0):
            summary = migrate.migrate(self.state)
        self.assertEqual(summary[migrate.PROJECT_JOINED], {"written": 1, "failed": 0})
        record = projects.read_record(self.state, PID)
        self.assertEqual({topic: projects.joined_at(record, topic) for topic in (EARLY, LATE, QUIET, KEPT)}, {
            EARLY: round(epoch_seconds("2026-11-19T00:10:00Z"), 3), LATE: round(epoch_seconds("2026-11-19T00:27:00Z"), 3),
            QUIET: 5000.0, KEPT: 1500.0})
        self.assertIn(migrate.PROJECT_JOINED, migrate.done(self.state))
        self.assertEqual(migrate.migrate(self.state), {})

    def test_with_its_oldest_counted_tasks_dropped_a_quiet_member_gets_the_projects_start(self):
        projects.update(self.state, PID, lambda record: record.update(
            token_jobs=[f"gone-{n}" for n in range(projects.TOKEN_JOBS_KEPT)]))
        migrate.migrate(self.state)
        self.assertEqual(projects.joined_at(projects.read_record(self.state, PID), QUIET), 1000.0)


if __name__ == "__main__":
    unittest.main()

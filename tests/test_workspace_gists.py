"""A new topic carries the 话题要点 of its workspace's last topics (app/workspace_gists.py, COVEY_WORKSPACE_GISTS; T604 #30):
off unset (the rules as before); on, only a new session's rules, only the owner's own turn in an ordinary topic, only topics
of the same chat and workspace, a redacting topic's gist only for a redacting one, the newest first."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from covey.app import workspace_gists
from covey.app.prompts import WORKSPACE_GISTS_HEAD, bridge_rules
from covey.core import topic_gist
from covey.core.jobs import upsert_job_record
from covey.core.state import write_json
from covey.core.topics import topic_file

CHAT = "oc_dm"
HERE = "f" * 16


class WorkspaceGistsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.jobs = 0
        self.enterContext(mock.patch.dict(os.environ, {"COVEY_WORKSPACE_GISTS": "2"}))

    def topic(self, topic, number, essence, *, workspace="covey", chat=CHAT, redaction=False, gist=True):
        """A topic with a job of ``workspace`` in ``chat`` (each newer than the one before) and, with ``gist``, its gist."""
        write_json(topic_file(self.state, topic), {"topic_number": number, "origin": {"title": f"标题{number}"},
                                                    **({"redaction": True} if redaction else {})})
        self.jobs += 1
        upsert_job_record(self.state, f"job-{self.jobs}", job_id=self.jobs, topic_id=topic, chat_id=chat,
                          workspace_name=workspace, status="done")
        if gist:
            topic_gist.save(self.state, topic, {"essence": essence, "goal": f"{essence}的目标"})

    def block(self, *, owner=True, topic=HERE, workspace="covey", chat=CHAT):
        ctx = SimpleNamespace(state_dir=self.state, workspace_name=workspace, topic_id=topic,
                              history_path=self.state / "topics" / topic / "history.jsonl")
        job = SimpleNamespace(topic_id=topic, chat_id=chat)
        return workspace_gists.block(ctx, job, owner_turn=lambda: owner)

    def test_the_switch(self):
        for raw, count in (("", 0), ("0", 0), ("3", 3), ("10", 10), ("11", 0), ("x", 0), (" 2 ", 2)):
            with mock.patch.dict(os.environ, {"COVEY_WORKSPACE_GISTS": raw}):
                self.assertEqual(workspace_gists.count(), count, raw)

    def test_the_newest_topics_of_the_workspace_in_this_chat_are_carried(self):
        self.topic("a" * 16, 1, "最早的事")
        self.topic("b" * 16, 2, "第二件事")
        self.topic("c" * 16, 3, "别的工作区的事", workspace="docs")
        self.topic("d" * 16, 4, "群里的事", chat="oc_group")
        self.topic("e" * 16, 5, "没有要点的", gist=False)
        self.topic("9" * 16, 6, "最新的事")
        self.topic(HERE, 7, "这个话题自己")
        block = self.block()
        lines = block.splitlines()
        self.assertEqual(lines[0], WORKSPACE_GISTS_HEAD.format(name="covey"))
        self.assertEqual(lines[1:], ["- T6「标题6」：最新的事；现在的目标：最新的事的目标",
                                     "- T2「标题2」：第二件事；现在的目标：第二件事的目标"])

    def test_nothing_unless_the_owners_own_turn_and_the_switch(self):
        self.topic("a" * 16, 1, "一件事")
        self.assertTrue(self.block())
        self.assertEqual(self.block(owner=False), "")
        self.assertEqual(self.block(workspace=""), "")
        with mock.patch.dict(os.environ, {"COVEY_WORKSPACE_GISTS": ""}):
            self.assertEqual(self.block(), "")

    def test_a_redacting_topics_gist_goes_only_to_a_redacting_topic(self):
        self.topic("a" * 16, 1, "脱敏话题的事", redaction=True)
        self.assertEqual(self.block(), "")
        write_json(topic_file(self.state, HERE), {"redaction": True})
        self.assertIn("脱敏话题的事", self.block())

    def test_only_a_new_sessions_rules_carry_it(self):
        base = dict(backend_name="Claude Code", workspace_name="covey", workdir=self.state)
        self.assertEqual(bridge_rules(**base), bridge_rules(**base, gists=""))
        self.assertIn("- T1「x」：y", bridge_rules(**base, gists="- T1「x」：y"))
        from covey.app.runs import Runs
        runs = Runs.__new__(Runs)
        runs.harness = SimpleNamespace(spec=SimpleNamespace(prompt_agent_name="Claude Code"),
                                       backend=SimpleNamespace(prompt_runtime_note=lambda ctx: ""))
        runs.owner_turn = lambda ctx, job: True
        runs.principles = lambda ctx, job: ""
        ctx = SimpleNamespace(state_dir=self.state, workspace_name="covey", workdir=self.state, device="",
                              remote_workdir="", topic_id=HERE, options={},
                              history_path=self.state / "topics" / HERE / "history.jsonl")
        job = SimpleNamespace(topic_id=HERE, chat_id=CHAT, message_id="m1", job_id=9, user_text="")
        self.topic("a" * 16, 1, "一件事")
        with mock.patch("covey.app.runs.project_context.block", return_value=""), \
                mock.patch("covey.app.runs.knowledge_turn.block", return_value=""), \
                mock.patch("covey.app.runs.knowledge_turn.remembers", return_value=False), \
                mock.patch("covey.app.runs.memory_turn.block", return_value=""), \
                mock.patch("covey.app.runs.memory_turn.fix_rule", return_value=False):
            self.assertIn("一件事", runs.bridge_rules(ctx, job, fresh=True))
            self.assertNotIn("一件事", runs.bridge_rules(ctx, job))


if __name__ == "__main__":
    unittest.main()

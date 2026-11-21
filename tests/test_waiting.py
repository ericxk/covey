"""等我 is one count (the owner, 2026-11-19: 汇总说 6 个“等我”、看板只有 3 个、管家说 4 个，在私聊里也找不到): the
same records give the same things waiting for the owner to the 早晚汇总 and the 看板 (app/digest.py collect, cards/board.py),
手上的事 (app/overview.py), the butler's snapshot (app/butler.py) and the web page (app/web_sync.py), the ones
core/board_read.py waits finds, and each opens the card it is acted on."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import state_dir, write_job, write_topic

from covey.app import butler, digest, overview, pins
from covey.app.web_collect import Collector
from covey.cards import board as board_card
from covey.core import board_read
from covey.core.butler import butler_topic_id
from covey.core.state import write_private_text

CHAT = "oc_dm"
HOUR, DAY = 3600, 86400


class OneCountTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(state_dir(self))
        self.now = now = time.time()
        self.topics = {}
        # Waits, each with its card:
        self.question(1, "q1", now - 300)  # A topic's question five minutes old.
        self.question(1, "q1-old", now - 900)  # An older one of the same topic: one row a topic, the newest.
        self.start(2, now - 7 * HOUR)  # A message waiting for 开始 on its launch card (T536 and T537 on 11-19).
        self.question(3, "q3", now - 2 * DAY)  # Asked two days ago: on the line of old ones, still counted.
        self.question(0, "q-butler", now - 60, topic=butler_topic_id(CHAT))  # The butler asks at the top level.
        # Residue, counted nowhere:
        self.question(4, "q4-held", now - 120, display_pending=True,
                      triage={"state": "held", "at": now - 120})  # Held for a project's lead (app/triage.py).
        self.question(5, "q5-nocard", now - 120, question_message_id="")  # Its card never went out.
        self.question(6, "q6-answered", now - 120, status="answered")
        self.start(7, now - HOUR, launch=None)  # No launch card that can start it: nothing to act on.
        self.start(8, now - HOUR, launch="submitted")  # Its launch card was used.
        self.question(9, "q9-other", now - 120, chat="oc_other")  # Another chat's.

    def topic(self, number, topic=None):
        topic = topic or f"{number:02d}" * 16
        self.topics[number] = topic
        write_topic(self.state, topic_id=topic, chat_id=CHAT, topic_number=number, harness_id="claude",
                    workspace_name="bridge", root_id=f"om_root{number}")
        return topic

    def question(self, number, token, asked, *, topic=None, chat=CHAT, **fields):
        topic = self.topics.get(number) or self.topic(number, topic)
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic, "chat_id": chat, "job_id": number, "status": "pending",
            "created_at": asked, "question_message_id": f"om_{token}", "questions": [{"question": f"问 {token}？"}],
            **fields}))

    def start(self, number, received, launch="ready"):
        topic = self.topic(number)
        write_job(self.state, f"om_start{number}", chat_id=CHAT, topic_id=topic, job_id=100 + number,
                  status="waiting_workspace", harness_id="claude", timings={"received": received})
        if launch:
            write_private_text(self.state / "launch-forms" / f"form{number}.json", json.dumps({
                "topic_id": topic, "chat_id": CHAT, "status": launch, "message_id": f"om_launch{number}"}))

    def expected(self):
        return {self.topics[1]: "om_q1", self.topics[2]: "om_launch2", self.topics[3]: "om_q3",
                self.topics[0]: "om_q-butler"}

    def test_waits_finds_what_waits_with_its_card_and_no_residue(self):
        found = board_read.waits(self.state, CHAT, self.now)
        self.assertEqual({wait.topic: wait.card for wait in found.all}, self.expected())
        self.assertEqual([wait.topic for wait in found.stale], [self.topics[3]])  # Over QUESTION_AGE: the line.
        self.assertEqual([wait.topic for wait in found.live], [self.topics[0], self.topics[1], self.topics[2]])
        self.assertEqual(found.count(), 4)
        self.assertEqual({wait.topic: wait.kind for wait in found.all}[self.topics[2]], "start")
        # A topic the owner took off the board is not counted.
        self.assertEqual(board_read.waits(self.state, CHAT, self.now, off={self.topics[2]: {}}).count(), 3)

    def test_a_question_with_a_copy_at_the_top_level_opens_the_copy(self):
        """A project's question is in its topic's thread and copied to the private chat's top level (app/question_display.py
        mirror): 等我 opens the copy, where the owner looks (2026-11-19)."""
        self.question(1, "q1", self.now - 200, mirror_message_id="om_q1-top")
        found = board_read.waits(self.state, CHAT, self.now)
        self.assertEqual({wait.topic: wait.card for wait in found.all}[self.topics[1]], "om_q1-top")

    def test_every_reader_counts_the_same(self):
        expected = set(self.expected())
        # 早晚汇总 and the 看板 (the same collect): its 等你 rows and its line of old ones, each with its card.
        view = digest.collect(self.state, CHAT, self.now, self.now - HOUR, self.now)
        rows = [*view["waiting"], *view["stale"]]
        self.assertEqual({row["topic"]: row["card"] for row in rows}, self.expected())
        # The 看板's count: every 等你, a live project's topics too (not left out to its 专项 part).
        view["projects"] = [{"name": "专项", "topics": [self.topics[1]], "waiting": 1}]
        found = board_card.groups(view)
        self.assertEqual(board_card.waiting_count(found), 4)
        self.assertIn(("4 等你", "orange"), board_card.counts(found))
        # 手上的事: its rows, each with the link of its card.
        with mock.patch.object(overview, "link_for", lambda state, topic, card="", profile=None: f"https://open/{card}"):
            looked = overview.Overview(None, None, None, {}).view(SimpleNamespace(state_dir=self.state), CHAT, self.now)
        self.assertEqual({row["topic"] for row in looked["waiting"]}, expected)
        self.assertEqual({row["topic"]: row["url"] for row in looked["waiting"]},
                         {topic: f"https://open/{card}" for topic, card in self.expected().items()})
        # The butler's snapshot.
        groups = butler.snapshot_groups(SimpleNamespace(state_dir=self.state), CHAT, self.now)
        self.assertEqual(set(groups["waiting"]), expected)
        # The pinned board: the ones it pins (within QUESTION_AGE, of the topics it lists), by the same cards; the launch
        # card too (2026-11-21 T622: a topic waiting for 开始 was 等你 everywhere but had no pin, and could not be found).
        rows, _ = pins.PinnedTopics(None, None).look(self.state, CHAT, self.now)
        self.assertEqual({row["topic"]: row["message_id"] for row in rows if row["group"] == "waiting"},
                         {self.topics[1]: "om_q1", self.topics[2]: "om_launch2"})
        # The web page: 等你 for a question that asks and for a task waiting for 开始 with a launch card, not for residue.
        questions = lambda token, **fields: [{"status": "pending", "topic_id": "t", "question_message_id": f"om_{token}", **fields}]
        self.assertEqual(Collector.word([], None, questions("a")), "等你")
        self.assertEqual(Collector.word([], None, questions("a", display_pending=True)), "空闲")
        self.assertEqual(Collector.word([], None, questions("a", question_message_id="")), "空闲")
        self.assertEqual(Collector.word([], None, [], starts=True), "等你")
        self.assertEqual(Collector.tone([], None, [], False, None, starts=True), "waiting")


if __name__ == "__main__":
    unittest.main()

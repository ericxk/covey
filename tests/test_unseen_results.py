"""有结果未看 is one set (the owner, 2026-11-10: 以看板为准): the same records fed to the board (app/pins.py), 手上的事
(app/overview.py), 早晚汇总 (app/digest.py), the main thread's snapshot (app/main_thread.py) and its tool topics_list
(app/main_tools.py) give the same topics, the board's (core/board_read.py unseen)."""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import state_dir, write_job, write_topic

from covey.app import digest, main_thread, main_tools, overview, pins
from covey.app.main_env import MainEnv
from covey.app.relay_send import relay_path
from covey.core import main_requests
from covey.core.jobs import job_path
from covey.core.main_thread import main_topic_id
from covey.core.state import write_json

CHAT = "oc_dm"
HOUR, DAY = 3600, 86400
ON = {"COVEY_PINNED_TOPICS": "1", "COVEY_PINNED_OVERVIEW": "1"}


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class OneSetTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(state_dir(self))
        self.now = time.time()
        now = self.now
        self.topics = {}
        # T1 stopped after it ran: a result on the board (stopped ones too), never seen.
        self.job(1, "cancelled", now - 1800, timings={"backend_started": now - 2400})
        # T2 ended four days ago, never seen: the board's record keeps it, a result not seen stays however old.
        self.job(2, "done", now - 4 * DAY, updated=now - 4 * DAY)
        # T3 seen an hour ago, after it ended: on the board for KEEP_SEEN more, but seen.
        self.job(3, "done", now - 2 * HOUR)
        # T4 seen thirteen hours ago: off the board.
        self.job(4, "done", now - 14 * HOUR)
        # T5's conclusion was written on the main thread's card (a release before 2026-11-09), and the owner read the
        # top level since: seen.
        record = self.job(5, "done", now - 1200)
        write_json(relay_path(self.state, record["key"]), {"state": "inline", "outcome": "done", "sent_at": now - 1140})
        # T6 done ten minutes ago, never seen.
        self.job(6, "done", now - 600)
        write_json(self.state / "topic-seen.json", {self.topics[3]: now - HOUR, self.topics[4]: now - 13 * HOUR,
                                                    main_topic_id(CHAT): now - 60})
        long_ago = now - 30 * DAY
        write_json(self.state / pins.RECORD, {
            "schema_version": 1, "chat_id": CHAT, "since": long_ago, "keep_since": long_ago,
            "stopped_since": long_ago, "pins": [], "dismissed": {},
            "results": {self.topics[2]: {"at": now - 4 * DAY, "seen": None}}})

    def job(self, number, status, ended, *, timings=None, updated=None):
        topic = f"{number:02d}" * 16
        self.topics[number] = topic
        write_topic(self.state, topic_id=topic, chat_id=CHAT, topic_number=number, harness_id="claude",
                    workspace_name="bridge", root_id=f"om_root{number}")
        key = f"om_job{number}-{number}"
        record = write_job(self.state, key, chat_id=CHAT, topic_id=topic, job_id=number, status=status,
                           card_message_id=f"om_card{number}", harness_id="claude",
                           timings={"backend_started": ended - 60, **(timings or {}), "execution_finished": ended})
        if updated is not None:
            path = job_path(self.state, key)
            data = json.loads(path.read_text())
            data["updated_at"] = iso(updated)
            path.write_text(json.dumps(data))
        return {**record, "key": key}

    def numbers(self, topics):
        named = {topic: number for number, topic in self.topics.items()}
        return sorted(named[topic] for topic in topics)

    def board(self):
        rows, _ = pins.PinnedTopics(None, None).look(self.state, CHAT, self.now)
        return self.numbers(row["topic"] for row in rows if row["group"] == "result" and row["seen"] is None)

    def overview(self):
        view = overview.Overview(None, None, None, {}).view(SimpleNamespace(state_dir=self.state), CHAT, self.now)
        return self.numbers(row["topic"] for row in view["board"]["result"] if not row.get("seen"))

    def digest(self):
        view = digest.collect(self.state, CHAT, self.now, self.now - HOUR, self.now, board=True)
        return self.numbers(row["topic"] for row in view["done"] if not row["seen"])

    def snapshot(self):
        groups = main_thread.snapshot_groups(SimpleNamespace(state_dir=self.state), CHAT, self.now)
        return self.numbers(groups.get("result", {}))

    def topics_list(self):
        runtime = SimpleNamespace(log=lambda line: None, access=SimpleNamespace(owner_chat=lambda: CHAT))
        env = MainEnv(runtime, SimpleNamespace(harnesses={}, default="claude", target=lambda *args: None), None, None,
                      state_dir=self.state, profile="unit")
        answer = main_tools.topics_list(env, main_requests.Request("r1", "topics_list", {"group": "result", "limit": 60},
                                                                   "main-1", self.now))
        lines = answer["text"].splitlines() if answer["data"].get("total") else []
        return self.numbers(line.split(" · ")[-1] for line in lines if " · " in line)

    def test_the_five_readers_have_the_boards_unseen_results(self):
        with mock.patch.dict(os.environ, ON):
            board = self.board()
            self.assertEqual(board, [1, 2, 6])  # Stopped, four days old; not T3/T4 (seen), nor T5 (read at the top).
            self.assertEqual({"手上的事": self.overview(), "早晚汇总": self.digest(), "快照": self.snapshot(),
                              "topics_list": self.topics_list()},
                             {"手上的事": board, "早晚汇总": board, "快照": board, "topics_list": board})

    def test_topics_list_still_takes_the_old_group_name(self):
        with mock.patch.dict(os.environ, ON):
            runtime = SimpleNamespace(log=lambda line: None, access=SimpleNamespace(owner_chat=lambda: CHAT))
            env = MainEnv(runtime, SimpleNamespace(harnesses={}, default="claude", target=lambda *args: None), None,
                          None, state_dir=self.state, profile="unit")
            answer = main_tools.topics_list(env, main_requests.Request("r1", "topics_list", {"group": "unread"},
                                                                       "main-1", self.now))
        self.assertEqual(answer["data"]["total"], 3)

    def test_with_the_board_off_an_old_records_topics_taken_off_filter_nothing(self):
        # r4 review P4: a deployment that had the board on and turned it off reads 有结果未看 as one without it would.
        with mock.patch.object(pins, "laid", return_value=False):
            before, off = pins.board_results(self.state, CHAT, self.now)
        self.assertIn(self.topics[6], before)
        self.assertEqual(off, {})
        record = json.loads((self.state / pins.RECORD).read_text())
        record["dismissed"] = {self.topics[6]: {"group": "result", "at": self.now}}
        write_json(self.state / pins.RECORD, record)
        with mock.patch.object(pins, "laid", return_value=False):
            self.assertEqual(pins.board_results(self.state, CHAT, self.now), (before, {}))
        with mock.patch.dict(os.environ, ON):  # The board on: taken off.
            unseen, off = pins.board_results(self.state, CHAT, self.now)
        self.assertNotIn(self.topics[6], unseen)
        self.assertIn(self.topics[6], off)


if __name__ == "__main__":
    unittest.main()

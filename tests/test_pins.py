"""The pinned board (app/pins.py) under 手上的事: which topics are pinned in which order, the steps from the pins
there to that order, the rounds that take them against a fake Feishu, and the card as its index."""
from __future__ import annotations

import json
import os
import random
import unittest
from unittest import mock

from support.world import UnifiedCase
from support.records import write_topic
from covey.app import overview, pins
from covey.app.pins import MAX_PINS, active_dismissed, board, buffered, choose, dismiss, plan
from covey.core import board_read
from covey.core.board_read import KEEP_SEEN, ended_at
from covey.core.topic_seen import PINNED_TOPICS_ENV, SEEN_FILE
from covey.cards.overview import overview_card
from covey.core.jobs import upsert_job_record
from covey.core.state import write_json, write_private_text

NOW = 1793856000.0  # 2026-11-05T05:20:00Z
ON = {overview.ENV: "1", PINNED_TOPICS_ENV: "1"}


def question(topic, at, message="", **fields):
    return {"topic_id": topic, "created_at": at, "question_message_id": message or f"om_q_{topic}", **fields}


def job(topic, job_id, status, card="", **fields):
    return {"topic_id": topic, "job_id": job_id, "status": status, "card_message_id": card or f"om_job_{job_id}",
            **fields}


def apply(steps, current):
    """The pins after ``steps``, as Feishu would order them: a pin goes to the top."""
    current = list(current)
    for op, message in steps:
        if op in ("unpin", "move"):
            current.remove(message)
        if op in ("pin", "move"):
            current.insert(0, message)
    return current


class BoardTests(unittest.TestCase):
    def test_groups_in_order_and_what_each_leaves_out(self):
        questions = [question("a", NOW - 60), question("b", NOW - 600),
                     question("old", NOW - pins.QUESTION_AGE - 1),  # Too long ago.
                     question("bare", NOW - 60, message=None)]  # Its card not shown: nothing to pin.
        questions[-1]["question_message_id"] = ""
        jobs = [job("a", 1, "running"),  # Asks: it is under 等你回答.
                job("r1", 2, "done", timings={"delivered": NOW - 100}),
                job("r2", 3, "failed", timings={"execution_finished": NOW - 50}),
                job("seen", 4, "done", timings={"delivered": NOW - 100}),
                job("cancel", 5, "cancelled", timings={"delivered": NOW - 10}),
                job("stale", 6, "done", timings={"delivered": NOW - board_read.RESULT_AGE - 1}),
                job("again", 7, "done", timings={"delivered": NOW - 100}), job("again", 8, "waiting_workspace"),
                job("run1", 9, "running", timings={"backend_started": NOW - 300}),
                job("run2", 10, "queued", timings={"queued": NOW - 30}),
                job("nocard", 11, "running", card="x"), job("r1", 1, "done")]  # r1's older job.
        jobs[-2]["card_message_id"] = ""
        # Results that ended before the 2026-11-07 rules (keep_since): off once seen, or RESULT_AGE after.
        rows = board(questions, jobs, {"seen": NOW - 99, "r1": NOW - 101}, {}, NOW, keep_since=NOW)
        # 等我 → 在做 → 做完 (the owner's D2, 2026-11-08).
        self.assertEqual([(row["group"], row["topic"], row["message_id"]) for row in rows], [
            ("waiting", "a", "om_q_a"), ("waiting", "b", "om_q_b"),
            ("running", "run2", "om_job_10"), ("running", "run1", "om_job_9"),
            ("result", "r2", "om_job_3"), ("result", "r1", "om_job_2")])
        # Since: a result stays till seen and KEEP_SEEN more (the owner: as a phone keeps a notification), the one
        # not seen however old.
        rows = board(questions, jobs, {"seen": NOW - 99, "r1": NOW - 101}, {}, NOW)
        self.assertEqual([(row["topic"], row["seen"]) for row in rows if row["group"] == "result"], [
            ("r2", None), ("r1", None), ("seen", NOW - 99), ("stale", None)])
        rows = board(questions, jobs, {"seen": NOW - 99}, {}, NOW - 99 + KEEP_SEEN)
        self.assertNotIn("seen", [row["topic"] for row in rows])
        self.assertIn("r1", [row["topic"] for row in rows])

    def test_a_task_waiting_for_start_waits_with_its_launch_card(self):
        """2026-11-21 T622: a reply to a node's alert opened a topic whose task waited 21 minutes for 开始 on its launch card,
        in the alert's thread. Every reader said 等你 (core/board_read.py waits) but the board pinned nothing of it, and the
        owner found the topic nowhere in the private chat. It waits as a question does (core/board_read.py starts): its
        launch card is the pin; a question of its topic comes first, one that came in over QUESTION_AGE ago is not on the
        board (手上的事 lists it apart), and a topic waiting is in no other group."""
        def wait(topic, at, job_id):
            return board_read.Wait(topic, "start", job(topic, job_id, "waiting_workspace"), at, f"om_launch_{topic}")
        starts = {"s": wait("s", NOW - 60, 1), "asks": wait("asks", NOW - 30, 2),
                  "old": wait("old", NOW - pins.QUESTION_AGE - 1, 3), "ran": wait("ran", NOW - 20, 5)}
        jobs = [job("ran", 4, "done", timings={"delivered": NOW - 100}), job("ran", 5, "waiting_workspace"),
                job("old", 3, "waiting_workspace"), job("s", 1, "waiting_workspace"), job("asks", 2, "waiting_workspace")]
        rows = board([question("asks", NOW - 10)], jobs, {}, {}, NOW, starts=starts)
        self.assertEqual([(row["group"], row["topic"], row["message_id"], row["at"]) for row in rows], [
            ("waiting", "asks", "om_q_asks", NOW - 10), ("waiting", "ran", "om_launch_ran", NOW - 20),
            ("waiting", "s", "om_launch_s", NOW - 60)])
        self.assertEqual(rows[1]["record"]["job_id"], 5)
        # Without them (a caller that has none) nothing waits for 开始, as before.
        self.assertEqual(board([], jobs, {}, {}, NOW), [])

    def test_a_stopped_turn_stays_with_the_results_as_one_done(self):
        ran = {"backend_started": NOW - 300, "execution_finished": NOW - 100}
        stopped = job("s", 2, "cancelled", timings=ran)
        jobs = [stopped,
                # Merged into the task running as its supplement, or taken out of the queue, or never started: not
                # a turn of its topic, whose latest is the one before it.
                job("m", 3, "done", timings=ran), job("m", 4, "cancelled", merged_into_job_id=3, timings=ran),
                job("q", 5, "failed", timings=ran), job("q", 6, "cancelled", timings={"queued": NOW - 50}),
                job("n", 7, "cancelled", never_started=True, timings=ran)]
        rows = board([], jobs, {}, {}, NOW, stopped_since=NOW - 1000)
        self.assertEqual(sorted((row["topic"], row["group"], row["message_id"], row["seen"]) for row in rows), [
            ("m", "result", "om_job_3", None), ("q", "result", "om_job_5", None), ("s", "result", "om_job_2", None)])
        # Seen: it stays KEEP_SEEN from when it was first seen, as a done one.
        self.assertEqual(board([], [stopped], {"s": NOW - 50}, {}, NOW - 51 + KEEP_SEEN)[0]["seen"], NOW - 50)
        self.assertEqual(board([], [stopped], {"s": NOW - 50}, {}, NOW - 50 + KEEP_SEEN), [])
        # One that ended before the record's stopped_since: off the board, as before 2026-11-09.
        self.assertEqual(board([], [stopped], {}, {}, NOW, stopped_since=NOW - 50), [])
        self.assertFalse(board_read.unrun(job("d", 1, "done")))

    def test_a_result_stays_kept_seen_from_when_it_was_first_seen(self):
        done = job("t", 1, "done", timings={"delivered": NOW - 100})
        later = {"t": NOW - 1}  # Read again since: the first time counts.
        first = {"t": {"at": NOW - 100, "seen": NOW - 90}}
        self.assertEqual(board([], [done], later, {}, NOW, results=first)[0]["seen"], NOW - 90)
        self.assertEqual(board([], [done], later, {}, NOW - 90 + KEEP_SEEN, results=first), [])
        # Not seen when the record was written: the read now is the first.
        self.assertEqual(board([], [done], later, {}, NOW, results={"t": {"at": NOW - 100, "seen": None}})[0]["seen"],
                         NOW - 1)
        # The record's is of an earlier result of the topic: this one's is its own.
        self.assertEqual(board([], [done], later, {}, NOW, results={"t": {"at": NOW - 500, "seen": NOW - 400}})[0][
            "seen"], NOW - 1)
        self.assertEqual(board([], [done], {}, {}, NOW, results={"t": {"at": NOW - 100, "seen": True}})[0]["seen"],
                         None)

    def test_the_seen_results_give_way_first_when_there_are_too_many(self):
        rows = ([{"topic": f"q{n}", "group": "waiting", "message_id": f"mq{n}"} for n in range(3)]
                + [{"topic": f"s{n}", "group": "result", "message_id": f"ms{n}", "seen": NOW - n} for n in range(4)]
                + [{"topic": "u", "group": "result", "message_id": "mu", "seen": None}]
                + [{"topic": f"r{n}", "group": "running", "message_id": f"mr{n}"} for n in range(4)])
        pinned, rest = choose(rows, {})
        # Two over: the two seen first go (s3, s2), the others keep their order.
        self.assertEqual([row["topic"] for row in pinned],
                         ["q0", "q1", "q2", "s0", "s1", "u", "r0", "r1", "r2", "r3"])
        self.assertEqual([row["topic"] for row in rest], ["s2", "s3"])
        many = rows + [{"topic": f"x{n}", "group": "running", "message_id": f"mx{n}"} for n in range(4)]
        pinned, rest = choose(many, {})  # Not enough seen ones: the end of the order goes too.
        self.assertEqual(len(pinned), MAX_PINS)
        self.assertEqual([row["topic"] for row in rest], ["s0", "s1", "s2", "s3", "x2", "x3"])

    def test_a_topic_shows_its_running_job_before_its_queued_next(self):
        rows = board([], [job("t", 1, "running"), job("t", 2, "queued")], {}, {}, NOW)
        self.assertEqual([row["message_id"] for row in rows], ["om_job_1"])
        rows = board([], [job("t", 1, "running"), job("t", 2, "cancel_requested")], {}, {}, NOW)
        self.assertEqual([row["message_id"] for row in rows], ["om_job_2"])

    def test_the_newest_question_of_a_topic(self):
        rows = board([question("t", NOW - 60, "om_new"), question("t", NOW - 600, "om_old")], [], {}, {}, NOW)
        self.assertEqual([row["message_id"] for row in rows], ["om_new"])

    def test_priority_comes_first_in_its_group(self):
        jobs = [job(f"t{n}", n, "running", timings={"backend_started": NOW - n}) for n in range(1, 5)]
        rows = board([question("q", NOW - 1)], jobs, {}, {"t4": NOW - 5, "t3": NOW - 1, "q": NOW}, NOW)
        self.assertEqual([row["topic"] for row in rows], ["q", "t3", "t4", "t1", "t2"])

    def test_when_a_job_ended(self):
        self.assertEqual(ended_at({"timings": {"delivered": 5, "execution_finished": 4}, "updated_at": "x"}), 4)
        self.assertEqual(ended_at({"timings": {"delivered": 5}}), 5)
        self.assertEqual(ended_at({"updated_at": "2026-11-05T05:20:00Z"}), NOW)
        self.assertEqual(ended_at({}), 0)

    def test_choose_keeps_the_limit_and_skips_what_the_owner_took_off(self):
        rows = [{"topic": f"t{n}", "group": "running", "message_id": f"m{n}"} for n in range(MAX_PINS + 3)]
        pinned, rest = choose(rows, {"t0": {"group": "running", "at": 0.0}})
        self.assertEqual([row["topic"] for row in pinned], [f"t{n}" for n in range(1, MAX_PINS + 1)])
        self.assertEqual([row["topic"] for row in rest], ["t0", f"t{MAX_PINS + 1}", f"t{MAX_PINS + 2}"])

    def test_a_topic_taken_off_comes_back_with_a_newer_question_or_result(self):
        def row(topic, group, at):
            return {"topic": topic, "group": group, "message_id": f"m_{topic}", "at": at}
        off = {topic: {"group": "waiting", "at": NOW - 100} for topic in ("same", "asked", "ended", "runs", "stale",
                                                                          "gone")}
        rows = [row("same", "waiting", NOW - 100), row("asked", "waiting", NOW - 10), row("ended", "result", NOW - 5),
                row("runs", "running", NOW - 1)]
        kept = active_dismissed(off, rows, asking=frozenset({"stale"}))
        # A newer question or result brings it back; running does not; one with nothing left is forgotten, but not
        # one whose question waits over a day (no row then).
        self.assertEqual(sorted(kept), ["runs", "same", "stale"])
        self.assertEqual(kept["same"], off["same"])
        # Before 2026-11-06 the value was the group: off while the topic stays in it, as of its row there.
        old = active_dismissed({"same": "waiting", "runs": "result", "gone": "waiting", "bad": 3}, rows)
        self.assertEqual(old, {"same": {"group": "waiting", "at": NOW - 100}})
        self.assertEqual(active_dismissed(None, rows), {})

    def test_a_pin_that_drops_to_running_keeps_its_place_for_a_while(self):
        rows = [{"topic": "q", "group": "running", "message_id": "m_job", "at": NOW - 60},
                {"topic": "x", "group": "running", "message_id": "m_x", "at": NOW - 90},
                {"topic": "r", "group": "result", "message_id": "m_r", "at": NOW - 50}]
        pins_now = [{"message_id": "m_q", "topic_id": "q", "group": "waiting", "at": NOW - 200}]
        placed = buffered(rows, pins_now, {}, NOW)
        self.assertEqual([(row["topic"], row["group"], row["message_id"]) for row in placed],
                         [("q", "waiting", "m_q"), ("x", "running", "m_x"), ("r", "result", "m_r")])
        self.assertIs(buffered(rows, pins_now, {}, NOW - 60 + pins.DEMOTE), rows)  # Run long enough: moves down.
        self.assertIs(buffered(rows, [{**pins_now[0], "at": None}], {}, NOW), rows)  # A pin from before: as it is.


class PlanTests(unittest.TestCase):
    def test_cases(self):
        cases = [
            ([], [], []),
            (["a", "b", "c"], [], [("pin", "c"), ("pin", "b"), ("pin", "a")]),
            (["a", "b"], ["a", "b"], []),  # In place: nothing.
            (["a"], ["x", "a", "y"], [("unpin", "x"), ("unpin", "y")]),
            (["new", "a", "b"], ["a", "b"], [("pin", "new")]),  # Only the new one, to the top.
            (["a", "new", "b"], ["a", "b"], [("pin", "new"), ("move", "a")]),
            (["a", "b", "c"], ["c", "a", "b"], [("move", "b"), ("move", "a")]),  # c stays at the bottom.
            (["a", "b", "c"], ["a", "c", "b"], [("move", "b"), ("move", "a")]),
            (["a", "b"], ["b", "a"], [("move", "a")]),
            (["a", "b"], ["x", "b", "a"], [("unpin", "x"), ("move", "a")]),
        ]
        for wanted, current, steps in cases:
            with self.subTest(wanted=wanted, current=current):
                self.assertEqual(plan(wanted, current), steps)
                self.assertEqual(apply(steps, current), wanted)

    def test_stale_pins_all_move(self):
        steps = plan(["a", "b"], ["a", "b"], stale=frozenset({"a", "b"}))
        self.assertEqual(steps, [("move", "b"), ("move", "a")])
        self.assertEqual(plan(["a", "b"], ["a", "b"], stale=frozenset({"a"})), [("move", "a")])

    def test_any_start_reaches_the_order_wanted(self):
        rng = random.Random(7)
        for _ in range(300):
            names = [f"m{n}" for n in range(8)]
            wanted = rng.sample(names, rng.randint(0, 6))
            current = rng.sample(names, rng.randint(0, 6))
            stale = frozenset(rng.sample(current, rng.randint(0, len(current))))
            steps = plan(wanted, current, stale)
            self.assertEqual(apply(steps, current), wanted, (wanted, current, stale))
            self.assertFalse({message for op, message in steps if op != "unpin"} & (set(current) - set(wanted)))


class FakePins:
    """Feishu's pins in one chat: ``pins create`` answers the original create_time of a message pinned already."""

    def __init__(self):
        self.created = {}  # message -> create_time (ms)
        self.clock = int(NOW * 1000)
        self.calls = []
        self.fail = set()

    def pin_info(self, profile, message):
        self.calls.append(("pin", message))
        if ("pin", message) in self.fail:
            return None
        if message not in self.created:
            self.clock += 1000
            self.created[message] = self.clock
        return {"message_id": message, "create_time": str(self.created[message])}

    def pin(self, profile, message):
        return self.pin_info(profile, message) is not None

    def unpin(self, profile, message):
        self.calls.append(("unpin", message))
        if ("unpin", message) in self.fail:
            return False
        self.created.pop(message, None)
        return True

    def order(self):
        return sorted(self.created, key=self.created.get, reverse=True)


class RoundTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        self.feishu = FakePins()
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_index1"))
        messenger.put_card = mock.Mock(return_value={"code": 0})
        messenger.pin, messenger.unpin, messenger.pin_info = self.feishu.pin, self.feishu.unpin, self.feishu.pin_info
        self.overview = self.app.overview
        self.app.runtime.access.remember_owner_chat("oc_dm")
        for number in range(1, 15):
            self.topic(f"t{number}", 400 + number)
        write_json(self.state / pins.RECORD, {"schema_version": 1, "since": NOW - 7 * 86400})  # A board running for a week.
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)

    def topic(self, topic_id, number):
        write_topic(self.state, {"topic_number": number}, topic_id=topic_id)
        write_private_text(self.state / "topics" / topic_id / "history.jsonl",
                           json.dumps({"role": "user", "display_content": f"话题 {number}"}) + "\n")

    def job(self, key, job_id, status="running", topic="t1", **fields):
        upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id="oc_dm", topic_id=topic,
                          card_message_id=f"om_{key}", harness_id="claude", **fields)

    def question(self, token, topic, at, **fields):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic, "chat_id": "oc_dm", "job_id": 9, "status": "pending",
            "created_at": at, "question_message_id": f"om_{token}", "questions": [{"question": "要不要做？"}],
            **fields}))

    def record(self):
        return json.loads((self.state / pins.RECORD).read_text())

    def last_card(self):
        messenger = self.app.messenger
        return (messenger.put_card if messenger.put_card.called else messenger.create_and_send_card).call_args.args[2]

    def test_the_board_is_pinned_above_the_index_in_its_order(self):
        self.job("run", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("res", 2, status="done", timings={"delivered": NOW - 30}, topic="t2")
        self.question("q", "t3", NOW - 10)
        self.question("q_old", "t4", NOW - 2 * 86400)
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_q", "om_run", "om_res", "om_index1"])  # The index card first.
        record = self.record()
        self.assertEqual(record["chat_id"], "oc_dm")
        self.assertEqual(record["index_message_id"], "om_index1")
        self.assertEqual([(pin["message_id"], pin["topic_id"], pin["group"]) for pin in record["pins"]],
                         [("om_q", "t3", "waiting"), ("om_run", "t1", "running"), ("om_res", "t2", "result")])
        self.assertEqual(record["pins"][0]["create_time"], str(self.feishu.created["om_q"]))
        self.assertIn("pinned board pin message=om_run topic=t1 group=running", self.logs)
        # Once in order, nothing is called again.
        calls = len(self.feishu.calls)
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)
        self.assertEqual(len(self.feishu.calls), calls)
        # The card redrawn as the index: counts, one line for the pins, the old question.
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("等你 2 · 在做 1 · 有结果 1", card)  # 等我: the pinned one and the old one (core/board_read.py waits).
        self.assertIn("Pin 着 3 个话题", card)  # Once, in the header.
        self.assertEqual(card.count("Pin 着 3 个话题"), 1)
        self.assertIn("在 Pin 页里点话题的卡片，直接打开那个话题", card)
        self.assertIn("另有 1 个等了一天以上：T404", card)
        self.assertNotIn("话题 401", card)  # Pinned: not listed again.

    def test_a_task_waiting_for_start_is_pinned_by_its_launch_card(self):
        """2026-11-21 T622: pinned under 等你回答 by its launch card (a topic opened by a reply to a node's alert has no other
        card at the top level), and 手上的事 does not list it again; once 开始 is pressed it runs as any topic."""
        self.job("ws", 1, status="waiting_workspace", topic="t1", timings={"received": NOW - 60})
        write_json(self.state / "launch-forms" / "f1.json", {"topic_id": "t1", "chat_id": "oc_dm", "status": "ready",
                                                             "message_id": "om_launch1", "harness_id": "claude"})
        self.job("run", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_launch1", "om_run", "om_index1"])
        self.assertEqual([(pin["message_id"], pin["topic_id"], pin["group"]) for pin in self.record()["pins"]],
                         [("om_launch1", "t1", "waiting"), ("om_run", "t2", "running")])
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)  # The card redrawn as the index of the pins.
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("等你 1 · 在做 1", card)
        self.assertNotIn("话题 401", card)  # Pinned: not listed again.
        # 开始: the card is used, the task runs; the topic's pin goes to its task card once it has run DEMOTE seconds.
        write_json(self.state / "launch-forms" / "f1.json", {"topic_id": "t1", "chat_id": "oc_dm", "status": "submitted",
                                                             "message_id": "om_launch1", "harness_id": "claude"})
        self.job("ws", 1, status="queued", topic="t1")
        self.job("ws", 1, status="running", topic="t1", timings={"received": NOW - 60, "backend_started": NOW + 10})
        self.overview.tick(self.ctx, NOW + 10 + pins.DEMOTE)
        self.assertEqual(self.feishu.order(), ["om_ws", "om_run", "om_index1"])

    def test_a_change_waits_for_the_gap_and_moves_only_what_must(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index1"])
        # b's result goes below the running a.
        self.job("b", 2, status="done", timings={"delivered": NOW + 10}, topic="t2")
        self.overview.tick(self.ctx, NOW + 20)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index1"])  # Within GAP.
        del self.feishu.calls[:]
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_a", "om_b", "om_index1"])
        # om_b stays at the bottom; om_a: probed, unpinned, pinned.
        self.assertEqual(self.feishu.calls, [("pin", "om_a"), ("unpin", "om_a"), ("pin", "om_a")])
        self.assertIn("pinned board move message=om_a topic=t1 group=running", self.logs)
        self.assertEqual([pin["group"] for pin in self.record()["pins"]], ["running", "result"])

    def test_results_from_before_the_board_first_ran_are_not_pinned(self):
        (self.state / pins.RECORD).unlink()  # Switched on now.
        self.overview.pins.since = None
        self.job("old", 1, status="done", timings={"execution_finished": NOW - 600}, topic="t1")
        self.job("run", 2, timings={"backend_started": NOW - 60}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_run", "om_index1"])
        self.assertEqual(self.record()["since"], NOW)
        self.job("run", 2, status="done", timings={"execution_finished": NOW + 30}, topic="t2")  # Ends after: pinned.
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_run", "om_index1"])
        self.assertEqual(self.record()["pins"][0]["group"], "result")
        self.overview.pins.since = None  # A restart keeps the record's.
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)
        self.assertEqual(self.overview.pins.since, NOW)

    def test_seen_results_and_ended_tasks_come_off(self):
        self.job("a", 1, status="done", timings={"delivered": NOW - 30}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])
        write_json(self.state / SEEN_FILE, {"t1": NOW - 5})
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_index1"])
        self.assertEqual(self.record()["pins"], [])
        self.assertIn("pinned board unpin message=om_a topic=t1 group=result", self.logs)

    def test_a_result_that_ends_since_the_rules_stays_kept_seen_from_its_first_read(self):
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.record()["keep_since"], NOW)  # A record from before: its rules start now.
        self.job("a", 1, status="done", timings={"delivered": NOW + 30}, topic="t1")
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])
        self.assertEqual(self.record()["results"], {"t1": {"at": NOW + 30, "seen": None}})
        write_json(self.state / SEEN_FILE, {"t1": NOW + 40})
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])  # Seen: it stays.
        self.assertEqual(self.record()["results"]["t1"]["seen"], NOW + 40)
        write_json(self.state / SEEN_FILE, {"t1": NOW + 500})  # Read again: KEEP_SEEN still runs from the first.
        self.overview.pins.keep_since = None  # A restart keeps the record's.
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)
        self.assertEqual(self.overview.pins.keep_since, NOW)
        self.assertEqual(self.record()["results"]["t1"]["seen"], NOW + 40)
        self.overview.tick(self.ctx, NOW + 40 + board_read.KEEP_SEEN)
        self.assertEqual(self.feishu.order(), ["om_index1"])
        self.assertEqual(self.record()["results"], {})
        self.assertIn("pinned board unpin message=om_a topic=t1 group=result", self.logs)

    def test_a_turn_stopped_since_the_rule_stays_pinned_under_the_results(self):
        self.job("old", 1, status="cancelled", topic="t1",
                 timings={"backend_started": NOW - 300, "execution_finished": NOW - 100})
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.record()["stopped_since"], NOW)  # A record from before: the rule starts now.
        self.assertEqual(self.feishu.order(), ["om_index1"])  # Stopped before it: not pinned, as before.
        self.job("a", 2, status="cancelled", topic="t2",
                 timings={"backend_started": NOW + 5, "execution_finished": NOW + 30}, status_message="用户停止了任务")
        self.overview.pins.stopped_since = None  # A restart keeps the record's.
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.overview.pins.stopped_since, NOW)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])  # Its task card, under 有结果.
        self.assertEqual([(pin["topic_id"], pin["group"]) for pin in self.record()["pins"]], [("t2", "result")])
        self.assertEqual(self.record()["results"], {"t2": {"at": NOW + 30, "seen": None}})
        write_json(self.state / SEEN_FILE, {"t2": NOW + 40})
        self.overview.tick(self.ctx, NOW + 39 + board_read.KEEP_SEEN)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])  # Seen: it stays KEEP_SEEN.
        self.overview.tick(self.ctx, NOW + 40 + board_read.KEEP_SEEN)
        self.assertEqual(self.feishu.order(), ["om_index1"])

    def test_a_pin_the_owner_took_off_stays_off_until_its_topic_has_something_new(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index1"])
        self.feishu.unpin("", "om_a")  # By hand in the client.
        write_json(self.state / board_read.PRIORITY, {"t1": NOW})  # om_a has to move to the top.
        self.overview.tick(self.ctx, NOW + pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_b", "om_index1"])
        self.assertEqual(self.record()["dismissed"], {"t1": {"group": "running", "at": NOW - 60}})
        self.assertIn("pinned board dismissed message=om_a topic=t1 group=running", self.logs)
        calls = len(self.feishu.calls)
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)
        self.assertEqual(len(self.feishu.calls), calls)  # Not pinned again while it runs.
        self.job("a", 1, status="done", timings={"delivered": NOW + 4 * pins.GAP})
        self.overview.tick(self.ctx, NOW + 5 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index1"])  # A result now: pinned again, under b.
        self.assertEqual(self.record()["dismissed"], {})

    def test_a_new_index_card_has_every_pin_laid_over_it_again(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        self.app.messenger.create_and_send_card.return_value = ("card2", "om_index2")
        self.overview.tick(self.ctx, NOW + overview.ROTATE)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index2"])
        self.assertEqual(self.record()["index_message_id"], "om_index2")
        self.assertIn("pinned board move message=om_b topic=t2 group=running", self.logs)

    def test_more_than_the_limit_are_left_to_the_index(self):
        for n in range(1, MAX_PINS + 3):
            self.job(f"j{n}", n, timings={"backend_started": NOW - 100 + n}, topic=f"t{n}")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), [f"om_j{n}" for n in range(MAX_PINS + 2, 2, -1)] + ["om_index1"])
        card = json.dumps(self.app.messenger.create_and_send_card.call_args.args[2], ensure_ascii=False)
        self.assertIn("**在做 · 未 Pin**  <font color='grey'>12</font>", card)  # The first card: drawn before any pin.
        self.overview.tick(self.ctx, NOW + 20)
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("**在做 · 未 Pin**  <font color='grey'>2</font>", card)
        self.assertIn("话题 401", card)
        self.assertIn("话题 402", card)

    def test_a_failed_call_stops_the_round_and_waits_longer(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.feishu.fail.add(("pin", "om_b"))
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])
        self.assertIn("pinned board failed op=pin message=om_b failures=1", self.logs)
        self.assertEqual([pin["message_id"] for pin in self.record()["pins"]], ["om_a"])  # Written as it went.
        self.feishu.fail.clear()
        self.overview.tick(self.ctx, NOW + pins.GAP)  # Twice GAP after one failure.
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_b", "om_a", "om_index1"])

    def test_a_message_found_pinned_already_is_moved_to_the_top(self):
        self.feishu.created["om_a"] = int((NOW - 3600) * 1000)  # Pinned by hand an hour ago.
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])
        self.assertIn("pinned board move message=om_a topic=t1 group=running", self.logs)

    def test_nothing_before_the_index_card_is_pinned(self):
        (self.state / pins.RECORD).unlink()
        self.job("a", 1, topic="t1")
        self.app.messenger.pin = mock.Mock(return_value=False)
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.calls, [])
        self.assertFalse((self.state / pins.RECORD).exists())

    def test_off_unless_both_switches(self):
        (self.state / pins.RECORD).unlink()
        self.job("a", 1, topic="t1")
        with mock.patch.dict(os.environ, {PINNED_TOPICS_ENV: ""}):
            self.overview.tick(self.ctx, NOW)
            self.assertEqual(self.feishu.calls, [("pin", "om_index1")])  # The index card's own pin only.
            self.assertFalse((self.state / pins.RECORD).exists())
            card = self.app.messenger.create_and_send_card.call_args.args[2]
            self.assertNotIn("Pin 着", json.dumps(card, ensure_ascii=False))
        with mock.patch.dict(os.environ, {overview.ENV: ""}):
            self.assertNotIn("overview", dict(self.app.worker_targets()))


class DismissTests(RoundTests):
    """Topics taken off the board: off the pins, the index card's rows and counts, back with something new."""

    def button(self, card):
        found = []

        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "callback" and isinstance(value.get("value"), dict):
                    found.append(value["value"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(card)
        return found

    def click(self, value, at):
        with mock.patch("time.time", return_value=at):
            self.app.handle_card_action_event({"event_id": f"c{at}", "operator_id": "owner", "chat_id": "oc_dm",
                                               "message_id": "om_index1", "action_value": value}, self.ctx)

    def tick(self, at):
        self.feishu.clock = max(self.feishu.clock, int(at * 1000))  # Feishu's clock is the round's.
        self.overview.tick(self.ctx, at)

    def test_the_index_card_leaves_them_out_but_one_line(self):
        self.job("run", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.question("q", "t3", NOW - 10)
        dismiss(self.state, "oc_dm", ["t3"], NOW - 5, "waiting")
        self.overview.tick(self.ctx, NOW)
        self.overview.tick(self.ctx, NOW + 20)
        self.assertEqual(self.feishu.order(), ["om_run", "om_index1"])
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("等你 0 · 在做 1 · 有结果 0", card)
        self.assertIn("你撤下的 1 个：T403", card)
        self.assertNotIn("话题 403", card)
        self.question("q2", "t3", NOW + 30)  # A newer question: back, pinned at once.
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_q2", "om_run", "om_index1"])
        self.assertEqual(self.record()["dismissed"], {})
        self.assertNotIn("你撤下的", json.dumps(self.last_card(), ensure_ascii=False))

    def test_these_need_no_answer_takes_off_the_topics_it_lists(self):
        self.question("q_old1", "t4", NOW - 2 * 86400)
        self.question("q_old2", "t5", NOW - 3 * 86400)
        self.question("q", "t3", NOW - 10)
        self.overview.tick(self.ctx, NOW)
        values = self.button(self.last_card())
        self.assertEqual(values, [{"kind": "bridge_overview_dismiss", "topics": ["t4", "t5"]}])
        self.question("q_old3", "t6", NOW - 2 * 86400)  # Not on the card clicked: not taken off.
        before = (self.state / "questions" / "q_old1.json").read_text()
        self.click(values[0], NOW + 30)
        self.assertTrue(any("outcome=overview_dismiss " in line for line in self.logs), self.logs)
        self.assertEqual(sorted(self.record()["dismissed"]), ["t4", "t5"])
        self.assertEqual((self.state / "questions" / "q_old1.json").read_text(), before)  # Still answerable.
        card = json.dumps(self.last_card(), ensure_ascii=False)  # Redrawn after the click, not GAP later.
        self.assertTrue(self.app.messenger.put_card.called)
        self.assertIn("另有 1 个等了一天以上：T406", card)
        self.assertIn("你撤下的 2 个：T404、T405", card)
        self.assertEqual(self.button(self.last_card()), [
            {"kind": "bridge_overview_dismiss", "topics": ["t6"]},
            {"kind": "bridge_overview_undismiss", "taken": {"t4": NOW + 30, "t5": NOW + 30}}])  # 撤销最近一次.
        # Not the owner: refused, nothing changes.
        self.assertEqual(self.overview.claim(self.ctx, {}, values[0], False), "owner_only")
        self.assertIsNone(self.overview.claim(self.ctx, {}, {"kind": "bridge_job_stop"}, True))
        # A result after it brings a topic back.
        self.job("res", 2, status="done", timings={"delivered": NOW + 100}, topic="t4")
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)
        self.assertEqual(sorted(self.record()["dismissed"]), ["t5"])
        self.assertIn("om_res", self.feishu.order())

    def undo_values(self):
        return [value for value in self.button(self.last_card()) if value["kind"] == "bridge_overview_undismiss"]

    def test_undo_puts_back_the_latest_click_only_and_not_what_he_unpinned_or_the_butler_took_off(self):
        for token, topic in (("q1", "t4"), ("q2", "t5"), ("q3", "t6")):
            self.question(token, topic, NOW - 2 * 86400)
        self.job("run", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.overview.tick(self.ctx, NOW)
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t4"]}, NOW + 30)
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t5", "t6"]}, NOW + 60)
        dismiss(self.state, "oc_dm", ["t1"], NOW + 90, "running")  # The butler's (or a hand unpin's): newer, not a click.
        self.overview.tick(self.ctx, NOW + 100)
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertIn("你撤下的 4 个", card)
        self.assertIn("撤销最近一次", card)
        undo, = self.undo_values()
        self.assertEqual(undo, {"kind": "bridge_overview_undismiss", "taken": {"t5": NOW + 60, "t6": NOW + 60}})
        self.click(undo, NOW + 110)
        self.assertTrue(any("outcome=overview_undismiss " in line for line in self.logs), self.logs)
        self.assertEqual(sorted(self.record()["dismissed"]), ["t1", "t4"])
        card = json.dumps(self.last_card(), ensure_ascii=False)  # Redrawn at once, the two back among the old ones.
        self.assertIn("另有 2 个等了一天以上：T405、T406", card)
        undo, = self.undo_values()  # Now his earlier click is the latest one.
        self.assertEqual(undo["taken"], {"t4": NOW + 30})

    def test_undoing_a_dismissal_whose_question_was_answered_or_retired_meanwhile_does_nothing_wrong(self):
        self.question("q1", "t4", NOW - 2 * 86400)
        self.question("q2", "t5", NOW - 2 * 86400)
        self.overview.tick(self.ctx, NOW)
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t4", "t5"]}, NOW + 30)
        undo, = self.undo_values()
        self.question("q1", "t4", NOW - 2 * 86400, status="answered")  # Answered on its card meanwhile.
        self.question("q2", "t5", NOW - 2 * 86400, status="superseded")  # Retired (its task is gone).
        self.overview.tick(self.ctx, NOW + 3 * pins.GAP)  # The round forgets what nothing waits for any more.
        self.assertEqual(self.record()["dismissed"], {})
        self.click(undo, NOW + 4 * pins.GAP)  # No crash, nothing brought back that no longer waits.
        self.assertTrue(any("outcome=overview_undismiss " in line for line in self.logs), self.logs)
        self.assertIn("pinned overview undismissed topics=0/2", self.logs)
        card = json.dumps(self.last_card(), ensure_ascii=False)
        self.assertNotIn("另有", card)
        self.assertNotIn("你撤下的", card)
        self.assertEqual(self.undo_values(), [])

    def test_an_undo_of_an_earlier_click_leaves_a_topic_taken_off_again_since(self):
        self.question("q1", "t4", NOW - 2 * 86400)
        self.overview.tick(self.ctx, NOW)
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t4"]}, NOW + 30)
        old, = self.undo_values()
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t4"]}, NOW + 60)  # Again, from another card.
        self.click(old, NOW + 90)
        self.assertEqual(self.record()["dismissed"]["t4"]["at"], NOW + 60)
        self.assertIn("pinned overview undismissed topics=0/1", self.logs)

    def test_undo_is_the_owners_and_reads_its_value_as_data(self):
        self.question("q1", "t4", NOW - 2 * 86400)
        self.overview.tick(self.ctx, NOW)
        self.click({"kind": "bridge_overview_dismiss", "topics": ["t4"]}, NOW + 30)
        undo, = self.undo_values()
        self.assertEqual(self.overview.claim(self.ctx, {}, undo, False), "owner_only")
        self.assertEqual(sorted(self.record()["dismissed"]), ["t4"])
        for bad in ({"kind": undo["kind"]}, {"kind": undo["kind"], "taken": ["t4"]},
                    {"kind": undo["kind"], "taken": {"t4": "x", "": 1, "t5": True}}):
            self.assertEqual(self.overview.claim(self.ctx, {}, bad, True), "overview_undismiss_ignored")
        self.assertEqual(sorted(self.record()["dismissed"]), ["t4"])

    def test_an_undo_during_a_round_is_not_written_back_by_it(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        self.overview.tick(self.ctx, NOW)
        dismiss(self.state, "oc_dm", ["t2"], NOW + 5, "running", "card")
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)  # Its pin comes off.
        self.job("c", 3, timings={"backend_started": NOW}, topic="t3")  # Something to pin: the round has a step.
        pin_info = self.feishu.pin_info

        def meanwhile(profile, message):
            pins.undismiss(self.state, "oc_dm", {"t2": NOW + 5})
            return pin_info(profile, message)
        self.app.messenger.pin_info = meanwhile
        self.overview.tick(self.ctx, NOW + 4 * pins.GAP)
        self.app.messenger.pin_info = pin_info
        self.assertEqual(self.record()["dismissed"], {})
        self.overview.tick(self.ctx, NOW + 6 * pins.GAP)
        self.assertIn("om_b", self.feishu.order())  # Back on the board, pinned again.

    def test_a_dismissal_written_during_a_round_is_kept(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        pin_info = self.feishu.pin_info

        def meanwhile(profile, message):
            if message == "om_a":
                dismiss(self.state, "oc_dm", ["t2"], NOW)
            return pin_info(profile, message)
        self.app.messenger.pin_info = meanwhile
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(list(self.record()["dismissed"]), ["t2"])
        self.app.messenger.pin_info = pin_info
        self.overview.tick(self.ctx, NOW + 2 * pins.GAP)
        self.assertEqual(self.feishu.order(), ["om_a", "om_index1"])

    def test_a_record_from_before_reads_as_it_did(self):
        self.job("a", 1, timings={"backend_started": NOW - 60}, topic="t1")
        self.job("b", 2, timings={"backend_started": NOW - 30}, topic="t2")
        write_json(self.state / pins.RECORD, {"schema_version": 1, "since": NOW - 86400, "chat_id": "oc_dm",
                                              "pins": [], "dismissed": {"t1": "running", "t2": "result"}})
        self.overview.tick(self.ctx, NOW)
        self.assertEqual(self.feishu.order(), ["om_b", "om_index1"])
        self.assertEqual(self.record()["dismissed"], {"t1": {"group": "running", "at": NOW - 60}})

    def test_a_topic_looping_between_questions_and_short_runs_moves_few_pins(self):
        """T391 on 2026-11-06: question, answer, a one or two minute run, the next question. The question card stays
        pinned while it runs, so only each new question costs a pin and an unpin; the rest never move."""
        self.job("res", 1, status="done", timings={"delivered": NOW - 900}, topic="t2")
        self.job("run3", 2, timings={"backend_started": NOW - 900}, topic="t3")
        self.job("run4", 3, timings={"backend_started": NOW - 800}, topic="t4")
        self.overview.tick(self.ctx, NOW - 600)
        self.assertEqual(self.feishu.order(), ["om_run4", "om_run3", "om_res", "om_index1"])
        del self.feishu.calls[:]
        cycles = 6
        for cycle in range(cycles):
            start = NOW + cycle * 300
            self.question(f"q{cycle}", "t1", start)
            for at in (start, start + 60):
                self.tick(at)
            self.assertEqual(self.feishu.order()[0], f"om_q{cycle}")  # A new question: pinned at once.
            self.question(f"q{cycle}", "t1", start, status="answered")
            if cycle:
                self.job(f"loop{cycle - 1}", 10 + cycle - 1, status="continued", topic="t1")
            self.job(f"loop{cycle}", 10 + cycle, timings={"backend_started": start + 120}, topic="t1")
            for at in (start + 120, start + 180, start + 240):
                self.tick(at)
            self.assertEqual(self.feishu.order(), [f"om_q{cycle}", "om_run4", "om_run3", "om_res", "om_index1"])
            card = json.dumps(self.last_card(), ensure_ascii=False)
            self.assertIn("等你 0 · 在做 3 · 有结果 1", card)  # The counts are the real state.
            self.job(f"loop{cycle}", 10 + cycle, status="waiting_user", topic="t1")
        self.assertLessEqual(len(self.feishu.calls), 2 * cycles, self.feishu.calls)
        # Running past DEMOTE, it goes down to 进行中 at last.
        start = NOW + cycles * 300
        self.question(f"q{cycles}", "t1", start)
        self.tick(start)
        self.question(f"q{cycles}", "t1", start, status="answered")
        self.job(f"loop{cycles}", 10 + cycles, timings={"backend_started": start + 60}, topic="t1")
        self.tick(start + 60 + pins.DEMOTE)
        self.assertEqual(self.feishu.order(), [f"om_loop{cycles}", "om_run4", "om_run3", "om_res", "om_index1"])


class CardTests(unittest.TestCase):
    def test_the_index_card(self):
        view = {"waiting": [], "running": [], "ended": [], "board": {
            "counts": {"waiting": 0, "result": 0, "running": 0}, "pinned": 0, "waiting": [], "result": [],
            "running": [], "stale": []}}
        card = overview_card(view, NOW)
        # The 看板's look (T604 #52): the band alone, the counts in the chat list's preview.
        (band,) = card["body"]["elements"]
        self.assertIn("都在掌握中", json.dumps(band, ensure_ascii=False))
        self.assertEqual(card["header"]["subtitle"]["content"], "私聊里在推进的事")
        self.assertEqual(card["config"]["summary"]["content"], "手上的事 · 等你 0 · 在做 0 · 有结果 0")

    def test_a_stopped_topic_without_a_pin_is_listed_with_the_results(self):
        rows = [{"label": "T401", "title": "改看板", "topic": "t1", "status": "cancelled", "at": NOW - 60},
                {"label": "T402", "title": "查日志", "topic": "t2", "status": "done", "at": NOW - 30}]
        view = {"waiting": [], "running": [], "ended": [], "board": {
            "counts": {"waiting": 0, "result": 2, "running": 0}, "pinned": 0, "waiting": [], "result": rows,
            "running": [], "stale": []}}
        card = json.dumps(overview_card(view, NOW), ensure_ascii=False)
        self.assertIn("**有结果 · 未 Pin**  <font color='grey'>2</font>", card)
        # Each row's state as the 看板 says it (cards/ui.py topic_row): the stop, the result not seen.
        self.assertIn("<font color='grey'>**已停止**</font> **T401 改看板**", card)
        self.assertIn("<font color='green'>**有结果**</font> **T402 查日志**", card)


if __name__ == "__main__":
    unittest.main()

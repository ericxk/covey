"""早晚汇总 (app/digest.py, cards/digest.py): off unless COVEY_DIGEST_TIMES lists a time; which time is due
(across midnight, within the catch-up hour, each once), what the three parts list, nothing sent when all are empty,
each part cut at SHOWN rows, a send retried with the same key and given up; the topics taken off the board left out,
the questions waiting over a day as 手上的事's line with its button 这些都不用回答了 (only while it takes effect), and
its click taking them off and turning that line grey on the digest card; the bot menu's 看板, the same card at any
time, folding in place on a second click, without touching digest.json."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import state_dir, write_topic
from support.world import UnifiedCase
from covey.app import digest, overview, pins
from covey.app.digest import (CATCH_UP, ENV, RECORD, ZONE, Digest, collect, due, parse_times, since_of,
                                       slots)
from covey.cards.digest import SHOWN, digest_card, digest_text
from covey.cards.overview import DISMISS
from covey.core.jobs import upsert_job_record
from covey.core.butler import butler_topic_id
from covey.core.state import write_json, write_private_text
from covey.core.topic_seen import PINNED_TOPICS_ENV
from covey.feishu.messenger import Voice

CHAT = "oc_dm"
ON = {overview.ENV: "1", PINNED_TOPICS_ENV: "1"}  # 手上的事 and the board: 这些都不用回答了 takes effect.


def at(day, hour, minute, second=0):
    """Epoch seconds of 2026-11-<day> hour:minute at UTC+8."""
    return datetime.datetime(2026, 11, day, hour, minute, second, tzinfo=ZONE).timestamp()


def texts(card):
    return [element.get("content", "") for element in card["body"]["elements"]]


def buttons(card):
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


class TimeTests(unittest.TestCase):
    def test_parse_sorts_drops_repeats_and_logs_what_is_not_a_time(self):
        logs = []
        self.assertEqual(parse_times("23:30, 09:30,9:30,,bad,24:00,12:60", logs.append), [570, 1410])
        self.assertEqual(len(logs), 3)
        self.assertEqual(parse_times(""), [])

    def test_due_once_within_the_catch_up_hour(self):
        times = [570, 1410]  # 09:30, 23:30
        self.assertEqual(due(times, at(8, 9, 30), None), at(8, 9, 30))
        self.assertEqual(due(times, at(8, 10, 29), None), at(8, 9, 30))  # The Bridge was stopped at 09:30.
        self.assertIsNone(due(times, at(8, 9, 30) + CATCH_UP + 1, None))  # Too late: skipped.
        self.assertIsNone(due(times, at(8, 9, 45), at(8, 9, 30)))  # Sent already.
        self.assertEqual(due(times, at(8, 23, 31), at(8, 9, 30)), at(8, 23, 30))
        self.assertIsNone(due(times, at(8, 9, 29), at(7, 23, 30)))

    def test_across_midnight(self):
        self.assertEqual(due([1410], at(9, 0, 15), at(7, 23, 30)), at(8, 23, 30))
        self.assertEqual(slots([1410], at(9, 0, 15))[-2:], [at(7, 23, 30), at(8, 23, 30)])
        self.assertEqual(due([5], at(9, 0, 6), None), at(9, 0, 5))

    def test_done_starts_at_the_time_before_or_the_last_sent_when_times_were_missed(self):
        times = [570, 1410]
        self.assertEqual(since_of(times, at(8, 23, 30), at(8, 9, 30)), at(8, 9, 30))
        self.assertEqual(since_of(times, at(8, 9, 30), at(7, 23, 30)), at(7, 23, 30))
        self.assertEqual(since_of(times, at(8, 23, 30), at(7, 9, 30)), at(7, 9, 30))  # Two missed.
        self.assertEqual(since_of(times, at(8, 23, 30), at(1, 9, 30)), at(8, 23, 30) - digest.LOOKBACK)
        self.assertEqual(since_of([570], at(8, 9, 30), None), at(7, 9, 30))


class Records:
    """Topics, questions and jobs written as the Bridge writes them, for ``collect`` at the real time (board_read.read
    reads jobs by when their record was written)."""

    def setUp(self):
        self.state = state_dir(self)
        self.now = time.time()

    def topic(self, topic_id, number):
        write_topic(self.state, {"topic_number": number}, topic_id=topic_id)
        write_private_text(self.state / "topics" / topic_id / "history.jsonl",
                           json.dumps({"role": "user", "display_content": f"话题 {number}"}) + "\n")

    def job(self, key, job_id, status, topic, **fields):
        upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id=CHAT, topic_id=topic,
                          card_message_id=f"om_{key}", harness_id="claude", **fields)

    def question(self, token, topic, asked_at, text="要不要做？"):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic, "chat_id": CHAT, "job_id": 9, "status": "pending",
            "created_at": asked_at, "question_message_id": f"om_{token}", "questions": [{"question": text}]}))


class CollectTests(Records, unittest.TestCase):
    def test_three_parts(self):
        now, slot, since = self.now, self.now - 60, self.now - 12 * 3600
        for number, topic in enumerate(("ask", "start", "run", "seen", "unseen", "old", "again"), 401):
            self.topic(topic, number)
        self.question("q1", "ask", now - 600, "用表格吗？")
        self.job("j-start", 1, "waiting_workspace", "start", timings={"received": now - 120})
        self.job("j-run", 2, "running", "run", phase="跑测试", timings={"backend_started": now - 300})
        self.job("j-run-next", 3, "queued", "run")
        self.job("j-seen", 4, "done", "seen", timings={"backend_started": now - 7200, "execution_finished": now - 3600})
        self.job("j-unseen", 5, "failed", "unseen",
                 timings={"backend_started": now - 1900, "execution_finished": now - 1800})
        self.job("j-old", 6, "done", "old", timings={"execution_finished": since - 60})  # Before the last digest.
        self.job("j-again", 7, "done", "again", timings={"execution_finished": now - 900})
        self.job("j-again2", 8, "running", "again", timings={"backend_started": now - 30})  # Runs again: 在做.
        self.job("j-late", 10, "done", "old", timings={"execution_finished": now - 10})  # After this digest's time.
        self.job("j-main", 9, "done", butler_topic_id(CHAT), timings={"execution_finished": now - 100})
        write_json(self.state / "topic-seen.json", {"seen": now - 3000})
        view = collect(self.state, CHAT, slot, since, now)
        self.assertEqual([(row["label"], row["state"], row["took"]) for row in view["waiting"]],
                         [("T401", "等你回答：用表格吗？", "等了 10分00秒"), ("T402", "等你开工", "等了 2分00秒")])
        self.assertEqual([(row["label"], row["state"], row["took"]) for row in view["running"]],
                         [("T407", "在做", "已运行 30秒"), ("T403", "跑测试", "已运行 5分00秒")])
        self.assertEqual([(row["label"], row["state"], row["took"], row["seen"]) for row in view["done"]],
                         [("T405", "失败", "用时 1分40秒", False), ("T404", "做完", "用时 1小时0分", True)])
        self.assertEqual(view["waiting"][0]["title"], "话题 401")


    def test_a_task_the_owner_stopped_reads_已停止_in_the_words_of_turn_words_and_never_做完(self):
        """Acceptance 2026-11-17: a stopped T3 read 做完 · 用时 40秒. A stopped task (its card's stop, or the lead's stop of a
        topic that waited for an answer: app/project_tools.py project_topic_stop) is ``cancelled`` and reads as turn_words says."""
        from covey.core import turn_words
        now = self.now
        for number, topic in enumerate(("stopped", "waited", "queued"), 421):
            self.topic(topic, number)
        ran = {"backend_started": now - 100, "execution_finished": now - 60}
        self.job("j-stopped", 1, "cancelled", "stopped", cancel_requested_by="card", timings=ran)
        self.job("j-waited", 2, "cancelled", "waited", status_message="负责人按主人的意思停了这个话题", timings=ran)
        self.job("j-queued", 3, "cancelled", "queued", timings={"received": now - 100})  # Never ran: not a result.
        view = collect(self.state, CHAT, now - 10, now - 3600, now)
        rows = {row["label"]: row for row in view["done"]}
        self.assertEqual(set(rows), {"T421", "T422"})
        for row in rows.values():
            self.assertEqual(row["state"], turn_words.STATES["cancelled"])
            self.assertEqual(row["state"], "已停止")

    def test_a_day_old_question_is_on_the_stale_line_not_its_own_row(self):
        now = self.now
        for number, topic in enumerate(("day", "over", "start"), 411):
            self.topic(topic, number)
        self.question("q-day", "day", now - pins.QUESTION_AGE)  # Exactly a day: still a row, as on the board.
        self.question("q-over", "over", now - pins.QUESTION_AGE - 1)  # Just over: the line.
        self.job("j-start", 1, "waiting_workspace", "start", timings={"received": now - 3 * 86400})  # Never stale.
        view = collect(self.state, CHAT, now, now - 3600, now)
        self.assertEqual([row["label"] for row in view["waiting"]], ["T411", "T413"])
        self.assertEqual([(row["topic"], row["label"]) for row in view["stale"]], [("over", "T412")])
        self.assertFalse(view["dismissable"])  # Board off: the line without its button.

    def test_the_topics_taken_off_the_board_are_in_no_part(self):
        now = self.now
        for number, topic in enumerate(("ask", "old", "run", "done", "kept"), 421):
            self.topic(topic, number)
        write_json(self.state / pins.RECORD, {"schema_version": 1, "since": now - 7 * 86400})
        self.question("q-ask", "ask", now - 600)
        self.question("q-old", "old", now - 2 * 86400)
        self.job("j-run", 1, "running", "run", timings={"backend_started": now - 300})
        self.job("j-done", 2, "done", "done", timings={"execution_finished": now - 900})
        self.question("q-kept", "kept", now - 3 * 86400)
        pins.dismiss(self.state, CHAT, ["ask", "old", "run", "done"], now - 60, "waiting")
        with mock.patch.dict(os.environ, ON):  # board=True: 手上的事 and the board are on (overview.dismissable).
            view = collect(self.state, CHAT, now, now - 3600, now, board=True)
            self.assertEqual((view["waiting"], view["running"], view["done"]), ([], [], []))
            self.assertEqual([row["topic"] for row in view["stale"]], ["kept"])
            self.assertTrue(view["dismissable"])
            # The same reading as the board's: a question asked after the topic was taken off brings it back.
            self.question("q-ask2", "ask", now - 30)
            self.assertEqual([row["label"] for row in collect(self.state, CHAT, now, now - 3600, now,
                                                              board=True)["waiting"]], ["T421"])
        # 手上的事 or the board off: nothing is taken off (手上的事 does not filter either).
        view = collect(self.state, CHAT, now, now - 3600, now)
        self.assertEqual(sorted(row["topic"] for row in view["stale"]), ["kept", "old"])
        self.assertEqual([row["label"] for row in view["running"]], ["T423"])


class FakeMessenger:
    def __init__(self):
        self.sends, self.ok = [], True

    def send_message(self, profile, chat, text, key, *, voice, card=None, on_card=None, **_):
        self.sends.append({"chat": chat, "key": key, "card": card, "text": text})
        if self.ok and on_card:
            on_card("om_digest")
        return self.ok


class TickTests(Records, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx = SimpleNamespace(state_dir=self.state, profile="test-bot")
        self.logs = []
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(),
                                       access=SimpleNamespace(owner_chat=lambda: CHAT, notify_chat=lambda: CHAT))
        self.messenger = FakeMessenger()
        self.feature = self.bridge()
        # The real time as the digest time's: board_read.read reads by when records were written.
        local = datetime.datetime.fromtimestamp(self.now, ZONE)
        self.slot = local.replace(second=0, microsecond=0).timestamp()
        self.times = [local.hour * 60 + local.minute]

    def bridge(self):
        feature = Digest(self.runtime, self.messenger, Voice("lark-bridge", "LARK_BRIDGE"))
        feature.link = lambda ctx, chat, text: "\n".join(f"[{label}](https://link/{label})" for label in text.split("\n"))
        return feature

    def record(self):
        return json.loads((self.state / RECORD).read_text())

    def test_nothing_to_list_sends_nothing_and_the_time_is_done(self):
        self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        self.assertEqual(self.messenger.sends, [])
        self.assertEqual(self.record()["last_slot"], self.slot)

    def test_sent_once_with_links_and_not_again_after_a_restart(self):
        self.topic("run", 403)
        self.job("j-run", 2, "running", "run", timings={"backend_started": self.now - 300})
        self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        self.assertEqual(len(self.messenger.sends), 1)
        card = self.messenger.sends[0]["card"]
        self.assertIn("[T403](https://link/T403) · 话题 403 · 在做", texts(card)[1])
        self.assertEqual(self.record()["message_id"], "om_digest")
        self.feature.tick(self.ctx, self.times, now=self.slot + 40)
        self.feature = self.bridge()
        self.feature.tick(self.ctx, self.times, now=self.slot + 80)
        self.assertEqual(len(self.messenger.sends), 1)

    def test_each_part_is_cut_and_says_how_many_more(self):
        for number in range(SHOWN + 3):
            self.topic(f"t{number}", 500 + number)
            self.job(f"j{number}", number + 1, "running", f"t{number}", timings={"backend_started": self.now - number})
        self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        lines = texts(self.messenger.sends[0]["card"])
        self.assertEqual(lines[0], f"**在做（{SHOWN + 3}）**")
        self.assertEqual(lines[SHOWN + 1], "<font color='grey'>另有 3 个</font>")

    def test_only_old_questions_is_sent_with_the_line(self):
        self.topic("old", 404)
        self.question("q-old", "old", self.now - 2 * 86400)
        with mock.patch.dict(os.environ, ON):
            self.feature.tick(self.ctx, self.times, now=self.slot + 5)
        card = self.messenger.sends[0]["card"]
        self.assertIn("另有 1 个等了一天以上：[T404](https://link/T404)", json.dumps(card, ensure_ascii=False))
        self.assertEqual(buttons(card), [{"kind": DISMISS, "topics": ["old"], "from": "digest"}])
        self.assertIn("另有 1 个等了一天以上", self.messenger.sends[0]["text"])
        saved = self.record()["sent"]["om_digest"]
        self.assertEqual(saved["slot"], self.slot)
        self.assertEqual([row["topic"] for row in saved["view"]["stale"]], ["old"])

    def test_a_send_that_fails_is_tried_again_with_the_same_key_then_given_up(self):
        self.topic("run", 403)
        self.job("j-run", 2, "running", "run", timings={"backend_started": self.now - 300})
        self.messenger.ok = False
        for second in (5, 30, 70, 140, 210, 280):
            self.feature.tick(self.ctx, self.times, now=self.slot + second)
        self.assertEqual(len(self.messenger.sends), digest.SEND_TRIES)
        self.assertEqual(len({send["key"] for send in self.messenger.sends}), 1)
        self.assertEqual(len({json.dumps(send["card"]) for send in self.messenger.sends}), 1)
        self.assertEqual(self.record()["last_slot"], self.slot)
        self.assertIsNone(self.record()["pending"])


class CardTests(unittest.TestCase):
    def test_empty_parts_are_left_out_and_seen_rows_are_grey(self):
        row = {"label": "T1", "title": "a", "state": "做完", "took": "用时 5秒", "seen": True}
        card = digest_card({"slot": at(8, 9, 30), "since": at(7, 23, 30), "waiting": [], "running": [],
                            "done": [row]})
        lines = texts(card)
        self.assertTrue(lines[0].startswith("**做完（1） · "))
        self.assertEqual(lines[1], "<font color='grey'>T1 · a · 做完 · 用时 5秒 · 已看</font>")


    def stale_view(self, **fields):
        return {"slot": at(8, 9, 30), "since": at(7, 23, 30), "waiting": [], "running": [], "done": [],
                "stale": [{"topic": "t1", "label": "T1", "title": "a"}, {"topic": "t2", "label": "T2", "title": "b"}],
                **fields}

    def test_the_stale_line_has_its_button_only_while_it_takes_effect(self):
        card = digest_card(self.stale_view(dismissable=True))
        self.assertEqual(texts(card)[0], "**等你（2）**")
        self.assertEqual(buttons(card), [{"kind": DISMISS, "topics": ["t1", "t2"], "from": "digest"}])
        card = digest_card(self.stale_view(dismissable=False))
        self.assertEqual(buttons(card), [])
        self.assertEqual(texts(card)[1], "<font color='grey'>另有 2 个等了一天以上：T1、T2</font>")

    def test_once_taken_off_the_line_is_grey_without_the_button(self):
        view = digest.cleared(self.stale_view(dismissable=True), {"t1", "t2"})
        card = digest_card(view)
        self.assertEqual(buttons(card), [])
        # The ones taken off count no more: the part and the header drop them (2026-11-09).
        self.assertEqual(texts(card)[0], "**等你（0）**")
        self.assertNotIn("等你", json.dumps(card["header"], ensure_ascii=False))
        self.assertIn("等你 2", json.dumps(digest_card(self.stale_view(dismissable=True))["header"], ensure_ascii=False))
        self.assertIn("<font color='grey'>已撤下 2 个：T1、T2</font>", texts(card))
        self.assertIn("已撤下 2 个：T1、T2", digest_text(view))
        self.assertIsNone(digest.cleared(view, {"t1"}))  # Nothing of theirs left on it.

    def test_a_view_saved_before_the_line_draws_as_before(self):
        row = {"label": "T1", "title": "a", "state": "在做", "took": "", "seen": False}
        card = digest_card({"slot": at(8, 9, 30), "since": at(7, 23, 30), "waiting": [], "running": [row],
                            "done": []})
        self.assertEqual(texts(card)[:2], ["**在做（1）**", "T1 · a · 在做"])


class SwitchTests(UnifiedCase):
    def test_off_unless_the_variable_lists_a_time(self):
        self.assertNotIn("digest", dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {ENV: "09:30,23:30"}):
            self.assertIn("digest", dict(self.app.worker_targets()))
        with mock.patch.dict(os.environ, {ENV: "nine"}):
            self.assertNotIn("digest", dict(self.app.worker_targets()))


class ClickTests(UnifiedCase):
    """这些都不用回答了 on the digest card: 手上的事's click, then the digest card redrawn from its saved view."""

    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(return_value=("card1", "om_index1"))
        messenger.put_card = mock.Mock(return_value={"code": 0})
        messenger.pin = mock.Mock(return_value=True)
        messenger.unpin = mock.Mock(return_value=True)
        messenger.pin_info = mock.Mock(return_value={"create_time": "1"})
        messenger.patch_card = mock.Mock(return_value=True)
        self.app.runtime.access.remember_owner_chat(CHAT)
        write_topic(self.state, {"topic_number": 404}, topic_id="old")
        write_private_text(self.state / "questions" / "q-old.json", json.dumps({  # Still pending: stays taken off.
            "token": "q-old", "topic_id": "old", "chat_id": CHAT, "job_id": 9, "status": "pending",
            "created_at": time.time() - 2 * 86400, "question_message_id": "om_q-old", "questions": []}))
        patch = mock.patch.dict(os.environ, ON)
        patch.start()
        self.addCleanup(patch.stop)
        self.view = {"slot": 1.0, "since": 0.0, "waiting": [], "running": [], "done": [], "dismissable": True,
                     "stale": [{"topic": "old", "label": "[T404](https://link/T404)", "title": "a"}]}
        self.value = buttons(digest_card(self.view))[0]

    def click(self, value, message="om_digest"):
        self.app.handle_card_action_event({"event_id": "c1", "operator_id": "owner", "chat_id": CHAT,
                                           "message_id": message, "action_value": value}, self.ctx)

    def test_the_topics_are_taken_off_and_both_cards_change(self):
        write_json(self.state / RECORD, {"sent": {"om_digest": {"slot": 1.0, "view": self.view}}})
        self.click(self.value)
        self.assertEqual(list(json.loads((self.state / pins.RECORD).read_text())["dismissed"]), ["old"])
        self.assertTrue(self.app.messenger.create_and_send_card.called)  # 手上的事 drawn after the click.
        message, card = self.app.messenger.patch_card.call_args.args[1:]
        self.assertEqual(message, "om_digest")
        self.assertEqual(buttons(card), [])
        self.assertIn("已撤下 1 个：[T404](https://link/T404)", json.dumps(card, ensure_ascii=False))
        saved = json.loads((self.state / RECORD).read_text())["sent"]["om_digest"]["view"]
        self.assertEqual((saved["stale"], saved["cleared"]), ([], ["[T404](https://link/T404)"]))

    def test_a_card_without_a_saved_view_is_left_as_it_is(self):
        write_json(self.state / RECORD, {"last_slot": 1.0, "message_id": "om_digest"})  # Sent before views were kept.
        self.click(self.value)
        self.assertEqual(list(json.loads((self.state / pins.RECORD).read_text())["dismissed"]), ["old"])
        self.app.messenger.patch_card.assert_not_called()
        self.assertTrue(any("digest card not redrawn" in line for line in self.logs), self.logs)

    def test_only_the_owner(self):
        write_json(self.state / RECORD, {"sent": {"om_digest": {"slot": 1.0, "view": self.view}}})
        self.assertEqual(self.app.overview.claim(self.ctx, {"message_id": "om_digest"}, self.value, False),
                         "owner_only")
        self.assertFalse((self.state / pins.RECORD).exists())
        self.app.messenger.patch_card.assert_not_called()


class BoardTests(UnifiedCase):
    """看板 (app/digest.py board): the bot menu's item with a time set, the same card as the timed one at any time."""

    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        self.enterContext(mock.patch.dict(os.environ, {ENV: "09:30,23:30", **ON}))
        self.puts = []
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            self.puts.append(("new", card, 0)), ("bcard", f"om_board{len(self.puts)}"))[1])
        messenger.put_card = mock.Mock(side_effect=lambda profile, card_id, card, **kw: (
            self.puts.append((card_id, card, kw["sequence"])), {"code": 0})[1])
        for name in ("pin", "unpin", "patch_card"):
            setattr(messenger, name, mock.Mock(return_value=True))
        messenger.pin_info = mock.Mock(return_value={"create_time": "1"})
        self.app.runtime.access.remember_owner_chat(CHAT)
        self.app.digest.link = lambda ctx, chat, text: text  # No T links to look up.
        menu = self.app.menu
        self.assertEqual(set(self.app.digest.menu_items()), {"digest_board"})
        menu.actions.update(self.app.digest.menu_items())  # As a Bridge launched with the times set (test_bot_menu).
        menu.folds.update(self.app.digest.menu_folds())
        self.now = time.time()
        self.last = self.now - 3 * 3600
        self.saved = {"schema_version": 1, "last_slot": self.last, "pending": None,
                      "sent": {"om_digest": {"slot": self.last, "view": {"slot": self.last}}}}
        write_json(self.state / RECORD, self.saved)

    def press(self, event_id):
        return self.app.handle_menu_event({"event_id": event_id, "event_key": "digest_board",
                                           "operator_open_id": "owner"}, self.ctx)

    def card(self):
        return self.puts[-1][1]

    def test_with_tidy_on_the_board_lists_the_projects_and_has_the_tidy_button(self):
        """The owner, 2026-11-18: 整理专项 only on the 看板 — not on the 设置 card, not on 早晚汇总."""
        from covey.cards import project_proposal
        from covey.cards.board import board_card
        from covey.core import project_proposals, projects
        record = projects.new_record(chat_id=CHAT, name="账号额度运行台", goal="额度看得准")
        projects.create(self.state, record)
        with mock.patch.dict(os.environ, {projects.ENV: "1", project_proposals.ENV: "suggest"}):
            view = self.app.digest.board_view(self.ctx, CHAT)
            card = json.dumps(board_card(view), ensure_ascii=False)
            timed = json.dumps(digest_card(collect(self.state, CHAT, self.now, self.last, self.now)), ensure_ascii=False)
        self.assertEqual([project["name"] for project in view["projects"]], ["账号额度运行台"])
        self.assertIn("专项", card)
        self.assertIn("账号额度运行台", card)
        self.assertIn("目标：额度看得准", card)
        self.assertIn(project_proposal.START_KIND, card)
        self.assertNotIn(project_proposal.START_KIND, timed)
        with mock.patch.dict(os.environ, {projects.ENV: "1", project_proposals.ENV: ""}):
            self.assertNotIn("tidy", self.app.digest.board_view(self.ctx, CHAT))

    def test_a_stopped_topic_has_its_own_group_with_why_and_its_not_my_concern_takes_it_off(self):
        write_topic(self.state, {"topic_number": 525}, topic_id="stop")
        write_topic(self.state, {"topic_number": 526}, topic_id="run")
        checkpoint = self.state / "checkpoints" / "job-7.json"
        write_json(checkpoint, {"phase": "等主人选插件形态", "steps": [{"title": "a", "state": "done"}]})
        upsert_job_record(self.state, "j-stop", job_id=7, status="cancelled", chat_id=CHAT, topic_id="stop",
                          card_message_id="om_stop", harness_id="claude", phase="等主人选插件形态",
                          timings={"backend_started": self.now - 900, "execution_finished": self.now - 600})
        upsert_job_record(self.state, "j-run", job_id=8, status="running", chat_id=CHAT, topic_id="run",
                          card_message_id="om_run", harness_id="claude", phase="梳理还剩哪些要收尾",
                          timings={"backend_started": self.now - 300})
        with mock.patch("time.time", return_value=self.now):
            self.press("ev1")
        text = json.dumps(self.card(), ensure_ascii=False)
        for part in ("停住了", "T525", "被停止了；停在：等主人选插件形态", "red-50", "不管了", "现在：梳理还剩哪些要收尾", "已跑"):
            self.assertIn(part, text)
        off, = [v for v in buttons(self.card()) if v.get("kind") == DISMISS]
        self.assertEqual(off["topics"], ["stop"])
        self.app.handle_card_action_event({"event_id": "c2", "operator_id": "owner", "chat_id": CHAT,
                                           "message_id": "om_board1", "action_value": off}, self.ctx)
        redrawn = json.dumps([c for cid, c, _ in self.puts if cid == "bcard"][-1], ensure_ascii=False)
        self.assertNotIn("T525", redrawn)
        self.assertIn("T526", redrawn)

    def test_off_without_a_time(self):
        with mock.patch.dict(os.environ, {ENV: ""}):
            self.assertEqual((self.app.digest.menu_items(), self.app.digest.menu_folds()), ({}, {}))

    def test_the_same_card_as_the_timed_one_and_digest_json_untouched(self):
        for number, topic in enumerate(("ask", "run", "done", "early", "old"), 501):
            write_topic(self.state, {"topic_number": number}, topic_id=topic)
        write_private_text(self.state / "questions" / "q1.json", json.dumps({
            "token": "q1", "topic_id": "ask", "chat_id": CHAT, "job_id": 1, "status": "pending",
            "created_at": self.now - 600, "question_message_id": "om_q1", "questions": [{"question": "要吗？"}]}))
        write_private_text(self.state / "questions" / "q2.json", json.dumps({
            "token": "q2", "topic_id": "old", "chat_id": CHAT, "job_id": 5, "status": "pending",
            "created_at": self.now - 2 * 86400, "question_message_id": "om_q2", "questions": []}))
        for key, job_id, status, topic, timings in (
                ("j-run", 2, "running", "run", {"backend_started": self.now - 300}),
                ("j-done", 3, "done", "done", {"backend_started": self.now - 1000, "execution_finished": self.now - 900}),
                ("j-early", 4, "done", "early", {"execution_finished": self.last - 60})):  # Before the last digest.
            upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id=CHAT, topic_id=topic,
                              card_message_id=f"om_{key}", harness_id="claude", timings=timings)
        before = (self.state / RECORD).read_bytes()
        with mock.patch("time.time", return_value=self.now):
            self.press("ev1")
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)
        card = self.card()
        # Its own card now (cards/board.py, the owner's 样张 A, 2026-11-18): coloured blocks by what they ask of him.
        self.assertEqual(card["header"]["title"]["content"], "看板")
        text = json.dumps(card, ensure_ascii=False)
        for part in ("T501", "T502", "T503", "这些都不用回答了", "等你", "在做", "做完的话题 1 个，都看过了", "orange-50", "blue-50"):
            self.assertIn(part, text)
        self.assertNotIn("T504", text)
        self.assertNotIn("我本来想找你的事", text)
        self.assertEqual((self.state / RECORD).read_bytes(), before)  # last_slot, pending and sent as they were.
        # 这些都不用回答了 on it: 手上的事's click, then this card redrawn in place from its record.
        dismiss, = [v for v in buttons(card) if v.get("kind") == DISMISS]
        self.app.handle_card_action_event({"event_id": "c1", "operator_id": "owner", "chat_id": CHAT,
                                           "message_id": "om_board1", "action_value": dismiss}, self.ctx)
        board = [(cid, c) for cid, c, _ in self.puts if cid == "bcard"]
        self.assertTrue(board)
        self.assertIn("已撤下 1 个", json.dumps(board[-1][1], ensure_ascii=False))
        self.app.messenger.patch_card.assert_not_called()
        self.assertEqual((self.state / RECORD).read_bytes(), before)

    def test_nothing_to_list_says_so_and_a_second_click_folds_and_unfolds(self):
        self.press("ev1")
        text = json.dumps(self.card(), ensure_ascii=False)
        self.assertIn("现在没有在等你、在做或停住的事", text)
        self.press("ev2")
        self.assertEqual((self.puts[-1][0], self.card()["header"]["title"]["content"]), ("bcard", "看板"))
        self.assertIn("再点菜单「看板」展开", json.dumps(self.card(), ensure_ascii=False))
        self.press("ev3")
        self.assertIn("现在没有在等你、在做或停住的事", json.dumps(self.card(), ensure_ascii=False))
        self.assertEqual([seq for _, _, seq in self.puts], [0, 1, 2])
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 1)
        # Once the owner wrote at the top level, the card is not the newest: a new one, the one before recalled.
        self.app.menu.owner_said(CHAT)
        self.press("ev4")
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, 2)
        self.app.messenger.recall.assert_called_with(self.ctx.profile, "om_board1")
        self.assertEqual(json.loads((self.state / RECORD).read_text()), self.saved)


if __name__ == "__main__":
    unittest.main()

"""The evening digest's own parts (app/digest.py ``daily``, the day's last digest time, the owner's private chat; T604): 本周找你
N 次 on it and on the 本周用量 card (app/weekly_usage.py ``reached``, #1: questions and stops, counted only), 今天代答 (``proxy_view``,
#45) and 你反复说的 (app/memory_candidates.py, #28, COVEY_MEMORY=1): the corrections the owner repeated in the day's ended topics,
at most three, a topic with redaction never read, each with 「记下」「不用」 and nothing taken without a click."""
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
from covey.app import digest, memory_candidates as candidates, weekly_usage
from covey.app.digest import RECORD, ZONE, Digest, last_of_day, proxy_view
from covey.app.track_standby import Failed
from covey.cards.digest import REMEMBER_KIND, digest_card, digest_text
from covey.cards.weekly_usage import reached_line, usage_card, usage_text
from covey.core import principles, triage
from covey.core import proxy as proxy_records
from covey.core.jobs import upsert_job_record
from covey.core.state import write_json, write_private_text
from covey.feishu.messenger import Voice

CHAT = "oc_dm"
OWNER = "ou_owner"


def at(day, hour, minute):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=ZONE).timestamp()


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


class State(unittest.TestCase):
    def setUp(self):
        self.state = state_dir(self)
        self.now = time.time()
        self.jobs = 0

    def topic(self, topic, number, *, workspace="covey", redaction=False, proxy=False):
        write_topic(self.state, {"topic_number": number, "workspace_name": workspace,
                                 **({"redaction": True} if redaction else {}), **({"proxy": {"key": "x"}} if proxy else {})},
                    topic_id=topic)

    def turn(self, topic, text, *, ago=None, status="done", sender=OWNER, message="om_m", **fields):
        """A turn of ``topic`` that ended ``ago`` seconds ago (by default each one after the one before)."""
        self.jobs += 1
        ago = 1000 - self.jobs if ago is None else ago
        upsert_job_record(self.state, f"job-{self.jobs}", job_id=self.jobs, status=status, chat_id=CHAT, topic_id=topic,
                          user_text=text, sender_id=sender, message_id=f"{message}{self.jobs}",
                          timings={"execution_finished": self.now - ago}, **fields)


def owner_turn(chat, sender):
    return sender == OWNER


class SentenceTests(State):
    def test_the_owners_corrections_of_the_days_ended_ordinary_topics(self):
        self.topic("a" * 16, 1)
        self.topic("b" * 16, 2)
        self.topic("c" * 16, 3, redaction=True)
        self.topic("d" * 16, 4, proxy=True)
        self.turn("a" * 16, "帮我看看日志。以后回答先给结论，别铺垫。")
        self.turn("b" * 16, "又是一大段铺垫，以后回答先给结论")
        self.turn("c" * 16, "以后别提客户的名字")  # Redaction on: never read.
        self.turn("d" * 16, "以后都这样回")  # A 代答 topic.
        self.turn("a" * 16, "以后别再问部署", sender="ou_member")  # Not the owner's own turn.
        self.turn("a" * 16, "以后都用中文", message="main-")  # Handed on by the butler: its words, not his.
        self.turn("b" * 16, "以后先跑测试", ago=2 * 86400)  # Not today's.
        self.turn("b" * 16, "以后先跑测试再说", status="running")  # Not ended.
        found = candidates.sentences(self.state, CHAT, self.now - 86400, self.now, owner_turn)
        self.assertEqual(found, [{"topic": "a" * 16, "text": "以后回答先给结论，别铺垫。"},
                                 {"topic": "b" * 16, "text": "又是一大段铺垫，以后回答先给结论"}])

    def test_the_rules_alone_group_what_reads_nearly_the_same_across_topics(self):
        rows = [{"topic": "a", "text": "以后回答先给结论"}, {"topic": "b", "text": "以后回答先给结论！"},
                {"topic": "a", "text": "以后回答先给出结论"}, {"topic": "c", "text": "别用表情"}]
        self.assertEqual(candidates.by_rules(rows), [("以后回答先给结论", [0, 1, 2])])
        self.assertEqual(candidates.by_rules(rows[:1] + rows[2:3]), [])  # One topic only: not repeated.

    def test_the_models_answer_is_checked(self):
        rows = [{"topic": "a", "text": "x"}, {"topic": "b", "text": "y"}, {"topic": "a", "text": "z"}]
        text = json.dumps({"repeated": [{"text": "回答先给结论", "indexes": [1, 2]},
                                        {"text": "只在一个话题", "indexes": [1, 3]},
                                        {"text": "越界", "indexes": [0, 9, True]}, {"text": "", "indexes": [1, 2]}, "x"]})
        self.assertEqual(candidates.parse("好的：" + text, rows), [("回答先给结论", [0, 1])])
        with self.assertRaises(Failed):
            candidates.parse("没有", rows)


class FindTests(State):
    def setUp(self):
        super().setUp()
        for number, topic in enumerate(("a" * 16, "b" * 16, "e" * 16), 1):
            self.topic(topic, number)
        self.turn("a" * 16, "以后回答先给结论。测试通过就合并")
        self.turn("b" * 16, "说过了，回答先给结论。又没跑全量就说通过")
        self.turn("e" * 16, "以后先跑全量再说通过")

    def find(self, groups=None, error=None):
        def ask(rows, existing, declined, backend, ctx, state_dir):
            if error:
                raise Failed(error)
            self.asked = (rows, existing, declined)
            return groups
        logs = []
        with mock.patch.object(candidates, "ask", ask):
            found = candidates.find(self.state, CHAT, self.now, owner_turn=owner_turn, log=logs.append)
        self.assertTrue(all("先给结论" not in line for line in logs), logs)  # The owner's words never in a log line.
        return found

    def test_groups_over_two_topics_with_their_workspace_and_labels(self):
        found = self.find([("回答先给结论", [0, 1]), ("跑完全量再说测试通过", [2, 3])])
        self.assertEqual(found, [{"text": "回答先给结论", "topics": ["a" * 16, "b" * 16], "labels": ["T1", "T2"],
                                  "workspace": "covey"},
                                 {"text": "跑完全量再说测试通过", "topics": ["b" * 16, "e" * 16], "labels": ["T2", "T3"],
                                  "workspace": "covey"}])
        rows, existing, declined = self.asked
        self.assertEqual([(row["label"], row["text"]) for row in rows], [
            ("T1", "以后回答先给结论。"), ("T2", "说过了，回答先给结论。"), ("T2", "又没跑全量就说通过"),
            ("T3", "以后先跑全量再说通过")])  # 测试通过就合并 has no correction's word.

    def test_what_a_principle_says_or_the_owner_declined_is_not_offered_and_three_at_most(self):
        principles.save(self.state, principles.OWNER, "回答先给结论。")
        write_json(candidates.path(self.state), {"declined": [principles.same_key("跑完全量再说测试通过")]})
        self.assertEqual(self.find([("回答先给结论", [0, 1]), ("跑完全量再说测试通过", [2, 3])]), [])
        many = [(f"规矩 {i}", [0, 1]) for i in range(5)]
        self.assertEqual(len(self.find(many)), candidates.SHOWN)

    def test_topics_in_two_workspaces_make_a_rule_of_all_of_them(self):
        self.topic("b" * 16, 2, workspace="docs")
        self.assertEqual(self.find([("回答先给结论", [0, 1])])[0]["workspace"], "")

    def test_without_the_model_the_rules_alone(self):
        self.turn("e" * 16, "以后回答先给结论")
        found = self.find(error="no_backend")
        self.assertEqual([item["text"] for item in found], ["以后回答先给结论"])



class LoneTopicTests(FindTests.__base__):
    def test_nothing_is_read_unless_two_topics_have_corrections(self):
        self.topic("a" * 16, 1)
        self.turn("a" * 16, "以后回答先给结论。以后别铺垫")
        asked = []
        with mock.patch.object(candidates, "ask", lambda *args: asked.append(args) or [("x", [0, 1])]):
            self.assertEqual(candidates.find(self.state, CHAT, self.now, owner_turn=owner_turn, log=lambda _: None), [])
        self.assertEqual(asked, [])


class RecordTests(State):
    def test_a_time_is_looked_at_once_and_a_click_decides_once(self):
        self.topic("a" * 16, 1)
        calls = []

        def finder():
            calls.append(1)
            return [{"text": "回答先给结论", "topics": ["a" * 16, "b" * 16], "labels": ["T1", "T2"], "workspace": "covey"}]
        rows = candidates.offer(self.state, CHAT, 100.0, finder)
        self.assertEqual(candidates.offer(self.state, CHAT, 100.0, finder), rows)
        self.assertEqual(len(calls), 1)
        self.assertEqual(principles.entries(self.state), [])  # Nothing without a click.
        row = candidates.decide(self.state, rows[0]["id"], "save")
        self.assertEqual(row["state"], candidates.SAVED)
        saved = principles.entries(self.state)
        self.assertEqual([(p["scope"], p["workspace"], p["text"]) for p in saved], [("workspace", "covey", "回答先给结论")])
        self.assertEqual(row["principle"], saved[0]["id"])
        self.assertEqual(candidates.decide(self.state, rows[0]["id"], "drop")["state"], candidates.SAVED)  # Decided already.
        self.assertEqual(len(principles.entries(self.state)), 1)

    def test_a_full_group_says_so_in_the_owners_words_and_stays_open(self):
        for i in range(principles.GROUP_LIMIT):
            principles.save(self.state, principles.OWNER, f"第 {i} 条")
        rows = candidates.offer(self.state, CHAT, 100.0, lambda: [{"text": "别用表情", "topics": [], "labels": [],
                                                                    "workspace": ""}])
        row = candidates.decide(self.state, rows[0]["id"], "save")
        self.assertEqual((row["state"], row["error"]), (candidates.OPEN, candidates.FULL))
        self.assertNotIn("principle_delete", row["error"])

    def test_a_stored_row_whose_time_cannot_be_read_goes_with_the_old_ones(self):
        write_json(candidates.path(self.state), {"rows": [{"id": "c00000001", "text": "x", "at": "bad", "slot": 5.0}]})
        rows = candidates.offer(self.state, CHAT, 100.0, lambda: [])
        self.assertEqual(rows, [])
        self.assertEqual(candidates.load(self.state)["rows"], [])

    def test_不用_sets_it_aside_for_good(self):
        rows = candidates.offer(self.state, CHAT, 100.0, lambda: [{"text": "别用表情", "topics": [], "labels": [],
                                                                    "workspace": ""}])
        self.assertEqual(candidates.decide(self.state, rows[0]["id"], "drop")["state"], candidates.DECLINED)
        self.assertEqual(candidates.load(self.state)["declined"], [principles.same_key("别用表情")])
        self.assertEqual(principles.entries(self.state), [])
        self.assertIsNone(candidates.decide(self.state, "c00000000", "save"))


class ReachedTests(State):
    def question(self, token, created, *, sent=True, **fields):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": "a" * 16, "chat_id": CHAT, "status": "answered", "created_at": created,
            **({"question_message_id": f"om_{token}"} if sent else {}), **fields}))

    def test_questions_that_reached_the_owner_and_tasks_that_stopped_this_week(self):
        self.question("q1", self.now - 3600)
        self.question("q2", self.now - 6 * 86400)
        self.question("q3", self.now - 8 * 86400)  # Before the week.
        self.question("q4", self.now - 3600, sent=False)  # Its card never went out.
        self.question("q5", self.now - 3600, triage={"state": triage.ANSWERED})  # The lead answered it in his place.
        for status in ("failed", "interrupted", "done", "cancelled"):  # The owner stopped the cancelled one: 已停止.
            upsert_job_record(self.state, f"j-{status}", job_id=len(status), status=status, chat_id=CHAT, topic_id="t",
                              harness_id="claude", timings={"backend_requested": 1.0}, session_id="s",
                              **({"cancel_requested_by": "card"} if status == "cancelled" else {}))
        later = time.time() + 60  # The jobs' records were written after self.now.
        self.assertEqual(weekly_usage.reached(self.state, later), {"questions": 2, "stopped": 2})
        self.assertEqual(reached_line({"questions": 2, "stopped": 2}), "本周找你 4 次：问题 2 次、停住 2 次")

    def test_the_usage_card_has_the_line(self):
        summary = weekly_usage.summarize([{"harness_id": "claude", "status": "failed", "created_at": "2026-11-20T00:00:00Z",
                                           "timings": {"backend_requested": 1.0}, "session_id": "s", "output_tokens": 1}],
                                         datetime.datetime(2026, 11, 21, tzinfo=ZONE).timestamp(), {"claude": "Claude"}, 3)
        self.assertEqual(summary["reached"], {"questions": 3, "stopped": 1})
        self.assertIn("本周找你 4 次：问题 3 次、停住 1 次", json.dumps(usage_card(summary), ensure_ascii=False))
        self.assertIn("本周找你 4 次", usage_text(summary))
        older = {key: value for key, value in summary.items() if key != "reached"}  # A summary of before: no line.
        self.assertNotIn("本周找你", json.dumps(usage_card(older), ensure_ascii=False))


class ProxyCountTests(State):
    def batch(self, batch_id, status, moves):
        proxy_records.write_batch(self.state, {"id": batch_id, "contact": "k", "status": status, "messages": [],
                                               "history": moves})

    def test_todays_replies_sent_and_not_sent(self):
        now = at(21, 21, 30)
        start = at(21, 0, 0)
        self.batch("b1", "sent", [["received", start + 60], ["sending", start + 70], ["sent", start + 80]])
        self.batch("b2", "sent", [["sent", start - 60]])  # Yesterday's.
        self.batch("b3", "held", [["received", start + 100], ["held", start + 200, "等你"]])
        self.batch("b4", "uncertain", [["uncertain", start - 10]])  # Left so yesterday.
        with mock.patch("covey.app.proxy_send.available", return_value=True):
            self.assertEqual(proxy_view(self.state, now), {"sent": 1, "unsent": 1})
            for path in (proxy_records.proxy_dir(self.state) / "batches").glob("*.json"):
                path.unlink()
            proxy_records._changed(proxy_records.proxy_dir(self.state) / "batches" / "x.json")
            self.assertEqual(proxy_view(self.state, now), {})  # 代答 off and nothing of today's.
            proxy_records.write_settings(self.state, {"enabled": True})
            self.assertEqual(proxy_view(self.state, now), {"sent": 0, "unsent": 0})
        self.assertEqual(proxy_view(self.state, now), {})  # No sender: no 代答.


class FakeMessenger:
    def __init__(self):
        self.sends = []

    def send_message(self, profile, chat, text, key, *, voice, card=None, on_card=None, **_):
        self.sends.append({"card": card, "text": text})
        if on_card:
            on_card("om_digest")
        return True


class EveningTests(State):
    def setUp(self):
        super().setUp()
        self.ctx = SimpleNamespace(state_dir=self.state, profile="test-bot")
        self.logs = []
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(),
                                       access=SimpleNamespace(owner_chat=lambda: CHAT, notify_chat=lambda: CHAT,
                                                              owner_turn=owner_turn))
        self.messenger = FakeMessenger()
        self.feature = Digest(self.runtime, self.messenger, Voice("lark-bridge", "LARK_BRIDGE"))
        self.feature.link = lambda ctx, chat, text, cards=None: text
        self.offered = []
        self.feature.candidates = SimpleNamespace(offer=lambda ctx, chat, slot: self.offered)
        local = datetime.datetime.fromtimestamp(self.now, ZONE)
        self.slot = local.replace(second=0, microsecond=0).timestamp()
        minute = local.hour * 60 + local.minute
        self.evening, self.morning = [minute], [minute, (minute + 1) % 1440 if minute < 1439 else minute]

    def test_the_last_time_of_the_day_is_the_evening(self):
        self.assertTrue(last_of_day([570, 1290], at(21, 21, 30)))
        self.assertFalse(last_of_day([570, 1290], at(21, 9, 30)))
        self.assertTrue(last_of_day([570], at(21, 9, 30)))

    def test_an_open_candidate_is_sent_alone_with_its_buttons_and_the_weeks_count(self):
        self.offered = [{"id": "c0a1b2c3d", "text": "回答先给结论", "labels": ["T1", "T2"], "workspace": "covey",
                         "state": candidates.OPEN}]
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "1"}):
            self.feature.tick(self.ctx, self.evening, now=self.slot + 5)
        card = self.messenger.sends[0]["card"]
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("你反复说的（1）", text)
        self.assertIn("来源：T1、T2 · 记到工作区 covey", text)
        self.assertIn("本周找你 0 次：问题 0 次、停住 0 次", text)
        self.assertEqual([b for b in buttons(card) if b["kind"] == REMEMBER_KIND],
                         [{"kind": REMEMBER_KIND, "id": "c0a1b2c3d", "op": "save"},
                          {"kind": REMEMBER_KIND, "id": "c0a1b2c3d", "op": "drop"}])
        self.assertIn("回答先给结论（来源：T1、T2 · 记到工作区 covey）", self.messenger.sends[0]["text"])
        saved = json.loads((self.state / RECORD).read_text())["sent"]["om_digest"]["view"]
        self.assertEqual(saved["remember"][0]["id"], "c0a1b2c3d")

    def test_memory_off_or_a_morning_digest_has_none_of_them(self):
        self.offered = [{"id": "c0a1b2c3d", "text": "回答先给结论", "labels": [], "workspace": "", "state": "open"}]
        self.feature.tick(self.ctx, self.evening, now=self.slot + 5)  # COVEY_MEMORY unset: nothing to list, nothing sent.
        self.assertEqual(self.messenger.sends, [])
        self.topic("r" * 16, 9)
        upsert_job_record(self.state, "j-run", job_id=99, status="running", chat_id=CHAT, topic_id="r" * 16,
                          timings={"backend_started": self.now - 60})
        if self.morning[1] != self.morning[0]:
            write_json(self.state / RECORD, {})
            with mock.patch.dict(os.environ, {"COVEY_MEMORY": "1"}):
                self.feature.tick(self.ctx, self.morning, now=self.slot + 5)
            text = json.dumps(self.messenger.sends[-1]["card"], ensure_ascii=False)
            self.assertNotIn("你反复说的", text)
            self.assertNotIn("本周找你", text)

    def test_candidates_that_cannot_be_read_leave_the_digest_as_it_is(self):
        def broken(ctx, chat, slot):
            raise OSError("disk")
        self.feature.candidates = SimpleNamespace(offer=broken)
        self.topic("r" * 16, 9)
        upsert_job_record(self.state, "j-run", job_id=99, status="running", chat_id=CHAT, topic_id="r" * 16,
                          timings={"backend_started": self.now - 60})
        with mock.patch.dict(os.environ, {"COVEY_MEMORY": "1"}):
            self.feature.tick(self.ctx, self.evening, now=self.slot + 5)
        self.assertEqual(len(self.messenger.sends), 1)
        self.assertNotIn("你反复说的", json.dumps(self.messenger.sends[0]["card"], ensure_ascii=False))
        self.assertIn("digest candidates unreadable: OSError", self.logs)

    def test_counts_never_make_a_card_be_sent(self):
        self.feature.tick(self.ctx, self.evening, now=self.slot + 5)
        self.assertEqual(self.messenger.sends, [])
        self.assertIn("digest not sent: nothing to list", self.logs)


class CardTests(unittest.TestCase):
    def test_a_decided_candidate_says_what_came_of_it_and_has_no_button(self):
        view = {"slot": 1.0, "since": 0.0, "waiting": [], "stale": [], "running": [], "done": [],
                "remember": [{"id": "c1", "text": "回答先给结论", "labels": ["T1"], "workspace": "", "state": "saved"},
                             {"id": "c2", "text": "别用表情", "labels": [], "workspace": "covey", "state": "declined"},
                             {"id": "c3", "text": "周报用表格", "labels": ["T3"], "workspace": "", "state": "open",
                              "error": "通用的原则已有 30 条"}],
                "reached": {"questions": 1, "stopped": 2}, "proxy": {"sent": 5, "unsent": 1}}
        card = digest_card(view)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("记到全局 · 已记下", text)
        self.assertIn("记到工作区 covey · 不用了，以后不再提", text)
        self.assertIn("⚠️ 没记下：通用的原则已有 30 条", text)
        self.assertEqual({b["id"] for b in buttons(card) if b["kind"] == REMEMBER_KIND}, {"c3"})
        self.assertIn("本周找你 3 次：问题 1 次、停住 2 次\n今天代答发出 5 条，1 条没发出，在菜单「代答」里看",
                      [element.get("content") for element in card["body"]["elements"]])
        self.assertIn("今天代答发出 5 条", digest_text(view))
        # A view of before has none of these parts.
        old = {key: value for key, value in view.items() if key not in ("remember", "reached", "proxy")}
        self.assertNotIn("你反复说的", json.dumps(digest_card(old), ensure_ascii=False))


class ClickTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.state = self.ctx.state_dir
        self.app.messenger.patch_card = mock.Mock(return_value=True)
        self.rows = candidates.offer(self.state, CHAT, 1.0, lambda: [
            {"text": "回答先给结论", "topics": ["a" * 16], "labels": ["T1"], "workspace": ""}])
        view = {"slot": 1.0, "since": 0.0, "waiting": [], "stale": [], "running": [], "done": [],
                "remember": [digest.remember_row(row) for row in self.rows]}
        write_json(self.state / RECORD, {"sent": {"om_digest": {"slot": 1.0, "view": view}}})
        self.value = next(b for b in buttons(digest_card(view)) if b.get("op") == "save")

    def click(self, value, operator="owner"):
        return self.app._handle_card_action_event({"event_id": "c1", "operator_id": operator, "chat_id": CHAT,
                                                   "message_id": "om_digest", "action_value": value}, self.ctx)

    def test_记下_writes_the_principle_and_redraws_the_card(self):
        self.assertEqual(self.click(self.value), "digest_remember")
        self.assertEqual([p["text"] for p in principles.entries(self.state)], ["回答先给结论"])
        message, card = self.app.messenger.patch_card.call_args.args[1:]
        self.assertEqual(message, "om_digest")
        self.assertIn("记到全局 · 已记下", json.dumps(card, ensure_ascii=False))
        self.assertEqual([b for b in buttons(card) if b["kind"] == REMEMBER_KIND], [])
        saved = json.loads((self.state / RECORD).read_text())["sent"]["om_digest"]["view"]
        self.assertEqual(saved["remember"][0]["state"], candidates.SAVED)

    def test_only_the_owner_and_a_bad_value_is_ignored(self):
        self.assertEqual(self.app.digest.claim(self.ctx, {"message_id": "om_digest"}, self.value, False), "owner_only")
        self.assertEqual(principles.entries(self.state), [])
        self.assertEqual(self.click({**self.value, "id": "nope"}), "digest_remember_ignored")
        self.app.messenger.patch_card.assert_not_called()

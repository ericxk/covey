"""网页工作台同步 (app/web_sync.py, app/web_ops.py, core/web_sync.py, COVEY_WEB=1): against a fake /openapi of the page (core/serving.py's
loopback server): the items pushed are right and only the changed ones go again, batches are cut, the page's say / answer / stop / reopen are
each done once (an op_id seen again is only acknowledged), a failing page is backed off and does not stop the thread, the key is in no log line, and
with the switch off there is no thread and no request. The butler's conversation is one topic of ``kind`` "butler" and a say to it is the owner's message at the top
level of the private chat (the id ``web-<op_id>`` is quoted, replied to and reacted on by nothing); ``seen`` marks a topic's result as read."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import write_job, write_topic

from covey.app import answers as answer_words, source_watch, web_ops, web_said, web_sync as app_web
from covey.app.web_sync import WebError, WebSync
from covey.app import project_lead, web_turns
from covey.core import butler_opened, http_upstream, model_names, placement, project_library, project_memory, projects, triage, web_sync
from covey.core.butler import butler_topic_id
from covey.core.history_storage import append_history
from covey.core.serving import LoopbackServer
from covey.core.jobs import job_file
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.topic_seen import read_seen
from covey.core.topics import context_for_topic, message_topic, remember_topic_message
from support.fake_backends import codex_context
from test_butler import ButlerUnifiedCase

from test_project_lead import CHAT, LEAD, PID, Case

@dataclasses.dataclass
class Ctx:
    topic_id: str = ""
    state_dir: object = None


T0 = time.time()
KEY = "fake-key-do-not-log-0123456789"
TOPIC = "c" * 32
OTHER = "d" * 32
BUTLER = butler_topic_id(CHAT)
TOKEN = "tok123456789"


class Web:
    """The page's /openapi: what it was sent, and the outbox rows it hands out."""

    def __init__(self, test):
        self.upserts, self.acks, self.outbox, self.seen, self.fail = [], [], [], [], 0
        self.server = LoopbackServer(self.handle, "web-fake").__enter__()
        test.addCleanup(self.server.__exit__)

    def handle(self, handler, body):
        self.seen.append((handler.command, handler.path, handler.headers.get("Authorization")))
        status, answer = 200, {"ok": True}
        if self.fail:
            status, answer = self.fail, {"ok": False}
        elif handler.headers.get("Authorization") != "Bearer " + KEY:
            status, answer = 401, {"ok": False}
        elif handler.path.startswith("/sync/upsert"):
            self.upserts.append(json.loads(body))
        elif handler.path.startswith("/sync/inbox"):
            after = int(handler.path.split("after=")[1].split("&")[0])
            answer = {"items": [row for row in self.outbox if row["seq"] > after and row.get("state", "new") == "new"]}
        elif handler.path.startswith("/sync/ack"):
            for result in json.loads(body)["results"]:
                self.acks.append(result)
                for row in self.outbox:
                    if row["op_id"] == result["op_id"]:
                        row["state"] = result["state"]
        payload = json.dumps(answer).encode()
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def op(self, kind, seq, op_id, topic="", project="", **payload):
        self.outbox.append({"seq": seq, "op_id": op_id, "kind": kind, "topic_key": topic, "project_key": project, "payload": payload})

    def items(self, kind=None):
        found = [item for batch in self.upserts for item in batch["items"]]
        return [item for item in found if kind in (None, item["type"])]


class FakeApp:
    """A Harness app that records what it is asked to do."""

    def __init__(self, case):
        self.case = case
        self.queued, self.steered, self.answers, self.stopped, self.painted = [], [], [], [], []
        self.harness = SimpleNamespace(context=object())
        self.submission = SimpleNamespace(event_context=self.event_context, enqueue_chat_job=self.enqueue)
        self.steering = SimpleNamespace(steer_stage=lambda job: "running", deliver_steer=self.deliver)
        self.questions = SimpleNamespace(submit_answer=lambda ctx, chat, token, fields, event: self.answers.append((ctx.topic_id, token, fields, event)),
                                         paint_question_state=lambda ctx, record: self.painted.append((record["token"], record["status"])))
        self.controls = SimpleNamespace(cancel_reply_job_from_card=lambda ctx, action, chat, message, others=0: self.stopped.append(action))

    def event_context(self, base, event):
        topic = message_topic(self.case.state, CHAT, event["message_id"])
        return SimpleNamespace(topic_id=topic, history_path=self.case.state / "topics" / topic / "history.jsonl")

    def enqueue(self, ctx, content, chat, message, **fields):
        self.queued.append((ctx.topic_id, message, content))
        return SimpleNamespace(job_id=9)

    def deliver(self, ctx, job, text, message, **fields):
        self.steered.append((job.job_id, message, text))


class WebCase(Case):
    def setUp(self):
        super().setUp()
        self.web = Web(self)
        key = self.state / "key.txt"
        key.write_text(KEY + "\n")
        config = self.state / "web.json"
        write_json(config, {"base_url": self.web.server.base_url, "key_file": str(key)})
        patch = mock.patch.dict(os.environ, {web_sync.ENV: "1", web_sync.CONFIG_ENV: str(config)})
        patch.start()
        self.addCleanup(patch.stop)
        self.jobs = {}
        self.app = FakeApp(self)
        self.runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(), owner_open_id="ou_owner",
                                       access=SimpleNamespace(owner_chat=lambda: CHAT),
                                       jobs=SimpleNamespace(topic_jobs=lambda topic, chat: self.jobs.get(topic, [])))
        self.env = SimpleNamespace(state_dir=self.state, runtime=self.runtime, topics=SimpleNamespace(target=self.target))
        self.sync = WebSync(self.runtime, self.env)
        self.sync.client.opener = http_upstream.opener(direct=True)

    def target(self, state, topic, chat):
        if topic != TOPIC or chat != CHAT:
            return None
        return self.app, Ctx(topic, state), {}

    def topic(self, topic=TOPIC, number=7, title="整理账单", status="done", lines=(("user", "你好"), ("assistant", "好的"))):
        write_topic(self.state, topic_id=topic, chat_id=CHAT, harness_id="claude", topic_number=number, origin={"title": title},
                    workspace_name="ws1")
        write_job(self.state, f"k-{topic[:4]}", topic_id=topic, chat_id=CHAT, job_id=number, status=status, model="opus", card_message_id="om_card")
        for role, text in lines:
            append_history(self.state / "topics" / topic / "history.jsonl", role, text, {"message_id": "m" + text})

    def butler_topic(self, status="done", lines=(("user", "今天先做什么"), ("assistant", "先整理账单，开了 T7")), job_id=3):
        write_topic(self.state, topic_id=BUTLER, chat_id=CHAT, harness_id="claude", main_thread=True, workspace_name="ws1",
                    workdir=str(self.state))
        write_job(self.state, "k-butler", topic_id=BUTLER, chat_id=CHAT, job_id=job_id, status=status, model="opus", message_id="om_b1")
        for role, text in lines:
            append_history(self.state / "topics" / BUTLER / "history.jsonl", role, text, {"message_id": "m" + text})

    def hearing(self, why=""):
        """The butler's door is a recorder: the events it was given."""
        self.heard = []
        self.sync.ops.hear = lambda event: self.heard.append(event) or why

    def question(self, token=TOKEN, **fields):
        row = {"token": token, "chat_id": CHAT, "topic_id": TOPIC, "status": "pending", "created_at": T0, "question_message_id": "om_q",
               "questions": [{"id": "q1", "header": "选哪个", "question": "用哪个库？", "options": [{"label": "A", "description": "老库"},
                                                                                             {"label": "B", "description": ""}]}], **fields}
        write_json(self.state / "questions" / f"{token}.json", row)


class ConfigTest(WebCase):
    def test_the_key_is_read_from_its_file_and_is_not_in_the_repr(self):
        config = web_sync.load_config()
        self.assertEqual(config.key, KEY)
        self.assertNotIn(KEY, repr(config))
        self.assertEqual((config.push_seconds, config.poll_seconds), (1.0, 1.0))

    def test_a_missing_key_file_is_said_without_the_key(self):
        write_json(self.state / "bad.json", {"base_url": "https://x.example/openapi", "key_file": str(self.state / "none")})
        with self.assertRaises(ValueError) as caught:
            web_sync.load_config(self.state / "bad.json")
        self.assertNotIn(KEY, str(caught.exception))

    def test_batches_are_cut_by_count_and_by_size(self):
        items = [web_sync.item("turn", str(n), {"body": "x"}) for n in range(1201)]
        self.assertEqual([len(batch) for batch in web_sync.batches(items)], [500, 500, 201])
        big = [web_sync.item("turn", str(n), {"body": "y" * 400_000}) for n in range(5)]
        self.assertEqual([len(batch) for batch in web_sync.batches(big)], [2, 2, 1])

    def test_a_batch_of_chinese_text_is_cut_by_its_bytes_as_sent(self):
        """The page refuses a request over 1 MiB; 300 000 Chinese characters are 900 000 bytes in UTF-8."""
        wide = [web_sync.item("turn", str(n), {"body": "汉" * 300_000}) for n in range(3)]
        cut = web_sync.batches(wide)
        self.assertEqual([len(batch) for batch in cut], [1, 1, 1])
        for batch in cut:
            self.assertLessEqual(len(json.dumps({"items": batch, "deleted": []}, ensure_ascii=False).encode()), 1_048_576)

    def test_a_long_body_is_cut_and_says_so(self):
        body = web_sync.clip("好" * 40000)
        self.assertLessEqual(len(body.encode()), web_sync.BODY_BYTES)
        self.assertIn("截断", body)


class PushTest(WebCase):
    def test_the_four_kinds_of_items_are_pushed(self):
        self.topic()
        self.question()
        self.thread(OTHER, "对账", 8, status="running")
        self.question("tok-other-0001", topic_id=OTHER)
        count = self.sync.push_once(T0)
        self.assertGreater(count, 4)
        by = {(item["type"], item["key"]): item["data"] for item in self.web.items()}
        topic = by[("topic", TOPIC)]
        self.assertEqual((topic["number"], topic["title"], topic["state"], topic["workspace"], topic["harness"], topic["model"]),
                         (7, "整理账单", "等你", "ws1", "claude", "opus"))
        self.assertEqual([(by[("turn", f"{TOPIC}:{n}")]["role"], by[("turn", f"{TOPIC}:{n}")]["body"]) for n in (1, 2)],
                         [("owner", "你好"), ("agent", "好的")])
        asked = by[("question", TOKEN)]
        self.assertEqual((asked["state"], asked["topic_key"], asked["questions"][0]["options"][0]), (
            "open", TOPIC, {"label": "A", "description": "老库"}))
        self.assertEqual(by[("project", PID)]["title"], "迁移账单")
        self.assertEqual(by[("project", PID)]["topic_keys"], [OTHER])
        self.assertEqual(by[("topic", OTHER)]["project_key"], PID)
        self.assertEqual(by[("topic", OTHER)]["state"], "等你")
        self.assertEqual(by[("project", PID)]["status"], "等你")
        for item in self.web.items():
            self.assertEqual(item["rev"], web_sync.rev(item["data"]))

    def test_a_finished_topic_is_unseen_until_read_and_projects_follow_the_state(self):
        self.topic(lines=())
        self.sync.push_once(T0)
        self.assertTrue(next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]["unseen"])
        self.assertEqual(next(item for item in self.web.items("project"))["data"]["status"], "在做")
        projects.update(self.state, PID, lambda record: record.update(state="paused", state_reason="idle"))
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.items("project")[-1]["data"]["status"], "收起")

    def test_only_what_changed_is_pushed_again(self):
        self.topic()
        self.sync.push_once(T0)
        first = len(self.web.items())
        self.assertEqual(self.sync.push_once(T0 + 1), 0)
        self.assertEqual(len(self.web.items()), first)
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "user", "再来一句")
        self.sync.collector.touch(TOPIC)
        self.assertEqual(self.sync.push_once(T0 + 2), 2)  # The new turn, and its topic: its last_seq moved (data contract v12).
        self.assertEqual((self.web.items()[-1]["key"], self.web.items()[-1]["data"]["body"]), (f"{TOPIC}:3", "再来一句"))

    def test_a_running_turn_pushes_its_progress_notes_each_time_they_change(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        notes = {"steps": {"a": {"title": "进展说明", "detail": "先看了日志", "status": "note"}}, "step_order": ["a"]}
        with mock.patch.object(app_web, "load_trace", side_effect=lambda *args: notes):
            self.sync.push_once(T0)
            self.assertEqual(self.sync.push_once(T0 + 1), 0)
            notes["steps"]["b"] = {"title": "进展说明", "detail": "再看了配置", "status": "note"}
            notes["step_order"].append("b")
            self.assertEqual(self.sync.push_once(T0 + 2), 1)
        live = self.web.items("turn")[-1]
        self.assertEqual((live["key"], live["data"]["status"]), (f"{TOPIC}:2", "running"))
        self.assertEqual(live["data"]["body"], "先看了日志\n\n再看了配置")

    def test_a_topic_that_left_the_chat_is_deleted_on_a_full_look(self):
        self.topic()
        self.sync.push_once(T0)
        self.sync.collector.pushed_topics.add(("topic", OTHER))
        self.sync.push_once(T0 + 100)
        self.assertIn({"type": "topic", "key": OTHER}, [row for batch in self.web.upserts for row in batch["deleted"]])

    def test_the_key_is_sent_and_never_logged(self):
        self.topic()
        self.sync.push_once(T0)
        self.assertTrue(all(auth == "Bearer " + KEY for _, _, auth in self.web.seen))
        self.web.fail = 500
        with self.assertRaises(WebError):
            self.sync.inbox_once()
        self.runtime.shutdown.wait = lambda seconds: self.runtime.shutdown.set() or True
        self.sync.loop("inbox", self.sync.inbox_once, lambda: 1)
        self.assertTrue(self.logs)
        self.assertNotIn(KEY, "\n".join(self.logs))

    def test_the_butler_conversation_is_one_topic_of_kind_butler_and_topics_say_topic(self):
        self.topic()
        self.butler_topic()
        self.sync.push_once(T0)
        by = {(item["type"], item["key"]): item["data"] for item in self.web.items()}
        mine = by[("topic", BUTLER)]
        self.assertEqual((mine["kind"], mine["number"], mine["title"], mine["project_key"], mine["state"], mine["unseen"]),
                         ("butler", 0, "管家", "", "做完", False))
        self.assertEqual([(by[("turn", f"{BUTLER}:{n}")]["role"], by[("turn", f"{BUTLER}:{n}")]["body"]) for n in (1, 2)],
                         [("owner", "今天先做什么"), ("agent", "先整理账单，开了 T7")])
        self.assertEqual(by[("topic", TOPIC)]["kind"], "topic")
        self.assertEqual([item["key"] for item in self.web.items("topic")].count(BUTLER), 1)
        for item in self.web.items():
            self.assertEqual(item["rev"], web_sync.rev(item["data"]))
        self.assertEqual(self.sync.push_once(T0 + 1), 0)  # Nothing changed, nothing goes again.

    def test_a_butler_with_no_history_and_no_task_is_not_pushed(self):
        write_topic(self.state, topic_id=BUTLER, chat_id=CHAT, harness_id="claude", main_thread=True)
        self.sync.push_once(T0)
        self.assertEqual(self.web.items("topic"), [])
        self.butler_topic()
        self.sync.push_once(T0 + 100)
        self.assertEqual([item["key"] for item in self.web.items("topic")], [BUTLER])

    def test_the_butlers_running_turn_is_looked_at_every_second_with_no_touch(self):
        self.butler_topic(status="running", lines=(("user", "做一下"),))
        notes = {"steps": {"a": {"title": "进展说明", "detail": "先看了日志", "status": "note"}}, "step_order": ["a"]}
        with mock.patch.object(app_web, "load_trace", side_effect=lambda *args: notes):
            self.sync.push_once(T0)
            self.assertEqual(self.sync.push_once(T0 + 1), 0)
            notes["steps"]["b"] = {"title": "进展说明", "detail": "再看了配置", "status": "note"}
            notes["step_order"].append("b")
            self.assertEqual(self.sync.push_once(T0 + 2), 1)
        live = self.web.items("turn")[-1]
        self.assertEqual((live["key"], live["data"]["status"], live["data"]["body"]), (f"{BUTLER}:2", "running", "先看了日志\n\n再看了配置"))
        self.assertEqual([item["data"]["state"] for item in self.web.items("topic")][-1], "在做")

    def test_a_quiet_butler_is_looked_at_again_only_after_a_web_op_touched_it_or_in_a_full_look(self):
        self.butler_topic()
        self.sync.push_once(T0)
        append_history(self.state / "topics" / BUTLER / "history.jsonl", "user", "再来一句")
        self.assertEqual(self.sync.push_once(T0 + 1), 0)
        self.sync.collector.touch(BUTLER)
        self.assertEqual(self.sync.push_once(T0 + 2), 2)  # The new turn, and the butler's topic: its last_seq moved (contract v12).
        self.assertEqual((self.web.items()[-1]["key"], self.web.items()[-1]["data"]["body"]), (f"{BUTLER}:3", "再来一句"))

    def test_the_butler_topic_and_other_chats_are_not_pushed(self):
        self.topic()
        write_topic(self.state, topic_id=OTHER, chat_id="oc_other", harness_id="claude", topic_number=9)
        write_job(self.state, "k-other", topic_id=OTHER, chat_id="oc_other", job_id=9, status="done")
        self.sync.push_once(T0)
        self.assertNotIn(OTHER, [item["key"] for item in self.web.items("topic")])


    def test_a_quiet_full_look_still_posts_an_empty_batch_so_the_page_sees_it_alive(self):
        self.topic()
        self.sync.push_once(T0)
        sent = len(self.web.upserts)
        self.assertEqual(self.sync.push_once(T0 + 1), 0)
        self.assertEqual(len(self.web.upserts), sent)
        self.assertEqual(self.sync.push_once(T0 + 61), 0)
        self.assertEqual(self.web.upserts[sent:], [{"items": [], "deleted": []}])

    def test_starting_over_pushes_everything_again_and_the_old_look_keeps_to_its_own_records(self):
        self.topic()
        first = self.sync.push_once(T0)
        old = self.sync.sent
        self.sync.start_over()
        self.assertEqual(self.sync.push_once(T0 + 1), first)
        self.assertIsNot(self.sync.sent, old)
        self.assertEqual(len(old), first)


class StuckTest(WebCase):
    """A step stuck in the file system (an NFS open that never returns holds its thread) is left behind; the next runs in a new thread."""

    def test_a_stuck_step_is_left_behind_and_the_next_runs_in_a_new_thread(self):
        release, threads, resets = threading.Event(), [], []

        def step():
            threads.append(threading.current_thread())
            if len(threads) == 1:
                release.wait(10)
            elif len(threads) >= 3:
                self.runtime.shutdown.set()
        with mock.patch.object(app_web, "STUCK_SECONDS", 0.2):
            self.sync.loop("push", step, lambda: 0.01, lambda: resets.append(1))
        release.set()
        threads[0].join(5)
        threads[1].join(5)
        self.assertEqual(len([line for line in self.logs if "web sync push stuck" in line]), 1)
        self.assertEqual(resets, [1])
        self.assertIsNot(threads[0], threads[1])
        self.assertIs(threads[1], threads[2])
        self.assertFalse(threads[0].is_alive())
        self.assertFalse(threads[1].is_alive())

    def test_no_new_thread_while_too_many_are_still_stuck(self):
        release, starts = threading.Event(), []

        def step():
            starts.append(time.monotonic())
            if len(starts) == 1:
                release.wait(10)
            else:
                self.runtime.shutdown.set()
        timer = threading.Timer(0.6, release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(app_web, "STUCK_SECONDS", 0.1), mock.patch.object(app_web, "MAX_LEFT", 1):
            began = time.monotonic()
            self.sync.loop("push", step, lambda: 0.01)
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - began, 0.55)


    def test_at_the_cap_it_goes_on_after_a_while_even_if_none_ends(self):
        """2026-11-19: three opens on the network disk never returned and the push waited for one of them for good, so the page
        kept a topic 在做 an hour after its turn had ended."""
        hang, starts = threading.Event(), []
        self.addCleanup(hang.set)

        def step():
            starts.append(time.monotonic())
            if len(starts) == 1:
                hang.wait(10)
            else:
                self.runtime.shutdown.set()
        guard = threading.Timer(3, self.runtime.shutdown.set)
        guard.start()
        self.addCleanup(guard.cancel)
        with mock.patch.object(app_web, "STUCK_SECONDS", 0.1), mock.patch.object(app_web, "MAX_LEFT", 1), \
                mock.patch.object(app_web, "LEFT_WAIT", 0.3, create=True):
            self.sync.loop("push", step, lambda: 0.01)
        self.assertEqual(len(starts), 2)
        self.assertTrue(any("still stuck" in line for line in self.logs))

    def test_a_topic_whose_read_hangs_is_left_out_until_it_returns(self):
        """The next look does not hang on the same topic again: the others are pushed, it is not deleted from the page, and once
        its read returns it is looked at again."""
        self.topic()
        self.topic(OTHER, number=8, title="写周报")
        hang, order = threading.Event(), []
        self.addCleanup(hang.set)
        real = app_web.Collector.topic_items

        def topic_items(collector, state, chat, topic, records, now):
            if topic == TOPIC and not hang.is_set():
                hang.wait(10)
            return real(collector, state, chat, topic, records, now)

        def step():
            count = self.sync.push_once()
            for key in (item["key"] for item in self.web.items("topic")):
                if key not in order:
                    order.append(key)
            if OTHER in order:
                hang.set()
            if TOPIC in order:
                self.runtime.shutdown.set()
            return count
        guard = threading.Timer(8, self.runtime.shutdown.set)
        guard.start()
        self.addCleanup(guard.cancel)
        with mock.patch.object(app_web.Collector, "topic_items", topic_items), mock.patch.object(app_web, "STUCK_SECONDS", 0.2), \
                mock.patch.object(app_web, "MAX_LEFT", 2):
            self.sync.loop("push", step, lambda: 0.01, self.sync.start_over, lambda: self.sync.collector.moved)
        self.assertEqual(order[:2], [OTHER, TOPIC])
        self.assertFalse([gone for batch in self.web.upserts for gone in batch["deleted"] if gone["key"] == TOPIC])
        self.assertTrue(any("leaves topic" in line for line in self.logs))

    def test_a_left_behind_look_that_comes_back_sends_nothing(self):
        """2026-11-19 14:36: a look stuck since before 13:00 came back and put T542 back to 在做 on the page, over what the
        newer looks had pushed: what it read is old, so it pushes none of it."""
        self.topic(status="running")
        hang, entered = threading.Event(), threading.Event()
        self.addCleanup(hang.set)
        real = app_web.Collector.collect

        def collect(collector, chat, now, full):
            found = real(collector, chat, now, full)
            entered.set()
            hang.wait(10)
            return found
        with mock.patch.object(app_web.Collector, "collect", collect):
            late = threading.Thread(target=self.sync.push_once, args=(T0,))
            late.start()
            self.assertTrue(entered.wait(5))
            self.sync.start_over()
            hang.set()
            late.join(5)
        self.assertEqual(self.web.upserts, [])
        self.sync.push_once(T0 + 1)
        self.assertTrue(self.web.items("topic"))

    def test_an_answered_question_goes_out_on_the_next_quick_look(self):
        """The page's 等你 count and question panel follow the record: the question answered is pushed answered at once,
        not only at the next full look."""
        self.topic(status="waiting_user")
        self.question()
        self.sync.push_once(T0)
        self.assertEqual([item["data"]["state"] for item in self.web.items("question")], ["open"])
        row = read_object(self.state / "questions" / f"{TOKEN}.json")
        write_json(self.state / "questions" / f"{TOKEN}.json", {**row, "status": "answered", "answers": {"q1": {"answers": ["A"]}}})
        self.sync.push_once(T0 + 1)
        self.assertEqual([item["data"]["state"] for item in self.web.items("question")], ["open", "answered"])

    def test_a_long_step_that_keeps_moving_is_not_left_behind(self):
        """The first push of a big chat: many batches, longer in all than STUCK_SECONDS, each one progress."""
        stamps, threads = [], []

        def step():
            threads.append(threading.current_thread())
            for _ in range(6):
                time.sleep(0.08)
                stamps.append(time.monotonic())
            self.runtime.shutdown.set()
        with mock.patch.object(app_web, "STUCK_SECONDS", 0.2):
            began = time.monotonic()
            self.sync.loop("push", step, lambda: 0.01, lambda: self.fail("reset"), lambda: stamps[-1] if stamps else 0.0)
        self.assertGreater(time.monotonic() - began, 0.4)
        self.assertEqual(len(threads), 1)
        self.assertFalse([line for line in self.logs if "stuck" in line])

    def test_progress_from_before_the_step_does_not_count(self):
        release, threads = threading.Event(), []

        def step():
            threads.append(threading.current_thread())
            if len(threads) == 1:
                release.wait(10)
            else:
                self.runtime.shutdown.set()
        early = time.monotonic()
        with mock.patch.object(app_web, "STUCK_SECONDS", 0.2):
            self.sync.loop("push", step, lambda: 0.01, None, lambda: early)
        release.set()
        threads[0].join(5)
        self.assertEqual(len([line for line in self.logs if "web sync push stuck" in line]), 1)

    def test_each_batch_of_a_push_and_each_topic_of_a_look_is_progress(self):
        self.topic()
        self.topic(OTHER, number=8, title="写周报")
        stamps = []
        real = self.sync.client.request

        def request(method, path, body=None):
            stamps.append(self.sync.collector.moved)
            return real(method, path, body)
        with mock.patch.object(self.sync.client, "request", request), mock.patch.object(web_sync, "BATCH_ITEMS", 1):
            self.sync.push_once(T0 + 1)
        self.assertGreaterEqual(len(stamps), 4)
        self.assertGreater(stamps[0], 0)
        self.assertTrue(all(later > earlier for earlier, later in zip(stamps, stamps[1:])), stamps)


class BackoffTest(WebCase):
    def test_a_failing_page_is_waited_out_with_a_growing_delay_and_logged_once(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) >= 5:
                self.runtime.shutdown.set()
            return self.runtime.shutdown.is_set()
        self.runtime.shutdown.wait = wait
        self.web.fail = 503
        self.sync.loop("inbox", self.sync.inbox_once, lambda: 1)
        self.assertEqual(waits, [2, 4, 8, 16, 30])
        self.assertEqual(len([line for line in self.logs if "failed" in line]), 1)

    def test_it_recovers_when_the_page_does(self):
        waits = []

        def wait(seconds):
            waits.append(seconds)
            self.web.fail = 0
            if len(waits) >= 2:
                self.runtime.shutdown.set()
            return self.runtime.shutdown.is_set()
        self.runtime.shutdown.wait = wait
        self.web.fail = 500
        self.sync.loop("inbox", self.sync.inbox_once, lambda: 1)
        self.assertEqual(waits, [2, 1])
        self.assertTrue(any("recovered" in line for line in self.logs))

    def test_a_missing_config_is_a_failure_not_a_crash(self):
        with mock.patch.dict(os.environ, {web_sync.CONFIG_ENV: str(self.state / "nope.json")}):
            self.sync.client.config = None
            with self.assertRaises(WebError):
                self.sync.inbox_once()


class InboxTest(WebCase):
    def test_say_to_an_idle_topic_starts_the_next_turn_once(self):
        self.topic()
        self.web.op("say", 1, "op-1", TOPIC, text="接着做吧")
        self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual(self.app.queued, [(TOPIC, "web-op-1", "接着做吧")])
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-1", "done")])
        lines = (self.state / "topics" / TOPIC / "history.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["content"], "接着做吧")
        self.web.outbox[0]["state"] = "new"  # The page's row did not take the ack: it is handed out again.
        self.sync.ledger.ops["op-1"]["acked"] = False
        self.assertEqual(self.sync.inbox_once(), 0)
        self.assertEqual(len(self.app.queued), 1)
        self.assertEqual([r["op_id"] for r in self.web.acks], ["op-1", "op-1"])

    def test_say_to_a_running_topic_is_a_supplement(self):
        self.topic()
        self.jobs[TOPIC] = [SimpleNamespace(job_id=3)]
        self.web.op("say", 1, "op-2", TOPIC, text="补一句")
        self.sync.inbox_once()
        self.assertEqual(self.app.steered, [(3, "web-op-2", "补一句")])
        self.assertEqual(self.app.queued, [])

    def test_say_goes_to_the_running_turn_not_a_message_queued_behind_it(self):
        # T551, 2026-11-19: the newest task of the topic was a queued message; the supplement belongs to the running one.
        self.topic()
        running, queued = SimpleNamespace(job_id=3), SimpleNamespace(job_id=4)
        self.jobs[TOPIC] = [running, queued]
        stages = {3: "running", 4: "queued"}
        self.app.steering.steer_stage = lambda job: stages[job.job_id]
        self.web.op("say", 1, "op-2", TOPIC, text="补一句")
        self.sync.inbox_once()
        self.assertEqual(self.app.steered, [(3, "web-op-2", "补一句")])
        stages[3] = "ended"  # Only the queued one is open: it takes it into its request.
        self.web.op("say", 2, "op-3", TOPIC, text="再补一句")
        self.sync.inbox_once()
        self.assertEqual(self.app.steered[-1], (4, "web-op-3", "再补一句"))

    def test_say_to_a_topic_waiting_for_one_answer_is_that_answer(self):
        """As a message in the topic's thread in Feishu (app/interaction.py): with one question waiting, the words are its answer, written.
        2026-11-19: they were its 只发补充说明 before, so the page's handiest box closed the question unanswered."""
        self.topic()
        self.question()
        self.web.op("say", 1, "op-3", TOPIC, text="都不合适，用标准库")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        self.assertIn("回答", self.web.acks[0]["message"])
        self.assertEqual(self.app.answers, [(TOPIC, TOKEN, {"answer_0": "都不合适，用标准库"}, "web-op-3")])
        self.assertEqual(self.app.queued, [])
        self.assertEqual(self.app.steered, [])

    def test_say_to_a_question_of_several_items_is_refused_and_leaves_it_open(self):
        self.topic()
        self.question(questions=[{"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
                                 {"id": "q2", "question": "放哪？", "options": []}])
        self.web.op("say", 1, "op-3", TOPIC, text="我再想想")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")
        self.assertIn("逐题回答", self.web.acks[0]["message"])
        self.assertEqual((self.app.answers, self.app.queued, self.app.steered), ([], [], []))
        self.assertEqual(json.loads((self.state / "questions" / f"{TOKEN}.json").read_text())["status"], "pending")

    def test_say_with_two_questions_waiting_is_refused(self):
        self.topic()
        self.question()
        self.question(token="tok223456789", created_at=T0 + 5)
        self.web.op("say", 1, "op-3", TOPIC, text="我来说")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")
        self.assertIn("几道题", self.web.acks[0]["message"])
        self.assertEqual((self.app.answers, self.app.queued), ([], []))

    def test_a_question_without_options_takes_the_owners_written_answer(self):
        self.topic()
        self.question(questions=[{"id": "answer", "header": "补充信息", "question": "请核对样例集表格", "options": []}])
        self.web.op("answer", 1, "op-4", TOPIC, token=TOKEN, answers={"answer": "12 改成等断点"})
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        self.assertEqual(self.app.answers[0][2], {"answer_0": "12 改成等断点", "supplement": ""})

    def test_only_the_note_is_sent_when_asked_and_it_needs_one(self):
        self.topic()
        self.question()
        self.web.op("answer", 1, "op-4", TOPIC, token=TOKEN, skip=True, note="")
        self.web.op("answer", 2, "op-5", TOPIC, token=TOKEN, skip=True, note="先放一放")
        self.sync.inbox_once()
        self.assertEqual([(r["state"], r["message"][:6]) for r in self.web.acks], [("failed", "只发补充说明"), ("done", "补充说明已交")])
        self.assertEqual([a[2] for a in self.app.answers], [{"skip_0": True, "supplement": "先放一放"}])

    def test_the_butlers_proxy_answer_still_refuses_a_free_text_question(self):
        from covey.app.answers import answer_fields
        record = {"questions": [{"id": "answer", "question": "写点什么", "options": []}]}
        with self.assertRaises(ValueError):
            answer_fields(record, {"answer": "随便"}, "")
        self.assertEqual(answer_fields(record, {"answer": "随便"}, "", owner=True), {"answer_0": "随便", "supplement": ""})

    def test_answer_goes_to_the_questions_own_path(self):
        self.topic()
        self.question()
        self.web.op("answer", 1, "op-4", TOPIC, token=TOKEN, answers={"q1": "B"}, note="就这个")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        topic, token, fields, event = self.app.answers[0]
        self.assertEqual((topic, token, event), (TOPIC, TOKEN, "web-op-4"))
        self.assertEqual(fields, {"option_0": "1", "supplement": "就这个"})

    def test_an_answer_to_an_answered_or_unknown_question_fails(self):
        self.topic()
        self.question(status="answered")
        self.web.op("answer", 1, "op-5", TOPIC, token=TOKEN, answers={"q1": "A"})
        self.web.op("answer", 2, "op-6", TOPIC, token="nosuchtoken1", answers={"q1": "A"})
        self.web.op("answer", 3, "op-7", TOPIC, token=TOKEN, answers={"q1": "Z"})
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed"] * 3)
        self.assertEqual(self.app.answers, [])

    def test_stop_goes_to_the_task_runner_or_is_stopped_here(self):
        self.topic()
        job = SimpleNamespace(job_id=4, message_id="om_job", cancel_event=threading.Event(), answer_finalized=False,
                              backend_control=object(), execution_started=True)
        self.jobs[TOPIC] = [job]
        self.web.op("stop", 1, "op-8", TOPIC)
        self.web.op("stop", 2, "op-9", TOPIC)
        with mock.patch("covey.app.runner_ipc.forward_card_action", side_effect=[True, False]) as forward:
            self.sync.inbox_once()
        self.assertEqual(forward.call_count, 2)
        self.assertEqual([r["state"] for r in self.web.acks], ["done", "done"])
        self.assertEqual([action["job_id"] for action in self.app.stopped], ["4"])
        self.assertEqual(forward.call_args_list[0].args[3]["job_id"], "4")

    def test_stop_without_a_task_fails(self):
        self.topic()
        self.web.op("stop", 1, "op-10", TOPIC)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")

    def test_stop_closes_the_question_a_topic_waits_on(self):
        """The page's 停止 on a topic waiting for the owner (no task runs): its question cancelled (web_stop), its waiting turn cancelled."""
        self.topic(status="waiting_user")
        self.question()
        self.web.op("stop", 1, "op-10", TOPIC)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        self.assertIn("等你回答的题已收掉", self.web.acks[0]["message"])
        saved = read_object(self.state / "questions" / f"{TOKEN}.json")
        self.assertEqual((saved["status"], saved.get("cancel_reason")), ("cancelled", "web_stop"))
        self.assertGreaterEqual(saved["cancelled_at"], T0)  # core/store.py SETTLED_STAMPS: the page's report ends its 等你 there.
        self.assertEqual(self.app.painted, [(TOKEN, "cancelled")])
        job = read_object(job_file(self.state, f"k-{TOPIC[:4]}"))
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self.app.stopped, [])

    def test_stop_closes_a_launch_card_not_started(self):
        """A topic whose only task waits for 开始 on its launch card (T561, 2026-11-19): the page's 停止 closes the card, the
        task is cancelled as never started, and the topic waits no more."""
        self.topic(status="waiting_workspace")
        form = self.state / "launch-forms" / "f1.json"
        write_json(form, {"topic_id": TOPIC, "chat_id": CHAT, "status": "ready", "message_id": "om_launch", "harness_id": "claude",
                          "schema_version": 3})
        self.web.op("stop", 1, "op-launch", TOPIC)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done", self.web.acks)
        self.assertIn("启动卡", self.web.acks[0]["message"])
        self.assertEqual(read_object(form)["status"], "closed")
        job = read_object(job_file(self.state, f"k-{TOPIC[:4]}"))
        self.assertEqual((job["status"], job.get("never_started")), ("cancelled", True))
        self.sync.push_once(T0)
        topic = next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]
        self.assertNotEqual(topic["tone"], "waiting")

    def test_words_never_run_get_a_line_that_says_so(self):
        """T561 (2026-11-19): its launch card closed by a stop, its one message never ran; the page drew 排队中… under it for good,
        as nothing answered it. It gets a stopped line: 没开工，已取消, and why."""
        self.topic(status="waiting_workspace", lines=(("user", "修专项右栏"),))
        form = self.state / "launch-forms" / "f1.json"
        write_json(form, {"topic_id": TOPIC, "chat_id": CHAT, "status": "ready", "message_id": "om_launch", "harness_id": "claude",
                          "schema_version": 3})
        self.web.op("stop", 1, "op-launch2", TOPIC)
        self.sync.inbox_once()
        self.sync.push_once(T0)
        turns = [item["data"] for item in self.web.items("turn")]
        self.assertEqual([(turn["role"], turn["status"]) for turn in turns[-2:]], [("owner", "done"), ("agent", "stopped")], turns)
        self.assertEqual(turns[-1]["body"], "没开工，已取消（你在网页上停了这个话题）")

    def test_a_topic_the_owner_stopped_on_the_page_is_his_stop_not_stopped_by_someone(self):
        """2026-11-18 T478: stopped on the page while it waited for an answer, it showed 停住了 (red, counted in 要你处理, with
        检查并继续): the cancelled turn did not say who stopped it. It says web now, and reads 已停止."""
        self.topic(status="waiting_user")
        write_job(self.state, f"k-{TOPIC[:4]}", timings={"backend_started": T0 - 60})
        self.question()
        self.web.op("stop", 1, "op-10", TOPIC)
        self.sync.inbox_once()
        job = read_object(job_file(self.state, f"k-{TOPIC[:4]}"))
        self.assertEqual((job["status"], job.get("cancel_requested_by")), ("cancelled", "web"))
        self.sync.push_once(T0)
        topic = next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]
        self.assertEqual(topic["tone"], "cancelled")

    def test_reopen_unfolds_a_folded_project_and_refuses_the_rest(self):
        projects.update(self.state, PID, lambda record: record.update(state="paused", state_reason="idle", idle={"at": T0, "days": 7}))
        self.web.op("reopen", 1, "op-11", project=PID)
        self.web.op("reopen", 2, "op-12", project=PID)
        self.web.op("reopen", 3, "op-13", project="nosuchproject")
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["done", "done", "failed"])
        self.assertEqual(self.reload()["state"], "active")
        projects.update(self.state, PID, lambda record: record.update(state="done"))
        self.web.op("reopen", 4, "op-14", project=PID)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "failed")
        self.assertEqual(self.reload()["state"], "done")

    def test_an_unknown_topic_and_an_unknown_kind_fail_with_a_reason(self):
        self.web.op("say", 1, "op-15", OTHER, text="喂")
        self.web.op("fly", 2, "op-16", TOPIC)
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed", "failed"])
        self.assertTrue(all(r["message"] for r in self.web.acks))

    def test_a_restart_does_not_do_an_operation_again_and_an_unfinished_one_is_not_replayed(self):
        self.topic()
        self.web.op("say", 1, "op-17", TOPIC, text="一次")
        self.sync.inbox_once()
        again = WebSync(self.runtime, self.env)
        again.client.opener = http_upstream.opener(direct=True)
        self.web.outbox[0]["state"] = "new"
        again.inbox_once()
        self.assertEqual(len(self.app.queued), 1)
        # A crash between "taken" and "done": the ledger says received.
        web_sync.Ledger(self.state).take("op-18", 2)
        self.web.op("say", 2, "op-18", TOPIC, text="半路")
        third = WebSync(self.runtime, self.env)
        third.client.opener = http_upstream.opener(direct=True)
        third.inbox_once()
        self.assertEqual(len(self.app.queued), 1)
        self.assertEqual(self.web.acks[-1]["state"], "failed")
        self.assertIn("没有重发", self.web.acks[-1]["message"])

    def test_the_ledger_is_written_before_the_operation_runs(self):
        self.topic()
        self.web.op("say", 1, "op-19", TOPIC, text="先记")
        seen = []
        real = web_ops.Ops.run

        def run(ops, row):
            seen.append(web_sync.Ledger(self.state).get("op-19"))
            return real(ops, row)
        with mock.patch.object(web_ops.Ops, "run", run):
            self.sync.inbox_once()
        self.assertEqual((seen[0]["state"], seen[0]["seq"]), ("received", 1))
        self.assertEqual(web_sync.Ledger(self.state).get("op-19")["state"], "done")

    def test_after_stays_below_an_operation_whose_ack_did_not_go(self):
        ledger = web_sync.Ledger(self.state)
        ledger.take("a", 5)
        ledger.finish("a", "done", "")
        self.assertEqual(ledger.after(), 4)
        ledger.acked(["a"])
        self.assertEqual(ledger.after(), 5)


class ButlerSayTest(WebCase):
    def test_say_to_the_butler_is_the_owners_top_level_message_and_reaches_it_once(self):
        self.butler_topic()
        self.hearing()
        self.web.op("say", 1, "op-b1", BUTLER, text="帮我看下账单")
        self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual(len(self.heard), 1)
        event = self.heard[0]
        self.assertEqual((event["message_id"], event["chat_id"], event["chat_type"], event["sender_id"], event["message_type"]),
                         ("web-op-b1", CHAT, "p2p", "ou_owner", "text"))
        self.assertEqual(json.loads(event["content"]), {"text": "帮我看下账单"})
        for field in ("root_id", "parent_id", "thread_id"):
            self.assertNotIn(field, event)  # At the top level, quoting nothing.
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-b1", "done")])
        self.assertEqual(self.app.queued, [])  # Not the topic path.
        self.web.outbox[0]["state"] = "new"  # The ack did not take: handed out again.
        self.sync.ledger.ops["op-b1"]["acked"] = False
        self.assertEqual(self.sync.inbox_once(), 0)
        self.assertEqual(len(self.heard), 1)

    def test_say_to_the_butler_fails_with_the_reason_when_it_cannot_take_it(self):
        self.butler_topic()
        self.hearing("管家现在没有开")
        self.web.op("say", 1, "op-b2", BUTLER, text="喂")
        self.sync.inbox_once()
        self.assertEqual([(r["state"], r["message"]) for r in self.web.acks], [("failed", "管家现在没有开")])
        self.sync.ops.hear = None
        self.web.op("say", 2, "op-b3", BUTLER, text="喂")
        self.web.op("say", 3, "op-b4", BUTLER, text="  ")
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed"] * 3)

    def test_an_answer_to_the_butlers_question_is_left_to_feishu_with_a_reason(self):
        self.butler_topic()
        self.question("tok-butler-001", topic_id=BUTLER)
        self.web.op("answer", 1, "op-b5", BUTLER, token="tok-butler-001", answers={"q1": "A"})
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed"])
        self.assertIn("飞书", self.web.acks[0]["message"])
        self.assertEqual(self.app.answers, [])


class NoReplyToTheWebMessageTest(WebCase):
    """``web-<op_id>`` is no Feishu message: no reply quotes it, no thread opens under it, no reaction goes on it."""

    def job(self, message, topic=BUTLER):
        return {"message_id": message, "topic_id": topic, "chat_id": CHAT, "job_id": 5}

    def test_the_turn_replies_at_the_top_level_and_is_quiet_with_no_reaction(self):
        self.butler_topic()
        remember_topic_message(self.state, BUTLER, CHAT, "web-op-1")
        remember_topic_message(self.state, BUTLER, CHAT, "om_real")
        ctx = dataclasses.replace(codex_context(self.state, "stream-json"), state_dir=self.state)
        web = context_for_topic(ctx, {"chat_id": CHAT, "chat_type": "p2p", "message_id": "web-op-1"}, lambda w: w)
        real = context_for_topic(ctx, {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_real"}, lambda w: w)
        self.assertEqual((web.topic_id, web.reply_to_message_id), (BUTLER, ""))
        self.assertEqual((real.topic_id, real.reply_to_message_id), (BUTLER, "om_real"))  # A Feishu message is quoted as ever.
        job = self.job("web-op-1")
        self.assertTrue(placement.quiet(self.state, job))  # No card until it has run a while, as the owner's own message.
        self.assertFalse(placement.marked(self.state, job))  # No message to react on.
        self.assertTrue(placement.marked(self.state, self.job("om_real")))
        self.assertFalse(placement.quiet(self.state, self.job("main-tool-1")))  # What was not quiet is not.
        self.assertFalse(placement.quiet(self.state, self.job("web-op-1", topic=OTHER)))  # A topic's turn is its own.
        from covey.app import butler_reactions
        messenger = mock.Mock()
        started = SimpleNamespace(job_id=5, message_id="web-op-1", topic_id=BUTLER, chat_id=CHAT)
        self.assertFalse(butler_reactions.start(messenger, self.state, "p", started, self.logs.append))
        self.assertFalse(butler_reactions.owner_quiet(self.state, CHAT, "web-op-1"))
        messenger.react.assert_not_called()


class ButlerWebTest(ButlerUnifiedCase):
    """Through the real Bridge: the page's words are the owner's top-level message, the butler's turn is queued once, nothing is sent."""

    def test_say_to_the_butler_queues_one_turn_of_the_butler_and_sends_nothing(self):
        env = SimpleNamespace(runtime=self.app.runtime, state_dir=self.state, topics=self.app.topics)
        ops = web_ops.Ops(env, self.app.web_message)
        row = {"kind": "say", "op_id": "op-e1", "topic_key": BUTLER, "payload": {"text": "帮我看下账单"}}
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            self.assertEqual(ops.run(row), ("done", "已交给管家"))
            self.assertEqual(ops.run(row)[0], "done")  # The same message again: the duplicate check takes it, no second turn.
        jobs = self.app.runtime.jobs.topic_jobs(BUTLER, CHAT)
        self.assertEqual([(job.message_id, job.topic_id, job.reply_to_message_id) for job in jobs], [("web-op-e1", BUTLER, "")])
        history = [json.loads(line) for line in (self.state / "topics" / BUTLER / "history.jsonl").read_text().splitlines()]
        self.assertEqual([(row["role"], row["content"]) for row in history], [("user", "帮我看下账单")])
        self.assertFalse(placement.marked(self.state, jobs[0]))
        self.app.messenger.react.assert_not_called()
        self.app.messenger.send_message.assert_not_called()

    def test_it_is_refused_with_a_reason_when_the_butler_is_off(self):
        env = SimpleNamespace(runtime=self.app.runtime, state_dir=self.state, topics=self.app.topics)
        ops = web_ops.Ops(env, self.app.web_message)
        self.settings(enabled=False)  # 纯话题: a message of the private chat would open a topic of its own.
        with mock.patch.object(self.app.runtime.access, "owner_chat", return_value=CHAT):
            state, message = ops.run({"kind": "say", "op_id": "op-e2", "topic_key": BUTLER, "payload": {"text": "喂"}})
        self.assertEqual(state, "failed")
        self.assertIn("管家", message)
        self.assertEqual(self.app.runtime.jobs.topic_jobs(BUTLER, CHAT), [])


class BoardGroupTest(WebCase):
    """Data contract v6 ``board``: the group the board (app/pins.py) has a topic in, the page's 要你处理 by it. T514 (2026-11-19):
    stopped by an official refusal on 11-17 and seen at once, it was off the board from 11-18 00:51, yet the page listed it
    停住了 under 要你处理 (its tone, within three days) from 11-19 08:55."""

    def setUp(self):
        super().setUp()
        from covey.core import topic_seen
        patch = mock.patch.dict(os.environ, {topic_seen.PINNED_TOPICS_ENV: "1", "COVEY_PINNED_OVERVIEW": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        long_ago = T0 - 10 * 86400
        write_json(self.state / "pinned-topics.json", {"chat_id": CHAT, "since": long_ago, "keep_since": long_ago,
                                                       "stopped_since": long_ago, "results": {}})

    def stopped(self, seen):
        self.topic(status="cancelled")
        ended = T0 - 20 * 3600
        write_job(self.state, f"k-{TOPIC[:4]}", cancel_requested_by="guard",
                  timings={"backend_started": ended - 60, "execution_finished": ended})
        if seen:
            from covey.core.topic_seen import record_reads
            record_reads(self.state, {TOPIC: ended + 22}, now=ended + 22)
        self.sync.push_once(T0)
        return next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]

    def test_a_stop_seen_long_ago_is_stopped_but_not_on_the_board(self):
        data = self.stopped(seen=True)
        self.assertEqual((data["tone"], data["board"]), ("stopped", ""))

    def test_a_stop_not_seen_is_on_the_board_as_stopped(self):
        data = self.stopped(seen=False)
        self.assertEqual((data["tone"], data["board"]), ("stopped", "stopped"))

    def test_without_the_board_there_is_no_board_field(self):
        os.environ.pop("COVEY_PINNED_OVERVIEW")
        self.topic()
        self.sync.push_once(T0)
        self.assertNotIn("board", self.web.items("topic")[-1]["data"])


class SeenTest(WebCase):
    def test_seen_marks_the_latest_result_as_read_and_the_next_push_says_so(self):
        self.topic(lines=())
        self.sync.push_once(T0)
        self.assertTrue(self.web.items("topic")[-1]["data"]["unseen"])
        self.web.op("seen", 1, "op-s1", TOPIC)
        self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual([(r["state"], r["message"]) for r in self.web.acks], [("done", "已记为看过")])
        self.assertIn(TOPIC, read_seen(self.state))
        self.sync.push_once(T0 + 1)
        last = self.web.items("topic")[-1]
        self.assertEqual((last["key"], last["data"]["unseen"]), (TOPIC, False))

    def test_seen_again_is_done_and_writes_nothing(self):
        self.topic(lines=())
        self.web.op("seen", 1, "op-s2", TOPIC)
        self.web.op("seen", 2, "op-s3", TOPIC)
        self.sync.inbox_once()
        self.assertEqual([(r["state"], r["message"]) for r in self.web.acks], [("done", "已记为看过"), ("done", "已经记为看过")])
        saved = read_seen(self.state)
        self.web.op("seen", 3, "op-s4", TOPIC)
        self.sync.inbox_once()
        self.assertEqual((self.web.acks[-1]["state"], read_seen(self.state)), ("done", saved))

    def test_a_result_that_comes_later_is_unseen_again(self):
        self.topic(lines=())
        self.web.op("seen", 1, "op-s5", TOPIC)
        self.sync.inbox_once()
        self.sync.push_once(time.time())
        self.assertFalse(self.web.items("topic")[-1]["data"]["unseen"])
        time.sleep(1.1)  # A record's updated_at has a second's resolution.
        write_job(self.state, "k-later", topic_id=TOPIC, chat_id=CHAT, job_id=70, status="done", card_message_id="om_card2")
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(time.time())
        self.assertTrue(self.web.items("topic")[-1]["data"]["unseen"])

    def test_an_unknown_topic_another_chats_topic_and_a_bad_id_fail_with_a_reason(self):
        write_topic(self.state, topic_id=OTHER, chat_id="oc_other", harness_id="claude", topic_number=9)
        self.topic()
        self.web.op("seen", 1, "op-s6", "e" * 32)
        self.web.op("seen", 2, "op-s7", OTHER)
        self.web.op("seen", 3, "op-s8", "../x")
        self.web.op("seen", 4, "op-s9", "")
        self.web.op("seen", 5, "op-s0", TOPIC)
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed"] * 4 + ["done"])
        self.assertTrue(all(r["message"] for r in self.web.acks))
        self.assertEqual(list(read_seen(self.state)), [TOPIC])

    def test_the_butler_has_nothing_unseen_and_the_answer_is_done(self):
        self.butler_topic()
        self.web.op("seen", 1, "op-s10", BUTLER)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        self.assertEqual(read_seen(self.state), {})


class SwitchTest(WebCase):
    def test_off_means_no_thread_and_no_request(self):
        with mock.patch.dict(os.environ, {web_sync.ENV: "0"}):
            self.assertEqual(self.sync.threads(), ())
        with mock.patch.dict(os.environ):
            os.environ.pop(web_sync.ENV)
            self.assertEqual(self.sync.threads(), ())
        self.assertEqual(self.web.seen, [])

    def test_off_the_butler_and_web_messages_behave_as_before(self):
        self.butler_topic()
        with mock.patch.dict(os.environ, {web_sync.ENV: "0"}):
            self.assertEqual(self.sync.threads(), ())
            self.assertEqual(self.web.seen, [])
        self.assertTrue(placement.quiet(self.state, {"message_id": "om_1", "topic_id": BUTLER, "chat_id": CHAT}))
        self.assertFalse(placement.quiet(self.state, {"message_id": "main-tool-1", "topic_id": BUTLER, "chat_id": CHAT}))
        ctx = dataclasses.replace(codex_context(self.state, "stream-json"), state_dir=self.state)
        remember_topic_message(self.state, BUTLER, CHAT, "om_real")
        self.assertEqual(context_for_topic(ctx, {"chat_id": CHAT, "chat_type": "p2p", "message_id": "om_real"}, lambda w: w).reply_to_message_id, "om_real")
        self.assertTrue(web_sync.is_web_message("web-x") and not web_sync.is_web_message("om_web-x"))

    def test_on_there_are_two_threads(self):
        self.assertEqual([name for name, _ in self.sync.threads()], ["web-push", "web-inbox"])

    def test_the_bridge_lists_the_threads_only_with_the_switch(self):
        from covey.app.unified import UnifiedBridgeApp
        self.assertIn("web_sync", UnifiedBridgeApp.PARTS)


def snapshot_of(now):
    """A running turn's process snapshot: a read that ended, a command that runs, a note, and the figures."""
    return {"steps": {"s1": {"title": "读取文件", "detail": "/x/a.py", "status": "done", "started": now, "ended": now + 1},
                      "s2": {"title": "执行命令", "detail": "ls", "status": "running", "started": now + 2},
                      "n1": {"title": "进展说明", "detail": "先看了文件", "status": "note", "started": now}},
            "step_order": ["s1", "n1", "s2"], "started_at": now, "ended_at": None, "input_tokens": 1200, "output_tokens": 300,
            "cache_read_tokens": 900, "measured_output_tokens": 300, "generation_seconds": 10, "context_tokens": 5000, "context_window": 200000}


class ExtrasTest(WebCase):
    """Data contract v3: a turn's process, plan and figures, a question's times, a project's memory and library."""

    def traced(self, snapshot, **fields):
        for module in (app_web, web_turns):
            patch = mock.patch.object(module, "load_trace", side_effect=lambda *args: snapshot)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_running_turn_carries_its_steps_plan_and_figures(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        plan = self.state / "plan.json"
        write_json(plan, {"steps": [{"title": "读代码", "state": "done"}, {"title": "改代码", "state": "doing"}, {"title": "测试", "state": "todo"}]})
        write_job(self.state, f"k-{TOPIC[:4]}", checkpoint_path=str(plan), timings={"backend_started": T0})
        self.traced(snapshot_of(T0))
        self.sync.push_once(T0 + 13)
        turn = self.web.items("turn")[-1]["data"]
        self.assertEqual([(row["id"], row["kind"], row["status"], row["ended_at"]) for row in turn["steps"]],
                         [("s1", "read", "done", T0 + 1), ("s2", "command", "running", None)])
        self.assertEqual(turn["steps"][0]["label"], "读取文件：/x/a.py")
        self.assertEqual([(row["title"], row["state"]) for row in turn["plan"]], [("读代码", "done"), ("改代码", "doing"), ("测试", "todo")])
        self.assertEqual(turn["usage"], {"input": 1200, "output": 300, "cache_read": 900, "total": 1500})
        self.assertEqual((turn["speed"], turn["context"], turn["seconds"]), (30, {"used": 5000, "limit": 200000}, 10.0))
        topic = next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]
        self.assertEqual((topic["round"], topic["context"]), (1, {"used": 5000, "limit": 200000}))
        self.assertEqual(turn["body"], "先看了文件")

    def test_an_ended_turn_has_no_step_doing(self):
        """T542 2026-11-19: its 4th turn ended with 真机验收 still doing in its checkpoint, and the page drew it 在做 with a
        spinner on a topic that was done. The step was not done: todo."""
        self.topic(lines=(("user", "你好"), ("assistant", "好的")))
        plan = self.state / "plan.json"
        write_json(plan, {"steps": [{"title": "读代码", "state": "done"}, {"title": "真机验收", "state": "doing"}, {"title": "部署", "state": "todo"}]})
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="m你好", checkpoint_path=str(plan),
                  timings={"backend_started": T0, "execution_finished": T0 + 42})
        self.traced(dict(snapshot_of(T0), ended_at=T0 + 42))
        self.sync.push_once(T0 + 60)
        turn = {item["key"]: item["data"] for item in self.web.items("turn")}[f"{TOPIC}:2"]
        self.assertEqual([(row["title"], row["state"]) for row in turn["plan"]], [("读代码", "done"), ("真机验收", "todo"), ("部署", "todo")])

    def test_a_turn_waiting_for_an_answer_keeps_its_step_doing(self):
        self.topic(status="waiting_user", lines=(("user", "做一下"),))
        plan = self.state / "plan.json"
        write_json(plan, {"steps": [{"title": "读代码", "state": "done"}, {"title": "改代码", "state": "doing"}]})
        write_job(self.state, f"k-{TOPIC[:4]}", checkpoint_path=str(plan), timings={"backend_started": T0})
        self.traced(snapshot_of(T0))
        self.sync.push_once(T0 + 13)
        turn = self.web.items("turn")[-1]["data"]
        self.assertEqual([row["state"] for row in turn["plan"]], ["done", "doing"])

    def test_a_subtask_is_one_step_from_its_start_to_its_end_in_its_own_words(self):
        """T488 2026-11-19: two subtasks started in the background ran 35 of the turn's 46 minutes; the page drew
        子任务：子任务 at 0 and 分配子任务：Agent at 0.1 s, and the time looked spent on nothing."""
        snapshot = {"steps": {"s1": {"title": "读取文件", "detail": "/x/a.py", "status": "done", "started": T0, "ended": T0 + 1},
                              "toolu_a": {"title": "分配子任务", "detail": "Agent", "status": "done", "started": T0 + 2, "ended": T0 + 2.1},
                              "agent-toolu_a": {"title": "子任务", "detail": "子任务", "status": "done", "started": T0 + 2},
                              "toolu_b": {"title": "分配子任务", "detail": "Agent", "status": "done", "started": T0 + 3, "ended": T0 + 3.1},
                              "agent-toolu_b": {"title": "子任务", "detail": "子任务", "status": "running", "started": T0 + 3},
                              "agent-th1": {"title": "子任务", "detail": "子任务", "status": "done", "started": T0 + 4}},
                    "step_order": ["s1", "toolu_a", "agent-toolu_a", "toolu_b", "agent-toolu_b", "agent-th1"],
                    "started_at": T0, "ended_at": None,
                    "agents": {"toolu_a": {"name": "v1.4 剩余失败题找根因", "started_at": T0 + 2, "status": "completed", "ended_at": T0 + 2100},
                               "toolu_b": {"name": "子任务", "started_at": T0 + 3, "status": "running"},
                               "th1": {"name": "子任务 3", "description": "找出 卡片 的根因", "started_at": T0 + 4, "status": "errored",
                                       "ended_at": T0 + 50}}}
        rows = [(row["id"], row["kind"], row["label"], row["started_at"], row["ended_at"], row["status"]) for row in web_turns.steps_of(snapshot)]
        self.assertEqual(rows, [("s1", "read", "读取文件：/x/a.py", T0, T0 + 1, "done"),
                                ("agent-toolu_a", "subagent", "子任务：v1.4 剩余失败题找根因", T0 + 2, T0 + 2100, "done"),
                                ("agent-toolu_b", "subagent", "子任务：子任务", T0 + 3, None, "running"),
                                ("agent-th1", "subagent", "子任务：找出 卡片 的根因", T0 + 4, T0 + 50, "failed")])
        # The turn ended with a subtask still live: it ended with the turn, not at 0.
        rows = web_turns.steps_of(dict(snapshot, ended_at=T0 + 2400))
        self.assertEqual([(row["ended_at"], row["status"]) for row in rows if row["id"] == "agent-toolu_b"], [(T0 + 2400, "done")])

    def test_a_quiet_running_turn_does_not_change_its_rev_every_second(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        write_job(self.state, f"k-{TOPIC[:4]}", timings={"backend_started": T0})
        self.traced(snapshot_of(T0))
        self.sync.push_once(T0 + 11)
        self.assertEqual([self.sync.push_once(T0 + 11 + second) for second in range(1, 8)], [0] * 7)
        self.assertEqual(self.sync.push_once(T0 + 21), 1)
        self.assertEqual(self.web.items("turn")[-1]["data"]["seconds"], 20.0)

    def test_an_ended_turn_has_its_figures_once_and_a_turn_without_them_none(self):
        self.topic(lines=(("user", "你好"), ("assistant", "好的")))
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="m你好", timings={"backend_started": T0, "execution_finished": T0 + 42})
        done = dict(snapshot_of(T0), ended_at=T0 + 42)
        done["steps"] = {key: dict(step, status="done" if step["status"] == "running" else step["status"]) for key, step in done["steps"].items()}
        self.traced(done)
        self.sync.push_once(T0 + 60)
        turns = {item["key"]: item["data"] for item in self.web.items("turn")}
        self.assertNotIn("usage", turns[f"{TOPIC}:1"])
        self.assertEqual((turns[f"{TOPIC}:2"]["seconds"], turns[f"{TOPIC}:2"]["usage"]["total"], turns[f"{TOPIC}:2"]["speed"]), (42.0, 1500, 30.0))
        self.assertEqual(self.sync.push_once(T0 + 61), 0)

    def test_a_turn_carries_the_first_estimate_its_task_kept_and_one_without_none(self):
        self.topic(lines=(("user", "你好"), ("assistant", "好的")))
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="m你好", estimate_seconds=3000)
        self.sync.push_once(T0)
        turns = {item["key"]: item["data"] for item in self.web.items("turn")}
        self.assertNotIn("estimate_seconds", turns[f"{TOPIC}:1"])
        self.assertEqual(turns[f"{TOPIC}:2"]["estimate_seconds"], 3000)

    def test_a_running_turn_carries_its_first_estimate(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        write_job(self.state, f"k-{TOPIC[:4]}", timings={"backend_started": T0}, estimate_seconds=1800)
        self.traced(snapshot_of(T0))
        self.sync.push_once(T0 + 13)
        self.assertEqual(self.web.items("turn")[-1]["data"]["estimate_seconds"], 1800)

    def test_without_a_trace_the_turn_is_as_before(self):
        self.topic(lines=(("user", "你好"), ("assistant", "好的")))
        self.sync.push_once(T0)
        for item in self.web.items("turn"):
            self.assertFalse({"steps", "plan", "usage", "speed", "context", "notes", "started_topics"} & set(item["data"]))

    def test_memory_notes_of_the_record_go_as_they_are_and_none_without(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        self.traced(snapshot_of(T0))
        self.sync.push_once(T0)
        self.assertNotIn("notes", self.web.items("turn")[-1]["data"])
        note = {"kind": "remembered", "text": "用新库", "scope": "管这个专项", "entry_id": "ab12"}
        undone = {"kind": "undone", "text": "先别动卡片", "scope": "管这个话题", "entry_id": "cd34"}
        write_job(self.state, f"k-{TOPIC[:4]}", memory_notes=[note, {"kind": "unknown", "text": "x"}, {"kind": "voided", "text": ""}, undone])
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.items("turn")[-1]["data"]["notes"], [note, undone])

    def test_memory_notes_written_after_the_turn_ended_reach_the_page(self):
        self.topic(status="done")
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="m你好")  # The turn of the owner's "你好".
        self.sync.push_once(T0)
        self.assertNotIn("notes", self.web.items("turn")[-1]["data"])
        note = {"kind": "remembered", "text": "用新库", "scope": "管这个专项", "entry_id": "ab12"}
        ended = read_object(job_file(self.state, f"k-{TOPIC[:4]}"))["updated_at"]
        with mock.patch("covey.core.jobs.utc_now_iso", return_value=ended):  # Written within the second the turn ended.
            write_job(self.state, f"k-{TOPIC[:4]}", memory_notes=[note])
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        pushed = {item["key"]: item["data"] for item in self.web.items("turn")}
        self.assertEqual([data.get("notes") for data in pushed.values() if data.get("notes")], [[note]], pushed)

    def test_a_question_the_lead_holds_is_not_the_owners_to_answer_until_passed_on(self):
        self.topic()
        self.question(triage={"state": "held", "deadline": T0 + 180})
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.items("question")[-1]["data"]["state"], "held")
        self.question(triage={"state": "escalated"})
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 61)
        self.assertEqual(self.web.items("question")[-1]["data"]["state"], "open")

    def test_a_turn_that_did_not_succeed_says_why_and_what_next(self):
        """C4 (data contract v4, turn.data failure): the card's reason and next steps, read from the record."""
        self.topic(status="failed", lines=(("user", "做一下"),))
        write_job(self.state, f"k-{TOPIC[:4]}", failure_code="rate_limited", status_message="模型服务限流，本轮没有产出回复")
        self.sync.push_once(T0)
        ended = self.web.items("turn")[-1]["data"]
        self.assertEqual(ended["status"], "failed")
        self.assertEqual(ended["failure"]["actions"], ["retry", "switch_model"])
        self.assertTrue(ended["failure"]["title"].startswith("上游限流，和任务内容无关"))
        self.assertIn("错误码 rate_limited", ended["failure"]["detail"])

    def test_a_turn_the_policy_stopped_is_failed_not_done(self):
        """2026-11-19 T514 (data contract v10): a 官方拒绝 stop writes its words as the turn's answer line; the page read it as a
        finished turn whose conclusion was the raw stop message, with no reason and no next step."""
        stop = "本轮已停止：SubAgent toolu_01 触发官方内容安全拒绝（frontier_llm）。"
        policy = {"kind": "refusal", "category": "frontier_llm", "scope": "subagent", "evidence": "refusal_stop"}
        self.topic(status="cancelled", lines=(("user", "改一下"), ("assistant", stop)))
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="m改一下", status_message=stop, policy_stop=policy,
                  timings={"backend_started": T0 - 60})
        self.sync.push_once(T0)
        answer = self.web.items("turn")[-1]["data"]
        self.assertEqual((answer["role"], answer["status"]), ("agent", "failed"))
        self.assertIn("官方安全策略", answer["failure"]["title"])
        self.assertEqual(answer["failure"]["actions"], ["split", "switch_model"])

    def test_a_stop_without_an_answer_line_is_failed_unless_the_owner_made_it(self):
        policy = {"kind": "refusal", "category": "frontier_llm", "scope": "main", "evidence": "refusal_stop"}
        self.topic(status="cancelled", lines=(("user", "改一下"),))
        write_job(self.state, f"k-{TOPIC[:4]}", status_message="本轮已停止", policy_stop=policy, timings={"backend_started": T0 - 60})
        self.sync.push_once(T0)
        self.assertEqual(self.web.items("turn")[-1]["data"]["status"], "failed")
        write_job(self.state, f"k-{TOPIC[:4]}", policy_stop=None, cancel_requested_by="owner")
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        ended = self.web.items("turn")[-1]["data"]
        self.assertEqual(ended["status"], "stopped")
        self.assertNotIn("failure", ended)

    def test_a_continued_turn_is_not_running_and_an_unknown_status_is_idle(self):
        """2026-11-18: T75 (its last turn ``continued`` on 10-22) showed 在做 on the page for a month; so did a waiting_workspace task."""
        self.topic()
        write_job(self.state, "k-cont", topic_id=TOPIC, chat_id=CHAT, job_id=8, status="continued", model="opus")
        write_topic(self.state, topic_id=OTHER, chat_id=CHAT, harness_id="claude", topic_number=8, origin={"title": "开工前"})
        write_job(self.state, "k-ws", topic_id=OTHER, chat_id=CHAT, job_id=9, status="waiting_workspace", model="opus")
        self.sync.push_once(T0 + 1)
        states = {item["key"]: item["data"]["state"] for item in self.web.items("topic")}
        self.assertEqual((states[TOPIC], states[OTHER]), ("做完", "空闲"))

    def test_a_topic_says_its_last_turn_so_the_page_drops_a_stale_running_one(self):
        """2026-11-19 T552: a task pushed as running at seq count + 1 whose answer came in at a lower seq left that running row on
        the page for good (T519, T559 ...): the 专项报告 counted its tokens twice and drew it as running. The topic says its last
        seq (data contract v12) and the page drops the turns past it."""
        self.topic(status="running", lines=(("user", "你好"), ("assistant", "好的"), ("user", "再看看")))
        self.sync.push_once(T0)
        topic = [item["data"] for item in self.web.items("topic") if item["key"] == TOPIC][-1]
        seqs = [item["data"]["seq"] for item in self.web.items("turn") if item["data"]["topic_key"] == TOPIC]
        self.assertEqual((topic["last_seq"], max(seqs)), (4, 4))
        write_job(self.state, f"k-{TOPIC[:4]}", status="done")
        self.web.upserts.clear()
        self.sync.push_once(T0 + 1)
        topic = [item["data"] for item in self.web.items("topic") if item["key"] == TOPIC][-1]
        self.assertEqual(topic["last_seq"], 3)

    def test_a_member_topic_says_when_it_came_into_its_project(self):
        """T552: the 专项报告 counts a member's turns from when it came in (data contract v13), as the 专项卡's token does."""
        self.thread(OTHER, "对账", 8, status="done")
        projects.update(self.state, PID, lambda record: record.update(joined={OTHER: 1234.5}))
        self.topic()
        self.sync.push_once(T0)
        by = {item["key"]: item["data"] for item in self.web.items("topic")}
        self.assertEqual(by[OTHER]["joined_at"], 1234.5)
        self.assertNotIn("joined_at", by[TOPIC])

    def test_a_question_has_its_times_and_where_it_belongs(self):
        self.topic()
        self.question(status="answered", answered_at=T0 + 30, answers={"q1": {"answers": ["A"]}})
        self.question("tok-two-000001", created_at=T0 + 5)
        self.sync.push_once(T0 + 40)
        asked = {item["key"]: item["data"] for item in self.web.items("question")}
        self.assertEqual((asked[TOKEN]["asked_at"], asked[TOKEN]["answered_at"], asked[TOKEN]["project_key"], asked[TOKEN]["topic_number"]),
                         (T0, T0 + 30, "", 7))
        self.assertEqual((asked["tok-two-000001"]["asked_at"], asked["tok-two-000001"]["answered_at"]), (T0 + 5, None))

    def test_a_closed_question_says_when_and_how_it_stopped_waiting(self):
        """2026-11-19 T550: question records had no answer time, every pushed ``answered_at`` was null, and the page's 专项报告
        drew all 59 questions of a project as waiting until now (等你 31 小时) while the board said none waited. Each closed
        question now says when (``closed_at``) and how (``closed_by``); a record from before the stamp takes its answer's
        follow-up task, the lead's triage move or the question that folded it."""
        self.topic(status="waiting_user")
        write_job(self.state, "answer-tok-old-0001", topic_id=TOPIC, chat_id=CHAT, job_id=8, status="done",
                  message_id="answer-tok-old-0001", created_at="2026-11-19T01:00:00Z")
        self.question("tok-old-0001", status="answered", answers={"q1": {"answers": ["A"]}}, continuation_job_id=8)
        self.question("tok-new-0001", status="answered", answers={"q1": {"answers": ["B"]}}, answered_at=T0 + 50, response_kind="feedback")
        self.question("tok-lead-001", status="answered", answers={"q1": {"answers": ["A"]}},
                      triage={"state": "answered_by_lead", "at": T0 + 70, "by": "lead"})
        self.question("tok-skip-001", status="answered", answers={"q1": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}}, answered_at=T0 + 80)
        self.question("tok-fold-001", status="superseded", superseded_by="tok-open-001")
        self.question("tok-open-001", created_at=T0 + 90)
        self.question("tok-stale-01", status="cancelled", cancel_reason="stale", cancelled_at=T0 + 100)
        self.sync.push_once(T0 + 200)
        by = {item["key"]: item["data"] for item in self.web.items("question")}
        old = epoch_seconds("2026-11-19T01:00:00Z")
        self.assertEqual({key: (row["state"], row["closed_at"], row["closed_by"], row["answered_at"]) for key, row in by.items()}, {
            "tok-old-0001": ("answered", old, "owner", old),
            "tok-new-0001": ("answered", T0 + 50, "feedback", T0 + 50),
            "tok-lead-001": ("answered", T0 + 70, "lead", T0 + 70),
            "tok-skip-001": ("answered", T0 + 80, "skipped", T0 + 80),
            "tok-fold-001": ("cancelled", T0 + 90, "superseded", None),
            "tok-open-001": ("open", None, "", None),
            "tok-stale-01": ("cancelled", T0 + 100, "stale", None),
        })

    def test_the_memory_and_library_of_a_project_say_who_wrote_them(self):
        self.thread(OTHER, "对账", 8, status="done")
        mine, _ = project_memory.add(self.state, PID, "用新库", "decision", "owner_answer", now=T0)
        lead, _ = project_memory.add(self.state, PID, "对账一致才算完", "constraint", "lead", now=T0 + 1)
        project_library.add_ref(self.state, PID, "link", "https://example.invalid/doc", name="设计稿", topic_id=OTHER, now=T0 + 2)
        project_library.add_ref(self.state, PID, "path", "/x/shot.png", name="shot.png", now=T0 + 3)
        self.sync.push_once(T0 + 5)
        data = self.web.items("project")[-1]["data"]
        rows = {row["id"]: row for row in data["memory"]}
        self.assertEqual((rows[mine["id"]]["said_by"], rows[lead["id"]]["said_by"], rows[mine["id"]]["origin"]), ("你说的", "它总结的", {"at": T0}))
        self.assertEqual([(row["kind"], row.get("topic_number"), row["name"]) for row in data["library"]], [("link", 8, "设计稿"), ("image", None, "shot.png")])
        self.assertEqual(data["started_at"], self.record["created_at"])


class LeadTest(WebCase):
    """The lead's conversation is a topic of kind lead; the page can speak to it."""

    def history(self, *lines):
        for role, text, message in lines:
            append_history(self.state / "topics" / LEAD / "history.jsonl", role, text, {"message_id": message})

    def conversation(self):
        now = time.time()
        self.lead_job("lead-1", job_id=1, status="done", message_id="om_l1", timings={"backend_started": now - 100})
        self.lead_job("lead-2", job_id=2, status="done", message_id=projects.wake_id(PID, 1), timings={"backend_started": now - 50})
        self.history(("user", "把页面改版做了", "om_l1"), ("assistant", "好，我先拆一下", ""),
                     ("user", project_lead_wake(), projects.wake_id(PID, 1)), ("assistant", "已派出 T831", ""))
        self.thread(OTHER, "页面改版", 831, status="running")
        return now

    def test_the_lead_is_a_topic_of_kind_lead_with_the_owners_words_and_its_answers(self):
        now = self.conversation()
        project_lead.add_receipt(self.state, PID, "thread_end", "T831 的任务 #831 完成：改完了", thread=OTHER, at=now)
        project_lead.add_receipt(self.state, PID, "stalled", "专项还有要做的事", at=now)
        project_lead.add_receipt(self.state, PID, "owner_said", "主人在 T831 里说：好", thread=OTHER, at=now)
        self.sync.push_once(T0)
        by = {(item["type"], item["key"]): item["data"] for item in self.web.items()}
        topic = by[("topic", LEAD)]
        self.assertEqual((topic["kind"], topic["title"], topic["project_key"], topic["number"], topic["unseen"], topic["round"]),
                         ("lead", "负责人", PID, 0, False, 2))
        turns = sorted((item["data"] for item in self.web.items("turn") if item["data"]["topic_key"] == LEAD), key=lambda turn: turn["seq"])
        self.assertEqual([(turn["role"], turn["body"]) for turn in turns if turn["role"] != "event"],
                         [("owner", "把页面改版做了"), ("agent", "好，我先拆一下")])
        events = [turn for turn in turns if turn["role"] == "event"]
        self.assertEqual(sorted(turn["body"] for turn in events), ["T831 做完了", "负责人派出了 T831 页面改版"])
        started = next(turn for turn in events if turn.get("started_topics"))["started_topics"]
        self.assertEqual(started, [{"key": OTHER, "number": 831, "title": "页面改版", "harness": "claude"}])
        self.assertEqual([turn["seq"] for turn in turns], list(range(1, len(turns) + 1)))
        self.assertNotIn(("topic", OTHER + "x"), by)

    def test_a_wake_up_reply_the_lead_meant_for_the_owner_is_its_answer(self):
        self.conversation()
        with mock.patch.object(app_web.project_loop, "reported", return_value=True):
            self.sync.push_once(T0)
        bodies = [item["data"]["body"] for item in self.web.items("turn") if item["data"]["role"] == "agent"]
        self.assertIn("已派出 T831", bodies)
        agent = next(item["data"] for item in self.web.items("turn") if item["data"]["body"] == "已派出 T831")
        self.assertEqual([row["number"] for row in agent["started_topics"]], [831])
        self.assertFalse([item for item in self.web.items("turn") if item["data"]["role"] == "event" and "派出" in item["data"]["body"]])

    def test_a_lead_is_not_pushed_without_projects_and_not_before_it_has_spoken(self):
        self.sync.push_once(T0)
        self.assertEqual([item["key"] for item in self.web.items("topic")], [])
        self.conversation()
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.sync = WebSync(self.runtime, self.env)
            self.sync.client.opener = http_upstream.opener(direct=True)
            self.sync.push_once(T0 + 1)
        self.assertNotIn("lead", [item["data"]["kind"] for item in self.web.items("topic")])
        self.assertFalse([item for item in self.web.items("turn") if item["data"]["role"] == "event"])

    def test_a_running_lead_turn_is_looked_at_every_second_and_is_a_running_turn(self):
        now = self.conversation()
        self.lead_job("lead-3", job_id=3, status="running", message_id="om_l3", timings={"backend_started": now - 5})
        self.history(("user", "再加一个对账", "om_l3"))
        self.sync.push_once(T0)
        self.assertEqual(self.web.items("turn")[-1]["data"]["status"], "running")
        self.assertEqual(self.web.items("turn")[-1]["data"]["topic_key"], LEAD)
        self.assertEqual(self.sync.push_once(T0 + 1), 0)

    def test_say_to_the_lead_is_the_owners_reply_to_the_project_card(self):
        self.conversation()
        self.env.topics.apps = {"claude": self.app}
        self.web.op("say", 1, "op-l1", LEAD, text="先别碰数据库")
        self.sync.inbox_once()
        self.assertEqual(self.app.queued, [(LEAD, "web-op-l1", "先别碰数据库")])
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-l1", "done")])
        self.assertEqual(message_topic(self.state, CHAT, "web-op-l1"), LEAD)
        self.assertTrue(web_sync.is_web_message("web-op-l1"))

    def test_say_to_a_running_lead_is_a_supplement(self):
        self.env.topics.apps = {"claude": self.app}
        self.jobs[LEAD] = [SimpleNamespace(job_id=4)]
        self.web.op("say", 1, "op-l2", LEAD, text="补一句")
        self.sync.inbox_once()
        self.assertEqual((self.app.steered, self.app.queued), ([(4, "web-op-l2", "补一句")], []))

    def test_say_to_the_lead_of_a_finished_project_or_without_projects_fails_with_a_reason(self):
        self.env.topics.apps = {"claude": self.app}
        projects.update(self.state, PID, lambda record: record.update(state="done"))
        self.web.op("say", 1, "op-l3", LEAD, text="还在吗")
        self.sync.inbox_once()
        self.assertEqual((self.web.acks[-1]["state"], "已经结束" in self.web.acks[-1]["message"]), ("failed", True))
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            self.web.op("say", 2, "op-l4", LEAD, text="还在吗")
            self.sync.inbox_once()
        self.assertEqual((self.web.acks[-1]["state"], self.app.queued), ("failed", []))

    def test_forget_voids_one_memory_entry_like_the_card_does(self):
        mine, _ = project_memory.add(self.state, PID, "数据库先别动，等对账脚本出来以后再说，这一条写得很长很长很长很长很长很长很长", "constraint", "owner")
        self.web.op("forget", 1, "f-1", project=PID, entry_id=mine["id"])
        self.web.op("forget", 2, "f-2", project=PID, entry_id=mine["id"])
        self.web.op("forget", 3, "f-3", project=PID, entry_id="nope")
        self.web.op("forget", 4, "f-4", project="zzzzzz999999", entry_id=mine["id"])
        self.sync.inbox_once()
        states = {row["op_id"]: (row["state"], row["message"]) for row in self.web.acks}
        self.assertEqual(states["f-1"], ("done", "已作废：" + mine["text"][:40]))
        self.assertEqual([states[name][0] for name in ("f-2", "f-3", "f-4")], ["failed"] * 3)
        self.assertEqual(project_memory.live(self.state, self.reload()), [])
        self.assertEqual(len(project_memory.tombstones(self.state, self.reload())), 1)

    def test_forget_of_another_chats_project_fails(self):
        mine, _ = project_memory.add(self.state, PID, "x", "fact", "owner")
        projects.update(self.state, PID, lambda record: record.update(chat_id="oc_other"))
        self.web.op("forget", 1, "f-5", project=PID, entry_id=mine["id"])
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "failed")
        self.assertEqual(len(project_memory.live(self.state, self.reload())), 1)


def project_lead_wake():
    from covey.app.project_loop import WAKE_TEXT
    return WAKE_TEXT


class ProgressAndAnswersTest(WebCase):
    """Data contract v4: topic.data ``progress`` (core/progress.py) and turn.data ``answers`` (from the question records)."""

    def by(self):
        return {(item["type"], item["key"]): item["data"] for item in self.web.items()}

    def test_a_running_topic_carries_its_progress_and_an_idle_one_none(self):
        self.topic(status="running", lines=(("user", "做一下"),))
        steps = [{"title": "读", "state": "done"}, {"title": "改", "state": "done"}, {"title": "测", "state": "doing"}]
        times = {"读": {"started": T0 - 1200, "ended": T0 - 600}, "改": {"ended": T0}, "测": {"started": T0}}
        write_job(self.state, f"k-{TOPIC[:4]}", phase="改卡片", steps=steps, step_times=times,
                  timings={"backend_requested": T0 - 1200}, last_activity_at=T0 - 120, last_activity="在跑测试")
        self.sync.push_once(T0)
        found = self.by()[("topic", TOPIC)]["progress"]
        self.assertEqual(found, {"phase": "改卡片", "plan_done": 2, "plan_total": 3, "last_activity_at": int(T0 - 120),
                                 "last_activity": "在跑测试", "eta_seconds": 600, "slow_step": None,
                                 "maybe_stuck": False})
        self.assertEqual(self.sync.push_once(T0 + 1), 0)  # To the minute: not pushed again a second later.
        write_job(self.state, f"k-{TOPIC[:4]}", status="done")
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 2)
        self.assertNotIn("progress", [item for item in self.web.items("topic") if item["key"] == TOPIC][-1]["data"])

    def test_the_turn_that_answered_a_question_card_says_what_was_chosen(self):
        self.topic(lines=(("user", "你好"), ("assistant", "选哪个？")))
        self.question(status="answered", questions=[
            {"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
            {"id": "q2", "question": "要不要部署？", "options": [{"label": "要"}, {"label": "不要"}]},
            {"id": "q3", "question": "测哪些？", "multiple": True, "options": [{"label": "单测"}, {"label": "回放"}]}],
            answers={"q1": {"answers": ["B"]}, "q2": {"answers": [app_web.SKIPPED]}, "q3": {"answers": ["单测", "回放"]}})
        write_job(self.state, "k-answer", topic_id=TOPIC, chat_id=CHAT, job_id=9, status="done", message_id="answer-" + TOKEN)
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", "用哪个库？ → B", {"message_id": "answer-" + TOKEN})
        append_history(history, "assistant", "好，用 B", {"message_id": "om_reply"})
        self.sync.push_once(T0)
        by = self.by()
        self.assertEqual(by[("turn", f"{TOPIC}:3")]["answers"], [
            {"question": "用哪个库？", "choice": "B"}, {"question": "要不要部署？", "choice": "没答"},
            {"question": "测哪些？", "choice": "单测、回放"}])
        for seq in (1, 2, 4):  # Only the owner's turn of the answer.
            self.assertNotIn("answers", by[("turn", f"{TOPIC}:{seq}")])

    def test_feedback_instead_of_answers_is_not_answered(self):
        self.question(status="answered", response_kind="feedback", answers={"q1": {"answers": ["用户选择反馈问题"]}})
        self.assertEqual(app_web.answer_rows(read_object(self.state / "questions" / f"{TOKEN}.json")),
                         [{"question": "用哪个库？", "choice": "没答"}])


class SpeakerTest(WebCase):
    """Data contract v7: an owner line says who said it (``by``, ``via``) and the words the page shows (``display``), as the
    cards do (T550, 2026-11-19: the page showed "用户补充回答：", "用户选择不回答此题（不表示同意或授权）" and "【主人编辑了刚才的消息】…"
    as the owner's words, and the lead's hand-ons and answers as his)."""

    def by(self):
        return {(item["type"], item["key"]): item["data"] for item in self.web.items()}

    def owner_turns(self, topic=TOPIC):
        turns = sorted((item["data"] for item in self.web.items("turn") if item["data"]["topic_key"] == topic), key=lambda turn: turn["seq"])
        return [turn for turn in turns if turn["role"] == "owner"]

    def answered(self, given, token=TOKEN, supplement="", **fields):
        """A question of two answered with ``given`` (q1, q2), its line in the history as the answer's path writes it, the
        task it started and that task's reply."""
        questions = [{"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
                     {"id": "q2", "question": "要不要部署？", "options": [{"label": "要"}, {"label": "不要"}]}]
        answers = {"q1": {"answers": [given[0]]}, "q2": {"answers": [given[1]]}}
        self.question(token, status="answered", questions=questions, answers=answers, created_at=T0 - 60,
                      **({"supplement": supplement} if supplement else {}), **fields)
        record = read_object(self.state / "questions" / f"{token}.json")
        _, text, _ = answer_words.answer_message(record, answers, "", [])
        write_job(self.state, "k-answer-" + token, topic_id=TOPIC, chat_id=CHAT, job_id=20, status="done", message_id="answer-" + token)
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", text)
        append_history(history, "assistant", "好，用 B", {"message_id": "om_reply"})
        return text

    def test_an_answer_shows_its_choices_without_the_bridges_headings_and_is_the_owners(self):
        self.topic(lines=(("user", "你好"), ("assistant", "选哪个？")))
        text = self.answered(["B", answer_words.SKIPPED])
        self.sync.push_once(T0)
        owner = self.owner_turns()[-1]
        self.assertEqual(owner["body"], text)  # The line as kept.
        self.assertEqual((owner["by"], owner["display"]), ("owner", "答了 2 题：B；没答"))
        self.assertEqual(owner["answers"], [{"question": "用哪个库？", "choice": "B"}, {"question": "要不要部署？", "choice": "没答"}])
        self.assertNotIn("用户", owner["display"])
        self.assertEqual(self.owner_turns()[0], {**self.owner_turns()[0], "by": "owner"})
        self.assertNotIn("display", self.owner_turns()[0])  # Words with no heading go as they are.

    def test_every_question_skipped_with_a_note_is_the_note(self):
        self.topic(lines=())
        self.answered([answer_words.SKIPPED, answer_words.SKIPPED], supplement="先说说为什么要换库")
        self.sync.push_once(T0)
        owner = self.owner_turns()[-1]
        self.assertEqual((owner["display"], owner["note"]), ("没答，补充：先说说为什么要换库", "先说说为什么要换库"))

    def test_an_answer_the_lead_or_the_butler_gave_is_theirs_not_the_owners(self):
        self.topic(lines=())
        self.answered(["A", "要"], supplement=web_said.LEAD_NOTE, triage={"state": triage.ANSWERED, "at": T0})
        self.answered(["B", "不要"], token="tok-butler-0001", supplement=web_said.BUTLER_NOTE, answer_event="main-r1")
        self.sync.push_once(T0)
        lead, butler = self.owner_turns()
        self.assertEqual((lead["by"], lead["display"]), ("lead", "答了 2 题：A；要"))
        self.assertNotIn("note", lead)  # The note that says who answered is not words of his.
        self.assertEqual((butler["by"], butler["display"]), ("butler", "答了 2 题：B；不要"))

    def test_feedback_an_edit_and_a_recall_say_what_the_owner_did(self):
        self.topic(lines=())
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", answer_words.FEEDBACK_START + "问题问偏了")
        append_history(history, "user", source_watch.edited_text("改成用 B 库"), {"message_id": "edit-om_1"})
        append_history(history, "user", source_watch.recalled_text("先别部署"), {"message_id": "recall-om_2"})
        self.sync.push_once(T0)
        feedback, edited, recalled = self.owner_turns()
        self.assertEqual((feedback["by"], feedback["display"]), ("owner", "没答，补充：问题问偏了"))
        self.assertEqual((edited["by"], edited["display"], edited["edited"]), ("owner", "改成用 B 库", True))
        self.assertTrue(edited["body"].startswith("【主人编辑了刚才的消息】"))
        self.assertEqual(recalled["display"], "撤回了刚才那条补充：「先别部署」")

    def test_a_hand_on_is_the_leads_or_the_butlers_own_and_the_owners_words_handed_on_are_his(self):
        self.thread(OTHER, "对账", 8)
        write_json(butler_opened.folder(self.state) / "lead1.json", {"job_key": "lead-9", "chat_id": CHAT, "topic_id": OTHER})
        write_job(self.state, "lead-9", topic_id=LEAD, chat_id=CHAT, job_id=91, status="done", message_id=projects.wake_id(PID, 3))
        write_json(butler_opened.folder(self.state) / "butler1.json", {"job_key": "k-b1", "chat_id": CHAT, "topic_id": OTHER})
        write_job(self.state, "k-b1", topic_id=BUTLER, chat_id=CHAT, job_id=92, status="done", message_id="om_owner1")
        write_json(butler_opened.folder(self.state) / "butler2.json", {"job_key": "k-b2", "chat_id": CHAT, "topic_id": OTHER})
        write_job(self.state, "k-b2", topic_id=BUTLER, chat_id=CHAT, job_id=93, status="done", message_id="lead-x")
        write_job(self.state, "k-o", topic_id=OTHER, chat_id=CHAT, job_id=8, status="done", message_id="main-lead1")
        history = self.state / "topics" / OTHER / "history.jsonl"
        for text, message in (("T538 合进 main 了，部署时带上", "main-lead1"), ("把对账表也导出", "main-butler1"),
                              ("先停一下", "main-butler2"), ("老记录没了", "main-gone"), ("我自己说的", "om_mine")):
            append_history(history, "user", text, {"message_id": message})
        self.sync.push_once(T0)
        said = [(turn["body"], turn["by"], turn.get("via", "")) for turn in self.owner_turns(OTHER)]
        self.assertEqual(said, [("T538 合进 main 了，部署时带上", "lead", ""), ("把对账表也导出", "owner", "butler"),
                                ("先停一下", "butler", ""), ("老记录没了", "owner", ""), ("我自己说的", "owner", "")])

    def test_the_model_is_pushed_by_its_readable_name(self):
        self.topic()
        write_job(self.state, f"k-{TOPIC[:4]}", model="router/claude-opus-5-5")
        self.sync.push_once(T0)
        topic = self.by()[("topic", TOPIC)]
        self.assertEqual((topic["model"], topic["model_name"]), ("router/claude-opus-5-5", "Opus 5.5"))

    def test_a_projects_goal_is_the_one_its_lead_wrote_and_none_before(self):
        self.sync.push_once(T0)
        self.assertEqual(self.by()[("project", PID)]["goal"], "")  # Not the words it was opened with.
        projects.update(self.state, PID, lambda record: record.update(lead_goal={"text": "账单迁完、对账一致", "at": T0}))
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.web.items("project")[-1]["data"]["goal"], "账单迁完、对账一致")


def at(seconds):
    """T0 + ``seconds`` as a history line's ``time`` and a record's ``updated_at`` write it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(T0 + seconds))


def ran(job_id, message, start, end, **fields):
    """A job record that ran from T0 + ``start`` and was last written at T0 + ``end``."""
    return {"job_id": job_id, "message_id": message, "status": "done", "timings": {"backend_started": T0 + start}, "updated_at": at(end),
            **fields}


class TurnJobsTest(WebCase):
    """Every answer turn is paired with its task (Collector.pair): 2026-11-19 T550 found 97 of the Covey project's 127 answer turns
    without one, so with no token, process or plan on the page (the project's tile, the report's token table, a turn's 过程)."""

    def paired(self, rows, records, questions=()):
        return [(row["role"], row.get("content"), job and job["job_id"]) for row, job, _ in app_web.Collector.pair(rows, records, questions)]

    def test_a_supplement_sent_into_the_running_task_leaves_that_task_to_the_answer(self):
        # T542: the lead's 补充 (main-…, merged into the running task: no record of its own) came after the answer that started it.
        self.question(status="answered", answers={"q1": {"answers": ["A"]}}, created_at=T0 - 50)
        rows = [{"role": "user", "content": "做菜单卡", "message_id": "main-1", "time": at(-100)},
                {"role": "assistant", "content": "用哪个库？", "time": at(-60)},
                {"role": "user", "content": answer_words.ANSWER_START + "\n用哪个库？\nA", "time": at(-40)},
                {"role": "user", "content": "负责人补充：顺便改看板", "message_id": "main-2", "time": at(-30)},
                {"role": "assistant", "content": "都改好了", "time": at(-5)}]
        records = [ran(1, "main-1", -100, -40, status="continued"), ran(2, "answer-" + TOKEN, -40, -4),
                   ran(3, "main-2", -30, -30, status="cancelled", timings={})]  # The supplement's own record, merged before it ran.
        questions = [read_object(self.state / "questions" / f"{TOKEN}.json")]
        self.assertEqual(self.paired(rows, records, questions), [
            ("user", "做菜单卡", 1), ("assistant", "用哪个库？", 1), ("user", rows[2]["content"], 2),
            ("user", "负责人补充：顺便改看板", None),  # The line itself carries no task: the answers stay on the answer's line.
            ("assistant", "都改好了", 2)])

    def test_answers_in_a_row_each_take_the_task_that_ran_then(self):
        # T531: three automatic retries after a rate limit, then a stop: four answer lines with no user line between.
        rows = [{"role": "user", "content": "开始第 0 步", "message_id": "main-1", "time": at(0)},
                {"role": "assistant", "content": "限流", "time": at(780)},
                {"role": "assistant", "content": "限流", "time": at(845)},
                {"role": "assistant", "content": "限流", "time": at(1035)},
                {"role": "assistant", "content": "任务 #4 已停止", "time": at(3257)}]
        records = [ran(1, "main-1", 0, 830, status="failed"), ran(2, "auto-retry-1-1", 830, 1020, status="failed"),
                   ran(3, "auto-retry-2-2", 1019, 1800, status="failed"), ran(4, "om_resume", 1800, 3257, status="cancelled")]
        self.assertEqual([job for _, _, job in self.paired(rows, records)], [1, 1, 2, 3, 4])

    def test_a_line_written_with_its_task_has_it(self):
        rows = [{"role": "user", "content": "做一下", "message_id": "om_1", "time": at(0)},
                {"role": "assistant", "content": "好了", "job_id": 7, "time": at(50)}]
        self.assertEqual(self.paired(rows, [ran(6, "om_1", 0, 60), ran(7, "om_other", 10, 60)])[1][2], 7)

    def test_an_answer_written_before_the_queued_task_started_is_the_running_ones(self):
        # T488 11-16: the owner's next message was queued behind the running task; its line came before that task's answer.
        rows = [{"role": "user", "content": "跑一下", "message_id": "answer-x", "time": at(0)},
                {"role": "user", "content": "我去睡了，接着优化", "message_id": "om_2", "time": at(3000)},
                {"role": "assistant", "content": "两层都出来了", "time": at(4000)},
                {"role": "assistant", "content": "优化好了", "time": at(9000)}]
        records = [ran(1, "answer-x", 0, 4006), ran(2, "om_2", 4006, 9005)]
        self.assertEqual([job for _, _, job in self.paired(rows, records)], [1, 2, 1, 2])

    def test_a_task_interrupted_and_retried_is_part_of_the_turn_the_retry_answers(self):
        # T488 11-18: task 4598 spent 137M tokens, the bridge's restart interrupted it, and the automatic retry 4624 answered.
        self.topic(lines=())
        with mock.patch("covey.core.jobs.utc_now_iso", return_value=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 - 600))):
            write_job(self.state, f"k-{TOPIC[:4]}", message_id="om_go", status="interrupted", timings={"backend_started": T0 - 900})
        write_job(self.state, "k-retry", topic_id=TOPIC, chat_id=CHAT, job_id=8, status="done", message_id="auto-retry-7-1",
                  retry_of="om_go", timings={"backend_started": T0 - 300})
        history = self.state / "topics" / TOPIC / "history.jsonl"
        append_history(history, "user", "跑对比", {"message_id": "om_go"})
        append_history(history, "assistant", "对比跑完了")
        for module in (app_web, web_turns):
            patch = mock.patch.object(module, "load_trace", side_effect=lambda state, record: dict(
                snapshot_of(T0), input_tokens=1000 * record["job_id"], output_tokens=10 * record["job_id"], cache_read_tokens=900 * record["job_id"]))
            patch.start()
            self.addCleanup(patch.stop)
        self.sync.push_once(T0)
        turn = {item["key"]: item["data"] for item in self.web.items("turn")}[f"{TOPIC}:2"]
        self.assertEqual((turn["job_id"], turn["usage"]), ("8", {"input": 15000, "output": 150, "cache_read": 13500, "total": 15150}))

    def test_every_kept_turn_has_its_figures_and_its_task_and_the_newest_their_process(self):
        self.topic(lines=())
        history = self.state / "topics" / TOPIC / "history.jsonl"
        for number in range(1, 16):  # 30 turns: the oldest ten are past EXTRA_TURNS.
            write_job(self.state, f"k-{number}", topic_id=TOPIC, chat_id=CHAT, job_id=100 + number, status="done", message_id=f"om_{number}")
            append_history(history, "user", f"第 {number} 件", {"message_id": f"om_{number}"})
            append_history(history, "assistant", f"第 {number} 件做完了")
        for module in (app_web, web_turns):
            patch = mock.patch.object(module, "load_trace", side_effect=lambda *args: snapshot_of(T0))
            patch.start()
            self.addCleanup(patch.stop)
        self.sync.push_once(T0)
        answers = sorted((item["data"] for item in self.web.items("turn") if item["data"]["role"] == "agent"), key=lambda data: data["started_at"])
        self.assertEqual([data["job_id"] for data in answers], [str(100 + number) for number in range(1, 16)])
        self.assertTrue(all(data["usage"]["total"] == 1500 for data in answers))
        self.assertEqual([("steps" in data) for data in answers], [False] * 5 + [True] * 10)
        self.assertEqual(self.sync.push_once(T0 + 1), 0)  # Read once: nothing changes the next second.

    def test_an_answer_turn_with_no_task_record_is_logged_once(self):
        self.topic(lines=(("user", "你好"), ("assistant", "好的")))
        write_job(self.state, f"k-{TOPIC[:4]}", message_id="om_gone", timings={"backend_started": T0 + 3600})
        self.sync.push_once(T0)
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        said = [line for line in self.logs if "no task record" in line]
        self.assertEqual(said, [f"web sync topic {TOPIC[:12]}: 1 of 1 answer turns have no task record"])

    def test_the_answer_line_keeps_its_task(self):
        history = self.state / "h.jsonl"
        append_history(history, "assistant", "好了", {"job_id": 42})
        self.assertEqual(json.loads(history.read_text())["job_id"], 42)


class ModelNameTest(unittest.TestCase):
    def test_a_recorded_model_reads_as_one_name_everywhere(self):
        for model, name in (("opus", "Opus"), ("claude-opus-5", "Opus 5"), ("claude-opus-5-5", "Opus 5.5"),
                            ("acme/team/claude-opus-5-5", "Opus 5.5"), ("claude-opus-4-7[1m]", "Opus 4.7"),
                            ("acme/gpt-6-astra", "GPT-6 Astra"), ("gpt-6-astra_reasoning_xhigh", "GPT-6 Astra"),
                            ("gpt56_sol_reasoning_max", "GPT-5.6 Sol"), ("ali-deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
                            ("default", ""), ("", ""), (None, "")):
            self.assertEqual(model_names.model_name(model), name, model)


class OffTest(WebCase):
    def test_forget_is_one_of_the_kinds_and_the_off_switch_still_means_no_request(self):
        self.assertIn("forget", web_sync.KINDS)
        with mock.patch.dict(os.environ, {web_sync.ENV: "0"}):
            self.assertEqual(self.sync.threads(), ())
        self.assertEqual(self.web.seen, [])


if __name__ == "__main__":
    unittest.main()

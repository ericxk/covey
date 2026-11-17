"""网页工作台同步 (app/web_sync.py, app/web_ops.py, core/web_sync.py, COVEY_WEB=1): against a fake /openapi of the page (core/serving.py's
loopback server): the items pushed are right and only the changed ones go again, batches are cut, the page's say / answer / stop / reopen are
each done once (an op_id seen again is only acknowledged), a failing page is backed off and does not stop the thread, the key is in no log line, and
with the switch off there is no thread and no request."""
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

from covey.app import web_ops, web_sync as app_web
from covey.app.web_sync import WebError, WebSync
from covey.core import http_upstream, projects, web_sync
from covey.core.history_storage import append_history
from covey.core.serving import LoopbackServer
from covey.core.state import write_json
from covey.core.topics import message_topic

from test_project_lead import CHAT, PID, Case

@dataclasses.dataclass
class Ctx:
    topic_id: str = ""


T0 = time.time()
KEY = "fake-key-do-not-log-0123456789"
TOPIC = "c" * 32
OTHER = "d" * 32
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
        self.queued, self.steered, self.answers, self.stopped = [], [], [], []
        self.harness = SimpleNamespace(context=object())
        self.submission = SimpleNamespace(event_context=self.event_context, enqueue_chat_job=self.enqueue)
        self.steering = SimpleNamespace(steer_stage=lambda job: "running", deliver_steer=self.deliver)
        self.questions = SimpleNamespace(submit_answer=lambda ctx, chat, token, fields, event: self.answers.append((ctx.topic_id, token, fields, event)))
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
        return self.app, Ctx(topic), {}

    def topic(self, topic=TOPIC, number=7, title="整理账单", status="done", lines=(("user", "你好"), ("assistant", "好的"))):
        write_topic(self.state, topic_id=topic, chat_id=CHAT, harness_id="claude", topic_number=number, origin={"title": title},
                    workspace_name="ws1")
        write_job(self.state, f"k-{topic[:4]}", topic_id=topic, chat_id=CHAT, job_id=number, status=status, model="opus", card_message_id="om_card")
        for role, text in lines:
            append_history(self.state / "topics" / topic / "history.jsonl", role, text, {"message_id": "m" + text})

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
        self.assertEqual(self.sync.push_once(T0 + 2), 1)
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

    def test_the_butler_topic_and_other_chats_are_not_pushed(self):
        self.topic()
        write_topic(self.state, topic_id=OTHER, chat_id="oc_other", harness_id="claude", topic_number=9)
        write_job(self.state, "k-other", topic_id=OTHER, chat_id="oc_other", job_id=9, status="done")
        self.sync.push_once(T0)
        self.assertNotIn(OTHER, [item["key"] for item in self.web.items("topic")])


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

    def test_say_to_a_topic_waiting_for_an_answer_is_refused(self):
        self.topic()
        self.question()
        self.web.op("say", 1, "op-3", TOPIC, text="我来说")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")
        self.assertIn("答题", self.web.acks[0]["message"])
        self.assertEqual(self.app.queued, [])

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


class SwitchTest(WebCase):
    def test_off_means_no_thread_and_no_request(self):
        with mock.patch.dict(os.environ, {web_sync.ENV: "0"}):
            self.assertEqual(self.sync.threads(), ())
        with mock.patch.dict(os.environ):
            os.environ.pop(web_sync.ENV)
            self.assertEqual(self.sync.threads(), ())
        self.assertEqual(self.web.seen, [])

    def test_on_there_are_two_threads(self):
        self.assertEqual([name for name, _ in self.sync.threads()], ["web-push", "web-inbox"])

    def test_the_bridge_lists_the_threads_only_with_the_switch(self):
        from covey.app.unified import UnifiedBridgeApp
        self.assertIn("web_sync", UnifiedBridgeApp.PARTS)


if __name__ == "__main__":
    unittest.main()

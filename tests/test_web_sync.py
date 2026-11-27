"""网页工作台同步 (app/web_sync.py, app/web_ops.py, core/web_sync.py, COVEY_WEB=1): against a fake /openapi of the page (core/serving.py's
loopback server): the items pushed are right and only the changed ones go again, batches are cut, the page's say / answer / stop / reopen are
each done once (an op_id seen again is only acknowledged), a failing page is backed off and does not stop the thread, the key is in no log line, and
with the switch off there is no thread and no request. The butler's conversation is one topic of ``kind`` "butler" and a say to it is the owner's message at the top
level of the private chat (the id ``web-<op_id>`` is quoted, replied to and reacted on by nothing); ``seen`` marks a topic's result as read."""
from __future__ import annotations

import base64
import dataclasses
import datetime
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import write_job, write_topic

from covey.app import answers as answer_words, card_lanes, source_watch, web_ops, web_said, web_sync as app_web, web_collect as app_collect, web_pairing as app_pairing
from covey.app.web_sync import WebError, WebSync
from covey.app import project_lead, web_turns
from covey.core import butler_opened, collab, http_upstream, model_names, placement, project_checklist, project_library, project_memory, project_proposals, projects, triage, turn_source, web_sync
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
        self.waited = None  # True: a page that knows the inbox's ``wait`` (data contract: docs/web.md 更快的取数); None: an older one.
        self.limits = None  # A dict: a page of data contract v44, which says what it takes in its upsert answers; None: an older one.
        self.uploads = {}  # id -> (meta, [part bytes]): the files the owner uploaded on the page (data contract v21).
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
            if self.limits is not None:
                answer["limits"] = self.limits
        elif handler.path.startswith("/sync/inbox"):
            after = int(handler.path.split("after=")[1].split("&")[0])
            answer = {"items": [row for row in self.outbox if row["seq"] > after and row.get("state", "new") == "new"]}
            if self.waited is not None:
                answer["waited"] = self.waited
        elif handler.path.startswith("/sync/upload"):
            query = dict(part.split("=", 1) for part in handler.path.split("?", 1)[1].split("&"))
            found = self.uploads.get(query["id"])
            if found is None:
                status, answer = 404, {"ok": False}
            elif handler.path.startswith("/sync/upload/part"):
                answer = {"n": int(query["n"]), "data": base64.b64encode(found[1][int(query["n"])]).decode()}
            else:
                answer = {**found[0], "id": query["id"], "parts": len(found[1])}
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
        self.answered_by = []  # Who each answer of ``answers`` was by (app/questions.py owner_submitted).
        self.questions = SimpleNamespace(submit_answer=lambda ctx, chat, token, fields, event, by=None: (
                                             self.answered_by.append(by), self.answers.append((ctx.topic_id, token, fields, event))),
                                         paint_question_state=lambda ctx, record: self.painted.append((record["token"], record["status"])),
                                         set_aside=self.set_aside, restore=self.restore)
        self.lesser = []  # (what, token, by, event) of 不处理 and 撤销.
        self.controls = SimpleNamespace(cancel_reply_job_from_card=lambda ctx, action, chat, message, others=0: self.stopped.append(action))

    def set_aside(self, ctx, chat, token, event, *, by=""):
        self.lesser.append(("aside", token, by, event))
        return True

    def restore(self, ctx, chat, token, event):
        self.lesser.append(("restore", token, "", event))
        return True

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
        self.assertEqual((config.push_seconds, config.poll_seconds), (1.0, 0.5))

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

    def test_the_project_avatar_reuses_the_feishu_assignment_after_a_rename(self):
        from covey.core import avatars

        write_json(self.state / avatars.FILE, {"leads": {CHAT: {PID: "sea-whale"}}})
        self.sync.push_once(T0)
        self.assertEqual(self.web.items("project")[-1]["data"]["avatar"], "sea-whale")
        projects.update(self.state, PID, lambda record: record.update(name="换个专项名字"))
        self.sync.push_once(T0 + 1)
        item = self.web.items("project")[-1]
        self.assertEqual((item["data"]["title"], item["data"]["avatar"]), ("换个专项名字", "sea-whale"))
        self.assertEqual(item["rev"], web_sync.rev(item["data"]))
        self.assertEqual(avatars.lead_name(self.state, CHAT, PID), "sea-whale")

    def test_an_avatar_first_seen_on_the_web_is_kept_for_feishu(self):
        from covey.core import avatars

        self.assertFalse((self.state / avatars.FILE).exists())
        self.sync.push_once(T0)
        name = self.web.items("project")[-1]["data"]["avatar"]
        self.assertIn(name, avatars.POOL)
        self.assertEqual(avatars.lead_name(self.state, CHAT, PID), name)
        self.assertEqual(read_object(self.state / avatars.FILE)["leads"][CHAT][PID], name)
        self.assertEqual(self.sync.push_once(T0 + 1), 0)

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
        with mock.patch.object(app_collect, "load_trace", side_effect=lambda *args: notes):
            self.sync.push_once(T0)
            self.assertEqual(self.sync.push_once(T0 + 1), 0)
            notes["steps"]["b"] = {"title": "进展说明", "detail": "再看了配置", "status": "note"}
            notes["step_order"].append("b")
            self.assertEqual(self.sync.push_once(T0 + 2), 1)
        live = self.web.items("turn")[-1]
        self.assertEqual((live["key"], live["data"]["status"]), (f"{TOPIC}:2", "running"))
        self.assertEqual(live["data"]["body"], "先看了日志\n\n再看了配置")

    def test_a_task_between_its_answer_line_and_its_end_is_neither_undelivered_nor_a_second_running_turn(self):
        # T588 #5309 (2026-11-20 16:29): app/worker.py writes the answer line once the result went out, then the record; a look in
        # between pushed the answer as 没发出去 (red) and the same task again as a running turn after it.
        self.topic(status="running", lines=(("user", "做一下"),))
        write_job(self.state, f"k-{TOPIC[:4]}", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="running", model="opus",
                  card_message_id="om_card", result_retry_policy="saved_delivery_only", verified_reply_authorized=True,
                  verified_reply_state="authorized", delivery_ok=False, execution_status="done", final_text="好了")
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "assistant", "好了", {"job_id": 7})
        self.sync.push_once(T0)
        turns = {each["key"]: each["data"] for each in self.web.items("turn")}
        self.assertEqual(sorted(turns), [f"{TOPIC}:1", f"{TOPIC}:2"])
        self.assertEqual((turns[f"{TOPIC}:2"]["status"], turns[f"{TOPIC}:2"].get("failure")), ("done", None))
        topic = next(each["data"] for each in self.web.items("topic") if each["key"] == TOPIC)
        self.assertEqual(topic["last_seq"], 2)

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
        with mock.patch.object(app_collect, "load_trace", side_effect=lambda *args: notes):
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

    def test_starting_over_pushes_only_what_changed(self):
        """2026-11-19 23:52: after a stuck read the next look pushed all ~7000 items again, minutes in which nothing new went out."""
        self.topic()
        self.assertGreater(self.sync.push_once(T0), 0)
        self.sync.start_over()
        self.assertEqual(self.sync.push_once(T0 + 1), 0)

    def test_a_full_pass_goes_a_slice_a_step_and_the_hot_topics_every_step(self):
        """T554, 2026-11-19: one look of every topic took minutes and pushed only at its end, with what it had read before the owner's
        answer. A pass now takes PASS_SLICE topics a step, the running ones after them every step."""
        quiet = [f"{n:02d}" * 16 for n in range(10, 16)]
        for number, topic in enumerate(quiet, 20):
            self.topic(topic, number=number, title=f"话题{number}")
        self.topic(status="running", lines=(("user", "做一下"),))
        with mock.patch.object(app_collect, "PASS_SLICE", 2, create=True):
            self.sync.push_once(T0)
            first = {item["key"] for item in self.web.items("topic")}
            self.assertIn(TOPIC, first)
            self.assertEqual(len(first & set(quiet)), 2)
            write_job(self.state, f"k-{TOPIC[:4]}", status="done")
            self.sync.push_once(T0 + 1)
            last = [item for item in self.web.items("topic") if item["key"] == TOPIC][-1]["data"]
            self.assertNotEqual(last["state"], "在做")
            for step in range(2, 6):
                self.sync.push_once(T0 + step)
        self.assertEqual({item["key"] for item in self.web.items("topic")} & set(quiet), set(quiet))

    def test_the_hot_topics_go_first_and_the_pass_a_step_s_worth_after_them(self):
        """2026-11-20 00:31-00:53: four restarts, each first pass ~20 MB over ~5 minutes; starts and ends reached the page 55-73 s late.
        The hot topics' changes go first every step; what the pass read waits in a backlog and goes a batch (the client's
        batch_bytes) a step after them."""
        quiet = [f"{n:02d}" * 16 for n in range(10, 16)]
        for number, topic in enumerate(quiet, 20):
            self.topic(topic, number=number, title=f"话题{number}")
        self.topic(status="running", lines=(("user", "做一下"),))
        with mock.patch.object(self.sync.client, "batch_bytes", return_value=1):
            self.sync.push_once(T0)
            self.assertEqual(self.web.upserts[0]["items"][0]["key"], TOPIC)
            self.assertLess(len({item["key"] for item in self.web.items("topic")} & set(quiet)), 2)
            write_job(self.state, f"k-{TOPIC[:4]}", status="done")
            self.sync.push_once(T0 + 1)
            last = [item for item in self.web.items("topic") if item["key"] == TOPIC][-1]["data"]
            self.assertNotEqual(last["state"], "在做")
            for step in range(2, 60):
                self.sync.push_once(T0 + step)
        self.assertEqual({item["key"] for item in self.web.items("topic")} & set(quiet), set(quiet))
        self.assertEqual(self.sync.backlog, {})

    def test_a_restart_sends_only_what_changed_since(self):
        """Every restart sent all ~7000 items (~20 MB) again (2026-11-20): what the page took is kept in <state>/web/sent.json."""
        self.topic()
        self.thread(OTHER, "对账", 8, status="done")
        self.assertGreater(self.sync.push_once(T0), 0)
        self.assertTrue((self.state / "web" / "sent.json").exists())
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "user", "再来一句")
        again = WebSync(self.runtime, self.env)
        again.client.opener = http_upstream.opener(direct=True)
        before = len(self.web.items())
        self.assertGreater(again.push_once(T0 + 1), 0)
        self.assertEqual({item["key"] for item in self.web.items()[before:] if item["type"] == "topic"}, {TOPIC})
        other = WebSync(self.runtime, self.env)  # A day after the one that started empty: everything once more.
        other.client.opener = http_upstream.opener(direct=True)
        before = len(self.web.items())
        other.push_once(T0 + 86401)
        self.assertIn(OTHER, {item["key"] for item in self.web.items()[before:] if item["type"] == "topic"})

    def test_what_was_sent_to_another_page_is_not_used(self):
        self.topic()
        self.sync.push_once(T0)
        path = self.state / "web" / "sent.json"
        data = json.loads(path.read_text())
        write_json(path, {**data, "base_url": "http://elsewhere.invalid"})
        again = WebSync(self.runtime, self.env)
        again.client.opener = http_upstream.opener(direct=True)
        self.assertGreater(again.push_once(T0 + 1), 0)

    def test_a_day_on_everything_goes_once_more(self):
        """A page whose database was emptied fills again within a day, without a restart."""
        self.topic()
        self.sync.push_once(T0)
        self.assertEqual(self.sync.push_once(T0 + 61), 0)
        self.assertGreater(self.sync.push_once(T0 + 86401), 0)

    def test_a_topic_whose_only_task_never_started_reads_stopped(self):
        """T561, T562 (2026-11-19): their launch cards closed by a stop, nothing ever ran; the page's 全图 listed them under 空闲."""
        self.topic(status="cancelled", lines=(("user", "修专项右栏"),))
        write_job(self.state, f"k-{TOPIC[:4]}", never_started=True)
        self.sync.push_once(T0)
        data = next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]
        self.assertEqual((data["state"], data["tone"]), ("已停止", "cancelled"))


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
        real = app_collect.Collector.topic_items

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
        with mock.patch.object(app_collect.Collector, "topic_items", topic_items), mock.patch.object(app_web, "STUCK_SECONDS", 0.2), \
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
        real = app_collect.Collector.collect_hot

        def collect(collector, chat, now):
            found = real(collector, chat, now)
            entered.set()
            hang.wait(10)
            return found
        with mock.patch.object(app_collect.Collector, "collect_hot", collect):
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
        self.assertEqual([(r["state"], r["message"][:6]) for r in self.web.acks], [("failed", "补充说明是空"), ("done", "补充说明已交")])
        self.assertEqual([a[2] for a in self.app.answers], [{"skip_0": True, "supplement": "先放一放"}])

    def test_a_note_alone_or_with_some_answers_skips_what_is_left_out(self):
        """T631 (data contract v29): 提交回答 is the one button, a 补充说明 alone skips every question and one with some answers
        the questions left out; a page before v29 sends 先问一句 as ``ask``, which is the note alone."""
        self.topic()
        self.question(questions=[{"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
                                 {"id": "q2", "question": "放哪？", "options": []}])
        self.web.op("answer", 1, "op-4", TOPIC, token=TOKEN, note="先说说 A 和 B 差在哪")
        self.web.op("answer", 2, "op-5", TOPIC, token=TOKEN, answers={}, note="")
        self.web.op("answer", 3, "op-6", TOPIC, token=TOKEN, answers={"q1": "A"}, note="")
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["done", "failed", "failed"])
        self.assertEqual(self.web.acks[0]["message"], "补充说明已交给任务（题目算没答）")
        self.assertIn("不答题时写上补充说明", self.web.acks[1]["message"])
        self.assertIn("每题都要答，还差：放哪？（不答的题写上补充说明就算不回答）", self.web.acks[2]["message"])
        self.assertEqual(self.app.answers[0][2], {"skip_0": True, "skip_1": True, "supplement": "先说说 A 和 B 差在哪"})
        self.app.answers.clear()
        self.question(questions=[{"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
                                 {"id": "q2", "question": "放哪？", "options": []}])
        self.web.op("answer", 4, "op-7", TOPIC, token=TOKEN, answers={"q1": "B"}, note="放哪你定")
        self.web.op("answer", 5, "op-8", TOPIC, token=TOKEN, ask=True, note="B 稳吗？")
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks[3:]], ["done", "done"])
        self.assertEqual(self.web.acks[3]["message"], "答案已交给任务")
        self.assertEqual([a[2] for a in self.app.answers], [{"option_0": "1", "skip_1": True, "supplement": "放哪你定"},
                                                            {"skip_0": True, "skip_1": True, "supplement": "B 稳吗？"}])
        self.assertEqual(self.app.lesser, [])

    def test_each_question_can_be_left_unanswered_or_answered_in_words(self):
        """T617, 2026-11-21: the page's form had only the options, where the card has 不回答此题 for each question and an answer box
        for when no option fits. ``{"skip": true}`` is the first, words that are no option the second."""
        self.topic()
        self.question(questions=[{"id": "q1", "question": "用哪个库？", "options": [{"label": "A"}, {"label": "B"}]},
                                 {"id": "q2", "question": "放哪？", "options": []},
                                 {"id": "q3", "question": "几点？", "options": [{"label": "早上"}]}])
        self.web.op("answer", 1, "op-4", TOPIC, token=TOKEN, answers={"q1": "都不合适，用标准库", "q2": {"skip": True}, "q3": {"skip": True}})
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "done")
        fields = self.app.answers[0][2]
        self.assertEqual(fields, {"answer_0": "都不合适，用标准库", "skip_1": True, "skip_2": True, "supplement": ""})
        from covey.app.answers import SKIPPED, form_answers
        record = read_object(self.state / "questions" / f"{TOKEN}.json")
        self.assertEqual(form_answers(record["questions"], fields),
                         {"q1": {"answers": ["都不合适，用标准库"]}, "q2": {"answers": [SKIPPED]}, "q3": {"answers": [SKIPPED]}})

    def test_the_butlers_proxy_answer_cannot_skip_a_question(self):
        from covey.app.answers import answer_fields
        record = {"questions": [{"id": "q1", "question": "用哪个？", "options": [{"label": "A"}]}]}
        with self.assertRaises(ValueError):
            answer_fields(record, {"q1": {"skip": True}}, "")

    def test_set_aside_and_restore_go_to_the_questions_own_paths(self):
        self.topic()
        self.question()
        self.web.op("answer", 1, "op-6", TOPIC, token=TOKEN, aside=True)
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["done"])
        self.assertEqual(self.app.lesser, [("aside", TOKEN, "ou_owner", "web-op-6")])
        self.question(status="set_aside", set_aside_at=T0 + 10, set_aside_event="web-op-6")
        self.web.op("answer", 2, "op-7", TOPIC, token=TOKEN, answers={"q1": "A"})
        self.web.op("answer", 3, "op-8", TOPIC, token=TOKEN, restore=True)
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks[1:]], ["failed", "done"])
        self.assertIn("先点撤销", self.web.acks[1]["message"])
        self.assertIn("在网页上", self.web.acks[1]["message"])
        self.assertEqual(self.app.lesser[-1], ("restore", TOKEN, "", "web-op-8"))
        self.assertEqual(self.app.answers, [])

    def test_set_aside_on_a_question_answered_already_says_who_answered(self):
        self.topic()
        self.question(status="answered", answers={"q1": {"answers": ["A"]}}, answered_at=T0 + 60)
        self.web.op("answer", 1, "op-9", TOPIC, token=TOKEN, aside=True)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")
        self.assertIn("答过了", self.web.acks[0]["message"])
        self.assertEqual(self.app.lesser, [])

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
        self.assertEqual(self.app.answered_by, ["ou_owner"])  # The page is the owner's: his answer, noted as his (W0-1).

    def test_an_answer_to_an_answered_or_unknown_question_fails(self):
        self.topic()
        self.question(status="answered")
        self.web.op("answer", 1, "op-5", TOPIC, token=TOKEN, answers={"q1": "A"})
        self.web.op("answer", 2, "op-6", TOPIC, token="nosuchtoken1", answers={"q1": "A"})
        self.web.op("answer", 3, "op-7", TOPIC, token=TOKEN, answers={"q1": "Z"})
        self.sync.inbox_once()
        self.assertEqual([r["state"] for r in self.web.acks], ["failed"] * 3)
        self.assertEqual(self.app.answers, [])

    def test_an_answer_to_a_closed_question_says_who_closed_it_and_what_to_do(self):
        # T602, 2026-11-20 22:34: the owner answered T586's two questions on the page (op 57, done) and the same form went
        # again (op 58): "回答：失败：这个问题已经有回答，或已经取消" said neither who answered nor what to do now.
        self.topic()
        answered = {"q1": {"answers": ["A"]}}
        self.question(status="answered", answers=answered, answer_event="web-op-1", answered_at=T0 + 60)
        self.web.op("answer", 1, "op-10", TOPIC, token=TOKEN, answers={"q1": "A"})  # The same answer again: done, nothing sent.
        self.web.op("answer", 2, "op-11", TOPIC, token=TOKEN, answers={"q1": "B"})
        self.sync.inbox_once()
        same, other = self.web.acks
        self.assertEqual(same["state"], "done")
        self.assertIn("答过同样的答案", same["message"])
        self.assertEqual(other["state"], "failed")
        self.assertIn("你已经在网页上答过了", other["message"])
        self.assertIn("：A", other["message"])
        self.assertIn("在这个话题里直接说一句", other["message"])
        self.assertEqual(self.app.answers, [])
        cases = (
            ({"status": "answered", "answers": answered, "triage": {"state": "answered_by_lead", "at": T0}},
             ("专项负责人已经替你答了", "：A", "「不对」")),
            ({"status": "superseded", "superseded_by": "x" * 16}, ("已经收起", "新的那组")),
            ({"status": "cancelled", "cancel_reason": "stale"}, ("每日维护",)),
            ({"status": "cancelled", "cancel_reason": "lead_stop"}, ("负责人按你的意思停了这个话题",)),
            ({"status": "cancelled"}, ("作废了", "重新说一句")),
            ({"status": "submitting"}, ("正在交给任务",)),
        )
        for seq, (fields, words) in enumerate(cases, start=3):
            self.question(**fields)
            self.web.op("answer", seq, f"op-{seq + 10}", TOPIC, token=TOKEN, answers={"q1": "A"})
            self.sync.inbox_once()
            ack = self.web.acks[-1]
            self.assertEqual(ack["state"], "failed", fields)
            for word in words:
                self.assertIn(word, ack["message"], fields)
            self.assertNotIn("这个问题已经有回答，或已经取消", ack["message"])
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

    def test_a_settings_change_is_pushed_and_acked_before_the_next_one_in_the_batch_runs(self):
        order = []
        self.web.op("settings", 1, "op-s1", what="row")
        self.web.op("settings", 2, "op-s2", what="row")

        def run(row):
            order.append(("run", row["op_id"], len(self.web.acks)))
            return "done", "ok"

        with mock.patch.object(self.sync.ops, "run", side_effect=run), \
                mock.patch.object(self.sync, "push_settings", side_effect=lambda *a, **k: order.append(("push", len(self.web.acks))) or 0):
            self.sync.inbox_once()
        self.assertEqual(order[:4], [("run", "op-s1", 0), ("push", 0), ("run", "op-s2", 1), ("push", 1)])
        self.assertEqual([r["op_id"] for r in self.web.acks], ["op-s1", "op-s2"])
        self.assertTrue(any(line.startswith("web op settings seq=1 queue=") and " run=" in line and " ack=" in line for line in self.logs))

    def test_the_inbox_goes_again_at_once_after_new_operations(self):
        waits = []
        self.sync.runtime.shutdown = SimpleNamespace(is_set=lambda: len(waits) >= 2, wait=lambda s: waits.append(s) or False)
        with mock.patch.object(self.sync, "inbox_once", side_effect=[1, 0, 0]):
            self.sync.loop("inbox", self.sync.inbox_once, lambda: 0.5, again=True)
        self.assertEqual(waits, [0, 0.5])

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
        # The unknown project's is in a lane of its own (OpLanes): acknowledged in its own time, the project's two in order.
        self.assertEqual({r["op_id"]: r["state"] for r in self.web.acks}, {"op-11": "done", "op-12": "done", "op-13": "failed"})
        self.assertEqual([r["op_id"] for r in self.web.acks if r["op_id"] != "op-13"], ["op-11", "op-12"])
        self.assertEqual(self.reload()["state"], "active")
        projects.update(self.state, PID, lambda record: record.update(state="done"))
        self.web.op("reopen", 4, "op-14", project=PID)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "done")  # A finished project opens again (T574).
        self.assertEqual(self.reload()["state"], "active")
        projects.update(self.state, PID, lambda record: record.update(state="cancelled"))
        self.web.op("reopen", 5, "op-15b", project=PID)
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "failed")
        self.assertEqual(self.reload()["state"], "cancelled")

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


class FilesTest(WebCase):
    """T616: the + of the page's chat box: files uploaded to the page come with a say (data contract v21)."""

    def upload(self, upload_id, name, mime, data, parts=2, done=True):
        cut = max(1, -(-len(data) // parts))
        self.web.uploads[upload_id] = ({"name": name, "mime": mime, "size": len(data), "done": done},
                                       [data[i:i + cut] for i in range(0, len(data), cut)])
        return {"id": upload_id, "name": name, "mime": mime, "size": len(data)}

    def test_a_say_with_files_saves_them_in_the_topic_and_lists_them_as_a_feishu_message_does(self):
        self.topic()
        image = self.upload("a" * 32, "截图.png", "image/png", b"\x89PNG" + b"x" * 100)
        doc = self.upload("b" * 32, "../../etc/计划 v2.md", "text/markdown", "- 第一项\n- 第二项\n".encode(), parts=3)
        self.web.op("say", 1, "op-f1", TOPIC, text="看看这两个", files=[image, doc])
        self.sync.inbox_once()
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-f1", "done")])
        [(topic, message, content)] = self.app.queued
        media = self.state / "topics" / TOPIC / "incoming_media"
        saved = sorted(media.iterdir())
        self.assertEqual([path.read_bytes() for path in saved], [b"\x89PNG" + b"x" * 100, "- 第一项\n- 第二项\n".encode()])
        self.assertTrue(all(path.parent == media for path in saved))  # The page's name never leaves the topic's folder.
        self.assertTrue(content.startswith("看看这两个\n\n收到的附件（已下载到本地）："), content)
        self.assertIn(f"- 图片 1: `{saved[0]}`", content)
        self.assertIn(f"- 文件 1（../../etc/计划 v2.md）: `{saved[1]}`", content)
        from covey.core.attachments import native_image_attachments
        self.assertEqual([item["path"] for item in native_image_attachments(content, media)], [str(saved[0].resolve())])

    def test_files_alone_are_a_say_and_one_that_cannot_be_taken_fails_it_whole(self):
        self.topic()
        image = self.upload("c" * 32, "a.png", "image/png", b"png-bytes")
        self.web.op("say", 1, "op-f2", TOPIC, text="", files=[image])
        self.web.op("say", 2, "op-f3", TOPIC, text="还有这个", files=[image, {"id": "d" * 32, "name": "过期.pdf", "mime": "application/pdf", "size": 3}])
        self.web.op("say", 3, "op-f4", TOPIC, text="x", files=[{"id": "../x", "name": "a"}])
        self.sync.inbox_once()
        self.assertEqual([(r["op_id"], r["state"]) for r in self.web.acks], [("op-f2", "done"), ("op-f3", "failed"), ("op-f4", "failed")])
        self.assertEqual(self.web.acks[1]["message"], "附件 过期.pdf 取不到：已过期或没传完")
        self.assertEqual(len(self.app.queued), 1)
        self.assertTrue(self.app.queued[0][2].startswith("收到的附件（已下载到本地）："))

    def test_a_file_not_finished_or_bigger_than_it_said_is_refused(self):
        self.topic()
        half = self.upload("e" * 32, "half.bin", "application/octet-stream", b"12345", done=False)
        self.web.op("say", 1, "op-f5", TOPIC, text="x", files=[half])
        big = self.upload("f" * 32, "big.bin", "application/octet-stream", b"12345")
        self.web.uploads["f" * 32][0]["size"] = 3
        self.web.op("say", 2, "op-f6", TOPIC, text="x", files=[big])
        self.sync.inbox_once()
        self.assertEqual([r["message"] for r in self.web.acks], ["附件 half.bin 还没传完", "附件 big.bin 比它说的大"])
        self.assertEqual(self.app.queued, [])

    def test_a_say_with_files_to_the_butler_lists_them_in_its_top_level_message(self):
        self.butler_topic()
        self.hearing()
        image = self.upload("9" * 32, "a.png", "image/png", b"png")
        self.web.op("say", 1, "op-f7", BUTLER, text="这张图", files=[image])
        self.sync.inbox_once()
        [event] = self.heard
        text = json.loads(event["content"])["text"]
        [saved] = (self.state / "topics" / BUTLER / "incoming_media").iterdir()
        self.assertEqual(text, f"这张图\n\n收到的附件（已下载到本地）：\n- 图片 1: `{saved}`\n请在需要理解图片或文件内容时读取上述本地路径。")


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

    def members(self):
        """A project's member that stopped (no one asked it to: an official refusal's record) and another at work, which its
        project's cell on the board shows; one push: (the pushed 要你处理, the topics drawn 停住了, the topics' items)."""
        ended = T0 - 3600
        self.thread(OTHER, "试跑", 8, status="cancelled", card_message_id="om_card_o",
                    timings={"backend_started": ended - 60, "execution_finished": ended})
        self.thread("e" * 32, "接管试跑", 9, status="running", card_message_id="om_card_e",
                    timings={"received": T0 - 120, "backend_started": T0 - 100})
        self.sync.push_once(T0)
        topics = {item["key"]: item["data"] for item in self.web.items("topic")}
        asks = next(item["data"] for item in self.web.items("settings") if item["key"] == web_sync.ASKS_KEY)
        return asks, sorted(key for key, data in topics.items() if data.get("board") == "stopped"), topics

    def test_a_projects_member_that_stopped_is_drawn_where_it_is_counted(self):
        """2026-11-24 T734: the page said 9 停住了 (要你处理 as every card counts it, data contract v34) and drew none: nine members
        of one project, each ``board`` empty, the board's row being their project's cell showing a thread at work. Without
        COVEY_PROJECT_AUTOWAKE nothing wakes the lead for them: each is the owner's, counted and drawn."""
        asks, drawn, topics = self.members()
        self.assertEqual((asks["stopped"], drawn), (1, [OTHER]))
        self.assertEqual(topics["e" * 32]["board"], "running")

    def test_a_members_stop_its_lead_is_woken_for_is_neither_counted_nor_drawn(self):
        with mock.patch.dict(os.environ, {projects.AUTOWAKE_ENV: "1"}):
            asks, drawn, topics = self.members()
        self.assertEqual((asks["stopped"], drawn), (0, []))
        self.assertEqual((topics[OTHER]["tone"], topics[OTHER]["board"]), ("stopped", ""))  # Grey on the page: the board let it go.

    def test_a_stop_from_before_the_boards_rules_is_drawn_where_it_is_counted(self):
        """要你处理 counts a stop however long before the board's rules it ended (app/pins.py PinnedTopics.look); the board's rows
        do not hold it, and the page must draw it all the same."""
        write_json(self.state / "pinned-topics.json", {"chat_id": CHAT, "since": T0, "keep_since": T0 - 10 * 86400,
                                                       "stopped_since": T0, "results": {}})
        data = self.stopped(seen=False)
        asks = next(item["data"] for item in self.web.items("settings") if item["key"] == web_sync.ASKS_KEY)
        self.assertEqual((asks["stopped"], data["board"]), (1, "stopped"))

    def test_without_the_board_there_is_no_board_field(self):
        os.environ["COVEY_PINNED_OVERVIEW"] = "0"
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
        # Topics' operations run side by side and are acknowledged as each is done (T765): by operation, not in order.
        self.assertEqual({r["op_id"]: r["state"] for r in self.web.acks},
                         {"op-s6": "failed", "op-s7": "failed", "op-s8": "failed", "op-s9": "failed", "op-s0": "done"})
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

    def test_on_there_are_four_threads(self):
        """The quick looks, the full pass (its own thread since T673: a slow read there holds up no quick look) and the inbox."""
        self.assertEqual([name for name, _ in self.sync.threads()], ["web-push", "web-pass", "web-inbox", "web-touch", "web-reread"])

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
        for module in (app_collect, web_turns):
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

    def test_a_step_is_labelled_by_its_purpose_in_plain_words_and_carries_its_command(self):
        """T577 2026-11-20: the page cut "后台命令：正式部署阶段 2（后台） · R=/mnt/…" to its first words, and the command it would
        show read "&amp;" and "COVEY\\_CORE": the trace keeps a step's words escaped for cards."""
        long_path = "/x/" + "a" * 90 + ".py"
        snapshot = {"steps": {"c1": {"title": "后台命令", "detail": "正式部署阶段 2（后台） · R=/x &amp;&amp; COVEY\\_CORE=1 bash d.sh &gt; o.log",
                                     "status": "done", "started": T0, "ended": T0 + 90},
                              "c2": {"title": "执行命令", "detail": "git status", "status": "done", "started": T0 + 91, "ended": T0 + 92},
                              "r1": {"title": "读取文件", "detail": long_path, "status": "done", "started": T0 + 93, "ended": T0 + 94}},
                    "step_order": ["c1", "c2", "r1"], "started_at": T0, "ended_at": T0 + 95}
        rows = {row["id"]: row for row in web_turns.steps_of(snapshot)}
        self.assertEqual((rows["c1"]["label"], rows["c1"]["detail"]), ("后台命令：正式部署阶段 2（后台）", "R=/x && COVEY_CORE=1 bash d.sh > o.log"))
        self.assertEqual(rows["c2"]["label"], "执行命令：git status")
        self.assertNotIn("detail", rows["c2"])
        self.assertEqual((len(rows["r1"]["label"]), rows["r1"]["detail"]), (web_turns.LABEL_CHARS, long_path))

    def test_a_command_without_a_purpose_is_labelled_by_what_it_runs_or_its_comment(self):
        """T738 (2026-11-24): the page's 过程 of Antigravity and Codex turns read "set -euo pipefail for v in http_proxy https_proxy…"
        row under row. A comment the command began with labels it; else what it runs, past its wrappers and setup lines; a
        checkpoint write its title alone. The whole command is the detail."""
        from covey.core.text import safe_text
        boiler = "set -euo pipefail\nfor v in http_proxy https_proxy all_proxy; do unset \"$v\"; done\nS=state/pool-a\n"
        snapshot = {"steps": {"a": {"title": "执行命令", "detail": safe_text(boiler + ".venv/bin/python tools/check.py --pool a", None),
                                    "status": "done", "started": T0, "ended": T0 + 600},
                              "b": {"title": "执行命令", "detail": safe_text("/bin/bash -lc '# 看 a 池的在线状态\n" + boiler + "python3 x.py'", None),
                                    "status": "running", "started": T0 + 601},
                              "c": {"title": "更新检查点", "detail": safe_text("/bin/bash -lc \"cat > /x/checkpoints/a.json\"", None),
                                    "status": "done", "started": T0 + 602, "ended": T0 + 603}},
                    "step_order": ["a", "b", "c"], "started_at": T0}
        rows = {row["id"]: row for row in web_turns.steps_of(snapshot)}
        self.assertEqual(rows["a"]["label"], "执行命令：.venv/bin/python tools/check.py --pool a")
        self.assertTrue(rows["a"]["detail"].startswith("set -euo pipefail for v in http_proxy"))
        self.assertEqual(rows["b"]["label"], "执行命令：看 a 池的在线状态")
        self.assertEqual((rows["c"]["label"], rows["c"]["detail"]), ("更新检查点", '/bin/bash -lc "cat > /x/checkpoints/a.json"'))

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

    def test_a_topic_waiting_for_start_says_what_it_waits_for_and_runs_with(self):
        """2026-11-21 T622, a reply to a node's alert whose task waited 21 minutes for 开始 on its launch card: the page said
        等你 · 第 1 轮 over 再早的 1 轮网页上没有载入 and 第 2 轮 · 排队中… (the topic's ``round`` counted the task, no turn stood for
        it), nothing said what to do, and its head said Codex · GPT-6 Astra · ws-root, the binding the topic was given to wait
        with, for the card's Claude Code · Opus 5.5 · covey. Its words get a 等你 turn that says to press 开始 on the launch card,
        what it would run with and the card's link once known; the topic says the card's choice and has no ``config``."""
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="codex", topic_number=7, origin={"title": "看看占比"},
                    workspace_name="ws-root")
        write_job(self.state, f"k-{TOPIC[:4]}", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="waiting_workspace", model="gpt-6-astra",
                  timings={"received": T0 - 600})
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "user", "帮忙看看是什么数据占比比较多", {"message_id": "om_ask"})
        write_json(self.state / "launch-forms" / "f1.json", {
            "topic_id": TOPIC, "chat_id": CHAT, "status": "ready", "message_id": "om_launch", "harness_id": "codex", "schema_version": 3,
            "compact": True, "selected_model": "claude:opus", "selected_effort": "max",
            "models": {"claude:opus": {"display_name": "Opus 5.5", "harness": "claude", "model": "opus", "label": "Claude Code · Opus 5.5"}},
            "form_defaults": {"workspace": "covey", "path": "", "run_mode": "direct"}})
        self.sync.push_once(T0)
        topic = next(item for item in self.web.items("topic") if item["key"] == TOPIC)["data"]
        self.assertEqual((topic["state"], topic["tone"], topic["round"], topic["last_seq"]), ("等你", "waiting", 1, 2))
        self.assertEqual((topic["harness"], topic["model"], topic["model_name"], topic["workspace"]), ("claude", "opus", "Opus 5.5", "covey"))
        self.assertNotIn("config", topic)
        turns = [item["data"] for item in self.web.items("turn") if item["data"]["topic_key"] == TOPIC]
        self.assertEqual([(turn["seq"], turn["role"], turn["status"]) for turn in turns], [(1, "owner", "done"), (2, "agent", "waiting")])
        waiting = turns[-1]
        self.assertTrue(waiting["body"].startswith(app_collect.START_WORDS), waiting["body"])
        self.assertIn("点了开始会用：Claude Code · Opus 5.5 · 思考预算 max · 工作区 covey", waiting["body"])
        self.assertIn("启动卡在飞书里这个话题的话题串中", waiting["body"])  # No link known: the push reads no message from Feishu.
        self.assertEqual((waiting["job_id"], waiting["started_at"]), ("7", T0 - 600))
        # Once 手上的事 has read where the card is (app/topic_links.py), the turn links to it.
        write_json(self.state / "topic-links.json", {TOPIC: {"messages": {"om_launch": {
            "thread_id": "omt_1", "position": "3", "thread_position": "0"}}}})
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        body = [item["data"] for item in self.web.items("turn") if item["key"] == f"{TOPIC}:2"][-1]["body"]
        self.assertIn(f"[去飞书点开始](https://applink.feishu.cn/client/thread/open?open_chat_id={CHAT}&open_thread_id=omt_1"
                      "&thread_position=0)", body)
        # The card used (开始): the task runs as any, and the topic says what it is bound to again.
        write_json(self.state / "launch-forms" / "f1.json", {"topic_id": TOPIC, "chat_id": CHAT, "status": "submitted",
                                                             "message_id": "om_launch"})
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, workspace_name="covey", model="opus")
        write_job(self.state, f"k-{TOPIC[:4]}", status="queued", model="opus")
        write_job(self.state, f"k-{TOPIC[:4]}", status="running", timings={"received": T0 - 600, "backend_started": T0})
        self.sync.push_once(T0 + 2)
        topic = [item["data"] for item in self.web.items("topic") if item["key"] == TOPIC][-1]
        self.assertEqual((topic["tone"], topic["harness"], topic["workspace"]), ("running", "claude", "covey"))
        self.assertIn("config", topic)
        last = [item["data"] for item in self.web.items("turn") if item["key"] == f"{TOPIC}:2"][-1]
        self.assertEqual(last["status"], "running")

    def test_a_topic_says_what_it_runs_with_for_the_pages_switch(self):
        """T616 (data contract v21): the chat box's 工作区 · Harness · 模型 line opens 换配置, which starts from the topic's ``config``."""
        self.topic()
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, workspace_name="ws1", model="opus",
                    effort="high", workdir="/absolute/ws1")
        self.butler_topic()
        self.sync.push_once(T0)
        topics = {item["key"]: item["data"] for item in self.web.items("topic")}
        self.assertEqual(topics[TOPIC]["config"], {"model": "claude:opus", "effort": "high", "work_mode": "", "workspace": "ws1",
                                                   "run_mode": "direct", "redaction": "off", "device": ""})
        self.assertNotIn("config", topics[BUTLER])
        from covey.app import web_switch
        # A budget preset is its model with the preset's budget; a Bridge worktree is ``worktree``.
        menu = {"claude:opus-x": {"harness": "claude", "presets": {"max": "opus-max", "high": "opus-x"}}}
        self.assertEqual(web_switch.topic_config({"harness_id": "claude", "model": "opus-max", "workdir": "/wt/abc/repo",
                                                  "redaction": True}, menu, prefix="/wt/"),
                         {"model": "claude:opus-x", "effort": "max", "work_mode": "", "workspace": "", "run_mode": "worktree", "redaction": "on",
                          "device": ""})
        self.assertIsNone(web_switch.topic_config({}))

    def test_a_switched_topic_names_the_model_it_is_bound_to_before_its_next_turn(self):
        """2026-11-24 T724: a topic opened on Claude Code · opus, its turn stopped, 换配置 to Antigravity · Gemini 4 Neon. Until its
        next turn ran, the chat box's 工作区 · Harness · 模型 line said Antigravity · Opus: the Harness its binding's, the model the
        stopped task's. The topic names its binding's model, as its Harness and ``config`` do."""
        self.topic()  # Claude Code; its one task ran on opus.
        write_job(self.state, f"k-{TOPIC[:4]}", binding_revision=1)
        switched = {"switches": [{"revision": 2, "from": {"harness_id": "claude", "model": "opus"},
                                  "to": {"harness_id": "agy", "model": "gemini-4-neon"}}], "binding_revision": 2}
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="agy", topic_number=7, workspace_name="ws1",
                    model="gemini-4-neon", effort="xhigh", **switched)
        self.sync.push_once(T0)
        topic = [item["data"] for item in self.web.items("topic") if item["key"] == TOPIC][-1]
        self.assertEqual((topic["harness"], topic["model"], topic["model_name"], topic["config"]["model"]),
                         ("agy", "gemini-4-neon", "Gemini 4 Neon", "agy:gemini-4-neon"))

    def test_a_topic_names_its_binding_model_else_its_last_tasks_unless_switched_since(self):
        """bound_model (T724): the binding's model, which the next message runs on, though the last task ran on another (a
        safety refusal's retry on an older model keeps the binding, app/policy_retry.py); a topic.json that names none (older
        topics) says the last task's, unless a 换配置 to a Harness's own default model bound it anew since."""
        latest = {"model": "claude-opus-4-8", "binding_revision": 1}
        self.assertEqual(app_collect.bound_model({"harness_id": "claude", "model": "claude-opus-5-5", "binding_revision": 1}, latest),
                         "claude-opus-5-5")
        self.assertEqual(app_collect.bound_model({"harness_id": "claude"}, latest), "claude-opus-4-8")
        self.assertEqual(app_collect.bound_model({"harness_id": "claude"}, None), "")
        to_default = {"harness_id": "codex", "model": "", "binding_revision": 2, "switches": [{"revision": 2}]}
        self.assertEqual(app_collect.bound_model(to_default, latest), "")
        self.assertEqual(app_collect.bound_model(to_default, {"model": "gpt-6-luna", "binding_revision": 2}), "gpt-6-luna")

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

    def test_a_question_says_its_background_and_whether_it_can_be_set_aside(self):
        """T617 (data contract v20): each question's 背景 line, ``extras`` (不处理 can be clicked; v29 took 先问一句 off, and the last
        one, ``asked``, with it) and a question set aside, ``restorable`` until the topic's next turn takes it."""
        self.topic()
        self.question(questions=[{"id": "q1", "question": "用哪个库？", "context": "决定依赖", "options": [{"label": "A"}]}],
                      asked_first={"at": T0 + 3, "text": "A 稳吗？", "event": "om_x"})
        self.question("tok-live-0001", native_live_question=True, created_at=T0 + 1)
        self.question("tok-aside-001", status="set_aside", set_aside_at=T0 + 20, created_at=T0 + 2,
                      answers={"q1": {"answers": ["用户选择不回答此题（不表示同意或授权）"]}})
        self.sync.push_once(T0 + 40)
        by = {item["key"]: item["data"] for item in self.web.items("question")}
        self.assertEqual(by[TOKEN]["questions"][0]["context"], "决定依赖")
        self.assertEqual((by[TOKEN]["extras"], by[TOKEN]["restorable"]), (True, False))
        self.assertNotIn("asked", by[TOKEN])  # A record that still has a 先问一句 from before.
        self.assertEqual(by["tok-live-0001"]["extras"], False)
        aside = by["tok-aside-001"]
        self.assertEqual((aside["state"], aside["closed_by"], aside["closed_at"], aside["restorable"], aside["extras"]),
                         ("answered", "set_aside", T0 + 20, True, False))

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

    def checklist(self, *rows):
        projects.update(self.state, PID, lambda record: record.update(checklist={"items": list(rows), "next": len(rows) + 1}))

    def test_a_project_carries_its_checklist_with_topic_keys(self):
        """The page's 进度 tab (docs/web.md 专项条目): the 目标检查表 as the lead keeps it; ``topic`` is the topic's key on the page,
        ``needs`` and ``state`` as written, and nothing of the lead's (``job``)."""
        self.thread(OTHER, "对账", 8, status="done")
        self.checklist({"id": "c1", "criterion": "新库能写", "topic": OTHER, "needs": [], "evidence": "提交 a1b2c3d", "gap": "",
                        "state": "done", "at": T0, "job": "k-dddd"},
                       {"id": "c2", "criterion": "对账一致", "topic": "", "needs": ["c1", "x9"], "evidence": "", "gap": "只差主人点头",
                        "state": "blocked", "at": T0 + 1, "job": "k-dddd"})
        self.sync.push_once(T0 + 5)
        data = self.web.items("project")[-1]["data"]
        self.assertEqual(data["checklist"], [
            {"id": "c1", "criterion": "新库能写", "topic": OTHER, "needs": [], "evidence": "提交 a1b2c3d", "gap": "", "state": "done", "at": T0},
            {"id": "c2", "criterion": "对账一致", "topic": "", "needs": ["c1"], "evidence": "", "gap": "只差主人点头", "state": "blocked",
             "at": T0 + 1}])
        self.assertIn(OTHER, {item["key"] for item in self.web.items("topic")})

    def test_a_changed_checklist_alone_builds_the_project_again(self):
        self.checklist({"id": "c1", "criterion": "新库能写", "topic": "", "needs": [], "evidence": "", "gap": "", "state": "open", "at": T0})
        self.sync.push_once(T0 + 5)
        self.assertEqual(self.sync.push_once(T0 + 6), 0)
        self.checklist({"id": "c1", "criterion": "新库能写", "topic": "", "needs": [], "evidence": "提交 a1b2c3d", "gap": "", "state": "done",
                        "at": T0 + 7})
        self.assertEqual(self.sync.push_once(T0 + 8), 1)
        data = self.web.items("project")[-1]["data"]
        self.assertEqual([(row["state"], row["evidence"], row["at"]) for row in data["checklist"]], [("done", "提交 a1b2c3d", T0 + 7)])

    def test_a_project_without_a_checklist_or_notes_carries_none_of_them(self):
        """An older page reads the item as before: no key for what the record has nothing of (a note is no milestone)."""
        project_lead.append(self.state, PID, "notes", project_lead.entry("派出了 T8", kind="thread"), project_lead.NOTES_KEPT)
        self.sync.push_once(T0 + 5)
        data = self.web.items("project")[-1]["data"]
        self.assertFalse({"checklist", "milestones", "decisions", "background", "actions", "gaps"} & set(data))

    def test_milestones_are_the_milestone_and_finish_notes_and_decisions_the_newest_ten(self):
        for number, kind in enumerate(("note", "milestone", "thread", "finish", "milestone"), 1):
            project_lead.append(self.state, PID, "notes", {"id": f"n{number}", "at": T0 + number, "text": f"第 {number} 条", "kind": kind},
                                project_lead.NOTES_KEPT)
        for number in range(1, 13):
            project_lead.append(self.state, PID, "decisions", {"id": f"d{number}", "at": T0 + number, "text": f"决定 {number}",
                                                                "kind": "note", "report": False}, project_lead.DECISIONS_KEPT)
        self.sync.push_once(T0 + 20)
        data = self.web.items("project")[-1]["data"]
        self.assertEqual(data["milestones"], [{"id": "n2", "at": T0 + 2, "kind": "milestone", "text": "第 2 条"},
                                              {"id": "n4", "at": T0 + 4, "kind": "finish", "text": "第 4 条"},
                                              {"id": "n5", "at": T0 + 5, "kind": "milestone", "text": "第 5 条"}])
        self.assertEqual([row["id"] for row in data["decisions"]], [f"d{number}" for number in range(3, 13)])
        self.assertEqual(data["decisions"][-1], {"id": "d12", "at": T0 + 12, "kind": "note", "text": "决定 12"})
        self.assertNotIn("checklist", data)

    def brief(self, **fields):
        """The record of a project whose lead wrote a 简报: a background, two actions (the second dropped) and a checklist whose first
        item has all the newer fields and whose second has none."""
        projects.update(self.state, PID, lambda record: record.update(
            lead_background={"text": "迁账单到新库", "at": T0, "job": "k-lead"},
            actions={"items": [{"id": "a1", "title": "导出", "note": "在做", "items": ["c1", "c2"], "topics": [OTHER], "state": "open",
                                "at": T0 + 1, "job": "k-lead"},
                               {"id": "a2", "title": "试试看", "note": "", "items": [], "topics": [], "state": "dropped", "at": T0 + 2,
                                "job": "k-lead"}], "next": "a3", "at": T0 + 2, "job": "k-lead"},
            checklist={"items": [{"id": "c1", "criterion": "新库能写", "topic": OTHER, "needs": [], "evidence": "提交 a1b2c3d", "gap": "", "state": "done",
                                  "at": T0, "job": "k-lead", "title": "新库能写", "waiting_on": "", "done_at": T0 + 3, "topics": [TOPIC]},
                                 {"id": "c2", "criterion": "对账一致", "topic": "", "needs": ["c1"], "evidence": "", "gap": "只差主人点头", "state": "open",
                                  "at": T0 + 1, "job": "k-lead"}], "closed": {}, "next": "c3", "at": T0 + 1, "job": "k-lead"}, **fields))

    def test_a_project_carries_its_background_its_actions_and_the_newer_fields_of_its_items(self):
        """The page's 简报 (docs/web.md 专项条目 v41): ``background`` and ``actions`` (the dropped too, nothing of the lead's ``job``), the item's
        ``title``, ``waiting_on`` (written: '' too), ``done_at`` and ``topics`` only on an item that has them."""
        self.thread(OTHER, "对账", 8, status="done")
        self.brief()
        self.sync.push_once(T0 + 5)
        data = self.web.items("project")[-1]["data"]
        self.assertEqual(data["background"], "迁账单到新库")
        self.assertEqual(data["actions"], [
            {"id": "a1", "title": "导出", "note": "在做", "items": ["c1", "c2"], "topics": [OTHER], "state": "open", "at": T0 + 1},
            {"id": "a2", "title": "试试看", "note": "", "items": [], "topics": [], "state": "dropped", "at": T0 + 2}])
        first, second = data["checklist"]
        self.assertEqual({key: first[key] for key in ("title", "waiting_on", "done_at", "topics")},
                         {"title": "新库能写", "waiting_on": "", "done_at": T0 + 3, "topics": [TOPIC]})
        self.assertEqual(set(second), set(app_collect.CHECKLIST_SHOWN))  # An item with none of them: the keys as before.
        self.assertNotIn("gaps", data)

    def test_the_newer_fields_are_each_left_out_when_the_record_says_nothing(self):
        self.brief()
        projects.update(self.state, PID, lambda record: record["checklist"]["items"][0].update(waiting_on=None, done_at=0, topics=[], title=""))
        self.sync.push_once(T0 + 5)
        first = self.web.items("project")[-1]["data"]["checklist"][0]
        self.assertEqual(set(first), set(app_collect.CHECKLIST_SHOWN))

    def test_a_changed_action_or_background_alone_builds_the_project_again(self):
        self.brief()
        self.sync.push_once(T0 + 5)
        self.assertEqual(self.sync.push_once(T0 + 6), 0)
        projects.update(self.state, PID, lambda record: record["actions"]["items"][0].update(note="做完了", at=T0 + 7))
        self.assertEqual(self.sync.push_once(T0 + 8), 1)
        self.assertEqual(self.web.items("project")[-1]["data"]["actions"][0]["note"], "做完了")
        projects.update(self.state, PID, lambda record: record["lead_background"].update(text="换了背景"))
        self.assertEqual(self.sync.push_once(T0 + 9), 1)
        self.assertEqual(self.web.items("project")[-1]["data"]["background"], "换了背景")

    def gap(self, text="缺原版基线数据", at=T0 + 3, thread=OTHER):
        return project_lead.add_receipt(self.state, PID, collab.RECEIPT_GAP, text, thread=thread, at=at)

    def test_the_gaps_not_dealt_with_are_pushed_and_one_that_comes_or_is_closed_builds_the_project_again(self):
        mock.patch.dict(os.environ, {turn_source.SWITCH: "1"}).start()
        self.addCleanup(mock.patch.stopall)
        self.thread(OTHER, "对账", 8, status="done")
        self.brief()
        self.sync.push_once(T0 + 5)
        self.assertNotIn("gaps", self.web.items("project")[-1]["data"])
        self.assertEqual(self.sync.push_once(T0 + 6), 0)
        receipt = self.gap(text="缺" * 300)
        self.gap(text="两份结果冲突", at=T0 + 4, thread="")
        self.assertEqual(self.sync.push_once(T0 + 7), 1)  # A receipt alone, no change of the record.
        gaps = self.web.items("project")[-1]["data"]["gaps"]
        self.assertEqual([(row["thread"], len(row["text"]), row["at"]) for row in gaps], [(OTHER, 200, T0 + 3), ("", 6, T0 + 4)])
        self.assertEqual(gaps[0]["id"], project_checklist.gap_id(receipt["id"]))
        self.assertEqual(set(gaps[0]), {"id", "thread", "text", "at"})
        projects.update(self.state, PID, lambda record: project_checklist.apply(
            record, close=[{"gap": gaps[0]["id"], "how": "T8 补了"}], receipts=project_lead.inbox_items(self.state, PID), now=T0 + 8))
        self.assertEqual(self.sync.push_once(T0 + 9), 1)
        self.assertEqual([row["id"] for row in self.web.items("project")[-1]["data"]["gaps"]], [gaps[1]["id"]])

    def test_with_collab_off_no_gap_is_pushed(self):
        self.brief()
        self.gap()
        self.sync.push_once(T0 + 5)
        self.assertNotIn("gaps", self.web.items("project")[-1]["data"])

    def checkpoint(self, **fields):
        path = self.state / "checkpoint.json"
        write_json(path, {"phase": "改卡片", "completed": [], "artifacts": [], "next_step": "", "requires_user": False, **fields})
        return path

    def test_a_running_topic_says_its_next_step_and_what_it_waits_for_from_its_checkpoint(self):
        """Data contract v41: ``progress`` carries ``next_step`` (120 characters) and ``waiting`` (the question while ``requires_user``, 160),
        each only when the checkpoint has one: not the seed's next step, not "无"; core/progress.py's own fields are as they were."""
        self.topic(status="running", lines=(("user", "做一下"),))
        path = self.checkpoint(next_step="补测试" + "试" * 200, requires_user=True, question="用哪个库？" + "问" * 200)
        write_job(self.state, f"k-{TOPIC[:4]}", checkpoint_path=str(path), phase="改卡片", timings={"backend_requested": T0 - 60})
        self.sync.push_once(T0)
        progress = [item for item in self.web.items("topic") if item["key"] == TOPIC][-1]["data"]["progress"]
        self.assertEqual((len(progress["next_step"]), progress["next_step"][:3], len(progress["waiting"]), progress["waiting"][:6]),
                         (120, "补测试", 160, "用哪个库？问"))
        self.assertEqual(progress["phase"], "改卡片")
        for fields in ({"next_step": "检查任务要求与已有状态"}, {"next_step": "无"}, {"next_step": "无。"}, {"next_step": "", "question": "x"},
                       {"requires_user": True, "question": ""}):
            with self.subTest(fields=fields):
                path = self.checkpoint(**fields)
                self.sync.collector.touch(TOPIC)
                write_job(self.state, f"k-{TOPIC[:4]}", checkpoint_path=str(path), updated_at=f"2026-11-25T10:00:0{len(fields)}Z")
                self.sync.push_once(T0 + 1)
                progress = [item for item in self.web.items("topic") if item["key"] == TOPIC][-1]["data"]["progress"]
                self.assertFalse({"next_step", "waiting"} & set(progress), progress)


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
        with mock.patch.object(app_collect.project_loop, "reported", return_value=True):
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

    def test_say_to_the_lead_of_a_finished_project_opens_it_again(self):
        self.env.topics.apps = {"claude": self.app}
        projects.update(self.state, PID, lambda record: record.update(state="done", state_reason="做完了"))
        self.web.op("say", 1, "op-l3", LEAD, text="还在吗")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "done")
        self.assertEqual((self.reload()["state"], self.reload()["reopened"][-1]["summary"]), ("active", "做完了"))
        self.assertEqual(self.app.queued, [(LEAD, "web-op-l3", "还在吗")])

    def test_say_to_the_lead_of_a_cancelled_project_or_without_projects_fails_with_a_reason(self):
        self.env.topics.apps = {"claude": self.app}
        projects.update(self.state, PID, lambda record: record.update(state="cancelled"))
        self.web.op("say", 1, "op-l3", LEAD, text="还在吗")
        self.sync.inbox_once()
        self.assertEqual((self.web.acks[-1]["state"], "已经取消" in self.web.acks[-1]["message"]), ("failed", True))
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
            answers={"q1": {"answers": ["B"]}, "q2": {"answers": [answer_words.SKIPPED]}, "q3": {"answers": ["单测", "回放"]}})
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
        self.assertEqual(app_pairing.answer_rows(read_object(self.state / "questions" / f"{TOKEN}.json")),
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
    return datetime.datetime.fromtimestamp(T0 + seconds).astimezone().isoformat(timespec="seconds")  # +08:00, 3.10 reads it.


def ran(job_id, message, start, end, **fields):
    """A job record that ran from T0 + ``start`` and was last written at T0 + ``end``."""
    return {"job_id": job_id, "message_id": message, "status": "done", "timings": {"backend_started": T0 + start}, "updated_at": at(end),
            **fields}


class TurnJobsTest(WebCase):
    """Every answer turn is paired with its task (Collector.pair): 2026-11-19 T550 found 97 of the Covey project's 127 answer turns
    without one, so with no token, process or plan on the page (the project's tile, the report's token table, a turn's 过程)."""

    def paired(self, rows, records, questions=()):
        return [(row["role"], row.get("content"), job and job["job_id"]) for row, job, _ in app_pairing.pair(rows, records, questions)]

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
        for module in (app_collect, web_turns):
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
        for module in (app_collect, web_turns):
            patch = mock.patch.object(module, "load_trace", side_effect=lambda *args: snapshot_of(T0))
            patch.start()
            self.addCleanup(patch.stop)
        self.sync.push_once(T0)
        answers = sorted((item["data"] for item in self.web.items("turn") if item["data"]["role"] == "agent"), key=lambda data: data["started_at"])
        self.assertEqual([data["job_id"] for data in answers], [str(100 + number) for number in range(1, 16)])
        self.assertTrue(all(data["usage"]["total"] == 1500 for data in answers))
        self.assertEqual([("steps" in data) for data in answers], [False] * 5 + [True] * 10)
        self.assertEqual(self.sync.push_once(T0 + 1), 0)  # Read once: nothing changes the next second.

    def test_a_turn_that_ended_more_than_seven_days_ago_goes_without_its_process(self):
        self.topic(lines=())
        history = self.state / "topics" / TOPIC / "history.jsonl"
        for number in range(1, 4):
            write_job(self.state, f"k-{number}", topic_id=TOPIC, chat_id=CHAT, job_id=100 + number, status="done", message_id=f"om_{number}")
            append_history(history, "user", f"第 {number} 件", {"message_id": f"om_{number}"})
            append_history(history, "assistant", f"第 {number} 件做完了")
        for module in (app_collect, web_turns):
            patch = mock.patch.object(module, "load_trace", side_effect=lambda *args: snapshot_of(T0))
            patch.start()
            self.addCleanup(patch.stop)

        def answers():  # The newest of each turn the page has.
            latest = {item["key"]: item["data"] for item in self.web.items("turn")}
            return [data for _, data in sorted(latest.items()) if data["role"] == "agent"]
        self.sync.push_once(T0 + 6 * 86400)
        self.assertEqual([("steps" in data, data["usage"]["total"]) for data in answers()], [(True, 1500)] * 3)
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 8 * 86400)
        self.assertEqual([("steps" in data, data["usage"]["total"]) for data in answers()], [(False, 1500)] * 3)
        self.assertFalse(app_collect.steps_expired({"ended_at": 0}, T0 + 8 * 86400), "a running turn keeps its process")

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


class RanWithTest(WebCase):
    """An answer's foot on the page (data contract v27): what its turn ran with, from its task's record."""

    MENU = {"claude:opus": {"title": "Claude Code", "provider": "", "provider_title": "自带", "display_name": "Opus 5.5"},
            "claude:gw/opus": {"title": "Claude Code", "provider": "gw", "provider_title": "网关", "display_name": "Opus 5.5 · 网关"}}

    def test_a_record_reads_in_the_launch_cards_words(self):
        job = {"harness_id": "claude", "model": "opus", "effort": "high", "workspace_name": "ws-root"}
        self.assertEqual(web_turns.ran_with(job, self.MENU, False), {"harness": "Claude Code", "provider": "自带", "model": "Opus 5.5",
                                                                     "effort": "high", "workspace": "ws-root", "redaction": "off"})
        self.assertEqual(web_turns.ran_with(dict(job, model="gw/opus"), self.MENU, True)["provider"], "网关")
        self.assertEqual(web_turns.ran_with(dict(job, model="gw/opus"), self.MENU, True)["redaction"], "on")
        # No menu (it could not be read) or a model the menu no longer has: the model's own name, no provider.
        self.assertEqual(web_turns.ran_with(dict(job, model="claude-opus-5-5"), {}, False),
                         {"harness": "claude", "model": "Opus 5.5", "effort": "high", "workspace": "ws-root", "redaction": "off"})
        self.assertEqual(web_turns.ran_with({"model": "opus"}, self.MENU), {})
        self.assertEqual(web_turns.ran_with({"model": "opus"}, self.MENU, topic={"harness_id": "claude", "workspace_name": "ws1"})["workspace"],
                         "ws1")

    def test_an_answer_turn_carries_it_and_the_owners_does_not(self):
        self.topic()
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "整理账单"},
                    workspace_name="ws1", redaction=True)
        with mock.patch.object(app_collect.Collector, "menu", return_value=self.MENU):  # Both collectors' (the pass builds it).
            self.sync.push_once(T0)
        turns = {item["key"]: item["data"] for item in self.web.items("turn")}
        self.assertEqual(turns[f"{TOPIC}:2"]["ran_with"], {"harness": "Claude Code", "provider": "自带", "model": "Opus 5.5",
                                                         "workspace": "ws1", "redaction": "on"})
        self.assertNotIn("ran_with", turns[f"{TOPIC}:1"])


class FailureOfTest(unittest.TestCase):
    """What a turn that did not succeed tells the page beside failure_view (app/web_turns.py failure_of, data contract v33): each
    button's words as the card says them, how the page does each, and a pending automatic retry (T650)."""

    def failed(self, **fields):
        return {"job_id": 7, "topic_id": TOPIC, "chat_id": CHAT, "status": "failed", "execution_status": "failed", **fields}

    def test_each_action_has_the_cards_words_and_the_way_the_page_does_it(self):
        view = web_turns.failure_of(self.failed(failure_code="root_failed"))
        self.assertEqual((view["actions"], view["labels"], view["how"]),
                         (["continue", "diagnose"], {"continue": "检查并继续", "diagnose": "看原因"},
                          {"continue": "recover", "diagnose": "recover"}))
        self.assertNotIn("auto_retry", view)
        # A result that did not go out: 检查并继续 is 重新投递结果 there, and redelivers it (never the model again).
        undelivered = web_turns.failure_of(self.failed(execution_status="done", delivery_ok=False, final_text="回答"))
        self.assertEqual(undelivered["labels"], {"continue": "重新投递结果"})
        # 换个模型 is the page's own 换配置 for a topic; the butler's configuration is changed in Feishu only.
        self.assertEqual(web_turns.failure_of(self.failed(failure_code="quota_exhausted"))["how"], {"switch_model": "switch"})
        butler = web_turns.failure_of(self.failed(failure_code="quota_exhausted", topic_id=BUTLER))
        self.assertEqual(butler["how"], {"switch_model": "feishu"})
        self.assertIsNone(web_turns.failure_of({"status": "done", "topic_id": TOPIC}))

    def test_a_pending_automatic_retry_says_when_what_runs_it_now_and_what_stops_it(self):
        at = time.time() + 170
        view = web_turns.failure_of(self.failed(failure_code="server_fault", auto_retry_state="pending", auto_retry_at=at,
                                                auto_retry_attempt=1, auto_retry_kind="server_fault"))
        self.assertEqual(view["auto_retry"], {"state": "pending", "at": round(at), "attempt": 1, "total": 3, "kind": "server_fault",
                                              "now": "retry"})
        self.assertEqual((view["labels"]["retry"], view["labels"]["stop_auto_retry"], view["how"]["stop_auto_retry"]),
                         ("现在重试", "停止自动重试", "recover"))
        self.assertIn("会自动重试（第 1/3 次）", view["title"])
        # A restart's: nothing to click in its actions, but 检查并继续 runs it now, as its card's button does.
        restart = web_turns.failure_of({"job_id": 7, "topic_id": TOPIC, "status": "interrupted", "interrupted_by": "restart",
                                        "auto_retry_state": "pending", "auto_retry_at": at, "auto_retry_attempt": 1,
                                        "auto_retry_kind": "restarted"})
        self.assertEqual((restart["actions"], restart["auto_retry"]["now"], restart["labels"]["continue"]), ([], "continue", "检查并继续"))
        # A rate limit's ladder starts over after progress: no total to count against.
        limit = web_turns.failure_of(self.failed(failure_code="rate_limited", auto_retry_state="pending", auto_retry_at=at,
                                                 auto_retry_attempt=2, auto_retry_kind="rate_limited"))
        self.assertIsNone(limit["auto_retry"]["total"])


class RecoverTest(WebCase):
    """``recover`` (data contract v33): a button of a turn that did not succeed, done as its task card's button does it (T650)."""

    def setUp(self):
        super().setUp()
        self.topic()
        self.env.topics.apps = {"claude": self.app}
        self.retries, self.follow_ups, self.stops = [], [], []
        self.app.controls.retry_topic = self.retry_topic
        self.app.controls.stop_auto_retry = self.stop_auto_retry
        self.app.submission.follow_up_job = self.follow_up_job
        for name in ("retry_context", "record_topic"):
            patch = mock.patch.object(web_ops.auto_retry, name, side_effect=lambda *args: Ctx(TOPIC, self.state))
            patch.start()
            self.addCleanup(patch.stop)

    def failed(self, job_id=21, **fields):
        fields = {"status": "failed", "execution_status": "failed", **fields}
        return write_job(self.state, f"k-{job_id}", job_id=job_id, topic_id=TOPIC, chat_id=CHAT, backend="claude",
                         message_id=f"om_{job_id}", **fields)

    def retry_topic(self, ctx, chat, message, *, job_id=None):
        """TaskControls.retry_topic as the card's click runs it: a failed task queued again, once."""
        self.retries.append((ctx.topic_id, chat, message, job_id))
        record = read_object(job_file(self.state, f"k-{job_id}"))
        if record.get("retried_by") or self.jobs.get(TOPIC):
            return "本话题还有运行或排队的任务，请完成或停止后再重试。"
        write_job(self.state, f"k-{job_id}", retried_by=message)
        write_job(self.state, message, job_id=22, topic_id=TOPIC, chat_id=CHAT, message_id=message, status="queued")
        return "本话题任务已重新入队，任务 #22。"

    def follow_up_job(self, ctx, chat, message, job_id, name):
        self.follow_ups.append((message, job_id, name))
        return "", 23

    def stop_auto_retry(self, ctx, chat, job_id):
        self.stops.append(job_id)
        write_job(self.state, f"k-{job_id}", auto_retry_state="cancelled")
        return "已停止"

    def recover(self, op_id, **payload):
        self.web.op("recover", len(self.web.outbox) + 1, op_id, TOPIC, **payload)
        self.sync.inbox_once()
        return self.web.acks[-1]

    def test_continue_is_the_cards_check_and_continue_once(self):
        self.failed(failure_code="root_failed")
        ack = self.recover("op-1", action="continue", job_id="21")
        self.assertEqual((ack["state"], ack["message"]), ("done", "本话题任务已重新入队，任务 #22。"))
        # Its message id says no words of the owner's (not web-): as a card click's event id.
        self.assertEqual(self.retries, [(TOPIC, CHAT, "recover-op-1", 21)])
        self.assertEqual(self.app.queued, [])  # Not a say: the page's words never stood in for the button.
        again = self.recover("op-2", action="continue", job_id="21")  # The page clicked twice.
        self.assertEqual((again["state"], again["message"]), ("failed", "这一轮已经接着做了（任务 #22），没有再开一次"))
        self.assertEqual(len(self.retries), 1)

    def test_a_refused_retry_is_failed_with_the_cards_words(self):
        self.failed(failure_code="root_failed")
        self.jobs[TOPIC] = [SimpleNamespace(job_id=30)]
        ack = self.recover("op-1", action="retry", job_id="21")
        self.assertEqual((ack["state"], ack["message"]), ("failed", "本话题还有运行或排队的任务，请完成或停止后再重试。"))

    def test_continue_runs_a_pending_automatic_retry_now(self):
        self.failed(status="interrupted", interrupted_by="restart", auto_retry_state="pending", auto_retry_at=time.time() + 60,
                    auto_retry_attempt=1, auto_retry_kind="restarted")
        self.assertEqual(self.recover("op-1", action="continue", job_id="21")["state"], "done")

    def test_what_the_record_does_not_allow_is_failed(self):
        self.failed(failure_code="timeout")  # 检查并继续 and 拆小再试; no 看原因.
        self.failed(job_id=24, failure_code="quota_exhausted")  # 换个模型 alone.
        self.failed(job_id=25, failure_code="root_failed", binding_revision=1)
        write_topic(self.state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", topic_number=7, origin={"title": "整理账单"},
                    switches=[{"revision": 2}])
        cases = [("op-1", {"action": "diagnose", "job_id": "21"}, "这一轮现在没有这一步可做"),
                 ("op-2", {"action": "continue", "job_id": "24"}, "这一轮现在不能接着做"),
                 ("op-3", {"action": "continue", "job_id": "25"}, "话题之后换过配置"),
                 ("op-4", {"action": "stop_auto_retry", "job_id": "21"}, "没有待执行的自动重试"),
                 ("op-5", {"action": "rerun", "job_id": "21"}, "不认识的恢复操作"),
                 ("op-6", {"action": "continue", "job_id": "99"}, "这个话题里没有任务 #99"),
                 ("op-7", {"action": "continue"}, "没有任务编号")]
        for op_id, payload, words in cases:
            with self.subTest(op_id):
                ack = self.recover(op_id, **payload)
                self.assertEqual(ack["state"], "failed")
                self.assertIn(words, ack["message"])
        self.assertEqual((self.retries, self.follow_ups), ([], []))

    def test_diagnose_and_split_queue_the_cards_follow_up(self):
        self.failed(failure_code="root_failed")
        ack = self.recover("op-1", action="diagnose", job_id="21")
        self.assertEqual(ack["state"], "done")
        self.assertIn("已开一轮只读排查", ack["message"])
        self.assertIn("任务 #23", ack["message"])
        self.assertEqual(self.follow_ups, [("recover-op-1", 21, "diagnose")])

    def test_stop_auto_retry_cancels_the_pending_one(self):
        self.failed(failure_code="server_fault", auto_retry_state="pending", auto_retry_at=time.time() + 60, auto_retry_attempt=1,
                    auto_retry_kind="server_fault")
        ack = self.recover("op-1", action="stop_auto_retry", job_id="21")
        self.assertEqual((ack["state"], self.stops), ("done", [21]))

    def test_another_chats_topic_is_refused(self):
        write_topic(self.state, topic_id=OTHER, chat_id="oc_someone_else", harness_id="claude")
        self.web.op("recover", 1, "op-1", OTHER, action="continue", job_id="21")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "failed")


class RecoverRedeliveryTest(unittest.TestCase):
    """``recover`` on a turn whose result did not go out, through the real task controls (T650, P0-5): the saved result is sent
    again, the model does not run again; the page's ``say`` of 继续 ran a new turn for it before."""

    def test_continue_redelivers_the_saved_result(self):
        import tempfile
        from pathlib import Path
        from support import make_context
        from support.world import build_claude_app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = build_claude_app()
        self.addCleanup(app.runtime.shutdown.set)
        ctx = make_context(Path(tmp.name), topic_id=TOPIC)
        state = ctx.state_dir
        write_topic(state, topic_id=TOPIC, chat_id=CHAT, harness_id="claude", root_id="om_root")
        write_job(state, "job-k", job_id=5, chat_id=CHAT, topic_id=TOPIC, backend="claude", harness_id="claude", kind="chat",
                  status="failed", execution_status="done", final_text="回答全文", delivery_ok=False,
                  delivery_error="network_timeout", message_id="job-k")
        env = SimpleNamespace(state_dir=state, runtime=app.runtime, topics=SimpleNamespace(apps={"claude": app}))
        ops = web_ops.Ops(env)
        with mock.patch.object(web_ops.auto_retry, "retry_context", return_value=ctx), \
                mock.patch.object(web_ops.auto_retry, "record_topic", return_value={}), \
                mock.patch.object(app.controls.messenger, "reply", return_value=True) as reply:
            said = ops.recover(state, CHAT, {"op_id": "op-1", "topic_key": TOPIC}, {"action": "continue", "job_id": "5"})
        self.assertIn("重新投递", said)
        self.assertEqual(reply.call_args.args[2], "回答全文")
        self.assertIs(read_object(job_file(state, "job-k"))["delivery_ok"], True)
        self.assertEqual(app.runtime.jobs.topic_jobs(TOPIC, CHAT), [])  # Nothing queued: the model did not run again.


class PageUrlTest(unittest.TestCase):
    """The web page's address for the cards' 打开网页 (core/web_sync.py page_url, T657 B9): only with COVEY_WEB=1 and a
    ``page_url`` of http(s) in the config; the key is not read for it, and nothing raises."""

    def test_only_with_the_switch_and_an_address(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(folder.cleanup)
        config = Path(folder.name) / "web.json"
        cases = ({"page_url": "https://example.invalid/app/x"}, {}, {"page_url": "ftp://example.invalid"},
                 {"page_url": "javascript:alert(1)"}, {"page_url": "https://exa mple.invalid"})
        for data, want in zip(cases, ("https://example.invalid/app/x", "", "", "", "")):
            config.write_text(json.dumps({"base_url": "https://example.invalid/openapi", "key_file": "/absolute/none", **data}))
            with mock.patch.dict(os.environ, {web_sync.ENV: "1", web_sync.CONFIG_ENV: str(config)}):
                self.assertEqual(web_sync.page_url(), want, data)
        with mock.patch.dict(os.environ, {web_sync.ENV: "0", web_sync.CONFIG_ENV: str(config)}):
            config.write_text(json.dumps(cases[0]))
            self.assertEqual(web_sync.page_url(), "")
        with mock.patch.dict(os.environ, {web_sync.ENV: "1", web_sync.CONFIG_ENV: str(config) + ".missing"}):
            self.assertEqual(web_sync.page_url(), "")
        config.write_text("{not json")
        with mock.patch.dict(os.environ, {web_sync.ENV: "1", web_sync.CONFIG_ENV: str(config)}):
            self.assertEqual(web_sync.page_url(), "")


class OffTest(WebCase):
    def test_forget_is_one_of_the_kinds_and_the_off_switch_still_means_no_request(self):
        self.assertIn("forget", web_sync.KINDS)
        with mock.patch.dict(os.environ, {web_sync.ENV: "0"}):
            self.assertEqual(self.sync.threads(), ())
        self.assertEqual(self.web.seen, [])


if __name__ == "__main__":
    unittest.main()


class FakeTidy:
    """整理专项 as the web sync sees it (app/project_tidy.py web_stamps, web_view, web_decide)."""

    def __init__(self):
        self.stamp, self.decided = 1, []
        self.data = {"proposals": [{"id": "p1", "kind": "split", "headline": "拆出新专项「A」", "state": "open"}], "running": None,
                     "updated_at": 1.0}

    def web_stamps(self):
        return (self.stamp,)

    def web_view(self, now=None):
        return self.data

    def web_decide(self, pid, choice):
        self.decided.append((pid, choice))
        self.data = {**self.data, "proposals": [{**self.data["proposals"][0], "state": "done", "note": "已拆出专项「A」"}]}
        self.stamp += 1
        return True, "已拆出专项「A」"


class TidyPushTest(WebCase):
    """Data contract v16: 整理专项's suggestions as the ``settings`` item ``tidy``, in a request of their own."""

    def setUp(self):
        super().setUp()
        self.env.tidy = FakeTidy()
        patch = mock.patch.dict(os.environ, {project_proposals.ENV: "suggest"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_pushed_when_it_changed_alone_in_its_request_and_a_page_that_refuses_it_holds_nothing_up(self):
        self.assertEqual(self.sync.push_tidy(T0), 1)
        self.assertEqual([(each["type"], each["key"]) for batch in self.web.upserts for each in batch["items"]], [("settings", "tidy")])
        self.assertEqual(self.sync.push_tidy(T0 + 1), 0)  # Nothing changed.
        self.assertEqual(self.sync.push_tidy(T0 + 2, full=True), 0)  # A full look: the same rev is not sent again.
        self.env.tidy.stamp += 1
        self.env.tidy.data = {**self.env.tidy.data, "running": {"done": 1, "total": 9}}
        self.web.fail = 400  # A page older than v16 refuses the key.
        self.assertEqual(self.sync.push_tidy(T0 + 3), 0)
        self.assertEqual(self.sync.push_tidy(T0 + 4), 0)  # Not again each second: only on a change or a full look.
        self.assertEqual(len([line for line in self.logs if "web tidy not pushed" in line]), 1)
        self.web.fail = 0
        self.assertEqual(self.sync.push_tidy(T0 + 5, full=True), 1)

    def test_a_decision_from_the_page_goes_through_the_tidy_and_the_new_item_is_pushed_before_the_ack(self):
        self.sync.push_tidy(T0)
        self.web.op("tidy_decide", 1, "op-t1", proposal="p1", choice="ok")
        self.assertEqual(self.sync.inbox_once(), 1)
        self.assertEqual(self.env.tidy.decided, [("p1", "ok")])
        self.assertEqual(self.web.acks[-1]["state"], "done")
        self.assertEqual(self.web.acks[-1]["message"], "已拆出专项「A」")
        last = [each for batch in self.web.upserts for each in batch["items"] if each["key"] == "tidy"][-1]
        self.assertEqual(last["data"]["proposals"][0]["state"], "done")
        self.web.op("tidy_decide", 2, "op-t2", proposal="p1", choice="later")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[-1]["state"], "failed")


class RoomTest(WebCase):
    """The room (大厅, data contract v23, app/web_room.py): a butler turn says whom it handed the owner's words to, a topic turn
    whose conclusion went to the top level says when; a lead's turns carry neither."""

    def opened(self, name, **fields):
        write_json(self.state / butler_opened.OPENED / f"{name}.json", {"answer": {"ok": True, "data": {}}, "at": T0, **fields})

    def relay(self, key, **fields):
        write_json(self.state / "main-thread" / "relays" / f"{key}.json", {"state": "sent", "job_key": key, "sent_at": T0 + 5, **fields})

    def turn(self, topic, seq):
        found = [item["data"] for item in self.web.items("turn") if item["key"] == f"{topic}:{seq}"]
        return found[-1] if found else None

    def test_a_butler_turn_names_the_topic_and_the_lead_it_handed_on_to(self):
        self.topic()
        self.butler_topic()
        self.opened("r1", job_key="om_b1", topic_id=TOPIC, plan={"harness": "claude"}, at=T0)
        self.opened("r2", job_key="om_b1", lead_topic_id=LEAD, project_id=PID, at=T0 + 1)
        self.opened("r3", job_key="om_b1", topic_id=OTHER, answer={"ok": False, "text": "没开成"})  # Failed: not said.
        self.opened("r4", job_key="om_other", topic_id=OTHER)  # Another butler turn's.
        self.sync.push_once(T0)
        self.assertEqual(self.turn(BUTLER, 2)["handed"], [
            {"key": TOPIC, "kind": "topic", "number": 7, "title": "整理账单", "project_key": "", "how": "start"},
            {"key": LEAD, "kind": "lead", "number": 0, "title": "迁移账单", "project_key": PID, "how": "send"}])
        self.assertNotIn("handed", self.turn(BUTLER, 1))  # The owner's line: the answer's says it.

    def test_a_topic_turn_whose_conclusion_went_to_the_private_chat_says_when(self):
        self.topic()
        self.butler_topic()
        self.sync.push_once(T0)
        self.assertNotIn("relayed_at", self.turn(TOPIC, 2))
        self.assertNotIn("handed", self.turn(BUTLER, 2))
        self.relay("7", topic_id=TOPIC, job_id=7)  # The topic's job (no message id: its key is its number).
        self.relay("om_b1", state="uncertain", job_id=3)  # Not confirmed: not said, and never of the butler anyway.
        self.sync.collector.touch(TOPIC)
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.turn(TOPIC, 2)["relayed_at"], int(T0 + 5))
        self.assertNotIn("relayed_at", self.turn(TOPIC, 1))
        self.assertNotIn("relayed_at", self.turn(BUTLER, 2))


class OpLaneTest(WebCase):
    """T765 (2026-11-25): the page's operations are acknowledged as soon as each is done, their follow-ups after (as a card click's,
    app/card_lanes.py after_ack), in order within a topic and side by side across topics; one still running is not failed."""

    def wait_for(self, check, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return check()

    def test_an_answers_follow_up_runs_after_its_ack(self):
        self.topic()
        self.question()
        order = []

        def submit(ctx, chat, token, fields, event, by=None):
            order.append(("answer", len(self.web.acks)))
            card_lanes.after_ack(lambda: order.append(("follow-up", len(self.web.acks))))
        self.app.questions.submit_answer = submit
        self.web.op("answer", 1, "op-a1", TOPIC, token=TOKEN, answers={"q1": "A"})
        self.sync.inbox_once()
        self.assertTrue(self.wait_for(lambda: len(order) == 2))
        self.assertEqual(order, [("answer", 0), ("follow-up", 1)])  # The ack went out between the answer and its task card.
        self.assertEqual(self.web.acks[0]["state"], "done")

    def test_a_follow_up_that_raises_is_logged_after_a_done_ack(self):
        self.topic()
        self.question()

        def submit(ctx, chat, token, fields, event, by=None):
            card_lanes.after_ack(lambda: 1 / 0)
        self.app.questions.submit_answer = submit
        self.web.op("answer", 1, "op-a2", TOPIC, token=TOKEN, answers={"q1": "A"})
        self.sync.inbox_once()
        self.assertTrue(self.wait_for(lambda: any("web op follow-up error: ZeroDivisionError" in line for line in self.logs)))
        self.assertEqual(self.web.acks[0]["state"], "done")

    def test_a_slow_operation_holds_its_topic_not_the_others(self):
        release = threading.Event()
        ran = []

        def run(row):
            ran.append(row["op_id"])
            if row["topic_key"] == "slow":
                release.wait(5)
            return "done", "ok"
        self.web.op("say", 1, "op-s1", "slow", text="a")
        self.web.op("say", 2, "op-s2", "slow", text="b")
        self.web.op("say", 3, "op-f1", "fast", text="c")
        with mock.patch.object(app_web, "OP_WAIT", 0.3), mock.patch.object(self.sync.ops, "run", side_effect=run):
            self.sync.inbox_once()
            self.assertTrue(self.wait_for(lambda: [r["op_id"] for r in self.web.acks] == ["op-f1"]))
            self.assertNotIn("op-s2", ran)  # Behind the slow one in its own topic.
            release.set()
            self.assertTrue(self.wait_for(lambda: len(self.web.acks) == 3))
        self.assertEqual([r["op_id"] for r in self.web.acks], ["op-f1", "op-s1", "op-s2"])

    def test_one_still_running_is_left_alone_and_acked_with_its_outcome(self):
        """2026-11-23 15:28: a step stuck on the network disk was left behind, the next one failed its say as uncertain, and the say
        had opened its turn all the same. Now a step skips what is still being done; that ends with its own ack."""
        release = threading.Event()

        def run(row):
            release.wait(5)
            return "done", "已开下一轮"
        self.web.op("say", 1, "op-r1", TOPIC, text="接着做")
        with mock.patch.object(app_web, "OP_WAIT", 0.1), mock.patch.object(self.sync.ops, "run", side_effect=run):
            self.assertEqual(self.sync.inbox_once(), 1)
            self.assertEqual(self.sync.inbox_once(), 0)  # Handed out again (still new on the page), still running here.
            self.assertEqual(self.web.acks, [])
            release.set()
            self.assertTrue(self.wait_for(lambda: len(self.web.acks) == 1))
        self.assertEqual((self.web.acks[0]["state"], self.web.acks[0]["message"]), ("done", "已开下一轮"))
        self.assertEqual(self.sync.ledger.get("op-r1")["state"], "done")

    def test_one_taken_by_an_earlier_process_is_still_failed_as_uncertain(self):
        self.sync.ledger = web_sync.Ledger(self.state)
        self.sync.ledger.take("op-u1", 1)
        self.web.op("say", 1, "op-u1", TOPIC, text="a")
        self.sync.inbox_once()
        self.assertEqual(self.web.acks[0]["state"], "failed")
        self.assertIn("结果不确定", self.web.acks[0]["message"])

    def test_every_operation_logs_where_its_seconds_went(self):
        self.topic()
        self.web.op("seen", 1, "op-w1", TOPIC)
        self.web.outbox[-1]["created_at"] = datetime.datetime.fromtimestamp(time.time() - 2, datetime.timezone.utc).isoformat()
        self.sync.inbox_once()
        line = next(line for line in self.logs if line.startswith("web op seen seq=1 "))
        for part in ("wait=", "queue=", "run=", "push=", "ack="):
            self.assertIn(part, line)
        self.assertGreaterEqual(int(line.split("wait=")[1].split("ms")[0]), 1500)

    def test_the_inbox_asks_with_wait_and_goes_at_once_after_a_held_request(self):
        self.sync.inbox_once()
        self.assertIn("wait=8", [path for _, path, _ in self.web.seen if path.startswith("/sync/inbox")][-1])
        self.assertEqual(self.sync.inbox_pause(), web_sync.POLL_SECONDS)  # An older page answers at once: poll_seconds as before.
        self.web.waited = True
        self.sync.inbox_once()
        self.assertEqual(self.sync.inbox_pause(), 0)

    def test_one_still_running_makes_the_next_ask_pause(self):
        """A page that holds the inbox answers at once while an operation is new on it, and one still being done here is: the next ask
        waits poll_seconds, not none, or the inbox would ask without a pause until that operation ends."""
        release = threading.Event()
        self.web.waited = True
        self.web.op("say", 1, "op-p1", TOPIC, text="a")
        with mock.patch.object(app_web, "OP_WAIT", 0.1), \
                mock.patch.object(self.sync.ops, "run", side_effect=lambda row: release.wait(5) and ("done", "ok")):
            self.sync.inbox_once()
            self.sync.inbox_once()
            self.assertEqual(self.sync.inbox_pause(), web_sync.POLL_SECONDS)
            release.set()
            self.assertTrue(self.wait_for(lambda: len(self.web.acks) == 1))
        self.sync.inbox_once()
        self.assertEqual(self.sync.inbox_pause(), 0)

    def test_what_an_operation_touched_is_pushed_by_the_touch_thread(self):
        self.topic()
        self.sync.load_sent()
        self.web.op("seen", 1, "op-t1", TOPIC)
        self.sync.inbox_once()
        self.assertTrue(self.wait_for(lambda: self.sync.touch_event.is_set()))
        self.assertGreater(self.sync.touch_once(), 0)
        self.assertIn(TOPIC, [item["key"] for item in self.web.items("topic")])
        self.assertTrue(any(line.startswith("web sync touch steps=") for line in self.logs) or self.sync.pace.seconds.get("touch"))

    def test_an_older_read_of_a_topic_is_never_posted_after_a_newer_one(self):
        item = lambda rev: {"type": "topic", "key": TOPIC, "rev": rev, "data": {}}  # noqa: E731
        with self.sync.backlog_lock:
            self.assertEqual(self.sync.fresh_items([(TOPIC, 7, [item("after-answer")])]), [item("after-answer")])
            self.assertEqual(self.sync.fresh_items([(TOPIC, 5, [item("before-answer")])]), [])  # The quick look read it before the touch.
            self.assertEqual(self.sync.fresh[TOPIC], 7)


class TouchPastStuckTest(WebCase):
    """2026-11-26 T837: the owner answered T732's question on the page at 18:17:15. Covey took the answer and opened the next turn
    within two seconds, yet the page said 等你 · 第 10 轮, the question open and no answer of his, until 18:20:43, and his 不处理
    on it came back 没办成. A quick look's read of T732 had hung on the network disk at 18:17:05 and was left behind at 18:17:20;
    every look after it, the touches for the answer too, left T732 out while that read lasted. A touch reads such a topic again
    (a hung call is that call's); only a touch's own hung read leaves its topic to the quick look, and nothing is dropped silently."""

    def wait_for(self, check, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.02)
        return check()

    def hang_a_quick_look(self, topic=TOPIC):
        """A quick look's read of ``topic`` hangs and is left behind (WebSync.loop's Stuck, then start_over_hot's leave_out)."""
        hang = threading.Event()
        self.addCleanup(hang.set)
        thread = threading.Thread(target=hang.wait, args=(10,), daemon=True)
        thread.start()
        self.sync.leave_out(SimpleNamespace(at=topic, thread=thread), "push")
        return hang

    def test_the_answer_reaches_the_page_while_a_quick_look_is_stuck_on_its_topic(self):
        self.topic(status="waiting_user")
        self.question()
        self.sync.push_once(T0)
        self.assertEqual([item["data"]["state"] for item in self.web.items("question")], ["open"])
        self.hang_a_quick_look()
        self.assertIn(TOPIC, self.sync.shared.alive_stuck())
        path = self.state / "questions" / f"{TOKEN}.json"

        def submit(ctx, chat, token, fields, event, by=None):
            # As app/questions.py: the answer is saved (submitting) at once; the history line, the next turn and answered after the ack.
            write_json(path, {**read_object(path), "status": "submitting"})

            def follow_up():
                append_history(self.state / "topics" / TOPIC / "history.jsonl", "user", "你自己点呗", {"message_id": "answer-" + TOKEN})
                write_job(self.state, f"k-{TOPIC[:4]}", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="continued", model="opus",
                          card_message_id="om_card")
                write_job(self.state, "k-next", topic_id=TOPIC, chat_id=CHAT, job_id=8, status="running", model="opus",
                          message_id="answer-" + TOKEN)
                write_json(path, {**read_object(path), "status": "answered", "answers": {"q1": {"answers": ["你自己点呗"]}},
                                  "continuation_job_id": 8})
            card_lanes.after_ack(follow_up)
        self.app.questions.submit_answer = submit
        self.web.op("answer", 1, "op-a1", TOPIC, token=TOKEN, answers={"q1": "你自己点呗"})
        self.sync.inbox_once()
        self.assertTrue(self.wait_for(lambda: not self.sync.op_lanes.lanes))  # Done, acknowledged, its follow-up run.
        self.assertEqual(self.web.acks[0]["state"], "done")
        self.assertGreater(self.sync.touch_once(T0 + 1), 0)
        self.assertEqual(self.web.items("question")[-1]["data"]["state"], "answered")
        self.assertEqual(self.web.items("topic")[-1]["data"]["state"], "在做")
        self.assertTrue([item for item in self.web.items("turn") if item["data"].get("body") == "你自己点呗"])

    def test_a_touchs_own_hung_read_leaves_that_topic_to_the_quick_look_and_says_so(self):
        self.topic()
        self.topic(OTHER, number=8, title="写周报")
        self.sync.push_once(T0)
        hang, reads = threading.Event(), []
        self.addCleanup(hang.set)
        real = app_collect.Collector.topic_items

        def topic_items(collector, state, chat, topic, records, now):
            if topic == TOPIC and not hang.is_set():
                reads.append(topic)
                hang.wait(10)
            return real(collector, state, chat, topic, records, now)

        def touch():
            count = self.sync.touch_once()
            touched = {item["key"][:32] for item in self.web.items()[before:]}
            if step["n"] == 0 and OTHER in touched:  # The others were put back and sent while the touch was stuck on TOPIC.
                step["n"] = 1
                self.sync.touch_soon([TOPIC])
            elif step["n"] == 1 and any("leaves topic " + TOPIC[:12] + " to the quick look" in line for line in self.logs):
                step["n"] = 2
                hang.set()
            elif step["n"] == 2 and not self.sync.touch_hung_now():
                step["n"] = 3
                self.sync.touch_soon([TOPIC])
            elif step["n"] == 3 and TOPIC in touched:
                self.runtime.shutdown.set()
            return count
        step, before = {"n": 0}, len(self.web.items())
        for topic in (TOPIC, OTHER):  # Something of each changed: the page gets them again.
            append_history(self.state / "topics" / topic / "history.jsonl", "user", "再看看", {"message_id": "m-again"})
        guard = threading.Timer(8, self.runtime.shutdown.set)
        guard.start()
        self.addCleanup(guard.cancel)
        self.sync.touch_soon([TOPIC, OTHER])
        with mock.patch.object(app_collect.Collector, "topic_items", topic_items), mock.patch.object(app_web, "MAX_LEFT", 2):
            self.sync.loop("touch", touch, lambda: 0, self.sync.start_over_touch, lambda: self.sync.toucher.moved, stuck=0.2,
                           where=lambda: self.sync.toucher.doing)
        self.assertEqual(step["n"], 3)
        self.assertEqual(reads, [TOPIC])  # Read once while hung: the touch did not hang on it again.
        self.assertTrue(any("web sync touch stuck" in line for line in self.logs))

    def test_a_touch_stuck_where_a_quick_look_is_stuck_too_keeps_the_quick_looks_thread(self):
        """The quick look leaves the topic out until every read stuck on it has returned, its own and the touch's."""
        self.topic()
        hang = self.hang_a_quick_look()
        looked = self.sync.shared.stuck[TOPIC]
        touch_hang = threading.Event()
        self.addCleanup(touch_hang.set)
        touch = threading.Thread(target=touch_hang.wait, args=(10,), daemon=True)
        touch.start()
        self.sync.toucher.at, self.sync.toucher.thread, self.sync.toucher.touching = TOPIC, touch, {TOPIC}
        self.sync.start_over_touch()
        self.assertEqual(self.sync.shared.stuck[TOPIC], (looked, touch))  # Both held: the topic is out while either lives.
        self.assertEqual(self.sync.touch_hung_now(), {TOPIC})
        touch_hang.set()
        touch.join(5)
        self.assertEqual(self.sync.touch_hung_now(), set())
        self.assertIn(TOPIC, self.sync.shared.alive_stuck())  # The quick look's read has not returned yet.
        self.assertIs(self.sync.shared.stuck[TOPIC], looked)
        hang.set()
        looked.join(5)
        self.assertNotIn(TOPIC, self.sync.shared.alive_stuck())

    def test_a_touch_given_up_for_a_busy_store_is_logged_and_tried_again(self):
        from covey.core import store
        self.topic()
        self.sync.push_once(T0)
        real, calls = app_collect.Collector.collect_touched, []

        def collect(collector, chat, topics, now, skip=()):
            calls.append(list(topics))
            if len(calls) == 1:
                raise store.Busy("jobs")
            return real(collector, chat, topics, now, skip)
        with mock.patch.object(app_collect.Collector, "collect_touched", collect):
            self.sync.touch_soon([TOPIC])
            self.assertEqual(self.sync.touch_once(T0 + 1), 0)
            self.assertTrue(any(line.startswith("web sync touch waits: the jobs index") for line in self.logs))
            self.sync.touch_once(T0 + 2)
        self.assertEqual(calls, [[TOPIC], [TOPIC]])
        self.assertTrue(any(line.startswith("web sync touch goes on") for line in self.logs))

    def test_a_touch_the_page_did_not_take_is_sent_again(self):
        self.topic(status="waiting_user")
        self.question()
        self.sync.push_once(T0)
        path = self.state / "questions" / f"{TOKEN}.json"
        write_json(path, {**read_object(path), "status": "answered", "answers": {"q1": {"answers": ["A"]}}})
        self.web.fail = 500
        self.sync.touch_soon([TOPIC])
        with self.assertRaises(WebError):
            self.sync.touch_once(T0 + 1)
        self.web.fail = 0
        self.assertGreater(self.sync.touch_once(T0 + 2), 0)
        self.assertEqual(self.web.items("question")[-1]["data"]["state"], "answered")


class RereadTest(WebCase):
    """2026-11-26 T837, the half without a click: a topic the quick look leaves out while a read of it hangs on the network disk stood still
    on the page for as long as that read lasted (T732: 18:17:05 to 18:20:43, its new turn running all the while). web-reread reads such a
    topic again in a thread of its own, one at a time, and holds back after a reread that hangs too (backoff, a cap on stuck rereads)."""

    def hang_a_quick_look(self, topic=TOPIC):
        hang = threading.Event()
        self.addCleanup(hang.set)
        thread = threading.Thread(target=hang.wait, args=(10,), daemon=True)
        thread.start()
        self.sync.leave_out(SimpleNamespace(at=topic, thread=thread), "push")
        return hang, thread

    def states(self):
        return [item["data"]["state"] for item in self.web.items("topic") if item["key"] == TOPIC]

    def test_a_topic_whose_run_ended_while_the_quick_look_was_stuck_on_it_reaches_the_page(self):
        self.topic(status="running")
        self.sync.push_once(T0)
        self.assertEqual(self.states(), ["在做"])
        self.hang_a_quick_look()
        write_job(self.state, f"k-{TOPIC[:4]}", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", card_message_id="om_card")
        append_history(self.state / "topics" / TOPIC / "history.jsonl", "assistant", "做完了", {"message_id": "m-done"})
        self.sync.push_once(T0 + 1)
        self.assertEqual(self.states(), ["在做"])  # The quick look leaves it out: before web-reread, so it stayed until the read came back.
        with mock.patch.object(app_web, "REREAD_AFTER", 0):
            self.assertGreater(self.sync.reread_once(T0 + 2), 0)
            self.assertEqual(self.states(), ["在做", "做完"])
            self.assertEqual(self.sync.reread_once(T0 + 3), 0)  # Quiet now and read since: not read again.
        self.assertTrue(any(line.startswith(f"web sync reread topic {TOPIC[:12]}: read again in") for line in self.logs))

    def test_a_running_one_is_read_again_every_gap_and_once_more_when_it_ends(self):
        self.topic(status="running")
        self.sync.push_once(T0)
        self.hang_a_quick_look()
        history = self.state / "topics" / TOPIC / "history.jsonl"
        with mock.patch.object(app_web, "REREAD_AFTER", 0), mock.patch.object(app_web, "REREAD_GAP", 0.3):
            self.sync.reread_once(T0 + 1)
            self.assertEqual(self.sync.rereads[TOPIC]["reads"], 1)
            append_history(history, "user", "再看看", {"message_id": "m-again"})
            self.assertEqual(self.sync.reread_once(T0 + 2), 0)  # Within the gap.
            time.sleep(0.35)
            self.assertGreater(self.sync.reread_once(T0 + 3), 0)  # Running (hot): read again after the gap.
            write_job(self.state, f"k-{TOPIC[:4]}", topic_id=TOPIC, chat_id=CHAT, job_id=7, status="done", model="opus", card_message_id="om_card")
            self.sync.push_once(T0 + 4)  # The quick look finds it no longer runs.
            self.assertNotIn(TOPIC, self.sync.collector.hot)
            time.sleep(0.35)
            self.assertGreater(self.sync.reread_once(T0 + 5), 0)  # Once more: it ended since the last reread.
            time.sleep(0.35)
            self.assertEqual(self.sync.reread_once(T0 + 6), 0)
        self.assertEqual(self.sync.rereads[TOPIC]["reads"], 3)
        self.assertEqual(self.states()[-1], "做完")

    def test_a_reread_that_hangs_too_backs_off_and_at_the_cap_none_starts(self):
        self.topic(status="running")
        self.topic(OTHER, number=8, title="写周报", status="running")
        self.sync.push_once(T0)
        self.hang_a_quick_look(TOPIC)
        self.hang_a_quick_look(OTHER)
        for topic in (TOPIC, OTHER):
            append_history(self.state / "topics" / topic / "history.jsonl", "user", "再看看", {"message_id": "m-again"})
        hang, reads, seen = threading.Event(), [], {}
        self.addCleanup(hang.set)
        real = app_collect.Collector.topic_items

        def topic_items(collector, state, chat, topic, records, now):
            reads.append(topic)
            if topic == TOPIC and not hang.is_set():
                hang.wait(10)
            return real(collector, state, chat, topic, records, now)

        def reread():
            count = self.sync.reread_once()
            pushed = {item["key"][:32] for item in self.web.items()[before:]}
            if step["n"] == 0 and any(line.startswith("web sync reread waits: 1 rereads still stuck") for line in self.logs):
                step["n"], seen["other_at_cap"] = 1, OTHER in pushed or OTHER in reads
                hang.set()
            elif step["n"] == 1 and OTHER in pushed:
                step["n"] = 2
                self.runtime.shutdown.set()
            return count
        step, before = {"n": 0}, len(self.web.items())
        guard = threading.Timer(8, self.runtime.shutdown.set)
        guard.start()
        self.addCleanup(guard.cancel)
        with mock.patch.object(app_collect.Collector, "topic_items", topic_items), mock.patch.object(app_web, "REREAD_AFTER", 0), \
                mock.patch.object(app_web, "REREAD_MAX_LEFT", 1), mock.patch.object(app_web, "REREAD_BACKOFF", 30):
            self.sync.loop("reread", reread, lambda: 0.01, self.sync.start_over_reread, lambda: self.sync.rereader.moved, stuck=0.2,
                           where=lambda: self.sync.rereader.doing)
        self.assertEqual(step["n"], 2)
        self.assertFalse(seen["other_at_cap"])  # At the cap no reread started, not even of another topic.
        self.assertEqual(reads.count(TOPIC), 1)  # Its backoff: not read again.
        self.assertTrue(any(line.startswith(f"web sync reread of topic {TOPIC[:12]} hung too") for line in self.logs))
        self.assertTrue(any(line == "web sync reread goes on: a stuck reread ended" for line in self.logs))
        self.assertIn(TOPIC, self.sync.rereads)
        self.assertGreater(self.sync.rereads[TOPIC]["next"], time.monotonic() + 20)

    def test_the_touch_and_the_reread_build_turns_with_the_images_as_the_quick_look_does(self):
        """A collector without the images' ledger builds turns with no images: a touch's push took them off the page until the next quick look."""
        self.assertIsNotNone(self.sync.images)
        for collector in (self.sync.collector, self.sync.passer, self.sync.toucher, self.sync.rereader):
            self.assertIs(collector.images, self.sync.images)

    def test_a_stuck_read_that_comes_back_is_logged_and_its_topic_dropped(self):
        self.topic()
        self.sync.push_once(T0)
        hang, thread = self.hang_a_quick_look()
        with mock.patch.object(app_web, "REREAD_AFTER", 0):
            self.sync.reread_once(T0 + 1)
        self.assertIn(TOPIC, self.sync.rereads)
        hang.set()
        thread.join(5)
        self.sync.reread_once(T0 + 2)
        self.assertEqual(self.sync.rereads, {})
        self.assertTrue(any(line.startswith(f"web sync push read of topic {TOPIC[:12]} came back after") and line.endswith("read again 1 times meanwhile")
                            for line in self.logs))


class KeepAliveTest(unittest.TestCase):
    """core/http_upstream.py KeepAlive: one connection per thread and host; one the server closed while idle is replaced once."""

    def serve(self, close_each=False):
        import http.server
        conns = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                conns.append(self.client_address)

            def do_GET(self):
                body = b'{"items": []}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = close_each  # Kept alive as far as the client knows, then closed: an idle timeout.

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/sync/inbox?after=1", conns

    def kept(self):
        import http.client
        kept = http_upstream.KeepAlive(factory=lambda host, port, timeout: http.client.HTTPConnection(host, port, timeout=timeout))
        self.addCleanup(kept.close)
        return kept

    def test_requests_share_one_connection(self):
        url, conns = self.serve()
        kept = self.kept()
        for _ in range(3):
            self.assertEqual(kept.send("GET", url, None, {}, 5), (200, b'{"items": []}'))
        self.assertEqual(len(conns), 1)

    def test_a_thread_takes_the_connection_another_left_idle(self):
        """The page's operations are acknowledged from short-lived lane threads (app/web_sync.py OpLanes)."""
        url, conns = self.serve()
        kept = self.kept()
        for _ in range(3):
            worker = threading.Thread(target=kept.send, args=("GET", url, None, {}, 5))
            worker.start()
            worker.join(5)
        self.assertEqual(len(conns), 1)

    def test_a_connection_closed_while_idle_is_replaced_once(self):
        url, conns = self.serve(close_each=True)
        kept = self.kept()
        for _ in range(3):
            self.assertEqual(kept.send("GET", url, None, {}, 5)[0], 200)
        self.assertEqual(len(conns), 3)

    def test_no_answer_is_an_os_error(self):
        kept = self.kept()
        with self.assertRaises(OSError):
            kept.send("GET", "http://127.0.0.1:9/sync/inbox", None, {}, 2)


class PaceTest(unittest.TestCase):
    def test_a_line_a_minute_with_the_step_times(self):
        lines, now = [], [0.0]
        pace = app_web.Pace(lines.append, every=60, clock=lambda: now[0])
        for at, seconds in ((0, 0.2), (20, 3.0), (40, 1.0)):
            now[0] = at
            pace.note("push", seconds)
        self.assertEqual(lines, [])
        now[0] = 61
        pace.note("push", 0.5)
        self.assertEqual(lines, ["web sync push steps=4 p50=1000ms p90=3000ms max=3000ms"])

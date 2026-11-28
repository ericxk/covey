"""协作的执行闭环 (app/collab_flow.py, docs/architecture.md 多参与者协作; 验收矩阵 C1–C9): the owner's turn of a topic asks
another topic; the ask runs in a channel of its own, a read-only fork of the helper's session, with no card; its authorized
reply goes back to the integrator, whose continuation delivers; and the helper's own things are exactly as they were
(R17/R18): its session pointer, history, 回退 marker, records and latest delivery, the butler's snapshot record, a lead's
receipts."""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from unittest import mock

from support.world import UnifiedCase
from covey.app import collab_channel, collab_flow, tool_limits
from covey.core import collab, projects, turn_source
from covey.core import collab_records as records
from covey.core.harness import CONSULT, TOOL_LIMIT, JobCancelled
from covey.core.history_storage import append_history
from covey.core.jobs import context_for_job, find_job_record, topic_job_records, upsert_job_record
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.saved_results import authorize_verified_reply, observe_verified_reply
from covey.core.state import read_object, write_json
from covey.core.topics import load_topic, topic_file

SESSION = "11111111-2222-4333-8444-555555555555"


def tree(path: Path) -> dict:
    """Every file under ``path`` with its sha256: what a helper's topic folder holds."""
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


class Case(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {turn_source.SWITCH: "1"}))
        self.app.runtime.access.agent_chat = lambda chat, group=None: chat == "chat"
        # The 协作卡 (app/collab_cards.py) is not sent here (tests/test_collab_cards.py has its own CardKit): the line it would
        # replace goes out as the fallback, and no request leaves for Feishu.
        self.app.messenger.create_and_send_card = mock.Mock(return_value=("", ""))
        self.state = self.ctx.state_dir
        self.flow = self.app.collab
        self.jobs = 0

    def topic(self, mid, *, status="done", session=True):
        """A topic of the owner's private chat on Claude Code whose first turn ended ``status``: its id."""
        self.receive(mid, "initial " + mid)
        _, ctx = self.route(mid)
        self.app.topics.select_harness(ctx, "chat", "claude")
        app, ctx = self.route(mid)
        app.submission.start_workspace(ctx, "chat", ctx.workspace_name)
        job = self.app.runtime.jobs.queue.get_nowait()
        upsert_job_record(self.state, job.message_id, status=status, prompt_uuid="u-" + mid, prompt_session=SESSION,
                          final_text="T done " + mid)
        if status != "waiting_user":
            self.app.runtime.unregister_reply_job(job)
        if session:
            (topic_file(self.state, ctx.topic_id).parent / "claude_session_id").write_text(SESSION)
        return ctx.topic_id

    def label(self, topic):
        return f"T{load_topic(self.state, topic)['topic_number']}"

    def root_turn(self, topic, asks, *, key="om_root", text=None, job_id=50, status="done", **fields):
        """A turn of ``topic`` the owner started, ended ``status``, whose checkpoint asks ``asks``: its job."""
        checkpoint = self.state / "checkpoints" / f"{key}.json"
        write_json(checkpoint, {"phase": "完成", collab_flow.FIELD: asks}, ensure_ascii=False)
        words = text if text is not None else "问问 " + " ".join(item["to"] for item in asks) + " 那边是怎么定的"
        append_history(topic_file(self.state, topic).parent / "history.jsonl", "user", words, {"message_id": key})
        upsert_job_record(self.state, key, **{"status": status, "verified_reply_authorized": True, "job_id": job_id,
                                              "topic_id": topic, "chat_id": "chat", "message_id": key, "sender_id": "owner",
                                              "user_text": words, "checkpoint_path": str(checkpoint), "backend": "claude",
                                              "harness_id": "claude", **fields})
        return find_job_record(self.state, key)

    def end(self, record):
        """The Bridge once ``record``'s runner is gone."""
        self.flow.after_runner(type("Job", (), {"message_id": record["message_id"], "job_id": record.get("job_id")})(),
                               self.ctx)

    def queued(self):
        jobs = []
        while not self.app.runtime.jobs.queue.empty():
            jobs.append(self.app.runtime.jobs.queue.get_nowait())
        return jobs

    def answer(self, job, text, status="done"):
        """``job`` (an ask's or a continuation's) ended ``status``; ``text`` its saved, authorized reply."""
        app = self.app.apps[job.harness_id]
        ctx = context_for_job(app.harness.context, job)
        name = app.harness.spec.name
        if text:
            job.bridge_run_id = str(uuid.uuid4())
            reply = VerifiedRootReply(backend=name, run_id=job.bridge_run_id, session_id="22222222-2222-4333-8444-555555555555",
                                      boundary=("u1",), message_ids=("a1",), text=text)
            observe_verified_reply(ctx, job, name, reply)
            outcome = ExecutionOutcome(status="completed", identity_confirmed=True, cleanup_confirmed=True,
                                       protection_confirmed=True, resume_allowed=True, reply_fingerprint=reply.fingerprint)
            self.assertTrue(authorize_verified_reply(ctx, job, name, reply, outcome))
        upsert_job_record(self.state, job.message_id, status=status)
        self.app.runtime.unregister_reply_job(job)
        self.flow.after_runner(job, self.ctx)

    def record(self):
        [found] = records.records(self.state)
        return found


class StartTest(Case):
    def test_an_owner_turn_asking_another_topic_queues_one_read_only_ask_in_a_channel_and_touches_nothing_of_the_helper(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        before = tree(topic_file(self.state, helper).parent)
        records_before = sorted(r["message_id"] for r in topic_job_records(self.state, helper))
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "方案定的是哪个？"}]))
        record = self.record()
        [ask] = record["asks"]
        self.assertEqual((record["state"], ask["state"], ask["number"]), (collab.OPEN, collab.TAKEN, 1))
        self.assertEqual(record["integrator"]["key"], integrator)
        self.assertEqual(record["root"]["message_id"], "om_root")
        [job] = self.queued()
        channel = collab.channel_id(record["id"], helper)
        self.assertEqual((job.topic_id, job.message_id), (channel, collab.ask_message(ask["id"])))
        self.assertEqual(turn_source.kind(job), turn_source.ASK)
        self.assertEqual((job.turn_source["collab"], job.turn_source["ask"], job.turn_source["level"]),
                         (record["id"], ask["id"], turn_source.LEVEL_AGENT))  # The integrator's words, not the owner's.
        self.assertTrue(job.card_deferred)  # No task card: nothing of the ask is in the chat.
        self.assertEqual(job.sender_id, "owner")  # In the owner's private chat: his memory and principles are read.
        bound, source = load_topic(self.state, channel), load_topic(self.state, helper)
        for key in ("harness_id", "workdir", "workspace_name"):
            self.assertEqual(bound.get(key), source.get(key), key)
        self.assertEqual(bound[collab_channel.FIELD]["helper"], helper)
        self.assertNotIn("topic_number", bound)
        # R17/R18: the helper's folder (session pointer, history, topic.json, 回退) and its records as they were.
        self.assertEqual(tree(topic_file(self.state, helper).parent), before)
        self.assertEqual(sorted(r["message_id"] for r in topic_job_records(self.state, helper)), records_before)
        self.assertTrue(any("🤝 协作" in text and f"已请 {self.label(helper)} 协助" in text for text in self.said()))

    def test_the_same_turn_read_twice_starts_one_collaboration(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        root = self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}])
        self.end(root)
        self.end(root)
        self.assertEqual(len(records.records(self.state)), 1)
        self.assertEqual(len(self.queued()), 1)

    def test_the_owner_names_three_at_most_and_two_unnamed_nothing_sent_when_more(self):
        helpers = [self.topic(f"om_h{n}") for n in range(4)]
        integrator = self.topic("om_i")
        asks = [{"to": self.label(h), "question": "?"} for h in helpers]
        self.end(self.root_turn(integrator, asks, key="om_four"))
        self.assertEqual(records.records(self.state), [])
        self.assertTrue(any("一次最多请 3 位" in text for text in self.said()))
        # Not named by the owner: two at most.
        self.end(self.root_turn(integrator, asks[:3], key="om_three", text="帮我问问其他几个话题", job_id=51))
        self.assertEqual(records.records(self.state), [])
        self.assertTrue(any("一次最多请 2 位" in text for text in self.said()))
        self.end(self.root_turn(integrator, asks[:3], key="om_named", job_id=52))
        self.assertEqual(len(self.record()["asks"]), 3)

    def test_only_the_owners_own_turn_starts_one(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        ask = [{"to": self.label(helper), "question": "?"}]
        for key, fields in (("lead-p1-1", {}), ("om_retry", {"auto_retry": 1}), ("om_other", {"sender_id": "ou_x"}),
                            ("om_failed", {"status": "failed"})):
            with self.subTest(key):
                self.end(self.root_turn(integrator, ask, key=key, **fields))
        self.assertEqual(records.records(self.state), [])
        self.assertEqual(self.queued(), [])

    def test_a_task_runner_after_its_turn_leaves_it_to_the_bridge(self):
        # app/job_dispatch.py runs the end-of-turn hooks in the runner too: only the Bridge writes and queues.
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        self.app.runtime.runner = object()
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}]))
        self.assertEqual(records.records(self.state), [])
        self.app.runtime.runner = None
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}]))
        self.assertEqual(len(records.records(self.state)), 1)

    def test_off_nothing_is_read_or_written(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        with mock.patch.dict(os.environ, {turn_source.SWITCH: ""}):
            self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}]))
            self.assertEqual(self.flow.threads(), ())
        self.assertFalse((self.state / records.FOLDER).exists())


class ReceiverTest(Case):
    """C3: the helper running, waiting for the owner's answer, never started, stopped by the owner; C9: device, Harness."""

    def ask(self, helper, integrator, key="om_root"):
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}], key=key))
        return self.record()["asks"][0]

    def test_a_busy_helper_keeps_the_ask_queued_never_steered_and_it_goes_once_idle(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        upsert_job_record(self.state, "om_busy", job_id=60, topic_id=helper, chat_id="chat", status="running",
                          message_id="om_busy")
        ask = self.ask(helper, integrator)
        self.assertEqual(ask["state"], collab.QUEUED)
        self.assertEqual(self.queued(), [])
        upsert_job_record(self.state, "om_busy", status="done")
        self.flow.tick(self.ctx)
        self.assertEqual(self.record()["asks"][0]["state"], collab.TAKEN)
        self.assertEqual(len(self.queued()), 1)

    def test_queued_past_its_wait_it_expires_and_the_integrator_goes_on_with_the_gap(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        upsert_job_record(self.state, "om_busy", job_id=60, topic_id=helper, chat_id="chat", status="running",
                          message_id="om_busy")
        self.ask(helper, integrator)
        self.flow.tick(self.ctx, now=time.time() + collab.QUEUE_SECONDS + 1)
        record = self.record()
        self.assertEqual(record["asks"][0]["state"], collab.EXPIRED)
        [reply] = self.queued()
        self.assertEqual((reply.topic_id, turn_source.kind(reply)), (integrator, turn_source.REPLY))
        self.assertIn("到期", reply.user_text)
        self.assertEqual(find_job_record(self.state, "om_root")["status"], "done")  # The owner's turn is not failed.

    def test_the_queue_and_the_window_can_be_set(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        upsert_job_record(self.state, "om_busy", job_id=60, topic_id=helper, chat_id="chat", status="running",
                          message_id="om_busy")
        with mock.patch.dict(os.environ, {"COVEY_COLLAB_QUEUE_SECONDS": "60", "COVEY_COLLAB_WINDOW_SECONDS": "120"}):
            ask = self.ask(helper, integrator)
            record = self.record()
            self.assertAlmostEqual(ask["deadline"] - ask["created_at"], 60, delta=1)
            self.assertAlmostEqual(record["deadline"] - record["created_at"], 120, delta=1)
            self.flow.tick(self.ctx, now=time.time() + 61)
        self.assertEqual(self.record()["asks"][0]["state"], collab.EXPIRED)
        self.assertIn("排队超过 1 分钟", self.record()["asks"][0]["reason"])

    def test_a_helper_waiting_for_the_owners_answer_is_not_taken_and_its_question_still_waits(self):
        integrator, waiting = self.topic("om_i"), self.topic("om_w", status="waiting_user")
        ask = self.ask(waiting, integrator)
        self.assertEqual(ask["state"], collab.NOT_TAKEN)
        self.assertIn("在等主人回答", ask["reason"])
        self.assertEqual(find_job_record(self.state, "om_w")["status"], "waiting_user")
        [reply] = self.queued()  # The integrator goes on with the gap.
        self.assertEqual(reply.topic_id, integrator)

    def test_a_helper_the_owner_stopped_or_that_never_started_is_not_taken(self):
        from covey.app import collab_checks as checks

        integrator, stopped = self.topic("om_i"), self.topic("om_s", status="cancelled")
        upsert_job_record(self.state, "om_s", cancel_requested_by="card", timings={"backend_requested": time.time()},
                          execution_status="cancelled")
        ask = self.ask(stopped, integrator)
        self.assertEqual(ask["state"], collab.NOT_TAKEN)
        self.assertIn("主人停了", ask["reason"])
        self.receive("om_new", "还没开始")
        _, ctx = self.route("om_new")
        self.assertEqual(checks.receiver_now(self.app.runtime, self.state, "chat", ctx.topic_id), checks.NEVER_RAN)

    def test_a_topic_on_a_device_or_on_a_harness_without_the_capability_is_not_taken(self):
        from covey.app import collab_checks as checks
        from covey.core import devices
        from covey.core.harness import HarnessCapabilities

        helper, integrator = self.topic("om_h"), self.topic("om_i")
        data = load_topic(self.state, helper)
        write_json(topic_file(self.state, helper), {**data, "workdir": "gpu1:/home/me/p"}, ensure_ascii=False)
        with mock.patch.object(devices, "enabled", return_value=True):
            ask = self.ask(helper, integrator)
        self.assertEqual((ask["state"], ask["reason"]), (collab.NOT_TAKEN, checks.DEVICE))
        other = self.topic("om_p")
        who = checks.participant(self.state, "chat", integrator)
        for caps in (HarnessCapabilities(read_only=True), HarnessCapabilities(collab=True)):
            with mock.patch.object(checks, "capabilities", return_value=(self.app.apps["claude"], caps)):
                reason = checks.helper_refusal(self.app.topics, self.state, "chat", who, other, owner_named=True)
            self.assertIn("还不能只读咨询", reason)

    def test_another_project_unnamed_is_not_taken(self):
        from covey.app import collab_checks as checks

        helper, integrator = self.topic("om_h"), self.topic("om_i")
        who = checks.participant(self.state, "chat", integrator)
        data = load_topic(self.state, helper)
        write_json(topic_file(self.state, helper), {**data, "project": {"id": "p1p1p1", "role": projects.MEMBER}},
                   ensure_ascii=False)
        self.assertIn("跨专项", checks.helper_refusal(self.app.topics, self.state, "chat", who, helper, owner_named=False))
        self.assertEqual(checks.helper_refusal(self.app.topics, self.state, "chat", who, helper, owner_named=True), "")

    def test_a_group_or_a_proxy_chat_starts_nothing(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        self.app.runtime.access.agent_chat = lambda chat, group=None: False
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}]))
        self.assertEqual(records.records(self.state), [])
        self.assertTrue(any("协作只在主人的私聊里发起" in text for text in self.said()))


class AnswerTest(Case):
    """C1/C2: the helper answers in its fork, the integrator goes on with the answer and delivers; the helper's latest
    delivery and project stay its own."""

    def asked(self):
        self.helper, self.integrator = self.topic("om_h"), self.topic("om_i")
        self.before = tree(topic_file(self.state, self.helper).parent)
        self.end(self.root_turn(self.integrator, [{"to": self.label(self.helper), "question": "方案定的是哪个？"}]))
        [job] = self.queued()
        return job

    def test_the_ask_turn_runs_held_to_consult_in_a_fork_with_no_checkpoint(self):
        job = self.asked()
        app = self.app.apps["claude"]
        target = context_for_job(app.harness.context, job)
        seen = []

        def run(request):
            seen.append(request)
            raise JobCancelled("stop here")
        with mock.patch.object(app.backend, "run", side_effect=run), \
                mock.patch.object(collab_channel, "_transcript", return_value=True):
            try:
                app.runs.generate_reply_for_worker(target, job, None, None)
            except JobCancelled:
                pass
        [request] = seen
        self.assertEqual(request.context.options[TOOL_LIMIT], CONSULT)
        self.assertEqual(request.context.options["fork_session"], SESSION)
        self.assertIn("方案定的是哪个？", request.prompt)
        self.assertTrue(request.prompt.endswith(tool_limits.CONSULT_NOTE))
        self.assertNotIn("checkpoints", request.prompt)
        self.assertIn(collab_channel.RULES, request.instructions or request.prompt)
        self.assertNotIn("requires_user", request.instructions or "")
        evidence = find_job_record(self.state, job.message_id)[collab_channel.EVIDENCE]
        self.assertEqual((evidence["level"], evidence["session"]), (collab.FORKED, SESSION))
        self.assertEqual(tree(topic_file(self.state, self.helper).parent), self.before)

    def test_its_authorized_reply_goes_to_the_integrators_continuation_and_the_helper_gets_one_line(self):
        job = self.asked()
        said = len(self.said())
        self.answer(job, "定的是方案 B（见 docs/plan.md）")
        record = self.record()
        [ask] = record["asks"]
        self.assertEqual(ask["state"], collab.READ)
        self.assertEqual(ask["reply"]["text"], "定的是方案 B（见 docs/plan.md）")
        [reply] = self.queued()
        self.assertEqual((reply.topic_id, reply.message_id), (self.integrator, collab.reply_message(record["id"], 1)))
        self.assertEqual(turn_source.kind(reply), turn_source.REPLY)
        self.assertFalse(turn_source.behavior(reply).owner_words)
        self.assertIn("定的是方案 B", reply.user_text)
        self.assertIn("主人的原话", reply.user_text)
        lines = self.said()[said:]
        self.assertEqual(len([text for text in lines if text.startswith("🤝 已协助")]), 1)
        # Nothing of the ask in the helper's own records; its latest delivery is its own.
        self.assertEqual(tree(topic_file(self.state, self.helper).parent), self.before)
        latest = max(topic_job_records(self.state, self.helper), key=lambda r: r["job_id"])
        self.assertEqual(latest["final_text"], "T done om_h")
        # The continuation ends: the collaboration is done.
        self.answer(reply, "综合 T 的答复：方案 B")
        self.assertEqual(self.record()["state"], collab.DONE)

    def test_a_failed_ask_is_queued_once_more_by_its_record_then_fails_and_no_recovery_card_shows(self):
        job = self.asked()
        with mock.patch.object(self.app.apps["claude"].controls, "show_recovery_card") as recovery:
            self.answer(job, "", status="failed")
            [again] = self.queued()
            self.assertEqual(again.message_id, collab.ask_message(self.record()["asks"][0]["id"], 2))
            self.assertEqual(self.record()["asks"][0]["state"], collab.TAKEN)
            self.answer(again, "", status="failed")
        recovery.assert_not_called()
        record = self.record()
        self.assertEqual(record["asks"][0]["state"], collab.FAILED)
        [reply] = self.queued()
        self.assertIn("停住了", reply.user_text)

    def test_the_owner_stopping_it_stops_its_queued_ask_and_nothing_else_of_the_helper(self):
        job = self.asked()
        upsert_job_record(self.state, "om_other", job_id=90, topic_id=self.helper, chat_id="chat", status="queued",
                          message_id="om_other")
        self.flow.stop(self.ctx, self.record()["id"], by="web")
        record = self.record()
        self.assertEqual((record["state"], record["asks"][0]["state"]), (collab.CANCELLED, collab.CANCELLED))
        self.assertEqual(find_job_record(self.state, job.message_id)["status"], "cancelled")
        self.assertEqual(find_job_record(self.state, "om_other")["status"], "queued")  # The helper's own work goes on.

    def test_the_owner_stopping_it_cancels_it_and_a_late_answer_is_kept_and_wakes_nobody(self):
        job = self.asked()
        self.app.runtime.unregister_reply_job(job)  # Its runner took it and ends on its own, after the stop.
        self.flow.stop(self.ctx, self.record()["id"], by="web")
        record = self.record()
        self.assertEqual((record["state"], record["asks"][0]["state"]), (collab.CANCELLED, collab.CANCELLED))
        self.answer(job, "迟到的答复")
        record = self.record()
        self.assertEqual(record["asks"][0]["state"], collab.CANCELLED)
        self.assertEqual(record["asks"][0]["late"]["text"], "迟到的答复")
        self.assertEqual(self.queued(), [])

    def test_the_owners_words_since_the_root_come_first_in_the_continuation(self):
        job = self.asked()
        upsert_job_record(self.state, "om_new", job_id=70, topic_id=self.integrator, chat_id="chat", status="queued",
                          message_id="om_new", user_text="改成要方案 C 的数据", sender_id="owner")
        self.answer(job, "方案 B")
        [reply] = self.queued()
        note = collab_flow.goal_note(context_for_job(self.app.apps["claude"].harness.context, reply), reply)
        self.assertIn("改成要方案 C 的数据", note)
        self.assertIn("以主人最新的要求为准", note)

    def test_a_clarification_goes_on_the_same_fork_or_forks_anew_when_the_helper_ran_since(self):
        job = self.asked()
        self.answer(job, "方案 B")
        [reply] = self.queued()
        record = self.record()
        channel = collab.channel_id(record["id"], self.helper)
        (topic_file(self.state, channel).parent / "claude_session_id").write_text("33333333-2222-4333-8444-555555555555")
        found = self.flow.clarify(self.ctx, record["id"], self.helper, "B 的基线是哪次？")
        self.assertNotIn("refused", found)
        [second] = self.queued()
        self.assertEqual(second.topic_id, channel)
        self.assertEqual(second.message_id, collab.ask_message(collab.ask_id(record["id"], self.helper, 2)))
        self.assertTrue((topic_file(self.state, channel).parent / "claude_session_id").read_text())  # A: same fork.
        self.assertNotIn("你上一次", second.user_text)
        self.assertIn("每位最多问 2 次", self.flow.clarify(self.ctx, record["id"], self.helper, "三问")["refused"])
        self.assertTrue(self.flow.clarify(self.ctx, record["id"], self.helper, "B 的基线是哪次？")["again"])  # Read twice.
        self.answer(second, "基线是 #12")
        self.assertIn("每位最多问 2 次", self.flow.clarify(self.ctx, record["id"], self.helper, "三问")["refused"])

    def test_a_clarification_waits_for_the_first_answer_and_goes_only_to_a_helper_asked(self):
        self.asked()
        record = self.record()
        self.assertEqual(self.flow.clarify(self.ctx, record["id"], self.helper, "再问")["refused"], "上一次还没有答复")
        other = self.topic("om_o")
        self.assertIn("只能向已经请过的协助方澄清", self.flow.clarify(self.ctx, record["id"], other, "?")["refused"])

    def test_a_clarification_after_the_helper_ran_forks_anew_with_the_first_round(self):
        job = self.asked()
        self.answer(job, "方案 B")
        self.queued()
        record = self.record()
        channel = collab.channel_id(record["id"], self.helper)
        (topic_file(self.state, channel).parent / "claude_session_id").write_text("33333333-2222-4333-8444-555555555555")
        upsert_job_record(self.state, "om_later", job_id=80, topic_id=self.helper, chat_id="chat", status="done",
                          message_id="om_later", user_text="改了方案", sender_id="owner")
        self.flow.clarify(self.ctx, record["id"], self.helper, "现在呢？")
        [second] = self.queued()
        self.assertIn("你上一次（第 1 次）被问到", second.user_text)
        self.assertIn("又跑了 1 轮", second.user_text)
        pointer = topic_file(self.state, channel).parent / "claude_session_id"
        self.assertFalse(pointer.exists() and pointer.read_text().strip())  # B: the channel forks the helper anew.

    def test_a_restart_queues_a_taken_ask_never_queued_and_settles_one_that_ended_unseen(self):
        job = self.asked()
        record = self.record()
        ask = record["asks"][0]
        # As if the Bridge stopped between marking it taken and queuing it: its job record is gone.
        (self.state / "jobs" / f"{job.message_id}.json").unlink()
        self.app.runtime.unregister_reply_job(job)
        self.flow.tick(self.ctx, now=time.time() + collab_flow.LOST + 1)
        [again] = self.queued()
        self.assertEqual(again.message_id, ask["message_id"])
        # It ended while no Bridge watched: the thread settles it from its record, once.
        app = self.app.apps["claude"]
        ctx = context_for_job(app.harness.context, again)
        again.bridge_run_id = str(uuid.uuid4())
        reply = VerifiedRootReply(backend="claude", run_id=again.bridge_run_id, session_id="22222222-2222-4333-8444-555555555555",
                                  boundary=("u1",), message_ids=("a1",), text="答复")
        observe_verified_reply(ctx, again, "claude", reply)
        authorize_verified_reply(ctx, again, "claude", reply, ExecutionOutcome(
            status="completed", identity_confirmed=True, cleanup_confirmed=True, protection_confirmed=True,
            resume_allowed=True, reply_fingerprint=reply.fingerprint))
        upsert_job_record(self.state, again.message_id, status="done")
        self.app.runtime.unregister_reply_job(again)
        self.flow.tick(self.ctx)
        self.flow.tick(self.ctx)
        self.assertEqual(self.record()["asks"][0]["state"], collab.READ)
        self.assertEqual(len(self.queued()), 1)  # One continuation, not two.

    def continued(self):
        """The integrator's first continuation, queued once the helper answered."""
        job = self.asked()
        self.answer(job, "方案 B")
        [reply] = self.queued()
        return reply

    def test_a_failed_continuation_is_queued_once_more_then_the_collaboration_ends_with_a_line(self):
        """W2-C: one that did not end well is queued again under a message id of its own (the failed one's 检查并继续 finds
        nothing to retry); the second ends the collaboration as the Bridge's and says so in the integrator's topic."""
        reply = self.continued()
        cid = self.record()["id"]
        self.answer(reply, "", status="failed")
        [again] = self.queued()
        self.assertEqual(again.message_id, collab.reply_message(cid, 1, 2))
        self.assertEqual(turn_source.kind(again), turn_source.REPLY)
        record = self.record()
        self.assertEqual((record["state"], record["replies"][0]["state"], record["replies"][0]["attempt"]), (collab.OPEN, "queued", 2))
        self.assertEqual(find_job_record(self.state, reply.message_id)["retried_by"], again.message_id)
        said = len(self.said())
        self.answer(again, "", status="failed")
        record = self.record()
        self.assertEqual((record["state"], record["stopped_by"]), (collab.CANCELLED, "bridge"))
        self.assertEqual(self.queued(), [])
        [line] = [text for text in self.said()[said:] if "Bridge 取消了这次协作" in text]
        self.assertIn("试了 2 次", line)
        self.flow.tick(self.ctx, now=time.time() + collab_flow.LOST + 1)  # Nothing is open any more: nothing again.
        self.assertEqual(self.queued(), [])

    def stopped_by(self, by):
        reply = self.continued()
        upsert_job_record(self.state, reply.message_id, **({"cancel_requested_by": by} if by else {}))
        self.answer(reply, "", status="cancelled")
        [again] = self.queued()
        self.assertEqual(again.message_id, collab.reply_message(self.record()["id"], 1, 2))
        self.assertEqual(self.record()["state"], collab.OPEN)

    def test_a_continuation_stopped_with_no_name_is_not_taken_for_the_owners_stop(self):
        """W2-C: a stop that names nobody was taken for the owner's card before, and the collaboration was cancelled."""
        self.stopped_by("")

    def test_a_continuation_the_lead_stopped_is_queued_once_more(self):
        self.stopped_by("lead")

    def test_the_owner_stopping_the_continuation_still_cancels_it_as_his(self):
        reply = self.continued()
        upsert_job_record(self.state, reply.message_id, cancel_requested_by="card")
        self.answer(reply, "", status="cancelled")
        record = self.record()
        self.assertEqual((record["state"], record["stopped_by"]), (collab.CANCELLED, "owner"))
        self.assertEqual(self.queued(), [])

    def test_an_automatic_retry_to_come_is_waited_for(self):
        reply = self.continued()
        upsert_job_record(self.state, reply.message_id, auto_retry_state="pending")
        self.answer(reply, "", status="failed")
        self.assertEqual(self.queued(), [])
        record = self.record()
        self.assertEqual((record["state"], record["replies"][0]["state"]), (collab.OPEN, "failed"))

    def test_a_continuation_retried_from_its_card_keeps_its_source(self):
        job = self.asked()
        self.answer(job, "方案 B")
        [reply] = self.queued()
        upsert_job_record(self.state, reply.message_id, status="failed")
        self.app.runtime.unregister_reply_job(reply)
        app = self.app.apps["claude"]
        ctx = context_for_job(app.harness.context, reply)
        app.controls.requeue(ctx, "chat", reply.message_id, find_job_record(self.state, reply.message_id))
        [again] = self.queued()
        self.assertEqual(turn_source.kind(again), turn_source.REPLY)  # Never the owner's words.


class ParticipantsTest(Case):
    """The butler and a lead can be asked: their snapshot or state pack is read, never saved or taken (R17)."""

    def test_a_leads_receipts_stay_untaken_and_the_pack_is_read_only(self):
        from covey.app import project_lead

        lead_record = {"id": "p1p1p1", "name": "迁移", "chat_id": "chat", "lead_topic_id": projects.lead_topic_id("chat", "p1p1p1"),
                       "threads": [], "state": projects.ACTIVE, "goal": "迁完"}
        item = project_lead.add_receipt(self.state, "p1p1p1", "thread_end", "T9 完成了")
        with mock.patch.object(project_lead, "member_rows", return_value=[]):
            text = project_lead.read_only_pack(self.state, lead_record)
        self.assertIn("T9 完成了", text)
        saved = read_object(project_lead.inbox_dir(self.state, "p1p1p1") / (item["id"] + ".json"))
        self.assertEqual(saved["taken_by"], "")

    def test_the_butlers_snapshot_record_is_not_saved_by_a_consult(self):
        from covey.app import butler as butler_app
        from covey.core import butler

        path = topic_file(self.state, butler.butler_topic_id("chat")).parent / butler_app.SNAPSHOT
        write_json(path, {"schema_version": 1, "job": "k0", "at": 1, "groups": {}})
        before = path.read_bytes()
        with mock.patch.object(butler_app, "snapshot_groups", return_value={}), \
                mock.patch.object(butler_app, "render_snapshot", return_value="快照"):
            text = collab_channel.read_only_view(self.ctx, "chat", butler.butler_topic_id("chat"), collab.BUTLER)
        self.assertIn("快照", text)
        self.assertEqual(path.read_bytes(), before)


class ReceiptTest(Case):
    """Package D's receipts: an answer goes into the lead's inbox and wakes nobody; a gap the integrator cannot close wakes."""

    def test_answered_never_wakes_and_a_gap_always_does(self):
        from covey.app import project_loop

        with mock.patch.dict(os.environ, {projects.AUTOWAKE_ENV: "1"} if hasattr(projects, "AUTOWAKE_ENV") else {}):
            self.assertFalse(project_loop.autowake_due([{"kind": collab.RECEIPT_ANSWERED}]))
        self.assertTrue(project_loop.autowake_due([{"kind": collab.RECEIPT_GAP}]))
        self.assertTrue(project_loop.autowake_due([{"kind": "start"}, {"kind": collab.RECEIPT_ANSWERED}]))

    def test_a_project_members_ask_writes_the_answered_receipt_and_its_gap_the_gap(self):
        from covey.app import project_lead

        self.enterContext(mock.patch.dict(os.environ, {projects.ENV: "1"}))
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        for topic in (helper, integrator):
            data = load_topic(self.state, topic)
            write_json(topic_file(self.state, topic), {**data, "project": {"id": "p1p1p1", "role": projects.MEMBER}},
                       ensure_ascii=False)
        self.end(self.root_turn(integrator, [{"to": self.label(helper), "question": "?"}]))
        [job] = self.queued()
        self.answer(job, "答复")
        inbox = [read_object(p) for p in sorted(project_lead.inbox_dir(self.state, "p1p1p1").glob("*.json"))]
        self.assertEqual([item["kind"] for item in inbox], [collab.RECEIPT_ANSWERED])
        [reply] = self.queued()
        self.assertIn("collab_gap", reply.user_text)
        checkpoint = self.state / "checkpoints" / "reply.json"
        write_json(checkpoint, {"phase": "完成", collab_flow.GAP_FIELD: "缺压测基线"}, ensure_ascii=False)
        upsert_job_record(self.state, reply.message_id, checkpoint_path=str(checkpoint), verified_reply_authorized=True)
        self.answer(reply, "交付")
        self.flow.after_runner(reply, self.ctx)  # Its end read again (the thread's reconciliation): one receipt still.
        kinds = [read_object(p)["kind"] for p in sorted(project_lead.inbox_dir(self.state, "p1p1p1").glob("*.json"))]
        self.assertEqual(kinds, [collab.RECEIPT_ANSWERED, collab.RECEIPT_GAP])
        self.assertFalse(any(read_object(p)["kind"] == "thread_end" and "collab-" in str(read_object(p).get("thread"))
                             for p in project_lead.inbox_dir(self.state, "p1p1p1").glob("*.json")))


class ViewTest(Case):
    def test_the_api_gives_the_record_as_the_page_and_the_cards_read_it(self):
        helper, integrator = self.topic("om_h"), self.topic("om_i")
        found = self.flow.start(self.ctx, "chat", root={"message_id": "web-op1", "job_key": "web-op1", "job_id": 0,
                                                        "words": "问问 T"}, integrator=integrator,
                                asks=[{"to": helper, "question": "?"}], owner_named=[helper],
                                asserted_by=turn_source.PARTY_OWNER)
        view = found["collab"]
        self.assertEqual(view["asks"][0]["words"], collab.ASK_WORDS[collab.TAKEN])
        [job] = self.queued()
        self.assertEqual(job.turn_source["level"], turn_source.LEVEL_OWNER)  # The owner's own words, from the page.
        again = self.flow.start(self.ctx, "chat", root={"message_id": "web-op1", "job_key": "web-op1", "job_id": 0},
                                integrator=integrator, asks=[{"to": helper, "question": "?"}], owner_named=[helper])
        self.assertTrue(again["again"])
        self.assertEqual(self.flow.view(self.ctx, view["id"])["id"], view["id"])
        self.assertEqual([row["id"] for row in self.flow.of_chat(self.ctx, "chat")], [view["id"]])

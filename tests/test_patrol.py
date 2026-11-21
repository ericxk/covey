"""What the butler would have told the owner, only recorded (docs/butler.md 我本来想找你的事): off unless COVEY_PATROL is
``shadow``; the rules of the first layer (a question waiting long, a result unseen long, failures in a row, a task with no
sign of life, topics that look like one matter, a topic that fits a project, a project with work nobody does or one growing
untidy), each object recorded once while its state stays; the small model's input and output (principles in, a redacted
topic's title out, a bad answer, a failure, the day's calls); and the digest's block of what it would have done."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support.records import state_dir, write_topic
from covey.app import patrol, patrol_judge, patrol_rules, track_judge
from covey.app.digest import Digest, collect, wanted_view
from covey.app.track_standby import Failed
from covey.cards.digest import digest_card, digest_text
from covey.core import butler, patrol as store, principles, projects
from covey.core.jobs import job_key, upsert_job_record
from covey.core.maintenance import maintain
from covey.core.state import write_private_text
from covey.feishu.messenger import Voice

CHAT = "oc_dm"
HOUR = 3600


class Records:
    def setUp(self):
        self.state = state_dir(self)
        self.now = time.time()
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in list(os.environ):
            if name.startswith(("COVEY_PATROL", "LARK_BRIDGE_PATROL", "COVEY_PROJECTS", "LARK_BRIDGE_PROJECTS")):
                del os.environ[name]

    def topic(self, topic_id, number, title=""):
        write_topic(self.state, {"topic_number": number, **({"origin": {"title": title}} if title else {})}, topic_id=topic_id)

    def job(self, key, job_id, status, topic, **fields):
        upsert_job_record(self.state, key, job_id=job_id, status=status, chat_id=CHAT, topic_id=topic,
                          card_message_id=f"om_{key}", harness_id="claude", **fields)

    def question(self, token, topic, asked_at):
        write_private_text(self.state / "questions" / (token + ".json"), json.dumps({
            "token": token, "topic_id": topic, "chat_id": CHAT, "job_id": 9, "status": "pending",
            "created_at": asked_at, "question_message_id": f"om_{token}", "questions": [{"question": "要不要做？"}]}))

    def project(self, project_id, name, goal="", threads=(), state=projects.ACTIVE, **fields):
        record = projects.new_record(chat_id=CHAT, name=name, goal=goal, project_id=project_id, now=self.now - 100 * HOUR)
        record.update({"threads": list(threads), "state": state, **fields})
        projects.create(self.state, record)

    def find(self, later=0.0):
        now = self.now + later
        return patrol_rules.find(patrol_rules.look(self.state, CHAT, now))

    def kinds(self, later=0.0):
        return sorted(candidate.key for candidate in self.find(later))


class RuleTests(Records, unittest.TestCase):
    def test_a_question_waits_long_enough_only_after_two_hours(self):
        self.topic("a", 1, "发布方案")
        self.question("q1", "a", self.now - 3 * HOUR)
        self.assertEqual(self.kinds(), ["ask:a"])
        found = self.find()[0]
        self.assertEqual((found.kind, found.sig, found.topics), ("ask", "q1", ("a",)))
        self.assertIn("T1", found.what)
        self.question("q1", "a", self.now - HOUR)
        self.assertEqual(self.kinds(), [])

    def test_the_hours_can_be_set(self):
        self.topic("a", 1)
        self.question("q1", "a", self.now - HOUR)
        with mock.patch.dict(os.environ, {"COVEY_PATROL_ASK_MINUTES": "30"}):
            self.assertEqual(self.kinds(), ["ask:a"])
            os.environ["COVEY_PATROL_ASK_MINUTES"] = "nine"
            self.assertEqual(store.number("ask_minutes"), 120)
            os.environ["COVEY_PATROL_FAILS"] = "1"
            self.assertEqual(store.number("fails"), 2)  # Clamped.
            os.environ["COVEY_PATROL_JUDGE_MINUTES"] = "0"
            self.assertEqual(store.number("judge_minutes"), 1)
            os.environ["COVEY_PATROL_TICK_SECONDS"] = "10"
            self.assertEqual(store.number("tick_seconds"), 10)

    def test_a_result_not_seen_for_six_hours(self):
        self.topic("a", 1)
        self.job("j1", 1, "done", "a", timings={"execution_finished": self.now - 7 * HOUR})
        self.assertEqual(self.kinds(), ["unseen:a"])
        self.job("j2", 2, "done", "a", timings={"execution_finished": self.now - 5 * HOUR})
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.kinds(later=2 * HOUR), ["unseen:a"])

    def test_a_result_the_owner_saw_is_not_a_candidate(self):
        self.topic("a", 1)
        self.job("j1", 1, "done", "a", timings={"execution_finished": self.now - 7 * HOUR})
        write_private_text(self.state / "topic-seen.json", json.dumps({"a": self.now - HOUR}))
        self.assertEqual(self.kinds(), [])

    def test_failures_in_a_row(self):
        self.topic("a", 1)
        self.job("j1", 1, "failed", "a", timings={"execution_finished": self.now - 3 * HOUR})
        self.assertEqual([key for key in self.kinds() if key.startswith("fails")], [])
        self.job("j2", 2, "failed", "a", timings={"execution_finished": self.now - 2 * HOUR})
        self.assertIn("fails:a", self.kinds())
        self.job("j3", 3, "done", "a", timings={"execution_finished": self.now - HOUR})
        self.assertNotIn("fails:a", self.kinds())

    def test_a_task_with_no_sign_of_life(self):
        self.topic("a", 1)
        self.job("j1", 1, "running", "a")
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.kinds(later=4 * HOUR), ["stuck:a"])
        self.question("q1", "a", self.now + 4 * HOUR - 60)
        self.assertEqual(self.kinds(later=4 * HOUR), [])  # It waits for the owner, which is not stuck.

    def test_a_candidate_of_a_projects_topic_names_its_project(self):
        # The acceptance of 2026-11-17: questions waiting in a project's topics came with "project": "", so the model could not
        # tell they were in one (and could not propose waking its lead).
        self.topic("a", 1)
        self.topic("b", 2)
        self.question("qa", "a", self.now - 3 * HOUR)
        self.question("qb", "b", self.now - 3 * HOUR)
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "整理资料", threads=["a"])
            found = {candidate.key: candidate.project for candidate in self.find() if candidate.kind == "ask"}
        self.assertEqual(found, {"ask:a": "proj001", "ask:b": ""})

    def test_topics_of_one_workspace_with_alike_titles_are_one_matter(self):
        for number, (topic, title) in enumerate((("a", "整理发布流程文档"), ("b", "发布流程文档补充"), ("c", "买机票")), 1):
            self.topic(topic, number, title)
            self.job(f"j{topic}", number, "done", topic, workspace_name="docs", timings={"execution_finished": self.now - 60})
        self.job("jd", 4, "done", "c", workspace_name="other", timings={"execution_finished": self.now - 60})
        found = [candidate for candidate in self.find() if candidate.kind == "group"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].topics, ("a", "b"))
        self.assertIn("T1", found[0].what)

    def test_alike_titles_in_different_workspaces_need_four_words(self):
        self.topic("a", 1, "整理发布流程")
        self.topic("b", 2, "发布流程补充")
        self.job("ja", 1, "done", "a", workspace_name="x")
        self.job("jb", 2, "done", "b", workspace_name="y")
        self.assertEqual([c for c in self.find() if c.kind == "group"], [])

    def test_the_butlers_topic_and_a_projects_topics_are_not_grouped(self):
        projects_on = {"COVEY_PROJECTS": "1"}
        with mock.patch.dict(os.environ, projects_on):
            self.topic("a", 1, "整理发布流程文档")
            self.topic("b", 2, "发布流程文档补充")
            self.job("ja", 1, "done", "a", workspace_name="x")
            self.job("jb", 2, "done", "b", workspace_name="x")
            self.project("proj001", "无关", threads=["b"])
            self.assertEqual([c for c in self.find() if c.kind == "group"], [])

    def test_a_topic_that_fits_an_active_project(self):
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "发布流程改造", goal="把发布流程文档化")
            self.topic("a", 1, "发布流程文档")
            self.job("ja", 1, "done", "a", workspace_name="x")
            self.topic("b", 2, "买机票")
            self.job("jb", 2, "done", "b", workspace_name="x")
            found = [candidate for candidate in self.find() if candidate.kind == "join"]
            self.assertEqual([(c.key, c.project, c.sig) for c in found], [("join:a", "proj001", "proj001")])

    def test_a_project_with_work_nobody_does(self):
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "整理资料", threads=["t1"])
            self.assertEqual([c for c in self.find() if c.kind == "project_idle"], [])  # No receipt waits.
            from covey.app import project_lead
            project_lead.add_receipt(self.state, "proj001", "thread_end", "做完了", thread="t1", at=self.now - 5 * HOUR)
            self.assertEqual([c for c in self.find() if c.kind == "project_idle"], [])  # Not 12 hours yet.
            found = [c for c in self.find(later=14 * HOUR) if c.kind == "project_idle"]
            self.assertEqual([c.key for c in found], ["project_idle:proj001"])

    def test_a_project_a_guard_paused_counts_without_receipts(self):
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj002", "停住的", state=projects.PAUSED, state_reason="stalled")
            self.assertEqual([c.key for c in self.find(later=13 * HOUR) if c.kind == "project_idle"], ["project_idle:proj002"])
            self.project("proj003", "主人停的", state=projects.PAUSED, state_reason="主人停止")
            self.assertNotIn("project_idle:proj003", self.kinds(later=13 * HOUR))

    def test_a_project_with_many_topics_grows_untidy(self):
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "大专项", threads=[f"t{n}" for n in range(6)])
            self.project("proj002", "小专项", threads=["x1", "x2"])
            self.assertEqual([c.key for c in self.find() if c.kind == "project_messy"], ["project_messy:proj001"])

    def test_a_project_spread_over_three_workspaces_grows_untidy(self):
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "杂专项", threads=["t1", "t2", "t3"])
            for number, topic in enumerate(("t1", "t2", "t3"), 1):
                self.topic(topic, number, f"事 {number}")
                self.job(f"j{topic}", number, "done", topic, workspace_name=f"w{number}")
            found = [c for c in self.find() if c.kind == "project_messy"]
            self.assertEqual([(c.key, c.sig) for c in found], [("project_messy:proj001", "spread")])

    def test_nothing_is_written_by_looking(self):
        self.topic("a", 1)
        self.question("q1", "a", self.now - 3 * HOUR)
        files = lambda: sorted((str(p), p.stat().st_mtime_ns) for p in self.state.rglob("*") if p.is_file())  # noqa: E731
        before = files()
        self.find()
        self.assertEqual(files(), before)


def candidate(key="ask:a", sig="q1", kind="ask", **fields):
    return patrol_rules.Candidate(kind, key, sig, CHAT, fields.pop("title", "标题"), fields.pop("what", "T1：有个问题等了你 3小时0分"),
                                  fields.pop("topics", ("a",)), **fields)


class FollowUpRuleTests(Records, unittest.TestCase):
    """#55: the butler said it would come back to something and handed it to no topic; a topic's conclusion due at the
    top level whose relay is not known to have gone. Recorded only: never judged, nothing sent."""

    def setUp(self):
        super().setUp()
        for cache in (patrol_rules._handed, patrol_rules._due, patrol_rules._relayed):
            cache.clear()

    def butler_job(self, key, job_id, text, ended):
        self.job(key, job_id, "done", butler.butler_topic_id(CHAT), message_id=key, user_text="看看昨晚的日志",
                 final_text=text, timings={"execution_finished": ended})

    def topic_job(self, key, job_id, ended, topic="t"):
        self.job(key, job_id, "done", topic, message_id=key, final_text="结论：好了", timings={"execution_finished": ended})

    def test_a_butler_reply_put_off_with_no_topic_is_recorded(self):
        self.butler_job("om_b1", 1, "我去查一下，稍后告诉你。", self.now - 60)
        found = [c for c in self.find() if c.kind == "deferred"]
        self.assertEqual([(c.key, c.sig, c.title) for c in found],
                         [(f"deferred:{job_key('om_b1', 1)}", job_key("om_b1", 1), "看看昨晚的日志")])
        self.assertIn("没开也没转话题", found[0].what)
        # Handed on in that turn, a reply naming a topic, a reply that puts nothing off, one over a day old: none.
        self.butler_job("om_b2", 2, "等下我看看。", self.now - 60)
        write_private_text(self.state / "main-thread" / "opened" / "r2.json", json.dumps(
            {"job_key": job_key("om_b2", 2), "topic_id": "t", "answer": {"ok": True, "data": {}}}))
        self.butler_job("om_b3", 3, "迁移账单（T12）在跑，稍后结论会发过来。", self.now - 60)
        self.butler_job("om_b4", 4, "已经查完：14 个通过。", self.now - 60)
        self.butler_job("om_b5", 5, "我去查一下，稍后告诉你。", self.now - store.GONE - 60)
        self.assertEqual([c.key for c in self.find() if c.kind == "deferred"], [f"deferred:{job_key('om_b1', 1)}"])
        # A topic's own reply is no butler's.
        self.topic("t", 1, "发布方案")
        self.job("om_t1", 9, "done", "t", message_id="om_t1", final_text="我去查一下，稍后告诉你。",
                 timings={"execution_finished": self.now - 60})
        self.assertEqual(len([c for c in self.find() if c.kind == "deferred"]), 1)

    def test_a_conclusion_due_at_the_top_level_and_not_known_sent_is_recorded_after_ten_minutes(self):
        from covey.app import relay_send
        self.topic("t", 1, "发布方案")
        self.topic_job("om_t1", 1, self.now - 20 * 60)
        with mock.patch("covey.app.relay_targets.relay_target", return_value={"kind": "start"}):
            found = [c for c in self.find() if c.kind == "unrelayed"]
            self.assertEqual([(c.key, c.sig, c.topics) for c in found], [(f"unrelayed:{job_key('om_t1', 1)}", "none", ("t",))])
            self.assertIn("T1", found[0].what)
            path = relay_send.relay_path(self.state, job_key("om_t1", 1))
            write_private_text(path, json.dumps({"state": "uncertain"}))
            self.assertEqual([c.sig for c in self.find() if c.kind == "unrelayed"], ["uncertain"])
            write_private_text(path, json.dumps({"state": "sent"}))
            self.assertEqual([c for c in self.find() if c.kind == "unrelayed"], [])
            self.topic_job("om_t2", 2, self.now - 5 * 60)  # Not ten minutes yet.
            self.assertEqual([c for c in self.find() if c.kind == "unrelayed"], [])
            with mock.patch.dict(os.environ, {"COVEY_PATROL_RELAY_MINUTES": "3"}):
                self.assertEqual([c.key for c in self.find() if c.kind == "unrelayed"], [f"unrelayed:{job_key('om_t2', 2)}"])
        self.topic_job("om_t3", 3, self.now - 20 * 60)
        with mock.patch("covey.app.relay_targets.relay_target", return_value=None):
            self.assertEqual([c for c in self.find() if c.kind == "unrelayed"], [])  # Not due at the top level.

    def test_they_are_recorded_and_never_put_to_the_model(self):
        self.assertEqual(store.RECORD_ONLY, {"deferred", "unrelayed"})
        self.assertTrue(store.RECORD_ONLY <= set(store.KINDS))
        data = store.load(self.state)
        rows = patrol.record(data, [candidate(kind="deferred", key="deferred:x"), candidate(kind="unrelayed", key="unrelayed:y"),
                                    candidate()], self.now, self.state)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["kind"] for row in data["pending"]], ["ask"])
        lines = (store.folder(self.state) / "candidates" / f"{store.day_of(self.now)}.jsonl").read_text().splitlines()
        self.assertEqual(sorted(json.loads(line)["kind"] for line in lines), ["ask", "deferred", "unrelayed"])


class RecordTests(Records, unittest.TestCase):
    def test_an_object_is_recorded_once_while_its_state_stays(self):
        data = store.load(self.state)
        self.assertEqual(len(patrol.record(data, [candidate()], self.now, self.state)), 1)
        self.assertEqual(patrol.record(data, [candidate()], self.now + 60, self.state), [])
        self.assertEqual(len(data["pending"]), 1)
        lines = (store.folder(self.state) / "candidates" / f"{store.day_of(self.now)}.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["key"], "ask:a")

    def test_a_changed_state_is_a_new_candidate(self):
        data = store.load(self.state)
        patrol.record(data, [candidate()], self.now, self.state)
        self.assertEqual(len(patrol.record(data, [candidate(sig="q2")], self.now + 60, self.state)), 1)

    def test_an_object_that_was_gone_for_a_day_is_new_again(self):
        data = store.load(self.state)
        patrol.record(data, [candidate()], self.now, self.state)
        self.assertEqual(len(patrol.record(data, [candidate()], self.now + store.GONE + 1, self.state)), 1)

    def test_pending_is_bounded(self):
        data = store.load(self.state)
        patrol.record(data, [candidate(key=f"ask:{n}") for n in range(patrol.PENDING_KEPT + 5)], self.now)
        self.assertEqual(len(data["pending"]), patrol.PENDING_KEPT)


class JudgeTests(Records, unittest.TestCase):
    def rows(self, count=2, **fields):
        return [{"id": f"id{n}", "kind": "ask", "key": f"ask:{n}", "title": f"标题{n}", "what": f"事{n}", "topics": [f"t{n}"],
                 "wait": 3 * HOUR, "project": "", **fields} for n in range(1, count + 1)]

    def reply(self, decisions):
        return json.dumps({"is_error": False, "result": json.dumps({"decisions": decisions}, ensure_ascii=False)})

    def test_the_input_has_the_items_and_the_owners_principles(self):
        principles.save(self.state, "owner", "生活类的事不用找我")
        data = patrol_judge.payload(self.state, self.rows())
        self.assertEqual(data["principles"], ["生活类的事不用找我"])
        self.assertEqual([item["index"] for item in data["items"]], [1, 2])
        self.assertEqual(data["items"][0]["title"], "标题1")
        self.assertEqual(data["items"][0]["kind"], "问题等了很久没回")
        self.assertIn("3小时", data["items"][0]["waited"])

    def test_a_redacting_topic_never_reaches_the_model(self):
        write_topic(self.state, {"redaction": True}, topic_id="t1")
        with mock.patch("covey.core.redaction.TOPIC_KEY", "redaction"):
            data = patrol_judge.payload(self.state, self.rows())
        self.assertEqual(data["items"][0]["title"], patrol_judge.HIDDEN)
        self.assertEqual(data["items"][0]["what"], patrol_judge.HIDDEN)
        self.assertEqual(data["items"][1]["title"], "标题2")

    def test_parse_cuts_the_reason_and_drops_what_it_cannot_use(self):
        batch = self.rows(3)
        text = json.dumps({"decisions": [
            {"index": 1, "verdict": "handle", "why": "字" * 60, "action": "ask"},
            {"index": 2, "verdict": "maybe", "why": "x", "action": "ask"},
            {"index": 9, "verdict": "note", "why": "x", "action": "none"},
            {"index": 3, "verdict": "handle", "why": "归进去", "action": "join"},  # No project: it cannot join.
            {"index": 1, "verdict": "drop", "why": "again", "action": "none"}]})
        found = patrol_judge.parse("好的 " + text, batch)
        # id3 could not join (no project): nothing it would have done, so only noted (the acceptance of 2026-11-17 saw handle + none).
        self.assertEqual([(d["candidate"], d["verdict"], d["action"]) for d in found], [("id1", "handle", "ask"), ("id3", "note", "none")])
        self.assertEqual(len(found[0]["why"]), patrol_judge.WHY_CHARS + 1)
        with self.assertRaises(Failed):
            patrol_judge.parse("没有 JSON", batch)

    def test_a_project_candidate_may_join_or_tidy(self):
        batch = self.rows(1, project="proj001")
        found = patrol_judge.parse(json.dumps({"decisions": [{"index": 1, "verdict": "handle", "why": "像", "action": "join"}]}), batch)
        self.assertEqual(found[0]["action"], "join")

    def test_decide_asks_haiku_through_the_harness_wrapping(self):
        seen = []
        backend = SimpleNamespace(wrap_argv=lambda argv, ctx: seen.append(list(argv)) or argv, child_env=lambda ctx: {})
        ctx = SimpleNamespace(state_dir=self.state, backend_cmd=["claude"])
        answer = self.reply([{"index": 1, "verdict": "note", "why": "先记下", "action": "none"}])
        with mock.patch.object(track_judge, "run", return_value=answer) as run:
            found, why = patrol_judge.decide(self.state, self.rows(1), backend=backend, ctx=ctx, log=lambda _: None)
        self.assertEqual((why, [d["verdict"] for d in found]), ("ok", ["note"]))
        argv = seen[0]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], patrol_judge.SYSTEM)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(run.call_args.args[4], 30.0)
        self.assertEqual(json.loads(run.call_args.args[1])["items"][0]["title"], "标题1")

    def test_a_failure_gives_no_decision_and_the_reason(self):
        backend = SimpleNamespace(wrap_argv=lambda argv, ctx: argv)
        ctx = SimpleNamespace(state_dir=self.state, backend_cmd=["claude"])
        logs = []
        for error, reason in ((Failed("timeout"), "timeout"), (RuntimeError("x"), "error")):
            with mock.patch.object(track_judge, "run", side_effect=error):
                self.assertEqual(patrol_judge.decide(self.state, self.rows(1), backend=backend, ctx=ctx, log=logs.append), ([], reason))
        with mock.patch.object(track_judge, "run", return_value="not json"):
            self.assertEqual(patrol_judge.decide(self.state, self.rows(1), backend=backend, ctx=ctx, log=logs.append)[1], "bad_output")
        self.assertEqual(patrol_judge.decide(self.state, self.rows(1), backend=None, ctx=None, log=logs.append)[1], "no_backend")
        self.assertTrue(all("detail" not in line or "标题" not in line for line in logs))


class FakeApp:
    def __init__(self, state):
        self.harness = SimpleNamespace(backend=SimpleNamespace(wrap_argv=lambda argv, ctx: argv, child_env=lambda ctx: {}),
                                       context=SimpleNamespace(state_dir=state, backend_cmd=["claude"]))


class TickTests(Records, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.logs = []
        runtime = SimpleNamespace(log=self.logs.append, shutdown=threading.Event(), access=SimpleNamespace(notify_chat=lambda: CHAT))
        env = SimpleNamespace(state_dir=self.state, topics=SimpleNamespace(apps={"claude": FakeApp(self.state)}))
        self.feature = patrol.Patrol(runtime, env)
        self.topic("a", 1, "发布方案")
        self.question("q1", "a", self.now - 3 * HOUR)

    def on(self):
        patcher = mock.patch.dict(os.environ, {"COVEY_PATROL": "shadow"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, verdict="handle", action="ask"):
        return json.dumps({"is_error": False, "result": json.dumps({"decisions": [
            {"index": 1, "verdict": verdict, "why": "等太久了", "action": action}]})})

    def test_off_has_no_thread_and_does_nothing(self):
        for value in ("", "off", "0", "1", "yes"):
            with mock.patch.dict(os.environ, {"COVEY_PATROL": value}):
                self.assertEqual(self.feature.threads(), ())
                with mock.patch.object(track_judge, "run") as run:
                    self.feature.tick(self.now)
                run.assert_not_called()
        self.assertFalse((self.state / "patrol").exists())

    def test_shadow_and_on_each_have_the_thread(self):
        for value in ("shadow", "on", " Shadow "):
            with mock.patch.dict(os.environ, {"COVEY_PATROL": value}):
                self.assertEqual([name for name, _ in self.feature.threads()], ["patrol"])

    def test_a_minute_records_and_half_an_hour_judges(self):
        self.on()
        with mock.patch.object(track_judge, "run", return_value=self.answer()) as run:
            self.feature.tick(self.now)  # The first round judges at once: the candidates are there.
            self.assertEqual(run.call_count, 1)
            self.feature.tick(self.now + 60)
            self.assertEqual(run.call_count, 1)  # Nothing new, nothing to ask.
        decisions = store.rows(self.state, "decisions", self.now - 1, self.now + 1)
        self.assertEqual([(d["key"], d["verdict"], d["action"], d["why"]) for d in decisions], [("ask:a", "handle", "ask", "等太久了")])
        self.assertEqual(store.load(self.state)["pending"], [])
        self.assertEqual(store.load(self.state)["calls"], {"day": store.day_of(self.now), "n": 1})

    def test_new_candidates_wait_for_the_next_half_hour(self):
        self.on()
        with mock.patch.object(track_judge, "run", return_value=self.answer()) as run:
            self.feature.tick(self.now)
            self.topic("b", 2)
            self.question("q2", "b", self.now - 4 * HOUR)
            self.feature.tick(self.now + 60)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(store.load(self.state)["pending"]), 1)
            self.feature.tick(self.now + patrol.JUDGE_EVERY + 1)
            self.assertEqual(run.call_count, 2)

    def test_a_failed_call_is_tried_again_and_gives_up_after_four_rounds(self):
        self.on()
        with mock.patch.object(track_judge, "run", side_effect=Failed("timeout")) as run:
            for round_ in range(5):
                self.feature.tick(self.now + round_ * (patrol.JUDGE_EVERY + 1))
        self.assertEqual(run.call_count, patrol.TRIES)
        self.assertEqual(store.load(self.state)["pending"], [])
        self.assertEqual(store.rows(self.state, "decisions", 0, self.now + 10 ** 6), [])

    def test_the_days_calls_are_limited(self):
        self.on()
        now = self.now
        if store.day_of(now) != store.day_of(now + 2 * patrol.JUDGE_EVERY + 2):
            now += 2 * patrol.JUDGE_EVERY + 2  # Run between 23:00 and midnight: keep the three ticks in one day.
        with mock.patch.dict(os.environ, {"COVEY_PATROL_DAILY_CALLS": "1"}), \
                mock.patch.object(track_judge, "run", side_effect=Failed("timeout")) as run:
            self.feature.tick(now)
            self.feature.tick(now + patrol.JUDGE_EVERY + 1)
            self.feature.tick(now + 2 * patrol.JUDGE_EVERY + 2)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(any("skipped" in line for line in self.logs))
        # The next day it asks again.
        with mock.patch.dict(os.environ, {"COVEY_PATROL_DAILY_CALLS": "1"}), \
                mock.patch.object(track_judge, "run", side_effect=Failed("timeout")) as run:
            self.feature.tick(self.now + 25 * HOUR)
        self.assertEqual(run.call_count, 1)

    def test_the_model_is_not_called_while_the_lock_is_held(self):
        self.on()
        from covey.core.locks import resource_lock
        held = []

        def run(*args, **kwargs):
            acquired = resource_lock("patrol", str(store.folder(self.state))).acquire(False) if hasattr(
                resource_lock("patrol", str(store.folder(self.state))), "acquire") else True
            held.append(acquired)
            return self.answer()
        with mock.patch.object(track_judge, "run", side_effect=run):
            self.feature.tick(self.now)
        self.assertEqual(held, [True])

    def test_nothing_is_sent_and_no_project_changes(self):
        self.on()
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "发布方案", goal="发布")
            before = projects.read_record(self.state, "proj001")
            with mock.patch.object(track_judge, "run", return_value=self.answer(action="wake")):
                self.feature.tick(self.now)
            self.assertEqual(projects.read_record(self.state, "proj001"), before)
        self.assertEqual(sorted(path.name for path in (self.state / "patrol").iterdir()), ["candidates", "decisions", "judge", "state.json"])


class WantedTests(Records, unittest.TestCase):
    def decision(self, key, verdict="handle", at=None, chat=CHAT, **fields):
        store.append(self.state, "decisions", at or self.now - 600, {
            "id": f"d-{key}", "at": at or self.now - 600, "candidate": "c", "kind": "ask", "key": key, "chat": chat, "title": "t",
            "what": f"事 {key}", "project": "", "verdict": verdict, "why": f"因 {key}", "action": "ask", **fields})

    def on(self):
        patcher = mock.patch.dict(os.environ, {"COVEY_PATROL": "shadow"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def view(self):
        return collect(self.state, CHAT, self.now, self.now - 12 * HOUR, self.now)

    def test_only_what_it_would_have_acted_on_is_listed(self):
        self.on()
        self.decision("ask:a")
        self.decision("ask:b", verdict="note")
        self.decision("ask:c", verdict="drop")
        self.decision("ask:d", chat="oc_group")
        self.decision("ask:e", at=self.now - 20 * HOUR)  # Before the digest before.
        self.assertEqual(self.view()["wanted"], [{"what": "事 ask:a", "why": "因 ask:a", "would": "主动来问你"}])

    def test_five_at_most_the_newest_first_each_object_once(self):
        self.on()
        for number in range(7):
            self.decision(f"ask:{number}", at=self.now - 3000 + number * 100)
        self.decision("ask:6", at=self.now - 100)
        rows = wanted_view(self.state, CHAT, self.now - 12 * HOUR, self.now)
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["what"] for row in rows][:2], ["事 ask:6", "事 ask:5"])
        self.assertEqual(len({row["what"] for row in rows}), 5)

    def test_a_join_names_the_project(self):
        self.on()
        with mock.patch.dict(os.environ, {"COVEY_PROJECTS": "1"}):
            self.project("proj001", "发布改造")
            self.decision("join:a", action="join", project="proj001")
            self.assertEqual(self.view()["wanted"][0]["would"], "归进专项「发布改造」")
        self.decision("tidy:a", action="none")
        self.assertIn("先记着", [row["would"] for row in self.view()["wanted"]][0])

    def test_off_or_without_decisions_the_view_has_no_such_part(self):
        self.decision("ask:a")
        self.assertNotIn("wanted", self.view())  # Off.
        self.on()
        self.assertIn("wanted", self.view())
        os.environ["COVEY_PATROL"] = "shadow"
        self.assertNotIn("wanted", collect(self.state, CHAT, self.now, self.now - HOUR / 100, self.now))

    def test_the_card_and_its_text_have_the_block(self):
        self.on()
        self.decision("ask:a", action="wake")
        view = self.view()
        card = digest_card(view)
        texts = [element.get("content", "") for element in card["body"]["elements"]]
        self.assertIn("**我本来想找你的事（1）**", texts)
        self.assertIn("事 ask:a · 因 ask:a · 本来会：叫醒负责人看一眼", texts)
        self.assertIn("我本来想找你的事（1）", digest_text(view))
        plain = digest_card({k: v for k, v in view.items() if k != "wanted"})
        self.assertFalse(any("我本来想找你的事" in element.get("content", "") for element in plain["body"]["elements"]))

    def test_it_alone_does_not_make_a_digest_be_sent(self):
        self.on()
        self.decision("ask:a")
        sends = []
        messenger = SimpleNamespace(send_message=lambda *a, **k: sends.append(a) or True)
        runtime = SimpleNamespace(log=lambda _: None, shutdown=threading.Event(),
                                  access=SimpleNamespace(owner_chat=lambda: CHAT, notify_chat=lambda: CHAT))
        feature = Digest(runtime, messenger, Voice("lark-bridge", "LARK_BRIDGE"))
        import datetime
        from covey.app.digest import ZONE
        local = datetime.datetime.fromtimestamp(self.now, ZONE)
        slot = local.replace(second=0, microsecond=0).timestamp()
        feature.tick(SimpleNamespace(state_dir=self.state, profile="p"), [local.hour * 60 + local.minute], now=slot + 5)
        self.assertEqual(sends, [])


class MaintenanceTests(Records, unittest.TestCase):
    def test_the_day_files_go_after_fourteen_days_and_state_stays(self):
        old = store.folder(self.state) / "decisions" / "2026-01-01.jsonl"
        new = store.folder(self.state) / "candidates" / "2026-11-17.jsonl"
        keep = store.folder(self.state) / "state.json"
        for path in (old, new, keep):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        later = time.time() + 15 * 86400
        os.utime(new, (later - 86400, later - 86400))
        plan = maintain(self.state, apply=True, now=later)
        self.assertEqual(plan["remove_patrol"], [str(old)])
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and keep.exists())


if __name__ == "__main__":
    unittest.main()

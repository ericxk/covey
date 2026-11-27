"""A member topic's choice question goes to the project's lead first (docs/projects.md 专项的问题分流, app/triage.py, COVEY_PROJECT_TRIAGE=1):
what holds a question and what does not, that a held question is not a failed delivery (E2), the receipt and the three minutes, the
lead's two tools, the gate words (E4), the 分流记录 and the 不对 correction, and that with the switch off nothing is different."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.records import write_job
from support.world import QuestionFixture

from covey.app import project_context, project_facts, project_lead, project_loop, triage as flow
from covey.app.project_loop import ProjectLoop
from covey.app.questions import recover_question_delivery
from covey.cards import project_card
from covey.cards.question_cards import HELD_NOTE, question_card, question_element
from covey.core import butler, butler_tool_specs, project_gates, project_library, project_memory, projects, recovery, triage
from covey.core.jobs import find_job_record, upsert_job_record
from covey.core.state import read_object, write_json

from test_project_lead import CHAT, LEAD, NOW, ON, PID, Case, ToolsCase

THREAD = "b" * 32
TOKEN = "c" * 16
SWITCH = {**ON, triage.ENV: "1"}
CHOICE = [{"id": "0", "header": "版本", "question": "用哪个库版本？", "multiple": False,
           "options": [{"label": "新版", "description": "推荐"}, {"label": "旧版", "description": "兼容"}]}]


def question(topic=THREAD, **fields):
    return {"token": TOKEN, "topic_id": topic, "chat_id": CHAT, "job_id": 41, "job_key": "k-bbbb", "status": "pending",
            "blocking": True, "display_pending": True, "body_delivery_required": True, "body_delivery_confirmed": True,
            "request_id": "checkpoint-k-bbbb", "created_at": NOW, "questions": CHOICE, **fields}


def card_out(shown, state):
    """A ``show`` that sent the card: it records the token and, as the real one does, leaves the question's card shown."""
    def show(env, record):
        shown.append(record["token"])
        path = Path(state) / "questions" / (record["token"] + ".json")
        write_json(path, {**read_object(path), "display_pending": False}, ensure_ascii=False)
        return True
    return show


class Held(Case):
    """A project with one member topic and a question it asked, the switch on."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, SWITCH).start()
        self.addCleanup(mock.patch.stopall)
        self.thread(THREAD, "导出旧账单", 41, status="waiting_user")
        self.path = self.state / "questions" / (TOKEN + ".json")
        self.triage = flow.Triage(runtime=SimpleNamespace(log=self.logs.append), cards=mock.Mock())
        self.ctx = SimpleNamespace(state_dir=self.state, topic_id=THREAD)

    def write(self, **fields):
        write_json(self.path, question(**fields), ensure_ascii=False)

    def saved(self):
        return read_object(self.path)

    def route(self):
        return self.triage.route(self.ctx, None, self.saved())


class RouteTest(Held):
    def test_a_choice_question_of_a_member_topic_is_held_for_the_lead(self):
        self.write()
        before = time.time()
        self.assertEqual(self.route(), flow.LEAD)
        found = triage.of(self.saved())
        self.assertEqual((found["state"], found["project"], found["receipt"]), (triage.HELD, PID, False))
        self.assertAlmostEqual(found["deadline"] - found["at"], triage.DEADLINE)
        self.assertGreaterEqual(found["at"], before)
        self.assertEqual(self.saved()["status"], "pending")  # Nothing that reads questions sees a new status.
        self.assertTrue(self.saved()["display_pending"])  # And no list of pending questions has it.

    def test_a_second_look_keeps_the_first_hold_and_does_not_write_again(self):
        self.write()
        self.route()
        first = self.saved()
        self.assertEqual(self.route(), flow.LEAD)
        self.assertEqual(self.saved(), first)

    def test_what_stays_the_owners(self):
        cases = {
            "free text": {"questions": [{"id": "0", "question": "写一句原因", "options": []}]},
            "one free among choices": {"questions": [*CHOICE, {"id": "1", "question": "还有吗？"}]},
            "a gate word in the question": {"questions": [{**CHOICE[0], "question": "要部署到哪台机器？"}]},
            "a gate word in an option": {"questions": [{**CHOICE[0], "options": [{"label": "删除旧表"}, {"label": "保留"}]}]},
            "an ascii gate word": {"questions": [{**CHOICE[0], "question": "跑 rm -rf 吗？"}]},
            "not blocking": {"blocking": False},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                self.write(**fields)
                self.assertEqual(self.route(), flow.OWNER)
                self.assertNotIn("triage", self.saved())

    def test_a_question_outside_a_member_topic_or_an_active_project_is_the_owners(self):
        self.write(topic=LEAD)  # The lead's own topic.
        self.assertEqual(self.triage.route(SimpleNamespace(state_dir=self.state, topic_id=LEAD), None, self.saved()), flow.OWNER)
        self.write()
        projects.set_state(self.state, PID, projects.PAUSED, "stalled")
        self.assertEqual(self.route(), flow.OWNER)
        self.assertNotIn("triage", self.saved())

    def test_with_the_switch_off_nothing_is_written(self):
        self.write()
        with mock.patch.dict(os.environ, {triage.ENV: "0"}):
            self.assertEqual(self.route(), flow.OWNER)
        self.assertNotIn("triage", self.saved())
        os.environ[projects.ENV] = "0"
        os.environ[triage.ENV] = "1"  # The projects switch is the first one.
        self.assertFalse(triage.enabled())
        self.assertEqual(self.route(), flow.OWNER)

    def test_a_late_redelivery_of_a_held_question_makes_it_the_owners_and_escalated(self):
        self.write()
        self.route()
        data = self.saved()
        data["triage"]["deadline"] = time.time() - 1
        write_json(self.path, data, ensure_ascii=False)
        self.assertEqual(self.route(), flow.OWNER)
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)

    def test_the_states_only_move_along_the_table(self):
        held = {"triage": {"state": triage.HELD}}
        self.assertEqual(triage.state(triage.moved(held, triage.ANSWERED)), triage.ANSWERED)
        for done in (triage.ANSWERED, triage.ESCALATED):
            with self.assertRaises(ValueError):
                triage.moved({"triage": {"state": done}}, triage.HELD)
        with self.assertRaises(ValueError):
            triage.moved({"triage": {"state": triage.ANSWERED}}, triage.ESCALATED)


class LeadFreeTest(Held):
    """W1-A C3/C4 ③: what is the owner's by its kind goes to him at once; so does every question while the lead cannot take it
    (a turn of it running, a guard over it, a wait for a limit). A lead that only waits for the owner's answer to its own
    project_ask takes questions as ever. The card the owner gets says nothing of the lead (no hold was written)."""

    def lead_turn(self, status, key="lead-k1"):
        write_job(self.state, key, topic_id=LEAD, chat_id=CHAT, job_id=1, message_id=f"lead-{PID}-1", status=status)
        return key

    def owners(self):
        self.assertEqual(self.route(), flow.OWNER)
        self.assertNotIn("triage", self.saved())  # Not 已交负责人: it never was.

    def test_a_question_of_the_five_kinds_or_the_owners_own_by_its_declared_kind_is_never_held(self):
        for kind in ("deploy", "delete", "spend", "send", "occupy", "owner_asked"):
            with self.subTest(kind=kind):
                self.write(questions=[{**CHOICE[0], "kind": kind}])  # Words the gates do not know: the kind says it.
                self.assertFalse(project_gates.hit([{**CHOICE[0], "kind": "other"}]))  # Not by its words.
                self.owners()
        # Raised, never lowered (RT-5 ①): declared blocked, its words say deleting and deploying.
        self.write(questions=[{**CHOICE[0], "kind": "blocked", "question": "现在删掉旧服务并上线新版本吗？"}])
        self.owners()
        for kind in ("blocked", "other"):
            with self.subTest(kind=kind):
                self.write(questions=[{**CHOICE[0], "kind": kind}])
                self.assertEqual(self.route(), flow.LEAD)

    def test_a_lead_that_cannot_take_it_now_leaves_it_to_the_owner_at_once(self):
        cases = {"running": lambda key: self.lead_turn("running", key), "queued": lambda key: self.lead_turn("queued", key),
                 "waiting on a question of its checkpoint": lambda key: self.lead_turn("waiting_user", key),
                 "a guard": lambda key: projects.update(self.state, PID, lambda r: r.update(wakes={"streak": projects.DEFAULT_WAKES})),
                 "a limit": lambda key: mock.patch.object(project_loop, "limit_wait", return_value=("retry", NOW + 60)).start()}
        for number, (name, make) in enumerate(cases.items()):
            with self.subTest(name):
                key = f"lead-k{number}"
                make(key)
                self.write()
                self.owners()
                mock.patch.stopall()
                mock.patch.dict(os.environ, SWITCH).start()
                self.lead_turn("cancelled", key)
                projects.update(self.state, PID, lambda r: r.update(wakes={}))
        self.write()
        self.assertEqual(self.route(), flow.LEAD)  # All of it over: the lead takes it.

    def test_a_lead_waiting_on_its_own_project_ask_still_takes_questions(self):
        key = self.lead_turn("waiting_user")
        write_json(self.state / "questions" / ("e" * 16 + ".json"),
                   {"token": "e" * 16, "topic_id": LEAD, "chat_id": CHAT, "job_key": key, "status": "pending",
                    "origin": project_loop.ASK_ORIGIN}, ensure_ascii=False)
        self.write()
        self.assertEqual(self.route(), flow.LEAD)
        self.assertEqual(triage.state(self.saved()), triage.HELD)

    def test_leads_says_what_route_would(self):
        self.write()
        self.assertTrue(self.triage.leads(self.ctx, self.saved()))
        self.lead_turn("running")
        self.assertFalse(self.triage.leads(self.ctx, self.saved()))
        self.lead_turn("cancelled")
        self.assertFalse(self.triage.leads(self.ctx, {**self.saved(), "questions": [{**CHOICE[0], "kind": "deploy"}]}))


class EscalateRaceTest(Held):
    """§5.4 R1: the lead answering (project_answer) and the question handed to the owner (his word in the topic, or
    project_escalate in the same turn) with a copy read before: one of the two, never both."""

    def setUp(self):
        super().setUp()
        self.write()
        self.route()
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append), topics=mock.Mock())
        self.inside, self.go = threading.Event(), threading.Event()
        self.app = SimpleNamespace(questions=SimpleNamespace(submit_answer=self.submit))
        self.shown = mock.patch.object(flow, "show", return_value=True).start()

    def submit(self, ctx, chat, token, fields, event_id, *, by=None):
        self.inside.set()
        self.assertTrue(self.go.wait(10))
        write_json(self.path, {**self.saved(), "status": "answered", "answers": {"0": {"answers": ["新版"]}}}, ensure_ascii=False)

    def rows(self):
        return [row for row in self.reload().get("triage") or () if row.get("kind") in ("answered", "escalated")]

    def test_the_owners_word_while_the_lead_answers_leaves_the_answer_alone(self):
        old = self.saved()  # What owner_spoke read before it took the lock.
        result = {}
        lead = threading.Thread(target=lambda: result.setdefault("done", flow.answer(
            self.env, self.app, self.ctx, CHAT, self.reload(), {"0": "新版"}, "lead-r1")))
        lead.start()
        self.assertTrue(self.inside.wait(10))
        for by in ("owner", "lead", "timeout"):
            self.assertFalse(flow.escalate(self.env, old, "你在这个话题里说话了", by=by), by)
        self.go.set()
        lead.join(10)
        saved = self.saved()
        self.assertEqual((saved["status"], triage.state(saved), saved["display_pending"]), ("answered", triage.ANSWERED, False))
        self.shown.assert_not_called()  # No card: the lead answered.
        self.assertEqual([row["kind"] for row in self.rows()], ["answered"])

    def test_handed_to_the_owner_first_the_lead_answers_nothing(self):
        self.assertTrue(flow.escalate(self.env, self.saved(), "你在这个话题里说话了", by="owner"))
        with self.assertRaisesRegex(ValueError, "没有交给你的问题"):
            flow.answer(self.env, self.app, self.ctx, CHAT, self.reload(), {"0": "新版"}, "lead-r1")
        saved = self.saved()
        self.assertEqual((saved["status"], triage.state(saved), saved["display_pending"]), ("pending", triage.ESCALATED, True))
        self.assertFalse(self.inside.is_set())
        self.assertEqual([row["kind"] for row in self.rows()], ["escalated"])

    def test_the_answers_own_failure_still_hands_it_on(self):
        def failed(*args, **kwargs):
            raise ValueError("原生问题已失去原执行上下文")
        self.app.questions.submit_answer = failed
        with self.assertRaisesRegex(ValueError, "转给主人"):
            flow.answer(self.env, self.app, self.ctx, CHAT, self.reload(), {"0": "新版"}, "lead-r1")
        saved = self.saved()
        self.assertEqual((triage.state(saved), saved["triage"]["by"], saved["display_pending"]), (triage.ESCALATED, "error", True))


class ShowTest(Held):
    """The owner, 2026-11-19: T538's question the lead handed on (09:59) went to the topic's thread alone; the turn had ended
    while it was held, so no copy went to the private chat's top level. ``show`` makes that copy once the card is out."""

    def target(self, sent):
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class Ctx:
            state_dir: Path
            topic_id: str = ""
            reply_to_message_id: str = ""
            profile: str = "unit"

        display = mock.Mock()
        app = SimpleNamespace(questions=SimpleNamespace(display=display,
                                                        deliver_checkpoint_questions=mock.Mock(return_value=sent)))
        env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append),
                              topics=SimpleNamespace(target=lambda state, topic, chat: (app, Ctx(self.state), None)))
        return env, display

    def test_a_card_handed_on_to_the_owner_is_copied_to_the_top_level_with_what_the_turn_wrote(self):
        upsert_job_record(self.state, "k-bbbb", final_text="三张样卡在上面", reply_to_message_id="om_root")
        self.write(display_pending=False, question_message_id="om_q")
        env, display = self.target(True)
        self.assertTrue(flow.show(env, self.saved()))
        record, output = display.mirror.call_args.args[1:3]
        self.assertEqual((record["token"], output), (TOKEN, "三张样卡在上面"))
        self.assertEqual(display.mirror.call_args.kwargs["job"]["key"], "k-bbbb")

    def test_the_copy_of_a_question_handed_on_brings_the_turn_s_longer_progress_notes(self):
        from covey.core.trace import STAGE_HEADING, task_trace
        from covey.core.trace_store import save_trace
        upsert_job_record(self.state, "k-bbbb", final_text="三张样卡在上面", reply_to_message_id="om_root")
        trace = task_trace()
        trace.note("n1", "初步结果：v1.7-B 和 v1.8 都比基线多过题。" + "细节" * 200)
        save_trace(self.state, "k-bbbb", trace.snapshot())
        self.write(display_pending=False, question_message_id="om_q")
        env, display = self.target(True)
        self.assertTrue(flow.show(env, self.saved()))
        output = display.mirror.call_args.args[2]
        self.assertTrue(output.startswith("三张样卡在上面\n\n---\n\n" + STAGE_HEADING))
        self.assertIn("初步结果", output)

    def test_no_copy_when_the_card_did_not_go(self):
        upsert_job_record(self.state, "k-bbbb", final_text="x", reply_to_message_id="om_root")
        self.write()
        env, display = self.target(False)
        self.assertFalse(flow.show(env, self.saved()))
        display.mirror.assert_not_called()

    def test_a_live_question_in_a_task_runner_is_shown_by_that_runner(self):
        """T488 #5528, 2026-11-21: the lead handed an AskUserQuestion on; the Bridge tried the card itself with the runner's
        RemoteControl, which cannot, and the owner got nothing. The runner that waits on it shows it now."""
        self.write(native_live_question=True)
        env, display = self.target(True)
        questions = env.topics.target(None, None, None)[0].questions
        questions.runtime, questions.show_handed_on = SimpleNamespace(jobs=None), mock.Mock()
        for answer in (True, False):
            with mock.patch("covey.app.runner_ipc.show_in_runner", return_value=answer) as handed:
                self.assertIs(flow.show(env, self.saved()), answer)
            self.assertEqual(handed.call_args.args[2]["token"], TOKEN)
        questions.show_handed_on.assert_not_called()
        display.mirror.assert_not_called()  # The runner makes the top-level copy, with what the turn wrote.

    def test_a_live_question_of_a_turn_run_here_is_shown_here(self):
        self.write(native_live_question=True)
        env, _ = self.target(True)
        questions = env.topics.target(None, None, None)[0].questions
        job = SimpleNamespace(job_id=41)
        questions.runtime = SimpleNamespace(jobs=SimpleNamespace(live_job=lambda topic, chat, job_id: job))
        questions.show_handed_on = mock.Mock(return_value=True)
        with mock.patch("covey.app.runner_ipc.show_in_runner", return_value=None):
            self.assertTrue(flow.show(env, self.saved()))
        self.assertIs(questions.show_handed_on.call_args.args[1], job)
        questions.runtime = SimpleNamespace(jobs=SimpleNamespace(live_job=lambda topic, chat, job_id: None))
        with mock.patch("covey.app.runner_ipc.show_in_runner", return_value=None):
            self.assertFalse(flow.show(env, self.saved()))


class ShowInRunnerTest(Held):
    """The Bridge's side of runner_ipc.SHOW_QUESTION: the inbox message and its acknowledgement."""

    def test_no_runner_no_message(self):
        from covey.app import runner_ipc
        runtime = SimpleNamespace(in_runner=False)
        with mock.patch.object(runner_ipc, "runner_for", return_value=None), mock.patch.object(runner_ipc, "write_inbox") as wrote:
            self.assertIsNone(runner_ipc.show_in_runner(runtime, self.state, question()))
        wrote.assert_not_called()
        self.assertIsNone(runner_ipc.show_in_runner(SimpleNamespace(in_runner=True), self.state, question()))

    def test_the_runner_says_whether_the_card_went_and_an_older_one_says_nothing(self):
        from covey.app import runner_ipc
        runtime = SimpleNamespace(in_runner=False)
        for reply, expected in (({"ok": True}, True), ({"ok": False, "error": "x"}, False), (None, False)):
            with mock.patch.object(runner_ipc, "runner_for", return_value=self.state), \
                    mock.patch.object(runner_ipc, "write_inbox", return_value="op") as wrote, \
                    mock.patch.object(runner_ipc, "await_reply", return_value=reply):
                self.assertIs(runner_ipc.show_in_runner(runtime, self.state, question()), expected)
            self.assertEqual(wrote.call_args.args[1], {"kind": runner_ipc.SHOW_QUESTION, "token": TOKEN})


class GateTest(unittest.TestCase):
    """E4: the words that keep a question from the lead (the list was tuned against 30 days of the owner's questions)."""

    def test_the_words_the_owner_named_all_hit(self):
        for word in ("部署", "发布", "上线", "发给", "转账", "付费", "额度", "删除", "不可逆"):
            self.assertEqual(project_gates.hit([{"question": f"要{word}吗？", "options": [{"label": "是"}]}]), word)
        self.assertEqual(project_gates.hit([{"question": "git push --force 吗？", "options": [{"label": "是"}]}]), "force")
        self.assertEqual(project_gates.hit([{"question": "x", "options": [{"label": "rm", "description": ""}]}]), "rm")

    def test_ordinary_questions_pass(self):
        for text in ("用哪个库版本？", "首 token 延迟要不要盯？", "覆盖检查加在第几步？", "先做①还是②？", "format 用哪种？"):
            self.assertEqual(project_gates.hit([{"question": text, "options": [{"label": "A"}]}]), "", text)

    def test_options_and_headers_are_looked_at_too(self):
        self.assertEqual(project_gates.hit([{"header": "上线时间", "question": "什么时候？", "options": [{"label": "周一"}]}]), "上线")
        self.assertEqual(project_gates.hit([{"question": "怎么办？", "options": [{"label": "A", "description": "会花钱"}]}]), "花钱")

    def test_taking_machines_quota_and_services_hits(self):
        """T722, 2026-11-24 12:13: three questions asking to take cards, charge quota and make a service were held for the lead."""
        asked = [
            {"header": "A 型卡 走 W4A8", "question": "是否接受 W4A8 版按新模型走独立槽位试跑，并批准在 pool-a 的 zone-1 占 1 台 8 卡 A 型机（扣 pool-a shared）？",
             "options": [{"label": "接受，占 1 台 A 型卡 试跑（推荐）", "description": "不选的话 pool-a 的 70 张 A 型卡 只能空着或给评测用"},
                         {"label": "不接受量化版", "description": "只走原精度路线"}]},
            {"header": "pool-a B 型卡 +2台", "question": "是否批准 T27 在 pool-a 的 zone-2 再占 2 台 8 卡（B 型 128G 1 台、B 型 112G 1 台，"
             "扣 pool-a shared，不碰 pool-b），用已验收的原精度方案做单机验收？", "options": [{"label": "批准两台（推荐）"}, {"label": "暂不"}]},
            {"header": "pool-b C 型", "question": "是否批准在 pool-b 的 zone-3 建 1 个不挂业务标签的普通服务（挂 team-x 节点），"
             "占 1 台 C 型 整机（扣 pool-b dedicated），只做拓扑和 多卡通信 验证、不接流？",
             "options": [{"label": "批准 1 台验证（推荐）"}, {"label": "不用 pool-b 的 B 型卡"}]},
        ]
        for question in asked:
            self.assertTrue(project_gates.hit([question]), question["header"])
        self.assertLessEqual({"建 1 个不挂业务标签的普通服务", "占 1 台", "扣 pool-b dedicated"}, set(project_gates.hits([asked[2]])))
        for text in ("要扩容到 4 台吗？", "缩到 0 吗？", "释放这 8 张卡吗？", "现在申请配额吗？", "scale up 到 4 个吗？", "新建一个推理服务吗？"):
            self.assertTrue(project_gates.hit([{"question": text, "options": [{"label": "是"}]}]), text)

    def test_talk_of_cards_and_services_without_taking_them_passes(self):
        for text in ("建议先做中小模型服务的评测方案？", "执行 tailscale up 吗？", "扣分项按哪种算？", "这张卡的显存够不够？", "服务的首 token 慢在哪？"):
            self.assertEqual(project_gates.hit([{"question": text, "options": [{"label": "A"}]}]), "", text)

    def test_only_questions_with_options_are_choices(self):
        self.assertTrue(project_gates.choice_only(CHOICE))
        self.assertFalse(project_gates.choice_only([]))
        self.assertFalse(project_gates.choice_only([{"question": "x"}]))
        self.assertFalse(project_gates.choice_only("x"))


class SweepCase(Held):
    """A held question, its card's sending recorded (``shown``)."""

    def setUp(self):
        super().setUp()
        self.shown = []
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append), topics=mock.Mock())
        self.write()
        self.route()
        patch = mock.patch.object(flow, "show", side_effect=card_out(self.shown, self.state))
        patch.start()
        self.addCleanup(patch.stop)

    def receipts(self):
        return [item for item in project_loop.unread(self.state, PID) if item["kind"] == "question"]


class SweepTest(SweepCase):
    """The Bridge's look: the receipt for the lead, and the owner's turn once the three minutes are up."""

    def test_a_held_question_gets_one_receipt_that_names_it(self):
        flow.sweep(self.env, time.time())
        flow.sweep(self.env, time.time())
        found = self.receipts()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["thread"], THREAD)
        self.assertIn("用哪个库版本", found[0]["text"])
        self.assertTrue(triage.of(self.saved())["receipt"])
        self.assertEqual(self.shown, [])

    def test_a_receipt_wakes_the_lead_without_the_autowake_switch(self):
        flow.sweep(self.env, time.time())
        self.assertTrue(project_loop.autowake_due(self.receipts()))
        self.assertFalse(project_loop.autowake_due([{"kind": "thread_end"}]))

    def test_after_three_minutes_the_owner_gets_it(self):
        flow.sweep(self.env, time.time() + triage.DEADLINE + 1)
        record = self.saved()
        self.assertEqual((triage.state(record), record["triage"]["why"], record["display_pending"]),
                         (triage.ESCALATED, "3 分钟没人处理", False))
        self.assertEqual(self.shown, [TOKEN])
        row = self.reload()["triage"][-1]
        self.assertEqual(row["kind"], "escalated")
        self.assertIn("转给你", row["text"])
        flow.sweep(self.env, time.time() + 1000)  # Escalated is final: nothing more is sent.
        self.assertEqual(self.shown, [TOKEN])

    def test_once_it_is_the_owners_its_receipt_wakes_the_lead_no_more_and_no_turn_takes_it(self):
        """W2-C: the receipt the sweep wrote is settled when the question goes to the owner (here, its three minutes up)."""
        flow.sweep(self.env, time.time())
        [receipt] = self.receipts()
        self.assertEqual(triage.of(self.saved())["receipt"], receipt["id"])
        self.assertTrue(project_loop.autowake_due(self.receipts()))
        flow.sweep(self.env, time.time() + triage.DEADLINE + 1)
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(project_lead.take_receipts(self.state, PID, "lead-later"), [])
        self.assertFalse(project_lead.settle_receipt(self.state, PID, receipt["id"], "again"))  # Once.

    def test_a_record_of_an_older_release_settles_its_topics_unread_question_receipts(self):
        flow.sweep(self.env, time.time())
        data = self.saved()
        data["triage"]["receipt"] = True  # What an older release kept.
        write_json(self.path, data, ensure_ascii=False)
        project_lead.add_receipt(self.state, PID, "thread_end", "T41 做完了", thread=THREAD)
        flow.escalate(self.env, self.saved(), "要主人定", by="lead")
        self.assertEqual(self.receipts(), [])
        self.assertEqual([item["kind"] for item in project_loop.unread(self.state, PID)], ["thread_end"])

    def test_a_receipt_a_lead_turn_has_already_is_still_its_own(self):
        flow.sweep(self.env, time.time())
        [taken] = project_lead.take_receipts(self.state, PID, "lead-now")
        flow.escalate(self.env, self.saved(), "要主人定", by="lead")  # project_escalate in that same turn.
        self.assertEqual([item["id"] for item in project_lead.take_receipts(self.state, PID, "lead-now")], [taken["id"]])

    def test_an_answer_on_its_way_is_not_cut_off_by_the_time(self):
        late = time.time() + triage.DEADLINE + 1
        data = self.saved()
        data["triage"]["answering"] = late - 10
        write_json(self.path, data, ensure_ascii=False)
        flow.sweep(self.env, late)
        self.assertEqual(triage.state(self.saved()), triage.HELD)
        flow.sweep(self.env, late + triage.ANSWERING)
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)

    def test_a_project_that_is_not_on_hands_its_questions_back(self):
        projects.set_state(self.state, PID, projects.PAUSED, "stalled")
        flow.sweep(self.env, time.time())
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)
        self.assertEqual(self.shown, [TOKEN])

    def test_a_card_that_cannot_be_sent_leaves_the_task_asking_for_redelivery(self):
        write_job(self.state, "k-bbbb", topic_id=THREAD, chat_id=CHAT, job_id=41, status="waiting_user", question_delivery_ok=True,
                  checkpoint_question_deferred=True, execution_status="waiting_user")
        flow.show.side_effect = lambda env, record: False
        flow.sweep(self.env, time.time() + triage.DEADLINE + 1)
        job = find_job_record(self.state, "k-bbbb")
        self.assertEqual((job["status"], job["question_delivery_ok"]), ("failed", False))
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)

    def test_with_the_switch_off_the_sweep_sends_no_receipt(self):
        with mock.patch.dict(os.environ, {triage.ENV: "0"}):
            flow.sweep(self.env, time.time())
        self.assertEqual(self.receipts(), [])

    def test_the_loop_runs_the_sweep_each_look(self):
        loop = ProjectLoop(SimpleNamespace(log=self.logs.append, shutdown=mock.Mock()), self.env)
        loop.tick(time.time())
        self.assertEqual(len(self.receipts()), 1)


class StuckLoopTest(SweepCase):
    """T726, 2026-11-24: 12:13:14 the sweep wrote a held question's receipt, then the project loop's thread was stuck on the disk until
    the Bridge restarted at 12:35: the lead was not woken and the 12:16:03 deadline passed with nobody told."""

    def test_a_look_stuck_on_a_project_is_left_behind_and_the_late_question_still_goes_to_the_owner(self):
        gate, calls, real = threading.Event(), [], project_loop.unread

        def unread(state, pid):
            calls.append(pid)
            gate.wait(10)  # An NFS open that does not return.
            return real(state, pid)
        loop = ProjectLoop(SimpleNamespace(log=self.logs.append, shutdown=threading.Event()), self.env)
        with mock.patch.object(project_loop, "unread", side_effect=unread), \
                mock.patch.object(project_loop, "STUCK_SECONDS", 0.3, create=True):
            loop.step(time.time())
            old = loop.left[0].thread
            self.assertTrue(any("project loop stuck" in line and f"project:{PID}" in line for line in self.logs), self.logs)
            began = time.monotonic()
            loop.step(time.time() + triage.DEADLINE + 1)
            self.assertLess(time.monotonic() - began, 5)
            self.assertEqual(self.shown, [TOKEN])  # The sweep went on in the new thread.
            self.assertEqual(calls, [PID])  # The project the old look is stuck on is left out.
            gate.set()
            old.join(10)
        self.assertFalse(old.is_alive())
        self.assertFalse(project_loop.wakes_of(self.reload())["count"])  # The look that came back late woke nobody.

    def test_the_sweep_looks_at_every_question_only_now_and_then(self):
        flow.sweep(self.env, time.time())  # A process's first look: every question.
        data = self.saved()
        data["triage"]["answering"] = time.time()
        write_json(self.path, data, ensure_ascii=False)
        with mock.patch.object(flow, "lead_held", wraps=flow.lead_held) as every_question:
            flow.sweep(self.env, time.time())
            data = self.saved()
            data["status"] = "answered"  # The lead's answer landed: the question is no longer pending.
            write_json(self.path, data, ensure_ascii=False)
            flow.sweep(self.env, time.time())
        self.assertEqual(every_question.call_count, 0)
        self.assertEqual((triage.state(self.saved()), self.saved()["triage"]["by"]), (triage.ANSWERED, "lead"))

    def test_receipts_the_lead_has_read_are_read_from_disk_once(self):
        write_job(self.state, "k-lead", topic_id=LEAD, chat_id=CHAT, job_id=7, status="done")
        item = project_lead.add_receipt(self.state, PID, "thread_end", "做完了", thread=THREAD)
        path = project_lead.inbox_dir(self.state, PID) / (item["id"] + ".json")
        write_json(path, {**item, "taken_by": "k-lead"}, ensure_ascii=False)
        project_loop.receipts(self.state, PID)
        with mock.patch.object(project_loop, "read_object", wraps=project_loop.read_object) as reads:
            found = project_loop.receipts(self.state, PID)
            self.assertNotIn(item["id"], [item["id"] for item in project_loop.unread(self.state, PID)])
        self.assertIn(item["id"], [each.get("id") for each in found])
        self.assertNotIn(str(path), [str(call.args[0]) for call in reads.call_args_list])


class AnswerTest(Held):
    def setUp(self):
        super().setUp()
        self.write()
        self.route()
        self.submitted = []
        self.app = SimpleNamespace(questions=SimpleNamespace(submit_answer=self.submit))
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append), topics=mock.Mock())
        self.seen = {}
        self.failure = None

    def submit(self, ctx, chat, token, fields, event_id, *, by=None):
        self.seen = self.saved()
        self.submitted.append((chat, token, fields, event_id))
        self.by = by
        if self.failure:
            raise self.failure
        write_json(self.path, {**self.saved(), "status": "answered", "answers": {"0": {"answers": ["新版"]}}}, ensure_ascii=False)

    def answer(self, answers):
        return flow.answer(self.env, self.app, self.ctx, CHAT, self.reload(), answers, "lead-r1")

    def test_the_answer_goes_the_way_the_owners_click_does_with_a_note_that_the_lead_gave_it(self):
        done = self.answer({"0": "新版"})
        chat, token, fields, event = self.submitted[0]
        self.assertEqual((chat, token, event), (CHAT, TOKEN, "lead-r1"))
        self.assertEqual(fields["option_0"], "0")
        self.assertEqual(fields["supplement"], flow.NOTE)
        self.assertIn("负责人", flow.NOTE)
        # The lead's, in the owner's place (WAVE12 C2): never his decision, its continuation answer_for.
        self.assertEqual((self.by.party, self.by.channel, self.by.message_id), ("lead", "tool", "lead-r1"))
        # The form's own gate: shown as delivered when it was sent, and still the lead's while it went.
        self.assertFalse(self.seen["display_pending"])
        self.assertEqual(triage.state(self.seen), triage.HELD)
        self.assertEqual(triage.state(self.saved()), triage.ANSWERED)
        self.assertEqual(done["line"], "T41 · 用哪个库版本？ → 新版")
        row = self.reload()["triage"][-1]
        self.assertEqual((row["kind"], row["token"], row["topic"]), ("answered", TOKEN, THREAD))

    def test_an_option_that_is_not_one_refuses_and_the_question_stays_held(self):
        with self.assertRaisesRegex(ValueError, "不是"):
            self.answer({"0": "随便"})
        with self.assertRaisesRegex(ValueError, "单选"):
            self.answer({"0": ["新版", "旧版"]})
        self.assertEqual(self.submitted, [])
        self.assertEqual((triage.state(self.saved()), self.saved()["display_pending"]), (triage.HELD, True))

    def test_an_answer_that_could_not_be_sent_hands_the_question_to_the_owner(self):
        self.failure = ValueError("原生问题已失去原执行上下文")
        with mock.patch.object(flow, "show", return_value=True) as show, self.assertRaisesRegex(ValueError, "转给主人"):
            self.answer({"0": "新版"})
        self.assertEqual((triage.state(self.saved()), self.saved()["display_pending"]), (triage.ESCALATED, True))
        show.assert_called_once()

    def test_nothing_held_nothing_to_answer(self):
        data = self.saved()
        data["triage"]["state"] = triage.ESCALATED
        write_json(self.path, data, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "没有交给你"):
            self.answer({"0": "新版"})


class ToolTest(ToolsCase):
    """project_answer and project_escalate as the lead calls them."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, SWITCH).start()
        self.addCleanup(mock.patch.stopall)
        self.topic = self.tool("project_topic_start", request_id="m1", title="导出旧账单", prompt="把旧库账单导出", workspace="project",
                               readonly=True, why="脚本在这个仓库")["data"]["topic_id"]
        self.path = self.state / "questions" / (TOKEN + ".json")
        write_json(self.path, question(topic=self.topic, job_key="x-1"), ensure_ascii=False)
        # The member asked while the lead was idle (W1-A C4 ③: a busy lead's question goes to the owner at once); the wake-up's
        # turn, ``lead-job``, answers it.
        with mock.patch.object(project_loop, "lead_busy", return_value=False):
            flow.Triage(runtime=SimpleNamespace(log=print), cards=mock.Mock())._route(SimpleNamespace(state_dir=self.state), read_object(self.path))
        self.label = "T" + str(json.loads((self.state / "topics" / self.topic / "topic.json").read_text())["topic_number"])

    def owner_ground(self) -> str:
        """A line of the owner's answer to an earlier question card (his, with its origin: a ground project_answer may name)."""
        record = {"token": "d" * 16, "topic_id": self.topic, "job_id": 3, "questions": CHOICE, "answers": {"0": {"answers": ["旧版"]}}}
        [(entry, _)] = project_memory.note_answers(self.state, PID, record, [{"index": 1, "item": "0", "text": "主人答：用哪个库版本？→旧版"}])
        return entry["id"]

    def test_the_lead_answers_and_the_row_is_on_the_card(self):
        app = self.env.topics.target(self.state, self.topic, CHAT)[0]
        with mock.patch.object(app.questions, "submit_answer", side_effect=lambda *a, **k: write_json(
                self.path, {**read_object(self.path), "status": "answered", "answers": {"0": {"answers": ["旧版"]}}},
                ensure_ascii=False)) as submit:
            answer = self.tool("project_answer", thread=self.label, answers={"0": "旧版"}, evidence=[self.owner_ground()])
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(submit.call_args.args[3]["supplement"].startswith("（负责人替主人答的，依据："), submit.call_args.args[3])
        self.assertEqual(triage.state(read_object(self.path)), triage.ANSWERED)
        self.assertEqual(project_facts.triage_lines(self.reload())[0][1], TOKEN)

    def test_without_collab_an_answer_on_no_grounds_is_refused(self):
        # W0-2 (T892 P1-5): with 协作 (COVEY_COLLAB) off, project_answer checked nothing: the lead answered for the owner on nothing.
        self.assertNotEqual(os.environ.get("COVEY_COLLAB"), "1")
        app = self.env.topics.target(self.state, self.topic, CHAT)[0]
        with mock.patch.object(app.questions, "submit_answer") as submit:
            answer = self.tool("project_answer", thread=self.label, answers={"0": "旧版"})
            lead, _ = project_memory.add(self.state, PID, "这个专项一律用旧版", "decision", "lead")
            own = self.tool("project_answer", thread=self.label, answers={"0": "旧版"}, evidence=[lead["id"]])
        for refused in (answer, own):
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["data"]["refused"], "evidence")
        self.assertIn("待核对参考", own["text"])  # The lead's own line is no ground alone.
        submit.assert_not_called()
        self.assertEqual(triage.state(read_object(self.path)), triage.HELD)
        self.assertIn("project_answer 必须写 evidence", project_lead.lead_rules())
        (tool,) = [tool for tool in butler_tool_specs.lead_tools({}) if tool["name"] == "project_answer"]
        self.assertIn("evidence", tool["input_schema"]["required"])

    def test_the_lead_hands_the_question_back_with_why(self):
        with mock.patch.object(flow, "show", return_value=True) as show:
            answer = self.tool("project_escalate", thread=self.label, why="记忆里没有这个版本的依据")
        self.assertTrue(answer["ok"], answer)
        record = read_object(self.path)
        self.assertEqual((triage.state(record), record["triage"]["why"], record["triage"]["by"]),
                         (triage.ESCALATED, "记忆里没有这个版本的依据", "lead"))
        self.assertTrue(record["display_pending"])
        show.assert_called_once()
        self.assertIn("记忆里没有这个版本的依据", json.dumps(question_card({**record, "display_pending": False}, "T1"), ensure_ascii=False))

    def test_the_lead_may_add_background_and_a_recommendation_to_the_card(self):
        with mock.patch.object(flow, "show", return_value=True):
            answer = self.tool("project_escalate", thread=self.label, why="要不要停服务得你定", context="导出会锁表约 5 分钟",
                               recommend="今晚 23 点", if_not="白天停会挡住下单")
        self.assertTrue(answer["ok"], answer)
        record = read_object(self.path)
        self.assertEqual((record["triage"]["context"], record["triage"]["recommend"], record["triage"]["if_not"]),
                         ("导出会锁表约 5 分钟", "今晚 23 点", "白天停会挡住下单"))
        text = json.dumps(question_card({**record, "display_pending": False}, "T1"), ensure_ascii=False)
        for words in ("背景：导出会锁表约 5 分钟", "负责人推荐：今晚 23 点", "不选它的代价：白天停会挡住下单"):
            self.assertIn(words, text)

    def test_without_them_the_escalated_card_is_as_before(self):
        with mock.patch.object(flow, "show", return_value=True):
            self.tool("project_escalate", thread=self.label, why="记忆里没有依据")
        record = read_object(self.path)
        self.assertFalse({"context", "recommend", "if_not"} & set(record["triage"]))
        text = json.dumps(question_card({**record, "display_pending": False}, "T1"), ensure_ascii=False)
        self.assertNotIn("负责人推荐", text)
        self.assertNotIn("背景：", text)

    def test_a_topic_that_is_not_the_projects_or_has_nothing_held_says_so(self):
        again = self.tool("project_escalate", thread=self.label, why="x")  # Holds nothing once handed back.
        with mock.patch.object(flow, "show", return_value=True):
            again = self.tool("project_escalate", thread=self.label, why="x")
        self.assertFalse(again["ok"])
        self.assertIn("没有交给你", again["text"])

    def test_without_the_switch_they_say_it_is_off(self):
        os.environ[triage.ENV] = "0"
        for name, args in (("project_answer", {"thread": "T1", "answers": {"0": "新版"}}), ("project_escalate", {"thread": "T1", "why": "x"})):
            answer = self.tool(name, **args)
            self.assertFalse(answer["ok"])
            self.assertIn("问题分流没有开", answer["text"])


class LeadSideTest(Held):
    """What the lead reads, and which tools it has."""

    def test_the_lead_reads_the_held_question_in_its_state_pack(self):
        self.write()
        self.route()
        pack = project_lead.state_pack(self.state, self.reload(), "lead-1", full=False)
        self.assertIn("交给你的话题问题", pack)
        self.assertIn("[0] 用哪个库版本？", pack)
        self.assertIn("新版 / 旧版", pack)
        self.assertIn("project_answer", pack)

    def test_without_the_switch_the_pack_and_the_rules_are_what_they_were(self):
        self.write()
        self.route()
        os.environ[triage.ENV] = "0"
        self.assertNotIn("交给你的话题问题", project_lead.state_pack(self.state, self.reload(), "lead-1", full=False))
        rules = project_lead.lead_rules()
        self.assertIn(project_lead.NOT_YET, rules)
        self.assertNotIn("project_answer", rules)

    def test_with_the_switch_the_rules_say_how_to_triage_and_no_longer_that_it_is_off(self):
        rules = project_lead.lead_rules()
        self.assertNotIn("分流还没开", rules)
        for word in ("project_answer", "project_escalate", "分流记录", "自由文字"):
            self.assertIn(word, rules)
        self.assertNotIn("不对", rules)  # The 分流记录's 不对 is retired (T898 U7): the owner says it in the topic.

    def test_the_two_tools_are_listed_to_the_lead_only_with_the_switch(self):
        names = lambda environ: [tool["name"] for tool in butler_tool_specs.listed({butler.LEAD_ENV: PID, **environ})]  # noqa: E731
        self.assertNotIn("project_answer", names({triage.ENV: "0"}))
        self.assertNotIn("project_escalate", names({triage.ENV: "0"}))
        self.assertEqual(names({triage.ENV: "1"})[-2:], ["project_answer", "project_escalate"])
        self.assertEqual(names({}), names({triage.ENV: "1"}))  # Unset is on.
        self.assertEqual(names({triage.ENV: "1"})[:-2], names({triage.ENV: "0"}))
        self.assertNotIn("project_answer", [tool["name"] for tool in butler_tool_specs.listed({triage.ENV: "1"})])  # Not the butler's.

    def test_the_server_is_told_the_switch_only_when_it_is_on(self):
        config = lambda: butler.mcp_config(Path("/r"), self.state, "k", "python3", lead=PID)["mcpServers"]["bridge"]["env"]  # noqa: E731
        self.assertIn(triage.ENV, config())
        os.environ[triage.ENV] = "0"
        self.assertEqual(config()[triage.ENV], "0")  # The server is told it is off: unset would mean on.


class CardTest(Held):
    def test_the_row_of_an_answer_has_no_wrong_button_any_more_and_shows_the_mark_once_corrected(self):
        flow.log_row(self.state, PID, "答了：T41 · 用哪个库版本？ → 新版", token=TOKEN, topic=THREAD, kind="answered")
        flow.log_row(self.state, PID, "转给你：T41 · 能不能停服务？", kind="escalated")
        lines = project_facts.triage_lines(self.reload())
        self.assertEqual(lines[1][1], TOKEN)  # The older one, the answer.
        self.assertIsInstance(lines[0], str)  # The hand-back has no button.
        view = project_card.Project(**{**project_card_sample().__dict__, "triage": lines})
        buttons = [b for b in walk(project_card.triage_rows(view)) if b.get("tag") == "button"]
        self.assertEqual(buttons, [])  # The 不对 button was taken off (T898); a row is a line.
        projects.update(self.state, PID, lambda data: data["triage"][0].__setitem__("corrected", True))
        self.assertTrue(project_facts.triage_lines(self.reload())[1].endswith("（已纠正）"))

    def test_a_long_corrected_row_keeps_its_mark_when_it_is_clipped(self):
        # The acceptance of 2026-11-17: “…→ LF（已纠…”, the mark cut off at the row's width.
        long = "答了：T11 · g.txt 用 LF 还是 CRLF 换行？这个文件之后还要给 Windows 上的同事用，" * 3 + "→ LF"
        flow.log_row(self.state, PID, long, token=TOKEN, topic=THREAD, kind="answered")
        projects.update(self.state, PID, lambda data: data["triage"][-1].__setitem__("corrected", True))
        lines = project_facts.triage_lines(self.reload())
        view = project_card.Project(**{**project_card_sample().__dict__, "triage": lines})
        shown = [e["content"] for e in walk(project_card.triage_rows(view)) if e.get("tag") == "markdown"]
        self.assertEqual(len(shown), 1)
        self.assertIn("…", shown[0])
        self.assertTrue(shown[0].endswith("（已纠正）</font>"), shown[0])

    def test_rows_without_a_token_draw_as_they_always_did(self):
        view = project_card.Project(**{**project_card_sample().__dict__, "triage": ("09:40 答了：用哪个库版本？",)})
        self.assertFalse([b for b in walk(project_card.triage_rows(view)) if b.get("tag") == "button"])

    def test_the_task_card_says_the_question_is_with_the_lead(self):
        self.write()
        self.route()
        element = question_element(self.saved())
        self.assertEqual((element["element_id"], element["content"]), ("q_" + TOKEN, HELD_NOTE))
        self.assertIn("负责人", HELD_NOTE)

    def test_a_question_handed_back_says_why_on_its_card(self):
        record = question(display_pending=False, triage={"state": triage.ESCALATED, "why": "记忆里没有依据"})
        self.assertIn("记忆里没有依据", json.dumps(question_card(record, "T41"), ensure_ascii=False))
        self.assertNotIn("负责人转给你", json.dumps(question_card(question(display_pending=False), "T41"), ensure_ascii=False))


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from walk(value)


def project_card_sample():
    return project_card.Project(project_id=PID, name="迁移账单", status="在做", lead_topic=LEAD)


class HeldFixture(QuestionFixture, unittest.TestCase):
    """E2: the real Harness app, a task that ended waiting for the owner, and its question held for a project's lead."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, SWITCH).start()
        self.addCleanup(mock.patch.stopall)
        state = self.ctx_topic.state_dir
        self.record.update(body_delivery_required=True, display_pending=True, body_delivery_confirmed=False,
                           request_id="checkpoint-" + self.job.message_id, job_key=self.job.message_id,
                           questions=[{"id": "0", "header": "版本", "question": "用哪个库版本？", "options": [{"label": "新版"}, {"label": "旧版"}]}])
        self.path.write_text(json.dumps(self.record))
        projects.create(state, projects.new_record(chat_id="oc_chat", name="迁移账单", goal="目标", project_id=PID, now=NOW))
        projects.add_member(state, PID, self.job.topic_id)
        topic = state / "topics" / self.job.topic_id / "topic.json"
        data = json.loads(topic.read_text()) if topic.exists() else {}
        write_json(topic, {**data, "topic_id": self.job.topic_id, "chat_id": "oc_chat",
                           "project": projects.project_field(PID, projects.MEMBER)}, ensure_ascii=False)
        projects.update(state, PID, lambda data: data.update(lead_topic_id=LEAD, chat_id="oc_chat"))
        self.app.questions.checkpoints.result_delivered = lambda ctx, record: True
        self.app.questions.publish_question_card = mock.Mock(return_value=True)
        self.state = state
        upsert_job_record(state, self.job.message_id, status="waiting_user", execution_status="waiting_user", delivery_ok=True,
                          question_delivery_ok=True, checkpoint_question_deferred=True, topic_id=self.job.topic_id,
                          chat_id="oc_chat", job_id=self.job.job_id, session_id="s", workdir=str(self.root))

    def deliver(self):
        return self.app.questions.deliver_checkpoint_questions(self.ctx_topic, self.job, False)


class HeldDeliveryTest(HeldFixture):
    """(docs/projects.md 专项的问题分流): a held question is not a question that failed to go out. Bridge restart and task process alike."""

    def test_a_held_question_is_delivered_as_far_as_the_task_is_concerned(self):
        self.assertTrue(self.deliver())
        self.assertFalse(self.app.questions.publish_question_card.called)
        saved = json.loads(self.path.read_text())
        self.assertEqual((triage.state(saved), saved["status"], saved["display_pending"], saved["body_delivery_confirmed"]),
                         (triage.HELD, "pending", True, True))
        self.app.cards.set_task_element.assert_called()  # The task card says 已交负责人.
        self.assertEqual(self.app.cards.set_task_element.call_args.args[2]["content"], HELD_NOTE)

    def test_nothing_offers_a_held_question_again(self):
        self.deliver()
        self.assertEqual(self.app.questions.undelivered_checkpoint_jobs(self.ctx_topic, "oc_chat"), [])
        before = find_job_record(self.state, self.job.message_id)
        self.assertEqual(self.deliver(), True)  # The topic's redelivery, as 重新投递 would run it.
        self.assertFalse(self.app.questions.publish_question_card.called)
        self.assertEqual(json.loads(self.path.read_text())["triage"]["deadline"], triage.of(json.loads(self.path.read_text()))["deadline"])
        self.assertEqual(find_job_record(self.state, self.job.message_id)["question_delivery_ok"], before["question_delivery_ok"])

    def test_the_recovery_card_of_a_held_question_is_the_one_of_a_shown_one(self):
        self.deliver()
        held = find_job_record(self.state, self.job.message_id)
        shown_record = dict(held)
        self.assertEqual(recovery.classify(held, saved=False), recovery.classify(shown_record, saved=False))
        self.assertNotEqual(recovery.classify(held, saved=False).kind, recovery.REDELIVER_FAILED)

    def test_a_bridge_restart_leaves_a_held_question_alone_and_still_fails_an_unshown_one(self):
        self.deliver()
        patch = mock.patch("covey.app.questions.live_keys", return_value=set())  # The task ended: its runner does not keep the question.
        patch.start()
        self.addCleanup(patch.stop)
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        job = find_job_record(self.state, self.job.message_id)
        self.assertEqual((job["status"], job["question_delivery_ok"]), ("waiting_user", True))

    def test_the_same_record_without_the_hold_fails_the_job_at_a_restart(self):
        self.deliver()
        patch = mock.patch("covey.app.questions.live_keys", return_value=set())
        patch.start()
        self.addCleanup(patch.stop)
        data = json.loads(self.path.read_text())
        data.pop("triage")
        self.path.write_text(json.dumps(data))
        from covey.core.store import invalidate_directory
        invalidate_directory(self.path.parent)  # The test wrote the file itself.
        recover_question_delivery(self.ctx_topic, [self.app.questions], self.app.runtime.log)
        job = find_job_record(self.state, self.job.message_id)
        self.assertEqual((job["status"], job["question_delivery_ok"]), ("failed", False))

    def test_handed_back_it_goes_out_through_the_same_door_as_any_question(self):
        self.deliver()
        record = json.loads(self.path.read_text())
        self.app.questions.publish_question_card.reset_mock()
        moved = triage.moved(record, triage.ESCALATED, why="记忆里没有依据")
        self.path.write_text(json.dumps({**moved, "display_pending": True}))
        self.assertTrue(self.deliver())
        self.assertTrue(self.app.questions.publish_question_card.called)
        saved = json.loads(self.path.read_text())
        self.assertFalse(saved["display_pending"])
        self.assertEqual(triage.state(saved), triage.ESCALATED)

    def test_with_the_switch_off_the_card_goes_out_as_always(self):
        os.environ[triage.ENV] = "0"
        self.assertTrue(self.deliver())
        self.assertTrue(self.app.questions.publish_question_card.called)
        self.assertNotIn("triage", json.loads(self.path.read_text()))

    def test_a_free_text_question_goes_out_even_with_the_switch(self):
        self.record["questions"] = [{"id": "0", "question": "写一句原因"}]
        self.path.write_text(json.dumps(self.record))
        self.assertTrue(self.deliver())
        self.assertTrue(self.app.questions.publish_question_card.called)


class HeldLiveQuestionTest(HeldFixture):
    """The live question of Claude (the task process stays up, waiting): no card, and the job says its question is delivered."""

    def test_a_live_question_is_held_without_a_card(self):
        self.record.update(native_live_question=True, display_pending=True, body_delivery_required=False)
        self.path.write_text(json.dumps(self.record))
        record = json.loads(self.path.read_text())
        self.app.cards.close_streaming_reply = mock.Mock()
        self.app.questions.native.show(self.ctx_topic, self.job, self.rpc, self.path, record, False, True)
        self.assertFalse(self.app.questions.publish_question_card.called)
        self.assertFalse(self.app.cards.close_streaming_reply.called)  # The turn goes on waiting, its card as it is.
        self.assertEqual(triage.state(json.loads(self.path.read_text())), triage.HELD)
        self.assertTrue(find_job_record(self.state, self.job.message_id)["question_delivery_ok"])

    def test_with_the_switch_off_the_form_is_sent_as_always(self):
        os.environ[triage.ENV] = "0"
        self.record.update(native_live_question=True, display_pending=True, body_delivery_required=False)
        self.path.write_text(json.dumps(self.record))
        record = json.loads(self.path.read_text())
        self.app.cards.close_streaming_reply = mock.Mock()
        self.rpc.control_lock = threading.RLock()
        with mock.patch.object(self.app.questions.native, "live_control"):
            self.app.questions.native.show(self.ctx_topic, self.job, self.rpc, self.path, record, False, True)
        self.assertTrue(self.app.questions.publish_question_card.called)
        self.assertNotIn("triage", json.loads(self.path.read_text()))


class EscalatedLiveQuestionTest(HeldFixture):
    """The process that runs the turn shows a live question the lead handed on (show_handed_on): the card, and the top-level copy
    with what the turn wrote before it asked."""

    def setUp(self):
        super().setUp()
        self.record.update(native_live_question=True, display_pending=True, body_delivery_required=False)
        self.path.write_text(json.dumps(self.record))
        self.rpc.control_lock = threading.RLock()
        self.job.cancel_event = threading.Event()
        self.app.questions.display.mirror = mock.Mock(return_value="om_top")
        self.app.questions.display.paint_task_card = mock.Mock()
        mock.patch.object(self.app.questions.native, "live_control").start()

    def escalated(self):
        record = json.loads(self.path.read_text())
        record["triage"] = {"state": triage.ESCALATED, "project": PID, "by": "lead"}
        self.path.write_text(json.dumps(record))
        return record

    def test_the_card_goes_out_and_is_copied_with_what_the_turn_wrote(self):
        with mock.patch("covey.app.questions.turn_output", return_value="第 4 批三列报告") as wrote:
            self.assertTrue(self.app.questions.show_handed_on(self.ctx_topic, self.job, self.escalated()))
        self.assertTrue(self.app.questions.publish_question_card.called)
        self.assertFalse(json.loads(self.path.read_text())["display_pending"])
        self.assertTrue(find_job_record(self.state, self.job.message_id)["question_delivery_ok"])
        record, output = self.app.questions.display.mirror.call_args.args[1:3]
        self.assertEqual((record["token"], output), (self.token, "第 4 批三列报告"))
        self.assertIs(wrote.call_args.args[0], self.job)
        self.assertTrue(self.app.questions.display.paint_task_card.called)

    def test_a_card_that_did_not_go_is_not_copied(self):
        self.app.questions.publish_question_card.return_value = False
        self.assertFalse(self.app.questions.show_handed_on(self.ctx_topic, self.job, self.escalated()))
        self.app.questions.display.mirror.assert_not_called()
        self.assertFalse(find_job_record(self.state, self.job.message_id)["question_delivery_ok"])

    def test_a_held_question_gets_no_top_level_line_while_the_lead_has_it(self):
        record = json.loads(self.path.read_text())
        record["triage"] = {"state": triage.HELD, "project": PID, "deadline": time.time() + 100}
        self.path.write_text(json.dumps(record))
        with mock.patch("covey.app.butler_relay.question_asked") as asked:
            self.app.questions.native.mirror(self.ctx_topic, self.job, self.path)
        self.app.questions.display.mirror.assert_not_called()
        asked.assert_not_called()


class MemoryInterplayTest(Held):
    """COVEY_PROJECT_MEMORY=1 with the triage: what the lead answered for the owner is not noted as the owner's answer; what the
    owner answered after a question came to them, and what they corrected, is."""

    def setUp(self):
        super().setUp()
        mock.patch.dict(os.environ, {projects.MEMORY_ENV: "1"}).start()
        self.write()
        self.route()

    def notes(self):
        rows = project_memory.stored(self.state, PID)  # And the owner's answers in decisions.jsonl (core/memstore.py).
        return [(row["kind"], row["source"], row["text"]) for row in rows + project_context.decision_rows(self.state, PID, rows)]

    def answered(self, record):
        return {**record, "status": "submitting", "answers": {"0": {"answers": ["新版"]}}}

    def test_the_saved_record_of_a_held_question_still_says_held_while_the_lead_answers_it(self):
        from covey.core.store import transition_question
        saved = transition_question(self.path, {"pending"}, status="submitting", answers={"0": {"answers": ["新版"]}})
        self.assertEqual(triage.state(saved), triage.HELD)

    def test_an_answer_the_lead_gave_is_not_noted_as_the_owners(self):
        project_context.remember_answer(self.state, THREAD, self.answered(self.saved()), self.logs.append)
        self.assertEqual(self.notes(), [])

    def test_an_answer_the_owner_gave_to_a_question_handed_back_is_noted(self):
        data = self.saved()
        write_json(self.path, triage.moved(data, triage.ESCALATED, why="碰到硬闸", by="lead", at=time.time()), ensure_ascii=False)
        project_context.remember_answer(self.state, THREAD, self.answered(self.saved()), self.logs.append, owners=True)
        self.assertEqual([(kind, source) for kind, source, _ in self.notes()], [("decision", "owner_answer")])
        self.assertIn("新版", self.notes()[0][2])

    def test_an_answer_to_a_question_the_triage_never_saw_is_noted_as_it_was(self):
        data = self.saved()
        data.pop("triage")
        project_context.remember_answer(self.state, THREAD, self.answered(data), self.logs.append, owners=True)
        self.assertEqual([source for _, source, _ in self.notes()], ["owner_answer"])

class PauseInterplayTest(Held):
    """A tokens pause stops the member topics' tasks (COVEY_PROJECT_ASK=1) while a question of theirs is held for the lead: the
    question goes to the owner once, on the sweep that finds the project no longer on hands, and is not sent again."""

    def test_a_held_question_goes_to_the_owner_once_when_the_tokens_pause_comes(self):
        from test_project_loop import lead_app
        mock.patch.dict(os.environ, {projects.ASK_ENV: "1", projects.BUDGET_ENV: "10000000"}).start()
        shown = []
        mock.patch.object(flow, "show", side_effect=card_out(shown, self.state)).start()
        self.write()
        self.route()
        projects.update(self.state, PID, lambda data: data.update(tokens_used=10_000_000))
        stop = mock.Mock(return_value=1)
        runtime = SimpleNamespace(log=self.logs.append, shutdown=mock.Mock())
        env = SimpleNamespace(state_dir=self.state, runtime=runtime, topics=SimpleNamespace(apps={"claude": lead_app(self)}))
        loop = ProjectLoop(runtime, env, stop)
        start = time.time()
        loop.tick(start)  # The sweep looks first: the project is on hands, the question is held and gets its receipt; then the guard.
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.PAUSED, "tokens"))
        self.assertEqual(stop.call_args.args[2], [THREAD])
        self.assertEqual((triage.state(self.saved()), shown), (triage.HELD, []))
        loop.tick(start + 10)  # The next look finds the project paused and hands the question back.
        self.assertEqual((triage.state(self.saved()), shown), (triage.ESCALATED, [TOKEN]))
        self.assertEqual(triage.of(self.saved())["why"], "专项现在没有在做")
        for later in (20, 40, 600):
            loop.tick(start + later)
        self.assertEqual(shown, [TOKEN])  # Never sent twice.
        rows = [row["kind"] for row in self.reload()["triage"]]
        self.assertEqual(rows.count("escalated"), 1)
        # W2-C: the question is the owner's now, its receipt settled: it wakes the lead no more.
        self.assertEqual(len([item for item in project_loop.unread(self.state, PID) if item["kind"] == "question"]), 0)
        [receipt] = [item for item in project_lead.inbox_items(self.state, PID) if item["kind"] == "question"]
        self.assertTrue(receipt["done"]["why"].startswith("转给主人"))

    def test_the_loop_is_wired_with_the_stop_and_the_end_of_a_turn_still_gets_all_its_notes(self):
        unified = (Path(project_loop.__file__).parent / "unified.py").read_text()
        self.assertIn("ProjectLoop(self.runtime, self.butler_env, stop_tasks)", unified)
        mock.patch.dict(os.environ, {projects.MEMORY_ENV: "1"}).start()
        checkpoint = self.state / "cp.json"
        write_json(checkpoint, {"artifacts": ["/data/out/bill.csv"]})
        write_job(self.state, "m-1", topic_id=THREAD, chat_id=CHAT, status="done", message_id="m", checkpoint_path=str(checkpoint), job_id=1)
        job = SimpleNamespace(topic_id=THREAD, chat_id=CHAT, message_id="m", job_id=1)
        with mock.patch.object(project_loop, "job_key", return_value="m-1"):
            project_loop.after_runner(self.logs.append, self.state, job)
        self.assertEqual([item["kind"] for item in project_loop.unread(self.state, PID)], ["thread_end"])
        self.assertEqual([row["ref"] for row in project_library.entries(self.state, PID)], ["/data/out/bill.csv"])
        self.assertIn("m-1", self.reload()["token_jobs"])


class ToolListInterplayTest(unittest.TestCase):
    """The tool lists with the triage's switch off and on, and COVEY_DIRECTIONS (gone) doing nothing."""
    BUTLER = {"COVEY_BUTLER_RULES": "1", "COVEY_PROJECTS": "1", triage.ENV: "0"}

    def test_off_nothing_of_the_new_features_is_listed(self):
        listed = {tool["name"]: tool for tool in butler_tool_specs.listed(self.BUTLER)}
        self.assertFalse({"direction", "direction_by_owner"} & set(listed["topic_start"]["input_schema"]["properties"]))
        lead = [tool["name"] for tool in butler_tool_specs.listed({**self.BUTLER, butler_tool_specs.LEAD_ENV: PID})]
        self.assertFalse({"project_answer", "project_escalate"} & set(lead))

    def test_on_each_adds_its_own_and_the_memory_kinds_are_there_too(self):
        start = {tool["name"]: tool for tool in butler_tool_specs.listed({**self.BUTLER, "COVEY_DIRECTIONS": "1"})}["topic_start"]["input_schema"]["properties"]
        self.assertTrue({"project", "harness", "model", "effort"} <= set(start))
        self.assertFalse({"direction", "direction_by_owner"} & set(start))  # COVEY_DIRECTIONS is not read any more.
        lead = {tool["name"]: tool for tool in butler_tool_specs.listed({**self.BUTLER, butler_tool_specs.LEAD_ENV: PID, triage.ENV: "1"})}
        self.assertTrue({"project_answer", "project_escalate", "project_remember", "project_topic_start"} <= set(lead))
        self.assertEqual(lead["project_remember"]["input_schema"]["properties"]["kind"]["enum"], ["rule", "note"])
        self.assertIn("harness", lead["project_topic_start"]["input_schema"]["properties"])


# Review fixes (s2/review): the owner speaking, the switch going off, an answer that never landed.

class HeldSpeakTest(HeldFixture):
    """The owner writes in a topic whose question the project's lead holds: the question is theirs first, then their message is handled."""

    def setUp(self):
        super().setUp()
        self.deliver()
        self.env = SimpleNamespace(state_dir=self.state, runtime=self.app.runtime,
                                   topics=SimpleNamespace(target=lambda state, topic, chat: (self.app, self.ctx_topic, None)))
        patch = mock.patch.object(flow, "ENV", self.env)
        patch.start()
        self.addCleanup(patch.stop)
        caps = mock.patch("covey.core.harness.backend_capabilities", return_value=SimpleNamespace(questions=True, steering=True))
        caps.start()
        self.addCleanup(caps.stop)
        self.app.questions.submit_answer = mock.Mock()
        self.app.questions.hold_note = mock.Mock()
        self.app.messenger.reply = mock.Mock(return_value=True)
        self.app.questions.publish_question_card.reset_mock()

    def speak(self, text="新版"):
        return self.app.interaction.interactive_command(self.ctx_topic, text, "oc_chat", "m-owner")

    def test_the_held_question_goes_to_the_owner_and_their_message_is_held_for_its_answer(self):
        # WAVE12 C2's second condition: the question is shown to him only now, so what he wrote is no answer to it (he had
        # not seen it), but a note that goes with the answer he gives on its card.
        self.assertTrue(self.speak())
        saved = json.loads(self.path.read_text())
        self.assertEqual((triage.state(saved), saved["triage"]["by"], saved["triage"]["why"]), (triage.ESCALATED, "owner", "你在这个话题里说话了"))
        self.assertTrue(self.app.questions.publish_question_card.called)
        self.app.questions.submit_answer.assert_not_called()
        self.assertEqual(self.app.questions.hold_note.call_args.args[2], "新版")
        self.app.messenger.reply.assert_not_called()

    def test_several_questions_keep_the_message_as_a_note(self):
        data = json.loads(self.path.read_text())
        data["questions"].append({"id": "1", "header": "顺序", "question": "先做哪个？", "options": [{"label": "A"}, {"label": "B"}]})
        self.path.write_text(json.dumps(data))
        self.assertTrue(self.speak("另外看下日志"))
        self.app.questions.hold_note.assert_called_once()
        self.app.questions.submit_answer.assert_not_called()

    def test_a_card_that_cannot_be_sent_leaves_the_old_refusal(self):
        with mock.patch.object(flow, "show", return_value=False):
            self.assertTrue(self.speak())
        self.assertIn("尚未送达", self.app.messenger.reply.call_args.args[2])
        self.app.questions.submit_answer.assert_not_called()

    def test_an_answer_of_the_lead_on_its_way_is_not_taken_away(self):
        data = json.loads(self.path.read_text())
        data["triage"]["answering"] = time.time()
        data["display_pending"] = False
        self.path.write_text(json.dumps(data))
        with mock.patch.object(flow, "show", return_value=True) as show:
            flow.owner_spoke(self.env, self.job.topic_id, "oc_chat")
        show.assert_not_called()
        self.assertEqual(triage.state(json.loads(self.path.read_text())), triage.HELD)


class ClosedTest(Held):
    """The switch goes off with questions held: nobody would handle them, so they are the owner's."""

    def setUp(self):
        super().setUp()
        self.shown = []
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append), topics=mock.Mock())
        self.write()
        self.route()
        patch = mock.patch.object(flow, "show", side_effect=card_out(self.shown, self.state))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_sweep_with_the_switch_off_hands_every_held_question_to_the_owner(self):
        with mock.patch.dict(os.environ, {triage.ENV: "0"}):
            flow.sweep(self.env, time.time())
        record = self.saved()
        self.assertEqual((triage.state(record), record["triage"]["why"], record["display_pending"]), (triage.ESCALATED, "问题分流已关闭", False))
        self.assertEqual(self.shown, [TOKEN])

    def test_with_the_projects_switch_off_too(self):
        with mock.patch.dict(os.environ, {projects.ENV: "0"}):
            flow.settle_closed(self.env)
        self.assertEqual(triage.state(self.saved()), triage.ESCALATED)

    def test_the_start_settles_nothing_while_the_triage_is_on(self):
        flow.settle_closed(self.env)
        self.assertEqual(triage.state(self.saved()), triage.HELD)

    def test_the_bridge_start_runs_the_check_after_the_question_recovery(self):
        import inspect
        from covey.app import unified
        source = inspect.getsource(unified)
        self.assertLess(source.index("recover_question_delivery(ctx,"), source.index("settle_closed(self.butler_env)"))

    def test_an_answer_on_its_way_is_left_alone_when_the_switch_goes_off(self):
        data = self.saved()
        data["triage"]["answering"] = time.time()
        write_json(self.path, data, ensure_ascii=False)
        with mock.patch.dict(os.environ, {triage.ENV: "0"}):
            flow.sweep(self.env, time.time())
        self.assertEqual(triage.state(self.saved()), triage.HELD)


class ForwardedAnswerTest(Held):
    """The lead's answer went to a task process that has not said it took it."""

    def setUp(self):
        super().setUp()
        self.write()
        self.route()
        self.env = SimpleNamespace(state_dir=self.state, runtime=SimpleNamespace(log=self.logs.append), topics=mock.Mock())
        self.app = SimpleNamespace(questions=SimpleNamespace(submit_answer=lambda *args, **kwargs: None))  # Forwarded: pending.
        patch = mock.patch.object(flow, "show", return_value=True)
        self.show = patch.start()
        self.addCleanup(patch.stop)
        self.done = flow.answer(self.env, self.app, self.ctx, CHAT, self.reload(), {"0": "新版"}, "lead-r1")
        self.sent = time.time()

    def test_the_question_is_still_the_leads_and_the_answer_is_not_called_taken(self):
        self.assertEqual(self.done["status"], "pending")
        self.assertIn("新版", self.done["line"])
        found = self.saved()
        self.assertEqual((triage.state(found), bool(found["triage"]["answering"])), (triage.HELD, True))

    def test_it_is_left_alone_while_the_answer_is_on_its_way_whatever_else_is_true(self):
        projects.set_state(self.state, PID, projects.PAUSED, "stalled")
        flow.sweep(self.env, self.sent + triage.ANSWERING - 5)
        self.assertEqual(triage.state(self.saved()), triage.HELD)
        self.show.assert_not_called()

    def test_an_answer_that_never_landed_goes_to_the_owner_with_a_row_in_the_log(self):
        flow.sweep(self.env, self.sent + triage.ANSWERING + 1)
        record = self.saved()
        self.assertEqual((triage.state(record), record["triage"]["why"]), (triage.ESCALATED, "负责人的回答没有送到"))
        self.show.assert_called_once()
        self.assertEqual(self.reload()["triage"][-1]["kind"], "escalated")

    def test_an_answer_that_landed_makes_the_state_answered_by_the_lead(self):
        write_json(self.path, {**self.saved(), "status": "submitting", "answers": {"0": {"answers": ["新版"]}}}, ensure_ascii=False)
        flow.sweep(self.env, self.sent + 1)
        self.assertEqual((triage.state(self.saved()), self.saved()["triage"]["by"]), (triage.ANSWERED, "lead"))
        flow.sweep(self.env, self.sent + 10000)
        self.show.assert_not_called()

    def test_the_lead_is_told_the_answer_is_not_confirmed(self):
        from covey.app import project_tools
        with mock.patch.object(project_tools, "held_topic", return_value=(self.app, self.ctx, CHAT)), \
                mock.patch.object(project_tools.triage_flow, "answer", return_value=self.done), \
                mock.patch.object(project_tools, "lead", return_value=(PID, self.reload())), mock.patch.object(project_tools, "active"), \
                mock.patch.object(project_tools.triage_flow, "grounds", return_value=[]):  # Its grounds hold (W0-2: always checked).
            answer = project_tools.project_answer(self.env, SimpleNamespace(args={"answers": {"0": "新版"}}, id="r9"))
        self.assertTrue(answer["ok"])
        self.assertIn("还没确认", answer["text"])
        self.assertNotIn("已替主人答了", answer["text"])



class EndedTaskQuestionTest(HeldFixture):
    """T488, 2026-11-21: the owner stopped #5528 while its AskUserQuestion was never shown (handed on to the owner, its card not
    sent). Its question stayed pending with display_pending, and every later message of the topic got 原结果或问题控件尚未送达.
    A task's end cancels its questions never shown too, and a live question whose task is gone no longer holds the topic."""

    def setUp(self):
        super().setUp()
        self.record.update(native_live_question=True, display_pending=True, body_delivery_required=False,
                           request_id="req-live", triage={"state": triage.ESCALATED, "project": PID, "by": "lead"})
        self.path.write_text(json.dumps(self.record))
        caps = mock.patch("covey.core.harness.backend_capabilities", return_value=SimpleNamespace(questions=True, steering=True))
        caps.start()
        self.addCleanup(caps.stop)
        self.app.messenger.reply = mock.Mock(return_value=True)

    def status(self):
        return json.loads(self.path.read_text())["status"]

    def test_the_end_of_the_task_cancels_a_question_never_shown(self):
        self.app.questions.dispatch_question_event(self.ctx_topic, self.job, "cancel_questions", {}, None)
        self.assertEqual(self.status(), "cancelled")

    def test_a_held_question_goes_with_its_task_too(self):
        data = json.loads(self.path.read_text())
        data["triage"] = {"state": triage.HELD, "project": PID, "deadline": time.time() + 100}
        self.path.write_text(json.dumps(data))
        self.app.questions.dispatch_question_event(self.ctx_topic, self.job, "cancel_questions", {}, None)
        self.assertEqual(self.status(), "cancelled")

    def test_a_live_question_of_a_task_that_ended_no_longer_refuses_the_owner(self):
        self.app.runtime.jobs.unregister(self.job)
        upsert_job_record(self.state, self.job.message_id, status="cancelled", execution_status="cancelled",
                          question_delivery_ok=False, checkpoint_question_deferred=False)
        self.app.interaction.interactive_command(self.ctx_topic, "继续", "oc_chat", "m-owner")
        self.assertEqual(self.status(), "cancelled")
        self.assertFalse(any("尚未送达" in str(call.args[2]) for call in self.app.messenger.reply.call_args_list))

    def test_while_its_task_still_runs_it_stays_and_the_owner_is_told(self):
        self.assertEqual(self.app.questions.retire_orphaned_live_questions(self.ctx_topic, "oc_chat"), [])
        self.app.interaction.interactive_command(self.ctx_topic, "继续", "oc_chat", "m-owner")
        self.assertEqual(self.status(), "pending")
        self.assertIn("尚未送达", self.app.messenger.reply.call_args.args[2])



class UnshownLiveQuestionTest(HeldFixture):
    """The owner, 2026-11-21 (T620): a live question handed on whose card did not go (the task runner did not acknowledge
    show_question within its 20 seconds) left the turn waiting on a question nobody saw, and the owner's message got 尚未送达
    with nothing to click. The sweep asks for the card again, the owner's message asks once more and then answers it, and the
    task's record is not failed while its turn still runs."""

    def setUp(self):
        super().setUp()
        self.record.update(native_live_question=True, display_pending=True, body_delivery_required=False, request_id="req-live",
                           triage={"state": triage.ESCALATED, "project": PID, "by": "lead", "at": NOW})
        self.path.write_text(json.dumps(self.record))
        upsert_job_record(self.state, self.job.message_id, status="running", execution_status="running", checkpoint_question_deferred=False)
        self.env = SimpleNamespace(state_dir=self.state, runtime=self.app.runtime,
                                   topics=SimpleNamespace(target=lambda state, topic, chat: (self.app, self.ctx_topic, None)))
        mock.patch.object(flow, "ENV", self.env).start()
        mock.patch("covey.core.harness.backend_capabilities", return_value=SimpleNamespace(questions=True, steering=True)).start()
        self.app.questions.submit_answer = mock.Mock()
        self.app.messenger.reply = mock.Mock(return_value=True)
        self.tries = []

    def shows(self, ok):
        def show(env, record):
            self.tries.append(record["token"])
            if ok:
                data = json.loads(self.path.read_text())
                self.path.write_text(json.dumps({**data, "display_pending": False}))
            return ok
        return mock.patch.object(flow, "show", side_effect=show)

    def saved(self):
        return json.loads(self.path.read_text())

    def test_a_card_that_did_not_go_does_not_fail_the_running_task(self):
        record = {**self.saved(), "triage": {"state": triage.HELD, "project": PID, "deadline": NOW}}
        self.path.write_text(json.dumps(record))
        with self.shows(False):
            self.assertFalse(flow.escalate(self.env, record, "要主人定"))
        job = find_job_record(self.state, self.job.message_id)
        self.assertEqual((job["status"], job["question_delivery_ok"]), ("running", False))

    def test_the_sweep_asks_again_every_minute_until_it_goes(self):
        now = time.time()
        data = self.saved()
        data["triage"]["at"] = now
        self.path.write_text(json.dumps(data))
        with self.shows(False):
            flow.sweep(self.env, now + 10)
            self.assertEqual(self.tries, [])  # Not within the minute of the hand-over.
            flow.sweep(self.env, now + flow.RESHOW_EVERY)
            flow.sweep(self.env, now + flow.RESHOW_EVERY + 10)
            self.assertEqual(len(self.tries), 1)
        with self.shows(True):
            flow.sweep(self.env, now + 2 * flow.RESHOW_EVERY)
            flow.sweep(self.env, now + 3 * flow.RESHOW_EVERY)
        self.assertEqual(len(self.tries), 2)
        self.assertEqual(triage.of(self.saved())["reshows"], 2)

    def test_the_sweep_gives_up_after_its_tries(self):
        now = time.time()
        data = self.saved()
        data["triage"]["at"] = now
        self.path.write_text(json.dumps(data))
        with self.shows(False):
            for step in range(1, flow.RESHOW_TRIES + 4):
                flow.sweep(self.env, now + step * flow.RESHOW_EVERY)
        self.assertEqual(len(self.tries), flow.RESHOW_TRIES)

    def test_the_owners_message_shows_it_and_answers_it(self):
        with self.shows(True):
            self.assertTrue(self.app.interaction.interactive_command(self.ctx_topic, "新版", "oc_chat", "m-owner"))
        self.assertEqual(self.tries, [self.token])
        self.assertEqual(self.app.questions.submit_answer.call_args.args[3]["answer_0"], "新版")
        self.app.messenger.reply.assert_not_called()

    def test_a_card_that_still_does_not_go_says_so_without_a_retry_button(self):
        with self.shows(False):
            self.assertTrue(self.app.interaction.interactive_command(self.ctx_topic, "新版", "oc_chat", "m-owner"))
        said = self.app.messenger.reply.call_args.args[2]
        self.assertIn("问题卡没发出来", said)
        self.assertNotIn("重新投递", said)
        self.assertEqual(self.saved()["status"], "pending")
        self.app.questions.submit_answer.assert_not_called()


class StrandedCheckpointQuestionTest(HeldFixture):
    """T752, 2026-11-23: the lead handed a member's checkpoint question to the owner and the tool said ok, but the card did not go
    (nothing logged) and nothing asked again: the question stayed pending with the task marked failed until a click on 重试. The
    hand-over logs a card that did not go, the sweep sends it again (once a minute, five tries), a card that goes out puts the task
    back to waiting for the answer, and the task's 重试 is what is left when the tries are used up."""

    def setUp(self):
        super().setUp()
        record = {**json.loads(self.path.read_text()),
                  "triage": {"state": triage.HELD, "project": PID, "at": NOW, "deadline": NOW + 180, "receipt": True}}
        self.path.write_text(json.dumps(record))
        self.env = SimpleNamespace(state_dir=self.state, runtime=self.app.runtime,
                                   topics=SimpleNamespace(target=lambda state, topic, chat: (self.app, self.ctx_topic, None)))
        upsert_job_record(self.state, self.job.message_id, final_text="结果正文")  # The saved reply 重新投递 would send.
        self.logs = []
        self.app.runtime.log = self.logs.append
        self.app.questions.publish_question_card.return_value = False  # Feishu would not take the card.
        self.record = record

    def saved(self):
        return json.loads(self.path.read_text())

    def job_now(self):
        return find_job_record(self.state, self.job.message_id)

    def escalate(self):
        self.assertFalse(flow.escalate(self.env, self.record, "要主人定"))

    def sweep(self, now):
        flow.sweep(self.env, now)

    def test_a_card_that_did_not_go_is_logged_and_the_task_says_it_was_not_delivered(self):
        self.escalate()
        self.assertTrue(any("escalation card not sent token=" + self.record["token"][:8] in line for line in self.logs))
        self.assertTrue(any("card did not go out" in line for line in self.logs))
        self.assertTrue(flow.stranded(self.saved()))
        self.assertEqual((self.job_now()["status"], self.job_now()["question_delivery_ok"]), ("failed", False))

    def test_the_reason_is_the_result_that_is_not_delivered_when_it_is_not(self):
        self.app.questions.checkpoints.result_delivered = lambda ctx, record: False
        self.escalate()
        self.assertTrue(any("result is not delivered yet" in line for line in self.logs))

    def test_the_sweep_sends_it_again_after_a_minute_and_mends_the_task(self):
        self.escalate()
        at = triage.of(self.saved())["at"]
        self.sweep(at + 10)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 1)  # Only the hand-over's own: not within the minute.
        self.app.questions.publish_question_card.return_value = True
        self.sweep(at + flow.RESHOW_EVERY)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 2)
        saved = self.saved()
        self.assertEqual((saved["display_pending"], triage.of(saved)["reshows"]), (False, 1))
        self.assertEqual((self.job_now()["status"], self.job_now()["question_delivery_ok"]), ("waiting_user", True))
        self.assertTrue(any("card again token=" + self.record["token"][:8] + " try=1 ok=True" in line for line in self.logs))
        self.sweep(at + 3 * flow.RESHOW_EVERY)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 2)  # Nothing more once it is out.

    def test_it_gives_up_after_its_tries_and_the_task_stays_for_its_retry_button(self):
        self.escalate()
        at = triage.of(self.saved())["at"]
        for step in range(1, flow.RESHOW_TRIES + 4):
            self.sweep(at + step * flow.RESHOW_EVERY)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 1 + flow.RESHOW_TRIES)
        self.assertEqual(len([line for line in self.logs if "card gave up" in line]), 1)
        self.assertEqual((self.job_now()["status"], self.job_now()["question_delivery_ok"]), ("failed", False))

    def test_with_the_switch_off_the_sweep_does_not_send_it_again(self):
        self.escalate()
        os.environ[triage.ENV] = "0"
        self.app.questions.publish_question_card.return_value = True
        self.sweep(triage.of(self.saved())["at"] + 10 * flow.RESHOW_EVERY)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 1)

    def test_a_question_that_is_not_stranded_is_left_alone(self):
        self.escalate()
        data = {**self.saved(), "status": "cancelled"}
        self.path.write_text(json.dumps(data))
        self.app.questions.publish_question_card.return_value = True
        self.sweep(triage.of(data)["at"] + 10 * flow.RESHOW_EVERY)
        self.assertEqual(self.app.questions.publish_question_card.call_count, 1)


if __name__ == "__main__":
    unittest.main()

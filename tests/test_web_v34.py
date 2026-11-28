"""Data contract v34 of the web page (docs/web.md): 收尾 and 停止 of a project (``project_finish``, ``project_stop``) as its cards do,
不对 on a memory line (``memory_undo``) as its answer card does, a turn's 完成标准 and 改动 (``criteria``, ``delivery``), a topic's
到点回来看 and 代发 drafts (``revisit``, ``owner_send``), and 要你处理 as every card counts it (the ``asks`` settings item)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from covey.app import memory_collect, project_lead, project_loop, project_tools, runner_ipc, web_collect, web_turns  # noqa: E402
from covey.core import criteria, projects, revisits, web_sync  # noqa: E402
from covey.core.jobs import find_job_record, upsert_job_record  # noqa: E402
from test_project_lead import LEAD, PID  # noqa: E402
from test_web_sync import CHAT, TOPIC, Ctx, WebCase  # noqa: E402


class V34Case(WebCase):
    def setUp(self):
        super().setUp()
        self.runtime.access.is_owner = lambda open_id: open_id == "ou_owner"
        self.open = {}  # topic -> its open jobs (the project's lead and members)
        self.app.controls.runtime = SimpleNamespace(jobs=SimpleNamespace(open_in_topic=lambda topic, chat: self.open.get(topic, [])),
                                                    owner_open_id="ou_owner", log=self.logs.append)
        self.app.controls.messenger = object()
        self.env.topics.apps = {"claude": self.app}
        patch = mock.patch.object(runner_ipc, "forward_card_action", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def target(self, state, topic, chat):
        """As app/topics.py Topics.target: no lead (its Traits row is no card target); the page reaches it as app/web_ops.py lead_topic does."""
        if topic != TOPIC or chat != CHAT:
            return None
        return self.app, Ctx(topic, state), {}

    def run_op(self, kind, op_id, topic=None, project=None, **payload):
        self.web.op(kind, len(self.web.outbox) + 1, op_id, topic, project=project, **payload)
        self.sync.inbox_once()
        return self.web.acks[-1]


class ProjectStopTest(V34Case):
    def test_stop_pauses_an_active_project_as_the_owner_and_stops_its_tasks(self):
        self.open[LEAD] = [SimpleNamespace(job_id=4, message_id="om_l4")]
        ack = self.run_op("project_stop", "op-s1", project=PID)
        self.assertEqual(ack["state"], "done", ack)
        self.assertIn("已停住专项「迁移账单」，已请 1 个任务停下", ack["message"])
        record = self.reload()
        self.assertEqual((record["state"], record["state_reason"]), (projects.PAUSED, projects.OWNER_STOP))
        self.assertEqual([(a["job_id"], a["by"]) for a in self.app.stopped], [("4", "web")])  # The owner's own stop: 已停止.

    def test_stop_answers_the_pause_put_to_the_owner(self):
        projects.update(self.state, PID, lambda r: r.update(state=projects.PAUSED, state_reason="tokens", tokens_used=900,
                                                            pause_ask={"reason": "tokens", "why": "token 用完了", "card": {"message_id": "om_pa"}}))
        ack = self.run_op("project_stop", "op-s2", project=PID)
        self.assertEqual(ack["state"], "done", ack)
        record = self.reload()
        self.assertEqual((record["pause_ask"]["choice"], record["state_reason"], record["token_floor"]), ("stop", projects.OWNER_STOP, 900))

    def test_stop_refuses_what_is_not_on_and_what_is_not_the_owners(self):
        projects.update(self.state, PID, lambda r: r.update(state=projects.DONE))
        self.assertIn("已经是做完的", self.run_op("project_stop", "op-s3", project=PID)["message"])
        self.assertEqual(self.run_op("project_stop", "op-s4", project="nosuchproject1")["state"], "failed")
        projects.update(self.state, PID, lambda r: r.update(state=projects.ACTIVE))
        self.runtime.access.is_owner = lambda open_id: False
        ack = self.run_op("project_stop", "op-s5", project=PID)
        self.assertEqual((ack["state"], ack["message"]), ("failed", "只有主人能停止专项"))
        self.assertEqual(self.reload()["state"], projects.ACTIVE)


class ProjectFinishTest(V34Case):
    def setUp(self):
        super().setUp()
        self.receipts = []
        patch = mock.patch.object(project_lead, "add_receipt", lambda state, pid, kind, text: self.receipts.append((pid, kind, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_finish_of_a_project_that_is_on_leaves_its_lead_the_owners_choice(self):
        ack = self.run_op("project_finish", "op-f1", project=PID)
        self.assertEqual(ack["state"], "done", ack)
        self.assertEqual([(pid, kind) for pid, kind, _ in self.receipts], [(PID, "owner_choice")])
        self.assertIn("主人在网页上选了收尾", self.receipts[0][2])
        self.assertTrue(self.reload().get("owner_at"))  # His word: 整理专项's owner-first gate is passed.

    def test_finish_answers_the_pause_put_to_the_owner(self):
        projects.update(self.state, PID, lambda r: r.update(state=projects.PAUSED, state_reason="stalled",
                                                            pause_ask={"reason": "stalled", "why": "很久没有动静", "card": {"message_id": "om_pa"}}))
        ack = self.run_op("project_finish", "op-f2", project=PID)
        self.assertEqual(ack["state"], "done", ack)
        record = self.reload()
        self.assertEqual((record["pause_ask"]["choice"], record["state"]), ("finish", projects.ACTIVE))
        self.assertIn("选了收尾", self.receipts[0][2])
        self.assertIn("很久没有动静", self.receipts[0][2])

    def test_finish_is_refused_with_the_leads_own_reasons(self):
        with mock.patch.object(project_lead, "live_members", return_value=["T7"]):
            ack = self.run_op("project_finish", "op-f3", project=PID)
        self.assertEqual((ack["state"], ack["message"]), ("failed", "还有话题在做或等你（T7），不能收尾：等它们结束，或先处理掉"))
        unmet = "目标检查表还有没满足的：c1「对账一致」没开始。" + project_tools.FINISH_NEXT
        with mock.patch.object(project_tools, "checklist_gate", return_value=(unmet, ["c1"], [])):
            ack = self.run_op("project_finish", "op-f4", project=PID)
        self.assertEqual(ack["state"], "failed")
        self.assertIn("目标检查表还有没满足的：c1「对账一致」没开始。", ack["message"])
        self.assertNotIn("project_topic_send", ack["message"])  # The lead's steps are its own, not the owner's.
        self.assertEqual(self.receipts, [])
        projects.update(self.state, PID, lambda r: r.update(state=projects.CANCELLED))
        self.assertIn("已经取消了", self.run_op("project_finish", "op-f5", project=PID)["message"])


class MemoryUndoTest(V34Case):
    def setUp(self):
        super().setUp()
        self.topic()
        record = find_job_record(self.state, "k-cccc")
        upsert_job_record(self.state, record["key"], memory_notes=[
            {"kind": "remembered", "entry_id": "e1", "text": "以后报表一律按周出", "scope": "管这个话题"}])
        self.forgot, self.drawn = [], []
        for name, fake in (("_forget", lambda *args: self.forgot.append(args)),
                           ("show", lambda ctx, messenger, record, notes, log: self.drawn.append([n["kind"] for n in notes]))):
            patch = mock.patch.object(memory_collect, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def test_undo_is_the_answer_cards_and_a_second_one_says_so(self):
        ack = self.run_op("memory_undo", "op-u1", TOPIC, job_id="7", entry_id="e1")
        self.assertEqual((ack["state"], ack["message"]), ("done", "已撤销：以后报表一律按周出"))
        self.assertEqual(self.forgot, [(self.state, TOPIC, "e1", "管这个话题")])
        self.assertEqual(self.drawn, [["undone"]])
        self.assertEqual(find_job_record(self.state, "k-cccc")["memory_notes"][0]["kind"], "undone")
        ack = self.run_op("memory_undo", "op-u2", TOPIC, job_id="7", entry_id="e1")
        self.assertEqual((ack["state"], ack["message"]), ("done", "这条记忆已经撤销过了"))
        self.assertEqual(len(self.forgot), 1)

    def test_undo_refuses_what_the_turn_did_not_write_and_anyone_but_the_owner(self):
        self.assertEqual(self.run_op("memory_undo", "op-u3", TOPIC, job_id="7", entry_id="nope")["message"], "这一轮没有记下这条记忆")
        self.assertEqual(self.run_op("memory_undo", "op-u4", TOPIC, job_id="99", entry_id="e1")["message"], "这个话题里没有任务 #99")
        self.runtime.access.is_owner = lambda open_id: False
        self.assertEqual(self.run_op("memory_undo", "op-u5", TOPIC, job_id="7", entry_id="e1")["message"], "只有主人能撤销记忆")
        self.assertEqual(self.forgot, [])


class CheckedTest(WebCase):
    def checkpoint(self, data, diff=False):
        path = self.state / "cp.json"
        path.write_text(json.dumps(data, ensure_ascii=False))
        record = {"topic_id": TOPIC, "status": "done", "checkpoint_path": str(path)}
        if diff:
            (self.state / "diff.json").write_text("{}")
            record["diff_json_path"] = str(self.state / "diff.json")
        return record

    def test_criteria_follow_the_topics_list_and_delivery_shows_as_the_card_does(self):
        self.topic()
        with mock.patch.object(criteria, "read", return_value=["测试全过", "文档改了", "截图验收"]):
            out = web_turns.checked(self.state, self.checkpoint({
                "criteria": [{"item": "测试全过", "state": "满足", "evidence": "165 条"}, {"item": "文档改了", "state": "没满足"}],
                "delivery": {"commit": "d060d7da0d74703e5f59a7d9eaf3eb1b0373722f", "verified": "lint 通过"}}))
        self.assertEqual(out["criteria"], [{"item": "测试全过", "state": "满足", "evidence": "165 条"},
                                           {"item": "文档改了", "state": "没满足"}, {"item": "截图验收", "state": "没查"}])
        self.assertEqual(out["delivery"]["commit"], "d060d7da0d74703e5f59a7d9eaf3eb1b0373722f")

    def test_a_research_turn_shows_no_delivery_and_no_checkpoint_says_nothing(self):
        with mock.patch.object(criteria, "read", return_value=[]):
            out = web_turns.checked(self.state, self.checkpoint({"delivery": {"verified": "读了三份报告"}}))
            self.assertEqual(out, {})  # No commit and no files changed: the card shows no 改动 either.
            out = web_turns.checked(self.state, self.checkpoint({"delivery": {"deployed": "第 9 代"}}, diff=True))
            self.assertEqual(out, {"delivery": {"deployed": "第 9 代"}})
            self.assertEqual(web_turns.checked(self.state, {"topic_id": TOPIC}), {})


class LaterAndDraftsTest(WebCase):
    def test_a_due_revisit_and_the_drafts_that_wait_go_with_the_topic_when_their_switches_are_on(self):
        pending = {"state": revisits.PENDING, "due_at": 1800000000.4, "set_at": 1799999000.25, "look": "CI 跑完没有\n再看一眼"}
        drafts = [{"id": "d1", "status": "drafted", "to_label": "张三", "text": "周报已更新，\n请看", "created_at": 1700000000.6},
                  {"id": "d2", "status": "sent", "to": "李四", "text": "旧的"}, {"id": "d3", "status": "unsent", "to": "王五", "text": "重发"}]
        with mock.patch.object(revisits, "enabled", return_value=True), mock.patch.object(revisits, "read", return_value=pending), \
                mock.patch.object(web_collect.revisit_rules, "ordinary", return_value=True), \
                mock.patch.object(web_collect.owner_send, "enabled", return_value=True), \
                mock.patch.object(web_collect.owner_send_records, "topic_records", return_value=drafts):
            out = web_collect.later_and_drafts(self.state, TOPIC)
        self.assertEqual(out["revisit"], {"at": 1800000000, "what": "CI 跑完没有 再看一眼", "set_at": 1799999000.25, "running": False})
        self.assertEqual([(d["id"], d["to"], d["state"], d["preview"]) for d in out["owner_send"]],
                         [("d1", "张三", "waiting", "周报已更新， 请看"), ("d3", "王五", "failed", "重发")])
        self.assertEqual(out["owner_send"][0]["created_at"], 1700000001)

    def test_off_or_nothing_pending_adds_nothing(self):
        with mock.patch.object(revisits, "enabled", return_value=False), mock.patch.object(web_collect.owner_send, "enabled", return_value=False):
            self.assertEqual(web_collect.later_and_drafts(self.state, TOPIC), {})
        with mock.patch.object(revisits, "enabled", return_value=True), \
                mock.patch.object(revisits, "read", return_value={"state": "gone", "due_at": 1.0}), \
                mock.patch.object(web_collect.revisit_rules, "ordinary", return_value=True), \
                mock.patch.object(web_collect.owner_send, "enabled", return_value=True), \
                mock.patch.object(web_collect.owner_send_records, "topic_records", return_value=[{"id": "d2", "status": "sent"}]):
            self.assertEqual(web_collect.later_and_drafts(self.state, TOPIC), {})

    def test_a_fired_revisit_is_running_and_a_project_topics_left_over_one_is_not_shown(self):
        fired = {"state": revisits.FIRED, "due_at": 1800000000.0, "set_at": 1799999000.0, "look": "看 CI"}
        with mock.patch.object(revisits, "enabled", return_value=True), mock.patch.object(revisits, "read", return_value=fired), \
                mock.patch.object(web_collect.owner_send, "enabled", return_value=False):
            with mock.patch.object(web_collect.revisit_rules, "ordinary", return_value=True):
                out = web_collect.later_and_drafts(self.state, TOPIC)
            self.assertEqual(out["revisit"], {"at": 1800000000, "what": "看 CI", "set_at": 1799999000.0, "running": True})
            with mock.patch.object(web_collect.revisit_rules, "ordinary", return_value=False):  # A project's topic now.
                self.assertEqual(web_collect.later_and_drafts(self.state, TOPIC), {})


class RevisitCancelTest(V34Case):
    """取消 on the page's 定时回看 (data contract v35, ``revisit_cancel``): the 看板's 取消 with the same ``set_at`` guard."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict("os.environ", {revisits.SWITCH: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.set_at = 1800000000.5

    def arm(self, **extra):
        revisits.change(self.state, TOPIC, lambda data: {**revisits.armed(TOPIC, CHAT, {"minutes": 30, "look": "看 CI"}, by="job", round_=1,
                                                                        now=self.set_at), **extra})

    def test_the_kind_is_one_the_page_may_send(self):
        self.assertIn("revisit_cancel", web_sync.KINDS)  # The settings item's ``ops`` is this list (app/web_settings.py).

    def test_the_owner_cancels_the_revisit_and_the_topic_is_looked_at_again_at_once(self):
        self.arm()
        touched = []
        self.sync.collector.touch = touched.append
        ack = self.run_op("revisit_cancel", "op-r1", topic=TOPIC, set_at=self.set_at)
        self.assertEqual((ack["state"], ack["message"]), ("done", "已取消定时回看"), ack)
        self.assertEqual(revisits.read(self.state, TOPIC), {})
        self.assertIn(TOPIC, touched)
        self.assertTrue(any("revisit cancelled from the page" in line for line in self.logs))

    def test_one_gone_says_so_and_one_replaced_stays_and_says_so(self):
        ack = self.run_op("revisit_cancel", "op-r2", topic=TOPIC, set_at=self.set_at)
        self.assertEqual(ack["state"], "done", ack)
        self.assertIn("已经没有了", ack["message"])
        self.arm()
        ack = self.run_op("revisit_cancel", "op-r3", topic=TOPIC, set_at=self.set_at - 100)  # The page's row was an older one.
        self.assertEqual(ack["state"], "failed", ack)
        self.assertIn("换成了新的", ack["message"])
        self.assertEqual(revisits.read(self.state, TOPIC)["set_at"], self.set_at)

    def test_one_covey_is_looking_at_now_stays(self):
        self.arm(state=revisits.FIRED)
        ack = self.run_op("revisit_cancel", "op-r4", topic=TOPIC, set_at=self.set_at)
        self.assertEqual(ack["state"], "failed", ack)
        self.assertIn("已经在回看了", ack["message"])
        self.assertEqual(revisits.read(self.state, TOPIC)["state"], revisits.FIRED)

    def test_only_the_owner_on_a_topic_of_his_chat_with_a_time_cancels(self):
        self.arm()
        self.runtime.access.is_owner = lambda open_id: False
        self.assertEqual(self.run_op("revisit_cancel", "op-r5", topic=TOPIC, set_at=self.set_at)["state"], "failed")
        self.runtime.access.is_owner = lambda open_id: open_id == "ou_owner"
        self.assertEqual(self.run_op("revisit_cancel", "op-r6", topic="f" * 32, set_at=self.set_at)["state"], "failed")
        self.assertEqual(self.run_op("revisit_cancel", "op-r7", topic=TOPIC)["state"], "failed")
        self.assertEqual(self.run_op("revisit_cancel", "op-r8", topic=TOPIC, set_at="x")["state"], "failed")
        self.assertEqual(revisits.read(self.state, TOPIC)["set_at"], self.set_at)


class AsksPushTest(WebCase):
    def test_asks_go_on_their_own_when_they_change_and_a_refusal_is_logged_once(self):
        collector = self.sync.collector
        self.assertEqual(self.sync.push_asks(collector), 0)  # Not read yet.
        collector.asks = {"waiting": 2, "stopped": 1, "stuck": 0}
        self.assertEqual(self.sync.push_asks(collector), 1)
        item = next(each for each in self.web.items("settings") if each["key"] == web_sync.ASKS_KEY)
        self.assertEqual(item["data"], {"waiting": 2, "stopped": 1, "stuck": 0})
        self.assertEqual(self.sync.push_asks(collector), 0)  # Unchanged: not again.
        collector.asks = {"waiting": 1, "stopped": 1, "stuck": 0}
        with mock.patch.object(self.sync.client, "request", side_effect=RuntimeError("400 settings key")):
            self.assertEqual(self.sync.push_asks(collector), 0)
            self.assertEqual(self.sync.push_asks(collector), 0)  # The same counts wait for a full look.
        self.assertEqual(sum("web asks not pushed" in line for line in self.logs), 1)
        self.assertEqual(self.sync.push_asks(collector, full=True), 1)

    def test_the_collector_counts_as_the_board_does(self):
        looked = SimpleNamespace(dismissed={"t-off": {}}, asks=lambda state, chat, found, now, off, proposals: SimpleNamespace(
            counts=lambda: {"waiting": len(found.all) + (1 if proposals else 0), "stopped": len(off), "stuck": 0}))
        with mock.patch.object(web_collect.board_read, "waits", return_value=SimpleNamespace(all=[1, 2])):
            self.assertEqual(web_collect.Collector.asked(self.state, CHAT, 0, looked).counts(), {"waiting": 3, "stopped": 1, "stuck": 0})
        self.assertIsNone(web_collect.Collector.asked(self.state, CHAT, 0, SimpleNamespace()))


class KindsTest(V34Case):
    def test_the_new_operations_are_known(self):
        self.assertTrue({"project_finish", "project_stop", "memory_undo"} <= set(web_sync.KINDS))
        self.assertIs(project_loop.CHOICE_RECEIPT is not None, True)

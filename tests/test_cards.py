"""Cards as CardKit sees them, and card clicks as the Bridge takes them.

- Element IDs are CardKit-valid and unique on every card; a rejected task card falls back to
  one with only the stop control, and failures log what Feishu said.
- Card sequences only go up: across retries, task-runner handoffs, restarts and recovery cards.
- Publishing from a source card updates it in place; retired commands are refused.
- The action table: job-starting kinds stay with the bot, a failed click names its action.
- Clicks run in per-topic lanes and per-controller queues.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.cards import (TOPIC, remember_browse_card, sample_job, split_panel,
                           stream_runtime, trace_panel)
from covey.core import extensions
from support.clock import FakeClock
from support.fake_lark import FakeCardKit
from support.world import TopicFixture, UnifiedCase

from covey.app import card_lanes, runner_ipc, task_card
from covey.app import cards as cards_module
from covey.core.trace import ToolTrace
from covey.feishu import events as feishu_events
from covey.app.cards import Cards
from covey.app.runtime import Runtime
from covey.app.unified import UnifiedBridgeApp
from covey.cards.artifact_panels import artifact_panel
from covey.app.card_actions import BOT_ONLY, action_label
from covey.app.card_lanes import CardLanes
from covey.app.card_queue import CardActionQueue
from covey.app.runner_spawn import dump_job, load_job
from covey.cards.topic_actions import topic_panel
from covey.app.switch_handoff import switch_panel
from covey.cards.switch_form import build_switch_card
from covey.cards.rewind import done_card as rewind_done_card, rewind_card
from covey.cards.elements import notice_card
from covey.feishu.events import ConsumerHealth
from covey.feishu.messenger import Messenger
from covey.cards.held_note import note_card
from covey.app.launch import build_launch_card
from covey.app.task_card import StreamingReply, bind_record
from covey.app.recovery_cards import recovery_card
from covey.cards.directory import directory_card
from covey.cards.workspace_manager import manager_card
from covey.cards.launch_receipt import build_preparing_card, build_receipt_card
from covey.cards.proxy_panel import expired_card, proxy_panel_card
from covey.cards.proxy_resend import resend_card
from covey.cards.question_cards import question_card, question_element, question_feedback_card
from covey.cards.steer_receipts import receipt_card
from covey.cards import card_cost, streaming
from covey.cards.streaming import build_stream_card, card_details_text, card_title_for_question
from covey.core.card_sequence import (
    CardHandle,
    LEASE,
    adopt,
    file_card,
    job_card,
    raise_card_sequence,
    reserve,
)
from covey.core.jobs import BridgeReplyJob, JOB_STOP_ACTION_KIND, find_job_record, upsert_job_record
from covey.core.topics import remember_topic_message
from covey.feishu import transport
from covey.feishu.transport import Failure, classify, operation, set_failure


ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")


TOKEN = "0123456789abcdef"


def element_ids(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        if "element_id" in node:
            found.append(node["element_id"])
        for value in node.values():
            element_ids(value, found)
    elif isinstance(node, list):
        for value in node:
            element_ids(value, found)
    return found


def question_record(status="pending", **fields):
    record = {"token": TOKEN, "status": status, "topic_id": TOPIC, "chat_id": "chat", "job_id": 431, "blocking": True,
              "harness_id": "codex", "binding_revision": 2, "created_at": time.time(),
              "questions": [{"id": "fmt", "header": "格式", "question": "用哪种格式输出？",
                             "options": [{"label": "表格", "description": "Markdown 表格"},
                                         {"label": "列表", "description": "无序列表"}]},
                            {"id": "note", "header": "补充", "question": "还有别的要求吗？"}],
              "answers": {"fmt": {"answers": ["表格"]}, "note": {"answers": ["无"]}}}
    record.update(fields)
    return record


def sample_cards(work=None):
    """One card (or panel) of every kind the bridge sends or puts on a card, by name."""

    work = work or Path(tempfile.gettempdir())
    run = stream_runtime()
    # A record from before the Harness, provider and workspace selects still draws.
    old_switch_form = {"token": "b" * 24, "topic": "T70", "workspace": "project",
                       "current": {"label": "Codex · GPT-6-Astra", "effort": "xhigh"},
                       "selected_model": "codex:gpt-6-astra", "selected_effort": "xhigh",
                       "config_revision": 2, "models": {
                           "codex:gpt-6-astra": {"label": "Codex · GPT-6-Astra",
                                                 "levels": ["low", "high", "xhigh", "max"]},
                           "dsh:ali-deepseek-v4.1-flash": {"label": "DeepSeek Harness · flash",
                                                           "levels": ["low", "high", "max"]}}}
    route = {"codex:gpt-6-astra": {"harness": "codex", "title": "Codex", "provider": "openai",
                                   "provider_title": "Codex 自带", "display_name": "GPT-6-Astra"},
             "claude:opus": {"harness": "claude", "title": "Claude", "provider": "anthropic",
                             "provider_title": "Claude 自带", "display_name": "Opus 5.5",
                             "label": "Claude · Opus 5.5", "levels": ["high", "xhigh", "max"]},
             "dsh:ali-deepseek-v4.1-flash": {"harness": "dsh", "title": "DeepSeek Harness", "provider": "acme",
                                             "provider_title": "Acme", "display_name": "DeepSeek V4.1 Flash"}}
    switch_form = {**old_switch_form, "models": {key: {**old_switch_form["models"].get(key, {}), **entry}
                                                 for key, entry in route.items()},
                   "current": {"label": "Codex · GPT-6-Astra", "effort": "xhigh", "key": "codex:gpt-6-astra",
                               "budget": "xhigh", "workspace": "project", "run_mode": "worktree"},
                   "workspaces": ["project", "DataLab", "demo-app"], "selected_workspace": "project",
                   "selected_run_mode": "worktree"}
    rewind_form = {"token": "c" * 24, "topic": "T70", "job_id": 12, "user_text": "把 a.py 里的重试改成指数退避",
                   "workdir": str(work), "point": {"keep": "k", "checkpoint": True, "file_count": 2, "files": [
                       (str(work / "a.py"), "restore"), (str(work / "notes.md"), "delete")]}}
    from covey.cards import dispatch_card as dc
    from covey.cards.dispatch_card import Dispatch, dispatch_card, settled_card
    working = Dispatch(token="d" * 16, topic_id="topic-fake", request="提交到 GitHub", status=dc.RUNNING, label="T453",
                       title="比对主干", workspace="covey", started="12:41", took="2分00秒", body="第 2/4 步", job_id="7",
                       url="https://applink.feishu.cn/client/thread/open?open_chat_id=oc_x",
                       inputs=("Claude Code", "Sonnet"), outputs=("**开始** 12:41",))
    cards = {
        "dispatch_running": dispatch_card(working),
        "dispatch_line": settled_card(dc.DONE, "提交到 GitHub", url="https://applink.feishu.cn/client/chat/open?openChatId=oc_x"),
        "task_running": build_stream_card("正在处理", "任务", run, job=sample_job(), include_stop_action=True),
        "task_running_essential": build_stream_card("正在处理", "任务", run, job=sample_job(), include_stop_action=True,
                                                    essential=True),
        "task_plain": build_stream_card("正在处理", "任务", run, job=sample_job()),
        "switch_card": build_switch_card(switch_form),
        "switch_card_old": build_switch_card(old_switch_form),
        "switch_card_refused": build_switch_card({**switch_form,
                                                  "notice": "**⚠️ 还没有换（20:08）**：本话题还有任务在排队或执行"}),
        "switch_closed": notice_card("**🔁 已换配置**", "已换配置"),
        "rewind_card": rewind_card(rewind_form),
        "rewind_card_no_files": rewind_card({**rewind_form, "point": {"keep": "k", "checkpoint": False, "files": [],
                                                                        "file_count": 0}}),
        "rewind_done": rewind_done_card({**rewind_form, "done": {"files": 0, "keep": "k"}}),
        "rewind_done_files": rewind_done_card({**rewind_form, "done": {"files": 2, "keep": "k"}}),
        "launch_receipt": build_receipt_card({"model_label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                              "workdir": str(work), "topic": "T70", "run_mode": "worktree",
                                              "branch": "bridge/t70"}),
        "launch_preparing": build_preparing_card({"label": "Codex · GPT-6-Astra", "effort": "xhigh",
                                                  "workspace": "project", "topic": "T70"}),
        "question_card": question_card(question_record()),
        "question_card_cancelled": question_card(question_record("cancelled", cancel_reason="switched")),
        "question_card_answered": question_card(question_record("answered", delivery_state="running")),
        "question_card_folded": question_card(question_record("superseded", superseded_reason="newer_question",
                                                              superseded_by_job=432, refusal="这组问题已收起")),
        "question_feedback": question_feedback_card(question_record()),
        "steer_receipt": receipt_card({"status": "sent", "job_id": 431, "text": "再补充一点\n第二行", "saved_at": 1.0,
                                       "sent_at": 2.0, "topic_id": TOPIC, "token": "c" * 64}, source="来源：#432"),
        "held_note": note_card({"status": "held", "job_id": 431, "text": "这条先暂存", "topic_id": TOPIC,
                                "token": "d" * 24}),
        "held_note_moved": note_card({"status": "held", "text": "这条先暂存", "topic_id": TOPIC, "token": "d" * 24,
                                      "waiting": [{"job_id": 432, "created_at": 2.0, "count": 2}],
                                      "folded": [{"job_id": 431, "created_at": 1.0, "count": 1}]}),
        "recovery": recovery_card({"job_id": 431, "topic_id": TOPIC, "status": "interrupted", "message_id": "om_task",
                                   "harness_id": "codex", "binding_revision": 2, "user_text": "继续"}),
        "directory": directory_card("e" * 24, {"path": str(work), "revision": 1,
                                               "entries": [{"name": f"child-{i}", "link": False} for i in range(30)]}),
        **{f"workspace_manager{'' if standalone else '_launch'}": manager_card(
            "a" * 24, {"revision": 3, "notice": "✓ 已从列表移除 1 项，磁盘目录未删除", "details": "**old** · /x/old"},
            {"default": "w0", "workspaces": {f"w{i}": f"/x/w{i}" for i in range(14)}, "recent": {"w1": time.time()}},
            {"/x/w2": False, "/x/w3": None}, standalone=standalone) for standalone in (True, False)},
        "proxy_panel": proxy_panel_card(
            "f" * 24, {"revision": 2, "notice": "✓ 已加入白名单", "query": "张三", "found": {"query": "张三", "candidates": [
                {"key": "p2p:ou_z", "kind": "p2p", "target": "ou_z", "name": "张三", "detail": "数据", "email": "z@x"}]}},
            settings={"enabled": True, "group_chat_id": "oc_p", "harness": "codex", "model": "gpt-6-astra",
                      "effort": "xhigh", "user_profile": "me", "web_python": "/py", "workspace": "project"},
            contacts=[{"key": "p2p:ou_z", "kind": "p2p", "target": "ou_z", "name": "张三", "paused": False},
                      {"key": "group:oc_g", "kind": "group", "target": "oc_g", "name": "项目群", "paused": True}],
            batches=[{"contact": "p2p:ou_z"}], choices=switch_form["models"], current=("codex:gpt-6-astra", "xhigh")),
        "proxy_panel_lookup": proxy_panel_card(
            "f" * 24, {"revision": 3, "query": "张三、王五 Covey", "page": 1, "lookup": {"id": "abcd1234", "text": "张三、王五 Covey", "left": 0, "items": [
                {"query": "张三", "state": "unique", "candidates": [
                    {"key": "p2p:ou_z", "kind": "p2p", "target": "ou_z", "name": "张三", "detail": "数据", "email": ""}]},
                {"query": "Covey", "state": "unique", "candidates": [
                    {"key": "group:oc_c", "kind": "group", "target": "oc_c", "name": "Covey", "detail": "群", "email": "",
                     "members": 12}]},
                {"query": "王五", "state": "choices", "more": 0, "candidates": [
                    {"key": f"p2p:ou_w{i}", "kind": "p2p", "target": f"ou_w{i}", "name": "王五", "detail": "部门", "email": ""}
                    for i in range(2)]},
                {"query": "没这个人", "state": "none", "candidates": []}]},
             "members": {"id": "beef0001", "chat_id": "oc_c", "name": "Covey", "total": 12, "bots": 1, "skipped": 2,
                         "add": [{"open_id": f"ou_m{i}", "name": f"成员{i}"} for i in range(10)], "partial": False}},
            settings={"enabled": True, "group_chat_id": "oc_p", "harness": "codex", "model": "gpt-6-astra",
                      "effort": "xhigh", "user_profile": "me", "web_python": "/py", "workspace": "project"},
            contacts=[{"key": f"p2p:ou_{i}", "kind": "p2p", "target": f"ou_{i}", "name": f"联系人{i}", "paused": False}
                      for i in range(12)] + [
                {"key": f"p2p:ou_s{i}", "kind": "p2p", "target": f"ou_s{i}", "name": f"成员{i}", "paused": i == 0,
                 "source": {"chat_id": "oc_s", "name": "项目群"}} for i in range(5)],
            batches=[], choices=switch_form["models"], current=("codex:gpt-6-astra", "xhigh"), busy="⏳ 正在查找…"),
        "proxy_panel_expired": expired_card(),
        **{f"proxy_resend_{state}": resend_card({"state": state, "notice": "⚠️ 没有重新发送：代答已关闭。"}, {
            "id": "0" * 32, "reply": "收到 哥\n**这两条** 挺到位的", "reason": "网页端未登录", "messages": []}, "赵六")
           for state in ("failed", "again", "resending", "sent", "uncertain", "void", "dropped")},
    }
    from covey.app.welcome import EXAMPLES, TOPIC_EXAMPLES
    from covey.cards.welcome import NO_CLAUDE, SENT, welcome_card
    welcome = {"host": "devbox", "harnesses": ["Codex"], "down": [["Codex", "没登录"]], "workspace": "work",
               "workdir": str(work), "butler": "", "examples": list(EXAMPLES), "tried": {"1": {"state": SENT}}}
    cards.update(welcome=welcome_card(welcome), welcome_no_butler=welcome_card({**welcome, "butler": NO_CLAUDE, "examples": list(TOPIC_EXAMPLES)}))
    # T669: relays at the top level with 换配置 (a row of its own) and a stop with 换个模型, and the switch card there.
    from covey.app import relay_stops
    from covey.cards.relay_card import DONE, STOPPED, relay_card
    from covey.cards.topic_actions import alone, relay_switch
    from covey.core.failure import failure_view
    stop = {"job_id": 431, "topic_id": TOPIC, "status": "failed", "execution_status": "failed",
            "failure_code": "rate_limited", "harness_id": "codex", "binding_revision": 2}
    cards.update(
        relay_done_switch_alone=relay_card("T70", DONE, text="改好了。", buttons=alone(
            relay_switch(TOPIC, 431, "codex", 2)))[0],
        relay_stopped_switch=relay_card("T70", STOPPED, reason=failure_view(stop)["title"], buttons=[
            relay_stops.buttons(stop, failure_view(stop))])[0],
        switch_card_top=build_switch_card({**switch_form, "top_level": True}))
    trace = ToolTrace()
    for i in range(40):
        trace.note(f"n{i}", f"第 {i} 步说明 " * 20)
    task_card_panels = {
        "topic_panel": topic_panel(TOPIC, 431, ("claude", 2), rewind=True),
        "switch_panel": switch_panel(TOPIC, 431, "codex", 2),
        "question_on_task": question_element(question_record()),
        "trace_panel": split_panel(trace_panel(trace, True)),
        "artifacts": artifact_panel([str(work / "result.md"), str(work / "diff.md")], {"job_id": 431}, TOPIC),
    }
    # A panel is checked inside the task card it is inserted into, so IDs must not collide there either.
    for name, panel in task_card_panels.items():
        card = json.loads(json.dumps(cards["task_plain"]))
        card["body"]["elements"].append(panel)
        cards["task+" + name] = card
    return cards


class CardIdTests(unittest.TestCase):
    def test_every_card_has_cardkit_valid_unique_element_ids(self):
        cards = sample_cards()
        self.assertGreaterEqual(len(cards), 20)
        for name, card in cards.items():
            with self.subTest(card=name):
                ids = element_ids(card)
                bad = [i for i in ids if not isinstance(i, str) or not ELEMENT_ID.match(i)]
                self.assertEqual(bad, [], f"{name}: element IDs CardKit rejects")
                self.assertEqual(len(ids), len(set(ids)), f"{name}: duplicate element IDs {sorted(ids)}")

    def test_open_question_and_supplement_cards_preview_the_topics_state(self):
        # T314 2026-11-02: the topic list shows a topic's latest message's preview.
        card = question_card(question_record(), "T191")
        self.assertNotIn("header", card)  # The topic's row leads (cards/question_cards.py); the preview says what is asked.
        self.assertRegex(card["config"]["summary"]["content"], r"^T191 等你 · 要你定：")  # No task number (T666).
        from covey.cards.steer_form import steer_form
        form = steer_form(2431, "T314 · #2431", {"kind": "bridge_steer_submit"})
        self.assertEqual(form["config"]["summary"]["content"], "T314 · #2431 在做 · 补充表单")

    def test_the_essential_task_card_keeps_only_the_stop_control(self):
        cards = sample_cards()
        full, essential = (json.dumps(cards[n], ensure_ascii=False) for n in ("task_running", "task_running_essential"))
        for kind in ("bridge_steer", "bridge_switch_open"):
            self.assertIn(kind, full)
            self.assertNotIn(kind, essential)
        self.assertIn("job_stop_action", essential)


class LaunchCardIdTests(UnifiedCase):
    def test_the_launch_card_has_valid_ids(self):
        self.receive("om_launch", "task")
        app, ctx = self.route("om_launch")
        ids = element_ids(build_launch_card(app.topics, ctx))
        self.assertTrue(ids)
        self.assertEqual([i for i in ids if not ELEMENT_ID.match(i)], [])
        self.assertEqual(len(ids), len(set(ids)))


REJECTED = Failure("api_rejected", code="300301", detail="ErrMsg: elementID format error")


class TaskCardFallbackTests(unittest.TestCase):
    def create(self, outcomes, job=None):
        cards, logs = [], []

        def api(profile, method, path, data, **kwargs):
            cards.append(json.loads(data["data"]))
            card_id = outcomes.pop(0)
            if card_id is None:
                set_failure(REJECTED)
                return None
            return {"code": 0, "data": {"card_id": card_id}}
        run = dataclasses.replace(stream_runtime(), log=logs.append, progress_factory=lambda **kw: kw,
                                  messenger=Messenger(logs.append), send_card=lambda *_args, **_kwargs: ("om_card", False))
        with FakeCardKit(side_effect=api):
            progress = run.create("unit", job or sample_job(), "已入队")
        return progress, cards, logs

    def test_a_rejected_card_is_retried_with_only_the_stop_control(self):
        progress, cards, logs = self.create([None, "card_essential"])
        self.assertEqual(progress["card_id"], "card_essential")
        first, second = (json.dumps(card, ensure_ascii=False) for card in cards)
        self.assertIn("bridge_switch_open", first)
        for kind in ("bridge_switch_open", "bridge_steer", "bridge_fork"):
            self.assertNotIn(kind, second)
        self.assertIn("job_stop_action", second)
        self.assertTrue(any("retrying with only the stop control" in line for line in logs))

    def test_a_card_rejected_twice_gives_up_and_the_notice_says_why(self):
        progress, cards, _ = self.create([None, None])
        self.assertIsNone(progress)
        self.assertEqual(len(cards), 2)
        notice = task_card.card_failure_notice(sample_job())
        self.assertIn("任务 #431 已收到，但任务卡没有发出去（飞书返回 300301 ErrMsg: elementID format error）", notice)
        self.assertIn("无法在飞书里停止它", notice)

    def test_a_card_accepted_first_time_is_unchanged(self):
        progress, cards, _ = self.create(["card_full"])
        self.assertEqual(progress["card_id"], "card_full")
        self.assertIn("bridge_steer", json.dumps(cards[0]))

    def test_a_new_card_previews_its_task_as_queued_and_a_replacement_as_running(self):
        # T314 2026-11-02: a topic's row in the topic list shows its latest message's preview, and a task card's
        # title cannot change once sent: a running topic read the same as a stopped or failed one.
        progress, cards, _ = self.create(["card_full"])
        title = card_title_for_question(sample_job().user_text, stream_runtime())  # As the title bar writes it.
        self.assertEqual(progress["summary"], f"T70 排队 · {title}")
        self.assertEqual(cards[0]["config"]["summary"]["content"], progress["summary"])
        running = build_stream_card("", sample_job().user_text, stream_runtime(), job=sample_job(execution_started=True))
        self.assertEqual(running["config"]["summary"]["content"], f"T70 在做 · {title}")
        self.assertTrue(running["config"]["streaming_mode"])

    def test_a_new_card_has_no_task_details(self):
        # The owner, 2026-11-22: 任务详情就没必要了. Not even with something to say (a timeout): a card has 本轮请求 and
        # 执行过程, and its progress knows it has no 任务详情 to write.
        for job in (sample_job(), sample_job(timeout=600)):
            progress, cards, _ = self.create(["card_full"], job=job)
            self.assertFalse(progress["live_details"] or progress["details_panel"])
            self.assertIsNone(card_details_text(cards[0]))
            self.assertNotIn("task_details", json.dumps(cards[0]))


TOO_BIG = Failure("api_rejected", code="200860", detail="ErrMsg: card over max size;")
NUMBERS = "".join(f"{i}\n" for i in range(1, 5001))  # #1150's answer: 23,893 characters, 5,000 lines.



INTERNAL = Failure("api_rejected", code="2200", detail="Something went wrong. Please contact technical support")


class TaskCardReplyFallbackTests(unittest.TestCase):
    """#3560 (2026-11-10 13:46): Feishu refused its card's reply to the topic's root with 2200 ("Something went
    wrong"; the root was not withdrawn and took a text reply a second later), and the task ran with no card."""

    def create(self, sends, reply_to="om_root"):
        calls = []

        def send_card(profile, chat_id, card_id, key, *, reply_to_message_id=""):
            calls.append((key, reply_to_message_id))
            outcome = sends.pop(0)
            if isinstance(outcome, Failure):
                set_failure(outcome)
                return "", False
            return outcome, False
        logs = []
        run = dataclasses.replace(stream_runtime(), log=logs.append, progress_factory=lambda **kw: kw,
                                  messenger=Messenger(logs.append), send_card=send_card)
        job = sample_job(reply_to_message_id=reply_to, last_card_error="")
        with FakeCardKit(side_effect=lambda *_a, **_k: {"code": 0, "data": {"card_id": "card_1"}}):
            progress = run.create("unit", job, "已入队")
        set_failure(None)
        return progress, job, calls, logs

    def test_a_reply_refused_outright_goes_once_more_then_to_the_chat(self):
        progress, job, calls, logs = self.create([INTERNAL, INTERNAL, "om_top"])
        self.assertEqual(progress["message_id"], "om_top")
        self.assertEqual([target for _, target in calls], ["om_root", "om_root", ""])
        first = calls[0][0]
        self.assertEqual([key for key, _ in calls], [first, f"{first}-again", f"{first}-top"])
        self.assertIn("2200", job.last_card_error)
        self.assertTrue(job.last_card_error.endswith("改发到会话顶层成功"))
        self.assertTrue(any("task card sent after a refused reply" in line and "to=top" in line for line in logs))

    def test_the_second_reply_is_enough(self):
        progress, job, calls, _ = self.create([INTERNAL, "om_again"])
        self.assertEqual(progress["message_id"], "om_again")
        self.assertEqual(len(calls), 2)
        self.assertTrue(job.last_card_error.endswith("回复重发成功"))

    def test_a_reply_that_may_have_landed_is_sent_nowhere_else(self):
        progress, job, calls, _ = self.create([Failure("timeout", retryable=True, ambiguous=True)])
        self.assertIsNone(progress)
        self.assertEqual(len(calls), 1)  # Rule 1: it may be in the thread already.
        self.assertIn("timeout", job.last_card_error)

    def test_a_card_for_the_chat_itself_is_not_tried_again(self):
        progress, _, calls, _ = self.create([INTERNAL], reply_to="")
        self.assertIsNone(progress)
        self.assertEqual(len(calls), 1)

    def test_refused_everywhere_the_job_keeps_why(self):
        progress, job, calls, _ = self.create([INTERNAL, INTERNAL, Failure("forbidden", code="230002")])
        self.assertIsNone(progress)
        self.assertEqual(len(calls), 3)
        self.assertIn("2200", job.last_card_error)
        self.assertIn("230002", job.last_card_error)
        notice = task_card.card_failure_notice(job)
        self.assertIn("执行中会再补发任务卡", notice)


class TaskCardResendTests(unittest.TestCase):
    """A running task whose card never went out sends it again from its status ticks (Cards.resend_missing)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.ctx = SimpleNamespace(state_dir=self.state, profile="unit")
        self.cards = Cards.__new__(Cards)
        self.cards.runtime = mock.Mock()
        self.made = []

        def create(ctx, job, content):
            progress = self.made.pop(0) if self.made else None
            job.card_missing = progress is None
            return progress
        self.cards.create_streaming_reply = mock.Mock(side_effect=create)

    def job(self, **fields):
        from support import make_job
        return make_job(**{"job_id": 3560, "message_id": "om_key", "topic_id": TOPIC, "checkpoint_path": "",
                           "card_missing": True, **fields})

    def test_sent_again_from_a_tick_and_recorded(self):
        job = self.job()
        self.made = [SimpleNamespace(card_id="card_2", message_id="om_late")]
        self.assertTrue(self.cards.update_card_activity(self.ctx, job, "⏳ 执行中"))
        self.assertEqual(job.progress.message_id, "om_late")
        self.assertFalse(job.card_missing)
        record = find_job_record(self.state, "om_key")
        self.assertEqual((record["card_id"], record["card_message_id"]), ("card_2", "om_late"))
        self.assertIn("执行中", self.cards.create_streaming_reply.call_args.args[2])

    def test_at_most_three_times_a_minute_apart(self):
        job = self.job()
        clock = [1000.0]
        with mock.patch("covey.app.cards.time.monotonic", side_effect=lambda: clock[0]):
            for _ in range(5):
                self.assertFalse(self.cards.update_card_activity(self.ctx, job, "x"))
            self.assertEqual(self.cards.create_streaming_reply.call_count, 1)
            for _ in range(4):
                clock[0] += cards_module.CARD_RESEND_EVERY
                self.cards.update_card_activity(self.ctx, job, "x")
        self.assertEqual(self.cards.create_streaming_reply.call_count, cards_module.CARD_RESENDS)

    def test_a_card_not_sent_marks_the_job_and_its_task_runner_gets_the_mark(self):
        from covey.app import runner_spawn
        cards = Cards.__new__(Cards)
        cards.runtime, cards.streaming, cards.card_facts = mock.Mock(), True, mock.Mock()
        cards.stream = mock.Mock()
        cards.stream.create.return_value = None
        job = self.job(card_missing=False)
        self.assertIsNone(cards.create_streaming_reply(self.ctx, job, "已入队"))
        self.assertTrue(job.card_missing)
        self.assertTrue(runner_spawn.dump_job(job)["extras"]["card_missing"])
        cards.stream.create.return_value = SimpleNamespace(card_id="card_3", message_id="om_3")
        self.assertIsNotNone(cards.create_streaming_reply(self.ctx, job, "已入队"))
        self.assertFalse(job.card_missing)
        self.assertNotIn("card_missing", runner_spawn.dump_job(job)["extras"])

    def test_none_for_a_card_that_went_out_or_was_given_up_or_a_task_stopping_or_delivering(self):
        for fields in ({"card_missing": False}, {"cancel_requested_at": "1"}, {"answer_finalized": True}):
            self.assertFalse(self.cards.update_card_activity(self.ctx, self.job(**fields), "x"))
        self.cards.create_streaming_reply.assert_not_called()


class CardSizeTests(unittest.TestCase):
    """CardKit's card size counts rendered lines and marks, not characters or JSON bytes: #1150's 5,000
    numbers (24 K characters, under the 28,000 limit) were refused three times and the card stayed
    at 执行中; 40,000 characters of Chinese prose were taken (the probe in BATCH2-REPORT.md)."""

    def runtime(self):
        return dataclasses.replace(stream_runtime(), overflow_suffix="\n\n[已截断]")

    def test_a_long_list_of_lines_is_cut_at_a_line_to_the_cards_size(self):
        body = card_cost.truncate_card_content(NUMBERS, self.runtime(), 5000)
        self.assertLess(len(body), len(NUMBERS))
        self.assertTrue(body.endswith("\n\n[已截断]"))
        head = body.removesuffix("\n\n[已截断]")
        self.assertTrue(NUMBERS.startswith(head + "\n"))  # Cut after a whole line.
        self.assertLessEqual(card_cost.text_cost(body) + 5000, card_cost.CARD_COST_BUDGET)
        english = "short line\n" * 2500  # 27,500 characters
        self.assertLessEqual(card_cost.text_cost(card_cost.truncate_card_content(english, self.runtime(), 5000)) + 5000,
                             card_cost.CARD_COST_BUDGET)
        self.assertGreater(card_cost.text_cost(english), card_cost.CARD_COST_BUDGET)

    def test_long_chinese_and_english_prose_under_the_character_limit_stay_whole(self):
        chinese = ("飞书卡片的上限不按字符算，中文一句话占三个字节，但整段文字只算很少的节点。" * 6 + "\n\n") * 50
        english = ("A long English paragraph of plain prose keeps every word on the card. " * 4 + "\n\n") * 90
        for text in (chinese, english):
            self.assertLess(len(text), 28000)
            self.assertEqual(card_cost.truncate_card_content(text, self.runtime(), 5000), text)

    def test_the_rest_of_the_card_leaves_the_body_less_room(self):
        # A process record's lines share the card's size with the answer.
        panel = streaming.build_collapsible_markdown_panel("process_panel", "执行过程", "子任务 · 运行命令\n" * 400)
        alone = card_cost.truncate_card_content(NUMBERS, self.runtime(), 5000)
        beside = card_cost.truncate_card_content(NUMBERS, self.runtime(), 5000 + card_cost.card_cost(panel))
        self.assertLess(len(beside), len(alone))
        self.assertGreater(card_cost.card_cost(panel), 400 * card_cost.LINE_COST)

    def test_a_code_block_cut_short_is_closed(self):
        code = "```python\n" + "x = 1\n" * 6000 + "```\n"
        body = card_cost.truncate_card_content(code, self.runtime(), 0).removesuffix("\n\n[已截断]")
        self.assertTrue(body.endswith("\n```"))
        self.assertEqual(body.count("```") % 2, 0)

    def test_a_cut_by_characters_ends_at_a_line_outside_its_code_block(self):
        # The character limit cut inside a line, and the note that the body is cut sat in the code block.
        runtime = dataclasses.replace(self.runtime(), card_limit=200)
        code = "改动如下：\n```python\n" + "value = compute(1, 2)\n" * 30 + "```\n"
        body = card_cost.truncate_card_content(code, runtime)
        self.assertLessEqual(len(body), 200)
        self.assertTrue(body.endswith("\n```\n\n[已截断]"))
        head = body.removesuffix("\n```\n\n[已截断]")
        self.assertTrue(code.startswith(head + "\n"))  # Cut after a whole line.
        prose = "一行说明文字。\n" * 60
        body = card_cost.truncate_card_content(prose, runtime).removesuffix("\n\n[已截断]")
        self.assertTrue(prose.startswith(body + "\n"))
        self.assertNotIn("```", body)
        # One line longer than the limit keeps its head, as _cut_to_cost keeps it.
        body = card_cost.truncate_card_content("```\n" + "z" * 500 + "\n```", runtime)
        self.assertLessEqual(len(body), 200)
        self.assertTrue(body.startswith("```\n" + "z" * 150))
        self.assertTrue(body.endswith("z\n```\n\n[已截断]"))

    def test_the_task_card_counts_its_other_elements_before_its_body(self):
        job = sample_job(user_text="请求\n" * 3000)  # 本轮请求 is cut to 40 lines, its panel still costs.
        card = build_stream_card(NUMBERS, "t", self.runtime(), job=job, include_stop_action=True)
        others = streaming.other_costs(card)
        self.assertIn("current_question", others)
        self.assertIn("card", others)
        body = next(e for e in card["body"]["elements"] if e.get("element_id") == "answer")["content"]
        self.assertTrue(body.endswith("[已截断]"))
        self.assertLessEqual(sum(others.values()) + card_cost.text_cost(body), card_cost.CARD_COST_BUDGET)
        self.assertGreater(sum(others.values()) + card_cost.text_cost(body), card_cost.CARD_COST_BUDGET - 1000)


class CardSizeRefusalTests(unittest.TestCase):
    """A body CardKit refuses as over its size is shortened and written again at once; that never
    disables the card. Other failed writes still do, after three."""

    def setUp(self):
        self.logs = []
        self.runtime = dataclasses.replace(stream_runtime(), log=self.logs.append, messenger=Messenger(self.logs.append),
                                           safe_idempotency_key=lambda s: s, overflow_suffix="\n\n[已截断]")
        self.progress = StreamingReply("card", "om_card", sequence=1)

    def refuse_over(self, cost):
        def api(profile, method, path, data, **kwargs):
            content = json.loads(data["partial_element"])["content"]
            if card_cost.text_cost(content) > cost:
                set_failure(TOO_BIG)
                return None
            return {"code": 0}
        return api

    def test_a_body_over_cardkits_size_is_shortened_and_never_disables_the_card(self):
        with FakeCardKit(side_effect=self.refuse_over(200_000)) as lark:
            for end in (3000, 4000, 5000):
                text = "".join(f"{i}\n" for i in range(1, end + 1))
                self.assertTrue(self.runtime.update(None, self.progress, text, force=True))
        self.assertFalse(self.progress.disabled)
        self.assertEqual(self.progress.update_failures, 0)
        self.assertTrue(self.progress.cut)
        self.assertTrue(self.progress.last_content.endswith("[已截断]"))
        self.assertLessEqual(card_cost.text_cost(self.progress.last_content), 200_000)
        # Refused once, then taken shorter in the same call; the longer bodies after it are cut to that room.
        self.assertEqual(len(lark.calls), 2)
        sequences = [c.data["sequence"] for c in lark.calls]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertTrue(any("task card body over CardKit's size" in line for line in self.logs))

    def test_a_body_refused_however_short_counts_as_a_failure(self):
        # Then it is the rest of the card that is too big: after shrinking, the card is disabled as before.
        with FakeCardKit(side_effect=self.refuse_over(0)) as lark:
            for _ in range(12):
                if self.progress.disabled:
                    break
                self.assertFalse(self.runtime.update(None, self.progress, NUMBERS, force=True))
        self.assertTrue(self.progress.disabled and self.progress.failed_off)
        self.assertLessEqual(len(lark.calls), 20)

    def test_other_failures_disable_the_card_as_before(self):
        def api(*args, **kwargs):
            set_failure(REJECTED)
            return None
        with FakeCardKit(side_effect=api) as lark:
            for text in ("A", "B", "C", "D"):
                self.runtime.update(None, self.progress, text, force=True)
        self.assertEqual(len(lark.calls), 3)
        self.assertTrue(self.progress.disabled and self.progress.failed_off)
        self.assertFalse(self.progress.cut)

    def test_a_short_body_after_a_cut_one_is_not_cut(self):
        with FakeCardKit(side_effect=self.refuse_over(200_000)):
            self.runtime.update(None, self.progress, NUMBERS, force=True)
            self.assertTrue(self.progress.cut)
            self.runtime.update(None, self.progress, "↓ 回答已单独发送", force=True)
        self.assertFalse(self.progress.cut)


class FailureDetailTests(unittest.TestCase):
    def test_failures_keep_feishus_code_and_short_message(self):
        failure = classify({"ok": False, "error": {"type": "api", "code": "300301",
                                                   "message": "ErrMsg: elementID format error. " + "x" * 300}})
        self.assertEqual((failure.category, failure.code), ("api_rejected", "300301"))
        self.assertTrue(failure.detail.startswith("ErrMsg: elementID format error."))
        self.assertLessEqual(len(failure.detail), 120)
        self.assertEqual(classify({"code": 99991400, "msg": "too many"}).code, "99991400")
        self.assertEqual(classify(None).code, "")

    def test_the_log_names_the_api_not_the_content(self):
        self.assertEqual(operation(["lark-cli", "--profile", "p", "api", "PUT", "/open-apis/cardkit/v1/cards/1",
                                    "--data", '{"secret": "body"}']), "PUT /open-apis/cardkit/v1/cards/1")
        self.assertEqual(operation(["lark-cli", "im", "+messages-reply", "--content", "body"]), "im +messages-reply")
        logs = []
        proc = mock.Mock(returncode=1, stdout="", stderr=json.dumps(
            {"ok": False, "error": {"type": "api", "code": 300301, "message": "elementID format error"}}))
        with mock.patch.object(transport, "_root", None):
            transport.call(["lark-cli", "api", "POST", "/open-apis/cardkit/v1/cards", "--data", "{\"card\": 1}"],
                           log=logs.append, runner=lambda *a, **k: proc)
        self.assertEqual(logs, ["delivery failed category=api_rejected code=300301 op=POST /open-apis/cardkit/v1/cards "
                                "retryable=False attempt=1 detail=elementID format error"])


NOW = 1_800_000_000.0


CARD = {'schema': '2.0', 'body': {'elements': [{'tag': 'markdown', 'content': 'x'}]}}


class CardSequenceRegressionTests(UnifiedCase):

    def sequences(self, lark):
        return [call.data['sequence'] for call in lark.calls]

    def test_an_unconfirmed_write_never_lends_its_sequence_to_the_next_one_in_the_same_second(self):
        app, ctx = self.route('om_task')
        upsert_job_record(ctx.state_dir, 'job', job_id=5, topic_id=ctx.topic_id, chat_id='chat', card_id='task-card',
                          card_message_id='task-msg', status='interrupted')
        remember_browse_card(app, ctx, 'browse-msg', 'browse-card', chat_id='chat')
        with mock.patch('time.time', return_value=NOW):
            # A task card redrawn from its record.
            with FakeCardKit(side_effect=[None, {}]) as lark:
                self.assertFalse(app.cards.replace_record_card(ctx, find_job_record(ctx.state_dir, 'job'), CARD))
                self.assertTrue(app.cards.replace_record_card(ctx, find_job_record(ctx.state_dir, 'job'), CARD))
            first, second = self.sequences(lark)
            self.assertGreater(second, first)
            # A browse card updated in place.
            with FakeCardKit(side_effect=[None, {}]) as lark:
                with self.assertRaises(ValueError):
                    app.messenger.publish_card(ctx, 'chat', 'e1', CARD, source_message='browse-msg', update_only=True,
                                                    voice=app.voice)
                self.assertTrue(app.messenger.publish_card(ctx, 'chat', 'e2', CARD, source_message='browse-msg',
                                                                update_only=True, voice=app.voice))
            first, second = self.sequences(lark)
            self.assertGreater(second, first)

    def test_a_task_card_ahead_of_the_clock_is_never_overtaken_by_its_recovery_card(self):
        app, ctx = self.route('om_live')
        job = BridgeReplyJob(job_id=7, user_text='task', chat_id='chat', message_id='om_live', timeout=0,
                             topic_id=ctx.topic_id)
        # The bot publishes the task card and queues the job.
        with FakeCardKit({'data': {'card_id': 'live-card'}}, sent=('live-msg', False)):
            job.progress = Cards.create_streaming_reply(app.cards, ctx, job, 'queued')  # Not the fixture's stub.
        app.record_job_state(ctx, job, 'queued', 'queued')
        # The task runner takes the card over: its copy comes through the handoff.
        runner = load_job(dump_job(job))
        job.progress = None
        # A time-based write (a page click) and then more than one write a second put
        # the live card ahead of the clock.
        runner.progress.sequence = int(time.time()) + 10
        with FakeCardKit() as live:
            for text in ('a', 'ab', 'abc'):
                self.assertTrue(app.cards.update_streaming_reply(ctx, runner.progress, text, force=True))
        last = max(self.sequences(live))
        # The runner exits without saying more; the bot replaces the card with the recovery card.
        with FakeCardKit({}) as recovery:
            self.assertTrue(app.cards.replace_record_card(ctx, find_job_record(ctx.state_dir, 'om_live'), CARD))
        self.assertGreater(self.sequences(recovery)[0], last)

    def test_a_runners_write_refused_as_behind_the_bots_redraw_is_written_once_more_above_it(self):
        # #1290, 18:17 on 10-28: the next task started, the bot redrew the old card from its job record,
        # and the runner's artifacts panel 11 s later was refused (300317 sequence number compare failed).
        app, ctx = self.route('om_old')
        upsert_job_record(ctx.state_dir, 'old', job_id=1290, topic_id=ctx.topic_id, chat_id='chat',
                          card_id='old-card', card_message_id='old-msg', status='continued')
        job = BridgeReplyJob(job_id=1290, user_text='task', chat_id='chat', message_id='om_old', timeout=0,
                             topic_id=ctx.topic_id)
        job.progress = StreamingReply('old-card', 'old-msg', sequence=int(time.time()) + 5)  # The runner's.
        bind_record(job.progress, ctx.state_dir, 'old')
        top = {}

        def card(profile, method, path, data, **kwargs):  # CardKit: a sequence not above the card's is refused.
            if data['sequence'] <= top.get('seq', 0):
                set_failure(Failure('api_rejected', code='300317', detail='sequence number compare failed'))
                return None
            top['seq'] = data['sequence']
            set_failure(None)
            return {'code': 0}
        panel = {'tag': 'markdown', 'element_id': 'task_artifacts', 'content': '产物'}
        with FakeCardKit(side_effect=card) as lark:
            self.assertTrue(app.cards.update_streaming_reply(ctx, job.progress, 'answer', force=True))
            self.assertTrue(app.cards.replace_record_card(ctx, find_job_record(ctx.state_dir, 'old'), CARD))
            redraw = top['seq']
            self.assertTrue(app.cards.set_task_element(ctx, job, panel))
            self.assertTrue(app.cards.update_streaming_reply(ctx, job.progress, 'answer, more', force=True))
        refused, again = lark.calls[2], lark.calls[3]
        self.assertLess(refused.data['sequence'], redraw)
        self.assertGreater(again.data['sequence'], redraw)  # Fresh, from reserve: above the record's.
        self.assertNotEqual(refused.data['uuid'], again.data['uuid'])
        self.assertEqual(json.loads(again.data['elements']), [panel])
        self.assertEqual(len(lark.calls), 5)  # The body after it goes on from there at once.
        self.assertGreaterEqual(find_job_record(ctx.state_dir, 'old')['card_sequence'], again.data['sequence'])
        # Any other failure may have landed: never written again. A refusal with nothing saved above it
        # neither (a card with no record, ahead of the clock): a fresh sequence would be refused the same way.
        for failure, unbound in ((Failure('network_timeout', True, True), False),
                                 (Failure('api_rejected', code='300317'), True)):
            if unbound:
                job.progress.record_key, job.progress.sequence = '', int(time.time()) + 10 ** 6

            def fail(profile, method, path, data, failure=failure, **kwargs):
                set_failure(failure)
                return None
            with self.subTest(failure=failure.category), FakeCardKit(side_effect=fail) as lark:
                self.assertFalse(app.cards.set_task_element(ctx, job, panel))
            self.assertEqual(len(lark.calls), 1)


class ElementInsertStormTests(UnifiedCase):
    """T784: an element the card already has (another runner of the job added it, or an insert whose answer was lost)
    was inserted again at every trace update for the rest of the turn, each refused with 300315 Duplicate ID."""

    DUPLICATE = Failure('api_rejected', code='300315', detail='ErrMsg: msg: [ElementID process_panel: Code 1001: Duplicate ID]')
    MISSING = Failure('api_rejected', code='300315', detail='ErrMsg: msg: [not find elementID : current_question]')
    PANEL = {'tag': 'markdown', 'element_id': 'process_panel', 'content': '过程'}

    def job(self, ctx):
        job = BridgeReplyJob(job_id=7, user_text='task', chat_id='chat', message_id='om_t', timeout=0, topic_id=ctx.topic_id)
        job.progress = StreamingReply('card', 'msg', sequence=int(time.time()) + 5)
        return job

    def cardkit(self, refused, held):
        def card(profile, method, path, data, **kwargs):
            if method == 'POST' and path.endswith('/elements') and refused is not None:
                set_failure(refused)
                return None
            set_failure(None)
            held.append((method, path))
            return {'code': 0}
        return FakeCardKit(side_effect=card)

    def test_an_element_the_card_has_is_replaced_in_place_not_inserted_again(self):
        app, ctx = self.route('om_t')
        job, held = self.job(ctx), []
        with self.cardkit(self.DUPLICATE, held) as lark:
            for _ in range(6):
                self.assertTrue(app.cards.set_task_element(ctx, job, self.PANEL))
        self.assertEqual([call.method for call in lark.calls], ['POST', 'PUT', 'PUT', 'PUT', 'PUT', 'PUT', 'PUT'])
        self.assertEqual(job.progress.failures_total, 0)

    def test_an_element_refused_again_and_again_is_left_alone_for_a_while(self):
        app, ctx = self.route('om_t')
        job = self.job(ctx)
        with mock.patch('time.monotonic', return_value=1000.0), self.cardkit(self.MISSING, []) as lark:
            for _ in range(20):
                self.assertFalse(app.cards.set_task_element(ctx, job, self.PANEL))
            self.assertLess(len(lark.calls), 6)
            self.assertLess(job.progress.failures_total, 6)
        with mock.patch('time.monotonic', return_value=1000.0 + 600), self.cardkit(self.MISSING, []) as lark:
            self.assertFalse(app.cards.set_task_element(ctx, job, self.PANEL))
            self.assertEqual(len(lark.calls), 1)  # Tried again once the wait is over.
        self.assertIn('300315', job.last_card_error)  # The reason is in the record, not only in the log.


class LeadFirstTests(UnifiedCase):
    """T288: a finished card's lead read first, and the chat list's preview saying what came of the turn."""

    def job(self, mid):
        app, ctx = self.route(mid)
        job = BridgeReplyJob(job_id=7, user_text='task', chat_id='chat', message_id=mid, timeout=0, topic_id=ctx.topic_id)
        job.progress = StreamingReply('card', 'msg')
        return app, ctx, job

    def test_the_lead_moves_under_the_status_line_in_one_batch_and_what_follows_keeps_its_place(self):
        app, ctx, job = self.job('om_lead')
        panel = {'tag': 'markdown', 'element_id': 'task_artifacts', 'content': '产物'}
        with FakeCardKit() as lark:
            self.assertTrue(app.cards.update_streaming_reply(ctx, job.progress, '**↓ 好了**', force=True))
            self.assertTrue(app.cards.lead_first(ctx, job))
            self.assertTrue(app.cards.lead_first(ctx, job))  # Moved once: nothing written again.
            self.assertTrue(app.cards.set_task_element(ctx, job, panel))
        self.assertEqual(len(lark.calls), 3)
        batch, insert = lark.calls[1], lark.calls[2]
        self.assertEqual((batch.method, batch.path.rsplit('/', 1)[-1]), ('POST', 'batch_update'))
        self.assertEqual(json.loads(batch.data['actions']), [
            {'action': 'delete_elements', 'params': {'element_ids': ['answer']}},
            {'action': 'add_elements', 'params': {'type': 'insert_before', 'target_element_id': 'current_question',
                                                  'elements': [{'tag': 'markdown', 'element_id': 'answer',
                                                                'text_size': 'normal_v2', 'content': '**↓ 好了**'}]}}])
        # Inserted "after the body" goes where the body was: under 任务详情, not under the lead.
        self.assertEqual((insert.data['type'], insert.data['target_element_id']), ('insert_after', 'task_details'))

    def test_a_card_that_refuses_the_move_keeps_its_order(self):
        # A card sent before 本轮请求 existed has no current_question: CardKit refuses the batch whole.
        app, ctx, job = self.job('om_old_card')

        def refuse(profile, method, path, data, **kwargs):
            set_failure(REJECTED)
            return None
        with FakeCardKit(side_effect=refuse):
            self.assertFalse(app.cards.lead_first(ctx, job))
        self.assertFalse(job.progress.lead_first)
        with FakeCardKit() as lark:
            self.assertTrue(app.cards.set_task_element(ctx, job, {'tag': 'markdown', 'element_id': 'x', 'content': 'x'}))
        self.assertEqual(lark.last.data['target_element_id'], 'answer')
        job.progress.disabled = True  # A closed card is not written.
        with FakeCardKit() as lark:
            self.assertFalse(app.cards.lead_first(ctx, job))
        self.assertEqual(lark.calls, [])

    def test_closing_the_card_sets_the_chat_list_preview_only_with_a_lead(self):
        app, ctx, job = self.job('om_close')
        with FakeCardKit() as lark:
            app.cards.close_streaming_reply(ctx, job.progress)
            job.progress.summary = '✅ T298 · 界面验收结果（只读）'
            app.cards.close_streaming_reply(ctx, job.progress)
        plain, summed = (json.loads(call.data['settings']) for call in lark.calls)
        self.assertEqual(plain, {'config': {'streaming_mode': False}})
        self.assertEqual(summed, {'config': {'streaming_mode': False, 'summary': {'content': '✅ T298 · 界面验收结果（只读）'}}})
        self.assertEqual({(call.method, call.path.rsplit('/', 1)[-1]) for call in lark.calls}, {('PATCH', 'settings')})


class CardSequenceRuleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def test_a_handle_opened_from_a_shared_card_starts_above_the_clock_and_a_form_from_its_saved_value(self):
        saved = []
        with mock.patch('time.time', return_value=NOW):
            shared = CardHandle('c', saved.append, 3)
            self.assertEqual(reserve(shared), int(NOW) + 1)
            self.assertEqual(CardHandle('c', saved.append, int(NOW) + 50).sequence, int(NOW) + 50)
        own = CardHandle('c', saved.append, 3, shared=False)
        self.assertEqual([reserve(own), reserve(own)], [4, 5])
        self.assertEqual(saved, [int(NOW) + 1, 4, 5])  # Saved as handed out, before any request.

    def test_a_file_card_saves_each_sequence_in_its_record(self):
        path = self.state / 'launch-forms' / 'f.json'
        path.parent.mkdir()
        record = {'card_id': 'cid', 'sequence': 4, 'config_card_id': 'cfg', 'config_sequence': 9}
        path.write_text(json.dumps(record))
        self.assertEqual(reserve(file_card(path, record, shared=False)), 5)
        self.assertEqual(reserve(file_card(path, record, card_field='config_card_id', sequence_field='config_sequence',
                                           shared=False)), 10)
        self.assertEqual({k: v for k, v in json.loads(path.read_text()).items() if 'sequence' in k},
                         {'sequence': 5, 'config_sequence': 10})

    def test_a_job_record_holds_a_ceiling_that_never_goes_down_and_is_never_created(self):
        self.assertEqual(raise_card_sequence(self.state, 'missing', 10), 0)
        self.assertIsNone(find_job_record(self.state, 'missing'))
        upsert_job_record(self.state, 'job', job_id=1, status='running')
        self.assertEqual(raise_card_sequence(self.state, 'job', 10), 10)
        self.assertEqual(raise_card_sequence(self.state, 'job', 4), 10)
        self.assertEqual(find_job_record(self.state, 'job')['card_sequence'], 10)
        self.assertEqual(find_job_record(self.state, 'job')['status'], 'running')
        with mock.patch('time.time', return_value=NOW):
            self.assertEqual(reserve(job_card(self.state, 'job', 'cid')), int(NOW) + 1)
        self.assertEqual(find_job_record(self.state, 'job')['card_sequence'], int(NOW) + 1)

    def test_a_live_card_saves_its_ceiling_once_per_lease(self):
        upsert_job_record(self.state, 'job', job_id=1, status='running')
        progress = StreamingReply(card_id='cid', message_id='mid')
        bind_record(progress, self.state, 'job')
        revisions = []
        for _ in range(LEASE + 2):
            reserve(progress)
            revisions.append(find_job_record(self.state, 'job')['revision'])
        held = find_job_record(self.state, 'job')['card_sequence']
        self.assertGreaterEqual(held, progress.sequence)
        self.assertEqual(len(set(revisions)), 2)  # The first write and the one past the first lease.
        # Unbound (a test double, or a card from an older release's handoff) it saves nothing.
        loose = StreamingReply(card_id='cid2', message_id='mid2')
        self.assertEqual(reserve(loose), 1)

    def test_the_handoff_carries_the_binding_and_a_restart_starts_above_every_used_sequence(self):
        upsert_job_record(self.state, 'om_job', job_id=3, status='queued')
        job = BridgeReplyJob(job_id=3, user_text='t', chat_id='chat', message_id='om_job', timeout=0)
        job.progress = StreamingReply(card_id='cid', message_id='mid')
        bind_record(job.progress, self.state, 'om_job')
        for _ in range(5):
            reserve(job.progress)
        copy = load_job(json.loads(json.dumps(dump_job(job)))).progress
        self.assertEqual((copy.record_key, copy.saved_sequence, copy.sequence), ('om_job', 1 + LEASE, 5))
        self.assertEqual(reserve(copy), 6)
        # A bot restarted the next second reads the record; every sequence used is below where it starts.
        later = StreamingReply(card_id='cid', message_id='mid', sequence=2)
        adopt(later, find_job_record(self.state, 'om_job')['card_sequence'])
        self.assertGreater(reserve(later), copy.sequence)
        with mock.patch('time.time', return_value=NOW):
            self.assertEqual(adopt(StreamingReply(card_id='c', message_id='m', sequence=5)).sequence, int(NOW))


REPO = Path(__file__).resolve().parents[1]
# A task runner (another process) writing a browse card: it reads what is saved, says so, then takes its sequence
# a second later, under the card's lock.
RUNNER_WRITE = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from covey.core.card_sequence import file_card, reserve
from covey.core.locks import configure_card_locks, resource_lock
configure_card_locks(sys.argv[2])
path = Path(sys.argv[3])
with resource_lock("card", "c1"):
    saved = json.loads(path.read_text())
    print("read", flush=True)
    time.sleep(1)
    print(reserve(file_card(path, saved)), flush=True)
"""


class CardLockTests(unittest.TestCase):
    """Repo review E4: the bot and a task runner writing one card in the same second both took
    "this second + 1" (#972 and three more: 200770 on the first try), the card lock being only
    in-process. Configured with the state directory, it is held across processes."""

    def setUp(self):
        from covey.core import locks
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name).resolve()
        self.addCleanup(locks.configure_card_locks, locks.configure_card_locks(self.state))

    def test_two_processes_writing_one_card_never_take_the_same_sequence(self):
        import subprocess
        import sys
        from covey.core.locks import resource_lock
        path = self.state / 'browse-cards' / 'm.json'
        path.parent.mkdir()
        ahead = 10 ** 10  # Above the clock: each writer's sequence is the saved one + 1.
        path.write_text(json.dumps({'card_id': 'c1', 'topic_id': 't', 'chat_id': 'chat', 'sequence': ahead}))
        runner = subprocess.Popen([sys.executable, '-B', '-c', RUNNER_WRITE, str(REPO), str(self.state), str(path)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(runner.wait, 10)
        self.addCleanup(runner.stdout.close)
        self.assertEqual(runner.stdout.readline().strip(), 'read')  # It has read the saved sequence.
        with resource_lock('card', 'c1'):
            mine = reserve(file_card(path, json.loads(path.read_text())))
        theirs = int(runner.stdout.readline())
        self.assertEqual(sorted([mine, theirs]), [ahead + 1, ahead + 2])
        self.assertEqual(json.loads(path.read_text())['sequence'], ahead + 2)
        self.assertTrue((self.state / 'cards' / '.locks' / 'c1.lock').exists())

    def test_a_card_is_locked_in_this_process_only_without_a_state_directory_or_a_card_id(self):
        from covey.core import locks
        self.assertIsInstance(locks.resource_lock('card', 'c2'), locks.SharedFileLock)
        self.assertNotIsInstance(locks.resource_lock('card', 12345), locks.SharedFileLock)  # id(progress): no card yet.
        self.assertEqual(locks.configure_card_locks(None), self.state)
        self.assertNotIsInstance(locks.resource_lock('card', 'c2'), locks.SharedFileLock)
        with locks.resource_lock('card', 'c3'):
            pass
        self.assertFalse((self.state / 'cards').exists())


def card_failure(code, message, kind='api'):
    return subprocess_result(1, '', json.dumps({'ok': False, 'error': {'type': kind, 'code': code, 'message': message}}))


def subprocess_result(returncode, stdout, stderr=''):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class CardWriteRetryTests(unittest.TestCase):
    """Repo review E5: CardKit's 300308 was never tried again (7 streaming writes lost), and a consumed uuid on a
    try after an unknown outcome (#871) was taken as a failure though the write had landed."""

    ELEMENT = '/open-apis/cardkit/v1/cards/c1/elements/activity'
    GATEWAY = card_failure(2200, 'Gateway timeout. Please try again later.', kind='network')
    SERVER = card_failure(300308, 'Server Internal Error')
    CONSUMED = card_failure(200770, 'ErrMsg: this UUID has been recently consumed;')

    def write(self, outcomes, method='PATCH', path=ELEMENT):
        from covey.feishu.cardkit import cardkit_api
        logs = []
        with mock.patch('covey.feishu.transport.subprocess.run', side_effect=outcomes) as run, \
                mock.patch('covey.feishu.transport.time.sleep'), mock.patch.object(transport, '_root', None):
            result = cardkit_api('p', method, path, {'partial_element': '{}', 'sequence': 9, 'uuid': 'u9'}, log=logs.append)
        self.assertEqual(len({str(call.args) for call in run.call_args_list}), 1)  # The same write each try.
        return result, run.call_count, logs

    def test_a_server_error_on_a_card_write_is_tried_again(self):
        result, tries, logs = self.write([self.SERVER, subprocess_result(0, '{"code": 0, "data": {}}')])
        self.assertIsNotNone(result)
        self.assertEqual(tries, 2)
        self.assertEqual(logs, [f'delivery failed category=network code=300308 op=PATCH {self.ELEMENT} retryable=True '
                                'attempt=1 detail=Server Internal Error'])
        # Within the existing limit: three tries.
        self.assertEqual(self.write([self.SERVER] * 3)[:2], (None, 3))

    def test_a_consumed_uuid_after_an_unknown_outcome_is_the_write_having_landed(self):
        result, tries, logs = self.write([self.GATEWAY, self.CONSUMED])
        self.assertEqual((result, tries), ({'code': 0, 'data': {}}, 2))
        self.assertEqual(logs[-1], f'delivery confirmed code=200770 op=PATCH {self.ELEMENT} attempt=2: an earlier try landed')
        result, tries, _ = self.write([self.SERVER, self.CONSUMED], method='POST', path='/open-apis/cardkit/v1/cards/c1/batch_update')
        self.assertEqual((result, tries), ({'code': 0, 'data': {}}, 2))
        # On a first try it is another writer's write with this uuid (E4), not this one: a failure as before.
        self.assertEqual(self.write([self.CONSUMED])[:2], (None, 1))

    def test_a_duplicate_id_after_an_unknown_outcome_is_the_added_elements_having_landed(self):
        # runs/answer-7365643a2f23169d: 300308, then 300315 Duplicate ID for the element the first try added.
        DUPLICATE = card_failure(300315, 'ErrMsg: msg: [ElementID task_artifacts: Code 1001: Duplicate ID], code: 300301;')
        ELEMENTS = '/open-apis/cardkit/v1/cards/c1/elements'
        result, tries, logs = self.write([self.SERVER, DUPLICATE], method='POST', path=ELEMENTS)
        self.assertEqual((result, tries), ({'code': 0, 'data': {}}, 2))
        self.assertEqual(logs[-1], f'delivery confirmed code=300315 op=POST {ELEMENTS} attempt=2: an earlier try landed')
        # On a first try the id is already on the card from another write: a failure as before.
        self.assertEqual(self.write([DUPLICATE], method='POST', path=ELEMENTS)[:2], (None, 1))
        # Only adding elements: another card write's 300315 after an unknown outcome is still a failure.
        self.assertIsNone(self.write([self.SERVER, DUPLICATE])[0])
        # Only a Duplicate ID: another 300315 on adding elements after an unknown outcome is still a failure.
        OTHER = card_failure(300315, 'ErrMsg: msg: [ElementID task_artifacts: Code 1002: invalid element], code: 300301;')
        self.assertEqual(self.write([self.SERVER, OTHER], method='POST', path=ELEMENTS)[:2], (None, 2))

    def test_a_message_write_is_never_taken_as_delivered_or_tried_again_for_these(self):
        # Rule 6: only a card's own writes; a message that may have gone out is never sent again for them.
        for outcomes in ([self.SERVER], [self.GATEWAY, self.CONSUMED]):
            result, tries, _ = self.write(outcomes, method='POST', path='/open-apis/im/v1/messages')
            self.assertIsNone(result)
            self.assertEqual(tries, len(outcomes))
        # Creating a card is not a write to one.
        self.assertEqual(self.write([self.SERVER], method='POST', path='/open-apis/cardkit/v1/cards')[:2], (None, 1))


class CardPublishTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.receive("om_a", "first")
        self.target = self.topic("om_a")

    def test_republishing_from_a_source_card_updates_it_in_place(self):
        card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "page"}]}}
        with FakeCardKit({"data": {"card_id": "card-test"}}, sent=("om_browser", False)) as lark:
            self.assertTrue(self.app.messenger.publish_card(self.target, "oc_chat", "create-browser", card, voice=self.app.voice))
            self.assertTrue(self.app.messenger.publish_card(self.target, "oc_chat", "turn-page", card, "om_browser",
                                                            voice=self.app.voice))
        self.assertEqual(lark.send.call_count, 1)
        self.assertEqual(lark.last.method, "PUT")
        self.assertEqual(lark.last.path, "/open-apis/cardkit/v1/cards/card-test")


    def refused(self, code="300305", detail="ErrMsg: element exceeds the limit;", category="api_rejected"):
        """A CardKit create that Feishu refused, its failure where the transport leaves it."""
        def create(*args, **kwargs):
            from covey.feishu import transport
            transport.set_failure(transport.Failure(category, code=code, detail=detail))
            return None
        return create

    def test_a_card_not_created_says_whose_what_why_and_where_to_go_on(self):
        """T889 (2026-11-27): T884's copy was refused (300305) and the line in its place said only 卡片暂不可用，请稍后重试。"""
        from covey.core.topics import topic_label
        from covey.feishu.messenger import CardAbout
        card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "page"}]}}
        label = topic_label(self.target.state_dir, self.target.topic_id)
        self.assertTrue(label)
        with FakeCardKit(side_effect=self.refused()), mock.patch.object(self.app.messenger, "reply") as reply:
            self.assertFalse(self.app.messenger.publish_card(
                self.target, "oc_chat", "question-q1", card, voice=self.app.voice,
                about=CardAbout("问题卡", where=lambda: "可以直接在话题里回复作答，或到网页工作台答题")))
        text = reply.call_args.args[2]
        self.assertTrue(text.startswith(f"⚠️ {label}"), text)
        self.assertIn("的问题卡没建成：卡片超过飞书一张卡 200 个组件的上限（300305）。", text)
        self.assertTrue(text.endswith("可以直接在话题里回复作答，或到网页工作台答题。"))
        self.assertEqual(reply.call_args.args[3], self.app.voice.key("browse-error", "question-q1"))  # Said once a card.

    def test_a_card_created_but_not_sent_and_one_of_no_topic(self):
        from covey.feishu import transport
        from covey.feishu.messenger import not_shown_text, CardAbout
        card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "page"}]}}

        def not_sent(*args, **kwargs):
            transport.set_failure(transport.Failure("rate_limited", True))
            return "", False
        with FakeCardKit({"data": {"card_id": "card-x"}}, sent_effect=not_sent), \
                mock.patch.object(self.app.messenger, "reply") as reply:
            self.assertFalse(self.app.messenger.publish_card(self.target, "oc_chat", "page-1", card, voice=self.app.voice))
        text = reply.call_args.args[2]
        self.assertIn("的卡片建好了但没发出去：飞书限流，暂时不收。稍后再试；急的话直接在话题里说。", text)
        self.assertEqual(reply.call_args.args[3], self.app.voice.key("browse-send-error", "page-1"))
        # The bot menu's cards have no topic: the line says so without making one up; an unknown code keeps Feishu's words.
        failure = transport.Failure("api_rejected", code="123", detail="some reason")
        self.assertEqual(not_shown_text(CardAbout("设置卡"), "", failure, False),
                         "⚠️ 这张设置卡没建成：飞书拒绝（123：some reason）。稍后再试。")

    def test_a_quiet_card_leaves_the_line_to_its_caller(self):
        from covey.feishu.messenger import CardAbout
        card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "page"}]}}
        with FakeCardKit(side_effect=self.refused()), mock.patch.object(self.app.messenger, "reply") as reply, \
                mock.patch.object(self.app.messenger, "log") as log:
            self.assertFalse(self.app.messenger.publish_card(self.target, "oc_chat", "note-1", card, voice=self.app.voice,
                                                             about=CardAbout("暂存卡", quiet=True)))
        reply.assert_not_called()
        self.assertIn("card not shown what=暂存卡 step=create category=api_rejected code=300305",
                      [c.args[0] for c in log.call_args_list])

    def test_whose_names_what_it_could_not_read(self):
        from covey.feishu.messenger import whose
        self.assertEqual(whose(SimpleNamespace(state_dir=self.target.state_dir, topic_id="")), "")
        self.assertEqual(whose(SimpleNamespace(state_dir=self.target.state_dir, topic_id="z" * 64)), "话题（T 号和标题都没读到）")


class CardActionTableTest(unittest.TestCase):

    def test_job_starting_kinds_stay_with_the_bot(self):
        # bridge_question_skip: the Bridge hands the answer on to a runner that waits for it (forward_answer).
        # bridge_fork, bridge_compact: retired buttons on older cards, answered here (app/task_controls.py retired_action).
        # bridge_project_triage_wrong, _fix, bridge_revisit_hint, bridge_board_seen, _revisit_cancel, _project_go, _recover: retired
        # buttons (T898), answered here and never forwarded to a task runner, which an older release might still run them in.
        # bridge_job_look: 看看 looks at the task runner from outside (app/stuck_check.py), which may be the stuck one.
        # bridge_question_ask: 先问一句 queues the topic's turn or a supplement of its running task (app/questions.py).
        self.assertEqual(BOT_ONLY - set(extensions.card_kinds()), {"bridge_job_retry", "bridge_fork", "bridge_compact", "bridge_steer_requeue",
                                    "bridge_note", "bridge_job_retry_cancel", "bridge_question_skip", "bridge_question_ask",
                                    "bridge_turn_export",
                                    "bridge_project_triage_fix", "bridge_project_triage_wrong", "bridge_revisit_hint",
                                    "bridge_board_seen", "bridge_board_revisit_cancel", "bridge_project_ask_continue", "bridge_project_ask_finish",
                                    "bridge_job_look", "bridge_job_diagnose", "bridge_job_split", "bridge_job_report",
                                    "bridge_welcome_try", "bridge_board_project_go",
                                    "bridge_board_recover"})  # 看看 reads a stuck task; 查原因 and 拆小再试 queue a turn; 复制诊断 reads an ended one. Triage: the correction queues a message in a member topic; the pause card's two wake the lead. The welcome card's example is the owner's message.
        self.assertFalse(runner_ipc.forward_card_action(Runtime(script_path="unit"), None, {},
                                                     {"kind": "bridge_fork"}))

    def test_a_failed_stop_names_the_stop(self):
        self.assertEqual(action_label(JOB_STOP_ACTION_KIND), "停止任务")
        self.assertEqual(action_label("bridge_model_settings"), "卡片操作")


def click(topic="", message="om_card", chat="chat", label=""):
    value = {"kind": "bridge_trace", "label": label}
    if topic:
        value["topic_id"] = topic
    return {"chat_id": chat, "message_id": message, "action_value": json.dumps(value)}


class CardLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = SimpleNamespace(state_dir=Path(self.tmp.name))
        self.shutdown = threading.Event()
        self.addCleanup(self.shutdown.set)
        self.logs = []

    def lanes(self, handle, health=None, notify=None):
        return CardLanes(handle, UnifiedBridgeApp.card_lane_key, self.logs.append, self.shutdown, health, notify)

    def wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("lanes did not finish")

    def test_a_slow_click_holds_its_own_topic_but_not_others(self):
        release, entered, seen = threading.Event(), threading.Event(), []

        def handle(event, ctx):
            label = json.loads(event["action_value"])["label"]
            if label == "slow":
                entered.set()
                release.wait(5)
            seen.append(label)
        lanes = self.lanes(handle)
        lanes.submit(click("t1", label="slow"), self.ctx)
        lanes.submit(click("t1", label="after-slow"), self.ctx)
        lanes.submit(click("t2", label="other-topic"), self.ctx)
        self.assertTrue(entered.wait(5))
        self.wait(lambda: seen == ["other-topic"])
        # The same topic's later click still waits, queued behind the running one.
        self.assertEqual([json.loads(work.args[0]["action_value"])["label"] for work, _, _ in lanes.lanes["topic:t1"]],
                         ["slow", "after-slow"])
        self.assertEqual(seen, ["other-topic"])
        release.set()
        self.wait(lambda: len(seen) == 3)
        self.assertEqual(seen, ["other-topic", "slow", "after-slow"])

    def test_a_follow_up_runs_after_the_click_without_holding_its_topic(self):
        release, following, seen = threading.Event(), threading.Event(), []

        def follow_up():
            following.set()
            release.wait(5)
            seen.append("follow-up")

        def handle(event, ctx):
            label = json.loads(event["action_value"])["label"]
            if label == "answer":
                card_lanes.after_ack(follow_up)
            seen.append(label)
        lanes = self.lanes(handle)
        lanes.submit(click("t1", label="answer"), self.ctx)
        self.assertTrue(following.wait(5))
        lanes.submit(click("t1", label="next"), self.ctx)  # The topic's next click does not wait for it.
        self.wait(lambda: seen == ["answer", "next"])
        release.set()
        self.wait(lambda: seen == ["answer", "next", "follow-up"])
        self.wait(lambda: any(line.startswith("card follow-up kind=") for line in self.logs))

    def test_a_follow_up_beside_the_others_runs_at_the_same_time(self):
        # S36: 开始任务's receipt (0.4 s) was written before its task cards, which waited for it.
        release, seen = threading.Event(), []
        self.addCleanup(release.set)

        def handle(event, ctx):
            card_lanes.after_ack(lambda: (release.wait(5), seen.append("receipt")), beside=True)
            card_lanes.after_ack(lambda: seen.append("task card"))
        lanes = self.lanes(handle)
        lanes.submit(click("t1"), self.ctx)
        self.wait(lambda: seen == ["task card"])
        release.set()
        self.wait(lambda: seen == ["task card", "receipt"])
        self.wait(lambda: lanes.unsettled == 0)

    def test_follow_ups_run_in_order_and_report_their_failure(self):
        seen, reported = [], []

        def failing():
            seen.append("first")
            raise ValueError("后续任务未能入队")

        def handle(event, ctx):
            label = json.loads(event["action_value"])["label"]
            with card_lanes.reporting(reported.append):
                card_lanes.after_ack(failing if label == "one" else lambda: seen.append("second"))
        lanes = self.lanes(handle)
        lanes.submit(click("t1", label="one"), self.ctx)
        lanes.submit(click("t1", label="two"), self.ctx)
        self.wait(lambda: seen == ["first", "second"])
        self.assertEqual([str(exc) for exc in reported], ["后续任务未能入队"])
        self.assertIn("card follow-up error: ValueError", self.logs)

    def test_a_clean_stop_runs_acknowledged_follow_ups_but_starts_no_new_click(self):
        # S21 review item 3: a deploy between the 提交回答 ack and its follow-up dropped the follow-up.
        release, following, seen = threading.Event(), threading.Event(), []

        def handle(event, ctx):
            label = json.loads(event["action_value"])["label"]
            seen.append(label)
            if label == "answer":
                card_lanes.after_ack(lambda: (following.set(), release.wait(5), seen.append("follow-up")))
                card_lanes.after_ack(lambda: seen.append("second follow-up"))
        lanes = self.lanes(handle)
        lanes.submit(click("t1", label="answer"), self.ctx)
        self.assertTrue(following.wait(5))
        self.shutdown.set()
        lanes.submit(click("t1", label="late click"), self.ctx)  # Arrives while the Bridge stops.
        threading.Timer(0.2, release.set).start()
        self.assertTrue(lanes.drain(timeout=5))
        self.assertEqual(seen[:3], ["answer", "follow-up", "second follow-up"])
        time.sleep(0.6)
        self.assertNotIn("late click", seen)  # Never acknowledged: not started while stopping.

    def test_drain_gives_up_after_its_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        lanes = self.lanes(lambda event, ctx: card_lanes.after_ack(lambda: release.wait(5)))
        lanes.submit(click("t1"), self.ctx)
        self.wait(lambda: lanes.unsettled == 1 and not lanes.lanes.get("topic:t1"))
        self.shutdown.set()
        started = time.monotonic()
        self.assertFalse(lanes.drain(timeout=0.3))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("card follow-ups not finished at stop: 1 left after 0.3 s", self.logs)
        release.set()
        self.wait(lambda: lanes.unsettled == 0)

    def test_off_a_lane_a_follow_up_runs_at_once_and_raises_to_its_caller(self):
        seen = []
        card_lanes.after_ack(lambda: seen.append("now"))
        self.assertEqual(seen, ["now"])
        with self.assertRaises(ValueError):
            card_lanes.after_ack(mock.Mock(side_effect=ValueError("x")))

    def test_clicks_find_their_topic_from_the_card_message(self):
        remember_topic_message(self.ctx.state_dir, "t9", "chat", "om_launch")
        self.assertEqual(UnifiedBridgeApp.card_lane_key(click(message="om_launch"), self.ctx), "topic:t9")
        self.assertEqual(UnifiedBridgeApp.card_lane_key(click("t3", message="om_launch"), self.ctx), "topic:t3")
        self.assertEqual(UnifiedBridgeApp.card_lane_key(click(message="om_main"), self.ctx), "card:chat:om_main")

    def test_a_failing_click_does_not_end_its_lane(self):
        seen = []

        def handle(event, ctx):
            label = json.loads(event["action_value"])["label"]
            seen.append(label)
            if label == "boom":
                raise RuntimeError("broken click")
        lanes = self.lanes(handle)
        lanes.submit(click("t1", label="boom"), self.ctx)
        lanes.submit(click("t1", label="next"), self.ctx)
        self.wait(lambda: seen == ["boom", "next"])
        self.assertIn("handle_card_action_event error: broken click", self.logs)

    def test_a_click_that_raised_is_told_under_its_card_once_per_card_and_button(self):
        told = []

        def handle(event, ctx):
            raise RuntimeError("broken click")
        lanes = self.lanes(handle, notify=lambda event, ctx, exc: told.append((event["message_id"], type(exc).__name__)))
        for message, label in (("om_a", "x"), ("om_a", "x"), ("om_b", "x")):
            lanes.submit(click("t1", label=label, message=message), self.ctx)
        self.wait(lambda: len([line for line in self.logs if line.startswith("handle_card_action_event error")]) == 3)
        self.assertEqual(told, [("om_a", "RuntimeError"), ("om_b", "RuntimeError")])

    def test_a_notify_that_raises_does_not_end_the_lane(self):
        seen = []

        def handle(event, ctx):
            seen.append(event["message_id"])
            raise RuntimeError("broken click")

        def notify(event, ctx, exc):
            raise OSError("no network")
        lanes = self.lanes(handle, notify=notify)
        lanes.submit(click("t1", message="om_a"), self.ctx)
        lanes.submit(click("t1", message="om_b"), self.ctx)
        self.wait(lambda: seen == ["om_a", "om_b"])
        self.wait(lambda: self.logs.count("card click failure not told: OSError") == 2)

    def test_a_full_backlog_handles_on_the_consumer_thread(self):
        threads = []
        lanes = self.lanes(lambda event, ctx: threads.append(threading.current_thread()))
        with mock.patch.object(card_lanes, "LIMIT", 0):
            lanes.submit(click("t1"), self.ctx)
        self.assertEqual(threads, [threading.current_thread()])

    def test_health_reports_each_clicks_own_time(self):
        clock = FakeClock()  # Health time moves only when the test says so.
        patcher = mock.patch.object(feishu_events, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        health = ConsumerHealth("card.action.trigger")
        health.inline_durations = False
        release, entered = threading.Event(), threading.Event()
        lanes = self.lanes(lambda event, ctx: (entered.set(), release.wait(5)), health)
        health.begin()
        lanes.submit(click("t1"), self.ctx)
        health.end()  # The consumer thread's own part takes no measurable time.
        self.assertEqual(health.snapshot()["handler_ms"]["samples"], 0)
        self.assertTrue(entered.wait(5))
        clock.advance(0.2)
        self.assertEqual(health.snapshot()["handling_seconds"], 0.2)  # A stuck click stays visible.
        release.set()
        self.wait(lambda: health.snapshot()["handler_ms"]["samples"] == 1)
        self.assertEqual(health.snapshot()["handler_ms"]["p50"], 200)
        self.assertEqual(health.snapshot()["events"], 1)


class CardActionQueueTests(unittest.TestCase):
    def gateway(self):
        gateway = SimpleNamespace(runtime=Runtime(script_path='unit', log_sink=mock.Mock()))
        self.addCleanup(gateway.runtime.shutdown.set)
        return gateway

    def test_a_full_queue_drops_the_click_and_says_so(self):
        gateway = self.gateway()
        actions = CardActionQueue(gateway, name='test-actions', workers=1, label='test picker')
        actions.started = True  # No worker drains it.
        ran = []
        self.assertTrue(all(actions.submit('dropped', ran.append, n) for n in range(CardActionQueue.LIMIT)))
        self.assertFalse(actions.submit('dropped', ran.append, 'late'))
        gateway.runtime.log_sink.assert_called_once_with('test picker busy; dropped')
        self.assertEqual(actions.queue.qsize(), CardActionQueue.LIMIT)
        self.assertEqual(ran, [])

    def test_workers_start_once_and_outlive_a_failing_click(self):
        gateway = self.gateway()
        actions = CardActionQueue(gateway, name='test-actions-workers', workers=2, label='test picker')
        done = []

        def fail():
            raise RuntimeError('boom')

        actions.submit('dropped', fail)
        for n in range(5):
            actions.submit('dropped', done.append, n)
        actions.queue.join()
        self.assertEqual(sorted(done), list(range(5)))
        gateway.runtime.log_sink.assert_called_once_with('test picker failed: RuntimeError')
        names = [t.name for t in threading.enumerate() if t.name == 'test-actions-workers']
        self.assertEqual(len(names), 2)

    def test_a_clean_stop_runs_the_clicks_already_queued(self):
        # A restart dropped a 确认更换 or 回退 queued behind another click: the workers left at shutdown.
        gateway = self.gateway()
        actions = CardActionQueue(gateway, name='test-actions-stop', workers=1, label='test picker')
        self.assertIn(actions, gateway.runtime.card_queues)
        release, ran = threading.Event(), []
        self.addCleanup(release.set)
        actions.submit('dropped', release.wait, 5)
        actions.submit('dropped', ran.append, 'queued')
        gateway.runtime.shutdown.set()
        lanes = CardLanes(mock.Mock(), lambda event: '', gateway.runtime.log, gateway.runtime.shutdown)
        threading.Timer(.2, release.set).start()
        self.assertTrue(lanes.drain(5, gateway.runtime.card_queues))
        self.assertEqual(ran, ['queued'])
        # Within the same deadline: what is still running then is left, with one line.
        release.clear()
        actions.submit('dropped', release.wait, 5)  # A click queued during the stop runs too.
        self.assertFalse(lanes.drain(.2, gateway.runtime.card_queues))
        gateway.runtime.log_sink.assert_called_once_with('test picker clicks not finished at stop: 1 left after 0.2 s')


class ControllerQueueTests(UnifiedCase):
    def test_each_controller_has_its_own_queue_and_workers(self):
        controllers = {'launch-model': self.app.launch_model_picker, 'directory-picker': self.app.directory_picker,
                       'workspace-manager': self.app.workspace_manager}
        self.assertEqual({name: (c.name, c.workers) for name, c in controllers.items()},
                         {'launch-model': ('launch-model', 2), 'directory-picker': ('directory-picker', 3),
                          'workspace-manager': ('workspace-manager', 2)})
        self.assertEqual(len({id(c.queue) for c in controllers.values()}), 3)
        self.assertTrue(all(c.queue.maxsize == 32 for c in controllers.values()))

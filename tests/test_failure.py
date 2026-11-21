"""Why a turn did not succeed and what next (core/failure.py): the task card's body and buttons, the recovery card and
the web page's turn read one view (C4, 2026-11-18)."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from covey.app import auto_retry
from covey.cards.recovery import DIAGNOSE_KIND, SPLIT_KIND
from covey.core import failure

POLICY = {"kind": "refusal", "category": "frontier_llm", "scope": "subagent", "child_id": "toolu_1", "response_id": "",
          "source_model": "", "target_model": "", "evidence": "refusal_stop"}


def _buttons(elements):
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("tag") == "button":
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(elements)
    return found


def _value(button):
    return button.get("value") or next((b.get("value") for b in button.get("behaviors") or () if b.get("value")), {})


def failed(**fields):
    return {"job_id": 7, "topic_id": "topic", "status": "failed", "execution_status": "failed", **fields}


class ViewTest(unittest.TestCase):
    def test_each_ending_has_a_plain_reason_and_its_next_steps(self):
        now = time.time()
        cases = {
            "rate_limited": (failed(failure_code="rate_limited", auto_retry_state="pending", auto_retry_at=now + 170,
                                    auto_retry_attempt=1, auto_retry_kind="rate_limited"),
                             "上游限流，和任务内容无关；约 3 分钟后", ["retry", "switch_model"]),
            "quota": (failed(failure_code="quota_exhausted"), "额度用完了", ["switch_model"]),
            "unfinished": (failed(failure_code="root_failed"), "这一轮没做完（主任务中途出错），已做的部分保留着；可以在新会话里接着做",
                           ["continue", "diagnose"]),
            "restart": ({"job_id": 7, "topic_id": "topic", "status": "interrupted", "interrupted_by": "restart"},
                        "Covey 重启时打断了这一轮；可以接着做", ["continue"]),
            "restart_pending": ({"job_id": 7, "topic_id": "topic", "status": "interrupted", "interrupted_by": "restart",
                                 "auto_retry_state": "pending", "auto_retry_at": now + 60, "auto_retry_attempt": 1,
                                 "auto_retry_kind": "restarted"}, "Covey 重启时打断了这一轮，约 1 分钟后", []),
            "error": (failed(failure_code="transport_error"), "任务异常：和执行后端的连接断了", ["continue", "diagnose"]),
            "timeout": (failed(failure_code="timeout"), "到了任务的时间上限", ["continue", "split"]),
            "policy": ({"job_id": 7, "topic_id": "topic", "status": "cancelled", "policy_stop": POLICY,
                        "failure_detail": "子代理「标注第 1 块」在读 chunk01.txt 第 1201–1600 行时被拦（frontier_llm）。"},
                       "子代理「标注第 1 块」在读 chunk01.txt 第 1201–1600 行时被拦（frontier_llm）；这一轮已停止",
                       ["split", "switch_model"]),
        }
        for name, (record, title, actions) in cases.items():
            with self.subTest(name):
                view = failure.failure_view(record, now=now)
                self.assertIn(title, view["title"])
                self.assertEqual(view["actions"], actions)
                self.assertNotIn("原生会话", view["title"])
        self.assertIn("toolu_1", failure.failure_view(cases["policy"][0])["detail"])  # Internal words go to the detail.
        self.assertIn("错误码 root_failed", failure.failure_view(cases["unfinished"][0])["detail"])

    def test_an_old_record_is_read_from_its_status_line(self):
        view = failure.failure_view(failed(status_message="本轮执行未全部完成，原生会话不可续接"))
        self.assertEqual(view["title"], "这一轮没做完，已做的部分保留着；可以在新会话里接着做，或先看原因")
        self.assertEqual(failure.failure_view(failed(status_message="模型服务限流，本轮没有产出回复"))["actions"],
                         ["retry", "switch_model"])

    def test_no_view_for_a_turn_that_succeeded_runs_or_the_owner_stopped(self):
        for record in ({"status": "done"}, {"status": "running"}, {"status": "waiting_user"},
                       {"status": "cancelled", "cancel_requested_by": "ou_owner"}, {"status": "cancelled"},
                       failed(result_retry_policy="saved_delivery_only", verified_reply_authorized=True,
                              verified_reply_state="authorized", delivery_ok=True)):
            self.assertIsNone(failure.failure_view(record), record)

    def test_steps_the_record_does_not_allow_are_left_out(self):
        self.assertEqual(failure.failure_view(failed(failure_code="root_failed", topic_id=""))["actions"], [])
        # 检查并继续 would redeliver a result that did not go out, not run it again: no 继续, no 看原因.
        undelivered = failed(execution_status="done", delivery_ok=False, final_text="回答")
        self.assertEqual(failure.failure_view(undelivered)["actions"], ["continue"])
        self.assertIn("结果没能发出去", failure.failure_view(undelivered)["title"])
        # A policy stop whose retry on an older model is scheduled: nothing to click beside it.
        retried = {"job_id": 7, "topic_id": "topic", "status": "cancelled", "policy_stop": {**POLICY, "category": "cyber"},
                   "policy_retry_state": "pending", "policy_retry_model": "claude-opus-4-8"}
        view = failure.failure_view(retried)
        self.assertEqual((view["actions"], view["title"].endswith("将换 Opus 4.8 自动重试一次")), ([], True))

    def test_the_follow_ups_say_what_the_owner_asked_for(self):
        record = failed(failure_code="timeout")
        view = failure.failure_view(record)
        shown, prompt = failure.follow_up(failure.DIAGNOSE, record, view)
        self.assertEqual(shown, "看原因：任务 #7 为什么没成功")
        self.assertIn("只读排查", prompt)
        self.assertIn("不要改文件", prompt)
        self.assertIn(view["title"], prompt)
        shown, prompt = failure.follow_up(failure.SPLIT, record, view)
        self.assertEqual(shown, "拆小再试：任务 #7")
        self.assertIn("拆成更小的几步", prompt)


class CardTest(unittest.TestCase):
    def test_a_failed_card_has_its_old_words_in_details_and_its_next_steps(self):
        record = failed(failure_code="root_failed", final_text="本轮任务未全部完成。主任务后续执行失败。")
        text = json.dumps(auto_retry.failed_controls("topic", 7, record), ensure_ascii=False)
        self.assertIn("技术详情", text)
        self.assertIn("主任务后续执行失败", text)
        self.assertIn(DIAGNOSE_KIND, text)
        self.assertIn("检查并继续", text)
        limited = json.dumps(auto_retry.failed_controls("topic", 7, failed(failure_code="rate_limited")), ensure_ascii=False)
        self.assertIn("现在重试", limited)
        self.assertNotIn(DIAGNOSE_KIND, limited)
        stopped = {"job_id": 7, "topic_id": "topic", "status": "cancelled", "policy_stop": POLICY, "final_text": "本轮已停止。"}
        text = json.dumps(auto_retry.failed_controls("topic", 7, stopped), ensure_ascii=False)
        self.assertIn(SPLIT_KIND, text)
        self.assertNotIn("技术详情", text)  # Its body says who was refused doing what itself.
        self.assertNotIn("检查并继续", text)

    def test_a_failure_a_retry_cannot_fix_offers_another_model_in_its_row(self):
        """换个模型 (2026-11-20): 额度用完 and the like put the 换配置 click in the row as 换个模型, with the topic's
        route; a record without one (no binding) gets no such button, as before."""
        from covey.cards.recovery import SWITCH_LABEL, offers_switch, recovery_view
        from covey.cards.topic_actions import SWITCH_OPEN
        bound = failed(failure_code="quota_exhausted", harness_id="claude", binding_revision=3)
        controls = auto_retry.failed_controls("topic", 7, bound)
        switch, = [v for v in map(_value, _buttons(controls)) if v.get("kind") == SWITCH_OPEN]
        self.assertEqual(switch, {"kind": SWITCH_OPEN, "topic_id": "topic", "job_id": 7, "harness_id": "claude",
                                  "binding_revision": 3})
        text = json.dumps(controls, ensure_ascii=False)
        self.assertIn(SWITCH_LABEL, text)
        self.assertIn("换个模型：打开换配置", text)
        self.assertNotIn(SWITCH_OPEN, json.dumps(auto_retry.failed_controls("topic", 7, failed(failure_code="quota_exhausted")),
                                                 ensure_ascii=False))
        # The recovery card puts it in the row and leaves out the 换配置 row under 执行过程 that would open the same card.
        row = auto_retry.result_actions("topic", 7, "", follow_ups=("switch_model",), route=("claude", 3))
        card = recovery_view({**bound, "status": "failed"}, "", word="stopped", headline="额度用完了", topic="T9", label="",
                             continued=False, moved_to=None, how="", retry=None, actions=(row,), snapshot=None)
        self.assertTrue(offers_switch([row]))
        self.assertEqual(json.dumps(card, ensure_ascii=False).count(SWITCH_OPEN), 1)
        plain = recovery_view({**bound, "status": "failed"}, "", word="stopped", headline="x", topic="T9", label="",
                              continued=False, moved_to=None, how="", retry=None, actions=(), snapshot=None)
        self.assertIn('"element_id": "switch_actions"', json.dumps(plain))

    def test_a_limit_offers_another_harness_through_the_switch_card_never_by_itself(self):
        """#36: a rate limit or a used-up quota adds 换个 Harness after 换个模型, the same 换配置 click with ``other``;
        only with another Harness enabled and a topic that does not redact."""
        import tempfile
        from covey.cards.recovery import OTHER_LABEL
        from covey.cards.topic_actions import SWITCH_OPEN
        from covey.core.redaction import TOPIC_KEY
        from covey.core.state import write_json

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            write_json(state / "topics" / "topic" / "topic.json", {"topic_id": "topic"})
            both = SimpleNamespace(apps={"claude": object(), "codex": object()})
            for code in ("quota_exhausted", "rate_limited"):
                bound = failed(failure_code=code, harness_id="claude", binding_revision=3)
                self.assertTrue(auto_retry.other_harness(state, bound, both), code)
                controls = auto_retry.failed_controls("topic", 7, bound, other=True)
                values = [v for v in map(_value, _buttons(controls)) if v.get("kind") == SWITCH_OPEN]
                self.assertEqual(values[-1], {"kind": SWITCH_OPEN, "topic_id": "topic", "job_id": 7, "harness_id": "claude",
                                              "binding_revision": 3, "other": True})
                self.assertIn(OTHER_LABEL, json.dumps(controls, ensure_ascii=False))
                self.assertNotIn(OTHER_LABEL, json.dumps(auto_retry.failed_controls("topic", 7, bound), ensure_ascii=False))
            bound = failed(failure_code="quota_exhausted", harness_id="claude", binding_revision=3)
            self.assertFalse(auto_retry.other_harness(state, bound, SimpleNamespace(apps={"claude": object()})))
            self.assertFalse(auto_retry.other_harness(state, failed(failure_code="root_failed", harness_id="claude",
                                                                    binding_revision=3), both))
            self.assertFalse(auto_retry.other_harness(state, failed(failure_code="quota_exhausted"), both))  # No route.
            write_json(state / "topics" / "topic" / "topic.json", {"topic_id": "topic", TOPIC_KEY: True})
            self.assertFalse(auto_retry.other_harness(state, bound, both))  # A topic that redacts stays where it is.

    def test_the_body_of_a_failed_card_is_its_reason(self):
        from covey.app.turn_delivery import TurnDelivery
        turn = SimpleNamespace(status="failed", verified_delivery=None, status_message="本轮执行未全部完成，原生会话不可续接",
                               outcome=SimpleNamespace(error_code="root_failed", policy_event=None), failure_detail="",
                               final_text="本轮任务未全部完成。主任务后续执行失败。")
        delivery = SimpleNamespace(job=SimpleNamespace(topic_id="topic", job_id=7, progress=object()), turn=turn, retry=None)
        delivery.failure_fields = lambda: TurnDelivery.failure_fields(delivery)
        self.assertEqual(TurnDelivery.card_text(delivery, turn.final_text),
                         "**这一轮没做完（主任务中途出错），已做的部分保留着；可以在新会话里接着做，或先看原因**")
        turn.outcome = SimpleNamespace(error_code="policy_refusal", policy_event=POLICY)
        self.assertEqual(TurnDelivery.card_text(delivery, "本轮已停止：……"), "本轮已停止：……")

    def test_a_click_queues_the_follow_up_once_the_topic_is_idle(self):
        from covey.app.submission import Submission
        record = failed(failure_code="timeout")
        queued = []
        submission = SimpleNamespace(
            runtime=SimpleNamespace(jobs=SimpleNamespace(topic_jobs=lambda topic, chat: [])),
            harness=SimpleNamespace(topic_records=lambda ctx, chat: [record]),
            enqueue_chat_job=lambda ctx, content, chat, mid, **kw: queued.append((content, kw["prompt_content"])))
        ctx = SimpleNamespace(topic_id="topic")
        self.assertEqual(Submission.queue_follow_up(submission, ctx, "chat", "ev1", 7, failure.SPLIT), "")
        self.assertEqual(queued[0][0], "拆小再试：任务 #7")
        self.assertEqual(Submission.queue_follow_up(submission, ctx, "chat", "ev2", 7, failure.DIAGNOSE),
                         "这个任务现在没有这一步可做。")  # A timeout offers 拆小再试, not 看原因.
        submission.runtime.jobs.topic_jobs = lambda topic, chat: [object()]
        self.assertIn("还有运行或排队的任务", Submission.queue_follow_up(submission, ctx, "chat", "ev3", 7, failure.SPLIT))
        self.assertEqual(len(queued), 1)
        with mock.patch("covey.app.submission.after_ack", lambda run: run()):
            submission.queue_follow_up = lambda *args: "x"
            submission.messenger = mock.Mock()
            submission.voice = SimpleNamespace(key=lambda *parts: "-".join(parts))
            event = {"chat_id": "chat", "event_id": "ev4", "action_value": {"kind": SPLIT_KIND, "topic_id": "topic",
                                                                             "job_id": 7}}
            self.assertTrue(Submission.follow_up(submission, ctx, event))
            submission.messenger.reply.assert_called_once()


if __name__ == "__main__":
    unittest.main()

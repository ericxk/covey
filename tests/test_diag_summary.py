"""复制诊断 beside 看原因: the block a failed turn's owner copies to whoever helps him (app/diag_summary.py)."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from covey.app import diag_summary
from covey.app.card_actions import ACTIONS, BOT_ONLY
from covey.app.diag_summary import DiagSummary, NOT_OWNER, summary
from covey.cards.recovery import REPORT_KIND, REPORT_LABEL, result_actions
from covey.core.jobs import job_path
from covey.core.state import write_json
from covey.core.trace import ToolTrace
from covey.core.trace_store import save_trace

TOPIC = "topic-diag"
OWNER = "ou_owner_diag"


class Voice:
    def key(self, name, suffix):
        return f"{name}:{suffix}"


def buttons(element):
    return [(column["elements"][0]["text"]["content"], column["elements"][0]["value"]["kind"])
            for column in element["columns"]]


class SummaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "jobs").mkdir()
        now = time.time()
        trace = ToolTrace()
        for index, (status, detail) in enumerate((("done", "python3 -B -m unittest test_x"), ("failed", "make deploy"))):
            trace.accept("tool", {"id": f"s{index}", "name": "bash", "status": "running", "detail": detail,
                                  "started_at": now - 60 + index})
            trace.accept("tool", {"id": f"s{index}", "name": "bash", "status": status, "detail": detail,
                                  "started_at": now - 60 + index, "ended_at": now - 50 + index})
        save_trace(self.state, "om_task", trace.snapshot())
        checkpoint = self.state / "checkpoint.json"
        checkpoint.write_text(json.dumps({"phase": "部署", "completed": ["写测试", "跑测试"], "next_step": "重新部署"}))
        write_json(job_path(self.state, "om_before"), {"key": "om_before", "job_id": 41, "topic_id": TOPIC})
        self.record = {"key": "om_task", "message_id": "om_task", "job_id": 42, "topic_id": TOPIC, "chat_id": "oc_chat",
                       "status": "failed", "execution_status": "failed", "failure_code": "root_failed",
                       "status_message": "主任务中途出错", "harness_id": "claude", "model": "claude-fable-5",
                       "effort": "xhigh", "auto_resume": 1, "retry_of": "om_before",
                       "checkpoint_path": str(checkpoint),
                       "final_text": "部署失败：curl -H 'Authorization: Bearer sk-live-1234567890abcdef' 返回 401",  # privacy: allow a fake key to be redacted
                       "timings": {"backend_requested": now - 70, "execution_finished": now - 40},
                       "created_at": "2026-11-21T02:00:00Z"}

    def test_the_block_names_version_turn_last_good_step_retries_and_the_masked_error(self):
        block = summary(self.state, self.record, bridge_release="1a2b3c4d5e6f", generation="463")
        lines = block.splitlines()
        self.assertEqual(lines[0], "Covey 诊断摘要")
        self.assertIn("版本：Bridge 1a2b3c4d5e6f（第 463 代）", lines)
        self.assertIn(f"话题：（{TOPIC}）", lines)
        self.assertIn("任务：#42 · claude · claude-fable-5 xhigh · failed / failed", lines)
        step = next(line for line in lines if line.startswith("最近成功的一步："))
        self.assertIn("跑了测试", step)  # The failed deploy after it is not the last good step.
        self.assertIn("检查点：停在 部署；已完成 2 项", lines)
        self.assertIn("重试：自动检查并继续 1 次（本轮被重启打断）；接着任务 #41 做", lines)
        self.assertTrue(any(line.startswith("原因：这一轮没做完") for line in lines))
        self.assertIn("详情：主任务中途出错；错误码 root_failed", lines)  # Its code once, in the detail.
        answer = next(line for line in lines if line.startswith("回答开头："))
        self.assertIn("部署失败：curl -H", answer)
        self.assertIn("[已隐藏]", answer)
        self.assertNotIn("sk-live-1234567890abcdef", block)  # core/text.py redact.  # privacy: allow a fake key to be redacted
        self.assertTrue(next(line for line in lines if line.startswith("时间：")).endswith("（用时 30秒）"))

    def test_a_turn_with_nothing_recorded_still_says_what_it_knows(self):
        block = summary(self.state, {"key": "om_none", "job_id": 7, "status": "interrupted"}, bridge_release="covey")
        self.assertIn("最近成功的一步：没有记下", block)
        self.assertIn("重试：没有", block)
        self.assertIn("版本：Bridge covey（第 - 代）", block)

    def test_the_runner_of_another_release_is_named(self):
        run = self.state / "runs" / "om_task"
        run.mkdir(parents=True)
        (run / "runner.json").write_text(json.dumps({"pid": 1, "release": "/absolute/releases/0ld0ld0ld0ld",
                                                     "key": "om_task", "job_id": 42}))
        block = summary(self.state, self.record, bridge_release="1a2b3c4d5e6f", generation="463")
        self.assertIn("版本：Bridge 1a2b3c4d5e6f（第 463 代）；这一轮的任务进程 0ld0ld0ld0ld", block)


class ClickTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.record = {"key": "om_task", "job_id": 42, "topic_id": TOPIC, "status": "interrupted",
                       "harness_id": "codex"}
        self.replies, self.logs = [], []
        harness = SimpleNamespace(voice=Voice(), topic_record=lambda ctx, chat, job: self.record if job == 42 else None)
        runtime = SimpleNamespace(access=SimpleNamespace(is_owner=lambda who: who == OWNER), log=self.logs.append,
                                  script_path=str(Path(__file__).resolve().parents[1] / "bin" / "covey_bridge.py"))
        messenger = SimpleNamespace(reply=lambda ctx, chat, text, key, voice=None: self.replies.append((chat, text, key)))
        self.component = DiagSummary(harness, runtime=runtime, messenger=messenger)
        self.ctx = SimpleNamespace(topic_id=TOPIC, state_dir=self.state)

    def click(self, operator=OWNER, topic=TOPIC, job=42):
        return {"event_id": "ev_1", "chat_id": "oc_chat", "operator_id": operator,
                "action_value": json.dumps({"kind": REPORT_KIND, "topic_id": topic, "job_id": job})}

    def test_the_owners_click_gets_the_block_in_the_topic_once_per_click(self):
        self.assertTrue(self.component.copy_action(self.ctx, self.click()))
        [(chat, text, key)] = self.replies
        self.assertEqual((chat, key), ("oc_chat", "diag-summary:ev_1"))
        self.assertTrue(text.startswith("📋 任务 #42 的诊断摘要"))
        self.assertIn("```text\nCovey 诊断摘要\n", text)
        self.assertTrue(text.endswith("\n```"))
        self.assertIn("diag summary job=#42 sent", self.logs)

    def test_only_the_owner_and_only_this_topics_turn(self):
        with self.assertRaisesRegex(ValueError, NOT_OWNER):
            self.component.copy_action(self.ctx, self.click(operator="ou_member"))
        self.assertTrue(self.component.copy_action(self.ctx, self.click(topic="another-topic")))
        with self.assertRaisesRegex(ValueError, "找不到这一轮任务"):
            self.component.copy_action(self.ctx, self.click(job=99))
        self.assertEqual(self.replies, [])

    def test_a_block_that_cannot_be_read_says_so(self):
        original = diag_summary.summary
        diag_summary.summary = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unreadable"))
        try:
            self.component.copy_action(self.ctx, self.click())
        finally:
            diag_summary.summary = original
        [(_, text, _)] = self.replies
        self.assertIn("诊断摘要这次没读出来", text)


class ButtonTests(unittest.TestCase):
    def test_it_comes_right_after_look_why_and_only_with_it(self):
        row = result_actions(TOPIC, 42, "检查并继续", follow_ups=("diagnose",))
        self.assertEqual(buttons(row), [("检查并继续", "bridge_job_retry"), ("看原因", "bridge_job_diagnose"),
                                        (REPORT_LABEL, REPORT_KIND)])
        self.assertEqual(row["columns"][2]["elements"][0]["value"], {"kind": REPORT_KIND, "topic_id": TOPIC, "job_id": 42})
        self.assertNotIn(REPORT_KIND, json.dumps(result_actions(TOPIC, 42, "检查并继续", follow_ups=("split",))))
        self.assertEqual(ACTIONS[REPORT_KIND].handler, "diag_summary.copy_action")
        self.assertIn(REPORT_KIND, BOT_ONLY)
        self.assertLessEqual(len(REPORT_LABEL), 5)  # docs/ui.md 6: 2 to 5 characters.


if __name__ == "__main__":
    unittest.main()

"""独立复核 (app/review.py, #20): the button on the answer card of a turn that changed code (COVEY_REVIEW=1), the owner's
click alone, the read-only topic it opens once on another vendor's model with only the owner's words, the criteria and
the turn's changes, and the helpers it picks the model with (core/model_names.py vendor_of, app/availability.py usable)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_butler_topics import CHAT, ButlerTopicsCase

from covey.app import availability, review
from covey.cards.answer_card import REVIEW_ID, REVIEW_KIND, tail
from covey.core.jobs import upsert_job_record
from covey.core.model_names import vendor_of
from covey.core.state import write_json
from covey.core.topics import load_topic

COMMIT = "951327a0c1d2e3f4a5b6c7d8e9f00112233445566"[:40]


class VendorTest(unittest.TestCase):
    def test_who_makes_a_model(self):
        self.assertEqual(vendor_of("codex", "acme/gpt-6-astra"), "openai")
        self.assertEqual(vendor_of("dsh", "ali-deepseek-v4.1-flash"), "deepseek")
        self.assertEqual(vendor_of("claude", "claude-opus-5-5"), "anthropic")
        self.assertEqual(vendor_of("agy", "gemini-4-argon_reasoning_high"), "google")
        # A Claude model through Codex's gateway is Anthropic's; its name says so, not the Harness.
        self.assertEqual(vendor_of("codex", "acme/claude-sonnet-5", "Claude Sonnet 5"), "anthropic")
        self.assertEqual(vendor_of("claude", ""), "anthropic")  # The Harness's default.
        self.assertEqual(vendor_of("codex", ""), "openai")
        self.assertEqual(vendor_of("newharness", "x"), "newharness")


class UsableTest(unittest.TestCase):
    def test_a_harness_is_usable_unless_its_preflight_or_its_last_task_says_not_now(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = SimpleNamespace(availability_health={"preflight": {"codex": {"status": "unavailable"},
                                                                         "claude": {"status": "configured"}}})
            self.assertFalse(availability.usable(runtime, state, "codex"))
            self.assertTrue(availability.usable(runtime, state, "claude"))
            self.assertTrue(availability.usable(SimpleNamespace(), state, "dsh"))  # Never sampled.
            now = time.time()
            write_json(state / "backend-health" / "claude-a.json",
                       {"backend": "claude", "model": "m", "checked_at": now - 60, "status": "failed",
                        "error": "quota_exhausted"})
            self.assertFalse(availability.usable(runtime, state, "claude", now=now))
            self.assertTrue(availability.usable(runtime, state, "claude", now=now + 1000))  # An old failure.
            write_json(state / "backend-health" / "claude-b.json",
                       {"backend": "claude", "model": "n", "checked_at": now - 10, "status": "responded", "error": ""})
            self.assertTrue(availability.usable(runtime, state, "claude", now=now))  # Answered since.
            write_json(state / "backend-health" / "claude-b.json",
                       {"backend": "claude", "model": "n", "checked_at": now - 5, "status": "failed",
                        "error": "process_exit"})
            self.assertTrue(availability.usable(runtime, state, "claude", now=now))  # Not a limit.


class ButtonTest(unittest.TestCase):
    def test_only_a_turn_that_changed_code_gets_the_button_with_the_switch_on(self):
        with tempfile.TemporaryDirectory() as directory:
            point, diff = Path(directory) / "cp.json", Path(directory) / "diff.json"
            point.write_text(json.dumps({"delivery": {"commit": COMMIT}}))
            job = SimpleNamespace(topic_id="t1", job_id=7, checkpoint_path=str(point))
            self.assertEqual(review.buttons(job), [])  # The switch is off unset.
            with mock.patch.dict(os.environ, {review.ENV: "1"}):
                built = review.buttons(job)
                self.assertEqual([element["element_id"] for element in built], [REVIEW_ID])
                button = built[0]["columns"][0]["elements"][0]
                self.assertEqual(button["text"]["content"], "独立复核")
                self.assertEqual(button["behaviors"][0]["value"], {"kind": REVIEW_KIND, "topic_id": "t1", "job_id": 7})
                # A report's turn (no commit, no files changed), a topic on a device, a job outside a topic: none.
                point.write_text(json.dumps({"delivery": {"verified": "报告发了"}}))
                self.assertEqual(review.buttons(job), [])
                diff.write_text("{}")
                job.diff_json_path = str(diff)  # The Bridge saw it change files.
                self.assertEqual(len(review.buttons(job)), 1)
                self.assertEqual(review.buttons(SimpleNamespace(**{**job.__dict__, "device": "box"})), [])
                self.assertEqual(review.buttons(SimpleNamespace(**{**job.__dict__, "topic_id": ""})), [])
            # After the 输入/输出 box (docs/ui.md 5.2: buttons last).
            shown = tail(None, ("Claude Code",), ("用时 1分",), buttons=built)
            self.assertEqual([element.get("element_id") for element in shown], ["answer_facts", REVIEW_ID])


class OpenTest(ButlerTopicsCase):
    def setUp(self):
        super().setUp()
        self.app.messenger.react = mock.Mock(return_value="")
        answer = self.start("src", harness="claude", title="改看板",
                            criteria=["全量测试通过", "合进 main"])
        self.assertTrue(answer["ok"], answer)
        self.source = answer["data"]["topic_id"]
        self.label = answer["data"]["label"]
        job = self.job("src")
        self.app.runtime.unregister_reply_job(job)
        state = self.ctx.state_dir
        point = state / "cp-src.json"
        point.write_text(json.dumps({"delivery": {"commit": COMMIT, "verified": "我自己测过了，都好"}}, ensure_ascii=False))
        diff = state / "job-diffs" / "src" / "diff.md"
        diff.parent.mkdir(parents=True)
        diff.write_text("# Job Diff\n## M board.py\n```diff\n-old\n+new\n```\n")
        upsert_job_record(state, job.message_id, status="done", checkpoint_path=str(point), diff_artifact_path=str(diff))
        self.job_id = job.job_id
        self.raw = {"kind": REVIEW_KIND, "topic_id": self.source, "job_id": self.job_id}
        self.click = {"chat_id": CHAT, "event_id": "ev-1", "message_id": "om_answer", "action_value": self.raw}

    def opened(self):
        token = review.token_of(self.source, self.job_id)
        return self.app.runtime.jobs.find(0, "review-" + token), json.loads(review.record_path(self.ctx.state_dir, token).read_text())

    def test_the_owners_click_opens_one_read_only_topic_on_another_vendors_model(self):
        cards = self.app.messenger.create_and_send_card.call_count
        self.app.review.open(self.ctx, self.click, self.raw)
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, cards + 1)  # Its topic card, top level.
        sent = self.app.messenger.create_and_send_card.call_args
        self.assertEqual(sent.args[1], CHAT)
        self.assertIn(f"独立复核：{self.label} 改看板", json.dumps(sent.args[2], ensure_ascii=False))
        job, record = self.opened()
        topic = load_topic(self.ctx.state_dir, record["topic_id"])
        source = load_topic(self.ctx.state_dir, self.source)
        self.assertNotEqual(topic["harness_id"], "claude")  # Another vendor than the turn's Claude.
        self.assertNotEqual(vendor_of(topic["harness_id"], topic["model"]), "anthropic")
        self.assertEqual((topic["workdir"], topic["workspace_name"]), (source["workdir"], source["workspace_name"]))
        self.assertEqual(topic["origin"]["kind"], "review")
        self.assertTrue(topic["origin"]["readonly"])
        self.assertEqual(topic["origin"]["review_of"], {"topic_id": self.source, "job_id": self.job_id})
        self.assertEqual(topic["criteria"], ["全量测试通过", "合进 main"])
        self.assertNotIn("forked_from", topic)  # Not the source's session.
        prompt = job.prompt_text
        self.assertIn("只读复核", prompt)
        self.assertIn("> 看看昨晚的失败", prompt)  # The owner's words in the reviewed topic.
        self.assertIn(f"提交 {COMMIT}", prompt)
        self.assertIn("diff.md", prompt)
        self.assertNotIn("我自己测过了", prompt)  # Not the implementer's own account.
        self.assertTrue((self.ctx.state_dir / "topics" / record["topic_id"] / "incoming_media" / "diff.md").is_file())
        self.assertEqual(job.user_text, f"独立复核 {self.label} 任务 #{self.job_id} 的改动")
        self.assertTrue(any(text.startswith("已开 T") and "只读" in text for text in self.said()))
        # Clicked again: nothing more opens, the topic it opened is named.
        self.app.review.open(self.ctx, {**self.click, "event_id": "ev-2"}, self.raw)
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, cards + 1)
        self.assertIn("这一轮已经开过独立复核", self.said()[-1])

    def test_only_the_owner_and_only_an_ended_turn(self):
        with mock.patch.object(self.app.review, "submit") as submit:
            self.assertEqual(self.app.review.claim(self.ctx, self.click, self.raw, False), "owner_only")
            submit.assert_not_called()
            self.assertEqual(self.app.review.claim(self.ctx, self.click, self.raw, True), "review")
            submit.assert_called_once()
            self.assertIsNone(self.app.review.claim(self.ctx, self.click, {"kind": "bridge_job_stop"}, True))
        other = self.start("src2", harness="claude", title="还在跑")["data"]  # Its first turn is still queued.
        cards = self.app.messenger.create_and_send_card.call_count
        raw = {"kind": REVIEW_KIND, "topic_id": other["topic_id"], "job_id": self.job("src2").job_id}
        self.app.review.open(self.ctx, {**self.click, "action_value": raw}, raw)
        self.assertEqual(self.app.messenger.create_and_send_card.call_count, cards)
        self.assertIn("没开独立复核：这一轮还没结束", self.said()[-1])

    def test_a_redacting_topic_reviews_only_on_a_model_that_redacts(self):
        from covey.core.redaction import TOPIC_KEY
        path = self.ctx.state_dir / "topics" / self.source / "topic.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, TOPIC_KEY: True}))
        with mock.patch("covey.app.launch_models.backend_capabilities",
                        side_effect=lambda backend, ctx: SimpleNamespace(redaction=False)):
            self.app.review.open(self.ctx, self.click, self.raw)
        self.assertIn("没开独立复核：现在没有能用的模型", self.said()[-1])


if __name__ == "__main__":
    unittest.main()

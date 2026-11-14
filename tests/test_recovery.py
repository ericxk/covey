"""The recovery decision (core/recovery.py): the card's state and button and what 检查并继续 does, as one pure
function; the divergences between the button and the click are kept on purpose."""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from covey.app.task_controls import TaskControls
from covey.core import recovery
from covey.core.recovery import classify

SAVED = {"result_retry_policy": "saved_delivery_only", "verified_reply_state": "authorized",
         "verified_reply_authorized": True, "backend": "codex"}


class ClassifyTests(unittest.TestCase):
    def kind(self, record, saved=False, native=False):
        decided = classify(record, saved=saved, native_live_question=native)
        return decided.kind, decided.after

    def test_label_and_card_do_not_need_the_files(self):
        decided = classify({"delivery_ok": False, "final_text": "t"})
        self.assertEqual((decided.kind, decided.label, decided.card), ("", recovery.REDELIVER_LABEL, recovery.INTERRUPTED))
        self.assertEqual(classify({}).label, recovery.RETRY_LABEL)
        self.assertEqual(classify({"runner_start_failed": True, "policy_stop": True}).card, recovery.START_FAILED)
        self.assertEqual(classify({"policy_stop": True}).card, recovery.POLICY_STOP)
        self.assertEqual([classify({**SAVED, **extra}).card for extra in
                          ({"verified_reply_authorized": 1}, {"delivery_ok": True}, {})],
                         [recovery.SAVED_FAILED, recovery.SAVED_DELIVERED, recovery.SAVED_PENDING])

    def test_kinds_in_the_click_order(self):
        self.assertEqual(self.kind({"policy_guard_requested": True, "policy_stop": True}), (recovery.GUARDED, ""))
        self.assertEqual(self.kind({"policy_stop": True, "delivery_ok": False, "final_text": "t"}),
                         (recovery.REDELIVER_FAILED, ""))
        self.assertEqual(self.kind({"policy_stop": True}), (recovery.POLICY_STOPPED, ""))
        self.assertEqual(self.kind({"paused_question_delivery": {}}, native=True), (recovery.NATIVE_LOST, ""))
        self.assertEqual(self.kind({"paused_question_delivery": {}}, saved=True, native=True),
                         (recovery.REDELIVER_QUESTION, ""))
        self.assertEqual(self.kind({"result_retry_policy": "native_answer_once"}), (recovery.ANSWERED_ONCE, ""))
        self.assertEqual(self.kind({}, saved=True), (recovery.REDELIVER_SAVED, ""))
        question = {"execution_status": "waiting_user", "checkpoint_question_deferred": True,
                    "native_resume_allowed": True}
        self.assertEqual(self.kind(question, saved=True), (recovery.SAVED_QUESTION, recovery.REQUEUE))
        self.assertEqual(self.kind({**question, "checkpoint_question_stopped": True}, saved=True),
                         (recovery.SAVED_QUESTION, recovery.WAIT_CANCELLED))
        self.assertEqual(self.kind({"question_delivery_ok": False, "final_text": "t"}), (recovery.REDELIVER_FAILED, ""))
        self.assertEqual(self.kind({}), (recovery.REQUEUE, ""))

    def test_native_lookup_runs_only_when_the_decision_reaches_it(self):
        asked = []

        def live():
            asked.append(1)
            return True
        for record, saved in (({"policy_guard_requested": True}, False), ({"policy_stop": True}, False), ({}, True)):
            self.assertNotEqual(classify(record, saved=saved, native_live_question=live).kind, recovery.NATIVE_LOST)
        self.assertEqual(asked, [])
        self.assertEqual(classify({}, saved=False, native_live_question=live).kind, recovery.NATIVE_LOST)
        self.assertEqual(asked, [1])

    def test_saved_lookup_runs_after_the_policy_checks_and_once(self):
        asked = []

        def saved():
            asked.append(1)
            return True
        for record in ({"policy_guard_requested": True}, {"policy_stop": True}):
            classify(record, saved=saved, native_live_question=True)
        self.assertEqual(asked, [])
        self.assertEqual(classify({}, saved=saved, native_live_question=True).kind, recovery.REDELIVER_SAVED)
        self.assertEqual(asked, [1])

    def test_a_policy_stop_click_does_not_read_the_saved_results(self):
        # A symlinked verified-results directory makes has_saved_reply raise; the policy refusals come first.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "real").mkdir()
            os.symlink(state / "real", state / "verified-results")
            ctx = types.SimpleNamespace(topic_id="topic", state_dir=state)
            for extra, text in (({"policy_guard_requested": True}, "停止通知由后台重投"),
                                ({"policy_stop": True}, "不会重跑原请求")):
                record = {"status": "failed", "job_id": 7, "key": "k7", **extra}
                controls = types.SimpleNamespace(
                    runtime=types.SimpleNamespace(jobs=types.SimpleNamespace(topic_jobs=lambda topic, chat: [])),
                    harness=types.SimpleNamespace(topic_records=lambda c, chat, r=record: [r]))
                self.assertIn(text, TaskControls._retry_topic(controls, ctx, "chat", "message"))

    def test_known_divergences_are_kept(self):
        # The button reads the record's fields only; the click also reads the saved reply's index file.
        self.assertEqual((classify({}).label, self.kind({}, saved=True)[0]), (recovery.RETRY_LABEL, recovery.REDELIVER_SAVED))
        # A delivery that needs reconciling draws no button, but an older card's click or an automatic retry
        # redelivers the saved reply.
        for error in ("reconciliation_required", "idempotency_conflict"):
            record = {**SAVED, "delivery_error": error}
            self.assertEqual((classify(record).label, self.kind(record, saved=True)[0]), ("", recovery.REDELIVER_SAVED))
        # A policy stop whose question form was not delivered shows the button; the click refuses.
        record = {"policy_stop": True, "question_delivery_ok": False, "final_text": "t"}
        self.assertEqual((classify(record).label, self.kind(record)[0]), (recovery.REDELIVER_LABEL, recovery.POLICY_STOPPED))
        # An empty paused question counts as absent for the button; the click redelivers it.
        record = {"paused_question_delivery": {}}
        self.assertEqual((classify(record).label, self.kind(record)[0]), (recovery.RETRY_LABEL, recovery.REDELIVER_QUESTION))


if __name__ == "__main__":
    unittest.main()

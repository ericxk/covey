"""Answer submission (app/submission.py, app/questions.py): a duplicate of the same event cannot mark the original
uncertain."""
from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.world import QuestionFixture
from covey.core.state import write_private_text
from covey.core.store import transition_question


class SubmissionIsolationTest(QuestionFixture, unittest.TestCase):
    def test_duplicate_same_event_cannot_mark_original_uncertain(self):
        f = self
        entered, release = threading.Event(), threading.Event()
        f.rpc.answer.side_effect = lambda *a: (entered.set(), release.wait(3))
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(f.app.questions.submit_answer, f.ctx_topic, 'oc_chat', f.token, {'answer_0':'A'}, 'same')
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(ValueError):
                    f.app.questions.submit_answer(f.ctx_topic, 'oc_chat', f.token, {'answer_0':'A'}, 'same')
                self.assertEqual(json.loads(f.path.read_text())['status'], 'submitting')
            finally:
                release.set()
            future.result(2)
        self.assertEqual(f.rpc.answer.call_count, 1)

    def test_slow_answer_does_not_block_other_topic(self):
        f = self
        f.receive('other-topic', 'another task')
        other = f.app.runtime.jobs.queue.get_nowait()
        ctx = f.topic('other-topic')
        other.checkpoint_path = str(f.root / 'other-checkpoint.json')
        Path(other.checkpoint_path).write_text('{}')
        control = SimpleNamespace(user_requests={18}, answer=mock.Mock(), steer=mock.Mock())
        other.backend_control = control
        f.app.runtime.jobs.register(other)
        token = 'b' * 16
        row = {**f.record, 'token':token, 'topic_id':ctx.topic_id, 'job_id':other.job_id, 'request_id':18}
        write_private_text(f.path.parent / (token + '.json'), json.dumps(row))
        entered, release = threading.Event(), threading.Event()
        f.rpc.answer.side_effect = lambda *a: (entered.set(), release.wait(3))
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(f.app.questions.submit_answer, f.ctx_topic, 'oc_chat', f.token, {'answer_0':'A'}, 'a')
            try:
                self.assertTrue(entered.wait(1))
                b = pool.submit(f.app.questions.submit_answer, ctx, 'oc_chat', token, {'answer_0':'B'}, 'b')
                b.result(1)
                control.answer.assert_called_once()
            finally:
                release.set()
            a.result(2)

    def test_ack_during_network_write_survives_commit(self):
        f = self
        def ack(*args):
            f.app.interaction.structured_harness_event(f.ctx_topic, f.job, 'question_resolved', {'requestId':17}, f.rpc)
        f.rpc.answer.side_effect = ack
        f.app.questions.submit_answer(f.ctx_topic, 'oc_chat', f.token, {'answer_0':'A'}, 'ack')
        record = json.loads(f.path.read_text())
        self.assertEqual((record['status'], record['delivery_state']), ('answered', 'delivered'))
        if hasattr(f.job, 'card_updates'):
            f.job.card_updates.close()

    def test_cancelled_question_rejects_late_answer(self):
        f = self
        transition_question(f.path, {'pending'}, status='cancelled')
        with self.assertRaises(ValueError):
            f.app.questions.submit_answer(f.ctx_topic, 'oc_chat', f.token, {'answer_0':'A'}, 'late')
        f.rpc.answer.assert_not_called()

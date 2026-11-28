"""Task-card writes (app/task_card.py, app/cards.py): one slow card does not hold another, failed writes are
counted, a replacement card keeps the first card's facts."""
from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.fake_lark import FakeCardKit
from covey.feishu.messenger import Messenger
from covey.core.jobs import BridgeReplyJob, find_job_record, record_bridge_job_state
from covey.app.task_card import StreamingCard, StreamingReply


class CardIsolationTest(unittest.TestCase):
    def test_slow_card_does_not_block_other_card(self):
        runtime = StreamingCard('test', 'test', 'answer', StreamingReply, lambda *a: None, lambda x: x,
                                mock.Mock(return_value=True), lambda s, n: s, Messenger(lambda *a: None),
                                lambda *a, **k: (None, False), card_limit=10000, update_interval=0)
        def progress(cid):
            return SimpleNamespace(card_id=cid, message_id=cid, element_id='answer', sequence=0,
                disabled=False, last_content='', last_update_at=0, update_failures=0)
        a, b = progress('a'), progress('b')
        entered, release = threading.Event(), threading.Event()
        def send(profile, method, url, *args, **kwargs):
            if '/cards/a/' in url:
                entered.set()
                release.wait(3)
            return {'ok':True}
        with FakeCardKit(side_effect=send), ThreadPoolExecutor(2) as pool:
            first = pool.submit(runtime.update, None, a, 'A')
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(pool.submit(runtime.update, None, b, 'B').result(1))
            finally:
                release.set()
            first.result(2)


class CardFailureCountTest(unittest.TestCase):
    """The job record keeps every failed card write, though a later success resets the
    consecutive count that disables a card."""

    def test_record_counts_failures_across_successes_and_replacement_cards(self):
        runtime = StreamingCard('test', 'test', 'answer', StreamingReply, lambda *a: None, lambda x: x,
                                mock.Mock(return_value=True), lambda s, n: s, Messenger(lambda *a: None),
                                lambda *a, **k: (None, False), card_limit=10000, update_interval=0)
        job = BridgeReplyJob(job_id=1, user_text='x', chat_id='c', message_id='m', timeout=0,
                             progress=StreamingReply('card', 'm'))
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)

            def record():
                record_bridge_job_state(state_dir=state, backend='claude', job=job, status='running')
                return find_job_record(state, 'm')['card_update_failures']

            with FakeCardKit(side_effect=[None, {'ok': True}, None]):
                for content in ('A', 'B', 'C'):
                    runtime.update(None, job.progress, content, force=True)
            self.assertEqual(job.progress.update_failures, 1)
            self.assertEqual(record(), 2)
            with mock.patch.object(StreamingCard, 'create', return_value=StreamingReply('card2', 'm2')):
                self.assertTrue(runtime.replace_card(None, job, 'D', 'failed', 'test'))
            self.assertEqual(job.progress.card_id, 'card2')
            self.assertEqual(record(), 2)
            # Past the replacement limit the job has no card; the saved total stays.
            self.assertFalse(runtime.replace_card(None, job, 'E', 'failed', 'test'))
            self.assertIsNone(job.progress)
            self.assertEqual(record(), 2)


class ReplacementCardTest(unittest.TestCase):
    """A replacement card is drawn from a copy of the job: the copy keeps what the first card was drawn
    with (app/cards.py create_streaming_reply), not the declared defaults or no attribute at all."""

    def test_a_replacement_card_keeps_the_first_cards_facts(self):
        runtime = StreamingCard('test', 'test', 'answer', StreamingReply, lambda *a: None, lambda x: x,
                                mock.Mock(return_value=True), lambda s, n: s, Messenger(lambda *a: None),
                                lambda *a, **k: (None, False), card_limit=10000, update_interval=0)
        job = BridgeReplyJob(job_id=1, user_text='x', chat_id='c', message_id='m', timeout=0,
                             progress=StreamingReply('card', 'm'))
        facts = {'harness_capabilities': SimpleNamespace(steering=True), 'topic_label': 'T7',
                 'model_label': 'Claude · Opus 5.5', 'workspace_label': 'repo'}
        for name, value in facts.items():
            setattr(job, name, value)
        drawn = []
        with mock.patch.object(StreamingCard, 'create',
                               side_effect=lambda profile, copy, content: drawn.append(copy) or StreamingReply('card2', 'm2')):
            self.assertTrue(runtime.replace_card(None, job, 'D', 'failed', 'test'))
        self.assertTrue(drawn[0].message_id.startswith('m-replacement-'))
        self.assertEqual({name: getattr(drawn[0], name, None) for name in facts}, facts)
        # A job never shown with them gets none: the card's own defaults stay.
        bare = BridgeReplyJob(job_id=2, user_text='x', chat_id='c', message_id='n', timeout=0,
                              progress=StreamingReply('card', 'n'))
        with mock.patch.object(StreamingCard, 'create',
                               side_effect=lambda profile, copy, content: drawn.append(copy) or StreamingReply('card3', 'n2')):
            self.assertTrue(runtime.replace_card(None, bare, 'D', 'failed', 'test'))
        self.assertFalse(any(hasattr(drawn[1], name) for name in ('workspace_label', 'input_parts')))

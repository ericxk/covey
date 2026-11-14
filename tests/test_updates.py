"""Coalesced card updates (core/updates.py), and a long trace that keeps its running tool through a snapshot."""
import json
import threading
import unittest
from unittest import mock

from covey.core.trace import ToolTrace
from covey.core.updates import CoalescedUpdates
from support.cards import trace_panel


class UpdateTest(unittest.TestCase):
    def test_updates_coalesce_without_waiting_for_blocked_network(self):
        entered, release = threading.Event(), threading.Event()
        updates = CoalescedUpdates(lambda _: None, interval=0.05)
        seen = []
        def slow():
            entered.set()
            release.wait(2)
        updates.submit('trace', slow, urgent=True)
        self.assertTrue(entered.wait(1))
        for n in range(50):
            updates.submit('trace', lambda n=n: seen.append(n))
        release.set()
        self.assertTrue(updates.close(2))
        self.assertEqual(seen, [49])

    def test_failed_update_does_not_prevent_final_update(self):
        log = mock.Mock()
        updates = CoalescedUpdates(log)
        updates.submit('bad', lambda: 1/0)
        seen=[]
        updates.submit('final', lambda: seen.append('done'))
        self.assertTrue(updates.close())
        self.assertEqual(seen,['done'])
        log.assert_called_once()

    def test_full_trace_retains_active_tool_after_forty_notes(self):
        trace = ToolTrace()
        for n in range(100): trace.note(str(n), '说明'+str(n))
        trace.accept('tool',{'id':'active','name':'bash','detail':'sleep 10','status':'running','started_at':1000})
        self.assertIn('active',trace.steps)
        trace.finish()
        trace.output_tokens=100
        restored=ToolTrace.restore(json.loads(json.dumps(trace.snapshot(), sort_keys=True)))
        self.assertEqual(restored.snapshot(),trace.snapshot())
        self.assertEqual(trace_panel(restored, False),trace_panel(trace, False))

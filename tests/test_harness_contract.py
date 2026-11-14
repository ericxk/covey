"""The backend contract (core/harness.py RunRequest): an explicit request without option callbacks, live tool events
that survive replay, delivery repair without a rerun."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from covey.core.trace import ToolTrace
from covey.core.jobs import upsert_job_record


class RuntimeContractTest(unittest.TestCase):
    def test_new_backend_uses_explicit_request_without_options_callbacks(self):
        from covey.core.harness import RunCallbacks, RunInputs, RunRequest, RunResult
        events=[]
        class FakeBackend:
            def run(self, request):
                assert request.inputs.attachments == ({'path':'/cache/p.png','mime':'image/png'},)
                request.callbacks.session('session-1')
                request.callbacks.event('token_usage',{'output_tokens':3},None)
                return RunResult('done','session-1')
        sessions=[]
        request=RunRequest(SimpleNamespace(options={}), 'task',
            callbacks=RunCallbacks(event=lambda k,d,c:events.append((k,d)), session=sessions.append),
            inputs=RunInputs(threading.Event(), '/checkpoint', ({'path':'/cache/p.png','mime':'image/png'},)))
        self.assertEqual(FakeBackend().run(request).text,'done')
        self.assertEqual(sessions,['session-1'])
        self.assertEqual(events,[('token_usage',{'output_tokens':3})])

    def test_sanitized_live_tool_events_survive_replay(self):
        rows=json.loads((Path(__file__).parent/'fixtures/harness/codex-live-tools.json').read_text())['events']
        trace=ToolTrace()
        for kind,data in rows+rows:
            trace.accept(kind,data)
        self.assertTrue(trace.steps)
        self.assertTrue(all(step['status']=='done' for step in trace.steps.values()))
        self.assertEqual(trace.total,len({d['id'] for k,d in rows if k=='tool'}))

    def test_delivery_repair_does_not_rerun_execution(self):
        with tempfile.TemporaryDirectory() as root:
            state=Path(root)
            upsert_job_record(state,'job',status='failed',execution_status='done',final_text='answer',delivery_ok=False)
            row=upsert_job_record(state,'job',status='done',delivery_ok=True)
            self.assertEqual(row['status'],'done')
            self.assertEqual(row['final_text'],'answer')

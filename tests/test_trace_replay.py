"""The execution trace replayed (core/trace.py ToolTrace snapshot and restore, backends/codex/events.py): what a
replay draws matches the live trace."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from support.cards import panel_title, trace_panel, walk_dicts
from covey.core.trace import ToolTrace
from covey.backends.codex.events import CodexEventAdapter
from covey.cards.streaming import usage_lines


class TraceReplayTest(unittest.TestCase):
    def test_snapshot_and_render_are_independent(self):
        trace = ToolTrace(); trace.started_at = 1
        trace.note('n', '进展\n' + '说明'*4000, received_at=2)
        trace.output_tokens = 123
        trace.ended_at = 4
        snapshot = trace.snapshot()
        replay = ToolTrace.restore(snapshot)
        expected = trace_panel(replay, False)
        trace.note('later','later',received_at=5)
        self.assertEqual(expected, trace_panel(ToolTrace.restore(snapshot), False))
        # Ended: the title is the rows; the tokens are the token tile's and 任务详情's (2026-11-04 15:07).
        self.assertEqual(panel_title(expected), '执行过程 · 1 步')
        self.assertEqual(usage_lines(snapshot), [])  # The 输入/输出 box's since 2026-11-09, without subtasks.
        # One page (2026-11-04): a note longer than a card keeps its newest part whole, its start in the fold.
        body = ''.join(e['content'] for e in walk_dicts(expected['elements']) if e.get('tag') == 'markdown')
        self.assertIn('说明' * 600, body)
        self.assertGreater(body.count('说明'), 2500)  # Most of it: each block's text_size (12 px since 2026-11-04) costs a little.
        fold = next(e for e in expected['elements'] if e.get('element_id') == 'trace_older')
        self.assertIn('进展 说明', str(fold))

    def test_nested_agent_event_does_not_reparent_its_parent(self):
        events=[]
        adapter=CodexEventAdapter(lambda kind,data:events.append((kind,data.copy())))
        adapter.agents({'receiverThreadIds':['parent'], 'agentsStates':{'parent':{'status':'running'}}},1)
        adapter.agents({'receiverThreadIds':['child'], 'agentsStates':{'child':{'status':'running'}}},2,'parent')
        latest=events[-1][1]
        self.assertEqual((latest['id'],latest['parent_id']),('child','parent'))
        self.assertEqual(len([kind for kind,_ in events if kind=='agent_state']),2)

    def test_tool_completion_then_replayed_start_is_still_complete(self):
        trace=ToolTrace()
        adapter=CodexEventAdapter(lambda kind,data:trace.apply_normalized(data))
        adapter.item('item/completed',{'id':'t','type':'commandExecution','command':'pwd','exitCode':0,'durationMs':1000},2)
        adapter.item('item/started',{'id':'t','type':'commandExecution','command':'pwd'},3)
        self.assertEqual(trace.total,1)
        self.assertEqual(trace.steps['t']['status'],'done')
        self.assertEqual(trace.steps['t']['elapsed'],1)

    def test_public_protocol_fixture_replays_without_backend_or_ui(self):
        events=json.loads((Path(__file__).parent/'fixtures/harness/public-events.json').read_text())['events']
        trace=ToolTrace(); trace.started_at=0
        for kind,data in events:
            trace.accept(kind,data)
        trace.ended_at=8
        snapshot=trace.snapshot()
        self.assertEqual(snapshot['output_tokens'],321)
        self.assertEqual(snapshot['steps']['t1']['status'],'done')
        self.assertEqual(snapshot['children']['a']['children']['b']['steps']['t2']['status'],'failed')
        self.assertEqual(trace_panel(ToolTrace.restore(snapshot), False),trace_panel(trace, False))
        self.assertIn('先读取配置',json.dumps(trace_panel(trace, False),ensure_ascii=False))

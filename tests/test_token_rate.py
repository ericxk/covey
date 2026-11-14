import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from support import make_job
from support.records import saved, state_dir
from support.cards import figures_line, panel_title, trace_panel
from covey.core.trace import ToolTrace
from covey.core.usage import ActiveGenerationClock, rate
from covey.cards.streaming import usage_lines
from covey.app.interaction import Interaction
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.progress import CAPABILITY, ProgressReporter
from covey_internal.maso.bridge import MasoEvents, MasoProtocolError


class TokenRateTests(unittest.TestCase):
    def test_overlap_and_user_wait_excluded_once(self):
        clock = ActiveGenerationClock(100)
        clock.block('tool-a', 110)
        clock.block('tool-b', 115)
        clock.unblock('tool-a', 120)
        clock.unblock('tool-b', 130)
        clock.block('question', 140)
        clock.unblock('question', 940)
        self.assertEqual(clock.sample(950), 30)
        self.assertEqual(clock.sample(945), 30)
        self.assertEqual(rate(1200, clock.seconds), 40)

    def test_zero_and_missing_are_distinct(self):
        self.assertEqual(rate(0, 10), 0)
        for tokens, seconds in [(True, 1), (-1, 1), (1, 0), (None, 10), (1, float('nan')),
                                (1, float('inf')), (1, -1)]:
            self.assertIsNone(rate(tokens, seconds))

    def test_metric_freezes_and_survives_snapshot_without_any_tools(self):
        trace = ToolTrace()
        trace.started_at = 100
        trace.accept('token_usage', {'output_tokens': 1200, 'measured_output_tokens': 1200,
                                     'generation_seconds': 30})
        with patch('time.time', return_value=999):
            trace.finish()
        restored = ToolTrace.restore(json.loads(json.dumps(trace.snapshot())))
        for now in [2000, 20000]:
            with patch('time.time', return_value=now):
                # Ended: the title is the rows; the speed is 任务详情's (2026-11-04 15:07).
                self.assertEqual(panel_title(trace_panel(restored, False)), '执行过程 · 无执行记录')
                rows = '\n'.join(usage_lines(restored.snapshot()))
                self.assertIn('平均 40.0 token/s', figures_line(restored.snapshot()))
                self.assertEqual(rows, '')  # The 输入/输出 box has the counts since 2026-11-09.
                self.assertNotIn('只算', figures_line(restored.snapshot()))  # The rate counts all of 输出.
        self.assertFalse(restored.accept('token_usage', {'output_tokens': 100}))
        self.assertEqual(restored.output_tokens, 1200)

    def test_a_rate_over_part_of_the_output_names_the_output_it_counts(self):
        # Codex: its requests while a command ran added output but no generation time (MeasuredOutput).
        trace = ToolTrace()
        trace.started_at = 100
        trace.accept('token_usage', {'output_tokens': 12345, 'measured_output_tokens': 1236,
                                     'generation_seconds': 30})
        self.assertIn('模型生成 30.0 秒 · 平均 41.2 token/s（只算模型生成计时内的 1,236 token）', figures_line(trace.snapshot()))
        self.assertNotIn('token/s', panel_title(trace_panel(trace, True)))  # 任务详情 has it (2026-11-04 15:07).
        trace.finish()
        self.assertNotIn('token/s', panel_title(trace_panel(trace, False)))

    def test_old_snapshot_does_not_divide_by_whole_task_wall_time(self):
        trace = ToolTrace.restore({'started_at': 100, 'ended_at': 1100, 'output_tokens': 1000})
        self.assertNotIn('token/s', str(trace_panel(trace, False)))
        self.assertNotIn('平均 1.0', str(trace_panel(trace, False)))

    def test_numeric_event_creates_trace_and_schedules_live_refresh(self):
        queued = []
        app = NS(cards=NS(queue_trace=lambda *a: queued.append(a)))
        job = make_job(message_id='job', timings={'backend_requested': 10})
        state = state_dir(self)
        Interaction._dispatch_structured_event(app, NS(state_dir=state), job, 'token_usage',
            {'output_tokens': 300, 'measured_output_tokens': 300, 'generation_seconds': 10}, None)
        self.assertEqual(len(queued), 1)
        self.assertEqual(saved(state, 'job')['generation_seconds'], 10)
        self.assertIn('30.0 token/s', figures_line(job.tool_trace.snapshot()))

    def test_maso_native_usage_excludes_gap_and_deduplicates_messages(self):
        now, events = [100], []
        reporter = ProgressReporter('run', 'root', '.', lambda kind, **data: events.append((kind, data)),
                                    clock=lambda: now[0])
        ctx = NS(agent_id='root')
        reporter.request(ctx)
        now[0] = 110
        reporter.usage(ctx, NS(id='a', role='assistant', usage={'completion_tokens': 200}))
        now[0] = 900  # external tool/user waiting is outside the next request.
        reporter.request(ctx)
        reporter.usage(ctx, NS(id='a', role='assistant', usage={'completion_tokens': 200}))
        now[0] = 920
        reporter.usage(ctx, NS(id='b', role='assistant', usage={'completion_tokens': 400}))
        reporter.usage(NS(agent_id='child'), NS(id='c', role='assistant', usage={'completion_tokens': 999}))
        # The first request also says the model call started (S30: 正在启动 MASO… until then).
        self.assertEqual([kind for kind, _ in events], ['model_request', 'token_usage', 'token_usage'])
        self.assertEqual(events[-1][1]['generation_seconds'], 30)
        self.assertEqual(events[-1][1]['output_tokens'], 600)
        received = []
        consumer = MasoEvents('run', 'root', NS(event=lambda *args: received.append(args)), capabilities=[CAPABILITY])
        consumer.accept(json.dumps({'version': 1, 'run_id': 'run', 'type': 'session',
                                    'session_id': 'root', 'capabilities': [CAPABILITY], 'progress_sequence': True}))
        for kind, data in events:
            envelope = {'version': 1, 'run_id': 'run', 'type': kind, **data}
            consumer.accept(json.dumps(envelope))
            consumer.accept(json.dumps(envelope))
        self.assertEqual([args[0] for args in received], ['model_request', 'token_usage', 'token_usage'])
        bad = {**envelope, 'sequence': 4, 'id': 'different-root'}
        with self.assertRaises(MasoProtocolError):
            consumer.accept(json.dumps(bad))


if __name__ == '__main__':
    unittest.main()

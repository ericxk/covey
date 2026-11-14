import json
import unittest
from types import SimpleNamespace as NS
from support import make_job
from support.records import saved, state_dir
from covey.backends.codex.usage import CodexTurnUsage
from support.cards import figures_line, trace_panel
from covey.app.interaction import Interaction
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.progress import ProgressReporter
from covey.core.trace import ToolTrace

class InputTokensTest(unittest.TestCase):
    def test_codex_resumed_turn_deltas_dedup_and_late_foreign_events(self):
        usage=CodexTurnUsage()
        def sample(i,o,li,lo):
            return {'total':{'inputTokens':i,'outputTokens':o},'last':{'inputTokens':li,'outputTokens':lo}}
        self.assertIsNone(usage.update(sample(2000,500,500,100),False))
        self.assertEqual(usage.update(sample(3200,600,1200,100),True),{'input_tokens':1200,'output_tokens':100})
        self.assertIsNone(usage.update(sample(3200,600,1200,100),True))
        self.assertIsNone(usage.update(sample(9999,9999,100,100),False))
        self.assertEqual(usage.update(sample(5000,900,1800,300),True),{'input_tokens':3000,'output_tokens':400})
        self.assertIsNone(usage.update(sample(3200,600,1200,100),True))

    def test_codex_first_update_infers_baseline_once_and_missing_is_unknown(self):
        usage=CodexTurnUsage()
        self.assertEqual(usage.update({'total':{'inputTokens':9000,'outputTokens':200},
            'last':{'inputTokens':4000,'outputTokens':30}},True),{'input_tokens':4000,'output_tokens':30})
        self.assertEqual(usage.update({'total':{'inputTokens':14000,'outputTokens':240},
            'last':{'inputTokens':5000,'outputTokens':40}},True),{'input_tokens':9000,'output_tokens':70})
        usage=CodexTurnUsage()
        self.assertEqual(usage.update({'total':{'outputTokens':20},'last':{'outputTokens':20}},True),
            {'input_tokens':None,'output_tokens':20})
        self.assertEqual(usage.update({'total':{'inputTokens':1000,'outputTokens':40},
            'last':{'inputTokens':500,'outputTokens':20}},True),{'input_tokens':None,'output_tokens':40})

    def test_maso_sums_prompt_usage_once_for_root_and_does_not_add_cache_twice(self):
        events=[]
        reporter=ProgressReporter('run','root','.',lambda kind,**data:events.append(data))
        def response(id,inputs):return NS(id=id,role='assistant',usage={'prompt_tokens':inputs,'completion_tokens':20,'cached_tokens':300})
        reporter.usage(NS(agent_id='root'),response('a',1000))
        reporter.usage(NS(agent_id='root'),response('a',1000))
        reporter.usage(NS(agent_id='child'),response('child',9999))
        reporter.usage(NS(agent_id='root'),response('b',2000))
        self.assertEqual(events[-1]['input_tokens'],3000)
        reporter.usage(NS(agent_id='root'),response('c',None))
        self.assertIsNone(events[-1]['input_tokens'])
        reporter.usage(NS(agent_id='root'),response('d',500))
        self.assertIsNone(events[-1]['input_tokens'])

    def test_input_survives_dispatch_snapshot_and_is_displayed_only_once(self):
        app = NS(cards=NS(queue_trace=lambda *a:None))
        job=make_job(message_id='job',timings={})
        state=state_dir(self)
        Interaction._dispatch_structured_event(app,NS(state_dir=state),job,'token_usage',
            {'input_tokens':12345,'output_tokens':600,'measured_output_tokens':600,'generation_seconds':20},None)
        self.assertEqual(saved(state,'job')['input_tokens'],12345)
        self.assertEqual(saved(state,'job')['input_tokens_scope'],'parent_turn')
        trace=ToolTrace.restore(json.loads(json.dumps(job.tool_trace.snapshot())))
        panel=trace_panel(trace, True)
        # Not in 执行过程 since 2026-11-04 15:07 (the owner: …在好几个地方都有): the token tile and 任务详情 have them.
        self.assertNotIn('1.23 万',str(panel))
        self.assertNotIn('token/s',str(panel))
        self.assertNotIn('统计口径',str(panel))
        self.assertIn('平均 30.0 token/s',figures_line(trace.snapshot()))
        from covey.cards.streaming import task_details_panel
        from covey.cards.task_figures import figure_cells
        self.assertEqual(figure_cells(trace.snapshot())[1:3],[('token','1.3万',''),('速度','30','token/s')])  # A tile of its own.
        # Since 2026-11-09 the counts are the 输入/输出 box's alone (the owner: 和上面重复了): 任务详情 has none.
        self.assertIsNone(task_details_panel(NS(job_id=1,model='m',timeout=0),trace.snapshot()))
        # S29: a figure the backend did not give is left out, not written as "未提供".
        trace.accept('token_usage',{'input_tokens':None,'output_tokens':700})
        self.assertIsNone(task_details_panel(NS(job_id=1,model='m',timeout=0),trace.snapshot()))
        for bad in [True,-1,'123']:
            trace.accept('token_usage',{'input_tokens':bad,'output_tokens':700})
            self.assertIsNone(trace.input_tokens)
        trace.accept('token_usage',{'input_tokens':0,'output_tokens':700})
        self.assertEqual(trace.input_tokens,0)

    def test_maso_protocol_accepts_numeric_input_and_rejects_invalid_input(self):
        from covey_internal.maso.progress import validate_progress_event
        event={'id':'root','input_tokens':3000,'output_tokens':20,'measured_output_tokens':20,'generation_seconds':2}
        self.assertEqual(validate_progress_event('token_usage',event)['input_tokens'],3000)
        for bad in [True,-1,'3000']:
            with self.assertRaises(ValueError):validate_progress_event('token_usage',{**event,'input_tokens':bad})

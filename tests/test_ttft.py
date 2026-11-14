import io
import json
import queue
import threading
import time
import unittest
from types import SimpleNamespace as NS

from support import make_job
from support.records import saved, state_dir
from support.cards import figures_line, panel_title, trace_panel
from covey.core.trace import ToolTrace
from covey.core.ttft import elapsed, codex_marker
from covey.app.interaction import Interaction
from covey.backends.codex.rpc import AppServer
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.progress import ProgressReporter, CAPABILITY
from covey_internal.maso.bridge import MasoEvents, MasoProtocolError


class TTFTTests(unittest.TestCase):
    def test_only_valid_first_timing_is_frozen_and_persisted(self):
        trace=ToolTrace()
        for bad in [None, -1, True, float('nan'), float('inf')]:
            self.assertFalse(trace.accept('first_token', {'ttft_seconds':bad,'ttft_source':'codex_delta'}))
        self.assertTrue(trace.accept('first_token', {'ttft_seconds':0,'ttft_source':'codex_delta'}))
        self.assertFalse(trace.accept('first_token', {'ttft_seconds':30,'ttft_source':'codex_delta'}))
        trace.finish()
        restored=ToolTrace.restore(json.loads(json.dumps(trace.snapshot())))
        self.assertEqual(figures_line(restored.snapshot()), 'TTFT 0.00 秒')  # In 任务详情 once ended.
        ended = panel_title(trace_panel(ToolTrace.restore({'started_at':1,'ended_at':99}), False))
        self.assertEqual(ended, '执行过程 · 无执行记录')  # Nothing known, no second line; the time is on the status line.
        self.assertNotIn('TTFT', str(trace_panel(ToolTrace(), True)))  # The status line says 等待模型开始输出.
        self.assertEqual(figures_line({'started_at':1,'ended_at':99,'ttft_seconds':25.5,'ttft_source':'codex_item'}),
                         'TTFT 25.50 秒')

    def test_the_title_never_says_ttft(self):
        # Codex #832 read TTFT 等待首输出 after a command; the 2026-11-02 review moved every figure to 任务详情.
        def title(*events, running=True):
            trace=ToolTrace()
            for kind,data in events:trace.accept(kind,data)
            return panel_title(trace_panel(trace, running))
        note=('progress_note',{'id':'backend-retry','text':'模型请求重试中'})
        self.assertNotIn('TTFT',title(note))
        for output in [('token_usage',{'input_tokens':900,'output_tokens':1432}),
                       ('tool',{'id':'sleep','name':'bash','detail':'sleep 60','status':'running','started_at':time.time()}),
                       ('first_token',{'ttft_seconds':2.0,'ttft_source':'codex_delta'})]:
            self.assertNotIn('TTFT',title(note,output))
            self.assertNotIn('TTFT',title(output,running=False))

    def test_first_token_dispatch_does_not_reuse_lifecycle_latency(self):
        queued=[]
        app = NS(cards=NS(queue_trace=lambda *a:queued.append(a)))
        job=make_job(message_id='job',timings={'backend_requested':10,'first_output':10.01})
        state=state_dir(self)
        Interaction._dispatch_structured_event(app,NS(state_dir=state),job,'first_token',
            {'ttft_seconds':2.35,'ttft_source':'codex_delta'},None)
        self.assertEqual(saved(state,'job')['ttft_seconds'],2.35)
        self.assertEqual(len(queued),1)
        self.assertIn('TTFT 2.35 秒',figures_line(job.tool_trace.snapshot()))

    def test_codex_stream_reader_strips_content_and_ignores_status(self):
        rows=[{'method':m,'params':{'threadId':'root','turnId':'turn','delta':'PRIVATE_CONTENT'}} for m in
              ['item/agentMessage/delta','item/reasoning/textDelta','item/reasoning/summaryTextDelta']]
        self.assertIsNone(codex_marker({'method':'item/started','params':{'delta':'ignore'}}))
        self.assertIsNone(codex_marker({'method':'item/agentMessage/delta','params':{'delta':''}}))
        rpc=AppServer.__new__(AppServer)
        rpc.process=NS(stdout=io.StringIO('\n'.join(json.dumps(r) for r in rows)))
        rpc.events=queue.Queue();rpc.pending={};rpc.lock=threading.RLock()
        rpc._read()
        events=[]
        while not rpc.events.empty():events.append(rpc.events.get())
        self.assertEqual([r['method'] for r in events[:3]],['bridge/modelToken']*3)
        self.assertNotIn('PRIVATE_CONTENT',json.dumps(events))
        self.assertEqual(events[0]['params'],{'threadId':'root','turnId':'turn'})

    def test_maso_native_numeric_timing_root_only_and_protocol_gates(self):
        events=[]
        reporter=ProgressReporter('run','root','.',lambda kind,**data:events.append((kind,data)))
        response=NS(role='assistant',extra={'_llm_timing':{'llm_request_ts':100,'llm_first_token_ts':101.2,'llm_response_ts':110}})
        reporter.first_token(NS(agent_id='child'),response)
        self.assertEqual(events,[])
        reporter.first_token(NS(agent_id='root'),response)
        reporter.first_token(NS(agent_id='root'),response)
        self.assertEqual(len(events),1)
        self.assertAlmostEqual(events[0][1]['ttft_seconds'],1.2)
        delivered=[]
        consumer=MasoEvents('run','root',NS(event=lambda *a:delivered.append(a)),capabilities=[CAPABILITY])
        consumer.accept(json.dumps({'version':1,'run_id':'run','type':'session','session_id':'root',
                                    'capabilities':[CAPABILITY],'progress_sequence':True}))
        envelope={'version':1,'run_id':'run','type':events[0][0],**events[0][1]}
        consumer.accept(json.dumps(envelope));consumer.accept(json.dumps(envelope))
        self.assertEqual(len(delivered),1)
        with self.assertRaises(MasoProtocolError):
            consumer.accept(json.dumps({**envelope,'sequence':2,'id':'child'}))

    def test_maso_streamed_output_is_reported_early_without_content(self):
        # S28 #849: TTFT 2.11 s, but first_token came with the whole response 13 s later.
        events,now=[],[100.0]
        reporter=ProgressReporter('run','root','.',lambda kind,**data:events.append((kind,data)),clock=lambda:now[0])
        reporter.stream('child')
        for now[0] in (100.0,101.5,102.0):
            reporter.observe('stream','root')
        self.assertEqual(events,[('model_output',{'sequence':1,'id':'root','received_at':100.0}),
                                 ('model_output',{'sequence':2,'id':'root','received_at':102.0})])
        delivered=[]
        consumer=MasoEvents('run','root',NS(event=lambda *a:delivered.append(a)),capabilities=[CAPABILITY])
        consumer.accept(json.dumps({'version':1,'run_id':'run','type':'session','session_id':'root',
                                    'capabilities':[CAPABILITY],'progress_sequence':True}))
        envelope={'version':1,'run_id':'run','type':events[0][0],**events[0][1]}
        consumer.accept(json.dumps(envelope))
        self.assertEqual(delivered,[('model_output',{'id':'root','received_at':100.0},None)])
        for bad in ({'id':'child'},{'received_at':'soon'}):
            with self.assertRaises(MasoProtocolError):
                consumer.accept(json.dumps({**envelope,'sequence':2,**bad}))
        job=make_job(tool_trace=ToolTrace(),activity_monitor=NS(output=lambda:events.append('output')))
        Interaction._dispatch_structured_event(NS(),NS(),job,*delivered[0])
        self.assertEqual(events[-1],'output')
        self.assertEqual(job.tool_trace.snapshot()['ttft_seconds'],None)  # TTFT stays MASO's own figure.

    def test_maso_reports_its_first_model_request_once_root_only(self):
        # S29 #863: the SDK set up for about 8 s before its first model call; the status line said 已提交给模型.
        events,now=[],[100.0]
        reporter=ProgressReporter('run','root','.',lambda kind,**data:events.append((kind,data)),clock=lambda:now[0])
        reporter.request(NS(agent_id='child'))
        reporter.request(NS(agent_id='root'))
        now[0]=104.0
        reporter.request(NS(agent_id='root'))  # The next call: generation time only, no event.
        self.assertEqual(events,[('model_request',{'sequence':1,'id':'root','received_at':100.0})])
        self.assertEqual(reporter.request_started,104.0)
        delivered=[]
        consumer=MasoEvents('run','root',NS(event=lambda *a:delivered.append(a)),capabilities=[CAPABILITY])
        consumer.accept(json.dumps({'version':1,'run_id':'run','type':'session','session_id':'root',
                                    'capabilities':[CAPABILITY],'progress_sequence':True}))
        envelope={'version':1,'run_id':'run','type':events[0][0],**events[0][1]}
        consumer.accept(json.dumps(envelope))
        self.assertEqual(delivered,[('model_request',{'id':'root','received_at':100.0},None)])
        for bad in ({'id':'child'},{'received_at':-1}):
            with self.assertRaises(MasoProtocolError):
                consumer.accept(json.dumps({**envelope,'sequence':2,**bad}))

    def test_missing_first_response_timing_cannot_be_replaced_by_later_turn(self):
        events=[]
        reporter=ProgressReporter('run','root','.',lambda *a,**k:events.append((a,k)))
        reporter.first_token(NS(agent_id='root'),NS(role='assistant',extra={}))
        reporter.first_token(NS(agent_id='root'),NS(role='assistant',extra={'_llm_timing':{
            'llm_request_ts':100,'llm_first_token_ts':101,'llm_response_ts':110}}))
        self.assertEqual(events,[])
        for a,b,c in [(2,1,3),(1,4,3),(None,3,4),(1,float('nan'),4)]:
            self.assertIsNone(elapsed(a,b,c))


if __name__=='__main__':unittest.main()

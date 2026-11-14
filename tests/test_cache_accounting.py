import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from support import requires_extension, make_job
from support.fake_lark import FakeCardKit
from support.cards import panel_title, trace_panel
from covey.backends.codex.usage import CodexTurnUsage
from covey.backends.claude.usage import ClaudeTurnUsage
from covey.core.trace import ToolTrace, task_trace
from covey.cards.streaming import task_details_panel, task_details_text, usage_lines
from covey.cards.task_figures import figure_cells
from covey.core.trace import turn_usage

class CacheAccountingTests(unittest.TestCase):
 def test_codex_cumulative_cache_is_this_turn_only(self):
  u=CodexTurnUsage()
  u.update({'total':{'inputTokens':100,'outputTokens':10,'cachedInputTokens':80}},False)
  result=u.update({'total':{'inputTokens':160,'outputTokens':15,'cachedInputTokens':120}},True)
  self.assertEqual((result['input_tokens'],result['cache_read_tokens']),(60,40))
  self.assertIsNone(u.update({'total':{'inputTokens':160,'outputTokens':15,'cachedInputTokens':120}},True))
 def test_claude_disjoint_buckets_are_not_added_twice(self):
  events=[];u=ClaudeTurnUsage(lambda k,v:events.append(v))
  u.update('one',{'input_tokens':10,'cache_read_input_tokens':60,'cache_creation_input_tokens':30,'output_tokens':5},1)
  u.update('one',{'output_tokens':8},2)
  self.assertEqual((events[-1]['input_tokens'],events[-1]['cache_read_tokens'],events[-1]['cache_write_tokens']),(100,60,30))
  u.update('two',{'input_tokens':10,'output_tokens':1},3)
  self.assertIsNone(events[-1]['cache_read_tokens'])
 @requires_extension()
 def test_maso_cached_tokens_are_subset_and_missing_is_unknown(self):
  from covey_internal.maso.progress import ProgressReporter
  rows=[];u=ProgressReporter('run','root','.',lambda k,**d:rows.append(d))
  ctx=NS(agent_id='root')
  one=NS(id='one',role='assistant',usage={'prompt_tokens':100,'completion_tokens':5,'cached_tokens':70})
  u.usage(ctx,one);u.usage(ctx,one)
  self.assertEqual(len(rows),1);self.assertEqual(rows[-1]['cache_read_tokens'],70)
  u.usage(ctx,NS(id='two',role='assistant',usage={'prompt_tokens':100,'completion_tokens':5}))
  self.assertIsNone(rows[-1]['cache_read_tokens']);self.assertEqual(rows[-1]['input_tokens'],200)
 def test_trace_persists_cache_but_rejects_invalid_or_partial_counts(self):
  t=ToolTrace();base={'input_tokens':100,'output_tokens':10,'measured_output_tokens':10,'generation_seconds':1}
  t.accept('token_usage',{**base,'cache_read_tokens':80,'cache_write_tokens':20})
  restored=ToolTrace.restore(t.snapshot());self.assertEqual(restored.cache_read_tokens,80)
  t.accept('token_usage',{**base,'cache_read_tokens':101,'cache_write_tokens':True})
  self.assertIsNone(t.cache_read_tokens);self.assertIsNone(t.cache_write_tokens)
  t.accept('token_usage',base);self.assertIsNone(t.cache_read_tokens)
 def test_cost_detail_is_collapsed_and_does_not_repeat_the_box(self):
  # The owner, 2026-11-09 13:48: the token counts and their cache split repeated the 输入/输出 box (只保留上面的).
  panel=task_details_panel(NS(job_id=3,model='test',timeout=0,work_mode='focus'),{'cache_read_tokens':1000,'cache_write_tokens':None})
  self.assertFalse(panel['expanded'])
  self.assertEqual(panel['elements'][0]['content'],'工作模式：focus')
 def test_details_without_anything_to_say_are_not_shown(self):
  # S28: "缓存命中：0 token · 缓存写入：未提供" plus a two-sentence disclaimer, for nothing; since 2026-11-09 the
  # cache figures are the box's, and 任务详情 with nothing else to say is not on the card (no 暂无统计).
  job=NS(job_id=3,model='test',timeout=0)
  for usage in (None,{'cache_read_tokens':0,'cache_write_tokens':None},{},{'cache_read_tokens':True,'cache_write_tokens':-1},
                {'cache_read_tokens':1200,'cache_write_tokens':300},{'input_tokens':5,'output_tokens':2}):
   self.assertIsNone(task_details_panel(job,usage),usage)
  self.assertEqual(task_details_panel(NS(job_id=3,model='test',timeout=600),None)['elements'][0]['content'],'超时：600s')
 def test_a_redacted_turn_says_how_many_replacements_without_the_terms(self):
  import json
  from covey.app.interaction import Interaction
  queued=[]
  app=NS(cards=NS(queue_trace=lambda *a:queued.append(a)))
  job=make_job(message_id='job',timings={})
  Interaction._dispatch_structured_event(app,NS(state_dir=Path('/nonexistent')),job,'redaction',
      {'terms':3,'hits':1234,'requests':2},None)
  self.assertEqual(len(queued),1)  # Through the process record, as token usage goes.
  snapshot=json.loads(json.dumps(job.tool_trace.snapshot()))
  self.assertEqual(snapshot['redaction'],{'terms':3,'hits':1234,'requests':2})
  def details(usage):return task_details_text(NS(job_id=3,model='test',timeout=0),usage)
  restored=ToolTrace.restore(snapshot).snapshot()
  self.assertIn('敏感词脱敏：本轮替换 1,234 处（词表 3 个词）',details(restored))  # 脱敏开 is the box's.
  empty=task_trace();empty.accept('redaction',{'terms':0,'hits':0,'requests':1})
  self.assertIn('敏感词脱敏：词表为空，未替换',details(empty.snapshot()))
  # Not a redacted turn, or counts that are not counts: no line, and nothing kept.
  for bad in ({},{'terms':2,'hits':-1,'requests':1},{'terms':'2','hits':1,'requests':1},{'terms':True,'hits':1,'requests':1}):
   t=task_trace();self.assertFalse(t.accept('redaction',bad))
   self.assertNotIn('redaction',t.snapshot());self.assertNotIn('敏感词',details(t.snapshot()))
  self.assertNotIn('敏感词',details(None))
 def test_trace_refresh_uses_separate_queue_and_terminal_is_immediate(self):
  from support.world import build_claude_app
  app=build_claude_app()
  queue=Mock();job=make_job(tool_trace=ToolTrace(),trace_card_updates=queue)
  app.cards.queue_trace(NS(),job,urgent=False)
  self.assertIs(queue.submit.call_args.args[2],False)
  job.tool_trace.finish();app.cards.queue_trace(NS(),job,urgent=True)
  self.assertIs(queue.submit.call_args.args[2],True)
  self.assertIsNone(job.card_updates)
 def test_existing_details_are_replaced_and_rejection_is_counted(self):
  from support.world import build_claude_app
  app=build_claude_app()
  progress=NS(card_id='card',element_id='answer',sequence=1,update_failures=0)
  job=make_job(job_id=3,model='test',timeout=0,message_id='message',progress=progress)
  panel=task_details_panel(job,{'redaction':{'terms':3,'hits':1,'requests':1}})
  with FakeCardKit({}) as lark:
   self.assertTrue(app.cards.set_task_element(NS(profile='test'),job,panel))
   self.assertEqual((lark.last.method,lark.last.path),('PUT','/open-apis/cardkit/v1/cards/card/elements/task_details'))
  progress.card_id='replacement'
  with FakeCardKit(None) as lark:
   self.assertFalse(app.cards.set_task_element(NS(profile='test'),job,panel))
   self.assertEqual(lark.last.method,'PUT')
   self.assertEqual(progress.update_failures,1)
   self.assertIn('task_details',job.last_card_error)

U={'input_tokens':2,'cache_read_input_tokens':100,'cache_creation_input_tokens':10,'output_tokens':40}

def sub(owner,mid,usage=U,stop='end_turn',**extra):
 return {'type':'assistant','parent_tool_use_id':owner,'message':{'id':mid,'usage':dict(usage),'stop_reason':stop},**extra}

def call(owner,cid,name='检查'):
 return {'type':'assistant','parent_tool_use_id':owner,'message':{'id':'call-'+cid,'content':[
  {'type':'tool_use','id':cid,'name':'Agent','input':{'description':name}}]}}

def transcript(root,name,meta,rows,mtime=None):
 import json,os
 root.mkdir(parents=True,exist_ok=True)
 (root/(name+'.meta.json')).write_text(json.dumps(meta))
 path=root/(name+'.jsonl')
 path.write_text(''.join(json.dumps({'type':'assistant','timestamp':stamp,'message':message})+'\n' for stamp,message in rows))
 if mtime is not None:os.utime(path,(mtime,mtime))

def iso(epoch):
 from datetime import datetime,timezone
 return datetime.fromtimestamp(epoch,timezone.utc).isoformat()


class SubtaskUsageCardTests(unittest.TestCase):
 """S31 #868: the card read the main task's 11.8 M as the turn's; its 22 subagents used 395 M more."""
 def trace(self,report,ended=True,**main):
  t=task_trace();t.accept('token_usage',{'input_tokens':1000,'cache_read_tokens':900,'cache_write_tokens':50,'output_tokens':10,**main})
  if report is not None:t.accept('subtask_usage',report)
  if ended:t.finish()
  return t
 def details(self,trace):
  return task_details_text(NS(job_id=868,model='test',timeout=0),trace.snapshot()).split('\n')
 def agents(self,n,level=1):
  return {f'a{i}':{'name':f'子任务{i}','level':level,'input_tokens':100,'cache_read_tokens':80,'cache_write_tokens':10,'output_tokens':5} for i in range(n)}
 def test_card_shows_the_turn_total_with_its_parts(self):
  report={'agents':{**self.agents(3),'deep':{**self.agents(1)['a0'],'name':'深层','level':2}},'unreported':0,'pending':0}
  t=self.trace(report)
  self.assertEqual(figure_cells(t.snapshot())[1],('token','1,430',''))  # The token tile: the turn's total.
  # Not the turn's total or its split by the cache: the 输入/输出 box has them (the owner, 2026-11-09).
  self.assertEqual(self.details(t),['token 用量：本轮主任务 + 4 个子任务','主任务：输入 1,000 · 输出 10 token',
   '子任务 · 子任务0（第 1 层）：输入 100 · 输出 5 token','子任务 · 子任务1（第 1 层）：输入 100 · 输出 5 token',
   '子任务 · 子任务2（第 1 层）：输入 100 · 输出 5 token','子任务 · 深层（第 2 层）：输入 100 · 输出 5 token',
   '输入是各次请求的输入之和，不是单次上下文长度，也不是计费折算量。'])
  many=self.trace({'agents':{**self.agents(6),**{k+'x':v for k,v in self.agents(2,level=2).items()}},'unreported':0,'pending':0})
  restored=ToolTrace.restore(many.snapshot())
  self.assertNotIn('agents',restored.subtask_usage)  # bounded: per level once there are more than five
  self.assertEqual(self.details(restored)[2:4],['第 1 层子任务（6 个）：输入 600 · 输出 30 token','第 2 层子任务（2 个）：输入 200 · 输出 10 token'])
 def test_partial_totals_say_what_is_missing(self):
  t=self.trace({'agents':self.agents(1),'unreported':2,'pending':3},ended=False)
  # While running, the two without usage yet are started subtasks too; afterwards they are 未计入.
  self.assertEqual((turn_usage(t.snapshot())['subtasks'],turn_usage(t.snapshot())['missing']),(3,['运行中']))
  self.assertEqual(usage_lines(t.snapshot())[0],'token 用量：本轮主任务 + 3 个子任务（部分统计：运行中）')
  self.assertNotIn('部分统计',panel_title(trace_panel(t)))  # Not under the title since 2026-11-04 15:07: 任务详情 says it.
  t.finish()
  self.assertIn('部分统计：2 个子任务未计入，3 次子任务请求的输出 token 未取到最终值',usage_lines(t.snapshot())[0])
  self.assertEqual(self.details(t)[0],'token 用量：本轮主任务 + 1 个子任务（部分统计：2 个子任务未计入，3 次子任务请求的输出 token 未取到最终值）')
 def test_a_started_subtask_is_in_the_total_before_its_usage(self):
  # S31 K6: 主任务 … for 16 s after two subagents started; the first report came before any usage.
  t=self.trace({'agents':{},'unreported':1,'pending':0},ended=False)
  for sid in ('a0','a1'):t.accept('agent_state',{'id':sid,'name':sid,'status':'running','started_at':1})
  self.assertEqual((turn_usage(t.snapshot())['subtasks'],turn_usage(t.snapshot())['missing']),(2,['运行中']))
  self.assertEqual(usage_lines(t.snapshot())[0],'token 用量：本轮主任务 + 2 个子任务（部分统计：运行中）')  # 任务详情 says so too.
  t.accept('subtask_usage',{'agents':self.agents(2),'unreported':0,'pending':0});t.finish()
  self.assertEqual(usage_lines(t.snapshot())[:2],['token 用量：本轮主任务 + 2 个子任务','主任务：输入 1,000 · 输出 10 token'])
 def test_backend_without_subtask_usage_shows_the_main_task_as_partial(self):
  # MASO reports no child usage; its children are on the trace.
  t=self.trace(None,ended=False);t.accept('agent_state',{'id':'child','name':'子任务','status':'running','started_at':1})
  self.assertEqual(usage_lines(t.snapshot())[0],'token 用量：只含主任务（部分统计：1 个子任务未计入）')
  t.finish();self.assertEqual(self.details(t)[:2],['token 用量：只含主任务（部分统计：1 个子任务未计入）','主任务：输入 1,000 · 输出 10 token'])
 def test_main_task_only_and_older_snapshots(self):
  t=self.trace(None)
  self.assertEqual(figure_cells(t.snapshot())[1],('token','1,010',''))
  # The main task alone: its counts are the box's (the owner, 2026-11-09), 任务详情 has none, and is not shown.
  self.assertEqual(self.details(t),[''])
  self.assertIsNone(task_details_panel(NS(job_id=3,model='test',timeout=0),t.snapshot()))
  old=t.snapshot();del old['subtask_usage']
  self.assertEqual(figure_cells(old)[1],('token','1,010',''))
  self.assertIsNone(task_details_panel(NS(job_id=3,model='test',timeout=0),old))
 def test_record_keeps_the_report_beside_the_main_task_fields(self):
  from support.records import saved,state_dir
  from covey.app.interaction import Interaction
  app=NS(cards=NS(queue_trace=lambda *a:None));job=make_job(message_id='job',timings={})
  state=state_dir(self)
  Interaction._dispatch_structured_event(app,NS(state_dir=state),job,'subtask_usage',{'agents':self.agents(1),'unreported':0,'pending':0},None)
  self.assertEqual(saved(state,'job')['subtask_usage']['levels'][0]['input_tokens'],100)

class ClaudeSubtaskUsageTests(unittest.TestCase):
 """Each subagent request once, at every level, final counts from the transcripts, earlier turns out."""
 def usage(self,**kw):
  from covey.backends.claude.usage import ClaudeSubtaskUsage
  self.events=[];return ClaudeSubtaskUsage(lambda k,v:self.events.append((k,v)),**kw)
 def tmp(self):
  import tempfile
  d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup);return Path(d.name)
 def test_every_level_counts_each_message_once_and_retries_twice(self):
  u=self.usage(interval=0)
  rows=[call(None,'a1'),call('a1','a2','嵌套'),sub('a1','m1',stop=None,usage={**U,'output_tokens':1}),sub('a1','m1'),sub('a1','m1'),
        sub('a2','m2'),sub('a2','m3',usage={**U,'output_tokens':0},stop=None),sub('a2','m3-retry'),
        sub('a2','error',usage={'input_tokens':0,'output_tokens':0},is_api_error_message=True),
        {'type':'assistant','parent_tool_use_id':None,'message':{'id':'root','usage':U,'stop_reason':'end_turn'}},
        {'type':'result','usage':{'input_tokens':10**6,'output_tokens':10**6}}]
  for row in rows:u.accept(row,1)
  report=self.events[-1][1]
  self.assertEqual({k:(a['level'],a['input_tokens'],a['output_tokens']) for k,a in report['agents'].items()},
                   {'a1':(1,112,40),'a2':(2,336,80)})
  self.assertEqual(report['agents']['a2']['name'],'嵌套')
  self.assertEqual((report['unreported'],report['pending']),(0,1))  # m3's stream was cut off; no transcript yet
  u.settle(None,0);self.assertEqual(self.events[-1][1]['pending'],1)
 def test_transcripts_settle_final_counts_and_leave_earlier_turns_out(self):
  import time
  root=self.tmp()/'subagents';since=time.time()-60
  u=self.usage();u.accept(call(None,'a1'),since);u.accept(sub('a1','m1',stop=None,usage={**U,'output_tokens':1}),since+1)
  transcript(root,'agent-x',{'toolUseId':'a1','spawnDepth':1},[(iso(since-3600),{'id':'old','usage':U,'stop_reason':'end_turn'}),
   (iso(since+1),{'id':'m1','usage':U,'stop_reason':'end_turn'}),(iso(since+2),{'id':'m2','usage':U,'stop_reason':None})])
  transcript(root,'agent-old',{'toolUseId':'gone','spawnDepth':1},[(iso(since+5),{'id':'stale','usage':U,'stop_reason':'end_turn'})],mtime=since-3600)
  with (root/'agent-x.jsonl').open('a') as handle:handle.write('{"type":"assistant","timest')  # torn last line
  u.settle(root,since)
  report=self.events[-1][1]
  # The earlier turn's request and a transcript untouched long before the turn stay out; m2 was cut off
  # when the run ended, and what Claude Code recorded of it counts.
  self.assertEqual(list(report['agents']),['a1'])
  self.assertEqual((report['agents']['a1']['input_tokens'],report['agents']['a1']['output_tokens']),(224,80))
  self.assertEqual((report['unreported'],report['pending']),(0,0))
 def test_missing_transcripts_or_unstarted_agents_are_partial(self):
  u=self.usage();u.accept(call(None,'a1'),1);u.accept(call(None,'a2'),1);u.accept(sub('a1','m1',stop=None),1)
  u.settle(self.tmp()/'absent',0)
  self.assertEqual((self.events[-1][1]['unreported'],self.events[-1][1]['pending']),(1,1))
  root=self.tmp()/'subagents';transcript(root,'agent-x',{'toolUseId':'a1'},[(iso(5),{'id':'m1','usage':U,'stop_reason':'end_turn'})])
  u=self.usage();u.accept(call(None,'a1'),1);u.accept(call(None,'a2'),1);u.accept(sub('a1','m1',stop=None),1);u.settle(root,0)
  self.assertEqual((self.events[-1][1]['unreported'],self.events[-1][1]['pending']),(0,0))  # a2 never started
 def test_reports_are_throttled_and_the_settle_is_forced(self):
  u=self.usage(interval=10);u.accept(call(None,'a1'),0);u.accept(sub('a1','m1'),1);u.accept(sub('a1','m2'),2)
  self.assertEqual(len(self.events),1)
  u.accept(sub('a1','m3'),11);self.assertEqual(self.events[-1][1]['agents']['a1']['output_tokens'],120)
  u.accept(sub('a1','m4'),12);u.settle(None,None)
  self.assertEqual((len(self.events),self.events[-1][1]['agents']['a1']['output_tokens']),(3,160))
  self.usage().settle(None,0);self.assertEqual(self.events,[])  # no subagents, no report

 def test_an_ended_subagent_is_counted_from_its_transcript_while_the_turn_runs(self):
  # Repo review S1: #1184's background subagents read 1.1 万 output while the turn ran, 24.1 万 once settled.
  import time
  root=self.tmp()/'subagents';since=time.time()-60
  u=self.usage(interval=10,locate=lambda:(root,since))
  for agent in ('a1','a2'):
   u.accept(call(None,agent),since);u.accept(sub(agent,'m-'+agent,stop=None,usage={**U,'output_tokens':1}),since+1)
  self.assertEqual(u.report()['pending'],2)
  transcript(root,'agent-x',{'toolUseId':'a1','spawnDepth':1},[(iso(since+1),{'id':'m-a1','usage':U,'stop_reason':'end_turn'})])
  before=len(self.events)
  u.finished('a1',since+2)  # Reported at once, inside the interval.
  report=self.events[-1][1]
  self.assertEqual((len(self.events)-before,report['agents']['a1']['output_tokens'],report['pending']),(1,40,1))  # a2 runs on.
  # a2's last row is written just after its notification: read again at a later report, a few times at most.
  transcript(root,'agent-y',{'toolUseId':'a2','spawnDepth':1},[])
  u.finished('a2',since+3)
  self.assertEqual(self.events[-1][1]['pending'],1)
  transcript(root,'agent-y',{'toolUseId':'a2','spawnDepth':1},[(iso(since+1),{'id':'m-a2','usage':U,'stop_reason':'end_turn'})])
  u.accept({'type':'assistant','parent_tool_use_id':None,'message':{'id':'root'}},since+20)
  self.assertEqual((self.events[-1][1]['agents']['a2']['output_tokens'],self.events[-1][1]['pending']),(40,0))
  self.assertEqual(u.ended,{})
  u.finished('a3',since+21)
  for at in range(4):
   u.accept({'type':'assistant','parent_tool_use_id':None,'message':{'id':'root'}},since+40+at*20)
  self.assertEqual(u.ended,{})  # Never written: given up.
 def test_the_adapter_settles_a_subagent_when_it_ends(self):
  from covey.backends.claude.events import ClaudeEventAdapter
  adapter=ClaudeEventAdapter(lambda *a:None);adapter.subtasks.finished=Mock()
  background={'type':'assistant','parent_tool_use_id':None,'message':{'id':'r1','content':[
   {'type':'tool_use','id':'bg','name':'Agent','input':{'description':'后台','run_in_background':True}},
   {'type':'tool_use','id':'fg','name':'Agent','input':{'description':'前台'}}]}}
  adapter.accept(background)
  adapter.accept({'type':'user','parent_tool_use_id':None,'message':{'content':[
   {'type':'tool_result','tool_use_id':'bg','content':'Async agent launched'},{'type':'tool_result','tool_use_id':'fg','content':'done'}]}})
  self.assertEqual([c.args[0] for c in adapter.subtasks.finished.call_args_list],['fg'])  # bg only started.
  adapter.accept({'type':'system','subtype':'task_notification','tool_use_id':'bg','status':'completed'})
  self.assertEqual([c.args[0] for c in adapter.subtasks.finished.call_args_list],['fg','bg'])
 def test_a_cache_hit_near_all_keeps_one_decimal(self):
  from covey.core.text import share
  # 99.6% read 100%: only all of it does now; below 99% whole percent as before.
  self.assertEqual([share(p,w) for p,w in ((996,1000),(9996,10000),(1000,1000),(990,1000),(981,1000),(1,1000))],
                   ['99.6%','99.9%','100%','99.0%','98%','<1%'])

class CodexSubtaskUsageTests(unittest.TestCase):
 def test_codex_child_threads_count_from_their_own_baseline(self):
  from covey.backends.codex.events import CodexEventAdapter
  events=[];a=CodexEventAdapter(lambda k,v:events.append((k,v)))
  a.agents({'type':'collabAgentToolCall','tool':'spawnAgent','receiverThreadIds':['t1','t2'],'agentsStates':{}})
  # t1 resumes with 500 earlier tokens: only this turn's requests count.
  a.child_tokens('t1',{'total':{'inputTokens':700,'outputTokens':30,'cachedInputTokens':100},'last':{'inputTokens':200,'outputTokens':10,'cachedInputTokens':50}},1)
  a.publish_subtasks(2,force=True)
  report=[v for k,v in events if k=='subtask_usage'][-1]
  self.assertEqual((report['agents']['t1']['input_tokens'],report['agents']['t1']['output_tokens'],report['agents']['t1']['cache_read_tokens']),(200,10,50))
  self.assertEqual(report['unreported'],1)
 def test_a_spawned_child_is_reported_before_its_usage(self):
  # Until a report the card cannot tell Codex from a backend that never reports (MASO: 子任务未计入).
  from covey.backends.codex.events import CodexEventAdapter
  events=[];a=CodexEventAdapter(lambda k,v:events.append((k,v)))
  a.agents({'type':'collabAgentToolCall','tool':'spawnAgent','receiverThreadIds':['t1'],'agentsStates':{}},1)
  self.assertEqual([k for k,_ in events],['subtask_usage','agent_state'])
  self.assertEqual(events[0][1],{'agents':{},'unreported':1,'pending':0})
  t=task_trace()
  for k,v in events:t.accept(k,v)
  self.assertEqual((turn_usage(t.snapshot())['subtasks'],turn_usage(t.snapshot())['missing']),(1,['运行中']))

if __name__=='__main__':unittest.main()

import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AlertTriangle, Check, ChevronDown, Clock, Loader2 } from 'lucide-react';
import type { PlanItem, SnapshotProject, SnapshotTopic, StepView, TopicProgress, TurnFailure, TurnView } from '@shared/api.interface';
import { useSnapshot, useTurns } from '@client/src/hooks/use-workbench';
import { DismissButton, RecoverActions, useBoardOn } from './RecoverBits';
import { DraftLines, LaterLine } from './V34Bits';
import { Dot, useTick } from './ui';
import { fmtDur, hhmm, isFinished, isRunning, isWaiting, shortWhen, toneOf, toneWord, type ToneKey } from './lib';
import { TopicMemory } from './MemoryPanel';
import { MemoryList, useV43TopicEntries } from './MemoryList';
import {
  FALLBACK_FAILURE, ago, etaText, firstParagraph, firstSentence, groupRounds, isHelperTurn, lastAgent, lastDoneAgent, listTone, progressNote, splitTitle, stopPressing, type Round,
} from './topicText';

/** 一个话题此刻的进度：v4 的 progress 优先，没有就从 v3 的 plan / steps 和轮次里凑 */
export interface ProgressView {
  topic: SnapshotTopic | undefined;
  progress: TopicProgress | undefined;
  plan: PlanItem[];
  done: number;
  total: number;
  live: TurnView | undefined;
  last: TurnView | undefined;
  failed: boolean;
  /** 这一轮在等你答题：没在跑，也没结束 */
  waiting: boolean;
  /** 停住的还在看板上（要你处理）；看板不列了就是“已停止”，灰着 */
  pressing: boolean;
  rounds: Round[];
  turns: TurnView[];
}

export function useProgressView(topicKey: string): ProgressView {
  const { data } = useTurns(topicKey);
  const { data: snap } = useSnapshot();
  const topic: SnapshotTopic | undefined = snap?.topics.find((t: SnapshotTopic) => t.key === topicKey) ?? (data?.topic as SnapshotTopic | undefined);
  // v31：协助方答咨询的那几行不算这个话题自己的进度和结论（它自己的“有结果”和最近交付不变）
  const turns: TurnView[] = (data?.turns ?? []).filter((t: TurnView) => !isHelperTurn(t));
  const agents: TurnView[] = turns.filter((t: TurnView) => t.role !== 'owner' && t.role !== 'event');
  const live: TurnView | undefined = agents.find((t: TurnView) => t.status === 'running');
  const last: TurnView | undefined = lastAgent(turns);
  const withPlan: TurnView | undefined = live?.plan?.length ? live : [...agents].reverse().find((t: TurnView) => t.plan?.length);
  const plan: PlanItem[] = withPlan?.plan ?? [];
  const progress: TopicProgress | undefined = topic?.progress ?? (data?.topic as SnapshotTopic | undefined)?.progress;
  const total: number = progress?.plan_total ?? plan.length;
  const done: number = progress?.plan_total ? (progress.plan_done ?? 0) : plan.filter((p: PlanItem) => p.state === 'done').length;
  const failed: boolean = !live && last?.status === 'failed';
  const waiting: boolean = !live && (last?.status === 'waiting' || (!!topic && isWaiting(topic)));
  const pressing: boolean = !topic || stopPressing(topic);
  return { topic, progress, plan, done, total, live, last, failed, waiting, pressing, rounds: groupRounds(turns, topic?.round ?? 0), turns };
}

/** 进度条的样子：在跑的画当前一段，停住的画红，结束了的没做的一律灰 */
export type SegState = 'live' | 'failed' | 'ended';
export function segState(v: Pick<ProgressView, 'live' | 'failed' | 'pressing'>): SegState {
  return v.live ? 'live' : v.failed && v.pressing ? 'failed' : 'ended';
}

/** 右栏的大字：在跑、做满了写“步骤 5 / 8”；没做满就结束了要说清，免得以为还在跑 */
export function stepsHeadline(v: Pick<ProgressView, 'live' | 'failed' | 'waiting' | 'pressing' | 'done' | 'total'>): string {
  if (v.live || v.waiting || v.done >= v.total) return `步骤 ${v.done} / ${v.total}`;
  if (v.failed) return `做到 ${v.done}/${v.total} 步${v.pressing ? '停住了' : '已停止'}`;
  return `做到 ${v.done}/${v.total} 步就结束了`;
}

/** 分段线：做完的绿；在跑的下一段蓝，停住的下一段红；已经结束的，没做的一律灰（T550 P1-3） */
export const SegBar: React.FC<{ done: number; total: number; state: SegState; className?: string }> = ({ done, total, state, className }) => {
  if (total <= 0) return null;
  const n: number = Math.min(total, 12);
  const on: number = Math.round((Math.min(done, total) / total) * n);
  const next: string = state === 'live' ? 'cur' : state === 'failed' ? 'cur fail' : '';
  return (
    <div className={`a-seg${className ? ` ${className}` : ''}`} role="img" aria-label={`步骤 ${done} / ${total}`}>
      {Array.from({ length: n }).map((_, i: number) => (
        <i key={i} className={i < on ? 'on' : i === on && done < total ? next : ''} />
      ))}
    </div>
  );
};

/** 话题页顶上的总览：目标、现在到哪、最新结论、等你的事（拿不到的那行不画）。
 * compact：主人往下滚时收成一行（状态 + 现在到哪），点开再展开 */
export const TopicOverview: React.FC<{ topicKey: string; mobile?: boolean; compact?: boolean; onExpand?: () => void }> = ({ topicKey, mobile, compact, onExpand }) => {
  const v: ProgressView = useProgressView(topicKey);
  const { data } = useTurns(topicKey);
  const nav = useNavigate();
  const now: number = useTick(30000);
  const board: boolean = useBoardOn();
  const t: SnapshotTopic | undefined = v.topic;
  if (!t) return null;
  const p: TopicProgress | undefined = v.progress;
  // 目标用“本质”（对象 + 要达成的事，做完也不变）；旧要点的“现在的目标”常写成了状态（T542：“已全部上线…”）
  const goal: string = t.gist?.essence || t.gist?.goal || t.title;
  const doneT: TurnView | undefined = lastDoneAgent(v.turns);
  const doneRound: Round | undefined = doneT ? v.rounds.find((r: Round) => r.agent === doneT) : undefined;
  const concl: string = doneT ? firstParagraph(doneT.body, 220) : '';
  const question = data?.question ?? null;
  const lastRound: Round | undefined = v.rounds.filter((r: Round) => !r.event).slice(-1)[0];
  // 停住的还在看板上（要你处理）才是“等你的事”；看板不列了，就只说原因和能做什么（T550 P1-4）
  const pressing: boolean = v.pressing;

  // 现在到哪
  let where: React.ReactNode = null;
  const stepPill: React.ReactNode = v.total > 0 ? <span className="a-step">步骤 {v.done}/{v.total}</span> : null;
  const eta: string = etaText(p?.eta_seconds);
  // 在等命令、这步比平均慢、可能卡住（slow_step、command，同飞书任务卡的状态行）
  const note = v.live ? progressNote(p, now) : { text: '', stuck: false };
  if (p?.phase || p?.last_activity || note.text) {
    where = (
      <>
        {p?.phase ? <b>{p.phase}</b> : null}
        <span className="a-sub">
          {stepPill}
          {eta ? <span>预计{v.failed ? ' 继续后' : '还要'} {eta}</span> : null}
          {p?.last_activity ? (
            <span><Clock />{p.last_activity_at ? `${ago(p.last_activity_at, now)}：` : ''}{p.last_activity}</span>
          ) : null}
          {note.text ? <span className={note.stuck ? 'a-stuck' : 'a-slow'}>{note.text}</span> : null}
        </span>
      </>
    );
  } else if (v.live) {
    const doing: string = v.plan.find((x: PlanItem) => x.state === 'doing')?.title ?? '';
    where = (
      <>
        <b>在做第 {lastRound?.n ?? t.round} 轮{doing ? `：${doing}` : ''}</b>
        <span className="a-sub">
          {stepPill}
          {v.live.started_at ? <span><Clock />本轮 {fmtDur(now - v.live.started_at, false)}</span> : null}
        </span>
      </>
    );
  } else if (v.failed) {
    const at: number | null = v.last?.ended_at ?? v.last?.started_at ?? null;
    where = (
      <>
        <b>第 {lastRound?.n ?? t.round} 轮{pressing ? '停住了' : '已停止'}</b>
        <span className="a-sub">
          {stepPill}
          {at ? <span><Clock />{shortWhen(at, now)}</span> : null}
        </span>
      </>
    );
  } else if (question || isWaiting(t)) {
    where = <b>等你</b>;
  } else if (v.last) {
    where = (
      <b className="a-plain">
        {isFinished(t) ? `第 ${lastRound?.n ?? t.round} 轮做完了` : `${toneWord(listTone(t))} · 第 ${lastRound?.n ?? t.round} 轮`}
        <span className="a-mute"> · {hhmm(v.last.ended_at ?? v.last.started_at)}</span>
      </b>
    );
  }

  if (compact) {
    const tn: ToneKey = v.failed && !isRunning(t) && !question ? (pressing ? 'stopped' : 'cancelled') : question ? 'waiting' : toneOf(t);
    const line: string = p?.phase
      || (v.live ? `在做第 ${lastRound?.n ?? t.round} 轮` : v.failed ? `第 ${lastRound?.n ?? t.round} 轮${pressing ? '停住了' : '已停止'}：${splitTitle((v.last?.failure ?? FALLBACK_FAILURE).title)[0]}` : question || isWaiting(t) ? '等你' : concl ? `最新结论：${concl}` : '');
    return (
      <section className="a-ov a-ovc" aria-label="话题总览（已收起）">
        <button type="button" className="a-ovmini" onClick={onExpand} aria-expanded="false" title="展开总览">
          <Dot tone={tn} />
          <span className="a-ol">{toneWord(tn)}</span>
          <span className="a-om">{line}</span>
          {stepPill}
          <ChevronDown />
        </button>
      </section>
    );
  }

  // 等你的事
  let wait: React.ReactNode = null;
  if (question) {
    const first: string = question.questions[0]?.question ?? '';
    wait = (
      <>
        <span>{question.questions.length} 件：{first}{question.questions.length > 1 ? ` 等 ${question.questions.length} 题` : ''}</span>
        <button type="button" className="btn pri sm" onClick={() => document.getElementById('a-question')?.scrollIntoView({ block: 'start', behavior: 'smooth' })}>
          去回答
        </button>
      </>
    );
  } else if (v.failed && v.last) {
    // v33：按钮照 failure 的 labels、how、auto_retry 画，点了发 recover（同飞书任务卡上的同名按钮），点完原地写 Covey 的回执；
    // 「不管了」告诉 Covey 从看板拿掉（P1-C3，以前的“先放着”只记在本机）
    const f: TurnFailure = v.last.failure ?? FALLBACK_FAILURE;
    wait = (
      <RecoverActions
        className="a-wb"
        failure={f}
        topicKey={topicKey}
        jobId={v.last.job_id}
        canSwitch={t.kind === 'topic'}
        extra={pressing && board && t.kind === 'topic' ? <DismissButton topicKey={topicKey} className="btn sm a-ghost" /> : null}
      />
    );
  }

  // 夜航 v2：顶部不再是三行表，而是一条结论带——一句大字（要你定的题、停住的原因或最新结论的第一句），
  // 两行补充，要你做的事带主按钮；“现在”收成带子底下一行；目标降成带子上方一行灰字。结论只在这里说一遍
  const nowTone: ToneKey = question ? 'waiting' : v.failed ? (pressing ? 'stopped' : 'cancelled') : toneOf(t);
  let label: React.ReactNode;
  let head: string;
  let rest: string = '';
  if (question) {
    label = <>等你 · 要你定{question.questions.length > 1 ? `（共 ${question.questions.length} 题）` : ''}</>;
    head = question.questions[0]?.question ?? '在等你的回答';
  } else if (v.failed && v.last) {
    const [h, r] = splitTitle((v.last.failure ?? FALLBACK_FAILURE).title);
    label = <>{pressing ? '停住了' : '已停止'} · 第 {lastRound?.n ?? t.round} 轮</>;
    head = h;
    rest = r;
  } else if (concl && doneT) {
    label = (
      <>最新结论{doneRound ? <> · <a className="a-lnk" role="link" onClick={() => nav(`?seq=${doneT.seq}`, { replace: true })}>第 {doneRound.n} 轮 ›</a></> : null}</>
    );
    head = firstSentence(concl, 80);
    rest = head.endsWith('…') ? '' : concl.slice(head.length).trim();
  } else {
    label = <>现在</>;
    head = p?.phase || (v.live ? `在做第 ${lastRound?.n ?? t.round} 轮` : t.title);
  }
  return (
    <section className={`a-ov ov2${mobile ? ' m' : ''}`} aria-label="话题总览">
      {!mobile ? <p className="ov2-goal" title={goal}><span>目标</span>{goal}</p> : null}
      <div className="ov2-band">
        <div className="ov2-l1">{label}</div>
        <h2 className="ov2-head">{head}</h2>
        {rest ? <p className="ov2-rest">{rest}</p> : null}
        {question ? (
          <div className="ov2-act">
            <button type="button" className="btn pri sm" onClick={() => document.getElementById('a-question')?.scrollIntoView({ block: 'start', behavior: 'smooth' })}>去回答</button>
          </div>
        ) : wait && v.failed ? <div className="ov2-act">{wait}</div> : null}
        {t.revisit || t.owner_send?.length ? (
          <div className="ov2-later">
            <LaterLine t={t} />
            <DraftLines drafts={t.owner_send} />
          </div>
        ) : null}
        {where && !(question && !v.live) ? (
          <div className="ov2-now">
            <Dot tone={nowTone} />
            <span className="ov2-nowl">现在</span>
            <span className="ov2-nowt">{where}</span>
          </div>
        ) : null}
      </div>
    </section>
  );
};

/** 手机：右栏变成总览下面的一条分段线；话题有记忆时（v31）下面一块“这个话题的记忆” */
export const MobileProgress: React.FC<{ topicKey: string }> = ({ topicKey }) => {
  const v: ProgressView = useProgressView(topicKey);
  const { data } = useTurns(topicKey);
  const v43 = useV43TopicEntries(topicKey);
  const mem = Array.isArray(v43)
    ? (v43.length ? <div className="a-mobmem"><MemoryList entries={v43} scope="topics" /></div> : null)
    : data?.memory?.length ? <div className="a-mobmem"><TopicMemory topicKey={topicKey} items={data.memory} /></div> : null;
  if (v.total <= 0) return mem;
  return (
    <>
      <SegBar done={v.done} total={v.total} state={segState(v)} className="a-mprog" />
      {mem}
    </>
  );
};

/** 右栏只放进度：步骤清单、预计还要多久，加所在专项（“最近动静”和时间线重复，夜航 v2 去掉） */
export const ProgressRail: React.FC<{ topicKey: string }> = ({ topicKey }) => {
  const v: ProgressView = useProgressView(topicKey);
  const { data: snap } = useSnapshot();
  const { data } = useTurns(topicKey);
  const v43 = useV43TopicEntries(topicKey);
  const now: number = useTick(v.live ? 1000 : 30000);
  const t: SnapshotTopic | undefined = v.topic;
  const p: TopicProgress | undefined = v.progress;
  const project: SnapshotProject | undefined = snap?.projects.find((x: SnapshotProject) => x.key === t?.project_key);
  const eta: string = etaText(p?.eta_seconds);
  // 进度说明：可能卡住时说它多久没动静（阈值是 Covey 的设置，不写死 30 分钟）；在等命令、这步比平均慢也说一句
  const note = v.live ? progressNote(p, now) : { text: '', stuck: false };
  const stuck: boolean = note.stuck;
  return (
    <aside className="a-rail" aria-label="进度">
      <h3>进度</h3>
      {v.total > 0 ? (
        <>
          <div className="a-rbig">
            <b>{stepsHeadline(v)}</b>
            {eta ? <span>预计{v.failed ? '继续后' : '还要'}{eta}</span> : v.live?.started_at ? <span>本轮已跑 {fmtDur(now - v.live.started_at, false)}</span> : null}
          </div>
          <SegBar done={v.done} total={v.total} state={segState(v)} />
        </>
      ) : (
        <div className="a-rbig">
          {/* 停住的跟看板走：点过不管了、看板不列了是灰的已停止，和页头、结论带一个说法 */}
          <b>{v.live ? '在做' : t ? toneWord(listTone(t)) : ''}</b>
          {v.live?.started_at ? <span>本轮已跑 {fmtDur(now - v.live.started_at, false)}</span> : eta ? <span>预计还要{eta}</span> : null}
        </div>
      )}
      {note.text ? <p className={note.stuck ? 'a-stuck' : 'a-slow'}>{note.text}</p> : null}
      {v.plan.length ? (
        <ol className="a-steps">
          {v.plan.map((s: PlanItem, i: number) => {
            const cur: boolean = s.state === 'doing';
            const fail: boolean = cur && v.failed;
            return (
              <li key={i} className={`a-st ${s.state === 'done' ? 'done' : fail ? 'fail' : cur ? 'cur' : 'todo'}`}>
                <span className="a-smk">
                  {s.state === 'done' ? <Check /> : fail ? <AlertTriangle /> : cur ? <Loader2 className="spin" /> : <i className="a-hole" />}
                </span>
                <span className="a-sn">
                  {s.title}
                  {fail ? <em>出错，待继续</em> : cur ? <em>{stuck ? '可能卡住' : '在做'}</em> : null}
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}
      {project ? (
        <>
          <h3>所在专项</h3>
          <Link className="a-rproj" to={`/projects/${encodeURIComponent(project.key)}`}>
            <b>{project.title}</b>
            <span>
              {project.counts.topics} 个话题
              {project.counts.waiting ? ` · ${project.counts.waiting} 等你` : ''}
              {project.counts.running ? ` · ${project.counts.running} 在做` : ''}
            </span>
          </Link>
        </>
      ) : null}
      {!v.total && !project ? <p className="a-note">这个话题还没有进度数据。</p> : null}
      {Array.isArray(v43) ? (v43.length ? <MemoryList entries={v43} scope="topics" /> : null) : <TopicMemory topicKey={topicKey} items={data?.memory} />}
    </aside>
  );
};

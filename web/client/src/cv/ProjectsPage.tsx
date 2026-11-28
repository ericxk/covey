import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight, Sparkles } from 'lucide-react';
import type { SnapshotProject, SnapshotResponse, SnapshotTopic } from '@shared/api.interface';
import { useSnapshot, useTidy } from '@client/src/hooks/use-workbench';
import { Head } from './Shell';
import { TidyButton } from './Tidy';
import { Dot, Pill, useTick } from './ui';
import { STATES, TONE_KEYS } from './look.gen';
import { byRecent, fmtDur, isRunningTone, isWaiting, pref, setPref, shortWhen, toneOf, TONE_RANK, type ToneKey } from './lib';
import { isStopped, listTone } from './topicText';
import { isHalted, leadOf, projectNote, projectPill, toneCounts } from './projectData';
import { briefOf, type ActionView, type Brief } from './briefData';
import { ActPill } from './ProjectBrief';
import { cut } from './progressData';
import { TopicBadge } from './RoleAvatar';
import './projects.css';
import './brief.css';

/** 专项一览（/projects）：手机底部「专项」标签打开的就是这一页（以前是侧栏抽屉，只有屏幕六成宽、和最近话题混在一起）。
 * 一张卡一个专项：状态胶囊、名字、目标一行、按状态的话题数和最后动静；卡下列出要紧的话题（等你、停住、在做，最多 HOT_SHOWN 个）和
 * 等你回答的负责人，点哪行进哪。要紧的专项排前面（和侧栏、看板一个排法）；安静的（没有要紧的话题，做完、收起或三天没动）收进“更早的专项”。 */

const HOT_SHOWN = 3;
const QUIET_DAYS = 3;
const HOT: readonly ToneKey[] = ['waiting', 'stopped', 'stuck', 'running'];

/** 一条细色条：按状态分段，段长是这个状态的话题数，要紧的在左（话题多时也不会挤出去） */
export const ToneBar: React.FC<{ tones: ToneKey[] }> = ({ tones }) => {
  const parts: [ToneKey, number][] = [...TONE_KEYS]
    .sort((a: ToneKey, b: ToneKey) => TONE_RANK[a] - TONE_RANK[b])
    .map((k: ToneKey): [ToneKey, number] => [k, tones.filter((v: ToneKey) => v === k).length])
    .filter(([, n]) => n > 0);
  if (!parts.length) return null;
  return (
    <span className="pl-bar" aria-hidden="true">
      {parts.map(([k, n]) => <i key={k} className={`tone-${k}`} style={{ flexGrow: n }} />)}
    </span>
  );
};

interface View {
  p: SnapshotProject;
  topics: SnapshotTopic[];
  tones: Map<string, ToneKey>;
  hot: SnapshotTopic[];
  lead: SnapshotTopic | undefined;
  last: number;
  rank: number;
}

/** 简报卡（v41）：背景一句、每个动作一行（状态和负责人那句话）、话题数和检查表数；点了进专项的「简报」。
 * 负责人还没写简报的专项只写检查表数（有检查表时），目标在上面的 .pl-goal 里 */
const BriefBlock: React.FC<{ b: Brief; base: string }> = ({ b, base }) => {
  const foot: string = `${b.members.length} 个话题（${b.liveN} 在跑）${b.total ? ` · 检查表 ${b.met}/${b.total} 满足` : ''}`;
  if (!b.has) {
    return b.total ? <Link to={base} className="pb-oc"><div className="pb-ocf">检查表 {b.met}/{b.total} 满足 · 负责人还没写背景和动作</div></Link> : null;
  }
  return (
    <Link to={`${base}/brief`} className="pb-oc">
      {b.background ? <div className="pb-ocl" title={b.background}><b>背景：</b>{cut(b.background, 80)}</div> : null}
      {b.actions.map((v: ActionView) => (
        <div key={v.action.id} className="pb-orow">
          <span className="pb-on">{v.n}</span>
          <span className="pb-otx" title={v.action.title}>{v.action.title}</span>
          <ActPill state={v.state} />
          {v.state !== 'done' && v.action.note ? <span className="pb-onote" title={v.action.note}>{cut(v.action.note, 24)}</span> : null}
        </div>
      ))}
      <div className="pb-ocf">{b.actions.length} 个动作 · {foot}<span className="pb-ogo short">点开看简报 ›</span></div>
    </Link>
  );
};

const ProjectCard: React.FC<{ x: View; now: number; snap: SnapshotResponse }> = ({ x, now, snap }) => {
  const { p, lead } = x;
  const brief: Brief = briefOf(p, snap, now);
  const base: string = `/projects/${encodeURIComponent(p.key)}`;
  const tones: ToneKey[] = x.topics.map((t: SnapshotTopic) => x.tones.get(t.key) ?? listTone(t));
  const pill = projectPill(p, tones, lead);
  const counts: [ToneKey, number][] = toneCounts(tones);
  const goal: string = projectNote(p);
  const leadWaits: boolean = !!lead && isWaiting(lead);
  const shown: SnapshotTopic[] = x.hot.slice(0, HOT_SHOWN);
  const more: number = x.hot.length - shown.length;
  return (
    <section className="pl-card">
      <Link to={base} className="pl-head">
        <div className="pl-l1">
          <b className="pl-name">{p.title}</b>
          <Pill tone={pill.tone} text={pill.text} />
        </div>
        {goal ? <div className="pl-goal">{goal}</div> : null}
        <div className="pl-meta">
          <ToneBar tones={tones} />
          <span className="pl-counts">
            {x.topics.length ? `${x.topics.length} 个话题` : '还没有话题'}
            {counts.map(([k, n]) => <span key={k} className={`pl-n tone-${k}`}> · {n} {STATES[k].word}</span>)}
          </span>
          <span className="pl-when short">{shortWhen(x.last, now)}</span>
        </div>
      </Link>
      <BriefBlock b={brief} base={base} />
      {leadWaits || shown.length ? (
        <div className="pl-hot">
          {leadWaits && lead ? (
            <Link to={base} className="pl-row">
              <Sparkles className="pl-lead" />
              <span className="pl-tt">负责人问你</span>
              <span className="pl-st short tone-waiting">{STATES.waiting.word}</span>
            </Link>
          ) : null}
          {shown.map((t: SnapshotTopic) => {
            const k: ToneKey = x.tones.get(t.key) ?? listTone(t);
            const sec: number | null = isRunningTone(k) && t.running?.started_at ? now - t.running.started_at : null;
            return (
              <Link key={t.key} to={`/topics/${encodeURIComponent(t.key)}`} className="pl-row">
                <Dot tone={k} />
                <TopicBadge number={t.number} />
                <span className="pl-tt">{t.title}</span>
                <span className={`pl-st short tone-${k}`}>{sec ? fmtDur(sec, false) : STATES[k].word}</span>
              </Link>
            );
          })}
          {more > 0 ? (
            <Link to={`${base}/graph`} className="pl-row pl-more">
              还有 {more} 个要紧的 <ChevronRight />
            </Link>
          ) : null}
        </div>
      ) : null}
    </section>
  );
};

const ProjectsPage: React.FC = () => {
  const { data: snap } = useSnapshot();
  const tidyOn: boolean = !!useTidy().data;
  const now: number = useTick(30000);
  const [oldOpen, setOldOpen] = useState<boolean>(() => pref('grp.old', '0') === '1');
  if (!snap) return null;
  const topics: SnapshotTopic[] = snap.topics.filter((t: SnapshotTopic) => t.kind === 'topic');
  const qKeys = new Set<string>(snap.questions.map((q) => q.topic_key));
  // 和侧栏一样读一个话题的状态：有题在等是等你；停住了跟看板走（点过“不管了”的不算）；其余按列表的颜色
  const toneFor = (t: SnapshotTopic): ToneKey => (qKeys.has(t.key) || isWaiting(t) ? 'waiting' : isStopped(t, now) ? 'stopped' : listTone(t));
  const views: View[] = snap.projects.map((p: SnapshotProject): View => {
    const own: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => t.project_key === p.key).sort(byRecent);
    const tones = new Map<string, ToneKey>(own.map((t: SnapshotTopic) => [t.key, toneFor(t)]));
    const k = (t: SnapshotTopic): ToneKey => tones.get(t.key) as ToneKey;
    const hot: SnapshotTopic[] = own.filter((t: SnapshotTopic) => HOT.includes(k(t))).sort((a: SnapshotTopic, b: SnapshotTopic) => TONE_RANK[k(a)] - TONE_RANK[k(b)] || byRecent(a, b));
    const lead: SnapshotTopic | undefined = leadOf(snap, p);
    const leadHot: boolean = !!lead && (isWaiting(lead) || isRunningTone(toneOf(lead)));
    const waitN: number = hot.filter((t: SnapshotTopic) => ['waiting', 'stopped', 'stuck'].includes(k(t))).length + (lead && isWaiting(lead) ? 1 : 0);
    return {
      p,
      topics: own,
      tones,
      hot,
      lead,
      last: Math.max(p.updated_at, ...own.map((t: SnapshotTopic) => t.updated_at)),
      // 护栏停住的专项自己就是一件停住了（要你处理），排最前
      rank: waitN || isHalted(p) ? 0 : hot.length || leadHot ? 1 : 2,
    };
  });
  const quiet = (x: View): boolean => x.rank === 2 && (['做完', '收起', '已取消'].includes(x.p.status) || now - x.last > QUIET_DAYS * 86400);
  const live: View[] = views.filter((x: View) => !quiet(x)).sort((a: View, b: View) => a.rank - b.rank || b.last - a.last || a.p.key.localeCompare(b.p.key));
  const old: View[] = views.filter(quiet).sort((a: View, b: View) => b.last - a.last);
  const act: number = live.filter((x: View) => x.rank === 0).length;
  const sub: string = views.length ? `${views.length} 个${act ? ` · ${act} 个要你处理` : ''}` : '';
  const toggleOld = (): void => {
    setPref('grp.old', oldOpen ? '0' : '1');
    setOldOpen(!oldOpen);
  };
  return (
    <>
      <Head title="专项" sub={sub || undefined} right={tidyOn ? <TidyButton /> : undefined} />
      <div className="scroll pl-page" style={{ '--tw': topics.reduce((most: number, t: SnapshotTopic) => Math.max(most, t.number ? String(t.number).length + 1 : 0), 4) } as React.CSSProperties}>
        <div className="pl-col">
          {!views.length ? (
            <p className="pl-empty">{tidyOn ? '还没有专项。对管家说一件要做很久的事，它会开成专项；或点右上角的「整理」，Covey 会看看哪些话题该归到一起。' : '还没有专项。对管家说一件要做很久的事，它会开成专项。'}</p>
          ) : null}
          {live.map((x: View) => <ProjectCard key={x.p.key} x={x} now={now} snap={snap} />)}
          {old.length ? (
            <section className={`pl-old${oldOpen ? ' open' : ''}`}>
              <button type="button" className="pl-oldh" onClick={toggleOld} aria-expanded={oldOpen}>
                <ChevronRight />
                <span>更早的专项</span>
                <em>{old.length}</em>
              </button>
              {oldOpen
                ? old.map((x: View) => (
                    <Link key={x.p.key} to={`/projects/${encodeURIComponent(x.p.key)}`} className="pl-orow">
                      <span className="pl-tt">{x.p.title}</span>
                      <span className="pl-om">{x.topics.length} 个话题 · {x.p.status || '安静'} · {shortWhen(x.last, now)}</span>
                    </Link>
                  ))
                : null}
            </section>
          ) : null}
        </div>
      </div>
    </>
  );
};

export default ProjectsPage;

import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Play } from 'lucide-react';
import type { QuestionView, SnapshotProject, SnapshotResponse, SnapshotTopic, StatsResponse, UsageDay } from '@shared/api.interface';
import { useIsDesktop } from '@client/src/hooks/use-breakpoint';
import { newOpId, prefetchTurns, useSettings, useSnapshot, useStats, useTidy, useUsage } from '@client/src/hooks/use-workbench';
import { oneLineText } from '@client/src/lib/workbench-format';
import { cleanLabel, isRec, quickAnswerable } from './Question';
import { Head, SyncDot } from './Shell';
import { ConclusionBand, Dot, Pill, RollNum, Spark, StateBar, StepCells, TodoBar, useTick } from './ui';
import { byNumberDesc, byRecent, fmtTok, hhmm, staleWords, toneOf, waitText, type ToneKey } from './lib';
import { BRAND, STATES } from './look.gen';
import { TidyPanel } from './Tidy';
import { etaText, FALLBACK_FAILURE, progressNote, splitTitle } from './topicText';
import { UsageBars } from './UsageBars';
import { cachePct, dayFigures, harnessShares, shownDays, spanOf, weekWord, type DayCell, type Share } from './usageData';
import { askCounts, asks, askSum, haltWords, isHalted, type Asks } from './projectData';
import { DismissButton, RecoverActions, useBoardOn } from './RecoverBits';
import { RoleAvatar, TopicBadge } from './RoleAvatar';
import { PaneHandle } from './PaneHandle';
import { BOARD_RAIL } from './panes';
import { sendQuiet, useOutboxOp } from './sendOp';
import { DraftPanel, ProjectOps, RevisitPanel } from './V34Bits';
import { QuotaBrief } from './QuotaBrief';
import './boardrail.css';

const topicPath = (t: SnapshotTopic): string => `/topics/${encodeURIComponent(t.key)}`;
const tno = (t: SnapshotTopic): string => (t.number ? `T${t.number}` : '');
/** 卡上写谁：话题是 T 号头像加标题，负责人是专项头像加“专项名 · 负责人”，管家是管家头像（和侧栏、大厅一套） */
const whoName = (t: SnapshotTopic, p: SnapshotProject | undefined): string =>
  t.kind === 'butler' ? '管家' : t.kind === 'lead' ? `${p?.title ?? '专项'} · 负责人` : t.title;
const Who: React.FC<{ t: SnapshotTopic; p: SnapshotProject | undefined }> = ({ t, p }) => (
  <>
    {t.kind === 'butler' ? <RoleAvatar name="butler" size={20} /> : t.kind === 'lead' ? <RoleAvatar name={p?.avatar} size={20} /> : <TopicBadge number={t.number} />}
    <span className="nb-who">{whoName(t, p)}</span>
  </>
);

/** 话题、管家、负责人的“要你处理”卡：等你、停住了、可能卡住。一张卡最多一个主按钮。
 * 等你：管家的题只能在飞书里答（Covey 不收网页替管家答），负责人的题在它的话题页答；停住了：按钮照 v33 的 failure 画，点了发 recover */
const ActCard: React.FC<{ t: SnapshotTopic; tone: ToneKey; snap: SnapshotResponse; q?: QuestionView }> = ({ t, tone, snap, q }) => {
  const nav = useNavigate();
  const qc = useQueryClient();
  const now: number = useTick(30000);
  // 看板上的快速答题（T765 F2）：点了就锁，写“已发出，等 Covey 处理…”，到回执才放开；办好了按钮一直锁着，等快照里这张卡消失
  const quickOp = useOutboxOp();
  const board: boolean = useBoardOn();
  const feishu: string = useSettings().data?.data?.feishu ?? '';
  const p: SnapshotProject | undefined = snap.projects.find((x: SnapshotProject) => x.key === t.project_key);
  const item = q?.questions[0];
  const butler: boolean = t.kind === 'butler';
  const quick: boolean = tone === 'waiting' && !butler && !!q && quickAnswerable(q) && (item?.options.length ?? 0) <= 3;
  const f = t.latest_failure;
  const since: string = waitText(Math.max(60, now - t.updated_at));
  let line: React.ReactNode;
  let meta: string;
  if (tone === 'waiting') {
    line = <>要你定：{item ? item.question : oneLineText(t.latest, 120) || '在等你的回复'}</>;
    meta = `等了 ${since}`;
  } else if (tone === 'stopped') {
    line = <>原因：{f ? splitTitle(f.title)[0] : '上一轮没做完'}</>;
    meta = `${hhmm(t.updated_at)} 停住`;
  } else {
    // 可能卡住：多久没动静从最后动静算（阈值是 Covey 的设置，不写死）
    const note = progressNote(t.progress, now);
    const quiet: string = note.text || `${waitText(Math.max(60, now - (t.progress?.last_activity_at ?? t.updated_at)))}没有新动静`;
    line = <>{t.progress?.last_activity ? `${t.progress.last_activity}，` : ''}{quiet}</>;
    meta = t.progress?.plan_total ? `${t.progress.plan_done ?? 0}/${t.progress.plan_total} 步` : '';
  }
  // 负责人的题在它的话题页答（专项页的负责人对话里没有问题面板）；管家的在大厅看、去飞书答
  const where: string = butler ? '/butler' : topicPath(t);
  const open = (): void => { void nav(where); };
  // 不管了：告诉 Covey 把话题从看板拿掉（同飞书看板卡），推回来 board 为空就不算要你处理了；不再在本机另记（P1-C3）
  const dismissBtn: React.ReactNode = board && t.kind === 'topic' ? <DismissButton topicKey={t.key} /> : null;
  return (
    <article className={`nb-act tone-${tone}`} onMouseEnter={() => prefetchTurns(qc, t.key)} onTouchStart={() => prefetchTurns(qc, t.key)}>
      <div className="nb-acth">
        <Pill tone={tone} />
        <span className="nb-meta">{meta}</span>
      </div>
      <Link to={where} className="nb-actt" title={t.kind === 'topic' && t.number ? `${tno(t)} ${t.title}` : undefined}><Who t={t} p={p} /></Link>
      <p className="nb-clamp2">{line}</p>
      {quick && q && item ? (
        <div className="nb-opts">
          {item.options.map((o) => (
            <button type="button" key={o.label} className={`btn sm${isRec(o) ? ' pri' : ''}`} disabled={quickOp.busy || quickOp.phase === 'done'} onClick={() => void quickOp.run({ op_id: newOpId(), kind: 'answer', topic_key: t.key, payload: { token: q.token, answers: { [item.id]: o.label }, note: '' } })} title="点一下就写入回答">
              {cleanLabel(o)}
            </button>
          ))}
          {quickOp.line ? <span className={`rc-said${quickOp.phase === 'failed' || quickOp.phase === 'unsent' ? ' bad' : ''}`} role={quickOp.phase === 'failed' || quickOp.phase === 'unsent' ? 'alert' : 'status'}>{quickOp.line}</span> : null}
        </div>
      ) : null}
      {tone === 'stopped' ? (
        <RecoverActions
          className="nb-btns"
          failure={f ?? FALLBACK_FAILURE}
          topicKey={t.key}
          jobId={t.latest_job_id}
          canSwitch={t.kind === 'topic'}
          noJob={open}
          // 换个模型：进话题页，输入框下沿的换配置面板直接展开（?switch=1）
          onSwitch={() => { void nav(`${topicPath(t)}?switch=1`); }}
          extra={<>{dismissBtn}<button type="button" className="btn sm" onClick={open}>打开话题</button></>}
        />
      ) : (
        <div className="nb-btns">
          {tone === 'waiting' ? (
            butler ? (
              <>
                {feishu ? <a className="btn sm pri" href={feishu} target="_blank" rel="noopener noreferrer">去飞书回答</a> : null}
                <button type="button" className={`btn sm${feishu ? '' : ' pri'}`} onClick={open}>打开大厅</button>
              </>
            ) : (
              <>
                {quick ? null : <button type="button" className="btn sm pri" onClick={open}>去回答</button>}
                <button type="button" className="btn sm" onClick={open}>打开话题</button>
              </>
            )
          ) : (
            <button type="button" className="btn sm pri" onClick={open}>打开话题</button>
          )}
          <span className="nb-meta nb-proj">{p && t.kind === 'topic' ? p.title : ''}</span>
        </div>
      )}
    </article>
  );
};

/** 护栏停住的专项（T647 P0-3，docs/ui.md 2.2）：红色“停住了 · 原因”和「继续」（发 reopen，同飞书停住卡的继续），算进要你处理 */
const HaltCard: React.FC<{ p: SnapshotProject }> = ({ p }) => {
  const nav = useNavigate();
  const op = useOutboxOp();
  const base: string = `/projects/${encodeURIComponent(p.key)}`;
  return (
    <article className="nb-act tone-stopped">
      <div className="nb-acth">
        <Pill tone="stopped" />
        <span className="nb-meta">{hhmm(p.updated_at)} 停住</span>
      </div>
      <Link to={base} className="nb-actt"><RoleAvatar name={p.avatar} size={20} /><span className="nb-who">{p.title}</span></Link>
      <p className="nb-clamp2">原因：{p.status_reason || '护栏停住了'}；点「继续」接着做</p>
      <div className="nb-btns">
        {op.phase === 'done' ? null : (
          <button type="button" className="btn sm pri" disabled={op.busy} onClick={() => void op.run({ op_id: newOpId(), kind: 'reopen', project_key: p.key, payload: {} })}>
            <Play /> 继续
          </button>
        )}
        {op.phase === 'done' ? null : <ProjectOps p={p} finishOnly toastDone />}
        <button type="button" className="btn sm" onClick={() => void nav(base)}>打开专项</button>
        <span className="nb-meta nb-proj">专项</span>
      </div>
      {op.line ? <div className={`nb-meta${op.phase === 'failed' || op.phase === 'unsent' ? ' a-errt' : ''}`}>{op.line}</div> : null}
    </article>
  );
};

/** 话题行 topic_row（在做，夜航 v2 的表格行）：状态点、T 号头像、标题和一行“现在”，右边预计、步骤格和步数；只有一条发丝线。
 * “现在”那行后面跟进度说明：在等一条命令跑完、这步比平均慢（slow_step、command，同飞书任务卡） */
const RunRow: React.FC<{ t: SnapshotTopic }> = ({ t }) => {
  const nav = useNavigate();
  const qc = useQueryClient();
  const now: number = useTick(30000);
  const r = t.running;
  const p = t.progress;
  const total: number = p?.plan_total ?? r?.plan_total ?? 0;
  const done: number = p?.plan_total ? (p.plan_done ?? 0) : (r?.plan_done ?? 0);
  // “现在”只认检查点写的阶段和计划里在做的那一步（同侧栏在做组）；不拿最新一轮的进展说明原文：那是模型边做边写的过程话，
  // 可能是英文或半句（T959 曾显示“Now implement: …”），进展另由下面的 progressNote 说
  const phase: string = p?.phase || r?.plan_doing || '在做';
  const note: string = progressNote(p, now).text;
  const eta: string = etaText(p?.eta_seconds);
  return (
    <div
      className="nb-trow tone-running"
      role="link"
      tabIndex={0}
      onClick={() => nav(topicPath(t))}
      onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') nav(topicPath(t)); }}
      onMouseEnter={() => prefetchTurns(qc, t.key)}
      onTouchStart={() => prefetchTurns(qc, t.key)}
    >
      <Dot tone="running" />
      <TopicBadge number={t.number} />
      <span className="nb-tt">
        <b>{t.title}</b>
        <small>{note ? `${phase} · ${note}` : phase}</small>
      </span>
      <span className="nb-eta">{eta}</span>
      {total ? <StepCells done={done} total={total} /> : <span />}
      <span className="nb-frac">{total ? `${done}/${total}` : ''}</span>
    </div>
  );
};

/** 有结果没看：一行，右边「记为看过」。点了先乐观记为看过（sendOp.sendQuiet），这一行跟着从“有结果没看”里拿掉，Covey 没办成再回来 */
const ResultRow: React.FC<{ t: SnapshotTopic }> = ({ t }) => {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [seen, setSeen] = useState<boolean>(false);
  const line: string = t.latest_role !== 'owner' ? oneLineText(t.latest, 90) : '';
  return (
    <div className="nb-row nb-res" role="link" tabIndex={0} onClick={() => nav(topicPath(t))} onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') nav(topicPath(t)); }}>
      <div className="nb-rowh">
        <Pill tone="result" />
        <TopicBadge number={t.number} />
        <b className="nb-rowt">{t.title}</b>
        <span className="nb-rowl nb-inl">{line}</span>
        {seen ? (
          <span className="nb-meta nb-right">已记为看过</span>
        ) : (
          <button
            type="button"
            className="btn sm nb-right"
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); setSeen(true); sendQuiet(qc, { op_id: newOpId(), kind: 'seen', topic_key: t.key, payload: {} }); }}
          >
            记为看过
          </button>
        )}
      </div>
    </div>
  );
};

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/** 近 24 小时（统计接口的 today_* 字段，窗口是往前 24 小时）：做完几轮、Agent 干了多久、答过的题平均等了你多久，三个数一排；
 *  有任务在排队时下面多一行。旧服务端没有这几个字段时不画 */
const DayPanel: React.FC<{ st: StatsResponse | undefined; queued: number }> = ({ st, queued }) => {
  const cells: DayCell[] = dayFigures(st);
  if (!cells.length) return null;
  return (
    <section className="nb-panel nb-day">
      <h3 className="nb-ph">近 24 小时</h3>
      <div className="nd-cells">
        {cells.map((c: DayCell) => (
          <div key={c.label}>
            <span className="nd-n">{c.n}{c.unit ? <small>{c.unit}</small> : null}</span>
            <span className="nd-l">{c.label}</span>
          </div>
        ))}
      </div>
      {queued ? <div className="nd-q">{queued} 个话题在排队</div> : null}
    </section>
  );
};

/** 本周用量下面一行：各 Harness 占的 token，一条灰度的分段条加一行字（颜色只表示状态，这里不上色）；只有一个 Harness 时不画 */
const HarnessShares: React.FC<{ rows: Share[] }> = ({ rows }) =>
  rows.length ? (
    <div className="nb-hs" title="近 7 天各 Harness 用的 token 占比">
      <span className="nb-hsbar">{rows.map((r: Share) => <i key={r.title} style={{ flexGrow: Math.max(r.pct, 0.5) }} />)}</span>
      <span className="nb-hst">{rows.map((r: Share) => `${r.title} ${r.text}`).join(' · ')}</span>
    </div>
  ) : null;

/** 本周用量：近 7 天（含今天）的 token，下面一行任务、话题和缓存命中，底部每天一根柱子，停上去看那天的数；「明细」去用量页。
 *  Covey 推了按天的用量（v26）才这样画，旧 Covey 照原来的趋势线 */
const WeekUsage: React.FC<{ st: StatsResponse | undefined }> = ({ st }) => {
  const { data: u } = useUsage();
  if (u) {
    const span = spanOf(u, 7);
    const pct: number | null = cachePct(span);
    const today: string = u.days[u.days.length - 1]?.date ?? '';
    return (
      <section className="nb-panel nb-usage">
        <h3 className="nb-ph">本周用量<em>近 7 天</em><Link className="nb-more" to="/usage">明细 ›</Link></h3>
        <div className="nb-big">{fmtTok(span.tokens)} <small>token</small></div>
        <div className="nb-meta">{span.tasks} 个任务{span.exact ? ` · ${span.topics} 个话题` : ''}{pct === null ? '' : ` · 缓存命中 ${pct}%`}</div>
        <HarnessShares rows={harnessShares(span.harnesses)} />
        <UsageBars
          days={shownDays(u, 7)}
          today={today}
          height={44}
          label={(d: UsageDay) => (d.date === today ? '今天' : weekWord(d.date))}
          asOf={hhmm(u.at)}
          name="近 7 天每天的 token，左右键看每一天"
        />
      </section>
    );
  }
  if (!st || st.week_tokens === undefined) return null;
  const days: string[] = Array.from({ length: 7 }, (_, i: number) => WEEKDAYS[new Date(Date.now() - (6 - i) * 86400000).getDay()]);
  return (
    <section className="nb-panel">
      <h3 className="nb-ph">本周用量<em>近 7 天</em></h3>
      <div className="nb-big">{fmtTok(st.week_tokens)} <small>token</small></div>
      <div className="nb-meta">{st.week_turns ?? 0} 个任务{typeof st.week_topics === 'number' ? ` · ${st.week_topics} 个话题` : ''}{typeof st.week_cache_pct === 'number' ? ` · 缓存命中 ${st.week_cache_pct}%` : ''}</div>
      <HarnessShares rows={harnessShares(st.week_harnesses)} />
      {st.week_series ? <Spark series={st.week_series} color={BRAND} className="nb-spark" h={40} /> : null}
      <div className="nb-days">{days.map((d: string, i: number) => <span key={i}>{d}</span>)}</div>
    </section>
  );
};

/** 最后同步的时刻；超过 3 分钟后面跟一句灰字“N 分钟没同步”（docs/ui.md 第 7 节，不用橙色） */
const Updated: React.FC<{ at: string | null | undefined }> = ({ at }) => {
  const now: number = useTick(15000);
  const stale: string = staleWords(at, now * 1000);
  const t: string = at ? hhmm(Date.parse(at) / 1000) : '';
  return <p className={`nb-updated${stale && at ? ' stale' : ''}`}>{t ? `更新于 ${t}${stale ? ` · ${stale}` : ''}` : '还没同步'}</p>;
};

/** 结论带下面那条“要你做”：最急的那一件是谁、什么事 */
function firstAsk(a: Asks, qBy: Map<string, QuestionView>, snap: SnapshotResponse): string {
  const proj = (t: SnapshotTopic): SnapshotProject | undefined => snap.projects.find((x: SnapshotProject) => x.key === t.project_key);
  const name = (t: SnapshotTopic): string => (t.kind === 'topic' ? tno(t) || t.title : whoName(t, proj(t)));
  if (a.waiting[0]) return `${name(a.waiting[0])}：${qBy.get(a.waiting[0].key)?.questions[0]?.question ?? a.waiting[0].title}`;
  if (a.stopped[0]) return `${name(a.stopped[0])}：${a.stopped[0].latest_failure ? splitTitle(a.stopped[0].latest_failure.title)[0] : '上一轮没做完'}`;
  if (a.projects[0]) return `${a.projects[0].title}：${haltWords(a.projects[0])}`;
  if (a.stuck[0]) return `${name(a.stuck[0])}：可能卡住了`;
  return '';
}

const Board: React.FC = () => {
  const desktop: boolean = useIsDesktop();
  const { data: snap } = useSnapshot();
  const { data: st } = useStats();
  const tidy = useTidy().data;
  const now: number = useTick(30000);
  if (!snap) return null;
  const topics: SnapshotTopic[] = snap.topics.filter((t: SnapshotTopic) => t.kind === 'topic');
  const qBy = new Map<string, QuestionView>(snap.questions.map((q: QuestionView) => [q.topic_key, q]));
  // 要你处理：和侧栏角标、手机标签栏同一个算法（projectData.ts asks，口径同 covey board_read）：管家、负责人的题也算，护栏停住的专项算停住了，
  // 整理专项要你定的建议算等你（下面的整理面板画它们）
  const a: Asks = asks(snap, now, tidy);
  const { waiting, stopped, stuck, projects: halted } = a;
  // 数就是下面画出来的卡片数加整理面板里要你定的建议数（askCounts），不拿 Covey 另推的 asks 条目盖（T978）
  const c = askCounts(a);
  const act: number = askSum(c);
  const running: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => toneOf(t) === 'running' && !qBy.has(t.key)).sort(byNumberDesc);
  const unseen: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => toneOf(t) === 'result').sort(byRecent);
  const queued: number = topics.filter((t: SnapshotTopic) => toneOf(t) === 'queued').length;
  // Covey 停推了（超过 3 分钟没同步）：右上和结论带都照实说，不再写“已同步”“都在掌握中”（T647 P1-C1）
  const stale: string = staleWords(snap.synced_at, now * 1000);
  const syncedAt: number | null = snap.synced_at ? Date.parse(snap.synced_at) / 1000 : null;
  // 结论带：按 look 的 PRESSING 说最急的；等你、停住了时数字旁的点慢慢呼吸
  const breathe: ToneKey | null = c.waiting ? 'waiting' : c.stopped ? 'stopped' : null;
  const sub: string[] = [];
  let n: number | null = null;
  let words = '都在掌握中';
  if (act) {
    n = act;
    words = '件事要你处理';
  } else if (unseen.length) {
    n = unseen.length;
    words = '个结果没看';
  } else if (running.length) {
    n = running.length;
    words = '件在做';
  }
  if (running.length && words !== '件在做') sub.push(`${running.length} 件在做`);
  if (unseen.length && words !== '个结果没看') sub.push(`${unseen.length} 个结果没看`);
  if (!n) {
    if (stale) words = syncedAt ? `${hhmm(syncedAt)} 时没有事要你处理` : '还没同步';
    else sub.push('现在没有在等你的事');
  }
  if (!act && n) sub.unshift(stale ? '没有事等你' : '没有事等你，都在掌握中');
  // 停推的那句单独一行在最前（灰字），不和件数挤在一句里折行
  const staleLine: string = stale && syncedAt ? `Covey ${stale}，这里是 ${hhmm(syncedAt)} 的样子` : '';
  const firstLine: string = firstAsk(a, qBy, snap);
  const band = (
    <ConclusionBand
      label={<span className="nb-l1t">{stale && syncedAt ? `${hhmm(syncedAt)} 时` : '现在'}</span>}
      n={n}
      words={words}
      breathe={breathe}
      sub={staleLine ? <><span className="nb-stale">{staleLine}</span>{sub.length ? <><br />{sub.join('，')}</> : null}</> : sub.join('，')}
      counts={[['waiting', c.waiting], ['stopped', c.stopped], ...(c.stuck ? [['stuck', c.stuck] as [ToneKey, number]] : []), ['result', unseen.length], ['running', running.length]]}
    >
      {/* 只有 1 件时下面的卡就是它，结论带不再重复 */}
      {act > 1 && firstLine ? <TodoBar>{oneLineText(firstLine, 60)}</TodoBar> : null}
      <StateBar legend={false} parts={[['waiting', c.waiting], ['stopped', c.stopped], ['stuck', c.stuck], ['result', unseen.length], ['running', running.length]]} />
    </ConclusionBand>
  );
  // 右栏只放跨话题的资源和安排（T973）：额度（有来源才画）→ Covey 定时回看（有才画）→ 近 24 小时 → 本周用量。
  // 话题、专项的清单在侧栏，这里不再列（原来的专项、最近动态和侧栏重复）
  const rail = (
    <>
      <QuotaBrief />
      <RevisitPanel topics={topics} />
      <DayPanel st={st} queued={queued} />
      <WeekUsage st={st} />
    </>
  );
  // 卡和行里的 T 号头像一样宽（按最长的号码，至少 T 加三位），标题对成一条线（同侧栏 --tw）
  const logoChars: number = topics.reduce((most: number, t: SnapshotTopic) => Math.max(most, t.number ? String(t.number).length + 1 : 0), 4);
  return (
    <div className="a-board nb-board" style={{ '--tw': logoChars } as React.CSSProperties}>
      <div className="a-center">
        <Head title="看板" right={<span className={`nb-synced${stale ? ' stale' : ''}`}><SyncDot at={snap.synced_at} />{stale || (desktop ? '已同步' : '')}</span>} />
        <div className="a-scroll nb-scroll">
          {band}
          {waiting.length + stopped.length + halted.length + stuck.length ? (
            <div className="nb-actgrid">
              {waiting.map((t: SnapshotTopic) => <ActCard key={t.key} t={t} tone="waiting" q={qBy.get(t.key)} snap={snap} />)}
              {stopped.map((t: SnapshotTopic) => <ActCard key={t.key} t={t} tone="stopped" snap={snap} />)}
              {halted.map((p: SnapshotProject) => <HaltCard key={p.key} p={p} />)}
              {stuck.map((t: SnapshotTopic) => <ActCard key={t.key} t={t} tone="stuck" snap={snap} />)}
            </div>
          ) : null}
          <TidyPanel />
          {/* 顺序照 docs/ui.md 5.1：要你处理（含等你在飞书确认的代发）→ 告诉你一声（有结果没看）→ 背景（在做）。
              在做排最后：十几个在做时它占满一屏，排在前面会把代发和有结果挤出首屏（T973） */}
          <DraftPanel topics={topics} />
          {unseen.length ? (
            <>
              <h2 className="nb-sh">{STATES.result.heading}<em>{unseen.length}</em></h2>
              <div className="nb-rows">{unseen.slice(0, 12).map((t: SnapshotTopic) => <ResultRow key={t.key} t={t} />)}</div>
            </>
          ) : null}
          {running.length ? (
            <>
              <h2 className="nb-sh">{STATES.running.heading}<em><RollNum value={running.length} /></em></h2>
              <div className="nb-rows">{running.map((t: SnapshotTopic) => <RunRow key={t.key} t={t} />)}</div>
            </>
          ) : null}
          {!desktop ? <div className="nb-mrail">{rail}</div> : null}
          {!topics.length ? (
            <div className="nb-empty">
              <b>还没有话题</b>
              <span>在飞书里给 Covey 发一句要做的事，开出话题后这里就会列出来。</span>
            </div>
          ) : null}
          <Updated at={snap.synced_at} />
        </div>
      </div>
      {desktop ? <PaneHandle spec={BOARD_RAIL} /> : null}
      {desktop ? <aside className="nb-rail">{rail}</aside> : null}
    </div>
  );
};
export default Board;

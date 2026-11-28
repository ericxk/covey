import React, { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, Sparkles } from 'lucide-react';
import type { SnapshotProject } from '@shared/api.interface';
import { newOpId, useSnapshot, useTidy } from '@client/src/hooks/use-workbench';
import { errorText } from '@client/src/lib/workbench-format';
import { pref, setPref } from './lib';
import { opWords, plainReceipt } from './opWords';
import { sendOp, trackedOf, trackOp, useSendOutbox, useTrackedOp, type OpResult } from './sendOp';
import {
  choice, dismissible, failedRun, failWords, isOpen, look, nextWords, outcome, panelWords, progressShare, remember, shown,
  wantsSettings, TIDY_SHOW_ALL, TIDY_WORD, type TidyChoice, type TidyLastRun, type TidyLook, type TidyProposal, type TidyTopic,
} from './tidyData';
import './tidy.css';

/** 点「知道了」时全部建议的样子（tidyData.ts remember）；点收起时要你定的是哪几条（tidyData.ts startOpen） */
const GONE_PREF = 'tidy.gone';
const FOLD_PREF = 'tidy.fold';

function readGone(): string[] {
  try {
    const v: unknown = JSON.parse(pref(GONE_PREF, '[]'));
    return Array.isArray(v) ? v.filter((x: unknown): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

const TIDY_KEY = 'tidy.op';
const TIDY_SHOW_MS = 30 * 60 * 1000;

/** 「整理」：发件箱写一条 tidy，按钮旁边原地显示 Covey 回的结果（说法同 opWords：等满一分钟没回音不算失败）。只在开了整理专项时出现，
 * 放在侧栏「专项」小节的标题行和专项一览的页头（原来在看板右栏的专项块上，T973 去掉了那一块）。
 * 回执由跟踪器问（cv/sendOp.ts，T765 F1）；刷新过页面还在 30 分钟内的那条，打开时接着问 */
export const TidyButton: React.FC<{ compact?: boolean }> = ({ compact }) => {
  const send = useSendOutbox();
  const qc = useQueryClient();
  const [op, setOp] = useState<{ id: string; at: number } | null>(() => {
    try {
      const v = JSON.parse(pref(TIDY_KEY, 'null')) as { id: string; at: number } | null;
      return v && Date.now() - v.at < TIDY_SHOW_MS ? v : null;
    } catch {
      return null;
    }
  });
  const [err, setErr] = useState<string>('');
  const tr = useTrackedOp(op?.id);
  useEffect(() => {
    // 刷新页面以后小仓库是空的：接着问这一条的回执
    if (op && !trackedOf(op.id)) void trackOp(qc, { op_id: op.id, kind: 'tidy', payload: { scope: '' } });
  }, [op, qc]);
  const go = (): void => {
    const id: string = newOpId();
    setErr('');
    send.mutate(
      { op_id: id, kind: 'tidy', payload: { scope: '' } },
      {
        onSuccess: () => {
          const v = { id, at: Date.now() };
          setPref(TIDY_KEY, JSON.stringify(v));
          setOp(v);
        },
        onError: (e: Error) => setErr(errorText(e)),
      },
    );
  };
  const waiting: boolean = !!op && (!tr || tr.phase === 'waiting');
  const late: boolean = tr?.phase === 'timeout';
  const busy: boolean = send.isPending || waiting;
  // 回执的话和颜色（说法同 opWords）；Covey 回的话照原样（新版说进度和结果显示在整理面板上，旧版说发到私聊）
  let note: [string, 'a-errt' | 'a-faint' | 'a-okt'] | null = null;
  if (err) note = [opWords('unsent', err), 'a-errt'];
  else if (send.isPending) note = [opWords('sending'), 'a-faint'];
  else if (waiting) note = [opWords('waiting'), 'a-faint'];
  else if (late) note = [opWords('timeout'), 'a-faint'];
  else if (tr?.phase === 'done') note = [opWords('done', tr.message || '已开始整理'), 'a-okt'];
  else if (tr?.phase === 'failed') note = [opWords('failed', tr.message), 'a-errt'];
  const tip = '把现有专项和最近 7 天的话题重新看一遍，给出归进、新建、合并等建议';
  // compact（侧栏「专项」标题行，宽 248px）：只放按钮，回执写进悬停提示，没发出、没办成时按钮字变红；整理的进度和结果在看板的整理面板上
  if (compact) {
    return (
      <span className="a-tidy compact">
        <button type="button" className={`btn sm txt${note && note[1] === 'a-errt' ? ' a-errt' : ''}`} onClick={go} disabled={busy} title={note ? `${note[0]}\n\n${tip}` : tip} aria-label={note ? `整理：${note[0]}` : '整理'}>
          <Sparkles /> {busy ? '整理中' : '整理'}
        </button>
      </span>
    );
  }
  return (
    <span className="a-tidy">
      {note ? <span className={note[1]}>{note[0]}</span> : null}
      <button type="button" className="btn sm" onClick={go} disabled={busy} title={tip}>
        <Sparkles /> 整理
      </button>
    </span>
  );
};

/** 话题列表：不多于 4 个全列；多于 4 个先列前 3 个，其余折成“还有 N 个话题”，点开展开。T 号是链接 */
const TopicList: React.FC<{ topics: TidyTopic[] }> = ({ topics }) => {
  const [open, setOpen] = useState<boolean>(false);
  const fold: boolean = topics.length > TIDY_SHOW_ALL && !open;
  const list: TidyTopic[] = fold ? topics.slice(0, 3) : topics;
  return (
    <ul className="td-topics">
      {list.map((t: TidyTopic) => (
        <li key={t.key}>
          <Link to={`/topics/${encodeURIComponent(t.key)}`} className="nb-tno">{t.label}</Link>
          <span className="td-tt">{t.title}</span>
        </li>
      ))}
      {fold ? (
        <li>
          <button type="button" className="td-more" aria-expanded={false} onClick={() => setOpen(true)}>还有 {topics.length - 3} 个话题<ChevronDown /></button>
        </li>
      ) : null}
    </ul>
  );
};

/** 最近一次没整理成（T884）：红字原因（整理卡上那句，带问的是哪个 Harness），下一步，再试没用时「去设置」换辅助模型 */
const FailNote: React.FC<{ r: TidyLastRun }> = ({ r }) => (
  <div className="td-fail" role="alert">
    <p className="td-fail-w">{failWords(r)}</p>
    <p className="td-fail-n">{nextWords(r)}</p>
    {wantsSettings(r) ? <Link className="btn sm td-fail-go" to="/settings/config">去设置</Link> : null}
  </div>
);

/** 正在整理的进度条：知道读了几个、一共几个才画 */
const RunBar: React.FC<{ l: TidyLook }> = ({ l }) => {
  const share: number | null = progressShare(l);
  if (share === null) return null;
  return (
    <div className="td-bar" role="progressbar" aria-valuemin={0} aria-valuemax={l.running?.total ?? 0} aria-valuenow={l.running?.done ?? 0}>
      <i style={{ width: `${Math.round(share * 100)}%` }} />
    </div>
  );
};

/** 来源、原因、目标、话题：要你定的一块里直接摆出来，定好的一行点开才有 */
const Facts: React.FC<{ p: TidyProposal }> = ({ p }) => (
  <>
    {p.from ? <p className="td-from">{p.from}</p> : null}
    {p.why ? <p className="td-why">原因：{p.why}</p> : null}
    {p.goal ? <p className="td-goal">目标：{p.goal}</p> : null}
    {p.topics.length ? <TopicList topics={p.topics} /> : null}
  </>
);

/** late：等满一分钟 Covey 还没回音（不是失败，操作还在发件箱里；按钮放开，再点一次 Covey 会说定过没有） */
type Phase = { phase: 'saving' | 'done' | 'failed' | 'late'; message: string };

/** 要你定的一条：一块，点按钮发 tidy_decide，等 Covey 回结果，结果原地显示在这一块底部；新快照到了它就变成定好的一行 */
const OpenCard: React.FC<{ p: TidyProposal; onDecided: (id: string) => void }> = ({ p, onDecided }) => {
  const qc = useQueryClient();
  const [ph, setPh] = useState<Phase | null>(null);
  const alive = useRef<boolean>(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const decide = async (choice: 'ok' | 'decline'): Promise<void> => {
    const put = (x: Phase): void => { if (alive.current) setPh(x); };
    onDecided(p.id);
    put({ phase: 'saving', message: choice === 'ok' ? `正在${p.verb}…` : opWords('sending') });
    // 说法同 opWords（P1-C2）：没发出去、没办成写原因；等满一分钟没回音不算失败，操作还在发件箱里
    // 回执后整理建议和快照由跟踪器取（opTrack.pullPlan：先取一次，再每 0.5 秒补取几次），这里不再硬等 1.5 秒、4 秒
    const r: OpResult = await sendOp({ op_id: newOpId(), kind: 'tidy_decide', payload: { proposal: p.id, choice } }, qc, undefined, () => !alive.current);
    if (r.phase === 'done' || r.phase === 'failed') {
      put({ phase: r.phase, message: r.phase === 'done' ? (plainReceipt(r.message) || TIDY_WORD[choice === 'ok' ? 'done' : 'declined']) : opWords('failed', r.message) });
      return;
    }
    put({ phase: r.phase === 'timeout' ? 'late' : 'failed', message: opWords(r.phase, r.message) });
  };
  const busy: boolean = ph?.phase === 'saving';
  return (
    <article className="td-a">
      <div className="td-head">
        <span className="td-tag">{p.tag}</span>
        <b className="td-title">{p.headline}</b>
      </div>
      <Facts p={p} />
      {ph?.phase !== 'done' ? (
        <div className="td-btns">
          <button type="button" className="btn sm pri" disabled={busy} onClick={() => void decide('ok')}>{p.verb}</button>
          <button type="button" className="btn sm txt" disabled={busy} onClick={() => void decide('decline')}>不用</button>
        </div>
      ) : null}
      {ph ? <p className={`td-settled${ph.phase === 'failed' ? ' st-failed' : ''}`} role={ph.phase === 'failed' ? 'alert' : 'status'}>{ph.message}</p> : null}
    </article>
  );
};

/** 定好的一条：一行（标签、标题、几个话题、结果词，照办了的带「去专项」），点开看来源、原因、目标、话题和 Covey 写的结果。
    said：刚在这里定的，结果那句直接写在行下面 */
const SettledRow: React.FC<{ p: TidyProposal; project: string; said: boolean }> = ({ p, project, said }) => {
  const [open, setOpen] = useState<boolean>(false);
  const id: string = `td-${useId().replace(/:/gu, '')}`;
  const o = outcome(p);
  // Covey 写的结果照原样说，只去掉开关名这类内部词（P2：整理回执不露开关名）
  const note: string = plainReceipt(p.note) || TIDY_WORD[p.state] || '';
  const bad: boolean = o.tone === 'bad';
  return (
    <div className={`td-row${open ? ' open' : ''}`}>
      <button type="button" className="td-rt" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} title={note}>
        <span className="td-tag">{p.tag}</span>
        <span className="td-rh">{p.headline}</span>
        {p.topics.length > 1 ? <span className="td-rn">{p.topics.length} 个话题</span> : null}
        <span className={`td-out ${o.tone}`}>{o.word}</span>
        <ChevronDown className="td-chev" />
      </button>
      <span className="td-go-slot">
        {project ? <Link className="td-go" to={`/projects/${encodeURIComponent(project)}`}>去专项 ›</Link> : null}
      </span>
      {said && !open && note ? <p className={`td-said${bad ? ' st-failed' : ''}`}>{note}</p> : null}
      {open ? (
        <div className="td-detail" id={id}>
          <Facts p={p} />
          {note ? <p className={`td-settled${bad ? ' st-failed' : ''}`}>{note}</p> : null}
        </div>
      ) : null}
    </div>
  );
};

/** 整理专项给你的建议，看板“要你处理”下面：
    - 有要你定的：展开，一条一块，能直接定；标题栏「收起」收成一行（记在这一批名下，来了新的再展开）；
    - 都定好了：收成一行，「展开」看一行一条，点一行看原因和话题，照办了的「去专项」；「知道了」收走，有新建议或结果变了再出来；
    - 有要你定的时，定好了的收进底下“已定 N 条”；刚在这里定的留在原处成一行。没推过、或一条都没有就不显示 */
export const TidyPanel: React.FC = () => {
  const q = useTidy();
  const { data: snap } = useSnapshot();
  const [gone, setGone] = useState<string[]>(readGone);
  const [picked, setPicked] = useState<TidyChoice | null>(null);
  const [mine, setMine] = useState<string[]>([]);
  const [rest, setRest] = useState<boolean>(false);
  const d = q.data;
  if (!d) return null;
  const l: TidyLook = look(d);
  if (!shown(l, gone)) return null;
  const w = panelWords(l);
  const open: boolean = isOpen(l, picked, pref(FOLD_PREF, ''));
  const canGo: boolean = dismissible(l);
  const fold = (): void => {
    setPicked(choice(l, false));
    if (l.open.length) setPref(FOLD_PREF, l.batch);
  };
  const unfold = (): void => {
    setPicked(choice(l, true));
    setPref(FOLD_PREF, '');
  };
  const dismiss = (): void => {
    const g: string[] = remember(l, gone);
    setPref(GONE_PREF, JSON.stringify(g));
    setGone(g);
    setPicked(null);
    setMine([]);
  };
  const fail: TidyLastRun | null = failedRun(l);
  if (!l.open.length && !l.settled.length) {
    // 一条建议都没有：正在整理就画进度，没整理成就画原因和「去设置」（T884：网页上点的整理在这里看得到）
    return (
      <section className="nb-panel td-panel td-solo" aria-label="整理专项">
        <h3 className="nb-ph">整理专项
          {w.run ? <em>· {w.run}</em> : fail ? <em className="st-failed">· 没整理成</em> : null}
          <span className="td-acts">
            {canGo ? <button type="button" className="td-act" onClick={dismiss} title="从看板收走；下次没整理成会再出来">知道了</button> : null}
          </span>
        </h3>
        {l.running ? <RunBar l={l} /> : null}
        {fail ? <FailNote r={fail} /> : null}
      </section>
    );
  }
  const projects = new Set<string>((snap?.projects ?? []).map((x: SnapshotProject) => x.key));
  const row = (p: TidyProposal): React.ReactNode => (
    <SettledRow key={p.id} p={p} project={p.project && projects.has(p.project) ? p.project : ''} said={mine.includes(p.id)} />
  );
  if (!open) {
    return (
      <div className={`td-line${l.open.length ? ' asking' : ''}`}>
        <button type="button" className="td-lm" onClick={unfold} aria-expanded={false}>
          <span className="td-tag">整理专项</span>
          {w.state ? <span className="td-state">{w.state}</span> : null}
          {w.count ? <span className="td-cnt td-mn">{w.count}</span> : null}
          {w.head ? <b>{w.head}</b> : null}
          {w.more ? <span className="td-cnt td-lmore">{w.more}</span> : null}
          {w.run ? <span className="td-cnt">{w.run}</span> : null}
          {w.failed ? <span className="td-cnt st-failed">{w.failed}</span> : null}
          {w.fail ? <span className="td-cnt st-failed">{w.fail}</span> : null}
          <span className="td-open"><span className="td-ot">展开</span><ChevronDown /></span>
        </button>
        {canGo ? <button type="button" className="td-x" onClick={dismiss} title="从看板收走；有新建议或结果变了会再出来">知道了</button> : null}
      </div>
    );
  }
  const here: TidyProposal[] = l.settled.filter((p: TidyProposal) => mine.includes(p.id));
  const others: TidyProposal[] = l.settled.filter((p: TidyProposal) => !mine.includes(p.id));
  const tuck: boolean = l.open.length > 0 && others.length > 0;
  return (
    <section className="nb-panel td-panel" aria-label="整理专项">
      <h3 className="nb-ph">整理专项
        {w.state ? <em className={l.open.length ? 'asking' : undefined}>· {w.state}</em> : null}
        {w.run && w.run !== w.state ? <span className="td-run">{w.run}</span> : null}
        {w.failed ? <span className="td-run st-failed">{w.failed}</span> : null}
        <span className="td-acts">
          {canGo ? <button type="button" className="td-act" onClick={dismiss} title="从看板收走；有新建议或结果变了会再出来">知道了</button> : null}
          <button type="button" className="td-act" onClick={fold} aria-expanded>收起<ChevronUp /></button>
        </span>
      </h3>
      {l.running ? <RunBar l={l} /> : null}
      {fail ? <FailNote r={fail} /> : null}
      {l.open.length ? (
        <div className="td-list">
          {l.open.map((p: TidyProposal) => (
            <OpenCard key={p.id} p={p} onDecided={(id: string) => { setPicked(choice(l, true)); setMine((m: string[]) => (m.includes(id) ? m : [...m, id])); }} />
          ))}
        </div>
      ) : null}
      {here.length || (others.length && !tuck) ? (
        <div className="td-rows">
          {here.map(row)}
          {tuck ? null : others.map(row)}
        </div>
      ) : null}
      {tuck ? (
        <>
          <button type="button" className={`td-tuck${rest ? ' open' : ''}`} aria-expanded={rest} onClick={() => setRest(!rest)}>已定 {others.length} 条<ChevronDown /></button>
          {rest ? <div className="td-rows">{others.map(row)}</div> : null}
        </>
      ) : null}
    </section>
  );
};
export default TidyPanel;

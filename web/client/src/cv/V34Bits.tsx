import React, { useState, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';
import { CalendarClock, Play, Send } from 'lucide-react';
import { toast } from 'sonner';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import type { OutboxRequest, OwnerSendDraft, RoomResponse, SnapshotProject, SnapshotTopic, TurnsResponse, TurnView } from '@shared/api.interface';
import { newOpId, ROOM_KEY, turnsKey, useSettings } from '@client/src/hooks/use-workbench';
import { opWords } from './opWords';
import { canFinish, canStop, isHalted } from './projectData';
import { TopicBadge } from './RoleAvatar';
import { useOutboxOp, type OpResult, type OutboxOp } from './sendOp';
import { useTick } from './ui';
import { canCancelRevisit, canDo, draftWords, revisitMark, revisitWords } from './v34';
import './v34.css';

/* 数据约定 v34 在网页上的几样：专项的收尾、停止，到点回来看，等你在飞书确认的代发草稿，记忆的「不对」。
 * 新按钮只在 Covey 认这个操作（settings.ops）时画：旧 Covey 照旧只读，不会点了才说“不认识的操作”。 */

/** 这个 Covey 认的网页操作（v34 settings.ops）；旧 Covey 是 undefined */
export function useOps(): readonly string[] | undefined {
  return useSettings().data?.data?.ops;
}

type ProjectKind = 'finish' | 'stop' | 'again';

/* 专项上点过的那一颗的回执，记在这一页里（内存，不存本机，10 分钟）：护栏停住的那条收尾后专项接着做，停住条换成普通的按钮行，
 * 回执跟着留在新的那一行（同 RecoverBits 的 useRecentSaid） */
interface ProjectSaid {
  kind: ProjectKind;
  line: string;
  ok: boolean;
  at: number;
}
let projectSaid: ReadonlyMap<string, ProjectSaid> = new Map<string, ProjectSaid>();
const saidListeners = new Set<() => void>();
const SAID_MS = 10 * 60 * 1000;
function putProjectSaid(key: string, v: ProjectSaid): void {
  projectSaid = new Map<string, ProjectSaid>([...projectSaid, [key, v]]);
  saidListeners.forEach((l: () => void) => l());
}
function useProjectSaid(key: string): ProjectSaid | null {
  const all: ReadonlyMap<string, ProjectSaid> = useSyncExternalStore(
    (cb: () => void) => {
      saidListeners.add(cb);
      return () => saidListeners.delete(cb);
    },
    () => projectSaid,
  );
  const v: ProjectSaid | undefined = all.get(key);
  return v && Date.now() - v.at < SAID_MS ? v : null;
}

const KIND_OP: Record<ProjectKind, OutboxRequest['kind']> = { finish: 'project_finish', stop: 'project_stop', again: 'reopen' };

/** 专项的「收尾」「停止」（同飞书专项卡的停止专项、停住卡的收尾）：收尾叫负责人总结后结束（有在做、等你的话题，或目标检查表没满足时
 * Covey 照负责人的规矩拒绝，原样说为什么）；停止先问一句再停，免得误点。你自己停的专项另有「继续」（reopen，老操作，旧 Covey 也认），
 * 停完原地就能接着做。回执原地写最后点的那一个；toastDone：所在的卡办成后会离开看板（护栏停住的专项卡），回执另用 toast 说 */
export const ProjectOps: React.FC<{ p: SnapshotProject; finishOnly?: boolean; className?: string; wrap?: boolean; toastDone?: boolean }> = ({
  p,
  finishOnly,
  className,
  wrap,
  toastDone,
}) => {
  const ops = useOps();
  const recent: ProjectSaid | null = useProjectSaid(p.key);
  const each: Record<ProjectKind, OutboxOp> = { finish: useOutboxOp(), stop: useOutboxOp(), again: useOutboxOp() };
  const [asking, setAsking] = useState<boolean>(false);
  const [last, setLast] = useState<ProjectKind | ''>('');
  const mayFinish: boolean = canDo(ops, 'project_finish') && canFinish(p);
  const mayStop: boolean = !finishOnly && canDo(ops, 'project_stop') && canStop(p);
  const mayAgain: boolean = !finishOnly && p.status === '停住' && !isHalted(p);
  const mine: OutboxOp | null = last ? each[last] : null;
  // 这一行自己点的在等或刚回来就写它；否则写这一页 10 分钟内在这个专项上点过的（停住条换成按钮行后接着写）
  const said: { line: string; bad: boolean } | null = mine?.line
    ? { line: mine.line, bad: mine.phase === 'failed' || mine.phase === 'unsent' }
    : recent
      ? { line: recent.line, bad: !recent.ok }
      : null;
  if (!mayFinish && !mayStop && !mayAgain && !said) return null;
  const busy: boolean = each.finish.busy || each.stop.busy || each.again.busy;
  // 办成了的那颗先收起来，等 Covey 推回新状态；点了别的（停了又继续）就回来
  const shown = (k: ProjectKind): boolean => (last ? !(last === k && each[k].phase === 'done') : !(recent?.kind === k && recent.ok));
  const act = async (k: ProjectKind): Promise<void> => {
    setAsking(false);
    setLast(k);
    // keep：停住条、专项卡办成后会换掉或离开，回执照样等到并记下
    const r: OpResult = await each[k].run({ op_id: newOpId(), kind: KIND_OP[k], project_key: p.key, payload: {} }, true);
    const line: string = opWords(r.phase, r.message);
    putProjectSaid(p.key, { kind: k, line, ok: r.phase === 'done', at: Date.now() });
    if (toastDone && r.phase === 'done') toast.success(line);
  };
  const body = (
    <span className={`pj-ops${className ? ` ${className}` : ''}`}>
      {asking ? (
        <span className="pj-confirm" role="group" aria-label="确认停止专项">
          <span className="pj-cq">停止专项「{p.title}」？负责人和话题在跑的任务会被请停</span>
          <button type="button" className="btn sm" disabled={busy} onClick={() => void act('stop')}>确认停止</button>
          <button type="button" className="btn sm txt" onClick={() => setAsking(false)}>取消</button>
        </span>
      ) : (
        <>
          {mayAgain && shown('again') ? (
            <button type="button" className="btn sm pri" disabled={busy} title="让专项接着做：负责人重新被叫醒" onClick={() => void act('again')}>
              <Play /> 继续
            </button>
          ) : null}
          {mayFinish && shown('finish') ? (
            <button type="button" className="btn sm" disabled={busy} title="叫负责人总结做完了什么、还有什么没做完，再结束这个专项" onClick={() => void act('finish')}>
              收尾
            </button>
          ) : null}
          {mayStop && shown('stop') ? (
            <button type="button" className="btn sm txt" disabled={busy} title="停住这个专项，负责人和话题在跑的任务会被请停" onClick={() => setAsking(true)}>
              停止
            </button>
          ) : null}
        </>
      )}
      {said ? <span className={`rc-said${said.bad ? ' bad' : ''}`} role={said.bad ? 'alert' : 'status'}>{said.line}</span> : null}
    </span>
  );
  return wrap ? <div className="pj-opsbar">{body}</div> : body;
};

/* 页面上取消办好的定时回看（内存，这一页）：Covey 回执前已推了新数据，但页面的快照下一次取到才换；这之前不画它，免得“取消了”还挂着 */
let cancelledRevisits: ReadonlySet<string> = new Set<string>();
const cancelListeners = new Set<() => void>();
function putCancelled(mark: string): void {
  cancelledRevisits = new Set<string>([...cancelledRevisits, mark]);
  cancelListeners.forEach((l: () => void) => l());
}
function useCancelled(): ReadonlySet<string> {
  return useSyncExternalStore(
    (cb: () => void) => {
      cancelListeners.add(cb);
      return () => cancelListeners.delete(cb);
    },
    () => cancelledRevisits,
  );
}
type LaterTopic = Pick<SnapshotTopic, 'key' | 'revisit'>;
const stillLater = (t: LaterTopic, gone: ReadonlySet<string>): boolean => !!t.revisit && !gone.has(revisitMark(t.key, t.revisit.set_at));

/** Covey 定时回看（话题总览、看板的一行）：“Covey 将于 21:40 自动回看：CI 跑完没有”，后面有「取消」（Covey 认 revisit_cancel、还没开始回看时）。
 * 取消的是这一条（带 set_at）：已经回看过、取消过或换了新的，Covey 回明确的话，页面照回执写；办好后这一行消失 */
export const LaterLine: React.FC<{ t: LaterTopic }> = ({ t }) => {
  const now: number = useTick(30000);
  const ops = useOps();
  const op = useOutboxOp();
  const gone: ReadonlySet<string> = useCancelled();
  if (!stillLater(t, gone) || !t.revisit) return null;
  const r = t.revisit;
  return (
    <span className="v34-later">
      <CalendarClock />
      <span className="v34-lt">{revisitWords(r, now)}</span>
      {canCancelRevisit(ops, r) && op.phase !== 'done' ? (
        <button type="button" className="btn sm txt v34-cancel" disabled={op.busy} title="不让 Covey 到点自动回看这个话题"
          onClick={() => {
            void op.run({ op_id: newOpId(), kind: 'revisit_cancel', topic_key: t.key, payload: { set_at: r.set_at } }).then((res: OpResult) => {
              if (res.phase === 'done') putCancelled(revisitMark(t.key, r.set_at));
            });
          }}>
          取消
        </button>
      ) : null}
      {op.line && op.phase !== 'done' ? <span className={`t3 v34-said${op.phase === 'failed' || op.phase === 'unsent' ? ' bad' : ''}`} role="status">{op.line}</span> : null}
    </span>
  );
};

/** 等你在飞书卡上确认的代发草稿：网页只读，确认（可改正文）和发出都在飞书原卡上，这里只给去飞书的入口 */
export const DraftLines: React.FC<{ drafts: OwnerSendDraft[] | undefined }> = ({ drafts }) => {
  const feishu: string = useSettings().data?.data?.feishu ?? '';
  if (!drafts?.length) return null;
  return (
    <span className="v34-drafts">
      {drafts.map((d: OwnerSendDraft) => (
        <span className="v34-draft" key={d.id}>
          <Send />
          <span className="v34-dt">代发草稿 {draftWords(d)}</span>
          {d.link || feishu ? (
            <a className="v34-go" href={d.link || feishu} target="_blank" rel="noopener noreferrer">去飞书确认</a>
          ) : <span className="t3">在飞书话题的草稿卡上确认</span>}
        </span>
      ))}
    </span>
  );
};

/** 看板主栏的代发草稿（要你去飞书确认，算要你动手的事，排在有结果、在做前面）：一个话题一行；没有就不画 */
export const DraftPanel: React.FC<{ topics: SnapshotTopic[] }> = ({ topics }) => {
  const drafting: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => !!t.owner_send?.length);
  if (!drafting.length) return null;
  return (
    <section className="v34-panel" aria-label="等你在飞书确认的代发草稿">
      <h2 className="nb-sh">等你在飞书确认的代发<em>{drafting.reduce((n: number, t: SnapshotTopic) => n + (t.owner_send?.length ?? 0), 0)}</em></h2>
      <div className="nb-rows">
        {drafting.map((t: SnapshotTopic) => (
          <div className="nb-row v34-row" key={`d-${t.key}`}>
            <TopicBadge number={t.number} />
            <Link className="v34-tt" to={`/topics/${encodeURIComponent(t.key)}`}>{t.title}</Link>
            <DraftLines drafts={t.owner_send} />
          </div>
        ))}
      </div>
    </section>
  );
};

/** 看板右栏的「Covey 定时回看」（Covey 自己排好的安排，不是要你做的事）：按时刻排，一个话题两行（T 号和标题、几点回看什么和「取消」）；没有就不画 */
export const RevisitPanel: React.FC<{ topics: SnapshotTopic[] }> = ({ topics }) => {
  const gone: ReadonlySet<string> = useCancelled();
  const later: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => stillLater(t, gone)).sort((a, b) => (a.revisit?.at ?? 0) - (b.revisit?.at ?? 0));
  if (!later.length) return null;
  return (
    <section className="nb-panel nb-revisit" aria-label="Covey 定时回看">
      <h3 className="nb-ph">Covey 定时回看<em>{later.length}</em></h3>
      {later.map((t: SnapshotTopic) => (
        <div className="nr-item" key={`r-${t.key}`}>
          <Link className="nr-t" to={`/topics/${encodeURIComponent(t.key)}`} title={t.title}>
            <TopicBadge number={t.number} />
            <span>{t.title}</span>
          </Link>
          <LaterLine t={t} />
        </div>
      ))}
    </section>
  );
};

/** 撤销办好后，把这一轮那条记忆在页面缓存里改成已撤销（Covey 已改了任务记录、推了这一轮；对话按 seq 增量取，做完的轮次不会再取到）。
 * 先停掉在路上的那次取数，免得它拿改之前的缓存合并回来盖掉 */
async function markUndone(qc: QueryClient, topicKey: string, jobId: string, entryId: string): Promise<void> {
  const patch = (t: TurnView): TurnView =>
    t.job_id === jobId && t.notes?.some((n) => n.entry_id === entryId && n.kind === 'remembered')
      ? { ...t, notes: t.notes.map((n) => (n.entry_id === entryId && n.kind === 'remembered' ? { ...n, kind: 'undone' } : n)) }
      : t;
  await Promise.all([qc.cancelQueries({ queryKey: turnsKey(topicKey) }), qc.cancelQueries({ queryKey: ROOM_KEY })]);
  qc.setQueryData<TurnsResponse>(turnsKey(topicKey), (prev) => (prev ? { ...prev, turns: prev.turns.map(patch) } : prev));
  qc.setQueryData<RoomResponse>(ROOM_KEY, (prev) => (prev ? { ...prev, turns: prev.turns.map(patch) } : prev));
  void qc.invalidateQueries({ queryKey: turnsKey(topicKey) });
  void qc.invalidateQueries({ queryKey: ROOM_KEY });
}

/** 记忆的「不对」（同答案卡上的不对）：撤销这一轮刚记下的这一条，办好后这一行原地变成“已撤销”；Covey 不认 memory_undo（旧版）时
 * 只写一行去飞书点的说明 */
export const MemoryWrong: React.FC<{ topicKey: string; jobId: string | null | undefined; entryId: string | undefined }> = ({ topicKey, jobId, entryId }) => {
  const ops = useOps();
  const qc: QueryClient = useQueryClient();
  const op = useOutboxOp();
  if (!canDo(ops, 'memory_undo') || !entryId || !jobId || !/^\d+$/u.test(jobId) || !topicKey) {
    return <span className="t3 mem-undo">记错了在飞书答案卡上点「不对」</span>;
  }
  if (op.phase === 'done') return <span className="t3 mem-undo">{op.line}</span>;
  return (
    <>
      <button type="button" className="wrong" disabled={op.busy} title="这条记错了：撤销它，以后不再照着做"
        onClick={() => {
          void op.run({ op_id: newOpId(), kind: 'memory_undo', topic_key: topicKey, payload: { job_id: jobId, entry_id: entryId } }).then((r: OpResult) => {
            if (r.phase === 'done') void markUndone(qc, topicKey, jobId, entryId);
          });
        }}>
        不对
      </button>
      {op.line ? <span className={`t3 mem-undo${op.phase === 'failed' || op.phase === 'unsent' ? ' bad' : ''}`}>{op.line}</span> : null}
    </>
  );
};

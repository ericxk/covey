import type { ReportResponse, ReportTurn, SnapshotProject, SnapshotResponse, SnapshotTopic, TidyData, TidyProposal } from '@shared/api.interface';
import { cacheShare, closedAt, sinceOf } from './reportData';
import { PRESSING, type ToneKey } from './look.gen';
import { byAsked, byNumberDesc, byRecent, isWaiting, toneOf } from './lib';
import { isStopped } from './topicText';

/* ---- 专项自己停住的口径（covey core/projects.py pause_tone、halted；docs/ui.md 2.2：护栏停住的专项是“停住了”的红） ----
 * Covey 推的专项 status 是中文：停住、收起、做完、已取消、等你、在做；停住时 status_reason 是护栏的原因
 * （token 用完了、连续唤醒次数到上限、很久没有动静），你自己停的是“主人停止”。 */
const OWNER_STOP = '主人停止';
/** 护栏停住的专项（停住了，要你处理，能「继续」）：停住、不是你自己停的；认不得的原因也算（和 Covey 一样，不当成你停的） */
export const isHalted = (p: Pick<SnapshotProject, 'status' | 'status_reason'>): boolean => p.status === '停住' && p.status_reason !== OWNER_STOP;
/** 专项自己的状态词表 key：护栏停住 stopped、你停的 cancelled、收起 idle；别的（在做、等你、做完）不归它说，是 '' */
export function projectTone(p: Pick<SnapshotProject, 'status' | 'status_reason'>): ToneKey | '' {
  if (p.status === '停住') return isHalted(p) ? 'stopped' : 'cancelled';
  if (p.status === '收起') return 'idle';
  if (p.status === '已取消') return 'cancelled';
  return '';
}
/** 胶囊和卡上的字：护栏停住的“停住了 · token 用完了”，你停的“已停止” */
export function haltWords(p: Pick<SnapshotProject, 'status' | 'status_reason'>): string {
  if (!isHalted(p)) return p.status === '停住' ? '已停止' : p.status;
  return p.status_reason ? `停住了 · ${p.status_reason}` : '停住了';
}

/** 专项名下面那行灰字：目标；没写目标时用 Covey 给的一句状态说明（有问题等你回答、没有新的事）。
 * 停住的不用：原因已经在胶囊上（停住了 · token 用完了），你自己停的那句是内部的“主人停止”，不给人看 */
export const projectNote = (p: Pick<SnapshotProject, 'goal' | 'status' | 'status_reason'>): string =>
  (p.goal || (p.status === '停住' ? '' : p.status_reason) || '').replace(/^\s*目标\s*[:：]\s*/u, '');

/* ---- 要你处理（T647 P0-2）：看板、侧栏角标、手机标签栏共用这一个数，口径同 covey core/board_read.py Asks ----
 * - 等你：每个话题一件，有题在等或在等你点开始的；管家、负责人的题也算（要你定的事谁问都进等你）；整理专项要你定的建议每条一件
 *   （看板上的整理面板画它们，侧栏「要你处理」每条一行）；
 * - 停住了：普通话题最新一轮停住了（跟看板分组走：看过 12 小时、点了“不管了”的不算），加上护栏停住的专项；
 * - 可能卡住：普通话题在做、可能卡住（Covey 已除去在等命令跑完的）。
 * 「不管了」（以前网页上只记在本机的“先放着”，P1-C3 并过来了）告诉 Covey 把话题从看板拿掉，Covey 推回 board 为空，这里就不算了。
 * 数只从这里列出的条目数（askCounts），不再拿 Covey 另推的 snap.asks 盖掉：那是另一条推送，和话题条目不同步，
 * 曾经顶上写“1 件事要你处理”、下面一件都没有（T978，2026-11-29）。页面上的数永远等于页面上能点的条目。 */
export interface Asks {
  waiting: SnapshotTopic[];
  stopped: SnapshotTopic[];
  stuck: SnapshotTopic[];
  /** 护栏停住的专项：算进停住了 */
  projects: SnapshotProject[];
  /** 整理专项要你定的建议（state open）：算进等你；没取到整理专项（没开、还没取到）时空 */
  proposals: TidyProposal[];
}

export function asks(snap: Pick<SnapshotResponse, 'topics' | 'projects' | 'questions'>, now: number, tidy?: Pick<TidyData, 'proposals'> | null): Asks {
  const qKeys = new Set<string>(snap.questions.filter((q) => q.state === 'open').map((q) => q.topic_key));
  const waits = (t: SnapshotTopic): boolean => isWaiting(t) || qKeys.has(t.key);
  const topics: SnapshotTopic[] = snap.topics.filter((t: SnapshotTopic) => t.kind === 'topic');
  return {
    waiting: snap.topics.filter(waits).sort(byAsked),
    stopped: topics.filter((t: SnapshotTopic) => !waits(t) && isStopped(t, now)).sort(byRecent),
    stuck: topics.filter((t: SnapshotTopic) => !waits(t) && toneOf(t) === 'stuck').sort(byNumberDesc),
    projects: snap.projects.filter(isHalted).sort((a: SnapshotProject, b: SnapshotProject) => b.updated_at - a.updated_at || a.key.localeCompare(b.key)),
    proposals: (tidy?.proposals ?? []).filter((p: TidyProposal) => p.state === 'open'),
  };
}

/** 各状态几件（covey board_read Asks.counts）：护栏停住的专项和停住的话题一起算停住了，整理专项的建议和题一起算等你。
 *  看板结论带、统计条、侧栏角标、手机标签栏都只用它：数的就是画出来的那些条目 */
export const askCounts = (a: Asks): { waiting: number; stopped: number; stuck: number } => ({
  waiting: a.waiting.length + a.proposals.length,
  stopped: a.stopped.length + a.projects.length,
  stuck: a.stuck.length,
});
/** 要你处理一共几件：每处说的都是这个数（covey board_read act_count） */
export const askTotal = (a: Asks): number => {
  const c = askCounts(a);
  return c.waiting + c.stopped + c.stuck;
};
export const askSum = (c: { waiting: number; stopped: number; stuck: number }): number => c.waiting + c.stopped + c.stuck;

/* ---- 专项的收尾、停止（v34，同飞书专项卡的停止专项、停住卡的收尾）：什么状态给哪颗；Covey 还会按守卫再核一次 ---- */
/** 收尾：在做、等你的，和停住的（护栏停住、你停的、收起的；Covey 先叫醒负责人收尾） */
export const canFinish = (p: Pick<SnapshotProject, 'status'>): boolean => ['在做', '等你', '停住', '收起'].includes(p.status);
/** 停止：只给在做、等你的（停住了的再停没有意义；护栏停住的问你那张卡在飞书上） */
export const canStop = (p: Pick<SnapshotProject, 'status'>): boolean => ['在做', '等你'].includes(p.status);

/** 停住了的话题（看板、侧栏、专项一览都按它画红点） */
export const stoppedSet = (a: Asks): Set<string> => new Set<string>(a.stopped.map((t: SnapshotTopic) => t.key));

export function leadOf(snap: SnapshotResponse, p: SnapshotProject): SnapshotTopic | undefined {
  return (
    snap.topics.find((t: SnapshotTopic) => t.kind === 'lead' && t.key === p.lead_topic_key) ??
    snap.topics.find((t: SnapshotTopic) => t.kind === 'lead' && t.project_key === p.key)
  );
}
export const ownTopics = (snap: SnapshotResponse, p: SnapshotProject): SnapshotTopic[] =>
  snap.topics.filter((t: SnapshotTopic) => t.kind === 'topic' && t.project_key === p.key);

export interface PerTopic {
  seconds: number;
  tokens: number;
}
export interface ProjectMetrics {
  work: number;
  tokens: number;
  cachePct: number | null;
  avgWait: number | null;
  questions: number;
  startedAt: number | null;
  per: Map<string, PerTopic>;
}

export function metrics(rep: ReportResponse | undefined, p: SnapshotProject, topics: SnapshotTopic[] = []): ProjectMetrics {
  const per = new Map<string, PerTopic>();
  let work = 0;
  let tokens = 0;
  let cache = 0;
  let inp = 0;
  let first: number | null = null;
  // 挂进专项的已有话题带着以前的轮次：话题算进专项（专项开始、它加入里晚的那个）以前就结束的不算（和报告、专项卡一致）
  const since: number | null = p.started_at;
  const byKey = new Map<string, SnapshotTopic>(topics.map((t: SnapshotTopic) => [t.key, t]));
  const ours = (x: ReportTurn): boolean => {
    const from: number | null = sinceOf(since, byKey.get(x.topic_key));
    return x.role !== 'owner' && (!from || !x.ended_at || x.ended_at >= from);
  };
  for (const t of (rep?.turns ?? []).filter(ours)) {
    const sec: number = t.seconds || (t.started_at && t.ended_at ? t.ended_at - t.started_at : 0);
    const tok: number = t.usage?.total ?? 0;
    const cur: PerTopic = per.get(t.topic_key) ?? { seconds: 0, tokens: 0 };
    cur.seconds += sec;
    cur.tokens += tok;
    per.set(t.topic_key, cur);
    if (t.kind !== 'lead') work += sec;
    tokens += tok;
    cache += t.usage?.cache_read ?? 0;
    inp += t.usage?.input ?? 0;
    if (t.started_at && (first === null || t.started_at < first)) first = t.started_at;
  }
  // 你答过的题各等了多久（负责人代答、关掉的不算你回得慢）
  const waits: number[] = (rep?.questions ?? [])
    .filter((q) => q.asked_at && closedAt(q) && ['owner', 'feedback', 'skipped'].includes(q.closed_by || (q.answered_at ? 'owner' : '')))
    .map((q) => (closedAt(q) ?? 0) - (q.asked_at ?? 0));
  return {
    work,
    tokens,
    cachePct: cacheShare(cache, inp),
    avgWait: waits.length ? waits.reduce((a: number, b: number) => a + b, 0) / waits.length : null,
    questions: rep?.questions.length ?? 0,
    startedAt: p.started_at ?? first,
    per,
  };
}

/** 专项的胶囊：护栏停住的专项先说它自己（红色“停住了 · 原因”，docs/ui.md 2.2）；其余是话题里最急的那件（look 的 PRESSING；
 * 话题按列表里的颜色传进来，负责人按它自己的）；都不急时是专项自己的状态，灰色（你停的写“已停止”） */
export function projectPill(p: SnapshotProject, tones: ToneKey[], lead: SnapshotTopic | undefined): { tone: ToneKey; text: string | undefined } {
  if (isHalted(p)) return { tone: 'stopped', text: haltWords(p) };
  const all: ToneKey[] = lead ? [...tones, toneOf(lead)] : tones;
  const tone: ToneKey = PRESSING.find((k: ToneKey) => all.includes(k)) ?? 'idle';
  return { tone, text: tone === 'idle' ? haltWords(p) : undefined };
}

/** 按状态数话题：要紧的在前、做完在后，已停止、空闲、排队不单列；只留有的 */
const COUNTED: readonly ToneKey[] = ['waiting', 'stopped', 'stuck', 'result', 'running', 'done'];
export function toneCounts(tones: ToneKey[]): [ToneKey, number][] {
  return COUNTED.map((k: ToneKey): [ToneKey, number] => [k, tones.filter((v: ToneKey) => v === k).length]).filter(([, n]) => n > 0);
}

export type { ReportTurn };

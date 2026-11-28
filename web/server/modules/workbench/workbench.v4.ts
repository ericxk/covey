/* 数据约定 v4 的可选字段：话题要点 gist、进度 progress、问题卡的回答 answers、没做成的一轮 failure。
 * Covey 推来的是 jsonb，形状不对就当没有（返回 undefined），页面拿不到就不画那一块。 */
import {
  ACTION_STATES,
  CHECKLIST_STATES,
  FAILURE_ACTIONS,
  FAILURE_HOWS,
  RECOVER_ACTIONS,
  type ActionStoredState,
  type BriefAction,
  type ChecklistItem,
  type ChecklistState,
  type FailureAction,
  type OwnerSendDraft,
  type ProjectGap,
  type ProjectNote,
  type SettingsAsks,
  type TopicRevisit,
  type TurnCriterion,
  type TurnDelivery,
  type FailureButton,
  type FailureHow,
  type TopicCommand,
  type TopicConfig,
  type TopicGist,
  type TopicProgress,
  type TopicSlowStep,
  type TurnAnswer,
  type TurnAutoRetry,
  type TurnFailure,
} from '@shared/api.interface';
import { obj, str } from './workbench.util';

const isObj = (v: unknown): boolean => !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);

export function gistOf(v: unknown): TopicGist | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const g: TopicGist = {};
  for (const k of ['essence', 'goal', 'involves', 'changed'] as const) {
    const s: string = str(d[k]).trim();
    if (s) g[k] = s;
  }
  if (Array.isArray(d.related)) {
    const rel: number[] = d.related.map((x: unknown) => (typeof x === 'string' ? Number(x.replace(/^T/iu, '')) : x)).filter((x: unknown): x is number => typeof x === 'number' && Number.isInteger(x) && x > 0);
    if (rel.length) g.related = rel.slice(0, 20);
  }
  const at: number | undefined = finite(d.updated_at);
  if (at !== undefined) g.updated_at = at;
  return Object.keys(g).length ? g : undefined;
}

export function progressOf(v: unknown): TopicProgress | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const p: TopicProgress = {};
  const phase: string = str(d.phase).trim();
  if (phase) p.phase = phase;
  const done: number | undefined = finite(d.plan_done);
  const total: number | undefined = finite(d.plan_total);
  if (total !== undefined && total > 0) {
    p.plan_total = Math.round(total);
    p.plan_done = Math.max(0, Math.min(Math.round(done ?? 0), p.plan_total));
  }
  const lastAt: number | undefined = finite(d.last_activity_at);
  if (lastAt !== undefined && lastAt > 0) p.last_activity_at = lastAt;
  const last: string = str(d.last_activity).trim();
  if (last) p.last_activity = last;
  if ('eta_seconds' in d) {
    const eta: number | undefined = finite(d.eta_seconds);
    p.eta_seconds = eta !== undefined && eta >= 0 ? eta : null;
  }
  if (d.maybe_stuck === true) p.maybe_stuck = true;
  // 进度说明（docs/web.md 进度）：这步比平均慢、在等一条命令跑完。形状不对就当没有
  if ('slow_step' in d) p.slow_step = slowStepOf(d.slow_step);
  const command: TopicCommand | undefined = commandOf(d.command);
  if (command) p.command = command;
  // v41：下一步、在等主人答什么（专项「简报」的 session 行用）
  const next: string = clip(d.next_step, 120);
  if (next) p.next_step = next;
  const waiting: string = clip(d.waiting, 160);
  if (waiting) p.waiting = waiting;
  return Object.keys(p).length ? p : undefined;
}

const seconds = (v: unknown): number | undefined => {
  const n: number | undefined = finite(v);
  return n !== undefined && n >= 0 ? n : undefined;
};
const moment = (v: unknown): number | null => {
  const n: number | undefined = finite(v);
  return n !== undefined && n > 0 ? n : null;
};

/** 当前这步比此前平均每步慢：{number, since, seconds, usual}；不是这样的对象就是 null */
function slowStepOf(v: unknown): TopicSlowStep | null {
  if (!isObj(v)) return null;
  const d: Record<string, unknown> = obj(v);
  const number: number | undefined = finite(d.number);
  const secs: number | undefined = seconds(d.seconds);
  if (number === undefined || number < 1 || secs === undefined) return null;
  const usual: number | undefined = seconds(d.usual);
  return { number: Math.round(number), since: moment(d.since), seconds: secs, usual: usual ?? null };
}

/** 最新一步是还在跑的命令：{since, seconds, long} */
function commandOf(v: unknown): TopicCommand | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const secs: number | undefined = seconds(d.seconds);
  if (secs === undefined) return undefined;
  return { since: moment(d.since), seconds: secs, long: d.long === true };
}

export function answersOf(v: unknown): TurnAnswer[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: TurnAnswer[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const question: string = str(d.question).trim();
    const choice: string = str(d.choice).trim();
    if (question || choice) out.push({ question, choice: choice || '没答' });
  }
  return out.length ? out.slice(0, 40) : undefined;
}

/** 按钮的 key：没成功的一轮能做的，加上排着自动重试时的“停止自动重试” */
const BUTTONS: readonly FailureButton[] = [...FAILURE_ACTIONS, 'stop_auto_retry'];

export function failureOf(v: unknown): TurnFailure | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const title: string = str(d.title).trim();
  if (!title) return undefined;
  const actions: FailureAction[] = Array.isArray(d.actions)
    ? d.actions.filter((a: unknown): a is FailureAction => typeof a === 'string' && (FAILURE_ACTIONS as readonly string[]).includes(a))
    : [];
  const detail: string = str(d.detail).trim();
  const out: TurnFailure = { title, ...(detail ? { detail } : {}), actions: [...new Set(actions)] };
  // v33：按钮的字、怎么做、排着的自动重试（只留认得的 key；旧 Covey 不推就没有）
  const labels: Partial<Record<FailureButton, string>> = {};
  const how: Partial<Record<FailureButton, FailureHow>> = {};
  const lo: Record<string, unknown> = obj(d.labels);
  const ho: Record<string, unknown> = obj(d.how);
  for (const k of BUTTONS) {
    const label: string = str(lo[k]).trim();
    if (label) labels[k] = Array.from(label).slice(0, 20).join('');
    if ((FAILURE_HOWS as readonly string[]).includes(str(ho[k]))) how[k] = ho[k] as FailureHow;
  }
  if (Object.keys(labels).length) out.labels = labels;
  if (Object.keys(how).length) out.how = how;
  const auto: TurnAutoRetry | undefined = autoRetryOf(d.auto_retry);
  if (auto) out.auto_retry = auto;
  return out;
}


/** v33：排着的自动重试 {state: pending, at, attempt, total, kind, now}；不在排着（或形状不对）就没有 */
function autoRetryOf(v: unknown): TurnAutoRetry | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  if (d.state !== 'pending') return undefined;
  const whole = (x: unknown): number | null => {
    const n: number | undefined = finite(x);
    return n !== undefined && n > 0 ? Math.round(n) : null;
  };
  return {
    state: 'pending',
    at: moment(d.at),
    attempt: whole(d.attempt),
    total: whole(d.total),
    kind: str(d.kind).slice(0, 40),
    now: d.now === 'continue' ? 'continue' : 'retry',
  };
}

/** v20：话题现在的配置。model、workspace 必须有；别的缺了按空串、direct、off 补，页面按它预选换配置面板 */
export function configOf(v: unknown): TopicConfig | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const model: string = str(d.model).trim();
  const workspace: string = str(d.workspace).trim();
  if (!model || !workspace) return undefined;
  return {
    model,
    effort: str(d.effort),
    work_mode: str(d.work_mode),
    workspace,
    run_mode: d.run_mode === 'worktree' ? 'worktree' : 'direct',
  };
}

/** v33：recover 操作的形状（没成功的一轮上的按钮：检查并继续、现在重试、看原因、拆小再试、停止自动重试）：
 * 要 topic_key、不带 project_key，payload 只有 job_id（任务号，数字或数字串）和 action。能不能做由 Covey 按记录定，这里只挡形状不对的。
 * 对就返回规整后的任务号，不对返回原因（400 的消息） */
export function recoverShape(payload: Record<string, unknown>, topicKey: string, projectKey: string): { job_id: string } | { error: string } {
  if (!topicKey || projectKey) return { error: 'recover 要 topic_key、不带 project_key' };
  if (Object.keys(payload).some((k: string) => k !== 'job_id' && k !== 'action')) return { error: 'recover 只有 job_id 和 action' };
  const job: string = typeof payload.job_id === 'number' && Number.isInteger(payload.job_id) ? String(payload.job_id) : str(payload.job_id).trim();
  if (!/^\d{1,12}$/u.test(job)) return { error: 'recover 要任务号 job_id' };
  if (!(RECOVER_ACTIONS as readonly string[]).includes(str(payload.action))) return { error: 'recover 的 action 不合法' };
  return { job_id: job };
}

/* ---- v34：到点回来看、代发草稿、完成标准与改动；形状不对就当没有 ---- */
const clip = (v: unknown, n: number): string => Array.from(str(v).trim()).slice(0, n).join('');

/** 话题的定时回看 {at, what, set_at?, running?}：at 是正数秒；set_at 是正数秒（v35，取消时核对）；running 只认 true（Covey 正在回看） */
export function revisitOf(v: unknown): TopicRevisit | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const at: number | undefined = finite(d.at);
  if (at === undefined || at <= 0) return undefined;
  const setAt: number | undefined = finite(d.set_at);
  return { at, what: clip(d.what, 200), ...(setAt !== undefined && setAt > 0 ? { set_at: setAt } : {}), ...(d.running === true ? { running: true } : {}) };
}

/** 等你确认的代发草稿：只留认得的字段，链接只认 https；最多 5 条 */
export function ownerSendOf(v: unknown): OwnerSendDraft[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: OwnerSendDraft[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const id: string = clip(d.id, 64);
    if (!id) continue;
    const link: string = str(d.link).trim();
    const at: number | undefined = finite(d.created_at);
    out.push({
      id, to: clip(d.to, 60), state: clip(d.state, 20) || 'waiting', preview: clip(d.preview, 120), created_at: at !== undefined && at > 0 ? at : null,
      ...(/^https:\/\//u.test(link) ? { link: link.slice(0, 500) } : {}),
    });
  }
  return out.length ? out.slice(0, 5) : undefined;
}

/** 一轮的完成标准 [{item, state, evidence}]：最多 20 条 */
export function criteriaOf(v: unknown): TurnCriterion[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: TurnCriterion[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const item: string = clip(d.item, 300);
    if (!item) continue;
    const evidence: string = clip(d.evidence, 300);
    out.push({ item, state: clip(d.state, 10) || '没查', ...(evidence ? { evidence } : {}) });
  }
  return out.length ? out.slice(0, 20) : undefined;
}

/** 一轮的交付 {commit, deployed, verified}：都空就没有 */
export function deliveryOf(v: unknown): TurnDelivery | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const out: TurnDelivery = {};
  const commit: string = clip(d.commit, 400);
  const deployed: string = clip(d.deployed, 200);
  const verified: string = clip(d.verified, 400);
  if (commit) out.commit = commit;
  if (deployed) out.deployed = deployed;
  if (verified) out.verified = verified;
  return Object.keys(out).length ? out : undefined;
}

/** v34：专项的收尾、停止（同飞书专项卡、停住卡上的按钮）：要 project_key、不带 topic_key，payload 是空的 */
export function projectOpShape(kind: string, payload: Record<string, unknown>, topicKey: string, projectKey: string): string | null {
  if (!projectKey || topicKey) return `${kind} 要 project_key、不带 topic_key`;
  if (Object.keys(payload).some((k: string) => k !== 'project_key')) return `${kind} 的 payload 要是空的`;
  return null;
}

/** v34：撤销刚记下的一条记忆（同答案卡上的「不对」）：要 topic_key，payload 是 job_id（任务号）和 entry_id */
export function memoryUndoShape(payload: Record<string, unknown>, topicKey: string, projectKey: string): { job_id: string; entry_id: string } | { error: string } {
  if (!topicKey || projectKey) return { error: 'memory_undo 要 topic_key、不带 project_key' };
  if (Object.keys(payload).some((k: string) => k !== 'job_id' && k !== 'entry_id')) return { error: 'memory_undo 只有 job_id 和 entry_id' };
  const job: string = typeof payload.job_id === 'number' && Number.isInteger(payload.job_id) ? String(payload.job_id) : str(payload.job_id).trim();
  if (!/^\d{1,12}$/u.test(job)) return { error: 'memory_undo 要任务号 job_id' };
  const entry: string = str(payload.entry_id).trim();
  if (!/^[A-Za-z0-9_.:-]{1,64}$/u.test(entry)) return { error: 'memory_undo 要 entry_id' };
  return { job_id: job, entry_id: entry };
}

/** v35：取消一条定时回看：要 topic_key、不带 project_key，payload 只有 set_at（正数秒，Covey 核对是同一条才取消） */
export function revisitCancelShape(payload: Record<string, unknown>, topicKey: string, projectKey: string): { set_at: number } | { error: string } {
  if (!topicKey || projectKey) return { error: 'revisit_cancel 要 topic_key、不带 project_key' };
  if (Object.keys(payload).some((k: string) => k !== 'set_at')) return { error: 'revisit_cancel 只有 set_at' };
  const at: number | undefined = finite(payload.set_at);
  if (at === undefined || at <= 0) return { error: 'revisit_cancel 要预约时刻 set_at' };
  return { set_at: at };
}

/** v34：Covey 算好的要你处理 {waiting, stopped, stuck}（settings 类条目 asks）：三个都是非负整数才认 */
export function asksOf(v: unknown): SettingsAsks | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const n = (x: unknown): number | undefined => (typeof x === 'number' && Number.isInteger(x) && x >= 0 ? x : undefined);
  const waiting: number | undefined = n(d.waiting);
  const stopped: number | undefined = n(d.stopped);
  const stuck: number | undefined = n(d.stuck);
  if (waiting === undefined || stopped === undefined || stuck === undefined) return undefined;
  return { waiting, stopped, stuck };
}

/* v40：专项的「进度」标签（Covey app/web_collect.py progress_fields）。数组才收，每条按类型过滤，形状不对的那条不要；
 * 一条都不剩就当没推（undefined），页面写“这个专项还没有目标检查表” */
const ITEM_ID = /^c[1-9][0-9]{0,3}$/u;
const isState = (v: unknown): v is ChecklistState => (CHECKLIST_STATES as readonly unknown[]).includes(v);

/** 目标检查表（≤20 条，Covey 的 MAX_ITEMS；这里放宽到 50）：id 是 c 加数字，criterion 不空；state 只认 open、done、blocked、dropped */
export function checklistOf(v: unknown): ChecklistItem[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: ChecklistItem[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const id: string = str(d.id);
    const criterion: string = clip(d.criterion, 400);
    if (!ITEM_ID.test(id) || !criterion || !isState(d.state) || out.some((it: ChecklistItem) => it.id === id)) continue;
    const needs: string[] = Array.isArray(d.needs) ? d.needs.filter((n: unknown): n is string => typeof n === 'string' && ITEM_ID.test(n)).slice(0, 12) : [];
    const item: ChecklistItem = { id, criterion, topic: clip(d.topic, 64), needs, evidence: clip(d.evidence, 400), gap: clip(d.gap, 400), state: d.state, at: finite(d.at) ?? 0 };
    // v41 的可选字段：没有（或形状不对）就不带键，旧页面照旧
    const title: string = clip(d.title, 40);
    if (title) item.title = title;
    if (d.waiting_on === 'owner' || d.waiting_on === '') item.waiting_on = d.waiting_on;
    const doneAt: number | undefined = finite(d.done_at);
    if (doneAt !== undefined && doneAt > 0) item.done_at = doneAt;
    const topics: string[] = refsOf(d.topics, 12);
    if (topics.length) item.topics = topics;
    out.push(item);
  }
  return out.length ? out.slice(0, 50) : undefined;
}

/** 话题引用的列表（key 或 T 号）：字符串、去空去重、各 64 字 */
function refsOf(v: unknown, max: number): string[] {
  if (!Array.isArray(v)) return [];
  return [...new Set(v.map((x: unknown) => clip(x, 64)).filter(Boolean))].slice(0, max);
}

/* v41：专项「简报」（Covey app/web_collect.py progress_fields）。动作、缺口都是数组，每条按类型过滤；一条都不剩就当没推 */
const ACTION_ID = /^a[1-9][0-9]{0,3}$/u;

/** 动作（Covey 限没 dropped 的最多 10 个，id 不复用，所以 dropped 的也会推来；这里放宽到 40）：id 是 a 加数字，title 不空，
 * state 只认 open、dropped；items 只留条目 id，topics 是话题引用 */
export function actionsOf(v: unknown): BriefAction[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: BriefAction[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const id: string = str(d.id);
    const title: string = clip(d.title, 60);
    if (!ACTION_ID.test(id) || !title || !(ACTION_STATES as readonly unknown[]).includes(d.state) || out.some((a: BriefAction) => a.id === id)) continue;
    const items: string[] = Array.isArray(d.items) ? [...new Set(d.items.filter((n: unknown): n is string => typeof n === 'string' && ITEM_ID.test(n)))].slice(0, 50) : [];
    out.push({ id, title, note: clip(d.note, 200), items, topics: refsOf(d.topics, 40), state: d.state as ActionStoredState, at: finite(d.at) ?? 0 });
  }
  return out.length ? out.slice(0, 40) : undefined;
}

/** 没处理的协作缺口（最多 50 条）：id 和 text 不空才要 */
export function gapsOf(v: unknown): ProjectGap[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: ProjectGap[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const id: string = clip(d.id, 32);
    const text: string = clip(d.text, 200);
    if (!id || !text) continue;
    out.push({ id, thread: clip(d.thread, 64), text, at: finite(d.at) ?? 0 });
  }
  return out.length ? out.slice(0, 50) : undefined;
}

/** 背景（≤300 字）：非空字符串才要 */
export const backgroundOf = (v: unknown): string | undefined => clip(v, 300) || undefined;

/** 里程碑、决定日志（各最新 10 条，旧的在前）：text 不空才要 */
export function projectNotesOf(v: unknown): ProjectNote[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out: ProjectNote[] = [];
  for (const x of v) {
    const d: Record<string, unknown> = obj(x);
    const text: string = clip(d.text, 400);
    if (!text) continue;
    out.push({ id: clip(d.id, 32), at: finite(d.at) ?? 0, kind: clip(d.kind, 20) || 'note', text });
  }
  return out.length ? out.slice(-10) : undefined;
}

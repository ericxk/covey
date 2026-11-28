import { Injectable, Inject, NotFoundException, BadRequestException, ConflictException } from '@nestjs/common';
import type { PostgresJsDatabase } from '@lark-apaas/fullstack-nestjs-core';
import { WB_DB } from './workbench.tokens';
import { sql } from 'drizzle-orm';
import {
  isOutboxKind,
  MEMORY_FIXES,
  PROXY_OPS,
  LAUNCH_RUN_MODES,
  UPLOAD_MAX_BYTES,
  UPLOAD_MAX_FILES,
  type SayFile,
  SETTINGS_WHATS,
  type SettingsResponse,
  type TidyResponse,
  artifactsOf,
  type BriefAction,
  type ChecklistItem,
  type HomeResponse,
  type LibraryItem,
  type MemoryItem,
  type ProjectGap,
  type ProjectNote,
  type OutboxRequest,
  type OutboxResponse,
  type OutboxView,
  type ProjectBrief,
  type ProjectDetailResponse,
  type QuestionView,
  type ActivityItem,
  type OutboxStatus,
  type ReportResponse,
  type RunningNow,
  type SearchHit,
  type StatsResponse,
  type UsageResponse,
  type StepView,
  type PlanItem,
  type SnapshotProject,
  type SnapshotResponse,
  type SnapshotTopic,
  type TopicKind,
  type TurnsResponse,
  type RoomResponse,
  type HandedTarget,
  type CollabView,
  type LocateResponse,
  type TurnRanWith,
  type TopicBrief,
  type TopicDetailResponse,
  type TurnImage,
  type TurnView,
  type WaitingItem,
  type SettingsAsks, type MainMemoryData, type MainMemoryResponse, type UpgradeData, type UpgradeResponse, type QuotaData, type QuotaResponse, type MemoryKeyResponse } from '@shared/api.interface';
import { usageFromSnapshot } from '@shared/usage';
import { collabOf, turnCollabOf } from '@shared/collab';
import { arr, asRows, missingTable, num, obj, oneLine, str, unescapeHtml } from './workbench.util';
import { wake, waitFor } from './workbench.wake';
import { Batcher } from './workbench.guard';
import {
  actionsOf, answersOf, asksOf, backgroundOf, checklistOf, configOf, criteriaOf, deliveryOf, failureOf, gapsOf, gistOf, memoryUndoShape, ownerSendOf, progressOf, projectNotesOf, projectOpShape, recoverShape,
  revisitCancelShape, revisitOf,
} from './workbench.v4';
import { upgradeOf, upgradeShape } from './workbench.upgrade';
import { checkMemoryEdit, devicesSettingsError, launchReposError, reposSettingsError, turnMemoryOf } from './workbench.memory-edit';
import { quotaOf, quotaRefreshShape } from './workbench.quota';

interface TopicRow {
  key: string;
  number: number | null;
  title: string;
  project_key: string | null;
  state: string;
  unseen: boolean;
  updated_at: number;
  data: Record<string, unknown>;
  latest: string | null;
  latest_role: string | null;
  latest_status: string | null;
}
interface ProjectRow {
  key: string;
  title: string;
  status: string;
  updated_at: number;
  data: Record<string, unknown>;
}
interface QuestionRow {
  key: string;
  topic_key: string;
  turn_key: string | null;
  state: string;
  data: Record<string, unknown>;
}
interface SnapTopicRow {
  key: string;
  number: number | null;
  title: string;
  project_key: string | null;
  state: string;
  unseen: boolean;
  updated_at: number;
  kind: string;
  harness: string | null;
  model: string | null;
  model_name: string | null;
  latest: string | null;
  latest_role: string | null;
  round: string | null;
  started_at: number | null;
  context: Record<string, unknown> | null;
  rdata: Record<string, unknown> | null;
  gist: unknown;
  progress: unknown;
  latest_status: string | null;
  latest_key: string | null;
  latest_job: string | null;
  latest_failure: unknown;
  tone: string | null;
  board: string | null;
  joined_at: number | string | null;
  config: unknown;
  revisit: unknown;
  owner_send: unknown;
}
interface SearchRow {
  topic_key: string;
  number: number | null;
  title: string;
  kind: string;
  project_key: string | null;
  seq: number | null;
  role: string | null;
  by: string | null;
  snippet: string;
  at: number;
}
interface TurnRow {
  key: string;
  seq: number;
  role: string;
  status: string;
  body: string;
  data: Record<string, unknown>;
}

/** v7：一轮给人看的正文（去掉内部前缀的 display，没有就是 body）；先 `|| ''` 见下面 SNAP_TOPICS 的说明 */
const SHOWN = sql.raw(`(coalesce(nullif(u.data->>'display', ''), u.body) || '')`);
/** v31：协助方答咨询的那几行（collab.role = helper，含回执行）不算话题自己的最新一轮：侧栏摘要、停住和“先放着”都照它自己的工作算 */
const OWN_TURN = sql.raw(`coalesce(u.data->'collab'->>'role', '') <> 'helper'`);

const TOPIC_SELECT = sql`select t.key, t.number, t.title, t.project_key, t.state, t.unseen, t.updated_at, t.data,
  l.body as latest, l.role as latest_role, l.status as latest_status
  from topics t left join lateral (
    select ${SHOWN} as body, role, status from turns u where u.topic_key = t.key and ${OWN_TURN} order by u.seq desc limit 1
  ) l on true`;

/** 快照用的话题一行：每个话题带最新一轮（lateral + 索引 (topic_key, seq)），正文只取前 600 字。
 * 截正文一律先 `body || ''`：PostgreSQL 17.5 对压缩存放（TOAST）的长中文直接 left()/substring() 只解压一段，
 * 段尾切在汉字中间就报 22021“invalid byte sequence”，整条查询失败（2026-11-18 现网首推后整页 500）。 */
const SNAP_TOPICS = sql`select t.key, t.number, t.title, t.project_key, t.state, t.unseen, t.updated_at,
  coalesce(nullif(t.data->>'kind', ''), 'topic') as kind, t.data->>'harness' as harness, t.data->>'model' as model,
  t.data->>'model_name' as model_name, left(l.body || '', 600) as latest, l.role as latest_role, l.status as latest_status, l.lkey as latest_key, l.ljob as latest_job, l.failure as latest_failure,
  t.data->>'round' as round,
  case when jsonb_typeof(t.data->'started_at') = 'number' then (t.data->>'started_at')::float8 end as started_at,
  t.data->'context' as context, t.data->'gist' as gist, t.data->'progress' as progress, t.data->>'tone' as tone, t.data->>'board' as board,
  case when jsonb_typeof(t.data->'joined_at') = 'number' then (t.data->>'joined_at')::float8 end as joined_at, t.data->'config' as config,
  t.data->'revisit' as revisit, t.data->'owner_send' as owner_send, r.data as rdata
  from topics t left join lateral (
    select ${SHOWN} as body, role, status, u.key as lkey, u.data->>'job_id' as ljob, u.data->'failure' as failure from turns u where u.topic_key = t.key and ${OWN_TURN}
      order by u.seq desc limit 1
  ) l on true
  left join lateral (
    select data from turns w where w.topic_key = t.key and w.status = 'running' and w.role <> 'owner'
      order by w.seq desc limit 1
  ) r on (t.state = '在做' or t.data->>'tone' in ('running', 'stuck'))`;

/** 快照游标往前留多少秒，保证并发写入、同一秒里的写入不漏 */
const CURSOR_OVERLAP_S = 5;
/** 挂着等回执时多久查一次库（毫秒） */
const OUTBOX_STEP_MS = 250;

const UPLOAD_ID = /^[0-9a-f]{32}$/u;
/** 话题、协作、记忆条目的 key：Covey 给的 id（十六进制哈希、web-<op_id> 之类），不含空白和斜杠 */
const ID = /^[A-Za-z0-9_.:-]{1,128}$/u;
const isId = (v: unknown): v is string => typeof v === 'string' && ID.test(v);
/** v31：一句话最多点几位（汇总方 + 3 位协助方，再留一位给“在话题里”）；多了由 Covey 按 route 拒，这里只挡离谱的 */
const SAY_TO_MAX = 8;

/** v31：say 的 to、integrator、quote、room。形状不对抛 400；旧网页发的 to（一个 key）、quote（{key, text}，Covey 从来不读）
 * 换成新形状或去掉，免得开着旧页面的人发不出去 */
function sayRouting(payload: Record<string, unknown>, topicKey: string): void {
  if (payload.room !== undefined && typeof payload.room !== 'boolean') throw new BadRequestException('room 要是布尔');
  if (typeof payload.to === 'string') payload.to = payload.to ? [payload.to] : undefined;
  if (payload.to !== undefined) {
    const to: unknown = payload.to;
    if (!Array.isArray(to) || !to.length || to.length > SAY_TO_MAX || !to.every(isId)) throw new BadRequestException(`to 要是 1–${SAY_TO_MAX} 个参与者的 key`);
    if (new Set(to).size !== to.length) throw new BadRequestException('to 里有重复的参与者');
  }
  if (payload.integrator !== undefined) {
    const to: string[] = arr<string>(payload.to);
    if (!isId(payload.integrator)) throw new BadRequestException('integrator 要是参与者的 key');
    if (!to.includes(payload.integrator) && payload.integrator !== topicKey) throw new BadRequestException('汇总方要是点到的一位或当前话题');
  }
  if (payload.quote !== undefined) {
    const q: Record<string, unknown> = obj(payload.quote);
    if (!q.topic_key && (q.key !== undefined || q.text !== undefined)) {
      delete payload.quote;
      return;
    }
    if (!isId(q.topic_key)) throw new BadRequestException('quote 要 topic_key');
    if (q.seq !== undefined && !(typeof q.seq === 'number' && Number.isInteger(q.seq) && q.seq >= 0)) throw new BadRequestException('quote 的 seq 要是整数');
    if (q.collab !== undefined && !isId(q.collab)) throw new BadRequestException('quote 的 collab 不合法');
    payload.quote = { topic_key: q.topic_key, ...(q.seq !== undefined ? { seq: q.seq } : {}), ...(q.collab !== undefined ? { collab: q.collab } : {}) };
  }
}

/** v31：协作条目，collabs 表还没建时是 [] */
async function collabRows(run: () => Promise<unknown>): Promise<CollabView[]> {
  try {
    return asRows<{ key: string; data: unknown }>(await run()).map((r: { key: string; data: unknown }) => collabOf(r.key, r.data))
      .filter((c: CollabView | null): c is CollabView => c !== null);
  } catch (e) {
    if (missingTable(e)) return [];
    throw e;
  }
}
/** v20：say 的 files，形状不对抛 400；没有是 [] */
function sayFiles(v: unknown): SayFile[] {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v)) throw new BadRequestException('files 要是数组');
  if (v.length > UPLOAD_MAX_FILES) throw new BadRequestException(`一句话最多 ${UPLOAD_MAX_FILES} 个附件`);
  return v.map((x: unknown): SayFile => {
    const d: Record<string, unknown> = obj(x);
    const id: string = str(d.id);
    const name: string = str(d.name);
    const size: number = num(d.size);
    if (!UPLOAD_ID.test(id)) throw new BadRequestException('附件 id 不合法');
    if (!name || Array.from(name).length > 200) throw new BadRequestException('附件名不合法');
    if (size <= 0 || size > UPLOAD_MAX_BYTES) throw new BadRequestException('附件大小不合法');
    return { id, name, mime: str(d.mime).slice(0, 128) || 'application/octet-stream', size };
  });
}

function runningNow(d: Record<string, unknown>): RunningNow {
  const steps: StepView[] = arr<StepView>(d.steps);
  const plan: PlanItem[] = arr<PlanItem>(d.plan);
  const live: StepView | undefined = [...steps].reverse().find((x: StepView) => x.status === 'running');
  return {
    step_label: unescapeHtml(str(live?.label)),
    step_kind: str(live?.kind),
    plan_done: plan.filter((x: PlanItem) => x.state === 'done').length,
    plan_total: plan.length,
    plan_doing: str(plan.find((x: PlanItem) => x.state === 'doing')?.title),
    started_at: typeof d.started_at === 'number' ? d.started_at : null,
  };
}

/** v4 的话题字段：有才带上，形状不对就不带 */
function v4Topic(r: SnapTopicRow): Partial<SnapshotTopic> {
  const out: Partial<SnapshotTopic> = {};
  const gist = gistOf(r.gist);
  const progress = progressOf(r.progress);
  const failure = failureOf(r.latest_failure);
  if (gist) out.gist = gist;
  if (progress) out.progress = progress;
  if (r.latest_status) out.latest_status = r.latest_status;
  if (r.latest_key) out.latest_key = r.latest_key;
  // v33 recover 要任务号：看板上停住的那张卡直接点「检查并继续」
  if (r.latest_job && /^\d{1,12}$/u.test(r.latest_job)) out.latest_job_id = r.latest_job;
  if (failure) out.latest_failure = failure;
  if (r.tone) out.tone = r.tone;
  if (typeof r.board === 'string') out.board = r.board;
  if (r.joined_at !== null && r.joined_at !== undefined && Number(r.joined_at) > 0) out.joined_at = Number(r.joined_at);
  // v20 话题现在的配置；v36 起负责人也带（它的换配置）。管家的不在这里换
  const config = r.kind === 'topic' || r.kind === 'lead' || !r.kind ? configOf(r.config) : undefined;
  if (config) out.config = config;
  // v34：约好到点回来看、等你确认的代发草稿（Covey 开关关着或没有时不推，这里也就没有）
  const revisit = revisitOf(r.revisit);
  if (revisit) out.revisit = revisit;
  const drafts = ownerSendOf(r.owner_send);
  if (drafts) out.owner_send = drafts;
  return out;
}

/** v4 的一轮字段：answers（这一轮是回答问题卡）、failure（没做成）；v7：by、via、display、edited、note */
function v4Turn(d: Record<string, unknown>): Partial<TurnView> {
  const out: Partial<TurnView> = {};
  const answers = answersOf(d.answers);
  const failure = failureOf(d.failure);
  if (answers) out.answers = answers;
  if (failure) out.failure = failure;
  if (d.by === 'owner' || d.by === 'lead' || d.by === 'butler') out.by = d.by;
  if (d.via === 'lead' || d.via === 'butler') out.via = d.via;
  if (typeof d.display === 'string' && d.display) out.display = d.display;
  if (d.edited === true) out.edited = true;
  if (d.merged === true) out.merged = true;
  if (typeof d.note === 'string' && d.note) out.note = d.note;
  const handed: HandedTarget[] = handedOf(d.handed);
  if (handed.length) out.handed = handed;
  if (typeof d.relayed_at === 'number' && d.relayed_at > 0) out.relayed_at = d.relayed_at;
  const ran: TurnRanWith = ranWithOf(d.ran_with);
  if (Object.keys(ran).length) out.ran_with = ran;
  return out;
}

/** v31 的一轮字段：属于哪次协作（collab）、主人那句话的消息 id（记忆的“查看原文”按它找） */
function v31Turn(d: Record<string, unknown>): Partial<TurnView> {
  const out: Partial<TurnView> = {};
  const collab = turnCollabOf(d.collab);
  if (collab) out.collab = collab;
  if (typeof d.message_id === 'string' && d.message_id && d.message_id.length <= 160) out.message_id = d.message_id;
  return out;
}

/** v34 的一轮字段：完成标准（criteria）和交付（delivery），来自这一轮的检查点，没有就不带 */
function v34Turn(d: Record<string, unknown>): Partial<TurnView> {
  const out: Partial<TurnView> = {};
  const criteria = criteriaOf(d.criteria);
  if (criteria) out.criteria = criteria;
  const delivery = deliveryOf(d.delivery);
  if (delivery) out.delivery = delivery;
  return out;
}

/** v43 的一轮字段：这一轮带了几条记忆（memory，Covey 推来才有） */
function v43Turn(d: Record<string, unknown>): Partial<TurnView> {
  const memory = turnMemoryOf(d.memory);
  return memory ? { memory } : {};
}

/** v27：这一轮用的什么配置，只留认得的字段、每个最多 60 字 */
function ranWithOf(v: unknown): TurnRanWith {
  const o: Record<string, unknown> = obj(v);
  const out: TurnRanWith = {};
  for (const k of ['harness', 'provider', 'model', 'effort', 'workspace'] as const) {
    if (typeof o[k] === 'string' && o[k]) out[k] = (o[k] as string).slice(0, 60);
  }
  return out;
}

/** v23：管家这一轮交给了谁，只留认得的字段 */
function handedOf(v: unknown): HandedTarget[] {
  return arr<unknown>(v).map((x: unknown) => obj(x)).filter((x: Record<string, unknown>) => typeof x.key === 'string' && x.key
    && (x.kind === 'topic' || x.kind === 'lead')).slice(0, 8).map((x: Record<string, unknown>): HandedTarget => ({
    key: str(x.key), kind: x.kind === 'lead' ? 'lead' : 'topic', number: num(x.number), title: str(x.title),
    project_key: str(x.project_key), how: str(x.how),
  }));
}

/** 大厅每类最多取多少轮：管家、每位负责人、发到私聊的话题结论 */
const ROOM_BUTLER = 160;
const ROOM_LEAD = 60;
const ROOM_RELAYED = 80;

/** Covey 的本周用量快照 → 看板右栏的数：总 token = input + output（input 已含缓存读），缓存命中 = cache_read / input，每日取 daily 最后 7 天 */
function weekFromUsage(u: Record<string, unknown>): {
  series: number[]; tokens: number; tasks: number; topics: number; cachePct: number | null; harnesses: { title: string; tasks: number; tokens: number }[];
} {
  const t: Record<string, unknown> = obj(u.tokens);
  const input: number = num(t.input_tokens);
  const daily: number[] = arr<Record<string, unknown>>(u.daily).slice(-7).map((d: Record<string, unknown>) => num(d.tokens));
  return {
    series: [...new Array<number>(Math.max(0, 7 - daily.length)).fill(0), ...daily],
    tokens: input + num(t.output_tokens),
    tasks: num(u.tasks),
    topics: num(u.topics),
    cachePct: input > 0 ? Math.round((num(t.cache_read_tokens) / input) * 100) : null,
    harnesses: arr<Record<string, unknown>>(u.harnesses).map((h: Record<string, unknown>) => ({ title: str(h.title), tasks: num(h.tasks), tokens: num(h.tokens) })),
  };
}

const NUMF = (col: string, key: string) =>
  sql.raw(`case when jsonb_typeof(${col}->'${key}') = 'number' then (${col}->>'${key}')::float8 end`);

const TURN_COLS = sql`key, seq, role, status, body, data`;

/** v19：一轮的图片，只留认得的字段（id 是十六进制哈希，过期的没有 id） */
function turnImages(v: unknown): TurnImage[] {
  return arr<unknown>(v).map((x: unknown) => obj(x)).filter((x: Record<string, unknown>) => typeof x.id === 'string' && /^[0-9a-f]{0,64}$/u.test(x.id))
    .slice(0, 9).map((x: Record<string, unknown>): TurnImage => ({
      id: str(x.id),
      ...(typeof x.w === 'number' ? { w: x.w } : {}),
      ...(typeof x.h === 'number' ? { h: x.h } : {}),
      kind: x.kind === 'shown' ? 'shown' : 'sent',
      ...(typeof x.note === 'string' && x.note ? { note: x.note.slice(0, 200) } : {}),
      ...(x.gone === true ? { gone: true } : {}),
    }));
}

@Injectable()
export class WorkbenchQueryService {
  /** 挂着等回执的请求各自每 250 毫秒查一次：同一时刻的合成一条 `op_id in (...)`（T876） */
  private readonly acks: Batcher<string, OutboxStatus>;

  constructor(@Inject(WB_DB) private readonly db: PostgresJsDatabase) {
    this.acks = new Batcher<string, OutboxStatus>(async (ids: string[]): Promise<Map<string, OutboxStatus>> => {
      const rows = asRows<OutboxStatus>(await this.db.execute(sql`select op_id, kind, state, coalesce(message, '') as message, _created_at as created_at
        from outbox where op_id in (${sql.join(ids.map((x: string) => sql`${x}`), sql`, `)})`));
      return new Map(rows.map((r: OutboxStatus) => [r.op_id, r]));
    });
  }

  private brief(r: TopicRow): TopicBrief {
    return {
      key: r.key,
      number: r.number,
      title: r.title,
      project_key: r.project_key,
      state: r.state,
      unseen: r.unseen,
      updated_at: num(r.updated_at),
      harness: str(r.data?.harness),
      model: str(r.data?.model),
      ...(str(r.data?.model_name) ? { model_name: str(r.data?.model_name) } : {}),
      latest: oneLine(r.latest ?? ''),
      latest_role: r.latest_role ?? '',
      latest_status: r.latest_status ?? '',
    };
  }

  private questionView(q: QuestionRow): QuestionView {
    const d: Record<string, unknown> = obj(q.data);
    return {
      token: q.key,
      topic_key: q.topic_key,
      turn_key: q.turn_key,
      questions: arr(d.questions),
      state: q.state,
      answer: d.answer ?? null,
      extras: d.extras === true,
      restorable: d.restorable === true,
      closed_at: typeof d.closed_at === 'number' ? d.closed_at : null,
      asked_at: typeof d.asked_at === 'number' ? d.asked_at : null,
    };
  }

  private async syncedAt(): Promise<string | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = 'last_sync'`);
    const row = asRows<{ value: Record<string, unknown> }>(res)[0];
    return row ? str(row.value?.at) || null : null;
  }

  private async outboxFor(column: 'topic_key' | 'project_key', key: string, n: number): Promise<OutboxView[]> {
    const res =
      column === 'topic_key'
        ? await this.db.execute(sql`select seq::int as seq, op_id, kind, topic_key, project_key, payload, state, message,
            _created_at as created_at from outbox where topic_key = ${key} order by seq desc limit ${n}`)
        : await this.db.execute(sql`select seq::int as seq, op_id, kind, topic_key, project_key, payload, state, message,
            _created_at as created_at from outbox where project_key = ${key} order by seq desc limit ${n}`);
    return asRows<OutboxView>(res);
  }

  async home(): Promise<HomeResponse> {
    const topicRows = asRows<TopicRow>(
      await this.db.execute(sql`${TOPIC_SELECT} order by t.updated_at desc`),
    );
    const questionRows = asRows<QuestionRow>(
      await this.db.execute(sql`select key, topic_key, turn_key, state, data from questions where state = 'open'`),
    );
    const projectRows = asRows<ProjectRow>(
      await this.db.execute(sql`select key, title, status, updated_at, data from projects order by updated_at desc`),
    );
    const briefs: TopicBrief[] = topicRows.map((r: TopicRow) => this.brief(r));
    const byKey = new Map<string, TopicBrief>(briefs.map((b: TopicBrief) => [b.key, b]));
    const waiting: WaitingItem[] = [];
    const seen = new Set<string>();
    for (const q of questionRows) {
      const topic: TopicBrief | undefined = byKey.get(q.topic_key);
      if (!topic) continue;
      seen.add(topic.key);
      waiting.push({ topic, question: this.questionView(q) });
    }
    for (const b of briefs) {
      if (b.state === '等你' && !seen.has(b.key)) waiting.push({ topic: b, question: null });
    }
    const projects: ProjectBrief[] = projectRows.map((p: ProjectRow): ProjectBrief => {
      const own: TopicBrief[] = briefs.filter((b: TopicBrief) => b.project_key === p.key);
      return {
        key: p.key,
        title: p.title,
        status: p.status,
        status_reason: str(p.data?.status_reason),
        goal: str(p.data?.goal),
        topic_count: own.length,
        waiting_count: own.filter((b: TopicBrief) => b.state === '等你').length,
        running_count: own.filter((b: TopicBrief) => b.state === '在做').length,
        updated_at: num(p.updated_at),
      };
    });
    const running: TopicBrief[] = briefs.filter((b: TopicBrief) => b.state === '在做');
    const unseen: TopicBrief[] = briefs.filter((b: TopicBrief) => b.state === '做完' && b.unseen);
    const shown = new Set<string>([...waiting.map((w: WaitingItem) => w.topic.key), ...running.map((b: TopicBrief) => b.key), ...unseen.map((b: TopicBrief) => b.key)]);
    const loose: TopicBrief[] = briefs.filter((b: TopicBrief) => !b.project_key && !shown.has(b.key));
    return {
      waiting,
      running,
      unseen,
      projects,
      loose,
      synced_at: await this.syncedAt(),
    };
  }

  async project(key: string): Promise<ProjectDetailResponse> {
    const res = await this.db.execute(
      sql`select key, title, status, updated_at, data from projects where key = ${key}`,
    );
    const p: ProjectRow | undefined = asRows<ProjectRow>(res)[0];
    if (!p) throw new NotFoundException('没有这个专项');
    const topicRows = asRows<TopicRow>(
      await this.db.execute(sql`${TOPIC_SELECT} where t.project_key = ${key} order by t.number asc nulls last`),
    );
    const d: Record<string, unknown> = obj(p.data);
    return {
      key: p.key,
      title: p.title,
      goal: str(d.goal),
      status: p.status,
      status_reason: str(d.status_reason),
      lead_topic_key: str(d.lead_topic_key) || null,
      memory: arr<MemoryItem>(d.memory),
      library: arr<LibraryItem>(d.library),
      updated_at: num(p.updated_at),
      topics: topicRows.map((r: TopicRow) => this.brief(r)),
      outbox: await this.outboxFor('project_key', key, 10),
      synced_at: await this.syncedAt(),
    };
  }

  async topic(key: string): Promise<TopicDetailResponse> {
    const res = await this.db.execute(sql`${TOPIC_SELECT} where t.key = ${key}`);
    const t: TopicRow | undefined = asRows<TopicRow>(res)[0];
    if (!t) throw new NotFoundException('没有这个话题');
    const turnRows = asRows<TurnRow>(
      await this.db.execute(
        sql`select key, seq, role, status, body, data from turns where topic_key = ${key} order by seq asc`,
      ),
    );
    const qRow: QuestionRow | undefined = asRows<QuestionRow>(
      await this.db.execute(
        sql`select key, topic_key, turn_key, state, data from questions
          where topic_key = ${key} and (state = 'open' or data->>'restorable' = 'true') order by _created_at desc limit 1`,
      ),
    )[0];
    let projectTitle: string | null = null;
    if (t.project_key) {
      const pr = asRows<{ title: string }>(
        await this.db.execute(sql`select title from projects where key = ${t.project_key}`),
      )[0];
      projectTitle = pr?.title ?? null;
    }
    const turns: TurnView[] = turnRows.map((r: TurnRow): TurnView => {
      const d: Record<string, unknown> = obj(r.data);
      return {
        key: r.key,
        seq: r.seq,
        role: r.role,
        status: r.status,
        body: r.body,
        job_id: str(d.job_id) || null,
        started_at: typeof d.started_at === 'number' ? d.started_at : null,
        ended_at: typeof d.ended_at === 'number' ? d.ended_at : null,
        artifacts: artifactsOf(d.artifacts),
      };
    });
    return {
      topic: this.brief(t),
      workspace: str(t.data?.workspace),
      project_title: projectTitle,
      turns,
      question: qRow ? this.questionView(qRow) : null,
      outbox: await this.outboxFor('topic_key', key, 20),
      synced_at: await this.syncedAt(),
    };
  }

  private turnView(r: TurnRow): TurnView {
    const d: Record<string, unknown> = obj(r.data);
    return {
      key: r.key,
      seq: r.seq,
      role: r.role,
      status: r.status,
      body: r.body,
      job_id: str(d.job_id) || null,
      started_at: typeof d.started_at === 'number' ? d.started_at : null,
      ended_at: typeof d.ended_at === 'number' ? d.ended_at : null,
      artifacts: artifactsOf(d.artifacts),
      ...(Array.isArray(d.steps) ? { steps: arr<StepView>(d.steps).map((x: StepView) => (typeof x.label === 'string' ? { ...x, label: unescapeHtml(x.label) } : x)) } : {}),
      ...(Array.isArray(d.plan) ? { plan: arr<PlanItem>(d.plan) } : {}),
      ...(d.usage && typeof d.usage === 'object' ? { usage: obj(d.usage) } : {}),
      ...(typeof d.seconds === 'number' ? { seconds: d.seconds } : {}),
      ...(typeof d.speed === 'number' ? { speed: d.speed } : {}),
      ...(d.context && typeof d.context === 'object' ? { context: obj(d.context) as { used: number; limit: number } } : {}),
      ...(Array.isArray(d.notes) ? { notes: arr<NonNullable<TurnView['notes']>[number]>(d.notes) } : {}),
      ...(Array.isArray(d.started_topics)
        ? { started_topics: arr<NonNullable<TurnView['started_topics']>[number]>(d.started_topics) }
        : {}),
      ...(Array.isArray(d.images) ? { images: turnImages(d.images) } : {}),
      ...v4Turn(d),
      ...v31Turn(d),
      ...v34Turn(d),
      ...v43Turn(d),
    };
  }

  private snapTopic(r: SnapTopicRow): SnapshotTopic {
    const kind: TopicKind = r.kind === 'butler' ? 'butler' : r.kind === 'lead' ? 'lead' : 'topic';
    const ctx = obj(r.context);
    return {
      round: num(Number(r.round)),
      started_at: typeof r.started_at === 'number' ? r.started_at : null,
      context: typeof ctx.used === 'number' && typeof ctx.limit === 'number' ? { used: ctx.used, limit: ctx.limit } : null,
      running: r.rdata ? runningNow(obj(r.rdata)) : null,
      key: r.key,
      number: r.number,
      title: r.title,
      kind,
      project_key: r.project_key || null,
      state: r.state,
      unseen: kind !== 'topic' ? false : r.unseen,
      updated_at: num(r.updated_at),
      harness: r.harness ?? '',
      model: r.model ?? '',
      ...(r.model_name ? { model_name: r.model_name } : {}),
      latest: oneLine(r.latest ?? ''),
      latest_role: r.latest_role ?? '',
      ...v4Topic(r),
    };
  }

  /**
   * 侧栏、看板、专项页的列表都从这里来：几条 SQL 并行，一次返回。
   * since（v38，上一次响应的 cursor）：只返回这之后变过的话题（话题行或它的轮次的 _updated_at 晚于 since），外加 keys（现在所有话题的 key）
   * 和新的 cursor，响应带 partial: true；projects、questions、asks 照旧整份。since 不合法就当没带，返回整份。每次响应都带 cursor。
   * cursor 是查询开始时数据库的 now() 往前留 CURSOR_OVERLAP_S 秒：并发写入、同一秒里的写入都不会漏，重叠的部分前端按 key 合并。
   */
  async snapshot(sinceRaw: string | null = null): Promise<SnapshotResponse> {
    const sinceMs: number = sinceRaw ? Date.parse(sinceRaw) : NaN;
    const since: string | null = Number.isNaN(sinceMs) ? null : new Date(sinceMs).toISOString();
    const changed = since
      ? sql` where t._updated_at > ${since}::timestamptz or t.key in (select topic_key from turns where _updated_at > ${since}::timestamptz)`
      : sql``;
    // 游标和数据查询一起发：数据库的时钟，往前留的几秒盖得住它们之间的先后
    const [cursorRes, topicRes, lightRes, projectRes, questionRes, syncedAt, asks] = await Promise.all([
      this.db.execute(sql`select now() - make_interval(secs => ${CURSOR_OVERLAP_S}) as c`),
      this.db.execute(sql`${SNAP_TOPICS}${changed} order by t.updated_at desc`),
      since
        ? this.db.execute(sql`select key, project_key, state, coalesce(nullif(data->>'kind', ''), 'topic') as kind, updated_at from topics order by updated_at desc`)
        : Promise.resolve([]),
      this.db.execute(sql`select key, title, status, updated_at, data from projects order by updated_at desc`),
      this.db.execute(sql`select key, topic_key, turn_key, state, data from questions where state = 'open'`),
      this.syncedAt(),
      this.snapshotData('asks'),
    ]);
    const cursor: string = new Date(asRows<{ c: string | Date }>(cursorRes)[0]?.c ?? Date.now()).toISOString();
    const all: SnapshotTopic[] = asRows<SnapTopicRow>(topicRes).map((r: SnapTopicRow) => this.snapTopic(r));
    // 网页里 kind=butler 的话题只留一个（updated_at 最新，已按它倒序）。增量时变过的只是一部分，最新的那个要从所有话题里找
    interface Light { key: string; project_key: string | null; state: string; kind: string }
    const lights: Light[] = since ? asRows<Light>(lightRes) : [];
    const butlerKey: string | undefined = since ? lights.find((t: Light) => t.kind === 'butler')?.key : all.find((t: SnapshotTopic) => t.kind === 'butler')?.key;
    const topics: SnapshotTopic[] = all.filter((t: SnapshotTopic) => t.kind !== 'butler' || t.key === butlerKey);
    // 专项卡上的话题计数要算所有话题，不只是变过的
    const counted: Light[] = since ? lights.filter((t: Light) => t.kind !== 'butler' || t.key === butlerKey) : topics;
    const projects: SnapshotProject[] = asRows<ProjectRow>(projectRes).map((p: ProjectRow): SnapshotProject => {
      const d: Record<string, unknown> = obj(p.data);
      const own: Light[] = counted.filter((t: Light) => (t.project_key || null) === p.key && t.kind !== 'lead');
      // v40：「进度」标签的三样，按类型过滤；没推的不带这个键
      const checklist: ChecklistItem[] | undefined = checklistOf(d.checklist);
      const milestones: ProjectNote[] | undefined = projectNotesOf(d.milestones);
      const decisions: ProjectNote[] | undefined = projectNotesOf(d.decisions);
      // v41：「简报」的背景、动作、缺口，同样按类型过滤
      const background: string | undefined = backgroundOf(d.background);
      const actions: BriefAction[] | undefined = actionsOf(d.actions);
      const gaps: ProjectGap[] | undefined = gapsOf(d.gaps);
      return {
        key: p.key,
        avatar: str(d.avatar),
        title: p.title,
        goal: str(d.goal),
        status: p.status,
        status_reason: str(d.status_reason),
        lead_topic_key: str(d.lead_topic_key) || null,
        started_at: typeof d.started_at === 'number' ? d.started_at : null,
        topic_keys: arr<string>(d.topic_keys),
        memory: arr<MemoryItem>(d.memory),
        ...(/^[0-9a-f]{1,64}$/u.test(str(d.memory_version)) ? { memory_version: str(d.memory_version) } : {}),
        library: arr<LibraryItem>(d.library),
        updated_at: num(p.updated_at),
        counts: {
          topics: own.length,
          waiting: own.filter((t: Light) => t.state === '等你').length,
          running: own.filter((t: Light) => t.state === '在做').length,
        },
        ...(checklist ? { checklist } : {}),
        ...(milestones ? { milestones } : {}),
        ...(decisions ? { decisions } : {}),
        ...(background ? { background } : {}),
        ...(actions ? { actions } : {}),
        ...(gaps ? { gaps } : {}),
      };
    });
    const asked: SettingsAsks | undefined = asksOf(asks);
    return {
      synced_at: syncedAt,
      butler_key: (since ? butlerKey : topics.find((t: SnapshotTopic) => t.kind === 'butler')?.key) ?? null,
      projects,
      topics,
      questions: asRows<QuestionRow>(questionRes).map((q: QuestionRow) => this.questionView(q)),
      // v34：Covey 算好的要你处理，和话题同一步推来；旧 Covey 没推过就不带，网页自己数
      ...(asked ? { asks: asked } : {}),
      cursor,
      ...(since ? { partial: true as const, keys: counted.map((t: Light) => t.key) } : {}),
    };
  }

  /** 增量 turns：seq > after 的，加上在跑的那一轮（每次都带）；after 省略 = 最近 200 轮 */
  /**
   * after：增量取数的起点（seq），只取它之后的和在跑的。since（上一次响应的 cursor）：另外带上这之后改过的轮次，排在在跑那一轮
   * 前面、后来才补全的那几行（你那一行的图后传上来、编辑并进原消息、答题记上回答）也换成新的，不用刷新页面。每次响应都带 cursor，
   * 和快照一样是数据库的 now() 往前留 CURSOR_OVERLAP_S 秒；since 不合法就当没带。
   */
  async turns(key: string, after: number | null, sinceRaw: string | null = null): Promise<TurnsResponse> {
    const sinceMs: number = sinceRaw ? Date.parse(sinceRaw) : NaN;
    const since: string | null = Number.isNaN(sinceMs) ? null : new Date(sinceMs).toISOString();
    const changed = since ? sql` or _updated_at > ${since}::timestamptz` : sql``;
    const [cursorRes, headRes, turnRes, qRes, outbox] = await Promise.all([
      this.db.execute(sql`select now() - make_interval(secs => ${CURSOR_OVERLAP_S}) as c`),
      this.db.execute(sql`${SNAP_TOPICS} where t.key = ${key}`),
      after === null
        ? this.db.execute(sql`select * from (select ${TURN_COLS} from turns where topic_key = ${key}
            order by seq desc limit 200) x order by seq asc`)
        : this.db.execute(sql`select ${TURN_COLS} from turns where topic_key = ${key}
            and (seq > ${after} or status = 'running'${changed}) order by seq asc limit 500`),
      this.db.execute(sql`select key, topic_key, turn_key, state, data from questions
        where topic_key = ${key} and (state = 'open' or data->>'restorable' = 'true') order by _created_at desc limit 1`),
      this.outboxFor('topic_key', key, 20),
    ]);
    const head: SnapTopicRow | undefined = asRows<SnapTopicRow>(headRes)[0];
    if (!head) throw new NotFoundException('没有这个话题');
    const [extraRes, collabs] = await Promise.all([
      this.db.execute(sql`select t.data->>'workspace' as workspace, t.data->'memory' as memory,
        (select title from projects p where p.key = t.project_key) as project_title from topics t where t.key = ${key}`),
      // v31：它当汇总方或协助方的协作；增量取时只带还开着的和两小时内动过的（页面按 key 合并，漏一次轮询也补得上）
      collabRows(() => this.db.execute(sql`select key, data from collabs
        where (integrator = ${key} or data->'helpers' @> jsonb_build_array(${key}::text))
          ${after === null ? sql`` : sql`and (state = 'open' or _updated_at > now() - interval '2 hours')`}
        order by updated_at desc limit 20`)),
    ]);
    const extra = asRows<{ workspace: string | null; project_title: string | null; memory: unknown }>(extraRes)[0];
    const q: QuestionRow | undefined = asRows<QuestionRow>(qRes)[0];
    const memory: MemoryItem[] = arr<MemoryItem>(extra?.memory);
    return {
      topic: { ...this.snapTopic(head), workspace: extra?.workspace ?? '', project_title: extra?.project_title ?? null },
      turns: asRows<TurnRow>(turnRes).map((r: TurnRow) => this.turnView(r)),
      cursor: new Date(asRows<{ c: string | Date }>(cursorRes)[0]?.c ?? Date.now()).toISOString(),
      question: q ? this.questionView(q) : null,
      outbox,
      ...(collabs.length ? { collabs } : {}),
      ...(memory.length ? { memory } : {}),
    };
  }

  /** 大厅：管家的对话、各负责人的对话、结论发到私聊的话题轮次，一条时间线（前端按时间排）。
   * after 省略 = 各取最近一段；after 是上次的 cursor（行的 _updated_at），只取这之后改过的和在跑的 */
  async room(after: string | null): Promise<RoomResponse> {
    const since = after && !Number.isNaN(Date.parse(after)) ? new Date(after).toISOString() : null;
    const fresh = since ? sql`and (u._updated_at > ${since}::timestamptz or u.status = 'running')` : sql``;
    const butlerRow = asRows<{ key: string }>(await this.db.execute(sql`select key from topics
      where data->>'kind' = 'butler' order by updated_at desc limit 1`))[0];
    const butler: string | null = butlerRow?.key ?? null;
    const freshCollab = since ? sql`where _updated_at > ${since}::timestamptz` : sql``;
    const [mine, leads, relayed, outRes, collabRes] = await Promise.all([
      butler
        ? this.db.execute(sql`select * from (select u.topic_key, u.key, u.seq, u.role, u.status, u.body, u.data, u._updated_at as at
            from turns u where u.topic_key = ${butler} ${fresh} order by u.seq desc limit ${ROOM_BUTLER}) x`)
        : Promise.resolve([]),
      this.db.execute(sql`select * from (select u.topic_key, u.key, u.seq, u.role, u.status, u.body, u.data, u._updated_at as at,
          row_number() over (partition by u.topic_key order by u.seq desc) as rn
          from turns u where u.topic_key in (select key from topics where data->>'kind' = 'lead') ${fresh}) x where x.rn <= ${ROOM_LEAD}`),
      this.db.execute(sql`select u.topic_key, u.key, u.seq, u.role, u.status, u.body, u.data, u._updated_at as at from turns u
          where u.data->>'relayed_at' is not null ${fresh} order by (u.data->>'relayed_at')::float desc limit ${ROOM_RELAYED}`),
      this.db.execute(sql`select seq::int as seq, op_id, kind, topic_key, project_key, payload, state, message, _created_at as created_at
          from outbox where kind = 'say' and (payload->>'room' = 'true' or topic_key = ${butler ?? ''}) order by seq desc limit 40`),
      // v31：最近的协作，和轮次一样按 cursor（行的 _updated_at）增量取
      this.db.execute(sql`select key, data, _updated_at as at from collabs ${freshCollab} order by updated_at desc limit 40`)
        .catch((e: unknown) => { if (missingTable(e)) return []; throw e; }),
    ]);
    type RoomRow = TurnRow & { topic_key: string; at: string | Date };
    const rows: RoomRow[] = [...asRows<RoomRow>(mine), ...asRows<RoomRow>(leads), ...asRows<RoomRow>(relayed)];
    let cursor: string | null = since;
    const seen = new Set<string>();
    const turns: TurnView[] = [];
    for (const r of rows) {
      const at: string = new Date(r.at).toISOString();
      if (!cursor || at > cursor) cursor = at;
      if (seen.has(r.key)) continue;
      seen.add(r.key);
      turns.push({ ...this.turnView(r), topic_key: r.topic_key });
    }
    const collabs: CollabView[] = [];
    for (const r of asRows<{ key: string; data: unknown; at: string | Date }>(collabRes)) {
      const at: string = new Date(r.at).toISOString();
      if (!cursor || at > cursor) cursor = at;
      const c: CollabView | null = collabOf(r.key, r.data);
      if (c) collabs.push(c);
    }
    return { butler_key: butler, turns, outbox: asRows<OutboxView>(outRes), cursor, ...(collabs.length ? { collabs } : {}) };
  }

  /** v31：记忆的“查看原文”：一道题（question 是它的 token）或一条消息（message_id）在哪个话题的哪一轮。
   * 消息先按一轮带的 message_id 找；网页发的（web-<op_id>）退回发件箱，只知道话题。找不到 404 */
  async locate(messageId: string, question: string): Promise<LocateResponse> {
    let topicKey = '';
    let seq: number | null = null;
    let turnKey: string | null = null;
    if (question) {
      const row = asRows<{ topic_key: string; turn_key: string | null; seq: number | null }>(await this.db.execute(sql`select q.topic_key, q.turn_key,
          u.seq from questions q left join turns u on u.key = q.turn_key where q.key = ${question} limit 1`))[0];
      if (row) [topicKey, seq, turnKey] = [row.topic_key, row.seq ?? null, row.turn_key ?? null];
    }
    if (!topicKey && messageId) {
      const row = asRows<{ topic_key: string; seq: number; key: string }>(await this.db.execute(sql`select topic_key, seq, key from turns
          where data->>'message_id' = ${messageId} order by seq asc limit 1`))[0];
      if (row) [topicKey, seq, turnKey] = [row.topic_key, row.seq, row.key];
      else if (messageId.startsWith('web-')) {
        const op = asRows<{ topic_key: string | null }>(await this.db.execute(sql`select topic_key from outbox where op_id = ${messageId.slice(4)}`))[0];
        topicKey = op?.topic_key ?? '';
      }
    }
    const t = topicKey
      ? asRows<{ kind: string; project_key: string | null }>(await this.db.execute(sql`select coalesce(nullif(data->>'kind', ''), 'topic') as kind,
          project_key from topics where key = ${topicKey}`))[0]
      : undefined;
    if (!t) throw new NotFoundException('网页上找不到原文（可能太早，或只在飞书里）');
    const kind: TopicKind = t.kind === 'butler' ? 'butler' : t.kind === 'lead' ? 'lead' : 'topic';
    return { topic_key: topicKey, kind, project_key: t.project_key || null, seq, turn_key: turnKey };
  }

  /** 看板指标：今天（Asia/Shanghai）的 token、话题共工作时长、平均等你，加最近动态 */
  async stats(): Promise<StatsResponse> {
    const [turnRes, qRes, evRes, qevRes, openRes, weekRes] = await Promise.all([
      this.db.execute(sql`select ${NUMF('u.data', 'started_at')} as s, ${NUMF('u.data', 'ended_at')} as e,
          ${NUMF('u.data', 'seconds')} as sec, ${NUMF("u.data->'usage'", 'total')} as tok, u.status as status
        from turns u where u.role not in ('owner', 'event') and u._updated_at > now() - interval '36 hours' limit 4000`),
      this.db.execute(sql`select ${NUMF('q.data', 'asked_at')} as a, ${NUMF('q.data', 'answered_at')} as b
        from questions q where q._updated_at > now() - interval '36 hours' limit 2000`),
      this.db.execute(sql`select ${NUMF('u.data', 'ended_at')} as at, u.status, t.key as topic_key, t.number, t.title,
          left(u.body || '', 80) as body from turns u join topics t on t.key = u.topic_key
        where u.role <> 'owner' and u.status in ('done', 'failed') and ${NUMF('u.data', 'ended_at')} is not null
          and coalesce(t.data->>'kind', '') <> 'butler'
        order by ${NUMF('u.data', 'ended_at')} desc limit 12`),
      this.db.execute(sql`select ${NUMF('q.data', 'asked_at')} as a, ${NUMF('q.data', 'answered_at')} as b,
          t.key as topic_key, t.number, t.title from questions q join topics t on t.key = q.topic_key
        where ${NUMF('q.data', 'asked_at')} is not null order by ${NUMF('q.data', 'asked_at')} desc limit 12`),
      this.db.execute(sql`select ${NUMF('q.data', 'asked_at')} as a from questions q where q.state = 'open'`),
      // 近 7 天每天（北京时间）的 token 和结束的轮数：看板右栏的「本周用量」
      this.db.execute(sql`select floor((x.e + 28800) / 86400)::int as d, coalesce(sum(x.tok), 0)::float8 as tok, count(*)::int as n,
          coalesce(sum(x.inp), 0)::float8 as inp, coalesce(sum(x.cr), 0)::float8 as cr
        from (select ${NUMF('u.data', 'ended_at')} as e, ${NUMF("u.data->'usage'", 'total')} as tok,
            ${NUMF("u.data->'usage'", 'input')} as inp, ${NUMF("u.data->'usage'", 'cache_read')} as cr from turns u
          where u.role not in ('owner', 'event') and u.status in ('done', 'failed')
            and ${NUMF('u.data', 'ended_at')} > extract(epoch from now()) - 8 * 86400) x
        group by 1`),
    ]);
    const now: number = Date.now() / 1000;
    // 看板右栏的「近 24 小时」：往前 24 小时，不按 0 点切（主人常在 0 点以后干活，按自然日凌晨只剩一两个小时的数）
    const since: number = now - 86400;
    const hourBuckets = (): number[] => new Array<number>(12).fill(0);
    const tokS: number[] = hourBuckets();
    const workS: number[] = hourBuckets();
    const bucket = (t: number): number => Math.min(11, Math.max(0, 11 - Math.floor((now - t) / 3600)));
    let tokens = 0;
    let work = 0;
    let done = 0;
    for (const r of asRows<{ s: number | null; e: number | null; sec: number | null; tok: number | null; status: string }>(turnRes)) {
      if (r.status === 'running' && r.s) {
        // 还在跑的一轮：这 24 小时里已经干了多久也算进工作时间
        work += Math.max(0, now - Math.max(r.s, since));
        continue;
      }
      const end: number = r.e ?? r.s ?? 0;
      if (end >= now - 12 * 3600) {
        tokS[bucket(end)] += num(r.tok);
        workS[bucket(end)] += num(r.sec) || (r.s && r.e ? r.e - r.s : 0);
      }
      if (end < since) continue;
      tokens += num(r.tok);
      work += num(r.sec) || (r.s && r.e ? r.e - r.s : 0);
      if (r.e && r.status === 'done') done += 1;
    }
    const waits: number[] = [];
    const waitS: number[] = hourBuckets();
    for (const r of asRows<{ a: number | null; b: number | null }>(qRes)) {
      if (r.a && r.b && r.b >= r.a) {
        if (r.b >= since) waits.push(r.b - r.a);
        if (r.b >= now - 12 * 3600) waitS[bucket(r.b)] = Math.max(waitS[bucket(r.b)], r.b - r.a);
      }
    }
    const openWaits: number[] = asRows<{ a: number | null }>(openRes).filter((r) => r.a).map((r) => Math.max(0, now - (r.a ?? now)));
    const activity: ActivityItem[] = [];
    for (const r of asRows<{ at: number; status: string; topic_key: string; number: number | null; title: string; body: string }>(evRes)) {
      activity.push({ at: r.at, kind: r.status === 'failed' ? 'failed' : 'done', topic_key: r.topic_key, number: r.number, title: r.title, text: oneLine(r.body ?? '', 60) });
    }
    for (const r of asRows<{ a: number | null; b: number | null; topic_key: string; number: number | null; title: string }>(qevRes)) {
      if (r.a) activity.push({ at: r.a, kind: 'asked', topic_key: r.topic_key, number: r.number, title: r.title, text: '问了你一个问题' });
      if (r.b) activity.push({ at: r.b, kind: 'answered', topic_key: r.topic_key, number: r.number, title: r.title, text: '你回答了' });
    }
    activity.sort((a: ActivityItem, b: ActivityItem) => b.at - a.at);
    const today: number = Math.floor((now + 8 * 3600) / 86400);
    const week: number[] = new Array<number>(7).fill(0);
    let weekTurns = 0;
    let weekIn = 0;
    let weekCache = 0;
    for (const r of asRows<{ d: number; tok: number; n: number; inp: number; cr: number }>(weekRes)) {
      const i: number = 6 - (today - num(r.d));
      if (i < 0 || i > 6) continue;
      week[i] += num(r.tok);
      weekTurns += num(r.n);
      weekIn += num(r.inp);
      weekCache += num(r.cr);
    }
    // v15：Covey 推了本周用量（和飞书「用量」卡同一个算法）就用它；没有再用上面按轮次算的
    const usage: Record<string, unknown> | null = await this.usageSnapshot();
    const weekFrom = usage ? weekFromUsage(usage) : null;
    return {
      week_series: weekFrom?.series ?? week,
      week_tokens: weekFrom?.tokens ?? week.reduce((x: number, y: number) => x + y, 0),
      week_turns: weekFrom?.tasks ?? weekTurns,
      week_cache_pct: weekFrom ? weekFrom.cachePct : weekIn > 0 ? Math.round((weekCache / weekIn) * 100) : null,
      ...(weekFrom ? { week_topics: weekFrom.topics, week_harnesses: weekFrom.harnesses } : {}),
      today_tokens: tokens,
      today_work_seconds: work,
      avg_wait_seconds: waits.length ? waits.reduce((x: number, y: number) => x + y, 0) / waits.length : null,
      today_done: done,
      open_wait_count: openWaits.length,
      longest_open_wait_seconds: openWaits.length ? Math.max(...openWaits) : null,
      token_series: tokS,
      work_series: workS,
      wait_series: waitS,
      activity: activity.slice(0, 14),
    };
  }

  /** 专项报告：这个专项所有话题（含负责人）每一轮的起止、用时、token、预计用时，主人的话（role owner，画成菱形），加问题的提出和回答时间 */
  async report(key: string): Promise<ReportResponse> {
    const [turnRes, qRes] = await Promise.all([
      this.db.execute(sql`select t.key as topic_key, t.number, t.title, coalesce(nullif(t.data->>'kind', ''), 'topic') as kind,
          coalesce(t.data->>'harness', '') as harness, u.seq, u.role, u.status,
          ${NUMF('u.data', 'started_at')} as started_at, ${NUMF('u.data', 'ended_at')} as ended_at,
          coalesce(${NUMF('u.data', 'seconds')}, 0) as seconds, u.data->'usage' as usage,
          case when jsonb_typeof(u.data->'artifacts') = 'array' then jsonb_array_length(u.data->'artifacts') else 0 end as artifacts,
          case when jsonb_typeof(u.data->'artifacts') = 'array' then u.data->'artifacts'->0->>'name' end as artifact,
          ${NUMF('u.data', 'estimate_seconds')} as estimate_seconds,
          case when u.role = 'agent' and u.data->>'estimate_seconds' is not null then encode(substring(convert_to(u.body, 'UTF8') from 1 for 480), 'hex') end as head_hex
        from turns u join topics t on t.key = u.topic_key
        where t.project_key = ${key} and u.role <> 'event' order by u.seq asc limit 1200`),
      this.db.execute(sql`select t.key as topic_key, t.number, ${NUMF('q.data', 'asked_at')} as asked_at,
          ${NUMF('q.data', 'answered_at')} as answered_at, q.state, ${NUMF('q.data', 'closed_at')} as closed_at,
          coalesce(q.data->>'closed_by', '') as closed_by
        from questions q join topics t on t.key = q.topic_key where t.project_key = ${key}
        order by ${NUMF('q.data', 'asked_at')} desc nulls last limit 500`),
    ]);
    return {
      project_key: key,
      turns: asRows<ReportResponse['turns'][number] & { usage: unknown; head_hex?: string | null }>(turnRes).map(({ head_hex: headHex, ...r }) => ({
        ...r,
        number: r.number,
        seconds: num(r.seconds),
        usage: r.usage && typeof r.usage === 'object' ? obj(r.usage) : null,
        artifact: r.artifact ?? null,
        estimate_seconds: r.estimate_seconds === null || r.estimate_seconds === undefined ? null : num(r.estimate_seconds),
        // 按字节截的开头（在 SQL 里按字截正文，现网库会报编码错）：解出来，去掉截断的半个字
        head: typeof headHex === 'string' && headHex ? Buffer.from(headHex, 'hex').toString('utf8').replace(/\uFFFD+$/, '') || null : null,
      })),
      questions: asRows<ReportResponse['questions'][number]>(qRes).map((q) => ({
        ...q,
        asked_at: q.asked_at === null || q.asked_at === undefined ? null : num(q.asked_at),
        answered_at: q.answered_at === null || q.answered_at === undefined ? null : num(q.answered_at),
        closed_at: q.closed_at === null || q.closed_at === undefined ? null : num(q.closed_at),
        state: q.state || 'open',
        closed_by: q.closed_by || '',
      })),
    };
  }

  /** 在话题标题和轮次正文里不分大小写找（用 position 而不是 ILIKE，免得转义 % _） */
  async search(q: string, limit: number): Promise<SearchHit[]> {
    const needle: string = q.trim().toLowerCase();
    if (!needle) return [];
    const n: number = Math.min(Math.max(Math.floor(limit) || 30, 1), 100);
    const [bodyRes, titleRes] = await Promise.all([
      this.db.execute(sql`select t.key as topic_key, t.number, t.title, coalesce(nullif(t.data->>'kind', ''), 'topic') as kind,
          t.project_key, u.seq, u.role, u.data->>'by' as by,
          substring(${SHOWN} from greatest(position(${needle} in lower(${SHOWN})) - 40, 1) for 100) as snippet,
          coalesce(case when jsonb_typeof(u.data->'ended_at') = 'number' then (u.data->>'ended_at')::float8 end,
            case when jsonb_typeof(u.data->'started_at') = 'number' then (u.data->>'started_at')::float8 end,
            extract(epoch from u._updated_at)) as at
        from turns u join topics t on t.key = u.topic_key
        where position(${needle} in lower(${SHOWN})) > 0
        order by at desc limit ${n}`),
      this.db.execute(sql`select t.key as topic_key, t.number, t.title, coalesce(nullif(t.data->>'kind', ''), 'topic') as kind,
          t.project_key, null::int as seq, null::text as role, null::text as by, t.title as snippet, t.updated_at as at
        from topics t where position(${needle} in lower(t.title)) > 0
        order by t.updated_at desc limit ${n}`),
    ]);
    // 搜的是 T 号（T514）：那个话题本身排第一；其余按标题命中的话题在前、消息在后，各自按时间新到旧
    const tNum: RegExpMatchArray | null = /^t\s*(\d{1,6})$/u.exec(needle);
    const numberRows: SearchRow[] = tNum
      ? asRows<SearchRow>(
          await this.db.execute(sql`select t.key as topic_key, t.number, t.title, coalesce(nullif(t.data->>'kind', ''), 'topic') as kind,
              t.project_key, null::int as seq, null::text as role, t.title as snippet, t.updated_at as at
            from topics t where t.number = ${Number(tNum[1])} and coalesce(nullif(t.data->>'kind', ''), 'topic') = 'topic'
            order by t.updated_at desc limit 1`),
        )
      : [];
    const byAt = (a: SearchRow, b: SearchRow): number => num(b.at) - num(a.at);
    const seen = new Set<string>(numberRows.map((r: SearchRow) => r.topic_key));
    const titles: SearchRow[] = asRows<SearchRow>(titleRes).filter((r: SearchRow) => !seen.has(r.topic_key)).sort(byAt);
    const rows: SearchRow[] = [...numberRows, ...titles, ...asRows<SearchRow>(bodyRes).sort(byAt)];
    return rows.slice(0, n).map(
      (r: SearchRow): SearchHit => ({
        topic_key: r.topic_key,
        number: r.number,
        title: r.title,
        kind: r.kind === 'butler' ? 'butler' : r.kind === 'lead' ? 'lead' : 'topic',
        project_key: r.project_key || null,
        seq: r.seq,
        role: r.role,
        by: r.by ?? null,
        snippet: r.snippet.replace(/\s+/gu, ' ').trim(),
        at: num(r.at),
      }),
    );
  }

  async addOutbox(req: OutboxRequest): Promise<OutboxResponse> {
    const opId: string = str(req?.op_id);
    if (!opId || opId.length > 64) throw new BadRequestException('op_id 不合法');
    if (!isOutboxKind(str(req?.kind))) throw new BadRequestException('kind 不合法');
    const payload: Record<string, unknown> = obj(req.payload);
    this.checkPayload(req.kind, payload, req);
    // 敏感词脱敏已下线：旧页面还带着的 redaction 直接丢掉，不报错也不往下传
    if (req.kind === 'launch' || req.kind === 'switch') delete payload.redaction;
    // v25：开新话题也能带附件，同 say
    if ((req.kind === 'say' || req.kind === 'launch') && payload.files !== undefined) {
      const files: SayFile[] = sayFiles(payload.files);
      payload.files = files;
      if (files.length) {
        const ids: string[] = files.map((f: SayFile) => f.id);
        const ok = asRows<{ n: number }>(await this.db.execute(sql`select count(*)::int as n from uploads
          where id in (${sql.join(ids.map((x: string) => sql`${x}`), sql`, `)}) and done and expires_at > now()`))[0];
        if (num(ok?.n) !== new Set(ids).size) throw new BadRequestException('有附件没传完或已过期，删掉重传');
      }
    }
    const res = await this.db.execute(
      sql`insert into outbox(op_id, kind, topic_key, project_key, payload)
        values (${opId}, ${req.kind}, ${str(req.topic_key) || null}, ${str(req.project_key) || null},
          ${JSON.stringify(payload)}::jsonb)
        on conflict (op_id) do nothing returning seq::int as seq, op_id, state`,
    );
    const row = asRows<OutboxResponse>(res)[0];
    if (!row) throw new ConflictException('这个操作已经发过了');
    // 同进程里挂着等取件的 Covey 请求（/sync/inbox?wait=）马上取走，不用等下一次查库
    wake('inbox');
    return row;
  }

  /**
   * 一条发件的结果（页面按 op_id 问）。waitMs > 0（v38，?wait=<秒>）：它还是 new 就挂着，每 200 毫秒查一次库
   * （同进程里 Covey 回执会提前叫醒），最多等 waitMs 毫秒，不是 new 了马上返回；没有这条发件仍是 404。不带 wait 和以前一样。
   */
  async outboxStatus(opId: string, waitMs = 0, aborted: () => boolean = () => false): Promise<OutboxStatus> {
    let last: OutboxStatus | null = null;
    const read = async (): Promise<OutboxStatus> => {
      const row: OutboxStatus | undefined = await this.acks.get(opId);
      if (!row) throw new NotFoundException('没有这条发件');
      last = row;
      return row;
    };
    return waitFor<OutboxStatus>(
      'ack',
      waitMs,
      OUTBOX_STEP_MS,
      async () => {
        const row: OutboxStatus = await read();
        return row.state === 'new' ? null : row;
      },
      () => last as unknown as OutboxStatus,
      aborted,
    );
  }

  /** v15：本周用量快照（sync_state name='usage'）的 data；没有是 null */
  private usageSnapshot(): Promise<Record<string, unknown> | null> {
    return this.snapshotData('usage');
  }

  /** v26：用量页：Covey 推的按天用量；没收到过、或 Covey 还不推按天的是 null */
  async usage(): Promise<UsageResponse | null> {
    const u: Record<string, unknown> | null = await this.usageSnapshot();
    return u ? usageFromSnapshot(u) : null;
  }

  /** v7：设置快照；没收到过是 null */
  async settings(): Promise<SettingsResponse | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = 'settings'`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    if (!o.data) return null;
    return { rev: str(o.rev), data: o.data as SettingsResponse['data'], at: str(o.at) };
  }

  /** v37：主记忆（设置条目 key memory）；Covey 还没推过是 null */
  async memory(): Promise<MainMemoryResponse | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = ${'memory'}`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    if (!o.data) return null;
    return { rev: str(o.rev), data: o.data as MainMemoryData, at: str(o.at) };
  }

  /** v43：记忆中心的一键（memory:index、memory:global…，键已由调用方按 MEMORY_KEY_RE 核过）；Covey 没推过、或 data 不是对象是 null */
  async memoryKey(name: string): Promise<MemoryKeyResponse | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = ${name}`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    if (!o.data || typeof o.data !== 'object' || Array.isArray(o.data)) return null;
    return { rev: str(o.rev), data: o.data, at: str(o.at) };
  }

  /** v39：版本与更新（设置条目 key upgrade）；Covey 没推过、或形状不对是 null */
  async upgrade(): Promise<UpgradeResponse | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = ${'upgrade'}`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    const data: UpgradeData | undefined = upgradeOf(o.data);
    return data ? { rev: str(o.rev), data, at: str(o.at) } : null;
  }

  /** v42：账号与额度（设置条目 key quota）；Covey 没推过、或形状不对是 null */
  async quota(): Promise<QuotaResponse | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = ${'quota'}`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    const data: QuotaData | undefined = quotaOf(o.data);
    return data ? { rev: str(o.rev), data, at: str(o.at) } : null;
  }

  /** v16：整理专项的建议；没收到过是 null */
  async tidy(): Promise<TidyResponse> {
    const d: Record<string, unknown> | null = await this.snapshotData('tidy');
    return d && Array.isArray(d.proposals) ? (d as unknown as TidyResponse) : null;
  }

  private async snapshotData(name: string): Promise<Record<string, unknown> | null> {
    const res = await this.db.execute(sql`select value from sync_state where name = ${name}`);
    const v = asRows<{ value: unknown }>(res)[0]?.value;
    const o: Record<string, unknown> = obj(typeof v === 'string' ? JSON.parse(v) : v);
    const d: Record<string, unknown> = obj(o.data);
    return Object.keys(d).length ? d : null;
  }

  private checkSettings(payload: Record<string, unknown>): void {
    const what: string = str(payload.what);
    // 敏感词脱敏已下线（T947）：下线前画的旧页面还会发这两种
    if (what === 'redaction' || what === 'terms') throw new BadRequestException('敏感词脱敏已下线，这次没有改任何设置');
    if (!(SETTINGS_WHATS as readonly string[]).includes(what)) throw new BadRequestException('settings 的 what 不合法');
    if (what === 'row') {
      if (!str(payload.kind)) throw new BadRequestException('row 要 kind');
      if (typeof payload.option !== 'string') throw new BadRequestException('row 要 option（可为空串）');
      if (payload.confirm !== undefined && typeof payload.confirm !== 'boolean') throw new BadRequestException('row 的 confirm 要是布尔');
    } else if (what === 'untrust') {
      if (!str(payload.key)) throw new BadRequestException('untrust 要 key');
    } else if (what === 'note') {
      if (!str(payload.workspace)) throw new BadRequestException('note 要 workspace');
      if (typeof payload.note !== 'string') throw new BadRequestException('note 要 note');
      if (Array.from(payload.note).length > 100) throw new BadRequestException('用途最多 100 字');
    } else if (what === 'group') {
      if (!str(payload.group)) throw new BadRequestException('group 要 group');
      if (typeof payload.on !== 'boolean') throw new BadRequestException('group 要 on（布尔）');
    } else if (what === 'free') {
      if (!str(payload.group)) throw new BadRequestException('free 要 group');
      if (payload.mode !== '' && payload.mode !== 'owner') throw new BadRequestException('free 的 mode 只能是空串或 owner');
    } else if (what === 'workspace_add') {
      const path: string = str(payload.path).trim();
      if (!path) throw new BadRequestException('workspace_add 要 path');
      if (path.length > 1000) throw new BadRequestException('path 最多 1000 字');
    } else if (what === 'workspace_remove') {
      if (!str(payload.workspace) || str(payload.workspace).length > 200) throw new BadRequestException('workspace_remove 要 workspace');
    } else if (what === 'workspace_prune' || what === 'refresh') {
      return;
    } else if (what === 'repos_set' || what === 'repos_add' || what === 'repos_remove' || what === 'worktrees_prune') {
      // v43：仓库页的操作（形状校验放在 workbench.memory-edit.ts，纯函数）
      const err: string | null = reposSettingsError(what, payload);
      if (err) throw new BadRequestException(err);
    } else if (what === 'device_add' || what === 'device_set' || what === 'device_remove' || what === 'device_check') {
      // v46：设备名单的操作（形状校验放在 workbench.memory-edit.ts，纯函数）
      const err: string | null = devicesSettingsError(what, payload);
      if (err) throw new BadRequestException(err);
    } else if (what === 'node_add') {
      if (!/^[a-z][a-z0-9-]{0,23}$/u.test(str(payload.name))) throw new BadRequestException('名字要小写字母开头，只用小写字母、数字和 -，最多 24 个');
      if (payload.title !== undefined && (typeof payload.title !== 'string' || Array.from(payload.title).length > 40)) throw new BadRequestException('显示名最多 40 字');
      const ssh: string = str(payload.ssh).trim();
      if (!ssh) throw new BadRequestException('node_add 要 ssh');
      if (ssh.length > 200) throw new BadRequestException('ssh 最多 200 字');
    } else if (what === 'node_remove') {
      if (!str(payload.alias) || str(payload.alias).length > 24) throw new BadRequestException('node_remove 要 alias');
    } else if (what === 'proxy') {
      const op: string = str(payload.op);
      if (!(PROXY_OPS as readonly string[]).includes(op)) throw new BadRequestException('proxy 的 op 不合法');
      if (op === 'row') {
        if (!str(payload.kind) || str(payload.kind).length > 100) throw new BadRequestException('proxy row 要 kind');
        if (typeof payload.option !== 'string' || payload.option.length > 200) throw new BadRequestException('proxy row 要 option（可为空串）');
      } else if (op === 'pause' || op === 'resume' || op === 'remove') {
        if (!str(payload.contact) || str(payload.contact).length > 200) throw new BadRequestException(`proxy ${op} 要 contact`);
      }
    }
  }

  private checkPayload(kind: string, payload: Record<string, unknown>, req: OutboxRequest): void {
    if (kind === 'settings') {
      this.checkSettings(payload);
      return;
    }
    if (kind === 'memory_edit') {
      checkMemoryEdit(payload, req);
      return;
    }
    if (kind === 'upgrade') {
      // v39：检查更新、全部更新；能不能升级由 Covey 定（没有更新、正在升级、检出有改动都回 failed）
      const shape = upgradeShape(payload, str(req.topic_key), str(req.project_key));
      if ('error' in shape) throw new BadRequestException(shape.error);
      return;
    }
    if (kind === 'quota_refresh') {
      // v42：账号与额度的「刷新」；冷却、只有主人能点由 Covey 定
      const shape = quotaRefreshShape(payload, str(req.topic_key), str(req.project_key));
      if ('error' in shape) throw new BadRequestException(shape.error);
      return;
    }
    if (kind === 'tidy_decide') {
      // v16：对整理专项的一条建议点「照办」或「不用」；不带 topic_key，payload 只有 proposal 和 choice
      if (str(req.topic_key) || str(req.project_key)) throw new BadRequestException('tidy_decide 不带 topic_key');
      if (Object.keys(payload).some((k: string) => k !== 'proposal' && k !== 'choice')) throw new BadRequestException('tidy_decide 只有 proposal 和 choice');
      if (!/^[A-Za-z0-9-]{1,64}$/u.test(str(payload.proposal))) throw new BadRequestException('tidy_decide 的 proposal 不合法');
      if (payload.choice !== 'ok' && payload.choice !== 'decline') throw new BadRequestException('tidy_decide 的 choice 只能是 ok 或 decline');
      return;
    }
    if (kind === 'launch') {
      if (str(req.topic_key) || str(req.project_key)) throw new BadRequestException('launch 不带 topic_key、project_key');
      const text: string = str(payload.text);
      if (!text.trim()) throw new BadRequestException('话不能为空');
      if (Array.from(text).length > 20000) throw new BadRequestException('话太长（最多 20000 字）');
      for (const k of ['model', 'effort'] as const) {
        const v: unknown = payload[k];
        if (typeof v !== 'string' || !v || Array.from(v).length > 200) throw new BadRequestException(`launch 的 ${k} 要是 1–200 字的文字`);
      }
      // v43：workspace、run_mode 可选（新 Covey 用 repos）；带了照旧校验
      if (payload.workspace !== undefined) {
        const v: unknown = payload.workspace;
        if (typeof v !== 'string' || !v || Array.from(v).length > 200) throw new BadRequestException('launch 的 workspace 要是 1–200 字的文字');
      }
      if (payload.run_mode !== undefined && !(LAUNCH_RUN_MODES as readonly string[]).includes(str(payload.run_mode))) throw new BadRequestException('launch 的 run_mode 要是 direct 或 worktree');
      const repoErr: string | null = launchReposError(payload.repos);
      if (repoErr) throw new BadRequestException(repoErr);
      // v43：执行设备（可选，只在多设备时网页才带）
      if (payload.device !== undefined && (typeof payload.device !== 'string' || Array.from(payload.device).length > 64)) throw new BadRequestException('device 要是最多 64 字的文字');
      if (typeof payload.work_mode !== 'string' || Array.from(payload.work_mode).length > 200) throw new BadRequestException('launch 的 work_mode 要是文字（可为空）');
      return;
    }
    if (kind === 'switch') {
      // v20：话题页换配置，同飞书 🔁 换配置的“确认更换”；校验交给 Covey，这里只挡形状不对的
      if (!str(req.topic_key) || str(req.project_key)) throw new BadRequestException('switch 要 topic_key、不带 project_key');
      {
        const v: unknown = payload.model;
        if (typeof v !== 'string' || !v || Array.from(v).length > 200) throw new BadRequestException('switch 的 model 要是 1–200 字的文字');
      }
      // v43：workspace、run_mode 可选；带了照旧校验
      if (payload.workspace !== undefined) {
        const v: unknown = payload.workspace;
        if (typeof v !== 'string' || !v || Array.from(v).length > 200) throw new BadRequestException('switch 的 workspace 要是 1–200 字的文字');
      }
      const repoErr: string | null = launchReposError(payload.repos);
      if (repoErr) throw new BadRequestException(repoErr);
      // v43：执行设备（可选，只在多设备时网页才带）
      if (payload.device !== undefined && (typeof payload.device !== 'string' || Array.from(payload.device).length > 64)) throw new BadRequestException('device 要是最多 64 字的文字');
      for (const k of ['effort', 'work_mode'] as const) {
        const v: unknown = payload[k];
        if (typeof v !== 'string' || Array.from(v).length > 200) throw new BadRequestException(`switch 的 ${k} 要是文字（可为空）`);
      }
      if (payload.run_mode !== undefined && !(LAUNCH_RUN_MODES as readonly string[]).includes(str(payload.run_mode))) throw new BadRequestException('switch 的 run_mode 要是 direct 或 worktree');
      return;
    }
    if (kind === 'dismiss') {
      if (!str(req.topic_key)) throw new BadRequestException('dismiss 要 topic_key');
      if (Object.keys(payload).length) throw new BadRequestException('dismiss 的 payload 要是空的');
      return;
    }
    if (kind === 'tidy') {
      // 整理专项：scope 为空就是整理全部，也可以是专项 id 或一个词
      if (payload.scope !== undefined && typeof payload.scope !== 'string') throw new BadRequestException('tidy 的 scope 要是文字');
      if (str(payload.scope).length > 200) throw new BadRequestException('scope 太长');
      return;
    }
    if (kind === 'forget') {
      if (!str(req.project_key) || !str(payload.entry_id)) throw new BadRequestException('forget 要 project_key 和 entry_id');
      return;
    }
    if (kind === 'memory_fix') {
      // v31：三层记忆的纠正。专项记忆带 project_key、话题记忆带 topic_key（只带一个），payload 里也写一份
      const project: string = str(req.project_key);
      const topic: string = str(req.topic_key);
      if (!project === !topic) throw new BadRequestException('memory_fix 要 project_key 或 topic_key（只带一个）');
      if (!isId(payload.entry_id)) throw new BadRequestException('memory_fix 要 entry_id');
      if (!(MEMORY_FIXES as readonly string[]).includes(str(payload.what))) throw new BadRequestException('memory_fix 的 what 只能是 scope、once 或 confirm');
      if (payload.what === 'scope') {
        const applies: string = str(payload.applies).trim();
        if (!applies) throw new BadRequestException('改条件要写上适用的范围或条件');
        if (Array.from(applies).length > 500) throw new BadRequestException('条件最多 500 字');
        payload.applies = applies;
      } else if (payload.applies !== undefined) {
        throw new BadRequestException('只有改条件（scope）带 applies');
      }
      if (project) payload.project_key = project;
      else payload.topic_key = topic;
      return;
    }
    if (kind === 'reopen') {
      if (!str(req.project_key)) throw new BadRequestException('reopen 要 project_key');
      return;
    }
    if (kind === 'project_finish' || kind === 'project_stop') {
      // v34：专项的收尾、停止，同飞书专项卡、停住卡上的按钮；能不能做由 Covey 按专项记录和守卫定
      const bad: string | null = projectOpShape(kind, payload, str(req.topic_key), str(req.project_key));
      if (bad) throw new BadRequestException(bad);
      payload.project_key = str(req.project_key);
      return;
    }
    if (kind === 'memory_undo') {
      // v34：撤销刚记下的一条记忆，同答案卡上的「不对」
      const shape = memoryUndoShape(payload, str(req.topic_key), str(req.project_key));
      if ('error' in shape) throw new BadRequestException(shape.error);
      payload.job_id = shape.job_id;
      payload.entry_id = shape.entry_id;
      return;
    }
    if (kind === 'revisit_cancel') {
      // v35：取消话题的定时回看；set_at 是约下它的时刻，Covey 只取消同一条
      const shape = revisitCancelShape(payload, str(req.topic_key), str(req.project_key));
      if ('error' in shape) throw new BadRequestException(shape.error);
      payload.set_at = shape.set_at;
      return;
    }
    if (kind === 'recover') {
      // v33：没成功的一轮上的按钮（检查并继续、现在重试、看原因、拆小再试、停止自动重试），同任务卡上点同名按钮；能不能做由 Covey 按记录定
      const shape = recoverShape(payload, str(req.topic_key), str(req.project_key));
      if ('error' in shape) throw new BadRequestException(shape.error);
      payload.job_id = shape.job_id;
      return;
    }
    if (!str(req.topic_key)) throw new BadRequestException(`${kind} 要 topic_key`);
    if (kind === 'seen') return;
    if (kind === 'stop') {
      // v31：带 collab 时只停这次协作排队和在跑的咨询轮
      if (payload.collab !== undefined && !isId(payload.collab)) throw new BadRequestException('stop 的 collab 不合法');
      return;
    }
    if (kind === 'say') {
      const files: SayFile[] = sayFiles(payload.files);
      if (!str(payload.text).trim() && !files.length) throw new BadRequestException('话不能为空');
      sayRouting(payload, str(req.topic_key));
      return;
    }
    if (kind !== 'answer') return;
    if (!str(payload.token)) throw new BadRequestException('answer 要 token');
    // 不处理和它的撤销不带答案（Covey v20；以前这里要 answers，网页上点不处理会被拒）
    if (payload.aside === true || payload.restore === true) return;
    const note: string = str(payload.note).trim();
    // skip：只交补充说明；ask 是 v29 前的网页发的“先问一句”，Covey 按 skip 处理
    if (payload.skip === true || payload.ask === true) {
      if (!note) throw new BadRequestException('补充说明是空的：写上你想说的再提交');
      return;
    }
    const answers: unknown = payload.answers;
    if (answers !== undefined && (answers === null || typeof answers !== 'object' || Array.isArray(answers))) {
      throw new BadRequestException('answers 要是“题目 id → 回答”的对象');
    }
    // 提交回答：带了补充说明时没答的题算不回答，answers 可以为空（Covey v29）
    if (!note && (!answers || !Object.keys(answers as Record<string, unknown>).length)) {
      throw new BadRequestException('先答题，或写上补充说明再提交');
    }
  }
}

export { checkMemoryEdit } from './workbench.memory-edit';

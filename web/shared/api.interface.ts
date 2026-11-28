/* 前后端共享的类型。字段名照 CONTRACT.md（snake_case 保持不变）。 */

export const PROJECT_STATUSES = ['在做', '等你', '停住', '收起', '做完', '已取消'] as const;
export type ProjectStatus = (typeof PROJECT_STATUSES)[number];

export const TOPIC_STATES = ['等你', '在做', '做完', '已停止', '失败', '空闲'] as const;
export type TopicState = (typeof TOPIC_STATES)[number];

/** memory_fix（v31）：三层记忆的纠正（改条件、只限当次、确认长期），见 MemoryFixPayload；upgrade（v39）：检查更新、全部更新，见 UpgradePayload；quota_refresh（v42）：账号与额度的「刷新」，不带 topic_key、project_key，payload 空；v43：记忆中心与仓库（`memory:*` 键、MemoryEditPayloadV43、repos_* 设置操作、启动与换配置的 repos），全是可选字段，旧 Covey 不推新键时与 v42 行为相同 */
export const OUTBOX_KINDS = ['say', 'answer', 'stop', 'reopen', 'seen', 'forget', 'tidy', 'tidy_decide', 'settings', 'dismiss', 'launch', 'switch', 'memory_fix', 'recover', 'project_finish', 'project_stop', 'memory_undo', 'revisit_cancel', 'memory_edit', 'upgrade', 'quota_refresh'] as const;
export type OutboxKind = (typeof OUTBOX_KINDS)[number];
export const isOutboxKind = (v: string): v is OutboxKind =>
  (OUTBOX_KINDS as readonly string[]).includes(v);

export const OUTBOX_STATES = ['new', 'done', 'failed'] as const;
export type OutboxState = (typeof OUTBOX_STATES)[number];

/** collab（v31）：一次协作（多参与者的往返），见 CollabView。
 * login（自托管，T653）：主人在飞书对一个网页登录请求点了允许或拒绝，key 是请求 id，data 是 `{decision: 'approved' | 'denied', decided_at}`；妙搭模式没有这张表，收到就跳过 */
export const SYNC_TYPES = ['project', 'topic', 'turn', 'question', 'settings', 'collab', 'login'] as const;
export type SyncType = (typeof SYNC_TYPES)[number];
export const isSyncType = (v: string): v is SyncType =>
  (SYNC_TYPES as readonly string[]).includes(v);

/** 三层记忆（v31，docs/web.md 协作与三层记忆）：rule 长期约定、decision 当前决定、history 历史记录 */
export const MEMORY_LAYERS = ['rule', 'decision', 'history'] as const;
export type MemoryLayer = (typeof MEMORY_LAYERS)[number];
/** 一条记忆现在还算不算数：active 有效、done 做完、superseded 被替代（带 superseded_by）、void 作废 */
export const MEMORY_VALID = ['active', 'done', 'superseded', 'void'] as const;
export type MemoryValid = (typeof MEMORY_VALID)[number];
/** 来源方（Covey core/turn_source.py 的 PARTIES）；level 只由 Bridge 按真实入口写：owner 是主人原话且出处是 Bridge 收进来的，其余 agent */
export type MemoryParty = 'owner' | 'butler' | 'lead' | 'topic' | 'bridge';
/** 一条记忆的出处：旧数据只有 at（记下的时刻）；v31（Covey app/web_collect.py memory_row）知道的另带 message_id（那条消息）
 * 或 question（那道题的记录）和它的 item、index，topic 和它的 topic_number，job，主人的原话 quote */
export interface MemoryOrigin {
  topic_number?: number;
  at?: number | string;
  message_id?: string;
  question?: string;
  item?: string;
  index?: number;
  topic?: string;
  seq?: number;
  job?: string;
  quote?: string;
  chat?: string;
}
/** 适用范围和条件：Covey 推的是一句话（空串是没写）；也认分开写的几项（范围、什么时候、例外） */
export type MemoryApplies = string | { scope?: string; when?: string; except?: string; [k: string]: unknown };
/** 一轮带它的那一段（Covey core/memory.py 的 PARTS）：主人的话在它那一层（rule 长期约定、decision 当前决定），别人记的是 reference 待核对参考；历史记录和风格行没有 */
export type MemoryPart = 'rule' | 'decision' | 'reference';
export interface MemoryItem {
  id?: string;
  said_by?: string;
  origin?: MemoryOrigin;
  text: string;
  kind?: string;
  source?: string;
  at?: number | string;
  /** v31，都可选：没有 layer 的是旧数据，面板照旧画。layer 风格行也是 rule（kind 是 style） */
  layer?: MemoryLayer | string;
  asserted_by?: MemoryParty | string;
  recorded_by?: MemoryParty | string;
  level?: 'owner' | 'agent' | string;
  applies?: MemoryApplies;
  valid?: MemoryValid | string;
  superseded_by?: string;
  /** owner 或 lead（said_by 跟着它） */
  by?: string;
  part?: MemoryPart | string | null;
  /** project:<专项>:<id>，和任务记录的 memory_ids 一样 */
  ref?: string;
  /** 专项的话题下一轮带不带它 */
  carried?: boolean;
}
export interface LibraryItem {
  topic_number?: number;
  kind?: string;
  size?: number;
  name: string;
  link?: string;
  at?: number | string;
}
/** 一轮的一个产物（Covey 数据约定 v32，和飞书答案卡的「产物」同一份）：只有文件名，没有本机路径 */
export interface ArtifactItem {
  name: string;
  /** 传到云空间后的链接（https） */
  link?: string;
  /** v32：传上去的字节数 */
  size?: number;
  /** v32：没传上的原因 */
  error?: string;
  /** v32：还在往云空间传 */
  uploading?: boolean;
}

/** v32：服务端收下一轮的产物时只留认得的字段：文件名（必有，200 字内）、云空间链接（只认 https）、大小、没传上的原因、在传；最多 50 个 */
export function artifactsOf(v: unknown): ArtifactItem[] {
  const out: ArtifactItem[] = [];
  for (const x of Array.isArray(v) ? v.slice(0, 50) : []) {
    const o: Record<string, unknown> = x && typeof x === 'object' && !Array.isArray(x) ? (x as Record<string, unknown>) : {};
    const name: string = typeof o.name === 'string' ? o.name.trim().slice(0, 200) : '';
    if (!name) continue;
    const item: ArtifactItem = { name };
    if (typeof o.link === 'string' && /^https:\/\/[^\s]+$/u.test(o.link) && o.link.length <= 2000) item.link = o.link;
    if (typeof o.size === 'number' && Number.isFinite(o.size) && o.size >= 0) item.size = Math.round(o.size);
    if (typeof o.error === 'string' && o.error.trim()) item.error = o.error.trim().slice(0, 300);
    if (o.uploading === true) item.uploading = true;
    out.push(item);
  }
  return out;
}
export interface QuestionOption {
  label: string;
  description?: string;
}
export interface QuestionItem {
  id: string;
  header?: string;
  question: string;
  options: QuestionOption[];
  multiple?: boolean;
  /** 题目下面那行“背景”（Covey v20） */
  context?: string;
}

/** 一个 open 的问题（key 即问题 token） */
export interface QuestionView {
  token: string;
  topic_key: string;
  turn_key: string | null;
  questions: QuestionItem[];
  state: string;
  answer: unknown;
  /** “不处理”能不能点（Covey v20，同飞书问题卡；v29 去掉了先问一句） */
  extras?: boolean;
  /** 点了“不处理”、话题下一轮还没带走：还能撤销（这时 state 不是 open） */
  restorable?: boolean;
  closed_at?: number | null;
  /** 什么时候问的（秒，Covey v9）：大厅按它把问题排进时间线 */
  asked_at?: number | null;
}

export interface TopicBrief {
  key: string;
  number: number | null;
  title: string;
  project_key: string | null;
  state: string;
  unseen: boolean;
  updated_at: number;
  harness: string;
  model: string;
  /** v7：Covey 给的可读模型名（Opus 5.5） */
  model_name?: string;
  /** v43：这个话题用的仓库；home_start 是从主目录起的话题 */
  repos?: TopicRepo[];
  home_start?: boolean;
  /** 最新一轮的一句摘要 */
  latest: string;
  latest_role: string;
  latest_status: string;
}

export interface WaitingItem {
  topic: TopicBrief;
  question: QuestionView | null;
}

export interface ProjectBrief {
  key: string;
  title: string;
  status: string;
  status_reason: string;
  goal: string;
  topic_count: number;
  waiting_count: number;
  running_count: number;
  updated_at: number;
}

export interface HomeResponse {
  waiting: WaitingItem[];
  running: TopicBrief[];
  unseen: TopicBrief[];
  projects: ProjectBrief[];
  /** 不属于任何专项、也没出现在上面三栏的话题（契约外追加） */
  loose: TopicBrief[];
  synced_at: string | null;
}

export interface OutboxView {
  seq: number;
  op_id: string;
  kind: string;
  topic_key: string | null;
  project_key: string | null;
  payload: Record<string, unknown>;
  state: string;
  message: string;
  created_at: string;
}

export interface ProjectDetailResponse {
  key: string;
  title: string;
  goal: string;
  status: string;
  status_reason: string;
  lead_topic_key: string | null;
  memory: MemoryItem[];
  library: LibraryItem[];
  updated_at: number;
  topics: TopicBrief[];
  outbox: OutboxView[];
  synced_at: string | null;
}

export interface StepView {
  id?: string;
  kind: string;
  label: string;
  /** 点开一行时显示：模型说了目的的那一步的命令，或被截断的完整名字（Covey contract v18，旧版没有） */
  detail?: string;
  started_at: number | null;
  ended_at: number | null;
  status: string;
}
export interface PlanItem {
  title: string;
  state: string;
}
export interface TokenUsage {
  input?: number;
  output?: number;
  cache_read?: number;
  total?: number;
}
export interface TurnNote {
  /** remembered / voided / undone / skipped_ask / reference；v45 多 superseded（收起了）、pending（待审核） */
  kind: string;
  text: string;
  scope?: string;
  entry_id?: string;
}
export interface StartedTopic {
  key: string;
  number: number;
  title: string;
  harness?: string;
}
/** v23：管家交给了谁。kind=topic 是话题（how：start 开的、send 转的、fork 分叉的），kind=lead 是专项的负责人（title 是专项名） */
export interface HandedTarget {
  key: string;
  kind: 'topic' | 'lead';
  number: number;
  title: string;
  project_key: string;
  how: string;
}
export interface ContextUse {
  used: number;
  limit: number;
}

/* ---- v4：话题要点、进度、问题卡的回答、没做成的一轮（都可选，拿不到就不画） ---- */
export interface TopicGist {
  essence?: string;
  goal?: string;
  involves?: string;
  related?: number[];
  changed?: string;
  updated_at?: number;
}
export interface TopicProgress {
  /** 检查点的阶段那句话 */
  phase?: string;
  plan_done?: number;
  plan_total?: number;
  last_activity_at?: number | null;
  /** 最后一步的人话，如“在跑测试” */
  last_activity?: string;
  /** 估不出来时为 null */
  eta_seconds?: number | null;
  /** 超过 Covey 设的分钟数（COVEY_PROGRESS_STUCK_MINUTES）没有动静；最新一步是还在跑的命令时不算 */
  maybe_stuck?: boolean;
  /** 当前这步比此前平均每步慢时才有（docs/web.md 进度）：第几步、几时开始、已做几秒、此前平均每步几秒；否则为 null */
  slow_step?: TopicSlowStep | null;
  /** 最新一步是还在跑的命令时才有：几时开始、已跑几秒（到分钟）、是否已跑满卡住的分钟数 */
  command?: TopicCommand;
  /** v41：检查点里的下一步，≤120 字（专项「简报」里 session 行的“下一步”） */
  next_step?: string;
  /** v41：检查点 requires_user 时的 question，≤160 字：话题在等主人答什么 */
  waiting?: string;
}
export interface TopicSlowStep {
  number: number;
  since: number | null;
  seconds: number;
  usual: number | null;
}
export interface TopicCommand {
  since: number | null;
  seconds: number;
  long: boolean;
}
export interface TurnAnswer {
  question: string;
  choice: string;
}
export const FAILURE_ACTIONS = ['continue', 'retry', 'diagnose', 'split', 'switch_model'] as const;
export type FailureAction = (typeof FAILURE_ACTIONS)[number];
/** v33：网页发 recover 操作能做的（和任务卡上同名按钮是同一个调用）；stop_auto_retry 只在排着自动重试时有 */
export const RECOVER_ACTIONS = ['continue', 'retry', 'diagnose', 'split', 'stop_auto_retry'] as const;
export type RecoverAction = (typeof RECOVER_ACTIONS)[number];
/** 一颗按钮在网页上怎么做（v33 failure.how）：recover 发 recover 操作、switch 打开同页的换配置、feishu 只有飞书能做 */
export const FAILURE_HOWS = ['recover', 'switch', 'feishu'] as const;
export type FailureHow = (typeof FAILURE_HOWS)[number];
/** 按钮的 key：没成功的一轮能做的，加上排着自动重试时的“停止自动重试” */
export type FailureButton = FailureAction | 'stop_auto_retry';
/** v33：排好的自动重试（只在排着时有）：几点重试（秒）、第几次、一共几次（限流那条有进展就重新数，为 null）、为什么、现在就跑它的是哪个 action */
export interface TurnAutoRetry {
  state: 'pending';
  at: number | null;
  attempt: number | null;
  total: number | null;
  kind: string;
  now: 'retry' | 'continue';
}
export interface TurnFailure {
  title: string;
  detail?: string;
  actions: FailureAction[];
  /** v33：按钮上的字（和任务卡一样：continue 是“检查并继续”，结果没发出去时是“重新投递结果”；retry 是“现在重试”） */
  labels?: Partial<Record<FailureButton, string>>;
  /** v33：每颗按钮怎么做 */
  how?: Partial<Record<FailureButton, FailureHow>>;
  /** v33：排着的自动重试；有它时只画“现在重试”“停止自动重试”和 actions 里别的，不另画 continue/retry */
  auto_retry?: TurnAutoRetry;
}
/** v33：recover 操作的 payload（topic_key 是那一轮所在的话题、管家或负责人） */
export interface RecoverPayload {
  job_id: string;
  action: RecoverAction;
}

/* ---- v31：多参与者协作（docs/web.md 协作与三层记忆；规则在 Covey core/collab.py，网页一侧的移植在 shared/collab.ts） ---- */
/** 一轮属于哪次协作：汇总方交结论的那一轮是 integrator，协助方答咨询的那一轮（和它的回执行）是 helper */
export interface TurnCollab {
  key: string;
  role: 'integrator' | 'helper';
}
/** 一条证据：一句话，或带出处（话题的哪一轮、链接） */
export interface CollabEvidence {
  text: string;
  link?: string;
  topic_key?: string;
  seq?: number;
}
/** 协助方的答复：正文、证据、出处话题的哪一轮、核查时刻（秒） */
export interface CollabReply {
  text: string;
  evidence: CollabEvidence[];
  topic_key?: string;
  seq?: number;
  at?: number;
}
/** 一次咨询（number 1）或澄清（number 2）：谁问谁、状态（core/collab.py 的 ASK_TRANSITIONS）、问的话、答复 */
export interface CollabAsk {
  id: string;
  from: string;
  to: string;
  number: number;
  state: string;
  text: string;
  reply: CollabReply | null;
  /** 未接手、失败、到期、已取消时的原因（Covey collab_records.view 的 reason）；没有是空串 */
  reason: string;
  /** Covey 给的状态词（ASK_WORDS 的词），没有时网页自己按 state 取 */
  words?: string;
  /** 已结束（取消、到期）后才到的答复：只留档，不唤醒 */
  late?: { text: string; at?: number } | null;
}
/** 根请求：主人发起这次协作的那句话（消息 id；网页发的是 web-<op_id>），可另带任务键 */
export interface CollabRoot {
  message_id?: string;
  job?: string;
}
/** 协作条目（type=collab）：一次协作只有一个汇总方，其余是协助方；state 是 open、done、cancelled */
export interface CollabView {
  key: string;
  root: CollabRoot;
  integrator: string;
  helpers: string[];
  state: string;
  goal_rev?: string;
  created_at: number;
  updated_at: number;
  asks: CollabAsk[];
}

/** v7：主人那一轮是谁说的（你、专项负责人、管家） */
export type TurnBy = 'owner' | 'lead' | 'butler';
/** v19：一轮带的一张图。id 是内容哈希，图从 /api/images/:id 取（要登录、只有主人）；sent 主人发的，shown 任务推给主人看的（show_images，带 note）；gone 是网页上已过期 */
export interface TurnImage {
  id: string;
  w?: number;
  h?: number;
  kind?: 'sent' | 'shown';
  note?: string;
  gone?: boolean;
}

/** v27：这一轮用的什么配置（启动卡上的叫法），网页写在回答下面 */
export interface TurnRanWith {
  harness?: string;
  provider?: string;
  model?: string;
  effort?: string;
  workspace?: string;
}

export interface TurnView {
  /** v31 新增，可选：这一轮属于哪次协作、是汇总方还是协助方 */
  collab?: TurnCollab;
  /** v31 新增，可选：这一轮主人那句话的消息 id（记忆的“查看原文”按它找） */
  message_id?: string;
  /** v27 新增，可选：这一轮用的 Harness、供应商、模型、思考、工作区 */
  ran_with?: TurnRanWith;
  /** v19 新增，可选：这一轮的图 */
  images?: TurnImage[];
  /** v19：主人对上一条消息的编辑，改后的全文已经放在原消息那一行（display、edited），这一行不再单画 */
  merged?: boolean;
  /** v7 新增，可选：谁说的；via 是把你的原话转来的一方 */
  by?: TurnBy;
  via?: 'lead' | 'butler';
  /** v7：去掉内部前缀、给人看的正文（和 body 不同时才有）；edited 是你编辑过的消息；note 是答题时选项之外写的字 */
  display?: string;
  edited?: boolean;
  note?: string;
  /** v4 新增，可选 */
  answers?: TurnAnswer[];
  failure?: TurnFailure;
  /** v3 新增，都可选 */
  steps?: StepView[];
  plan?: PlanItem[];
  usage?: TokenUsage;
  seconds?: number;
  speed?: number;
  context?: ContextUse;
  notes?: TurnNote[];
  started_topics?: StartedTopic[];
  /** v23：管家这一轮把你的话交给了谁（开的、转的话题，转给的负责人） */
  handed?: HandedTarget[];
  /** v23：普通话题这一轮的结论发到私聊顶层的时刻（秒） */
  relayed_at?: number;
  /** 大厅（/api/room）：这一轮属于哪个话题（管家、负责人或普通话题） */
  topic_key?: string;
  key: string;
  seq: number;
  role: string;
  status: string;
  body: string;
  job_id: string | null;
  started_at: number | null;
  ended_at: number | null;
  artifacts: ArtifactItem[];
  /** v34：这一轮检查点里的完成标准（{item, state: 满足|没满足|没查, evidence}），没有就不带 */
  criteria?: TurnCriterion[];
  /** v34：这一轮检查点里的交付（提交号、部署到哪、怎么验的），没有就不带 */
  delivery?: TurnDelivery;
  /** v43：这一轮带了几条记忆（used 是标题，codes 是短码），没有就不带 */
  memory?: TurnMemory;
}

export interface TopicDetailResponse {
  topic: TopicBrief;
  workspace: string;
  project_title: string | null;
  turns: TurnView[];
  question: QuestionView | null;
  outbox: OutboxView[];
  synced_at: string | null;
}

export interface OutboxRequest {
  op_id: string;
  kind: OutboxKind;
  topic_key?: string;
  project_key?: string;
  payload: Record<string, unknown>;
}
export interface OutboxResponse {
  seq: number;
  op_id: string;
  state: string;
}
/** 按 op_id 读一条发件的结果（看板「整理」这种不属于话题的动作用） */
export interface OutboxStatus {
  op_id: string;
  kind: string;
  state: string;
  message: string;
  created_at: string;
}

/* ---- v1：快照、增量 turns、搜索 ---- */
export type TopicKind = 'topic' | 'butler' | 'lead';

export interface SnapshotProject {
  key: string;
  /** Bridge 分配的固定头像名，与飞书卡片相同；旧数据可能没有。 */
  avatar?: string;
  title: string;
  goal: string;
  status: string;
  status_reason: string;
  lead_topic_key: string | null;
  started_at: number | null;
  topic_keys: string[];
  memory: MemoryItem[];
  /** v31：专项记忆的版本（8 位十六进制，增删一条就变；负责人的状态包末尾那行“专项记忆版本”是同一个值） */
  memory_version?: string;
  library: LibraryItem[];
  updated_at: number;
  counts: { topics: number; waiting: number; running: number };
  /** v40：负责人的目标检查表（「进度」标签）；旧 Covey、负责人还没写时没有 */
  checklist?: ChecklistItem[];
  /** v40：负责人记的里程碑和收尾，最新 10 条，旧的在前 */
  milestones?: ProjectNote[];
  /** v40：决定日志，最新 10 条，旧的在前 */
  decisions?: ProjectNote[];
  /** v41：负责人写的背景（≤300 字）；没写就没有这个键 */
  background?: string;
  /** v41：动作（含 dropped，网页不画 dropped）；没写就没有这个键 */
  actions?: BriefAction[];
  /** v41：没处理的协作缺口；没有就没有这个键 */
  gaps?: ProjectGap[];
}

/** v41：动作的状态只有 open、dropped（Covey core/project_actions.py）；做完、在做、等你这些是网页按条目和话题算的，不存 */
export const ACTION_STATES = ['open', 'dropped'] as const;
export type ActionStoredState = (typeof ACTION_STATES)[number];
/** v41：负责人拆出的一个动作。items 是检查表条目 id；topics 是话题 key（也认 T 号）；title ≤30 字，note ≤80 字（负责人那一句现状）；
 * at 是动作最后改动的时刻（秒） */
export interface BriefAction {
  id: string;
  title: string;
  note: string;
  items: string[];
  topics: string[];
  state: ActionStoredState;
  at: number;
}
/** v41：一条没处理的协作缺口：thread 是报缺口的话题（key 或 T 号），text ≤200 字 */
export interface ProjectGap {
  id: string;
  thread: string;
  text: string;
  at: number;
}

/** 目标检查表的状态（Covey core/project_checklist.py STATES）：open 没满足、done 满足、blocked 卡住、dropped 不做了 */
export const CHECKLIST_STATES = ['open', 'done', 'blocked', 'dropped'] as const;
export type ChecklistState = (typeof CHECKLIST_STATES)[number];
/** v40：目标检查表的一条（docs/web.md 专项条目）。topic 是交付话题的 key（还没派是空串）；needs 是它等的条目 id；
 * at 是条目最后改动的时刻（秒），不是完成时刻 */
export interface ChecklistItem {
  id: string;
  criterion: string;
  topic: string;
  needs: string[];
  evidence: string;
  gap: string;
  state: ChecklistState;
  at: number;
  /** v41，都可选（旧 Covey 不推）：title 是 ≤24 字的短标题；waiting_on 写过才有，'owner' 是下一步在主人手上、空串是写过“不在主人手上”
   * （没有这个键就退回按缺口文字推断）；done_at 是转成 done 的时刻（秒，非 0 才有）；topics 是参与这一条的几个话题（key 或 T 号，
   * 不含 topic 自己时 topic 仍是主要的那个） */
  title?: string;
  waiting_on?: string;
  done_at?: number;
  topics?: string[];
}
/** v40：专项记录里的一条进展或决定：kind 是 milestone、finish（里程碑），决定日志里多是 note */
export interface ProjectNote {
  id: string;
  at: number;
  kind: string;
  text: string;
}

export interface SnapshotTopic {
  /** v43：同 TopicBrief；workspace 保留只读，旧话题显示用 */
  repos?: TopicRepo[];
  home_start?: boolean;
  key: string;
  number: number | null;
  title: string;
  kind: TopicKind;
  project_key: string | null;
  state: string;
  unseen: boolean;
  updated_at: number;
  harness: string;
  model: string;
  /** v7：Covey 给的可读模型名（Opus 5.5） */
  model_name?: string;
  /** 最新一轮一句话，≤120 字 */
  latest: string;
  latest_role: string;
  /** v3：已有几轮、第一轮开始、上下文、在跑那一轮此刻的样子 */
  round: number;
  started_at: number | null;
  context: ContextUse | null;
  running: RunningNow | null;
  /** v4，可选 */
  gist?: TopicGist;
  progress?: TopicProgress;
  /** 最新一轮的 status 和它的 failure（看板“停住”用） */
  latest_status?: string;
  latest_failure?: TurnFailure;
  /** 最新一轮的 key（“先放着”记的是它） */
  latest_key?: string;
  /** 最新一轮的任务号（v33 recover 要它：看板上的「检查并继续」直接点，不用先进话题） */
  latest_job_id?: string;
  /** v5：状态词表的 key（covey core/look.py STATES：waiting、stopped、stuck、result、running、queued、done、cancelled、idle）。
   * 网页按它上色、取词；旧 Bridge 推的数据没有它，退回看 state */
  tone?: string;
  /** v6：它在 Covey 看板上的哪一组（同 tone 的 key；空串＝不在看板上：看过 12 小时的结果、点了“不管了”的）。
   * “要你处理”里的停住了和侧栏红点按它算，和看板一致；没开看板的 Bridge 不推它，退回看 tone */
  board?: string;
  /** v13：成员话题什么时候进的专项（秒）。专项报告只算它进来以后结束的轮次，和专项卡的 token 同一个口径；没有就从专项开始算 */
  joined_at?: number;
  /** v20：话题现在的配置（只有 kind=topic、Covey 读得到模型目录时才有）。有它且设置里有 launch 时，输入框下的配置栏能点开换配置。
   * v36 起专项负责人（kind=lead）也带：同一个面板只列 bridge_tools 的模型、不画工作区和运行方式（负责人留在它读的地方） */
  config?: TopicConfig;
  /** v34：Covey 的定时回看（开了到点回来看、有预约才有）：几点（秒）、看什么；v35 起带 set_at（取消时核对）和 running（Covey 正在回看） */
  revisit?: TopicRevisit;
  /** v34：等你在飞书卡上确认的代发草稿（开了代发、有草稿才有）；网页只读，确认和发送都在飞书原卡上 */
  owner_send?: OwnerSendDraft[];
}

export interface RunningNow {
  step_label: string;
  step_kind: string;
  /** 检查点里正在做的那一步（plan 里 state=doing 的标题），没有就空 */
  plan_doing?: string;
  plan_done: number;
  plan_total: number;
  started_at: number | null;
}

export interface SnapshotResponse {
  synced_at: string | null;
  butler_key: string | null;
  projects: SnapshotProject[];
  topics: SnapshotTopic[];
  questions: QuestionView[];
  /** v34：Covey 算好的要你处理（settings 类条目 asks，和话题同一步推来）；旧 Covey 没有，网页自己数 */
  asks?: SettingsAsks;
  /** v38：下次取增量（?since=）用的游标：这次查询开始时数据库的时间往前留几秒。旧服务端没有 */
  cursor?: string;
  /** v38：带了 since 时为 true：topics 只有这个游标之后变过的，其余话题看 keys，要和上一份按 key 合并。没有就是整份，直接替换 */
  partial?: true;
  /** v38：partial 时现在所有话题的 key（按 updated_at 倒序，管家只留最新那个）：不在里面的话题已经没了，顺序也照它排 */
  keys?: string[];
}

export interface TurnsResponse {
  topic: SnapshotTopic & { workspace: string; project_title: string | null };
  turns: TurnView[];
  question: QuestionView | null;
  outbox: OutboxView[];
  /** v31：这个话题当汇总方或协助方的协作（没有就不带）。增量取时只带还开着的和两小时内动过的，页面按 key 合并 */
  collabs?: CollabView[];
  /** v31：话题自己的记忆（Covey 推在话题条目的 memory 里；没有就不带） */
  memory?: MemoryItem[];
  /** 下次增量取数带回来的 since：这之后改过的轮次也一起回来（排在在跑那一轮前面、后来补上图的那一行）。旧服务端不带 */
  cursor?: string;
}

/** 大厅：管家、各负责人的对话和发到私聊的话题结论排在一条时间线上（按 room_at 排）；cursor 是增量取数的起点 */
export interface RoomResponse {
  butler_key: string | null;
  turns: TurnView[];
  /** 大厅发出的话（管家、负责人和引用转给话题的），最近的在前 */
  outbox: OutboxView[];
  cursor: string | null;
  /** v31：最近的协作（按 cursor 增量，同 turns），页面按 key 合并 */
  collabs?: CollabView[];
}

export interface SearchHit {
  topic_key: string;
  number: number | null;
  title: string;
  kind: TopicKind;
  project_key: string | null;
  /** 标题命中时为 null */
  seq: number | null;
  role: string | null;
  /** v7：主人那一轮是谁说的 */
  by?: string | null;
  snippet: string;
  at: number;
}

/* ---- v3：统计与报告 ---- */
export interface ActivityItem {
  at: number;
  kind: 'done' | 'asked' | 'answered' | 'failed';
  topic_key: string;
  number: number | null;
  title: string;
  text: string;
}
export interface StatsResponse {
  today_tokens: number;
  today_work_seconds: number;
  avg_wait_seconds: number | null;
  today_done: number;
  /** 现在还开着的问题数，和其中等得最久的（秒） */
  open_wait_count: number;
  longest_open_wait_seconds: number | null;
  token_series: number[];
  work_series: number[];
  wait_series: number[];
  activity: ActivityItem[];
  /** 近 24 小时按话题合并的动态（“T518 · 做完 5 轮 · 问过 2 次”），新近的在前 */
  feed?: FeedItem[];
  /** 近 7 天（按北京时间的自然日，今天在最后）：每天的 token、合计、做完的轮数；旧服务端没有 */
  week_series?: number[];
  week_tokens?: number;
  week_turns?: number;
  /** 近 7 天输入里缓存命中的比例（%），没有输入时 null */
  week_cache_pct?: number | null;
  /** v15：本周用量来自 Covey 的 usage 快照时带上：话题数、各 Harness */
  week_topics?: number;
  week_harnesses?: { title: string; tasks: number; tokens: number }[];
}
/** v26：用量按天，和飞书「用量」卡同一个算法（按任务开始的那天算，含子任务；token 含缓存命中），按 Covey 那台机器的本地自然日切开 */
export interface UsageFigures {
  tasks: number;
  /** 这些任务里的话题数（去重） */
  topics: number;
  tokens: number;
  input: number;
  /** 缓存命中，算在 input 里 */
  cache: number;
  output: number;
  /** 没有 token 记录的任务（如还没收到回复就停了），算进 tasks，不在 token 里 */
  untracked: number;
}
export interface UsageDay extends UsageFigures {
  /** 2026-11-21 */
  date: string;
}
export interface UsageHarness {
  title: string;
  tasks: number;
  tokens: number;
  input: number;
  cache: number;
  untracked: number;
}
/** 一段时间（最近 days 天，含今天）的合计：话题数不是每天相加，所以由 Covey 单算 */
export interface UsageSpan extends UsageFigures {
  days: number;
  since: string;
  harnesses: UsageHarness[];
}
export interface UsageResponse {
  /** Covey 算这份数的时刻（秒） */
  at: number | null;
  /** 最近 90 天，旧的在前，最后一天是今天（还没过完）；没任务的天也在 */
  days: UsageDay[];
  /** 7、30、90 天各自的合计 */
  spans: UsageSpan[];
  /** 最早一条任务记录的日期，落在这些天里才有；那之前还没有记录，不画成 0 */
  first: string | null;
}
export interface FeedItem {
  topic_key: string;
  number: number | null;
  title: string;
  done: number;
  failed: number;
  asked: number;
  at: number;
}
export interface ReportTurn {
  topic_key: string;
  number: number | null;
  title: string;
  kind: string;
  harness: string;
  seq: number;
  role: string;
  status: string;
  started_at: number | null;
  ended_at: number | null;
  seconds: number;
  usage: TokenUsage | null;
  artifacts: number;
  /** 第一个产物的名字（有产物才有） */
  artifact?: string | null;
  /** Covey 第一次估出来的这一轮总用时（秒）：开始到估的那一刻 + 当时估的剩余；没估过为 null */
  estimate_seconds?: number | null;
  /** 估过用时的轮次：回答的开头（最多 160 字），报告页取第一句说这一轮做了什么 */
  head?: string | null;
}
export interface ReportQuestion {
  topic_key: string;
  number: number | null;
  asked_at: number | null;
  answered_at: number | null;
  /** open 还在等你；held 在负责人手上；answered、cancelled 已经不等了 */
  state: string;
  /** 不再等的时刻（Covey 数据约定 v7）；还在等、或旧推送不知道时为 null */
  closed_at: number | null;
  /** 怎么不等的：owner、feedback、skipped、lead、superseded、stale、switched、rewound、lead_stop、web_stop、cancelled；还在等为空 */
  closed_by: string;
}
export interface ReportResponse {
  project_key: string;
  turns: ReportTurn[];
  questions: ReportQuestion[];
}

/* ---- openapi（Covey 一侧） ---- */
export interface SyncItem {
  type: SyncType;
  key: string;
  rev?: string;
  data: Record<string, unknown>;
}
export interface SyncDeleted {
  type: SyncType;
  key: string;
}
/** v19：Covey 传一张图的一份（POST /openapi/sync/image）；data 是 base64，解出来不超过 760 KB（v44：不超过服务端 limits.image_bytes） */
export interface ImageUploadRequest {
  id: string;
  v: 'thumb' | 'full';
  mime: string;
  w?: number;
  h?: number;
  /** 秒：消息之后 90 天，过了就删 */
  expires_at: number;
  data: string;
}
/** v19：POST /openapi/sync/images/have，哪些图两份都有了 */
export interface ImageHaveRequest {
  ids: string[];
}
export interface ImageHaveResponse {
  have: string[];
  /** v44：同 SyncUpsertResponse.limits；Covey 传图之前先问 have，所以从这里就知道一张图能多大 */
  limits?: SyncLimits;
}

export interface SyncUpsertRequest {
  items?: SyncItem[];
  deleted?: SyncDeleted[];
}
export interface SyncUpsertResponse {
  ok: boolean;
  upserted: number;
  skipped: number;
  deleted: number;
  /** v44：这个服务端此刻的收发上限和内存读数。旧服务端没有，Covey 照 1 MB 一批、700 KB 一张图的老办法 */
  limits?: SyncLimits;
}
/** v44：服务端的上限（upsert、images/have 的响应里带）。Covey 照它放大一批和一张图；数都按字节 */
export interface SyncLimits {
  /** 请求体上限：BODY_SIZE_LIMIT，没设是代码默认 8mb（妙搭 FaaS 的硬顶是 12 MB） */
  body_bytes: number;
  /** 一份图片（/sync/image 的 data 解出来）最多多少字节 */
  image_bytes: number;
  /** 图片和附件这会儿存在哪：file 是妙搭文件存储，db 是本应用库（旧办法；文件存储用不了时退回它） */
  store: 'file' | 'db';
  /** 内存（MB）：V8 堆上限（看 NODE_OPTIONS 的 --max-old-space-size 生效没有）、此刻常驻、容器上限；读不到是 0 */
  heap_limit_mb: number;
  rss_mb: number;
  mem_limit_mb: number;
}
export interface InboxItem {
  seq: number;
  op_id: string;
  kind: string;
  topic_key: string | null;
  project_key: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}
export interface InboxResponse {
  items: InboxItem[];
  /** 请求带了 wait 时才有：这个服务端认 wait（不管这次有没有真的等）。旧服务端没有，不带 wait 的旧 Covey 看到的和以前一样 */
  waited?: true;
}
export interface AckResult {
  seq?: number;
  op_id: string;
  state: 'done' | 'failed';
  message?: string;
}
export interface SyncAckRequest {
  results: AckResult[];
}
export interface SyncAckResponse {
  ok: boolean;
  updated: number;
}

/* ---- v7：设置页（Covey 推一条 type=settings, key=settings；页面用 outbox kind=settings 改） ---- */
export type SettingsTone = 'green' | 'orange' | 'red' | 'neutral' | 'blue';
export interface SettingsTab {
  key: string;
  label: string;
}
export interface SettingsOption {
  value: string;
  text: string;
}
export interface SettingsRow {
  key: string;
  kind: string;
  label: string;
  current: string;
  text: string;
  options: SettingsOption[];
  editable: boolean;
}
export interface SettingsError {
  error: string;
}
export interface SettingsTopicPage {
  summary: string;
  note: string;
  rows: SettingsRow[];
  start_note: string;
  trusted: { key: string; text: string }[] | null;
  more: SettingsRow[];
  more_note: string;
  locked: string;
}
export interface SettingsMainPage {
  summary: string;
  on: boolean;
  harness?: string;
  rows: SettingsRow[];
  more?: SettingsRow[];
  note?: string;
  locked?: string;
}
/** 配置（v22，T619）：飞书设置卡的「配置」一页，分段：新话题、管家（部署允许管家时）、专项负责人（v36，开了 COVEY_PROJECTS 时）、
 * 辅助模型、更多。网页上各段都展开 */
export interface SettingsConfigSection {
  title: string;
  rows: SettingsRow[];
  note?: string;
}
export interface SettingsConfigTopic extends SettingsConfigSection {
  /** 直接开工下线后新版 Covey 不再推（start 行、trusted、start_note）；旧数据有就照旧显示 */
  start_note?: string;
  trusted?: { key: string; text: string }[] | null;
}
export interface SettingsConfigMain extends SettingsConfigSection {
  on: boolean;
}
export interface SettingsConfigPage {
  summary: string;
  topic: SettingsConfigTopic;
  main?: SettingsConfigMain;
  /** 独立复核：T937 下线，新版 Covey 不再推；旧版推来的页面也不画，改它的行新版回“独立复核已下线” */
  review?: SettingsConfigSection;
  /** v36：专项负责人（开了 COVEY_PROJECTS 才有）：新建的专项，负责人用这里选的 Harness、模型、思考预算；行的 key 是 l_harness、l_provider、l_model、l_effort，
   * 改行同管家一段（settings 的 row）。已有专项的负责人在它自己的配置栏换（话题条目 kind=lead 的 config） */
  lead?: SettingsConfigSection;
  /** 辅助模型（T884）：分流、整理专项、记忆自己长这些后台活都用这一套。行：key assist（kind bridge_main_assist_harness：自动、各 Harness、不用模型）；
   * T924 起点名了 Harness 再有 a_provider、a_model、a_effort（kind bridge_main_assist_provider/model/effort，同专项负责人）；
   * 改行同别的段：settings 的 row，管家关着也能改；旧版 Covey 只有 assist 一行，没有这一段的更旧 */
  assist?: SettingsConfigAssist;
  more: SettingsRow[];
  more_note: string;
  locked: string;
}
/** 后台活最近一次的一行：每个功能一行，只有原因码和说法，没有原话 */
export interface SettingsAssistRecent {
  /** track（分流）、tidy（整理专项，含整理时补话题要点）、learn（记忆自己长）；旧版 Covey 还可能有 backfill 和已下线的几项（T898） */
  feature: string;
  label: string;
  /** Harness 的名字（claude、codex、agy）；没问到谁时是空串 */
  harness: string;
  /** Harness 的显示名（Claude Code、Codex、Antigravity） */
  name: string;
  model: string;
  ok: boolean;
  /** 原因码（core/assist.py）；成功是空串 */
  code: string;
  /** 原因的说法（“登录失效或没有权限”）；成功是空串 */
  words: string;
  ms: number;
  /** 秒 */
  at: number;
  /** 主人点的（✨ 整理） */
  manual: boolean;
}
export interface SettingsConfigAssist extends SettingsConfigSection {
  /** 现在实际用的（“现在：轻的判断用 Codex · gpt-6-luna，整理用 …”），或用不了的原因 */
  words: string;
  recent: SettingsAssistRecent[];
}
export interface SettingsWorkspaceItem {
  name: string;
  path: string;
  tags: { text: string; tone: SettingsTone }[];
  used: number | null;
  note?: string;
  /** v15：默认那条不能移除 */
  removable?: boolean;
}
export interface SettingsWorkspacePage {
  summary: string;
  items: SettingsWorkspaceItem[];
  notes: boolean;
  note: string;
  note_length: number;
  /** v15：目录确认不存在的有几个 */
  missing?: number;
}
export interface SettingsGroupItem {
  key: string;
  name: string;
  space: string;
  state: { text: string; tone: SettingsTone } | null;
  free: string;
  switch: 'off' | 'on' | '';
  /** v15：免@ 的当前值（没开免@ 是 null）和网页能选的（只有“关闭”“只有你”，或空） */
  free_mode?: '' | 'owner' | 'some' | 'all' | null;
  free_choices?: SettingsOption[];
}
export interface SettingsGroupsPage {
  summary: string;
  items: SettingsGroupItem[];
}
export type SettingsNodeDot = 'ok' | 'tight' | 'down' | 'never';
export interface SettingsNodesPage {
  summary: string;
  items: { alias: string; title: string; line: string; grey: boolean; local: boolean; dot?: SettingsNodeDot }[];
  /** v15：正在经 ssh 添加的和失败的 */
  joins?: { alias: string; state: 'joining' | 'failed' | 'done'; message: string }[];
  can_add?: boolean;
  /** v46：devices.json 和节点按别名并成一行（新页面画它；旧页面照画 items） */
  rows?: SettingsDeviceRow[];
  /** v46：页面上正在添加（先体检）的设备 */
  adding?: { alias: string; state: 'checking' | 'failed' | 'done'; message: string }[];
  /** v46：这里能不能加设备（多设备开着） */
  can_add_device?: boolean;
  /** v46：devices.json 读不出来时的原因 */
  error?: string;
}
/** v46：设备页的一行：一台机器（devices.json 里的、只装了监控节点的，或两者都有） */
export interface SettingsDeviceRow {
  alias: string;
  title: string;
  /** 用途，一行 */
  note: string;
  /** 可写还是只读；'' 是 devices.json 里没写 */
  access: 'write' | 'read' | '';
  /** 在 devices.json 里 */
  listed: boolean;
  /** ssh 写法（选项在前，主机在后）；没有 ssh 是 '' */
  ssh: string;
  /** 有 ssh，能去执行；false 是只能看状况 */
  runs: boolean;
  /** Covey 上次经 ssh 看它：ok 连得上，down 连不上，never 还没连过；没有 ssh 是 null */
  link: { state: 'ok' | 'down' | 'never'; text: string } | null;
  /** 监控节点的状况；没装节点是 null */
  node: { dot: SettingsNodeDot; line: string; grey: string | boolean } | null;
  /** 上次体检（或自检）：steps 3 是网页上的体检（ssh、设备端小程序、hello） */
  check: { ok: boolean; at: number; text: string; steps?: number } | null;
  /** 网页上点的体检正在跑或刚结束 */
  checking: { state: 'checking' | 'failed' | 'done'; message: string } | null;
  /** Covey 自己这台（本机的节点） */
  local: boolean;
}
export interface SettingsProxyContact {
  key: string;
  name: string;
  kind: string;
  paused: boolean;
  from?: string | null;
}
/** v15：代答页 */
export interface SettingsProxyPage {
  summary: string;
  enabled: boolean;
  group: string;
  login_ok: boolean;
  problems: string[];
  rows: SettingsRow[];
  contacts: SettingsProxyContact[];
}
/* ---- v17：开新话题（settings.launch；页面用 outbox kind=launch 发，payload = LaunchPayload） ---- */
export interface LaunchModel {
  key: string;
  harness: string;
  harness_title: string;
  provider: string;
  provider_title: string;
  label: string;
  budgets: SettingsOption[];
  default_budget: string;
  start_budget: string;
  work_modes: string[];
  /** 卡上对每个工作模式的叫法（Covey v16 后补的字段，旧 Covey 没有） */
  work_mode_texts?: Record<string, string>;
  /** v36：这个模型能当专项负责人（它的 Harness 能接 Bridge 工具，和管家能选的一样）。负责人的换配置只列这些；旧 Covey 没有 */
  bridge_tools?: boolean;
}
/** v43：新 Covey 不再推 workspaces、run_modes，改推 devices 和每个话题自己的 repos；旧 Covey 照旧 */
export interface LaunchDevice {
  /** 设备别名；本机是空串（T879 起 Covey 把本机排在第一行推来，旧 Covey 只推远程设备，网页自己补本机） */
  name: string;
  /** T879：选项上的叫法（本机写“本机”，设备是 devices.json 的 title）；旧 Covey 没有，用 name */
  title?: string;
  dot: string;
  /** 这台能用的 Harness；空 = 不限（本机） */
  harnesses: string[];
  /** T879：本机那一行 */
  local?: boolean;
}
export interface LaunchWorkspace {
  name: string;
  device: string;
  harnesses: string[];
}
export interface LaunchPreselect {
  model: string;
  effort: string;
  work_mode: string;
  /** v43：可选（新 Covey 不推） */
  workspace?: string;
  /** v43：可选（新 Covey 不推） */
  run_mode?: string;
  /** v43（WEB 包加，向后兼容）：执行设备，LaunchData.devices 里的 name。只在有远程设备、选了远程设备时网页才带；不带或空串 = 本机。
   *  话题的 config 里（T879）是话题所在的设备（空串 = 本机，只读：换配置换不了机器）；旧 Covey 的 config 没有这个键 */
  device?: string;
}
export interface LaunchData {
  models: LaunchModel[];
  /** v43：可选（新 Covey 不推） */
  workspaces?: LaunchWorkspace[];
  /** v43：可选（新 Covey 不推） */
  run_modes?: SettingsOption[];
  /** v43：可选，设备列表（name、状态点、能用的 Harness） */
  devices?: LaunchDevice[];
  preselect: LaunchPreselect;
}
/** v43：启动、换配置带的仓库（≤10 项）：read 只读，write 可写 */
export interface LaunchRepo {
  name: string;
  mode: 'read' | 'write';
}
export interface LaunchPayload extends LaunchPreselect {
  text: string;
  /** v43：这个话题用的仓库 */
  repos?: LaunchRepo[];
  /** v25：第一轮带的附件（同 say 的 files，传完的） */
  files?: SayFile[];
}

/* ---- v20：话题页换配置（outbox kind=switch，payload = SwitchPayload）、输入框附件 ---- */
/** 话题现在的配置：和开新话题同一套键（model 是 launch.models[].key；键不在目录里时网页要你重选模型） */
export type TopicConfig = LaunchPreselect;
/** outbox kind=switch 的 payload：同飞书 🔁 换配置的“确认更换” */
export type SwitchPayload = LaunchPreselect;

/** say 的 payload 里的一个附件（先经 /api/uploads 传完） */
export interface SayFile {
  id: string;
  name: string;
  mime: string;
  size: number;
}
/** v31：结构化引用，引的是哪个话题的哪一轮、属于哪次协作（大厅自己补的行没有 seq） */
export interface SayQuote {
  topic_key: string;
  seq?: number;
  collab?: string;
}
/** outbox kind=say 的 payload；有附件时 text 可以为空。
 * v31：to 是点到的参与者（管家、负责人、话题的 key，按点的顺序；网页不从正文里的 @ 或 T 号猜）；integrator 是主人选的汇总方
 * （to 里的一位或当前话题，只在和 route 的默认不同时带）；quote 是结构化引用。交给谁、谁汇总由 Covey 按 core/collab.py 的 route 定 */
export interface SayPayload {
  text: string;
  files?: SayFile[];
  /** 大厅发的（不是话题页） */
  room?: boolean;
  to?: string[];
  integrator?: string;
  quote?: SayQuote;
}
/** outbox kind=stop 的 payload（v31）：带 collab 时只停这次协作排队和在跑的咨询轮，不停接收方别的工作 */
export interface StopPayload {
  collab?: string;
}
/** memory_fix 的 what：scope 改适用范围或条件（带 applies）、once 只限当次、confirm 确认长期（只认主人） */
export const MEMORY_FIXES = ['scope', 'once', 'confirm'] as const;
export type MemoryFix = (typeof MEMORY_FIXES)[number];
/** outbox kind=memory_fix 的 payload（v31）：专项记忆带 project_key，话题记忆带 topic_key（和发件行上的一致） */
export interface MemoryFixPayload {
  project_key?: string;
  topic_key?: string;
  entry_id: string;
  what: MemoryFix;
  applies?: string;
}
/** GET /api/locate?message_id=|question=：记忆的“查看原文”找那条消息或那道题在哪个话题的哪一轮；seq 不知道是 null */
export interface LocateResponse {
  topic_key: string;
  kind: TopicKind;
  project_key: string | null;
  seq: number | null;
  turn_key: string | null;
}
/** 附件：单个文件上限、每段原始字节上限、一句话最多几个 */
export const UPLOAD_MAX_BYTES = 20 * 1024 * 1024;
export const UPLOAD_PART_BYTES = 512 * 1024;
export const UPLOAD_MAX_FILES = 9;
/** POST /api/uploads */
export interface UploadStartRequest {
  name: string;
  mime: string;
  size: number;
}
export interface UploadStartResponse {
  id: string;
  parts: number;
  /** v44：这个附件每段原始字节（最后一段可以更短）。旧服务端没有，按 UPLOAD_PART_BYTES 切 */
  part_bytes?: number;
}
/** POST /api/uploads/:id/parts：n 从 0 起，data 是 base64 */
export interface UploadPartRequest {
  n: number;
  data: string;
}
/** POST /api/uploads/:id/done */
export type UploadDoneResponse = SayFile;
/** GET /openapi/sync/upload?id= */
export interface OpenUploadInfo extends SayFile {
  parts: number;
  done: boolean;
}
/** GET /openapi/sync/upload/part?id=&n= */
export interface OpenUploadPart {
  n: number;
  data: string;
}
export const LAUNCH_RUN_MODES = ['direct', 'worktree'] as const;

export interface SettingsData {
  launch?: LaunchData;
  /** v34：要你处理的权威计数 */
  asks?: SettingsAsks;
  tabs: SettingsTab[];
  feishu: string;
  /** v34：这个 Covey 认的网页操作；有 project_finish、project_stop、memory_undo 才画收尾、停止和记忆的「不对」，有 revisit_cancel（v35）才画定时回看的「取消」，有 upgrade（v39）才画「版本与更新」，有 quota_refresh（v42）才画「账号与额度」 */
  ops?: string[];
  config?: SettingsConfigPage | SettingsError;
  /** v21 及以前：新话题、管家两页（v22 起并进 config） */
  topic?: SettingsTopicPage | SettingsError;
  main?: SettingsMainPage | SettingsError;
  workspace?: SettingsWorkspacePage | SettingsError;
  /** v43：仓库页（「工作区」页 workspace 保留给旧 Covey） */
  repos?: SettingsReposPage | SettingsError;
  groups?: SettingsGroupsPage | SettingsError;
  nodes?: SettingsNodesPage | SettingsError;
  proxy?: SettingsProxyPage | SettingsError;
}
/** GET /api/settings：sync_state 里 name='settings' 的那一行；没收到过是 null */
export interface SettingsResponse {
  rev: string;
  data: SettingsData;
  at: string;
}
/** outbox kind=settings 的 payload（v7） */
export const SETTINGS_WHATS = [
  'row', 'untrust', 'note', 'group',
  'free', 'workspace_add', 'workspace_remove', 'workspace_prune', 'node_add', 'node_remove', 'refresh', 'proxy',
  // v43：仓库（旧 workspace_add/remove/prune 照收，Bridge 转成 repos_*）
  'repos_set', 'repos_add', 'repos_remove', 'worktrees_prune',
  // v46：设备名单（devices.json）
  'device_add', 'device_set', 'device_remove', 'device_check',
] as const;
export type SettingsWhat = (typeof SETTINGS_WHATS)[number];
/** v15：proxy 的 op */
export const PROXY_OPS = ['enable', 'disable', 'row', 'pause', 'resume', 'remove'] as const;
/** 同步条目 type=settings 的 key；settings 是设置快照，usage 是本周用量（v15），tidy 是整理专项的建议（v16），asks 是要你处理（v34），
 * memory 是主记忆（v37），upgrade 是版本与更新（v39），quota 是账号与额度（v42） */
export const SETTINGS_KEYS = ['settings', 'usage', 'tidy', 'asks', 'memory', 'upgrade', 'quota'] as const;
/** v43 记忆中心的键（≤41 字符，sync_state.name 是 varchar(64)）：
 *  memory:index    小键（侧栏角标只轮询它）：总数、待审核数、各范围摘要与每键 rev（v45 起没有体检、导入摘要、配额）
 *  memory:global   全局条目 + 待审核列表 + 最近 100 条变更记录
 *  memory:topics   活跃话题（近 30 天有记忆）的话题范围条目汇总
 *  memory:p:<专项 id>  一个专项的条目（专项 id 同 core/projects.py 的 [0-9a-z]{6,32}）
 *  memory:import:<页>、memory:previews  v45 起不再推（Bridge 会对旧的发 SyncDeleted）；正则仍认，否则删不掉库里的旧行
 *  旧键 memory（v37 形状）继续推给旧网页。每键的值 ≤256 KB，带 rev。 */
export const MEMORY_KEY_RE = /^memory:(index|global|previews|topics|p:[0-9a-z]{6,32}|import:\d{1,3})$/;
export const isSettingsKey = (v: string): boolean =>
  (SETTINGS_KEYS as readonly string[]).includes(v) || MEMORY_KEY_RE.test(v);

/** v16：整理专项的建议。kind 对应的标签词、按钮字由 Covey 给（tag、verb），headline、from 也是 Covey 拼好的 */
export const TIDY_KINDS = ['split', 'create', 'leave', 'join', 'merge', 'rename', 'fold'] as const;
export type TidyKind = (typeof TIDY_KINDS)[number];
/** open 待定；done 照办了；declined 不用；failed 没做成；expired 已过时；never 以后别提议 */
export const TIDY_STATES = ['open', 'done', 'declined', 'failed', 'expired', 'never'] as const;
export type TidyState = (typeof TIDY_STATES)[number];
export interface TidyTopic {
  /** 话题 key（64 位十六进制） */
  key: string;
  /** “T566” */
  label: string;
  title: string;
}
export interface TidyProposal {
  id: string;
  /** 哪一次整理 */
  run: string;
  kind: TidyKind;
  tag: string;
  verb: string;
  headline: string;
  from: string;
  why: string;
  goal: string;
  topics: TidyTopic[];
  state: TidyState;
  /** 非 open 时的一行灰字 */
  note: string;
  /** v28：照办了以后「去专项」打开哪个专项（新建、拆出是新的那个，合并是留下的那个，其余是动到的那个）；没有、或专项没了是空串；旧版 Covey 不带 */
  project?: string;
  at: number;
}
/** 最近一次整理（T884）：在跑时带进度，失败时带原因（整理卡上的那句）和问的是哪个 Harness；只有原因码和说法，没有原话 */
export interface TidyLastRun {
  run: string;
  state: 'running' | 'done' | 'failed';
  /** 失败的说法（“Codex 登录失效或没有权限”） */
  why?: string;
  /** 原因码（core/assist.py） */
  why_code?: string;
  /** 问的 Harness（claude、codex、agy） */
  via?: string;
  model?: string;
  /** 过一会儿再试有用（超时、限流、临时故障） */
  retry: boolean;
  /** 只整理和它有关的 */
  scope?: string;
  /** scheduled、asked、settings、web、butler */
  origin: string;
  /** 秒：结束时间，没结束是开始时间 */
  at: number;
  progress: { done: number; total: number; current: string };
}
export interface TidyData {
  proposals: TidyProposal[];
  /** 正在整理时有 */
  running: { done: number; total: number } | null;
  updated_at: number;
  /** T884：最近一次整理；旧版 Covey 不带 */
  last_run?: TidyLastRun;
}
/** GET /api/tidy：sync_state 里 name='tidy' 的 data；没收到过是 null */
export type TidyResponse = TidyData | null;
/** outbox kind=tidy_decide 的 payload：不带 topic_key */
export interface TidyDecidePayload {
  proposal: string;
  choice: 'ok' | 'decline';
}

/* ---- v34：到点回来看、代发草稿、完成标准与改动、要你处理的权威计数 ---- */
export interface TopicRevisit {
  /** 约好的时刻（秒） */
  at: number;
  /** 到点要看什么（一句话） */
  what: string;
  /** v35：这条预约是几点约下的（秒，带小数）；「取消」带上它，Covey 只取消同一条，换了新的不误删。旧 Covey 没有，就不画「取消」 */
  set_at?: number;
  /** v35：Covey 已经排出回看那一轮、正在回看（这时取消不了） */
  running?: boolean;
}
export interface OwnerSendDraft {
  id: string;
  /** 发给谁（名字或群名，给人看的） */
  to: string;
  /** 草稿的状态：waiting 等你确认，sending 在发，failed 没发出去 */
  state: string;
  /** 正文开头（给人认出是哪条，全文只在飞书卡上） */
  preview: string;
  created_at: number | null;
  /** 打开飞书里那张草稿卡的链接（拿得到才有） */
  link?: string;
}
export const CRITERION_STATES = ['满足', '没满足', '没查'] as const;
export interface TurnCriterion {
  item: string;
  state: string;
  evidence?: string;
}
export interface TurnDelivery {
  commit?: string;
  deployed?: string;
  verified?: string;
}
/** v34：Covey 算好的要你处理（core/board_read.py Asks.counts），网页有它就以它为准 */
export interface SettingsAsks {
  waiting: number;
  stopped: number;
  stuck: number;
  /** 算的时刻（秒） */
  at?: number;
}
/** v34：撤销刚记下的一条记忆（同答案卡上的「不对」）；topic_key 是那一轮所在的话题 */
export interface MemoryUndoPayload {
  job_id: string;
  entry_id: string;
}

/* ---------- 主记忆（v37，设置条目 key memory；Covey 的 app/web_memory.py view） ---------- */

/** 一条主记忆：原则、知识条目，或作为“它总结的”带进下一轮的候选 */
export interface MainMemoryEntry {
  /** principle:<id>、knowledge:<id>、candidate:<id>；改、删都用它 */
  key: string;
  kind: 'principle' | 'knowledge' | 'candidate';
  text: string;
  /** owner 你说的，lead 它总结的 */
  by: 'owner' | 'lead';
  pinned: boolean;
  /** 只有原则能标常驻 */
  pinnable: boolean;
  /** 原则、知识条目能改正文；候选不能 */
  editable: boolean;
  /** 下一轮带不带（放不下的是 false） */
  carried: boolean;
  /** 这一条占的额度（算在这一组的上限里：正文加编号、标注） */
  chars: number;
  /** 正文自己的字数（和编辑框里数的一样）；旧版 Covey 不推，页面按 text 数 */
  size?: number;
  /** 从哪句话学来的：主人的原话（自动学习才有），没有是空串 */
  quote?: string;
  /** 在哪个话题写的（T83；晚间候选转正是几个话题），说不清是空串 */
  topic?: string;
  /** 记下的时刻（秒） */
  at: number;
  /** 从哪来：网页上记的、管家记的、自动学习记的、话题 T83 里记的、设置卡里升为全局的、晚间候选里记下的；说不清是空串 */
  origin: string;
  /** 原则在网页上改过的时刻（ISO），没改过是空串 */
  edited: string;
  /** 知识条目的种类（decision、practice、pitfall、fact、question）和原因 */
  knowledge_kind?: string;
  why?: string;
}

export interface MainMemoryGroup {
  scope: 'global' | 'workspace';
  /** 全局是空串 */
  workspace: string;
  title: string;
  /** 下一轮这一组用了多少字、上限、放不下几条 */
  used: number;
  limit: number;
  left: number;
  /** 常驻的在前，再新的在前 */
  entries: MainMemoryEntry[];
}

/** 开着的候选（开了 COVEY_MEMORY_LEARN）：记下就成原则，不用就不再提 */
export interface MainMemoryCandidate {
  key: string;
  text: string;
  /** 空串是全局 */
  workspace: string;
  /** 已作为“它总结的”带进下一轮 */
  carried: boolean;
  at: number;
  /** 上次记下没成功的原因（比如这一组满了） */
  error: string;
  from: string;
}

/** 某个工作区下一轮实际带上的记忆那一段 */
export interface MainMemoryPreview {
  count: number;
  chars: number;
  left: number;
  text: string;
}

export interface MainMemoryData {
  /** false：记忆关着，页面收起这一页 */
  on: boolean;
  knowledge?: boolean;
  learn?: boolean;
  limits?: { text: number; group: number; pinned: number; global: number; workspace: number };
  kinds?: { value: string; text: string }[];
  default?: string;
  workspaces?: string[];
  groups?: MainMemoryGroup[];
  candidates?: MainMemoryCandidate[];
  previews?: Record<string, MainMemoryPreview>;
}

export interface MainMemoryResponse {
  rev: string;
  data: MainMemoryData;
  at: string;
}

/** outbox kind=memory_edit 的 payload（v37）：不带 topic_key、project_key */
export interface MemoryEditPayload {
  action: 'add' | 'edit' | 'pin' | 'unpin' | 'delete' | 'keep' | 'drop';
  key?: string;
  what?: 'principle' | 'knowledge';
  scope?: 'global' | 'workspace';
  workspace?: string;
  text?: string;
  /** 改之前看到的正文：Covey 核对，对不上就不覆盖 */
  base?: string;
  knowledge_kind?: string;
  why?: string;
}

/* v39：版本与更新（设置条目 key upgrade）。只有配了升级器的 Covey 才推，同时 settings.ops 里才有 upgrade */
/** 网页工作台：kind 是怎么部署的；current 已是 latest 那一版；manual 不是部署脚本建的，升级不动它 */
export interface UpgradeWeb {
  kind: 'selfhost' | 'miaoda' | string;
  current: boolean;
  manual: boolean;
}
/** 一个新提交：repo 是仓库目录名（covey、covey-internal），at 秒 */
export interface UpgradeChange {
  repo: string;
  sha: string;
  subject: string;
  at: number;
}
/** 正在跑的升级：started_at 秒，log 是升级输出的文件（Covey 那台机器上的路径） */
export interface UpgradeRunning {
  op_id: string;
  started_at: number;
  log: string;
}
/** 上一次从网页点的升级：done 全好；partial Bridge 升好了、网页没更新上；failed 没升成。tail 是升级输出最后几行 */
export interface UpgradeLast {
  state: 'done' | 'partial' | 'failed' | string;
  message: string;
  at: number;
  release?: string;
  log?: string;
  tail?: string[];
}
export interface UpgradeData {
  /** Bridge 现在跑的 release */
  live: string;
  /** 升级会部署的 release；没查过、查不到是空串 */
  latest: string;
  /** 升级会换东西（Bridge 或网页） */
  available: boolean;
  /** Bridge 会换 */
  bridge: boolean;
  /** null：没有网页工作台 */
  web: UpgradeWeb | null;
  /** 新提交数（两个库合计） */
  count: number;
  /** 最新的至多 20 个，新的在前 */
  changes: UpgradeChange[];
  /** 上次检查的时刻，秒；0 没查过 */
  checked_at: number;
  checking: boolean;
  /** 上次查失败的原因 */
  error: string;
  /** 现在不能升级的原因；空串能升级 */
  blocked: string;
  running: UpgradeRunning | null;
  last: UpgradeLast | null;
}
/** GET /api/upgrade：sync_state 里 name='upgrade' 的那一行；Covey 没推过是 null */
export interface UpgradeResponse {
  rev: string;
  data: UpgradeData;
  at: string;
}
/** outbox kind=upgrade 的 payload（v39）：不带 topic_key、project_key。check 马上查一次；run 查过再在独立进程里开始升级 */
export const UPGRADE_ACTIONS = ['check', 'run'] as const;
export type UpgradeAction = (typeof UPGRADE_ACTIONS)[number];
export interface UpgradePayload {
  action: UpgradeAction;
}

/* v42：账号与额度（设置条目 key quota）。Covey 有额度来源（<状态目录>/quota/ 里有快照）才推，同时 settings.ops 里才有 quota_refresh；
 * 来源都没了推一次 sources: []，页面收起这一页。Covey 的 app/web_quota.py 的 view 是它的出处 */
export const QUOTA_STATES = ['ok', 'spent', 'cooling', 'paused', 'login_required', 'expired', 'unknown'] as const;
export type QuotaState = (typeof QUOTA_STATES)[number];
export const QUOTA_KINDS = ['agy', 'gateway', 'command', 'url'] as const;
export type QuotaKind = (typeof QUOTA_KINDS)[number];
/** 一个额度窗口：kind 5h、7d（表里的两列）、day、other（不画）；left 是剩下的比例 0~1，读不到是 null（不是 0）；时刻都是秒 */
export interface QuotaWindow {
  kind: '5h' | '7d' | 'day' | 'other';
  label: string;
  left: number | null;
  resets_at?: number;
  checked_at?: number;
  ceiling?: number;
}
/** 一个号的一个模型池：agy 是 Gemini、Claude/GPT（key gemini、3p），别的来源一个号一个池（key、label 是空串） */
export interface QuotaPool {
  key: string;
  label: string;
  /** 这一池现在在用这个号（agy 一次只用一个号，每池各有一个） */
  active: boolean;
  state: QuotaState;
  available_at: number | null;
  windows: QuotaWindow[];
}
export interface QuotaAccount {
  /** 来源给的名字 */
  name: string;
  /** 这个号登录的邮箱（agy 从自己的日志读；唯一不脱敏的文字）；没有就显示 name */
  login?: string;
  state: QuotaState;
  available_at: number | null;
  pools: QuotaPool[];
}
export interface QuotaSource {
  id: string;
  kind: QuotaKind;
  title: string;
  /** 来源给的 https 链接，池主才有，页面上写“运维台↗” */
  link?: string;
  collected_at: number;
  ttl: number;
  /** 读数比这更早的数字变灰（秒） */
  stale_after: number;
  read_at: number;
  /** 上次读没成功，数字是更早的 */
  failing: boolean;
  usable: number;
  total: number;
  next_available_at: number | null;
  /** 整体见底：能用的号每个都有一个窗口剩不到 20%，或一个都不能用 */
  low: boolean;
  /** 一个能用的号都没有、有的会自己恢复 */
  dry: boolean;
  /** 页面默认展开（agy，或外接来源里有号要重新登录、用完） */
  expand: boolean;
  /** 要重新登录的号的显示名 */
  relogin: string[];
  accounts: QuotaAccount[];
}
export interface QuotaRefresh {
  /** 最近一次「刷新」开始的时刻，0 没点过 */
  at: number;
  busy: boolean;
  cooldown: number;
}
export interface QuotaData {
  /** 这条生成的时刻，不算进 rev */
  at: number;
  read_at: number;
  relogin: { source: string; name: string }[];
  sources: QuotaSource[];
  refresh: QuotaRefresh;
}
/** GET /api/quota：sync_state 里 name='quota' 的那一行；Covey 没推过是 null */
export interface QuotaResponse {
  rev: string;
  data: QuotaData;
  at: string;
}

/* ===== v43：记忆中心与仓库（docs/web.md 数据约定 v43）。全部可选，旧 Covey 不推新键时网页行为与 v42 相同 =====
 * v45 变化：memory:index 去掉 health、imports、scopes[].rules；不再推 memory:previews、memory:import:<页>；条目去掉 carried_next；
 * memory_edit 去掉 import、ignore；TurnNote.kind 多 superseded、pending。 */

/** v43：记忆中心的类型。键见 MEMORY_KEY_RE */
export const MEMORY_KINDS = ['rule', 'decision', 'note'] as const;
export type MemoryKind = (typeof MEMORY_KINDS)[number];
export const MEMORY_SCOPES = ['global', 'project', 'topic'] as const;
export type MemoryScope = (typeof MEMORY_SCOPES)[number];
export const MEMORY_STATUS = ['active', 'pending', 'done', 'archived', 'void'] as const;
export type MemoryStatus = (typeof MEMORY_STATUS)[number];
export const MEMORY_BY = ['owner', 'lead', 'butler', 'learn', 'import', 'bridge', 'web'] as const;
export type MemoryBy = (typeof MEMORY_BY)[number];
/** 待审核的类型：learn（推断全局、放行类）、conflict（回滚冲突）、migrate（旧版迁移）；旧 Covey 留下的别的类型按已过期算、不再推 */
export const MEMORY_REVIEW_TYPES = ['learn', 'conflict', 'migrate'] as const;
export type MemoryReviewType = (typeof MEMORY_REVIEW_TYPES)[number];

export interface MemoryEntryOrigin {
  at: number;
  topic_key?: string; topic_number?: number; quote?: string;
  message_id?: string; question?: string; file?: string; harness?: string;
}
export interface MemoryEntry {
  key: string;              // ref：global:<id> / project:<pid>:<id> / topic:<tid>:<id> / decision:<pid|tid>:<id>
  code: string;             // 短码：R/D/N + 4~6 位 base32
  kind: MemoryKind;
  scope: MemoryScope;
  project_key?: string;
  topic_key?: string;
  topic_number?: number;
  title: string;            // ≤40 字
  text: string;             // rule/note ≤200 字，decision ≤600 字
  why?: string;
  status: MemoryStatus;
  said: 'owner' | 'summary';
  by: MemoryBy;
  origin: MemoryEntryOrigin;
  repos?: string[];
  device?: string;
  verified_at?: number;
  over_cap?: boolean;       // 规矩超出所在范围的名额：下一轮只带标题
  superseded_by?: string;
  edited?: boolean;
  in_progress?: boolean;    // 决定：在办
  rev: string;              // 条目 rev，memory_edit 的 base 用它做乐观锁
}
/** migrate：pending 里“迁移时带过来的”还没处理的条数（Bridge 新增，旧的没有这个字段就当 0）；它们不算待办，不进侧栏角标 */
export interface MemoryCounts { rule: number; decision: number; note: number; pending: number; migrate?: number }
export interface MemoryScopeSummary {
  key: string;              // 'global' | 'topics' | 'p:<专项 id>'
  title: string;
  counts: MemoryCounts;
  rev: string;
}
export interface MemoryReviewItem {
  key: string;
  type: MemoryReviewType;
  title: string; text: string; quote?: string;
  from?: { topic_key?: string; topic_number?: number };
  suggest: { kind: MemoryKind; scope: MemoryScope; project_key?: string; grant?: string };  // grant：放行类，涉及要主人点头的哪类事
  conflict_with?: string;
  items?: MemoryEntry[];    // 推断全局：先在原处照做的那一条；迁移项：带过来的那一条
  flags?: ('vague' | 'dynamic' | 'repeat' | 'old_repo')[];
  created_at: number;
}
export interface MemoryLogItem {
  at: number; who: string; action: string; refs: string[]; title: string;
  because?: { topic_number?: number; quote?: string };
  undo_token?: string;
}
/** memory:index（小键） */
export interface MemoryIndex {
  on: boolean;
  version: number;          // v45 起是 45；老 Covey 还是 43
  counts: MemoryCounts;     // counts.pending 就是侧栏角标
  scopes: MemoryScopeSummary[];
  revs: Record<string, string>;   // 每个 memory:* 键 → rev；网页据此决定拉哪些键
  limits: { title: number; text: number; decision_text: number };
  /** ⌘K 搜记忆的精简标题表：[code, title, key, kind]，≤2000 条，标题已截到 40 字；kind 是后加的，旧 Covey 只有前三项 */
  titles: ([string, string, string] | [string, string, string, MemoryKind])[];
}
/** memory:global */
export interface MemoryGlobalData { key: 'global'; entries: MemoryEntry[]; review: MemoryReviewItem[]; log: MemoryLogItem[] }
/** memory:p:<专项 id> */
export interface MemoryProjectData { key: string; title: string; entries: MemoryEntry[] }
/** memory:topics */
export interface MemoryTopicsData {
  topics: { topic_key: string; topic_number?: number; title: string; project_key?: string; entries: MemoryEntry[] }[];
}
/** v43：memory_edit 的新 payload（带 v: 43；没有 v 的是 v37 的 MemoryEditPayload） */
export const MEMORY_EDIT_ACTIONS_V43 = [
  'add', 'edit', 'move', 'archive', 'restore', 'void', 'accept', 'reject', 'accept_all', 'reject_all',
  'keep_left', 'keep_right', 'keep_both', 'promote', 'undo',
] as const;
export interface MemoryEditPayloadV43 {
  v: 43;
  action: (typeof MEMORY_EDIT_ACTIONS_V43)[number];
  keys: string[];           // 条目 ref / 待审核 key；accept_all/reject_all 可为空数组（= 当前全部待审核）
  title?: string;           // ≤40
  text?: string;            // ≤200；kind=decision 时 ≤600
  why?: string;             // ≤200
  kind?: MemoryKind; scope?: MemoryScope; project_key?: string; topic_key?: string;
  repos?: string[];         // ≤10 个，每个 ≤64
  undo_token?: string;      // ≤64
  base?: string;            // 条目 rev（乐观锁，不是正文）
}

/** v43：话题带的仓库（话题条目 repos；workspace 保留只读，旧话题显示用） */
export interface TopicRepo { name: string; mode: 'read' | 'write'; worktree_tail?: string; branch?: string; rules_read: boolean }
/** v43：一轮带了几条记忆。used 是模型说用到的（code 可能是空串：旧 Bridge 只发标题），codes 是这一轮带入的全部短码；两个列表不按下标对应 */
export interface TurnMemoryUsed { code: string; title: string }
export interface TurnMemory { count: number; used: TurnMemoryUsed[]; codes: string[] }
export interface SettingsRepoItem {
  name: string; tail: string; note: string; status: 'active' | 'readonly' | 'frozen';
  write_mode: 'worktree' | 'inplace'; branch: string; rules: string[]; last_used?: number; live_topics: number;
  /** v46：git 是仓库，dir 是普通目录（原地改）；旧 Covey 不推，当 git */
  kind?: 'git' | 'dir';
  /** v46：在哪台设备上（devices.json 的别名）；本机是 ''，旧 Covey 不推 */
  device?: string;
}
export interface SettingsReposPage {
  home: { tail: string };
  items: SettingsRepoItem[];
  worktrees: { total: number; prunable: { repo: string; branch: string; tail: string; topic_number?: number; ended_at?: number }[] };
  /** v46：能在上面加路径的设备（devices.json 里有 ssh 的）；多设备没开是空 */
  devices?: { alias: string; title: string; access: 'write' | 'read' | '' }[];
}
// SettingsData.repos?: SettingsReposPage | SettingsError;（「工作区」页 workspace 保留给旧 Covey）
/** v43（WEB 包加）：GET /api/memory?key=<memory:* 键> 的响应，sync_state 里那一行（rev 是 Covey 给的内容哈希）；没推过是 null */
export interface MemoryKeyResponse<T = unknown> {
  rev: string;
  data: T;
  at: string;
}

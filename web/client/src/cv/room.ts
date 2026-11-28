/**
 * 大厅（T624）：你、管家和各专项的负责人在一条时间线上，和飞书私聊顶层一样。
 * 这里是纯函数：把 /api/room 的几路轮次排成一条线、补上专项的进出和等你回答的问题、算出每句话交给了谁、拼出发出去的话。
 */
import type { HandedTarget, OutboxView, SnapshotProject, SnapshotResponse, SnapshotTopic, TurnView } from '@shared/api.interface';

/** 成员栏和 @ 的一位：管家，或一个没结束的专项的负责人 */
export interface Member {
  key: string;
  kind: 'butler' | 'lead';
  name: string;
  /** 负责人的专项 id；管家为空 */
  project: string;
  /** 收起、停住的负责人灰着；@ 它会重新展开专项 */
  dim: boolean;
}

/** 引用：哪一条、谁说的、引了哪段 */
export interface Quote {
  key: string;
  /** 引用交给谁：发这条的话题（管家、负责人或普通话题） */
  topic: string;
  who: string;
  text: string;
}

/** 你说的这句交给了谁，结构化：经管家转给 to、直达 to，或由 integrator 汇总、helpers 协助 */
export interface RouteView {
  via?: 'butler';
  to: string[];
  integrator?: string;
  helpers?: string[];
}

/** 这句话交给谁 */
export interface Target {
  key: string;
  label: string;
}

/** 时间线上的一条：原来的轮次（带 topic_key），或大厅自己补的（专项进出、问题、引用转给话题的那句话） */
export type RoomTurn = TurnView & {
  topic_key: string;
  /** 排序用的时刻（秒） */
  room_at: number;
  /** 补出来的：project（专项进出）、question（等你回答）、sent（引用转给话题的那句话）；
   * v31：collab（一次协作的往返，cv/collab.ts 放进来）、receipt（话题页里协助方的回执） */
  synth?: 'project' | 'question' | 'sent' | 'collab' | 'receipt';
  /** question：问的是哪个话题；sent：交给的话题；collab、receipt：哪次协作 */
  ref?: string;
  /** 这一轮在它自己话题里的 seq（大厅从 1 重新编号，引用要原来的） */
  orig_seq?: number;
  /** receipt：收进这行回执的那几行（协助方答咨询的那一轮和回执行） */
  rows?: TurnView[];
  /** 你说的这句交给了谁（“→ 管家 → T12”） */
  route?: string;
  /** 同一句话的结构化写法，界面上画成头像（认不全时画上面的文字） */
  routeView?: RouteView;
  /** 你说的这句最后到了哪些话题、负责人（专项页按负责人筛） */
  targets?: string[];
};

const LIVE_PROJECT = new Set<string>(['在做', '等你', '停住', '收起']);
const GONE_PROJECT: Record<string, string> = { 做完: '做完了', 已取消: '降为普通话题了' };

/** 负责人在名字里的角色词 */
export const LEAD_ROLE = '负责人';

/** 负责人在界面上的名字：“<专项名> 负责人” */
export const leadName = (title: string): string => `${title || '专项'} ${LEAD_ROLE}`;

export function members(snap: SnapshotResponse | undefined): Member[] {
  if (!snap) return [];
  const out: Member[] = [];
  if (snap.butler_key) out.push({ key: snap.butler_key, kind: 'butler', name: '管家', project: '', dim: false });
  const projects: SnapshotProject[] = [...snap.projects].sort((a: SnapshotProject, b: SnapshotProject) => (a.started_at ?? 0) - (b.started_at ?? 0));
  for (const p of projects) {
    if (!p.lead_topic_key || !LIVE_PROJECT.has(p.status)) continue;
    out.push({ key: p.lead_topic_key, kind: 'lead', name: leadName(p.title), project: p.key, dim: p.status === '收起' || p.status === '停住' });
  }
  return out;
}

/** 一轮在时间线上的时刻：你说的按开始，回答按结束（在跑的按开始），发到私聊的话题结论按发出的时刻 */
export function turnAt(t: TurnView): number {
  if (t.relayed_at) return t.relayed_at;
  if (t.role === 'owner') return t.started_at ?? t.ended_at ?? 0;
  return t.ended_at || t.started_at || 0;
}

/** 交给了谁，写成一行：“→ 管家 → T12、夜航改版 负责人” */
export function routeWords(handed: HandedTarget[] | undefined): string {
  const names: string[] = (handed ?? []).map((h: HandedTarget) => (h.kind === 'lead' ? leadName(h.title) : h.number ? `T${h.number}` : h.title));
  return names.length ? `→ 管家 → ${names.join('、')}` : '→ 管家';
}

interface Build {
  turns: TurnView[];
  outbox: OutboxView[];
  snap: SnapshotResponse | undefined;
  butler: string | null;
}

/**
 * 排成一条线：
 * - 管家：全部轮次；你说的那句后面写交给了谁（取它后面紧跟的那条管家回答的 handed）。
 * - 负责人：它的回答；你直接对它说的（不经管家）也在，写“→ X 负责人”；经管家转来的那句和管家那边是同一句，不再画；回执行（role=event）不画。
 * - 普通话题：只有结论发到私聊的那一轮。
 * - 补上：专项建好（负责人加入）、专项做完或降级（负责人离开）、等你回答的问题、引用转给话题的那句话（发件箱里的）。
 * 时刻相同按话题内的 seq；最后从 1 重新编号（刻度、跳转、刚发出的气泡都按它）。
 */
export function buildRoom({ turns, outbox, snap, butler }: Build): RoomTurn[] {
  const leads = new Map<string, SnapshotProject>();
  for (const p of snap?.projects ?? []) if (p.lead_topic_key) leads.set(p.lead_topic_key, p);
  const topics = new Map<string, SnapshotTopic>((snap?.topics ?? []).map((t: SnapshotTopic) => [t.key, t]));
  const byTopic = new Map<string, TurnView[]>();
  for (const t of turns) {
    const k: string = t.topic_key ?? '';
    if (!byTopic.has(k)) byTopic.set(k, []);
    byTopic.get(k)!.push(t);
  }
  const out: RoomTurn[] = [];
  for (const [key, list] of byTopic) {
    list.sort((a: TurnView, b: TurnView) => a.seq - b.seq);
    const lead: SnapshotProject | undefined = leads.get(key);
    list.forEach((t: TurnView, i: number) => {
      const row: RoomTurn = { ...t, topic_key: key, room_at: turnAt(t), orig_seq: t.seq };
      if (key === butler) {
        if (t.role === 'owner') {
          const answer: TurnView | undefined = list.slice(i + 1).find((x: TurnView) => x.role !== 'owner');
          row.route = routeWords(answer?.handed);
          row.targets = (answer?.handed ?? []).map((h: HandedTarget) => h.key);
          row.routeView = { via: 'butler', to: row.targets };
        }
        out.push(row);
      } else if (lead) {
        if (t.role === 'event') return;
        if (t.role === 'owner') {
          if (t.via === 'butler' || (t.by && t.by !== 'owner')) return;
          row.route = `→ ${leadName(lead.title)}`;
          row.targets = [key];
          row.routeView = { to: [key] };
        }
        out.push(row);
      } else if (t.role !== 'owner' && t.relayed_at && topics.has(key)) {
        out.push(row);
      }
    });
  }
  for (const p of snap?.projects ?? []) {
    if (!p.lead_topic_key || !p.started_at) continue;
    out.push(synth(`pj-in-${p.key}`, p.lead_topic_key, p.started_at, 'project', `专项「${p.title}」建好了，${leadName(p.title)}加入`));
    const gone: string | undefined = GONE_PROJECT[p.status];
    if (gone && p.updated_at > p.started_at) {
      out.push(synth(`pj-out-${p.key}`, p.lead_topic_key, p.updated_at, 'project', `专项「${p.title}」${gone}，${leadName(p.title)}离开`));
    }
  }
  for (const q of snap?.questions ?? []) {
    if (q.state !== 'open' || !q.questions.length) continue;
    const t: SnapshotTopic | undefined = topics.get(q.topic_key);
    if (!t) continue;
    const head: string = q.questions[0].question || q.questions[0].header || '';
    const more: string = q.questions.length > 1 ? `（共 ${q.questions.length} 题）` : '';
    const row: RoomTurn = synth(`q-${q.token}`, q.topic_key, q.asked_at ?? t.updated_at, 'question', `${head}${more}`);
    row.ref = q.topic_key;
    out.push(row);
  }
  const echoed = new Set<string>(turns.filter((t: TurnView) => t.role === 'owner').map((t: TurnView) => t.body.trim()));
  const who = (k: string): string => {
    if (k === butler) return '管家';
    const p: SnapshotProject | undefined = leads.get(k);
    if (p) return leadName(p.title);
    const t: SnapshotTopic | undefined = topics.get(k);
    return t?.number ? `T${t.number}` : t?.title ?? '话题';
  };
  for (const o of outbox) {
    // 交给管家、负责人的话会作为它们的一轮回来；交给普通话题的（引用了它的结论）只在话题里，这里补一行
    if (o.kind !== 'say' || o.state !== 'done' || !o.topic_key || o.topic_key === butler || leads.has(o.topic_key)) continue;
    const text: string = typeof o.payload?.text === 'string' ? o.payload.text : '';
    if (!text.trim() || echoed.has(text.trim())) continue;
    const t: SnapshotTopic | undefined = topics.get(o.topic_key);
    const at: number = Date.parse(o.created_at) / 1000 || 0;
    const row: RoomTurn = { ...synth(`ob-${o.op_id}`, o.topic_key, at, 'sent', text), role: 'owner' };
    row.ref = o.topic_key;
    row.route = `→ ${t?.number ? `T${t.number}` : t?.title ?? '话题'}`;
    row.targets = [o.topic_key];
    row.routeView = { to: [o.topic_key] };
    // v31：点了几位的那句（payload.to）：写谁汇总、谁协助（交给的那位就是汇总方）
    const to: string[] = Array.isArray(o.payload?.to) ? (o.payload.to as unknown[]).filter((k): k is string => typeof k === 'string') : [];
    const helpers: string[] = to.filter((k: string) => k !== o.topic_key);
    if (helpers.length) {
      row.route = `→ ${who(o.topic_key)} 汇总 · 协助 ${helpers.map(who).join('、')}`;
      row.targets = [o.topic_key, ...helpers];
      row.routeView = { to: [], integrator: o.topic_key, helpers };
    }
    out.push(row);
  }
  out.sort((a: RoomTurn, b: RoomTurn) => a.room_at - b.room_at || (a.topic_key === b.topic_key ? a.seq - b.seq : 0));
  return out.map((t: RoomTurn, i: number) => ({ ...t, seq: i + 1 }));
}

/** 专项页（T624）：大厅里和这位负责人有关的那部分——它说的、你对它说的（直接说或经管家转来）、专项里话题发到私聊的结论和问题、专项的进出 */
export function onlyLead(rows: RoomTurn[], lead: string, project: string, snap: SnapshotResponse | undefined, butler: string | null): RoomTurn[] {
  const mine = new Set<string>((snap?.topics ?? []).filter((t: SnapshotTopic) => t.project_key === project && t.kind === 'topic').map((t: SnapshotTopic) => t.key));
  return rows.filter((t: RoomTurn) => {
    if (t.synth === 'collab') return !!t.targets?.includes(lead) || mine.has(t.topic_key);
    if (t.topic_key === lead) return true;
    if (t.topic_key === butler) return t.role === 'owner' && !!t.targets?.includes(lead);
    if (t.synth === 'sent') return mine.has(t.ref ?? '');
    return mine.has(t.topic_key);
  }).map((t: RoomTurn, i: number) => ({ ...t, seq: i + 1 }));
}

/**
 * 专项页的负责人对话（T952）：负责人自己在等你答的题，和话题页上同一道题一样画——顶上“等你 · 要你定”、底下问题面板、输入框就是回答。
 * 题和它的发件行（交过回答的锁住）都从负责人自己的对话（`own`，/api/topics/<负责人>）读，发件行再并上大厅的；
 * 大厅不是哪一个话题的，没有这道题（大厅里的题是一行“等你”，点了去那个话题答）。以前专项页也一律没有，题只在从看板点进负责人时才看得到。
 */
export function roomQuestion<Q>(lead: boolean, own: { question?: Q | null; outbox?: OutboxView[] } | undefined, roomOutbox: OutboxView[]): { question: Q | null; ops: OutboxView[] } {
  if (!lead) return { question: null, ops: roomOutbox };
  const mine: OutboxView[] = own?.outbox ?? [];
  const seen = new Set<string>(mine.map((o: OutboxView) => o.op_id));
  return { question: own?.question ?? null, ops: [...mine, ...roomOutbox.filter((o: OutboxView) => !seen.has(o.op_id))] };
}

function synth(key: string, topic: string, at: number, kind: 'project' | 'question' | 'sent', body: string): RoomTurn {
  return {
    key, seq: 0, role: kind === 'sent' ? 'owner' : 'event', status: 'done', body, job_id: null,
    started_at: at, ended_at: at, artifacts: [], topic_key: topic, room_at: at, synth: kind,
  };
}

/** 说话的是谁，头像跟飞书私聊区一样（T645）：管家一张固定图；负责人用它专项的固定头像（`avatar`，旧数据没有）；话题用自己的 T 号（`number`） */
export interface Speaker {
  name: string;
  kind: 'butler' | 'lead' | 'topic';
  /** 负责人：专项的固定头像名（Covey v30） */
  avatar?: string;
  /** 话题：T 号 */
  number?: number | null;
  /** 话题：标题（T 号在头像上，名字只写标题） */
  title?: string;
}

/** 一条回答是谁说的：管家、“X 负责人”，或“T12 标题” */
export function speaker(t: RoomTurn, snap: SnapshotResponse | undefined, butler: string | null): Speaker {
  if (t.topic_key === butler) return { name: '管家', kind: 'butler' };
  const p: SnapshotProject | undefined = snap?.projects.find((x: SnapshotProject) => x.lead_topic_key === t.topic_key);
  if (p) return { name: leadName(p.title), kind: 'lead', ...(p.avatar ? { avatar: p.avatar } : {}) };
  const topic: SnapshotTopic | undefined = snap?.topics.find((x: SnapshotTopic) => x.key === t.topic_key);
  return topic
    ? { name: `${topic.number ? `T${topic.number} ` : ''}${topic.title}`, kind: 'topic', number: topic.number, title: topic.title }
    : { name: '话题', kind: 'topic' };
}

/** 编辑器里光标前那段字的胶囊占位（胶囊不是“@ 前面的字母数字”，也不是查询词的一部分） */
export const OBJ = '\uFFFC';

/**
 * 光标前正在打的 @：返回 @ 后面的字和 @ 的位置；不在打就是 null。
 * 中文后紧挨着也算（“请@T5”），字母数字、下划线后面的不算（邮箱 a@b）。
 */
export function mentionAt(text: string, caret: number): { query: string; start: number } | null {
  const before: string = text.slice(0, caret);
  const m: RegExpMatchArray | null = before.match(/(?:^|[^A-Za-z0-9_])@([^\s@\uFFFC]{0,20})$/u);
  if (!m) return null;
  return { query: m[1], start: caret - m[1].length - 1 };
}

/** @ 候选：名字里有打的字（不分大小写）的成员 */
export function mentionMatches(list: Member[], query: string): Member[] {
  const q: string = query.trim().toLowerCase();
  return q ? list.filter((m: Member) => m.name.toLowerCase().includes(q)) : list;
}

const QUOTE_CHARS = 300;

/** 引用的那段：去掉多余空行，太长截断 */
export function quoteText(text: string): string {
  const t: string = text.replace(/\r/gu, '').replace(/\n{3,}/gu, '\n\n').trim();
  return t.length > QUOTE_CHARS ? `${t.slice(0, QUOTE_CHARS)}…` : t;
}

/** 发出去的正文：有引用时在前面放一段 Markdown 引用（“> 引用 T12 标题：”），和飞书里引用一条回复一样交给模型 */
export function withQuote(text: string, quote: Quote | null): string {
  const body: string = text.trim();
  if (!quote) return body;
  const lines: string[] = quoteText(quote.text).split('\n').map((l: string) => `> ${l}`);
  return `> 引用 ${quote.who}：\n${lines.join('\n')}\n\n${body}`;
}

/** 这句话交给谁：@ 的那位优先，其次是引用的那条的话题，都没有就是管家 */
export function targetOf(to: Member | null, quote: Quote | null, butler: string | null, snap: SnapshotResponse | undefined): Target | null {
  if (to) return { key: to.key, label: to.name };
  if (quote) {
    const p: SnapshotProject | undefined = snap?.projects.find((x: SnapshotProject) => x.lead_topic_key === quote.topic);
    if (quote.topic === butler) return { key: quote.topic, label: '管家' };
    if (p) return { key: quote.topic, label: leadName(p.title) };
    const t: SnapshotTopic | undefined = snap?.topics.find((x: SnapshotTopic) => x.key === quote.topic);
    return { key: quote.topic, label: t?.number ? `T${t.number}` : t?.title ?? '话题' };
  }
  return butler ? { key: butler, label: '管家' } : null;
}

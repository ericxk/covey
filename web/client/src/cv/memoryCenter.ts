/**
 * 记忆页（数据约定 v45，MemoryCenter.tsx，一页式）的纯函数：拉哪些键、条目按“管哪儿”分组（全局、各专项、话题）、组里的小节与排序、
 * 超额提示、搜索（规矩、资料、你答过的决定）、?code= 和 ?scope= 定位、待审核每条的说明、15 个 memory_edit 动作的 payload
 * （带 v: 43，动一条时带 base = 条目 rev 做乐观锁）、⌘K 用 memory:index.titles 秒搜。不碰 DOM、不引别的模块（只有类型）：
 * 单测 scripts/memory-center.test.mjs。
 *
 * v45 做减法：没有页签、范围下拉、体检、配额条、“下一轮带什么”、导入；决定不在分组里列，只在搜索里以“上次你答过”出现。
 */
import type {
  MemoryEditPayloadV43,
  MemoryEntry,
  MemoryCounts,
  MemoryGlobalData,
  MemoryIndex,
  MemoryKind,
  MemoryLogItem,
  MemoryProjectData,
  MemoryReviewItem,
  MemoryScope,
  MemoryTopicsData,
} from '@shared/api.interface';

export const KIND_WORD: Record<MemoryKind, string> = { rule: '规矩', decision: '决定', note: '资料' };

/** 要主人处理的待审核数：pending 里去掉迁移项（旧 Bridge 没有 counts.migrate 就当 0）。侧栏角标、待审核块的数都用它 */
export const todoPending = (c: Pick<MemoryCounts, 'pending' | 'migrate'> | null | undefined): number => {
  const n: number = Math.max(0, Number(c?.pending) || 0);
  const m: number = typeof c?.migrate === 'number' && Number.isFinite(c.migrate) ? Math.max(0, c.migrate) : 0;
  return Math.max(0, n - m);
};

/** 记忆页的 memory:index（形状不对、版本比 43 还老的一律当没有；v45 的 version 是 45，老 Covey 是 43） */
export function isMemoryIndex(d: unknown): d is MemoryIndex {
  const o = d as Partial<MemoryIndex> | null;
  return !!o && typeof o === 'object' && typeof o.version === 'number' && o.version >= 43 && !!o.counts && Array.isArray(o.scopes) && !!o.revs && typeof o.revs === 'object';
}

/* ---------- 拉哪些键 ---------- */
export interface KeyRev {
  key: string;
  rev: string;
}
const scopeTotal = (c: Pick<MemoryCounts, 'rule' | 'decision' | 'note'> | undefined): number => (c ? (c.rule || 0) + (c.decision || 0) + (c.note || 0) : 1);
/**
 * 页面要的数据键（按 index.revs 的 rev 取，没变就用缓存）：全局、话题汇总，和有规矩、决定或资料的专项（要画每组的计数、搜决定）；
 * 没有任何在用条目的专项不取。v45 起不再推 memory:previews、memory:import:<页>，这里也不取。
 */
export function keysToLoad(idx: Pick<MemoryIndex, 'revs'> & Partial<Pick<MemoryIndex, 'scopes'>> | null | undefined): KeyRev[] {
  if (!idx) return [];
  const out: KeyRev[] = [];
  for (const [key, rev] of Object.entries(idx.revs)) {
    if (!rev || key === 'memory:index') continue;
    if (key === 'memory:global' || key === 'memory:topics') out.push({ key, rev });
    else if (key.startsWith('memory:p:')) {
      const s = idx.scopes?.find((x) => x.key === `p:${key.slice('memory:p:'.length)}`);
      if (scopeTotal(s?.counts) > 0) out.push({ key, rev });
    }
  }
  return out.sort((a: KeyRev, b: KeyRev) => a.key.localeCompare(b.key));
}
export const projectKeyOf = (key: string): string => (key.startsWith('memory:p:') ? key.slice('memory:p:'.length) : '');

/* ---------- 合成一池 ---------- */
export interface Pool {
  entries: MemoryEntry[];
  review: MemoryReviewItem[];
  log: MemoryLogItem[];
  /** 专项 id → 专项名 */
  projects: Record<string, string>;
  /** 话题 key → T 号和标题 */
  topics: Record<string, { number?: number; title: string }>;
}
export function poolOf(global: MemoryGlobalData | null | undefined, topics: MemoryTopicsData | null | undefined, projects: (MemoryProjectData | null | undefined)[]): Pool {
  const entries: MemoryEntry[] = [...(global?.entries ?? [])];
  const pnames: Record<string, string> = {};
  for (const p of projects) {
    if (!p) continue;
    const id: string = p.key.startsWith('p:') ? p.key.slice(2) : p.key;
    pnames[id] = p.title;
    for (const e of p.entries ?? []) entries.push(e.project_key ? e : { ...e, project_key: id });
  }
  const tnames: Pool['topics'] = {};
  for (const t of topics?.topics ?? []) {
    tnames[t.topic_key] = { number: t.topic_number, title: t.title };
    for (const e of t.entries ?? []) entries.push(e.topic_key ? e : { ...e, topic_key: t.topic_key, topic_number: e.topic_number ?? t.topic_number });
  }
  // 同一条可能出现在两处（专项里的话题条目），按 key 去重，先来的留着
  const seen = new Set<string>();
  const uniq: MemoryEntry[] = entries.filter((e: MemoryEntry) => (seen.has(e.key) ? false : (seen.add(e.key), true)));
  return { entries: uniq, review: global?.review ?? [], log: global?.log ?? [], projects: pnames, topics: tnames };
}

/* ---------- 管哪儿 ---------- */
/** 'global' 全局、'topics' 话题、'p:<专项 id>' 某个专项 */
export const scopeOfEntry = (e: Pick<MemoryEntry, 'scope' | 'project_key'>): string =>
  e.scope === 'global' ? 'global' : e.scope === 'topic' ? 'topics' : `p:${e.project_key ?? ''}`;
/** 待审核按建议的去处归范围 */
export const reviewScope = (r: MemoryReviewItem): string =>
  r.suggest.scope === 'global' ? 'global' : r.suggest.scope === 'topic' ? 'topics' : `p:${r.suggest.project_key ?? ''}`;

const atOf = (e: Pick<MemoryEntry, 'origin'>): number => (typeof e.origin?.at === 'number' ? e.origin.at : 0);
const byNew = (a: MemoryEntry, b: MemoryEntry): number => atOf(b) - atOf(a) || a.code.localeCompare(b.code);
const tno = (n: number | undefined): string => (typeof n === 'number' && n > 0 ? `T${n}` : '');
export const mmdd = (sec: number): string =>
  new Date(sec * 1000).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit' }).replace(/\//gu, '-');

/** 在用（规矩、资料：active）和已收起（归档、作废；决定不算） */
const inUse = (e: Pick<MemoryEntry, 'status'>): boolean => e.status === 'active';
export const isShelved = (e: Pick<MemoryEntry, 'kind' | 'status'>): boolean => e.kind !== 'decision' && (e.status === 'archived' || e.status === 'void');

/* ---------- 每行的小标签 ---------- */
export const BY_WORD: Record<string, string> = {
  owner: '你定的', lead: '负责人记的', butler: '管家记的', learn: '自动学的', import: '导入的', bridge: 'Covey 记的', web: '你定的',
};
/** 谁定的：主人原话是“你定的”，别的写谁记的 */
export const saidWord = (e: Pick<MemoryEntry, 'said' | 'by'>): string => (e.said === 'owner' ? '你定的' : BY_WORD[e.by] ?? '它总结的');
/** 条目在哪的说法：全局、专项名、T12 */
export function placeWord(e: Pick<MemoryEntry, 'scope' | 'project_key' | 'topic_number' | 'topic_key'>, pool: Pick<Pool, 'projects' | 'topics'>): string {
  if (e.scope === 'global') return '全局';
  if (e.scope === 'project') return pool.projects[e.project_key ?? ''] || '专项';
  return tno(e.topic_number ?? pool.topics[e.topic_key ?? '']?.number) || '话题';
}
/** 搜索结果里写它属于哪一组：全局、专项「名」、话题 T12 */
export function groupWord(e: Pick<MemoryEntry, 'scope' | 'project_key' | 'topic_number' | 'topic_key'>, pool: Pick<Pool, 'projects' | 'topics'>): string {
  if (e.scope === 'global') return '全局';
  if (e.scope === 'project') return `专项「${pool.projects[e.project_key ?? ''] || '专项'}」`;
  const t: string = tno(e.topic_number ?? pool.topics[e.topic_key ?? '']?.number);
  return t ? `话题 ${t}` : '话题';
}
/** 一行行尾的小标签：谁定的（小节标题已经说过的不重复）、话题组里写 T 号 */
export function rowTags(e: MemoryEntry, o: { said: boolean; pool: Pick<Pool, 'projects' | 'topics'> }): string[] {
  const out: string[] = [];
  if (o.said) out.push(saidWord(e));
  if (e.scope === 'topic') {
    const t: string = tno(e.topic_number ?? o.pool.topics[e.topic_key ?? '']?.number);
    if (t) out.push(t);
  }
  return out;
}

/* ---------- 分组 ---------- */
export interface MemSection {
  key: string;
  title: string;
  rows: MemoryEntry[];
  /** 已收起：行里多一个「放回」 */
  shelf?: boolean;
  /** 行尾要不要写谁定的（小节标题已经说了谁就不写） */
  said: boolean;
}
export interface MemGroup {
  /** 'global' | 'topics' | 'p:<专项 id>' */
  key: string;
  kind: 'global' | 'topics' | 'project';
  /** 全局、话题、专项名 */
  name: string;
  /** 组标题 */
  title: string;
  /** 在用规矩数 */
  total: number;
  owner: number;
  others: number;
  /** 超额：只带标题的在用规矩数 */
  over: number;
  /** 你定的规矩，直接列 */
  rules: MemoryEntry[];
  sections: MemSection[];
}
/** 组标题后面的灰字：“N 条只带标题”，0 不写 */
export const overLine = (n: number): string => (n > 0 ? `${n} 条只带标题` : '');
export function groupTitle(g: Pick<MemGroup, 'kind' | 'name' | 'total' | 'owner' | 'others'>): string {
  if (g.kind === 'global') return `全局 · ${g.total} 条 · 你的每一轮都带`;
  if (g.kind === 'topics') return `话题 · ${g.total} 条`;
  return `专项「${g.name}」 · ${[g.owner ? `你定的 ${g.owner} 条` : '', g.others ? `负责人记的 ${g.others} 条` : ''].filter(Boolean).join(' · ')}`;
}

function build(key: string, kind: MemGroup['kind'], name: string, mine: MemoryEntry[]): MemGroup | null {
  const rules: MemoryEntry[] = mine.filter((e) => e.kind === 'rule' && inUse(e)).sort(byNew);
  const notes: MemoryEntry[] = mine.filter((e) => e.kind === 'note' && inUse(e)).sort(byNew);
  const shelf: MemoryEntry[] = mine.filter(isShelved).sort(byNew);
  // 专项组：没有在用规矩就不出现；话题组同理；全局只要有东西就出现
  if (kind !== 'global' && !rules.length) return null;
  if (kind === 'global' && !rules.length && !notes.length && !shelf.length) return null;
  const owner: MemoryEntry[] = rules.filter((e) => e.said === 'owner');
  const others: MemoryEntry[] = rules.filter((e) => e.said !== 'owner');
  const sections: MemSection[] = [];
  if (kind === 'project') {
    if (others.length) sections.push({ key: 'others', title: `负责人记的 ${others.length} 条 · 只在这个专项照做`, rows: others, said: false });
  } else {
    const by = new Map<string, MemoryEntry[]>();
    for (const e of others) by.set(saidWord(e), [...(by.get(saidWord(e)) ?? []), e]);
    for (const [w, rows] of [...by.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))) {
      sections.push({ key: `others:${w}`, title: `${w} ${rows.length} 条`, rows, said: false });
    }
  }
  if (notes.length) sections.push({ key: 'notes', title: `资料 ${notes.length} 条（相关时才带）`, rows: notes, said: true });
  if (shelf.length) sections.push({ key: 'shelved', title: `已收起 ${shelf.length} 条`, rows: shelf, shelf: true, said: true });
  const g: MemGroup = { key, kind, name, title: '', total: rules.length, owner: owner.length, others: others.length, over: rules.filter((e) => !!e.over_cap).length, rules: owner, sections };
  g.title = groupTitle(g);
  return g;
}

/**
 * 把条目分成组，顺序：全局、各专项（最近活跃的在前）、话题。
 * order 是 index.scopes 里专项 id 的顺序，updated 是专项最近活跃时刻（快照里专项的 updated_at，取不到的不写）；
 * 有时刻的按时刻新到旧，没有的按 order 排在后面。
 */
export function buildGroups(pool: Pool, order: string[] = [], updated: Record<string, number> = {}): MemGroup[] {
  const out: MemGroup[] = [];
  const g0 = build('global', 'global', '全局', pool.entries.filter((e) => e.scope === 'global'));
  if (g0) out.push(g0);
  const ids: string[] = [...order];
  for (const e of pool.entries) if (e.scope === 'project' && e.project_key && !ids.includes(e.project_key)) ids.push(e.project_key);
  const pos = new Map<string, number>(ids.map((id, i) => [id, i]));
  const ranked: string[] = [...ids].sort((a, b) => (updated[b] ?? -1) - (updated[a] ?? -1) || (pos.get(a) ?? 0) - (pos.get(b) ?? 0));
  for (const id of ranked) {
    const g = build(`p:${id}`, 'project', pool.projects[id] || '专项', pool.entries.filter((e) => e.scope === 'project' && e.project_key === id));
    if (g) out.push(g);
  }
  const gt = build('topics', 'topics', '话题', pool.entries.filter((e) => e.scope === 'topic'));
  if (gt) out.push(gt);
  return out;
}
/** 默认展开哪些组：全局，和最近活跃的两个专项；话题组默认折叠 */
export function defaultOpen(groups: MemGroup[]): Set<string> {
  const open = new Set<string>();
  let projects = 0;
  for (const g of groups) {
    if (g.kind === 'global') open.add(g.key);
    else if (g.kind === 'project' && projects < 2) {
      open.add(g.key);
      projects += 1;
    }
  }
  return open;
}
/** 快照里专项的最近活跃时刻（专项 id → updated_at） */
export function updatedOf(projects: { key: string; updated_at?: number }[] | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of projects ?? []) if (typeof p.updated_at === 'number' && p.updated_at > 0) out[p.key] = p.updated_at;
  return out;
}

/* ---------- 链接进来：/memory?code=…&scope=…、/memory/review、?view=log ---------- */
export interface MemoryLink {
  code: string;
  scope: string;
  review: boolean;
  log: boolean;
}
export const SCOPE_RE = /^(global|topics|p:[0-9a-z]{1,32})$/u;
/** tab 是旧路径 /memory/<页签> 的那一段（review 滚到待审核块，log 打开记录，别的照样画这一页）；query 是地址栏的参数 */
export function parseLink(tab: string | undefined, query: { get: (k: string) => string | null }): MemoryLink {
  const scope: string = query.get('scope') ?? '';
  return {
    code: (query.get('code') ?? '').trim().slice(0, 32),
    scope: SCOPE_RE.test(scope) ? scope : '',
    review: tab === 'review',
    log: tab === 'log' || query.get('view') === 'log',
  };
}
export interface Located {
  entry: MemoryEntry;
  /** 它所在的组（决定不在组里：空串） */
  group: string;
  /** 它所在的小节（你定的规矩直接列：空串） */
  section: string;
  /** 决定：当作一条展开的搜索结果显示 */
  decision: boolean;
}
export function locate(pool: Pick<Pool, 'entries'>, groups: MemGroup[], code: string): Located | null {
  const c: string = code.toLowerCase();
  const entry: MemoryEntry | undefined = c ? pool.entries.find((x: MemoryEntry) => x.code.toLowerCase() === c) : undefined;
  if (!entry) return null;
  if (entry.kind === 'decision') return { entry, group: '', section: '', decision: true };
  for (const g of groups) {
    if (g.rules.some((e) => e.key === entry.key)) return { entry, group: g.key, section: '', decision: false };
    const s: MemSection | undefined = g.sections.find((x) => x.rows.some((e) => e.key === entry.key));
    if (s) return { entry, group: g.key, section: s.key, decision: false };
  }
  return { entry, group: '', section: '', decision: false };
}

/* ---------- 搜索 ---------- */
export interface SearchHit {
  key: string;
  code: string;
  title: string;
  kind: MemoryKind;
  /** 已取到的条目；只在标题表里的没有 */
  entry?: MemoryEntry;
  /** 灰字：规矩、资料写属于哪一组；决定写“上次你答过：…（T12 · 11-26）” */
  line: string;
}
/** 短码的第一个字母：R 规矩、D 决定、N 资料 */
export const kindOfCode = (code: string): MemoryKind => ({ R: 'rule', D: 'decision', N: 'note' } as Record<string, MemoryKind>)[code.charAt(0).toUpperCase()] ?? 'rule';
const clip = (s: string, n: number): string => (Array.from(s).length > n ? `${Array.from(s).slice(0, n).join('')}…` : s);
/** 决定的搜索结果：上次你答过：<正文或标题>（T12 · 11-26）；T 号取 origin.topic_number 或 topic_number，日期取 origin.at */
export function decisionLine(e: Pick<MemoryEntry, 'text' | 'title' | 'origin' | 'topic_number'>): string {
  const from: string = [tno(e.origin?.topic_number ?? e.topic_number), e.origin?.at ? mmdd(e.origin.at) : ''].filter(Boolean).join(' · ');
  return `上次你答过：${clip((e.text || e.title || '').replace(/\s+/gu, ' ').trim(), 60)}${from ? `（${from}）` : ''}`;
}
/**
 * 搜索：在已取到的规矩、资料、决定里按标题、正文、短码筛（不分大小写，标题命中的在前，再按新旧），
 * 再用 memory:index.titles 补没取到的（只有标题和短码）。已收起的规矩和资料也能搜到，灰字里标明。决定只收在办和办完的，作废、归档的不算。
 */
export function searchMemory(pool: Pool, titles: MemoryIndex['titles'] | null | undefined, query: string, n = 60): SearchHit[] {
  const q: string = query.trim().toLowerCase();
  if (!q) return [];
  const seen = new Set<string>();
  const scored: [number, MemoryEntry][] = [];
  for (const e of pool.entries) {
    if (e.kind === 'decision' ? e.status !== 'active' && e.status !== 'done' : !inUse(e) && !isShelved(e)) continue;
    const t: boolean = e.title.toLowerCase().includes(q) || e.code.toLowerCase() === q;
    if (!t && !`${e.text}\n${e.code}`.toLowerCase().includes(q)) continue;
    scored.push([t ? 0 : 1, e]);
  }
  scored.sort((a, b) => a[0] - b[0] || byNew(a[1], b[1]));
  const out: SearchHit[] = [];
  for (const [, e] of scored) {
    seen.add(e.code);
    out.push({
      key: e.key, code: e.code, title: e.title, kind: e.kind, entry: e,
      line: e.kind === 'decision' ? decisionLine(e) : `${KIND_WORD[e.kind]} · ${groupWord(e, pool)}${isShelved(e) ? ' · 已收起' : ''}`,
    });
  }
  for (const h of searchTitles(titles, query, n)) {
    if (seen.has(h.code)) continue;
    const kind: MemoryKind = kindOfCode(h.code);
    out.push({ key: h.key, code: h.code, title: h.title, kind, line: KIND_WORD[kind] });
  }
  return out.slice(0, n);
}
/** ?code= 是决定：当作一条展开的搜索结果 */
export function decisionHit(pool: Pool, code: string): SearchHit | null {
  const l: Located | null = locate(pool, [], code);
  return l && l.decision ? { key: l.entry.key, code: l.entry.code, title: l.entry.title, kind: 'decision', entry: l.entry, line: decisionLine(l.entry) } : null;
}

/* ---------- 待审核 ---------- */
/** 待审核分成两块：要处理的（推断全局、放行类、矛盾），和迁移项（type === 'migrate'，负责人约束在按规矩带，不算待办）；迁移项按去处折成组 */
export interface ReviewGroup { key: string; label: string; rows: MemoryReviewItem[] }
export function reviewSplit(rows: MemoryReviewItem[], pool: Pick<Pool, 'projects'>): { todo: MemoryReviewItem[]; groups: ReviewGroup[] } {
  const todo: MemoryReviewItem[] = [];
  const by = new Map<string, ReviewGroup>();
  for (const r of rows) {
    if (r.type !== 'migrate') {
      todo.push(r);
      continue;
    }
    const key: string = reviewScope(r);
    const label: string = key === 'global' ? '全局' : key === 'topics' ? '话题' : pool.projects[r.suggest.project_key ?? ''] || '专项';
    const g: ReviewGroup = by.get(key) ?? { key, label, rows: [] };
    g.rows.push(r);
    by.set(key, g);
  }
  return { todo: todo.sort((a, b) => b.created_at - a.created_at), groups: [...by.values()].sort((a: ReviewGroup, b: ReviewGroup) => b.rows.length - a.rows.length || a.label.localeCompare(b.label)) };
}
/** 组头一行 */
export const groupLine = (g: Pick<ReviewGroup, 'label' | 'rows'>): string => `${g.label}：${g.rows.length} 条负责人约束在按规矩带`;
/** 标题是正文开头自动截出来的（可能带省略号）就不再单独画标题，只画正文 */
export function titleShown(r: Pick<MemoryReviewItem, 'title' | 'text'>): boolean {
  const t: string = (r.title ?? '').replace(/(?:…|\.{3})$/u, '').trim();
  if (!t) return false;
  const x: string = (r.text ?? '').trim();
  return !(x && (x === t || x.startsWith(t)));
}
/**
 * 待审核每条的一行灰字：它是哪一种 · 点「采纳」「不要」各会怎样（「不要」都是保持原样）。和飞书设置卡「记忆」同一张表
 * （covey/cards/memory_panel.py WIDER_WORDS、GRANT_WORDS、REVIEW_WORDS）：放行类（suggest.grant）和先在原处照做的推断全局（带 items）
 * 各一句，其余按 type 和 suggest.scope 取，取不到用 REVIEW_OTHER。
 */
export const WIDER_WORDS = '推断要管全局 · 现在先在说的地方照做；采纳就挪到全局，不要就留在原处';
export const GRANT_WORDS = '涉及要你点头的事 · 采纳后才生效，能放行它写明的那类事；不要就不记';
export const REVIEW_WORDS: Record<string, string> = {
  'learn:global': '推断要管全局 · 还没生效；采纳后每一轮都照做，不要就不记',
  learn: '自动学到的 · 还没生效；采纳后在这里照做，不要就不记',
  'migrate:project': '迁移带过来的负责人约束 · 采纳算你定的，不要就照旧是负责人记的',
  migrate: '旧版工作区里的一条 · 采纳后管全局，不要就不带过来',
  conflict: '和已有的一条说法不同 · 采纳换成新的说法，不要就留原来的',
};
export const REVIEW_OTHER = '采纳后生效，不要就保持原样';
export function reviewLine(r: Pick<MemoryReviewItem, 'type' | 'suggest' | 'items'>, _pool?: Pick<Pool, 'projects'>): string {
  if (r.type === 'learn' && r.suggest?.grant) return GRANT_WORDS;
  if (r.type === 'learn' && r.items?.length) return WIDER_WORDS;
  return REVIEW_WORDS[`${r.type}:${r.suggest?.scope ?? ''}`] ?? REVIEW_WORDS[r.type] ?? REVIEW_OTHER;
}
export const FLAG_WORD: Record<string, string> = { vague: '过后可能读不懂', dynamic: '写的是会变的现状', repeat: '说过不止一次', old_repo: '旧仓库' };

/* ---------- 15 个动作 ---------- */
type Extra = Omit<MemoryEditPayloadV43, 'v' | 'action' | 'keys'>;
const clean = (p: MemoryEditPayloadV43): MemoryEditPayloadV43 => {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== '') out[k] = v;
  if (!('keys' in out)) out.keys = [];
  return out as unknown as MemoryEditPayloadV43;
};
export const op = (action: MemoryEditPayloadV43['action'], keys: string[], extra: Extra = {}): MemoryEditPayloadV43 => clean({ v: 43, action, keys, ...extra });
/** 去处：范围和它的专项或话题 */
export interface Place {
  scope: MemoryScope;
  project_key?: string;
  topic_key?: string;
}
const placeOf = (p: Place): Extra => ({ scope: p.scope, ...(p.scope === 'project' ? { project_key: p.project_key } : {}), ...(p.scope === 'topic' ? { topic_key: p.topic_key } : {}) });

export const addOp = (d: { title: string; text: string; kind: MemoryKind; why?: string } & Place): MemoryEditPayloadV43 =>
  op('add', [], { title: d.title.trim(), text: d.text.trim(), kind: d.kind, why: d.why?.trim(), ...placeOf(d) });
/** 改：带 kind（决定正文能到 600 字，服务端按它数）和 base（乐观锁） */
export const editOp = (e: Pick<MemoryEntry, 'key' | 'kind' | 'rev'>, d: { title: string; text: string; why?: string }): MemoryEditPayloadV43 =>
  op('edit', [e.key], { title: d.title.trim(), text: d.text.trim(), why: d.why?.trim(), kind: e.kind, base: e.rev });
export const moveOp = (e: Pick<MemoryEntry, 'key' | 'rev'>, to: Place): MemoryEditPayloadV43 => op('move', [e.key], { ...placeOf(to), base: e.rev });
export const archiveOp = (e: Pick<MemoryEntry, 'key' | 'rev'>): MemoryEditPayloadV43 => op('archive', [e.key], { base: e.rev });
export const restoreOp = (e: Pick<MemoryEntry, 'key' | 'rev'>): MemoryEditPayloadV43 => op('restore', [e.key], { base: e.rev });
export const voidOp = (e: Pick<MemoryEntry, 'key' | 'rev'>, why?: string): MemoryEditPayloadV43 => op('void', [e.key], { why, base: e.rev });
/** 提到更大的范围：话题 → 它的专项（没有专项就全局），专项 → 全局 */
export function promoteTarget(e: Pick<MemoryEntry, 'scope' | 'project_key'>, topicProject?: string): Place | null {
  if (e.scope === 'global') return null;
  if (e.scope === 'topic') return topicProject || e.project_key ? { scope: 'project', project_key: topicProject || e.project_key } : { scope: 'global' };
  return { scope: 'global' };
}
export const promoteOp = (e: Pick<MemoryEntry, 'key' | 'rev' | 'repos'>, to: Place): MemoryEditPayloadV43 =>
  op('promote', [e.key], { ...placeOf(to), ...(e.repos?.length ? { repos: e.repos.slice(0, 10) } : {}), base: e.rev });
/** 采纳：可以先改标题、正文、类别、范围 */
export const acceptOp = (r: MemoryReviewItem, d: Partial<{ title: string; text: string; kind: MemoryKind } & Place> = {}): MemoryEditPayloadV43 => {
  const title: string | undefined = d.title !== undefined && d.title.trim() !== r.title ? d.title.trim() : undefined;
  const text: string | undefined = d.text !== undefined && d.text.trim() !== r.text ? d.text.trim() : undefined;
  const kind: MemoryKind = d.kind ?? r.suggest.kind;
  const place: Place = d.scope ? { scope: d.scope, project_key: d.project_key, topic_key: d.topic_key } : { scope: r.suggest.scope, project_key: r.suggest.project_key };
  return op('accept', [r.key], { title, text, kind, ...placeOf(place) });
};
export const rejectOp = (r: Pick<MemoryReviewItem, 'key'>): MemoryEditPayloadV43 => op('reject', [r.key]);
export const keepOp = (side: 'keep_left' | 'keep_right' | 'keep_both', r: Pick<MemoryReviewItem, 'key' | 'conflict_with'>): MemoryEditPayloadV43 =>
  op(side, [r.conflict_with ?? '', r.key].filter(Boolean));
export const undoOp = (token: string): MemoryEditPayloadV43 => op('undo', [], { undo_token: token });

/** 字数（和 Covey 一样按字符数） */
export const charCount = (s: string): number => Array.from(s).length;
/** 标题、正文、原因的上限（index.limits 没有就用契约的 40/200/600） */
export function limitsOf(idx: Pick<MemoryIndex, 'limits'> | null | undefined, kind: MemoryKind): { title: number; text: number; why: number } {
  const l = idx?.limits;
  return { title: l?.title ?? 40, text: kind === 'decision' ? (l?.decision_text ?? 600) : (l?.text ?? 200), why: 200 };
}
/** 编辑框能不能存：返回原因，能存是空串 */
export function draftProblem(d: { title: string; text: string; why?: string }, lim: { title: number; text: number; why: number }): string {
  if (!d.title.trim()) return '要写标题';
  if (!d.text.trim()) return '要写正文';
  if (charCount(d.title.trim()) > lim.title) return `标题最多 ${lim.title} 字`;
  if (charCount(d.text.trim()) > lim.text) return `正文最多 ${lim.text} 字`;
  if (d.why && charCount(d.why.trim()) > lim.why) return `原因最多 ${lim.why} 字`;
  return '';
}

/* ---------- ⌘K：标题表秒搜 ---------- */
export interface TitleHit {
  code: string;
  title: string;
  key: string;
}
/** 短码完全一样的最前，其次短码开头，再其次标题里有这几个字；最多 n 条 */
export function searchTitles(titles: MemoryIndex['titles'] | null | undefined, query: string, n = 8): TitleHit[] {
  const q: string = query.trim().toLowerCase();
  if (!q || !titles) return [];
  const scored: [number, TitleHit][] = [];
  for (const row of titles) {
    if (!Array.isArray(row) || row.length < 3) continue;
    const [code, title, key] = row;
    const c: string = String(code).toLowerCase();
    const t: string = String(title).toLowerCase();
    const s: number = c === q ? 0 : c.startsWith(q) ? 1 : t.startsWith(q) ? 2 : t.includes(q) ? 3 : -1;
    if (s >= 0) scored.push([s, { code: String(code), title: String(title), key: String(key) }]);
  }
  return scored.sort((a, b) => a[0] - b[0]).slice(0, n).map((x) => x[1]);
}
/** 记忆中心里某一条的链接 */
export const codeHref = (code: string): string => `/memory?code=${encodeURIComponent(code)}`;

/** 短码 → 标题：先查已经拉到的条目，再查 ⌘K 的标题表 */
export function titleIndex(entries: Pick<MemoryEntry, 'code' | 'title'>[], titles: MemoryIndex['titles'] | null | undefined): Map<string, string> {
  const m = new Map<string, string>();
  for (const row of titles ?? []) if (Array.isArray(row) && row[0]) m.set(String(row[0]), String(row[1] ?? ''));
  for (const e of entries) m.set(e.code, e.title);
  return m;
}

/** 一轮下面“带了 N 条记忆”展开的两段：用到的（有 code 的能点进去看），和这一轮带入的全部短码（标题查 memory:index.titles，查不到只留短码）。
 *  used 和 codes 是两份不对应的清单，不按下标配对；带入的里已经列在“用到”里的不再列第二遍 */
export interface TurnMemoryRow { code: string; title: string }
export function turnMemoryRows(
  mem: { used: { code: string; title: string }[]; codes: string[] },
  titles: MemoryIndex['titles'] | null | undefined,
): { used: TurnMemoryRow[]; carried: TurnMemoryRow[] } {
  const known = titleIndex([], titles);
  const used: TurnMemoryRow[] = mem.used.map((u) => ({ code: u.code, title: u.title || known.get(u.code) || '' }));
  const seen = new Set<string>(used.map((u) => u.code).filter(Boolean));
  const carried: TurnMemoryRow[] = [];
  for (const code of mem.codes) {
    if (!code || seen.has(code)) continue;
    seen.add(code);
    carried.push({ code, title: known.get(code) ?? '' });
  }
  return { used, carried };
}

/* ---------- 记录 ---------- */
export const ACTION_WORD: Record<string, string> = {
  add: '记下', edit: '改了', move: '换了范围', archive: '归档', restore: '放回', void: '作废', accept: '采纳', reject: '不要',
  accept_all: '全部采纳', reject_all: '全部不要', keep_left: '留旧的', keep_right: '留新的', keep_both: '两条都留', promote: '提到更大范围',
  // v45 起不再有导入，但旧的变更记录里还有这两个词
  import: '导入', ignore: '留在原工具', undo: '撤销',
  // Bridge 的 memstore 还会写这几个动作（change/set_status 的 action）
  confirm: '确认了', kind: '改了类别', settle: '标为已办', supersede: '替代了',
};
export const WHO_WORD: Record<string, string> = { owner: '你', lead: '负责人', butler: '管家', learn: '自动学习', import: '导入', bridge: 'Covey', web: '你' };
export function logLine(l: MemoryLogItem): { who: string; action: string; title: string; because: string } {
  const because: string = l.because ? [tno(l.because.topic_number), l.because.quote ? `“${l.because.quote}”` : ''].filter(Boolean).join(' ') : '';
  return { who: WHO_WORD[l.who] ?? l.who, action: ACTION_WORD[l.action] ?? l.action, title: l.title, because };
}

/* ---------- 专项页、话题页右栏的精简清单（MemoryList） ---------- */
export interface Brief {
  /** 至多 n 条在用规矩，你定的在前 */
  rules: MemoryEntry[];
  total: number;
}
/** 一组条目的精简看法：在用规矩，你定的在前、新的在前，至多 n 条；不看决定和资料 */
export function briefOf(entries: MemoryEntry[] | null | undefined, n = 6): Brief {
  const rules: MemoryEntry[] = (entries ?? []).filter((e: MemoryEntry) => e.kind === 'rule' && inUse(e));
  const sorted: MemoryEntry[] = [...rules.filter((e) => e.said === 'owner').sort(byNew), ...rules.filter((e) => e.said !== 'owner').sort(byNew)];
  return { rules: sorted.slice(0, n), total: rules.length };
}
/** 右栏第一行：这个专项的规矩 N 条（范围是话题时写“这个话题的”） */
export const briefLine = (b: Pick<Brief, 'total'>, scope: string): string => `这个${scope.startsWith('p:') ? '专项' : '话题'}的规矩 ${b.total} 条`;

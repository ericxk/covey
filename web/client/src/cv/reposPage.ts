// 设置页「常用路径」页（v43 起，v46 加目录和设备上的路径）的纯函数：页签替换、概要、筛选、灰字、改动字段、清理 worktree 的选择与分批。
// 不碰 DOM，测试直接导入。
import type { SettingsData, SettingsRepoItem, SettingsReposPage, SettingsTab } from '@shared/api.interface';

/** 一次 worktrees_prune 最多带几个路径（服务端上限 50） */
export const PRUNE_MAX = 50;
export const NOTE_MAX = 200;
export const NAME_MAX = 64;
export const PATH_MAX = 500;

export type RepoStatus = SettingsRepoItem['status'];
export type RepoWriteMode = SettingsRepoItem['write_mode'];
export type Prunable = SettingsReposPage['worktrees']['prunable'][number];

export const REPOS_TAB: SettingsTab = { key: 'repos', label: '常用路径' };
export type RepoKind = NonNullable<SettingsRepoItem['kind']>;
export type PathDevice = NonNullable<SettingsReposPage['devices']>[number];

/** 类型：旧 Covey 不推 kind，都是 git 仓库 */
export const kindOf = (it: Pick<SettingsRepoItem, 'kind'>): RepoKind => (it.kind === 'dir' ? 'dir' : 'git');
/** 在哪台设备上：本机是 '' */
export const deviceOf = (it: Pick<SettingsRepoItem, 'device'>): string => it.device ?? '';
/** 目录和设备上的路径只能原地改（Covey 也这么拦） */
export const fixedInPlace = (it: Pick<SettingsRepoItem, 'kind' | 'device'>): boolean => kindOf(it) === 'dir' || !!deviceOf(it);

/** 筛选「在哪」：'' 全部，'local' 本机，别的是设备别名 */
export const LOCAL_PLACE = 'local';
/** 筛选「类型」：'' 全部 */
export type KindFilter = '' | RepoKind;
export const KIND_FILTERS: { value: KindFilter; text: string }[] = [
  { value: '', text: '全部' },
  { value: 'git', text: '仓库' },
  { value: 'dir', text: '目录' },
];

/** 状态下拉的叫法 */
export const STATUS_OPTIONS: { value: RepoStatus; text: string }[] = [
  { value: 'active', text: '正常' },
  { value: 'readonly', text: '只读' },
  { value: 'frozen', text: '冻结' },
];
/** 写法下拉的叫法 */
export const MODE_OPTIONS: { value: RepoWriteMode; text: string }[] = [
  { value: 'worktree', text: '独立 worktree' },
  { value: 'inplace', text: '原目录' },
];

const isErr = (p: unknown): boolean => !!p && typeof p === 'object' && typeof (p as { error?: unknown }).error === 'string';

/** 仓库页能用的数据：Covey 推了 repos 且不是错误条目；错误条目也算“有仓库页”（页里显示错误），见 reposMode */
export function reposPageOf(data: Pick<SettingsData, 'repos'> | null | undefined): SettingsReposPage | null {
  const p: unknown = data?.repos;
  return p && !isErr(p) ? (p as SettingsReposPage) : null;
}

/** 仓库模式：Covey 推了 repos（含错误条目：错误显示在仓库页里，不退回旧的「工作区」页） */
export function reposMode(data: Pick<SettingsData, 'repos'> | null | undefined): boolean {
  return !!data && data.repos !== undefined && data.repos !== null;
}

/**
 * 页签：Covey 推的列表里有 repos 就用它；没有 repos 但有 workspace 且 data.repos 存在，
 * 把 workspace 那一项原位换成「仓库」；data.repos 不存在时一字不动（旧 Covey 照旧「工作区」）。
 */
export function tabsWithRepos(tabs: SettingsTab[], data: Pick<SettingsData, 'repos'> | null | undefined): SettingsTab[] {
  if (tabs.some((t) => t.key === REPOS_TAB.key)) return tabs;
  if (!reposMode(data) || !tabs.some((t) => t.key === 'workspace')) return tabs;
  return tabs.map((t) => (t.key === 'workspace' ? { ...REPOS_TAB } : t));
}

/** 仓库模式下访问 /settings/workspace 要跳去 /settings/repos：返回该跳的 tab key，不用跳返回 null */
export function repoRedirect(tab: string | undefined, tabs: SettingsTab[]): string | null {
  return tab === 'workspace' && tabs.some((t) => t.key === REPOS_TAB.key) && !tabs.some((t) => t.key === 'workspace') ? REPOS_TAB.key : null;
}

/** …/tail：tail 已经是尾部几段；空的不写 */
export function tailText(tail: string): string {
  const t: string = (tail ?? '').trim().replace(/^(?:…|\.\.\.)?\/*/u, '');
  return t ? `…/${t}` : '';
}

/** 概要一行：主目录 …/x · 常用路径 N 个（目录 a 个、在设备上 b 个） · 可清理 worktree M 个 */
export function summaryText(p: SettingsReposPage): string {
  const home: string = tailText(p.home?.tail ?? '');
  const dirs: number = p.items.filter((it) => kindOf(it) === 'dir').length;
  const remote: number = p.items.filter((it) => !!deviceOf(it)).length;
  const more: string[] = [dirs ? `目录 ${dirs} 个` : '', remote ? `在设备上 ${remote} 个` : ''].filter(Boolean);
  const count = `常用路径 ${p.items.length} 个${more.length ? `（${more.join('、')}）` : ''}`;
  return [home ? `主目录 ${home}` : '', count, `可清理 worktree ${p.worktrees?.prunable?.length ?? 0} 个`].filter(Boolean).join(' · ');
}

/** 「在哪」的选项：全部、本机，再是名单里出现过的设备（列表里的和能加路径的都算）；没有设备是空（不画这一组） */
export function placeOptions(p: SettingsReposPage): { value: string; text: string }[] {
  const titles = new Map<string, string>();
  for (const d of p.devices ?? []) titles.set(d.alias, d.title || d.alias);
  for (const it of p.items) if (deviceOf(it) && !titles.has(deviceOf(it))) titles.set(deviceOf(it), deviceOf(it));
  if (!titles.size) return [];
  return [{ value: '', text: '全部' }, { value: LOCAL_PLACE, text: '本机' },
    ...[...titles.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([value, text]) => ({ value, text }))];
}

/** 按「在哪」「类型」筛 */
export function filterItems(items: SettingsRepoItem[], place: string, kind: KindFilter): SettingsRepoItem[] {
  return items.filter((it) => (place === '' || (place === LOCAL_PLACE ? !deviceOf(it) : deviceOf(it) === place))
    && (kind === '' || kindOf(it) === kind));
}

/** 「类型」这一组只在两种都有时才画 */
export const showKindFilter = (items: SettingsRepoItem[]): boolean => items.some((it) => kindOf(it) === 'dir') && items.some((it) => kindOf(it) === 'git');

/** 状态标签字样；正常（active）不画 */
export function statusLabel(s: RepoStatus): string {
  return s === 'readonly' ? '只读' : s === 'frozen' ? '冻结' : '';
}

/** 规范文件名的简短说法：1 个写名字，多个“规范 AGENTS.md 等 N 个” */
export function rulesText(rules: string[] | undefined): string {
  const r: string[] = (rules ?? []).filter(Boolean);
  if (!r.length) return '';
  return r.length === 1 ? `规范 ${r[0]}` : `规范 ${r[0]} 等 ${r.length} 个`;
}

/** 写法的说法：设备上的写“在设备上改”，目录和原地改的写“原目录” */
export function modeText(it: SettingsRepoItem): string {
  if (deviceOf(it)) return '在设备上改';
  return it.write_mode === 'inplace' || kindOf(it) === 'dir' ? '原目录' : '独立 worktree';
}

/** 行上的小标签：类型（目录才画）和设备（设备上的才画），状态另画 */
export function placeTags(it: SettingsRepoItem, devices: PathDevice[] | undefined): string[] {
  const out: string[] = [];
  if (kindOf(it) === 'dir') out.push('目录');
  const dv: string = deviceOf(it);
  if (dv) out.push((devices ?? []).find((d) => d.alias === dv)?.title || dv);
  return out;
}

/** 每行灰字的各段：…/tail、分支、写法、规范、最近、在跑话题；空的、为 0 的不写。agoOf 由调用方传（现有的 ago） */
export function repoMeta(it: SettingsRepoItem, now: number, agoOf: (sec: number | null | undefined, now: number) => string): string[] {
  const out: string[] = [];
  const tail: string = tailText(it.tail);
  if (tail) out.push(tail);
  if (it.branch) out.push(it.branch);
  out.push(`写法 ${modeText(it)}`);
  const rules: string = rulesText(it.rules);
  if (rules) out.push(rules);
  const last: string = it.last_used ? agoOf(it.last_used, now) : '';
  if (last) out.push(`最近 ${last}`);
  if (it.live_topics > 0) out.push(`在跑 ${it.live_topics} 个话题`);
  return out;
}

export interface RepoDraft { note: string; status: RepoStatus; write_mode: RepoWriteMode }

export function draftOf(it: SettingsRepoItem): RepoDraft {
  return { note: it.note ?? '', status: it.status, write_mode: it.write_mode };
}

/** repos_set 只带改了的字段 + name；没有改动返回 null（保存按钮置灰） */
export function repoSetPayload(it: SettingsRepoItem, d: RepoDraft): Record<string, unknown> | null {
  const out: Record<string, unknown> = { what: 'repos_set', name: it.name };
  let n = 0;
  const note: string = d.note.trim();
  if (note !== (it.note ?? '').trim()) { out.note = Array.from(note).slice(0, NOTE_MAX).join(''); n++; }
  if (d.status !== it.status) { out.status = d.status; n++; }
  if (d.write_mode !== it.write_mode) { out.write_mode = d.write_mode; n++; }
  return n ? out : null;
}

/** repos_add：路径必填；名字、用途空的不带；device 是选了设备（v46，本机不带） */
export function repoAddPayload(path: string, name: string, note: string, device = ''): Record<string, unknown> | null {
  const p: string = path.trim();
  if (!p) return null;
  const out: Record<string, unknown> = { what: 'repos_add', path: p.slice(0, PATH_MAX) };
  if (device) out.device = device;
  if (name.trim()) out.name = name.trim().slice(0, NAME_MAX);
  if (note.trim()) out.note = Array.from(note.trim()).slice(0, NOTE_MAX).join('');
  return out;
}

export const repoRemovePayload = (name: string): Record<string, unknown> => ({ what: 'repos_remove', name });

/** 加路径输入框的提示：本机写“绝对路径”，选了设备写“<设备> 上的绝对路径” */
export function addHint(device: string, devices: PathDevice[] | undefined): string {
  if (!device) return '绝对路径';
  return `${(devices ?? []).find((d) => d.alias === device)?.title || device} 上的绝对路径`;
}

/** 可清理项的稳定键（tail 就是 Bridge 认的标识） */
export const pruneKey = (p: Prunable): string => p.tail;

/** MM-DD（本地时区）；没有时间返回空 */
export function mmdd(sec: number | undefined): string {
  if (!sec) return '';
  const d = new Date(sec * 1000);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** 可清理行的字样：repo · branch · …/tail · T12 · 结束于 MM-DD */
export function pruneLine(p: Prunable): string {
  const parts: string[] = [p.repo, p.branch, tailText(p.tail)].filter(Boolean);
  if (p.topic_number) parts.push(`T${p.topic_number}`);
  const d: string = mmdd(p.ended_at);
  if (d) parts.push(`结束于 ${d}`);
  return parts.join(' · ');
}

/** 勾选的键里仍然存在于列表的（快照变了，没了的自动丢掉） */
export function keepSelected(sel: string[], list: Prunable[]): string[] {
  const have = new Set(list.map(pruneKey));
  return sel.filter((k) => have.has(k));
}

/** 切换一项勾选 */
export function toggleKey(sel: string[], key: string): string[] {
  return sel.includes(key) ? sel.filter((k) => k !== key) : [...sel, key];
}

/** 全选 / 全不选：现在全选了就清空，否则选满 */
export function toggleAll(sel: string[], list: Prunable[]): string[] {
  const all: string[] = list.map(pruneKey);
  return all.length > 0 && all.every((k) => sel.includes(k)) ? [] : all;
}

/** 一次最多发 PRUNE_MAX 个：返回这次发的路径（去重、按列表顺序）和剩下的个数 */
export function pruneBatch(sel: string[], list: Prunable[]): { paths: string[]; rest: number } {
  const want = new Set(sel);
  const all: string[] = [...new Set(list.map(pruneKey).filter((k) => want.has(k) && k.length > 0).map((k) => k.slice(0, PATH_MAX)))];
  return { paths: all.slice(0, PRUNE_MAX), rest: Math.max(0, all.length - PRUNE_MAX) };
}

export const prunePayload = (paths: string[]): Record<string, unknown> => ({ what: 'worktrees_prune', paths });

/** 没有可清理项时的那行灰字 */
export function pruneEmptyText(total: number): string {
  return total > 0 ? `共 ${total} 个 worktree，都在用` : '没有可清理的 worktree';
}

/** 二次确认的文案：数量 + 明确后果；超过一批时提示分批 */
export function pruneAskText(count: number, rest: number): string {
  const base = `清理选中的 ${count} 个？会删除这些 worktree 目录和分支`;
  return rest > 0 ? `${base}。一次最多 ${PRUNE_MAX} 个，剩下 ${rest} 个再点一次` : base;
}

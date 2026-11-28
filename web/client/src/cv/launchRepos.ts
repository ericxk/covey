/**
 * 去工作区（v43）的纯函数：新旧两代 Covey 的判定、仓库选择、payload 清洗、标签文字。
 * 不碰 DOM、只引类型：防回归 scripts/launch-repos.test.mjs。
 * 判定：LaunchData.workspaces 存在 = 旧 Covey（面板、联动、payload 一字不变）；不存在 = 新 Covey（没有工作区、运行方式，带仓库）。
 * D2Y4T 起：新话题不能再选执行设备（话题都从主目录启动，要用设备在话题里用 covey device）；
 * 网页不画设备下拉、payload 一律不带 device（哪怕较旧的 Covey 还推 devices）；设备只在换配置里只读显示话题本来所在的那台。
 */
import type { LaunchData, LaunchDevice, LaunchPayload, LaunchRepo, SayFile, SettingsRepoItem } from '@shared/api.interface';

/** 一次最多带几个仓库（服务端 ≤10） */
export const MAX_REPOS = 10;

type WithPlace = { workspace?: string; run_mode?: string; device?: string; repos?: LaunchRepo[] };

/** 新 Covey：不推 workspaces */
export const isRepoMode = (d: LaunchData): boolean => !d.workspaces;

/** 本机的叫法：话题不在设备上（空串）就是本机 */
export const LOCAL_TEXT = '本机';

/** 话题或选择所在设备的叫法：空串是本机，别的取设备的叫法（设备列表里没有就写别名，绝不拿第一台顶替） */
export function deviceWord(d: LaunchData, name: string | undefined): string {
  if (!name) return LOCAL_TEXT;
  const dv: LaunchDevice | undefined = (d.devices ?? []).find((x: LaunchDevice) => x.name === name);
  return dv?.title || name;
}

export type RepoMode = 'none' | 'read' | 'write';
export const repoModeOf = (repos: LaunchRepo[] | undefined, name: string): RepoMode => (repos ?? []).find((r: LaunchRepo) => r.name === name)?.mode ?? 'none';

/** 点一下轮换：不用 → 只读 → 可写 → 不用（只读仓库跳过可写）；已满 10 个时“不用”不能再加 */
export function cycleRepo(repos: LaunchRepo[] | undefined, name: string, readonly = false): LaunchRepo[] {
  const cur: LaunchRepo[] = repos ?? [];
  const mode: RepoMode = repoModeOf(cur, name);
  const rest: LaunchRepo[] = cur.filter((r: LaunchRepo) => r.name !== name);
  if (mode === 'none') return cur.length >= MAX_REPOS ? cur : [...cur, { name, mode: 'read' }];
  if (mode === 'read' && !readonly) return cur.map((r: LaunchRepo) => (r.name === name ? { name, mode: 'write' as const } : r));
  return rest;
}

/** 一块的“在哪、是什么”（v46）：目录写“目录”，设备上的写设备名；本机的仓库是空串 */
export function repoWhere(i: Pick<SettingsRepoItem, 'kind' | 'device'>): string {
  return [i.kind === 'dir' ? '目录' : '', i.device ? `在 ${i.device} 上` : ''].filter(Boolean).join(' · ');
}

/** 仓库选择行列哪些块：常用路径里非冻结的，加上已选但名单里没有的（话题带着、后来冻结了的，要能取消）；where 见 repoWhere */
export function repoChoices(items: SettingsRepoItem[], selected: LaunchRepo[] | undefined): { name: string; readonly: boolean; note: string; where: string }[] {
  const out = items
    .filter((i: SettingsRepoItem) => i.status !== 'frozen')
    .map((i: SettingsRepoItem) => ({ name: i.name, readonly: i.status === 'readonly', note: i.note, where: repoWhere(i) }));
  for (const r of selected ?? []) if (!out.some((o) => o.name === r.name)) out.push({ name: r.name, readonly: false, note: '', where: '' });
  return out;
}

/** 两份仓库选择是否一样（不看顺序） */
export function sameRepos(a: LaunchRepo[] | undefined, b: LaunchRepo[] | undefined): boolean {
  const key = (x: LaunchRepo[] | undefined): string => (x ?? []).map((r: LaunchRepo) => `${r.name}:${r.mode}`).sort().join('|');
  return key(a) === key(b);
}

/** “仓库 a、b”，最多列 2 个，多了写“仓库 a、b 等 N 个”；没有是空串 */
export function repoLabel(repos: { name: string }[] | undefined): string {
  const names: string[] = (repos ?? []).map((r) => r.name);
  if (!names.length) return '';
  return `仓库 ${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 个` : ''}`;
}

/** 仓库的短写法（换配置折叠栏）：“covey（改）、web（读）”，多于 max 个写“… 等 N 个”；没有是空串 */
export function repoWords(repos: LaunchRepo[] | undefined, max = 3): string {
  const list: LaunchRepo[] = repos ?? [];
  const words: string[] = list.slice(0, max).map((r: LaunchRepo) => `${r.name}（${r.mode === 'write' ? '改' : '读'}）`);
  return words.join('、') + (list.length > max ? ` 等 ${list.length} 个` : '');
}

/** 换配置折叠栏上的摘要：执行设备只在话题本来就在设备上（device 是非空字符串）时写，本机、旧 Covey 没说（undefined）都是 null；仓库 */
export function placeSummary(d: LaunchData, device: string | undefined, repos: LaunchRepo[] | undefined): { device: string | null; repos: string } {
  return { device: device ? deviceWord(d, device) : null, repos: repoWords(repos) || '未选仓库' };
}

/** 新 Covey 的 payload 清洗：删掉 workspace、run_mode（不留空串、不留 undefined 键）；device 一律删掉（D2Y4T：新话题不选设备）；repos 空就不带。
 *  emptyRepos：换配置里把仓库全取消时要明确发空数组，不然 Covey 当作没改 */
export function cleanRepoPayload<T extends WithPlace>(_d: LaunchData, p: T, emptyRepos = false): T {
  const out: T = { ...p };
  delete out.workspace;
  delete out.run_mode;
  delete out.device;
  if (!out.repos?.length) {
    if (emptyRepos) out.repos = [];
    else delete out.repos;
  }
  return out;
}

/** 开新话题的 payload：旧 Covey 一字不变（含 run_mode: 设备上只能 direct），新 Covey 走清洗 */
export function launchPayload(d: LaunchData, sel: Omit<LaunchPayload, 'text'>, text: string, files: SayFile[], onDevice: boolean): LaunchPayload {
  const extra = files.length ? { files } : {};
  if (isRepoMode(d)) return cleanRepoPayload(d, { ...sel, text, ...extra });
  return { ...sel, text, run_mode: onDevice ? 'direct' : sel.run_mode, ...extra };
}

/** 换配置的 payload：旧 Covey 原样；新 Covey 清洗，并且仓库从有变成全空时发 []。
 *  都不带 device（T879）：换配置换不了机器，config 里的 device 只是给面板看的 */
export function switchPayload<T extends WithPlace>(d: LaunchData, cur: T, hadRepos: boolean, lead: boolean): Record<string, unknown> {
  const { device: _device, ...rest } = cur;
  void _device;
  if (!isRepoMode(d)) return { ...rest };
  return cleanRepoPayload(d, (lead ? { ...rest, repos: undefined } : rest) as T, !lead && hadRepos);
}

/* 「账号与额度」（数据约定 v42，Covey 推的设置条目 quota）：从条目算出表里每一格写什么、颜色、旧不旧，折起来的来源一行写什么，
 * 侧栏胶囊说什么。纯函数，单测在 scripts/quota.test.mjs。时间都是秒；重置、恢复和读数的时刻都按上海时区写几月几号几点几分（11月27日 03:30）。 */
import type { QuotaAccount, QuotaData, QuotaPool, QuotaResponse, QuotaSource, QuotaWindow } from '@shared/api.interface';
import { canDo } from './v34';

/** 设置页里的页签；Covey 有额度来源才有 */
export const QUOTA_TAB = { key: 'quota', label: '账号与额度' } as const;

/** 剩余不到这个比例就红 */
export const TIGHT = 0.2;
/** 剩余不超过这个比例算用完（agy 的口径：1% 以下） */
export const SPENT = 0.01;

/** 这个 Covey 有「账号与额度」：认 quota_refresh 操作，也推过来源 */
export const quotaOn = (ops: readonly string[] | null | undefined, r: QuotaResponse | null | undefined): boolean =>
  canDo(ops, 'quota_refresh') && (r?.data?.sources?.length ?? 0) > 0;

const TZ = 'Asia/Shanghai';
const STAMP = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

/** 一个时刻怎么写：上海时间的几月几号几点几分（11月27日 03:30）。当天、跨天、周窗口一个写法，一眼看清哪天几点
 * （主人 11-27：刷新时刻要写几月几号几点几分）；不看运行它的机器在哪个时区；没有是空串 */
export function whenWords(sec: number): string {
  if (!(sec > 0)) return '';
  const part: Record<string, string> = {};
  for (const p of STAMP.formatToParts(new Date(sec * 1000))) part[p.type] = p.value;
  return `${part.month}月${part.day}日 ${part.hour}:${part.minute}`;
}

/** “6 小时前”这样的话，悬停说读数多旧 */
export function agoWords(sec: number, now: number): string {
  const s: number = Math.max(0, now - sec);
  if (s < 90) return '刚刚';
  if (s < 5400) return `${Math.round(s / 60)} 分钟前`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

/** 一格：none 读不到（写“—”、不画条）；out 用完；bar 画条；pct 是剩余百分比（0~100）；at 是时刻或“N 个号平均” */
export interface Cell {
  kind: 'none' | 'out' | 'bar';
  pct: number | null;
  at: string;
  stale: boolean;
  tight: boolean;
  /** 号现在用不了（另一个窗口用完、冷却中）：这一格只是窗口本身的读数，灰色，平均里按 0 算（主人 11-29：用完的号别当 100% 算） */
  off: boolean;
  /** 悬停：这个数怎么来的、读数是什么时候的、为什么写“—” */
  title: string;
}
const NONE = (title = '', at = ''): Cell => ({ kind: 'none', pct: null, at, stale: false, tight: false, off: false, title });

const KIND_WORDS: Record<'5h' | '7d', string> = { '5h': '5 小时', '7d': '周' };
const pctOf = (left: number): number => Math.round(left * 100);

/** 同一种窗口有几个（网关的“Fable 周”和“周”）取剩得最少的；都读不到取第一个 */
export function pickWindow(pool: QuotaPool, kind: '5h' | '7d'): QuotaWindow | null {
  const of: QuotaWindow[] = pool.windows.filter((w: QuotaWindow) => w.kind === kind);
  const read: QuotaWindow[] = of.filter((w: QuotaWindow) => w.left !== null);
  if (read.length) return read.reduce((a: QuotaWindow, b: QuotaWindow) => ((b.left as number) < (a.left as number) ? b : a));
  return of[0] ?? null;
}

/** 这个池因为 state 用完，该写在哪一列：读数里已经用完的那个窗口，没有就写在 5 小时那一列 */
function outKind(pool: QuotaPool): '5h' | '7d' | null {
  if (pool.state !== 'spent' && pool.state !== 'cooling') return null;
  const spent: QuotaWindow | undefined = pool.windows.find((w: QuotaWindow) => (w.kind === '5h' || w.kind === '7d') && w.left !== null && w.left <= SPENT);
  return spent ? (spent.kind as '5h' | '7d') : '5h';
}

/** 号（池）现在能不能用：yes 能用；no 用不了（用完、冷却、暂停、要重新登录、到期），平均里按 0 算；unknown 读不到，不算进平均。
 * 能不能用由来源判断（网关有保留线、保护线），页面不从百分比推 */
export function usableOf(a: QuotaAccount, pool: QuotaPool | undefined): 'yes' | 'no' | 'unknown' {
  if (a.state === 'login_required' || a.state === 'paused' || a.state === 'expired') return 'no';
  const state: string = pool?.state ?? a.state;
  if (state === 'ok') return 'yes';
  return state === 'unknown' ? 'unknown' : 'no';
}

/** 号为什么用不了，一句话：“周用完，12月1日 03:02 恢复”“冷却中，11月29日 02:00 恢复”“要重新登录”；能用是空串 */
export function offWords(a: QuotaAccount, pool: QuotaPool | undefined): string {
  if (a.state === 'login_required') return '要重新登录';
  if (a.state === 'paused') return '已暂停';
  if (a.state === 'expired') return '已到期';
  const state: string = pool?.state ?? a.state;
  const back: number = pool?.available_at ?? a.available_at ?? 0;
  const when: string = back ? `，${whenWords(back)} 恢复` : '';
  if (state === 'spent') {
    const k: '5h' | '7d' | null = pool ? outKind(pool) : null;
    const spentRead: boolean = !!pool && pool.windows.some((w: QuotaWindow) => w.kind === k && w.left !== null && w.left <= SPENT);
    return `${spentRead && k ? `${KIND_WORDS[k]}` : '额度'}用完${when}`;
  }
  if (state === 'cooling') return `冷却中${when}`;
  if (state === 'paused') return '已暂停';
  if (state === 'login_required') return '要重新登录';
  if (state === 'expired') return '已到期';
  return '';
}

/** 悬停里一个窗口一行：“周 剩 9%，11月30日 19:59 重置，用到 99% 停派”；同一种窗口有几个（周、Fable 周）都列出来 */
function windowLines(pool: QuotaPool, kind: '5h' | '7d'): string[] {
  return pool.windows
    .filter((w: QuotaWindow) => w.kind === kind)
    .map((w: QuotaWindow) => {
      const name: string = w.label || KIND_WORDS[kind];
      if (w.left === null) return `${name} 读不到`;
      const parts: string[] = [`${name} 剩 ${pctOf(w.left)}%`];
      if (w.resets_at) parts.push(`${whenWords(w.resets_at)} 重置`);
      else if (w.left >= 0.995) parts.push('还没开始计时（下次用到这个号才开始算）');
      if (w.ceiling !== undefined && w.ceiling > 0 && w.ceiling < 1) parts.push(`用到 ${pctOf(w.ceiling)}% 停派`);
      return parts.join('，');
    });
}

/** 来源的读数比 ttl 的两倍还旧：表里整份写“—”（数字可能早就不对了），组名下一行说停在几点、为什么 */
export const isOld = (source: QuotaSource, now: number): boolean => source.collected_at > 0 && now - source.collected_at > 2 * source.ttl;
const OLD = (source: QuotaSource, now: number): Cell => NONE(`读数停在 ${whenWords(source.collected_at)}（${agoWords(source.collected_at, now)}），太旧不显示`);

export function cellOf(source: QuotaSource, pool: QuotaPool, kind: '5h' | '7d', now: number, account?: QuotaAccount): Cell {
  const w: QuotaWindow | null = pickWindow(pool, kind);
  const out: boolean = outKind(pool) === kind;
  if (isOld(source, now)) return OLD(source, now);
  const back: number = pool.available_at ?? w?.resets_at ?? 0;
  if (!w || w.left === null) {
    if (out) return { kind: 'out', pct: 0, at: back ? `${whenWords(back)} 恢复` : '', stale: false, tight: false, off: false, title: '' };
    return NONE(w ? '读不到' : `没有${KIND_WORDS[kind]}窗口的读数`);
  }
  const checked: number = w.checked_at ?? source.collected_at;
  if (!out && w.resets_at && w.resets_at <= now && checked < w.resets_at) {
    return NONE(`${whenWords(w.resets_at)} 已重置，还没有新读数（重置前剩 ${pctOf(w.left)}%）`, '已重置');
  }
  const stale: boolean = source.failing || (checked > 0 && now - checked > source.stale_after);
  const lines: string[] = windowLines(pool, kind);
  if (stale) lines.push(checked > 0 ? `${agoWords(checked, now)}的读数（${whenWords(checked)}）${source.failing ? '，上次读取没成功' : ''}` : '上次读取没成功');
  if (out || w.left <= SPENT) {
    const at: number = pool.available_at ?? w.resets_at ?? 0;
    return { kind: 'out', pct: 0, at: at ? `${whenWords(at)} 恢复` : '', stale, tight: false, off: false, title: lines.join('\n') };
  }
  const why: string = account && usableOf(account, pool) === 'no' ? offWords(account, pool) : '';
  if (why) lines.unshift(`这个号现在用不了（${why}），这一格只是窗口本身的读数，平均里按 0 算`);
  return {
    kind: 'bar',
    pct: pctOf(w.left),
    at: w.resets_at ? whenWords(w.resets_at) : w.left >= 0.995 ? '未开始计时' : '',
    stale,
    tight: !why && w.left < TIGHT,
    off: !!why,
    title: lines.join('\n'),
  };
}

/** 账号的名字：登录的邮箱（完整），读不到才用来源给的名字 */
export const nameOf = (a: QuotaAccount): string => a.login || a.name;

/** 整行的话（要重新登录、暂停、到期）；有读数可画的是空串 */
export function rowNote(a: QuotaAccount): string {
  if (a.state === 'login_required') return '要重新登录';
  if (a.state === 'paused') return '已暂停';
  if (a.state === 'expired') return '已到期';
  return '';
}

/** 号在不在用：agy 只有一个池时，在用的号名字前一个蓝点；两个池时蓝点标在模型名前（poolActive） */
export const accountActive = (a: QuotaAccount): boolean => a.pools.length === 1 && a.pools[0].active;

/** 来源折成一行时某一列的平均（主人 11-29 定的口径）：每个号算它现在还能用的那份——能用的号按这一列的剩余，
 * 用不了的号（用完、冷却、暂停、要重新登录、到期）按 0，读不到的号不算；加起来除以算进去的号数。悬停写出算式和谁按 0 算、谁没算 */
export function avgCell(source: QuotaSource, kind: '5h' | '7d', now: number): Cell {
  if (isOld(source, now)) return OLD(source, now);
  const parts: number[] = [];
  const zero: string[] = [];
  const skipped: string[] = [];
  let staleAll = true;
  for (const a of source.accounts) {
    const p: QuotaPool | undefined = a.pools[0];
    const usable: 'yes' | 'no' | 'unknown' = usableOf(a, p);
    if (usable === 'no') {
      parts.push(0);
      zero.push(`${nameOf(a)} ${offWords(a, p) || '用不了'}`);
      staleAll = false;
      continue;
    }
    const c: Cell = p && usable === 'yes' ? cellOf(source, p, kind, now, a) : NONE();
    if (c.kind === 'none') {
      skipped.push(`${nameOf(a)}${c.at === '已重置' ? ' 已重置、还没有新读数' : ' 读不到'}`);
      continue;
    }
    parts.push(c.kind === 'out' ? 0 : c.pct ?? 0);
    staleAll = staleAll && c.stale;
  }
  if (!parts.length) return NONE(skipped.length ? `没有读得到的号：${skipped.join('；')}` : '');
  const pct: number = Math.round(parts.reduce((sum: number, n: number) => sum + n, 0) / parts.length);
  const n: number = parts.length;
  const sum: string = n <= 12 ? `（${parts.join('+')}）÷ ${n} ≈ ${pct}%` : `${n} 个号相加除以 ${n} ≈ ${pct}%`;
  const lines: string[] = [`${n} 个号的${KIND_WORDS[kind]}剩余平均：${sum}`];
  if (zero.length) lines.push(`现在用不了、按 0 算：${zero.join('；')}`);
  if (skipped.length) lines.push(`没算进去：${skipped.join('；')}`);
  return { kind: 'bar', pct, at: `${n} 个号平均`, stale: staleAll, tight: pct < TIGHT * 100, off: false, title: lines.join('\n') };
}

/** 一个号有没有事：冷却、用完，或有一格剩不到 20%（红点） */
export function accountWarn(source: QuotaSource, a: QuotaAccount, now: number): boolean {
  if (a.state === 'spent' || a.state === 'cooling') return true;
  return a.pools.some((p: QuotaPool) =>
    (['5h', '7d'] as const).some((k: '5h' | '7d') => {
      const c: Cell = cellOf(source, p, k, now, a);
      return c.tight || c.kind === 'out';
    }),
  );
}

/** 组名后一行小字：来源读不到时说清楚，免得表里一片“—”却不知道为什么。读数太旧（超过 ttl 的两倍）写“读数停在几点 · 原因”；
 * 只是上次没读到写“上次读取没成功 · 原因（数字是几点的）”；读得好好的是空串 */
export function sourceAlert(source: QuotaSource, now: number): string {
  const why: string = source.why ? ` · ${source.why}` : '';
  if (isOld(source, now)) {
    return `读数停在 ${whenWords(source.collected_at)}（${agoWords(source.collected_at, now)}）${source.failing ? why || ' · 上次读取没成功' : ''}`;
  }
  if (!source.failing) return '';
  return source.collected_at > 0 ? `上次读取没成功${why}，数字是 ${whenWords(source.collected_at)} 的` : `读不到${why}`;
}

/** 组名旁“k/n 个能用”的悬停：哪几个号现在用不了、为什么 */
export function usableTitle(source: QuotaSource): string {
  const off: string[] = source.accounts
    .filter((a: QuotaAccount) => usableOf(a, a.pools[0]) === 'no')
    .map((a: QuotaAccount) => `${nameOf(a)}：${offWords(a, a.pools[0]) || '用不了'}`);
  return off.length ? `现在用不了的号\n${off.join('\n')}` : '';
}

/** 组名后的红点：有号有事 */
export const sourceWarn = (source: QuotaSource, now: number): boolean => source.accounts.some((a: QuotaAccount) => accountWarn(source, a, now));

/** 展开还是折起：用户点过听用户的，否则 Covey 说展开（expand）就展开 */
export const isOpen = (source: QuotaSource, picked: boolean | undefined): boolean => (picked === undefined ? source.expand : picked);

/** 组名旁的小字：agy 写“N 个号 · 用完自动顺延”；展开的外接来源写“N 个号”；网关和折起的写“k/n 个能用”（读不到的原因另在 sourceAlert） */
export function groupNote(source: QuotaSource, open: boolean): string {
  const n: number = source.accounts.length;
  if (source.kind === 'agy') return `${n} 个号 · 用完自动顺延`;
  if (open && source.kind !== 'gateway') return `${n} 个号`;
  return source.total > 0 ? `${source.usable}/${source.total} 个能用` : '';
}

/** 侧栏胶囊：某个来源整体见底，或有号要重新登录；都没有是空串（不画）。点了到这一页。
 * 恢复时刻和表里一样写（几月几号几点几分） */
export function capsuleOf(data: QuotaData | null | undefined): string {
  if (!data) return '';
  const low: QuotaSource | undefined = data.sources.find((s: QuotaSource) => s.low);
  if (low) return `${low.title} 额度见底${low.dry && low.next_available_at ? ` · ${whenWords(low.next_available_at)} 恢复` : ''}`;
  const first = data.relogin[0];
  if (!first) return '';
  const same: number = data.relogin.filter((r: { source: string }) => r.source === first.source).length;
  return `${first.source} 有 ${same} 个号要重新登录`;
}

/** 表上方的红条：哪个来源的哪个号要重新登录；没有是空串 */
export const reloginLine = (data: QuotaData): string => data.relogin.map((r: { source: string; name: string }) => `${r.source} · ${r.name} 要重新登录`).join('；');

/** 标题行右边的读数时刻；没有读过是空串 */
export const readWords = (data: QuotaData): string => whenWords(data.read_at);

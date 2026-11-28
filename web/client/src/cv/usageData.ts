// 用量页和看板「本周用量」的数（Covey 数据约定 v26 的 usage.history）：取哪几天、合计、坐标刻度、日期的写法。
// 只放纯函数（node --test scripts/usage.test.mjs 直接测），画在 UsageBars.tsx、UsagePage.tsx、Board.tsx。
// 看板右栏「近 24 小时」的三个数（统计接口的 today_* 字段）也在这里。
import type { StatsResponse, UsageDay, UsageFigures, UsageHarness, UsageResponse, UsageSpan } from '@shared/api.interface';

/** 用量页能选的几段：最近 7、30、90 天（含今天），和 Covey 合计的 spans 一一对应 */
export const RANGES: readonly number[] = [7, 30, 90];

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/** 2026-11-21 是星期几（0 是周日）；按日期本身算，和浏览器在哪个时区无关 */
export function weekdayOf(date: string): number {
  return new Date(`${date}T00:00:00Z`).getUTCDay();
}
export const weekWord = (date: string): string => WEEK[weekdayOf(date)] ?? '';
/** 11-21 */
export const mmdd = (date: string): string => date.slice(5);
/** 11-21 周五 */
export const dayWords = (date: string): string => `${mmdd(date)} ${weekWord(date)}`;

/** 最近 n 天（旧的在前，今天最后）；Covey 说了记录从哪天开始，那之前的天不算（不是 0，是还没有记录） */
export function shownDays(u: UsageResponse, n: number): UsageDay[] {
  const last: UsageDay[] = u.days.slice(-n);
  const first: string | null = u.first;
  return first ? last.filter((d: UsageDay) => d.date >= first) : last;
}

/** 几天的数加起来（话题数不能这样加，留给 spanOf 用 Covey 的） */
export function sumDays(days: UsageDay[]): UsageFigures {
  const z: UsageFigures = { tasks: 0, topics: 0, tokens: 0, input: 0, cache: 0, output: 0, untracked: 0 };
  for (const d of days) {
    z.tasks += d.tasks;
    z.topics += d.topics;
    z.tokens += d.tokens;
    z.input += d.input;
    z.cache += d.cache;
    z.output += d.output;
    z.untracked += d.untracked;
  }
  return z;
}

/** 最近 n 天的合计：Covey 算好的那段（话题去重、各 Harness）；没有就把每天加起来，话题数和 Harness 不给 */
export function spanOf(u: UsageResponse, n: number): UsageSpan & { exact: boolean } {
  const s: UsageSpan | undefined = u.spans.find((x: UsageSpan) => x.days === n);
  if (s) return { ...s, exact: true };
  const days: UsageDay[] = u.days.slice(-n);
  return { ...sumDays(days), days: n, since: days[0]?.date ?? '', harnesses: [], exact: false };
}

/** 缓存命中占输入的百分比；没有输入是 null */
export const cachePct = (f: Pick<UsageFigures, 'input' | 'cache'>): number | null => (f.input > 0 ? Math.round((f.cache / f.input) * 100) : null);

/** 表格和浮窗里的 token：比首屏大字多一位，好比较（2.63B、327M、12.4M、8.2K） */
export function tokText(n: number): string {
  if (!n) return '0';
  const unit = (v: number, u: string): string => `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)}${u}`;
  if (n >= 1e9) return unit(n / 1e9, 'B');
  if (n >= 1e6) return unit(n / 1e6, 'M');
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(Math.round(n));
}

/** 纵轴刻度：0 起，步长取 1、2、2.5、5 × 10 的幂，最多 count 格，最上一格不低于 max */
export function niceTicks(max: number, count = 3): number[] {
  if (!(max > 0)) return [0];
  const raw: number = max / count;
  const pow: number = 10 ** Math.floor(Math.log10(raw));
  const step: number = [1, 2, 2.5, 5, 10].map((m: number) => m * pow).find((s: number) => s >= raw) ?? 10 * pow;
  const ticks: number[] = [];
  for (let v = 0; v < max + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  if (ticks[ticks.length - 1] < max) ticks.push(Number((ticks[ticks.length - 1] + step).toPrecision(12)));
  return ticks;
}

/** 刻度上的字：0、500M、1B、2.5B */
export function axisText(v: number): string {
  if (!v) return '0';
  const strip = (x: number): string => String(Number(x.toFixed(2)));
  if (v >= 1e9) return `${strip(v / 1e9)}B`;
  if (v >= 1e6) return `${strip(v / 1e6)}M`;
  if (v >= 1e3) return `${strip(v / 1e3)}K`;
  return strip(v);
}

/** 横轴哪几天写日期：7 天以内每天都写；更长的只写周一，排不下就隔几个周一写一个 */
export function xTicks(days: UsageDay[], maxLabels: number): number[] {
  if (days.length <= 7) return days.map((_: UsageDay, i: number) => i);
  const mondays: number[] = days.flatMap((d: UsageDay, i: number) => (weekdayOf(d.date) === 1 ? [i] : []));
  const every: number = Math.max(1, Math.ceil(mondays.length / Math.max(1, maxLabels)));
  return mondays.filter((_: number, k: number) => k % every === 0);
}

/** 用量页的日均：合计除以画出来的天数（今天没过完也算一天） */
export const perDay = (tokens: number, days: number): number => (days > 0 ? tokens / days : 0);

/** 各 Harness 占了多少 token（看板「本周用量」下面一行）：从多到少，不到 10% 的写一位小数 */
export interface Share {
  title: string;
  pct: number;
  text: string;
}

/** 有 token 的 Harness 不到 2 个时是空数组（只有一个就是 100%，不写） */
export function harnessShares(rows: readonly Pick<UsageHarness, 'title' | 'tokens'>[] | undefined): Share[] {
  const xs: Pick<UsageHarness, 'title' | 'tokens'>[] = (rows ?? []).filter((h: Pick<UsageHarness, 'title' | 'tokens'>) => h.tokens > 0);
  const total: number = xs.reduce((sum: number, h: Pick<UsageHarness, 'title' | 'tokens'>) => sum + h.tokens, 0);
  if (xs.length < 2 || total <= 0) return [];
  return [...xs]
    .sort((x: Pick<UsageHarness, 'title' | 'tokens'>, y: Pick<UsageHarness, 'title' | 'tokens'>) => y.tokens - x.tokens)
    .map((h: Pick<UsageHarness, 'title' | 'tokens'>): Share => {
      const pct: number = (h.tokens / total) * 100;
      const text: string = pct >= 9.95 ? `${Math.round(pct)}%` : pct >= 0.05 ? `${pct.toFixed(1)}%` : '不到 0.1%';
      return { title: h.title, pct, text };
    });
}

/** 看板右栏「近 24 小时」的一格：大字的数、后面的单位、下面的说明 */
export interface DayCell {
  n: string;
  unit: string;
  label: string;
}

const oneDecimal = (v: number): string => (v >= 100 ? String(Math.round(v)) : v.toFixed(1).replace(/\.0$/, ''));

/** 一段时长写成数和单位：不到 1 分钟写秒，不到 1 小时写分钟（不到 10 分钟带一位小数），再长写小时 */
function spanCell(sec: number): [string, string] {
  if (sec < 60) return [String(Math.round(sec)), '秒'];
  if (sec < 3600) return [sec < 600 ? oneDecimal(sec / 60) : String(Math.round(sec / 60)), '分钟'];
  return [oneDecimal(sec / 3600), '小时'];
}

/** 做完几轮、Agent 干了多久、答过的题平均等了你多久；旧服务端没有这几个字段时是空数组（不画） */
export function dayFigures(st: Pick<StatsResponse, 'today_done' | 'today_work_seconds' | 'avg_wait_seconds'> | undefined): DayCell[] {
  if (!st || typeof st.today_done !== 'number' || typeof st.today_work_seconds !== 'number') return [];
  const [wn, wu] = spanCell(Math.max(0, st.today_work_seconds));
  const [an, au] = st.avg_wait_seconds === null || st.avg_wait_seconds === undefined ? ['—', ''] : spanCell(Math.max(0, st.avg_wait_seconds));
  return [
    { n: String(st.today_done), unit: '轮', label: '做完' },
    { n: wn, unit: wu, label: 'Agent 干活' },
    { n: an, unit: au, label: '平均等你' },
  ];
}

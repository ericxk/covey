/* 看板右栏的「额度」（数据约定 v42 的设置条目 quota，和设置页「账号与额度」、侧栏胶囊同一份）：一个来源一行，
 * 名字和“k/n 个能用”；有号用不了、偏紧、要重新登录，或读数太旧时，下面每件事一行（状态色），都没有写一行灰字“都够用”。
 * 只说几个号、哪种窗口、几点恢复，逐号的数在设置页。纯函数，单测在 scripts/quota-brief.test.mjs。时间都是秒。 */
import type { QuotaAccount, QuotaData, QuotaPool, QuotaSource, QuotaWindow } from '@shared/api.interface';
import { isOld, sourceAlert, SPENT, TIGHT, usableOf, whenWords } from './quota';

export interface BriefLine {
  /** warn 状态色（有号用不了、偏紧、要重新登录）；ok、faint 灰字 */
  tone: 'warn' | 'ok' | 'faint';
  text: string;
}

export interface BriefRow {
  id: string;
  title: string;
  /** “5/6 个能用”；来源没给号数是空串 */
  count: string;
  lines: BriefLine[];
}

const KIND_WORDS: Record<'5h' | '7d', string> = { '5h': '5 小时', '7d': '周' };
const OFF_WORDS: Record<string, string> = { spent: '用完', cooling: '冷却中', paused: '已暂停', expired: '已到期', login_required: '要重新登录' };
/** 先说哪种：要你动手的（重新登录）在前，会自己恢复的（用完、冷却）在后 */
const OFF_ORDER: string[] = ['login_required', 'expired', 'paused', 'spent', 'cooling'];

/** 号的一个池为什么用不了（能不能用照设置页的 usableOf，由来源判断）：要重新登录、暂停、到期是号的事，用完、冷却看池（agy 一个号几个池，各自用完） */
const offState = (a: QuotaAccount, p: QuotaPool | undefined): string =>
  a.state === 'login_required' || a.state === 'paused' || a.state === 'expired' ? a.state : p?.state ?? a.state;

/** 这一格的剩余还作数：读得到，而且没有在读数之后重置过 */
function leftOf(w: QuotaWindow, source: QuotaSource, now: number): number | null {
  if (w.left === null) return null;
  const checked: number = w.checked_at ?? source.collected_at;
  if (w.resets_at && w.resets_at <= now && checked < w.resets_at) return null;
  return w.left;
}

/** 池名（agy 的 Gemini、Claude/GPT）：一个来源里有不止一种池才写，网关这类一个号一个池的不写 */
function poolWord(source: QuotaSource, p: QuotaPool): string {
  const keys = new Set<string>(source.accounts.flatMap((a: QuotaAccount) => a.pools.map((x: QuotaPool) => x.key)));
  return keys.size > 1 && p.label ? `${p.label} ` : '';
}

/** 一个来源的几行话 */
function linesOf(source: QuotaSource, now: number): BriefLine[] {
  // 读不到、读数太旧：和设置页组名下那行同一句（sourceAlert）；太旧的数不再报偏紧
  const alert: string = sourceAlert(source, now);
  if (alert && (isOld(source, now) || !source.collected_at)) return [{ tone: 'faint', text: alert }];
  // 用不了的：按原因数号，记最早恢复的时刻
  const off = new Map<string, { names: Set<string>; back: number }>();
  // 偏紧的：能用的号里某种窗口剩不到 TIGHT（用完的那格算进上面）
  const tight = new Map<string, Set<string>>();
  for (const a of source.accounts) {
    const pools: (QuotaPool | undefined)[] = a.pools.length ? a.pools : [undefined];
    for (const p of pools) {
      const usable = usableOf(a, p);
      const st: string = offState(a, p);
      const where: string = p ? poolWord(source, p) : '';
      if (usable === 'no') {
        const k = `${st}|${where}`;
        const e = off.get(k) ?? { names: new Set<string>(), back: 0 };
        e.names.add(a.name);
        const back: number = p?.available_at ?? a.available_at ?? 0;
        if (back > now && (!e.back || back < e.back)) e.back = back;
        off.set(k, e);
        continue;
      }
      if (usable !== 'yes' || !p) continue;
      for (const kind of ['5h', '7d'] as const) {
        const lefts: number[] = p.windows
          .filter((w: QuotaWindow) => w.kind === kind)
          .map((w: QuotaWindow) => leftOf(w, source, now))
          .filter((x: number | null): x is number => x !== null);
        if (!lefts.length) continue;
        const left: number = Math.min(...lefts);
        if (left < TIGHT) {
          const k = `${kind}|${where}|${left <= SPENT ? 'out' : 'low'}`;
          tight.set(k, (tight.get(k) ?? new Set<string>()).add(a.name));
        }
      }
    }
  }
  const lines: BriefLine[] = alert ? [{ tone: 'faint', text: alert }] : [];
  const offKeys: string[] = [...off.keys()].sort((x: string, y: string) => OFF_ORDER.indexOf(x.split('|')[0]) - OFF_ORDER.indexOf(y.split('|')[0]));
  for (const k of offKeys) {
    const [st, where] = k.split('|');
    const e = off.get(k)!;
    const when: string = e.back && st !== 'login_required' ? ` · ${whenWords(e.back)} 恢复` : '';
    lines.push({ tone: 'warn', text: `${e.names.size} 个号${where ? ` ${where}` : ''}${OFF_WORDS[st] ?? '用不了'}${when}` });
  }
  const tightKeys: string[] = [...tight.keys()].sort((x: string, y: string) => (x.startsWith('7d') === y.startsWith('7d') ? 0 : x.startsWith('7d') ? -1 : 1));
  for (const k of tightKeys) {
    const [kind, where, how] = k.split('|');
    const n: number = tight.get(k)!.size;
    const what: string = `${where}${KIND_WORDS[kind as '5h' | '7d']}额度`;
    const who: string = `${n} 个号${where || /^\d/.test(what) ? ' ' : ''}${what}`;
    lines.push({ tone: 'warn', text: how === 'out' ? `${who}用完` : `${who}剩不到 ${Math.round(TIGHT * 100)}%` });
  }
  if (!lines.some((l: BriefLine) => l.tone === 'warn')) lines.push({ tone: 'ok', text: source.total > 0 && source.usable === 0 ? '暂时没有能用的号' : '都够用' });
  return lines;
}

/** 右栏额度块的行：一个来源一行；没有条目或一个来源都没有是空数组（整块不画） */
export function briefRows(data: QuotaData | null | undefined, now: number): BriefRow[] {
  if (!data?.sources?.length) return [];
  return data.sources.map((s: QuotaSource): BriefRow => ({
    id: s.id,
    title: s.title,
    count: s.total > 0 ? `${s.usable}/${s.total} 个能用` : '',
    lines: linesOf(s, now),
  }));
}

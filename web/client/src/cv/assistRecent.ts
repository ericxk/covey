/** 设置页「辅助模型」下面的“后台活最近一次”（T884）：每个后台功能最近一次用了谁、成没成、原因、耗时、什么时候。纯函数（npm run test:assist） */
import type { SettingsAssistRecent } from '@shared/api.interface';
import { ago } from './topicText';

/** 没调模型的原因（core/assist.py NOT_ASKED）：设置里选了不用、选的用不了、刚失败过、被冷却 */
const NOT_ASKED: Set<string> = new Set(['none', 'not_allowed', 'no_backend', 'broken', 'cooling', 'refusal_gate', 'not_one_shot', 'unusable', 'not_enabled']);

/** ok 成了；bad 调了没成（红字）；off 没调模型（灰字） */
export type RecentTone = 'ok' | 'bad' | 'off';

export interface RecentLine {
  key: string;
  /** 功能 */
  feature: string;
  /** 用了谁：Harness · 模型；没问到谁是 — */
  who: string;
  /** 结果 */
  result: string;
  tone: RecentTone;
  /** 原因（成功是空串） */
  why: string;
  /** 耗时 */
  took: string;
  /** 时间（“3 分钟前”，主人点的加“· 你点的”） */
  when: string;
}

export function tookWords(ms: number): string {
  if (!ms || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} 秒`;
}

export function recentLine(r: SettingsAssistRecent, now: number): RecentLine {
  const notAsked: boolean = !r.ok && NOT_ASKED.has(r.code);
  return {
    key: r.feature,
    feature: r.label,
    who: r.name ? `${r.name}${r.model ? ` · ${r.model}` : ''}` : '—',
    result: r.ok ? '成功' : notAsked ? '没调模型' : '没成',
    tone: r.ok ? 'ok' : notAsked ? 'off' : 'bad',
    why: r.ok ? '' : r.words,
    took: notAsked ? '—' : tookWords(r.ms),
    when: `${ago(r.at, now) || '—'}${r.manual ? ' · 你点的' : ''}`,
  };
}

/** 列哪几行、什么顺序（T898：调模型的后台功能只剩分流、整理专项、记忆自己长；Covey 那边是 app/assist.py SHOWN） */
export const SHOWN: readonly string[] = ['track', 'tidy', 'learn'];
const LABEL: Record<string, string> = { track: '分流', tidy: '整理专项', learn: '记忆自己长' };
/** 旧版 Covey 单独记的补话题要点，并进整理专项那一行（整理时先补要点再判断，取较新的一次） */
const ROW_OF: Record<string, string> = { backfill: 'tidy' };

/** 每个功能最近一次一行；旧版 Covey 还发来的、现在已下线（实时归并、巡逻）或不再调模型（看看、晚间候选、整理记忆）的几行不列 */
export function recentLines(rows: readonly SettingsAssistRecent[] | undefined, now: number): RecentLine[] {
  const latest: Map<string, SettingsAssistRecent> = new Map();
  for (const r of rows ?? []) {
    const key: string = ROW_OF[r.feature] ?? r.feature;
    if (!SHOWN.includes(key)) continue;
    const had: SettingsAssistRecent | undefined = latest.get(key);
    if (!had || r.at > had.at) latest.set(key, { ...r, feature: key, label: LABEL[key] ?? r.label });
  }
  return SHOWN.filter((key: string) => latest.has(key)).map((key: string) => recentLine(latest.get(key) as SettingsAssistRecent, now));
}

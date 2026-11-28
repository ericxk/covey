/* 数据约定 v42：账号与额度。Covey 推来的 settings 条目 quota（jsonb）理成页面要的形状，形状不对的字段当没有；
 * 发件 quota_refresh 的 payload 校验。纯函数，单测在 scripts/quota.test.mjs。 */
import {
  QUOTA_KINDS, QUOTA_STATES, type QuotaAccount, type QuotaData, type QuotaKind, type QuotaPool, type QuotaRefresh, type QuotaSource, type QuotaState, type QuotaWindow,
} from '@shared/api.interface';
import { obj, str } from './workbench.util';

const isObj = (v: unknown): boolean => !!v && typeof v === 'object' && !Array.isArray(v);
const seconds = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0);
const count = (v: unknown): number => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : 0);
const cut = (v: unknown, n: number): string => str(v).slice(0, n);
const stateOf = (v: unknown): QuotaState => ((QUOTA_STATES as readonly string[]).includes(str(v)) ? (str(v) as QuotaState) : 'unknown');
const when = (v: unknown): number | null => (seconds(v) > 0 ? seconds(v) : null);

/** 一个来源最多几个号、一个号最多几个池、一个池最多几个窗口、最多几个来源 */
export const QUOTA_MAX = { sources: 20, accounts: 50, pools: 4, windows: 8 } as const;

function windowOf(v: unknown): QuotaWindow | null {
  if (!isObj(v)) return null;
  const w: Record<string, unknown> = obj(v);
  const kind: string = str(w.kind);
  const left: unknown = w.left;
  const out: QuotaWindow = {
    kind: kind === '5h' || kind === '7d' || kind === 'day' ? kind : 'other',
    label: cut(w.label, 12),
    // 读不到是 null，不是 0；超出 0~1 的收回来
    left: typeof left === 'number' && Number.isFinite(left) ? Math.min(1, Math.max(0, left)) : null,
  };
  if (seconds(w.resets_at)) out.resets_at = seconds(w.resets_at);
  if (seconds(w.checked_at)) out.checked_at = seconds(w.checked_at);
  if (typeof w.ceiling === 'number' && Number.isFinite(w.ceiling)) out.ceiling = Math.min(1, Math.max(0, w.ceiling));
  return out;
}

function poolOf(v: unknown): QuotaPool | null {
  if (!isObj(v)) return null;
  const p: Record<string, unknown> = obj(v);
  const windows: QuotaWindow[] = (Array.isArray(p.windows) ? p.windows : [])
    .map(windowOf)
    .filter((w: QuotaWindow | null): w is QuotaWindow => w !== null)
    .slice(0, QUOTA_MAX.windows);
  return { key: cut(p.key, 20), label: cut(p.label, 20), active: p.active === true, state: stateOf(p.state), available_at: when(p.available_at), windows };
}

const EMAIL = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/u;

function accountOf(v: unknown): QuotaAccount | null {
  if (!isObj(v)) return null;
  const a: Record<string, unknown> = obj(v);
  const name: string = cut(a.name, 20);
  if (!name) return null;
  const pools: QuotaPool[] = (Array.isArray(a.pools) ? a.pools : [])
    .map(poolOf)
    .filter((p: QuotaPool | null): p is QuotaPool => p !== null)
    .slice(0, QUOTA_MAX.pools);
  const login: string = str(a.login);
  return { name, ...(login.length <= 80 && EMAIL.test(login) ? { login } : {}), state: stateOf(a.state), available_at: when(a.available_at), pools };
}

function sourceOf(v: unknown): QuotaSource | null {
  if (!isObj(v)) return null;
  const s: Record<string, unknown> = obj(v);
  const id: string = cut(s.id, 60);
  if (!id) return null;
  const accounts: QuotaAccount[] = (Array.isArray(s.accounts) ? s.accounts : [])
    .map(accountOf)
    .filter((a: QuotaAccount | null): a is QuotaAccount => a !== null)
    .slice(0, QUOTA_MAX.accounts);
  const kind: string = str(s.kind);
  const link: string = str(s.link);
  return {
    id,
    kind: ((QUOTA_KINDS as readonly string[]).includes(kind) ? kind : 'command') as QuotaKind,
    title: cut(s.title, 20) || id,
    // 只留 https 链接
    ...(/^https:\/\/\S{1,290}$/u.test(link) ? { link } : {}),
    collected_at: seconds(s.collected_at),
    ttl: Math.max(30, seconds(s.ttl) || 600),
    stale_after: Math.max(30, seconds(s.stale_after) || 600),
    read_at: seconds(s.read_at),
    failing: s.failing === true,
    why: s.failing === true ? cut(s.why, 60) : '',
    usable: count(s.usable),
    total: count(s.total),
    next_available_at: when(s.next_available_at),
    low: s.low === true,
    dry: s.dry === true,
    expand: s.expand === true,
    relogin: (Array.isArray(s.relogin) ? s.relogin : []).filter((x: unknown) => typeof x === 'string').slice(0, QUOTA_MAX.accounts).map((x: string) => x.slice(0, 80)),
    accounts,
  };
}

function refreshOf(v: unknown): QuotaRefresh {
  const r: Record<string, unknown> = obj(v);
  return { at: seconds(r.at), busy: r.busy === true, cooldown: seconds(r.cooldown) || 300 };
}

/** settings 条目 quota 的 data → 页面要的形状；不是对象就是 undefined（页面不画「账号与额度」）；sources 是空的也照给，页面据此收起这一页 */
export function quotaOf(v: unknown): QuotaData | undefined {
  if (!isObj(v)) return undefined;
  const d: Record<string, unknown> = obj(v);
  const sources: QuotaSource[] = (Array.isArray(d.sources) ? d.sources : [])
    .map(sourceOf)
    .filter((s: QuotaSource | null): s is QuotaSource => s !== null)
    .slice(0, QUOTA_MAX.sources);
  const relogin = (Array.isArray(d.relogin) ? d.relogin : [])
    .filter(isObj)
    .map((r: unknown) => ({ source: cut(obj(r).source, 20), name: cut(obj(r).name, 80) }))
    .filter((r: { source: string; name: string }) => r.name !== '')
    .slice(0, QUOTA_MAX.accounts);
  return { at: seconds(d.at), read_at: seconds(d.read_at), relogin, sources, refresh: refreshOf(d.refresh) };
}

/** v42：发件 quota_refresh：不带 topic_key、project_key，payload 是空的 */
export function quotaRefreshShape(payload: Record<string, unknown>, topicKey: string, projectKey: string): { ok: true } | { error: string } {
  if (topicKey || projectKey) return { error: 'quota_refresh 不带 topic_key、project_key' };
  if (Object.keys(payload).length > 0) return { error: 'quota_refresh 的 payload 是空的' };
  return { ok: true };
}

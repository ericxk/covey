import { keepPreviousData, useQueries, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef } from 'react';
import type {
  OutboxRequest,
  ReportResponse,
  RoomResponse,
  SearchHit,
  SettingsResponse,
  TidyResponse,
  StatsResponse,
  UsageResponse,
  SnapshotResponse,
  SnapshotTopic,
  TurnView,
  TurnsResponse, MainMemoryResponse, UpgradeResponse, QuotaResponse, MemoryIndex, MemoryKeyResponse } from '@shared/api.interface';
import { fetchMe, fetchReport, fetchRoom, fetchSearch, fetchSettings, fetchStats, fetchTidy, fetchMemory, fetchMemoryKey, fetchUpgrade, fetchQuota, fetchSnapshot, fetchTurns, fetchUsage, postOutbox } from '@client/src/api/workbench';
import { mergeCollabs } from '@client/src/cv/collab';
import { needsWarm, warmList, warmSig, WARM_STALE_MS } from '@client/src/cv/warmup';
import { calibFailed, calibGotFull, calibHidden, calibOnline, calibStart, calibVisible, isSnapshot, mergeSnapshot, planSnapshot, type Calib } from '@client/src/cv/snapshotMerge';

/**
 * 所有列表来自同一份快照，每 1 秒取一次：带 ?since= 只取变过的话题（数据约定 v38，见 cv/snapshotMerge.ts），整份只在要校准时取
 * （第一次、增量断档、断线重连、网络恢复、在后台待满 30 秒切回来，另有 30 分钟的长兜底：cv/snapshotMerge.ts planSnapshot）；
 * 标签页在后台时 react-query 的定时取数本来就停（refetchIntervalInBackground 默认关），切回前台马上取一次（watchPage）；
 * 旧服务端不认 since，每次都是整份（和以前一样，只是更勤了，所以旧服务端上 1 秒一份整份：699 个话题约 1 MB，库里约 76 毫秒——
 * 要退回去把 POLL_MS 改回 2000）。
 * 话题的轮次在有任务在跑（有人在等它出结果）时也是 1 秒，其余 2 秒；大厅 2 秒。发过操作的话题另由 cv/sendOp.ts 的跟踪器勤取几秒。
 */
export const POLL_MS = 1000;
/** 话题、大厅在不忙时的轮询间隔 */
export const SLOW_POLL_MS = 2000;
const KEEP_MS = 30 * 60 * 1000;

export const SNAPSHOT_KEY = ['snapshot'] as const;
export const turnsKey = (key: string): readonly unknown[] => ['turns', key];

/** 当前登录的人（头像用）；取不到就是 null，页面画中性图标 */
export const useMe = () =>
  useQuery<{ name: string | null }>({ queryKey: ['me'], queryFn: fetchMe, staleTime: Infinity, gcTime: Infinity, retry: 1 });

/** 快照校准的状态（上一次整份的时刻、待校准的理由、切到后台的时刻，cv/snapshotMerge.ts）和上一份响应的游标。游标不放进缓存的数据里：
 * 它每次都变，放进去的话快照没变也会让所有用它的页面每秒重画一遍（数据一样时 react-query 沿用旧对象，页面不动） */
let calib: Calib = calibStart();
let snapCursor: string | null = null;

/** 取快照：能带 since 就取增量并进上一份，并不进去（缓存和响应对不上）就再取一份整份；要不要整份照 planSnapshot。
 * 取失败了记下来（calibFailed），下一次成功的那次取整份 */
export async function fetchSnapshotMerged(qc: QueryClient): Promise<SnapshotResponse> {
  const plan = planSnapshot(snapCursor, !!qc.getQueryData<SnapshotResponse>(SNAPSHOT_KEY), calib, Date.now());
  calib = plan.next;
  const get = async (since: string | null): Promise<SnapshotResponse> => {
    try {
      const r: unknown = await fetchSnapshot(since);
      if (!isSnapshot(r)) throw new Error('快照响应不完整');
      return r;
    } catch (e) {
      calib = calibFailed(calib);
      throw e;
    }
  };
  const res: SnapshotResponse = await get(plan.since);
  snapCursor = res.cursor ?? null;
  let full: boolean = !res.partial;
  let out: SnapshotResponse | null = res.partial ? mergeSnapshot(qc.getQueryData<SnapshotResponse>(SNAPSHOT_KEY), res) : res;
  if (!out) {
    // 并不进去（增量断了档）：再取一份整份
    out = await get(null);
    snapCursor = out.cursor ?? null;
    full = true;
  }
  if (full) calib = calibGotFull(calib, Date.now());
  const { cursor: _cursor, ...data } = out;
  return data;
}

/* 标签页前后台、网络恢复（T876）：切到后台记下时刻；切回前台马上取一次快照和开着的话题、大厅（在后台待满 30 秒的这次快照取整份），
 * 网络恢复时下一次快照取整份。用快照的组件很多，监听只挂一份：第一个挂上、最后一个卸下时摘掉 */
let watchers = 0;
let unwatch: (() => void) | null = null;
/** 取数一律“上一次还没回来就跳过”，不取消在途的请求（同 cv/sendOp.ts） */
const no = { cancelRefetch: false } as const;
function watchPage(qc: QueryClient): () => void {
  watchers += 1;
  if (watchers === 1 && typeof document !== 'undefined') {
    const pull = (): void => {
      void qc.refetchQueries({ queryKey: SNAPSHOT_KEY, type: 'active' }, no);
      void qc.refetchQueries({ queryKey: ['turns'], type: 'active' }, no);
      void qc.refetchQueries({ queryKey: ROOM_KEY, type: 'active' }, no);
    };
    const onVisibility = (): void => {
      if (document.visibilityState === 'hidden') {
        calib = calibHidden(calib, Date.now());
        return;
      }
      calib = calibVisible(calib, Date.now());
      pull();
    };
    const onOnline = (): void => {
      calib = calibOnline(calib);
      void qc.refetchQueries({ queryKey: SNAPSHOT_KEY, type: 'active' }, no);
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('online', onOnline);
    unwatch = (): void => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('online', onOnline);
    };
  }
  return (): void => {
    watchers -= 1;
    if (watchers === 0) {
      unwatch?.();
      unwatch = null;
    }
  };
}

export const useSnapshot = () => {
  const qc: QueryClient = useQueryClient();
  useEffect(() => watchPage(qc), [qc]);
  return useQuery<SnapshotResponse>({
    queryKey: SNAPSHOT_KEY,
    queryFn: () => fetchSnapshotMerged(qc),
    refetchInterval: POLL_MS,
    staleTime: 500,
    gcTime: KEEP_MS,
  });
};

/** 编号 → 话题（正文里 T12 链到它） */
export function useTopicByNumber(): Map<number, SnapshotTopic> {
  const { data } = useSnapshot();
  return useMemo(() => {
    const m = new Map<number, SnapshotTopic>();
    for (const t of data?.topics ?? []) if (t.number !== null && t.kind === 'topic') m.set(t.number, t);
    return m;
  }, [data]);
}

/** 增量取数的起点：最大 seq；最近几轮里还有在跑/等回答的，从它前一轮开始，好让它的最终正文覆盖旧的 */
export function afterOf(prev: TurnsResponse | undefined): number | null {
  if (!prev || !prev.turns.length) return null;
  const turns: TurnView[] = prev.turns;
  const maxSeq: number = turns[turns.length - 1].seq;
  const live: TurnView | undefined = turns.slice(-5).find((t: TurnView) => t.status === 'running' || t.status === 'waiting');
  return live ? Math.min(maxSeq, live.seq - 1) : maxSeq;
}

export async function fetchMerged(qc: QueryClient, key: string): Promise<TurnsResponse> {
  const prev: TurnsResponse | undefined = qc.getQueryData<TurnsResponse>(turnsKey(key));
  const res: TurnsResponse = await fetchTurns(key, afterOf(prev));
  if (!prev) return res;
  const byKey = new Map<string, TurnView>(prev.turns.map((t: TurnView) => [t.key, t]));
  for (const t of res.turns) byKey.set(t.key, t);
  const turns: TurnView[] = [...byKey.values()].sort((a: TurnView, b: TurnView) => a.seq - b.seq);
  // v31：协作增量只带还开着的和最近动过的：按 key 合并进上一份
  const collabs = prev.collabs || res.collabs ? mergeCollabs(prev.collabs, res.collabs) : undefined;
  return { ...res, turns, ...(collabs?.length ? { collabs } : {}) };
}

/** 一个话题的对话：合并进每话题自己的缓存；没有缓存时先拿快照里的头部当占位，不出骨架 */
export function useTurns(key: string) {
  const qc: QueryClient = useQueryClient();
  return useQuery<TurnsResponse>({
    queryKey: turnsKey(key),
    queryFn: () => fetchMerged(qc, key),
    refetchInterval: (q): number => (q.state.data?.turns.some((t: TurnView) => t.status === 'running') ? POLL_MS : SLOW_POLL_MS),
    staleTime: 500,
    gcTime: KEEP_MS,
    placeholderData: (prev: TurnsResponse | undefined): TurnsResponse | undefined => {
      if (prev && prev.topic.key === key) return prev;
      const snap: SnapshotResponse | undefined = qc.getQueryData<SnapshotResponse>(SNAPSHOT_KEY);
      const t: SnapshotTopic | undefined = snap?.topics.find((x: SnapshotTopic) => x.key === key);
      if (!t) return undefined;
      return { topic: { ...t, workspace: '', project_title: null }, turns: [], question: null, outbox: [] };
    },
  });
}

export const ROOM_KEY = ['room'] as const;

/** 大厅的几路轮次：按 cursor 增量取，按 key 合并（同一轮的新内容覆盖旧的），发件取最新的 */
export function useRoomTurns(enabled = true) {
  const qc: QueryClient = useQueryClient();
  return useQuery<RoomResponse>({
    queryKey: ROOM_KEY,
    queryFn: async (): Promise<RoomResponse> => {
      const prev: RoomResponse | undefined = qc.getQueryData<RoomResponse>(ROOM_KEY);
      const res: RoomResponse = await fetchRoom(prev?.cursor ?? null);
      if (!prev || prev.butler_key !== res.butler_key) return res;
      const byKey = new Map<string, TurnView>(prev.turns.map((t: TurnView) => [t.key, t]));
      for (const t of res.turns) byKey.set(t.key, t);
      const collabs = prev.collabs || res.collabs ? mergeCollabs(prev.collabs, res.collabs) : undefined;
      return { ...res, turns: [...byKey.values()], cursor: res.cursor ?? prev.cursor, ...(collabs?.length ? { collabs } : {}) };
    },
    enabled,
    refetchInterval: SLOW_POLL_MS,
    staleTime: 1000,
    gcTime: KEEP_MS,
  });
}

/** 鼠标移上去、手指一按就先取，点进去时已经在缓存里 */
export function prefetchTurns(qc: QueryClient, key: string): void {
  if (!key) return;
  void qc.prefetchQuery({ queryKey: turnsKey(key), queryFn: () => fetchMerged(qc, key), staleTime: 5000, gcTime: KEEP_MS });
}

/** 快照一到就把要紧的话题先取好（等你、在做、没看过的、最近的几个），点进去时已在缓存里。delayMs：非话题页（记忆中心、设置）晚一点再开始，让出首屏 */
export function useWarmup(delayMs = 0): void {
  const qc: QueryClient = useQueryClient();
  const { data } = useSnapshot();
  // 依赖是集合不是顺序（cv/warmup.ts）：在做的话题一推进顺序就变，以前每变一次就把 24 个话题的轮次重取一遍
  const list: string[] = useMemo(() => warmList(data?.topics ?? []), [data]);
  const sig: string = warmSig(list);
  const latest = useRef<string[]>(list);
  latest.current = list;
  useEffect(() => {
    const now: number = Date.now();
    const todo: string[] = latest.current.filter((k: string) => needsWarm(qc.getQueryState(turnsKey(k))?.dataUpdatedAt, now));
    const timers: number[] = todo.map((k: string, i: number) =>
      window.setTimeout(() => void qc.prefetchQuery({ queryKey: turnsKey(k), queryFn: () => fetchMerged(qc, k), staleTime: WARM_STALE_MS, gcTime: KEEP_MS }), delayMs + 150 * i));
    return () => timers.forEach((t: number) => window.clearTimeout(t));
  }, [sig, qc, delayMs]);
}

export const useSearch = (q: string) =>
  useQuery<SearchHit[]>({
    queryKey: ['search', q],
    queryFn: () => fetchSearch(q),
    enabled: q.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: 10000,
  });

export function newOpId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** 不在乎结果的一次性发件（seen）：失败了也不打扰 */
export function postOutboxQuiet(body: OutboxRequest): void {
  postOutbox(body).catch(() => undefined);
}

export const useStats = () =>
  useQuery<StatsResponse>({ queryKey: ['stats'], queryFn: fetchStats, refetchInterval: 15000, staleTime: 10000, gcTime: KEEP_MS });

/** v26：按天的用量。Covey 每分钟推一次，这里一分钟取一次 */
export const useUsage = () =>
  useQuery<UsageResponse | null>({ queryKey: ['usage'], queryFn: fetchUsage, refetchInterval: 60000, staleTime: 30000, gcTime: KEEP_MS });

export const useReport = (key: string) =>
  useQuery<ReportResponse>({
    queryKey: ['report', key],
    queryFn: () => fetchReport(key),
    refetchInterval: 10000,
    staleTime: 5000,
    gcTime: KEEP_MS,
    placeholderData: keepPreviousData,
  });

export const SETTINGS_KEY = ['settings'] as const;
/** v7 设置页：打开时每 10 秒拉一次；改完另外隔 1.5 秒、4 秒各拉一次（见 Settings.tsx） */
export const useSettings = () =>
  useQuery<SettingsResponse | null>({
    queryKey: SETTINGS_KEY,
    queryFn: fetchSettings,
    refetchInterval: 10000,
    staleTime: 3000,
    gcTime: KEEP_MS,
  });

export const TIDY_KEY = ['tidy'] as const;
export const MEMORY_KEY = ['memory'] as const;
/** v16 整理专项的建议：看板上每 10 秒拉一次；点完按钮另外隔 1.5 秒、4 秒各拉一次（见 Tidy.tsx） */
export const useTidy = () =>
  useQuery<TidyResponse>({ queryKey: TIDY_KEY, queryFn: fetchTidy, refetchInterval: 10000, staleTime: 3000, gcTime: KEEP_MS });
/** v37：主记忆（设置页「记忆」）；改了以后 Covey 回执之前就推，页面收到 done 再取一次。
 *  enabled（v43）：收到了 memory:index 就不再取旧键（侧栏、记忆中心都只认新键） */
export const useMemory = (enabled = true) =>
  useQuery<MainMemoryResponse | null>({ queryKey: MEMORY_KEY, queryFn: fetchMemory, enabled, refetchInterval: 30000, staleTime: 3000, gcTime: KEEP_MS });

/** v43：记忆中心的小键 memory:index（总数、待审核角标、各键 rev、体检、⌘K 标题表）。侧栏只轮询它，15 秒一次；
 *  旧服务端不认 ?key= 时回的是 v37 的 memory，version 不是 43 就当没有（页面退回 v37）。查询键挂在 MEMORY_KEY 下，
 *  memory_edit 回执后的取数（opTrack.pullPlan 的 memory）连它和下面各键一起重取 */
export const MEMORY_INDEX_KEY = ['memory', 'memory:index'] as const;
const indexOf = async (): Promise<MemoryKeyResponse<MemoryIndex> | null> => {
  const r = await fetchMemoryKey<MemoryIndex>('memory:index');
  const v: unknown = r?.data ? (r.data as { version?: unknown }).version : undefined;
  return r && r.data && typeof v === 'number' && v >= 43 && Array.isArray(r.data.scopes) ? r : null;  // v43 起（v45 是一页式）
};
export const useMemoryIndex = (enabled = true) =>
  useQuery<MemoryKeyResponse<MemoryIndex> | null>({ queryKey: MEMORY_INDEX_KEY, queryFn: indexOf, enabled, refetchInterval: 15000, staleTime: 3000, gcTime: KEEP_MS, retry: 1 });

/** v43 还是 v37：先取 memory:index，取到了（version 43 或更新，v45 是一页式）就是记忆页；取过了没有才退回取旧键 memory（v37 的设置页「记忆」）。
 *  mode：v43 记忆中心、v37 旧页、off 记忆关着或都没推、loading 还在判断 */
export type MemoryMode = 'v43' | 'v37' | 'off' | 'loading';
export function useMemoryMode(): { mode: MemoryMode; index: MemoryIndex | null } {
  const idx = useMemoryIndex();
  const fallback: boolean = idx.isFetched && !idx.data;
  const old = useMemory(fallback);
  const index: MemoryIndex | null = idx.data?.data ?? null;
  const mode: MemoryMode = index
    ? (index.on ? 'v43' : 'off')
    : !fallback ? 'loading'
      : old.data?.data?.on ? 'v37' : old.isLoading ? 'loading' : 'off';
  return { mode, index };
}

/** v43：记忆中心的一个数据键，按 memory:index.revs 里它的 rev 取：rev 没变就用缓存，不再取（Bridge 那头 rev 没变也不推）。
 *  key 或 rev 是空的不取（index 里没有这个键）；换 rev 时先留着上一份，免得列表闪一下 */
export const useMemoryKey = <T,>(key: string | null, rev: string | undefined) =>
  useQuery<MemoryKeyResponse<T> | null>({
    queryKey: ['memory', key ?? '', rev ?? ''],
    queryFn: () => fetchMemoryKey<T>(key ?? ''),
    enabled: !!key && !!rev,
    staleTime: Infinity,
    gcTime: KEEP_MS,
    retry: 1,
    placeholderData: keepPreviousData,
  });

export const UPGRADE_KEY = ['upgrade'] as const;
/** v39：版本与更新。只在 Covey 认 upgrade 操作（settings.ops）时取：在查、在升级时每 3 秒，其余每分钟 */
export const useUpgrade = (enabled = true) =>
  useQuery<UpgradeResponse | null>({
    queryKey: UPGRADE_KEY,
    queryFn: fetchUpgrade,
    enabled,
    refetchInterval: (q): number => (q.state.data?.data.running || q.state.data?.data.checking ? 3000 : 60000),
    staleTime: 2000,
    gcTime: KEEP_MS,
    retry: 1,
  });

export const QUOTA_KEY = ['quota'] as const;
/** v42：账号与额度。只在 Covey 认 quota_refresh 操作（settings.ops）时取：在重读时每 3 秒，其余每 30 秒（Covey 每分钟写一次快照） */
export const useQuota = (enabled = true) =>
  useQuery<QuotaResponse | null>({
    queryKey: QUOTA_KEY,
    queryFn: fetchQuota,
    enabled,
    refetchInterval: (q): number => (q.state.data?.data.refresh.busy ? 3000 : 30000),
    staleTime: 2000,
    gcTime: KEEP_MS,
    retry: 1,
  });

/** v43：一次取几个记忆键（记忆中心用，键数随专项多少变，所以用 useQueries）。返回 键 → data（没取到是 undefined）和还有没有在取的 */
export function useMemoryKeys(keys: readonly { key: string; rev: string }[]): { data: Record<string, unknown>; loading: boolean } {
  const res = useQueries({
    queries: keys.map((k: { key: string; rev: string }) => ({
      queryKey: ['memory', k.key, k.rev],
      queryFn: () => fetchMemoryKey(k.key),
      staleTime: Infinity,
      gcTime: KEEP_MS,
      retry: 1,
      // 键按名字排好序，同一位置换 rev 时先留着上一份（列表不闪）
      placeholderData: keepPreviousData,
    })),
  });
  const data: Record<string, unknown> = {};
  keys.forEach((k: { key: string; rev: string }, i: number) => {
    const d = res[i]?.data?.data;
    if (d !== undefined) data[k.key] = d;
  });
  return { data, loading: res.some((r) => r.isLoading) };
}

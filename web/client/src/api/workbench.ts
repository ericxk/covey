import { axiosForBackend } from '@lark-apaas/client-toolkit/utils/getAxiosForBackend';
import type {
  HomeResponse,
  LocateResponse,
  OutboxRequest,
  OutboxResponse,
  OutboxStatus,
  ProjectDetailResponse,
  ReportResponse,
  RoomResponse,
  SearchHit,
  SettingsResponse,
  StatsResponse,
  TidyResponse,
  UsageResponse,
  SnapshotResponse,
  TopicDetailResponse,
  TurnsResponse,
  UploadDoneResponse,
  UploadPartRequest,
  UploadStartRequest,
  UploadStartResponse, MainMemoryResponse, UpgradeResponse, QuotaResponse, MemoryKeyResponse,
} from '@shared/api.interface';

export async function fetchMe(): Promise<{ name: string | null }> {
  const res = await axiosForBackend({ url: '/api/me', method: 'GET' });
  return res.data as { name: string | null };
}

/** since：上一份响应的 cursor（v38），只取这之后变过的话题；null 是整份。旧服务端不认 since，回的就是整份 */
export async function fetchSnapshot(since: string | null = null): Promise<SnapshotResponse> {
  const res = await axiosForBackend({ url: '/api/snapshot', method: 'GET', params: since ? { since } : undefined });
  return res.data as SnapshotResponse;
}

/** after：增量的起点（seq）；since：上一份响应的 cursor，这之后改过的轮次也一起回来（旧服务端不认，照旧只按 after） */
export async function fetchTurns(key: string, after: number | null, since: string | null = null): Promise<TurnsResponse> {
  const res = await axiosForBackend({
    url: `/api/topics/${encodeURIComponent(key)}/turns`,
    method: 'GET',
    params: after === null ? undefined : { after, ...(since ? { since } : {}) },
  });
  return res.data as TurnsResponse;
}

/** 大厅：after 是上次的 cursor，只取这之后改过的 */
export async function fetchRoom(after: string | null): Promise<RoomResponse> {
  const res = await axiosForBackend({ url: '/api/room', method: 'GET', params: after ? { after } : undefined });
  return res.data as RoomResponse;
}

export async function fetchSearch(q: string): Promise<SearchHit[]> {
  const res = await axiosForBackend({ url: '/api/search', method: 'GET', params: { q, limit: 30 } });
  return res.data as SearchHit[];
}

/* 旧接口（服务端仍保留，页面不再用） */
export async function fetchHome(): Promise<HomeResponse> {
  const res = await axiosForBackend({ url: '/api/home', method: 'GET' });
  return res.data as HomeResponse;
}
export async function fetchProject(key: string): Promise<ProjectDetailResponse> {
  const res = await axiosForBackend({ url: `/api/projects/${encodeURIComponent(key)}`, method: 'GET' });
  return res.data as ProjectDetailResponse;
}
export async function fetchTopic(key: string): Promise<TopicDetailResponse> {
  const res = await axiosForBackend({ url: `/api/topics/${encodeURIComponent(key)}`, method: 'GET' });
  return res.data as TopicDetailResponse;
}

export async function postOutbox(body: OutboxRequest): Promise<OutboxResponse> {
  const res = await axiosForBackend({ url: '/api/outbox', method: 'POST', data: body });
  return res.data as OutboxResponse;
}

export async function fetchStats(): Promise<StatsResponse> {
  const res = await axiosForBackend({ url: '/api/stats', method: 'GET' });
  return res.data as StatsResponse;
}
/** v26：用量页的按天用量；Covey 还没推按天的是 null */
export async function fetchUsage(): Promise<UsageResponse | null> {
  const res = await axiosForBackend({ url: '/api/usage', method: 'GET' });
  return (res.data as UsageResponse | null) ?? null;
}
export async function fetchReport(key: string): Promise<ReportResponse> {
  const res = await axiosForBackend({ url: `/api/projects/${encodeURIComponent(key)}/report`, method: 'GET' });
  return res.data as ReportResponse;
}

/** v31：记忆的“查看原文”：那道题、那条消息在哪个话题的哪一轮；找不到是 404 */
export async function fetchLocate(q: { message_id?: string; question?: string }): Promise<LocateResponse> {
  const res = await axiosForBackend({ url: '/api/locate', method: 'GET', params: q });
  return res.data as LocateResponse;
}

/** wait（秒，v38）：新服务端还没回执就挂着等，旧服务端不认、马上回 */
export async function fetchOutboxStatus(opId: string, wait = 0): Promise<OutboxStatus> {
  const res = await axiosForBackend({ url: `/api/outbox/${encodeURIComponent(opId)}`, method: 'GET', params: wait > 0 ? { wait } : undefined });
  return res.data as OutboxStatus;
}

/** v7：设置快照；Covey 还没推过时服务端返回 null（空响应体），这里统一成 null */
export async function fetchSettings(): Promise<SettingsResponse | null> {
  const res = await axiosForBackend({ url: '/api/settings', method: 'GET' });
  const d = res.data as SettingsResponse | '' | null;
  return d && typeof d === 'object' && d.data ? d : null;
}

/** v37：主记忆；Covey 还没推过时是 null（空响应体） */
export async function fetchMemory(): Promise<MainMemoryResponse | null> {
  const res = await axiosForBackend({ url: '/api/memory', method: 'GET' });
  const d = res.data as MainMemoryResponse | '';
  return d && typeof d === 'object' && d.data && typeof d.data === 'object' ? d : null;
}

/** v43：记忆中心的一键（memory:index、memory:global、memory:p:<专项>…）；Covey 没推过时是 null（空响应体）。
 *  旧服务端不认 ?key=，回的是 v37 的 memory：形状对不上由调用方（memoryCenter.ts 的 isMemoryIndex 等）当没有 */
export async function fetchMemoryKey<T = unknown>(key: string): Promise<MemoryKeyResponse<T> | null> {
  const res = await axiosForBackend({ url: '/api/memory', method: 'GET', params: { key } });
  const d = res.data as MemoryKeyResponse<T> | '';
  return d && typeof d === 'object' && d.data && typeof d.data === 'object' ? d : null;
}

/** v39：版本与更新；Covey 没推过时是 null（空响应体） */
export async function fetchUpgrade(): Promise<UpgradeResponse | null> {
  const res = await axiosForBackend({ url: '/api/upgrade', method: 'GET' });
  const d = res.data as UpgradeResponse | '';
  return d && typeof d === 'object' && d.data && typeof d.data === 'object' ? d : null;
}

/** v42：账号与额度；Covey 没推过时是 null（空响应体） */
export async function fetchQuota(): Promise<QuotaResponse | null> {
  const res = await axiosForBackend({ url: '/api/quota', method: 'GET' });
  const d = res.data as QuotaResponse | '';
  return d && typeof d === 'object' && d.data && typeof d.data === 'object' ? d : null;
}

/** v16：整理专项的建议；Covey 还没推过时是 null（空响应体） */
export async function fetchTidy(): Promise<TidyResponse> {
  const res = await axiosForBackend({ url: '/api/tidy', method: 'GET' });
  const d = res.data as TidyResponse | '';
  return d && typeof d === 'object' && Array.isArray(d.proposals) ? d : null;
}

/* v20：输入框附件，分段传 */
export async function uploadStart(body: UploadStartRequest): Promise<UploadStartResponse> {
  const res = await axiosForBackend({ url: '/api/uploads', method: 'POST', data: body });
  return res.data as UploadStartResponse;
}
export async function uploadPart(id: string, body: UploadPartRequest): Promise<void> {
  await axiosForBackend({ url: `/api/uploads/${encodeURIComponent(id)}/parts`, method: 'POST', data: body });
}
export async function uploadDone(id: string): Promise<UploadDoneResponse> {
  const res = await axiosForBackend({ url: `/api/uploads/${encodeURIComponent(id)}/done`, method: 'POST' });
  return res.data as UploadDoneResponse;
}

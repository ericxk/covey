// 设置页「设备」页（v46）的纯函数：一台机器一行（devices.json 和监控节点按别名并成一行）的点、几行灰字，
// 添加、改、装监控节点的 payload。不碰 DOM，测试直接导入（scripts/devices-page.test.mjs）。
import type { SettingsDeviceRow, SettingsNodeDot, SettingsNodesPage } from '@shared/api.interface';

export const NAME = /^[a-z][a-z0-9-]{0,23}$/u;
export const TITLE_MAX = 40;
export const NOTE_MAX = 60;
export const SSH_MAX = 200;
export type Access = 'write' | 'read';

/** Covey 推了 v46 的 rows 才画新页（旧 Covey 照旧画节点列表） */
export const hasRows = (p: SettingsNodesPage): boolean => Array.isArray(p.rows);

/** 可写还是只读的说法；devices.json 没写的是“没设” */
export function accessText(a: SettingsDeviceRow['access']): string {
  return a === 'write' ? '可写' : a === 'read' ? '只读' : '没设';
}

/** 行首的点：有节点看节点；没节点看上次经 ssh 连没连上；都没有是灰点 */
export function rowDot(r: SettingsDeviceRow): SettingsNodeDot {
  if (r.checking?.state === 'checking') return 'never';
  if (r.node) return r.node.dot;
  if (r.link?.state === 'ok') return 'ok';
  if (r.link?.state === 'down') return 'down';
  return 'never';
}

/** 体检在跑时点在闪 */
export const busy = (r: SettingsDeviceRow): boolean => r.checking?.state === 'checking';

/** 行上的小标签：可写 / 只读 / 没设（名单里的），没有 ssh 的写“只能看状况”，名单里没有的写“只有监控” */
export function rowTags(r: SettingsDeviceRow): string[] {
  if (r.local) return ['Covey 所在'];
  if (!r.listed) return ['只有监控'];
  return r.runs ? [accessText(r.access)] : [accessText(r.access), '只能看状况'];
}

/** 连接这一行：ssh 写法 · 连得上 · 2 分钟前；没有 ssh 不写 */
export function linkLine(r: SettingsDeviceRow): string {
  if (!r.runs) return '';
  return [r.ssh ? `ssh ${r.ssh}` : '', r.link?.text ?? ''].filter(Boolean).join(' · ');
}

/** 监控这一行：节点在线的写它的数字；没装节点写“没装监控” */
export function nodeLine(r: SettingsDeviceRow): string {
  if (!r.node) return r.local ? '' : '没装监控';
  return `监控 ${r.node.line || (r.node.dot === 'down' ? '失联' : '在线')}`;
}

/** 体检这一行：在跑写“体检中…”；刚点的结果、上次的结果（带多久前），从没体检过写“还没体检” */
export function checkLine(r: SettingsDeviceRow, now: number, agoOf: (sec: number | null | undefined, now: number) => string): { text: string; bad: boolean } {
  if (!r.listed || !r.runs) return { text: '', bad: false };
  if (r.checking?.state === 'checking') return { text: '体检中…（ssh、设备端小程序、hello）', bad: false };
  if (r.checking?.state === 'failed') return { text: `体检没过：${r.checking.message}`, bad: true };
  if (!r.check) return { text: '还没体检', bad: false };
  const when: string = agoOf(r.check.at, now);
  if (r.check.ok) return { text: `体检通过${when ? ` · ${when}` : ''}`, bad: false };
  return { text: `体检没过${when ? `（${when}）` : ''}：${r.check.text}`, bad: true };
}

/** 能不能点“装监控节点”：有 ssh、还没节点、不是本机，且 Covey 在听节点 */
export const canInstallNode = (r: SettingsDeviceRow, canAdd: boolean): boolean => canAdd && r.runs && !r.node && !r.local;

/** 装监控节点：用现有的 node_add（经这台设备的 ssh 登上去执行加入命令） */
export const nodeAddPayload = (r: SettingsDeviceRow): Record<string, unknown> => ({ what: 'node_add', name: r.alias, title: r.title.slice(0, TITLE_MAX), ssh: r.ssh });

const line = (s: string, max: number): string => Array.from(s.replace(/\s+/gu, ' ').trim()).slice(0, max).join('');

export interface DeviceDraft { name: string; title: string; ssh: string; note: string; access: Access }
export const emptyDraft = (): DeviceDraft => ({ name: '', title: '', ssh: '', note: '', access: 'read' });

/** device_add：名字不对返回 null；没填 ssh 就是只看状况的机器；新加的默认只读 */
export function deviceAddPayload(d: DeviceDraft): Record<string, unknown> | null {
  const name: string = d.name.trim();
  if (!NAME.test(name)) return null;
  const out: Record<string, unknown> = { what: 'device_add', name, access: d.access };
  if (d.title.trim()) out.title = line(d.title, TITLE_MAX);
  if (d.note.trim()) out.note = line(d.note, NOTE_MAX);
  if (d.ssh.trim()) out.ssh = d.ssh.trim().slice(0, SSH_MAX);
  return out;
}

/** 添加按钮的字：填了 ssh 先体检，没填直接加 */
export const addLabel = (d: DeviceDraft): string => (d.ssh.trim() ? '体检并添加' : '添加');

export interface EditDraft { title: string; note: string; ssh: string }
export const editDraftOf = (r: SettingsDeviceRow): EditDraft => ({ title: r.title, note: r.note, ssh: r.ssh });

/** device_set：只带改了的；ssh 清空不算改（网页上不去掉 ssh）；没有改动返回 null */
export function deviceSetPayload(r: SettingsDeviceRow, d: EditDraft): Record<string, unknown> | null {
  const out: Record<string, unknown> = { what: 'device_set', alias: r.alias };
  let n = 0;
  if (line(d.title, TITLE_MAX) !== r.title && d.title.trim()) { out.title = line(d.title, TITLE_MAX); n++; }
  if (line(d.note, NOTE_MAX) !== r.note) { out.note = line(d.note, NOTE_MAX); n++; }
  if (d.ssh.trim() && d.ssh.trim() !== r.ssh) { out.ssh = d.ssh.trim().slice(0, SSH_MAX); n++; }
  return n ? out : null;
}

/** 可写 / 只读开关 */
export const accessPayload = (r: SettingsDeviceRow, access: Access): Record<string, unknown> => ({ what: 'device_set', alias: r.alias, access });

/** 移除的确认文案：说清楚留什么、撤什么 */
export function removeAsk(r: SettingsDeviceRow): string {
  const parts: string[] = [];
  if (r.listed) parts.push('从设备名单去掉，它的体检记录留着');
  if (r.node) parts.push('撤销它的监控节点');
  return `移除 ${r.title}？${parts.join('，')}`;
}

/* memory_edit 的 payload 校验（v37 旧版 + v43 新版）和 v43 的仓库校验（settings 的 repos_*、启动与换配置的 repos）。
 * 纯函数，单测在 scripts/memory-v43.test.mjs。v37 的校验原样搬来，一字未改。 */
import { BadRequestException } from '@nestjs/common';
import { MEMORY_EDIT_ACTIONS_V43, MEMORY_KINDS, MEMORY_SCOPES, type OutboxRequest, type TurnMemory, type TurnMemoryUsed } from '@shared/api.interface';
import { str } from './workbench.util';

const len = (v: unknown): number => Array.from(str(v)).length;
const isStr = (v: unknown): v is string => typeof v === 'string';

/* v43：memory_edit 的新 payload（payload.v === 43）。只挡形状不对的；能不能记、有没有冲突由 Covey 回执说 */
const MEMORY_FIELDS_V43: readonly string[] = ['v', 'action', 'keys', 'title', 'text', 'why', 'kind', 'scope', 'project_key', 'topic_key', 'repos', 'undo_token', 'base'];
const NO_KEYS_V43: readonly string[] = ['add', 'accept_all', 'reject_all', 'undo'];

function checkMemoryEditV43(payload: Record<string, unknown>): void {
  if (Object.keys(payload).some((k: string) => !MEMORY_FIELDS_V43.includes(k))) throw new BadRequestException('memory_edit 有不认识的字段');
  const action: string = str(payload.action);
  if (!(MEMORY_EDIT_ACTIONS_V43 as readonly string[]).includes(action)) throw new BadRequestException('memory_edit 的 action 不认识');
  const keys: unknown = payload.keys;
  if (!Array.isArray(keys) || !keys.every((k: unknown) => isStr(k) && k.length > 0 && len(k) <= 200)) throw new BadRequestException('memory_edit 的 keys 要是文字列表（每个 1–200 字）');
  if (keys.length > 200) throw new BadRequestException('memory_edit 的 keys 最多 200 个');
  if (!keys.length && !NO_KEYS_V43.includes(action)) throw new BadRequestException('memory_edit 要 keys');
  for (const [k, max] of [['title', 40], ['why', 200], ['project_key', 200], ['topic_key', 200], ['base', 200]] as const) {
    if (payload[k] !== undefined && (!isStr(payload[k]) || len(payload[k]) > max)) throw new BadRequestException(`memory_edit 的 ${k} 最多 ${max} 字`);
  }
  if (payload.kind !== undefined && !(MEMORY_KINDS as readonly string[]).includes(str(payload.kind))) throw new BadRequestException('memory_edit 的 kind 不认识');
  if (payload.scope !== undefined && !(MEMORY_SCOPES as readonly string[]).includes(str(payload.scope))) throw new BadRequestException('memory_edit 的 scope 不认识');
  if (payload.text !== undefined) {
    const max: number = payload.kind === 'decision' ? 600 : 200;
    if (!isStr(payload.text) || len(payload.text) > max) throw new BadRequestException(`一条${payload.kind === 'decision' ? '决定' : '记忆'}最多 ${max} 字`);
  }
  if (payload.repos !== undefined) {
    const r: unknown = payload.repos;
    if (!Array.isArray(r) || r.length > 10 || !r.every((x: unknown) => isStr(x) && x.length > 0 && len(x) <= 64)) throw new BadRequestException('memory_edit 的 repos 最多 10 个、每个 1–64 字');
  }
  if (payload.undo_token !== undefined && (!isStr(payload.undo_token) || len(payload.undo_token) > 64)) throw new BadRequestException('memory_edit 的 undo_token 最多 64 字');
  if (action === 'undo' && !str(payload.undo_token)) throw new BadRequestException('undo 要 undo_token');
  if (action === 'add') {
    if (!str(payload.title).trim() || !str(payload.text).trim()) throw new BadRequestException('add 要 title 和 text');
    if (!payload.kind || !payload.scope) throw new BadRequestException('add 要 kind 和 scope');
  }
}

/* v43：启动、换配置带的 repos（可选）：≤10 项，name 1–64 字，mode 是 read 或 write。返回错误文字，没问题返回 null */
export function launchReposError(v: unknown): string | null {
  if (v === undefined) return null;
  if (!Array.isArray(v) || v.length > 10) return 'repos 最多 10 项';
  for (const r of v as unknown[]) {
    const o = r as { name?: unknown; mode?: unknown } | null;
    if (!o || typeof o !== 'object' || !isStr(o.name) || !o.name || len(o.name) > 64) return 'repos 的 name 要是 1–64 字的文字';
    if (o.mode !== 'read' && o.mode !== 'write') return 'repos 的 mode 要是 read 或 write';
  }
  return null;
}

/* v43：settings 的 repos_set、repos_add、repos_remove、worktrees_prune。返回错误文字，没问题返回 null */
export function reposSettingsError(what: string, payload: Record<string, unknown>): string | null {
  const tooLong = (k: string, max: number): boolean => payload[k] !== undefined && (!isStr(payload[k]) || len(payload[k]) > max);
  if (what === 'repos_set') {
    if (!str(payload.name) || len(payload.name) > 64) return 'repos_set 要 name（最多 64 字）';
    if (tooLong('note', 200)) return 'repos_set 的 note 最多 200 字';
    if (payload.status !== undefined && !['active', 'readonly', 'frozen'].includes(str(payload.status))) return 'repos_set 的 status 要是 active、readonly 或 frozen';
    if (payload.write_mode !== undefined && !['worktree', 'inplace'].includes(str(payload.write_mode))) return 'repos_set 的 write_mode 要是 worktree 或 inplace';
  } else if (what === 'repos_add') {
    if (!str(payload.path).trim() || len(payload.path) > 500) return 'repos_add 要 path（最多 500 字）';
    if (tooLong('name', 64)) return 'repos_add 的 name 最多 64 字';
    if (tooLong('note', 200)) return 'repos_add 的 note 最多 200 字';
    if (payload.device !== undefined && payload.device !== '' && !DEVICE_ALIAS.test(str(payload.device))) return 'repos_add 的 device 要是设备名';
  } else if (what === 'repos_remove') {
    if (!str(payload.name) || len(payload.name) > 64) return 'repos_remove 要 name（最多 64 字）';
  } else if (what === 'worktrees_prune') {
    const p: unknown = payload.paths;
    if (!Array.isArray(p) || !p.every((x: unknown) => isStr(x) && x.length > 0 && len(x) <= 500)) return 'worktrees_prune 要 paths（文字列表，每个 1–500 字）';
    if (p.length > 50) return 'worktrees_prune 的 paths 最多 50 个';
  }
  return null;
}

const DEVICE_ALIAS = /^[a-z][a-z0-9-]{0,23}$/u;

/* v46：settings 的 device_add、device_set、device_remove、device_check（设备名单 devices.json）。返回错误文字，没问题返回 null。
   不收密码、私钥：ssh 只是地址（~/.ssh/config 的别名或 user@host 加选项），能不能连由 Covey 体检说 */
export function devicesSettingsError(what: string, payload: Record<string, unknown>): string | null {
  const line = (k: string, max: number): boolean =>
    payload[k] !== undefined && (!isStr(payload[k]) || len(payload[k]) > max || /[\r\n]/u.test(payload[k] as string));
  const key: string = what === 'device_add' ? 'name' : 'alias';
  if (!DEVICE_ALIAS.test(str(payload[key]))) return `${what} 要 ${key}：小写字母开头，只用小写字母、数字和 -，最多 24 个`;
  if (what === 'device_remove' || what === 'device_check') return null;
  if (line('title', 40)) return `${what} 的 title 最多 40 字、一行`;
  if (line('note', 60)) return `${what} 的 note 最多 60 字、一行`;
  if (line('ssh', 200)) return `${what} 的 ssh 最多 200 字、一行`;
  if (payload.access !== undefined && payload.access !== 'write' && payload.access !== 'read') return `${what} 的 access 要是 write 或 read`;
  for (const k of ['password', 'key', 'private_key', 'identity']) if (payload[k] !== undefined) return '不收密码和私钥：钥匙留在 ~/.ssh 里';
  return null;
}

/* v37：memory_edit 的 payload（改主记忆，Covey 的 app/web_memory.py edit）。不带 topic_key、project_key；正文最多 200 字（和 Covey 一样，
   这里先拦下明显不对的，具体能不能记、有没有重复由 Covey 回执说） */
const MEMORY_ACTIONS: readonly string[] = ['add', 'edit', 'pin', 'unpin', 'delete', 'keep', 'drop'];
const MEMORY_KEY = /^(principle:p[0-9a-f]{6}|knowledge:k[0-9a-f]{6}|candidate:c[0-9a-f]{8})$/u;
const MEMORY_FIELDS: readonly string[] = ['action', 'key', 'what', 'scope', 'workspace', 'text', 'base', 'knowledge_kind', 'why'];
const MEMORY_TEXT = 200;

export function checkMemoryEdit(payload: Record<string, unknown>, req: OutboxRequest): void {
  if (str(req.topic_key) || str(req.project_key)) throw new BadRequestException('memory_edit 不带 topic_key、project_key');
  // v43：带 v: 43 走新校验；没有 v 的是 v37 旧网页，下面一字未改
  if (payload.v === 43) return checkMemoryEditV43(payload);
  if (Object.keys(payload).some((k: string) => !MEMORY_FIELDS.includes(k))) throw new BadRequestException('memory_edit 有不认识的字段');
  const action: string = str(payload.action);
  if (!MEMORY_ACTIONS.includes(action)) throw new BadRequestException('memory_edit 的 action 不认识');
  const long = (k: string): boolean => payload[k] !== undefined && Array.from(str(payload[k])).length > MEMORY_TEXT;
  if (['text', 'base', 'why'].some(long)) throw new BadRequestException(`一条记忆最多 ${MEMORY_TEXT} 字`);
  if (str(payload.workspace).length > 200 || str(payload.knowledge_kind).length > 20) throw new BadRequestException('memory_edit 的字段太长');
  if (action === 'add') {
    if (payload.what !== 'principle' && payload.what !== 'knowledge') throw new BadRequestException('要选记成原则还是知识条目');
    if (payload.scope !== 'global' && payload.scope !== 'workspace') throw new BadRequestException('要选记在全局还是某个工作区');
    if (payload.scope === 'workspace' && !str(payload.workspace).trim()) throw new BadRequestException('要选记在哪个工作区');
    if (!str(payload.text).trim()) throw new BadRequestException('记忆的正文是空的');
    return;
  }
  if (!MEMORY_KEY.test(str(payload.key))) throw new BadRequestException('memory_edit 的 key 不合法');
  if (action === 'edit' && !str(payload.text).trim()) throw new BadRequestException('记忆的正文是空的');
}

/** v43：一轮的 memory（Covey app/web_turns.py memory_of：带了几条、说用到的 {code,title}（旧的只有标题）、带的全部短码），只留认得的形状；没有或不像就是 null */
export function turnMemoryOf(v: unknown): TurnMemory | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  const count: number = typeof o.count === 'number' && Number.isFinite(o.count) && o.count >= 0 ? Math.floor(o.count) : 0;
  const texts = (x: unknown, most: number, chars: number): string[] =>
    (Array.isArray(x) ? x : []).filter((t): t is string => typeof t === 'string' && t.length > 0).slice(0, most).map((t: string) => t.slice(0, chars));
  // used：新 Bridge 发 [{code, title}]（code 可能是空串），旧 Bridge 只发标题字符串；两种都收，转成对象
  const used: TurnMemoryUsed[] = (Array.isArray(o.used) ? o.used : [])
    .map((u: unknown): TurnMemoryUsed | null => {
      if (typeof u === 'string') return u ? { code: '', title: u.slice(0, 80) } : null;
      if (!u || typeof u !== 'object' || Array.isArray(u)) return null;
      const r = u as Record<string, unknown>;
      const title: string = typeof r.title === 'string' ? r.title.slice(0, 80) : '';
      const code: string = typeof r.code === 'string' ? r.code.slice(0, 16) : '';
      return title || code ? { code, title } : null;
    })
    .filter((u: TurnMemoryUsed | null): u is TurnMemoryUsed => u !== null)
    .slice(0, 40);
  const codes: string[] = texts(o.codes, 40, 16);
  if (!count && !used.length && !codes.length) return null;
  return { count, used, codes };
}

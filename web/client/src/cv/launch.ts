import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import type { LaunchData, LaunchModel, LaunchPayload, LaunchWorkspace, SayFile, SettingsOption, SnapshotResponse } from '@shared/api.interface';
import { newOpId, SNAPSHOT_KEY, useSettings } from '@client/src/hooks/use-workbench';
import { pref, setPref } from './lib';
import { isRepoMode, launchPayload, repoLabel } from './launchRepos';
import { opWords } from './opWords';
import { sendOp, type OpResult } from './sendOp';

/* ---------- 联动（和飞书启动卡一样，只有这几条；v17） ---------- */
export type Sel = Omit<LaunchPayload, 'text'>;

const modelOf = (d: LaunchData, key: string): LaunchModel | undefined => d.models.find((m: LaunchModel) => m.key === key);
const workspaceOf = (d: LaunchData, name: string): LaunchWorkspace | undefined => (d.workspaces ?? []).find((w: LaunchWorkspace) => w.name === name);

/** 一项选择画成什么（设置页「配置」和启动、换配置面板共用）：只有一个选项是只读文字，其余一律下拉（含关/开、原目录/独立 worktree） */
export function pickKind(options: SettingsOption[]): 'one' | 'sel' {
  return options.length <= 1 ? 'one' : 'sel';
}

/** 规则 3：换了模型，思考预算取新模型的 start_budget；工作模式取第一个（没有就空） */
export function withModel(m: LaunchModel, sel: Sel): Sel {
  return { ...sel, model: m.key, effort: m.start_budget, work_mode: m.work_modes[0] ?? '' };
}

/** 选项 = models 里出现过的值，按出现顺序 */
function uniq(d: LaunchData, pick: (m: LaunchModel) => [string, string], keep: (m: LaunchModel) => boolean = () => true): SettingsOption[] {
  const out: SettingsOption[] = [];
  for (const m of d.models) {
    if (!keep(m)) continue;
    const [value, text] = pick(m);
    if (!out.some((o: SettingsOption) => o.value === value)) out.push({ value, text });
  }
  return out;
}
export const harnessOptions = (d: LaunchData): SettingsOption[] => uniq(d, (m: LaunchModel) => [m.harness, m.harness_title]);
export const providerOptions = (d: LaunchData, harness: string): SettingsOption[] =>
  uniq(d, (m: LaunchModel) => [m.provider, m.provider_title], (m: LaunchModel) => m.harness === harness);
export const modelOptions = (d: LaunchData, harness: string, provider: string): SettingsOption[] =>
  uniq(d, (m: LaunchModel) => [m.key, m.label], (m: LaunchModel) => m.harness === harness && m.provider === provider);

/** 规则 2：换 Harness */
export function pickHarness(d: LaunchData, sel: Sel, harness: string): Sel {
  const cur: LaunchModel | undefined = modelOf(d, sel.model);
  if (cur?.harness === harness) return sel;
  const m: LaunchModel | undefined = d.models.find((x: LaunchModel) => x.harness === harness);
  return m ? withModel(m, sel) : sel;
}
/** 规则 2：换供应商 */
export function pickProvider(d: LaunchData, sel: Sel, provider: string): Sel {
  const cur: LaunchModel | undefined = modelOf(d, sel.model);
  if (!cur) return sel;
  const pool: LaunchModel[] = d.models.filter((x: LaunchModel) => x.harness === cur.harness && x.provider === provider);
  if (pool.some((x: LaunchModel) => x.key === cur.key)) return sel;
  return pool[0] ? withModel(pool[0], sel) : sel;
}
export function pickModel(d: LaunchData, sel: Sel, key: string): Sel {
  const m: LaunchModel | undefined = modelOf(d, key);
  return m && m.key !== sel.model ? withModel(m, sel) : sel;
}
/** 规则 4：换工作区 */
export function pickWorkspace(d: LaunchData, sel: Sel, name: string): Sel {
  const ws: LaunchWorkspace | undefined = workspaceOf(d, name);
  let next: Sel = { ...sel, workspace: name };
  if (!ws) return next;
  const cur: LaunchModel | undefined = modelOf(d, sel.model);
  if (ws.harnesses.length && cur && !ws.harnesses.includes(cur.harness)) {
    const m: LaunchModel | undefined = d.models.find((x: LaunchModel) => ws.harnesses.includes(x.harness));
    if (m) next = withModel(m, next);
  }
  if (ws.device) next = { ...next, run_mode: 'direct' };
  return next;
}
export const onDevice = (d: LaunchData, sel: Sel): boolean => !!workspaceOf(d, sel.workspace)?.device;

/** 一行标签：Harness · 模型 · 思考 <预算> · 工作区 · 运行方式 （有工作模式加一个） */
export function summaryTags(d: LaunchData, sel: Sel): string[] {
  const m: LaunchModel | undefined = modelOf(d, sel.model);
  const out: string[] = [m?.harness_title ?? sel.model, m?.label ?? sel.model];
  out.push(`思考 ${m?.budgets.find((b: SettingsOption) => b.value === sel.effort)?.text ?? sel.effort}`);
  // 卡上的叫法去掉括号里的说明（“自适应（按任务自己定）”→“自适应”）
  if (sel.work_mode) out.push((m?.work_mode_texts?.[sel.work_mode] ?? sel.work_mode).split('（')[0]);
  if (isRepoMode(d)) {
    // 新 Covey：没有工作区和运行方式，也不写执行设备（D2Y4T）；选了仓库写仓库
    if (sel.repos?.length) out.push(repoLabel(sel.repos));
  } else {
    out.push(sel.workspace ?? '', (d.run_modes ?? []).find((r: SettingsOption) => r.value === sel.run_mode)?.text ?? sel.run_mode ?? '');
  }
  return out;
}

/* ---------- 选择的状态：没动过就跟着快照的 preselect，动过就保留到发出；A、B 两处共用 ---------- */
interface Store {
  touched: Sel | null;
  mode: 'butler' | 'new';
  open: boolean;
}
let store: Store = { touched: null, mode: pref('launch_mode', 'butler') === 'new' ? 'new' : 'butler', open: false };
const listeners = new Set<() => void>();
const put = (p: Partial<Store>): void => {
  store = { ...store, ...p };
  listeners.forEach((l: () => void) => l());
};
const useStore = (): Store =>
  useSyncExternalStore(
    (cb: () => void) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => store,
  );

/** late：等满了 Covey 还没回音（不是失败：操作还在发件箱里，Covey 回来会照开；说法同 opWords，P1-C2） */
export type Phase = 'idle' | 'sending' | 'done' | 'failed' | 'late';
export interface LaunchBox {
  data: LaunchData;
  sel: Sel;
  mode: 'butler' | 'new';
  open: boolean;
  setMode: (m: 'butler' | 'new') => void;
  setOpen: (v: boolean) => void;
  edit: (f: (d: LaunchData, s: Sel) => Sel) => void;
  phase: Phase;
  message: string;
  /** 发出；done 时调用 onDone（清输入框）；之后等话题出现在快照里再跳过去 */
  send: (text: string, onDone: () => void, files?: SayFile[]) => void;
}

const sleep = (ms: number): Promise<void> => new Promise<void>((r) => window.setTimeout(r, ms));

/** 页面用的开新话题：没有 launch 块（旧 Covey）是 null */
export function useLaunchBox(): LaunchBox | null {
  const { data: settings } = useSettings();
  const st: Store = useStore();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState<string>('');
  const alive = useRef<boolean>(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const data: LaunchData | undefined = settings?.data?.launch;
  if (!data) return null;
  const kept: Sel | null = st.touched && modelOf(data, st.touched.model) ? st.touched : null;
  const sel: Sel = kept ?? data.preselect;
  const edit = (f: (d: LaunchData, s: Sel) => Sel): void => put({ touched: f(data, sel) });
  const send = (text: string, onDone: () => void, files: SayFile[] = []): void => {
    const value: string = text.trim();
    if (!value || phase === 'sending') return;
    const payload: LaunchPayload = launchPayload(data, sel, value, files, onDevice(data, sel));
    setPhase('sending');
    setMessage('');
    void (async (): Promise<void> => {
      const show = (p: Phase, m: string): void => {
        if (alive.current) {
          setPhase(p);
          setMessage(m);
        }
      };
      // 开话题要先取回附件，回执可能比别的操作慢：多等一会（2 分钟），等满了也不算失败
      const r: OpResult = await sendOp({ op_id: newOpId(), kind: 'launch', payload: { ...payload } }, qc, undefined, () => !alive.current, 120000);
      if (r.phase === 'unsent' || r.phase === 'failed') {
        show('failed', opWords(r.phase, r.message));
        return;
      }
      if (r.phase === 'timeout') {
        show('late', opWords('timeout'));
        return;
      }
      put({ touched: null });
      if (alive.current) onDone();
      show('done', r.message);
      // 等新话题推上来再跳：跟踪器回执后每 0.5 秒取快照，最长 3 秒（opTrack.expectation：快照里有这个 T 号）；
      // 还没到就每 0.5 秒看缓存（快照自己每秒在取），最多等 90 秒（现网第一次验收等了一分多钟）
      const num: number = Number(/T(\d+)/.exec(r.message)?.[1] ?? 0);
      if (!num) return;
      await r.settled;
      for (let k = 0; k < 180 && alive.current; k += 1) {
        const t = qc.getQueryData<SnapshotResponse>(SNAPSHOT_KEY)?.topics.find((x) => x.number === num && x.kind === 'topic');
        if (t) {
          nav(`/topics/${encodeURIComponent(t.key)}`);
          return;
        }
        await sleep(500);
      }
    })();
  };
  return {
    data,
    sel,
    mode: st.mode,
    open: st.open,
    setMode: (m: 'butler' | 'new') => {
      setPref('launch_mode', m);
      put({ mode: m });
      setMessage('');
      setPhase('idle');
    },
    setOpen: (v: boolean) => put({ open: v }),
    edit,
    phase,
    message,
    send,
  };
}

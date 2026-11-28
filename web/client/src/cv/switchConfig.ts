/**
 * 话题页换配置（Covey 数据约定 v20，T616）的纯函数：面板的初值、和话题现在的配置比有没有变化、什么情况网页上换不了。
 * v36 起专项负责人也能换（同飞书专项卡上的「换配置」）：只列能当负责人的模型（bridge_tools），不换工作区和运行方式。
 * 不碰 DOM、不引别的模块（只有类型）：防回归 scripts/switch-config.test.mjs。
 */
import type { LaunchData, LaunchModel, LaunchPreselect, LaunchRepo, TopicConfig, TopicRepo } from '@shared/api.interface';

/* 不引 launchRepos（本文件的单测不带路径别名，只能引类型）：新 Covey 判定和仓库比较在这里留一份，规则同 launchRepos.ts */
const isRepoMode = (d: LaunchData): boolean => !d.workspaces;
const repoKey = (x: LaunchRepo[] | undefined): string => (x ?? []).map((r: LaunchRepo) => `${r.name}:${r.mode}`).sort().join('|');
const sameRepos = (a: LaunchRepo[] | undefined, b: LaunchRepo[] | undefined): boolean => repoKey(a) === repoKey(b);

/** v43：换配置的选择多一个 repos（TS 类型上 TopicConfig 没有；新 Covey 初值取话题的 repos） */
export type SwitchSel = LaunchPreselect & { repos?: LaunchRepo[] };

export const SWITCH_FIELDS = ['model', 'effort', 'work_mode', 'workspace', 'run_mode', 'device', 'repos'] as const;

/** 负责人只能用的模型说不过去时的那句（同 Covey app/lead_harness.py 的 CANNOT_LEAD） */
export const LEAD_ONLY = '负责人只能用能接 Bridge 工具的模型（和管家能选的一样）';

/** 面板的初值：话题的配置，补成这个模型能选的（预算不在列表里取 start_budget，工作模式不在取第一个）。
 *  模型不在目录里（目录变了）原样留着：面板上那一行显示原值，要重选模型才能确认 */
export function fromConfig(d: LaunchData, c: TopicConfig): SwitchSel {
  const m: LaunchModel | undefined = d.models.find((x: LaunchModel) => x.key === c.model);
  if (!m) return { ...c };
  const effort: string = m.budgets.some((b) => b.value === c.effort) ? c.effort : m.start_budget;
  const work_mode: string = m.work_modes.includes(c.work_mode) ? c.work_mode : (m.work_modes[0] ?? '');
  return { ...c, effort, work_mode };
}

/** 负责人的面板用的目录（v36）：只留能当负责人的模型（bridge_tools）；Harness、供应商、模型几行和联动都从它出 */
export function leadData(d: LaunchData): LaunchData {
  return { ...d, models: d.models.filter((m: LaunchModel) => m.bridge_tools === true) };
}

/** 和话题现在的配置比，哪几项变了（比的是补过的初值，所以只是补齐不算变化） */
export function changedFields(base: SwitchSel, sel: SwitchSel): string[] {
  return SWITCH_FIELDS.filter((k) => (k === 'repos' ? !sameRepos(base.repos, sel.repos) : (base[k] ?? '') !== (sel[k] ?? '')));
}

/** 新 Covey：面板初值带上话题的仓库（name+mode）；旧 Covey 或话题没带原样返回 */
export function withTopicRepos(d: LaunchData, base: SwitchSel, repos: TopicRepo[] | undefined): SwitchSel {
  if (!isRepoMode(d) || !repos?.length) return base;
  return { ...base, repos: repos.map((r: TopicRepo) => ({ name: r.name, mode: r.mode })) };
}

/** 网页上不能确认的原因；能确认返回 ''。running：话题在做或排队。
 *  lead（v36，专项负责人）：在做时等它做完（它没有任务卡可停）；模型要能当负责人；工作区和运行方式面板上不画，不查 worktree */
export function switchBlock(d: LaunchData, base: SwitchSel, sel: SwitchSel, running: boolean, lead = false): string {
  if (running) return lead ? '负责人在做时不能换，等它做完' : '在做时不能换，先停止';
  const m: LaunchModel | undefined = d.models.find((x: LaunchModel) => x.key === sel.model);
  if (!m) return '这个模型已不在可选目录里，请重选模型';
  if (lead && m.bridge_tools !== true) return LEAD_ONLY;
  if (!changedFields(base, sel).length) return '';
  if (lead) return '';
  // 原地回到话题现在的 worktree 不用新建；换工作区或从原目录换过去都要新建，只有飞书的换配置卡能建
  // 只有旧 Covey（有 workspaces、run_mode）才有这条；新 Covey 没有运行方式
  if (!isRepoMode(d) && sel.run_mode === 'worktree' && !(base.run_mode === 'worktree' && base.workspace === sel.workspace)) {
    return '新建独立 worktree 要在飞书的换配置卡上做';
  }
  return '';
}

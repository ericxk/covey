import React, { useState } from 'react';
import { ChevronDown, ChevronUp, Eye, PencilLine, Plus } from 'lucide-react';
import type { LaunchModel, LaunchRepo, SettingsOption } from '@shared/api.interface';
import { useSettings } from '@client/src/hooks/use-workbench';
import {
  harnessOptions,
  pickKind,
  modelOptions,
  onDevice,
  pickHarness,
  pickModel,
  pickProvider,
  pickWorkspace,
  providerOptions,
  summaryTags,
  type LaunchBox,
} from './launch';
import {
  cycleRepo,
  isRepoMode,
  MAX_REPOS,
  placeSummary,
  repoChoices,
  repoModeOf,
  type RepoMode,
} from './launchRepos';
import './settings.css';
import './launch.css';

/** 一个选项是只读文字，其余一律下拉（和设置页「配置」同一套，规则在 launch.ts 的 pickKind） */
const Pick: React.FC<{
  label: string;
  options: SettingsOption[];
  value: string;
  onPick: (v: string) => void;
  disabled?: (v: string) => string;
}> = ({ label, options, value, onPick, disabled }) => {
  const kind = pickKind(options);
  if (kind === 'one') return <span className="st-one" title={options[0]?.text ?? value}>{options[0]?.text ?? value}</span>;
  return (
    <select className="st-sel" title={options.find((o: SettingsOption) => o.value === value)?.text} value={value} aria-label={label} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onPick(e.target.value)}>
      {options.some((o: SettingsOption) => o.value === value) ? null : <option value={value}>{value}</option>}
      {options.map((o: SettingsOption) => (
        <option key={o.value} value={o.value} title={disabled?.(o.value) || o.text} disabled={!!disabled?.(o.value)}>{o.text}</option>
      ))}
    </select>
  );
};

/** 话题页换配置（T879）用的：话题在哪台机器上（'' 本机；undefined 是旧 Covey 没说）、仓库这一块改过没有，
 *  负责人的仓库（跟着专项，只读显示） */
export interface SwitchPlace {
  device?: string;
  changed?: boolean;
  leadRepos?: LaunchRepo[];
}

/** 面板只用到的那几样：开新话题的 LaunchBox 是，话题页换配置（v20）也拼得出。
 *  fixedPlace（v36，负责人的换配置）：不画工作区、运行方式两行，选择里照带原值（负责人留在它读的地方），仓库只读。
 *  switching（T879，话题页换配置）：模型几项在上；执行设备只读（换配置换不了机器），设备和仓库默认折成一行摘要 */
export type PanelBox = Pick<LaunchBox, 'data' | 'sel' | 'edit'> & { fixedPlace?: boolean; switching?: SwitchPlace };

const REPO_WORD: Record<RepoMode, string> = { none: '不用', read: '只读', write: '可写' };

/** 仓库选择：一块一个仓库，点一下 不用 → 只读 → 可写 → 不用；已选的实底、有图标和角标，未选的只有边框和加号 */
const RepoPicker: React.FC<{ box: PanelBox; choices: ReturnType<typeof repoChoices> }> = ({ box, choices }) => {
  const picked: number = (box.sel.repos ?? []).length;
  return (
    <div className="lx-repo-box">
      <div className="lx-repo-head">
        <span className="lx-l">仓库</span>
        <span className="lx-repo-count">{picked ? `已选 ${picked} 个` : '未选仓库（从主目录起）'}</span>
        <span className="lx-repo-legend" aria-hidden="true">
          <i className="lx-rk read"><Eye />只读</i>
          <i className="lx-rk write"><PencilLine />可写</i>
          <span>点一下切换，最多 {MAX_REPOS} 个</span>
        </span>
      </div>
      <div className="lx-repos" role="group" aria-label="仓库">
        {choices.map((c) => {
          const mode: RepoMode = repoModeOf(box.sel.repos, c.name);
          return (
            <button
              key={c.name}
              type="button"
              className={`lx-rp ${mode}`}
              aria-pressed={mode !== 'none'}
              aria-label={`${c.name}：${REPO_WORD[mode]}，点一下切换`}
              title={[c.where, c.note || (c.readonly ? '只读仓库：不用 → 只读' : '点一下：不用 → 只读 → 可写')].filter(Boolean).join(' · ')}
              data-mode={mode}
              onClick={() => box.edit((_d, s) => ({ ...s, repos: cycleRepo(s.repos, c.name, c.readonly) }))}
            >
              {mode === 'write' ? <PencilLine /> : mode === 'read' ? <Eye /> : <Plus />}
              <span className="lx-rn">{c.name}</span>
              {mode === 'none' ? null : <span className="lx-rm">{REPO_WORD[mode]}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
};

/** 配置面板：飞书启动卡的各行，A 版展开时、B 版一直在；话题页换配置也用它（v20） */
export const LaunchPanel: React.FC<{ box: PanelBox; className?: string; foot?: React.ReactNode }> = ({ box, className, foot }) => {
  const { data, sel } = box;
  const m: LaunchModel | undefined = data.models.find((x: LaunchModel) => x.key === sel.model);
  const modes: SettingsOption[] = (m?.work_modes ?? []).map((v: string) => ({ value: v, text: m?.work_mode_texts?.[v] ?? v }));
  const device: boolean = onDevice(data, sel);
  const place: boolean = !box.fixedPlace;
  const repoMode: boolean = isRepoMode(data);
  const switching: SwitchPlace | undefined = box.switching;
  // 话题页换配置（新 Covey）：设备和仓库默认折起来，只写摘要；点开才能动仓库（负责人的点不开）
  const folded: boolean = !!switching && repoMode;
  const [open, setOpen] = useState<boolean>(false);
  // 新 Covey：仓库名单来自设置的 repos（出错或没有就不画仓库行）
  const { data: settings } = useSettings();
  const reposPage = settings?.data?.repos;
  const items = reposPage && 'items' in reposPage ? reposPage.items : [];
  const choices = repoMode && place ? repoChoices(items, sel.repos) : [];
  const row = (label: string, body: React.ReactNode, note?: string, wide = false): React.ReactNode => (
    <div className={`lx-r st-cell${['模型', '工作区'].includes(label) ? ' wd' : ''}${wide ? ' lx-full' : ''}`}>
      <div className="lx-l st-l">{label}</div>
      <div className="lx-c">
        {body}
        {note ? <div className="lx-n">{note}</div> : null}
      </div>
    </div>
  );
  // 模型那一组：Harness、供应商、模型、思考预算，模型有的话再加工作模式（换个模型只要改这一组）
  const modelRows = (
    <div className="st-gr cfg">
      {row('Harness', <Pick label="Harness" options={harnessOptions(data)} value={m?.harness ?? ''} onPick={(v: string) => box.edit((d, s) => pickHarness(d, s, v))} />)}
      {row('供应商', <Pick label="供应商" options={providerOptions(data, m?.harness ?? '')} value={m?.provider ?? ''} onPick={(v: string) => box.edit((d, s) => pickProvider(d, s, v))} />)}
      {row('模型', <Pick label="模型" options={modelOptions(data, m?.harness ?? '', m?.provider ?? '')} value={sel.model} onPick={(v: string) => box.edit((d, s) => pickModel(d, s, v))} />)}
      {row('思考预算', <Pick label="思考预算" options={m?.budgets ?? []} value={sel.effort} onPick={(v: string) => box.edit((_d, s) => ({ ...s, effort: v }))} />)}
      {modes.length ? row('工作模式', <Pick label="工作模式" options={modes} value={sel.work_mode} onPick={(v: string) => box.edit((_d, s) => ({ ...s, work_mode: v }))} />) : null}
    </div>
  );
  if (folded && switching) {
    // 换配置：执行设备只读（Covey 的换配置换不了机器，话题在哪台就写哪台，不知道就不写）；负责人的仓库跟着专项
    const lead: boolean = !!box.fixedPlace;
    const sum = placeSummary(data, switching.device, lead ? switching.leadRepos : sel.repos);
    const canOpen: boolean = !lead && choices.length > 0;
    const shown: boolean = canOpen && open;
    const bar = (
      <>
        <span className="lx-fold-sum">
          {sum.device !== null ? (
            <span className="lx-fold-it"><span className="lx-fold-k">执行设备</span><b data-testid="switch-device">{sum.device}</b></span>
          ) : null}
          <span className="lx-fold-it"><span className="lx-fold-k">仓库</span><b data-testid="switch-repos">{sum.repos}</b></span>
        </span>
        <span className={`lx-fold-tag${switching.changed ? ' on' : ''}`}>{switching.changed ? '已改' : '不变'}</span>
        {canOpen ? (
          <span className="lx-fold-go">
            {shown ? '收起' : '调整仓库'}
            {shown ? <ChevronUp /> : <ChevronDown />}
          </span>
        ) : null}
      </>
    );
    return (
      <div className={`lx-panel${className ? ` ${className}` : ''}`}>
        {modelRows}
        <div className={`lx-fold${shown ? ' open' : ''}`}>
          {canOpen ? (
            <button type="button" className="lx-fold-bar" aria-expanded={shown} onClick={() => setOpen(!shown)} data-testid="switch-place">
              {bar}
            </button>
          ) : (
            <div className="lx-fold-bar ro" data-testid="switch-place">{bar}</div>
          )}
          {lead ? <div className="lx-fold-note">负责人用的仓库跟着专项，这里不能换；执行设备也不能换</div> : null}
          {shown ? (
            <div className="lx-fold-body">
              {sum.device !== null ? (
                <div className="lx-fold-ro">
                  <span className="lx-l">执行设备</span>
                  <b>{sum.device}</b>
                  <span className="lx-n">换配置换不了机器；要用别的设备，在话题里用 covey device</span>
                </div>
              ) : null}
              <RepoPicker box={box} choices={choices} />
            </div>
          ) : null}
        </div>
        {foot}
      </div>
    );
  }
  return (
    <div className={`lx-panel${className ? ` ${className}` : ''}`}>
      {modelRows}
      {place && !repoMode ? (
        <div className="st-gr cfg">
          {row(
            '工作区',
            <Pick
              label="工作区"
              options={(data.workspaces ?? []).map((w) => ({ value: w.name, text: w.device ? `${w.name}（${w.device}）` : w.name }))}
              value={sel.workspace ?? ''}
              onPick={(v: string) => box.edit((d, s) => pickWorkspace(d, s, v))}
            />,
          )}
          {row(
            '运行方式',
            <Pick label="运行方式" options={data.run_modes ?? []} value={device ? 'direct' : (sel.run_mode ?? '')} onPick={(v: string) => box.edit((_d, s) => ({ ...s, run_mode: v }))} disabled={(v: string) => (device && v === 'worktree' ? '设备上只能原目录' : '')} />,
            device ? '设备上只能原目录' : undefined,
          )}
        </div>
      ) : null}
      {choices.length ? <RepoPicker box={box} choices={choices} /> : null}
      {foot}
    </div>
  );
};

/** 输入框上沿一行：左边“交给管家 / 开新话题”，开新话题时是当前选择的小标签和“调整” */
export const LaunchLine: React.FC<{ box: LaunchBox }> = ({ box }) => {
  const fresh: boolean = box.mode === 'new';
  return (
    <div className="lx-line">
      <div className="lx-switch" role="radiogroup" aria-label="这句话交给谁">
        <button type="button" role="radio" aria-checked={!fresh} className={fresh ? '' : 'on'} onClick={() => box.setMode('butler')}>交给管家</button>
        <button type="button" role="radio" aria-checked={fresh} className={fresh ? 'on' : ''} onClick={() => box.setMode('new')}>开新话题</button>
      </div>
      {fresh ? (
        <>
          <div className="lx-tags">
            {summaryTags(box.data, box.sel).map((t: string, i: number) => (
              <span className="lx-tag" key={`${i}-${t}`}>{t}</span>
            ))}
          </div>
          <button type="button" className="lx-edit" aria-expanded={box.open} onClick={() => box.setOpen(!box.open)}>
            {box.open ? '收起' : '调整'}
            {box.open ? <ChevronUp /> : <ChevronDown />}
          </button>
        </>
      ) : null}
    </div>
  );
};

/** 发出后的一行：独立 worktree 的提示、回执、失败原因 */
export const LaunchNote: React.FC<{ box: LaunchBox }> = ({ box }) => {
  const worktree: boolean = !isRepoMode(box.data) && box.sel.run_mode === 'worktree' && !onDevice(box.data, box.sel);
  return (
    <>
      {worktree ? <div className="lx-hint">独立 worktree 要在飞书启动卡上点开始（建 worktree 只有启动卡能做）</div> : null}
      {box.phase === 'sending' ? <div className="lx-hint">正在开话题…</div> : null}
      {box.phase === 'failed' ? <div className="lx-hint bad" role="alert">{box.message}</div> : null}
      {box.phase === 'late' ? <div className="lx-hint" role="status">{box.message}</div> : null}
      {box.phase === 'done' && box.message ? <div className="lx-hint" role="status">{box.message}</div> : null}
    </>
  );
};

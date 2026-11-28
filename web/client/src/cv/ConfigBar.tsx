import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import type { LaunchData, LaunchModel, SnapshotTopic, SwitchPayload } from '@shared/api.interface';
import { newOpId, useSettings } from '@client/src/hooks/use-workbench';
import { isRepoMode, repoLabel, sameRepos, switchPayload } from './launchRepos';
import { LaunchPanel, type PanelBox } from './LaunchPanel';
import { engineText } from './lib';
import { opWords } from './opWords';
import { sendOp, type OpResult } from './sendOp';
import { onOpenSwitch, type OpenWhy } from './switchOpen';
import { popPlace, POP_GAP, type PopPlace } from './popPlace';
import { changedFields, fromConfig, leadData, switchBlock, withTopicRepos, type SwitchSel } from './switchConfig';
import './launch.css';
import './composer.css';

const FIELD_WORD: Record<string, string> = {
  model: '模型', effort: '思考预算', work_mode: '工作模式', workspace: '工作区', run_mode: '运行方式', device: '执行设备', repos: '仓库',
};
type Phase = 'idle' | 'sending' | 'failed';

/** 负责人改了配置以后怎样（都开新会话、重读专项的状态） */
const leadNext = (): string => '从负责人下一轮起按新配置做（开新会话，重新读一遍专项的状态）';

/**
 * 话题页输入框下沿的配置栏（截图红框那行，T616）：“工作区 covey · Claude Code · Opus”。
 * 话题带 config（Covey v20）且设置里有开新话题的目录（launch）时，点开是和启动卡同样各行的面板，初值是话题现在的配置，
 * 「确认更换」发 switch，同飞书 🔁 换配置的确认；旧数据没有 config 时只是一行字。
 * 专项负责人（kind=lead，Covey v36 起带 config）也用它，同飞书专项卡上的「换配置」：只列能当负责人的模型（bridge_tools），
 * 不画工作区、运行方式（payload 照带现在的值），从负责人下一轮起生效。管家不用它（管家的配置在设置的「配置」里）。
 */
export const ConfigBar: React.FC<{ topic: SnapshotTopic | undefined; workspace: string; busy: boolean }> = ({ topic, workspace, busy }) => {
  const { data: settings } = useSettings();
  const all: LaunchData | undefined = settings?.data?.launch;
  const lead: boolean = topic?.kind === 'lead';
  const config = topic?.kind === 'topic' || lead ? topic?.config : undefined;
  // 面板的目录：负责人只列能当负责人的模型（Harness、供应商、模型几行和联动都从它出）；普通话题原样
  const data: LaunchData | undefined = useMemo(() => (all && lead ? leadData(all) : all), [all, lead]);
  const qc = useQueryClient();
  const [open, setOpen] = useState<boolean>(false);
  // 怎么打开的（T879）：从「换个模型」（失败卡、看板带 ?switch=1）打开时标题写“换个模型”，设备和仓库照旧折着、不动
  const [why, setWhy] = useState<OpenWhy>(undefined);
  const [sel, setSel] = useState<SwitchSel | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState<string>('');
  const alive = useRef<boolean>(true);
  const wrap = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // 初值按整个目录补（负责人现在的模型不在负责人能用的里面时也认得出它，下面说清为什么不能确认）
  const base: SwitchSel | null = useMemo(() => (all && config ? withTopicRepos(all, fromConfig(all, config), lead ? undefined : topic?.repos) : null), [all, config, lead, topic?.repos]);
  // 没成功的一轮上的「换个模型」（v33 how=switch）直接展开这里，不再让你去飞书点「换配置」；
  // 从看板点过来的带 ?switch=1，面板能用了就展开一次
  const live: boolean = !!base && !!topic;
  const loc = useLocation();
  const asked: boolean = new URLSearchParams(loc.search).get('switch') === '1';
  const [opened, setOpened] = useState<string>('');
  const show = (reason?: OpenWhy): void => {
    setSel(null);
    setPhase('idle');
    setMessage('');
    setWhy(reason);
    setOpen(true);
    window.requestAnimationFrame(() => wrap.current?.scrollIntoView({ block: 'nearest' }));
  };
  useEffect(() => {
    if (!live || !topic) return undefined;
    return onOpenSwitch(topic.key, show);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, topic?.key]);
  useEffect(() => {
    if (live && asked && topic && opened !== `${loc.key}:${topic.key}`) {
      setOpened(`${loc.key}:${topic.key}`);
      show('model');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, asked, topic?.key, loc.key]);
  // 面板放在哪（T789）：按输入框（手机是配置栏那一行 .mcfg）的框固定定位，不受对话区 overflow 裁切；输入框长高、窗口变、滚动时重算
  const [place, setPlace] = useState<PopPlace | null>(null);
  useLayoutEffect(() => {
    if (!open) {
      setPlace(null);
      return undefined;
    }
    const host: HTMLElement | null = wrap.current?.closest<HTMLElement>('.composer, .mcfg') ?? null;
    if (!host) return undefined;
    const gap: number = host.classList.contains('mcfg') ? 6 : POP_GAP;
    const fit = (): void => {
      const next: PopPlace = popPlace(host.getBoundingClientRect(), window.innerHeight, gap);
      setPlace((cur: PopPlace | null) => (cur && cur.left === next.left && cur.width === next.width && cur.bottom === next.bottom && cur.room === next.room ? cur : next));
    };
    fit();
    const ro: ResizeObserver | null = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
    ro?.observe(host);
    window.addEventListener('resize', fit);
    window.addEventListener('scroll', fit, true);
    return () => {
      ro?.disconnect();
      window.removeEventListener('resize', fit);
      window.removeEventListener('scroll', fit, true);
    };
  }, [open]);
  // 点面板外面、按 Esc 收起（发送中不收，免得回执没人看）
  useEffect(() => {
    if (!open) return undefined;
    const away = (e: MouseEvent): void => {
      if (phase !== 'sending' && wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent): void => {
      if (e.key === 'Escape' && phase !== 'sending') setOpen(false);
    };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open, phase]);

  const model: LaunchModel | undefined = all && config ? all.models.find((m: LaunchModel) => m.key === config.model) : undefined;
  // 话题带仓库（新 Covey）时写“仓库 a、b”；没带仓库：新 Covey（没有 workspaces）写“主目录 x”，旧 Covey 照旧写工作区
  const label: string = [
    repoLabel(topic?.repos) || ((config?.workspace ?? workspace) ? `${all && isRepoMode(all) ? '主目录' : '工作区'} ${config?.workspace ?? workspace}` : ''),
    model ? `${model.harness_title} · ${topic?.model_name || model.label}` : engineText(topic),
  ].filter(Boolean).join(' · ');
  if (!all || !data || !config || !base) return <span className="t3 cfg-ro" style={{ fontSize: 12 }}>{label}</span>;

  const cur: SwitchSel = sel ?? base;
  const changed: string[] = changedFields(base, cur);
  const blocked: string = switchBlock(all, base, cur, busy, lead);
  const box: PanelBox = {
    data,
    sel: cur,
    edit: (f) => setSel(f(data, cur)),
    ...(lead ? { fixedPlace: true } : {}),
    // 执行设备只读：话题所在的（config.device，'' 本机；旧 Covey 没推这个键就不写），仓库这块改过没有，负责人的仓库只读显示
    switching: {
      device: config && 'device' in config ? (config.device ?? '') : undefined,
      changed: changed.includes('repos'),
      ...(lead ? { leadRepos: (topic?.repos ?? []).map((r) => ({ name: r.name, mode: r.mode })) } : {}),
    },
  };
  const toggle = (): void => {
    if (!open) {
      setSel(null);
      setPhase('idle');
      setMessage('');
      setWhy(undefined);
    }
    setOpen(!open);
  };
  const forModel: boolean = why === 'model';
  const confirm = (): void => {
    if (!topic || blocked || !changed.length || phase === 'sending') return;
    // 旧 Covey 原样；新 Covey 去掉 workspace、run_mode，仓库带上（全取消发 []）
    const payload: SwitchPayload = switchPayload(all, cur, !sameRepos(base.repos, undefined), lead) as unknown as SwitchPayload;
    setPhase('sending');
    setMessage('');
    void (async (): Promise<void> => {
      const fail = (m: string): void => {
        if (!alive.current) return;
        setPhase('failed');
        setMessage(m);
      };
      const r: OpResult = await sendOp({ op_id: newOpId(), kind: 'switch', topic_key: topic.key, payload: { ...payload } }, qc, undefined, () => !alive.current);
      if (r.phase !== 'done') {
        fail(opWords(r.phase, r.message));
        return;
      }
      // 回执后跟踪器已经取了这个话题的轮次和快照（opTrack.pullPlan）
      toast.success(opWords('done', r.message || '已换配置'));
      if (alive.current) {
        setPhase('idle');
        setSel(null);
        setOpen(false);
      }
    })();
  };
  const foot = (
    <div className="cfg-foot">
      <div className="cfg-note">
        {phase === 'failed' ? (
          <span className="bad" role="alert">{message}</span>
        ) : phase === 'sending' ? (
          <span>正在换…</span>
        ) : blocked ? (
          <span className={busy ? 'warn' : 'bad'}>{blocked}</span>
        ) : changed.length ? (
          <span>改了{changed.map((k: string) => FIELD_WORD[k] ?? k).join('、')}：{lead ? leadNext() : `从下一句话起按新配置做（${isRepoMode(all) ? '模型变了会开新会话' : '模型、工作区变了会开新会话'}，带上最近几轮）`}</span>
        ) : (
          <span>{lead ? '这是负责人现在的配置，改哪项点哪项' : forModel ? '只改模型这几项就行，执行设备和仓库不展开就不会动' : '这是话题现在的配置，改哪项点哪项'}</span>
        )}
      </div>
      <button type="button" className="btn sm" onClick={() => setOpen(false)} disabled={phase === 'sending'}>取消</button>
      <button
        type="button"
        className="btn sm pri"
        onClick={confirm}
        disabled={!!blocked || !changed.length || phase === 'sending'}
        data-testid="switch-confirm"
      >
        确认更换
      </button>
    </div>
  );
  return (
    <span className="cfg" ref={wrap}>
      <button type="button" className={`cfg-btn${open ? ' on' : ''}`} onClick={toggle} aria-expanded={open} title={lead ? '换负责人的配置：Harness、模型、思考预算' : `换配置：Harness、模型、思考预算${isRepoMode(all) ? '、仓库' : '、工作区'}…`} data-testid="config-bar">
        <span className="cfg-tx">{label}</span>
        {open ? <ChevronUp /> : <ChevronDown />}
      </button>
      {open ? (
        <div
          className={`cfg-pop${place ? ' fixed' : ''}`}
          role="dialog"
          aria-label={forModel ? '换个模型' : lead ? '换负责人的配置' : '换配置'}
          style={place ? ({ left: place.left, width: place.width, bottom: place.bottom, '--room': `${place.room}px` } as React.CSSProperties) : undefined}
        >
          <div className="cfg-head">
            <b>{forModel ? (lead ? '给负责人换个模型' : '换个模型') : lead ? '换负责人的配置' : '换配置'}</b>
            <span className="t3">{forModel ? '其他配置保持不变' : lead ? '同飞书专项卡上的「换配置」' : '同飞书话题里的「换配置」'}</span>
          </div>
          <LaunchPanel box={box} className="flat" foot={foot} />
        </div>
      ) : null}
    </span>
  );
};

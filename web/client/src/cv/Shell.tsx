/// <reference types="vite/client" />
import React, { useEffect, useState } from 'react';
import { Link, Outlet, useLocation, useNavigate, useMatch } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Brain, ChevronDown, ChevronRight, FolderOpen, Gauge, LayoutDashboard, ListTree, Menu, MessagesSquare, PanelLeft, Plus, Search, Settings, Share2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import type { SnapshotProject, SnapshotResponse, SnapshotTopic } from '@shared/api.interface';
import { useIsDesktop } from '@client/src/hooks/use-breakpoint';
import { prefetchTurns, useSettings, useSnapshot, useTidy, useWarmup, useMemoryMode, useQuota, useUpgrade } from '@client/src/hooks/use-workbench';
import { ageMs, errorText } from '@client/src/lib/workbench-format';
import { byNumberDesc, byRecent, isRunning, isWaiting, pref, setPref, steadyAt, toneOf, toneWord, topicNumberOf, type ToneKey } from './lib';
import { Avatar, Dot, useTick } from './ui';
import { RoleAvatar, TopicBadge } from './RoleAvatar';
import { setCollapsed, setMenu, useUi } from './store';
import { ThemeToggle } from './ThemeToggle';
import { listTone } from './topicText';
import brandMark from './brand/covey.png';

/** 大厅的图标：和看板、记忆一样是线条图标（侧栏和手机标签栏同一个）；管家头像只给管家自己，Covey 的鹌鹑只在品牌位 */
const HallIcon = MessagesSquare;
import { askCounts, asks, askSum, haltWords, isHalted, stoppedSet, type Asks } from './projectData';
import { PaneHandle } from './PaneHandle';
import { StatusGroups } from './StatusGroups';
import { TidyButton } from './Tidy';
import { useHeld } from './sideHold';
import { SIDE_PANE, showSavedPanes } from './panes';
import { upgradeMark } from './upgrade';
import { capsuleOf } from './quota';
import { canDo } from './v34';
import './covey.css';
import './layout-a.css';
import './night.css';
import './reading.css';
import './v2.css';
import './tags.css';
import { warmupDelay } from './warmup';
import { todoPending } from './memoryCenter';
import './sidebar.css';
import './unify.css';
import './v3.css';
import './upgrade.css';
import './quota.css';


/** 侧栏话题行右下角画状态点的状态：要你处理、告诉你一声和在做的；做完、空闲、排队、已停止不画（灰点不带信息） */
const LOUD: ReadonlySet<ToneKey> = new Set<ToneKey>(['waiting', 'stopped', 'stuck', 'result', 'running']);

/** 话题的头像（T645，和飞书私聊区、大厅一样）：自己颜色上的 T 号；状态是右下角一个点，同专项头像上负责人的状态点 */
const TopicLogo: React.FC<{ n: number | null; tone: ToneKey }> = ({ n, tone }) => (
  <span className="a-tlogo">
    <TopicBadge number={n} />
    {LOUD.has(tone) ? <Dot tone={tone} /> : null}
  </span>
);

/** 和话题头像一样宽的空位：「其余 N 个话题」「还有 N 个」的字和上面的标题对齐 */
const LogoSpace: React.FC = () => <span className="a-tlogo a-tspace" aria-hidden="true"><span className="a-tbadge" /></span>;

/** 侧栏里一个话题：一行一条。最近话题能有几百行、快照两秒一刷：没变的话题（快照里同一个对象）不重画 */
const TopicRow = React.memo(function TopicRow({ t, active, stopped, mini }: { t: SnapshotTopic; active: boolean; stopped?: boolean; mini?: boolean }) {
  const qc = useQueryClient();
  const tn: ToneKey = stopped ? 'stopped' : listTone(t);
  const name: string = `${t.number ? `T${t.number} ` : ''}${t.title}`;
  if (mini) {
    return (
      <Link to={`/topics/${encodeURIComponent(t.key)}`} className={`row ${active ? 'on' : ''}`} title={name} aria-label={name}>
        <TopicLogo n={t.number} tone={tn} />
      </Link>
    );
  }
  return (
    <Link
      to={`/topics/${encodeURIComponent(t.key)}`}
      className={`a-tp${active ? ' on' : ''}${t.unseen ? ' unseen' : ''}${tn === 'cancelled' || tn === 'idle' ? ' dim' : ''}`}
      title={name}
      aria-label={t.unseen ? `${name}（做完还没看）` : name}
      onMouseEnter={() => prefetchTurns(qc, t.key)}
      onTouchStart={() => prefetchTurns(qc, t.key)}
    >
      {/* 夜航 v2：一行一条不折行；T645：行首是话题头像（彩色 T 号，状态点在它右下角），标题左对齐成一条线，行尾不再写 T 号 */}
      <TopicLogo n={t.number} tone={tn} />
      <span className="a-tt">{t.title}</span>
      {t.unseen ? <i className="a-blue" aria-label="做完还没看" /> : null}
    </Link>
  );
});

interface SideProject {
  p: SnapshotProject;
  topics: SnapshotTopic[];
  waiting: number;
  stopped: number;
  running: number;
  last: number;
  /** 排先后用的最近活动（steadyAt：在跑的话题不算），last 里在跑的话题每次同步都变 */
  steady: number;
}

/** 组里先列要紧的（等你、停住、在做），再补最近的，凑到 SHOW_IN_GROUP 个；其余收成“其余 N 个话题” */
const SHOW_IN_GROUP = 3;

const ProjectGroup: React.FC<{ x: SideProject; snap: SnapshotResponse; stoppedKeys: ReadonlySet<string> }> = ({ x, snap, stoppedKeys }) => {
  const loc = useLocation();
  const { p } = x;
  const base: string = `/projects/${encodeURIComponent(p.key)}`;
  const activeTopic: SnapshotTopic | undefined = x.topics.find((t: SnapshotTopic) => loc.pathname === `/topics/${encodeURIComponent(t.key)}`);
  const onProject: boolean = loc.pathname === base || loc.pathname.startsWith(`${base}/`);
  // 有等你、停住或在做的话题（或专项自己被护栏停住）时默认展开；手动收起过的照手动的
  const attention: boolean = x.waiting > 0 || x.stopped > 0 || x.running > 0 || isHalted(p);
  const saved: string = pref(`grp.${p.key}`, '');
  const [open, setOpen] = useState<boolean>(saved ? saved === '1' : attention);
  const [all, setAll] = useState<boolean>(false);
  // 走进这个专项（点了专项名，或打开了它的一个话题）就展开，看得到它的话题；不记成偏好，箭头照样能收起
  const here: string = onProject ? base : activeTopic ? activeTopic.key : '';
  const [was, setWas] = useState<string>('');
  if (here !== was) {
    setWas(here);
    if (here && !open) setOpen(true);
  }
  const show: boolean = open;
  const hot = (t: SnapshotTopic): boolean => isWaiting(t) || isRunning(t) || stoppedKeys.has(t.key);
  // 等你、在做、停住的在前，按编号（T779：在跑的 updated_at 每次同步都变，照它排几个在跑的会来回换位）；其余按最近
  const ordered: SnapshotTopic[] = [...x.topics.filter(hot).sort(byNumberDesc), ...x.topics.filter((t: SnapshotTopic) => !hot(t))];
  const n: number = Math.max(SHOW_IN_GROUP, x.topics.filter(hot).length);
  let list: SnapshotTopic[] = all ? ordered : ordered.slice(0, n);
  if (activeTopic && !list.includes(activeTopic)) list = [...list, activeTopic];
  const lead: SnapshotTopic | undefined = snap.topics.find((t: SnapshotTopic) => t.key === p.lead_topic_key);
  // 护栏停住的专项自己也算一件停住了（和看板、飞书一个数），名字后面的数是红的
  const halted: boolean = isHalted(p);
  const cnt: React.ReactNode = x.waiting || x.stopped || halted
    ? <em className={`a-cnt wait${halted ? ' halt' : ''}`} title={`${x.waiting} 等你 · ${x.stopped + (halted ? 1 : 0)} 停住了${halted ? `（专项${haltWords(p)}）` : ''}`}>{x.waiting + x.stopped + (halted ? 1 : 0)}</em>
    : x.running ? <em className="a-cnt run" title={`${x.running} 在做`}>{x.running}</em> : null;
  const toggle = (): void => {
    setPref(`grp.${p.key}`, show ? '0' : '1');
    setOpen(!show);
  };
  // 头像和专项名共用一个入口；状态点仍表示负责人在做或等你。
  const leadBusy: boolean = !!lead && (isRunning(lead) || isWaiting(lead));
  return (
    <div className={`a-grp${show ? ' open' : ''}`}>
      <div className={`a-grph${onProject ? ' on' : ''}`}>
        <button type="button" className="a-chev" aria-label={show ? `收起 ${p.title}` : `展开 ${p.title}`} aria-expanded={show} onClick={toggle}>
          <ChevronRight />
        </button>
        <Link to={base} className={`a-gname${onProject ? ' on' : ''}`} title={p.goal ? `${p.title}\n${p.goal}` : p.title}>
          <span className="a-project-avatar" title={leadBusy && lead ? `负责人 · ${toneWord(toneOf(lead))}` : '负责人'}>
            <RoleAvatar name={p.avatar} />
            {leadBusy && lead ? <i className={`tone-${toneOf(lead)}`} /> : null}
          </span>
          <span className="a-gtitle">{p.title}</span>
        </Link>
        {cnt}
      </div>
      {show ? (
        <div className="a-grpb">
          {list.map((t: SnapshotTopic) => (
            <TopicRow key={t.key} t={t} active={t === activeTopic} stopped={stoppedKeys.has(t.key)} />
          ))}
          {ordered.length > list.length || all ? (
            <button type="button" className="a-more" onClick={() => setAll(!all)}>
              <LogoSpace />
              {all ? '收起' : `其余 ${ordered.length - list.length} 个话题`}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

/** 安静的专项：没有等你、停住、在做的话题，且三天没动静（或已经做完、收起） */
const QUIET_DAYS = 3;
/** 最近话题先画 LOOSE_PAGE 个，滚到离底部不到 LOOSE_AHEAD 像素时再画 LOOSE_PAGE 个（数据都在快照里，只是少画些行） */
const LOOSE_PAGE = 50;
const LOOSE_AHEAD = 320;

/** 最近话题的底部：滚到附近自动接着往下画；也是个按钮（键盘、或浏览器没有 IntersectionObserver 时点它） */
const LoadMore: React.FC<{ root: React.RefObject<HTMLDivElement>; at: number; left: number; onMore: () => void }> = ({ root, at, left, onMore }) => {
  const ref = React.useRef<HTMLButtonElement>(null);
  const more = React.useRef<() => void>(onMore);
  more.current = onMore;
  useEffect(() => {
    const el: HTMLButtonElement | null = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return undefined;
    // 每画完一页换一个观察者：刚开始观察时会先回调一次，画完它还在视野里（屏幕高、行少）就接着画，直到填满
    const io = new IntersectionObserver(
      (es: IntersectionObserverEntry[]) => {
        if (es.some((e: IntersectionObserverEntry) => e.isIntersecting)) more.current();
      },
      { root: root.current, rootMargin: `0px 0px ${LOOSE_AHEAD}px 0px` },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [root, at]);
  return (
    <button ref={ref} type="button" className="a-more a-loadmore" onClick={onMore}>
      <LogoSpace />
      还有 {left} 个
    </button>
  );
};

const p2done = (status: string, quietLong: boolean): boolean => ['做完', '收起', '已取消'].includes(status) || quietLong;

const SideSearch: React.FC<{ mini: boolean }> = ({ mini }) => {
  const nav = useNavigate();
  const loc = useLocation();
  const ref = React.useRef<HTMLInputElement>(null);
  const [value, setValue] = useState<string>('');
  const { data } = useSnapshot();
  // 输入的是 T 号、快照里有这个话题：回车直接打开它
  const direct = (): SnapshotTopic | undefined => {
    const n: number | null = topicNumberOf(value);
    return n === null ? undefined : data?.topics.find((t: SnapshotTopic) => t.kind === 'topic' && t.number === n);
  };
  useEffect(() => {
    const on = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener('keydown', on);
    return () => window.removeEventListener('keydown', on);
  }, []);
  useEffect(() => {
    const q: string = value.trim();
    const id: number = window.setTimeout(() => {
      if (q) nav(`/search?q=${encodeURIComponent(q)}`, { replace: loc.pathname === '/search' });
    }, 300);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  if (mini) {
    return (
      <div className="search" style={{ justifyContent: 'center', padding: 0 }}>
        <Search />
      </div>
    );
  }
  return (
    <label className="search">
      <Search />
      <input
        ref={ref}
        value={value}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) => setValue(e.target.value)}
        onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => {
          if (e.key !== 'Enter' || e.nativeEvent.isComposing) return;
          const t: SnapshotTopic | undefined = direct();
          if (t) {
            e.preventDefault();
            setValue('');
            nav(`/topics/${encodeURIComponent(t.key)}`);
          } else if (value.trim()) nav(`/search?q=${encodeURIComponent(value.trim())}`, { replace: loc.pathname === '/search' });
        }}
        placeholder="搜索话题、对话或 T 号"
        aria-label="搜索"
        style={{ flex: 1, minWidth: 0, background: 'transparent', border: 0, outline: 0, color: 'var(--t1)', font: 'inherit' }}
      />
      <span className="kbd">⌘K</span>
    </label>
  );
};

export const SyncDot: React.FC<{ at: string | null | undefined }> = ({ at }) => {
  useTick(15000);
  const age: number | null = ageMs(at);
  const fresh: boolean = age !== null && age <= 180000;
  return <i className="sync" title={at ? `最后同步 ${at}` : '还没同步'} style={fresh ? undefined : { background: 'var(--stop)', animation: 'none' }} />;
};

/** v39：「设置」旁的“有新版本”“更新中”：Covey 认 upgrade 操作才去取版本条目；没有就是空串，什么都不画 */
function useUpgradeMark(): string {
  const ops: readonly string[] | undefined = useSettings().data?.data?.ops;
  const on: boolean = canDo(ops, 'upgrade');
  const up = useUpgrade(on);
  return on ? upgradeMark(up.data?.data) : '';
}

/** v42：搜索框下的一行：某个来源额度见底或有号要重新登录；Covey 认 quota_refresh 操作才去取条目；没有就是空串，什么都不画 */
function useQuotaCapsule(): string {
  const ops: readonly string[] | undefined = useSettings().data?.data?.ops;
  const on: boolean = canDo(ops, 'quota_refresh');
  const q = useQuota(on);
  return on ? capsuleOf(q.data?.data) : '';
}

const Sidebar: React.FC<{ mini: boolean; drawer?: boolean }> = ({ mini, drawer }) => {
  // 记忆：v43 只轮询小键 memory:index（角标是待审核数），去记忆中心；旧 Covey 只推 v37 的 memory，照旧去设置页「记忆」
  const mem = useMemoryMode();
  const memoryOn: boolean = mem.mode === 'v43' || mem.mode === 'v37';
  const memoryTo: string = mem.mode === 'v43' ? '/memory' : '/settings/memory';
  const pending: number = mem.mode === 'v43' ? todoPending(mem.index?.counts) : 0;
  // 鼠标在侧栏里时先画它进来那刻的快照，移出或停着不动几秒再换新的：每秒的同步不让正要点的那行从鼠标底下挪走（sideHold.ts）
  const hold = useHeld(useSnapshot().data);
  const data = hold.view;
  const ui = useUi();
  const loc = useLocation();
  const now: number = useTick(60000);
  const [shown, setShown] = useState<number>(LOOSE_PAGE);
  const scroller = React.useRef<HTMLDivElement>(null);
  const launchOn: boolean = !!useSettings().data?.data?.launch;
  const upMark: string = useUpgradeMark();
  const capsule: string = useQuotaCapsule();
  const { data: tidy } = useTidy();  // 整理专项要你定的建议：算进要你处理（同看板）
  const tidyOn: boolean = !!tidy;
  const [unOpen, setUnOpen] = useState<boolean>(false);
  const [oldOpen, setOldOpen] = useState<boolean>(() => pref('grp.old', '0') === '1');
  const [oldWas, setOldWas] = useState<string>('');
  useEffect(() => {
    if (drawer && ui.focus) document.getElementById(`side-${ui.focus}`)?.scrollIntoView({ block: 'start' });
  }, [drawer, ui.focus]);
  if (!data) return null;
  const topics: SnapshotTopic[] = data.topics.filter((t: SnapshotTopic) => t.kind === 'topic');
  // 话题头像一样宽（按最长的号码，至少 T 加三位）：一列标题对成一条线，T7 和 T645 不会让标题错开
  const logoChars: number = topics.reduce((most: number, t: SnapshotTopic) => Math.max(most, t.number ? String(t.number).length + 1 : 0), 4);
  const qKeys = new Set<string>(data.questions.map((q) => q.topic_key));
  // 要你处理：和看板、手机标签栏同一个数（projectData.ts asks）：管家、负责人的题，整理专项的建议，可能卡住，护栏停住的专项都算；
  // 数的就是「要你处理」列出的那几行（askCounts），不拿 Covey 另推的 asks 条目盖（T978）
  const a: Asks = asks(data, now, tidy);
  const act: number = askSum(askCounts(a));
  const stoppedKeys: Set<string> = stoppedSet(a);
  // “N 在做”同看板结论带：可能卡住的算在要你处理里，不再数一遍
  const running: number = topics.filter((t: SnapshotTopic) => toneOf(t) === 'running').length;
  const ids = new Set<string>(data.projects.map((p: SnapshotProject) => p.key));
  const groups: SideProject[] = data.projects.map((p: SnapshotProject): SideProject => {
    const own: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => t.project_key === p.key).sort(byRecent);
    return {
      p,
      topics: own,
      waiting: own.filter((t: SnapshotTopic) => isWaiting(t) || qKeys.has(t.key)).length,
      stopped: own.filter((t: SnapshotTopic) => stoppedKeys.has(t.key)).length,
      running: own.filter(isRunning).length,
      last: Math.max(p.updated_at, ...own.map((t: SnapshotTopic) => t.updated_at)),
      steady: Math.max(p.started_at ?? 0, ...own.map(steadyAt)),
    };
  });
  const quiet = (x: SideProject): boolean =>
    !x.waiting && !x.stopped && !x.running && !isHalted(x.p) && p2done(x.p.status, now - x.last > QUIET_DAYS * 86400);
  const rank = (x: SideProject): number => (x.waiting || x.stopped || isHalted(x.p) ? 0 : x.running ? 1 : 2);
  const live: SideProject[] = groups.filter((x: SideProject) => !quiet(x)).sort((a: SideProject, b: SideProject) => rank(a) - rank(b) || b.steady - a.steady || a.p.key.localeCompare(b.p.key));
  const old: SideProject[] = groups.filter(quiet).sort((a: SideProject, b: SideProject) => b.last - a.last);
  const hot = (t: SnapshotTopic): boolean => isWaiting(t) || isRunning(t) || qKeys.has(t.key) || stoppedKeys.has(t.key);
  const loose: SnapshotTopic[] = topics.filter((t: SnapshotTopic) => !t.project_key || !ids.has(t.project_key)).sort(byRecent);
  const looseHot: SnapshotTopic[] = loose.filter(hot).sort(byNumberDesc);
  const numbered: SnapshotTopic[] = [...looseHot, ...loose.filter((t: SnapshotTopic) => !hot(t) && !!t.number)];
  const unnumbered: SnapshotTopic[] = loose.filter((t: SnapshotTopic) => !hot(t) && !t.number);
  const shownN: number = Math.max(shown, looseHot.length);
  const isOn = (t: SnapshotTopic): boolean => loc.pathname === `/topics/${encodeURIComponent(t.key)}`;
  // 走进一个更早的专项（它的页或它的话题）时把“更早的专项”打开，侧栏里看得到自己在哪；不记成偏好
  const inProject = (x: SideProject): boolean => {
    const base: string = `/projects/${encodeURIComponent(x.p.key)}`;
    return loc.pathname === base || loc.pathname.startsWith(`${base}/`) || x.topics.some(isOn);
  };
  const oldHere: string = old.find(inProject)?.p.key ?? '';
  if (oldHere !== oldWas) {
    setOldWas(oldHere);
    if (oldHere && !oldOpen) setOldOpen(true);
  }
  const toggleOld = (): void => {
    setPref('grp.old', oldOpen ? '0' : '1');
    setOldOpen(!oldOpen);
  };
  // 手机抽屉：点了链接才收起（换页本来也会收）；展开收起专项、点“还有 N 个”、点搜索框都不收
  const go = (e: React.MouseEvent): void => {
    if (drawer && e.target instanceof Element && e.target.closest('a')) setMenu(false);
  };
  return (
    <aside className={`side a-side${mini ? ' mini' : ''}${drawer ? ' drawer-side' : ''}`} onClick={go} {...hold.bind} style={{ '--tw': logoChars } as React.CSSProperties}>
      <div className="brand">
        <img className="logo" src={brandMark} alt="Covey" width={24} height={24} />
        {mini ? null : (
          <>
            <span className="name">Covey</span>
            <SyncDot at={data.synced_at} />
            <span className="sp" />
          </>
        )}
        {drawer ? null : (
          <button type="button" aria-label={mini ? '展开侧栏' : '收起侧栏'} onClick={() => setCollapsed(!ui.collapsed)} style={{ background: 'none', border: 0, cursor: 'pointer', color: 'var(--t3)' }}>
            <PanelLeft />
          </button>
        )}
      </div>
      <SideSearch mini={mini} />
      {capsule && !mini ? (
        <Link to="/settings/quota" className="qcap" title="账号与额度">
          <i />
          {/* “· 11月27日 02:24 恢复”整块换行，不把“恢复”一个词甩到下一行 */}
          <span>{capsule.split(' · ').map((part: string, i: number) => (i ? <React.Fragment key={i}> · <span className="qcap-w">{part}</span></React.Fragment> : part))}</span>
          <ChevronRight aria-hidden="true" />
        </Link>
      ) : null}
      {/* 收起成窄条时：同样的条件，一个带红点的小图标（悬停是胶囊那句话），点了到设置 → 账号与额度 */}
      {capsule && mini ? (
        <Link to="/settings/quota" className="qcap-mini" title={capsule} aria-label={capsule}>
          <Gauge aria-hidden="true" />
          <i aria-hidden="true" />
        </Link>
      ) : null}
      <div ref={scroller} style={{ overflowY: 'auto', flex: 1, minHeight: 0 }} onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <div onClick={go}>
          {launchOn ? (
            <Link to="/new" className={`row a-nav ${loc.pathname === '/new' ? 'on' : ''}`} title="新话题">
              <Plus className="ic" />
              {mini ? null : <span className="t">新话题</span>}
            </Link>
          ) : null}
          <Link to="/butler" className={`row a-nav ${loc.pathname === '/butler' ? 'on' : ''}`} title="大厅：你、管家和各专项的负责人">
            <HallIcon className="ic" />
            {mini ? null : <span className="t">大厅</span>}
          </Link>
          <Link to="/" className={`row a-nav ${loc.pathname === '/' ? 'on' : ''}`} title="看板">
            <LayoutDashboard className="ic" />
            {mini ? null : (
              <>
                <span className="t">看板</span>
                <span className="r">
                  {act ? <em className="a-badge wait" title="要你处理（等你、停住了、可能卡住）">{act} 件</em> : null}
                  {running ? <em className="a-badge run">{running} 在做</em> : null}
                </span>
              </>
            )}
          </Link>
          {memoryOn ? (
            <Link
              to={memoryTo}
              className={`row a-nav ${loc.pathname === memoryTo || loc.pathname.startsWith('/memory/') ? 'on' : ''}`}
              title={mem.mode === 'v43' ? `记忆：规矩、资料${pending ? ` · 待审核 ${pending} 条` : ''}` : '记忆：全局和各工作区的原则、知识条目'}
              data-testid="nav-memory"
            >
              <Brain className="ic" />
              {mini ? null : (
                <>
                  <span className="t">记忆</span>
                  {pending ? <span className="r"><em className="a-badge wait" title="待审核：要你定记不记">{pending}</em></span> : null}
                </>
              )}
            </Link>
          ) : null}
          {mini ? (
            topics.filter(hot).sort(byNumberDesc).slice(0, 12).map((t: SnapshotTopic) => <TopicRow key={t.key} t={t} active={isOn(t)} stopped={stoppedKeys.has(t.key)} mini />)
          ) : (
            <>
              <StatusGroups a={a} data={data} running={topics.filter((t: SnapshotTopic) => t.kind === 'topic' && toneOf(t) === 'running')} now={now} />
              {groups.length || tidyOn ? (
                <div className="sec" id="side-projects">
                  {/* 点“专项”打开专项一览（/projects，一个专项一张简报卡）：电脑上没有底部标签栏，从这里进 */}
                  <Link to="/projects" className="a-secl" title="专项一览：一个专项一张简报卡">专项</Link>
                  {/* 「整理」（开了整理专项才有）：原来在看板右栏的专项块上，那一块去掉后挪到这里，每一页都点得到（T973） */}
                  {tidyOn ? <TidyButton compact /> : null}
                  {groups.length ? <span>{groups.length}</span> : null}
                </div>
              ) : null}
              {live.map((x: SideProject) => (
                <ProjectGroup key={x.p.key} x={x} snap={data} stoppedKeys={stoppedKeys} />
              ))}
              {old.length ? (
                <section className={`a-archive${oldOpen ? ' open' : ''}`} aria-label="更早的专项">
                  <button type="button" className="a-archive-toggle" aria-expanded={oldOpen} aria-controls={`${drawer ? 'drawer' : 'side'}-earlier-projects`} onClick={toggleOld}>
                    <span>更早的专项</span>
                    <span className="a-archive-count">{old.length}</span>
                    <ChevronRight aria-hidden="true" />
                  </button>
                  {/* 分组只负责收放；里面仍是同一级专项，不套用子话题的缩进和竖线。 */}
                  {oldOpen ? (
                    <div className="a-archive-list" id={`${drawer ? 'drawer' : 'side'}-earlier-projects`}>
                      {old.map((x: SideProject) => (
                        <ProjectGroup key={x.p.key} x={x} snap={data} stoppedKeys={stoppedKeys} />
                      ))}
                    </div>
                  ) : null}
                </section>
              ) : null}
              <div className="sec a-sech" id="side-topics" title={groups.length ? '不属于专项的话题' : undefined}>
                <span>最近话题 <b className="a-secn">{numbered.length}</b></span>
              </div>
              {numbered.slice(0, shownN).map((t: SnapshotTopic) => (
                <TopicRow key={t.key} t={t} active={isOn(t)} stopped={stoppedKeys.has(t.key)} />
              ))}
              {numbered.length > shownN ? (
                <LoadMore root={scroller} at={shownN} left={numbered.length - shownN} onMore={() => setShown(shownN + LOOSE_PAGE)} />
              ) : null}
              {unnumbered.length ? (
                <>
                  <div className={`a-grp a-old${unOpen ? ' open' : ''}`}>
                    <div className="a-grph">
                      <button type="button" className="a-chev" aria-expanded={unOpen} aria-label={unOpen ? '收起未编号的话题' : '展开未编号的话题'} onClick={() => setUnOpen(!unOpen)}>
                        <ChevronRight />
                      </button>
                      <button type="button" className="a-gname" onClick={() => setUnOpen(!unOpen)}>未编号</button>
                      <em className="a-cnt">{unnumbered.length}</em>
                    </div>
                    {unOpen ? (
                      <div className="a-grpb">
                        {unnumbered.map((t: SnapshotTopic) => <TopicRow key={t.key} t={t} active={isOn(t)} />)}
                      </div>
                    ) : null}
                  </div>
                </>
              ) : null}
            </>
          )}
        </div>
      </div>
      <div className="sidefoot">
        <Link to={mini && upMark ? '/settings/upgrade' : '/settings'} className={`sidelink${loc.pathname.startsWith('/settings') ? ' on' : ''}`} title={mini && upMark ? `设置 · ${upMark}` : '设置'} aria-label="设置">
          <Settings />
          {mini ? null : <span>设置</span>}
          {mini && upMark ? <i className="up-dot" aria-label={upMark} /> : null}
        </Link>
        {!mini && upMark ? (
          <Link to="/settings/upgrade" className="short up-new" title="版本与更新：机器人和网页一起换成最新版本">{upMark}</Link>
        ) : null}
        {/* 深色、浅色切换（T842）：常驻在设置旁，哪一页都能点；收起的侧栏里叠在设置上面 */}
        <ThemeToggle />
        {mini ? null : <Avatar />}
      </div>
    </aside>
  );
};

/** 妙搭平台在右下角悬浮的“妙搭生成”（@lark-apaas/client-toolkit 的 Watermark，挂在 #root 下、不在 .cv 里）。
 * 应用里关不掉（主人可以在它的菜单里点“不再显示”）：量出它占了多宽，手机标签栏右边让出来；它没了就不让 */
function badgeGap(): number {
  const w: number = window.innerWidth;
  const h: number = window.innerHeight;
  const els: Element[] = [...document.querySelectorAll('[data-custom-element$="-watermark-mobile"]')];
  for (const [x, y] of [[w - 24, h - 30], [w - 40, h - 40]]) {
    for (const el of document.elementsFromPoint(x, y)) {
      if (el !== document.body && el !== document.documentElement && el.id !== 'root' && !el.closest('.cv')) els.push(el);
    }
  }
  let gap = 0;
  for (const el of els) {
    const r: DOMRect = el.getBoundingClientRect();
    if (r.width > 0 && r.width < w * 0.6 && r.bottom > h - 70 && r.top < h) gap = Math.max(gap, Math.ceil(w - r.left + 6));
  }
  return gap;
}

function usePlatformBadgeGap(): number {
  const [gap, setGap] = useState<number>(0);
  useEffect(() => {
    const measure = (): void => setGap(badgeGap());
    measure();
    // 它是页面载入、问过平台以后才挂上的，主人点“不再显示”又会拿掉：盯着 #root 的直接子节点
    const root: HTMLElement | null = document.getElementById('root');
    const mo = new MutationObserver(() => window.requestAnimationFrame(measure));
    if (root) mo.observe(root, { childList: true });
    window.addEventListener('resize', measure);
    return () => {
      mo.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, []);
  return gap;
}

/** 手机（<768px）底部标签栏：看板、大厅、专项、话题、设置。专项是专项一览页（/projects）；话题打开侧栏抽屉并滚到最近话题 */
const TabBar: React.FC = () => {
  const gap: number = usePlatformBadgeGap();
  const loc = useLocation();
  const ui = useUi();
  const { data } = useSnapshot();
  const now: number = useTick(60000);
  const { data: tidy } = useTidy();
  // 和看板、侧栏同一个数（projectData.ts asks）
  const act: number = data ? askSum(askCounts(asks(data, now, tidy))) : 0;
  const on = (focus: 'topics'): boolean => ui.menu && ui.focus === focus;
  const upMark: string = useUpgradeMark();
  return (
    <nav className="nb-tabs" aria-label="主导航" style={gap ? { paddingRight: gap } : undefined}>
      <Link to="/" className={loc.pathname === '/' && !ui.menu ? 'on' : ''}>
        <LayoutDashboard />
        <span>看板</span>
        {act ? <em>{act}</em> : null}
      </Link>
      <Link to="/butler" className={loc.pathname === '/butler' && !ui.menu ? 'on' : ''}>
        <HallIcon />
        <span>大厅</span>
      </Link>
      <Link to="/projects" className={(loc.pathname === '/projects' || loc.pathname.startsWith('/projects/')) && !ui.menu ? 'on' : ''}>
        <FolderOpen />
        <span>专项</span>
      </Link>
      <button type="button" className={on('topics') || (loc.pathname.startsWith('/topics/') && !ui.menu) ? 'on' : ''} onClick={() => setMenu(true, 'topics')}>
        <ListTree />
        <span>话题</span>
      </button>
      <Link to={upMark ? '/settings/upgrade' : '/settings'} className={loc.pathname.startsWith('/settings') && !ui.menu ? 'on' : ''}>
        <Settings />
        <span>设置</span>
        {upMark ? <i className="up-dot" aria-label={upMark} /> : null}
      </Link>
    </nav>
  );
};

/** 页面头：电脑一行标题；手机是顶栏（汉堡或返回）。expand：手机上点标题区把副标题（专项的目标）展开成几行，再点收起 */
export const Head: React.FC<{
  title: React.ReactNode;
  sub?: React.ReactNode;
  badge?: React.ReactNode;
  right?: React.ReactNode;
  back?: string;
  tags?: React.ReactNode;
  expand?: boolean;
}> = ({ title, sub, badge, right, back, tags, expand }) => {
  const desktop: boolean = useIsDesktop();
  const nav = useNavigate();
  const [full, setFull] = useState<boolean>(false);
  if (desktop) {
    return (
      <div className="hd">
        <div className="ttl">
          <div className="l1">
            {tags}
            <h1>{title}</h1>
            {badge}
          </div>
          {sub ? <div className="goal">{sub}</div> : null}
        </div>
        <span className="sp" />
        {right}
      </div>
    );
  }
  const canExpand: boolean = !!expand && !!sub;
  return (
    <div className={`mtop${canExpand && full ? ' open' : ''}`}>
      {back ? (
        <a className="ib" role="button" aria-label="返回" onClick={() => nav(back)}>
          <ArrowLeft />
        </a>
      ) : (
        <a className="ib" role="button" aria-label="菜单" onClick={() => setMenu(true)}>
          <Menu />
        </a>
      )}
      <div
        className={`tt${canExpand ? ' tap' : ''}`}
        role={canExpand ? 'button' : undefined}
        aria-expanded={canExpand ? full : undefined}
        onClick={canExpand ? () => setFull(!full) : undefined}
      >
        <h1>{title}</h1>
        {sub ? (
          <span className="ms">
            <span className="mst">{sub}</span>
            {canExpand ? <ChevronDown className="msc" /> : null}
          </span>
        ) : null}
      </div>
      {badge}
      {right}
      {/* 手机没有侧栏：深色、浅色切换（T842）常驻在顶栏最右 */}
      <ThemeToggle />
    </div>
  );
};

// 两侧栏拖过的宽度（T741）：模块载入时就放上，首屏按记下的宽度画
try {
  showSavedPanes();
} catch {
  /* 没有 DOM */
}

const Shell: React.FC = () => {
  const desktop: boolean = useIsDesktop();
  const ui = useUi();
  const { isLoading, isError, error, refetch, data } = useSnapshot();
  const loc = useLocation();
  // 只看打开网页时的首屏：之后在页面间切换不再重排一轮预热（T870 Wave 3.5 复核）
  const [warmDelay] = useState<number>(() => warmupDelay(loc.pathname));
  useWarmup(warmDelay);
  useEffect(() => setMenu(false), [loc.pathname]);
  useMatch('/');
  const cls: string = `cv${ui.light ? ' light' : ''}${desktop ? '' : ' m'}`;
  if (isLoading) {
    return (
      <div className={cls} data-testid="first-load">
        <div className="app">
          <div className="side" style={{ gap: 12, paddingTop: 16 }}>
            <Skeleton className="h-6 w-24" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-7 w-full" />
            <Skeleton className="h-7 w-3/4" />
          </div>
          <div className="main" />
        </div>
      </div>
    );
  }
  if (isError && !data) {
    return (
      <div className={cls}>
        <div style={{ display: 'grid', placeItems: 'center', height: '100dvh', gap: 12, textAlign: 'center' }}>
          <p style={{ color: 'var(--fail)' }}>{errorText(error)}</p>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            重试
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className={cls}>
      <div className="app">
        {desktop ? <Sidebar mini={ui.collapsed} /> : null}
        {desktop && !ui.collapsed ? <PaneHandle spec={SIDE_PANE} /> : null}
        <main className="main">
          <Outlet />
        </main>
        {!desktop && ui.menu ? (
          <>
            <div className="scrim" onClick={() => setMenu(false)} />
            <Sidebar mini={false} drawer />
          </>
        ) : null}
        {desktop ? null : <TabBar />}
      </div>
    </div>
  );
};

export default Shell;
export { Plus };

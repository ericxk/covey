import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ArrowDown, ChevronRight, Paperclip } from 'lucide-react';
import { toast } from 'sonner';
import type { CollabView, OutboxView, SayFile, SnapshotProject, SnapshotTopic, TurnView } from '@shared/api.interface';
import { useIsDesktop } from '@client/src/hooks/use-breakpoint';
import { useQueryClient } from '@tanstack/react-query';
import { newOpId, useRoomTurns, useSnapshot, useTurns } from '@client/src/hooks/use-workbench';
import { sendQuiet, useSendOutbox, useTrackedOp } from './sendOp';
import {
  addPending, getDraft, getScroll, keepLocalImages, localImagesOf, markSeenSent, patchPending, removePending, setDraft, setScroll, usePending,
  type LocalImage, type Pending, type PendingFile,
} from '@client/src/lib/conv-store';
import { ageMs, errorText } from '@client/src/lib/workbench-format';
import type { LaunchBox } from './launch';
import { Composer, useSay, type ComposerHandle, type ComposerRoute, type KeyLike } from './Composer';
import { ConfigBar } from './ConfigBar';
import { Md } from './Md';
import { composerWords, QuestionPanel, useQuestionForm } from './Question';
import { ctxLimitOk, fmtDur, fmtWindow, harnessName, hhmm, isRunning, isWaiting, toneOf } from './lib';
import { FailBox, stopWords } from './Timeline';
import { bubbleWords, opLine, opWords } from './opWords';
import { AgentTurn, ChatRound, OwnerBubble } from './chatTurn';
import { progressNote, saidLabel, shownText, stopPressing } from './topicText';
import { Dot, useTick } from './ui';
import { JumpRail } from './JumpRail';
import { oneLine, type Mark } from './jumpMarks';
import { SentImages, withoutImageLine } from './TurnImages';
import { localImage } from './attachments';
import * as F from './scrollFollow';
import './images.css';
import { buildRoom, members as roomMembers, onlyLead, quoteText, roomQuestion, speaker, type Member, type RoomTurn } from './room';
import { AskBand, ProjectEvent, QuestionRow, RelayTurn, RoleAvatar, RouteChip, RouteTop, RouteWho, SelectionQuote, toQuestion } from './RoomBits';
import { dropMention, participants, plainMentions, type MentionAttrs } from './mention';
import { clearMentionEnv, onMentionRequest, setMentionEnv } from './MentionNode';
import type { Trigger } from './RichText';
import { CollabBlock, CollabReceipt } from './CollabBits';
import {
  candidates, groupTurns, knownKey, mentionInfo, nameOf, plan as planSay, receiptOf, roomCollabs, sayPayload, searchCandidates, syncPicks, topicRows,
  type Candidate, type Group, type MentionInfo, type Plan, type PlanInput, type QuoteRef,
} from './collab';

/** 没有协作时用同一个空数组（免得每次重算对话的分组） */
const NO_COLLABS: CollabView[] = [];

/** 输入框上沿的运行胶囊（T753，学 Granola、Persona）：话题在跑时写它此刻在干什么（progressNote：在等一条命令跑完 · 已 59 分钟），
 *  没有细节时写阶段或“在做”；可能卡住时换成可能卡住的点。自己每秒刷新，不带着整段对话重画 */
const LivePill: React.FC<{ topic: SnapshotTopic }> = ({ topic }) => {
  const now: number = useTick(1000);
  const note = progressNote(topic.progress, now);
  return (
    <div className={`livepill${note.stuck ? ' stuck' : ''}`} role="status">
      <Dot tone={note.stuck ? 'stuck' : 'running'} />
      <span>{note.text || topic.progress?.phase || '在做'}</span>
    </div>
  );
};

/** 输入框下沿的上下文用量（T753）：知道窗口多大时前面一条细进度条，过八成变橙（快满了，下一轮可能先压缩）；不知道时只写用了多少 */
const ContextMeter: React.FC<{ used: number; limit: number }> = ({ used, limit }) => {
  const pct: number = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  return (
    <span className="cp-ctx" title={limit > 0 ? `上下文已用 ${pct}%` : undefined}>
      {limit > 0 ? (
        <span className={`cp-ctxbar${pct >= 80 ? ' full' : ''}`} aria-hidden="true">
          <i style={{ width: `${pct}%` }} />
        </span>
      ) : null}
      {limit > 0 ? `上下文 ${Math.round(used / 1000)}K / ${fmtWindow(limit)}` : `上下文 ${Math.round(used / 1000)}K`}
    </span>
  );
};

/** 认回显时容的钟差：Covey 记下你那一行的时刻和本页发出的时刻不是一台机器的钟 */
const ECHO_SKEW_MS = 120_000;

/** 刚发出的一句话（T765 F3）：发送中时半透明；写进网页库（POST 成功）就画成正式的样子，旁边写“✓ 已发出”；
 * Covey 回执后写它的回执原话（“已开下一轮”“已作为补充交给在做的任务 #…”），没办成写原因，没办成的可以关掉（opWords.ts bubbleWords） */
const PendingBubble: React.FC<{ p: Pending; row: OutboxView | undefined; onClose: () => void }> = ({ p, row, onClose }) => {
  const tr = useTrackedOp(p.id);
  const w = bubbleWords(p, row, tr);
  const failed: boolean = w.tone === 'bad';
  const text: string = w.text;
  const imgs: LocalImage[] = localImagesOf(p.files);
  return (
    <div className={`um${imgs.length ? ' has-img' : ''}`} style={{ opacity: w.faded ? 0.8 : 1 }}>
      <div className="um-tx">
        {p.text ? <Md body={p.text} said /> : null}
        {p.files?.some((f: PendingFile) => !f.src) ? (
          <div className="um-files">
            {p.files.filter((f: PendingFile) => !f.src).map((f: PendingFile, i: number) => (
              <span className="um-file" key={`${i}-${f.name}`}>
                <Paperclip /> {f.name}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <span className="tm" role={failed ? 'alert' : undefined} title={w.title || undefined} style={failed ? { color: 'var(--fail)' } : undefined}>
        {text}
        {failed ? (
          <button type="button" aria-label="关闭" onClick={onClose} style={{ marginLeft: 6, background: 'none', border: 0, cursor: 'pointer', color: 'inherit' }}>×</button>
        ) : null}
      </span>
      {imgs.length ? <SentImages images={imgs} /> : null}
    </div>
  );
};

/** 聊天样式（管家、负责人）的刻度：你说的每一句；卡片正文是紧跟着的那条回答 */
function chatMarks(groups: Group[]): Mark[] {
  const out: Mark[] = [];
  for (const g of groups) {
    const reply: string = g.agent && g.agent.role !== 'event' ? oneLine(g.agent.body, 160) : '';
    for (const o of g.owner) {
      const said: string = oneLine(withoutImageLine(shownText(o), o.images), 90);
      const by: boolean = !!(o.by && o.by !== 'owner') || !!o.via;
      out.push({
        seq: o.seq,
        title: said || (o.images?.length ? '（图片）' : '（空）'),
        body: reply,
        meta: `${by ? `${saidLabel(o, !!o.answers?.length)} · ` : ''}${hhmm(o.started_at)}`,
        quiet: by,
      });
    }
  }
  return out;
}

/** 一个对话：话题、负责人、管家共用，都是聊天样式（T623：话题页不再有“第 N 轮”的行）。mode=topic 时回答的人是话题的 Harness，没做完的一轮带原因和按钮。 */
export const Conversation: React.FC<{
  topicKey: string;
  mode: 'topic' | 'chat';
  who?: string;
  placeholder: string;
  butler?: boolean;
  head?: React.ReactNode;
  /** 滚动区最上面的内容（手机话题页的总览） */
  lead?: React.ReactNode;
  /** 输入框下面那行小字；不给就是上下文 */
  extra?: React.ReactNode;
  /** 话题页：输入框下沿的配置栏（换配置，v20） */
  config?: React.ReactNode;
  /** 主人自己往下滚（true）或滚回顶上（false）：话题页拿它收起、展开总览。程序滚动（打开时定位、新内容贴底）不算 */
  onUserScroll?: (down: boolean) => void;
  /** 大厅：输入框可以切到“开新话题” */
  launch?: LaunchBox | null;
  /** 大厅（T624）：管家、负责人和发到私聊的话题结论一条时间线；给 lead 时只看和这位负责人有关的部分，说的话都交给它 */
  room?: { lead?: { key: string; project: string } };
}> = ({ topicKey, mode, who = '', placeholder, butler = false, head, lead, extra, config, onUserScroll, launch, room }) => {
  const desktop: boolean = useIsDesktop();
  const { data: snap } = useSnapshot();
  const tq = useTurns(topicKey);
  const rq = useRoomTurns(!!room);
  const roomTurns: RoomTurn[] = useMemo(() => {
    if (!room || !rq.data) return [];
    const who: string | null = rq.data.butler_key ?? snap?.butler_key ?? null;
    // v31：协作往返跟在发起它的那句话后面（cv/collab.ts roomCollabs）
    const all: RoomTurn[] = roomCollabs(buildRoom({ turns: rq.data.turns, outbox: rq.data.outbox, snap, butler: who }), rq.data.collabs ?? NO_COLLABS, snap, who);
    return room.lead ? onlyLead(all, room.lead.key, room.lead.project, snap, rq.data.butler_key ?? null) : all;
  }, [room, rq.data, snap]);
  // v31：这个对话认得的协作（大厅的、话题当汇总方或协助方的）
  const collabs: CollabView[] = (room ? rq.data?.collabs : tq.data?.collabs) ?? NO_COLLABS;
  const collabMap = useMemo(() => new Map<string, CollabView>(collabs.map((c: CollabView) => [c.key, c])), [collabs]);
  const butlerKey: string | null = rq.data?.butler_key ?? snap?.butler_key ?? null;
  // 专项页的负责人对话：负责人自己在等你的题照话题页画（T952，以前一律没有，只有从看板点进负责人才看得到）；大厅没有
  const roomAsk = room ? roomQuestion(!!room.lead, tq.data, rq.data?.outbox ?? []) : null;
  const data = room ? (rq.data ? { ...(tq.data ?? {}), turns: roomTurns as TurnView[], question: roomAsk?.question ?? null, outbox: rq.data.outbox } : undefined) : tq.data;
  const isPlaceholderData: boolean = room ? !rq.data : tq.isPlaceholderData;
  const isError: boolean = room ? rq.isError : tq.isError;
  const error: Error | null = room ? rq.error : tq.error;
  const refetch = room ? rq.refetch : tq.refetch;
  const pending: Pending[] = usePending(topicKey);
  const location = useLocation();
  const box = useRef<HTMLDivElement>(null);
  // 跟随到底和总览收起的状态（scrollFollow.ts）；latest：要不要显示「回到最新」
  const fs = useRef<F.Follow>(F.start(true));
  const [latest, setLatest] = useState<boolean>(false);
  const restored = useRef<boolean>(false);
  const [hl, setHl] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const send = useSendOutbox();
  const say = useSay(topicKey);
  const qc = useQueryClient();

  const topic: SnapshotTopic | undefined = snap?.topics.find((t: SnapshotTopic) => t.key === topicKey) ?? (tq.data?.topic as SnapshotTopic | undefined);
  const turns: TurnView[] = useMemo(() => (isPlaceholderData ? [] : (data?.turns ?? [])), [data, isPlaceholderData]);
  const outbox: OutboxView[] = data?.outbox ?? [];
  const maxSeq: number = turns.length ? turns[turns.length - 1].seq : 0;
  const running: boolean = turns.some((t: TurnView) => t.status === 'running') || (!!topic && isRunning(topic));
  const question = data?.question ?? null;
  const loaded: boolean = !isPlaceholderData;
  // 底部输入框写的字：有题在等时它就是回答（或随回答带上的补充说明），问题面板也要读
  const [draft, setDraftState] = useState<{ key: string; text: string }>(() => ({ key: topicKey, text: getDraft(topicKey) }));
  const text: string = draft.key === topicKey ? draft.text : getDraft(topicKey);
  const setText = (v: string): void => setDraftState({ key: topicKey, text: v });
  const input = useRef<ComposerHandle>(null);
  const form = useQuestionForm(topicKey, butler ? null : question, roomAsk ? roomAsk.ops : outbox);

  useEffect(() => {
    if (butler || !topic?.unseen || topic.kind !== 'topic') return;
    if (!markSeenSent(topicKey)) return;
    // 乐观更新：侧栏、看板马上记为看过，Covey 没办成再改回（sendOp.sendQuiet）
    sendQuiet(qc, { op_id: newOpId(), kind: 'seen', topic_key: topicKey, payload: {} });
  }, [butler, topic?.unseen, topic?.kind, topicKey, qc]);

  useEffect(() => {
    if (!pending.length || !loaded) return;
    const gone: string[] = [];
    for (const p of pending) {
      // 带附件的那一轮，Covey 在正文后面拼了附件块：比开头；只有附件没有字的，比“这之后来了你的一轮”
      // 补充进在跑的任务时，你那一行排在在跑那一轮前面、占了它原来的 seq（它挪到下一个）：同 seq 的，写下的时刻在发出之后（容两分钟钟差）也算回来了
      const back: TurnView | undefined = turns.find((t: TurnView) => t.role === 'owner'
        && (t.seq > p.baseSeq || (t.seq === p.baseSeq && (t.started_at ?? 0) * 1000 >= p.at - ECHO_SKEW_MS))
        && (p.files?.length ? t.body.trim().startsWith(p.text.trim()) : t.body.trim() === p.text.trim()));
      const echoed: boolean = !!back;
      // 你那一行回来了：接着画这里的本地图（T933）。Covey 的图没到时不退回“附件：图片 N 张”，到了也不用再取一遍、不闪
      if (back) keepLocalImages(back.key, localImagesOf(p.files));
      const row: OutboxView | undefined = outbox.find((o: OutboxView) => o.op_id === p.id);
      const landed: boolean = !!room && turns.some((t: TurnView) => t.key === `ob-${p.id}`);
      if (echoed || landed || (row?.state === 'done' && (ageMs(p.at) ?? 0) > 20000)) gone.push(p.id);
    }
    if (gone.length) removePending(topicKey, gone);
  }, [pending, turns, outbox, loaded, topicKey]);

  useLayoutEffect(() => {
    const el: HTMLDivElement | null = box.current;
    if (!el || !loaded || restored.current) return;
    restored.current = true;
    const s = getScroll(topicKey);
    if (s && !s.bottom) {
      el.scrollTop = s.top;
      fs.current = F.start(false, el.scrollTop);
    } else if (mode === 'topic' && !desktop) {
      // 手机：先看总览和最近几轮
      el.scrollTop = 0;
      fs.current = F.start(el.scrollHeight - el.clientHeight < F.NEAR_BOTTOM, 0);
    } else {
      el.scrollTop = el.scrollHeight;
      fs.current = F.start(true, el.scrollTop);
    }
    // 打开时的定位是程序的：它的滚动事件不算你滚的（还在 0 就不会有滚动事件）
    if (el.scrollTop > 0) fs.current = { ...fs.current, ownTop: el.scrollTop, lastTop: el.scrollTop };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, topicKey]);

  const signal = `${turns.length}:${turns[turns.length - 1]?.body.length ?? 0}:${turns[turns.length - 1]?.steps?.length ?? 0}:${pending.length}:${question?.token ?? ''}:${question?.state ?? ''}:${running}`;
  // 跟随时贴底：程序自己的滚动记进 ownTop，不当成你往下滚（T603：被当成往下滚会收起总览，滚动区随之滚不动）
  const pull = (): void => {
    const el: HTMLDivElement | null = box.current;
    if (!el || !restored.current || !fs.current.follow) return;
    const before: number = el.scrollTop;
    el.scrollTop = el.scrollHeight;
    fs.current = F.pulled(fs.current, before, el.scrollTop);
  };
  const metrics = (el: HTMLElement): F.Metrics => ({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight });
  const syncLatest = (): void => {
    const el: HTMLDivElement | null = box.current;
    if (el) setLatest(F.showLatest(fs.current, metrics(el)));
  };
  useLayoutEffect(() => {
    pull();
    syncLatest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal]);
  // 滚动区或内容的高度变了（图片载入、总览收起展开、展开一轮）：跟随时贴底；刚点过里面的东西就不拉，不在底部附近就停止跟随
  const clickAt = useRef<number>(0);
  useEffect(() => {
    const el: HTMLDivElement | null = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (Date.now() - clickAt.current < 1000) fs.current = F.clickedResize(fs.current, metrics(el));
      else pull();
      syncLatest();
    });
    ro.observe(el);
    const col: Element | null = el.firstElementChild;
    if (col) ro.observe(col);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, isError && !data]);

  // 刻度导航（JumpRail）点的跳转：和链接里的 ?seq= 走同一条路（展开那一轮、滚过去、闪一下），不改地址；换了地址就以地址为准
  const [jump, setJump] = useState<{ seq: number; id: number; at: string } | null>(null);
  const jumpNow: boolean = !!jump && jump.at === location.key;
  const wantSeq: number | null = (() => {
    if (jump && jumpNow) return jump.seq;
    const params = new URLSearchParams(location.search);
    const turnKey: string | null = params.get('turn');
    if (turnKey) {
      const hit: TurnView | undefined = turns.find((t: TurnView) => t.key === turnKey);
      if (hit) return hit.seq;
    }
    const v: string | null = params.get('seq');
    return v && Number.isFinite(Number(v)) ? Number(v) : null;
  })();
  const doneFor = useRef<string>('');
  useEffect(() => {
    if (wantSeq === null || !loaded) return;
    const id = `${location.key}:${wantSeq}:${jumpNow ? jump?.id : ''}`;
    if (doneFor.current === id) return;
    const el: Element | null | undefined = box.current?.querySelector(`[data-seq="${wantSeq}"]`);
    if (!el) return;
    doneFor.current = id;
    // 长的一轮居中会把开头滚出去（看着像被上面的概览卡盖住）：比半屏高就从开头露出来
    const feed: Element | null | undefined = box.current;
    const tall: boolean = !!feed && el.getBoundingClientRect().height > feed.clientHeight / 2;
    el.scrollIntoView({ block: tall ? 'start' : 'center' });
    fs.current = F.unfollow(fs.current);
    syncLatest();
    setHl(wantSeq);
    const t: number = window.setTimeout(() => setHl(null), 3000);
    return () => window.clearTimeout(t);
  }, [wantSeq, loaded, turns, location.key, jump]);

  // 你的滚动：最近 800ms 里有滚轮、触摸、按键或按下（滚动条），才算你滚的；程序自己的滚动由 ownTop 认出来
  const userAt = useRef<number>(0);
  const touchY = useRef<number | null>(null);
  const apply = (st: F.Step): void => {
    fs.current = st.s;
    if (st.overview && onUserScroll) onUserScroll(st.overview === 'compact');
    syncLatest();
  };
  const touched = (): void => {
    userAt.current = Date.now();
  };
  const towards = (dir: 'up' | 'down'): void => {
    const el: HTMLDivElement | null = box.current;
    touched();
    if (el) apply(F.intent(fs.current, dir, metrics(el)));
  };
  const onWheel = (e: React.WheelEvent): void => {
    if (e.deltaY < 0) towards('up');
    else if (e.deltaY > 0) towards('down');
    else touched();
  };
  const onTouchStart = (e: React.TouchEvent): void => {
    touched();
    touchY.current = e.touches[0]?.clientY ?? null;
  };
  const onTouchMove = (e: React.TouchEvent): void => {
    const y: number | undefined = e.touches[0]?.clientY;
    if (touchY.current === null || y === undefined) return touched();
    // 手指往下拖是往上看
    if (Math.abs(y - touchY.current) > 4) towards(y > touchY.current ? 'up' : 'down');
    touchY.current = y;
  };
  const onKeyDown = (e: React.KeyboardEvent): void => {
    const t = e.target as HTMLElement;
    if (t.closest('input, textarea, select, [contenteditable="true"]')) return;
    const dir = F.keyDir(e.key, e.shiftKey);
    if (dir) towards(dir);
    else touched();
  };
  const onPointerDown = (): void => {
    touched();
    clickAt.current = Date.now();
  };
  const onScroll = (): void => {
    const el: HTMLDivElement | null = box.current;
    if (!el) return;
    if (sel) setSel(null);
    apply(F.scrolled(fs.current, metrics(el), Date.now() - userAt.current < 800));
    const bottom: boolean = F.distance(metrics(el)) < F.NEAR_BOTTOM;
    setScroll(topicKey, { top: el.scrollTop, bottom });
  };
  const toLatest = (): void => {
    const el: HTMLDivElement | null = box.current;
    if (!el) return;
    fs.current = F.home(fs.current);
    setLatest(false);
    const reduce: boolean = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
  };

  // 分组：你说的几句 + 回答（cv/collab.ts groupTurns）。话题页先放进协作往返和协助方的回执（topicRows；没有协作时原样）
  const rows: TurnView[] = useMemo(() => (room ? turns : topicRows(turns, collabs, topicKey)), [room, turns, collabs, topicKey]);
  const groups: Group[] = useMemo(() => groupTurns(rows), [rows]);
  const agentGroups: Group[] = groups.filter((g: Group) => g.agent && g.agent.role !== 'event');
  // 刻度：你说的每一句一条（data-seq 和那个气泡对上），话题页和管家、负责人一样
  const marks: Mark[] = useMemo(() => (desktop && loaded ? chatMarks(groups) : []), [desktop, loaded, groups]);
  // 话题页回答的人：头像是话题自己的（T 号块，和侧栏、标题栏、大厅一样；负责人是专项头像，管家是管家头像，T756），
  // 名字是它跑的 Harness。原来按 Harness 画小图标，只认得 Codex，别的（agy、Pi……）都画成 Claude Code 的橙色星号
  const lastAgentSeq: number | null = [...groups].reverse().find((g: Group) => g.agent && g.agent.role !== 'event')?.agent?.seq ?? null;
  const topicWho: string = harnessName(topic?.harness ?? '') || 'Covey';
  const topicFace: { kind: 'butler' | 'lead' | 'topic'; avatar?: string; number?: number | null } = topic?.kind === 'butler'
    ? { kind: 'butler' }
    : topic?.kind === 'lead'
      ? { kind: 'lead', avatar: snap?.projects.find((p: SnapshotProject) => p.key === topic.project_key)?.avatar }
      : { kind: 'topic', number: topic?.number ?? null };
  // 不折叠
  const foldBelow: number = 0;

  // -- 点名、汇总方、引用（大厅和话题页，包 E）：正文里的 @ 胶囊就是参与者（按第一次插入的先后），不从手打的 @ 或 T 号猜；交给谁、谁汇总照 route --------
  const memberList: Member[] = useMemo(() => roomMembers(snap), [snap]);
  const fixedLead: Member | null = room?.lead ? (memberList.find((m: Member) => m.key === room.lead?.key) ?? null) : null;
  /** 点到的人（按第一次插入的先后；刷新后从草稿读回时按正文顺序）、主人选的汇总方、引用 */
  const [picks, setPicks] = useState<string[]>(() => participants([], getDraft(topicKey)));
  const [chosen, setChosen] = useState<string | null>(null);
  const [quote, setQuote] = useState<QuoteRef | null>(null);
  /** 光标前正在打的 @（查询词和位置），不在打是 null */
  const [trigger, setTrigger] = useState<Trigger | null>(null);
  const [active, setActive] = useState<number>(0);
  // 有题在等时底部输入框就是回答，点名先放下（话题页）
  const asking = question && question.state === 'open' ? question : null;
  const answering: boolean = !butler && !!asking && asking.questions.length > 0 && !form.locked;
  /** 能点名：大厅（专项页的负责人对话交给谁是定的）、普通话题的话题页（没在答题） */
  const canPick: boolean = room ? !fixedLead : mode === 'topic' && !butler && topic?.kind === 'topic' && !answering;
  const here: string | null = room ? null : topicKey;
  const planInput: PlanInput = {
    where: room ? 'room' : 'topic', here, picks: fixedLead ? [fixedLead.key] : canPick ? picks : [], integrator: chosen, quote, snap, butler: butlerKey, collabs,
  };
  const planRef = useRef<PlanInput>(planInput);
  planRef.current = planInput;
  const sayPlan: Plan = planSay(planInput);
  const cands: Candidate[] = useMemo(() => (canPick ? candidates(snap, here ? [here] : []) : []), [canPick, snap, here]);
  const found: Candidate[] | null = canPick && trigger ? searchCandidates(cands, trigger.query) : null;
  /** 胶囊和头像条上一位的样子（认不得的 key 不给，胶囊用自己存的名字） */
  const infoOf = (k: string): MentionInfo => mentionInfo(k, snap, butlerKey);
  // 正文变了：名单跟着胶囊走（后加人不偷偷改汇总方，删光就移出）
  const onText = (v: string): void => {
    setText(v);
    if (!canPick) return;
    const cur: PlanInput = planRef.current;
    const next = syncPicks(cur, v);
    if (next.integrator !== cur.integrator || next.picks.length !== cur.picks.length || next.picks.some((k: string, i: number) => k !== cur.picks[i])) {
      planRef.current = { ...cur, picks: next.picks, integrator: next.integrator };
      setPicks(next.picks);
      setChosen(next.integrator);
    }
  };
  const [sel, setSel] = useState<{ x: number; y: number; turn: RoomTurn; text: string } | null>(null);
  const focusInput = (): void => {
    window.requestAnimationFrame(() => input.current?.focus());
  };
  const putQuote = (q: QuoteRef): void => {
    setQuote(q);
    setSel(null);
    focusInput();
  };
  // 引用一条：结构化引用带它在自己话题里的 seq；属于一次认得的协作就带上它（话进那次协作）
  const quoteOf = (t: RoomTurn, part?: string): void => {
    const c: string = t.collab && collabMap.has(t.collab.key) ? t.collab.key : '';
    putQuote({
      key: t.key, topic_key: t.topic_key, ...(t.orig_seq ? { seq: t.orig_seq } : {}), ...(c ? { collab: c } : {}),
      who: speaker(t, snap, butlerKey).name, text: quoteText(part ?? t.body),
    });
  };
  // 选中一位：把“@查询词”换成胶囊（插入、名单由 onText 跟着算）
  const attrsOf = (k: string): MentionAttrs => {
    const i: MentionInfo = infoOf(k);
    return { key: k, kind: i.kind, label: i.label, ...(i.avatar ? { avatar: i.avatar } : {}), number: i.number ?? null };
  };
  const pickKey = (key: string): void => {
    input.current?.mention(attrsOf(key), trigger ?? undefined);
    setTrigger(null);
    setActive(0);
    focusInput();
  };
  // 移出一位：删掉正文里它的所有 @（名单、汇总方由 onText 重算，别的字不动）
  const dropKey = (key: string): void => {
    const v: string = dropMention(text, key);
    setDraft(topicKey, v);
    onText(v);
  };
  // 话题页“请人一起看”：在输入框末尾打一个 @（和自己打的一样），下面列出能请的
  const invite = (): void => {
    setActive(0);
    input.current?.insert(text && !/\s$/u.test(text) ? ' @' : '@');
  };
  // 顶栏「成员」点了一位：胶囊插在输入框光标处
  useEffect(() => (canPick ? onMentionRequest((a: MentionAttrs) => {
    input.current?.mention(a);
    focusInput();
  }) : undefined), [canPick]);
  // 胶囊要的环境：每位的样子、汇总方（带“汇总”角标）、点胶囊的菜单
  const mentionKeys: string[] = picks;
  const infoMap = useMemo(
    () => new Map<string, MentionInfo>(mentionKeys.filter((k: string) => knownKey(k, snap, butlerKey)).map((k: string) => [k, mentionInfo(k, snap, butlerKey)])),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mentionKeys.join('\u0000'), snap, butlerKey],
  );
  const summarizer: string | null = sayPlan.chip === 'collab' ? sayPlan.owner : null;
  const canSet: boolean = sayPlan.chip === 'collab' || sayPlan.chip === 'router';
  const setIntegrator = (k: string): void => setChosen(k);
  useEffect(() => {
    if (!canPick) return undefined;
    setMentionEnv({ info: infoMap, summarizer, canSet, setIntegrator, remove: dropKey });
    return undefined;
  });
  useEffect(() => () => clearMentionEnv(), []);
  const routeSend = (raw: string, files: SayFile[]): boolean => {
    // 正文里的胶囊记号发出去是纯文字 @T586、@管家：模型读得到谁做什么
    const value: string = plainMentions(raw);
    if (sayPlan.blocked) {
      toast.error(sayPlan.blocked);
      return false;
    }
    // 话题页没点人也没引用：和原来一样，就是对这个话题说
    if (!room && !picks.length && !quote) {
      say(value, maxSeq, files);
      return true;
    }
    const payload: Record<string, unknown> = sayPayload(value, files, planInput, sayPlan);
    const body: string = typeof payload.text === 'string' ? payload.text : '';
    const target: string | null = sayPlan.topicKey;
    if (!target || (!body && !files.length)) return false;
    const id: string = newOpId();
    const shown: PendingFile[] = files.map((f: SayFile) => ({ name: f.name, mime: f.mime, ...(f.mime.startsWith('image/') ? localImage(f.id) ?? {} : {}) }));
    addPending(topicKey, { id, text: body, at: Date.now(), baseSeq: maxSeq, phase: 'sending', ...(shown.length ? { files: shown } : {}) });
    send.mutate(
      { op_id: id, kind: 'say', topic_key: target, payload },
      { onSuccess: () => patchPending(topicKey, id, { phase: 'sent' }), onError: (e: Error) => patchPending(topicKey, id, { phase: 'failed', error: errorText(e) }) },
    );
    // 多选只管这一句：发出去就清掉，下一句不自动发给同样几位
    setQuote(null);
    setPicks([]);
    setChosen(null);
    setTrigger(null);
    return true;
  };
  const pickerKeys = (e: KeyLike): boolean => {
    if (!found) return false;
    if (e.key === 'Escape') {
      e.preventDefault();
      setTrigger(null);
      return true;
    }
    if (!found.length) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i: number) => (i + (e.key === 'ArrowDown' ? 1 : found.length - 1)) % found.length);
      return true;
    }
    if ((e.key === 'Enter' && !e.metaKey && !e.ctrlKey) || e.key === 'Tab') {
      e.preventDefault();
      pickKey(found[Math.min(active, found.length - 1)].key);
      return true;
    }
    return false;
  };
  const route: ComposerRoute | null = room || canPick ? {
    top: (
      <RouteTop
        plan={sayPlan}
        quote={quote}
        found={found}
        active={Math.min(active, Math.max(0, (found?.length ?? 1) - 1))}
        info={infoOf}
        onPick={pickKey}
        onClearQuote={() => setQuote(null)}
        onInvite={!room && canPick ? invite : undefined}
        hasPicks={picks.length > 0}
      />
    ),
    chip: fixedLead ? null : <RouteChip plan={sayPlan} info={infoOf} here={here} chosen={!!chosen} onIntegrator={(k: string | null) => setChosen(k)} />,
    title: sayPlan.title,
    keys: pickerKeys,
    send: routeSend,
    onTrigger: (t: Trigger | null) => {
      setTrigger(t);
      if (t) setActive(0);
    },
    ...(sayPlan.blocked ? { blocked: sayPlan.blocked } : {}),
  } : null;
  // 回执上的“回复这次协作”：引用带上协作，由它的汇总方接
  const replyCollab = (row: RoomTurn, c: CollabView | undefined): void => {
    if (!c) return;
    const asked: string = c.asks.find((a) => a.to === topicKey)?.text ?? '';
    putQuote({ key: row.key, topic_key: topicKey, ...(row.seq ? { seq: row.seq } : {}), collab: c.key, who: '这次协作', text: asked });
  };
  // 链接里的 ?collab=：滚到那次协作的往返（或回执），展开、闪一下
  const focusCollab: string | null = new URLSearchParams(location.search).get('collab');
  const collabFor = useRef<string>('');
  useEffect(() => {
    if (!focusCollab || !loaded) return;
    const id = `${location.key}:${focusCollab}`;
    if (collabFor.current === id) return;
    const el: Element | null | undefined = box.current?.querySelector(`[data-collab="${CSS.escape(focusCollab)}"]`);
    if (!el) return;
    collabFor.current = id;
    el.scrollIntoView({ block: 'center' });
    fs.current = F.unfollow(fs.current);
    syncLatest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusCollab, loaded, groups, location.key]);
  // 选中回答里的一段：浮出“引用这段”（只认一条回答之内的字）
  const onMouseUp = (): void => {
    if (!room) return;
    window.setTimeout(() => {
      const s = window.getSelection();
      const words: string = s?.toString().trim() ?? '';
      const node: Node | null | undefined = s?.anchorNode;
      const el: Element | null = node ? (node.nodeType === 1 ? (node as Element) : node.parentElement) : null;
      const host: Element | null | undefined = el?.closest('[data-room-key]');
      const focusEl: Node | null | undefined = s?.focusNode;
      if (!words || !host || !s?.rangeCount || (focusEl && !host.contains(focusEl))) {
        setSel(null);
        return;
      }
      const t: RoomTurn | undefined = roomTurns.find((x: RoomTurn) => x.key === host.getAttribute('data-room-key'));
      if (!t) return;
      const r: DOMRect = s.getRangeAt(0).getBoundingClientRect();
      setSel({ x: r.left + r.width / 2, y: r.top - 6, turn: t, text: words });
    }, 0);
  };
  const turnTopic = (key: string): SnapshotTopic | undefined => snap?.topics.find((x: SnapshotTopic) => x.key === key);
  // 大厅、负责人对话：每个说话人自己最新的一轮回答（停住了才给按钮），和它现在是不是在做、等你
  const roomLast: Map<string, string> = useMemo(() => {
    const m = new Map<string, string>();
    if (room) for (const t of roomTurns) if (t.role !== 'owner' && t.role !== 'event' && !t.synth && t.topic_key) m.set(t.topic_key, t.key);
    return m;
  }, [room, roomTurns]);
  const busyTopic = (key: string): boolean => {
    const t: SnapshotTopic | undefined = turnTopic(key);
    return !!t && (isRunning(t) || isWaiting(t));
  };

  // 停止（T765 F2）：点了就写“正在停…”并禁用，直到话题不在跑（running 为假，按钮自己没了）、停止失败或等满了没回音
  const [stopOp, setStopOp] = useState<{ id: string; posted: boolean } | null>(null);
  const stopTr = useTrackedOp(stopOp?.id);
  const stop = (): void => {
    const id: string = newOpId();
    send.mutate(
      { op_id: id, kind: 'stop', topic_key: topicKey, payload: {} },
      {
        onSuccess: () => setStopOp({ id, posted: true }),
        onError: (e: Error) => {
          setStopOp(null);
          toast.error(opWords('unsent', errorText(e)));
        },
      },
    );
    // 点击里直接 setState 会在请求发出去之前先整页重画一遍（话题页很大，慢机器上晚发几百毫秒；微任务里 setState 也一样，
    // React 当它是点击里的更新）：放到下一个任务，请求发出以后再写“正在停…”
    window.setTimeout(() => setStopOp((cur) => (cur?.id === id ? cur : { id, posted: false })), 0);
  };
  // 专项页里点开的成员话题也有配置栏（话题页由 TopicPage 给）：和话题页一样能换配置。
  // v36：专项页里只看这位负责人的那部分也有负责人的配置栏（同飞书专项卡上的「换配置」）；忙不忙只看负责人自己（这一路里还有专项话题的轮次）
  const leadBusy: boolean = (!!topic && (isRunning(topic) || toneOf(topic) === 'queued'))
    || roomTurns.some((t: RoomTurn) => t.topic_key === topicKey && t.status === 'running');
  const ownConfig: React.ReactNode = mode === 'topic' && !butler && topic?.kind === 'topic'
    ? <ConfigBar topic={topic} workspace={data?.topic.workspace ?? ''} busy={running || toneOf(topic) === 'queued'} />
    : room?.lead && !butler && topic?.kind === 'lead'
      ? <ConfigBar topic={topic} workspace={tq.data?.topic.workspace ?? ''} busy={leadBusy} />
      : undefined;
  // 大厅里负责人停住的那一轮点「换个模型」（v36 how=switch）：这一页没有它的配置栏，就去它的专项页展开（?switch=1）。
  // 已经在这位负责人的对话里还打不开（设置里还没有模型目录），就照旧说去飞书（专项卡上的「换配置」）
  const nav = useNavigate();
  const leadSwitch = (key: string): (() => void) | undefined => {
    const t: SnapshotTopic | undefined = turnTopic(key);
    if (room?.lead || t?.kind !== 'lead' || !t.project_key) return undefined;
    const to: string = `/projects/${encodeURIComponent(t.project_key)}?switch=1`;
    return () => { void nav(to); };
  };
  const stopPending: boolean = outbox.some((o: OutboxView) => o.kind === 'stop' && o.state === 'new');
  // 回执到了话题却还显示在跑（Covey 的新状态晚一步推来）也算正在停，最多再等 15 秒
  const stopping: boolean = stopPending || (!!stopOp && (!stopOp.posted || stopTr?.phase === 'waiting' || (stopTr?.phase === 'done' && running && Date.now() - stopTr.at < 15000)));

  if (isError && !data) {
    return (
      <div className="conv" style={{ placeItems: 'center', display: 'grid', textAlign: 'center' }}>
        <div>
          <p style={{ color: 'var(--fail)' }}>{errorText(error)}</p>
          <button type="button" className="btn" onClick={() => refetch()}>重试</button>
        </div>
      </div>
    );
  }
  const recentOps: OutboxView[] = outbox
    .filter((o: OutboxView) => o.kind !== 'say' && o.kind !== 'seen')
    .filter((o: OutboxView) => (o.state === 'done' ? (ageMs(o.created_at) ?? 0) < 60000 : o.state === 'failed' ? (ageMs(o.created_at) ?? 0) < 30 * 60000 : true))
    .slice(0, 3);
  const waiting: boolean = !!asking || (!!topic && isWaiting(topic));
  // 有题在等、还没交时 answering 为真（上面算的）：底部输入框就是回答（和飞书一样，话题里的一句话就是这道题的回答；几道题时随回答一起发出）
  const words = composerWords(form, text);
  const answerFromComposer = (value: string): boolean => {
    if (form.complete(value)) {
      form.submit(value);
      return true;
    }
    // 几道题没答完：字留着，滚到面板、标出没答的题
    form.nudge();
    window.requestAnimationFrame(() => {
      const panel: HTMLElement | null = document.getElementById('a-question');
      (panel?.querySelector<HTMLElement>('[data-missing="1"]') ?? panel)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    if (form.missing) toast.message(`还差 ${form.missing} 题没答：答完，或写一句补充说明再提交`);
    return false;
  };

  return (
    <div className="conv">
      {head}
      {room?.lead && asking ? <AskBand q={asking} /> : null}
      <div className="feedwrap">
      <div
        className="feed"
        ref={box}
        onScroll={onScroll}
        onWheel={onWheel}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onMouseUp={room ? onMouseUp : undefined}
        data-testid="turns"
      >
        <div className="col">
          {!loaded && !turns.length ? <p className="t3" style={{ textAlign: 'center', padding: 24 }}>载入对话…</p> : null}
          {loaded && !turns.length && !pending.length ? <p className="t3" style={{ textAlign: 'center', padding: 24 }}>还没有对话。</p> : null}
          {lead}
          {foldBelow > 0 ? <div className="daysep">更早的 {Math.max(0, foldBelow)} 轮</div> : null}
          {groups.map((g: Group, gi: number) => {
            const rt = g.agent as RoomTurn | null;
            if (rt?.synth === 'collab') {
              return <CollabBlock key={rt.key} c={collabMap.get(rt.ref ?? '')} seq={rt.seq} snap={snap} butler={butlerKey} focus={focusCollab === rt.ref} onQuote={putQuote} />;
            }
            if (rt?.synth === 'receipt') {
              const c: CollabView | undefined = collabMap.get(rt.ref ?? '');
              return (
                <CollabReceipt key={rt.key} r={receiptOf(rt, c, topicKey, snap, butlerKey)} rows={rt.rows ?? []} c={c} snap={snap} butler={butlerKey} onReply={() => replyCollab(rt, c)} />
              );
            }
            if (room && rt?.synth === 'project') return <ProjectEvent key={rt.key} t={rt} />;
            if (room && rt?.synth === 'question') {
              // 负责人自己的题就在这页底下（T952）：去答滚过去
              const here: boolean = !!asking && rt.topic_key === room.lead?.key;
              return <QuestionRow key={rt.key} t={rt} topic={turnTopic(rt.topic_key)} butler={rt.topic_key === butlerKey} onAnswer={here ? toQuestion : undefined} />;
            }
            if (g.agent && g.agent.role === 'event') {
              return (
                <div className="mem" key={g.agent.key} data-seq={g.agent.seq} style={{ justifyContent: 'center', margin: '8px 0' }}>
                  <span>{g.agent.body}</span>
                  <span className="t3">{hhmm(g.agent.started_at ?? g.agent.ended_at)}</span>
                </div>
              );
            }
            const folded: boolean = !!g.agent && g.n <= foldBelow && !expanded.has(g.n) && hl !== g.agent.seq;
            if (folded && g.agent) {
              const first: string = g.agent.body.replace(/[#>*`|_-]+/gu, ' ').replace(/\s+/gu, ' ').trim().slice(0, 60);
              return (
                <button
                  type="button"
                  key={g.agent.key}
                  className="old"
                  data-seq={g.agent.seq}
                  style={{ display: 'block', width: '100%', textAlign: 'left', cursor: 'pointer' }}
                  aria-expanded="false"
                  onClick={() => setExpanded((cur: Set<number>) => new Set([...cur, g.n]))}
                >
                  <span className="tl1">
                    <ChevronRight style={{ width: 14 }} />
                    <b style={{ color: 'var(--t1)' }}>第 {g.n} 轮</b>
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--t2)' }}>{first}</span>
                    <span>{g.agent.seconds ? fmtDur(g.agent.seconds, false) : ''}</span>
                    <span>{hhmm(g.agent.started_at)}</span>
                  </span>
                </button>
              );
            }
            return (
              <React.Fragment key={g.agent?.key ?? `o${gi}`}>
                {mode === 'topic' ? (
                  <ChatRound
                    owner={g.owner}
                    agent={g.agent}
                    hl={hl}
                    who={topicWho}
                    avatar={<RoleAvatar who={topicFace} />}
                    fail={g.agent ? (
                      <FailBox
                        failure={g.agent.failure}
                        turn={g.agent}
                        topicKey={topicKey}
                        actionable={g.agent.seq === lastAgentSeq && !running && !asking}
                        pressing={!topic || stopPressing(topic)}
                        canSwitch={topic?.kind === 'topic'}
                      />
                    ) : undefined}
                    hideBody={!!g.agent && stopWords(g.agent)}
                    topicKey={topicKey}
                  />
                ) : room && rt && g.agent && speaker(rt, snap, butlerKey).kind === 'topic' ? (
                  <RelayTurn t={rt} topic={turnTopic(rt.topic_key)} hl={hl === rt.seq} onQuote={() => quoteOf(rt)} />
                ) : room ? (
                  <>
                    {g.owner.map((o: TurnView) => (
                      <OwnerBubble key={o.key} o={o} answers={g.agent?.answers} hl={hl === o.seq} route={(o as RoomTurn).route ? <RouteWho t={o as RoomTurn} snap={snap} butler={butlerKey} /> : undefined} />
                    ))}
                    {g.agent && rt ? (
                      <AgentTurn
                        turn={g.agent}
                        hl={hl === g.agent.seq}
                        who={speaker(rt, snap, butlerKey).name}
                        avatar={<RoleAvatar who={speaker(rt, snap, butlerKey)} />}
                        onQuote={() => quoteOf(rt)}
                        roomKey={rt.key}
                        // 停住的那一轮，正文是停下时的原话（已在技术详情里），不当回答画（同话题页）
                        hideBody={stopWords(g.agent)}
                        fail={g.agent.status === 'failed' && g.agent.failure ? (
                          // v33：管家、负责人停住的那一轮也能「检查并继续」（recover 认管家和负责人）；只给它自己最新的一轮、没在跑时
                          <FailBox
                            failure={g.agent.failure}
                            turn={{ key: g.agent.key, job_id: g.agent.job_id }}
                            topicKey={rt.topic_key}
                            actionable={roomLast.get(rt.topic_key) === rt.key && !busyTopic(rt.topic_key)}
                            pressing={false}
                            canSwitch={false}
                            onSwitch={leadSwitch(rt.topic_key)}
                          />
                        ) : undefined}
                      />
                    ) : null}
                  </>
                ) : (
                  <ChatRound owner={g.owner} agent={g.agent} hl={hl} who={who} />
                )}
              </React.Fragment>
            );
          })}
          {pending.map((p: Pending) => (
            <PendingBubble key={p.id} p={p} row={outbox.find((o: OutboxView) => o.op_id === p.id)} onClose={() => removePending(topicKey, [p.id])} />
          ))}
          {recentOps.map((o: OutboxView) => (
            <div className="mem" key={o.op_id} role={o.state === 'failed' ? 'alert' : undefined} style={{ justifyContent: 'center', color: o.state === 'failed' ? 'var(--fail)' : undefined }}>
              <span>{opLine(o)}</span>
            </div>
          ))}
          {mode !== 'topic' && !asking && !running && !butler && !turns[turns.length - 1]?.failure && ((!!topic && toneOf(topic) === 'stopped') || turns[turns.length - 1]?.status === 'failed') ? (
            <p className="t3" role="note" style={{ fontSize: 12, textAlign: 'center', margin: '10px 0' }}>这一轮没有做完。在下面写一句话，会开始下一轮。</p>
          ) : null}
          {question ? (
            <div id="a-question">
              <QuestionPanel key={question.token} form={form} text={text} onWrite={() => input.current?.focus()} />
            </div>
          ) : null}
        </div>
      </div>
      {marks.length > 1 ? (
        <JumpRail marks={marks} feed={box} onJump={(seq: number) => setJump((j) => ({ seq, id: (j?.id ?? 0) + 1, at: location.key }))} />
      ) : null}
      {sel ? <SelectionQuote at={{ x: sel.x, y: sel.y }} onClick={() => quoteOf(sel.turn, sel.text)} /> : null}
      {latest ? (
        <button type="button" className="btn sm to-latest" onClick={toLatest} data-testid="to-latest">
          <ArrowDown /> 回到最新
        </button>
      ) : null}
      </div>
      {running && topic && !butler ? <LivePill topic={topic} /> : null}
      {/* 话题在等你、题还没画出来时，话题里的一句话就是那道题的回答；负责人不是（和飞书一样，不引用问题卡的话是它新的一轮，题照旧等着；T952） */}
      <Composer
        key={topicKey}
        topicKey={topicKey}
        maxSeq={maxSeq}
        value={text}
        onValue={onText}
        inputRef={input}
        placeholder={answering ? words.placeholder : waiting && !butler && topic?.kind !== 'lead' ? '写你的回答' : placeholder}
        hint={answering ? words.hint : undefined}
        onSubmit={answering ? answerFromComposer : undefined}
        canSubmit={answering ? form.complete(text) : undefined}
        launch={launch}
        route={route}
        config={config ?? ownConfig}
        sendLabel={answering ? '提交回答' : '发送'}
        onStop={running && !butler ? stop : undefined}
        stopping={stopping}
        extra={extra !== undefined ? extra : topic?.context ? <ContextMeter used={topic.context.used} limit={ctxLimitOk(topic.context) ? topic.context.limit : 0} /> : undefined}
      />
    </div>
  );
};



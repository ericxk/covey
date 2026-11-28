import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Check, ChevronDown, ChevronRight, CornerUpLeft, Handshake, Quote as QuoteIcon, UserPlus, X } from 'lucide-react';
import type { QuestionView, SnapshotResponse, SnapshotTopic } from '@shared/api.interface';
import { TOPIC } from '@shared/collab';
import { Md } from './Md';
import { hasPicture, RoleAvatar as Picture, TopicBadge } from './RoleAvatar';
import { AnswerFoot } from './AnswerFoot';
import { ArtifactFold } from './ArtifactFold';
import { hhmm } from './lib';
import { askBand, firstParagraph } from './topicText';
import type { RoomTurn, RouteView } from './room';
import { knownKey, mentionInfo, type Candidate, type MentionInfo, type Plan, type QuoteRef } from './collab';
import './room.css';
import './collab.css';

/** 点开别处就收起、Esc 收起 */
function useAway(open: boolean, close: () => void): React.RefObject<HTMLSpanElement> {
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e: MouseEvent): void => {
      if (!box.current?.contains(e.target as Node)) close();
    };
    const esc = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') close();
    };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open, close]);
  return box;
}

/** 一位的头像（管家、负责人的固定头像，话题是它的 T 号）；不是 bare 时管家、负责人后面带名字（话题的 T 号头像自己就写着号码）。
 *  刷新后从草稿读回的胶囊没有号码，从名字“T586”里认 */
export const Who: React.FC<{ info: MentionInfo; bare?: boolean }> = ({ info, bare = false }) => {
  const num: number | null = info.number ?? (/^T(\d+)$/u.exec(info.label)?.[1] ? Number(/^T(\d+)$/u.exec(info.label)?.[1]) : null);
  return (
    <span className={`who ${info.kind}`} data-who={info.key}>
      <RoleAvatar who={{ kind: info.kind, avatar: info.avatar, number: num }} />
      {!bare && info.kind !== 'topic' ? <span className="who-n">{info.label}</span> : null}
    </span>
  );
};

/** 底栏那颗汇总小胶囊：多人协作“由 [T586] 汇总 ▾”（点开在参与者里换一位）、只点了负责人“由管家归口 ▾”（可指定其中一位）、协助方超过 3 位变红；
 *  单 @、没 @ 什么都不画；回复这次协作写在引用条上，这里不画 */
export const RouteChip: React.FC<{
  plan: Plan;
  info: (key: string) => MentionInfo;
  here: string | null;
  chosen: boolean;
  onIntegrator: (key: string | null) => void;
}> = ({ plan, info, here, chosen, onIntegrator }) => {
  const [open, setOpen] = useState<boolean>(false);
  const close = React.useCallback(() => setOpen(false), []);
  const box = useAway(open, close);
  if (plan.chip === 'bad') {
    return <span className="rc bad" role="alert" data-testid="room-blocked"><AlertTriangle />{plan.blocked}</span>;
  }
  if (plan.chip !== 'collab' && plan.chip !== 'router') return null;
  const router: boolean = plan.chip === 'router';
  const owner: MentionInfo | null = router ? null : info(plan.owner);
  return (
    <span className="rc-wrap" ref={box}>
      <button
        type="button"
        className={`rc${open ? ' open' : ''}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={plan.title}
        data-testid="route-chip"
      >
        {router ? (
          <>由管家归口</>
        ) : (
          <>
            <Handshake />由 {owner ? <Who info={owner} /> : null}{here && plan.owner === here ? '（这个话题）' : ''} 汇总
          </>
        )}
        <ChevronDown className="rc-d" />
      </button>
      {open ? (
        <span className="rm-menu rc-menu" role="menu" aria-label="换一位汇总">
          {router ? <span className="rc-h t3">管家先找出这件事归谁；也可以指定其中一位</span> : null}
          {!router && owner ? <span className="rc-cur" role="menuitem" aria-disabled="true"><Check /><Who info={owner} /><span className="t3">{here && plan.owner === here ? '这个话题' : '汇总方'}</span></span> : null}
          {plan.choices.map((k: string) => (
            <button type="button" role="menuitem" key={k} onClick={() => { onIntegrator(k); close(); }} data-testid="change-integrator-pick">
              <span className="rc-gap" />由 <Who info={info(k)} /> 汇总
            </button>
          ))}
          {chosen ? <button type="button" role="menuitem" onClick={() => { onIntegrator(null); close(); }}><span className="t3">用默认的那位</span></button> : null}
        </span>
      ) : null}
    </span>
  );
};

/** 输入框上沿：打 @ 时的候选（每行头像、名字，话题再带标题和状态）、引用哪条；话题页没点人时一个“请人一起看”。
 *  点到的人在正文里是胶囊，由谁汇总在底栏（RouteChip），这里不再有“参与”芯片行和“由谁汇总”整行 */
export const RouteTop: React.FC<{
  plan: Plan;
  quote: QuoteRef | null;
  found: Candidate[] | null;
  active: number;
  info: (key: string) => MentionInfo;
  onPick: (key: string) => void;
  onClearQuote: () => void;
  /** 话题页：没点人时露一个“请人一起看”（在输入框里打一个 @） */
  onInvite?: () => void;
  /** 正文里已经有人了 */
  hasPicks: boolean;
}> = ({ plan, quote, found, active, info, onPick, onClearQuote, onInvite, hasPicks }) => (
  <div className="rm-top">
    {found ? (
      <div className="rm-picks" role="listbox" aria-label="@ 谁" data-testid="mention-list">
        {found.length ? found.map((c: Candidate, i: number) => (
          <button
            type="button"
            role="option"
            aria-selected={i === active}
            key={c.key}
            className={`rm-pick${i === active ? ' on' : ''}${c.dim ? ' dim' : ''}`}
            data-key={c.key}
            onMouseDown={(e: React.MouseEvent) => {
              e.preventDefault();
              onPick(c.key);
            }}
          >
            <Who info={info(c.key)} />
            {c.role === TOPIC ? <span className="rm-ptitle">{c.title}</span> : <span className="rm-ptitle" />}
            {c.role === TOPIC && c.state ? <span className="rm-pstate">{c.state}</span> : c.dim ? <span className="t3">收起了</span> : null}
          </button>
        )) : <span className="t3 rm-none">没找到：管家、负责人按名字，话题按 T 号或标题找</span>}
      </div>
    ) : null}
    {quote ? (
      <div className="rm-quote" data-testid="room-quote">
        <QuoteIcon />
        <span className="rm-qwho">
          {quote.collab && plan.collab ? <>回复这次协作 · 由 <Who info={info(plan.owner)} /> 汇总</> : `引用 ${quote.who}`}
        </span>
        <span className="rm-qtx">{(quote.text || quote.who).replace(/\s+/gu, ' ')}</span>
        <button type="button" className="rm-x" onClick={onClearQuote} aria-label="取消引用" title="取消引用"><X /></button>
      </div>
    ) : null}
    {onInvite && !hasPicks && !found && !quote ? (
      <button type="button" className="rm-invite" onClick={onInvite} title="请别的话题、负责人或管家一起看，这个话题汇总" data-testid="invite">
        <UserPlus />请人一起看
      </button>
    ) : null}
  </div>
);

/** 你说的那句下面的去向：“→ [头像] 汇总 · 协助 [头像][头像]”“→ 管家 → [T12]”；认不全时画文字 */
export const RouteWho: React.FC<{ t: RoomTurn; snap: SnapshotResponse | undefined; butler: string | null }> = ({ t, snap, butler }) => {
  const v: RouteView | undefined = t.routeView;
  const keys: string[] = v ? [...(v.integrator ? [v.integrator] : []), ...(v.helpers ?? []), ...v.to] : [];
  if (!v || !t.route || !keys.every((k: string) => knownKey(k, snap, butler)) || (v.via && !butler)) return <>{t.route}</>;
  const who = (k: string): React.ReactNode => <Who key={k} info={mentionInfo(k, snap, butler)} />;
  return (
    <span className="rt" data-testid="route-who" title={t.route}>
      →{' '}
      {v.via && butler ? <>{who(butler)}{v.to.length ? ' → ' : ''}</> : null}
      {v.integrator ? <>{who(v.integrator)} 汇总{v.helpers?.length ? <> · 协助 {v.helpers.map(who)}</> : null}</> : v.to.map(who)}
    </span>
  );
};

/** 一条话题结论（发到私聊的那一轮）：收成一行，点开看全文；可以引用、打开话题 */
export const RelayTurn: React.FC<{ t: RoomTurn; topic: SnapshotTopic | undefined; hl: boolean; onQuote: () => void }> = ({ t, topic, hl, onQuote }) => {
  const [open, setOpen] = useState<boolean>(false);
  // 和飞书私聊区的回带一样：T 号是头像，名字只写标题
  const name: string = topic?.title || '话题';
  const failed: boolean = t.status === 'failed' || t.status === 'stopped';
  return (
    <div className={`rm-relay${hl ? ' hl' : ''}`} data-seq={t.seq} data-room-key={t.key}>
      <button type="button" className="rm-rhead" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={topic?.number ? `T${topic.number} ${name}` : undefined}>
        {open ? <ChevronDown /> : <ChevronRight />}
        <TopicBadge number={topic?.number} />
        <b>{name}</b>
        <span className={t.status === 'failed' ? 'rm-bad' : 't3'}>{t.status === 'stopped' ? '已停止' : failed ? '停住了' : '做完了'}</span>
        <span className="rm-first">{open ? '' : firstParagraph(t.body, 120)}</span>
        <span className="t3 rm-tm">{hhmm(t.room_at)}</span>
      </button>
      <span className="rm-acts">
        <button type="button" className="rm-act" onClick={onQuote} title="引用这条：接着对这个话题说"><CornerUpLeft />引用</button>
        <Link className="rm-act" to={`/topics/${encodeURIComponent(t.topic_key)}`} title="打开这个话题">打开 ›</Link>
      </span>
      {open ? (
        <div className="rm-rbody">
          <Md body={t.body} dim={false} />
          <ArtifactFold items={t.artifacts} />
          {/* 和飞书回带卡一样，展开后有输入、输出两行（T647 P2） */}
          <AnswerFoot turn={t} />
        </div>
      ) : null}
    </div>
  );
};

/** 一道等你回答的题：哪个话题在问、问的第一句，去答。onAnswer：题就在这页底下（专项页负责人自己的题，T952），去答是滚过去，不换页 */
export const QuestionRow: React.FC<{ t: RoomTurn; topic: SnapshotTopic | undefined; butler: boolean; onAnswer?: () => void }> = ({ t, topic, butler, onAnswer }) => (
  <div className="rm-ask" data-seq={t.seq} data-room-key={t.key}>
    {butler ? <RoleAvatar who={{ kind: 'butler' }} /> : <TopicBadge number={topic?.number} />}
    {butler ? <b>管家</b> : topic?.number ? null : <b>话题</b>}
    <span className="rm-wait">等你</span>
    <span className="t2">问你：{t.body}</span>
    <span className="t3 rm-tm">{hhmm(t.room_at)}</span>
    {butler ? (
      <span className="t3">去飞书答</span>
    ) : onAnswer ? (
      <button type="button" className="rm-act" onClick={onAnswer}>去答 ›</button>
    ) : (
      <Link className="rm-act" to={`/topics/${encodeURIComponent(t.topic_key)}`}>去答 ›</Link>
    )}
  </div>
);

/** 滚到底下的问题面板（Conversation 的 #a-question） */
export const toQuestion = (): void => document.getElementById('a-question')?.scrollIntoView({ block: 'start', behavior: 'smooth' });

/** 专项页负责人对话的顶上（T952）：负责人在等你答的题，和话题页总览的“等你 · 要你定”同一条带子；去回答滚到底下的问题面板 */
export const AskBand: React.FC<{ q: QuestionView }> = ({ q }) => {
  const { label, head } = askBand(q);
  return (
    <div className="a-ovwrap rm-askband">
      <section className="a-ov ov2" aria-label="等你 · 要你定">
        <div className="ov2-band">
          <div className="ov2-l1">{label}</div>
          <h2 className="ov2-head">{head}</h2>
          <div className="ov2-act">
            <button type="button" className="btn pri sm" onClick={toQuestion}>去回答</button>
          </div>
        </div>
      </section>
    </div>
  );
};

/** 专项建好、做完或降级：负责人加入、离开 */
export const ProjectEvent: React.FC<{ t: RoomTurn }> = ({ t }) => (
  <div className="rm-event" data-seq={t.seq}>
    <span>{t.body}</span>
    <span className="t3">{hhmm(t.room_at)}</span>
  </div>
);

/** 回答下面的“引用”（管家、负责人的回答） */
export const QuoteButton: React.FC<{ onClick: () => void }> = ({ onClick }) => (
  <button type="button" className="rm-act rm-qbtn" onClick={onClick} title="引用这条：接着对说这话的人说"><CornerUpLeft />引用</button>
);

/** 选中一段字时浮出的“引用这段” */
export const SelectionQuote: React.FC<{ at: { x: number; y: number }; onClick: () => void }> = ({ at, onClick }) => (
  <button
    type="button"
    className="rm-selq"
    style={{ left: at.x, top: at.y }}
    onMouseDown={(e: React.MouseEvent) => e.preventDefault()}
    onClick={onClick}
  >
    <QuoteIcon />引用这段
  </button>
);

/** 大厅说话人的头像，和飞书私聊区一样（T645）：管家是管家那张图，负责人是它专项的固定头像（旧数据没分到时一个“负”字），
 *  话题是它的 T 号（自己的颜色上白色粗体） */
export const RoleAvatar: React.FC<{ who: { kind: 'butler' | 'lead' | 'topic'; avatar?: string; number?: number | null } }> = ({ who }) => {
  if (who.kind === 'topic') return <TopicBadge number={who.number} />;
  const name: string | undefined = who.kind === 'butler' ? 'butler' : who.avatar;
  if (hasPicture(name)) return <span className="rm-pic"><Picture name={name} size={22} /></span>;
  return <span className={`avc rm-av ${who.kind}`}>{who.kind === 'lead' ? '负' : '管'}</span>;
};

import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { StepView, TurnAnswer, TurnImage, TurnView } from '@shared/api.interface';
import { AnswerFoot } from './AnswerFoot';
import { ArtifactFold } from './ArtifactFold';
import { Md } from './Md';
import { QuoteButton } from './RoomBits';
import { Waterfall } from './Waterfall';
import { Dot, Pill, useTick } from './ui';
import { ctxLimitOk, fmtDur, fmtTok, fmtWindow, hhmm, turnStats } from './lib';
import { isAnswerBody, saidLabel, shownText } from './topicText';
import { Notes, StartedChips } from './turnBits';
import { SentImages, ShownImages, withoutImageLine, type DrawnImage } from './TurnImages';
import { useLocalImages, type LocalImage } from '@client/src/lib/conv-store';

/* 一轮对话在大厅（管家、负责人）和话题页里画法一样（T623）：你说的是靠右的气泡，回答前一行是谁、几点，
   下面一颗“工具 N · 用时”点开是过程（瀑布和 token、速度、上下文），再是正文、产物（折叠，T645）、输入/输出两行、图。
   话题页只多一行“第 N 轮 → 结论”当目录。 */

/** 回答问题卡的那一句：问题 → 选择。bare 时不画自己的标题（在气泡里，标题是气泡那一行） */
export const AnswerTable: React.FC<{ answers: TurnAnswer[]; label?: string; bare?: boolean }> = ({ answers, label = '你的回答', bare }) => (
  <div className="a-blk">
    {bare ? null : <div className="a-bl">{label}（问题 → {label === '你的回答' ? '你的选择' : '选择'}）</div>}
    <table className="a-qa">
      <thead>
        <tr><th>问题</th><th aria-hidden="true" /><th>{label === '你的回答' ? '你的选择' : '选择'}</th></tr>
      </thead>
      <tbody>
        {answers.map((a: TurnAnswer, i: number) => (
          <tr key={i}>
            <td className="q"><span className="a-qq" title={a.question}>{a.question}</span></td>
            <td className="ar" aria-hidden="true">→</td>
            <td className={`a${a.choice === '没答' || a.choice === '跳过' ? ' skip' : ''}`}>{a.choice}</td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const isAnswer = (o: TurnView): boolean => !!o.answers?.length || isAnswerBody(o.body);

/** 跳到某一轮时闪的那一圈：品牌色（橙色只说等你和可能卡住，docs/ui.md 2.2） */
const JUMP_RING = '0 0 0 2px color-mix(in srgb, var(--a1) 70%, transparent)';

/** 你说的一句（或负责人、管家替你说的）：靠右的气泡。答题的那句画成“问题 → 选择”的表，选项外写的字跟在下面；改过的能看原文 */
export const OwnerBubble: React.FC<{
  o: TurnView; answers?: TurnAnswer[] | null; hl?: boolean;
  /** 大厅（T624）：这句交给了谁（“→ 管家 → T12”），写在时间后面 */
  route?: React.ReactNode;
}> = ({ o, answers, hl, route }) => {
  const [raw, setRaw] = useState<boolean>(false);
  const answer: boolean = isAnswer(o);
  const qa: TurnAnswer[] | null = answer ? (o.answers?.length ? o.answers : answers ?? null) : null;
  const label: string | null = answer || (o.by && o.by !== 'owner') || o.via || o.edited ? saidLabel(o, answer) : null;
  // 刚从本页发出的这一行（T933）：Covey 的图没到时画本页留下的图；到了用 Covey 的编号和尺寸，图还是画本页已有的那张，不再取一遍、不闪
  const local: LocalImage[] = useLocalImages(o.key);
  const imgs: DrawnImage[] | undefined = o.images?.length
    ? local.length === o.images.length ? o.images.map((img: TurnImage, i: number): DrawnImage => (img.gone ? img : { ...img, src: local[i].src })) : o.images
    : local.length ? local : undefined;
  return (
    <div className={`um${imgs?.length ? ' has-img' : ''}${qa ? ' has-qa' : ''}`} data-seq={o.seq} style={hl ? { boxShadow: JUMP_RING } : undefined}>
      <div className="um-tx">
        {label ? (
          <span className="a-who">
            {label}
            {o.edited ? ' · 改过' : ''}
            {o.edited ? (
              <button type="button" className="a-x um-raw" onClick={() => setRaw(!raw)}>{raw ? '看改后的' : '看原文'}</button>
            ) : null}
          </span>
        ) : null}
        {raw ? (
          <div className="a-saytx">{o.body}</div>
        ) : qa ? (
          <>
            <AnswerTable answers={qa} label={saidLabel(o, true)} bare />
            {o.note ? <Md body={o.note} said /> : null}
          </>
        ) : (
          <Md body={withoutImageLine(shownText(o), imgs)} said />
        )}
      </div>
      <span className="tm">
        {hhmm(o.started_at)}
        {route ? <span className="rm-route"> {route}</span> : null}
      </span>
      {imgs?.length ? <SentImages images={imgs} /> : null}
    </div>
  );
};

/** 过程面板下面的数：token、速度、上下文 */
const Figures: React.FC<{ turn: TurnView }> = ({ turn }) => {
  if (!turn.usage && !turn.speed && !turn.context) return null;
  return (
    <div className="a-figs">
      {turn.usage ? (
        <span>
          token {fmtTok(turn.usage.total)}
          {turn.usage.input !== undefined ? ` · 输入 ${fmtTok(turn.usage.input)}` : ''}
          {turn.usage.input !== undefined && turn.usage.cache_read ? `（其中缓存 ${fmtTok(turn.usage.cache_read)}）` : ''}
          {turn.usage.output !== undefined ? ` · 输出 ${fmtTok(turn.usage.output)}` : ''}
        </span>
      ) : null}
      {turn.speed ? <span>速度 {Math.round(turn.speed)} token/s</span> : null}
      {turn.context ? (
        <span>
          上下文 {fmtTok(turn.context.used)}
          {ctxLimitOk(turn.context) ? ` / ${fmtWindow(turn.context.limit)}（${Math.round((turn.context.used / turn.context.limit) * 100)}%）` : ''}
        </span>
      ) : null}
    </div>
  );
};

/** 回答的那一轮：谁、几点 → 工具（点开是过程）→ 正文 → 图、说明、开的话题、产物 → 没做完时的那一块 */
export const AgentTurn: React.FC<{
  turn: TurnView;
  hl: boolean;
  who: string;
  /** 头像：不给就是名字的第一个字 */
  avatar?: React.ReactNode;
  /** 没做完时画在最后的（话题页是原因和按钮）；不给就是一行字 */
  fail?: React.ReactNode;
  /** 这一轮的正文不画（话题页：停止时只有一句“已停止”） */
  hideBody?: boolean;
  /** 大厅（T624）：名字旁的“引用”，和选中一段时认出是哪一条 */
  onQuote?: () => void;
  roomKey?: string;
  /** 这一轮所在的话题（记忆的「不对」发给它，v34）；大厅里的轮次自己带 topic_key */
  topicKey?: string;
}> = ({ turn, hl, who, avatar, fail, hideBody, onQuote, roomKey, topicKey }) => {
  const live: boolean = turn.status === 'running';
  const now: number = useTick(live ? 1000 : 3600000);
  const [open, setOpen] = useState<boolean>(false);
  const st = turnStats(turn);
  const elapsed: number | null = live && turn.started_at ? now - turn.started_at : (st?.seconds ?? turn.seconds ?? null);
  const hasFigs: boolean = !!(turn.usage || turn.speed || turn.context);
  const chip: boolean = !live && !!(st || hasFigs);
  // 在跑的一轮也默认折成一行（T753，学 Linear、Cursor：过程是细节，要看再展开），跑完的一轮折成“工具 N · 用时”
  const showFall: boolean = open && (!!turn.steps?.length || hasFigs);
  const cur: StepView | undefined = live && turn.steps?.length ? ([...turn.steps].reverse().find((s: StepView) => s.status === 'running') ?? turn.steps[turn.steps.length - 1]) : undefined;
  const ring: React.CSSProperties = hl ? { boxShadow: JUMP_RING, borderRadius: 10 } : {};
  return (
    <div className="msg" data-seq={turn.seq} data-room-key={roomKey} style={ring}>
      <div className="who">
        {avatar ?? <span className="avc">{who.slice(0, 1)}</span>}
        <b>{who}</b>
        <span>{hhmm(turn.ended_at || turn.started_at)}</span>
        {live ? <Pill tone="running" /> : null}
        {onQuote && !live ? <QuoteButton onClick={onQuote} /> : null}
      </div>
      {chip ? (
        <button type="button" className="fold" onClick={() => setOpen(!open)} aria-expanded={open} style={{ border: 0, cursor: 'pointer' }}>
          {open ? <ChevronDown /> : <ChevronRight />}
          {st ? <>工具 {st.tools}{st.thinks ? ` · 思考 ${st.thinks}` : ''}</> : '过程'}
          {elapsed ? ` · ${fmtDur(elapsed)}` : ''}
        </button>
      ) : null}
      {cur ? (
        <button type="button" className="fold livefold" onClick={() => setOpen(!open)} aria-expanded={open} title={open ? '收起过程' : '展开过程'}>
          <Dot tone="running" />
          <b>{turn.steps?.length} 步</b>
          {elapsed ? <span className="lf-t">{fmtDur(elapsed)}</span> : null}
          <span className="lf-now">现在：{cur.label}</span>
          {open ? <ChevronDown /> : <ChevronRight />}
        </button>
      ) : null}
      {showFall ? (
        <div className="wfc">
          <div className="wh">
            <b>过程</b>
            <span className="t3">{live ? `本轮 ${fmtDur(elapsed)}` : ''}</span>
            <span className="sp" />
            {st ? <span className="t3">工具 {st.tools}{st.thinks ? ` · 思考 ${st.thinks}` : ''}</span> : null}
          </div>
          {turn.steps?.length ? <Waterfall steps={turn.steps} live={live} /> : null}
          {live ? null : <Figures turn={turn} />}
        </div>
      ) : null}
      {/* 在做的那一轮不显示模型中途的过程话（常是英文的半句），做完才显示结论 */}
      {turn.body && !live && !hideBody ? <Md body={turn.body} dim={false} /> : null}
      {live ? null : <ArtifactFold items={turn.artifacts} />}
      {/* 停住的那一轮也有底栏（输入、输出两行，T647 P2）：正文是停下时的原话不画时，复制的是原因和技术详情 */}
      {!live && turn.body && !hideBody ? <AnswerFoot turn={turn} /> : null}
      {!live && hideBody && turn.status === 'failed' ? (
        <AnswerFoot turn={turn} copy={{ text: [turn.failure?.title ?? '', turn.failure?.detail ?? turn.body].filter(Boolean).join('\n'), label: '复制停住的原因' }} />
      ) : null}
      {turn.images?.length ? <ShownImages images={turn.images} /> : null}
      {live && !cur ? (
        <div className="typing">
          <span className="dots"><i /><i /><i /></span>
          {turn.steps?.length ? '正在做' : '在做…'}
        </div>
      ) : null}
      {turn.notes?.length ? <Notes notes={turn.notes} topicKey={turn.topic_key ?? topicKey} jobId={turn.job_id} /> : null}
      {turn.started_topics?.length ? <StartedChips items={turn.started_topics} /> : null}
      {turn.status === 'failed' ? fail ?? <p style={{ color: 'var(--fail)', fontSize: 12 }}>这一轮停住了</p> : null}
      {turn.status === 'stopped' ? <p className="t3" style={{ fontSize: 12 }}>这一轮已停止</p> : null}
    </div>
  );
};

/** 一轮：你说的几句（气泡）和回答 */
export const ChatRound: React.FC<{
  owner: TurnView[];
  agent: TurnView | null;
  hl: number | null;
  who: string;
  avatar?: React.ReactNode;
  fail?: React.ReactNode;
  hideBody?: boolean;
  topicKey?: string;
}> = ({ owner, agent, hl, who, avatar, fail, hideBody, topicKey }) => (
  <>
    {owner.map((o: TurnView) => (
      <OwnerBubble key={o.key} o={o} answers={agent?.answers} hl={hl === o.seq} />
    ))}
    {agent ? <AgentTurn turn={agent} hl={hl === agent.seq} who={who} avatar={avatar} fail={fail} hideBody={hideBody} topicKey={topicKey} /> : null}
  </>
);

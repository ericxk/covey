import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import { toast } from 'sonner';
import type { LocateResponse, MemoryItem, OutboxRequest, SnapshotTopic } from '@shared/api.interface';
import { fetchLocate } from '@client/src/api/workbench';
import { newOpId, useSnapshot, useTopicByNumber } from '@client/src/hooks/use-workbench';
import { toDate } from '@client/src/lib/workbench-format';
import { participantOf } from './collab';
import { topicHref } from './lib';
import {
  appliesText, carriedCount, carriedWord, fixesFor, isLayered, isLive, layers, originOf, quoteOf, replacedBy, sourceOf, summary, VALID_WORD, whoCounts,
  type Layer, type LayerKey, type Part, type Who,
} from './memoryLayers';
import { useSendAndWait } from './sendOp';
import './rail.css';
import './memory.css';

/* 三层记忆面板（包 E，docs/web.md 协作与三层记忆）：主轴是长期约定｜当前决定｜历史记录，“你说的 / 它总结的”只作来源标签和筛选。
   专项右栏（专项记忆）和话题页右栏（话题记忆）共用；旧数据没有 layer 时专项右栏照旧画（ProjectRail 的 MemBlock），不进这里。 */

/** 专项记忆带 project_key，话题记忆带 topic_key */
export type MemoryScope = { project_key: string } | { topic_key: string };
const PART_FIRST = 6;

/** docs/ui.md 第 7 节：今天写 23:10，别的日子写 11-17 23:10 */
function when(v: number | string | null | undefined): string {
  const d: Date | null = toDate(v);
  if (!d) return '';
  const part = (o: Intl.DateTimeFormatOptions): string => d.toLocaleString('zh-CN', { ...o, timeZone: 'Asia/Shanghai' });
  const day = (x: Date): string => x.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' });
  const hm: string = part({ hour12: false, hour: '2-digit', minute: '2-digit' });
  if (day(d) === day(new Date())) return hm;
  const [mo, da] = [part({ month: 'numeric' }), part({ day: 'numeric' })].map((s: string) => s.replace(/\D/gu, '').padStart(2, '0'));
  return `${mo}-${da} ${hm}`;
}

/** “查看原文”：出处写了话题（和第几轮）就直接去；题和消息先问网页库它在哪一轮 */
function useOpenOrigin(): (m: MemoryItem) => Promise<void> {
  const nav = useNavigate();
  const { data: snap } = useSnapshot();
  return async (m: MemoryItem): Promise<void> => {
    const o = originOf(m);
    if (!o) return;
    let at: Pick<LocateResponse, 'topic_key' | 'kind' | 'project_key' | 'seq' | 'turn_key'> | null = null;
    if (o.question || (o.message_id && !o.seq)) {
      try {
        at = await fetchLocate(o.question ? { question: o.question } : { message_id: o.message_id });
      } catch {
        at = null;
      }
    }
    if (!at && o.topic) {
      const who = participantOf(o.topic, snap, snap?.butler_key ?? null);
      at = { topic_key: o.topic, kind: who.role, project_key: who.project || null, seq: o.seq ?? null, turn_key: null };
    }
    if (!at) {
      toast.message('网页上找不到原文（可能太早，或只在飞书里）');
      return;
    }
    const key: string = encodeURIComponent(at.topic_key);
    if (at.kind === 'topic') nav(`/topics/${key}${at.seq ? `?seq=${at.seq}` : ''}`);
    else if (at.kind === 'butler' || !at.project_key) nav(`/butler${at.turn_key ? `?turn=${encodeURIComponent(at.turn_key)}` : ''}`);
    else nav(`/projects/${encodeURIComponent(at.project_key)}${at.turn_key ? `?turn=${encodeURIComponent(at.turn_key)}` : ''}`);
  };
}

const Row: React.FC<{
  m: MemoryItem; all: MemoryItem[]; scope: MemoryScope; open: boolean; onToggle: () => void; onJump: (id: string) => void;
}> = ({ m, all, scope, open, onToggle, onJump }) => {
  const byNum = useTopicByNumber();
  const openOrigin = useOpenOrigin();
  const [editing, setEditing] = useState<boolean>(false);
  const [applies, setApplies] = useState<string>(() => appliesText(m.applies));
  const [busy, setBusy] = useState<boolean>(false);
  const sendWait = useSendAndWait();
  const src = sourceOf(m);
  const cond: string = appliesText(m.applies);
  const live: boolean = isLive(m);
  const can = fixesFor(m, 'project_key' in scope ? 'project' : 'topic');
  const next: MemoryItem | undefined = replacedBy(m, all);
  const n: number | undefined = m.origin?.topic_number;
  const from: SnapshotTopic | undefined = n ? byNum.get(n) : undefined;
  const fix = async (what: 'scope' | 'once' | 'confirm', sent: string): Promise<void> => {
    if (!m.id) return;
    const where = 'project_key' in scope ? { project_key: scope.project_key } : { topic_key: scope.topic_key };
    const body: OutboxRequest = {
      op_id: newOpId(), kind: 'memory_fix', ...where,
      payload: { ...where, entry_id: m.id, what, ...(what === 'scope' ? { applies: applies.trim() } : {}) },
    };
    setBusy(true);
    const r = await sendWait(body, sent);
    setBusy(false);
    if (r === 'done') setEditing(false);
  };
  const forget = async (): Promise<void> => {
    if (!m.id || !('project_key' in scope)) return;
    setBusy(true);
    await sendWait({ op_id: newOpId(), kind: 'forget', project_key: scope.project_key, payload: { project_key: scope.project_key, entry_id: m.id } }, '已发出：作废这条记忆');
    setBusy(false);
  };
  return (
    <div className={`a-mem${open ? ' open' : ''}${live ? '' : ' gone'}`} data-mem={m.id}>
      <button type="button" className="a-mrow" onClick={onToggle} aria-expanded={open}>
        <span className={`tag ${src.you ? 'you' : 'ai'}`}>{src.tag}</span>
        <span className="a-msum">{summary(m.text)}</span>
        {live ? null : <span className="a-mvalid">{VALID_WORD[m.valid ?? ''] ?? m.valid}</span>}
        <span className="a-mat">{when(m.at)}</span>
      </button>
      {cond && !open ? <div className="a-mapp" title="适用范围和条件">适用：{cond}</div> : null}
      {open ? (
        <div className="a-mdet">
          <div className="a-mfull">{m.text}</div>
          {quoteOf(m) ? <div className="a-mapp full a-mquote"><b>原话</b>{quoteOf(m)}</div> : null}
          {cond ? <div className="a-mapp full"><b>适用</b>{cond}</div> : null}
          {next ? (
            <div className="a-mapp full">
              <b>被替代</b>
              <button type="button" className="a-mlink" onClick={() => onJump(next.id ?? '')}>{summary(next.text).slice(0, 40)}</button>
            </div>
          ) : null}
          <div className="a-mmeta">
            <span>{src.detail}</span>
            {n ? (from ? <Link to={topicHref(from)} className="mono short">来自 T{n}</Link> : <span className="mono short">来自 T{n}</span>) : null}
            {m.at ? <span>{when(m.at)} 记下</span> : null}
            <span>{VALID_WORD[m.valid ?? 'active'] ?? m.valid}</span>
            {carriedWord(m) ? <span>{carriedWord(m)}</span> : null}
          </div>
          {editing ? (
            <div className="a-mscope">
              <textarea
                value={applies}
                rows={2}
                maxLength={500}
                placeholder="写清什么时候适用、范围和例外，例如：只在习题专项对跑新策略时"
                aria-label="适用范围和条件"
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setApplies(e.target.value)}
              />
              <span className="a-mfix">
                <button type="button" className="on" disabled={busy || !applies.trim()} onClick={() => void fix('scope', '已发出：改适用条件')}>保存条件</button>
                <button type="button" onClick={() => setEditing(false)}>取消</button>
              </span>
            </div>
          ) : (
            <div className="a-mfix" role="group" aria-label="纠正这条">
              {originOf(m) ? <button type="button" onClick={() => void openOrigin(m)} title="跳到记下它的那条消息或那道题">查看原文</button> : null}
              {can.scope ? <button type="button" disabled={busy} onClick={() => setEditing(true)} title="改它的适用范围或条件">改条件</button> : null}
              {can.once ? <button type="button" disabled={busy} onClick={() => void fix('once', '已发出：只限当次')} title="只算那一次，以后不照着做">只限当次</button> : null}
              {can.confirm ? <button type="button" disabled={busy} onClick={() => void fix('confirm', '已发出：确认长期')} title="你确认：以后一直照着做">确认长期</button> : null}
              {/* 撤销一条记忆网页上一律叫「不对」（同飞书答案卡；专项右栏也是它），点了作废这条；红色只给停住了，这里不用红 */}
              {can.forget ? <button type="button" disabled={busy} onClick={() => void forget()} title="这条记错了：作废它，以后不再照着做">不对</button> : null}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
};

const PartList: React.FC<{
  p: Part; all: MemoryItem[]; scope: MemoryScope; open: string | null; setOpen: (id: string | null) => void; onJump: (id: string) => void; titled: boolean;
}> = ({ p, all, scope, open, setOpen, onJump, titled }) => {
  const [more, setMore] = useState<boolean>(false);
  const [gone, setGone] = useState<boolean>(false);
  const shown: MemoryItem[] = more ? p.live : p.live.slice(0, PART_FIRST);
  const keyOf = (m: MemoryItem, i: number): string => m.id ?? `${p.key}-${i}`;
  const row = (m: MemoryItem, i: number): React.ReactNode => (
    <Row key={keyOf(m, i)} m={m} all={all} scope={scope} open={open === keyOf(m, i)} onToggle={() => setOpen(open === keyOf(m, i) ? null : keyOf(m, i))} onJump={onJump} />
  );
  return (
    <div className={`a-mpart ${p.key}`}>
      {titled ? <div className="a-mptitle"><b>{p.title}</b><span className="n">{p.live.length}</span><span className="a-rhint">{p.hint}</span></div> : null}
      {p.live.length ? shown.map(row) : <p className="t3 a-rempty">{titled ? '没有。' : '这一层还没有记忆。'}</p>}
      {p.live.length > PART_FIRST ? (
        <button type="button" className="a-rfold" onClick={() => setMore(!more)}>{more ? '收起' : <>全部 {p.live.length} 条<ChevronRight /></>}</button>
      ) : null}
      {p.gone.length ? (
        <>
          <button type="button" className="a-rfold a-mgone" onClick={() => setGone(!gone)} aria-expanded={gone}>
            {gone ? '收起已失效的' : <>已失效 {p.gone.length} 条（做完、被替代、作废）<ChevronRight /></>}
          </button>
          {gone ? p.gone.map((m: MemoryItem, i: number) => row(m, p.live.length + i)) : null}
        </>
      ) : null}
    </div>
  );
};

/** 分了层的记忆：三个标签（带有效条数），长期约定里分已确认的规则和待核对参考；上面按谁说的筛 */
export const MemoryPanel: React.FC<{ items: MemoryItem[]; scope: MemoryScope; title?: string; version?: string }> = ({ items, scope, title = '记忆', version }) => {
  const [who, setWho] = useState<Who>('all');
  const all: Layer[] = layers(items, who);
  const firstFull: LayerKey = all.find((l: Layer) => l.count > 0)?.key ?? 'rule';
  const [tab, setTab] = useState<LayerKey | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const counts = whoCounts(items);
  const cur: Layer = all.find((l: Layer) => l.key === (tab ?? firstFull)) ?? all[0];
  const live: number = items.filter(isLive).length;
  const carried: number | null = carriedCount(items);
  // 被替代的那条点过去：换到它那一层、打开它
  const jump = (id: string): void => {
    const target: Layer | undefined = layers(items, 'all').find((l: Layer) => l.parts.some((p: Part) => [...p.live, ...p.gone].some((m: MemoryItem) => m.id === id)));
    if (!target) return;
    setWho('all');
    setTab(target.key);
    setOpen(id);
  };
  return (
    <div className="rps a-rblk a-mlay" data-testid="memory-layers">
      <div className="sh">
        <b>{title}</b><span className="n">{live}</span><span className="sp" />
        <span className="a-rhint">{carried !== null ? `话题下一轮带 ${carried} 条` : '按现在怎么用分三层'}</span>
        {version ? <span className="a-mver" title="专项记忆的版本：负责人和话题这一轮读到的是同一版">版本 {version}</span> : null}
      </div>
      {counts.you && counts.ai ? (
        <div className="a-mwho" role="group" aria-label="按谁说的筛">
          {([['all', `全部 ${counts.all}`], ['you', `你说的 ${counts.you}`], ['ai', `它总结的 ${counts.ai}`]] as [Who, string][]).map(([k, label]) => (
            <button key={k} type="button" className={who === k ? 'on' : ''} aria-pressed={who === k} onClick={() => setWho(k)}>{label}</button>
          ))}
        </div>
      ) : null}
      <div className="a-mtabs" role="tablist" aria-label="记忆的三层">
        {all.map((l: Layer) => (
          <button key={l.key} type="button" role="tab" aria-selected={l.key === cur.key} className={l.key === cur.key ? 'on' : ''} onClick={() => { setTab(l.key); setOpen(null); }}>
            {l.title}<span className="n">{l.count}</span>
          </button>
        ))}
      </div>
      {cur.parts.some((p: Part) => p.live.length || p.gone.length) ? (
        cur.parts.filter((p: Part) => p.live.length || p.gone.length).map((p: Part) => (
          <PartList key={p.key} p={p} all={items} scope={scope} open={open} setOpen={setOpen} onJump={jump} titled={cur.parts.length > 1} />
        ))
      ) : (
        <p className="t3 a-rempty">{who === 'all' ? `${cur.title}还没有记忆。` : '这样筛下来，这一层没有。'}</p>
      )}
    </div>
  );
};

/** 没分层的记忆（话题记忆的旧形状）：一行一条，来源只作标签 */
const FlatMemory: React.FC<{ items: MemoryItem[]; scope: MemoryScope }> = ({ items, scope }) => {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="rps a-rblk">
      <div className="sh"><b>记忆</b><span className="n">{items.length}</span></div>
      {items.map((m: MemoryItem, i: number) => {
        const k: string = m.id ?? `m${i}`;
        return <Row key={k} m={m} all={items} scope={scope} open={open === k} onToggle={() => setOpen(open === k ? null : k)} onJump={setOpen} />;
      })}
    </div>
  );
};

/** 话题记忆（v31，话题条目的 memory）：分了层的画三层，没分层的一行一条；没有就不画 */
export const TopicMemory: React.FC<{ topicKey: string; items: MemoryItem[] | undefined }> = ({ topicKey, items }) => {
  if (!items?.length) return null;
  return isLayered(items)
    ? <MemoryPanel items={items} scope={{ topic_key: topicKey }} title="这个话题的记忆" />
    : <FlatMemory items={items} scope={{ topic_key: topicKey }} />;
};

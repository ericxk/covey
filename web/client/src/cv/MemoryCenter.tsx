import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Check, ChevronRight, Plus, Search, X } from 'lucide-react';
import type {
  MemoryEditPayloadV43,
  MemoryEntry,
  MemoryGlobalData,
  MemoryIndex,
  MemoryKind,
  MemoryLogItem,
  MemoryProjectData,
  MemoryReviewItem,
  MemoryTopicsData,
  SnapshotProject,
} from '@shared/api.interface';
import { newOpId, useMemoryIndex, useMemoryKeys, useMemoryMode, useSnapshot } from '@client/src/hooks/use-workbench';
import { Head } from './Shell';
import { MainMemory } from './MainMemory';
import { sendOp, type OpResult } from './sendOp';
import { opWords } from './opWords';
import {
  KIND_WORD,
  acceptOp, addOp, buildGroups, charCount, decisionHit, defaultOpen, draftProblem, editOp, groupLine, keepOp, keysToLoad, limitsOf, locate, logLine, mmdd,
  moveOp, overLine, parseLink, poolOf, projectKeyOf, rejectOp, restoreOp, reviewLine, reviewSplit, rowTags, searchMemory, titleShown, todoPending, undoOp,
  updatedOf, voidOp,
  type Located, type MemGroup, type MemSection, type Place, type Pool, type ReviewGroup, type SearchHit,
} from './memoryCenter';
import './memorycenter.css';

/* 记忆页（数据约定 v45，路由 /memory，一页式）。侧栏「记忆」只轮询小键 memory:index（角标是待审核数）；这一页按 index.revs
   取 memory:global、memory:topics、有记忆的专项 memory:p:<专项>。第一屏：标题、搜索框、「＋ 记一条」、待审核块（有才画）、分组列表
   （全局、各专项、话题；决定不在分组里，只在搜索里以“上次你答过”出现）；页底「记录」。旧路径 /memory/<页签> 照样画这一页。
   没收到 memory:index（旧 Covey 只推 v37 的 memory）时整页退回原来的主记忆页（MainMemory）。 */

/* ---------- 发一条 memory_edit ---------- */
interface Receipt {
  phase: 'sending' | 'done' | 'failed';
  message: string;
}
function useMemoryOp(): (payload: MemoryEditPayloadV43) => Promise<OpResult> {
  const qc = useQueryClient();
  return useCallback(async (payload: MemoryEditPayloadV43): Promise<OpResult> => {
    // 回执后跟踪器重取 ['memory', …] 下所有开着的查询（index 和各数据键）；等取回来再返回，页面上已经是新的
    const r: OpResult = await sendOp({ op_id: newOpId(), kind: 'memory_edit', payload: payload as unknown as Record<string, unknown> }, qc);
    if (r.settled) await r.settled;
    return r;
  }, [qc]);
}
const failWords = (r: OpResult): string => (r.phase === 'done' ? '' : opWords(r.phase === 'timeout' ? 'timeout' : r.phase, r.message));

/** 页面里共用的：池子、发操作、回执 */
interface Ctx {
  pool: Pool;
  index: MemoryIndex;
  receipts: Record<string, Receipt>;
  run: (id: string, payload: MemoryEditPayloadV43, notice?: string) => Promise<boolean>;
  focus: string;
  editing: string;
  setEditing: (key: string) => void;
}

/* ---------- 零件 ---------- */
const Counter: React.FC<{ n: number; max: number }> = ({ n, max }) => <span className={`mc-count${n > max ? ' over' : ''}`}>{n}/{max}</span>;

const ReceiptLine: React.FC<{ r?: Receipt }> = ({ r }) => {
  if (!r) return null;
  if (r.phase === 'sending') return <div className="mc-rc">发送中…</div>;
  return <div className={`mc-rc${r.phase === 'failed' ? ' bad' : ''}`} role={r.phase === 'failed' ? 'alert' : 'status'}>{r.message}</div>;
};

/** 标题、正文、原因的编辑框；记一条时多两个下拉（种类：规矩、资料；管哪儿）。⌘/Ctrl+Enter 存，Esc 取消 */
const Editor: React.FC<{
  index: MemoryIndex;
  init: { title: string; text: string; why?: string; kind: MemoryKind };
  places?: { value: string; label: string }[];
  place?: string;
  saveWord: string;
  busy: boolean;
  onSave: (d: { title: string; text: string; why: string; kind: MemoryKind; place: string }) => void;
  onCancel: () => void;
}> = ({ index, init, places, place, saveWord, busy, onSave, onCancel }) => {
  const [title, setTitle] = useState<string>(init.title);
  const [text, setText] = useState<string>(init.text);
  const [why, setWhy] = useState<string>(init.why ?? '');
  const [kind, setKind] = useState<MemoryKind>(init.kind);
  const [to, setTo] = useState<string>(place ?? 'global');
  const [whyOpen, setWhyOpen] = useState<boolean>(!!init.why);
  const lim = limitsOf(index, kind);
  const problem: string = draftProblem({ title, text, why }, lim);
  const save = (): void => {
    if (!problem && !busy) onSave({ title, text, why, kind, place: to });
  };
  const keys = (e: React.KeyboardEvent): void => {
    if (e.key === 'Escape') onCancel();
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save();
  };
  return (
    <div className="mc-ed" onKeyDown={keys}>
      {places ? (
        <div className="mc-ed-row">
          <select className="mc-sel" value={kind} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setKind(e.target.value as MemoryKind)} aria-label="记成">
            {(['rule', 'note'] as MemoryKind[]).map((k: MemoryKind) => <option key={k} value={k}>{KIND_WORD[k]}</option>)}
          </select>
          <select className="mc-sel" value={to} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setTo(e.target.value)} aria-label="管哪儿">
            {places.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </div>
      ) : null}
      <label className="mc-field">
        <input value={title} autoFocus maxLength={lim.title + 20} placeholder="标题（一句话说清是什么）" aria-label="标题" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTitle(e.target.value)} />
        <Counter n={charCount(title.trim())} max={lim.title} />
      </label>
      <label className="mc-field area">
        <textarea value={text} rows={3} placeholder="正文" aria-label="正文" onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setText(e.target.value)} />
        <Counter n={charCount(text.trim())} max={lim.text} />
      </label>
      {whyOpen ? (
        <label className="mc-field">
          <input value={why} placeholder="为什么（可不写）" aria-label="原因" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setWhy(e.target.value)} />
          <Counter n={charCount(why.trim())} max={lim.why} />
        </label>
      ) : null}
      <div className="mc-ed-foot">
        {whyOpen ? null : <button type="button" className="mc-link" onClick={() => setWhyOpen(true)}>加原因</button>}
        <span className="mc-why">{problem && (title || text) ? problem : ''}</span>
        <button type="button" className="mc-btn" onClick={onCancel} disabled={busy}>取消</button>
        <button type="button" className="mc-btn pri" onClick={save} disabled={!!problem || busy} data-testid="memory-save">{busy ? '发送中…' : saveWord}</button>
      </div>
    </div>
  );
};

/** 管哪儿的选项：全局、各专项、近 30 天有记忆的话题 */
function placeOptions(pool: Pool, index: MemoryIndex): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [{ value: 'global', label: '全局' }];
  for (const s of index.scopes) if (s.key.startsWith('p:')) out.push({ value: s.key, label: `专项「${s.title || pool.projects[s.key.slice(2)] || s.key}」` });
  const topics = Object.entries(pool.topics).sort((a, b) => (b[1].number ?? 0) - (a[1].number ?? 0));
  for (const [key, t] of topics) out.push({ value: `t:${key}`, label: `话题 ${t.number ? `T${t.number} ` : ''}${Array.from(t.title).slice(0, 18).join('')}` });
  return out;
}
const placeFromValue = (v: string): Place => (v.startsWith('p:') ? { scope: 'project', project_key: v.slice(2) } : v.startsWith('t:') ? { scope: 'topic', topic_key: v.slice(2) } : { scope: 'global' });
const placeValue = (e: Pick<MemoryEntry, 'scope' | 'project_key' | 'topic_key'>): string =>
  (e.scope === 'project' ? `p:${e.project_key ?? ''}` : e.scope === 'topic' ? `t:${e.topic_key ?? ''}` : 'global');

/* ---------- 一条 ---------- */
const EntryRow: React.FC<{ e: MemoryEntry; ctx: Ctx; tags?: string[]; meta?: string; shelf?: boolean }> = ({ e, ctx, tags = [], meta, shelf }) => {
  const [open, setOpen] = useState<boolean>(ctx.focus === e.code);
  const [moving, setMoving] = useState<boolean>(false);
  const decision: boolean = e.kind === 'decision';
  const editing: boolean = ctx.editing === e.key;
  const r: Receipt | undefined = ctx.receipts[e.key];
  const busy: boolean = r?.phase === 'sending';
  useEffect(() => {
    if (ctx.focus === e.code) {
      setOpen(true);
      window.requestAnimationFrame(() => document.getElementById(`mc-${e.code}`)?.scrollIntoView({ block: 'center' }));
    }
  }, [ctx.focus, e.code]);
  const quote: string = e.origin?.quote ?? '';
  const showQuote: boolean = !!quote && !e.text.includes(quote.replace(/[。，,.\s]+$/u, ''));
  const from: string = [e.origin?.topic_number ?? e.topic_number ? `T${e.origin?.topic_number ?? e.topic_number}` : '', e.origin?.at ? mmdd(e.origin.at) : ''].filter(Boolean).join(' · ');
  const options = useMemo(() => placeOptions(ctx.pool, ctx.index), [ctx.pool, ctx.index]);
  return (
    <li className={`mc-row${ctx.focus === e.code ? ' hit' : ''}${shelf ? ' shelf' : ''}`} id={`mc-${e.code}`}>
      <button type="button" className="mc-main" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="mc-t1">
          <span className="mc-title">{decision ? meta : e.title}</span>
          {tags.map((t: string) => <span key={t} className="mc-off short">{t}</span>)}
        </span>
        {meta && !decision ? <span className="mc-meta">{meta}</span> : null}
      </button>
      {open && !editing ? (
        <div className="mc-body">
          {decision ? <p className="mc-sub">{e.title}</p> : null}
          <p className="mc-text">{e.text}</p>
          {e.why ? <p className="mc-sub">为什么：{e.why}</p> : null}
          {showQuote ? <p className="mc-sub">你的原话：“{quote}”</p> : null}
          {from || e.origin?.topic_key ? (
            <p className="mc-sub">
              出处：{e.origin?.topic_key ? <Link to={`/topics/${encodeURIComponent(e.origin.topic_key)}`}>{from || '话题'}</Link> : from}
              <span className="mono"> · {e.code}</span>
            </p>
          ) : <p className="mc-sub mono">{e.code}</p>}
          <div className="mc-acts">
            {shelf ? <button type="button" className="mc-act" disabled={busy} onClick={() => void ctx.run(e.key, restoreOp(e), '已放回')}>放回</button> : null}
            {!shelf && !decision ? <button type="button" className="mc-act" disabled={busy} onClick={() => ctx.setEditing(e.key)}>改</button> : null}
            {!shelf && !decision ? <button type="button" className="mc-act" disabled={busy} onClick={() => setMoving(!moving)}>换管哪儿</button> : null}
            {!shelf ? <button type="button" className="mc-act danger" disabled={busy} onClick={() => void ctx.run(e.key, voidOp(e), decision ? '已作废这条决定' : '已删')}>{decision ? '作废' : '删'}</button> : null}
          </div>
        </div>
      ) : null}
      {moving ? (
        <div className="mc-ed-row mc-move">
          <span className="mc-sub">挪到</span>
          <select
            className="mc-sel"
            defaultValue={placeValue(e)}
            aria-label="管哪儿"
            onChange={(ev: React.ChangeEvent<HTMLSelectElement>) => {
              setMoving(false);
              if (ev.target.value !== placeValue(e)) void ctx.run(e.key, moveOp(e, placeFromValue(ev.target.value)), '已换管哪儿');
            }}
          >
            {options.some((p) => p.value === placeValue(e)) ? null : <option value={placeValue(e)}>{e.scope === 'topic' ? '这个话题' : '现在这里'}</option>}
            {options.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
          <button type="button" className="mc-link" onClick={() => setMoving(false)}>取消</button>
        </div>
      ) : null}
      {editing ? (
        <Editor
          index={ctx.index}
          init={{ title: e.title, text: e.text, why: e.why, kind: e.kind }}
          saveWord="保存"
          busy={busy}
          onCancel={() => ctx.setEditing('')}
          onSave={(d) => {
            void ctx.run(e.key, editOp(e, d), '已改').then((ok: boolean) => {
              if (ok) ctx.setEditing('');
            });
          }}
        />
      ) : null}
      <ReceiptLine r={r} />
    </li>
  );
};

/* ---------- 一组 ---------- */
const SectionBox: React.FC<{ s: MemSection; ctx: Ctx; open: boolean; onToggle: () => void }> = ({ s, ctx, open, onToggle }) => (
  <div className="mc-sec" data-testid="memory-section">
    <button type="button" className="mc-link mc-sec-h" aria-expanded={open} onClick={onToggle}>
      <ChevronRight aria-hidden="true" className={open ? 'rot' : ''} />{s.title}
    </button>
    {open ? (
      <ul className="mc-list">
        {s.rows.map((e: MemoryEntry) => <EntryRow key={e.key} e={e} ctx={ctx} shelf={s.shelf} tags={rowTags(e, { said: s.said, pool: ctx.pool })} />)}
      </ul>
    ) : null}
  </div>
);

const GroupBox: React.FC<{
  g: MemGroup;
  ctx: Ctx;
  open: boolean;
  onToggle: () => void;
  secOpen: (sec: string) => boolean;
  onSec: (sec: string) => void;
}> = ({ g, ctx, open, onToggle, secOpen, onSec }) => {
  const over: string = overLine(g.over);
  return (
    <section className="mc-g" id={`mcg-${g.key}`} data-testid="memory-group">
      <button type="button" className="mc-gh" aria-expanded={open} onClick={onToggle}>
        <ChevronRight aria-hidden="true" className={open ? 'rot' : ''} />
        <span className="mc-gt">{g.title}</span>
        {over ? <span className="mc-over">{over}</span> : null}
      </button>
      {open ? (
        <div className="mc-gb">
          {g.rules.length ? <ul className="mc-list">{g.rules.map((e: MemoryEntry) => <EntryRow key={e.key} e={e} ctx={ctx} tags={rowTags(e, { said: true, pool: ctx.pool })} />)}</ul> : null}
          {g.sections.map((s: MemSection) => <SectionBox key={s.key} s={s} ctx={ctx} open={secOpen(s.key)} onToggle={() => onSec(s.key)} />)}
        </div>
      ) : null}
    </section>
  );
};

/* ---------- 待审核 ---------- */
const ReviewCard: React.FC<{ r: MemoryReviewItem; ctx: Ctx }> = ({ r, ctx }) => {
  const rc: Receipt | undefined = ctx.receipts[r.key];
  const busy: boolean = rc?.phase === 'sending';
  const left: MemoryEntry | undefined = r.conflict_with ? ctx.pool.entries.find((e: MemoryEntry) => e.key === r.conflict_with) : undefined;
  const line: string = reviewLine(r, ctx.pool);
  const from: string = r.from?.topic_number ? `T${r.from.topic_number}` : '';
  const source = from ? (
    <span className="mc-sub">出处：{r.from?.topic_key ? <Link to={`/topics/${encodeURIComponent(r.from.topic_key)}`}>{from}</Link> : from}</span>
  ) : null;
  if (r.type === 'conflict') {
    return (
      <li className="mc-rv">
        <div className="mc-t1"><span className="mc-title wrap">{r.title}</span></div>
        <div className="mc-meta wrap">{line}</div>
        <div className="mc-pair">
          <div><span className="mc-sub">已有的</span><b>{left?.title ?? r.conflict_with}</b><p>{left?.text ?? ''}</p></div>
          <div><span className="mc-sub">新来的</span><b>{r.title}</b><p>{r.text}</p></div>
        </div>
        {source}
        <div className="mc-rv-acts">
          <button type="button" className="mc-btn" disabled={busy} onClick={() => void ctx.run(r.key, keepOp('keep_left', r))}>留已有的</button>
          <button type="button" className="mc-btn" disabled={busy} onClick={() => void ctx.run(r.key, keepOp('keep_right', r))}>换成新的</button>
          <button type="button" className="mc-btn" disabled={busy} onClick={() => void ctx.run(r.key, keepOp('keep_both', r))}>都留</button>
        </div>
        <ReceiptLine r={rc} />
      </li>
    );
  }
  return (
    <li className="mc-rv">
      <div className="mc-t1"><span className="mc-title wrap">{titleShown(r) ? r.title : r.text}</span></div>
      <div className="mc-meta wrap">{line}</div>
      {titleShown(r) && r.text ? <p className="mc-text">{r.text}</p> : null}
      {r.quote ? <p className="mc-sub">你的原话：“{r.quote}”</p> : null}
      {r.items?.length ? (
        <ul className="mc-tidy">{r.items.map((e: MemoryEntry) => <li key={e.key}><span className="mono">{e.code}</span>{e.title}</li>)}</ul>
      ) : null}
      {source}
      <div className="mc-rv-acts">
        <button type="button" className="mc-btn pri" disabled={busy} onClick={() => void ctx.run(r.key, acceptOp(r), '已采纳')}>采纳</button>
        <button type="button" className="mc-btn" disabled={busy} onClick={() => void ctx.run(r.key, rejectOp(r), '已不要')}>不要</button>
      </div>
      <ReceiptLine r={rc} />
    </li>
  );
};

/** 迁移项的一个专项组：组头一行，点开逐条处理；组里不放一键批量（90 条负责人约束，闭着眼点不得） */
const MigrateGroup: React.FC<{ g: ReviewGroup; ctx: Ctx }> = ({ g, ctx }) => {
  const [open, setOpen] = useState<boolean>(false);
  return (
    <section className="mc-grp" data-testid="memory-migrate-group">
      <button type="button" className="mc-grp-h" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronRight aria-hidden="true" className={open ? 'open' : ''} />
        <span>{groupLine(g)}</span>
        <span className="mc-sub">{open ? '收起' : '逐条看'}</span>
      </button>
      {open ? <ul className="mc-list">{g.rows.map((r: MemoryReviewItem) => <ReviewCard key={r.key} r={r} ctx={ctx} />)}</ul> : null}
    </section>
  );
};

const ReviewBlock: React.FC<{ rows: MemoryReviewItem[]; ctx: Ctx }> = ({ rows: all, ctx }) => {
  const { todo, groups } = useMemo(() => reviewSplit(all, ctx.pool), [all, ctx.pool]);
  if (!todo.length && !groups.length) return null;
  return (
    <section className="mc-review" id="mc-review" data-testid="memory-review">
      {todo.length ? <h3 className="mc-h">待审核 · {todo.length}</h3> : null}
      {todo.length ? <ul className="mc-list">{todo.map((r: MemoryReviewItem) => <ReviewCard key={r.key} r={r} ctx={ctx} />)}</ul> : null}
      {groups.length ? <div className="mc-grps" aria-label="迁移时带过来的">{groups.map((g: ReviewGroup) => <MigrateGroup key={g.key} g={g} ctx={ctx} />)}</div> : null}
    </section>
  );
};

/* ---------- 记录 ---------- */
const at = (sec: number): string =>
  new Date(sec * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).replace(/\//gu, '-');
const LogView: React.FC<{ rows: MemoryLogItem[]; ctx: Ctx }> = ({ rows, ctx }) => {
  if (!rows.length) return <p className="mc-empty">还没有变更记录。</p>;
  return (
    <ul className="mc-list">
      {[...rows].sort((a: MemoryLogItem, b: MemoryLogItem) => b.at - a.at).map((l: MemoryLogItem, i: number) => {
        const w = logLine(l);
        const id = `undo:${l.undo_token ?? i}`;
        const rc: Receipt | undefined = ctx.receipts[id];
        return (
          <li key={`${l.at}:${i}`} className="mc-row">
            <div className="mc-line">
              <div className="mc-main static">
                <span className="mc-t1"><span className="mc-title wrap">{w.who}{w.action}{w.title ? `「${w.title}」` : ''}</span></span>
                <span className="mc-meta wrap">{[at(l.at), w.because].filter(Boolean).join(' · ')}</span>
              </div>
              {l.undo_token ? (
                <span className="mc-acts">
                  <button type="button" className="mc-act" disabled={rc?.phase === 'sending'} onClick={() => void ctx.run(id, undoOp(l.undo_token ?? ''))}>撤销</button>
                </span>
              ) : null}
            </div>
            <ReceiptLine r={rc} />
          </li>
        );
      })}
    </ul>
  );
};

/* ---------- 整页 ---------- */
export const MemoryCenter: React.FC<{ index: MemoryIndex }> = ({ index }) => {
  const params = useParams<{ tab?: string }>();
  const [sp, setSp] = useSearchParams();
  const send = useMemoryOp();
  const snap = useSnapshot();
  const keys = useMemo(() => keysToLoad(index), [index]);
  const got = useMemoryKeys(keys);
  const pool: Pool = useMemo(() => poolOf(
    got.data['memory:global'] as MemoryGlobalData | undefined,
    got.data['memory:topics'] as MemoryTopicsData | undefined,
    keys.filter((k) => projectKeyOf(k.key)).map((k) => got.data[k.key] as MemoryProjectData | undefined),
  ), [got.data, keys]);
  const link = parseLink(params.tab, sp);
  const [query, setQuery] = useState<string>('');
  const [editing, setEditing] = useState<string>('');
  const [adding, setAdding] = useState<boolean>(false);
  const [receipts, setReceipts] = useState<Record<string, Receipt>>({});
  const [notice, setNotice] = useState<{ id: number; text: string; undo?: string } | null>(null);
  const [openG, setOpenG] = useState<Record<string, boolean>>({});
  const [openS, setOpenS] = useState<Record<string, boolean>>({});
  const timer = useRef<number>(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const order: string[] = useMemo(() => index.scopes.filter((s) => s.key.startsWith('p:')).map((s) => s.key.slice(2)), [index.scopes]);
  const updated = useMemo(() => updatedOf(snap.data?.projects as Pick<SnapshotProject, 'key' | 'updated_at'>[] | undefined), [snap.data]);
  const groups: MemGroup[] = useMemo(() => buildGroups(pool, order, updated), [pool, order, updated]);
  const defaults = useMemo(() => defaultOpen(groups), [groups]);
  const hit: Located | null = useMemo(() => (link.code ? locate(pool, groups, link.code) : null), [pool, groups, link.code]);
  const isOpenG = (g: MemGroup): boolean => openG[g.key] ?? (link.scope === g.key || hit?.group === g.key || defaults.has(g.key));
  const isOpenS = (g: MemGroup, s: string): boolean => openS[`${g.key}/${s}`] ?? (hit?.group === g.key && hit.section === s);

  // 链接进来：?scope= 滚到那一组，/memory/review 滚到待审核块（数据到了才滚；每个链接只滚一次）
  const scrolled = useRef<string>('');
  useEffect(() => {
    const id: string = link.review ? 'mc-review' : link.scope ? `mcg-${link.scope}` : '';
    if (!id || scrolled.current === id) return;
    const el: HTMLElement | null = document.getElementById(id);
    if (!el) return;
    scrolled.current = id;
    window.requestAnimationFrame(() => el.scrollIntoView({ block: 'start' }));
  }, [link.review, link.scope, groups, pool.review]);

  const setView = (view: '' | 'log'): void => {
    const q = new URLSearchParams(sp);
    q.delete('code');
    if (view) q.set('view', view);
    else q.delete('view');
    setSp(q);
  };
  const clearCode = (): void => {
    const q = new URLSearchParams(sp);
    q.delete('code');
    setSp(q, { replace: true });
  };

  const run = useCallback(async (id: string, payload: MemoryEditPayloadV43, done?: string): Promise<boolean> => {
    setReceipts((r) => ({ ...r, [id]: { phase: 'sending', message: '' } }));
    const res: OpResult = await send(payload);
    const ok: boolean = res.phase === 'done';
    const message: string = ok ? res.message || done || '已办好' : failWords(res);
    setReceipts((r) => ({ ...r, [id]: { phase: ok ? 'done' : 'failed', message } }));
    // 撤销条已经关了，回执只在别处画：撤销失败（“这条后来又改过了”）用页面提示告诉用户
    if (!ok && id.startsWith('undo:')) toast.error(`没撤销成：${message}`);
    if (ok) {
      window.setTimeout(() => setReceipts((r) => {
        if (r[id]?.phase !== 'done') return r;
        const next = { ...r };
        delete next[id];
        return next;
      }), 4000);
      if (done) {
        // 删、改、换管哪儿、放回这类：一行页面提示，5 秒内能撤销（撤销码从变更记录里找这一条的）
        window.clearTimeout(timer.current);
        setNotice((n) => ({ id: (n?.id ?? 0) + 1, text: message, undo: payload.keys[0] }));
        timer.current = window.setTimeout(() => setNotice(null), 5000);
      }
    }
    return ok;
  }, [send]);

  const ctx: Ctx = { pool, index, receipts, run, focus: hit ? hit.entry.code : '', editing, setEditing };
  const undoFor = (key: string | undefined): string => {
    if (!key) return '';
    const l: MemoryLogItem | undefined = [...pool.log].sort((a, b) => b.at - a.at).find((x: MemoryLogItem) => x.refs.includes(key) && !!x.undo_token);
    return l?.undo_token ?? '';
  };
  const noticeUndo: string = undoFor(notice?.undo);
  const loading: boolean = got.loading && !pool.entries.length && !pool.review.length;
  const total: number = index.counts.rule + index.counts.decision + index.counts.note;
  const q: string = query.trim();
  // 搜索结果；?code= 是决定时，当作一条展开的搜索结果
  const asDecision: SearchHit | null = !q && hit?.decision ? decisionHit(pool, link.code) : null;
  const hits: SearchHit[] = useMemo(() => (q ? searchMemory(pool, index.titles, q) : []), [pool, index.titles, q]);
  const shown: SearchHit[] = asDecision ? [asDecision] : hits;
  const searching: boolean = !!q || !!asDecision;
  const todo: number = todoPending(index.counts);
  const places = useMemo(() => placeOptions(pool, index), [pool, index]);
  const addPlace: string = link.scope && places.some((p) => p.value === link.scope) ? link.scope : 'global';

  return (
    <div className="mc-page">
      <Head title="记忆" />
      <div className="scroll">
        <div className="mc" data-testid="memory-center">
          {link.log ? (
            <>
              <div className="mc-top">
                <button type="button" className="mc-link back" onClick={() => setView('')} data-testid="memory-back">‹ 返回列表</button>
                <h3 className="mc-h grow">记录</h3>
              </div>
              <LogView rows={pool.log} ctx={ctx} />
            </>
          ) : (
            <>
              <div className="mc-top">
                <label className="mc-search">
                  <Search aria-hidden="true" />
                  <input type="search" value={query} placeholder="搜索记忆" aria-label="搜索记忆" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)} />
                </label>
                <button type="button" className="mc-btn pri" onClick={() => { setEditing(''); setAdding(true); }} data-testid="memory-new"><Plus />记一条</button>
              </div>
              {notice ? (
                <div key={notice.id} className="mc-notice" role="status">
                  <Check aria-hidden="true" /><span>{notice.text}</span>
                  {noticeUndo ? <button type="button" className="mc-link" onClick={() => { setNotice(null); void run(`undo:${noticeUndo}`, undoOp(noticeUndo)); }}>撤销</button> : <span className="mc-sub">可在记录里找回</span>}
                  <button type="button" className="mc-ic" aria-label="知道了" onClick={() => setNotice(null)}><X /></button>
                </div>
              ) : null}
              {adding ? (
                <Editor
                  index={index}
                  init={{ title: '', text: '', kind: 'rule' }}
                  places={places}
                  place={addPlace}
                  saveWord="记下"
                  busy={receipts['new']?.phase === 'sending'}
                  onCancel={() => setAdding(false)}
                  onSave={(d) => {
                    void run('new', addOp({ title: d.title, text: d.text, why: d.why, kind: d.kind, ...placeFromValue(d.place) })).then((ok: boolean) => {
                      if (ok) setAdding(false);
                    });
                  }}
                />
              ) : null}
              <ReceiptLine r={receipts['new']} />
              {loading ? <p className="mc-empty">读取中…</p> : searching ? (
                <section className="mc-results" data-testid="memory-results">
                  {asDecision ? <div className="mc-focus"><span>打开的是你答过的一条决定</span><button type="button" className="mc-link" onClick={clearCode}>回到列表</button></div> : null}
                  {shown.length ? (
                    <ul className="mc-list">
                      {shown.map((h: SearchHit) => (h.entry
                        ? <EntryRow key={h.key} e={h.entry} ctx={ctx} meta={h.line} shelf={h.entry.kind !== 'decision' && h.entry.status !== 'active'} />
                        : (
                          <li key={h.key} className="mc-row">
                            <div className="mc-main static"><span className="mc-t1"><span className="mc-title">{h.title}</span></span><span className="mc-meta">{h.line} · {h.code}</span></div>
                          </li>
                        )))}
                    </ul>
                  ) : <p className="mc-empty">没有搜到“{q}”</p>}
                </section>
              ) : (
                <>
                  <ReviewBlock rows={pool.review} ctx={ctx} />
                  {groups.map((g: MemGroup) => (
                    <GroupBox
                      key={g.key}
                      g={g}
                      ctx={ctx}
                      open={isOpenG(g)}
                      onToggle={() => setOpenG((m) => ({ ...m, [g.key]: !isOpenG(g) }))}
                      secOpen={(s: string) => isOpenS(g, s)}
                      onSec={(s: string) => setOpenS((m) => ({ ...m, [`${g.key}/${s}`]: !isOpenS(g, s) }))}
                    />
                  ))}
                  {!groups.length && !pool.review.length ? (
                    <p className="mc-empty">{total ? '还没有规矩或资料。' : '还没有记忆。'}在对话里说“以后……”“记住……”“记一下……”，或点上面的「记一条」。</p>
                  ) : null}
                  <div className="mc-foot"><button type="button" className="mc-link" onClick={() => setView('log')} data-testid="memory-log-link">记录</button>{todo ? <span className="mc-sub">待审核 {todo}</span> : null}</div>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

/** 路由 /memory（和旧的 /memory/<页签>）：收到 memory:index 是记忆页；旧 Covey（只推 v37 的 memory）退回原来的主记忆页 */
const MemoryCenterPage: React.FC = () => {
  const { mode, index } = useMemoryMode();
  const idx = useMemoryIndex();
  if (mode === 'v43' && index) return <MemoryCenter index={index} />;
  return (
    <div className="mc-page">
      <Head title="记忆" />
      <div className="scroll">
        <div className="mc">
          {mode === 'v37' ? <MainMemory /> : mode === 'loading' || idx.isLoading ? <p className="mc-empty">读取中…</p> : <p className="mc-empty">记忆没开，或者 Covey 还没推送记忆。</p>}
        </div>
      </div>
    </div>
  );
};
export default MemoryCenterPage;

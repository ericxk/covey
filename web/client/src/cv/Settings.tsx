import React, { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { Navigate, NavLink, useParams } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ExternalLink } from 'lucide-react';
import { SELFHOST } from '@client/src/selfhost/flag';
import type {
  SettingsConfigAssist,
  SettingsConfigPage,
  SettingsData,
  SettingsDeviceRow,
  SettingsError,
  SettingsGroupItem,
  SettingsGroupsPage,
  SettingsMainPage,
  SettingsNodesPage,
  SettingsProxyContact,
  SettingsProxyPage,
  SettingsRepoItem,
  SettingsReposPage,
  SettingsRow,
  SettingsTone,
  SettingsTopicPage,
  SettingsWorkspaceItem,
  SettingsWorkspacePage,
} from '@shared/api.interface';
import { newOpId, useMemoryMode, useQuota, useSettings, useUpgrade } from '@client/src/hooks/use-workbench';
import { MainMemory } from './MainMemory';
import { UpgradePanel } from './Upgrade';
import { QuotaPanel } from './Quota';
import { QUOTA_TAB, capsuleOf, quotaOn } from './quota';
import { UPGRADE_TAB, upgradeMark, upgradeOn } from './upgrade';
import { canDo } from './v34';
import { Head } from './Shell';
import { ago } from './topicText';
import { useTick } from './ui';
import { pickKind } from './launch';
import { opWords, plainReceipt } from './opWords';
import { sendOp, type OpResult } from './sendOp';
import * as RP from './reposPage';
import * as DV from './devicesPage';
import { recentLines, type RecentLine } from './assistRecent';
import './settings.css';
import './repos.css';

/* ---------- 发件：每个动作一个 id，保存中 → 回执（Covey 回的话，回执前 Covey 已推新快照）→ 立刻重拉快照，2 秒后再拉一次保险 ---------- */
/** late：等满了 Covey 还没回音（不是失败：操作还在发件箱里，Covey 回来会照做；说法同 opWords，P1-C2） */
interface Phase {
  phase: 'saving' | 'done' | 'failed' | 'late';
  message: string;
}
interface Ops {
  st: Record<string, Phase>;
  run: (id: string, payload: Record<string, unknown>) => Promise<void>;
}

function useOps(): Ops {
  const qc = useQueryClient();
  const [st, setSt] = useState<Record<string, Phase>>({});
  const alive = useRef<boolean>(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const run = useCallback(
    async (id: string, payload: Record<string, unknown>): Promise<void> => {
      const put = (p: Phase): void => {
        if (alive.current) setSt((s: Record<string, Phase>) => ({ ...s, [id]: p }));
      };
      // 失败的提示 8 秒后收起（还是这一条才收）
      const fade = (message: string): void => {
        window.setTimeout(() => {
          if (alive.current) setSt((s: Record<string, Phase>) => (s[id]?.phase === 'failed' && s[id].message === message ? { ...s, [id]: { phase: 'done', message: '' } } : s));
        }, 8000);
      };
      put({ phase: 'saving', message: '' });
      // Covey 回执前已经推了新快照：跟踪器问到回执就重取设置（opTrack.pullPlan），取回来了才算保存完（格子在这之前显示“保存中”）
      const r: OpResult = await sendOp({ op_id: newOpId(), kind: 'settings', payload }, qc);
      if (r.phase === 'unsent') {
        const message = opWords('unsent', r.message);
        put({ phase: 'failed', message });
        fade(message);
        return;
      }
      if (r.phase === 'timeout') {
        put({ phase: 'late', message: opWords('timeout') });
        return;
      }
      await r.settled;
      const message = r.phase === 'done' ? plainReceipt(r.message) : opWords('failed', r.message);
      put({ phase: r.phase, message });
      if (r.phase === 'failed') fade(message);
    },
    [qc],
  );
  return { st, run };
}

const Receipt: React.FC<{ s?: Phase; cell?: boolean }> = ({ s, cell }) => {
  if (!s) return null;
  if (cell) {
    // 配置页的格子：保存中、成功都不占位（格子自己有转圈和半透明）；失败是格子下方浮着的一条，不撑高格子
    return s.phase === 'failed' ? <div className="st-pop" role="alert">{s.message}</div> : s.phase === 'late' ? <div className="st-pop late" role="status">{s.message}</div> : null;
  }
  if (s.phase === 'saving') return <div className="st-rc">保存中…</div>;
  if (s.phase === 'failed') return <div className="st-rc bad">{s.message}</div>;
  return s.message ? <div className="st-rc">{s.message}</div> : null;
};

/* ---------- 零件 ---------- */
const TONE: Record<SettingsTone, string> = { green: 'result', orange: 'waiting', red: 'stopped', neutral: 'queued', blue: 'running' };
const Tag: React.FC<{ tone: SettingsTone; text: string }> = ({ tone, text }) => <span className={`pill tone-${TONE[tone] ?? 'queued'}`}>{text}</span>;

/** 去飞书改：有链接就在新窗口打开机器人；没有就提示去哪里改 */
const FeishuBtn: React.FC<{ feishu: string; link?: boolean }> = ({ feishu, link }) => {
  const [hint, setHint] = useState<boolean>(false);
  const cls: string = link ? 'st-link' : 'btn';
  if (feishu) {
    return (
      <a className={cls} href={feishu} target="_blank" rel="noopener noreferrer">
        去飞书改{link ? null : <ExternalLink />}
      </a>
    );
  }
  return (
    <span className="st-fx">
      <button type="button" className={cls} onClick={() => setHint(!hint)}>去飞书改</button>
      {hint ? <span className="st-hint">在飞书机器人菜单「设置」里改</span> : null}
    </span>
  );
};

const Top: React.FC<{ summary: string; feishu: string; extra?: React.ReactNode }> = ({ summary, feishu, extra }) => (
  <div className="st-top">
    <div className="st-sum">{summary}</div>
    {extra}
    <FeishuBtn feishu={feishu} />
  </div>
);

/** 两步确认：先点“移除”，行内换成“确认移除 / 取消”；文案写清楚丢什么 */
const Confirm: React.FC<{ text: string; ok: string; busy?: boolean; onOk: () => void; onNo: () => void }> = ({ text, ok, busy, onOk, onNo }) => (
  <div className="st-edit st-ask">
    <span className="st-usev">{text}</span>
    <button type="button" className={`btn sm${ok === "确定改" ? " pri" : " danger"}`} disabled={busy} onClick={onOk}>{ok}</button>
    <button type="button" className="btn sm st-quiet" onClick={onNo}>取消</button>
  </div>
);

const Choice: React.FC<{ row: SettingsRow; busy: boolean; onPick: (v: string) => void; compact?: boolean }> = ({ row, busy, onPick, compact }) => {
  // 配置页的格子（compact）：一个选项是只读文字，其余一律下拉；别处照旧：多于 5 个下拉
  const kind: 'one' | 'sel' | 'seg' = compact ? pickKind(row.options) : row.options.length > 5 ? 'sel' : 'seg';
  if (kind === 'one') {
    return <span className="st-one" title={row.text}>{row.options[0]?.text ?? row.text}</span>;
  }
  if (kind === 'sel') {
    return (
      <select className="st-sel" title={row.text} value={row.current} disabled={busy} aria-label={row.label} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onPick(e.target.value)}>
        {row.options.some((o) => o.value === row.current) ? null : <option value={row.current}>{row.text}</option>}
        {row.options.map((o) => <option key={o.value} value={o.value} title={o.text}>{o.text}</option>)}
      </select>
    );
  }
  return (
    <div className="st-opts" role="radiogroup" aria-label={row.label}>
      {row.options.map((o) => (
        <button key={o.value} title={o.text} type="button" role="radio" aria-checked={o.value === row.current} className={`st-opt${o.value === row.current ? ' on' : ''}`} disabled={busy} onClick={() => o.value !== row.current && onPick(o.value)}>
          {o.text}
        </button>
      ))}
    </div>
  );
};

/** 字长的项：手机上占整行 */
const WIDE: Set<string> = new Set(['t_model', 'model', 'r_model', 'l_model', 'a_model', 't_space', 'space', 'mode', 'subagent', 'assist']);

const RowView: React.FC<{ row: SettingsRow; ops: Ops; feishu: string; mk?: (row: SettingsRow, option: string) => Record<string, unknown>; cell?: boolean; stale?: boolean }> = ({ row, ops, feishu, mk, cell, stale }) => {
  const s: Phase | undefined = ops.st[row.key];
  const busy: boolean = s?.phase === 'saving';
  // 点了就先照选的画，Covey 推来新快照（current 变了）再以快照为准；没成功就回到快照的值
  const [want, setWant] = useState<string | null>(null);
  // 要先确认的选项（管家的“私聊顶层的消息”改成 topics）：先在行内问，确定才发
  const [ask, setAsk] = useState<string | null>(null);
  useEffect(() => setWant(null), [row.current]);
  const shown: SettingsRow =
    want !== null && s?.phase === 'saving'
      ? { ...row, current: want, text: row.options.find((o) => o.value === want)?.text ?? row.text }
      : row;
  return (
    <div className={cell ? `st-cell${WIDE.has(row.key) ? ' wd' : ''}${row.key === 'subagent' ? ' x2' : ''}${busy ? ' saving' : ''}${stale ? ' upd' : ''}` : 'st-r'}>
      <div className="st-l">{row.label}</div>
      <div className="st-c">
        {stale ? (
          <span className="st-one st-wait" aria-live="polite">更新中…</span>
        ) : row.editable ? (
          <Choice
            compact={cell}
            row={shown}
            busy={busy}
            onPick={(v: string) => {
              if (v === 'topics' && row.confirm?.topics) {
                setAsk(v);
                return;
              }
              setAsk(null);
              setWant(v);
              void ops.run(row.key, mk ? mk(row, v) : { what: 'row', kind: row.kind, option: v });
            }}
          />
        ) : (
          <div className="st-ro"><span className="st-val" title={row.text}>{row.text}</span><FeishuBtn feishu={feishu} link /></div>
        )}
        {ask !== null && row.confirm?.topics ? (
          <Confirm
            text={row.confirm.topics}
            ok="确定改"
            onOk={() => {
              setAsk(null);
              setWant(ask);
              void ops.run(row.key, { what: 'row', kind: row.kind, option: ask, confirm: true });
            }}
            onNo={() => setAsk(null)}
          />
        ) : null}
        <Receipt s={s} cell={cell} />
      </div>
    </div>
  );
};

const Rows: React.FC<{ rows: SettingsRow[]; ops: Ops; feishu: string; head?: React.ReactNode; mk?: (row: SettingsRow, option: string) => Record<string, unknown> }> = ({ rows, ops, feishu, head, mk }) => (
  <div className="st-list">
    {head}
    {rows.map((r: SettingsRow) => <RowView key={r.key} row={r} ops={ops} feishu={feishu} mk={mk} />)}
  </div>
);

/** 联动：Harness → 供应商 → 模型 → 思考预算。同一组里前面的项正在保存，后面的项在新快照到来前显示“更新中…”；辅助模型的 Harness 行 key 是 assist */
const LINK = /^(?:t_|r_|l_|a_)?(harness|provider|model|effort)$/;
const LINK_ORDER: string[] = ['harness', 'provider', 'model', 'effort'];
const role = (key: string): string => (key === 'assist' ? 'harness' : LINK.exec(key)?.[1] ?? '');
const linked = (g: SettingsRow[], i: number, ops: Ops): boolean => {
  const me: number = LINK_ORDER.indexOf(role(g[i].key));
  if (me < 1) return false;
  return g.some((r: SettingsRow) => {
    const at: number = LINK_ORDER.indexOf(role(r.key));
    return at >= 0 && at < me && ops.st[r.key]?.phase === 'saving';
  });
};

/** 配置页的紧凑排版：按 key 分行（每行一组格子），数据里没有的 key 跳过；不认识的 key 放到最后一行。start（直接开工）在下线，有就照旧一行显示 */
const Grid: React.FC<{ rows: SettingsRow[]; layout: string[][]; ops: Ops; feishu: string; mk?: (row: SettingsRow, option: string) => Record<string, unknown> }> = ({ rows, layout, ops, feishu, mk }) => {
  const by: Record<string, SettingsRow> = {};
  rows.forEach((r: SettingsRow) => { by[r.key] = r; });
  const known: Set<string> = new Set(layout.flat());
  const groups: SettingsRow[][] = layout.map((g: string[]) => g.filter((k: string) => by[k]).map((k: string) => by[k])).filter((g: SettingsRow[]) => g.length);
  const rest: SettingsRow[] = rows.filter((r: SettingsRow) => !known.has(r.key));
  if (rest.length) groups.push(rest);
  if (!groups.length) return null;
  return (
    <div className="st-list">
      {groups.map((g: SettingsRow[]) => (
        <div key={g[0].key} className={`st-gr cfg n${Math.min(g.length, 4)}`}>
          {g.map((r: SettingsRow, i: number) => <RowView key={r.key} row={r} ops={ops} feishu={feishu} mk={mk} cell stale={linked(g, i, ops)} />)}
        </div>
      ))}
    </div>
  );
};
const TOPIC_LAYOUT: string[][] = [['t_harness', 't_provider', 't_model', 't_effort'], ['t_space', 't_run_mode']];
const MAIN_LAYOUT: string[][] = [['harness', 'provider', 'model', 'effort'], ['space', 'compact'], ['mode']];
/** v36 专项负责人：新建的专项，负责人用的 Harness、模型、思考预算（开了专项才有这一段） */
const LEAD_LAYOUT: string[][] = [['l_harness', 'l_provider', 'l_model', 'l_effort']];
const MORE_LAYOUT: string[][] = [['subagent']];
/** 辅助模型（T884）：分流、整理专项、记忆自己长这些后台活都用这一个；T924 起和上面几段一样，选了 Harness 再选供应商、模型、思考预算；管家关着也能改 */
const ASSIST_LAYOUT: string[][] = [['assist', 'a_provider', 'a_model', 'a_effort']];

const Note: React.FC<{ text?: string }> = ({ text }) => (text ? <p className="st-note">{text}</p> : null);

const ErrorPage: React.FC<{ error: string; feishu: string }> = ({ error, feishu }) => (
  <div className="st-empty">
    <p>{error}</p>
    <FeishuBtn feishu={feishu} />
  </div>
);
const isErr = (p: unknown): p is SettingsError => !!p && typeof p === 'object' && typeof (p as SettingsError).error === 'string';

/* ---------- 各页 ---------- */
const TopicTab: React.FC<{ p: SettingsTopicPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => (
  <>
    <Top summary={p.summary} feishu={feishu} />
    <Rows rows={p.rows} ops={ops} feishu={feishu} />
    <Note text={p.note} />
    <Note text={p.start_note} />
    {p.trusted ? (
      <>
        <h3 className="st-h">记住的工作区</h3>
        <div className="st-list">
          {p.trusted.length ? p.trusted.map((t) => (
            <div key={t.key} className="st-r st-tr">
              <div className="st-c">
                <div className="st-ro"><span className="st-val">{t.text}</span>
                  <button type="button" className="st-link" disabled={ops.st[`untrust:${t.key}`]?.phase === 'saving'} onClick={() => void ops.run(`untrust:${t.key}`, { what: 'untrust', key: t.key })}>移除</button>
                </div>
                <Receipt s={ops.st[`untrust:${t.key}`]} />
              </div>
            </div>
          )) : <div className="st-r st-tr"><span className="st-faint">还没有记住的工作区</span></div>}
        </div>
      </>
    ) : null}
    {p.more.length ? (
      <>
        <h3 className="st-h">更多</h3>
        <Rows rows={p.more} ops={ops} feishu={feishu} />
        <Note text={p.more_note} />
      </>
    ) : null}
    <Note text={p.locked} />
  </>
);

/** 记住的工作区：管家问过“以后这类直接开工吗”、主人同意过的，每项可移除 */
const Trusted: React.FC<{ items: { key: string; text: string }[]; ops: Ops }> = ({ items, ops }) => (
  <>
    <h4 className="st-h4">记住的工作区</h4>
    <div className="st-list">
      {items.length ? items.map((t) => (
        <div key={t.key} className="st-r st-tr">
          <div className="st-c">
            <div className="st-ro"><span className="st-val">{t.text}</span>
              <button type="button" className="st-link" disabled={ops.st[`untrust:${t.key}`]?.phase === 'saving'} onClick={() => void ops.run(`untrust:${t.key}`, { what: 'untrust', key: t.key })}>移除</button>
            </div>
            <Receipt s={ops.st[`untrust:${t.key}`]} />
          </div>
        </div>
      )) : <div className="st-r st-tr"><span className="st-faint">还没有记住的工作区</span></div>}
    </div>
  </>
);

/** 后台活最近一次：每个功能一行，功能、用了谁、结果、原因、耗时、时间（只有原因码的说法，没有原话） */
const RecentTable: React.FC<{ lines: RecentLine[] }> = ({ lines }) => (
  <>
    <h4 className="st-h4">后台活最近一次</h4>
    {lines.length ? (
      <div className="st-list st-rec" role="table" aria-label="后台活最近一次">
        <div className="st-rec-r st-rec-h" role="row">
          {['功能', '用了谁', '结果', '原因', '耗时', '时间'].map((t: string) => <span key={t} role="columnheader">{t}</span>)}
        </div>
        {lines.map((x: RecentLine) => (
          <div key={x.key} className={`st-rec-r ${x.tone}`} role="row">
            <span className="st-rec-f" role="cell">{x.feature}</span>
            <span className="st-rec-w" role="cell" title={x.who}>{x.who}</span>
            <span className={`st-rec-o ${x.tone}`} role="cell">{x.result}</span>
            <span className="st-rec-y" role="cell" title={x.why}>{x.why || '—'}</span>
            <span className="st-rec-t" role="cell">{x.took}</span>
            <span className="st-rec-a" role="cell">{x.when}</span>
          </div>
        ))}
      </div>
    ) : <p className="st-note">还没有后台活调过模型。</p>}
  </>
);

/** 辅助模型：Harness（自动跟随管家、各 Harness、不用模型），选了 Harness 再有供应商、模型、思考预算；说明，现在实际用的，后台活最近一次 */
const AssistSection: React.FC<{ p: SettingsConfigAssist; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const now: number = Math.floor(useTick(30000));
  return (
    <section className="st-sec st-assist">
      <h3 className="st-h">{p.title}</h3>
      <Grid rows={p.rows} layout={ASSIST_LAYOUT} ops={ops} feishu={feishu} />
      <Note text={p.note} />
      {p.words ? <p className="st-note st-now">{p.words}</p> : null}
      <RecentTable lines={recentLines(p.recent, now)} />
    </section>
  );
};

/** 配置（v22）：和飞书设置卡的「配置」同一页同几段；卡片一次只展开一段（放不下），网页没有这个限制，各段都展开 */
const ConfigTab: React.FC<{ p: SettingsConfigPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => (
  <>
    <Top summary={p.summary} feishu={feishu} />
    <Note text={p.locked} />
    <section className="st-sec">
      <h3 className="st-h">{p.topic.title}</h3>
      <Grid rows={p.topic.rows} layout={TOPIC_LAYOUT} ops={ops} feishu={feishu} />
      <Note text={p.topic.note} />
      {p.topic.start_note ? <Note text={p.topic.start_note} /> : null}
      {p.topic.trusted ? <Trusted items={p.topic.trusted} ops={ops} /> : null}
    </section>
    {p.main ? (
      <section className="st-sec">
        <h3 className="st-h">{p.main.title}</h3>
        <Grid rows={p.main.rows} layout={MAIN_LAYOUT} ops={ops} feishu={feishu} />
        <Note text={p.main.note} />
      </section>
    ) : null}
    {p.lead ? (
      <section className="st-sec">
        <h3 className="st-h">{p.lead.title}</h3>
        <Grid rows={p.lead.rows} layout={LEAD_LAYOUT} ops={ops} feishu={feishu} />
        <Note text={p.lead.note} />
      </section>
    ) : null}
    {p.assist ? <AssistSection p={p.assist} ops={ops} feishu={feishu} /> : null}
    {p.more.length ? (
      <section className="st-sec">
        <h3 className="st-h">更多</h3>
        <Grid rows={p.more} layout={MORE_LAYOUT} ops={ops} feishu={feishu} />
        <Note text={p.more_note} />
      </section>
    ) : null}
  </>
);

const MainTab: React.FC<{ p: SettingsMainPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => (
  <>
    <Top summary={p.summary} feishu={feishu} />
    <Rows
      rows={p.rows}
      ops={ops}
      feishu={feishu}
      head={p.harness ? <div className="st-r"><div className="st-l">Harness</div><div className="st-c"><div className="st-ro"><span className="st-val">{p.harness}</span></div></div></div> : undefined}
    />
    {p.more?.length ? (
      <>
        <h3 className="st-h">更多</h3>
        <Rows rows={p.more} ops={ops} feishu={feishu} />
      </>
    ) : null}
    <Note text={p.note} />
    <Note text={p.locked} />
  </>
);

const WsItem: React.FC<{ it: SettingsWorkspaceItem; page: SettingsWorkspacePage; ops: Ops; now: number }> = ({ it, page, ops, now }) => {
  const [edit, setEdit] = useState<boolean>(false);
  const [text, setText] = useState<string>(it.note ?? '');
  const [ask, setAsk] = useState<boolean>(false);
  const rid = `wsrm:${it.name}`;
  const id = `note:${it.name}`;
  const s: Phase | undefined = ops.st[id];
  const busy: boolean = s?.phase === 'saving';
  const save = (): void => {
    setEdit(false);
    void ops.run(id, { what: 'note', workspace: it.name, note: text.trim() });
  };
  return (
    <div className="st-ws">
      <div className="st-wh">
        <b className="st-wn">{it.name}</b>
        {it.tags.map((t) => <Tag key={t.text} tone={t.tone} text={t.text} />)}
        <span className="st-when">{it.used ? ago(it.used, now) : '还没用过'}</span>
        {it.removable && !ask ? <button type="button" className="st-link danger" disabled={ops.st[rid]?.phase === 'saving'} onClick={() => setAsk(true)}>移除</button> : null}
      </div>
      {ask ? (
        <Confirm
          text="只是不再登记，目录不删"
          ok="确认移除"
          busy={ops.st[rid]?.phase === 'saving'}
          onOk={() => { setAsk(false); void ops.run(rid, { what: 'workspace_remove', workspace: it.name }); }}
          onNo={() => setAsk(false)}
        />
      ) : null}
      <Receipt s={ops.st[rid]} />
      <div className="st-path" title={it.path}>{it.path}</div>
      {page.notes ? (
        edit ? (
          <div className="st-edit">
            <input
              className="st-in-t"
              value={text}
              maxLength={page.note_length}
              autoFocus
              placeholder="写一句它是干什么的"
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setText(e.target.value)}
              onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEdit(false); }}
            />
            <span className="st-cnt">{Array.from(text).length}/{page.note_length}</span>
            <button type="button" className="btn pri sm" onClick={save}>保存</button>
            <button type="button" className="btn sm st-quiet" onClick={() => { setText(it.note ?? ''); setEdit(false); }}>取消</button>
          </div>
        ) : (
          <div className="st-use">
            <span className={it.note ? "st-usev" : "st-faint"}>{it.note ? `用途：${it.note}` : '还没写用途'}</span>
            <button type="button" className="st-link" disabled={busy} onClick={() => { setText(it.note ?? ''); setEdit(true); }}>改用途</button>
          </div>
        )
      ) : null}
      <Receipt s={s} />
    </div>
  );
};

const WorkspaceAdd: React.FC<{ p: SettingsWorkspacePage; ops: Ops }> = ({ p, ops }) => {
  const [path, setPath] = useState<string>('');
  const s: Phase | undefined = ops.st['ws:add'];
  const busy: boolean = s?.phase === 'saving';
  const ps: Phase | undefined = ops.st['ws:prune'];
  const missing: number = p.missing ?? 0;
  const add = (): void => {
    if (!path.trim()) return;
    void ops.run('ws:add', { what: 'workspace_add', path: path.trim() });
    setPath('');
  };
  return (
    <>
      <h3 className="st-h">添加工作区</h3>
      <div className="st-list">
        <div className="st-addr">
          <input className="st-in-t" value={path} maxLength={1000} disabled={busy} placeholder="绝对路径，或 设备别名:路径" aria-label="工作区路径"
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPath(e.target.value)}
            onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') add(); }} />
          <button type="button" className="btn pri" disabled={busy || !path.trim()} onClick={add}>添加</button>
        </div>
        {missing > 0 ? (
          <div className="st-addr st-prune">
            <span className="st-why">有 {missing} 个目录确认不存在</span>
            <button type="button" className="btn" disabled={ps?.phase === 'saving'} onClick={() => void ops.run('ws:prune', { what: 'workspace_prune' })}>移除不存在的（{missing}）</button>
          </div>
        ) : null}
      </div>
      <Receipt s={s} />
      <Receipt s={ps} />
    </>
  );
};

const WorkspaceTab: React.FC<{ p: SettingsWorkspacePage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const now: number = useTick(60000);
  return (
    <>
      <Top summary={p.summary} feishu={feishu} />
      <div className="st-list">
        {p.items.length ? p.items.map((it) => <WsItem key={it.name} it={it} page={p} ops={ops} now={now} />) : <div className="st-r st-tr"><span className="st-faint">还没有工作区</span></div>}
      </div>
      <WorkspaceAdd p={p} ops={ops} />
      <Note text={p.note} />
      <Note text="目录浏览请到飞书设置卡上用。" />
    </>
  );
};

/* ---------- 常用路径页（v43 仓库页，v46 加目录和设备上的路径）：纯函数在 reposPage.ts，样式在 repos.css（类名前缀 rp-） ---------- */
const RepoRow: React.FC<{ it: SettingsRepoItem; ops: Ops; now: number; devices?: RP.PathDevice[] }> = ({ it, ops, now, devices }) => {
  const [edit, setEdit] = useState<boolean>(false);
  const [ask, setAsk] = useState<boolean>(false);
  const [d, setD] = useState<RP.RepoDraft>(RP.draftOf(it));
  const sid = `repo:set:${it.name}`;
  const rid = `repo:rm:${it.name}`;
  const busy: boolean = ops.st[sid]?.phase === 'saving';
  const payload = RP.repoSetPayload(it, d);
  const save = (): void => {
    if (!payload) return;
    setEdit(false);
    void ops.run(sid, payload);
  };
  const label: string = RP.statusLabel(it.status);
  return (
    <div className={`rp-row${it.status !== 'active' ? ' dim' : ''}`}>
      <div className="rp-main">
        <div className="rp-hd">
          <b className="rp-name">{it.name}</b>
          {RP.placeTags(it, devices).map((t: string) => <span key={t} className="pill tone-running">{t}</span>)}
          {label ? <span className="pill tone-queued">{label}</span> : null}
        </div>
        {it.note ? <div className="rp-note">{it.note}</div> : null}
        <div className="rp-meta">{RP.repoMeta(it, now, ago).join(' · ')}</div>
      </div>
      {!edit && !ask ? (
        <div className="rp-act">
          <button type="button" className="st-link" disabled={busy} onClick={() => { setD(RP.draftOf(it)); setEdit(true); }}>改</button>
          <button type="button" className="st-link danger" disabled={ops.st[rid]?.phase === 'saving'} onClick={() => setAsk(true)}>移除</button>
        </div>
      ) : null}
      {edit ? (
        <div className="rp-edit">
          <div className="rp-f">
            <input
              className="st-in-t"
              value={d.note}
              maxLength={RP.NOTE_MAX}
              autoFocus
              placeholder="写一句它是干什么的"
              aria-label="用途"
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, note: e.target.value })}
              onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEdit(false); }}
            />
            <span className="st-cnt">{Array.from(d.note).length}/{RP.NOTE_MAX}</span>
          </div>
          <div className="rp-f">
            <select className="st-sel" aria-label="状态" value={d.status} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setD({ ...d, status: e.target.value as RP.RepoStatus })}>
              {RP.STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
            </select>
            <select className="st-sel" aria-label="写法" value={d.write_mode} disabled={RP.fixedInPlace(it)} title={RP.fixedInPlace(it) ? '目录和设备上的路径只能原地改' : undefined}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setD({ ...d, write_mode: e.target.value as RP.RepoWriteMode })}>
              {RP.MODE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
            </select>
          </div>
          <div className="rp-f">
            <button type="button" className="btn pri sm" disabled={!payload} onClick={save}>保存</button>
            <button type="button" className="btn sm st-quiet" onClick={() => setEdit(false)}>取消</button>
          </div>
        </div>
      ) : null}
      {ask ? (
        <Confirm
          text="只是不再登记，目录不删"
          ok="确认移除"
          busy={ops.st[rid]?.phase === 'saving'}
          onOk={() => { setAsk(false); void ops.run(rid, RP.repoRemovePayload(it.name)); }}
          onNo={() => setAsk(false)}
        />
      ) : null}
      <Receipt s={ops.st[sid]} />
      <Receipt s={ops.st[rid]} />
    </div>
  );
};

const RepoAdd: React.FC<{ ops: Ops; devices?: RP.PathDevice[] }> = ({ ops, devices }) => {
  const [path, setPath] = useState<string>('');
  const [name, setName] = useState<string>('');
  const [note, setNote] = useState<string>('');
  const [device, setDevice] = useState<string>('');
  const [more, setMore] = useState<boolean>(false);
  const s: Phase | undefined = ops.st['repo:add'];
  const busy: boolean = s?.phase === 'saving';
  const list: RP.PathDevice[] = devices ?? [];
  const where: string = list.some((d) => d.alias === device) ? device : '';
  const payload = RP.repoAddPayload(path, name, note, where);
  const add = (): void => {
    if (!payload) return;
    void ops.run('repo:add', payload);
    setPath(''); setName(''); setNote('');
  };
  const onKey = (e: React.KeyboardEvent): void => { if (e.key === 'Enter') add(); };
  return (
    <>
      <h3 className="st-h">添加常用路径</h3>
      <div className="st-list">
        <div className="rp-add">
          <div className="rp-addrow">
            {list.length ? (
              <select className="st-sel rp-where" aria-label="在哪台机器上" value={where} disabled={busy} onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setDevice(e.target.value)}>
                <option value="">本机</option>
                {list.map((d: RP.PathDevice) => <option key={d.alias} value={d.alias}>{d.title || d.alias}</option>)}
              </select>
            ) : null}
            <input className="st-in-t" value={path} maxLength={RP.PATH_MAX} disabled={busy} placeholder={RP.addHint(where, list)} aria-label="仓库路径"
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPath(e.target.value)} onKeyDown={onKey} />
            <button type="button" className="btn pri" disabled={busy || !payload} onClick={add}>添加</button>
          </div>
          {more ? (
            <div className="rp-addrow">
              <input className="st-in-t" value={name} maxLength={RP.NAME_MAX} disabled={busy} placeholder="名字（可不填）" aria-label="仓库名字"
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value)} onKeyDown={onKey} />
              <input className="st-in-t" value={note} maxLength={RP.NOTE_MAX} disabled={busy} placeholder="用途（可不填）" aria-label="仓库用途"
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setNote(e.target.value)} onKeyDown={onKey} />
            </div>
          ) : <button type="button" className="st-link rp-more" onClick={() => setMore(true)}>更多</button>}
        </div>
      </div>
      <Receipt s={s} />
      <Note text={list.length ? '有 .git 的记成仓库，没有的记成目录（只能原地改）。设备上的路径由那台设备查，最多等 5 秒。' : '有 .git 的记成仓库，没有的记成目录（只能原地改）。'} />
    </>
  );
};

const RepoPrune: React.FC<{ p: SettingsReposPage; ops: Ops }> = ({ p, ops }) => {
  const list = p.worktrees?.prunable ?? [];
  const [sel, setSel] = useState<string[]>([]);
  const [ask, setAsk] = useState<boolean>(false);
  const s: Phase | undefined = ops.st['wt:prune'];
  const busy: boolean = s?.phase === 'saving';
  // 快照变了，没了的勾选自动丢掉
  const picked: string[] = RP.keepSelected(sel, list);
  const batch = RP.pruneBatch(picked, list);
  if (!list.length) {
    return (
      <>
        <div className="rp-none">{RP.pruneEmptyText(p.worktrees?.total ?? 0)}</div>
        <Receipt s={s} />
      </>
    );
  }
  const all: boolean = picked.length === list.length;
  return (
    <>
      <h3 className="st-h">可清理的 worktree（{list.length}）</h3>
      <div className="st-list">
        <div className="rp-pr-hd">
          <label className="rp-chk"><input type="checkbox" checked={all} onChange={() => { setAsk(false); setSel(RP.toggleAll(picked, list)); }} />全选</label>
        </div>
        {list.map((w) => {
          const k: string = RP.pruneKey(w);
          return (
            <label key={k} className="rp-pr">
              <input type="checkbox" checked={picked.includes(k)} onChange={() => { setAsk(false); setSel(RP.toggleKey(picked, k)); }} />
              <span className="rp-prt">{RP.pruneLine(w)}</span>
            </label>
          );
        })}
        <div className="rp-pr-ft">
          {ask && picked.length ? (
            <Confirm
              text={RP.pruneAskText(batch.paths.length, batch.rest)}
              ok="确认清理"
              busy={busy}
              onOk={() => { setAsk(false); setSel(picked.filter((x) => !batch.paths.includes(x))); void ops.run('wt:prune', RP.prunePayload(batch.paths)); }}
              onNo={() => setAsk(false)}
            />
          ) : (
            <button type="button" className="btn" disabled={busy || !picked.length} onClick={() => setAsk(true)}>清理选中的 {picked.length} 个</button>
          )}
        </div>
      </div>
      <Receipt s={s} />
    </>
  );
};

/** 一组小筛选（全部 / 本机 / 各台设备，或 全部 / 仓库 / 目录） */
const Filter: React.FC<{ label: string; value: string; options: { value: string; text: string }[]; onPick: (v: string) => void }> = ({ label, value, options, onPick }) => (
  <div className="st-opts rp-filter" role="radiogroup" aria-label={label}>
    {options.map((o) => (
      <button key={o.value || 'all'} type="button" role="radio" aria-checked={value === o.value} className={`st-opt sm${value === o.value ? ' on' : ''}`} onClick={() => onPick(o.value)}>{o.text}</button>
    ))}
  </div>
);

const ReposTab: React.FC<{ p: SettingsReposPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const now: number = useTick(60000);
  const [place, setPlace] = useState<string>('');
  const [kind, setKind] = useState<RP.KindFilter>('');
  const places = RP.placeOptions(p);
  const kinds: boolean = RP.showKindFilter(p.items);
  const at: string = places.some((o) => o.value === place) ? place : '';
  const shown: SettingsRepoItem[] = RP.filterItems(p.items, at, kinds ? kind : '');
  return (
    <>
      <Top summary={RP.summaryText(p)} feishu={feishu} />
      {places.length || kinds ? (
        <div className="rp-filters">
          {places.length ? <Filter label="在哪" value={at} options={places} onPick={setPlace} /> : null}
          {kinds ? <Filter label="类型" value={kind} options={RP.KIND_FILTERS} onPick={(v: string) => setKind(v as RP.KindFilter)} /> : null}
        </div>
      ) : null}
      <div className="st-list rp-list">
        {shown.length ? shown.map((it) => <RepoRow key={it.name} it={it} ops={ops} now={now} devices={p.devices} />)
          : <div className="st-r st-tr"><span className="st-faint">{p.items.length ? '没有符合筛选的' : '还没有常用路径'}</span></div>}
      </div>
      <RepoAdd ops={ops} devices={p.devices} />
      <RepoPrune p={p} ops={ops} />
    </>
  );
};

const GroupRow: React.FC<{ g: SettingsGroupItem; ops: Ops }> = ({ g, ops }) => {
  const [ask, setAsk] = useState<boolean>(false);
  const s: Phase | undefined = ops.st[`group:${g.key}`];
  const busy: boolean = s?.phase === 'saving';
  const send = (on: boolean): void => {
    setAsk(false);
    void ops.run(`group:${g.key}`, { what: 'group', group: g.key, on });
  };
  const fid = `free:${g.key}`;
  const fs: Phase | undefined = ops.st[fid];
  const choices = g.free_choices ?? [];
  // 点了先照选的画，快照回来再以快照为准；没成功回到快照
  const [want, setWant] = useState<string | null>(null);
  useEffect(() => setWant(null), [g.free_mode]);
  const mode: string | null = want !== null && fs?.phase !== 'failed' ? want : (g.free_mode ?? null);
  return (
    <div className="st-ws st-gr">
      <div className="st-wh">
        <b className="st-wn">{g.name}</b>
        {g.space ? <span className="st-tag">{g.space}</span> : null}
        {g.state ? <Tag tone={g.state.tone} text={g.state.text} /> : null}
        <span className="st-when">{g.free}</span>
        {g.switch === 'off' && !ask ? <button type="button" className="st-link danger" disabled={busy} onClick={() => setAsk(true)}>停用</button> : null}
        {g.switch === 'on' ? <button type="button" className="btn sm" disabled={busy} onClick={() => send(true)}>启用</button> : null}
      </div>
      {ask ? (
        <div className="st-edit">
          <span className="st-usev">停用后只处理你的消息</span>
          <button type="button" className="btn sm danger" onClick={() => send(false)}>确认停用</button>
          <button type="button" className="btn sm st-quiet" onClick={() => setAsk(false)}>取消</button>
        </div>
      ) : null}
      <Receipt s={s} />
      {choices.length ? (
        <div className="st-use st-free">
          <span className="st-faint">免@</span>
          <div className="st-opts" role="radiogroup" aria-label="免@">
            {choices.map((o) => (
              <button key={o.value} title={o.text} type="button" role="radio" aria-checked={mode === o.value || (o.value === '' && mode === null)} disabled={fs?.phase === 'saving'}
                className={`st-opt sm${mode === o.value || (o.value === '' && mode === null) ? ' on' : ''}`}
                onClick={() => {
                  if (mode === o.value || (o.value === '' && mode === null)) return;
                  setWant(o.value);
                  void ops.run(fid, { what: 'free', group: g.key, mode: o.value });
                }}>{o.text}</button>
            ))}
          </div>
          {mode === 'some' || mode === 'all' ? <span className="st-faint">现在：{g.free}</span> : null}
        </div>
      ) : null}
      <Receipt s={fs} />
    </div>
  );
};

const GroupsTab: React.FC<{ p: SettingsGroupsPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => (
  <>
    <Top summary={p.summary} feishu={feishu} />
    <div className="st-list">
      {p.items.length ? p.items.map((g) => <GroupRow key={g.key} g={g} ops={ops} />) : <div className="st-r st-tr"><span className="st-faint">还没有群</span></div>}
    </div>
    <Note text="免@ 里“你和选中的人”“群里所有人”请到飞书设置卡上改。" />
  </>
);

const DOT: Record<string, string> = { ok: 'ok', tight: 'tight', down: 'down', never: 'never' };
const NODE_NAME = /^[a-z][a-z0-9-]{0,23}$/u;

const NodeRow: React.FC<{ n: SettingsNodesPage['items'][number]; ops: Ops }> = ({ n, ops }) => {
  const [ask, setAsk] = useState<boolean>(false);
  const id = `node:${n.alias}`;
  const s: Phase | undefined = ops.st[id];
  return (
    <div className="st-ws">
      <div className="st-wh">
        <i className={`st-dot ${DOT[n.dot ?? (n.grey ? 'never' : 'ok')] ?? 'never'}`} aria-hidden="true" />
        <b className="st-wn">{n.title}</b>
        <span className={`st-nl ${n.grey ? 'st-faint' : 'st-usev'}`}>{n.line}</span>
        {!n.local && !ask ? <button type="button" className="st-link danger st-end" disabled={s?.phase === 'saving'} onClick={() => setAsk(true)}>移除</button> : null}
      </div>
      {ask ? (
        <Confirm text={`移除设备 ${n.title}？`} ok="确认移除" busy={s?.phase === 'saving'} onOk={() => { setAsk(false); void ops.run(id, { what: 'node_remove', alias: n.alias }); }} onNo={() => setAsk(false)} />
      ) : null}
      <Receipt s={s} />
    </div>
  );
};

const NodeAdd: React.FC<{ can: boolean; ops: Ops }> = ({ can, ops }) => {
  const [name, setName] = useState<string>('');
  const [title, setTitle] = useState<string>('');
  const [ssh, setSsh] = useState<string>('');
  const s: Phase | undefined = ops.st['node:add'];
  const busy: boolean = s?.phase === 'saving';
  const nameOk: boolean = NODE_NAME.test(name);
  const ok: boolean = can && nameOk && !!ssh.trim() && !busy;
  const send = (): void => {
    if (!ok) return;
    void ops.run('node:add', { what: 'node_add', name, title: title.trim(), ssh: ssh.trim() });
    setName('');
    setTitle('');
    setSsh('');
  };
  return (
    <>
      <h3 className="st-h">添加设备</h3>
      <div className="st-list">
        <div className="st-form">
          <label className="st-fl">名字<input className="st-in-t" value={name} maxLength={24} disabled={!can || busy} placeholder="小写字母开头，如 buildbox" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value.trim())} /></label>
          <label className="st-fl">显示名<input className="st-in-t" value={title} maxLength={40} disabled={!can || busy} placeholder="可不填" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTitle(e.target.value)} /></label>
          <label className="st-fl">ssh 地址<input className="st-in-t" value={ssh} maxLength={200} disabled={!can || busy} placeholder="user@host -p 22" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSsh(e.target.value)} onKeyDown={(e: React.KeyboardEvent) => { if (e.key === 'Enter') send(); }} /></label>
          <div className="st-bar st-bar-in">
            <span className={`st-why${name && !nameOk ? ' bad' : ''}`}>
              {!can ? 'Hub 还没开始监听' : name && !nameOk ? '名字只用小写字母、数字和 -，小写字母开头，最多 24 个' : '名字只用小写字母、数字和 -，最多 24 个'}
            </span>
            <button type="button" className="btn pri" disabled={!ok} onClick={send}>添加</button>
          </div>
        </div>
      </div>
      <Receipt s={s} />
      <Note text="网页上只能经 ssh 添加；不填 ssh 的方式请到飞书设置卡上拿命令。" />
    </>
  );
};

/* ---------- 设备页（v46）：devices.json 和监控节点按别名并成一行；纯函数在 devicesPage.ts，样式在 repos.css（类名前缀 dv-） ---------- */
const AccessSwitch: React.FC<{ r: SettingsDeviceRow; ops: Ops }> = ({ r, ops }) => {
  const id = `device:access:${r.alias}`;
  const s: Phase | undefined = ops.st[id];
  const [want, setWant] = useState<DV.Access | null>(null);
  useEffect(() => setWant(null), [r.access]);
  const cur: string = want !== null && s?.phase !== 'failed' ? want : r.access;
  return (
    <div className="st-opts dv-access" role="radiogroup" aria-label={`${r.title} 可写还是只读`}>
      {(['read', 'write'] as DV.Access[]).map((a: DV.Access) => (
        <button key={a} type="button" role="radio" aria-checked={cur === a} disabled={s?.phase === 'saving'} className={`st-opt sm${cur === a ? ' on' : ''}`}
          onClick={() => { if (cur === a) return; setWant(a); void ops.run(id, DV.accessPayload(r, a)); }}>{DV.accessText(a)}</button>
      ))}
    </div>
  );
};

const DeviceRow: React.FC<{ r: SettingsDeviceRow; ops: Ops; now: number; canNode: boolean }> = ({ r, ops, now, canNode }) => {
  const [ask, setAsk] = useState<boolean>(false);
  const [edit, setEdit] = useState<boolean>(false);
  const [d, setD] = useState<DV.EditDraft>(DV.editDraftOf(r));
  const cid = `device:check:${r.alias}`;
  const sid = `device:set:${r.alias}`;
  const rid = `device:rm:${r.alias}`;
  const nid = `device:node:${r.alias}`;
  const check = DV.checkLine(r, now, ago);
  const link: string = DV.linkLine(r);
  const node: string = DV.nodeLine(r);
  const payload = DV.deviceSetPayload(r, d);
  const removable: boolean = !r.local && (r.listed || !!r.node);
  const save = (): void => {
    if (!payload) return;
    setEdit(false);
    void ops.run(sid, payload);
  };
  return (
    <div className="rp-row dv-row">
      <div className="rp-main">
        <div className="rp-hd">
          <i className={`st-dot ${DV.busy(r) ? 'busy' : DV.rowDot(r)}`} aria-hidden="true" />
          <b className="rp-name">{r.title}</b>
          {r.title !== r.alias && !r.local ? <span className="st-faint dv-alias">{r.alias}</span> : null}
          {DV.rowTags(r).map((t: string) => <span key={t} className={`pill ${t === '可写' ? 'tone-result' : 'tone-queued'}`}>{t}</span>)}
        </div>
        {r.note ? <div className="rp-note">{r.note}</div> : null}
        {link ? <div className="rp-meta">{link}</div> : null}
        {node ? <div className="rp-meta">{node}</div> : null}
        {check.text ? <div className={`rp-meta${check.bad ? ' dv-bad' : ''}`}>{check.text}</div> : null}
      </div>
      {!edit && !ask ? (
        <div className="rp-act">
          {r.listed ? <AccessSwitch r={r} ops={ops} /> : null}
          {r.listed && r.runs ? <button type="button" className="st-link" disabled={DV.busy(r) || ops.st[cid]?.phase === 'saving'} onClick={() => void ops.run(cid, { what: 'device_check', alias: r.alias })}>体检</button> : null}
          {DV.canInstallNode(r, canNode) ? <button type="button" className="st-link" disabled={ops.st[nid]?.phase === 'saving'} onClick={() => void ops.run(nid, DV.nodeAddPayload(r))}>装监控节点</button> : null}
          {r.listed ? <button type="button" className="st-link" onClick={() => { setD(DV.editDraftOf(r)); setEdit(true); }}>改</button> : null}
          {removable ? <button type="button" className="st-link danger" disabled={ops.st[rid]?.phase === 'saving'} onClick={() => setAsk(true)}>移除</button> : null}
        </div>
      ) : null}
      {edit ? (
        <div className="rp-edit">
          <div className="rp-f">
            <input className="st-in-t" value={d.title} maxLength={DV.TITLE_MAX} placeholder="名字" aria-label="名字" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, title: e.target.value })} />
            <input className="st-in-t" value={d.note} maxLength={DV.NOTE_MAX} placeholder="用途（一行）" aria-label="用途" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, note: e.target.value })} />
          </div>
          {r.runs ? (
            <div className="rp-f">
              <input className="st-in-t" value={d.ssh} maxLength={DV.SSH_MAX} placeholder="ssh 地址，如 me@host -p 22" aria-label="ssh 地址" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, ssh: e.target.value })} />
            </div>
          ) : null}
          <div className="rp-f">
            <button type="button" className="btn pri sm" disabled={!payload} onClick={save}>保存</button>
            <button type="button" className="btn sm st-quiet" onClick={() => setEdit(false)}>取消</button>
          </div>
        </div>
      ) : null}
      {ask ? (
        <Confirm text={DV.removeAsk(r)} ok="确认移除" busy={ops.st[rid]?.phase === 'saving'} onOk={() => { setAsk(false); void ops.run(rid, { what: 'device_remove', alias: r.alias }); }} onNo={() => setAsk(false)} />
      ) : null}
      {r.checking?.state === 'done' && r.checking.message ? <div className="st-rc">{r.checking.message}</div> : null}
      <Receipt s={ops.st[cid]} />
      <Receipt s={ops.st[sid]} />
      <Receipt s={ops.st[rid]} />
      <Receipt s={ops.st[nid]} />
      <Receipt s={ops.st[`device:access:${r.alias}`]} />
    </div>
  );
};

const DeviceAdd: React.FC<{ ops: Ops }> = ({ ops }) => {
  const [d, setD] = useState<DV.DeviceDraft>(DV.emptyDraft());
  const s: Phase | undefined = ops.st['device:add'];
  const busy: boolean = s?.phase === 'saving';
  const payload = DV.deviceAddPayload(d);
  const bad: boolean = !!d.name && !DV.NAME.test(d.name);
  const send = (): void => {
    if (!payload || busy) return;
    void ops.run('device:add', payload);
    setD(DV.emptyDraft());
  };
  return (
    <>
      <h3 className="st-h">添加设备</h3>
      <div className="st-list">
        <div className="st-form">
          <label className="st-fl">名字<input className="st-in-t" value={d.name} maxLength={24} disabled={busy} placeholder="小写字母开头，如 buildbox" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, name: e.target.value.trim() })} /></label>
          <label className="st-fl">显示名<input className="st-in-t" value={d.title} maxLength={DV.TITLE_MAX} disabled={busy} placeholder="可不填" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, title: e.target.value })} /></label>
          <label className="st-fl">ssh 地址<input className="st-in-t" value={d.ssh} maxLength={DV.SSH_MAX} disabled={busy} placeholder="~/.ssh/config 的别名或 user@host -p 22；可不填" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, ssh: e.target.value })} /></label>
          <label className="st-fl">用途<input className="st-in-t" value={d.note} maxLength={DV.NOTE_MAX} disabled={busy} placeholder="一行，可不填" onChange={(e: React.ChangeEvent<HTMLInputElement>) => setD({ ...d, note: e.target.value })} /></label>
          <div className="st-fl dv-fl">读写
            <div className="st-opts dv-access" role="radiogroup" aria-label="新设备可写还是只读">
              {(['read', 'write'] as DV.Access[]).map((a: DV.Access) => (
                <button key={a} type="button" role="radio" aria-checked={d.access === a} disabled={busy} className={`st-opt sm${d.access === a ? ' on' : ''}`} onClick={() => setD({ ...d, access: a })}>{DV.accessText(a)}</button>
              ))}
            </div>
          </div>
          <div className="st-bar st-bar-in">
            <span className={`st-why${bad ? ' bad' : ''}`}>{bad ? '名字只用小写字母、数字和 -，小写字母开头，最多 24 个' : d.ssh.trim() ? '先体检（ssh、设备端小程序、hello），过了才加进名单' : '不填 ssh：只能看状况，不能执行'}</span>
            <button type="button" className="btn pri" disabled={!payload || busy} onClick={send}>{DV.addLabel(d)}</button>
          </div>
        </div>
      </div>
      <Receipt s={s} />
      <Note text="新加的设备默认只读。网页不收密码和私钥：ssh 要能免密登录，钥匙留在 ~/.ssh。只装监控节点不填 ssh 的方式，请到飞书设置卡上拿命令。" />
    </>
  );
};

const DevicesTab: React.FC<{ p: SettingsNodesPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const now: number = useTick(30000);
  const rs: Phase | undefined = ops.st['node:refresh'];
  const rows: SettingsDeviceRow[] = p.rows ?? [];
  return (
    <>
      <Top
        summary={p.summary}
        feishu={feishu}
        extra={<button type="button" className="btn" disabled={rs?.phase === 'saving'} onClick={() => void ops.run('node:refresh', { what: 'refresh' })}>刷新</button>}
      />
      <Receipt s={rs} />
      {p.error ? <ul className="st-problems"><li>devices.json 有问题，先手工改好：{p.error}</li></ul> : null}
      <div className="st-list rp-list">
        {rows.length ? rows.map((r: SettingsDeviceRow) => <DeviceRow key={r.alias} r={r} ops={ops} now={now} canNode={!!p.can_add} />)
          : <div className="st-r st-tr"><span className="st-faint">还没有设备</span></div>}
        {(p.adding ?? []).filter((j) => j.state !== 'done').map((j) => (
          <div key={`add-${j.alias}`} className="st-ws">
            <div className="st-wh">
              <i className={`st-dot ${j.state === 'checking' ? 'busy' : 'down'}`} aria-hidden="true" />
              <b className="st-wn">{j.alias}</b>
              <span className={`st-nl ${j.state === 'failed' ? 'st-fail' : 'st-usev'}`}>{j.state === 'checking' ? '体检中…' : `没加上：${j.message}`}</span>
            </div>
          </div>
        ))}
        {(p.joins ?? []).filter((j) => j.state !== 'done').map((j) => (
          <div key={`join-${j.alias}`} className="st-ws">
            <div className="st-wh">
              <i className={`st-dot ${j.state === 'joining' ? 'busy' : 'down'}`} aria-hidden="true" />
              <b className="st-wn">{j.alias}</b>
              <span className={`st-nl ${j.state === 'failed' ? 'st-fail' : 'st-usev'}`}>{j.state === 'joining' ? '监控节点安装中…' : `监控节点没装上：${j.message}`}</span>
            </div>
          </div>
        ))}
      </div>
      {p.can_add_device ? <DeviceAdd ops={ops} /> : <Note text="多设备没开（COVEY_DEVICES=1）：这里只看监控节点，加不了设备。" />}
    </>
  );
};

const NodesTab: React.FC<{ p: SettingsNodesPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  if (DV.hasRows(p)) return <DevicesTab p={p} ops={ops} feishu={feishu} />;
  return <OldNodesTab p={p} ops={ops} feishu={feishu} />;
};

/** 旧 Covey（v45 及以前）：只有节点 */
const OldNodesTab: React.FC<{ p: SettingsNodesPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const rs: Phase | undefined = ops.st['node:refresh'];
  return (
    <>
      <Top
        summary={p.summary}
        feishu={feishu}
        extra={<button type="button" className="btn" disabled={rs?.phase === 'saving'} onClick={() => void ops.run('node:refresh', { what: 'refresh' })}>刷新</button>}
      />
      <Receipt s={rs} />
      <div className="st-list">
        {p.items.length ? p.items.map((n) => <NodeRow key={n.alias} n={n} ops={ops} />) : <div className="st-r st-tr"><span className="st-faint">还没有设备</span></div>}
        {(p.joins ?? []).filter((j) => j.state !== 'done').map((j) => (
          <div key={`join-${j.alias}`} className="st-ws">
            <div className="st-wh">
              <i className={`st-dot ${j.state === 'joining' ? 'busy' : 'down'}`} aria-hidden="true" />
              <b className="st-wn">{j.alias}</b>
              <span className={`st-nl ${j.state === 'failed' ? 'st-fail' : 'st-usev'}`}>{j.state === 'joining' ? '安装中…' : `没装上：${j.message}`}</span>
            </div>
          </div>
        ))}
      </div>
      <NodeAdd can={p.can_add !== false} ops={ops} />
    </>
  );
};

/* ---------- 代答（v15）：开关、登录状态、问题、模型几行、白名单 ---------- */
const ContactRow: React.FC<{ c: SettingsProxyContact; ops: Ops }> = ({ c, ops }) => {
  const [ask, setAsk] = useState<boolean>(false);
  const id = `contact:${c.key}`;
  const s: Phase | undefined = ops.st[id];
  const busy: boolean = s?.phase === 'saving';
  return (
    <div className="st-ws">
      <div className="st-wh">
        <b className="st-wn">{c.name}</b>
        <span className="st-tag">{c.kind}</span>
        {c.from ? <span className="st-faint">来自 {c.from}</span> : null}
        {c.paused ? <Tag tone="neutral" text="已暂停" /> : null}
        {!ask ? (
          <span className="st-end st-acts2">
            <button type="button" className="st-link" disabled={busy} onClick={() => void ops.run(id, { what: 'proxy', op: c.paused ? 'resume' : 'pause', contact: c.key })}>{c.paused ? '恢复' : '暂停'}</button>
            <button type="button" className="st-link danger" disabled={busy} onClick={() => setAsk(true)}>移除</button>
          </span>
        ) : null}
      </div>
      {ask ? <Confirm text={`把 ${c.name} 从白名单里移除？`} ok="确认移除" busy={busy} onOk={() => { setAsk(false); void ops.run(id, { what: 'proxy', op: 'remove', contact: c.key }); }} onNo={() => setAsk(false)} /> : null}
      <Receipt s={s} />
    </div>
  );
};

const ProxyTab: React.FC<{ p: SettingsProxyPage; ops: Ops; feishu: string }> = ({ p, ops, feishu }) => {
  const s: Phase | undefined = ops.st['proxy:toggle'];
  const probs: string[] = p.problems ?? [];
  return (
    <>
      <Top
        summary={p.summary}
        feishu={feishu}
        extra={
          <button type="button" className={`btn${p.enabled ? '' : ' pri'}`} disabled={s?.phase === 'saving'} onClick={() => void ops.run('proxy:toggle', { what: 'proxy', op: p.enabled ? 'disable' : 'enable' })}>
            {p.enabled ? '关闭代答' : '开启代答'}
          </button>
        }
      />
      <Receipt s={s} />
      <div className="st-login">
        <i className={`st-dot ${p.login_ok ? 'ok' : 'tight'}`} aria-hidden="true" />
        <span className={p.login_ok ? 'st-usev' : 'st-warn'}>{p.login_ok ? '网页登录正常' : '网页登录需要在飞书代答卡上处理'}</span>
        {p.login_ok ? null : <FeishuBtn feishu={feishu} link />}
      </div>
      {probs.length ? <ul className="st-problems">{probs.map((t: string, i: number) => <li key={i}>{t}</li>)}</ul> : null}
      <Rows rows={p.rows} ops={ops} feishu={feishu} mk={(row: SettingsRow, option: string) => ({ what: 'proxy', op: 'row', kind: row.kind, option })} />
      <h3 className="st-h">白名单</h3>
      <div className="st-list">
        {p.contacts.length ? p.contacts.map((c) => <ContactRow key={c.key} c={c} ops={ops} />) : <div className="st-r st-tr"><span className="st-faint">白名单是空的</span></div>}
      </div>
      <Note text="按名字查找添加联系人、扫码登录请到飞书代答卡上做。" />
    </>
  );
};

/** 自托管构建才有的“已登录的浏览器”（妙搭上不渲染，也不会加载这一块） */
const SessionsPanel = lazy(() => import('@client/src/selfhost/SessionsPanel'));

const Body: React.FC<{ data: SettingsData; tab: string; ops: Ops }> = ({ data, tab, ops }) => {
  const page = (data as unknown as Record<string, unknown>)[tab];
  const f: string = data.feishu;
  if (isErr(page)) return <ErrorPage error={page.error} feishu={f} />;
  if (!page) return <ErrorPage error="这一页 Covey 还没推过来" feishu={f} />;
  switch (tab) {
    case 'config': return <ConfigTab p={page as SettingsConfigPage} ops={ops} feishu={f} />;
    case 'topic': return <TopicTab p={page as SettingsTopicPage} ops={ops} feishu={f} />;
    case 'main': return <MainTab p={page as SettingsMainPage} ops={ops} feishu={f} />;
    case 'repos': return <ReposTab p={page as SettingsReposPage} ops={ops} feishu={f} />;
    case 'workspace': return <WorkspaceTab p={page as SettingsWorkspacePage} ops={ops} feishu={f} />;
    case 'groups': return <GroupsTab p={page as SettingsGroupsPage} ops={ops} feishu={f} />;
    case 'nodes': return <NodesTab p={page as SettingsNodesPage} ops={ops} feishu={f} />;
    case 'proxy': return <ProxyTab p={page as SettingsProxyPage} ops={ops} feishu={f} />;
    default: return <ErrorPage error="这一页网页还不会画" feishu={f} />;
  }
};

/** 「记忆」页签（v37）：Covey 推了主记忆（key memory）且开着才有，排在「配置」后面；数据不在 settings 条目里，自己取 */
const MEMORY_TAB = { key: 'memory', label: '记忆' };

const SettingsPage: React.FC = () => {
  const { tab } = useParams<{ tab: string }>();
  const q = useSettings();
  const mem = useMemoryMode();
  const ops: Ops = useOps();
  const snap = q.data ?? null;
  // v43：记忆搬到了记忆中心（/memory），设置页不再有「记忆」页签；旧 Covey（只推 v37 的 memory）照旧
  const memoryOn: boolean = mem.mode === 'v37';
  // 「更新」（v39）：Covey 认 upgrade 操作、也推过版本条目才有，排在最后
  const upgradable: boolean = canDo(snap?.data.ops, 'upgrade');
  const up = useUpgrade(upgradable);
  const upgradeShown: boolean = upgradeOn(snap?.data.ops, up.data);
  // v43：Covey 推了 repos 就把「工作区」换成「仓库」（旧 Covey 照旧）
  const covTabs: { key: string; label: string }[] = snap ? RP.tabsWithRepos(snap.data.tabs, snap.data) : [];
  const base: { key: string; label: string }[] = snap
    ? (memoryOn ? [...covTabs.slice(0, 1), MEMORY_TAB, ...covTabs.slice(1)] : covTabs)
    : [];
  // 「账号与额度」（v42）：Covey 认 quota_refresh 操作、也推了来源才有，排在「更新」前
  const quotable: boolean = canDo(snap?.data.ops, 'quota_refresh');
  const qt = useQuota(quotable);
  const quotaShown: boolean = quotaOn(snap?.data.ops, qt.data);
  const withQuota: { key: string; label: string }[] = quotaShown ? [...base, QUOTA_TAB] : base;
  const tabs: { key: string; label: string }[] = upgradeShown ? [...withQuota, UPGRADE_TAB] : withQuota;
  const qMark: boolean = quotaShown && !!capsuleOf(qt.data?.data);
  const mark: boolean = !!upgradeMark(up.data?.data);
  const first: string | undefined = tabs[0]?.key;
  // 「记忆」「更新」要等各自的数据取回来再判断有没有，免得刷新时先被跳到第一页
  // 仓库模式下旧地址 /settings/workspace 跳到 /settings/repos
  const toRepos: string | null = snap ? RP.repoRedirect(tab, tabs) : null;
  if (toRepos) return <Navigate to={`/settings/${toRepos}`} replace />;
  if (tab === MEMORY_TAB.key && mem.mode === 'v43') return <Navigate to="/memory" replace />;
  const waiting: boolean = (tab === MEMORY_TAB.key && mem.mode === 'loading') || (tab === UPGRADE_TAB.key && upgradable && up.isLoading) || (tab === QUOTA_TAB.key && quotable && qt.isLoading);
  if (snap && first && !waiting && (!tab || !tabs.some((t) => t.key === tab))) return <Navigate to={`/settings/${first}`} replace />;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <Head title="设置" />
      <div className="st-wrap">
        <div className={`st-in${tab === 'config' || tab === MEMORY_TAB.key || tab === QUOTA_TAB.key ? ' wide' : ''}`}>
          {q.isLoading ? <p className="st-faint">读取中…</p> : snap ? (
            <>
              <nav className="st-tabs" aria-label="设置分页">
                {tabs.map((t) => (
                  <NavLink key={t.key} to={`/settings/${t.key}`} className={({ isActive }) => (isActive ? 'on' : '')}>
                    {t.label}
                    {t.key === UPGRADE_TAB.key && mark ? <i className="up-tabdot" aria-label="有新版本" /> : null}
                    {t.key === QUOTA_TAB.key && qMark ? <i className="q-tabdot" aria-label="有号要处理" /> : null}
                  </NavLink>
                ))}
              </nav>
              {tab === MEMORY_TAB.key ? <MainMemory /> : tab === UPGRADE_TAB.key ? <UpgradePanel /> : tab === QUOTA_TAB.key ? <QuotaPanel /> : <Body data={snap.data} tab={tab ?? ''} ops={ops} />}
            </>
          ) : (
            <div className="st-empty">
              <p>Covey 还没推送设置（需要部署新版 Covey）</p>
              <FeishuBtn feishu="" />
            </div>
          )}
          {SELFHOST ? <Suspense fallback={null}><SessionsPanel /></Suspense> : null}
        </div>
      </div>
    </div>
  );
};
export default SettingsPage;

import React, { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import type { QuotaAccount, QuotaData, QuotaPool, QuotaSource } from '@shared/api.interface';
import { newOpId, useQuota } from '@client/src/hooks/use-workbench';
import { useOutboxOp, type OutboxOp } from './sendOp';
import { useTick } from './ui';
import {
  accountActive,
  accountWarn,
  avgCell,
  cellOf,
  groupNote,
  isOpen,
  nameOf,
  readWords,
  reloginLine,
  rowNote,
  sourceAlert,
  sourceWarn,
  usableTitle,
  type Cell,
} from './quota';
import './quota.css';

/* 设置页「账号与额度」页签（数据约定 v42，样张 F）：一张表，账号｜5 小时剩余｜周剩余，格里只有百分比、细条、时刻。
 * agy 展开（一个号一行，只画这个号近 7 天跑过的模型池）；网关和外接来源折成一行平均值，点开看每个号。只有一个操作：右上角「刷新」。 */

/** 表头的悬停：一格的数和来源那一行的平均怎么来的 */
const HEAD_TITLE: string =
  '每个号这个窗口还剩多少（100% 减去已用）；同一种窗口有几个（周、Fable 周）取剩得最少的。\n' +
  '号现在用不了（另一个窗口用完、冷却中）时这一格灰色，只是窗口本身的读数。\n' +
  '来源那一行是平均：能用的号按剩余、用不了的号按 0、读不到的不算，悬停看算式。';

const Bar: React.FC<{ c: Cell }> = ({ c }) => {
  if (c.kind === 'none') return null;
  const cls: string = c.kind === 'out' ? ' out' : c.tight ? ' tight' : '';
  return (
    <span className={`qb${cls}${c.stale || c.off ? ' stale' : ''}`} role="img" aria-label={c.kind === 'out' ? '用完' : `剩 ${c.pct}%`}>
      <i style={{ width: `${c.pct ?? 0}%` }} />
    </span>
  );
};

const CellView: React.FC<{ c: Cell; lab: string; pool?: string; dot?: boolean }> = ({ c, lab, pool, dot }) => (
  <div className={`mq-c${c.kind === 'out' ? ' out' : c.tight ? ' tight' : ''}${c.stale ? ' stale' : ''}${c.off ? ' off' : ''}${c.kind === 'none' ? ' none' : ''}`} title={c.title || undefined}>
    {pool ? <span className="pool">{dot ? <i className="mq-pdot" aria-label="在用" /> : null}{pool}</span> : null}
    <span className="lab">{lab}</span>
    <span className="n">{c.kind === 'none' ? '—' : c.kind === 'out' ? '用完' : `${c.pct}%`}</span>
    <span className="b"><Bar c={c} /></span>
    <span className="t">{c.at}</span>
  </div>
);

/** 一个号在一列里的格子：一个池一格，两个池同一格上下两行（池名在上，蓝点标在在用的那个池名前） */
const Cells: React.FC<{ s: QuotaSource; a: QuotaAccount; kind: '5h' | '7d'; now: number }> = ({ s, a, kind, now }) => {
  const lab: string = kind === '5h' ? '5 小时' : '周';
  if (!a.pools.length) return <div className="mq-c none"><span className="lab">{lab}</span><span className="n">—</span></div>;
  if (a.pools.length === 1) return <CellView c={cellOf(s, a.pools[0], kind, now, a)} lab={lab} />;
  return (
    <div className="mq-two">
      {a.pools.map((p: QuotaPool) => <CellView key={p.key} c={cellOf(s, p, kind, now, a)} lab={lab} pool={p.label} dot={p.active} />)}
    </div>
  );
};

const AccountRow: React.FC<{ s: QuotaSource; a: QuotaAccount; now: number; sub?: boolean }> = ({ s, a, now, sub }) => {
  const note: string = rowNote(a);
  const red: boolean = a.state === 'login_required' || (s.kind !== 'agy' && accountWarn(s, a, now));
  const mark: string = red ? 'reddot' : accountActive(a) ? 'dot' : 'nodot';
  return (
    <div className={`mq-row${sub ? ' mq-sub' : ''}${note ? ' login' : ''}`}>
      <div className="mq-name">
        <i className={mark} aria-label={red ? '有事' : accountActive(a) ? '在用' : undefined} />
        <span title={a.login ? a.name : undefined}>{nameOf(a)}</span>
      </div>
      {note ? <span className="msg">{note}</span> : (
        <>
          <Cells s={s} a={a} kind="5h" now={now} />
          <Cells s={s} a={a} kind="7d" now={now} />
        </>
      )}
    </div>
  );
};

const SourceBlock: React.FC<{ s: QuotaSource; now: number }> = ({ s, now }) => {
  const [picked, setPicked] = useState<boolean | undefined>(undefined);
  const open: boolean = s.kind === 'agy' || isOpen(s, picked);
  const foldable: boolean = s.kind !== 'agy';
  const note: string = groupNote(s, open);
  const alert: string = sourceAlert(s, now);
  const warn: boolean = sourceWarn(s, now);
  const sums: boolean = foldable && (!open || s.kind === 'gateway') && s.accounts.length > 0;
  const toggle = (): void => setPicked(!open);
  return (
    <>
      <div
        className={`mq-row mq-grp${foldable ? ' mq-fold' : ''}`}
        {...(foldable ? { role: 'button', tabIndex: 0, 'aria-expanded': open, onClick: toggle, onKeyDown: (e: React.KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } } } : {})}
      >
        <div className="mq-name">
          {foldable ? <ChevronRight className={`mq-chev${open ? ' open' : ''}`} aria-hidden="true" /> : null}
          <b>{s.title}</b>
          {foldable && warn ? <i className="reddot mq-gdot" aria-label="有号冷却或额度偏低" /> : null}
          {note ? <small title={usableTitle(s) || undefined}>{note}</small> : null}
          {s.link ? (
            <a className="mq-link qlink" href={s.link} target="_blank" rel="noopener noreferrer" onClick={(e: React.MouseEvent) => e.stopPropagation()}>运维台↗</a>
          ) : null}
          {alert ? <small className="mq-why" role="status">{alert}</small> : null}
        </div>
        {sums ? (
          <>
            <CellView c={avgCell(s, '5h', now)} lab="5 小时" />
            <CellView c={avgCell(s, '7d', now)} lab="周" />
          </>
        ) : <><span /><span /></>}
      </div>
      {open ? s.accounts.map((a: QuotaAccount, i: number) => <AccountRow key={`${a.name}:${i}`} s={s} a={a} now={now} sub={foldable} />) : null}
    </>
  );
};

export const QuotaPanel: React.FC = () => {
  const q = useQuota();
  const now: number = useTick(30000);
  const refresh: OutboxOp = useOutboxOp('quota:refresh');
  const d: QuotaData | undefined = q.data?.data;
  if (!d) return <p className="st-faint">{q.isLoading ? '读取中…' : q.isError ? '这次没取到账号与额度，30 秒后自动再取' : 'Covey 还没推送账号与额度'}</p>;
  const said: string = refresh.line;
  const bad: boolean = refresh.phase === 'failed' || refresh.phase === 'unsent';
  const busy: boolean = refresh.busy || d.refresh.busy;
  const relogin: string = reloginLine(d);
  const read: string = readWords(d);
  return (
    <>
      <div className="mq-top">
        <h3 className="st-h">账号与额度</h3>
        <span className="upd">
          {read ? <span title="最近一次读数">{read}</span> : null}
          {read ? <span className="qsep" aria-hidden="true">·</span> : null}
          <button
            type="button"
            className="st-link"
            disabled={busy}
            title="Covey 立刻重读所有来源，agy 查一遍每个号的额度（5 分钟内只查一次）"
            onClick={() => void refresh.run({ op_id: newOpId(), kind: 'quota_refresh', payload: {} })}
          >
            {busy ? '读取中…' : '刷新'}
          </button>
        </span>
      </div>
      {said ? <div className={`st-rc${bad ? ' bad' : ''}`} role={bad ? 'alert' : 'status'}>{said}</div> : null}
      {relogin ? <div className="mq-alert" role="alert"><i />{relogin}</div> : null}
      <div className="mq">
        <div className="mq-row mq-head">
          <span>账号</span>
          <span title={HEAD_TITLE}>5 小时剩余</span>
          <span title={HEAD_TITLE}>周剩余</span>
        </div>
        {d.sources.map((s: QuotaSource) => <SourceBlock key={s.id} s={s} now={now} />)}
      </div>
    </>
  );
};

import React from 'react';
import { Link } from 'react-router-dom';
import { useQuota, useSettings } from '@client/src/hooks/use-workbench';
import { useTick } from './ui';
import { canDo } from './v34';
import { briefRows, type BriefLine, type BriefRow } from './quotaBrief';
import './boardrail.css';

/** 看板右栏的「额度」：一个来源一行，有事一件一行（颜色同侧栏胶囊和设置页的偏紧），没事一行灰字。
 *  和侧栏胶囊、设置页「账号与额度」同一份条目（同一个查询，不多发请求）；Covey 不认 quota_refresh 或一个来源都没有时整块不画。
 *  「明细」去设置页那一页。说法和算法在 quotaBrief.ts */
export const QuotaBrief: React.FC = () => {
  const ops: readonly string[] | undefined = useSettings().data?.data?.ops;
  const on: boolean = canDo(ops, 'quota_refresh');
  const q = useQuota(on);
  const now: number = useTick(30000);
  const rows: BriefRow[] = on ? briefRows(q.data?.data, now) : [];
  if (!rows.length) return null;
  return (
    <section className="nb-panel nb-quota">
      <h3 className="nb-ph">额度<Link className="nb-more" to="/settings/quota">明细 ›</Link></h3>
      {rows.map((r: BriefRow) => (
        <div className="nq-src" key={r.id}>
          <div className="nq-h"><b>{r.title}</b>{r.count ? <span>{r.count}</span> : null}</div>
          {r.lines.map((l: BriefLine, i: number) => <div key={i} className={`nq-l ${l.tone}`}>{l.text}</div>)}
        </div>
      ))}
    </section>
  );
};

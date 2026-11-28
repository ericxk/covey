import React, { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { ChevronRight, Sparkles } from 'lucide-react';
import type { SnapshotResponse, SnapshotTopic } from '@shared/api.interface';
import { prefetchTurns } from '@client/src/hooks/use-workbench';
import { RoleAvatar, TopicBadge } from './RoleAvatar';
import { pref, setPref } from './lib';
import { Dot } from './ui';
import type { Asks } from './projectData';
import { shownRows, sinceWords, statusGroups, type StatusRow } from './statusGroups';

/** 鼠标移上去（手机按下）就先取这个话题的对话，点下去时多半已经在了（同侧栏话题行 TopicRow）；专项行没有对话可取 */
const Row: React.FC<{ r: StatusRow; now: number; on: boolean }> = ({ r, now, on }) => {
  const qc = useQueryClient();
  const warm = r.kind === 'project' || r.kind === 'tidy' ? undefined : () => prefetchTurns(qc, r.key);
  return (
  <Link to={r.to} className={`ss-row${on ? ' on' : ''}`} title={`${r.title}\n${r.sub}`} onMouseEnter={warm} onTouchStart={warm}>
    {r.kind === 'topic' ? (
      <span className="a-tlogo">
        <TopicBadge number={r.number} />
        <Dot tone={r.tone} />
      </span>
    ) : r.kind === 'tidy' ? (  // 整理专项的建议：同看板上「整理」按钮的图标
      <span className="ss-av">
        <Sparkles className="a-avatar" aria-hidden="true" width={18} height={18} />
        <Dot tone={r.tone} />
      </span>
    ) : (
      <span className="ss-av">
        <RoleAvatar name={r.kind === 'butler' ? 'butler' : r.avatar} size={18} />
        <Dot tone={r.tone} />
      </span>
    )}
    <span className="ss-t">{r.title}</span>
    <span className="ss-m">{r.plan ?? (r.since ? sinceWords(Math.max(0, now - r.since)) : '')}</span>
    <span className="ss-s">{r.sub}</span>
  </Link>
  );
};

/** 侧栏顶上的「要你处理」「在做」两组（statusGroups.ts）。两组都空时什么也不画。
 *  每组收着时最多列 SHOWN 行（多出 1 个就直接列出，shownRows）：要你处理多的链到看板；在做多的写“还有 N 个在做 · T 号”，
 *  点开列全、再点「收起」，展开与否记在这台浏览器上（同“更早的专项”）。「要你处理」旁的数就是它的行数（和侧栏角标同一个数） */
export const StatusGroups: React.FC<{ a: Asks; data: SnapshotResponse; running: SnapshotTopic[]; now: number }> = ({ a, data, running, now }) => {
  const loc = useLocation();
  const [runAll, setRunAll] = useState<boolean>(() => pref('ss.running', '0') === '1');
  const g = statusGroups(a, data, running);
  if (!g.act.length && !g.running.length) return null;
  const on = (r: StatusRow): boolean => loc.pathname === r.to;
  const actN: number = shownRows(g.act.length, false);
  const runN: number = shownRows(g.running.length, runAll);
  const folds: boolean = shownRows(g.running.length, false) < g.running.length;
  const toggleRun = (): void => {
    setPref('ss.running', runAll ? '0' : '1');
    setRunAll(!runAll);
  };
  return (
    <div className="ss" aria-label="按状态">
      {g.act.length ? (
        <>
          <Link to="/" className="sec ss-h act" title="看板">
            <span>要你处理</span>
            <b>{g.act.length}</b>
          </Link>
          {g.act.slice(0, actN).map((r: StatusRow) => <Row key={r.key} r={r} now={now} on={on(r)} />)}
          {g.act.length > actN ? <Link to="/" className="ss-more">还有 {g.act.length - actN} 件，在看板里</Link> : null}
        </>
      ) : null}
      {g.running.length ? (
        <>
          <div className="sec ss-h">
            <span>在做</span>
            <b>{g.running.length}</b>
          </div>
          {g.running.slice(0, runN).map((r: StatusRow) => <Row key={r.key} r={r} now={now} on={on(r)} />)}
          {g.running.length > runN ? (
            <button type="button" className="ss-more" aria-expanded={false} onClick={toggleRun}>
              <span>还有 {g.running.length - runN} 个在做</span>
              <span className="ss-ids">{g.running.slice(runN).map((r: StatusRow) => (r.number ? `T${r.number}` : r.title)).join('、')}</span>
              <ChevronRight aria-hidden="true" />
            </button>
          ) : runAll && folds ? (
            <button type="button" className="ss-more" aria-expanded onClick={toggleRun}>收起</button>
          ) : null}
        </>
      ) : null}
    </div>
  );
};

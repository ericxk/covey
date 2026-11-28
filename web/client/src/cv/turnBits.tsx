import React from 'react';
import { Link } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import type { SnapshotTopic, StartedTopic, TurnNote } from '@shared/api.interface';
import { useSnapshot } from '@client/src/hooks/use-workbench';
import { harnessName, isRunning, isWaiting } from './lib';
import { MemoryWrong } from './V34Bits';

/* 一轮回答下面的小东西：记忆行、派出去的话题胶囊。对话（聊天样式）和话题时间线共用。 */
const NOTE_WORD: Record<string, string> = { remembered: '记下了', voided: '作废了', undone: '已撤销', skipped_ask: '没再问', reference: '参考了', superseded: '收起了', pending: '待审核' };

/** 这一轮记下的、作废的记忆。刚记下的那条带「不对」（v34 memory_undo，同答案卡上的不对）；Covey 不认这个操作（旧版）时照实写一行灰字 */
export const Notes: React.FC<{ notes: TurnNote[]; topicKey?: string; jobId?: string | null }> = ({ notes, topicKey, jobId }) => (
  <>
    {notes.map((n: TurnNote, i: number) => (
      <div className="mem" key={`${n.entry_id ?? ''}${i}`}>
        <Sparkles />
        <span>
          {NOTE_WORD[n.kind] ?? '记忆'}：{n.text}
          {n.scope ? ` · ${n.scope}` : ''}
        </span>
        {n.kind === 'remembered' || n.kind === 'superseded' || n.kind === 'pending' ? <MemoryWrong topicKey={topicKey ?? ''} jobId={jobId} entryId={n.entry_id} /> : null}
      </div>
    ))}
  </>
);

export const StartedChips: React.FC<{ items: StartedTopic[] }> = ({ items }) => {
  const { data } = useSnapshot();
  return (
    <div className="chips">
      {items.map((s: StartedTopic) => {
        const t: SnapshotTopic | undefined = data?.topics.find((x: SnapshotTopic) => x.key === s.key);
        const cls: string = t && isWaiting(t) ? ' wait' : t && isRunning(t) ? ' run' : '';
        return (
          <Link key={s.key} to={`/topics/${encodeURIComponent(s.key)}`} className={`chip${cls}`}>
            → <b>T{s.number}</b> {s.title}
            {s.harness ? ` · ${harnessName(s.harness)}` : ''}
          </Link>
        );
      })}
    </div>
  );
};


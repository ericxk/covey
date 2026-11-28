import React from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import type { MemoryEntry, MemoryIndex, MemoryProjectData, MemoryTopicsData } from '@shared/api.interface';
import { useMemoryIndex, useMemoryKey } from '@client/src/hooks/use-workbench';
import { briefLine, briefOf, codeHref, type Brief } from './memoryCenter';
import './memorycenter.css';

/* 专项页、话题页右栏的精简记忆（数据约定 v43、v45）：一行“这个专项的规矩 N 条 ›”（点了去记忆页那一组），
   下面至多 6 条规矩，你定的在前，每条点了去记忆页那一条；超额只带标题的标一个小“只带标题”。不列决定，不写资料数。
   没收到 memory:index（旧 Covey）时 useV43ProjectEntries / useV43TopicEntries 返回 null，调用方照旧画 v37 的面板。 */

/** 专项 id 的 v43 条目：index 里有 memory:p:<id> 就取，取到前是 undefined；不是 v43（或没这个专项的键）是 null */
export function useV43ProjectEntries(projectKey: string | null | undefined): MemoryEntry[] | null | undefined {
  const idx = useMemoryIndex();
  const index: MemoryIndex | null = idx.data?.data ?? null;
  const key: string = projectKey ? `memory:p:${projectKey}` : '';
  const rev: string | undefined = key ? index?.revs[key] : undefined;
  const q = useMemoryKey<MemoryProjectData>(rev ? key : null, rev);
  if (!index || !index.on || !rev) return idx.isLoading ? undefined : null;
  return q.data?.data?.entries ?? (q.isLoading ? undefined : []);
}

/** 话题的 v43 条目：从 memory:topics 里挑这个话题的（近 30 天有记忆的话题才在里面；不在里面是空数组） */
export function useV43TopicEntries(topicKey: string | null | undefined): MemoryEntry[] | null | undefined {
  const idx = useMemoryIndex();
  const index: MemoryIndex | null = idx.data?.data ?? null;
  const rev: string | undefined = index?.revs['memory:topics'];
  const q = useMemoryKey<MemoryTopicsData>(rev ? 'memory:topics' : null, rev);
  if (!index || !index.on) return idx.isLoading ? undefined : null;
  if (!rev) return [];
  const t = q.data?.data?.topics.find((x) => x.topic_key === topicKey);
  return t ? t.entries : q.isLoading ? undefined : [];
}

/** 精简清单：scope 是记忆页的范围（p:<专项 id> 或 topics）。一行“这个专项的规矩 N 条 ›”，下面至多 6 条规矩（你定的在前），不列决定、不写资料数 */
export const MemoryList: React.FC<{ entries: MemoryEntry[]; scope: string }> = ({ entries, scope }) => {
  const b: Brief = briefOf(entries);
  const href: string = `/memory?scope=${encodeURIComponent(scope)}`;
  return (
    <div className="ml" data-testid="memory-list">
      <Link to={href} className="ml-head" title="在记忆页看全部">
        <span>{briefLine(b, scope)}</span>
        <ChevronRight />
      </Link>
      {b.rules.map((e: MemoryEntry) => (
        <Link key={e.key} to={codeHref(e.code)} className="ml-row" title={e.text}>
          <span className="ml-t">{e.title}</span>
          {e.over_cap ? <span className="mc-off short">只带标题</span> : null}
        </Link>
      ))}
    </div>
  );
};

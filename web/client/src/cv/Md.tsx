import React, { useMemo } from 'react';
import { defaultUrlTransform } from 'react-markdown';
import { Link } from 'react-router-dom';
import { Markdown } from '@/components/ui/markdown';
import type { SnapshotTopic } from '@shared/api.interface';
import { useTopicByNumber } from '@client/src/hooks/use-workbench';
import { MdTable } from './MdTable';
import { CodeBlock, RichBody } from './RichBody';
import { hasRich, richBlocks, type RichBlock } from './richBlocks';

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}
const SKIP: readonly string[] = ['code', 'inlineCode', 'link', 'linkReference', 'definition', 'html'];
const T_NUMBER = /\bT(\d{1,6})\b/gu;
const SCHEME = 'covey-topic:';

/** 把正文里的 T12 变成指向那个话题的链接（快照里查得到的才变；代码块不转） */
function linkify(node: MdNode, byNumber: Map<number, SnapshotTopic>): void {
  if (!node.children || SKIP.includes(node.type)) return;
  const out: MdNode[] = [];
  for (const child of node.children) {
    if (child.type !== 'text' || !child.value) {
      linkify(child, byNumber);
      out.push(child);
      continue;
    }
    const text: string = child.value;
    let last = 0;
    for (const m of text.matchAll(T_NUMBER)) {
      const t: SnapshotTopic | undefined = byNumber.get(Number(m[1]));
      if (!t || m.index === undefined) continue;
      if (m.index > last) out.push({ type: 'text', value: text.slice(last, m.index) });
      out.push({ type: 'link', url: `${SCHEME}${encodeURIComponent(t.key)}`, children: [{ type: 'text', value: m[0] }] });
      last = m.index + m[0].length;
    }
    if (last === 0) out.push(child);
    else if (last < text.length) out.push({ type: 'text', value: text.slice(last) });
  }
  node.children = out;
}

/** 你说的话（T616）：单个换行保留成换行（Markdown 本来把它并进一段），代码里的不动 */
function softBreaks(node: MdNode): void {
  if (!node.children || node.type === 'code' || node.type === 'inlineCode') return;
  const out: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === 'text' && child.value?.includes('\n')) {
      child.value.split(/\r?\n/u).forEach((part: string, i: number) => {
        if (i) out.push({ type: 'break' });
        if (part) out.push({ type: 'text', value: part });
      });
      continue;
    }
    softBreaks(child);
    out.push(child);
  }
  node.children = out;
}

const urlTransform = (url: string): string => (url.startsWith(SCHEME) ? url : defaultUrlTransform(url));

// 组件表放在模块里、引用不变：每秒的快照重画时，表格、代码块不被当成新组件卸载重建（打开的放大弹层、点开的折叠、高亮都留着）
const COMPONENTS = {
  // 宽表格：横向滚动框、右缘渐隐、放大弹层、复制
  table: MdTable,
  // 代码块：语言标签、复制、diff 按行着色、按需高亮（T943）
  pre: CodeBlock,
  a: ({ href, children }: { href?: string; children?: React.ReactNode }) => {
    if (href?.startsWith(SCHEME)) {
      const key: string = decodeURIComponent(href.slice(SCHEME.length));
      // 真实的路由地址：能新标签打开、能复制链接；普通点击由路由接管
      return <Link to={`/topics/${encodeURIComponent(key)}`}>{children}</Link>;
    }
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
} as const;

/** said：你说的话（气泡、时间线里你说的那行、输入框的预览），换行照写的保留，字号和留白收紧；不认富文本块（贴进来的日志不变样）。
 * 回答里的富文本块（提示框、折叠块、指标条，T943）切开来画，块里的 Markdown 还是这里画 */
const MdInner: React.FC<{ body: string; dim?: boolean; said?: boolean }> = ({ body, dim, said }) => {
  const byNumber = useTopicByNumber();
  const plugin = useMemo(
    () => () => (tree: MdNode) => {
      linkify(tree, byNumber);
      if (said) softBreaks(tree);
    },
    [byNumber, said],
  );
  const blocks: RichBlock[] | null = useMemo(() => (said || !hasRich(body) ? null : richBlocks(body)), [body, said]);
  const render = (text: string): React.ReactNode => (
    <Markdown className="break-words" remarkPlugins={[plugin]} urlTransform={urlTransform} components={COMPONENTS}>
      {text}
    </Markdown>
  );
  return (
    <div className={said ? 'mdans mdsaid' : 'mdans'} style={dim ? { opacity: 0.75 } : undefined}>
      {blocks ? <RichBody blocks={blocks} render={render} /> : render(body)}
    </div>
  );
};
export const Md = React.memo(MdInner);

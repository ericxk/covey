import React, { useMemo } from 'react';
import { defaultUrlTransform } from 'react-markdown';
import { Link } from 'react-router-dom';
import { Markdown } from '@/components/ui/markdown';
import type { SnapshotTopic } from '@shared/api.interface';
import { useTopicByNumber } from '@client/src/hooks/use-workbench';
import { MdTable } from './MdTable';
import { mathNodes, protectMath } from './mathText';
import { mdMathComponents, preloadMath } from './MdMath';
import './math.css';

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

/** said：你说的话（气泡、时间线里你说的那行、输入框的预览），换行照写的保留，字号和留白收紧 */
const MdInner: React.FC<{ body: string; dim?: boolean; said?: boolean }> = ({ body, dim, said }) => {
  const byNumber = useTopicByNumber();
  const plugin = useMemo(
    () => () => (tree: MdNode) => {
      mathNodes(tree);
      linkify(tree, byNumber);
      if (said) softBreaks(tree);
    },
    [byNumber, said],
  );
  // 数学公式（$…$、$$…$$、\(…\)、\[…\]）先换成 Markdown 不会动的行内代码，见 mathText.ts
  const source: string = useMemo(() => protectMath(body), [body]);
  // 有公式才去取 KaTeX（懒加载，没有公式的页面不下载它）
  if (source !== body) void preloadMath();
  return (
    <div className={said ? 'mdans mdsaid' : 'mdans'} style={dim ? { opacity: 0.75 } : undefined}>
      <Markdown
        className="break-words"
        remarkPlugins={[plugin]}
        urlTransform={(url: string) => (url.startsWith(SCHEME) ? url : defaultUrlTransform(url))}
        components={{
          // 宽表格：横向滚动框、右缘渐隐、放大弹层。直接给组件本身：包一层箭头函数的话每次重绘都是新组件，
          // 表格会被卸载重建，打开的放大弹层也跟着没了
          table: MdTable,
          // 公式：mathText.ts 标出的 span/div.cv-math 换成 KaTeX 排好的（MdMath.tsx，懒加载），别的 span/div 照旧
          ...mdMathComponents,
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
        }}
      >
        {source}
      </Markdown>
    </div>
  );
};
export const Md = React.memo(MdInner);

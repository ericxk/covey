import React, { useEffect, useState } from 'react';
import { Check, Copy, Info, Lightbulb, MessageSquareWarning, OctagonAlert, TriangleAlert } from 'lucide-react';
import { DETAILS_WORD, TONE_WORDS, hasRich, richBlocks, type RichBlock, type Tone } from './richBlocks';
import './rich.css';

/**
 * 富文本块的样子（covey docs/ui.md 富文本块，T943）。切块在 richBlocks.ts（和 Bridge 一样的规则），这里只画：
 * 提示框是左边一条细色条、图标加类型词，不套底色；折叠块是原生 <details>（点开的状态由浏览器记着，每秒的快照重画不会把它收起来）；
 * 指标条是一行等宽的小格，手机上两列。块里的普通 Markdown 交回 Md 画（render），不开 rehype-raw，不用 dangerouslySetInnerHTML。
 */

const TONE_ICONS: Record<Tone, React.FC<{ className?: string }>> = {
  note: Info,
  tip: Lightbulb,
  important: MessageSquareWarning,
  warning: TriangleAlert,
  caution: OctagonAlert,
};

/** 标题里的 Markdown 只认行内代码，粗体记号去掉（标题本来就加粗） */
const Inline: React.FC<{ text: string }> = ({ text }) => (
  <>
    {text
      .replace(/\*\*/gu, '')
      .split(/(`[^`]+`)/u)
      .map((part: string, i: number) => (part.startsWith('`') && part.endsWith('`') && part.length > 2 ? <code key={i}>{part.slice(1, -1)}</code> : part))}
  </>
);

/** 标题只是把类型词再说一遍（`> [!WARNING] 注意`）就不再写（和 Bridge 的 rich_md.shown_title 一样） */
const sameWord = (title: string, tone: Tone): boolean =>
  [TONE_WORDS[tone], tone].includes(title.replace(/^[*：:\s]+|[*：:\s]+$/gu, '').toLowerCase());

/** 块里的正文：还有块（提示框里的折叠、折叠里的提示框、指标）就再切一层画，最多两层，不露原样的标记 */
const Inner: React.FC<{ text: string; render: (md: string) => React.ReactNode; depth: number }> = ({ text, render, depth }) => {
  const blocks: RichBlock[] | null = React.useMemo(() => (depth < 2 && hasRich(text) ? richBlocks(text) : null), [text, depth]);
  return <>{blocks ? <RichBody blocks={blocks} render={render} depth={depth + 1} /> : render(text)}</>;
};

export const RichBody: React.FC<{ blocks: RichBlock[]; render: (md: string) => React.ReactNode; depth?: number }> = ({ blocks, render, depth = 0 }) => (
  <div className="mdr">
    {blocks.map((b: RichBlock, i: number) => {
      if (b.kind === 'callout') {
        const tone: Tone = b.tone ?? 'note';
        const Icon = TONE_ICONS[tone];
        return (
          <aside key={i} className={`mdc mdc-${tone}`} role="note">
            <div className="mdc-h">
              <Icon className="mdc-i" />
              <span className="mdc-w">{TONE_WORDS[tone]}</span>
              {b.title && !sameWord(b.title, tone) ? (
                <span className="mdc-t">
                  <Inline text={b.title} />
                </span>
              ) : null}
            </div>
            {b.body ? (
              <div className="mdc-b">
                <Inner text={b.body} render={render} depth={depth} />
              </div>
            ) : null}
          </aside>
        );
      }
      if (b.kind === 'details') {
        return (
          <details key={i} className="mdd" open={b.open || undefined}>
            <summary>
              <Inline text={b.title || DETAILS_WORD} />
            </summary>
            <div className="mdd-b">{b.body ? <Inner text={b.body} render={render} depth={depth} /> : null}</div>
          </details>
        );
      }
      if (b.kind === 'metrics') {
        const items = b.items ?? [];
        return (
          <dl key={i} className="mdm" data-n={items.length <= 4 ? items.length : items.length <= 6 ? 3 : 4} aria-label="指标">
            {items.map(([label, value, delta], k: number) => (
              <div key={k} className="mdm-i">
                <dt>{label}</dt>
                <dd className="mdm-v">{value}</dd>
                {delta ? <dd className="mdm-d">{delta}</dd> : null}
              </div>
            ))}
          </dl>
        );
      }
      return <React.Fragment key={i}>{render(b.source)}</React.Fragment>;
    })}
  </div>
);

/* ---------- 代码块：语言标签、复制、diff 按行着色、其余语言按需高亮 ---------- */

interface HastNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: { className?: string[] | string };
  children?: HastNode[];
}
interface Low {
  registered: (lang: string) => boolean;
  highlight: (lang: string, code: string) => HastNode;
}

// 高亮器（lowlight 的常用语言）用到才加载，单独一个分块，首屏不带它
let lowLoad: Promise<Low> | null = null;
const loadLow = (): Promise<Low> => {
  lowLoad ??= import('lowlight').then((m) => m.createLowlight(m.common) as unknown as Low);
  return lowLoad;
};
// 高亮过的（语言 + 代码 → 结果）记着：每秒的快照重画、换话题再回来都不用再算，也不闪
const lit = new Map<string, HastNode>();
const LIT_MAX = 300;

function toReact(node: HastNode, key: React.Key): React.ReactNode {
  if (node.type === 'text') return node.value ?? '';
  const kids: React.ReactNode[] = (node.children ?? []).map((c: HastNode, i: number) => toReact(c, i));
  if (node.type !== 'element') return <React.Fragment key={key}>{kids}</React.Fragment>;
  const cls = node.properties?.className;
  return (
    <span key={key} className={Array.isArray(cls) ? cls.join(' ') : cls}>
      {kids}
    </span>
  );
}

// 这些不高亮（diff 自己按行着色，纯文本没什么可亮的），也不为它们去加载高亮器
const PLAIN = new Set(['', 'diff', 'patch', 'text', 'txt', 'plaintext', 'plain']);

/** 高亮结果和它那份代码绑在一起：同一位置换了代码（换语言、换成纯文本）时旧结果不会画到新代码上 */
function useLit(lang: string, code: string): HastNode | null {
  const key = `${lang}\u0000${code}`;
  const [made, setMade] = useState<{ key: string; tree: HastNode } | null>(() => {
    const known: HastNode | undefined = lit.get(key);
    return known ? { key, tree: known } : null;
  });
  const skip: boolean = PLAIN.has(lang) || code.length > 60_000;
  useEffect(() => {
    if (skip) return undefined;
    const known: HastNode | undefined = lit.get(key);
    if (known) {
      setMade({ key, tree: known });
      return undefined;
    }
    let live = true;
    loadLow()
      .then((low: Low) => {
        if (!live || !low.registered(lang)) return;
        const tree: HastNode = low.highlight(lang, code);
        if (lit.size >= LIT_MAX) lit.delete(lit.keys().next().value as string);
        lit.set(key, tree);
        setMade({ key, tree });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [key, lang, code, skip]);
  return made && made.key === key && !skip ? made.tree : lit.get(key) ?? null;
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 落到下面的老办法
  }
  try {
    const area: HTMLTextAreaElement = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok: boolean = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 复制按钮：点了说“已复制”两秒 */
export const CopyButton: React.FC<{ text: () => string; label: string; className?: string }> = ({ text, label, className }) => {
  const [done, setDone] = useState<boolean>(false);
  useEffect(() => {
    if (!done) return undefined;
    const t: number = window.setTimeout(() => setDone(false), 2000);
    return () => window.clearTimeout(t);
  }, [done]);
  return (
    <button
      type="button"
      className={className ?? 'mdk-copy'}
      title={label}
      aria-label={label}
      onClick={(e: React.MouseEvent) => {
        e.stopPropagation();
        void copyText(text()).then((ok: boolean) => setDone(ok));
      }}
    >
      {done ? <Check /> : <Copy />}
      <span>{done ? '已复制' : '复制'}</span>
    </button>
  );
};

const DIFF = new Set(['diff', 'patch']);

function diffClass(line: string): string | undefined {
  if (line.startsWith('+++') || line.startsWith('---')) return 'dl-file';
  if (line.startsWith('+')) return 'dl-add';
  if (line.startsWith('-')) return 'dl-del';
  if (line.startsWith('@@')) return 'dl-hunk';
  return undefined;
}

const CodeBody: React.FC<{ lang: string; code: string }> = ({ lang, code }) => {
  const tree: HastNode | null = useLit(lang, code);
  if (DIFF.has(lang)) {
    return (
      <code>
        {code.split('\n').map((line: string, i: number) => (
          <span key={i} className={diffClass(line)}>
            {line}
            {'\n'}
          </span>
        ))}
      </code>
    );
  }
  return <code className={tree ? 'hljs' : undefined}>{tree ? toReact(tree, 'root') : code}</code>;
};

function textOf(node: HastNode | undefined): string {
  if (!node) return '';
  if (node.type === 'text') return node.value ?? '';
  return (node.children ?? []).map(textOf).join('');
}

/** Markdown 的 <pre>：语言标签和复制在右上角一行，diff 一行一色，认得的语言按需高亮 */
export const CodeBlock: React.FC<{ node?: HastNode; children?: React.ReactNode }> = ({ node, children }) => {
  const inner: HastNode | undefined = node?.children?.find((c: HastNode) => c.type === 'element' && c.tagName === 'code');
  if (!inner) return <pre>{children}</pre>;
  const cls = inner.properties?.className;
  const named: string | undefined = (Array.isArray(cls) ? cls : [cls ?? '']).find((c: string) => c.startsWith('language-'));
  const lang: string = named ? named.slice('language-'.length).toLowerCase() : '';
  const code: string = textOf(inner).replace(/\n$/u, '');
  return (
    <div className="mdk">
      <div className="mdk-h">
        <span className="mdk-l">{lang || '代码'}</span>
        <CopyButton text={() => code} label="复制代码" />
      </div>
      <pre className={DIFF.has(lang) ? 'mdk-pre mdk-diff' : 'mdk-pre'}>
        <CodeBody lang={lang} code={code} />
      </pre>
    </div>
  );
};

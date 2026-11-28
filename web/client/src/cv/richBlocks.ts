/**
 * 富文本块（covey docs/ui.md 富文本块，T943）：回答 Markdown 里除了 CommonMark / GFM 以外认的几种块，按行在顶层切开。
 * 和 Bridge 的 covey/core/rich_md.py 一行对一行（两边都读 covey tests/fixtures/rich/cases.json，切出来的块要一样），
 * 模型只写一份，飞书卡片和网页各画各的：
 * - callout：首行 `> [!NOTE]`（TIP、IMPORTANT、WARNING、CAUTION，大小写不限），后面可跟标题；引用的其余行是正文
 * - details：单独一行 `<details>`（或 `<details open>`）到它的 `</details>` 行；标题是开头那行或里面第一行的 `<summary>…</summary>`；
 *   里面的 details 摊平成“加粗标题 + 正文”，不嵌套
 * - metrics：围栏 metrics，一行一项：`标签 | 数值 | 变化` 或 `标签: 数值`（Collie 的写法）
 * - md：其余的 Markdown（代码块、表格、任务清单都在里面）
 * 块只从不缩进的行首开始（列表项里缩进的引用、围栏算列表项的），代码围栏里的不算；没闭合的围栏、details（还在写、被截断）照原样是 Markdown。
 * 纯函数、不依赖别的模块：node 测试直接转译这个文件。
 */

export type RichKind = 'md' | 'callout' | 'details' | 'metrics';
export type Tone = 'note' | 'tip' | 'important' | 'warning' | 'caution';
export type Metric = [label: string, value: string, delta: string];

export interface RichBlock {
  kind: RichKind;
  source: string;
  tone?: Tone;
  title?: string;
  body?: string;
  open?: boolean;
  items?: Metric[];
}

export const TONE_WORDS: Record<Tone, string> = { note: '说明', tip: '提示', important: '重要', warning: '注意', caution: '警告' };
export const DETAILS_WORD = '详情';

const FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/u;
const CALLOUT = /^>[ \t]?\[!(note|tip|important|warning|caution)\][ \t]*(.*)$/iu;
const QUOTE = /^ {0,3}>[ \t]?(.*)$/u;
const DETAILS_OPEN = /^<details(\s+open(?:=\S*)?)?\s*>(.*)$/iu;
const DETAILS_CLOSE = /^[ \t]*<\/details>[ \t]*$/iu;
const SUMMARY = /^[ \t]*<summary>(.*?)<\/summary>(.*)$/iu;
const TABLE_RULE = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/u;
const PIPE = /(?<!\\)\|/u;
const PAIR = /^([^:：]+?)\s*[:：]\s*(.+)$/u;

const trimNl = (s: string): string => s.replace(/^\n+|\n+$/gu, '');

function opensFence(line: string): boolean {
  const m = FENCE.exec(line);
  return !!m && !(m[2][0] === '`' && m[3].includes('`'));
}

/** 从 i 开的围栏在哪一行收；没收就是 null */
function fenceEnd(lines: string[], i: number): number | null {
  const mark: string = (FENCE.exec(lines[i]) as RegExpExecArray)[2];
  const close = new RegExp(`^ {0,3}${mark[0] === '`' ? '`' : '~'}{${mark.length},}[ \\t]*$`, 'u');
  for (let j = i + 1; j < lines.length; j++) if (close.test(lines[j])) return j;
  return null;
}

/** 从 i 开的 details 在哪一行收（里面开的算层数，围栏里的不算）；没收就是 null */
function detailsEnd(lines: string[], i: number): number | null {
  let depth = 1;
  let j = i + 1;
  while (j < lines.length) {
    if (opensFence(lines[j])) {
      const end: number | null = fenceEnd(lines, j);
      if (end === null) return null;
      j = end + 1;
      continue;
    }
    if (DETAILS_OPEN.test(lines[j])) depth += 1;
    else if (DETAILS_CLOSE.test(lines[j])) {
      depth -= 1;
      if (!depth) return j;
    }
    j += 1;
  }
  return null;
}

function cells(line: string): string[] {
  let s: string = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split(PIPE).map((c: string) => c.trim().replace(/\\\|/gu, '|'));
}

/** metrics 块每行的（标签，数值，变化）；表格分隔行和它上面的表头行不算 */
export function metricItems(body: string): Metric[] {
  const items: Metric[] = [];
  let last: number | null = null;
  body.split('\n').forEach((raw: string, n: number) => {
    const line: string = raw.trim();
    if (!line) return;
    if (TABLE_RULE.test(line)) {
      if (items.length && last === n - 1) items.pop();
      return;
    }
    let label: string;
    let value: string;
    let delta: string;
    if (line.includes('|')) {
      const c: string[] = cells(line);
      if (c.length < 2) return;
      [label, value] = [c[0], c[1]];
      delta = c.slice(2).filter(Boolean).join(' · ');
    } else {
      const m = PAIR.exec(line);
      if (!m) return;
      [label, value, delta] = [m[1].trim(), m[2].trim(), ''];
    }
    if (label && value) {
      items.push([label, value, delta]);
      last = n;
    }
  });
  return items;
}

/** 正文里的 details 写成“加粗标题 + 正文”：details 的正文里不再有 details */
export function flatten(text: string): string {
  const found: RichBlock[] = richBlocks(text);
  if (!found.some((b: RichBlock) => b.kind === 'details')) return text;
  return found
    .map((b: RichBlock) => (b.kind === 'details' ? `**${b.title || DETAILS_WORD}**\n\n${b.body ?? ''}`.trimEnd() : b.source))
    .join('\n\n');
}

/** text 的块，按顺序；块之间的 Markdown 是 md 块（不会是空白） */
export function richBlocks(text: string): RichBlock[] {
  const lines: string[] = String(text ?? '').replace(/\r\n/gu, '\n').split('\n');
  const out: RichBlock[] = [];
  let run: string[] = [];
  const flush = (): void => {
    if (run.some((l: string) => l.trim())) out.push({ kind: 'md', source: trimNl(run.join('\n')) });
    run = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line: string = lines[i];
    if (opensFence(line)) {
      const end: number | null = fenceEnd(lines, i);
      if (end === null) {
        run.push(...lines.slice(i));
        break;
      }
      const fence = FENCE.exec(line) as RegExpExecArray;
      const info: string = fence[3].trim();
      const kind: string = info && !fence[1] ? info.split(/\s+/u)[0].toLowerCase() : '';
      const items: Metric[] = kind === 'metrics' ? metricItems(lines.slice(i + 1, end).join('\n')) : [];
      if (items.length) {
        flush();
        out.push({ kind: 'metrics', source: lines.slice(i, end + 1).join('\n'), items });
      } else run.push(...lines.slice(i, end + 1));
      i = end + 1;
      continue;
    }
    const callout = CALLOUT.exec(line);
    if (callout) {
      let j = i + 1;
      while (j < lines.length && QUOTE.test(lines[j]) && !CALLOUT.test(lines[j])) j += 1;
      const body: string = trimNl(lines.slice(i + 1, j).map((row: string) => (QUOTE.exec(row) as RegExpExecArray)[1]).join('\n'));
      flush();
      out.push({ kind: 'callout', source: lines.slice(i, j).join('\n'), tone: callout[1].toLowerCase() as Tone, title: callout[2].trim(), body });
      i = j;
      continue;
    }
    const opened = DETAILS_OPEN.exec(line);
    const end: number | null = opened ? detailsEnd(lines, i) : null;
    if (opened && end !== null) {
      let rest: string = opened[2];
      let inner: string[] = lines.slice(i + 1, end);
      let title = '';
      let summary = SUMMARY.exec(rest);
      if (summary) [title, rest] = [summary[1], summary[2]];
      else if (!rest.trim()) {
        const k: number = inner.findIndex((row: string) => row.trim());
        summary = k >= 0 ? SUMMARY.exec(inner[k]) : null;
        if (summary) {
          title = summary[1];
          inner = [summary[2], ...inner.slice(k + 1)];
        }
      }
      const body: string = flatten(trimNl((rest.trim() ? [rest, ...inner] : inner).join('\n')));
      flush();
      out.push({ kind: 'details', source: lines.slice(i, end + 1).join('\n'), title: title.trim(), body, open: !!opened[1] });
      i = end + 1;
      continue;
    }
    run.push(line);
    i += 1;
  }
  flush();
  return out;
}

/** 有没有 Markdown 以外的块（先粗看一眼：大多数回答一个都没有） */
export function hasRich(text: string): boolean {
  const body = String(text ?? '');
  const low: string = body.toLowerCase();
  if (!body.includes('[!') && !low.includes('<details') && !low.includes('metrics')) return false;
  return richBlocks(body).some((b: RichBlock) => b.kind !== 'md');
}

/** 两边要对得上的部分（cases.json 的 blocks）：块的种类和它找出来的东西 */
export function shape(b: RichBlock): Record<string, unknown> {
  if (b.kind === 'callout') return { kind: b.kind, tone: b.tone, title: b.title, body: b.body };
  if (b.kind === 'details') return { kind: b.kind, title: b.title, body: b.body, open: b.open };
  if (b.kind === 'metrics') return { kind: b.kind, items: b.items };
  return { kind: b.kind, source: b.source };
}

/**
 * 回答里的数学公式（对标 Codex）：`$…$`、`\(…\)` 是行内公式，`$$…$$`、`\[…\]` 和 ```math 围栏是块级公式。
 *
 * 分两步，都不碰 KaTeX（渲染在 mathRender.ts）：
 * 1. protectMath 在交给 Markdown 之前，把认出来的公式换成一段带私有区标记的行内代码（U+E000 + i/d + 编码后的 TeX）。
 *    这样 Markdown 不会把公式里的 `_`、`*`、`|`、`\(` 当成强调、表格分隔或转义，公式原样到渲染那一步。
 * 2. mathNodes（remark 插件里调）把这种行内代码和 ```math 围栏标成公式节点：`span.cv-math` / `div.cv-math`，
 *    data-math 写 inline 或 display，文字是 TeX 原文（没有渲染组件时照样读得懂）。
 *
 * 不当公式的：围栏代码块和缩进代码块、行内代码里的；链接的网址（`[文字](网址)` 的网址、`[id]: 网址`、`<https://…>`、
 * 裸的 `https://…`、`www.…`，链接文字里的公式照常认）；`\$` 转义的；`$5`、`$10 - $20`、`$HOME`、`$PATH/$USER` 这类
 * 美元金额和变量，文件路径里夹在两个 `/` 之间的一段（`/docs/$fee$/latest`、`./$x$/a`、`/srv/$name$.txt`）
 * （行内 `$` 的规则同 pandoc：开头 `$` 后面紧跟非空白、结尾 `$` 前面紧跟非空白且后面不是数字或字母）；
 * 没闭合的（流式输出写到一半的 `$$`、`\[`）原样留着，等闭合了才变成公式，中间不报错、不打乱排版。
 */

/** 行内代码里的公式标记：私有区字符，正常文字里不会有 */
export const MATH_MARK = '\uE000';

export interface MathSpan {
  tex: string;
  display: boolean;
}

/** 公式换成的那段行内代码：编码后没有反引号、空白和 Markdown 的特殊字符 */
function carrier(tex: string, display: boolean): string {
  return `\`${MATH_MARK}${display ? 'd' : 'i'}${encodeURIComponent(tex)}\``;
}

/** 行内代码的内容是不是 protectMath 换出来的公式；是就还原 TeX */
export function decodeCarrier(value: string): MathSpan | null {
  if (value.length < 3 || value[0] !== MATH_MARK) return null;
  const mode: string = value[1];
  if (mode !== 'i' && mode !== 'd') return null;
  try {
    return { tex: decodeURIComponent(value.slice(2)), display: mode === 'd' };
  } catch {
    return null;
  }
}

const SPACE = /\s/u;
const ASCII_WORD = /[A-Za-z0-9_]/u;
const DIGIT = /[0-9]/u;
/** 公式里有空行就不是一个公式（Markdown 里空行分段；引用块里的空行是只剩 `>` 的行） */
const BLANK_LINE = /\n[ \t>]*\n/u;
/** 行文里的 `\[1\]`、`\[1, 2\]` 是转义了方括号的引用编号，`\[ \]`、`\[x\]` 是转义了的勾选框，都不是公式（单独占一行的除外） */
const LITERAL_BRACKET = /^(?:\s*|[xX]|[\d\s,.\-–]+)$/u;
/** `$5 and 10$` 这种：数字开头、有空格、没有一个公式符号，按金额算 */
const MONEY_LIKE = /^\d[\d,.]*\s/u;
const TEX_SIGN = /[\\^_{}=+\-*/<>()[\]|]/u;

interface Found {
  end: number;
  tex: string;
  display: boolean;
}

function runOf(s: string, i: number, ch: string): number {
  let n = 0;
  while (s[i + n] === ch) n += 1;
  return n;
}

/** 从 from 起找恰好 n 个反引号的那一串（行内代码的结尾），没有返回 -1；行内代码不跨段，遇到空行就停 */
function codeSpanEnd(s: string, from: number, n: number): number {
  let j: number = from;
  while (j < s.length) {
    if (s[j] === '\n' && /^\n[ \t>]*(?:\n|$)/u.test(s.slice(j, j + 80))) return -1;
    if (s[j] !== '`') {
      j += 1;
      continue;
    }
    const run: number = runOf(s, j, '`');
    if (run === n) return j;
    j += run;
  }
  return -1;
}

/**
 * 路径里的一段：开头 `$` 紧跟在 `/` 后面（这个 `/` 前面不是另一条公式的结尾 `$`，`$a$/$b$` 照样是两条公式），
 * 结尾 `$` 后面紧跟 `/`、`\` 或 `.扩展名`。`/docs/$fee$/latest`、`docs/$fee$/a`、`./$x$/y`、`/srv/$name$.txt` 都是
 */
function inPath(s: string, i: number, end: number): boolean {
  if (s[i - 1] !== '/' || s[i - 2] === '$') return false;
  const next: string | undefined = s[end + 1];
  return next === '/' || next === '\\' || (next === '.' && ASCII_WORD.test(s[end + 2] ?? ''));
}

/** 行内 `$…$`：同一行里、开头和结尾的规则见文件头；中间遇到不能当结尾的 `$` 就放弃（`$5 and $10` 不是公式） */
function inlineDollar(s: string, i: number): Found | null {
  const before: string | undefined = s[i - 1];
  if (before !== undefined && ASCII_WORD.test(before)) return null;
  const first: string | undefined = s[i + 1];
  if (first === undefined || SPACE.test(first) || first === '$') return null;
  for (let j = i + 1; j < s.length; j += 1) {
    const c: string = s[j];
    if (c === '\\') {
      j += 1;
      continue;
    }
    if (c === '\n' || c === '`') return null;
    if (c !== '$') continue;
    const prev: string = s[j - 1];
    const next: string | undefined = s[j + 1];
    if (SPACE.test(prev) || next === '$' || (next !== undefined && (DIGIT.test(next) || ASCII_WORD.test(next)))) return null;
    const tex: string = s.slice(i + 1, j);
    if (MONEY_LIKE.test(tex) && !TEX_SIGN.test(tex)) return null;
    if (inPath(s, i, j)) return null;
    return { end: j + 1, tex, display: false };
  }
  return null;
}

/** 块级 `$$…$$`：可以跨行，不能有空行；里面单个的 `$`（如 `\text{$x$}`）可以有 */
function displayDollar(s: string, i: number): Found | null {
  for (let j = i + 2; j < s.length; j += 1) {
    const c: string = s[j];
    if (c === '\\') {
      j += 1;
      continue;
    }
    if (c === '`') return null;
    if (c !== '$') continue;
    const run: number = runOf(s, j, '$');
    if (run === 1) continue;
    if (run !== 2) return null;
    const tex: string = s.slice(i + 2, j);
    if (!tex.trim() || BLANK_LINE.test(tex)) return null;
    return { end: j + 2, tex, display: true };
  }
  return null;
}

/** `\[…\]` 是不是单独占一行（前面只有空白或引用的 `>`，后面到行尾只有空白）：这样写的就是块级公式，`\[x\]`、`\[42\]` 也是 */
function ownLine(s: string, start: number, end: number): boolean {
  const lineStart: number = s.lastIndexOf('\n', start - 1) + 1;
  return /^[ \t>]*$/u.test(s.slice(lineStart, start)) && /^[ \t]*(?:\r?\n|$)/u.test(s.slice(end, end + 80));
}

/** `\(…\)` 和 `\[…\]`：可以跨行，不能有空行；`file\(s\)` 这种紧跟在字母后面的、行文里 `\[1\]` 这种引用编号不算 */
function bracketMath(s: string, i: number, open: string): Found | null {
  const display: boolean = open === '[';
  const close: string = display ? ']' : ')';
  const before: string | undefined = s[i - 1];
  if (before !== undefined && ASCII_WORD.test(before)) return null;
  for (let j = i + 2; j < s.length; j += 1) {
    const c: string = s[j];
    if (c === '`') return null;
    if (c !== '\\') continue;
    if (s[j + 1] !== close) {
      j += 1;
      continue;
    }
    const tex: string = s.slice(i + 2, j);
    if (!tex.trim() || BLANK_LINE.test(tex)) return null;
    if (display && LITERAL_BRACKET.test(tex) && !ownLine(s, i, j + 2)) return null;
    return { end: j + 2, tex, display };
  }
  return null;
}

/** 块级公式跨行时，后面各行开头的引用标记 `>`（和开头那行一样多）去掉，TeX 里不该有它们 */
function unquote(s: string, start: number, tex: string): string {
  if (!tex.includes('\n')) return tex;
  const lineStart: number = s.lastIndexOf('\n', start - 1) + 1;
  const lead: string = s.slice(lineStart, start);
  if (!/^[ \t>]*$/u.test(lead)) return tex;
  const depth: number = (lead.match(/>/gu) ?? []).length;
  if (!depth) return tex;
  const marks = new RegExp(`^(?:[ \\t]*>){1,${depth}}[ \\t]?`, 'u');
  return tex
    .split('\n')
    .map((line: string, k: number) => (k ? line.replace(marks, '') : line))
    .join('\n');
}

/** 链接目的地的写法：`<网址>` 或一段不含空白的网址，后面可以跟引号或括号里的标题（`[0,1]($x$ 处)` 不是链接） */
const LINK_DEST = /^[ \t\n]*(?:<[^<>\n]*>|[^\s<][^\s]*)?(?:[ \t\n]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t\n]*$/u;

/** `](` 起的链接目的地（可以带标题、括号要配对）到哪个 `)` 结束；不是链接、没闭合的返回 -1 */
function destinationEnd(s: string, open: number): number {
  let depth = 0;
  for (let j: number = open; j < s.length; j += 1) {
    const c: string = s[j];
    if (c === '\\') j += 1;
    else if (c === '(') depth += 1;
    else if (c === ')') {
      depth -= 1;
      if (!depth) return LINK_DEST.test(s.slice(open + 1, j)) ? j : -1;
    } else if (c === '\n' && /^\n[ \t>]*(?:\n|$)/u.test(s.slice(j, j + 80))) return -1;
  }
  return -1;
}

/** `<https://…>` 这种尖括号自动链接 */
const AUTOLINK = /<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>/uy;
/** GFM 认的裸网址：到空白或 `<` 为止都算网址（GFM 自己也这么切） */
const BARE_URL = /(?:https?:\/\/|ftp:\/\/|www\.)[^\s<]*/iuy;
/** 行首的链接引用定义 `[id]: 网址`，网址到空白为止 */
const DEFINITION = /^ {0,3}\[[^\]\n]+\]:[ \t]*/u;

function stickyEnd(re: RegExp, s: string, i: number): number {
  re.lastIndex = i;
  return re.test(s) ? re.lastIndex : -1;
}

/** i 处开头的网址（链接目的地、自动链接、裸网址、引用定义）在哪结束；不是网址返回 -1。网址里的 `$` 不是公式 */
function urlEnd(s: string, i: number): number {
  const c: string = s[i];
  if (c === ']' && s[i + 1] === '(') {
    const end: number = destinationEnd(s, i + 1);
    return end === -1 ? -1 : end + 1;
  }
  if (c === '<') return stickyEnd(AUTOLINK, s, i);
  if ('hHfFwW'.includes(c) && !(i > 0 && ASCII_WORD.test(s[i - 1]))) return stickyEnd(BARE_URL, s, i);
  if (c === '[' && (i === 0 || s[i - 1] === '\n' || /^[ \t]{1,3}$/u.test(s.slice(s.lastIndexOf('\n', i - 1) + 1, i)))) {
    const m: RegExpMatchArray | null = s.slice(i, i + 1000).match(DEFINITION);
    if (!m) return -1;
    const rest: RegExpMatchArray | null = s.slice(i + m[0].length).match(/^\S*/u);
    return i + m[0].length + (rest ? rest[0].length : 0);
  }
  return -1;
}

/** 一段不在代码块里的 Markdown：跳过行内代码、转义和网址，把认出来的公式换成 carrier */
function protectText(s: string): string {
  let out = '';
  let last = 0;
  let i = 0;
  while (i < s.length) {
    const c: string = s[i];
    let found: Found | null = null;
    if (c === '\\') {
      const n: string | undefined = s[i + 1];
      if (n === '(' || n === '[') found = bracketMath(s, i, n);
      if (!found) {
        i += 2;
        continue;
      }
    } else if (c === '`') {
      const run: number = runOf(s, i, '`');
      const end: number = codeSpanEnd(s, i + run, run);
      i = end === -1 ? i + run : end + run;
      continue;
    } else if (c === '$') {
      const run: number = runOf(s, i, '$');
      if (run === 1) found = inlineDollar(s, i);
      else if (run === 2) found = displayDollar(s, i);
      if (!found) {
        i += run;
        continue;
      }
    } else {
      const end: number = urlEnd(s, i);
      i = end === -1 ? i + 1 : end;
      continue;
    }
    const tex: string = unquote(s, i, found.tex).trim();
    out += s.slice(last, i) + carrier(tex, found.display);
    i = found.end;
    last = i;
  }
  return out + s.slice(last);
}

/** 行首的引用标记和列表标记（`> `、`- `、`1. `），围栏可以在它们后面 */
const LEAD = String.raw`^(?:[ \t]*>)*[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*`;
const FENCE_OPEN = new RegExp(`${LEAD}(\`{3,}|~{3,})(.*)$`, 'u');
const FENCE_CLOSE = /^(?:[ \t]*>)*[ \t]*(`{3,}|~{3,})[ \t]*$/u;
const LIST_ITEM = /^(?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\S/u;
const INDENTED = /^(?: {4}|\t)/u;

/**
 * 交给 Markdown 之前把公式换成 carrier（见文件头）。按行分出代码块（围栏的、缩进的）原样留着，
 * 其余各段里找公式；没闭合的围栏一直到结尾都算代码（和 CommonMark 一样，流式输出写到一半的代码块也不碰）。
 */
export function protectMath(md: string): string {
  if (!md.includes('$') && !md.includes('\\(') && !md.includes('\\[')) return md;
  const lines: string[] = md.split(/(?<=\n)/u);
  let out = '';
  let text = '';
  let fence: { ch: string; len: number } | null = null;
  let prevBlank = true;
  let inIndented = false;
  let inList = false;
  for (const line of lines) {
    const bare: string = line.replace(/\r?\n$/u, '');
    if (fence) {
      out += line;
      const m: RegExpMatchArray | null = bare.match(FENCE_CLOSE);
      if (m && m[1][0] === fence.ch && m[1].length >= fence.len) fence = null;
      continue;
    }
    const open: RegExpMatchArray | null = bare.match(FENCE_OPEN);
    if (open && !(open[1][0] === '`' && open[2].includes('`'))) {
      out += protectText(text) + line;
      text = '';
      fence = { ch: open[1][0], len: open[1].length };
      prevBlank = false;
      inIndented = false;
      continue;
    }
    const blank: boolean = !bare.trim();
    if (!blank && INDENTED.test(bare) && !inList && (prevBlank || inIndented)) {
      out += protectText(text) + line;
      text = '';
      inIndented = true;
      prevBlank = false;
      continue;
    }
    if (!blank) {
      inIndented = false;
      if (LIST_ITEM.test(bare)) inList = true;
      else if (!/^[ \t]/u.test(bare)) inList = false;
    }
    prevBlank = blank;
    text += line;
  }
  return out + protectText(text);
}

/** mdast 里要用到的那点 */
export interface MathMdNode {
  type: string;
  value?: string;
  lang?: string | null;
  children?: MathMdNode[];
  data?: Record<string, unknown>;
}

function markMath(node: MathMdNode, tex: string, display: boolean, tag: 'span' | 'div'): void {
  node.data = {
    ...node.data,
    hName: tag,
    hProperties: { className: ['cv-math'], dataMath: display ? 'display' : 'inline' },
    hChildren: [{ type: 'text', value: tex }],
  };
}

/**
 * remark 插件里调：protectMath 换出来的行内代码、```math 围栏变成公式节点（span/div.cv-math，data-math，文字是 TeX）。
 * 节点类型不变（还是 inlineCode / code），别的插件照旧把它当代码跳过（如 T 号变链接）。
 * ```latex、```tex 是给人看的 LaTeX 源码（常常是整篇文档），照旧当代码显示。
 */
export function mathNodes(node: MathMdNode): void {
  for (const child of node.children ?? []) {
    if (child.type === 'inlineCode') {
      const m: MathSpan | null = decodeCarrier(child.value ?? '');
      if (m) markMath(child, m.tex, m.display, 'span');
      continue;
    }
    if (child.type === 'code') {
      if (child.lang?.toLowerCase() === 'math' && child.value?.trim()) markMath(child, child.value, true, 'div');
      continue;
    }
    mathNodes(child);
  }
}

function textOf(children: unknown): string {
  if (typeof children === 'string') return children;
  if (Array.isArray(children)) return children.map(textOf).join('');
  return '';
}

/** react-markdown 给 span/div 组件的 props 里认出 mathNodes 标的公式：data-math 和文字（TeX）；不是公式返回 null */
export function mathOfProps(props: { 'data-math'?: string; children?: unknown }): MathSpan | null {
  const mode: string | undefined = props['data-math'];
  if (mode !== 'inline' && mode !== 'display') return null;
  return { tex: textOf(props.children), display: mode === 'display' };
}

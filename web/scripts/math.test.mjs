// 回答里的数学公式（client/src/cv/mathText.ts、mathRender.ts）：哪些认成公式、哪些不碰（代码、美元金额、变量、转义），
// 流式输出写到一半的、写错的公式怎么显示，KaTeX 的输出不带脚本和链接；最后照 Md.tsx 的接法用 react-markdown 整条渲染一遍。
// 跑：npm run test:math
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import katex from 'katex';
import { decodeCarrier, mathNodes, protectMath } from '../client/src/cv/mathText.ts';
import { MAX_TEX, mathComponents, texToHtml } from '../client/src/cv/mathRender.ts';

/** protectMath 认出的公式，按出现顺序：[TeX, 'i' | 'd'] */
const found = (md) =>
  [...protectMath(md).matchAll(/`(\uE000[id][^`]*)`/gu)].map((m) => {
    const s = decodeCarrier(m[1]);
    return [s.tex, s.display ? 'd' : 'i'];
  });
const none = (md) => assert.equal(protectMath(md), md);

/** 和 Md.tsx 一样的接法：先 protectMath，remark 插件里 mathNodes，components 里换成 KaTeX */
const render = (md) =>
  renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm, () => (tree) => mathNodes(tree)], components: mathComponents }, protectMath(md)),
  );
const count = (html, cls) => (html.match(new RegExp(`class="${cls}"`, 'gu')) ?? []).length;

// ---- 认成公式的 ----

test('行内 $…$ 和 \\(…\\)', () => {
  assert.deepEqual(found('设 $x^2 + y^2 = 1$，则 \\(x \\le 1\\)。'), [
    ['x^2 + y^2 = 1', 'i'],
    ['x \\le 1', 'i'],
  ]);
});
test('块级 $$…$$ 和 \\[…\\]，可以跨行', () => {
  assert.deepEqual(found('$$\n\\int_0^1 x\\,dx = \\frac12\n$$'), [['\\int_0^1 x\\,dx = \\frac12', 'd']]);
  assert.deepEqual(found('\\[\n  a^2 + b^2 = c^2\n\\]'), [['a^2 + b^2 = c^2', 'd']]);
  assert.deepEqual(found('能量 $$E = mc^2$$ 写在一行里'), [['E = mc^2', 'd']]);
});
test('中文紧挨着公式也认（中文前后不加空格是常态）', () => {
  assert.deepEqual(found('函数$f(x)=x^2$的导数是$2x$。'), [
    ['f(x)=x^2', 'i'],
    ['2x', 'i'],
  ]);
});
test('公式里的 _ * | 不被 Markdown 当成强调和表格分隔', () => {
  const html = render('$a_1 * b_1 + a_2 * b_2$ 和 $|x|$');
  assert.equal(count(html, 'katex'), 2);
  assert.doesNotMatch(html, /<em>/u);
  const table = render('| 名称 | 公式 |\n| --- | --- |\n| 绝对值 | $|x|$ |');
  assert.match(table, /<td><span class="cv-math cv-math-inline"><span class="katex">/u);
  assert.equal((table.match(/<td>/gu) ?? []).length, 2);
});
test('公式里转义的 \\$ 和 \\{ 原样留给 KaTeX', () => {
  assert.deepEqual(found('$\\$5 + \\{a\\}$'), [['\\$5 + \\{a\\}', 'i']]);
  assert.deepEqual(found('$$\\text{价格 $p$}$$'), [['\\text{价格 $p$}', 'd']]);
});
test('引用块里跨行的块级公式，后面几行的 > 去掉', () => {
  assert.deepEqual(found('> $$\n> a + b\n> $$'), [['a + b', 'd']]);
  assert.match(render('> $$\n> a + b\n> $$'), /<blockquote>[\s\S]*class="katex-display"/u);
});
test('列表里的公式', () => {
  const html = render('- 第一项 $\\alpha$\n- 第二项\n\n  $$\\beta$$');
  assert.equal(count(html, 'katex'), 2);
  assert.equal(count(html, 'katex-display'), 1);
});
test('```math 围栏是块级公式，```latex 是 LaTeX 源码照旧当代码', () => {
  const html = render('```math\n\\sum_{i=1}^n i\n```\n\n```latex\n\\documentclass{article}\n```');
  assert.equal(count(html, 'katex-display'), 1);
  assert.match(html, /<pre><code class="language-latex">\\documentclass\{article\}/u);
});

// ---- 不碰的 ----

test('围栏代码块、缩进代码块、行内代码里的不碰', () => {
  none('```bash\necho "$HOME" $x$ $$y$$\n```');
  none('~~~\n$a$ \\(b\\)\n~~~');
  none('看这段：\n\n    price = $a$ + $b$\n');
  none('用 `$x$` 或 ``\\(y\\)`` 表示');
  const html = render('```python\nprint(f"${x}$")\n```\n\n`$y$`');
  assert.equal(count(html, 'katex'), 0);
  assert.match(html, /<code>\$y\$<\/code>/u);
});
test('没配对的反引号不跨段：下一段里的公式照常认', () => {
  assert.deepEqual(found('a ` b\n\n$x$ ` c'), [['x', 'i']]);
  none('a `$x$\n$y$` b');
});
test('列表里缩进四格的续行不当成缩进代码块', () => {
  assert.deepEqual(found('1. 第一步\n\n    其中 $k > 0$'), [['k > 0', 'i']]);
});
test('没闭合的围栏一直到结尾都算代码（流式输出写到一半的代码块）', () => {
  none('前面\n```js\nconst a = "$x$";\n');
});
test('美元金额不当公式', () => {
  none('苹果 $5，梨 $10。');
  none('价格在 $10 - $20 之间');
  none('$10-$20');
  none('$5,$10 和 $3.50');
  none('花了 $5 and 10$ 块');
  none('US$5 和 5$');
  none('It costs $5. Then $6.');
});
test('shell 变量、路径和 URL 里的 $ 不当公式', () => {
  none('echo $HOME and $PATH');
  none('cd $HOME/$USER/bin');
  none('见 https://example.com/a$b$c 和 C:\\Users\\$name$x');
  none('${HOME}');
  none('a$b$c');
});
test('转义的 \\$ 不是定界符', () => {
  none('打折后 \\$5 到 \\$10');
  none('\\$x\\$');
  none('\\\\(不是公式\\\\)');
});
test('行文里的 \\[1\\]、列表里的 \\[x\\] 这类转义了的方括号、file\\(s\\) 这类转义了的括号不当公式', () => {
  none('参考文献 \\[1\\] 和 \\[2, 3\\]');
  none('- \\[ \\] 待办\n- \\[x\\] 已办');
  none('选择 file\\(s\\)');
});
test('$ 后是空白、$ 前是空白的不是行内公式；空的 $$ $$ 不是公式', () => {
  none('a $ b $ c');
  none('$$ $$');
  none('$$\n\n$$');
});
test('公式中间有空行就不是一个公式（不会把两段吞成一条）', () => {
  none('前面 $$x\n\n后面 y$$');
  none('\\[a\n\nb\\]');
});

// ---- 链接的网址（整条渲染，href 必须原样）----

const hrefs = (html) => [...html.matchAll(/\s(?:href|src)="([^"]*)"/gu)].map((m) => m[1]);
const URL_FEE = 'https://example.test/$fee$/latest';

test('显式链接、图片、尖括号自动链接、裸网址、引用定义里的 $ 不当公式，href 原样', () => {
  for (const md of [
    `[price](${URL_FEE})`,
    `[price](${URL_FEE} "标题 $t$")`,
    `[price](<${URL_FEE}>)`,
    `![图](${URL_FEE})`,
    `<${URL_FEE}>`,
    `见 ${URL_FEE} 这里`,
    `[price][fee]\n\n[fee]: ${URL_FEE}`,
  ]) {
    const html = render(md);
    // 图片在 React 19 的服务端渲染里还多一个 <link rel="preload" href>，所以是“至少一个、全都原样”
    const urls = hrefs(html);
    assert.ok(urls.length >= 1 && urls.every((u) => u === URL_FEE), `${md} → ${JSON.stringify(urls)}`);
    assert.equal(count(html, 'katex'), 0, md);
    assert.doesNotMatch(html, /\uE000|%EE%80%80/u, md);
  }
  const www = render('打开 www.example.test/$a$/b 看');
  assert.deepEqual(hrefs(www), ['http://www.example.test/$a$/b']);
});
test('链接文字里的公式照常渲染，网址原样；网址后面的公式照常认', () => {
  const html = render(`[$x^2$ 的价格](${URL_FEE}) 和 $y$`);
  assert.deepEqual(hrefs(html), [URL_FEE]);
  assert.equal(count(html, 'katex'), 2);
  assert.match(html, /<a href="[^"]*"><span class="cv-math cv-math-inline"><span class="katex">/u);
});
test('不是链接的方括号加圆括号（括号里不是网址）里的公式照常认', () => {
  const html = render('区间 [0,1]($x$ 处取最大)');
  assert.equal(hrefs(html).length, 0);
  assert.equal(count(html, 'katex'), 1);
});

// ---- 没标成链接的文件路径（整条渲染）----

const plain = (html) => html.replace(/<[^>]+>/gu, '');
test('文件路径里夹在两个 / 之间的 $…$ 不当公式：绝对、相对、~、扩展名', () => {
  for (const path of ['/docs/$fee$/latest', 'docs/$fee$/latest', './$fee$/x', '../$a$/b', '~/$x$/y', '/srv/$name$.txt', '/a/$b$\\c']) {
    const html = render(`Copy to ${path} now`);
    assert.equal(count(html, 'katex'), 0, path);
    assert.ok(plain(html).includes(`Copy to ${path} now`), `${path} → ${plain(html)}`);
  }
});
test('路径和正常的行内公式共存：路径原样，公式照常（除号、中文紧挨、公式之间的 /）', () => {
  const html = render('Copy to /docs/$fee$/latest，再算 $x^2$；函数$f(x)=a/b$的值是 $x$/$y$，还有 $a$/$b$/$c$ 和 $n$/2');
  assert.ok(plain(html).startsWith('Copy to /docs/$fee$/latest，再算 '), plain(html));
  assert.equal(count(html, 'katex'), 8);
  assert.match(html, /<annotation encoding="application\/x-tex">f\(x\)=a\/b<\/annotation>/u);
  // 链接文字里的公式不受影响
  const link = render('[$x$ 的价格](https://example.test/$fee$/latest) 放在 /docs/$fee$/latest');
  assert.equal(count(link, 'katex'), 1);
  assert.ok(plain(link).endsWith('放在 /docs/$fee$/latest'));
});

// ---- 单独占一行的 \[…\] ----

test('单独占一行的 \\[x\\]、\\[42\\] 是块级公式（行文里的、列表复选框不是）', () => {
  for (const md of ['\\[x\\]', '\\[42\\]', '前一段\n\n\\[42\\]\n\n后一段', '> \\[x\\]', '\\[\n42\n\\]', '  \\[1, 2\\]  ']) {
    const html = render(md);
    assert.equal(count(html, 'katex-display'), 1, md);
  }
  assert.match(render('\\[42\\]'), /<annotation encoding="application\/x-tex">42<\/annotation>/u);
  const prose = render('参考 \\[1\\] 和 \\[x\\]\n\n- \\[ \\] 待办\n- \\[x\\] 已办\n\n价格 $5 到 $10，`\\[x\\]`');
  assert.equal(count(prose, 'katex'), 0);
  assert.match(prose, /参考 \[1\] 和 \[x\]/u);
  assert.match(prose, /<li>\[x\] 已办<\/li>/u);
  assert.match(prose, /价格 \$5 到 \$10，<code>\\\[x\\\]<\/code>/u);
});

// ---- 流式输出写到一半 ----

test('流式：写到一半的 $$、\\[、$ 原样留着，闭合后才变公式', () => {
  const full = '结果是\n$$\n\\frac{a}{b}\n$$\n然后 $x$。';
  for (let n = 1; n < full.length; n += 1) {
    const part = full.slice(0, n);
    const html = render(part);
    assert.doesNotMatch(html, /katex-error/u, `截到第 ${n} 个字时出现了报错：${JSON.stringify(part)}`);
  }
  none('结果是\n$$\n\\frac{a}{b}\n');
  none('结果是\n$$\n\\frac{a}{b}\n$');
  none('\\[\n\\sum');
  none('设 $x^2 + ');
  assert.deepEqual(found(full), [
    ['\\frac{a}{b}', 'd'],
    ['x', 'i'],
  ]);
});
test('流式：前面闭合了的公式照常渲染，后面没闭合的不影响它', () => {
  const html = render('已知 $a=1$，求 $$b');
  assert.equal(count(html, 'katex'), 1);
  assert.match(html, /\$\$b/u);
});

// ---- 写错的公式、太长的公式 ----

test('写错的公式不抛错：画成原文（次一档灰），title 里是错误说明', () => {
  const html = texToHtml('\\frac{a', false);
  assert.match(html, /class="katex-error"/u);
  assert.match(html, /title="ParseError: KaTeX parse error:/u);
  assert.match(html, /\\frac\{a/u);
  assert.match(html, /color:var\(--t3, #888\)/u);
  // 不认识的命令：原地画成次一档灰，后面的公式照常
  assert.match(render('坏的 $\\badcommand{x}$ 好的 $y$'), /style="color:var\(--t3, #888\);">\\badcommand<[\s\S]*mathnormal[^>]*>y</u);
});
test('太长的、空的公式退回 TeX 原文', () => {
  assert.equal(texToHtml('x'.repeat(MAX_TEX + 1), false), null);
  assert.equal(texToHtml('   ', true), null);
  const html = render(`$${'a+'.repeat(MAX_TEX / 2)}a$`);
  assert.match(html, /<code class="cv-math cv-math-inline cv-math-raw">\$a\+a\+/u);
});
test('宏无限展开被 maxExpand 拦住，不卡住页面', () => {
  const html = texToHtml('\\def\\a{\\a\\a}\\a', true);
  assert.match(html, /katex-error/u);
  assert.match(html, /Too many expansions/u);
});
test('同一条公式第二次直接用缓存（流式时反复渲染），不再调 KaTeX', () => {
  const real = katex.renderToString;
  let calls = 0;
  katex.renderToString = (...args) => {
    calls += 1;
    return real(...args);
  };
  try {
    const tex = '\\sum_{k=1}^{100} k^3 + \\text{cache}';
    const a = texToHtml(tex, true);
    const b = texToHtml(tex, true);
    assert.equal(a, b);
    assert.equal(calls, 1);
    texToHtml(tex, false);
    assert.equal(calls, 2, '行内和块级分开缓存');
  } finally {
    katex.renderToString = real;
  }
});

// ---- 安全 ----

const XSS = [
  '\\href{javascript:alert(1)}{点我}',
  '\\url{javascript:alert(1)}',
  '\\includegraphics{https://example.com/x.png}',
  '\\htmlClass{x" onmouseover="alert(1)}{y}',
  '\\htmlData{onclick=alert(1)}{y}',
  '\\htmlStyle{background:url(javascript:alert(1))}{y}',
  '\\htmlId{x}{y}',
  '\\text{<img src=x onerror=alert(1)>}',
  '\\text{</span><script>alert(1)</script>}',
  '<script>alert(1)</script>',
  '\\textcolor{red" onmouseover="alert(1)}{x}',
];
/** HTML 里真正的标签（正文里的 < 都被转义成 &lt;，所以这里匹配到的都是标签），标签名和属性 */
const tags = (html) => [...html.matchAll(/<([a-zA-Z][\w-]*)((?:\s+[^\s=>]+(?:="[^"]*")?)*)\s*\/?>/gu)].map((m) => ({ name: m[1].toLowerCase(), attrs: m[2] }));
const BAD_TAGS = ['script', 'img', 'a', 'iframe', 'object', 'embed', 'link', 'style', 'foreignobject', 'form', 'input', 'video', 'audio'];
const assertSafe = (html, what) => {
  for (const { name, attrs } of tags(html)) {
    assert.ok(!BAD_TAGS.includes(name), `${what}：出现了 <${name}>`);
    // 逐个属性看名字和值（值里的引号被转义成 &quot;，不会截断属性）
    for (const [, attr, value = ''] of attrs.matchAll(/\s([^\s=>]+)(?:="([^"]*)")?/gu)) {
      assert.doesNotMatch(attr, /^(?:on[a-z]+|href|src|xlink:href|action|formaction|srcdoc)$/iu, `${what}：<${name} ${attr}>`);
      if (attr !== 'title') assert.doesNotMatch(value, /javascript:|url\(/iu, `${what}：<${name} ${attr}="${value}">`);
    }
  }
};
test('KaTeX 的输出不带脚本、链接、图片和事件属性（trust 关着）', () => {
  for (const tex of XSS) {
    for (const display of [false, true]) assertSafe(texToHtml(tex, display) ?? '', tex);
  }
  // \href 不生成链接：只把命令名画成次一档灰
  assert.doesNotMatch(texToHtml('\\href{https://example.com}{x}', false), /<a\s/u);
});
test('整条渲染：公式里的 HTML 被转义，正文里的原始 HTML 照旧不渲染', () => {
  const html = render('$\\text{<b>x</b>}$ <img src=x onerror=alert(1)> $$\\href{javascript:alert(1)}{y}$$');
  assertSafe(html, '整条渲染');
  assert.ok(!tags(html).some((t) => t.name === 'b'));
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/u);
});
test('carrier 的编码里不会混进反引号、空白，伪造的 carrier 也只是一条普通公式', () => {
  const out = protectMath('$a` b$');
  assert.equal(out, '$a` b$');
  // 公式里有反引号就不认（行内代码优先）；编码后的 carrier 里没有反引号、空白和 |（GFM 先按 | 拆表格列，再解析行内代码）
  assert.equal(protectMath('$x\\text{ `` }$'), '$x\\text{ `` }$');
  const s = protectMath('$a \\cdot b | c * d_1$');
  assert.match(s, /^`\uE000i[^`\s|]+`$/u);
  assert.equal(decodeCarrier('\uE000i%E0%A4%A'), null);
  assert.equal(decodeCarrier('普通代码'), null);
});

// ---- 整条渲染 ----

test('整条渲染：行内、块级、四种定界符、普通段落和代码各就各位', () => {
  const md = [
    '勾股定理 $a^2+b^2=c^2$，欧拉公式 \\(e^{i\\pi}+1=0\\)。',
    '',
    '$$',
    '\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}',
    '$$',
    '',
    '\\[ \\nabla \\cdot \\vec{E} = \\frac{\\rho}{\\varepsilon_0} \\]',
    '',
    '这件 $5，那件 $10；`$NOT_MATH$` 不是公式。',
  ].join('\n');
  const html = render(md);
  assert.equal(count(html, 'katex'), 4);
  assert.equal(count(html, 'katex-display'), 2);
  assert.match(html, /这件 \$5，那件 \$10；<code>\$NOT_MATH\$<\/code>/u);
  // 无障碍：同时输出 MathML，读屏和 copy-tex 都用得上
  assert.match(html, /<annotation encoding="application\/x-tex">a\^2\+b\^2=c\^2<\/annotation>/u);
});
test('没有公式的 Markdown 原样不变（不碰平常的回答）', () => {
  const md = '# 标题\n\n- 一\n- 二\n\n```js\nconst a = 1;\n```\n\n| a | b |\n| - | - |\n| 1 | 2 |';
  assert.equal(protectMath(md), md);
});

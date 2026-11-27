/**
 * 公式的渲染（KaTeX，随前端打包、字体自托管，不连外网 CDN）：mathText.ts 标出的 span/div.cv-math 换成 KaTeX 排好的公式。
 *
 * - 安全：trust 关着（\href、\url、\includegraphics、\htmlClass 这类会生成链接、图片、任意属性的命令都不执行），
 *   KaTeX 的输出只有它自己的标签，正文里的 < > & 都被转义；maxSize、maxExpand 限住超大尺寸和宏展开，免得一条公式卡住页面。
 * - 写错的公式不抛错：throwOnError 关着，KaTeX 把原文画成次一档灰（title 里是错误说明）；别的意外或太长的公式退回 TeX 原文。
 * - 流式输出时同一条公式会反复渲染：按 TeX 缓存排好的 HTML（最近用过的留着）。
 * 这里不用 JSX、不导入 CSS，单测（scripts/math.test.mjs）直接导入它在 node 里渲染；页面上经 mathKatex.ts 懒加载。
 */
import { createElement, type HTMLAttributes, type ReactElement } from 'react';
import katex from 'katex';
import { mathOfProps, type MathSpan } from './mathText';

/** 再长就不排了，显示原文（几屏长的矩阵也够用） */
export const MAX_TEX = 10000;
const CACHE_SIZE = 500;
const cache = new Map<string, string>();

const OPTIONS: katex.KatexOptions = {
  throwOnError: false,
  // 写错的公式用正文的次一档灰，深浅色主题都跟着变（KaTeX 默认是写死的红色）
  errorColor: 'var(--t3, #888)',
  trust: false,
  strict: 'ignore',
  output: 'htmlAndMathml',
  maxSize: 50,
  maxExpand: 500,
};

/** TeX 排成 HTML；空的、太长的、KaTeX 意外出错的返回 null（调用方显示原文） */
export function texToHtml(tex: string, display: boolean): string | null {
  if (!tex.trim() || tex.length > MAX_TEX) return null;
  const key = `${display ? 'd' : 'i'}${tex}`;
  const hit: string | undefined = cache.get(key);
  if (hit !== undefined) {
    cache.delete(key);
    cache.set(key, hit);
    return hit;
  }
  let html: string;
  try {
    html = katex.renderToString(tex, { ...OPTIONS, displayMode: display });
  } catch {
    return null;
  }
  cache.set(key, html);
  if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value as string);
  return html;
}

/** 一条公式：排好的 KaTeX；排不了的显示 TeX 原文（带定界符，读得懂、能复制） */
export function MathView({ tex, display }: { tex: string; display: boolean }): ReactElement {
  const html: string | null = texToHtml(tex, display);
  const cls: string = display ? 'cv-math cv-math-display' : 'cv-math cv-math-inline';
  if (html === null) return createElement('code', { className: `${cls} cv-math-raw` }, display ? `$$${tex}$$` : `$${tex}$`);
  // KaTeX 自己生成并转义的 HTML（trust 关着，见文件头），不含正文里的原始 HTML
  return createElement('span', { className: cls, dangerouslySetInnerHTML: { __html: html } });
}

type HastProps = HTMLAttributes<HTMLElement> & { node?: unknown; 'data-math'?: string };

function mathOrTag(tag: 'span' | 'div') {
  return function MathOrTag({ node: _node, ...props }: HastProps): ReactElement {
    const m: MathSpan | null = mathOfProps(props);
    if (m) return createElement(MathView, m);
    const { children, ...rest } = props;
    return createElement(tag, rest, children);
  };
}

/** 同步版的 components（KaTeX 已经在手上时用，如单测）；页面上用 MdMath.tsx 的懒加载版，认公式的规则同一个 mathOfProps */
export const mathComponents = { span: mathOrTag('span'), div: mathOrTag('div') };

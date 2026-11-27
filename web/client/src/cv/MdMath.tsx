import React, { useEffect, useReducer } from 'react';
import { mathOfProps, type MathSpan } from './mathText';

/**
 * Md 里的公式（mathText.ts 标出的 span/div.cv-math）：KaTeX 懒加载（mathKatex.ts），加载好以前显示 TeX 原文，
 * 加载好了换成排好的公式；加载失败（断网、升级后旧 chunk 没了）就一直是原文，下次再有公式时重试。
 */
type MathModule = typeof import('./mathKatex');

let loaded: MathModule | null = null;
let loading: Promise<MathModule | null> | null = null;

/** 开始加载 KaTeX（正文里认出公式时就调，不等渲染到那一条） */
export function preloadMath(): Promise<MathModule | null> {
  loading ??= import('./mathKatex').then(
    (m: MathModule) => (loaded = m),
    () => {
      loading = null;
      return null;
    },
  );
  return loading;
}

const MathLazy: React.FC<{ tex: string; display: boolean }> = ({ tex, display }) => {
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const mod: MathModule | null = loaded;
  useEffect(() => {
    if (mod) return;
    // 画的是原文：等加载好再画一遍（画完到这里之间刚好加载好的，promise 已经 resolve，也会马上重画）
    let alive = true;
    void preloadMath().then((m: MathModule | null) => {
      if (alive && m) rerender();
    });
    return () => {
      alive = false;
    };
  }, [mod]);
  if (mod) return <mod.MathView tex={tex} display={display} />;
  const cls: string = display ? 'cv-math cv-math-display' : 'cv-math cv-math-inline';
  return <code className={`${cls} cv-math-raw cv-math-pending`}>{display ? `$$${tex}$$` : `$${tex}$`}</code>;
};

type HastProps = React.HTMLAttributes<HTMLElement> & { node?: unknown; 'data-math'?: string };

function mathOrTag(Tag: 'span' | 'div'): React.FC<HastProps> {
  return function MathOrTag({ node: _node, ...props }: HastProps) {
    const m: MathSpan | null = mathOfProps(props);
    if (m) return <MathLazy tex={m.tex} display={m.display} />;
    const { children, ...rest } = props;
    return <Tag {...rest}>{children}</Tag>;
  };
}

/** 给 react-markdown 的 components：模块级常量，重绘时组件不变（不会卸载重建） */
export const mdMathComponents = { span: mathOrTag('span'), div: mathOrTag('div') };

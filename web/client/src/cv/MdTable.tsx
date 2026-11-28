import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Maximize2, X } from 'lucide-react';
import { CopyButton } from './RichBody';
import './table.css';

/** 表格的字：一行一行、格子之间一个制表符（贴进飞书表格、Excel 列对得上），格子里的换行和制表符换成空格 */
export function tableText(table: HTMLTableElement | null | undefined): string {
  if (!table) return '';
  return Array.from(table.rows)
    .map((row: HTMLTableRowElement) =>
      Array.from(row.cells)
        .map((cell: HTMLTableCellElement) => (cell.innerText || cell.textContent || '').replace(/[\t\r\n]+/gu, ' ').trim())
        .join('\t'),
    )
    .join('\n');
}

/** 回答里的 Markdown 表格（covey docs/ui.md 2.5 表格）：放进能横向滚的框，右边还有内容时右缘渐隐；
 * 右上角「放大」（悬停时出现，触屏一直在）在全屏弹层里看完整表格。列宽按内容：短的不折行，长文字列到上限才折行 */
export const MdTable: React.FC<{ children?: React.ReactNode }> = ({ children }) => {
  const box = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState<{ l: boolean; r: boolean }>({ l: false, r: false });
  const [open, setOpen] = useState<boolean>(false);
  const measure = useCallback((): void => {
    const el: HTMLDivElement | null = box.current;
    if (!el) return;
    const l: boolean = el.scrollLeft > 2;
    const r: boolean = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setEdge((x) => (x.l === l && x.r === r ? x : { l, r }));
  }, []);
  useEffect(() => {
    measure();
    const el: HTMLDivElement | null = box.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, [measure]);
  return (
    <div className={`mdt${edge.l ? ' l' : ''}${edge.r ? ' r' : ''}`}>
      <div className="mdt-scroll" ref={box} onScroll={measure}>
        <table>{children}</table>
      </div>
      <CopyButton className="mdt-copy" label="复制表格" text={() => tableText(box.current?.querySelector('table'))} />
      <button type="button" className="mdt-zoom" onClick={(e: React.MouseEvent) => { e.stopPropagation(); setOpen(true); }} title="放大看完整表格" aria-label="放大看完整表格">
        <Maximize2 />
      </button>
      {open ? <TableModal onClose={() => setOpen(false)}>{children}</TableModal> : null}
    </div>
  );
};

/** 放大：全屏弹层，表格可以横向、纵向滚；Esc、关闭按钮、点遮罩都能关。挂在 .cv 里，夜航的颜色照用 */
const TableModal: React.FC<{ children?: React.ReactNode; onClose: () => void }> = ({ children, onClose }) => {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const key = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', key, true);
    const overflow: string = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();
    return () => {
      window.removeEventListener('keydown', key, true);
      document.body.style.overflow = overflow;
    };
  }, [onClose]);
  const host: Element = document.querySelector('.cv') ?? document.body;
  return createPortal(
    // React 的事件会顺着组件树冒泡到这一轮的行上（点了会收起这一轮）：弹层里的点击一律到此为止
    <div className="mdt-modal" role="dialog" aria-modal="true" aria-label="完整表格" onClick={(e: React.MouseEvent) => { e.stopPropagation(); onClose(); }}>
      <div className="mdt-panel" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <div className="mdt-head">
          <span>完整表格</span>
          <span className="mdt-hint">可以横向、纵向滚动 · Esc 关闭</span>
          <button type="button" ref={closeRef} className="mdt-close" onClick={(e: React.MouseEvent) => { e.stopPropagation(); onClose(); }} title="关闭（Esc）" aria-label="关闭">
            <X />
          </button>
        </div>
        <div className="mdt-body">
          <table>{children}</table>
        </div>
      </div>
    </div>,
    host,
  );
};

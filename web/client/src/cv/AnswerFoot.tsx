import React, { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import type { TurnRanWith, TurnView } from '@shared/api.interface';
import { useMemoryIndex } from '@client/src/hooks/use-workbench';
import { turnMemoryRows, type TurnMemoryRow } from './memoryCenter';
import { fmtDur, fmtTok, turnStats } from './lib';
import { criteriaSummary, deliveryBits } from './v34';
import './v34.css';
import './launch.css';

/* 回答下面的输入、输出两行和「复制」（T645）：话题页、大厅的回答和大厅里展开的话题结论（回带）共用，和飞书答案卡、回带卡一样 */

/** 复制一段字：先用剪贴板接口，嵌在飞书里拿不到时退回选中再复制 */
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
    area.setAttribute('readonly', '');
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

/** 回答下面一行的“输入”：Harness ｜ 供应商 ｜ 模型 ｜ 思考 ｜ 工作区（v27 ran_with，没有的不写） */
export function inputBits(r: TurnRanWith | undefined): string[] {
  if (!r) return [];
  return [r.harness, r.provider, r.model, r.effort ? `思考 ${r.effort}` : '', r.workspace]
    .filter((x): x is string => !!x);
}

/** “输出”：用时 ｜ 速度 ｜ token ｜ 缓存命中（缓存读到的占输入的几成） */
export function outputBits(t: TurnView): string[] {
  const out: string[] = [];
  const secs: number | null | undefined = turnStats(t)?.seconds ?? t.seconds;
  if (secs) out.push(`用时 ${fmtDur(secs)}`);
  if (t.speed) out.push(`${Math.round(t.speed)} token/s`);
  if (t.usage?.total) out.push(`token ${fmtTok(t.usage.total)}`);
  const input: number = t.usage?.input ?? 0;
  if (input > 0 && t.usage?.cache_read !== undefined) out.push(`缓存命中 ${((t.usage.cache_read / input) * 100).toFixed(1)}%`);
  return out;
}

/** 回答下面两行（像 Codex，和飞书答案卡一样）：输入（用的什么配置）、输出（用了多久、多少 token），各占一行、两个字左对齐；
 *  复制正文放在最后一行的末尾（T645：放在最前面时把“输入”挤到右边，和下一行的“输出”错开了） */
export const AnswerFoot: React.FC<{
  turn: TurnView;
  /** 复制什么（默认是回答正文）；停住那一轮正文是停下时的原话、不画，复制的是原因和技术详情 */
  copy?: { text: string; label: string };
}> = ({ turn, copy: what }) => {
  const [done, setDone] = useState<boolean>(false);
  const [open, setOpen] = useState<boolean>(false);
  const [memOpen, setMemOpen] = useState<boolean>(false);
  const rows: { label: string; tip: string; bits: string[] }[] = [
    { label: '输入', tip: '这一轮用的配置', bits: inputBits(turn.ran_with) },
    { label: '输出', tip: '这一轮的用时和用量', bits: outputBits(turn) },
    // v34：和飞书答案卡一样，改了代码的一轮再加一行“改动 提交号 ｜ 部署到哪 ｜ 已自检”
    { label: '改动', tip: turn.delivery?.verified ? `自检：${turn.delivery.verified}` : '这一轮的提交和部署', bits: deliveryBits(turn.delivery) },
  ].filter((r) => r.bits.length > 0);
  // v34：完成标准一行：几条满足，点开是每一条和它的依据
  const crit = criteriaSummary(turn.criteria);
  // v43：这一轮带了几条记忆；点开先列“用到”的，再列这一轮带入的全部短码，每条是链接，去记忆中心按短码看
  const mem = turn.memory && (turn.memory.count > 0 || turn.memory.used.length > 0 || turn.memory.codes.length > 0) ? turn.memory : null;
  const memIndex = useMemoryIndex(memOpen).data?.data;
  const memRows = mem ? turnMemoryRows(mem, memIndex?.titles) : null;
  const memCount: number = mem ? Math.max(mem.count, mem.codes.length) : 0;
  // 有短码或用到的条目才能展开；旧格式的任务记录只记了 M 号、没有短码时只写“带了 N 条”，不给一个点开是空的按钮
  const memCanOpen: boolean = !!memRows && (memRows.used.length > 0 || memRows.carried.length > 0);
  const memText: string = mem ? `带了 ${memCount} 条记忆${mem.used.length ? ` · 用到 ${mem.used.length} 条` : ''}` : '';
  const memItem = (r: TurnMemoryRow, i: number): React.ReactNode => (
    <span key={`${r.code}-${i}`}>
      {r.code ? <Link to={`/memory?code=${encodeURIComponent(r.code)}`}><span className="st">{r.code}</span>{r.title}</Link> : r.title}
    </span>
  );
  const memRow: React.ReactNode = mem && memRows ? (
    <>
      <div className="afoot-row" key="memory">
        <b>记忆</b>
        <span className="afoot-bits">
          {memCanOpen ? (
            <button type="button" className="afoot-crit" aria-expanded={memOpen} onClick={() => setMemOpen(!memOpen)}>
              {memText}{memOpen ? ' · 收起' : ' · 展开'}
            </button>
          ) : <span>{memText}</span>}
        </span>
      </div>
      {memOpen && memCanOpen ? (
        <div className="afoot-list">
          {memRows.used.length ? <b className="afoot-sub">用到</b> : null}
          {memRows.used.map(memItem)}
          {memRows.carried.length ? <b className="afoot-sub">{memRows.used.length ? '还带入了' : '带入了'}</b> : null}
          {memRows.carried.map(memItem)}
        </div>
      ) : null}
    </>
  ) : null;
  const copy = (): void => {
    void copyText(what?.text ?? turn.body).then((ok: boolean) => {
      if (!ok) {
        toast.error('没复制上，请手动选中');
        return;
      }
      setDone(true);
      window.setTimeout(() => setDone(false), 1500);
    });
  };
  const button: React.ReactNode = (
    <button type="button" className="afoot-copy" onClick={copy} title={what?.label ?? '复制这条回答'} aria-label={what?.label ?? '复制这条回答'}>
      {done ? <Check /> : <Copy />}
      <span>{done ? '已复制' : '复制'}</span>
    </button>
  );
  return (
    <div className="afoot">
      {rows.some((r) => r.label === '输入') ? null : memRow}
      {rows.map((r, i: number) => (
        <React.Fragment key={r.label}>
        <div className="afoot-row">
          <b>{r.label}</b>
          <span className="afoot-bits">
            {r.bits.map((x: string, j: number) => <span key={j} title={r.tip}>{x}</span>)}
            {i === rows.length - 1 ? button : null}
          </span>
        </div>
        {r.label === '输入' ? memRow : null}
        </React.Fragment>
      ))}
      {rows.length ? null : <div className="afoot-row">{button}</div>}
      {crit.text ? (
        <div className="afoot-row">
          <b>完成标准</b>
          <span className="afoot-bits">
            <button type="button" className={`afoot-crit${crit.unmet ? ' unmet' : ''}`} aria-expanded={open} onClick={() => setOpen(!open)}>
              {crit.text}{open ? ' · 收起' : ' · 展开'}
            </button>
          </span>
        </div>
      ) : null}
      {crit.text && open ? (
        <div className="afoot-list">
          {(turn.criteria ?? []).map((c, i: number) => (
            <span key={i}><span className={`st${c.state === '满足' ? '' : ' no'}`}>{c.state}</span>{c.item}{c.evidence ? `（${c.evidence}）` : ''}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
};

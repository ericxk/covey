import React, { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUp, Code2, ExternalLink, FileText, Maximize2, Minimize2, Plus, RotateCw, Square, X } from 'lucide-react';
import { toast } from 'sonner';
import type { SayFile, SayPayload } from '@shared/api.interface';
import { useIsDesktop } from '@client/src/hooks/use-breakpoint';
import { newOpId } from '@client/src/hooks/use-workbench';
import { useSendOutbox } from './sendOp';
import { addPending, getDraft, patchPending, setDraft, type PendingFile } from '@client/src/lib/conv-store';
import { errorText } from '@client/src/lib/workbench-format';
import { blobOf, localImage, useAttachments, type Attachment } from './attachments';
import { BIG_HINT, ENTER_HINT, enterAction, fmtSize, lineBreak, listEnter, listIndent, SEND_KEY, type TextEdit } from './composerText';
import type { LaunchBox } from './launch';
import { LaunchLine, LaunchNote, LaunchPanel } from './LaunchPanel';
import { pref, setPref } from './lib';
import { RichText, type EditHandle, type Trigger } from './RichText';
import { mentionAt } from './room';
import { mentionToken, plainMentions } from './mention';
import type { MentionAttrs } from './mention';
import './composer.css';

/** 对话题说一句（发件箱 say）：先进“刚发出”的气泡，结果跟着发件行走。输入框和「继续」这类按钮共用。v20：可带附件（已传完的） */
export function useSay(topicKey: string): (text: string, baseSeq: number, files?: SayFile[]) => void {
  const send = useSendOutbox();
  return (text: string, baseSeq: number, files: SayFile[] = []): void => {
    const value: string = text.trim();
    if (!value && !files.length) return;
    const id: string = newOpId();
    const shown: PendingFile[] = files.map((f: SayFile) => ({ name: f.name, mime: f.mime, ...(f.mime.startsWith('image/') ? localImage(f.id) ?? {} : {}) }));
    addPending(topicKey, { id, text: value, at: Date.now(), baseSeq, phase: 'sending', ...(shown.length ? { files: shown } : {}) });
    const payload: SayPayload = { text: value, ...(files.length ? { files } : {}) };
    send.mutate(
      { op_id: id, kind: 'say', topic_key: topicKey, payload: { ...payload } },
      { onSuccess: () => patchPending(topicKey, id, { phase: 'sent' }), onError: (e: Error) => patchPending(topicKey, id, { phase: 'failed', error: errorText(e) }) },
    );
  };
}

/** 源码模式：光标前正在打的 @ */
const mentionTrigger = (text: string, caret: number): Trigger | null => {
  const m = mentionAt(text, caret);
  return m ? { query: m.query, from: m.start, to: caret } : null;
};

/** 一次按键里大厅要看的部分（React 的和编辑器的按键事件都有） */
export interface KeyLike {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  preventDefault: () => void;
}

/** 触屏手机（没有鼠标悬停、指针粗）：屏幕键盘的回车只换行，点发送钮发；窄窗口的电脑仍按电脑算 */
const touchFirst = (): boolean => typeof window !== 'undefined' && !!window.matchMedia?.('(hover: none) and (pointer: coarse)').matches;

/** 外面能对输入框做的：聚焦、在末尾插几个字（和打字一样走 onValue，大厅和话题页的 @ 候选跟着变） */
export interface ComposerHandle {
  focus: () => void;
  insert: (s: string) => void;
  /** 插一枚 @ 胶囊：给了 range（正在打的“@查询词”）就换掉它，没给就插在光标处；源码模式写成 [@T586](covey-mention:…) 记号 */
  mention: (attrs: MentionAttrs, range?: Trigger) => void;
}

/** 大厅和话题页的输入框：上沿多一块（点到谁、由谁汇总、引用哪条、@ 候选）；按键先给 keys（@ 候选的上下键、回车、Esc），它返回 true 就不再往下走 */
export interface ComposerRoute {
  top: React.ReactNode;
  keys?: (e: KeyLike) => boolean;
  send: (text: string, files: SayFile[]) => boolean;
  /** 光标前正在打 @（查询词和位置）或不在打（null）：@ 候选跟着它；组字（中文输入法）时不会给 */
  onTrigger?: (t: Trigger | null) => void;
  /** 底栏左侧的小胶囊（由谁汇总）；手机放输入框上方一行 */
  chip?: React.ReactNode;
  /** 发送钮的 title：完整的去向（“由 T586 汇总，请 T505 协助”） */
  title?: string;
  /** 发起前就拦下的原因（协助方太多）：发送钮点不了，写着为什么 */
  blocked?: string;
}

/** 一个附件小卡：图片缩略图、文件图标 + 名字 + 大小；传的时候有进度，失败能重试，随时能删 */
const Chip: React.FC<{ a: Attachment; onRemove: () => void; onRetry: () => void; onOpen: () => void }> = ({ a, onRemove, onRetry, onOpen }) => (
  <div className={`cp-att${a.phase === 'failed' ? ' bad' : ''}`} title={a.error ? `${a.name}：${a.error}` : a.name} data-testid="attachment">
    <button type="button" className="cp-open" onClick={onOpen} aria-label={`看 ${a.name}`} title={a.image ? '点开看大图' : blobOf(a.key) ? '在新标签页打开' : a.name}>
      {a.image && a.thumb ? <img src={a.thumb} alt="" className="cp-thumb" /> : <span className="cp-ficon"><FileText /></span>}
    </button>
    <span className="cp-meta" onClick={onOpen} role="presentation">
      <span className="cp-name">{a.name}</span>
      <span className="cp-size">
        {a.phase === 'uploading' ? `传 ${Math.round(a.progress * 100)}%` : a.phase === 'failed' ? '没传上' : fmtSize(a.size)}
      </span>
    </span>
    {a.phase === 'failed' ? (
      <button type="button" className="cp-x" onClick={onRetry} aria-label={`重传 ${a.name}`} title="重传">
        <RotateCw />
      </button>
    ) : null}
    <button type="button" className="cp-x" onClick={onRemove} aria-label={`删掉 ${a.name}`} title="删掉">
      <X />
    </button>
    {a.phase === 'uploading' ? <span className="cp-bar" style={{ width: `${Math.round(a.progress * 100)}%` }} /> : null}
  </div>
);

/** 草稿附件里的图点开看大图：有原文件就看原图，刷新过页面只剩小图；Esc、点空白处关，左右键换一张 */
const DraftLightbox: React.FC<{ items: Attachment[]; at: number; onAt: (i: number) => void; onClose: () => void }> = ({ items, at, onAt, onClose }) => {
  const a: Attachment = items[at];
  const n: number = items.length;
  const [url, setUrl] = useState<string>('');
  useEffect(() => {
    const f: File | undefined = blobOf(a.key);
    if (!f) {
      setUrl(a.thumb ?? '');
      return undefined;
    }
    const u: string = URL.createObjectURL(f);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [a.key, a.thumb]);
  useEffect(() => {
    const key = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      } else if (e.key === 'ArrowLeft' && n > 1) onAt((at - 1 + n) % n);
      else if (e.key === 'ArrowRight' && n > 1) onAt((at + 1) % n);
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, [at, n, onAt, onClose]);
  return createPortal(
    <div className="lbx" role="dialog" aria-modal="true" aria-label="看大图" onClick={onClose}>
      <div className="lbx-top" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <span className="lbx-n">{n > 1 ? `${at + 1} / ${n}` : ''}</span>
        <span className="lbx-note">{a.name}{blobOf(a.key) ? '' : '（刷新过页面，只剩小图）'}</span>
        {url && blobOf(a.key) ? (
          <a className="lbx-btn" href={url} target="_blank" rel="noreferrer" title="新标签页打开"><ExternalLink /></a>
        ) : null}
        <button type="button" className="lbx-btn" onClick={onClose} title="关闭（Esc）" aria-label="关闭"><X /></button>
      </div>
      <div className="lbx-stage">
        {url ? <img src={url} alt={a.name} onClick={(e: React.MouseEvent) => e.stopPropagation()} /> : <span className="lbx-gone">没有预览</span>}
      </div>
    </div>,
    document.body,
  );
};

/**
 * 底部输入框（T616）：
 * - 按键和飞书聊天一样（T761，composerText.ts 的 enterAction）：收着时 Enter 发送，Shift / Ctrl / ⌘ / Alt + Enter 换行；
 *   放大后几种 Enter 都换行，只有发送钮发；触屏手机上回车换行、发送钮发。换行都是另起一段：列表里续下一项，Tab / Shift+Tab 缩进列表项。
 * - 边写边渲染（RichText）：写 Markdown 直接变样子，发出去的仍是 Markdown 文字；「</>」切到源码（纯文字框），选择记在本机。
 * - 随内容长高（上限按对话区算），再高出滚动条；「放大」切成大编辑区，再点或 Esc 收回。
 * - 写的字和附件按对话存（换对话、刷新都在，发出去才清）；附件小卡点开看大图，文件在新标签页打开。
 * - 加号选文件（多选），也能粘贴图片、把文件拖进来；附件全部传完才能发。开新话题和答题时不带附件。
 * 有 value/onValue 时由外面管着写的字（有题在等时问题面板也要读它）。有 onSubmit 时发送交给它（有题在等时就是提交回答）：
 * 它返回 true 才清空输入框；canSubmit 管发送钮能不能点。config 是输入框下沿的配置栏（话题页的换配置），没有就显示 extra。
 */
export const Composer: React.FC<{
  topicKey: string;
  maxSeq: number;
  placeholder: string;
  hint?: React.ReactNode;
  onStop?: () => void;
  /** 停止已经发出、话题还没停下：停止钮禁用，写“正在停…”（T765 F2） */
  stopping?: boolean;
  extra?: React.ReactNode;
  config?: React.ReactNode;
  value?: string;
  onValue?: (v: string) => void;
  onSubmit?: (text: string) => boolean;
  canSubmit?: boolean;
  sendLabel?: string;
  inputRef?: React.Ref<ComposerHandle>;
  /** 大厅给：输入框上沿多一行“交给管家 / 开新话题”，切到开新话题时发出走 launch */
  launch?: LaunchBox | null;
  /** 新话题页：发出去总是开新话题；上沿的“交给管家 / 开新话题”和配置面板不画（页面自己画） */
  launchOnly?: boolean;
  /** 大厅给（T624）：输入框上沿的 @、引用条和 @ 候选；发出交给 route.send（它决定交给谁），返回 true 才清空 */
  route?: ComposerRoute | null;
}> = ({ topicKey, maxSeq, placeholder: basePlaceholder, hint, onStop, stopping = false, extra, config, value, onValue, onSubmit, canSubmit, sendLabel: baseLabel = '发送', inputRef, launch, route, launchOnly = false }) => {
  const [own, setOwn] = useState<string>(() => getDraft(topicKey));
  const text: string = value ?? own;
  const desktop: boolean = useIsDesktop();
  const say = useSay(topicKey);
  const att = useAttachments(topicKey);
  const [big, setBig] = useState<boolean>(false);
  /** Enter 发送还是换行：收着时发送，放大后、触屏手机上换行 */
  const sendOnEnter: boolean = !big && !touchFirst();
  /** 看 Markdown 源码（纯文字框）；默认边写边渲染。记在本机 */
  const [source, setSourceState] = useState<boolean>(() => pref('composer_source', '0') === '1');
  const setSource = (v: boolean): void => {
    setPref('composer_source', v ? '1' : '0');
    setSourceState(v);
  };
  const [drag, setDrag] = useState<boolean>(false);
  const [viewing, setViewing] = useState<number | null>(null);
  const localRef = useRef<HTMLTextAreaElement | null>(null);
  const rich = useRef<EditHandle | null>(null);
  /** 对话区（.conv）变高变矮时重算输入框的上限 */
  const [room, setRoom] = useState<number>(0);
  const pick = useRef<HTMLInputElement | null>(null);
  /** 改写后要放回的光标（列表续行、缩进） */
  const caret = useRef<{ start: number; end: number } | null>(null);
  const focus = (): void => {
    if (source) localRef.current?.focus();
    else rich.current?.focus();
  };
  // 插字：边写边渲染时交给编辑器（走它的 onUpdate），源码框直接改值、光标放到最后
  const insert = (s: string): void => {
    if (!source) {
      rich.current?.insert(s);
      return;
    }
    const v: string = text + s;
    change(v);
    route?.onTrigger?.(mentionTrigger(v, v.length));
    caret.current = { start: v.length, end: v.length };
    localRef.current?.focus();
  };
  // 源码模式：把“@查询词”（或光标处）换成胶囊记号
  const mention = (attrs: MentionAttrs, range?: Trigger): void => {
    if (!source) {
      rich.current?.mention(attrs, range);
      return;
    }
    const at: number = localRef.current?.selectionStart ?? text.length;
    const from: number = range?.from ?? at;
    const to: number = range?.to ?? at;
    const put: string = `${mentionToken(attrs.key, attrs.label)} `;
    const v: string = text.slice(0, from) + put + text.slice(to);
    change(v);
    route?.onTrigger?.(null);
    caret.current = { start: from + put.length, end: from + put.length };
    localRef.current?.focus();
  };
  useImperativeHandle(inputRef, () => ({ focus, insert, mention }));
  const change = (v: string): void => {
    if (onValue) onValue(v);
    else setOwn(v);
    setDraft(topicKey, v);
  };
  const box: LaunchBox | null = launch && !onSubmit ? launch : null;
  const fresh: boolean = !!box && (launchOnly || box.mode === 'new');
  const placeholder: string = fresh ? '写下要开的新话题：要做什么' : basePlaceholder;
  const sendLabel: string = fresh ? '开始任务' : baseLabel;
  // 开新话题、答题时不带附件
  const noFiles: string = onSubmit ? '答题时不带附件，答完再发' : '';

  // 随内容长高：先放开高度量 scrollHeight，再夹到上限；放大时取能放下的最大高度。
  // 上限按对话区（.conv）实际的高算，不按窗口：飞书里嵌着的页面窗口比看得见的区域高，按窗口算会把底下的按钮栏顶出去（T616 17:30）。
  useLayoutEffect(() => {
    const el: HTMLElement | null = source ? localRef.current : (rich.current?.box() ?? null);
    if (!el) return;
    const shell: HTMLElement | null = el.closest('.composer, .mcomp');
    const conv: HTMLElement | null = el.closest('.conv');
    const vh: number = window.innerHeight || 800;
    const space: number = conv?.clientHeight || vh;
    // 输入框里除了编辑区以外的部分（附件卡、按钮栏、提示行）
    const chrome: number = (shell ? shell.offsetHeight - el.offsetHeight : 60) + 34;
    // 不在对话区里（新话题页）：从编辑区顶上到窗口底，留出下面的按钮栏和提示行
    const below: number = shell ? shell.getBoundingClientRect().bottom - el.getBoundingClientRect().bottom + 34 : 60;
    const limit: number = conv ? space - chrome - (big ? 24 : 120) : Math.max(120, vh - el.getBoundingClientRect().top - below - 16);
    const fit = (want: number): number => Math.max(48, Math.min(want, limit));
    if (big) {
      el.style.height = `${fit(Math.round(space * 0.85))}px`;
      el.style.overflowY = 'auto';
      return;
    }
    el.style.height = 'auto';
    const max: number = fit(Math.round(vh * (desktop ? 0.4 : 0.3)));
    el.style.height = `${Math.min(el.scrollHeight + 2, max)}px`;
    el.style.overflowY = el.scrollHeight + 2 > max ? 'auto' : 'hidden';
  }, [text, big, desktop, source, room, att.items.length]);
  useEffect(() => {
    const conv: Element | null | undefined = (source ? localRef.current : rich.current?.box())?.closest('.conv');
    if (!conv || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setRoom(conv.clientHeight));
    ro.observe(conv);
    return () => ro.disconnect();
  }, [source]);
  useLayoutEffect(() => {
    const el: HTMLTextAreaElement | null = localRef.current;
    const c = caret.current;
    if (el && c) {
      el.setSelectionRange(c.start, c.end);
      caret.current = null;
    }
  }, [text]);
  // 换话题、发出去以后退出放大
  useEffect(() => setBig(false), [topicKey]);
  // 放大时 Esc 收回：焦点在放大钮、预览上也算
  useEffect(() => {
    if (!big) return undefined;
    const esc = (e: KeyboardEvent): void => {
      if (e.key === 'Escape' && !e.isComposing) setBig(false);
    };
    document.addEventListener('keydown', esc);
    return () => document.removeEventListener('keydown', esc);
  }, [big]);
  const toggleBig = (): void => {
    setBig(!big);
    window.requestAnimationFrame(focus);
  };

  const apply = (r: TextEdit | null, e: React.KeyboardEvent): boolean => {
    if (!r) return false;
    e.preventDefault();
    caret.current = { start: r.start, end: r.end };
    change(r.value);
    return true;
  };
  const done = (): void => {
    change('');
    att.clear();
    setBig(false);
  };
  const submit = (): void => {
    if (box && fresh) {
      if (!att.ready) {
        toast.message(att.items.some((a: Attachment) => a.phase === 'failed') ? '有附件没传上，重传或删掉再发' : '附件还在传，传完再发');
        return;
      }
      // v25：附件随第一轮一起交给新话题
      box.send(plainMentions(text), () => {
        change('');
        att.clear();
        setBig(false);
      }, att.files);
      return;
    }
    if (onSubmit) {
      if (onSubmit(text)) {
        change('');
        setBig(false);
      }
      return;
    }
    if (!att.ready) {
      toast.message(att.items.some((a: Attachment) => a.phase === 'failed') ? '有附件没传上，重传或删掉再发' : '附件还在传，传完再发');
      return;
    }
    if (!text.trim() && !att.files.length) return;
    if (route) {
      if (route.blocked) {
        toast.message(route.blocked);
        return;
      }
      if (route.send(text, att.files)) done();
      return;
    }
    say(text, maxSeq, att.files);
    done();
  };
  const sendable: boolean = onSubmit
    ? !!canSubmit
    : fresh
      ? !!text.trim() && box?.phase !== 'sending' && att.ready
      : (!!text.trim() || att.items.length > 0) && att.ready && !route?.blocked;
  const addFiles = (list: FileList | File[] | null | undefined): void => {
    if (!list || !Array.from(list).length) return;
    if (noFiles) {
      toast.message(noFiles);
      return;
    }
    const note: string = att.add(list);
    if (note) toast.message(note);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing) return;
    if (route?.keys && route.keys(e)) return;
    const el: HTMLTextAreaElement = e.currentTarget;
    if (e.key === 'Tab' && !e.altKey && !e.metaKey && !e.ctrlKey) {
      apply(listIndent(el.value, el.selectionStart, el.selectionEnd, e.shiftKey), e);
      return;
    }
    const act = enterAction({ key: e.key, shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, keyCode: e.keyCode }, sendOnEnter);
    if (act === 'send') {
      e.preventDefault();
      submit();
      return;
    }
    if (act !== 'break') return;
    // 换行：列表项里续下一项；Ctrl / ⌘ / Alt + Enter 浏览器自己不换，替它换；Enter、Shift+Enter 交给浏览器
    if (apply(listEnter(el.value, el.selectionStart, el.selectionEnd), e)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) apply(lineBreak(el.value, el.selectionStart, el.selectionEnd), e);
  };
  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>): void => {
    const files: File[] = Array.from(e.clipboardData?.files ?? []);
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
  };
  const dropProps = {
    onDragOver: (e: React.DragEvent): void => {
      if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return;
      e.preventDefault();
      setDrag(true);
    },
    onDragLeave: (e: React.DragEvent): void => {
      if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
      setDrag(false);
    },
    onDrop: (e: React.DragEvent): void => {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      setDrag(false);
      addFiles(e.dataTransfer.files);
    },
  };
  const area = !source ? (
    <RichText
      ref={rich}
      value={text}
      onChange={change}
      placeholder={placeholder}
      onSubmit={submit}
      sendOnEnter={sendOnEnter}
      onKey={route?.keys}
      onTrigger={route?.onTrigger}
      onFiles={addFiles}
      className={big ? 'big' : undefined}
    />
  ) : (
    <textarea
      ref={localRef}
      value={text}
      rows={1}
      placeholder={placeholder}
      aria-label={placeholder}
      className={big ? 'big' : undefined}
      onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => {
        change(e.target.value);
        // 组字（中文输入法）时不弹候选
        route?.onTrigger?.((e.nativeEvent as InputEvent).isComposing ? null : mentionTrigger(e.target.value, e.target.selectionStart ?? e.target.value.length));
      }}
      onSelect={route?.onTrigger ? (e: React.SyntheticEvent<HTMLTextAreaElement>) => route.onTrigger?.(mentionTrigger(e.currentTarget.value, e.currentTarget.selectionStart ?? 0)) : undefined}
      onCompositionEnd={route?.onTrigger ? (e: React.CompositionEvent<HTMLTextAreaElement>) => route.onTrigger?.(mentionTrigger(e.currentTarget.value, e.currentTarget.selectionStart ?? 0)) : undefined}
      onKeyDown={onKeyDown}
      onPaste={onPaste}
    />
  );
  const images: Attachment[] = att.items.filter((a: Attachment) => a.image && (a.thumb || blobOf(a.key)));
  const openChip = (a: Attachment): void => {
    const i: number = images.findIndex((x: Attachment) => x.key === a.key);
    if (i >= 0) {
      setViewing(i);
      return;
    }
    const f: File | undefined = blobOf(a.key);
    if (f) {
      const u: string = URL.createObjectURL(f);
      window.open(u, '_blank', 'noopener');
      window.setTimeout(() => URL.revokeObjectURL(u), 60000);
    } else toast.message('刷新过页面，这个文件只留了名字；发出后 Covey 那边照样拿得到');
  };
  const chips = att.items.length ? (
    <div className="cp-atts">
      {att.items.map((a: Attachment) => (
        <Chip key={a.key} a={a} onRemove={() => att.remove(a.key)} onRetry={() => att.retry(a.key)} onOpen={() => openChip(a)} />
      ))}
      {viewing !== null && images[viewing] ? <DraftLightbox items={images} at={viewing} onAt={setViewing} onClose={() => setViewing(null)} /> : null}
    </div>
  ) : null;
  const fileInput = (
    <input
      ref={pick}
      type="file"
      multiple
      hidden
      onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
        addFiles(e.target.files);
        e.target.value = '';
      }}
    />
  );
  const plus = (
    <button
      type="button"
      className="cp-ic"
      onClick={() => pick.current?.click()}
      disabled={!!noFiles}
      aria-label="添加图片或文件"
      title={noFiles || '添加图片或文件（也可以粘贴、拖进来）'}
    >
      <Plus />
    </button>
  );
  const tools = (
    <>
      <button type="button" className={`cp-ic${source ? ' on' : ''}`} onClick={() => { setSource(!source); window.requestAnimationFrame(() => (source ? rich.current?.focus() : localRef.current?.focus())); }} aria-pressed={source} aria-label="Markdown 源码" title={source ? '回到边写边渲染' : '看 Markdown 源码'}>
        <Code2 />
      </button>
      <button type="button" className={`cp-ic${big ? ' on' : ''}`} onClick={toggleBig} aria-pressed={big} aria-label={big ? '收回' : '放大'} title={big ? '收回（Esc）' : '放大编辑'}>
        {big ? <Minimize2 /> : <Maximize2 />}
      </button>
    </>
  );
  if (!desktop) {
    return (
      <>
        {box && !launchOnly ? <LaunchLine box={box} /> : null}
        {box && fresh && !launchOnly && box.open ? <LaunchPanel box={box} /> : null}
        {box && fresh && !launchOnly ? <LaunchNote box={box} /> : null}
        {config ? <div className="mcfg">{config}</div> : null}
        {route?.top ? <div className="mcroute">{route.top}</div> : null}
        {route?.chip && !fresh ? <div className="mcchip">{route.chip}</div> : null}
        {chips ? <div className="mcatts">{chips}</div> : null}
        <div className={`mcomp${drag ? ' drag' : ''}`} {...dropProps}>
          {fileInput}
          <button type="button" className="pl" onClick={() => pick.current?.click()} disabled={!!noFiles} aria-label="添加图片或文件" title={noFiles || '添加图片或文件'} style={{ border: 0 }}>
            <Plus />
          </button>
          <div className="in" style={{ padding: '6px 14px', display: 'flex', alignItems: 'center' }}>
            {area}
          </div>
          {onStop ? (
            <button type="button" className="stopb" onClick={onStop} disabled={stopping} aria-label={stopping ? '正在停' : '停止'} style={{ height: 40, borderRadius: 20 }}>
              <Square /> {stopping ? '正在停…' : '停止'}
            </button>
          ) : null}
          <button type="button" className="send" onClick={submit} disabled={!sendable} aria-label={sendLabel} title={route?.blocked || route?.title || sendLabel} style={{ border: 0 }}>
            <ArrowUp />
          </button>
        </div>
      </>
    );
  }
  return (
    <>
      {box && fresh && !launchOnly && box.open ? <LaunchPanel box={box} /> : null}
      <div className={`composer${big ? ' big' : ''}${drag ? ' drag' : ''}`} {...dropProps}>
        {fileInput}
        {box && !launchOnly ? <LaunchLine box={box} /> : null}
        {box && fresh && !launchOnly ? <LaunchNote box={box} /> : null}
        {route && !fresh ? route.top : null}
        {chips}
        {area}
        <div className="bar">
          {plus}
          {route?.chip && !fresh ? <span className="cp-chip">{route.chip}</span> : null}
          {config ?? <span className="t3" style={{ fontSize: 12 }}>{extra}</span>}
          {config && extra ? <span className="t3 cp-extra" style={{ fontSize: 12 }}>{extra}</span> : null}
          <span className="sp" />
          {tools}
          {onStop ? (
            <button type="button" className="stopb" onClick={onStop} disabled={stopping} aria-label={stopping ? '正在停' : undefined}>
              <Square /> {stopping ? '正在停…' : '停止'}
            </button>
          ) : null}
          <button type="button" className="send" onClick={submit} disabled={!sendable} aria-label={sendLabel} title={route?.blocked || `${route?.title ? `${route.title}：` : ''}${sendLabel}${sendOnEnter ? `（${SEND_KEY}）` : ''}`} style={{ border: 0 }}>
            <ArrowUp />
          </button>
        </div>
        {drag ? <div className="cp-drop">松开就加进附件</div> : null}
      </div>
      <div className="hint">{big ? BIG_HINT : (hint ?? ENTER_HINT)}</div>
    </>
  );
};

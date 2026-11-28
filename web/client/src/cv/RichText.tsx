import React, { forwardRef, useEffect, useImperativeHandle, useLayoutEffect, useRef } from 'react';
import { EditorContent, useEditor, type Editor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import { Placeholder } from '@tiptap/extensions';
import { TaskItem, TaskList } from '@tiptap/extension-list';
import { enterAction } from './composerText';
import { mentionAt, OBJ } from './room';
import { Mention } from './MentionNode';
import type { MentionAttrs } from './mention';

/** 空段落在 Markdown 里是一串空行：代码块外连着的空行并成一个，结尾的空行去掉（发出去、存草稿都用这份） */
export function tidyMarkdown(md: string): string {
  const out: string[] = [];
  let fence = false;
  let blank = 0;
  for (const line of md.split('\n')) {
    if (/^\s*(```|~~~)/u.test(line)) fence = !fence;
    if (!fence && !line.trim()) {
      blank += 1;
      if (blank > 1) continue;
    } else blank = 0;
    out.push(line);
  }
  return out.join('\n').replace(/\s+$/u, '');
}

/** 输入框的编辑区要给外面的：聚焦、拿到会滚动的那层（量高度用） */
export interface EditHandle {
  focus: () => void;
  box: () => HTMLElement | null;
  /** 在末尾插几个字（和打字一样触发 onChange） */
  insert: (s: string) => void;
  /** 插一枚 @ 胶囊：给了 range（正在打的“@查询词”）就换掉它，没给就插在光标处（没点过输入框时在末尾） */
  mention: (attrs: MentionAttrs, range?: Trigger) => void;
}

/** 光标前正在打的 @：@ 后面的字，和“@查询词”在文档里的位置 */
export interface Trigger {
  query: string;
  from: number;
  to: number;
}

/** 光标前这个段落里正在打的 @；胶囊不算字母数字，组字（IME）时不算 */
export function triggerOf(ed: Editor): Trigger | null {
  const { state, view } = ed;
  const sel = state.selection;
  if (!sel.empty || view.composing) return null;
  const $from = sel.$from;
  if (!$from.parent.isTextblock) return null;
  let before = '';
  $from.parent.forEach((child, offset) => {
    if (offset >= $from.parentOffset) return;
    before += child.isText ? (child.text ?? '').slice(0, $from.parentOffset - offset) : OBJ;
  });
  const m = mentionAt(before, before.length);
  return m ? { query: m.query, from: $from.pos - (before.length - m.start), to: $from.pos } : null;
}

/** 换行键（Shift / Ctrl / ⌘ / Alt + Enter）转成的普通 Enter：自己的 handleKeyDown 认出它就放过，交给编辑器的键位 */
const replayed = new WeakSet<KeyboardEvent>();

/**
 * 边写边渲染的输入框（T616，所见即所得）：写 Markdown 就直接变样子——行首“- ”“1. ”“[ ] ”成列表、“# ”成标题、“> ”成引用、
 * “```”成代码块、**粗体**、`代码`；Tab / Shift+Tab 缩进列表项。
 * Enter 按 composerText.ts 的 enterAction（T761，和飞书一样）：收着时 Enter 发送，Shift / Ctrl / ⌘ / Alt + Enter 换行；放大后、手机上都换行。
 * 换行一律是另起一段（列表里是下一项，空项再按一次结束列表），不用段内的软换行：软换行后面打的“* ”不在段首，不变列表。
 * 对外仍是 Markdown 文字：value 进、onChange 出，发出去的就是这段文字。粘贴或拖进来的文件交给 onFiles（附件），不进正文。
 */
export const RichText = forwardRef<
  EditHandle,
  {
    value: string;
    onChange: (md: string) => void;
    placeholder: string;
    /** Enter 发送（enterAction 说 send 时） */
    onSubmit: () => void;
    /** 收着时 true：Enter 发送；放大后、手机上 false：Enter 换行 */
    sendOnEnter: boolean;
    onFiles: (files: File[]) => void;
    className?: string;
    /** 大厅（T624）：按键先给它（@ 候选的上下键、回车、Esc），返回 true 就不再往下走 */
    onKey?: (e: KeyboardEvent) => boolean;
    /** 光标前正在打 @ 时给出查询词和位置，不在打（或在组字）就是 null（@ 候选跟着它） */
    onTrigger?: (t: Trigger | null) => void;
  }
>(({ value, onChange, placeholder, onSubmit, sendOnEnter, onFiles, className, onKey, onTrigger }, ref) => {
  const last = useRef<string>(value);
  const box = useRef<HTMLDivElement | null>(null);
  // 编辑器只建一次：回调走 ref，拿到的总是最新的
  const cb = useRef({ onChange, onSubmit, sendOnEnter, onFiles, onKey, onTrigger });
  cb.current = { onChange, onSubmit, sendOnEnter, onFiles, onKey, onTrigger };
  const edRef = useRef<Editor | null>(null);
  // 占位字也走 ref：TipTap 的 extension.options 每次读都是一份新拷贝，改它不会进插件（T952：题后到时输入框还写着旧的占位）
  const hint = useRef<string>(placeholder);
  hint.current = placeholder;
  const focused = useRef<boolean>(false);
  const trig = (ed: Editor | null): void => {
    if (ed) cb.current.onTrigger?.(triggerOf(ed));
  };
  const editor: Editor | null = useEditor({
    extensions: [
      StarterKit.configure({ link: false, underline: false }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Markdown.configure({ indentation: { style: 'space', size: 2 } }),
      Placeholder.configure({ placeholder: () => hint.current }),
      Mention,
    ],
    content: value,
    contentType: 'markdown',
    immediatelyRender: true,
    shouldRerenderOnTransaction: false,
    editorProps: {
      attributes: { class: 'cp-rich prose max-w-none', 'aria-label': placeholder, 'aria-multiline': 'true', role: 'textbox', spellcheck: 'false' },
      handleDOMEvents: {
        // 组字结束（中文输入法）后再看一次要不要弹候选：组字中不弹、不碰输入
        compositionend: () => {
          window.setTimeout(() => trig(edRef.current), 0);
          return false;
        },
        focus: () => {
          focused.current = true;
          return false;
        },
      },
      handleKeyDown: (view, e: KeyboardEvent): boolean => {
        if (replayed.has(e)) return false;
        if (!e.isComposing && cb.current.onKey?.(e)) return true;
        const act = enterAction(e, cb.current.sendOnEnter);
        if (act === 'send') {
          e.preventDefault();
          cb.current.onSubmit();
          return true;
        }
        if (act === 'break' && (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey)) {
          // 带修饰键的换行当成普通的 Enter 交给编辑器的键位（换段、续列表、结束空项、代码块里换行、``` 回车成代码块），
          // 不走 HardBreak 的 Shift/Mod+Enter 软换行。标题里也一样：末尾换行是另起一段正文（T616 21:26）
          e.preventDefault();
          const plain = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true });
          replayed.add(plain);
          if (!view.someProp('handleKeyDown', (f) => f(view, plain))) edRef.current?.commands.splitBlock();
          return true;
        }
        return false;
      },
      handlePaste: (_view, e: ClipboardEvent): boolean => {
        const files: File[] = Array.from(e.clipboardData?.files ?? []);
        if (!files.length) return false;
        e.preventDefault();
        cb.current.onFiles(files);
        return true;
      },
      handleDrop: (_view, e: DragEvent): boolean => {
        const files: File[] = Array.from(e.dataTransfer?.files ?? []);
        if (!files.length) return false;
        e.preventDefault();
        cb.current.onFiles(files);
        return true;
      },
    },
    onUpdate: ({ editor: ed }: { editor: Editor }) => {
      const md: string = ed.isEmpty ? '' : tidyMarkdown(ed.getMarkdown());
      last.current = md;
      cb.current.onChange(md);
      trig(ed);
    },
    onSelectionUpdate: ({ editor: ed }: { editor: Editor }) => trig(ed),
    onBlur: () => cb.current.onTrigger?.(null),
  });
  edRef.current = editor;
  // 外面换了字（发出后清空、换对话、“先问一句”清掉）：整段换掉，不触发 onChange。
  // 要在 useLayoutEffect 里换：Composer 在它自己的 useLayoutEffect 里按编辑区的 scrollHeight 定高，子组件的 layout effect 先于父组件跑，
  // 这里先把字换掉，量到的才是新字的高。原来写在 useEffect 里（晚于父组件的 layout effect），发完一段长消息后量到的还是旧字的高、
  // 写死在 style.height 上，清空以后不再重算（emitUpdate: false），空输入框卡在近半屏高（T789，主人截图 T774）
  useLayoutEffect(() => {
    if (!editor || value === last.current) return;
    last.current = value;
    editor.commands.setContent(value, { contentType: 'markdown', emitUpdate: false });
  }, [editor, value]);
  useEffect(() => {
    if (!editor) return;
    editor.view.dom.setAttribute('aria-label', placeholder);
    editor.view.dispatch(editor.state.tr); // 让占位字重画
  }, [editor, placeholder]);
  useImperativeHandle(ref, () => ({
    focus: () => editor?.commands.focus('end'),
    box: () => box.current,
    // 在末尾插几个字，和打字一样走 onUpdate（“请人一起看”打一个 @）
    insert: (s: string) => {
      editor?.chain().focus('end').insertContent(s).run();
    },
    mention: (attrs: MentionAttrs, range?: Trigger) => {
      if (!editor) return;
      const content = [{ type: 'mention', attrs: { ...attrs, avatar: attrs.avatar ?? '' } }, { type: 'text', text: ' ' }];
      if (range) editor.chain().focus().insertContentAt({ from: range.from, to: range.to }, content).run();
      else if (focused.current) editor.chain().focus().insertContent(content).run();
      else editor.chain().focus('end').insertContent(content).run();
    },
  }), [editor]);
  return (
    <div ref={box} className={`cp-richbox${className ? ` ${className}` : ''}`} onClick={(e: React.MouseEvent) => e.target === box.current && editor?.commands.focus('end')}>
      <EditorContent editor={editor} />
    </div>
  );
});
RichText.displayName = 'RichText';

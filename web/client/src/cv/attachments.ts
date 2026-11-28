import { useSyncExternalStore } from 'react';
import { UPLOAD_MAX_BYTES, UPLOAD_MAX_FILES, type SayFile } from '@shared/api.interface';
import { uploadDone, uploadPart, uploadStart } from '@client/src/api/workbench';
import { partBytesOf, partRange } from './uploadParts';
import { errorText } from '@client/src/lib/workbench-format';

/** 输入框的一个附件（v20）：选好就开始传，传完才有 file（say 里带的编号） */
export interface Attachment {
  key: string;
  name: string;
  mime: string;
  size: number;
  image: boolean;
  /** 图片的本地预览：小图（data URL，存进 localStorage，刷新后还在） */
  thumb?: string;
  /** 加进来的时刻（毫秒）：网页库里的附件 7 天过期，存着的草稿附件 6 天后不再拿出来 */
  at?: number;
  phase: 'uploading' | 'done' | 'failed';
  /** 0–1 */
  progress: number;
  error?: string;
  file?: SayFile;
}

const RETRIES = 2;

function base64Of(blob: Blob): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s: string = String(r.result ?? '');
      resolve(s.slice(s.indexOf(',') + 1));
    };
    r.onerror = () => reject(r.error ?? new Error('读不了这个文件'));
    r.readAsDataURL(blob);
  });
}

async function withRetry<T>(fn: () => Promise<T>): Promise<T> {
  let last: unknown;
  for (let i = 0; i <= RETRIES; i += 1) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      await new Promise<void>((r) => window.setTimeout(r, 600 * (i + 1)));
    }
  }
  throw last;
}

/** 粘贴的截图没有名字（image.png），加上时刻免得几张同名 */
function nameOf(f: File): string {
  if (f.name && f.name !== 'image.png') return f.name;
  const d = new Date();
  const p = (n: number): string => String(n).padStart(2, '0');
  return `截图-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${(f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`;
}

/* 草稿附件按对话分开存（T616）：模块里一份，切换对话、组件卸载都不停传，也不丢；传完的连同小图另存进 localStorage */
const STORE_PREFIX = 'covey.atts.';
const KEEP_MS = 6 * 86400000;
const THUMB_PX = 240;
const EMPTY: Attachment[] = [];
const lists = new Map<string, Attachment[]>();
const blobs = new Map<string, File>(); // 附件 key -> 原文件（看大图、重传用；刷新后就没有了）
const listeners = new Set<() => void>();
const emit = (): void => listeners.forEach((l: () => void) => l());

function loadList(conv: string): Attachment[] {
  try {
    const raw: string | null = window.localStorage.getItem(STORE_PREFIX + conv);
    const v: unknown = raw ? JSON.parse(raw) : [];
    return (Array.isArray(v) ? (v as Attachment[]) : []).filter((a: Attachment) => a.phase === 'done' && a.file && Date.now() - (a.at ?? 0) < KEEP_MS);
  } catch {
    return [];
  }
}
function saveList(conv: string, items: Attachment[]): void {
  try {
    const kept: Attachment[] = items.filter((a: Attachment) => a.phase === 'done' && a.file);
    if (kept.length) window.localStorage.setItem(STORE_PREFIX + conv, JSON.stringify(kept));
    else window.localStorage.removeItem(STORE_PREFIX + conv);
  } catch {
    /* 小图太多存不下：只在内存里 */
  }
}
export function getAttachments(conv: string): Attachment[] {
  if (!lists.has(conv)) lists.set(conv, loadList(conv));
  return lists.get(conv) ?? EMPTY;
}
function setList(conv: string, f: (cur: Attachment[]) => Attachment[]): void {
  const next: Attachment[] = f(getAttachments(conv));
  lists.set(conv, next);
  saveList(conv, next);
  emit();
}
const patch = (conv: string, key: string, p: Partial<Attachment>): void =>
  setList(conv, (cur: Attachment[]) => cur.map((a: Attachment) => (a.key === key ? { ...a, ...p } : a)));

/** 发出去的图在本页的样子（T933）：上传编号 -> 原文件（这次打开页面以来传的）；地址按需建、最多留 LOCAL_KEPT 个 */
const LOCAL_KEPT = 40;
const sentById = new Map<string, File>();
const sizes = new WeakMap<File, [number, number]>();
const localUrls = new Map<string, string>();

/** 刚发出的一张图的本地地址和尺寸：Covey 的图传上来之前，气泡和你那一行先用它，不用等。没有原文件（刷新过）时用草稿里存的小图 */
export function localImage(id: string): { src: string; w?: number; h?: number } | undefined {
  const f: File | undefined = sentById.get(id);
  if (!f) {
    for (const list of lists.values()) {
      const a: Attachment | undefined = list.find((x: Attachment) => x.file?.id === id && x.thumb);
      if (a?.thumb) return { src: a.thumb };
    }
    return undefined;
  }
  let src: string | undefined = localUrls.get(id);
  if (!src) {
    src = URL.createObjectURL(f);
    localUrls.set(id, src);
    if (localUrls.size > LOCAL_KEPT) {
      const [oldId, oldUrl] = localUrls.entries().next().value as [string, string];
      localUrls.delete(oldId);
      sentById.delete(oldId);
      URL.revokeObjectURL(oldUrl);
    }
  }
  const wh: [number, number] | undefined = sizes.get(f);
  return wh ? { src, w: wh[0], h: wh[1] } : { src };
}

/** 图片缩成长边 THUMB_PX 的 JPEG data URL（存得下）；读不了就 undefined */
function thumbOf(f: File): Promise<string | undefined> {
  return new Promise((resolve) => {
    const url: string = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => {
      if (img.naturalWidth && img.naturalHeight) sizes.set(f, [img.naturalWidth, img.naturalHeight]);
      const k: number = Math.min(1, THUMB_PX / Math.max(img.naturalWidth || 1, img.naturalHeight || 1));
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * k));
      c.height = Math.max(1, Math.round(img.naturalHeight * k));
      c.getContext('2d')?.drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      try {
        resolve(c.toDataURL('image/jpeg', 0.8));
      } catch {
        resolve(undefined);
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(undefined);
    };
    img.src = url;
  });
}

async function upload(conv: string, key: string, f: File, name: string): Promise<void> {
  patch(conv, key, { phase: 'uploading', progress: 0, error: undefined });
  try {
    const mime: string = f.type || 'application/octet-stream';
    const started = await withRetry(() => uploadStart({ name, mime, size: f.size }));
    const { id, parts } = started;
    // 每段多大照服务端说的（v44 part_bytes，按它的请求体上限放大，段数少了请求也少）；旧服务端不说，照 512 KiB
    const each: number = partBytesOf(started, f.size);
    for (let n = 0; n < parts; n += 1) {
      if (!blobs.has(key)) return; // 删掉了
      const [from, to] = partRange(n, each, f.size);
      const data: string = await base64Of(f.slice(from, to));
      await withRetry(() => uploadPart(id, { n, data }));
      patch(conv, key, { progress: (n + 1) / parts });
    }
    const file: SayFile = await withRetry(() => uploadDone(id));
    if (mime.startsWith('image/')) sentById.set(file.id, f);
    if (blobs.has(key)) patch(conv, key, { phase: 'done', progress: 1, file });
  } catch (e) {
    patch(conv, key, { phase: 'failed', error: errorText(e) });
  }
}

/** 附件的原文件（这次打开页面以来加的才有） */
export const blobOf = (key: string): File | undefined => blobs.get(key);

/** 一个对话（conv：话题、管家、负责人的 key）的草稿附件：add 选进来就开始传，remove 删掉，retry 重传失败的，clear 发出后清空 */
export function useAttachments(conv: string): {
  items: Attachment[];
  add: (files: FileList | File[]) => string;
  remove: (key: string) => void;
  retry: (key: string) => void;
  clear: () => void;
  ready: boolean;
  files: SayFile[];
} {
  const items: Attachment[] = useSyncExternalStore(
    (cb: () => void) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => getAttachments(conv),
  );
  const add = (list: FileList | File[]): string => {
    const files: File[] = Array.from(list);
    const room: number = UPLOAD_MAX_FILES - items.length;
    const notes: string[] = [];
    if (files.length > room) notes.push(`一句话最多 ${UPLOAD_MAX_FILES} 个附件，多的没加`);
    const next: Attachment[] = [];
    for (const f of files.slice(0, Math.max(0, room))) {
      if (f.size > UPLOAD_MAX_BYTES) {
        notes.push(`${f.name} 超过 ${UPLOAD_MAX_BYTES / 1024 / 1024} MB，没加`);
        continue;
      }
      if (!f.size) {
        notes.push(`${f.name} 是空文件，没加`);
        continue;
      }
      const key: string = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
      const image: boolean = f.type.startsWith('image/');
      const name: string = nameOf(f);
      blobs.set(key, f);
      next.push({ key, name, mime: f.type || 'application/octet-stream', size: f.size, image, phase: 'uploading', progress: 0, at: Date.now() });
      if (image) void thumbOf(f).then((thumb?: string) => thumb && blobs.has(key) && patch(conv, key, { thumb }));
      void upload(conv, key, f, name);
    }
    if (next.length) setList(conv, (cur: Attachment[]) => [...cur, ...next]);
    return notes.join('；');
  };
  const remove = (key: string): void => {
    blobs.delete(key);
    setList(conv, (cur: Attachment[]) => cur.filter((a: Attachment) => a.key !== key));
  };
  const retry = (key: string): void => {
    const f: File | undefined = blobs.get(key);
    const a: Attachment | undefined = items.find((x: Attachment) => x.key === key);
    if (f && a) void upload(conv, key, f, a.name);
  };
  const clear = (): void => {
    items.forEach((a: Attachment) => blobs.delete(a.key));
    setList(conv, () => []);
  };
  const ready: boolean = items.every((a: Attachment) => a.phase === 'done');
  const files: SayFile[] = items.filter((a: Attachment) => a.file).map((a: Attachment) => a.file as SayFile);
  return { items, add, remove, retry, clear, ready, files };
}

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronLeft, ChevronRight, ExternalLink, ImageOff, X } from 'lucide-react';
import { axiosForBackend } from '@lark-apaas/client-toolkit/utils/getAxiosForBackend';
import type { TurnImage } from '@shared/api.interface';

/** 一轮带的图片（Covey 数据约定 v19）：主人发的图（sent）、任务用 show_images 推给主人的图（shown）。
 *  图片只走本应用的 /api/images/:id（要登录、只有主人），页面拿不到本机路径，也没有公开直链。
 *  /api 每个请求都要带 CSRF 头，<img src> 带不上（现网 403），所以和别的接口一样用 axios 取成 blob，再给 <img> 一个 blob: 地址。 */
type Size = 'thumb' | 'full';
type Got = { url: string } | 'gone' | 'failed';
/** 一张要画的图：Covey 推来的按 id 取；刚发出、Covey 的图还没到的带本页的地址 src（T933），直接画 */
export type DrawnImage = TurnImage & { src?: string };
const KEPT = 300;
const loaded = new Map<string, Promise<Got>>();

function load(id: string, v: Size): Promise<Got> {
  const key = `${id}:${v}`;
  let got: Promise<Got> | undefined = loaded.get(key);
  if (got) return got;
  got = axiosForBackend({ url: `/api/images/${encodeURIComponent(id)}`, method: 'GET', params: { v }, responseType: 'blob' })
    .then((res: { data: unknown }): Got => ({ url: URL.createObjectURL(res.data as Blob) }))
    .catch((err: { response?: { status?: number } }): Got => {
      if (err?.response?.status === 404) return 'gone';
      loaded.delete(key); // 网络出错：下次再取
      return 'failed';
    });
  loaded.set(key, got);
  if (loaded.size > KEPT) {
    const first = loaded.entries().next().value as [string, Promise<Got>];
    loaded.delete(first[0]);
    void first[1].then((g: Got) => (typeof g === 'object' ? URL.revokeObjectURL(g.url) : undefined));
  }
  return got;
}

/** 一张图的 blob: 地址；``on`` 为 false 时先不取（还没滚到眼前） */
function useImage(id: string | undefined, v: Size, on = true): Got | null {
  const [got, setGot] = useState<Got | null>(null);
  useEffect(() => {
    if (!id || !on) return undefined;
    let live = true;
    setGot(null);
    void load(id, v).then((g: Got) => live && setGot(g));
    return () => {
      live = false;
    };
  }, [id, v, on]);
  return id ? got : 'gone';
}

/** 元素进入视口附近才为 true（缩略图不一次全取） */
function useNear<T extends Element>(): [React.RefObject<T>, boolean] {
  const ref = useRef<T>(null);
  const [near, setNear] = useState<boolean>(false);
  useEffect(() => {
    const el: T | null = ref.current;
    if (!el || near) return undefined;
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true);
      return undefined;
    }
    const seen = new IntersectionObserver((rows: IntersectionObserverEntry[]) => {
      if (rows.some((r: IntersectionObserverEntry) => r.isIntersecting)) setNear(true);
    }, { rootMargin: '600px 0px' });
    seen.observe(el);
    return () => seen.disconnect();
  }, [near]);
  return [ref, near];
}

/** “附件：图片 N 张”那一行：N 张图都画在下面时就不再写；有文件、下载失败或图没传上来的照留 */
export function withoutImageLine(text: string, images?: TurnImage[]): string {
  const sent: number = (images ?? []).filter((i: TurnImage) => (i.kind ?? 'sent') === 'sent').length;
  if (!sent) return text;
  return text.replace(/\n?附件：图片 (\d+) 张(?=$|\n)/u, (line: string, n: string) => (Number(n) === sent ? '' : line)).trimEnd();
}

/** 过期了（Covey 说 gone）或网页上已经没有了（取图 404）都画成“已过期” */
const Gone: React.FC = () => (
  <span className="ti-gone"><ImageOff />已过期</span>
);

const Tile: React.FC<{ img: DrawnImage; cls: string; style?: React.CSSProperties; onOpen: () => void; more?: number }> = ({ img, cls, style, onOpen, more }) => {
  const [ref, near] = useNear<HTMLButtonElement>();
  const fetched: Got | null = useImage(img.gone || img.src ? undefined : img.id, 'thumb', near && !img.src);
  const got: Got | null = img.src ? { url: img.src } : fetched;
  const gone: boolean = got === 'gone';
  return (
    <button ref={ref} type="button" className={`ti ${cls}${gone ? ' gone' : ''}`} style={style} onClick={onOpen} aria-label={gone ? '图片已过期' : img.note || '看大图'}
      title={gone ? '网页上只留 90 天，飞书里还能看' : undefined}>
      {gone ? <Gone /> : got === 'failed' ? (
        <span className="ti-gone"><ImageOff />没加载出来</span>
      ) : got && typeof got === 'object' ? (
        <img src={got.url} alt={img.note || ''} decoding="async" draggable={false} />
      ) : null}
      {more ? <span className="ti-more">+{more}</span> : null}
    </button>
  );
};

/** 主人消息里的图（sent），主人 11-20 选的方案 A：一张按原比例（宽图最宽 360、竖图最宽 200）；2、4 张两列方块；
 *  其余三列方块，超过 6 张第 6 格写 +N，点开都能翻到 */
export const SentImages: React.FC<{ images: DrawnImage[] }> = ({ images }) => {
  const [at, setAt] = useState<number | null>(null);
  let body: React.ReactNode;
  if (images.length === 1) {
    const one: DrawnImage = images[0];
    const r: number = one.w && one.h ? one.w / one.h : 4 / 3;
    body = (
      <div className="tis tis-a">
        <Tile img={one} cls="one" style={{ aspectRatio: String(Math.min(Math.max(r, 0.75), 2.4)), width: one.gone || (!one.id && !one.src) ? 'min(100%, 160px)' : r >= 1 ? 'min(100%, 360px)' : 'min(100%, 200px)' }} onOpen={() => setAt(0)} />
      </div>
    );
  } else {
    const shown: DrawnImage[] = images.slice(0, 6);
    body = (
      <div className={`tis tis-a grid${images.length === 2 || images.length === 4 ? ' two' : ''}`}>
        {shown.map((img: DrawnImage, i: number) => (
          <Tile key={`${img.id}-${i}`} img={img} cls="sq" onOpen={() => setAt(i)} more={i === shown.length - 1 && images.length > 6 ? images.length - 6 : 0} />
        ))}
      </div>
    );
  }
  return (
    <>
      {body}
      {at !== null ? <Lightbox images={images} at={at} onAt={setAt} onClose={() => setAt(null)} /> : null}
    </>
  );
};

/** 任务推来的一张图：高 200，宽按比例（比例夹在 0.75–2.4，二维码、竖截图都不裁太多） */
const figSize = (img: TurnImage): React.CSSProperties => {
  const r: number = img.w && img.h ? Math.min(Math.max(img.w / img.h, 0.75), 2.4) : 4 / 3;
  return { width: `${Math.round(200 * r)}px`, ['--r' as string]: String(r) } as React.CSSProperties;
};

/** 任务推给主人看的图（shown）：每张带它的一句说明，排在回答下面 */
export const ShownImages: React.FC<{ images: TurnImage[] }> = ({ images }) => {
  const [at, setAt] = useState<number | null>(null);
  return (
    <>
      <div className="tis-shown">
        {images.map((img: TurnImage, i: number) => (
          <figure key={`${img.id}-${i}`} className="tis-fig">
            <Tile img={img} cls="fig" style={figSize(img)} onOpen={() => setAt(i)} />
            {img.note ? <figcaption>{img.note}</figcaption> : null}
          </figure>
        ))}
      </div>
      {at !== null ? <Lightbox images={images} at={at} onAt={setAt} onClose={() => setAt(null)} /> : null}
    </>
  );
};

/** 看大图：全屏遮罩，左右切换（← → 键、两侧按钮、下方缩略条），Esc 或点空白处关 */
export const Lightbox: React.FC<{ images: DrawnImage[]; at: number; onAt: (i: number) => void; onClose: () => void }> = ({ images, at, onAt, onClose }) => {
  const img: DrawnImage = images[at];
  const n: number = images.length;
  const fetched: Got | null = useImage(img.gone || img.src ? undefined : img.id, 'full', !img.src);
  const got: Got | null = img.src ? { url: img.src } : fetched;
  const gone: boolean = got === 'gone';
  const go = useCallback((d: number) => onAt((at + d + n) % n), [at, n, onAt]);
  useEffect(() => {
    const key = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft' && n > 1) go(-1);
      else if (e.key === 'ArrowRight' && n > 1) go(1);
    };
    window.addEventListener('keydown', key);
    const overflow: string = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', key);
      document.body.style.overflow = overflow;
    };
  }, [go, n, onClose]);
  return createPortal(
    <div className="lbx" role="dialog" aria-modal="true" aria-label="看大图" onClick={onClose}>
      <div className="lbx-top" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <span className="lbx-n">{n > 1 ? `${at + 1} / ${n}` : ''}</span>
        <span className="lbx-note">{img.note || (img.w && img.h ? `${img.w} × ${img.h}` : '')}</span>
        {got && typeof got === 'object' ? (
          <a className="lbx-btn" href={got.url} target="_blank" rel="noreferrer" title="新标签页打开">
            <ExternalLink />
          </a>
        ) : null}
        <button type="button" className="lbx-btn" onClick={onClose} title="关闭（Esc）" aria-label="关闭">
          <X />
        </button>
      </div>
      <div className="lbx-stage">
        {gone ? (
          <span className="lbx-gone" onClick={(e: React.MouseEvent) => e.stopPropagation()}><ImageOff />这张图已过期，网页上只留 90 天；飞书里还能看</span>
        ) : got === 'failed' ? (
          <span className="lbx-gone" onClick={(e: React.MouseEvent) => e.stopPropagation()}><ImageOff />没加载出来，稍后再点开试试</span>
        ) : got && typeof got === 'object' ? (
          <img key={img.id} src={got.url} alt={img.note || ''} onClick={(e: React.MouseEvent) => e.stopPropagation()} />
        ) : (
          <span className="lbx-gone">加载中…</span>
        )}
      </div>
      {n > 1 ? (
        <>
          <button type="button" className="lbx-nav l" onClick={(e: React.MouseEvent) => { e.stopPropagation(); go(-1); }} aria-label="上一张"><ChevronLeft /></button>
          <button type="button" className="lbx-nav r" onClick={(e: React.MouseEvent) => { e.stopPropagation(); go(1); }} aria-label="下一张"><ChevronRight /></button>
          <div className="lbx-strip" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
            {images.map((x: DrawnImage, i: number) => (
              <button type="button" key={`${x.id}-${i}`} className={i === at ? 'on' : ''} onClick={() => onAt(i)} aria-label={`第 ${i + 1} 张`}>
                <StripThumb img={x} />
              </button>
            ))}
          </div>
        </>
      ) : null}
    </div>,
    document.body,
  );
};

const StripThumb: React.FC<{ img: DrawnImage }> = ({ img }) => {
  const fetched: Got | null = useImage(img.gone || img.src ? undefined : img.id, 'thumb', !img.src);
  const got: Got | null = img.src ? { url: img.src } : fetched;
  return got && typeof got === 'object' ? <img src={got.url} alt="" draggable={false} /> : <ImageOff />;
};

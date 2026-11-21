"""网页上传的附件 (docs/web.md, COVEY_WEB=1; data contract v21): the files the owner attached to a ``say`` on the page, taken from the page's
/openapi in parts and saved in the topic's incoming_media, where a Feishu message's attachments are saved (app/app.py), so the turn's
words list them as Feishu's do (core/attachments.py attachment_block: an image is 图片 N and reaches the model as an image, any other file is
文件 N with its name) and the page shows them back as the turn's images (data contract v19).

A file that cannot be taken whole (gone, not finished, bigger than it said or than MAX_BYTES) fails the whole ``say``: no turn starts with half
of what the owner sent. Nothing is kept of the page's copy here but the saved file."""
from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path

from covey.core.attachments import ListedAttachment, attachment_block

MAX_FILES = 9
MAX_BYTES = 20 * 1024 * 1024
UPLOAD_ID = re.compile(r"[0-9a-f]{16,64}")
UNSAFE = re.compile(r"[^\w.\-（）()一-鿿]+")
NAME_CHARS = 80


def safe_name(name, mime="") -> str:
    """A file name safe in a directory: the last part of ``name``, odd characters made ``_``, at most NAME_CHARS; an image without its
    extension gets the one of its type."""
    base = UNSAFE.sub("_", Path(str(name or "")).name).strip("._") or "file"
    if "." not in base and mime:
        base += mimetypes.guess_extension(mime) or ""
    stem, dot, ext = base.rpartition(".")
    if len(base) > NAME_CHARS:
        base = (stem[:NAME_CHARS - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:NAME_CHARS]
    return base


def listed(files) -> list:
    """The page's ``files`` of a say, checked: [{id, name, mime, size}]; ValueError says what is wrong."""
    if files in (None, ""):
        return []
    if not isinstance(files, list):
        raise ValueError("附件的格式不对")
    if len(files) > MAX_FILES:
        raise ValueError(f"一句话最多带 {MAX_FILES} 个附件")
    out = []
    for each in files:
        if not isinstance(each, dict) or not UPLOAD_ID.fullmatch(str(each.get("id") or "")):
            raise ValueError("附件的编号不对")
        out.append({"id": str(each["id"]), "name": str(each.get("name") or "")[:200], "mime": str(each.get("mime") or "")[:100]})
    return out


def fetch(request, media_dir, files) -> list:
    """Each of ``files`` (``listed``) taken from the page with ``request`` (app/web_push_client.py Client.request) and saved under
    ``media_dir``: ListedAttachment each, in order. ValueError names the file that could not be taken and why."""
    from covey.app.web_push_client import WebError
    media_dir = Path(media_dir)
    saved = []
    for each in files:
        name = each["name"] or "附件"
        try:
            meta = request("GET", f"/sync/upload?id={each['id']}")
            size, parts = int(meta.get("size") or 0), int(meta.get("parts") or 0)
            if not meta.get("done") or parts <= 0:
                raise ValueError(f"附件 {name} 还没传完")
            if size > MAX_BYTES:
                raise ValueError(f"附件 {name} 超过 {MAX_BYTES // 1024 // 1024} MB")
            chunks, total = [], 0
            for n in range(parts):
                data = base64.b64decode(str(request("GET", f"/sync/upload/part?id={each['id']}&n={n}").get("data") or ""), validate=True)
                total += len(data)
                if total > size:
                    raise ValueError(f"附件 {name} 比它说的大")
                chunks.append(data)
            if total != size:
                raise ValueError(f"附件 {name} 不完整")
        except WebError as exc:
            raise ValueError(f"附件 {name} 取不到：" + ("已过期或没传完" if exc.status == 404 else str(exc))) from None
        except (TypeError, ValueError) as exc:
            if str(exc).startswith("附件 "):
                raise
            raise ValueError(f"附件 {name} 取不到：内容不对") from None
        mime = meta.get("mime") or each["mime"]
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"web-{each['id'][:16]}-{safe_name(meta.get('name') or name, mime)}"
        path.write_bytes(b"".join(chunks))
        kind = "image" if str(mime).startswith("image/") else "file"
        saved.append(ListedAttachment(kind, "" if kind == "image" else str(meta.get("name") or name), str(path)))
    return saved


def with_files(text, items) -> str:
    """The owner's words followed by the attachments block, as a Feishu message's model text has it."""
    block = "\n".join(attachment_block(items))
    return "\n\n".join(part for part in (str(text or "").strip(), block) if part)

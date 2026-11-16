"""Image attachments a Harness hands its model itself (Claude Code's stream-json input, pi's prompt)."""
from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from covey.core.paths import expand_user

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def encoded_size(size):
    """The bytes ``size`` bytes take in base64."""
    return 4 * ((size + 2) // 3)


def fit_images(attachments, *, limit=0, frame=0, used=0):
    """The attachments a Harness takes whole, in order, and the ones it would refuse: (kept, skipped). ``limit``: one
    image's bytes; ``frame``: what the request's text (``used`` bytes) and its base64 images may add up to; 0 is no
    limit. A skipped item is a copy with its ``size`` and ``reason`` ('image' or 'frame'). A file whose size cannot be
    read is kept: the Harness's own read reports it, as before."""
    kept, skipped = [], []
    for item in attachments:
        try:
            size = Path(item["path"]).stat().st_size
        except OSError:
            kept.append(item)
            continue
        if limit and size > limit:
            skipped.append({**item, "size": size, "reason": "image"})
        elif frame and used + encoded_size(size) > frame:
            skipped.append({**item, "size": size, "reason": "frame"})
        else:
            used += encoded_size(size)
            kept.append(item)
    return kept, skipped


def read_images(attachments, *, limit):
    """(mime, base64 data) of each attachment, read as the caller iterates, so a caller's own check stops it before
    the next file is opened. ValueError for a type not in IMAGE_TYPES, a symlink, an empty file or one over ``limit``
    bytes; OSError when the file cannot be read."""
    for item in attachments:
        path = Path(item["path"])
        mime = item.get("mime")
        if mime not in IMAGE_TYPES or path.is_symlink():
            raise ValueError("Unsupported image attachment")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError("Image attachment exceeds size limit")
        yield mime, base64.b64encode(data).decode()


# An image a request names is forwarded up to this size; MAX_IMAGE_BYTES is what a Harness takes whole.
FORWARDED_IMAGE_BYTES = 20 * 1024 * 1024


def path_candidates(prompt: str) -> list[str]:
    """The words of ``prompt`` that may name an attachment's path: anything in backquotes, and what follows 图片: / 文件:
    in brackets, in the order written (a word that is no path is for the caller to skip)."""
    return (re.findall(r"`([^`\n]+)`", prompt)
            + re.findall(r"\[(?:被回复)?(?:图片|文件): ([^\]\n]+)\]", prompt))


def referenced_files(prompt: str, *media_dirs: Path) -> list[tuple[Path, list[str]]]:
    """Each file saved in one of ``media_dirs`` that ``prompt`` names by its absolute path (``path_candidates``), of any
    type and size: ``(resolved path, the words that name it)``, once each, in the order first named. A symbolic link
    that leads out of its directory, a directory and a path that cannot be read are left out, as
    ``native_image_attachments`` does."""
    roots = [Path(media).resolve() for media in media_dirs]
    found: dict[Path, list[str]] = {}
    for raw in path_candidates(prompt):
        path = expand_user(raw)
        if path is None or not path.is_absolute():
            continue
        try:
            path = path.resolve()
            if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
                continue
        except (OSError, ValueError):
            continue
        words = found.setdefault(path, [])
        if raw not in words:
            words.append(raw)
    return list(found.items())


def native_image_attachments(prompt: str, media_dir: Path) -> list[dict[str, str]]:
    """The images saved in ``media_dir`` this request names by their absolute path (in backquotes, or after 图片: /
    文件:), each once, of IMAGE_TYPES and at most FORWARDED_IMAGE_BYTES: {path, mime} each, for the Harness to hand its
    model. Only forward downloaded images explicitly referenced in this request."""
    root = media_dir.resolve()
    result = []
    seen = set()
    for raw in path_candidates(prompt):
        # Any backquoted words are a candidate: one that is no path (core/paths.py expand_user) or cannot be read is
        # skipped, never the turn's failure.
        path = expand_user(raw)
        if path is None or not path.is_absolute():
            continue
        try:
            path = path.resolve()
            if path in seen or not path.is_relative_to(root) or not path.is_file():
                continue
            size = path.stat().st_size
        except (OSError, ValueError):
            continue
        mime = mimetypes.guess_type(path.name)[0]
        if mime in IMAGE_TYPES and 0 < size <= FORWARDED_IMAGE_BYTES:
            result.append({'path': str(path), 'mime': mime})
            seen.add(path)
    return result


# The text a received message's attachments and the Bridge's other blocks are listed in for the model (feishu/media.py
# enrich_incoming_event writes it; the butler's handoff and topics read it back).
ATTACHMENTS_HEADER = "收到的附件（已下载到本地）："
ATTACHMENTS_HINT = "请在需要理解图片或文件内容时读取上述本地路径。"
FAILED_HEADER = "附件下载失败（内容未传给模型）："
# One downloaded attachment under ATTACHMENTS_HEADER, as ``attachment_line`` writes it.
ATTACHMENT_LINE_RE = re.compile(r"- (图片|文件) \d+(?:（(.*)）)?: `([^`\n]+)`")


@dataclass(frozen=True)
class ListedAttachment:
    """One attachment a message's model text lists under ATTACHMENTS_HEADER: "image" or "file", its file name as
    the message gave it ('' when none), where it was saved."""
    kind: str
    name: str
    path: str


def attachment_line(kind: str, number: int, path, name: str = "") -> str:
    """The line ATTACHMENTS_HEADER lists one attachment on; ``parse_attachment_block`` reads it back."""
    return f"- {'图片' if kind == 'image' else '文件'} {number}" + (f"（{name}）" if name else "") + f": `{path}`"


def attachment_block(items) -> list[str]:
    """The lines a message's model text lists ``items`` (ListedAttachment) on, numbered per kind: the header, one
    line each and the hint; [] for none. The paths are in backquotes, which native_image_attachments looks for."""
    if not items:
        return []
    counts = {"image": 0, "file": 0}
    lines = [ATTACHMENTS_HEADER]
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        lines.append(attachment_line(item.kind, counts[item.kind], item.path, item.name))
    return [*lines, ATTACHMENTS_HINT]


def parse_attachment_block(text: str) -> list[ListedAttachment]:
    """Every attachment the ATTACHMENTS_HEADER blocks of ``text`` list (``attachment_block``, the lines
    enrich_incoming_event writes), in order; blank lines between them are skipped."""
    found, inside = [], False
    for line in str(text or "").split("\n"):
        line = line.strip()
        if line == ATTACHMENTS_HEADER:
            inside = True
            continue
        if not inside or not line:
            continue
        match = ATTACHMENT_LINE_RE.fullmatch(line)
        if not match:
            inside = False
            continue
        found.append(ListedAttachment("image" if match[1] == "图片" else "file", match[2] or "", match[3]))
    return found


# The blocks enrich_incoming_event adds after what the user wrote, each on a line of its own.
_ADDED_BLOCKS = re.compile(r"\n[ \t]*(?:" + "|".join(re.escape(h) for h in (ATTACHMENTS_HEADER, FAILED_HEADER))
                           + r"|提及信息[：:]|回复上下文[：:]|话题起始消息（背景参考）[：:]|消息中有 \d+ 个飞书文档链接)")


def message_words(text: str) -> str:
    """What the user wrote of a message's model text: the text before the blocks the Bridge added to it
    (attachments, failed downloads, the mentions with their open_ids, render_mentions, the quoted message, the
    document-link note)."""
    return _ADDED_BLOCKS.split("\n" + str(text or ""), 1)[0].strip()


def attachment_summary(images: int, files: int, failed: int, names: list[str] = ()) -> str:
    """What the bridge received, in words: never the raw keys or rendered tags."""
    parts = ([f"图片 {images} 张"] if images else []) + ([f"文件 {files} 个"] if files else [])
    text = "、".join(parts)
    if names:
        text += "（" + "、".join(names[:3]) + ("等" if len(names) > 3 else "") + "）"
    if failed:
        text += ("，" if text else "") + f"{failed} 个下载失败"
    return text


FEISHU_DOC_RE = re.compile(r"https?://[A-Za-z0-9.-]*(?:feishu\.cn|larksuite\.com|larkoffice\.com)/"  # privacy: allow 飞书公开的文档域名，不是内部名
                           r"(?:docx|docs|wiki|sheets|base|mindnotes|slides|file|drive/folder)/[A-Za-z0-9_-]+")


def document_link_note(text: str) -> str:
    """A Feishu document link is an address and a title, not the document."""
    links = list(dict.fromkeys(FEISHU_DOC_RE.findall(text)))
    if not links:
        return ""
    return (f"消息中有 {len(links)} 个飞书文档链接。桥接只取得了链接和标题，没有读取文档正文；"
            "需要正文时请按当前可用的权限读取（例如 lark-cli docs +fetch），读不到或无权限时如实说明，不要按标题推测内容。")

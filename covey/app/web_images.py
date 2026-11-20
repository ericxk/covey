"""The images of a turn on the web page (docs/butler.md 网页工作台, COVEY_WEB=1, data contract v19): the ones the owner sent with his
words and the ones a task showed him (its checkpoint's ``show_images``), sent up once each, and the turn says which by id alone.

- Which: an owner line's images are the files its words list under "收到的附件" (feishu/media.py) or name in plain words (a hand-on
  that quotes a screenshot), and only files in a topic's ``incoming_media/`` of this state directory; a task's are its checkpoint's
  ``show_images`` (app/turn_images.py ``wanted``), none of a task on a device. At most PER_TURN a turn, none over MAX_SOURCE bytes.
- Sent: a thumbnail (long side THUMB_SIDE) and a large one (FULL_SIDE), WebP, each at most MAX_PART bytes so its request stays under the
  page's 1 MiB body limit; Pillow makes them. Without Pillow a PNG, JPEG, WebP or GIF of at most MAX_PART bytes goes up as it is, both
  sizes the same file; a larger one is not sent. The id is the SHA-256 of the file (its first ID_CHARS hex digits): the same image goes
  up once. The page keeps an image KEEP_DAYS after the message it came with (``expires_at``), and so does this side: an image of an
  older message is not sent and its ref says ``gone``.
- Never on the page: a local path. ``scrub`` takes them out of the words (the attachment blocks become their "附件：…" line).
- ``<state>/web/images.json``: the ids the page has (with their size and expiry) and which file was which id, so a restart neither
  sends nor hashes them again."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import time
from pathlib import Path

from covey.app.turn_images import wanted
from covey.core import web_sync
from covey.core.attachments import ATTACHMENT_LINE_RE, ATTACHMENTS_HEADER, ATTACHMENTS_HINT, FAILED_HEADER, attachment_summary
from covey.core.checkpoint import read_checkpoint
from covey.core.state import read_object, write_json

LEDGER_FILE = "images.json"
PER_TURN = 9
MAX_SOURCE = 20 * 1024 * 1024
MAX_PART = 700_000  # Bytes of one encoded image: base64 in its JSON request stays under the page's 1 MiB.
THUMB_SIDE, FULL_SIDE = 480, 2048
MAX_PIXELS = 60_000_000
ID_CHARS = 32
KEEP_DAYS = 90
UPLOADS_PER_STEP = 8  # Images sent in one push step, and at most UPLOAD_SECONDS of it: a step must keep moving (web_sync STUCK_SECONDS).
UPLOAD_SECONDS = 20.0
FILES_KEPT = 20000
BAD_KEPT = 2000
MEDIA = "incoming_media"
RAW_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
# A path to a received file in plain words (a hand-on's "截图: /…/incoming_media/x.png"), in backquotes or not.
PATH_RE = re.compile(r"`?(/[^\s`'\"<>()（）]*/" + MEDIA + r"/[^\s`'\"<>()（），。；]+)`?")
IMAGE_WORD = "（图片）"

try:  # Pillow is optional: without it only small images go up as they are.
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - depends on the machine
    Image = ImageOps = None


class BadImage(ValueError):
    """A file that is not sent: unreadable, not an image, too large."""


def scrub(text) -> str:
    """``text`` without local paths of received files: each "收到的附件" block becomes its "附件：…" line (none when the line before
    already is one, as a display text's is), a "附件下载失败" block the count of what failed, any other path to a received file
    IMAGE_WORD."""
    lines = str(text or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        head = line.strip()
        if head in (ATTACHMENTS_HEADER, FAILED_HEADER):
            images = files = failed = 0
            i += 1
            while i < len(lines):
                item = lines[i].strip()
                if not item:
                    i += 1
                    continue
                if head == ATTACHMENTS_HEADER and (match := ATTACHMENT_LINE_RE.fullmatch(item)):
                    images, files = (images + 1, files) if match[1] == "图片" else (images, files + 1)
                elif head == ATTACHMENTS_HEADER and item == ATTACHMENTS_HINT:
                    pass
                elif head == FAILED_HEADER and item.startswith("- "):
                    failed += 1
                else:
                    break
                i += 1
            summary = attachment_summary(images, files, failed)
            before = next((each for each in reversed(out) if each.strip()), "")
            if summary and not (head == ATTACHMENTS_HEADER and before.startswith("附件：")):
                out.append(f"附件：{summary}")
            continue
        out.append(PATH_RE.sub(IMAGE_WORD, line))
        i += 1
    return "\n".join(out).rstrip()


def media_paths(text, state) -> list[Path]:
    """The received images ``text`` names (its attachment blocks and plain paths), in order, once each: files in
    ``<state>/topics/<topic>/incoming_media/`` only."""
    root = Path(state).resolve() / "topics"
    found, seen = [], set()
    for raw in PATH_RE.findall(str(text or "")):
        path = Path(raw)
        if path.suffix.lower() not in RAW_TYPES and path.suffix.lower() not in (".bmp", ".tif", ".tiff"):
            continue
        try:
            real = path.resolve()
        except (OSError, RuntimeError):
            continue
        if real.parent.name != MEDIA or real.parent.parent.parent != root or real in seen:
            continue
        seen.add(real)
        found.append(real)
    return found[:PER_TURN]


def encode(path) -> tuple:
    """(width, height, thumb, full, mime) of the image at ``path``: WebP bytes from Pillow, or the file itself without it."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BadImage(type(exc).__name__) from None
    if not 0 < size <= MAX_SOURCE:
        raise BadImage("size")
    if Image is None:
        mime = RAW_TYPES.get(path.suffix.lower())
        if mime is None or size > MAX_PART:
            raise BadImage("no Pillow")
        data = path.read_bytes()
        return 0, 0, data, data, mime
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(path) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise BadImage(type(exc).__name__) from None
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode or "transparency" in image.info else "RGB")
    width, height = image.size
    return width, height, _webp(image, THUMB_SIDE, 80), _webp(image, FULL_SIDE, 85), "image/webp"


def _webp(image, side, quality) -> bytes:
    """``image`` within ``side`` × ``side`` as WebP, at ``quality`` or lower (and smaller) until it is at most MAX_PART bytes."""
    while True:
        copy = image.copy()
        copy.thumbnail((side, side))
        buffer = io.BytesIO()
        copy.save(buffer, "WEBP", quality=quality, method=4)
        data = buffer.getvalue()
        if len(data) <= MAX_PART or side < 256:
            return data
        if quality > 60:
            quality -= 10
        else:
            side = int(side * 0.8)


def file_id(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:ID_CHARS]


class WebImages:
    """The images of the turns one push thread builds: ``refs`` says what a turn carries and remembers what is still to be sent;
    ``upload`` sends some of it and says which topics to build again. One thread uses it."""

    def __init__(self, state, log=None) -> None:
        self.state = Path(state)
        self.log = log
        self.loaded = False
        self.ids = {}  # id -> [width, height, expires_at]: on the page.
        self.files = {}  # str(path) -> [size, mtime_ns, id]
        self.bad = {}  # id -> why it is not sent (not an image, too large, refused)
        self.pending = {}  # str(path) -> (topic, expires_at), in the order the turns asked
        self.checkpoints = {}  # (checkpoint path, mtime_ns) -> [(path, note)]
        self.named = {}  # an owner line's words -> media_paths of them (a hot topic is built every second: no path resolved again)
        self.backfilled = 0.0  # When every topic's turns were built once with their images (the first pass after this came).
        self.changed = False

    # -- the ledger -----------------------------------------------------------------------------------------------------

    def path(self) -> Path:
        return self.state / web_sync.FOLDER / LEDGER_FILE

    def load(self) -> None:
        self.loaded = True
        data = read_object(self.path())
        ids, files, bad = data.get("ids"), data.get("files"), data.get("bad")
        self.ids = {key: value for key, value in (ids if isinstance(ids, dict) else {}).items()
                    if isinstance(value, list) and len(value) == 3}
        self.files = {key: value for key, value in (files if isinstance(files, dict) else {}).items()
                      if isinstance(value, list) and len(value) == 3}
        self.bad = dict(bad) if isinstance(bad, dict) else {}
        self.backfilled = data.get("backfilled") if isinstance(data.get("backfilled"), (int, float)) else 0.0

    def save(self, now=None) -> None:
        if not self.changed:
            return
        now = time.time() if now is None else now
        self.ids = {key: value for key, value in self.ids.items() if (value[2] or now) >= now - 30 * 86400}
        if len(self.files) > FILES_KEPT:
            self.files = dict(list(self.files.items())[-FILES_KEPT:])
        if len(self.bad) > BAD_KEPT:
            self.bad = dict(list(self.bad.items())[-BAD_KEPT:])
        try:
            self.path().parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path(), {"ids": self.ids, "files": self.files, "bad": self.bad, "backfilled": self.backfilled})
            self.changed = False
        except OSError:
            pass

    def mark_backfilled(self, now) -> None:
        self.backfilled, self.changed = now, True
        self.save(now)

    # -- what a turn carries --------------------------------------------------------------------------------------------

    def sent(self, text, topic, at, now) -> list:
        """The refs of the images the owner's line ``text`` (said at ``at``) came with."""
        text = str(text or "")
        if "/" + MEDIA + "/" not in text:
            return []
        paths = self.named.get(text)
        if paths is None:
            if len(self.named) > 4096:
                self.named.clear()
            paths = self.named[text] = media_paths(text, self.state)
        return self.refs([(path, "") for path in paths], "sent", topic, at, now, stable=True)

    def shown(self, record, topic, at, now) -> list:
        """The refs of the images the task ``record`` showed its owner (its checkpoint's ``show_images``); none of a device's task."""
        raw = record.get("checkpoint_path") if isinstance(record, dict) else None
        if not raw or record.get("device"):
            return []
        path = Path(raw)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return []
        found = self.checkpoints.get((raw, stamp))
        if found is None:
            if len(self.checkpoints) > 4096:
                self.checkpoints.clear()
            found = self.checkpoints[(raw, stamp)] = [(each, note) for each, note, _ in wanted(read_checkpoint(path))]
        return self.refs(found[:PER_TURN], "shown", topic, at, now)

    def refs(self, images, kind, topic, at, now, stable=False) -> list:
        """[{id, w, h, kind, note?, gone?}] of the ``images`` ((path, note)) already on the page, or gone with their message (no
        id then); the others wait in ``pending`` (``upload``)."""
        if not self.loaded:
            self.load()
        expires = (at or now) + KEEP_DAYS * 86400
        out = []
        for path, note in images:
            if expires < now:  # Its message is older than KEEP_DAYS: the page has dropped it, nothing to hash or send.
                out.append({"id": "", "kind": kind, "gone": True, **({"note": note} if note else {})})
                continue
            ident = self.known(path, stable)
            if ident is None or ident in self.bad:
                continue
            ref = {"id": ident, "kind": kind}
            if note:
                ref["note"] = note
            have = self.ids.get(ident)
            if have is None:
                self.pending.setdefault(str(path), (topic, expires))
                continue
            if have[0] and have[1]:
                ref.update(w=have[0], h=have[1])
            out.append(ref)
        return out

    def known(self, path, stable=False):
        """The id of the file at ``path`` (hashed once per size and modification time); None for one that is not sent. ``stable``: a
        received file, never written again, so one hashed before is not even looked at."""
        if stable and str(path) in self.files:
            return self.files[str(path)][2]
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        key = str(path)
        if not 0 < stat.st_size <= MAX_SOURCE:
            return None
        row = self.files.get(key)
        if row and row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            return row[2]
        try:
            ident = file_id(path)
        except OSError:
            return None
        self.files[key] = [stat.st_size, stat.st_mtime_ns, ident]
        self.changed = True
        return ident

    # -- sending --------------------------------------------------------------------------------------------------------

    def send(self, client, raw, ident, expires, moved) -> None:
        """Both sizes of the image at ``raw`` to the page. BadImage: not an image to send, or one the page refused (400, 413)."""
        try:
            width, height, thumb, full, mime = encode(raw)
        except OSError as exc:
            raise BadImage(type(exc).__name__) from None
        for part, data in (("thumb", thumb), ("full", full)):
            try:
                client.request("POST", "/sync/image", {"id": ident, "v": part, "mime": mime, "w": width, "h": height,
                                                       "expires_at": int(expires), "data": base64.b64encode(data).decode()})
            except Exception as exc:
                if getattr(exc, "status", 0) in (400, 413):
                    raise BadImage(f"refused {exc.status}") from None
                raise
            if moved is not None:
                moved()
        self.ids[ident] = [width, height, int(expires)]
        self.changed = True

    def upload(self, client, now=None, moved=None) -> set:
        """Send up to UPLOADS_PER_STEP pending images (UPLOAD_SECONDS at most); the topics whose turns now have more of their
        images. Raises the client's error when the page refuses; what was sent before stays sent."""
        if not self.pending:
            return set()
        now = time.time() if now is None else now
        began = time.monotonic()
        batch = list(self.pending.items())[:UPLOADS_PER_STEP]
        idents = {}
        for raw, _ in batch:
            row = self.files.get(raw)
            if row:
                idents[raw] = row[2]
        have = set()
        asked = sorted({ident for ident in idents.values() if ident not in self.ids})
        if asked:
            answer = client.request("POST", "/sync/images/have", {"ids": asked})
            have = {str(each) for each in answer.get("have") or []}
        topics = set()
        for raw, (topic, expires) in batch:
            if time.monotonic() - began > UPLOAD_SECONDS:
                break
            ident = idents.get(raw)
            if ident is not None and ident not in self.ids and ident in have:
                self.ids[ident] = [0, 0, int(expires)]  # On the page already (sent before this ledger): its size is the page's.
                self.changed = True
            if ident is not None and ident not in self.ids:
                try:
                    self.send(client, raw, ident, expires, moved)
                except BadImage as exc:
                    self.bad[ident] = str(exc)[:80]
                    self.changed = True
                    ident = None
            self.pending.pop(raw, None)  # Only now: a request that failed leaves it to the next step.
            if ident is not None:
                topics.add(topic)
        self.save(now)
        return topics

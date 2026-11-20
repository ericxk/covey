"""Images a running task shows its owner at once (the checkpoint's ``show_images``, core/checkpoint.py
CHECKPOINT_RULES): a login QR code, a screenshot he has to act on.

The task card's 产物 go up when the turn ends; a QR code expires in minutes. A turn that sends its
QR codes itself with lark-cli replies to the root its history shows for the owner's last message, which may be a
top-level relay card: they land in that card's thread, not the topic's, and the owner never sees them. So the
Bridge sends them, where its own messages for the turn go (the task's context, ``ctx.reply_to_message_id``: the
topic's thread), each a line of what it is and the image itself (an image message: a phone reads a QR code in it).
One marked ``urgent``, and one whose thread send failed, also goes to the chat's top level, its line naming the
topic and linking its thread (the owner, 23:35: 需要主人马上处理的图片……同时发到私聊聊天区); the butler's
own go to the top level only (no thread under the owner's message, D1).

Each image is sent once a turn: its digest (path, size, modification time) is kept in the job record's
``shown_images``, so the same file written again is a new image, and a restart sends none twice. Sends run on a
CARD_UPDATES thread, which a task runner waits for before it exits (core/background.py); never under a lock."""
from __future__ import annotations

import dataclasses
import hashlib
import threading
from pathlib import Path

from covey.app import device_files
from covey.app.artifact_policy import device_policy
from covey.core import background
from covey.core.jobs import find_job_record, job_key, upsert_job_record
from covey.core.butler import is_butler_topic
from covey.core.topics import topic_label
from covey.feishu.outgoing_images import IMAGE_SUFFIXES

FIELD = "show_images"
SHOWN = "shown_images"  # The job record's digests of the images sent.
MOST = 5  # Images taken from one reading of the checkpoint.
MAX_BYTES = 10 * 1024 * 1024  # Feishu's limit for an image message.
NOTE_CHARS = 200


def wanted(checkpoint) -> list[tuple[Path, str, bool]]:
    """(path, note, urgent) of each image ``checkpoint``'s ``show_images`` lists: a path, or {"path", "note",
    "urgent"}; only an absolute path to an image file there, at most MAX_BYTES, at most MOST of them. Never raises."""
    found = []
    entries = checkpoint.get(FIELD) if isinstance(checkpoint, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = Path(entry["path"])
        try:
            ok = (path.is_absolute() and path.suffix.lower() in IMAGE_SUFFIXES and path.is_file()
                  and 0 < path.stat().st_size <= MAX_BYTES)
        except OSError:
            ok = False
        if ok:
            note = " ".join(str(entry.get("note") or "").split())[:NOTE_CHARS]
            found.append((path, note, entry.get("urgent") is True))
        if len(found) >= MOST:
            break
    return found


def digest(path) -> str:
    """The image's identity: its path, size and modification time (a file written again is another image). An image of a
    device (``RemoteImage``, app/device_files.py) has its own, from what the device said."""
    if isinstance(path, device_files.RemoteImage):
        return path.digest()
    stat = path.stat()
    return hashlib.sha256(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()[:16]


class TurnImages:
    """The ``images`` of a task's TaskMonitor (core/longtask.py): told each checkpoint it reads, it sends the images
    not sent yet."""

    def __init__(self, ctx, job, *, messenger, voice, log, link=None):
        self.ctx, self.job, self.messenger, self.voice, self.log = ctx, job, messenger, voice, log
        self.link = link  # (state_dir, topic_id) -> the applink of the topic's thread; None: app/topic_links.py.
        self.key = job_key(job.message_id, job.job_id)
        self.sent = None  # Read from the job record at the first checkpoint (``_sent``).
        self.lock = threading.Lock()

    def _sent(self) -> set:
        """The digests sent this turn, the job record's ``shown_images`` read once (a restarted runner's too).
        The caller holds the lock."""
        if self.sent is None:
            kept = (find_job_record(self.ctx.state_dir, self.key) or {}).get(SHOWN)
            self.sent = set(kept) if isinstance(kept, list) else set()
        return self.sent

    def __call__(self, checkpoint) -> None:
        """Start sending the images ``checkpoint`` lists that were not sent; each is claimed first, so two
        readings never send one twice. Never raises."""
        try:
            # On a device ``show_images`` names files of the device: looked at there, in one ``stat`` through the shim.
            found = self.remote_wanted(checkpoint) if getattr(self.ctx, "device", "") else wanted(checkpoint)
            for path, note, urgent in found:
                mark = digest(path)
                with self.lock:
                    sent = self._sent()
                    if mark in sent:
                        continue
                    sent.add(mark)
                    shown = sorted(sent)
                try:
                    upsert_job_record(self.ctx.state_dir, self.key, **{SHOWN: shown})
                except OSError as exc:
                    self.log(f"shown image not recorded job=#{self.job.job_id}: {type(exc).__name__}")
                background.start(background.CARD_UPDATES, self.send, path, note, urgent, mark)
        except Exception as exc:  # noqa: BLE001 - the turn goes on; its 产物 still list the file
            self.log(f"images not sent job=#{self.job.job_id}: {type(exc).__name__}")

    def remote_wanted(self, checkpoint):
        """``wanted`` for a device: (``RemoteImage``, note, urgent), the files looked at on the device (app/device_files.py)."""
        return device_files.wanted_images(self.ctx, checkpoint, device_policy(), field=FIELD, suffixes=IMAGE_SUFFIXES,
                                          most=MOST, max_bytes=MAX_BYTES, note_chars=NOTE_CHARS)

    def send(self, path, note, urgent, mark) -> None:
        """One image: in the turn's thread (a topic's), then at the top level when it is urgent, the thread send
        failed or the turn is the butler's. Each step's outcome is one log line. Never raises."""
        ctx, job, voice = self.ctx, self.job, self.voice
        chat, topic = str(job.chat_id or ""), str(job.topic_id or "")
        main = is_butler_topic(topic, chat)
        base = f"image-{job.message_id}-{mark}"
        try:
            if isinstance(path, device_files.RemoteImage):  # The hub's copy of the device's file is what is sent.
                path = Path(path.fetch(ctx.state_dir, self.key))
            thread = ""
            if not main:
                self.messenger.send_message(ctx.profile, chat, note or "图片", f"{base}-note", voice=voice,
                                            reply_to_message_id=ctx.reply_to_message_id)
                thread = self.messenger.send_image(ctx.profile, chat, path, f"{base}-thread", voice=voice,
                                                   reply_to_message_id=ctx.reply_to_message_id)
            top = ""
            if main or urgent or not thread:
                label = topic_label(Path(ctx.state_dir), topic) if not main else ""
                url = self.thread_link(topic) if label else ""
                line = " · ".join(part for part in (label, note or "图片") if part)
                if url:
                    line += f"（[到话题里看 ›]({url})）"
                top_ctx = dataclasses.replace(ctx, reply_to_message_id="")
                self.messenger.send_message(top_ctx.profile, chat, line, f"{base}-top-note", voice=voice)
                top = self.messenger.send_image(top_ctx.profile, chat, path, f"{base}-top", voice=voice)
            self.log(f"turn image job=#{job.job_id} {path.name} thread={thread[-8:] or '-'} top={top[-8:] or '-'}"
                     f" urgent={urgent}")
        except Exception as exc:  # noqa: BLE001 - logged; the checkpoint's file is still there
            self.log(f"turn image failed job=#{job.job_id} {path.name}: {type(exc).__name__}")

    def thread_link(self, topic) -> str:
        if self.link is not None:
            return self.link(self.ctx.state_dir, topic)
        from covey.app.topic_links import topic_thread_link

        return topic_thread_link(self.ctx.state_dir, topic, profile=self.ctx.profile)[0]

"""What a topic gets when the main thread hands it work (app/main_topics.py, docs/main-thread.md 交给话题的消息).

The model gets ``handoff_prompt``: the Bridge's note (which topic it is, that the main thread handed this on at the
owner's word and already did what the owner said about where it goes, that the Bridge sends its conclusion), the
main thread's task, the owner's words quoted and the attachments block. The card, the topic's history and the line
in its thread show the owner's words (``shown``, ``thread_line``): never the note, the main thread's task or a local
path.

The owner's words come from the main-thread turn itself (``asked``), not from the model: its job record (the message
and the supplements it took before it started) and the supplements it took while it ran (their delivery records,
core/deliveries.py). The attachments those list are linked or copied into the target topic's incoming_media
(``carry``), so they reach its model as they would from a message sent there (core/attachments.py
native_image_attachments, Codex's read-only roots); only files saved in the main thread's own incoming_media (or the
state directory's) are, as a listed path can come from a forwarded message's text.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from covey.core.attachments import (ATTACHMENTS_HEADER, FAILED_HEADER, ListedAttachment, attachment_block,
                                             attachment_summary, message_words, parse_attachment_block)
from covey.core.deliveries import CLOCK_SLACK, read_delivery
from covey.core.jobs import find_job_record
from covey.core.main_thread import main_topic_id
from covey.core.metrics import timings_of
from covey.core.state import ensure_private_dir, epoch_seconds

START, NEXT, STEER, FORK = "start", "next", "steer", "fork"
NOTE = "[Bridge 说明]"
# What app/steering.py merge_before_start puts before a supplement the main-thread turn took before it started.
MERGED = "\n\n用户在任务开始前补充：\n"


def _clock(at) -> str:
    return time.strftime("%H:%M", time.localtime(at)) if at else ""


@dataclasses.dataclass(frozen=True)
class Asked:
    """The owner's messages a main-thread turn answers: ``said``, (words, when in epoch seconds, 0 unknown) for the
    message it started with (and the supplements it took before it started) then each supplement it took while it
    ran, in order; the attachments they list, saved under ``roots`` (the main thread's incoming_media)."""
    said: tuple = ()
    attachments: tuple = ()
    roots: tuple = ()

    @property
    def words(self) -> str:
        return "\n".join(words for words, _ in self.said if words)

    @property
    def at(self) -> float:
        return self.said[0][1] if self.said else 0

    @property
    def clock(self) -> str:
        """When the owner said it: "HH:MM", the times of each message one after another ("13:40、13:42")."""
        clocks = []
        for _, at in self.said:
            if _clock(at) and _clock(at) not in clocks:
                clocks.append(_clock(at))
        return "、".join(clocks)


def _display_words(user_text, prompt):
    """The owner's words of a job's display text: without the "附件：…" line enrich_incoming_event adds when the
    message had attachments."""
    lines = str(user_text or "").strip().split("\n")
    if lines[-1].startswith("附件：") and (ATTACHMENTS_HEADER in prompt or FAILED_HEADER in prompt):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _inside(path, roots) -> bool:
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    return Path(path).is_absolute() and any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def _supplements(state_dir, record, job_key, since) -> list:
    """The delivery records of the supplements the main-thread job ``job_key`` took while it ran (sent, read, or held
    to be sent), in the order they came; merged ones are in its request already, ``requeue_auto`` ones a turn of their
    own (app/steering.py requeue_lost: the turn never took them), which asks them. Files last written before
    ``since`` (the turn's start, less CLOCK_SLACK) are skipped unread, as core/deliveries.py end_unread does."""
    topic = str(record.get("topic_id") or "")
    found = []
    since = float(since or 0) - CLOCK_SLACK
    for path in sorted((Path(state_dir) / "interaction-deliveries").glob("*.json")):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        delivery = read_delivery(path)
        if (isinstance(delivery, dict) and delivery.get("job_key") == job_key
                and (not topic or delivery.get("topic_id") == topic)
                and delivery.get("status") in {"sent", "read", "held"} and not delivery.get("merged")
                and not delivery.get("requeue_auto")):
            found.append(delivery)
    return sorted(found, key=lambda delivery: float(delivery.get("saved_at") or 0))


def asked(state_dir, job_key) -> Asked:
    """The owner's messages the main-thread job ``job_key`` answers, from its record and its supplements' delivery
    records; Asked() when its record is gone."""
    job_key = str(job_key or "")
    record = find_job_record(Path(state_dir), job_key) or {}
    if not record:
        return Asked()
    prompt = str(record.get("prompt_text") or record.get("user_text") or "")
    first, *merged = prompt.split(MERGED)
    words = [_display_words(record.get("user_text"), first)] + [message_words(part) for part in merged]
    timings = timings_of(record)
    at = epoch_seconds(record.get("created_at")) or next(
        (timings[stage] for stage in ("received", "queued") if isinstance(timings.get(stage), (int, float))), 0)
    said, texts = [("\n".join(word for word in words if word), at)], [prompt]
    for delivery in _supplements(state_dir, record, job_key, at):
        text = str(delivery.get("text") or "")
        said.append((message_words(text), float(delivery.get("saved_at") or 0)))
        texts.append(text)
    topic = str(record.get("topic_id") or main_topic_id(str(record.get("chat_id") or "")))
    roots = (Path(state_dir) / "topics" / topic / "incoming_media", Path(state_dir) / "incoming_media")
    items = [item for text in texts for item in parse_attachment_block(text) if _inside(item.path, roots)]
    return Asked(tuple(said), tuple(items), roots)


def choice_error(value) -> str:
    """Why the tool argument ``attachments`` is not one: "all", "none" or a list of 1-based numbers or names like
    "图片 1"; '' when it is."""
    if value is None or value in ("all", "none"):
        return ""
    if isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool)
                                       or isinstance(item, str) and re.fullmatch(r"(图片|文件)?\s*\d+", item.strip())
                                       for item in value):
        return ""
    return "attachments 只能是 \"all\"、\"none\" 或序号列表（如 [1, 2] 或 [\"图片 1\"]）。"


def picked(items, value) -> list:
    """The attachments of ``items`` the argument ``value`` (``choice_error`` says it is one) names: all of them by
    default; a bare number counts all of them in order, "图片 N" and "文件 N" each kind on its own."""
    items = list(items)
    if value is None or value == "all":
        return items
    if value == "none":
        return []
    numbered = {}
    for index, item in enumerate(items, 1):
        numbered[str(index)] = item
        kind = "图片" if item.kind == "image" else "文件"
        numbered[f"{kind}{sum(1 for other in items[:index] if other.kind == item.kind)}"] = item
    chosen = []
    for entry in value:
        item = numbered.get(re.sub(r"\s+", "", str(entry)))
        if item is not None and item not in chosen:
            chosen.append(item)
    return chosen


def carry(items, media_dir, roots) -> list:
    """``items`` linked (copied when a link cannot be made) into ``media_dir``, the target topic's incoming_media,
    under their own names; one already there is taken as it is, so a second try copies nothing. Only a file saved
    under one of ``roots`` (Asked.roots, the main thread's incoming_media) is: one elsewhere, or gone, is left out."""
    carried = []
    for item in items:
        if not _inside(item.path, roots):
            continue
        source = Path(item.path).resolve()
        if not source.is_file():
            continue
        target_dir = Path(media_dir)
        ensure_private_dir(target_dir)
        target = target_dir / source.name
        if target.exists() and target.stat().st_size != source.stat().st_size:
            digest = hashlib.sha256(str(source).encode()).hexdigest()[:8]
            target = target_dir / f"{source.stem}-{digest}{source.suffix}"
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                partial = target.with_name(target.name + ".part")
                shutil.copy2(source, partial)
                os.replace(partial, target)
        carried.append(ListedAttachment(item.kind, item.name, str(target)))
    return carried


def summary(items) -> str:
    """The attachments in words, as a message's display text says them ("图片 1 张、文件 1 个"); '' for none."""
    return attachment_summary(sum(1 for item in items if item.kind == "image"),
                              sum(1 for item in items if item.kind != "image"), 0) if items else ""


def shown(owner: Asked, task, items) -> str:
    """What the topic's card and history show of the turn: the owner's words (the main thread's task when the
    turn's record is gone), and the attachments it got in words."""
    words = owner.words or task
    return words + (f"\n附件：{summary(items)}" if items else "")


def thread_head(kind, owner: Asked) -> str:
    """"主线程转来你 17:54 的消息" ("的补充" for STEER): the head of the line the topic's thread gets."""
    when = f"你 {owner.clock} 的" if owner.clock else "你的"
    return f"主线程转来{when}补充" if kind == STEER else f"主线程转来{when}消息"


def thread_line(kind, owner: Asked, task, items) -> str:
    """The line the topic's thread gets when the main thread passes the owner's words on: no task, note or path."""
    return thread_head(kind, owner) + "：\n" + shown(owner, task, items)


def thread_card(kind, owner: Asked, task, items, url):
    """``thread_line`` as a card whose whole block opens ``url``, the owner's message at its place in the chat
    (cards/answer_card.py handoff_card); None without ``url``: the line goes as text, as before."""
    if not url:
        return None
    from covey.cards.answer_card import handoff_card

    return handoff_card(thread_head(kind, owner), shown(owner, task, items), url)


def _named(label, title):
    return f"{label}「{title}」" if label and title else label or (f"「{title}」" if title else "")


def _spaced(name):
    """``name`` set in Chinese text: a space on a side where it starts or ends with a T number."""
    return (" " if name[:1].isascii() else "") + name + (" " if name[-1:].isascii() else "")


def note(kind, *, label, title, owner: Asked, source="") -> str:
    """The Bridge's note at the head of what the topic's model gets, for ``kind`` (START, NEXT, STEER, FORK);
    ``source``: the topic a fork came from."""
    noun, where = "话题区", "私聊聊天区"
    named = _named(label, title)
    name = _spaced(named) if named else ""
    here = _spaced(label) if label else f"这个{noun}"
    who = f"主人 {owner.clock} " if owner.clock else "主人"
    done = "你的回复就是这一轮的结论，由 Bridge 发给主人。"
    settled = (f"原话里“开个话题区”“单开一个”“放到哪个 T 号里”“转给谁”这类安排主线程已经办完了，你就是那个{noun}："
               "直接做事，不要回应这些安排，不要再试图开话题或切话题，也不要说“开不了话题区”“切不到 T 号”。"
               "原话里如果还有别的事，主线程另有安排，你只做任务说明里的这件。")
    if kind == STEER:
        current = name or "本" + noun
        return (f"{NOTE} 主线程转来{who}的补充（主人是在{where}说的，不是在本{noun}里），你仍在{current}的这一轮里："
                f"把它并进手上的工作。其中“接着在{here}里做”“转给那个话题”这类安排主线程已经办完，"
                "不要再试图开话题或切话题。")
    if kind == NEXT:
        return (f"{NOTE} 你在{noun}{name}。主线程把{who}在{where}说的一条消息转给你，作为这个{noun}的下一轮；"
                "下面是主线程整理的任务说明和主人的原话（引用）。"
                f"原话里“接着放到{here}里做”“转给那个话题”这类安排主线程已经办完，你就在{here}里："
                f"直接做事，不要再试图开话题或切话题。{done}")
    opened = (f"主线程按{who}在{where}的一条消息，从 {source} 分叉出这个{noun}（带着 {source} 的上下文），"
              if kind == FORK and source else f"主线程按{who}在{where}的一条消息开了这个{noun}，")
    return (f"{NOTE} 你在{noun}{name}。{opened}把这件事交给你；下面先是主线程整理的任务说明，再是主人的原话（引用）。"
            f"{settled}{done}")


def handoff_prompt(kind, *, label, title, task, owner: Asked, items=(), extra="", source="") -> str:
    """What the topic's model gets: the Bridge's ``note``, the main thread's task (its ``extra`` line after it),
    the owner's words quoted, and the attachments block of ``items`` (those carried into the topic)."""
    parts = [note(kind, label=label, title=title, owner=owner, source=source),
             "任务说明（主线程整理）：\n" + task + (f"\n主线程补充：{extra}" if extra else "")]
    if owner.words or items:
        quoted = []
        for index, (words, at) in enumerate(owner.said):
            if index and not words:
                continue  # A supplement of attachments only: they are in the block below.
            head = "主人随后补充" if index else "主人原话"
            quoted.append(f"{head}（{_clock(at)}）：" if _clock(at) else f"{head}：")
            quoted += ["> " + line for line in words.split("\n")] if words else ["（只有附件，没有文字）"]
        if items:
            quoted.append(f"附件：{summary(items)}（见下方“收到的附件”）")
        parts.append("\n".join(quoted))
    if items:
        parts.append("\n".join(attachment_block(items)))
    return "\n\n".join(parts)

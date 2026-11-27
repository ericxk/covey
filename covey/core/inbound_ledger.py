"""主人消息的去向 (docs/architecture.md 主人消息的去向): where each message the owner sent went, as one read.

A message that becomes a turn is found by its id in the job records (the job key is the message id); one that went into
a running task is found by its job record's ``merged_into_job_id`` or its supplement's delivery record. What no such record
says is written here, a small JSON in the message's own marker of ``inbound-messages/<sha256 of the id>`` (the empty file
app/card_routing.py claims against a second delivery; a restore does not hold it, maintenance drops it after 7 days): the
fates ``ignored``, ``refused``, ``answer``, ``held_note``, ``stop_word``, ``supplement``, ``job``, ``not_queued``, ``error``
and ``lost``, with a reason.

Reading is read-only: no lock, no Feishu request, nothing created. Writing (``received``, ``note``) is best effort, never
raises, holds only a process-local lock for the one file and sends nothing (rules 1 and 2). A message whose marker holds no
fate and no job after ``STALE_SECONDS`` was taken in and never handled (a restart in between): ``where`` says so.

Command line: ``bin/covey_inbound.py where <message id> --state <dir>`` and ``bin/covey_inbound.py recent --state <dir>``."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from covey.core.deliveries import delivery_path, read_delivery
from covey.core.jobs import job_file, read_job_record
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json
from covey.core.store import directory_store
from covey.core.topics import label_title

INBOUND_DIR = "inbound-messages"
STALE_SECONDS = 120  # Taken in, and neither a job nor a fate after this long: it was never handled.
PREVIEW = 40
RECENT = 10
SCAN = 400  # Markers read at most by ``recent``, newest first.

# The fates written here, and what each says to the owner.
FATES = {
    "ignored": "没有处理",
    "refused": "被拒绝",
    "answer": "当作问题的回答",
    "held_note": "暂存，等问题答完一并交给话题",
    "stop_word": "当作停止词",
    "supplement": "作为补充送进在做的一轮",
    "job": "变成了一轮",
    "not_queued": "没有排上队",
    "error": "入队出错",
    "lost": "收下后没有处理完",
    "command": "当作命令处理",
    "extension": "交给扩展处理",
}
# Why a message was ignored, as Access.admit says it (app/access.py).
REASONS = {
    "not_mentioned": "群里没有 @ 机器人", "group_inactive": "这个群没有对成员开放", "not_owner": "不是主人的消息",
    "bot_sender": "机器人发的消息", "no_sender": "消息里没有发送人", "bot_identity_unknown": "还不知道机器人自己的身份",
    "pairing_in_group": "配对只能在私聊里做", "empty": "群里只有 @ 机器人、没有内容",
    "no_text": "消息里没有读得出的内容（不支持的消息类型）",
}


def marker(state_dir, message_id) -> Path:
    """The message's marker: the name app/card_routing.py claims against a duplicate."""
    return Path(state_dir) / INBOUND_DIR / hashlib.sha256(str(message_id).encode()).hexdigest()


def entry(state_dir, message_id) -> dict:
    """What the marker holds ({} for none, an empty one or one that is not an object). Read only."""
    try:
        value = read_json(marker(state_dir, message_id))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def preview(content) -> str:
    """The first words of a message as the event carries it (its text, or the ``text`` of a JSON content), one line."""
    text = str(content or "").strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
            text = str(value.get("text") or "") if isinstance(value, dict) else text
        except ValueError:
            pass
    text = " ".join(text.split())
    return text[:PREVIEW] + ("…" if len(text) > PREVIEW else "")


def note(state_dir, message_id, fate="", *, event=None, **fields) -> None:
    """Add ``fate`` and ``fields`` to the message's entry, creating it from ``event`` (the message as Feishu sent it) when
    the marker has none yet. Never raises."""
    if not state_dir or not message_id:
        return
    path = marker(state_dir, message_id)
    try:
        with resource_lock("inbound-ledger", str(path)):
            current = entry(state_dir, message_id)
            if not current:
                event = event or {}
                current = {"message_id": str(message_id), "chat_id": str(event.get("chat_id") or ""),
                           "thread_id": str(event.get("thread_id") or ""), "chat_type": str(event.get("chat_type") or ""),
                           "at": time.time(), "text": preview(event.get("content"))}
            if fate:
                current.update(fate=fate, fate_at=time.time())
            current.update({key: value for key, value in fields.items() if value not in (None, "")})
            write_json(path, current, ensure_ascii=False)
    except (OSError, ValueError, TypeError):
        pass  # The message is handled either way; only the answer of ``where`` is poorer.


def received(state_dir, event) -> None:
    """The message was claimed (app/card_routing.py): its entry, with no fate yet, and the message it quotes (``quoted``,
    WAVE12 C2: whether a top-level message answered a question card is told by it)."""
    quoted = str(event.get("parent_id") or event.get("reply_to") or "")
    note(state_dir, str(event.get("message_id") or event.get("id") or ""), event=event, **({"quoted": quoted} if quoted else {}))


def lost_before(state_dir, message_id, now=None) -> bool:
    """Whether the message was taken in earlier, long enough ago, and never handled (no fate, no job): a duplicate of
    it now, a replay after a restart, finds it so. The caller marks it ``lost`` and tells the owner once."""
    found = entry(state_dir, message_id)
    now = time.time() if now is None else now
    return bool(found) and not found.get("fate") and now - float(found.get("at") or now) > STALE_SECONDS \
        and not job_record(state_dir, message_id)


def job_record(state_dir, key) -> dict | None:
    """The job record whose key is ``key`` (a message id), read only; None for none."""
    try:
        return read_job_record(job_file(state_dir, str(key)))
    except (OSError, ValueError):
        return None


def job_by_number(state_dir, number) -> dict | None:
    try:
        rows = directory_store(Path(state_dir) / "jobs").query(job_id=int(number))
    except (OSError, ValueError, TypeError):
        return None
    return rows[0][1] if rows else None


def place(state_dir, topic_id, chat_id="") -> str:
    """Where a topic is named: ``T12「标题」``, or 管家 for the butler's (it has no number)."""
    number, title = label_title(Path(state_dir), str(topic_id or ""))
    if not number:
        return "管家"
    return f"{number}「{title[:24]}」" if title else number


def turn_words(state_dir, record) -> str:
    """``T12「标题」的任务 #34（状态）`` of a job record."""
    status = str(record.get("status") or "")
    where = place(state_dir, record.get("topic_id"))
    return f"{where}的任务 #{record.get('job_id')}" + (f"（{status}）" if status else "")


def where(state_dir, message_id, now=None) -> dict:
    """Where the owner's message ``message_id`` went: ``{"message_id", "kind", "says", ...}``. ``kind``: ``round`` (a turn of
    its own: ``job_id``, ``topic``, ``status``), ``merged`` (into a running or queued task's request: ``into``),
    ``answer``, ``held_note``, ``stop_word``, ``supplement``, ``ignored``, ``refused``, ``not_queued``, ``error``,
    ``lost`` (taken in, never handled), ``pending`` (taken in just now) or ``unknown`` (nothing is known of it).
    ``says`` is the sentence the owner reads. Read only."""
    state = Path(state_dir)
    now = time.time() if now is None else now
    found = entry(state, message_id)
    out = {"message_id": str(message_id), "kind": "unknown", "text": found.get("text", ""), "at": found.get("at", 0)}
    record = job_record(state, message_id)
    fate, reason = found.get("fate", ""), found.get("reason", "")
    if record is not None:
        out.update(job_id=record.get("job_id"), topic_id=record.get("topic_id", ""), status=str(record.get("status") or ""),
                   topic=place(state, record.get("topic_id")))
        into = record.get("merged_into_job_id")
        target = job_by_number(state, into) if into not in (None, "") else None
        if into not in (None, ""):
            out.update(kind="merged", into=into, into_topic=place(state, (target or {}).get("topic_id")))
            out["says"] = (f"没有单独成一轮：作为补充并进了{turn_words(state, target)}" if target else
                           f"没有单独成一轮：作为补充并进了任务 #{into}") + \
                (f"。{record.get('status_message')}" if record.get("status_message") else "")
        else:
            out.update(kind="round")
            out["says"] = "变成了" + turn_words(state, record)
            if record.get("status") == "cancelled" and record.get("status_message"):
                out["says"] += f"。{record['status_message']}"
        return out
    if fate in ("supplement", "job") and found.get("job_id"):
        topic_id = found.get("topic_id", "")
        out.update(kind=fate, job_id=found.get("job_id"), topic_id=topic_id, topic=place(state, topic_id))
        if fate == "supplement":
            saved = read_delivery(delivery_path(state, topic_id, str(message_id))) or {}
            shown = {"sent": "已送达", "read": "已读", "held": "任务还没准备好，稍后自动送入", "uncertain": "送达没有确认",
                     "refused": "被拒绝，没有送达"}.get(saved.get("status"), "")
            out["says"] = (f"作为补充送进了{out['topic']}在做的任务 #{found['job_id']}" + (f"（{shown}）" if shown else "")
                           + "，没有单独成一轮")
        else:
            out["says"] = f"变成了{out['topic']}的任务 #{found['job_id']}（任务记录已清理或还没写出）"
        return out
    if fate:
        out.update(kind=fate, topic=place(state, found.get("topic_id")) if found.get("topic_id") else "")
        base = FATES.get(fate, fate)
        why = REASONS.get(reason, reason)
        where_ = f"（{out['topic']}" + (f"的任务 #{found['job_id']}" if found.get("job_id") else "") + "）" if out["topic"] else ""
        out["says"] = f"{base}{where_}" + (f"：{why}" if why else "")
        return out
    if found:
        late = now - float(found.get("at") or now) > STALE_SECONDS
        out.update(kind="lost" if late else "pending")
        out["says"] = ("收下后没有处理完（Bridge 可能在这中间重启），没有变成任何一轮；请再发一次" if late
                       else "刚收到，正在处理")
        return out
    out["says"] = "查不到这条消息：不是主人发的、已过 7 天，或是这个版本之前收到的"
    return out


def recent(state_dir, count=RECENT) -> list[dict]:
    """The owner's last ``count`` messages with their places, newest first (those whose entry says it was taken in)."""
    folder = Path(state_dir) / INBOUND_DIR
    try:
        names = [item for item in os.scandir(folder) if item.is_file() and item.stat().st_size > 0]
    except OSError:
        return []
    names.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    rows = []
    for item in names[:SCAN]:
        try:
            value = read_json(Path(item.path))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("message_id"):
            rows.append(value)
    rows.sort(key=lambda value: float(value.get("at") or 0), reverse=True)
    return [where(state_dir, row["message_id"]) for row in rows[:max(1, int(count))]]


def render(row) -> str:
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(row["at"])) if row.get("at") else "--"
    shown = f"「{row['text']}」" if row.get("text") else ""
    return f"{when} {row['message_id']} {shown}\n    {row['says']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_inbound.py", description="查主人发的消息变成了哪一轮、哪个话题，或为什么没处理")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("where", help="查一条消息的去向")
    one.add_argument("message_id", help="消息 id（飞书的 om_… 或网页的 web-…）")
    many = commands.add_parser("recent", help="列最近几条主人消息各自的去向")
    many.add_argument("--limit", type=int, default=RECENT, help=f"最多列几条（默认 {RECENT}）")
    for each in (one, many):
        each.add_argument("--state", required=True, help="这个部署的状态目录")
        each.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)
    rows = [where(args.state, args.message_id)] if args.command == "where" else recent(args.state, args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        print("\n".join(render(row) for row in rows) or "没有记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())

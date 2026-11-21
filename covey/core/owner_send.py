"""代发 records: a message a topic drafted for the owner to send as themselves, sent only once the owner confirmed it on
its card (app/owner_send.py; docs/proxy.md 代发). Only the Bridge writes them; a task runner reads them for the next
turn's note (``turn_lines``). Standard library and core only, no Feishu.

A topic puts ``send_as_owner`` into its checkpoint (``drafts``): [{"id", "to": {"email" | "name" | "open_id" | "chat_id"},
"text" or "text_file", "why"}], at most MOST. One record a draft: <state>/owner-send/<id>.json, its id from the job and
the draft's own id (``record_id``), so a turn read twice drafts once. Its status only moves by TRANSITIONS (rule 4):

  drafted → sending | declined | superseded
  sending → sent | uncertain | unsent
  unsent  → sending | declined
  sent, uncertain, declined, superseded: final (uncertain may have gone out and is never sent again, rules 1 and 6)
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from covey.core.state import read_object, write_json

FIELD = "send_as_owner"
MOST = 3  # Drafts taken from one checkpoint.
TEXT_LIMIT = 4000  # Characters of a message: the card's input box takes this many.
FILE_LIMIT = 64 * 1024  # Bytes of a text_file read.
WHY_CHARS = 200
REF_CHARS = 40
NOTE_HOURS = 24  # A turn's note lists the topic's drafts that ended within this many hours (``turn_lines``).
NOTE_MOST = 3

TRANSITIONS = {
    "drafted": ("sending", "declined", "superseded"),
    "sending": ("sent", "uncertain", "unsent"),
    "unsent": ("sending", "declined"),
    "sent": (), "uncertain": (), "declined": (), "superseded": (),
}
ENDED = ("sent", "uncertain", "unsent", "declined")
TARGETS = ("email", "open_id", "chat_id", "name")

# Told to a topic's every turn while 代发 is on (app/owner_send.py turn_note), after the checkpoint rules.
RULE = (
    "以主人身份给别人发飞书消息（通知同事、把结果发给某人）时，不要用 lark-cli、不要申请 send_as_user 授权、也不要只写文件让主人转发："
    '在检查点里写 send_as_owner：[{"id":"稳定标识","to":{"email":"对方邮箱"} 或 {"name":"姓名"} 或 {"chat_id":"oc_…"},'
    '"text":"完整正文（纯文本）","why":"一句话：为什么要以主人身份发"}]，最多 3 条；正文里有密钥、AK 时改写 "text_file":"正文文件的绝对路径"，'
    "不要把密钥写进检查点。收件人优先写邮箱：@ 消息里的 open_id 是机器人应用的，主人身份查不到。"
    "本轮结束后话题里会出一张代发草稿卡，主人确认（可改正文）后才以主人身份发出，结果会在话题里说，并在之后的轮次告诉你；"
    "在回复里说明草稿已备好、等主人在卡上确认，不要说已经发出。正文里不要留【…】、{{…}}、TODO、xx月 这类没填的占位符，卡上会拒绝发送。"
)


# A draft still holding a blank to fill in is not sent as it is (app/owner_send.py ``decide`` refuses, the card says so). A local
# copy of the 代答 pre-send check's placeholder pattern (core/proxy.py ``_PLACEHOLDER``, branch t604b/guard), kept apart so the two
# branches do not conflict; change both together.
PLACEHOLDER = re.compile(
    r"【[^】\n]{1,30}】|\{\{[^{}\n]{0,40}\}\}|\$\{[^{}\n]{1,40}\}|<[^<>\n]{0,10}(?:待|填|此处|这里)[^<>\n]{0,10}>|"
    r"[\[［](?:待|填)[^\]］\n]{0,20}[\]］]|(?<![A-Za-z])(?:TODO|TBD|FIXME|XXX)(?![A-Za-z])|待填|"
    r"[Xx]{2,}(?:月|日|号|点|时|分|元|个|人)")


def placeholders(text) -> list[str]:
    """The unfilled placeholders in ``text`` (【…】, {{…}}, ${…}, TODO, 待填, xx月 and the like), each once, in order."""
    found: list[str] = []
    for match in PLACEHOLDER.finditer(str(text or "")):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def folder(state_dir) -> Path:
    return Path(state_dir) / "owner-send"


def record_id(job_key: str, ref: str) -> str:
    return hashlib.sha256(f"{job_key}\0{ref}".encode()).hexdigest()[:32]


def path(state_dir, draft_id: str) -> Path:
    return folder(state_dir) / f"{draft_id}.json"


def read(state_dir, draft_id: str) -> dict:
    return read_object(path(state_dir, draft_id))


def write(state_dir, record: dict) -> None:
    target = path(state_dir, record["id"])
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(target, record, ensure_ascii=False)
    os.chmod(target, 0o600)  # The text may hold what the owner sends a colleague (an AK): the owner's alone.


def move(state_dir, record: dict, status: str, **fields) -> dict | None:
    """``record`` moved to ``status`` with ``fields``, as it is on disk; None when its status there does not allow the
    move (another click or the worker moved it first). The caller holds the record's lock."""
    current = read(state_dir, record["id"]) or record
    if status not in TRANSITIONS.get(current.get("status"), ()):
        return None
    now = time.time()
    moved = {**current, **fields, "status": status, "updated_at": now,
             "history": [*current.get("history", []), [status, now, str(fields.get("reason") or "")]]}
    if status in ENDED:
        moved["ended_at"] = now
    write(state_dir, moved)
    return moved


def digest(text: str) -> str:
    return hashlib.sha256(str(text or "").strip().encode()).hexdigest()[:12]


def _text(entry) -> tuple[str, str]:
    """(the draft's text, why it has none): ``text``, else the file ``text_file`` names (absolute, at most FILE_LIMIT)."""
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        text = text.strip()
    elif isinstance(entry.get("text_file"), str):
        file = Path(entry["text_file"])
        try:
            if not file.is_absolute() or not file.is_file() or file.stat().st_size > FILE_LIMIT:
                return "", "text_file 不是绝对路径下不超过 64 KB 的文件"
            text = file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return "", "text_file 读不出来"
    else:
        return "", "没有正文"
    if not text:
        return "", "正文是空的"
    if len(text) > TEXT_LIMIT:
        return "", f"正文超过 {TEXT_LIMIT} 字"
    return text, ""


def _target(entry) -> dict:
    """The draft's recipient as given: {kind: value}, its first known kind and the name with it."""
    to = entry.get("to")
    if isinstance(to, str):
        to = {"email": to} if "@" in to else {"chat_id": to} if to.startswith("oc_") else \
            {"open_id": to} if to.startswith("ou_") else {"name": to}
    if not isinstance(to, dict):
        return {}
    return {kind: " ".join(str(to[kind]).split())[:100] for kind in TARGETS
            if isinstance(to.get(kind), str) and to[kind].strip()}


def drafts(checkpoint) -> list[dict]:
    """The checkpoint's ``send_as_owner`` as [{ref, to, text, why, problem}], at most MOST; an entry that is not an
    object is left out, one without a recipient or a text keeps why (``problem``). Never raises."""
    entries = checkpoint.get(FIELD) if isinstance(checkpoint, dict) else None
    found = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict):
            continue
        ref = " ".join(str(entry.get("id") or index).split())[:REF_CHARS] or str(index)
        text, problem = _text(entry)
        to = _target(entry)
        if not to:
            problem = problem or "没有收件人"
        found.append({"ref": ref, "to": to, "text": text, "problem": problem,
                      "why": " ".join(str(entry.get("why") or "").split())[:WHY_CHARS]})
        if len(found) >= MOST:
            break
    return found


def topic_records(state_dir, topic_id: str) -> list[dict]:
    """The records of ``topic_id``'s drafts, newest first."""
    found = []
    for file in folder(state_dir).glob("*.json") if folder(state_dir).is_dir() else ():
        record = read_object(file)
        if record.get("topic_id") == topic_id:
            found.append(record)
    return sorted(found, key=lambda r: r.get("created_at") or 0, reverse=True)


OUTCOME = {
    "sent": "已发出",
    "uncertain": "可能已发出，没能确认（不会再自动发，请主人到会话里看）",
    "unsent": "没发出",
    "declined": "没发",
}


def turn_lines(state_dir, topic_id: str, now: float | None = None) -> list[str]:
    """One line a draft of the topic that ended within NOTE_HOURS (at most NOTE_MOST): its id, recipient, outcome."""
    now = time.time() if now is None else now
    lines = []
    for record in topic_records(state_dir, topic_id):
        if record.get("status") not in ENDED or now - float(record.get("ended_at") or 0) > NOTE_HOURS * 3600:
            continue
        at = time.strftime("%m-%d %H:%M", time.localtime(float(record["ended_at"])))
        reason = f"（{record['reason']}）" if record.get("status") in ("unsent", "declined") and record.get("reason") else ""
        edited = "，主人改过正文" if record.get("edited") and record.get("status") == "sent" else ""
        lines.append(f"- 草稿 {record.get('ref')}（给 {record.get('to_label') or '对方'}）：{OUTCOME[record['status']]}"
                     f"{reason}{edited}，{at}")
        if len(lines) >= NOTE_MOST:
            break
    return lines

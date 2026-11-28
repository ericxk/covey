"""topics_search, the butler's tool that finds which topic, and which turn of it, a clue came from
(docs/butler.md 按线索找话题): a Feishu link or its token, a path, a keyword. Only for the butler
(app/butler_tools.py ButlerTools runs it; ButlerSearch's thread warms its reads).

Every turn of the owner's private chat's topics is searched, the butler's own excepted: the owner's words
(user_text; saved_user_answers in the checkpoint, the answers the Bridge keeps there, count as the owner's too), the
result (final_text), the status line and that turn's checkpoint, every field but the Bridge's bridge_preflight and
updated_at (artifacts, completed, phase …). The job records come from the jobs/ cache, only the fields in KEPT taken
(turns_of: no copy of whole records); a checkpoint is read once and kept once its job has ended (status in ENDED: an
ended job's checkpoint is not written again, and a compressed one has a new path, read once more), and ButlerSearch
reads them all once in the background after the Bridge starts, so a search is a scan in memory. No record lock, no
Feishu call, nothing written.

A clue that is a Feishu link is matched by its token; a token also matches when written shortened (its first PREFIX
characters and "…"); a path that is not found whole matches by its last two parts. Both of those are partial. A
keyword matches case-insensitively. Each turn a clue is in says how: 第一次出现 (in its result or checkpoint and in no
earlier turn: most likely made in it), 结果里也有 (the first turn of its topic with it in the result or checkpoint, the
owner having given it or an earlier turn of another topic having had it), 后续沿用 (a later turn of a topic that
already had it there: a checkpoint carries a link along turn after turn), 只是提到 (only in the owner's words). Turns
rank by what they match (a token or path over a keyword, whole over partial, then by how it is there, added over the
clues), then the oldest first: the question is where a thing came from. The ones shown are listed by topic, in the
order of each topic's best, its turns in their order.
"""
from __future__ import annotations

import dataclasses
import re
import time
from pathlib import Path

from covey.app.butler_env import reply
from covey.app.butler_tools import label, title, when
from covey.core.board_read import job_number
from covey.core import butler, collab
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import jobs_dir
from covey.core.store import directory_store
from covey.core.statuses import ENDED
from covey.core.text import stamp

LIMIT, MAX_LIMIT = 10, 30  # Turns shown.
MAX_CLUES = 5
SNIPPET = 70  # Characters each side of a match.
TITLE_CHARS = 30
PREFIX = 8  # A token written shortened: at least its first 8 characters, then "…" or "...".
DOC_LINK = re.compile(r"/(?:wiki|docx|docs|doc|sheets|sheet|base|bitable|file|slides|mindnotes|minutes|drive/folder)"
                      r"/([A-Za-z0-9]{8,})")
TOKEN = re.compile(r"[A-Za-z0-9]{16,64}")
SKIPPED = frozenset({"bridge_preflight", "updated_at"})  # The Bridge's own checkpoint fields, not the turn's.
OWNERS = frozenset({"saved_user_answers"})  # Checkpoint fields with the owner's words: the answers the Bridge keeps.
CHECKPOINT_NAMES = {"phase": "阶段", "completed": "做完的步骤", "artifacts": "产物", "external_tasks": "外部任务",
                    "next_step": "下一步", "question": "在问", "questions": "问题", "saved_user_answers": "主人的回答"}
FIRST, ALSO, LATER, MENTIONED = "第一次出现", "结果里也有", "后续沿用", "只是提到"
CLASS_WEIGHT = {FIRST: 3, ALSO: 2, LATER: 1, MENTIONED: 1}
KEPT = ("topic_id", "job_id", "status", "user_text", "prompt_text", "final_text", "status_message", "checkpoint_path",
        "updated_at", "created_at", "workspace_name")  # Of a job record: what a search reads.
KIND_NAMES = {"token": "token", "path": "路径", "keyword": "关键词"}
NOTHING = ("没有命中：话题的记录（主人的话、结果、检查点）里没有这些线索。wiki 链接可先查出它对应的 docx token 再搜，"
           "也可以读文档，用标题或正文里的词再搜；还找不到就告诉主人没找到来源。")

_checkpoints = {}  # Checkpoint path -> its fields, kept once the job has ended.


@dataclasses.dataclass(frozen=True)
class Clue:
    given: str  # As the butler wrote it.
    kind: str  # "token", "path" or "keyword".
    needle: str  # What is looked for; a keyword's in lower case.
    tail: str = ""  # A path's last two parts, looked for when the whole path is not there.

    def find(self, text, lowered):
        """(start, end, whole) of the clue in ``text`` (``lowered``: its lower case), None when it is not there."""
        if self.kind == "keyword":
            at = lowered.find(self.needle)
            return (at, at + len(self.needle), True) if at >= 0 else None
        at = text.find(self.needle)
        if at >= 0:
            return at, at + len(self.needle), True
        if self.kind == "token":
            for match in re.finditer(re.escape(self.needle[:PREFIX]) + r"([A-Za-z0-9]*)(?:…|\.\.\.)", text):
                if self.needle.startswith(self.needle[:PREFIX] + match.group(1)):
                    return match.start(), match.end(), False
        elif self.tail:
            at = text.find(self.tail)
            if at >= 0:
                return at, at + len(self.tail), False
        return None

    def weight(self, whole):
        return 1 if self.kind == "keyword" else 3 if whole else 2


def clue(raw) -> Clue:
    """The clue ``raw`` names; ValueError for one too short to look for."""
    given = str(raw or "").strip().strip("<>\"'`“”‘’ ")
    link = DOC_LINK.search(given)
    if link:
        return Clue(given, "token", link.group(1))
    if TOKEN.fullmatch(given) and re.search("[A-Za-z]", given) and re.search("[0-9]", given):
        return Clue(given, "token", given)
    if "/" in given and not re.search(r"\s", given):
        whole = given.rstrip("/")
        parts = [part for part in whole.split("/") if part]
        return Clue(given, "path", whole, "/".join(parts[-2:]) if len(parts) >= 3 else "")
    if len(given) < 2:
        raise ValueError(f"线索“{given}”太短：给链接、token、路径，或至少两个字的关键词")
    return Clue(given, "keyword", given.lower())


def clues_of(args):
    raw = args.get("clues")
    raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    found = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            one = clue(item)
            if one not in found:
                found.append(one)
    if not found:
        raise ValueError("clues 要给 1 到 5 条线索：飞书链接或 token、文件路径、关键词")
    if len(found) > MAX_CLUES:
        raise ValueError(f"线索最多 {MAX_CLUES} 条")
    return found


def checkpoint_of(record) -> dict:
    """The fields of the turn's checkpoint, {} without one; kept once the job has ended."""
    path = str(record.get("checkpoint_path") or "")
    if not path:
        return {}
    if path in _checkpoints:
        return _checkpoints[path]
    data = read_checkpoint(Path(path))
    if record.get("status") in ENDED:
        _checkpoints[path] = data
    return data


def fields(record):
    """(where, text, made) of a turn: made is True for its result and checkpoint, False for the owner's words."""
    found = [("结果", str(record.get("final_text") or ""), True)]
    for key, value in checkpoint_of(record).items():
        if key in SKIPPED:
            continue
        name = "检查点" + CHECKPOINT_NAMES.get(key, key)
        for item in value if isinstance(value, list) else [value]:
            if item not in (None, "", [], {}):
                found.append((name, item if isinstance(item, str) else str(item), key not in OWNERS))
    found.append(("状态", str(record.get("status_message") or ""), True))
    found.append(("主人的话", str(record.get("user_text") or record.get("prompt_text") or ""), False))
    return [(where, text, made) for where, text, made in found if text]


def snippet(text, start, end):
    left, right = max(0, start - SNIPPET), min(len(text), end + SNIPPET)
    body = " ".join(text[left:right].split())
    return ("…" if left else "") + body + ("…" if right < len(text) else "")


@dataclasses.dataclass
class Hit:
    topic_id: str
    record: dict
    found: list  # (clue index, class, where, snippet, whole) per clue the turn has.
    score: int = 0


def turns_of(env, chat):
    """Each turn of the owner's topics but the butler's (and a 咨询通道's, no topic of his), oldest first: its KEPT
    fields, taken from the jobs/ cache as the query looks (``where``, which only reads; returning False, so no whole
    record is copied)."""
    kept = []

    def keep(record):
        topic_id = record.get("topic_id")
        if (isinstance(topic_id, str) and topic_id and not butler.is_butler_topic(topic_id, chat)
                and not collab.is_channel(topic_id)):
            kept.append({key: record.get(key) for key in KEPT})
        return False
    directory_store(jobs_dir(env.state_dir)).query(keep, chat_id=chat)
    return sorted(kept, key=job_number)


def search(env, chat, clues):
    """The turns that have any of ``clues``, ranked, and how many turns each clue is in."""
    seen, counts, hits = [False] * len(clues), [0] * len(clues), []
    had = [set() for _ in clues]  # Per clue: the topics with a turn that had it in its result or checkpoint.
    for record in turns_of(env, chat):
        topic_id = record["topic_id"]
        texts = [(where, text, text.lower(), made) for where, text, made in fields(record)]
        found = []
        for index, one in enumerate(clues):
            best, made_in, owner_has = None, False, False
            for where, text, lowered, made in texts:
                match = one.find(text, lowered)
                if match is None:
                    continue
                made_in, owner_has = made_in or made, owner_has or not made
                if best is None or (match[2] and not best[3]):
                    best = (where, text, match, match[2])
            if best is None:
                continue
            if not made_in:
                kind = MENTIONED
            elif not owner_has and not seen[index]:
                kind = FIRST
            else:
                kind = LATER if topic_id in had[index] else ALSO
            if made_in:
                had[index].add(topic_id)
            seen[index] = True
            counts[index] += 1
            where, text, match, whole = best
            found.append((index, kind, where, snippet(text, match[0], match[1]), whole))
        if found:
            score = sum(clues[index].weight(whole) * CLASS_WEIGHT[kind] for index, kind, _, _, whole in found)
            hits.append(Hit(topic_id, record, found, score))
    hits.sort(key=lambda hit: (-hit.score, job_number(hit.record)))
    return hits, counts


def hit_line(hit, many):
    parts = []
    for index, kind, where, text, whole in hit.found:
        mark = f"[{index + 1}] " if many else ""
        parts.append(f"{mark}{kind}{'' if whole else '（部分匹配）'} · {where}：{text}")
    return f"  #{hit.record.get('job_id')} · {stamp(when(hit.record))} · " + "；".join(parts)


def topics_search(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, "还不知道主人的私聊，没有话题可搜。")
    clues = clues_of(request.args)
    limit = request.args.get("limit") if type(request.args.get("limit")) is int else LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    hits, counts = search(env, chat, clues)
    many = len(clues) > 1
    heads = [f"{f'[{i + 1}] ' if many else ''}线索 {KIND_NAMES[one.kind]} "
             f"{one.needle if one.kind != 'keyword' else one.given}：{counts[i]} 轮" for i, one in enumerate(clues)]
    if not hits:
        return reply(True, "\n".join([*heads, NOTHING]), total=0)
    shown = hits[:limit]
    order, by_topic = [], {}
    for hit in shown:
        if hit.topic_id not in by_topic:
            order.append(hit.topic_id)
        by_topic.setdefault(hit.topic_id, []).append(hit)
    lines = [*heads]
    for topic_id in order:
        data_label = label(env, topic_id)
        workspace = str(by_topic[topic_id][-1].record.get("workspace_name") or "—")
        lines.append(f"{data_label} · {title(env, topic_id)[:TITLE_CHARS]} · {workspace} · {topic_id}")
        lines += [hit_line(hit, many) for hit in sorted(by_topic[topic_id], key=lambda hit: job_number(hit.record))]
    if len(hits) > len(shown):
        lines.append(f"（共 {len(hits)} 轮命中，只列了最可能的 {len(shown)} 轮；加线索或换更具体的词缩小）")
    lines.append("用 topic_read 的 job 读某一轮的全文。")
    best = shown[0]
    return reply(True, "\n".join(lines), total=len(hits), shown=len(shown),
                 best={"topic": label(env, best.topic_id), "job": best.record.get("job_id"), "topic_id": best.topic_id})


class ButlerSearch:
    """Reads the checkpoints of the owner's ended turns once after the Bridge starts, so the first topics_search does
    not wait on the disk, unless the deployment rules the butler out (COVEY_BUTLER=0). ``env``: app/butler_env.py
    ButlerEnv."""

    def __init__(self, env):
        self.env = env

    def threads(self):
        return (("main-search", self.run),) if butler.available() else ()

    def run(self, ctx):
        started = time.monotonic()
        try:
            count = self.warm()
            self.env.runtime.log(f"main search warm checkpoints={count} ms={int((time.monotonic() - started) * 1000)}")
        except Exception as exc:  # noqa: BLE001 - topics_search reads what is not kept itself.
            self.env.runtime.log(f"main search warm failed: {type(exc).__name__}: {exc}"[:300])
        self.env.runtime.shutdown.wait()  # Once: a target that returns is run again (core/loops.py supervised).

    def warm(self):
        """Read every checkpoint of the owner's turns not kept yet; how many checkpoints are kept."""
        chat = self.env.chat()
        for record in turns_of(self.env, chat) if chat else ():
            if self.env.runtime.shutdown.is_set():
                break
            checkpoint_of(record)
        return len(_checkpoints)


TOOLS = {"topics_search": topics_search}

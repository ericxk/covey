"""目标检查表 (docs/projects.md 目标检查表, COVEY_COLLAB=1): a project's done-when as a few items its lead keeps, and the
协作 gaps it has not dealt with yet, as plain functions of the project record and its inbox. Standard library and core only.

- An item (``items``) is {id, criterion, topic, needs, evidence, gap, state, at, job, title, waiting_on, done_at, topics}:
  what is to be true (``criterion``), the member topic that delivers it (``topic``, a topic id; '' before one is named), the
  items it waits on (``needs``, their ids), where the proof is (``evidence``: a commit, a path, the check a topic's checkpoint
  said), what is still missing and who does it next (``gap``), and its state (``STATES``): ``open`` not met yet, ``done`` met,
  with its evidence, ``blocked`` held up (by ``needs`` or what ``gap`` says), ``dropped`` the owner let it go (``gap`` says
  why; an item turned dropped names in its ``evidence`` the owner's word for it, ``msg:<his message id>`` or
  ``ask:<a question he answered>``, which the lead's tool checks: ``bases``, W2-C; one dropped before is left as it is).
  Ids are ``c1``, ``c2`` … in the order written and never given again; MAX_ITEMS of them not met at most, MAX_TOTAL in all. Optional, for the owner's 简报 and 进度表
  (docs/web.md 专项条目): a short ``title`` (TITLE characters); ``waiting_on`` ``owner`` when the next step is the owner's
  ('' when the lead says it is not, writing ``none`` or ''; None when it never said: the gap's words are read then, ``owners``); ``done_at``, written
  by ``apply`` when the item turns done and cleared when it turns back (0 for one done before it was kept); ``topics``, the
  other topics (TOPICS at most) that took part in it beside ``topic`` (``topics_of`` is all of them). A record from before has
  none of them and reads as before.
- The record keeps them in ``checklist``: {items, closed, next, at, job}. ``closed`` is the 协作 gaps the lead dealt with
  (gap id -> {how, at, job}). Only the lead's tool writes it (app/project_tools.py project_checklist), under the record's
  lock (core/projects.py update), and ``apply`` is that change: it checks everything before it edits, so a refusal writes
  nothing.
- A gap is a ``collab_gap`` receipt of the project's inbox (core/collab.py RECEIPT_GAP: what an integrator's continuation
  could not close). It stays open until the lead closes it with how (``gaps``), read or not: its id comes from the
  receipt's (``gap_id``).
- ``unmet``: the items not met (open, blocked) and the gaps not closed, what project_finish refuses on; ``conflicts``: an
  item marked met while one it waits on is not.

Not a project manager: no dates, no people but topics, no history but what the record keeps."""
from __future__ import annotations

import hashlib
import re
import time

from covey.core import collab

FIELD = "checklist"
OPEN, DONE, BLOCKED, DROPPED = "open", "done", "blocked", "dropped"
STATES = (OPEN, DONE, BLOCKED, DROPPED)
MET = frozenset({DONE, DROPPED})  # What project_finish lets through; ``dropped`` only because the owner let it go.
STATE_WORDS = {OPEN: "没满足", DONE: "满足", BLOCKED: "卡住", DROPPED: "不做了"}
MAX_ITEMS = 20  # Items not met (open, blocked) at once (W2-C: the met ones do not count against it).
MAX_TOTAL = 100  # Items in all, met or not: the record does not grow without end.
TEXT = 200  # Characters of a criterion, an evidence, a gap.
HOW = 120  # Characters of how a gap was dealt with.
NEEDS = 6  # Items one item may wait on.
CLOSED_KEPT = 100  # Closed gaps the record remembers.
TITLE = 24  # Characters of a short title.
TOPICS = 6  # Topics besides ``topic`` that took part in one item.
OWNER = "owner"
WAITING_ON = (OWNER, "")  # What ``waiting_on`` may be: the owner does the next step, or not (None: the lead never said).
# What the lead may write for it: ``none`` is kept as "" (the web page and older releases read that), and "" itself as
# before. The tool says ``none`` (core/butler_tool_specs.py): Gemini refuses an empty enum value (T839).
WAITING_WRITTEN = {OWNER: OWNER, "none": "", "": ""}
# 等你 read off a gap's words where the lead wrote no ``waiting_on`` (the web page's progressData.ts OWNER_GAP).
OWNER_GAP = re.compile(r"(只差|等|要|需要)主人")
DONE_COL, DOING_COL, NEXT_COL, OWNER_COL = "done", "doing", "next", "owner"  # ``column``: 做完, 正在做, 将要做, 等你.
ITEM_ID = re.compile(r"c([1-9][0-9]{0,3})")
GAP_ID = re.compile(r"g[0-9a-f]{6}")
GAP_KIND = collab.RECEIPT_GAP
# The owner's word an item turned ``dropped`` names in its ``evidence`` (W2-C): his message in the project (msg:<id>, the
# pack's 主人在专项里说过的话) or a question he answered himself (ask:<token>, the line the Bridge puts after his answer; the
# pause card's 收尾 too, app/project_loop.py pause_receipt). The lead's tool checks each (app/project_tools.py dropped_bases).
BASIS = re.compile(r"(?<![0-9A-Za-z_])((?:msg|ask):[0-9A-Za-z_\-]{4,128})")
NO_BASIS = ("{item} 标 dropped 要带主人的依据：evidence 写 msg:<消息 id>（主人在专项里说的话，状态包“主人在专项里说过的话”里的）"
            "或 ask:<问题 token>（主人本人答过的题，Bridge 写在他的回答后面）；没有依据就用 project_ask 问主人哪些不做了，不要自己定")


def clean(text, limit=TEXT) -> str:
    return " ".join(str(text or "").split())[:limit]


def _field(record) -> dict:
    found = record.get(FIELD) if isinstance(record, dict) else None
    return found if isinstance(found, dict) else {}


def _item(row) -> dict | None:
    """``row`` as an item, None for one that is not (no id of ours, no criterion)."""
    if not isinstance(row, dict) or not ITEM_ID.fullmatch(str(row.get("id") or "")) or not clean(row.get("criterion")):
        return None
    needs = [need for need in row.get("needs") or () if isinstance(need, str) and ITEM_ID.fullmatch(need)]
    at, done_at = row.get("at"), row.get("done_at")
    return {"id": row["id"], "criterion": clean(row.get("criterion")), "topic": clean(row.get("topic"), 64),
            "needs": needs[:NEEDS], "evidence": clean(row.get("evidence")), "gap": clean(row.get("gap")),
            "state": row.get("state") if row.get("state") in STATES else OPEN,
            "at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else 0, "job": clean(row.get("job"), 120),
            "title": clean(row.get("title"), TITLE),
            "waiting_on": row["waiting_on"] if row.get("waiting_on") in WAITING_ON else None,
            "done_at": done_at if isinstance(done_at, (int, float)) and not isinstance(done_at, bool) else 0,
            "topics": list(dict.fromkeys(topic for topic in (clean(each, 64) for each in row.get("topics") or ()
                                                              if isinstance(each, str)) if topic))[:TOPICS]}


ASK_LINE = "[Bridge] 这道题是主人本人答的：要据此把目标检查表的条目标 dropped，evidence 写 ask:{token}。"


def ask_line(question) -> str:
    """The line after the owner's own answer to a lead's question (``question``, its record: origin ``project_ask``, his
    ``answer_by``) that names it as the ground of a drop (``BASIS``); '' for any other. app/questions.py adds it with 协作 on."""
    if not isinstance(question, dict):
        return ""
    by = question.get("answer_by") if isinstance(question.get("answer_by"), dict) else {}
    token = str(question.get("token") or "")
    if question.get("origin") != "project_ask" or by.get("party") != "owner" or not BASIS.fullmatch("ask:" + token):
        return ""
    return ASK_LINE.format(token=token)


PAUSE_REF = "ask:pause-"  # + when the pause was put to the owner: his 收尾 on the pause card (``pause_ref``).


def pause_ref(ask) -> str:
    """The ref (``ask:pause-<n>``, ``BASIS``) of the owner's choice on the pause card ``ask`` (the record's ``pause_ask``,
    app/project_loop.py): what the 收尾 receipt names as the ground of the drops it asks for; '' for none."""
    at = ask.get("at") if isinstance(ask, dict) else None
    return f"{PAUSE_REF}{int(at)}" if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0 else ""


def bases_of(text) -> list:
    """The owner's words ``text`` (an item's ``evidence``) names as the ground of a drop (``BASIS``), each once, in order."""
    return list(dict.fromkeys(BASIS.findall(str(text or ""))))


def stored(item) -> dict:
    """``item`` as the record keeps it: the optional fields only when they say something (a record stays as before for an
    item that has none; an older release reads it the same)."""
    return {key: value for key, value in item.items()
            if not (key in ("title", "topics", "done_at") and not value or key == "waiting_on" and value is None)}


def items(record) -> list:
    """The record's items, each well formed, in the order they were written; [] when the lead has written none."""
    rows = _field(record).get("items")
    return [item for item in map(_item, rows if isinstance(rows, list) else ()) if item is not None]


def topics_of(item) -> list:
    """The topics of ``item``, each once: its ``topic`` first (the main one), then ``topics`` (those that took part)."""
    return list(dict.fromkeys(topic for topic in (item["topic"], *item["topics"]) if topic))


def kept(record) -> bool:
    """Whether the lead keeps a checklist for ``record``'s project (one item at least)."""
    return bool(items(record))


def closed(record) -> dict:
    """Gap id -> {how, at, job} of the 协作 gaps the lead dealt with."""
    found = _field(record).get("closed")
    return {key: value for key, value in found.items() if GAP_ID.fullmatch(str(key)) and isinstance(value, dict)} \
        if isinstance(found, dict) else {}


def gap_id(receipt_id) -> str:
    """The id a ``collab_gap`` receipt goes by: 'g' and six hex digits of its receipt id."""
    return "g" + hashlib.sha1(str(receipt_id).encode()).hexdigest()[:6]


def gaps(record, receipts) -> list:
    """The 协作 gaps not dealt with: of ``receipts`` (every receipt of the inbox, read or not, oldest first) the
    ``collab_gap`` ones the lead has not closed, as {id, receipt, thread, text, at}."""
    done = closed(record)
    found = []
    for item in receipts:
        if not isinstance(item, dict) or item.get("kind") != GAP_KIND or not item.get("id"):
            continue
        gap = gap_id(item["id"])
        if gap not in done and all(row["id"] != gap for row in found):
            found.append({"id": gap, "receipt": str(item["id"]), "thread": str(item.get("thread") or ""),
                          "text": clean(item.get("text"), 500), "at": float(item.get("at") or 0)})
    return found


def unmet(record, receipts) -> tuple:
    """(the items not met, the gaps not closed): what keeps ``record``'s project from being finished."""
    return [item for item in items(record) if item["state"] not in MET], gaps(record, receipts)


def owners(item) -> tuple:
    """(whether the next step of ``item`` is the owner's, whether that is read off its gap's words: the lead wrote no
    ``waiting_on``). An item met is nobody's."""
    if item["state"] in MET:
        return False, False
    if item["waiting_on"] is not None:
        return item["waiting_on"] == OWNER, False
    guessed = bool(OWNER_GAP.search(item["gap"]))
    return guessed, guessed


def column(item, by_id) -> str:
    """Where the 进度表 puts ``item`` (``by_id``: the checklist's items by id), the same rule as the web page's progressData.ts,
    in this order: met (done, dropped) 做完; the owner's next step (``owners``) 等你; waiting on an item not met, or blocked,
    将要做; a topic on it (``topics_of``) 正在做 (whether it still runs or has ended and waits for the lead to check it);
    else 将要做 (可开始)."""
    if item["state"] in MET:
        return DONE_COL
    if owners(item)[0]:
        return OWNER_COL
    if any(need in by_id and by_id[need]["state"] not in MET for need in item["needs"] if need != item["id"]):
        return NEXT_COL
    if item["state"] == BLOCKED:
        return NEXT_COL
    return DOING_COL if topics_of(item) else NEXT_COL


def columns(record) -> dict:
    """How many items of ``record``'s checklist each ``column`` has, and ``total``; every count 0 without a checklist."""
    entries = items(record)
    by_id = {item["id"]: item for item in entries}
    counts = dict.fromkeys((DONE_COL, DOING_COL, NEXT_COL, OWNER_COL), 0)
    for item in entries:
        counts[column(item, by_id)] += 1
    return {**counts, "total": len(entries)}


def rev(entries) -> dict:
    """Item id -> the ids of the items (of ``entries``, ``items``) that wait on it, in the order written."""
    found = {item["id"]: [] for item in entries}
    for item in entries:
        for need in item["needs"]:
            if need in found and need != item["id"]:
                found[need].append(item["id"])
    return found


def unlocks(entries, mine) -> list:
    """For each item not met that waits on any of ``mine`` (ids) directly: (its id, the ids it still waits on besides ``mine``);
    [] when it waits on nothing else (it can begin once ``mine`` is met). Only the next step, not what lies further on."""
    by_id = {item["id"]: item for item in entries}
    after = rev(entries)
    found = []
    for item_id in dict.fromkeys(x for each in mine for x in after.get(each, ())):
        if item_id in mine or by_id[item_id]["state"] in MET:
            continue
        found.append((item_id, [need for need in by_id[item_id]["needs"]
                                if need in by_id and need not in mine and by_id[need]["state"] not in MET and need != item_id]))
    return found


def conflicts(record) -> list:
    """(item, the ids of what it waits on that is not met) for each item marked met while something it needs is not."""
    by_id = {item["id"]: item for item in items(record)}
    found = []
    for item in by_id.values():
        if item["state"] not in MET:
            continue
        waiting = [need for need in item["needs"] if need in by_id and by_id[need]["state"] not in MET]
        if waiting:
            found.append((item, waiting))
    return found


def _number(item_id) -> int:
    found = ITEM_ID.fullmatch(str(item_id or ""))
    return int(found.group(1)) if found else 0


def apply(record, rows=(), close=(), *, receipts=(), job="", now=None, bases=None) -> list:
    """Edit ``record`` in place (core/projects.py update's ``change``): ``rows`` are items to add (no ``id``: a
    ``criterion`` at least) or to change (an ``id`` of one there: the keys given replace those it has; ``topic`` already a
    topic id or ''), ``close`` the gaps dealt with ({gap, how}; ``receipts`` the inbox, to know the open ones). Everything is
    checked first: ValueError says what does not fit and nothing is changed. The ids of the items added or changed and of the
    gaps closed, in order. ``bases``: the owner's words the Bridge found to be his (``BASIS`` refs; the lead's tool always
    gives them, app/project_tools.py): an item that turns ``dropped`` now must name one of them in its ``evidence``
    (``NO_BASIS``); None checks none (the record's own tests)."""
    stamp = time.time() if now is None else now
    current = items(record)
    before = {item["id"]: item["state"] for item in current}
    by_id = {item["id"]: dict(item) for item in current}
    order = [item["id"] for item in current]
    field = _field(record)
    number = max(0, _number(field.get("next")) - 1, *(_number(item_id) for item_id in order))
    touched, problems = [], []
    for row in rows:
        if not isinstance(row, dict):
            problems.append("每一条要是一个对象")
            continue
        item_id = clean(row.get("id"), 8)
        if item_id:
            if item_id not in by_id:
                problems.append(f"没有条目 {item_id}（状态包“目标检查表”里每行开头的是 id）")
                continue
            item = by_id[item_id]
        else:
            if not clean(row.get("criterion")):
                problems.append("新加的条目要写 criterion（完成标准）")
                continue
            number += 1
            item_id = f"c{number}"
            item = by_id[item_id] = {"id": item_id, "criterion": "", "topic": "", "needs": [], "evidence": "", "gap": "",
                                     "state": OPEN, "at": stamp, "job": clean(job, 120), "title": "", "waiting_on": None,
                                     "done_at": 0, "topics": []}
            order.append(item_id)
        for key in ("criterion", "evidence", "gap"):
            if key in row:
                item[key] = clean(row.get(key))
        if "title" in row:
            item["title"] = clean(row.get("title"), TITLE)
        if "waiting_on" in row:
            written = row.get("waiting_on") if row.get("waiting_on") is not None else ""
            waiting = WAITING_WRITTEN.get(written) if isinstance(written, str) else None
            if waiting is None:
                problems.append(f"{item_id} 的 waiting_on 要是 owner（下一步在主人手上）或 none（不在主人手上）")
                continue
            item["waiting_on"] = waiting
        if "topic" in row:
            item["topic"] = clean(row.get("topic"), 64)
        if "topics" in row:
            given = row.get("topics") if isinstance(row.get("topics"), list) else [row.get("topics")] if row.get("topics") else []
            item["topics"] = list(dict.fromkeys(clean(each, 64) for each in given if clean(each, 64)))
        if "needs" in row:
            given = row.get("needs") if isinstance(row.get("needs"), list) else [row.get("needs")] if row.get("needs") else []
            item["needs"] = list(dict.fromkeys(clean(need, 8) for need in given if clean(need, 8)))
        if "state" in row:
            if row.get("state") not in STATES:
                problems.append(f"{item_id} 的 state 要是 " + "、".join(STATES) + " 之一")
                continue
            item["state"] = row["state"]
        item["at"], item["job"] = stamp, clean(job, 120) or item.get("job", "")
        touched.append(item_id)
    for item_id in dict.fromkeys(touched):
        item = by_id[item_id]
        if not item["criterion"]:
            problems.append(f"{item_id} 的 criterion 不能为空")
        if len(item["needs"]) > NEEDS:
            problems.append(f"{item_id} 最多依赖 {NEEDS} 条")
        if len(item["topics"]) > TOPICS:
            problems.append(f"{item_id} 的 topics 最多 {TOPICS} 个（主要的那个写在 topic）")
        for need in item["needs"]:
            if need == item_id:
                problems.append(f"{item_id} 不能依赖自己")
            elif need not in by_id:
                problems.append(f"{item_id} 依赖的 {need} 不在检查表里")
        if item["state"] == DONE and not item["evidence"]:
            problems.append(f"{item_id} 标 done 要写 evidence（证据在哪：提交号、产物路径、检查点里的自检）")
        if item["state"] == BLOCKED and not (item["needs"] or item["gap"]):
            problems.append(f"{item_id} 标 blocked 要写卡在哪（needs 或 gap）")
        if item["state"] == DROPPED and not item["gap"]:
            problems.append(f"{item_id} 标 dropped 要在 gap 里写为什么不做了（主人怎么说的）")
        if (item["state"] == DROPPED and bases is not None and before.get(item_id) != DROPPED
                and not any(ref in bases for ref in bases_of(item["evidence"]))):
            problems.append(NO_BASIS.format(item=item_id))
        if item["waiting_on"] == OWNER and item["state"] not in MET and not item["gap"]:
            problems.append(f"{item_id} 写了 waiting_on=owner，要在 gap 里写要主人做什么")
        # 完成时间: when it turns done (kept while it stays done), none once it turns back.
        if item["state"] == DONE:
            if before.get(item_id) != DONE:
                item["done_at"] = stamp
        else:
            item["done_at"] = 0
    unmet_count = sum(1 for item_id in order if by_id[item_id]["state"] not in MET)
    if unmet_count > MAX_ITEMS:
        problems.append(f"目标检查表没满足的条目最多 {MAX_ITEMS} 条，这次会有 {unmet_count} 条：合并几条再加，或先核对证据把做完的标 done")
    if len(order) > MAX_TOTAL:
        problems.append(f"目标检查表连满足的一共最多 {MAX_TOTAL} 条，这次会有 {len(order)} 条：合并几条再加")
    open_gaps = {gap["id"] for gap in gaps(record, receipts)}
    shut = []
    for row in close:
        row = row if isinstance(row, dict) else {"gap": row}
        gap, how = clean(row.get("gap"), 8), clean(row.get("how"), HOW)
        if gap not in open_gaps:
            problems.append(f"没有没处理的缺口 {gap or '（空）'}（状态包“没处理的协作缺口”里每行开头的是 id）")
        elif not how:
            problems.append(f"关掉缺口 {gap} 要写 how（怎么处理的：谁补了、证据在哪，或主人说不用补）")
        else:
            shut.append((gap, how))
    if problems:
        raise ValueError("；".join(dict.fromkeys(problems)))
    done = closed(record)
    for gap, how in shut:
        done[gap] = {"how": how, "at": stamp, "job": clean(job, 120)}
    record[FIELD] = {"items": [stored(by_id[item_id]) for item_id in order],
                     "closed": dict(sorted(done.items(), key=lambda pair: pair[1].get("at") or 0)[-CLOSED_KEPT:]),
                     "next": f"c{number + 1}", "at": stamp, "job": clean(job, 120)}
    return [*dict.fromkeys(touched), *(gap for gap, _ in shut)]

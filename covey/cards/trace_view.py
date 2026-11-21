"""Feishu rendering from an isolated execution snapshot; no backend or storage I/O."""
from __future__ import annotations

import hashlib
import html
import re
import time
from typing import NamedTuple

from covey.cards.card_cost import card_cost, text_cost
from covey.cards.elements import (element_count, fold, grey, json_size, markdown_blocks,
                                           markdown_panel, md)
from covey.core.metrics import clock_text, took_text
from covey.core.gateways import is_busy_note
from covey.core.protocol import COMPACT_STEP
from covey.core.text import COMMAND_TITLES, safe_text, shown_command
from covey.core.trace import CHECKPOINT_STEP, has_subtasks, turn_usage
from covey.core.ttft import SOURCES
from covey.core.usage import number
from covey.cards.trace import cut_escaped, literal_code

NOTE_TITLES = {"进展说明", "子任务说明"}
# A step's mark in 执行过程: done green, running blue, failed red; the others as they are.
STEP_MARKS = {"running": "●", "done": "✓", "failed": "✕", "unconfirmed": "?", "stopped": "⏹", "note": "•"}
STEP_COLORS = {"done": "green", "running": "blue", "failed": "red"}
BRIDGE_NOTE = "机器人"  # app/detached.py: "机器人 HH:MM 重启过，任务未中断。", a line of its own, not an operation.
# A sub-task's last note longer than this shows its start; the rest folds (a final report
# of several thousand characters, written for the main task, took about 2,000 px).
FOLD_CHARS = 600


def ttft_line(snapshot):
    """任务详情's TTFT (time to the model's first output) of the main task, when the backend gave it: the one figure
    of ``figures_line`` the 输入/输出 box lacks (模型生成 and token/s repeated the box); ""
    else. 主任务 leads when the turn had subtasks."""
    seconds, source = snapshot.get("ttft_seconds"), snapshot.get("ttft_source")
    if "started_at" not in snapshot or not number(seconds) or source not in SOURCES:
        return ""
    return ("主任务 " if has_subtasks(turn_usage(snapshot)) else "") + f"TTFT {seconds:.2f} 秒"


def clock(stamp, *, seconds=False, day_of=None):
    """HH:MM for today, MM-DD HH:MM for an earlier day; ``seconds``: with them. ``day_of``: the stamp whose day
    decides, for an end shown as its step's start is."""
    today = time.localtime(stamp if day_of is None else day_of)[:3] == time.localtime()[:3]
    return time.strftime(("%H:%M" if today else "%m-%d %H:%M") + (":%S" if seconds else ""), time.localtime(stamp))


def compaction_line(step):
    """A context compaction's line in 执行过程: seen at a glance, not folded among operations."""
    states = {"running": "执行中", "done": "已完成", "failed": "未成功", "unconfirmed": "结束状态未确认", "stopped": "已停止"}
    took = f" · 用时 {clock_text(step['elapsed'])}" if "elapsed" in step else ""
    return (f"📦 {clock(step['started'])} · {COMPACT_STEP} · {states.get(step['status'], '状态未知')}{took}"
            + (f"\n{step['detail']}" if step.get("detail") else ""))


def owner_prefix(owner, named):
    """Who a sub-task's entry is for: its name on the first of consecutive entries, 子任务 after."""
    return "" if not owner else f"子任务 · {owner} · " if named else "子任务 · "


def step_title(step):
    return "调用工具" if step["title"] == "tool" else step["title"]


def step_detail(step):
    """A step's detail on one line without its own title in front (执行命令：执行命令…), a command as
    shown_command shows it, a long path by its last two parts. ToolTrace already escaped this
    producer-approved, bounded detail."""
    title = step_title(step)
    detail = " ".join(str(step.get("detail") or "").split())
    for prefix in dict.fromkeys((safe_text(step["title"], None), safe_text(title, None))):
        if detail == prefix:
            return ""
        if detail.startswith((prefix + "：", prefix + ":", prefix + " ")):
            detail = detail[len(prefix) + 1:].lstrip()
            break
        if detail.startswith((prefix + "（", prefix + "(")):
            detail = detail[len(prefix):]
            break
    if (title in COMMAND_TITLES or title == CHECKPOINT_STEP) and detail:
        # A checkpoint write is a command too: Codex's showed its /bin/bash -lc "…" wrapper.
        detail = shown_command(detail)
    if title in {"读取文件", "写入文件", "修改文件", "浏览目录"} and detail.startswith("/") and len(detail) > 96:
        detail = "…/" + "/".join(detail.rstrip("/").split("/")[-2:])  # Recognisable without the mount prefix.
    if len(detail) > 160:
        # The end may say how it runs (…（后台运行）): kept, the middle cut, never from inside an entity.
        tail = detail[-34:]
        semi = tail.find(";")
        if semi != -1 and "&" not in tail[:semi]:
            tail = tail[semi + 1:]
        detail = cut_escaped(detail, 120) + tail
    return detail


def checkpoint_purpose(step):
    """A checkpoint write's purpose as Claude wrote it before its command ("<purpose> · <command>"), else ""."""
    purpose, sep, _ = " ".join(str(step.get("detail") or "").split()).partition(" · ")
    return purpose if sep else ""


def run_counts(rows, lead=""):
    """What a run of operations did, on one line: how many of each kind, whose when sub-tasks ran some, and
    how many did not succeed; ``lead`` "前 " for the earlier part of a run whose last ones show."""
    tally = {}
    for row in rows:
        title = step_title(row["step"])
        tally[title] = tally.get(title, 0) + 1
    kinds = "、".join(f"{title} {count}" for title, count in sorted(tally.items(), key=lambda kv: -kv[1])[:4])
    owners = {row["owner"] for row in rows}
    if owners == {""}:
        who = ""
    elif len(owners) == 1:
        who = owner_prefix(rows[0]["owner"], True)
    else:
        who = ""
        kinds += f"（子任务 {sum(1 for row in rows if row['owner'])} 项）"
    statuses = [row["step"]["status"] for row in rows]
    labels = [f"{statuses.count(status)} 项{label}" for status, label in
              (("failed", "失败"), ("unconfirmed", "状态未确认"), ("stopped", "已停止")) if status in statuses]
    return f"{who}{lead}{len(rows)} 项操作：{kinds}" + ("，" + "，".join(labels) if labels else "")


def row_line(row):
    """An operation as 执行过程 lists it: the mark, whose when a sub-task's, the kind in bold with how long it
    took and when, and its detail on a grey line under it."""
    return row["mark"] + " " + (grey(f"子任务 · {row['owner']} · ") if row["owner"] else "") + row["text"]


def row_blocks(rows):
    """Operations as markdown blocks of at most 3 KB each: one card node for a few of them."""
    return markdown_blocks(map(row_line, rows))


def count_line(rows, lead=""):
    return md(grey("· " + run_counts(rows, lead)), size="notation")


def small_fold(element_id, title, elements):
    """A fold of 执行过程 the client opens itself, its title in the small grey print of the rows, the chevron after it."""
    return fold(element_id, md(grey(title), size="notation"), elements, title_padding="2px 0px 2px 0px",
                icon_position="follow_text")


def run_fold(rows, lead=""):
    """The earlier operations of a long run, folded under their count: its ID, from the run's first operation,
    keeps it open while the run grows."""
    return small_fold("ops_" + hashlib.sha256(rows[0]["key"].encode()).hexdigest()[:16], run_counts(rows, lead),
                      row_blocks(rows))


# A run of operations (those between two other entries) of at most SHOW_OPERATIONS shows each with its detail; a
# longer one shows its last KEEP_OPERATIONS and folds the others under their count (run_fold). An operation still
# running always shows. 5 folded runs of 6 to "前 3 项操作：执行命令 3", which read as something to do: a fold now saves at least six rows and says it holds the earlier ones.
SHOW_OPERATIONS = 8
KEEP_OPERATIONS = 3
FOLD_LEAD = "收起了更早的 "  # run_fold's title: 收起了更早的 21 项操作：执行命令 18、读取文件 3
COUNT_LEAD = "更早的 "  # The same once the fold is left out for room: · 更早的 21 项操作：…


def run_elements(rows, state):
    """A run's elements: ``state`` "full" (as above), "folded" (the fold left as its count line) or "collapsed"
    (the whole run as its count line, but for what still runs)."""
    live = [row for row in rows if row["step"]["status"] == "running"]
    if state == "collapsed":
        done = [row for row in rows if row not in live]
        return ([count_line(done)] if done else []) + row_blocks(live)
    if len(rows) <= SHOW_OPERATIONS:
        return row_blocks(rows)
    head = [row for row in rows[:-KEEP_OPERATIONS] if row not in live]
    tail = [row for row in rows if row not in head]
    return ([run_fold(head, FOLD_LEAD) if state == "full" else count_line(head, COUNT_LEAD)] if head else []) + row_blocks(tail)


def first_sentence(text, limit=70):
    """A 进展说明's first sentence on one line, at most ``limit`` characters (cut_escaped keeps entities)."""
    plain = " ".join(str(text).split())
    end = SENTENCE_END.search(plain)
    return cut_escaped(plain[:end.end()].strip() if end else plain, limit)


def older_fold(briefs, dropped=0):
    """The entries 执行过程 has no room for, oldest first, one line each in a fold (small_fold); its ID keeps it
    open across redraws. ``dropped``: the oldest lines left out as well."""
    lines = ([grey(f"最早的 {dropped} 条已略去")] if dropped else []) + list(briefs)
    return small_fold("trace_older", f"更早的 {len(briefs) + dropped} 条记录（只留首句）", markdown_blocks(lines))


class Room(NamedTuple):
    """What the records of the process panel may take: bytes of JSON, CardKit's tagged nodes, and CardKit's size
    (cards/card_cost.py card_cost: every line and mark counts, so a panel of many very short steps is small in bytes
    and large there)."""
    bytes: int
    nodes: int
    cost: int


def measure(elements) -> tuple:
    """(bytes, nodes, cost) of ``elements`` the way a Room counts them."""
    return (sum(json_size(e) + 2 for e in elements), sum(element_count(e) for e in elements),
            sum(card_cost(e) for e in elements))


def within(used, room) -> bool:
    return used[0] <= room.bytes and used[1] <= room.nodes and used[2] <= room.cost


def fold_older(elements, briefs, room, end=None):
    """``elements`` as they are when they fit ``room``, else with as few of the oldest as it takes moved into
    ``older_fold`` (one line each; an element without a brief line, a note's later chunk, goes without one),
    the fold's own oldest lines left out if even it is too long. Nothing from ``end`` on folds (the last
    进展说明 and what came after it). A long task keeps every 进展说明 it has room for, without pages (about one
    task in ten has more than a card holds, up to 72 KB)."""
    costs = [measure([e]) for e in elements]
    total = tuple(map(sum, zip(*costs))) if costs else (0, 0, 0)
    if within(total, room):
        return elements

    def fits(panel, used):
        return within(tuple(a + b for a, b in zip(used, measure([panel]))), room)

    end = len(elements) if end is None else end
    used, lines, least = total, [], []  # least[i]: what lines[:i] add to the fold at the least (bytes, size).
    for count in range(1, end + 1):  # Fold the oldest `count`; the fold grows by a line at a time.
        used = tuple(a - b for a, b in zip(used, costs[count - 1]))
        if briefs.get(id(elements[count - 1])):
            line = briefs[id(elements[count - 1])]
            last = least[-1] if least else (0, 0)
            lines.append(line)
            least.append((last[0] + json_size(line) + 1, last[1] + text_cost(line) + 1))
        if not lines:
            if within(used, room):
                return elements[count:]
            continue
        # The fold's size grows with its lines: measure it only once the rest leaves room for its lines at all (a
        # measure of the whole fold at each step took seconds for a task of thousands of short steps).
        if (used[0] + least[-1][0] <= room.bytes and used[2] + least[-1][1] <= room.cost
                and fits(older_fold(lines), used)):
            return [older_fold(lines)] + elements[count:]
    for dropped in range(1, len(lines) + 1):
        kept = (least[-1][0] - least[dropped - 1][0], least[-1][1] - least[dropped - 1][1])
        if (used[0] + kept[0] <= room.bytes and used[2] + kept[1] <= room.cost
                and fits(older_fold(lines[dropped:], dropped), used)):
            return [older_fold(lines[dropped:], dropped)] + elements[end:]
    return elements[end:]


# CardKit update docs advise <=30 KB per card; 200 tagged nodes is a hard
# limit. Reserve space for the request, task controls, status and delivery text.
OLD_SNAPSHOT_BYTES = 23200  # The budget of a snapshot saved without one; a new trace writes its own (core/trace.py TRACE_BYTES).
TRACE_ELEMENTS = 115  # The whole panel's nodes, its wrapper's (WRAPPER_NODES) among them.
# The wrapper cards/trace_updates.py panel_json puts around the records: the two column sets and their columns, the
# collapsible with its title and chevron, the figures line, and a page notice of an older card's.
WRAPPER_NODES = 9
# The whole panel's CardKit size (cards/card_cost.py), of the card's CARD_COST_BUDGET (300,000). A panel full to its bytes
# with ordinary steps measured 100,000-112,000, a 600-line plan beside 100 commands 130,000; one of many very short steps
# and notes 211,000 (a card of it left the answer less than a third of its room). Its wrapper's part (WRAPPER_COST) comes
# off first.
TRACE_COST = 150_000
WRAPPER_COST = 3000
CHILD_STEPS_BELOW = "子任务的过程记录见下方"
CHILD_NO_EVENTS = "尚未收到子任务的公开过程事件"


SENTENCE_END = re.compile(r"[。！？；]|[.!?;:][ \t]+")  # A Latin one with the spaces after it.


def _break(text, start, end):
    """Where a chunk of ``text[start:end]`` that must end before ``end`` ends: after the last line
    break in its second half, else after a sentence end, else after a space (not
    "unit te" / "sts"); else at ``end``, but never inside an entity or after an escaping backslash."""
    floor = start + (end - start) // 2
    newline = text.rfind("\n", floor, end)
    if newline != -1:
        return newline + 1
    ends = [match.end() for match in SENTENCE_END.finditer(text, floor, end)]
    if ends and ends[-1] > floor:
        return ends[-1]
    space = max(text.rfind(" ", floor, end), text.rfind("　", floor, end))
    if space != -1:
        return space + 1
    amp = text.rfind("&", max(start, end - 10), end)
    if amp > start and ";" not in text[amp:end]:
        end = amp
    while end - 1 > start and (end - start - len(text[start:end].rstrip("\\"))) % 2:
        end -= 1
    return end


def text_chunks(text, limit=5000):
    """Split on Unicode boundaries, counting serialized UTF-8 text bytes: each chunk is at most
    ``limit`` and at least one character, ending at a line break, a sentence end or a space where
    one is in its second half (``_break``). A slice is measured whole (a panel re-splits every note
    on each render; per character it took seconds)."""
    start = 0
    while start < len(text):
        end = min(len(text), start + max(limit, 1))  # Every character takes at least one byte.
        if json_size(text[start:end]) - 2 > limit:
            low, high = start + 1, end - 1  # The longest fitting slice ends in [low, high], else at low.
            while low < high:
                middle = (low + high + 1) // 2
                if json_size(text[start:middle]) - 2 <= limit:
                    low = middle
                else:
                    high = middle - 1
            end = _break(text, start, low)
        elif end < len(text):
            end = _break(text, start, end)
        yield text[start:end]
        start = end


# How a 进展说明 stands out among the operations : its text under a blue heading on a light blue block, in the operations' small size:
# at the card's 14 px it read larger than the 12 px rows around it (12 号常规: the block sets it apart). A block costs three of the card's nodes, so a very long task folds its oldest
# notes into 更早的 N 条记录 sooner.
NOTE_BACKGROUND = "blue-50"


def note_element(content):
    element = md(content, size="notation")
    return {"tag": "column_set", "flex_mode": "none", "background_style": NOTE_BACKGROUND, "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "padding": "6px 10px 6px 10px", "elements": [element]}]}


def note_blocks(note, chunk_bytes, named=True):
    """A 进展说明's elements: text blocks of at most ``chunk_bytes`` under its grey heading (unfilled: blue
    blocks in the grey panel looked off), and for a sub-task's last note
    over FOLD_CHARS, the rest in folded panels after its start."""
    heading = owner_prefix(note["owner"], named) + note["heading"]
    detail, rest = note["detail"], ""
    if note["fold"] and len(detail) > FOLD_CHARS:
        cut = _break(detail, 0, FOLD_CHARS)
        detail, rest = detail[:cut], detail[cut:]
    # One element a chunk, its grey heading on the first line: a note costs one of the card's nodes, so a long
    # task's every 进展说明 fits without pages.
    blocks = [note_element(f"<font color='blue'>{heading}{'（续）' if i else ''}</font>\n" + chunk)
              for i, chunk in enumerate(text_chunks(detail, chunk_bytes))]
    if rest.strip():
        # Named only where a page starts with it; after its start the sub-task is plain.
        title = ((f"子任务 · {note['owner']} · " if named else "")
                 + f"{note['title']}其余部分 · 约 {len(html.unescape(rest)):,} 字")
        for i, chunk in enumerate(text_chunks(rest, chunk_bytes)):
            key = "rest_" + hashlib.sha256(f"{note['key']}:{i}".encode()).hexdigest()[:15]
            # Small as the note it goes on.
            blocks.append(markdown_panel(key, title + ("（续）" if i else ""), chunk, size="notation", quiet=True,
                                         border=False, icon_position="follow_text", padding="4px 0px 4px 0px",
                                         spacing="4px"))
    return blocks


def trace_items(lines, child_elements, notes, tool_rows, chunk_bytes):
    """The panel's entries, oldest first (the task's and its sub-tasks' together): 进展说明 whole, sub-tasks and the
    Bridge's lines as they are, and between them each run of operations with their details (run_elements). Long
    runs of operations fold, and past the limit the oldest operations' details go first. An item is a dict: kind "note" (full, short: its blocks with and
    without the sub-task's name; brief: its line in older_fold), "run" (rows, state) or "other" (element, brief)."""
    items, run = [], []

    def end_run():
        if run:
            items.append({"kind": "run", "rows": list(run), "state": "full"})
            run.clear()

    for line in lines:
        if line in tool_rows:
            row = tool_rows[line]
            if row["step"]["status"] != "note":
                run.append(row)
                continue
            # A note of another kind (a plan): the narrative, kept in full where it came.
            end_run()
            element = md(row["mark"] + " " + row["text"], size="notation")
            items.append({"kind": "other", "element": element,
                          "brief": f"{row['mark']} {step_title(row['step'])}：{first_sentence(row['step']['detail'])}"})
            continue
        end_run()
        if line in notes and "element" not in notes[line]:
            note = notes[line]
            items.append({"kind": "note", "owner_id": note["owner_id"], "full": note_blocks(note, chunk_bytes),
                          "short": note_blocks(note, chunk_bytes, named=False),
                          "brief": grey(owner_prefix(note["owner"], True) + note["heading"].split(" · ")[0]) + " "
                                   + first_sentence(note["detail"])})
        elif line in child_elements:
            element = child_elements[line]
            items.append({"kind": "other", "element": element,
                          "brief": grey("子任务 · " + element["header"]["title"]["content"])})
        elif line in notes:
            element = notes[line]["element"]
            items.append({"kind": "other", "element": element, "brief": element["content"].split("\n")[0]})
    end_run()
    return items


def assemble(items):
    """(elements, short, briefs, protected) of the items: a run that is only its count line goes onto the first
    line of the 进展说明 after it (one card node fewer). ``short`` maps the id of a note's block that follows an
    entry of the same sub-task to its form without the sub-task's name; ``briefs`` maps an element's id to its
    line in older_fold; ``protected``: the index of the last 进展说明's last text block: it and what follows
    never fold."""
    elements, short, briefs, protected = [], {}, {}, None
    previous, carry = "", None
    for item in items:
        if item["kind"] == "run":
            parts = run_elements(item["rows"], item["state"])
            if len(parts) == 1 and parts[0]["content"].startswith("<font") and item["state"] == "collapsed":
                carry = parts[0]  # Joined to the note after it, if one follows.
                continue
            if carry:
                elements.append(carry)
                carry = None
            if parts:
                briefs[id(parts[0])] = grey("· " + run_counts(item["rows"]))
            elements.extend(parts)
            previous = ""
            continue
        if item["kind"] == "note":
            full, brief = list(item["full"]), list(item["short"])
            if carry and full[0].get("tag") == "markdown":
                for blocks in (full, brief):
                    blocks[0] = {**blocks[0], "content": carry["content"] + "\n" + blocks[0]["content"]}
                carry = None
            elif carry:  # A note on its own block: the count line goes above it.
                elements.append(carry)
                carry = None
            for i, (element, other) in enumerate(zip(full, brief)):
                if item["owner_id"] and (i or item["owner_id"] == previous) and element != other:
                    short[id(element)] = other
            briefs[id(full[0])] = item["brief"]
            # Its last text block: a note longer than the card keeps its end, its start may fold.
            protected = len(elements) + max(i for i, e in enumerate(full) if e.get("tag") in ("markdown", "column_set"))
            elements.extend(full)
            previous = item["owner_id"]
            continue
        if carry:
            elements.append(carry)
            carry = None
        briefs[id(item["element"])] = item["brief"]
        elements.append(item["element"])
        previous = ""
    if carry:
        elements.append(carry)
    return elements, short, briefs, protected


def item_cost(item, state=None):
    """(bytes, nodes, cost) of an item's elements as assemble writes them, a run in ``state`` (``measure``)."""
    if item["kind"] == "run":
        parts = run_elements(item["rows"], state or item["state"])
    else:
        parts = item["full"] if item["kind"] == "note" else [item["element"]]
    return measure(parts)


def fit_items(items, room):
    """Make room the way the owner asked: the oldest runs' details go first, each run then its
    count line, the last run last; a run's operations still running stay. The 进展说明 are folded afterwards, by
    fold_older, only when even that is not enough."""
    costs = [item_cost(item) for item in items]
    used = tuple(map(sum, zip(*costs))) if costs else (0, 0, 0)
    runs = [i for i, item in enumerate(items) if item["kind"] == "run"]
    order = [(i, "collapsed") for i in runs[:-1]] + [(i, "folded") for i in runs[-1:]]
    for i, state in order:
        if within(used, room):
            break
        cost = item_cost(items[i], state)
        used = tuple(total - old + new for total, old, new in zip(used, costs[i], cost))
        costs[i] = cost
        items[i]["state"] = state
    return items


def step_row(s, step_id, owner, owner_id):
    """An operation's row in tool_rows. The A 仪表盘 sample's step line: the mark, the kind in bold, then
    grey how long it took (or its state) and when it started, the exit code; the detail on a grey line under it."""
    facts = [f"{s['elapsed']:g} 秒"] if "elapsed" in s else []
    facts += [{"unconfirmed": "结束状态未确认", "stopped": "已停止", "running": "执行中"}.get(s["status"], "")]
    # One time: an ended step's end, else its start (a phone's side panel wrapped "13:13:39 → 13:13:39"). The date
    # only when the step did not start today; the group's title names the sub-task.
    facts.append(clock(s["ended"], seconds=True, day_of=s["started"]) if "ended" in s
                 else clock(s["started"], seconds=True) + (" 开始" if s["status"] == "running" else ""))
    if s.get("exit_code") not in (None, 0, "0"):  # ✓ says 0 already (Codex wrote 退出码 0 on every row).
        facts.append(f"退出码 {s['exit_code']}")
    # A checkpoint write: its title says it; of its command (a heredoc) only Claude's purpose shows, which
    # says what else it did.
    detail = (s["detail"] if s["status"] == "note" else checkpoint_purpose(s) if s["title"] == CHECKPOINT_STEP
              else step_detail(s))
    mark = STEP_MARKS[s["status"]]
    mark = f"<font color='{STEP_COLORS[s['status']]}'>{mark}</font>" if s["status"] in STEP_COLORS else mark
    # A note's detail is kept whole and may be cut into chunks (text_chunks), so it gets no tag around it.
    shown = detail if s["status"] == "note" else grey(cut_escaped(detail, 160)) if detail else ""
    text = f"**{step_title(s)}** " + grey("· " + " · ".join(f for f in facts if f)) + (f"\n{shown}" if shown else "")
    return {"step": s, "owner": owner, "owner_id": owner_id, "key": owner_id + ":" + step_id, "mark": mark, "text": text}


def line_entry(view, step_id, s, owner, owner_id, last_note):
    """What a step that is not a sub-task's start is in the panel, (kind, entry): "note", a 进展说明 or a line of the
    Bridge's (``trace_items``), or "tool", an operation's row. ``last_note(view)``: the key of a view's last 进展说明."""
    if view.stopped and s["status"] == "unconfirmed":
        s = {**s, "status": "stopped"}
    if s["status"] == "note" and s["title"] in NOTE_TITLES:
        return "note", {"owner": owner, "owner_id": owner_id, "key": owner_id + ":" + step_id,
                        "title": s["title"], "heading": f"{clock(s['started'])} · {s['title']}",
                        "detail": literal_code(s["detail"] or " "),
                        "fold": bool(owner) and step_id == last_note(view)}
    if s["status"] == "note" and s["title"] == BRIDGE_NOTE:
        return "note", {"element": md(s["detail"] or BRIDGE_NOTE, size="notation")}
    if s["status"] == "note" and is_busy_note(s["title"]):
        # The Bridge's count of a wait for a busy gateway: seen at a glance, not folded among operations,
        # and a sub-task's says whose it is.
        return "note", {"element": md(f"{owner_prefix(owner, True)}{clock(s['started'])} · {s['detail'] or s['title']}",
                                      size="notation")}
    if s["title"] == COMPACT_STEP and s["status"] != "note" and not owner:
        return "note", {"element": md(compaction_line(s), size="notation")}
    return "tool", step_row(s, step_id, owner, owner_id)


class PanelModel(NamedTuple):
    """The process panel 执行过程 (TraceView.panel_model), drawn by cards/trace_updates.py ``panel_json``."""
    state: str  # "12 步", "等待进展" or "无执行记录", with " · 用时 …" on an ended timed one.
    live: bool  # The turn runs: the title says 进行中 and the first line inside has ``state``.
    # Drawn open: only a redraw the owner asked for by opening it (an old card's page controls, app/interaction.py
    # show_trace_page). A running panel starts folded too: the status line says 现在：… (docs/ui.md 5.2).
    expanded: bool
    elements: list  # The records, or one line that there are none.
    rows: int  # The records' count.
    notice: dict | None = None  # Above the records: a typed page that does not exist (app/interaction.py).

    @property
    def ended_empty(self):
        """An ended turn without records: its title says so, with nothing to unfold."""
        return not self.live and not self.rows


def process_title(model):
    """The collapsible's title. The count changes with every step, and a sent collapsible's title changes only
    with the whole collapsible, which would undo the user's open or closed (cards/trace_updates.py): a running
    panel's title says 进行中 and its first line inside has the count, by its own ID."""
    return "**执行过程**　" + grey("进行中" if model.live else model.state)


def panel_metrics(model):
    """The first line inside the panel (``trace_metrics``): a running one's count; "" for none."""
    return model.state if model.live else ""


class TraceView:
    def __init__(self, snapshot, depth=0, stopped=None):
        self.depth = depth
        # A stopped task's open steps are drawn with the display-only status "stopped" (⏹ 已停止);
        # a sub-task's view takes the task's flag.
        self.stopped = snapshot.get("stopped") is True if stopped is None else stopped
        self.steps = snapshot.get("steps", {})
        self.total = snapshot.get("total", len(self.steps))
        self.started_at = snapshot["started_at"]
        self.ended_at = snapshot.get("ended_at")
        self.input_tokens = snapshot.get("input_tokens")
        self.output_tokens = snapshot.get("output_tokens")
        self.measured_output_tokens = snapshot.get("measured_output_tokens")
        self.generation_seconds = snapshot.get("generation_seconds")
        self.ttft_seconds = snapshot.get("ttft_seconds")
        self.ttft_source = snapshot.get("ttft_source")
        self.agents = snapshot.get("agents", {})
        self.children = snapshot.get("children", {})
        self.byte_budget = snapshot.get("trace_byte_budget", OLD_SNAPSHOT_BYTES)
        self.source = snapshot

    def agent_element(self, sid):
        agent = self.agents[sid]
        labels = {"running":"执行中", "pendingInit":"等待启动", "completed":"已完成", "errored":"失败", "interrupted":"已停止", "shutdown":"已关闭"}
        status = labels.get(agent["status"], "状态未知")
        if self.ended_at and agent["status"] in {"running", "pendingInit"}:
            status = "已停止" if self.stopped else "结束状态未确认"
        end = agent.get("ended_at", self.ended_at or time.time())
        elapsed = max(0, end - agent["started_at"])
        content = safe_text(agent.get("description", ""), 400)
        if agent.get("summary"):
            content += "\n" + safe_text(agent["summary"], 700)
        if not content.strip():
            content = (CHILD_STEPS_BELOW if self.children.get(sid, {}).get("steps")
                       else CHILD_NO_EVENTS)
        title = f"{time.strftime('%H:%M:%S', time.localtime(agent['started_at']))} · {agent['name']} · {status} · {elapsed:.1f} 秒"
        return markdown_panel("sub_" + hashlib.sha256(sid.encode()).hexdigest()[:16], title, content[:2400], size="notation")

    def _elements(self, running, cost=TRACE_COST):
        """(elements, lines) of the panel, oldest first: the oldest operations' details hidden first when there is
        no room for all (fit_items), then the oldest entries folded (fold_older). ``cost``: the whole panel's CardKit
        size at most (TRACE_COST, less where the card has less room: app/task_card.py panel_room)."""
        chunk_bytes = max(400, min(4000, self.byte_budget - 7500))
        lines, child_elements, notes, tool_rows = self._trace_lines()
        room = Room(self.byte_budget - 1400, TRACE_ELEMENTS - WRAPPER_NODES, max(0, cost - WRAPPER_COST))
        items = trace_items(lines, child_elements, notes, tool_rows, chunk_bytes)
        hidden = self.total - len(self.steps)
        if hidden:
            line = md(f"已隐藏 {hidden} 条较早工具记录；进展说明保留全文。", size="notation")
            items.insert(0, {"kind": "other", "element": line, "brief": line["content"]})
        # Measured with every sub-task named, so the first entry after the fold has room for its name.
        elements, short, briefs, protected = assemble(fit_items(items, room))
        elements = fold_older(elements, briefs, room, protected)
        first = 1 if elements and elements[0].get("element_id") == "trace_older" else 0
        return [short.get(id(e), e) if i > first else e for i, e in enumerate(elements)], lines

    def panel_model(self, running=True, *, timed=False, opened=False, cost=TRACE_COST):
        """The process panel as data, one page (``fold_older``); cards/trace_updates.py
        ``panel_json`` draws it. ``timed``: an ended turn's state says how long it took, on a card whose status
        line does not (the interrupted task's). ``opened``: drawn open (``PanelModel.expanded``); every other panel
        is drawn folded, running or ended, and the owner opens it. ``cost``: its CardKit size at most (``_elements``)."""
        elements, lines = self._elements(running, cost)
        # The rows and the time; no token line: the 输入/输出 box has the tokens and the speed, 任务详情 the TTFT
        # (cards/streaming.py task_details_text, by ``ttft_line``), kept
        # current while the turn runs. The time is on the status line (✅ 已完成 · 用时 …), not here as well.
        live = running and self.ended_at is None
        state = f"{len(lines)} 步" if lines else "等待进展" if live else "无执行记录"
        if timed and not live:
            state += f" · 用时 {took_text(max(0, (self.ended_at or time.time()) - self.started_at))}"
        empty = ("正在处理，尚无可展示的执行记录。" if live
                 else "执行已结束，本轮没有可展示的执行记录。")
        return PanelModel(state, live, opened, elements or [md(empty, size="notation")], len(lines))

    def _walk(self):
        """Every step of the task's tree, one iterative timeline, a sub-task's right after the step that started it:
        (view, step ID, step, owner, owner's ID). Of a sub-task's steps in its parent, only the first comes."""
        stack = [(self, iter(self.steps.items()), "", "")]
        visited = set()
        while stack:
            view, steps, owner, owner_id = stack[-1]
            entry = next(steps, None)
            if entry is None:
                stack.pop()
                continue
            step_id, s = entry
            sid = s.get("agent_id")
            if sid not in view.agents:
                yield view, step_id, s, owner, owner_id
            elif sid not in visited:
                visited.add(sid)
                yield view, step_id, s, owner, owner_id
                child_data = view.children.get(sid)
                if child_data:
                    child = TraceView(child_data, stopped=self.stopped)
                    stack.append((child, iter(child.steps.items()), view.agents[sid]["name"], sid))

    def _trace_lines(self):
        """The panel's rows by when they began, the task's and its sub-tasks' together, so the newest of
        either is at the bottom:
        (lines, child_elements, notes, tool_rows); a line is a marker for an element, a note or a tool row."""
        lines = []
        child_elements = {}
        entries = {"note": {}, "tool": {}}
        last_notes = {}
        began = {}  # marker -> when it began, for the order

        def last_note(view):
            """The key of a view's last 进展说明: a sub-task's is its report to the main task. Keyed by the
            view itself, not its id(): a sub-task's view is made during the walk and freed after it, and a
            later sibling's view at the same address read the freed one's key (its report never folded)."""
            if view not in last_notes:
                last_notes[view] = next((key for key, step in reversed(view.steps.items())
                                         if step.get("status") == "note" and step.get("title") in NOTE_TITLES), None)
            return last_notes[view]

        for view, step_id, s, owner, owner_id in self._walk():
            if s.get("agent_id") in view.agents:
                sid = s["agent_id"]
                marker = "child:" + sid
                child_elements[marker] = view.agent_element(sid)
                began[marker] = view.agents[sid].get("started_at") or s.get("started") or 0
            else:
                kind, entry = line_entry(view, step_id, s, owner, owner_id, last_note)
                marker = f"{kind}:{len(lines)}"
                entries[kind][marker] = entry
                began[marker] = s["started"]
            lines.append(marker)
        order = {marker: i for i, marker in enumerate(lines)}
        lines.sort(key=lambda marker: (began.get(marker) or 0, order[marker]))
        return lines, child_elements, entries["note"], entries["tool"]

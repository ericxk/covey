"""富文本块 (docs/ui.md 富文本块): the few kinds of block a reply's Markdown may hold beyond CommonMark and GFM, told
apart line by line at its top level, the same way the web page does (web/client/src/cv/richBlocks.ts; both read
tests/fixtures/rich/), so the model writes one text and each place draws it its own way: the cards
(cards/rich_body.py), one card Markdown text (``card_markdown``: the task card, an answer over several cards, a
reply sent as Markdown) and words without the marks (``plain``: a title, the chat list's preview, a draft).

- ``callout``: a quote whose first line is ``> [!NOTE]`` (TIP, IMPORTANT, WARNING, CAUTION; any case), an optional
  title after it; the quote's other lines are its body.
- ``details``: a line ``<details>`` (or ``<details open>``) up to its line ``</details>``, its summary the
  ``<summary>…</summary>`` on the opening line or on the first line in it. Not nested: one inside is its body's words.
- ``metrics``: a fenced block ``metrics``, a line an item: ``标签 | 数值 | 变化`` or ``标签: 数值`` (Collie's forms).
- ``md``: the rest, code blocks, tables and task lists included.

A block opens only at a line's start (not indented: a quote or a fence in a list item is the item's), outside a code
fence; a fence or details left open (a reply still being written, or cut) is Markdown as it stands. Standard library
only."""
from __future__ import annotations

import dataclasses
import re

MD, CALLOUT, DETAILS, METRICS = "md", "callout", "details", "metrics"
TONES = ("note", "tip", "important", "warning", "caution")
# How a callout's kind reads and its mark. The words keep the body's colour: orange says 等你 and 可能卡住 alone and red
# 停住了 alone (docs/ui.md 2.2, tests/test_look.py ColourRulesTest), so the mark tells the kinds apart.
TONE_WORDS = {"note": "说明", "tip": "提示", "important": "重要", "warning": "注意", "caution": "警告"}
TONE_MARKS = {"note": "ℹ️", "tip": "💡", "important": "📌", "warning": "⚠️", "caution": "⛔"}
DETAILS_WORD = "详情"  # A details' title when it has no summary.
TASK_MARKS = {True: "<font color='green'>✓</font>", False: "<font color='grey'>○</font>"}

_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")
_CALLOUT = re.compile(r">[ \t]?\[!(note|tip|important|warning|caution)\][ \t]*(.*)", re.IGNORECASE)
_QUOTE = re.compile(r" {0,3}>[ \t]?(.*)")
_DETAILS_OPEN = re.compile(r"<details(\s+open(?:=\S*)?)?\s*>(.*)", re.IGNORECASE)
_DETAILS_CLOSE = re.compile(r"[ \t]*</details>[ \t]*", re.IGNORECASE)
_SUMMARY = re.compile(r"[ \t]*<summary>(.*?)</summary>(.*)", re.IGNORECASE)
_TABLE_RULE = re.compile(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?")
_PIPE = re.compile(r"(?<!\\)\|")
_PAIR = re.compile(r"([^:：]+?)\s*[:：]\s*(.+)")
_TASK = re.compile(r"^([ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)\[([ xX])\](?=[ \t])")


@dataclasses.dataclass
class Block:
    """One block: its ``kind``, its lines as written (``source``) and what its kind has: a callout's ``tone``, a
    callout's or details' ``title`` and ``body`` (Markdown), a details' ``open``, a metrics block's ``items``
    ((label, value, delta), each as written)."""
    kind: str
    source: str
    tone: str = ""
    title: str = ""
    body: str = ""
    open: bool = False
    items: tuple = ()

    def shape(self) -> dict:
        """What both sides must agree on (tests/fixtures/rich/cases.json): the kind and what it found."""
        found = {"kind": self.kind}
        if self.kind == CALLOUT:
            found.update(tone=self.tone, title=self.title, body=self.body)
        elif self.kind == DETAILS:
            found.update(title=self.title, body=self.body, open=self.open)
        elif self.kind == METRICS:
            found["items"] = [list(item) for item in self.items]
        else:
            found["source"] = self.source
        return found


def _opens_fence(line) -> bool:
    found = _FENCE.fullmatch(line)
    return bool(found) and not (found.group(2)[0] == "`" and "`" in found.group(3))


def _fence_end(lines, i):
    """The index of the line closing the fence opened at ``i``; None when it is not closed."""
    mark = _FENCE.fullmatch(lines[i]).group(2)
    close = re.compile(r" {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*")
    return next((j for j in range(i + 1, len(lines)) if close.fullmatch(lines[j])), None)


def _details_end(lines, i):
    """The index of the ``</details>`` line closing the details opened at ``i`` (those opened in it counted), outside
    fences; None when there is none."""
    depth, j = 1, i + 1
    while j < len(lines):
        if _opens_fence(lines[j]):
            end = _fence_end(lines, j)
            if end is None:
                return None
            j = end + 1
            continue
        if _DETAILS_OPEN.fullmatch(lines[j]):
            depth += 1
        elif _DETAILS_CLOSE.fullmatch(lines[j]):
            depth -= 1
            if not depth:
                return j
        j += 1
    return None


def flatten(text) -> str:
    """``text`` with each details in it as its summary in bold over its body: a details' body holds none."""
    found = blocks(text)
    if not any(block.kind == DETAILS for block in found):
        return text
    return "\n\n".join(f"**{block.title or DETAILS_WORD}**\n\n{block.body}".rstrip() if block.kind == DETAILS
                        else block.source for block in found)


def _cells(line) -> list:
    return [cell.strip().replace("\\|", "|") for cell in _PIPE.split(line.strip().removeprefix("|").removesuffix("|"))]


def metric_items(body) -> tuple:
    """(label, value, delta) of each line of a metrics block that has a label and a value; a table's rule skipped, and
    the header row over it."""
    items, last = [], None  # last: the line the last item came from.
    for n, raw in enumerate(str(body).split("\n")):
        line = raw.strip()
        if not line:
            continue
        if _TABLE_RULE.fullmatch(line):
            if items and last == n - 1:
                items.pop()
            continue
        if "|" in line:
            cells = _cells(line)
            if len(cells) < 2:
                continue
            label, value, delta = cells[0], cells[1], " · ".join(cell for cell in cells[2:] if cell)
        else:
            pair = _PAIR.fullmatch(line)
            if not pair:
                continue
            label, value, delta = pair.group(1).strip(), pair.group(2).strip(), ""
        if label and value:
            items.append((label, value, delta))
            last = n
    return tuple(items)


def blocks(text) -> list:
    """``text``'s blocks in order, the Markdown between them as ``md`` blocks (none blank)."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    out, run = [], []

    def flush():
        if any(line.strip() for line in run):
            out.append(Block(MD, "\n".join(run).strip("\n")))
        run.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if _opens_fence(line):
            end = _fence_end(lines, i)
            if end is None:  # Open to the end: code as it stands.
                run.extend(lines[i:])
                break
            fence = _FENCE.fullmatch(line)
            info = fence.group(3).strip()
            kind = info.split()[0].lower() if info and not fence.group(1) else ""
            items = metric_items("\n".join(lines[i + 1:end])) if kind == METRICS else ()
            if items:
                flush()
                out.append(Block(METRICS, "\n".join(lines[i:end + 1]), items=items))
            else:
                run.extend(lines[i:end + 1])
            i = end + 1
            continue
        callout = _CALLOUT.fullmatch(line)
        if callout:
            j = i + 1
            while j < len(lines) and _QUOTE.fullmatch(lines[j]) and not _CALLOUT.fullmatch(lines[j]):
                j += 1
            body = "\n".join(_QUOTE.fullmatch(row).group(1) for row in lines[i + 1:j]).strip("\n")
            flush()
            out.append(Block(CALLOUT, "\n".join(lines[i:j]), tone=callout.group(1).lower(),
                             title=callout.group(2).strip(), body=body))
            i = j
            continue
        opened = _DETAILS_OPEN.fullmatch(line)
        end = _details_end(lines, i) if opened else None
        if end is not None:
            rest, inner, title = opened.group(2), lines[i + 1:end], ""
            summary = _SUMMARY.fullmatch(rest)
            if summary:
                title, rest = summary.group(1), summary.group(2)
            elif not rest.strip():
                k = next((k for k, row in enumerate(inner) if row.strip()), None)
                summary = _SUMMARY.fullmatch(inner[k]) if k is not None else None
                if summary:
                    title, inner = summary.group(1), [summary.group(2), *inner[k + 1:]]
            body = flatten("\n".join([rest, *inner] if rest.strip() else inner).strip("\n"))
            flush()
            out.append(Block(DETAILS, "\n".join(lines[i:end + 1]), title=title.strip(), body=body,
                             open=bool(opened.group(1))))
            i = end + 1
            continue
        run.append(line)
        i += 1
    flush()
    return out


def rich(text) -> bool:
    """Whether ``text`` holds any block beyond Markdown (a quick look first: most replies hold none)."""
    body = str(text or "")
    lowered = body.lower()
    if "[!" not in body and "<details" not in lowered and "metrics" not in lowered:
        return False
    return any(block.kind != MD for block in blocks(body))


def outside_code(text, change) -> str:
    """``text`` with ``change`` applied to each line outside code fences."""
    lines, out, i = str(text).split("\n"), [], 0
    while i < len(lines):
        if _opens_fence(lines[i]):
            end = _fence_end(lines, i)
            stop = len(lines) if end is None else end + 1
            out.extend(lines[i:stop])
            i = stop
            continue
        out.append(change(lines[i]))
        i += 1
    return "\n".join(out)


def task_marks(text, marks=None) -> str:
    """``text`` with GFM task list items' ``[ ]`` and ``[x]`` as a mark outside code (a card's Markdown shows the
    brackets as they are): a green ✓ and a grey ○ (``TASK_MARKS``), or ``marks`` {done: mark}."""
    marks = marks or TASK_MARKS
    if "[" not in str(text):
        return str(text)
    return outside_code(text, lambda line: _TASK.sub(lambda m: m.group(1) + marks[m.group(2) in "xX"], line))


def callout_head(block) -> str:
    """A callout's first line on a card: its mark and kind in bold, then its title."""
    return f"**{TONE_MARKS[block.tone]} {TONE_WORDS[block.tone]}**" + (f"  {title}" if (title := shown_title(block)) else "")


def shown_title(block) -> str:
    """A callout's title as shown after its kind: none when it only says the kind again ("> [!WARNING] 注意")."""
    return "" if block.title.strip("*：: ") in (TONE_WORDS[block.tone], block.tone, block.tone.upper()) else block.title


def metric_line(item) -> str:
    label, value, delta = item
    return f"{label}：**{value}**" + (f"  <font color='grey'>{delta}</font>" if delta else "")


def card_markdown(text) -> str:
    """``text`` as one card Markdown text: a callout a quote under its kind, a details' summary bold over its body,
    metrics a line an item, task marks as ✓ and ○; ``text`` unchanged when it holds none of them."""
    text = str(text or "")
    if not rich(text):
        return task_marks(loose_tags(text))
    parts = []
    for block in blocks(text):
        if block.kind == CALLOUT:
            inner = card_markdown(block.body)
            # A bare ">" glues the lines around it in a card (cards/review.py question_preview): a full-width space.
            parts.append("\n".join([f"> {callout_head(block)}", *(f"> {row}" if row.strip() else "> 　"
                                                                  for row in inner.split("\n") if inner)]))
        elif block.kind == DETAILS:
            parts.append(f"**{block.title or DETAILS_WORD}**\n{card_markdown(block.body)}".rstrip())
        elif block.kind == METRICS:
            parts.append("\n".join(metric_line(item) for item in block.items))
        else:
            parts.append(task_marks(loose_tags(block.source)))
    return "\n\n".join(parts)


def loose_tags(text) -> str:
    """``text`` with a details left open (a reply still being written) in words outside code: its ``<details>`` line
    dropped, its summary bold, a lone ``</details>`` dropped; a card's Markdown shows the tags as they are."""
    if "<details" not in text.lower() and "</details" not in text.lower():
        return text

    def words(line):
        opened = _DETAILS_OPEN.fullmatch(line)
        if opened:
            summary = _SUMMARY.fullmatch(opened.group(2))
            rest = summary.group(2) if summary else opened.group(2)
            return " ".join(x for x in (f"**{summary.group(1).strip()}**" if summary else "", rest.strip()) if x)
        summary = _SUMMARY.fullmatch(line)
        if summary:
            return f"**{summary.group(1).strip()}**" + (f" {summary.group(2).strip()}" if summary.group(2).strip() else "")
        return "" if _DETAILS_CLOSE.fullmatch(line) else line
    return outside_code(text, words)


def plain(text) -> str:
    """``text`` with its blocks as Markdown words: no ``[!NOTE]``, ``<details>``, ``<summary>`` or metrics fence, task
    marks as ☑ and ☐; cards/review.py plain_markdown strips the remaining marks from it for a title or a preview."""
    text = str(text or "")
    if not rich(text):
        return task_marks(text, {True: "☑", False: "☐"})
    parts = []
    for block in blocks(text):
        if block.kind == CALLOUT:
            parts.append(f"{TONE_WORDS[block.tone]}：" + "\n".join(x for x in (shown_title(block), plain(block.body)) if x))
        elif block.kind == DETAILS:
            parts.append("\n".join(x for x in (block.title, plain(block.body)) if x))
        elif block.kind == METRICS:
            parts.append("\n".join(f"{label}：{value}" + (f"（{delta}）" if delta else "")
                                   for label, value, delta in block.items))
        else:
            parts.append(task_marks(block.source, {True: "☑", False: "☐"}))
    return "\n\n".join(parts)

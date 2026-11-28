"""The digest card (app/digest.py): sent at the times the deployer set, at the top level of the owner's private chat.
It has the 看板's 夜航 look (cards/board.py, docs/ui.md 5.2) and its parts: the conclusion band (要你处理 N 件事 as of its
time, counted as the 看板 and 手上的事 count it: core/board_read.py Asks; the most pressing topic; each state's count as a
segment bar), then 等你 → 停住了 → 可能卡住 → 有结果没看 → 在做, each topic a topic row (cards/ui.py topic_row: its T
number's badge, its state's dot and word, its title, a line in words, grey figures; the whole row opens it, ``url``),
做完 and 已停止 folded last. A part shows SHOWN rows (fewer while the card would pass what a card takes, as the 看板's
FIT_SHOWN) and says how many more there are. Under 等你, 整理专项's proposals waiting for his 好 (no buttons: this card is
a record of its time) and the questions waiting over a day as one grey line of topic numbers, 手上的事's
(cards/overview.py stale_line), with its button 不管了 when ``dismissable``; once clicked, the line is 已撤下 N 个
(dismissed_line), grey, with 撤销 (``cleared_taken``: what it puts back). A project a guard paused is a row of 停住了.

``view`` is plain data (app/digest.py collect, tick): ``slot`` and ``since`` (epoch seconds), ``waiting``, ``running`` and
``done`` rows as the 看板's (label, title, line, meta, url, card, tone, stuck, seen), ``older`` (results not seen that
ended before ``since``), ``stale`` rows {topic, label, title, url}, ``pending`` (proposals), ``projects`` (the projects a guard
paused), ``knowledge``, ``dismissable``, ``cleared`` (the labels
taken off from this card), ``web`` (the web page's address: 打开网页 on the last line). A view saved by an older release
(its link in its label, a ``state`` and ``took`` in place of a line and figures, no ``tone``) is drawn the same way
(``shaped``). ``asked``: the bot menu's 看板 of an older release (随时查看 in the title).

The evening digest's own parts (app/digest.py ``daily``), each left out when the view has none: ``reached`` (本周找你 N 次：问题 a
次、停住 b 次, cards/weekly_usage.py reached_line) and ``proxy`` ({sent, unsent}: 今天代答) as two lines after the topics,
then ``memory_tidy`` (整理记忆, app/memory_tidy.py view_of: {run, away {idle, done, foreign}, undone}): how many lines it put
away and why, with 「撤销」 (MEMORY_KIND, ``op`` undo). A view an older release saved (``remember``, ``wanted``, a ``rules`` list in
``memory_tidy``) is drawn without those parts; ``review`` is the memory's 待审核 line."""
from __future__ import annotations

from covey.cards import board, ui
from covey.cards.project_proposal import board_rows
from covey.cards.elements import callback_button, fits, folded_panel, grey, item, md, menu_item, note, panel_header, styled_panel
from covey.cards.overview import clock, named
from covey.cards.weekly_usage import reached_line
from covey.core import look
from covey.core.text import literal, safe_text

TITLE = "汇总"
SHOWN = 8
FIT_SHOWN = board.FIT_SHOWN  # Rows a part shows, tried in turn until the card fits (``digest_card``).
TITLE_CHARS, STATE_CHARS = 30, 40
PARTS = (("waiting", "等你"), ("running", "在做"), ("done", "做完"))  # The Markdown's parts (``digest_text``).
ASKED = "随时查看"  # 看板's title: the card the owner asked for, not a timed one.
EMPTY = "{} 时没有要你处理的：没有等你的、没有在做的，{} 之后也没有做完的。"  # A record as of its time (the card stays in the chat).
ICON = "waiting_outlined"


ORIGIN = "digest"  # The stale line's button value's ``from`` (app/overview.py claim hands such a click on).
MEMORY_KIND = "bridge_digest_memory"  # 整理记忆's 「撤销」: ``run``, ``op`` undo (an older card's confirm and wrong are retired).
MEMORY_TITLE = "整理记忆"
MEMORY_AWAY = {"done": "办完的", "foreign": "别的专项的", "idle": "很久没用的"}
MEMORY_HINT = "只收起不删除：收起的不再每轮带，在网页记录里能放回。"
GO_LINE = "在菜单「看板」上点「继续」接着做"  # What a project a guard paused waits for (cards/board.py PROJECT_GO_KIND).
# How an older release's row said a state and a question (``shaped``).
STATE_WORDS = frozenset({look.word(key) for key in look.STATES} | {word for words in look.RETIRED.values() for word in words})
ASKED_STARTS = tuple(f"{word}：" for word in (look.word("waiting"), *look.RETIRED["waiting"]))


def rows_of(view, key):
    """How many ``key`` has: the 等你 part's old questions too, not the ones taken off from this card ("等你 4" over
    已撤下 3 个 reads as four still waiting), so a redraw after the click lowers the header
    and the part's count with the line."""
    extra = len(view.get("stale") or []) if key == "waiting" else 0
    return len(view[key]) + extra


def counts(view):
    return " · ".join(f"{name} {rows_of(view, key)}" for key, name in PARTS if rows_of(view, key))


def tail_text(view):
    if view.get("stale"):
        return [f"另有 {len(view['stale'])} 个等了一天以上：{named([row.get('label') for row in view['stale']])}"]
    return [f"已撤下 {len(view['cleared'])} 个：{named(view['cleared'])}"] if view.get("cleared") else []


def line(row, *, plain=False):
    """A row in one line of Markdown (the card refused: ``digest_text``)."""
    parts = [ui.split_link(row.get("label"))[0] if plain else row.get("label") or "",
             safe_text(row.get("title") or "", TITLE_CHARS), safe_text(row.get("state") or "", STATE_CHARS), row.get("took") or ""]
    text = " · ".join(part for part in parts if part)
    if not row.get("seen"):
        return text
    return f"{text} · 已看" if plain else grey(f"{text} · 已看")


def done_since(view):
    """有结果没看's heading after its count: since when, and how many results not seen ended before (``older``)."""
    older = view.get("older") or 0  # A view saved by an older release has none.
    return f"{clock(view['since'])} 之后结束" + (f"，另有更早没看的 {older} 个" if older else "")


def title_of(view):
    return f"{TITLE} · {ASKED} · {clock(view['slot'])}" if view.get("asked") else f"{TITLE} · {clock(view['slot'])}"


def shaped(row):
    """``row`` as the 看板 draws one, from a view of any release: its link taken out of its label (an older release wrote
    the T number as a Markdown link), its line and figures from the state and time it said before, its tone from its
    status (cards/board.py tone), else from those words."""
    row = dict(row)
    label, url = ui.split_link(row.get("label"))
    row["label"] = label
    if url and not row.get("url"):
        row["url"] = url
    if "line" not in row:  # Its state's words as its line: not the state's word again, a question as the 看板 says it.
        words = str(row.get("state") or "")
        asked = next((words[len(start):] for start in ASKED_STARTS if words.startswith(start)), None)
        row["line"] = f"要你定：{asked}" if asked else "" if words in STATE_WORDS else words
    if "meta" not in row:
        row["meta"] = [row.get("took") or ""]
    if not row.get("tone") and not row.get("status"):
        words = str(row.get("state") or "")
        if words == look.word("stopped") or words in look.RETIRED["stopped"]:
            row["tone"] = "stopped"
        elif words == look.word("cancelled"):
            row["tone"] = "cancelled"
    return row


def project_row(project):
    """A project a guard paused, under 停住了: 专项「名」, why, and where 继续 is; it opens its card's topic."""
    reason = project["halted"] if isinstance(project.get("halted"), str) else ""
    return ui.topic_row("stopped", f"专项「{project.get('name') or ''}」", "；".join(part for part in (reason, GO_LINE) if part),
                        url=project.get("url") or "")


def heading_with(key, count, after=""):
    """A part's heading as the 看板 draws one (cards/board.py heading), ``after`` grey behind its count."""
    element = board.heading(key, count)
    if after:
        element["content"] += "  " + grey(literal(after))
    return element


def part_heading(title, count):
    """The heading of a part that lists no topics (整理记忆, 新记的知识), as a group's is drawn."""
    return md(f"**{literal(title)}**  {grey(str(count))}", size="normal_v2")


def digest_card(view):
    """The card for ``view``. Many rows would pass what a card takes (cards/elements.py CARD_NODES, CARD_BYTES): fewer a part
    (FIT_SHOWN) until it fits, the band counting them all the same."""
    for shown in FIT_SHOWN:
        built = _drawn(view, shown)
        if fits(built, board.NODES, board.BYTES):
            break
    return built


def _drawn(view, shown):
    view = {**view, **{key: [shaped(row) for row in view.get(key) or ()] for key in ("waiting", "running", "done")},
            "stale": [{**row, "label": ui.split_link(row.get("label"))[0],
                       "url": row.get("url") or ui.split_link(row.get("label"))[1]} for row in view.get("stale") or ()]}
    found = board.groups(view)
    stopped_projects = board.halted(view)
    sizes = board.state_counts(found, stopped_projects)
    at, since = clock(view["slot"]), clock(view["since"])
    listed = any(found[key] for key in found) or stopped_projects or view.get("cleared")
    quiet = f"{at} 时没有在等你的事" if listed else EMPTY.format(at, since)
    elements = [board.band(found, None, sizes, stopped_projects, label=f"要你处理 · {at}", quiet=quiet)]
    elements += board.waiting_part(view, found, shown, origin=ORIGIN, answer=False)
    if found["stopped"] or stopped_projects:
        elements.append(board.heading("stopped", len(found["stopped"]) + len(stopped_projects)))
        elements += board.blocks("stopped", found["stopped"], board.task_card_button, shown)
        elements += [project_row(project) for project in stopped_projects[:shown]]
    if found["stuck"]:
        elements.append(board.heading("stuck", len(found["stuck"])))
        elements += board.blocks("stuck", found["stuck"], board.task_card_button, shown)
    if found["result"]:
        elements.append(heading_with("result", len(found["result"]), done_since(view)))
        elements += board.blocks("result", found["result"], shown=shown)
    if found["running"]:
        elements.append(board.heading("running", len(found["running"])))
        elements += board.blocks("running", found["running"], shown=shown)
    elements += board.done_fold(found, since, shown)
    lines = daily_lines(view)
    if lines:
        elements.append(md("\n".join(lines)))
    review = review_line(view)
    if review:
        elements.append(md(review, size="notation"))
    elements += memory_elements(view)
    elements += knowledge_elements(view)
    if isinstance(view.get("tidy"), dict):  # A view saved when this card had 整理专项 (it is the 看板's alone now).
        elements += board_rows([str(name) for name in view["tidy"].get("projects") or ()])
    last = note(f"只列这个{view.get('where') or '私聊'}里的话题，管家不列")
    elements.append(item(last, ui.link_button(board.WEB, view["web"])) if view.get("web") else last)
    title = title_of(view)
    tags = board.counts(found, None, sizes)
    head = panel_header(ICON, title, f"{view.get('where') or '私聊'}里在推进的事", tags)
    head["text_tag_list"] = [{"tag": "text_tag", "text": {"tag": "plain_text", "content": text}, "color": color}
                             for text, color in tags[:3]]
    return ui.styled(styled_panel(head, elements, " · ".join([title, *(text for text, _ in tags)]), spacing="8px"))


def daily_lines(view):
    """The evening digest's count lines: 本周找你 and 今天代答; none in a view without them."""
    lines = [reached_line(view["reached"])] if isinstance(view.get("reached"), dict) else []
    proxied = view.get("proxy") if isinstance(view.get("proxy"), dict) else None
    if proxied is not None:
        unsent = int(proxied.get("unsent") or 0)
        lines.append(f"今天代答发出 {int(proxied.get('sent') or 0)} 条" + (f"，{unsent} 条没发出，在菜单「代答」里看" if unsent else ""))
    return lines


def review_line(view, *, plain=False) -> str:
    """The memory's 待审核 line (``view["review"]``: {pending, url}, app/digest.py review_view): ``记忆：待审核 N 条`` and, with the web
    page on, the link to the 网页记忆中心's 待审核 tab; '' when nothing waits or the view has none."""
    part = view.get("review")
    waiting = int(part.get("pending") or 0) if isinstance(part, dict) else 0
    if waiting <= 0:
        return ""
    url = str(part.get("url") or "")
    line = f"记忆：待审核 {waiting} 条"
    if not url:
        return line
    return f"{line} · {url}" if plain else f"{line} · [去网页处理]({url})"


def away_line(part) -> str:
    """整理记忆's count of what it put away: ``收起 14 条：办完的 8 条、别的专项的 2 条、很久没用的 1 条``; '' with none."""
    away = part.get("away") or {}
    counts = [(MEMORY_AWAY[key], int(away.get(key) or 0)) for key in MEMORY_AWAY]
    total = sum(count for _, count in counts)
    if not total:
        return ""
    return f"收起 {total} 条：" + "、".join(f"{words} {count} 条" for words, count in counts if count)


def memory_elements(view):
    """整理记忆's part of the evening digest (``memory_tidy``); none in a view without it."""
    part = view.get("memory_tidy")
    if not isinstance(part, dict) or not part:
        return []
    away = away_line(part)
    elements = [part_heading(MEMORY_TITLE, sum(int((part.get("away") or {}).get(key) or 0) for key in MEMORY_AWAY))]
    value = {"kind": MEMORY_KIND, "run": part.get("run") or ""}
    if away and part.get("undone"):
        elements.append(note(f"{away} · 已撤销，都放回去了"))
    elif away:
        elements.append(item(md(away), callback_button("撤销", {**value, "op": "undo"}, style="text", size="small")))
    elements.append(note(MEMORY_HINT))
    return elements


def memory_lines(view):
    """整理记忆 in Markdown (``digest_text``); none in a view without it."""
    part = view.get("memory_tidy")
    if not isinstance(part, dict) or not part:
        return []
    lines = [f"**{MEMORY_TITLE}**"]
    if away_line(part):
        lines.append(away_line(part) + ("（已撤销）" if part.get("undone") else ""))
    return lines


def knowledge_lines(view):
    """新记的知识's rows (app/digest.py knowledge_view): op · id · group · text; none when the view has no such part
    (a view saved before it, or nothing new)."""
    part = view.get("knowledge") or {}
    return [f"{row['op']} · {row['id']} · {row['where']} · {row['text']}" for row in part.get("rows") or []]


def knowledge_elements(view):
    lines = knowledge_lines(view)
    if not lines:
        return []
    more = (view.get("knowledge") or {}).get("more") or 0
    return [part_heading("新记的知识", len(lines) + more), *(md(text) for text in lines),
            *([note(f"另有 {more} 条")] if more else [])]


def folded_board():
    """看板's card folded in place (a second menu click, app/digest.py fold_board)."""
    return folded_panel("bitablekanban_outlined", "看板", menu_item("看板"))


def digest_text(view):
    """The card in Markdown, for when Feishu refuses the card."""
    lines = [f"**{title_of(view)}**"]
    for key, name in PARTS:
        rows, last = view[key], tail_text(view) if key == "waiting" else []
        if rows or last:
            lines.append(f"**{name}（{rows_of(view, key)}）**")
            lines += [line(row, plain=True) for row in rows[:SHOWN]]
            if len(rows) > SHOWN:
                lines.append(f"另有 {len(rows) - SHOWN} 个")
            lines += last
    if len(lines) == 1:
        lines.append(EMPTY.format(clock(view["slot"]), clock(view["since"])))
    lines += daily_lines(view)
    if review_line(view, plain=True):
        lines.append(review_line(view, plain=True))
    lines += memory_lines(view)
    new = knowledge_lines(view)
    if new:
        more = (view.get("knowledge") or {}).get("more") or 0
        lines += [f"**新记的知识（{len(new) + more}）**", *new, *([f"另有 {more} 条"] if more else [])]
    return "\n".join(lines)

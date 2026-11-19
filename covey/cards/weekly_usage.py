"""The 本周用量 card (app/weekly_usage.py), the bot menu's 用量 panel, in the 夜航 look (docs/ui.md 5.2): the window
in its title bar; the conclusion band with the week's tokens (the cache hits counted in), the tasks, topics and the
cache's share of the input, and how the tasks ended as a segment bar in the words of the state table (core/look.py:
做完, 停住了, 已停止 …); tiles for the input, cache hits and output; the tokens of each day as bars; each Harness's share
of the tokens as a ring and its tasks, tokens and cache hits in a table; the small print, then 更新于 last. Read only,
no buttons; ``usage_text`` is the same figures in Markdown, sent when the card cannot be."""
from __future__ import annotations

import time

from covey.cards import ui
from covey.cards.elements import folded_panel, menu_item, note, panel_header, section, styled_panel, tiles
from covey.core import look
from covey.core.text import short_count

TITLE = "本周用量"
EMPTY = "这 7 天没有任务。"
# How the tasks ended, in the order the band's bar and the text list them: core/look.py STATES keys. A status the table
# does not know (``idle``) is left out of the bar.
ENDED = ("done", "running", "waiting", "queued", "stopped", "cancelled")
BAR_COLOR = "#5E6AD2"  # The trend's bars: the band's indigo (core/look.py BAND rgb), the same in light and dark mode.
RING_FLOOR = 2  # Percent of the tokens a Harness needs for a slice of its own; the rest is one 其他.


def share(part, whole):
    """``part`` of ``whole`` as a whole percent, "<1%" for a sliver, "—" when there is no whole."""
    if not whole:
        return "—"
    value = part * 100 / whole
    return "<1%" if 0 < value < 1 else f"{value:.0f}%"


def _at(seconds):
    return time.strftime("%m-%d %H:%M", time.localtime(seconds))


def stamp(seconds, now=None):
    """``更新于``'s time: 23:10 today, 11-17 23:10 another day (docs/ui.md 7)."""
    now = time.time() if now is None else now
    today = time.strftime("%Y%m%d", time.localtime(now)) == time.strftime("%Y%m%d", time.localtime(seconds))
    return time.strftime("%H:%M" if today else "%m-%d %H:%M", time.localtime(seconds))


def ended_counts(summary):
    """{state key: how many} of how the tasks ended, in ENDED's order, the ones there are."""
    found = summary.get("ended") or {}
    return {key: found[key] for key in ENDED if found.get(key)}


def ended_line(summary):
    """How the tasks ended in words: ``做完 198 · 停住了 1 · 在做 1``."""
    return " · ".join(f"{look.word(key)} {n:,}" for key, n in ended_counts(summary).items())


def headline(summary):
    """The band's grey line: the tasks, the topics and the cache's share of the input."""
    tokens = summary["tokens"]
    return (f"{summary['tasks']:,} 个任务 · {summary['topics']:,} 个话题 · 缓存命中 "
            f"{share(tokens['cache_read_tokens'], tokens['input_tokens'])}")


def total_tokens(tokens):
    """The input, cache hits and writes counted in as 任务详情 counts them, plus the output: the card's total and
    each Harness's share. The owner, 2026-11-06: 按用量来看，应该是显示含缓存的 (until then the cache hits were
    left out, about nine tenths of the input)."""
    return tokens["input_tokens"] + tokens["output_tokens"]


def sections(summary):
    """The card's Markdown blocks, top to bottom; the last one says when the data was read."""
    window = f"最近 {summary['days']} 天：{_at(summary['since'])} 起开始的任务"
    tasks = summary["tasks"]
    if not tasks:
        return [window, EMPTY, f"更新于 {stamp(summary['until'])}"]
    outcomes = ended_line(summary)
    # A turn that asked a question ends 收到回答后继续 and the turn after the answer is another task.
    twice = "（问了问题、收到回答后接着做的算两次）" if summary["outcomes"].get("continued") else ""
    tokens = summary["tokens"]
    total = total_tokens(tokens)
    rows = [f"- {row['title']}：{row['tasks']:,} 次（{share(row['tasks'], tasks)}）· "
            + ("token 无记录" if row["untracked"] == row["tasks"] else
               f"token {short_count(row['tokens'])}（{share(row['tokens'], total)}）· "
               f"缓存命中 {short_count(row['cache'])}（{share(row['cache'], row['input'])}）")
            for row in summary["harnesses"]]
    lines = [f"**token 总量 {short_count(total)}**（输入 + 输出，含缓存命中，含子任务）",
             f"- 输入：{short_count(tokens['input_tokens'])}（含缓存命中）",
             f"- 缓存命中：{short_count(tokens['cache_read_tokens'])}（占输入 "
             f"{share(tokens['cache_read_tokens'], tokens['input_tokens'])}）",
             f"- 输出：{short_count(tokens['output_tokens'])}"]
    # Its own block: Feishu runs a line that follows a list item on into that item (输出：4591 万另有 …).
    untracked = ([f"另有 {summary['untracked']:,} 次任务没有 token 记录（如还没收到模型回复就停了），不在总量里。"]
                 if summary["untracked"] else [])
    return [window,
            f"**任务 {tasks:,} 次**{twice} · 话题 {summary['topics']:,} 个\n{outcomes}",
            "**各 Harness**（次数占比 · token 占比，token 含缓存命中）\n" + "\n".join(rows),
            "\n".join(lines),
            *untracked,
            f"更新于 {stamp(summary['until'])}"]


def preview(summary):
    """The chat list's preview of the card."""
    if not summary["tasks"]:
        return f"{TITLE} · 没有任务"
    return f"{TITLE} · {summary['tasks']:,} 次任务 · token {short_count(total_tokens(summary['tokens']))}"


def ring(rows, total):
    """Each Harness's share of the tokens as a ring, the total in its middle; small shares as one 其他."""
    slices = [(row["title"], row["tokens"] * 100 / total) for row in rows if row["tokens"] * 100 >= RING_FLOOR * total]
    rest = 100 - sum(value for _, value in slices)
    if rest >= 0.5:
        slices.append(("其他", rest))
    values = [{"name": f"{title}  {value:.0f}%", "value": round(value, 1)} for title, value in slices]
    # A fixed height keeps the ring short (aspect_ratio takes only 1:1, 2:1, 4:3 and 16:9; 2:1 was 270px).
    return {"tag": "chart", "aspect_ratio": "2:1", "height": "180px", "chart_spec": {
        "type": "pie", "data": {"values": values}, "categoryField": "name", "valueField": "value",
        "outerRadius": 0.9, "innerRadius": 0.62, "padAngle": 1, "label": {"visible": False},
        "legends": {"visible": True, "orient": "right"},
        "indicator": {"visible": True, "title": {"style": {"text": "token"}},
                      "content": [{"style": {"text": short_count(total)}}]}}}


def table(rows, tasks, total):
    """Harness, its tasks and its tokens, each with its share, and its cache hits with their share of its input."""
    def tokens(row):
        return "无记录" if row["untracked"] == row["tasks"] else f"{short_count(row['tokens'])} · {share(row['tokens'], total)}"

    def cache(row):
        return "无记录" if row["untracked"] == row["tasks"] else f"{short_count(row['cache'])} · {share(row['cache'], row['input'])}"
    return {"tag": "table", "page_size": min(max(len(rows), 1), 10), "row_height": "low",
            "header_style": {"text_align": "left", "text_size": "normal", "background_style": "none",
                             "text_color": "grey", "bold": False, "lines": 1},
            # Short headers on one line; what the figures leave out is the small print's first line.
            "columns": [{"name": "harness", "display_name": "Harness", "data_type": "text", "width": "32%"},
                        {"name": "tasks", "display_name": "次数", "data_type": "text", "width": "18%",
                         "horizontal_align": "right"},
                        {"name": "tokens", "display_name": "token", "data_type": "text", "width": "25%",
                         "horizontal_align": "right"},
                        {"name": "cache", "display_name": "缓存命中", "data_type": "text", "width": "25%",
                         "horizontal_align": "right"}],
            "rows": [{"harness": row["title"], "tasks": f"{row['tasks']:,} · {share(row['tasks'], tasks)}",
                      "tokens": tokens(row), "cache": cache(row)} for row in rows]}


def trend(daily):
    """The tokens of each day (``daily``: app/weekly_usage.py summarize) as bars, in 万 or in 亿 as the largest day
    needs, each bar's figure on it; None when no day has tokens."""
    top = max((day["tokens"] for day in daily), default=0)
    if not top:
        return None
    unit, size = ("亿", 10 ** 8) if top >= 99995000 else ("万", 10 ** 4)
    values = [{"day": day["day"], "value": round(day["tokens"] / size, 2 if size > 10 ** 4 else 1)} for day in daily]
    return [section(f"每天的 token（{unit}）"),
            {"tag": "chart", "aspect_ratio": "2:1", "height": "150px", "chart_spec": {
                "type": "bar", "data": {"values": values}, "xField": "day", "yField": "value", "color": [BAR_COLOR],
                "label": {"visible": True}, "axes": [{"orient": "left", "visible": False},
                                                     {"orient": "bottom", "label": {"visible": True}}],
                "barMaxWidth": 28}}]


def band(summary):
    """The conclusion band: the week's tokens, the tasks and how they ended."""
    tokens = summary["tokens"]
    return ui.conclusion_band("本周 token · 含缓存命中和子任务", ui.number(short_count(total_tokens(tokens)), "token"),
                              headline(summary), below=ui.seg_bar(ended_counts(summary)))


def usage_card(summary):
    """The card: the window as the subtitle, then the band, tiles, trend, ring, table, small print and 更新于."""
    head = panel_header("usage", TITLE, f"{_at(summary['since'])} – {_at(summary['until'])} · 最近 {summary['days']} 天")
    tasks = summary["tasks"]
    updated = note(f"更新于 {stamp(summary['until'])}")
    if not tasks:
        return ui.styled(styled_panel(head, [ui.conclusion_band("最近 7 天", "**没有任务**", EMPTY), updated],
                                      preview(summary)))
    tokens = summary["tokens"]
    total = total_tokens(tokens)
    elements = [band(summary),
                tiles(("输入", short_count(tokens["input_tokens"]), "含缓存命中"),
                      ("缓存命中", short_count(tokens["cache_read_tokens"]),
                       f"占输入 {share(tokens['cache_read_tokens'], tokens['input_tokens'])}"),
                      ("输出", short_count(tokens["output_tokens"]), ""))]
    elements.extend(trend(summary.get("daily") or ()) or ())
    elements.append(section("各 Harness"))
    if total:
        elements.append(ring(summary["harnesses"], total))
    elements.append(table(summary["harnesses"], tasks, total))
    small = ["表里 token 含缓存命中；缓存命中后的百分比是它占这个 Harness 输入的比例。"]
    if summary["outcomes"].get("continued"):
        small.append("问了问题、收到回答后接着做的算两次任务。")
    if summary["untracked"]:
        small.append(f"另有 {summary['untracked']:,} 次任务没有 token 记录（如还没收到模型回复就停了），不在总量里。")
    elements.extend([note("\n".join(small)), updated])
    return ui.styled(styled_panel(head, elements, preview(summary)))


def folded_card():
    """The card folded in place (app/weekly_usage.py, a second menu click)."""
    return folded_panel("usage", TITLE, menu_item("用量"))


def usage_text(summary):
    return "\n\n".join([f"**{TITLE}**", *sections(summary)])

"""The 本周用量 card (app/weekly_usage.py), the bot menu's 用量 panel: the window in its title bar; tiles for
the tasks, the tokens (the cache hits counted in) and the cache's share of the input; how the tasks ended as
coloured tags; each Harness's share of the tokens as a ring and its tasks, tokens and cache hits in a
table; the input and output last. Read only, no buttons; ``usage_text`` is the same figures in Markdown, sent when the
card cannot be."""
from __future__ import annotations

import time

from covey.cards.elements import folded_panel, md, menu_item, note, panel_header, styled_panel, tag, tiles
from covey.core.text import short_count

TITLE = "本周用量"
# How a task ended, in the order the card lists them; "active" is every status a later one can follow.
OUTCOMES = (("done", "完成"), ("continued", "收到回答后继续"), ("failed", "失败"), ("cancelled", "取消"),
            ("interrupted", "中断"), ("active", "进行中"), ("other", "其他"))
COLORS = {"done": "green", "continued": "blue", "failed": "red", "cancelled": "neutral", "interrupted": "orange",
          "active": "wathet", "other": "neutral"}
RING_FLOOR = 2  # Percent of the tokens a Harness needs for a slice of its own; the rest is one 其他.


def share(part, whole):
    """``part`` of ``whole`` as a whole percent, "<1%" for a sliver, "—" when there is no whole."""
    if not whole:
        return "—"
    value = part * 100 / whole
    return "<1%" if 0 < value < 1 else f"{value:.0f}%"


def _at(seconds):
    return time.strftime("%m-%d %H:%M", time.localtime(seconds))


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
        return [window, "这段时间没有任务。", f"数据截至 {_at(summary['until'])}"]
    outcomes = " · ".join(f"{label} {summary['outcomes'][key]:,}" for key, label in OUTCOMES
                          if summary["outcomes"].get(key))
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
            f"数据截至 {_at(summary['until'])}"]


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


def usage_card(summary):
    """The card: the window as the subtitle, then the tiles, tags, ring, table and small print."""
    head = panel_header("usage", TITLE, f"{_at(summary['since'])} – {_at(summary['until'])} · 最近 {summary['days']} 天")
    tasks = summary["tasks"]
    if not tasks:
        return styled_panel(head, [md("这段时间没有任务。")], preview(summary))
    tokens = summary["tokens"]
    total = total_tokens(tokens)
    elements = [tiles(("任务", f"{tasks:,}", f"{summary['topics']:,} 个话题"), ("token", short_count(total), "含缓存命中"),
                      ("缓存命中", share(tokens["cache_read_tokens"], tokens["input_tokens"]), "占输入")),
                md(" ".join(tag(f"{label} {summary['outcomes'][key]:,}", COLORS[key]) for key, label in OUTCOMES
                            if summary["outcomes"].get(key)))]
    if total:
        elements.append(ring(summary["harnesses"], total))
    elements.append(table(summary["harnesses"], tasks, total))
    small = ["表里 token 含缓存命中；缓存命中后的百分比是它占这个 Harness 输入的比例。",
             f"输入 {short_count(tokens['input_tokens'])}（其中缓存命中 {short_count(tokens['cache_read_tokens'])}）· "
             f"输出 {short_count(tokens['output_tokens'])}，含子任务"]
    if summary["outcomes"].get("continued"):
        small.append("问了问题、收到回答后接着做的算两次任务。")
    if summary["untracked"]:
        small.append(f"另有 {summary['untracked']:,} 次任务没有 token 记录（如还没收到模型回复就停了），不在总量里。")
    elements.append(note("\n".join(small)))
    return styled_panel(head, elements, preview(summary))


def folded_card():
    """The card folded in place (app/weekly_usage.py, a second menu click)."""
    return folded_panel("usage", TITLE, menu_item("用量"))


def usage_text(summary):
    return "\n\n".join([f"**{TITLE}**", *sections(summary)])

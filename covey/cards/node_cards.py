"""The cards of nodes (节点, core/nodes.py): an alert (⚠️ testbox 内存紧张, what was seen, since when, who used the most
memory; for a disk who wrote the most, how fast it grew and its biggest places) and the same card redrawn in place
once it clears (✅ … 已恢复 · 持续 25 分钟); ``alert_text`` is the Markdown sent when Feishu refuses the card. Times are the machine's local time, as on every card.

``machine_lines`` says one node's state in a line or two, for the 设备 tab (app/node_panel.py) and the butler.
"""
from __future__ import annotations

import time

from covey.cards.elements import md, note, panel_header, styled_panel
from covey.core.text import safe_text

GIB = 1 << 30
TITLES = {"mem": "内存紧张", "oom": "有进程因内存不足被杀", "disk": "磁盘快满", "write": "磁盘写不进", "offline": "失联"}


def clock(seconds, *, dated=False):
    return time.strftime("%m-%d %H:%M" if dated else "%H:%M", time.localtime(seconds))


def since(seconds):
    """When an alert began, dated when not today: one that waited out the night (app/node_alerts.py hold) began
    yesterday."""
    return clock(seconds, dated=time.localtime(seconds)[:3] != time.localtime()[:3])


def lasted(seconds):
    minutes = int(max(0, seconds) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")


def size(value):
    if not isinstance(value, (int, float)):
        return "?"
    if value >= GIB:
        return f"{value / GIB:.1f} GiB"
    return f"{value / (1 << 20):.0f} MiB"


def family(rule):
    return rule.partition(":")[0]


def title_of(event, name):
    return f"{safe_text(name, 40)} {TITLES.get(family(event.get('rule', '')), '告警')}"


def what(event):
    """The line saying what was seen."""
    rule = family(event.get("rule", ""))
    if rule == "mem":
        return f"内存用到 {event.get('pct')}%（{size(event.get('used'))} / {size(event.get('limit'))}），超过 1 分钟"
    if rule == "oom":
        return f"系统按内存杀掉了 {event.get('count', 1)} 个进程" + (f"，当时内存 {event['pct']}%" if event.get("pct") else "")
    if rule == "disk":
        return f"{safe_text(event.get('path', ''), 80)} 用了 {event.get('pct')}%，剩 {size(event.get('free'))}"
    if rule == "write":
        return f"{safe_text(event.get('path', ''), 80)} 试写失败：{safe_text(event.get('error') or '原因不明', 60)}"
    if rule == "offline":
        return f"{lasted(event.get('silent', 0))}没有收到它的消息" + (f"（最后 {clock(event['heard'])}）" if event.get("heard") else "")
    return safe_text(event.get("rule", ""), 60)


def top_lines(top, tasks):
    """Who used the most: ``comm`` RSS, CPU, and the topic of its task when ``tasks`` knows it."""
    lines = []
    for proc in top or []:
        line = f"{safe_text(proc.get('comm', '?'), 30)} {size(proc.get('rss'))}"
        if proc.get("cpu"):
            line += f" · CPU {proc['cpu']}%"
        task = tasks.get(proc.get("task") or "")
        if task:
            line += f" · {safe_text(task, 40)}"
        lines.append(line)
    return lines


def span_text(seconds):
    """近 1 小时 / 近 24 小时 / 近 40 分钟, for a span the node measured."""
    minutes = int(max(0, seconds or 0) // 60)
    if minutes >= 55:
        return f"近 {round(minutes / 60)} 小时"
    return f"近 {max(1, minutes)} 分钟"


def growth_line(event):
    """增长：近 1 小时 +2.0 GiB，近 24 小时 +45.0 GiB；按近 1 小时的速度约 23 小时写满."""
    spans = [g for g in event.get("growth") or [] if isinstance(g, dict) and isinstance(g.get("bytes"), (int, float))]
    if not spans:
        return ""
    parts = [f"{span_text(g.get('secs'))} {'+' if g['bytes'] >= 0 else '−'}{size(abs(g['bytes']))}" for g in spans]
    line = "**增长**：" + "，".join(parts)
    rising = next((g for g in spans if g["bytes"] > 0 and g.get("secs")), None)
    free = event.get("free")
    if rising and isinstance(free, (int, float)) and free > 0:
        line += f"；按{span_text(rising['secs'])}的速度约 {lasted(free / rising['bytes'] * rising['secs'])}写满"
    return line


def writer_lines(writers, tasks):
    """Who wrote the most: ``comm`` bytes written, and the topic of its task when ``tasks`` knows it."""
    lines = []
    for proc in writers or []:
        line = f"{safe_text(proc.get('comm', '?'), 30)} 写了 {size(proc.get('bytes'))}"
        task = tasks.get(proc.get("task") or "")
        if task:
            line += f" · {safe_text(task, 40)}"
        lines.append(line)
    return lines


def dir_lines(dirs):
    """The biggest places: a directory and its size, or the files lying loose in one (with their name shape)."""
    lines = []
    for item in (dirs or {}).get("items") or []:
        where = safe_text(item.get("path", "?"), 80)
        if item.get("files"):
            line = f"{where} 下散落的 {item['files']} 个文件 {size(item.get('bytes'))}"
            if item.get("shape"):
                line += f"（多为 {safe_text(item['shape'], 60)}）"
        else:
            line = f"{where} {size(item.get('bytes'))}"
        lines.append(line)
    return lines


def disk_sections(event, tasks):
    """(title, lines, grey note) of each part a disk alert adds; a part with nothing to say is left out."""
    out = []
    growth = growth_line(event)
    if growth:
        out.append((growth, [], ""))
    writers = event.get("writers") or []
    lines = writer_lines(writers, tasks)
    if lines:
        out.append((f"**写入最多的进程**（{span_text(writers[0].get('secs'))}，所有盘合计，含网络盘）", lines, ""))
    dirs = event.get("dirs") or {}
    lines = dir_lines(dirs)
    if lines:
        grey = f"统计于 {clock(dirs['at'])}" if dirs.get("at") else ""
        if dirs.get("partial"):
            grey += ("，" if grey else "") + "限时内没统计完，只算了一部分"
        out.append(("**大目录**", lines, grey))
    return out


def sections(event, tasks):
    if family(event.get("rule", "")) in ("disk", "write"):
        return disk_sections(event, tasks)
    lines = top_lines(event.get("top"), tasks)
    return [("**内存占用最多**", lines, "")] if lines else []


def alert_card(event, name, tasks):
    head = panel_header("robot_outlined", f"⚠️ {title_of(event, name)}", color="red")
    elements = [md(safe_text(what(event), 300)), note(f"从 {since(event.get('at') or time.time())} 起")]
    for title, lines, grey in sections(event, tasks):
        elements.append(md("\n".join([title] + [f"- {line}" for line in lines])))
        if grey:
            elements.append(note(grey))
    return styled_panel(head, elements, f"⚠️ {title_of(event, name)}")


def cleared_card(event, name, tasks, cleared_at):
    since = event.get("at") or cleared_at
    head = panel_header("robot_outlined", f"✅ {title_of(event, name)} · 已恢复", color="green")
    elements = [md(f"✅ 已恢复 · 持续 {lasted(cleared_at - since)}（{clock(since)}–{clock(cleared_at)}）"),
                note(safe_text(what(event), 300))]
    return styled_panel(head, elements, f"✅ {title_of(event, name)} · 已恢复")


def alert_text(event, name, tasks):
    lines = [f"⚠️ **{title_of(event, name)}**", safe_text(what(event), 300), f"从 {since(event.get('at') or time.time())} 起"]
    for title, rows, grey in sections(event, tasks):
        lines += [title] + [f"- {row}" for row in rows] + ([grey] if grey else [])
    return "\n".join(lines)


def percent(value):
    return f"{value:.0f}%" if isinstance(value, (int, float)) else "?"


def machine_lines(name, link, latest, *, online, now=None):
    """(the line, the grey line under it) for one node: 🟢 testbox · CPU 12% · 内存 41% · 盘 87%."""
    now = time.time() if now is None else now
    if not latest and not link:
        return f"⚪ {safe_text(name, 40)} · 还没连上过", ""
    if not online:
        heard = float(link.get("heard") or latest.get("got") or 0)
        return (f"🔴 {safe_text(name, 40)} · 失联 {lasted(now - heard)}" if heard else f"🔴 {safe_text(name, 40)} · 失联",
                f"最后：CPU {percent((latest.get('cpu') or {}).get('pct'))} · 内存 {percent((latest.get('mem') or {}).get('pct'))}")
    cpu, mem = latest.get("cpu") or {}, latest.get("mem") or {}
    disks = [d for d in latest.get("disk") or [] if isinstance(d, dict) and isinstance(d.get("pct"), (int, float))]
    fullest = max(disks, key=lambda d: d["pct"]) if disks else None
    tight = (isinstance(mem.get("pct"), (int, float)) and mem["pct"] >= 85) or (fullest and fullest["pct"] >= 85)
    parts = [f"{'🟠' if tight else '🟢'} {safe_text(name, 40)}", f"CPU {percent(cpu.get('pct'))}",
             f"内存 {percent(mem.get('pct'))}"]
    if fullest:
        parts.append(f"盘 {percent(fullest['pct'])}" + (" ⚠" if fullest["pct"] >= 85 else ""))
    gpus = latest.get("gpu") or []
    if gpus:
        busy = sum(1 for g in gpus if (g.get("util") or 0) >= 50)
        parts.append(f"GPU {busy}/{len(gpus)} 忙")
    info = link.get("info") or {}
    grey_bits = []
    if mem.get("limit"):
        grey_bits.append(f"内存 {size(mem.get('used'))} / {size(mem['limit'])}")
    if cpu.get("cores"):
        grey_bits.append(f"{cpu['cores']:g} 核")
    if info.get("hostname"):
        grey_bits.append(safe_text(info["hostname"], 40))
    return " · ".join(parts), " · ".join(grey_bits)

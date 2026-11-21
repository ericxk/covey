"""The 设置 card's 能做什么 tab (app/about_panel.py): what this deployment can do and what it lacks, read only.

One layer under the tab row: 这台机器 (the Harnesses this Bridge enables, those unavailable now), 能做的 (each feature on,
one line of what it does and, grey, the switch it reads), 还没开的 (each feature off, one line of how to get it: 没有
Claude Code → 没有管家), 扩展 (the enabled packages), and a grey line on where the switches live. No button: a tap on the
tab draws it again from the deployment as it is then. ``view`` (app/about_panel.py ``facts``):

- ``host``: the machine's name; ``harnesses``: the titles enabled; ``down``: [title, why] of those unavailable now;
- ``features``: [{name, on, line, switch}] in the order of app/about_panel.py FEATURES;
- ``extensions``: the enabled extension packages' names;
- ``notice``: one line on top (a refusal).
"""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import grey, md, note, panel_header, section, styled_panel
from covey.core.text import literal

TITLE = "能做什么"
ICON = "setting_outlined"
SWITCHES = "开关写在服务清单里：照 docs/configuration.md 的「改开关三步」改完重新注册服务，再点这一页看。这一页只读。"


def machine_lines(view) -> str:
    lines = []
    if view.get("harnesses"):
        lines.append("Harness：" + "、".join(literal(title) for title in view["harnesses"]))
    else:
        lines.append("Harness：一个都没启用")
    down = [f"{literal(title)}（{literal(why)}）" for title, why in view.get("down") or []]
    if down:
        lines.append("现在用不了：" + "、".join(down))
    return "\n".join(lines)


def feature_line(row) -> str:
    """``- 名字：一句话`` and, for one on, its switch in grey."""
    text = f"- **{literal(row['name'])}**：{literal(row['line'])}"
    return text + (" " + grey(literal(row["switch"])) if row.get("on") and row.get("switch") else "")


def about_card(token, view, *, top=()):
    """The tab for ``view`` (the module's docstring); ``top``: the tab row."""
    del token  # No button carries it: the tab row has its own.
    features = view.get("features") or []
    on = [row for row in features if row.get("on")]
    off = [row for row in features if not row.get("on")]
    elements = [*top]
    if view.get("notice"):
        elements.append(ui.receipt(view["notice"]))
    elements += [section("这台机器", "computer_outlined"), md(machine_lines(view))]
    if on:
        elements += [section(f"能做的（{len(on)}）", "yes_outlined"), md("\n".join(map(feature_line, on)))]
    if off:
        elements += [section(f"还没开的（{len(off)}）", "time_outlined"), md("\n".join(map(feature_line, off)))]
    names = view.get("extensions") or []
    elements += [section("扩展"), md("、".join(literal(name) for name in names) if names else "没有启用扩展")]
    elements.append(note(SWITCHES))
    subtitle = f"装在 {view['host']}" if view.get("host") else ""
    summary = f"{TITLE} · 开着 {len(on)} 项，没开 {len(off)} 项"
    return styled_panel(panel_header(ICON, TITLE, subtitle), elements, summary)

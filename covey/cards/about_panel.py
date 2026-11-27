"""The 设置 card's 能做什么 part (app/about_panel.py): what this deployment can do and what it lacks, read only. It is
the lower half of the 诊断 tab now (cards/doctor_card.py ``below``; the 能做什么 tab is retired into 诊断).

One layer under the tab row: 这台机器 (the Harnesses this Bridge enables, those unavailable now), then the features in three
groups by what the owner does with them (T647 D6: 交给它办事, 不断、不丢, 看得全、记得住), each line what it does for him first:
on, its name bold and, grey, a detail and the switch it reads; off, the whole line grey with how to get it (管家用的 Harness 没启用
→ 没有管家). Then 扩展 (the enabled packages) and a grey line with the command that changes a switch. No button: a tap on the tab
draws it again from the deployment as it is then. ``view`` (app/about_panel.py ``facts``):

- ``host``: the machine's name; ``harnesses``: the titles enabled; ``down``: [title, why] of those unavailable now;
- ``groups``: [key, title] in order; ``features``: [{name, group, good, on, line, switch}] in the order of app/about_panel.py
  FEATURES (a row without a group goes in the last);
- ``extensions``: the enabled extension packages' names; ``state_dir``: the service's, for the command;
- no ``notice``: the 诊断 card has the refusal line.
"""
from __future__ import annotations

from covey.cards.elements import grey, md, note, section
from covey.core.text import literal

TITLE = "能做什么"
ICON = "setting_outlined"
STATE = "<状态目录>"  # The command's --state-dir when the view has none.
ICONS = {"work": "yes_outlined", "safe": "safe_outlined", "see": "insert-chart_outlined"}


def switches_note(state_dir="") -> str:
    """Where the switches live and the command that changes one, with the service's state directory."""
    state = state_dir or STATE
    return (f"开关写在服务清单里。改一个：在服务的代码目录里运行 python3 -B bin/covey_service.py set --state-dir {state} 开关=值"
            "（先加 --dry-run 看会改什么），再点这一页看；想一次打开演示用的一组，加 --preset showcase。这一页只读。")


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
    """On: ``- **名字**：好处`` and, grey, its detail and switch. Off: the whole line grey, ``名字：好处 · 没开：怎么开``."""
    good = literal(row.get("good") or "")
    if row.get("on"):
        extra = "（" + literal(row["line"]) + "）" if row.get("line") else ""
        text = f"- **{literal(row['name'])}**：{good}{extra}"
        return text + (" " + grey(literal(row["switch"])) if row.get("switch") else "")
    how = " · 没开：" + literal(row["line"]) if row.get("line") else " · 没开"
    return "- " + grey(f"{literal(row['name'])}：{good}{how}")


def about_elements(view, *, heading=False):
    """The part's elements for ``view`` (the module's docstring); ``heading``: a 能做什么 section first, for the part under
    another tab's body."""
    features = view.get("features") or []
    groups = [tuple(group) for group in view.get("groups") or ()] or [("", "能做的")]
    keys = [key for key, _ in groups]
    elements = [section(TITLE, ICON)] if heading else []
    elements += [section("这台机器", "computer_outlined"), md(machine_lines(view))]
    for key, title in groups:
        rows = [row for row in features if (row.get("group") if row.get("group") in keys else keys[-1]) == key]
        if not rows:
            continue
        rows = [*[row for row in rows if row.get("on")], *[row for row in rows if not row.get("on")]]
        count = sum(1 for row in rows if row.get("on"))
        elements += [section(f"{title}（开着 {count}/{len(rows)}）", ICONS.get(key, "yes_outlined")),
                     md("\n".join(map(feature_line, rows)))]
    names = view.get("extensions") or []
    elements += [section("扩展"), md("、".join(literal(name) for name in names) if names else "没有启用扩展")]
    elements.append(note(literal(switches_note(view.get("state_dir") or ""))))
    return elements

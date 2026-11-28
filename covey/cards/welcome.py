"""The welcome card (app/welcome.py) in the 夜航 look: what this machine has, how a message is answered, three examples.
Sent once per deployment, at the top of the owner's private chat.

``welcome_card(view)``: the title bar (the machine), the band (with the butler: 有事直接在这里说 and what it does; without
it: 每条消息开一个话题, why there is no butler and, in the band's 要你做 bar, how to get it), the line under it (with the
butler: a reaction on his message means it was received, a card follows when there is something to show; without it:
the launch card), 这台机器 (the Harnesses, those unavailable now, the home), 先试一个 (each example with
发这句, 已发出 once tried) and a grey line. ``view``:

- ``host``: the machine's name; ``harnesses``: the titles of the Harnesses this Bridge enables; ``down``: [title, why] of
  those the availability watch found unavailable;
- ``workspace``, ``workdir``: the default workspace's name and directory, the deployment's home (shown as 主目录, its directory);
  ``bare_workspace``: True while it holds no project (BARE_WORKSPACE under it);
- ``butler``: '' with the butler, else why not: NO_CLAUDE, OFF, DOWN (BUTLER_WORDS);
- ``examples``: their words; ``tried``: the examples clicked, by their index as a string, and what came of each
  (TRIED: 已发出, the button off; anything else, FAILED, the button again); ``notice``: one grey line under them.
"""
from __future__ import annotations

from covey.cards import ui
from covey.cards.elements import callback_button, item, md, note, panel_header, section, styled_panel
from covey.core.text import literal

KIND = "bridge_welcome_try"  # 发这句: the value carries ``example``, its index.
ICON = "start_outlined"
TITLE = "欢迎使用 Covey"
TRY, TRIED_LABEL = "发这句", "已发出"
# Under the home while it holds no project (``bare_workspace``): how to put one in its place.
BARE_WORKSPACE = "里面还没有项目：把你的项目放进主目录，或在网页设置的「常用路径」页加上；新话题都从主目录开始"
STARTING, SENT, FAILED, UNCERTAIN = "starting", "sent", "failed", "uncertain"  # What became of a click on an example.
TRIED = frozenset({STARTING, SENT, UNCERTAIN})  # The button is off: never sent twice (a failed one is offered again).
NO_CLAUDE, OFF, DOWN = "no_claude", "off", "down"  # Why the private chat has no butler.
# Why there is no butler (the band's grey line) and how to get it (its 要你做 bar, labelled 要管家).
BUTLER_WORDS = {
    NO_CLAUDE: ("管家用的 Harness 在这台机器上没启用",
                "装好并登录 Claude Code、Codex 或 Antigravity 中的一个，再运行一次 ./install；选过别的 Harness 的，在「设置」→「配置」的管家一段换成启用了的"),
    OFF: ("部署时关了管家（COVEY_BUTLER=0）", "去掉这个设置，照 docs/configuration.md 的「改开关三步」重新注册服务"),
    DOWN: ("管家用的 Harness 刚才没起来", "几分钟后自动再试，不用做什么"),
}


def example_rows(view) -> list:
    """Each example with its button: 发这句, or 已发出 (off) once tried."""
    tried = view.get("tried") or {}
    rows = []
    for index, text in enumerate(view.get("examples") or []):
        done = (tried.get(str(index)) or {}).get("state") in TRIED
        value = {"kind": KIND, "example": index}
        rows.append(item(md(literal(text)), callback_button(TRIED_LABEL if done else TRY, value, style="default",
                                                            disabled=True if done else None, size="small")))
    return rows


def machine_lines(view) -> str:
    """这台机器: the Harnesses, those unavailable now, the home."""
    lines = []
    if view.get("harnesses"):
        lines.append("Harness：" + "、".join(literal(title) for title in view["harnesses"]))
    down = [f"{literal(title)}（{literal(why)}）" for title, why in view.get("down") or []]
    if down:
        lines.append("现在用不了：" + "、".join(down))
    if view.get("workspace"):
        lines.append(f"主目录：{literal(view['workdir'] or view['workspace'])}")
        if view.get("bare_workspace"):
            lines.append(ui.font(BARE_WORKSPACE, "grey"))
    return "\n".join(lines)


def welcome_card(view) -> dict:
    """The whole card for ``view`` (the module's docstring), sent once and drawn again from the same view."""
    butler = view.get("butler") or ""
    if butler:
        why, how = BUTLER_WORDS.get(butler, BUTLER_WORDS[NO_CLAUDE])
        band = ui.conclusion_band("装好了", "**每条消息开一个话题**", literal(why), below=[ui.todo_bar(how, label="要管家")])
        line = "每条消息下面开一个话题，先出启动卡，点「开始任务」才开工。"
    else:
        band = ui.conclusion_band("装好了", "**有事直接在这里说**", "管家先接住每句话：能直接答的就答，要查、要跑、要改的开话题去做")
        line = "你的话下面出现表情，就是收到了；有东西要给你看时才发卡片。"
    elements = [band, md(line)]
    machine = machine_lines(view)
    if machine:
        elements += [section("这台机器", "computer_outlined"), md(machine)]
    elements.append(section("先试一个", "chat_outlined"))
    elements += example_rows(view)
    if view.get("notice"):
        elements.append(note(literal(view["notice"])))
    elements.append(note("这几句都只读，不改文件。发完点菜单「看板」，看它们做到哪、有没有在等你；新话题用的模型在「设置」里改。"))
    headline = "每条消息开一个话题" if butler else "有事直接在这里说"
    head = panel_header(ICON, TITLE, f"装在 {view['host']}" if view.get("host") else "")
    return ui.styled(styled_panel(head, elements, f"{TITLE} · {headline}", spacing="8px"))

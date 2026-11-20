"""The card sketches in README.md, README.en.md and docs/usage.md (docs/media/screenshots), drawn again from the cards
of the code at hand: the real card builders with demo data, a small HTML renderer of the schema 2.0 elements they
use, a headless Chromium for the PNGs (docs/testing.md 卡片示意图).

    python3 -B scripts/card_sketches.py --out /absolute/dir                 # the HTML pages only
    <python with Playwright> -B scripts/card_sketches.py --out /absolute/dir --png [--browser /absolute/chrome]
    <python with Pillow> -B scripts/card_sketches.py --out /absolute/dir --install   # the PNGs into the docs

Only demo data: no real name, chat, open_id or path. The renderer stops at an element it does not know rather than
leave it out; Feishu draws the real thing, so the pages are sketches (fonts, spacing and emoji differ).
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SHOTS_DIR = REPO / "docs" / "media" / "screenshots"
FROZEN = 1795154760.0  # 2026-11-20 14:06 at UTC+8: the demo's clock.
ZONE = "CST-8"
ASK = "给 demo-app 加个登录页，邮箱加密码就行"
TITLE = "给 demo-app 加登录页"
LABEL = "T7"
CAPTION = "卡片示意 · 演示数据，由卡片 JSON 渲染，非真实客户端截图"
INPUTS = ("Claude Code", "Claude 自带", "Opus 5.5", "high", False, "demo-app")
CHOICES = {
    "claude:opus": {"harness": "claude", "title": "Claude Code", "provider": "anthropic", "provider_title": "Claude 自带",
                    "display_name": "Opus 5.5", "redaction": True, "label": "Claude Code · Opus 5.5",
                    "levels": ["low", "high", "xhigh", "max"]},
    "codex:gpt": {"harness": "codex", "title": "Codex", "provider": "openai", "provider_title": "Codex 自带",
                  "display_name": "GPT-6", "label": "Codex · GPT-6", "levels": ["low", "high", "xhigh"]},
}


# ---------------------------------------------------------------------------------------------------- the cards


def demo_job(**fields):
    """The demo topic's running task as the task card sees it."""
    from covey.cards.answer_card import input_parts
    from covey.core.harness import HarnessCapabilities
    values = dict(job_id=12, user_text=ASK, topic_id="t" * 64, topic_label=LABEL, message_id="om_demo",
                  chat_id="oc_demo", reply_to_message_id="om_root", harness_id="claude", binding_revision=1,
                  model="claude-opus-5-5", effort="high", timeout=0, kind="chat", title=TITLE,
                  harness_capabilities=HarnessCapabilities(steering=True), execution_started=True,
                  input_parts=input_parts(*INPUTS))
    values.update(fields)
    return SimpleNamespace(**values)


def runtime():
    from covey.app.task_card import StreamingCard
    return StreamingCard("t", "t", "answer", lambda **kw: kw, lambda _: None, lambda s: s, lambda *a: True,
                         lambda s, n: s[:n], lambda *a, **k: None, lambda *a, **k: (None, False),
                         update_interval=0, replacement_limit=0)


def outputs(took):
    from covey.cards.answer_card import output_parts
    return output_parts({"input_tokens": 410_000, "output_tokens": 9_800, "cache_read_tokens": 360_000,
                         "measured_output_tokens": 8_600, "generation_seconds": 150}, took)


def answer_text():
    return ("**结论**：登录页加好了，构建和测试都通过。\n\n"
            "- 新增 `/login` 路由和表单，邮箱格式和密码长度（至少 8 位）会校验\n"
            "- 登录成功跳首页，失败在表单上方给出提示\n"
            "- 补了 3 个组件测试")


def artifacts():
    from covey.cards.answer_card import ARTIFACTS_ID
    from covey.cards.elements import markdown_panel
    return markdown_panel(ARTIFACTS_ID, "产物 2 个 · login-page.diff 等",
                          "- [login-page.diff](https://example.invalid/a) · 6 KB\n"
                          "- [测试结果.txt](https://example.invalid/b) · 2 KB", quiet=True)


def butler_sketch():
    """The private chat with the butler on (docs/butler.md 放置表, ``area`` x ``butler``): the owner's sentence gets a
    Get reaction and the topic T7 hangs under it as its thread; once T7 is done its result comes back at the top
    level with whose it is and the sentence it answers."""
    from covey.cards.answer_card import ask_block, input_parts
    from covey.cards.relay_card import DONE, relay_card
    relay = relay_card(LABEL, DONE, text=answer_text(), inputs=input_parts(*INPUTS), outputs=outputs(242),
                       files=artifacts(), ask=ask_block(ASK, at="14:02", url="https://example.invalid/t7",
                                                        who=f"{LABEL} {TITLE}", doing="done"))[0]
    return [("user", ASK), ("reaction", "Get"), ("thread", f"{LABEL} {TITLE} · 4 条回复"), ("time", "14:06"),
            ("card", relay)]


def task_sketch():
    """The topic's task card two minutes in: status line, 输入/输出, 执行过程, 本轮请求 and the buttons."""
    from covey.cards.answer_card import running_figures
    from covey.cards.streaming import build_stream_card
    from covey.cards.task_figures import IO_BOX_ID, io_box, io_lines
    from covey.cards.task_status import ACTIVITY_ELEMENT_ID, STATUS_MORE_ID, status_elements
    from covey.cards.trace_updates import panel_json
    from covey.cards.trace_view import TraceView
    from covey.core.trace import task_trace

    job = demo_job()
    card = build_stream_card("", TITLE, runtime(), job=job, include_stop_action=True)
    began = FROZEN - 134
    trace = task_trace()
    steps = [("read", "src/router.tsx"), ("grep", "Form"),
             ("note", "先看路由和现有表单组件，登录页沿用它们的样式。"),
             ("write", "src/pages/Login.tsx"), ("bash", "npm test -- Login"),
             ("note", "页面和校验写好了，测试过了，再补一个失败提示。"),
             ("edit", "src/pages/Login.tsx")]
    at = began + 4
    for i, (name, detail) in enumerate(steps):
        at += 18
        if name == "note":
            trace.accept("progress_note", {"id": f"n{i}", "text": detail, "received_at": at})
            continue
        running = i == len(steps) - 1
        data = {"id": f"t{i}", "name": name, "status": "running" if running else "done", "started_at": at,
                "detail": detail}
        if not running:
            data.update(ended_at=at + 3, exit_code=0)
        trace.accept("tool", data)
    trace.accept("token_usage", {"output_tokens": 6100, "input_tokens": 236000, "cache_read_tokens": 198000,
                                 "measured_output_tokens": 5400, "generation_seconds": 95.0,
                                 "context_tokens": 61000, "context_window": 1000000})
    snapshot = trace.snapshot()
    snapshot["started_at"] = began
    status = status_elements("在做 · 已运行 2 分 14 秒 · 阶段：写登录页\n现在：编辑 src/pages/Login.tsx", True, io=True)
    replaced = {ACTIVITY_ELEMENT_ID: status[0], STATUS_MORE_ID: status[1],
                IO_BOX_ID: io_box(job, io_lines(running_figures(snapshot, began), "2分14秒"))}
    elements = []
    for element in card["body"]["elements"]:
        elements.append(replaced.get(element.get("element_id"), element))
        if element.get("element_id") == "answer":
            elements.append(panel_json(TraceView(snapshot).panel_model(True)))
    card["body"]["elements"] = [e for e in elements if not (e.get("element_id") == "answer" and not e.get("content"))]
    return [("user", ASK), ("card", card)]


def answer_sketch():
    """The answer card in the topic: the conclusion band, the answer, 产物 and 输入/输出."""
    from covey.cards.answer_card import input_parts, tail
    from covey.cards.review import answer_band, answer_card
    card = answer_card(answer_text(), f"{LABEL} 有结果 · 结论", tail(artifacts(), input_parts(*INPUTS), outputs(242)),
                       [answer_band("done", LABEL, TITLE, "登录页加好了，构建和测试都通过")])
    return [("card", card)]


def launch_sketch():
    """The launch card of a new topic, its settings unfolded."""
    from covey.cards.launch_config import launch_form_card
    card = launch_form_card("d" * 24, model="claude:opus", choices=CHOICES, effort="high", workspace="demo-app",
                            names=["demo-app", "docs-site"], run_mode="worktree", topic_tag=LABEL, expanded=True)
    return [("user", ASK), ("card", card)]


def redaction_sketch():
    """The 设置 card's 脱敏 tab: terms shown by their first character and length, the topics that use them."""
    from covey.cards.redaction_panel import redaction_card
    from covey.cards.settings_hub import tab_row
    token = "d" * 24
    facts = {"error": "", "config": {"default_on": False, "ignore_case": True, "restore": True, "upstream": ""},
             "rows": [{"key": "a" * 12, "label": "星··（4 字）", "anywhere": True, "restore": "还原", "hits": 14, "week": 9},
                      {"key": "b" * 12, "label": "金··（5 字）", "anywhere": False, "restore": "还原", "hits": 7, "week": 5},
                      {"key": "c" * 12, "label": "D··（8 字）", "anywhere": True, "restore": "不还原", "hits": 3, "week": 0}],
             "count": 3, "week": 14, "updated": "11-20 13:40",
             "topics": [{"number": 7, "title": TITLE, "turns": 2, "hits": 9, "last": "11-20 14:05"},
                        {"number": 5, "title": "修登录接口的报错", "turns": 3, "hits": 5, "last": "11-19 17:12"}],
             "turns": [{"time": "11-20 14:05", "number": 7, "model": "opus", "requests": 6, "replaced": 4},
                       {"time": "11-20 14:02", "number": 7, "model": "opus", "requests": 3, "replaced": 5}]}
    card = redaction_card(token, {"revision": 1, "hub": True}, facts,
                          top=[tab_row("redaction", token, ["topic", "main", "workspace", "redaction", "groups"])])
    return [("card", card)]


SKETCHES = {"01-butler": butler_sketch, "02-task-card": task_sketch, "03-answer-card": answer_sketch,
            "04-launch-card": launch_sketch, "05-settings-redaction": redaction_sketch}


def build():
    """Every sketch as name -> items, drawn at the demo's clock in UTC+8."""
    real_localtime = time.localtime
    zone = os.environ.get("TZ")
    os.environ["TZ"] = ZONE
    time.tzset()
    try:
        with mock.patch("time.time", return_value=FROZEN), \
                mock.patch("time.localtime", lambda seconds=None: real_localtime(FROZEN if seconds is None else seconds)):
            return {name: make() for name, make in SKETCHES.items()}
    finally:
        if zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = zone
        time.tzset()


# ---------------------------------------------------------------------------------------------------- HTML

# Feishu's colour names in light mode (docs/ui.md 2: the hex values are approximations for sketches).
INK = {"grey": "#8f959e", "blue": "#245bdb", "indigo": "#4b52e0", "green": "#2ea121", "red": "#f54a45",
       "orange": "#de7802", "violet": "#7a35f0", "purple": "#7a35f0", "wathet": "#1a8aae", "turquoise": "#0f9e8a",
       "yellow": "#b38600", "carmine": "#d83931", "neutral": "#1f2329", "default": "#1f2329", "black": "#1f2329"}
TAG = {"blue": ("#e1eaff", "#245bdb"), "neutral": ("#eff0f1", "#4e5969"), "grey": ("#eff0f1", "#4e5969"),
       "green": ("#d9f5d6", "#237b19"), "orange": ("#feead2", "#b25f00"), "red": ("#fde2e2", "#d83931"),
       "wathet": ("#d2f1fb", "#1a8aae"), "violet": ("#ece2fe", "#7a35f0"), "purple": ("#ece2fe", "#7a35f0"),
       "indigo": ("#e0e2fa", "#4b52e0"), "turquoise": ("#d5f6f2", "#0f9e8a"), "yellow": ("#faf1d1", "#8f6c00")}
GROUND = {"grey-50": "#f5f6f7", "grey-100": "#f2f3f5", "grey-200": "#eef0f2", "blue-50": "#f0f4ff",
          "indigo-50": "#f2f3fd", "violet-50": "#f6f1fe", "orange-50": "#fff5eb", "red-50": "#fef1f1",
          "green-50": "#f0fbef", "bg-white": "#ffffff", "white": "#ffffff", "default": "transparent",
          "grey": "#f5f6f7", "blue": "#f0f4ff", "transparent": "transparent"}
SIZE = {"normal_v2": 14, "normal": 14, "notation": 12, "heading": 16, "heading-0": 24, "heading-1": 20,
        "heading-2": 18, "heading-3": 16, "heading-4": 14, "small": 12, "xxxx-large": 30, "xx-large": 24}
# No colour emoji font in a headless Chromium on a server: the emoji the cards use as small drawn badges.
EMOJI = {"✅": '<i class="em ok">✓</i>', "⏳": '<i class="em wait">…</i>', "⏹": '<i class="em stop"></i>',
         "❌": '<i class="em no">×</i>', "⚠️": '<i class="em warn">!</i>', "📝": '<i class="em plain">✎</i>',
         "🔁": '<i class="em plain">⇄</i>', "↩️": '<i class="em plain">↶</i>', "⚙️": '<i class="em plain">⚙</i>',
         "📥": '<i class="em plain">↓</i>', "📦": '<i class="em plain">▣</i>', "🕒": '<i class="em wait">…</i>',
         "💡": '<i class="em plain">✦</i>', "🔒": '<i class="em plain">⚿</i>'}
ICON = {"robot_outlined": "◆", "start_outlined": "▶", "setting_outlined": "⚙", "down-small-ccm_outlined": "⌄",
        "right-small-ccm_outlined": "›", "yes_outlined": "✓", "folder_outlined": "▤", "chat_outlined": "✉",
        "group_outlined": "☰", "member_outlined": "●", "replace_outlined": "⇄", "undo_outlined": "↶",
        "safe_outlined": "◈", "waiting_outlined": "◔", "insert-chart_outlined": "▥", "member-add_outlined": "＋",
        "edit_outlined": "✎", "close_outlined": "×", "add_outlined": "＋", "delete-trash_outlined": "⌫",
        "lock_outlined": "⚿", "file-link-text_outlined": "▤", "info_outlined": "ⓘ", "search_outlined": "⌕"}


class Page:
    """One sketch's HTML, with the card colours (``config.style.color``) of the card being drawn."""

    def __init__(self):
        self.colors = {}

    def color(self, name, table=INK):
        if name in self.colors:
            return self.colors[name]
        return table.get(name) or (name if str(name).startswith(("#", "rgb")) else table.get("default", "inherit"))

    def ground(self, name):
        if name in self.colors:
            return self.colors[name]
        if name not in GROUND:
            raise ValueError(f"unknown background: {name!r}")
        return GROUND[name]

    def inline(self, text):
        text = re.sub(r"\*\*(.+?)\*\*", "\x01\\1\x02", text)
        parts = re.split(r"(<font color='[^']*'>|</font>|<text_tag color='[^']*'>|</text_tag>)", text)
        out, stack = [], []
        for part in parts:
            font = re.match(r"<font color='([^']*)'>", part)
            tag = re.match(r"<text_tag color='([^']*)'>", part)
            if font:
                out.append(f'<span style="color:{self.color(font.group(1))}">')
                stack.append("span")
            elif tag:
                bg, fg = TAG.get(tag.group(1), TAG["neutral"])
                out.append(f'<span class="tt" style="background:{bg};color:{fg}">')
                stack.append("span")
            elif part in ("</font>", "</text_tag>"):
                if stack:
                    out.append("</" + stack.pop() + ">")
            else:
                escaped = escape(part)
                escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
                escaped = escaped.replace("\x01", "<b>").replace("\x02", "</b>")
                escaped = re.sub(r"\[([^\]]+)\]\(([^)]*)\)", r"<a>\1</a>", escaped)
                out.append(escaped)
        out.extend("</" + tag + ">" for tag in reversed(stack))
        drawn = "".join(out)
        for emoji, badge in EMOJI.items():
            drawn = drawn.replace(emoji, badge)
        return drawn.replace("⁠", "")

    def markdown(self, text, size="normal_v2", align=""):
        lines = str(text).split("\n")
        blocks, i = [], 0
        while i < len(lines):
            line = lines[i]
            if line.startswith(">"):
                quoted = []
                while i < len(lines) and lines[i].startswith(">"):
                    quoted.append(self.inline(lines[i][1:].strip()))
                    i += 1
                blocks.append("<blockquote>" + "<br>".join(quoted) + "</blockquote>")
                continue
            if re.match(r"\s*[-*] ", line):
                items = []
                while i < len(lines) and re.match(r"\s*[-*] ", lines[i]):
                    items.append("<li>" + self.inline(re.sub(r"^\s*[-*] ", "", lines[i])) + "</li>")
                    i += 1
                blocks.append("<ul>" + "".join(items) + "</ul>")
                continue
            heading = re.match(r"(#{1,4}) (.*)", line)
            if heading:
                blocks.append(f'<div class="h{len(heading.group(1))}">{self.inline(heading.group(2))}</div>')
                i += 1
                continue
            if not line.strip():
                i += 1
                continue
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(>|\s*[-*] |#{1,4} )", lines[i]):
                para.append(self.inline(lines[i].rstrip()))
                i += 1
            blocks.append("<p>" + "<br>".join(para) + "</p>")
        style = f"font-size:{SIZE.get(size, 14)}px" + (f";text-align:{align}" if align else "")
        return f'<div class="md" style="{style}">{"".join(blocks)}</div>'

    def box_style(self, element):
        styles = []
        if element.get("padding"):
            styles.append("padding:" + pixels(element["padding"]))
        if element.get("margin"):
            styles.append("margin:" + pixels(element["margin"]))
        ground = element.get("background_style") or element.get("background_color")
        if ground:
            styles.append("background:" + self.ground(ground))
        border = element.get("border")
        if isinstance(border, dict):
            styles.append(f"border:1px solid {self.line(border.get('color'))};"
                          f"border-radius:{border.get('corner_radius', '0px')}")
        if element.get("has_border"):
            styles.append(f"border:1px solid {self.line(element.get('border_color'))}")
        if element.get("corner_radius"):
            styles.append("border-radius:" + element["corner_radius"])
        return ";".join(styles)

    def line(self, name):
        if name in self.colors:
            return self.colors[name]
        return "#dee0e3" if name in (None, "grey", "default", "grey-200", "grey-300") else self.color(name)

    def render(self, element):
        tag = element.get("tag")
        if tag == "markdown":
            return self.markdown(element.get("content") or "", element.get("text_size", "normal_v2"),
                                 element.get("text_align", ""))
        if tag in ("plain_text", "div"):
            text = element.get("content") if tag == "plain_text" else element.get("text", {}).get("content", "")
            return f'<div class="md" style="font-size:{SIZE.get(element.get("text_size"), 14)}px">{escape(text or "")}</div>'
        if tag == "hr":
            return '<div class="hr"></div>'
        if tag == "button":
            return self.button(element)
        if tag == "standard_icon":
            return f'<i class="ic" style="color:{self.color(element.get("color", "grey"))}">{ICON.get(element["token"], "•")}</i>'
        if tag == "column_set":
            return self.columns(element)
        if tag == "interactive_container":
            gap = element.get("vertical_spacing", "8px")
            inner = "".join(self.render(e) for e in element.get("elements", []))
            return f'<div class="col" style="gap:{gap};{self.box_style(element)}">{inner}</div>'
        if tag == "collapsible_panel":
            return self.panel(element)
        if tag == "form":
            gap = element.get("vertical_spacing", "8px")
            return f'<div class="col" style="gap:{gap}">{"".join(self.render(e) for e in element.get("elements", []))}</div>'
        if tag == "input":
            hint = element.get("placeholder", {}).get("content", "")
            tall = element.get("input_type") == "multiline_text" or element.get("rows")
            return f'<div class="inp" style="min-height:{64 if tall else 32}px">{escape(hint)}</div>'
        if tag == "select_static":
            hint = element.get("placeholder", {}).get("content", "")
            return f'<div class="inp">{escape(hint)}<i class="chev">⌄</i></div>'
        if tag == "text_tag":
            bg, fg = TAG.get(element.get("color"), TAG["neutral"])
            return f'<span class="tt" style="background:{bg};color:{fg}">{escape(element["text"]["content"])}</span>'
        raise ValueError(f"unsupported tag: {tag!r}")

    def button(self, element):
        kind = element.get("type", "default")
        label = self.inline(element.get("text", {}).get("content", ""))
        icon = element.get("icon")
        mark = f'<i class="ic">{ICON.get(icon.get("token"), "•")}</i>' if isinstance(icon, dict) else ""
        wide = " fill" if element.get("width") == "fill" else ""
        return f'<span class="btn {kind} {element.get("size", "medium")}{wide}">{mark}{label}</span>'

    def columns(self, element):
        drawn = []
        for column in element.get("columns", []):
            width = column.get("width", "auto")
            flex = ("0 0 auto" if width == "auto" else f"0 0 {width}" if str(width).endswith("px")
                    else f"{column.get('weight', 1)} 1 0")
            align = {"center": "center", "bottom": "flex-end"}.get(column.get("vertical_align"), "flex-start")
            inner = "".join(self.render(e) for e in column.get("elements", []))
            drawn.append(f'<div class="col" style="flex:{flex};min-width:0;gap:{column.get("vertical_spacing", "4px")};'
                         f'justify-content:{align};{self.box_style(column)}">{inner}</div>')
        align = {"center": "center", "bottom": "flex-end"}.get(element.get("vertical_align"), "flex-start")
        gap = element.get("horizontal_spacing", "8px")
        gap = {"small": "4px", "medium": "8px", "large": "12px", "default": "8px"}.get(gap, gap)
        return (f'<div class="row" style="gap:{gap};align-items:{align};{self.box_style(element)}">'
                f'{"".join(drawn)}</div>')

    def panel(self, element):
        head = element.get("header", {})
        title = head.get("title", {})
        if title.get("tag") == "markdown":
            drawn = self.markdown(title.get("content", ""), title.get("text_size", "normal_v2"))
        else:
            drawn = (f'<div class="md" style="font-size:{SIZE.get(title.get("text_size"), 14)}px;'
                     f'color:{self.color(title.get("text_color", "default"))}">{escape(title.get("content", ""))}</div>')
        opened = bool(element.get("expanded"))
        chevron = f'<i class="chev">{"⌃" if opened else "⌄"}</i>'
        follows = head.get("icon_position") == "follow_text"
        line = (f'<div class="phead{" follow" if follows else ""}" style="padding:{pixels(head.get("padding", "6px 0px 6px 0px"))}">'
                f'{drawn}{chevron}</div>')
        body = ""
        if opened:
            inner = "".join(self.render(e) for e in element.get("elements", []))
            body = f'<div class="col" style="gap:{element.get("vertical_spacing", "8px")}">{inner}</div>'
        outer = dict(element)
        if element.get("background_color"):
            outer["background_style"] = element["background_color"]
        gap = element.get("vertical_spacing", "8px") if opened else "0px"
        return f'<div class="col" style="gap:{gap};{self.box_style(outer)}">{line}{body}</div>'

    def card(self, card):
        self.colors = {name: value.get("light_mode") for name, value in
                       card.get("config", {}).get("style", {}).get("color", {}).items()}
        body = card.get("body", {})
        head = card.get("header")
        top = ""
        if head:
            icon = self.render(head["icon"]) if head.get("icon") else ""
            tags = "".join(self.render(t) for t in head.get("text_tag_list", []))
            sub = (f'<div class="sub">{escape(head["subtitle"]["content"])}</div>' if head.get("subtitle") else "")
            top = (f'<div class="chead" style="padding:{pixels(head.get("padding", "12px 14px 0px 14px"))}">'
                   f'<div class="row" style="gap:6px;align-items:center">{icon}<span class="t">'
                   f'{escape(head["title"]["content"])}</span>{tags}</div>{sub}</div>')
        padding = pixels(body.get("padding", "12px 14px 12px 14px"))
        inner = "".join(self.render(e) for e in body.get("elements", []))
        return (f'<div class="card">{top}<div class="cbody" style="gap:{body.get("vertical_spacing", "8px")};'
                f'padding:{padding}">{inner}</div></div>')

    def page(self, items, clock="14:02"):
        """``items``: ('user', text), ('card', card JSON), ('time', a clock line), and under the owner's message
        ('reaction', its name) and ('thread', what its thread holds)."""
        out = [f'<div class="page"><div class="time">{clock}</div>']
        for kind, value in items:
            if kind == "user":
                out.append(f'<div class="user">{escape(value)}</div>')
            elif kind == "time":
                out.append(f'<div class="time">{escape(value)}</div>')
            elif kind == "reaction":
                out.append(f'<div class="under"><span class="react"><b>{escape(value)}</b> Covey</span></div>')
            elif kind == "thread":
                out.append(f'<div class="under"><span class="thread">话题　{escape(value)} ›</span></div>')
            else:
                out.append('<div class="msg"><div class="av">C</div><div><div class="who">Covey</div>'
                           + self.card(value) + "</div></div>")
        out.append(f'<div class="cap">{CAPTION}</div></div>')
        return "<!doctype html><meta charset=utf-8><style>" + CSS + "</style>" + "".join(out)


def escape(text):
    return html.escape(str(text), quote=False).replace("&amp;#", "&#")


def pixels(padding):
    return " ".join(p if p.endswith("px") else p + "px" for p in str(padding).split()) if padding else "0"


CSS = """
*{box-sizing:border-box}body{margin:0;background:#f2f3f5;font-family:'Noto Sans CJK SC','Noto Sans CJK',sans-serif;
color:#1f2329;-webkit-font-smoothing:antialiased}
.page{width:620px;padding:22px 20px 14px}
.time{text-align:center;color:#8f959e;font-size:12px;margin:2px 0 14px}
.msg{display:flex;gap:10px;margin-bottom:14px;align-items:flex-start}
.av{flex:none;width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#4b52e0,#7c83ff);color:#fff;
font-weight:700;font-size:19px;display:flex;align-items:center;justify-content:center}
.who{font-size:12px;color:#646a73;margin:0 0 4px 1px}
.card{width:500px;background:#fff;border:1px solid #dee0e3;border-radius:10px;overflow:hidden}
.chead .t{font-weight:600;font-size:16px}.chead .sub{color:#8f959e;font-size:12px;margin-top:2px}
.cbody{display:flex;flex-direction:column}
.user{margin:0 0 14px auto;width:fit-content;max-width:420px;background:#d6e4ff;border-radius:10px;padding:9px 13px;
font-size:14px;line-height:1.5}
.row{display:flex;flex-wrap:nowrap}.col{display:flex;flex-direction:column}
.md{line-height:1.55;word-break:break-word}.md p{margin:0 0 4px}.md p:last-child{margin:0}
.md blockquote{margin:0;padding-left:8px;border-left:2px solid #dee0e3;color:#646a73}
.md ul{margin:2px 0;padding-left:20px}
.md code{background:#eff0f1;border-radius:4px;padding:0 4px;font-family:'DejaVu Sans Mono',monospace;font-size:.9em}
.md .h1{font-size:1.35em;font-weight:700;margin:2px 0 6px}.md .h2{font-size:1.25em;font-weight:700;margin:0 0 2px}
.md .h3,.md .h4{font-weight:700;margin:2px 0 2px}
.md a{color:#245bdb}.tt{display:inline-block;font-size:12px;line-height:18px;padding:0 6px;border-radius:4px;
vertical-align:1px;white-space:nowrap}
.hr{height:1px;background:#eef0f2}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:4px;height:32px;padding:0 14px;border-radius:6px;
font-size:14px;border:1px solid #d0d3d6;background:#fff;white-space:nowrap}
.btn.fill{display:flex;width:100%}.btn.small{height:28px;padding:0 10px;font-size:13px}
.btn.primary{border-color:#3370ff;color:#245bdb}.btn.primary_filled{background:#3370ff;border-color:#3370ff;color:#fff}
.btn.danger{border-color:#f54a45;color:#f54a45}.btn.danger_filled{background:#f54a45;border-color:#f54a45;color:#fff}
.btn.text,.btn.primary_text,.btn.danger_text,.btn.laser{border-color:transparent;background:transparent}
.btn.primary_text{color:#245bdb}.btn.danger_text{color:#f54a45}
.panel{display:block}.phead{display:flex;justify-content:space-between;align-items:center;gap:8px}
.phead.follow{justify-content:flex-start}
.chev{font-style:normal;color:#8f959e;font-size:14px}.ic{font-style:normal;font-size:14px}
.inp{display:flex;justify-content:space-between;align-items:flex-start;border:1px solid #dee0e3;border-radius:6px;
padding:6px 10px;color:#8f959e;font-size:14px;background:#fff}
.em{font-style:normal;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;
border-radius:50%;font-size:10px;color:#fff;vertical-align:-2px;margin-right:2px;font-weight:700}
.em.ok{background:#34c724}.em.wait{background:#ff8800}.em.no{background:#f54a45}.em.warn{background:#ff8800}
.em.stop{background:#f54a45;border-radius:3px;width:11px;height:11px;margin:0 3px 0 0}
.em.plain{background:none;color:inherit;font-size:14px;width:auto;height:auto}
.under{display:flex;justify-content:flex-end;margin:-8px 0 14px}
.react{font-size:12px;color:#646a73;background:#e6e8eb;border-radius:12px;padding:2px 9px}
.react b{color:#2ea121;font-weight:700;margin-right:3px}
.thread{font-size:13px;color:#245bdb;background:#fff;border:1px solid #dee0e3;border-radius:8px;padding:5px 10px}
.cap{color:#8f959e;font-size:11px;text-align:center;margin-top:6px}
"""


# ---------------------------------------------------------------------------------------------------- PNG


def shoot(pages, browser=""):
    """Each HTML page's ``.page`` as a PNG beside it, at twice the CSS pixels."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        launch = {"args": ["--no-sandbox"]}
        if browser:
            launch["executable_path"] = browser
        chromium = playwright.chromium.launch(**launch)
        try:
            for page_file in pages:
                tab = chromium.new_page(viewport={"width": 620, "height": 400}, device_scale_factor=2)
                tab.goto(page_file.resolve().as_uri())
                tab.locator(".page").screenshot(path=str(page_file.with_suffix(".png")))
                tab.close()
        finally:
            chromium.close()


def install(out):
    """The PNGs in ``out`` into the docs, as 128-colour palette PNGs: a third of the size, no visible loss on flat
    cards (needs Pillow)."""
    from PIL import Image
    for name in SKETCHES:
        shot = out / f"{name}.png"
        with Image.open(shot) as image:
            image.convert("RGB").quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
                SHOTS_DIR / shot.name, optimize=True)
        print(SHOTS_DIR / shot.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="where the HTML pages and PNGs go")
    parser.add_argument("--png", action="store_true", help="shoot each page as a PNG (needs Playwright)")
    parser.add_argument("--browser", default="", help="a Chromium to shoot with instead of Playwright's own")
    parser.add_argument("--install", action="store_true",
                        help=f"put the PNGs of --out into {SHOTS_DIR.relative_to(REPO)} (needs Pillow), nothing else")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.install:
        install(out)
        return 0
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for name, items in build().items():
        page = out / f"{name}.html"
        page.write_text(Page().page(items), encoding="utf-8")
        pages.append(page)
        print(page)
    if args.png:
        shoot(pages, args.browser)
        for page in pages:
            print(page.with_suffix(".png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

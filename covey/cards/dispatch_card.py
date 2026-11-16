"""The 派发卡 (app/dispatch_cards.py; docs/main-thread.md 派发卡): the card the main thread's dispatch of a job to a
topic leaves at the chat's top level, quoting the owner's message. It is drawn whole from ``Dispatch`` (what the job
records say), again each time that changes, and once more as a single line when the conclusion went out.

Top to bottom, element IDs (CardKit takes at most 20 characters, rule 9) in the order drawn:

- ``answer_ask``: the grey "你问 · 12:31 ›" block (cards/answer_card.py ``ask_block``) with the owner's words, opening his
  message at its place in the chat. The card is a message of its own at the chat's top level, not a reply to his
  message: Feishu puts a reply to a message that has a thread already (the topic the work was handed to hangs under
  that message) into the thread whatever ``reply_in_thread`` says, so the block says whose question it is instead;
- the header: the owner's request, its first ``HEAD_CHARS`` characters, and the state as a tag (在做, 等你, 做完, 失败, 已停);
- ``dp_meta``: one grey line, "T453 · 12:41 起 · 用时 2分05秒";
- ``dp_thread``: the fold 话题 (open while the job works or waits on the owner, closed once it ended), its title line the
  topic's T number, title and workspace; inside it ``dp_body`` (the stage or step the job is at) and ``dp_notes``;
- ``dp_io``: the grey 输入/输出 box (cards/answer_card.py ``box``);
- ``dp_btns``: 补充 (``dp_steer``), 停止 (``dp_stop``) and › 打开话题串 (``dp_open``), those that can be used now.

No input field: a card that is redrawn must not hold one (docs/cards.md 卡片的客户端限制); 补充 sends a card of its own.
The single line a settled card becomes: ``dp_line``, one grey block that opens the conclusion (``settled_card``)."""
from __future__ import annotations

from dataclasses import dataclass

from covey.cards import answer_card
from covey.cards.answer_card import literal_text
from covey.cards.elements import (button_row, card, collapsible, grey, md, note, panel_header, styled_panel,
                                  topic_button)

HEAD_CHARS = 30
THREAD_TITLE = 40  # Of the topic's title on the fold's title line.
STEER_KIND, STOP_KIND = "bridge_dispatch_steer", "bridge_dispatch_stop"
RUNNING, WAITING, DONE, FAILED, STOPPED = "在做", "等你", "做完", "失败", "已停"
# status -> (tag colour, header icon: one the other cards use, '' for none, whether the 话题 fold is open).
LOOKS = {RUNNING: ("blue", "waiting_outlined", True), WAITING: ("orange", "chat_outlined", True),
         DONE: ("green", "yes_outlined", False), FAILED: ("red", "", False), STOPPED: ("neutral", "", False)}
LINE_MARKS = {DONE: "✅", FAILED: "⚠️", STOPPED: "⏹"}


@dataclass(frozen=True)
class Dispatch:
    """What one card says. ``job_id`` the topic's job the buttons act on ('' while it has none); ``url`` opens the
    topic's thread ('' while it is not known)."""
    token: str
    topic_id: str
    request: str
    status: str
    label: str = ""
    title: str = ""
    workspace: str = ""
    started: str = ""
    took: str = ""
    body: str = ""
    notes: tuple = ()
    inputs: tuple = ()
    outputs: tuple = ()
    job_id: str = ""
    url: str = ""
    ask_at: str = ""
    ask_url: str = ""


def head_words(request) -> str:
    words = " ".join(str(request or "").split()) or "派出去的事"
    return words if len(words) <= HEAD_CHARS else words[:HEAD_CHARS - 1] + "…"


def header(dispatch):
    color, icon, _ = LOOKS[dispatch.status]
    head = panel_header(icon or "waiting_outlined", head_words(dispatch.request), "", [(dispatch.status, color)], color=color)
    if not icon:
        head.pop("icon")
    return head


def meta_line(dispatch) -> str:
    parts = [dispatch.label, f"{dispatch.started} 起" if dispatch.started else "",
             f"用时 {dispatch.took}" if dispatch.took else ""]
    return " · ".join(part for part in parts if part)


def thread_title(dispatch) -> str:
    title = " ".join(str(dispatch.title or "").split())
    title = title if len(title) <= THREAD_TITLE else title[:THREAD_TITLE - 1] + "…"
    return "话题 · " + " · ".join(part for part in (dispatch.label, title, dispatch.workspace) if part)


def buttons(dispatch) -> list:
    """The buttons that can be used now: 补充 and 停止 while the job works (they act on its ``job_id``, which also
    routes a click to its task process), › 打开话题串 whenever the topic's thread has a link."""
    row = []
    if dispatch.status == RUNNING and dispatch.job_id:
        value = {"topic_id": dispatch.topic_id, "job_id": str(dispatch.job_id)}
        row.append(topic_button("📝 补充", element_id="dp_steer", value={"kind": STEER_KIND, **value}))
        row.append(topic_button("⏹ 停止", element_id="dp_stop", tone="danger", value={"kind": STOP_KIND, **value}))
    if dispatch.url:
        row.append(topic_button("› 打开话题串", element_id="dp_open",
                                behaviors=[{"type": "open_url", "default_url": dispatch.url}]))
    return [button_row(row, "dp_btns")] if row else []


def summary(dispatch) -> str:
    return f"{dispatch.status} · {head_words(dispatch.request)}"


def dispatch_card(dispatch):
    """The live card: ``dispatch`` drawn whole."""
    _, _, opened = LOOKS[dispatch.status]
    inside = [md(literal_text(dispatch.body) if dispatch.body else grey("—"), element_id="dp_body", size="normal_v2")]
    if dispatch.notes:
        inside.append(md("\n".join(grey(literal_text(line)) for line in dispatch.notes), element_id="dp_notes",
                         size="notation"))
    elements = [answer_card.ask_block(dispatch.request, at=dispatch.ask_at, url=dispatch.ask_url),
                md(grey(literal_text(meta_line(dispatch))), element_id="dp_meta", size="notation"),
                collapsible("dp_thread", thread_title(dispatch), inside, expanded=opened, size="normal_v2"),
                answer_card.box("dp_io", answer_card.input_lines(dispatch.inputs), list(dispatch.outputs)),
                *buttons(dispatch)]
    return styled_panel(header(dispatch), [element for element in elements if element], summary(dispatch))


def settled_card(status, request, *, url="", reason="", label=""):
    """What a card becomes once its conclusion went out (docs/main-thread.md 派发卡 · 结论沉底): one grey line,
    "✅ 做完 · 结论在下方 ›" opening the conclusion at ``url``; for 失败 and 已停 the line says why (``reason``) and
    opens nothing. No fold, no buttons."""
    mark = LINE_MARKS.get(status, "")
    if status == DONE:
        words = f"{mark} {status} · 结论在下方" + ("　›" if url else "")
    else:
        words = f"{mark} {status}" + (f" · {reason}" if reason else "")
    text = " · ".join(part for part in (label, head_words(request)) if part)
    block = {"tag": "interactive_container", "element_id": "dp_line", "width": "fill", "padding": "6px 10px 6px 10px",
             "corner_radius": "6px", "background_style": "grey-50", "vertical_spacing": "2px",
             "elements": [md(f"**{literal_text(words)}**", size="normal_v2"), note(literal_text(text))]}
    if url and status == DONE:
        block["behaviors"] = [{"type": "open_url", "default_url": url}]
    built = card([block], f"{words} · {text}", update_multi=True)
    built["config"]["width_mode"] = "compact"
    return built


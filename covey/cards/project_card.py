"""The 专项卡 (app/project_cards.py; docs/main-thread.md 专项卡), when COVEY_PROJECTS=1: the one card of a project at the chat's
top level, a message of its own (never a reply: Feishu puts a reply to a message that has a thread into the thread, see
cards/dispatch_card.py). It is drawn whole from ``Project`` (what the project's record and its topics' job records say),
again each time that changes, and once more as a single line when the project is done.

Top to bottom, element IDs (CardKit takes at most 20 characters, rule 9) in the order drawn:

- ``answer_ask`` (only for a project the owner's own message started): the grey "你问" block;
- the header: the project's name, its state as a tag (在做, 等你, 停住, 做完) and its direction (a grey tag; P2-8, drawn only
  when the record has one);
- ``pj_need``: 需要你 and what waits, with › 打开问题卡 when the question card has a link;
- ``pj_meta``: one grey line, threads and how they stand, 用时, token;
- ``pj_lead``: the lead as it is now and the last line it noted;
- ``pt_<topic>_<s>``: one fold a thread, open while it works or waits on the owner and closed once it ended; ``s`` is the
  state's letter, so a thread that moves from one to the other is a new element and the client draws the fold as
  that state says (a changed ``expanded`` on the same id is kept as the owner left it); at most ``MAX_THREADS``, the rest
  one grey line ``pj_more``. Inside, ``pb_<topic>_<s>``, what it is doing and the link that opens its thread;
- ``pj_triage``, ``pj_mem``, ``pj_lib``: the 分流记录, 专项记忆 and 资料库 folds (closed), drawn only with rows; each row of
  the last two has a 删 button;
- ``pj_io``: the grey 输入/输出 box (cards/answer_card.py ``box``);
- ``pj_btns``: 补充 (``pj_steer``), 停止专项 (``pj_stop``), 不要专项 (``pj_down``).

No input field: a card that is redrawn must not hold one (docs/cards.md 卡片的客户端限制); 补充 sends a card of its own.
Two more cards of one line: ``settled_card`` (the project is done: ``pj_line``, linking to the conclusion) and
``moved_card`` (a card that was replaced by a new one because CardKit stops accepting writes to a card after 14 days)."""
from __future__ import annotations

from dataclasses import dataclass

from covey.cards import answer_card
from covey.cards.answer_card import literal_text
from covey.cards.elements import (button_row, card, collapsible, grey, item, md, note, panel_header, styled_panel,
                                  text_button, topic_button)

HEAD_CHARS = 30
THREAD_TITLE = 36  # Of a topic's title on its fold's title line.
MAX_THREADS = 8  # Folds drawn; the older ones are one line.
MAX_ROWS = 10  # Rows of the 分流记录, 专项记忆 and 资料库 folds.
ROW_CHARS = 90
STEER_KIND, STOP_KIND, DOWN_KIND = "bridge_project_steer", "bridge_project_stop", "bridge_project_downgrade"
MEM_DEL_KIND, LIB_DEL_KIND = "bridge_project_mem_del", "bridge_project_lib_del"
KINDS = frozenset({STEER_KIND, STOP_KIND, DOWN_KIND, MEM_DEL_KIND, LIB_DEL_KIND})
RUNNING, WAITING, PAUSED, DONE = "在做", "等你", "停住", "做完"
# state -> (tag colour, header icon: one the other cards use, '' for none).
LOOKS = {RUNNING: ("blue", "waiting_outlined"), WAITING: ("orange", "chat_outlined"), PAUSED: ("neutral", ""),
         DONE: ("green", "yes_outlined")}
LETTER = {RUNNING: "r", WAITING: "w", PAUSED: "p", DONE: "d"}
OPEN = frozenset({RUNNING, WAITING})  # The folds that are open.


@dataclass(frozen=True)
class Thread:
    """One thread topic of the project. ``status`` is one of 在做, 等你, 做完 (a thread that failed or was stopped is 做完
    too, its ``body`` says what happened); ``url`` opens its thread ('' while it is not known)."""
    topic_id: str
    status: str
    label: str = ""
    title: str = ""
    body: str = ""
    took: str = ""
    url: str = ""


@dataclass(frozen=True)
class Project:
    """What one card says. ``rows`` of the three folds: ``triage`` lines, ``memory`` (id, text), ``library`` (name,).
    ``lead_topic`` and ``project_id`` go into the buttons' values."""
    project_id: str
    lead_topic: str
    name: str
    status: str
    direction: str = ""
    need: str = ""
    need_url: str = ""
    threads: tuple = ()
    elapsed: str = ""
    tokens: str = ""
    lead_state: str = ""
    lead_note: str = ""
    triage: tuple = ()
    memory: tuple = ()
    library: tuple = ()
    inputs: tuple = ()
    outputs: tuple = ()
    ask_text: str = ""
    ask_at: str = ""
    ask_url: str = ""


def clipped(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def header(project):
    color, icon = LOOKS[project.status]
    tags = [{"tag": "text_tag", "text": {"tag": "plain_text", "content": project.status}, "color": color}]
    if project.direction:
        tags.append({"tag": "text_tag", "text": {"tag": "plain_text", "content": clipped(project.direction, 12)},
                     "color": "neutral"})
    head = panel_header(icon or "waiting_outlined", clipped(project.name, HEAD_CHARS) or "专项", "", [(project.status, color)],
                        color=color)
    head["text_tag_list"] = tags
    if not icon:
        head.pop("icon")
    return head


def short(topic_id) -> str:
    return str(topic_id or "")[:6]


def count_line(project) -> str:
    kinds = {}
    for thread in project.threads:
        kinds[thread.status] = kinds.get(thread.status, 0) + 1
    parts = " · ".join(f"{word} {kinds[word]}" for word in (RUNNING, WAITING, DONE) if kinds.get(word))
    return f"线程 {len(project.threads)}" + (f"（{parts}）" if parts else "")


def meta_line(project) -> str:
    parts = [count_line(project), f"用时 {project.elapsed}" if project.elapsed else "",
             f"token {project.tokens}" if project.tokens else ""]
    return " · ".join(part for part in parts if part)


def lead_line(project) -> str:
    text = f"**负责人** · {literal_text(project.lead_state or '待命')}"
    if project.lead_note:
        text += "\n" + grey(literal_text(clipped(project.lead_note, 120)))
    return text


def need_element(project):
    if not project.need:
        return None
    text = f"**需要你** · {literal_text(clipped(project.need, 140))}"
    if project.need_url:
        text += f"　[打开问题卡 ›]({project.need_url})"
    return md(text, element_id="pj_need", size="normal_v2")


def ordered(threads) -> tuple:
    """The threads as the card lists them: the ones that wait for the owner, then the ones at work, then the ones that
    ended; each in the order they came in (the record's)."""
    rank = {WAITING: 0, RUNNING: 1, DONE: 2}
    return tuple(sorted(threads, key=lambda thread: rank.get(thread.status, 2)))


def thread_fold(thread):
    letter = LETTER.get(thread.status, "d")
    key = f"{short(thread.topic_id)}_{letter}"
    title = " · ".join(part for part in (thread.label, clipped(thread.title, THREAD_TITLE), thread.status) if part)
    body = [md(literal_text(clipped(thread.body, 200)) if thread.body else grey("—"), element_id=f"pb_{key}", size="normal_v2")]
    tail = " · ".join(part for part in (f"用时 {thread.took}" if thread.took else "",
                                        f"[打开话题串 ›]({thread.url})" if thread.url else "") if part)
    if tail:
        body.append(md(grey(tail) if not thread.url else tail, element_id=f"pq_{key}", size="notation"))
    return collapsible(f"pt_{key}", title, body, expanded=thread.status in OPEN, size="normal_v2")


def thread_elements(project) -> list:
    shown = ordered(project.threads)
    elements = [thread_fold(thread) for thread in shown[:MAX_THREADS]]
    if len(shown) > MAX_THREADS:
        elements.append(note(f"更早 {len(shown) - MAX_THREADS} 条已收起，在各自的话题里看"))
        elements[-1]["element_id"] = "pj_more"
    return elements


def row_fold(element_id, title, rows) -> dict | None:
    rows = list(rows)
    if not rows:
        return None
    inside = rows[:MAX_ROWS]
    if len(rows) > MAX_ROWS:
        inside.append(note(f"另有 {len(rows) - MAX_ROWS} 条"))
    return collapsible(element_id, f"{title} · {len(rows)}", inside, expanded=False, size="normal_v2")


def triage_rows(project) -> list:
    return [md(grey(literal_text(clipped(line, ROW_CHARS))), size="notation") for line in project.triage]


def memory_rows(project) -> list:
    return [item(md(literal_text(clipped(text, ROW_CHARS)), size="normal_v2"),
                 text_button("删", {"kind": MEM_DEL_KIND, "project_id": project.project_id, "topic_id": project.lead_topic,
                                    "entry": str(entry_id)}, tone="danger"))
            for entry_id, text in project.memory]


def library_rows(project) -> list:
    return [item(md(literal_text(clipped(name, ROW_CHARS)), size="normal_v2"),
                 text_button("删", {"kind": LIB_DEL_KIND, "project_id": project.project_id, "topic_id": project.lead_topic,
                                    "name": str(name)}, tone="danger"))
            for (name, *_) in project.library]


def buttons(project) -> list:
    """补充 and 停止专项 while the project is on, 不要专项 until it is done: the lead's topic id and the project's id are
    what a click carries (the click routes to the lead's topic, which the card's message belongs to)."""
    if project.status == DONE:
        return []
    value = {"project_id": project.project_id, "topic_id": project.lead_topic}
    row = [topic_button("📝 补充", element_id="pj_steer", value={"kind": STEER_KIND, **value})]
    if project.status != PAUSED:
        row.append(topic_button("⏹ 停止专项", element_id="pj_stop", tone="danger", value={"kind": STOP_KIND, **value}))
    row.append(topic_button("不要专项", element_id="pj_down", value={"kind": DOWN_KIND, **value}))
    return [button_row(row, "pj_btns")]


def summary(project) -> str:
    return f"{project.status} · {clipped(project.name, HEAD_CHARS)}"


def project_card(project):
    """The live card: ``project`` drawn whole."""
    ask = [answer_card.ask_block(project.ask_text, at=project.ask_at, url=project.ask_url)] if project.ask_text else []
    elements = [*ask, need_element(project)]
    elements.append(md(grey(literal_text(meta_line(project))), element_id="pj_meta", size="notation"))
    elements.append(md(lead_line(project), element_id="pj_lead", size="normal_v2"))
    elements += thread_elements(project)
    elements += [row_fold("pj_triage", "分流记录", triage_rows(project)),
                 row_fold("pj_mem", "专项记忆", memory_rows(project)),
                 row_fold("pj_lib", "资料库", library_rows(project)),
                 answer_card.box("pj_io", answer_card.input_lines(project.inputs), list(project.outputs)),
                 *buttons(project)]
    return styled_panel(header(project), [element for element in elements if element], summary(project))


def line_block(words, text, url=""):
    block = {"tag": "interactive_container", "element_id": "pj_line", "width": "fill", "padding": "6px 10px 6px 10px",
             "corner_radius": "6px", "background_style": "grey-50", "vertical_spacing": "2px",
             "elements": [md(f"**{literal_text(words)}**", size="normal_v2"), note(literal_text(text))]}
    if url:
        block["behaviors"] = [{"type": "open_url", "default_url": url}]
    built = card([block], f"{words} · {text}", update_multi=True)
    built["config"]["width_mode"] = "compact"
    return built


def settled_card(name, *, url="", reason="", direction=""):
    """What the card of a project that is done becomes: one grey line, "✅ 做完 · 结论在下方 ›" opening the conclusion at
    ``url``. ``reason``: said instead for a project that ended some other way (stopped, no longer a project); no link."""
    words = f"✅ {DONE} · 结论在下方" + ("　›" if url else "") if not reason else f"⏹ {reason}"
    text = " · ".join(part for part in (direction, clipped(name, HEAD_CHARS) or "专项") if part)
    return line_block(words, text, url if not reason else "")


def moved_card(name, url="", *, direction=""):
    """The old card of a project whose card was sent again (the 14 days): "已移到新卡 ›" opening the new one."""
    text = " · ".join(part for part in (direction, clipped(name, HEAD_CHARS) or "专项") if part)
    return line_block("已移到新卡" + ("　›" if url else ""), text, url)

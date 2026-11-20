"""The 专项卡 (app/project_cards.py; docs/butler.md 专项卡), when COVEY_PROJECTS=1: the one card of a project at the chat's
top level, a message of its own (never a reply: Feishu puts a reply to a message that has a thread into the thread, see
cards/dispatch_card.py). It is drawn whole from ``Project`` (what the project's record and its topics' job records say),
again each time that changes, and once more as a single line when the project is done.

Top to bottom, element IDs (CardKit takes at most 20 characters, rule 9) in the order drawn:

- ``answer_ask`` (only for a project the owner's own message started): the grey "你问" block;
- the header: the project's name, its state as a tag (在做, 等你, 停住, 做完);
- ``pj_need``: 需要你 and what waits, with › 打开问题卡 when the question card has a link;
- ``pj_meta``: one grey line, member topics and how they stand, 用时, token;
- ``pj_lead``: the lead as it is now and the last line it noted;
- ``pt_<topic>_<s>``: one fold a member, open while it works or waits on the owner and closed once it ended; ``s`` is the
  state's letter, so a member that moves from one to the other is a new element and the client draws the fold as
  that state says (a changed ``expanded`` on the same id is kept as the owner left it); at most ``MAX_FOLDS``, the rest
  one grey line ``pj_more``. Inside, ``pb_<topic>_<s>``, what it is doing and the link that opens its thread;
- ``pj_triage``, ``pj_mem``, ``pj_lib``: the 分流记录, 专项记忆 and 资料库 folds (closed), drawn only with rows; each row of
  the last two has a 删 button, and a row of the first that the lead answered for the owner a 不对 button (``triage_rows``);
- ``pj_io``: the grey 输入/输出 box (cards/answer_card.py ``box``);
- ``pj_btns``: 补充 (``pj_steer``), 停止专项 (``pj_stop``), 不要专项 (``pj_down``).

Two cards of the project's own more, with COVEY_PROJECT_ASK=1 (``ask_card``, ``asked_card``): the one a guard's pause sends the owner,
a message of its own where the project's card is (``pa_why`` the reason in a sentence, ``pa_note``, ``pa_btns``: 继续 ``pa_go``,
收尾 ``pa_wrap``, 停 ``pa_stop``), and what it becomes once the owner chose (one line, ``pj_line``, saying what he chose).

No input field: a card that is redrawn must not hold one (docs/cards.md 卡片的客户端限制); 补充 sends a card of its own.
Two more cards of one line: ``settled_card`` (the project is done: ``pj_line``, linking to the conclusion) and
``idle_card`` (a project with nothing to do for COVEY_PROJECT_IDLE_DAYS days, folded away; the whole card is drawn again when it opens), ``moved_card`` (a card that was replaced by a new one because CardKit stops accepting writes to a card after 14 days)."""
from __future__ import annotations

from dataclasses import dataclass

from covey.cards import answer_card
from covey.cards.answer_card import literal_text
from covey.cards.elements import (CARD_BYTES, CARD_NODES, button_row, card, collapsible, fits, grey, item, md, note,
                                  panel_header, styled_panel, text_button, topic_button)

HEAD_CHARS = 30
MEMBER_TITLE = 36  # Of a topic's title on its fold's title line.
MAX_FOLDS = 8  # Folds drawn; the older ones are one line.
MAX_ROWS = 10  # Rows of the 分流记录, 专项记忆 and 资料库 folds.
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 2000  # The header is outside.
FIT_STEPS = ((MAX_ROWS, MAX_FOLDS), (6, MAX_FOLDS), (4, 6), (3, 5), (2, 4), (1, 3), (0, 2))  # (rows of a fold, topic folds) tried in turn.
ROW_CHARS = 90
CORRECTED = "（已纠正）"  # The end of a 分流记录 line the owner corrected (app/project_facts.py triage_lines), kept when it is clipped.
STEER_KIND, STOP_KIND, DOWN_KIND = "bridge_project_steer", "bridge_project_stop", "bridge_project_downgrade"
MEM_DEL_KIND, LIB_DEL_KIND = "bridge_project_mem_del", "bridge_project_lib_del"
TRIAGE_WRONG_KIND = "bridge_project_triage_wrong"
# The pause card's buttons (choice -> kind): app/project_loop.py CHOICES.
ASK_KINDS = {"continue": "bridge_project_ask_continue", "finish": "bridge_project_ask_finish", "stop": "bridge_project_ask_stop"}
KINDS = frozenset({STEER_KIND, STOP_KIND, DOWN_KIND, MEM_DEL_KIND, LIB_DEL_KIND, TRIAGE_WRONG_KIND, "bridge_project_triage_fix", *ASK_KINDS.values()})
RUNNING, WAITING, PAUSED, DONE = "在做", "等你", "停住", "做完"
# state -> (tag colour, header icon: one the other cards use, '' for none).
LOOKS = {RUNNING: ("blue", "waiting_outlined"), WAITING: ("orange", "chat_outlined"), PAUSED: ("neutral", ""),
         DONE: ("green", "yes_outlined")}
LETTER = {RUNNING: "r", WAITING: "w", PAUSED: "p", DONE: "d"}
OPEN = frozenset({RUNNING, WAITING})  # The folds that are open.


@dataclass(frozen=True)
class Member:
    """One member topic of the project. ``status`` is one of 在做, 等你, 做完 (a member that failed or was stopped is 做完
    too, its ``body`` says what happened); ``url`` opens its thread ('' while it is not known)."""
    topic_id: str
    status: str
    label: str = ""
    title: str = ""
    body: str = ""
    took: str = ""
    url: str = ""
    harness: str = ""  # The Harness it runs on, as the owner reads it (Claude Code); '' when not known.


@dataclass(frozen=True)
class Project:
    """What one card says. ``rows`` of the three folds: ``triage`` lines, ``memory`` (id, text), ``library`` (label, entry id) or (file name,).
    ``lead_topic`` and ``project_id`` go into the buttons' values."""
    project_id: str
    lead_topic: str
    name: str
    status: str
    need: str = ""
    need_url: str = ""
    members: tuple = ()
    elapsed: str = ""
    tokens: str = ""
    lead_state: str = ""
    lead_note: str = ""
    triage: tuple = ()
    joins: tuple = ()  # The 归入记录: (line, topic id) each, the newest first; a topic id '' when the line has no 移出 button.
    memory: tuple = ()
    library: tuple = ()
    inputs: tuple = ()
    outputs: tuple = ()
    ask_text: str = ""
    ask_at: str = ""
    ask_url: str = ""
    origin_note: str = ""  # How a project that grew from a quick job says so: "由管家转成专项" (app/projects.py).


def clipped(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def header(project):
    color, icon = LOOKS[project.status]
    tags = [{"tag": "text_tag", "text": {"tag": "plain_text", "content": project.status}, "color": color}]
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
    for member in project.members:
        kinds[member.status] = kinds.get(member.status, 0) + 1
    parts = " · ".join(f"{word} {kinds[word]}" for word in (RUNNING, WAITING, DONE) if kinds.get(word))
    return f"话题 {len(project.members)}" + (f"（{parts}）" if parts else "")


def meta_line(project) -> str:
    parts = [count_line(project), f"用时 {project.elapsed}" if project.elapsed else "",
             f"token {project.tokens}" if project.tokens else "", clipped(project.origin_note, 40)]
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


def ordered(members) -> tuple:
    """The members as the card lists them: the ones that wait for the owner, then the ones at work, then the ones that
    ended; each in the order they came in (the record's)."""
    rank = {WAITING: 0, RUNNING: 1, DONE: 2}
    return tuple(sorted(members, key=lambda member: rank.get(member.status, 2)))


def member_fold(member):
    letter = LETTER.get(member.status, "d")
    key = f"{short(member.topic_id)}_{letter}"
    title = " · ".join(part for part in (member.label, clipped(member.title, MEMBER_TITLE), member.status) if part)
    body = [md(literal_text(clipped(member.body, 200)) if member.body else grey("—"), element_id=f"pb_{key}", size="normal_v2")]
    tail = " · ".join(part for part in (member.harness, f"用时 {member.took}" if member.took else "",
                                        f"[打开话题串 ›]({member.url})" if member.url else "") if part)
    if tail:
        body.append(md(grey(tail) if not member.url else tail, element_id=f"pq_{key}", size="notation"))
    return collapsible(f"pt_{key}", title, body, expanded=member.status in OPEN, size="normal_v2")


def member_elements(project, folds=MAX_FOLDS) -> list:
    shown = ordered(project.members)
    elements = [member_fold(member) for member in shown[:folds]]
    if len(shown) > folds:
        elements.append(note(f"更早 {len(shown) - folds} 条已收起，在各自的话题里看"))
        elements[-1]["element_id"] = "pj_more"
    return elements


def row_fold(element_id, title, rows, limit=MAX_ROWS) -> dict | None:
    rows = list(rows)
    if not rows:
        return None
    inside = rows[:limit]
    if len(rows) > limit:
        inside.append(note(f"另有 {len(rows) - limit} 条"))
    return collapsible(element_id, f"{title} · {len(rows)}", inside, expanded=False, size="normal_v2")


def triage_rows(project) -> list:
    """The 分流记录: a line each; a line that is (text, token) is a question the lead answered for the owner, and has the 不对
    button that opens the form of the correction (cards/triage_card.py)."""
    rows = []
    for line in project.triage:
        text, token = line if isinstance(line, tuple) else (line, "")
        text = str(text)
        mark = CORRECTED if text.endswith(CORRECTED) else ""  # Clipped, a long line kept its start and lost the mark.
        body = md(grey(literal_text(clipped(text[:len(text) - len(mark)], ROW_CHARS - len(mark)) + mark)), size="notation")
        rows.append(item(body, text_button("不对", {"kind": TRIAGE_WRONG_KIND, "project_id": project.project_id,
                                                  "topic_id": project.lead_topic, "token": token}, tone="danger"))
                    if token else body)
    return rows


def join_rows(project) -> list:
    """The 归入记录: who took which topic in and why, a line each; one still in the project has 移出 when 整理专项 is on (its
    click is 整理专项's, the topic card's 移出: kept out for good)."""
    from covey.cards.project_proposal import LEAVE_KIND

    rows = []
    for text, topic in project.joins:
        body = md(grey(literal_text(clipped(str(text), ROW_CHARS))), size="notation")
        rows.append(item(body, text_button("移出", {"kind": LEAVE_KIND, "topic_id": topic, "project_id": project.project_id}))
                    if topic else body)
    return rows


def memory_rows(project) -> list:
    return [item(md(literal_text(clipped(text, ROW_CHARS)), size="normal_v2"),
                 text_button("删除", {"kind": MEM_DEL_KIND, "project_id": project.project_id, "topic_id": project.lead_topic,
                                    "entry": str(entry_id)}, tone="danger"))
            for entry_id, text in project.memory]


def library_rows(project) -> list:
    return [item(md(literal_text(clipped(name, ROW_CHARS)), size="normal_v2"),
                 text_button("删除", {"kind": LIB_DEL_KIND, "project_id": project.project_id, "topic_id": project.lead_topic,
                                    "name": str(rest[0] if rest else name)}, tone="danger"))
            for (name, *rest) in project.library]


def buttons(project) -> list:
    """补充 and 停止专项 while the project is on, 不要专项 until it is done: the lead's topic id and the project's id are
    what a click carries (the click routes to the lead's topic, which the card's message belongs to)."""
    if project.status == DONE:
        return []
    value = {"project_id": project.project_id, "topic_id": project.lead_topic}
    row = [topic_button("补充", element_id="pj_steer", value={"kind": STEER_KIND, **value})]
    if project.status != PAUSED:
        row.append(topic_button("停止专项", element_id="pj_stop", tone="danger", value={"kind": STOP_KIND, **value}))
    row.append(topic_button("不要专项", element_id="pj_down", value={"kind": DOWN_KIND, **value}))
    return [button_row(row, "pj_btns")]


def summary(project) -> str:
    return f"{project.status} · {clipped(project.name, HEAD_CHARS)}"


def project_elements(project, rows, folds) -> list:
    ask = [answer_card.ask_block(project.ask_text, at=project.ask_at, url=project.ask_url)] if project.ask_text else []
    elements = [*ask, need_element(project)]
    elements.append(md(grey(literal_text(meta_line(project))), element_id="pj_meta", size="notation"))
    elements.append(md(lead_line(project), element_id="pj_lead", size="normal_v2"))
    elements += member_elements(project, folds)
    elements += [row_fold("pj_join", "归入记录", join_rows(project), rows),
                 row_fold("pj_triage", "分流记录", triage_rows(project), rows),
                 row_fold("pj_mem", "专项记忆", memory_rows(project), rows),
                 row_fold("pj_lib", "资料库", library_rows(project), rows),
                 answer_card.box("pj_io", answer_card.input_lines(project.inputs), list(project.outputs)),
                 *buttons(project)]
    return [element for element in elements if element]


def project_card(project):
    """The live card: ``project`` drawn whole. With every list full (ten rows in each of the three folds, eight topics, the
    texts at their longest) it would pass what a card takes (``CARD_NODES``, ``CARD_BYTES``): then the folds show fewer rows
    and fewer topics, step by step (``FIT_STEPS``), the rest said in one line each, until it fits."""
    for rows, folds in FIT_STEPS:
        elements = project_elements(project, rows, folds)
        if fits(elements, NODES, BYTES):
            break
    return styled_panel(header(project), elements, summary(project))


def line_block(words, text, url=""):
    block = {"tag": "interactive_container", "element_id": "pj_line", "width": "fill", "padding": "6px 10px 6px 10px",
             "corner_radius": "6px", "background_style": "grey-50", "vertical_spacing": "2px",
             "elements": [md(f"**{literal_text(words)}**", size="normal_v2"), note(literal_text(text))]}
    if url:
        block["behaviors"] = [{"type": "open_url", "default_url": url}]
    built = card([block], f"{words} · {text}", update_multi=True)
    built["config"]["width_mode"] = "compact"
    return built


def settled_card(name, *, url="", reason=""):
    """What the card of a project that is done becomes: one grey line, "✅ 做完 · 结论在下方 ›" opening the conclusion at
    ``url``. ``reason``: said instead for a project that ended some other way (stopped, no longer a project); no link."""
    words = f"✅ {DONE} · 结论在下方" + ("　›" if url else "") if not reason else f"⏹ {reason}"
    text = clipped(name, HEAD_CHARS) or "专项"
    return line_block(words, text, url if not reason else "")


def idle_card(name, days):
    """What the card of a project folded away for being idle becomes (state_reason ``idle``): one grey line, "收起 · 7 天没有新的事", and
    that new work opens it again. Not final: the next look draws the whole card once the project is active again."""
    return line_block(f"💤 收起 · {days:g} 天没有新的事", f"{clipped(name, HEAD_CHARS) or '专项'} · 有新的事会自动展开")


def moved_card(name, url=""):
    """The old card of a project whose card was sent again (the 14 days): "已移到新卡 ›" opening the new one."""
    text = clipped(name, HEAD_CHARS) or "专项"
    return line_block("已移到新卡" + ("　›" if url else ""), text, url)


def ask_card(name, why, *, project_id, lead_topic, note=""):
    """The card a guard's pause sends the owner: ``why`` in a sentence ("token 用到上限 1000 万"), ``note`` what became of the
    running topics, and 继续 / 收尾 / 停止. The buttons carry the project's id and the lead's topic, as the project card's do."""
    head = panel_header("chat_outlined", clipped(name, HEAD_CHARS) or "专项", "", [(PAUSED, "orange")], color="orange")
    value = {"project_id": project_id, "topic_id": lead_topic}
    elements = [md(f"**{literal_text(clipped(why, 80))}**", element_id="pa_why", size="normal_v2")]
    if note:
        elements.append(md(grey(literal_text(clipped(note, 80))), element_id="pa_note", size="notation"))
    elements.append(button_row([
        topic_button("继续", element_id="pa_go", tone="primary", value={"kind": ASK_KINDS["continue"], **value}),
        topic_button("收尾", element_id="pa_wrap", value={"kind": ASK_KINDS["finish"], **value}),
        topic_button("停止", element_id="pa_stop", tone="danger", value={"kind": ASK_KINDS["stop"], **value})], "pa_btns"))
    return styled_panel(head, elements, f"{PAUSED} · {clipped(name, HEAD_CHARS)}：{clipped(why, 40)}")


ASKED_MARKS = {"continue": "▶️ 你选了继续", "finish": "🏁 你选了收尾", "stop": "⏹ 你选了停"}


def asked_card(name, why, choice=""):
    """What the pause card becomes once it is answered: one grey line saying what the owner chose (``choice`` one of
    ``ASK_KINDS``), or, for ``''``, that the project moved on some other way (the owner's own turn, the lead, a stop)."""
    return line_block(ASKED_MARKS.get(choice, "已在别处处理"), " · ".join(part for part in (clipped(name, HEAD_CHARS) or "专项", clipped(why, 40)) if part))


def merged_card(name, url=""):
    """The old 派发卡 of a job that became part of a project (app/projects.py upgrade): "已并入专项 ›" opening the project's card."""
    text = clipped(name, HEAD_CHARS) or "专项"
    return line_block("已并入专项" + ("　›" if url else ""), text, url)

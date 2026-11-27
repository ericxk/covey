"""The 专项卡 (app/project_cards.py; docs/projects.md 专项卡), when COVEY_PROJECTS=1: the one card of a project at the chat's
top level, a message of its own (never a reply: Feishu puts a reply to a message that has a thread into the thread, see
cards/dispatch_card.py). It is drawn whole from ``Project`` (what the project's record and its topics' job records say),
again each time that changes, and once more as a single line when the project is done.

It is the lead's card (docs/ui.md 5.3: a reply to it goes to the lead), so it has no title bar: its first element is the
lead's speaker row, as the lead's answers have (cards/ui.py speaker_row, docs/ui.md 4 说话人). Top to bottom, element IDs
(CardKit takes at most 20 characters, rule 9) in the order drawn:

- ``speaker_row``: the lead's picture (``avatar``, an image key app/project_cards.py had uploaded: none, the name alone),
  **项目名 · 负责人**, the project's state as a tag in its colour (core/look.py STATES: 在做, 等你, 停住了 and why for a
  guard's pause, 已停止 for the owner's stop, 做完) and, for a project the owner's own message started, what it does with
  it ("在办你 09:10 交代的事 ›", the row opening his message);
- ``answer_words`` (that project only): his words, one grey line that opens (cards/answer_card.py words_fold);
- ``pj_need``: what waits for him, as the conclusion band of a waiting relay says it (等你 · T12, 要你定：…), the whole
  band opening the question card when it has a link;
- ``pj_meta``: one grey line, member topics and how they stand, 用时, token;
- ``pj_lead``: the lead as it is now and the last line it noted;
- ``pt_<topic>_<s>``: a member at work or asking for the owner (等你, 停住了, 可能卡住, 在做: a member that failed, was
  interrupted or was stopped by the lead is 停住了), as a topic row of the 看板 (cards/ui.py topic_row: its T number's badge,
  its state's dot and word, its title, what it does or why it stopped, its Harness and 用时; the whole row opens its
  thread); ``s`` is the state's letter. At most the step's count (``FIT_STEPS``), the rest one grey line ``pj_more``;
- ``pj_ended``: the members done or stopped by the owner (做完, 已停止), one fold, a line each (its T number a link);
- ``pj_triage``, ``pj_lib``: the 分流记录 and 资料库 folds (closed), drawn only with rows; each row of the second has a 删
  button, and a row of the first that the lead answered for the owner a 不对 button (``triage_rows``);
- ``pj_mem``: 专项记忆 as one grey line of counts (规矩 · 决定 · 资料, 待审核 when some wait) and, with the web page on, the link to
  the 网页记忆中心; no rows and no 删 button here (managing the memory is the page's; the older card's 删 says it is offline);
- ``pj_io``: the grey 输入/输出 box (cards/answer_card.py ``box``);
- ``pj_btns``: 补充 (``pj_steer``), 换配置 (``pj_switch``: the lead's Harness, model and budget, app/harness_switch.py's card
  with the project named, ``SWITCH_LEAD``), 停止专项 (``pj_stop``, while the project is at work), 不要专项 (``pj_down``).

Two cards of the project's own more, with COVEY_PROJECT_ASK=1 (``ask_card``, ``asked_card``): the one a guard's pause sends the owner,
a message of its own where the project's card is (``pa_why`` the reason in a sentence, ``pa_note``, ``pa_btns``: 继续 ``pa_go``,
收尾 ``pa_wrap``, 停 ``pa_stop``), and what it becomes once the owner chose (one line, ``pj_line``, saying what he chose).

No input field: a card that is redrawn must not hold one (docs/cards.md 卡片的客户端限制); 补充 sends a card of its own.
Two more cards of one line: ``settled_card`` (the project is done: ``pj_line``, linking to the conclusion) and
``idle_card`` (a project with nothing to do for COVEY_PROJECT_IDLE_DAYS days, folded away; the whole card is drawn again when it opens), ``moved_card`` (a card that was replaced by a new one because CardKit stops accepting writes to a card after 14 days)."""
from __future__ import annotations

from dataclasses import dataclass

from covey.cards import answer_card, ui
from covey.cards.answer_card import literal_text
from covey.cards.topic_actions import SWITCH_OPEN
from covey.cards.elements import (CARD_BYTES, CARD_NODES, button_row, card, collapsible, fits, grey, item, md, note,
                                  panel_header, styled_panel, text_button, topic_button)
from covey.core import look

HEAD_CHARS = 30
REASON_CHARS = 16  # Of a guard's reason in the header's tag (停住了 · token 用完了).
MEMBER_TITLE = 36  # Of a topic's title on its fold's title line.
MAX_FOLDS = 8  # Members at work or asking drawn as rows; the rest are one line.
MAX_ROWS = 10  # Rows of the 分流记录, 专项记忆 and 资料库 folds.
NODES, BYTES = CARD_NODES - 10, CARD_BYTES - 2000  # Room left for what CardKit adds.
NEED_CHARS = 60  # Of what waits for the owner on the band's headline.
FIT_STEPS = ((MAX_ROWS, MAX_FOLDS), (6, MAX_FOLDS), (4, 6), (3, 5), (2, 4), (1, 3), (0, 2))  # (rows of a fold, topic folds) tried in turn.
ROW_CHARS = 90
CORRECTED = "（已纠正）"  # The end of a 分流记录 line the owner corrected (app/project_facts.py triage_lines), kept when it is clipped.
STEER_KIND, STOP_KIND, DOWN_KIND = "bridge_project_steer", "bridge_project_stop", "bridge_project_downgrade"
MEM_DEL_KIND, LIB_DEL_KIND = "bridge_project_mem_del", "bridge_project_lib_del"  # MEM_DEL_KIND: no longer drawn, kept to take a card drawn before.
SWITCH_LEAD = "project_id"  # In 换配置's value (app/harness_switch.py LEAD): the project whose lead it changes.
# The pause card's buttons (choice -> kind): app/project_loop.py CHOICES.
ASK_KINDS = {"continue": "bridge_project_ask_continue", "finish": "bridge_project_ask_finish", "stop": "bridge_project_ask_stop"}
KINDS = frozenset({STEER_KIND, STOP_KIND, DOWN_KIND, MEM_DEL_KIND, LIB_DEL_KIND, *ASK_KINDS.values()})
# A project's state and a member's, as core/look.py STATES keys: the words and colours are the table's (docs/ui.md 2.2,
# 3). A project a guard paused is STOPPED (停住了 and why, ``Project.reason``), one the owner stopped CANCELLED (已停止);
# a member STUCK may be stuck (core/progress.py maybe_stuck).
RUNNING, WAITING, STOPPED, STUCK, CANCELLED, DONE = "running", "waiting", "stopped", "stuck", "cancelled", "done"
IDLE = "idle"  # A project folded away (its card is ``idle_card``): 空闲.
# What the lead does with the owner's words, by the project's state (cards/answer_card.py SAYS ``doing``).
DOING = {RUNNING: "running", WAITING: "waiting", STOPPED: "stopped", CANCELLED: "stopped", DONE: "done"}
LETTER = {RUNNING: "r", WAITING: "w", STOPPED: "s", STUCK: "k", CANCELLED: "c", DONE: "d", IDLE: "i"}
OPEN = frozenset({RUNNING, WAITING, STOPPED, STUCK})  # The members drawn as rows: at work, or asking for the owner.
AT_WORK = frozenset({RUNNING, WAITING})  # A project 停止专项 can stop.
RANK = {WAITING: 0, STOPPED: 1, STUCK: 2, RUNNING: 3, DONE: 4, CANCELLED: 4}  # The order of the folds (``ordered``).


@dataclass(frozen=True)
class Member:
    """One member topic of the project. ``status``: its state, a core/look.py STATES key (RUNNING, WAITING, STUCK,
    STOPPED, CANCELLED, DONE; ``body`` says what it does, or why it stopped); ``url`` opens its thread ('' while it is not
    known)."""
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
    """What one card says. ``status``: its state, a core/look.py STATES key (RUNNING, WAITING, STOPPED with ``reason``,
    CANCELLED, DONE). ``rows`` of the three folds: ``triage`` lines, ``memory`` (id, text), ``library`` (label, entry id)
    or (file name,). ``lead_topic`` and ``project_id`` go into the buttons' values."""
    project_id: str
    lead_topic: str
    name: str
    status: str
    reason: str = ""  # Why a guard paused it ("token 用完了", core/projects.py stop_reason): after 停住了 in its tag.
    need: str = ""
    need_url: str = ""
    need_from: str = ""  # Who asks it: a member's T number, else ''.
    members: tuple = ()
    elapsed: str = ""
    tokens: str = ""
    lead_state: str = ""
    lead_note: str = ""
    triage: tuple = ()
    joins: tuple = ()  # The 归入记录: (line, topic id) each, the newest first; a topic id '' when the line has no 移出 button.
    memory: tuple = ()
    memory_total: int = 0  # How many lines the memory holds in all (``memory`` lists the newest LIST_CAP of them); 0: as many as listed.
    memory_counts: tuple = ()  # (rules, decisions, notes, waiting for review) of the project's memory (core/memstore.py); () when not read.
    memory_url: str = ""  # The 网页记忆中心's address (app/web_memory.py center_url); '' without the web page.
    library: tuple = ()
    inputs: tuple = ()
    outputs: tuple = ()
    ask_text: str = ""
    ask_at: str = ""
    ask_url: str = ""
    origin_note: str = ""  # How a project that grew from a quick job says so: "由管家转成专项" (app/projects.py).
    avatar: str = ""  # The lead's picture, an image key (app/answer_cards.py avatar); '' for its name alone.


def clipped(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def state_words(project) -> str:
    """The project's state as its tag says it: the word (core/look.py STATES; a state it does not know as it is), and
    why for a guard's pause (停住了 · token 用完了)."""
    word = look.word(project.status) if project.status in look.STATES else str(project.status or "")
    return f"{word} · {clipped(project.reason, REASON_CHARS)}" if project.status == STOPPED and project.reason else word


def speaker(project):
    """(the lead's speaker row, the owner's words folded or None): **名 · 负责人**, the project's state as a tag (``state_words``
    in its colour), what the lead does with the owner's words when his message started the project (its row opening it)."""
    found = look.STATES.get(project.status)
    tag = (state_words(project), found.color if found else "neutral")
    who = look.speaker("lead", name=clipped(project.name, HEAD_CHARS) or "专项")
    meta = None
    if project.ask_text:
        block = answer_card.ask_block(project.ask_text, at=project.ask_at, url=project.ask_url, who=who,
                                      doing=DOING.get(project.status, "running"), avatar=project.avatar,
                                      full=project.ask_text)
        meta, _ = ui.split_ask([block] if block else [])
    row = ui.speaker_row(meta, label=who, tag=tag, avatar=project.avatar)
    return row, (answer_card.words_fold(meta) if meta else None)


def short(topic_id) -> str:
    return str(topic_id or "")[:6]


def count_line(project) -> str:
    """话题 N and how many of each state, what asks for the owner first, in the words of core/look.py STATES."""
    kinds = {}
    for member in project.members:
        kinds[member.status] = kinds.get(member.status, 0) + 1
    parts = " · ".join(f"{look.word(key)} {kinds[key]}" for key in (WAITING, STOPPED, STUCK, RUNNING, DONE, CANCELLED)
                       if kinds.get(key))
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
    """What waits for the owner, the card's visual centre (docs/ui.md 4 结论带): 等你 · who asks, 要你定：what; the band opens
    the question card when it has a link (``need_url``)."""
    if not project.need:
        return None
    band = ui.conclusion_band(ui.band_label(WAITING, project.need_from), literal_text("要你定：" + clipped(project.need, NEED_CHARS)),
                              "点这里打开问题卡" if project.need_url else "在它的话题里回答", url=project.need_url)
    band["element_id"] = "pj_need"
    return band


def ordered(members) -> tuple:
    """The members as the card lists them: what asks for the owner first (等你, 停住了, 可能卡住: docs/ui.md 5.1), then
    the ones at work, then the ones that ended; each in the order they came in (the record's)."""
    return tuple(sorted(members, key=lambda member: RANK.get(member.status, RANK[DONE])))


def member_row(member):
    """A member at work or asking for the owner as the 看板 draws a topic (cards/ui.py topic_row): its T number's badge, its
    state, its title, what it does (why it stopped), its Harness and 用时; the whole row opens its thread."""
    key = f"{short(member.topic_id)}_{LETTER.get(member.status, 'd')}"
    state = member.status if member.status in look.STATES else "idle"
    head = " ".join(part for part in (member.label, clipped(member.title, MEMBER_TITLE)) if part) or "话题"
    return ui.topic_row(state, head, clipped(member.body, 200) if member.body else "",
                        (member.harness, f"用时 {member.took}" if member.took else ""), url=member.url, element_id=f"pt_{key}")


def ended_line(member):
    """A member done (or stopped by the owner) as a line of the fold: its T number a link, its title, its state, 用时."""
    word = look.word(member.status) if member.status in look.STATES else str(member.status or "")
    tail = " · ".join(part for part in (word, f"用时 {member.took}" if member.took else "") if part)
    return md(ui.topic_ref(member.label, clipped(member.title, MEMBER_TITLE), member.url) + " " + grey("· " + tail),
              size="normal_v2")


def member_elements(project, folds=MAX_FOLDS, rows=MAX_ROWS) -> list:
    """The members at work or asking first (``ordered``), a row each, at most ``folds`` of them, the rest one line; those done
    or stopped by the owner one fold, at most ``rows`` lines."""
    shown = ordered(project.members)
    live = [member for member in shown if member.status in OPEN]
    ended = [member for member in shown if member.status not in OPEN]
    elements = [member_row(member) for member in live[:folds]]
    if len(live) > folds:
        more = note("还有 " + "、".join(member.label or "话题" for member in live[folds:folds + 6])
                    + (" 等" if len(live) > folds + 6 else "") + f" {len(live) - folds} 个话题，在各自的话题里看")
        more["element_id"] = "pj_more"
        elements.append(more)
    if ended:
        counts = [(look.word(key), sum(member.status == key for member in ended)) for key in (DONE, CANCELLED)]
        others = len(ended) - sum(n for _, n in counts)
        title = " · ".join([f"{word} {n} 个" for word, n in counts if n] + ([f"其他 {others} 个"] if others else []))
        lines = [ended_line(member) for member in ended[:rows]]
        if len(ended) > rows:
            lines.append(note(f"另有 {len(ended) - rows} 个"))
        elements.append(collapsible("pj_ended", title, lines, quiet=True))
    return elements


def row_fold(element_id, title, rows, limit=MAX_ROWS, total=0) -> dict | None:
    """A fold of ``rows`` (``limit`` shown); ``total``: how many there are in all when the rows are fewer (the count and the
    rest are said of all of them)."""
    rows = list(rows)
    if not rows:
        return None
    total = max(total, len(rows))
    inside = rows[:limit]
    if total > limit:
        inside.append(note(f"另有 {total - limit} 条"))
    return collapsible(element_id, f"{title} · {total}", inside, expanded=False, size="normal_v2")


def triage_rows(project) -> list:
    """The 分流记录: a line each (a line that is (text, token) is a question the lead answered for the owner; its 不对 button
    was taken off, T898: the owner says the correction in the topic)."""
    rows = []
    for line in project.triage:
        text = str(line[0] if isinstance(line, tuple) else line)
        mark = CORRECTED if text.endswith(CORRECTED) else ""  # Clipped, a long line kept its start and lost the mark.
        body = md(grey(literal_text(clipped(text[:len(text) - len(mark)], ROW_CHARS - len(mark)) + mark)), size="notation")
        rows.append(body)
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


def memory_line(project) -> dict | None:
    """专项记忆 as a line of counts (``pj_mem``): ``规矩 N · 决定 N · 资料 N`` and ``· 待审核 N`` when some wait, with the web page on a
    link to the 网页记忆中心. None when the project has none and nothing waits."""
    if project.memory_counts:
        rules, decisions, notes, waiting = project.memory_counts
        if not (rules or decisions or notes or waiting):
            return None
        words = f"规矩 {rules} · 决定 {decisions} · 资料 {notes}" + (f" · 待审核 {waiting}" if waiting else "")
    elif project.memory_total or project.memory:
        words = f"{max(project.memory_total, len(project.memory))} 条"
    else:
        return None
    link = f" · [去网页记忆中心]({project.memory_url})" if project.memory_url else ""
    return md(grey(f"专项记忆 · {words}") + link, element_id="pj_mem", size="notation")


def library_rows(project) -> list:
    return [item(md(literal_text(clipped(name, ROW_CHARS)), size="normal_v2"),
                 text_button("删除", {"kind": LIB_DEL_KIND, "project_id": project.project_id, "topic_id": project.lead_topic,
                                    "name": str(rest[0] if rest else name)}, tone="danger"))
            for (name, *rest) in project.library]


def buttons(project) -> list:
    """补充 and 换配置 (the lead's Harness, model and budget: 换配置's own kind, the project named, SWITCH_LEAD) until the project
    is done, 停止专项 while it is at work, 不要专项 until it is done: the lead's topic id and the project's id are what a click
    carries (the click routes to the lead's topic, which the card's message belongs to)."""
    if project.status == DONE:
        return []
    value = {"project_id": project.project_id, "topic_id": project.lead_topic}
    row = [topic_button("补充", element_id="pj_steer", value={"kind": STEER_KIND, **value}),
           topic_button("换配置", element_id="pj_switch", value={"kind": SWITCH_OPEN, "topic_id": project.lead_topic,
                                                                 SWITCH_LEAD: project.project_id})]
    if project.status in AT_WORK:
        row.append(topic_button("停止专项", element_id="pj_stop", tone="danger", value={"kind": STOP_KIND, **value}))
    row.append(topic_button("不要专项", element_id="pj_down", value={"kind": DOWN_KIND, **value}))
    return [button_row(row, "pj_btns")]


def summary(project) -> str:
    return f"{state_words(project)} · {clipped(project.name, HEAD_CHARS)}"


def project_elements(project, rows, folds) -> list:
    row, said = speaker(project)
    elements = [row, said, need_element(project)]
    elements.append(md(grey(literal_text(meta_line(project))), element_id="pj_meta", size="notation"))
    elements.append(md(lead_line(project), element_id="pj_lead", size="normal_v2"))
    elements += member_elements(project, folds, rows)
    elements += [row_fold("pj_join", "归入记录", join_rows(project), rows),
                 row_fold("pj_triage", "分流记录", triage_rows(project), rows),
                 memory_line(project),
                 row_fold("pj_lib", "资料库", library_rows(project), rows),
                 answer_card.box("pj_io", answer_card.input_lines(project.inputs), list(project.outputs)),
                 *buttons(project)]
    return [element for element in elements if element]


def project_card(project):
    """The live card: ``project`` drawn whole, the lead's speaker row first, no title bar. With every list full (ten rows in
    each of the folds, eight topics, the texts at their longest) it would pass what a card takes (``CARD_NODES``,
    ``CARD_BYTES``): then the folds show fewer rows and fewer topics, step by step (``FIT_STEPS``), the rest said in one line
    each, until it fits. The band's and the badges' colours go into its config (cards/ui.py banded)."""
    for rows, folds in FIT_STEPS:
        elements = project_elements(project, rows, folds)
        if fits(elements, NODES, BYTES):
            break
    return ui.banded(card(elements, summary(project), default_width=True, update_multi=True, padding=ui.BODY_PADDING,
                          spacing=ui.BODY_SPACING))


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
    words = f"✅ {look.word(DONE)} · 结论在下方" + ("　›" if url else "") if not reason else f"⏹ {reason}"
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
    running topics, and 继续 / 收尾 / 停止. The buttons carry the project's id and the lead's topic, as the project card's do.
    Its tag is 停住了 in its red (core/look.py STATES: a guard's stop is not the owner's, docs/ui.md 3), not orange, which
    says 等你 and 可能卡住 alone."""
    stopped = look.STATES["stopped"]
    head = panel_header("chat_outlined", clipped(name, HEAD_CHARS) or "专项", "", [(stopped.word, stopped.color)],
                        color=stopped.color)
    value = {"project_id": project_id, "topic_id": lead_topic}
    elements = [md(f"**{literal_text(clipped(why, 80))}**", element_id="pa_why", size="normal_v2")]
    if note:
        elements.append(md(grey(literal_text(clipped(note, 80))), element_id="pa_note", size="notation"))
    elements.append(button_row([
        topic_button("继续", element_id="pa_go", tone="primary", value={"kind": ASK_KINDS["continue"], **value}),
        topic_button("收尾", element_id="pa_wrap", value={"kind": ASK_KINDS["finish"], **value}),
        topic_button("停止", element_id="pa_stop", tone="danger", value={"kind": ASK_KINDS["stop"], **value})], "pa_btns"))
    return styled_panel(head, elements, f"{stopped.word} · {clipped(name, HEAD_CHARS)}：{clipped(why, 40)}")


ASKED_MARKS = {"continue": "▶️ 你选了继续", "finish": "🏁 你选了收尾", "stop": "⏹ 你选了停"}


def asked_card(name, why, choice=""):
    """What the pause card becomes once it is answered: one grey line saying what the owner chose (``choice`` one of
    ``ASK_KINDS``), or, for ``''``, that the project moved on some other way (the owner's own turn, the lead, a stop)."""
    return line_block(ASKED_MARKS.get(choice, "已在别处处理"), " · ".join(part for part in (clipped(name, HEAD_CHARS) or "专项", clipped(why, 40)) if part))


def merged_card(name, url=""):
    """The old 派发卡 of a job that became part of a project (app/projects.py upgrade): "已并入专项 ›" opening the project's card."""
    text = clipped(name, HEAD_CHARS) or "专项"
    return line_block("已并入专项" + ("　›" if url else ""), text, url)

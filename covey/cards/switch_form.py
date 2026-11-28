"""The 🔁 换配置 card (app/harness_switch.py keeps the record it is drawn from and handles its clicks).

Harness, then provider, then model, each a short select whose choice redraws the card with the next
one's options; the model's budget and, where it has them,
its work mode. Each select records its choice and redraws the card; 确认更换 applies them together.

The look: the topic's current choice in one grey line, then the settings
list (cards/launch_config.py setting_row), a row whose choice differs from the topic's with its label blue
and bold. A row's options open under it, as on the launch card (no select).

A card drawn since the home start (``no_place`` in its record) has no workspace or run mode rows either; a card sent before
still draws and applies them (compat: it is a record's own choice).

A project's lead's card (the record's ``lead``, opened from its 专项卡, app/harness_switch.py) has no workspace or run mode
rows, names the project in its subtitle and says what the lead's next turn gets (LEAD_NOTE).
"""
from __future__ import annotations

from covey.cards.elements import callback_button, fold, grey, md, note, panel_header, row, styled_panel
from covey.cards.launch_config import (cut_rows, fitted, model_count, model_settings, run_mode_setting,
                                       setting_row, settings_block, workspace_setting)
from covey.core.menu_choice import harness_of
from covey.core.defaults import work_mode_tag
from covey.core.text import literal, redact
from covey.core.workspaces import RUN_MODES

HARNESS = "bridge_switch_harness"
PROVIDER = "bridge_switch_provider"
MODEL = "bridge_switch_model"
BUDGET = "bridge_switch_budget"
WORK_MODE = "bridge_switch_work_mode"
WORKSPACE = "bridge_switch_workspace"
RUN_MODE = "bridge_switch_run_mode"
CONFIRM = "bridge_switch_confirm"
CANCEL = "bridge_switch_cancel"
PAGE = "bridge_switch_page"  # A row's tap on a card sent with options pages (84caa8a): that row drawn open.
# What a switch does, as its card says it: the handoff's details (the transcript file, HANDOFF_ROUNDS
# rounds in plain text, app/switch_handoff.py) are the model's to know, not the card's.
SWITCH_NOTE = ("换 Harness、模型或预算会开新会话，上下文不断：新会话拿到交接摘要和最近的问答，完整记录放在文件里，"
               "需要时自己去读；交接了什么写在下一张任务卡上。旧会话没答的问题会取消。")
SWITCH_GIST = "换了以后：开新会话接着做，上下文不断"  # SWITCH_NOTE's one line under the buttons, which opens on it.
# A lead's card (``lead``): what the switch does to the lead, and its one line.
LEAD_NOTE = ("换 Harness、模型或预算后，负责人下一轮开新会话：重新读一遍专项的状态（目标、话题、回执、记忆），最近几轮对话一起带上；"
             "它在哪个 Harness 上都只读，只用 Bridge 工具派话题。这里只列能当负责人的模型（能接 Bridge 工具）；工作区不变。")
LEAD_GIST = "换了以后：负责人开新会话，重读专项的状态"
LEAD_BUSY = "负责人还有一轮在排队或在做：等它做完再点确认更换，这张卡一直有效。"
NOTE_ID = "switch_note"
HANDOFF_ID = "switch_handoff"  # The 交接 block of the first task card after a switch (``handoff_block``).
HANDOFF_SUMMARY_CHARS = 6000  # Of the old model's summary the block opens on; its file keeps it whole.
UNCHANGED = "先改一项再点确认更换"
# The subtitle's second half on a card at the private chat's top level (``top_level``, opened from a relay card there,
# app/harness_switch.py): a top-level message is the butler's, a reply to this card the topic's.
TOP_SUBTITLE = "确认后回复这张卡接着做"


def offered(form):
    """The models the card offers: all of them, each naming its Harness (a record from before the Harness select: its
    route key's)."""
    return {key: {"harness": key.partition(":")[0], **entry} for key, entry in form["models"].items()}


def place(workspace, run_mode):
    return f"{workspace}（{RUN_MODES.get(run_mode, run_mode)}）"


def current_line(form):
    current = form["current"]
    mode = work_mode_tag(current.get("work_mode"))
    return (f"当前：{current['label']} · 思考预算 {current.get('effort') or '模型默认'}" + (f" · {mode}" if mode else "")
            + ("" if form.get("no_place") else
               f"\n工作区 {place(current.get('workspace') or form.get('workspace') or '', current.get('run_mode') or 'direct')}"))


def changes(form):
    """The settings-list labels whose choice differs from the topic's binding or place."""
    current, models = form["current"], form["models"]
    now, was = models.get(form.get("selected_model"), {}), models.get(current.get("key"), {})
    harness_now, harness_was = harness_of(form.get("selected_model") or "", now), harness_of(current.get("key") or "", was)
    differs = {
        "Harness": harness_now != harness_was,
        "供应商": (harness_now, now.get("provider", "")) != (harness_was, was.get("provider", "")),
        "模型": form.get("selected_model") != current.get("key"),
        "思考预算": form.get("selected_effort") != current.get("budget"),
        "工作模式": (form.get("selected_work_mode") or "") != (current.get("work_mode") or ""),
        # A card drawn since the home start has neither row (``no_place``): nothing of the place can differ.
        "工作区": not form.get("no_place") and form.get("selected_workspace") != current.get("workspace"),
        "运行方式": not form.get("no_place") and form.get("selected_run_mode") != current.get("run_mode"),
    }
    return frozenset(label for label, changed in differs.items() if changed)


def unchanged(form):
    """Nothing chosen differs from the topic's binding and place."""
    return not (changes(form) - {"Harness", "供应商"})  # Those two change with the model.


def switch_settings(form, limit=None):
    """The card's settings rows, as the record chose them; ``limit``: the most models its 模型 row lists (fitted), the
    topic's own among them all the same."""
    model = form["selected_model"]
    choices = offered(form)
    settings = []
    if choices and model in choices:
        settings += model_settings(choices, model, kinds=(HARNESS, PROVIDER, MODEL, BUDGET, WORK_MODE),
                                   revision=form["config_revision"], effort=form.get("selected_effort", ""),
                                   work_mode=form.get("selected_work_mode", ""), limit=limit,
                                   keep=(form["current"].get("key") or "",))
    # compat(ecb0458): a record from before the workspace select names only the topic's workspace; retire when no
    # switch-forms record lacks selected_workspace (compat_report: switch-form-fields)
    workspace = form.get("selected_workspace") or form.get("workspace") or ""
    names = form.get("workspaces") or [workspace]
    if form.get("lead") or form.get("no_place"):
        return settings  # A project's lead stays where it reads; a card of the home start has no place to choose.
    settings += [workspace_setting("workspace", WORKSPACE, workspace, names),
                 run_mode_setting("run_mode", RUN_MODE, form.get("selected_run_mode") or "direct")]
    return settings


def build_switch_card(form):
    """The 🔁 换配置 card; ``form`` is the switch record, its ``page`` the row drawn open, ``top_level`` a card at the private
    chat's top level (its subtitle names the topic and where a reply goes). Its 模型 row is cut to what one card holds
    (cards/launch_config.py fitted)."""
    choices, model = offered(form), form["selected_model"]
    total = model_count(choices, model) if model in choices else 0
    return fitted(lambda limit: switch_card(form, switch_settings(form, limit), total), total)


def switch_card(form, settings, total):
    """build_switch_card with ``settings``, of a 模型 row out of ``total`` models."""
    token, model = form["token"], form["selected_model"]
    choices = offered(form)
    changed = changes(form)
    lead = form.get("lead") or {}
    subtitle = (f"专项「{lead.get('name') or '专项'}」的负责人 · 确认后负责人下一轮起生效" if lead
                else (f"话题 {form['topic']} · " if form.get("topic") else "") + (
                    TOP_SUBTITLE if form.get("top_level") else "下一条消息起生效"))
    elements = [note(current_line(form))]
    if not choices or model not in choices:
        elements.append(md("模型目录准备中，请稍后重新打开换配置。"))
    # Its token stays the same across redraws: the revision makes each redraw's rows new, so a chosen row closes.
    stamp = f"r{form['config_revision'] % 10000}"
    elements.append(settings_block(cut_rows(settings, [setting_row(setting, token=token, open_key=form.get("page") or "",
                                                                   changed=setting.label in changed, stamp=stamp)
                                                       for setting in settings], total)))
    if form.get("active_task"):
        elements.append(note(LEAD_BUSY if lead else "任务还在排队或执行时不能更换：先在任务卡上停止，停止完成后再确认。"))
    if form.get("notice"):
        elements.append(md(form["notice"]))
    elif unchanged(form):
        elements.append(note(UNCHANGED))
    # Greyed out until a choice changes (a blue 确认更换 over 先改一项再点确认更换 read as clickable);
    # a click on an older card is still refused with UNCHANGED (app/binding.py).
    confirm = callback_button("确认更换", {"kind": CONFIRM, "token": token}, style="primary_filled", width="fill",
                              disabled=True if unchanged(form) else None)
    elements.append(row((confirm, 1), (callback_button("取消", {"kind": CANCEL, "token": token}, style="default",
                                                       width="fill"), 1)))
    # What a switch does, one grey line under the buttons that opens on it: its four lines sat between the choices and
    # the buttons (the owner, 2026-11-23, of the same on a question card: 不再横亘在正文和按钮中间).
    elements.append(fold(NOTE_ID, md(grey(LEAD_GIST if lead else SWITCH_GIST), size="notation"),
                         [md(LEAD_NOTE if lead else SWITCH_NOTE, size="notation")],
                         title_padding="0px 0px 0px 0px", icon_position="follow_text", spacing="4px"))
    return styled_panel(panel_header("switch", "换配置", subtitle), elements, "换配置 · Harness、模型、预算")


def handoff_block(before, after, *, summary="", rounds=0, kept=0, state="done", why=""):
    """What the first task card after a switch says the new model was handed (T647 E1: a line in the folded 执行过程 was all
    there was), right above the answer: "**交接** Claude Code · opus（high） → Codex · gpt-5（high）" and a grey line under it.
    ``state``: "waiting" while the old model writes its summary (nothing has started yet), "stopped" when the turn was
    stopped then, else handed over. With the old model's ``summary`` the block is a fold that opens on it, its line "原模型写了
    交接摘要（1,234 字，点开看），和最近 1 轮原文、完整记录（共 12 轮）一起交给了新模型"; without one, ``why`` there is none
    and that the rounds went instead. ``rounds``: the transcript's, ``kept``: those in plain text. One element by
    HANDOFF_ID, replaced in place as the handoff goes on."""
    head = f"**交接** {literal(before)} → {literal(after)}"
    if state == "waiting":
        return md(head + "\n" + grey("正在请原模型写交接摘要（最多 3 分钟），写好后开始这一轮"), element_id=HANDOFF_ID)
    if state == "stopped":
        return md(head + "\n" + grey("已停止，没有写交接摘要"), element_id=HANDOFF_ID)
    record = f"完整记录（共 {rounds} 轮）" if rounds else "完整记录"
    if not summary:
        reason = f"没有交接摘要（{literal(why)}）" if why else "没有交接摘要"
        recent = f"最近 {kept} 轮原文和" if kept else ""
        return md(head + "\n" + grey(f"{reason}：{recent}{record}交给了新模型"), element_id=HANDOFF_ID)
    recent = f"最近 {kept} 轮原文、" if kept else ""
    text = redact(summary).strip()
    shown = text if len(text) <= HANDOFF_SUMMARY_CHARS else text[:HANDOFF_SUMMARY_CHARS] + "\n\n…（完整摘要在话题的交接文件里）"
    line = grey(f"原模型写了交接摘要（{len(text):,} 字，点开看），和{recent}{record}一起交给了新模型")
    return fold(HANDOFF_ID, md(head + "\n" + line), [md(shown)], spacing="6px")

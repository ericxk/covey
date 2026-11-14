"""The 🔁 换配置 card (app/harness_switch.py keeps the record it is drawn from and handles its clicks).

Harness, then provider, then model, each a short select whose choice redraws the card with the next
one's options (the owner, 2026-11-03: 不光是能换模型，Harness 等等都可以换); the model's budget and, where it has them,
its work mode; the workspace and the run mode; 敏感词脱敏 where the model can redact, and while it
is on only such models. Each select records its choice and redraws the card; 确认更换 applies them together.

The look (the owner, 2026-11-03 22:59): the topic's current choice in one grey line, then the settings
list (cards/launch_config.py setting_row), a row whose choice differs from the topic's with its label blue
and bold. A row's options open under it, as on the launch card (2026-11-04: no select).
"""
from __future__ import annotations

from covey.cards.elements import callback_button, md, note, panel_header, plain, row, styled_panel
from covey.cards.launch_config import (model_settings, redaction_setting, run_mode_setting, setting_row, settings_block,
                                                workspace_setting)
from covey.core.menu_choice import harness_of
from covey.core.defaults import work_mode_tag
from covey.core.workspaces import RUN_MODES

HARNESS = "bridge_switch_harness"
PROVIDER = "bridge_switch_provider"
MODEL = "bridge_switch_model"
BUDGET = "bridge_switch_budget"
WORK_MODE = "bridge_switch_work_mode"
WORKSPACE = "bridge_switch_workspace"
RUN_MODE = "bridge_switch_run_mode"
REDACTION = "bridge_switch_redaction"
CONFIRM = "bridge_switch_confirm"
CANCEL = "bridge_switch_cancel"
PAGE = "bridge_switch_page"  # A row's tap on a card sent with options pages (84caa8a): that row drawn open.
# What a switch does, as its card says it: the handoff's details (the transcript file, HANDOFF_ROUNDS
# rounds in plain text, app/switch_handoff.py) are the model's to know, not the card's (T194).
SWITCH_NOTE = ("换 Harness、模型、预算、工作区或运行方式会开新会话：新会话拿到交接摘要和最近的问答，完整记录放在文件里，需要时自己去读；"
               "旧会话没答的问题会取消。只改敏感词脱敏时会话不变。")
UNCHANGED = "先改一项再点确认更换"
# A model that cannot redact, chosen on a redacting topic, turns 脱敏 off on the card (the owner, 2026-11-04 17:13:
# 当前切换不了Harness — only Claude was listed, and the reason sat in small print at the bottom).
AUTO_OFF_NOTE = "所选模型不能脱敏：确认更换后本话题关闭敏感词脱敏，之后各轮把原文发给模型服务。再选回 Claude 会重新打开。"
OFF_TITLE = "关闭敏感词脱敏？"
OFF_TEXT = "关闭后，之后各轮会把原文发给模型服务；之前各轮不受影响。"


def offered(form):
    """The models the card offers: all of them, each naming its Harness (a record from before the Harness select: its
    route key's). One that cannot redact turns a chosen 脱敏 off (app/harness_switch.py, AUTO_OFF_NOTE); until
    2026-11-04 a redacting topic offered only those that redact."""
    return {key: {"harness": key.partition(":")[0], **entry} for key, entry in form["models"].items()}


def choice_redacts(form):
    return bool(form["models"].get(form.get("selected_model"), {}).get("redaction"))


def place(workspace, run_mode):
    return f"{workspace}（{RUN_MODES.get(run_mode, run_mode)}）"


def current_line(form):
    current = form["current"]
    mode = work_mode_tag(current.get("work_mode"))
    return (f"当前：{current['label']} · 思考预算 {current.get('effort') or '模型默认'}" + (f" · {mode}" if mode else "")
            + f"\n工作区 {place(current.get('workspace') or form.get('workspace') or '', current.get('run_mode') or 'direct')}"
            + f" · 敏感词脱敏{'开' if current.get('redaction') else '关'}")


def changes(form):
    """The settings-list labels whose choice differs from the topic's binding, place and redaction."""
    current, models = form["current"], form["models"]
    now, was = models.get(form.get("selected_model"), {}), models.get(current.get("key"), {})
    harness_now, harness_was = harness_of(form.get("selected_model") or "", now), harness_of(current.get("key") or "", was)
    differs = {
        "Harness": harness_now != harness_was,
        "供应商": (harness_now, now.get("provider", "")) != (harness_was, was.get("provider", "")),
        "模型": form.get("selected_model") != current.get("key"),
        "思考预算": form.get("selected_effort") != current.get("budget"),
        "工作模式": (form.get("selected_work_mode") or "") != (current.get("work_mode") or ""),
        "脱敏": (form.get("selected_redaction") == "on") != bool(current.get("redaction")),
        "工作区": form.get("selected_workspace") != current.get("workspace"),
        "运行方式": form.get("selected_run_mode") != current.get("run_mode"),
    }
    return frozenset(label for label, changed in differs.items() if changed)


def unchanged(form):
    """Nothing chosen differs from the topic's binding, place and redaction."""
    return not (changes(form) - {"Harness", "供应商"})  # Those two change with the model.


def switch_settings(form):
    """The card's settings rows, as the record chose them."""
    model = form["selected_model"]
    choices = offered(form)
    choice = form["models"].get(model, {})
    settings = []
    if choices and model in choices:
        settings += model_settings(choices, model, kinds=(HARNESS, PROVIDER, MODEL, BUDGET, WORK_MODE),
                                   revision=form["config_revision"], effort=form.get("selected_effort", ""),
                                   work_mode=form.get("selected_work_mode", ""))
    if choice.get("redaction"):
        settings.append(redaction_setting("redaction", REDACTION, form.get("selected_redaction") or "off"))
    # compat(ecb0458): a record from before the workspace select names only the topic's workspace; retire when no
    # switch-forms record lacks selected_workspace (compat_report: switch-form-fields)
    workspace = form.get("selected_workspace") or form.get("workspace") or ""
    names = form.get("workspaces") or [workspace]
    settings += [workspace_setting("workspace", WORKSPACE, workspace, names),
                 run_mode_setting("run_mode", RUN_MODE, form.get("selected_run_mode") or "direct")]
    return settings


def build_switch_card(form):
    """The 🔁 换配置 card; ``form`` is the switch record, its ``page`` the row drawn open."""
    token, model = form["token"], form["selected_model"]
    choices = offered(form)
    changed = changes(form)
    settings = switch_settings(form)
    subtitle = (f"话题 {form['topic']} · " if form.get("topic") else "") + "下一条消息起生效"
    elements = [note(current_line(form))]
    if not choices or model not in choices:
        elements.append(md("模型目录准备中，请稍后重新打开换配置。"))
    # Its token stays the same across redraws: the revision makes each redraw's rows new, so a chosen row closes.
    stamp = f"r{form['config_revision'] % 10000}"
    elements.append(settings_block([setting_row(setting, token=token, open_key=form.get("page") or "",
                                                changed=setting.label in changed, stamp=stamp) for setting in settings]))
    if form["current"].get("redaction") and form.get("selected_redaction") == "off" and not choice_redacts(form):
        elements.append(note(AUTO_OFF_NOTE))
    if form.get("active_task"):
        elements.append(note("任务还在排队或执行时不能更换：先在任务卡上停止，停止完成后再确认。"))
    elements.append(note(SWITCH_NOTE))
    if form.get("notice"):
        elements.append(md(form["notice"]))
    elif unchanged(form):
        elements.append(note(UNCHANGED))
    # Greyed out until a choice changes (the 2026-11-03 client check: a blue 确认更换 over 先改一项再点确认更换);
    # a click on an older card is still refused with UNCHANGED (app/binding.py).
    confirm = callback_button("确认更换", {"kind": CONFIRM, "token": token}, style="primary_filled", width="fill",
                              disabled=True if unchanged(form) else None)
    if form["current"].get("redaction") and form.get("selected_redaction") == "off":
        confirm["confirm"] = {"title": plain(OFF_TITLE), "text": plain(OFF_TEXT)}
    elements.append(row((confirm, 1), (callback_button("取消", {"kind": CANCEL, "token": token}, style="default",
                                                       width="fill"), 1)))
    return styled_panel(panel_header("switch", "换配置", subtitle), elements, "换配置 · Harness、模型、工作区、脱敏")

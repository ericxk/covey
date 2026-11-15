"""Change a topic's Harness, provider, model, workspace, run mode or 敏感词脱敏 mid-conversation (🔁 换配置).

Task and question cards offer 🔁 换配置, which opens the switch card (cards/switch_form.py; a message of
换配置 is an ordinary one): Harness, provider and model selects, the budget (and its work mode), the
workspace and run mode, 敏感词脱敏, then 确认更换 or 取消. Confirming a new model, budget, work mode or place
rebinds the topic under a new binding revision; a new place is another configured workspace or the other
run mode, a worktree made then from the workspace's current commit (the old one stays). Pending questions of
the old binding are cancelled, and cards of earlier jobs can no longer start jobs. 敏感词脱敏 alone only
sets the topic's choice: each request goes through the relay whole, so the session goes on.

This module keeps the card's record and handles its clicks; the rebind itself and its checks are in
app/binding.py, and the next run's handoff (recent rounds and a transcript file) in app/switch_handoff.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from covey.app.binding import (REDACTION_ONLY, STALE_CARD, active_task, current_binding_card, run_mode_of,
                                        switch_topic)
from covey.app.card_queue import CardActionQueue
from covey.app.switch_handoff import tagged
from covey.cards import switch_form
from covey.cards.elements import notice_card
from covey.cards.launch_config import PAGES
from covey.core.menu_choice import budget_effort, chosen_work_mode, effort_options, parse_budget, tier_pick
from covey.cards.switch_form import (BUDGET, CANCEL, CONFIRM, HARNESS, MODEL, PAGE, PROVIDER, REDACTION,
                                              RUN_MODE, WORK_MODE, WORKSPACE, build_switch_card)
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json
from covey.core import devices
from covey.core.workspaces import (RUN_MODES, add_topic_worktree, load_config, plan_topic_worktree, resolve_binding,
                                    visible_workspaces)
from covey.app.card_actions import action_value
from covey.cards.topic_actions import SWITCH_OPEN
from covey.app.launch_models import available_models, catalog_changed, run_choice
from covey.app.preselection import switch_selection
from covey.app import model_usage
from covey.core.topics import topic_label
from covey.app.form_cards import click_token, publish_form, refused, replace_form_card

OPEN = SWITCH_OPEN
SELECTS = frozenset({HARNESS, PROVIDER, MODEL, BUDGET, WORK_MODE, WORKSPACE, RUN_MODE, REDACTION})
# Recorded on their own; the others choose the model (chosen_model). Either way the card is redrawn: its rows
# mark what differs from the topic and 确认更换 is greyed out while nothing does (a budget alone left it grey).
RECORDED = frozenset({BUDGET, WORK_MODE, WORKSPACE, RUN_MODE})
KINDS = frozenset({OPEN, CONFIRM, CANCEL, PAGE} | SELECTS)
REBUILD_FAILED = "选项没有刷新成功，卡片上的选项可能还是之前的。请重新选一次，再点确认更换。"


def done_text(record, choice, effort, mode, place, redaction, was, switched):
    """The 已换配置 card: each change, from what to what, and what the next message gets."""
    current = record["current"]
    lines = ["**🔁 已换配置**" + (f" · 话题 {record['topic']}" if record.get("topic") else "")]
    if switched:
        before, after = switched
        if [before[k] for k in ("harness_id", "model", "effort", "work_mode")] != [
                after[k] for k in ("harness_id", "model", "effort", "work_mode")]:
            lines.append(f"模型：{current['label']}{tagged(current.get('work_mode'))} → "
                         f"{choice['label']} · 思考预算 {effort or '模型默认'}{tagged(mode)}")
        if place:
            lines.append(f"工作区：{switch_form.place(current['workspace'], current['run_mode'])} → "
                         f"{switch_form.place(place['workspace'], place['run_mode'])}"
                         + (f"，分支 {place['branch']}" if place.get("branch") else ""))
            if current["run_mode"] == "worktree":
                lines.append(f"原来的独立 worktree 还在 {current.get('workdir')}，其中的改动不会自动带过来。")
        else:
            lines.append(f"工作区沿用：{switch_form.place(current.get('workspace') or record.get('workspace') or '', current.get('run_mode') or 'direct')}")
    if redaction != was:
        lines.append("敏感词脱敏：关 → 开（之前各轮已经原样发出过）" if redaction else "敏感词脱敏：开 → 关")
    lines.append(("下一条消息由新会话接手：它拿到交接摘要和最近的问答，完整记录放在文件里，需要时自己去读。"
                  "更换前的任务卡只能查看，不能再重试、回退或换配置。") if switched
                 else "会话不变，下一条消息起生效。")
    return "\n".join(lines)


class HarnessSwitch(CardActionQueue):
    """The 🔁 换配置 button and the switch card; card writes run on a worker."""

    def __init__(self, topics):
        super().__init__(topics, name="harness-switch", workers=2, label="harness switch")

    action = staticmethod(action_value)

    def matches(self, event):
        return self.action(event).get("kind") in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is 换配置's, else None (app/unified.py EARLY)."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return "harness_switch"

    def path(self, ctx, token):
        return ctx.state_dir / "switch-forms" / (token + ".json")

    @staticmethod
    def save(path, record):
        write_json(path, record, ensure_ascii=False)

    def topic_context(self, ctx, topic_id, chat):
        """The topic's current app, context and topic.json; Nones when it cannot be routed."""
        return self.topics.target(ctx.state_dir, topic_id, chat) or (None, None, None)

    @staticmethod
    def tell(app, target, chat, text, tag, click):
        app.messenger.reply(target, chat, text, app.key("switch-" + tag, click), voice=app.voice)

    def tell_topic(self, ctx, topic_id, chat, text, tag, click):
        app, target, _ = self.topic_context(ctx, topic_id, chat)
        if app is not None:
            self.tell(app, target, chat, text, tag, click)

    def dropped(self, ctx, event, topic_id, text):
        """A click the full queue dropped still gets an answer, never silence."""
        chat = str(event.get("chat_id") or "")
        self.tell_topic(ctx, topic_id, chat, text, "dropped", str(event.get("event_id") or event.get("message_id") or ""))

    def dispatch(self, ctx, event):
        raw, chat = self.action(event), str(event.get("chat_id") or "")
        kind = raw.get("kind")
        if kind == OPEN:
            if not self.submit("switch card not opened", self.open, ctx, event, raw):
                self.dropped(ctx, event, raw.get("topic_id"), "换配置卡片暂时没有打开（处理队列已满），请稍后再点一次“换配置”。")
            return
        token = raw.get("token")
        if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{24}", token):
            return
        path = self.path(ctx, token)
        if kind in SELECTS or kind == PAGE:
            with resource_lock("switch-form", path):
                try:
                    record = read_json(path)
                except (OSError, ValueError):
                    return
                if (record.get("status") != "ready" or record.get("chat_id") != chat
                        or record.get("message_id") != event.get("message_id")):
                    return
                # A row's tap (a card sent with options pages) draws that row open (‹ 返回: none); a choice there, the current one or one
                # no longer offered too, returns to the list. A select (a card sent before) has no page.
                page = record.get("page") or ""
                option = raw.get("page") if kind == PAGE else event.get("option")
                if not isinstance(option, str):
                    return
                if kind == PAGE:
                    if option == page or option not in PAGES:
                        return
                    key = record["selected_model"]
                elif kind in RECORDED:
                    # A budget from a card a redraw is replacing names the old revision and is refused.
                    if not self.record_choice(record, kind, option) and not page:
                        return
                    key = record["selected_model"]
                else:
                    if kind == REDACTION:
                        if option in ("on", "off") and option != record.get("selected_redaction"):
                            record["selected_redaction"] = option
                            record.pop("redaction_auto_off", None)  # The owner's own choice from here.
                        elif not page:
                            return
                    key = self.chosen_model(record, kind, option)
                    followed = False
                    if kind != REDACTION:  # A model's choice, or its own again: 脱敏 follows what it can do.
                        was = record.get("selected_redaction")
                        self.follow_redaction(record, key or record["selected_model"])
                        followed = record.get("selected_redaction") != was
                    if key is None:
                        if not page and not followed:
                            return
                        key = record["selected_model"]
                revision = record["config_revision"] + 1
                # A refusal answered the previous choice; the rebuilt card drops it.
                record.update(config_pending=True, config_revision=revision, notice="", page=option if kind == PAGE else "",
                              selected_effort=(record.get("selected_effort") if key == record["selected_model"]
                                               else self.effort_for(record, key)), selected_model=key)
                self.save(path, record)
            if not self.submit("switch card model not updated", self.rebuild, ctx, path, revision):
                self.dropped(ctx, event, record["topic_id"], REBUILD_FAILED)
        elif kind in (CONFIRM, CANCEL):
            if not self.submit("switch not confirmed", self.finish, ctx, event, path, kind == CONFIRM):
                try:
                    topic_id = read_json(path).get("topic_id")
                except (OSError, ValueError):
                    return
                self.dropped(ctx, event, topic_id, "这次点击没有处理（处理队列已满），话题配置没有改变，请稍后再点一次。")

    @staticmethod
    def record_choice(record, kind, option):
        """Record a budget, work mode, workspace or run mode the card's select chose; whether it was one of its
        options. A model without work modes ignores a recorded one at confirmation."""
        choice = record["models"][record["selected_model"]]
        if kind == WORK_MODE and option in (choice.get("work_modes") or ()):
            record["selected_work_mode"] = option
        elif kind == BUDGET and (budget := parse_budget(option, record["config_revision"], choice)) is not None:
            record["selected_effort"] = budget
        elif kind == WORKSPACE and option in (record.get("workspaces") or ()):
            record["selected_workspace"] = option
        elif kind == RUN_MODE and option in RUN_MODES:
            record["selected_run_mode"] = option
        else:
            return False
        return True

    @staticmethod
    def follow_redaction(record, key):
        """A model that cannot redact turns a chosen 脱敏 off (confirmed with 确认更换's OFF_TITLE); choosing one
        that can again turns it back on, unless the owner set 脱敏 since. The launch card does not: it keeps the
        choice, draws no 脱敏 row for such a model, and starts it without redaction (app/launch.py plan)."""
        redacts = bool(record["models"].get(key, {}).get("redaction"))
        if record.get("selected_redaction") == "on" and not redacts:
            record.update(selected_redaction="off", redaction_auto_off=True)
        elif record.get("redaction_auto_off") and redacts:
            record["selected_redaction"] = "on"
            record.pop("redaction_auto_off")

    @staticmethod
    def chosen_model(record, kind, option):
        """The model a Harness, provider, model or 敏感词脱敏 choice selects: the topic's own where it is among
        those the choice leaves, else the first of them (the chat's latest first); None when it changes nothing."""
        offered, prefer = switch_form.offered(record), record["current"]["key"]
        if kind == HARNESS:
            return tier_pick(offered, record["selected_model"], harness=option, prefer=prefer)
        if kind == PROVIDER:
            # The selected entry's own Harness field: one from before the Harness select has none, so no models.
            harness = record["models"].get(record["selected_model"], {}).get("harness") or ""
            return tier_pick(offered, record["selected_model"], harness=harness, provider=option, prefer=prefer)
        if kind == MODEL:
            pool = [option] if option in offered else []
        else:  # 敏感词脱敏 redraws the card; turned on, a model that cannot redact gives way.
            redacting = [key for key, entry in offered.items() if entry.get("redaction")]
            on = record.get("selected_redaction") == "on"
            pool = [record["selected_model"]] if not on or record["selected_model"] in redacting else redacting
        if not pool:
            return None
        key = prefer if prefer in pool else pool[0]
        return None if key == record["selected_model"] and kind != REDACTION else key

    @staticmethod
    def effort_for(record, key):
        """The budget a model comes with on the card: the topic's for its own model, else the one the chat
        usually gives it, else the model's default."""
        if key == record["current"]["key"] and record["current"].get("budget"):
            return record["current"]["budget"]
        return (record.get("usual_efforts") or {}).get(key) or effort_options(record["models"][key])[1]

    def open(self, ctx, event, raw):
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        app, target, topic = self.topic_context(ctx, raw.get("topic_id"), chat)
        if app is None:
            return
        if not current_binding_card(ctx.state_dir, topic, raw, chat):
            self.tell(app, target, chat, STALE_CARD, "stale", click)
            return
        # Opening only selects. The binding changes in switch_topic,
        # which rechecks activity under LOCK when the user confirms.
        token = click_token(target.topic_id, click)
        path = self.path(ctx, token)
        with resource_lock("switch-form", path):
            if path.exists():
                return  # A redelivered click.
            choices = available_models(self.topics, target)
            if not choices:
                self.tell(app, target, chat, "模型目录准备中，请稍后再点换配置。", "catalog", click)
                return
            try:
                config = load_config(ctx.state_dir, ctx.default_workdir)
                # A device's workspaces only where its ``spaces`` say (app/access.py allows_device).
                config = visible_workspaces(config, lambda alias: self.topics.runtime.access.allows_device(chat, alias))
                names = sorted(config["workspaces"], key=lambda name: -config.get("recent", {}).get(name, 0))
            except (OSError, ValueError):
                names = []
            sel = switch_selection(choices, model_usage.recent(ctx.state_dir, chat), topic, names)
            current = choices.get(sel.key, {})
            mode, run_mode = str(topic.get("work_mode") or ""), run_mode_of(topic.get("workdir") or "")
            record = {"token": token, "topic_id": target.topic_id, "chat_id": chat, "job_id": raw.get("job_id"),
                      "harness_id": topic["harness_id"], "binding_revision": topic.get("binding_revision", 1),
                      "status": "publishing", "models": sel.choices, "selected_model": sel.selected, "config_revision": 1,
                      "selected_effort": sel.effort, "usual_efforts": sel.usual,
                      "current": {"label": current.get("label") or f"{app.spec.title} · {topic.get('model') or '默认模型'}",
                                  "effort": topic.get("effort") or "", "key": sel.key, "work_mode": mode,
                                  # What the card preselects for the topic's own model: confirming it unchanged changes nothing.
                                  "budget": sel.effort if sel.selected == sel.key else "",
                                  "workspace": sel.workspace, "run_mode": run_mode, "redaction": sel.redacting,
                                  "workdir": str(topic.get("workdir") or "")},
                      # The topic's mode is preselected while the select shows; "" is the default's.
                      "selected_work_mode": mode, "workspaces": sel.names, "selected_workspace": sel.workspace,
                      "selected_run_mode": run_mode, "selected_redaction": "on" if sel.redacting else "off",
                      "workspace": sel.workspace, "topic": topic_label(ctx.state_dir, target.topic_id),
                      "active_task": active_task(self.topics, ctx.state_dir, target.topic_id)}
            _, mid = publish_form(path, record, lambda: app.messenger.create_and_send_card(
                ctx.profile, chat, build_switch_card(record), app.key("switch-card", token),
                reply_to_message_id=target.reply_to_message_id, voice=app.voice), topic=(ctx.state_dir, target.topic_id, chat))
            if not mid:
                self.tell(app, target, chat, "换配置卡片暂未送达，话题配置没有改变；请稍后再点。", "undelivered", click)

    def replace(self, ctx, path, record, candidate, card, kind):
        return replace_form_card(ctx, path, record, candidate, card, self.topics.messenger, kind)

    def rebuild(self, ctx, path, revision):
        """A choice rebuilds the whole card (never one element): a model's with that model's budgets."""
        with resource_lock("switch-form", path):
            record = read_json(path)
            if record.get("status") != "ready" or record.get("config_revision") != revision:
                return
            candidate = {**record, "config_pending": False,
                         "active_task": active_task(self.topics, ctx.state_dir, record["topic_id"])}
            if not self.replace(ctx, path, record, candidate, build_switch_card(candidate), "switch-model"):
                self.topics.runtime.log(f"switch card model update failed token={path.stem}")
                # Choosing the model again retries; say so instead of leaving 确认 refused with no way out.
                self.tell_topic(ctx, record["topic_id"], record["chat_id"], REBUILD_FAILED,
                                "rebuild", f"{path.stem}-{revision}")

    def finish(self, ctx, event, path, confirm):
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        with resource_lock("switch-form", path):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                return
            if record.get("chat_id") != chat or record.get("message_id") != event.get("message_id"):
                return
            app, target, topic = self.topic_context(ctx, record["topic_id"], chat)
            if app is None or record.get("status") != "ready":
                return
            if not confirm:
                current = record["current"]  # Named as the card's 当前 line names it.
                text = (f"已取消换配置，话题继续使用 {current['label']} · 思考预算 {current.get('effort') or '模型默认'}"
                        f"{tagged(current.get('work_mode'))}。")
                self.replace(ctx, path, record, {**record, "status": "cancelled"}, notice_card(text, "已取消换配置"),
                             "switch-cancel")
                return
            if record.get("config_pending"):
                # Redrawing the card for the refusal also shows the chosen model's budgets.
                self.refuse(ctx, path, record, app, target, chat, click, "模型选项刚刷新，请确认思考预算后再点确认更换",
                            "模型选项还在刷新。如果卡片上的思考预算还是之前模型的，请重新选一次模型再确认")
                return
            try:
                name, model, effort, choice = self.selection(target, record)
                mode = chosen_work_mode(choice, record.get("selected_work_mode"))
                current = record["current"]
                if record["selected_model"] == current["key"] and record.get("selected_effort") == current.get("budget"):
                    # The topic's own model and budget, untouched: kept exactly (a budget preset, a native default).
                    model, effort = str(topic.get("model") or ""), str(topic.get("effort") or "")
                was = topic.get(REDACTION_KEY) is True
                # compat(ecb0458): a card from before its 敏感词脱敏 select leaves the topic's choice as it is; retire
                # when no switch-forms record lacks the select's field (compat_report: switch-form-fields)
                redaction = record["selected_redaction"] == "on" if "selected_redaction" in record else was
                if redaction and not choice.get("redaction"):
                    raise ValueError(REDACTION_ONLY)
                place = self.place(target, record)
                if isinstance(place, dict) and "top" in place:
                    record = self.make_worktree(ctx, path, record, place)
                    place = self.place(target, record)
                if isinstance(place, dict) and devices.is_device_value(place.get("workdir") or ""):
                    alias = place["workdir"].split(":", 1)[0]
                    if not self.topics.runtime.access.allows_device(chat, alias):
                        raise ValueError(f"设备 {alias} 不允许在这个聊天里使用（devices.json 的 spaces）")
                # Where the topic runs after the switch: its Harness must be one that device declares.
                where = place["workdir"] if isinstance(place, dict) else str(topic.get("workdir") or "")
                if devices.is_device_value(where):
                    refused = devices.undeclared_refusal(where.split(":", 1)[0], name, choice.get("title") or name)
                    if refused:
                        raise ValueError(refused.replace("任务尚未启动", "话题配置没有改变"))
                switched = switch_topic(self.topics, target, chat, record, name, model, effort, mode, place=place,
                                        redaction=redaction)
            except ValueError as exc:
                self.refuse(ctx, path, record, app, target, chat, click, str(exc))
                return
            if switched is not None:
                try:
                    model_usage.record(ctx.state_dir, chat, record["selected_model"], record.get("selected_effort") or "",
                                       mode, "switch", form=path.stem)
                except OSError as exc:
                    self.topics.runtime.log(f"model usage save failed: {type(exc).__name__}; the switch is done")
            text = done_text(record, choice, effort, mode, place, redaction, was, switched)
            done = {**record, "status": "done", **({"switched": {"from": switched[0], "to": switched[1]}} if switched else {})}
            if not self.replace(ctx, path, record, done, notice_card(text, "已换配置"), "switch-done"):
                # With the sequence that write reserved (record holds it), never the one before it (rule 3).
                self.save(path, {**done, "sequence": record["sequence"]})
                self.tell(self.topics.apps[name], target, chat, text, "done", click)

    @staticmethod
    def place(ctx, record):
        """Where the topic runs after the switch: None for where it runs now; {workspace, workdir, run_mode,
        branch} for another place; a worktree plan ({top, branch, dest, subdir, workspace, source}) when the
        place is a Bridge worktree not made yet. ValueError for a workspace gone or a directory outside git."""
        current = record["current"]
        workspace, run_mode = record.get("selected_workspace"), record.get("selected_run_mode") or "direct"
        if (workspace, run_mode) == (current.get("workspace"), current.get("run_mode")):
            return None
        name, binding = resolve_binding(ctx.state_dir, ctx.default_workdir, workspace)
        source = binding.workdir
        if binding.device:
            # The switch card was drawn for this chat (names above); a click on an older card is checked again here.
            if run_mode != "direct":
                raise ValueError("设备上的工作区只能用原目录运行")
            return {"workspace": name, "workdir": binding.value, "run_mode": "direct", "branch": ""}
        if run_mode == "direct":
            return {"workspace": name, "workdir": str(source), "run_mode": "direct", "branch": ""}
        made = record.get("made_worktree") or {}
        if made.get("source") == str(source) and Path(made.get("workdir") or "/nonexistent").is_dir():
            return {"workspace": name, "workdir": made["workdir"], "run_mode": "worktree", "branch": made["branch"]}
        return {**plan_topic_worktree(source, ctx.topic_id), "workspace": name, "source": str(source)}

    def make_worktree(self, ctx, path, record, plan):
        """Check out the planned worktree (seconds to tens of seconds on the NAS) with the card saying so; the
        record, which keeps it for a confirmation again when the switch is refused after all."""
        preparing = notice_card(f"**正在创建独立 worktree…**\n从 {plan['workspace']} 的当前提交新建分支 {plan['branch']}；"
                                "大仓库可能要几十秒，完成后自动换配置。", "正在创建独立 worktree")
        self.replace(ctx, path, record, dict(record), preparing, "switch-preparing")
        record = read_json(path)
        directory, branch = add_topic_worktree(plan)
        record["made_worktree"] = {"source": plan["source"], "workdir": str(directory), "branch": branch,
                                   "dest": plan["dest"]}
        self.save(path, record)
        self.topics.runtime.log(f"switch worktree created topic={record['topic_id'][:12]} branch={branch}")
        return record

    def refuse(self, ctx, path, record, app, target, chat, click, reason, fallback=""):
        """A refused confirm is answered on the switch card, so a later success replaces it.

        A separate topic message would stay under the 已换配置 card; it is only
        the fallback when the card cannot be updated."""
        candidate = {**record, "notice": refused("还没有换", reason), "config_pending": False,
                     "active_task": active_task(self.topics, ctx.state_dir, record["topic_id"])}
        if not self.replace(ctx, path, record, candidate, build_switch_card(candidate), "switch-refused"):
            self.tell(app, target, chat, f"换配置未完成：{fallback or reason}", "refused", click)

    def selection(self, ctx, record):
        key = record["selected_model"]
        choice = record["models"].get(key)
        live = available_models(self.topics, ctx).get(key)
        if not choice or catalog_changed(choice, live):
            raise ValueError("模型或预算目录已更新，请重新点换配置后选择")
        effort = budget_effort(choice, record.get("selected_effort") or "")
        if effort and effort not in choice["levels"]:
            raise ValueError("该模型不支持此思考预算")
        return (choice["harness"], *run_choice(choice, effort), choice)

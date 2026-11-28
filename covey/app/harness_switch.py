"""Change a topic's Harness, provider, model, workspace or run mode mid-conversation (🔁 换配置).

Task and question cards offer 🔁 换配置, which opens the switch card (cards/switch_form.py; a message of
换配置 is an ordinary one): Harness, provider and model selects, the budget (and its work mode), then 确认更换 or 取消 (a card sent before the home start also has the workspace and run mode rows; it
still moves the topic: ``place``). Confirming a new model, budget, work mode or place
rebinds the topic under a new binding revision; a new place (an old card's) is another configured workspace or the other
run mode, a worktree made then from the workspace's current commit (the old one stays); a workspace on a device only for a
topic already on one (主人 D2Y4T: TO_DEVICE). Pending questions of the old binding are cancelled, and cards of earlier jobs can
no longer start jobs.

A click from a relay card at the private chat's top level (cards/topic_actions.py RELAY: a 做完了 relay's 换配置, a 停住了
relay's 换个模型, app/relay_stops.py) opens the card there, a message of its own under the relay that names the topic and
whose quote goes to it; whatever it answers goes there too, and the card closes there, in place. The record says so
(``top_level``); a click on a task or question card in the topic's thread is answered in the thread, as always. A 停住了 relay's
card is redrawn once its 换个模型 switched the topic (app/relay_stops.py switched), or when it meets a card from before the
topic's latest switch.

A project's lead (COVEY_PROJECTS=1) has no thread and no task card of its own: its 换配置 is on the 专项卡 (cards/project_card.py
``buttons``, the value naming the project, ``LEAD``). Its card is a reply to the 专项卡 at the chat's top level (a reply to
either is the lead's), lists only the models a lead can run on (app/lead_harness.py ``can_lead``: the Bridge's tools),
has no workspace or run mode rows (the lead stays where it reads), and always acts on the lead's binding as it is now; the
record says so (``lead``: the project's id and name). The rebind is a topic's (a new session, the handoff, the old
binding's questions void); the lead's next turn reads the project's state pack whole, as on any new session.

This module keeps the card's record and handles its clicks; the rebind itself and its checks are in
app/binding.py, and the next run's handoff (recent rounds and a transcript file) in app/switch_handoff.py.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.app.binding import (STALE_CARD, active_task, current_binding_card, run_mode_of,
                                        switch_topic)
from covey.app.card_queue import CardActionQueue
from covey.app.switch_handoff import tagged
from covey.cards import switch_form
from covey.cards.elements import notice_card
from covey.cards.project_card import SWITCH_LEAD
from covey.cards.launch_config import PAGES
from covey.core.menu_choice import budget_effort, chosen_work_mode, effort_options, parse_budget, named_budget, tier_pick
from covey.cards.switch_form import (BUDGET, CANCEL, CONFIRM, HARNESS, MODEL, PAGE, PROVIDER,
                                              RUN_MODE, WORK_MODE, WORKSPACE, build_switch_card)
from covey.app.menu_panel import valid_token
from covey.core.locks import resource_lock
from covey.core.state import keyed_json_path, read_json, write_json
from covey.core import devices
from covey.core.workspaces import RUN_MODES, add_topic_worktree, plan_topic_worktree, resolve_binding
from covey.app.card_actions import action_value
from covey.cards.topic_actions import PREFER_OTHER, RELAY, SWITCH_OPEN
from covey.app.launch_models import available_models, catalog_changed, run_choice
from covey.app.preselection import switch_selection
from covey.app import lead_harness, model_usage
from covey.core import projects
from covey.core.topics import route_message, topic_label
from covey.app.form_cards import click_token, publish_form, refused, replace_form_card

OPEN = SWITCH_OPEN
SELECTS = frozenset({HARNESS, PROVIDER, MODEL, BUDGET, WORK_MODE, WORKSPACE, RUN_MODE})
# Recorded on their own; the others choose the model (chosen_model). Either way the card is redrawn: its rows
# mark what differs from the topic and 确认更换 is greyed out while nothing does (a budget alone left it grey).
RECORDED = frozenset({BUDGET, WORK_MODE, WORKSPACE, RUN_MODE})
KINDS = frozenset({OPEN, CONFIRM, CANCEL, PAGE} | SELECTS)
# An older card's workspace on a device for a topic here (主人 D2Y4T): refused, as the page's 换配置 refuses it (app/web_switch.py).
TO_DEVICE = "换配置不能把话题换到设备上：执行设备入口已下线，话题配置没有改变；要用别的设备，在话题里用 covey device"
REBUILD_FAILED = "选项没有刷新成功，卡片上的选项可能还是之前的。请重新选一次，再点确认更换。"
NO_OTHER_HARNESS = "现在没有别的 Harness 能接着做（没启用、刚报过限流或额度用完）；可以稍后再试，或点「换个模型」。"
# What a switch confirmed at the top level says comes next: a top-level message is the butler's, a reply to this card the topic's.
TOP_NEXT = "回复这张卡说一句，就由新会话接着做"
LEAD = SWITCH_LEAD  # In a 换配置 click from a 专项卡: the project whose lead it changes (cards/project_card.py ``buttons``).
# What a lead's switch says comes next: its next turn (a wake-up, or the owner's words) starts a new session.
LEAD_NEXT = ("负责人下一轮起由新会话接着做：它重新读一遍专项的状态，最近几轮对话一起带上，仍然只读。"
             "引用专项卡或这张卡回复，就是对负责人说。")
NO_LEAD_MODEL = "现在没有能当负责人的模型：要能接 Bridge 工具的 Harness（设置卡「配置」里管家一段能选的那些），在 harnesses.json 里启用一个。"


def other_harness_key(topics, state_dir, sel, topic) -> str:
    """The menu entry 换个 Harness preselects (#36): the first in the card's order (the chat's recent ones first,
    app/preselection.py switch_selection) on another Harness than the topic's that can take a task now
    (app/availability.py usable); '' for none. The owner still confirms the switch."""
    from covey.app import availability
    from covey.core.menu_choice import harness_of

    usable = {}
    for key, entry in sel.choices.items():
        name = harness_of(key, entry)
        if name == topic.get("harness_id"):
            continue
        if name not in usable:
            usable[name] = availability.usable(topics.runtime, state_dir, name)
        if usable[name]:
            return key
    return ""


def done_text(record, choice, effort, mode, place, switched):
    """The 已换配置 card: each change, from what to what, and what the next message gets (a card at the private chat's top
    level, ``top_level``: a reply to it, as a top-level message is the butler's)."""
    current, lead = record["current"], record.get("lead") or {}
    lines = ["**🔁 已换配置**" + (f" · 专项「{lead.get('name') or '专项'}」的负责人" if lead
                                  else f" · 话题 {record['topic']}" if record.get("topic") else "")]
    if switched:
        before, after = switched
        if [before[k] for k in ("harness_id", "model", "effort", "work_mode")] != [
                after[k] for k in ("harness_id", "model", "effort", "work_mode")]:
            lines.append(f"模型：{current['label']}{tagged(current.get('work_mode'))} → "
                         f"{choice['label']} · 思考预算 {effort or '模型默认'}{tagged(mode)}")
        if lead or record.get("no_place"):
            pass  # The lead stays where it reads; a card drawn since the home start has no workspace row either.
        elif place:
            lines.append(f"工作区：{switch_form.place(current['workspace'], current['run_mode'])} → "
                         f"{switch_form.place(place['workspace'], place['run_mode'])}"
                         + (f"，分支 {place['branch']}" if place.get("branch") else ""))
            if current["run_mode"] == "worktree":
                lines.append(f"原来的独立 worktree 还在 {current.get('workdir')}，其中的改动不会自动带过来。")
        else:
            lines.append(f"工作区沿用：{switch_form.place(current.get('workspace') or record.get('workspace') or '', current.get('run_mode') or 'direct')}")
    top = bool(record.get("top_level"))
    if lead:
        lines.append(LEAD_NEXT if switched else "会话不变，负责人下一轮起生效。")
        return "\n".join(lines)
    lines.append(((TOP_NEXT if top else "下一条消息由新会话接手") + "，上下文不断：它拿到交接摘要和最近的问答，"
                  "完整记录放在文件里，需要时自己去读；交接了什么写在下一张任务卡上。"
                  "更换前的任务卡只能查看，不能再重试、回退或换配置。") if switched
                 else "会话不变，话题的下一轮起生效。" if top else "会话不变，下一条消息起生效。")
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
        return keyed_json_path(ctx.state_dir, "switch-forms", token)

    @staticmethod
    def save(path, record):
        write_json(path, record, ensure_ascii=False)

    def topic_context(self, ctx, topic_id, chat, project=""):
        """The topic's current app, context and topic.json; Nones when it cannot be routed. ``project``: the topic is that
        project's lead (``lead_context``)."""
        if project:
            return self.lead_context(ctx, topic_id, chat, project)
        return self.topics.target(ctx.state_dir, topic_id, chat) or (None, None, None)

    def lead_context(self, ctx, topic_id, chat, project):
        """(app, context, topic.json) of project ``project``'s lead ``topic_id`` in ``chat`` (app/lead_harness.py ``target``:
        the context that of the 专项卡's message); Nones when it is not that project's lead in this chat."""
        found = lead_harness.target(self.topics, ctx.state_dir, topic_id, chat, str(project))
        return found[:3] if found else (None, None, None)

    @staticmethod
    def here(target, top):
        """Where the switch answers: the topic's thread (``target``, its context), or the private chat's top level for a
        click from a relay card there (``top``), a message of its own that is still the topic's (Messenger.reply)."""
        return dataclasses.replace(target, reply_to_message_id="") if top else target

    @staticmethod
    def tell(app, target, chat, text, tag, click, top=False):
        app.messenger.reply(HarnessSwitch.here(target, top), chat, text, app.key("switch-" + tag, click), voice=app.voice)

    def tell_topic(self, ctx, topic_id, chat, text, tag, click, top=False, project=""):
        app, target, _ = self.topic_context(ctx, topic_id, chat, project)
        if app is not None:
            self.tell(app, target, chat, text, tag, click, top)

    def dropped(self, ctx, event, topic_id, text, top=False, project=""):
        """A click the full queue dropped still gets an answer, never silence."""
        chat = str(event.get("chat_id") or "")
        self.tell_topic(ctx, topic_id, chat, text, "dropped", str(event.get("event_id") or event.get("message_id") or ""),
                        top, project)

    def dispatch(self, ctx, event):
        raw, chat = self.action(event), str(event.get("chat_id") or "")
        kind = raw.get("kind")
        if kind == OPEN:
            if not self.submit("switch card not opened", self.open, ctx, event, raw):
                self.dropped(ctx, event, raw.get("topic_id"), "换配置卡片暂时没有打开（处理队列已满），请稍后再点一次“换配置”。",
                             bool(raw.get(RELAY)), str(raw.get(LEAD) or ""))
            return
        token = raw.get("token")
        if not valid_token(token):
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
                    key = self.chosen_model(record, kind, option)
                    if key is None:
                        if not page:
                            return
                        key = record["selected_model"]
                revision = record["config_revision"] + 1
                # A refusal answered the previous choice; the rebuilt card drops it.
                record.update(config_pending=True, config_revision=revision, notice="", page=option if kind == PAGE else "",
                              selected_effort=(record.get("selected_effort") if key == record["selected_model"]
                                               else self.effort_for(record, key)), selected_model=key)
                self.save(path, record)
            if not self.submit("switch card model not updated", self.rebuild, ctx, path, revision):
                self.dropped(ctx, event, record["topic_id"], REBUILD_FAILED, bool(record.get("top_level")), lead_of(record))
        elif kind in (CONFIRM, CANCEL):
            if not self.submit("switch not confirmed", self.finish, ctx, event, path, kind == CONFIRM):
                try:
                    record = read_json(path)
                except (OSError, ValueError):
                    return
                self.dropped(ctx, event, record.get("topic_id"), "这次点击没有处理（处理队列已满），话题配置没有改变，请稍后再点一次。",
                             bool(record.get("top_level")), lead_of(record))

    @staticmethod
    def record_choice(record, kind, option):
        """Record a budget, work mode, workspace or run mode the card's select chose; whether it was one of its
        options. A model without work modes ignores a recorded one at confirmation."""
        choice = record["models"][record["selected_model"]]
        if kind == WORK_MODE and option in (choice.get("work_modes") or ()):
            record["selected_work_mode"] = option
        elif kind == BUDGET and (budget := parse_budget(option, record["config_revision"], choice)) is not None:
            record["selected_effort"] = budget  # 推荐 or 模型原生默认 tapped on a card sent before stays as tapped.
        elif record.get("no_place") and kind in (WORKSPACE, RUN_MODE):
            return False  # A card drawn since the home start has neither row: a stale tap moves nothing.
        elif kind == WORKSPACE and option in (record.get("workspaces") or ()):
            record["selected_workspace"] = option
        elif kind == RUN_MODE and option in RUN_MODES:
            record["selected_run_mode"] = option
        else:
            return False
        return True

    @staticmethod
    def chosen_model(record, kind, option):
        """The model a Harness, provider or model choice selects: the topic's own where it is among
        those the choice leaves, else the first of them (the chat's latest first); None when it changes nothing."""
        offered, prefer = switch_form.offered(record), record["current"]["key"]
        if kind == HARNESS:
            return tier_pick(offered, record["selected_model"], harness=option, prefer=prefer)
        if kind == PROVIDER:
            # The selected entry's own Harness field: one from before the Harness select has none, so no models.
            harness = record["models"].get(record["selected_model"], {}).get("harness") or ""
            return tier_pick(offered, record["selected_model"], harness=harness, provider=option, prefer=prefer)
        pool = [option] if kind == MODEL and option in offered else []
        if not pool:
            return None
        key = prefer if prefer in pool else pool[0]
        return None if key == record["selected_model"] else key

    @staticmethod
    def effort_for(record, key):
        """The budget a model comes with on the card: the topic's for its own model, else the one the chat
        usually gives it, else the model's default."""
        if key == record["current"]["key"] and record["current"].get("budget"):
            return record["current"]["budget"]  # The topic's own, as it is bound.
        # By the level it runs (docs/ui.md 13 U5): the card lists no 推荐 or 模型原生默认.
        return named_budget(record["models"][key], (record.get("usual_efforts") or {}).get(key)
                            or effort_options(record["models"][key])[1])

    def open(self, ctx, event, raw):
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        project = str(raw.get(LEAD) or "")  # A 专项卡's 换配置: its project's lead, as it is now (no binding to check).
        app, target, topic = self.topic_context(ctx, raw.get("topic_id"), chat, project)
        if app is None:
            if project:  # Not this chat's lead, projects off, no card, or the lead's Harness is not enabled here.
                self.topics.runtime.log(f"lead switch not opened project={project[:8]}: no lead to switch here")
            return
        top = bool(raw.get(RELAY))  # A relay card's, at the private chat's top level: answered there.
        if project and not self.topics.runtime.access.is_owner(str(event.get("operator_id") or "")):
            self.tell(app, target, chat, "只有主人能换专项负责人的配置。", "owner", click)
            return
        if not project and not current_binding_card(ctx.state_dir, topic, raw, chat):
            # A 停住了 relay's 换个模型 says so on its own card, redrawn (as its 检查并继续, app/card_routing.py job_route).
            if not (top and self.relay_redrawn(ctx, app, raw, chat)):
                self.tell(app, target, chat, STALE_CARD, "stale", click, top)
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
                self.tell(app, target, chat, "模型目录准备中，请稍后再点换配置。", "catalog", click, top)
                return
            if project:  # Only the models a lead can run on; the workspace it reads in stays.
                choices = lead_harness.lead_choices(choices)
                if not choices:
                    self.tell(app, target, chat, NO_LEAD_MODEL, "no-lead-model", click)
                    return
            # No workspace or run mode to choose: the card is drawn without those rows (``no_place`` in its record).
            names = [str(topic.get("workspace_name") or "")]
            sel = switch_selection(choices, model_usage.recent(ctx.state_dir, chat), topic, names)
            # 换个 Harness on a limit's failure (cards/recovery.py OTHER_HARNESS, #36), and a 停住了 relay's 换个模型
            # (PREFER_OTHER), which opens on the topic's own model when no other Harness can take it now.
            if raw.get("other") or raw.get(PREFER_OTHER):
                picked = other_harness_key(self.topics, ctx.state_dir, sel, topic)
                if not picked and raw.get("other"):
                    self.tell(app, target, chat, NO_OTHER_HARNESS, "no-other", click, top)
                    return
                if picked:
                    sel = sel._replace(selected=picked, effort=named_budget(
                        choices[picked], sel.usual.get(picked) or effort_options(choices[picked])[1]))
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
                                  "workspace": sel.workspace, "run_mode": run_mode,
                                  "workdir": str(topic.get("workdir") or "")},
                      # The topic's mode is preselected while the select shows; "" is the default's.
                      "selected_work_mode": mode, "no_place": True,
                      "workspace": sel.workspace, "topic": topic_label(ctx.state_dir, target.topic_id),
                      "active_task": active_task(self.topics, ctx.state_dir, target.topic_id),
                      # Opened from a relay card at the top level: it goes there, and so does what it answers.
                      **({"top_level": True} if top else {}),
                      # Opened from a 专项卡: the project's lead (no workspace rows; its words name the project).
                      **({"lead": lead_harness.named(projects.read_record(ctx.state_dir, project))} if project else {})}
            _, mid = publish_form(path, record, lambda: app.messenger.create_and_send_card(
                ctx.profile, chat, build_switch_card(record), app.key("switch-card", token),
                reply_to_message_id=self.here(target, top).reply_to_message_id, voice=app.voice),
                topic=(ctx.state_dir, target.topic_id, chat))
            if mid and (top or project):
                # The topic's message at the top level, as its relay is (app/relay_send.py remember_sent): a quote of it
                # goes to the topic, so a reply to its 已换配置 card is the topic's next turn (a lead's: as a reply to the 专项卡).
                route_message(ctx.state_dir, chat, mid, target.topic_id)
            if not mid:
                self.tell(app, target, chat, "换配置卡片暂未送达，话题配置没有改变；请稍后再点。", "undelivered", click, top)

    def relay_redrawn(self, ctx, app, raw, chat) -> bool:
        """A 停住了 relay's card, redrawn from the records for its button's click (app/relay_stops.py redraw); whether it was
        written. Never raises: the owner is told by a message instead."""
        from covey.app import relay_stops

        try:
            return relay_stops.redraw(ctx.state_dir, app.messenger, ctx.profile, raw.get("topic_id"), chat,
                                      raw.get("job_id"))
        except Exception as exc:  # noqa: BLE001 - said by a message instead
            self.topics.runtime.log(f"stopped relay card not redrawn for a switch: {type(exc).__name__}")
            return False

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
                                "rebuild", f"{path.stem}-{revision}", bool(record.get("top_level")), lead_of(record))

    def finish(self, ctx, event, path, confirm):
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        with resource_lock("switch-form", path):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                return
            if record.get("chat_id") != chat or record.get("message_id") != event.get("message_id"):
                return
            app, target, topic = self.topic_context(ctx, record["topic_id"], chat, lead_of(record))
            if app is None or record.get("status") != "ready":
                return
            if not confirm:
                current = record["current"]  # Named as the card's 当前 line names it.
                text = (f"已取消换配置，{'负责人' if lead_of(record) else '话题'}继续使用 {current['label']} · 思考预算 "
                        f"{current.get('effort') or '模型默认'}"
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
                if lead_of(record) and not lead_harness.can_lead(choice):
                    raise ValueError(lead_harness.CANNOT_LEAD)
                place = None if lead_of(record) else self.place(target, record)  # A lead stays where it reads.
                if isinstance(place, dict) and "top" in place:
                    record = self.make_worktree(ctx, path, record, place)
                    place = self.place(target, record)
                if isinstance(place, dict) and devices.is_device_value(place.get("workdir") or ""):
                    if not devices.is_device_value(str(topic.get("workdir") or "")):
                        # An older card's workspace on a device (主人 D2Y4T): only a topic already on one moves there.
                        raise ValueError(TO_DEVICE)
                    alias = place["workdir"].split(":", 1)[0]
                    if not self.topics.runtime.access.allows_device(chat, alias):
                        raise ValueError(f"设备 {alias} 不允许在这个聊天里使用（devices.json 的 spaces）")
                # Where the topic runs after the switch: its Harness must be one that device declares.
                where = place["workdir"] if isinstance(place, dict) else str(topic.get("workdir") or "")
                if devices.is_device_value(where):
                    refused = devices.undeclared_refusal(where.split(":", 1)[0], name, choice.get("title") or name)
                    if refused:
                        raise ValueError(refused.replace("任务尚未启动", "话题配置没有改变"))
                switched = switch_topic(self.topics, target, chat, record, name, model, effort, mode, place=place)
            except ValueError as exc:
                reason = str(exc)
                if lead_of(record):  # A lead has no task card: its 换配置 is on the 专项卡.
                    reason = reason.replace("请在最新的任务卡上重新打开换配置", "请在专项卡上重新点「换配置」")
                self.refuse(ctx, path, record, app, target, chat, click, reason)
                return
            if switched is not None:
                try:
                    model_usage.record(ctx.state_dir, chat, record["selected_model"], record.get("selected_effort") or "",
                                       mode, "switch", form=path.stem)
                except OSError as exc:
                    self.topics.runtime.log(f"model usage save failed: {type(exc).__name__}; the switch is done")
            text = done_text(record, choice, effort, mode, place, switched)
            done = {**record, "status": "done", **({"switched": {"from": switched[0], "to": switched[1]}} if switched else {})}
            if not self.replace(ctx, path, record, done, notice_card(text, "已换配置"), "switch-done"):
                # With the sequence that write reserved (record holds it), never the one before it (rule 3).
                self.save(path, {**done, "sequence": record["sequence"]})
                self.tell(self.topics.apps[name], target, chat, text, "done", click, bool(record.get("top_level")))
        if switched is not None and record.get("top_level") and not lead_of(record):
            # A 停住了 relay whose 换个模型 opened this card: its buttons become one line (outside the form's lock).
            from covey.app import relay_stops

            relay_stops.switched(ctx.state_dir, self.topics.messenger, ctx.profile, record, choice.get("label") or "")

    @staticmethod
    def place(ctx, record):
        """Where the topic runs after the switch: None for where it runs now; {workspace, workdir, run_mode,
        branch} for another place; a worktree plan ({top, branch, dest, subdir, workspace, source}) when the
        place is a Bridge worktree not made yet. ValueError for a workspace gone or a directory outside git."""
        current = record["current"]
        workspace, run_mode = record.get("selected_workspace"), record.get("selected_run_mode") or "direct"
        if record.get("no_place") or not workspace:
            return None  # A card without the rows (drawn since the home start) never moves the topic.
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
            self.tell(app, target, chat, f"换配置未完成：{fallback or reason}", "refused", click, bool(record.get("top_level")))

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


def lead_of(record) -> str:
    """The project whose lead a switch record changes ('' for an ordinary topic's)."""
    lead = record.get("lead") if isinstance(record, dict) else None
    return str(lead.get("project_id") or "") if isinstance(lead, dict) else ""

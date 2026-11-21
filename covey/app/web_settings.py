"""网页工作台的设置页 (docs/web.md, COVEY_WEB=1; data contract v7 ``settings``, v14 its ``terms``): the 设置 card's tabs as one item the page
draws (``snapshot``), and the few changes the page may make (``change``, app/web_ops.py's ``settings``), both through the code the card
itself draws and writes with. Nothing here is a second copy of a rule.

- What is pushed: the tabs ``hub_tabs`` shows (app/menu_panel.py: 新话题, 管家 unless COVEY_BUTLER=0, 工作区, 脱敏, 群聊, 设备 with
  COVEY_NODE_LISTEN; not 记忆 and 能做什么, the card's alone: CARD_ONLY), each with the rows the card draws (cards/butler_panel.py ``topic_settings``, ``butler_setting_list``, ``start_setting``,
  ``mode_setting``, ``subagent_setting``: label, kind, the chosen option and every option offered), the workspaces with their tags and 用途
  (cards/workspace_manager.py ``list_entries``, ``tag_words``), 脱敏's figures, the topics it is on in, its two switches (app/redaction_panel.py
  ``facts``) and its terms as they are (the page edits the list, so the terms go into the page's database;
  never a stand-in), the groups with their state and 免@
  (cards/group_manager.py ``state_words``, ``free_words``; a group is named by a hash of its chat id) and the nodes' lines (app/node_panel.py
  ``facts``). ``editable`` says which rows the page may change (EDITABLE); every other row says 去飞书改 (``feishu``: the bot's applink).
- What the page may change: every row of 新话题 and 记住的工作区's 移除; 管家's 供应商, 模型, 思考预算 and
  压缩阈值 (its 脱敏 and 私聊顶层的消息 are read only); a workspace's 用途; 脱敏's two switches and terms; a group's 停用 / 启用 (its 免@ is read only).
  Data contract v15: the butler's 脱敏 and 私聊顶层的消息 (每条直接开一个话题 with
  ``confirm``), 免@ set to 关闭 or 只有你 (``free``), a workspace added by its path, removed, the missing ones removed, and the 设备 and 代答
  pages (app/web_panels.py); 本周用量 as the card counts it (``usage``, the item key USAGE_KEY).
  脱敏's terms, many at once: ``terms`` (``add`` and ``remove``, the terms as written) in one ``update_config``, its receipt counts and
  never names a term (it is kept in the page's outbox).
  A row's change is the card's own (app/butler_panel.py ButlerPanel ``changes`` then core/butler_settings.py ``update``), so both write the one
  main-thread.json and the card and the page show the same; the page's receipt is the card's (``SAVED``). Only the owner: app/access.py
  ``is_owner`` of the owner the Bridge knows (the page's own check is its login).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

from covey.app import butler_panel as panel_rules
from covey.app.menu_panel import hub_tabs
from covey.cards import butler_panel as cards
from covey.cards.group_manager import FREE_CHOICES, KIND_WORDS, PROXY_BLOCKED, free_limit, free_mode, free_words, name_of, ordered, state_words
from covey.cards.redaction_panel import SWITCH_ROWS, TOPICS
from covey.cards.settings_hub import ABOUT, BUTLER, GROUPS, LABELS, MEMORY, NODES, PROXY, REDACTION, TOPIC, WORKSPACE
from covey.cards.workspace_manager import list_entries, tag_words, tail
from covey.app import web_launch, web_panels
from covey.app.access import free_talk_on
from covey.core import butler, butler_settings
from covey.core.menu_choice import tiers
from covey.core.workspaces import add_workspace, load_config, missing_workspaces, remove_workspaces

LIMIT = 60  # The most options a row lists (the card cuts to fit its size; the page need not).
CARD_ONLY = (MEMORY, ABOUT)  # The 设置 card's tabs the page's data contract has no page for: not pushed.
NAMES_SECONDS = 1800  # A group's name, as Feishu gave it, is asked again after this long.
NOTE_LENGTH = cards.NOTE_LENGTH
EDITABLE = frozenset({cards.TOPIC_HARNESS, cards.TOPIC_PROVIDER, cards.TOPIC_MODEL, cards.TOPIC_EFFORT, cards.TOPIC_RUN_MODE,
                      cards.TOPIC_REDACTION, cards.TOPIC_WORKSPACE, cards.START_ROW, cards.SUBAGENTS,
                      cards.HARNESS, cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT, cards.REDACTION, cards.SPACE,
                      cards.MODE})
BUTLER_KINDS = frozenset({cards.HARNESS, cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT, cards.REDACTION,
                          cards.SPACE})
CHANGES = ("row", "untrust", "note", "redaction", "terms", "group", "free", "workspace_add", "workspace_remove", "workspace_prune",
           "node_add", "node_remove", "refresh", "proxy")  # payload["what"].
PROXY_TAB, PROXY_LABEL = "proxy", "代答"  # The bot menu's own 代答 card, a tab of the page's 设置 (not of the card's).
FREE_ON_PAGE = ("", "owner")  # 免@ the page may set: only ever tighter; 你和选中的人 and 群里所有人 stay on the card.
MODE_TOPICS = "topics"  # cards/butler_panel.py MODE_CHOICES: 每条直接开一个话题, saved only with ``confirm`` (the card's 确定改).
APPLINK = "https://applink.feishu.cn/client/bot/open?appId="
OWNER_ONLY = "只有机器人主人能改设置"
GONE = "这个选项已不可用，页面会按最新设置重画"
READ_ONLY = "这一项网页上只读，请到飞书设置卡上改"
TERMS_SHAPE = "词表改动的格式不对，页面会按最新词表重画"
TERMS_UNREADABLE = "词表读不出来，请到飞书设置卡上看"


def group_key(chat_id) -> str:
    """How the page names a group: no chat id leaves the machine."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def row(setting, editable=False, follow="") -> dict:
    """A cards/launch_config.py Setting as the page reads it."""
    options = [{"value": str(value), "text": str(text)} for value, text in setting.options]
    return {"key": setting.key, "kind": setting.kind, "label": setting.label, "current": str(setting.current),
            "text": setting.text, "options": options, "editable": bool(editable and setting.kind in EDITABLE)}


class WebSettings:
    """The 设置 card's tabs for the page (``snapshot``) and the page's changes (``change``). ``env``: app/butler_env.py ButlerEnv, whose
    ``settings`` is the 设置 card (app/settings_hub.py SettingsHub: its panels draw and write)."""

    def __init__(self, env) -> None:
        self.env = env
        self.names = {}  # chat id -> (name, when asked)

    @property
    def hub(self):
        return getattr(self.env, "settings", None)

    def context(self):
        """The Bridge's default Harness's context (state_dir, profile, default_workdir), as a panel's click gets one."""
        topics = self.env.topics
        apps = getattr(topics, "apps", {}) or {}
        app = apps.get(getattr(topics, "default", "")) or next(iter(apps.values()), None)
        base = getattr(getattr(app, "harness", None), "context", None)
        return SimpleNamespace(state_dir=Path(self.env.state_dir), profile=getattr(base, "profile", None) or self.env.profile,
                               default_workdir=getattr(base, "default_workdir", None))

    def stamps(self) -> tuple:
        """What a change of the settings on the card shows in, read each second: main-thread.json's, workspaces.json's and the term
        list's (redaction/config.json) mtimes."""
        from covey.core.workspaces import config_path
        state = Path(self.env.state_dir)
        out = []
        from covey.core.redaction import config_path as terms_path
        from covey.app.web_panels import joins_path
        for path in (butler_settings.path(state), config_path(state), terms_path(state), joins_path(state)):
            try:
                out.append(path.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    # -- the snapshot ------------------------------------------------------------------------------------------------------------

    def snapshot(self, chat, now=None) -> dict:
        """The item's data: {tabs: [{key, label}], feishu, <tab key>: what it shows}."""
        now = time.time() if now is None else now
        ctx = self.context()
        tabs = [tab for tab in hub_tabs() if tab not in CARD_ONLY]
        settings = butler_settings.load(ctx.state_dir)
        choices = self.choices(ctx)
        labels = {**LABELS, PROXY_TAB: PROXY_LABEL}
        tabs = [*tabs, *([PROXY_TAB] if self.proxy_panel() is not None else [])]
        data = {"tabs": [{"key": tab, "label": labels[tab]} for tab in tabs], "feishu": self.feishu()}
        build = {TOPIC: lambda: self.topic(ctx, settings, choices, chat), BUTLER: lambda: self.butler(ctx, settings, choices),
                 WORKSPACE: lambda: self.workspaces(ctx, settings), REDACTION: lambda: self.redaction(ctx),
                 GROUPS: lambda: self.groups(ctx, now), NODES: lambda: self.nodes(ctx),
                 PROXY_TAB: lambda: web_panels.proxy_tab(ctx, self.proxy_panel(), row, LIMIT)}
        try:  # 开新话题's choices for the page's chat box (data contract v17), not a tab.
            data["launch"] = web_launch.catalog(self.env, ctx, chat)
        except Exception as exc:  # noqa: BLE001 - the rest of the settings go on
            self.env.runtime.log(f"web settings launch unreadable: {type(exc).__name__}")
        for tab in tabs:
            try:
                data[tab] = build[tab]()
            except Exception as exc:  # noqa: BLE001 - one tab that cannot be read leaves the others
                self.env.runtime.log(f"web settings tab {tab} unreadable: {type(exc).__name__}")
                data[tab] = {"error": "这一页现在读不出来，请到飞书设置卡上看"}
        return data

    def feishu(self) -> str:
        app_id = str(getattr(self.env.runtime.access, "app_id", "") or "")
        return APPLINK + app_id if app_id else ""

    def choices(self, ctx):
        from covey.app.launch_models import available_models
        return available_models(self.env.topics, ctx)

    def panel(self):
        return self.hub.panel(TOPIC)

    def proxy_panel(self):
        """The 代答 card's panel (app/proxy_panel.py) when this deployment has a sender, else None."""
        from covey.app.proxy_send import available
        if not available():
            return None
        try:
            return self.hub.panel(PROXY)
        except KeyError:
            return None

    def usage(self, now=None) -> dict:
        """本周用量 as the bot menu's card counts it (app/weekly_usage.py ``summarize`` over the job records of the window), for the
        page's own 本周用量 (the page summed its pushed turns, 8.41B against the card's 9.43B)."""
        from covey.app import weekly_usage
        from covey.core.jobs import select_job_records
        now = time.time() if now is None else now
        since = now - weekly_usage.WINDOW_DAYS * 86400
        titles = {name: harness.spec.title for name, harness in (getattr(self.env.topics, "harnesses", {}) or {}).items()}
        records = select_job_records(Path(self.env.state_dir), lambda r: (weekly_usage.created_at(r) or 0) >= since)
        found = weekly_usage.summarize(records, now, titles)
        return {**found, "outcomes": dict(found["outcomes"]), "ended": dict(found["ended"]), "at": now}

    def topic(self, ctx, settings, choices, chat) -> dict:
        """新话题, as the card's page: its rows (every one the page may change), 直接开工 with the butler on, 记住的工作区 while it counts,
        子 Agent under 更多."""
        panel, defaults = self.panel(), settings.topic_defaults
        on = butler.available() and settings.enabled
        found = cards.topic_settings(defaults, choices, panel_rules.workspace_names(ctx.state_dir), LIMIT,
                                     panel.follows(ctx, defaults, choices, chat), LIMIT, panel.fallback(ctx, defaults, choices, chat),
                                     butler=on)
        rows = [row(each, True) for each in found + ([cards.start_setting(settings)] if on else [])]
        trusted = [{"key": key, "text": f"{key.rpartition('|')[0]} · {'只读' if key.endswith('|read') else '改文件'}"}
                   for key in settings.trusted] if on and cards.trusted_shown(settings) else None
        return {"summary": cards.subtitle(settings, choices, TOPIC), "note": cards.TOPIC_NOTE, "rows": rows,
                "start_note": cards.START_NOTE if on else "", "trusted": trusted,
                "more": [row(cards.subagent_setting(settings), True)], "more_note": cards.SUBAGENT_NOTE,
                "locked": cards.LOCKED if on else ""}

    def butler(self, ctx, settings, choices) -> dict:
        """管家: off (纯话题), the mode's row alone; else its rows (Harness first) and under 更多 压缩阈值 and the mode. The page
        may change them all; 每条直接开一个话题 only after it asked ``mode_confirm`` (the card's 确定改). ``harness``: the
        Harness's name, for a page that still shows it in its own line; ``note``: REDACT_FIRST when it counts."""
        mode = {**row(cards.mode_setting(settings), True), "confirm": {MODE_TOPICS: cards.MODE_CONFIRM}}
        if not settings.enabled:
            return {"summary": cards.subtitle(settings, choices, BUTLER), "on": False, "rows": [mode], "note": cards.MODE_OFF}
        own = cards.butler_setting_list(settings, choices, LIMIT, panel_rules.workspace_names(ctx.state_dir))
        harness = settings.butler_harness
        return {"summary": cards.subtitle(settings, choices, BUTLER), "on": True,
                "harness": tiers(choices, harness)[0].get(harness) or harness,
                **({"note": cards.REDACT_FIRST} if cards.redact_first(settings, choices) else {}),
                "rows": [row(each, True) for each in own if each.key != "compact"],
                "more": [row(each, True) for each in own if each.key == "compact"] + [mode], "locked": cards.LOCKED}

    def nodes(self, ctx) -> dict:
        """设备: app/web_panels.py ``nodes_tab``."""
        return web_panels.nodes_tab(ctx.state_dir, self.hub.panel(NODES))

    def workspaces(self, ctx, settings) -> dict:
        """工作区: the list in the card's order, each row's tags, path's tail, last use and 用途 (with the butler)."""
        config = load_config(ctx.state_dir, ctx.default_workdir)
        status = self.hub.panel(WORKSPACE).status(config)
        notes = butler.available()
        items = [{"name": entry["name"], "path": tail(entry["path"], 80),
                  "tags": [{"text": text, "tone": colour} for text, colour in tag_words(entry, config["default"])],
                  "used": entry.get("used") or None, "removable": entry["name"] != config["default"],
                  **({"note": settings.note(entry["name"])} if notes else {})}
                 for entry in list_entries(config, status)]
        return {"summary": f"{len(items)} 个工作区", "items": items, "notes": notes,
                "note": cards.WORKSPACE_NOTE if notes else "", "note_length": NOTE_LENGTH,
                "missing": len(missing_workspaces(config, status))}

    def redaction(self, ctx) -> dict:
        """脱敏: the figures, the topics it is on in and the two switches, as the tab's ``facts`` read them, and the terms in the list's
        order (``terms``, oldest first; no stand-in) with what the page checks a line against before it saves."""
        from covey.app.redaction_panel import facts
        from covey.core.redaction import MAX_LENGTH, MAX_TERMS, load_config as terms_config
        data = facts(ctx.state_dir, b"web")
        if data.get("error"):
            return {"error": data["error"]}
        try:
            terms = [row["term"] for row in terms_config(ctx.state_dir)["terms"]]
        except ValueError:
            return {"error": TERMS_UNREADABLE}
        return {"summary": f"{data['count']} 个词 · 近 7 天替换 {data['week']} 次 · {len(data['topics'])} 个话题开着",
                "count": data["count"], "week": data["week"],
                # The card's first TOPICS, newest first; ``last`` in epoch seconds (the card's own words are its time of day).
                "topics": [{"number": t.get("number"), "title": t.get("title", ""), "hits": t.get("hits", 0), "last": t.get("at")}
                           for t in data["topics"][:TOPICS]],
                "switches": [{"name": name, "label": label, "hint": hint, "on": data["config"].get(name) is True, "editable": True}
                             for name, (label, hint) in SWITCH_ROWS.items()],
                # 记录: the relays' latest turns as the card's 记录 fold lists them (figures only).
                "records": [{"time": t.get("time", ""), "number": t.get("number"), "model": t.get("model", ""),
                             "requests": t.get("requests", 0), "replaced": t.get("replaced", 0)} for t in data.get("turns") or ()],
                "terms": terms, "ignore_case": data["config"].get("ignore_case") is True, "max_terms": MAX_TERMS,
                "max_length": MAX_LENGTH}

    def groups(self, ctx, now) -> dict:
        """群聊: every group the card lists, active first, its state and 免@; 停用 / 启用 the page may do."""
        manager = self.hub.panel(GROUPS)
        records, kinds = manager.listed()
        names = self.group_names(ctx, manager, records, now)
        active = sum(1 for g in records if g.get("active"))
        items = []
        for record in ordered(records, names):
            kind = (kinds or {}).get(record["chat_id"]) or ""
            personal = kind == "personal"
            text, tone = state_words(record)
            # The card's buttons: none on a personal group or an external one, else 停用 or 启用.
            switch = "" if personal or record.get("external") is True else "off" if record.get("active") is True else "on"
            free = free_talk_on() and not personal
            # 免@ the page may set: 关闭 and 只有你, where the card offers them (never in the proxy's group).
            blocked = free and free_limit(record, self.env.runtime.access.proxy_chat(record["chat_id"])) == PROXY_BLOCKED
            items.append({"key": group_key(record["chat_id"]), "name": name_of(record, names), "space": KIND_WORDS.get(kind, ""),
                          "state": None if personal else {"text": text, "tone": tone},
                          "free": free_words(record) if free else "", "switch": switch,
                          "free_mode": free_mode(record) if free else None,
                          "free_choices": [{"value": mode, "text": FREE_CHOICES[mode][0]} for mode in FREE_ON_PAGE]
                          if free and not blocked else []})
        return {"summary": f"{len(records)} 个群 · 已启用 {active}" if records else "还没有群", "items": items}

    def group_names(self, ctx, manager, records, now) -> dict:
        """{chat id: name}: asked of Feishu (the card's own lookup) for a group not asked in NAMES_SECONDS, kept between looks."""
        stale = [g for g in records if now - self.names.get(g["chat_id"], ("", 0.0))[1] >= NAMES_SECONDS]
        if stale:
            found = manager.names(ctx, stale)
            for g in stale:
                self.names[g["chat_id"]] = (found.get(g["chat_id"]) or self.names.get(g["chat_id"], ("", 0.0))[0], now)
        return {chat: name for chat, (name, _) in self.names.items() if name}

    # -- a change from the page ------------------------------------------------------------------------------------------------

    def change(self, payload, owner) -> str:
        """One change the page asked for (``payload["what"]``, CHANGES); the card's receipt. ValueError says why it was not made."""
        if not owner:
            raise ValueError(OWNER_ONLY)
        if self.hub is None:
            raise ValueError("设置卡不可用")
        what = str(payload.get("what") or "")
        if what not in CHANGES:
            raise ValueError("不认识的设置改动")
        ctx = self.context()
        return getattr(self, "change_" + what)(ctx, payload)

    def apply(self, ctx, kind, value) -> str:
        """``value`` (a card button's value) through the card's ``changes``, written by ``update``; the card's receipt."""
        changes = self.panel().changes(ctx, {}, {"kind": kind, **value})
        if changes is None:
            raise ValueError(GONE)
        if not changes:
            return "没有变化"
        butler_settings.update(ctx.state_dir, **changes)
        return (panel_rules.MODE_NOTICES.get(changes.get("enabled")) or panel_rules.SAVED_OF.get(kind)
                or panel_rules.SAVED.get(cards.PAGE_OF.get(kind), "已保存。"))

    def change_row(self, ctx, payload) -> str:
        kind, option = str(payload.get("kind") or ""), payload.get("option")
        if kind not in EDITABLE:
            raise ValueError(READ_ONLY)
        if not isinstance(option, str):
            raise ValueError(GONE)
        settings = butler_settings.load(ctx.state_dir)
        on = butler.available() and settings.enabled
        if kind in BUTLER_KINDS and not on or kind == cards.START_ROW and not on:
            raise ValueError("管家没开，这一项现在不起作用")
        if kind == cards.MODE:
            if not butler.available():
                raise ValueError(READ_ONLY)
            if option == MODE_TOPICS and settings.enabled and payload.get("confirm") is not True:
                raise ValueError("改成每条直接开一个话题要先确认")
        return self.apply(ctx, kind, {"option": option})

    def change_untrust(self, ctx, payload) -> str:
        key = str(payload.get("key") or "")
        if key not in butler_settings.load(ctx.state_dir).trusted:
            raise ValueError("这一条已经不在了")
        return self.apply(ctx, cards.UNTRUST, {"key": key})

    def change_note(self, ctx, payload) -> str:
        """A workspace's 用途, by the card's 保存用途 (ButlerPanel ``notes``): empty removes it."""
        name, text = str(payload.get("workspace") or ""), payload.get("note")
        if not butler.available():
            raise ValueError("管家没开，没有工作区用途")
        if name not in panel_rules.workspace_names(ctx.state_dir) or not isinstance(text, str):
            raise ValueError("没有这个工作区")
        changes = panel_rules.ButlerPanel.notes(butler_settings.load(ctx.state_dir), {"workspaces": [name]}, {"note_0": text})
        butler_settings.update(ctx.state_dir, **changes)
        return panel_rules.SAVED[WORKSPACE]

    def change_redaction(self, ctx, payload) -> str:
        """不区分大小写 or 回复还原, by the tab's own switch (RedactionPanel ``switch``)."""
        from covey.core.redaction import Refused
        name, on = payload.get("name"), payload.get("on")
        if name not in SWITCH_ROWS or not isinstance(on, bool):
            raise ValueError("没有这个开关")
        try:
            notice = self.hub.redaction.switch(ctx, {"name": name, "on": on}).get("notice") or ""
        except Refused as exc:
            raise ValueError(str(exc)) from None
        return notice.lstrip("✓ ").strip() or "已保存。"

    def change_terms(self, ctx, payload) -> str:
        """Terms added and removed at once (``add``, ``remove``: lists of terms as written), in one save (core/redaction.py
        ``edit_terms``, ``whole``): the removed go first (a term written again in another case is a remove and an add), a term not in
        the list is passed over, as is an added one already there; an added term the list cannot take saves nothing. The receipt
        counts."""
        from covey.core.redaction import Refused, edit_terms
        add, remove = payload.get("add"), payload.get("remove")
        if not isinstance(add, list) or not isinstance(remove, list) or not all(isinstance(t, str) for t in add + remove):
            raise ValueError(TERMS_SHAPE)
        add = [t.strip() for t in add if t.strip()]
        remove = [t.strip() for t in remove if t.strip()]
        if not add and not remove:
            return "没有变化"

        try:
            edit = edit_terms(ctx.state_dir, add=add, remove=remove, whole=True)
        except Refused as exc:
            wrong = {}
            for number, why in exc.skipped:
                wrong.setdefault(why, []).append(number)
            raise ValueError("没有保存：" + "；".join(f"新加的第 {'、'.join(map(str, n[:5]))}{' 等' if len(n) > 5 else ''} 个词{why}"
                                                for why, n in wrong.items()) if wrong else str(exc)) from None
        except ValueError:  # Its words may quote a term, and the receipt is kept in the page's outbox.
            raise ValueError(TERMS_UNREADABLE) from None
        except OSError:
            raise ValueError("没有保存：词表写不进去") from None
        added, removed, there, missing = len(edit.added), len(edit.removed), len(edit.already), len(edit.missing)
        done = "、".join(f"{verb} {n} 个" for verb, n in (("加", added), ("删", removed)) if n)
        passed = "，".join(words for words, n in ((f"{there} 个已在词表里", there), (f"{missing} 个已经不在词表里", missing)) if n)
        words = (f"已保存：{done}" if done else "没有变化") + (f"（{passed}）" if passed else "")
        return words + ("。下一轮起生效。" if done else "。")

    def change_group(self, ctx, payload) -> str:
        """停用 / 启用 of one group (GroupManager ``switch``, the card's)."""
        key, on = str(payload.get("group") or ""), payload.get("on")
        if not isinstance(on, bool):
            raise ValueError("没说停用还是启用")
        manager = self.hub.panel(GROUPS)
        records, _ = manager.listed()
        chat = next((g["chat_id"] for g in records if group_key(g["chat_id"]) == key), "")
        if not chat:
            raise ValueError("没有这个群的记录了")
        words = manager.switch(ctx, chat, on, {c: n for c, (n, _) in self.names.items() if n})
        if words.startswith("⚠️"):
            raise ValueError(words.removeprefix("⚠️").strip())
        return words.removeprefix("✓").strip()

    def change_free(self, ctx, payload) -> str:
        """A group's 免@ set to 关闭 or 只有你 (FREE_ON_PAGE), by the card's own (GroupManager ``free``: its limits, its words)."""
        mode = payload.get("mode")
        if mode not in FREE_ON_PAGE:
            raise ValueError("网页上只能把免@ 改成关闭或只有你；“你和选中的人”“群里所有人”请到飞书设置卡上改")
        if not free_talk_on():
            raise ValueError("这个部署没有打开免@")
        chat = self.group_chat(payload)
        notice = self.hub.panel(GROUPS).free(ctx, chat, mode, {c: n for c, (n, _) in self.names.items() if n}, {}).get("notice") or ""
        if notice.startswith("⚠️"):
            raise ValueError(notice.removeprefix("⚠️").strip())
        return notice.removeprefix("✓").strip() or "已保存。"

    def group_chat(self, payload) -> str:
        key = str(payload.get("group") or "")
        records, _ = self.hub.panel(GROUPS).listed()
        chat = next((g["chat_id"] for g in records if group_key(g["chat_id"]) == key), "")
        if not chat:
            raise ValueError("没有这个群的记录了")
        return chat

    def change_workspace_add(self, ctx, payload) -> str:
        """添加 by the path typed (or ``设备别名:路径``), the card's ``add_workspace``: the directory checked for at most 5 seconds."""
        text = payload.get("path")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("请填写目录的绝对路径")
        name, added = add_workspace(ctx.state_dir, ctx.default_workdir, text.strip()[:1000])
        return f"已添加工作区 {name}" if added else f"该目录已在列表中：{name}"

    def change_workspace_remove(self, ctx, payload) -> str:
        """移除 of one entry (confirmed on the page first): the list only, the directory and the topics on it stay."""
        name = str(payload.get("workspace") or "")
        removed = remove_workspaces(ctx.state_dir, ctx.default_workdir, [name])
        if not removed:
            raise ValueError("该工作区已不在列表中")
        return f"已从列表移除 {name}，磁盘目录未删除"

    def change_workspace_prune(self, ctx, payload) -> str:
        """移除不存在的: every entry whose directory is confirmed gone (never the default, never one that did not answer)."""
        config = load_config(ctx.state_dir, ctx.default_workdir)
        removed = remove_workspaces(ctx.state_dir, ctx.default_workdir, missing_workspaces(config))
        return f"已从列表移除 {len(removed)} 项，磁盘目录未删除" if removed else "没有目录不存在的工作区"

    def nodes_panel(self):
        from covey.core import nodes
        if not nodes.enabled():
            raise ValueError("这个部署没有开设备（COVEY_NODE_LISTEN）")
        return self.hub.panel(NODES)

    def change_node_add(self, ctx, payload) -> str:
        return web_panels.node_add(ctx, self.nodes_panel(), payload)

    def change_node_remove(self, ctx, payload) -> str:
        return web_panels.node_remove(ctx, self.nodes_panel(), payload)

    def change_refresh(self, ctx, payload) -> str:
        """刷新: nothing changes; the settings are pushed again now (app/web_sync.py makes them due after any settings op)."""
        return "已刷新"

    def change_proxy(self, ctx, payload) -> str:
        panel = self.proxy_panel()
        if panel is None:
            raise ValueError("这个部署没有代答")
        return web_panels.proxy_change(ctx, panel, payload)

"""网页工作台的设置页 (docs/butler.md 网页工作台, COVEY_WEB=1; data contract v7 ``settings``, v14 its ``terms``): the 设置 card's tabs as one item the page
draws (``snapshot``), and the few changes the page may make (``change``, app/web_ops.py's ``settings``), both through the code the card
itself draws and writes with. Nothing here is a second copy of a rule.

- What is pushed: the tabs ``hub_tabs`` shows (app/menu_panel.py: 新话题, 管家 unless COVEY_BUTLER=0, 工作区, 脱敏, 群聊, 设备 with
  COVEY_NODE_LISTEN), each with the rows the card draws (cards/butler_panel.py ``topic_settings``, ``butler_setting_list``, ``start_setting``,
  ``mode_setting``, ``subagent_setting``: label, kind, the chosen option and every option offered), the workspaces with their tags and 用途
  (cards/workspace_manager.py ``list_entries``, ``tag_words``), 脱敏's figures, the topics it is on in, its two switches (app/redaction_panel.py
  ``facts``) and its terms as they are (the owner, 2026-11-19 T567: the page edits the list, so the terms go into the page's database;
  never a stand-in), the groups with their state and 免@
  (cards/group_manager.py ``state_words``, ``free_words``; a group is named by a hash of its chat id) and the nodes' lines (app/node_panel.py
  ``facts``). ``editable`` says which rows the page may change (EDITABLE); every other row says 去飞书改 (``feishu``: the bot's applink).
- What the page may change (the owner, 2026-11-19, T550 三、设置): every row of 新话题 and 记住的工作区's 移除; 管家's 供应商, 模型, 思考预算 and
  压缩阈值 (its 脱敏 and 私聊顶层的消息 are read only); a workspace's 用途; 脱敏's two switches and terms; a group's 停用 / 启用 (its 免@ is read only).
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
from covey.cards.group_manager import KIND_WORDS, name_of, ordered, state_words, free_words
from covey.cards.redaction_panel import SWITCH_ROWS, TOPICS
from covey.cards.settings_hub import BUTLER, GROUPS, LABELS, NODES, REDACTION, TOPIC, WORKSPACE
from covey.cards.workspace_manager import list_entries, tag_words, tail
from covey.app.access import free_talk_on
from covey.core import butler, butler_settings
from covey.core.menu_choice import tiers
from covey.core.workspaces import load_config

LIMIT = 60  # The most options a row lists (the card cuts to fit its size; the page need not).
NAMES_SECONDS = 1800  # A group's name, as Feishu gave it, is asked again after this long.
NOTE_LENGTH = cards.NOTE_LENGTH
EDITABLE = frozenset({cards.TOPIC_HARNESS, cards.TOPIC_PROVIDER, cards.TOPIC_MODEL, cards.TOPIC_EFFORT, cards.TOPIC_RUN_MODE,
                      cards.TOPIC_REDACTION, cards.TOPIC_WORKSPACE, cards.START_ROW, cards.SUBAGENTS,
                      cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT})
BUTLER_KINDS = frozenset({cards.PROVIDER, cards.MODEL, cards.EFFORT, cards.COMPACT})
CHANGES = ("row", "untrust", "note", "redaction", "terms", "group")  # payload["what"].
APPLINK = "https://applink.feishu.cn/client/bot/open?appId="
OWNER_ONLY = "只有机器人主人能改设置"
GONE = "这个选项已不可用，页面会按最新设置重画"
READ_ONLY = "这一项网页上只读，请到飞书设置卡上改"
TERMS_SHAPE = "词表改动的格式不对，页面会按最新词表重画"
TERMS_UNREADABLE = "词表读不出来，请到飞书设置卡上看"
ALREADY = "词表里已经有这个词"  # core/redaction.py row_problem's words for a term already in the list.


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
        for path in (butler_settings.path(state), config_path(state), terms_path(state)):
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
        tabs = hub_tabs()
        settings = butler_settings.load(ctx.state_dir)
        choices = self.choices(ctx)
        data = {"tabs": [{"key": tab, "label": LABELS[tab]} for tab in tabs], "feishu": self.feishu()}
        build = {TOPIC: lambda: self.topic(ctx, settings, choices, chat), BUTLER: lambda: self.butler(settings, choices),
                 WORKSPACE: lambda: self.workspaces(ctx, settings), REDACTION: lambda: self.redaction(ctx),
                 GROUPS: lambda: self.groups(ctx, now), NODES: lambda: self.nodes(ctx)}
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

    def butler(self, settings, choices) -> dict:
        """管家: off (纯话题), the mode's row alone; else its Harness, its rows, and under 更多 压缩阈值 and the mode (read only)."""
        mode = row(cards.mode_setting(settings))
        if not settings.enabled:
            return {"summary": cards.subtitle(settings, choices, BUTLER), "on": False, "rows": [mode], "note": cards.MODE_OFF}
        own = cards.butler_setting_list(settings, choices, LIMIT)
        return {"summary": cards.subtitle(settings, choices, BUTLER), "on": True,
                "harness": tiers(choices, cards.CLAUDE)[0].get(cards.CLAUDE) or "Claude Code",
                "rows": [row(each, True) for each in own if each.key != "compact"],
                "more": [row(each, True) for each in own if each.key == "compact"] + [mode], "locked": cards.LOCKED}

    def workspaces(self, ctx, settings) -> dict:
        """工作区: the list in the card's order, each row's tags, path's tail, last use and 用途 (with the butler)."""
        config = load_config(ctx.state_dir, ctx.default_workdir)
        status = self.hub.panel(WORKSPACE).status(config)
        notes = butler.available()
        items = [{"name": entry["name"], "path": tail(entry["path"], 80),
                  "tags": [{"text": text, "tone": colour} for text, colour in tag_words(entry, config["default"])],
                  "used": entry.get("used") or None, **({"note": settings.note(entry["name"])} if notes else {})}
                 for entry in list_entries(config, status)]
        return {"summary": f"{len(items)} 个工作区", "items": items, "notes": notes,
                "note": cards.WORKSPACE_NOTE if notes else "", "note_length": NOTE_LENGTH}

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
            items.append({"key": group_key(record["chat_id"]), "name": name_of(record, names), "space": KIND_WORDS.get(kind, ""),
                          "state": None if personal else {"text": text, "tone": tone},
                          "free": free_words(record) if free_talk_on() and not personal else "", "switch": switch})
        return {"summary": f"{len(records)} 个群 · 已启用 {active}" if records else "还没有群", "items": items}

    def group_names(self, ctx, manager, records, now) -> dict:
        """{chat id: name}: asked of Feishu (the card's own lookup) for a group not asked in NAMES_SECONDS, kept between looks."""
        stale = [g for g in records if now - self.names.get(g["chat_id"], ("", 0.0))[1] >= NAMES_SECONDS]
        if stale:
            found = manager.names(ctx, stale)
            for g in stale:
                self.names[g["chat_id"]] = (found.get(g["chat_id"]) or self.names.get(g["chat_id"], ("", 0.0))[0], now)
        return {chat: name for chat, (name, _) in self.names.items() if name}

    def nodes(self, ctx) -> dict:
        """设备: each node's line, read only (添加 and 移除 stay on the card)."""
        from covey.app.node_panel import facts
        rows = facts(ctx.state_dir, "")["rows"]
        return {"summary": f"{len(rows)} 台设备" if rows else "还没有设备",
                "items": [{"alias": r["alias"], "title": r["title"], "line": r["line"], "grey": r["grey"], "local": r["local"]}
                          for r in rows]}

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
        return panel_rules.SAVED.get(cards.PAGE_OF.get(kind), "已保存。")

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
        """Terms added and removed at once (``add``, ``remove``: lists of terms as written), in one ``update_config``: the removed go
        first (a term written again in another case is a remove and an add), a term not in the list is passed over, as is an added one
        already there; an added term the list cannot take (core/redaction.py ``row_problem``) saves nothing. The receipt counts."""
        from covey.core.redaction import Refused, row_problem, update_config
        add, remove = payload.get("add"), payload.get("remove")
        if not isinstance(add, list) or not isinstance(remove, list) or not all(isinstance(t, str) for t in add + remove):
            raise ValueError(TERMS_SHAPE)
        add = [t.strip() for t in add if t.strip()]
        remove = [t.strip() for t in remove if t.strip()]
        if not add and not remove:
            return "没有变化"

        def change(config):
            fold = str.casefold if config["ignore_case"] else (lambda t: t)
            gone = {fold(t) for t in remove}
            kept = [row for row in config["terms"] if fold(row["term"]) not in gone]
            removed, config["terms"] = len(config["terms"]) - len(kept), kept
            added, there, wrong = 0, 0, {}
            for number, term in enumerate(add, 1):
                why = "替身词自动生成，不用写 =>" if "=>" in term else row_problem(
                    config["terms"], term, "", ignore_case=config["ignore_case"])
                if why == ALREADY:
                    there += 1
                elif why:
                    wrong.setdefault(why, []).append(number)
                else:
                    config["terms"].append({"term": term, "stand_in": "", "anywhere": False})
                    added += 1
            if wrong:
                raise Refused("没有保存：" + "；".join(f"新加的第 {'、'.join(map(str, n[:5]))}{' 等' if len(n) > 5 else ''} 个词{why}"
                                                   for why, n in wrong.items()))
            return added, removed, there, len(gone) - removed

        try:
            added, removed, there, missing = update_config(ctx.state_dir, change)
        except Refused as exc:
            raise ValueError(str(exc)) from None
        except ValueError:  # Its words may quote a term, and the receipt is kept in the page's outbox.
            raise ValueError(TERMS_UNREADABLE) from None
        except OSError:
            raise ValueError("没有保存：词表写不进去") from None
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

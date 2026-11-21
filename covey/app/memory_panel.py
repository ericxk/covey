"""The 设置 card's 记忆 tab (cards/memory_panel.py), shown when COVEY_MEMORY=1 (core/memory.py): the owner's memory by scope
(全局, 工作区, 专项, 话题), each entry deletable through its own store, a principle marked 常驻, and what the next turn in a
chosen workspace carries, the block as app/prompts.py memory_block builds it. No model, no Feishu call but the card's.

What it reads (``facts``), at each draw: the principles (core/principles.py) and, with COVEY_KNOWLEDGE=1, the knowledge entries
(core/knowledge.py) of 全局 and of the chosen workspace (the default one until the 工作区 row chose another), through
core/memory.py ``items``, ``fill`` and ``select`` exactly as a turn does (app/memory_turn.py: an ordinary new topic there, the
owner's own turn); the memory of every project not cancelled (core/project_memory.py, COVEY_PROJECT_MEMORY=1) and of every
topic a turn wrote one for (core/topic_memory.py; the topics are found by the job records' ``memory_notes``,
app/memory_collect.py). A scope near its limit (NEAR) or past it says so on the card, with how many entries the next turn
leaves out: they are not dropped silently any more. A pinned principle (常驻, core/principles.py ``pin``) is carried
whatever its room (core/memory.py ``fill``).

Deleting goes through the paths that already exist: core/principles.py ``delete``, core/knowledge.py ``drop`` (with a reason
that says it was the 设置 card), core/project_memory.py ``delete`` and core/topic_memory.py ``delete`` (tombstones, as 不对 on an
answer card leaves). A button's ``key`` names the entry in its store: ``principle:<id>``, ``knowledge:<id>``,
``project:<project id>:<id>``, ``topic:<topic id>:<id>``.

The tab's record is the 设置 card's (app/settings_hub.py, app/menu_panel.py); a click (app/unified.py PANELS, the owner's) acts
outside the record's lock, then the whole card is drawn again under it once the click is acknowledged (``notice`` says what
happened). The same click delivered again acts once: the view keeps the last ``seen`` event ids.
"""
from __future__ import annotations

import re

from covey.app.card_lanes import after_ack
from covey.app.memory_collect import TOPIC_WORDS
from covey.app.menu_panel import FOLDED, MenuPanel, hub_top
from covey.app.prompts import memory_block
from covey.cards.memory_panel import DELETE, KINDS, OPEN, PIN, WORKSPACE, memory_card
from covey.cards.settings_hub import MEMORY
from covey.core import knowledge, memory, principles, project_memory, projects, topic_memory
from covey.core.jobs import select_job_records
from covey.core.topics import label_title
from covey.core.workspaces import load_config

SEEN = 8
NEAR = 0.8  # A scope this full (of its limit) says so on the card.
WORKSPACES = 12  # Workspaces the 工作区 row offers (the chosen one always among them).
ENTRY = re.compile(r"[0-9A-Za-z_-]{1,40}")  # A project's or a topic's entry id.
DROP_REASON = "主人在设置卡「记忆」里删掉"
OWNER_ONLY = '⚠️ 仅机器人主人可以改记忆。'
SCOPE_TITLES = {memory.GLOBAL: "全局", memory.WORKSPACE: "工作区", memory.PROJECT: "专项", memory.TOPIC: "话题"}


def workspace_names(ctx) -> tuple[str, list]:
    """(the default workspace, the names the 工作区 row offers); ('', []) when the list cannot be read."""
    try:
        config = load_config(ctx.state_dir, ctx.default_workdir)
    except (OSError, ValueError, TypeError, AttributeError):
        return "", []
    return str(config["default"]), [str(name) for name in config["workspaces"]]


def room_line(title, info) -> str:
    """The line of a scope near its limit or past it ('' otherwise): how many entries the next turn leaves out."""
    used, limit, left = info["used"], info["limit"], len(info["left"])
    if left:
        return (f"⚠️ {title}放不下 {left} 条，下一轮不带它们（最早的先放不下）："
                "要一直带上的点「常驻」，用不着的删掉。")
    if used > limit:
        return f"{title}的常驻已超过 {limit:,} 字，照样都带上；这里再记的会放不下。"
    if used >= NEAR * limit:
        return f"{title}快满了（{used:,}/{limit:,} 字）：再记新的，最早的会放不下。"
    return ""


def row_of(item, key, carried=None, where="") -> dict:
    """An entry of the card: ``carried`` the refs the next turn carries (None: the part has no preview)."""
    return {"key": key, "text": item["text"], "by": memory.OWNER_WORDS[item["by"]], "pinned": bool(item.get("pinned")),
            "pinnable": key.startswith("principle:"), "where": where,
            "out": carried is not None and item["ref"] not in carried, "at": item["at"]}


def newest(rows) -> list:
    return sorted(rows, key=lambda row: (-row["at"], row["key"]))


def memory_topics(state_dir) -> list:
    """The topics a turn wrote a memory line for (the job records' ``memory_notes`` of 管这个话题), newest first, each once."""
    records = select_job_records(state_dir, lambda record: any(
        isinstance(note, dict) and note.get("scope") == TOPIC_WORDS for note in record.get("memory_notes") or ()))
    return list(dict.fromkeys(str(record.get("topic_id") or "") for record in records if record.get("topic_id")))


def other_workspaces(state_dir, chosen, knowledge_on) -> str:
    """One grey line of the other workspaces' entries: ``其他工作区：docs 3 条、web 1 条``; '' when there are none."""
    counts = {}
    for name, group in principles.load(state_dir)["workspaces"].items():
        counts[name] = counts.get(name, 0) + len(group)
    if knowledge_on:
        for scope, name, group in knowledge.groups(state_dir):
            if scope == knowledge.WORKSPACE:
                counts[name] = counts.get(name, 0) + sum(1 for entry in group.values() if entry["state"] == knowledge.CURRENT)
    shown = [f"{name} {n} 条" for name, n in sorted(counts.items()) if n and name != chosen]
    return ("其他工作区：" + "、".join(shown) + "（上面的工作区一行换着看）") if shown else ""


def facts(state_dir, names, chosen) -> dict:
    """The tab's data (cards/memory_panel.py): ``preview`` (the next turn of an ordinary new topic in ``chosen`` on the
    owner's own turn) and ``parts`` (全局, 工作区, 专项, 话题: their entries, the newest first)."""
    knowledge_on = knowledge.enabled()
    mine, space = principles.for_turn(state_dir, chosen)
    entries = knowledge.for_turn(state_dir, chosen) if knowledge_on else ((), ())
    found = memory.items(principles=(mine, space), entries=entries)
    scopes = memory.fill(found)
    kept, left = memory.select([dict(item) for item in found])
    carried = {item["ref"] for item in kept}
    rooms = [("全局", scopes[memory.GLOBAL]["used"], scopes[memory.GLOBAL]["limit"])]
    if chosen:
        rooms.append((f"工作区 {chosen}", scopes[memory.WORKSPACE]["used"], scopes[memory.WORKSPACE]["limit"]))
    warnings = [line for line in (room_line("全局", scopes[memory.GLOBAL]),
                                  room_line(f"工作区 {chosen}", scopes[memory.WORKSPACE]) if chosen else "") if line]
    parts = [{"scope": memory.GLOBAL, "title": "全局",
              "rows": newest(row_of(item, item["ref"], carried) for item in found if item["scope"] == memory.GLOBAL)},
             {"scope": memory.WORKSPACE, "title": f"工作区 {chosen}" if chosen else "工作区",
              "rows": newest(row_of(item, item["ref"], carried) for item in found if item["scope"] == memory.WORKSPACE),
              "others": other_workspaces(state_dir, chosen, knowledge_on)}]
    rows = []
    if projects.memory_enabled():
        for record in projects.records(state_dir):
            if projects.state_of(record) == projects.CANCELLED:
                continue
            items = memory.items(project=project_memory.live(state_dir, record, style=False), project_id=record["id"],
                                 redacted=True)
            name = f"专项「{record.get('name') or record['id']}」"
            rows += [row_of(item, item["ref"], where=name) for item in items]
            warnings.append(room_line(name, memory.fill(items)[memory.PROJECT]))
    parts.append({"scope": memory.PROJECT, "title": "专项", "rows": newest(rows),
                  "empty": "没有" if projects.memory_enabled() else "专项的记忆没开（COVEY_PROJECT_MEMORY=1）"})
    rows = []
    for topic in memory_topics(state_dir):
        items = memory.items(topic=topic_memory.live(state_dir, topic), redacted=True)
        label = label_title(state_dir, topic)[0] or "一个话题"
        rows += [row_of(item, f"topic:{topic}:{item['ref'].partition(':')[2]}", where=label) for item in items]
        warnings.append(room_line(label, memory.fill(items)[memory.TOPIC]))
    parts.append({"scope": memory.TOPIC, "title": "话题", "rows": newest(rows)})
    shown = list(dict.fromkeys([chosen, *names]))[:WORKSPACES] if chosen else names[:WORKSPACES]
    preview = {"workspace": chosen, "workspaces": shown, "count": len(kept),
               "chars": sum(memory.line_size(item) for item in kept), "left": left, "rooms": rooms,
               "warnings": [line for line in warnings if line], "text": memory_block(kept, left)}
    return {"preview": preview, "parts": parts}


def forget(state_dir, key):
    """The text of the entry ``key`` names, deleted through its own store; None when it is not there (any more)."""
    kind, _, rest = str(key or "").partition(":")
    if kind == "principle" and principles.ID.fullmatch(rest):
        found = principles.delete(state_dir, rest)
        return found["text"] if found else None
    if kind == "knowledge" and knowledge.ID.fullmatch(rest):
        try:
            return knowledge.drop(state_dir, rest, DROP_REASON, source={"via": "settings_card"}, by="main")["text"]
        except ValueError:
            return None
    if kind == "project":
        pid, _, entry = rest.partition(":")
        if not (projects.valid_id(pid) and ENTRY.fullmatch(entry)):
            return None
        row = next((row for row in project_memory.entries(state_dir, projects.read_record(state_dir, pid))
                    if row["id"] == entry and not row["deleted"]), None)
        try:
            return row["text"] if row is not None and project_memory.delete(state_dir, pid, entry) else None
        except KeyError:  # The project has no record any more.
            return None
    if kind == "topic":
        topic, _, entry = rest.partition(":")
        if not (projects.TOPIC_RE.fullmatch(topic) and ENTRY.fullmatch(entry)):
            return None
        row = next((row for row in topic_memory.live(state_dir, topic) if row["id"] == entry), None)
        return row["text"] if row is not None and topic_memory.delete(state_dir, topic, entry) else None
    return None


def short(text, limit=24) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class MemoryPanel:
    def __init__(self, runtime, messenger, voice):
        self.runtime, self.messenger, self.voice = runtime, messenger, voice
        self.menu = MenuPanel(self, 'memory-panel', 'memory-cards', 'memory-card', tab=MEMORY)

    def path(self, ctx, token):
        return self.menu.path(ctx, token)

    @staticmethod
    def matches(value):
        return value.get('kind') in KINDS

    def claim(self, ctx, event, raw, owner):
        return self.click(ctx, event, raw, owner) if self.matches(raw) else None

    @staticmethod
    def tab_view(token):
        return {'token': token, 'revision': 1, 'hub': True, 'workspace': '', 'open': '', 'page': 0}

    @staticmethod
    def tab_fields(ctx):
        return {}

    def tab_card(self, ctx, token, record):
        return self.draw(ctx, token, record['view'])

    def tab_shown(self, ctx, token):
        self.runtime.log(f'memory tab shown token={token[:8]}')

    def refuse(self, ctx, path, event):
        return self.menu.refuse(ctx, path, event, OWNER_ONLY, lambda view, record: self.draw(ctx, path.stem, view))

    def chosen(self, ctx, view) -> tuple[str, list]:
        default, names = workspace_names(ctx)
        wanted = view.get('workspace') or ''
        return (wanted if wanted in names else default), names

    def draw(self, ctx, token, view):
        chosen, names = self.chosen(ctx, view)
        return memory_card(token, view, facts(ctx.state_dir, names, chosen), top=hub_top(MEMORY, token, view))

    def click(self, ctx, event, value, owner):
        entry = self.menu.entry(ctx, event, value, unfolded=True)
        if entry is None:
            return 'memory_unknown'
        token, path, record = entry
        if not owner:
            self.refuse(ctx, path, event)
            return 'owner_only'
        event_id = str(event.get('event_id') or '')
        if event_id and event_id in (record['view'].get('seen') or ()):
            return 'duplicate'
        changes = self.act(ctx, value, record['view'])
        after_ack(lambda: self.redraw(ctx, path, event, changes, event_id))
        return 'memory_tab'

    def redraw(self, ctx, path, event, changes, event_id=''):
        with self.menu.lock(path):
            record = self.menu.clicked(path, event)
            if record is None or record.get(FOLDED):
                return False
            view = {**record['view'], 'notice': '', **changes, 'token': path.stem}
            if event_id:
                view['seen'] = [*(view.get('seen') or ())][-(SEEN - 1):] + [event_id]
            view['revision'] += 1
            confirmed = self.menu.publish(ctx, path, record, view, self.draw(ctx, path.stem, view))
        if not confirmed and view.get('notice'):
            self.menu.unsaved(ctx, record, view['notice'], f"{path.stem}-{view['revision']}")
        return True

    def act(self, ctx, value, view):
        """The view's changes for the click; the store written first when it deletes or pins (no Feishu call)."""
        kind = value.get('kind')
        if kind == WORKSPACE:
            _, names = workspace_names(ctx)
            option = value.get('option')
            return {'workspace': option, 'confirm': None} if isinstance(option, str) and option in names else {}
        if kind == OPEN:
            scope, page = value.get('scope'), value.get('page')
            if scope not in ('', *memory.SCOPES) or isinstance(page, bool) or not isinstance(page, int) or page < 0:
                return {}
            return {'open': scope, 'page': page, 'confirm': None}
        key, op = value.get('key'), value.get('op')
        if not isinstance(key, str):
            return {}
        if kind == DELETE:
            if op == 'ask':
                return {'confirm': key}
            if op == 'cancel':
                return {'confirm': None}
            if op != 'confirm':
                return {}
            text = forget(ctx.state_dir, key)
            self.runtime.log(f'memory entry deleted from the 记忆 tab kind={key.partition(":")[0]} found={text is not None}')
            return {'confirm': None, 'notice': f'已删掉：{short(text)}' if text is not None else '这条已经不在了，卡片按现在的记忆重画了。'}
        if kind == PIN and key.startswith('principle:') and isinstance(value.get('on'), bool):
            try:
                found = principles.pin(ctx.state_dir, key.partition(':')[2], value['on'])
            except ValueError as exc:
                return {'notice': f'⚠️ {exc}'}
            if found is None:
                return {'notice': '这条已经不在了，卡片按现在的记忆重画了。'}
            self.runtime.log(f'memory entry pinned={value["on"]} from the 记忆 tab')
            return {'notice': ('已标常驻：' if value['on'] else '已取消常驻：') + short(found['text'])}
        return {}

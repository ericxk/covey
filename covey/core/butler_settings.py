"""The butler's settings (docs/butler.md): <state>/main-thread.json. Only the owner changes them, on the
settings card (app/butler_panel.py); the butler cannot change its own. A missing or bad field reads as
its default, so an older or hand-edited file still loads. The private chat mode (``enabled``) is read through
core/butler.py ``agent_mode``, the one rule every process (task runners, the MCP server) shares.

A topic the butler opens always starts at once (its only wait is a worktree's launch card). The settings 直接开工
(``start``) and 记住的工作区 (``trusted``) that decided it are retired (docs/legacy.md): a file that still has them
loads, they are not read and not written back. 独立复核 is retired too (docs/legacy.md): its model (``review``) is not
read, and a write keeps what the file has (``KEPT``) so an older release goes on reading it.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core import butler as contract
from covey.core.locks import resource_lock
from covey.core.roster import RETIRED
from covey.core.state import read_for_write, read_object, write_json

FILE = contract.SETTINGS_FILE
SCHEMA = 1
COMPACT_TOKENS = 200_000
RUN_MODES = ("direct", "worktree")  # core/workspaces.py RUN_MODES: 原目录, 独立 worktree.
# Which models Claude Code's subagents run on, in every Claude Code turn the Bridge starts (topics and the butler
# ; backends/claude/subagents.py): "light" Sonnet, with a read-only Explore on Haiku; "same" Claude Code's own
# way (each on the turn's model). Bot-wide, under 更多 on the 设置 card's 配置; missing or unknown reads as "light".
SUBAGENTS = ("light", "same")


@dataclasses.dataclass(frozen=True)
class TopicDefaults:
    """What a topic the butler opens runs with when the call does not say: "" is the Bridge's own default."""
    harness: str = ""  # A Harness name (core/roster.py NAMES); "" the Bridge's default Harness.
    model: str = ""
    effort: str = ""
    run_mode: str = "direct"
    redaction: bool = False
    workspace: str = ""  # "" the butler picks one; else always this workspace.


@dataclasses.dataclass(frozen=True)
class LeadModel:
    """What a new project's lead (core/projects.py, COVEY_PROJECTS=1) runs on, set on the 设置 card's 配置, 专项负责人: ""
    harness follows the butler (its Harness, model and budget as they are when the project is made); else this Harness's
    ``model`` with ``effort`` while it can run a lead (enabled, takes the Bridge's tools, and redacts where the butler
    does), the butler's otherwise (app/lead_harness.py). A lead that is there keeps its own: 换配置 on the 专项卡 changes it."""
    harness: str = ""
    model: str = ""
    effort: str = ""


@dataclasses.dataclass(frozen=True)
class AssistChoice:
    """辅助模型 (app/assist.py): what the background model calls (分流, 整理专项 and its 补话题要点, 记忆自己长) run on, one
    setting for all of them, set on the 设置 card's 配置 and the web page as the sections above it. ``harness`` "" and
    ``off`` false: 自动（跟随管家）, the butler's Harness, model and budget; ``off``: 不用模型 (no call at all, the rules
    alone); else only this Harness, never another one (one it cannot use now says why), on ``model`` with ``effort`` (""
    each: the Harness's own default). A Harness named here that is retired or not enabled stays named: the calls then say it
    cannot be used instead of picking another one."""
    harness: str = ""
    off: bool = False
    model: str = ""
    effort: str = ""


@dataclasses.dataclass(frozen=True)
class Settings:
    # The private chat mode (docs/butler.md 私聊模式): True 管家, False 纯话题, the chat's top level
    # opening topics as before. Saved as ``mode``, and as ``enabled`` for an older release.
    enabled: bool = True
    # The butler's own Harness (core/roster.py NAMES; "" follows what is enabled: Claude Code while it can run the butler,
    # else the first enabled Harness that can, app/butler_harness.py), model and budget; "" the Harness's default model. A
    # Harness named here that cannot run the butler (no Bridge tools, not enabled) is not replaced: app/butler.py says so
    # and the chat's messages open topics.
    harness: str = ""
    model: str = ""
    effort: str = ""
    # Its own turns through the term relay (core/redaction.py), from the next one: app/butler.py keep_topic
    # writes it into its topic.json, which the relay reads. Each request goes through whole, so the session goes on.
    redaction: bool = False
    workspace: str = ""  # Where the butler runs; "" the default workspace.
    compact_tokens: int = COMPACT_TOKENS  # Its context is compacted between turns past this; 0 never.
    topic_defaults: TopicDefaults = TopicDefaults()
    workspace_notes: tuple = ()  # (workspace name, one line on what it is for), in the owner's order.
    subagents: str = SUBAGENTS[0]  # SUBAGENTS.
    lead: LeadModel = LeadModel()  # A new project's lead's model (LeadModel); saved as ``lead``.
    assist: AssistChoice = AssistChoice()  # 辅助模型 (AssistChoice); saved as ``assist``, an older release ignores it.
    # Retired: main-thread.json's desk_topics opened front-desk topics in older releases, frozen since
    # (core/migrate.py desk-retire). It is no longer read, so every topic the butler
    #  opens is a 话题区; a file that says true gets one warning line at the Bridge's start
    # (``desk_topics_left``), and ``save`` keeps the value it has for an older release. Not a field: no one can set it.

    @property
    def butler_harness(self) -> str:
        """``harness``, or Claude Code: what the file says, knowing nothing of what is enabled. The Harness the butler runs
        on is app/butler_harness.py's (name_of, chosen), which fills an empty one from the enabled Harnesses."""
        return self.harness or contract.HARNESS

    def note(self, workspace: str) -> str:
        return dict(self.workspace_notes).get(workspace, "")


def _text(value, default=""):
    return value.strip() if isinstance(value, str) else default


def _choice(row):
    """(harness, model, effort) of a settings section ``row``; all "" when its Harness is retired (core/roster.py
    RETIRED: Pi, chosen before it went), so the Bridge picks as if none were chosen instead of a Harness it has no more."""
    harness = _text(row.get("harness"))
    if harness in RETIRED:
        return "", "", ""
    return harness, _text(row.get("model")), _text(row.get("effort"))


def from_json(data) -> Settings:
    data = data if isinstance(data, dict) else {}
    base, topic = Settings(), data.get("topic_defaults") if isinstance(data.get("topic_defaults"), dict) else {}
    run_mode = _text(topic.get("run_mode"), "direct")
    compact = data.get("compact_tokens")
    notes = data.get("workspace_notes")
    lead = data.get("lead") if isinstance(data.get("lead"), dict) else {}
    assist = data.get("assist") if isinstance(data.get("assist"), dict) else {}
    harness, model, effort = _choice(data)
    return Settings(
        enabled=contract.agent_mode(data),
        harness=harness, model=model, effort=effort, redaction=data.get("redaction") is True,
        workspace=_text(data.get("workspace")),
        compact_tokens=compact if type(compact) is int and compact >= 0 else base.compact_tokens,
        topic_defaults=TopicDefaults(
            **dict(zip(("harness", "model", "effort"), _choice(topic))),
            run_mode=run_mode if run_mode in RUN_MODES else "direct",
            redaction=topic.get("redaction") is True, workspace=_text(topic.get("workspace"))),
        workspace_notes=tuple((str(k), str(v)) for k, v in notes.items() if isinstance(v, str))
        if isinstance(notes, dict) else (),
        subagents=data["subagents"] if data.get("subagents") in SUBAGENTS else base.subagents,
        lead=LeadModel(*_choice(lead)),
        # Not through _choice: a retired Harness named here stays named (the calls say it is gone), never 自动.
        assist=AssistChoice(harness=_text(assist.get("harness")), off=assist.get("off") is True,
                            **({"model": _text(assist.get("model")), "effort": _text(assist.get("effort"))}
                               if _text(assist.get("harness")) else {})))


def to_json(settings: Settings) -> dict:
    data = dataclasses.asdict(settings)
    data.update(schema_version=SCHEMA, mode=contract.AGENT if settings.enabled else contract.TOPICS,
                workspace_notes=dict(settings.workspace_notes))
    return data


def path(state_dir) -> Path:
    return Path(state_dir) / FILE


def load(state_dir) -> Settings:
    return from_json(read_object(path(state_dir)))


# The Bridge's start logs it once when main-thread.json still says desk_topics true (app/unified.py).
DESK_TOPICS_RETIRED = ("warning: main-thread.json desk_topics is retired and read as false: topics the "
                       "main thread opens hang under the owner's question; old front-desk topics are frozen (core/migrate.py desk-retire)")


def desk_topics_left(state_dir) -> bool:
    """Whether main-thread.json still says desk_topics true, which no longer counts (no Settings field reads it): the
    Bridge logs a warning line at its start (app/unified.py). Never raises."""
    try:
        return read_object(path(state_dir)).get("desk_topics") is True
    except Exception:  # noqa: BLE001 - nothing to warn of
        return False


# What a write of the settings says when the file is there but cannot be read now (core/state.py Unreadable: nothing is
# written, so a network disk's passing error never resets them to the defaults). The settings card and the web page show it.
UNREADABLE = "设置文件暂时读不出，未改动：稍后再试"


def _for_write(state_dir) -> dict:
    """What main-thread.json holds, read under the caller's lock for a write: {} without a file; Unreadable when it is
    there and cannot be read (the write is refused)."""
    return read_for_write(path(state_dir))


# Keys of retired settings no Settings field reads, written back as the file has them so an older release goes on
# reading what it wrote: desk_topics (front-desk topics) and review (独立复核's model).
KEPT = ("desk_topics", "review")


def _write(state_dir, settings: Settings, data: dict) -> None:
    """``settings`` written, under the caller's lock, with the KEPT keys as the file has them (``data``, read by
    ``_for_write``; not Settings fields): an older release goes on reading what it wrote."""
    kept = {key: value for key, value in data.items() if key in KEPT}
    write_json(path(state_dir), {**kept, **to_json(settings)}, ensure_ascii=False, indent=1)


# Called after every write of the settings with (state directory, the settings before, after), outside the lock: what
# keeps something of the settings in memory (app/assist.py on_settings_changed) hears of it. A listener that raises is
# skipped.
_listeners: list = []


def listen(listener) -> None:
    """``listener(state_dir, before, after)`` after each ``save`` and ``update`` (once however often it is given)."""
    if listener not in _listeners:
        _listeners.append(listener)


def _told(state_dir, before, after) -> None:
    for listener in list(_listeners):
        try:
            listener(state_dir, before, after)
        except Exception:  # noqa: BLE001 - the write is done; a listener's trouble is its own
            pass


def save(state_dir, settings: Settings) -> None:
    """``settings`` written whole. Unreadable (nothing written) when the file is there and cannot be read now."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = _for_write(state_dir)
        before = from_json(data)
        _write(state_dir, settings, data)
    contract.forget_mode()
    _told(state_dir, before, settings)


def update(state_dir, **changes) -> Settings:
    """Read, change the fields given, write; the new settings. One writer at a time (the settings card). Unreadable
    (nothing written: the fields not given would go back to their defaults) when the file is there and cannot be read now."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = _for_write(state_dir)
        before = from_json(data)
        settings = dataclasses.replace(before, **changes)
        _write(state_dir, settings, data)
    contract.forget_mode()
    _told(state_dir, before, settings)
    return settings


def merge_topic_defaults(state_dir, **fields) -> None:
    """``fields`` of topic_defaults written as they are, the rest of the file left as it is (no field it lacks, the mode
    least of all: ``settle_mode`` writes that), for a migration (core/migrate.py redaction-default)."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = _for_write(state_dir)
        topic = data.get("topic_defaults") if isinstance(data.get("topic_defaults"), dict) else {}
        write_json(path(state_dir), {**data, "topic_defaults": {**topic, **fields}}, ensure_ascii=False, indent=1)
    contract.forget_mode()
    _told(state_dir, None, None)


def preset_harness(state_dir, harness) -> bool:
    """The butler's Harness written into a settings file that names none yet, the rest of the file left as it is (no mode:
    ``settle_mode`` writes that at the Bridge's first start, 管家 for a new deployment): what ./install does for a new service
    where Claude Code did not answer and this Harness did (service/installer.py butler_preset, docs/install.md 没有 Claude
    Code 时). A file that names a Harness (the owner chose it on the 设置 card) stays as it is. Whether it was written."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = _for_write(state_dir)
        if _text(data.get("harness")):
            return False
        write_json(path(state_dir), {**data, "harness": harness}, ensure_ascii=False, indent=1)
    contract.forget_mode()
    _told(state_dir, None, None)
    return True


def _fresh(state_dir) -> bool:
    """No job and no topic yet: a new deployment's first start."""
    jobs, topics = Path(state_dir) / "jobs", Path(state_dir) / "topics"
    return not (jobs.is_dir() and any(jobs.glob("*.json"))) \
        and not (topics.is_dir() and any(p.name != "aliases" and p.is_dir() for p in topics.iterdir()))


def settle_mode(state_dir):
    """Write the private chat mode once, at the Bridge's start (core/migrate.py private-mode): 管家 for a new
    deployment, else what the settings said without one (``agent_mode``: on only with COVEY_BUTLER=1 and
    the card's 开启), so a deployment the owner had keeps its way. From then on the mode alone decides, whatever the
    switch says (but 0). The mode written, or None when the file has one already."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = _for_write(state_dir)
        if data.get("mode") in contract.MODES:
            return None
        settings = dataclasses.replace(from_json(data), enabled=_fresh(state_dir) or contract.agent_mode(data))
        write_json(path(state_dir), {**data, **to_json(settings)}, ensure_ascii=False, indent=1)
    contract.forget_mode()
    _told(state_dir, None, None)
    return contract.AGENT if settings.enabled else contract.TOPICS

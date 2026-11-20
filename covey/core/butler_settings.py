"""The butler's settings (docs/butler.md): <state>/main-thread.json. Only the owner changes them, on the
settings card (app/butler_panel.py); the butler cannot change its own. A missing or bad field reads as
its default, so an older or hand-edited file still loads. The private chat mode (``enabled``) is read through
core/butler.py ``agent_mode``, the one rule every process (task runners, the MCP server) shares.

``starts_at_once`` is Persona's planStartsAtOnce (nwverse/persona server/internal/assistant/tasks.go): a topic the
butler opens starts at once when it only reads, when the owner asked for that very work in the message, or
when it is a kind (workspace × reads or edits) the owner told it to start without asking; any other waits on its
launch card for the owner's 开始任务.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from covey.core import butler as contract
from covey.core.locks import resource_lock
from covey.core.state import read_object, write_json

FILE = contract.SETTINGS_FILE
SCHEMA = 1
COMPACT_TOKENS = 200_000
RUN_MODES = ("direct", "worktree")  # core/workspaces.py RUN_MODES: 原目录, 独立 worktree.
TIERS = ("readonly", "requested", "trusted")  # What may start at once (starts_at_once).
# Which models Claude Code's subagents run on, in every Claude Code turn the Bridge starts (topics and the butler
# ; backends/claude/subagents.py): "light" Sonnet, with a read-only Explore on Haiku; "same" Claude Code's own
# way (each on the turn's model). Bot-wide, on the 设置 card's 新话题 tab; missing or unknown reads as "light".
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
class Settings:
    # The private chat mode (docs/butler.md 私聊模式): True 管家, False 纯话题, the chat's top level
    # opening topics as before. Saved as ``mode``, and as ``enabled`` for an older release.
    enabled: bool = True
    model: str = ""  # The butler's own Claude Code model; "" the default.
    effort: str = ""
    # Its own turns through the term relay (core/redaction.py), from the next one: app/butler.py keep_topic
    # writes it into its topic.json, which the relay reads. Each request goes through whole, so the session goes on.
    redaction: bool = False
    workspace: str = ""  # Where the butler runs; "" the default workspace.
    compact_tokens: int = COMPACT_TOKENS  # Its context is compacted between turns past this; 0 never.
    topic_defaults: TopicDefaults = TopicDefaults()
    start: tuple = TIERS  # The kinds of topic that start at once (TIERS).
    trusted: tuple = ()  # trust_key()s the owner told it to start without asking.
    workspace_notes: tuple = ()  # (workspace name, one line on what it is for), in the owner's order.
    subagents: str = SUBAGENTS[0]  # SUBAGENTS.
    # Retired: main-thread.json's desk_topics opened front-desk topics in older releases, frozen since
    # (core/migrate.py desk-retire). It is no longer read, so every topic the butler
    #  opens is a 话题区; a file that says true gets one warning line at the Bridge's start
    # (``desk_topics_left``), and ``save`` keeps the value it has for an older release. Not a field: no one can set it.

    def note(self, workspace: str) -> str:
        return dict(self.workspace_notes).get(workspace, "")


def trust_key(workspace: str, readonly: bool) -> str:
    return f"{workspace}|{'read' if readonly else 'edit'}"


def starts_at_once(settings: Settings, *, workspace: str, readonly: bool, user_requested: bool) -> bool:
    return (("readonly" in settings.start and bool(readonly))
            or ("requested" in settings.start and bool(user_requested))
            or ("trusted" in settings.start and trust_key(workspace, readonly) in settings.trusted))


def _text(value, default=""):
    return value.strip() if isinstance(value, str) else default


def from_json(data) -> Settings:
    data = data if isinstance(data, dict) else {}
    base, topic = Settings(), data.get("topic_defaults") if isinstance(data.get("topic_defaults"), dict) else {}
    run_mode = _text(topic.get("run_mode"), "direct")
    compact = data.get("compact_tokens")
    notes = data.get("workspace_notes")
    return Settings(
        enabled=contract.agent_mode(data),
        model=_text(data.get("model")), effort=_text(data.get("effort")), redaction=data.get("redaction") is True,
        workspace=_text(data.get("workspace")),
        compact_tokens=compact if type(compact) is int and compact >= 0 else base.compact_tokens,
        topic_defaults=TopicDefaults(
            harness=_text(topic.get("harness")), model=_text(topic.get("model")), effort=_text(topic.get("effort")),
            run_mode=run_mode if run_mode in RUN_MODES else "direct",
            redaction=topic.get("redaction") is True, workspace=_text(topic.get("workspace"))),
        start=tuple(t for t in TIERS if t in data["start"]) if isinstance(data.get("start"), list) else base.start,
        trusted=tuple(k for k in data.get("trusted") or () if isinstance(k, str) and "|" in k),
        workspace_notes=tuple((str(k), str(v)) for k, v in notes.items() if isinstance(v, str))
        if isinstance(notes, dict) else (),
        subagents=data["subagents"] if data.get("subagents") in SUBAGENTS else base.subagents)


def to_json(settings: Settings) -> dict:
    data = dataclasses.asdict(settings)
    data.update(schema_version=SCHEMA, mode=contract.AGENT if settings.enabled else contract.TOPICS,
                start=list(settings.start), trusted=list(settings.trusted), workspace_notes=dict(settings.workspace_notes))
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


def _write(state_dir, settings: Settings) -> None:
    """``settings`` written, under the caller's lock, with desk_topics as the file has it (not a Settings field):
    an older release goes on reading what it wrote."""
    kept = {key: value for key, value in read_object(path(state_dir)).items() if key == "desk_topics"}
    write_json(path(state_dir), {**kept, **to_json(settings)}, ensure_ascii=False, indent=1)


def save(state_dir, settings: Settings) -> None:
    with resource_lock("main-thread-settings", str(path(state_dir))):
        _write(state_dir, settings)
    contract.forget_mode()


def update(state_dir, **changes) -> Settings:
    """Read, change the fields given, write; the new settings. One writer at a time (the settings card)."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        settings = dataclasses.replace(load(state_dir), **changes)
        _write(state_dir, settings)
    contract.forget_mode()
    return settings


def merge_topic_defaults(state_dir, **fields) -> None:
    """``fields`` of topic_defaults written as they are, the rest of the file left as it is (no field it lacks, the mode
    least of all: ``settle_mode`` writes that), for a migration (core/migrate.py redaction-default)."""
    with resource_lock("main-thread-settings", str(path(state_dir))):
        data = read_object(path(state_dir))
        topic = data.get("topic_defaults") if isinstance(data.get("topic_defaults"), dict) else {}
        write_json(path(state_dir), {**data, "topic_defaults": {**topic, **fields}}, ensure_ascii=False, indent=1)
    contract.forget_mode()


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
        data = read_object(path(state_dir))
        if data.get("mode") in contract.MODES:
            return None
        settings = dataclasses.replace(from_json(data), enabled=_fresh(state_dir) or contract.agent_mode(data))
        write_json(path(state_dir), {**data, **to_json(settings)}, ensure_ascii=False, indent=1)
    contract.forget_mode()
    return contract.AGENT if settings.enabled else contract.TOPICS

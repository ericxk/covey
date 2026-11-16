"""The butler (管家), in the private chat mode 管家 (docs/butler.md 私聊模式): the owner's private
chat with the bot, at its top level, talks to one long-lived Claude Code session. It answers by itself, or with the
Bridge's tools it opens a topic, hands the message to a topic, forks one, or answers a topic's choice question for
the owner. Topics keep working as before; a reply in a topic's thread still goes to that topic. The other mode, 纯话题,
is the chat as it was before the butler: each top-level message opens a topic.

Two layers decide it (``enabled``): the deployment allows it unless COVEY_BUTLER=0 (``available``), and
the owner picks the mode on the settings card, saved as ``mode`` in <state>/main-thread.json (``agent_mode``).

The names every part shares: the switch, the butler's topic id, and its tool channel's entry and environment
(core/butler_requests.py carries the calls, core/butler_tool_specs.py names the tools). Standard library only (with
core/metrics.py, itself standard library only): the MCP server (bin/covey_butler_mcp.py) imports this module.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from covey.core import env as cenv
from covey.core import butler_tracks, metrics, projects

ENV = "COVEY_BUTLER"
MCP_SERVER = "bridge"  # The MCP server's name in the butler's session: its tools are mcp__bridge__<tool>.
MCP_ENTRY = "bin/covey_butler_mcp.py"  # Relative to the release the Bridge runs from.
STATE_ENV = "COVEY_BUTLER_STATE"  # Tells the MCP server the Bridge's state directory.
JOB_ENV = "COVEY_BUTLER_JOB"  # The key of the butler job whose session runs the server; the Bridge checks it.
LEAD_ENV = "COVEY_PROJECT_LEAD"  # The project's id, in the session of that project's lead only: it lists the lead's tools.


# The owner's settings (core/butler_settings.py), the mode among them: the file ``enabled`` reads.
SETTINGS_FILE = "main-thread.json"
AGENT, TOPICS = "agent", "topics"  # The private chat modes: 管家, 纯话题.
MODES = (AGENT, TOPICS)


def _switch() -> str:
    return cenv.get(ENV, "").strip()


def available() -> bool:
    """Whether this deployment lets the butler run: unless COVEY_BUTLER=0. Then the settings card has
    its 管家 tab and the butler's workers run; whether the chat talks to it is the mode (``enabled``)."""
    return _switch() != "0"


def agent_mode(data) -> bool:
    """Whether the settings ``data`` (main-thread.json, a mapping) choose 管家: its ``mode``; without one
    (written before 2026-11-08, or never written) as then: on only with COVEY_BUTLER=1 and the card's
    ``enabled`` not false. The Bridge's start writes the mode once (core/migrate.py private-mode)."""
    data = data if isinstance(data, Mapping) else {}
    mode = data.get("mode")
    if mode in MODES:
        return mode == AGENT
    return _switch() == "1" and data.get("enabled") is not False


_mode_seen = None  # (settings file, its stat, the switch, agent_mode): the last read of the mode (``enabled``).


def enabled() -> bool:
    """Whether the butler is on: ``available`` and the mode 管家, read from the state directory
    ``configure_state`` set (none: as main-thread.json without a mode says). With it off nothing of the butler
    happens and the chat works as before it; its records stay. One stat a call; the file is read again only once it
    changed."""
    if not available():
        return False
    global _mode_seen
    if _state is None:
        return agent_mode({})
    path, switch = _state / SETTINGS_FILE, _switch()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError:
        stamp = None
    seen = _mode_seen
    if seen is not None and seen[:3] == (path, stamp, switch):
        return seen[3]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if stamp is not None else {}
    except (OSError, ValueError):
        data = {}
    on = agent_mode(data)
    _mode_seen = (path, stamp, switch, on)
    return on


def forget_mode() -> None:
    """The settings file was just written in this process: ``enabled`` reads it again, whatever its stat says."""
    global _mode_seen
    _mode_seen = None


def butler_topic_id(chat_id: str) -> str:
    """The id of the butler's topic in the owner's private chat ``chat_id``: hex like every topic id
    (app/topics.py Topics.target takes [0-9a-f]{16,128} only), and no message's alias, so no event routes to it.
    Slot 0 of the family (core/butler_tracks.py): the other slots' ids are ``track_topic_id``."""
    return butler_tracks.root_topic_id(chat_id)


# A butler message with no words of its own (a forward, a card, a file, a bare link) waits this long for the
# owner's words before its turn starts (app/butler.py Butler.hold_for); the card says so.
HOLD_SECONDS = 30
HOLD_STATUS = f"收到，等你说要做什么（最多 {HOLD_SECONDS} 秒）"
# In the 话题区 layout a butler turn's task card goes out (in the owner's message's thread) only once it has run
# this long (core/placement.py Row.quiet, D1 2026-11-08): most turns end before, with no thread opened.
CARD_AFTER = 90


def is_butler_topic(topic_id, chat_id) -> bool:
    """The one predicate: whether ``topic_id`` is the butler's topic in ``chat_id`` (butler_topic_id), whether
    or not the switch is on now. Only app/butler.py Butler.keep_topic writes that topic's topic.json, with
    ``main_thread: true``, so for every record the product writes this says the same as that flag.
    The family (core/butler_tracks.py): with COVEY_BUTLER_TRACKS=1 the topics of slots 1..4 count too, each a track of the
    butler; with it off only slot 0, as before. A project's lead topic (core/projects.py, COVEY_PROJECTS=1) is of
    the family too (``is_lead``). The call sites that mean the root alone ask ``is_butler_root``."""
    if not topic_id:
        return False
    return butler_tracks.track_of(topic_id, str(chat_id or "")) is not None or is_lead(topic_id, chat_id)


def is_lead(topic_id, chat_id) -> bool:
    """Whether ``topic_id`` is a project's lead topic in ``chat_id`` (core/projects.py is_lead_topic, read from the state
    directory ``configure_state`` set): False with COVEY_PROJECTS off, and in a process with no state directory."""
    return bool(topic_id) and _state is not None and projects.is_lead_topic(_state, topic_id, str(chat_id or "") or None)


def is_butler_root(topic_id, chat_id) -> bool:
    """Whether ``topic_id`` is slot 0, the butler's own topic (butler_topic_id), the switch of the tracks aside:
    the one whose session the tracks fork, whose snapshot and settings are the butler's."""
    return bool(topic_id) and topic_id == butler_topic_id(str(chat_id or ""))


@dataclass(frozen=True)
class Traits:
    """What the butler does differently from a topic (docs/butler.md 和话题的不同), one row each. A True
    field is a topic's way; a False one is ruled out here, whatever else (the Harness, a 代答 topic, a group) may
    rule out too. The call sites ask ``traits``/``traits_of``, never compare topic ids themselves."""

    name: str
    # Its topic: the butler's topic.json has no root message and no T number, whether the switch is on or not.
    switch_config: bool  # 🔁 换配置 on its task cards (app/switch_handoff.py, cards/streaming.py).
    rewind: bool  # ↩️ 回退 on its finished task cards (app/switch_handoff.py can_rewind).
    card_target: bool  # A card that names it can address it (app/topics.py Topics.target), as 换配置 does.
    numbered: bool  # Gets a T number (core/topics.py topic_number).
    top_level: bool  # Answers quoting each message at the chat's top level, not in a thread (core/topics.py).
    listed: bool  # Pinned on the board and listed on 手上的事 (core/board_read.py read, app/overview.py view).
    # Its session: only with the butler on (``enabled``: COVEY_BUTLER not 0, the mode 管家).
    own_turn: bool  # Its rules, snapshot and tools open each turn, compacted past compact_tokens (app/butler.py).
    answers_in_place: bool  # The reply is the task card's body, no answer card (app/butler_reply.py answers_in_place).
    lists_supplements: bool  # Its supplements have no receipt card: a stopped or refused card lists them
    # (app/butler_reply.py show_dropped), one not taken gets a line under the owner's message (app/steering.py).
    hold_seconds: int  # A message with no words of its own waits this long for them (app/butler.py hold_for).
    # Its task card is the slim one (cards/streaming.py build_stream_card): no title bar, the body, one status line,
    # and the rest folded in 详情; a card sent before keeps its look (app/task_card.py StreamingReply.slim).
    slim_card: bool
    wording: tuple  # (topic's phrase, its own) pairs every Bridge text on its cards goes through (``worded``).
    # Where its turns' words go (core/placement.py adds the layout: the butler on or off).
    relays: bool  # A turn the butler started or handed on relays to the chat's top level (app/butler_relay.py).
    has_thread: bool  # It has a thread of its own: its status card (app/status_cards.py), 管家转来 lines in it.
    linkify_body: bool  # T numbers in its task card's body are links to those topics (app/worker.py, topic_links).


TOPIC = Traits("topic", switch_config=True, rewind=True, card_target=True, numbered=True,
               top_level=False, listed=True, own_turn=False, answers_in_place=False, lists_supplements=False,
               hold_seconds=0, wording=(), slim_card=False, relays=True, has_thread=True, linkify_body=False)
# What a text written for a topic says on the butler, which is no topic: the private chat's top level. In
# order, the longer phrase first.
WORDING = (("本话题前方", "管家前方"), ("本话题的", "管家的"), ("本话题", "私聊里"))
BUTLER = Traits("butler", switch_config=False, rewind=False, card_target=False, numbered=False,
              top_level=True, listed=False, own_turn=True, answers_in_place=True, lists_supplements=True,
              hold_seconds=HOLD_SECONDS, wording=WORDING, slim_card=True, relays=False, has_thread=False,
              linkify_body=True)
# The butler's topic with the butler off now (the switch 0 or the mode 纯话题; its records stay): its topic is still no topic's, but its
# session's ways are gone, so its texts read as a topic's again. The two old predicates, topic.json's flag and the
# switch-gated id check, parted here; this row keeps what each call site did.
BUTLER_OFF = dataclasses.replace(BUTLER, name="butler_off", own_turn=False, answers_in_place=False, lists_supplements=False,
                               hold_seconds=0, wording=(), slim_card=False, linkify_body=False)
# A project's lead topic (core/projects.py; COVEY_PROJECTS): the butler's ways, as its family member, except that no
# owner's message waits for words (its wake-up turns have none). Its session's rules and tools are phase 2's (app/project_lead.py):
# until then ``own_turn`` only says the turn is not an ordinary topic's.
LEAD = dataclasses.replace(BUTLER, name="lead", hold_seconds=0)
LEAD_OFF = dataclasses.replace(LEAD, name="lead_off", own_turn=False, answers_in_place=False, lists_supplements=False,
                               wording=(), slim_card=False, linkify_body=False)


_state = None  # The process's state directory (``configure_state``); None: the mode as main-thread.json without one.


def configure_state(state_dir):
    """Set the state directory whose settings ``enabled`` reads, in this process (the Bridge and every task runner:
    app/butler.py Butler.configure); None, none. The previous one, to restore."""
    global _state
    previous, _state = _state, (Path(state_dir) if state_dir else None)
    if _state != previous:
        forget_mode()
    return previous


def run_seconds(job):
    """How long ``job``'s run took, by its timings: its 用时 as every card says it (core/metrics.py run_seconds); None
    when no start is known."""
    return metrics.run_seconds(getattr(job, "timings", None))


def traits(topic_id, chat_id) -> Traits:
    """The row of ``topic_id`` in ``chat_id``: BUTLER, BUTLER_OFF with the butler off (``enabled``: the switch or
    the mode 纯话题), TOPIC for every other (an old front-desk topic too: retired, core/topics.py retired_topic; a
    project's thread too). A project's lead topic is LEAD, LEAD_OFF with the butler off."""
    if not topic_id:
        return TOPIC
    if is_lead(topic_id, chat_id):
        return LEAD if enabled() else LEAD_OFF
    if not is_butler_topic(topic_id, chat_id):
        return TOPIC
    return BUTLER if enabled() else BUTLER_OFF


def traits_of(of) -> Traits:
    """``traits`` of a job (its ``topic_id`` and ``chat_id``), a job record or topic.json (a mapping with those
    keys), or a row already looked up. A context has no chat: core/topics.py topic_traits reads its topic.json."""
    if isinstance(of, Traits):
        return of
    if isinstance(of, Mapping):
        return traits(of.get("topic_id"), of.get("chat_id"))
    return traits(getattr(of, "topic_id", ""), getattr(of, "chat_id", ""))


def worded(text: str, of) -> str:
    """``text`` as the butler says it when ``of`` (as ``traits_of`` takes) is the butler's; unchanged
    otherwise. Every text that can reach a butler card or line goes through here."""
    wording = traits_of(of).wording
    if not wording or not isinstance(text, str):
        return text
    for topic, here in wording:
        text = text.replace(topic, here)
    return text


def mcp_config(release: Path, state_dir: Path, job_key: str, python: str, lead: str = "") -> dict:
    """The --mcp-config of a butler job's Claude Code: the Bridge's tools and nothing else. The variables that
    switch on the knowledge and history tools and the butler's wording of topic_start (core/butler_tool_specs.py listed) are
    handed on when the Bridge has them. ``lead``: the id of the project whose lead's job it is (COVEY_PROJECTS on): the
    server then lists the lead's tools (project_*) instead (app/project_lead.py)."""
    env = {}
    handed = {STATE_ENV: str(state_dir), JOB_ENV: job_key,
              "COVEY_KNOWLEDGE": cenv.get("COVEY_KNOWLEDGE", "").strip(),  # core/butler_tool_specs.py
              "COVEY_HISTORY_CMD": cenv.get("COVEY_HISTORY_CMD", "").strip(),  # KNOWLEDGE_ENV, HISTORY_ENV
              "COVEY_BUTLER_RULES": cenv.get("COVEY_BUTLER_RULES", "").strip(),  # BUTLER_RULES_ENV
              "COVEY_PROJECTS": cenv.get("COVEY_PROJECTS", "").strip(),  # PROJECTS_ENV: the butler's project tools
              "COVEY_PROJECT_TRIAGE": cenv.get("COVEY_PROJECT_TRIAGE", "").strip()}  # TRIAGE_ENV: the lead's question tools
    if lead and projects.enabled():
        handed[LEAD_ENV] = lead
    for name, value in handed.items():
        if value:
            cenv.put(env, name, value)  # Both spellings: the server may be an older release's.
    return {"mcpServers": {MCP_SERVER: {"command": python, "args": ["-B", str(Path(release) / MCP_ENTRY)], "env": env}}}

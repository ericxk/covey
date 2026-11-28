"""Rules sent (WAVE12 C9, W1-E): the Bridge's fixed rules at the system level of each Harness's session, so a resumed turn
no longer restates them in its message, and a compaction of the conversation cannot drop them.

A resumed turn used to restate its rules whole in its message every turn (app/runs.py ``_with_rules``: the reminders, the
主目录段, the memory's 规矩, the checkpoint contract, about 6,500 characters), and a compaction could summarize them away
(the owner asked, 11-28, for the system level instead). Each core Harness has a system channel (core/harness.py
HarnessCapabilities ``system_instructions``), and each keeps it its own way (``system_refresh``, measured 11-28 against
a local fake model service):

- ``"turn"`` (dsh: its patch's ``personaSuffix``; agy: the private home's ``~/.gemini/GEMINI.md``): read again at every
  turn. A change there would cost the whole conversation's cache (the system prompt is its prefix), so the session keeps
  its own text (``pinned_text``, under ``<topic>/rules-text/``) until a compaction, and is told a newer one as below.
- ``"compaction"`` (Claude Code: ``--append-system-prompt`` on every invocation): a session keeps the system prompt of its
  first invocation, until a compaction renders it again from the invocation's own.
- ``""`` (Codex: a new thread's ``developerInstructions``): kept in the thread's history from its start, through a
  compaction too; nothing changes it afterwards.

So on a Harness that does not read them again, a session's rules may be an older version than the turn's (a deploy
changed them, the memory's 规矩 grew). ``plan`` says what the turn's message carries:

- ``SYSTEM``: nothing, the session has them (a new session; the version it has is the turn's; or the turn's was told in a
  message already, ``OVERRIDE``, and the conversation is ``intact`` since).
- ``OVERRIDE``: the turn's rules whole in its message, headed by ``OVERRIDE_HEAD`` (they win over the session's own).

``intact``: no compaction in the previous turn (``compactions``) or before this one (``compact_first``), no fall of the
previous turn's ``context_tokens`` by more than ``DROP`` from the one before (a compaction no event said), the previous
turn ended well and its checkpoint did not say ``rules_missing: true``, and fewer than ``N_FULL`` turns since the
override. A lead's state pack says only what changed while it holds (app/project_lead.py), the butler's snapshot is
whole when it does not.

The record, ``<state>/topics/<topic>/rules-sent.json`` ({"v":2,"sessions":{"<harness>:<session_id>":{"system",
"override","turns","binding"}}}), is written only after a turn that ended well (``settle``): ``system`` the version the
session has at the system level, ``override`` the version a message told it since. A fork's first turn reads its
source's record; a ↩️ 回退 (``forget``) and a 🔁 换配置 (``binding``) count as none. A session with no system rules (one from
before, or the switch off) gets its rules in each resumed turn's message, as before.

COVEY_SYSTEM_RULES=0: no system level at all, every turn as before (read each turn: no deploy to go back).
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from covey.core import env as cenv
from covey.core.checkpoint import read_checkpoint
from covey.core.jobs import job_key, job_path, read_job_record
from covey.core.locks import resource_lock
from covey.core.state import Unreadable, read_object, update_json

SYSTEM, OVERRIDE = "system", "override"
MODES = (SYSTEM, OVERRIDE)
FULL = "full"  # What a turn without system rules sends (its job record's ``rules_sent``), as before.
N_FULL = 8  # Turns after an override before it is told again.
REBUILD_TURNS_FALLBACK = 12  # A lead's turn without context_tokens: its session is rebuilt after this many in a row (project_lead.py).
DROP = 0.3  # The previous turn's context this much smaller than the one before: a compaction no event said.
ENV = "COVEY_SYSTEM_RULES"  # "0": every turn's rules in its message, as before.
FILE = "rules-sent.json"
SESSIONS_KEPT = 20  # Sessions a topic's record keeps; the oldest go.
GOOD_ENDS = ("done", "waiting_user", "continued")  # A turn that ended well: its prompt is in its session.
MISSING = "rules_missing"  # A checkpoint's field: the turn could not see its rules.
REREAD, ON_COMPACTION = "turn", "compaction"  # HarnessCapabilities.system_refresh's values; "" a session's start only.
OVERRIDE_HEAD = ("[Bridge 规则有新版：这个会话系统提示里的 Bridge 规则是旧的，下面是现在的全文（v{version}），"
                 "和系统提示里的不一样时以这份为准]")


def enabled(environ=None) -> bool:
    """Whether the rules go at the system level where the Harness has a channel: COVEY_SYSTEM_RULES not 0/off."""
    return str(cenv.get(ENV, "", environ) or "").strip().lower() not in ("0", "off", "false", "no")


def version(text: str) -> str:
    """The rules' sha12."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


@dataclasses.dataclass(frozen=True)
class Plan:
    """A turn's rules: ``mode`` (SYSTEM or OVERRIDE) and why (``reason``), their version (``current``), the Harness's
    ``refresh``, whether the conversation is ``intact`` since the previous turn, the session's record as it stood."""
    mode: str
    reason: str
    current: str
    refresh: str = ""
    intact: bool = True
    entry: dict = dataclasses.field(default_factory=dict)
    # On a Harness that reads them again at every turn: the session's own text, kept for it (``pinned_text``), to put
    # at the system level in place of the turn's, so its prompt's prefix, and its cache, stay as they were; '' none.
    pinned: str = dataclasses.field(default="", repr=False)
    text: str = dataclasses.field(default="", repr=False)  # The turn's rules, kept for the session when it takes them.


def path(state_dir, topic_id) -> Path:
    return Path(state_dir) / "topics" / str(topic_id) / FILE


def _key(harness: str, session_id: str) -> str:
    return f"{harness}:{session_id}"


def _topic(ctx, job) -> str:
    return str(getattr(job, "topic_id", "") or getattr(ctx, "topic_id", "") or "")


def _harness(ctx, job) -> str:
    return str(getattr(job, "result_backend", "") or getattr(ctx, "backend", "") or "")


def binding(ctx) -> str:
    """The topic's binding as a turn runs it: its revision (topic.json ``binding_revision``, a 🔁 换配置 adds one) and model."""
    from covey.core.topics import read_topic

    try:
        revision = read_topic(ctx).get("binding_revision", 1)
    except (OSError, ValueError, AttributeError):
        revision = 1
    return f"{revision}:{getattr(ctx, 'model', '') or ''}"


def entry(ctx, job, session_id: str | None) -> dict:
    """The record of ``session_id`` for the turn's Harness, or, without one (a fork's first turn), of the session it forks
    (``ctx.options`` ``fork_session``); {} without either, or when its binding is not the topic's now."""
    topic = _topic(ctx, job)
    fork = (getattr(ctx, "options", None) or {}).get("fork_session") or ""
    if not topic or not (session_id or fork):
        return {}
    sessions = read_object(path(ctx.state_dir, topic)).get("sessions")
    sessions = sessions if isinstance(sessions, dict) else {}
    for sid in (session_id, fork):
        found = sessions.get(_key(_harness(ctx, job), str(sid))) if sid else None
        if isinstance(found, dict):
            return dict(found) if found.get("binding") in (None, binding(ctx)) else {}
    return {}


def _int(value) -> int:
    return value if type(value) is int and value > 0 else 0


def intact(ctx, previous: dict | None, before: dict | None, found: dict | None = None) -> tuple[bool, str]:
    """(whether the conversation holds what the turns before told it, why not): see the module's docstring."""
    if (getattr(ctx, "options", None) or {}).get("compact_first"):
        return False, "compacted"  # Compacted before this turn's prompt (the butler's request).
    previous = previous if isinstance(previous, dict) else {}
    if _int(previous.get("compactions")):
        return False, "compacted"
    now, then = _int(previous.get("context_tokens")), _int((before or {}).get("context_tokens"))
    if now and then and now < then * (1 - DROP):
        return False, "context_dropped"
    if previous and previous.get("status") not in GOOD_ENDS:
        return False, "previous_failed"
    if previous.get("checkpoint_path"):
        try:
            if read_checkpoint(Path(previous["checkpoint_path"])).get(MISSING) is True:
                return False, MISSING
        except (OSError, ValueError, TypeError):
            pass
    if found and found.get("override") and _int(found.get("turns")) >= N_FULL:
        return False, "every_n"
    return True, ""


CACHE_GONE = ("compacted", "context_dropped")  # An ``intact`` reason after which the cache has nothing to keep.


def pinned_path(state_dir, topic_id, version_: str) -> Path:
    """Where a session's own rules of ``version_`` are kept (a Harness that reads them again at every turn)."""
    return Path(state_dir) / "topics" / str(topic_id) / "rules-text" / f"{version_}.md"


def pinned_text(ctx, job, found: dict) -> str:
    """The rules ``found`` (a session's record) says the session has, as kept (``pinned_path``); '' when not kept."""
    sha = str((found or {}).get("system") or "")
    if not sha or not _topic(ctx, job):
        return ""
    try:
        text = pinned_path(ctx.state_dir, _topic(ctx, job), sha).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return text if version(text) == sha else ""


def decide(ctx, job, session_id, *, current: str, refresh: str, previous: dict | None,
           before: dict | None) -> tuple[str, str, bool]:
    """(mode, reason, intact) of the turn whose rules are of version ``current`` on a Harness of ``refresh``;
    ``previous`` the topic's last turn's job record before this one and ``before`` the one before that (None: none).
    A Harness that reads them again at every turn keeps a session's own (``pinned_text``) at the system level: its
    prompt's prefix stays the same, and its cache with it; it takes the turn's in a new session, when it has none kept,
    and after a compaction (``CACHE_GONE``), else is told them as any other (OVERRIDE)."""
    found = entry(ctx, job, session_id)
    held, why = intact(ctx, previous, before, found)
    if not session_id and not (getattr(ctx, "options", None) or {}).get("fork_session"):
        return SYSTEM, "new_session", True
    if refresh == REREAD:
        if found.get("system") == current:
            return SYSTEM, "", held
        if not pinned_text(ctx, job, found):
            return SYSTEM, "reread", held  # Nothing kept: the turn's go there, and are kept from now on.
        if why in CACHE_GONE:
            return SYSTEM, "refresh", held  # The cache has nothing left to keep: the turn's go there.
    if found.get("system") == current:
        return SYSTEM, "", held
    if found.get("override") == current and held:
        return SYSTEM, "told", held
    return OVERRIDE, why or ("changed" if found.get("system") else "unknown"), held


def plan(ctx, job, session_id, *, current: str, refresh: str, previous: dict | None, before: dict | None) -> str:
    """The turn's mode (``decide``): SYSTEM or OVERRIDE."""
    return decide(ctx, job, session_id, current=current, refresh=refresh, previous=previous, before=before)[0]


def make(ctx, job, session_id, *, current: str, refresh: str, previous: dict | None, before: dict | None,
         text: str = "") -> Plan:
    """``decide`` as a Plan, with the session's record, and on a Harness that reads them again the session's own rules
    to keep at the system level (``pinned``: '' when the turn's, ``text``, go there)."""
    mode, reason, held = decide(ctx, job, session_id, current=current, refresh=refresh, previous=previous, before=before)
    found = entry(ctx, job, session_id)
    keep = refresh == REREAD and reason not in ("new_session", "reread", "refresh") and found.get("system") != current
    return Plan(mode, reason, current, refresh, held, found, pinned=pinned_text(ctx, job, found) if keep else "",
                text=text)


def system_text(found, rules: str) -> str:
    """What goes at the system level this turn: the session's own rules when the plan keeps them, else ``rules``."""
    return found.pinned if isinstance(found, Plan) and found.pinned else rules


def mode_of(job) -> str:
    """The mode of ``job``'s turn (its ``rules_plan``): SYSTEM, OVERRIDE, or FULL without one (its rules in its message)."""
    found = getattr(job, "rules_plan", None)
    return found.mode if isinstance(found, Plan) else FULL


def at_system(job) -> bool:
    """Whether ``job``'s turn has its rules at the system level (a plan): the butler's and a lead's own go there too."""
    return isinstance(getattr(job, "rules_plan", None), Plan)


def lost(job) -> bool:
    """Whether ``job``'s turn has a plan that says the conversation may have lost what earlier turns told it."""
    found = getattr(job, "rules_plan", None)
    return isinstance(found, Plan) and not found.intact


def override(text: str, current: str) -> str:
    """The turn's rules in its message (OVERRIDE), under OVERRIDE_HEAD."""
    return OVERRIDE_HEAD.format(version=current) + "\n" + text


def record(ctx, job, session_id, found: Plan, *, compacted: bool = False) -> None:
    """After a turn that ended well (the caller's to know): ``session_id``'s record says what it has. A new session (or one
    a compaction rendered again, on a Harness of ``ON_COMPACTION``): the turn's version at the system level; an override:
    the turn's version told; else one turn more since the override. A Harness that reads them again keeps none. A fork's
    first turn starts from its source's record. Unreadable leaves the file as it was."""
    topic, harness = _topic(ctx, job), _harness(ctx, job)
    if not session_id or not topic or not harness or found.mode not in MODES:
        return
    target = path(ctx.state_dir, topic)
    base = entry(ctx, job, session_id)
    now = binding(ctx)
    renewed = (found.reason in ("new_session", "reread", "refresh") if found.refresh == REREAD else
               found.reason == "new_session" or (compacted and found.refresh == ON_COMPACTION))
    if found.refresh == REREAD and renewed:
        if not found.text:
            return  # Nothing to keep: the next turn takes its own again.
        kept = pinned_path(ctx.state_dir, topic, found.current)
        if not kept.is_file():
            from covey.core.state import write_private_text

            write_private_text(kept, found.text)

    def change(saved):
        sessions = saved.get("sessions") if isinstance(saved.get("sessions"), dict) else {}
        item = dict(sessions.get(_key(harness, session_id)) or base)
        if renewed:
            item = {"system": found.current, "override": "", "turns": 0}
        elif found.mode == OVERRIDE:
            item.update(override=found.current, turns=0)
        else:
            item["turns"] = _int(item.get("turns")) + 1
        item["binding"] = now
        sessions.pop(_key(harness, session_id), None)
        sessions[_key(harness, session_id)] = item
        while len(sessions) > SESSIONS_KEPT:
            sessions.pop(next(iter(sessions)))
        return {"v": 2, "sessions": sessions}

    with resource_lock("record", target.absolute()):
        update_json(target, change)


def forget(state_dir, topic_id, harness: str = "", session_id: str = "") -> None:
    """The record of one session (or, without ``session_id``, every session of the topic) gone: a ↩️ 回退 cut the session
    back, the next turn tells the rules again. Never raises."""
    target = path(state_dir, topic_id)
    if not topic_id or not target.is_file():
        return

    def change(saved):
        sessions = saved.get("sessions") if isinstance(saved.get("sessions"), dict) else {}
        if session_id:
            sessions.pop(_key(harness, session_id), None)
        else:
            sessions.clear()
        return {"v": 2, "sessions": sessions}

    try:
        with resource_lock("record", target.absolute()):
            update_json(target, change)
    except (OSError, ValueError, Unreadable):
        return


def settle(ctx, job, status: str, log=None) -> None:
    """``record`` for ``job``'s turn when it ended well (``status`` in done/waiting_user) and planned its rules
    (``rules_plan``), under the session it ended in; ``compacted`` from its job record's ``compactions``. Never raises:
    the turn is over."""
    found = getattr(job, "rules_plan", None)
    if not isinstance(found, Plan) or status not in ("done", "waiting_user"):
        return
    try:
        key = job_key(job.message_id, job.job_id)
        compacted = _int((read_job_record(job_path(ctx.state_dir, key)) or {}).get("compactions")) > 0
        record(ctx, job, getattr(job, "session_id", None), found, compacted=compacted)
    except Exception as exc:  # noqa: BLE001 - the next turn then tells the rules again
        if log is not None:
            log(f"rules sent not recorded job=#{getattr(job, 'job_id', '')}: {type(exc).__name__}")

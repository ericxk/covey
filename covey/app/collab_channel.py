"""咨询通道 (docs/architecture.md 多参与者协作, COVEY_COLLAB): where a collaboration's ask runs. Never in the helper's own
topic: a turn there would write its session (a new last turn, a 回退 point used up), its history and its records, and the
butler's or a lead's turn would save its snapshot or take its receipts. So each (collaboration, helper) has a topic of its
own (core/collab.py channel_id, core/butler.py CHANNEL: no thread, no T number, not listed), with its own history, session
pointer and job records, bound as the helper is (Harness, model, workspace, 敏感词脱敏), and the ask's turn runs in a fork
of the helper's native session there.

- ``open_channel`` (the Bridge, app/collab_flow.py, when an ask is taken): the channel's topic.json, written once, its
  binding the helper's (again, with its pointer cleared, when a clarification must fork the helper anew).
- ``fork_start`` (the task runner, app/runs.py, a channel's turn on a new session): fork the helper's session as it is now
  (Claude: ``fork_at`` the entry before a turn of the helper running meanwhile or a 回退 waiting in it, never removing
  the marker; Codex: ``thread/fork``, only while no turn of the helper runs), else start from the helper's recent history
  (``RECORD``: 话题记录（无原会话）). What it stood on goes into the turn's own record (``collab_evidence``).
- ``ask_text``: what the turn reads: the integrator's question, a clarification's first round when the helper changed
  since, the butler's snapshot or a lead's state pack read without saving or taking anything (``read_only_view``).
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from covey.core import collab
from covey.core.harness import backend_capabilities
from covey.core.history_storage import recent_history
from covey.core.jobs import topic_job_records, upsert_job_record
from covey.core.locks import LOCK
from covey.core.session_state import capture_session_state, compare_and_set_session, read_session_id
from covey.core.state import read_json, read_object, write_json
from covey.core.statuses import RUNNING
from covey.core.text import cut
from covey.core.topics import read_topic, topic_file

FIELD = "collab_channel"  # A channel's topic.json: {collab, helper, integrator, opened_at}.
# The helper's binding the channel takes (app/forks.py takes the same of a fork's source).
BINDING = ("harness_id", "model", "effort", "model_settings_snapshot", "work_mode", "workspace_name", "workdir")
EVIDENCE = "collab_evidence"  # The ask's turn's record: what its answer stands on.
RECORD_ONLY = "collab_record_only"  # A channel's turn's option: it starts from the helper's record, not its session.
SNAPSHOT_CHARS = 6000  # Of the butler's snapshot or a lead's pack given to a consult of it.


def channel_of(ctx) -> dict:
    """The ``collab_channel`` of ctx's topic.json, {} for any topic that is not a channel."""
    if not collab.is_channel(getattr(ctx, "topic_id", "")):
        return {}
    try:
        found = read_topic(ctx).get(FIELD)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def open_channel(state, chat, record, helper, root, *, refresh=False) -> str:
    """The channel of ``helper`` in collaboration ``record`` (its topic.json, written once): bound as the helper is now,
    its replies under ``root`` (the integrator's thread). ``refresh``: bound anew to the helper as it is now (a
    clarification that forks the helper again). Its id."""
    channel = collab.channel_id(record["id"], helper)
    path = topic_file(Path(state), channel)
    if not refresh and read_object(path):
        return channel  # Written once: looked at without LOCK (a read hung on the network disk under it holds up others).
    with LOCK:
        current = read_object(path)
        if current and not refresh:
            return channel
        source = read_json(topic_file(Path(state), helper))
        data = {**current, "topic_id": channel, "chat_id": chat, "root_id": str(root or ""), "thread_id": "",
                **{key: source[key] for key in BINDING if key in source},
                "binding_revision": int(current.get("binding_revision") or 0) + 1, "workspace_locked": True,
                "harness_locked": True,
                FIELD: {"collab": record["id"], "helper": helper, "integrator": (record.get("integrator") or {}).get("key", ""),
                        "opened_at": time.time()}}
        write_json(path, data, ensure_ascii=False, indent=2)
    return channel


def clear_pointer(harness, state, channel) -> None:
    """The channel forgets its session (only the pointer; the fork's transcript stays): its next turn forks again."""
    path = topic_file(Path(state), channel).parent / harness.spec.session_filename
    revision, session = capture_session_state(path, harness.spec.session_pattern)
    if session:
        compare_and_set_session(path, harness.spec.session_pattern, revision, session, None)


def helper_session(harness, state, helper) -> str:
    """The helper's native session as its pointer says now, '' without one."""
    path = topic_file(Path(state), helper).parent / harness.spec.session_filename
    return read_session_id(path, harness.spec.session_pattern) or ""


def _transcript(harness, workdir, session) -> bool:
    finder = getattr(harness.store, "transcript", None)
    if not callable(finder):
        return False
    try:
        return finder(Path(workdir), session) is not None
    except (OSError, ValueError):
        return False


def fork_plan(harness, ctx, helper, chat) -> dict:
    """How the channel's turn on a new session starts: {"fork_session", "fork_at"?, ...evidence} forking the helper's
    session, or {} to start from the helper's recent history. Reads only: a 回退 waiting in the helper's topic is used
    (its ``keep``), never removed."""
    state = ctx.state_dir
    folder = topic_file(Path(state), helper).parent
    caps = backend_capabilities(harness.backend, ctx)
    if not caps.native_fork:
        return {}
    if str(read_object(folder / "topic.json").get("workdir") or "") != str(read_object(
            topic_file(Path(state), ctx.topic_id)).get("workdir") or ""):
        return {}  # A session belongs to a directory: the channel follows the helper's binding.
    session = helper_session(harness, state, helper)
    if not session or not _transcript(harness, ctx.workdir, session):
        return {}
    plan = {"fork_session": session}
    running = [record for record in topic_job_records(Path(state), helper, chat_id=chat) if record.get("status") in RUNNING]
    current = max(running, key=lambda record: int(record.get("job_id") or 0), default=None)
    if current is not None:
        # A turn of the helper runs meanwhile (the owner's message came after the ask was taken): only what came before it.
        if not caps.rewind or not current.get("prompt_uuid") or current.get("prompt_session") != session:
            return {}
        keep = str((harness.backend.rewind_point(ctx, session, current["prompt_uuid"]) or {}).get("keep") or "")
        return {**plan, "fork_at": keep} if keep else {}
    marker = read_object(folder / "rewind-next.json")
    if marker.get("token") and marker.get("session_id") == session and marker.get("keep"):
        plan["fork_at"] = str(marker["keep"])  # The owner dropped a turn the helper has not run past yet.
    return plan


def fork_start(harness, log, ctx, job, history_snapshot):
    """A channel's turn on a new session (the runner, app/runs.py _turn_message): ``ctx`` with the fork of the helper's
    session, else with ``RECORD_ONLY`` and ``history_snapshot`` the helper's recent history; the turn's record says which
    (``EVIDENCE``). Any other topic's turn: as it came."""
    channel = channel_of(ctx)
    helper, chat = str(channel.get("helper") or ""), str(getattr(job, "chat_id", "") or "")
    if not helper:
        return ctx, history_snapshot
    try:
        plan = fork_plan(harness, ctx, helper, chat)
    except Exception as exc:  # noqa: BLE001 - the helper's record, then
        log(f"collab fork not planned job=#{job.job_id}: {type(exc).__name__}")
        plan = {}
    from covey.app.collab_checks import last_ended

    base = last_ended(ctx.state_dir, chat, helper)
    evidence = {"level": collab.FORKED if plan else collab.RECORD, "helper": helper,
                "job_id": int(base.get("job_id") or 0), "prompt_uuid": str(base.get("prompt_uuid") or ""),
                "session": str(plan.get("fork_session") or ""), "fork_at": str(plan.get("fork_at") or ""),
                "at": time.time()}
    try:
        upsert_job_record(ctx.state_dir, job.message_id, **{EVIDENCE: evidence})
    except OSError as exc:
        log(f"collab evidence not recorded job=#{job.job_id}: {type(exc).__name__}")
    if plan:
        options = {key: value for key, value in plan.items() if key in ("fork_session", "fork_at")}
        log(f"collab fork job=#{job.job_id} helper={helper[:12]} at={'entry' if plan.get('fork_at') else 'end'}")
        return dataclasses.replace(ctx, options={**ctx.options, **options}), history_snapshot
    log(f"collab record only job=#{job.job_id} helper={helper[:12]}")
    rows = recent_history(topic_file(Path(ctx.state_dir), helper).parent / "history.jsonl")
    return dataclasses.replace(ctx, options={**ctx.options, RECORD_ONLY: True}), [*rows, *(history_snapshot or [])]


# The text of an ask's turn.

ASK_HEAD = ("[Bridge 协作咨询] {asker} 请你协助（协作 {short}，{which}）。这一轮在你自己会话的一个只读分叉里跑："
            "你原来的会话、记录和手上的事都不受影响，答复只回给 {asker}，不发给主人。")
RECORD_NOTE = "（你的原会话这次接不上，上面“最近的对话”是这个话题最近的记录：答复以它为准，并说明证据只有话题记录。）"
AGAIN_NOTE = ("你上一次（第 1 次）被问到：\n{asked}\n你上一次的回答：\n{answered}\n"
              "在你上次回答之后，你原来的话题又跑了 {turns} 轮：以现在的状态为准，变了的地方说清楚。")


def read_only_view(ctx, chat, helper, role, *, log=lambda _: None) -> str:
    """The butler's snapshot or a lead's state pack as a consult of it reads them: read only (the butler's record not
    saved, the lead's receipts not taken); '' for a topic, or when it cannot be read. ``ctx``: a context of the Bridge
    (its state directory)."""
    state = ctx.state_dir
    try:
        if role == collab.BUTLER:
            from covey.app.butler import take_snapshot

            text = take_snapshot(ctx, chat, "", full=True, save=False)
            return "管家此刻的快照（只读）：\n" + cut(text, SNAPSHOT_CHARS) if text else ""
        if role == collab.LEAD:
            from covey.app import project_lead
            from covey.core import projects

            found = projects.topic_project(state, helper)
            record = projects.read_record(state, found.id) if found is not None else {}
            if not record:
                return ""
            text = project_lead.read_only_pack(state, record)
            return cut(text, SNAPSHOT_CHARS)
    except Exception as exc:  # noqa: BLE001 - the ask goes without it
        log(f"collab read-only view unavailable role={role}: {type(exc).__name__}")
    return ""


def ask_text(record, ask, asker, *, again=None, view="") -> str:
    """The message of ``ask``'s turn: the head, the question, ``again`` ({asked, answered, turns}: a clarification on a
    new fork) and ``view`` (``read_only_view``)."""
    which = "澄清" if int(ask.get("number") or 1) > 1 else "咨询"
    parts = [ASK_HEAD.format(asker=asker, short=str(record.get("id") or "")[:8], which=which),
             f"{asker} 的问题：\n{ask.get('text') or ''}"]
    if again:
        parts.append(AGAIN_NOTE.format(asked=cut(again.get("asked"), 800), answered=cut(again.get("answered"), 3000),
                                       turns=int(again.get("turns") or 0)))
    if view:
        parts.append(view)
    return "\n\n".join(parts)


# What a channel's turn is told in place of the checkpoint contract (app/runs.py checkpoint_rules).
RULES = ("只读咨询的约定：这一轮没有检查点，也不能提问；只能用读文件和搜索的工具。"
         "回复就是答复：先给结论，再给依据（文件路径、哪一轮做的结论、产物的版本），不知道或缺材料就直说缺什么。")

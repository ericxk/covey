"""Runs: one job's turn on its Harness's backend.

The prompt (the bridge rules, at system level when the backend has a channel for them,
otherwise in the message; recent history on a fresh session, the message alone on a
continued one; a fork's or a switch's first-run context; the checkpoint file and the
previous status), the native sessions the backend reports (claimed until the result is
settled), and a checkpoint question the turn leaves behind.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from covey.app import (collab_channel, device_files, home_turn, knowledge_turn, memory_turn, owner_send,
                       project_context, quota_failure, repo_peers, repos_turn, subagent_recovery, tool_limits,
                       workspace_gists)
from covey.core.statuses import ENDED
from covey.app.leftovers import stop_task_leftovers
from covey.app.butler import butler_turn
from covey.app.prompts import (MEMORY_FIX_RULE, REMEMBER_RULE, bridge_rules, earlier_history, first_turn_message,
                                        home_section, principles_block, context_high, resume_message, resume_reminders,
                                        turn_tail)
from covey.app.sessions import mark_system_rules, started_with_system_rules
from covey.app.recovery_cards import paint_answered
from covey.app.switch_handoff import capped
from covey.cards.trace_view import BRIDGE_NOTE
from covey.core.attachments import encoded_size, fit_images, native_image_attachments
from covey.core import criteria, devices, gateways, quota_hold, turn_source, turn_words
from covey.core.checkpoint import (audit_recovery, checkpoint_note, checkpoint_rules, gist_note, images_note,
                                            needs_owner, normalize_questions, question_summary, read_checkpoint,
                                            release_user_wait)
from covey.core.harness import (CONSULT, TOOL_LIMIT, BridgeContext, JobCancelled, RunCallbacks, RunInputs, RunRequest, RunResult,
                                         backend_capabilities, normalize_run_result)
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import upsert_job_record
from covey.core import memory
from covey.core import principles as owner_principles
from covey.core.butler import is_butler_topic, traits
from covey.core.metrics import mark
from covey.core.proxy import brief_history, owner_turn_note
from covey.core.redaction import topic_redaction
from covey.core.result import ERROR_CODES, ExecutionOutcome
from covey.core.saved_results import consume_paused_question_answer, observe_verified_reply
from covey.core.session_guard import claim_native_session
from covey.core.history_storage import recent_history
from covey.core.topics import proxy_topic, read_topic


def _file_size(item) -> int:
    try:
        return Path(item["path"]).stat().st_size
    except OSError:  # Read by the Harness, which reports it.
        return 0


def _megabytes(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def _why(item, limit: int) -> str:
    return (f"超过单张 {_megabytes(limit)} 的上限" if item["reason"] == "image"
            else "本轮图片合计超过一次请求能带的大小")


def skipped_images_note(skipped, limit: int) -> str:
    """The prompt's note for the images left out: their paths are in the message above it."""
    lines = "\n".join(f"- `{item.get('shown') or item['path']}`（{_megabytes(item['size'])}，{_why(item, limit)}）" for item in skipped)
    return ("\n\n[这些图片没有作为图片附上，只在上文给了路径；需要看时自己用工具处理这个文件（例如先缩小再读取）：\n"
            + lines + "]")


def skipped_images_card_note(skipped, limit: int, at: str, shown: int = 5) -> str:
    """The task card's line for the images left out."""
    names = "；".join(f"{Path(item['path']).name}（{_megabytes(item['size'])}，{_why(item, limit)}）"
                     for item in skipped[:shown])
    more = f"；等共 {len(skipped)} 张" if len(skipped) > shown else ""
    return (f"机器人 {at}：{len(skipped)} 张图片没有直接交给模型：{names}{more}。模型只拿到文件路径，需要时自己读取；"
            "要让模型直接看图，请压缩后重发。")


def log_backend_failure(log, job_id, data) -> None:
    """The ``backend_failure`` event (core/protocol.py) as log lines, in the task runner's log: one line of what failed and
    how the backend ended, then each of its last stderr lines. Only short plain values are written, whatever the event
    holds; never raises."""
    try:
        if not isinstance(data, dict):
            return
        facts = " ".join(f"{name}={str(data[name])[:80].replace(' ', '_')}" for name in (
            "status", "code", "stage", "exception", "reason", "errno", "process_exit_code", "exit_code", "write_size",
            "write_offset") if type(data.get(name)) in (str, int))
        log(f"backend failure job=#{job_id} {facts}")
        lines = [str(line)[:400] for line in data.get("stderr") or () if isinstance(line, str)]
        for line in lines:
            log(f"backend stderr job=#{job_id} | {line}")
        if not lines:
            log(f"backend stderr job=#{job_id} | (nothing)")
    except Exception:  # noqa: BLE001 - a diagnosis never decides the turn
        return


def proxy_turn_note(ctx, job) -> str:
    """What the owner's own turn in a 代答 topic is told while a batch there waits: that turn's reply is
    sent to the contact (app/proxy.py), and the rules are only in the batches' messages."""
    try:
        contact = (read_topic(ctx).get("proxy") or {}).get("contact")
    except (OSError, ValueError, AttributeError):
        return ""
    return owner_turn_note(ctx.state_dir, contact, job.message_id) if contact else ""


@dataclasses.dataclass
class _Live:
    """What a run's callbacks (Runs._callbacks) read when they are called, not when they are made: the context the
    turn runs in, which a fork, the butler, a ↩️ 回退 and a compaction replace, and whether its session has the
    bridge rules at system level."""
    ctx: BridgeContext
    rules_session: bool = False


class Runs:
    """- ``harness``: whose backend runs the turn, and its session pattern.
    - ``runtime``: the log.
    - ``interaction``: receives the backend's structured events.
    - ``forks`` / ``switch``: the first run of a forked or switched topic.
    - ``cards``: the card of the answered task a checkpoint continuation continues.
    - ``questions``: a checkpoint question the turn leaves behind.
    - ``compaction``: a compaction request (the butler's) the turn takes.
    - ``rewinds``: a ↩️ 回退 the turn resumes for, and its prompt.
    - ``record_job_state``: the Harness's job-state recorder (late-bound: it sits above).
    """

    def __init__(self, harness, *, runtime, interaction, forks, switch, cards, questions, compaction, rewinds,
                 record_job_state) -> None:
        self.harness = harness
        self.runtime = runtime
        self.interaction = interaction
        self.forks = forks
        self.switch = switch
        self.cards = cards
        self.questions = questions
        self.compaction = compaction
        self.rewinds = rewinds
        self.record_job_state = record_job_state

    def bridge_rules(self, ctx: BridgeContext, job: ReplyJob | None = None, *, fresh: bool = False) -> str:
        """The rules of ``job``'s turn; ``fresh``: a new session's, which alone carry the workspace's recent topics
        (app/workspace_gists.py, COVEY_WORKSPACE_GISTS). Static within a topic, one cacheable prefix (docs/butler.md 记忆 · 每轮怎么带): what changes
        turn by turn is ``_turn_tail``'s."""
        return bridge_rules(
            backend_name=self.harness.spec.prompt_agent_name,
            workspace_name=ctx.workspace_name,
            workdir=ctx.workdir, device=ctx.device, remote_workdir=ctx.remote_workdir,
            runtime_note=self.harness.backend.prompt_runtime_note(ctx),
            notes=self.backend_notes(ctx),
            principles=self.principles(ctx, job),
            project=project_context.block(ctx, job, resumed=False),
            knowledge_text=knowledge_turn.block(ctx, job, owner_turn=lambda: self.owner_turn(ctx, job), resumed=False),
            remember=knowledge_turn.remembers(ctx, job, owner_turn=lambda: self.owner_turn(ctx, job)),
            memory_text=memory_turn.block(ctx, job, owner_turn=lambda: self.owner_turn(ctx, job)),
            memory_fix=memory_turn.fix_rule(ctx, job, lambda: self.owner_turn(ctx, job)),
            proxy=proxy_topic(ctx),
            gists=workspace_gists.block(ctx, job, owner_turn=lambda: self.owner_turn(ctx, job)) if fresh else "",
            checkpoint=self.checkpoint_rules(ctx, job),
            home=home_turn.static_block(ctx),
        )

    def backend_notes(self, ctx: BridgeContext) -> tuple[str, ...]:
        """The backend's own constraints of ``ctx``'s turn (core/harness.py ``prompt_notes``), in the rules and in each
        resumed turn's reminder: one that changed after the session started (redaction turned on) reaches the next turn."""
        return tuple(getattr(self.harness.backend, "prompt_notes", lambda ctx: ())(ctx) or ())

    @staticmethod
    def slim(ctx: BridgeContext, job: ReplyJob | None) -> bool:
        """Whether ``job``'s turn is the butler's or a lead's (Traits.own_turn): it answers and dispatches, so its
        checkpoint contract has no steps, delivery or images (``checkpoint_rules``). No job: a topic's."""
        return job is not None and traits(job.topic_id or ctx.topic_id, job.chat_id).own_turn

    def checkpoint_rules(self, ctx: BridgeContext, job: ReplyJob | None, *, resumed: bool = False) -> str:
        """The checkpoint contract of ``job``'s turn (core/checkpoint.py checkpoint_rules, 5.9 row 7): the part every turn
        uses for a ``slim`` one; all of it at the head of a topic's new session, which may run long, change code or
        show an image. A topic's ``resumed`` turn, whose session read that, is reminded of the part every turn uses and
        of what the topic uses (``_used``)."""
        if ctx.options.get(TOOL_LIMIT) == CONSULT:
            return collab_channel.RULES  # A 协作 ask: no checkpoint at all (app/tool_limits.py CONSULT).
        if self.slim(ctx, job):
            return checkpoint_rules(steps=False, delivery=False, images=False)
        if not resumed or job is None:
            return checkpoint_rules()
        steps, delivery, images = self._used(ctx, job)
        return checkpoint_rules(steps=steps, delivery=delivery, images=images)

    def _used(self, ctx: BridgeContext, job: ReplyJob) -> tuple[bool, bool, bool]:
        """(steps, delivery, images): whether a turn of the topic before ``job`` listed steps (a long task), whether one
        changed code (it wrote a delivery, or the Bridge saw it change files: its diff), and whether the owner's words
        of this turn are about an image to show at once (core/checkpoint.py IMAGE_WORDS)."""
        images = bool(images_note(job.user_text))
        try:
            records = [record for record in self.harness.topic_records(ctx, job.chat_id)
                       if record.get("message_id") != job.message_id]
        except (OSError, ValueError, AttributeError):  # Unreadable: remind of all of it, as before the split.
            return True, True, True
        return (any(record.get("steps") for record in records),
                any(record.get("delivery") or record.get("diff_json_path") for record in records), images)

    def principles(self, ctx: BridgeContext, job: ReplyJob | None) -> str:
        """The owner's principles block for ``job``'s turn (core/principles.py): ctx's workspace's always (the
        project's conventions, whoever asks), 通用 only on the owner's own turn (``owner_turn``: a colleague's topic
        in a group does not read the owner's personal preferences); '' when there are none, and for a 代答 topic,
        whose replies go to other people as the owner."""
        if proxy_topic(ctx) or memory.enabled():  # With COVEY_MEMORY=1 they are in the one memory block (app/memory_turn.py).
            return ""
        owner, workspace = owner_principles.for_turn(ctx.state_dir, ctx.workspace_name)
        return principles_block(owner if self.owner_turn(ctx, job) else [], workspace, ctx.workspace_name)

    def owner_turn(self, ctx: BridgeContext, job: ReplyJob | None) -> bool:
        """Whether ``job`` is the owner's own turn: the butler's always, else as app/access.py says from its
        sender (rule 11); a task runner has the Bridge's owner and state too (app/runner.py). No job: not."""
        if job is None:
            return False
        if is_butler_topic(job.topic_id or ctx.topic_id, job.chat_id):
            return True
        return self.runtime.access.owner_turn(job.chat_id, getattr(job, "sender_id", ""))

    def generate_reply_for_worker(
        self,
        ctx: BridgeContext,
        job: ReplyJob,
        session_id: str | None,
        progress_callback: Callable[[str, bool], None] | None,
    ) -> RunResult:
        job.bridge_run_id = job.bridge_run_id or str(uuid.uuid4())
        job.native_session_claims = set()
        job.result_backend = self.harness.spec.name
        job.last_backend_control = None
        live = _Live(ctx)
        if isinstance(job.recovery_context, dict) and "native_question_continuation" in job.recovery_context:
            session_id = consume_paused_question_answer(ctx, job, self.harness.spec.name, session_id)
        callbacks = self._callbacks(live, job, session_id)
        if session_id:
            callbacks.session(session_id)
        prompt, rules, system, attachments = self._prompt(live, job, session_id)
        cancel = getattr(job, "cancel_event", None)
        if cancel is not None and cancel.is_set():
            # Stopped (or a SIGTERM) while the prompt was made, as during a 换配置 handoff summary (up to 3 minutes):
            # no new Harness starts; the worker ends the turn as cancelled or interrupted.
            raise JobCancelled("任务在开始执行前停止")
        refused = self._refusal(live.ctx, session_id, job)
        if refused is not None:
            return refused
        live.ctx = self.rewinds.turn_context(live.ctx, job, session_id)
        live.ctx = self.compaction.turn_context(live.ctx, job, session_id)
        live.ctx = devices.spawn_extras(live.ctx, job.checkpoint_path)  # A device's checkpoint mirror and seed file.
        return self._run(live.ctx, job, session_id, progress_callback, callbacks, prompt, rules if system else "",
                         attachments)

    def _callbacks(self, live: _Live, job: ReplyJob, session_id: str | None) -> RunCallbacks:
        """The run's callbacks: the backend's control, native sessions (claimed; marked as having the bridge rules
        at system level when ``live`` says so), process and verified reply, and its structured events (the status
        line's model requests and a ↩️ 回退's prompt here, the rest to Interaction). They read ``live`` when they are
        called: a fork, the butler, a 回退 and a compaction replace the context after they are made."""

        def observe_control(control):
            job.backend_control = control
            if control is not None:
                job.last_backend_control = control

        def observe_session(sid):
            if self.harness.spec.session_pattern.fullmatch(sid):
                claim_native_session(live.ctx, self.harness.spec.name, sid, job.bridge_run_id)
                job.native_session_claims.add(sid)
                job.session_id = sid
                # Observation is not permission to activate a topic session.
                if live.rules_session and sid != session_id:
                    try:
                        mark_system_rules(live.ctx, self.harness.spec.name, sid)
                    except (OSError, ValueError) as exc:  # Unmarked only costs the fuller message.
                        self.runtime.log(f"session {sid} not marked: {type(exc).__name__}")

        def observe_process(process):
            job.backend_process = process
            if process is not None:
                mark(job, 'backend_started')
                if job.execution_started:
                    upsert_job_record(live.ctx.state_dir, job.message_id, timings=job.timings)

        def observe_event(kind, data, control):
            if kind == "model_request":  # Only the status line hears of it; the trace has nothing to show.
                if job.activity_monitor is not None:
                    job.activity_monitor.request()
                return None
            if kind == "backend_failure":  # Only the log hears of it.
                log_backend_failure(self.runtime.log, job.job_id, data)
                return None
            if kind == "prompt_read":  # The turn a ↩️ 回退 would drop starts here (app/rewind.py).
                try:
                    self.rewinds.prompt_read(live.ctx, job, data)
                except (OSError, ValueError) as exc:
                    self.runtime.log(f"prompt of job=#{job.job_id} not recorded: {type(exc).__name__}")
                return None
            return self.interaction.structured_harness_event(live.ctx, job, kind, data, control)

        return RunCallbacks(
            event=observe_event,
            session=observe_session,
            control=observe_control,
            process=observe_process,
            verified_reply=lambda reply: observe_verified_reply(live.ctx, job, self.harness.spec.name, reply),
        )

    def _prompt(self, live: _Live, job: ReplyJob, session_id: str | None):
        """(prompt, rules, whether the backend takes them at system level, image attachments) of the turn; sets
        ``live``'s context (a fork's first run and the butler's turn replace it) and whether its session has the
        rules at system level."""
        fresh = not session_id
        # What the turn may do (app/tool_limits.py), in its context before anything of it runs.
        live.ctx = tool_limits.limited(self.runtime.access, live.ctx, job, self.runtime.log,
                                       held=backend_capabilities(self.harness.backend, live.ctx).look_gate)
        # A fork replaces ctx; the callbacks read it when they are called.
        live.ctx, prompt = self._turn_message(live.ctx, job, session_id)
        # The butler's turn (app/butler.py): its rules and snapshot open the message, its Bridge tools
        # join the run; the whole snapshot on a new session and on one compacted before this turn.
        live.ctx, prompt = butler_turn(live.ctx, job, prompt, session_id, self.compaction, self.runtime.log, self.harness)
        ctx = live.ctx
        fresh = fresh and not ctx.options.get("track_fork")  # A track's fork has its source's conversation and rules.
        rules = self.bridge_rules(ctx, job, fresh=fresh)
        caps = backend_capabilities(self.harness.backend, ctx)
        system = caps.system_instructions
        # A native session keeps the system rules of its first invocation: a new one starts with
        # them, a fork has its source's, a resumed one has them only if it started with them.
        source = session_id or ctx.options.get("fork_session")
        live.rules_session = system and (started_with_system_rules(ctx, self.harness.spec.name, source) if source else True)
        prompt = self._with_rules(ctx, job, prompt, rules, system=system, fresh=fresh, rules_session=live.rules_session)
        if ctx.options.get(TOOL_LIMIT) == CONSULT:
            # A 协作 ask: no checkpoint file, no context or repository note; its limit's words last.
            prompt += ("\n\n" + collab_channel.RECORD_NOTE if ctx.options.get(collab_channel.RECORD_ONLY) else "") + tool_limits.note(ctx)
        else:
            prompt += (self._checkpoint_note(ctx, job) + self._turn_tail(ctx, job) + self._context_note(ctx, job, session_id)
                       + self._repo_note(ctx, job) + tool_limits.note(ctx))
        attachments = self._image_attachments(ctx, prompt)
        if ctx.device:
            prompt, attachments, live.ctx = self._stage_attachments(ctx, job, prompt, attachments)
        prompt, attachments = self._fit_images(live.ctx, job, caps, prompt, attachments)
        return prompt, rules, system, attachments

    def _stage_attachments(self, ctx: BridgeContext, job: ReplyJob, prompt: str, attachments):
        """(prompt, attachments, context) of a device turn with the files the prompt names put on the device (app/device_files.py):
        the prompt names the copies, an image gains its ``device_path``, and the directory they are in is the context's
        ``device_incoming`` (Codex's workspace reader may read it). Files left out are one line on the task card and in the log."""
        staged = device_files.stage(ctx, job, prompt, attachments)
        if staged.put or staged.skipped:
            self.runtime.log(f"device attachments job=#{job.job_id} device={ctx.device} put={len(staged.put)} "
                             f"skipped={len(staged.skipped)} bytes={sum(size for _, size, _ in staged.put)}")
        line = staged.card_line(ctx.device)
        if line:
            try:
                self._bridge_note(ctx, job, "bridge-device-files", line)
            except Exception as exc:  # noqa: BLE001 - the turn goes on; only the card line failed
                self.runtime.log(f"device attachments note not shown: {type(exc).__name__}")
        if staged.directory:
            ctx = dataclasses.replace(ctx, options={**ctx.options, "device_incoming": staged.directory})
        return staged.prompt, staged.attachments, ctx

    def _device_refusal(self, ctx: BridgeContext, job: ReplyJob | None) -> str:
        """Why a turn on ``ctx.device`` may not start, or '': the device is not in devices.json, not for this chat's space
        (``Access.allows_device``), not for this Harness, or does not answer that its directory is there (one shim
        ``stat``, 5 seconds)."""
        try:
            device = devices.current().device(ctx.device)
        except devices.UnknownDevice:
            return f"本话题的工作区在设备 {ctx.device} 上，但 devices.json 里没有这台设备（或没有打开 COVEY_DEVICES），本轮没有启动。"
        chat_id = getattr(job, "chat_id", "") or read_topic(ctx).get("chat_id", "")
        if not self.runtime.access.allows_device(chat_id, device):
            return f"设备 {ctx.device} 不允许在这个聊天里使用（devices.json 的 spaces），本轮没有启动。"
        if self.harness.spec.name not in device.harnesses:
            return f"设备 {ctx.device} 没有声明 {self.harness.spec.title}（devices.json 的 harnesses），本轮没有启动。"
        state, why = devices.check_dir(ctx.state_dir, ctx.device, ctx.remote_workdir)
        if state == "offline":  # Not queued, not run here either: the card's retry button asks again.
            return turn_words.device_unreachable(ctx.device, why.partition("（")[2].rstrip("）") or "没有答复")
        return "" if state == "ok" else why + "，本轮没有启动。"

    def _refusal(self, ctx: BridgeContext, session_id: str | None, job: ReplyJob | None = None) -> RunResult | None:
        """The failed result of a turn that may not start (its workdir is missing, or redaction is on where the
        Harness cannot do it), or None when it may. A device's directory is the device's to answer
        (``_device_refusal``); the hub's stand-in never exists."""
        if ctx.device:
            detail = self._device_refusal(ctx, job)
            if detail:
                outcome = ExecutionOutcome("failed", "workdir_missing", cleanup_confirmed=True, protection_confirmed=True)
                return RunResult(outcome.summary(detail), session_id, True, None, outcome, failure_detail=detail)
        elif not Path(ctx.workdir).is_dir():
            # Every backend would fail to start here, each with a generic transport error.
            outcome = ExecutionOutcome("failed", "workdir_missing", cleanup_confirmed=True, protection_confirmed=True)
            return RunResult(outcome.summary(), session_id, True, None, outcome)
        refused = self._redaction_refusal(ctx)
        if refused:
            # Never sent unredacted: a Harness without the relay does not start (core/redaction.py).
            outcome = ExecutionOutcome("failed", "redaction_failed", cleanup_confirmed=True, protection_confirmed=True)
            return RunResult(outcome.summary(refused), session_id, True, None, outcome, failure_detail=refused)
        refused = self._limit_refusal(ctx)
        if refused:
            # A member's turn of a read-only group never runs where it could do more (app/tool_limits.py READ).
            outcome = ExecutionOutcome("failed", "read_only_refused", cleanup_confirmed=True, protection_confirmed=True)
            return RunResult(outcome.summary(refused), session_id, True, None, outcome, failure_detail=refused)
        found = quota_hold.blocks(ctx.state_dir, self.harness.spec.name, ctx.model)
        if found:
            # A gateway's quota spent until its reset: the turn would only fail the same way, minutes later.
            refused = quota_hold.failure_words(*found)
            outcome = ExecutionOutcome("failed", "quota_exhausted", cleanup_confirmed=True, protection_confirmed=True)
            return RunResult(refused, session_id, True, None, outcome, failure_detail=refused)
        found = self._backend_refusal(ctx)
        if found:
            # The Harness knows the turn would only fail (its model gone from the account, its quota held, T872).
            code, refused, reset = found
            outcome = ExecutionOutcome("failed", code, cleanup_confirmed=True, protection_confirmed=True)
            return RunResult(outcome.summary(refused), session_id, True, None, outcome, failure_detail=refused,
                             limit={"reset_at": reset} if reset else None)
        return None

    def _backend_refusal(self, ctx: BridgeContext):
        """The backend's own refusal of the turn (BackendRuntime.start_refusal), or None; one that fails is none."""
        check = getattr(self.harness.backend, "start_refusal", None)
        try:
            found = check(ctx) if callable(check) else None
        except Exception as exc:  # noqa: BLE001 - a check never stops a turn
            self.runtime.log(f"start check of {self.harness.spec.name} failed: {type(exc).__name__}")
            return None
        if (not isinstance(found, tuple) or len(found) != 3 or found[0] not in ERROR_CODES or not found[0]
                or not isinstance(found[1], str)):
            return None
        return found

    def _limit_refusal(self, ctx: BridgeContext) -> str:
        """Why the turn may not start for its limit (app/tool_limits.py refusal), or '' when it may."""
        return tool_limits.refusal(ctx, backend_capabilities(self.harness.backend, ctx), self.harness.spec.title)

    def _run(self, ctx: BridgeContext, job: ReplyJob, session_id: str | None, progress_callback, callbacks, prompt: str,
             system_rules: str, attachments) -> RunResult:
        """The turn on the backend, then what its commands left running stopped and its checkpoint question left."""
        try:
            result = normalize_run_result(self.harness.backend.run(RunRequest(ctx, prompt, session_id, progress_callback,
                callbacks, RunInputs(job.cancel_event, job.checkpoint_path, tuple(attachments), job.bridge_run_id),
                system_rules)))
        finally:
            # The backend has closed its Harness: what the turn's commands left running (app/leftovers.py).
            stop_task_leftovers(self.runtime, self.interaction, ctx, job)
        self._leave_checkpoint_question(ctx, job, result)
        return quota_failure.refine(self.harness.spec.name, ctx, self._quota_words(ctx, result))

    def _quota_words(self, ctx: BridgeContext, result: RunResult) -> RunResult:
        """A turn that failed on a gateway's spent quota, in core/quota_hold.py's words: when it is back (the hold
        the failure left, if it said), that every model on it shares it, and where to go instead."""
        outcome = result.outcome
        if outcome is None or outcome.error_code != "quota_exhausted":
            return result
        gateway = gateways.quota_gateway(self.harness.spec.name, ctx.model)
        if gateway is None or not gateway.quota_pattern:
            return result  # A quota of the Harness's own account: its words stand.
        detail = quota_hold.failure_words(gateway, quota_hold.read(ctx.state_dir, gateway))
        return dataclasses.replace(result, text=detail if result.is_error else result.text, failure_detail=detail)

    def _redaction_refusal(self, ctx: BridgeContext) -> str:
        """Why the turn may not start for redaction, or '' when it may."""
        try:
            if not topic_redaction(ctx.state_dir, ctx.topic_id):
                return ""
        except ValueError as exc:
            return str(exc).rstrip("。") + "。"
        if backend_capabilities(self.harness.backend, ctx).redaction:
            return ""
        if ctx.device:  # The term relay runs on the hub; a Harness on a device would talk to its service itself.
            return (f"本话题开着敏感词脱敏，设备 {ctx.device} 上的任务不支持脱敏，本轮没有发给模型服务。"
                    "可在换配置里关掉脱敏，或把工作区换成本机目录。")
        return (f"本话题开着敏感词脱敏，{self.harness.spec.title} 不支持脱敏，本轮没有发给模型服务。"
                "可点任务卡上的“🔁 换配置”换到 Claude 的模型。")

    def _turn_message(self, ctx: BridgeContext, job: ReplyJob, session_id: str | None) -> tuple[BridgeContext, str]:
        """The message before the rules, and the context to run in (a fork's first run has its own)."""
        user_text = ((job.prompt_text or job.user_text) + proxy_turn_note(ctx, job)
                     + owner_send.turn_note(ctx, job, getattr(self.runtime, "access", None)))
        if session_id:
            return ctx, resume_message(user_text)
        history_snapshot = (
            job.history_snapshot if job.history_snapshot is not None else recent_history(ctx.history_path)
        )
        ctx, history_snapshot = self.forks.fork_start(ctx, history_snapshot)
        # A 协作 ask's channel (app/collab_channel.py): a fork of its helper's session, else the helper's record.
        ctx, history_snapshot = collab_channel.fork_start(self.harness, self.runtime.log, ctx, job, history_snapshot)
        if getattr(job, "policy_retry", None):
            # A retry after a safety refusal (app/policy_retry.py) starts from its own snapshot, the refused round
            # taken out: a switch handoff would bring that round back, and the switch notice, again.
            return ctx, first_turn_message(user_text, earlier_history(history_snapshot, job.message_id, user_text))
        # The old model's summary is not asked for a turn generate_reply refuses below (a missing workdir, 敏感词脱敏, a
        # member's read-only turn on a Harness that cannot hold it).
        history_snapshot, handoff = self.switch.switch_handoff(
            ctx, history_snapshot, getattr(job, "cancel_event", None),
            ready=lambda: ((bool(ctx.device) or Path(ctx.workdir).is_dir()) and not self._redaction_refusal(ctx)
                           and not self._limit_refusal(ctx)),
            note=lambda text: self._bridge_note(ctx, job, "bridge-handoff", text),
            show=lambda element: self._handoff_block(ctx, job, element))
        earlier = earlier_history(history_snapshot, job.message_id, user_text)
        if proxy_topic(ctx):
            earlier = brief_history(earlier)  # Each earlier batch by its messages; its rules are this batch's.
        # After a switch each earlier message is cut short: the transcript file has them whole.
        return ctx, first_turn_message(user_text, capped(earlier) if handoff else earlier) + handoff

    def _handoff_block(self, ctx: BridgeContext, job: ReplyJob, element: dict) -> None:
        """The 交接 block (app/switch_handoff.py) on the job's task card, right above its answer; replaced in place."""
        progress = getattr(job, "progress", None)
        if progress is None or getattr(progress, "slim", False):
            return  # The butler's slim card never switches; no card, no block.
        self.cards.set_task_element(ctx, job, element, before=progress.element_id)

    def _bridge_note(self, ctx: BridgeContext, job: ReplyJob, key: str, text: str) -> None:
        """A line of the Bridge's own in the task card's 执行过程 (one ``key`` per turn, a later text replaces it)."""
        if job.tool_trace is None:
            return
        at = _dt.datetime.now().astimezone().strftime("%H:%M")
        self.interaction.structured_harness_event(ctx, job, "progress_note", {
            "id": f"{key}-{job.job_id}", "title": BRIDGE_NOTE, "text": f"机器人 {at}：{text}"}, None, from_backend=False)

    def _with_rules(self, ctx: BridgeContext, job: ReplyJob, prompt: str, rules: str, *,
                    system: bool, fresh: bool, rules_session: bool) -> str:
        if fresh or not rules_session or not (proxy_topic(ctx) or ctx.options.get(TOOL_LIMIT) == CONSULT):
            # COVEY_MEMORY=1: the numbers carried, every turn of a topic (a resumed session with the rules at system level
            # carries them too: its tail does), not a 代答 topic's or a 协作 ask's resumed one.
            memory_turn.record(ctx, job, lambda: self.owner_turn(ctx, job), self.runtime.log)
        if not system and fresh:
            prompt = rules + "\n\n" + prompt
        elif not rules_session and not proxy_topic(ctx):
            # A 代答 topic's batch prompts carry its rules each turn (core/proxy.py batch_prompt): nothing appended.
            # Sessions without the rules at system level (older ones, or no such channel); their original prompts may still describe the tool repository as the
            # output root, or compaction may have dropped them.
            # The principles too: one the owner gave after the session started reaches its next turn.
            principles = self.principles(ctx, job)
            # The project's memory too (COVEY_PROJECT_MEMORY=1): one noted after the session started reaches its next turn.
            project = project_context.block(ctx, job, resumed=True)
            # The knowledge entries (core/knowledge.py) too, only those since the topic's previous turn.
            owner = lambda: self.owner_turn(ctx, job)  # noqa: E731
            knowledge_text = knowledge_turn.block(ctx, job, owner_turn=owner, resumed=True)
            remember = knowledge_turn.remembers(ctx, job, owner_turn=owner)
            memory_text = memory_turn.block(ctx, job, owner_turn=owner)
            memory_fix = memory_turn.fix_rule(ctx, job, owner)
            # The backend's constraints of this turn (the redaction note once the topic turned it on), and the 主目录段 as
            # the rules have it: a topic from the home keeps its repo map and the rules of working from it (T900 W0-4).
            where = home_section(ctx.workspace_name, ctx.workdir, ctx.device, ctx.remote_workdir,
                                 home=home_turn.static_block(ctx))
            prompt += "\n\n" + resume_reminders(self.backend_notes(ctx)) + (
                "\n\n" + where + ("\n\n" + memory_text if memory_text else "")
                + ("\n\n" + principles if principles else "")
                + ("\n\n" + project if project else "")
                + ("\n\n" + knowledge_text if knowledge_text else "")
                + "\n\n" + self.checkpoint_rules(ctx, job, resumed=True) + ("\n\n" + REMEMBER_RULE if remember else "")
                + ("\n\n" + MEMORY_FIX_RULE if memory_fix else ""))
        return prompt

    def _checkpoint_note(self, ctx: BridgeContext, job: ReplyJob) -> str:
        """The checkpoint file and the previous status; continuing an answered task closes its card. None in a 代答
        topic: its turns never wait for the owner and write no checkpoint (app/prompts.py proxy_constraints)."""
        if not job.checkpoint_path or proxy_topic(ctx):
            return ""
        prior = job.recovery_context
        if prior is None:
            prior = self._continue_previous(ctx, job)
        dead = None
        if prior is None:
            dead = self._dead_subagents(ctx, job)
            job.recovery_context = {"subagents": dead} if dead else None
        if prior is not None and set(prior) == {"subagents"}:
            pass  # Only what the subagents delivered (a second call for the same turn): nothing to audit.
        elif prior is not None:
            job.recovery_context = prior
            audit = (audit_recovery(prior, ctx.remote_workdir,
                                    stat_paths=devices.stat_paths(ctx.state_dir, ctx.device, ctx.remote_workdir))
                     if ctx.device else audit_recovery(prior,ctx.workdir))
            prior = {**prior, "bridge_preflight": audit}
            self.record_job_state(ctx,job,"running","已检查本地恢复信息；执行前须核查外部任务",recovery_audit=prior["bridge_preflight"])
        elif dead:
            prior = {"subagents": dead}  # No checkpoint to verify: only the block of what they delivered.
        shown = devices.device_checkpoint_path(ctx, job.checkpoint_path) if ctx.device else ""
        prior = owner_send.untaken(ctx, job, prior)  # The 代发 drafts the Bridge took in are in the turn's 代发 note (T825).
        return ("\n\n" + checkpoint_note(Path(job.checkpoint_path), prior, shown=shown) + self._gist_note(ctx, job)
                + self._revisit_note(ctx, job) + self._ask_note(ctx, job) + self._collab_note(ctx, job)
                + self._criteria_note(ctx, job) + self._images_note(ctx, job))

    def _turn_tail(self, ctx: BridgeContext, job: ReplyJob) -> str:
        """The turn's dynamic block (app/prompts.py turn_tail): the memory's (app/memory_turn.py tail) and the repositories'
        (app/repos_turn.py tail); none in a 代答 topic. '' while both are empty, as before the overhaul."""
        if proxy_topic(ctx):
            return ""
        return turn_tail(memory_turn.tail(ctx, job, lambda: self.owner_turn(ctx, job)), repos_turn.tail(ctx, job))

    def _dead_subagents(self, ctx: BridgeContext, job: ReplyJob) -> dict | None:
        """What the subagents of the topic's last ended task delivered (app/subagent_recovery.py), when that task failed
        or was interrupted and ``job`` is the owner's next message, not a retry (which carries it already); None else."""
        if job.recovery_context is not None or job.kind != "chat":
            return None
        try:
            ended = next((r for r in self.harness.topic_records(ctx, job.chat_id)
                          if r.get("job_id") != job.job_id and r.get("status") in ENDED), None)
        except (OSError, ValueError, AttributeError):
            return None
        return subagent_recovery.collect(self.harness, ctx, ended) if ended else None

    @staticmethod
    def _repo_note(ctx: BridgeContext, job: ReplyJob) -> str:
        """One line when other topics work in this topic's git repository (app/repo_peers.py); '' without."""
        line = repo_peers.note(ctx, job)
        return "\n\n" + line if line else ""

    def _context_note(self, ctx: BridgeContext, job: ReplyJob, session_id: str | None) -> str:
        """The line (app/prompts.py context_high) for an ordinary topic's resumed turn whose previous turn ended with the context
        high in its window: its figures, the checkpoint first, then the turn as usual. '' for a new session, the butler's family
        (it compacts; a project's lead rebuilds its session from the disk) and a 代答 topic, and when the previous turn's window
        is not known."""
        topic = job.topic_id or ctx.topic_id
        if not session_id or not job.checkpoint_path or proxy_topic(ctx) or is_butler_topic(topic, job.chat_id):
            return ""
        try:
            last = next((r for r in self.harness.topic_records(ctx, job.chat_id)
                         if r.get("job_id") != job.job_id and r.get("status") in ENDED), None)
        except (OSError, ValueError, AttributeError):
            return ""
        line = context_high(last)
        return "\n\n" + line if line else ""

    @staticmethod
    def _criteria_note(ctx: BridgeContext, job: ReplyJob) -> str:
        """The topic's completion criteria and how its checkpoint marks them (core/criteria.py note), '' for a topic
        without: its message is as before."""
        found = criteria.note(criteria.read(ctx.state_dir, job.topic_id or ctx.topic_id))
        return "\n\n" + found if found else ""

    def _images_note(self, ctx: BridgeContext, job: ReplyJob) -> str:
        """show_images's rule in the message of a ``slim`` turn (its rules lack it) whose owner's words are about an image
        to show at once (core/checkpoint.py images_note); '' otherwise."""
        note = images_note(job.user_text) if self.slim(ctx, job) else ""
        return "\n\n" + note if note else ""

    @staticmethod
    def _gist_note(ctx: BridgeContext, job: ReplyJob) -> str:
        """话题要点's rule and the topic's current gist (COVEY_PROJECT_TIDY on, an ordinary topic or a project's topic: not the
        butler's family, not a lead), '' otherwise (core/checkpoint.py TOPIC_GIST_RULE)."""
        from covey.core import project_proposals, topic_gist

        topic = job.topic_id or ctx.topic_id
        if not project_proposals.enabled() or not topic or is_butler_topic(topic, job.chat_id):
            return ""
        return "\n\n" + gist_note(topic_gist.lines(topic_gist.read(ctx.state_dir, topic)))

    @staticmethod
    def _revisit_note(ctx: BridgeContext, job: ReplyJob) -> str:
        """到点回来看's rule (COVEY_REVISIT=1, an ordinary topic: app/revisits.py ordinary), '' otherwise."""
        from covey.app import revisits

        topic = job.topic_id or ctx.topic_id
        if not revisits.records.enabled() or not revisits.ordinary(ctx.state_dir, topic, job.chat_id):
            return ""
        return "\n\n" + revisits.REVISIT_RULE

    def _ask_note(self, ctx: BridgeContext, job: ReplyJob) -> str:
        """话题转交管家's rule (app/butler_asks.py: COVEY_PROJECTS=1 with the butler on, the owner's own turn of a topic
        in a chat with a butler), '' otherwise."""
        from covey.app import butler_asks

        if not butler_asks.offered(self.runtime.access, ctx.state_dir, job, job.topic_id or ctx.topic_id):
            return ""
        return "\n\n" + butler_asks.RULE

    def _collab_note(self, ctx: BridgeContext, job: ReplyJob) -> str:
        """协作 (app/collab_flow.py, COVEY_COLLAB=1): the rule for asking other topics in the owner's own turn of a topic
        that may, or in an integrator's continuation what the owner said since; '' otherwise."""
        from covey.app import collab_flow

        try:
            return collab_flow.turn_note(self.runtime.access, self.harness, ctx, job)
        except Exception as exc:  # noqa: BLE001 - the turn goes on without it
            self.runtime.log(f"collab note unavailable job=#{job.job_id}: {type(exc).__name__}")
            return ""

    def _continue_previous(self, ctx: BridgeContext, job: ReplyJob) -> dict | None:
        """A task of the topic waiting for answers when ``job`` starts without being one: it becomes continued and
        its card says its questions went unanswered. Its checkpoint, which ``job`` goes on from; None without one. A turn whose
        source may not answer a waiting question (core/turn_source.py answers_questions: a 协作 turn) leaves it waiting."""
        if not turn_source.behavior(job).answers_questions:
            return None
        previous = next((r for r in self.harness.topic_records(ctx,job.chat_id)
                         if r.get("status") == "waiting_user" and r.get("checkpoint_path")), None)
        if not previous:
            return None
        # A message queued before that task asked, starting now: not an answer to its questions,
        # which stay open (repo review U8: the card said 已收到回答 and lost all but its body).
        prior = read_checkpoint(Path(previous["checkpoint_path"]))
        upsert_job_record(ctx.state_dir, previous["key"], status="continued",
                          status_message=f"#{job.job_id} 已接着开始，这组问题没有回答", resumed_by=job.message_id)
        paint_answered(self.cards, self.runtime.log, ctx, job.chat_id, job.job_id, previous, answered=False)
        return prior

    @staticmethod
    def _image_attachments(ctx: BridgeContext, prompt: str) -> list[dict[str, str]]:
        attachments = native_image_attachments(prompt, ctx.history_path.parent / 'incoming_media')
        for item in native_image_attachments(prompt, ctx.state_dir / 'incoming_media'):
            if item not in attachments:
                attachments.append(item)
        return attachments

    def _fit_images(self, ctx: BridgeContext, job: ReplyJob, caps, prompt: str, attachments: list[dict[str, str]]):
        """(prompt, attachments): the images the Harness takes whole go with the prompt. One over its limits
        (``caps.image_bytes``, ``caps.image_frame``) is left out rather than fail the turn (Claude
        Code ends such a turn with protocol_error): the prompt tells the model, whose message
        already names its path; the task card says so in a line of the Bridge's own; the log in one line."""
        if not attachments or not (caps.image_bytes or caps.image_frame):
            return prompt, attachments
        kept, skipped = fit_images(attachments, limit=caps.image_bytes, frame=caps.image_frame,
                                   used=len(prompt.encode()))
        while skipped:
            note = skipped_images_note(skipped, caps.image_bytes)
            sizes = sum(encoded_size(_file_size(item)) for item in kept)
            if not caps.image_frame or not kept or len((prompt + note).encode()) + sizes <= caps.image_frame:
                break
            # The note itself took the room of the last image kept.
            last = kept.pop()
            skipped.append({**last, "size": _file_size(last), "reason": "frame"})
        if not skipped:
            return prompt, attachments
        self.runtime.log(f"images not attached job=#{job.job_id} skipped={len(skipped)} kept={len(kept)} "
                         f"sizes={','.join(str(item['size']) for item in skipped)} "
                         f"reasons={','.join(item['reason'] for item in skipped)}")
        if job.tool_trace is not None:
            at = _dt.datetime.now().astimezone().strftime("%H:%M")
            try:
                self.interaction.structured_harness_event(ctx, job, "progress_note", {
                    "id": f"bridge-images-{time.time_ns()}", "title": BRIDGE_NOTE,
                    "text": skipped_images_card_note(skipped, caps.image_bytes, at)}, None, from_backend=False)
            except Exception as exc:  # noqa: BLE001 - the turn goes on; only the card line failed
                self.runtime.log(f"images note not shown: {type(exc).__name__}")
        return prompt + note, kept

    def _leave_checkpoint_question(self, ctx: BridgeContext, job: ReplyJob, result: RunResult) -> None:
        """A checkpoint that asks the user becomes a deferred question; the reply is kept either way.
        A 代答 topic (app/proxy.py) never waits for the owner: the owner asked that its replies go out
        without their confirmation, so its question is dropped and the turn ends with its reply."""
        cp = read_checkpoint(Path(job.checkpoint_path)) if job.checkpoint_path else {}
        if cp.get("requires_user") is True and proxy_topic(ctx):
            release_user_wait(Path(job.checkpoint_path), drop_questions=True)
            self.runtime.log(f"proxy job=#{job.job_id} checkpoint question dropped")
            return
        question_allowed = result.outcome is None or (result.outcome.status == "completed" and result.outcome.resume_allowed)
        # Something to ask (core/checkpoint.py needs_owner): ``question``, or only ``questions``, which used to stand 等你
        # with no card; ``requires_user`` with neither waits for nothing.
        asks = question_allowed and not result.is_error and needs_owner(cp)
        if question_allowed and not result.is_error and cp.get("requires_user") is True and not asks:
            self.runtime.log(f"checkpoint asks nothing job=#{job.job_id}: requires_user without a question; not waiting")
        # Only a native question still waiting holds the checkpoint's back; answered ones do not.
        if asks and self.questions.has_open_native_questions(ctx, job.job_id):
            self.runtime.log(f"checkpoint question skipped job=#{job.job_id}: native question still open")
            asks = False
        if asks:
            # Registration is delivery bookkeeping, not a second model result.
            # Preserve a successful backend reply even if question storage fails.
            job.checkpoint_question_deferred = True
            job.checkpoint_question_registration_pending = True
            try:
                upsert_job_record(ctx.state_dir, job.message_id, final_text=result.text,
                                  execution_status="waiting_user", delivery_ok=False,
                                  checkpoint_question_deferred=True, checkpoint_question_registration_pending=True)
                # As the turn wrote them, made into what a card takes (core/checkpoint.py normalize_questions).
                questions = normalize_questions(cp.get("questions"), question_summary(cp))
                self.questions.defer_checkpoint_question(ctx, job, questions)
                upsert_job_record(ctx.state_dir, job.message_id, checkpoint_question_registration_pending=False)
                job.checkpoint_question_registration_pending = False
            except Exception as exc:
                job.checkpoint_question_error = "问题登记失败（" + type(exc).__name__ + "）"
                self.runtime.log(job.checkpoint_question_error)
                try:  # The first try: a redelivery's second one that fails too stops the wait (app/redelivery.py).
                    upsert_job_record(ctx.state_dir, job.message_id, checkpoint_question_tries=1)
                except Exception as error:  # noqa: BLE001 - its redelivery then tries once more before it stops
                    self.runtime.log(f"checkpoint question tries not kept: {type(error).__name__}")

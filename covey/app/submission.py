"""Submitting a topic's message as a job; a topic without a workspace waits for its launch card."""

from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import Any

from covey.core import background, devices, failure, home, placement, timing, turn_source
from covey.core import env as cenv
from covey.core.jobs import BridgeReplyJob, carry_device, find_job_record
from covey.core.metrics import mark, run_start, took_text
from covey.core.model_settings import recorded_model_settings, recorded_work_mode
from covey.core.deliveries import topic_steer
from covey.cards.review import recent_user_questions, render_stream_status
from covey.cards.streaming import steer_label
from covey.core.scheduler import keeps_slot, room_for, topic_limit, worker_limit
from covey.core.butler import HOLD_STATUS, traits_of
from covey.core.state import write_json
from covey.core.history_storage import recent_history
from covey.core.topics import bind_topic, proxy_topic, read_topic, workspace_locked
from covey.core.locks import LOCK, topic_lock
from covey.core.workspaces import load_config, resolve_selection, topic_context, touch_workspace
from covey.app import butler_reactions, relay_stops
from covey.app.card_actions import action_value
from covey.app.card_lanes import after_ack
from covey.app.runtime import job_stage
from covey.cards.recovery import DIAGNOSE_KIND, SPLIT_KIND
from covey.app.runner_spawn import prestart_runner
from covey.app.task_card import card_failure_notice
from covey.app.launch import WAITING_STATUS, publish_launch_cards
from covey.app.launch_submit import launch_in_progress


def steers_on_arrival(job, ahead) -> bool:
    """Whether ``job``, a topic's message just queued behind ``ahead`` (the topic's tasks before it), goes at once into
    the task it is bound to (``steer_target_job_id``, the last of them), as a click on its queued card's 补充 sends it
    (app/steering.py steer_on_arrival): the switch is on (``topic_steer``), the Harness takes supplements (not dsh),
    the topic is not the butler's family (its supplements have no receipt card and go their own way, app/butler.py
    supplement) and that task has started. One waiting for an answer, closing or ended is tried too: it takes nothing,
    and the card says why."""
    target = ahead[-1] if ahead else None
    return (target is not None and target.job_id == job.steer_target_job_id and topic_steer()
            and getattr(job.harness_capabilities, "steering", False) is True
            and not traits_of(job).lists_supplements and job_stage(target) != "queued")


# 追问只发答案卡 (docs/cards.md): a topic's follow-up holds its task card back this many seconds at most (``quick_hold``).
QUICK_ENV = "COVEY_QUICK_ANSWER_SECONDS"
QUICK_SECONDS = 10
QUICK_MAX = 120


def quick_seconds(environ=None) -> int:
    """COVEY_QUICK_ANSWER_SECONDS: how long a topic's follow-up holds its task card back (``quick_hold``); QUICK_SECONDS
    when unset or not a whole number from 0 to QUICK_MAX; 0 holds none (every turn's card at once, as before)."""
    raw = str(cenv.get(QUICK_ENV, "", environ) or "").strip()
    try:
        seconds = int(raw)
    except ValueError:
        return QUICK_SECONDS
    return seconds if 0 <= seconds <= QUICK_MAX else QUICK_SECONDS


def follow_up(job) -> bool:
    """Whether ``job`` follows a turn of its topic that answered: its history (as read when it was queued, back to the
    topic's last reset) has an answer in it. A topic's first message is no follow-up."""
    return any(isinstance(row, dict) and row.get("role") == "assistant" for row in job.history_snapshot or ())


def quick_hold(ctx, job) -> int:
    """How many seconds ``job``, a message the owner sent in a topic's thread (or on the web page) that starts at once, holds its task card
    back (``quick_seconds``; app/cards.py Cards.reveal sends it once the turn has run them, made its first step or
    asks, app/worker.py only its answer card when it answered before): a follow-up (``follow_up``) in a topic's own
    turn (core/placement.py TOPIC_ROLE, MEMBER_ROLE) whose answer goes on its own in its thread, not a 代答 topic's.
    0 for any other: its card at once."""
    seconds = quick_seconds()
    # The owner's words: a Feishu message (om_), or the web page's (web-<op_id>, core/web_sync.py PREFIX): its card from the task
    # runner too, so the page's say is acknowledged without waiting for the card's create and send (2026-11-25 T765, about 1 s).
    if (not seconds or not str(job.message_id or "").startswith(("om_", "web-")) or not follow_up(job)
            or proxy_topic(ctx)):
        return 0
    try:
        where = placement.plan(ctx.state_dir, job)
    except Exception:  # noqa: BLE001 - its card at once, as before
        return 0
    if where.role not in (placement.TOPIC_ROLE, placement.MEMBER_ROLE) or where.row.answer != placement.THREAD:
        return 0
    return seconds


def capacity_words(job) -> int:
    """How many tasks the card and the reply say may run at once for ``job``: the limit for the butler's and the leads' turns,
    the ordinary topics' share of it for any other (core/scheduler.py topic_limit)."""
    limit = worker_limit()
    return limit if keeps_slot(job) else topic_limit(limit)


def queued_line(job) -> str:
    """The status line of a topic's queued card with a task before it (docs/ui.md 5.2: the status line is the
    conclusion): where its message goes. '' with none before it (等待处理)."""
    target = job.steer_target_job_id
    return f"这句话会在 #{target} 结束后单独执行" if target else ""


def steering_line(job) -> str:
    """The status line of a queued card whose message is being sent into the task before it (``steers_on_arrival``):
    its receipt or, should nothing go, ``queued_line`` follows within seconds."""
    return f"正在补充到 #{job.steer_target_job_id}"


def queue_hint(job):
    """What a queued card says its message waits for and what its buttons do. The butler's slim card keeps its words
    (its 补充 stays 补充, cards/streaming.py steer_label)."""
    if getattr(job.harness_capabilities, "steering", False):
        target = job.steer_target_job_id
        if target and not traits_of(job).slim_card:
            return f"点“{steer_label(job)}”，这句话马上交给 #{target}，不再单独执行；点“停止”取消排队。"
        if target:
            return (f"本条还没执行。点“补充当前任务”打开补充表单（已填好本条，可改），提交后送入 #{target}，"
                    "本条不再单独执行；点“停止”取消本条排队。")
        return "这条消息还没传给正在执行的任务。点本卡“补充当前任务”可直接送入；点“停止”取消排队。"
    return "这条消息排在前面的任务之后执行；点本卡“停止”取消排队。"


def started_at(record):
    """When the task of ``record`` began to run: core/metrics.py run_start, the start of the task card's 已运行 and
    用时; None before it started."""
    return run_start(record.get("timings"))


def queue_status(state_dir, ahead, now=None):
    """A queued card's stage: how many of the topic's tasks are before it and, for the one running, how long it
    has run as the card is drawn and when it began (repo review U3: the owner stopped tasks that had run 85
    minutes to 6 hours to get a message in, and the card did not say how long they had run)."""
    running = next((job for job in ahead if job.execution_started), None)
    began = None
    if running is not None:
        # The saved record has what the task runner marked; this process's job what it marked itself (a record
        # saved at queueing knows only ``queued``). One start from both, as the running card's 已运行 counts.
        record = find_job_record(state_dir, running.message_id) or {}
        saved = record.get("timings") if isinstance(record.get("timings"), dict) else {}
        own = running.timings if isinstance(getattr(running, "timings", None), dict) else {}
        began = started_at({"timings": {**own, **saved}})
    if len(ahead) == 1:
        line = f"排队中 · 排在 #{ahead[0].job_id} 后面"
    else:
        line = f"排队中 · 本话题前方 {len(ahead)} 个任务"
    if began is not None:
        took = took_text(max(0, (time.time() if now is None else now) - began))
        line += f" · #{running.job_id} 已运行 {took}（{time.strftime('%H:%M', time.localtime(began))} 开始）"
    return line



class HarnessChanged(ValueError):
    """The topic was bound to another Harness (换配置 or the launch card) after this submission was routed."""

class Submission:
    """One Harness's submission of a topic's work: the context of a topic's message, a new job
    from it, and holding that job until its topic has a workspace (the launch card) before it
    is queued behind the topic's earlier jobs.

    - ``harness``: whose jobs, sessions and voice.
    - ``runtime``: the job registry and queue, and the log.
    - ``messenger``: the queued notices.
    - ``cards``: the new job's task card.
    - ``models``: the model and effort a job runs with.
    - ``controls``: an answered task's card is acknowledged when its continuation is queued.
    - ``record_job_state``: the Harness's job-state recorder (late-bound: it sits above).
    - ``topics``: the Bridge's Harnesses, for the launch card (None in an app built alone).
    - ``steering``: a topic's message sent while its task runs goes into it (``steers_on_arrival``,
      app/steering.py steer_on_arrival); None, never.
    """

    def __init__(self, harness, *, runtime, messenger, cards, models, controls, record_job_state, topics,
                 steering=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.voice = harness.voice
        self.cards = cards
        self.models = models
        self.controls = controls
        self.record_job_state = record_job_state
        self.topics = topics
        self.steering = steering

    def event_context(self, ctx, event):
        ctx = self.models.configured_context(ctx)
        # Again inside the topic: its own snapshot overrides the Harness defaults.
        return self.models.configured_context(topic_context(ctx, event, self.harness.store.sessions_dir))

    def follow_up(self, ctx, event):
        """看原因 and 拆小再试 on a task card that did not succeed (core/failure.py actions): a turn of their own in the
        topic, queued as a message of the owner's would be, after the click is acknowledged."""
        raw = action_value(event)
        name = {DIAGNOSE_KIND: failure.DIAGNOSE, SPLIT_KIND: failure.SPLIT}.get(raw.get("kind"))
        if not name or not ctx.topic_id or raw.get("topic_id") != ctx.topic_id:
            return True
        chat_id = str(event.get("chat_id") or "")
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        try:
            job_id = int(raw.get("job_id") or 0)
        except (ValueError, TypeError):
            return True

        def run():
            text = self.queue_follow_up(ctx, chat_id, event_id, job_id, name)
            if text:
                self.messenger.reply(ctx, chat_id, text, self.voice.key("follow-up", event_id), voice=self.voice)
        if raw.get(relay_stops.RELAY):
            # On a stopped relay at the top level: the same follow-up, then its card redrawn in place (app/relay_stops.py).
            after_ack(lambda: relay_stops.clicked(self, ctx, event, raw,
                                                  lambda: self.follow_up_job(ctx, chat_id, event_id, job_id, name)))
        else:
            after_ack(run)
        return True

    def queue_follow_up(self, ctx, chat_id, message_id, job_id, name):
        """Queue the follow-up ``name`` of task #``job_id``; what to tell the owner when it was not queued, else ""."""
        return self.follow_up_job(ctx, chat_id, message_id, job_id, name)[0]

    def follow_up_job(self, ctx, chat_id, message_id, job_id, name):
        """``queue_follow_up``'s words, and the job id of the follow-up it queued (None when it queued none: refused, or the
        submission said why in the topic itself)."""
        if self.runtime.jobs.topic_jobs(ctx.topic_id, chat_id):
            return "本话题还有运行或排队的任务，完成或停止后再点。", None
        record = next((r for r in self.harness.topic_records(ctx, chat_id) if r.get("job_id") == job_id), None)
        view = failure.failure_view(record) if record else None
        if view is None or name not in view["actions"]:
            return "这个任务现在没有这一步可做。", None
        content, prompt = failure.follow_up(name, record, view)
        # Still the task of whoever sent its message (app/tool_limits.py READ), whoever clicked.
        job = self.enqueue_chat_job(ctx, content, chat_id, message_id, prompt_content=prompt,
                                    sender_id=str(record.get("sender_id") or ""))
        return "", (getattr(job, "job_id", None) if job is not None else None)

    def submit_topic_job(self, ctx, job, *, steer=False):
        """Decide and register under LOCK; the cards and notices after it, in this topic's order. ``steer``: a
        topic's message, which goes into the task before it when it runs (``steers_on_arrival``). The plan."""
        with topic_lock(ctx):
            self._release_waiting(ctx, job.chat_id)  # A started topic's launch messages go first.
            timing.mark("records")
            with LOCK:
                plan = self.reserve_submission(ctx, job, steer=steer)
            timing.mark("reserve")
            self.publish_submission(ctx, job, plan)
        return plan

    def release_waiting(self, ctx, chat_id):
        """Queue the messages that waited for this topic's launch, oldest first: after 开始任务 is
        acknowledged, or at startup when a restart came between the two. How many it queued."""
        with topic_lock(ctx):
            return self._release_waiting(ctx, chat_id)

    def _release_waiting(self, ctx, chat_id):
        """The caller holds the topic lock. Nothing while the topic still waits for its launch card."""
        records = self.harness.topic_records(ctx, chat_id, status="waiting_workspace")
        if not records or not workspace_locked(ctx):
            return 0
        for record in sorted(records, key=lambda r: int(r["job_id"])):
            job = self.job_from_record(ctx, record)
            with LOCK:
                plan = self.reserve_submission(ctx, job)
            self.publish_submission(ctx, job, plan)
        return len(records)

    def reserve_submission(self, ctx, job, *, steer=False):
        """What this submission does, decided under LOCK. A queued job is registered here, so a
        binding switch or idle maintenance sees the topic busy before its card exists. ``steer``: as
        ``submit_topic_job`` says; the plan's ``steer`` is whether it goes into the task before it."""
        self.runtime.refuse_new_job()  # Not in a task runner: before the job is registered or any record written.
        data = read_topic(ctx)
        if data.get('harness_id') != self.harness.spec.name:
            raise HarnessChanged('话题 Harness 已改变，请在当前选择下重新提交')
        job.harness_id = self.harness.spec.name
        job.binding_revision = int(data.get('binding_revision', 1))
        locked = workspace_locked(ctx)  # Setting harness_locked below does not change it.
        if locked and data.get('harness_locked') is not True:  # Once: no rewrite of topic.json under LOCK per message (T680).
            data['harness_locked'] = True
            write_json(ctx.history_path.parent / 'topic.json', data, ensure_ascii=False)
        if ctx.options.get("workspace_selection") and not locked:
            self.record_job_state(
                ctx, job, "waiting_workspace", WAITING_STATUS, history_snapshot=job.history_snapshot
            )

            return {"wait": "launching" if launch_in_progress(ctx) else "select"}
        ahead = self.runtime.jobs.ahead_of(job)
        if job.steer_target_job_id is None:
            job.steer_target_job_id = ahead[-1].job_id if ahead else 0
        # Full as the scheduler counts it (core/scheduler.py room_for: the butler's and the leads' turns take the reserved slots too).
        limit = worker_limit()
        topics = (self.runtime.jobs.topic_count(), self.runtime.jobs.topic_count(lambda each: not keeps_slot(each)))
        capacity_full = not room_for(job, topics, limit)
        from covey.core.harness import backend_capabilities
        job.harness_capabilities = backend_capabilities(self.harness.backend, ctx)
        self.runtime.jobs.register(job)
        return {"ahead": len(ahead), "jobs_ahead": ahead, "capacity_full": capacity_full,
                "steer": steer and self.steering is not None and steers_on_arrival(job, ahead),
                "follow_up": steer}

    def publish_submission(self, ctx, job, plan):
        if plan.get("wait") == "launching":
            timing.outcome("launch_waiting")
            # The launch already submitted is creating the worktree; it starts this message too.
            self.messenger.reply(ctx, job.chat_id, "独立 worktree 正在创建，本条消息会在任务开始后依次执行。",
                                 self.voice.key("launch-waiting", job.message_id), voice=self.voice)
            return
        if plan.get("wait"):
            self.selection_card(ctx, job.chat_id, job.message_id)
            return
        ahead, capacity_full = plan["ahead"], plan["capacity_full"]
        steering = plan.get("steer", False)  # It goes into the task before it next (Steering.steer_on_arrival).
        held = (getattr(job, "start_held_until", 0) or 0) > time.time()
        status = (queue_status(ctx.state_dir, plan.get("jobs_ahead") or []) if plan.get("jobs_ahead")
                  else f"排队中 · 本话题前方 {ahead} 个任务" if ahead
                  else f"排队中 · 并发已满（最多 {capacity_words(job)} 个任务）" if capacity_full
                  else HOLD_STATUS if held else "已入队")
        # The status line, the card's first: where the message goes (the butler's slim card takes its own from the body).
        line = (steering_line(job) if steering else queued_line(job)) if ahead and not traits_of(job).slim_card else ""
        # A held one may wait longer than a prestarted runner waits for its handoff.
        if not ahead and not capacity_full and not held:
            # It runs next: its task runner starts while its card and record are written (S35).
            prestart_runner(self.runtime, job, ctx)
        recorded = False
        try:
            # A butler turn of the 话题区 layout (core/placement.py Row.quiet): none yet either; its task runner
            # sends it once the turn has run CARD_AFTER seconds (Cards.reveal), the owner's message gets OnIt below.
            # A 协作 ask's (core/turn_source.py own_delivery): never one; its outcome is its collaboration record's.
            job.card_deferred = placement.quiet(ctx.state_dir, job) or not turn_source.behavior(job).own_delivery
            if not job.card_deferred and plan.get("follow_up") and not ahead and not capacity_full and not held:
                # A topic's follow-up that starts at once (追问只发答案卡): its card too, for a few seconds (quick_hold).
                job.card_after = quick_hold(ctx, job)
                job.card_deferred = job.card_after > 0
            if job.card_deferred:
                # What its card would be drawn with, its answer card's 输入 too (the workspace was "—").
                try:
                    self.cards.card_facts(ctx, job)
                except Exception as exc:  # noqa: BLE001 - the answer card falls back to the job's own fields
                    self.runtime.log(f"held card facts unavailable job=#{job.job_id}: {type(exc).__name__}")
            # Being sent into the task before it, the card says no more than where it waits: its receipt follows.
            job.progress = None if job.card_deferred else self.cards.create_streaming_reply(
                ctx, job, render_stream_status(job, status, [queue_hint(job)] if ahead and not steering else []),
                **({"status": line} if line else {}))
            card_notice = ""
            if not job.progress and self.cards.streaming and not job.card_deferred:
                # Read the failure now: later calls on this thread replace it.
                card_notice = card_failure_notice(job)
            answered = self.controls.acknowledge_waiting_answer(ctx, job)
            # Not its history_snapshot: a restart rebuilds a queued job from queued/<key>.json (S41).
            self.record_job_state(ctx, job, "queued", status)
            recorded = True
            timing.mark("recorded")
            # Read before it is queued: a task runner may take the job, and its card, at once (S37: the
            # card was gone by the time the answered card was repainted, and a text notice went out).
            carded = job.progress is not None or job.card_deferred
            self.runtime.enqueue_reply_job(job, self.harness.name)
            timing.outcome("queued_card" if carded else "queued_text")
        except BaseException as exc:
            self.runtime.unregister_reply_job(job)  # Never queued: it must not keep the topic busy.
            if recorded and isinstance(exc, Exception):
                self.controls.not_queued(ctx, job, exc)  # Nor may its record stay queued with nothing to run it.
            raise
        if answered is not None:
            # The answered task's card, once the continuation is queued, beside what its caller does next: the
            # answer's question card waited 470 ms for it (S48). Writes to one card still go one at a time.
            repaint = timing.carry(functools.partial(self._repaint_answered, answered), "answered")
            try:
                background.start(background.CARD_UPDATES, repaint)
            except RuntimeError:  # No thread to spare: here, as before.
                timing.drop(repaint)
                self._repaint_answered(answered)
        if job.card_deferred:
            butler_reactions.start(self.messenger, ctx.state_dir, ctx.profile, job, self.runtime.log)
        if job.top_message and not steering:
            # A message quoted from outside its topic's thread (引用直达): its card is in that thread, so the message says
            # it is queued: OnIt, or 排队中 while tasks of its topic or a full house are before it. One being sent into
            # the task before it says what became of it instead (Steering.steer_queued_job).
            butler_reactions.top_queued(self.messenger, ctx.profile, job, self.runtime.log,
                                        waits=bool(ahead or capacity_full or held))
        if carded:
            return  # The queued card says all of it and changes with the message; no second notice.
        if steering:
            return  # What became of it is said next (Steering.steer_queued_job), not that it is queued.
        if card_notice:
            # The card should exist: say it is missing and what that costs, not just "queued".
            notice = card_notice
            if ahead:
                notice += f"\n前面有 {ahead} 个同话题任务，本条排在它们之后。"
            elif capacity_full:
                notice += f"\n当前并发名额已满（最多 {capacity_words(job)} 个任务），本条已排队。"
            self.runtime.log(f"task card missing job=#{job.job_id}")
            self.messenger.reply(ctx, job.chat_id, notice, self.voice.key("ack", job.message_id), voice=self.voice)
        elif ahead:
            self.messenger.reply(
                ctx, job.chat_id, f"已收到，前面有 {ahead} 个同话题任务。本条消息已排队，尚未传给正在执行的任务。",
                self.voice.key("queue", job.message_id), voice=self.voice)
        elif capacity_full:
            self.messenger.reply(
                ctx, job.chat_id, f"已收到，当前并发名额已满（最多 {capacity_words(job)} 个任务），本条消息已排队。",
                self.voice.key("queue", job.message_id), voice=self.voice)
        else:
            self.messenger.reply(
                ctx, job.chat_id, "任务已入队，完成后会在本话题回复。", self.voice.key("ack", job.message_id), voice=self.voice)

    def _repaint_answered(self, answered):
        try:
            answered()
        except Exception as exc:  # noqa: BLE001 - its job is queued; only its old card is not repainted
            self.runtime.log(f"answered task card not repainted: {type(exc).__name__}")

    def job_from_record(self, ctx, r):
        model, effort, explicit = recorded_model_settings(ctx, r)
        job = BridgeReplyJob(
            timings=dict(r.get('timings', {})),
            job_id=int(r["job_id"]),
            user_text=r["user_text"],
            prompt_text=r.get("prompt_text") or None,
            steer_target_job_id=r.get("steer_target_job_id"),
            chat_id=r["chat_id"],
            message_id=r["message_id"],
            timeout=ctx.timeout,
            model=model,
            effort=effort,
            model_settings_snapshot=explicit,
            work_mode=recorded_work_mode(r) if explicit else ctx.work_mode,
            topic_id=ctx.topic_id,
            reply_to_message_id=ctx.reply_to_message_id,
            workspace_name=ctx.workspace_name,
            workdir=ctx.workdir,
            history_path=ctx.history_path,
            session_path=ctx.session_path,
            sessions_dir=ctx.sessions_dir,
            history_snapshot=r.get("history_snapshot"),
            recent_questions=recent_user_questions(r.get("history_snapshot") or [], r["user_text"]),
            # Who sent it, as the record says: a group member's message that waited for its launch card stays theirs
            # (app/tool_limits.py READ), and the owner's his (his principles, his memory).
            sender_id=str(r.get("sender_id") or ""),
        )
        carry_device(job, ctx)
        job.read_only = r.get("read_only") is True
        job.turn_source = r.get(turn_source.FIELD) or {}  # A source it was made with goes on with it (app/job_dispatch.py).
        job.top_message = str(r.get("top_message") or "")  # Its reactions too, once it is queued (publish_submission).
        job.harness_id = r.get('harness_id') or r.get('backend') or self.harness.spec.name
        job.binding_revision = int(r.get('binding_revision') or ctx.options.get('binding_revision', 1))
        return job

    def start_workspace(self, ctx, chat_id, name, directory=None, *, on_bound=None):
        """Bind the topic and release its waiting messages; `directory` overrides the
        workspace's own path (a topic worktree). `on_bound()` runs once the binding is saved
        (the launch card's receipt). The messages' task cards (seconds each) follow once the click
        is acknowledged (after_ack): inside it 开始任务 took 2.6 s of Feishu's 3 s.
        A message sent meanwhile releases them before itself (submit_topic_job), so order holds.
        A `directory` that is the home (app/launch_submit.py home_binding) is bound as it is: the workspace list is
        neither read nor written (no ``recent``)."""
        at_home = directory is not None and Path(directory).resolve() == home.of(ctx).resolve()
        with topic_lock(ctx):
            with LOCK:
                if at_home:
                    binding = devices.Binding(Path(directory), "", "", "")
                else:
                    listed = load_config(ctx.state_dir, ctx.default_workdir)["workspaces"].get(name, "")
                    if devices.is_device_value(listed):
                        # Already checked on the device when the launch card was submitted (app/launch_submit.py): no second
                        # 5 seconds under LOCK. The topic keeps the value; its context the stand-in and the device's directory.
                        binding = devices.bind_workdir(ctx, listed, strict=False)
                    else:
                        name, resolved = resolve_selection(ctx.state_dir, ctx.default_workdir, name)
                        binding = devices.Binding(directory or resolved, "", "", "")
                if workspace_locked(ctx):
                    return "本话题已经开始，工作区已固定。新任务请在主聊天发送消息。"
                bind_topic(ctx, name, binding.workdir, stored=binding.value if binding.device else "")
                if not at_home:
                    touch_workspace(ctx.state_dir, ctx.default_workdir, name)
                ctx = devices.with_binding(ctx, binding, workspace_name=name,
                                           sessions_dir=self.harness.store.sessions_dir(binding.workdir))
            if on_bound is not None:
                on_bound()  # Outside LOCK: it may talk to Feishu.
        after_ack(functools.partial(self.release_waiting, ctx, chat_id))
        return ""

    def selection_card(self, ctx, chat_id, message_id, *, new_topic=False):
        """Publish the launch card (model, effort, workspace) for a topic that has none of them yet."""
        return publish_launch_cards(self.topics, self.voice, ctx, chat_id, message_id, new_topic=new_topic)

    def enqueue_chat_job(
        self, ctx: Any, content: str, chat_id: str, message_id: str,
        *, prompt_content: str | None = None, recovery_context: dict | None = None, received_at=None, title: str = "",
        sender_id: str = "", hold: float = 0, policy_retry: dict | None = None, steer: bool = False,
        read_only: bool = False, source: dict | None = None, top: str = "",
    ) -> BridgeReplyJob | None:
        """Submit one message of ctx's topic as a new job of this Harness; ``sender_id`` is who sent it.
        ``hold``: seconds it waits before it starts (the butler's, app/butler.py). ``policy_retry``: an
        answer's continuation of a retry after a safety refusal keeps its mark (app/policy_retry.py). ``read_only``:
        a turn the butler started at once because it judged it read-only (app/tool_limits.py LOOK). ``steer``: a
        message the owner (or whoever the topic serves) sent in the topic (app/app.py handle_event): while the task
        before it runs, it goes into that task (``steers_on_arrival``), outside the topic's lock as a 补充 click
        does; the job is then cancelled, its card the receipt. ``source``: the job's 轮次来源 as the Bridge made it
        (core/turn_source.py make), none for the owner's and every message as before; a source whose words are not the
        owner's starts no owner turn of a lead and never goes into a running task. ``top``: the message whose reactions
        say how the job stands, because its card is not under it (app/butler_reactions.py top_message: a message
        quoted from outside the topic's thread, or the answer, held note or supplement of one). In a task runner it
        raises RuntimeError before anything is written or sent (Runtime.refuse_new_job): a runner never starts a job."""
        self.runtime.refuse_new_job()
        try:
            ctx = self.models.prepare_submission_context(ctx)
        except (ValueError, OSError, RuntimeError) as exc:
            self.messenger.reply(ctx, chat_id, "任务未提交：模型配置无法确认。" + str(exc),
                                 self.voice.key("model-config-error", message_id), voice=self.voice)
            butler_reactions.top_refused(self.messenger, ctx.profile, top, self.runtime.log)  # Said in its topic's thread.
            return None
        history_snapshot = recent_history(ctx.history_path)
        timing.mark("prepare")
        job = BridgeReplyJob(
            job_id=self.runtime.jobs.next_id(),
            user_text=content,
            chat_id=chat_id,
            message_id=message_id,
            timeout=ctx.timeout,
            model=getattr(ctx,"model",""), effort=getattr(ctx,"effort",""), model_settings_snapshot=True,
            work_mode=getattr(ctx, "work_mode", ""),
            topic_id=getattr(ctx, "topic_id", ""),
            reply_to_message_id=getattr(ctx, "reply_to_message_id", ""),
            prompt_text=prompt_content or None,
            workspace_name=ctx.workspace_name,
            workdir=ctx.workdir,
            history_path=ctx.history_path,
            session_path=ctx.session_path,
            sessions_dir=ctx.sessions_dir,
            recent_questions=recent_user_questions(history_snapshot, content),
            history_snapshot=history_snapshot, recovery_context=recovery_context, title=title, sender_id=sender_id,
        )
        carry_device(job, ctx)
        mark(job, 'received', received_at)
        if policy_retry:
            job.policy_retry = dict(policy_retry)
        if read_only:
            job.read_only = True
        if source:
            job.turn_source = dict(source)
        if top:
            job.top_message = top
        if hold:
            job.start_held_until = time.time() + hold
        from covey.app import project_loop

        acts = turn_source.behavior(job)
        if acts.owner_words:
            project_loop.owner_turn(ctx.state_dir, job.topic_id, chat_id, message_id)  # The owner's turn in a lead's topic.
        plan = self.submit_topic_job(ctx, job, steer=steer and acts.steers)
        if plan.get("steer") and not self.steering.steer_on_arrival(ctx, chat_id, job, message_id) and job.top_message:
            # Not taken: it stays queued behind the task it was bound to (or starts, that task having ended).
            butler_reactions.top_queued(self.messenger, ctx.profile, job, self.runtime.log, waits=True)
        return job

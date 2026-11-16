"""BridgeApp: one Harness's components and its entry points.

The Bridge (app/unified.py) builds one BridgeApp per Harness, over the process's
Runtime and Messenger, and routes each topic's messages, clicks and jobs to the app of
the topic's Harness. A BridgeApp holds that Harness's components, each a plain class in
its own module that receives its collaborators here:

- ``cards`` (app/cards.py): every task and process card write.
- ``sessions`` (app/sessions.py): the topic's native session pointer.
- ``models`` (core/model_settings.py): the model and effort a job runs with.
- ``artifacts``, ``switch`` (app/artifacts.py, app/switch_handoff.py): the artifact and the 换配置/回退
  controls of a finished task card.
- ``forks`` (app/forks.py): the topics the butler's topic_fork opens, and their first run.
- ``compaction`` (app/compaction.py): the butler's compaction requests, taken by its next turn.
- ``rewinds`` (app/rewind.py): a ↩️ 回退 the topic's next turn resumes for, and each turn's prompt.
- ``turn_export`` (app/turn_export.py): 下载请求与结果 on an ended task card.
- ``questions`` (app/questions.py): native and checkpoint questions, answers, held notes.
- ``steering`` (app/steering.py): supplements (补充) and the end of a task's controls.
- ``controls`` (app/task_controls.py): stop, retry, redelivery and recovery cards.
- ``submission`` (app/submission.py): a topic message as a job, and its launch card.
- ``interaction`` (app/interaction.py): backend events and the clicks on live controls.
- ``runs`` (app/runs.py): one job's turn on the backend.

Its own methods are the entry points (a topic message, a card click, a queued job,
startup) and ``record_job_state``, which records a job's state and updates the
controls that state shows.
"""

from __future__ import annotations

import time
from typing import Any

from covey.app.artifacts import Artifacts
from covey.app.card_actions import ACTIONS, action_label, action_value
from covey.cards.question_cards import SKIP_ALL_KIND
from covey.app.card_lanes import reporting
from covey.cards.launch_config import SUBMIT_PREFIX
from covey.app.cards import Cards
from covey.app.compaction import Compaction
from covey.core.turn_words import interrupted_notice
from covey.app.forks import Forks
from covey.app.switch_handoff import SwitchHandoff
from covey.app.harnesses import Harness
from covey.app.interaction import Interaction
from covey.app.butler import compact_butler
from covey.app.questions import Questions
from covey.app.rewind import Rewinds
from covey.app.runs import Runs
from covey.app.runtime import Runtime
from covey.app.sessions import Sessions
from covey.app.steering import Steering
from covey.app.submission import HarnessChanged, Submission
from covey.app.topics import RetiredHarness
from covey.app.task_controls import TaskControls
from covey.app.turn_export import TurnExport
from covey.app.worker import process_bridge_reply_job
from covey.core.harness import BridgeContext, HarnessBackend, HarnessSpec, SessionStore
from covey.app import devices as device_turns, project_context, project_loop
from covey.core import device_reap, turn_words
from covey.core.run_dir import run_dir
from covey.core.jobs import BridgeReplyJob as ReplyJob
from covey.core.jobs import context_for_job, record_bridge_job_state, recover_stale_jobs
from covey.core import timing
from covey.core.metrics import mark
from covey.core.model_settings import ModelSettings
from covey.core.history_storage import append_history
from covey.core.topics import context_for_topic, has_topic_message, remember_topic_message
from covey.feishu.media import enrich_incoming_event
from covey.feishu.messenger import Messenger, Voice
from covey.app.launch import submit_launch_form



# A message routed to one Harness while the topic was switched to another, twice (D6, 2026-10-28).
HARNESS_CHANGED_REPLY = "这条消息没有提交：本话题刚换了配置，请再发一次。"
ANSWER_KINDS = frozenset({"bridge_question", SKIP_ALL_KIND})  # Clicks that save an answer before sending it.

class BridgeApp:
    """One Harness's components (see the module docstring) and the entry points that use them.
    ``topics`` is the Bridge's Topics, for the launch card; None for an app built alone."""

    def __init__(self, harness: Harness, *, runtime: Runtime, messenger: Messenger, topics=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.messenger = messenger
        self.topics = topics
        self.models = ModelSettings(harness)
        self.sessions = Sessions(harness, runtime=runtime)

        # Late-bound: recording a job's state and submitting a job reach components built after these.
        def record_job_state(*args, **fields):
            return self.record_job_state(*args, **fields)

        def enqueue_chat_job(ctx, content, chat_id, message_id, **fields):
            return self.submission.enqueue_chat_job(ctx, content, chat_id, message_id, **fields)

        self.cards = Cards(harness, runtime=runtime, messenger=messenger, record_job_state=record_job_state,
                           topics=topics)
        self.compaction = Compaction(harness, runtime=runtime)
        self.rewinds = Rewinds(harness, runtime=runtime)
        self.forks = Forks(harness, runtime=runtime, messenger=messenger, cards=self.cards, sessions=self.sessions)
        self.questions = Questions(harness, runtime=runtime, messenger=messenger, cards=self.cards,
                                   enqueue_chat_job=enqueue_chat_job)
        self.artifacts = Artifacts(harness, runtime=runtime, messenger=messenger, cards=self.cards,
                                   copies=self.questions)
        self.turn_export = TurnExport(harness, runtime=runtime, cards=self.cards, artifacts=self.artifacts)
        self.switch = SwitchHandoff(harness, runtime=runtime, cards=self.cards, topics=topics)
        self.steering = Steering(harness, runtime=runtime, messenger=messenger, cards=self.cards,
                                 record_job_state=record_job_state, enqueue_chat_job=enqueue_chat_job,
                                 event_context=lambda ctx, event: self.submission.event_context(ctx, event))
        self.controls = TaskControls(harness, runtime=runtime, messenger=messenger, cards=self.cards,
                                     questions=self.questions, artifacts=self.artifacts, models=self.models,
                                     steering=self.steering, record_job_state=record_job_state)
        self.submission = Submission(harness, runtime=runtime, messenger=messenger, cards=self.cards, models=self.models,
                                     controls=self.controls, record_job_state=record_job_state, topics=topics)
        self.interaction = Interaction(harness, runtime=runtime, messenger=messenger, cards=self.cards,
                                       questions=self.questions, steering=self.steering)
        self.runs = Runs(harness, runtime=runtime, interaction=self.interaction, forks=self.forks, switch=self.switch,
                         cards=self.cards, questions=self.questions, compaction=self.compaction, rewinds=self.rewinds,
                         record_job_state=record_job_state)

    @property
    def spec(self) -> HarnessSpec:
        return self.harness.spec

    @property
    def backend(self) -> HarnessBackend:
        return self.harness.backend

    @property
    def store(self) -> SessionStore:
        return self.harness.store

    @property
    def voice(self) -> Voice:
        return self.harness.voice

    def safe_idempotency_key(self, value: str) -> str:
        return self.voice.safe_key(value)

    def key(self, tag: str, message_id: str) -> str:
        return self.voice.key(tag, message_id)

    def reply(self, ctx: BridgeContext, chat_id: str, text: str, key: str) -> bool:
        """A text message in ctx's topic, in this Harness's voice."""
        return self.messenger.reply(ctx, chat_id, text, key, voice=self.voice)

    def record_job_state(
        self, ctx: BridgeContext, job: ReplyJob, status: str, status_message: str = "", **fields: Any
    ) -> None:
        if status == 'queued':
            mark(job, 'submitted')
            mark(job, 'queued')
        if job.progress:
            remember_topic_message(ctx.state_dir, job.topic_id, job.chat_id, job.progress.message_id)
            timing.mark("alias")
        record_bridge_job_state(
            state_dir=ctx.state_dir,
            backend=self.spec.name,
            job=job,
            status=status,
            status_message=status_message,
            workspace_name=ctx.workspace_name,
            workdir=ctx.workdir,
            history_path=ctx.history_path,
            session_path=ctx.session_path,
            sessions_dir=ctx.sessions_dir,
            **fields,
        )
        timing.mark("state_write")
        if status in {'done', 'waiting_user'} and fields.get('delivery_ok') is True:
            # The butler's context past its threshold: its next turn compacts first.
            compact_butler(self.compaction, ctx, job)
            try:
                self.artifacts.attach_artifact_controls(ctx, job)
            except Exception as exc:
                self.runtime.log(f"Artifact controls unavailable: {type(exc).__name__}")
            self.attach_export_controls(ctx, job)  # First: the 换配置/回退 panel then goes in above it.
            try:
                self.switch.attach_topic_controls(ctx, job)
            except Exception as exc:
                self.runtime.log(f"Topic controls unavailable: {type(exc).__name__}")
        if status in {'failed', 'cancelled', 'interrupted'}:
            self.attach_export_controls(ctx, job)
            try:
                self.switch.attach_switch_controls(ctx, job)
            except Exception as exc:
                self.runtime.log(f"Switch controls unavailable: {type(exc).__name__}")
        try:
            self.questions.update_continuation_card(ctx, job, status)
        except Exception as exc:
            self.runtime.log(f"Continuation card update failed: {type(exc).__name__}")

    def attach_export_controls(self, ctx, job):
        try:
            self.turn_export.attach_export_controls(ctx, job)
        except Exception as exc:
            self.runtime.log(f"Export controls unavailable: {type(exc).__name__}")

    def process_reply_job(self, job: ReplyJob, ctx: BridgeContext) -> None:
        try:
            process_bridge_reply_job(job, ctx, harness=self.harness, runtime=self.runtime, messenger=self.messenger,
                                     cards=self.cards, sessions=self.sessions, runs=self.runs, artifacts=self.artifacts,
                                     questions=self.questions, steering=self.steering,
                                     record_job_state=self.record_job_state)
        except Exception as exc:  # noqa: BLE001 - surface unexpected worker failures to the originating topic.
            target = context_for_job(ctx, job)
            message = f"任务 #{job.job_id} 发生异常（{type(exc).__name__}）；未满足结果发布条件。"
            self.record_job_state(target, job, "failed", message)
            self.messenger.reply(target, job.chat_id, message,
                                 self.key("unexpected",job.message_id), voice=self.voice)
        finally:
            # The process panel's updater too: only a successful final card write closes it, and
            # a task runner waits for every leftover updater thread before it exits.
            for updates in (job.card_updates, job.trace_card_updates):
                if updates is not None:
                    updates.close()
            self.runtime.unregister_reply_job(job)
            # Out of the registry, a supplement is refused, not held: those still held are settled now.
            self.steering.settle_held_at_end(context_for_job(ctx, job), job)

    def handle_event(self, event: dict[str, Any], ctx: BridgeContext, *, fetch_message_details: bool = True,
                     supplement=None, hold=0, routed=False) -> None:
        """A message of a topic of this Harness: access check, attachments and forwards, history, then a job.
        In a group each prompt starts with who sent it (app/access.py). ``fetch_message_details``
        False skips looking up quoted messages (tests). ``supplement(ctx, prompt, message_id, content=, sender=)``:
        True when it took the message into a running task instead (the butler's, app/butler.py); it
        writes the history line then. ``content`` and ``sender`` are what a new task of the message would be queued
        with, should the turn never take it (app/steering.py requeue_lost). ``supplement`` may instead answer the context of another topic: the message is queued there (多路并行). ``hold``: seconds the new task waits before it starts (the butler's, for the
        owner's words after a forward). ``routed``: ``ctx`` is the message's topic already, not looked up again from the
        event (the butler's, for a message whose reply chain starts in a frozen front-desk topic, app/butler.py)."""
        sender = str(event.get("sender_id") or "")
        chat_id = str(event.get("chat_id") or "")
        message_id = str(event.get("message_id") or event.get("id") or "")
        if not sender or not chat_id or not message_id or not str(event.get("content") or "").strip():
            return
        access = self.runtime.access
        if not access.allows(sender, chat_id):
            return
        group = event.get("chat_type") == "group"
        received_at = time.time()
        enriched = enrich_incoming_event(event, ctx.history_path.parent / "incoming_media",
                                         profile=ctx.profile, fetch_message_details=fetch_message_details,
                                         **({"owner_id": self.runtime.owner_open_id or "",
                                             "hidden_mentions": access.own_ids()} if group else {}))
        timing.mark("enrich")
        if not enriched.text:
            return
        ctx = ctx if routed else self.submission.event_context(ctx, enriched.event)
        timing.mark("context")
        content = enriched.display_text or enriched.text
        attachments = getattr(enriched, "attachments", "")
        self.runtime.log(f"received message id={message_id}")
        if self.workspace_command(ctx, content, chat_id, message_id, attachments):
            timing.outcome("answer")  # An answer, a held note or why neither (app/interaction.py).
            return
        timing.mark("command")
        project_loop.heard(ctx.state_dir, ctx.topic_id, enriched.text)  # The owner's word in a project's thread: a receipt.
        project_context.owner_files(ctx.state_dir, ctx.topic_id, chat_id, enriched.text, ctx.history_path.parent / "incoming_media",
                                    self.runtime.log)  # A file to a project's lead: into its library (COVEY_PROJECT_MEMORY=1).
        prompt = (access.sender_label(event) + "\n\n" + enriched.text) if group else enriched.text
        if supplement is not None:
            taken = supplement(ctx, prompt, message_id, content=content, sender=sender)
            if isinstance(taken, type(ctx)):
                ctx = taken  # A supplement the task refused: a track of its own, queued in its topic (app/butler_tracks.py).
            elif taken:
                timing.outcome("supplement")
                return
        append_history(ctx.history_path, "user", prompt, {**enriched.event, "display_content": content})
        timing.mark("history")
        submit = dict(prompt_content=prompt, received_at=received_at, sender_id=sender, **({"hold": hold} if hold else {}))
        try:
            self.submission.enqueue_chat_job(ctx, content, chat_id, message_id, **submit)
        except HarnessChanged:
            # 换配置 committed while this message was read: it goes to the topic's Harness now, once (its
            # history line is the topic's, already written).
            self.resubmit(enriched.event, content, chat_id, message_id, submit)

    def resubmit(self, event, content, chat_id, message_id, submit):
        app = None
        if self.topics is not None:
            try:
                app, target = self.topics.route(self.harness.context, event)
                app.submission.enqueue_chat_job(target, content, chat_id, message_id, **submit)
                self.runtime.log(f"message rerouted to harness={app.spec.name} message={message_id[-10:]}")
                return
            except (HarnessChanged, RetiredHarness):
                pass
        ctx = self.submission.event_context(self.harness.context, event)
        self.reply(ctx, chat_id, HARNESS_CHANGED_REPLY, self.key("harness-changed", message_id))

    def handle_card_action_event(self, event: dict[str, Any], ctx: BridgeContext, item: dict | None = None) -> None:
        """``item``: the card's message as Feishu returned it, when app/unified.py had to ask; every click comes
        through there, a task runner's inbox click too. Asked here only when it is not given and the topic
        aliases do not know the card, which after unified's own check is next to never."""
        if not self.runtime.access.allows(str(event.get("operator_id") or ""), str(event.get("chat_id") or "")):
            return
        from covey.feishu.message_read import apply_message_metadata, message_item, fetch_message_detail

        message_id = str(event.get("message_id") or "")
        chat_id = str(event.get("chat_id") or "")
        raw = action_value(event)
        label = action_label(raw.get("kind"))
        if raw.get("view") == "artifacts":
            label = "查看产物"
        elif raw.get("kind") == "bridge_question" and raw.get("mode") in {"feedback", "feedback_submit"}:
            label = "反馈问题"
        elif raw.get("view") in {"history","question"}:
            label = "查看历史问题"
        elif str(event.get("action_name") or "").startswith(SUBMIT_PREFIX):
            label = "开始任务"  # Its task cards follow the click: a failure there is reported as this.
        try:
            if message_id and has_topic_message(ctx.state_dir, chat_id, message_id):
                ctx = context_for_topic(ctx, event, self.store.sessions_dir)
            elif message_id:
                if item is None:
                    detail = fetch_message_detail(message_id, ctx.profile)
                    item = message_item(detail, message_id) if detail else None
                if item and (item.get("root_id") or item.get("thread_id")):
                    metadata = dict(event)
                    apply_message_metadata(metadata, item)
                    ctx = context_for_topic(ctx, metadata, self.store.sessions_dir)
            ctx = self.models.configured_context(ctx)
            # What the handler leaves for after the click is acknowledged reports here as well.
            with reporting(lambda exc, ctx=ctx: self.card_action_failed(ctx, event, raw, label, exc)):
                handler = self.card_handler(raw.get("kind"))
                # A launch form's 开始任务 has no kind: its button's name carries the form.
                if not (handler and handler(ctx, event)) and not submit_launch_form(self, ctx, event):
                    self.unclaimed_card_action(event, ctx)
        except Exception as exc:  # noqa: BLE001 - report callback failures without stopping the consumer
            self.card_action_failed(ctx, event, raw, label, exc)

    def card_handler(self, kind):
        """The component method ACTIONS names for a click's ``kind`` (app/card_actions.py), or None."""
        action = ACTIONS.get(kind)
        if action is None or not action.handler:
            return None
        component, method = action.handler.split(".")
        return getattr(getattr(self, component), method)

    def card_action_failed(self, ctx, event, raw, label, exc):
        chat_id, source = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        if isinstance(exc, ValueError):
            self.messenger.reply(ctx, chat_id, f"{label}未完成：{exc}", self.key("action-invalid", source), voice=self.voice)
            return
        self.runtime.log(f"card action failed: {exc}")
        self.messenger.reply(ctx, chat_id, f"{label}暂未完成，请稍后重试。" + ("回答已保存时请先查看提交状态，避免重复发送。" if raw.get("kind") in ANSWER_KINDS else "已完成的任务结果仍保留。"),
                             self.key("action-failure", source), voice=self.voice)

    def notify_stale_jobs(self, ctx: BridgeContext) -> None:
        keep = self.runtime.adopted_keys
        for stale in recover_stale_jobs(ctx.state_dir, self.spec.name, keep):
            # A turn on a device the Bridge's restart left: its Agent may still run there. Registered only; the next turn of the
            # topic (or the devices thread) reaps it first (core/device_reap.py).
            if stale.get("device"):
                device_turns.register_left(self.runtime.log, ctx.state_dir, stale, run_dir(ctx.state_dir, str(stale.get("key"))),
                                           device_reap.RESTART)
            chat_id = str(stale.get("chat_id") or "")
            if not chat_id or not stale.get("topic_id") or stale.get("kind") != "chat":
                continue
            # The restart that left it unfinished is not the user's doing: 检查并继续 once by itself, a minute
            # from now, unless they stop it on this card first (owner's choice, T314 2026-11-02).
            self.controls.report_unfinished(ctx, stale,
                                            turn_words.device_interrupted(stale) or interrupted_notice(stale.get('job_id')),
                                            self.key("stale", str(stale.get("key"))), keep_question_forms=True)

    def unclaimed_card_action(self, event: dict[str, Any], ctx: BridgeContext) -> None:
        """A click no topic handler took: a retired card."""
        if not self.runtime.access.allows(str(event.get("operator_id") or ""), str(event.get("chat_id") or "")):
            return
        chat_id = str(event.get("chat_id") or "")
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        if chat_id and getattr(ctx, "topic_id", ""):
            self.reply(ctx, chat_id, "旧卡片功能已移除，请在话题里直接描述需求。", self.key("retired-card", event_id))

    def workspace_command(self, ctx, content, chat_id, message_id, attachments=""):
        return self.interaction.interactive_command(ctx, content, chat_id, message_id, attachments)

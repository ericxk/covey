"""Running a dequeued job (process_reply_job: its Harness's checks, then a task runner or this process) and the
start-up pass over jobs a restart left (notify_stale_jobs: adopted runners, waiting launches, retired Harnesses'
jobs, the butler's lost supplements). JobDispatch is a part of app/unified.py's UnifiedBridgeApp, whose apps
and runtime it reads."""
from __future__ import annotations

from covey.app import detached, knowledge_collect, memory_collect, butler_relay, owner_send
from covey.app.availability import check_or_raise
from covey.app.launch_submit import recover_worktree_launches
from covey.app.butler import HARNESS as BUTLER_HARNESS
from covey.app.topics import retired_notice, topic_target
from covey.core import timing
from covey.core.jobs import (context_for_job, find_job_record, harness_of, running_job_records,
                                      upsert_job_record)
from covey.core.statuses import RUNNING
from covey.core.state import utc_now_iso
from covey.core.store import directory_store
from covey.core.topics import load_topic, read_topic

BACKEND_UNAVAILABLE = '后端暂不可用，任务尚未执行。'


class JobDispatch:
    """UnifiedBridgeApp's job hand-off and its start-up recovery of jobs."""

    def process_reply_job(self, job, ctx):
        # One timing line per hand-off (core/timing.py): from the scheduler taking the job to its runner's
        # spawn (detached.run_detached ends it). Inside a task runner its phases join the runner's start.
        flow = None if job.detached_run_dir else timing.begin('job_start', job=f'#{job.job_id}',
                                                               harness=harness_of(job, 'codex'))
        if flow is not None:
            queued = (getattr(job, 'timings', None) or {}).get('queued')
            flow.note(queued_ms=timing.delay_ms(round(queued * 1000), flow.wall) if isinstance(queued, (int, float)) else None)
        try:
            return self._process_reply_job(job, ctx)
        finally:
            timing.end(flow)

    def _process_reply_job(self, job, ctx):
        name = harness_of(job, 'codex')
        app = self.apps.get(name)
        if app is None:
            # A queued job may have been saved before this bot's enabled Harnesses changed.
            self.runtime.unregister_reply_job(job)
            self.close_retired_jobs(ctx)
            return
        if job.detached_run_dir:
            # Adopted after a restart: its runner already passed every check below.
            return detached.run_detached(self, app, job, app.harness.context)
        if job.cancelled_before_start:
            # Stopped while queued: its record and card already say so. No availability notice
            # or recovery card for a job the user cancelled.
            app.runtime.unregister_reply_job(job)
            return
        record_ctx = context_for_job(app.harness.context, job)
        data = read_topic(record_ctx) if job.topic_id else {}
        if job.topic_id and (data.get('harness_id') != name or (job.binding_revision and data.get('binding_revision') != job.binding_revision)):
            app.record_job_state(record_ctx, job, 'failed', '话题绑定与入队快照不一致，未执行任务')
            app.runtime.unregister_reply_job(job)
            return
        try:
            # Pending launch jobs enter the queue directly. Initialize their
            # selected backend here, outside the fast card callback. A task runner's
            # Bridge checked it just before the handoff: Claude's check is
            # `claude auth status`, 1.0-1.5 s (2026-10-26), and is not run twice.
            if not self.runtime.in_runner:
                check_or_raise(name, record_ctx)
            timing.mark('check')
            app.harness.ensure_started(record_ctx)
            timing.mark('ready')
        except (OSError, ValueError, RuntimeError) as exc:
            app.record_job_state(record_ctx, job, 'failed', '后端初始化失败：' + type(exc).__name__)
            app.runtime.unregister_reply_job(job)
            app.messenger.reply(record_ctx, job.chat_id, BACKEND_UNAVAILABLE, app.key('startup-failed', job.message_id), voice=app.voice)
            app.controls.show_recovery_card(record_ctx, find_job_record(ctx.state_dir, job.message_id))
            return
        if self.runtime.detached():
            # The task runs to the end in its own process; restarts and deploys leave it running.
            return detached.run_detached(self, app, job, app.harness.context)
        app.process_reply_job(job, app.harness.context)
        knowledge_collect.collect(self.runtime, job, ctx)
        memory_collect.collect(self.runtime, getattr(app, "messenger", None), job, ctx)
        owner_send.collect(self, job, ctx)
        butler_relay.after_runner(self, job, ctx)
        if not job.progress:
            # A text fallback must still offer topic-scoped recovery now that
            # slash commands are retired. Never infer or rerun a failed send.
            record = find_job_record(ctx.state_dir, job.message_id)
            if record and record.get('status') in {'failed', 'interrupted'}:
                try:
                    app.controls.show_recovery_card(record_ctx, record)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.runtime.log(f'recovery card unavailable: {type(exc).__name__}')

    def notify_stale_jobs(self, ctx):
        recover_worktree_launches(self.topics, ctx)
        detached.adopt_runners(self, ctx)  # Live task runners keep their jobs; only the rest are interrupted.
        for app in self.apps.values():
            app.notify_stale_jobs(app.harness.context)
        self.close_retired_jobs(ctx)
        self.release_started_launches(ctx)
        # The butler's messages a turn that ended while no Bridge watched never took: turns of their own now.
        detached.queue_stranded(self, ctx, self.apps.get(BUTLER_HARNESS))

    def release_started_launches(self, ctx):
        """A restart between 开始任务's acknowledgement and its follow-up left a bound topic's
        messages waiting: queue them, as the follow-up would have (the receipt already said started)."""
        topics = {(r.get("topic_id"), r.get("chat_id")) for _, r in
                  directory_store(ctx.state_dir / "jobs").query(status="waiting_workspace")}
        for topic_id, chat_id in sorted(topics, key=str):
            try:
                topic = load_topic(ctx.state_dir, topic_id)
                app = self.apps.get(topic.get("harness_id"))
                if app is None or not topic.get("workspace_locked"):
                    continue  # Still waiting for its launch card, or its Harness is retired.
                target = topic_target(app, chat_id, topic)
                if released := app.submission.release_waiting(target, chat_id):
                    self.runtime.log(f"released {released} waiting messages of started topic={topic_id[:12]}")
            except Exception as exc:  # noqa: BLE001 - one topic never blocks startup
                self.runtime.log(f"waiting messages not released topic={str(topic_id)[:12]}: {type(exc).__name__}")

    def close_retired_jobs(self, ctx):
        """Unfinished jobs of a Harness no longer in the roster can never run: end each one once."""
        live = detached.live_keys(ctx.state_dir)  # An older release's task runner still finishes its own job.
        for record in running_job_records(ctx.state_dir):
            name = record.get('backend')
            if (not name or name in self.apps or not record.get('key') or record.get('key') in live
                    or record.get('status') not in RUNNING):
                continue
            notice = retired_notice(name)
            upsert_job_record(ctx.state_dir, record['key'], status='interrupted', interrupted_at=utc_now_iso(),
                              status_message=notice)
            self.runtime.log(f"job #{record.get('job_id')} of retired harness={name} marked interrupted")
            if record.get('chat_id') and record.get('topic_id') and record.get('kind') == 'chat':
                self.messenger.send_message(ctx.profile, record['chat_id'], f"任务 #{record.get('job_id')} 已中断。{notice}",
                                            self.voice.key('retired-stale', str(record['key'])),
                                            reply_to_message_id=str(record.get('reply_to_message_id') or ''), voice=self.voice)

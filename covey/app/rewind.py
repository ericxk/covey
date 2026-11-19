"""↩️ 回退: drop a topic's latest turn, on a Harness that can (Claude Code; the owner, 2026-11-03).

The button sits in the panel of every finished task card of such a topic (app/switch_handoff.py); a click
opens a card (cards/rewind.py) that names the turn and the files its edit tools changed, with
对话和文件都回退, 只回退对话 (or 回退这一轮 when no file changed) and 取消. Only the topic's latest turn that
reached the native session can go, one per next message, while nothing in the topic runs, and only
within the binding it ran in (a switch began a session of its own).

Confirming, under the topic lock: the files go back first when chosen (the backend's rewind_files;
a refusal changes nothing), then the turn's rows leave the topic history (kept on the card's record),
its waiting questions are cancelled, and the session forgets it: the topic's REWIND file (app/binding.py) names the
entry the next turn resumes at (the backend's ``resume_at``), and the turn's ``prompt_read`` takes
the file away once Claude Code has its prompt, so a turn that failed before then leaves it for the
next. A turn that began its session clears the topic's pointer instead: the next turn starts fresh
on the history left. 撤销回退, while the conversation alone went and before the next turn takes it,
puts the file or the pointer and the rows back. A turn's ``prompt_uuid`` and ``prompt_session`` are
in its job record from its prompt_read on; turns run before that cannot go back.
"""
from __future__ import annotations

import dataclasses
import time

from covey.app.card_queue import CardActionQueue
from covey.app.card_actions import action_value
from covey.app.binding import busy_reason, marker_path, read_marker, retire_open_questions
from covey.app.form_cards import click_token, publish_form, refused, replace_form_card
from covey.cards.elements import notice_card
from covey.cards.rewind import CANCEL, CONFIRM, KINDS, OPEN, UNDO, done_card, rewind_card
from covey.core.harness import backend_capabilities
from covey.core.jobs import upsert_job_record
from covey.app.menu_panel import valid_token
from covey.core.locks import resource_lock, topic_lock
from covey.core.state import read_json, write_json
from covey.core.session_state import compare_and_set_session, capture_session_state
from covey.core.history_storage import drop_last_round, restore_rounds
from covey.core.store import directory_store
from covey.core.topics import proxy_topic, read_topic, topic_label

UNSUPPORTED = "本话题的 Harness 不能回退（目前只有 Claude Code 可以）"
STALE = "话题换过配置，这一轮在换之前的会话里，不能回退"
PENDING = "已经回退了一轮；下一条消息发出前只能回退这一步"


class Rewinds:
    """One Harness's side of ↩️ 回退 in a turn: the entry the turn resumes at, and its prompt.

    - ``harness``: whether its backend can rewind.
    - ``runtime``: the log.
    """

    def __init__(self, harness, *, runtime) -> None:
        self.harness = harness
        self.runtime = runtime

    def supported(self, ctx):
        return backend_capabilities(self.harness.backend, ctx).rewind

    def turn_context(self, ctx, job, session_id):
        """The run's context: resuming at the kept entry while a rewind of this session waits. One for
        another session (the pointer changed since) is void and goes."""
        if not ctx.topic_id or not self.supported(ctx):
            return ctx
        marker = read_marker(ctx)
        if marker is None:
            return ctx
        if not session_id or marker.get("session_id") != session_id:
            with resource_lock("record", marker_path(ctx).absolute()):
                if (read_marker(ctx) or {}).get("token") == marker["token"]:
                    marker_path(ctx).unlink(missing_ok=True)
            self.runtime.log(f"rewind dropped topic={ctx.topic_id[:12]}: the session changed")
            return ctx
        self.runtime.log(f"rewind taken by job=#{job.job_id} topic={ctx.topic_id[:12]}")
        return dataclasses.replace(ctx, options={**ctx.options, "resume_at": marker["keep"],
                                                 "rewind_token": marker["token"]})

    def prompt_read(self, ctx, job, data):
        """The turn's prompt is in its session: the job records it (the turn a rewind would drop), and a
        rewind this turn resumed for is done."""
        uuid, session = str(data.get("uuid") or ""), str(data.get("session_id") or "")
        if uuid and session:
            upsert_job_record(ctx.state_dir, job.message_id, prompt_uuid=uuid, prompt_session=session)
        token = ctx.options.get("rewind_token")
        if token and ctx.topic_id:
            with resource_lock("record", marker_path(ctx).absolute()):
                if (read_marker(ctx) or {}).get("token") == token:
                    marker_path(ctx).unlink(missing_ok=True)
            self.runtime.log(f"rewind done by job=#{job.job_id} topic={ctx.topic_id[:12]}")


def latest_turn(state_dir, topic_id, chat, session_id):
    """The topic's latest job whose prompt reached ``session_id``, or None."""
    rows = [r for _, r in directory_store(state_dir / "jobs").query(topic_id=topic_id, chat_id=chat)
            if r.get("prompt_uuid") and r.get("prompt_session") == session_id]
    return max(rows, key=lambda r: int(r.get("job_id") or 0), default=None)


def check_rewind(topics, app, ctx, chat, job_id):
    """(the job's record, the session) when the turn of ``job_id`` can go back now; ValueError saying why not."""
    if not backend_capabilities(app.backend, ctx).rewind:
        raise ValueError(UNSUPPORTED)
    if proxy_topic(ctx):
        raise ValueError("代答话题的回复已经发给对方，不能回退")
    topic = read_topic(ctx)
    record = next((r for _, r in directory_store(ctx.state_dir / "jobs").query(
        topic_id=ctx.topic_id, chat_id=chat, job_id=job_id)), None)
    if record is None:
        raise ValueError("当前话题找不到这一轮任务")
    if (record.get("harness_id") or record.get("backend")) != topic.get("harness_id") or int(
            record.get("binding_revision") or 1) != int(topic.get("binding_revision") or 1):
        raise ValueError(STALE)
    reason = busy_reason(topics, ctx, chat, "回退")
    if reason:
        raise ValueError(reason)
    if record.get("rewound"):
        raise ValueError("这一轮已经回退过了")
    if read_marker(ctx) is not None:
        raise ValueError(PENDING)
    if not record.get("prompt_uuid"):
        raise ValueError("这一轮没有进入 Claude Code 的会话（或是在回退功能上线前运行的），不能回退")
    session = app.sessions.read_session_id(ctx.session_path) or ""
    if record.get("prompt_session") != session:
        raise ValueError("本话题的会话已经换过（重置、换配置或回退过），这一轮不能再回退")
    latest = latest_turn(ctx.state_dir, ctx.topic_id, chat, session)
    if latest is not None and int(latest.get("job_id") or 0) != job_id:
        raise ValueError(f"只能回退本话题最新的一轮（任务 #{latest['job_id']}）")
    return record, session


def rewind_turn(topics, app, ctx, chat, form, files):
    """Drop the turn of ``form`` (and its files when ``files``); the outcome for the done card."""
    with topic_lock(ctx):
        record, session = check_rewind(topics, app, ctx, chat, int(form["job_id"]))
        point = app.backend.rewind_point(ctx, session, record["prompt_uuid"])
        restored = 0
        if files:
            if not point.get("checkpoint"):
                raise ValueError("这一轮没有文件快照，只能回退对话")
            app.backend.rewind_files(ctx, session, record["prompt_uuid"])
            restored = int(point.get("file_count") or 0)
        dropped = drop_last_round(ctx.history_path)
        done = {"at": time.time(), "files": restored, "rows": dropped, "keep": point["keep"], "session_id": session}
        if point["keep"]:
            with resource_lock("record", marker_path(ctx).absolute()):
                write_json(marker_path(ctx), {"session_id": session, "keep": point["keep"], "job_id": form["job_id"],
                                              "token": form["token"], "at": done["at"]}, ensure_ascii=False)
        else:
            # The turn began the session: nothing of it is left to resume.
            with app.runtime.session_lock:
                revision, current = capture_session_state(ctx.session_path, app.spec.session_pattern)
                compare_and_set_session(ctx.session_path, app.spec.session_pattern, revision, current, None)
        upsert_job_record(ctx.state_dir, record["key"], rewound={"at": done["at"], "files": restored,
                                                                 "token": form["token"]})
    retire_open_questions(topics, app, ctx, chat, reason="rewound", message="这一轮已回退")
    topics.runtime.log(f"rewound topic={ctx.topic_id[:12]} job=#{form['job_id']} files={restored} "
                       f"rows={len(dropped)} fresh={not point['keep']}")
    return done


def undo_rewind(topics, app, ctx, chat, form):
    """Put the conversation back as it was before ``form``'s rewind, while the next turn has not taken it."""
    done = form["done"]
    with topic_lock(ctx):
        reason = busy_reason(topics, ctx, chat, "撤销")
        if reason:
            raise ValueError(reason)
        if done["keep"]:
            with resource_lock("record", marker_path(ctx).absolute()):
                if (read_marker(ctx) or {}).get("token") != form["token"]:
                    raise ValueError("下一轮已经开始，回退不能撤销了")
                marker_path(ctx).unlink(missing_ok=True)
        else:
            with app.runtime.session_lock:
                revision, current = capture_session_state(ctx.session_path, app.spec.session_pattern)
                if current or not compare_and_set_session(ctx.session_path, app.spec.session_pattern, revision, None,
                                                          done["session_id"]):
                    raise ValueError("下一轮已经开始，回退不能撤销了")
        restore_rounds(ctx.history_path, done.get("rows") or [])
        record = next((r for _, r in directory_store(ctx.state_dir / "jobs").query(
            topic_id=ctx.topic_id, chat_id=chat, job_id=int(form["job_id"]))), None)
        if record is not None:
            upsert_job_record(ctx.state_dir, record["key"], rewound=None)
    topics.runtime.log(f"rewind undone topic={ctx.topic_id[:12]} job=#{form['job_id']}")


class RewindCards(CardActionQueue):
    """The ↩️ 回退 button and its card; their clicks run on a worker."""

    def __init__(self, topics):
        super().__init__(topics, name="rewind", workers=1, label="rewind")

    def matches(self, event):
        return action_value(event).get("kind") in KINDS

    def claim(self, ctx, event, raw, owner):
        """The click's outcome when it is 回退's, else None (app/unified.py EARLY)."""
        if not self.matches(event):
            return None
        self.dispatch(ctx, event)
        return "rewind"

    @staticmethod
    def path(ctx, token):
        return ctx.state_dir / "rewind-forms" / (token + ".json")

    def topic_context(self, ctx, topic_id, chat):
        """The topic's app, context and topic.json; Nones when it cannot be routed."""
        return self.topics.target(ctx.state_dir, topic_id, chat) or (None, None, None)

    def dispatch(self, ctx, event):
        raw = action_value(event)
        if raw.get("kind") == OPEN:
            self.submit("rewind card not opened", self.open, ctx, event, raw)
            return
        token = raw.get("token")
        if valid_token(token):
            self.submit("rewind click dropped", self.click, ctx, event, raw, self.path(ctx, token))

    def open(self, ctx, event, raw):
        chat, click = str(event.get("chat_id") or ""), str(event.get("event_id") or event.get("message_id") or "")
        app, target, _ = self.topic_context(ctx, raw.get("topic_id"), chat)
        if app is None:
            return
        token = click_token(target.topic_id, "rewind", click)
        path = self.path(ctx, token)
        with resource_lock("rewind-form", path):
            if path.exists():
                return  # A redelivered click.
            try:
                job_id = int(raw.get("job_id") or 0)
                record, session = check_rewind(self.topics, app, target, chat, job_id)
                point = app.backend.rewind_point(target, session, record["prompt_uuid"])
            except (ValueError, OSError) as exc:
                text = str(exc) if isinstance(exc, ValueError) else f"读不到会话记录（{type(exc).__name__}）"
                app.messenger.reply(target, chat, "不能回退：" + text, app.key("rewind-refused", click), voice=app.voice)
                return
            form = {"token": token, "topic_id": target.topic_id, "chat_id": chat, "job_id": job_id,
                    "status": "publishing", "user_text": record.get("user_text") or "", "point": point,
                    "workdir": str(target.workdir), "topic": topic_label(ctx.state_dir, target.topic_id)}
            publish_form(path, form, lambda: app.messenger.create_and_send_card(
                ctx.profile, chat, rewind_card(form), app.key("rewind-card", token),
                reply_to_message_id=target.reply_to_message_id, voice=app.voice), topic=(ctx.state_dir, target.topic_id, chat))

    def click(self, ctx, event, raw, path):
        chat = str(event.get("chat_id") or "")
        with resource_lock("rewind-form", path):
            try:
                form = read_json(path)
            except (OSError, ValueError):
                return
            if form.get("chat_id") != chat or form.get("message_id") != event.get("message_id"):
                return
            app, target, _ = self.topic_context(ctx, form["topic_id"], chat)
            if app is None:
                return
            kind = raw["kind"]
            if kind == CANCEL and form.get("status") == "ready":
                self.replace(ctx, path, form, {**form, "status": "cancelled"},
                             notice_card("已取消回退，话题没有变化。", "已取消回退"), "rewind-cancel")
            elif kind == CONFIRM and form.get("status") == "ready":
                try:
                    done = rewind_turn(self.topics, app, target, chat, form, raw.get("files") is True)
                except (ValueError, OSError) as exc:
                    self.refuse(ctx, path, form, str(exc) if isinstance(exc, ValueError)
                                else f"内部错误（{type(exc).__name__}）", rewind_card)
                    return
                finished = {**form, "status": "done", "done": done, "notice": ""}
                if not self.replace(ctx, path, form, finished, done_card(finished), "rewind-done"):
                    # With the sequence that write reserved (form holds it), never the one before it (rule 3).
                    write_json(path, {**finished, "sequence": form["sequence"]}, ensure_ascii=False)
            elif kind == UNDO and form.get("status") == "done" and not form["done"].get("files"):
                try:
                    undo_rewind(self.topics, app, target, chat, form)
                except (ValueError, OSError) as exc:
                    self.refuse(ctx, path, form, str(exc) if isinstance(exc, ValueError)
                                else f"内部错误（{type(exc).__name__}）", done_card)
                    return
                undone = {**form, "status": "undone"}
                if not self.replace(ctx, path, form, undone, notice_card(
                        "已撤销回退，话题恢复到回退之前。", "已撤销回退"), "rewind-undo"):
                    write_json(path, {**undone, "sequence": form["sequence"]}, ensure_ascii=False)

    def replace(self, ctx, path, form, candidate, card, kind):
        return replace_form_card(ctx, path, form, candidate, card, self.topics.messenger, kind)

    def refuse(self, ctx, path, form, reason, draw):
        """A refusal is said on the card, which keeps its buttons."""
        candidate = {**form, "notice": refused("没有完成", reason)}
        self.replace(ctx, path, form, candidate, draw(candidate), "rewind-refused")

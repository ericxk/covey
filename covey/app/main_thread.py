"""The main thread's session (docs/main-thread.md), only in the private chat mode Personal Agent (core/main_thread.py
enabled: COVEY_MAIN_THREAD not 0, the mode on the settings card): what the owner writes at the top level of the private chat goes to one long-lived Claude Code session, whose topic
(main_topic_id) has no root message and no T number; its topic.json says ``main_thread: true``.

- ``MainThread.handle_event``, called after the extensions' in app/card_routing.py: claims the message, keeps the topic's binding
  to the settings and hands it to the usual submission (app/app.py handle_event).
- Replies stay at the top level, quoting the owner's message: every message of the topic has an alias (core/topics.py)
  and ``configure`` tells feishu/message_request.py that a reply to such a message is not in a thread. Task runners
  build their context the same way, so their cards do it too; nothing of it crosses runs/<job key>/.
- While a turn runs, a message becomes its supplement with no receipt card (``supplement``); one the task cannot take
  queues behind it, one it certainly never took (refused after it was held, or ended unread) is queued as a turn of
  its own once the turn ends (app/steering.py requeue_lost), and one it may have lost is said in one line.
- A message with no words of its own (a forward, a card, a file, a bare link: ``bare_message``) while no turn runs
  or waits is held: its turn is queued but starts only after HOLD_SECONDS (core/scheduler.py), so the owner's words
  that Feishu sends a second later join its request (app/steering.py merge_before_start) and start it at once;
  another such message joins it and holds it again. Never saved: a restart starts it.
- ``main_turn``, in the task runner: this turn's rules and snapshot at the head of the message, and the Bridge's tools
  for this run (``options["mcp_config"]``, which backends/claude/argv.py turns into --mcp-config).
- ``compact_main_thread``: past ``compact_tokens`` at the end of a turn, a compaction request: the Bridge compacts
  while the topic is idle (app/idle_compaction.py), else the next turn first (app/compaction.py).

The snapshot lists 等你回答, 有结果未看, 进行中 and the topics the main thread opened (main-thread/opened/, app/main_topics.py)
from the indexes, with no Feishu call. Each turn saves it as topics/<id>/main-snapshot.json and says only what changed
since: everything on a new session, after a compaction (before the turn or idle, compacted.json), and when the turn that took the last one did not end well.
"""
from __future__ import annotations

import dataclasses
import functools
import re
import sys
import time
from pathlib import Path

from covey.app import idle_compaction, main_relay, pins
from covey.app.compaction import left_for_idle
from covey.core.topic_seen import SEEN_FILE
from covey.app.prompts import main_thread_message
from covey.core import main_thread as contract
from covey.core import main_tracks
from covey.core import board_read, main_opened, main_thread_settings, placement, timing, turn_words
from covey.core.board_read import ended_at, topic_label as topic_words
from covey.core.attachments import message_words
from covey.core.checkpoint import checkpoint_steps
from covey.core.jobs import job_key, job_path, read_job_record
from covey.core.locks import LOCK
from covey.core.main_thread import is_main_topic, main_topic_id, mcp_config
from covey.core.redaction import TOPIC_KEY as REDACTION_KEY
from covey.core.state import read_object, write_json
from covey.core.session_state import capture_session_state, compare_and_set_session
from covey.core.status_line import step_text
from covey.core.steer import SteerRefused, SteerUncertain
from covey.core.statuses import RUNNING
from covey.core.topics import (message_topic, move_topic_message, remember_topic_message, retired_topic,
                                        routed_topic, topic_file)
from covey.core.workspaces import load_config
from covey.feishu.message_parts import collect_message_parts_named, json_object, strip_paragraph_markup
from covey.feishu.message_request import configure_top_level

HARNESS = "claude"  # core/roster.py.
RELEASE = Path(__file__).resolve().parents[2]
SNAPSHOT = "main-snapshot.json"
RESULTS = turn_words.RESULTS  # How the snapshot says a result ended (core/board_read.py BOARD_RESULTS).
ROWS = 15  # Per group; topics_list has the rest.
LINE = 100
RELAYED = "（结论已发到聊天区）"
OPENED = 30
# What the main thread's /compact asks of the summary (backends/claude/compaction.py command): its summary is the
# compaction's time (2026-11-08 #3275: 7,851 token in 85 s), and the snapshot, topics_search and topic_read bring
# back the rest. A topic's compaction has no instructions.
COMPACT_INSTRUCTIONS = (
    "把这段对话压缩成不超过约 2000 token 的摘要。只保留：主人交代、还没做完的事；开过的话题，每个写 T 号、标题和一句话现状；"
    "还没定的问题，和在等谁回答；主人说过的偏好和定下的做法。丢掉过程细节：工具调用和输出、读过的文件内容、已经做完的步骤、"
    "快照里已有的话题列表。之后需要细节时用 topics_search、topic_read 查。")
GOOD_ENDS = frozenset({"done", "waiting_user", "continued"})  # The turn saw the snapshot it took.
GROUPS = (("waiting", turn_words.GROUP_NAMES["waiting"]), ("result", turn_words.GROUP_NAMES["result"]),
          ("running", turn_words.GROUP_NAMES["running"]), ("opened", "主线程开的话题"))
NO_RECEIPTS = "最近有结果（没有已读记录，不知道主人看没看）"
SUPPLEMENT_UNCERTAIN = "这条补充不确定送到了没有，没有重发。没看到回应的话再发一次。"
SUPPLEMENT_LOST = "这条补充这一轮没有读到，也没有重发。还需要的话再发一次。"
# Messages with no words of the owner's own: what Feishu forwards, shares or uploads. Text and post count by their text.
WORDLESS_TYPES = frozenset({"interactive", "merge_forward", "image", "file", "media", "video", "audio", "sticker",
                            "share_chat", "share_user", "share_calendar_event", "location", "folder"})
URL = re.compile(r"https?://\S+")


def bare_message(event) -> bool:
    """Whether the owner's message has no words of its own: a forward, a card, an image, a file or other media, or
    a text (or post) that is only links. An unknown type has words, as before."""
    kind = str(event.get("message_type") or event.get("msg_type") or "").strip().lower()
    if kind in WORDLESS_TYPES:
        return True
    if kind not in {"text", "post"}:
        return False
    raw = str(event.get("content") or "").strip()
    parsed = json_object(raw)
    if kind == "text":  # Its text field (some arrive as <p>…</p>), or the content itself when it is no JSON object.
        text = parsed.get("text") if isinstance(parsed, dict) else parsed if isinstance(parsed, str) else ""
        text = strip_paragraph_markup(str(text or ""))
    else:
        text = "\n".join(collect_message_parts_named(raw)[0])
    return not URL.sub("", text).strip() and (bool(URL.search(text)) or kind == "post")


class MainThread:
    """- ``topics``: the Harness apps, the runtime (who is the owner, the job registry)."""

    def __init__(self, topics) -> None:
        self.topics = topics
        self.runtime = topics.runtime

    @staticmethod
    def configure(ctx) -> None:
        """Where replies go, in this process. A reply to one of the main thread's messages goes in that message's
        thread (since 2026-11-08: the owner's message gets the main thread's card in its thread, no card of its own
        beside it). With the main thread off (the switch 0 or the mode 纯话题, which the owner changes while the
        process runs), every reply is in its thread."""
        state = ctx.state_dir

        def top_level(chat, message):
            if not contract.enabled():
                return False
            return placement.plan(state, contract.traits(message_topic(state, chat, message), chat)).top_level()

        configure_top_level(top_level if contract.available() else None)
        contract.configure_state(state)  # The mode (core/main_thread.py enabled).

    @staticmethod
    def settle(ctx, log) -> None:
        """At the Bridge's start, before any event: the private chat mode written once (core/main_thread_settings.py
        settle_mode), Personal Agent on a new deployment. A failure is a log line; the next start tries again. An idle
        compaction the last Bridge started stops (app/idle_compaction.py clear_left), whatever the switch says."""
        try:
            idle_compaction.clear_left(ctx.state_dir, log)
        except (OSError, ValueError) as exc:
            log(f"idle compaction not cleared: {type(exc).__name__}")
        if not contract.available():
            return
        try:
            mode = main_thread_settings.settle_mode(ctx.state_dir)
        except (OSError, ValueError) as exc:
            log(f"main thread mode not settled: {type(exc).__name__}: {exc}"[:300])
            return
        if mode:
            log(f"main thread mode settled mode={mode}")

    def app_for(self, ctx, event, owner):
        """The app that runs this message as the main thread's, or None when it is not the main thread's: not a chat
        with a main thread (Access.agent_chat: the owner's private chats, the personal groups), in
        or under a topic, the main thread off (the switch 0 or the mode 纯话题), or Claude Code not available."""
        chat = str(event.get("chat_id") or "")
        # ``enabled`` reads the mode of the state ``configure`` set: this one (app/unified.py, one state a process).
        if not owner or not contract.enabled() \
                or not self.runtime.access.agent_chat(chat, event.get("chat_type") == "group"):
            return None
        # The quoted message first: a top-level reply chain starts at the owner's message (the main thread's).
        above = [str(event.get(name) or "") for name in ("parent_id", "root_id", "thread_id")]
        if routed_topic(ctx.state_dir, chat, above[0]):
            # Quoting a message routed to a topic (the card of a turn that handed on to it alone, a relay), at the top
            # level or in a thread (S8, 2026-11-08: the card is in the owner's message's thread): that topic's (引用直达).
            return None
        known = [topic for topic in (message_topic(ctx.state_dir, chat, message) for message in above if message) if topic]
        if known and not is_main_topic(known[0], chat) and retired_topic(ctx.state_dir, known[0]):
            # A frozen front-desk topic's card quoted (core/topics.py retired_topic, 2026-11-10): the message is the
            # main thread's, as a new one at the top level (its chain's root is the owner's message, the main
            # thread's; core/topics.py context_for_topic finds that first).
            known = [main_topic_id(chat)]
        # A reply to a message no topic knows opens a topic on it, as ever; to one of the main thread's stays here.
        if (not is_main_topic(known[0], chat)) if known else any(above):
            return None
        app = self.topics.apps.get(HARNESS)
        if app is None:
            return None
        try:
            app.harness.refuse_while_unavailable()
        except RuntimeError:
            return None
        return app

    def handle_event(self, ctx, event, owner) -> bool:
        """True when the message went to the main thread."""
        app = self.app_for(ctx, event, owner)
        if app is None:
            return False
        chat = str(event.get("chat_id") or "")
        self.keep_topic(ctx, app, chat)
        message = str(event.get("message_id") or event.get("id") or "")
        remember_topic_message(ctx.state_dir, main_topic_id(chat), chat, message)
        target = app.submission.event_context(app.harness.context, event)
        routed = {}
        if not is_main_topic(target.topic_id, chat) and retired_topic(ctx.state_dir, target.topic_id):
            # Its reply chain starts at a frozen front-desk topic's message (core/topics.py context_for_topic finds the
            # root's alias first, and gave this message's to it): the main thread's all the same, as a new message at
            # the top level, never a turn in the frozen topic.
            move_topic_message(ctx.state_dir, main_topic_id(chat), chat, message)
            target = app.submission.event_context(app.harness.context, {"chat_id": chat, "message_id": message})
            routed = {"routed": True}
        timing.mark("route")
        bare = bare_message(event)
        app.handle_event(event, target, supplement=functools.partial(self.supplement, app, chat, bare=bare),
                         hold=self.hold_for(chat, bare), **routed)
        return True

    def hold_for(self, chat, bare) -> int:
        """How long a new turn of this message waits for the owner's words: HOLD_SECONDS for one with none of its own
        while no turn of the main thread runs or waits (``supplement`` takes it when one does); else 0, at once."""
        if not bare:
            return 0
        jobs = self.runtime.jobs.topic_jobs(main_topic_id(chat), chat)
        if any(not job.cancelled_before_start and not job.cancel_event.is_set() and not job.answer_finalized
               for job in jobs):
            return 0
        return contract.traits(main_topic_id(chat), chat).hold_seconds

    def keep_topic(self, ctx, app, chat):
        """The topic.json as the settings say: created on the first message. A new model, budget or 脱敏 applies to
        the next turn. Another workspace applies once nothing runs, on a new session (Claude Code finds a session by
        its directory; the topic's history goes with the new session's first message)."""
        settings = main_thread_settings.load(ctx.state_dir)
        topic_id = main_topic_id(chat)
        config = load_config(ctx.state_dir, ctx.default_workdir)
        name = settings.workspace if settings.workspace in config["workspaces"] else config["default"]
        workdir = str(Path(config["workspaces"][name]).expanduser().resolve())

        def build(saved):
            data = {**(saved or {
                "topic_id": topic_id, "chat_id": chat, "root_id": "", "thread_id": "", "main_thread": True,
                "harness_id": app.spec.name, "binding_revision": 1, "workspace_locked": True, "harness_locked": True,
                "model_settings_snapshot": True, "workspace_name": name, "workdir": workdir}),
                    "model": settings.model, "effort": settings.effort}
            # 脱敏 from the next turn, as 换配置 turns a topic's (app/binding.py set_redaction).
            if settings.redaction:
                data[REDACTION_KEY] = True
            else:
                data.pop(REDACTION_KEY, None)
            return data
        self.write_topic(ctx, app, topic_id, name, workdir, build)

    def keep_track_topic(self, ctx, app, chat, slot) -> str:
        """The topic.json of track ``slot`` (1..SLOTS, core/main_tracks.py), created or brought up to date: slot 0's
        (``keep_topic`` first, so it is current) with the settings as they are, ``track: slot`` and its own id. The
        track has no setting of its own: a new model, effort, workspace or 脱敏 of the main thread reaches it here,
        a new workspace once nothing runs in it (``write_topic``). Its topic id."""
        if type(slot) is not int or not 1 <= slot <= main_tracks.SLOTS:
            raise ValueError(f"track slot {slot!r} is not 1..{main_tracks.SLOTS}")
        self.keep_topic(ctx, app, chat)
        root_path = topic_file(ctx.state_dir, main_topic_id(chat))
        topic_id = main_tracks.track_topic_id(chat, slot)

        def build(saved):
            root = read_object(root_path)
            if not root.get("workdir"):
                raise ValueError("the main thread's topic.json cannot be read")
            data = {**{key: value for key, value in root.items() if key not in ("topic_id", "track")},
                    "topic_id": topic_id, "track": slot}
            for key in ("workdir", "workspace_name", "binding_revision"):  # ``write_topic`` moves these, nothing running.
                if saved and key in saved:
                    data[key] = saved[key]
            return data
        root = read_object(root_path)
        self.write_topic(ctx, app, topic_id, root.get("workspace_name", ""), root.get("workdir", ""), build)
        return topic_id

    def write_topic(self, ctx, app, topic_id, name, workdir, build) -> None:
        """Write the main thread's family topic ``topic_id``'s topic.json as ``build(saved)`` says (``saved``: what is
        there, None when nothing is), under LOCK. A workspace that changed with nothing running in the topic
        takes its next turn on a new session: ``name``, ``workdir`` are the new one."""
        path = topic_file(ctx.state_dir, topic_id)
        with LOCK:
            saved = read_object(path)
            data = build(saved or None)
            if saved and data.get("workdir") != workdir and not self.runtime.jobs.in_topic(topic_id):
                data.update(workspace_name=name, workdir=workdir,
                            binding_revision=int(data.get("binding_revision", 1)) + 1)
                pointer = path.parent / app.spec.session_filename
                with self.runtime.session_lock:
                    revision, session = capture_session_state(pointer, app.spec.session_pattern)
                    if session:
                        compare_and_set_session(pointer, app.spec.session_pattern, revision, session, None)
            if data != saved:
                write_json(path, data, ensure_ascii=False, indent=2)

    def supplement(self, app, chat, ctx, prompt, message, *, bare=False, content="", sender="") -> bool:
        """True when this turn took ``prompt`` (or may have: rule 6, it is not sent again); False to queue it behind.
        One the turn then certainly never takes (refused, or ended unread) becomes a turn of its own as it would have
        been queued now (``content``, ``sender``; app/steering.py requeue_lost): its ``requeue`` in the record.
        A turn held for the owner's words (``hold_for``) that took it into its request starts now, or, ``bare``
        (still no words), waits HOLD_SECONDS again. Its "你问" block notes the words he wrote (core/attachments.py
        message_words: not the attachments block, the quoted message or a local path), none of a ``bare`` one."""
        steering = app.steering
        jobs = sorted(self.runtime.jobs.topic_jobs(ctx.topic_id, chat), key=lambda job: int(job.job_id))
        job = next((job for job in reversed(jobs) if steering.steer_stage(job) != "ended"), None)
        if job is None or steering.steer_stage(job) == "paused":
            return False
        try:
            requeue = {"text": content or prompt, "prompt": prompt, **({"sender_id": sender} if sender else {})}
            saved = steering.deliver_steer(ctx, job, prompt, message, asked="" if bare else message_words(prompt),
                                           requeue=requeue)
            if job.start_held_until and (saved or {}).get("merged"):
                job.start_held_until = time.time() + contract.HOLD_SECONDS if bare else 0
                self.runtime.log(f"main thread hold {'renewed' if bare else 'released'} job=#{job.job_id}")
        except SteerUncertain:
            self.runtime.log(f"main thread supplement uncertain job=#{job.job_id} message={message[-10:]}")
            app.reply(ctx, chat, SUPPLEMENT_UNCERTAIN, app.key("main-supplement", message))
        except SteerRefused as exc:
            self.runtime.log(f"main thread supplement queued instead job=#{job.job_id}: {exc}")
            return False
        return True


def main_turn(ctx, job, prompt, session_id, compaction, log):
    """(context, message) of a turn: unchanged but for the main thread's, whose context carries its tools and what its
    compaction keeps (``COMPACT_INSTRUCTIONS``). ``compaction``: a request that waits means this turn runs on a
    compacted session, so it reads everything again."""
    if not contract.traits_of(job).own_turn:
        return ctx, prompt
    # Before the snapshot and the session: an idle compaction under way stops, its request back for this turn's end.
    idle_compaction.stop_for_turn(ctx, job, log)
    key = job_key(job.message_id, job.job_id)
    ctx = dataclasses.replace(ctx, options={**ctx.options, "compact_instructions": COMPACT_INSTRUCTIONS,
                                            "mcp_config": mcp_config(RELEASE, ctx.state_dir, key, sys.executable)})
    if job.kind != "chat":
        return ctx, prompt
    try:
        # A request this turn takes compacts the session first; one left to the idle compaction does not.
        request = compaction.request(ctx)
        snapshot = take_snapshot(ctx, job.chat_id, key,
                                 full=not session_id or (request is not None and not left_for_idle(request)))
    except Exception as exc:  # noqa: BLE001 - the owner's message goes on without it
        log(f"main thread snapshot unavailable job=#{job.job_id}: {type(exc).__name__}")
        snapshot = "[快照暂时读不到，需要时用 topics_list]"
    return ctx, main_thread_message(snapshot, prompt)


def compact_main_thread(compaction, ctx, job) -> None:
    """A turn of the main thread ended past ``compact_tokens``: a compaction request. One its task runner records is
    ``idle``: the Bridge compacts once the turn's runner has exited and the topic stayed idle (app/idle_compaction.py),
    and the next turn compacts first only when that did not happen in time (app/compaction.py left_for_idle)."""
    if not contract.traits_of(job).own_turn:
        return
    try:
        threshold = main_thread_settings.load(ctx.state_dir).compact_tokens
        tokens = getattr(job.tool_trace, "context_tokens", None)
        if type(tokens) is not int:
            tokens = (read_job_record(job_path(ctx.state_dir, job_key(job.message_id, job.job_id))) or {}).get("context_tokens")
        if threshold > 0 and type(tokens) is int and tokens >= threshold and compaction.supported(ctx):
            compaction.set_requested(ctx, job.job_id, tokens=tokens, threshold=threshold,
                                     idle=compaction.runtime.in_runner)
    except (OSError, ValueError) as exc:
        compaction.runtime.log(f"main thread compaction not requested job=#{job.job_id}: {type(exc).__name__}")


def line(*parts) -> str:
    text = " ".join(" ".join(str(part).split()) for part in parts if part)
    return text[:LINE] + ("…" if len(text) > LINE else "")


def snapshot_groups(ctx, chat, now) -> dict:
    """Each group's rows, topic id -> its line, as a turn should read them; the main thread's own topic in none, nor
    one the owner took off the board (app/pins.py dismissed, read only with the board on). The indexes are read once
    (core/board_read.py read): the questions and running jobs as 手上的事 lists them (app/overview.py view), the
    results not seen (``result``, 有结果未看) the board's (app/pins.py board_results: core/board_read.py unseen,
    however old; a topic waiting on a question older than the board's QUESTION_AGE is in both groups), and with the
    board on its look's taken off."""
    state = ctx.state_dir
    board = pins.PinnedTopics.enabled()
    read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
    unseen, off = pins.board_results(state, chat, now, read)
    off = off if board else {}
    waiting, running = {}, {}
    for record in sorted(read.questions, key=lambda row: row.get("created_at") or 0, reverse=True):
        topic = record.get("topic_id") or ""
        if topic in off:
            continue
        label, title = topic_words(state, topic)
        waiting.setdefault(topic, line(label, f"{title}：{turn_words.ask_words(record)}"))
    jobs = sorted((record for record in read.jobs if record.get("status") in RUNNING),
                  key=lambda record: int(record.get("job_id") or 0))
    for record in {record.get("key") or str(record.get("job_id")): record for record in jobs}.values():
        topic = record.get("topic_id") or ""
        if topic in off:
            continue
        label, title = topic_words(state, topic)
        phase = str(record.get("phase") or "")
        phase = "" if phase in turn_words.UNSET_PHASES else phase
        harness = record.get("harness_id") or record.get("backend") or ""
        # The step under way ("第 3/5 步：<title> · 本步 40 分钟") in place of the phase, as the task card says it.
        step, doing = step_text(checkpoint_steps({"steps": record.get("steps")}), record.get("step_times"), now)
        running.setdefault(topic, line(label, " · ".join(part for part in (
            title, harness, record.get("workspace_name") or "", not doing and phase and "阶段：" + phase,
            step) if part)))
    results = {}
    for topic, record in sorted(unseen.items(), key=lambda item: ended_at(item[1]), reverse=True):
        if topic in off:
            continue
        label, title = topic_words(state, topic)
        text = line(label, f"{title}：{RESULTS.get(record['status'], record['status'])}",
                    time.strftime("%m-%d %H:%M", time.localtime(ended_at(record))))
        # Its conclusion quoted the owner's message at the top level (app/main_relay.py): kept whole on the line.
        mark = RELAYED if main_relay.relayed(state, record) else ""
        results[topic] = text[:LINE - len(mark)] + mark if len(text) + len(mark) > LINE else text + mark
    opened = {}
    rows = []
    for _, record in main_opened.records(state):
        topic = record.get("topic_id") or record.get("fork_topic_id")
        if topic:
            rows.append((float(record.get("at") or 0), str(topic), record))
    for _, topic, record in sorted(rows, key=lambda row: row[0], reverse=True)[:OPENED]:
        label = topic_words(state, topic)[0]
        group = ("waiting" if topic in waiting else "result" if topic in results else "running" if topic in running
                 else "")
        where = "主人从看板撤下了" if topic in off else turn_words.GROUP_NAMES[group] if group else "没在动"
        opened[topic] = line(label, f"{record.get('title') or ''}（{where}）")
    return {"waiting": waiting, "result": results, "running": running, "opened": opened}


def render_snapshot(groups, previous, *, full, at, receipts) -> str:
    """``previous``: the groups the last turn read, when this one says only what changed."""
    sections, shown = [], 0
    for key, name in GROUPS:
        rows = groups[key]
        changed = rows if full else {topic: text for topic, text in rows.items() if previous.get(key, {}).get(topic) != text}
        name = name if receipts or key != "result" else NO_RECEIPTS
        if changed or full:
            items = [f"- {text}" for text in list(changed.values())[:ROWS]]
            if len(changed) > ROWS:
                items.append(f"- 另有 {len(changed) - ROWS} 个，用 topics_list 看")
            sections.append("\n".join((f"{name}：", *(items or ["（无）"]))))
            shown += len(changed)
        gone = [] if full else [text for topic, text in previous.get(key, {}).items() if topic not in rows]
        if gone and key != "opened":
            sections.append("\n".join((f"不再在“{name}”：", *(f"- {text}" for text in gone[:ROWS]))))
            shown += len(gone)
    head = ("[快照 {}：全部]" if full else "[快照 {}：只列和上一轮相比变了的，其余没变]" if shown
            else "[快照 {}：和上一轮相比没有变化]").format(at)
    return "\n".join((head, *sections))


def take_snapshot(ctx, chat, key, *, full, now=None) -> str:
    """This turn's snapshot text; saved, for the next turn to compare with."""
    now = time.time() if now is None else now
    path = topic_file(ctx.state_dir, main_topic_id(chat)).parent / SNAPSHOT
    saved = read_object(path)
    previous = saved.get("groups")
    if isinstance(previous, dict) and "results" in previous and "result" not in previous:
        previous = {**previous, "result": previous["results"]}  # Saved before 2026-11-10: the group's old key.
    last = saved.get("job")
    # The same job again, or a last turn that did not end well, may not have read it.
    settled = isinstance(last, str) and last != key and (read_job_record(job_path(ctx.state_dir, last)) or {}).get("status") in GOOD_ENDS
    # A compaction since (app/idle_compaction.py, between the turns) dropped what the session had read.
    compacted = idle_compaction.compacted_at(path.parent) > float(saved.get("at") or 0)
    full = full or compacted or not settled or not isinstance(previous, dict)
    groups = snapshot_groups(ctx, chat, now)
    text = render_snapshot(groups, previous if isinstance(previous, dict) else {}, full=full,
                           at=time.strftime("%m-%d %H:%M", time.localtime(now)), receipts=(ctx.state_dir / SEEN_FILE).exists())
    write_json(path, {"schema_version": 1, "job": key, "at": now, "groups": groups}, ensure_ascii=False)
    return text

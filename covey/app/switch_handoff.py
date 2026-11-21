"""A topic's first run after 🔁 换配置 rebinds it (app/harness_switch.py, app/binding.py).

The next message after a rebind starts a fresh native session on the new Harness. Its first
prompt carries, instead of the whole conversation, the path of a transcript file written for it: every user
message and final reply of the topic, plus where each earlier Harness keeps its native
session. Any Harness can read a file, while a deep link (such as Codex's) only
opens in its own client. When the old binding has a session its backend can summarize (``handoff_summary``:
Claude's, on a fork of the session, backends/claude/handoff.py), the old model's short summary comes with it
(<topic>/handoff/summary-r<revision>.md) and only the last round in plain text; otherwise (no session, another
Harness, a switch recorded before the summary existed or with a ↩️ 回退 pending, a failure or timeout) the last
HANDOFF_ROUNDS rounds. While the summary is asked for, a Bridge line on the task card says so, then its outcome.
No summary either of a session the Bridge has blocked or whose last turn a safety refusal stopped or retried
(``refused_session``): a fork would send the refused conversation again, to the model that refused it.
The handoff is the new binding's alone until one of its runs has finished with a session the topic may resume
(``settled``); a later fresh session of the same binding (a safety-refusal retry, a pointer cleared after a
failed turn, a ↩️ 回退 of a session's first turn) starts from its own history like any other fresh turn.
Each message in plain text is cut at MESSAGE_CHARS (no whole history for the new model
to cache again). SwitchHandoff is one Harness's side of it, with the 🔁 换配置 control on its
ended task cards (and ↩️ 回退 beside it on a finished one, app/rewind.py).
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from covey.app.binding import latest_switch_revision
from covey.cards.topic_actions import switch_panel, topic_panel
from covey.core import devices
from covey.core.defaults import work_mode_tag
from covey.core.harness import backend_capabilities
from covey.core.jobs import topic_job_records
from covey.core.redaction import topic_redaction
from covey.core.session_guard import session_is_blocked
from covey.core.state import epoch_seconds, write_private_text
from covey.core.history_storage import history_lines, recent_rounds
from covey.core.topics import proxy_topic, read_topic, topic_traits

HANDOFF_ROUNDS = 3  # Rounds the new Harness gets in plain text without a summary; the rest stays in the transcript file.
SUMMARY_ROUNDS = 1  # With a summary.
MESSAGE_CHARS = 2000  # Of each message in plain text.
CUT = "…（完整内容见记录文件）"
SUMMARY_CHARS = 4000  # Of the summary in the prompt; the file keeps it whole.
WAITING = "正在请原模型写交接摘要（最多 3 分钟），写好后再开始本轮。"
STOPPED = "已停止，没有写交接摘要。"
# Card lines of a summary not asked for: the old session may not run again.
# Keyed by refused_session's reason, each line says why.
_RAW = f"不再续接它写交接摘要，改为把最近 {HANDOFF_ROUNDS} 轮原文交给新模型。"
REFUSED = {"policy refusal": "原会话被安全拒绝停过，" + _RAW,
           "policy fallback": "原会话因官方模型降级停过，" + _RAW,
           "policy retry": "原会话是安全拒绝后换旧模型的重试，" + _RAW,
           "session blocked": f"原会话已禁止续接，没有写交接摘要，改为把最近 {HANDOFF_ROUNDS} 轮原文交给新模型。"}
SUMMARY_PROMPT = (
    "[Bridge 交接请求] 本话题马上换到另一个模型接着做，它看不到这段会话。请为它写一份简短的交接摘要："
    "不要调用任何工具，不要继续做任务，只回复摘要本身。用本话题对话所用的语言写，不超过 1500 字，分这几块："
    "目标；已做的决定；当前状态；涉及的文件、路径和提交；未决问题和下一步。只写会话里确有的内容，不确定的标明。")


def tagged(mode):
    """" · Adaptive" after a binding's model and budget; nothing for the default mode."""
    tag = work_mode_tag(mode)
    return " · " + tag if tag else ""


def _binding_label(binding):
    effort = binding.get("effort") or "模型默认"
    return (f"{binding.get('title') or binding.get('harness_id')} · {binding.get('model') or '默认模型'}（{effort}）"
            + tagged(binding.get("work_mode")))


def write_transcript(ctx, topic):
    """The topic's whole conversation as one Markdown file; returns (path, rounds)."""
    switches = topic.get("switches") or []
    lines = [f"# 话题完整会话记录（话题 {topic.get('topic_number') or topic.get('topic_id', '')[:12]}）", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S %Z')}；工作区：{topic.get('workspace_name')}（{topic.get('workdir')}）",
             "按时间顺序记录本话题每条用户消息和每轮最终回复，不含工具调用过程。", ""]
    if switches:
        lines.append("配置更换记录：")
        lines += [f"- {time.strftime('%m-%d %H:%M', time.localtime(s['at']))} "
                  f"{_binding_label(s['from'])} → {_binding_label(s['to'])}"
                  + (f"；工作目录 {s['from']['workdir']} → {s['to']['workdir']}" if moved(s) else "") for s in switches]
        lines += ["", "之前各模型的原生会话记录（含工具调用细节，格式由各 Harness 自定）："]
        lines += [f"- {_binding_label(s['from'])}：" + (s["from"].get("transcript") or
                  (f"会话 {s['from']['session_id']}，文件未找到" if s["from"].get("session_id") else "没有原生会话"))
                  for s in switches]
        lines.append("")
    # History times have whole seconds; a switch needs an idle topic, so its second starts the new binding.
    bindings = [(int(s["at"]), s["to"]) for s in switches]
    current = switches[0]["from"] if switches else {"title": "", "model": topic.get("model"), "effort": topic.get("effort")}
    rounds, answered = 0, True
    for raw in history_lines(ctx.history_path):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        at = epoch_seconds(str(row.get("time")))
        while bindings and at is not None and bindings[0][0] <= at:
            current = bindings.pop(0)[1]
            lines += ["", f"---- 以下由 {_binding_label(current)} 接手 ----", ""]
        if row["role"] == "user":
            if answered:
                rounds += 1
                answered = False
            lines += [f"## 第 {rounds} 轮 · 用户 · {row.get('time', '')}", "", content, ""]
        else:
            answered = True
            label = _binding_label(current) if current.get("title") else "回复"
            lines += [f"## 第 {rounds} 轮 · 回复（{label}） · {row.get('time', '')}", "", content, ""]
    path = ctx.history_path.parent / "handoff" / f"transcript-r{topic.get('binding_revision', 1)}.md"
    write_private_text(path, "\n".join(lines) + "\n")
    return path, rounds


def moved(switch):
    """The switch changed the topic's directory (another workspace or run mode)."""
    before, after = switch["from"].get("workdir"), switch["to"].get("workdir")
    return bool(before and after and before != after)


def _say(note, text):
    """``note(text)`` when given; a card line that fails does not stop the turn."""
    if note is None:
        return
    try:
        note(text)
    except Exception:  # noqa: BLE001 - only the card line failed
        pass


def capped(rows, limit=MESSAGE_CHARS):
    """``rows`` with each message cut at ``limit`` characters (the rest is in the transcript file)."""
    out = []
    for row in rows:
        content = row.get("content") if isinstance(row, dict) else None
        if isinstance(content, str) and len(content) > limit:
            row = {**row, "content": content[:limit] + CUT}
        out.append(row)
    return out


def settled(ctx, revision):
    """Whether a run of binding ``revision`` has finished with a session the topic may resume: its job record
    has the revision, a session and ``native_resume_allowed`` (written when the turn is delivered, the same
    condition under which app/turn_settle.py records the session pointer). Found once, it is kept as
    <topic>/handoff/settled-r<revision>, so records removed later do not matter. Job records rather than the
    history: a failed or stopped turn writes a reply row too, and a first run stopped before it started would
    then lose the handoff it never used."""
    marker = ctx.history_path.parent / "handoff" / f"settled-r{revision}"
    if marker.exists():
        return True
    try:
        found = any(int(r.get("binding_revision") or 0) == revision and r.get("session_id")
                    and r.get("native_resume_allowed") is True for r in topic_job_records(ctx.state_dir, ctx.topic_id))
    except (OSError, ValueError, TypeError):
        return False
    if found:
        try:
            write_private_text(marker, "")
        except OSError:
            pass
    return found


def refused_session(ctx, backend, session_id):
    """Why the old binding's ``session_id`` may not be forked for a summary, or '' (a key of REFUSED): when the
    latest job of that session was stopped by the policy guard, "policy refusal" or "policy fallback" after its
    event's kind (core/model_policy.py), else "policy retry" when it was the retry on an older model after a
    refusal (app/policy_retry.py; in all three the fork would resend that conversation to the model that stopped
    or refused it), "session blocked" when the Bridge has blocked the native session (core/session_guard.py:
    never resumed automatically; an unreadable guard counts)."""
    try:
        jobs = [r for r in topic_job_records(ctx.state_dir, ctx.topic_id) if r.get("session_id") == session_id]
    except (OSError, ValueError, TypeError):
        jobs = []
    if jobs:
        latest = max(jobs, key=lambda r: int(r.get("job_id") or 0))
        stop = latest.get("policy_stop")
        if stop:
            return "policy fallback" if isinstance(stop, dict) and stop.get("kind") == "fallback" else "policy refusal"
        if latest.get("policy_retry"):
            return "policy retry"
    try:
        return "session blocked" if session_is_blocked(ctx, backend, session_id) else ""
    except ValueError:
        return "session blocked"


def handoff_note(topic, path, rounds, summary="", summary_path=None):
    switch = topic["switches"][-1]
    place = (f"工作目录从 {switch['from']['workdir']} 换成了上面的当前工作目录；之前各轮的改动在原目录里，"
             "没有提交的不会出现在新目录，需要时去原目录看。\n" if moved(switch) else "工作区不变，仍是上面的当前工作目录。\n")
    kept = SUMMARY_ROUNDS if summary else HANDOFF_ROUNDS
    if len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS] + "…（完整摘要见摘要文件）"
    where = f"，原文在 {summary_path}" if summary_path else ""
    brief = f"- 交接摘要（{_binding_label(switch['from'])} 在更换前写的{where}）：\n\n{summary}\n\n" if summary else ""
    return (f"\n\n本话题刚从 {_binding_label(switch['from'])} 更换为 {_binding_label(switch['to'])}，由你接着处理。"
            + place + brief +
            f"- 完整会话记录：{path}（共 {rounds} 轮，按时间顺序记录每条用户消息和每轮最终回复，不含工具调用过程）。"
            f"上面的“最近的对话”只有最近 {kept} 轮原文（每条最多 {MESSAGE_CHARS} 字）；"
            "需要更早的背景、决定或产物位置时先读这个文件，不要凭印象猜。文件较大时分段读取。\n"
            "- 记录开头列出了之前各模型的原生会话文件，需要工具调用细节时再查。\n"
            "- 更换前留下的待确认问题已作废，按用户最新的消息继续。")


class SwitchHandoff:
    """One Harness's side of a 换配置 switch: the controls on an ended task card and the
    handoff into its first run after a switch.

    - ``harness``: the Harness a switched topic now runs on (and whether it can rewind).
    - ``runtime``: the log.
    - ``cards``: the controls on the task card.
    - ``topics``: the Bridge's Topics (its ``apps``), for the old binding's summary; None for an app built alone.
    """

    def __init__(self, harness, *, runtime, cards, topics=None) -> None:
        self.harness = harness
        self.runtime = runtime
        self.cards = cards
        self.topics = topics  # The Bridge's Topics: the old binding's app, for its summary.

    def attach_topic_controls(self, ctx, job):
        """A finished turn's 换配置 and 回退 (cards/topic_actions.py topic_panel), at the bottom of its card."""
        if not job.progress or job.topic_controls_attached or not ctx.topic_id:
            return
        name = ctx.options.get("harness_id")
        panel = topic_panel(ctx.topic_id, job.job_id, (name, ctx.options.get("binding_revision", 1)) if name else None,
                            rewind=self.can_rewind(ctx), switch=topic_traits(ctx).switch_config)
        job.topic_controls_attached = bool(panel and self.cards.set_task_element(ctx, job, panel, at_bottom=True))

    def can_rewind(self, ctx):
        """The panel has ↩️ 回退 (app/rewind.py): the Harness can drop a turn; not in a 代答 topic, whose replies
        have gone to the contact."""
        return backend_capabilities(self.harness.backend, ctx).rewind and not proxy_topic(ctx) and topic_traits(ctx).rewind

    def attach_switch_controls(self, ctx, job):
        """An ended turn's 换配置 alone: failed, stopped or interrupted."""
        if (not job.progress or job.switch_controls_attached or job.topic_controls_attached
                or not ctx.topic_id or not ctx.options.get("harness_id") or not topic_traits(ctx).switch_config):
            return  # The butler's settings are on its settings card.
        job.switch_controls_attached = bool(self.cards.set_task_element(ctx, job, switch_panel(
            ctx.topic_id, job.job_id, ctx.options["harness_id"], ctx.options.get("binding_revision", 1)), at_bottom=True))

    def switch_handoff(self, ctx, history_snapshot, cancel=None, ready=None, note=None):
        """The first run(s) after a switch, until one has finished with a session (``settled``): the old model's
        summary, recent rounds in plain text (cut with ``capped`` after the turn's own message is taken out) and the
        transcript note. ``ready()``: whether the
        turn will start (app/runs.py); a turn about to be refused does not wait for a summary first.
        ``note(text)``: a line on the task card while the summary is asked for, and its outcome."""
        try:
            topic = read_topic(ctx)
        except (OSError, ValueError, KeyError):
            return history_snapshot, ""
        switches = topic.get("switches") or []
        if (not switches or latest_switch_revision(topic) != topic.get("binding_revision")
                or topic.get("harness_id") != self.harness.spec.name
                or settled(ctx, topic.get("binding_revision", 1))):
            return history_snapshot, ""
        try:
            path, rounds = write_transcript(ctx, topic)
        except OSError as exc:
            self.runtime.log(f"switch transcript unavailable: {type(exc).__name__}")
            return history_snapshot, ""
        summary, summary_path = self.summary(ctx, topic, cancel, ready, note)
        rows = recent_rounds(ctx.history_path, SUMMARY_ROUNDS if summary else HANDOFF_ROUNDS) or history_snapshot
        return rows, handoff_note(topic, path, rounds, summary, summary_path)

    def summary(self, ctx, topic, cancel=None, ready=None, note=None):
        """(text, file) of the old binding's handoff summary, made once per switch; ("", None) without one.
        Never raises, and never waits past the backend's own limit (backends/claude/handoff.py SUMMARY_SECONDS)."""
        switch = topic["switches"][-1]
        old, revision = switch.get("from") or {}, topic.get("binding_revision", 1)
        path = ctx.history_path.parent / "handoff" / f"summary-r{revision}.md"
        try:
            kept = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):  # Not UTF-8 either: made again.
            kept = ""
        if kept:
            return kept, path  # An earlier attempt at this first run made it.
        app = self.topics.apps.get(old.get("harness_id")) if self.topics is not None else None
        summarize = getattr(app.backend, "handoff_summary", None) if app is not None else None
        workdir = Path(str(old.get("workdir") or ctx.workdir))
        if not old.get("session_id"):
            why = "no session"
        elif not callable(summarize) or app.harness.context is None:
            why = "not supported"
        elif ready is not None and not ready():
            # Before refused_session: its card line says the rounds go to the new model, which a refused turn
            # never starts.
            why = "turn refused"
        elif refused := refused_session(ctx, app.spec.name, old["session_id"]):
            why = refused
        elif "rewind_keep" in old:
            # Switched with a ↩️ 回退 pending (app/binding.py): a fork of the session's tip would summarize the
            # round the owner dropped (and the files it changed, maybe put back since) for the new model.
            why = "rewind pending"
        elif "redaction" not in old:
            # Switched before the summary existed: whether its turns were redacted is not known, so nothing is sent.
            why = "no redaction record"
        elif getattr(ctx, "device", "") or devices.is_device_value(old.get("workdir") or ""):
            why = "device"  # The old session is on the device; the summary would run on this machine.
        elif not workdir.is_dir():
            why = "workdir missing"
        else:
            why = ""
        if not why:
            try:
                # Through the relay when the old turns went through it or the topic has 敏感词脱敏 on now (turned on
                # in this switch or after it): the fork sends the whole old session again.
                redact = old["redaction"] is True or topic_redaction(ctx.state_dir, ctx.topic_id)
            except ValueError:
                why = "redaction unknown"
        if why:
            self.runtime.log(f"switch summary skipped topic={ctx.topic_id[:12]} from={old.get('harness_id')} {why}")
            if why in REFUSED:
                _say(note, REFUSED[why])
            return "", None
        base = app.harness.context
        target = dataclasses.replace(
            base, topic_id=ctx.topic_id, workspace_name=old.get("workspace") or ctx.workspace_name, workdir=workdir,
            history_path=ctx.history_path, session_path=ctx.history_path.parent / app.spec.session_filename,
            model=old.get("model") or base.model, effort=old.get("effort") or base.effort,
            options={**base.options, "harness_id": old.get("harness_id")})
        started = time.monotonic()
        _say(note, WAITING)
        try:
            text, outcome = summarize(target, old["session_id"], SUMMARY_PROMPT, redact=redact,
                                      cancel=cancel, run_id=f"handoff-r{revision}")
        except Exception as exc:  # noqa: BLE001 - the turn goes on with the plain rounds
            text, outcome = "", f"出错（{type(exc).__name__}）"
        text = str(text or "").strip()
        if text:
            _say(note, f"交接摘要写好了（{len(text)} 字），和最近 {SUMMARY_ROUNDS} 轮原文一起交给新模型。")
        elif outcome == "已停止" or (cancel is not None and cancel.is_set()):
            _say(note, STOPPED)  # The turn ends before the new Harness starts (app/runs.py): no rounds go anywhere.
        else:
            _say(note, f"没拿到交接摘要（{outcome}），改为把最近 {HANDOFF_ROUNDS} 轮原文交给新模型。")
        if text:
            try:
                write_private_text(path, text + "\n")
            except OSError:
                path = None
        self.runtime.log(f"switch summary topic={ctx.topic_id[:12]} from={old.get('harness_id')}:{target.model} "
                         f"{outcome} chars={len(text)} {time.monotonic() - started:.1f}s")
        return (text, path) if text else ("", None)

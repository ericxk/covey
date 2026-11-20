"""What the buttons of a 专项卡 run: stop the project or a task, the pause card's choices, 不要专项, the 分流记录's 不对 form, deleting a memory or library row.

Each click is checked first: it must belong to the project's lead topic, the topic the card's message routes to (``click``).
``host`` is the component the click came to (its runtime, messenger and voice); app/card_actions.py claims the kinds
(``controls.project_*``, ``steering.project_steer``) and app/project_cards.py is the card these buttons are on.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from covey.app import project_loop
from covey.app.card_publish import words
from covey.app.project_cards import DOWNGRADED
from covey.cards.project_card import ASK_KINDS
from covey.core import dispatch_records as drecords
from covey.core import project_library, project_memory, projects
from covey.core.locks import topic_lock
from covey.core.state import read_object
from covey.core.topics import clear_topic_project, move_topic_message, route_message, topic_file, topic_label

NO_LEAD_JOB = "负责人现在没有在做的一轮。要对它说话，引用这张专项卡直接回复就行。"


def topic_ctx(state_dir, topic_id):
    """What ``topic_lock`` needs of a topic: where its history is, which is the topic's directory."""
    return SimpleNamespace(history_path=topic_file(Path(state_dir), topic_id).parent / "history.jsonl")


def downgrade(host, ctx, record) -> str:
    """不要专项: the line said to the owner. The record is cancelled first (``ProjectCards`` then shrinks the card to "⏹ 专项已降为
    普通话题"); the lead's queued and running tasks are stopped; the threads (and the topic the project grew from) lose their
    ``project`` field; the card's message, and the lines of the 派发卡 it took in, go back to the topic the project grew from
    (a direct project's, to its first thread; none when it has none). Every step is safe to repeat, so a click on a project that
    was turned back half way finishes it. ``host`` is the component the click came to (its runtime, messenger and voice)."""
    state, pid = Path(ctx.state_dir), str(record["id"])
    now = projects.read_record(state, pid) or record
    name = words(now.get("name"), 30) or "专项"
    if now.get("state") == projects.DONE:
        return f"专项「{name}」已经做完，没有要降的。"
    if now.get("state") == projects.CANCELLED and now.get("state_reason") != DOWNGRADED:
        return f"专项「{name}」已经取消了。"
    if now.get("state") != projects.CANCELLED:
        try:
            now = projects.set_state(state, pid, projects.CANCELLED, DOWNGRADED)
        except ValueError:  # Done, or cancelled, since it was read.
            return f"专项「{name}」刚刚已经结束了，没有要降的。"
    chat, lead = str(now.get("chat_id") or ""), str(now.get("lead_topic_id") or "")
    stopped = stop_lead(host, ctx, lead, chat, pid)
    origin = now.get("origin") if isinstance(now.get("origin"), dict) else {}
    topics = [topic for topic in dict.fromkeys([origin.get("topic_id"), *(now.get("threads") or ())])
              if isinstance(topic, str) and topic]
    plain = [topic for topic in topics if release(state, topic, pid)]
    back = origin.get("topic_id") or next(iter(topics), "")
    if back:
        messages = {str((now.get("card") or {}).get("message_id") or "")}
        for path in drecords.live_paths(state):
            saved = read_object(path)
            if saved.get("project_id") == pid:
                messages.add(str(saved.get("card_message_id") or ""))
        card = str((now.get("card") or {}).get("message_id") or "")
        for message in sorted(messages - {""}):
            if message == card:
                move_topic_message(state, back, chat, message)  # Its clicks too: the upgrade made the card the lead's.
            route_message(state, chat, message, back, holder=back)
    label = topic_label(state, back) if back else ""
    return (f"已把专项「{name}」降回普通话题：已请负责人的 {stopped} 个任务停下，{len(plain)} 个话题变回普通话题"
            + (f"；之后引用这张卡回复，会回到 {label or '原话题'}" if back else "；它没有话题，引用这张卡回复也没有话题可回") + "。")


def release(state, topic, pid) -> bool:
    """``topic`` is an ordinary topic again (its ``project`` field of project ``pid`` is gone); whether it is one now. A topic of
    another project, or whose topic.json is gone, is left as it is."""
    found = projects.topic_project(state, topic)
    if found is None:
        return True
    if found.id != pid:
        return False
    try:
        with topic_lock(topic_ctx(state, topic)):
            clear_topic_project(state, topic)
    except OSError:
        return False
    return True


STOP_ALIAS = "project-stop-"  # The Bridge's own message a forwarded stop of a project's task names: an alias of its topic.


def stop_task(host, ctx, topic, chat, job, event_id, by="project") -> None:
    """Ask ``job`` of ``topic`` to stop as a click on its card's 停止 does; ``by`` says who (core/jobs.py STOP_SOURCES),
    RECALLED: the owner recalled the message the task came from (app/source_watch.py), so its card says that instead of
    a click. A job in a live task runner is stopped there, by
    that click forwarded to the runner (app/runner_ipc.py forward_card_action, as app/butler_tracks.py and app/proxy.py
    stop_job do): the runner owns the job, and a stop set only in this Bridge's copy of it reaches nobody (accept8,
    2026-11-16: the threads' commands ran on to their end). The click names an alias of the topic's own (``STOP_ALIAS``),
    so the runner routes it to that topic whatever card the job has (a lead's quiet turn has none). Any other job (queued,
    or in this Bridge) is stopped here."""
    from covey.app import runner_ipc
    from covey.core.jobs import JOB_STOP_ACTION_KIND

    target = dataclasses.replace(ctx, topic_id=topic)
    message = STOP_ALIAS + str(topic)
    move_topic_message(ctx.state_dir, topic, chat, message)
    action = {"kind": JOB_STOP_ACTION_KIND, "job_id": str(job.job_id), "message_id": job.message_id, "by": by}
    click = {"chat_id": chat, "message_id": message, "event_id": event_id,
             "operator_id": host.runtime.owner_open_id or "", "action_value": action}
    if runner_ipc.forward_card_action(host.runtime, target, click, action):
        host.runtime.log(f"project stop job=#{job.job_id} sent to its task runner")
        return
    host.cancel_reply_job_from_card(target, action, chat, event_id)


def stop_tasks(host, ctx, topics, chat, event_id, by="project") -> int:
    """Stop the tasks of ``topics`` that are queued or running (``stop_task``, the event id of each is ``event_id`` and its
    job number, ``by`` who stopped them: core/jobs.py STOP_SOURCES); how many."""
    stopped = 0
    for topic in topics:
        for job in host.runtime.jobs.open_in_topic(topic, chat):
            stop_task(host, ctx, topic, chat, job, f"{event_id}-{job.job_id}", by)
            stopped += 1
    return stopped


def stop_lead(host, ctx, lead, chat, pid) -> int:
    """Stop the lead's tasks that are queued or running (as a stop on a task's own card does, ``stop_task``); how many."""
    stopped = 0
    for job in host.runtime.jobs.open_in_topic(lead, chat):
        stop_task(host, ctx, lead, chat, job, f"project-downgrade-{pid}-{job.job_id}")
        stopped += 1
    return stopped


def click(ctx, event):
    """(the click's value, the project's record) of a click on a project card: the value's topic must be the one the click
    routed to (the lead's), and the project one that is there, in this chat."""
    from covey.app.card_actions import action_value

    raw = action_value(event)
    if raw.get("topic_id") != ctx.topic_id:
        raise ValueError("操作不属于当前话题")
    record = projects.read_record(ctx.state_dir, str(raw.get("project_id") or ""))
    chat = str(event.get("chat_id") or "")
    if not record or record.get("lead_topic_id") != ctx.topic_id or (chat and record.get("chat_id") != chat):
        raise ValueError("这个专项已经不在了")
    return raw, record


def say(host, ctx, event, text, name) -> bool:
    event_id = str(event.get("event_id") or event.get("message_id") or "")
    host.messenger.reply(ctx, str(event.get("chat_id") or ""), text, host.voice.key(name, event_id), voice=host.voice)
    return True


def steer_job(host, ctx, event) -> str:
    """补充: the job of the lead's that the supplement goes to (its running turn), '' when the lead has none."""
    from covey.app.dispatch_cards import click_job

    raw, record = click(ctx, event)
    return click_job(host.runtime, ctx.state_dir, ctx.topic_id, str(event.get("chat_id") or ""))


def stop_project(host, ctx, event) -> bool:
    """停止专项: the project is 停住 (``PAUSED``) and every task of its lead and its threads that is queued or running is
    stopped, as a stop on its own card would. A project that is not on says so."""
    raw, record = click(ctx, event)
    name = words(record.get("name"), 30) or "专项"
    def idle():
        return say(host, ctx, event, f"专项「{name}」已经是{'做完' if record.get('state') == projects.DONE else '停住'}的，没有要停的。",
                   "project-stop-idle")
    if record.get("state") != projects.ACTIVE:
        return idle()
    try:
        projects.set_state(ctx.state_dir, record["id"], projects.PAUSED, "主人停止")
    except ValueError:  # Another click or the lead moved it first.
        return idle()
    stopped = stop_tasks(host, ctx, [record["lead_topic_id"], *(record.get("threads") or ())], str(event.get("chat_id") or ""),
                         str(event.get("event_id") or event.get("message_id")))
    return say(host, ctx, event, f"已停住专项「{name}」，已请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停）。要继续，引用这张卡回复。", "project-stop")


def triage_wrong(host, ctx, event) -> bool:
    """不对 on a row of 分流记录: the form of the correction, a card of its own in the lead's topic (cards/triage_card.py)."""
    from covey.cards.triage_card import fix_form

    raw, record = click(ctx, event)
    row = next((r for r in record.get("triage") or () if isinstance(r, dict) and r.get("token") == raw.get("token")
                and r.get("kind") == "answered"), None)
    if row is None or row.get("corrected"):
        return say(host, ctx, event, "这一条已经纠正过，或已经不在分流记录里了。", "project-triage-gone")
    value = {"project_id": record["id"], "topic_id": ctx.topic_id, "token": str(raw.get("token"))}
    event_id = str(event.get("event_id") or event.get("message_id") or "")
    host.messenger.publish_card(ctx, str(event.get("chat_id") or ""), "triage-fix-" + event_id,
                                fix_form(words(row.get("text"), 160), value), voice=host.voice)
    return True


def triage_fix(host, ctx, event) -> bool:
    """提交纠正 on that form: what the owner said goes to the member topic as a message, into the project's decisions and
    its 分流记录 row (``corrected``); app/triage.py ``correct`` does it. The form then becomes the card that says so."""
    from covey.app import triage
    from covey.app.card_actions import form_fields
    from covey.cards.triage_card import FIELD

    raw, record = click(ctx, event)
    text = " ".join(str((form_fields(event) or {}).get(FIELD) or "").split())
    if not text:
        raise ValueError("纠正的内容为空")
    token = str(raw.get("token") or "")
    row = next((r for r in record.get("triage") or () if isinstance(r, dict) and r.get("token") == token), {})
    done = triage.correct(record, token, text)
    say(host, ctx, event, done, "project-triage-fixed")
    settle_fix_form(host, ctx, event, f"{words(row.get('text'), 120)} → {words(text, 120)}")
    return True


def settle_fix_form(host, ctx, event, summary) -> None:
    """The submitted form, replaced whole by the card that says it was corrected and with what (AGENTS.md rule 9; the
    acceptance of 2026-11-17 found the form open after its 提交纠正). A failed write leaves the form, whose second submit
    ``triage.correct`` refuses."""
    from covey.cards.triage_card import fixed_card

    source = str(event.get("message_id") or "")
    if not source:
        return
    try:
        host.messenger.publish_card(ctx, str(event.get("chat_id") or ""), "triage-fixed-" + source, fixed_card(summary),
                                    source_message=source, update_only=True, voice=host.voice)
    except (ValueError, OSError, RuntimeError) as exc:
        host.runtime.log(f"project triage fix form not settled: {type(exc).__name__}")


ASK_REFUSED = {"none": "专项「{name}」现在没有要定的停住。", "answered": "专项「{name}」停住后的选择已经做过了。",
               "stale": "这是专项「{name}」之前一次停住的卡，已经过期了。",
               "moved": "专项「{name}」已经不是因为这个停住了，没有要定的。"}


def ask_choice(host, ctx, event) -> bool:
    """继续, 收尾 or 停 on the pause card (COVEY_PROJECT_ASK=1): the choice is kept in the record under its lock first
    (``project_loop.answer_pause``; ``ProjectCards`` then writes the card as one line and the project card follows the state).
    继续 and 收尾 leave the lead a receipt that wakes it; 停 stops the tasks of the lead and the member topics as 停止专项 does."""
    raw, record = click(ctx, event)
    choice = next((name for name, kind in ASK_KINDS.items() if kind == raw.get("kind")), "")
    if not choice:
        raise ValueError("不认识的按钮")
    pid, name = str(record["id"]), words(record.get("name"), 30) or "专项"
    outcome = project_loop.answer_pause(ctx.state_dir, pid, choice, str(event.get("message_id") or ""))
    if outcome != "ok":
        return say(host, ctx, event, ASK_REFUSED[outcome].format(name=name), "project-ask-" + outcome)
    ask = (projects.read_record(ctx.state_dir, pid) or {}).get("pause_ask") or {}
    if choice != "stop":
        project_loop.pause_receipt(ctx.state_dir, pid, choice, ask)
        return True
    stopped = stop_tasks(host, ctx, [record["lead_topic_id"], *(record.get("threads") or ())], str(event.get("chat_id") or ""),
                         str(event.get("event_id") or event.get("message_id")))
    return say(host, ctx, event, f"已停住专项「{name}」，已请 {stopped} 个任务停下（在跑的命令可能要过一会儿才停）。要继续，引用这张卡回复。",
               "project-ask-stop")


def downgrade_project(host, ctx, event) -> bool:
    raw, record = click(ctx, event)
    return say(host, ctx, event, downgrade(host, ctx, record), "project-down")


def delete_memory(host, ctx, event) -> bool:
    """删 on a row of 专项记忆: the entry becomes a tombstone in projects/<id>/memory.json (the lead's pack lists it for 30
    days, so it is not noted again)."""
    raw, record = click(ctx, event)
    if not project_memory.delete(ctx.state_dir, record["id"], str(raw.get("entry") or "")):
        return say(host, ctx, event, "这条记忆已经不在了。", "project-mem-gone")
    return say(host, ctx, event, "已删掉这条专项记忆。", "project-mem-del")


def delete_library(host, ctx, event) -> bool:
    """删 on a row of 资料库: an entry of the index (and its copy in library/) or, for a file the index does not name, the file
    (a plain name inside library/, nothing else)."""
    raw, record = click(ctx, event)
    name = str(raw.get("name") or "")
    if name and project_library.delete(ctx.state_dir, record["id"], name):
        return say(host, ctx, event, "已从资料库删掉这一条。", "project-lib-del")
    path = projects.folder(ctx.state_dir) / record["id"] / "library" / name
    if not name or "/" in name or name.startswith(".") or not path.is_file() or path.is_symlink():
        return say(host, ctx, event, "这份资料已经不在了。", "project-lib-gone")
    path.unlink()
    return say(host, ctx, event, f"已删掉资料「{words(name, 40)}」。", "project-lib-del")

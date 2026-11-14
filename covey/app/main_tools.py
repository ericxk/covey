"""The main thread's tools as the Bridge runs them (docs/main-thread.md 工具通道), unless COVEY_MAIN_THREAD=0:
without it MainTools starts no thread and nothing here runs.

The loop (``MainTools.run``) first answers UNCERTAIN to every request a previous process claimed and never answered
(core/main_requests.py orphans: never run twice, AGENTS.md rules 1 and 6), then every TICK seconds takes the pending
requests, oldest first, and runs each one it claims: only for the main-thread job that is running now (its record
exists, its topic is main_topic_id of the owner's private chat, its status is in RUNNING), only a tool it has a
handler for. A handler that raises is answered as failed and logged; the loop goes on. One log line a call:
``main tool name=… ok=… ms=… job=…``. The loop holds no lock: a handler takes the locks it needs and sends to
Feishu outside them (rules 1 and 2).

The handlers here, in TOOLS: the read-only ones (topics_list, topic_read, workspaces_list: records read without
their locks; topics_search, which looks into every turn, is app/main_search.py), topic_priority (<state>/board-priority.json; with ``remove``, the topic off the pinned board:
app/pins.py dismiss) and question_answer, which answers a topic's pending
choice question through the same path as 提交回答 on its card (app/questions.py submit_answer), and the owner's
principles: principle_save, principle_delete, principles_list (core/principles.py, <state>/principles.json).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from covey.app import pins
from covey.core import board_read
from covey.core.board_read import PRIORITY, TOOLS_AGE, job_number, topic_jobs, topic_questions
from covey.app.answers import AnswerIncomplete, answer_fields
from covey.app.main_env import reply
from covey.app.overview import UNSET_PHASES
from covey.core import main_requests, main_thread, main_thread_settings, principles, turn_words
from covey.core.checkpoint import (checkpoint_delivery, checkpoint_steps, current_step, read_checkpoint,
                                            step_words)
from covey.core.jobs import job_path, read_job_record
from covey.core.locks import resource_lock
from covey.core.metrics import step_age
from covey.core.loops import every
from covey.core.state import epoch_seconds, read_object, write_json
from covey.core.statuses import RUNNING
from covey.core.topics import retired_topic, topic_file, topic_title
from covey.core.text import cut
from covey.core.workspaces import config_path

TICK = 0.3
REFUSED = "只接受正在跑的主线程任务的调用：这次调用没有执行。"
RECENT = TOOLS_AGE  # recent: what moved in the last three days (not the unseen results: ``result``).
# A topic's group: the first of these it is in; result is 有结果未看, the board's (core/board_read.py unseen).
GROUPS = ("waiting", "result", "running", "recent")
ALIASES = {"unread": "result"}  # The group's name in the tool's schema before 2026-11-10 (a session may hold it).
GROUP_NAMES = turn_words.GROUP_NAMES
LIMIT, MAX_LIMIT = 20, 60
TURNS, MAX_TURNS = 2, 5
ASK_CHARS = 120  # Of 在问什么 / 最后一句 in a list row.
TEXT_CHARS = 300  # Of the owner's message in topic_read.
RESULT_CHARS = 500  # Of a result's opening in topic_read.
JOB_RESULT_CHARS, JOB_TEXT_CHARS = 3000, 1000  # Of the result and the owner's words when topic_read is given a job.
ARTIFACTS = 5  # Checkpoint artifacts listed per turn in topic_read.
RECENT_TOPICS = 3  # Per workspace in workspaces_list.
PROXY_NOTE = "（主线程按主人的话代答）"  # On every answer question_answer submits: its 补充说明.


class MainTools:
    """``env``: app/main_env.py MainEnv. ``handlers``: tool name -> handler(env, request) -> answer."""

    def __init__(self, env, handlers):
        self.env, self.handlers = env, dict(handlers)

    def threads(self):
        """Its worker, unless the deployment rules the main thread out (app/unified.py worker_targets): the owner
        switches the mode while the Bridge runs, and only a main-thread turn asks it anything."""
        return (("main-tools", self.run),) if main_thread.available() else ()

    def run(self, ctx):
        self.recover()
        every(self.env.runtime, TICK, self.tick, isolate="main tools")

    def recover(self):
        """Requests a previous process claimed and never answered: answered UNCERTAIN, not run again."""
        for request in main_requests.orphans(self.env.state_dir):
            main_requests.answer(self.env.state_dir, request.id, main_requests.UNCERTAIN)
            self.env.runtime.log(f"main tool name={request.tool} ok=uncertain job={request.job_key[-12:]}")

    def tick(self):
        for request in main_requests.pending(self.env.state_dir):
            if main_requests.claim(self.env.state_dir, request.id):
                main_requests.answer(self.env.state_dir, request.id, self.execute(request))

    def execute(self, request) -> dict:
        started = time.monotonic()
        try:
            with self.env.serving(self.request_chat(request)):
                answer = self.refusal(request) or self.handlers[request.tool](self.env, request)
            if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
                raise TypeError("a handler answers {ok, text, data}")
        except Exception as exc:  # noqa: BLE001 - the main thread hears of it; the loop goes on.
            self.env.runtime.log(f"main tool name={request.tool} failed: {type(exc).__name__}: {exc}"[:500])
            answer = reply(False, f"{request.tool} 没有完成：{exc}" if isinstance(exc, ValueError)
                           else f"{request.tool} 执行出错（{type(exc).__name__}），可能没有完成：先核对再决定要不要重试。")
        self.env.runtime.log(f"main tool name={request.tool} ok={bool(answer.get('ok'))} "
                             f"ms={int((time.monotonic() - started) * 1000)} job={request.job_key[-12:]}")
        return answer

    def request_chat(self, request) -> str:
        """The chat of the main thread whose job made ``request`` (Access.agent_chat); '' when it is none, and with an
        access that has no agent_chat: the owner's private chat then (MainEnv.chat)."""
        agent_chat = getattr(self.env.runtime.access, "agent_chat", None)
        record = read_job_record(job_path(self.env.state_dir, request.job_key)) if request.job_key and agent_chat else None
        chat = str((record or {}).get("chat_id") or "")
        return chat if chat and agent_chat(chat) else ""

    def refusal(self, request):
        """None for a call the Bridge runs, else its answer."""
        chat = self.env.chat()
        record = read_job_record(job_path(self.env.state_dir, request.job_key)) if request.job_key else None
        if (not chat or not record or record.get("topic_id") != main_thread.main_topic_id(chat)
                or record.get("status") not in RUNNING):
            return reply(False, REFUSED)
        if request.tool not in self.handlers:
            return reply(False, f"Bridge 没有工具 {request.tool}：这次调用没有执行。")
        return None


# Reading the records (no record lock, no Feishu call).

_numbers = {}  # (state dir, topic_id) -> its T number: once given, it never changes.


def topic_data(env, topic_id):
    return read_object(topic_file(env.state_dir, topic_id))


def number(env, topic_id, data=None):
    key = (str(env.state_dir), topic_id)
    if key not in _numbers:
        value = (topic_data(env, topic_id) if data is None else data).get("topic_number")
        if type(value) is not int:
            return 0
        _numbers[key] = value
    return _numbers[key]


def label(env, topic_id, data=None):
    found = number(env, topic_id, data)
    return f"T{found}" if found else topic_id[:8]


def title(env, topic_id):
    """The topic's title as its cards and the board name it (core/topics.py topic_title)."""
    return topic_title(Path(env.state_dir), topic_id) or "（没有标题）"


def resolve(env, chat, ref, records=None):
    """(topic_id, its records) of the owner's topic ``ref`` names (T412, 412, a topic id); ValueError otherwise."""
    ref = str(ref or "").strip()
    records = topic_jobs(env.state_dir, chat) if records is None else records
    match = re.fullmatch(r"[Tt]?(\d+)", ref)
    if match:
        wanted = int(match.group(1))
        for topic_id in records:
            if number(env, topic_id) == wanted:
                return topic_id, records[topic_id]
        raise ValueError(f"主人的私聊里没有话题 T{wanted}")
    if ref in records:
        return ref, records[ref]
    if re.fullmatch("[0-9a-f]{16,128}", ref) and topic_data(env, ref).get("chat_id") == chat \
            and ref != main_thread.main_topic_id(chat):
        return ref, []
    raise ValueError(f"找不到话题“{ref[:40]}”：写 T 号（如 T412）或 topic_id")


def when(record):
    return epoch_seconds(record.get("updated_at")) or epoch_seconds(record.get("created_at")) or 0.0


def asked_at(question):
    value = question.get("created_at")
    return float(value) if type(value) in (int, float) else epoch_seconds(value) or 0.0


def clock(seconds):
    return time.strftime("%m-%d %H:%M", time.localtime(seconds)) if seconds else "—"


def last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def harness_title(env, name):
    harness = (getattr(env.topics, "harnesses", None) or {}).get(name)
    return getattr(getattr(harness, "spec", None), "title", "") or name or ""


def topic_row(env, topic_id, records, questions, unseen, now):
    """One topic's line in topics_list and what it is filtered and sorted by; ``unseen``: 有结果未看's topics."""
    data, latest = topic_data(env, topic_id), records[-1] if records else {}
    moved = max([when(record) for record in records] + [asked_at(q) for q in questions] + [0.0])
    groups = {"waiting": bool(questions),
              "result": topic_id in unseen,
              "running": any(record.get("status") in RUNNING for record in records),
              "recent": now - moved <= RECENT}
    group = next((name for name in GROUPS if groups[name]), "")
    if questions:
        said = "问：" + turn_words.ask_words(questions[-1], 40)
    elif groups["running"]:
        phase = str(latest.get("phase") or "")
        doing = phase if phase not in UNSET_PHASES else ""
        said = "在做：" + (doing or str(latest.get("user_text") or "").split("\n", 1)[0])
    else:
        said = "最后：" + (last_line(latest.get("final_text"))
                          or str(latest.get("status_message") or latest.get("status") or ""))
    row = {"label": label(env, topic_id, data), "title": title(env, topic_id), "group": group,
           "workspace": str(latest.get("workspace_name") or data.get("workspace_name") or ""),
           "harness": harness_title(env, str(data.get("harness_id") or latest.get("harness_id") or "")),
           "said": cut(said, ASK_CHARS), "moved": moved, "topic_id": topic_id}
    # In one of waiting, result and running; also under result whenever its result is unseen (a topic waiting on a
    # question older than the board's QUESTION_AGE may be), so that group is 有结果未看 whole; recent is every topic
    # that moved.
    return row, {group, *(name for name in ("result", "recent") if groups[name])}


def topics_list(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, "还不知道主人的私聊，没有话题可列。")
    args = request.args
    wanted = ALIASES.get(args.get("group"), args.get("group"))
    wanted = wanted if wanted in (*GROUPS, "all") else "recent"
    limit = args.get("limit") if type(args.get("limit")) is int else LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    query = str(args.get("query") or "").strip().lower()
    now, questions = time.time(), topic_questions(env.state_dir, chat)
    records = topic_jobs(env.state_dir, chat)
    state = Path(env.state_dir)
    read = board_read.read(state, chat, now, keep=pins.kept(state, chat) if pins.laid() else ())
    unseen = pins.board_results(state, chat, now, read)[0]
    rows = []
    for topic_id in (set(records) | set(questions)) - {main_thread.main_topic_id(chat)}:
        row, groups = topic_row(env, topic_id, records.get(topic_id, []), questions.get(topic_id, []), unseen, now)
        if wanted != "all" and wanted not in groups:
            continue
        if query and query not in " ".join((row["label"], row["title"], row["workspace"])).lower():
            continue
        rows.append(row)
    order = {name: index for index, name in enumerate((*GROUPS, ""))}
    rows.sort(key=lambda row: (order[row["group"]], -row["moved"]))
    shown = rows[:limit]
    if not shown:
        return reply(True, f"没有{GROUP_NAMES.get(wanted, '')}的话题" + (f"（过滤：{query}）" if query else "") + "。",
                     total=0)
    lines = [f"{row['label']} · {row['title'][:40]} · {GROUP_NAMES[row['group']]} · {row['workspace'] or '—'} · "
             f"{row['harness'] or '—'} · {row['said']} · 更新 {clock(row['moved'])} · {row['topic_id']}" for row in shown]
    more = f"\n（共 {len(rows)} 个，只列了前 {len(shown)} 个）" if len(rows) > len(shown) else ""
    return reply(True, "\n".join(lines) + more, total=len(rows), shown=len(shown))


def question_lines(question):
    """A pending question record as lines: each question, its options, single or multiple choice."""
    lines = [f"等你回答（{clock(asked_at(question))} 提出）："]
    for q in question.get("questions") or []:
        options = [str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict)]
        head = f"- [{q.get('id')}] {q.get('header') + '：' if q.get('header') else ''}{q.get('question') or ''}"
        if options:
            lines.append(head + f"（{'多选' if q.get('multiple') else '单选'}）")
            lines += [f"    · {option}" for option in options]
        else:
            lines.append(head + "（自由回答，没有选项）")
    return lines


def checkpoint_lines(record):
    path = str(record.get("checkpoint_path") or "")
    checkpoint = read_checkpoint(Path(path)) if path else {}
    lines = []
    for key, name in (("phase", "阶段"), ("completed", "已完成"), ("next_step", "下一步"), ("question", "在问")):
        value = checkpoint.get(key)
        if isinstance(value, list):
            value = "；".join(str(item) for item in value)
        if value:
            lines.append(f"  检查点{name}：{cut(value, TEXT_CHARS)}")
    artifacts = [str(item) for item in checkpoint.get("artifacts") or [] if item] \
        if isinstance(checkpoint.get("artifacts"), list) else []
    if artifacts:
        more = f"（共 {len(artifacts)} 个）" if len(artifacts) > ARTIFACTS else ""
        lines.append(f"  检查点产物{more}：" + "；".join(cut(item, TEXT_CHARS) for item in artifacts[:ARTIFACTS]))
    if checkpoint.get("requires_user"):
        lines.append("  检查点：在等主人")
    lines += step_lines(checkpoint_steps(checkpoint) or checkpoint_steps({"steps": record.get("steps")}),
                        record.get("step_times"), record.get("status") in RUNNING, time.time())
    delivery = checkpoint_delivery(checkpoint) or checkpoint_delivery({"delivery": record.get("delivery")})
    if delivery:
        lines.append("  检查点改动：" + "；".join(f"{name} {cut(delivery.get(key) or empty, TEXT_CHARS)}" for key, name, empty in (
            ("commit", "提交", "未提交"), ("deployed", "部署", "未部署"), ("verified", "验收", "未验收"))))
    return lines


STEP_STATES = {"done": "完成", "doing": "在做", "todo": "待做"}


def step_lines(steps, times, running, now):
    """The checkpoint's ``steps`` for topic_read, each with when the Bridge saw it start and end (``times``, the
    record's step_times, core/longtask.py) and how long it took or, for the step under way of a running turn, has
    run: what "还要多久" is answered from. None without steps."""
    if not steps:
        return []
    times = times if isinstance(times, dict) else {}
    doing = current_step(steps)
    head = step_words(steps) if doing is None else f"第 {doing[0]}/{len(steps)} 步"
    lines = [f"  检查点步骤（{head}）："]
    for number, step in enumerate(steps, 1):
        seen = times.get(step["title"]) if isinstance(times.get(step["title"]), dict) else {}
        started, ended = (seen.get(key) if type(seen.get(key)) in (int, float) else None for key in ("started", "ended"))
        parts = [f"    {number}. [{STEP_STATES[step['state']]}] {step['title']}"]
        if started is not None:
            parts.append(f"{clock(started)} 开始")
        if ended is not None:
            parts.append(f"{clock(ended)} 完成")
        if started is not None and ended is not None and ended >= started:
            parts.append(f"用时 {step_age(ended - started)}")
        elif step["state"] == "doing" and started is not None and running:
            parts.append(f"已 {step_age(now - started)}")
        lines.append(" · ".join(parts))
    return lines


def turns_asked(records, args):
    """The records topic_read shows, and how much of each result and of the owner's words: the last ``turns``, or with
    ``job`` (2906, "#2906") the ``turns`` ending at that turn (one by default), with more of both; ValueError for a job
    not in the topic."""
    job = str(args.get("job") if args.get("job") is not None else "").strip().lstrip("#")
    turns = args.get("turns") if type(args.get("turns")) is int else (1 if job else TURNS)
    turns = max(1, min(MAX_TURNS, turns))
    if not job:
        return records[-turns:], RESULT_CHARS, TEXT_CHARS
    if not job.isdigit():
        raise ValueError(f"job 要写轮次的编号（如 2906 或 #2906），不是“{job[:20]}”")
    at = next((i for i, record in enumerate(records) if job_number(record) == int(job)), None)
    if at is None:
        known = f"它的轮次是 #{job_number(records[0])} 到 #{job_number(records[-1])}" if records else "它还没有任务"
        raise ValueError(f"这个话题没有 #{job} 这一轮；{known}")
    return records[max(0, at - turns + 1):at + 1], JOB_RESULT_CHARS, JOB_TEXT_CHARS


def topic_read(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, "还不知道主人的私聊。")
    topic_id, records = resolve(env, chat, request.args.get("topic"))
    shown, result_chars, text_chars = turns_asked(records, request.args)
    data = topic_data(env, topic_id)
    lines = [f"{label(env, topic_id, data)} · {title(env, topic_id)} · 工作区 {data.get('workspace_name') or '—'} · "
             f"{harness_title(env, str(data.get('harness_id') or ''))} · {topic_id}"]
    if not shown:
        lines.append("还没有任务。")
    for record in shown:
        lines.append(f"\n#{record.get('job_id')} {record.get('status') or ''} · 更新 {clock(when(record))}")
        lines.append("  主人：" + cut(record.get("user_text") or record.get("prompt_text"), text_chars))
        result = str(record.get("final_text") or "").strip()
        if result:
            lines.append("  结果开头：" + (result if len(result) <= result_chars else result[:result_chars] + "…"))
        elif record.get("status_message"):
            lines.append("  状态：" + cut(record["status_message"], TEXT_CHARS))
        lines += checkpoint_lines(record)
    asked = topic_questions(env.state_dir, chat).get(topic_id, [])
    for question in asked:
        lines += ["", *question_lines(question)]
    return reply(True, "\n".join(lines), topic_id=topic_id, pending_questions=len(asked))


def workspace_spaces(env):
    """(name -> path, the default's name) of the registered workspaces; the default Harness's workdir without any."""
    config = read_object(config_path(Path(env.state_dir)))
    spaces = config.get("workspaces") if isinstance(config.get("workspaces"), dict) else {}
    default = config.get("default") if config.get("default") in spaces else ""
    if not spaces:
        harness = (getattr(env.topics, "harnesses", None) or {}).get(getattr(env.topics, "default", ""))
        fallback = getattr(getattr(harness, "context", None), "default_workdir", None)
        if fallback:
            spaces, default = {Path(fallback).name: str(fallback)}, Path(fallback).name
    return spaces, default


def workspaces_list(env, request):
    spaces, default = workspace_spaces(env)
    if not spaces:
        return reply(False, "没有可用的工作区。")
    settings = main_thread_settings.load(env.state_dir)
    chat = env.chat()
    recent = {}
    if chat:
        latest = sorted(((when(records[-1]), topic_id, records[-1])
                         for topic_id, records in topic_jobs(env.state_dir, chat).items()), reverse=True)
        for _, topic_id, record in latest:
            name = str(record.get("workspace_name") or "")
            if name in spaces and len(recent.setdefault(name, [])) < RECENT_TOPICS:
                recent[name].append(f"{label(env, topic_id)} {title(env, topic_id)[:30]}")
    lines = []
    for name, path in spaces.items():
        line = f"{name}{'（默认）' if name == default else ''} · {path}"
        if settings.note(name):
            line += f" · 用途：{settings.note(name)}"
        if recent.get(name):
            line += " · 最近：" + "；".join(recent[name])
        lines.append(line)
    return reply(True, "\n".join(lines), default=default, count=len(spaces))


def topic_priority(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, "还不知道主人的私聊。")
    topic_id, _ = resolve(env, chat, request.args.get("topic"))
    if request.args.get("remove") is True:
        if not pins.enabled():
            return reply(False, "看板没开，没有可撤的。")
        pins.dismiss(Path(env.state_dir), chat, [topic_id], time.time())
        return reply(True, f"已把 {label(env, topic_id)} 从看板撤下；它有新问题或新结果时会自己回来。",
                     topic_id=topic_id, removed=True)
    path = Path(env.state_dir) / PRIORITY
    with resource_lock("board-priority", str(path)):
        data = read_object(path)
        data[topic_id] = time.time()
        write_json(path, data, ensure_ascii=False)
    return reply(True, f"已把 {label(env, topic_id)} 在看板里本组内提到最前。", topic_id=topic_id)


# question_answer.

def question_answer(env, request):
    chat = env.chat()
    if not chat:
        return reply(False, "还不知道主人的私聊。")
    topic_id, _ = resolve(env, chat, request.args.get("topic"))
    if retired_topic(Path(env.state_dir), topic_id):
        return reply(False, f"{label(env, topic_id)} 是已下线的前台话题，没有可回答的问题；在主线程里直接做。")
    found = env.topics.target(env.state_dir, topic_id, chat)
    if found is None:
        return reply(False, f"{label(env, topic_id)} 现在不能回答（话题的 Harness 不可用或记录读不了）。")
    app, ctx, _ = found
    asked = app.questions.pending_questions(ctx, chat)
    if not asked:
        return reply(False, f"{label(env, topic_id)} 没有等主人回答的问题。")
    record = asked[-1]  # The newest: an older one still pending is folded under it once its card shows.
    fields = answer_fields(record, request.args.get("answers"), PROXY_NOTE)
    try:
        app.questions.submit_answer(ctx, chat, record["token"], fields, f"main-{request.id}")
    except AnswerIncomplete as exc:
        return reply(False, f"没有提交：{exc}")
    saved = read_object(Path(env.state_dir) / "questions" / (record["token"] + ".json"))
    given = saved.get("answers") if isinstance(saved.get("answers"), dict) else {}
    answered = "；".join(f"{q.get('question')} → {'、'.join((given.get(q.get('id')) or {}).get('answers', []))}"
                        for q in record.get("questions") or [])
    status = saved.get("status")
    if status == "uncertain":
        return reply(False, f"{label(env, topic_id)} 的回答已保存，但送达状态未确认：先用 topic_read 核对，别再答一次。",
                     topic_id=topic_id, status=status)
    return reply(True, f"已替主人回答 {label(env, topic_id)}：{answered}", topic_id=topic_id, status=status,
                 older_pending=len(asked) - 1)


# The owner's principles (core/principles.py): the file's own lock, no Feishu call.

def principle_row(item):
    where = "通用" if item["scope"] == principles.OWNER else f"工作区 {item['workspace']}"
    when = (item.get("at") or "")[:10]
    return f"{item['id']} · {where}{' · ' + when if when else ''} · {item['text']}"


def principle_save(env, request):
    args = request.args
    scope = args.get("scope")
    workspace = principles.clean_text(args.get("workspace")) if scope == principles.WORKSPACE else ""
    if workspace:
        spaces, _ = workspace_spaces(env)
        if workspace not in spaces:
            raise ValueError(f"没有工作区“{workspace[:40]}”；可选：" + ("、".join(spaces) or "（没有）"))
    record = read_job_record(job_path(env.state_dir, request.job_key)) if request.job_key else None
    source = {"job_key": request.job_key, **({"job_id": record["job_id"]} if (record or {}).get("job_id") else {})}
    item, new = principles.save(env.state_dir, scope, args.get("text"), workspace=workspace, source=source)
    where = "通用" if scope == principles.OWNER else f"工作区 {workspace} "
    if not new:
        return reply(True, f"{where}的原则里已有这条（{item['id']}）：{item['text']}，没有重复记。", id=item["id"], new=False)
    return reply(True, f"已记下{where}的原则（{item['id']}）：{item['text']}", id=item["id"], new=True)


def principle_delete(env, request):
    pid = str(request.args.get("id") or "").strip()
    if not pid:
        raise ValueError("要给原则的 id（principles_list 里每行开头的那个）")
    item = principles.delete(env.state_dir, pid)
    if item is None:
        return reply(False, f"没有 id 为 {pid[:20]} 的原则：用 principles_list 看现有的。")
    return reply(True, f"已删掉：{principle_row(item)}", id=item["id"])


def principles_list(env, request):
    rows = principles.entries(env.state_dir)
    if not rows:
        return reply(True, "还没有记下的原则。", count=0)
    return reply(True, "\n".join(map(principle_row, rows)), count=len(rows))


TOOLS = {"topics_list": topics_list, "topic_read": topic_read, "workspaces_list": workspaces_list,
         "topic_priority": topic_priority, "question_answer": question_answer,
         "principle_save": principle_save, "principle_delete": principle_delete, "principles_list": principles_list}

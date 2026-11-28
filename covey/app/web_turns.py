"""What a turn tells the web page beside its words (docs/web.md, COVEY_WEB=1; data contract v3): the steps of its process,
the checkpoint's plan, the token figures, the time it took and the notes of the memory feature. Pure shaping of a job record and its trace
(core/trace_store.py) into the optional fields of a ``turn`` item; app/web_sync.py asks for them and keeps the result of an ended turn.

- ``steps``: the newest ``STEPS_KEPT`` steps of the main task as {id, kind, label, started_at, ended_at, status}; ``kind`` is by the step's
  title (``STEP_KINDS``), a running step's ``ended_at`` is None. A note (the progress note) is no step: it is the turn's body.
  A subtask is one step from its start to its end, in its own words (data contract v8, ``steps_of``). The words are plain text, not the
  trace's card escaping; a step whose model said what it is for (Claude's "<purpose> · <command>") is labelled by the purpose and carries
  the command in ``detail``, as does a step whose label had to be cut (data contract v18, ``step_row``).
- ``plan``: the checkpoint's ``steps`` ({title, state}, core/checkpoint.py ``checkpoint_steps``); once the turn has ended, a step left
  ``doing`` is ``todo`` (it was not done, and nothing does it now).
- ``usage`` {input, output, cache_read, total}, ``seconds``, ``speed``, ``context`` {used, limit}: as the cards show them (cards/facts.py
  ``usage_figures``, core/metrics.py ``run_seconds``). While the turn runs ``seconds`` moves in steps of ``RUNNING_STEP`` and ``speed`` is a
  whole number, so a quiet minute does not change the item's ``rev`` every second.
- ``notes``: the record's ``memory_notes`` as written by the memory feature ({kind, text, scope, entry_id}); none without the field.
- ``memory`` (data contract v43): how many memory entries the turn carried, the titles of those it used (what the ended turn's
  checkpoint names in ``memory_used``) and their short codes (``memory_of``); a topic item's ``repos`` and ``home_start`` are
  ``topic_fields``.
- ``ran_with`` (data contract v27): the Harness, provider, model, budget and workspace the turn ran with (``ran_with``).
- ``artifacts`` (data contract v32): the turn's 产物 as its answer card lists them, each by its file name only, with its cloud-space link
  and size once uploaded, why it could not be, or that it is going up (``artifacts_of``).
- ``failure`` (data contracts v4, v10, v33): why the turn did not succeed and what next (core/failure.py failure_view), with what the
  page needs to act as the task card's buttons do (``failure_of``): each button's words, how the page carries it out, and a pending
  automatic retry.

A field is left out when nothing is known; the page draws what it gets. Nothing here reads more than the trace file and the checkpoint of
one turn, and never raises."""
from __future__ import annotations

import html
import re
import time
from pathlib import Path

from covey.app import auto_retry
from covey.cards import facts
from covey.core import criteria, failure
from covey.core.butler import LEAD, LEAD_OFF, traits_of
from covey.core.checkpoint import artifact_key, artifact_paths, checkpoint_delivery, checkpoint_steps, read_checkpoint, shows_change, upload_ledger
from covey.core.look import WAITS
from covey.core.metrics import run_seconds, timings_of
from covey.core.state import epoch_seconds
from covey.core.text import COMMAND_TITLES, command_core, command_purpose
from covey.core.trace_store import load_trace

STEPS_KEPT = 80
RUNNING_STEP = 10  # Seconds: the 用时 of a running turn moves by this much.
LABEL_CHARS = 80
CHECKPOINT_TITLE = "更新检查点"  # core/trace.py CHECKPOINT_STEP: a command that writes the task's checkpoint.
DETAIL_CHARS = 400
NOTE_KINDS = ("remembered", "voided", "undone", "skipped_ask", "reference", "superseded", "pending")  # The last two carry 不对 too.
NOTES_KEPT = 20
STEP_KINDS = {"读取文件": "read", "浏览目录": "read", "修改文件": "edit", "写入文件": "edit", "执行命令": "command", "后台命令": "command",
              "启动后台命令": "command", "更新检查点": "command", "查找文件": "search", "搜索文件": "search", "分配子任务": "subagent", "子任务": "subagent",
              "读取网页": "web", "搜索网页": "web"}
STEP_STATES = {"running": "running", "done": "done", "failed": "failed", "stopped": "failed"}
AGENT_STATES = {"running": "running", "pendingInit": "running", "completed": "done", "errored": "failed", "interrupted": "failed",
                "cancelled": "failed", "shutdown": "done"}
SUBTASK = "子任务"
UPLOAD_WAIT = 600  # Seconds after its turn's record last changed that an artifact neither up nor failed still reads 上传中 (the upload starts as the turn ends).
NAME_CHARS = 120
ERROR_CHARS = 200
SAID_BY = {"owner": "你说的", "owner_answer": "你说的", "owner_fix": "你说的", "lead": "它总结的"}
RESULT_WORDS = {"完成": "做完了", "失败": "失败了", "中断": "中断了", "已停止": "停住了"}


def step_kind(title) -> str:
    title = str(title or "")
    return STEP_KINDS.get(title) or ("think" if "思考" in title else "other")


def steps_of(snapshot) -> list:
    """The newest STEPS_KEPT steps of the snapshot's main task, oldest first.

    A subtask is one step (data contract v8): the ``agent-<id>`` step the trace keeps for it, with kind ``subagent``, the
    subtask's own words (``agent_words``) and its end from the agent's state; the ``分配子任务`` call that started it is left
    out, as it is the same subtask (one started in the background ends that call in a tenth of a second), not
    子任务：子任务 at 0 and 分配子任务：Agent at 0.1 s."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    steps = snapshot.get("steps")
    if not isinstance(steps, dict):
        return []
    agents = snapshot.get("agents") if isinstance(snapshot.get("agents"), dict) else {}
    ended = snapshot.get("ended_at") if isinstance(snapshot.get("ended_at"), (int, float)) else None
    order = [key for key in (snapshot.get("step_order") or list(steps)) if key in steps]
    out = []
    for key in order:
        step = steps[key]
        if not isinstance(step, dict) or ("agent-" + str(key)) in steps:
            continue
        agent = agents.get(str(key)[len("agent-"):]) if str(key).startswith("agent-") else None
        if isinstance(agent, dict):
            out.append(agent_step(key, step, agent, ended))
            continue
        if step.get("status") == "note":
            continue
        out.append(step_row(key, step))
    return out[-STEPS_KEPT:]


def plain_words(text) -> str:
    """Trace text (escaped for cards by core/text.py safe_text: HTML entities and Markdown backslashes) as plain words on one line."""
    return " ".join(html.unescape(re.sub(r"\\([`*_\[\]\\])", r"\1", str(text or ""))).split())


def step_row(key, step) -> dict:
    """One ordinary step (data contract v18): ``label`` is its title and what it is about, in plain text; ``detail``, only when there
    is more to it, is the command after the model's stated purpose or the whole of a label that was cut, so
    the page neither cuts a long label to its first words nor shows "&amp;" in the command."""
    title = "调用工具" if step.get("title") == "tool" else str(step.get("title") or "")
    words = plain_words(step.get("detail"))
    purpose, cut, command = words.partition(" · ")
    name, more = (purpose, command) if cut and purpose else (words, "")
    if not (cut and purpose) and words and (title in COMMAND_TITLES or title == CHECKPOINT_TITLE):
        # A command its model said nothing of in a field (Codex, an Antigravity call without its summary): a comment it began
        # with, else what it runs past its wrappers and setup lines (core/text.py), never "set -euo pipefail for v in
        # http_proxy…" (2026-11-24 T738); a checkpoint write by its title alone. The whole command is the detail.
        text = html.unescape(re.sub(r"\\([`*_\[\]\\])", r"\1", str(step.get("detail") or "")))
        said = command_purpose(text)
        name = said or ("" if title == CHECKPOINT_TITLE else " ".join(command_core(text).split()))
        more = words if name != words else ""
    label = f"{title}：{name}" if name else title
    if not more and len(label) > LABEL_CHARS:
        more = words
    status = STEP_STATES.get(step.get("status"), "done")
    row = {"id": str(key), "kind": step_kind(step.get("title")), "label": label[:LABEL_CHARS],
           "started_at": step.get("started") if isinstance(step.get("started"), (int, float)) else 0,
           "ended_at": None if status == "running" else step.get("ended") if isinstance(step.get("ended"), (int, float)) else None,
           "status": status}
    if more:
        row["detail"] = more[:DETAIL_CHARS]
    return row


def agent_words(agent) -> str:
    """What a subtask is about: its name unless that is the placeholder (Claude Code names it by the Agent call's
    description, Codex by its path), else the prompt Codex started it with, else 子任务."""
    for value in (agent.get("name"), agent.get("description")):
        words = " ".join(str(value or "").split())
        if words and not words.startswith(SUBTASK):
            return words
    return SUBTASK


def agent_step(key, step, agent, ended) -> dict:
    """The step of one subtask, from its agent state; one still live when the trace has ended ends with it."""
    status = AGENT_STATES.get(agent.get("status"), "done")
    end = agent.get("ended_at") if isinstance(agent.get("ended_at"), (int, float)) else None
    if status == "running" and ended is not None:
        status = "done"
    if status != "running" and end is None:
        end = ended
    start = agent.get("started_at") if isinstance(agent.get("started_at"), (int, float)) else step.get("started")
    return {"id": str(key), "kind": "subagent", "label": f"{SUBTASK}：{agent_words(agent)}"[:LABEL_CHARS],
            "started_at": start if isinstance(start, (int, float)) else 0, "ended_at": None if status == "running" else end, "status": status}


def plan_of(record) -> list:
    path = str((record or {}).get("checkpoint_path") or "")
    if not path:
        return []
    return checkpoint_steps(read_checkpoint(Path(path)))


def figures(record, snapshot, running, now) -> dict:
    """usage, seconds, speed and context of the turn, each only when known."""
    out = {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    seconds = run_seconds(timings_of(record), snapshot, now)
    if seconds is not None:
        out["seconds"] = float(int(seconds // RUNNING_STEP * RUNNING_STEP)) if running else round(seconds, 1)
    if snapshot:
        shown = facts.usage_figures(snapshot)
        counts = shown["shown"]
        have = {name: counts.get(key) for name, key in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_tokens"))
                if type(counts.get(key)) is int}
        if have:
            out["usage"] = {"input": have.get("input", 0), "output": have.get("output", 0), "cache_read": have.get("cache_read", 0),
                            "total": have.get("input", 0) + have.get("output", 0)}
        speed = shown["speed"]
        if isinstance(speed, (int, float)) and speed > 0:
            out["speed"] = round(speed) if running else round(speed, 1)
        used = shown["context"]
        if type(used) is int and used > 0:
            window = shown["window"]
            out["context"] = {"used": used, "limit": window if type(window) is int and window > 0 else 0}
    return out


def ran_with(record, menu=None, topic=None) -> dict:
    """What the turn the job ``record`` made ran with, in the words the launch card names them (data contract v27 ``ran_with``, the
    answer's foot on the page): {harness, provider, model, effort, workspace}. ``menu`` is the launch card's model menu
    (app/launch_models.py available_models; {} or None: the model's own name, core/model_names.py, and no provider); ``topic`` (topic.json) names the Harness and workspace of an older record that
    does not. {} without a Harness."""
    from covey.core.menu_choice import tiers
    from covey.core.model_names import model_name
    record = record if isinstance(record, dict) else {}
    topic = topic if isinstance(topic, dict) else {}
    harness = str(record.get("harness_id") or topic.get("harness_id") or "")
    if not harness:
        return {}
    menu = menu if isinstance(menu, dict) else {}
    model = str(record.get("model") or "")
    entry = menu.get(f"{harness}:{model}") or {}
    harnesses, providers, _ = tiers(menu, harness) if menu else ({}, {}, {})
    out = {"harness": str(harnesses.get(harness) or harness), "provider": str(providers.get(entry.get("provider", ""), "")) if entry else "",
           "model": str(entry.get("display_name") or entry.get("label") or "") or model_name(model) or model,
           "effort": str(record.get("effort") or ""), "workspace": str(record.get("workspace_name") or topic.get("workspace_name") or "")}
    return {key: value for key, value in out.items() if value}


def artifacts_of(state, record, now=None) -> list:
    """The 产物 of the turn the job ``record`` made (data contract v32 ``artifacts``), the files its answer card lists
    (app/artifact_policy.py ``drive_paths``: configuration and credential paths left off) with what their cloud-space upload says
    (core/checkpoint.py ``upload_ledger``): {name} each, plus ``link`` and ``size`` once up, ``error`` when it could not go up,
    ``uploading`` while the upload is under way or, without a ledger, within UPLOAD_WAIT of the record's last change. A file's own name
    only: no local path goes to the page. [] without artifacts or when they cannot be read."""
    from covey.app.artifact_policy import drive_paths
    try:
        if not artifact_paths(record):
            return []
        paths = drive_paths(record)
        ledger = upload_ledger(state, record["key"]) if paths else {}
    except (OSError, ValueError, KeyError, TypeError):
        return []
    files, errors = ledger.get("files"), ledger.get("errors")
    files, errors = files if isinstance(files, dict) else {}, errors if isinstance(errors, dict) else {}
    now = time.time() if now is None else now
    going = ledger.get("status") == "uploading" or (not ledger and now - (epoch_seconds(record.get("updated_at")) or 0) < UPLOAD_WAIT)
    out = []
    for raw in paths:
        key = artifact_key(raw)
        up = files.get(key) if isinstance(files.get(key), dict) else {}
        link = str(up.get("url") or "")
        item = {"name": str(up.get("name") or Path(raw).name or raw)[:NAME_CHARS]}
        if link.startswith("https://"):
            item["link"] = link
            if type(up.get("size")) is int:
                item["size"] = up["size"]
        elif str(errors.get(key) or "").strip():
            item["error"] = " ".join(str(errors[key]).split())[:ERROR_CHARS]
        elif going:
            item["uploading"] = True
        out.append(item)
    return out


def ended_figures(state, record) -> dict:
    """``figures`` of the ended turn the job ``record`` made, its trace read from disk; {} when it cannot be read."""
    try:
        snapshot = load_trace(Path(state), record) or {}
        return figures(record, snapshot, False, time.time())
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# How the page carries out each next step (data contract v33): RECOVER is its ``recover`` operation (app/web_ops.py: the task card's
# own click), SWITCH its own 换配置 (the ``switch`` operation over the topic's ``config``; a project's lead's too since v36), FEISHU
# what only Feishu does (换个模型 of the butler, whose model is on the 设置 card).
RECOVER, SWITCH, FEISHU = "recover", "switch", "feishu"
STOP_AUTO_RETRY = "stop_auto_retry"  # 停止自动重试, the ``recover`` action that cancels a pending automatic retry.


def failure_of(record) -> dict | None:
    """The ``failure`` of the turn the job ``record`` made (core/failure.py failure_view: ``title``, ``detail``, ``actions``), or None
    when it succeeded, waits, runs or the owner stopped it; with ``labels`` (each action's button as the task card says it: continue is
    检查并继续 or 重新投递结果, retry 现在重试), ``how`` (``RECOVER``, ``SWITCH`` or ``FEISHU`` for each action) and, while an automatic
    retry is scheduled, ``auto_retry`` {state, at, attempt, total, kind, now}: ``now`` is the action that runs it at once as the card's
    button does (the scheduled one is then dropped, never both), ``total`` None for a ladder that starts over after progress. No ``now``
    argument: what is pushed does not change by the minute (the page counts down from ``at``)."""
    view = failure.failure_view(record)
    if view is None:
        return None
    actions = list(view["actions"])
    seat = traits_of(record)
    switch = SWITCH if seat.switch_config or seat in (LEAD, LEAD_OFF) else FEISHU
    how = {name: switch if name == failure.SWITCH_MODEL else RECOVER for name in actions}
    labels = {name: failure.label(record, name) for name in actions}
    out = {**view, "labels": labels, "how": how}
    if auto_retry.pending(record):
        kind = auto_retry.kind_of(record)
        now = failure.RETRY if failure.RETRY in actions else failure.CONTINUE
        counted = auto_retry.KINDS[kind].counted or not auto_retry.long_ladder()
        at, attempt = record.get("auto_retry_at"), record.get("auto_retry_attempt")
        out["auto_retry"] = {"state": "pending",
                             "at": round(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
                             "attempt": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None,
                             "total": len(auto_retry.delays_of(kind)) if counted else None, "kind": kind, "now": now}
        labels.setdefault(now, failure.LABELS[failure.RETRY] if now == failure.RETRY else failure.label(record, now))
        labels[STOP_AUTO_RETRY] = "停止自动重试"
        how.setdefault(now, RECOVER)
        how[STOP_AUTO_RETRY] = RECOVER
    return out


def notes_of(record) -> list:
    rows = (record or {}).get("memory_notes")
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and row.get("kind") in NOTE_KINDS and str(row.get("text") or "").strip():
            out.append({"kind": row["kind"], "text": str(row["text"])[:300], "scope": str(row.get("scope") or "")[:40],
                        "entry_id": str(row.get("entry_id") or "")[:40]})
    return out[-NOTES_KEPT:]


# 主目录启动 and the memory a turn carried (data contract v43, docs/web.md): optional, left out when nothing is known.
REPOS_KEPT = 10
MEMORY_KEPT = 40


def topic_fields(topic) -> dict:
    """What a topic item adds of its ``topic.json`` (``topic``): ``repos`` [{name, mode, worktree_tail?, branch?, rules_read}]
    (core/topic_repos.py TopicRepo, the worktree by its last part only) and ``home_start`` true for a topic that started
    in the home. {} for an older topic."""
    topic = topic if isinstance(topic, dict) else {}
    out, repos = {}, []
    for repo in topic.get("repos") if isinstance(topic.get("repos"), list) else ():
        if not isinstance(repo, dict) or not str(repo.get("name") or "").strip():
            continue
        row = {"name": str(repo["name"])[:64], "mode": "write" if repo.get("mode") == "write" else "read",
               "rules_read": bool(repo.get("rules_read_at"))}
        if str(repo.get("worktree") or "").strip():
            row["worktree_tail"] = Path(str(repo["worktree"])).name
        if str(repo.get("branch") or "").strip():
            row["branch"] = str(repo["branch"])[:120]
        repos.append(row)
    if repos:
        out["repos"] = repos[:REPOS_KEPT]
    if topic.get("home_start") is True:
        out["home_start"] = True
    return out


def memory_of(state, record, checkpoint=None) -> dict | None:
    """The turn's ``memory`` {count, used, codes}: how many entries it carried (the record's ``memory_ids``), those it said
    it used as [{code, title}] and the short codes it carried. None for a turn that carried none. What it used is what its
    checkpoint (``checkpoint``, read by the caller once the turn has ended) names in ``memory_used`` (a short code, a ref or an
    older M number, read against the turn's own ``memory_ids``: core/memstore.py find_in_turn, as app/memory_observe.py
    counts it), else what an older record kept in ``memory_used``; each the entry's short code ("" while it has none) and its
    title (the name itself while it cannot be told). ``used`` and ``codes`` are lists of their own (the page draws each code
    with its title, never by position)."""
    from covey.core import memstore

    def code_of(item):  # app/memory_turn.py writes {n: its short code, ref, scope, kind, by}; an older turn a code or an M number.
        if isinstance(item, dict):
            return str(item.get("n") or item.get("ref") or "")
        return str(item)
    carried = [row for row in (record or {}).get("memory_ids") or () if isinstance(row, dict)]
    ids = [code_of(item) for item in (record or {}).get("memory_ids") or () if code_of(item).strip()]
    named = (checkpoint or {}).get("memory_used") if isinstance(checkpoint, dict) else None
    if not isinstance(named, list) or not named:
        named = (record or {}).get("memory_used") or ()
    used = list(dict.fromkeys(str(item).strip() for item in named if isinstance(item, str) and item.strip()))
    if not ids and not used:
        return None

    def one(key):
        try:
            entry = memstore.find_in_turn(state, key, carried) if carried else memstore.find(state, key)
        except (NotImplementedError, OSError, ValueError, KeyError, TypeError):
            entry = None
        code = str(getattr(entry, "code", "") or (key if memstore.is_code(key) else ""))
        return {"code": code, "title": str(getattr(entry, "title", "") or key)[:memstore.TITLE_CHARS]}
    return {"count": len(ids), "used": [one(key) for key in used[:MEMORY_KEPT]],
            "codes": [key for key in ids if memstore.is_code(key)][:MEMORY_KEPT]}


def extras(state, record, running, now=None, snapshot=None) -> dict:
    """The optional fields of the turn the job ``record`` made (its trace read from disk, unless the caller has ``snapshot``)."""
    now = time.time() if now is None else now
    try:
        snapshot = load_trace(Path(state), record) or {} if snapshot is None else snapshot
    except (OSError, ValueError, KeyError, TypeError):
        snapshot = {}
    out = {}
    try:
        steps, plan = steps_of(snapshot), plan_of(record)
        if steps:
            out["steps"] = steps
        if plan and not running and record.get("status") not in WAITS:
            # The turn has ended: a step its checkpoint left ``doing`` was not done, and nothing does it now.
            plan = [{**step, "state": "todo"} if step.get("state") == "doing" else step for step in plan]
        if plan:
            out["plan"] = plan
        out.update(figures(record, snapshot, running, now))
        notes = notes_of(record)
        if notes:
            out["notes"] = notes
        ended = not running and record.get("status") not in WAITS
        cp = _checkpoint(record) if ended else {}
        memory = memory_of(state, record, cp)
        if memory:
            out["memory"] = memory
        if ended:
            out.update(checked(state, record, cp))
    except (OSError, ValueError, KeyError, TypeError):
        return out
    return out


def _checkpoint(record) -> dict:
    """The checkpoint of the turn of ``record`` ({} without one, or one that cannot be read)."""
    path = str(record.get("checkpoint_path") or "")
    try:
        return read_checkpoint(Path(path)) if path else {}
    except (OSError, ValueError, TypeError):
        return {}


def checked(state, record, cp=None) -> dict:
    """What an ended turn's checkpoint says of its topic's 完成标准 and of what it delivered (data contract v34), as its answer
    card has it: ``criteria`` [{item, state, evidence}] for each of the topic's criteria (core/criteria.py, the state its
    checkpoint marked, 没查 for one it did not; without criteria on the topic, the checkpoint's own marks), ``delivery``
    {commit, deployed, verified} (core/checkpoint.py checkpoint_delivery) when the card shows 改动 (``shows_change``: a commit,
    or files changed and a delivery written). Neither when there is nothing to say."""
    if cp is None:
        cp = _checkpoint(record)
    if not cp:
        return {}
    out = {}
    items, marked = criteria.read(state, str(record.get("topic_id") or "")), criteria.marks(cp)
    if items:
        unmet = {item: state_ for state_, item in criteria.unmet(items, cp)}
        evidence = {item: said for item, _, said in marked if said}
        rows = [{"item": item, "state": unmet.get(item, criteria.MET), **({"evidence": evidence[item]} if item in evidence else {})}
                for item in items]
    else:
        rows = [{"item": item, "state": state_, **({"evidence": said} if said else {})} for item, state_, said in marked if item]
    if rows:
        out["criteria"] = rows
    delivery = checkpoint_delivery(cp)
    diff = str(record.get("diff_json_path") or "")
    if delivery and shows_change(delivery, bool(diff) and Path(diff).is_file()):
        out["delivery"] = {name: value for name, value in delivery.items() if value}
    return out


IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
VIDEOS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


def library_kind(row) -> str:
    """A library entry's kind for the page: link, image, video or file (the library's own kinds are file, path, link, delivery)."""
    if row.get("kind") == "link":
        return "link"
    name = str(row.get("name") or row.get("ref") or "").lower()
    return "image" if name.endswith(IMAGES) else "video" if name.endswith(VIDEOS) else "file"


def said_by(source) -> str:
    return SAID_BY.get(str(source or ""), "")


def event_words(text) -> str:
    """A ``thread_end`` receipt ("T34 的任务 #5 完成：…") as the page's grey line ("T34 做完了"); '' for a text it cannot read."""
    head = str(text or "").split("：", 1)[0].split()
    if len(head) >= 4 and head[1] == "的任务" and head[3] in RESULT_WORDS:
        return f"{head[0]} {RESULT_WORDS[head[3]]}"
    return ""

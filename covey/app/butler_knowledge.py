"""The butler's knowledge-entry tools (core/knowledge.py, docs/butler.md 知识库) and history_search, the one
that asks the history command (COVEY_HISTORY_CMD). knowledge_save, knowledge_update, knowledge_drop and
knowledge_search work only with COVEY_KNOWLEDGE=1 and are listed only then (core/butler_tool_specs.py listed);
each takes the entry file's own lock and sends nothing to Feishu (rules 1 and 2).

history_search runs ``<cmd> search [--workspace W] [--since Nd] -- <query>`` as a subprocess of its own in its own
process group, waits HISTORY_TIMEOUT seconds at most (the command has its own 60 s limit; this one is the Bridge's)
and stops the whole group when it is over, reads at most HISTORY_BYTES of its output (the command cuts at the same
size), and answers a command that is missing, not executable, slow or failing in one sentence. It runs on the
main-tools loop, so a slow search holds the butler's other calls for as long: they come one after another anyway.
"""
from __future__ import annotations

import re
import subprocess
import threading

from covey.app.butler_env import reply
from covey.app.butler_tools import workspace_spaces
from covey.core import knowledge
from covey.core.jobs import job_path, read_job_record
from covey.core.process import stop_group

HISTORY_TIMEOUT = 70  # Seconds.
HISTORY_BYTES = 20 * 1024
OFF = "知识条目没有开（这个部署没设 COVEY_KNOWLEDGE=1），这次调用没有执行。"
TOPIC_REF = re.compile(r"[Tt]?(\d+)")
SINCE = re.compile(r"\d{1,4}d")


def _on():
    return None if knowledge.enabled() else reply(False, OFF)


def _workspace(env, name, *, required=False):
    """The registered workspace ``name`` names ('' for none); ValueError with the names to choose from otherwise."""
    name = knowledge.clean_text(name)
    if not name:
        if required:
            raise ValueError("工作区的条目要写是哪个工作区（workspaces_list 里的名字）")
        return ""
    spaces, _ = workspace_spaces(env)
    if name not in spaces:
        raise ValueError(f"没有工作区“{name[:40]}”；可选：" + ("、".join(spaces) or "（没有）"))
    return name


def _source(env, request, topic=""):
    record = read_job_record(job_path(env.state_dir, request.job_key)) if request.job_key else None
    source = {"job_key": request.job_key, **({"job_id": record["job_id"]} if (record or {}).get("job_id") else {})}
    topic = knowledge.clean_text(topic)
    if topic:
        found = TOPIC_REF.fullmatch(topic)
        if not found:
            raise ValueError("source_topic 要写 T 号，如 T483")
        source["topic"] = "T" + found.group(1)
    return source


def knowledge_save(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    scope = args.get("scope")
    workspace = _workspace(env, args.get("workspace"), required=True) if scope == knowledge.WORKSPACE else ""
    entry, new = knowledge.save(env.state_dir, scope, args.get("kind"), args.get("text"), workspace=workspace,
                                why=args.get("why"), source=_source(env, request, args.get("source_topic")))
    if not new:
        return reply(True, f"{knowledge.where_words(entry)}里已有这条（{entry['id']}）：{entry['text']}，没有重复记。",
                     id=entry["id"], new=False)
    return reply(True, f"已记下（{knowledge.row(entry)}）", id=entry["id"], new=True)


def knowledge_update(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    entry = knowledge.update(env.state_dir, args.get("id"), args.get("text"), kind=args.get("kind") or None,
                             why=args.get("why") if "why" in args else None, reason=args.get("reason"),
                             source=_source(env, request))
    return reply(True, f"已改写：{knowledge.row(entry)}（取代 {entry['replaces']}）", id=entry["id"],
                 replaces=entry["replaces"])


def knowledge_drop(env, request):
    if (off := _on()) is not None:
        return off
    entry = knowledge.drop(env.state_dir, request.args.get("id"), request.args.get("reason"), source=_source(env, request))
    return reply(True, f"已作废：{knowledge.row(entry)}", id=entry["id"])


def knowledge_search(env, request):
    if (off := _on()) is not None:
        return off
    args = request.args
    workspace = _workspace(env, args.get("workspace"))
    history = args.get("history") is True
    rows = knowledge.search(env.state_dir, args.get("query"), workspace=workspace or None, history=history)
    if not rows:
        return reply(True, "没有找到匹配的知识条目" + ("" if history else "（加 history: true 也搜已取代、已作废的）") + "。", count=0)
    return reply(True, "\n".join(knowledge.row(entry, state=history) for entry in rows), count=len(rows))


def history_search(env, request):
    command = knowledge.history_cmd()
    if not command:
        return reply(False, "历史搜索没有配置（这个部署没设 COVEY_HISTORY_CMD），这次调用没有执行。")
    args = request.args
    query = knowledge.clean_text(args.get("query"))
    if not query:
        raise ValueError("要给搜索词（query）")
    workspace = knowledge.clean_text(args.get("workspace"))
    since = knowledge.clean_text(args.get("since"))
    if since and not SINCE.fullmatch(since):
        raise ValueError("since 写天数，如 30d")
    argv = [command, "search", *(["--workspace", workspace] if workspace else []), *(["--since", since] if since else []),
            "--", query]
    return run_history(argv, command)


def run_history(argv, command):
    """The answer to running ``argv``: its output, or one sentence on why there is none."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except FileNotFoundError:
        return reply(False, f"历史搜索的命令不存在：{command}。检查 COVEY_HISTORY_CMD。")
    except PermissionError:
        return reply(False, f"历史搜索的命令不能执行：{command}。检查文件权限。")
    except OSError as exc:
        return reply(False, f"历史搜索的命令没能启动（{type(exc).__name__}）：{command}。")
    chunks = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read(HISTORY_BYTES + 1)), daemon=True,
                              name="history-search-read")
    reader.start()
    reader.join(HISTORY_TIMEOUT)
    timed_out = reader.is_alive()
    raw = chunks[0] if chunks else b""
    cut = len(raw) > HISTORY_BYTES
    if timed_out or cut:
        stop_group(proc, 2)  # The whole group: the command's own children too.
    else:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_group(proc, 2)
    reader.join(2)
    raw = chunks[0] if chunks else raw
    proc.stdout.close()
    if timed_out:
        return reply(False, f"历史搜索超过 {HISTORY_TIMEOUT} 秒没有结果，已经停掉：换个更具体的说法，或加 workspace、since 缩小范围再试。")
    text = raw[:HISTORY_BYTES].decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not cut:
        return reply(False, f"历史搜索的命令出错了（退出码 {proc.returncode}）：" + (text[-500:] or "没有输出") + "。")
    if cut:
        text += f"\n\n（输出超过 {HISTORY_BYTES // 1024} KB，已截断；缩小范围再查，或用 workspace、since。）"
    return reply(True, text or "没有找到相关的历史轮次。", truncated=cut)


TOOLS = {"knowledge_save": knowledge_save, "knowledge_update": knowledge_update, "knowledge_drop": knowledge_drop,
         "knowledge_search": knowledge_search, "history_search": history_search}

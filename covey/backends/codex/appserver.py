"""One Codex turn per task over its App Server (run_appserver): open or resume the thread, start the turn, read
it to its end (backends/codex/turn.py) and close the server (backends/codex/rpc.py), with bidirectional input and
typed events.

Only the owning task controls this process. A failed transport is never replayed
automatically: the caller's checkpoint-aware retry owns that decision.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
import time

from covey.backends.codex.events import CodexEventAdapter
from covey.backends.codex.rpc import AppServer, RpcError, failed
from covey.backends.codex.turn import close_turn, logged_words, paused_question_end, read_turn, said_words
from covey.backends.codex.usage import CodexTurnUsage
from covey.backends.codex import consult
from covey.core import repomap, topic_repos
from covey.core.butler_tool_specs import LEAD_ASK_TOOL
from covey.core.failure import said_of
from covey.core.harness import CONSULT, LEAD, LOOK, LOOK_ROOTS, PROXY, READ, TOOL_LIMIT, JobCancelled, RunResult
from covey.core.paths import entry_script
from covey.core.process import device_of
from covey.core.protocol import deliver_event
from covey.core.reply_state import ReplyState
from covey.core.result import is_identifier
from covey.core.usage import ActiveGenerationClock


def dispatch_structured_event(callback, kind, data, rpc):
    # Input/approval handling must retain its failure semantics. Observability
    # failures, however, must never interrupt a backend turn or skip cleanup.
    return deliver_event(callback, kind, data, rpc, logging.getLogger(__name__))


class CodexReplyState(ReplyState):
    error = RpcError  # A failed save is an RpcError, as the app server's callers catch it.


# After the bridge rules: questions and sub-agents on the Codex protocol. What to ask at all is the checkpoint rules' (core/checkpoint.py
# ASK_RULE: only what the owner must decide, asked once at the end); request_user_input pauses the turn until he answers
# (backends/codex/turn.py end_turn), so it is not called a question asked while the work goes on (W0-6, T892).
QUESTION_TOOL = "request_user_input"  # Its model's question tool (backends/codex/bridge.py question_tool).
# The words of CODEX_INSTRUCTIONS, chosen by the turn's TOOL_LIMIT (``codex_instructions``, W2-A): a 协作 ask (CONSULT)
# asks nobody and writes no checkpoint, a project's lead (LEAD) asks with its Bridge tool and has no checkpoint contract
# (C4, G7), a group member's read-only turn (READ) writes no checkpoint (app/tool_limits.py MEMBER_NOTE), a LOOK turn writes
# its checkpoint only through the workspace server's write tool, where it has one (C6).
ASK_CHECKPOINT = ("你通过飞书话题与用户交互。问不问、问什么照执行约定里“什么时候问主人”：要问的留到最后合成一次问，"
                  "按检查点约定写 requires_user=true、question 和可选的 questions；桥接器会先交付完整回复，再创建问题卡片并在回答后续接。")
ASK_TOOL = (f"{QUESTION_TOOL} 也会出问题卡，但它会停下这一轮、等用户回答才接着做，只在不答就做不下去时用"
            f"（不要使用 {QUESTION_TOOL}_async）。不要把未回答视为同意。提问正文支持长文本；避免在普通最终答案中假装等待工具回答。")
SUBAGENTS = ("子 agent 仅执行明确的子任务；最终由主任务汇总结果。结束主任务前必须等待所有子 agent 达到最终状态并收集结果，"
             "不能把仍在运行的子 agent 留到下一轮。")
CODEX_INSTRUCTIONS = ASK_CHECKPOINT + ASK_TOOL + SUBAGENTS
ASK_LEAD = (f"你通过 Bridge 工具和主人、专项的话题打交道，不写检查点。要主人定的事照负责人规则用 Bridge 工具 {LEAD_ASK_TOOL} 问，"
            f"不要用 {QUESTION_TOOL}。不要把未回答视为同意。")
ASK_MEMBER = ("你通过飞书话题与用户交互。这一轮只读，检查点不用写；要主人定的事写在回复里。"
              f"{QUESTION_TOOL} 会停下这一轮等回答，只在不答就做不下去时用（不要使用 {QUESTION_TOOL}_async）。不要把未回答视为同意。")
LOOK_HELD_WRITE = "这一轮只读：检查点文件和本话题产物目录用 bridge_workspace 的 workspace_write_file 写，别处写不了。"
LOOK_NO_WRITE = "这一轮只读、写不了文件：检查点不用写，要主人定的事写在回复里。"


def thread_instructions(ctx) -> str:
    """What a thread's developer instructions say of questions and sub-agents: a thread keeps the ones it started with,
    through every resume, fork and compaction (codex-cli 0.172.0, measured 11-28), so they are the topic's, not its first
    turn's: ASK_LEAD for a lead's (LEAD), CODEX_INSTRUCTIONS for any other. A turn whose limit says otherwise is told
    so in its own input (``turn_instructions``)."""
    return ASK_LEAD + SUBAGENTS if ctx.options.get(TOOL_LIMIT) == LEAD else CODEX_INSTRUCTIONS


TURN_HEAD = "[这一轮的说明：和开头的开发者说明不一样时，以这里为准]"


def turn_instructions(ctx) -> str:
    """``codex_instructions`` of the turn when they are not its thread's (``thread_instructions``: a CONSULT, READ or
    LOOK turn), under TURN_HEAD, for the end of its input; '' otherwise."""
    found = codex_instructions(ctx)
    return "" if found == thread_instructions(ctx) else TURN_HEAD + "\n" + found


def codex_instructions(ctx) -> str:
    """CODEX_INSTRUCTIONS as the turn's TOOL_LIMIT has them: SUBAGENTS alone for CONSULT, ASK_LEAD for LEAD, ASK_MEMBER for
    READ; LOOK with what its gate lets it write first (``look_writes``); every other turn the whole of it."""
    limit = ctx.options.get(TOOL_LIMIT)
    if limit == CONSULT:
        return SUBAGENTS
    if limit == LEAD:
        return ASK_LEAD + SUBAGENTS
    if limit == READ:
        return ASK_MEMBER + SUBAGENTS
    if limit == LOOK:
        return (ASK_CHECKPOINT + LOOK_HELD_WRITE if look_writes(ctx) else LOOK_NO_WRITE) + ASK_TOOL + SUBAGENTS
    return CODEX_INSTRUCTIONS


# A Bridge tool's call may take as long as core/butler_requests.py waits for its answer (120 seconds): Codex gives
# it longer, so the server's own answer (done, or "not answered yet: check before doing it again") comes first.
BRIDGE_TOOL_SECONDS = 150.0


# The turns held to reading (core/harness.py TOOL_LIMIT): the read-only sandbox for the thread and the turn.
READ_ONLY = (READ, LOOK, CONSULT, LEAD)
QUESTION_FLAG = "features.default_mode_request_user_input=true"  # request_user_input (it pauses the turn), on for every turn but CONSULT.
# A 代答 turn's (PROXY) switches: its question as before, and Codex's memories (made from other sessions and injected into each
# new one) neither read into it nor made by its server (keep_out_of_memories keeps its thread out of the others').
PROXY_FLAGS = (QUESTION_FLAG, "features.memories=false", "memories.generate_memories=false", "memories.use_memories=false")


def bridge_servers(ctx):
    """The thread config entries of ``options["mcp_config"]`` (the butler's Bridge tools, core/butler.py mcp_config):
    each server of it as a stdio MCP server of this thread, beside the workspace reader. Empty for every other turn, and
    for a CONSULT one whatever its options say (a 协作 ask never gets the Bridge's tools). In the read-only sandbox (a
    project lead's turn, LEAD) their tools are approved: there Codex asks before an MCP tool runs, and with the approval
    policy ``never`` it refuses every one ("MCP tool call requires approval", codex-cli 0.171.0); its commands stay held."""
    if ctx.options.get(TOOL_LIMIT) == CONSULT:
        return {}
    approve = ctx.options.get(TOOL_LIMIT) in READ_ONLY
    config = {}
    for name, server in ((ctx.options.get("mcp_config") or {}).get("mcpServers") or {}).items():
        prefix = f"mcp_servers.{name}."
        config.update({prefix + "command": server["command"], prefix + "args": list(server.get("args") or ()),
                       prefix + "env": dict(server.get("env") or {}), prefix + "enabled": True,
                       prefix + "tool_timeout_sec": BRIDGE_TOOL_SECONDS,
                       **({prefix + "default_tools_approval_mode": "approve"} if approve else {})})
    return config
def look_writes(ctx) -> list:
    """The workspace server's ``--allow-file``/``--allow-dir`` of a LOOK turn whose writes are held (``options[LOOK_ROOTS]``
    held, app/tool_limits.py look_roots; W1-C, contract C6): the read-only sandbox stays, and the server's
    ``workspace_write_file`` (core/look_write.py) writes the turn's checkpoint file and the topic's outputs directory, nothing
    else. [] for any other turn, and on a device."""
    roots = ctx.options.get(LOOK_ROOTS) or {}
    if ctx.options.get(TOOL_LIMIT) != LOOK or device_of(ctx) or not roots.get("held"):
        return []
    return ["--allow-file", str(roots.get("file") or ""), "--allow-dir", str(roots.get("dir") or "")]


def sandbox_mode(ctx) -> str:
    """The turn's sandbox: read-only for a turn held to READ, LOOK, CONSULT or LEAD (core/harness.py TOOL_LIMIT), else the
    Harness's --sandbox (danger-full-access unless it says otherwise). A 代答 turn keeps it: in a sandbox lark-cli could
    not read the chats (no network, its login not refreshed)."""
    if ctx.options.get(TOOL_LIMIT) in READ_ONLY:
        return "read-only"
    return str(ctx.options.get("sandbox") or "danger-full-access")


def _topic_data(ctx) -> dict:
    try:
        data = json.loads((ctx.history_path.parent / "topic.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def repo_roots(ctx, data=None) -> list:
    """The writable roots a ``workspace-write`` turn gets for the repositories (core/topic_repos.py ``writable_roots``):
    its task's run directory, the topic's worktrees directory (made now, so a worktree ``covey repo use --write`` makes
    this turn is inside it) and each repository the topic writes to (its ``.git``; in place, the repository). Fixed when
    the turn starts: a repository first asked for in this turn is writable from the next. [] when unreadable."""
    data = _topic_data(ctx) if data is None else data
    number = data.get("topic_number") if type(data.get("topic_number")) is int else 0
    # The worktrees directory of a topic that starts in the home or uses repositories: no empty one for every other topic.
    trees = ctx.topic_id and (data.get("home_start") is True or bool(data.get("repos")))
    try:
        repos = repomap.read(ctx.state_dir)
        return topic_repos.writable_roots(data, task_dir=topic_repos.task_dir_of(ctx.state_dir),
                                          worktrees=topic_repos.topic_worktrees(number, ctx.topic_id) if trees else None,
                                          frozen=[repo.path for repo in repos if repo.status != "active"],
                                          inplace=[repo.path for repo in repos if repo.write_mode == "inplace"])
    except Exception:  # noqa: BLE001 - the turn starts with its checkpoints writable, as before
        return []


def reader_roots(ctx, data) -> list:
    """The workspace reader's ``--extra-root`` for the topic's repositories outside its workdir (a worktree under a data
    root that is not in the home): each repository's directory the topic works in."""
    base = str(ctx.workdir).rstrip("/") + "/"
    try:
        return [where for where in dict.fromkeys(topic_repos.where(repo) for repo in topic_repos.of_topic(data))
                if not (where + "/").startswith(base)]
    except Exception:  # noqa: BLE001 - the reader keeps its roots as before
        return []


def thread_params(ctx, session_id, instructions="", process=None):
    """The thread/start, thread/resume or thread/fork parameters of this turn, and the session it
    forks (None unless a new topic forks one). Codex keeps the developer instructions a thread
    started with; they are sent on every turn all the same. On a device (``ctx.device``) the paths are the
    device's: its working directory, its task directory as the writable root and its copy of the workspace
    reader (``process``, the app server's ``DeviceProcess``, pushes and names it)."""
    if device_of(ctx):
        cwd = ctx.remote_workdir
        writable = [str(process.task_dir)]
        mirror = (ctx.options.get("device_spawn") or {}).get("checkpoint") or {}
        if mirror.get("path"):  # The job's checkpoint is outside the task directory (core/devices.py checkpoint_mirror).
            writable.append(str(mirror["path"]).rpartition("/")[0])
        # The task directory's own incoming/, and where the turn's attachments were put on the device (app/device_files.py).
        roots = [str(process.task_dir) + "/incoming", *filter(None, [ctx.options.get("device_incoming")])]
        command, args = process.workspace_reader(cwd, extra_roots=roots)
    else:
        cwd = str(ctx.workdir)
        writable = [str(ctx.state_dir / "checkpoints")]
        data = _topic_data(ctx)
        if sandbox_mode(ctx) == "workspace-write":
            writable += repo_roots(ctx, data)
        command = sys.executable
        # Attachments of this topic are downloaded outside the workspace, and so is
        # the transcript a model switch hands over (app/switch_handoff.py).
        args = ["-B", str(entry_script("covey_workspace_reader.py")), "--root", str(ctx.workdir),
                "--extra-root", str(ctx.history_path.parent / "incoming_media"),
                "--extra-root", str(ctx.history_path.parent / "handoff")]
        for root in reader_roots(ctx, data):
            args += ["--extra-root", root]
        args += look_writes(ctx)
    params = {
        "model": ctx.model,
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": sandbox_mode(ctx),
        "config": {
            **ctx.options.get("thread_config", {}),
            **({"model_reasoning_effort": ctx.effort} if ctx.effort else {}),
            "sandbox_workspace_write.writable_roots": writable,
            "mcp_servers.bridge_workspace.command": command,
            "mcp_servers.bridge_workspace.args": args,
            "mcp_servers.bridge_workspace.enabled": True,
            # Its write tool runs without asking: with the approval policy ``never`` Codex refuses an MCP tool that asks
            # (as the Bridge's tools of a LEAD turn, bridge_servers); where it writes is the server's check.
            **({"mcp_servers.bridge_workspace.default_tools_approval_mode": "approve"} if look_writes(ctx) else {}),
            **({} if device_of(ctx) else bridge_servers(ctx)),
        },
        # Kept from the thread's start (``thread_instructions``): the bridge rules at the system level (app/rules_sent.py).
        "developerInstructions": "\n\n".join(filter(None, (instructions, thread_instructions(ctx)))),
    }
    if ctx.options.get("model_provider"):
        params["modelProvider"] = ctx.options["model_provider"]
    fork = None if session_id else ctx.options.get("fork_session")
    if session_id or fork:
        params["threadId"] = session_id or fork
    return params, fork


def server_flags(ctx) -> list:
    """The app server's -c switches after its thread limit: request_user_input on, as before; a CONSULT turn's
    (backends/codex/consult.py FLAGS) instead: apps, plugins, memories, hooks and the question off; a READ, LOOK or LEAD
    turn's (READING_FLAGS): apps, plugins, memories and hooks off, the question on; a 代答 turn's (``PROXY_FLAGS``): the
    question on and Codex's memories neither read into the turn nor made by this server."""
    limit = ctx.options.get(TOOL_LIMIT)
    flags = (consult.FLAGS if limit == CONSULT else consult.READING_FLAGS if limit in (READ, LOOK, LEAD)
             else PROXY_FLAGS if limit == PROXY else (QUESTION_FLAG,))
    return [part for flag in flags for part in ("-c", flag)]


def keep_out_of_memories(rpc, ctx) -> None:
    """A 代答 turn's thread out of Codex's memories for good (``thread/memoryMode/set`` ``disabled``, experimental, codex-cli
    0.171.0): any other app server or CLI of this CODEX_HOME that makes memories reads only the threads marked ``enabled``,
    and a contact's chat must not become what every other session is told (docs/proxy.md). Set on each of its turns, since
    the threads before this were made enabled; not waited for: the turn goes on whatever the server answers."""
    if ctx.options.get(TOOL_LIMIT) != PROXY or not rpc.thread_id:
        return
    try:
        rpc.post("thread/memoryMode/set", {"threadId": rpc.thread_id, "mode": "disabled"})
    except RpcError:
        pass


def image_paths(ctx, attachments):
    """The paths the model's ``localImage`` inputs name: the files as the Harness sees them. On a device that is the copy
    put there (``device_path``, app/device_files.py); an image that was not put there is left out, never named by the hub's
    path."""
    if device_of(ctx):
        return [item["device_path"] for item in attachments if item.get("device_path")]
    return [item["path"] for item in attachments]


def start_turn(rpc, request, ctx, prompt, session_id):
    """Start the turn on the thread and check that both identities are the ones asked for."""
    reply = rpc.request(
        "turn/start",
        {
            "threadId": rpc.thread_id,
            "input": [{"type": "text", "text": "\n\n".join(filter(None, (prompt, turn_instructions(ctx))))}]
                     + [{"type": "localImage", "path": path} for path in image_paths(ctx, request.inputs.attachments)],
            "model": ctx.model,
            **({"effort": ctx.effort} if ctx.effort else {}),
            # A read-only turn says so for the turn too, whatever the thread resumed with; any other turn sends nothing
            # here, as before (its thread/resume's sandbox holds).
            **({"sandboxPolicy": {"type": "readOnly"}} if ctx.options.get(TOOL_LIMIT) in READ_ONLY else {}),
        },
        timeout=60,
    )
    rpc.turn_id = reply["turn"]["id"]
    if (not is_identifier(rpc.thread_id) or not is_identifier(rpc.turn_id)
            or (session_id is not None and rpc.thread_id != session_id)):
        raise failed("identity_mismatch")


def open_thread(rpc, request, state, observe):
    """Initialize the app server and start, resume or fork the thread; check its identity."""
    ctx, session_id = request.context, request.session_id
    rpc.reply_state = state
    rpc.paused_question_proof = None
    rpc._question_request_id = None
    rpc._paused_question = None
    observe(rpc.process)
    rpc.request("initialize", {"clientInfo": {"name": "feishu_bridge", "version": "1.0"},
                "capabilities": {"experimentalApi": True}})
    rpc.send({"method": "initialized"})
    params, fork = thread_params(ctx, session_id, request.instructions, rpc.process)
    reply = rpc.request("thread/resume" if session_id else "thread/fork" if fork else "thread/start", params, timeout=60)
    rpc.thread_id = reply["thread"]["id"]
    if (not is_identifier(rpc.thread_id) or (session_id is not None and rpc.thread_id != session_id)
            or (fork and rpc.thread_id == fork)):
        raise failed("identity_mismatch")
    request.observers.session(rpc.thread_id)
    state.invalidate()
    keep_out_of_memories(rpc, ctx)


def run_appserver(request, env):
    ctx, prompt, session_id = request.context, request.prompt, request.session_id
    state = CodexReplyState(ctx.options, request.inputs.run_id, request.publish)
    rpc = None
    observe, control, callback = request.observers.process, request.observers.control, request.observers.event
    def event(kind, data, owner):
        return dispatch_structured_event(callback, kind, data, owner)
    adapter = None
    started = time.monotonic()
    turn_usage = CodexTurnUsage()
    generation_clock = None
    status, code, text = "failed", "transport_error", ""
    identity = cleanup = protection = False
    native_question = False
    paused_turn_id = ""
    legacy_exception = None
    try:
        # Bound before anything can fail, so that finally closes the server.
        rpc = AppServer([*ctx.backend_cmd, "app-server", "--stdio", "-c", "agents.max_threads=4", *server_flags(ctx)],
                        ctx.workdir, env, **({"ctx": ctx} if device_of(ctx) else {}))
        rpc.no_questions = ctx.options.get(TOOL_LIMIT) == CONSULT  # A 协作 ask asks nobody (backends/codex/turn.py).
        open_thread(rpc, request, state, observe)
        # Its own thread is never one of its subtasks (CodexEventAdapter.agents).
        adapter = CodexEventAdapter(lambda kind, data: event(kind, data, rpc), rpc.thread_id)
        generation_clock = ActiveGenerationClock()
        first_token_start = generation_clock.last
        start_turn(rpc, request, ctx, prompt, session_id)
        identity = True
        control(rpc)
        status, code, text, native_question, paused_turn_id, finished = read_turn(
            rpc, request, ctx, state, adapter, generation_clock, first_token_start, event, started, turn_usage, text)
        if finished:
            event("turn_finished", {}, rpc)
        if status == "failed" and getattr(rpc, "turn_error", None) is not None:
            # Only the run's log hears Codex's own words (app/runs.py); the card shows them too, secrets hidden (said_words).
            event("backend_failure", {"status": status, "code": code, "stage": "turn",
                                      "stderr": logged_words(rpc.turn_error)}, rpc)
    except JobCancelled as exc:
        status, code, legacy_exception = "cancelled", "cancelled", exc
    except subprocess.TimeoutExpired:
        status, code = "timed_out", "timeout"
        legacy_exception = subprocess.TimeoutExpired("codex app-server", ctx.timeout)
    except RpcError as exc:
        status, code = "failed", "storage_failed" if state.storage_failed else exc.code
        if code == "identity_mismatch":
            identity = False
    except Exception:
        status, code = "failed", "storage_failed" if state.storage_failed else "protocol_error"
    finally:
        if rpc is not None:
            if adapter is not None:  # None before the thread opened: no subtasks to report.
                try:
                    adapter.publish_subtasks(time.time(), force=True)  # Display only, before trace_finished.
                except Exception:
                    pass
            status, code, cleanup, protection = close_turn(rpc, control, event, status, code)
    sid = rpc.thread_id if rpc is not None and rpc.thread_id else session_id
    cancel = request.inputs.cancel_event
    if native_question and status == "completed" and not cancel.is_set():
        refused = paused_question_end(rpc, state, ctx, started, paused_turn_id, identity and cleanup and protection)
        if refused is None:
            return RunResult(text, sid, False)  # Paused question only; never a verified root final.
        status, code = refused
    if legacy_exception is not None and not state.enabled:
        raise legacy_exception
    # trace_finished went out with the controls (close_turn).
    result = state.finish(text, sid, status, code, identity=identity, cleanup=cleanup, protection=protection,
                          cancel_event=cancel)
    said = said_words(getattr(rpc, "turn_error", None)) if rpc is not None and status == "failed" else ""
    if not said and rpc is not None and status == "failed":
        # No turn error said why (the app server ended, a protocol fault): its last error line, else its exit code.
        lines = list(getattr(rpc, "error_lines", None) or ())
        code_of = rpc.process.poll() if getattr(rpc, "process", None) is not None else None
        said = said_of(lines[-1]) if lines else (f"codex app-server {code}，退出码 {code_of}" if type(code_of) is int else "")
    return dataclasses.replace(result, backend_said=said) if said and result.is_error else result

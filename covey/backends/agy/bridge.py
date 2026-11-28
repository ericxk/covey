"""Antigravity (Google's ``agy`` CLI): Gemini and the other models of the owner's Google account, headless.

One turn is one ``agy --output-format stream-json --input-format stream-json`` process: the prompt goes
in as one ``user`` line on stdin, which is then closed, so agy ends after that turn. Its rows are one
``init`` (the conversation id, the native session), ``step_update`` rows (text deltas, tool steps) and one
``result`` with the turn's answer and status. ``--conversation <id>`` continues the topic's conversation.
agy signs in with the Google account it was logged in with on this machine (``agy`` once, interactively);
the Bridge never sees the token. It has no question a headless run can answer, so a task asks through
the checkpoint file like dsh's; stdin takes text only, so images are given by their path (agy opens
them with its own read_file).

agy takes nothing into a running turn (another ``user`` line is a turn of its own, after this one), so a
supplement stops the turn and goes on with it: SIGINT ends agy at once with ``result.error`` "interrupted"
(its conversation keeps what was done; seen on 1.5.13), the commands it left running are stopped,
and the same conversation goes on in a new process with the supplement as its message (AgyControl). The
job's reply is the last process's answer.

The butler's turn (``options["mcp_config"]``, its Bridge tools) runs with a home of its own whose MCP config adds them
(backends/agy/home.py): agy reads MCP servers from one file in the home and from nowhere a turn could name. agy may
still leave some out of what its model gets (its budget for skills and MCP tools); the log says which (``left_out``).

Several Google accounts on this machine (backends/agy/accounts.py, on with two or more in agy-accounts.json), else an
extension's ``agy_turn`` hook (core/extensions.py), may give a turn on this machine the environment its first process
runs with and, when a process ends failed on a spent quota, a limit or a sign-in (SWITCHED), another one to go on in
(``switch``): the same conversation goes on there in a new process, at most MAX_SWITCHES times a turn, with SWITCH_RESUMED
when agy had taken the words in (its ``user_input`` step), else with the same words again, and the switch's line in
执行过程 (each account a HOME of its own; agy keeps a conversation in local files, so another account goes on with it).
A turn that found no account left ends as the accounts say (``settle``: 额度用完, and when the first is back); without
either a turn runs as before.

On a device (``ctx.device``, docs/configuration.md 多设备) the process is the device's (core/process.py
stream_subprocess_lines with the turn's context): the device's own agy and Google sign-in, an environment from its profile,
the conversation kept there. A running turn there takes no supplement (``capabilities``, AgyControl: the SIGINT and the
process tree a supplement needs are this machine's), and its stderr stays on the device. The launch card's models are this
machine's ``agy models``.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from pathlib import Path
import re
import os
import shlex
import signal
import sqlite3
import subprocess
import threading
import time

from covey.backends.agy import accounts
from covey.backends.agy import compaction as agy_compaction
from covey.backends.agy.home import EAGER_ENV, PrivateHome
from covey.core.harness import (BackendConfig, BackendRuntime, HARNESS_REGISTRY, HarnessCapabilities, HarnessSpec,
                                CONSULT, EXTERNAL, JobCancelled, LEAD, READ, TOOL_LIMIT, UUID_RE,
                                for_device)
from covey.core import env as cenv
from covey.core import home as covey_home
from covey.core import extensions, procfs
from covey.core.process import device_of, is_owned, process_identity, stop_group, stream_subprocess_lines
from covey.core.process_env import child_process_env, process_device
from covey.core.reply_state import ReplyState
from covey.core.result import MAX_REPLY_BYTES
from covey.core.steer import SteerRefused, keeps_running, refuse_steer, steer_text
from covey.core.steps import AnswerNotes, Steps
from covey.core.failure import said_of
from covey.core.quota_hold import clock
from covey.core.text import redact, tool_detail
from covey.core.usage import TurnTokens

DEFAULT_MODEL = ""  # agy's own default (its saved /model, else the account's).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STDERR_LINES = 40
MODELS_SECONDS = 60  # How long ``agy models`` may take for the launch catalog.
# What agy's error says when the turn failed for the account, not the task (its own words, matched loosely).
AUTH_RE = re.compile(r"authenticat|sign(ed)? ?in|log(ged)? ?in|unauthori[sz]ed|\b401\b|credential", re.I)
LIMIT_RE = re.compile(r"\b429\b|rate.?limit|resource.?exhausted|too many requests", re.I)
QUOTA_RE = re.compile(r"quota|credits?\b.*(exhausted|insufficient|out)|out of credits|usage limit", re.I)
# A quota that comes back by itself, as Google words one ("Your quota will reset after 2h31m10s", "… per minute", "model
# … is out of quota until <time>"): a limit, retried like one, not a spent one.
WINDOW_RE = re.compile(r"\b(?:reset|refresh)\w*\s+(?:after|in|at|on)\b|\btry again (?:in|after|at)\b|"
                       r"\bretry (?:after|in)\b|per minute|\buntil\s+\d", re.I)
# A passing server fault: Google's status names as agy prints them ("UNAVAILABLE (code 503): …"; upper case only, so
# "internal" in a sentence is none), and the words of an overload or a dropped connection (agy's own "There was a
# network issue connecting to the server, please try again." among them).
FAULT_CODES = re.compile(r"\b(?:UNAVAILABLE|INTERNAL|DEADLINE_EXCEEDED)\b|\(code 50[0234]\)")
FAULT_RE = re.compile(r"\b50[0234] (?:internal|bad gateway|service unavailable|gateway time-?out)|overloaded|"
                      r"high demand|temporarily unavailable|service unavailable|connection reset|\beconnreset\b|"
                      r"broken pipe|unexpected eof|\bgoaway\b|i/o timeout|tls handshake timeout|deadline exceeded|"
                      r"stream (?:error|closed|disconnected|ended)|network (?:issue|error)", re.I)
# A safety block (Gemini's finish reasons and policy words): never a limit or a fault, whatever else the error says.
POLICY_RE = re.compile(r"\bsafety\b|prohibited.?content|blocklist|recitation|\bspii\b|violat|usage polic|"
                       r"content polic|acceptable use|flagged", re.I)
# The model is not the account's, not with this budget, or no longer anyone's (agy 1.5.18: 'invalid model selection
# (--model "gemini-4-neon" --effort "xhigh"): --effort is not supported for model …' once its list lost the model;
# 'unknown model key MODEL_PLACEHOLDER_M535: model not found' resuming a conversation of a model gone). T872.
MODEL_RE = re.compile(r"invalid model selection|unknown model key|model not found", re.I)
LONG_RESET = 6 * 3600  # A quota back later than this is a spent one, not a passing limit (app/auto_retry.py MAX_RESET_WAIT).
RESET_RE = re.compile(r"\b(?:reset|refresh)\w*\s+(?:after|in)\s+((?:\d+\s*[dhms]\s*)+)", re.I)
UNSAID = ("root_failed", "process_exit", "root_incomplete", "output_missing")  # Codes that say nothing of their own.
RETRIED = ("rate_limited", "server_fault")  # What app/auto_retry.py retries, of what error_code says.
NOT_LOGGED_IN = ("Antigravity 还没有登录：在运行机器人的账号下执行一次 agy，选 Google OAuth 登录"
                 "（远程机器上打开它给的链接、把授权码贴回终端），再重试本轮。")
# agy's tools that write or edit a file (1.5: write_file, edit_file; the older names it may still offer beside them).
FILE_WRITES = ["write_file(*)", "edit_file(*)", "write_to_file(*)", "replace_file_content(*)", "multi_replace_file_content(*)"]
# A turn held to reading (core/harness.py LEAD, READ, CONSULT): agy's own deny rules for this run alone (``--settings`` is
# never saved): no file written or edited, no command run, no URL read; its reading tools and its MCP tools (the Bridge's)
# stay. Seen on agy 1.5.14 with --dangerously-skip-permissions: write_file and a shell command were refused ("Matches
# user-configured deny rule"), a Bridge tool answered and a file was read. edit_file and the older names joined in W1-C:
# a LEAD turn could still edit a file through edit_file, and READ and CONSULT had no rule at all (they are refused before
# they start, app/tool_limits.py refusal: the rules are the second wall).
READ_ONLY_SETTINGS = {"permissions": {"deny": [*FILE_WRITES, "command(*)", "read_url(*)"]}}
HELD = (LEAD, READ, CONSULT)
# A turn that starts in the topic's outputs directory (core/home.py external_cwd): agy's file tools reach only its
# workspace (the directory it starts in) without this; what it may do there is still its toolPermission's.
OUTSIDE = {"allowNonWorkspaceAccess": True}

AGY_SPEC = HarnessSpec(
    name="agy", title="Antigravity", prompt_agent_name="Antigravity", idempotency_prefix="agy-lark",
    env_prefix="COVEY_AGY", state_dir_name="agy-bridge", session_filename="agy_session_id",
    session_pattern=UUID_RE,
    default_profile=cenv.get("COVEY_PROFILE", "").strip() or None)  # The Codex Harness's profile.


def native_home():
    """Where agy keeps its settings, conversations and logs."""
    return Path.home() / ".gemini" / "antigravity-cli"


class AntigravitySessionStore:
    def sessions_dir(self, workdir):
        return native_home() / "conversations"

    def transcript(self, workdir, session_id):
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
            return None
        return next(iter(sorted(self.sessions_dir(workdir).glob(f"{session_id}*"))), None)


# agy's tools by the step names the cards know (core/protocol.py STEP_TITLES); any other shows as it is. agy 1.5 renamed some
# (write_file, edit_file); its timer (schedule: wait DurationSeconds, then go on with Prompt), its plan (update_steps) and its
# background commands (manage_task) are named for what they do.
TOOL_NAMES = {"run_command": "bash", "view_file": "read", "read_file": "read", "view_file_outline": "read", "view_code_item": "read",
              "write_to_file": "write", "write_file": "write", "replace_file_content": "edit", "multi_replace_file_content": "edit",
              "edit_file": "edit", "grep_search": "grep", "find_by_name": "glob", "list_dir": "glob", "read_url_content": "webfetch",
              "search_web": "websearch", "invoke_subagent": "agent", "schedule": "定时等待", "update_steps": "更新计划",
              "manage_task": "后台任务"}
# Where agy keeps a conversation under a HOME: one SQLite database each, ``<id>.db`` (``tool_summary``).
CONVERSATIONS = Path(".gemini") / "antigravity-cli" / "conversations"
SUMMARY_ROWS = 8  # Rows before a tool step's own looked at for its call: one model response may call several tools.
SUMMARY_BYTES = 262144  # Of a row's payload, read for its calls.
SUMMARY_TRIES = 64  # JSON objects tried in a row's payload.


def tool_summary(home, conversation, index, parameters):
    """What agy's model said a tool call is for, its ``toolSummary`` (the bridge rules ask for it in the owner's language,
    app/prompts.py LANGUAGE_RULE), which agy's stream-json leaves out of the call's ``parameters``: on 1.5.14 a run_command
    streams its CommandLine alone while the call carries toolSummary and WaitMsBeforeAsync (2026-11-24 T738: 1753 of 1807
    run_command calls in this machine's latest conversations had one). Read from the call as agy keeps it on this machine:
    ``<home>/.gemini/antigravity-cli/conversations/<conversation>.db``, table ``steps``, whose ``idx`` is the stream's
    step_index and whose ``step_payload`` holds the call's arguments as a JSON object; the call is the object there with a
    toolSummary whose values are the streamed ``parameters`` (none streamed: the step's own row only). Read only, with a
    short wait for agy's lock, and only to say what a step is for: None for anything it cannot read or find, a format agy
    no longer has among them, and the step keeps what it streamed."""
    if not home or not isinstance(conversation, str) or not UUID_RE.fullmatch(conversation) or type(index) is not int:
        return None
    path = Path(home) / CONVERSATIONS / f"{conversation}.db"
    wanted = {str(key): value for key, value in (parameters or {}).items()} if isinstance(parameters, dict) else {}
    try:
        if not path.is_file():
            return None
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            rows = db.execute("SELECT step_payload FROM steps WHERE idx BETWEEN ? AND ? ORDER BY idx DESC",
                              (index if not wanted else index - SUMMARY_ROWS, index)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return None
    decoder = json.JSONDecoder()
    for (payload,) in rows:
        if not isinstance(payload, (bytes, bytearray)):
            continue
        text = bytes(payload[:SUMMARY_BYTES]).decode("utf-8", "replace")
        at, tried = text.find('{"'), 0
        while at != -1 and tried < SUMMARY_TRIES:
            tried += 1
            try:
                found = decoder.raw_decode(text, at)[0]
            except ValueError:
                found = None
            said = found.get("toolSummary") if isinstance(found, dict) else None
            if isinstance(said, str) and said.strip() and all(str(found.get(key)) == str(value) for key, value in wanted.items()):
                return " ".join(said.split())[:160]
            at = text.find('{"', at + 2)
    return None


# The MCP servers and their tools as agy's prompt lists them, in each model request it keeps (table gen_metadata): after
# a line "# <server>" the tools it loads up front under "Eager:" and those it names only under "Lazy:" (1.5.16).
SERVERS_OPEN, SERVERS_CLOSE = b"<mcp_servers>", b"</mcp_servers>"
GEN_ROWS = 16  # Of the newest rows, looked through for the latest request: a request's row is followed by small ones.


def listed_in_prompt(block, server):
    """The tools ``block`` (the text of a ``<mcp_servers>`` block) names under the line "# ``server``"; None when it
    has no such line."""
    lines = [line.strip() for line in block.splitlines()]
    if f"# {server}" not in lines:
        return None
    names = set()
    for line in lines[lines.index(f"# {server}") + 1:]:
        if line.startswith("#") or line.startswith("</"):
            break
        if line and line not in ("Eager:", "Lazy:"):
            names.add(line)
    return names


def left_out(home, conversation, listed):
    """Of ``listed`` ({server: [tool]}, what the Bridge's servers listed for the turn, backends/agy/home.py), the tools
    agy did not give its model in the conversation's latest request, {server: [tool]}: neither up front nor by name, all
    of a server's when the request has no line for it (it did not start). Read from
    ``<home>/.gemini/antigravity-cli/conversations/<conversation>.db``, read only; {} for anything it cannot read or
    find, a request without the block among them, and nothing is said."""
    if not home or not listed or not isinstance(conversation, str) or not UUID_RE.fullmatch(conversation):
        return {}
    path = Path(home) / CONVERSATIONS / f"{conversation}.db"
    block = None
    try:
        if not path.is_file():
            return {}
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            for (data,) in db.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT ?", (GEN_ROWS,)):
                at = data.rfind(SERVERS_OPEN) if isinstance(data, (bytes, bytearray)) else -1
                if at >= 0:
                    end = data.find(SERVERS_CLOSE, at)
                    block = bytes(data[at:end if end >= 0 else None]).decode("utf-8", "replace")
                    break
    except (sqlite3.Error, OSError, ValueError):
        return {}
    if block is None:
        return {}
    missing = {}
    for server, names in listed.items():
        named = listed_in_prompt(block, server)
        gone = [name for name in names if named is None or name not in named]
        if gone:
            missing[server] = gone
    return missing


def schedule_words(parameters):
    """A schedule call (agy's timer) as 执行过程 says it: what it comes back to do and when, "查看波次 2C 的结果（3 分钟后）"."""
    prompt = " ".join(str((parameters or {}).get("Prompt") or "").split())
    seconds = (parameters or {}).get("DurationSeconds")
    when = ""
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        when = (f"{round(seconds)} 秒后" if seconds < 90 else f"{round(seconds / 60)} 分钟后" if seconds < 5400
                else f"{seconds / 3600:.1f} 小时后".replace(".0 ", " "))
    return f"{prompt}（{when}）" if prompt and when else prompt or (f"{when}回来接着做" if when else "")
# A tool parameter by the key tool_detail reads.
PARAMETER_KEYS = {"commandline": "command", "command": "command", "absolutepath": "path", "targetfile": "path",
                  "file": "path", "path": "path", "searchpath": "path", "directorypath": "path",
                  "searchdirectory": "path", "query": "query", "pattern": "pattern", "url": "url"}
DETAIL_KEYS = ("command", "path", "pattern", "query", "url")
# Of a step's detail kept: a script's first lines often only set it up (strict mode, the proxy variables, assignments), and
# what it runs (core/text.py command_core) comes after them; at 500 it was cut before it (T738).
DETAIL_CHARS = 4000


def tool_inputs(parameters):
    """agy's parameters (``CommandLine``, ``AbsolutePath``…) under the keys tool_detail reads."""
    if not isinstance(parameters, dict):
        return {}
    found = {}
    for key, value in parameters.items():
        name = PARAMETER_KEYS.get(str(key).replace("_", "").lower())
        if name and name not in found and isinstance(value, (str, int, float)) and str(value).strip():
            found[name] = str(value)
    return found


def usage_counts(usage):
    """(output, uncached input, cache reads) of agy's usage: its ``input_tokens`` leaves the cache reads out
    (``total_tokens`` is input plus output; seen on 1.5.13), its output counts the thinking."""
    if not isinstance(usage, dict):
        return None
    output, uncached, read = (usage.get(k) for k in ("output_tokens", "input_tokens", "cache_read_tokens"))
    if type(output) is not int:
        return None
    return output, uncached, read


class AntigravityEventAdapter:
    """``step_update`` rows to public Harness events. Thinking and tool output are never published."""

    def __init__(self, emit):
        self.emit = emit
        self.steps = Steps(emit)
        self.notes = AnswerNotes(emit)
        # Where the process that runs now keeps its conversations (its HOME), None on a device (``tool_summary``), and the
        # conversation its init row named: a tool step's detail leads with its call's toolSummary when they find it.
        self.home, self.conversation = None, None
        self.summarized = set()  # The step keys whose detail has the call's toolSummary.
        self.texts = {}  # step key -> the text streamed so far
        self.segment = 0  # Which agy process of the turn (a supplement starts the next one).
        self.tokens = TurnTokens()
        self.counted = False  # Whether a model request's usage was added.
        # Whether the process that runs now started its conversation (run_stream sets it for each): agy's result.usage
        # is the whole conversation's, so it is this turn's only then (T776: a butler turn stopped for a supplement
        # before its first request had ended took the conversation's 164M as its own).
        self.fresh = True
        self.first_output = False
        # The turn's AgyControl (run_stream sets it): told when a tool or subagent step starts and ends, so a supplement
        # waits for the running step instead of stopping it (AgyControl.steer).
        self.control = None

    @property
    def tools(self):
        return self.steps.steps

    def accept(self, row, now):
        if row.get("event") == "result":
            if not self.counted and self.fresh:  # No model request said its usage: the turn's total, untimed, no context.
                self.add_usage((row.get("result") or {}).get("usage") if isinstance(row.get("result"), dict) else None,
                               context=False)
            return
        step = row.get("step_update")
        if row.get("event") != "step_update" or not isinstance(step, dict):
            return
        kind, index = step.get("step_type"), step.get("step_index")
        key = f"step-{index}" if not self.segment else f"step-{self.segment}-{index}"
        if kind == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                if not self.first_output:
                    self.first_output = True
                    self.emit("model_output", {"received_at": now})
                self.texts[key] = self.texts.get(key, "") + delta
                if self.texts[key].strip():
                    self.notes.text(key, self.texts[key])
            if step.get("state") == "DONE":
                # One model request: its usage and the time it took (thinking and text, no tool).
                seconds = step.get("duration_seconds")
                self.add_usage(step.get("usage"), seconds if isinstance(seconds, (int, float)) else None)
        elif kind == "system_message":
            self.notes.to_notes(now)  # A subagent came back: what the model wrote before it is a progress note.
        elif kind in ("tool", "subagent"):
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            native = str(step.get("tool_name") or info.get("name") or "tool")
            parameters = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
            if key not in self.steps:
                self.notes.to_notes(now)
                self.steps.start(key, TOOL_NAMES.get(native, native), self.detail(key, native, parameters, index), now)
                if self.control is not None:
                    # The command a shell tool runs (``keeps_running`` looks at it); "" for any other step.
                    command = tool_inputs(parameters).get("command", "") if TOOL_NAMES.get(native) == "bash" else ""
                    self.control.step_started(key, command)
            if step.get("state") == "DONE":
                # A call agy had not written down when it started: its summary is looked for once more.
                again = {} if key in self.summarized else {"detail": self.detail(key, native, parameters, index)}
                self.steps.finish(key, not info.get("error"), now, **again)
                if self.control is not None:
                    self.control.step_done(key)

    def detail(self, key, native, parameters, index):
        """A tool step's detail: a schedule's words (``schedule_words``), else its parameter (core/text.py tool_detail),
        led by what the model said the call is for (``tool_summary``, when it is found)."""
        if native == "schedule":
            return schedule_words(parameters)[:DETAIL_CHARS]
        said = tool_summary(self.home, self.conversation, index, parameters) if self.home else None
        if said:
            self.summarized.add(key)
        return tool_detail(tool_inputs(parameters), DETAIL_KEYS, said or "", said=said or "")[:DETAIL_CHARS]

    def stopped(self, now):
        """The process was stopped for a supplement, or failed and the turn goes on in another environment: its steps
        still running end failed (their command was stopped), what it wrote becomes a note, and the next process's
        steps get keys of their own."""
        for key in self.steps.running():
            self.steps.finish(key, False, now)
        self.notes.to_notes(now)
        self.segment += 1

    def add_usage(self, usage, seconds=None, *, context=True):
        """One request's usage into the turn's counts. ``context``: the request is the root conversation's, so what it
        read and wrote is the context the next request starts from (``context_tokens``, which the butler's compaction
        threshold reads, app/butler.py compact_butler)."""
        counts = usage_counts(usage)
        if counts is not None and self.tokens.add(*counts, 0, seconds=seconds):
            self.counted = True
            size = sum(value for value in counts if type(value) is int) if context else 0
            self.emit("token_usage", self.tokens.payload(**({"context_tokens": size} if size > 0 else {})))


SLUG_RE = re.compile(r"[a-z][a-z0-9]*(?:[-._][a-z0-9]+)+")
DEFAULT_EFFORTS = ("high", "medium")  # The budget a model's card preselects, the first it has.


def models_from_listing(text):
    """The launch catalog of ``agy models``' output, one ``<slug>\t<name>`` a line (1.5.13: ``gemini-4-neon-high\t
    Gemini 4 Neon (High)``). A slug ending in a budget is that budget of its model (``--model gemini-4-neon --effort
    high``, which agy takes and checks), so the card lists the model once with its budgets; one without
    (``gemini-pro-orchestrator``) is a model of its own. Any other line (``Fetching…``, a heading) is skipped."""
    catalog = {}
    for line in str(text or "").splitlines():
        slug, _, name = line.strip().partition("\t")
        slug, name = slug.strip(), name.strip()
        if not SLUG_RE.fullmatch(slug):
            continue
        base, _, effort = slug.rpartition("-")
        if effort in EFFORTS and base:
            row = catalog.setdefault(base, {"display_name": re.sub(r"\s*\([^()]*\)$", "", name) or base,
                                            "supported_reasoning_levels": []})
            if effort not in [level["effort"] for level in row["supported_reasoning_levels"]]:
                row["supported_reasoning_levels"].append({"effort": effort})
        else:
            catalog.setdefault(slug, {"display_name": name or slug, "supported_reasoning_levels": []})
    for row in catalog.values():
        row["supported_reasoning_levels"].sort(key=lambda level: EFFORTS.index(level["effort"]))
        levels = [level["effort"] for level in row["supported_reasoning_levels"]]
        default = next((effort for effort in DEFAULT_EFFORTS if effort in levels), levels[0] if levels else "")
        if default:
            row["default_reasoning_level"] = default
    return catalog


class AntigravityBackend(BackendRuntime):
    def add_cli_args(self, parser):
        parser.add_argument("--agy-cmd", default="agy")
        parser.add_argument("--agy-timeout", type=int, default=0)
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--reasoning-effort", default="")
        parser.add_argument("--no-skip-permissions", dest="skip_permissions", action="store_false")

    def build_config(self, args):
        return BackendConfig(args.model, args.reasoning_effort, args.agy_timeout, shlex.split(args.agy_cmd),
                             args.skip_permissions, {"harness_id": "agy"})

    def capabilities(self, ctx):
        # A supplement stops agy with SIGINT and looks for what it left in this machine's process tree (AgyControl): on a
        # device, whose program passes on TERM and KILL only, a running turn takes none (the next turn takes the words).
        # rate_limit_retry, fault_retry: what stream_outcome tells apart in agy's error. bridge_tools: the butler's tools through
        # a home of its own (backends/agy/home.py), a lead's turn held to reading by READ_ONLY_SETTINGS. ask_once:
        # backends/agy/judge.py (this machine's only: DEVICE_OFF).
        # compaction: agy's own /compact (backends/agy/compaction.py), on this machine's conversations only.
        # external_cwd: no setting keeps the home's AGENTS.md out (agy 1.5.19 refuses memoryRulesDisabled and the like), so a
        # topic that keeps off this machine's files starts in its outputs directory (core/home.py external_cwd).
        # command_stop: AgyControl.stop_command stops the commands agy runs (the same SIGINT), on this machine's only.
        here = not device_of(ctx)
        # system_instructions: the bridge rules in the private home's ~/.gemini/GEMINI.md, read again at every turn
        # (``Homes``, backends/agy/home.py), on this machine only.
        return for_device(HarnessCapabilities(steering=here, steer_reads=here, command_stop=here, bridge_tools=True,
                                              rate_limit_retry=True, fault_retry=True, ask_once=True, compaction=here,
                                              external_cwd=True, system_instructions=here,
                                              system_refresh="turn" if here else ""), ctx)

    # The butler's idle compaction (app/idle_compaction.py): how long after a turn it starts, and that it runs with the
    # butler's tools (the turn's MCP servers), so its request starts with the same prompt as the turn and reads it from
    # the cache (backends/agy/compaction.py).
    compact_idle_seconds = agy_compaction.COMPACT_IDLE_SECONDS
    compact_with_tools = True

    def compact_session(self, request):
        """``/compact`` on ``request.session_id`` and nothing else (the butler's idle compaction), in a turn's
        environment: the agy_turn hook's and, with ``mcp_config``, the butler's home."""
        ctx = request.context
        if device_of(ctx) or not request.session_id:
            return False  # A device's conversation is on the device.
        self.normalize_model_settings(ctx, ctx.model, ctx.effort)
        argv, env = self.argv(ctx, request.session_id), self.environment(ctx)
        homes = Homes(ctx, request.instructions)  # The rules too: its request starts as the turn's does.
        try:
            with extension_turn(ctx, env, run_id=request.inputs.run_id) as (env, _switch):
                done, _usage = agy_compaction.compact(ctx, argv, homes.env(env), request.session_id,
                                                      cancel_event=request.inputs.cancel_event,
                                                      process_observer=request.observers.process)
                return done
        finally:
            homes.close()

    def ask_once(self, ctx, system, data, *, limit, cwd=None, follow="", effort=""):
        """One question to agy and nothing else (backends/agy/judge.py): the answer's text, or AskFailed with the reason
        ``error_code`` reads of its error. ``follow``, ``effort``: the model and budget of the 辅助模型's setting."""
        from covey.backends.agy.judge import ask_once
        return ask_once(self, ctx, system, data, limit=limit, cwd=cwd, settings=READ_ONLY_SETTINGS,
                        follow=follow, effort=effort, classify=error_code)

    def ask_model(self, ctx, *, follow=""):
        """The model id ``ask_once`` would run on (backends/agy/judge.py model_of); '' for agy's own default, which only
        agy knows. Nothing is started."""
        from covey.backends.agy.judge import model_of
        return model_of(self, ctx, follow=follow)

    def environment(self, ctx):
        # COVEY_HOME, COVEY_TOPIC, COVEY_STATE and <数据根>/bin on PATH (core/home.py), none on a device; ``PWD`` where its
        # process starts (core/home.py external_cwd).
        where = Path(covey_home.external_cwd(ctx)) if ctx.options.get(EXTERNAL) else ctx.workdir
        return {**child_process_env(where, device=process_device(ctx)), **covey_home.env(ctx)}

    def start_refusal(self, ctx):
        """(error code, words, reset) when a turn on ``ctx.model`` would only fail, refused before it starts
        (app/runs.py): its group's quota held (accounts.holding, a spent one's reset said by an earlier turn), or a model
        the saved list (``model_catalog``) no longer has. None otherwise, for agy's own default, on a device, and on any
        doubt: the turn then starts and fails as it would."""
        if not ctx.model or device_of(ctx):
            return None
        try:
            found = None if accounts.configured() else accounts.holding(ctx.state_dir, ctx.model)
            if found is not None:
                return "quota_exhausted", failure_words(ctx, "quota_exhausted", "", found["reset_at"], time.time()), \
                    float(found["reset_at"])
            catalog = self.model_catalog(ctx)
            if catalog and ctx.model not in catalog:
                return ("model_unavailable", f"Antigravity 的模型列表里已经没有 {ctx.model}（账号没有它的权限，或它已下线），"
                        "这一轮没有开始；换个模型接着做", 0.0)
        except Exception:  # noqa: BLE001 - a check that fails never stops a turn
            logging.getLogger(__name__).warning("agy start check failed", exc_info=True)
        return None

    def model_catalog(self, ctx):
        path = Path(ctx.state_dir) / "launch-models" / "agy.json"
        try:
            saved = json.loads(path.read_text()).get("models") or {}
        except (OSError, ValueError, AttributeError):
            return {}
        return {model: {"display_name": row.get("display_name") or model,
                        "supported_reasoning_levels": [{"effort": e} for e in row.get("levels") or ()],
                        **({"default_reasoning_level": row["default"]} if row.get("default") else {})}
                for model, row in saved.items() if isinstance(row, dict)}

    def refresh_model_catalog(self, ctx):
        """The models ``agy models`` lists for the signed-in account; none (not signed in, not installed) raises."""
        try:
            done = subprocess.run([*ctx.backend_cmd, "models"], cwd=str(ctx.workdir), env=self.environment(ctx),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=MODELS_SECONDS,
                                  start_new_session=True, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("agy models 没有回应") from None
        except OSError as exc:
            raise ValueError(f"agy 不能运行：{exc.strerror or exc}") from None
        if done.returncode != 0:
            raise ValueError(NOT_LOGGED_IN if AUTH_RE.search(done.stderr + done.stdout) else
                             f"agy models 失败（退出码 {done.returncode}）")
        catalog = models_from_listing(done.stdout)
        if not catalog:
            raise ValueError("agy models 没有列出模型")
        return catalog

    def normalize_model_settings(self, ctx, model, effort):
        """An empty model is agy's own default; a budget agy does not know never reaches it, nor one the saved
        catalog says the model lacks (agy refuses those itself, after starting)."""
        if effort and effort not in EFFORTS:
            raise ValueError("Antigravity 不支持所选推理档位")
        levels = self.levels(ctx, model)
        if effort and levels and effort not in levels:
            raise ValueError("此模型不支持所选推理档位")
        return model, effort

    def levels(self, ctx, model):
        """The budgets the saved catalog lists for ``model``; None when the catalog does not know it."""
        row = self.model_catalog(ctx).get(model) if model else None
        return None if row is None else [level["effort"] for level in row["supported_reasoning_levels"]]

    def argv(self, ctx, session_id):
        # A model the catalog lists without budgets takes no --effort (agy refuses one).
        effort = "" if self.levels(ctx, ctx.model) == [] else ctx.effort
        return [*ctx.backend_cmd, "--output-format", "stream-json", "--input-format", "stream-json",
                "--disable-slash-commands",
                *(["--dangerously-skip-permissions"] if ctx.skip_permissions else []),
                *(["--conversation", session_id] if session_id else []),
                *(["--model", ctx.model] if ctx.model else []),
                *(["--effort", effort] if effort else []),
                *settings_args(ctx)]

    def run(self, request):
        request.check_cancelled()
        ctx = request.context
        self.normalize_model_settings(ctx, ctx.model, ctx.effort)
        options = {**ctx.options, "harness_id": "agy", "harness_label": "Antigravity",
                   "error_texts": {**ctx.options.get("error_texts", {}), "authentication_error": NOT_LOGGED_IN}}
        request = dataclasses.replace(request, context=dataclasses.replace(ctx, options=options))
        stdin = json.dumps({"event": "user", "message": {"content": request.prompt}}, ensure_ascii=False) + "\n"
        argv, env = self.argv(ctx, request.session_id), self.environment(ctx)
        homes = Homes(ctx, request.instructions)
        try:
            with extension_turn(ctx, env, run_id=request.inputs.run_id) as (env, switch):
                result = run_stream(request, argv, stdin, homes.env(env),
                                    switch=None if switch is None else lambda failure: homes.switched(switch(failure)))
                homes.check(result.session_id)
                settle = getattr(switch, "settle", None)
                return settle(result) if callable(settle) else result
        finally:
            homes.close()

    def prompt_runtime_note(self, ctx):
        if ctx.options.get(TOOL_LIMIT) == LEAD:
            return "Antigravity 权限模式：只读，这一轮写文件、执行命令和读网址都会被拒绝，Bridge 工具照常可用"
        return ("Antigravity 权限模式：always-proceed（--dangerously-skip-permissions）" if ctx.skip_permissions
                else "Antigravity 权限模式：request-review，要批准的操作会被跳过")

    def startup_lines(self, ctx):
        return [f"agy_cmd={ctx.backend_cmd} model={ctx.model or '(agy default)'} effort={ctx.effort or '(default)'} "
                f"home={native_home()}"]


def settings_args(ctx) -> list:
    """agy's ``--settings`` of a turn: READ_ONLY_SETTINGS for one held to reading (LEAD, READ, CONSULT), none for any
    other (as before); with ``OUTSIDE`` too for a turn that starts outside its workdir (a topic that
    keeps off this machine's files, core/home.py external_cwd), so its file tools still reach a repository it is given."""
    limit = ctx.options.get(TOOL_LIMIT)
    found = READ_ONLY_SETTINGS if limit in HELD else None
    if ctx.options.get(EXTERNAL) and covey_home.external_cwd(ctx) != str(ctx.workdir):
        found = {**(found or {}), **OUTSIDE}
    return ["--settings", json.dumps(found, separators=(",", ":"))] if found else []


def home_config(ctx) -> dict | None:
    """The --mcp-config of a turn's private home (``Homes``): the butler's or a lead's Bridge tools (``mcp_config``); None
    when there are none (no private home)."""
    servers = dict(((ctx.options.get("mcp_config") or {}).get("mcpServers") or {}))
    return {"mcpServers": servers} if servers else None


@contextlib.contextmanager
def extension_turn(ctx, env, *, run_id=""):
    """``(env, switch)`` of a turn on this machine: the accounts' (backends/agy/accounts.py) when two or more are set up,
    else the agy_turn hook's (core/extensions.py), else ``(env, None)``. A device's turn runs with the device's own agy
    and sign-in, so it has none. The accounts' switch has a ``settle`` for the turn's result."""
    if device_of(ctx):
        yield env, None
        return
    if accounts.configured():
        with accounts.turn(ctx, env, run_id=run_id) as found:
            yield found
        return
    hook = extensions.hook("agy_turn")
    if hook is None:
        yield env, None
        return
    with hook.turn(ctx, env, run_id=run_id) as found:
        yield found


class Homes:
    """The butler's turn (``options["mcp_config"]``) and every turn
    with the bridge rules at the system level (``rules``, RunRequest.instructions: they go in the home's GEMINI.md) run each
    agy process in a home of its own (backends/agy/home.py) over its environment's HOME: made again when the turn goes on
    in another environment, removed with the turn. Any other turn (a device's) runs in the environment as it is."""

    def __init__(self, ctx, rules=""):
        self.ctx, self.home, self.rules = ctx, None, "" if device_of(ctx) else str(rules or "")

    def env(self, env):
        """``env`` for the next process, the home of the one before removed."""
        self.close()
        config = home_config(self.ctx)
        if not config and not self.rules:
            return env
        # The same name at each turn of the topic, so the prompt agy starts it with reads from the cache.
        home = PrivateHome(self.ctx.state_dir, env.get("HOME") or Path.home(), config, env, key=self.ctx.topic_id,
                           rules=self.rules)
        path = home.__enter__()
        self.home = home
        if home.note:
            logging.getLogger(__name__).warning("agy private home: %s", home.note)
        return {**env, "HOME": str(path)}

    def switched(self, found):
        """The hook's ``(env, note)`` with this turn's home over ``env``; None stays None."""
        if found is None:
            return None
        env, note = found
        return self.env(env), note

    def check(self, conversation):
        """Say in the log which of the Bridge's tools agy left out of ``conversation``'s latest request (``left_out``):
        the turn ran without them, and its model could not know they were there."""
        home = self.home
        if home is None or device_of(self.ctx) or not home.listed:
            return
        try:
            missing = left_out(home.home, conversation, home.listed)
        except Exception:  # Only a note: the turn's result stands.
            logging.getLogger(__name__).debug("agy tools check failed", exc_info=True)
            return
        for server, names in missing.items():
            logging.getLogger(__name__).warning(
                "agy left %d of the %s server's %d tools out of conversation %s: %s. agy fits a turn's skills and MCP "
                "tools in one budget and drops the largest first; a lower %s lists more of them by name only "
                "(backends/agy/home.py), fewer skills leave more room", len(names), server, len(home.listed[server]),
                conversation, ", ".join(names), EAGER_ENV)

    def close(self):
        home, self.home = self.home, None
        if home is not None:
            home.__exit__(None, None, None)


class StreamRows:
    """What one run printed, fed line by line by the shared reader."""

    def __init__(self, request, adapter, expected_session, control=None, *, resumed=False):
        self.request, self.adapter, self.expected_session = request, adapter, expected_session
        self.control, self.resumed = control, resumed
        self.sid = self.result = None
        self.invalid = False
        self.process = None
        self.kinds = {}  # step_index -> step_type, of the root conversation's steps
        self.texts = {}  # step_index -> what an agent_response step wrote

    def reply(self):
        """The turn's answer: what the model wrote after its last step that was not writing (a tool, a subagent, a
        system message such as a subagent's return). agy's ``result.response`` joins everything the model wrote in
        the turn: the progress lines GEMINI.md asks for and the "waiting for the subagents" lines before the answer.
        '' when the answer is not the end of ``response`` (then ``response`` is taken as it is)."""
        response = str((self.result or {}).get("response") or "").strip()
        last = max((index for index, kind in self.kinds.items() if kind != "agent_response"), default=-1)
        answer = "".join(self.texts[index] for index in sorted(self.texts) if index > last).strip()
        return answer if answer and response.endswith(answer) else response

    def observe(self, p):
        self.process = p
        if self.control is not None:
            self.control.started(p)
        try:
            self.request.observers.process(p)
        except Exception:
            pass

    def on_line(self, line):
        # Never raise here: the shared reader still owns draining and reaping the process.
        try:
            row = json.loads(line)
        except ValueError:
            self.invalid = True
            return
        if not isinstance(row, dict):
            self.invalid = True
            return
        kind = row.get("event")
        if kind == "init":
            init = row.get("init") if isinstance(row.get("init"), dict) else {}
            value = row.get("conversation_id") or init.get("conversation_id")
            if (self.sid is not None or not isinstance(value, str) or not UUID_RE.fullmatch(value)
                    or (self.expected_session is not None and value != self.expected_session)):
                self.invalid = True
                return
            self.sid = value
            self.adapter.conversation = value
            if self.control is not None:
                with self.control.lock:
                    self.control.ready = not self.control.closing
            if not self.resumed:  # A process after a supplement goes on with the conversation already reported.
                try:
                    self.request.observers.session(value)
                except Exception:
                    pass
            return
        if kind == "result":
            result = row.get("result")
            if self.result is not None or not isinstance(result, dict):
                self.invalid = True
                return
            if self.sid is not None and result.get("conversation_id") not in (None, "", self.sid):
                self.invalid = True
                return
            self.result = result
        elif kind == "step_update":
            step = row.get("step_update")
            if self.sid is None or self.result is not None or not isinstance(step, dict):
                self.invalid = True
                return
            if step.get("conversation_id") not in (None, self.sid):
                # A subagent's steps carry their own conversation; never the root's answer.
                return
            index = step.get("step_index")
            if type(index) is int:
                self.kinds[index] = str(step.get("step_type") or "")
                if self.kinds[index] == "agent_response" and isinstance(step.get("text_delta"), str):
                    self.texts[index] = self.texts.get(index, "") + step["text_delta"]
        try:
            self.adapter.accept(row, time.time())
        except Exception:
            pass  # Observability never decides the outcome.


def error_code(error):
    """The failure code agy's ``result.error`` names, '' for none of these: a login (never retried), a safety block
    (``policy_refusal``), a model the account cannot use (``model_unavailable``), a spent quota (never retried), a limit
    or a quota that resets by itself (``rate_limited``), a passing server fault (``server_fault``); the last two are
    what app/auto_retry.py retries."""
    if AUTH_RE.search(error):
        return "authentication_error"
    if POLICY_RE.search(error):
        return "policy_refusal"
    if MODEL_RE.search(error):
        return "model_unavailable"
    if QUOTA_RE.search(error) and not WINDOW_RE.search(error):
        return "quota_exhausted"
    if LIMIT_RE.search(error) or QUOTA_RE.search(error):
        return "rate_limited"
    if FAULT_CODES.search(error) or FAULT_RE.search(error):
        return "server_fault"
    return ""


def stream_outcome(rows, rc, cancel_event):
    """(status, code) of a run whose output the reader has drained; ("completed", "") when the result's
    ``response`` is the reply."""
    result = rows.result or {}
    status, error = result.get("status"), str(result.get("error") or "")
    if rows.invalid:
        return "failed", "protocol_error"
    if status == "ERROR" or (rows.result is None and type(rc) is int and rc != 0):
        code = error_code(error)
        if code in RETRIED and cancel_event is not None and cancel_event.is_set():
            return "cancelled", "cancelled"  # The stop's own end (a cut stream, a closed connection): no fault.
        if code:
            return "failed", code
    if status in ("CANCELED", "INTERRUPTED") and cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled"
    if type(rc) is not int or rc != 0:
        return "failed", "process_exit" if rows.result is None else "root_failed"
    if not (rows.sid and rows.result is not None):
        return "failed", "root_incomplete"
    if status != "SUCCESS":
        return "failed", "root_failed"
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled", "cancelled"
    text = result.get("response")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_REPLY_BYTES:
        return "failed", "output_missing"
    return "completed", ""


def stderr_lines(stderr):
    """agy's last lines of stderr for the failure log, credentials hidden and its sign-in URL left out."""
    lines = [line for line in str(stderr or "").splitlines() if line.strip() and "oauth2" not in line]
    return [redact(line)[:400] for line in lines[-STDERR_LINES:]]


def auth_failure(stderr):
    """Whether agy stopped before its turn for want of a login (it prints its sign-in URL on stderr)."""
    return bool(AUTH_RE.search(stderr or ""))


# The last reply is the turn's whole answer: StreamRows.reply() takes only what the last process wrote.
WHOLE_REPLY = "最后的回复要写完整的结论，不要只写补充之后的部分。"
RESUMED = ("用户补充（你刚才的执行被这条补充打断了，已经做完的仍然有效，停下来的命令没有跑完；"
           f"先看补充，再决定怎么接着做。{WHOLE_REPLY}）：\n")
# A supplement that waited for the running step to end (AgyControl.step_done): nothing was cut off.
BETWEEN_RESUMED = ("用户补充（上一步做完后停下来读这条补充，已经做完的仍然有效；"
                   f"先看补充，再决定怎么接着做。{WHOLE_REPLY}）：\n")
STOP_SECONDS = 5  # How long the commands a stopped turn left running get to exit before they are killed.
# A process that failed as one of these may go on in another environment, when the agy_turn hook gives one (``switch``).
SWITCHED = ("quota_exhausted", "rate_limited", "authentication_error")
MAX_SWITCHES = 3  # How many times one turn goes on in another environment.
SWITCH_RESUMED = f"（刚才的执行因为模型额度或登录的问题停下了，已经做完的仍然有效；接着完成上面的要求。{WHOLE_REPLY}）"
SWITCH_NOTE = "换个环境接着做"  # The line in 执行过程 when the hook gives none.


def descendants(root):
    """The identities of this user's processes under ``root`` now (agy's commands and helpers)."""
    parents = {}
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            row = procfs.stat(entry.name)
            if row is not None:
                parents[int(entry.name)] = row.ppid
    found, frontier = [], [root]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent:
                identity = process_identity(pid)
                if identity is not None:
                    found.append(identity)
                    frontier.append(pid)
    return found


def stop_left(identities):
    """Stop the processes of ``identities`` still alive, each checked against its identity first (never a
    recycled PID): TERM, STOP_SECONDS, then KILL."""
    alive = [identity for identity in identities if is_owned(identity)]
    for signum in (signal.SIGTERM, signal.SIGKILL):
        for identity in alive:
            if is_owned(identity):
                try:
                    os.kill(int(identity["pid"]), signum)
                except OSError:
                    pass
        deadline = time.monotonic() + STOP_SECONDS
        while signum == signal.SIGTERM and time.monotonic() < deadline and any(map(is_owned, alive)):
            time.sleep(0.1)


class AgyControl:
    """The task's controls: a supplement is held and is the next process's message. While a tool or subagent step runs
    it waits for the step to end (``step_done``), then stops the running agy (SIGINT); with no step running it stops it
    at once. ``stop_command`` stops the step's command for an urgent one. Writes are never replayed (rule 6): an
    accepted supplement is held here until a process takes it. On a device (``device``) there is no supplement:
    ``supports_steer`` is false, so app/steering.py sends none."""
    native_blocking_questions = False
    supports_steer = True
    awaiting_answer = False

    def __init__(self, emit, *, device=False):
        self.emit = emit
        self.device = bool(device)
        if self.device:
            self.supports_steer = False
        self.user_requests = set()
        self.lock = threading.Lock()
        self.process = None
        self.ready = False  # The running process said its conversation (init).
        self.closing = False
        self.pending = []  # (text, event id), until a process takes them
        self.reaper = None  # The thread that stops what a stopped process left running, once it has exited.
        self.stopping = False
        self.steps = {}  # The running process's tool and subagent steps still running: step key -> its command ("" none).
        self.cut = False  # The process was stopped while a step ran or at once (not between two steps): RESUMED says so.
        self.note = ""  # What stop_command tells the model of why its command ended, ahead of the supplements.

    def steer(self, text, event_id):
        if self.device:
            raise SteerRefused("设备上的 Antigravity 不能在执行中补充，请发送下一轮要求")
        with self.lock:
            refuse_steer(text, closing=self.closing, ready=self.ready or self.stopping, label="Antigravity")
            self.pending.append((steer_text(text), event_id))
            if self.stopping or self.steps:
                # Already stopping for an earlier one: both go on together. A step runs: its end (step_done) stops the
                # process, so a command such as the whole test run is not cut off by a supplement.
                return
            self.interrupt(cut=True)

    def interrupt(self, *, cut):
        """Stop the running agy with SIGINT, its reaper after it. Under ``self.lock``. Nothing when there is none
        running (between two processes: the next one takes the supplements)."""
        process = self.process
        if self.stopping or process is None or process.poll() is not None:
            return False
        self.stopping = True
        self.cut = self.cut or cut
        left = descendants(process.pid)
        try:
            os.kill(process.pid, signal.SIGINT)
        except OSError:
            pass
        # Once agy has exited: a command it left may hold its output open, so this cannot wait for its end.
        self.reaper = threading.Thread(target=self.reap, args=(process, left), daemon=True)
        self.reaper.start()
        return True

    def step_started(self, key, command=""):
        with self.lock:
            self.steps[key] = str(command or "")

    def step_done(self, key):
        """A step ended. The last one running with a supplement held stops the process, once (``stopping``)."""
        with self.lock:
            self.steps.pop(key, None)
            if self.steps or not self.pending or self.closing:
                return
            self.interrupt(cut=False)

    def stop_command(self, note=""):
        """Stop the command the running step is, so the supplements held are read now (app/steer_interrupt.py): the
        same SIGINT as a supplement at once, ``note`` (why the command ended) ahead of them in the next process's
        message. Returns {"stopped": [commands]}, {"kept": command} for one never stopped (core/steer.py
        ``keeps_running``), or {"reason": why nothing was stopped}."""
        with self.lock:
            if self.device:
                return {"reason": "任务在设备上运行，不能从这里停命令"}
            if self.closing:
                return {"reason": "任务正在收尾"}
            if self.stopping:
                return {"reason": "任务正在停下来读补充"}
            if not self.steps:
                return {"reason": "模型现在没有在等命令"}
            commands = [command for command in self.steps.values() if command]
            if not commands:
                return {"reason": "模型现在在等子任务，不是命令"}
            kept = next((command for command in commands if keeps_running(command)), None)
            if kept is not None:
                return {"kept": kept}
            if not self.pending:
                return {"reason": "没有等着读的补充"}
            if not self.interrupt(cut=True):
                return {"reason": "任务进程已经结束"}
            if note:
                self.note = steer_text(note)
        return {"stopped": commands}

    @staticmethod
    def reap(process, left):
        """Until agy has exited, keep adding what runs under it (a command it was starting as it was stopped), then
        stop what is left; one that will not exit in STOP_SECONDS * 6 is stopped with its group."""
        seen = {identity["pid"]: identity for identity in left}
        deadline = time.monotonic() + STOP_SECONDS * 6
        while process.poll() is None and time.monotonic() < deadline:
            seen.update((identity["pid"], identity) for identity in descendants(process.pid))
            time.sleep(0.05)
        if process.poll() is None:
            stop_group(process, STOP_SECONDS)
        stop_left(list(seen.values()))

    def take(self):
        """The held supplements as the next process's message and their event ids; ('', []) when none. Taken when
        the task ends (stopped, closed) they are not read: no ``steer_read`` follows, so app/steering.py
        end_unread_steers tells the owner of each."""
        with self.lock:
            reaper, self.reaper = self.reaper, None
        if reaper is not None:
            reaper.join()
        with self.lock:
            pending, self.pending = self.pending, []
            cut, note, self.cut, self.note = self.cut, self.note, False, ""
            self.stopping, self.ready = False, False
            self.steps.clear()
        if not pending:
            return "", []
        words = "\n\n".join(text for text, _ in pending)
        return (RESUMED if cut else BETWEEN_RESUMED) + (note + "\n\n" if note else "") + words, [event_id for _, event_id in pending]

    def started(self, process):
        with self.lock:
            self.process, self.ready = process, False

    def answer(self, request_id, answers):
        raise SteerRefused("Antigravity 没有原生问题")

    def reject_question(self, request_id, reason):
        raise SteerRefused("Antigravity 没有原生问题")

    def wait_for_answer(self, question):
        pass

    def close(self):
        with self.lock:
            self.closing, self.ready = True, False
            process = self.process
        if process is not None and process.poll() is None:
            stop_group(process, STOP_SECONDS)


def resume_argv(argv, session_id):
    """``argv`` resuming ``session_id``: its --conversation set to it."""
    argv = list(argv)
    if "--conversation" in argv:
        argv[argv.index("--conversation") + 1] = session_id
        return argv
    return [*argv, "--conversation", session_id]


def user_line(text):
    return json.dumps({"event": "user", "message": {"content": text}}, ensure_ascii=False) + "\n"


def next_words(rows, argv, stdin):
    """The argv and words of the process that goes on after ``rows``' one failed: in its conversation when it said
    one; SWITCH_RESUMED when agy had taken its words in (its ``user_input`` step), else the same words again."""
    if rows.sid is not None:
        argv = resume_argv(argv, rows.sid)
    if "user_input" in rows.kinds.values():
        return argv, user_line(SWITCH_RESUMED)
    return argv, stdin


def error_words(rows, stderr) -> str:
    """What agy said of a failure: its result's ``error`` and the error lines of its stderr (``AGY_ERROR``, ``error:``)."""
    lines = [line for line in str(stderr or "").splitlines() if re.match(r"\s*(?:AGY_ERROR|error)\b", line, re.I)]
    return "\n".join([str((rows.result or {}).get("error") or ""), *lines[-STDERR_LINES:]]).strip()


def outcome(rows, rc, stderr, cancel_event):
    """``stream_outcome``, with a process that ended before its conversation for want of a login said as one, and one
    that said nothing of its own whose stderr names a safety block or a model the account cannot use said as that."""
    status, code = stream_outcome(rows, rc, cancel_event)
    if code in ("process_exit", "root_incomplete") and rows.sid is None and auth_failure(stderr):
        code = "authentication_error"
    elif status == "failed" and code in UNSAID:
        said = error_code(error_words(rows, stderr))
        code = said if said in ("policy_refusal", "model_unavailable") else code
    return status, code


def reset_of(words, now):
    """When the quota ``words`` say is back ("Resets in 157h33m41s", "reset after 4h12m"), epoch seconds; None."""
    found = RESET_RE.search(str(words or ""))
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    seconds = sum(int(n) * units[u.lower()] for n, u in re.findall(r"(\d+)\s*([dhms])", found.group(1), re.I)) if found else 0
    return now + seconds if seconds > 0 else None


def failure_words(ctx, code, words, reset, now) -> str:
    """What the failure card's title says of a failed turn in agy's terms (RunResult.failure_detail): the model it could
    not use, or a spent quota with when it is back; "" otherwise (agy's own words are in 技术详情, ``said_words``)."""
    model = ctx.model or "所选模型"
    if code == "model_unavailable":
        return f"Antigravity 不认 {model}（账号没有它的权限、它已下线，或不支持所选档位）"
    if code == "quota_exhausted" and reset:
        return f"Antigravity 的 {model} 额度用完了，{clock(reset, now)} 恢复；恢复前重试不会成功，可以换个模型接着做"
    return ""


def said_words(rows, stderr, rc=None) -> str:
    """What agy said of a failed turn (RunResult.backend_said, core/failure.py said_of): its result's error, else the
    last ``AGY_ERROR`` line's short_error, else its last ``error:`` line, else its last line of stderr (a sign-in URL
    left out, ``stderr_lines``), else its exit code."""
    said = str((rows.result or {}).get("error") or "").strip()
    if not said:
        for line in reversed(str(stderr or "").splitlines()):
            short = re.search(r'"short_error"\s*:\s*"((?:[^"\\]|\\.)*)"', line) if "AGY_ERROR" in line else None
            if short or re.match(r"\s*error\b", line, re.I):
                said = short[1] if short else line.strip()
                break
    if not said:
        lines = stderr_lines(stderr)
        said = lines[-1] if lines else (f"agy 退出码 {rc}" if type(rc) is int and rc else "")
    return said_of(said)


def failed_turn(result, ctx, code, words, reset, now, said=""):
    """``result`` of a failed turn with its words (``failure_words``) and a spent quota's reset (``limit.reset_at``, for
    app/auto_retry.py), and that quota held (accounts.hold: no turn on its group starts meanwhile, accounts.holding);
    any other result as it is. The accounts' own (accounts.turn) say theirs over these."""
    if not result.is_error:
        return result
    detail = result.failure_detail or failure_words(ctx, code, words, reset, now)
    limit = {**(result.limit or {}), "reset_at": reset} if reset else result.limit
    if code == "quota_exhausted" and reset and not device_of(ctx) and not accounts.configured():
        accounts.hold(ctx.state_dir, ctx.model, reset, now)
    return dataclasses.replace(result, failure_detail=detail, limit=limit, backend_said=said)


def run_stream(request, argv, stdin, env, switch=None):
    """One job's turn: agy processes, the first with the prompt, each next one resuming the same conversation with
    the supplements that stopped the one before (AgyControl), or, after one that failed as SWITCHED, in the environment
    ``switch`` gives for the failure (the agy_turn hook's ``(env, note)``; None: the turn fails as it did)."""
    ctx, expected_session = request.context, request.session_id
    state = ReplyState(ctx.options, request.inputs.run_id, request.publish)
    adapter = AntigravityEventAdapter(lambda kind, data: request.emit(kind, data, None))
    control = AgyControl(lambda kind, data: request.emit(kind, data, control), device=bool(device_of(ctx)))
    adapter.control = control
    cancel_event = request.inputs.cancel_event
    deadline = time.monotonic() + ctx.timeout if ctx.timeout > 0 else None
    rows = StreamRows(request, adapter, expected_session, control)
    rc, stderr, forced_cleanup = None, "", False
    status, code, text = "failed", "root_incomplete", ""
    switches = 0
    start = covey_home.external_cwd(ctx)  # Its workdir, or a topic's outputs directory (``external_cwd``).
    try:
        request.observers.control(control)
    except Exception:
        pass
    try:
        state.invalidate()
        if ctx.options.get("compact_first") and expected_session:
            # A compaction the butler's idle one did not take in time: before the turn, in its environment.
            agy_compaction.compact_before_turn(ctx, argv, env, expected_session, adapter.steps,
                                               add_usage=lambda usage: adapter.add_usage(usage, context=False),
                                               cancel_event=cancel_event, process_observer=request.observers.process)
        while True:
            left = 0 if deadline is None else max(1, int(deadline - time.monotonic()))
            # Where this process keeps its conversations (its HOME, the account the agy_turn hook gave it): a tool step's
            # summary is read there (tool_summary); a device's are on the device.
            adapter.home = None if device_of(ctx) else (env or {}).get("HOME") or str(Path.home())
            adapter.fresh = rows.expected_session is None  # Not resuming a conversation: its result.usage is the turn's.
            rc, stderr = stream_subprocess_lines(argv, stdin_text=stdin, timeout=left, on_line=rows.on_line,
                                                 env=env, cwd=start, cancel_event=cancel_event,
                                                 process_observer=rows.observe, ctx=ctx)
            message, event_ids = control.take()
            if cancel_event.is_set() or rows.invalid or control.closing or (message and rows.sid is None):
                break
            if message:
                # Stopped for a supplement (or one came as it ended): the same conversation goes on with it.
                argv, stdin = resume_argv(argv, rows.sid), user_line(message)
                rows = StreamRows(request, adapter, rows.sid, control, resumed=True)
                adapter.stopped(time.time())
                for event_id in event_ids:
                    control.emit("steer_read", {"event_id": event_id})
                continue
            status, code = outcome(rows, rc, stderr, cancel_event)
            if switch is None or switches >= MAX_SWITCHES or status != "failed" or code not in SWITCHED:
                break
            try:
                found = switch({"code": code, "error": str((rows.result or {}).get("error") or ""),
                                "stderr": stderr_lines(stderr), "model": ctx.model, "conversation": rows.sid or ""})
            except Exception:
                logging.getLogger(__name__).warning("agy_turn switch failed", exc_info=True)
                found = None
            if found is None or cancel_event.is_set() or control.closing:
                break
            env, note = found
            switches += 1
            # The same conversation goes on in the other environment.
            argv, stdin = next_words(rows, argv, stdin)
            rows = StreamRows(request, adapter, rows.sid or rows.expected_session, control,
                              resumed=rows.resumed or rows.sid is not None)
            now = time.time()
            adapter.stopped(now)
            adapter.emit("progress_note", {"id": f"agy-switch-{switches}", "text": str(note or SWITCH_NOTE)[:300],
                                           "received_at": now})
        status, code = outcome(rows, rc, stderr, cancel_event)
        if status == "completed":
            text = rows.reply()
            state.preserve(rows.sid, ("antigravity-stream", rows.sid, state.run_id), ("final:" + state.run_id,), text)
    except JobCancelled:
        if not state.enabled:
            raise
        status, code = "cancelled", "cancelled"
    except subprocess.TimeoutExpired:
        if not state.enabled:
            raise subprocess.TimeoutExpired("agy", ctx.timeout) from None
        status, code = "timed_out", "timeout"
    except OSError:
        code = "process_exit" if rc is None else "transport_error"
    except Exception:
        code = "storage_failed" if state.storage_failed else "protocol_error"
    finally:
        with control.lock:
            control.closing, control.ready = True, False
        # Only an exceptional exit leaves the process unreaped; signal its private group.
        process = rows.process
        if process is not None and process.poll() is None:
            forced_cleanup = True
            stop_group(process, 5)
        try:
            request.observers.control(None)
        except Exception:
            pass
    if status != "completed":
        try:
            request.emit("backend_failure", {"status": status, "code": code, "stage": "turn",
                                             **({"exit_code": rc} if type(rc) is int else {}),
                                             "stderr": stderr_lines(stderr)}, None)
        except Exception:
            pass
    observed = process.poll() if process is not None else None
    now = time.time()
    words = error_words(rows, stderr) if status == "failed" else ""
    reset = reset_of(words, now) if code in ("rate_limited", "quota_exhausted") else None
    if reset is not None and reset - now > LONG_RESET:
        # "Individual quota reached … Resets in 157h33m41s" (T872): back in days, not minutes; retried every few minutes
        # as a passing limit it was, each failing. Spent, it is retried only at a reset within app/auto_retry.py's wait.
        code = "quota_exhausted"
    result = state.finish(text, rows.sid or expected_session, status, code,
                          identity=bool(rows.sid and rows.result is not None and not rows.invalid),
                          cleanup=not forced_cleanup and type(observed) is int and (rc is None or observed == rc),
                          cancel_event=cancel_event, emit=request.emit)
    return failed_turn(result, ctx, code, words, reset, now, said_words(rows, stderr, rc) if status == "failed" else "")


HARNESS_REGISTRY.register("agy", AntigravityBackend)

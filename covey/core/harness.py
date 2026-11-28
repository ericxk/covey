"""Harness adapter contracts for the Feishu bridge.

One Feishu bot serves every coding-agent Harness (Codex / Claude Code / ...).
All Feishu-side behavior (jobs, task cards, questions, supplements) is identical
across Harnesses and lives in the components of each Harness's ``BridgeApp``
(``covey.app.app``). A Harness plugs in three things:

- :class:`HarnessSpec` — pure config: names, prefixes, state and session locations.
- a backend object satisfying :class:`HarnessBackend` — how to invoke the
  Harness CLI and stream its progress.
- a store object satisfying :class:`SessionStore` — where local sessions live.
"""

from __future__ import annotations

import dataclasses
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

# A session id that is a UUID (Codex's thread ids): a HarnessSpec's session_pattern.
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclasses.dataclass
class BridgeContext:
    """Per-bridge runtime context; identical shape for every Harness."""

    profile: str | None
    workspace_name: str
    default_workdir: Path
    workdir: Path
    state_dir: Path
    history_path: Path
    session_path: Path
    sessions_dir: Path
    model: str
    effort: str
    timeout: int
    backend_cmd: list[str]
    skip_permissions: bool
    options: dict[str, Any] = dataclasses.field(default_factory=dict)
    topic_id: str = ""
    reply_to_message_id: str = ""
    # A Harness's work mode where it has them (core/defaults.WORK_MODES); "" is its default.
    work_mode: str = ""
    # A topic on another machine (core/devices.py): the device's alias and the directory on it, as text (never
    # resolved on the hub: ``~`` is the device's home). Both are '' on this machine, where ``workdir`` is the directory;
    # for a device ``workdir`` is the stand-in, a hub path that never exists.
    device: str = ""
    remote_workdir: str = ""


@dataclasses.dataclass(frozen=True)
class BackendConfig:
    """Backend-owned CLI/env configuration resolved from argparse args."""

    model: str
    effort: str
    timeout: int
    backend_cmd: list[str]
    skip_permissions: bool = True
    options: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class HarnessSpec:
    """Pure per-Harness configuration. No behavior lives here."""

    name: str                       # job-record backend key, e.g. "codex"
    title: str                      # display title, e.g. "Codex"
    prompt_agent_name: str          # self-name inside the bridge rules
    idempotency_prefix: str         # e.g. "codex-lark"
    env_prefix: str                 # e.g. "COVEY" / "LARK_CLAUDE_BRIDGE"
    state_dir_name: str             # e.g. "codex-lark-bridge"
    session_filename: str           # e.g. "codex_session_id"
    session_pattern: re.Pattern[str]
    default_profile: str | None     # None => run on the default lark-cli profile
    # Capabilities the core reads off the spec (an extension's Harness declares them in its own spec):
    refuses_questions: bool = True  # A native question can be rejected with a reason and the turn goes on; False: it ends the run.
    native_default_model: bool = True  # An empty model in a recorded job is the backend's own default; False: a record must name its model.

    def env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"


@dataclasses.dataclass(frozen=True)
class HarnessCapabilities:
    questions: bool = False
    # The images it takes with the prompt: one image's bytes, and what the prompt and its base64 images may add up to
    # in one request; 0 is no limit the Bridge knows. An image over either is left out with a note (app/runs.py).
    image_bytes: int = 0
    image_frame: int = 0
    steering: bool = False
    native_fork: bool = False  # can start a new session from a copy of another
    # Takes RunRequest.instructions (the bridge rules) at the system level, beside the model's own system prompt, so a
    # resumed turn's message need not restate them and a compaction keeps them (app/rules_sent.py, WAVE12 C9).
    system_instructions: bool = False
    # How a session keeps them (app/rules_sent.py): "turn" read again at every turn (dsh's patch, agy's GEMINI.md),
    # "compaction" kept from the session's first invocation until a compaction renders them again from the invocation's
    # own (Claude Code's --append-system-prompt), "" kept from the session's start (Codex's developerInstructions).
    system_refresh: str = ""
    rate_limit_retry: bool = False  # a turn rate-limited with no reply is retried automatically (app/auto_retry.py)
    incomplete_retry: bool = False  # a turn ended incomplete (settle_timeout) with no reply is continued once, too
    fault_retry: bool = False  # tells a passing server fault (529, 5xx, a dropped connection: server_fault) from other failures
    model_requests: bool = False  # sends model_request when the main task's first model call starts (after its setup)
    steer_reads: bool = False  # sends steer_read when the model takes a supplement in (Claude replays it)
    # its control's stop_command(note) stops the command the model waits on, so a supplement is read now
    # (app/steer_interrupt.py)
    command_stop: bool = False
    compaction: bool = False  # compacts a resumed session before the turn on ``compact_first`` (the butler's threshold)
    session_files: bool = False  # its session records are on this machine: subagent_report reads a turn's subagents
    redaction: bool = False  # runs a topic that chose redaction through the term relay (core/redaction.py)
    # rewind_point/rewind_files(ctx, session_id, prompt_uuid), ``resume_at`` and the prompt_read event: drops a
    # session's last turn (↩️ 回退, app/rewind.py)
    rewind: bool = False
    # Takes ``options["mcp_config"]`` (the butler's Bridge tools, core/butler.py mcp_config) into the turn, and holds a
    # project lead's turn to LEAD with them: the butler and a project's lead run only on a Harness that does
    # (app/butler_harness.py, app/tool_limits.py refusal).
    bridge_tools: bool = False
    # holds a turn to its ``options[TOOL_LIMIT]`` (app/tool_limits.py): Claude Code's tools, Codex's sandbox. Without it
    # a READ turn does not run and the other limits are words in the prompt only.
    read_only: bool = False
    # holds a LOOK turn's writes to its checkpoint file and the topic's outputs directory (core/look_write.py; W1-C, C6):
    # Claude Code's PreToolUse hook, Codex's and Antigravity's workspace_write_file in a sandbox or deny rules that write
    # nothing else. Without it a LOOK turn's limits are as they were and its words say so (app/tool_limits.py note).
    look_gate: bool = False
    # runs a 协作 turn (core/turn_source.py NEEDS): an ask in a temporary fork of the helper's native session, in a channel
    # topic of its own (app/collab_channel.py), held to CONSULT there; the helper's session, its pointer, history and 回退
    # marker are never touched, and the fork's limits hold for that turn alone. Declared once a real write was refused in
    # such a turn on the installed CLI (C8, docs/architecture.md 多参与者协作 · 能力与预检).
    collab: bool = False
    # Answers one question by itself (``backend.ask_once(ctx, system, data, *, limit, cwd, follow, effort)``, the
    # answer's text, or AskFailed): a system prompt and one JSON text in, in a process of its own with no tool that changes
    # anything, no MCP server, no hook and no saved session, its whole process group killed at the limit. ``follow``: the
    # model of the 辅助模型's setting ('' the Harness's own; a model gateway's goes to that gateway); ``effort``: that
    # setting's budget, which the question is asked at when the model has it. ``backend.ask_model(ctx, *, follow)``
    # says which model such a question runs on ('' when only the CLI knows) without starting anything, for the words of
    # the 辅助模型 setting. The background model's questions go there when it is this Harness (app/assist.py); Claude
    # Code's own one-shot is app/track_judge.py's.
    ask_once: bool = False
    # The tool its model asks the owner with in the middle of a turn (a question card, answered before the turn goes on):
    # Claude Code's AskUserQuestion, Codex's request_user_input; '' for none, where a question goes in the checkpoint's
    # ``questions`` and the card comes after the turn. app/prompts.py ask_words says it so in the butler's rules (W2-A).
    question_tool: str = ""
    # Reminds the butler's own turn by itself once it has called ``tool_nudge``'s number of tools other than the Bridge's
    # (Claude Code's PostToolUse hook, backends/claude/hooks.py). Without it the Bridge counts the turn's tool events and
    # sends the reminder as an internal supplement (app/interaction.py, W2-A).
    nudges: bool = False


class AskFailed(Exception):
    """A question ``ask_once`` got no usable answer to: ``reason`` as the small model's log lines say it, one of
    core/assist.py's (a turn's code: authentication_error, quota_exhausted, rate_limited, server_fault, transport_error,
    model_unavailable, model_blocked, policy_refusal; or a question's own: not_signed_in, timeout, spawn_failed,
    exit_nonzero, bad_output, model_error, too_long), ``detail`` the CLI's own words as a card may show them
    (core/failure.py said_of), never the question's text."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


# What a turn may do, where its Harness can hold it to that (``HarnessCapabilities.read_only``): a context's
# ``options[TOOL_LIMIT]``, set by app/runs.py from app/tool_limits.py's ``limit``, absent for a turn as before. READ: a
# group member's turn in a group whose members are read-only (reading and searching only); LOOK: a turn started without
# the owner's click because the butler judged it read-only (no file changes); PROXY: a 代答 topic's turn (reading,
# searching, lark-cli's read commands, writing the contact's notes); CONSULT: a 协作 ask's turn (core/turn_source.py ASK):
# reading and searching only, none of the owner's MCP servers and none of the Bridge's tools whatever the turn's options
# say, no shell, no network, no question, no checkpoint (Codex: its read-only sandbox in a private CODEX_HOME). LEAD: a
# project lead's turn (app/project_lead.py lead_turn): reading and searching and its Bridge tools, no file change and no
# command that changes anything (Claude Code: Read, Grep and Glob; Codex: its read-only sandbox, the Bridge's tools
# approved; Antigravity: deny rules for commands, file writes and URL reads; DeepSeek Harness: its read-only permission
# preset, no approval asked).
TOOL_LIMIT = "tool_limit"
READ, LOOK, PROXY, CONSULT, LEAD = "read", "look", "proxy", "consult", "lead"
TOOL_NOTES = "tool_notes"  # With PROXY: the contact's notes directory, the one place its edit tools may write ('' none).
# With LOOK (app/tool_limits.py look_roots): {"file": the turn's checkpoint, "dir": the topic's outputs directory, "held":
# whether this Harness holds its writes to those two (``HarnessCapabilities.look_gate``)}; core/look_write.py checks a path.
LOOK_ROOTS = "look_roots"


def backend_capabilities(backend, ctx):
    method = getattr(backend, "capabilities", None)
    return method(ctx) if callable(method) else HarnessCapabilities()


# What a Harness process on a device cannot do: each reads files of the hub (the session store, the request log, the
# file snapshots) or runs the hub's relay, none of which exist there.
# bridge_tools: the butler's tools are a process on this machine, its state directory too. collab: no device ran C8, and
# its session store is the device's (app/collab_checks.py refuses a topic on a device). ask_once: its private home and
# directory are this machine's. look_gate: the hook script, the write tool's server and the private home are this machine's.
DEVICE_OFF = ("native_fork", "compaction", "session_files", "redaction", "rewind", "command_stop", "bridge_tools", "collab",
              "ask_once", "look_gate")


def for_device(capabilities, ctx):
    """``capabilities`` as they are for ``ctx``: unchanged on this machine, and on a device (``ctx.device``) without
    ``DEVICE_OFF``, so no card offers a 回退, a 下载 or a 压缩 there (every entry reads these flags)."""
    if not device_of(ctx):
        return capabilities
    return dataclasses.replace(capabilities, **{name: False for name in DEVICE_OFF})


from covey.core.process import JobCancelled, device_of  # JobCancelled's home is core/process.py, with stream_subprocess_lines.
from covey.core.result import ExecutionOutcome, VerifiedRootReply
from covey.core.protocol import HarnessEvent


@dataclasses.dataclass(frozen=True)
class RunCallbacks:
    event: Callable[[str, dict, Any], None] = lambda *args: None
    session: Callable[[str], None] = lambda *args: None
    control: Callable[[Any], None] = lambda *args: None
    process: Callable[[Any], None] = lambda *args: None
    verified_reply: Callable[[VerifiedRootReply | None], None] = lambda *args: None


@dataclasses.dataclass(frozen=True)
class RunInputs:
    cancel_event: threading.Event
    checkpoint_path: str = ""
    attachments: tuple[dict, ...] = ()
    run_id: str = ""


_UNOBSERVED = RunCallbacks()


@dataclasses.dataclass(frozen=True)
class RunRequest:
    """Everything a backend gets for one turn; backends read their observers and inputs here."""
    context: BridgeContext
    prompt: str
    session_id: str | None = None
    on_progress: Callable[[str, bool], None] | None = None
    callbacks: RunCallbacks | None = None  # None: nobody observes, and no reply is verified.
    inputs: RunInputs | None = None
    # The bridge rules, for a backend whose capabilities declare system_instructions; "" otherwise
    # (the rules are then in the prompt).
    instructions: str = ""

    def __post_init__(self):
        if self.inputs is None:  # Started without inputs: nothing can stop it.
            object.__setattr__(self, "inputs", RunInputs(threading.Event()))

    @property
    def observers(self) -> RunCallbacks:
        return self.callbacks or _UNOBSERVED

    @property
    def publish(self) -> Callable[[VerifiedRootReply | None], None] | None:
        """Where a verified root reply is saved; None without callbacks."""
        return self.callbacks.verified_reply if self.callbacks else None

    def emit(self, kind, data, control=None):
        """A structured event for the bridge; a failing progress-only event never stops the turn."""
        HarnessEvent(kind, data).deliver(self.observers.event, control)

    def check_cancelled(self, message="任务已停止"):
        if self.inputs.cancel_event.is_set():
            raise JobCancelled(message)


@dataclasses.dataclass(frozen=True)
class RunResult:
    text: str
    session_id: str | None
    is_error: bool = False
    public_reply: VerifiedRootReply | None = None
    outcome: ExecutionOutcome | None = None
    # The route's own words for the outcome's error_code, where the shared ones would mislead
    # (Acme's 429 is its upstream's capacity, not the owner's quota). Replaces the shared
    # detail on the failure card; the label and recovery steps stay.
    failure_detail: str = ""
    # The Harness's own words for the failure, as it said them (core/failure.py said_of: secrets, keys, paths, addresses
    # hidden, one line, bounded): the failure card's 技术详情 shows them whatever the error (T872), "" for none.
    backend_said: str = ""
    # What a limit or a passing server fault said of itself ({reset_at: epoch seconds, evidence: [rows]}), for the
    # automatic retry (app/auto_retry.py); None for a turn that ended otherwise.
    limit: dict | None = None


def normalize_run_result(value):
    """A backend's result, checked: only a RunResult is one."""
    if not isinstance(value, RunResult):
        raise ValueError("invalid backend result")
    return value


class RunHandle(Protocol):
    """Task-owned controls. Writes must never be automatically replayed."""
    user_requests: set
    supports_steer: bool
    # Backends that keep a turn open on a question (Claude) say so here;
    # Codex interrupts the turn instead and sets pause_for_question.
    awaiting_answer: bool

    def answer(self, request_id: str | int, answers: dict) -> None: ...
    def steer(self, text: str, event_id: str) -> None: ...
    def close(self) -> None: ...
    def reject_question(self, request_id: str | int, reason: str) -> None: ...
    def wait_for_answer(self, question: str) -> None: ...


class BackendRuntime:
    """Shared entry contract: a backend runs one turn from a RunRequest."""
    def capabilities(self, ctx: BridgeContext) -> HarnessCapabilities:
        return HarnessCapabilities()

    def run(self, request: RunRequest) -> RunResult:
        raise NotImplementedError("a backend implements run(request)")

    def start_refusal(self, ctx: BridgeContext):
        """(error code, words, reset epoch or 0) when a turn on ``ctx`` would only fail the same way, so it is refused
        before it starts (app/runs.py); None to start it. A backend that knows such a case says so (backends/agy)."""
        return None


class HarnessRegistry:
    def __init__(self):
        self._factories = {}

    def register(self, name, factory):
        if name in self._factories and self._factories[name] is not factory:
            raise ValueError('Harness 已注册：' + name)
        self._factories[name] = factory

    def create(self, name):
        if name not in self._factories:
            raise ValueError('未知 Harness：' + name)
        return self._factories[name]()


HARNESS_REGISTRY = HarnessRegistry()

class HarnessBackend(Protocol):
    """How to run one reply job on the Harness CLI."""

    def run(self, request: RunRequest) -> RunResult: ...

    def capabilities(self, ctx: BridgeContext) -> HarnessCapabilities: ...

    def add_cli_args(self, parser: Any) -> None:
        """Register backend-specific argparse flags (model/effort/cmd/timeout...)."""

    def build_config(self, args: Any) -> BackendConfig:
        """Resolve argparse args into a :class:`BackendConfig`."""

    def prompt_runtime_note(self, ctx: BridgeContext) -> str:
        """Permission/sandbox note in the bridge rules; "" uses the default wording."""

    def prompt_notes(self, ctx: BridgeContext) -> tuple[str, ...]:
        """Optional: further constraints for the bridge rules, one bullet each."""

    def startup_lines(self, ctx: BridgeContext) -> list[str]:
        """Backend-specific startup log lines."""

    def on_startup(self, ctx: BridgeContext) -> None:
        """Hook that runs once before the loops start."""


class SessionStore(Protocol):
    """Minimal session path capability: where local Harness sessions live."""

    def sessions_dir(self, workdir: Path) -> Path:
        """Display/context path of the local session store for ``workdir``."""


def format_timeout(seconds: int) -> str:
    if seconds <= 0:
        return "不限制"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60}m"
    days = hours // 24
    return f"{days}d{hours % 24}h"

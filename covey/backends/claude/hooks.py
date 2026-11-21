"""The SDK hook callbacks a turn's Claude Code is started with (``TurnHooks``): its initialize request registers them
(``registration``, backends/claude/process.py), and backends/claude/control.py answers each ``hook_callback`` control
request the CLI sends while the turn runs (``answer``), as the official Agent SDK does with its hook functions.

- 子 Agent 守则 (#33, 5.9 row 10): every subagent but Explore (backends/claude/subagents.py EXPLORE has a prompt of its own)
  is told RULES when it starts (SubagentStart's ``additionalContext``). A subagent's call of a Bridge tool (mcp__bridge__*,
  the butler's and a lead's, core/butler.py MCP_SERVER) is refused with REFUSED (PreToolUse: the CLI names the subagent's
  ``agent_id`` in the input of a hook it fires inside one): a subagent the butler sends out does not open or hand on topics.
- One reminder (#34): a turn whose context carries ``tool_nudge`` (app/butler.py: the butler's own turn, unless the owner
  said to do it there) is told its text once the main thread has called that many tools other than the Bridge's
  (PostToolUse's ``additionalContext``).

COVEY_CLAUDE_HOOKS=0 registers none of them: the initialize request and every turn are as before. A callback is answered
with {} (go on as without it) for whatever is not this module's to stop; Claude Code goes on the same when an answer
fails (the registration's default ``onFailure``, continue).
"""
from __future__ import annotations

from covey.core.process_env import env_flag

SWITCH = "COVEY_CLAUDE_HOOKS"
START, BRIDGE_TOOL, COUNT = "covey_subagent_start", "covey_subagent_bridge_tool", "covey_tool_count"
BRIDGE_PREFIX = "mcp__bridge__"
EXPLORE = "Explore"
NUDGE = "tool_nudge"  # ctx.options key: {"after": calls, "text": what the turn is told then}.
RULES = "\n".join((
    "[子 Agent 守则]",
    "- 你在替主 Agent 干活，不直接对主人说话，也不能发消息和文件。",
    "- 最终回复就是你的全部交付：写清做了什么、结果、没做完的、每个产物的绝对路径。",
    "- 需要主人拍板时，不可逆的事不要猜，用一个能原样转给主人的问题收尾。",
    "- 你之后可能被带着答案叫回来接着做。",
))
# A refusal with a way on (ClaudePartner's): what the subagent wanted done goes into its final reply.
REFUSED = "子 Agent 不能调用 Bridge 的工具（开话题、转交、回答问题这些）：把要做的事写进你的最终回复，主 Agent 会转达或去做。"


def enabled(environ=None) -> bool:
    """Whether a turn's claude gets the hook callbacks (COVEY_CLAUDE_HOOKS, on unless 0/false/no/off)."""
    return env_flag(SWITCH, True, environ)


class TurnHooks:
    """The hook callbacks of one turn: ``registration`` for its initialize request, ``answer`` for each callback.
    ``calls`` counts the main thread's tool calls other than the Bridge's, for the one reminder."""

    def __init__(self, ctx, environ=None) -> None:
        options = getattr(ctx, "options", None) or {}
        self.on = enabled(environ)
        self.bridge = bool(options.get("mcp_config"))
        nudge = options.get(NUDGE) if isinstance(options.get(NUDGE), dict) else {}
        after = nudge.get("after")
        self.after = after if type(after) is int and after > 0 else 0
        self.nudge = str(nudge.get("text") or "")
        self.calls = 0
        self.nudged = False

    def registration(self) -> dict:
        """The initialize request's ``hooks`` (event -> matchers with their callback ids); {} with the switch off."""
        if not self.on:
            return {}
        found = {"SubagentStart": [{"hookCallbackIds": [START]}]}
        if self.bridge:
            found["PreToolUse"] = [{"matcher": BRIDGE_PREFIX + ".*", "hookCallbackIds": [BRIDGE_TOOL]}]
        if self.after and self.nudge:
            found["PostToolUse"] = [{"hookCallbackIds": [COUNT]}]
        return found

    def answer(self, callback_id, data) -> dict:
        """The answer to the callback ``callback_id`` with the hook's input ``data``: {} to go on as without it."""
        data = data if isinstance(data, dict) else {}
        tool = str(data.get("tool_name") or "")
        if callback_id == START and self.on:
            if str(data.get("agent_type") or "") == EXPLORE:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": RULES}}
        if callback_id == BRIDGE_TOOL and self.on:
            if data.get("agent_id") and tool.startswith(BRIDGE_PREFIX):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": REFUSED}}
            return {}
        if callback_id == COUNT and self.on and self.after and self.nudge:
            if data.get("agent_id") or not tool or tool.startswith(BRIDGE_PREFIX):
                return {}
            self.calls += 1
            if self.calls < self.after or self.nudged:
                return {}
            self.nudged = True
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": self.nudge}}
        return {}

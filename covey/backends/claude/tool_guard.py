"""The PreToolUse hook of a 代答 topic's Claude Code turn (core/harness.py PROXY, app/tool_limits.py): ``settings`` is the
``hooks`` part of the turn's --settings, and Claude Code runs bin/covey_tool_guard.py (``main``) before each Bash, Edit,
Write, MultiEdit or NotebookEdit call, the call as JSON on its stdin. A call it does not allow exits 2 with the reason on
stderr, which Claude Code gives the model instead of running the call, --dangerously-skip-permissions or not. Anything
that goes wrong in here refuses the call.

- Bash (``command_refusal``): one ``lark-cli`` command that only reads: a ``+shortcut`` whose first or last word is a
  reading verb (READ_VERBS: ``+chat-messages-list``, ``+messages-search``, ``+fetch``, ``+search-user`` ...), a typed
  command whose method is one (``wiki spaces get_node``, ``im chats list``), ``api GET``, ``schema``, ``skills`` or
  ``--help``; at most ``timeout <duration>`` in front of it and pipes into FILTERS after it
  (``2>&1`` and ``2>/dev/null`` allowed); no other shell syntax (a redirection, ``;``, ``&&``, a subshell, ``$``, a
  backquote, a newline) and no flag that writes a file (WRITE_FLAGS).
- The edit tools (``path_refusal``): a file inside the contact's notes directory (``--notes``); none without one.

Standard library only."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys

TOOLS = ("Read", "Grep", "Glob", "Bash", "Edit", "Write")  # What --tools gives a 代答 turn.
HOOKED = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit")
READ_VERBS = frozenset({"list", "search", "get", "mget", "fetch", "read", "query", "info", "agenda", "view", "show"})
WRITE_FLAGS = ("--output", "--out", "-o", "--save", "--file", "--download", "--dest", "--dir", "--yes")
# Flags of lark-cli itself that take a value, skipped when looking for its first word (``schema``, ``skills``).
GLOBAL_VALUES = frozenset({"--profile", "--as", "--format", "--jq"})
SHELL_CHARS = re.compile(r"[$`\n\r]")
OPERATOR = re.compile(r"[();<>|&]+")
QUIET = re.compile(r"\s2>&1(?=\s|$)|\s2>\s*/dev/null(?=\s|$)")  # stderr into stdout or nowhere: harmless.
FILTERS = frozenset({"head", "tail", "jq", "wc", "grep"})  # What a lark-cli read may be piped into: none of them writes.
DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
BASH_WORDS = ("这是代答话题：命令行只能跑一条只读的 lark-cli 命令（+…-list、+…-search、+fetch、api GET 这类），"
              "不能接管道、重定向或别的命令；读代码和记录用 Read、Grep、Glob。")


def settings(notes, *, python=None, script=None) -> dict:
    """The ``hooks`` of a 代答 turn's Claude Code settings: this module's ``main`` before every call of HOOKED, with the
    contact's ``notes`` directory ('' when it has none)."""
    if script is None:
        from covey.core.paths import entry_script
        script = entry_script("covey_tool_guard.py")
    command = shlex.join([python or sys.executable, "-B", str(script), "--notes", str(notes or "")])
    return {"PreToolUse": [{"matcher": "|".join(HOOKED), "hooks": [{"type": "command", "command": command}]}]}


def lark_args(command):
    """The arguments after ``lark-cli`` of ``command``, or None when it is not one plain lark-cli command (piped into
    FILTERS at most)."""
    if SHELL_CHARS.search(command):
        return None
    lexer = shlex.shlex(QUIET.sub(" ", command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments = [[]]
    for token in lexer:
        if token == "|":
            segments.append([])
        elif OPERATOR.fullmatch(token):
            return None
        else:
            segments[-1].append(token)
    tokens, piped = segments[0], segments[1:]
    if not tokens or any(not part or os.path.basename(part[0]) not in FILTERS for part in piped):
        return None
    at = 0
    if tokens[0] == "timeout":
        at = 1
        while at < len(tokens) and tokens[at].startswith("-"):
            at += 2 if tokens[at] in ("-k", "-s", "--kill-after", "--signal") else 1
        if at >= len(tokens) or not DURATION.fullmatch(tokens[at]):
            return None
        at += 1
    if at >= len(tokens) or os.path.basename(tokens[at]) != "lark-cli":
        return None
    return tokens[at + 1:]


def words(args, count=3):
    """lark-cli's first ``count`` words (domain, resource, method), its own flags with their values skipped."""
    found, at = [], 0
    while at < len(args) and len(found) < count:
        token = args[at]
        if token.startswith("-"):
            at += 2 if token in GLOBAL_VALUES and "=" not in token else 1
            continue
        found.append(token)
        at += 1
    return found


def first_word(args):
    """lark-cli's first word (its domain or command); '' when none."""
    return (words(args, 1) or [""])[0]


def reading(name, sep) -> bool:
    """Whether ``name`` (a shortcut without its +, or a method) starts or ends with a word of READ_VERBS."""
    parts = name.lower().split(sep)
    return bool(parts) and (parts[0] in READ_VERBS or parts[-1] in READ_VERBS)


def reads(args) -> bool:
    """Whether lark-cli ``args`` only read: see the module docstring."""
    if any(t == flag or t.startswith(flag + "=") for t in args for flag in WRITE_FLAGS):
        return False
    if "--help" in args or "-h" in args:
        return True
    shortcuts = [t for t in args if t.startswith("+")]
    if shortcuts:
        return len(shortcuts) == 1 and reading(shortcuts[0][1:], "-")
    if any(a == "api" and b.upper() == "GET" for a, b in zip(args, args[1:])):
        return True
    found = words(args)
    if found[:1] in (["schema"], ["skills"]):
        return True
    return len(found) == 3 and found[0] not in ("api", "auth", "event") and reading(found[2], "_")


def command_refusal(command) -> str:
    """Why a Bash call of ``command`` does not run in a 代答 turn; '' when it runs."""
    args = lark_args(str(command or ""))
    return "" if args is not None and reads(args) else BASH_WORDS


def inside(path, root) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def path_refusal(tool, given, notes, cwd="") -> str:
    """Why ``tool`` (an edit tool) may not write ``given`` in a 代答 turn; '' when it may: inside ``notes``."""
    if not notes:
        return f"这是代答话题：{tool} 不能用，这个联系人没有笔记目录，代答话题不改别的文件。"
    path = os.path.expanduser(str(given or ""))
    if not path:
        return f"这是代答话题：{tool} 没有给出文件。"
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    if inside(path, notes):
        return ""
    return f"这是代答话题：只能改联系人笔记目录 {notes} 里的文件，{tool} 这次没有执行。"


def refusal(call, notes) -> str:
    """Why the PreToolUse ``call`` (Claude Code's hook input) does not run; '' when it does."""
    tool = str(call.get("tool_name") or "")
    given = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
    if tool == "Bash":
        return command_refusal(given.get("command"))
    if tool in ("Edit", "Write", "MultiEdit"):
        return path_refusal(tool, given.get("file_path"), notes, str(call.get("cwd") or ""))
    if tool == "NotebookEdit":
        return path_refusal(tool, given.get("notebook_path"), notes, str(call.get("cwd") or ""))
    return ""


def main(argv=None, stdin=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_tool_guard.py",
                                     description="A 代答 turn's PreToolUse hook: the call's JSON on stdin, exit 2 refuses it.")
    parser.add_argument("--notes", default="", help="the contact's notes directory, the one place edit tools may write")
    args = parser.parse_args(argv)  # An argument it does not take exits 2, which refuses the call too.
    try:
        call = json.load(stdin or sys.stdin)
        reason = refusal(call if isinstance(call, dict) else {}, args.notes)
    except Exception as exc:  # noqa: BLE001 - a guard that cannot tell refuses
        reason = f"代答话题的工具检查出错（{type(exc).__name__}），这次调用没有执行。"
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

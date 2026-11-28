"""Claude Code's PreToolUse hooks that Covey writes into a turn's --settings (``hooks``), run as bin/covey_tool_guard.py
(``main``) with the call as JSON on stdin. A call it does not allow exits 2 with the reason on stderr, which Claude Code
gives the model instead of running the call, --dangerously-skip-permissions or not. Two modes (``--mode``):

``proxy`` (the default; ``settings``): a 代答 topic's turn (core/harness.py PROXY, app/tool_limits.py), before each Bash,
Edit, Write, MultiEdit or NotebookEdit call. Anything that goes wrong in here refuses the call.

- Bash (``command_refusal``): one ``lark-cli`` command that only reads: a ``+shortcut`` whose first or last word is a
  reading verb (READ_VERBS: ``+chat-messages-list``, ``+messages-search``, ``+fetch``, ``+search-user`` ...), a typed
  command whose method is one (``wiki spaces get_node``, ``im chats list``), ``api GET``, ``schema``, ``skills`` or
  ``--help``; at most ``timeout <duration>`` in front of it and pipes into FILTERS after it
  (``2>&1`` and ``2>/dev/null`` allowed); no other shell syntax (a redirection, ``;``, ``&&``, a subshell, ``$``, a
  backquote, a newline) and no flag that writes a file (WRITE_FLAGS).
- The edit tools (``path_refusal``): a file inside the contact's notes directory (``--notes``); none without one.

``home-search`` (``home_search_settings``; backends/claude/argv.py ``home_hooks``): a turn started from the home
(``--home``, the service's --workdir), before each Bash, Grep or Glob call and never another tool's. The home holds every
repository next to the large data directories, and one search over all of it can take the machine's memory before
service/memguard.py stops it. It refuses only a recursive search with no depth limit whose target, resolved against the
call's ``cwd``, is the home or a directory above it (``covers_home``): ``rg <词>``, ``rg --files``, ``grep -r``,
``find .``, ``fd``, ``ls -R``, ``du``, ``tree``, ``os.walk('.')``/``rglob``/``glob('**/*')`` in an inline Python, the
Grep tool with no path, the Glob tool with ``**`` there. A search in a repository or any directory under the home runs
(``cd <仓库> && rg ...``, ``rg <词> <仓库>``, a data subdirectory), and so does one with a depth limit
(``find . -maxdepth 2``, ``rg --max-depth 2``, ``tree -L 2``; not ``du``, whose ``-d`` limits what it prints, not what
it reads). Anything it cannot read (a parse error, a variable it cannot resolve, the guard's own fault) lets the call
run: it only keeps an accident from the machine, never stands in the way of work (``search_refusal``).

``look`` (``look_settings``; backends/claude/argv.py ``look_hooks``): a LOOK turn on this machine (core/harness.py LOOK,
app/tool_limits.py ``look_roots``), before each Bash, Edit, Write, MultiEdit or NotebookEdit call. The edit tools write only
the turn's checkpoint file (``--allow-file``) and the topic's outputs directory (``--allow-dir``), by the check every LOOK
gate shares (core/look_write.py ``refusal``: ``.git`` and the shared read worktrees never). Bash runs, except an obvious
write elsewhere (``look_bash_refusal``): a redirection (``>``, ``>>``, ``&>``), ``tee``, ``sed -i``/``perl -i``,
``cp``/``mv``/``install``/``ln``/``rsync`` into it (``mv`` from it too), ``rm``/``rmdir``/``touch``/``mkdir``/``truncate``/
``chmod``/``chown``/``shred``/``unlink``, ``dd of=``, ``curl -o``/``-O`` and ``wget``, ``find -delete`` or ``-exec`` of
one of those, ``xargs`` of one, a git command that writes (``commit``, ``checkout``, ``reset``, ``push``, ``clean`` …;
``git log``, ``status``, ``diff``, ``show``, ``fetch`` run), ``covey repo use --write`` (``covey repo use`` to read
runs) and ``covey device`` other than ``list``, ``get``, ``ls`` and ``stat`` (``DEVICE_READS``: the channel itself holds
the turn to reading, core/device_access.py G3; this is its second wall). What a program writes by itself (a script, ``python -c``) it cannot see: docs/harnesses.md and tests/test_matrix.py
list that as the shell's limit. Anything that goes wrong in here refuses the call.

``shim`` (``shim_main``; ``--mode shim <命令名> <参数…>``): not a hook but what the search shims in ``<数据根>/bin``
(core/home.py ``SEARCH_SHIMS``: ``rg``, ``find``, ``fd``, ``du``, ``tree``, ``grep``, ``ls``) ask before they run the real
program, so every Harness's shell command gets the home-search rule, not only Claude Code's. The home is ``$COVEY_HOME``,
the start directory the cwd; the same ``searcher_targets``/``find_targets``/``covers_home``. Exit ``SHIM_REFUSED`` with one
line on stderr for a search of the whole home, 0 otherwise, and 0 for any fault. Input from a pipe or a file (fd 0) is what
``rg <词>`` then searches. Programs searching inside themselves (``os.walk``) never reach it.

Standard library only."""
from __future__ import annotations

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


# ---- home-search: a turn started from the home (module docstring) ----

PROXY_MODE, HOME_SEARCH = "proxy", "home-search"
SEARCH_HOOKED = ("Bash", "Grep", "Glob")  # Never Read, Edit or Write: they name one file.
SEARCH_WORDS = ("{what}会递归扫整个主目录 {home}（目标是主目录根或它的上级，又没限深度），主目录下混着各个仓库和大数据目录，"
                "整树扫一遍可能耗尽内存，这次没有执行。先按主目录的 AGENTS.md 或仓库地图找到具体仓库或子目录，"
                "再在那里搜（{fix}）。")
SHELL_FIX = "如 `rg <词> <仓库>`、`cd <仓库> && rg <词>`、`find <子目录> -name …` 或 `find . -maxdepth 2 -name …`"
GREP_FIX = "Grep 的 path 填具体仓库或子目录"
GLOB_FIX = "Glob 的 path 填具体仓库或子目录，或者让 pattern 以具体子目录开头，如 `covey/**/*.py`"
PY_FIX = "os.walk、rglob、glob('**') 的起点写具体仓库或子目录"
WILDCARD = re.compile(r"[*?\[{]")
FD_NUMBER = re.compile(r"(?<![\w$=/.-])\d+(?=[<>])")  # The 2 of 2>/dev/null: not an argument.
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([^\s'\"<>|&;()]+)\2")
SEPARATORS = frozenset({"&&", "||", "|", "|&", ";", ";;", "&", "(", ")", "\n"})
KEYWORDS = frozenset({"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until", "time", "$"})
PYTHON = re.compile(r"(?:python[\d.]*|pypy[\d.]*)")
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
STOP = frozenset({"xargs", "parallel", "watch"})  # What they run takes its paths from elsewhere: unknown.

# A program's options that take the next word as their value (short ones also glued: -m3), and those that limit how deep it
# goes. ``always`` marks the ones that walk the tree whatever their flags (rg, find, fd, du, tree); grep and ls only with
# their recursive flag. ``first`` is how many leading words are not paths (the pattern), unless one of ``given`` names it.
SEARCHERS = {
    "rg": dict(short="efgtTmABCjMrEd", long={"--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
               "--type-add", "--type-clear", "--max-count", "--after-context", "--before-context", "--context",
               "--threads", "--max-columns", "--replace", "--encoding", "--max-depth", "--maxdepth", "--max-filesize",
               "--path-separator", "--pre", "--pre-glob", "--sort", "--sortr", "--colors", "--color",
               "--context-separator", "--ignore-file", "--engine", "--dfa-size-limit", "--regex-size-limit"},
               depth={"-d", "--max-depth", "--maxdepth"}, always=True, first=1,
               given={"-e", "--regexp", "-f", "--file", "--files", "--type-list"},
               idle={"--type-list", "--version", "-V", "--help", "-h", "--generate", "--pcre2-version"}),
    "grep": dict(short="efmABCdD", long={"--regexp", "--file", "--max-count", "--after-context", "--before-context",
                 "--context", "--directories", "--devices", "--include", "--exclude", "--exclude-dir", "--exclude-from",
                 "--label", "--binary-files", "--group-separator"},
                 depth=set(), always=False, recursive="rR", recursive_long={"--recursive", "--dereference-recursive"},
                 first=1, given={"-e", "--regexp", "-f", "--file"}, idle={"--help", "-V", "--version"}),
    "fd": dict(short="etEdxXjSco", long={"--extension", "--type", "--exclude", "--max-depth", "--maxdepth",
               "--min-depth", "--exact-depth", "--threads", "--size", "--changed-within", "--changed-before",
               "--owner", "--color", "--base-directory", "--search-path", "--path-separator", "--max-results",
               "--max-buffer-time", "--ignore-file", "--format", "--batch-size"},
               depth={"-d", "--max-depth", "--maxdepth", "--exact-depth"}, always=True, first=1, given=set(),
               idle={"--help", "-h", "--version", "-V"}),
    "ls": dict(short="IwT", long={"--ignore", "--hide", "--format", "--width", "--tabsize", "--sort", "--time",
               "--time-style", "--quoting-style", "--indicator-style", "--block-size", "--color"},
               depth=set(), always=False, recursive="R", recursive_long={"--recursive"}, first=0, given=set(),
               idle={"--help", "--version"}),
    "du": dict(short="dBtX", long={"--max-depth", "--block-size", "--threshold", "--exclude-from", "--exclude",
               "--time-style"}, depth=set(), always=True, first=0, given=set(), idle={"--help", "--version"}),
    "tree": dict(short="LIPoHT", long={"--filelimit", "--charset", "--timefmt", "--sort", "-L", "-I", "-P", "-o"},
                 depth={"-L"}, always=True, first=0, given=set(), idle={"--help", "--version"}),
}
ALIASES = {"ripgrep": "rg", "egrep": "grep", "fgrep": "grep", "fdfind": "fd"}


def home_search_settings(home, *, python=None, script=None) -> dict:
    """The ``hooks`` of a turn started from ``home``: this module's ``main`` in its home-search mode before every Bash,
    Grep and Glob call."""
    if script is None:
        from covey.core.paths import entry_script
        script = entry_script("covey_tool_guard.py")
    command = shlex.join([python or sys.executable, "-B", str(script), "--mode", HOME_SEARCH, "--home", str(home)])
    return {"PreToolUse": [{"matcher": "|".join(SEARCH_HOOKED), "hooks": [{"type": "command", "command": command}]}]}


def covers_home(target, home) -> bool:
    """Whether a search from ``target`` walks the whole of ``home``: it is the home or a directory above it."""
    target, home = os.path.realpath(target), os.path.realpath(home)
    return home == target or home.startswith(target.rstrip(os.sep) + os.sep)


def expand(word, cwd, home):
    """``word`` as a path: ``~``, ``$HOME``, ``$PWD`` and ``$COVEY_HOME`` filled in and made absolute against ``cwd``; a
    pattern's directory part (``*`` is ``cwd``, ``covey/*`` is ``covey``: what the shell's expansion lists from); None
    when it cannot tell (another variable, a command's output, no ``cwd`` for a relative path)."""
    word = re.sub(r"\$\{?COVEY_HOME\}?(?!\w)", lambda _: home, str(word))
    word = re.sub(r"\$\{?HOME\}?(?!\w)", lambda _: os.path.expanduser("~"), word)
    if cwd:
        word = re.sub(r"\$\{?PWD\}?(?!\w)", lambda _: cwd, word)
    if "$" in word or "`" in word:
        return None
    word = os.path.expanduser(word)
    if WILDCARD.search(word):
        parts = word.split("/")
        at = next(i for i, part in enumerate(parts) if WILDCARD.search(part))
        word = "/".join(parts[:at]) or ("/" if word.startswith("/") else ".")
    if not os.path.isabs(word):
        if not cwd:
            return None
        word = os.path.join(cwd, word)
    return os.path.normpath(word)


def scan_options(words, spec):
    """``words`` (a command's, its name left off) read against ``spec`` (SEARCHERS): (its options' names, the positionals)."""
    options, positionals, at, done = set(), [], 0, False
    while at < len(words):
        word = words[at]
        at += 1
        if done or not word.startswith("-") or word == "-":
            positionals.append(word)
            continue
        if word == "--":
            done = True
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            options.add(name)
            if "=" not in word and name in spec["long"]:
                at += 1
            continue
        if word in spec["long"]:  # tree's -L, -I ...: one dash, a word of their own.
            options.add(word)
            at += 1
            continue
        for index, char in enumerate(word[1:], 1):
            options.add("-" + char)
            if char in spec["short"]:
                at += 0 if index < len(word) - 1 else 1  # Glued (-m3) or the next word.
                break
    return options, positionals


def searcher_targets(name, words, cwd, home, *, stdin=False):
    """Where ``name`` (SEARCHERS) with ``words`` walks unbounded: the paths ([] when it walks none: not recursive, a depth
    limit, its input from a pipe), None for one it cannot tell."""
    spec = SEARCHERS[name]
    if name == "fd" and any(w in ("-x", "--exec", "-X", "--exec-batch") for w in words):
        words = words[:min(words.index(w) for w in words if w in ("-x", "--exec", "-X", "--exec-batch"))]
    options, positionals = scan_options(words, spec)
    if options & spec["idle"] or options & spec["depth"]:
        return []
    if not spec["always"] and not (options & {"-" + c for c in spec["recursive"]} or options & spec["recursive_long"]
                                   or bool(options & {"-d", "--directories"}) and _value(words, "-d", "--directories") == "recurse"):
        return []
    paths = positionals if options & spec["given"] else positionals[spec["first"]:]
    if name == "fd":
        base = _value(words, "--base-directory")
        if base is not None:
            cwd = expand(base, cwd, home)
        searched = _value(words, "--search-path")
        paths = paths + ([searched] if searched is not None else [])
    if not paths:
        if name == "rg" and stdin and "--files" not in options:
            return []  # ``… | rg <词>`` searches what comes down the pipe.
        return [cwd]
    return [expand(path, cwd, home) for path in paths]


def _value(words, *names):
    """The value of the first of ``names`` in ``words`` (``--x v`` or ``--x=v``); None when none is there."""
    for at, word in enumerate(words):
        for name in names:
            if word == name and at + 1 < len(words):
                return words[at + 1]
            if word.startswith(name + "="):
                return word[len(name) + 1:]
    return None


def find_targets(words, cwd, home):
    """Where ``find`` with ``words`` walks unbounded ([] with -maxdepth)."""
    if "-maxdepth" in words:
        return []
    at = 0
    while at < len(words) and (words[at] in ("-H", "-L", "-P") or words[at].startswith("-O")
                               or words[at] == "-D"):
        at += 2 if words[at] == "-D" else 1
    paths = []
    while at < len(words) and not words[at].startswith("-") and words[at] not in ("!", "(", ","):
        paths.append(words[at])
        at += 1
    return [expand(path, cwd, home) for path in paths] if paths else [cwd]


def call_args(code, open_at):
    """The top-level arguments of the call whose ``(`` is at ``open_at`` in ``code``, as text; None when it is not closed."""
    depth, quote, start, args = 0, "", open_at + 1, []
    for at in range(open_at, len(code)):
        char = code[at]
        if quote:
            if char == quote and code[at - 1] != "\\":
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                args.append(code[start:at])
                return [arg.strip() for arg in args if arg.strip()]
        elif char == "," and depth == 1:
            args.append(code[start:at])
            start = at + 1
    return None


def receiver(code, dot_at):
    """(callee, its arguments) of the call that ends right before ``.method`` at ``dot_at`` (``Path('x')`` of
    ``Path('x').rglob``); None when it is not a call (a variable)."""
    at = dot_at - 1
    while at >= 0 and code[at].isspace():
        at -= 1
    if at < 0 or code[at] != ")":
        return None
    depth = 0
    for opened in range(at, -1, -1):
        depth += {")": 1, "(": -1}.get(code[opened], 0)
        if depth == 0:
            callee = re.search(r"[\w.]*$", code[:opened]).group(0)
            return callee, call_args(code, opened) or []
    return None


def py_path(expr, cwd, home):
    """The directory a Python expression names, as far as it can tell without running it; None when it cannot."""
    expr = expr.strip()
    if expr in ("", "'.'", '"."', "''", '""', "os.curdir", "os.getcwd()", "Path.cwd()", "pathlib.Path.cwd()", "Path()",
                "pathlib.Path()", "os.path.abspath('.')", 'os.path.abspath(".")'):
        return cwd
    if re.fullmatch(r"""os\.(?:environ\[|environ\.get\(|getenv\()\s*['"]COVEY_HOME['"].*""", expr):
        return home
    literal = re.fullmatch(r"""[rRuU]?(['"])([^'"{}]*)\1""", expr)
    if literal:
        return expand(literal.group(2), cwd, home) if "$" not in literal.group(2) else None
    call = re.fullmatch(r"(?:pathlib\.)?(?:Pure|Posix)?Path\((.*)\)", expr, re.S)
    if call:
        args = call_args(expr, expr.index("("))
        return py_path(args[0] if args else "", cwd, home) if args is not None and len(args) <= 1 else None
    if expr in ("Path.home()", "pathlib.Path.home()", "os.path.expanduser('~')", 'os.path.expanduser("~")'):
        return os.path.expanduser("~")
    return None


def python_targets(code, cwd, home):
    """Where an inline Python ``code`` walks a tree: os.walk's top, rglob's receiver, a ``**`` glob's fixed part."""
    found = []
    for match in re.finditer(r"\bos\.f?walk\s*\(", code):
        args = call_args(code, match.end() - 1)
        found.append(py_path(args[0], cwd, home) if args else None)
    for match in re.finditer(r"\.(r?glob)\s*\(", code):
        method, args = match.group(1), call_args(code, match.end() - 1) or []
        pattern = re.fullmatch(r"""[rR]?(['"])([^'"]*)\1""", args[0]) if args else None
        if method == "glob" and not (pattern and "**" in pattern.group(2)):
            continue
        owner = receiver(code, match.start())
        if owner is None:  # A variable's, or glob.glob (below).
            continue
        callee, owner_args = owner
        if callee in ("Path.cwd", "pathlib.Path.cwd"):
            base = cwd
        elif callee in ("Path.home", "pathlib.Path.home"):
            base = os.path.expanduser("~")
        elif re.fullmatch(r"(?:pathlib\.)?(?:Pure|Posix)?Path", callee) and len(owner_args) <= 1:
            base = py_path(owner_args[0] if owner_args else "", cwd, home)
        else:
            base = None
        if base is not None and method == "glob":
            base = expand(pattern.group(2), base, home) if not os.path.isabs(pattern.group(2)) else None
        found.append(base)
    for match in re.finditer(r"(?<![\w.])(?:glob\.)?i?glob\s*\(", code):
        args = call_args(code, match.end() - 1) or []
        pattern = re.fullmatch(r"""[rR]?(['"])([^'"]*)\1""", args[0]) if args else None
        recursive = any(re.fullmatch(r"recursive\s*=\s*True", arg) for arg in args)  # Without it ** is *.
        if pattern and "**" in pattern.group(2) and recursive and not any(arg.startswith(("root_dir", "dir_fd"))
                                                                          for arg in args):
            found.append(expand(pattern.group(2), cwd, home))
    return found


def heredocs(command):
    """``command`` with its here-documents' bodies taken out, and the bodies in order (a ``<<`` takes the next)."""
    lines, kept, bodies, at = command.split("\n"), [], [], 0
    while at < len(lines):
        line = lines[at]
        kept.append(line)
        at += 1
        for strip, _, word in HEREDOC.findall(line):
            body = []
            while at < len(lines) and (lines[at].strip() if strip else lines[at]) != word:
                body.append(lines[at])
                at += 1
            at += 1  # Its closing word.
            bodies.append("\n".join(body))
    return "\n".join(kept), bodies


def segments(command):
    """``command``'s simple commands: (words, its input from a pipe, a redirect or a here-document, the bodies of its
    here-documents). ValueError for what shlex cannot split (an open quote)."""
    text, bodies = heredocs(FD_NUMBER.sub("", str(command).replace("\\\n", " ")))
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    out, words, piped, fed, docs, skip = [], [], False, False, [], False
    for token in lexer:
        if skip:
            skip = False
            continue
        if token in SEPARATORS or (token and set(token) <= set(";&|()\n") and "<" not in token and ">" not in token):
            if words:
                out.append((words, piped, fed, docs))
            words, piped, fed, docs = [], token in ("|", "|&"), False, []
            continue
        if token and set(token) <= set("<>&|"):  # A redirection: it and its target are no argument.
            if "<" in token:
                fed = True
                if token == "<<" and bodies:
                    docs.append(bodies.pop(0))
            skip = True
            continue
        words.append(token)
    if words:
        out.append((words, piped, fed, docs))
    return out


def command_words(words):
    """``words`` with what runs the real command left off (``FOO=1``, ``timeout 30s``, ``nice -n 5``, ``env``, ``do``);
    None when what runs takes its arguments from elsewhere (``xargs``)."""
    while words:
        head = os.path.basename(words[0].lstrip("`\\"))
        if ASSIGNMENT.match(words[0]) or words[0] in KEYWORDS:
            words = words[1:]
        elif head == "timeout":
            at = 1
            while at < len(words) and words[at].startswith("-"):
                at += 2 if words[at] in ("-k", "-s") else 1
            words = words[at + 1:]
        elif head in ("nice", "ionice", "stdbuf", "sudo", "env", "nohup", "command", "builtin", "exec", "unbuffer"):
            at = 1
            while at < len(words) and (words[at].startswith("-") or (head == "env" and ASSIGNMENT.match(words[at]))):
                at += 2 if words[at] in ("-n", "-c", "-u", "-g", "-C", "-S") else 1
            words = words[at:]
        elif head in STOP:
            return None
        else:
            break
    return words


def bash_refusal(command, cwd, home, depth=0) -> str:
    """Why a Bash ``command`` run in ``cwd`` would walk all of ``home`` unbounded; '' when it would not (or cannot tell)."""
    for words, piped, fed, docs in segments(command):
        words = command_words(words)
        if not words:
            continue
        name = os.path.basename(words[0].lstrip("`\\"))
        name, rest = ALIASES.get(name, name), [word.rstrip("`") for word in words[1:]]
        if name in ("cd", "pushd"):
            given = [word for word in rest if not word.startswith("-")] if rest[:1] != ["-"] else None
            cwd = None if given is None else expand(given[0], cwd, home) if given else os.path.expanduser("~")
            continue
        if name in SEARCHERS:
            targets = searcher_targets(name, rest, cwd, home, stdin=piped or fed)
        elif name == "find":
            targets = find_targets(rest, cwd, home)
        elif PYTHON.fullmatch(name):
            targets = python_targets(_python_code(rest, docs), cwd, home)
            if any(target and covers_home(target, home) for target in targets):
                return SEARCH_WORDS.format(what="这段 Python ", home=home, fix=PY_FIX)
            continue
        elif name in SHELLS and depth < 3:
            script = _shell_script(rest)
            reason = bash_refusal(script, cwd, home, depth + 1) if script else ""
            if reason:
                return reason
            continue
        else:
            continue
        if any(target and covers_home(target, home) for target in targets):
            return SEARCH_WORDS.format(what=f"`{name}` 这一步", home=home, fix=SHELL_FIX)
    return ""


def _python_code(words, docs) -> str:
    """The code an inline Python runs: ``-c``'s, or the here-document of ``python3 -`` (or of no script at all)."""
    at = 0
    while at < len(words) and words[at].startswith("-") and words[at] != "-":
        if words[at] == "-c":
            return words[at + 1] if at + 1 < len(words) else ""
        if words[at].startswith("-c"):
            return words[at][2:]
        if words[at] == "-m":
            return ""
        at += 2 if words[at] in ("-X", "-W") else 1
    if at >= len(words) or words[at] == "-":
        return "\n".join(docs)
    return ""


def _shell_script(words) -> str:
    """The script of ``bash -c '<script>'`` (``-lc`` and the like too); '' for a script file or none."""
    for at, word in enumerate(words):
        if not word.startswith("-"):
            return ""
        if not word.startswith("--") and "c" in word[1:]:
            return words[at + 1] if at + 1 < len(words) else ""
    return ""


def search_refusal(call, home) -> str:
    """Why the PreToolUse ``call`` (Claude Code's hook input) walks all of ``home`` unbounded; '' when it does not or
    when it cannot tell (any error included: home-search never refuses for its own fault)."""
    try:
        tool = str(call.get("tool_name") or "")
        given = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
        cwd = str(call.get("cwd") or "") or os.getcwd()
        if not home or tool not in SEARCH_HOOKED:
            return ""
        if tool == "Bash":
            return bash_refusal(str(given.get("command") or ""), cwd, str(home))
        if tool == "Grep":
            target = expand(str(given.get("path") or "."), cwd, str(home))
            fix = GREP_FIX
        else:
            pattern = str(given.get("pattern") or "")
            if "**" not in pattern:
                return ""  # A fixed number of levels.
            base = expand(str(given.get("path") or "."), cwd, str(home))
            target = expand(pattern, base, str(home)) if base else None
            fix = GLOB_FIX
        if target and covers_home(target, str(home)):
            return SEARCH_WORDS.format(what=f"{tool} 这次", home=home, fix=fix)
    except Exception:  # noqa: BLE001 - home-search lets through what it cannot read
        return ""
    return ""


# ---- look: a LOOK turn on this machine (module docstring) ----

LOOK_MODE = "look"
SHIM_MODE = "shim"
MODES = (PROXY_MODE, HOME_SEARCH, LOOK_MODE, SHIM_MODE)
SHIM_REFUSED = 3  # The exit code of ``shim_main`` for a search it refuses (the shim turns it into 2; Python's own 1 and 2 are faults).
LOOK_HOOKED = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit")
# Programs that write each of their path arguments (``first`` leading ones are not paths: chmod's mode, chown's owner).
WRITES_EACH = {"rm": 0, "rmdir": 0, "unlink": 0, "touch": 0, "mkdir": 0, "truncate": 0, "shred": 0, "chmod": 1,
               "chown": 1, "chgrp": 1}
WRITES_LAST = ("cp", "install", "ln", "rsync")  # The last path argument (or ``-t <目录>``) is written.
# Options of those that take the next word as their value.
VALUED = {"-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g", "--group", "-s", "--size", "-S",
          "--suffix", "-n", "--iterations", "-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--reference"}
GIT_WRITES = frozenset({"commit", "checkout", "switch", "reset", "push", "pull", "clean", "merge", "rebase", "cherry-pick",
                        "revert", "am", "apply", "restore", "rm", "mv", "init", "clone", "gc", "prune", "update-ref",
                        "update-index", "notes", "filter-branch", "replace", "submodule", "lfs"})
GIT_GLOBAL_VALUES = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
DEVICE_TARGETS = re.compile(r"/dev/(null|stdout|stderr|tty|fd/\d+)$")
LOOK_FIX = "这一轮只读：{what}。只能写本轮检查点 {file} 和本话题产物目录 {dir}；要改别处的，在回复里写清楚改什么，等主人下一句。"


def look_settings(allow_file, allow_dir, *, python=None, script=None) -> dict:
    """The ``hooks`` of a LOOK turn: this module's ``main`` in its look mode before every call of LOOK_HOOKED, with the
    turn's checkpoint file and the topic's outputs directory."""
    if script is None:
        from covey.core.paths import entry_script
        script = entry_script("covey_tool_guard.py")
    command = shlex.join([python or sys.executable, "-B", str(script), "--mode", LOOK_MODE,
                          "--allow-file", str(allow_file or ""), "--allow-dir", str(allow_dir or "")])
    return {"PreToolUse": [{"matcher": "|".join(LOOK_HOOKED), "hooks": [{"type": "command", "command": command}]}]}


def look_segments(command):
    """``command``'s simple commands as ``segments`` reads them, each with the files its redirections write:
    (words, written targets, the bodies of its here-documents). ValueError for what shlex cannot split, or a redirection
    with no target."""
    text, bodies = heredocs(FD_NUMBER.sub("", str(command).replace("\\\n", " ")))
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    out, words, targets, docs, pending = [], [], [], [], None
    for token in lexer:
        if pending is not None:
            if token in SEPARATORS or (token and set(token) <= set("();<>|&\n")):
                raise ValueError("a redirection without its target")
            if pending == "write" and not token.isdigit() and token != "-":
                targets.append(token)
            pending = None
            continue
        if token in SEPARATORS or (token and set(token) <= set(";&|()\n") and "<" not in token and ">" not in token):
            if words or targets:
                out.append((words, targets, docs))
            words, targets, docs = [], [], []
            continue
        if token and set(token) <= set("<>&|"):  # A redirection: its target is the next word.
            if ">" in token:
                pending = "dup" if token.endswith("&") and token not in ("&>", "&>>") else "write"
            else:
                pending = "read"
                if token == "<<" and bodies:
                    docs.append(bodies.pop(0))
            continue
        words.append(token)
    if pending is not None:
        raise ValueError("a redirection without its target")
    if words or targets:
        out.append((words, targets, docs))
    return out


def _positionals(words, valued=VALUED):
    """``words``' arguments that are no option nor an option's value; and the value of ``-t``/``--target-directory``."""
    found, target, at, done = [], None, 0, False
    while at < len(words):
        word = words[at]
        at += 1
        if done or not word.startswith("-") or word == "-":
            found.append(word)
            continue
        if word == "--":
            done = True
            continue
        name = word.split("=", 1)[0]
        value = word.split("=", 1)[1] if "=" in word else None
        if name in valued and value is None:
            value = words[at] if at < len(words) else None
            at += 1
        if name in ("-t", "--target-directory"):
            target = value
    return found, target


def _git_refusal(words) -> str:
    """Why the git command ``words`` (after ``git``) writes; '' when it only reads."""
    at = 0
    while at < len(words) and words[at].startswith("-"):
        at += 2 if words[at] in GIT_GLOBAL_VALUES else 1
    if at >= len(words):
        return ""
    sub, rest = words[at], words[at + 1:]
    if sub in GIT_WRITES:
        return f"`git {sub}` 会改仓库"
    if sub == "stash" and (rest[:1] not in (["list"], ["show"])):
        return "`git stash` 会改工作区"
    if sub == "worktree" and rest[:1] != ["list"]:
        return "`git worktree` 会改仓库"
    if sub in ("branch", "tag"):
        flags = {word.split("=", 1)[0] for word in rest if word.startswith("-")}
        listing = flags & {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--contains", "--merged",
                           "--no-merged", "--points-at", "--show-current", "-n", "--sort", "--format", "--column"}
        writing = flags & {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                           "--set-upstream-to", "--unset-upstream", "--edit-description", "-s", "--sign"}
        named = [word for word in rest if not word.startswith("-")]
        if writing or (named and not listing):
            return f"`git {sub}` 加名字会改仓库"
    if sub == "config" and not ({word for word in rest} & {"--get", "--get-all", "--list", "-l", "--get-regexp", "--show-origin"}):
        if len([word for word in rest if not word.startswith("-")]) >= 2:
            return "`git config` 写配置"
    return ""


DEVICE_READS = ("list", "get", "ls", "stat", "--help", "-h")  # What ``covey device`` may do in a LOOK turn.


def look_bash_refusal(command, cwd, allow_file, allow_dir, depth=0) -> str:
    """Why a Bash ``command`` run in ``cwd`` in a LOOK turn obviously writes somewhere it may not (module docstring);
    '' when it does not, as far as it can see. ValueError when it cannot read the command."""
    from covey.core import env as cenv
    from covey.core import look_write

    home = cenv.get("COVEY_HOME", "")

    def blocked(target, base) -> str:
        if target is None:
            return "写入位置看不出来（变量、命令的输出或相对路径），写到产物目录里请用绝对路径"
        if DEVICE_TARGETS.match(target):
            return ""
        reason = look_write.refusal(target, allow_file=allow_file, allow_dir=allow_dir, cwd=base or "/nonexistent-cwd")
        return f"写 `{target}`，不在可写的地方" if reason else ""

    def where(word, base):
        if not word or (not home and re.search(r"\$\{?COVEY_HOME", word)):
            return None
        return expand(word, base, home)

    for words, targets, docs in look_segments(command):
        for target in targets:
            reason = blocked(where(target, cwd), cwd)
            if reason:
                return LOOK_FIX.format(what=f"重定向{reason}", file=allow_file, dir=allow_dir)
        words = command_words(words) if words else words
        if words is None:  # xargs, parallel, watch: what they run is in the words after them.
            continue_words = [w for w in words_after_stop(command) if os.path.basename(w) in
                              (*WRITES_EACH, *WRITES_LAST, "mv", "tee", "sed", "perl", "dd", "git")]
            if continue_words:
                return LOOK_FIX.format(what=f"`xargs {continue_words[0]}` 写什么看不出来", file=allow_file, dir=allow_dir)
            continue
        if not words:
            continue
        name = os.path.basename(words[0].lstrip("`\\"))
        rest = [word.rstrip("`") for word in words[1:]]
        if name in ("cd", "pushd"):
            given = [word for word in rest if not word.startswith("-")] if rest[:1] != ["-"] else None
            cwd = None if given is None else where(given[0], cwd) if given else os.path.expanduser("~")
            continue
        found = ""
        if name == "git":
            found = _git_refusal(rest)
        elif name == "covey" and rest[:2] == ["repo", "use"] and "--write" in rest:
            found = "`covey repo use --write` 会建写 worktree"
        elif name == "covey" and rest[:1] == ["device"] and (rest[1:2] or [""])[0] not in DEVICE_READS:
            # The device channel holds a LOOK turn to reading itself (core/device_access.py G3); this is the second wall.
            found = f"`covey device {(rest[1:2] or [''])[0]}` 可能改设备上的东西，这一轮只能 list、get、ls、stat"
        elif name == "tee":
            for path in _positionals(rest)[0]:
                found = found or blocked(where(path, cwd), cwd)
        elif name in ("sed", "perl") and any(word == "--in-place" or re.fullmatch(r"-[a-zA-Z]*i\S*", word) for word in rest):
            given = [word for word in rest if word.startswith(("-e", "--expression", "-f", "--file"))]
            paths = _positionals(rest, valued=VALUED | {"-e", "--expression", "-f", "--file"})[0]
            for path in paths[0 if given else 1:]:
                found = found or blocked(where(path, cwd), cwd)
        elif name in WRITES_EACH:
            paths = _positionals(rest)[0][WRITES_EACH[name]:]
            for path in paths:
                found = found or blocked(where(path, cwd), cwd)
        elif name == "mv":
            paths, target = _positionals(rest)
            for path in paths + ([target] if target else []):
                found = found or blocked(where(path, cwd), cwd)
        elif name in WRITES_LAST:
            paths, target = _positionals(rest)
            dest = target or (paths[-1] if len(paths) >= 2 else None)
            if dest is not None or paths:
                found = blocked(where(dest, cwd) if dest is not None else None, cwd)
        elif name == "dd":
            for word in rest:
                if word.startswith("of="):
                    found = found or blocked(where(word[3:], cwd), cwd)
        elif name == "curl":
            for at, word in enumerate(rest):
                if word in ("-o", "--output") and at + 1 < len(rest):
                    found = found or blocked(where(rest[at + 1], cwd), cwd)
                elif word.startswith("--output="):
                    found = found or blocked(where(word.split("=", 1)[1], cwd), cwd)
                elif word in ("-O", "--remote-name", "--remote-name-all"):
                    found = found or blocked(where("curl-download", cwd), cwd)
        elif name == "wget":
            value = _value(rest, "-O", "--output-document")
            folder = _value(rest, "-P", "--directory-prefix")
            if value != "-":
                found = blocked(where(value, cwd) if value else where(os.path.join(folder or ".", "wget-download"), cwd), cwd)
        elif name == "find":
            if "-delete" in rest:
                found = "`find -delete` 会删文件"
            for at, word in enumerate(rest):
                if word in ("-exec", "-execdir", "-ok", "-okdir") and at + 1 < len(rest):
                    if os.path.basename(rest[at + 1]) in (*WRITES_EACH, *WRITES_LAST, "mv", "tee", "sed", "perl", "git"):
                        found = found or f"`find {word} {rest[at + 1]}` 会改文件"
        elif name in SHELLS and depth < 3:
            script = _shell_script(rest)
            reason = look_bash_refusal(script, cwd, allow_file, allow_dir, depth + 1) if script else ""
            if reason:
                return reason
        if found:
            return LOOK_FIX.format(what=found, file=allow_file, dir=allow_dir)
    return ""


def words_after_stop(command) -> list:
    """The words after the first ``xargs``/``parallel``/``watch`` of ``command`` (what it runs)."""
    try:
        words = shlex.split(str(command))
    except ValueError:
        return []
    at = next((index for index, word in enumerate(words) if os.path.basename(word) in STOP), None)
    return [word for word in words[at + 1:] if not word.startswith("-")] if at is not None else []


def look_refusal(call, allow_file, allow_dir) -> str:
    """Why the PreToolUse ``call`` does not run in a LOOK turn; '' when it does. Raises for what it cannot read (``main``
    refuses then)."""
    from covey.core import look_write

    tool = str(call.get("tool_name") or "")
    given = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
    cwd = str(call.get("cwd") or "")
    if tool == "Bash":
        return look_bash_refusal(str(given.get("command") or ""), cwd or os.getcwd(), allow_file, allow_dir)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = given.get("notebook_path" if tool == "NotebookEdit" else "file_path")
        return look_write.refusal(path, allow_file=allow_file, allow_dir=allow_dir, cwd=cwd)
    return ""


def shim_refusal(name, words, cwd, home, *, stdin=False) -> str:
    """Why the command ``name`` (a search shim's, core/home.py ``SEARCH_SHIMS``) with ``words`` run in ``cwd`` would walk
    all of ``home`` unbounded; '' when it would not or when it cannot tell (any error included)."""
    try:
        if not home or not cwd:
            return ""
        name = ALIASES.get(name, name)
        if name == "find":
            targets = find_targets(words, cwd, home)
        elif name in SEARCHERS:
            targets = searcher_targets(name, words, cwd, home, stdin=stdin)
        else:
            return ""
        if any(target and covers_home(target, home) for target in targets):
            return SEARCH_WORDS.format(what=f"`{name}`", home=home, fix=SHELL_FIX)
    except Exception:  # noqa: BLE001 - the shim lets through what it cannot read
        return ""
    return ""


def shim_main(argv) -> int:
    """``--mode shim <命令名> <参数…>`` (the search shims in <数据根>/bin, core/home.py): the home is ``$COVEY_HOME``, the
    directory the process's cwd. ``SHIM_REFUSED`` (the reason on stderr) to refuse, 0 to run it; a fault never refuses."""
    try:
        import stat
        home, cwd = os.environ.get("COVEY_HOME", ""), os.getcwd()
        mode = os.fstat(0).st_mode
        fed = stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode)  # What rg takes for input to search.
        reason = shim_refusal(argv[0], argv[1:], cwd, home, stdin=fed) if argv else ""
    except Exception:  # noqa: BLE001
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return SHIM_REFUSED
    return 0


def main(argv=None, stdin=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:2] == ["--mode", SHIM_MODE]:  # Its arguments are another command's: argparse must not read them.
        return shim_main(argv[2:])
    import argparse  # Not at the top: a search shim starts this module on every ``rg`` and wants it light.
    import json
    parser = argparse.ArgumentParser(prog="covey_tool_guard.py",
                                     description="Claude Code's PreToolUse hook: the call's JSON on stdin, exit 2 refuses it.")
    parser.add_argument("--mode", choices=MODES, default=PROXY_MODE,
                        help="proxy: a 代答 turn's (the default); home-search: a turn started from the home; look: a LOOK turn's")
    parser.add_argument("--notes", default="", help="the contact's notes directory, the one place edit tools may write")
    parser.add_argument("--home", default="", help="home-search: the home whose whole tree no search may walk")
    parser.add_argument("--allow-file", default="", help="look: the turn's checkpoint file")
    parser.add_argument("--allow-dir", default="", help="look: the topic's outputs directory")
    home_search = any(HOME_SEARCH in arg for arg in argv)
    try:
        args = parser.parse_args(argv)  # An argument it does not take exits 2, which refuses the call in proxy mode.
    except SystemExit:
        if home_search:
            return 0
        raise
    if args.mode == LOOK_MODE:
        try:
            call = json.load(stdin or sys.stdin)
            reason = look_refusal(call if isinstance(call, dict) else {}, args.allow_file, args.allow_dir)
        except Exception as exc:  # noqa: BLE001 - a LOOK gate that cannot tell refuses
            reason = (f"这一轮只读：检查这次调用出错（{type(exc).__name__}），没有执行。命令写简单些"
                      f"（一条一条跑、写入用绝对路径），只写本轮检查点 {args.allow_file} 和产物目录 {args.allow_dir}。")
    elif args.mode == HOME_SEARCH:
        try:
            call = json.load(stdin or sys.stdin)
            reason = search_refusal(call, args.home) if isinstance(call, dict) else ""
        except Exception:  # noqa: BLE001 - home-search lets through what it cannot read
            reason = ""
    else:
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

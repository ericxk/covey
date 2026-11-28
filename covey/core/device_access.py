"""The device channel (设备通道): what a topic's Harness does from the home on a device of devices.json, in one place.

Every Harness reaches it the same way, ``covey device`` from its shell (bin/covey_device.py, through ``<数据根>/bin/covey``):
``list``, ``run``, ``ls``, ``stat``, ``get``, ``put`` and ``job start|log|status|stop``; a turn held to reading whose
shell cannot run it has the reading ones as tools of its ``bridge_workspace`` server instead (core/device_tools.py). The
Harness stays on the hub, so rewind, compaction and the Bridge's tools work as on any turn; a device needs Python 3.9 and
ssh keys only (docs/architecture.md 设备通道). The guards are written here and nowhere else:

- **G1 名单** (``Channel.device``): an alias devices.json lists with ``ssh``, used from a chat whose space is one of the
  device's ``spaces``. The space is app/access.py ``device_space`` (rule 11): the turn's start writes it with the turn's
  limit into ``runs/<任务键>/device-turn.json`` (``note_turn``), since a command has no Access of its own; a 代答 group
  or a chat the Bridge does not serve has none, and nothing is allowed there. A task whose turn wrote no such file (a
  runner of an older release) is refused too. Outside a task (no ``COVEY_TASK_DIR``: a person at the hub's terminal)
  the owner's private chat is assumed.
- **G2 只读设备** (``access`` other than ``write``; a device the page added is ``read``): ``list``, ``ls``, ``stat``,
  ``get`` and ``run`` of an argv ``read_only_refusal`` lets through (READ_COMMANDS, never a shell); ``put`` and ``job``
  are refused.
- **G3 只读轮**: a turn held to reading (core/harness.py TOOL_LIMIT: look, lead, read, consult, proxy) treats every device
  as G2 does, and ``get`` writes only into the topic's outputs directory then.
- **G4 危险命令** (``danger``, ``approval``): a ``run`` or ``job start`` that deletes a tree, kills processes, stops or
  restarts a service, reboots, formats a disk, removes containers, forces a push, resets hard or uninstalls a package
  (wrappers like sudo, env, timeout, xargs and ``sh -c`` scripts read through) is refused until the owner said yes: the
  refusal tells the Harness to ask with a question card first and run it again with ``--approved <答案号>``, which must
  name a question of this very topic the owner himself answered (``approval``: the Bridge's question record, its topic,
  status, ``answer_by`` and answer). The basis goes into the audit line (``approved``). A read-only device or turn
  refuses such a command before G4 is asked (G2, G3).
- **G5 凭据**: the command on the device gets devices.py ``effective_env`` with the Bridge's names and anything named
  like a secret dropped (``device_env``), never the hub's environment; the ssh client runs with the hub's environment
  less the same names (``ssh_env``); ``get`` and ``put`` refuse a path through SECRET_NAMES, ``get`` the path the device
  resolves it to too (a link to ``~/.ssh``), and so does a read-only ``run`` naming one.
- **G6 审计**: one JSON line per call in ``runs/<任务键>/devices.jsonl`` (``audit``; ``<state>/devices/terminal.jsonl``
  outside a task): when, device, op, the command (secrets hidden, core/text.py ``redact``), directory, exit code, bytes,
  seconds. A ``run`` writes a ``start`` line before it asks the device, so one cut short (its command's own timeout
  killed ``covey device``) is still known at the turn's end.
- **G7 收割**: a ``run`` carries the turn's mark (``mark_of``: the topic and the task); the device keeps the command's
  process group under it (core/device_shim.py ``exec``). The turn's end (``reap_turn``, app/repos_turn.py
  ``settle_devices``) reaps the marks of the runs that never answered; a device that cannot be reached then gets an
  entry in ``pending-reap.json`` (core/device_reap.py), which the devices thread replays.

``job`` (主人 DSAZJ): a command the device program supervises (its ``spawn``: double fork, its own process group, output
in the task's journal), so it outlives the call, the ssh connection and the turn. ``job log`` reads the journal from a byte
offset and says the next one; ``job stop`` stops the group. A turn the owner stopped stops the jobs it started
(``reap_turn`` with ``stop_jobs``); any other ending leaves them running.

``fold`` and ``trace_text`` read a task's audit for its topic (``topic.json`` ``devices``) and its task card. Only core
modules and the standard library; nothing here starts a thread.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import time
from pathlib import Path

import covey.core.env as cenv
from covey.core import device_reap, devices
from covey.core import device_transport as dt
from covey.core.harness import CONSULT, LEAD, PROXY, READ
from covey.core.run_dir import DEVICE_TURN, DEVICES
from covey.core.state import atomic_write_bytes, ensure_private_dir, read_object, write_json
from covey.core.text import redact

# Turns held to reading: every device is read-only to them (G3). "look": an older release's read-only first turn (gone since
# T956) still running across an upgrade, whose turn file says so.
READ_LIMITS = (LEAD, READ, CONSULT, PROXY, "look")
TERMINAL_SPACE = "owner_private"  # Outside a task: the hub's own terminal, its deployer.
RUN_SECONDS = 120.0  # A run's timeout unless --timeout says another (the device program's exec takes up to 3600).
RUN_MAX = 3600.0
OUT_MAX = 1 << 20  # Of a run's output, stdout and stderr together.
GET_MAX = 64 << 20  # One get brings at most this much.
PUT_MAX = 64 << 20
JOB_LOG_MAX = 1 << 20  # One job log read.
SHA_SECONDS = 30.0
COMMAND_SHOWN = 400  # Characters of a command an audit line keeps.
TERMINAL_AUDIT = "terminal.jsonl"
# A device's process never gets these from the hub (G5), whatever devices.json or the profile says.
BRIDGE_PREFIXES = ("LARK_", "FEISHU_", "COVEY_")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|AUTH", re.I)
KEPT_SECRET_LOOKING = ("SSH_AUTH_SOCK",)  # The ssh client's agent: a socket path, no secret in it.
# What ``get`` and ``put`` never touch, and a read-only ``run`` never names (G5): app/artifact_policy.py SENSITIVE_NAMES
# and the other places keys and logins live.
SECRET_NAMES = (".git", ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".codex", ".claude", ".gemini", ".config/gh",
                "credentials.json", "auth.json", ".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc")
SECRET_GLOBS = (".env", ".env.*", "*.env", "id_rsa*", "id_ecdsa*", "id_ed25519*", "id_dsa*", "*.pem", "*.key", "*.p12",
                "*.pfx", "*.keystore")
# G2: the commands a read-only device or turn may run (argv, never a shell); each its refusal of the arguments.
READ_COMMANDS = ("ls", "cat", "head", "tail", "wc", "stat", "file", "find", "git", "ps", "df", "free", "uptime", "uname",
                 "nvidia-smi", "hostname", "whoami", "id", "date", "pwd", "du", "realpath", "readlink", "md5sum",
                 "sha256sum", "sha1sum", "grep", "rg", "diff", "sw_vers")
GIT_READ = ("status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "describe", "blame", "shortlog")
FIND_ACTIONS = ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
FIND_DEPTH = 4
NVIDIA_READ = re.compile(r"(-L|--list-gpus|-q|--query|--query-[a-z-]+=.*|--format=.*|-i|--id=.*|-d|--display=.*|"
                         r"topo|-m|--matrix|-u|--unit|-x|--xml-format|[0-9a-z,_.]+)\Z")


class Refused(Exception):
    """A guard said no (G1-G3, G5, a file that changed since ``get``): ``str`` is the reason, in words for the model."""


@dataclasses.dataclass(frozen=True)
class Turn:
    """Who asks: the state directory, the task's run directory (None at the hub's terminal) and what the turn's start
    wrote of it (``note_turn``): its limit, its chat's device space, the topic and its outputs directory."""
    state_dir: Path
    task_dir: Path | None = None
    topic: str = ""
    limit: str = ""
    space: str = ""
    outputs: str = ""
    known: bool = True  # False: a task whose start wrote no device-turn.json (G1 refuses it everything).

    @property
    def held(self) -> bool:
        """Whether the turn may only read (G3): held to reading by its limit, or not known."""
        return self.limit in READ_LIMITS or not self.known


def listed(environ=None) -> bool:
    """Whether the channel is there: COVEY_DEVICES on and devices.json listing at least one device (with ssh or only
    watched). Off, or on with none listed (or a devices.json that cannot be read), a turn is as before (core/devices.py:
    a switch on with no device behaves as off): no turn file, no line in the rules."""
    if not devices.enabled(environ):
        return False
    try:
        config = devices.current(environ)
    except devices.DeviceConfigError:
        return False
    return bool(config.devices or config.watched)


def note_turn(task_dir, *, limit="", space="", topic="", outputs="") -> Path | None:
    """Write what ``covey device`` needs of a turn into its run directory (``device-turn.json``): its limit
    (core/harness.py TOOL_LIMIT, '' none), its chat's device space (app/access.py ``device_space``, '' none), the topic
    and its outputs directory. The path, None when there is no run directory or it cannot be written."""
    if not task_dir:
        return None
    path = Path(task_dir) / DEVICE_TURN
    try:
        write_json(path, {"version": 1, "limit": str(limit or ""), "space": str(space or ""), "topic": str(topic or ""),
                          "outputs": str(outputs or ""), "at": time.time()}, ensure_ascii=False)
    except OSError:
        return None
    return path


def turn_from_env(environ=None) -> Turn:
    """The ``Turn`` of the process ``covey device`` runs in: ``COVEY_STATE``, ``COVEY_TASK_DIR`` and its turn file
    (``note_turn``), or the hub's terminal when there is no task. ``Refused`` without a state directory."""
    state = str(cenv.get("COVEY_STATE", "", environ) or "").strip()
    if not state:
        raise Refused("不在 Covey 的任务里（没有 COVEY_STATE），设备通道用不了")
    named = str(cenv.get("COVEY_TASK_DIR", "", environ) or "").strip()
    topic = str(cenv.get("COVEY_TOPIC", "", environ) or "").strip()
    if not named:
        return Turn(Path(state), None, topic=topic, space=TERMINAL_SPACE)
    return turn_of(state, named, topic)


def turn_of(state_dir, task_dir, topic="") -> Turn:
    """The ``Turn`` of the task whose run directory is ``task_dir``, as its turn file (``note_turn``) says it; not known
    (G1 refuses it everything) when its start wrote none."""
    data = read_object(Path(task_dir) / DEVICE_TURN)
    if not data:
        return Turn(Path(state_dir), Path(task_dir), topic=topic, known=False)
    return Turn(Path(state_dir), Path(task_dir), topic=str(data.get("topic") or topic), limit=str(data.get("limit") or ""),
                space=str(data.get("space") or ""), outputs=str(data.get("outputs") or ""))


def mark_of(turn: Turn) -> str:
    """The turn's mark on a device (the device program's ``exec`` ``mark``): the topic's first 8 characters and a hash of
    its task; a terminal's run has none ('')."""
    if turn.task_dir is None:
        return ""
    topic = re.sub(r"[^A-Za-z0-9]", "", turn.topic)[:8] or "x"
    return "t%s-%s" % (topic, hashlib.sha256(turn.task_dir.name.encode("utf-8")).hexdigest()[:10])


# ---------------------------------------------------------------- the guards' pieces


def secret_path(path) -> str:
    """The part of ``path`` (a device's path, as given) that SECRET_NAMES or SECRET_GLOBS names, '' when none does."""
    import fnmatch
    text = str(path or "")
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".", "~")]
    joined = "/".join(parts)
    for name in SECRET_NAMES:
        if "/" in name and ("/" + name + "/") in ("/" + joined + "/"):
            return name
    for part in parts:
        if part in SECRET_NAMES or any(fnmatch.fnmatchcase(part, glob) for glob in SECRET_GLOBS):
            return part
    return ""


def read_only_refusal(argv) -> str:
    """Why ``argv`` is not a read-only command (G2), '' when it is: its program one of READ_COMMANDS and its arguments
    that program's reading ones; a word naming a secret (``secret_path``) is refused too."""
    if not argv:
        return "没有要跑的命令"
    name = posixpath.basename(argv[0])
    rest = list(argv[1:])
    if name not in READ_COMMANDS:
        return f"只读时只能跑只读命令（{' '.join(READ_COMMANDS[:14])} 等，全表见 covey device --help），`{name}` 不在里面"
    for word in rest:
        found = secret_path(word.split("=", 1)[-1])
        if found:
            return f"`{found}` 里是钥匙或登录信息，不读"
    if name == "git":
        return _git_read(rest)
    if name == "find":
        return _find_read(rest)
    if name == "tail" and any(word in ("-f", "-F", "--follow") or word.startswith("--follow=")
                              or re.fullmatch(r"-[a-zA-Z0-9]*[fF][a-zA-Z0-9]*", word) for word in rest):
        return "`tail -f` 不会自己结束；要看一直在长的输出用 covey device job，或去掉 -f"
    if name == "nvidia-smi" and any(not NVIDIA_READ.match(word) for word in rest):
        return "nvidia-smi 只读时只能查（-L、-q、--query-gpu=…、--format=…、-i）"
    if name == "file" and any(word == "-C" or word.startswith("--compile") for word in rest):
        return "`file -C` 会写文件"
    if name == "rg" and any(word == "--pre" or word.startswith("--pre=") for word in rest):
        return "`rg --pre` 会执行命令"
    if name == "hostname" and any(not word.startswith("-") for word in rest):
        return "`hostname <名字>` 会改主机名"
    if name == "date" and any(word in ("-s", "--set") or word.startswith("--set=") or word[:1].isdigit() for word in rest):
        return "`date -s` 会改时间"
    return ""


def _git_read(rest) -> str:
    at = 0
    while at < len(rest) and rest[at].startswith("-"):
        if rest[at] in ("-C", "--git-dir", "--work-tree") and at + 1 < len(rest):
            at += 2
            continue
        if rest[at] in ("--no-pager", "-P", "--no-optional-locks") or rest[at].startswith(("--git-dir=", "--work-tree=")):
            at += 1
            continue
        return f"git 只读时不收 `{rest[at]}`"
    sub = rest[at] if at < len(rest) else ""
    if sub not in GIT_READ:
        return f"git 只读时只能 {' / '.join(GIT_READ)}，`git {sub or ''}`".rstrip() + " 不行"
    args = rest[at + 1:]
    if sub in ("diff", "log", "show") and any(word == "--output" or word.startswith("--output=") or word == "-o"
                                               for word in args):
        return f"`git {sub} --output` 会写文件"
    if sub == "branch":
        listing = ("-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l", "--show-current",
                   "--no-color", "--color")
        valued = ("--contains", "--no-contains", "--merged", "--no-merged", "--sort", "--points-at", "--format")
        skip = False
        for word in args:
            if skip:
                skip = False
                continue
            if word in valued:
                skip = True
                continue
            if word in listing or word.startswith(tuple(v + "=" for v in valued)):
                continue
            if "--list" in args or "-l" in args:
                continue  # A pattern of ``--list``.
            return f"`git branch {word}` 会改分支；只读时只能列分支"
    return ""


def _find_read(rest) -> str:
    for word in rest:
        if word in FIND_ACTIONS:
            return f"`find {word}` 会执行命令或写文件"
    if "-maxdepth" not in rest:
        return f"find 只读时要带 -maxdepth（不超过 {FIND_DEPTH}）"
    at = rest.index("-maxdepth")
    try:
        depth = int(rest[at + 1])
    except (IndexError, ValueError):
        return "find 的 -maxdepth 后面要跟数字"
    return "" if 0 <= depth <= FIND_DEPTH else f"find 只读时 -maxdepth 不超过 {FIND_DEPTH}"


def device_env(device, profile) -> dict:
    """The environment a command on ``device`` gets from the hub (G5): devices.py ``effective_env`` (the profile's proxy
    variables, devices.json's ``env`` over them) without the Bridge's names or anything named like a secret."""
    return {name: value for name, value in devices.effective_env(device, profile).items()
            if not name.startswith(BRIDGE_PREFIXES) and not SECRET_NAME.search(name)}


def ssh_env(environ=None) -> dict:
    """The hub's environment for the ssh client (G5): everything ssh may need (HOME, PATH, the agent's socket, a
    ProxyCommand's), without the Bridge's names or anything named like a secret, so ``SendEnv`` cannot carry them."""
    source = os.environ if environ is None else environ
    return {name: value for name, value in source.items()
            if not name.startswith(BRIDGE_PREFIXES)
            and (name in KEPT_SECRET_LOOKING or not SECRET_NAME.search(name))}


# ---------------------------------------------------------------- G4 危险命令


# A word of a shell script that only joins or opens commands (``danger`` splits a script at these).
SHELL_JOINS = frozenset({";", ";;", "&", "&&", "|", "||", "|&", "(", ")", "{", "}", "!", "if", "then", "else", "elif",
                         "fi", "do", "done", "while", "until", "case", "esac", "time"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish"})
# A program that runs the command after its own options: name -> (its options that take a value, positional words before
# the command: timeout's duration, chrt's priority, taskset's mask).
WRAPPERS = {
    "sudo": (frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user", "--group", "--host",
                        "--prompt", "--close-from", "--chdir", "--role", "--type", "--other-user", "--command-timeout"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "env": (frozenset({"-u", "-C", "--unset", "--chdir"}), 0),
    "nohup": (frozenset(), 0), "setsid": (frozenset(), 0), "unbuffer": (frozenset(), 0), "busybox": (frozenset(), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "ionice": (frozenset({"-c", "-n", "--class", "--classdata"}), 0),
    "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "time": (frozenset({"-f", "-o", "--format", "--output"}), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "command": (frozenset(), 0), "builtin": (frozenset(), 0), "exec": (frozenset({"-a"}), 0),
    "xargs": (frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs", "--max-lines",
                         "--max-chars", "--delimiter", "--eof", "--arg-file", "--replace"}), 0),
    "chrt": (frozenset(), 1), "taskset": (frozenset(), 1),
    "caffeinate": (frozenset({"-t", "-w"}), 0),
}
SSH_VALUED = frozenset("-B -b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w".split())
SYSTEMCTL_STOPS = frozenset({"stop", "restart", "try-restart", "reload-or-restart", "try-reload-or-restart", "condrestart",
                             "kill", "disable", "mask", "isolate", "reboot", "poweroff", "halt", "kexec", "emergency",
                             "rescue", "suspend", "hibernate", "hybrid-sleep", "soft-reboot"})
SYSTEMCTL_VALUED = frozenset({"-H", "--host", "-M", "--machine", "-t", "--type", "-s", "--signal", "-p", "--property",
                              "--root", "-n", "--lines", "-o", "--output", "--state"})
LAUNCHCTL_STOPS = frozenset({"stop", "unload", "bootout", "disable", "kill", "remove", "reboot", "halt"})
SERVICE_STOPS = frozenset({"stop", "restart", "try-restart", "force-reload", "condrestart"})
POWER = frozenset({"reboot", "shutdown", "poweroff", "halt"})
DISKS = frozenset({"mkfs", "mke2fs", "mkswap", "wipefs", "shred", "fdisk", "sfdisk", "gdisk", "sgdisk", "parted"})
KILLERS = frozenset({"pkill", "killall", "skill", "xkill"})
CONTAINERS = frozenset({"docker", "podman", "nerdctl"})
CONTAINER_STOPS = frozenset({"rm", "rmi", "stop", "kill", "restart", "prune", "remove", "down"})
CONTAINER_GROUPS = frozenset({"container", "image", "volume", "network", "system", "builder", "compose", "buildx", "pod"})
CONTAINER_VALUED = frozenset({"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "-f", "--file", "-p",
                              "--project-name"})
# Uninstalling (or reinstalling) a package: program -> its subcommands that do (``-R…`` options for pacman, dpkg below).
UNINSTALLS = {
    "apt": {"remove", "purge", "autoremove", "autopurge", "reinstall"},
    "apt-get": {"remove", "purge", "autoremove", "autopurge", "reinstall"},
    "aptitude": {"remove", "purge", "reinstall"},
    "yum": {"remove", "erase", "reinstall", "autoremove"}, "dnf": {"remove", "erase", "reinstall", "autoremove"},
    "microdnf": {"remove", "reinstall"}, "zypper": {"remove", "rm"}, "apk": {"del"}, "snap": {"remove"},
    "flatpak": {"uninstall", "remove"}, "brew": {"uninstall", "remove", "rm", "reinstall", "autoremove"},
    "port": {"uninstall"}, "pip": {"uninstall"}, "pip3": {"uninstall"},
    "pipx": {"uninstall", "uninstall-all", "reinstall", "reinstall-all"},
    "conda": {"remove", "uninstall"}, "mamba": {"remove", "uninstall"}, "micromamba": {"remove", "uninstall"},
}
# Only with -g / --global: a project's own packages are its build's business.
NODE_UNINSTALLS = {"npm": {"uninstall", "remove", "rm", "r", "un", "unlink"}, "pnpm": {"remove", "rm", "uninstall", "un"},
                   "yarn": {"remove"}}
DEPTH_MAX = 8  # Shells within shells: deeper than this is taken as one to ask about.
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)


def _short_has(word, letters) -> bool:
    """Whether ``word`` is a cluster of short options (``-rf``) with one of ``letters``."""
    return word.startswith("-") and not word.startswith("--") and len(word) > 1 and any(c in word[1:] for c in letters)


def _program(word) -> str:
    return posixpath.basename(str(word).lstrip("\\"))


def _skip_options(rest, valued, positional=0) -> list:
    """``rest`` after a wrapper's own options (``valued`` ones with their values, and ``NAME=value`` words for env) and
    its ``positional`` words: the command it runs, [] when there is none."""
    at = 0
    while at < len(rest):
        word = rest[at]
        if word == "--":
            at += 1
            break
        if word.startswith("-") and len(word) > 1:
            at += 2 if (word in valued and "=" not in word) else 1
            continue
        if ASSIGNMENT.fullmatch(word):
            at += 1
            continue
        break
    return rest[at + positional:]


def _split_script(script) -> list:
    """The simple commands of a shell script, each a list of words (quotes gone): split at ``;``, ``&&``, ``|``, newlines,
    ``$(…)``, backticks and the reserved words, so a command anywhere in it is seen; the word after a redirection is
    left out. A script shlex cannot read (a quote not closed) is split at blanks and those marks instead."""
    text = str(script).replace("\\\n", " ").replace("`", " ; ").replace("$(", " ; ( ").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = [word for word in re.split(r"[\s;&|()<>]+", text) if word]
    commands, current, redirected = [], [], False
    for word in words:
        if word in SHELL_JOINS or (word and set(word) <= set(";&|()")):
            if current:
                commands.append(current)
            current, redirected = [], False
            continue
        if word and set(word) <= set("<>&"):
            redirected = True  # The next word is where the output goes (or comes from), not a command's.
            continue
        if redirected:
            redirected = False
            continue
        current.append(word)
    if current:
        commands.append(current)
    return commands


def _shell_script(rest):
    """The script a shell runs with ``-c`` (``bash -lc '…'``), None when it runs a file or reads its input."""
    at, has_c = 0, False
    while at < len(rest):
        word = rest[at]
        if word == "--":
            at += 1
            break
        if word in ("--rcfile", "--init-file", "-o", "-O", "+O", "+o"):
            at += 2
            continue
        if word.startswith(("-", "+")) and len(word) > 1:
            if not word.startswith("--") and "c" in word[1:]:
                has_c = True
            at += 1
            continue
        break
    return rest[at] if has_c and at < len(rest) else None


def danger(argv, depth=0) -> str:
    """What makes ``argv`` (a command; ``sh -c`` and its script included) one to ask the owner about first (G4), '' when
    nothing does: deleting a tree (``rm -r``, ``find -delete``), killing processes, stopping, restarting or turning off a
    service, rebooting, formatting a disk, removing or stopping containers, forcing or deleting a push, ``git reset
    --hard``, ``git clean -f``, ``git branch -D``, uninstalling or reinstalling a package. Wrappers (sudo, env, timeout,
    xargs, nohup, …), shells (``sh -c``, ``bash -lc``), ``eval``, ``watch``, ``find -exec`` and ssh are looked through.
    Only the words are read, nothing runs; a command a script builds at run time (``$cmd``) is not seen."""
    words = [str(word) for word in argv or ()]
    while len(words) > 1 and ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]  # ``FOO=1 rm -rf x``
    if not words:
        return ""
    if depth > DEPTH_MAX:
        return "命令套了太多层，看不清要做什么"
    name, rest = _program(words[0]), words[1:]
    if name in WRAPPERS:
        if name == "command" and any(word in ("-v", "-V") for word in rest):
            return ""  # Only says where a command is.
        if name in ("ionice", "taskset") and any(word in ("-p", "--pid") for word in rest):
            return ""  # Changes a running process, runs nothing.
        if name == "env":
            split = next((at for at, word in enumerate(rest) if word in ("-S", "--split-string")
                          or word.startswith(("--split-string=", "-S"))), None)
            if split is not None:
                head = rest[split]
                script = [head.split("=", 1)[1] if head.startswith("--split-string=") else head[2:]] if head not in (
                    "-S", "--split-string") else []
                return _script_danger(" ".join([*script, *rest[split + 1:]]), depth + 1)
        valued, positional = WRAPPERS[name]
        return danger(_skip_options(rest, valued, positional), depth + 1)
    if name in SHELLS:
        script = _shell_script(rest)
        return _script_danger(script, depth + 1) if script is not None else ""
    if name == "eval":
        return _script_danger(" ".join(rest), depth + 1)
    if name == "watch":
        return _script_danger(" ".join(_skip_options(rest, frozenset({"-n", "--interval"}))), depth + 1)
    if name in ("ssh", "autossh"):
        found = _skip_options(rest, SSH_VALUED)
        return _script_danger(" ".join(found[1:]), depth + 1) if len(found) > 1 else ""
    if name.startswith("python") and rest[:2] == ["-m", "pip"]:
        name, rest = "pip", rest[2:]
    return _plain_danger(name, rest, depth)


def _script_danger(script, depth) -> str:
    for command in _split_script(script):
        found = danger(command, depth)
        if found:
            return found
    return ""


def _sub(rest, valued=frozenset()) -> tuple:
    """(the first word of ``rest`` that is not an option, the words after it), skipping ``valued`` options' values."""
    at = 0
    while at < len(rest):
        word = rest[at]
        if word.startswith("-") and len(word) > 1:
            at += 2 if (word in valued and "=" not in word) else 1
            continue
        return word, rest[at + 1:]
    return "", []


def _plain_danger(name, rest, depth) -> str:
    if name == "rm" and any(word in ("-r", "-R", "--recursive") or _short_has(word, "rR") for word in rest):
        return "rm -r（递归删除）"
    if name == "find":
        if "-delete" in rest:
            return "find -delete（删除）"
        for at, word in enumerate(rest):
            if word in ("-exec", "-execdir", "-ok", "-okdir"):
                end = next((i for i in range(at + 1, len(rest)) if rest[i] in (";", "+", "\\;")), len(rest))
                found = danger(rest[at + 1:end], depth + 1)
                if found:
                    return found
        return ""
    if name in DISKS or name.startswith("mkfs."):
        return f"{name}（格式化、擦除或改分区）"
    if name == "dd" and any(word.startswith("of=/dev/") for word in rest):
        return "dd 写磁盘设备"
    if name == "kill":
        if not rest or any(word in ("-l", "-L", "--list", "--table") or word.startswith("--list") for word in rest):
            return ""
        zero = any(word in ("-0", "-s0", "--signal=0") for word in rest) or any(
            rest[at] in ("-s", "--signal", "-n") and rest[at + 1:at + 2] == ["0"] for at in range(len(rest)))
        return "" if zero else "kill（杀进程）"
    if name in KILLERS:
        return "" if all(word in ("-l", "--list", "-V", "--version", "--help") for word in rest) else f"{name}（杀进程）"
    if name == "systemctl":
        sub, _ = _sub(rest, SYSTEMCTL_VALUED)
        return f"systemctl {sub}（停、重启或关掉服务）" if sub in SYSTEMCTL_STOPS else ""
    if name == "launchctl":
        sub, _ = _sub(rest)
        return f"launchctl {sub}（停、卸载或关掉服务）" if sub in LAUNCHCTL_STOPS else ""
    if name == "service":
        return f"service {rest[1]}（停或重启服务）" if len(rest) > 1 and rest[1] in SERVICE_STOPS else ""
    if name in POWER:
        return "" if any(word in ("--help", "-c") for word in rest) else f"{name}（关机或重启）"
    if name in ("init", "telinit") and rest[:1] and rest[0] in ("0", "1", "6", "s", "S"):
        return f"{name} {rest[0]}（关机、重启或进单用户）"
    if name == "crontab" and any(word == "-r" or _short_has(word, "r") for word in rest):
        return "crontab -r（删掉定时任务）"
    if name in CONTAINERS or name == "docker-compose":
        sub, after = _sub(rest, CONTAINER_VALUED)
        if sub in CONTAINER_STOPS:
            return f"{name} {sub}（删、停容器或镜像）"
        if sub in CONTAINER_GROUPS and name != "docker-compose":
            second, _ = _sub(after, CONTAINER_VALUED)
            return f"{name} {sub} {second}（删、停容器或镜像）" if second in CONTAINER_STOPS else ""
        return ""
    if name == "kubectl":
        sub, _ = _sub(rest, frozenset({"-n", "--namespace", "--context", "--kubeconfig", "-s", "--server"}))
        return f"kubectl {sub}（删资源或腾空节点）" if sub in ("delete", "drain") else ""
    if name == "git":
        return _git_danger(rest)
    if name == "pacman" and any(word.startswith("-R") or word == "--remove" for word in rest):
        return "pacman -R（卸载包）"
    if name == "dpkg" and any(word in ("-r", "-P", "--remove", "--purge") for word in rest):
        return "dpkg 卸载包"
    if name in UNINSTALLS:
        sub, after = _sub(rest)
        if name in ("conda", "mamba", "micromamba") and sub == "env":
            sub, _ = _sub(after)
        return f"{name} {sub}（卸载或重装包）" if sub in UNINSTALLS[name] else ""
    if name == "uv":
        sub, after = _sub(rest)
        return f"uv {sub} uninstall（卸载包）" if sub in ("pip", "tool") and _sub(after)[0] == "uninstall" else ""
    if name in NODE_UNINSTALLS:
        sub, after = _sub(rest)
        if name == "yarn" and sub == "global":
            return "yarn global remove（卸载全局包）" if _sub(after)[0] == "remove" else ""
        if sub in NODE_UNINSTALLS[name] and any(word in ("-g", "--global") for word in rest):
            return f"{name} {sub} -g（卸载全局包）"
    return ""


def _git_danger(rest) -> str:
    sub, args = _sub(rest, frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"}))
    if sub == "push":
        forced = any(word in ("-f", "--force", "--mirror", "--delete", "-d", "--prune")
                     or word.startswith(("--force-with-lease", "--force-if-includes")) or _short_has(word, "fd")
                     for word in args)
        refs = [word for word in args if not word.startswith("-")][1:]  # After the remote.
        return "git push 强推或删远端分支" if forced or any(ref.startswith(("+", ":")) for ref in refs) else ""
    if sub == "reset" and "--hard" in args:
        return "git reset --hard（丢掉改动）"
    if sub == "clean" and not any(word in ("-n", "--dry-run") or _short_has(word, "n") for word in args):
        if any(word in ("-f", "--force") or _short_has(word, "f") for word in args):
            return "git clean -f（删掉没提交的文件）"
    if sub == "branch" and any(word.startswith("-") and not word.startswith("--") and "D" in word for word in args):
        return "git branch -D（强删分支）"
    return ""


# The approval of a dangerous command (G4): an answer the owner himself gave to a question of this very topic.
OWNER_PARTY = "owner"  # core/turn_source.py PARTY_OWNER: a question record's ``answer_by`` party when he answered.
SKIPPED_START = "用户选择不回答此题"  # How app/answers.py SKIPPED begins: a question left unanswered.
QUESTION_TOKEN = re.compile(r"[0-9a-f]{16}\Z")
QUESTION_ENTRY = re.compile(r"([0-9a-f]{16})-([0-9]{1,3})\Z")
ASK_FIRST = ("这条命令要先问主人（{what}）：先用问题卡问主人，检查点写 requires_user 和 questions，这道题给一个 id，写清要在 "
             "{alias} 上做什么；主人同意后带 --approved <那道题的 id> 重跑：covey device {op} {alias} --approved <id> …")
APPROVAL_SAID = 120  # Characters of the owner's answer an audit line keeps.


def _answered(record, question) -> list:
    """The owner's choices and words for ``question`` of ``record``, [] when he left it unanswered."""
    found = ((record.get("answers") or {}).get(str(question.get("id") or "")) or {}).get("answers")
    words = [str(word) for word in found if str(word).strip()] if isinstance(found, list) else []
    return [] if all(word.startswith(SKIPPED_START) for word in words) else words


def _approval_records(turn: Turn, given, token) -> list:
    """The question records ``given`` may name: the one of ``token``, else this topic's with a question of that id, the
    latest answered first. ``Refused`` when there is none, or the token's is another topic's."""
    folder = Path(turn.state_dir) / "questions"
    if token:
        record = read_object(folder / f"{token}.json")
        if not record:
            raise Refused(f"没有答案号 {given} 这个问题")
        if record.get("topic_id") != turn.topic:
            raise Refused(f"答案号 {given} 不是本话题的问题，不能拿来放行")
        return [record]
    from covey.core.store import JsonDirectoryStore
    # A store of its own, not the process's shared one: an answer given since this process first looked is seen.
    records = [record for _, record in JsonDirectoryStore(folder).query(topic_id=turn.topic)
               if any(isinstance(q, dict) and str(q.get("id") or "") == given for q in record.get("questions") or ())]
    if not records:
        raise Refused(f"本话题里没有 id 是「{given}」的问题：--approved 写本话题问题卡里那道题的 id，等主人答了再用")
    return sorted(records, key=lambda item: float(item.get("answered_at") or item.get("created_at") or 0), reverse=True)


def approval(turn: Turn, given) -> dict:
    """What lets a dangerous command run (G4): ``given`` (``--approved``) names a question of this turn's topic the owner
    himself answered: the id the topic gave it in its checkpoint's ``questions`` (the latest such answer), its record's
    token, or ``<token>-<n>`` for its n-th question. Read from the Bridge's question records (``<state>/questions/``):
    the record's topic is this turn's, its status ``answered``, its ``answer_by`` the owner's (not the butler's, a
    lead's or a group member's), not answered with feedback alone or set aside, and the question itself answered (not
    skipped). Its basis, which the audit keeps: ``{answer, question, index, id, answered_at, channel, said}``.
    ``Refused`` saying why not. A question record the topic's own Harness forged under the state directory would pass:
    the check is of where the answer came from as the Bridge recorded it, not of a secret."""
    given = str(given or "").strip()
    if turn.task_dir is None or not turn.topic:
        raise Refused("不在话题的任务里，核对不了主人的回答；要在设备上做这件事，请主人自己 ssh 过去")
    entry = QUESTION_ENTRY.fullmatch(given)
    token = entry.group(1) if entry else given if QUESTION_TOKEN.fullmatch(given) else ""
    why = ""
    for record in _approval_records(turn, given, token):
        questions = [q for q in record.get("questions") or () if isinstance(q, dict)]
        if entry:
            index = int(entry.group(2))
            chosen = [(index, questions[index - 1])] if 0 < index <= len(questions) else []
        else:
            chosen = [(n, q) for n, q in enumerate(questions, 1) if token or str(q.get("id") or "") == given]
        by = record.get("answer_by") if isinstance(record.get("answer_by"), dict) else {}
        if record.get("status") != "answered":
            why = why or f"「{given}」主人还没答（问题现在是 {record.get('status') or '未知'}），不能放行"
        elif by.get("party") != OWNER_PARTY:
            who = {"butler": "管家", "lead": "负责人"}.get(str(by.get("party") or ""), "不是主人的人")
            why = why or f"「{given}」不是主人本人答的（{who}答的），不能放行；要主人自己答"
        elif record.get("response_kind") in ("feedback", "set_aside"):
            why = why or f"主人没有答「{given}」（只写了补充说明或点了不处理），不能放行"
        else:
            for index, question in chosen:
                words = _answered(record, question)
                if words:
                    return {"answer": given, "question": str(record.get("token") or token), "index": index,
                            "id": str(question.get("id") or ""), "answered_at": record.get("answered_at"),
                            "channel": str(by.get("channel") or ""),
                            "said": redact(" / ".join(words))[:APPROVAL_SAID]}
            why = why or f"主人没有答「{given}」这道题（选了不回答），不能放行"
    raise Refused(why or f"答案号 {given} 对不上本话题里主人答过的问题")


# ---------------------------------------------------------------- the audit (G6)


def audit_path(turn: Turn) -> Path:
    if turn.task_dir is not None:
        return turn.task_dir / DEVICES
    return devices.devices_dir(turn.state_dir) / TERMINAL_AUDIT


def shown(argv) -> str:
    """A command as an audit line and the task card keep it: shell-quoted, secrets hidden, at most COMMAND_SHOWN."""
    text = redact(shlex.join([str(word) for word in argv]) if isinstance(argv, (list, tuple)) else str(argv or ""))
    return text if len(text) <= COMMAND_SHOWN else text[:COMMAND_SHOWN - 1] + "…"


def audit(turn: Turn, row: dict) -> None:
    """Append ``row`` (with ``at``) to the turn's audit; a write that fails loses the line, never the call."""
    path = audit_path(turn)
    line = json.dumps({"at": round(time.time(), 3), **row}, ensure_ascii=False) + "\n"
    try:
        ensure_private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def read_audit(task_dir) -> list:
    """The audit lines of a task (``runs/<任务键>/devices.jsonl``), [] when it has none."""
    try:
        text = (Path(task_dir) / DEVICES).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("device"):
            rows.append(row)
    return rows


# ---------------------------------------------------------------- the channel


def _ssh_link(config, alias, state_dir):
    link = devices.open_transport(config, alias, state_dir, environ=ssh_env())
    devices.push_file(link, dt.shim_bytes())
    return link


class Channel:
    """One process's calls to the devices of ``config`` for ``turn``. ``connect(config, alias, state_dir)`` gives a
    Transport with the device program on it (default: ssh, the program pushed); tests give a LocalTransport."""

    def __init__(self, turn: Turn, *, config=None, connect=None, clock=time.monotonic):
        self.turn = turn
        self.config = config if config is not None else devices.current()
        self.connect = connect or _ssh_link
        self.clock = clock
        self.links = {}

    # -- G1, G2, G3
    def writable(self, device) -> bool:
        """Whether ``device`` may be written to in this turn: it is ``access: write`` and the turn is not held (G2, G3)."""
        return device.access == "write" and not self.turn.held

    def why_not(self, device) -> str:
        """Why ``device`` cannot be used from this turn at all (G1), '' when it can."""
        if not self.turn.known:
            return "这一轮没有设备通道的记录（任务进程比设备通道旧），设备通道用不了；下一轮再用"
        if not self.turn.space:
            return "这个聊天（代答群、没开启的群）不能用设备"
        if self.turn.space not in device.spaces:
            return f"设备 {device.alias} 不给这个聊天用（devices.json 的 spaces：{'、'.join(device.spaces)}）"
        return ""

    def device(self, alias, op, *, write=False):
        """The device ``alias`` for ``op`` after G1 and, for a ``write``, G2 and G3; ``Refused`` naming the reason."""
        alias = str(alias or "")
        if alias in self.config.watched:
            raise Refused(f"设备 {alias} 没有 ssh，只能在网页上看状况，不能执行")
        try:
            device = self.config.device(alias)
        except devices.UnknownDevice:
            known = "、".join(sorted(self.config.devices)) or "（没有）"
            raise Refused(f"devices.json 里没有设备 {alias}；能用的：{known}")
        reason = self.why_not(device)
        if reason:
            self.refused(alias, op, reason)
            raise Refused(reason)
        if write and not self.writable(device):
            reason = self.read_only_words(device, op)
            self.refused(alias, op, reason)
            raise Refused(reason)
        return device

    def read_only_words(self, device, op) -> str:
        if device.access != "write":
            return f"设备 {device.alias} 是只读的（devices.json 的 access 不是 write），不能 {op}；只能 ls、stat、get 和只读命令"
        return f"这一轮只读（{self.turn.limit or '只读'}），所有设备都只能看，不能 {op}"

    def refused(self, alias, op, reason, **more) -> None:
        audit(self.turn, {"device": alias, "op": op, "refused": reason, **more})

    # -- G4
    def approved(self, alias, op, argv, given) -> dict | None:
        """None for a command G4 lets run as it is; for a dangerous one (``danger``) the owner's answer ``given`` names
        (``approval``), the basis the audit keeps. ``Refused`` (and a refused line) without one that holds."""
        what = danger(argv)
        if not what:
            return None
        if not str(given or "").strip():
            reason = ASK_FIRST.format(what=what, alias=alias, op="run" if op == "run" else "job start")
        else:
            try:
                return {**approval(self.turn, given), "danger": what}
            except Refused as exc:
                reason = f"这条命令要先问主人（{what}），--approved {str(given)[:40]} 没通过：{exc}"
        self.refused(alias, op, reason, command=shown(argv), danger=what,
                     **({"approved": str(given)[:80]} if given else {}))
        raise Refused(reason)

    def link(self, alias):
        if alias not in self.links:
            self.links[alias] = self.connect(self.config, alias, self.turn.state_dir)
        return self.links[alias]

    def close(self) -> None:
        self.links.clear()  # The ControlMaster stays (ControlPersist): the next call and the Bridge use it.

    # -- list
    def list(self) -> list:
        """Every device of devices.json as ``{alias, title, note, access, runs, writable, usable, why}``, by alias."""
        rows = []
        everything = {**{a: (d, True) for a, d in self.config.devices.items()},
                      **{a: (d, False) for a, d in self.config.watched.items()}}
        for alias in sorted(everything):
            device, runs = everything[alias]
            why = "" if runs else "没有 ssh，只能看状况"
            why = why or self.why_not(device)
            rows.append({"alias": alias, "title": device.title, "note": device.note,
                         "access": "write" if device.access == "write" else "read", "runs": runs,
                         "usable": not why, "writable": runs and not why and self.writable(device), "why": why})
        audit(self.turn, {"device": "*", "op": "list", "count": len(rows)})
        return rows

    # -- run
    def run(self, alias, argv=None, *, sh=None, cwd=None, timeout=RUN_SECONDS, approved="") -> dict:
        """``argv`` (or ``sh``, a script for ``/bin/sh -c``) on ``alias`` in ``cwd`` (the device's home unless given),
        within ``timeout`` seconds: ``{code, out, err, timed_out, truncated, seconds}``. A read-only device or turn runs
        only what ``read_only_refusal`` lets through, never ``sh``."""
        device = self.device(alias, "run")
        if sh is not None:
            argv = ["/bin/sh", "-c", str(sh)]
        argv = [str(word) for word in (argv or [])]
        if not argv:
            raise Refused("没有要跑的命令：covey device run <设备> -- <命令…>")
        if not self.writable(device):
            reason = read_only_refusal(argv) if sh is None else "只读时不能跑 shell 脚本，只能跑一条只读命令"
            if reason:
                reason = f"{self.read_only_words(device, 'run 这条命令')}：{reason}"
                self.refused(alias, "run", reason)
                raise Refused(reason)
        basis = self.approved(alias, "run", argv, approved)  # G4
        timeout = max(1.0, min(RUN_MAX, float(timeout or RUN_SECONDS)))
        call = secrets.token_hex(4)
        mark = mark_of(self.turn)
        row = {"device": alias, "op": "run", "call": call, "command": shown(argv), "dir": str(cwd or "~"), "mark": mark,
               **({"approved": basis} if basis else {"approved": str(approved)} if approved else {})}
        audit(self.turn, {**row, "phase": "start"})
        started = self.clock()
        fields = {"argv": argv, "cwd": cwd or "~", "env": device_env(device, devices.read_profile(self.turn.state_dir,
                                                                                                 alias)),
                  "timeout": timeout, "max_out": OUT_MAX, **({"mark": mark} if mark else {})}
        try:
            answer = self.link(alias).call("exec", patience=timeout + 30.0, **fields)
        except dt.DeviceError as exc:
            audit(self.turn, {**row, "phase": "end", "error": type(exc).__name__, "seconds": round(self.clock() - started, 3)})
            raise
        out, err = _text(answer, "out"), _text(answer, "stderr")
        seconds = round(self.clock() - started, 3)
        audit(self.turn, {**row, "phase": "end", "code": answer.get("code"), "timed_out": bool(answer.get("timed_out")),
                          "bytes": len(out.encode("utf-8")) + len(err.encode("utf-8")), "seconds": seconds})
        return {"code": answer.get("code"), "out": out, "err": err, "timed_out": bool(answer.get("timed_out")),
                "truncated": bool(answer.get("truncated")), "seconds": seconds}

    # -- reading
    def ls(self, alias, path="~") -> dict:
        self.device(alias, "ls")
        answer = self.link(alias).call("ls", path=str(path or "~"), limit=2000, patience=30.0)
        audit(self.turn, {"device": alias, "op": "ls", "path": str(path or "~"), "count": answer.get("count")})
        return answer

    def stat(self, alias, paths) -> list:
        self.device(alias, "stat")
        answer = self.link(alias).call("stat", paths=[str(p) for p in paths], patience=30.0)
        audit(self.turn, {"device": alias, "op": "stat", "path": " ".join(map(str, paths))[:COMMAND_SHOWN]})
        return answer.get("results") or []

    def get(self, alias, remote, local=None) -> dict:
        """``remote`` from ``alias`` written to ``local`` (default: ``<outputs>/devices/<alias>/<its name>``; in a
        held turn only under the outputs directory): ``{path, bytes, sha256}``."""
        self.device(alias, "get")
        self.fetchable(alias, remote, lexical=True)
        target = self.local_target(alias, remote, local)
        started = self.clock()
        data = self.fetch(alias, remote, GET_MAX)
        digest = hashlib.sha256(data).hexdigest()
        ensure_private_dir(target.parent)
        atomic_write_bytes(target, data)
        audit(self.turn, {"device": alias, "op": "get", "path": str(remote), "local": str(target), "bytes": len(data),
                          "sha256": digest, "seconds": round(self.clock() - started, 3)})
        return {"path": str(target), "bytes": len(data), "sha256": digest}

    def read(self, alias, remote, max_bytes=GET_MAX) -> bytes:
        """``remote``'s bytes from ``alias``, at most ``max_bytes``, written nowhere (core/device_tools.py ``device_get``):
        the guards and the audit line of a ``get``, without ``local``."""
        self.device(alias, "get")
        self.fetchable(alias, remote, lexical=True)
        started = self.clock()
        data = self.fetch(alias, remote, max_bytes)
        audit(self.turn, {"device": alias, "op": "get", "path": str(remote), "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "seconds": round(self.clock() - started, 3)})
        return data

    def fetchable(self, alias, remote, *, lexical=False) -> None:
        """``Refused`` when ``remote`` names a secret (G5): as given with ``lexical``, else as the device resolves it."""
        if lexical:
            found = secret_path(remote)
        else:
            answer = self.link(alias).call("stat", paths=[str(remote)], patience=30.0)
            found = secret_path(((answer.get("results") or [{}])[0]).get("realpath") or "")
        if found:
            reason = f"`{found}` 里是钥匙或登录信息，不取"
            self.refused(alias, "get", reason)
            raise Refused(reason)

    def fetch(self, alias, remote, max_bytes) -> bytes:
        """``remote``'s bytes once the path it resolves to on the device names no secret either (a link to ``~/.ssh``)."""
        self.fetchable(alias, remote)
        try:
            return self.link(alias).get_file(str(remote), max_bytes=max_bytes)
        except dt.TooLarge:
            raise Refused(f"设备上的 {remote} 超过 {max_bytes} 字节，一次取不了") from None

    def local_target(self, alias, remote, local) -> Path:
        name = posixpath.basename(str(remote).rstrip("/")) or "file"
        outputs = Path(self.turn.outputs) if self.turn.outputs else None
        if local:
            target = Path(local).expanduser()
            target = (target / name) if target.is_dir() else target
            target = target if target.is_absolute() else Path.cwd() / target
        elif outputs is not None:
            target = outputs / "devices" / alias / name
        else:
            target = Path.cwd() / name
        if self.turn.held:
            if outputs is None or not _within(target, outputs):
                reason = f"这一轮只读，取回的文件只能放本话题产物目录（{self.turn.outputs or '没有'}）下"
                self.refused(alias, "get", reason)
                raise Refused(reason)
        return target

    # -- writing
    def remote_file(self, alias, device, remote) -> dict:
        """``{exists, sha256, mode}`` of ``remote`` on the device (one small Python of the device's own)."""
        script = ("import hashlib,json,os,stat,sys\np=os.path.expanduser(sys.argv[1])\n"
                  "try:\n st=os.stat(p)\nexcept OSError:\n print(json.dumps({'exists':False}));sys.exit(0)\n"
                  "if not stat.S_ISREG(st.st_mode):\n print(json.dumps({'exists':True,'file':False}));sys.exit(0)\n"
                  "h=hashlib.sha256()\nwith open(p,'rb') as f:\n for b in iter(lambda:f.read(1<<20),b''):\n  h.update(b)\n"
                  "print(json.dumps({'exists':True,'file':True,'sha256':h.hexdigest(),'mode':stat.S_IMODE(st.st_mode)}))")
        answer = self.link(alias).call("exec", argv=[device.python, "-c", script, str(remote)], cwd="~", timeout=SHA_SECONDS,
                                       max_out=4096, patience=SHA_SECONDS + 30.0)
        try:
            return json.loads(_text(answer, "out").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise dt.DeviceError("cannot read %s on %s: %s" % (remote, alias, _text(answer, "stderr")[-200:]))

    def put(self, alias, local, remote, *, if_sha=None, overwrite=False) -> dict:
        """``local`` to ``remote`` on ``alias`` (writable only, G2/G3). A file there is replaced only when ``if_sha`` is
        its sha256 (it did not change since it was fetched) or with ``overwrite``: ``{path, bytes, sha256, same}``."""
        device = self.device(alias, "put", write=True)
        found = secret_path(remote)
        if found:
            reason = f"`{found}` 里是钥匙或登录信息，不往里写"
            self.refused(alias, "put", reason)
            raise Refused(reason)
        source = Path(local).expanduser()
        data = source.read_bytes()
        if len(data) > PUT_MAX:
            raise Refused(f"{source} 有 {len(data)} 字节，一次 put 最多 {PUT_MAX} 字节")
        there = self.remote_file(alias, device, remote)
        if there.get("exists") and not there.get("file"):
            raise Refused(f"设备上的 {remote} 不是普通文件")
        if there.get("exists") and not overwrite:
            if not if_sha:
                reason = (f"设备上已经有 {remote}：先 covey device get 取回、改完再 put（会核对它没被别人改过），"
                          "确定要整个覆盖就加 --write")
                self.refused(alias, "put", reason)
                raise Refused(reason)
            if there.get("sha256") != if_sha:
                reason = f"设备上的 {remote} 在取回之后变了（sha256 不一致），先重新 get 再改"
                self.refused(alias, "put", reason)
                raise Refused(reason)
        mode = there.get("mode") if there.get("exists") else (source.stat().st_mode & 0o777)
        started = self.clock()
        answer = self.link(alias).put_file(str(remote), data, mode=int(mode or 0o644))
        digest = hashlib.sha256(data).hexdigest()
        audit(self.turn, {"device": alias, "op": "put", "path": str(remote), "local": str(source), "bytes": len(data),
                          "sha256": digest, "replaced": bool(there.get("exists")), "seconds": round(self.clock() - started, 3)})
        return {"path": str(remote), "bytes": len(data), "sha256": digest, "same": bool(answer.get("same"))}

    # -- jobs
    def job_start(self, alias, argv=None, *, sh=None, cwd=None, approved="") -> dict:
        """Start ``argv`` (or ``sh``) on ``alias`` as a job the device program supervises: ``{job, pgid, started}``. Its
        stdout and stderr go to the job's journal (``job_log``); stdin is /dev/null. A dangerous one needs the owner's
        answer (G4, ``approved``)."""
        device = self.device(alias, "job start", write=True)
        if sh is not None:
            wrapped = ["/bin/sh", "-c", "exec </dev/null 2>&1\n" + str(sh)]
            argv = ["/bin/sh", "-c", str(sh)]
        else:
            argv = [str(word) for word in (argv or [])]
            wrapped = ["/bin/sh", "-c", 'exec "$@" </dev/null 2>&1', "covey-job", *argv]
        if not argv:
            raise Refused("没有要跑的命令：covey device job start <设备> -- <命令…>")
        basis = self.approved(alias, "job start", argv, approved)  # G4
        job = "job-%s" % secrets.token_hex(5)
        env = device_env(device, devices.read_profile(self.turn.state_dir, alias))
        answer = self.link(alias).call("spawn", task=job, argv=wrapped, cwd=cwd or "~", env=env, patience=40.0)
        audit(self.turn, {"device": alias, "op": "job start", "job": job, "command": shown(argv), "dir": str(cwd or "~"),
                          "pgid": answer.get("pgid"),
                          **({"approved": basis} if basis else {"approved": str(approved)} if approved else {})})
        return {"job": job, "pgid": answer.get("pgid"), "dir": str(cwd or "~")}

    def job_status(self, alias, job) -> dict:
        self.device(alias, "job status", write=True)
        answer = self.link(alias).call("status", task=_job_name(job), patience=30.0)
        exit_info = answer.get("exit") or None
        audit(self.turn, {"device": alias, "op": "job status", "job": job, "state": answer.get("state")})
        return {"job": job, "state": answer.get("state"), "exit": exit_info, "journal": answer.get("journal"),
                "task_dir": answer.get("task_dir")}

    def job_log(self, alias, job, offset=0, limit=JOB_LOG_MAX) -> dict:
        """The job's output from byte ``offset`` of its journal (whole lines only): ``{text, next, ended, exit}``; ``next``
        is the offset to read from next time."""
        self.device(alias, "job log", write=True)
        status = self.link(alias).call("status", task=_job_name(job), patience=30.0)
        head, data = self.link(alias).get(status["task_dir"].rstrip("/") + "/journal", offset=int(offset or 0),
                                          limit=int(limit), patience=60.0)
        end = data.rfind(b"\n") + 1
        parts, ended = [], None
        for line in data[:end].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("k") == "out":
                text = record.get("d")
                if text is None and record.get("b64"):
                    import base64
                    text = base64.b64decode(record["b64"]).decode("utf-8", "replace")
                parts.append((text or "") + ("" if record.get("more") else "\n"))
            elif record.get("k") == "exit":
                ended = {key: record.get(key) for key in ("code", "sig", "reason")}
        audit(self.turn, {"device": alias, "op": "job log", "job": job, "from": int(offset or 0), "bytes": end})
        return {"job": job, "text": "".join(parts), "next": int(offset or 0) + end, "ended": ended is not None,
                "exit": ended, "state": status.get("state")}

    def job_stop(self, alias, job, grace=5.0) -> dict:
        self.device(alias, "job stop", write=True)
        answer = self.link(alias).call("stop", task=_job_name(job), grace=grace, patience=grace + 40.0)
        audit(self.turn, {"device": alias, "op": "job stop", "job": job, "exited": bool(answer.get("exited"))})
        return {"job": job, "exited": bool(answer.get("exited")), "exit": answer.get("exit")}


def _job_name(job) -> str:
    job = str(job or "")
    if not re.fullmatch(r"job-[0-9a-f]{10}", job):
        raise Refused(f"任务号 {job or '（空）'} 不对：是 covey device job start 打印的 job-…")
    return job


def _text(answer, key) -> str:
    if answer.get(key) is not None:
        return str(answer[key])
    raw = answer.get(key + "_b64")
    if raw:
        import base64
        return base64.b64decode(raw).decode("utf-8", "replace")
    return ""


def _within(path, root) -> bool:
    path, root = os.path.normpath(os.path.abspath(path)), os.path.normpath(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


# ---------------------------------------------------------------- the end of a turn


def unanswered(rows) -> dict:
    """{alias: mark} of the devices where a ``run`` started and never ended (``covey device`` was cut short)."""
    ended = {row.get("call") for row in rows if row.get("op") == "run" and row.get("phase") == "end"}
    return {row["device"]: row.get("mark") for row in rows
            if row.get("op") == "run" and row.get("phase") == "start" and row.get("mark") and row.get("call") not in ended}


def reap_turn(state_dir, task_dir, *, stop_jobs=False, config=None, connect=None, log=None) -> dict:
    """At a turn's end (G7): reap on each device the mark of the runs that never answered, and with ``stop_jobs`` (the
    owner stopped the turn) stop the jobs it started. {alias: "reaped" | "pending" | "stopped …"}; a device that cannot
    be reached gets a pending-reap.json entry (core/device_reap.py) for its mark. Nothing at all for a task without
    an audit (no device was used): no file is read but that one, no connection opened."""
    rows = read_audit(task_dir)
    if not rows:
        return {}
    marks = unanswered(rows)
    jobs = {}
    if stop_jobs:
        for row in rows:
            if row.get("op") == "job start" and row.get("job"):
                jobs.setdefault(row["device"], []).append(row["job"])
    if not marks and not jobs:
        return {}
    found = {}
    try:
        config = config if config is not None else devices.current()
    except devices.DeviceConfigError:
        config = None  # devices.json cannot be read now: every mark waits for the devices thread.
    connect = connect or _ssh_link
    for alias in sorted(set(marks) | set(jobs)):
        try:
            if config is None:
                raise devices.DeviceConfigError("devices.json cannot be read")
            link = connect(config, alias, Path(state_dir))
            if alias in marks:
                answer = link.call("reap", mark=marks[alias], grace=device_reap.GRACE, patience=device_reap.GRACE + 30.0)
                if answer.get("after"):
                    raise dt.DeviceError("still running: %s" % answer.get("after"))
                found[alias] = "reaped"
            for job in jobs.get(alias, ()):
                link.call("stop", task=job, grace=device_reap.GRACE, patience=device_reap.GRACE + 40.0)
                found[alias] = (found.get(alias, "") + " stopped " + job).strip()
        except (dt.DeviceError, OSError, devices.UnknownDevice, devices.DeviceConfigError) as exc:
            if alias in marks:
                device_reap.register(state_dir, alias, MARK_KEY + marks[alias], reason=device_reap.TURN_END,
                                     mark=marks[alias], run_dir=str(task_dir))
            found[alias] = "pending"
            if log:
                log(f"device {alias}: turn end reap not confirmed ({type(exc).__name__}), registered")
    return found


def fold(rows, before=None) -> list:
    """The topic's ``devices`` (topic.json) with a task's audit ``rows`` folded in: one entry a device, ``{alias,
    first_at, last_at, calls, ops, jobs, files}``; ``files`` the sha256 each path had when last fetched or put (what
    ``put`` checks: ``if_sha``), at most FILES_KEPT."""
    entries = {str(item.get("alias")): dict(item) for item in before or () if isinstance(item, dict) and item.get("alias")}
    for row in rows:
        alias = str(row.get("device") or "")
        if not alias or alias == "*" or row.get("phase") == "start":
            continue
        item = entries.setdefault(alias, {"alias": alias, "first_at": row.get("at"), "calls": 0, "ops": {}, "jobs": [],
                                          "files": {}})
        item["last_at"] = row.get("at")
        item["calls"] = int(item.get("calls") or 0) + 1
        ops = dict(item.get("ops") or {})
        op = str(row.get("op") or "")
        ops[op] = int(ops.get(op) or 0) + 1
        item["ops"] = ops
        if op == "job start" and row.get("job"):
            item["jobs"] = list(dict.fromkeys([*(item.get("jobs") or []), row["job"]]))[-JOBS_KEPT:]
        if op in ("get", "put") and row.get("sha256") and row.get("path"):
            files = dict(item.get("files") or {})
            files.pop(str(row["path"]), None)
            files[str(row["path"])] = row["sha256"]
            item["files"] = dict(list(files.items())[-FILES_KEPT:])
    return [entries[alias] for alias in sorted(entries)]


# A pending reap of a mark is keyed ``mark-<mark>``: a name the device program takes for a task's, so a release from before
# marks that replays it is told there is no such task and forgets it, instead of retrying a key it refuses forever.
MARK_KEY = "mark-"
FILES_KEPT = 50
JOBS_KEPT = 20


def fetched_sha(task_dir, topic_devices, alias, remote) -> str:
    """The sha256 ``remote`` had on ``alias`` when this topic last fetched (or put) it: this task's audit first, then the
    topic's ``devices``; '' when it never did."""
    for row in reversed(read_audit(task_dir) if task_dir else []):
        if row.get("device") == alias and row.get("op") in ("get", "put") and row.get("path") == str(remote):
            return str(row.get("sha256") or "")
    for item in topic_devices or ():
        if isinstance(item, dict) and item.get("alias") == alias:
            return str((item.get("files") or {}).get(str(remote)) or "")
    return ""


def trace_text(rows, limit=12) -> str:
    """The task card's 设备 lines of a task's audit: ``gpu1：run `uname -a`，退出 0（0.3 秒）`` a call, the last
    ``limit`` of them, '' with none."""
    lines = []
    for row in rows:
        if row.get("phase") == "start" or row.get("device") == "*":
            continue
        alias, op = row.get("device"), row.get("op")
        if row.get("refused"):
            reason = str(row["refused"])
            lines.append(f"{alias}：{op} 被拦下（{reason if len(reason) <= 60 else reason[:59] + '…'}）")
            continue
        what = row.get("command") or row.get("path") or row.get("job") or ""
        tail = ""
        if op == "run":
            tail = "，超时" if row.get("timed_out") else (f"，退出 {row.get('code')}" if row.get("error") is None
                                                          else f"，没有答复（{row['error']}）")
        elif op == "job start":
            tail = f"，任务号 {row.get('job')}"
        if isinstance(row.get("approved"), dict):
            tail += f"，主人已同意（{row['approved'].get('answer')}）"  # G4
        seconds = f"（{row['seconds']:g} 秒）" if isinstance(row.get("seconds"), (int, float)) else ""
        lines.append(f"{alias}：{op} `{what}`{tail}{seconds}" if what else f"{alias}：{op}{tail}{seconds}")
    more = len(lines) - limit
    lines = lines[-limit:]
    if more > 0:
        lines.insert(0, f"（前面还有 {more} 次）")
    return "\n".join(lines)

"""Codex 原生快路 (F2b): a Codex topic's commands, patches and images run on a writable device of devices.json by the
device's own ``codex exec-server``, the Harness, its session and its sign-in staying on the hub (docs/architecture.md
设备通道 · Codex 原生快路). The device needs the codex CLI of the hub's version and no sign-in: its exec-server runs with a
CODEX_HOME of its own that is never signed in (``EXEC_HOME``).

- ``handshake`` (the 体检's ``exec-server`` step, core/device_probe.py): one ``initialize`` to ``codex exec-server --listen
  stdio`` on the device (``PROBE``, run by the device's Python: the answer is waited for before stdin closes, since
  codex-cli 0.172.0 ends at its stdin's end before it answers), recorded in ``profile.json`` ``exec_server``. Only a device
  that is ``access: write`` is asked; any other is never run on for this (a colleague's machine stays read-only).
- ``ready``: whether a device may be one of a Codex turn's environments now: ``access: write`` and the codex that answered
  the handshake is the hub's own version (``hub_version``).
- ``due`` (a Codex turn's end): the devices the topic used for the first time in this turn (it called ``covey device`` on
  one, or declared a path of one) that are ready and that the turn could write to (core/device_access.py G1-G3, a topic
  that keeps to this machine's files): kept in ``topic.json`` ``device_envs`` (``{alias, cwd, at}``) from then on.
- ``environments`` (a Codex turn's start): the turn's ``environments`` for the app server: None for a topic that never got
  one (its turns as before); else ``local`` (the topic's working directory) first and each kept device that the guards
  let this turn write to, in the order it was kept; only ``local`` for a turn that may only read (G3), in a chat the
  device is not for (G1), in a topic that keeps off this machine's files, or for a device that stopped being ready.
- ``entries``: what the private CODEX_HOME's ``environments.toml`` says of each ready device (backends/codex/device_home.py):
  ``program = "ssh"`` with Covey's own ssh options (its ControlPath: core/devices.py ``ssh_argv``) and the remote command
  ``remote_command`` (the device's Codex on a PATH that has its directory first, the turn's environment G5 allows).
- ``rules_text`` (G4 on this path): Codex's own exec-policy rules, ``decision="forbidden"`` for the commands to ask the
  owner about first (``G4_RULES``: a recursive rm, a reboot, a service stopped, containers removed, a forced push, a
  package removed …), in the same home's ``rules/``. Codex checks them on the hub before a command goes to any
  environment, a device's too (tests/test_codex_native.py U7), so they hold there as on this machine, and for this
  machine's commands of such a topic as well (a rule names no environment). Only a command Codex reads as a simple one
  (``rm -rf x``, ``sudo reboot``); a script it cannot read (``rm -rf x; echo $?``) runs: words, not a wall.

Standard library plus core modules; nothing here starts a thread.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from covey.core import device_access, devices, repomap

KEY = "device_envs"  # topic.json: the devices kept as the topic's environments.
LOCAL = "local"  # The app server's own environment id: the hub, the topic's working directory.
EXEC_HOME = ".covey/codex-exec"  # Under the device's home: the exec-server's CODEX_HOME, never signed in.
HANDSHAKE_SECONDS = 15.0
VERSION = re.compile(r"(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)")
# Where the device's codex is looked for after its PATH: core/device_shim.py EXTRA_BIN.
EXTRA_BIN = (".local/bin", ".npm-global/bin", ".volta/bin", ".bun/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin")
INITIALIZE = {"jsonrpc": "2.0", "method": "initialize", "params": {"clientName": "covey-checkup"}, "id": 1}
# Run by the device's Python (3.9, standard library): finds codex, asks its version and one initialize, then closes stdin
# and sees it end. One JSON line out.
PROBE = r'''
import json, os, select, shutil, subprocess, sys, tempfile, time
line, seconds, home_dir, extra = sys.argv[1].encode() + b"\n", float(sys.argv[2]), sys.argv[3], sys.argv[4:]
home = os.path.expanduser("~")
out = {"path": shutil.which("codex") or ""}
for entry in ([] if out["path"] else extra):
    candidate = os.path.join(entry if os.path.isabs(entry) else os.path.join(home, entry), "codex")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        out["path"] = candidate
        break
if out["path"]:
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(out["path"]) + os.pathsep + env.get("PATH", "")
    env["CODEX_HOME"] = os.path.join(home, home_dir)
    os.makedirs(env["CODEX_HOME"], mode=0o700, exist_ok=True)
    try:
        done = subprocess.run([out["path"], "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=20, env=env, cwd=home)
        text = done.stdout.decode("utf-8", "replace").strip()
        out["version"], out["version_code"] = (text.splitlines() or [""])[0][:200], done.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        out["version_error"] = type(exc).__name__
    err = tempfile.TemporaryFile()
    started = time.time()
    try:
        child = subprocess.Popen([out["path"], "exec-server", "--listen", "stdio"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=err, env=env, cwd=home, start_new_session=True)
    except OSError as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        buf = b""
        try:
            child.stdin.write(line)
            child.stdin.flush()
        except OSError:
            pass
        while b"\n" not in buf:
            left = started + seconds - time.time()
            if left <= 0:
                out["timed_out"] = True
                break
            if select.select([child.stdout], [], [], min(left, 0.5))[0]:
                data = os.read(child.stdout.fileno(), 65536)
                if not data:
                    break
                buf += data
        out["answer_seconds"] = round(time.time() - started, 2)
        out["answer"] = buf.split(b"\n")[0].decode("utf-8", "replace")[:20000]
        try:
            child.stdin.close()
        except OSError:
            pass
        closed = time.time()
        try:
            out["exit"] = child.wait(5)
        except subprocess.TimeoutExpired:
            out["exit"] = None
            try:
                os.killpg(child.pid, 9)
            except OSError:
                pass
            child.wait(5)
        out["exit_seconds"] = round(time.time() - closed, 2)
        err.seek(0)
        out["stderr"] = err.read()[-600:].decode("utf-8", "replace")
print(json.dumps(out))
'''


def version_of(text) -> str:
    """The version number in what ``codex --version`` printed (``codex-cli 0.172.0`` -> ``0.172.0``), '' when none."""
    found = VERSION.search(str(text or ""))
    return found.group(1) if found else ""


_HUB = {}


def hub_version(cmd=None) -> str:
    """The version of the hub's codex (``cmd``, default ``codex`` on PATH), asked once per binary (its path and mtime);
    '' when it cannot be run."""
    words = list(cmd or ["codex"])
    found = shutil.which(words[0]) if words else None
    if not found:
        return ""
    try:
        real = os.path.realpath(found)
        key = (real, os.stat(real).st_mtime_ns, tuple(words[1:]))
    except OSError:
        return ""
    if key not in _HUB:
        try:
            done = subprocess.run([found, *words[1:], "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=20)
            _HUB[key] = version_of(done.stdout.decode("utf-8", "replace")) if done.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    return _HUB[key]


def _uri_path(value) -> str:
    text = str(value or "")
    return text[len("file://"):] if text.startswith("file://") else text


def read_probe(out, *, hub="", hub_capabilities=None, now=None) -> dict:
    """``profile.json`` ``exec_server`` from what ``PROBE`` printed: ``path``, ``version`` (its ``--version``),
    ``executor`` and ``os`` (the handshake's ``executorVersion``, ``platformOs``), ``home``, ``capabilities`` (the ones
    it has), ``exit`` (after stdin closed), ``hub`` and ``same`` (the executor is the hub's version), ``differs`` (the
    capabilities the hub's own exec-server answers otherwise, when given), ``ok`` (it answered and ended cleanly) and
    ``why`` (in words, '' when it is ready)."""
    out = out if isinstance(out, dict) else {}
    record = {"at": now if now is not None else time.time(), "path": str(out.get("path") or ""),
              "version": str(out.get("version") or ""), "hub": hub}
    info = {}
    try:
        info = ((json.loads(out.get("answer") or "{}").get("result") or {}).get("environmentInfo") or {})
    except (ValueError, AttributeError):
        info = {}
    capabilities = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
    record.update(executor=str(info.get("executorVersion") or ""), os=str(info.get("platformOs") or ""),
                  home=_uri_path(info.get("userHomeDir")), capabilities=sorted(k for k, v in capabilities.items() if v),
                  exit=out.get("exit"), seconds=out.get("answer_seconds"))
    if hub_capabilities is not None:
        theirs = set(hub_capabilities)
        record["differs"] = sorted(theirs.symmetric_difference(record["capabilities"]))
    record["ok"] = bool(record["executor"]) and out.get("exit") == 0
    record["same"] = bool(record["executor"]) and record["executor"] == hub
    if not record["path"]:
        why = "设备上没有 codex（PATH 和常见安装目录里都找不到）"
    elif not record["executor"]:
        detail = str(out.get("error") or out.get("stderr") or ("没有答复" if out.get("timed_out") else "")).strip()
        why = "codex exec-server 没有答复 initialize" + ("：" + detail.splitlines()[-1][:160] if detail else "")
    elif out.get("exit") != 0:
        why = "codex exec-server 答了，但关掉输入后没有干净退出（退出码 %s）" % out.get("exit")
    elif not hub:
        why = "Hub 上的 codex 版本没查到"
    elif not record["same"]:
        why = "设备上的 codex 是 %s，Hub 是 %s：版本一致才挂" % (record["executor"], hub)
    else:
        why = ""
    record["why"] = why
    return record


def run_probe(transport, device, profile, *, seconds=HANDSHAKE_SECONDS):
    """What ``PROBE`` printed on ``device`` (a dict; ``{"error": …}`` when it printed nothing readable)."""
    argv = [device.python or "python3", "-c", PROBE, json.dumps(INITIALIZE), str(seconds), EXEC_HOME, *EXTRA_BIN]
    answer = transport.call("exec", argv=argv, env=device_access.device_env(device, profile), cwd="~",
                            timeout=seconds + 40, max_out=1 << 17, patience=seconds + 70)
    for line in reversed((answer.get("out") or "").splitlines()):
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return {"error": ((answer.get("stderr") or "") or "exit %s" % answer.get("code")).strip()[-300:]}


_LOCAL = {}


def local_probe(cmd=None, *, seconds=HANDSHAKE_SECONDS) -> dict:
    """``PROBE`` run here, with the hub's codex (``cmd``) first on PATH: what the hub's own exec-server answers (its
    capabilities, to compare a device's with), once per binary as ``hub_version``. {} when it cannot run."""
    words = list(cmd or ["codex"])
    found = shutil.which(words[0]) if words else None
    if not found:
        return {}
    try:
        real = os.path.realpath(found)
        key = (real, os.stat(real).st_mtime_ns)
    except OSError:
        return {}
    if key in _LOCAL:
        return _LOCAL[key]
    env = {**os.environ, "PATH": os.path.dirname(found) + os.pathsep + os.environ.get("PATH", "")}
    try:
        done = subprocess.run([sys.executable, "-c", PROBE, json.dumps(INITIALIZE), str(seconds), EXEC_HOME],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=seconds + 40, env=env)
        found = json.loads(done.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}
    if isinstance(found, dict) and found.get("answer"):
        _LOCAL[key] = found
    return found if isinstance(found, dict) else {}


def handshake(transport, device, profile, *, hub=None, hub_probe=None, now=time.time) -> dict:
    """The 体检's ``exec-server`` record of ``device`` (``read_probe``). A device that is not ``access: write`` is not
    asked (``why`` says so). ``hub``: the hub's codex version (default ``hub_version()``); ``hub_probe``: the hub's own
    ``PROBE`` output, for its capabilities (default ``local_probe()``)."""
    if device.access != "write":
        return {"at": now(), "ok": False, "same": False, "why": "只读设备不挂 Codex 原生快路，不在上面跑 codex"}
    hub = hub_version() if hub is None else hub
    mine = local_probe() if hub_probe is None else hub_probe
    theirs = read_probe(mine, hub=hub) if mine else {}
    return read_probe(run_probe(transport, device, profile), hub=hub,
                      hub_capabilities=theirs.get("capabilities") if theirs.get("executor") else None, now=now())


def ready(device, profile, hub) -> str:
    """Why ``device`` cannot be one of a Codex turn's environments now, '' when it can: ``access: write``, and the
    exec-server that answered the 体检 is the hub's version ``hub``."""
    if device.access != "write":
        return "只读设备"
    record = (profile or {}).get("exec_server") if isinstance((profile or {}).get("exec_server"), dict) else {}
    if not record:
        return "还没做过 exec-server 体检"
    if not record.get("ok"):
        return record.get("why") or "exec-server 体检没通过"
    if not hub:
        return "Hub 上的 codex 版本没查到"
    if record.get("executor") != hub:
        return "设备上的 codex 是 %s，Hub 是 %s" % (record.get("executor") or "?", hub)
    return ""


def device_home(device, profile) -> str:
    """The directory a device's environment starts in: devices.json's ``home`` when it names an absolute one (a ``~``
    one under the device's own home), else the device's home as its exec-server or hello said it."""
    record = (profile or {}).get("exec_server") or {}
    own = str(record.get("home") or (profile or {}).get("home") or "")
    named = devices.home_of(device)
    if named.startswith("/"):
        return named.rstrip("/") or "/"
    if named.startswith("~") and own:
        return (own.rstrip("/") + named[1:]).rstrip("/") or "/"
    return own


# ---------------------------------------------------------------- the turn


def turn_of(state_dir, task_dir, topic=""):
    """The ``device_access.Turn`` of a task (its turn file: limit and space), None without a task directory."""
    if not task_dir:
        return None
    return device_access.turn_from_env({"COVEY_STATE": str(state_dir), "COVEY_TASK_DIR": str(task_dir),
                                        "COVEY_TOPIC": str(topic or "")})


def kept(data) -> list:
    """The topic's kept devices (``topic.json`` ``device_envs``): ``[{alias, cwd, at}]`` in the order they were kept."""
    found = []
    for item in (data or {}).get(KEY) or ():
        if isinstance(item, dict) and item.get("alias") and str(item.get("cwd") or "").startswith("/"):
            if all(entry["alias"] != item["alias"] for entry in found):
                found.append({"alias": str(item["alias"]), "cwd": str(item["cwd"]), "at": item.get("at")})
    return found


def _allowed(channel, config, alias, state_dir, hub):
    """(device, profile) of ``alias`` when this turn may write to it as an environment, else None."""
    if alias not in config.devices:
        return None
    device = config.devices[alias]
    if channel.why_not(device) or not channel.writable(device):
        return None
    profile = devices.read_profile(state_dir, alias)
    if ready(device, profile, hub):
        return None
    return device, profile


def environments(turn, config, data, *, workdir, local=True, hub="") -> list | None:
    """The ``environments`` of a Codex turn: None when the topic kept no device (its turns as before, no
    ``environments`` sent); else ``[{environmentId: "local", cwd: workdir}]`` and, for a ``local`` topic (one that keeps
    to this machine's files), each kept device this turn may write to (``turn``: G1, G2, G3) and that is ``ready``,
    ``{environmentId: alias, cwd}`` with the cwd it was kept with."""
    entries = kept(data)
    if not entries:
        return None
    found = [{"environmentId": LOCAL, "cwd": str(workdir)}]
    if turn is None or not local or turn.held:
        return found
    channel = device_access.Channel(turn, config=config, connect=lambda *a: None)
    for entry in entries:
        if _allowed(channel, config, entry["alias"], turn.state_dir, hub):
            found.append({"environmentId": entry["alias"], "cwd": entry["cwd"]})
    return found


def used(rows, data) -> list:
    """The devices a turn used: those its ``covey device`` calls reached (its audit ``rows``, a refused call not), then
    those the topic declared a path of (``topic.json`` ``repos``: ``<别名>:/abs``), each once, in that order."""
    found = []
    for row in rows or ():
        alias = str(row.get("device") or "")
        if alias and alias != "*" and not row.get("refused") and row.get("op") != "native" and alias not in found:
            found.append(alias)
    for item in (data or {}).get("repos") or ():
        alias = repomap.device_path(item.get("path"))[0] if isinstance(item, dict) else ""
        if alias and alias not in found:
            found.append(alias)
    return found


def due(turn, config, rows, data, *, local=True, hub="", now=None) -> list:
    """The ``device_envs`` entries a Codex turn's end adds to its topic (``used`` and not kept yet): each device the turn
    could write to (``turn``: G1, G2, G3; a ``local`` topic) that is ``ready``, with the cwd it is kept with
    (``device_home``). [] for a turn without a turn file, one that may only read, or a topic that keeps off this machine."""
    if turn is None or not turn.known or turn.held or not local:
        return []
    have = {entry["alias"] for entry in kept(data)}
    channel = device_access.Channel(turn, config=config, connect=lambda *a: None)
    added = []
    for alias in used(rows, data):
        if alias in have:
            continue
        found = _allowed(channel, config, alias, turn.state_dir, hub)
        cwd = device_home(*found) if found else ""
        if cwd.startswith("/"):
            added.append({"alias": alias, "cwd": cwd, "at": round(now if now is not None else time.time(), 3)})
    return added


def note_attached(turn, envs) -> None:
    """One audit line (G6, ``devices.jsonl``) per device a Codex turn has as an environment, at its start: ``op``
    ``native`` and the cwd. Its commands themselves come through the app server, whose command items name no
    environment (codex-cli 0.172.0)."""
    if turn is None or not turn.task_dir:
        return
    for item in envs or ():
        if item.get("environmentId") != LOCAL:
            device_access.audit(turn, {"device": item["environmentId"], "op": "native", "path": item.get("cwd") or ""})


# ---------------------------------------------------------------- the private home's environments.toml and rules

RULES_FILE = "covey-devices.rules"
RULE_WHY = ("Covey：在设备上删、停、重启、强推、卸载先用问题卡问主人；主人同意后用 covey device run --approved <那道题的 id> 跑"
            "（Codex 原生快路 G4）")
_RECURSIVE = ["-r", "-R", "-rf", "-fr", "-Rf", "-fR", "-rF", "-Fr", "-rfv", "-vrf", "--recursive"]
_SYSTEMCTL = ["stop", "restart", "try-restart", "disable", "mask", "kill", "isolate", "reboot", "poweroff", "halt",
              "suspend", "hibernate"]
_LAUNCHCTL = ["stop", "unload", "bootout", "remove", "kill", "disable", "reboot"]
_CONTAINER = ["rm", "rmi", "stop", "kill", "restart"]
# Each a prefix_rule pattern (a list item that is a list: any of its words).
G4_RULES = (
    ["rm", _RECURSIVE], ["rm", "-f", ["-r", "-R"]],
    [["reboot", "shutdown", "poweroff", "halt"]],
    ["systemctl", _SYSTEMCTL], ["launchctl", _LAUNCHCTL], ["service", ["stop", "restart"]],
    [["pkill", "killall"]], ["kill", ["-9", "-KILL", "-s"]],
    [["docker", "podman", "nerdctl"], _CONTAINER], [["docker", "podman"], "system", "prune"],
    [["docker", "podman"], "compose", "down"], ["kubectl", "delete"],
    ["git", "push", ["--force", "-f", "--force-with-lease", "--delete", "-d", "--mirror"]],
    ["git", "reset", "--hard"], ["git", "clean", ["-f", "-fd", "-df", "-fdx", "-xdf", "-fx", "-xf", "-ffdx"]],
    [["apt", "apt-get", "yum", "dnf"], ["remove", "purge", "autoremove", "erase"]],
    ["brew", ["uninstall", "remove", "rm"]], [["pip", "pip3", "uv"], "uninstall"],
    ["npm", ["uninstall", "remove", "rm", "un"], ["-g", "--global"]],
    [["mkfs", "mkfs.ext4", "mkfs.xfs", "fdisk", "parted", "wipefs"]], ["diskutil", ["eraseDisk", "eraseVolume", "partitionDisk"]],
)


def rules_text() -> str:
    """The rules file ``RULES_FILE`` of the device home's ``rules/``: each of ``G4_RULES`` forbidden, and with ``sudo``
    before it, with ``RULE_WHY``."""
    lines = ["# Written by Covey (core/device_native.py G4_RULES): commands a Codex turn with a device among its",
             "# environments may not run before the owner said yes (Codex checks them on the hub for every environment)."]
    for pattern in G4_RULES:
        for words in (pattern, ["sudo", *pattern]):
            lines.append("prefix_rule(pattern=%s, decision=\"forbidden\", justification=%s)" % (
                json.dumps(words, ensure_ascii=False), json.dumps(RULE_WHY, ensure_ascii=False)))
    return "\n".join(lines) + "\n"



def remote_command(device, profile) -> str:
    """The command the device's shell runs for the exec-server: its codex (the 体检's path) with that directory first on
    PATH (an npm install's ``codex`` is a node script beside its ``node``), CODEX_HOME ``EXEC_HOME`` (made, never signed
    in) and the variables G5 lets a command on the device have (core/device_access.py ``device_env``)."""
    record = (profile or {}).get("exec_server") or {}
    path = str(record.get("path") or "codex")
    given = device_access.device_env(device, profile)
    base = shlex.quote(given.pop("PATH")) if given.get("PATH") else '"$PATH"'
    words = ["PATH=%s:%s" % (shlex.quote(os.path.dirname(path) or "."), base), 'CODEX_HOME="$HOME"/%s' % EXEC_HOME]
    words += ["%s=%s" % (name, shlex.quote(value)) for name, value in sorted(given.items()) if name != "CODEX_HOME"]
    return 'mkdir -p "$HOME"/%s && exec env %s %s exec-server --listen stdio' % (
        EXEC_HOME, " ".join(words), shlex.quote(path))


def entries(state_dir, config, hub) -> list:
    """``[{id, program, args}]`` of every ready device of ``config`` (by alias): the private home's environments, one
    ``ssh`` each with Covey's own options (its ControlPath, BatchMode) and ``remote_command``."""
    found = []
    for alias in sorted(config.devices):
        device = config.devices[alias]
        profile = devices.read_profile(state_dir, alias)
        if ready(device, profile, hub):
            continue
        argv = devices.ssh_argv(device, config, remote=remote_command(device, profile))
        found.append({"id": alias, "program": argv[0], "args": argv[1:]})
    return found


def toml_text(found) -> str:
    """``environments.toml`` of ``found`` (``entries``): the local environment kept (``include_local``), no default."""
    lines = ["# Written by Covey (core/device_native.py): the devices a Codex turn may use as its environments.",
             "include_local = true"]
    for item in found:
        lines += ["", "[[environments]]", "id = %s" % json.dumps(item["id"]), "program = %s" % json.dumps(item["program"]),
                  "args = [%s]" % ", ".join(json.dumps(word) for word in item["args"])]
    return "\n".join(lines) + "\n"


def topic_local(data) -> bool:
    """Whether a topic may use this machine's files: false only for one opened with ``local`` false (``origin.local``,
    as app/home_turn.py ``local`` reads it)."""
    origin = data.get("origin") if isinstance(data, dict) else None
    return not (isinstance(origin, dict) and origin.get("local") is False)

"""Devices: other machines (Linux over ssh) a topic can work on instead of this one. Default off.

``COVEY_DEVICES=1`` (old name ``LARK_BRIDGE_DEVICES``) turns it on; the devices are listed in ``devices.json`` under
the data home (``COVEY_DEVICES_FILE`` names another file; docs/configuration.md "多设备"). Off, this module reads no
file, makes no ``<state>/devices/`` directory, starts no thread, and ``gpu1:/path`` is an ordinary (relative) path:
nothing changes. A switch that is on with no device listed behaves the same.

- **Config** (``load``, ``Config``, ``Device``): validated whole; a problem raises ``DeviceConfigError`` naming each
  one. Aliases are ``[a-z][a-z0-9-]{0,23}``; ``control_dir`` must be short enough for a Unix socket.
- **Workspace values** (``bind_workdir``): ``gpu1:/home/me/proj`` becomes a ``Binding``: the hub path
  ``<state>/devices/gpu1/standin/<sha12>`` (which never exists, so a place that was not taught about devices fails
  loudly instead of touching a hub directory) with ``device`` and ``remote_workdir``. Callers use it only when
  ``binding.device`` is set and keep their own handling otherwise.
- **ssh** (``ssh_argv``, ``open_transport``): one ControlMaster per device under ``control_dir``;
  ``core/device_transport.py`` runs the shim's commands over it. The shim file is pushed by content hash
  (``push_file``): a file that is there is not sent again.
- **Records** under ``<state>/devices/``: ``hub-id`` (one per hub, so two hubs on one device do not meet),
  ``<alias>/hello.json`` (the last ``hello``) and ``<alias>/profile.json`` (what ``self_check`` learned: tools,
  logins, the proxy environment that passed the whitelist).
- **self_check** connects, pushes the shim, says hello, imports the environment profile and (once the hub passes a
  ``probe``) runs one minimal real request per declared Harness.

Standard library plus core/env, core/paths, core/state and core/device_transport.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import re
import secrets
import threading
import time
from pathlib import Path

import covey.core.env as cenv
from covey.core import device_transport as transport_module
from covey.core.device_transport import LinkError, SshTransport, shim_bytes, shim_id
from covey.core.paths import data_home
from covey.core.state import ensure_private_dir, read_object, write_json

SWITCH = "COVEY_DEVICES"
FILE_ENV = "COVEY_DEVICES_FILE"
FILE_NAME = "devices.json"
SCHEMA = 1
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:%\[\]-]{0,252}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
HARNESSES = ("claude", "codex")
SPACES = ("owner_private", "personal_groups", "team")
DEFAULTS = {"patience_minutes": 15, "waiting_patience_hours": 6, "connect_seconds": 5}
LIMITS = {"patience_minutes": (1, 1440), "waiting_patience_hours": (1, 168), "connect_seconds": (1, 60)}
# What a device's environment profile may hold: proxies and certificate paths, the names the shim's hello reports.
PROFILE_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
PROXY_URLS = tuple(name for name in PROFILE_ENV if name.lower().endswith("_proxy") and not name.lower().startswith("no_"))
SOCKET_PATH_MAX = 104  # sun_path of macOS; Linux has 108. ssh adds a "." and 16 random characters while it makes the socket.
CONTROL_SUFFIX = 1 + 40 + len(".sock") + 17  # "/%C.sock" with %C expanded to 40 hex digits, plus ssh's temporary suffix.
CONTROL_DIR_MAX = SOCKET_PATH_MAX - CONTROL_SUFFIX
ENV_VALUE_MAX = 4096
CREDENTIALS = re.compile(r"://[^/?#\s]*@")
BARE_CREDENTIALS = re.compile(r"[^/@\s]+@")
HELLO_TTL = 600.0
PUSH_TTL = 60.0  # How long a push_file confirmation is trusted; the device's gc may delete a shim directory nobody runs.


class DeviceConfigError(ValueError):
    """devices.json is not valid; the message lists every problem found."""


class UnknownDevice(ValueError):
    """A workspace value names an alias devices.json does not list."""


def enabled(environ=None):
    """Whether ``COVEY_DEVICES=1`` (only ``1``; read on every call)."""
    return str(cenv.get(SWITCH, "", environ)).strip() == "1"


# ---------------------------------------------------------------- config

@dataclasses.dataclass(frozen=True, eq=False)
class Device:
    alias: str
    title: str
    host: str
    options: tuple
    python: str
    harnesses: tuple
    env: dict
    login_shell: bool
    spaces: tuple


@dataclasses.dataclass(frozen=True, eq=False)
class Config:
    devices: dict
    control_dir: Path
    patience_minutes: float = DEFAULTS["patience_minutes"]
    waiting_patience_hours: float = DEFAULTS["waiting_patience_hours"]
    connect_seconds: float = DEFAULTS["connect_seconds"]
    note: str = ""  # Why there is nothing (switch off, no file, no devices), for one log line.

    def __bool__(self):
        return bool(self.devices)

    def device(self, alias):
        try:
            return self.devices[alias]
        except KeyError:
            raise UnknownDevice("no device %r in devices.json" % (alias,))


def default_control_dir(environ=None):
    """``/tmp/covey-ssh-<uid>``: short (a Unix socket path is limited) and on local disk, never the NAS."""
    return Path("/tmp/covey-ssh-%d" % os.getuid())


def config_path(environ=None):
    given = str(cenv.get(FILE_ENV, "", environ)).strip()
    if given:
        return Path(given).expanduser()
    override = str(cenv.get("COVEY_DATA_HOME", "", environ)).strip()
    home = Path(override).expanduser().resolve() if override else data_home()
    return home / FILE_NAME


def has_credentials(name, value):
    """Whether a value carries a login: ``scheme://user:pass@host`` anywhere, or ``user:pass@host`` as a proxy."""
    if CREDENTIALS.search(value):
        return True
    return name in PROXY_URLS and bool(BARE_CREDENTIALS.match(value))


def _plain(value):
    return isinstance(value, str) and "\0" not in value and "\n" not in value and "\r" not in value


def _unique_pairs(pairs):
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise DeviceConfigError("devices.json names %s more than once" % ", ".join(map(repr, duplicates)))
    return dict(pairs)


def parse(text, *, environ=None):
    """A ``Config`` from devices.json's text; ``DeviceConfigError`` listing every problem."""
    try:
        raw = json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError as exc:
        if isinstance(exc, DeviceConfigError):
            raise
        raise DeviceConfigError("devices.json is not JSON: %s" % exc)
    problems = []

    def fail(where, text):
        problems.append("%s: %s" % (where, text))

    if not isinstance(raw, dict):
        raise DeviceConfigError("devices.json must be a JSON object")
    unknown = sorted(set(raw) - {"schema", "defaults", "control_dir", "devices"})
    if unknown:
        fail("devices.json", "unknown keys %s" % ", ".join(unknown))
    if raw.get("schema") != SCHEMA or isinstance(raw.get("schema"), bool):
        fail("schema", "must be %d" % SCHEMA)
    numbers = dict(DEFAULTS)
    given = raw.get("defaults", {})
    if not isinstance(given, dict):
        fail("defaults", "must be an object")
        given = {}
    for key, value in given.items():
        low, high = LIMITS.get(key, (None, None))
        if low is None:
            fail("defaults", "unknown key %r" % key)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            fail("defaults.%s" % key, "must be a number from %s to %s" % (low, high))
        else:
            numbers[key] = value
    control = default_control_dir(environ)
    if "control_dir" in raw:
        value = raw["control_dir"]
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            fail("control_dir", "must be a path")
        else:
            control = Path(value.strip()).expanduser()
            size = len(os.fsencode(str(control)))
            if not control.is_absolute():
                fail("control_dir", "must be an absolute path on local disk")
            elif size > CONTROL_DIR_MAX:
                fail("control_dir", "is %d bytes: with ssh's socket name (%s) it must stay under the %d-byte limit of a "
                     "Unix socket path, so the directory is at most %d bytes" % (size, "/%C.sock", SOCKET_PATH_MAX,
                                                                                CONTROL_DIR_MAX))
    entries = raw.get("devices", {})
    devices = {}
    if not isinstance(entries, dict):
        fail("devices", "must be an object of aliases")
        entries = {}
    for alias, entry in entries.items():
        where = "devices.%s" % alias
        if not isinstance(alias, str) or not ALIAS.match(alias):
            fail(where, "the alias must match [a-z][a-z0-9-]{0,23}")
            continue
        device = _device(alias, entry, fail)
        if device is not None:
            devices[alias] = device
    if problems:
        raise DeviceConfigError("devices.json has %d problem(s):\n  %s" % (len(problems), "\n  ".join(problems)))
    return Config(devices, control, numbers["patience_minutes"], numbers["waiting_patience_hours"],
                  numbers["connect_seconds"], "" if devices else "no devices listed")


def _device(alias, entry, fail):
    where = "devices.%s" % alias
    if not isinstance(entry, dict):
        fail(where, "must be an object")
        return None
    unknown = sorted(set(entry) - {"title", "ssh", "harnesses", "env", "login_shell", "spaces"})
    if unknown:
        fail(where, "unknown keys %s" % ", ".join(unknown))
    ok = True
    title = entry.get("title", alias)
    if not _plain(title) or not title.strip() or len(title) > 60:
        fail(where + ".title", "must be a short text")
        title = alias
    ssh = entry.get("ssh")
    host, options, python = "", (), "python3"
    if not isinstance(ssh, dict):
        fail(where + ".ssh", "is required: {\"host\": ...}")
        ok = False
    else:
        extra = sorted(set(ssh) - {"host", "options", "python"})
        if extra:
            fail(where + ".ssh", "unknown keys %s" % ", ".join(extra))
        host = ssh.get("host")
        if not isinstance(host, str) or not HOST.match(host):
            fail(where + ".ssh.host", "must be an ssh alias or user@host (no spaces, not starting with -)")
            ok, host = False, ""
        options = ssh.get("options", [])
        if not isinstance(options, list) or not all(_plain(item) and item for item in options):
            fail(where + ".ssh.options", "must be a list of ssh arguments (strings)")
            ok, options = False, []
        python = ssh.get("python", "python3")
        if not _plain(python) or not python.strip():
            fail(where + ".ssh.python", "must be a command name or path")
            ok, python = False, "python3"
    harnesses = entry.get("harnesses", list(HARNESSES))
    if (not isinstance(harnesses, list) or not harnesses or not all(item in HARNESSES for item in harnesses)
            or len(set(harnesses)) != len(harnesses)):
        fail(where + ".harnesses", "must be a non-empty list from %s" % ", ".join(HARNESSES))
        ok, harnesses = False, []
    env = entry.get("env", {})
    if not isinstance(env, dict):
        fail(where + ".env", "must be an object of variable names")
        env = {}
    for name, value in env.items():
        if not ENV_NAME.match(name) or not _plain(value) or len(value) > ENV_VALUE_MAX:
            fail(where + ".env", "%r: a variable name and a one-line text value are expected" % (name,))
            ok = False
        elif has_credentials(name, value):
            fail(where + ".env", "%s holds a login (user:password@): keep credentials out of devices.json" % name)
            ok = False
    login = entry.get("login_shell", False)
    if not isinstance(login, bool):
        fail(where + ".login_shell", "must be true or false")
        ok, login = False, False
    spaces = entry.get("spaces", ["owner_private"])
    if (not isinstance(spaces, list) or not spaces or not all(item in SPACES for item in spaces)
            or len(set(spaces)) != len(spaces)):
        fail(where + ".spaces", "must be a non-empty list from %s" % ", ".join(SPACES))
        ok, spaces = False, []
    if not ok:
        return None
    return Device(alias, title.strip(), host, tuple(options), python.strip(), tuple(harnesses), dict(env), login,
                  tuple(spaces))


def load(environ=None, *, path=None):
    """The configuration. Off: an empty ``Config`` without reading anything. On: the file (``path`` or the
    configured one); a file that is not there is an empty ``Config`` too (its ``note`` says so)."""
    if not enabled(environ):
        return Config({}, default_control_dir(environ), note="COVEY_DEVICES is not 1")
    path = Path(path) if path else config_path(environ)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Config({}, default_control_dir(environ), note="%s does not exist" % path)
    except OSError as exc:
        raise DeviceConfigError("cannot read %s: %s" % (path, exc))
    try:
        return parse(text, environ=environ)
    except DeviceConfigError as exc:
        raise DeviceConfigError("%s: %s" % (path, exc))


_CACHE = {}


def current(environ=None):
    """``load()`` again only when the file changed (path, mtime and size are the key); off, always the empty one."""
    if not enabled(environ):
        return load(environ)
    path = config_path(environ)
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(path), None, None)
    if _CACHE.get("key") != key:
        _CACHE["value"], _CACHE["key"] = load(environ, path=path), key
    return _CACHE["value"]


# ---------------------------------------------------------------- workspace values

DEVICE_VALUE = re.compile(r"([a-z][a-z0-9-]{0,23}):(|/[^\0\n]*|~(?:/[^\0\n]*)?)\Z")


@dataclasses.dataclass(frozen=True)
class Binding:
    """A workspace value decoded: ``workdir`` is a hub path (for a device, the stand-in that never exists),
    ``device`` the alias ('' for a local value), ``remote_workdir`` the path on the device (a string: never
    resolved here; ``~`` is the device's home) and ``value`` the canonical text to store (``gpu1:/home/me/proj``)."""
    workdir: Path
    device: str = ""
    remote_workdir: str = ""
    value: str = ""


def normalize_remote(path):
    """A device path made canonical without touching a file system: ``//`` and ``.`` and ``..`` folded, no trailing
    ``/``, ``~`` for the home (``..`` cannot climb above ``/`` or ``~``). ``''`` is the home."""
    if path in ("", "~"):
        return "~"
    if path.startswith("~/"):
        rest = posixpath.normpath("/" + path[2:])
        return "~" if rest == "/" else "~" + rest
    return posixpath.normpath(path)


def standin(state_dir, alias, remote_workdir):
    """The hub path standing for ``remote_workdir`` on ``alias``: ``<state>/devices/<alias>/standin/<sha12>``. Never
    created."""
    digest = hashlib.sha256(("%s:%s" % (alias, remote_workdir)).encode("utf-8")).hexdigest()[:12]
    return Path(state_dir) / "devices" / alias / "standin" / digest


def bind_workdir(ctx, value, *, config=None, environ=None, strict=True):
    """Decode a workspace value (``alias:path`` or a hub path). ``ctx`` is a ``BridgeContext`` (its ``state_dir``)
    or the state directory itself. The one decoder: every place a stored workspace value becomes a context calls it.

    Off, or a value that is not ``alias:/path`` (``alias:~/path``, ``alias:``): ``Binding(Path(value))``, the value
    untouched. On, a device value: the stand-in and the device's path; an alias devices.json does not list raises
    ``UnknownDevice``, or with ``strict=False`` (a stored topic whose device was taken out of the file) still gives the
    binding, so the turn is refused by name (app/runs.py) instead of failing in the decoder. Creates nothing and
    starts nothing."""
    text = str(value)
    if not enabled(environ):
        return Binding(Path(text), "", "", text)
    found = DEVICE_VALUE.match(text.strip())
    if not found:
        return Binding(Path(text), "", "", text)
    alias, path = found.group(1), normalize_remote(found.group(2))
    try:
        (config if config is not None else current(environ)).device(alias)
    except UnknownDevice:
        if strict:
            raise
    state_dir = getattr(ctx, "state_dir", ctx)
    return Binding(standin(state_dir, alias, path), alias, path, "%s:%s" % (alias, path))


def with_binding(ctx, binding, **changes):
    """``ctx`` (a context) with ``binding`` applied: ``workdir``, ``device`` and ``remote_workdir`` together (a local
    binding clears the two device fields), plus ``changes``."""
    if binding.device or getattr(ctx, "device", ""):
        changes.update(device=binding.device, remote_workdir=binding.remote_workdir)
    return dataclasses.replace(ctx, workdir=binding.workdir, **changes)


def stored_value(source):
    """The workspace value to store for a context, job or job record (a mapping): ``gpu1:/home/me/proj`` for a device,
    else the hub path as text (what was stored before devices existed)."""
    get = source.get if isinstance(source, dict) else lambda name, default="": getattr(source, name, default)
    device = str(get("device", "") or "")
    if device:
        return "%s:%s" % (device, get("remote_workdir", ""))
    return str(get("workdir", "") or "")


def is_device_value(value, environ=None):
    """Whether ``value`` (a stored workspace value, a path or a name) is ``alias:path`` with the switch on. Reads no
    file and needs no listed alias: for the places that must not treat such a value as a hub path (the directory
    check of ``core/workspaces.py``)."""
    return enabled(environ) and bool(DEVICE_VALUE.match(str(value).strip()))


CHECK_SECONDS = 5.0


def check_dir(state_dir, alias, remote_workdir, *, timeout=CHECK_SECONDS, config=None, environ=None):
    """Whether ``remote_workdir`` is a directory on ``alias``: ``(state, text)``, state ``ok``, ``missing`` (no such
    directory, or not one), ``offline`` (the device did not answer within ``timeout`` seconds) or ``unknown`` (the
    alias is not in devices.json). One ``stat`` through the shim (the answer is the device's: ``~`` is its home); the
    hub never touches the path."""
    config = config if config is not None else current(environ)
    try:
        config.device(alias)
    except UnknownDevice:
        return "unknown", "设备 %s 不在 devices.json 里" % alias
    found = {}

    def ask():
        try:
            link = open_transport(config, alias, state_dir)
            push_file(link, shim_bytes())
            answer = link.call("stat", paths=["."], base=remote_workdir, patience=timeout)
            item = (answer.get("results") or [{}])[0]
            if item.get("is_dir"):
                found["state"] = ("ok", "")
            elif item.get("exists"):
                found["state"] = ("missing", "%s 不是目录" % remote_workdir)
            else:
                found["state"] = ("missing", "%s 上没有目录 %s" % (alias, remote_workdir))
        except Exception as exc:  # noqa: BLE001 - any failure to ask is "the device did not answer"
            found["state"] = ("offline", "设备 %s 没有答复（%s）" % (alias, type(exc).__name__))

    worker = threading.Thread(target=ask, name="device-check", daemon=True)
    worker.start()
    worker.join(timeout + 1.0)
    return found.get("state") or ("offline", "设备 %s 在 %g 秒内没有答复" % (alias, timeout))


def stat_paths(state_dir, alias, remote_workdir, *, timeout=CHECK_SECONDS, config=None, environ=None):
    """A function that looks at paths on ``alias`` for ``checkpoint.audit_recovery``: ``paths`` (relative to
    ``remote_workdir``, ``~`` the device's home) in, one shim ``stat`` out: a list of ``{path, exists, size,
    escapes}``. The shim judges ``escapes`` (outside the directory, links followed) on the device. A failure to ask
    raises."""
    def look(paths):
        use = config if config is not None else current(environ)
        link = open_transport(use, alias, state_dir)
        push_file(link, shim_bytes())
        answer = link.call("stat", paths=list(paths), root=remote_workdir, base=remote_workdir, patience=timeout)
        return [{"path": item.get("path", ""), "exists": bool(item.get("exists")), "size": item.get("size", 0),
                 "escapes": bool(item.get("escapes"))} for item in answer.get("results") or []]

    return look


# ---------------------------------------------------------------- ssh

def ssh_argv(device, config, *, remote=None, op=None, ssh="ssh", persist=600, alive=15):
    """The ssh command line for ``device``.

    Ours come first (ssh keeps the first value of an option, so ``options`` of devices.json cannot undo them):
    no tty, ``BatchMode`` (never a password prompt: a device needs working keys and a known host key),
    ControlMaster auto with ``ControlPersist=<persist>`` and ``ControlPath=<control_dir>/%C.sock`` (one connection
    per device, reused), ``ServerAliveInterval``, ``ConnectTimeout`` and ``LogLevel=ERROR``. Then the device's
    ``options`` (``-J`` jump hosts, ``-p``, ``-i``, ...), then the host. ``remote`` (one command string, run by the
    device's shell) follows ``--``; ``op`` (``check`` or ``exit``) is a control-master request instead."""
    argv = [ssh, "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", "ControlPersist=%d" % persist,
            "-o", "ControlPath=%s/%%C.sock" % config.control_dir, "-o", "ServerAliveInterval=%d" % alive,
            "-o", "ConnectTimeout=%d" % config.connect_seconds, "-o", "LogLevel=ERROR", *device.options]
    if op:
        if op not in ("check", "exit", "stop"):
            raise ValueError("unknown control operation %r" % op)
        argv += ["-O", op]
    argv.append(device.host)
    if remote is not None:
        argv += ["--", remote]
    return argv


def ensure_control_dir(config):
    """Make ``control_dir`` (private, on first real use: never at load)."""
    ensure_private_dir(config.control_dir)
    return config.control_dir


def devices_dir(state_dir):
    return Path(state_dir) / "devices"


def hub_id(state_dir):
    """This hub's id, ``<state>/devices/hub-id`` (made on first use): the shim keeps each hub's tasks apart."""
    path = devices_dir(state_dir) / "hub-id"
    for _ in range(3):
        try:
            found = path.read_text().strip()
        except FileNotFoundError:
            found = ""
        if re.fullmatch(r"[a-z0-9]{12}", found):
            return found
        ensure_private_dir(path.parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            time.sleep(0.05)
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(6) + "\n")
    raise OSError("cannot make %s" % path)


def open_transport(config, alias, state_dir, *, ssh="ssh", environ=None):
    """The ``SshTransport`` to device ``alias`` (its control directory made, this hub's id read or made)."""
    device = config.device(alias)
    ensure_control_dir(config)

    def argv(remote=None, op=None):
        return ssh_argv(device, config, remote=remote, op=op, ssh=ssh)

    return SshTransport(alias, argv, hub=hub_id(state_dir), shim=shim_id(shim_bytes()), python=device.python,
                        environ=environ)


# ---------------------------------------------------------------- pushing files

def push_file(transport, data, name="shim.py", *, ttl=PUSH_TTL, clock=time.monotonic):
    """Put ``data`` on the device as ``~/.covey/shim/<sha16>/<name>`` unless that file (named by its content) is
    there: ``(state, path)`` with state ``cached`` (confirmed within ``ttl`` seconds), ``present`` or ``pushed``.
    The file is written under a temporary name and renamed, so a half-sent file never has the final name."""
    if not FILE_NAME_RE.match(name):
        raise ValueError("a pushed file's name is letters, digits, _, . and -: %r" % name)
    digest = shim_id(data)
    path = "%s/%s/%s" % (transport_module.SHIM_ROOT, digest, name)
    key = (digest, name)
    if clock() - transport.pushed.get(key, -1e9) < ttl:
        return "cached", path
    shell_path = '"$HOME/.covey/shim/%s/%s"' % (digest, name)
    check = '[ -f %s ] && [ "$(wc -c < %s | tr -d " ")" = "%d" ] && echo present || echo missing' % (
        shell_path, shell_path, len(data))
    state = _last_word(transport.shell(check)[1])
    if state != "present":
        code, out, err = transport.shell(push_script(digest, name, len(data)), data, patience=120.0)
        state = _last_word(out)
        if state != "pushed":
            raise LinkError("pushing %s to %s failed (exit %s): %s" % (name, transport.alias, code,
                                                                       (err or out).strip()[-300:]))
    transport.pushed[key] = clock()
    return state, path


def push_script(digest, name, size):
    """The ``sh`` script that receives ``size`` bytes on stdin as ``~/.covey/shim/<digest>/<name>``. It reads exactly
    ``size`` bytes (``head -c``) instead of waiting for end of file: over ssh the end of stdin reaches the device a
    second late (measured: 1.03 s against 0.03 s). A short read leaves no file; the rename comes last."""
    return ('set -e; umask 077; d="$HOME/.covey/shim/%(d)s"; mkdir -p "$d"; t="$d/.%(n)s.$$"; head -c %(s)d > "$t"; '
            'if [ "$(wc -c < "$t" | tr -d " ")" != "%(s)d" ]; then rm -f "$t"; echo short; exit 1; fi; '
            'mv -f "$t" "$d/%(n)s"; echo pushed' % {"d": digest, "n": name, "s": size})


def _last_word(text):
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


# ---------------------------------------------------------------- records, profile

def device_dir(state_dir, alias):
    return devices_dir(state_dir) / alias


def write_hello(state_dir, alias, hello, *, now=None):
    write_json(device_dir(state_dir, alias) / "hello.json", {"at": now if now is not None else time.time(),
                                                            "hello": hello}, ensure_ascii=False)


def read_hello(state_dir, alias, *, max_age=HELLO_TTL, now=None):
    """The last ``hello`` of ``alias`` when it is younger than ``max_age`` seconds (None: any age), else None."""
    record = read_object(device_dir(state_dir, alias) / "hello.json")
    at, hello = record.get("at"), record.get("hello")
    if not isinstance(hello, dict) or not isinstance(at, (int, float)):
        return None
    if max_age is not None and (now if now is not None else time.time()) - at > max_age:
        return None
    return hello


def read_profile(state_dir, alias):
    return read_object(device_dir(state_dir, alias) / "profile.json")


def accept_env(items):
    """``(kept, rejected)`` of a mapping of environment variables a device reported: only the PROFILE_ENV names, a
    one-line value of reasonable length, and no proxy URL that carries a login. ``rejected`` is
    ``[{"name", "reason"}]`` (never the value: it may be the password)."""
    kept, rejected = {}, []
    for name, value in (items or {}).items():
        if name not in PROFILE_ENV:
            rejected.append({"name": str(name)[:64], "reason": "name"})
        elif not _plain(value) or len(value) > ENV_VALUE_MAX:
            rejected.append({"name": name, "reason": "value"})
        elif has_credentials(name, value):
            rejected.append({"name": name, "reason": "credentials"})
        else:
            kept[name] = value
    return kept, rejected


def parse_env_lines(text):
    """``{name: value}`` of the PROFILE_ENV names in an ``env`` output (a login shell's variables)."""
    found = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in PROFILE_ENV:
            found[name] = value
    return found


def build_profile(device, hello, *, login_env=None, now=None, probes=None):
    """The environment profile of a device from its ``hello`` (and a login shell's variables): its tools and logins,
    and ``env``: the process environment's proxy variables, then Claude's ``settings.json`` env over them, then the
    login shell's under both; ``device.env`` of devices.json is applied over this at run time (``effective_env``)."""
    merged, rejected = {}, []
    for source, items in (("proxy", hello.get("proxy")), ("settings", hello.get("settings_env")),
                          ("login", login_env)):
        kept, dropped = accept_env(items)
        merged.update(kept)
        rejected += [dict(entry, source=source) for entry in dropped]
    tools = {name: {key: value for key, value in info.items() if key in ("found", "path", "version", "login")}
             for name, info in (hello.get("tools") or {}).items() if isinstance(info, dict)}
    return {"alias": device.alias, "at": now if now is not None else time.time(), "shim": hello.get("shim"),
            "os": hello.get("os"), "machine": hello.get("machine"), "python": hello.get("python"),
            "python_exe": hello.get("python_exe"), "home": hello.get("home"), "home_network": hello.get("home_network"),
            "tools": tools, "env": merged, "env_rejected": rejected,
            "settings_env_dropped": hello.get("settings_env_dropped", 0), "probe": probes or {}}


def effective_env(device, profile):
    """The environment variables to give a task on ``device``: the profile's, with devices.json's ``env`` over them."""
    merged = dict((profile or {}).get("env") or {})
    merged.update(device.env)
    return merged


# ---------------------------------------------------------------- self check

LOGIN_ENV_SCRIPT = ["sh", "-c", 'exec "${SHELL:-sh}" -lc env']


@dataclasses.dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False


@dataclasses.dataclass
class CheckResult:
    alias: str
    steps: list
    hello: dict | None = None
    profile: dict | None = None

    @property
    def ok(self):
        return all(step.ok or step.skipped for step in self.steps)

    @property
    def failed(self):
        return next((step for step in self.steps if not (step.ok or step.skipped)), None)

    def lines(self):
        """One line per step, for ``bin/covey_devices.py check`` and the workspace card."""
        return ["%s %-9s %5.1fs  %s" % ("ok " if step.ok else "-- " if step.skipped else "FAIL", step.name,
                                        step.seconds, step.detail) for step in self.steps]


def self_check(config, alias, *, state_dir=None, transport=None, probe=None, now=time.time, data=None):
    """Check a device end to end and record what was learned.

    Steps, stopping at the first that fails: ``connect`` (a trivial command over ssh, BatchMode), ``shim`` (push
    ``core/device_shim.py`` unless the device has it), ``hello`` (the device answers with the shim we pushed),
    ``profile`` (tools, logins and the environment that passed the whitelist; written with ``hello`` to
    ``<state>/devices/<alias>/`` when ``state_dir`` is given), ``tools`` (every Harness the device declares is
    installed) and ``probe:<harness>`` (``probe(transport, device, harness, profile)`` -> ``{"ok", "reason",
    "detail"}``, the one real minimal request A7 supplies; without a ``probe`` these are skipped, not failed).
    ``transport`` defaults to ``open_transport`` (needs ``state_dir``)."""
    device = config.device(alias)
    data = shim_bytes() if data is None else data
    result = CheckResult(alias, [])
    if transport is None:
        if state_dir is None:
            raise ValueError("self_check needs a state_dir or a transport")
        transport = open_transport(config, alias, state_dir)

    def step(name, action):
        started = time.monotonic()
        try:
            detail = action() or ""
            result.steps.append(Step(name, True, detail, time.monotonic() - started))
            return True
        except (LinkError, transport_module.ShimError, OSError) as exc:
            result.steps.append(Step(name, False, str(exc)[:300], time.monotonic() - started))
            return False

    def connect():
        code, out, err = transport.shell("echo covey-connected", patience=config.connect_seconds + 5)
        if "covey-connected" not in out:
            raise LinkError("the device did not answer (exit %s): %s" % (code, (err or out).strip()[-200:]))
        return "ssh BatchMode"

    def push():
        state, path = push_file(transport, data)
        return "%s %s" % (state, path)

    hello = {}

    def say_hello():
        hello.update(transport.hello(device.harnesses))
        if hello.get("shim") != shim_id(data):
            raise LinkError("the shim that answered (%s) is not the one pushed (%s)" % (hello.get("shim"), shim_id(data)))
        return "%s %s, python %s" % (hello.get("os"), hello.get("machine"), ".".join(map(str, hello.get("python") or [])))

    def make_profile():
        login = {}
        if device.login_shell:
            answer = transport.call("exec", argv=LOGIN_ENV_SCRIPT, timeout=20, max_out=1 << 18, patience=40.0)
            login = parse_env_lines(answer.get("out") or "")
        result.profile = build_profile(device, hello, login_env=login, now=now())
        if state_dir is not None:
            write_hello(state_dir, alias, hello, now=now())
            write_json(device_dir(state_dir, alias) / "profile.json", result.profile, ensure_ascii=False)
        rejected = result.profile["env_rejected"]
        return "%d variable(s)%s" % (len(result.profile["env"]), ", %d refused" % len(rejected) if rejected else "")

    def tools():
        missing = [name for name in device.harnesses if not (result.profile["tools"].get(name) or {}).get("found")]
        if missing:
            raise LinkError("not installed on the device: %s" % ", ".join(missing))
        return ", ".join("%s %s" % (name, result.profile["tools"][name].get("version", "")) for name in device.harnesses)

    if not step("connect", connect) or not step("shim", push) or not step("hello", say_hello):
        return result
    result.hello = dict(hello)
    if not step("profile", make_profile):
        return result
    step("tools", tools)
    for name in device.harnesses:
        if probe is None:
            result.steps.append(Step("probe:" + name, False, "no probe supplied", 0.0, skipped=True))
            continue

        def run_probe(name=name):
            answer = probe(transport, device, name, result.profile) or {}
            result.profile["probe"][name] = answer
            if state_dir is not None:
                write_json(device_dir(state_dir, alias) / "profile.json", result.profile, ensure_ascii=False)
            if not answer.get("ok"):
                raise LinkError("%s: %s" % (answer.get("reason", "other"), answer.get("detail", "")))
            return "ok"

        step("probe:" + name, run_probe)
    return result

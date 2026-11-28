"""Devices: other machines (Linux over ssh) a topic can work on instead of this one. Default off.

``COVEY_DEVICES=1`` (old name ``LARK_BRIDGE_DEVICES``) turns it on; the devices are listed in ``devices.json`` under
the data home (``COVEY_DEVICES_FILE`` names another file; docs/configuration.md "多设备"). Off, this module reads no
file, makes no ``<state>/devices/`` directory, starts no thread, and ``gpu1:/path`` is an ordinary (relative) path:
nothing changes. A switch that is on with no device listed behaves the same.

- **Config** (``load``, ``Config``, ``Device``): validated whole; a problem raises ``DeviceConfigError`` naming each
  one. Aliases are ``[a-z][a-z0-9-]{0,23}``; ``control_dir`` must be short enough for a Unix socket. A device declares
  Harnesses whose turns run on a device (``harness_names``: core/roster.py ``devices``), Claude Code and Codex unless
  it names them.
- **Device home** (``home_of``): an entry may name its ``home`` (``/abs/path`` or ``~/path``, as the device reads it);
  without one callers keep what they did. ``access`` (``write`` or ``read``; the web page adds a device ``read``) and
  ``note`` (its use, one line, shown in ``repos.md``'s device table) are written by the settings page (``update``); the
  channel that obeys ``access`` comes later (``ACCESS``).
- **Watched only**: an entry without ``ssh`` (a machine whose node reports its state but the hub cannot ssh to) is
  ``Config.watched``, never ``Config.devices``: nothing runs there, nothing opens a link to it; the settings page lists it.
- **The file as the page changes it** (``update``): ``change(raw) -> raw`` under ``devices.json.lock``, the whole file
  checked by ``parse`` and written back at once; keys and entries the change did not touch stay as they were.
- **Workspace values** (``bind_workdir``): ``gpu1:/home/me/proj`` becomes a ``Binding``: the hub path
  ``<state>/devices/gpu1/standin/<sha12>`` (which never exists, so a place that was not taught about devices fails
  loudly instead of touching a hub directory) with ``device`` and ``remote_workdir``. Callers use it only when
  ``binding.device`` is set and keep their own handling otherwise.
- **ssh** (``ssh_argv``, ``open_transport``): one ControlMaster per device under ``control_dir``;
  ``core/device_transport.py`` runs the shim's commands over it. The shim file is pushed by content hash
  (``push_file``): a file that is there is not sent again.
- **Records** under ``<state>/devices/``: ``hub-id`` (one per hub, so two hubs on one device do not meet),
  ``<alias>/hello.json`` (the last ``hello``) and ``<alias>/profile.json`` (what core/device_probe.py ``self_check`` learned: tools,
  logins, the proxy environment that passed the whitelist).
- **Self-check** (``self_check``, ``check_device``, ``status_text``): core/device_probe.py. It reads and writes the
  ``hello.json`` and ``profile.json`` records above through this module.
- **Browsing** (``list_dirs``, ``search_dirs``): a directory listing and a name search done on the device (the shim's
  ``ls``; one ``exec`` of a small script), for the directory browser. **Codex's model list** (``codex_root``): the
  device's ``models_cache.json`` fetched with the shim's ``get`` into ``<state>/devices/<alias>/codex-home/``.

Standard library plus core/env, core/paths, core/state, core/file_lock and core/device_transport (core/roster, read on
use).
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
import threading
import time
from pathlib import Path

import covey.core.env as cenv
from covey.core import device_transport as transport_module
from covey.core.device_transport import LinkError, SshTransport, shim_bytes, shim_id
from covey.core.file_lock import file_lock
from covey.core.paths import data_home
from covey.core.state import ensure_private_dir, read_object, write_json, write_private_text

SWITCH = "COVEY_DEVICES"
FILE_ENV = "COVEY_DEVICES_FILE"
FILE_NAME = "devices.json"
SCHEMA = 1
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
HOST = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:%\[\]-]{0,252}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
# What a device that names no ``harnesses`` declares: the two devices.json meant before any other could run there.
DEFAULT_HARNESSES = ("claude", "codex")
SPACES = ("owner_private", "personal_groups", "team")
# A new topic is never started on a device any more (主人 D2Y4T: the 执行设备 entries are gone, what runs a topic there stays
# for the topics already on one): the launch card, the page, the butler's and a lead's topic_start and the 换配置 of a topic
# here all say this instead. A topic uses a device from the home (core/device_access.py, ``covey device``).
START_RETIRED = ("执行设备入口已下线：新话题都从主目录启动，不能开到设备上；要用别的设备，话题开好后在话题里用 covey device"
                 "（list、run、get、put、job）")
DEFAULTS = {"patience_minutes": 15, "waiting_patience_hours": 6, "connect_seconds": 5}
# A device's ``access`` and ``note`` (its use, one line): the settings page writes them (``update``); a release before the one
# that wrote them only checks them, so a rollback still reads the file. Nothing obeys ``access`` yet.
ACCESS = ("write", "read")
NOTE_MAX = 60
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
    patience_minutes: float | None = None  # This device's own patience (None: devices.json's defaults).
    waiting_patience_hours: float | None = None
    home: str = ""  # The home on the device ("" when devices.json gives none); read by ``home_of``, never resolved here.
    access: str = ""  # ``write`` or ``read`` ("" when devices.json gives none): ACCESS.
    note: str = ""  # What the device is for, one line of at most NOTE_MAX characters ("" when none).


@dataclasses.dataclass(frozen=True, eq=False)
class Config:
    devices: dict
    control_dir: Path
    patience_minutes: float = DEFAULTS["patience_minutes"]
    waiting_patience_hours: float = DEFAULTS["waiting_patience_hours"]
    connect_seconds: float = DEFAULTS["connect_seconds"]
    note: str = ""  # Why there is nothing (switch off, no file, no devices), for one log line.
    # The entries without ``ssh``: watched (their node), never run on, never linked to; not in ``devices``.
    watched: dict = dataclasses.field(default_factory=dict)

    def __bool__(self):
        return bool(self.devices)

    def device(self, alias):
        try:
            return self.devices[alias]
        except KeyError:
            raise UnknownDevice("no device %r in devices.json" % (alias,))


def harness_names():
    """The Harnesses a device may declare: those whose turns run on a device (core/roster.py ``devices``: Claude Code,
    Codex and Antigravity in the core), in the roster's order. Read on use, so only a devices.json being parsed asks."""
    from covey.core.roster import DEVICE_HARNESSES
    return DEVICE_HARNESSES


def _not_on_devices(items):
    """The Harnesses of ``items`` the roster has but that cannot run on a device (dsh in the core)."""
    from covey.core.roster import NAMES
    return [item for item in items if isinstance(item, str) and item in NAMES and item not in harness_names()]


def undeclared_refusal(alias, harness, title):
    """Why a task of the Harness ``harness`` (shown as ``title``) does not start on device ``alias``: devices.json does
    not declare it for that device ('' when it does, or when the device is not known here: the turn's own check says so)."""
    try:
        declared = current().device(alias).harnesses
    except (UnknownDevice, DeviceConfigError):
        return ""
    if harness in declared:
        return ""
    return ("设备 %s 只声明了 %s（devices.json 的 harnesses），没有 %s；请换一个 Harness 或工作区，任务尚未启动。"
            % (alias, " / ".join(declared), title))


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
    devices, watched = {}, {}
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
            (devices if device.host else watched)[alias] = device
    if problems:
        raise DeviceConfigError("devices.json has %d problem(s):\n  %s" % (len(problems), "\n  ".join(problems)))
    return Config(devices, control, numbers["patience_minutes"], numbers["waiting_patience_hours"],
                  numbers["connect_seconds"], "" if devices else "no devices listed", watched=watched)


def _device(alias, entry, fail):
    where = "devices.%s" % alias
    if not isinstance(entry, dict):
        fail(where, "must be an object")
        return None
    unknown = sorted(set(entry) - {"title", "ssh", "harnesses", "env", "login_shell", "spaces", "patience_minutes",
                                   "waiting_patience_hours", "home", "access", "note"})
    if unknown:
        fail(where, "unknown keys %s" % ", ".join(unknown))
    ok = True
    title = entry.get("title", alias)
    if not _plain(title) or not title.strip() or len(title) > 60:
        fail(where + ".title", "must be a short text")
        title = alias
    ssh = entry.get("ssh")
    host, options, python = "", (), "python3"
    if ssh is None:
        pass  # Watched only (Config.watched): its node reports, nothing runs there.
    elif not isinstance(ssh, dict):
        fail(where + ".ssh", "must be {\"host\": ...} (or left out for a machine that is only watched)")
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
    harnesses, names = entry.get("harnesses", list(DEFAULT_HARNESSES)), harness_names()
    if (not isinstance(harnesses, list) or not harnesses or not all(item in names for item in harnesses)
            or len(set(harnesses)) != len(harnesses)):
        cannot = _not_on_devices(harnesses) if isinstance(harnesses, list) else []
        fail(where + ".harnesses", "must be a non-empty list from %s%s" % (
            ", ".join(names), " (%s cannot run on a device)" % ", ".join(cannot) if cannot else ""))
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
    patience = {}
    home = entry.get("home", "")
    if home != "" and (not _plain(home) or not home.strip() or len(home) > 1024
                       or not (home.strip().startswith("/") or home.strip() == "~" or home.strip().startswith("~/"))):
        fail(where + ".home", "must be an absolute path on the device, or one starting with ~/ (the device's own home)")
        ok, home = False, ""
    access = entry.get("access", "")
    if access != "" and access not in ACCESS:
        fail(where + ".access", "must be %s" % " or ".join(ACCESS))
        ok, access = False, ""
    note = entry.get("note", "")
    if not _plain(note) or len(note.strip()) > NOTE_MAX:
        fail(where + ".note", "must be one line of at most %d characters" % NOTE_MAX)
        ok, note = False, ""
    for key in ("patience_minutes", "waiting_patience_hours"):
        if key in entry:
            value, (low, high) = entry[key], LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                fail("%s.%s" % (where, key), "must be a number from %s to %s" % (low, high))
                ok = False
            else:
                patience[key] = value
    if not ok:
        return None
    return Device(alias, title.strip(), host, tuple(options), python.strip(), tuple(harnesses), dict(env), login,
                  tuple(spaces), home=home.strip(), access=access, note=note.strip(), **patience)


def home_of(device):
    """The home the device's entry names in devices.json (``home``), as written: a ``~`` path is the device's own home and
    is never resolved on the hub. '' when it names none: callers then keep what they did before (the first workspace)."""
    return str(getattr(device, "home", "") or "")


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


class Unwritable(ValueError):
    """devices.json cannot be changed now: it cannot be read, or what the change makes is not valid (the message says)."""


def _lock_path(path):
    return Path(path).with_name(Path(path).name + ".lock")


def raw(path=None, environ=None):
    """devices.json as it is written ({} when there is none); Unwritable when it is there and is no JSON object."""
    path = Path(path) if path else config_path(environ)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Unwritable("devices.json 读不出来（%s）" % type(exc).__name__)
    try:
        found = json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError:
        raise Unwritable("devices.json 不是正确的 JSON，请先手工改好")
    if not isinstance(found, dict):
        raise Unwritable("devices.json 不是 JSON 对象，请先手工改好")
    return found


def update(change, *, path=None, environ=None):
    """``change(raw) -> raw`` applied to devices.json under its lock (``devices.json.lock`` beside it), the whole file then
    checked by ``parse`` and written back at once: every key and entry the change does not touch stays as it was (a newer
    release's included, as far as this one can check the file). A missing file starts as ``{"schema": 1, "devices": {}}``.
    The ``Config`` written; Unwritable when the file cannot be read or the result is not valid (nothing written then)."""
    path = Path(path) if path else config_path(environ)
    ensure_private_dir(path.parent)
    with file_lock(_lock_path(path)):
        before = raw(path)
        start = json.loads(json.dumps(before)) if before else {"schema": SCHEMA, "devices": {}}
        after = change(start)
        text = json.dumps(after, ensure_ascii=False, indent=2) + "\n"
        try:
            config = parse(text, environ=environ)
        except DeviceConfigError as exc:
            raise Unwritable(str(exc))
        if after != before:
            write_private_text(path, text)
    return config


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
    return enabled(environ) and device_shaped(value)


def device_shaped(value):
    """Whether ``value`` reads ``alias:path``, the switch on or off (reads nothing): only a device workspace is written so
    (a workspace of this machine is an absolute path), for the place that refuses one while the switch is off
    (app/butler_topics.py plan_start) instead of taking it for a path here."""
    return bool(DEVICE_VALUE.match(str(value).strip()))


# ---------------------------------------------------------------- the checkpoint and the seed files of a task

CHECKPOINT_POLL = 1.0


def checkpoint_mirror(state_dir, alias, hub_path):
    """Where the job's checkpoint (``hub_path``, the file the hub keeps and reads) lives on ``alias``: ``{"path", "hub_path",
    "poll"}``. The path is the device's, an absolute one under the user's home when the profile knows it (an Agent's file
    tools do not expand ``~``), and the same for every process of the job: it does not depend on the task key the shim
    gets. The device program mirrors changes to the hub (core/device_link.py writes them to ``hub_path``)."""
    home = str(read_profile(state_dir, alias).get("home") or "")
    root = (home.rstrip("/") if home.startswith("/") else "~") + "/.covey/checkpoints/" + hub_id(state_dir)
    return {"path": "%s/%s" % (root, Path(str(hub_path)).name), "hub_path": str(hub_path), "poll": CHECKPOINT_POLL}


def spawn_extras(ctx, hub_checkpoint):
    """``ctx`` for the run of a device turn: ``options["device_spawn"]`` carries what ``spawn_process`` puts in its request
    (core/device_link.py): the checkpoint mirror and the seed file, the hub's checkpoint as it is now (a Harness that does
    not write one still has the file the prompt names). A context of this machine, or a turn without a checkpoint, is
    returned as it is."""
    if not getattr(ctx, "device", "") or not hub_checkpoint:
        return ctx
    mirror = checkpoint_mirror(ctx.state_dir, ctx.device, hub_checkpoint)
    files = []
    try:
        files.append({"path": mirror["path"], "text": Path(hub_checkpoint).read_text(encoding="utf-8"), "mode": 0o600})
    except (OSError, UnicodeDecodeError):
        pass  # No seed: the Agent makes the file, as on this machine.
    return dataclasses.replace(ctx, options={**ctx.options, "device_spawn": {"checkpoint": mirror, "files": files}})


def device_checkpoint_path(ctx, hub_checkpoint):
    """The path the prompt names for the job's checkpoint on a device (``checkpoint_mirror``)."""
    return checkpoint_mirror(ctx.state_dir, ctx.device, hub_checkpoint)["path"]


CHECK_SECONDS = 5.0
# How check_dir words why a device did not answer (the exception's name otherwise).
OFFLINE_REASONS = {"Unreachable": "ssh 连不上", "LinkTimeout": "连接超时", "NoHi": "设备上的程序没有应答", "Closed": "连接被关闭",
                   "BadFrame": "设备返回了无法读取的数据"}


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
            found["state"] = ("offline", "设备 %s 没有答复（%s）" % (alias, OFFLINE_REASONS.get(type(exc).__name__, type(exc).__name__)))

    worker = threading.Thread(target=ask, name="device-check", daemon=True)
    worker.start()
    worker.join(timeout + 1.0)
    return found.get("state") or ("offline", "设备 %s 在 %g 秒内没有答复" % (alias, timeout))


def _asked(work, timeout, name):
    """``work()`` on a daemon thread, at most ``timeout`` seconds (plus one): its result, or ``("offline", text)`` when it
    did not end by then. ``work`` turns its own failures into results."""
    found = {}

    def run():
        found["result"] = work()

    worker = threading.Thread(target=run, name=name, daemon=True)
    worker.start()
    worker.join(timeout + 1.0)
    return found.get("result")


def inspect_dir(state_dir, alias, remote_dir, *, timeout=CHECK_SECONDS, config=None, environ=None):
    """``(state, kind, text)`` of ``remote_dir`` on ``alias`` for the 常用路径 page: state as ``check_dir`` says it (``ok``,
    ``missing``, ``offline``, ``unknown``), kind ``git`` when it holds ``.git``, else ``dir`` ('' unless ok). One shim
    ``stat`` of ``.`` and ``.git``; the hub never touches the path."""
    config = config if config is not None else current(environ)
    try:
        config.device(alias)
    except UnknownDevice:
        return "unknown", "", "设备 %s 不在 devices.json 里（或没有 ssh）" % alias

    def ask():
        try:
            link = open_transport(config, alias, state_dir)
            push_file(link, shim_bytes())
            answer = link.call("stat", paths=[".", ".git"], base=remote_dir, patience=timeout)
            here, git = ((answer.get("results") or []) + [{}, {}])[:2]
            if here.get("is_dir"):
                return "ok", "git" if git.get("exists") else "dir", ""
            if here.get("exists"):
                return "missing", "", "%s 不是目录" % remote_dir
            return "missing", "", "%s 上没有目录 %s" % (alias, remote_dir)
        except Exception as exc:  # noqa: BLE001 - any failure to ask is "the device did not answer"
            return "offline", "", "设备 %s 没有答复（%s）" % (alias, OFFLINE_REASONS.get(type(exc).__name__, type(exc).__name__))

    return _asked(ask, timeout, "device-inspect") or ("offline", "", "设备 %s 在 %g 秒内没有答复" % (alias, timeout))


def read_head(state_dir, alias, remote_dir, names, *, limit=4096, timeout=CHECK_SECONDS, config=None, environ=None):
    """``(file name, its first ``limit`` bytes cut at a line, cut short, why not)`` of the first of ``names`` that is a file
    in ``remote_dir`` on ``alias``, read with the shim's ``get`` (``limit`` + 1 bytes at most); ``("", "", False, why)``
    when none is there or the device did not answer within ``timeout`` seconds (``why`` '' for none there)."""
    config = config if config is not None else current(environ)
    try:
        config.device(alias)
    except UnknownDevice:
        return "", "", False, "设备 %s 不在 devices.json 里（或没有 ssh）" % alias

    def ask():
        try:
            link = open_transport(config, alias, state_dir)
            push_file(link, shim_bytes())
            for name in names:
                try:
                    _, data = link.get(posixpath.join(remote_dir, name), limit=limit + 1, patience=timeout)
                except transport_module.ShimError:
                    continue  # Not there, not a file, not readable: the next name.
                cut = len(data) > limit
                text = data[:limit].decode("utf-8", errors="ignore")
                if cut and "\n" in text:
                    text = text[:text.rindex("\n") + 1]
                return name, text, cut, ""
            return "", "", False, ""
        except Exception as exc:  # noqa: BLE001 - said as the device not answering
            return "", "", False, "设备 %s 没有答复（%s）" % (alias, OFFLINE_REASONS.get(type(exc).__name__, type(exc).__name__))

    return _asked(ask, timeout, "device-head") or ("", "", False, "设备 %s 在 %g 秒内没有答复" % (alias, timeout))


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

SSH_WITH_VALUE = set("bcDEeFIiJLlmOoPpQRSWw")  # ssh options that take the next word.
# Options that make ssh run something on this machine (or forward its sockets): never taken from what the owner typed.
SSH_UNSAFE = re.compile(r"(?i)(proxycommand|localcommand|knownhostscommand|^-[^-]*[MSWw])")


def ssh_words(text):
    """The ssh arguments of an address typed as a shell would split it (``user@host -p 22 -J jump``): ValueError when it
    cannot be one, or when it has an option that runs something here (``SSH_UNSAFE``). app/node_panel.py's add uses it."""
    try:
        args = shlex.split(text or "")
    except ValueError:
        raise ValueError("ssh 地址的引号没有配对")
    if not args:
        raise ValueError("ssh 地址是空的")
    if any(SSH_UNSAFE.search(arg) for arg in args):
        raise ValueError("ssh 地址里有会在本机执行命令的选项，不支持")
    hosts, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-"):
            skip = len(arg) == 2 and arg[1] in SSH_WITH_VALUE  # ``-p 22``: the next word is the option's.
        else:
            hosts.append(arg)
    if len(hosts) != 1:
        raise ValueError("ssh 地址里要有且只有一个 user@host")
    return args


def split_ssh(text):
    """``(host, options)`` of a typed address (``ssh_words``) as devices.json keeps them: ``{"host", "options"}``. ValueError
    says why not (a host devices.json would refuse too: spaces, a leading -)."""
    args, host, options, skip = ssh_words(text), "", [], False
    for arg in args:
        if skip:
            options.append(arg)
            skip = False
        elif arg.startswith("-"):
            options.append(arg)
            skip = len(arg) == 2 and arg[1] in SSH_WITH_VALUE
        else:
            host = arg
    if not HOST.match(host):
        raise ValueError("ssh 主机要是 ~/.ssh/config 里的别名或 user@host")
    return host, options


def ssh_text(device):
    """How a device's ssh is written back to the owner: its options, then its host (``-p 22 me@box``); '' for none."""
    return shlex.join([*getattr(device, "options", ()), device.host]) if getattr(device, "host", "") else ""


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


def refresh_hello(state_dir, alias, hello, *, now=None):
    """Write a periodic ``hello`` without losing what the last one said about the tools: a ``hello`` that carries no
    ``tools`` at all (the shim only probes the names it was asked for) keeps the previous ones, of any age. A ``hello``
    that does carry them (a tool reported ``found: false`` included) replaces them as it is."""
    if not hello.get("tools"):
        previous = (read_hello(state_dir, alias, max_age=None) or {}).get("tools")
        if previous:
            hello = {**hello, "tools": previous}
    write_hello(state_dir, alias, hello, now=now)


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


LIST_SECONDS = 5.0
SEARCH_LIMITS = {"depth": 3, "directories": 5000, "seconds": 6, "results": 200, "scan": 4000}
# The directory browser's name search (app/directory_picker.py search_directories), run on the device by ``exec``: the
# same walk (breadth first, no link followed, hidden and node_modules skipped), one round trip. Python 3.9.
SEARCH_SCRIPT = r"""
import json, os, sys, time
path, query = os.path.abspath(os.path.expanduser(sys.argv[1])), sys.argv[2]
depth, directories, seconds, results, scan = (int(x) for x in sys.argv[3:8])
started, needle = time.monotonic(), query.strip().casefold()
hidden = needle.startswith('.')
found, level, scanned, truncated, error = [], [''], 0, False, ''
for _ in range(depth):
    below = []
    for relative in level:
        if scanned >= directories or time.monotonic() - started > seconds:
            truncated = True
            break
        scanned += 1
        names = []
        try:
            with os.scandir(os.path.join(path, relative) if relative else path) as it:
                for count, entry in enumerate(it):
                    if count >= scan or time.monotonic() - started > seconds:
                        truncated = True
                        break
                    link = entry.is_symlink()
                    if link or entry.is_dir(follow_symlinks=False):
                        names.append((entry.name, link))
        except OSError as exc:
            if not relative:
                error = 'not_found' if isinstance(exc, (FileNotFoundError, NotADirectoryError)) else (
                    'permission' if isinstance(exc, PermissionError) else 'other')
                break
            continue
        for name, link in sorted(names, key=lambda item: (item[0].casefold(), item[0])):
            if not hidden and name.startswith('.'):
                continue
            child = relative + '/' + name if relative else name
            if needle in name.casefold():
                found.append({'name': child, 'link': link})
            if not link and name not in ('node_modules', '__pycache__'):
                below.append(child)
    if truncated or error:
        break
    level = below
print(json.dumps({'path': path, 'entries': found[:results], 'more': len(found) > results, 'code': error,
                  'truncated': truncated, 'scanned': scanned}))
"""
CODEX_MODELS = "~/.codex/models_cache.json"
CODEX_TTL = 600.0
BROWSE_CODES = {"not_found": "not_found", "not_dir": "not_found", "permission": "permission"}


def _device_python(state_dir, alias, device):
    return read_profile(state_dir, alias).get("python_exe") or device.python


def _browse_failure(exc):
    """The browser's error code for a failed device call: ``not_found``, ``permission``, ``offline`` (nothing answered)
    or ``other``."""
    if isinstance(exc, transport_module.ShimError):
        return BROWSE_CODES.get(getattr(exc, "code", ""), "other")
    return "offline" if isinstance(exc, (LinkError, OSError)) else "other"


def list_dirs(state_dir, alias, path, *, timeout=LIST_SECONDS, limit=4000, config=None, environ=None):
    """The directories (and links) in ``path`` on ``alias``, listed by the device: ``{"path"`` (the absolute path,
    ``~`` expanded by the device), ``"entries"`` (``[{"name", "link"}]`` by name), ``"truncated"``, ``"code"`` (empty,
    or ``not_found``, ``permission``, ``offline``, ``other``)``}``. One shim ``ls``; the hub looks at no path."""
    try:
        link = open_transport(config if config is not None else current(environ), alias, state_dir)
        push_file(link, shim_bytes())
        answer = link.call("ls", path=path, limit=limit, patience=timeout)
    except (transport_module.DeviceError, OSError) as exc:
        return {"path": path, "entries": [], "truncated": False, "code": _browse_failure(exc), "detail": str(exc)[:200]}
    entries = [{"name": item["name"], "link": item.get("kind") == "link"} for item in answer.get("entries") or []
               if item.get("kind") in ("dir", "link") and isinstance(item.get("name"), str)]
    entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return {"path": answer.get("path", path), "entries": entries, "truncated": bool(answer.get("truncated")), "code": ""}


def search_dirs(state_dir, alias, path, query, *, timeout=None, config=None, environ=None, limits=None):
    """Directories below ``path`` on ``alias`` whose name contains ``query``, found by ``SEARCH_SCRIPT`` on the device
    in one ``exec``: ``{"path", "entries", "more", "truncated", "scanned", "code"}`` like ``list_dirs``."""
    limits = {**SEARCH_LIMITS, **(limits or {})}
    timeout = timeout if timeout is not None else limits["seconds"] + 6.0
    try:
        use = config if config is not None else current(environ)
        link = open_transport(use, alias, state_dir)
        push_file(link, shim_bytes())
        argv = [_device_python(state_dir, alias, use.device(alias)), "-c", SEARCH_SCRIPT, path, query,
                *(str(limits[key]) for key in ("depth", "directories", "seconds", "results", "scan"))]
        answer = link.call("exec", argv=argv, cwd="~", timeout=timeout, max_out=1 << 20, patience=timeout + 5.0)
        found = json.loads((answer.get("out") or "").strip().splitlines()[-1])
    except (transport_module.DeviceError, OSError, ValueError, IndexError) as exc:
        return {"path": path, "entries": [], "truncated": False, "code": _browse_failure(exc), "detail": str(exc)[:200]}
    return found


def codex_root(state_dir, alias, *, refresh=False, ttl=CODEX_TTL, config=None, environ=None):
    """A directory on the hub holding the device's Codex ``models_cache.json`` (what ``core/model_settings.py``
    ``model_catalog(root)`` reads): fetched from the device with the shim's ``get`` when the copy is older than ``ttl``
    seconds (or ``refresh``). A device that does not answer leaves the older copy; none, an empty directory (no
    models). Always the same directory for the alias, so the caller needs no check."""
    root = device_dir(state_dir, alias) / "codex-home"
    target = root / "models_cache.json"
    try:
        fresh = time.time() - target.stat().st_mtime < ttl
    except OSError:
        fresh = False
    if fresh and not refresh:
        return root
    try:
        link = open_transport(config if config is not None else current(environ), alias, state_dir)
        push_file(link, shim_bytes())
        data = link.get_file(CODEX_MODELS, max_bytes=8 << 20, patience=LIST_SECONDS + 10.0)
        write_json(target, json.loads(data.decode("utf-8")), ensure_ascii=False)
    except (transport_module.DeviceError, OSError, ValueError):
        pass
    return root

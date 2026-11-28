"""The CODEX_HOME of a Codex turn that has a device among its environments (Codex 原生快路, core/device_native.py):
``<state>/codex-devices/<设备>`` (``path_of``: the turn's devices, by alias, joined with ``+``), made once and kept, as
backends/codex/consult.py's ``ReadingHome`` is made but never removed (a thread started or resumed in a private home is
recorded under it, tests/test_collab_c8.py, so this one has to stay).

Every entry of the real CODEX_HOME is a symbolic link to it (the sign-in, config.toml, sessions/, the state databases: a
thread here is the owner's thread, found from his own home and from another device home too, tests/test_codex_native.py)
except ``OWN``:

- ``environments.toml``, Covey's own: one ``ssh`` environment for each of the turn's devices (core/device_native.py
  ``entries``, ``toml_text``). One home per set of devices, not one for them all: the app server connects every
  environment its file lists (codex-cli 0.172.0, measured), so a topic's turn reaches its own devices alone, and two
  topics with other devices never rewrite one file under each other. It is never put into the real CODEX_HOME: a thread
  that names no environments gets every one listed there, the owner's own Codex too.
- ``rules/``, a directory of its own: a copy of each of the owner's rule files (Codex reads no rule file that is a link,
  codex-cli 0.172.0, measured: tests/test_codex_native.py), brought up to date before each turn, and Covey's
  ``covey-devices.rules`` (core/device_native.py ``rules_text``: G4 on this path).

``prepare(state_dir, env, entries)`` brings the home up to date before each such turn, and is all a turn does to it: a
link made for an entry the real home got since, a link whose entry went removed, environments.toml and Covey's rules
written again (whole, renamed into place) only when they say something else. Nothing is ever followed or removed but
those links and those files; an entry the app server made here of its own is left as it is.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from covey.backends.codex.consult import real_home
from covey.core.device_native import RULES_FILE, rules_text, toml_text
from covey.core.state import atomic_write_bytes, ensure_private_dir

NAME = "codex-devices"  # Under the state directory.
OWN = ("environments.toml", "rules")
SPARE = ".covey-link"  # A link being put in place of another (``_link``).
KEY_MAX = 96


def path_of(state_dir, ids) -> Path:
    """The device home of a set of devices (their aliases: core/devices.py ALIAS, so a safe file name), or of a set too
    long for one name, its hash."""
    key = "+".join(sorted(dict.fromkeys(ids))) or "none"
    if len(key) > KEY_MAX:
        key = "set-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(state_dir) / NAME / key


def _link(target: str, path: Path) -> None:
    """``path`` a symbolic link to ``target``: made, or replaced when it names another (a link only, never a file)."""
    try:
        if os.readlink(path) == target:
            return
    except FileNotFoundError:
        try:
            os.symlink(target, path)
        except FileExistsError:  # Another turn made it at the same moment.
            pass
        return
    except OSError:  # Not a link: something the app server made here, left as it is.
        return
    spare = path.with_name(path.name + SPARE)
    try:
        os.unlink(spare)
    except FileNotFoundError:
        pass
    os.symlink(target, spare)
    os.replace(spare, path)


def _mirror(source: Path, folder: Path, skip) -> None:
    """Each entry of ``source`` but ``skip`` a link in ``folder``; a link there whose entry went, removed."""
    try:
        names = {entry.name: entry.path for entry in os.scandir(source) if entry.name not in skip}
    except OSError:
        names = {}
    for name, target in names.items():
        _link(target, folder / name)
    for entry in os.scandir(folder):
        if entry.is_symlink() and entry.name not in names and not entry.name.endswith(SPARE):
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass


def _copies(source: Path, folder: Path, keep) -> None:
    """A copy in ``folder`` of each file of ``source`` (its own, or what a link there names); every other file there but
    ``keep`` removed."""
    found = {}
    try:
        for entry in os.scandir(source):
            if entry.name not in keep and entry.is_file():
                try:
                    found[entry.name] = Path(entry.path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
    except OSError:
        found = {}
    for name, text in found.items():
        _write(folder / name, text)
    for entry in os.scandir(folder):
        if entry.name not in found and entry.name not in keep and not entry.name.startswith("."):
            try:
                os.unlink(entry.path)
            except (FileNotFoundError, IsADirectoryError, PermissionError):
                pass


def _write(target: Path, text: str) -> None:
    """``text`` as the whole of ``target`` (renamed into place), only when it says something else (or is a link)."""
    data = text.encode("utf-8")
    try:
        current = target.read_bytes() if not target.is_symlink() else b""
    except OSError:
        current = b""
    if current != data or target.is_symlink():
        if target.is_symlink():
            os.unlink(target)
        atomic_write_bytes(target, data)


def prepare(state_dir, env, entries) -> dict:
    """``env`` with CODEX_HOME the device home of ``entries``' devices, its links, environments.toml and rules up to date."""
    home = path_of(state_dir, [item["id"] for item in entries])
    ensure_private_dir(home.parent)
    ensure_private_dir(home)
    real = real_home(env)
    try:
        same = real.resolve() == home.resolve()
    except OSError:
        same = True
    if not same:
        _mirror(real, home, OWN)
    _write(home / OWN[0], toml_text(entries))
    rules = home / "rules"
    if rules.is_symlink():  # An older home linked the owner's whole folder.
        os.unlink(rules)
    ensure_private_dir(rules)
    if not same:
        _copies(real / "rules", rules, (RULES_FILE,))
    _write(rules / RULES_FILE, rules_text())
    return {**dict(env or {}), "CODEX_HOME": str(home)}

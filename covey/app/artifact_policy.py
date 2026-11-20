"""What of a task's artifacts may be published: the denylist of configuration and credential names, resolving a recorded path, packing a directory.

Judged on the hub, and for a device's artifacts through the device program (app/device_files.py).
"""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from pathlib import Path

from covey.app import device_files
from covey.core import extensions
from covey.core.artifact_paths import artifact_key, artifact_paths
from covey.core.state import read_json

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_DRIVE_ARTIFACT_BYTES = 200 * 1024 * 1024
MAX_DIRECTORY_FILES = 5000
AGENT_DIRS = {".claude", *extensions.agent_dirs()}  # Where an agent keeps its worktrees (an extension adds its own).
SENSITIVE_NAMES = {".git", ".ssh", ".codex", "credentials.json", "auth.json", *AGENT_DIRS}
DIFF_PRUNED = "差异报告已过期清理"
# A 代答 contact's notes (core/proxy.py session_dir_name) and the run data the model keeps beside them:
# every 代答 turn recorded its README.md, and the card uploaded it again each time (UI review P8).
# Where they are kept is a deployment's (the extension's ``contact_notes`` setting, a regex); none by default.
CONTACT_NOTES = re.compile(extensions.setting("contact_notes", r"(?!)"))


def _local_path(record, raw):
    """``raw`` resolved against the task's workdir; ValueError for one that cannot be resolved
    (a symlink loop, ``~nosuchuser``), which the card then shows as that file's error. A path of a device
    (app/device_files.py) is never resolved here, the hub's links and home are not the device's: it is folded
    as text, against the device's working directory."""
    if device_files.is_remote(record, raw):
        return device_files.remote_path(record, raw)
    try:
        root = Path(record["workdir"]).resolve()
        path = Path(raw).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("产物路径无法解析") from exc


def _entry_name(name):
    """A zip entry name for a file name that may hold bytes that are not UTF-8 (os.fsdecode keeps them
    as surrogates, which a zip cannot store): those bytes become U+FFFD."""
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _secret_name(name):
    return name in SENSITIVE_NAMES or name == ".env" or name.startswith(".env.")


def _judged_parts(path):
    """The parts the denylist judges. Below an agent worktree (``<agent dir>/worktrees/<name>/``, as
    ``.claude/worktrees/<name>/``) the path is the agent's working copy, so the agent dir
    and ``worktrees`` are left out; everything else, the worktree name included, is still judged."""
    parts = path.parts
    for i, part in enumerate(parts[:-3]):
        if part in AGENT_DIRS and parts[i + 1] == "worktrees":
            return parts[:i] + parts[i + 2:]
    return parts


def device_policy():
    """The denylist a device's ``stat`` is given and the hub's own rule over the real path it reports back
    (app/device_files.py): the names and directories this module never publishes."""
    def denied(path):
        parts = _judged_parts(path)
        return ".local-state" in parts or any(part in SENSITIVE_NAMES for part in parts) or _secret_name(path.name)

    return device_files.Policy(tuple(sorted({*SENSITIVE_NAMES, ".env", ".env.*", ".local-state"})),
                               tuple(sorted("~/" + name for name in {".ssh", ".codex", *AGENT_DIRS})), denied)


def config_path(record, raw):
    """A configuration or credential path: never published, and left off the card."""
    if raw in {record.get("diff_artifact_path"), record.get("result_file_path")}:
        return False
    try:
        path = _local_path(record, raw)  # Resolved first, so ../ and links are judged where they lead.
    except ValueError:
        return False  # Not judged here: resolve_artifact reports it on the card.
    parts = _judged_parts(path)
    # A device's paths are not the hub's: its artifacts never live in the checkpoint's artifacts/ here.
    state_artifact = (bool(record.get("checkpoint_path")) and not device_files.is_remote(record, raw)
                      and path.is_relative_to(Path(record["checkpoint_path"]).resolve().parent / "artifacts"))
    return ((".local-state" in parts and not state_artifact) or
            any(part in SENSITIVE_NAMES for part in parts) or _secret_name(path.name))


def proxy_task(record):
    """Whether ``record`` is a 代答 task: the topic.json beside its history names the contact it answers."""
    try:
        return bool(read_json(Path(record["history_path"]).parent / "topic.json").get("proxy"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def contact_notes(record, raw):
    """A file of a 代答 contact's notes or their run data, judged where it resolves to."""
    try:
        path = _local_path(record, raw)
    except ValueError:
        path = Path(raw)
    return bool(CONTACT_NOTES.search(path.as_posix()))


def drive_paths(record):
    """The artifacts the Drive upload and its panel deal with: configuration and credential paths are
    skipped quietly, and so are, for a 代答 task, its contact's notes."""
    notes = proxy_task(record)
    return [raw for raw in artifact_paths(record)
            if not config_path(record, raw) and not (notes and contact_notes(record, raw))]


def resolve_artifact(record, key, max_bytes=MAX_ARTIFACT_BYTES, directories=False, state_dir=None):
    """The hub file for the artifact ``key`` of ``record``. A device's artifact is copied here first, through the
    device program (app/device_files.py; ``state_dir`` is where its copy goes, ValueError without it): a directory
    of a device is not one yet."""
    raw = next((p for p in artifact_paths(record) if artifact_key(p) == key), None)
    if raw is None:
        raise ValueError("产物记录已变化，请重新打开产物列表")
    path = _local_path(record, raw)
    if config_path(record, raw):
        raise ValueError("该文件属于配置或凭据目录，不能作为产物发送")
    if device_files.is_remote(record, raw):
        if state_dir is None:
            raise ValueError("设备上的产物需要状态目录才能取回")
        return device_files.fetch_artifact(state_dir, record, raw, max_bytes, device_policy())
    if path.is_dir():
        if directories:
            return path
        raise ValueError("这是目录，暂不支持发送")
    if not path.exists():
        # Maintenance bounds job-diffs/; the copy uploaded with the task's artifacts stays in Drive.
        raise ValueError(DIFF_PRUNED if raw == record.get("diff_artifact_path") else "产物文件不存在")
    if not path.is_file():
        raise ValueError("该产物不是普通文件，暂不支持发送")
    if not 0 < path.stat().st_size <= max_bytes:
        raise ValueError(f"仅支持非空且不超过 {max_bytes // (1024 * 1024)} MB 的文件")
    return path


def zip_directory(source, dest, max_bytes=MAX_DRIVE_ARTIFACT_BYTES, max_files=MAX_DIRECTORY_FILES):
    """Pack a registered directory as ``dest`` and return its SHA-256.

    Configuration and credential entries, symbolic links and special files are left out. Both caps
    are checked before anything is written. Entries are sorted and carry a fixed time and mode, so an
    unchanged directory packs to the same bytes and is uploaded once.
    """
    too_big = f"目录超过 {max_bytes // (1024 * 1024)} MB，未打包"
    entries, total, pending = [], 0, [(source, _entry_name(source.name or "artifact"))]
    try:
        while pending:
            folder, prefix = pending.pop()
            with os.scandir(folder) as listing:
                for entry in listing:
                    if _secret_name(entry.name) or entry.name == ".local-state" or entry.is_symlink():
                        continue
                    name = f"{prefix}/{_entry_name(entry.name)}"
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((entry.path, name))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        entries.append((name, entry.path, size))
                        total += size
                        if len(entries) > max_files:
                            raise ValueError(f"目录超过 {max_files} 个文件，未打包")
                        if total > max_bytes:
                            raise ValueError(too_big)
        if not entries:
            raise ValueError("目录里没有可打包的文件")
        written = 0
        with zipfile.ZipFile(dest, "w") as archive:
            for name, path, size in sorted(entries):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type, info.create_system, info.external_attr = zipfile.ZIP_DEFLATED, 3, 0o100644 << 16
                info.file_size = size
                with open(path, "rb") as incoming, archive.open(info, "w") as out:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(too_big)
                        out.write(chunk)
        digest = hashlib.sha256()
        with dest.open("rb") as packed:
            for chunk in iter(lambda: packed.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ValueError("目录里有读不了的文件，未打包") from exc

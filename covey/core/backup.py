"""The daily backup of a state directory: what it holds, writing and verifying one, restoring it, and which old ones go.

A backup copies with no lock held (core/maintenance.py says why that is safe); ``back_up`` is today's alone,
jobs running or not."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import time
from pathlib import Path

from covey.core.state import DAY, read_json, read_object, write_json


BACKUP_KEEP = 7


BACKUP_BYTES = 400 * 1024 * 1024  # The newest backup is kept whatever its size.


BACKUP_DIRS = {
    "topics",
    "jobs",
    "checkpoints",
    "browse-cards",
    "artifact-deliveries",
    "questions",
    "held-notes",  # The notes kept while several questions wait: they go with the answer (questions/ is backed up).
    "interaction-deliveries",
    "result-files",
    "model-settings",
    "verified-results",
    "native-session-guards",
    "system-rules-sessions",
    "delivery-receipts",
    "backend-health",
    "launch-forms",
    "workspace-managers",
    "traces",
    "queued",
    "groups",  # Which groups serve their members: a group the owner turned off stays off after a restore.
    "launch-defaults",  # Each chat's last launch, one small file per chat: launch cards preselect as before a restore.
    # 代答: of proxy/ only its settings, whitelist and the owner's style note; never web/
    # (the owner's Feishu web login), send-as-user/ (the send tool's credentials), shots/, batches/ or tmp/.
    "proxy/contacts",
    # 专项 (core/projects.py) and 整理专项's decisions (projects/tidy/); of a project's folder its memory, index and inbox,
    # not the files its library copied (BACKUP_SKIP).
    "projects",
    "knowledge",  # 知识条目 (core/knowledge.py): what the owner asked to be remembered.
    "status-cards",  # 手上的事's topic cards it pinned: Feishu lists no pins of a private chat (app/pins.py).
    # 网页工作台 (core/web_sync.py): the ledger of the page's operations, so a restore does none of them twice; its key
    # is in the config's key_file, outside the state directory.
    "web",
}


# model-usage.json: the chats' recent model selections, so the model selects keep their order after a restore.
# principles.json, main-thread.json and redaction/config.json are the owner's: his principles (and so his memory's,
# core/memory.py), the butler's settings and the sensitive terms. owner-chat.json names the private chat the menu,
# 手上的事 and the digests go to; migrations.json the migrations the records backed up with it have had (core/migrate.py).
# The pinned board's records (pinned-overview.json, pinned-topics.json, board-priority.json, topic-seen.json) and
# digest.json: what is pinned and sent is known from them alone, so a restore pins and sends nothing twice; so is the
# welcome card's (welcome.json, core/welcome.py), sent once a deployment.
BACKUP_FILES = {"workspace-config.json", "session_index.jsonl", "bot-identity.json", "launch-defaults.json",
                "model-usage.json", "proxy/settings.json", "proxy/style.md", "proxy/owner-context.md",
                "principles.json", "main-thread.json", "redaction/config.json", "owner-chat.json", "migrations.json",
                "pinned-overview.json", "pinned-topics.json", "board-priority.json", "topic-seen.json", "digest.json",
                "welcome.json"}


# Folders under a backed-up directory that a backup leaves out, by the directory: a project's library/ holds copies of
# the files the owner gave it, up to core/project_library.py PROJECT_LIMIT a project; its index library.json goes.
BACKUP_SKIP = {"projects": frozenset({"library"})}


# What a backup leaves out of a state directory, and why. With BACKUP_DIRS and BACKUP_FILES it decides every entry
# docs/operations.md's 状态目录 lists (tests/test_maintenance.py BackupListTest): an entry added there, or a new name
# the code puts under a state directory, says here or there whether a backup holds it. A directory backed up in
# part (proxy/, redaction/) is decided by its parts in the two lists.
NOT_BACKED_UP = {
    "bridge.pid": "the running Bridge's process number",
    "bridge.log*": "logs",
    "bridge-console.log*": "logs",
    "maintenance-status.json": "what the last maintenance of this directory did",
    "backups": "the backups themselves",
    "tmp": "temporary files of the Bridge and its task runners",
    "runs": "task runners' run directories: a restore starts no task",
    "inbound-messages": "marks against Feishu delivering a message twice, kept 7 days",
    "answer-cards": "answer cards of the last 7 days, for their 产物 links",
    "job-diffs": "each turn's diff report, up to 200 MiB, uploaded to the cloud space",
    "artifact-drive": "the uploads of 产物 to the cloud space",
    "artifact-upload": "files on their way to the cloud space",
    "turn-exports": "下载请求与结果's packs of sent cards",
    "policy-stops": "official refusals' stop records (policy-stop.md)",
    "policy-inbox": "the feed of official refusals to the running Bridge",
    "policy-source*.json": "the feed of official refusals to the running Bridge",
    "cards": "card locks",
    "switch-forms": "the state of cards already sent",
    "rewind-forms": "the state of cards already sent",
    "menu-panels.json": "the state of cards already sent",
    "settings-cards": "the state of cards already sent",
    "group-managers": "the state of cards already sent",
    "main-thread-panels": "the state of cards already sent",
    "weekly-usage-cards": "the state of cards already sent",
    "digest-boards": "the state of cards already sent",
    "dashboard-links": "the state of cards already sent",
    "topic-links.json": "a cache of what Feishu said of messages (app/topic_links.py)",
    "main-thread": "the butler's tool requests and send-once ledgers, kept 7 days",
    "app-setup": "自动补齐飞书应用's runs",
    "owner-send": "代发's drafts and sends, tied to cards already sent",
    "patrol": "what the butler would have said (COVEY_PATROL=shadow), kept 14 days",
    "devices": "a device's working files on the hub",
    "nodes": "节点's keys (credentials) and samples",
    "stuck-check": "the working directory of a small model's run",
    "standing-forms": "the working directory of a small model's run",
    "track-judge": "the working directory of a small model's run",
    "launch-models": "model lists, asked again",
    "native-models": "model lists, asked again",
    "pi-sessions": "a backend's own sessions: no Harness's session store is backed up",
    "<网关名>": "a model gateway's relay log",
    "<扩展>-sessions": "a backend's own sessions: no Harness's session store is backed up",
    "claude-diagnostics": "diagnostics",
    "<扩展>-diagnostics": "diagnostics",
}


def backup_paths(state):
    """The files a backup holds: no lock files, writers' temporary files, symlinked folders or ``BACKUP_SKIP``."""
    paths = [state / name for name in sorted(BACKUP_FILES)]
    for name in sorted(BACKUP_DIRS):
        if (state / name).is_symlink():
            continue
        skipped = BACKUP_SKIP.get(name, frozenset())
        for folder, dirs, files in os.walk(state / name):
            dirs[:] = sorted(d for d in dirs if d != ".locks" and d not in skipped)
            paths += [Path(folder) / f for f in sorted(files) if not (f.startswith(".") and f.endswith(".tmp"))]
    return paths


def _backup_bytes(path):
    """One file's content for the backup; None when it is gone or not a regular file.

    Records are replaced whole, so this reads one version of them. An append-only log
    (.jsonl) may be mid-append: its copy ends at the last complete line."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data[:data.rfind(b"\n") + 1] if path.suffix == ".jsonl" else data


def create_backup(state):
    """Write a verified backup of ``state``; it takes no lock (see the module docstring)."""
    folder = state / "backups"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = folder / f"bridge-{time.time_ns()}"
    archive = base.with_suffix(".tar.gz")
    temporary = base.with_suffix(".tmp")
    manifest = {}
    with tarfile.open(temporary, "w:gz") as tar:
        for path in backup_paths(state):
            data = _backup_bytes(path)
            if data is None:
                continue
            rel = str(path.relative_to(state))
            manifest[rel] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    os.chmod(temporary, 0o600)
    write_json(base.with_suffix(".manifest.json"), manifest, ensure_ascii=False, indent=2)
    write_json(base.with_suffix(".metadata.json"), {"source_state": str(state.resolve()), "created_at": time.time()})
    verify_backup(temporary, manifest)
    # Last: a .tar.gz is always complete and verified. Earlier leftovers are swept (old_backups).
    temporary.replace(archive)
    return archive


def verify_backup(archive, manifest=None):
    """Check every file of ``archive`` against ``manifest`` (default: its .manifest.json)."""
    if manifest is None:
        manifest = read_json(archive.with_name(archive.name.removesuffix(".tar.gz") + ".manifest.json"))
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) != len(manifest):
            raise ValueError("备份文件数不一致")
        for member in members:
            relative = Path(member.name)
            if not member.isfile() or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("备份包含不安全路径")
            if hashlib.sha256(tar.extractfile(member).read()).hexdigest() != manifest.get(member.name):
                raise ValueError("备份校验失败")
    return manifest


def restore_backup(archive, destination):
    verify_backup(archive)
    if destination.exists():
        raise ValueError("恢复目标必须是尚不存在的新目录")
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("wb") as output:
                output.write(tar.extractfile(member).read())
            target.chmod(0o600)
    metadata = archive.with_name(archive.name.removesuffix(".tar.gz") + ".metadata.json")
    if metadata.exists():
        old_root = read_json(metadata)["source_state"]

        def relocate(value):
            if isinstance(value, str) and value.startswith(old_root + "/"):
                return str(destination.resolve()) + value[len(old_root) :]
            if isinstance(value, dict):
                return {k: relocate(v) for k, v in value.items()}
            if isinstance(value, list):
                return [relocate(v) for v in value]
            return value

        for path in destination.rglob("*.json"):
            write_json(path, relocate(read_json(path)), ensure_ascii=False)


def backup_sets(state):
    """The backups in backups/, newest first: (base name, bytes, complete, last change). One is
    complete once its .tar.gz exists (create_backup renames it last); before that it is a
    backup being written, or what a stop in the middle left (.tmp, manifest, metadata)."""
    sets = {}
    folder = state / "backups"
    for path in folder.glob("bridge-*") if folder.is_dir() else []:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        entry = sets.setdefault(path.name.split(".", 1)[0], [0, False, 0.0])
        entry[0] += info.st_size
        entry[1] = entry[1] or path.name.endswith(".tar.gz")
        entry[2] = max(entry[2], info.st_mtime)
    return [(base, *entry) for base, entry in sorted(sets.items(), reverse=True)]


def old_backups(state, keep=BACKUP_KEEP, max_bytes=BACKUP_BYTES, *, now=None, incoming=None):
    """Backups to remove: past the newest ``keep`` or past ``max_bytes`` in total, newest kept
    first, and what an interrupted backup left a day ago. A backup still being written counts
    towards ``max_bytes`` and is never removed. ``incoming``: the size of a backup about to be
    written, counted as the newest (a preview). Each is named by its .tar.gz, or its base name."""
    now = time.time() if now is None else now
    total, number, old = incoming or 0, 0 if incoming is None else 1, []
    for base, size, complete, changed in backup_sets(state):
        name = str(state / "backups" / (base + ".tar.gz" if complete else base))
        total += size
        if not complete:
            if changed < now - DAY:
                old.append(name)
            continue
        if number and (number >= max(1, keep) or total > max_bytes):
            old.append(name)
        number += 1
    return old


def remove_backup(name):
    """Remove one backup of ``old_backups``: its archive or leftover .tmp, manifest and metadata."""
    path = Path(name)
    base = path.name.removesuffix(".tar.gz")
    for suffix in (".tar.gz", ".tmp", ".manifest.json", ".metadata.json"):
        (path.parent / (base + suffix)).unlink(missing_ok=True)


def back_up(state, *, keep=BACKUP_KEEP, now=None):
    """Today's backup alone, jobs running or not: it reads the state and holds no lock (``create_backup``), and
    the oldest backups go as after any. ``last_backup`` in maintenance-status.json says when; the cleanup's
    ``last_completed`` and plan stay as they were."""
    state = state.resolve()
    now = time.time() if now is None else now
    backup = create_backup(state)
    for name in old_backups(state, keep, now=now):
        remove_backup(name)
    status = state / "maintenance-status.json"
    write_json(status, {**read_object(status), "last_backup": now}, ensure_ascii=False)
    return backup


def last_backup(status):
    """When the last daily backup was made, from maintenance-status.json: ``last_backup``, or before there was
    one (an older release's) the last maintenance, which always made one."""
    return float(status.get("last_backup") or status.get("last_completed") or 0)

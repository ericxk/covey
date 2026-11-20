"""Files between the hub and a device's topic, always through the device program (core/device_shim.py: ``stat``, ``get``,
``put``): the hub never opens a path of the device, and the device's file system is never reached with a hub path.

In, the attachments of a message (``stage``): the files the prompt names under the topic's ``incoming_media`` are put in
``<device home>/.covey/incoming/<hub id>/<job key>/`` before the turn, and the prompt names the copies instead. The
device's task directory would be the natural place (A-SPEC 5.2), but its key is made when the process is spawned, after
the prompt is written; a job's key is the same on every try. One file is at most MAX_FILE bytes and one turn's files
at most MAX_TURN; what does not fit, or cannot be put, is left out and said so: in the prompt (in place of its path), on
the task card and in the log. An image the Harness takes whole (Claude's base64, from the hub's file) keeps its hub
path in ``RunInputs.attachments`` and gains a ``device_path``, which Codex's ``localImage`` names.

Out, the files the Agent lists as artifacts (``fetch_artifact``) and the images it shows at once
(``wanted_images``, ``RemoteImage``): one ``stat`` on the device judges them there (``realpath``, whether they leave the
directory, the denylist of app/artifact_policy.py as ``deny_names`` and ``deny_dirs``), the hub judges the ``realpath`` it is
told once more with the same rule (``_judged_parts``), then ``get`` copies the file to
``<state>/runs/<job key>/device-files/<h>/`` and the upload goes on from that copy as it does for a file of this
machine. A device's artifact must be inside its working directory (``escapes``); an image may be anywhere, as on this
machine, a QR code is saved in /tmp. A directory is not an artifact on a device yet. The 输入/输出 box of an answer card
(app/answer_cards.py) shows no size of a device's file.
"""
from __future__ import annotations

import dataclasses
import hashlib
import posixpath
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from covey.core import device_transport as dt
from covey.core import devices
from covey.core.attachments import referenced_files
from covey.core.jobs import job_key
from covey.core.run_dir import run_dir
from covey.core.state import atomic_write_bytes

MAX_FILE = 64 * 1024 * 1024  # One attachment put on a device.
MAX_TURN = 256 * 1024 * 1024  # The attachments of one turn.
SHOWN = 5  # Files named in the task card's line.
NOT_PUT = "未传到设备"  # What stands in the prompt for the path of a file that was not put there.
FOLDER = "device-files"
STAT_SECONDS = 20.0


def _megabytes(size):
    return "%.1f MB" % (size / 1024 / 1024)


def link_to(state_dir, alias):
    """The transport to device ``alias`` with the device program there (one confirmation per minute)."""
    link = devices.open_transport(devices.current(), alias, state_dir)
    devices.push_file(link, dt.shim_bytes())
    return link


def failure_text(alias, exc):
    """What a card says for a device call that failed: the device's own word for a refusal, else that it did not answer."""
    if isinstance(exc, dt.ShimError):
        return "设备 %s 上读写失败（%s）" % (alias, getattr(exc, "code", "shim"))
    return "设备 %s 没有答复，稍后再试" % alias


# ---------------------------------------------------------------- in: attachments

@dataclasses.dataclass
class Staged:
    """``stage``'s answer: the prompt and image attachments to run with, the device directory the files went to ('' when
    none went), ``put`` and ``skipped`` as ``(name, size, device path or reason)``."""
    prompt: str
    attachments: list
    directory: str = ""
    put: list = dataclasses.field(default_factory=list)
    skipped: list = dataclasses.field(default_factory=list)

    def card_line(self, alias):
        """The line for the task card about the files left out ('' when none)."""
        if not self.skipped:
            return ""
        names = "；".join("%s（%s，%s）" % (name, _megabytes(size), why) for name, size, why in self.skipped[:SHOWN])
        more = "；等共 %d 个" % len(self.skipped) if len(self.skipped) > SHOWN else ""
        return "%d 个附件没有传到设备 %s：%s%s。模型读不到这些文件。" % (len(self.skipped), alias, names, more)


def incoming_dir(state_dir, alias, key, link=None):
    """Where the attachments of job ``key`` are put on ``alias``: absolute, under the device user's home (the profile's, else
    the device's own answer to ``stat`` of ``~``): a Harness's file tools do not expand ``~``."""
    home = str(devices.read_profile(state_dir, alias).get("home") or "")
    if not home.startswith("/"):
        link = link or link_to(state_dir, alias)
        found = link.call("stat", paths=["~"], patience=STAT_SECONDS).get("results") or [{}]
        home = str(found[0].get("path") or "")
        if not home.startswith("/"):
            raise dt.BadFrame("%s did not say where its home is" % alias)
    return "%s/.covey/incoming/%s/%s" % (home.rstrip("/"), devices.hub_id(state_dir), key)


def _safe_name(name):
    name = re.sub(r"[^\w.\-]", "_", name).lstrip(".") or "file"
    return name[:120]


def stage(ctx, job, prompt, attachments=()):
    """Put the files ``prompt`` names under the topic's ``incoming_media`` on the device of ``ctx`` and return the turn's
    ``Staged``: the prompt with their device paths in place of the hub's (a path of those directories that is left over
    is replaced too), ``attachments`` (the image items) with a ``device_path`` each and a ``shown`` (what the prompt says
    for it). Never raises: a file that cannot be put is skipped with its reason; once the device does not answer, the
    rest are skipped without trying."""
    roots = [Path(ctx.history_path).parent / "incoming_media", Path(ctx.state_dir) / "incoming_media"]
    found = referenced_files(prompt, *roots)
    if not found:
        return Staged(prompt, list(attachments))
    alias, key = ctx.device, job_key(job.message_id, job.job_id)
    result = Staged(prompt, [])
    where, used, names, down = {}, 0, {}, ""
    link = directory = None
    for path, words in found:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        why = (down or ("超过单个文件 %d MiB 的上限" % (MAX_FILE >> 20) if size > MAX_FILE else "")
               or ("本轮附件合计超过 %d MiB" % (MAX_TURN >> 20) if used + size > MAX_TURN else ""))
        if not why:
            try:
                data = path.read_bytes()
            except OSError:
                why = "本机读不了这个文件"
        if not why:
            name = _safe_name(path.name)
            if names.setdefault(name, path) != path:  # Two files of one name: the second's gets its own.
                name = hashlib.sha256(str(path).encode()).hexdigest()[:8] + "-" + name
                names[name] = path
            try:
                if link is None:
                    link = link_to(ctx.state_dir, alias)
                    directory = incoming_dir(ctx.state_dir, alias, key, link)
                link.put_file("%s/%s" % (directory, name), data, mode=0o600)
            except (dt.DeviceError, ValueError, OSError) as exc:  # The device is not answering: the rest are not tried.
                down = why = failure_text(alias, exc)
        if why:
            result.skipped.append((path.name, size, why))
            continue
        used += size
        where[path] = "%s/%s" % (directory, name)
        result.put.append((path.name, size, where[path]))
    result.directory = directory if where else ""
    replacements = {}
    for path, words in found:
        for word in words:
            replacements[word] = where.get(path, NOT_PUT)
    text = _replace(prompt, replacements)
    text = _sweep(text, roots)
    if result.skipped:
        text += "\n\n[这些附件没有传到设备 %s，那里读不到：\n%s]" % (alias, "\n".join(
            "- %s（%s，%s）" % (name, _megabytes(size), why) for name, size, why in result.skipped))
    result.prompt = text
    for item in attachments:
        mine = where.get(Path(item["path"]).resolve())
        result.attachments.append({**item, "device_path": mine, "shown": mine or NOT_PUT} if mine
                                  else {**item, "shown": NOT_PUT})
    return result


def stage_message(ctx, job, text, *, log=None, say=None):
    """``text`` as it goes into a turn that is already running on a device (a supplement, the riders of a native answer):
    the files it names under ``incoming_media`` are put on the device as ``stage`` does for a first turn, and the text
    names the copies (a file that cannot be put is left out, with the same lines). ``text`` itself for a job on this
    machine and for a text with no such file; it never raises. ``log(line)`` gets the one log line, ``say(line)`` the line
    for the user about the files left out."""
    device = str(getattr(job, "device", "") or getattr(ctx, "device", "") or "")
    if not device or not text:
        return text
    if getattr(ctx, "device", "") != device:
        ctx = dataclasses.replace(ctx, device=device, remote_workdir=getattr(job, "remote_workdir", ""))
    try:
        staged = stage(ctx, job, text)
    except Exception as exc:  # noqa: BLE001 - the message is sent as it is rather than not at all
        if log:
            log("device supplement files not staged: %s" % type(exc).__name__)
        return text
    if log and (staged.put or staged.skipped):
        log("device attachments job=#%s device=%s put=%d skipped=%d bytes=%d (a message into the running turn)" % (
            job.job_id, device, len(staged.put), len(staged.skipped), sum(size for _, size, _ in staged.put)))
    line = staged.card_line(device)
    if line and say:
        try:
            say(line)
        except Exception as exc:  # noqa: BLE001 - the message goes on; only the line failed
            if log:
                log("device attachments note not shown: %s" % type(exc).__name__)
    return staged.prompt


def _replace(text, replacements):
    """``text`` with each key of ``replacements`` replaced in one pass, the longest first (a path that is the start of
    another is not cut out of it)."""
    if not replacements:
        return text
    pattern = re.compile("|".join(re.escape(word) for word in sorted(replacements, key=len, reverse=True)))
    return pattern.sub(lambda found: replacements[found.group(0)], text)


def _sweep(text, roots):
    """``text`` without a path of the directories the attachments are kept in on the hub that the words did not name as
    one (a path written bare, a spelling of the directory ``referenced_files`` does not read): the device has no such
    file, and the hub's own directory is nothing to show it."""
    for root in {str(root) for root in roots} | {str(root.resolve()) for root in roots}:
        text = re.sub(re.escape(root) + r"(?:/[^\s`\]）)\"']*)?", NOT_PUT, text)
    return text


# ---------------------------------------------------------------- out: artifacts and images

def is_remote(record, raw):
    """Whether ``raw``, a path in the job ``record``'s artifacts, is a path on the record's device. The files of the hub's
    own (the long reply saved as a file, the turn's diff) are listed beside the Agent's with their hub paths."""
    return bool(record.get("device")) and raw not in {record.get("result_file_path"), record.get("diff_artifact_path")}


def remote_path(record, raw):
    """``raw`` as a ``PurePosixPath`` on the device: against its working directory when relative, folded (``..``, ``//``)
    without any file system (the device's links are the device's to follow, ``stat`` does it); ``~`` is its home."""
    text = str(raw)
    if text.startswith("/"):
        return PurePosixPath(posixpath.normpath(text))
    if text.startswith("~"):
        return PurePosixPath(devices.normalize_remote(text))
    return PurePosixPath(devices.normalize_remote(posixpath.join(record.get("remote_workdir") or "~", text)))


@dataclasses.dataclass(frozen=True)
class Policy:
    """What the hub never publishes (app/artifact_policy.py ``device_policy``): ``names``, ``fnmatch`` patterns of a path's parts and
    ``dirs``, directories (``~`` the device's home), both sent to the device's ``stat`` as ``deny_names`` and
    ``deny_dirs``, and ``denied``, the hub's own rule over a path (a ``PurePosixPath``) the device reported as the real one.
    Passed in, so that this module imports nothing of the app."""
    names: tuple
    dirs: tuple
    denied: Callable


def look(state_dir, alias, paths, policy, *, base, root=None, link=None):
    """One ``stat`` of ``paths`` on ``alias`` (relative ones against ``base``, ``root`` the directory they may not leave):
    the answers, one per path, in order."""
    fields = {"paths": list(paths), "base": base, "deny_names": list(policy.names), "deny_dirs": list(policy.dirs)}
    if root:
        fields["root"] = root
    link = link or link_to(state_dir, alias)
    return link.call("stat", patience=STAT_SECONDS, **fields).get("results") or []


def _copy_to(state_dir, key, alias, real, max_bytes):
    """The hub's copy of the device's file ``real`` at ``<state>/runs/<key>/device-files/<h>/<name>`` (its ``get``)."""
    data = link_to(state_dir, alias).get_file(real, max_bytes=max_bytes)
    name = PurePosixPath(real).name or "file"
    folder = run_dir(state_dir, key) / FOLDER / hashlib.sha256(("%s:%s" % (alias, real)).encode()).hexdigest()[:24]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_bytes(folder / name, data, prefix=".part-", suffix="", notify=False)
    return folder / name


def fetch_artifact(state_dir, record, raw, max_bytes, policy):
    """The artifact ``raw`` of the device job ``record`` copied to the hub: its path. ValueError, with the words the card
    shows, for a path that is denied, outside the device's working directory, missing, a directory, not a regular file, empty
    or over ``max_bytes``, or when the device did not answer."""
    alias = record["device"]
    try:
        item = (look(state_dir, alias, [raw], policy, base=record["remote_workdir"], root=record["remote_workdir"]) or [{}])[0]
        if item.get("denied") or policy.denied(PurePosixPath(str(item.get("realpath") or raw))):
            raise ValueError("该文件属于配置或凭据目录，不能作为产物发送")
        if item.get("escapes"):
            raise ValueError("该文件在设备 %s 的工作区之外，不能作为产物发送" % alias)
        if not item.get("exists"):
            raise ValueError("产物文件不存在")
        if item.get("is_dir"):
            raise ValueError("设备上的目录暂不支持作为产物，请打包成一个文件")
        if not item.get("is_file"):
            raise ValueError("该产物不是普通文件，暂不支持发送")
        if not 0 < int(item.get("size") or 0) <= max_bytes:
            raise ValueError("仅支持非空且不超过 %d MB 的文件" % (max_bytes // (1024 * 1024)))
        return _copy_to(state_dir, record["key"], alias, item["realpath"], max_bytes)
    except dt.TooLarge as exc:
        raise ValueError("仅支持非空且不超过 %d MB 的文件" % (max_bytes // (1024 * 1024))) from exc
    except dt.DeviceError as exc:
        raise ValueError(failure_text(alias, exc)) from exc


@dataclasses.dataclass(frozen=True)
class RemoteImage:
    """An image the Agent shows at once that is on its device (app/turn_images.py): where it really is, its size and time
    as the device told them."""
    alias: str
    real: str
    size: int
    mtime: float

    @property
    def name(self):
        return PurePosixPath(self.real).name

    def digest(self):
        """The image's identity: device, path, size and modification time (a file written again is another image)."""
        return hashlib.sha256(("%s:%s\0%s\0%r" % (self.alias, self.real, self.size, self.mtime)).encode()).hexdigest()[:16]

    def fetch(self, state_dir, key):
        """The hub's copy of the file, to send."""
        return _copy_to(state_dir, key, self.alias, self.real, self.size)


def wanted_images(ctx, checkpoint, policy, *, field, suffixes, most, max_bytes, note_chars):
    """(``RemoteImage``, note, urgent) of each image ``checkpoint``'s ``field`` lists that is on the device: the same rule
    as app/turn_images.py ``wanted`` (absolute paths of an image file with one of ``suffixes``, at most ``max_bytes``, at
    most ``most`` of them), the files looked at with one ``stat`` there. The listing's order is kept. A device that does
    not answer raises."""
    entries = checkpoint.get(field) if isinstance(checkpoint, dict) else None
    candidates = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        raw = entry["path"]
        if raw.startswith("/") and "\0" not in raw and PurePosixPath(raw).suffix.lower() in suffixes:
            note = " ".join(str(entry.get("note") or "").split())[:note_chars]
            candidates.append((raw, note, entry.get("urgent") is True))
    candidates = candidates[:most * 2]
    if not candidates:
        return []
    found = look(ctx.state_dir, ctx.device, [raw for raw, _, _ in candidates], policy, base=ctx.remote_workdir)
    kept = []
    for (raw, note, urgent), item in zip(candidates, found):
        real = str(item.get("realpath") or "")
        if (not item.get("exists") or not item.get("is_file") or item.get("denied") or not real.startswith("/")
                or policy.denied(PurePosixPath(real)) or not 0 < int(item.get("size") or 0) <= max_bytes):
            continue
        kept.append((RemoteImage(ctx.device, real, int(item["size"]), float(item.get("mtime") or 0)), note, urgent))
        if len(kept) >= most:
            break
    return kept

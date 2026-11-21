"""Per-job workspace diff capture for shared Harness bridge jobs."""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from covey.core.jobs import job_key, safe_slug
from covey.core.state import ensure_private_dir, write_private_text


MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_GIT_DIFF_CAPTURE_CHARS = 240_000
GIT_DIFF_TRUNCATED_MARKER = "[git diff 输出过长，已截断]"
SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(\.env(?:[./_-]|$)|.*(?:secret|token|credential|password).*)|"
    r"\.(?:pem|key|p12|pfx|crt|cer)$",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class DiffLimits:
    max_snapshot_file_bytes: int = MAX_SNAPSHOT_FILE_BYTES
    max_snapshot_total_bytes: int = MAX_SNAPSHOT_TOTAL_BYTES
    max_git_diff_capture_chars: int = MAX_GIT_DIFF_CAPTURE_CHARS


@dataclasses.dataclass
class BaselineFile:
    path: str
    content: str = ""
    missing: bool = False
    skipped_reason: str = ""


@dataclasses.dataclass
class JobDiffSnapshot:
    workdir: Path
    git_root: Path | None
    head_oid: str
    started_status: dict[str, str]
    baseline_files: dict[str, BaselineFile]
    skipped_reason: str = ""


@dataclasses.dataclass
class JobDiffFile:
    path: str
    status: str
    diff_text: str = ""
    additions: int = 0
    deletions: int = 0
    hidden_reason: str = ""
    truncated: bool = False
    omitted: bool = False


@dataclasses.dataclass
class JobDiffResult:
    files: list[JobDiffFile]
    artifact_path: str = ""
    json_path: str = ""
    skipped_reason: str = ""
    truncated_file_count: int = 0
    hidden_file_count: int = 0


def _run_git(workdir: Path, args: list[str], *, max_chars: int | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(workdir), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    stdout_truncated = False
    if max_chars is not None and len(proc.stdout) > max_chars:
        proc.stdout = proc.stdout[:max_chars]
        stdout_truncated = True
    setattr(proc, "stdout_truncated", stdout_truncated)
    return proc


def _git_root(workdir: Path) -> Path | None:
    proc = _run_git(workdir, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def _git_head(root: Path) -> str:
    proc = _run_git(root, ["rev-parse", "--verify", "HEAD"])
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _status_paths(workdir: Path) -> dict[str, str]:
    proc = _run_git(workdir, ["status", "--porcelain=v1"])
    if proc.returncode != 0:
        return {}
    paths: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths[path] = code
    return paths


COMMIT_CHECK_SECONDS = 5  # missing_commits' one git call; a slower answer reads as "cannot be told".
COMMIT_CHECK_MAX = 20  # Of the commits a turn names, the first ones asked about.
COMMIT_ID = re.compile(r"[0-9a-fA-F]{7,40}")


def missing_commits(workdir: Path | str, commits, *, timeout: float = COMMIT_CHECK_SECONDS) -> list[str] | None:
    """Which of ``commits`` (hex ids, as a turn's checkpoint ``delivery`` names them; other words are not asked
    about) the git repository ``workdir`` is in has no commit for: one ``git cat-file --batch-check`` of
    ``<id>^{commit}`` each, bounded by ``timeout``, no lock taken, no model asked. A short id more than one object
    shares is not counted as missing. None when it cannot be told: no such directory, not a repository, no git, too
    slow, or an answer that does not read."""
    ids = list(dict.fromkeys(str(commit) for commit in commits or () if COMMIT_ID.fullmatch(str(commit))))
    ids = ids[:COMMIT_CHECK_MAX]
    if not ids:
        return []
    try:
        if not Path(workdir).is_dir():
            return None
        # No lazy fetch: a partial clone would ask its remote for an object it lacks.
        proc = subprocess.run(["git", "-C", str(workdir), "cat-file", "--batch-check"],
                              input="".join(f"{commit}^{{commit}}\n" for commit in ids), text=True,
                              capture_output=True, check=False, timeout=timeout,
                              env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != len(ids):
        return None
    return [commit for commit, line in zip(ids, lines) if line.strip() == f"{commit}^{{commit}} missing"]


def _changed_paths_between(root: Path, before: str, after: str) -> dict[str, str]:
    if not before or not after or before == after:
        return {}
    proc = _run_git(root, ["diff", "--name-status", "--find-renames", before, after])
    if proc.returncode != 0:
        return {}
    paths: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0]
        path = parts[-1]
        if path:
            paths[path] = code[:1] or "M"
    return paths


def _is_sensitive_path(path: str) -> bool:
    return bool(SENSITIVE_PATH_RE.search(path))


def _is_binary_bytes(data: bytes) -> bool:
    return b"\0" in data[:8192]


def _read_text_for_diff(workdir: Path, rel_path: str, limits: DiffLimits) -> tuple[str, str, bool]:
    if _is_sensitive_path(rel_path):
        return "", "敏感文件默认隐藏", False
    candidate = workdir / rel_path
    if candidate.is_symlink():
        return "", "符号链接文件，未展开", False
    root = workdir.resolve()
    path = candidate.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "", "文件位于工作区外，未展开", False
    try:
        if not path.exists():
            return "", "", True
        if not path.is_file():
            return "", "非普通文件，未展开", False
        size = path.stat().st_size
        if size > limits.max_snapshot_file_bytes:
            return "", f"文件超过 {limits.max_snapshot_file_bytes} bytes，未展开", False
        data = path.read_bytes()
    except OSError as exc:
        return "", f"读取失败：{exc}", False
    if _is_binary_bytes(data):
        return "", "二进制文件，未展开", False
    return data.decode("utf-8", errors="replace"), "", False


def _unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _diff_stats(diff_text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _status_label(code: str, path: str) -> str:
    if code == "??":
        return "A"
    if "D" in code:
        return "D"
    if "R" in code:
        return "R"
    if "A" in code:
        return "A"
    if path:
        return "M"
    return "?"


def capture_job_diff_snapshot(workdir: Path, limits: DiffLimits | None = None) -> JobDiffSnapshot:
    limits = limits or DiffLimits()
    root = _git_root(workdir)
    if root is None:
        return JobDiffSnapshot(
            workdir=workdir,
            git_root=None,
            head_oid="",
            started_status={},
            baseline_files={},
            skipped_reason="非 git workspace",
        )

    head_oid = _git_head(root)
    status = _status_paths(root)
    baseline: dict[str, BaselineFile] = {}
    total_bytes = 0
    for rel_path in sorted(status):
        if total_bytes >= limits.max_snapshot_total_bytes:
            baseline[rel_path] = BaselineFile(rel_path, skipped_reason="baseline 快照总大小达到上限")
            continue
        text, reason, missing = _read_text_for_diff(root, rel_path, limits)
        if reason:
            baseline[rel_path] = BaselineFile(rel_path, skipped_reason=reason)
            continue
        total_bytes += len(text.encode("utf-8", errors="replace"))
        baseline[rel_path] = BaselineFile(rel_path, content=text, missing=missing)
    return JobDiffSnapshot(workdir=workdir, git_root=root, head_oid=head_oid, started_status=status, baseline_files=baseline)


def _git_diff_path(root: Path, rel_path: str, limits: DiffLimits, *, baseline_ref: str = "HEAD") -> tuple[str, str]:
    if _is_sensitive_path(rel_path):
        return "", "敏感文件默认隐藏"
    proc = _run_git(
        root,
        ["diff", "--no-color", "--no-ext-diff", baseline_ref, "--", rel_path],
        max_chars=limits.max_git_diff_capture_chars,
    )
    if proc.returncode != 0:
        return "", f"git diff 失败：{(proc.stderr or proc.stdout).strip()[:300]}"
    diff_text = proc.stdout
    if getattr(proc, "stdout_truncated", False):
        diff_text += f"\n{GIT_DIFF_TRUNCATED_MARKER}\n"
    return diff_text, ""


def _build_file_diff(
    root: Path,
    rel_path: str,
    code: str,
    snapshot: JobDiffSnapshot,
    limits: DiffLimits,
    *,
    start_code: str,
    end_code: str,
    committed_code: str = "",
) -> JobDiffFile | None:
    status = _status_label(code or committed_code, rel_path)
    baseline = snapshot.baseline_files.get(rel_path)
    if baseline is not None:
        if baseline.skipped_reason:
            if start_code == end_code:
                return None
            return JobDiffFile(rel_path, status, hidden_reason=f"{baseline.skipped_reason}；开始/结束状态发生变化，无法展示 diff")
        after, reason, missing = _read_text_for_diff(root, rel_path, limits)
        if reason:
            return JobDiffFile(rel_path, status, hidden_reason=reason)
        before = "" if baseline.missing else baseline.content
        current = "" if missing else after
        if before == current:
            return None
        diff_text = _unified_diff(rel_path, before, current)
    elif code == "??":
        after, reason, missing = _read_text_for_diff(root, rel_path, limits)
        if reason:
            return JobDiffFile(rel_path, "A", hidden_reason=reason)
        if missing:
            return None
        diff_text = _unified_diff(rel_path, "", after)
    else:
        baseline_ref = snapshot.head_oid if snapshot.head_oid else "HEAD"
        diff_text, reason = _git_diff_path(root, rel_path, limits, baseline_ref=baseline_ref)
        if reason:
            return JobDiffFile(rel_path, status, hidden_reason=reason)
        if not diff_text.strip():
            return None
    additions, deletions = _diff_stats(diff_text)
    truncated = GIT_DIFF_TRUNCATED_MARKER in diff_text
    return JobDiffFile(rel_path, status, diff_text=diff_text, additions=additions, deletions=deletions, truncated=truncated)


def _build_diff_result(files: list[JobDiffFile], limits: DiffLimits) -> JobDiffResult:
    del limits
    return JobDiffResult(
        files=files,
        hidden_file_count=sum(1 for item in files if item.hidden_reason),
        truncated_file_count=sum(1 for item in files if item.truncated),
    )


def _diff_markdown(job: Any, result: JobDiffResult, limits: DiffLimits) -> str:
    message_id = str(getattr(job, "message_id", "") or "")
    job_id = getattr(job, "job_id", "")
    lines = [
        f"# Job Diff #{job_id}",
        "",
        f"- message_id：`{message_id or '-'}`",
        f"- 修改文件：{len(result.files)}",
        f"- 隐藏文件：{result.hidden_file_count}",
        f"- git diff 捕获字符上限：{limits.max_git_diff_capture_chars}",
        "",
    ]
    for item in result.files:
        lines.extend([f"## {item.status} {item.path}", ""])
        if item.hidden_reason:
            lines.extend([f"[{item.hidden_reason}]", ""])
        elif item.diff_text:
            lines.extend(["```diff", item.diff_text.rstrip(), "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _write_diff_artifacts(state_dir: Path, job: Any, result: JobDiffResult, limits: DiffLimits) -> str:
    message_id = str(getattr(job, "message_id", "") or "")
    job_id = getattr(job, "job_id", "")
    key = job_key(message_id, job_id)
    base_dir = state_dir / "job-diffs" / safe_slug(key)
    ensure_private_dir(base_dir)
    markdown_path = base_dir / "diff.md"
    json_path = base_dir / "diff.json"
    markdown = _diff_markdown(job, result, limits)
    write_private_text(markdown_path, markdown)
    # Each diff is stored once, in diff.md (the file uploaded with the task's artifacts); this
    # index keeps the per-file stats. Maintenance slims older indexes the same way.
    payload = {
        "files": [{k: v for k, v in dataclasses.asdict(item).items() if k != "diff_text"}
                  for item in result.files],
        "limits": dataclasses.asdict(limits),
        "summary": {
            "file_count": len(result.files),
            "hidden_file_count": result.hidden_file_count,
        },
    }
    write_private_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    result.artifact_path = str(markdown_path)
    result.json_path = str(json_path)
    return markdown


def finalize_job_diff(
    snapshot: JobDiffSnapshot | None,
    state_dir: Path,
    job: Any,
    limits: DiffLimits | None = None,
) -> JobDiffResult:
    limits = limits or DiffLimits()
    if snapshot is None:
        return JobDiffResult(files=[], skipped_reason="diff snapshot missing")
    if snapshot.git_root is None:
        return JobDiffResult(files=[], skipped_reason=snapshot.skipped_reason or "非 git workspace")

    root = snapshot.git_root
    end_head = _git_head(root)
    end_status = _status_paths(root)
    committed_status = _changed_paths_between(root, snapshot.head_oid, end_head)
    all_paths = sorted(set(snapshot.started_status) | set(end_status) | set(committed_status))
    files: list[JobDiffFile] = []
    for rel_path in all_paths:
        start_code = snapshot.started_status.get(rel_path) or ""
        end_code = end_status.get(rel_path) or ""
        committed_code = committed_status.get(rel_path) or ""
        code = end_code or start_code or committed_code
        item = _build_file_diff(
            root,
            rel_path,
            code,
            snapshot,
            limits,
            start_code=start_code,
            end_code=end_code,
            committed_code=committed_code,
        )
        if item is not None:
            files.append(item)
    result = _build_diff_result(files, limits)
    if result.files:
        _write_diff_artifacts(state_dir, job, result, limits)
    return result


def _file_label(item: JobDiffFile) -> str:
    suffix = f"(+{item.additions} -{item.deletions})" if item.diff_text else ""
    flags = []
    if item.truncated:
        flags.append("已截断")
    if item.hidden_reason:
        flags.append("已隐藏")
    flag_text = f" [{' / '.join(flags)}]" if flags else ""
    return " ".join(part for part in (f"{item.status} {item.path}", suffix) if part) + flag_text


def render_diff_summary_note(result: JobDiffResult | None) -> str:
    if result is None or not result.files:
        return ""
    lines = [
        "**本次修改 Diff**",
        f"- 修改文件：{len(result.files)} 个",
    ]
    if result.hidden_file_count:
        lines.append(f"- 隐藏文件：{result.hidden_file_count} 个")
    for item in result.files[:10]:
        lines.append(f"- {_file_label(item)}")
    omitted = max(0, len(result.files) - 10)
    if omitted:
        lines.append(f"- 另有 {omitted} 个文件，见完整 Diff")
    return "\n".join(lines)


def append_diff_summary_note(text: str, result: JobDiffResult | None) -> str:
    note = render_diff_summary_note(result)
    if not note:
        return text
    return f"{str(text or '').rstrip()}\n\n{note}"

"""Which files a task records as its artifacts, and the key a button names one by.

Read from the task's checkpoint and job record only; what may be published of them is app/artifact_policy.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from covey.core.checkpoint import read_checkpoint


def artifact_paths(record):
    cp = read_checkpoint(Path(record["checkpoint_path"])) if record.get("checkpoint_path") else {}
    raw = cp.get("artifacts", [])
    paths = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    if record.get("result_file_path"):
        paths.append(record["result_file_path"])
    if record.get("diff_artifact_path"):
        paths.append(record["diff_artifact_path"])
    return list(dict.fromkeys(paths))


def artifact_key(path):
    return hashlib.sha256(path.encode()).hexdigest()[:24]

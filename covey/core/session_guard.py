"""Block only the affected native identity; never clear a newer topic pointer."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading

from covey.core.state import read_json, write_json
from covey.core.file_lock import file_lock

_LOCK = threading.RLock()


def _path(ctx, backend, sid):
    key = hashlib.sha256(json.dumps([backend, sid]).encode()).hexdigest()
    folder = Path(ctx.state_dir) / "native-session-guards"
    if folder.is_symlink():
        raise ValueError("原生会话保护目录无效")
    path = folder / (key + ".json")
    if path.is_symlink():
        raise ValueError("原生会话保护记录无效")
    return path


@contextmanager
def _locked_guard(path):
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(path.parent / ".lock"):
            yield


def session_is_blocked(ctx, backend, sid):
    # An active owner that vanished without finalization stays blocked.
    return bool(sid) and _path(ctx, backend, sid).exists()


def claim_native_session(ctx, backend, sid, run_id):
    if not sid:
        return
    path = _path(ctx, backend, sid)
    with _locked_guard(path):
        if path.exists():
            saved = read_json(path)
            if (type(saved.get("version")) is not int or saved["version"] != 1
                    or saved.get("run_id") != run_id or saved.get("backend") != backend
                    or saved.get("session_id") != sid):
                raise ValueError("原生会话已被保护，不能自动续接；请新建会话")
            return
        write_json(path, {"version": 1, "backend": backend, "session_id": sid,
            "run_id": run_id, "workdir": str(Path(ctx.workdir).resolve()), "state": "active"})


def finish_native_session(ctx, backend, sid, run_id, allow_resume):
    if not sid:
        return True
    path = _path(ctx, backend, sid)
    with _locked_guard(path):
        if not path.exists():
            return False
        saved = read_json(path)
        if (type(saved.get("version")) is not int or saved["version"] != 1
                or saved.get("run_id") != run_id or saved.get("backend") != backend or saved.get("session_id") != sid):
            return False
        if allow_resume and saved.get('policy_evidence'):
            return False
        if allow_resume:
            path.unlink()
        else:
            saved["state"] = "blocked"
            write_json(path, saved)
        return True


def block_policy_session(ctx, backend, sid, evidence_id):
    """A confirmed external refusal revokes this native identity, never a topic pointer."""
    if not sid:
        return
    path = _path(ctx, backend, sid)
    with _locked_guard(path):
        saved = read_json(path) if path.exists() else {
            'version': 1, 'backend': backend, 'session_id': sid, 'run_id': 'policy:' + evidence_id,
            'workdir': str(Path(ctx.workdir).resolve())}
        if saved.get('backend') != backend or saved.get('session_id') != sid:
            raise ValueError('policy session identity mismatch')
        saved.update(state='blocked', policy_evidence=evidence_id)
        write_json(path, saved)


def transfer_question_session(ctx, backend, sid, source_run_id, answer_run_id, token):
    """Transfer an already-blocked identity to one answer run, without unlocking it."""
    path = _path(ctx, backend, sid)
    with _locked_guard(path):
        saved = read_json(path)
        if (type(saved.get("version")) is not int or saved["version"] != 1
                or saved.get("backend") != backend or saved.get("session_id") != sid
                or saved.get("run_id") != source_run_id or saved.get("state") != "blocked"
                or saved.get('policy_evidence')
                or saved.get("workdir") != str(Path(ctx.workdir).resolve())):
            raise ValueError("原生问答会话不能转交给本次回答")
        saved.update(run_id=answer_run_id, state="active", question_token=token)
        write_json(path, saved)

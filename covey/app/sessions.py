"""Sessions: one Harness's native-session pointer of a topic.

A topic remembers the native session its Harness last ran in (``ctx.session_path``).
A job captures the pointer before it runs, records the session the backend reported
afterwards (compare-and-set, so a concurrent /new wins), and claims every native
session it saw until the result is settled; a blocked session is never resumed.

A native session keeps the system prompt (Claude) or developer instructions (Codex)
of its first invocation; later ones are not applied. Sessions that started with the
bridge rules there are marked per (Harness, session ID) in ``system-rules-sessions/``
of the state directory, which releases before the mark never touch; an unmarked,
unreadable or lost mark means the rules must travel in the message.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from covey.core.harness import BridgeContext
from covey.core.session_guard import claim_native_session, finish_native_session, session_is_blocked
from covey.core.state import read_json, utc_now_iso, write_json
from covey.core.logs import append_private_text
from covey.core.session_state import capture_session_state, compare_and_set_session
from covey.core.session_state import read_session_id as state_read_session_id

SYSTEM_RULES_DIR = "system-rules-sessions"


def _rules_mark(ctx, backend: str, sid: str) -> Path:
    folder = Path(ctx.state_dir) / SYSTEM_RULES_DIR
    if folder.is_symlink():
        raise ValueError("会话规则标记目录无效")
    return folder / (hashlib.sha256(json.dumps([backend, sid]).encode()).hexdigest() + ".json")


def started_with_system_rules(ctx, backend: str, sid: str | None) -> bool:
    """The session started with the bridge rules at system level; anything unreadable is False."""
    if not sid:
        return False
    try:
        saved = read_json(_rules_mark(ctx, backend, sid))
        return isinstance(saved, dict) and saved.get("backend") == backend and saved.get("session_id") == sid
    except (OSError, ValueError):
        return False


def mark_system_rules(ctx, backend: str, sid: str) -> None:
    path = _rules_mark(ctx, backend, sid)
    if not path.is_file():
        write_json(path, {"version": 1, "backend": backend, "session_id": sid, "marked_at": utc_now_iso()})


class Sessions:
    """- ``harness``: whose session pattern and name the pointers use.
    - ``runtime``: the process-wide session lock and the log."""

    def __init__(self, harness, *, runtime) -> None:
        self.harness = harness
        self.runtime = runtime

    def read_session_id(self, path: Path) -> str | None:
        return state_read_session_id(path, self.harness.spec.session_pattern)

    def capture_session(self, ctx: BridgeContext) -> tuple[int, str | None]:
        with self.runtime.session_lock:
            for _ in range(3):
                revision, sid = capture_session_state(ctx.session_path, self.harness.spec.session_pattern)
                if not sid or not session_is_blocked(ctx, self.harness.spec.name, sid):
                    return revision, sid
                # Clear only this unchanged pointer, not a concurrent /new choice.
                compare_and_set_session(ctx.session_path, self.harness.spec.session_pattern, revision, sid, None)
            raise ValueError("会话状态正在变化，请稍后重试")

    def append_session_index(self, ctx: BridgeContext, session_id: str) -> None:
        if not ctx.workspace_name or not session_id:
            return
        record = {
            "time": utc_now_iso(),
            "backend": self.harness.spec.name,
            "session_id": session_id,
            "workspace_id": ctx.workspace_name,
            "topic_id": ctx.topic_id,
            "root_id": ctx.reply_to_message_id,
            "workdir": str(ctx.workdir),
        }
        append_private_text(ctx.state_dir / "session_index.jsonl", json.dumps(record, ensure_ascii=False) + "\n")

    def record_session_from_worker(
        self,
        ctx: BridgeContext,
        old_session_id: str | None,
        new_session_id: str | None,
        revision: int,
    ) -> None:
        if not new_session_id:
            return
        with self.runtime.session_lock:
            if not compare_and_set_session(ctx.session_path, self.harness.spec.session_pattern,
                                           revision, old_session_id, new_session_id):
                return
            self.runtime.log(f"recorded {self.harness.spec.name} session {new_session_id}")
        self.append_session_index(ctx, new_session_id)

    def finalize_session_from_worker(self, ctx, job, old_sid, new_sid, revision, *, allow_resume, clear_pointer=True):
        claims = job.native_session_claims
        if new_sid and new_sid not in claims:
            if not self.harness.spec.session_pattern.fullmatch(new_sid):
                return False
            claim_native_session(ctx, self.harness.spec.name, new_sid, job.bridge_run_id)
            claims.add(new_sid)
            job.native_session_claims = claims
        # One verified root cannot authorize releasing several native identities.
        # Keep every claim blocked rather than partially unlinking a group.
        multiple_roots = allow_resume and len(claims) > 1
        protected = not multiple_roots
        for sid in sorted(claims):
            try:
                confirmed = finish_native_session(ctx, self.harness.spec.name, sid, job.bridge_run_id,
                                                  allow_resume and not multiple_roots)
            except Exception:
                confirmed = False
            protected = protected and confirmed
        if (not allow_resume or not protected) and clear_pointer and claims:
            # The native identity remains blocked even if /new changed this pointer.
            compare_and_set_session(ctx.session_path, self.harness.spec.session_pattern, revision, old_sid, None)
        return protected

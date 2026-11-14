"""Stubbed Harness components for driving ``process_bridge_reply_job`` directly."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from support import make_context
from covey.core.harness import HarnessCapabilities, RunResult
from covey.app.worker import process_bridge_reply_job
from covey.core.jobs import BridgeReplyJob


class FinalDeliveryHarness:
    """The components the worker uses, stubbed (``app``); records every outgoing message key."""

    def __init__(self, root: Path, *, card_update_ok: bool, overflow: bool, close_delay: float = 0.0,
                 cut: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.answer_cards: dict[str, dict] = {}  # By key: the card a message went with (Messenger.reply's card).
        self.final_review_cards: list[tuple[str, bool]] = []
        self.card_events: list[tuple[str, str]] = []
        self.card_updates: list[str] = []
        self.allow_replacements: list[tuple[str, bool]] = []
        self.close_delay = close_delay
        self.close_finished = threading.Event()
        self.ctx = make_context(root, profile=None, state_dir=root / ".state")
        self.app = SimpleNamespace(
            # Every backend reports structured events: the worker gives each turn a trace.
            harness=SimpleNamespace(spec=SimpleNamespace(title="Harness", idempotency_prefix="unit"), voice=None,
                                    backend=SimpleNamespace(
                                        capabilities=lambda _ctx: HarnessCapabilities())),
            runtime=SimpleNamespace(log=lambda _message: None),
            messenger=SimpleNamespace(reply=lambda ctx, chat_id, text, key, voice, card=None, on_card=None: self._send_message(ctx, chat_id, text, key, card)),
            runs=SimpleNamespace(
                generate_reply_for_worker=lambda _ctx, _job, _session_id, _progress: RunResult("最终回复", None)),
            record_job_state=lambda *_args, **_kwargs: None,
            steering=SimpleNamespace(close_task_controls=lambda *_args: False),
            sessions=SimpleNamespace(
                capture_session=lambda _ctx: (0, None),
                record_session_from_worker=lambda *_args: None,
                finalize_session_from_worker=lambda *_args, **_kwargs: True,
            ),
            artifacts=SimpleNamespace(send_result_file=lambda *_args: False),
            questions=SimpleNamespace(deliver_checkpoint_questions=lambda *_args: True),
            cards=SimpleNamespace(
                render_stream_status=lambda _job, status, _events: f"status:{status}",
                render_stream_final=lambda _job, final_text, **kwargs: f"final:{final_text}",
                exceeds_stream_card_limit=lambda _text: overflow,
                body_cut=lambda _job: cut,  # The card cut the body to its size (not by characters).
                update_job_streaming_reply=self._update_job_streaming_reply,
                close_streaming_reply=self._close_streaming_reply,
                finalize_trace=self._finalize_trace,
                finish_result_controls=lambda *_args: None,
                update_card_activity=lambda *_args: False,
                show_outcome=lambda _ctx, _job, content, **_kwargs: self.card_events.append(("outcome", content)),
            ),
        )
        self.card_update_ok = card_update_ok

    def run(self, job) -> None:
        """``process_bridge_reply_job`` over these stubbed components."""
        process_bridge_reply_job(job, self.ctx, **vars(self.app))

    def _send_message(self, _ctx, _chat_id: str, text: str, key: str, card=None) -> bool:
        self.sent.append((key, text))
        if card is not None:
            self.answer_cards[key] = card
        return True

    def _update_job_streaming_reply(
        self, _ctx, _job, content: str, *, force: bool, idempotency_prefix: str, allow_replacement: bool = True
    ) -> bool:
        prefix = idempotency_prefix
        self.card_events.append(("update", prefix))
        self.card_updates.append(content)
        self.allow_replacements.append((prefix, allow_replacement))
        return self.card_update_ok

    def _close_streaming_reply(self, _ctx, _progress) -> None:
        if self.close_delay:
            time.sleep(self.close_delay)
        self.card_events.append(("close", ""))
        self.close_finished.set()

    def _create_final_review_card(self, _ctx, _job, card_text: str, _final_text: str, overflow: bool) -> bool:
        self.final_review_cards.append((card_text, overflow))
        return False

    def _finalize_trace(self, _ctx, _job, stopped=False) -> bool:
        self.card_events.append(("trace", "stopped" if stopped else ""))
        return True

    def keys(self) -> list[str]:
        return [key for key, _text in self.sent]


def make_reply_job(kind: str, *, root: Path, with_progress: bool) -> BridgeReplyJob:
    job = BridgeReplyJob(
        job_id=1,
        user_text="question",
        chat_id="oc_chat",
        message_id="om_msg",
        timeout=0,
        kind=kind,
        history_path=root / "history.jsonl",
    )
    if with_progress:
        job.progress = SimpleNamespace(disabled=False)
    return job

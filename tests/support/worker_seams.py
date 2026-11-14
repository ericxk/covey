"""What app/worker.py does around a turn besides running it and delivering the reply, left out
for tests that drive whole turns: one place instead of the same dotted patches in each driver."""
from __future__ import annotations

from unittest import mock

from covey.app import worker

# Taken before any test replaces them (a driver may install twice in one test): the fakes copy their signatures.
SNAPSHOT, IMAGES, CLOSE = worker.capture_job_diff_snapshot, worker.prepare_outgoing_markdown_images, \
    worker.close_streaming_reply_async
START, STOP = worker.TaskMonitor.start, worker.TaskMonitor.stop


def leave_out_side_work(stack, *, diff=True, images=True, closing=True, monitor=True):
    """Replace, for the rest of ``stack``, the worker's side work: the workspace diff snapshot (Git),
    uploading a reply's Markdown images to Feishu (the text goes as written), closing the streaming
    card on a background thread, and the TaskMonitor thread. Each is replaced where the worker looks
    it up, with its signature, so a call with other arguments still fails."""
    fakes = {"capture_job_diff_snapshot": diff and mock.create_autospec(SNAPSHOT, return_value=None),
             "prepare_outgoing_markdown_images": images and mock.create_autospec(IMAGES, side_effect=lambda text, **_: text),
             "close_streaming_reply_async": closing and mock.create_autospec(CLOSE)}
    for name, fake in fakes.items():
        if fake:
            stack.enter_context(mock.patch.object(worker, name, fake))
    if monitor:
        stack.enter_context(mock.patch.object(worker.TaskMonitor, "start", mock.create_autospec(START)))
        stack.enter_context(mock.patch.object(worker.TaskMonitor, "stop", mock.create_autospec(STOP)))

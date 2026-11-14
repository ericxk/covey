"""The work a task runner finishes before it exits (app/runner.py BACKGROUND waits for these threads by
name): a thread under another name dies with the runner, which left a card streaming ([生成中...] in the
chat list) or an upload cut off."""
import threading

UPLOAD = "artifact-upload"  # A task's artifacts going up to Drive (app/artifacts.py); waited for longest.
CARD_UPDATES = "card-updates"  # Card writes off the event thread: CoalescedUpdates, receipts, repaints.
CARD_CLOSE = "card-close"  # The streaming reply's close (app/worker.py close_streaming_reply_async).
WARMUP = "sdk-warmup"  # The SDK client built and warmed (feishu/sdk.py warm_sdk); a task runner waits for it too.
RECORDS = "records-warmup"  # The job and question stores caught up (app/runner.py warm_records); not waited for.


def start(kind, target, *args):
    """``target(*args)`` on a daemon thread named ``kind``, started; the thread. Raises what
    Thread.start raises (RuntimeError when no thread can be had)."""
    thread = threading.Thread(target=target, args=args, daemon=True, name=kind)
    thread.start()
    return thread

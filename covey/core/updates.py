"""Per-task coalesced UI updates, isolated from backend event handling."""
import threading

from covey.core import background


class CoalescedUpdates:
    def __init__(self, log, interval=0.5):
        self.log, self.interval = log, interval
        self.condition = threading.Condition()
        self.pending = {}
        self.busy = False
        self.closed = False
        self.urgent = False
        self.thread = background.start(background.CARD_UPDATES, self.run)

    def submit(self, key, callback, urgent=False):
        with self.condition:
            if self.closed:
                return False
            self.pending[key] = callback
            self.urgent |= urgent
            self.condition.notify_all()
            return True

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed)
                if self.closed and not self.pending:
                    return
                if not self.urgent and not self.closed:
                    self.condition.wait_for(lambda: self.urgent or self.closed, timeout=self.interval)
                batch, self.pending = self.pending, {}
                self.urgent = False
                self.busy = True
            for callback in batch.values():
                try:
                    callback()
                except Exception as exc:  # noqa: BLE001 - UI errors never fail a task
                    self.log(f'Card update failed: {exc}')
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    def close(self, timeout=15):
        with self.condition:
            self.closed = True
            self.urgent = True
            self.condition.notify_all()
        self.thread.join(timeout)
        return not self.thread.is_alive()

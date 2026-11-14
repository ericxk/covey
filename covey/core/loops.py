"""The Bridge's background threads: ``supervised`` keeps one running, ``every`` is the loop most of them run.
``runtime`` is anything with a ``shutdown`` event and a ``log(line)``, as app/runtime.py's Runtime. Standard library
only, and here rather than in app/runtime.py: core/maintenance.py runs one, and app/runtime.py imports modules
that start them."""
from __future__ import annotations

import threading


def supervised(runtime, name, target, *args):
    """Start the daemon thread ``name`` that calls ``target(*args)`` until ``runtime.shutdown``, again 5 s after each
    return or failure (logged as "worker <name> failed: <type>; restarting"). The thread."""
    def run():
        while not runtime.shutdown.is_set():
            try:
                target(*args)
            except Exception as exc:  # noqa: BLE001
                runtime.log(f'worker {name} failed: {type(exc).__name__}; restarting')
            if runtime.shutdown.wait(5):
                break
    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def every(runtime, seconds, *steps, args=(), isolate=""):
    """Until ``runtime.shutdown``: call each of ``steps`` with ``args``, then wait ``seconds``. A step that raises
    ends the loop, unless ``isolate`` names it: then the failure is logged as "<isolate> step <module>.<name>
    failed: <type>" and the next step runs."""
    while not runtime.shutdown.is_set():
        for step in steps:
            try:
                step(*args)
            except Exception as exc:
                if not isolate:
                    raise
                runtime.log(f"{isolate} step {step.__module__}.{step.__name__} failed: {type(exc).__name__}")
        runtime.shutdown.wait(seconds)

#!/usr/bin/env python3
"""A scripted stand-in for a Harness process, run as a real child by the device shim's tests.

It speaks lines on stdin and stdout like a stream-json Agent: each stdin line is echoed back as
``{"echo": <line>}``; ``{"exit": N}`` ends it with code N; ``{"cp": "text"}`` writes the checkpoint file given by
``--checkpoint``. ``--count N --delay S`` also prints ``{"n": i}`` lines; ``--stay`` keeps it running after the
count. ``--ignore-term`` ignores SIGTERM, ``--binary`` prints a line that is not UTF-8, ``--big N`` one line of N
bytes, ``--grandchild`` starts a sleeping child in the same group (its pid goes to ``<pidfile>.child``), and
``--stderr N`` writes N bytes of noise to stderr.
"""
import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--stay", action="store_true")
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--binary", action="store_true")
    parser.add_argument("--big", type=int, default=0)
    parser.add_argument("--grandchild", action="store_true")
    parser.add_argument("--stderr", type=int, default=0)
    parser.add_argument("--checkpoint", default="")
    parser.add_argument("--pidfile", default="")
    args = parser.parse_args()
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    out = sys.stdout.buffer

    def emit(obj):
        out.write(json.dumps(obj).encode() + b"\n")
        out.flush()

    if args.pidfile:
        with open(args.pidfile, "w") as handle:
            handle.write(str(os.getpid()))
    if args.grandchild:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        with open(args.pidfile + ".child", "w") as handle:
            handle.write(str(child.pid))
    if args.binary:
        out.write(b"\xff\xfe\x80 not utf-8\n")
        out.flush()
    if args.big:
        out.write(b"x" * args.big + b"\n")
        out.flush()
    if args.stderr:
        sys.stderr.write("e" * args.stderr)
        sys.stderr.flush()
    emit({"ready": os.getpid(), "cwd": os.getcwd(), "task_dir": os.environ.get("COVEY_TASK_DIR", ""),
          "var": os.environ.get("FAKE_VAR"), "home": os.environ.get("HOME")})
    number, next_at, reading = 0, time.time(), True
    while True:
        counting = args.count and number < args.count
        if args.count and not counting and not args.stay:
            return 0  # The count is done: end like an Agent that finished its turn.
        timeout = max(0.0, next_at - time.time()) if counting else 0.2
        if reading and select.select([0], [], [], timeout)[0]:
            line = sys.stdin.buffer.readline()
            if not line:
                reading = False
                if not args.count and not args.stay:
                    return 0
                continue
            text = line.decode("utf-8", "replace").rstrip("\n")
            try:
                command = json.loads(text)
            except ValueError:
                command = None
            if isinstance(command, dict) and "exit" in command:
                return int(command["exit"])
            if isinstance(command, dict) and "cp" in command and args.checkpoint:
                with open(args.checkpoint, "w") as handle:
                    handle.write(command["cp"])
                emit({"cp_written": len(command["cp"])})
                continue
            emit({"echo": text})
            continue
        if not reading:
            time.sleep(timeout)
        if counting and time.time() >= next_at:
            emit({"n": number})
            number += 1
            next_at = time.time() + args.delay


if __name__ == "__main__":
    sys.exit(main())

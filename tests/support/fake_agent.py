#!/usr/bin/env python3
"""A scripted stand-in for a stream-json Agent, run as a real child by the device process tests.

Every stdin line is JSON. ``{"say": <object>}`` prints the object as one line of its own (a ``control_request``, a
JSON-RPC request, anything); ``{"exit": N}`` ends with code N; any other line is echoed back as ``{"echo": <line>}``.
The first line printed is ``{"ready": <pid>}``. ``--count N`` also prints ``{"n": i}`` every ``--delay`` seconds.
"""
import argparse
import json
import os
import select
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    out = sys.stdout.buffer

    def emit(value):
        out.write(json.dumps(value).encode() + b"\n")
        out.flush()

    emit({"ready": os.getpid()})
    number, next_at, reading = 0, time.time(), True
    while True:
        if args.count and number >= args.count:
            return 0  # The count is done: end like an Agent that finished its turn.
        if not reading and not args.count:
            return 0
        wait = max(0.0, next_at - time.time()) if args.count else 0.2
        if reading and select.select([0], [], [], wait)[0]:
            line = sys.stdin.buffer.readline()
            if not line:
                reading = False
                continue
            text = line.decode("utf-8", "replace").rstrip("\n")
            try:
                command = json.loads(text)
            except ValueError:
                command = None
            if isinstance(command, dict) and "exit" in command:
                return int(command["exit"])
            if isinstance(command, dict) and "say" in command:
                emit(command["say"])
            else:
                emit({"echo": text})
        elif not reading:
            time.sleep(wait)
        if number < args.count and time.time() >= next_at:
            emit({"n": number})
            number += 1
            next_at = time.time() + args.delay


if __name__ == "__main__":
    sys.exit(main())

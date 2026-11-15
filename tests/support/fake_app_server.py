#!/usr/bin/env python3
"""A scripted stand-in for ``codex app-server --stdio``: JSON-RPC lines on stdin and stdout, run as a real child by the
device process tests.

``initialize`` and ``thread/start`` are answered; ``turn/start`` is answered, then the server asks the client one
question (a server request ``item/tool/requestUserInput``, id 900) and, once it has the answer, completes the turn with
an agent message that quotes it (with ``--plain`` among its arguments it completes the turn at once, no question). ``turn/steer`` is answered with the current turn. It exits when its stdin ends.
"""
import json
import os
import sys


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def main():
    thread, turn = "thread-1", "turn-1"
    if "--plain" in sys.argv:  # Identities a real server has (core checks them: UUIDs).
        thread, turn = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    for line in sys.stdin:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        method, rid = row.get("method"), row.get("id")
        if method == "initialize":
            emit({"id": rid, "result": {"cwd": os.getcwd(), "pid": os.getpid()}})
        elif method == "thread/start" or method == "thread/resume":
            emit({"id": rid, "result": {"thread": {"id": thread}, "params": row.get("params")}})
        elif method == "turn/start":
            emit({"id": rid, "result": {"turn": {"id": turn}}})
            emit({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn}}})
            if "--plain" in sys.argv:  # No question: the turn ends with its message.
                emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                    "id": turn, "status": "completed", "error": None, "itemsView": "full", "items": [
                        {"type": "agentMessage", "id": "a1", "text": "plain answer", "phase": "final_answer"}]}}})
                continue
            emit({"id": 900, "method": "item/tool/requestUserInput",
                  "params": {"threadId": thread, "turnId": turn, "questions": [{"id": "q1", "question": "Which?"}]}})
        elif method == "turn/steer":
            emit({"id": rid, "result": {"turnId": turn}})
        elif method is None and rid == 900:
            answers = row.get("result", {}).get("answers")
            emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": turn, "status": "completed", "items": [
                    {"type": "agentMessage", "id": "a1", "text": "answered %s" % json.dumps(answers), "phase": "final_answer"}]}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Synthetic CLI wire fixture. No model, SDK, network, or Feishu calls."""

import datetime
import json
import os
import re
import subprocess
import sys
import time

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
# "<scenario>:<seconds>" sets a scenario's pause (background-chain: between its replies).
scenario, _, pause = sys.argv[1].partition(":")
turn = 0


def emit(value):
    print(json.dumps(value), flush=True)


def assistant(text, key, **extra):
    emit(
        {
            "type": "assistant",
            "session_id": SID,
            "uuid": key,
            "message": {"id": key, "content": [{"type": "text", "text": text}]},
            **extra,
        }
    )


def finish(text="Complete answer", **extra):
    assistant(text, "answer-" + str(turn))
    emit(
        {
            "type": "result",
            "session_id": SID,
            "uuid": "result-" + str(turn),
            "subtype": "success",
            "is_error": False,
            "result": text,
            "terminal_reason": "completed",
            **extra,
        }
    )
    emit({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"})


def task_event(subtype, task, **extra):
    emit({"type": "system", "subtype": subtype, "session_id": SID, "task_id": task, "tool_use_id": task + "-call", **extra})


question_input = {
    "questions": [
        {
            "question": "Select inputs",
            "header": "Inputs",
            "multiSelect": True,
            "options": [{"label": "A", "description": "first"}, {"label": "B", "description": "second"}],
        }
    ]
}

for line in sys.stdin:
    row = json.loads(line)
    if row["type"] == "control_request":
        emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": row["request_id"],
                    "response": {
                        "claude_code_version": "2.1.300",
                        "models": [
                            {
                                "value": "fixture-model",
                                "displayName": "Fixture",
                                "supportsEffort": True,
                                "supportedEffortLevels": ["high", "max"],
                            }
                        ],
                    },
                },
            }
        )
    elif (row["type"] == "user" and len(row["message"]["content"]) == 1
          and str(row["message"]["content"][0].get("text", "")).split(" ", 1)[0] == "/compact"):
        # Claude Code 2.1.310 compacting a resumed session in a process of its own (backends/claude/compaction.py),
        # ``/compact`` or ``/compact <instructions>``.
        with open("compact-argv.json", "w") as handle:
            json.dump(sys.argv, handle)
        with open("compact-command.txt", "w") as handle:
            handle.write(row["message"]["content"][0]["text"])
        emit({"type": "system", "subtype": "init", "session_id": SID, "model": "fixture-model"})
        emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID})
        if scenario == "compact-error":
            emit({"type": "system", "subtype": "status", "status": None, "compact_result": "failed", "session_id": SID})
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": SID,
                  "uuid": "compact-result", "result": "", "num_turns": 0, "local_command": "compact"})
        else:
            emit({"type": "system", "subtype": "status", "status": None, "compact_result": "success", "session_id": SID})
            emit({"type": "system", "subtype": "compact_boundary", "session_id": SID,
                  "compact_metadata": {"trigger": "manual", "pre_tokens": 412000, "post_tokens": 9800}})
            emit({**row, "session_id": SID, "isReplay": True})
            emit({"type": "result", "subtype": "success", "is_error": False, "session_id": SID, "uuid": "compact-result",
                  "result": "", "num_turns": 0, "local_command": "compact", "usage": {"input_tokens": 0, "output_tokens": 0},
                  "model_usage_since_last_result": {"fixture-model": {
                      "inputTokens": 2, "outputTokens": 1900, "cacheReadInputTokens": 410000,
                      "cacheCreationInputTokens": 0, "contextWindow": 1000000}}})
        emit({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "idle"})
    elif (row["type"] == "user" and isinstance(row["message"]["content"], list) and row["message"]["content"]
          and str(row["message"]["content"][0].get("text", "")).startswith("[Bridge 交接请求]")):
        # The handoff summary on a fork of the old session (backends/claude/handoff.py): a new session ID.
        with open("handoff-argv.json", "w") as handle:
            json.dump(sys.argv, handle)
        with open("handoff-prompt.txt", "w") as handle:
            handle.write(row["message"]["content"][0]["text"])
        emit({"type": "system", "subtype": "init", "session_id": OTHER, "model": "fixture-model"})
        if scenario == "handoff-hang":
            time.sleep(30)
        elif scenario == "handoff-tool":
            emit({"type": "control_request", "request_id": "perm-1", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -rf x"}}})
            answer = json.loads(sys.stdin.readline())["response"]["response"]
            with open("handoff-permission.json", "w") as handle:
                json.dump(answer, handle)
        if scenario == "handoff-error":
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": OTHER,
                  "result": ""})
        else:
            emit({"type": "assistant", "session_id": OTHER, "uuid": "summary",
                  "message": {"id": "summary", "content": [{"type": "text", "text": "目标：修好 X。下一步：跑测试。"}]}})
            emit({"type": "result", "subtype": "success", "is_error": False, "session_id": OTHER,
                  "result": "目标：修好 X。下一步：跑测试。"})
    elif row["type"] == "user":
        turn += 1
        if turn == 1:
            emit({"type": "system", "subtype": "init", "session_id": SID})
        if scenario != "missing-ack":
            emit({**row, "session_id": SID, "isReplay": True})
        emit({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"})
        if scenario.startswith("policy-"):
            if "survivor" in scenario:
                subprocess.Popen([sys.executable, "-c", "import os,signal,time,pathlib; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "pathlib.Path('policy-child.pid').write_text(str(os.getpid())); time.sleep(30)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(200):
                    if os.path.exists('policy-child.pid'):
                        break
                    time.sleep(0.01)
            child = {"parent_tool_use_id": "child-policy"} if "child" in scenario else {}
            if "fallback" in scenario:
                event = {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                    "trigger": {"type": "refusal", "category": "frontier_llm", "explanation": "PRIVATE_POLICY_TEXT"}}}
            else:
                event = {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_details": {
                    "type": "refusal", "category": "cyber", "explanation": "PRIVATE_POLICY_TEXT"}}}
            emit({"type": "stream_event", "session_id": SID, "event": event, **child})
            finish("THIS LATER SUCCESS MUST NOT BE DELIVERED")
            time.sleep(30)  # The bridge must stop the process rather than wait for a result/timeout.
        elif scenario in {"cancel", "timeout"}:
            assistant("working", "progress")
            time.sleep(30)
        elif scenario == "question":
            emit(
                {
                    "type": "assistant",
                    "session_id": SID,
                    "uuid": "question-message",
                    "message": {
                        "id": "question-message",
                        "content": [
                            {"type": "tool_use", "id": "ask-call", "name": "AskUserQuestion", "input": question_input}
                        ],
                    },
                }
            )
            emit(
                {
                    "type": "control_request",
                    "request_id": "question-request",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "AskUserQuestion",
                        "tool_use_id": "ask-call",
                        "input": question_input,
                    },
                }
            )
        elif scenario == "steer" and turn == 1:
            assistant("First part", "progress")
            finish("First turn")
        elif scenario == "errors":
            emit(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "session_id": SID,
                    "uuid": "result-error",
                    "errors": ["PRIVATE_ERROR_SENTINEL"],
                }
            )
        elif scenario == "aborted":
            finish(terminal_reason="aborted_tools")
        elif scenario == "foreign":
            finish(session_id=OTHER)
        elif scenario == "child":
            assistant("CHILD_ONLY", "child-answer", parent_tool_use_id="agent-call")
            finish()
        elif scenario in ("subagents", "subagent-ends"):
            # Stream rows of a subagent carry the usage as it was when written (output 1, no stop_reason);
            # its transcript, written after Claude Code replaced the usage, has the final count and an
            # earlier turn's request that this turn must not count.
            usage = {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 1}
            emit({"type": "assistant", "session_id": SID, "uuid": "call", "message": {"id": "root-call", "content": [
                {"type": "tool_use", "id": "agent-call", "name": "Agent", "input": {"description": "检查"}}]}})
            child = {"id": "child-1", "content": [{"type": "text", "text": "CHILD_ONLY"}], "stop_reason": None, "usage": usage}
            for key in ("c1", "c1-again"):
                emit({"type": "assistant", "session_id": SID, "uuid": key, "parent_tool_use_id": "agent-call", "message": child})
            store = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd()),
                                 SID, "subagents")
            os.makedirs(store, exist_ok=True)
            with open(os.path.join(store, "agent-a1.meta.json"), "w") as handle:
                json.dump({"toolUseId": "agent-call", "spawnDepth": 1}, handle)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            final = {**child, "stop_reason": "end_turn", "usage": {**usage, "output_tokens": 40}}
            old = {"id": "earlier-turn", "stop_reason": "end_turn", "usage": {**usage, "output_tokens": 999}}
            with open(os.path.join(store, "agent-a1.jsonl"), "w") as handle:
                for stamp, message in (("2000-01-01T00:00:00.000Z", old), (now, final)):
                    handle.write(json.dumps({"type": "assistant", "timestamp": stamp, "message": message}) + "\n")
            if scenario == "subagent-ends":  # The subagent returns while the turn runs on.
                emit({"type": "user", "session_id": SID, "uuid": "call-result", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "agent-call", "content": "done"}]}})
            finish()
        elif scenario == "child-failed":
            # A check fails, the root reruns it and only then finishes the turn.
            for task, status in (("check-1", "failed"), ("check-2", "completed")):
                task_event("task_started", task)
                task_event("task_notification", task, status=status)
            finish()
        elif scenario == "child-failed-late":
            # The failure arrives after the final root result and the process ends
            # without a notification turn, so the root never saw it.
            finish()
            task_event("task_notification", "child-task", status="failed")
            sys.exit(0)
        elif scenario == "background-late":
            # Claude Code order: the turn ends while a background task runs; the
            # task's end is followed by a notification turn with its own result.
            task_event("task_started", "bg-task")
            finish("Waiting for the background task")
            time.sleep(0.3)
            task_event("task_notification", "bg-task", status="completed")
            emit({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"})
            turn += 1
            finish("Final after background")
        elif scenario in {"background-running", "background-chain"}:
            # Like a topic watching a training run: each turn starts a background wait, replies
            # and ends; the wait's end wakes the next turn. The last wait never ends here.
            rounds = 3 if scenario == "background-chain" else 1
            for n in range(1, rounds + 1):
                if n > 1:
                    time.sleep(float(pause or 1.2))
                    task_event("task_notification", f"bg-{n - 1}", status="completed")
                    emit({"type": "system", "subtype": "session_state_changed", "session_id": SID, "state": "running"})
                    turn += 1
                emit({"type": "assistant", "session_id": SID, "uuid": f"wait-{n}", "message": {"id": f"wait-{n}",
                      "content": [{"type": "tool_use", "id": f"bg-{n}-call", "name": "Bash", "input": {}}]}})
                task_event("task_started", f"bg-{n}")
                finish(f"Reply {n}")
            time.sleep(30)
        elif scenario == "max-tokens":
            finish(stop_reason="max_tokens")
        elif scenario == "image":
            assert any(
                b.get("type") == "image" and b["source"]["data"] == "aW1hZ2U=" for b in row["message"]["content"]
            )
            finish()
        elif scenario == "malformed":
            print("not json", flush=True)
        else:
            finish("Final after supplement" if scenario == "steer" else "Complete answer")
    elif row["type"] == "control_response":
        answer = row["response"]["response"]
        assert answer["updatedInput"]["answers"] == {"Select inputs": "A, B"}
        emit(
            {
                "type": "user",
                "session_id": SID,
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "ask-call", "content": "PRIVATE_TOOL_RESULT"}]
                },
            }
        )
        finish("Answered correctly")
if scenario == "nonzero":
    sys.exit(7)
if scenario == "slow-exit":
    sys.stdout.flush()
    os.close(1)  # sys.stdout.close() leaves fd 1 open.
    time.sleep(6)

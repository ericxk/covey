"""A scripted ``agy --output-format stream-json --input-format stream-json`` run: records its argv, stdin and
cwd, then prints the NDJSON rows of ``FAKE_AGY_SCENARIO`` (default: one successful turn)."""
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["models"]:
    if os.environ.get("FAKE_AGY_SCENARIO") == "logged-out":
        print("Authentication required. Please visit the URL to log in:", file=sys.stderr); sys.exit(1)
    print("Available models:\n  gemini-3.8-pro-high (default)\n  gemini-3.8-flash-medium\n  claude-opus-5-5\n"
          "Use --model <slug> to pick one.")
    sys.exit(0)
record = {"argv": args, "stdin": sys.stdin.read(), "cwd": os.getcwd()}
json.dump(record, open(os.environ["FAKE_AGY_RECORD"], "w"))
scenario = os.environ.get("FAKE_AGY_SCENARIO", "ok")
SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
sid = args[args.index("--conversation") + 1] if "--conversation" in args else SID
text = os.environ.get("FAKE_AGY_TEXT", "完成")
usage = {"input_tokens": 10415, "output_tokens": 657, "thinking_tokens": 616, "cache_read_tokens": 8113,
         "total_tokens": 11072}


def emit(row):
    print(json.dumps(row, ensure_ascii=False), flush=True)


def step(index, state, kind, **fields):
    emit({"event": "step_update", "step_update": {"conversation_id": sid, "step_index": index, "state": state,
                                                  "step_type": kind, **fields}})


def result(status, response="", **fields):
    emit({"event": "result", "result": {"conversation_id": sid, "status": status, "response": response,
                                        "duration_seconds": 7.1, "num_turns": 1, "usage": usage, **fields}})


if scenario == "logged-out":
    print("Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/o/oauth2/auth?x=1",
          file=sys.stderr)
    print("error: authentication failed or timed out", file=sys.stderr)
    emit({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "",
                                        "error": "authentication failed or timed out"}})
    sys.exit(1)
if scenario == "garbage":
    print("not json", flush=True); sys.exit(0)
emit({"event": "init", "conversation_id": "11111111-2222-4333-8444-555555555555" if scenario == "mismatch" else sid,
      "init": {"cwd": os.getcwd(), "tools": ["run_command", "view_file"], "permission_mode": "always-proceed"}})
step(0, "DONE", "user_input")
step(1, "ACTIVE", "agent_response", text_delta="先看一下")
step(1, "ACTIVE", "agent_response", text_delta="目录")
step(1, "DONE", "agent_response")
step(2, "ACTIVE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": {
    "CommandLine": "ls", "Cwd": "/x"}})
if scenario == "slow":
    time.sleep(30)
step(2, "DONE", "tool", tool_name="run_command", duration_seconds=0.07, tool_info={
    "name": "run_command", "parameters": {"CommandLine": "ls"}, "output": "PRIVATE_OUTPUT\r\n"})
# A subagent's own step: never the root's answer.
emit({"event": "step_update", "step_update": {"conversation_id": "99999999-2222-4333-8444-555555555555",
                                              "step_index": 0, "state": "DONE", "step_type": "agent_response",
                                              "text_delta": "PRIVATE_SUBAGENT"}})
if scenario == "busy":
    result("ERROR", error="429 RESOURCE_EXHAUSTED: PRIVATE_DETAIL"); sys.exit(3)
if scenario == "error":
    result("ERROR", error="PRIVATE_DETAIL internal"); sys.exit(3)
step(3, "ACTIVE", "agent_response", text_delta=text)
step(3, "DONE", "agent_response")
result("SUCCESS", text)
if scenario == "exit-after-result":
    sys.exit(3)

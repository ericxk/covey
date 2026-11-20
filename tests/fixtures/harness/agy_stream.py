"""A scripted ``agy --output-format stream-json --input-format stream-json`` run: records its argv, stdin and
cwd, then prints the NDJSON rows of ``FAKE_AGY_SCENARIO`` (default: one successful turn)."""
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["models"]:
    if os.environ.get("FAKE_AGY_SCENARIO") == "logged-out":
        print("Authentication required. Please visit the URL to log in:", file=sys.stderr); sys.exit(1)
    # As agy 1.5.13 prints them (2026-11-20); "Fetching available models..." goes to stderr.
    print("Fetching available models...", file=sys.stderr)
    print("gemini-4-neon-high\tGemini 4 Neon (High)\ngemini-4-neon-low\tGemini 4 Neon (Low)\n"
          "gemini-pro-orchestrator\tOrchestrator (Carbon)\ngemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)")
    sys.exit(0)
record = {"argv": args, "stdin": sys.stdin.read(), "cwd": os.getcwd()}
json.dump(record, open(os.environ["FAKE_AGY_RECORD"], "w"))
scenario = os.environ.get("FAKE_AGY_SCENARIO", "ok")
SID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"
sid = args[args.index("--conversation") + 1] if "--conversation" in args else SID
text = os.environ.get("FAKE_AGY_TEXT", "完成")
# Rows as agy 1.5.13 printed them (2026-11-20): each model request's usage on its agent_response's DONE, input
# without the cache reads; the result's usage is the turn's sum.
usage = {"input_tokens": 15436, "output_tokens": 132, "thinking_tokens": 65, "cache_read_tokens": 11130,
         "total_tokens": 15568}


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
step(1, "DONE", "agent_response", duration_seconds=4.0, usage={
    "input_tokens": 13187, "output_tokens": 91, "thinking_tokens": 40, "cache_read_tokens": 0, "total_tokens": 13278})
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
step(3, "DONE", "agent_response", text_delta="\n", duration_seconds=1.0, usage={
    "input_tokens": 2249, "output_tokens": 41, "thinking_tokens": 25, "cache_read_tokens": 11130, "total_tokens": 2290})
result("SUCCESS", text)
if scenario == "exit-after-result":
    sys.exit(3)

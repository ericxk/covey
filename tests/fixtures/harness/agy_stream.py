"""A scripted ``agy --output-format stream-json --input-format stream-json`` run: records its argv, stdin and
cwd, then prints the NDJSON rows of ``FAKE_AGY_SCENARIO`` (default: one successful turn). ``FAKE_AGY_SPENT`` in its
environment (an account whose quota is spent, the agy_turn hook's) ends it failed instead: ``before-init`` before the
conversation, ``after-input`` once agy took the words in, ``after-steps`` after a tool step, ``logged-out`` as one."""
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
record = {"argv": args, "stdin": sys.stdin.read(), "cwd": os.getcwd(), "home": os.environ.get("HOME", ""),
          "spent": os.environ.get("FAKE_AGY_SPENT", ""),
          "cli": os.path.realpath(os.path.join(os.environ.get("HOME", ""), ".gemini", "antigravity-cli"))}
spent = record["spent"]
try:  # The MCP servers this run's home gives agy (backends/agy/home.py).
    record["mcp"] = json.load(open(os.path.join(record["home"], ".gemini", "config", "mcp_config.json")))
except (OSError, ValueError):
    pass
json.dump(record, open(os.environ["FAKE_AGY_RECORD"], "w"))
with open(os.environ["FAKE_AGY_RECORD"] + ".all", "a") as runs:  # Every process of the turn, in order.
    runs.write(json.dumps(record) + "\n")
scenario = os.environ.get("FAKE_AGY_SCENARIO", "ok")
if scenario == "model-gone":
    # agy 1.5.18 once the account's list lost the model (T872): before the conversation, on stderr only.
    print('error: invalid model selection (--model "gemini-4-neon" --effort "xhigh"): --effort is not supported for '
          'model "gemini-4-neon" PRIVATE_DETAIL', file=sys.stderr); sys.exit(1)
if scenario == "crash":
    print("panic: runtime error: index out of range [3] with length 3", file=sys.stderr); sys.exit(2)
if scenario in ("steer", "steer-between") and "--conversation" in args:
    scenario = "ok"  # The process after the supplement: the turn goes on and ends.
if scenario in ("steer", "steer-between"):
    import signal

    def interrupted(*_):
        # agy 1.5.13 on SIGINT (2026-11-20): an ERROR result "interrupted", exit 1; the command it ran stays behind.
        print("error: interrupted", file=sys.stderr)
        result("ERROR", error="interrupted")
        os._exit(1)
    signal.signal(signal.SIGINT, interrupted)
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
    if scenario == "result-usage":  # No request says its usage: only the result does.
        fields.pop("usage", None)
    emit({"event": "step_update", "step_update": {"conversation_id": sid, "step_index": index, "state": state,
                                                  "step_type": kind, **fields}})


def result(status, response="", **fields):
    emit({"event": "result", "result": {"conversation_id": sid, "status": status, "response": response,
                                        "duration_seconds": 7.1, "num_turns": 1, "usage": usage, **fields}})


if spent == "before-init":
    emit({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "", "error":
          "RESOURCE_EXHAUSTED (code 429): You have exhausted your capacity on this model. Your quota will reset after "
          "1h2m3s."}})
    sys.exit(1)
if scenario == "logged-out" or spent == "logged-out":
    print("Authentication required. Please visit the URL to log in:\n  https://accounts.google.com/o/oauth2/auth?x=1",
          file=sys.stderr)
    print("error: authentication failed or timed out", file=sys.stderr)
    emit({"event": "result", "result": {"conversation_id": "", "status": "ERROR", "response": "",
                                        "error": "authentication failed or timed out"}})
    sys.exit(1)
if scenario == "garbage":
    print("not json", flush=True); sys.exit(0)
if json.loads(record["stdin"] or "{}").get("message", {}).get("content") == "/compact":
    # agy 1.5.16 with slash commands on (2026-11-25): a checkpoint step with its request's usage, an empty response. On a
    # conversation too short to compact (``compact-short``) the model answers the words instead.
    emit({"event": "init", "conversation_id": sid, "init": {"cwd": os.getcwd(), "tools": ["run_command"]}})
    step(2, "DONE", "user_input")
    if os.environ.get("FAKE_AGY_COMPACT") == "short":
        step(3, "DONE", "agent_response", text_delta="Context summary", usage={
            "input_tokens": 2290, "output_tokens": 482, "thinking_tokens": 436, "cache_read_tokens": 17575})
        result("SUCCESS", "Context summary"); sys.exit(0)
    if os.environ.get("FAKE_AGY_COMPACT") == "fail":
        result("ERROR", error="compaction failed"); sys.exit(1)
    step(3, "DONE", "system_message", duration_seconds=0.0003)
    step(4, "DONE", "checkpoint", duration_seconds=30.0, usage={
        "input_tokens": 3125, "output_tokens": 2598, "thinking_tokens": 130, "cache_read_tokens": 69866})
    result("SUCCESS", ""); sys.exit(0)
emit({"event": "init", "conversation_id": "11111111-2222-4333-8444-555555555555" if scenario == "mismatch" else sid,
      "init": {"cwd": os.getcwd(), "tools": ["run_command", "view_file"], "permission_mode": "always-proceed"}})
step(0, "DONE", "user_input")
if spent == "after-input":
    result("ERROR", error="You have exhausted your quota on this model."); sys.exit(1)
step(1, "ACTIVE", "agent_response", text_delta="先看一下")
step(1, "ACTIVE", "agent_response", text_delta="目录")
step(1, "DONE", "agent_response", duration_seconds=4.0, usage={
    "input_tokens": 13187, "output_tokens": 91, "thinking_tokens": 40, "cache_read_tokens": 0, "total_tokens": 13278})
if scenario in ("steer", "steer-between"):
    import subprocess
    # The command the tool step runs, in a session of its own and holding the output open: the stop ends it.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    open(os.environ["FAKE_AGY_RECORD"] + ".child", "w").write(str(child.pid))
step(2, "ACTIVE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": {
    "CommandLine": "ls", "Cwd": "/x"}})
if scenario == "slow":
    time.sleep(30)
if scenario == "steer":
    time.sleep(30)
if scenario == "steer-between":
    # The command runs until the test lets it end (a file): the supplement sent meanwhile must not stop it.
    waited = time.monotonic() + 30
    while not os.path.exists(os.environ["FAKE_AGY_RECORD"] + ".go") and time.monotonic() < waited:
        time.sleep(0.05)
step(2, "DONE", "tool", tool_name="run_command", duration_seconds=0.07, tool_info={
    "name": "run_command", "parameters": {"CommandLine": "ls"}, "output": "PRIVATE_OUTPUT\r\n"})
if scenario == "steer-between":
    time.sleep(30)  # The step is done: the stop for the supplement comes now.
# A subagent's own step: never the root's answer.
emit({"event": "step_update", "step_update": {"conversation_id": "99999999-2222-4333-8444-555555555555",
                                              "step_index": 0, "state": "DONE", "step_type": "agent_response",
                                              "text_delta": "PRIVATE_SUBAGENT"}})
if spent == "after-steps":
    result("ERROR", error="You have exhausted your quota on this model. PRIVATE_DETAIL"); sys.exit(1)
if scenario == "busy":
    result("ERROR", error="429 RESOURCE_EXHAUSTED: PRIVATE_DETAIL"); sys.exit(3)
if scenario == "error":
    result("ERROR", error="PRIVATE_DETAIL internal"); sys.exit(3)
if scenario == "unavailable":
    result("ERROR", error="UNAVAILABLE (code 503): The model is overloaded. PRIVATE_DETAIL"); sys.exit(3)
if scenario == "quota-window":
    result("ERROR", error="RESOURCE_EXHAUSTED (code 429): You have exhausted your capacity on this model. "
                          "Your quota will reset after 1h2m3s. PRIVATE_DETAIL"); sys.exit(3)
if scenario == "quota":
    result("ERROR", error="PRIVATE_DETAIL: you are out of credits"); sys.exit(3)
if scenario == "quota-days":
    # agy 1.5.18 on an account whose plan's quota is spent for days (T872).
    result("ERROR", error="RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to "
                          "increase your limits. Resets in 157h33m41s. PRIVATE_DETAIL"); sys.exit(3)
if scenario == "refused":
    result("ERROR", error="INVALID_ARGUMENT (code 400): response blocked by SAFETY. PRIVATE_DETAIL"); sys.exit(3)
if scenario == "bad-request":
    result("ERROR", error="INVALID_ARGUMENT (code 400): * GenerateContentRequest.tools PRIVATE_DETAIL"); sys.exit(3)
if scenario == "waits":
    # A subagent sent in the background, a line while it runs, its return as a system message (T588 #5298).
    step(3, "DONE", "subagent", tool_name="invoke_subagent", subagent_info={"subagents": [{"role": "Counter"}]})
    step(4, "DONE", "agent_response", text_delta="正在等待子 Agent。\n")
    step(5, "DONE", "system_message")
    step(6, "ACTIVE", "agent_response", text_delta=text)
    step(6, "DONE", "agent_response", text_delta="\n")
    result("SUCCESS", "先看一下目录正在等待子 Agent。\n" + text + "\n")
    sys.exit(0)
step(3, "ACTIVE", "agent_response", text_delta=text)
step(3, "DONE", "agent_response", text_delta="\n", duration_seconds=1.0, usage={
    "input_tokens": 2249, "output_tokens": 41, "thinking_tokens": 25, "cache_read_tokens": 11130, "total_tokens": 2290})
# agy's response joins all the turn wrote: the progress line before the tool, then the answer (1.5.13, T588).
result("SUCCESS", "先看一下目录" + text + "\n")
if scenario == "exit-after-result":
    sys.exit(3)

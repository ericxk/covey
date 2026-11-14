"""A scripted dsh headless run: records its argv, stdin, patch and env, then
prints the JSON rows of ``FAKE_DSH_SCENARIO`` (default: one completed turn)."""
import json, os, sys, time
args = sys.argv[1:]
patch = args[args.index("--patch") + 1]
record = {"argv": args, "stdin": sys.stdin.read(), "patch": json.load(open(patch)),
          "patch_mode": oct(os.stat(patch).st_mode & 0o777),
          "env": {k: os.environ.get(k) for k in ("ACME_API_KEY", "DSH_HOME", "DSH_PERMISSION_MODE", "DSH_TELEMETRY_MODE")}}
json.dump(record, open(os.environ["FAKE_DSH_RECORD"], "w"))
scenario = os.environ["FAKE_DSH_SCENARIO"]
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "session-11111111-1111-4111-8111-111111111111"
def emit(row):
    print(json.dumps(row), flush=True)
if scenario == "usage-error":
    emit({"type": "error", "message": "unknown option"}); sys.exit(1)
if scenario == "garbage":
    print("not json", flush=True); sys.exit(0)
emit({"type": "session", "sessionId": "session-22222222-2222-4222-8222-222222222222" if scenario == "mismatch" else sid, "cwd": os.getcwd()})
emit({"type": "status", "phase": "turn_start", "turn": 1})
emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
if scenario == "slow":
    time.sleep(30)
if scenario == "busy":
    emit({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "error", "error": {
        "code": "RATE_LIMIT", "message": "429 业务申请资源不足"}}})
    emit({"type": "final", "text": ""}); sys.exit(1)
if scenario == "missing-credential":
    emit({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "error", "error": {
        "code": "MISSING_CREDENTIAL", "message": "PRIVATE_DETAIL ACME_API_KEY"}}})
    emit({"type": "final", "text": ""}); sys.exit(1)
emit({"type": "text", "text": "先看一下目录"})
emit({"type": "thinking", "text": "PRIVATE_REASONING"})
emit({"type": "tool_call", "callId": "toolu_1", "tool": "bash", "input": {"command": "ls", "description": "List files"}})
emit({"type": "tool_result", "callId": "toolu_1", "status": "completed", "result": "PRIVATE_OUTPUT"})
emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {"inputTokens": 100, "outputTokens": 7}})
emit({"type": "status", "phase": "step_start", "turn": 1, "step": 2})
emit({"type": "text", "text": os.environ.get("FAKE_DSH_TEXT", "完成")})
emit({"type": "status", "phase": "step_end", "turn": 1, "step": 2,
      "usage": {"inputTokens": 20, "outputTokens": 3, "cacheReadTokens": 90}})
emit({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}})
emit({"type": "final", "text": os.environ.get("FAKE_DSH_TEXT", "完成")})
if scenario == "exit-after-final":
    sys.exit(3)

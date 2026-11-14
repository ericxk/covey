"""A scripted ``pi --mode rpc``: answers get_state, prompt, steer and abort like pi 0.73.1 (and 1.5.2) and plays
``FAKE_PI_SCENARIO`` (default: one completed turn). Records its argv, environment, home files and
every command in ``FAKE_PI_RECORD``; exits 0 when stdin closes. ``FAKE_PI_HOLD`` seconds pass between
the prompt and its answer, which then comes all at once, as Acme hands a response over."""
import json, os, sys, time
args = sys.argv[1:]
home = os.environ.get("PI_CODING_AGENT_DIR", "")
scenario = os.environ.get("FAKE_PI_SCENARIO", "ok")
record = {"argv": args, "cwd": os.getcwd(), "commands": [],
          "env": {k: os.environ.get(k) for k in ("PI_CODEX_ACCESS_TOKEN", "ACME_API_KEY", "PI_TELEMETRY", "PI_OFFLINE",
                                                  "NO_PROXY")},
          "home": {name: json.load(open(os.path.join(home, name))) for name in ("auth.json", "models.json", "settings.json")
                   if home and os.path.exists(os.path.join(home, name))},
          "home_mode": oct(os.stat(home).st_mode & 0o777) if home else ""}
def save():
    json.dump(record, open(os.environ["FAKE_PI_RECORD"], "w"))
save()
def emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()
def flag(name):
    return args[args.index(name) + 1] if name in args else None
if flag("--session"):
    path = flag("--session")
    sid = os.path.basename(path).split("_", 1)[1][:-len(".jsonl")]
else:
    sid = "01a1a217-4147-747b-a08f-83323257ddf3" if flag("--fork") is None else "01a1a21e-5b23-7308-9dd0-f72c93ca797c"
    folder = flag("--session-dir")  # pi 0.73.1 and 1.5.2 write straight into --session-dir, no directory per cwd.
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "2026-11-04T00-00-00-000Z_" + sid + ".jsonl")
    open(path, "a").close()
if scenario == "mismatch":
    sid = "01a1a21e-5b22-71e3-80e0-b8e0a319c443"
USAGE = {"input": 100, "output": 7, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 157}
SIGNATURE = json.dumps({"v": 1, "id": "msg_1", "phase": "final_answer"}, separators=(",", ":"))
def assistant(content, reason, usage=None, error=None):
    message = {"role": "assistant", "content": content, "api": "openai-codex-responses", "provider": "openai-codex",
               "model": "gpt-6-astra", "usage": usage or {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
               "stopReason": reason}
    if error:
        message["errorMessage"] = error
    return message
def stream(text, reason="stop", usage=None):
    emit({"type": "message_start", "message": assistant([], "stop")})
    emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0}})
    emit({"type": "message_update", "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0, "content": ""}})
    for piece in (text[:1], text[1:]):
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": piece}})
    final = assistant([{"type": "thinking", "thinking": ""}, {"type": "text", "text": text, "textSignature": SIGNATURE}],
                      reason, usage or USAGE)
    emit({"type": "message_end", "message": final})
    return final
def failed(error):
    emit({"type": "message_start", "message": assistant([], "stop")})
    message = assistant([], "error", error=error)
    emit({"type": "message_end", "message": message})
    emit({"type": "turn_end", "message": message})
    emit({"type": "agent_end", "messages": [message]})
def end(message):
    emit({"type": "turn_end", "message": message})
    emit({"type": "agent_end", "messages": [message]})
def tool(name="bash", command="sleep 6"):
    emit({"type": "message_start", "message": assistant([], "stop")})
    call = {"type": "toolCall", "id": "call_1|fc_1", "name": name, "arguments": {"command": command}}
    emit({"type": "message_end", "message": assistant([{"type": "text", "text": "先看一下目录"}, call], "toolUse", USAGE)})
    emit({"type": "tool_execution_start", "toolCallId": "call_1|fc_1", "toolName": name, "args": {"command": command}})
def tool_done(text="step-one\n", error=False):
    emit({"type": "tool_execution_end", "toolCallId": "call_1|fc_1", "toolName": "bash",
          "result": {"content": [{"type": "text", "text": text}]}, "isError": error})
    emit({"type": "turn_start"})
compacting = False
for line in sys.stdin:
    command = json.loads(line)
    record["commands"].append({k: v for k, v in command.items() if k != "images"}
                              | ({"images": len(command["images"])} if "images" in command else {}))
    save()
    kind, rid = command.get("type"), command.get("id")
    if kind == "get_state":
        emit({"id": rid, "type": "response", "command": "get_state", "success": True, "data": {
            "sessionId": sid, "sessionFile": path, "isStreaming": False, "isCompacting": compacting}})
        if compacting:
            compacting = False
            emit({"type": "compaction_end", "reason": "threshold", "result": {"summary": "s"}, "aborted": False, "willRetry": False})
        continue
    if kind == "abort":
        if scenario == "abort":
            tool_done("Command aborted", error=True)
            message = assistant([], "aborted")
            emit({"type": "message_start", "message": message})
            emit({"type": "message_end", "message": message})
            end(message)
        emit({"id": rid, "type": "response", "command": "abort", "success": True})
        continue
    if kind == "steer":
        if scenario == "steer-refused":
            emit({"id": rid, "type": "response", "command": "steer", "success": False, "error": "not streaming"})
            continue
        emit({"type": "queue_update", "steering": [command["message"]], "followUp": []})
        if scenario not in {"steer-early", "steer-unacked"}:
            emit({"id": rid, "type": "response", "command": "steer", "success": True})
        if scenario in {"steer", "steer-early", "steer-unacked"}:
            tool_done()
            emit({"type": "queue_update", "steering": [], "followUp": []})
            user = {"role": "user", "content": [{"type": "text", "text": command["message"]}]}
            emit({"type": "message_start", "message": user})
            emit({"type": "message_end", "message": user})
            if scenario == "steer-early":  # pi started the message before it printed the ack.
                time.sleep(0.5)
                emit({"id": rid, "type": "response", "command": "steer", "success": True})
            end(stream("banana"))
        continue
    if kind != "prompt":
        continue
    if scenario == "refused":
        emit({"id": rid, "type": "response", "command": "prompt", "success": False, "error": "No API key found for openai-codex"})
        continue
    emit({"id": rid, "type": "response", "command": "prompt", "success": True})
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    user = {"role": "user", "content": [{"type": "text", "text": command["message"]}]}
    emit({"type": "message_start", "message": user})
    emit({"type": "message_end", "message": user})
    if scenario in {"steer", "steer-early", "steer-unacked", "steer-refused", "abort"}:
        tool()
        if scenario == "steer-refused":
            tool_done()
            end(stream("完成"))
        continue
    if scenario == "tool":
        tool()
        tool_done()
    if scenario in {"retry", "retry-spent"}:
        for attempt in (1, 2):
            failed("429 业务申请资源不足，平台申请扩容 (https://gateway.example/model)")
            if scenario == "retry-spent" and attempt == 2:
                emit({"type": "auto_retry_end", "success": False, "attempt": 1, "finalError": "429"})
                break
            emit({"type": "auto_retry_start", "attempt": attempt, "maxAttempts": 2, "delayMs": 2000,
                  "errorMessage": "429 业务申请资源不足，平台申请扩容"})
            emit({"type": "agent_start"})
            emit({"type": "turn_start"})
        if scenario == "retry-spent":
            continue
    if scenario == "auth":
        failed("401 Unauthorized: token expired")
        continue
    if scenario == "length":
        end(stream("半截", reason="length"))
        continue
    time.sleep(float(os.environ.get("FAKE_PI_HOLD", "0")))
    final = stream(os.environ.get("FAKE_PI_TEXT", "完成"))
    if scenario == "retry":
        emit({"type": "auto_retry_end", "success": True, "attempt": 2})
    end(final)
    if scenario == "compact":
        compacting = True
        emit({"type": "compaction_start", "reason": "threshold"})
    if scenario == "exit-after-end":
        sys.exit(3)

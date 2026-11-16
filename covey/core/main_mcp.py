"""The MCP server in the main thread's session (docs/main-thread.md), run as bin/covey_main_mcp.py by the Claude
Code of a main-thread job (core/main_thread.py mcp_config). Standard library only.

stdio, JSON-RPC 2.0, one message a line: ``initialize`` (the protocol version the client asks for when this server
knows it, else the newest it knows), ``notifications/initialized``, ``ping``, ``tools/list`` (core/main_tool_specs.py
TOOLS, ``input_schema`` given as MCP's ``inputSchema``) and ``tools/call``. A call is handed to the Bridge through
core/main_requests.py (``submit``, with the state directory and job key of STATE_ENV and JOB_ENV) and its answer
returned as one text block, ``isError`` when it is not ok; its ``data``, when not empty, follows the text as a short
JSON line. Calls run each on its own thread, so the model's parallel calls wait on the Bridge together. Without
either variable every call answers what is missing; the tools are still listed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

from covey.core import env as cenv
from covey.core import main_requests, main_thread
from covey.core.main_tool_specs import ALL_NAMES, BUTLER_PROJECT_NAMES, PROJECT_NAMES, listed

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")  # Newest first: the answer to a version not here.
SERVER = {"name": "harness-bridge", "title": "Harness Bridge", "version": "1"}
DATA_CHARS = 2000  # Of the JSON line after an answer's text.
PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602


def tool_list(environ=None) -> list[dict]:
    """The tools in ``environ`` (core/main_tool_specs.py listed)."""
    return [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
            for tool in listed(os.environ if environ is None else environ)]


def content(answer) -> dict:
    """An answer {"ok", "text", "data"} as a tools/call result."""
    answer = answer if isinstance(answer, dict) else {}
    text = str(answer.get("text") or ("完成" if answer.get("ok") else "Bridge 没有给出说明"))
    data = answer.get("data")
    if isinstance(data, dict) and data:
        extra = json.dumps(data, ensure_ascii=False, default=str)
        text += "\n\n" + (extra if len(extra) <= DATA_CHARS else extra[:DATA_CHARS] + "…")
    return {"content": [{"type": "text", "text": text}], "isError": not answer.get("ok")}


class Server:
    """``environ`` names the state directory and the job (STATE_ENV, JOB_ENV); ``submit`` is
    core/main_requests.py's, a test's stand-in otherwise."""

    def __init__(self, environ=None, submit=main_requests.submit):
        environ = os.environ if environ is None else environ
        self.environ = environ
        self.state_dir = cenv.get(main_thread.STATE_ENV, "", environ).strip()
        self.job_key = cenv.get(main_thread.JOB_ENV, "", environ).strip()
        self.submit = submit

    def call(self, params) -> dict:
        name, args = params.get("name"), params.get("arguments")
        args = {} if args is None else args
        if name not in ALL_NAMES and name not in PROJECT_NAMES and name not in BUTLER_PROJECT_NAMES:
            return content({"ok": False, "text": f"没有这个工具：{name}"})
        if name not in {tool["name"] for tool in listed(self.environ)}:
            return content({"ok": False, "text": f"这个部署没有开 {name}，这次调用没有执行。"})
        if not isinstance(args, dict):
            return content({"ok": False, "text": "参数要是一个对象"})
        missing = [env for env, value in ((main_thread.STATE_ENV, self.state_dir), (main_thread.JOB_ENV, self.job_key))
                   if not value]
        if missing:
            return content({"ok": False, "text": f"Bridge 工具没有配置好：缺少环境变量 {'、'.join(missing)}，"
                                                 "这次调用没有执行。管家任务要由 Bridge 启动。"})
        try:
            return content(self.submit(self.state_dir, name, args, job_key=self.job_key))
        except OSError as exc:
            return content({"ok": False, "text": f"写不了 Bridge 的请求目录（{type(exc).__name__}），这次调用没有执行。"})

    def handle(self, message):
        """The response to one message, None for a notification (or a response the client sent)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
        method, params, request_id = message.get("method"), message.get("params"), message.get("id")
        if "id" not in message or not isinstance(method, str):
            return None  # A notification (notifications/initialized, cancelled ...), or a response.
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return result(request_id, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                                       "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER})
        if method == "ping":
            return result(request_id, {})
        if method == "tools/list":
            return result(request_id, {"tools": tool_list(self.environ)})
        if method == "tools/call":
            if not isinstance(params.get("name"), str):
                return error(request_id, INVALID_PARAMS, "tools/call needs a tool name")
            return result(request_id, self.call(params))
        return error(request_id, NO_METHOD, f"method not found: {method}")


def result(request_id, value) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(server: Server, lines, write) -> None:
    """Answer each line of ``lines`` with ``write(line)``; a tools/call on its own thread. Returns at the end of
    input once the calls in flight have answered."""
    lock, calls = threading.Lock(), []

    def send(response):
        if response is not None:
            text = json.dumps(response, ensure_ascii=False)
            with lock:
                write(text + "\n")

    def one(message):
        try:
            send(server.handle(message))
        except Exception as exc:  # noqa: BLE001 - the client hears of it; the server keeps serving.
            send(error(message.get("id"), -32603, f"internal error: {type(exc).__name__}"))

    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send(error(None, PARSE_ERROR, "parse error"))
            continue
        if isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message:
            thread = threading.Thread(target=one, args=(message,), name="main-mcp-call", daemon=True)
            thread.start()
            calls.append(thread)
        elif isinstance(message, dict):
            one(message)
        else:
            send(error(None, INVALID_REQUEST, "invalid request"))
    for thread in calls:
        thread.join()


def main(argv=None) -> None:
    argparse.ArgumentParser(prog="covey_main_mcp.py",
                            description="The main thread's Bridge tools as an MCP server on stdio; the Bridge starts it "
                                        f"with {main_thread.STATE_ENV} and {main_thread.JOB_ENV} set.").parse_args(argv)
    stdin = open(sys.stdin.fileno(), encoding="utf-8", errors="replace", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    def write(text):
        stdout.write(text)
        stdout.flush()

    serve(Server(), stdin, write)


if __name__ == "__main__":
    main()

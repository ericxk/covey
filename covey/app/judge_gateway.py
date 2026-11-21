"""The small model's one-shot questions through a model gateway (core/gateways.py) where there is no Claude Code to ask:
COVEY_JUDGE_GATEWAY=<gateway>:<model> (unset: as before, such a question fails with ``no_backend``).

The questions are the ones app/track_judge.py ``ask`` puts to Claude Code (整理专项 in app/project_sorter.py, the patrol's
second layer in app/patrol_judge.py, the track judge): a system prompt, one JSON text in, one JSON text out. Where the
Claude Harness is enabled they still go to it; this channel is the fallback, one HTTP request to the gateway with the
owner's key for it (backends/gateway/connection.py ``api_key``: the key never reaches a log or an argv), no redirect
followed (core/http_upstream.py), the environment's proxy used, killed at the question's own limit.

The wire protocol is the model's as the gateway's cached catalog says it (``native-models/<gateway>.json``), else
Anthropic Messages for a gateway only dsh uses and OpenAI Responses for the others. The answer comes back as Claude
Code's JSON envelope (``{"type": "result", "is_error": false, "result": <text>}``), so every caller reads it as it reads
Claude Code's (track_judge.answer_text). Every failure is a ``Failed`` with its reason (``no_gateway``, ``no_key``,
``timeout``, ``network``, ``http_<status>``, ``bad_output``) and never the response body, which may echo the request.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from covey.app.track_standby import Failed
from covey.core import env as cenv
from covey.core import gateways, http_upstream
from covey.core.state import read_json

SWITCH = "COVEY_JUDGE_GATEWAY"
RESPONSES, MESSAGES = "openai-responses", "anthropic-messages"  # backends/gateway/connection.py's protocol names.
ALIASES = {"anthropic": MESSAGES}
MAX_TOKENS = 4096  # Of an answer: the verdicts are a line of JSON; 整理专项's suggestions a few dozen lines.
READ_BYTES = 4 * 1024 * 1024
ANTHROPIC_VERSION = "2023-06-01"
OPENER = http_upstream.opener()


def configured(environ=None):
    """(the gateway, the model) COVEY_JUDGE_GATEWAY names, ``gateway:model``; None when it is unset. A value that names no
    gateway, or no model, is Failed("no_gateway") at the question, so a typo is a reason in the logs, not silence."""
    value = str(cenv.get(SWITCH, "", environ) or "").strip()
    if not value:
        return None
    name, _, model = value.partition(":")
    found = gateways.named(name.strip())
    return (found, model.strip()) if found is not None and model.strip() else (None, value)


def protocol(gateway, model, state_dir=None) -> str:
    """The wire protocol ``model`` is served over: the gateway's cached catalog says, else MESSAGES for a gateway only dsh
    uses, RESPONSES for the others."""
    if state_dir is not None:
        rows = read_json(Path(state_dir) / "native-models" / (gateway.name + ".json"), {}).get("rows")
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and row.get("id") == model and row.get("protocol"):
                found = ALIASES.get(row["protocol"], row["protocol"])
                if found in (RESPONSES, MESSAGES):
                    return found
    return MESSAGES if tuple(gateway.harnesses) == ("dsh",) else RESPONSES


def request(gateway, model, wire, system, data, key):
    """(url, body, headers) of one question."""
    headers = {**gateway.header_dict(), "Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + key}
    if wire == MESSAGES:
        headers.update({"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
        body = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                "messages": [{"role": "user", "content": data}]}
        return gateway.url() + "/v1/messages", body, headers
    body = {"model": model, "instructions": system, "input": data, "max_output_tokens": MAX_TOKENS, "stream": False}
    return gateway.url() + "/v1/responses", body, headers


def answer(wire, payload) -> str:
    """The model's text in a response ``payload``; Failed("bad_output") without one."""
    if not isinstance(payload, dict):
        raise Failed("bad_output")
    if wire == MESSAGES:
        parts = [block.get("text") for block in payload.get("content") or () if isinstance(block, dict)
                 and block.get("type") == "text"]
    else:
        parts = [payload["output_text"]] if isinstance(payload.get("output_text"), str) else [
            block.get("text") for item in payload.get("output") or () if isinstance(item, dict)
            for block in item.get("content") or () if isinstance(block, dict)
            and block.get("type") in ("output_text", "text")]
    text = "".join(part for part in parts if isinstance(part, str))
    if not text.strip():
        raise Failed("bad_output")
    return text


def ask(system, data, limit, *, state_dir=None, environ=None, opener=None) -> str:
    """The answer to ``data`` (a JSON text) under ``system`` from the gateway COVEY_JUDGE_GATEWAY names, as Claude Code's
    JSON envelope; within ``limit`` seconds, else Failed("timeout")."""
    from covey.backends.gateway.connection import api_key
    found = configured(environ)
    if found is None or found[0] is None:
        raise Failed("no_gateway")
    gateway, model = found
    try:
        key = api_key(gateway)
    except ValueError:
        raise Failed("no_key") from None
    if not key:
        raise Failed("no_key")
    wire = protocol(gateway, model, state_dir)
    url, body, headers = request(gateway, model, wire, system, data, key)
    started = time.monotonic()
    try:
        status, response = http_upstream.send(opener or OPENER, "POST", url, json.dumps(body, ensure_ascii=False).encode(),
                                              headers, max(0.1, limit))
        raw = response.read(READ_BYTES)
    except (socket.timeout, TimeoutError):
        raise Failed("timeout") from None
    except OSError as exc:
        reason = getattr(exc, "reason", None)
        raise Failed("timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "network",
                     type(exc).__name__) from None
    if time.monotonic() - started > limit:
        raise Failed("timeout")
    if status != 200:
        raise Failed(f"http_{status}")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise Failed("bad_output") from None
    return json.dumps({"type": "result", "is_error": False, "result": answer(wire, payload), "via": "gateway"},
                      ensure_ascii=False)

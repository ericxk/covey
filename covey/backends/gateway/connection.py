"""模型网关的连接：个人密钥、模型目录、请求头（网关由 core/gateways.py 的配置描述，可以有多个）。

模型用个人密钥直接调用，不经封装也不带 SSO 令牌。Codex 经每线程一个的 provider 跑 OpenAI Responses 模型
（backends/codex/gateway.py），Pi 同（backends/pi/），dsh 跑 Anthropic Messages 模型（backends/dsh/bridge.py）。
密钥只经 Harness 的环境变量到达它，从不进 argv、配置文件或日志。
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path

from covey.core import http_upstream
from covey.core.logs import line_log
from covey.core.state import read_json, write_json

RESPONSES, MESSAGES = "openai-responses", "anthropic-messages"
# 目录里协议的别名：Acme 把它的 Messages 模型标成 "anthropic"（deepseek-v4.1-1017 经 anthropic-messages
# 跑通 dsh）。别的网关不会写这个，留着无害。
PROTOCOL_ALIASES = {"anthropic": MESSAGES}
CACHE_SECONDS = 900
# 没有固定上游模型的路由别名，从不是手选的启动选项。
DISABLED_MODELS = frozenset({"auto", "auto-max"})
# 429 表示上游容量不足或密钥请求太频繁，不是额度用完（那是 403）。dsh 一轮重试多久：见过的故障最长约
# 40 分钟，所以定一小时。Codex 的中继（relay.py）没有上限。
BUSY_WINDOW = 60 * 60
# dsh 的请求重试：2、4、8、16、32，然后 60 s（±20 %），次数以填满 BUSY_WINDOW 为准（一小时 64 次）。dsh 用 maxDelayMs
# 封顶每次等待（dsh-llm-retry, 0.2.1-rc.2）。
RETRY_POLICY = {"mode": "normal", "maxRetries": 5 + math.ceil((BUSY_WINDOW - 62) / 60),
                "backoff": {"initialDelayMs": 2000, "maxDelayMs": 60000, "jitterRatio": 0.2}}


def busy_text(gateway):
    """dsh 失败卡的详情（core/result.py 的恢复步骤跟在后面）。"""
    return (f"{gateway.display} 当前繁忙（通常是上游资源不足、用的人多），本轮已自动重试约 {BUSY_WINDOW // 60} 分钟仍未成功，"
            "与任务内容无关。" + (gateway.busy_hint + "。" if gateway.busy_hint else ""))


def api_key(gateway):
    """The SK from the gateway's key variable, else from its owner-only key file; "" when neither exists."""
    value = os.environ.get(gateway.key_env, "").strip()
    if value:
        return value
    path = gateway.key_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(f"{gateway.display} 密钥文件必须是本人所有、权限 600 的普通文件：{path}")
        return handle.read(4096).decode("utf-8", "replace").strip()


def child_env(gateway, env):
    key = api_key(gateway)
    if not key:
        raise ValueError(gateway.missing_key)
    return {**env, gateway.key_env: key}


LOG_LIMIT = 1024 * 1024


def relay_log(gateway, state_dir):
    """Lines about busy runs in <state>/<name>/relay.log: counts, timing and the gateway's error code and
    message (emails masked), never headers or request bodies; one older file kept."""
    return line_log(Path(state_dir) / gateway.name / "relay.log", max_bytes=LOG_LIMIT)


NO_REDIRECT = http_upstream.opener()  # 密钥只发往网关地址：3xx 就是答案，不跟随。


def fetch_models(gateway, timeout=20):
    key = api_key(gateway)
    if not key:
        raise ValueError(gateway.missing_key)
    import urllib.request  # Here: the Bridge's startup and a model-catalog read use it, a task runner's start does not.
    request = urllib.request.Request(gateway.url() + "/v1/models", headers={
        "Authorization": "Bearer " + key, **gateway.header_dict(), "Accept": "application/json"})
    try:
        with NO_REDIRECT.open(request, timeout=timeout) as response:
            data = json.loads(response.read(8 * 1024 * 1024))
    except (OSError, ValueError):
        # Response bodies and request headers never reach cards or logs.
        raise ValueError(f"{gateway.display} 模型目录不可用，请检查个人密钥和网络") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{gateway.display} 模型目录格式无效")
    return [row for row in rows if isinstance(row, dict)]


def parse_catalog(rows, protocol):
    """Manual models served over one wire protocol, in launch-catalog form."""
    catalog = {}
    for row in rows:
        model = row.get("id")
        if (not isinstance(model, str) or not model or model in DISABLED_MODELS or PROTOCOL_ALIASES.get(row.get("protocol"), row.get("protocol")) != protocol
                or row.get("status") in {"deprecated", "removed"} or row.get("hidden")):
            continue
        levels = []
        for value in row.get("supported_reasoning_levels") or []:
            effort = value.get("effort") if isinstance(value, dict) else value
            if isinstance(effort, str) and effort and effort not in levels:
                levels.append(effort)
        default = row.get("default_reasoning_level") or ""
        catalog[model] = {"display_name": row.get("display_name") or model,
                          "supported_reasoning_levels": [{"effort": value} for value in levels],
                          "default_reasoning_level": default if default in levels else ""}
    return catalog


def cache_path(gateway, state_dir):
    return Path(state_dir) / "native-models" / (gateway.name + ".json")


def cached_catalog(gateway, state_dir, protocol):
    try:
        saved = read_json(cache_path(gateway, state_dir))
        if 0 <= time.time() - saved["updated_at"] <= CACHE_SECONDS and isinstance(saved["rows"], list):
            return parse_catalog([row for row in saved["rows"] if isinstance(row, dict)], protocol)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def refresh_catalog(gateway, state_dir, protocol):
    rows = fetch_models(gateway)
    write_json(cache_path(gateway, state_dir), {"updated_at": time.time(), "rows": rows}, ensure_ascii=False)
    return parse_catalog(rows, protocol)


def catalog(gateway, state_dir, protocol):
    """A fresh cached directory, refreshed when expired; for validating a task's model."""
    return cached_catalog(gateway, state_dir, protocol) or refresh_catalog(gateway, state_dir, protocol)


def check_model(gateway, state_dir, protocol, model, effort):
    models = catalog(gateway, state_dir, protocol)
    if model not in models:
        raise ValueError(f"模型已不在 {gateway.display} 目录中；请在主聊天发送新消息，在启动卡上选择其他模型")
    if effort and effort not in {row["effort"] for row in models[model]["supported_reasoning_levels"]}:
        raise ValueError(f"此 {gateway.display} 模型不支持所选推理档位")
    return models

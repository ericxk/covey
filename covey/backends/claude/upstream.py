"""Where a Claude Code turn's requests go, and the parts a loopback relay in front of it is made of.

An extension's ``claude_turn`` hook may put a relay on 127.0.0.1 between a turn's Claude Code and its model service
(covey-internal's protocol-normalization relay, COVEY_CLAUDE_CLI_NORMALIZE=1): it hands the turn a context whose
``options[SETTINGS_ENV]`` is the env for Claude Code's ``--settings`` (``settings``), whose ANTHROPIC_BASE_URL points at
the relay. That setting overrides the process environment, so the personal gateway's ``claude`` wrapper still injects
its token and model, and compaction, subagents and side requests of the same process all go through it.

Where requests go on to (the first that applies): the ANTHROPIC_BASE_URL already in Claude Code's environment; the
``endpoint`` that ``<claude_cmd> --gateway-status`` prints (the personal gateway's wrapper; cached STATUS_TTL seconds in
<state>/claude-upstream.json); ANTHROPIC_BASE_URL in Claude Code's settings.json env (CLAUDE_CONFIG_DIR or ~/.claude);
else Anthropic's API.
"""
from __future__ import annotations

import gzip
import ipaddress
import json
import os
import subprocess
import time
import urllib.parse
import zlib
from pathlib import Path

from covey.core.state import read_json, read_object, write_json

DEFAULT_UPSTREAM = "https://api.anthropic.com"
STATUS_TTL = 600  # Seconds a --gateway-status answer is reused.
STATUS_TIMEOUT = 60  # The wrapper may first open its SSH tunnel (45 s) and check the gateway.
GATEWAY_FAILURE = "claude gateway:"  # How the personal gateway's wrapper starts a failure on stderr.
READ_TIMEOUT = 1800  # An upstream may queue a request before it answers.
# ctx.options while a relay runs: the env for --settings.
SETTINGS_ENV = "relay_env"
# Claude Code keeps one list of the gateway's models (cache/gateway-models.json) under the base URL it fetched it
# from; with the relay's port in it, the next catalog probe listed none (Opus 5 came and went in the menus). The turn
# runs its model by id without the list, so it does not fetch one (2.1.310).
GATEWAY_MODELS_ENV = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
HOP = frozenset({"host", "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade",
                 "transfer-encoding", "content-length", "accept-encoding"})
RESPONSE_DROP = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length"})


class Unavailable(ValueError):
    """Where the turn's requests should go is not known (the personal gateway's wrapper says its gateway failed)."""


def checked_url(value):
    """``value`` without a trailing slash when it is an http(s) address with no credentials, else ''."""
    value = str(value or "").strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # A bad port raises ValueError.
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc or parts.query \
            or parts.fragment or any(c.isspace() for c in value):
        return ""
    return value


def is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def gateway_status(ctx, env):
    """The ``endpoint`` the claude command reports with ``--gateway-status`` (the personal gateway's
    wrapper), cached; '' when it reports none (a plain Claude Code). Unavailable, not cached, when the
    wrapper says its gateway failed."""
    path = Path(ctx.state_dir) / "claude-upstream.json"
    command = list(ctx.backend_cmd)
    try:
        cached = read_json(path)
        if (isinstance(cached, dict) and cached.get("cmd") == command
                and 0 <= time.time() - float(cached.get("at", 0)) < STATUS_TTL):
            return str(cached.get("endpoint") or "")
    except (OSError, ValueError, TypeError):
        pass
    try:
        done = subprocess.run([*command, "--gateway-status"], env=env, cwd=str(ctx.workdir), capture_output=True,
                              text=True, timeout=STATUS_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is not None and done.returncode != 0 and GATEWAY_FAILURE in done.stderr:
        # The wrapper is there but its gateway is not: the token it injects must not go anywhere else.
        raise Unavailable("个人网关没有就绪（--gateway-status 失败），不知道该发往哪里")
    try:
        status = json.loads(done.stdout) if done is not None and done.returncode == 0 else {}
    except ValueError:
        status = {}
    endpoint = checked_url(status.get("endpoint")) if isinstance(status, dict) else ""
    try:
        write_json(path, {"cmd": command, "endpoint": endpoint, "at": time.time()})
    except OSError:
        pass
    return endpoint


def settings_upstream(env):
    """ANTHROPIC_BASE_URL in the env of Claude Code's user settings, or ''."""
    folder = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude")
    value = read_object(Path(folder) / "settings.json").get("env", {}).get("ANTHROPIC_BASE_URL")
    return checked_url(value) if isinstance(value, str) else ""


def decoded(body, encoding):
    """A request body without its Content-Encoding; ValueError for one the relay cannot read."""
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    raise ValueError("unsupported Content-Encoding")


def read_body(handler):
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        chunks = []
        while True:
            size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if not size:
                while handler.rfile.readline().strip():
                    pass  # Trailers.
                return b"".join(chunks)
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
    return handler.rfile.read(int(handler.headers.get("Content-Length") or 0))


def settings(ctx) -> dict:
    """The part of the turn's Claude Code --settings that points it at the turn's relay; {} for a turn without one."""
    env = ctx.options.get(SETTINGS_ENV)
    return {"env": env} if env else {}

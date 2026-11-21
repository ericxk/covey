"""The loopback relay that redacts one Claude Code turn of a topic that chose redaction.

``turn`` starts a relay on 127.0.0.1 for exactly as long as the turn and hands back the context to run
with: ``stream_argv`` then passes ``--settings`` whose env points Claude Code's ANTHROPIC_BASE_URL at the
relay. That setting overrides the process environment, so the personal gateway's ``claude`` wrapper
still injects its token and model, and compaction, subagents and side requests of the same process all
go through it. Each request body is redacted (core/redactor.py) before it is sent on and each reply
turned back on its way to Claude Code. A body that is not JSON is refused, never sent on as it is.

Where requests go on to (the first that applies): the ANTHROPIC_BASE_URL already in Claude Code's
environment; the config's ``upstream``; the ``endpoint`` that
``<claude_cmd> --gateway-status`` prints (the personal gateway's wrapper; cached STATUS_TTL seconds in
<state>/redaction/upstream.json); ANTHROPIC_BASE_URL in Claude Code's settings.json env
(CLAUDE_CONFIG_DIR or ~/.claude); else Anthropic's API.

Anything that keeps the turn from being redacted (the config, the topic file, the relay, the upstream)
raises Unavailable before Claude Code starts; the backend fails the turn with its words
(``unavailable_result``). Headers, Authorization included, pass through and are never logged:
<state>/redaction/relay.log has one line per request with its method, path, status, replacements and
milliseconds, never content or terms.
"""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import gzip
import ipaddress
import json
import os
import subprocess
import threading
import time
import urllib.parse
import zlib
from pathlib import Path

from covey.core.harness import RunResult
from covey.core.redaction import load_config, record_turn, topic_redaction
from covey.core.redactor import Redactor
from covey.core.result import ExecutionOutcome
from covey.core.http_upstream import copy_headers, opener, pipe, send
from covey.core.state import read_json, read_object, write_json
from covey.core.logs import line_log

DEFAULT_UPSTREAM = "https://api.anthropic.com"
STATUS_TTL = 600  # Seconds a --gateway-status answer is reused.
STATUS_TIMEOUT = 60  # The wrapper may first open its SSH tunnel (45 s) and check the gateway.
GATEWAY_FAILURE = "claude gateway:"  # How the personal gateway's wrapper starts a failure on stderr.
READ_TIMEOUT = 1800  # An upstream may queue a request before it answers.
LOG_LIMIT = 2 * 1024 * 1024
# ctx.options while a relay runs: the env for --settings, and the turn's `redaction` event so far.
SETTINGS_ENV = "redaction_env"
REPORT = "redaction_report"
# Claude Code keeps one list of the gateway's models (cache/gateway-models.json) under the base URL it fetched it
# from; with the relay's port in it, the next catalog probe listed none (Opus 5 came and went in the menus). The turn runs its model by id without the list, so it does not fetch one (2.1.310).
GATEWAY_MODELS_ENV = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
HOP = frozenset({"host", "connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "upgrade",
                 "transfer-encoding", "content-length", "accept-encoding"})
RESPONSE_DROP = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length"})


class Unavailable(ValueError):
    """The turn cannot be redacted; it is not sent."""


def wanted(ctx):
    """Whether the context's topic chose redaction; Unavailable when its file cannot be read."""
    try:
        return topic_redaction(ctx.state_dir, ctx.topic_id)
    except ValueError as exc:
        raise Unavailable(str(exc)) from None


def unavailable_result(request, exc):
    """A failed turn that never started Claude Code, with ``exc``'s words on the card."""
    outcome = ExecutionOutcome("failed", "redaction_failed", cleanup_confirmed=True, protection_confirmed=True)
    detail = str(exc).rstrip("。") + "。"
    return RunResult(outcome.summary(detail), request.session_id, True, None, outcome, failure_detail=detail)


def relay_log(state_dir):
    return line_log(Path(state_dir) / "redaction" / "relay.log", max_bytes=LOG_LIMIT, keep=2, live=True)


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
    path = Path(ctx.state_dir) / "redaction" / "upstream.json"
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
        raise Unavailable("个人网关没有就绪（--gateway-status 失败），敏感词中继不知道该发往哪里，本轮未发送")
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


def resolve_upstream(ctx, env, config):
    """Where the turn's requests go on to (see the module docstring); Unavailable for a broken address."""
    chained = str((ctx.options.get(SETTINGS_ENV) or {}).get("ANTHROPIC_BASE_URL") or "").strip()
    if chained:
        upstream = checked_url(chained)
        if not upstream:
            raise Unavailable("模型服务地址（ANTHROPIC_BASE_URL）无法识别，敏感词中继不知道该发往哪里，本轮未发送")
        return upstream
    if str(env.get("ANTHROPIC_BASE_URL") or "").strip():
        upstream = checked_url(env["ANTHROPIC_BASE_URL"])
        if not upstream:
            raise Unavailable("模型服务地址（ANTHROPIC_BASE_URL）无法识别，敏感词中继不知道该发往哪里，本轮未发送")
        return upstream
    return config["upstream"] or gateway_status(ctx, env) or settings_upstream(env) or DEFAULT_UPSTREAM


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


class Relay:
    def __init__(self, upstream, redactor, *, log=None, timeout=READ_TIMEOUT):
        self.upstream, self.redactor, self.timeout = upstream.rstrip("/"), redactor, timeout
        self.log = log or (lambda line: None)
        self.lock = threading.Lock()
        self.requests = 0
        # Every request carries the whole history again, so a sum would count one term many times: the
        # turn's figures are the most any one request had masked, in all and per term.
        self.peak = 0
        self.peak_terms = collections.Counter()
        # The relay's own hop to a loopback gateway never goes through a proxy; others use the environment's.
        # A redirect goes back to Claude Code as it came; the body is never sent to another address.
        self.opener = opener(direct=is_loopback(self.upstream))
        # The request line is in relay.log.
        from covey.core.serving import LoopbackServer  # Here: http.server stays out of a task runner's start.
        self.server = LoopbackServer(self.forward, "redaction-relay", read_body=read_body,
                                     verbs=("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"))

    @property
    def base_url(self):
        return self.server.base_url

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *args):
        self.server.__exit__(*args)

    def report(self):
        """The turn's ``redaction`` event: listed terms, replacements in the request that had the most,
        requests relayed."""
        with self.lock:
            return {"terms": len(self.redactor.terms), "hits": self.peak, "requests": self.requests}

    def term_peaks(self):
        """Per listed term, the most replacements of it in one request of the turn."""
        with self.lock:
            return dict(self.peak_terms)

    def forward(self, handler, body):
        started = time.monotonic()
        path = handler.path.split("?", 1)[0]
        tally = collections.Counter()
        with self.lock:
            self.requests += 1
        encoded = handler.headers.get("Content-Encoding", "identity").strip().lower()
        if body:
            try:
                parsed = json.loads(decoded(body, encoded))
            except (ValueError, OSError, zlib.error):  # Fail closed: a body that cannot be redacted is never sent on.
                self.log(f"{handler.command} {path} refused=not_json ms={_ms(started)}")
                return self.answer(handler, 400, {"type": "error", "error": {
                    "type": "invalid_request_error",
                    "message": "敏感词中继拒绝发送：请求体不是 JSON，无法脱敏"}})
            body = json.dumps(self.redactor.redact_request(parsed, tally), ensure_ascii=False).encode("utf-8")
            with self.lock:
                self.peak = max(self.peak, sum(tally.values()))
                for term, count in tally.items():
                    self.peak_terms[term] = max(self.peak_terms[term], count)
        # The body goes on as plain JSON, whatever encoding it came in.
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
        headers["Accept-Encoding"] = "identity"
        try:
            status, response = send(self.opener, handler.command, self.upstream + handler.path, body if body else None,
                                    headers, self.timeout)
        except OSError as exc:
            self.log(f"{handler.command} {path} upstream_error={type(exc).__name__} replaced={sum(tally.values())} "
                     f"ms={_ms(started)}")
            # Claude Code retries a 502 as it retries other transport failures.
            return self.answer(handler, 502, {"type": "error", "error": {
                "type": "api_error", "message": "敏感词中继连不上模型服务"}})
        with response:
            try:
                self.relay(handler, status, response)
            except (BrokenPipeError, ConnectionResetError):
                handler.close_connection = True
        self.log(f"{handler.command} {path} status={status} replaced={sum(tally.values())} ms={_ms(started)}")

    def answer(self, handler, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True

    def relay(self, handler, status, response):
        kind = response.headers.get("Content-Type", "")
        plain = response.headers.get("Content-Encoding", "identity").lower() == "identity"
        payload = None
        if plain and "json" in kind and handler.command != "HEAD":
            payload = response.read()
            try:
                payload = json.dumps(self.redactor.restore_response(json.loads(payload)),
                                     ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass  # Not JSON after all: as it came.
        handler.send_response(status)
        copy_headers(handler, response, RESPONSE_DROP)
        handler.send_header("Connection", "close")
        if payload is not None:
            handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.close_connection = True
        if payload is not None:
            handler.wfile.write(payload)
            return
        pipe(handler, response, transform=self.redactor.stream() if plain and "text/event-stream" in kind else None)


def _ms(started):
    return round((time.monotonic() - started) * 1000)


def settings(ctx) -> dict:
    """The part of the turn's Claude Code --settings that points it at the turn's relay; {} for a turn without one."""
    env = ctx.options.get(SETTINGS_ENV)
    return {"env": env} if env else {}


def turn_event(ctx):
    """The turn's ``redaction`` event data, or None for a turn without a relay."""
    report = ctx.options.get(REPORT)
    return report() if callable(report) else None


@contextlib.contextmanager
def turn(ctx, env, *, run_id=""):
    """(context, environment) for one redacted Claude Code turn; the relay lives exactly as long as
    the turn and its counts are recorded when it ends. Unavailable when it cannot be redacted."""
    try:
        config = load_config(ctx.state_dir)
    except ValueError as exc:
        raise Unavailable(f"{exc}，敏感词脱敏无法进行，本轮未发送") from None
    redactor = Redactor(config)
    upstream = resolve_upstream(ctx, env, config)
    try:
        relay = Relay(upstream, redactor, log=relay_log(ctx.state_dir))
    except OSError:
        raise Unavailable("敏感词中继没能启动，本轮未发送") from None
    # Replaces Claude Code's NO_PROXY whole (the wrapper's own included): the loopback names stay in it.
    hosts = [h.strip() for h in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if h.strip()]
    no_proxy = ",".join(dict.fromkeys(hosts + ["127.0.0.1", "localhost", "::1"]))
    settings = {"ANTHROPIC_BASE_URL": relay.base_url, "NO_PROXY": no_proxy, "no_proxy": no_proxy,
                GATEWAY_MODELS_ENV: "0"}
    with relay:
        try:
            yield dataclasses.replace(ctx, options={**ctx.options, SETTINGS_ENV: settings, REPORT: relay.report}), env
        finally:
            report = relay.report()
            record_turn(ctx.state_dir, {
                "topic_id": ctx.topic_id, "run_id": run_id, "harness": ctx.options.get("harness_id", "claude"),
                "model": ctx.model, "terms": report["terms"],
                "requests": report["requests"], "hits": relay.term_peaks()})

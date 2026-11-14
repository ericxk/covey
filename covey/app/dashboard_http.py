"""The sensitive-term dashboard's HTTP side: its server, each request's route (the page's files from
dashboard_static/ and /api/*, answered from core/redaction_views.py, 探测 by ``detect``) and access.

Access: one token in <state>/redaction/dashboard-token (0600, made on first use: ``dashboard_token``).
``/?t=<token>`` (the link on the 设置 card's 脱敏 tab) or the page's login form turn it into the HttpOnly,
SameSite=Strict cookie COOKIE; /api/* needs that cookie or ``Authorization: Bearer``, and anything but GET
also the header HEADER. FAILURES wrong tokens within FAILURE_WINDOW seconds from one address shut it out
for that long. The log has one line per request (method, path, status), and the status of one refused before
any route (not HTTP, such as https: ``refused``), never a token, cookie, term or body.

Held by Dashboard (app/dashboard.py) as ``http``; its worker starts the server (``server``).
"""
from __future__ import annotations

import hmac
import http.cookies
import json
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from covey.backends.claude.redaction import STATUS_TIMEOUT, Unavailable
from covey.core.redaction import save_config
from covey.core.redaction_views import config_view, preview, stats_view
from covey.core.state import write_private_text

COOKIE = "hb_dash"
COOKIE_AGE = 30 * 86400
HEADER = "X-Dashboard"  # Required on writes: a form or image on another site cannot set it.
MAX_BODY = 1024 * 1024
FAILURES = 5  # Wrong tokens within FAILURE_WINDOW seconds from one address before it is shut out.
FAILURE_WINDOW = 60
DETECT_TIMEOUT = STATUS_TIMEOUT + 15
STATIC = Path(__file__).with_name("dashboard_static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
SECURITY = (("Cache-Control", "no-store"), ("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                        "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"))
_TOKEN_LOCK = threading.Lock()


def token_path(state_dir) -> Path:
    return Path(state_dir) / "redaction" / "dashboard-token"


def dashboard_token(state_dir, *, rotate=False):
    """The dashboard's token, made on first use; ``rotate`` replaces it."""
    path = token_path(state_dir)
    with _TOKEN_LOCK:
        if not rotate:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                token = ""
            if len(token) >= 16:
                return token
        token = secrets.token_urlsafe(24)
        write_private_text(path, token + "\n")
        return token


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bind, handler, http, state_dir):
        self.address_family = socket.AF_INET6 if ":" in bind[0] else socket.AF_INET
        self.http, self.state_dir = http, Path(state_dir)
        super().__init__(bind, handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # IPv4 clients too.
            except OSError:
                pass
        socketserver.TCPServer.server_bind(self)  # HTTPServer's would look its own name up.
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HarnessBridge"
    sys_version = ""
    timeout = 30

    def log_message(self, *args):
        pass  # DashboardHttp.handle logs each request itself, without its query.

    def log_error(self, format, *args):
        # A request answered before any route (send_error): its status alone, never its line, which may carry
        # the token. A connection that sent nothing in time is not logged.
        if format.startswith("code %d") and args:
            self.server.http.refused(args[0])

    def do_GET(self):
        self.server.http.handle(self)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET


class DashboardHttp:
    """Requests and access. ``detect`` () -> {upstream, source} is 探测; ``lock`` guards ``failures`` and
    ``blocked``; ``detecting`` is held while a 探测 runs."""

    def __init__(self, runtime, detect=None):
        self.runtime, self.detect = runtime, detect
        self.lock = threading.Lock()
        self.failures = {}  # Address -> times of its recent wrong tokens.
        self.blocked = {}  # Address -> when it may try again.
        self.detecting = threading.Lock()

    def server(self, state_dir, bind):
        """A server on ``bind`` for ``state_dir``, not yet serving; OSError when it cannot listen."""
        return _Server(bind, _Handler, self, state_dir)

    def handle(self, handler):
        method, path = handler.command, urllib.parse.urlsplit(handler.path).path
        try:
            status = self.route(handler, method, path)
        except (BrokenPipeError, ConnectionResetError):
            status, handler.close_connection = 0, True
        except Exception as exc:  # noqa: BLE001 - one request fails, the server goes on.
            status = self.send_json(handler, 500, {"error": f"看板出错（{type(exc).__name__}）"})
        self.runtime.log(f"dashboard {method} {path[:80]} {status}")

    def refused(self, status):
        """A request answered before any route (``_Handler.log_error``). 400 is one that was not HTTP: most often
        a browser speaking https to this http port (HSTS, or its 始终使用安全连接 setting)."""
        status = int(status)
        self.runtime.log(f"dashboard bad request {status}" + (": not HTTP (https to this http port?)"
                                                              if status == 400 else ""))

    def route(self, handler, method, path):
        address = handler.client_address[0]
        state_dir = handler.server.state_dir
        if path in FILES:
            if method not in ("GET", "HEAD"):
                return self.send_json(handler, 405, {"error": "不支持的方法"})
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
            if path == "/" and "t" in query:
                # The command's link: the token becomes the cookie and leaves the address bar.
                if self.is_blocked(address):
                    return self.send_json(handler, 429, {"error": "口令错误太多，一分钟后再试"})
                headers = [("Location", "/")]
                if self.valid(state_dir, query["t"][0], address):
                    headers.append(("Set-Cookie", self.cookie(state_dir)))
                return self.send(handler, 303, b"", "text/plain; charset=utf-8", headers)
            name, kind = FILES[path]
            return self.send(handler, 200, (STATIC / name).read_bytes(), kind)
        if not path.startswith("/api/"):
            return self.send_json(handler, 404, {"error": "没有这个页面"})
        if self.is_blocked(address):
            return self.send_json(handler, 429, {"error": "口令错误太多，一分钟后再试"})
        body = None
        if method not in ("GET", "HEAD"):
            if handler.headers.get(HEADER) != "1":
                return self.send_json(handler, 403, {"error": f"缺少请求头 {HEADER}"})
            body, problem = self.read_body(handler)
            if problem:
                return self.send_json(handler, problem[0], {"error": problem[1]})
        if path == "/api/login" and method == "POST":
            if not self.valid(state_dir, str(body.get("token") or ""), address):
                return self.send_json(handler, 401, {"error": "口令不对"})
            return self.send_json(handler, 200, {"ok": True}, [("Set-Cookie", self.cookie(state_dir))])
        given = self.presented(handler)
        if not given:
            return self.send_json(handler, 401, {"error": "需要口令"})
        if not self.valid(state_dir, given, address):
            return self.send_json(handler, 401, {"error": "口令不对或已经换过"})
        routes = {("GET", "/api/config"): lambda: (200, config_view(state_dir)),
                  ("PUT", "/api/config"): lambda: self.save(state_dir, body),
                  ("POST", "/api/preview"): lambda: (200, preview(state_dir, body)),
                  ("GET", "/api/stats"): lambda: (200, stats_view(state_dir)),
                  ("POST", "/api/upstream/detect"): self.detect_now}
        if (method, path) not in routes:
            known = any(p == path for _, p in routes)
            return self.send_json(handler, 405 if known else 404, {"error": "不支持的方法" if known else "没有这个接口"})
        try:
            status, payload = routes[method, path]()
        except ValueError as exc:
            status, payload = 400, {"error": str(exc)}
        return self.send_json(handler, status, payload)

    @staticmethod
    def save(state_dir, body):
        try:
            save_config(state_dir, body)
        except OSError as exc:
            return 500, {"error": f"保存失败（{type(exc).__name__}）"}
        return 200, config_view(state_dir)

    def detect_now(self):
        """探测, off the request thread and bounded: the wrapper may first open its SSH tunnel."""
        if self.detect is None:
            return 503, {"error": "这个 Bridge 没有 Claude Harness，无法探测"}
        if not self.detecting.acquire(blocking=False):
            return 409, {"error": "上一次探测还没结束"}
        result = {}

        def work():
            try:
                result["value"] = self.detect()
            except (Unavailable, ValueError) as exc:
                result["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - reported by type only: it may carry what the wrapper printed.
                result["error"] = f"探测失败（{type(exc).__name__}）"
            finally:
                self.detecting.release()

        thread = threading.Thread(target=work, name="dashboard-detect", daemon=True)
        thread.start()
        thread.join(DETECT_TIMEOUT)
        if thread.is_alive():
            return 504, {"error": "探测超时，稍后再试"}
        if "error" in result:
            return 502, {"error": result["error"]}
        return 200, result["value"]

    # Access.

    @staticmethod
    def cookie(state_dir):
        return (f"{COOKIE}={dashboard_token(state_dir)}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={COOKIE_AGE}")

    @staticmethod
    def presented(handler):
        auth = handler.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        try:
            jar = http.cookies.SimpleCookie(handler.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        return jar[COOKIE].value if COOKIE in jar else ""

    def valid(self, state_dir, given, address):
        """Whether ``given`` is the token; a wrong one counts against ``address``."""
        if given and hmac.compare_digest(given.encode("utf-8"), dashboard_token(state_dir).encode("utf-8")):
            return True
        now = time.monotonic()
        with self.lock:
            if len(self.failures) > 1000:
                self.failures = {a: t for a, t in self.failures.items() if t and now - t[-1] < FAILURE_WINDOW}
            times = [t for t in self.failures.get(address, []) if now - t < FAILURE_WINDOW] + [now]
            self.failures[address] = times
            if len(times) >= FAILURES:
                self.blocked[address] = now + FAILURE_WINDOW
                self.failures.pop(address, None)
        return False

    def is_blocked(self, address):
        with self.lock:
            until = self.blocked.get(address)
            if until is not None and until <= time.monotonic():
                del self.blocked[address]
                until = None
        return until is not None

    # Bodies.

    @staticmethod
    def read_body(handler):
        """(parsed JSON object, None), or (None, (status, error))."""
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY or "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
            handler.close_connection = True
            return None, (413, "请求太大")
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return None, (400, "请求不是 JSON")
        if not isinstance(body, dict):
            return None, (400, "请求必须是一个 JSON 对象")
        return body, None

    def send_json(self, handler, status, payload, headers=()):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.send(handler, status, data, "application/json; charset=utf-8", headers)

    @staticmethod
    def send(handler, status, body, kind, headers=()):
        handler.send_response(status)
        for key, value in (*SECURITY, ("Content-Type", kind), ("Content-Length", str(len(body))), *headers):
            handler.send_header(key, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
        return status

"""Opt-in long-lived bot SDK for CardKit writes and message sends and reads; CLI remains the default.

Resolve the explicit profile and its existing credential in memory. No token or
secret is copied to manifests, logs, jobs, or SDK configuration files. Linux
keychain compatibility follows larksuite/cli internal/keychain/keychain_other.go.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

from covey.core import env as cenv
from covey.core import background, timing
from covey.core.process_env import env_flag

_clients = {}
_lock = threading.Lock()
POOL = 12  # Connections kept: the 看板's six link reads and the card lanes' writes at once reuse them.
IDLE_SECONDS = 240  # A pooled connection idle longer is not written to (S48: Feishu's edge kept them 600 s and more).
CONNECT_SECONDS = 3  # To open a connection; a request then has its 8 s.
DIRECT = {'http': None, 'https': None, 'all': None}  # requests: no proxy for this request, whatever the environment.
TOKEN_PATH = '/open-apis/auth/v3/tenant_access_token/internal'  # A self-built app's tenant token.
DIRECT_SWITCH = 'COVEY_FEISHU_DIRECT'  # 0/false/no/off: the SDK and lark-cli reach Feishu through the proxy.
API_HOSTS = {'feishu': 'open.feishu.cn', 'lark': 'open.larksuite.com'}  # Open-platform host by lark-cli brand.


def _core():
    """Import lark_oapi.core without lark_oapi/__init__.py, which imports every API module: 2.4 s, most of
    a task runner's first card write (S33); this takes about 0.2 s. The package runs its __init__ the
    first time anything reads another of its names."""
    if 'lark_oapi' not in sys.modules:
        spec = importlib.util.find_spec('lark_oapi')
        if spec is None or spec.loader is None:
            raise ImportError('lark_oapi')
        package = importlib.util.module_from_spec(spec)

        def complete(name):
            if package.__dict__.pop('__getattr__', None) is None:
                raise AttributeError(name)
            spec.loader.exec_module(package)
            return getattr(package, name)
        package.__getattr__ = complete
        sys.modules['lark_oapi'] = package
    import lark_oapi.core
    return lark_oapi.core


class _Client:
    """lark_oapi's ``Client`` (lark-oapi 1.7.4: its builder and ``request``) on lark_oapi.core alone, its
    requests on one pooled HTTPS session (S48). lark_oapi's ``Transport.execute`` opened a connection per
    request, TCP and TLS through the proxy: 55 ms of the 75-95 ms each call spent before Feishu's edge.

    - Direct to Feishu, not through the environment's proxy, unless ``COVEY_FEISHU_DIRECT=0``. When a
      direct connection cannot be opened (refused, unresolved, timed out: nothing was sent) the request
      goes through the proxy, as does every later one of this client.
    - A connection is kept while requests come within IDLE_SECONDS of each other; after a longer pause the
      session is replaced, so no request goes out on a connection idle for longer (Feishu's edge kept idle
      ones 600 s and more, S48). A request that fails on a reused connection is not sent again: it may have
      reached Feishu (``network``), like any other.
    - The tenant token (every request's: the Bridge asks for no other) is fetched on that session too, as
      lark-oapi 1.7.4's TokenManager fetches a self-built app's and with its expiry (10 minutes early), by
      one thread at a time: a card write waits for ``warm``'s fetch rather than starting its own. Nothing
      here imports lark_oapi.core.http or .token: they load httpx and requests_toolbelt, unused (S48 B2)."""

    def __init__(self, app_id, secret, domain):
        core = _core()
        from lark_oapi.core.model import Config
        from lark_oapi.core.utils.user_agent import build_user_agent
        self.config = Config()
        self.config.app_id, self.config.app_secret, self.config.domain = app_id, secret, domain
        self.config.timeout, self.config.log_level = 8, core.LogLevel.ERROR
        core.logger.setLevel(int(core.LogLevel.ERROR.value))
        self.agent = build_user_agent()
        self.direct = env_flag(DIRECT_SWITCH, True)
        self.session, self.used = None, 0.0
        self.session_lock, self.token_lock = threading.Lock(), threading.Lock()
        self.token, self.token_until = '', 0.0

    def _session(self):
        import requests
        from http.cookiejar import DefaultCookiePolicy
        with self.session_lock:
            now = time.monotonic()
            old = self.session if self.session is not None and now - self.used > IDLE_SECONDS else None
            if self.session is None or old is not None:
                self.session = requests.Session()
                self.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # No cookies kept or sent.
                self.session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=POOL))
            self.used = now
            session = self.session
        if old is not None:
            old.close()  # Idle: no request of it is in flight.
        return session

    def send(self, method, url, **kwargs):
        """One HTTPS request on this client's session: requests' response."""
        import requests
        from urllib3.exceptions import ConnectTimeoutError
        kwargs.setdefault('timeout', (CONNECT_SECONDS, self.config.timeout))
        if self.direct:
            try:
                return self._session().request(method, url, proxies=DIRECT, **kwargs)
            except requests.exceptions.ConnectionError as exc:
                # Refused or unresolved (NewConnectionError) and timed out alike: no connection, nothing sent.
                if not isinstance(getattr(exc.args[0] if exc.args else None, 'reason', None), ConnectTimeoutError):
                    raise
                self.direct = False
        return self._session().request(method, url, **kwargs)

    def tenant_token(self):
        """The tenant token, fetched when there is none or it is within 10 minutes of expiring. The caller
        holds ``token_lock``. Raises ObtainAccessTokenException when Feishu refuses (``authentication``)."""
        if self.token and time.time() < self.token_until:
            return self.token
        from lark_oapi.core import JSON
        from lark_oapi.core.const import USER_AGENT, UTF_8
        from lark_oapi.core.exception import ObtainAccessTokenException
        body = JSON.marshal({'app_id': self.config.app_id, 'app_secret': self.config.app_secret}).encode(UTF_8)
        response = self.send('POST', self.config.domain + TOKEN_PATH, headers={USER_AGENT: self.agent}, data=body)
        payload = json.loads(response.content)
        if payload.get('code') != 0 or not payload.get('tenant_access_token'):
            raise ObtainAccessTokenException('obtain self tenant access token failed', payload.get('code'),
                                             payload.get('msg'))
        self.token = payload['tenant_access_token']
        self.token_until = time.time() + int(payload.get('expire') or 0) - 10 * 60
        return self.token

    def warm(self):
        """Ready the first request (S48: a task runner's first card write took 330 ms more than its later
        ones): load requests and fetch the tenant token, which opens the connection the request goes on.
        Whether it went through; a failure is the first request's to meet again."""
        try:
            with self.token_lock:
                self.tenant_token()
            return True
        except Exception:  # noqa: BLE001 - never a token or URL in a log
            return False

    def request(self, request):
        """lark_oapi 1.7.4's ``Client.request`` for the Bridge's requests: a path with no placeholders, the
        tenant token; no Content-Type, as its ``Transport.execute`` sent none."""
        from lark_oapi.core import JSON
        from lark_oapi.core.const import APPLICATION_JSON, AUTHORIZATION, CONTENT_TYPE, USER_AGENT, UTF_8
        from lark_oapi.core.model import BaseResponse, RawResponse
        with self.token_lock:
            token = self.tenant_token()
        headers = {**request.headers, USER_AGENT: self.agent, AUTHORIZATION: f'Bearer {token}'}
        data = None if request.body is None else JSON.marshal(request.body).encode(UTF_8)
        sent = self.send(request.http_method.name, self.config.domain + request.uri, headers=headers,
                         params=request.queries, data=data)
        raw = RawResponse()
        raw.status_code, raw.headers, raw.content = sent.status_code, dict(sent.headers), sent.content
        response = BaseResponse()
        content_type = raw.headers.get(CONTENT_TYPE)
        if content_type is not None and content_type.startswith(APPLICATION_JSON):
            response = JSON.unmarshal(str(raw.content, UTF_8), BaseResponse)
        elif 200 <= raw.status_code < 300:
            response.code = 0
        response.raw = raw
        return response


def _private_read(path):
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('credential storage permissions invalid')
    return path.read_bytes()


def api_host(profile):
    """The open-platform host of ``profile``'s app, by its brand in lark-cli's config; Feishu's when unknown.
    Reads no credential."""
    try:
        config = json.loads(_private_read(Path.home() / '.lark-cli/config.json'))
        brand = next(a.get('brand') for a in config.get('apps', []) if a.get('name') == profile)
    except (OSError, ValueError, StopIteration):
        brand = None
    return API_HOSTS.get(brand, API_HOSTS['feishu'])


def lark_cli_direct(profile, log, environ=None, connect=socket.create_connection):
    """Let lark-cli reach Feishu's open platform without the proxy (S48: a lark-cli call took 615 ms through
    it, 432 ms direct): the host joins this process's no_proxy and NO_PROXY, which every child inherits (each
    lark-cli call, the event consumers' endpoint lookups, task runners and their tasks). Only when that host
    answers a direct connection now, and DIRECT_SWITCH is not off; otherwise the environment stays as it was.
    An event stream's own host is not this one: it keeps the proxy. Whether the host was added."""
    environ = os.environ if environ is None else environ
    if not env_flag(DIRECT_SWITCH, True):
        return False
    host = api_host(profile)
    try:
        connect((host, 443), timeout=CONNECT_SECONDS).close()
    except OSError as exc:
        log(f'Feishu direct unavailable ({type(exc).__name__}); lark-cli keeps the proxy')
        return False
    # Both spellings: Go (lark-cli) reads NO_PROXY first, Python no_proxy; an empty one set on purpose stays theirs.
    for name in [n for n in ('no_proxy', 'NO_PROXY') if n in environ] or ['no_proxy', 'NO_PROXY']:
        hosts = [h.strip() for h in environ.get(name, '').split(',') if h.strip()]
        if host not in hosts:
            environ[name] = ','.join(hosts + [host])
    log(f'Feishu direct: lark-cli reaches {host} without the proxy')
    return True


def client(profile):
    if not profile or profile == 'default':
        raise ValueError('SDK requires an explicitly named bot profile')
    config_path = Path.home() / '.lark-cli/config.json'
    config = json.loads(_private_read(config_path))
    matches = [a for a in config.get('apps', []) if a.get('name') == profile]
    if len(matches) != 1:
        raise ValueError('SDK profile unavailable')
    app = matches[0]
    ref = app.get('appSecret')
    if not re.fullmatch(r'cli_[A-Za-z0-9]+', str(app.get('appId', ''))):
        raise ValueError('SDK app identity invalid')
    if ref != {'source': 'keychain', 'id': 'appsecret:' + app['appId']}:
        raise ValueError('SDK credential provider unsupported')
    folder = Path(os.environ.get('LARKSUITE_CLI_DATA_DIR', str(Path.home() / '.local/share'))) / 'lark-cli'
    path = folder / (re.sub(r'[^a-zA-Z0-9._-]', '_', ref['id']) + '.enc')
    version = (app['appId'], app.get('brand'), config_path.stat().st_mtime_ns,
               path.stat().st_mtime_ns, (folder / 'master.key').stat().st_mtime_ns)
    with _lock:
        if profile in _clients and _clients[profile][0] == version:
            return _clients[profile][1]
        core = _core()
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        data = _private_read(path)
        try:
            secret = AESGCM(_private_read(folder / 'master.key')).decrypt(data[:12], data[12:], None).decode()
        except InvalidTag:
            # A rotated master key or a rewritten secret: the CLI still reads its own store.
            raise ValueError('SDK credential unreadable') from None
        domain = {'feishu': core.FEISHU_DOMAIN, 'lark': core.LARK_DOMAIN}.get(app.get('brand'))
        if not domain:
            raise ValueError('SDK brand unsupported')
        value = _Client(app['appId'], secret, domain)
        _clients[profile] = (version, value)
        timing.mark('sdk_client')  # Built once per process: lark_oapi.core's import and the credential's decryption.
        return value


def enabled():
    """``COVEY_CARD_TRANSPORT=sdk``. The name predates messages going this way too."""
    return cenv.get('COVEY_CARD_TRANSPORT') == 'sdk'


def runner_or_none(profile, method, path, data, params=None, *, log=lambda _: None, label='CardKit',
                   cli_errors=False):
    """``runner(...)`` when the SDK transport is on; None (spawn lark-cli) when it is off or this
    process cannot build a client. No request has started either way."""
    if not enabled():
        return None
    try:
        return runner(profile, method, path, data, params, cli_errors=cli_errors)
    except (OSError, ValueError, ImportError):
        # Keep working with the CLI's credential providers on platforms the optional adapter cannot use.
        log(f'{label} SDK unavailable locally; using CLI')
        return None


# lark-cli v1.0.98 internal/errclass/codemeta.go: the error type of the codes a message call can get.
_CLI_ERROR_TYPES = {
    **dict.fromkeys((99991661, 99991671, 99991668, 99991663, 99991677, 20026, 20037, 20064, 20073, 20050),
                    'authentication'),
    **dict.fromkeys((99991672, 99991676, 99991679, 230027, 99991673, 99991662), 'authorization'),
    **dict.fromkeys((99991543, 10014), 'config'), **dict.fromkeys((21000, 21001), 'policy'),
}


def _cli_error(status, content):
    """The error lark-cli's own commands report for this response, None for a success
    (v1.0.98 errclass.BuildAPIError, then the HTTP status only when the body has no code)."""
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = payload.get('code')
        code = int(code) if isinstance(code, (int, float)) and not isinstance(code, bool) else 0
        if code:
            block = payload.get('error') if isinstance(payload.get('error'), dict) else {}
            message = block.get('message') if isinstance(block.get('message'), str) else ''
            message = message or (payload.get('msg') if isinstance(payload.get('msg'), str) else '')
            return {'type': _CLI_ERROR_TYPES.get(code, 'api'), **({'subtype': 'rate_limit'} if code == 99991400 else {}),
                    'code': code, 'message': (message or f'API error: [{code}]')[:500]}
        if status < 400:
            return None
    elif status < 400:
        return {'type': 'internal', 'subtype': 'invalid_response'}  # The bridge reads this as unconfirmed.
    if status >= 500:
        return {'type': 'network', 'subtype': 'server_error', 'code': status}
    return {'type': 'api', 'subtype': 'not_found' if status == 404 else 'unknown', 'code': status}


def _answered(argv, raw, returncode, stdout, stderr):
    """A request Feishu answered, with its own milliseconds (``Server-Timing``) for core/timing.py."""
    done = subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    done.served = timing.served_ms(getattr(raw, 'headers', None) or {})
    return done


def runner(profile, method, path, data, params=None, *, cli_errors=False):
    """Resolve credentials BEFORE a request; only this step may fall back to CLI. ``cli_errors``:
    a failed response is classified as lark-cli classifies it (message calls replacing it)."""
    sdk = client(profile)
    from lark_oapi.core.model import BaseRequest
    from lark_oapi.core import AccessTokenType, HttpMethod

    def execute(argv, **kwargs):
        request = (BaseRequest.builder().http_method(HttpMethod[method]).uri(path)
                   .token_types({AccessTokenType.TENANT}).body(data).build())
        for key, value in (params or {}).items():
            request.add_query(key, value)
        try:
            response = sdk.request(request)
            status = response.raw.status_code
            if cli_errors:
                error = _cli_error(status, response.raw.content)
                if error is None:
                    return _answered(argv, response.raw, 0, json.dumps(json.loads(response.raw.content)), '')
                return _answered(argv, response.raw, 1, '', json.dumps({'ok': False, 'error': error}))
            payload = json.loads(response.raw.content)
            if response.code == 0 and 200 <= status < 300:
                return _answered(argv, response.raw, 0, json.dumps(payload), '')
            # Structured safe fields only. Preserve Retry-After for the outbox. Feishu's
            # own short message ("elementID format error…") says why, as lark-cli reports it.
            return _answered(argv, response.raw, 1, '', json.dumps({'ok': False, 'error': {
                'type': 'api', 'code': response.code, 'message': str(getattr(response, 'msg', '') or '')[:160],
                'status_code': status, 'retry_after': response.raw.headers.get('Retry-After', 0)}}))
        except Exception as exc:
            # Never include SDK exception text: auth/HTTP errors can contain URLs.
            from lark_oapi.core.exception import ObtainAccessTokenException
            payload = {'ok': False, 'error': {'type': 'authentication' if isinstance(exc, ObtainAccessTokenException)
                       else 'network'}}
        return subprocess.CompletedProcess(argv, 1, '', json.dumps(payload))
    execute.via = 'sdk'  # How core/timing.py names this transport.
    return execute


def warm_sdk(profile, log):
    """Build a task runner's SDK client (app/runner.py) on a background thread while its job starts (the Bridge
    builds its own on the main thread at its start and only warms it on a thread, app/unified.py):
    built on the first card write instead, it held the task card's 执行中 and the backend's start
    for 2.7-3.7 s (S32). ``client`` builds under its lock, so a card write meanwhile waits for this
    one. Then ``warm`` it: its HTTP stack, a connection and the tenant token, 330 ms of the first
    card write before (S48). A failure changes nothing: the first card write builds it again or uses
    the CLI. Returns the thread; None when CardKit does not use the SDK."""
    if not enabled():
        return None

    def warm():
        try:
            client(profile).warm()
        except Exception as exc:  # noqa: BLE001 - never the task's failure; its text may name paths
            log(f"CardKit SDK warmup unavailable: {type(exc).__name__}")
    thread = threading.Thread(target=warm, daemon=True, name=background.WARMUP)
    try:
        thread.start()
    except RuntimeError:  # No thread to spare: the first card write builds it.
        return None
    return thread

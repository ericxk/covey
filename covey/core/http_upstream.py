"""Requests a local relay or gateway sends on to its upstream (an extension's Claude Code relay, backends/acme,
an extension's model catalog read), and the answer it passes back.

They never follow a redirect: a token, key or body only goes to the address it was meant for, and a 3xx is an
answer like any other error status.
"""
from __future__ import annotations

import threading

# urllib.request (with ssl and the mail parser behind it) is imported on the first request, not with the module:
# a task runner builds every Harness's backend, which imports this, and rarely sends one.


class _Opener:
    """An opener that never follows a redirect, built on first use; through the environment's proxy unless ``direct``."""

    def __init__(self, direct):
        self.direct = direct
        self.built = None
        self.lock = threading.Lock()

    def open(self, *args, **kwargs):
        if self.built is None:
            with self.lock:
                if self.built is None:
                    import urllib.request

                    class _NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *args, **kwargs):
                            return None

                    self.built = urllib.request.build_opener(
                        *([urllib.request.ProxyHandler({})] if self.direct else []), _NoRedirect())
        return self.built.open(*args, **kwargs)


def opener(*, direct=False):
    """An opener that never follows a redirect; through the environment's proxy unless ``direct``."""
    return _Opener(direct)


def send(opener, method, url, body, headers, timeout):
    """(status, response) of one request; an error status is a response like any other. OSError when no
    answer came."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    return (response.status if hasattr(response, "status") else response.code), response


class KeepAlive:
    """HTTPS requests over kept connections, idle ones pooled by host and taken by whichever thread asks next (the web page's
    operations run in short-lived lane threads), through the environment's proxy (a CONNECT tunnel) unless the host is one it
    bypasses, so a client that asks every half second does not shake hands each time: 2026-11-25 T765, the web page's /openapi
    from the dev machine, a new connection p50 203 ms (one of ten 1405 ms), a kept one 137 ms. Never follows a redirect, as
    ``opener``. A request that fails because a kept connection was closed while idle is sent once more on a new one; the callers'
    requests are idempotent (the web page's inbox, ack and upsert)."""

    IDLE = 4  # Idle connections kept per host.

    def __init__(self, factory=None, direct=False):
        self.lock = threading.Lock()
        self.idle = {}  # (host, port) -> [connection], the newest last
        self.factory = factory  # (host, port, timeout) -> a connection; None: HTTPS, through the proxy as above (tests pass plain HTTP).
        self.direct = direct  # Never through the environment's proxy.

    def send(self, method, url, body, headers, timeout):
        """(status, body bytes) of one request. OSError when no answer came."""
        import http.client
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        key = (parts.hostname, parts.port or 443)
        for attempt in (0, 1):
            conn, used = self.take(key, timeout)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (OSError, http.client.HTTPException) as exc:
                self.discard(conn)
                closed = isinstance(exc, (ConnectionError, http.client.BadStatusLine, http.client.CannotSendRequest))
                if attempt == 0 and used and closed:
                    continue  # Closed by the other side while idle: once more on a new connection (not after a timeout).
                raise exc if isinstance(exc, OSError) else OSError(type(exc).__name__) from exc
            if response.will_close:
                self.discard(conn)
            else:
                self.give_back(key, conn)
            return response.status, data
        raise OSError("no answer")  # Not reached: the second attempt returns or raises.

    def take(self, key, timeout):
        """(an idle connection to ``key`` or a new one, whether it has carried a request already)."""
        with self.lock:
            pool = self.idle.get(key)
            if pool:
                return pool.pop(), True
        return self.connect(key, timeout), False

    def give_back(self, key, conn):
        with self.lock:
            pool = self.idle.setdefault(key, [])
            pool.append(conn)
            extra = pool[:-self.IDLE] if len(pool) > self.IDLE else []
            del pool[:len(extra)]
        for each in extra:
            self.discard(each)

    def connect(self, key, timeout):
        import http.client
        import ssl
        import urllib.request
        host, port = key
        if self.factory is not None:
            return self.factory(host, port, timeout)
        proxy = None if self.direct else urllib.request.getproxies().get("https")
        if proxy and not urllib.request.proxy_bypass(host):
            from urllib.parse import unquote, urlsplit
            p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=timeout, context=ssl.create_default_context())
            tunnel = {}
            if p.username:
                import base64
                pair = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
                tunnel["Proxy-Authorization"] = "Basic " + base64.b64encode(pair).decode()
            conn.set_tunnel(host, port, headers=tunnel)
            return conn
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())

    @staticmethod
    def discard(conn):
        try:
            conn.close()
        except OSError:
            pass

    def close(self):
        """Every idle connection closed."""
        with self.lock:
            pools, self.idle = self.idle, {}
        for pool in pools.values():
            for conn in pool:
                self.discard(conn)


def copy_headers(handler, response, drop):
    """``response``'s headers in their order, but those whose lower-case name is in ``drop``, sent on ``handler``."""
    for key, value in response.headers.items():
        if key.lower() not in drop:
            handler.send_header(key, value)


def pipe(handler, response, *, held=b"", transform=None):
    """``held``, then the rest of ``response`` as it arrives, written to ``handler`` and flushed piece by piece.
    With ``transform`` (``feed(chunk)``, ``close()``), what it makes of each piece and, last, its ``close()``."""
    if held:
        handler.wfile.write(held)
        handler.wfile.flush()
    while True:
        chunk = response.read1(65536)
        if not chunk:
            break
        out = transform.feed(chunk) if transform else chunk
        if out:
            handler.wfile.write(out)
            handler.wfile.flush()
    if transform:
        handler.wfile.write(transform.close())
        handler.wfile.flush()

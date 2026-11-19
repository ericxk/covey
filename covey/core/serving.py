"""The thread a local HTTP server serves on: each turn's relay or gateway
(Claude redaction, Acme) starts its own here, and so do the tests' fake upstreams.

``serve_forever`` looks for a ``shutdown`` request once per poll, and ``shutdown`` blocks until it
does: with the standard half-second poll every stop waited up to 0.5 s. ``POLL_INTERVAL`` is 0.05 s;
a request is served as soon as it arrives either way, the poll only bounds the stop.

``LoopbackServer`` is the server the relays and gateways listen with: HTTP/1.1 on a free 127.0.0.1 port, each
request handed to one function, served on a named thread while the server is entered.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POLL_INTERVAL = 0.05


def serve_thread(server, name=None):
    """A daemon thread, not yet started, that serves ``server`` until ``server.shutdown()``."""
    return threading.Thread(target=server.serve_forever, kwargs={"poll_interval": POLL_INTERVAL},
                            name=name, daemon=True)


def content_length_body(handler):
    """The request body its Content-Length announces; b"" without one."""
    return handler.rfile.read(int(handler.headers.get("Content-Length") or 0))


class LoopbackServer(ThreadingHTTPServer):
    """An HTTP/1.1 server on a free 127.0.0.1 port that hands each request to ``handle(handler, body)``: GET and
    HEAD with body None, the other ``verbs`` with ``read_body(handler)``; a verb not in ``verbs`` is answered 501.
    Request lines are not logged (headers carry tokens). Entering it starts serving on a daemon thread named
    ``name``; leaving it stops and closes the server."""

    def __init__(self, handle, name, *, read_body=content_length_body, verbs=("GET", "POST")):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # Headers carry tokens.

        def without_body(handler):
            handle(handler, None)

        def with_body(handler):
            handle(handler, read_body(handler))

        for verb in verbs:
            setattr(Handler, "do_" + verb, without_body if verb in ("GET", "HEAD") else with_body)
        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = serve_thread(self, name)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.shutdown()
        self.server_close()

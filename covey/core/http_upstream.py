"""Requests a local relay or gateway sends on to its upstream (backends/claude/redaction.py, backends/acme,
an extension's model catalog read), and the answer it passes back.

They never follow a redirect: a token, key or body only goes to the address it was meant for, and a 3xx is an
answer like any other error status.
"""
from __future__ import annotations

import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def opener(*, direct=False):
    """An opener that never follows a redirect; through the environment's proxy unless ``direct``."""
    return urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if direct else []), _NoRedirect())


def send(opener, method, url, body, headers, timeout):
    """(status, response) of one request; an error status is a response like any other. OSError when no
    answer came."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    return (response.status if hasattr(response, "status") else response.code), response


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

"""Environment variable names: ``COVEY_<X>`` is the name, ``LARK_BRIDGE_<X>`` the old one that still works.

Code asks for a variable by either spelling (``get("COVEY_DATA_HOME")`` or ``get("LARK_BRIDGE_DATA_HOME")``);
``get`` reads ``COVEY_<X>`` first, then ``LARK_BRIDGE_<X>``. Reading only the old one from the process
environment logs one deprecation line per name to stderr. The Manager mirrors both spellings into every service it
starts (``mirrored``), so a service never warns about what its own manifest holds, and a task process of an older
release (which reads only the old names) still gets them. Manifests keep the old spelling (``canonical``) in this
release: an upgrade changes no manifest, a rollback finds the names the older release reads, and a deploy
comparison of a manifest with ``runtime_environment()`` cannot report a renamed variable as a change.

Standard library only (task processes and an extension's isolated interpreter import this).
"""
import os
import sys

NEW_PREFIX = "COVEY_"
OLD_PREFIX = "LARK_BRIDGE_"
_warned = set()


def spellings(name):
    """``(new, old)`` for a name written in either spelling; ``(name, name)`` for any other variable."""
    if name.startswith(NEW_PREFIX):
        return name, OLD_PREFIX + name[len(NEW_PREFIX):]
    if name.startswith(OLD_PREFIX):
        return NEW_PREFIX + name[len(OLD_PREFIX):], name
    return name, name


def _warn(old):
    if old not in _warned:
        _warned.add(old)
        print(f"{old} is the old name of {spellings(old)[0]}: set {spellings(old)[0]} instead "
              f"(the old name still works)", file=sys.stderr, flush=True)


def get(name, default=None, environ=None):
    """The value under either spelling (the new one wins), else ``default``. ``environ`` defaults to os.environ."""
    environ = os.environ if environ is None else environ
    new, old = spellings(name)
    if new in environ:
        return environ[new]
    if old != new and old in environ:
        if environ is os.environ:
            _warn(old)
        return environ[old]
    return default


def has(name, environ=None):
    return get(name, None, environ) is not None


def canonical(environ):
    """A copy with every ``COVEY_<X>`` renamed to ``LARK_BRIDGE_<X>`` (the new spelling's value wins)."""
    result = {k: v for k, v in environ.items() if not k.startswith(NEW_PREFIX)}
    result.update({OLD_PREFIX + k[len(NEW_PREFIX):]: v for k, v in environ.items() if k.startswith(NEW_PREFIX)})
    return result


def mirrored(environ):
    """A copy where each variable set under one spelling is also set under the other (the new one wins)."""
    result = dict(environ)
    for key in list(environ):
        new, old = spellings(key)
        if key.startswith((NEW_PREFIX, OLD_PREFIX)):
            result[new] = environ[new] if new in environ else environ[old]
            result[old] = result[new]
    return result


def put(environ, name, value):
    """Set ``name`` under both spellings in ``environ`` (os.environ, or a dict for a child process)."""
    new, old = spellings(name)
    environ[new] = value
    environ[old] = value

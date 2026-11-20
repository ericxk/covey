"""Environment variable names: ``COVEY_<X>`` is the name, ``LARK_BRIDGE_<X>`` the old one that still works.

Code asks for a variable by either spelling (``get("COVEY_DATA_HOME")`` or ``get("LARK_BRIDGE_DATA_HOME")``);
``get`` reads ``COVEY_<X>`` first, then ``LARK_BRIDGE_<X>``. Reading only the old one from the process
environment logs one deprecation line per name to stderr. The Manager mirrors every spelling into every service it
starts (``mirrored``), so a service never warns about what its own manifest holds, and a task process of an older
release (which reads only the old names) still gets them. Manifests keep the old spelling (``canonical``) in this
release: an upgrade changes no manifest, a rollback finds the names the older release reads, and a deploy
comparison of a manifest with ``runtime_environment()`` cannot report a renamed variable as a change.

Some variables changed their name as well as their prefix (``RENAMED``: the main thread became the
butler). Such a variable has three spellings, in the order ``get`` reads them: ``COVEY_<new>``, ``COVEY_<old>``,
``LARK_BRIDGE_<old>``; ``put`` and ``mirrored`` set all three, and ``canonical`` keeps ``LARK_BRIDGE_<old>``, the
one every older release reads.

Standard library only (task processes and an extension's isolated interpreter import this).
"""
import os
import sys

NEW_PREFIX = "COVEY_"
OLD_PREFIX = "LARK_BRIDGE_"
# Variables whose name changed along with the prefix: new name -> old name (without prefix), from when the main
# thread became the butler. The old tails are what every older release reads, so manifests keep them (``canonical``).
RENAMED = {
    "BUTLER": "MAIN_THREAD",
    "BUTLER_RULES": "MAIN_BUTLER",
    "BUTLER_TRACKS": "MAIN_TRACKS",
    "BUTLER_TRACKS_MAX": "MAIN_TRACKS_MAX",
    "DISPATCH_CARD": "MAIN_DISPATCH_CARD",
    "BUTLER_JOB": "MAIN_JOB",
    "BUTLER_STATE": "MAIN_STATE",
}
_BY_OLD = {old: new for new, old in RENAMED.items()}
# Settings born after the rename (devices, patrol, projects, the track judge): they never had a LARK_BRIDGE_ name. The
# Manager manifest still stores them as ``LARK_BRIDGE_<X>`` (``canonical``), so reading that spelling is no sign of an
# old configuration and says nothing. Prefixes of the name without the COVEY_ / LARK_BRIDGE_ prefix.
BORN_NEW = ("DEVICES", "NODE", "PATROL", "PROGRESS", "PROJECT", "TRACK_JUDGE", "WEB")
_warned = set()


def spellings(name):
    """Every spelling of a variable, the one ``get`` reads first first: ``COVEY_<new>``, ``COVEY_<old>``,
    ``LARK_BRIDGE_<old>`` for a RENAMED one, else ``COVEY_<X>``, ``LARK_BRIDGE_<X>``. Written in any of them; any
    other variable is ``(name,)``."""
    for prefix in (NEW_PREFIX, OLD_PREFIX):
        if name.startswith(prefix):
            tail = name[len(prefix):]
            break
    else:
        return (name,)
    new = _BY_OLD.get(tail, tail)
    old = RENAMED.get(new, new)
    names = [NEW_PREFIX + new]
    if old != new:
        names.append(NEW_PREFIX + old)
    names.append(OLD_PREFIX + old)
    return tuple(names)


def _has_old_name(new):
    tail = new[len(NEW_PREFIX):] if new.startswith(NEW_PREFIX) else new
    return not tail.startswith(BORN_NEW)


def _warn(old, new):
    if old not in _warned and _has_old_name(new):
        _warned.add(old)
        print(f"{old} is the old name of {new}: set {new} instead (the old name still works)", file=sys.stderr, flush=True)


def get(name, default=None, environ=None):
    """The value under the first spelling set (see ``spellings``), else ``default``. ``environ`` defaults to os.environ."""
    environ = os.environ if environ is None else environ
    names = spellings(name)
    for spelling in names:
        if spelling in environ:
            if spelling != names[0] and environ is os.environ:
                _warn(spelling, names[0])
            return environ[spelling]
    return default


def has(name, environ=None):
    return get(name, None, environ) is not None


def _rank(key, names):
    return names.index(key) if key in names else len(names)


def canonical(environ):
    """A copy with every ``COVEY_<X>`` renamed to its ``LARK_BRIDGE_`` spelling, the old name's for a RENAMED one (the
    spelling ``get`` reads first wins)."""
    result, ranks = {}, {}
    for key, value in environ.items():
        names = spellings(key)
        canon, rank = names[-1], _rank(key, names)
        if canon not in ranks or rank < ranks[canon]:
            result[canon], ranks[canon] = value, rank
    return result


def mirrored(environ):
    """A copy where each variable set under one spelling is also set under every other (the first one set wins)."""
    result = dict(environ)
    for key in list(environ):
        names = spellings(key)
        if len(names) > 1 and key in names:
            value = next(environ[spelling] for spelling in names if spelling in environ)
            result.update(dict.fromkeys(names, value))
    return result


def put(environ, name, value):
    """Set ``name`` under every spelling in ``environ`` (os.environ, or a dict for a child process)."""
    for spelling in spellings(name):
        environ[spelling] = value

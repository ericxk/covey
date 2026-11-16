"""方向标签 (docs/butler.md 方向标签), when COVEY_DIRECTIONS=1: which of the owner's directions a piece of work belongs to,
shown as one grey tag on the 派发卡 and the 专项卡 and kept as ``direction`` {name, source} in their records.

``<state>/directions.json`` holds ``{"directions": [{name, aliases[], repos[], status}]}`` (a bare list reads too):

- ``status`` is ``candidate`` for a name that came up once and ``active`` from its second use on; only an ``active`` one is
  drawn on a card. A name the owner wrote into the file by hand may be ``active`` at once.
- ``repos``: an absolute path (a work directory at or under it belongs to the direction) or a repository name such as
  ``owner/repo`` (a path with it as whole components does). The work directory alone never makes a direction: a name that
  is in no entry is not found by it, only an entry's own ``repos`` match it.

What decides, highest first (``resolve``): the owner said which direction it is (``owner``); the work directory or the
repository matches an entry's ``repos`` (``repo``); the butler declared one when it opened the topic or the project
(``butler``); none, and nothing is drawn. ``claim`` counts the use: a new name is written as a candidate, a candidate used
under another key (a request, a project) becomes active.

Standard library and core only: the MCP server's tool list reads the names (core/butler_tool_specs.py)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from covey.core import env as cenv
from covey.core.locks import resource_lock
from covey.core.state import read_json, write_json

ENV = "COVEY_DIRECTIONS"  # ``1``: on; anything else, unset too: off.
FILE = "directions.json"
CANDIDATE, ACTIVE = "candidate", "active"
OWNER, REPO, BUTLER = "owner", "repo", "butler"  # ``source`` of a direction.
SOURCES = (OWNER, REPO, BUTLER)
NAME_CHARS = 24
MAX_ENTRIES = 200
LISTED = 30  # Names the butler's tool description lists.

_cache: dict = {}
_cache_lock = threading.Lock()


def enabled() -> bool:
    """Whether COVEY_DIRECTIONS=1 (only ``1``; read on every call)."""
    return str(cenv.get(ENV, "")).strip() == "1"


def file_path(state_dir) -> Path:
    return Path(state_dir) / FILE


def clean(name) -> str:
    """A direction's name as it is kept: one line, at most NAME_CHARS."""
    return " ".join(str(name or "").split())[:NAME_CHARS]


def same(a, b) -> bool:
    return bool(a) and clean(a).casefold() == clean(b).casefold()


def _entry(raw) -> dict | None:
    if not isinstance(raw, dict) or not clean(raw.get("name")):
        return None
    texts = lambda value: [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []  # noqa: E731
    uses = [str(item) for item in raw.get("uses") or () if isinstance(item, (str, int)) and str(item)][:2]
    return {"name": clean(raw["name"]), "aliases": [clean(item) for item in texts(raw.get("aliases")) if clean(item)],
            "repos": texts(raw.get("repos")), "status": ACTIVE if raw.get("status") == ACTIVE else CANDIDATE, "uses": uses}


def load(state_dir) -> list:
    """The entries of the file, well formed ([] for a file that is missing or not readable). A read is repeated only when the
    file's stamp changed."""
    path = file_path(state_dir)
    try:
        stat = path.stat()
    except OSError:
        return []
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _cache_lock:
        kept = _cache.get(str(path))
    if kept is not None and kept[0] == stamp:
        return [dict(entry) for entry in kept[1]]
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return []
    rows = data.get("directions") if isinstance(data, dict) else data
    entries = [entry for entry in map(_entry, rows) if entry] if isinstance(rows, list) else []
    with _cache_lock:
        _cache[str(path)] = (stamp, entries)
    return [dict(entry) for entry in entries]


def find(entries, name) -> dict | None:
    """The entry that ``name`` is (its name or an alias, not told apart by case); None for a name nobody has."""
    return next((entry for entry in entries if same(name, entry["name"]) or any(same(name, alias) for alias in entry["aliases"])),
                None)


def by_repo(entries, workdir) -> dict | None:
    """The entry whose ``repos`` the work directory ``workdir`` is in (``repos`` in the module's words); None for none."""
    path = os.path.normpath(str(workdir or "").strip()) if str(workdir or "").strip() else ""
    if not path or not path.startswith("/"):
        return None
    for entry in entries:
        for repo in entry["repos"]:
            if repo.startswith("/"):
                base = os.path.normpath(repo)
                if path == base or path.startswith(base.rstrip("/") + "/"):
                    return entry
            else:
                wanted = "/" + repo.strip("/") + "/"
                if wanted in path + "/":
                    return entry
    return None


def named(entries, name, source) -> dict:
    """{name, source} for ``name``: the entry's own name when ``name`` is one's name or alias, else the name as given."""
    hit = find(entries, name)
    return {"name": hit["name"] if hit else clean(name), "source": source}


def resolve(entries, *, said="", workdir="", declared="") -> dict | None:
    """{name, source} of the direction the work belongs to, or None. ``said``: the name the owner gave; ``declared``: the
    butler's own. A name in an entry is that entry's own name, whatever alias was given."""
    if clean(said):
        return named(entries, said, OWNER)
    hit = by_repo(entries, workdir)
    if hit is not None:
        return {"name": hit["name"], "source": REPO}
    return named(entries, declared, BUTLER) if clean(declared) else None


def claim(state_dir, key, *, said="", workdir="", declared="") -> dict | None:
    """``resolve`` against the file, and the use counted under ``key`` (a request or a project id: the same key again counts
    nothing): a name no entry has is written as a candidate, a candidate used under a second key becomes active. {name, source}
    for what the record keeps (drawn only once the name is active, ``shown``); None without the switch or a direction."""
    if not enabled() or not state_dir:
        return None
    found = resolve(load(state_dir), said=said, workdir=workdir, declared=declared)
    if found is None:
        return None
    key, path = str(key or ""), file_path(state_dir)
    with resource_lock("record", str(path)):
        entries = load(state_dir)
        entry = find(entries, found["name"])
        if entry is None:
            if len(entries) >= MAX_ENTRIES:
                return found
            entries.append({"name": found["name"], "aliases": [], "repos": [], "status": CANDIDATE, "uses": [key] if key else []})
        elif entry["status"] == CANDIDATE and key and key not in entry["uses"]:
            entry["uses"] = [*entry["uses"], key][:2]
            if len(entry["uses"]) >= 2:
                entry["status"] = ACTIVE
        else:
            return found
        write_json(path, {"directions": entries}, ensure_ascii=False, indent=1)
    return found


def shown(state_dir, direction) -> str:
    """The name a card draws for the record's ``direction`` ({name, source}): the name when it is ``active`` in the file, else
    ''. '' without the switch."""
    if not enabled() or not isinstance(direction, dict) or not state_dir:
        return ""
    entry = find(load(state_dir), direction.get("name"))
    return entry["name"] if entry is not None and entry["status"] == ACTIVE else ""


def known(state_dir) -> list:
    """(name, aliases) of the entries the butler may choose from, the active ones first, at most LISTED."""
    entries = sorted(load(state_dir), key=lambda entry: entry["status"] != ACTIVE)
    return [(entry["name"], entry["aliases"]) for entry in entries[:LISTED]]

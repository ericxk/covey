"""代答's Feishu web login (its browser profile): where it is, whether a browser has it, and bounding the caches
Chromium rebuilds by itself (WEB_CACHES), each hour from maintenance_loop's prune_web."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from covey.core.maintenance_report import tree_bytes
from covey.core.process import paths_in_use
from covey.core.state import read_object


# 代答's Feishu web login (proxy/web/profile) is the owner's only kept one (2026-10-27). Of it only what Chromium
# rebuilds by itself goes, once together past WEB_CACHE_BYTES (repo review E6: 290 MB of them in a day):
# the HTTP, code and GPU caches and the service worker's copy of the site. Cookies, Local Storage, IndexedDB
# and everything else stay; a copy without these folders is the same login (docs/operations.md, 代答的配置).
WEB_CACHES = ("Default/Cache", "Default/Code Cache", "Default/Service Worker", "Default/GPUCache",
              "Default/DawnGraphiteCache", "Default/DawnWebGPUCache", "GrShaderCache", "GraphiteDawnCache",
              "ShaderCache")


WEB_CACHE_BYTES = 200 * 1024 * 1024


WEB_PAGE = "covey_feishu_web.py"  # Feishu web's page process (feishu/web_client.py).


PROC = Path("/proc")


def web_profile(state):
    """The 代答 web login's browser profile: ``web_home`` of proxy/settings.json, else proxy/web, + profile."""
    return Path(read_object(state / "proxy" / "settings.json").get("web_home") or state / "proxy" / "web") / "profile"


def web_caches(profile):
    """(folder, bytes) of each of WEB_CACHES the web login ``profile`` has."""
    folders = [Path(profile) / name for name in WEB_CACHES]
    return [(folder, tree_bytes(folder)) for folder in folders if folder.is_dir() and not folder.is_symlink()]


def browser_on(profile):
    """Whether a browser may have ``profile``: a process of this user holds one of its files or works in it
    (Chromium), or Feishu web's page process runs (it starts Chromium on a profile)."""
    if paths_in_use(profile):
        return True
    for proc in PROC.iterdir():
        try:
            # A program argument, not a shell command that only names it.
            if proc.name.isdigit() and proc.stat().st_uid == os.getuid() and any(
                    os.path.basename(arg) == WEB_PAGE.encode() for arg in (proc / "cmdline").read_bytes().split(b"\0")):
                return True
        except OSError:
            continue  # Exited meanwhile.
    return False


def prune_web_caches(profile, cap=WEB_CACHE_BYTES):
    """Remove the web login's caches (WEB_CACHES) once together past ``cap``: the bytes removed, 0 below it.
    The caller sees that no browser has the profile meanwhile (``browser_on``; app/proxy_send.py holds Proxy.web_lock)."""
    caches = web_caches(profile)
    if sum(size for _, size in caches) < cap:
        return 0
    for folder, _ in caches:
        shutil.rmtree(folder, ignore_errors=True)
    return sum(size for _, size in caches)

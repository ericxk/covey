"""Extensions (插件) from the command line: which are found and enabled, and turning one on or off for this deployment.

    python3 -B bin/covey_plugins.py list [--json]
    python3 -B bin/covey_plugins.py enable <package> | disable <package>
    python3 -B bin/covey_plugins.py doctor

An extension is a package with a ``manifest`` module on the search path (core/extensions.py: ``extensions/`` of the
code the Bridge runs, a release's included). Which are on is the release's ``extensions/enabled`` with the data
home's own choices applied (``<COVEY_DATA_HOME>/extensions/enabled``, core/extensions.py ``choices``), unless
``COVEY_EXTENSIONS`` is set, which overrides both. ``enable`` and ``disable`` write only that file of the data home:
the release and the service manifest stay as deployed, and the choice holds across deploys. Before writing, the new
list is loaded the way the Bridge loads it (each manifest, then the Harness and provider tables merged); a package
that would not load or collides is refused and nothing is written. The Bridge reads the list when it starts:
``covey_service.py restart`` (docs/operations.md 日常操作) puts a change into effect. Nothing here starts, stops or
restarts anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from covey.core import extensions
from covey.core.file_lock import file_lock
from covey.core.state import atomic_write_bytes

RESTART = "python3 -B bin/covey_service.py restart --state-dir \"$STATE_DIR\""


def found(environ=None, root: Path = extensions.ROOT) -> dict[str, Path]:
    """package -> its directory, for each top-level package with a manifest on the search path, the first found
    winning (as the import does)."""
    packages = {}
    for directory in extensions.search_path(environ, root):
        try:
            children = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for child in children:
            if (child / "manifest.py").is_file() and extensions.PACKAGE_NAME.fullmatch(child.name):
                packages.setdefault(child.name, child)
    return packages


def summary(directory: Path) -> str:
    """The first line of the package's README.md that says something, '' without one."""
    try:
        lines = (directory / "README.md").read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return ""


def check(packages, environ=None, root: Path = extensions.ROOT) -> str:
    """'' when ``packages`` load together as the Bridge would load them, else why not (one line)."""
    from covey.core import providers, roster

    environ = dict(os.environ if environ is None else environ)
    environ[extensions.ENABLED_ENV] = ",".join(packages)
    try:
        loaded = extensions.load(environ, root)
        roster.merged(roster.CORE_HARNESSES, roster.CORE_RETIRED, loaded)
        providers.merged(providers.CORE_PROVIDERS, providers.CORE_RETIRED_PREFIXES, loaded)
        kinds = {}
        for extension in loaded:
            for kind in extension.card_kinds:
                if kind in kinds:
                    raise extensions.ExtensionError(extension.package, f"card kind {kind!r} is already {kinds[kind]}'s")
                kinds[kind] = extension.package
    except extensions.ExtensionError as error:
        return str(error)
    return ""


def listing(environ=None, root: Path = extensions.ROOT) -> dict:
    """What ``list`` prints: where the list comes from, and each package found or enabled."""
    environ = os.environ if environ is None else environ
    overridden = extensions.cenv.has(extensions.ENABLED_ENV, environ)
    on = extensions.enabled(environ, root)
    chose_on, chose_off = extensions.choices(environ, root)
    present = found(environ, root)
    rows = []
    for package in list(present) + [name for name in on if name not in present]:
        source = ("COVEY_EXTENSIONS" if overridden else "数据根启用" if package in chose_on
                  else "数据根停用" if package in chose_off else "release 默认" if package in on else "")
        rows.append({"package": package, "enabled": package in on, "source": source,
                     "found": str(present[package]) if package in present else "",
                     "summary": summary(present[package]) if package in present else ""})
    return {"enabled": on, "choice_file": str(extensions.choice_path(environ, root)),
            "overridden_by_env": overridden, "packages": rows}


def write_choice(package: str, sign: str, environ=None, root: Path = extensions.ROOT) -> Path:
    """Record ``sign`` ('+' or '-') for ``package`` in the data home's choice file, replacing an earlier line about
    it; the file replaced whole (a reader sees the old one or the new one)."""
    path = extensions.choice_path(environ, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_name(path.name + ".lock")):
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = ["# 这个部署自己启用(+)或停用(-)的扩展，盖在 release 的 extensions/enabled 之上；",
                     "# 由 bin/covey_plugins.py enable/disable 写，改完重启 Bridge 生效。"]
        kept = [line for line in lines
                if line.split("#", 1)[0].strip().lstrip("+-").strip() != package]
        kept.append(sign + package)
        atomic_write_bytes(path, ("\n".join(kept) + "\n").encode("utf-8"), prefix=path.name + ".", notify=False)
    return path


def turn(package: str, on: bool, environ=None, root: Path = extensions.ROOT) -> dict:
    """Enable or disable ``package`` for this deployment: {'ok', 'message', 'enabled'}; nothing written unless ok."""
    environ = os.environ if environ is None else environ
    if not extensions.PACKAGE_NAME.fullmatch(package):
        return {"ok": False, "message": f"{package!r} 不是包名"}
    current = extensions.enabled(environ, root)
    if on and package not in found(environ, root):
        return {"ok": False, "message": f"找不到扩展 {package}：搜索路径 {extensions.search_path(environ, root)} 下没有带 "
                                        f"manifest.py 的 {package}/"}
    wanted = (current + [package] if package not in current else current) if on else [n for n in current if n != package]
    problem = check(wanted, environ, root) if on else ""
    if problem:
        return {"ok": False, "message": f"没有启用：{problem}", "enabled": current}
    path = write_choice(package, "+" if on else "-", environ, root)
    note = ""
    if extensions.cenv.has(extensions.ENABLED_ENV, environ):
        note = f"；注意：这个环境设了 {extensions.ENABLED_ENV}，它盖过数据根的选择，要先去掉它"
    return {"ok": True, "enabled": extensions.enabled(environ, root),
            "message": f"已{'启用' if on else '停用'} {package}（写在 {path}）。重启 Bridge 生效：{RESTART}{note}"}


def doctor(environ=None, root: Path = extensions.ROOT) -> dict:
    """Whether the enabled list loads as the Bridge would load it."""
    on = extensions.enabled(environ, root)
    problem = check(on, environ, root)
    return {"ok": not problem, "enabled": on, "message": problem or "启用的扩展都能加载，没有冲突"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="covey_plugins.py", description="查看、启用、停用扩展（插件）")
    commands = parser.add_subparsers(dest="command", required=True)
    shown = commands.add_parser("list", help="找到的扩展和是否启用")
    shown.add_argument("--json", action="store_true")
    for name, help_text in (("enable", "为这个部署启用一个扩展"), ("disable", "为这个部署停用一个扩展")):
        commands.add_parser(name, help=help_text).add_argument("package")
    commands.add_parser("doctor", help="检查启用的扩展能否一起加载")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = listing()
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=1))
            else:
                print(f"启用：{', '.join(result['enabled']) or '（无）'}"
                      f"{'（来自 COVEY_EXTENSIONS）' if result['overridden_by_env'] else ''}")
                print(f"本部署的选择：{result['choice_file']}")
                for row in result["packages"]:
                    mark = "●" if row["enabled"] else "○"
                    where = row["found"] or "找不到"
                    print(f"{mark} {row['package']:<20} {row['source'] or '未启用':<12} {row['summary']}  [{where}]")
            return 0
        result = doctor() if args.command == "doctor" else turn(args.package, args.command == "enable")
    except extensions.ExtensionError as error:
        result = {"ok": False, "message": str(error)}
    print(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""把“核心检出 + 若干扩展检出”拼成一个 release 目录，形状和旧单仓库 `git archive` 出来的一样（scripts/deploy_bridge.sh 用它）。

    python3 -B scripts/assemble_release.py --core CHECKOUT [--core-ref REV] --out DIR
        [--ext CHECKOUT [--ext-ref REV]] ...

- 核心：`git archive <核心提交>` 到 DIR。核心里若还带着扩展（旧单仓库），以扩展检出为准：先删掉同名的
  extensions/<包名>/ 和 extensions/enabled。
- 扩展检出：仓库根目录下含 manifest.py 的目录都是扩展包（scripts/export_extension.py 导出的布局：
  <包名>/manifest.py，另有 README.md、core.lock、dev/）。每个包 `git archive` 到 DIR/extensions/<包名>/，
  去掉包里的 standalone/；包里 bin/*.py 的入口（如扩展自带的运行时脚本）放进 DIR/bin/，盖掉核心里同名的转发脚本。
- 写 DIR/extensions/enabled：按 --ext 的顺序，每个扩展检出列出它根目录 `enabled` 文件里的包名（一行一个，# 开头是注释，
  必须是这个检出里的包）；检出没有 `enabled` 文件就列出它所有的包。没列的包随 release 带着，但默认不启用、DIR/RELEASE_MANIFEST（每个检出的完整提交号）；
  最后重写 DIR/AGENTS.md 里的模块表（scripts/code_map.py，和单仓库一样包含扩展的模块）。
- --ext 可重复，--ext-ref 按顺序和它配对（少给的用 HEAD）。不给 --ext 时就是纯核心的 git archive，
  不建 extensions/enabled。按提交取，不看工作区；DIR 必须不存在或为空。只用标准库，核心代码里不写任何扩展名。
- 最后一行输出 JSON：{"core": 完整提交, "extensions": [{"repo", "commit", "packages"}...], "out": DIR}。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENABLED_HEADER = ("# Extensions the company deployment turns on by default (core/extensions.py); "
                  "COVEY_EXTENSIONS overrides this file.\n")


class AssembleError(Exception):
    pass


def git(repo: Path, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=300)
    if p.returncode:
        raise AssembleError(f"git {' '.join(args)} failed in {repo}: {p.stderr.decode(errors='replace').strip()[-300:]}")
    return p.stdout


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def extract(repo: Path, sha: str, dest: Path, *paths: str) -> None:
    packed = git(repo, "archive", sha, *(["--", *paths] if paths else []))
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=packed, check=True, timeout=300)


def packages_of(repo: Path, sha: str) -> list[str]:
    """Top-level directories of the checkout at sha that hold a manifest.py: the extension packages."""
    names = git(repo, "ls-tree", "-r", "--name-only", sha).decode().splitlines()
    return sorted({n.split("/")[0] for n in names if re.fullmatch(r"[^/]+/manifest\.py", n)})


def core_lock(repo: Path, sha: str) -> str | None:
    """The core commit the extension names in its core.lock at sha (first non-comment word), or None."""
    p = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:core.lock"], capture_output=True, text=True)
    if p.returncode:
        return None
    for line in p.stdout.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line.split()[0]
    return None


def enabled_of(repo: Path, sha: str, packages: list[str]) -> list[str]:
    """The packages this extension checkout turns on by default: its root `enabled` file, or all of its packages."""
    p = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:enabled"], capture_output=True, text=True)
    if p.returncode:
        return list(packages)
    names = [ln.strip() for ln in p.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    unknown = [n for n in names if n not in packages]
    if unknown:
        raise AssembleError(f"{repo}: enabled names {unknown}, which are not packages of that checkout ({packages})")
    return names


def assemble(core: Path, core_ref: str, exts: list[tuple[Path, str]], out: Path) -> dict:
    out = out.resolve()
    core_sha = resolve(core, core_ref)
    found = []
    for repo, ref in exts:
        sha = resolve(repo, ref)
        packages = packages_of(repo, sha)
        if not packages:
            raise AssembleError(f"{repo} at {sha[:8]} has no extension package (a top-level directory with manifest.py)")
        found.append((repo, sha, packages))
    if out.exists() and any(out.iterdir()):
        raise AssembleError(f"{out} is not empty")
    extract(core, core_sha, out)
    manifest = [f"core {core_sha}"]
    if found:
        names = [name for _, _, packages in found for name in packages]
        if len(set(names)) != len(names):
            raise AssembleError(f"the same extension package twice: {sorted(n for n in names if names.count(n) > 1)}")
        (out / "extensions").mkdir(exist_ok=True)
        (out / "extensions" / "enabled").unlink(missing_ok=True)
        for repo, sha, packages in found:
            for name in packages:
                shutil.rmtree(out / "extensions" / name, ignore_errors=True)  # A core that still carries it: ours wins.
            extract(repo, sha, out / "extensions", *packages)
            for name in packages:
                shutil.rmtree(out / "extensions" / name / "standalone", ignore_errors=True)
                scripts = out / "extensions" / name / "bin"
                if scripts.is_dir():
                    (out / "bin").mkdir(exist_ok=True)
                    for script in sorted(scripts.glob("*.py")):
                        shutil.copy2(script, out / "bin" / script.name)
            manifest.append(f"extension {sha} {' '.join(packages)}")
        # The code map in AGENTS.md lists the extensions' modules too, as in the single repository.
        code_map = out / "scripts" / "code_map.py"
        if code_map.exists():
            done = subprocess.run([sys.executable, "-B", str(code_map), "--write"], cwd=out, timeout=120,
                                  capture_output=True, text=True)
            if done.returncode:
                raise AssembleError(f"code_map.py --write failed: {(done.stderr or done.stdout).strip()[-300:]}")
        turned_on = [n for repo, sha, packages in found for n in enabled_of(repo, sha, packages)]
        (out / "extensions" / "enabled").write_text(ENABLED_HEADER + "".join(f"{n}\n" for n in turned_on))
        (out / "RELEASE_MANIFEST").write_text("\n".join(manifest) + "\n")
    return {"core": core_sha, "out": str(out),
            "extensions": [{"repo": str(r), "commit": s, "packages": p} for r, s, p in found]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core", required=True, type=Path)
    ap.add_argument("--core-ref", default="HEAD")
    ap.add_argument("--ext", action="append", default=[], type=Path)
    ap.add_argument("--ext-ref", action="append", default=[])
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    if len(args.ext_ref) > len(args.ext):
        ap.error("more --ext-ref than --ext")
    refs = args.ext_ref + ["HEAD"] * (len(args.ext) - len(args.ext_ref))
    try:
        info = assemble(args.core, args.core_ref, list(zip(args.ext, refs)), args.out)
    except (AssembleError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"assemble_release: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

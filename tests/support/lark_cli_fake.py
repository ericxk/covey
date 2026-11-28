"""A fake npm global lark-cli for the upgrade's lark-cli step (covey/service/lark_cli_upgrade.py): the layout npm leaves
(<prefix>/bin/lark-cli -> ../lib/node_modules/@larksuite/cli/scripts/run.js, the package's bin/lark-cli) and an npm whose
``root -g`` is that prefix's, so nothing a test runs reaches the machine's own lark-cli, npm or the network.

``FAKE_LARK_MODE`` in the environment says what ``lark-cli update --json`` does: unset, it updates to LATEST (or says it is
up to date); ``eacces``, ``network`` fail as the real one does with no write access or no network; ``broken`` leaves no binary;
``lie`` says it updated and changes nothing; ``hang`` never ends (its child's pid in hang.pid). Every update is a line of
calls.jsonl. The folder must allow executing its files (support.exec_tempdir)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LATEST = "1.0.100"

RUN = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path
here = Path({folder!r})
state = here / "state.json"
package = Path(__file__).resolve().parents[1]
args = sys.argv[1:]
if args == ["--version"]:
    if not (package / "bin" / "lark-cli").is_file():
        sys.exit(1)
    print("lark-cli version " + json.loads(state.read_text())["version"])
    sys.exit(0)
if args[:1] != ["update"]:
    print("unknown command", file=sys.stderr)
    sys.exit(2)
mode = os.environ.get("FAKE_LARK_MODE", "")
deploy_log = Path(os.environ.get("FAKE_DEPLOY_LOG") or here / "none")
deploys = [json.loads(line)["argv"] for line in deploy_log.read_text().splitlines()] if deploy_log.is_file() else []
with (here / "calls.jsonl").open("a") as log:  # "deploys": the deploys (not the checks) a fake deploy_bridge.sh ran before.
    log.write(json.dumps({{"argv": args, "mode": mode, "path": os.environ.get("PATH", ""),
                          "deploys": sum("--check-only" not in argv for argv in deploys)}}) + "\n")
now = json.loads(state.read_text())["version"]
print("note: could not resolve the configured brand; syncing skills from the default source", file=sys.stderr)
if mode == "eacces":
    print(json.dumps({{"ok": False, "error": {{"type": "update_error", "message": "npm install failed: exit status 243",
                                             "hint": "Permission denied. Try: sudo lark-cli update",
                                             "detail": "npm error code EACCES"}}}}))
    sys.exit(1)
if mode == "network":
    print(json.dumps({{"ok": False, "error": {{"type": "network", "message": "failed to check latest version: connection refused"}}}}))
    sys.exit(4)
if mode == "broken":
    (package / "bin" / "lark-cli").unlink()
    print(json.dumps({{"ok": False, "error": {{"type": "update_error", "message": "binary verification failed"}}}}))
    sys.exit(1)
if mode == "hang":
    child = subprocess.Popen(["sleep", "300"])
    (here / "hang.pid").write_text(str(child.pid))
    time.sleep(300)
    sys.exit(0)
if now == {latest!r}:
    print(json.dumps({{"ok": True, "action": "already_up_to_date", "current_version": now, "latest_version": now,
                      "previous_version": now, "skills_action": "in_sync"}}, indent=2))
    sys.exit(0)
if mode != "lie":
    state.write_text(json.dumps({{"version": {latest!r}}}))
print(json.dumps({{"ok": True, "action": "updated", "previous_version": now, "current_version": {latest!r},
                  "latest_version": {latest!r}, "skills_action": "synced"}}, indent=2))
'''

BINARY = r'''#!{python}
import json, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("lark-cli version " + json.loads((Path({folder!r}) / "state.json").read_text())["version"])
    sys.exit(0)
sys.exit(2)
'''

NPM = r'''#!{python}
import sys
if sys.argv[1:] == ["root", "-g"]:
    print({root!r})
    sys.exit(0)
sys.exit(1)
'''


def _script(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def install(folder, version="1.0.99", latest=LATEST) -> dict:
    """Lay out npm's global lark-cli at ``version`` under ``folder``: {"bin" (put it first in PATH), "package", "state",
    "calls"}."""
    folder = Path(folder)
    prefix = folder / "prefix"
    root = prefix / "lib" / "node_modules"
    package = root / "@larksuite" / "cli"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps({"version": version}))
    values = {"python": sys.executable, "folder": str(folder), "latest": latest, "root": str(root)}
    _script(package / "scripts" / "run.js", RUN.format(**values))
    _script(package / "bin" / "lark-cli", BINARY.format(**values))
    _script(prefix / "bin" / "npm", NPM.format(**values))
    os.symlink(os.path.join("..", "lib", "node_modules", "@larksuite", "cli", "scripts", "run.js"), prefix / "bin" / "lark-cli")
    return {"bin": str(prefix / "bin"), "package": package, "state": folder / "state.json", "calls": folder / "calls.jsonl"}


def local_copy(folder, version="1.0.100") -> str:
    """A lark-cli npm installed into a project of its own (``npm install @larksuite/cli`` in ``folder``), not the global one:
    the directory to put in front of PATH."""
    folder = Path(folder)
    package = folder / "node_modules" / "@larksuite" / "cli"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps({"version": version}))
    values = {"python": sys.executable, "folder": str(folder), "latest": LATEST, "root": ""}
    _script(package / "scripts" / "run.js", RUN.format(**values))
    _script(package / "bin" / "lark-cli", BINARY.format(**values))
    bin_dir = folder / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.join("..", "@larksuite", "cli", "scripts", "run.js"), bin_dir / "lark-cli")
    return str(bin_dir)


def calls(fake) -> list:
    """The updates the fake was asked for, in order."""
    path = Path(fake["calls"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

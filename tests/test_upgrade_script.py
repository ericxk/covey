"""scripts/upgrade.sh (D11 of T647, docs/deploy.md 日常升级): a throwaway checkout with an origin a commit ahead and a fake
scripts/deploy_bridge.sh that records what it was given, so each boundary is seen without a deploy: a clean checkout, a
fetch and fast-forward only, the one service found, the manifest's own environment and command handed to the pre-check,
--dry-run changing nothing, a failed check or a running deploy stopping before anything, the checkout fast-forwarded only
after a deploy that went through, the machine's lark-cli updated between the check and the deploy (a fake npm global one,
support/lark_cli_fake.py, first in the service's PATH). One test runs the real deploy_bridge.sh --check-only on a fake
service root. Each run is one short-lived bash, Python and git child under TMPDIR; nothing touches the network."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import REPO, exec_tempdir
from support import lark_cli_fake

from covey.service.manager import runtime_environment

UPGRADE = REPO / "scripts" / "upgrade.sh"
APP = "cli_up"
# Records its arguments and environment (FAKE_DEPLOY_LOG), answers what FAKE_DEPLOY_ANSWER says for a check or a deploy.
FAKE_DEPLOY = """#!/bin/bash
exec python3 - "$@" <<'EOF'
import json, os, sys
from pathlib import Path
check = "--check-only" in sys.argv
with open(os.environ["FAKE_DEPLOY_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\\n")
answers = json.loads(Path(os.environ["FAKE_DEPLOY_ANSWER"]).read_text()) if os.environ.get("FAKE_DEPLOY_ANSWER") else {}
code, result = answers.get("check" if check else "deploy", [0, {"ok": True}])
if not check and code == 0:
    result = {"release": "/absolute/releases/" + sys.argv[sys.argv.index("--commit") + 1], "post": {"generation": 8}, **result}
print(json.dumps(result))
sys.exit(code)
EOF
"""
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@localhost", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *argv):
    done = subprocess.run(["git", *argv], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
                          timeout=60)
    if done.returncode:
        raise AssertionError(f"git {argv}: {done.stderr}")
    return done.stdout.strip()


class UpgradeCase(unittest.TestCase):
    deploy_script = FAKE_DEPLOY

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin, self.checkout, self.other = self.tmp / "origin.git", self.tmp / "covey", self.tmp / "other"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.checkout.mkdir()
        self.lay_out()
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-q", "-m", "first")
        git(self.checkout, "remote", "add", "origin", str(self.origin))
        git(self.checkout, "push", "-q", "-u", "origin", "main")
        self.first = git(self.checkout, "rev-parse", "HEAD")
        git(self.tmp, "clone", "-q", str(self.origin), str(self.other))
        (self.other / "NEWS.md").write_text("a newer commit\n")
        git(self.other, "add", "-A")
        git(self.other, "commit", "-q", "-m", "newer")
        git(self.other, "push", "-q", "origin", "main")
        self.newer = git(self.other, "rev-parse", "HEAD")
        self.data = self.tmp / "data"
        self.state, self.work = (self.data / f"harness-{APP}").resolve(), self.tmp / "work"
        for path in (self.state, self.data / "bridge-services" / "services", self.work):
            path.mkdir(parents=True)
        self.log, self.answer = self.tmp / "deploy-calls.jsonl", self.tmp / "answer.json"
        self.base = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "LANG": "C.UTF-8", "TMPDIR": str(self.tmp),
                     "COVEY_DATA_HOME": str(self.data), "FAKE_DEPLOY_LOG": str(self.log), **GIT_ENV}
        self.saved = {**runtime_environment(self.base), "LARK_BRIDGE_REVIEW": "1",
                      "https_proxy": "http://proxy.example.invalid:3128"}
        tools = exec_tempdir()  # The service's lark-cli is a fake npm global one: never the machine's.
        self.addCleanup(tools.cleanup)
        self.lark = lark_cli_fake.install(Path(tools.name) / "npm")
        self.saved["PATH"] = self.lark["bin"] + os.pathsep + self.saved["PATH"]
        self.manifest()

    def lay_out(self):
        (self.checkout / "README.md").write_text("covey\n")
        (self.checkout / "scripts").mkdir()
        (self.checkout / "scripts" / "deploy_bridge.sh").write_text(self.deploy_script)

    def args(self):
        return ["--harness", "unified", "--state-dir", str(self.state), "--workdir", str(self.work), "--profile", "covey",
                "--app-id", APP, "--sandbox", "danger-full-access", "--harness-config", str(self.data / "harnesses.json"),
                "--claude-transport", "stream-json"]

    def manifest(self, name="a", entry=None, state=None, python=None, **fields):
        state = state or self.state
        record = {"id": name, "state_dir": str(state), "desired_state": "running", "environment": self.saved,
                  "command": [python or shutil.which("python3", path=self.saved["PATH"]), "-B",
                              str(entry or self.checkout / "bin" / "covey_bridge.py"), *self.args()], **fields}
        (self.data / "bridge-services" / "services" / f"{name}.json").write_text(json.dumps(record))

    def upgrade(self, *argv, **env):
        done = subprocess.run(["bash", str(UPGRADE), "--repo", str(self.checkout), *argv], env={**self.base, **env},
                              capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def head(self):
        return git(self.checkout, "rev-parse", "HEAD")


class UpgradeTests(UpgradeCase):
    def test_a_dry_run_checks_with_the_service_s_own_environment_and_command_and_changes_nothing(self):
        code, out = self.upgrade("--dry-run", COVEY_PINNED_TOPICS="1", https_proxy="http://other.example.invalid:1")
        self.assertEqual(code, 0, out)
        for words in ("比检出新 1 个提交", "现网：直接从检出运行", "部署前的检查都过了", "--dry-run：什么都没换"):
            self.assertIn(words, out)
        [call] = self.calls()
        argv = call["argv"]
        self.assertIn("--check-only", argv)
        for flag, value in (("--repo", str(self.checkout)), ("--commit", self.newer), ("--workdir", str(self.work)),
                            ("--profile", "covey"), ("--app-id", APP), ("--state-dir", str(self.state)),
                            ("--harness-config", str(self.data / "harnesses.json"))):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertTrue(argv[argv.index("--run-dir") + 1].startswith(str(self.data / "deploy-runs")))
        self.assertEqual(argv[argv.index("--") + 1:], ["--claude-transport", "stream-json"])  # The rest of the live command.
        env = call["env"]
        self.assertEqual((env["LARK_BRIDGE_REVIEW"], env["https_proxy"], env["PATH"]),
                         ("1", "http://proxy.example.invalid:3128", self.saved["PATH"]))
        self.assertNotIn("COVEY_PINNED_TOPICS", env)  # This shell's own settings never reach the service.
        self.assertEqual(env["FAKE_DEPLOY_LOG"], str(self.log))  # What the manifest does not decide is this shell's.
        self.assertEqual(self.head(), self.first)

    def test_an_upgrade_deploys_after_the_check_then_fast_forwards_and_a_second_run_is_up_to_date(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertEqual([("--check-only" in call["argv"]) for call in self.calls()], [True, False])
        self.assertIn("部署中：机器人会重启，几秒内收不到消息和卡片点击", out)  # Not tens of seconds since the restart is quick.
        self.assertIn(f"升级好了：机器人第 8 代，从 release {self.newer[:8]} 运行。", out)
        self.assertIn("不要再用 ./install 改", out)  # It ran from the checkout until now.
        self.assertIn(f"检出已快进到 {self.newer[:8]}", out)
        self.assertEqual(self.head(), self.newer)
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn(f"已经是最新（{self.newer[:8]}）", out)
        self.assertEqual(len(self.calls()), 2)

    def test_lark_cli_is_said_by_a_dry_run_and_updated_between_the_check_and_the_deploy(self):
        code, out = self.upgrade("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("[lark-cli] " + os.path.join(self.lark["bin"], "lark-cli") + "（npm 全局，1.0.99）", out)
        self.assertEqual(lark_cli_fake.calls(self.lark), [])  # Read only: --version and npm root -g.
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("[lark-cli] 更新好了：1.0.99 → 1.0.100", out)
        [update] = lark_cli_fake.calls(self.lark)
        self.assertEqual((update["argv"], update["deploys"]), (["update", "--json"], 0))  # Before the deploy, after its check.
        self.assertEqual(update["path"], self.saved["PATH"])
        self.assertEqual([("--check-only" in call["argv"]) for call in self.calls()], [True, True, False])
        self.assertLess(out.index("[lark-cli] 更新好了"), out.index("部署中"))
        code, out = self.upgrade()  # Up to date: no deploy, the lark-cli step all the same.
        self.assertEqual(code, 0, out)
        self.assertIn("[lark-cli] 已经是最新（1.0.100）", out)
        self.assertEqual(len(lark_cli_fake.calls(self.lark)), 2)
        self.assertEqual(len(self.calls()), 3)

    def test_a_lark_cli_update_that_fails_never_stops_the_upgrade_and_a_broken_one_stops_before_the_deploy(self):
        code, out = self.upgrade(FAKE_LARK_MODE="eacces")
        self.assertEqual(code, 0, out)
        self.assertIn("[lark-cli] 没更新上，还用原来的 1.0.99（不影响这次升级）", out)
        self.assertIn("Permission denied", out)
        self.assertIn("升级好了：机器人第 8 代", out)
        git(self.checkout, "reset", "-q", "--hard", self.first)
        self.manifest()
        code, out = self.upgrade(FAKE_LARK_MODE="broken")
        self.assertEqual(code, 1, out)
        self.assertIn("lark-cli 更新后跑不起来了，Bridge 没换", out)
        self.assertIn("npm install -g @larksuite/cli@1.0.99", out)
        self.assertNotIn("部署中", out)
        self.assertEqual([("--check-only" in call["argv"]) for call in self.calls()], [True, False, True])

    def test_a_dirty_checkout_or_a_commit_upstream_lacks_stops_before_anything(self):
        (self.checkout / "README.md").write_text("changed\n")
        code, out = self.upgrade()
        self.assertEqual(code, 1, out)
        self.assertIn("检出里有没提交的改动：README.md", out)
        self.assertIn("git stash", out)
        git(self.checkout, "commit", "-q", "-am", "mine")
        code, out = self.upgrade()
        self.assertEqual(code, 1, out)
        self.assertIn("检出里有 origin/main 没有的提交（1 个）", out)
        self.assertIn("--ref HEAD", out)
        self.assertEqual(self.calls(), [])
        code, out = self.upgrade("--ref", "HEAD")  # Its own commit, asked for: no stop for it (it runs that already).
        self.assertEqual(code, 0, out)
        self.assertIn("已经是最新", out)

    def test_the_one_service_is_found_or_the_choice_is_asked_for(self):
        (self.data / "bridge-services" / "services" / "a.json").unlink()
        code, out = self.upgrade("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("没有注册的机器人", out)
        self.assertIn("./install", out)
        second = (self.data / "harness-cli_two").resolve()
        second.mkdir()
        self.manifest("a")
        self.manifest("b", state=second)
        code, out = self.upgrade("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("有 2 个机器人", out)
        self.assertIn(str(second), out)
        code, out = self.upgrade("--dry-run", "--state-dir", str(self.state))
        self.assertEqual(code, 0, out)
        self.manifest("a", desired_state="stopped")
        code, out = self.upgrade("--dry-run", "--state-dir", str(self.state))
        self.assertEqual(code, 1, out)
        self.assertIn("这个机器人停着", out)

    def test_a_live_release_at_the_target_is_up_to_date_and_one_with_extensions_is_left_to_its_own_steps(self):
        release = self.data / "releases" / self.newer
        (release / "bin").mkdir(parents=True)
        self.manifest(entry=release / "bin" / "covey_bridge.py")
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn(f"现网：release {self.newer}", out)
        self.assertIn("已经是最新", out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.head(), self.newer)  # The service runs from a release: the checkout catches up at once.
        mixed = self.data / "releases" / f"{self.first[:8]}-0123abcd"
        (mixed / "bin").mkdir(parents=True)
        (mixed / "RELEASE_MANIFEST").write_text(f"core {self.first}\nextension {'0' * 40} covey_internal\n")
        self.manifest(entry=mixed / "bin" / "covey_bridge.py")
        code, out = self.upgrade()
        self.assertEqual(code, 1, out)
        self.assertIn("带着扩展", out)
        self.assertIn("--ext-repo", out)
        self.assertEqual(self.calls(), [])

    def test_a_failed_check_or_a_running_deploy_is_said_and_nothing_is_deployed(self):
        self.answer.write_text(json.dumps({"check": [1, {"ok": False, "error": "pre-checks failed: ['restart_blockers']",
                                                         "pre": {"restart_blockers": [{"job_id": 3}]}}]}))
        code, out = self.upgrade(FAKE_DEPLOY_ANSWER=str(self.answer))
        self.assertEqual(code, 1, out)
        self.assertIn("部署前的检查没过", out)
        self.assertIn("有任务重启会被打断", out)
        self.assertIn(str(self.data / "deploy-runs"), out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.head(), self.first)
        self.answer.write_text(json.dumps({"check": [3, {"ok": False, "deploy_running": {"pid": 42, "started": "now"}}]}))
        code, out = self.upgrade(FAKE_DEPLOY_ANSWER=str(self.answer))
        self.assertEqual(code, 3, out)
        self.assertIn("另一个部署正在跑（pid 42", out)
        self.answer.write_text(json.dumps({"deploy": [1, {"ok": False, "error": "bootstrap failed, see bootstrap.log"}]}))
        code, out = self.upgrade(FAKE_DEPLOY_ANSWER=str(self.answer))
        self.assertEqual(code, 1, out)
        self.assertIn("部署没成功", out)
        self.assertEqual(self.head(), self.first)  # Not fast-forwarded after a deploy that did not go through.

    def test_bash_env_and_env_are_not_handed_to_the_deploy(self):
        code, out = self.upgrade("--dry-run", BASH_ENV=str(self.tmp / "bashrc"), ENV=str(self.tmp / "shrc"))
        self.assertEqual(code, 0, out)
        [call] = self.calls()
        self.assertNotIn("BASH_ENV", call["env"])
        self.assertNotIn("ENV", call["env"])

    def personal(self, name="mine"):
        path = self.data / "extensions" / name
        (path / "covey_mine").mkdir(parents=True)
        (path / "covey_mine" / "manifest.py").write_text("HARNESSES = {}\n")
        git(path, "init", "-q", "-b", "main")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "mine")
        for each in (path, path / ".git"):
            each.chmod(0o755)
        return path, git(path, "rev-parse", "HEAD")

    def test_a_personal_extension_not_in_the_live_release_is_an_upgrade_and_the_receipt_says_what_to_do(self):
        release = self.data / "releases" / self.newer
        (release / "bin").mkdir(parents=True)
        self.manifest(entry=release / "bin" / "covey_bridge.py")
        path, sha = self.personal()
        self.answer.write_text(json.dumps({"deploy": [0, {
            "ok": True, "release_id": f"{self.newer[:8]}-{sha[:8]}",
            "personal_extensions": [{"path": str(path), "commit": sha, "packages": ["covey_mine"]}],
            "pre": {"switches_on": ["COVEY_REVISIT"]},
            "post": {"generation": 9, "owner_todo": ["飞书应用待补：卡片按钮（card.action.trigger）"]}}]}))
        code, out = self.upgrade(FAKE_DEPLOY_ANSWER=str(self.answer))
        self.assertEqual(code, 0, out)
        self.assertNotIn("已经是最新", out)
        self.assertIn(f"个人扩展  mine（{sha[:8]}）：和现网带的不一样，这次一起换", out)
        self.assertEqual([("--check-only" in call["argv"]) for call in self.calls()], [True, False])
        self.assertIn(f"个人扩展 mine {sha[:8]}：covey_mine", out)
        self.assertIn("升级后要你补的：", out)
        self.assertIn("1. 这一版起默认打开（你的服务清单没设）：COVEY_REVISIT", out)
        self.assertIn("2. 飞书应用待补：卡片按钮", out)
        # Packed in the live release already: up to date.
        live = self.data / "releases" / f"{self.newer[:8]}-{sha[:8]}"
        (live / "bin").mkdir(parents=True)
        (live / "RELEASE_MANIFEST").write_text(f"core {self.newer}\npersonal {sha} covey_mine\n")
        self.manifest(entry=live / "bin" / "covey_bridge.py")
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("已经是最新", out)
        self.assertEqual(len(self.calls()), 2)

    def test_a_personal_extension_that_stops_the_check_is_named_with_the_command_that_turns_it_off(self):
        self.personal()
        command = f"COVEY_DATA_HOME={self.data} python3 -B /absolute/covey/bin/covey_plugins.py disable covey_mine"
        self.answer.write_text(json.dumps({"check": [1, {"ok": False, "error": "pre-checks failed: ['extension_checks']",
                                                         "pre": {"extension_checks": ["covey_mine: cannot import hook "
                                                                                      "claude_turn (covey_mine.turn): ImportError"],
                                                                 "extension_disable": {"covey_mine": command}}}]}))
        code, out = self.upgrade(FAKE_DEPLOY_ANSWER=str(self.answer))
        self.assertEqual(code, 1, out)
        self.assertIn("扩展自检没过", out)
        self.assertIn("covey_mine: cannot import hook claude_turn", out)
        self.assertIn("拦下升级的是扩展 covey_mine（个人扩展", out)
        self.assertIn(command, out)
        self.assertEqual(len(self.calls()), 1)

    def test_a_service_python_that_cannot_run_and_bad_arguments_stop(self):
        self.manifest(python="/absolute/no/python3")
        code, out = self.upgrade()
        self.assertEqual(code, 1, out)
        self.assertIn("服务用的 Python 不能用或低于 3.10", out)
        code, out = self.upgrade("--what")
        self.assertEqual(code, 2, out)
        code, out = self.upgrade("--ref", "origin/nowhere")
        self.assertEqual(code, 2, out)
        self.assertIn("找不到 origin/nowhere", out)
        self.assertEqual(self.calls(), [])


class RealCheckTests(UpgradeCase):
    """The real scripts/deploy_bridge.sh --check-only, run by upgrade.sh in a checkout whose bin/ and covey/ are this
    repository's: with the manifest loaded its environment and command checks pass, whatever this shell sets."""
    deploy_script = (REPO / "scripts" / "deploy_bridge.sh").read_text()

    def lay_out(self):
        super().lay_out()
        for name in ("bin", "covey"):
            (self.checkout / name).symlink_to(REPO / name)

    def test_the_pre_check_passes_with_the_service_s_environment(self):
        code, out = self.upgrade("--dry-run", COVEY_PINNED_TOPICS="1", LARK_BRIDGE_REVIEW="0")
        self.assertEqual(code, 0, out)
        self.assertIn("部署前的检查都过了", out)
        [run] = sorted((self.data / "deploy-runs").iterdir())
        self.assertTrue(run.name.endswith("-check"))
        self.assertFalse((self.data / "releases").exists())


# A fake web/deploy/selfhost/build.sh in a release: writes build.sh's mark naming the release's web/ (exit 0), or fails.
FAKE_BUILD = """#!/bin/bash
[ -n "$FAKE_BUILD_FAIL" ] && { echo "npm ERR! network"; exit 1; }
printf '%s\\n' "covey web selfhost build; source $(cd "$(dirname "$0")/../../.." && pwd)/web" > "$1/.covey-selfhost-build"
echo "built $1 $2"
"""


class StatusAndWebTests(UpgradeCase):
    """--status says what an upgrade would do and changes nothing (app/self_upgrade.py reads its last line); an upgrade brings a
    page build.sh built along and writes <data>/upgrade.json for the page and COVEY_AUTO_UPGRADE (T785)."""

    def status(self, *argv):
        code, out = self.upgrade("--status", "--json", *argv)
        self.assertEqual(code, 0, out)
        return json.loads(out.strip().splitlines()[-1]), out

    def selfhost(self):
        target = self.tmp / "web-app"
        target.mkdir()
        (target / ".covey-selfhost-build").write_text(f"covey web selfhost build; source {self.checkout}/web\n")
        self.saved = {**self.saved, "LARK_BRIDGE_WEB": "1", "LARK_BRIDGE_WEB_SELFHOST": "1",
                      "LARK_BRIDGE_WEB_SELFHOST_DIR": str(target / "app")}
        self.manifest()
        build = self.data / "releases" / self.newer / "web" / "deploy" / "selfhost" / "build.sh"
        build.parent.mkdir(parents=True)
        build.write_text(FAKE_BUILD)
        return target

    def test_status_says_what_is_newer_and_changes_nothing(self):
        found, out = self.status()
        self.assertEqual((found["live"], found["latest"], found["bridge"], found["count"], found["blocked"], found["web"]),
                         (f"检出 {self.first[:8]}", self.newer[:8], True, 1, "", None))
        self.assertEqual([row["subject"] for row in found["changes"]], ["newer"])
        self.assertIn("1 个新提交", out)
        self.assertEqual((self.calls(), self.head()), ([], self.first))
        self.assertFalse((self.data / "upgrade.json").exists())

    def test_status_in_words_says_the_lark_cli_and_the_page_s_json_runs_none(self):
        code, out = self.upgrade("--status")
        self.assertEqual(code, 0, out)
        self.assertIn("[lark-cli] " + os.path.join(self.lark["bin"], "lark-cli") + "（npm 全局，1.0.99）", out)
        _, out = self.status()
        self.assertNotIn("[lark-cli]", out)
        self.assertEqual(lark_cli_fake.calls(self.lark), [])

    def test_status_says_what_blocks_an_upgrade_instead_of_stopping(self):
        (self.checkout / "README.md").write_text("changed\n")
        found, _ = self.status()
        self.assertIn("没提交的改动", found["blocked"])
        self.assertEqual(found["latest"], self.newer[:8])  # Still what an upgrade would deploy.
        mixed = self.data / "releases" / f"{self.first[:8]}-0123abcd"
        (mixed / "bin").mkdir(parents=True)
        (mixed / "RELEASE_MANIFEST").write_text(f"core {self.first}\nextension {'0' * 40} covey_internal\n")
        git(self.checkout, "checkout", "-q", "README.md")
        self.manifest(entry=mixed / "bin" / "covey_bridge.py")
        found, _ = self.status()
        self.assertIn("带着扩展", found["blocked"])

    def test_an_upgrade_writes_the_upgrader_and_leaves_an_extension_s_alone(self):
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        written = json.loads((self.data / "upgrade.json").read_text())
        self.assertEqual(written["by"], "covey")
        self.assertEqual(written["status"], [str(self.checkout / "scripts" / "upgrade.sh"), "--status", "--json",
                                             "--state-dir", str(self.state)])
        self.assertEqual(written["run"], [str(self.checkout / "scripts" / "upgrade.sh"), "--state-dir", str(self.state)])
        (self.data / "upgrade.json").write_text(json.dumps({"by": "covey-internal", "status": ["/x"], "run": ["/y"]}))
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.data / "upgrade.json").read_text())["by"], "covey-internal")

    def test_a_self_hosted_page_comes_along_and_a_failed_build_is_exit_4(self):
        target = self.selfhost()
        found, _ = self.status()
        self.assertEqual(found["web"], {"kind": "selfhost", "current": False, "manual": False})
        code, out = self.upgrade(FAKE_BUILD_FAIL="1")
        self.assertEqual(code, 4, out)  # The Bridge went through: exit 4, never a rollback.
        self.assertIn("升级好了", out)
        self.assertIn("[网页] 没更新上（Bridge 已经是新版，不用回滚）", out)
        release = self.data / "releases" / self.newer
        (release / "bin").mkdir(parents=True, exist_ok=True)
        self.manifest(entry=release / "bin" / "covey_bridge.py")  # As the real deploy registers it.
        found, _ = self.status()
        self.assertEqual((found["bridge"], found["web"]["current"]), (False, False))  # Still offered: the page lags.
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("已经是最新", out)
        self.assertIn("[网页] 更新好了", out)
        self.assertIn(f"source {release / 'web'}", (target / ".covey-selfhost-build").read_text())
        found, _ = self.status()
        self.assertEqual(found["web"]["current"], True)
        (target / ".covey-selfhost-build").write_text(f"covey web selfhost build; source {self.checkout}/web\n")
        code, out = self.upgrade("--no-web")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[网页]", out)

    def test_a_page_made_another_way_is_only_said(self):
        self.saved = {**self.saved, "LARK_BRIDGE_WEB": "1", "LARK_BRIDGE_WEB_CONFIG": "/absolute/web.json"}
        self.manifest()
        found, _ = self.status()
        self.assertEqual(found["web"], {"kind": "app", "current": False, "manual": True})
        code, out = self.upgrade()
        self.assertEqual(code, 0, out)
        self.assertIn("没有替你更新", out)


if __name__ == "__main__":
    unittest.main()

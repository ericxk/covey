"""Products of a turn (app/artifacts.py): finding them, packing a directory, the Drive panel and sending."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import requires_extension
from covey.app.artifact_policy import artifact_key, drive_paths, resolve_artifact, zip_directory
from covey.cards.artifact_panels import drive_artifact_panel
from support.fake_lark import FakeCardKit
from support.world import TopicFixture


class ArtifactTest(TopicFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.receive("om_a", "task")
        self.ctx_topic = self.topic("om_a")
        self.file = self.root / "result.txt"
        self.file.write_text("result")
        self.cp = self.root / "cp.json"
        self.cp.write_text(json.dumps({"artifacts": [str(self.file)]}))
        self.record = {
            "key": "artifact",
            "job_id": 1,
            "chat_id": "oc_chat",
            "topic_id": self.ctx_topic.topic_id,
            "workdir": str(self.root),
            "checkpoint_path": str(self.cp),
            "reply_to_message_id": "om_a",
        }

    def test_recorded_external_artifact_allowed_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            resolve_artifact(self.record, "unknown")
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "report.md"
            path.write_text("report")
            self.cp.write_text(json.dumps({"artifacts": [str(path)]}))
            self.assertEqual(resolve_artifact(self.record, artifact_key(str(path))), path)
            secret = Path(outside) / ".env"
            secret.write_text("secret")
            self.cp.write_text(json.dumps({"artifacts": [str(secret)]}))
            with self.assertRaises(ValueError):
                resolve_artifact(self.record, artifact_key(str(secret)))

    def test_a_home_start_topic_resolves_against_its_home_and_may_publish_from_its_worktrees(self):
        """workdir is the home for a ``home_start`` topic; a file in the topic's worktree is an artifact, a secret there is not."""
        home = self.root / "home"
        tree = self.root / "worktrees" / "T7-abcd1234" / "proj"
        for folder in (home / "tasks", tree):
            folder.mkdir(parents=True)
        (home / "tasks" / "out.md").write_text("out")
        (tree / "report.md").write_text("report")
        (tree / ".env").write_text("secret")
        record = {**self.record, "workdir": str(home)}
        self.cp.write_text(json.dumps({"artifacts": ["tasks/out.md", str(tree / "report.md"), str(tree / ".env")]}))
        self.assertEqual(resolve_artifact(record, artifact_key("tasks/out.md")), (home / "tasks" / "out.md").resolve())
        self.assertEqual(resolve_artifact(record, artifact_key(str(tree / "report.md"))), (tree / "report.md").resolve())
        with self.assertRaises(ValueError):
            resolve_artifact(record, artifact_key(str(tree / ".env")))
        self.assertEqual(drive_paths(record), ["tasks/out.md", str(tree / "report.md")])

    def test_a_diff_report_removed_by_maintenance_says_so(self):
        from covey.app.artifact_policy import DIFF_PRUNED
        diff = self.root / "job-diffs" / "artifact" / "diff.md"
        record = {**self.record, "diff_artifact_path": str(diff)}
        with self.assertRaisesRegex(ValueError, DIFF_PRUNED):
            resolve_artifact(record, artifact_key(str(diff)))

    def test_a_kept_result_file_is_no_artifact_until_a_send_writes_it_over(self):
        from covey.app.artifacts import write_result_file
        from covey.core.jobs import job_path, read_job_record, upsert_job_record

        state = self.ctx.state_dir
        upsert_job_record(state, "artifact", status="done", checkpoint_path=str(self.cp), workdir=str(self.root))
        path = write_result_file(state, "artifact", "短的结论")  # C7: kept, sent nowhere.
        record = read_job_record(job_path(state, "artifact"))
        self.assertEqual((record["result_file_path"], record["result_file_kept"]), (str(path), True))
        self.assertEqual(drive_paths(record), [str(self.file)])  # No 产物 line for it on a card repainted later.
        self.assertEqual(write_result_file(state, "artifact", "又一次"), path)
        self.assertEqual(path.read_text(), "短的结论")
        self.assertIsNone(write_result_file(state, "blank", "  \n"))
        self.assertFalse((path.parent / "blank.md").exists())
        self.assertEqual(write_result_file(state, "artifact", "要发的长文", replace=True), path)  # send_result_file's.
        record = read_job_record(job_path(state, "artifact"))
        self.assertEqual((path.read_text(), record["result_file_kept"]), ("要发的长文", False))
        self.assertEqual(drive_paths(record), [str(self.file), str(path)])

    def test_upload_and_topic_delivery_are_deduplicated(self):
        with (
            mock.patch(
                "covey.feishu.drive.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, '{"data":{"file_key":"file-test"}}', ""),
            ) as upload,
            FakeCardKit({"data": {}}) as send,
        ):
            self.assertEqual(self.app.artifacts.send_artifact(self.ctx_topic, self.record, artifact_key(str(self.file))), "")
            self.assertIn("已发送", self.app.artifacts.send_artifact(self.ctx_topic, self.record, artifact_key(str(self.file))))
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(len(send.calls), 1)
        self.assertEqual(send.last.path, "/open-apis/im/v1/messages/om_a/reply")
        self.assertTrue(send.last.data["reply_in_thread"])
        self.assertFalse(list((self.ctx.state_dir / "artifact-upload").glob("*")))

    def test_upload_stuck_on_one_file_leaves_other_files_free(self):
        other = self.root / "other.txt"
        other.write_text("other")
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(other)]}))
        release, uploading = threading.Event(), threading.Event()

        def upload(argv, **kwargs):
            if any('"result.txt"' in arg for arg in argv):
                uploading.set()
                release.wait(10)
            return subprocess.CompletedProcess([], 0, '{"data":{"file_key":"file-test"}}', "")
        with (
            mock.patch("covey.feishu.drive.subprocess.run", side_effect=upload),
            FakeCardKit({"data": {}}) as send,
        ):
            stuck = threading.Thread(target=self.app.artifacts.send_artifact, daemon=True,
                                     args=(self.ctx_topic, self.record, artifact_key(str(self.file))))
            stuck.start()
            try:
                self.assertTrue(uploading.wait(5))
                sent = []
                second = threading.Thread(daemon=True, target=lambda: sent.append(
                    self.app.artifacts.send_artifact(self.ctx_topic, self.record, artifact_key(str(other)))))
                second.start()
                second.join(5)
                self.assertEqual(sent, [""])  # While the first upload is still stuck.
                self.assertTrue(stuck.is_alive())
            finally:
                release.set()
                stuck.join(5)
        self.assertEqual(len(send.calls), 2)

    def drive(self, *, fail=None, error=None, full=()):
        """Fake lark-cli drive calls; records argv and the uploaded bytes and answers like the real CLI.
        ``fail``: the verb refused with ``error`` (the missing Drive scope by default); ``full``: folder tokens a
        folder cannot be created in, refused as Feishu does (code 1062507, the JSON on stderr)."""
        calls, self.uploaded = [], {}

        def run(argv, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            verb = argv[argv.index("drive") + 1]
            if fail and verb == fail:
                body = {"ok": False, "error": error or {"type": "authorization", "subtype": "app_scope_not_applied",
                                                        "missing_scopes": ["drive:file"]}}
                return subprocess.CompletedProcess(argv, 3, json.dumps(body), "")
            if verb == "+create-folder" and "--folder-token" in argv and argv[argv.index("--folder-token") + 1] in full:
                body = {"ok": False, "identity": "bot", "error": {
                    "type": "api", "subtype": "quota_exceeded", "code": 1062507,
                    "message": "parent node out of sibling num.", "hint": "-", "log_id": f"LOG{len(calls)}"}}
                return subprocess.CompletedProcess(argv, 1, "", json.dumps(body))
            if verb == "+create-folder":
                data = {"folder_token": f"fld{len(calls)}", "url": f"https://drive/folder/fld{len(calls)}"}
            elif verb == "+member-add":
                data = {"perm": "full_access"}
            else:
                self.assertTrue((Path(kwargs["cwd"]) / argv[argv.index("--file") + 1]).is_file())
                data = {"file_token": f"box{len(calls)}", "file_name": argv[argv.index("--name") + 1],
                        "url": f"https://drive/file/box{len(calls)}"}
                self.uploaded[data["file_name"]] = (Path(kwargs["cwd"]) / argv[argv.index("--file") + 1]).read_bytes()
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "data": data}), "")
        return calls, mock.patch("covey.feishu.drive.subprocess.run", side_effect=run)

    def test_artifacts_upload_once_into_a_task_folder_the_owner_manages(self):
        secret, settings = self.root / ".env", self.root / ".claude" / "settings.json"
        secret.write_text("TOKEN=1")
        settings.parent.mkdir()
        settings.write_text("{}")
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(secret), str(settings)]}))
        self.app.runtime.owner_open_id = "ou_owner"
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
            again = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        verbs = [argv[argv.index("drive") + 1] for argv, _ in calls]
        # The root, shared with the owner; today's folder in it; the task's folder in that.
        self.assertEqual(verbs, ["+create-folder", "+member-add", "+create-folder", "+create-folder", "+upload"])
        root_grant = calls[1][0]
        self.assertEqual(root_grant[root_grant.index("--member-id") + 1], "ou_owner")
        self.assertEqual(calls[4][0][calls[4][0].index("--as") + 1], "bot")
        self.assertEqual(self.folder_args(calls[2][0]), (time.strftime("%Y-%m-%d"), "fld1"))
        self.assertEqual(self.folder_args(calls[3][0])[1], "fld3")
        self.assertEqual(ledger["status"], "done")
        self.assertEqual(again["files"], ledger["files"])
        uploaded = ledger["files"][artifact_key(str(self.file))]
        self.assertEqual((uploaded["name"], uploaded["url"]), ("result.txt", "https://drive/file/box5"))
        # Configuration and credential paths are skipped quietly: off the card, one log line per upload.
        self.assertEqual(ledger["errors"], {})
        self.assertEqual([line for line in self.logs if "skipped config paths" in line],
                         ["artifact upload skipped config paths job=#1 count=2"] * 2)
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        listing, *notes = [e["content"] for e in panel["elements"] if e["tag"] == "markdown"]
        # Folded once all is up, the title naming what is in it (T182 #1111: open, the tallest block).
        self.assertEqual(panel["header"]["title"]["content"], "产物 1 个 · result.txt")
        self.assertFalse(panel["expanded"])
        self.assertNotIn(".env", listing)
        self.assertNotIn("settings.json", listing)
        self.assertFalse([e for e in panel["elements"] if e["tag"] == "button"])
        self.assertIn("[result.txt](https://drive/file/box5)", listing)
        # The folder link is its own element, never glued to the last file line.
        self.assertNotIn("https://drive/folder/fld4", listing)
        self.assertIn("https://drive/folder/fld4", notes[-1])
        self.assertFalse(list((self.ctx.state_dir / "artifact-upload").iterdir()))
        self.file.write_text("changed")
        with patch:
            self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual([argv[argv.index("drive") + 1] for argv, _ in calls[5:]], ["+upload"])

    @staticmethod
    def folder_args(argv):
        """A +create-folder call's (name, parent folder token or None)."""
        parent = argv[argv.index("--folder-token") + 1] if "--folder-token" in argv else None
        return argv[argv.index("--name") + 1], parent

    def root_record(self, saved=None):
        path = self.ctx.state_dir / "artifact-drive" / "root-folder.json"
        if saved is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved))
        return json.loads(path.read_text())

    def test_an_older_root_record_is_read_and_gets_a_day_folder(self):
        # What every release before day folders wrote: the root, its link and the owner it was shared with.
        self.app.runtime.owner_open_id = "ou_owner"
        self.root_record({"folder_token": "old_root", "url": "https://drive/folder/old_root", "owner": "ou_owner"})
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["status"], "done")
        self.assertEqual([self.folder_args(argv) for argv, _ in calls if "+create-folder" in argv],
                         [(time.strftime("%Y-%m-%d"), "old_root"), (mock.ANY, "fld1")])
        self.assertFalse([argv for argv, _ in calls if "+member-add" in argv])
        saved = self.root_record()
        self.assertEqual((saved["folder_token"], saved["url"], saved["owner"]),
                         ("old_root", "https://drive/folder/old_root", "ou_owner"))
        self.assertEqual(saved["days"][time.strftime("%Y-%m-%d")]["token"], "fld1")

    def test_a_full_root_gives_way_to_a_new_root_the_owner_manages(self):
        # 2026-11-09: the root held 1500 task folders, and every upload failed (api_rejected on the card).
        self.app.runtime.owner_open_id = "ou_owner"
        self.root_record({"folder_token": "old_root", "url": "https://drive/folder/old_root", "owner": "ou_owner"})
        today = time.strftime("%Y-%m-%d")
        calls, patch = self.drive(full={"old_root"})
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["status"], "done", ledger.get("error"))
        verbs = [argv[argv.index("drive") + 1] for argv, _ in calls]
        self.assertEqual(verbs, ["+create-folder", "+create-folder", "+member-add", "+create-folder",
                                 "+create-folder", "+upload"])
        self.assertEqual(self.folder_args(calls[0][0]), (today, "old_root"))  # Refused: the root is full.
        self.assertEqual(self.folder_args(calls[1][0]), ("Harness Bridge 产物 2", None))  # In the bot's space.
        grant = calls[2][0]
        self.assertEqual((grant[grant.index("--token") + 1], grant[grant.index("--member-id") + 1],
                          grant[grant.index("--perm") + 1]), ("fld2", "ou_owner", "full_access"))
        self.assertEqual(self.folder_args(calls[3][0]), (today, "fld2"))
        self.assertEqual(self.folder_args(calls[4][0])[1], "fld4")
        self.assertEqual(ledger["folder_token"], "fld5")
        saved = self.root_record()
        self.assertEqual((saved["folder_token"], saved["name"], saved["owner"]), ("fld2", "Harness Bridge 产物 2", "ou_owner"))
        # The full root is kept as it is, listed; nothing deleted, moved or renamed on Drive.
        self.assertEqual([(r["folder_token"], r["url"]) for r in saved["roots"]],
                         [("old_root", "https://drive/folder/old_root")])
        self.assertEqual(saved["days"][today]["token"], "fld4")
        self.assertFalse({"+delete", "+move", "+rename"} & set(verbs))
        failed = [line for line in self.logs if line.startswith("drive call failed")]
        self.assertEqual(len(failed), 1)
        for part in ("op=+create-folder", "code=1062507", "msg=parent node out of sibling num.", "log_id=LOG1"):
            self.assertIn(part, failed[0])
        # The next task goes straight into today's folder of the new root.
        with patch:
            second = self.app.artifacts.upload_artifacts(self.ctx_topic, {**self.record, "key": "artifact-2", "job_id": 2})
        self.assertEqual(second["status"], "done")
        self.assertEqual([self.folder_args(argv)[1] for argv, _ in calls[6:] if "+create-folder" in argv], ["fld4"])

    def test_a_full_day_folder_moves_the_day_on_to_its_next_folder(self):
        today = time.strftime("%Y-%m-%d")
        old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        self.app.runtime.owner_open_id = "ou_owner"
        self.root_record({"folder_token": "root", "url": "u", "owner": "ou_owner", "days": {
            today: {"token": "day_full", "url": "d", "name": today, "number": 1, "root": "root"},
            old: {"token": "day_old", "url": "o", "name": old, "number": 1, "root": "root"}}})
        calls, patch = self.drive(full={"day_full"})
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["status"], "done", ledger.get("error"))
        self.assertEqual([self.folder_args(argv) for argv, _ in calls if "+create-folder" in argv],
                         [(mock.ANY, "day_full"), (f"{today} (2)", "root"), (mock.ANY, "fld2")])
        days = self.root_record()["days"]
        # A day older than a week leaves the record only; its folder stays on Drive.
        self.assertEqual(list(days), [today])
        self.assertEqual((days[today]["token"], days[today]["name"], days[today]["number"]), ("fld2", f"{today} (2)", 2))

    def test_a_drive_failure_says_what_feishu_said_and_logs_its_code(self):
        from covey.feishu.drive import DriveError, drive_error
        full = drive_error({"error": {"code": 1062507, "message": "parent node out of sibling num."}}, "api_rejected")
        self.assertIsInstance(full, ValueError)
        self.assertIsInstance(full, DriveError)
        self.assertEqual((str(full), full.code), ("云空间文件夹已满（一个文件夹最多 1500 个）", 1062507))
        self.assertEqual(str(drive_error(None, "api_rejected")), "云空间接口调用失败（api_rejected）")
        calls, patch = self.drive(fail="+create-folder", error={
            "type": "api", "code": "1061004", "message": "forbidden.", "log_id": "20261109ABC"})
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual((ledger["status"], ledger["error"]), ("failed", "云空间接口调用失败（错误码 1061004：forbidden.）"))
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        notes = [e["content"] for e in panel["elements"] if e["tag"] == "markdown"]
        self.assertIn("上传失败：云空间接口调用失败（错误码 1061004：forbidden.）", notes[-1])
        [line] = [line for line in self.logs if line.startswith("drive call failed")]
        self.assertIn("op=+create-folder code=1061004", line)
        self.assertIn("log_id=20261109ABC", line)

    def directory(self):
        out = self.root / "out"
        for name, text in [("a.txt", "a"), ("sub/b.txt", "b"), (".git/config", "git"), (".env", "TOKEN=1"),
                           ("sub/.ssh/id_rsa", "key"), ("credentials.json", "{}"), ("sub/.env.local", "TOKEN=2")]:
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_text(text)
        (out / "link.txt").symlink_to(self.file)
        (out / "linked").symlink_to(out / "sub")
        return out

    def test_a_directory_goes_up_as_one_zip_without_secrets_or_links(self):
        out = self.directory()
        listing = sorted(p.relative_to(out) for p in out.rglob("*"))
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(out)]}))
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["errors"], {})
        self.assertEqual(ledger["files"][artifact_key(str(out))]["name"], "out.zip")
        with zipfile.ZipFile(io.BytesIO(self.uploaded["out.zip"])) as packed:
            self.assertEqual(packed.namelist(), ["out/a.txt", "out/sub/b.txt"])
            self.assertEqual(packed.read("out/sub/b.txt"), b"b")
        # Packed in the state's staging folder, which is emptied; the workspace is untouched.
        self.assertTrue(all(Path(cwd).parent == self.ctx.state_dir / "artifact-upload" for argv, cwd in calls if "+upload" in argv))
        self.assertFalse(list((self.ctx.state_dir / "artifact-upload").iterdir()))
        self.assertEqual(sorted(p.relative_to(out) for p in out.rglob("*")), listing)
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        self.assertEqual(panel["header"]["title"]["content"], "产物 2 个 · result.txt 等")
        # A newer timestamp alone does not upload it again; changed content does.
        os.utime(out / "a.txt", (1, 1))
        with patch:
            self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(sum("+upload" in argv for argv, _ in calls), 2)
        (out / "a.txt").write_text("changed")
        with patch:
            self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(sum("+upload" in argv for argv, _ in calls), 3)
        # The older per-file send buttons still take files only.
        with self.assertRaisesRegex(ValueError, "这是目录"):
            resolve_artifact(self.record, artifact_key(str(out)))

    def test_a_directory_packs_to_the_same_bytes(self):
        out = self.directory()
        first, second = self.root / "first.zip", self.root / "second.zip"
        digest = zip_directory(out, first)
        os.utime(out / "sub" / "b.txt", (1, 1))
        (out / "a.txt").chmod(0o600)
        self.assertEqual(zip_directory(out, second), digest)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as packed:
            self.assertEqual({(i.date_time, i.external_attr >> 16) for i in packed.infolist()},
                             {((1980, 1, 1, 0, 0, 0), 0o100644)})

    def test_a_directory_over_either_cap_is_refused_before_packing(self):
        out = self.directory()
        dest = self.root / "out.zip"
        with self.assertRaisesRegex(ValueError, "^目录超过 1 个文件，未打包$"):
            zip_directory(out, dest, max_files=1)
        with self.assertRaisesRegex(ValueError, "^目录超过 \\d+ MB，未打包$"):
            zip_directory(out, dest, max_bytes=1)
        self.assertFalse(dest.exists())
        empty = self.root / "empty"
        (empty / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "^目录里没有可打包的文件$"):
            zip_directory(empty, dest)
        self.assertFalse(dest.exists())
        # Measured from the sizes on disk, so a sparse 201 MB file is refused without reading it.
        with (out / "big.bin").open("wb") as big:
            big.truncate(201 * 1024 * 1024)
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(out)]}))
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["errors"], {artifact_key(str(out)): "目录超过 200 MB，未打包"})
        self.assertEqual(list(self.uploaded), ["result.txt"])
        self.assertFalse(list((self.ctx.state_dir / "artifact-upload").iterdir()))
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        listing = next(e["content"] for e in panel["elements"] if e["tag"] == "markdown")
        self.assertIn("out：目录超过 200 MB，未打包", listing)
        self.assertIn("1 个未上传", panel["header"]["title"]["content"])

    def test_a_file_name_that_is_not_utf8_still_packs(self):
        out = self.directory()
        with open(os.fsencode(out) + b"/\xb1\xa8\xb8\xe6.txt", "wb") as handle:  # GBK "报告.txt", as unzip leaves it.
            handle.write(b"x")
        dest = self.root / "odd.zip"
        zip_directory(out, dest)
        with zipfile.ZipFile(dest) as packed:
            names = packed.namelist()
        self.assertIn("out/a.txt", names)
        self.assertTrue(any(n.startswith("out/\ufffd") and n.endswith(".txt") for n in names))

    def test_a_path_that_cannot_be_resolved_is_only_that_files_error(self):
        loop = self.root / "loop"
        loop.symlink_to("loop")
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(loop), "~nosuchuser-s25/x"]}))
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id)  # Raised for every file before.
        self.assertIn("3 个", panel["header"]["title"]["content"])
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["errors"], {artifact_key(str(loop)): "产物路径无法解析",
                                            artifact_key("~nosuchuser-s25/x"): "产物路径无法解析"})
        self.assertEqual(list(self.uploaded), ["result.txt"])

    @requires_extension()
    def test_files_in_agent_worktrees_upload_like_ordinary_files(self):
        maso, claude = self.root / "home" / ".maso" / "worktrees" / "w1", self.root / ".claude" / "worktrees" / "w2"
        for name, text in [(maso / "report.md", "r"), (claude / "notes.md", "n"), (claude / "out" / "a.txt", "a"),
                           (claude / "out" / ".env", "TOKEN=1"), (claude / "out" / ".claude" / "settings.json", "{}"),
                           (claude / ".env", "TOKEN=2"), (claude / ".git" / "config", "git"),
                           (maso / ".ssh" / "id_rsa", "key"), (self.root / ".claude" / "settings.json", "{}"),
                           (maso.parent / "index.json", "{}"), (self.root / "home" / ".ssh" / "id_rsa", "key")]:
            name.parent.mkdir(parents=True, exist_ok=True)
            name.write_text(text)
        (claude / "link.md").symlink_to(self.root / ".claude" / "settings.json")
        uploaded = [maso / "report.md", claude / "notes.md", claude / "out"]
        # Still refused below the root, outside worktrees/<name>/, and wherever ../ or a link leads.
        skipped = [claude / ".env", claude / ".git" / "config", maso / ".ssh" / "id_rsa",
                   self.root / ".claude" / "settings.json", maso.parent / "index.json", maso,
                   f"{claude}/../../settings.json", f"{maso}/../../../.ssh/id_rsa", claude / "link.md"]
        self.cp.write_text(json.dumps({"artifacts": [str(p) for p in uploaded + skipped]}))
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["errors"], {})
        self.assertEqual(sorted(self.uploaded), ["notes.md", "out.zip", "report.md"])
        with zipfile.ZipFile(io.BytesIO(self.uploaded["out.zip"])) as packed:
            self.assertEqual(packed.namelist(), ["out/a.txt"])
        self.assertEqual([line for line in self.logs if "skipped config paths" in line],
                         [f"artifact upload skipped config paths job=#1 count={len(skipped)}"])
        for raw in skipped:
            with self.assertRaisesRegex(ValueError, "配置或凭据"):
                resolve_artifact(self.record, artifact_key(str(raw)))

    def test_the_panel_stays_open_while_uploading_or_after_a_failure(self):
        # Folded only once every file is up: until then 重新上传 must be in sight.
        uploading = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id)
        failed = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, {"status": "failed", "error": "无权限"})
        for panel, state in ((uploading, "正在上传到云空间"), (failed, "云空间上传失败")):
            self.assertTrue(panel["expanded"], state)
            self.assertIn(state, panel["header"]["title"]["content"])
            self.assertTrue([e for e in panel["elements"] if e["tag"] == "button"], state)

    def test_a_file_that_failed_can_be_fixed_and_retried_from_the_same_card(self):
        missing = self.root / "later.csv"
        self.cp.write_text(json.dumps({"artifacts": [str(self.file), str(missing)]}))
        self.app.runtime.owner_open_id = "ou_owner"
        calls, patch = self.drive()
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        self.assertEqual(ledger["status"], "done")
        self.assertEqual(panel["header"]["title"]["content"], "产物 2 个 · result.txt 等 · 1 个未上传")
        self.assertTrue(panel["expanded"])  # Open where 重新上传 is.
        self.assertTrue([e for e in panel["elements"] if e["tag"] == "button"])
        uploaded = dict(ledger["files"])
        missing.write_text("a,b\n")
        with patch:
            again = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        # Only the fixed file is uploaded, into the same task folder.
        self.assertEqual([argv[argv.index("drive") + 1] for argv, _ in calls[len(calls) - 1:]], ["+upload"])
        self.assertEqual(sum(argv[argv.index("drive") + 1] == "+upload" for argv, _ in calls), 2)
        self.assertEqual({k: again["files"][k] for k in uploaded}, uploaded)
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, again)
        self.assertEqual(panel["header"]["title"]["content"], "产物 2 个 · result.txt 等")
        self.assertFalse(panel["expanded"])
        self.assertFalse([e for e in panel["elements"] if e["tag"] == "button"])

    def test_retry_refreshes_the_card_under_its_own_key_and_reports_a_failed_refresh(self):
        record = {**self.record, "job_id": 7, "card_message_id": "card"}
        event = {"chat_id": "oc_chat", "event_id": "click", "message_id": "card",
                 "action_value": {"kind": "bridge_artifact_upload", "topic_id": self.ctx_topic.topic_id, "job_id": 7}}
        for refresh in (None, ValueError("翻页更新失败，请在原卡片重试")):
            with mock.patch.object(self.app.harness, "topic_records", return_value=[record]), \
                    mock.patch.object(self.app.artifacts, "upload_artifacts", return_value={"status": "done", "files": {}, "errors": {}}), \
                    mock.patch.object(self.app.artifacts, "in_background", side_effect=lambda target, *a: target(*a)), \
                    mock.patch.object(type(self.app.cards), "update_record_panel", side_effect=[None, refresh]) as update, \
                    mock.patch.object(self.app.messenger, "reply") as sent:
                self.assertTrue(self.app.artifacts.artifact_action(self.ctx_topic, event))
            # CardKit rejects a second update under a key it has just consumed (code 200770).
            self.assertEqual([c.args[2] for c in update.call_args_list], ["click", "click-uploaded"])
            self.assertEqual(sent.called, refresh is not None)

    def test_missing_drive_scope_is_reported_and_retryable(self):
        self.app.runtime.owner_open_id = "ou_owner"
        calls, patch = self.drive(fail="+create-folder")
        with patch:
            ledger = self.app.artifacts.upload_artifacts(self.ctx_topic, self.record)
        self.assertEqual(ledger["status"], "failed")
        self.assertIn("drive:file", ledger["error"])
        panel = drive_artifact_panel(drive_paths(self.record), self.record, self.ctx_topic.topic_id, ledger)
        retry = [e for e in panel["elements"] if e["tag"] == "button"]
        self.assertEqual(retry[0]["behaviors"][0]["value"]["kind"], "bridge_artifact_upload")

    def test_delivery_retry_reuses_uploaded_file(self):
        with (
            mock.patch(
                "covey.feishu.drive.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, '{"data":{"file_key":"file-test"}}', ""),
            ) as upload,
            FakeCardKit(side_effect=[None, {"data": {}}]) as send,
        ):
            with self.assertRaises(ValueError):
                self.app.artifacts.send_artifact(self.ctx_topic, self.record, artifact_key(str(self.file)))
            self.app.artifacts.send_artifact(self.ctx_topic, self.record, artifact_key(str(self.file)))
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(send.calls[0].data["uuid"], send.calls[1].data["uuid"])

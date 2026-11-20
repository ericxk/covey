"""Files between the hub and a device topic (app/device_files.py): the attachments of a message put on the device before
the turn, and the artifacts and shown images copied back, all through the real device program on a fake device
(tests/support/fake_device.py: the real shim under a temporary HOME). The hub never opens a path of the device
(tests/support/device_guard.py), and a path the hub would refuse is refused for the device's file too.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from support import make_context, make_job
from support.device_guard import DeviceGuard
from support.fake_device import FakeDevice

from covey.app import artifact_policy, artifacts, device_files, turn_images
from covey.core.checkpoint import artifact_paths
from covey.app.artifact_policy import SENSITIVE_NAMES, artifact_key, config_path, drive_paths, resolve_artifact
from covey.backends.codex.appserver import image_paths
from covey.core import devices
from covey.core.jobs import job_key

PNG = b"\x89PNG\r\n\x1a\n pixels"
DOC = {"schema": 1, "control_dir": "/dev/shm/covey-ssh-files",
       "devices": {"gpu1": {"ssh": {"host": "gpu1.example"}, "harnesses": ["claude", "codex"],
                            "spaces": ["owner_private"]}}}


class Fixture(unittest.TestCase):
    """A hub (temporary root, state directory) and a device (temporary HOME with a project directory)."""

    def setUp(self):
        self.device = FakeDevice()
        self.addCleanup(self.finish)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "devices.json").write_text(json.dumps(DOC))
        env = {"COVEY_DEVICES": "1", "COVEY_DEVICES_FILE": str(self.root / "devices.json"),
               "COVEY_DATA_HOME": str(self.root / "data")}
        self.stack.enter_context(mock.patch.dict(os.environ, env))
        self.stack.enter_context(mock.patch.object(devices, "open_transport", return_value=self.device.transport))
        self.state = self.root / "state"
        profile = devices.device_dir(self.state, "gpu1")
        profile.mkdir(parents=True)
        (profile / "profile.json").write_text(json.dumps({"home": str(self.device.home)}))
        self.standin = devices.devices_dir(self.state) / "gpu1" / "standin"
        self.proj = self.device.home / "proj"
        self.proj.mkdir()
        self.topic = self.root / "topics" / "t1"
        self.media = self.topic / "incoming_media"
        self.media.mkdir(parents=True)
        self.ctx = make_context(self.root, device="gpu1", remote_workdir=str(self.proj), state_dir=self.state,
                                history_path=self.topic / "history.jsonl", topic_id="t1")
        self.job = make_job(job_id=7, message_id="om_job", chat_id="oc_dm", topic_id="t1")
        self.key = job_key("om_job", 7)
        self.calls = []
        for name in ("put", "get"):
            self.wrap(name)

    def wrap(self, name):
        real = getattr(self.device.transport, name)

        def logged(path, *args, **kwargs):
            self.calls.append((name, path))
            return real(path, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(self.device.transport, name, logged))

    def finish(self):
        self.assertEqual(self.device.cleanup(), [])

    def guard(self):
        return DeviceGuard(self.standin, device_root=self.device.home)

    def media_file(self, name, data=b"data"):
        path = self.media / name
        path.write_bytes(data)
        return path

    def message(self, *paths):
        """The text a message with attachments gives the model (feishu/media.py), the paths in backquotes."""
        lines = ["收到的附件（已下载到本地）："]
        lines += ["- 文件 %d: `%s`" % (number, path) for number, path in enumerate(paths, 1)]
        return "\n".join(lines + ["请在需要理解图片或文件内容时读取上述本地路径。"])

    def stat_calls(self):
        return [fields for command, fields in self.device.transport.log if command == "stat"]


class StageTest(Fixture):
    def test_the_files_the_prompt_names_are_put_on_the_device_and_the_prompt_names_the_copies(self):
        doc, pic = self.media_file("report.pdf", b"%PDF device"), self.media_file("shot.png", PNG)
        prompt = "see this\n" + self.message(doc, pic) + "\nand again `%s`" % doc
        attachments = [{"path": str(pic), "mime": "image/png"}]
        with self.guard() as guard:
            staged = device_files.stage(self.ctx, self.job, prompt, attachments)
        guard.assert_untouched(self)
        directory = "%s/.covey/incoming/%s/%s" % (self.device.home, devices.hub_id(self.state), self.key)
        self.assertEqual(staged.directory, directory)
        self.assertEqual((Path(directory) / "report.pdf").read_bytes(), b"%PDF device")
        self.assertEqual((Path(directory) / "shot.png").read_bytes(), PNG)
        self.assertEqual(staged.prompt.count(directory + "/report.pdf"), 2)  # Each mention of the path.
        self.assertIn(directory + "/shot.png", staged.prompt)
        for hub in (str(self.media), "incoming_media", str(self.root)):
            self.assertNotIn(hub, staged.prompt)
        self.assertEqual(staged.attachments, [{"path": str(pic), "mime": "image/png",
                                               "device_path": directory + "/shot.png", "shown": directory + "/shot.png"}])
        self.assertEqual(staged.skipped, [])
        self.assertEqual(staged.card_line("gpu1"), "")
        self.assertEqual(sorted(path for name, path in self.calls if name == "put"),
                         [directory + "/report.pdf", directory + "/shot.png"])
        self.assertEqual(oct((Path(directory) / "report.pdf").stat().st_mode & 0o777), "0o600")

    def test_a_prompt_that_names_nothing_of_the_topics_media_is_left_as_it_is_and_the_device_is_not_asked(self):
        elsewhere = self.root / "elsewhere.txt"
        elsewhere.write_text("x")
        staged = device_files.stage(self.ctx, self.job, "plain `%s` text" % elsewhere, [])
        self.assertEqual((staged.prompt, staged.attachments, staged.directory), ("plain `%s` text" % elsewhere, [], ""))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.device.transport.log, [])

    def test_a_file_over_the_limit_is_skipped_and_said_so_in_the_prompt_the_card_and_the_log_line(self):
        small, big = self.media_file("small.txt", b"1234"), self.media_file("big.bin", b"x" * 20)
        with mock.patch.object(device_files, "MAX_FILE", 10):
            staged = device_files.stage(self.ctx, self.job, self.message(small, big), [])
        self.assertEqual([name for name, _, _ in staged.put], ["small.txt"])
        self.assertEqual([(name, size) for name, size, _ in staged.skipped], [("big.bin", 20)])
        self.assertEqual(self.calls, [("put", staged.put[0][2])], "the big one is not even sent")
        self.assertNotIn(str(big), staged.prompt)
        self.assertIn(device_files.NOT_PUT, staged.prompt)
        self.assertIn("big.bin", staged.prompt.split("这些附件没有传到设备 gpu1")[1])
        line = staged.card_line("gpu1")
        self.assertIn("1 个附件没有传到设备 gpu1", line)
        self.assertIn("big.bin", line)
        self.assertIn("超过单个文件", line)

    def test_the_turns_files_together_are_bounded_too_and_the_later_ones_are_skipped(self):
        first, second, third = (self.media_file(name, b"x" * 6) for name in ("a.bin", "b.bin", "c.bin"))
        with mock.patch.object(device_files, "MAX_TURN", 13):
            staged = device_files.stage(self.ctx, self.job, self.message(first, second, third), [])
        self.assertEqual([name for name, _, _ in staged.put], ["a.bin", "b.bin"])
        self.assertEqual([(name, why) for name, _, why in staged.skipped], [("c.bin", "本轮附件合计超过 0 MiB")])

    def test_the_limits_are_the_specs(self):
        self.assertEqual((device_files.MAX_FILE, device_files.MAX_TURN), (64 << 20, 256 << 20))

    def test_two_files_of_one_name_each_get_a_copy(self):
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "same.txt").write_bytes(b"two")
        staged = device_files.stage(self.ctx, self.job, self.message(self.media_file("same.txt", b"one")), [])
        both = device_files.stage(
            make_context(self.root, device="gpu1", remote_workdir=str(self.proj), state_dir=self.state,
                         history_path=self.topic / "history.jsonl"),
            self.job, self.message(self.media / "same.txt"), [])
        self.assertEqual(staged.put[0][2], both.put[0][2])  # The same file again: the same copy.
        state_media = self.state / "incoming_media"
        state_media.mkdir()
        (state_media / "same.txt").write_bytes(b"three")
        both = device_files.stage(self.ctx, self.job, self.message(self.media / "same.txt", state_media / "same.txt"), [])
        paths = [path for _, _, path in both.put]
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(sorted(Path(path).read_bytes() for path in paths), [b"one", b"three"])

    def test_a_device_that_does_not_answer_skips_every_file_without_trying_them_all(self):
        files = [self.media_file("%d.txt" % number) for number in range(3)]
        self.device.faults.fail(1)
        staged = device_files.stage(self.ctx, self.job, self.message(*files), [{"path": str(files[0]), "mime": "image/png"}])
        self.assertEqual(staged.put, [])
        self.assertEqual(len(staged.skipped), 3)
        self.assertTrue(all("设备 gpu1 没有答复" in why for _, _, why in staged.skipped), staged.skipped)
        self.assertEqual(self.device.faults.failed, 1, "one failed call, then the rest are not tried")
        for hub in (str(self.media), "incoming_media"):
            self.assertNotIn(hub, staged.prompt)
        self.assertEqual(staged.attachments[0]["shown"], device_files.NOT_PUT)
        self.assertNotIn("device_path", staged.attachments[0])
        self.assertEqual(staged.directory, "")

    def test_a_path_of_the_media_directory_the_words_do_not_name_as_a_path_is_not_left_in_the_prompt(self):
        doc = self.media_file("n.txt")
        staged = device_files.stage(self.ctx, self.job, self.message(doc) + "\nalso %s/other.txt here" % self.media, [])
        self.assertNotIn("incoming_media", staged.prompt)
        self.assertIn(device_files.NOT_PUT + " here", staged.prompt)

    def test_a_link_out_of_the_media_directory_is_not_put(self):
        secret = self.root / "secret.txt"
        secret.write_text("hub secret")
        (self.media / "link.txt").symlink_to(secret)
        staged = device_files.stage(self.ctx, self.job, self.message(self.media / "link.txt"), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(staged.put, [])

    def test_without_a_home_in_the_profile_the_device_says_where_its_home_is(self):
        (devices.device_dir(self.state, "gpu1") / "profile.json").write_text("{}")
        staged = device_files.stage(self.ctx, self.job, self.message(self.media_file("a.txt")), [])
        self.assertTrue(staged.directory.startswith(str(self.device.home) + "/.covey/incoming/"), staged.directory)
        self.assertEqual(len(staged.put), 1)

    def test_the_codex_images_are_the_devices_copies_and_never_the_hubs_path(self):
        ctx = self.ctx
        items = [{"path": "/hub/a.png", "mime": "image/png", "device_path": "/dev/a.png"},
                 {"path": "/hub/b.png", "mime": "image/png", "shown": device_files.NOT_PUT}]
        self.assertEqual(image_paths(ctx, items), ["/dev/a.png"])
        self.assertEqual(image_paths(make_context(self.root), items), ["/hub/a.png", "/hub/b.png"])


class ArtifactTest(Fixture):
    def setUp(self):
        super().setUp()
        (self.proj / "out").mkdir()
        (self.proj / "out" / "result.txt").write_text("device result")
        self.cp = self.root / "cp.json"
        self.listed = ["out/result.txt"]
        self.write_cp()

    def write_cp(self, **fields):
        self.cp.write_text(json.dumps({"artifacts": self.listed, **fields}))

    def record(self, **fields):
        return {"key": "keyrec", "job_id": 7, "device": "gpu1", "remote_workdir": str(self.proj),
                "workdir": str(self.standin / "abc"), "checkpoint_path": str(self.cp), **fields}

    def resolve(self, raw, record=None, **kwargs):
        record = record or self.record()
        if raw not in artifact_paths(record):
            self.listed = [raw]
            self.write_cp()
        return resolve_artifact(record, artifact_key(raw), state_dir=self.state, **kwargs)

    def refused(self, raw, pattern, record=None):
        with self.assertRaisesRegex(ValueError, pattern):
            self.resolve(raw, record)

    def test_the_listed_artifact_is_copied_to_the_hub_and_is_the_file_the_upload_goes_on_with(self):
        with self.guard() as guard:
            path = self.resolve("out/result.txt")
        guard.assert_untouched(self)
        folder = self.state / "runs" / "keyrec" / "device-files"
        self.assertEqual(path.parent.parent, folder)
        self.assertEqual((path.name, path.read_text()), ("result.txt", "device result"))
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual([call for call in self.calls if call[0] == "get"], [("get", str(self.proj / "out" / "result.txt"))])

    def test_an_absolute_path_and_one_with_a_dot_dot_inside_the_directory_work_the_same(self):
        for raw in (str(self.proj / "out" / "result.txt"), "out/../out/result.txt", "~/proj/out/result.txt"):
            self.listed = [raw]
            self.write_cp()
            self.assertEqual(self.resolve(raw).read_text(), "device result", raw)

    def test_the_stat_carries_the_denylist_and_the_directory_the_device_judges_against(self):
        self.resolve("out/result.txt")
        fields = self.stat_calls()[-1]
        self.assertEqual(fields["root"], str(self.proj))
        self.assertEqual(fields["base"], str(self.proj))
        self.assertTrue(SENSITIVE_NAMES <= set(fields["deny_names"]), fields["deny_names"])
        self.assertTrue({".env", ".env.*", ".local-state"} <= set(fields["deny_names"]))
        for name in artifact_policy.AGENT_DIRS:
            self.assertIn("~/" + name, fields["deny_dirs"])

    def test_a_configuration_or_credential_path_is_refused_whatever_way_it_is_written(self):
        for where in (".ssh", ".claude", ".git", ".codex", "sub/.ssh"):
            (self.proj / where).mkdir(parents=True, exist_ok=True)
            (self.proj / where / "key").write_text("secret")
        for name in (".env", ".env.local", "auth.json", "credentials.json"):
            (self.proj / name).write_text("secret")
        for raw in (".ssh/key", ".claude/key", ".git/key", ".codex/key", "sub/.ssh/key", ".env", ".env.local", "auth.json",
                    "credentials.json", "out/../.ssh/key", str(self.proj / ".ssh" / "key"), "~/proj/.env"):
            self.listed = [raw]
            self.write_cp()
            self.assertTrue(config_path(self.record(), raw), raw)
            self.assertEqual(drive_paths(self.record()), [], raw)
            self.refused(raw, "配置或凭据")
        self.assertEqual([call for call in self.calls if call[0] == "get"], [], "nothing was fetched")

    def test_a_link_inside_the_directory_to_a_credential_is_refused_by_where_it_leads(self):
        (self.proj / ".ssh").mkdir()
        (self.proj / ".ssh" / "id").write_text("secret")
        (self.proj / "innocent.txt").symlink_to(self.proj / ".ssh" / "id")
        self.listed = ["innocent.txt"]
        self.write_cp()
        self.assertEqual(drive_paths(self.record()), ["innocent.txt"], "its name alone is no config path")
        self.refused("innocent.txt", "配置或凭据")
        self.assertEqual([call for call in self.calls if call[0] == "get"], [])

    def test_the_hub_judges_the_real_path_the_device_reports_once_more(self):
        """A device (or a shim without the denylist) that does not deny: the hub's own rule still does, part by part."""
        (self.proj / "plain.txt").write_text("x")
        for real in ("/home/me/.ssh/id", "/home/me/.claude/x", "/p/.env.prod", "/p/.local-state/y", "/p/.git/config"):
            answer = [{"path": "/p/plain.txt", "realpath": real, "exists": True, "is_file": True, "size": 1, "denied": False,
                       "escapes": False}]
            with mock.patch.object(device_files, "look", return_value=answer):
                self.refused("plain.txt", "配置或凭据")
        self.assertEqual([call for call in self.calls if call[0] == "get"], [])
        # The same answer with a clean real path goes through: the check is the path, not the call.
        answer = [{"path": "/p/plain.txt", "realpath": str(self.proj / "plain.txt"), "exists": True, "is_file": True,
                   "size": 1, "denied": False, "escapes": False}]
        with mock.patch.object(device_files, "look", return_value=answer):
            self.assertEqual(self.resolve("plain.txt").read_text(), "x")

    def test_a_file_outside_the_devices_working_directory_is_refused_a_link_out_of_it_too(self):
        outside = self.device.home / "elsewhere.txt"
        outside.write_text("not in the project")
        (self.proj / "out" / "leak.txt").symlink_to(outside)
        for raw in (str(outside), "../elsewhere.txt", "out/leak.txt"):
            self.listed = [raw]
            self.write_cp()
            self.refused(raw, "工作区之外")
        self.assertEqual([call for call in self.calls if call[0] == "get"], [])

    def test_a_missing_file_a_directory_an_empty_file_and_a_big_one_each_say_what_is_wrong(self):
        (self.proj / "empty.txt").write_text("")
        (self.proj / "big.txt").write_bytes(b"x" * 50)
        self.refused("nothing.txt", "产物文件不存在")
        self.refused("out", "目录暂不支持")
        self.refused("empty.txt", "非空")
        with self.assertRaisesRegex(ValueError, "非空且不超过 1 MB"):
            self.resolve_with_limit("big.txt", 1024 * 1024)
        self.assertEqual([call for call in self.calls if call[0] == "get"], [])

    def resolve_with_limit(self, raw, limit):
        self.listed = [raw]
        self.write_cp()
        with mock.patch.object(device_files, "look", return_value=[{
                "path": raw, "realpath": str(self.proj / raw), "exists": True, "is_file": True, "size": limit + 1,
                "denied": False, "escapes": False}]):
            return resolve_artifact(self.record(), artifact_key(raw), limit, state_dir=self.state)

    def test_a_file_that_grew_past_the_limit_after_the_stat_is_not_read_to_the_end(self):
        self.listed = ["out/result.txt"]
        with mock.patch.object(device_files, "look", return_value=[{
                "path": "x", "realpath": str(self.proj / "out" / "result.txt"), "exists": True, "is_file": True, "size": 3,
                "denied": False, "escapes": False}]):
            with self.assertRaisesRegex(ValueError, "不超过 0 MB"):
                resolve_artifact(self.record(), artifact_key("out/result.txt"), 3, state_dir=self.state)

    def test_a_hub_file_of_the_same_path_is_not_the_devices_artifact(self):
        hub_file = self.root / "plot.txt"
        hub_file.write_text("the hub's own")
        self.listed = [str(hub_file)]
        self.write_cp()
        self.assertIn(str(hub_file), artifact_paths(self.record()))
        self.refused(str(hub_file), "工作区之外|产物文件不存在")
        self.assertFalse(list((self.state / "runs").glob("**/plot.txt")) if (self.state / "runs").exists() else [])

    def test_the_hubs_own_files_in_the_record_are_still_resolved_here(self):
        result = self.root / "result-files" / "k.md"
        result.parent.mkdir()
        result.write_text("a long reply")
        record = self.record(result_file_path=str(result))
        self.assertEqual(artifact_paths(record)[-1], str(result))
        self.assertEqual(self.resolve(str(result), record), result)
        self.assertEqual(self.calls, [])
        self.assertFalse(config_path(record, str(result)))

    def test_a_device_path_is_judged_as_text_the_hubs_home_and_links_play_no_part(self):
        record = self.record()
        path = artifact_policy._local_path(record, "~/notes.md")
        self.assertEqual((type(path), str(path)), (PurePosixPath, "~/notes.md"))
        self.assertEqual(str(artifact_policy._local_path(record, "a/../b.txt")), str(self.proj / "b.txt"))
        self.assertEqual(str(artifact_policy._local_path(record, "/x//y/./z")), "/x/y/z")
        self.assertFalse(config_path(record, "~/notes.md"))
        self.assertTrue(config_path(record, "~/.ssh/id_rsa"))
        # The checkpoint's artifacts/ of the hub is not where a device's files are.
        inside = str(self.cp.parent / "artifacts" / ".local-state" / "f")
        self.assertTrue(config_path(record, inside))

    def test_a_device_that_does_not_answer_is_the_files_error_on_the_card(self):
        self.device.faults.fail(1)
        self.refused("out/result.txt", "设备 gpu1 没有答复")

    def test_the_copy_is_what_goes_to_drive_and_a_second_upload_of_the_same_content_is_skipped(self):
        from covey.app.artifacts import Artifacts

        uploaded = []
        owner = mock.Mock(voice=mock.Mock(safe_key=lambda value: value))
        ctx = make_context(self.root, device="gpu1", remote_workdir=str(self.proj), state_dir=self.state)
        subject = Artifacts.__new__(Artifacts)
        subject.voice, subject.runtime = owner.voice, mock.Mock()

        def upload(profile, path, folder, log=None):
            uploaded.append((path.name, path.read_bytes()))
            return {"file_token": "tok", "url": "https://drive/f", "file_name": path.name}

        ledger = {"folder_token": "fld", "folder_url": "u", "files": {}, "errors": {}}
        with mock.patch.object(artifacts, "drive_upload", upload):
            for _ in range(2):
                source = resolve_artifact(self.record(), artifact_key("out/result.txt"), state_dir=ctx.state_dir)
                subject._upload_one(ctx, ledger, artifact_key("out/result.txt"), source)
        self.assertEqual(uploaded, [("result.txt", b"device result")])
        self.assertEqual(ledger["files"][artifact_key("out/result.txt")]["name"], "result.txt")


class ImageTest(Fixture):
    def setUp(self):
        super().setUp()
        (self.proj / "shot.png").write_bytes(PNG)
        self.images = self.proj / "images"
        self.images.mkdir()

    def wanted(self, entries):
        return self.sender().remote_wanted({"show_images": entries})

    def sender(self, messenger=None, log=lambda text: None):
        return turn_images.TurnImages(self.ctx, self.job, messenger=messenger or mock.Mock(), voice=mock.Mock(), log=log)

    def test_only_the_devices_absolute_image_files_the_hub_would_show_are_wanted(self):
        (self.proj / "notes.txt").write_text("x")
        (self.proj / ".ssh").mkdir()
        (self.proj / ".ssh" / "qr.png").write_bytes(PNG)
        (self.images / "big.png").write_bytes(b"x" * 40)
        with mock.patch.object(turn_images, "MAX_BYTES", 30):
            found = self.wanted([str(self.proj / "shot.png"), "shot.png", str(self.proj / "notes.txt"),
                                 str(self.proj / "gone.png"), str(self.proj / ".ssh" / "qr.png"),
                                 str(self.images / "big.png"), {"path": str(self.proj / "shot.png"), "note": "  扫码  登录 ",
                                                                "urgent": True}, {"note": "x"}, 7])
        self.assertEqual([(image.real, note, urgent) for image, note, urgent in found],
                         [(str(self.proj / "shot.png"), "", False), (str(self.proj / "shot.png"), "扫码 登录", True)])
        self.assertEqual(len(self.wanted([str(self.proj / "shot.png")] * (turn_images.MOST + 3))), turn_images.MOST)
        self.assertEqual(self.wanted([]), [])
        self.assertEqual(self.sender().remote_wanted({}), [])
        self.assertEqual([call for call in self.calls if call[0] == "get"], [])

    def test_an_image_anywhere_on_the_device_may_be_shown_as_on_this_machine(self):
        outside = self.device.home / "qr.png"
        outside.write_bytes(PNG)
        self.assertEqual([image.real for image, _, _ in self.wanted([str(outside)])], [str(outside)])

    def test_the_digest_follows_the_file_and_the_device_not_the_hubs_files(self):
        path = str(self.proj / "shot.png")
        first = self.wanted([path])[0][0]
        self.assertEqual(first.digest(), self.wanted([path])[0][0].digest())
        (self.proj / "shot.png").write_bytes(PNG + b" again, longer")
        self.assertNotEqual(first.digest(), self.wanted([path])[0][0].digest())
        self.assertEqual(turn_images.digest(first), first.digest())
        other = device_files.RemoteImage("gpu2", first.real, first.size, first.mtime)
        self.assertNotEqual(other.digest(), first.digest())

    def test_the_turns_images_are_sent_from_the_hubs_copy_once_each(self):
        messenger = mock.Mock()
        sent = []
        messenger.send_image.side_effect = lambda profile, chat, path, key, **kw: sent.append(
            (Path(path), Path(path).read_bytes(), key)) or "om_image_1"
        image = self.sender(messenger)
        checkpoint = {"show_images": [{"path": str(self.proj / "shot.png"), "note": "QR"}]}
        with mock.patch.object(turn_images.background, "start", side_effect=lambda kind, fn, *args: fn(*args)):
            with self.guard() as guard:
                image(checkpoint)
                image(checkpoint)
        guard.assert_untouched(self)
        self.assertEqual(len(sent), 1, "the same image of the device is not sent twice")
        path, data, _ = sent[0]
        self.assertEqual(data, PNG)
        self.assertEqual(path.parent.parent, self.state / "runs" / self.key / "device-files")
        self.assertEqual(path.name, "shot.png")

    def test_a_device_that_does_not_answer_sends_nothing_and_does_not_raise(self):
        logs = []
        messenger = mock.Mock()
        image = self.sender(messenger, logs.append)
        self.device.faults.fail(1)
        image({"show_images": [str(self.proj / "shot.png")]})
        messenger.send_image.assert_not_called()
        self.assertTrue(any(line.startswith("images not sent") for line in logs), logs)


class TheBoxTest(Fixture):
    def test_the_input_output_box_of_a_device_job_has_no_file_of_the_device_in_it(self):
        from covey.app import answer_cards

        job = make_job(job_id=7, message_id="om_job", workspace_label="gpu1-proj", model="m", effort="high",
                       checkpoint_path=str(self.root / "cp.json"))
        harness = mock.Mock(spec=["spec"])
        harness.spec = mock.Mock(title="Claude Code", spec=["title", "name"])
        harness.spec.name = "claude"
        with self.guard() as guard:
            lines = answer_cards.inputs(job, harness, ctx=self.ctx)
            out = answer_cards.outputs(job, {})
            changed = answer_cards.files_changed(job)
        guard.assert_untouched(self)
        text = json.dumps([lines, out])
        self.assertIn("gpu1-proj", text)
        self.assertNotIn(str(self.proj), text)
        self.assertNotIn(str(self.standin), text)
        self.assertNotIn(" MB", text)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()

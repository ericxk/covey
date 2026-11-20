"""下载请求与结果: the element on an ended task card, the click and the packed file (app/turn_export.py)."""
import json
import lzma
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.fake_lark import FakeCardKit  # noqa: E402
from support.world import UnifiedCase, wait_for  # noqa: E402
from support.records import write_topic
from covey.app.turn_export import write_xz  # noqa: E402
from covey.cards.turn_export import KIND, export_panel  # noqa: E402
from covey.core.harness import HarnessCapabilities  # noqa: E402
from covey.core.jobs import find_job_record, upsert_job_record  # noqa: E402
from covey.core.topics import read_topic  # noqa: E402

SID = "11111111-1111-4111-8111-111111111111"


def walk(element):
    yield element
    for column in element.get("columns") or ():
        for child in column.get("elements") or ():
            yield from walk(child)


def buttons(panel):
    return [e for e in walk(panel) if e.get("tag") == "button"]


def text(panel):
    return "\n".join(e["content"] for e in walk(panel) if e.get("tag") == "markdown")


class ExportPanelTests(unittest.TestCase):
    def test_each_state(self):
        [button] = buttons(export_panel("t", 7))
        self.assertEqual(button["text"]["content"], "下载请求与结果")
        self.assertEqual(button["behaviors"][0]["value"], {"kind": KIND, "topic_id": "t", "job_id": 7})
        self.assertEqual(text(export_panel("t", 7)), "")
        # Sized to its label like the 🔁 📦 buttons above it: in an auto column, not the card-wide one.
        [holder] = [e for e in walk(export_panel("t", 7)) if button in [x for c in e.get("columns") or ()
                                                                         for x in c["elements"]]]
        self.assertEqual([c["width"] for c in holder["columns"]], ["auto"])
        building = export_panel("t", 7, {"status": "building"})
        self.assertIn("正在打包", text(building))
        self.assertNotIn("disabled", buttons(building)[0])  # A 'building' cut off by a restart is clicked again.
        done = export_panel("t", 7, {"status": "done", "url": "https://x/file/abc", "at": 0, "name": "a.json.xz",
                                     "requests": 3, "size": 2048, "raw_size": 5 * 1024 * 1024})
        self.assertEqual(buttons(done)[0]["behaviors"], [{"type": "open_url", "default_url": "https://x/file/abc"}])
        self.assertIn("[a.json.xz](https://x/file/abc) · 3 次请求 · 2 KB（解压后 5.0 MB）", text(done))
        self.assertIn("可以再点一次", text(export_panel("t", 7, {"status": "failed", "error": "找不到会话记录"})))

    def test_the_file_is_json_in_xz(self):
        path = Path(self.id().replace(".", "_") + ".json.xz")
        tmp = Path(__import__("tempfile").mkdtemp())
        path = tmp / path
        data = {"main": {"requests": [{"request": {"messages": ["你好" * 1000]}}] * 50}}
        raw = write_xz(data, path)
        self.assertEqual(json.loads(lzma.open(path).read()), data)
        self.assertEqual(raw, len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()))
        self.assertLess(path.stat().st_size, raw / 20)
        path.unlink()
        tmp.rmdir()


class TurnExportTests(UnifiedCase):
    def setUp(self):
        super().setUp()
        self.receive('src', 'first question')
        _, source = self.route('src')
        data = read_topic(source)
        data.update(harness_id='claude', model='claude-model', effort='high', model_settings_snapshot=True,
                    workspace_locked=True, harness_locked=True)
        write_topic(source, data)
        self.app_, self.source = self.route('src')
        self.record = upsert_job_record(self.source.state_dir, 'job-1', job_id=1, status='done', chat_id='chat',
                                        topic_id=self.source.topic_id, card_id='card-1', card_message_id='card',
                                        session_id=SID, timings={'backend_requested': 100.0, 'execution_finished': 160.0})
        self.uploads, self.folders = [], []

    def upload(self, profile, *args, cwd=None, timeout=120, log=None):
        if args[0] == "+create-folder":
            self.folders.append(args)
            token = f"fld{len(self.folders)}"
            return {"folder_token": token, "url": f"https://x/folder/{token}"}
        if args[0] == "+member-add":
            return {"perm": "full_access"}
        name = args[args.index("--file") + 1]
        self.uploads.append((args, json.loads(lzma.open(Path(cwd) / name).read())))
        return {"file_name": name, "url": "https://x/file/abc", "file_token": "tok"}

    def click(self, export):
        event = {'chat_id': 'chat', 'event_id': 'click-1', 'message_id': 'card',
                 'action_value': {'kind': KIND, 'topic_id': self.source.topic_id, 'job_id': 1}}
        backend = type(self.app_.backend)
        with FakeCardKit({'code': 0}) as lark, mock.patch.object(backend, 'export_turn', create=True, side_effect=export) as call, \
                mock.patch('covey.app.artifacts.drive_call', side_effect=self.upload), \
                mock.patch.object(type(self.app_.harness), 'topic_record', return_value=self.record):
            self.assertTrue(self.app_.turn_export.export_action(self.source, event))
            wait_for(self, lambda: (find_job_record(self.source.state_dir, 'job-1') or {}).get('turn_export', {})
                     .get('status') in {'done', 'failed'} and not self.app_.turn_export.running)
        puts = [json.loads(c.data['element']) for c in lark.calls if c.method == 'PUT']
        self.assertTrue(all(c.path.endswith('/elements/turn_export') for c in lark.calls if c.method == 'PUT'))
        return find_job_record(self.source.state_dir, 'job-1')['turn_export'], puts, call

    def test_a_click_packs_the_turn_uploads_it_and_the_button_opens_it(self):
        main = {"requests": [{"digest_verified": True}, {"digest_verified": False}], "rows": [{"type": "user"}]}
        state, puts, call = self.click(lambda ctx, sid, since, until: {"format": "f", "main": main,
                                                                      "subagents": [{"requests": [{}]}]})
        self.assertEqual(call.call_args.args[1:], (SID, 100.0, 160.0))
        self.assertEqual((state['status'], state['url'], state['requests'], state['verified'], state['subagents']),
                         ('done', 'https://x/file/abc', 3, 1, 1))
        [(args, packed)] = self.uploads
        # Into today's folder under the 产物 root, not into the root itself (it fills up: 1500 children at most).
        root, day = self.folders
        self.assertNotIn("--folder-token", root)
        self.assertEqual(day[day.index("--name") + 1], time.strftime("%Y-%m-%d"))
        self.assertEqual(day[day.index("--folder-token") + 1], 'fld1')
        self.assertEqual(args[args.index("--folder-token") + 1], 'fld2')
        self.assertRegex(args[args.index("--name") + 1], r'^T\d+-#1-请求与结果-\d{8}-\d{6}\.json\.xz$')
        self.assertEqual((packed['main'], packed['task']['job_id'], packed['task']['session_id']), (main, 1, SID))
        self.assertIn('正在打包', text(puts[0]))
        self.assertEqual(buttons(puts[-1])[0]['behaviors'][0], {'type': 'open_url', 'default_url': 'https://x/file/abc'})

    def test_a_failure_says_why_on_the_card(self):
        def missing(*args):
            raise ValueError("找不到本轮的 Claude Code 会话记录")
        state, puts, _ = self.click(missing)
        self.assertEqual((state['status'], state['error']), ('failed', '找不到本轮的 Claude Code 会话记录'))
        self.assertIn('打包没有完成：找不到本轮的 Claude Code 会话记录', text(puts[-1]))
        self.assertEqual(self.uploads, [])

    def test_only_a_turn_with_a_session_on_a_harness_that_exports_gets_the_button(self):
        job = SimpleNamespace(progress=object(), export_controls_attached=False, session_id=SID, job_id=1)
        with mock.patch.object(type(self.app_.cards), 'set_task_element', return_value=True) as put:
            self.app_.turn_export.attach_export_controls(self.source, job)
            self.assertEqual(put.call_args.args[2]['element_id'], 'turn_export')
            self.assertTrue(put.call_args.kwargs['at_bottom'])
            for changes in ({'session_id': None}, {}):
                job = SimpleNamespace(progress=object(), export_controls_attached=False, session_id=SID, job_id=1)
                job.__dict__.update(changes)
                with mock.patch.object(type(self.app_.backend), 'capabilities',
                                       return_value=HarnessCapabilities() if not changes else
                                       self.app_.backend.capabilities(self.source)):
                    put.reset_mock()
                    self.app_.turn_export.attach_export_controls(self.source, job)
                    put.assert_not_called()


if __name__ == '__main__':
    unittest.main()

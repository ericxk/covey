"""菜单「看板」 on its own (COVEY_BOARD=1, core/app_manifest.py board_switch; ./install writes it): the board without
早晚汇总, which stays its own switch, and a deployment that has 看板 through the digest keeps it. Before T604 看板 came
only with COVEY_DIGEST_TIMES, so a new install had none."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.app.digest import RECORD, Digest
from covey.core import app_manifest as manifest
from covey.service import installer

CHAT = "oc_dm"


def menu(env):
    return [need.name for need in manifest.required(env) if need.kind == "menu"]


class ManifestTest(unittest.TestCase):
    def test_the_app_needs_the_item_with_the_switch_or_the_digest_as_before(self):
        board = ["weekly_usage", "digest_board", "bridge_settings"]
        with mock.patch.object(manifest, "proxy_sender", return_value=False):
            self.assertEqual(menu({}), ["weekly_usage", "bridge_settings"])
            self.assertEqual(menu({"COVEY_BOARD": "0"}), ["weekly_usage", "bridge_settings"])
            self.assertEqual(menu({"COVEY_BOARD": "1"}), board)
            self.assertEqual(menu({"COVEY_DIGEST_TIMES": "09:30"}), board)  # Through the digest, as before.
            self.assertEqual(menu({"COVEY_BOARD": "0", "COVEY_DIGEST_TIMES": "09:30"}), board)

    def test_a_new_install_has_it(self):
        self.assertEqual(installer.INSTALL_SWITCHES["COVEY_BOARD"], "1")


class BoardMenuTest(UnifiedCase):
    def test_with_the_switch_the_menu_has_the_board_and_no_digest_runs(self):
        self.assertNotIn("digest_board", self.app.menu.actions)  # Neither set: as before.
        with mock.patch.dict(os.environ, {"COVEY_BOARD": "1"}):
            app = type(self.app)(script_path=self.app.runtime.script_path)
            self.assertEqual(app.digest.threads(), ())  # No 早晚汇总: no thread, nothing sent at any time.
        self.assertEqual((app.menu.actions["digest_board"].__func__, app.menu.folds["digest_board"].__func__),
                         (Digest.board, Digest.fold_board))

    def test_a_click_sends_the_board_of_the_last_day_and_writes_no_digest_record(self):
        puts = []
        messenger = self.app.messenger
        messenger.create_and_send_card = mock.Mock(side_effect=lambda profile, chat, card, key, **kw: (
            puts.append(card), ("bcard", f"om_board{len(puts)}"))[1])
        self.app.runtime.access.remember_owner_chat(CHAT)
        self.app.digest.link = lambda ctx, chat, text, cards=None: text  # No T links to look up.
        now = time.time()
        with mock.patch.dict(os.environ, {"COVEY_BOARD": "1"}):
            self.app.menu.actions.update(self.app.digest.menu_items())  # As a Bridge launched with the switch.
            self.app.menu.folds.update(self.app.digest.menu_folds())
            self.assertEqual(self.app.digest.board_view(self.ctx, CHAT, now=now)["since"], now - 86400)
            self.app.handle_menu_event({"event_id": "ev1", "event_key": "digest_board", "operator_open_id": "owner"},
                                       self.ctx)
        outcomes = [line.split("outcome=")[1] for line in self.logs if line.startswith("bot menu key=")]
        self.assertEqual((outcomes, len(puts)), (["digest_board"], 1))
        self.assertIn("都在掌握中", json.dumps(puts[0], ensure_ascii=False))
        self.assertFalse((self.ctx.state_dir / RECORD).exists())


if __name__ == "__main__":
    unittest.main()

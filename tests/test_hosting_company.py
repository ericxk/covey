# privacy: allow-file 测公司扩展的托管钩子对 --mode auto 的判断（开发机的 PID 1 识别），这些名字是平台接口
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import requires_extension


class HookAutoTests(unittest.TestCase):
    @requires_extension()
    def test_company_hook_applies_only_under_the_merlin_init(self):
        from covey_internal import hosting
        for argv, devbox, expected in [(['python', hosting.MERLIN_INIT], 'box-1', 'merlin-pid1'),
                                       (['python', hosting.MERLIN_INIT], '', None),
                                       (['/sbin/init'], 'box-1', None)]:
            with mock.patch.object(hosting, 'pid1_context', return_value=(argv, devbox)):
                self.assertEqual(hosting.auto_mode(), expected)
        with mock.patch.object(hosting, 'pid1_context', side_effect=FileNotFoundError):
            self.assertIsNone(hosting.auto_mode())


if __name__ == '__main__':
    unittest.main()

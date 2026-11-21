"""Fixed public failure summaries; no backend, process or transport is started."""
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from support import requires_extension
from covey.core.result import (DETAILS, ERROR_CODES, FAILURES, NOTICE, RECOVERY_DEFAULT, ExecutionOutcome,
                                        VerifiedRootReply)
from covey.core.saved_results import display_saved_reply


class FixedFailureNoticeTests(unittest.TestCase):
    def test_both_failure_tables_word_only_known_error_codes(self):
        # Two wordings of the same codes for now (DETAILS after a label, FAILURES whole with {label}); neither
        # may word a code no outcome can carry.
        self.assertLessEqual(set(DETAILS), ERROR_CODES)
        self.assertLessEqual(set(FAILURES), ERROR_CODES)
        self.assertTrue(all("{label}" in text for text in FAILURES.values()))

    @requires_extension()
    def test_each_fixed_error_code_has_a_specific_safe_detail_without_rewriting_root(self):
        details = {
            "child_error": ("子任务", "失败"),
            "child_aborted": ("子任务", "中止"),
            "child_unknown": ("子任务", "无法确认"),
            "settle_timeout": ("等待时间", "未全部结束"),
            "root_failed": ("主任务", "失败"),
            "root_incomplete": ("主任务", "无法确认"),
            "policy_refusal": ("明确拒绝", "停止自动继续"),
            "policy_fallback": ("明确降级", "停止自动继续"),
            "cancelled": ("执行已取消",),
            "timeout": ("任务执行时间", "已到"),
            "transport_error": ("连接", "异常"),
            "cleanup_unconfirmed": ("清理", "尚未确认"),
            "protocol_error": ("返回的信息", "未通过核对"),
            "session_guard": ("原生会话", "安全续接条件"),
            "process_exit": ("执行进程", "未正常退出"),
            "identity_mismatch": ("运行与回复身份", "未通过核对"),
            "output_missing": ("未取得", "完整的最终输出"),
            "storage_failed": ("结果保存", "未完成"),
            "max_turns": ("轮数", "上限"),
            "budget_exceeded": ("费用预算", "上限"),
            "authentication_error": ("身份或权限", "授权"),
            "rate_limited": ("限流", "额度"),
            "server_fault": ("临时故障", "连接中断"),
            "workspace_routing_timeout": ("工作区路由", "超时", "代理"),
            "workdir_missing": ("工作目录不存在", "检查并继续"),
            "background_timeout": ("后台任务", "时间上限", "已停止"),
            "quota_exhausted": ("额度", "已用完"),
            "model_blocked": ("安全拒绝", "不是限流"),
            "redaction_failed": ("敏感词脱敏", "没有发给模型服务"),
            "read_only_refused": ("群成员只读", "后端没有启动"),
        }
        self.assertEqual(set(details), set(ERROR_CODES) - {""})
        reply = VerifiedRootReply("maso", "11111111-2222-4333-8444-555555555555",
            "synthetic-session", ("synthetic-user",), ("synthetic-root",), "  Exact root body\nfinal line  ")
        generic = ExecutionOutcome("failed").notice()
        for code, tokens in details.items():
            with self.subTest(code=code):
                status = {"cancelled": "cancelled", "timeout": "timed_out"}.get(code, "failed")
                outcome = ExecutionOutcome(status, code)
                notice = outcome.notice()
                for token in tokens:
                    self.assertIn(token, notice)
                self.assertNotEqual(notice, generic)
                # A code with its own lead-in says how to go on instead.
                self.assertIn(NOTICE.get(code, "原生会话不可续接"), notice)
                self.assertNotIn(reply.session_id, notice)
                self.assertNotIn(reply.run_id, notice)
                self.assertEqual(display_saved_reply(reply, outcome), notice + "\n\n" + reply.text)
                self.assertFalse(outcome.delivery_authorized)
        with self.assertRaises(ValueError):
            ExecutionOutcome("failed", "private arbitrary exception body")

    def test_a_rate_limit_failure_says_why_and_how_to_go_on_in_plain_words(self):
        reply = ExecutionOutcome("failed", "rate_limited").failure_reply()
        self.assertTrue(reply.startswith("本轮任务未全部完成。模型服务限流：请求或 token 速率超过了上游限制"))
        for token in ("怎么继续", "几分钟", "在本话题直接发一条消息", "新会话", "🔁 换配置"):
            self.assertIn(token, reply)
        self.assertNotIn("原生会话不可续接", reply)
        # Other failures keep their wording.
        self.assertEqual(ExecutionOutcome("failed", "root_failed").failure_reply(),
                         "本轮任务未全部完成。主任务后续执行失败。未取得可交付的已验证公开回复，原生会话不可续接。")
        self.assertEqual(ExecutionOutcome("completed").failure_reply(), RECOVERY_DEFAULT)

    def test_a_background_wait_limit_says_what_was_stopped_and_how_to_go_on(self):
        outcome = ExecutionOutcome("timed_out", "background_timeout")
        notice = outcome.notice()
        self.assertTrue(notice.startswith("本轮任务已超时。回复之后仍有后台任务在运行，已到任务的时间上限"))
        for token in ("以下是本轮最后一条完整回复", "在本话题发一条消息", "新会话", "不会自动重跑"):
            self.assertIn(token, notice)
        self.assertNotIn("原生会话不可续接", notice)
        # The bridge serves every workspace: nothing here speaks of what the task was doing.
        for word in ("训练", "外部任务"):
            self.assertNotIn(word, notice)
        reply = outcome.failure_reply()
        self.assertIn("本轮没有可以发出的完整回复", reply)
        self.assertNotIn("未取得可交付的已验证公开回复", reply)

    def test_a_reply_kept_from_a_stopped_task_says_so_and_how_to_go_on(self):
        notice = ExecutionOutcome("cancelled", "cancelled").notice()
        self.assertTrue(notice.startswith("本轮任务已停止。"))
        for token in ("以下是停止前最后一条完整回复", "在本话题发一条消息", "新会话"):
            self.assertIn(token, notice)
        self.assertNotIn("原生会话不可续接", notice)

    def test_a_routes_detail_replaces_only_the_shared_detail_and_never_a_policy_stop(self):
        outcome = ExecutionOutcome("failed", "rate_limited")
        reply = outcome.failure_reply("Acme 当前繁忙。")
        self.assertEqual(reply, "本轮任务未全部完成。Acme 当前繁忙。" + outcome.failure_reply()[len(outcome.summary()):])
        self.assertNotIn("token 速率", reply)
        self.assertEqual(outcome.failure_reply(""), outcome.failure_reply())


if __name__ == "__main__":
    unittest.main()

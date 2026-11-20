"""Real-client review regressions: final-body selection and root metrics."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from covey.backends.claude.events import ClaudeEventAdapter
from covey.backends.claude.completion import ClaudeReplyState
from covey.backends.claude.usage import ClaudeTurnUsage
from support import require_extension

require_extension()  # Imports the company extension below.
from covey_internal.maso.reply import PublicReplyCapture
from covey.cards.review import answer_lead, compact_text
from covey.cards.streaming import card_title_for_question
from support.fake_backends import maso_message
from support.fake_backends import SID, assistant, result
from support.fake_backends import codex_context


class ReviewPresentationTests(unittest.TestCase):
    def test_maso_explicit_commentary_stays_out_of_final_but_unknown_plan_survives(self):
        capture = PublicReplyCapture('root')
        capture.start('root')
        progress = maso_message('p', 'Reading source now', tools=True)
        progress.extra['phase'] = ['commentary']
        capture.response('root', progress)
        capture.response('root', maso_message('plan', 'Plan to confirm', tools=True))
        capture.response('root', maso_message('final', 'Final finding'))
        capture.end('root')
        self.assertEqual(capture.completed_text('final'), 'Plan to confirm\n\nFinal finding')
        self.assertIn('p', capture.parts)

    def test_claude_tool_preambles_not_delivered_but_final_parts_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            state = ClaudeReplyState(codex_context(Path(folder), 'stream-json'))
            state.accept({'type': 'system', 'subtype': 'init', 'session_id': SID})
            state.accept(assistant('Reading source now', 'progress'))
            state.accept({'type': 'assistant', 'session_id': SID, 'uuid': 'tool',
                'message': {'id': 'native-tool', 'content': [
                    {'type': 'text', 'text': 'Checking another file'},
                    {'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}]}})
            state.accept(assistant('Conclusion part one', 'part1'))
            state.accept(assistant())
            state.accept(result())
            reply = state.capture()
            self.assertEqual(reply.text, 'Conclusion part one\n\nComplete answer')
            self.assertIn('progress', state.messages)
            state.result['result'] = 'unverified'
            self.assertIsNone(state.capture())

    def test_claude_web_target_is_useful_without_url_credentials(self):
        events = []
        adapter = ClaudeEventAdapter(lambda k, d: events.append((k, d)))
        adapter.accept({'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'id': 't', 'name': 'WebFetch',
            'input': {'url': 'https://user:SECRET@example.com/src/a.py?token=SECRET#SECRET'}}]}})
        tool = next(d for k, d in events if k == 'tool')
        self.assertEqual(tool['detail'], 'https://example.com/src/a.py')
        self.assertTrue(tool['public_progress'])
        self.assertNotIn('SECRET', str(events))


class CardTextTests(unittest.TestCase):
    """The 2026-10-27 UI review: titles with raw Markdown (P5) and a finished card's body (P1)."""

    def test_a_title_reads_as_plain_words_without_markdown_or_secrets(self):
        # "T137 · #847 · - **RL数据生产**： - UserFeedback 4000...", "T130 · #820 · ```PYTHON export OPENAI_API_KEY=$...".
        self.assertEqual(compact_text("- **RL数据生产**：\n - UserFeedback 4000", 36), "RL数据生产： UserFeedback 4000")
        self.assertEqual(compact_text("```PYTHON\nexport OPENAI_API_KEY=sk-live-123\nrun()\n```", 36),
                         "export OPENAI_API_KEY=[已隐藏] run()")
        self.assertEqual(compact_text("## 看 [文档](https://x/y) 里 `cfg.yaml` 的 # 部分\n> 1. 引用", 36),
                         "看 文档 里 cfg.yaml 的 # 部分 引用")
        cut = compact_text("一" * 50, 36)
        self.assertEqual((len(cut), cut[-1]), (36, "…"))  # The same length as before, one mark for a cut.
        runtime = SimpleNamespace(compact_text=compact_text, default_title="Claude")
        self.assertEqual(card_title_for_question("**修复** `bug`", runtime), "修复 bug")
        self.assertEqual(card_title_for_question("```\n```", runtime), "Claude")
        # Two lines in the topic side panel, without an option's recommendation mark (2026-11-02 review: T290 #2383).
        title = card_title_for_question("远端旧分支 → 删两个已合并的，刘晓东那个留着（推荐）; 本机检出 → 清理 (Recommended)", runtime)
        self.assertEqual(title, "远端旧分支 → 删两个已合并的，刘晓东那个留着…")
        self.assertEqual(len(card_title_for_question("一" * 50, runtime)), 24)

    def test_an_answer_s_lead_is_its_heading_or_first_sentence(self):
        self.assertEqual(answer_lead("## 新样式任务卡已上线试用，目前只在两处打开\n\n只在 **T182** 打开。"),
                         "新样式任务卡已上线试用，目前只在两处打开")
        self.assertEqual(answer_lead("**结论：可以合并**\n\n细节"), "结论：可以合并")
        self.assertEqual(answer_lead("只在 **T182** 和 `验收` 群打开。其他话题不变。"), "只在 T182 和 验收 群打开")
        self.assertEqual(answer_lead("```python\nprint(1)\n```\n\nFixed it: tests pass. Next."), "Fixed it: tests pass")
        self.assertEqual(answer_lead("| a | b |\n|---|---|\n\n- 第一项：改了 A；\n- 第二项"), "第一项：改了 A")
        self.assertEqual(answer_lead("token=abc123 已更新。"), "token=[已隐藏] 已更新")
        for empty in ("", "  \n\n", "```\nonly code\n```"):
            self.assertEqual(answer_lead(empty), "")
        long = answer_lead("很" * 100)
        self.assertEqual((len(long), long[-1]), (60, "…"))


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.usage = ClaudeTurnUsage(lambda k, d: self.events.append((k, d)))
        self.usage.arm(10)

    def event(self, now, event, **extra):
        self.usage.accept({'type': 'stream_event', 'event': event, **extra}, now)

    def message(self, now, mid, usage, content=(), **extra):
        self.usage.accept({'type': 'assistant', 'message': {'id': mid, 'usage': usage,
            'content': list(content)}, **extra}, now)

    def test_deduplicate_message_usage_and_sum_disjoint_cache_buckets(self):
        counts = {'input_tokens': 10, 'cache_read_input_tokens': 20,
            'cache_creation_input_tokens': 30, 'cache_creation': {'ephemeral_5m_input_tokens': 30},
            'output_tokens': 4}
        self.event(12, {'type': 'message_start', 'message': {'id': 'm1', 'usage': counts}})
        self.message(14, 'm1', counts)
        self.event(14, {'type': 'message_delta', 'usage': {'output_tokens': 8}})
        self.message(14, 'm1', counts)
        self.message(15, 'child', {'input_tokens': 900, 'output_tokens': 900}, parent_tool_use_id='t')
        self.message(17, 'm2', {'input_tokens': 7, 'output_tokens': 3})
        usage = self.usage.last
        self.assertEqual((usage['input_tokens'], usage['output_tokens']), (67, 11))
        self.assertEqual(usage['generation_seconds'], 7)
        self.usage.accept({'type': 'result', 'usage': {'output_tokens': 99999}}, 20)
        self.assertEqual(self.usage.last, usage)

    def test_ttft_uses_first_nonempty_root_text_or_thinking_arrival_once(self):
        self.event(11, {'type': 'content_block_delta', 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}})
        self.event(12, {'delta': {'type': 'text_delta', 'text': ''}})
        self.event(13, {'delta': {'type': 'text_delta', 'text': 'child'}}, parent_tool_use_id='t')
        self.event(14, {'delta': {'type': 'thinking_delta', 'thinking': 'private'}})
        self.event(20, {'delta': {'type': 'text_delta', 'text': 'final'}})
        self.assertEqual(self.events, [('first_token', {'ttft_seconds': 4, 'ttft_source': 'claude_delta'})])
        self.assertNotIn('private', str(self.usage.__dict__))

    def test_a_tool_call_first_gives_no_ttft(self):
        # 执行指标口径: the first output was a tool call, so the first token is not known; a later
        # text delta would count the tool's run as time to first token.
        for streamed in (True, False):
            with self.subTest(streamed=streamed):
                self.setUp()
                if streamed:
                    self.event(12, {'type': 'content_block_start', 'index': 0,
                                    'content_block': {'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {}}})
                self.message(13, 'm1', {'input_tokens': 5, 'output_tokens': 2},
                             [{'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {}}])
                self.event(40, {'delta': {'type': 'text_delta', 'text': 'after the command'}})
                self.assertEqual([k for k, _ in self.events if k == 'first_token'], [])

    def test_parallel_tools_and_question_wait_excluded_as_union(self):
        self.message(12, 'm1', {'input_tokens': 5, 'output_tokens': 2}, [
            {'type': 'tool_use', 'id': 'read', 'name': 'Read'},
            {'type': 'tool_use', 'id': 'ask', 'name': 'AskUserQuestion'}])
        self.usage.accept({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'read'}]}}, 15)
        self.usage.accept({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'ask'}]}}, 30)
        self.message(33, 'm2', {'input_tokens': 8, 'output_tokens': 3})
        self.assertEqual(self.usage.last['generation_seconds'], 5)
        self.assertEqual(self.usage.last['output_tokens'], 5)

    def test_missing_invalid_or_unarmed_values_do_not_invent_metrics(self):
        self.message(12, 'm1', {'input_tokens': True, 'output_tokens': -1})
        self.assertIsNone(self.usage.last)
        self.message(13, 'm1', {'output_tokens': 2})
        self.assertNotIn('input_tokens', self.usage.last)
        usage = ClaudeTurnUsage(lambda *a: None)
        usage.accept({'type': 'assistant', 'message': {'id': 'm', 'usage': {'output_tokens': 2}}}, 100)
        self.assertNotIn('generation_seconds', usage.last)


if __name__ == '__main__':
    unittest.main()

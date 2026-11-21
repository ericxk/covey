"""本周用量 (app/weekly_usage.py, cards/weekly_usage.py): which job records count, how their tokens add up,
and the card the bot menu's 本周用量 sends to the owner's private chat."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from support.world import UnifiedCase
from covey.app.bot_menu import PANELS
from covey.app.weekly_usage import created_at, job_tokens, summarize
from covey.cards.weekly_usage import sections, total_tokens, usage_card, usage_text
from covey.core.jobs import upsert_job_record

NOW = 1793635200.0  # 2026-11-03T00:00:00+08:00
DAY = 86400
ZONE = "Asia/Shanghai"  # The days below are UTC+8's, as the card's days are the machine's local ones.
_zone = None


def setUpModule():
    global _zone
    _zone = os.environ.get("TZ")
    os.environ["TZ"] = ZONE
    time.tzset()


def tearDownModule():
    if _zone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _zone
    time.tzset()


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def job(harness="claude", status="done", ago=DAY, topic="t1", **fields):
    return {"harness_id": harness, "backend": harness, "status": status, "created_at": iso(NOW - ago),
            "topic_id": topic, **fields}


def ran(**tokens):
    """Fields of a job a backend ran, with these token counts."""
    return {"timings": {"backend_requested": 1.0}, "session_id": "s", **tokens}


TITLES = {"claude": "Claude", "codex": "Codex"}  # The routed Harnesses.


class SummaryTests(unittest.TestCase):
    def test_only_the_last_seven_days_and_jobs_a_backend_ran_count(self):
        records = [job(ago=DAY), job(ago=7 * DAY - 60), job(ago=7 * DAY + 60), job(ago=-60),
                   {**job(), "created_at": "not a time"}, {k: v for k, v in job().items() if k != "created_at"},
                   job(status="waiting_workspace"), job(status="cancelled", never_started=True),
                   job(status="cancelled", **ran())]
        summary = summarize(records, NOW, TITLES)
        # Two in the window, the cancelled one that ran; not the older one, the future one, the ones
        # without a time, the launch card still waiting nor the merged supplement.
        self.assertEqual(summary["tasks"], 3)
        self.assertEqual((summary["since"], summary["until"], summary["days"]), (NOW - 7 * DAY, NOW, 7))

    def test_topics_and_outcomes(self):
        records = [job(topic="a"), job(topic="a", status="continued"), job(topic="b", status="failed"),
                   job(topic="b", status="cancelled", **ran()), job(topic="", status="interrupted"),
                   job(topic="c", status="running"), job(topic="c", status="waiting_user"), job(status="odd")]
        summary = summarize(records, NOW, TITLES)
        self.assertEqual(summary["topics"], 4)
        self.assertEqual(dict(summary["outcomes"]), {"done": 1, "continued": 1, "failed": 1, "cancelled": 1,
                                                     "interrupted": 1, "active": 2, "other": 1})

    def test_tokens_add_subtasks_and_leave_out_jobs_without_counts(self):
        subtasks = {"count": 2, "levels": [{"input_tokens": 1000, "cache_read_tokens": 800, "output_tokens": 30},
                                           {"input_tokens": 500, "cache_read_tokens": None, "output_tokens": 20}]}
        main = job(input_tokens=10000, cache_read_tokens=9000, cache_write_tokens=500, output_tokens=200,
                   subtask_usage=subtasks)
        self.assertEqual(job_tokens(main), {"input_tokens": 11500, "cache_read_tokens": 9800, "output_tokens": 250})
        # A report with no subtasks counted adds nothing; a record without an output count has no tokens.
        self.assertEqual(job_tokens(job(input_tokens=5, output_tokens=1, subtask_usage={"count": 0, "levels": subtasks["levels"]})),
                         {"input_tokens": 5, "cache_read_tokens": 0, "output_tokens": 1})
        self.assertIsNone(job_tokens(job(input_tokens=5)))
        summary = summarize([main, job("codex", input_tokens=3000, cache_read_tokens=1000, output_tokens=100),
                             job("codex"), job("codex")], NOW, TITLES)
        self.assertEqual(summary["tokens"], {"input_tokens": 14500, "cache_read_tokens": 10800, "output_tokens": 350})
        self.assertEqual(summary["untracked"], 2)
        # A Harness's tokens count the cache hits in (the owner, 2026-11-06): Claude 11500 + 250, Codex 3000 + 100;
        # its input and cache hits are kept for the cache column.
        self.assertEqual(summary["harnesses"], [
            {"title": "Codex", "tasks": 3, "tokens": 3100, "input": 3000, "cache": 1000, "untracked": 2},
            {"title": "Claude", "tasks": 1, "tokens": 11750, "input": 11500, "cache": 9800, "untracked": 0}])
        self.assertEqual(total_tokens(summary["tokens"]), 14500 + 350)
        # The cache hits are in the input already: nothing is taken off or added for them.
        self.assertEqual(total_tokens({"input_tokens": 5, "cache_read_tokens": 9, "output_tokens": 1}), 6)

    def test_harnesses_by_tasks_then_tokens_and_removed_ones_left_out(self):
        # A removed Harness's tasks, and any other ID the titles lack, are neither counted nor shown: the jobs of
        # 豆包 (a Harness until 2026-11-03, then a provider removed 2026-11-05) too, whose records keep "doubao".
        records = [job("codex", output_tokens=1, input_tokens=1), job("claude", output_tokens=5, input_tokens=5),
                   job("doubao", output_tokens=2, input_tokens=4, cache_read_tokens=3), job("neeko-next"), job("gone"),
                   {**job(), "harness_id": "", "backend": "dsh"}]
        summary = summarize(records, NOW, TITLES)
        self.assertEqual(summary["tasks"], 2)
        self.assertEqual([(row["title"], row["tasks"], row["input"], row["cache"]) for row in summary["harnesses"]],
                         [("Claude", 1, 5, 0), ("Codex", 1, 1, 0)])

    def test_created_at(self):
        self.assertEqual(created_at({"created_at": iso(NOW)}), NOW)
        self.assertEqual(created_at({"created_at": "2026-11-03T00:00:00+08:00"}), NOW)
        self.assertIsNone(created_at({"created_at": 5}))
        self.assertIsNone(created_at({}))


class CardTests(unittest.TestCase):
    def summary(self):
        records = [*(job("claude", input_tokens=10 ** 8, cache_read_tokens=9 * 10 ** 7, output_tokens=10 ** 6)
                     for _ in range(3)),
                   job("codex", topic="t2", status="failed", input_tokens=3 * 10 ** 7, cache_read_tokens=0, output_tokens=10 ** 5),
                   job("codex", topic="t3", status="running"),
                   *(job("codex", topic="t2", status="continued", input_tokens=1000, output_tokens=10) for _ in range(196))]
        return summarize(records, NOW, TITLES)

    def test_the_card_has_a_band_tiles_a_trend_a_ring_and_a_table(self):
        card = usage_card(self.summary())
        at = time.strftime('%m-%d %H:%M', time.localtime(NOW - 7 * DAY)) + " – " + time.strftime('%m-%d %H:%M', time.localtime(NOW))
        header = card["header"]
        self.assertEqual((header["title"]["content"], header["subtitle"]["content"], header["icon"]["token"]),
                         ("本周用量", f"{at} · 最近 7 天", "insert-chart_outlined"))
        self.assertEqual(card["config"]["summary"]["content"], "本周用量 · 201 次任务 · token 3.33 亿")
        # The band's colours are the card's own (cards/ui.py styled).
        self.assertIn("band-0", card["config"]["style"]["color"])
        band, tiles, trend_head, trend, harness_head, ring, table, small, updated = card["body"]["elements"]
        rows = [[e.get("content") for e in row["elements"]] for row in band["elements"]]
        # With the cache hits: Claude 3 × (1 亿 + 100 万), Codex 3000 万 + 10 万 + 196 × 1010.
        self.assertEqual(rows[:3], [["<font color='indigo'>本周 token · 含缓存命中和子任务</font>"],
                                    ["<font color='indigo'>**3.33 亿**</font> **token**"],
                                    ["<font color='grey'>201 个任务 · 3 个话题 · 缓存命中 82%</font>"]])
        # How the tasks ended in the state table's words: the answered turns are 做完 too, the failed one 停住了.
        self.assertIn("199 做完", rows[3][1])
        self.assertIn("1 在做", rows[3][1])
        self.assertIn("1 停住了", rows[3][1])
        self.assertEqual([[e["content"] for e in column["elements"]] for column in tiles["columns"]],
                         [["<font color='grey'>输入 · 含缓存命中</font>", "**3.30 亿**"],
                          ["<font color='grey'>缓存命中 · 占输入 82%</font>", "**2.70 亿**"],
                          ["<font color='grey'>输出</font>", "**310 万**"]])
        self.assertEqual(trend_head["content"], "**每天的 token（亿）**")
        values = trend["chart_spec"]["data"]["values"]
        self.assertEqual(len(values), 8)  # 7 × 24 hours from midnight: eight dates, the last only its first moment.
        self.assertEqual(values[-2], {"day": "11-02", "value": 3.33})
        # Claude 91%, Codex 9%.
        self.assertEqual(ring["chart_spec"]["data"]["values"], [{"name": "Codex  9%", "value": 9.1},
                                                                {"name": "Claude  91%", "value": 90.9}])
        self.assertEqual(ring["chart_spec"]["indicator"]["content"][0]["style"]["text"], "3.33 亿")
        self.assertEqual([column["display_name"] for column in table["columns"]],
                         ["Harness", "次数", "token", "缓存命中"])
        self.assertEqual(table["rows"], [{"harness": "Codex", "tasks": "198 · 99%", "tokens": "3030 万 · 9%",
                                          "cache": "0 · 0%"},
                                         {"harness": "Claude", "tasks": "3 · 1%", "tokens": "3.03 亿 · 91%",
                                          "cache": "2.70 亿 · 90%"}])
        self.assertEqual(small["content"], "<font color='grey'>表里 token 含缓存命中；缓存命中后的百分比是它占这个 Harness 输入的比例。\n"
                                           "问了问题、收到回答后接着做的算两次任务。\n"
                                           "另有 1 次任务没有 token 记录（如还没收到模型回复就停了），不在总量里。</font>")
        self.assertTrue(updated["content"].startswith("<font color='grey'>更新于 "))
        self.assertNotIn("button", str(card))

    def test_how_tasks_ended_tells_who_stopped_them(self):
        # The owner's own stop is 已停止; the lead's, the guard's and 代答's are 停住了 with the failed and interrupted.
        records = [job(status="cancelled", cancel_requested_by="card", **ran()),
                   job(status="cancelled", cancel_requested_by="lead", **ran()), job(status="failed"),
                   job(status="interrupted"), job(status="waiting_user"), job(status="queued", **ran())]
        summary = summarize(records, NOW, TITLES)
        self.assertEqual(dict(summary["ended"]), {"cancelled": 1, "stopped": 3, "waiting": 1, "queued": 1})
        from covey.cards.weekly_usage import ended_line
        self.assertEqual(ended_line(summary), "等你 1 · 排队 1 · 停住了 3 · 已停止 1")

    def test_a_day_without_tokens_is_a_bar_of_nothing_and_no_tokens_no_trend(self):
        from covey.cards.weekly_usage import trend
        self.assertIsNone(trend([{"day": "11-01", "tasks": 1, "tokens": 0}]))
        head, chart = trend([{"day": "11-01", "tasks": 1, "tokens": 0}, {"day": "11-02", "tasks": 1, "tokens": 123456}])
        self.assertEqual(head["content"], "**每天的 token（万）**")
        self.assertEqual(chart["chart_spec"]["data"]["values"], [{"day": "11-01", "value": 0.0},
                                                                 {"day": "11-02", "value": 12.3}])

    def test_small_shares_ring_as_one_other(self):
        rows = [{"title": "A", "tokens": 970, "tasks": 1, "untracked": 0}, {"title": "B", "tokens": 15, "tasks": 1, "untracked": 0},
                {"title": "C", "tokens": 15, "tasks": 1, "untracked": 0}]
        from covey.cards.weekly_usage import ring
        self.assertEqual(ring(rows, 1000)["chart_spec"]["data"]["values"],
                         [{"name": "A  97%", "value": 97.0}, {"name": "其他  3%", "value": 3.0}])

    def test_the_text_is_the_same_blocks_under_the_title(self):
        summary = self.summary()
        self.assertEqual(usage_text(summary), "\n\n".join(["**本周用量**", *sections(summary)]))

    def test_a_week_without_tasks(self):
        summary = summarize([job(ago=8 * DAY)], NOW, TITLES)
        card = usage_card(summary)
        band, updated = card["body"]["elements"]
        self.assertEqual([row["elements"][0]["content"] for row in band["elements"]],
                         ["<font color='indigo'>最近 7 天</font>", "**没有任务**", "<font color='grey'>这 7 天没有任务。</font>"])
        self.assertEqual(card["config"]["summary"]["content"], "本周用量 · 没有任务")


class MenuTests(UnifiedCase):
    def test_the_menu_item_sends_the_card_to_the_private_chat_and_no_task(self):
        upsert_job_record(self.ctx.state_dir, 'old', job_id=1, harness_id='codex', status='done', topic_id='t',
                          created_at=iso(time.time() - 8 * DAY), input_tokens=1, output_tokens=1)
        upsert_job_record(self.ctx.state_dir, 'new', job_id=2, harness_id='claude', status='done', topic_id='t',
                          input_tokens=2000, cache_read_tokens=1500, output_tokens=100,
                          timings={'backend_requested': 1.0})
        self.app.runtime.access.remember_owner_chat('dm')
        send = self.app.messenger.create_and_send_card = mock.Mock(return_value=('card', 'om_usage'))
        self.app.handle_menu_event({'event_id': 'ev1', 'event_key': 'weekly_usage', 'operator_open_id': 'owner'},
                                   self.ctx)
        self.assertEqual(send.call_count, 1)
        (_, chat, card, key), kwargs = send.call_args
        self.assertEqual(card['header']['title']['content'], '本周用量')
        self.assertEqual(next(e for e in card['body']['elements'] if e['tag'] == 'table')['rows'],  # 2000 + 100, the cache hits counted in
                         [{'harness': 'Claude Code', 'tasks': '1 · 100%', 'tokens': '2,100 · 100%', 'cache': '1,500 · 75%'}])
        self.assertEqual(chat, 'dm')
        self.assertNotIn('reply_to_message_id', kwargs)  # A new message: a menu click has none to reply to.
        self.assertIn('ev1', key)
        self.assertEqual(self.said(), [])
        self.assertEqual(json.loads((self.ctx.state_dir / PANELS).read_text())['weekly_usage']['message_id'], 'om_usage')
        self.assertEqual(list((self.ctx.state_dir / 'topics').glob('*/topic.json')), [])  # No task.
        self.assertTrue(any('weekly usage tasks=1 untracked=0' in line for line in self.logs))

    def test_the_text_goes_when_the_card_does_not(self):
        self.app.runtime.access.remember_owner_chat('dm')
        self.app.messenger.create_and_send_card = mock.Mock(return_value=('', ''))
        self.app.handle_menu_event({'event_id': 'ev1', 'event_key': 'weekly_usage', 'operator_open_id': 'owner'},
                                   self.ctx)
        self.assertEqual(self.said(), ['**本周用量**\n\n' + f"最近 7 天：{time.strftime('%m-%d %H:%M', time.localtime(time.time() - 7 * DAY))} 起开始的任务"
                                       + '\n\n这 7 天没有任务。\n\n' + f"更新于 {time.strftime('%H:%M')}"])
        self.assertNotIn('card', self.app.messenger.send_message.call_args.kwargs)

    def test_the_old_text_is_a_task_in_the_private_chat_and_in_a_group(self):
        access = self.app.runtime.access
        access.identify(app_id='cli_app', bot_open_id='ou_bot')
        access.lookup = lambda profile, chat_id: {'external': False}
        access.switch(self.ctx, 'group', True)  # An open group: the owner's @ asks nothing (app/group_manager.py ask).
        self.app.handle_event({'chat_id': 'dm', 'chat_type': 'p2p', 'sender_type': 'user', 'sender_id': 'owner',
                               'message_id': 'u1', 'content': '本周用量'}, self.ctx)
        self.app.handle_event({'chat_id': 'group', 'chat_type': 'group', 'sender_type': 'user',
                               'sender_id': 'owner', 'message_id': 'g1', 'content': '@_user_1 本周用量',
                               'mentions': [{'id': 'ou_bot', 'key': '@_user_1', 'name': 'Covey'}]}, self.ctx)
        self.assertEqual(self.said(), [])
        self.assertEqual(sum(app.submission.selection_card.call_count for app in self.app.apps.values()), 2)


if __name__ == "__main__":
    unittest.main()

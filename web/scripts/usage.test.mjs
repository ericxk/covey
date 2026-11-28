// 用量页和看板「本周用量」的数（client/src/cv/usageData.ts）：node --test scripts/usage.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { axisText, cachePct, dayFigures, dayWords, harnessShares, niceTicks, shownDays, spanOf, sumDays, tokText, weekWord, xTicks } from '../client/src/cv/usageData.ts';
import { usageFromSnapshot } from '../shared/usage.ts';

const day = (date, f = {}) => ({ date, tasks: 0, topics: 0, tokens: 0, input: 0, cache: 0, output: 0, untracked: 0, ...f });
const dates = (from, n) => Array.from({ length: n }, (_, i) => new Date(Date.parse(`${from}T00:00:00Z`) + i * 86400000).toISOString().slice(0, 10));

test('星期按日期本身算，和浏览器时区无关', () => {
  assert.equal(weekWord('2026-11-21'), '周六');
  assert.equal(weekWord('2026-11-16'), '周一');
  assert.equal(dayWords('2026-11-20'), '11-20 周五');
});

test('记录开始之前的天不画（不是 0）；之后没任务的天照样在', () => {
  const u = { at: 0, first: '2026-11-17', spans: [], days: dates('2026-11-12', 10).map((d) => day(d, { tokens: d >= '2026-11-17' ? 5 : 0 })) };
  assert.deepEqual(shownDays(u, 7).map((d) => d.date), ['2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20', '2026-11-21']);
  assert.equal(shownDays({ ...u, first: null }, 7).length, 7);
  assert.equal(shownDays(u, 3).length, 3);
});

test('一段的合计用 Covey 算的（话题去重）；没有才把每天加起来', () => {
  const days = [day('2026-11-20', { tasks: 2, topics: 2, tokens: 10, input: 9, cache: 6 }), day('2026-11-21', { tasks: 1, topics: 1, tokens: 5, input: 4, cache: 4 })];
  const span = { days: 7, since: '2026-11-15', tasks: 3, topics: 2, tokens: 15, input: 13, cache: 10, output: 2, untracked: 0, harnesses: [{ title: 'Claude Code', tasks: 3, tokens: 15, input: 13, cache: 10, untracked: 0 }] };
  const u = { at: 0, first: null, days, spans: [span] };
  assert.equal(spanOf(u, 7).topics, 2);
  assert.equal(spanOf(u, 7).exact, true);
  const sum = spanOf(u, 30);
  assert.deepEqual([sum.tasks, sum.tokens, sum.exact, sum.harnesses.length], [3, 15, false, 0]);
  assert.deepEqual(sumDays(days), { tasks: 3, topics: 3, tokens: 15, input: 13, cache: 10, output: 0, untracked: 0 });
  assert.equal(cachePct(span), 77);
  assert.equal(cachePct({ input: 0, cache: 0 }), null);
});

test('token 的写法：浮窗和表格比大字多一位', () => {
  assert.deepEqual([0, 950, 8200, 51_000, 1_240_000, 12_400_000, 327_000_000, 2_634_207_066, 14_708_045_230].map(tokText),
    ['0', '950', '8.2K', '51K', '1.24M', '12.4M', '327M', '2.63B', '14.7B']);
});

test('纵轴刻度：步长 1、2、2.5、5 × 10 的幂，顶格不低于最大值', () => {
  assert.deepEqual(niceTicks(3_330_999_168), [0, 2e9, 4e9]);
  assert.deepEqual(niceTicks(2_851_540_307, 4), [0, 1e9, 2e9, 3e9]);
  assert.deepEqual(niceTicks(700, 3), [0, 250, 500, 750]);
  assert.deepEqual(niceTicks(0), [0]);
  assert.deepEqual([0, 5e8, 1e9, 2.5e9, 1.5e6, 2000].map(axisText), ['0', '500M', '1B', '2.5B', '1.5M', '2K']);
});

test('横轴：7 天每天都写；更长只写周一，排不下隔几个写', () => {
  const week = dates('2026-11-15', 7).map((d) => day(d));
  assert.deepEqual(xTicks(week, 3), [0, 1, 2, 3, 4, 5, 6]);
  const month = dates('2026-10-23', 30).map((d) => day(d));
  const mondays = xTicks(month, 10);
  assert.deepEqual(mondays.map((i) => month[i].date), ['2026-10-26', '2026-11-02', '2026-11-09', '2026-11-16']);
  assert.deepEqual(xTicks(month, 2).map((i) => month[i].date), ['2026-10-26', '2026-11-09']);
});

test('服务端读 Covey 的快照：没有 history（旧 Covey）是 null，认不得的字段当 0 或丢掉', () => {
  assert.equal(usageFromSnapshot(null), null);
  assert.equal(usageFromSnapshot({ days: 7, tasks: 3, daily: [] }), null);  // v15 的样子：只有卡片的数
  assert.equal(usageFromSnapshot({ history: { days: [], spans: [] } }), null);
  const u = usageFromSnapshot({
    at: 1795269492.5, days: 7, tasks: 1543,
    history: {
      days: [day('2026-11-20', { tasks: 3, tokens: 10 }), { date: '11-21', tasks: 1 }, { date: '2026-11-21', tasks: '2', tokens: -5, cache: 4 }],
      spans: [{ days: 7, since: '2026-11-15', tasks: 3, topics: 2, tokens: 10, harnesses: [{ title: 'Claude Code', tasks: 3, tokens: 10, output: 9 }] },
        { days: 0, tasks: 9 }, { days: 30, since: 'x' }],
      first: 'soon',
    },
  });
  assert.deepEqual(u.days.map((d) => [d.date, d.tasks, d.tokens, d.cache]), [['2026-11-20', 3, 10, 0], ['2026-11-21', 0, 0, 4]]);
  assert.deepEqual(u.spans.map((s) => [s.days, s.since, s.tasks]), [[7, '2026-11-15', 3], [30, '', 0]]);
  assert.deepEqual(u.spans[0].harnesses, [{ title: 'Claude Code', tasks: 3, tokens: 10, input: 0, cache: 0, untracked: 0 }]);
  assert.deepEqual([u.at, u.first], [1795269492.5, null]);
  assert.equal(usageFromSnapshot({ history: { days: [day('2026-11-21')], spans: [], first: '2026-10-17' } }).first, '2026-10-17');
});

test('本周用量下的各 Harness 占比：从多到少，不到 10% 带一位小数；不到 2 个有 token 的不写', () => {
  const rows = [
    { title: 'Antigravity', tokens: 4003664808 },
    { title: 'Codex', tokens: 63032712 },
    { title: 'Claude Code', tokens: 24481471064 },
    { title: 'dsh', tokens: 0 },
  ];
  assert.deepEqual(harnessShares(rows).map((x) => `${x.title} ${x.text}`), ['Claude Code 86%', 'Antigravity 14%', 'Codex 0.2%']);
  assert.deepEqual(harnessShares([{ title: 'Claude Code', tokens: 5 }, { title: 'Codex', tokens: 0 }]), []);
  assert.deepEqual(harnessShares(undefined), []);
  assert.equal(harnessShares([{ title: 'A', tokens: 1e9 }, { title: 'B', tokens: 3e5 }])[1].text, '不到 0.1%');
});

test('近 24 小时的三个数：做完几轮、Agent 干了多久、平均等你多久', () => {
  const cells = dayFigures({ today_done: 36, today_work_seconds: 20.8 * 3600, avg_wait_seconds: 5.9 * 60 });
  assert.deepEqual(cells.map((c) => `${c.n}${c.unit} ${c.label}`), ['36轮 做完', '20.8小时 Agent 干活', '5.9分钟 平均等你']);
  assert.deepEqual(dayFigures({ today_done: 0, today_work_seconds: 1800, avg_wait_seconds: null }).map((c) => c.n + c.unit), ['0轮', '30分钟', '—']);
  assert.deepEqual(dayFigures({ today_done: 2, today_work_seconds: 7200, avg_wait_seconds: 40 }).map((c) => c.n + c.unit), ['2轮', '2小时', '40秒']);
  // 旧服务端没有这几个字段：不画
  assert.deepEqual(dayFigures(undefined), []);
  assert.deepEqual(dayFigures({}), []);
});

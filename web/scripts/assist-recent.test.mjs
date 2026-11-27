// 设置页「辅助模型」下面的“后台活最近一次”（T884）：一行说什么、什么颜色。跑：npm run test:assist
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as A from '../client/src/cv/assistRecent.ts';

const NOW = 1_800_000_000;
const R = (extra = {}) => ({ feature: 'tidy', label: '整理专项', harness: 'codex', name: 'Codex', model: 'gpt-6-astra', ok: true,
  code: '', words: '', ms: 8400, at: NOW - 180, manual: false, ...extra });

test('成了：用了谁带模型，耗时到 0.1 秒，几分钟前', () => {
  assert.deepEqual(A.recentLine(R(), NOW), { key: 'tidy', feature: '整理专项', who: 'Codex · gpt-6-astra', result: '成功', tone: 'ok',
    why: '', took: '8.4 秒', when: '3 分钟前' });
});

test('调了没成：红字和原因的说法；没调模型（不用模型、刚失败过）灰字、没有耗时', () => {
  const bad = A.recentLine(R({ ok: false, code: 'authentication_error', words: '登录失效或没有权限', ms: 900 }), NOW);
  assert.deepEqual([bad.result, bad.tone, bad.why, bad.took], ['没成', 'bad', '登录失效或没有权限', '900 毫秒']);
  const off = A.recentLine(R({ ok: false, code: 'broken', words: '刚失败过，15 分钟内后台先不用它', ms: 0 }), NOW);
  assert.deepEqual([off.result, off.tone, off.took], ['没调模型', 'off', '—']);
  const none = A.recentLine(R({ ok: false, harness: '', name: '', model: '', code: 'none', words: '设置里选了不用模型', ms: 0 }), NOW);
  assert.deepEqual([none.who, none.result], ['—', '没调模型']);
});

test('主人点的写明；没有时间写 —；旧版 Covey 没有 recent 是空表', () => {
  assert.equal(A.recentLine(R({ manual: true, at: NOW - 30 }), NOW).when, '刚刚 · 你点的');
  assert.equal(A.recentLine(R({ at: 0 }), NOW).when, '—');
  assert.deepEqual(A.recentLines(undefined, NOW), []);
  assert.equal(A.tookWords(12500), '13 秒');
});

test('只列分流、整理专项、记忆自己长三行（T898）：旧版 Covey 的补话题要点并进整理专项取较新的一次，已下线的几项不列', () => {
  const rows = [R({ feature: 'patrol', label: '巡逻' }), R({ feature: 'learn', label: '记忆自己长', at: NOW - 60 }),
    R({ feature: 'backfill', label: '补话题要点', at: NOW - 10, ms: 5200 }), R({ feature: 'tidy', at: NOW - 600 }),
    R({ feature: 'track', label: '分流', ms: 1900 }), R({ feature: 'tidy_realtime', label: '实时归并' }), R({ feature: 'look', label: '看看' })];
  const lines = A.recentLines(rows, NOW);
  assert.deepEqual(lines.map((l) => [l.key, l.feature]), [['track', '分流'], ['tidy', '整理专项'], ['learn', '记忆自己长']]);
  assert.equal(lines[1].took, '5.2 秒');
  assert.deepEqual(A.SHOWN, ['track', 'tidy', 'learn']);
});

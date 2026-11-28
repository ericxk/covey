// 看板右栏的「额度」（quotaBrief.ts）：一个来源一行，有事一件一行，没事“都够用”，没有来源整块不画。跑：npm run test:quotabrief
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { briefRows } from '../client/src/cv/quotaBrief.ts';

// 2026-11-29 01:13（上海）
const NOW = Date.parse('2026-11-29T01:13:00+08:00') / 1000;
const at = (d, h, m = 0) => Date.parse(`2026-${d}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00+08:00`) / 1000;

const win = (kind, left, over = {}) => ({ kind, label: kind === '5h' ? '5 小时' : '周', left, resets_at: at('12-03', 15), checked_at: NOW - 60, ...over });
const pool = (windows, over = {}) => ({ key: '', label: '', active: false, state: 'ok', available_at: null, windows, ...over });
const acc = (name, pools, over = {}) => ({ name, state: 'ok', available_at: null, pools, ...over });
const src = (accounts, over = {}) => ({
  id: 'gateway-x', kind: 'gateway', title: 'Claude 网关', collected_at: NOW - 60, ttl: 600, stale_after: 900, read_at: NOW - 60,
  failing: false, usable: accounts.filter((a) => a.state === 'ok').length, total: accounts.length, next_available_at: null,
  low: false, dry: false, expand: false, relogin: [], accounts, ...over,
});
const data = (sources) => ({ at: NOW, read_at: NOW - 60, relogin: [], sources, refresh: { at: 0, busy: false, cooldown: 0 } });
const texts = (row) => row.lines.map((l) => `${l.tone}:${l.text}`);

test('没有条目、没有来源：不画', () => {
  assert.deepEqual(briefRows(null, NOW), []);
  assert.deepEqual(briefRows(data([]), NOW), []);
});

test('网关：1 个用完写几点恢复，2 个周额度偏紧，宽裕的号不提', () => {
  const s = src([
    acc('A1', [pool([win('5h', 0.99), win('7d', 0.09), { ...win('7d', 0.56), label: 'Fable 周' }])]),
    acc('A2', [pool([win('5h', 0.34), win('7d', 0.44)])]),
    acc('A3', [pool([win('5h', 0.7), win('7d', 0.73)])]),
    acc('A4', [pool([win('5h', 1), win('7d', 0.01)], { state: 'spent', available_at: at('12-01', 3, 2) })], { state: 'spent', available_at: at('12-01', 3, 2) }),
    acc('A5', [pool([win('5h', 0.72), win('7d', 0.06)])]),
    acc('A6', [pool([win('5h', 0.98), win('7d', 0.87)])]),
  ]);
  const [row] = briefRows(data([s]), NOW);
  assert.equal(row.title, 'Claude 网关');
  assert.equal(row.count, '5/6 个能用');
  assert.deepEqual(texts(row), ['warn:1 个号用完 · 12月1日 03:02 恢复', 'warn:2 个号周额度剩不到 20%']);
});

test('都宽裕：一行灰字“都够用”', () => {
  const s = src([acc('A1', [pool([win('5h', 0.9), win('7d', 0.6)])]), acc('A2', [pool([win('7d', 1)])])], { id: 'agy', kind: 'agy', title: 'agy' });
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['ok:都够用']);
});

test('5 小时偏紧单独一行，周的排在前面；已重置、还没新读数的格不算偏紧', () => {
  const s = src([
    acc('A1', [pool([win('5h', 0.19, { resets_at: at('11-29', 2, 30) }), win('7d', 0.12)])]),
    acc('A2', [pool([win('5h', 0.05, { resets_at: at('11-29', 1, 0), checked_at: at('11-29', 0, 50) }), win('7d', 0.8)])]),
  ]);
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['warn:1 个号周额度剩不到 20%', 'warn:1 个号 5 小时额度剩不到 20%']);
});

test('agy 两种池：写池名；用完的池按池说，另一池照常', () => {
  const g = (left, over) => pool([win('5h', left), win('7d', 0.5)], { key: 'gemini', label: 'Gemini', ...over });
  const t = (left) => pool([win('5h', left), win('7d', 0.9)], { key: '3p', label: 'Claude/GPT' });
  const s = src([
    acc('a@x', [g(0, { state: 'spent', available_at: at('11-29', 3, 2) }), t(0.9)]),
    acc('b@x', [g(0.1), t(0.95)]),
  ], { id: 'agy', kind: 'agy', title: 'agy', usable: 2, total: 2 });
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['warn:1 个号 Gemini 用完 · 11月29日 03:02 恢复', 'warn:1 个号 Gemini 5 小时额度剩不到 20%']);
});

test('要重新登录排最前、不写恢复时刻；冷却写几点恢复', () => {
  const s = src([
    acc('A1', [pool([win('7d', 0.5)], { state: 'cooling', available_at: at('11-29', 2) })], { state: 'cooling', available_at: at('11-29', 2) }),
    acc('A2', [pool([win('7d', 0.5)])], { state: 'login_required' }),
    acc('A3', [pool([win('7d', 0.5)])]),
  ]);
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['warn:1 个号要重新登录', 'warn:1 个号冷却中 · 11月29日 02:00 恢复']);
});

test('读数太旧、读不到：一行灰字，不报偏紧', () => {
  const old = src([acc('A1', [pool([win('7d', 0.05)])])], { collected_at: NOW - 3 * 3600 });
  assert.deepEqual(texts(briefRows(data([old]), NOW)[0]), ['faint:读数停在 11月28日 22:13（3 小时前）']);
  const none = src([], { collected_at: 0, failing: true, usable: 0, total: 0 });
  const row = briefRows(data([none]), NOW)[0];
  assert.equal(row.count, '');
  assert.deepEqual(texts(row), ['faint:读不到']);
});

test('上次没读到但有更早的数：先说没读到，偏紧照报', () => {
  const s = src([acc('A1', [pool([win('7d', 0.05)])])], { failing: true, collected_at: NOW - 300, why: '连不上' });
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['faint:上次读取没成功 · 连不上，数字是 11月29日 01:08 的', 'warn:1 个号周额度剩不到 20%']);
});

test('一个能用的号都没有、又没有可说的原因：照实说', () => {
  const s = src([acc('A1', [pool([win('7d', 0.5)], { state: 'unknown' })], { state: 'unknown' })], { usable: 0, total: 1 });
  assert.deepEqual(texts(briefRows(data([s]), NOW)[0]), ['ok:暂时没有能用的号']);
});

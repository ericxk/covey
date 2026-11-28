// 数据约定 v43、v45（docs/web.md：记忆中心与仓库；v45 记忆一页式，golden 已是 v45 形状）：memory:* 键的认法、memory_edit 新旧两种 payload 的校验、
// settings 新操作和启动、换配置带 repos 的校验，以及 tests/golden/web_memory_v43.json 的形状。跑：npm run test:memv43
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as M from '../server/modules/workbench/workbench.memory-edit.ts';
import { MEMORY_EDIT_ACTIONS_V43, MEMORY_KEY_RE, SETTINGS_KEYS, SETTINGS_WHATS, isSettingsKey } from '../shared/api.interface.ts';

const golden = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'tests', 'golden', 'web_memory_v43.json'), 'utf8'));
const ok = (p) => assert.doesNotThrow(() => M.checkMemoryEdit(p, {}));
const bad = (p, re) => assert.throws(() => M.checkMemoryEdit(p, {}), re);

test('键：7 个旧键仍认，golden 里每个 memory:* 键都认，写错的键不认', () => {
  assert.deepEqual([...SETTINGS_KEYS], ['settings', 'usage', 'tidy', 'asks', 'memory', 'upgrade', 'quota']);
  for (const k of SETTINGS_KEYS) assert.equal(isSettingsKey(k), true, k);
  for (const k of Object.keys(golden.keys)) assert.equal(isSettingsKey(k), true, k);
  for (const k of ['memory:t:abc', 'memory:p:AB', 'memory:p:', 'memory:p:abc', 'memory:p:ABCDEF', 'memory:import:', 'memory:import:1234', 'memory:', 'memory:global:x', 'memory:p:' + 'a'.repeat(33), 'foo']) {
    assert.equal(isSettingsKey(k), false, k);
  }
  assert.ok(MEMORY_KEY_RE.test('memory:p:' + 'a'.repeat(32)));
  // v45 起不再推 previews、import:<页>，正则仍认（否则删不掉库里的旧行）
  assert.equal(isSettingsKey('memory:previews'), true);
  assert.equal(isSettingsKey('memory:import:0'), true);
  assert.equal('memory:p:'.length + 32 <= 41, true);
});

test('golden（v45）：4 种 memory:* 键各一份，没有 previews、import；global 有 ≥2 条待审核和 ≥2 条变更，条目短码前缀对得上 kind', () => {
  assert.equal(golden.version, 45);
  assert.deepEqual(Object.keys(golden.keys).sort(), ['memory:global', 'memory:index', 'memory:p:demo01', 'memory:topics']);
  const g = golden.keys['memory:global'];
  assert.ok(g.review.length >= 2 && g.log.length >= 2);
  const idx = golden.keys['memory:index'];
  assert.equal(idx.version, 45);
  for (const k of ['health', 'imports']) assert.equal(k in idx, false, `index 不该有 ${k}`);
  for (const s of idx.scopes) assert.equal('rules' in s, false, `${s.key} 不该有 rules（配额）`);
  for (const k of Object.keys(idx.revs)) assert.equal(isSettingsKey(k), true, k);
  const entries = [...g.entries, ...golden.keys['memory:p:demo01'].entries, ...golden.keys['memory:topics'].topics.flatMap((t) => t.entries)];
  for (const e of entries) {
    assert.equal(e.code[0], { rule: 'R', decision: 'D', note: 'N' }[e.kind], e.key);
    assert.equal('carried_next' in e, false, `${e.key} 不该有 carried_next`);
  }
});

test('memory_edit v43：golden 里每个 edit 都过，每个 action 至少一份', () => {
  for (const e of golden.edits) ok(e);
  const seen = new Set(golden.edits.map((e) => e.action));
  for (const a of MEMORY_EDIT_ACTIONS_V43) assert.ok(seen.has(a), `golden 缺 ${a}`);
  // v45：去掉 import、ignore，剩 15 个
  assert.equal(MEMORY_EDIT_ACTIONS_V43.length, 15);
  for (const a of ['import', 'ignore']) {
    assert.equal(seen.has(a), false, a);
    bad({ v: 43, action: a, keys: ['fp1'] }, /action/u);
  }
});

test('memory_edit v43：拒绝超长、缺字段、不认识的值', () => {
  const add = golden.edits.find((e) => e.action === 'add');
  const dec = { ...add, kind: 'decision', scope: 'project' };
  ok({ ...dec, text: '字'.repeat(600) });
  bad({ ...dec, text: '字'.repeat(601) }, /600/u);
  bad({ ...add, text: '字'.repeat(201) }, /200/u);
  bad({ ...add, title: '字'.repeat(41) }, /title/u);
  bad({ ...add, why: '字'.repeat(201) }, /why/u);
  bad({ v: 43, action: 'undo', keys: [] }, /undo_token/u);
  bad({ v: 43, action: 'undo', keys: [], undo_token: 'x'.repeat(65) }, /undo_token/u);
  bad({ v: 43, action: 'add', keys: [], title: 't', text: 'x', kind: 'rule' }, /kind 和 scope/u);
  bad({ v: 43, action: 'add', keys: [], text: 'x', kind: 'rule', scope: 'global' }, /title/u);
  bad({ v: 43, action: 'pin', keys: ['a'] }, /action/u);
  bad({ v: 43, action: 'archive', keys: [] }, /keys/u);
  bad({ v: 43, action: 'archive', keys: Array(201).fill('a') }, /200/u);
  bad({ v: 43, action: 'archive', keys: ['a'.repeat(201)] }, /keys/u);
  bad({ v: 43, action: 'edit', keys: ['a'], kind: 'idea' }, /kind/u);
  bad({ v: 43, action: 'edit', keys: ['a'], scope: 'world' }, /scope/u);
  bad({ v: 43, action: 'edit', keys: ['a'], repos: Array(11).fill('r') }, /repos/u);
  bad({ v: 43, action: 'edit', keys: ['a'], oops: 1 }, /字段/u);
  assert.throws(() => M.checkMemoryEdit({ v: 43, action: 'archive', keys: ['a'] }, { topic_key: 't1' }), /topic_key/u);
});

test('memory_edit 旧 v37（没有 v）：原规则不变', () => {
  ok({ action: 'add', what: 'principle', scope: 'global', text: '先跑测试' });
  ok({ action: 'pin', key: 'principle:pabcdef' });
  bad({ action: 'add', what: 'principle', scope: 'global', text: '字'.repeat(201) }, /200/u);
  bad({ action: 'archive', key: 'principle:pabcdef' }, /action/u);
  bad({ action: 'pin', key: 'bad' }, /key/u);
  bad({ action: 'pin', key: 'principle:pabcdef', keys: [] }, /字段/u);
});

test('settings：4 个新 what 在清单里，golden 样例过校验，写错的拒绝；旧 workspace_* 还在', () => {
  for (const w of ['repos_set', 'repos_add', 'repos_remove', 'worktrees_prune', 'workspace_add', 'workspace_remove', 'workspace_prune']) assert.ok(SETTINGS_WHATS.includes(w), w);
  for (const w of ['repos_set', 'repos_add', 'repos_remove', 'worktrees_prune']) assert.equal(M.reposSettingsError(w, golden.settings[w]), null, w);
  assert.match(M.reposSettingsError('repos_set', { name: 'x', status: 'gone' }), /status/u);
  assert.match(M.reposSettingsError('repos_set', { name: 'x', write_mode: 'copy' }), /write_mode/u);
  assert.match(M.reposSettingsError('repos_set', { name: 'x'.repeat(65) }), /name/u);
  assert.match(M.reposSettingsError('repos_set', { name: 'x', note: 'n'.repeat(201) }), /note/u);
  assert.match(M.reposSettingsError('repos_add', { path: '/' + 'p'.repeat(500) }), /path/u);
  assert.match(M.reposSettingsError('repos_add', { path: '' }), /path/u);
  assert.match(M.reposSettingsError('repos_remove', {}), /name/u);
  assert.match(M.reposSettingsError('worktrees_prune', { paths: Array(51).fill('/a') }), /50/u);
  assert.match(M.reposSettingsError('worktrees_prune', { paths: ['/' + 'p'.repeat(500)] }), /paths/u);
});

test('launch、switch 的 repos：golden 过，超过 10 项、name 太长、mode 不对拒绝；不带照旧', () => {
  assert.equal(M.launchReposError(undefined), null);
  assert.equal(M.launchReposError(golden.settings.launch.repos), null);
  assert.equal(M.launchReposError(golden.settings.switch.repos), null);
  assert.equal('workspace' in golden.settings.launch || 'run_mode' in golden.settings.launch, false);
  assert.equal(M.launchReposError(Array.from({ length: 10 }, (_, i) => ({ name: `r${i}`, mode: 'read' }))), null);
  assert.ok(M.launchReposError(Array.from({ length: 11 }, (_, i) => ({ name: `r${i}`, mode: 'read' }))));
  assert.ok(M.launchReposError([{ name: 'x'.repeat(65), mode: 'read' }]));
  assert.ok(M.launchReposError([{ name: 'x', mode: 'rw' }]));
  assert.ok(M.launchReposError('demo'));
});

test('一轮的 memory：Covey 推来的 {count, used, codes} 原样留下（截断到上限），不像的丢掉', () => {
  assert.deepEqual(M.turnMemoryOf({ count: 3, used: ['提交前先跑测试'], codes: ['R7K2', 'D3MX9'] }), { count: 3, used: [{ code: '', title: '提交前先跑测试' }], codes: ['R7K2', 'D3MX9'] });
  // 新契约：used 是 [{code, title}]，code 可以是空串；不认的形状丢掉
  assert.deepEqual(M.turnMemoryOf({ count: 5, used: [{ code: 'R7K2', title: '提交前先跑测试' }, { code: '', title: '只有标题' }, 7, null, {}], codes: ['R7K2', 'D3MX9'] }),
    { count: 5, used: [{ code: 'R7K2', title: '提交前先跑测试' }, { code: '', title: '只有标题' }], codes: ['R7K2', 'D3MX9'] });
  assert.deepEqual(M.turnMemoryOf({ count: 2, used: [], codes: ['A1', 'B2'] }), { count: 2, used: [], codes: ['A1', 'B2'] });
  assert.equal(M.turnMemoryOf({ count: 2, used: [{ code: 'x'.repeat(50), title: 't'.repeat(200) }], codes: [] }).used[0].title.length, 80);
  assert.equal(M.turnMemoryOf(null), null);
  assert.equal(M.turnMemoryOf([]), null);
  assert.equal(M.turnMemoryOf({ count: 0, used: [], codes: [] }), null);
  assert.deepEqual(M.turnMemoryOf({ count: -1, used: [1, 'x'], codes: 'R1' }), { count: 0, used: [{ code: '', title: 'x' }], codes: [] });
  assert.equal(M.turnMemoryOf({ count: 2, used: ['a'.repeat(200)], codes: [] }).used[0].title.length, 80);
});

// 去工作区（v43）：新旧两代 Covey 的判定、仓库轮换、payload 清洗、标签文字。D2Y4T：新话题不再选执行设备，payload 一律不带 device。
// 跑：node --import ./scripts/alias.mjs --test scripts/launch-repos.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as R from '../client/src/cv/launchRepos.ts';
import * as S from '../client/src/cv/switchConfig.ts';

const m = (key, extra = {}) => ({
  key, harness: key.split(':')[0], harness_title: key.split(':')[0], provider: '', provider_title: '自带', label: key,
  budgets: [{ value: 'high', text: '高' }], default_budget: 'high', start_budget: 'high', work_modes: [], ...extra,
});
const models = [m('claude:opus'), m('codex:gpt')];
const pre = { model: 'claude:opus', effort: 'high', work_mode: '' };
const NEW = { models, preselect: pre, devices: [{ name: 'mac', dot: 'ok', harnesses: [] }, { name: 'box', dot: 'down', harnesses: ['codex'] }] };
const OLD = {
  models, workspaces: [{ name: 'covey', device: '', harnesses: [] }, { name: 'dev', device: 'box', harnesses: ['codex'] }],
  run_modes: [{ value: 'direct', text: '原目录' }, { value: 'worktree', text: '独立 worktree' }],
  preselect: { ...pre, workspace: 'covey', run_mode: 'worktree' },
};

test('新旧判定', () => {
  assert.equal(R.isRepoMode(NEW), true);
  assert.equal(R.isRepoMode(OLD), false);
  assert.equal(R.isRepoMode({ ...OLD, devices: NEW.devices }), false);
});

// T879 起 Covey 把本机排第一行推来（name ''、local）；D2Y4T 起新 Covey 不推 devices，较旧的还会推
const LOCAL_ROW = { name: '', title: '本机', dot: 'ok', harnesses: [], local: true };
const LIVE = { ...NEW, devices: [LOCAL_ROW, { name: 'testbox', title: 'testbox', dot: 'ok', harnesses: ['codex'] }, { name: 'macmini', title: 'Mac mini', dot: 'never', harnesses: ['claude'] }] };

test('D2Y4T：下线的设备函数不再导出', () => {
  for (const k of ['deviceChoices', 'showDevice', 'deviceText', 'applyDevice', 'LOCAL_DEVICE']) assert.equal(R[k], undefined, k);
});

test('D2Y4T：Covey 仍推 devices 时，开新话题的 payload 也不带 device', () => {
  const sel = { ...pre, device: 'testbox', repos: [{ name: 'a', mode: 'read' }] };
  assert.ok(!('device' in R.launchPayload(LIVE, sel, 'hi', [], false)));
  assert.ok(!('device' in R.launchPayload(NEW, { ...pre, device: 'box' }, 'hi', [], false)));
  assert.ok(!('device' in R.cleanRepoPayload(LIVE, { ...pre, device: 'macmini' })));
  assert.deepEqual(R.launchPayload(LIVE, { ...pre, device: '' }, 'hi', [], false), { ...pre, text: 'hi' });
});

test('设备的叫法：没选就是本机，绝不拿第一台设备顶替', () => {
  assert.equal(R.deviceWord(LIVE, undefined), '本机');
  assert.equal(R.deviceWord(LIVE, ''), '本机');
  assert.equal(R.deviceWord(LIVE, 'macmini'), 'Mac mini');
  assert.equal(R.deviceWord(LIVE, 'gone'), 'gone'); // 列表里没有：照写别名
});

test('换配置折叠栏的摘要：话题在本机不写设备，在设备上才写叫法', () => {
  assert.deepEqual(R.placeSummary(LIVE, '', undefined), { device: null, repos: '未选仓库' });
  assert.deepEqual(R.placeSummary(LIVE, undefined, undefined), { device: null, repos: '未选仓库' });
  assert.deepEqual(R.placeSummary(LIVE, 'macmini', [{ name: 'covey', mode: 'write' }, { name: 'covey-internal', mode: 'read' }]),
    { device: 'Mac mini', repos: 'covey（改）、covey-internal（读）' });
  assert.deepEqual(R.placeSummary(LIVE, undefined, []), { device: null, repos: '未选仓库' }); // 旧 Covey 的 config 没有 device
  const bare = { ...NEW, devices: undefined };
  assert.deepEqual(R.placeSummary(bare, '', []), { device: null, repos: '未选仓库' }); // 新 Covey 不推 devices：本机不写设备
  assert.deepEqual(R.placeSummary(bare, 'gpu1', []), { device: 'gpu1', repos: '未选仓库' }); // 话题本来就在设备上：照写
  const many = ['a', 'b', 'c', 'd', 'e'].map((name) => ({ name, mode: 'read' }));
  assert.equal(R.repoWords(many), 'a（读）、b（读）、c（读） 等 5 个');
  assert.equal(R.repoWords([]), '');
});

test('换配置的 payload 不带 device（换配置换不了机器）', () => {
  const cur = { ...pre, device: 'testbox', repos: [{ name: 'a', mode: 'read' }] };
  assert.ok(!('device' in R.switchPayload(LIVE, cur, true, false)));
  assert.ok(!('device' in R.switchPayload(LIVE, cur, true, true)));
  assert.ok(!('device' in R.switchPayload(OLD, { ...OLD.preselect, device: 'box' }, false, false)));
});

test('仓库轮换：不用 → 只读 → 可写 → 不用，最多 10 个', () => {
  let r = R.cycleRepo(undefined, 'a');
  assert.deepEqual(r, [{ name: 'a', mode: 'read' }]);
  r = R.cycleRepo(r, 'a');
  assert.deepEqual(r, [{ name: 'a', mode: 'write' }]);
  assert.deepEqual(R.cycleRepo(r, 'a'), []);
  assert.deepEqual(R.cycleRepo([{ name: 'a', mode: 'read' }], 'a', true), []);
  const full = Array.from({ length: 10 }, (_, i) => ({ name: `r${i}`, mode: 'read' }));
  assert.equal(R.cycleRepo(full, 'x').length, 10);
  assert.equal(R.cycleRepo(full, 'r3').find((x) => x.name === 'r3').mode, 'write');
});

test('仓库名单：排除冻结，已选但不在名单的留着', () => {
  const items = [{ name: 'a', status: 'active', note: '' }, { name: 'b', status: 'frozen', note: '' }, { name: 'c', status: 'readonly', note: 'x' }];
  const out = R.repoChoices(items, [{ name: 'old', mode: 'read' }]);
  assert.deepEqual(out.map((x) => x.name), ['a', 'c', 'old']);
  assert.equal(out[1].readonly, true);
  assert.deepEqual(out.map((x) => x.where), ['', '', '']);
});

test('v46：常用路径里的目录和设备上的路径，块上说在哪、是什么', () => {
  const items = [{ name: 'a', status: 'active', note: '' }, { name: 'n', status: 'active', note: '', kind: 'dir' },
    { name: 'm', status: 'active', note: '', device: 'macmini' }, { name: 'd', status: 'active', note: '', kind: 'dir', device: 'pi' }];
  assert.deepEqual(R.repoChoices(items, []).map((x) => x.where), ['', '目录', '在 macmini 上', '目录 · 在 pi 上']);
  assert.equal(R.repoWhere({}), '');
});

test('repos 比较和标签文字', () => {
  const a = [{ name: 'a', mode: 'read' }, { name: 'b', mode: 'write' }];
  assert.equal(R.sameRepos(a, [a[1], a[0]]), true);
  assert.equal(R.sameRepos(a, [{ name: 'a', mode: 'write' }, a[1]]), false);
  assert.equal(R.sameRepos(undefined, []), true);
  assert.equal(R.repoLabel(a), '仓库 a、b');
  assert.equal(R.repoLabel([...a, { name: 'c' }, { name: 'd' }]), '仓库 a、b 等 4 个');
  assert.equal(R.repoLabel([]), '');
});

test('launch payload：旧 Covey 一字不变', () => {
  const p = R.launchPayload(OLD, OLD.preselect, 'hi', [], false);
  assert.deepEqual(p, { ...OLD.preselect, text: 'hi', run_mode: 'worktree' });
  const q = R.launchPayload(OLD, { ...OLD.preselect, workspace: 'dev' }, 'hi', [{ x: 1 }], true);
  assert.equal(q.run_mode, 'direct');
  assert.deepEqual(q.files, [{ x: 1 }]);
});

test('launch payload：新 Covey 没有 workspace、run_mode，键集合同 golden', () => {
  const golden = JSON.parse(readFileSync(new URL('../../tests/golden/web_memory_v43.json', import.meta.url), 'utf8'));
  const want = Object.keys(golden.settings.launch).sort();
  const sel = { ...pre, workspace: '', run_mode: '', device: 'mac', repos: [{ name: 'a', mode: 'write' }] };
  const p = R.launchPayload(NEW, sel, 'hi', [], false);
  assert.ok(!('workspace' in p) && !('run_mode' in p));
  assert.deepEqual(Object.keys(p).sort(), want.filter((k) => k !== 'device').sort()); // D2Y4T：不带 device
  // 默认：不带 repos
  const q = R.launchPayload(NEW, { ...pre, repos: [] }, 'hi', [], false);
  assert.deepEqual(Object.keys(q).sort(), ['effort', 'model', 'text', 'work_mode']);
});

test('switch payload：新 Covey 键集合同 golden；全取消发 []；负责人不带仓库', () => {
  const golden = JSON.parse(readFileSync(new URL('../../tests/golden/web_memory_v43.json', import.meta.url), 'utf8'));
  const cur = { ...pre, workspace: 'old', run_mode: 'direct', repos: [{ name: 'a', mode: 'read' }] };
  const p = R.switchPayload(NEW, cur, true, false);
  assert.deepEqual(Object.keys(p).sort(), Object.keys(golden.settings.switch).sort());
  assert.deepEqual(R.switchPayload(NEW, { ...cur, repos: [] }, true, false).repos, []);
  assert.ok(!('repos' in R.switchPayload(NEW, { ...cur, repos: [] }, false, false)));
  assert.ok(!('repos' in R.switchPayload(NEW, cur, true, true)));
  assert.deepEqual(R.switchPayload(OLD, OLD.preselect, false, false), { ...OLD.preselect });
});

test('换配置：初值取话题的 repos，变化才算 changed；worktree 拦截只在旧 Covey', () => {
  const cfg = { ...pre };
  const base = S.withTopicRepos(NEW, S.fromConfig(NEW, cfg), [{ name: 'a', mode: 'write', rules_read: true, branch: 'x' }]);
  assert.deepEqual(base.repos, [{ name: 'a', mode: 'write' }]);
  assert.deepEqual(S.changedFields(base, { ...base }), []);
  assert.deepEqual(S.changedFields(base, { ...base, repos: [{ name: 'a', mode: 'read' }] }), ['repos']);
  assert.deepEqual(S.changedFields(base, { ...base, device: 'box' }), ['device']);
  assert.equal(S.withTopicRepos(OLD, S.fromConfig(OLD, OLD.preselect), [{ name: 'a', mode: 'read' }]).repos, undefined);
  const wt = { ...base, run_mode: 'worktree', effort: 'low' };
  assert.equal(S.switchBlock(NEW, base, wt, false), '');
  assert.match(S.switchBlock(OLD, S.fromConfig(OLD, OLD.preselect), { ...OLD.preselect, run_mode: 'worktree', effort: 'low', workspace: 'dev' }, false), /飞书/u);
});

test('T879 样式与面板：仓库已选未选分得开，设备不拿第一台兜底，换配置里设备只读、仓库默认折起', () => {
  const css = readFileSync(new URL('../client/src/cv/launch.css', import.meta.url), 'utf8');
  const rule = (sel) => (css.match(new RegExp(`\\.cv ${sel.replace(/\./g, '\\.')}\\{([^}]*)\\}`)) ?? [])[1] ?? '';
  assert.doesNotMatch(rule('.lx-rp'), /dashed/u); // 未选不再是虚线灰
  assert.match(rule('.lx-rp.read'), /background:/u);
  assert.match(rule('.lx-rp.write'), /background:/u);
  assert.match(rule('.lx-rp.write .lx-rm'), /background:var\(--a1\)/u); // 可写的角标实心
  assert.notEqual(rule('.lx-rp.read'), rule('.lx-rp.write'));
  const panel = readFileSync(new URL('../client/src/cv/LaunchPanel.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(panel, /devices\[0\]\?\.name/u); // 旧写法：没选设备就显示第一台（testbox）
  assert.match(panel, /aria-expanded=\{shown\}/u); // 换配置：设备和仓库折成一行，点开才动
  assert.match(panel, /换配置换不了机器；要用别的设备，在话题里用 covey device/u);
  assert.doesNotMatch(panel, /label="执行设备"|pickDevice|deviceChoices|showDevice/u); // D2Y4T：新话题面板没有执行设备下拉
  const bar = readFileSync(new URL('../client/src/cv/ConfigBar.tsx', import.meta.url), 'utf8');
  assert.match(bar, /switching:/u);
  assert.match(bar, /'device' in config/u); // 旧 Covey 的 config 没有 device：不写设备，不假装是本机
});

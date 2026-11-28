// 设置页「设备」页（v46）的纯函数：一台机器一行的点、标签、几行灰字，添加、改、装监控节点的 payload。
// 跑：node --import ./scripts/alias.mjs --test scripts/devices-page.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as D from '../client/src/cv/devicesPage.ts';

const row = (extra = {}) => ({ alias: 'macmini', title: 'Mac mini', note: '构建', access: 'write', listed: true, ssh: '-p 22 me@mac', runs: true,
  link: { state: 'ok', text: '连得上 · 2 分钟前' }, node: null, check: null, checking: null, local: false, ...extra });
const ago = (sec, now) => `${Math.floor((now - sec) / 60)} 分钟前`;

test('旧 Covey 不推 rows：照旧画节点列表', () => {
  assert.equal(D.hasRows({ summary: '', items: [] }), false);
  assert.equal(D.hasRows({ summary: '', items: [], rows: [] }), true);
});
test('点：有节点看节点，没节点看 ssh，体检中闪', () => {
  assert.equal(D.rowDot(row()), 'ok');
  assert.equal(D.rowDot(row({ link: { state: 'down', text: '' } })), 'down');
  assert.equal(D.rowDot(row({ node: { dot: 'tight', line: '', grey: '' }, link: { state: 'down', text: '' } })), 'tight');
  assert.equal(D.rowDot(row({ runs: false, link: null })), 'never');
  assert.equal(D.busy(row({ checking: { state: 'checking', message: '' } })), true);
});
test('标签：可写 / 只读 / 没设；没 ssh 加“只能看状况”；名单外的写“只有监控”', () => {
  assert.deepEqual(D.rowTags(row()), ['可写']);
  assert.deepEqual(D.rowTags(row({ access: '' })), ['没设']);
  assert.deepEqual(D.rowTags(row({ access: 'read', runs: false })), ['只读', '只能看状况']);
  assert.deepEqual(D.rowTags(row({ listed: false, runs: false })), ['只有监控']);
  assert.deepEqual(D.rowTags(row({ local: true, listed: false })), ['Covey 所在']);
});
test('灰字：连接、监控、体检', () => {
  assert.equal(D.linkLine(row()), 'ssh -p 22 me@mac · 连得上 · 2 分钟前');
  assert.equal(D.linkLine(row({ runs: false })), '');
  assert.equal(D.nodeLine(row()), '没装监控');
  assert.equal(D.nodeLine(row({ node: { dot: 'ok', line: 'CPU 3%', grey: '' } })), '监控 CPU 3%');
  assert.equal(D.nodeLine(row({ local: true })), '');
  assert.deepEqual(D.checkLine(row(), 1000, ago), { text: '还没体检', bad: false });
  assert.deepEqual(D.checkLine(row({ check: { ok: true, at: 880, text: 'x' } }), 1000, ago), { text: '体检通过 · 2 分钟前', bad: false });
  assert.deepEqual(D.checkLine(row({ check: { ok: false, at: 880, text: '连不上' } }), 1000, ago), { text: '体检没过（2 分钟前）：连不上', bad: true });
  assert.deepEqual(D.checkLine(row({ checking: { state: 'failed', message: 'ssh 超时' } }), 1000, ago), { text: '体检没过：ssh 超时', bad: true });
  assert.equal(D.checkLine(row({ checking: { state: 'checking', message: '' } }), 1000, ago).text.startsWith('体检中'), true);
  assert.equal(D.checkLine(row({ runs: false }), 1000, ago).text, '');
});
test('添加：名字要对；新加的默认只读；没填 ssh 就不带', () => {
  assert.equal(D.deviceAddPayload({ ...D.emptyDraft(), name: 'Box' }), null);
  assert.deepEqual(D.deviceAddPayload({ ...D.emptyDraft(), name: 'box1' }), { what: 'device_add', name: 'box1', access: 'read' });
  assert.deepEqual(D.deviceAddPayload({ name: 'box1', title: ' 盒子 ', ssh: ' me@box -p 22 ', note: '跑\n数据', access: 'write' }),
    { what: 'device_add', name: 'box1', access: 'write', title: '盒子', note: '跑 数据', ssh: 'me@box -p 22' });
  assert.equal(D.addLabel({ ...D.emptyDraft(), ssh: 'x' }), '体检并添加');
  assert.equal(D.addLabel(D.emptyDraft()), '添加');
});
test('改：只带改了的，清空 ssh 不算改', () => {
  const r = row();
  assert.equal(D.deviceSetPayload(r, D.editDraftOf(r)), null);
  assert.deepEqual(D.deviceSetPayload(r, { ...D.editDraftOf(r), note: '新用途', ssh: '' }), { what: 'device_set', alias: 'macmini', note: '新用途' });
  assert.deepEqual(D.deviceSetPayload(r, { ...D.editDraftOf(r), ssh: 'me@mac2' }), { what: 'device_set', alias: 'macmini', ssh: 'me@mac2' });
  assert.deepEqual(D.accessPayload(r, 'read'), { what: 'device_set', alias: 'macmini', access: 'read' });
});
test('装监控节点：有 ssh、没节点、Covey 在听才有，用现有的 node_add', () => {
  assert.equal(D.canInstallNode(row(), true), true);
  assert.equal(D.canInstallNode(row(), false), false);
  assert.equal(D.canInstallNode(row({ node: { dot: 'ok', line: '', grey: '' } }), true), false);
  assert.equal(D.canInstallNode(row({ runs: false }), true), false);
  assert.deepEqual(D.nodeAddPayload(row()), { what: 'node_add', name: 'macmini', title: 'Mac mini', ssh: '-p 22 me@mac' });
});
test('移除的确认说清楚留什么、撤什么', () => {
  assert.equal(D.removeAsk(row()), '移除 Mac mini？从设备名单去掉，它的体检记录留着');
  assert.equal(D.removeAsk(row({ node: { dot: 'ok', line: '', grey: '' } })), '移除 Mac mini？从设备名单去掉，它的体检记录留着，撤销它的监控节点');
});

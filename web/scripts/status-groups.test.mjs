// 侧栏的状态分组（T753，client/src/cv/statusGroups.ts）：和看板同一口径、每行写什么。跑：npm run test:status
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { statusGroups, sinceWords, shownRows, SHOWN } from '../client/src/cv/statusGroups.ts';

const topic = (key, tone, extra = {}) => ({ key, kind: 'topic', number: Number(key.slice(1)), title: `话题${key}`, tone, updated_at: 1000, project_key: null, ...extra });
const snap = {
  projects: [{ key: 'p1', title: '账单专项', avatar: 'fox', status_reason: '', updated_at: 900 }, { key: 'p2', title: '夜航改版', avatar: 'owl', status_reason: 'token 用完了', updated_at: 950 }],
  questions: [{ topic_key: 'k31', state: 'open', asked_at: 800, questions: [{ question: '报表按周还是按月？' }] }, { topic_key: 'L1', state: 'open', asked_at: 700, questions: [{ question: '用 v1.9 还是 v1.8？' }] }],
  topics: [],
};
const k31 = topic('k31', 'waiting');
const b0 = { key: 'b0', kind: 'butler', number: 0, title: '管家', tone: 'waiting', updated_at: 990 };
const L1 = { key: 'L1', kind: 'lead', number: 0, title: '负责人', tone: 'waiting', updated_at: 990, project_key: 'p1' };
const k660 = topic('k660', 'stopped', { latest_failure: { title: '结果没能发出去；点重新投递再发一次' } });
const k641 = topic('k641', 'stopped');
const k655 = topic('k655', 'stuck', { progress: { phase: '跑安装器测试', last_activity_at: 600 } });
const k586 = topic('k586', 'running', { progress: { phase: '等新版组第 1 轮评审', plan_done: 1, plan_total: 4 } });
const a = { waiting: [k31, b0, L1], stopped: [k660, k641], stuck: [k655], projects: [snap.projects[1]], proposals: [] };

test('要你处理照看板的顺序：等你 → 停住了 → 护栏停住的专项 → 可能卡住', () => {
  const g = statusGroups(a, snap, [k586]);
  assert.deepEqual(g.act.map((r) => r.key), ['k31', 'b0', 'L1', 'k660', 'k641', 'p:p2', 'k655']);
  assert.deepEqual(g.act.map((r) => r.tone), ['waiting', 'waiting', 'waiting', 'stopped', 'stopped', 'stopped', 'stuck']);
});

test('整理专项要你定的建议：每条一行跟在等你的题后面，点了去看板；行数就是算进要你处理的件数（T978）', () => {
  const tidy = { id: 'x1', headline: '把 3 个账单话题建成专项', verb: '建专项', state: 'open', at: 850 };
  const g = statusGroups({ ...a, proposals: [tidy] }, snap, []);
  assert.deepEqual(g.act.map((r) => r.key), ['k31', 'b0', 'L1', 'tidy:x1', 'k660', 'k641', 'p:p2', 'k655']);
  const row = g.act.find((r) => r.key === 'tidy:x1');
  assert.deepEqual([row.kind, row.tone, row.to, row.title, row.sub, row.since], ['tidy', 'waiting', '/', '整理专项', '要你定：把 3 个账单话题建成专项', 850]);
});

test('第二行灰字：要你定什么、停在哪、可能卡在哪一步；等你的从提问时算起', () => {
  const g = statusGroups(a, snap, []);
  const by = Object.fromEntries(g.act.map((r) => [r.key, r]));
  assert.equal(by.k31.sub, '要你定：报表按周还是按月？');
  assert.equal(by.k31.since, 800);
  assert.equal(by.b0.sub, '在等你', '管家没有开着的题时只写在等你');
  assert.equal(by.k660.sub, '停住了：结果没能发出去', '原因只取第一个“；”前那句（同看板、core/failure.py split_title）');
  assert.equal(by.k641.sub, '停住了：上一轮没做完');
  assert.equal(by['p:p2'].sub, '停住了：token 用完了');
  assert.equal(by.k655.sub, '可能卡住 · 跑安装器测试');
  assert.equal(by.k655.since, 600);
});

test('谁：话题带 T 号去话题页，管家去大厅，负责人写专项名去专项页', () => {
  const by = Object.fromEntries(statusGroups(a, snap, []).act.map((r) => [r.key, r]));
  assert.deepEqual([by.k31.kind, by.k31.number, by.k31.to], ['topic', 31, '/topics/k31']);
  assert.deepEqual([by.b0.title, by.b0.to, by.b0.number], ['管家', '/butler', null]);
  assert.deepEqual([by.L1.title, by.L1.to, by.L1.avatar], ['账单专项 · 负责人', '/projects/p1', 'fox']);
  assert.deepEqual([by['p:p2'].kind, by['p:p2'].to, by['p:p2'].avatar], ['project', '/projects/p2', 'owl']);
});

test('在做：写现在这一步和第几步，有开着的题的不算在做', () => {
  const asked = topic('k31', 'running');
  const g = statusGroups({ waiting: [], stopped: [], stuck: [], projects: [], proposals: [] }, snap, [k586, asked]);
  assert.deepEqual(g.running.map((r) => r.key), ['k586']);
  assert.equal(g.running[0].sub, '等新版组第 1 轮评审');
  assert.equal(g.running[0].plan, '1/4');
  assert.equal(g.running[0].since, undefined);
});

test('在做按编号大的在前，同步改了 updated_at 也不换位（T779：快照按 updated_at 排，照它排一秒一换位）', () => {
  const k736 = topic('k736', 'running', { updated_at: 3000 });
  const k772 = topic('k772', 'running', { updated_at: 2000 });
  const k778 = topic('k778', 'running', { updated_at: 1000 });
  const before = statusGroups(a, snap, [k736, k772, k778]).running.map((r) => r.key);
  // 下一次同步：k772 刚写了进度，快照把它排到最前
  const after = statusGroups(a, snap, [{ ...k772, updated_at: 4000 }, k736, k778]).running.map((r) => r.key);
  assert.deepEqual(before, ['k778', 'k772', 'k736']);
  assert.deepEqual(after, before);
});

test('等了多久的字：刚刚、分、小时、天', () => {
  assert.equal(sinceWords(30), '刚刚');
  assert.equal(sinceWords(39 * 60 + 5), '39 分');
  assert.equal(sinceWords(3 * 3600 + 10), '3 小时');
  assert.equal(sinceWords(2 * 86400), '2 天');
});

test('每组收着时列几行：多出 1 个直接列出，多出 2 个以上才折；展开了列全（T768：在做 7 个只列了 6 个）', () => {
  assert.equal(SHOWN, 6);
  assert.equal(shownRows(0, false), 0);
  assert.equal(shownRows(6, false), 6);
  assert.equal(shownRows(7, false), 7, '7 个在做不再折成“还有 1 个在做”');
  assert.equal(shownRows(8, false), 6);
  assert.equal(shownRows(20, false), 6);
  assert.equal(shownRows(8, true), 8);
  assert.equal(shownRows(20, true), 20);
});

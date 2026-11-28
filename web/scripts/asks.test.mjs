// “要你处理”一个数（T647 P0-2）、护栏停住的专项（P0-3）、「先放着」并成「不管了」（P1-C3）。跑：npm run test:asks
// 口径照 covey core/board_read.py 的 Asks：等你（话题、管家、负责人的题）+ 停住了（普通话题，跟看板分组走；加护栏停住的专项）+ 可能卡住。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as P from '../client/src/cv/projectData.ts';
import * as L from '../client/src/cv/lib.ts';
import * as T from '../client/src/cv/topicText.ts';

const CV = join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'cv');
const NOW = 1_800_000_000;

const topic = (key, extra = {}) => ({
  key, number: Number(key.replace(/\D/gu, '')) || null, title: key, kind: 'topic', project_key: null, state: '做完', unseen: false,
  updated_at: NOW - 600, harness: 'claude', model: '', latest: '', latest_role: 'agent', round: 1, started_at: NOW - 3600, context: null, running: null, ...extra,
});
const project = (key, extra = {}) => ({
  key, title: `专项${key}`, goal: '', status: '在做', status_reason: '', lead_topic_key: `lead-${key}`, started_at: NOW - 86400, topic_keys: [],
  memory: [], library: [], updated_at: NOW - 300, counts: { topics: 0, waiting: 0, running: 0 }, ...extra,
});
const question = (topicKey) => ({ token: `q-${topicKey}`, topic_key: topicKey, turn_key: null, questions: [{ id: 'a', question: '选哪个？', options: [] }], state: 'open', answer: null });

// 一个开着看板的私聊：board 字段在（v6）
const snap = {
  synced_at: null,
  butler_key: 'butler',
  projects: [
    project('p1', { status: '停住', status_reason: 'token 用完了' }),
    project('p2', { status: '停住', status_reason: '主人停止' }),
    project('p3', { status: '收起', status_reason: '没有新的事' }),
    project('p4'),
  ],
  topics: [
    topic('butler', { kind: 'butler', number: 0, tone: 'waiting', state: '等你' }),
    topic('lead-p4', { kind: 'lead', number: 0, project_key: 'p4', tone: 'waiting', state: '等你' }),
    topic('T1', { tone: 'waiting', board: 'waiting', state: '等你' }),
    topic('T2', { tone: 'stopped', board: 'stopped', latest_status: 'failed' }),
    // 点了「不管了」：Covey 把它从看板拿掉，board 推回空串
    topic('T3', { tone: 'stopped', board: '', latest_status: 'failed' }),
    topic('T4', { tone: 'stuck', board: 'running', state: '在做' }),
    topic('T5', { tone: 'running', board: 'running', state: '在做' }),
    topic('T6', { tone: 'result', board: 'result', unseen: true }),
    // 有题在等的停住话题只算一次等你
    topic('T7', { tone: 'stopped', board: 'stopped', latest_status: 'failed' }),
  ],
  questions: [question('butler'), question('lead-p4'), question('T1'), question('T7')],
};

test('要你处理：管家和负责人的题算进等你，可能卡住算，护栏停住的专项算停住了；点过不管了的、你自己停的专项不算', () => {
  const a = P.asks(snap, NOW);
  assert.deepEqual(a.waiting.map((t) => t.key).sort(), ['T1', 'T7', 'butler', 'lead-p4']);
  assert.deepEqual(a.stopped.map((t) => t.key), ['T2']);
  assert.deepEqual(a.stuck.map((t) => t.key), ['T4']);
  assert.deepEqual(a.projects.map((p) => p.key), ['p1']);
  assert.deepEqual(P.askCounts(a), { waiting: 4, stopped: 2, stuck: 1 });
  assert.equal(P.askTotal(a), 7);
  assert.deepEqual([...P.stoppedSet(a)], ['T2']);
});

test('整理专项要你定的建议算进等你，定过的不算；答完的题不再让话题算等你（T978）', () => {
  const tidy = { proposals: [{ id: 'x1', state: 'open' }, { id: 'x2', state: 'done' }, { id: 'x3', state: 'open' }] };
  const a = P.asks(snap, NOW, tidy);
  assert.deepEqual(a.proposals.map((p) => p.id), ['x1', 'x3']);
  assert.deepEqual(P.askCounts(a), { waiting: 6, stopped: 2, stuck: 1 });
  assert.deepEqual(P.asks(snap, NOW).proposals, []);  // 没取到整理专项：不算
  // T5 在跑，它的题刚答完（state 不是 open）：不算等你
  const answered = { ...snap, questions: [...snap.questions, { ...question('T5'), state: 'answered' }] };
  assert.ok(!P.asks(answered, NOW).waiting.some((t) => t.key === 'T5'));
});

test('看板、侧栏、手机标签栏都用这一个函数，不再各数各的', () => {
  const src = (f) => readFileSync(join(CV, f), 'utf8');
  for (const f of ['Board.tsx', 'Shell.tsx']) assert.match(src(f), /\basks\(/u, `${f} 没用 projectData.ts 的 asks`);
  // 旧的三套：看板滤掉管家负责人（kind === 'topic' 后再数等你）、侧栏 waitingN + stoppedKeys.size、标签栏自己 filter
  assert.doesNotMatch(src('Shell.tsx'), /waitingN|stoppedKeys\.size\b/u);
  assert.doesNotMatch(src('Board.tsx'), /waiting\.length \+ stopped\.length \+ stuck\.length/u);
});

test('没开看板的旧数据（没有 board）：停住了取三天内的，管家和负责人停住不算', () => {
  const old = {
    ...snap,
    projects: [],
    topics: [
      topic('T8', { tone: 'stopped', latest_status: 'failed', updated_at: NOW - 3600 }),
      topic('T9', { tone: 'stopped', latest_status: 'failed', updated_at: NOW - 4 * 86400 }),
      topic('lead-x', { kind: 'lead', number: 0, tone: 'stopped', latest_status: 'failed' }),
    ],
    questions: [],
  };
  assert.deepEqual(P.asks(old, NOW).stopped.map((t) => t.key), ['T8']);
});

test('护栏停住的专项：红色“停住了 · 原因”；你自己停的是灰的已停止；收起的是空闲', () => {
  const [halted, mine, folded, live] = snap.projects;
  assert.equal(P.isHalted(halted), true);
  assert.equal(P.isHalted(mine), false);
  assert.equal(P.projectTone(halted), 'stopped');
  assert.equal(P.projectTone(mine), 'cancelled');
  assert.equal(P.projectTone(folded), 'idle');
  assert.equal(P.projectTone(live), '');
  assert.equal(P.haltWords(halted), '停住了 · token 用完了');
  assert.equal(P.haltWords(mine), '已停止');
  // 认不得的原因也算停住了（同 Covey pause_tone：不当成你停的）
  assert.equal(P.isHalted({ status: '停住', status_reason: '新原因' }), true);
  // 专项胶囊：护栏停住先说它自己，压过话题里最急的那件
  assert.deepEqual(P.projectPill(halted, ['waiting', 'running'], undefined), { tone: 'stopped', text: '停住了 · token 用完了' });
  assert.deepEqual(P.projectPill(mine, [], undefined), { tone: 'idle', text: '已停止' });
  assert.deepEqual(P.projectPill(live, ['running', 'stuck'], undefined), { tone: 'stuck', text: undefined });
  // 专项名下面那行：目标；没目标时用状态说明，但停住的原因已在胶囊上，“主人停止”这种内部说法不给人看
  assert.equal(P.projectNote({ goal: '目标：把第 18 题核对完', status: '在做', status_reason: '' }), '把第 18 题核对完');
  assert.equal(P.projectNote({ goal: '', status: '收起', status_reason: '没有新的事' }), '没有新的事');
  assert.equal(P.projectNote(mine), '');
  assert.equal(P.projectNote(halted), '');
});

test('先放着并成不管了：计数只看 Covey 推的看板分组，isStopped、asks 不再收本机记的轮次', () => {
  assert.equal(T.isStopped.length, 2);
  assert.equal(P.asks.length, 3);  // 快照、此刻、整理专项的建议（T978）：没有本机记的轮次
  // 页面代码里不再有只记在本机的“先放着”（useLater、putLater、covey.later 只剩清理旧缓存那一处）
  const hits = [];
  for (const f of readdirSync(CV).filter((n) => /\.(ts|tsx)$/u.test(n))) {
    const src = readFileSync(join(CV, f), 'utf8');
    if (/\b(useLater|putLater)\b|['"]先放着['"]|>先放着</u.test(src)) hits.push(f);
  }
  assert.deepEqual(hits, []);
  // 话题页的失败框和总览给的是「不管了」（DismissButton 发 dismiss）
  for (const f of ['Timeline.tsx', 'TopicOverview.tsx', 'Board.tsx']) assert.match(readFileSync(join(CV, f), 'utf8'), /DismissButton/u, `${f} 没用 DismissButton`);
});

test('旧的本机“先放着”缓存打开页面时删掉；没有就不动，隐私模式不抛', () => {
  const store = new Map([['covey.later', '["t1:3"]'], ['covey.theme', 'light']]);
  const fake = { getItem: (k) => (store.has(k) ? store.get(k) : null), removeItem: (k) => store.delete(k) };
  assert.deepEqual(L.dropLegacy(fake), ['covey.later']);
  assert.deepEqual([...store.keys()], ['covey.theme']);
  assert.deepEqual(L.dropLegacy(fake), []);
  assert.deepEqual(L.dropLegacy(null), []);
  const broken = { getItem: () => { throw new Error('SecurityError'); }, removeItem: () => { throw new Error('SecurityError'); } };
  assert.deepEqual(L.dropLegacy(broken), []);
});

test('不管了只在开着看板时画：话题带 board 才算开着', () => {
  assert.equal(L.boardOn(snap.topics), true);
  assert.equal(L.boardOn([{ key: 'a' }, { key: 'b' }]), false);
  assert.equal(L.boardOn([{ key: 'a', board: '' }]), true);
});

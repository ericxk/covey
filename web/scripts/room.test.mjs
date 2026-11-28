// 大厅（T624）的纯函数：几路轮次排成一条线、每句话交给了谁、@ 候选、引用怎么拼进正文、按负责人筛。跑：npm run test:room
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as R from '../client/src/cv/room.ts';

const B = 'butler';
const L = 'lead1';
const T = 'topic12';
const snap = {
  butler_key: B,
  synced_at: null,
  projects: [
    { key: 'p1', title: '夜航改版', goal: '', status: '在做', status_reason: '', lead_topic_key: L, started_at: 100, topic_keys: [T], memory: [], library: [], updated_at: 500, counts: { topics: 1, waiting: 0, running: 0 } },
    { key: 'p2', title: '旧专项', goal: '', status: '做完', status_reason: '', lead_topic_key: 'lead2', started_at: 50, topic_keys: [], memory: [], library: [], updated_at: 400, counts: { topics: 0, waiting: 0, running: 0 } },
    { key: 'p3', title: '节点', goal: '', status: '收起', status_reason: '', lead_topic_key: 'lead3', started_at: 60, topic_keys: [], memory: [], library: [], updated_at: 70, counts: { topics: 0, waiting: 0, running: 0 } },
  ],
  topics: [
    { key: T, number: 12, title: '密度', kind: 'topic', project_key: 'p1', state: '做完', unseen: false, updated_at: 300 },
    { key: 'other', number: 13, title: '别的', kind: 'topic', project_key: null, state: '做完', unseen: false, updated_at: 300 },
  ],
  questions: [{ token: 'q1', topic_key: T, turn_key: null, questions: [{ id: 'a', question: '字号用 13 还是 14？', options: [] }], state: 'open', answer: null, asked_at: 350 }],
};
const turn = (topic_key, seq, role, at, extra = {}) => ({
  key: `${topic_key}:${seq}`, seq, role, status: 'done', body: `${topic_key}-${seq}`, job_id: null, started_at: at, ended_at: at + 5, artifacts: [], topic_key, ...extra,
});

test('成员：管家在前，没结束的专项各一位负责人，收起的灰着，做完的不在', () => {
  const m = R.members(snap);
  assert.deepEqual(m.map((x) => [x.name, x.dim]), [['管家', false], ['节点 负责人', true], ['夜航改版 负责人', false]]);
});

test('一条线：按时刻排，管家那句写交给了谁；经管家转给负责人的那句不重画；话题只留发到私聊的结论', () => {
  const turns = [
    turn(B, 1, 'owner', 200),
    turn(B, 2, 'agent', 201, { handed: [{ key: L, kind: 'lead', number: 0, title: '夜航改版', project_key: 'p1', how: 'send' }] }),
    turn(L, 1, 'owner', 202, { by: 'owner', via: 'butler' }),
    turn(L, 2, 'agent', 210),
    turn(L, 3, 'event', 211),
    turn(T, 1, 'owner', 205),
    turn(T, 2, 'agent', 206, { relayed_at: 260 }),
    turn('other', 2, 'agent', 207),
  ];
  const rows = R.buildRoom({ turns, outbox: [], snap, butler: B });
  // 专项进出按时刻插进来（旧专项 p2 在 50 建、400 做完）；负责人的回执行不画；没发到私聊的话题轮次（other）不在
  assert.deepEqual(rows.map((r) => r.key), ['pj-in-p2', 'pj-in-p3', 'pj-in-p1', `${B}:1`, `${B}:2`, `${L}:2`, `${T}:2`, 'q-q1', 'pj-out-p2']);
  const owner = rows.find((r) => r.key === `${B}:1`);
  assert.equal(owner.route, '→ 管家 → 夜航改版 负责人');
  assert.deepEqual(owner.targets, [L]);
  assert.deepEqual(rows.map((r) => r.seq), rows.map((_, i) => i + 1));
});

test('直接对负责人说的写“→ X 负责人”；引用转给话题的那句（已执行）补一行“→ T12”', () => {
  const turns = [turn(L, 1, 'owner', 300), turn(L, 2, 'agent', 310)];
  const outbox = [
    { seq: 1, op_id: 'o1', kind: 'say', topic_key: T, project_key: null, payload: { text: '> 引用 T12 密度：\n> 正文\n\n再压一档', room: true }, state: 'done', message: '', created_at: new Date(320_000).toISOString() },
    { seq: 2, op_id: 'o2', kind: 'say', topic_key: T, project_key: null, payload: { text: '还没执行', room: true }, state: 'new', message: '', created_at: new Date(321_000).toISOString() },
  ];
  const rows = R.buildRoom({ turns, outbox, snap, butler: B });
  assert.equal(rows.find((r) => r.key === `${L}:1`).route, '→ 夜航改版 负责人');
  const sent = rows.filter((r) => r.synth === 'sent');
  assert.deepEqual(sent.map((r) => [r.key, r.route, r.role]), [['ob-o1', '→ T12', 'owner']]);
});

test('按负责人筛：它说的、你对它说的（直接或经管家）、专项里话题的结论和问题', () => {
  const turns = [
    turn(B, 1, 'owner', 200), turn(B, 2, 'agent', 201, { handed: [{ key: L, kind: 'lead', number: 0, title: '夜航改版', project_key: 'p1', how: 'send' }] }),
    turn(B, 3, 'owner', 220), turn(B, 4, 'agent', 221),
    turn(L, 2, 'agent', 230), turn(T, 2, 'agent', 240, { relayed_at: 241 }), turn('other', 2, 'agent', 250, { relayed_at: 251 }),
  ];
  const all = R.buildRoom({ turns, outbox: [], snap, butler: B });
  const mine = R.onlyLead(all, L, 'p1', snap, B);
  assert.deepEqual(mine.map((r) => r.key), ['pj-in-p1', `${B}:1`, `${L}:2`, `${T}:2`, 'q-q1']);
});

test('@ 候选：光标前打的 @ 和后面的字；名字里有就列', () => {
  assert.deepEqual(R.mentionAt('帮我 @夜', 5), { query: '夜', start: 3 });
  assert.equal(R.mentionAt('邮箱a@b', 5), null);
  assert.equal(R.mentionAt('@夜航 改', 6), null);
  // 放宽：中文后紧挨着也弹（T642）
  assert.deepEqual(R.mentionAt('请@夜', 3), { query: '夜', start: 1 });
  assert.deepEqual(R.mentionMatches(R.members(snap), '夜航').map((m) => m.name), ['夜航改版 负责人']);
});

test('交给谁：@ 优先，其次引用的那条，都没有是管家；引用拼成 Markdown 引用块', () => {
  const lead = R.members(snap)[2];
  const quote = { key: 'k', topic: T, who: 'T12 密度', text: '第一行\n\n\n\n第二行' };
  assert.equal(R.targetOf(null, null, B, snap).label, '管家');
  assert.equal(R.targetOf(null, quote, B, snap).label, 'T12');
  assert.equal(R.targetOf(lead, quote, B, snap).key, L);
  assert.equal(R.withQuote(' 再压一档 ', quote), '> 引用 T12 密度：\n> 第一行\n> \n> 第二行\n\n再压一档');
  assert.equal(R.withQuote('只说话', null), '只说话');
  assert.ok(R.quoteText('字'.repeat(400)).endsWith('…'));
});

test('专项页的负责人对话（T952）：负责人自己在等你的题照话题页画，发件行并上负责人自己的；大厅没有这道题', () => {
  const q = { token: 'ql', topic_key: L, turn_key: null, questions: [{ id: 'a', question: '合进 master 吗？', options: [] }], state: 'open', answer: null };
  const mine = [{ op_id: 'a1', kind: 'answer', state: 'done' }];
  const hall = [{ op_id: 's1', kind: 'say', state: 'failed' }, { op_id: 'a1', kind: 'answer', state: 'new' }];
  const lead = R.roomQuestion(true, { question: q, outbox: mine }, hall);
  assert.equal(lead.question, q);
  assert.deepEqual(lead.ops.map((o) => [o.op_id, o.state]), [['a1', 'done'], ['s1', 'failed']]);
  assert.deepEqual(R.roomQuestion(false, { question: q, outbox: mine }, hall), { question: null, ops: hall });
  // 负责人的对话还没取到（占位）时没有题，不报错
  assert.deepEqual(R.roomQuestion(true, undefined, hall), { question: null, ops: hall });
});

test('专项页的负责人对话接上了负责人自己的题：顶上“等你 · 要你定”、底下问题面板；不再一律 question: null（T952）', () => {
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'cv', 'Conversation.tsx'), 'utf8');
  assert.doesNotMatch(src, /question: null, outbox: rq\.data\.outbox/u);
  assert.match(src, /roomQuestion\(!!room\.lead, tq\.data/u);
  assert.match(src, /room\?\.lead && asking \? <AskBand q=\{asking\} \/>/u);
  // 负责人在等你时输入框不写“写你的回答”：不引用问题卡对它说的话是它新的一轮，题照旧等着（和飞书一样）
  assert.match(src, /waiting && !butler && topic\?\.kind !== 'lead' \? '写你的回答'/u);
});

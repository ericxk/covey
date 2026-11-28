// 网页发出的操作怎么等、回执后拉什么、气泡写什么（T765 F1、F2、F3）。纯函数和假时钟，不碰网络。
// 跑：npm run test:optrack
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { QueryClient } from '@tanstack/query-core';
import * as K from '../client/src/cv/opTrack.ts';
import * as O from '../client/src/cv/opWords.ts';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src');
const read = (rel) => readFileSync(join(SRC, rel), 'utf8');

/** 假时钟：sleep 只把虚拟时间往前拨；status 记下每次被问的时刻，按 script(t, n) 回话，耗 lat 毫秒 */
function clock(script, { lat = 0 } = {}) {
  const c = { t: 0, calls: [], pulls: [], sleeps: [] };
  c.now = () => c.t;
  c.sleep = async (ms) => { c.sleeps.push(ms); c.t += ms; };
  c.status = async (opId, waitSec) => {
    c.calls.push({ at: c.t, opId, waitSec });
    const r = script(c.t, c.calls.length);
    c.t += typeof lat === 'function' ? lat(c.calls.length) : lat;
    if (r instanceof Error) throw r;
    return r;
  };
  c.pull = async (p) => { c.pulls.push({ at: c.t, id: K.pullId(p) }); };
  return c;
}

test('问回执的间隔：250 毫秒起，几次后 500 毫秒，约 11 秒后 1 秒', () => {
  assert.deepEqual([0, 1, 2, 3].map(K.pollGap), [250, 250, 250, 250]);
  assert.deepEqual([4, 10, 19].map(K.pollGap), [500, 500, 500]);
  assert.deepEqual([20, 50].map(K.pollGap), [1000, 1000]);
});

test('等回执：旧服务端（马上回、不认 wait）按间隔问，第 3 次见到 done 就返回；每次都带 wait', async () => {
  const c = clock((t, n) => (n >= 3 ? { state: 'done', message: '已开下一轮' } : { state: 'new', message: '' }));
  const r = await K.trackReceipt(c, 'op1');
  assert.deepEqual(r, { phase: 'done', message: '已开下一轮' });
  assert.deepEqual(c.calls.map((x) => x.at), [250, 500, 750]);
  assert.ok(c.calls.every((x) => x.waitSec === K.SERVER_WAIT_S && x.opId === 'op1'));
});

test('等回执：新服务端挂着等（一次问 2 秒才回），问的时间算在间隔里，不连着打也不多睡', async () => {
  const c = clock((t, n) => (n >= 2 ? { state: 'done', message: 'ok' } : { state: 'new', message: '' }), { lat: 2000 });
  const r = await K.trackReceipt(c, 'op1');
  assert.equal(r.phase, 'done');
  // 第一次 250 毫秒问、挂到 2250；已经超过间隔，马上再问
  assert.deepEqual(c.calls.map((x) => x.at), [250, 2250]);
});

test('等回执：failed 带原因；问不到（网络抖）接着问；等满一分钟是 timeout，不是失败', async () => {
  const f = clock(() => ({ state: 'failed', message: '这一轮已经接着做了' }));
  assert.deepEqual(await K.trackReceipt(f, 'a'), { phase: 'failed', message: '这一轮已经接着做了' });
  const flaky = clock((t, n) => (n < 3 ? new Error('Network Error') : { state: 'done', message: '' }));
  assert.equal((await K.trackReceipt(flaky, 'a')).phase, 'done');
  assert.equal(flaky.calls.length, 3);
  const never = clock(() => ({ state: 'new' }));
  const r = await K.trackReceipt(never, 'a');
  assert.deepEqual(r, { phase: 'timeout', message: '' });
  assert.ok(never.t >= O.OP_WAIT_MS && never.t < O.OP_WAIT_MS + 1500, `等了 ${never.t} 毫秒`);
  // 一分钟里问的次数：4×250 + 16×500 + 之后每秒一次，约 70 次小请求（以前每 0.7、1.5 秒问，约 45 次）；新服务端挂着等时远少于这个数
  assert.ok(never.calls.length > 60 && never.calls.length < 80, `问了 ${never.calls.length} 次`);
});

test('等回执：组件卸了（gone）就不再问，按 timeout 返回', async () => {
  let gone = false;
  const c = clock((t, n) => { if (n === 2) gone = true; return { state: 'new' }; });
  const r = await K.trackReceipt(c, 'a', { gone: () => gone });
  assert.equal(r.phase, 'timeout');
  assert.equal(c.calls.length, 2);
});

test('回执后拉什么：话题的轮次、大厅和快照各一次，之后只补取这个话题的轮次（不取快照）', () => {
  const say = K.pullPlan({ kind: 'say', topic_key: 't1', payload: { text: 'hi' } });
  assert.deepEqual(say.all.map(K.pullId), ['turns:t1', 'snapshot']);
  assert.deepEqual(say.follow.map(K.pullId), ['turns:t1']);
  const room = K.pullPlan({ kind: 'say', topic_key: 'bt', payload: { text: 'hi', room: true } });
  assert.deepEqual(room.all.map(K.pullId), ['turns:bt', 'room', 'snapshot']);
  assert.deepEqual(room.follow.map(K.pullId), ['turns:bt', 'room']);
  // 话题页没开着（看板上答题）：补取快照代替话题轮次
  assert.deepEqual(K.pullPlan({ kind: 'answer', topic_key: 't1', payload: {} }, false).follow.map(K.pullId), ['turns:t1', 'snapshot']);
  assert.deepEqual(K.pullPlan({ kind: 'seen', topic_key: 't1', payload: {} }), { all: [], follow: [] });
  assert.deepEqual(K.pullPlan({ kind: 'settings', payload: {} }).all.map(K.pullId), ['settings']);
  assert.deepEqual(K.pullPlan({ kind: 'tidy_decide', payload: {} }).all.map(K.pullId), ['tidy', 'snapshot']);
  assert.deepEqual(K.pullPlan({ kind: 'memory_edit', payload: {} }).all.map(K.pullId), ['memory']);
  assert.deepEqual(K.pullPlan({ kind: 'launch', payload: {} }).all.map(K.pullId), ['snapshot']);
  // 任何一种都没有“每 300 毫秒取整份快照”：follow 里的快照只在话题页没开着时才有
  for (const k of ['say', 'answer', 'stop', 'recover', 'dismiss', 'switch']) {
    assert.ok(!K.pullPlan({ kind: k, topic_key: 't', payload: {} }).follow.some((p) => p.t === 'snapshot'), k);
  }
});

test('回执后取数的节奏：先取齐一次，没看到预期的新状态就每 500 毫秒取话题轮次，最长 3 秒；看到就停', async () => {
  const plan = K.pullPlan({ kind: 'say', topic_key: 't1', payload: { text: 'hi' } });
  const never = clock(() => null);
  const seenNo = await K.settle({ now: never.now, sleep: never.sleep, pull: never.pull, view: () => ({}) }, plan, { phase: 'done', message: '' }, () => false);
  assert.equal(seenNo, false);
  assert.deepEqual(never.pulls.slice(0, 2).map((x) => x.id), ['turns:t1', 'snapshot']);
  assert.ok(never.pulls.slice(2).every((x) => x.id === 'turns:t1'));
  assert.deepEqual(never.pulls.slice(2).map((x) => x.at), [500, 1000, 1500, 2000, 2500, 3000]);
  // 第二次取完就看到了：只取了两轮，不再往后
  const c = clock(() => null);
  let n = 0;
  const seen = await K.settle({ now: c.now, sleep: c.sleep, pull: async (p) => { await c.pull(p); n += 1; }, view: () => ({}) }, plan, { phase: 'done', message: '' }, () => n >= 4);
  assert.equal(seen, true);
  assert.deepEqual(c.pulls.map((x) => x.at), [0, 0, 500, 1000]);
});

test('回执后取数：没有预期的新状态的只补取两次；没办成的取一次；预期一开始就成立不再取', async () => {
  const plan = { all: [{ t: 'settings' }], follow: [{ t: 'settings' }] };
  const blind = clock(() => null);
  await K.settle({ now: blind.now, sleep: blind.sleep, pull: blind.pull, view: () => ({}) }, plan, { phase: 'done', message: '' }, null);
  assert.equal(blind.pulls.length, 1 + K.SETTLE_BLIND);
  const failed = clock(() => null);
  await K.settle({ now: failed.now, sleep: failed.sleep, pull: failed.pull, view: () => ({}) }, plan, { phase: 'failed', message: 'x' }, () => false);
  assert.equal(failed.pulls.length, 1);
  const already = clock(() => null);
  assert.equal(await K.settle({ now: already.now, sleep: already.sleep, pull: already.pull, view: () => ({}) }, plan, { phase: 'done', message: '' }, () => true), true);
  assert.equal(already.pulls.length, 1);
  // 一份取数失败不影响别的
  const boom = clock(() => null);
  await K.settle({ now: boom.now, sleep: boom.sleep, pull: async (p) => { if (p.t === 'turns') throw new Error('x'); await boom.pull(p); }, view: () => ({}) }, K.pullPlan({ kind: 'say', topic_key: 't', payload: {} }), { phase: 'done', message: '' }, null);
  assert.ok(boom.pulls.some((x) => x.id === 'snapshot'));
});

const turn = (key, role, body, seq = 1, status = 'done') => ({ key, role, body, seq, status });
test('预期的新状态：你的话出现、问题收起、话题停了、新一轮开了、卡片从看板消失、新话题进了快照', () => {
  const before = new Set(['t:1']);
  const say = K.expectation({ op_id: 'o1', kind: 'say', topic_key: 't', payload: { text: ' 你好 ' } }, before);
  assert.equal(say({ turns: { turns: [turn('t:1', 'owner', '你好')] } }), false, '原来就有的不算');
  assert.equal(say({ turns: { turns: [turn('t:1', 'owner', '你好'), turn('t:2', 'owner', '你好')] } }), true);
  assert.equal(say({ turns: { turns: [turn('t:2', 'owner', '你好\n\n附件：a.png')] } }), true, '带附件的正文后面有附件块');
  assert.equal(say({ turns: { turns: [turn('t:2', 'owner', '别的话')] } }), false);
  assert.equal(say({ room: { turns: [turn('ob-o1', 'owner', '')] } }), true, '大厅的落地条目');
  const ans = K.expectation({ op_id: 'o', kind: 'answer', topic_key: 't', payload: { token: 'q1', answers: {} } }, before);
  assert.equal(ans({ turns: { question: { token: 'q1', state: 'open' }, turns: [] } }), false);
  assert.equal(ans({ turns: { question: null, turns: [] } }), true);
  assert.equal(ans({ turns: { question: { token: 'q2', state: 'open' }, turns: [] } }), true);
  assert.equal(ans({ snap: { questions: [{ token: 'q1' }], topics: [] } }), false, '看板：快照里还有这道题');
  assert.equal(ans({ snap: { questions: [], topics: [] } }), true);
  assert.equal(ans({}), false, '什么数据都没有不算看到');
  const aside = K.expectation({ op_id: 'o', kind: 'answer', topic_key: 't', payload: { token: 'q1', aside: true } }, before);
  assert.equal(aside({ turns: { question: { token: 'q1', state: 'aside' }, turns: [] } }), true);
  const restore = K.expectation({ op_id: 'o', kind: 'answer', topic_key: 't', payload: { token: 'q1', restore: true } }, before);
  assert.equal(restore({ turns: { question: { token: 'q1', state: 'open' }, turns: [] } }), true);
  assert.equal(restore({ turns: { question: { token: 'q1', state: 'aside' }, turns: [] } }), false);
  const stop = K.expectation({ op_id: 'o', kind: 'stop', topic_key: 't', payload: {} }, before);
  assert.equal(stop({ turns: { topic: { state: '在做' }, turns: [] } }), false);
  assert.equal(stop({ turns: { topic: { state: '空闲' }, turns: [turn('t:2', 'agent', '', 2, 'running')] } }), false);
  assert.equal(stop({ turns: { topic: { state: '已停止' }, turns: [turn('t:2', 'agent', '', 2, 'cancelled')] } }), true);
  assert.equal(K.expectation({ op_id: 'o', kind: 'stop', topic_key: 't', payload: { collab: 'c1' } }, before), null);
  const rec = K.expectation({ op_id: 'o', kind: 'recover', topic_key: 't', payload: { action: 'continue', job_id: '3' } }, before);
  assert.equal(rec({ turns: { topic: { state: '已停止' }, turns: [turn('t:1', 'agent', '')] } }), false);
  assert.equal(rec({ turns: { topic: { state: '在做' }, turns: [] } }), true);
  assert.equal(K.expectation({ op_id: 'o', kind: 'recover', topic_key: 't', payload: { action: 'stop_auto_retry' } }, before), null);
  const dis = K.expectation({ op_id: 'o', kind: 'dismiss', topic_key: 't', payload: {} }, before);
  assert.equal(dis({ snap: { topics: [{ key: 't', board: 'stopped' }], questions: [] } }), false);
  assert.equal(dis({ snap: { topics: [{ key: 't', board: '' }], questions: [] } }), true);
  const launch = K.expectation({ op_id: 'o', kind: 'launch', payload: {} }, before, '已开话题 T915，任务 #6001');
  assert.equal(launch({ snap: { topics: [{ number: 914, kind: 'topic' }], questions: [] } }), false);
  assert.equal(launch({ snap: { topics: [{ number: 915, kind: 'topic' }], questions: [] } }), true);
  assert.equal(K.expectation({ op_id: 'o', kind: 'launch', payload: {} }, before, '已开话题'), null);
  assert.equal(K.expectation({ op_id: 'o', kind: 'settings', payload: {} }, before), null);
});

test('输入框发的话的气泡：写进网页库就是正式的样子“✓ 已发出”，Covey 回执后写它的原话，不是笼统的“已执行”', () => {
  const sending = O.bubbleWords({ phase: 'sending' });
  assert.deepEqual([sending.text, sending.faded], ['发送中…', true]);
  const sent = O.bubbleWords({ phase: 'sent' });
  assert.deepEqual([sent.text, sent.tone, sent.faded], ['✓ 已发出', 'wait', false]);
  assert.match(sent.title, /不在线也会接着办/u);
  assert.equal(O.SENT_WORD, '✓ 已发出');
  // 回执：发件行或跟踪器，谁先到听谁的；写 Covey 的原话
  assert.equal(O.bubbleWords({ phase: 'sent' }, { state: 'done', message: '已开下一轮' }).text, '已开下一轮');
  assert.equal(O.bubbleWords({ phase: 'sent' }, undefined, { phase: 'done', message: '已作为补充交给在做的任务 #12' }).text, '已作为补充交给在做的任务 #12');
  assert.equal(O.bubbleWords({ phase: 'sent' }, { state: 'new' }, { phase: 'waiting', message: '' }).text, '✓ 已发出');
  assert.equal(O.bubbleWords({ phase: 'sent' }, { state: 'done', message: '' }).text, '已执行', '回执没写话才用“已执行”');
  assert.equal(O.bubbleWords({ phase: 'sent' }, { state: 'done', message: '整理专项没有开（COVEY_PROJECT_TIDY）' }).text, '整理专项没有开');
  const bad = O.bubbleWords({ phase: 'sent' }, { state: 'failed', message: '话题不在了' });
  assert.deepEqual([bad.text, bad.tone], ['没办成：话题不在了', 'bad']);
  assert.equal(O.bubbleWords({ phase: 'failed', error: 'Network Error' }).text, '没发出去：Network Error');
  assert.equal(O.bubbleWords({ phase: 'sent' }, undefined, { phase: 'timeout', message: '' }).text, '✓ 已发出', '等满了没回音：操作还在发件箱里，仍写已发出');
});

/* ---------- 源码里的约定（旧写法不许回来） ---------- */
function files(dir, out = []) {
  for (const e of readdirSync(join(SRC, dir), { withFileTypes: true })) {
    if (e.isDirectory()) files(join(dir, e.name), out);
    else if (/\.(ts|tsx)$/u.test(e.name)) out.push(join(dir, e.name));
  }
  return out;
}

test('refetchQueries 一律 cancelRefetch: false（上一次没回来就跳过，不取消在途请求）；不再每次发完把快照整份 invalidate', () => {
  const bad = [];
  for (const f of files('')) {
    const src = read(f);
    for (const m of src.matchAll(/refetchQueries\(([^;]*?)\)(?:\.then|;|\))/gsu)) {
      if (!/cancelRefetch: false|, no\b/u.test(m[0])) bad.push(`${f}: ${m[0].slice(0, 80)}`);
    }
    if (/invalidateQueries\(\{ queryKey: SNAPSHOT_KEY/u.test(src)) bad.push(`${f}: invalidate 快照`);
  }
  assert.deepEqual(bad, []);
  assert.match(read('cv/sendOp.ts'), /const no = \{ cancelRefetch: false \} as const/u);
});

test('各处自己的等法都换成了跟踪器：opDelay、设置页的密问、整理按钮的 2 秒、整理建议的硬等、开话题每秒拉快照', () => {
  assert.doesNotMatch(read('cv/opWords.ts'), /opDelay|OP_TRIES/u);
  const settings = read('cv/Settings.tsx');
  assert.doesNotMatch(settings, /fetchOutboxStatus|postOutbox|i < 4 \? 250/u);
  assert.match(settings, /sendOp\(\{ op_id: newOpId\(\), kind: 'settings'/u);
  const board = read('cv/Board.tsx');
  assert.doesNotMatch(board, /fetchOutboxStatus|refetchInterval/u);
  // 「整理」按钮 T973 从看板右栏挪到侧栏和专项一览，组件搬进 Tidy.tsx
  const tidy = read('cv/Tidy.tsx');
  assert.match(tidy, /useTrackedOp\(op\?\.id\)/u);
  assert.doesNotMatch(tidy, /setTimeout\(refetch, (1500|4000)\)/u);
  const launch = read('cv/launch.ts');
  assert.doesNotMatch(launch, /invalidateQueries/u);
  assert.match(launch, /await r\.settled/u);
});

test('点了就锁、锁到有结果：看板快速答题走跟踪器；问题卡等 POST 回来才写“已发出”；停止写“正在停…”', () => {
  const board = read('cv/Board.tsx');
  assert.doesNotMatch(board, /ans\.send\(/u);
  assert.match(board, /disabled=\{quickOp\.busy \|\| quickOp\.phase === 'done'\}/u);
  const q = read('cv/Question.tsx');
  assert.match(q, /ans\.posted \? 'sent' : 'sending'/u);
  assert.match(q, /form\.phase === 'sending'/u);
  const conv = read('cv/Conversation.tsx');
  assert.match(conv, /stopping=\{stopping\}/u);
  assert.match(read('cv/Composer.tsx'), /正在停…/u);
  assert.match(conv, /bubbleWords\(p, row, tr\)/u);
});

test('同一件事在页面上有几处按钮（停住的一轮、右栏总览、看板卡）：不管了、接着做共用一份状态，点一处处处锁住', () => {
  const rb = read('cv/RecoverBits.tsx');
  assert.match(rb, /useOutboxOp\(`recover:\$\{topicKey\}`\)/u);
  assert.match(rb, /useOutboxOp\(`dismiss:\$\{topicKey\}`\)/u);
  const so = read('cv/sendOp.ts');
  assert.match(so, /export function useOutboxOp\(shareKey\?: string\)/u);
  assert.match(so, /\(\) => !keep && !shareKey && !alive\.current/u);
  // 停止：先发请求，再写“正在停…”（点击里直接 setState 会先整页重画一遍）
  assert.match(read('cv/Conversation.tsx'), /window\.setTimeout\(\(\) => setStopOp\(/u);
});

test('交过的题等新状态时，题面照话题的发件行锁住：刷新、换页面回来、另一个窗口都不让再点一次（T837）', () => {
  // 11-26 T732：回答 18:17 交给了任务，网页 3 分半后才收到新状态；页面重开后题又能点，主人点了“不处理”，回“这题你已经答过了”
  const op = (seq, payload, state) => ({ seq, op_id: `op-${seq}`, kind: 'answer', topic_key: 't', project_key: null, payload, state, message: '', created_at: '' });
  const ans = op(257, { token: 'tk', answers: { q1: '你自己点呗' }, note: '' }, 'done');
  assert.equal(K.priorAnswer([ans], 'tk'), ans);
  assert.equal(K.priorAnswer([{ ...ans, state: 'new' }], 'tk')?.op_id, 'op-257'); // 还在等 Covey 收
  assert.equal(K.priorAnswer([{ ...ans, state: 'failed' }], 'tk'), null); // 没办成：照常能答
  assert.equal(K.priorAnswer([ans], 'other'), null); // 别的题
  assert.equal(K.priorAnswer([{ ...ans, kind: 'say' }], 'tk'), null);
  const aside = op(258, { token: 'tk', aside: true }, 'done');
  const restore = op(259, { token: 'tk', restore: true }, 'done');
  assert.equal(K.answerAct(aside), 'aside');
  assert.equal(K.priorAnswer([ans, aside], 'tk'), aside); // 按 seq 取最新的一条
  assert.equal(K.priorAnswer([restore, aside, ans], 'tk'), null); // 撤销了不处理：题又开着
  // 题面：锁不锁、那一行字不只看这个组件发过什么，也看发件行
  const q = read('cv/Question.tsx');
  assert.match(q, /const prior: OutboxView \| null = !own && token \? priorAnswer\(ops, token\) : null;/u);
  assert.match(q, /const closing: boolean = own \|\| !!prior;/u);
  assert.match(q, /act: own \? ans\.act : prior \? answerAct\(prior\) : ''/u);
});

test('记忆回执后只重取 memory:index（和旧键 memory），带 rev 的数据键不重取：rev 走 A→B→A，缓存里不残留 B 的内容', async () => {
  assert.equal(K.isMemoryPullKey(['memory']), true);
  assert.equal(K.isMemoryPullKey(['memory', 'memory:index']), true);
  assert.equal(K.isMemoryPullKey(['memory', 'memory:items', 'A']), false);
  assert.equal(K.isMemoryPullKey(['settings']), false);
  // 假 Bridge：index 说现在的 rev，数据键永远回当下的内容
  let bridge = { rev: 'A', body: 'content-A' };
  const qc = new QueryClient();
  const fetched = [];
  // 页面在看：给它们挂上观察者，使其 active
  const { QueryObserver } = await import('@tanstack/query-core');
  const watch = (key, fn, extra = {}) => { const o = new QueryObserver(qc, { queryKey: key, queryFn: fn, staleTime: Infinity, ...extra }); return o.subscribe(() => {}); };
  const pull = () => qc.refetchQueries({ type: 'active', predicate: (q) => K.isMemoryPullKey(q.queryKey) }, { cancelRefetch: false });
  let un = watch(['memory', 'memory:index'], async () => { fetched.push('index'); return { rev: bridge.rev }; }, { staleTime: 0 });
  let unA = watch(['memory', 'memory:items', 'A'], async () => { fetched.push('items@A'); return bridge.body; });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(qc.getQueryData(['memory', 'memory:items', 'A']), 'content-A');
  // 操作回执后 Bridge 变成 B；回执后的取数只取 index
  bridge = { rev: 'B', body: 'content-B' };
  fetched.length = 0;
  await pull();
  assert.deepEqual(fetched, ['index']);
  assert.equal(qc.getQueryData(['memory', 'memory:items', 'A']), 'content-A');
  // 页面按 index 的新 rev 去取 B 的键，再因撤销回到 A
  unA();
  const unB = watch(['memory', 'memory:items', 'B'], async () => { fetched.push('items@B'); return bridge.body; });
  await new Promise((r) => setTimeout(r, 20));
  bridge = { rev: 'A', body: 'content-A' };
  await pull();
  unB();
  unA = watch(['memory', 'memory:items', 'A'], async () => { fetched.push('items@A'); return bridge.body; });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(qc.getQueryData(['memory', 'memory:items', 'A']), 'content-A');
  assert.equal(qc.getQueryData(['memory', 'memory:items', 'B']), 'content-B');
  un(); unA();
  qc.clear();
});

test('sendOp 的 memory 取数用 isMemoryPullKey，不再用整个 [memory] 前缀', () => {
  const so = read('cv/sendOp.ts');
  assert.match(so, /isMemoryPullKey\(q\.queryKey\)/u);
  assert.match(so, /p\.t === 'memory'/u);
});

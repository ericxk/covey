// 网页上的几处“说法”（T647）：Covey 停推的灰字（P1-C1）、发件的一套状态（P1-C2）、在做的进度说明（P1-C4）、退役状态词不回潮（P1-B8）。
// 跑：npm run test:words
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as L from '../client/src/cv/lib.ts';
import * as O from '../client/src/cv/opWords.ts';
import * as T from '../client/src/cv/topicText.ts';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const CV = join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'cv');

/* ---------- P1-C1：Covey 停推 ---------- */
test('停推灰字：3 分钟内不说；之后“N 分钟没同步”，再久说小时、天；从没同步过是“还没同步”', () => {
  const now = Date.parse('2026-11-22T23:00:00+08:00');
  const ago = (ms) => new Date(now - ms).toISOString();
  assert.equal(L.staleWords(ago(60_000), now), '');
  assert.equal(L.staleWords(ago(180_000), now), '');
  assert.equal(L.staleWords(ago(5 * 60_000 + 20_000), now), '5 分钟没同步');
  assert.equal(L.staleWords(ago(3 * 3600_000), now), '3 小时没同步');
  assert.equal(L.staleWords(ago(4 * 86400_000), now), '4 天没同步');
  assert.equal(L.staleWords(null, now), '还没同步');
  assert.equal(L.staleWords('不是时间', now), '还没同步');
});

test('看板停推时不说“已同步”“都在掌握中”，页底时间不变橙', () => {
  const board = readFileSync(join(CV, 'Board.tsx'), 'utf8');
  assert.match(board, /staleWords\(snap\.synced_at/u);
  assert.match(board, /stale \|\| \(desktop \? '已同步' : ''\)/u);
  const css = readFileSync(join(CV, 'unify.css'), 'utf8');
  assert.match(css, /\.nb-updated\.stale\{color:var\(--t3\)\}/u);
  assert.match(css, /\.nb-synced\.stale\{color:var\(--t3\)\}/u);
});

/* ---------- P1-C2：发件的一套说法 ---------- */
test('发件状态一套说法：发送中 → 已发出等 Covey → 回执 / 没办成：原因 / 没发出去：原因；等满不算失败', () => {
  assert.equal(O.opWords('sending'), '发送中…');
  assert.equal(O.opWords('waiting'), '已发出，等 Covey 处理…');
  assert.equal(O.opWords('done', '已重新展开'), '已重新展开');
  assert.equal(O.opWords('done'), '已执行');
  assert.equal(O.opWords('failed', '这一轮已经接着做了（任务 #23），没有再开一次'), '没办成：这一轮已经接着做了（任务 #23），没有再开一次');
  assert.equal(O.opWords('unsent', 'Network Error'), '没发出去：Network Error');
  const late = O.opWords('timeout');
  assert.doesNotMatch(late, /失败|没办成|没开看板/u);
  assert.match(late, /发件箱/u);
  // 一分钟的等法（节奏在 opTrack.ts 的 pollGap，单测在 scripts/op-track.test.mjs）：发出后气泡写“✓ 已发出”，回执后写 Covey 的原话
});

test('回执不露开关名（整理专项没开时 Covey 写的 COVEY_PROJECT_TIDY）；「不管了」不再说“只记在这台设备上”', () => {
  assert.equal(O.plainReceipt('整理专项没有开（COVEY_PROJECT_TIDY）'), '整理专项没有开');
  assert.equal(O.plainReceipt('没开 (set COVEY_MEMORY=1 first)'), '没开');
  assert.equal(O.plainReceipt('设 COVEY_MEMORY=1 再试'), '设 再试');
  assert.equal(O.plainReceipt('没开看板（手上的事和 Pin），只记在这台设备上'), '没开看板（手上的事和 Pin），Covey 拿不掉它');
  assert.equal(O.opWords('failed', '整理专项没有开（COVEY_PROJECT_TIDY）'), '没办成：整理专项没有开');
});

test('话题页的动作结果行：同一套说法；换配置、恢复办好时直接写 Covey 的回执', () => {
  assert.equal(O.opLine({ kind: 'stop', payload: {}, state: 'new', message: '' }), '停止：已发出，等 Covey 处理…');
  assert.equal(O.opLine({ kind: 'forget', payload: {}, state: 'failed', message: '这条已经作废了' }), '作废记忆：没办成：这条已经作废了');
  assert.equal(O.opLine({ kind: 'recover', payload: { action: 'continue' }, state: 'done', message: '本话题任务已重新入队，任务 #22。' }), '本话题任务已重新入队，任务 #22。');
  assert.equal(O.opLine({ kind: 'answer', payload: { aside: true }, state: 'done', message: '' }), '不处理：已执行');
});

test('各处发件都走 opWords 或 sendOp，不再各写一套“没有回应”“没有成功”', () => {
  const offenders = [];
  for (const f of readdirSync(CV).filter((n) => /\.(ts|tsx)$/u.test(n))) {
    const src = strings(f).join('\n');
    if (/Covey 没有回应|没有成功|没开看板），先只记|已发出，等 Covey 执行|已提交，等 Covey 执行/u.test(src)) offenders.push(f);
  }
  assert.deepEqual(offenders, []);
});

/* ---------- P1-C4：在做的进度说明 ---------- */
test('进度说明：在等命令跑完、这步比平均慢、可能卡住多久没动静（不写死 30 分钟）', () => {
  const now = 10_000;
  assert.deepEqual(T.progressNote({ command: { since: now - 38 * 60, seconds: 37 * 60, long: true } }, now), { text: '在等一条命令跑完 · 已 38 分钟', stuck: false });
  assert.deepEqual(T.progressNote({ slow_step: { number: 3, since: now - 35 * 60, seconds: 34 * 60, usual: 6 * 60 } }, now), { text: '第 3 步已做 35 分钟，此前平均每步约 6 分钟', stuck: false });
  assert.deepEqual(T.progressNote({ slow_step: { number: 2, since: null, seconds: 70 * 60, usual: null } }, now), { text: '第 2 步已做 1 小时 10 分钟', stuck: false });
  assert.deepEqual(T.progressNote({ maybe_stuck: true, last_activity_at: now - 47 * 60 }, now), { text: '47 分钟没有动静，可能卡住了', stuck: true });
  assert.deepEqual(T.progressNote({ maybe_stuck: true }, now), { text: '可能卡住了', stuck: true });
  assert.deepEqual(T.progressNote({ phase: '跑测试' }, now), { text: '', stuck: false });
  assert.deepEqual(T.progressNote(undefined, now), { text: '', stuck: false });
  const src = strings('TopicOverview.tsx').join('\n');
  assert.doesNotMatch(src, /30 分钟没有动静|可能卡住了（30 分钟/u);
});

/* ---------- P1-B8：退役的状态词不回潮 ---------- */
// 退役的状态词只有一个来源：covey core/look.py 的 RETIRED，经 scripts/export_look.py 导出进 look.json 的 retired（v34）；
// 这里不再照抄一份（covey tests/test_look.py 核对 web 的 look.json 就是那次导出）
const LOOK = JSON.parse(readFileSync(join(CV, 'look.json'), 'utf8'));
const RETIRED = LOOK.retired ?? {};
// 网页自己说过的旧词：“这一轮失败了 / 停了”（T647 B8）、被停止了、只记在本机的“先放着”（P1-C3 并成不管了）
const WEB_RETIRED = ['这一轮失败了', '这一轮被停止了', '停了', '先放着'];
// 短词只在整句就是它、或当状态前缀（“失败：…”）时算；长词出现在哪都算
const SHORT = new Set(['完成', '失败', '中断', '停了', '先放着']);

/** 一个 .ts/.tsx 文件里给人看的字：字符串、模板串的字面部分、JSX 文本（注释不算） */
function strings(file) {
  const text = readFileSync(join(CV, file), 'utf8');
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const out = [];
  const walk = (n) => {
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) out.push(n.text);
    else if (ts.isTemplateExpression(n)) {
      out.push(n.head.text);
      for (const s of n.templateSpans) out.push(s.literal.text);
    } else if (ts.isJsxText(n)) out.push(n.text);
    // 导入路径、类名、对象 key 里的字不是给人看的
    if (ts.isImportDeclaration(n)) return;
    ts.forEachChild(n, walk);
  };
  walk(sf);
  return out.map((s) => s.trim()).filter(Boolean);
}

test('页面代码里给人看的字没有退役的状态词（covey look.RETIRED + 网页旧说法）', () => {
  const words = [...Object.values(RETIRED).flat(), ...WEB_RETIRED];
  const hits = [];
  for (const f of readdirSync(CV).filter((n) => /\.(ts|tsx)$/u.test(n) && n !== 'look.gen.ts')) {
    for (const s of strings(f)) {
      for (const w of words) {
        const bad = SHORT.has(w) ? s === w || s.startsWith(`${w}：`) || s.startsWith(`${w}:`) : s.includes(w);
        if (bad) hits.push(`${f}: “${s.slice(0, 40)}” 含 ${w}`);
      }
    }
  }
  assert.deepEqual(hits, []);
});

test('退役词来自 look.json（covey look.RETIRED 的导出），不是网页自己抄的一份', () => {
  assert.deepEqual(Object.keys(RETIRED).sort(), ['done', 'result', 'revisit', 'running', 'stopped', 'waiting']);
  assert.ok(RETIRED.stopped.includes('执行失败') && RETIRED.waiting.includes('等你回答') && RETIRED.running.includes('进行中'));
});

test('退役词检查自己认得出来：整句的“失败”、“失败：”前缀、长词出现在句中都拦', () => {
  const bad = (s, w) => (SHORT.has(w) ? s === w || s.startsWith(`${w}：`) : s.includes(w));
  assert.ok(bad('失败', '失败'));
  assert.ok(bad('失败：原因', '失败'));
  assert.ok(bad('第 3 轮执行失败', '执行失败'));
  assert.ok(bad('T12 等你回答', '等你回答'));
  // “完成标准”是 Covey 的名词（话题的完成标准），不是状态词
  assert.ok(!bad('完成标准没满足', '完成'));
});

test('“等你 · 要你定”带子的两行：一道题不写题数，几道写“共 N 题”，题面空时有兜底（话题页总览和专项页负责人对话共用，T952）', () => {
  assert.deepEqual(T.askBand({ questions: [{ question: '合进 master 吗？' }] }), { label: '等你 · 要你定', head: '合进 master 吗？' });
  assert.deepEqual(T.askBand({ questions: [{ question: '合进 master 吗？' }, { question: '补文档吗？' }, { question: '另开话题吗？' }] }),
    { label: '等你 · 要你定（共 3 题）', head: '合进 master 吗？' });
  assert.equal(T.askBand({ questions: [{ question: '' }] }).head, '在等你的回答');
});

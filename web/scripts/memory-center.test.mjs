// 记忆页（数据约定 v45，一页式，client/src/cv/memoryCenter.ts）的纯函数：拉哪些键、条目按“管哪儿”分组（全局、专项按最近活跃、话题）、
// 组内排序与小节、超额提示、搜索（含你答过的决定）、?code= 和 ?scope= 定位、待审核每条的说明、15 个动作的 payload
// （过服务端 checkMemoryEdit 的 v43 校验）、⌘K 标题秒搜、记录。数据用 tests/golden/web_memory_v43.json（v45 形状，合成数据）。
// 跑：npm run test:memcenter
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as C from '../client/src/cv/memoryCenter.ts';
import * as P from '../client/src/cv/progressData.ts';
import { checkMemoryEdit } from '../server/modules/workbench/workbench.memory-edit.ts';
import { MEMORY_EDIT_ACTIONS_V43, MEMORY_KEY_RE } from '../shared/api.interface.ts';

const golden = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'tests', 'golden', 'web_memory_v43.json'), 'utf8'));
const K = golden.keys;
const idx = K['memory:index'];
const pool = () => C.poolOf(K['memory:global'], K['memory:topics'], [K['memory:p:demo01']]);
const passes = (p) => assert.doesNotThrow(() => checkMemoryEdit(p, {}), JSON.stringify(p));

test('合成一池：三处的条目都在，话题条目补上话题 key 和 T 号，专项名和话题名能查', () => {
  const p = pool();
  assert.equal(p.entries.length, 5);
  assert.equal(p.review.length, 4);
  assert.equal(p.log.length, 2);
  assert.equal(p.projects.demo01, '演示专项');
  assert.deepEqual(p.topics.t001, { number: 12, title: '演示话题' });
  const dup = C.poolOf(K['memory:global'], null, [{ key: 'p:x1y2z3', title: 'X', entries: [K['memory:global'].entries[0]] }]);
  assert.equal(dup.entries.length, 2, '同 key 只留一条');
});

test('往上提：话题 → 专项（没有专项就全局），专项 → 全局，全局不能再提', () => {
  assert.deepEqual(C.promoteTarget({ scope: 'topic', project_key: 'demo01' }), { scope: 'project', project_key: 'demo01' });
  assert.deepEqual(C.promoteTarget({ scope: 'topic' }), { scope: 'global' });
  assert.deepEqual(C.promoteTarget({ scope: 'project', project_key: 'demo01' }), { scope: 'global' });
  assert.equal(C.promoteTarget({ scope: 'global' }), null);
});

test('编辑框：标题、正文必填，按 index.limits 数字（决定 600）', () => {
  const l = C.limitsOf(idx, 'rule');
  assert.deepEqual(l, { title: 40, text: 200, why: 200 });
  assert.equal(C.limitsOf(idx, 'decision').text, 600);
  assert.equal(C.limitsOf(null, 'decision').text, 600);
  assert.equal(C.draftProblem({ title: '', text: 'x' }, l), '要写标题');
  assert.equal(C.draftProblem({ title: 'x', text: ' ' }, l), '要写正文');
  assert.equal(C.draftProblem({ title: '字'.repeat(41), text: 'x' }, l), '标题最多 40 字');
  assert.equal(C.draftProblem({ title: 'x', text: '字'.repeat(201) }, l), '正文最多 200 字');
  assert.equal(C.draftProblem({ title: 'x', text: 'y', why: '字'.repeat(201) }, l), '原因最多 200 字');
  assert.equal(C.draftProblem({ title: 'x', text: 'y' }, l), '');
  assert.equal(C.charCount('😀测试'), 3);
});

test('消费方：有 v43 专项条目就读决定记录（kind=decision），没有就照旧读 v37 的主人答', () => {
  const entries = K['memory:p:demo01'].entries;
  const topics = [{ key: 't001', number: 12 }];
  const d = P.projectDecisions([], entries, topics, 10);
  assert.equal(d.length, 1);
  assert.equal(d[0].question, '记忆中心先上线只读页');
  assert.match(d[0].answer, /^记忆中心第一版先只做查看和审核/u);
  assert.equal(d[0].number, 12);
  assert.equal(d[0].id, 'decision:demo01:d7f8a9');
  const old = [{ id: 'p1', kind: 'decision', source: 'owner_answer', text: '主人答：先做哪个→先做网页', at: 5, origin: { topic: 't001' } }];
  assert.deepEqual(P.projectDecisions(old, null, topics, 10).map((x) => [x.question, x.answer]), [['先做哪个', '先做网页']], 'v43 没有时走 v37');
  assert.deepEqual(P.projectDecisions(old, undefined, topics, 10).length, 1, '还没取到也先画 v37');
  assert.deepEqual(P.projectDecisions(old, [], topics, 10), [], '拿到了 v43 但是空的：不再读 v37');
  const arrow = [{ ...entries[0], key: 'decision:demo01:x', text: '主人答：用哪版→B 版（推荐）', status: 'done' }];
  assert.deepEqual(P.entryDecisions(arrow, topics).map((x) => [x.question, x.answer]), [['用哪版', 'B 版']]);
  assert.deepEqual(P.entryDecisions([{ ...entries[0], status: 'void' }], topics), [], '作废的不算');
  assert.deepEqual(P.entryDecisions([{ ...entries[0], kind: 'rule' }], topics), [], '只读决定');
});

test('一轮下“带了 N 条记忆”：用到的和带入的两份清单不按下标配对，标题查 memory:index.titles，查不到只留短码', () => {
  const titles = [['R7K2', '提交前先跑测试', 'k1'], ['D3MX9', '部署走灰度', 'k2'], ['Q1W2', '旧仓库不要动', 'k3']];
  // 用到的是后半段那条：不能被配成 codes[0]
  const r = C.turnMemoryRows({ used: [{ code: 'D3MX9', title: '部署走灰度' }], codes: ['R7K2', 'D3MX9', 'Q1W2', 'ZZZZ'] }, titles);
  assert.deepEqual(r.used, [{ code: 'D3MX9', title: '部署走灰度' }]);
  assert.deepEqual(r.carried, [{ code: 'R7K2', title: '提交前先跑测试' }, { code: 'Q1W2', title: '旧仓库不要动' }, { code: 'ZZZZ', title: '' }]);
  // 旧 Bridge：used 只有标题（code 空），不渲染成链接，也不影响 codes 的列出；没有 titles 就只给短码
  const old = C.turnMemoryRows({ used: [{ code: '', title: '只有标题' }], codes: ['R7K2'] }, null);
  assert.deepEqual(old.used, [{ code: '', title: '只有标题' }]);
  assert.deepEqual(old.carried, [{ code: 'R7K2', title: '' }]);
  // used 空、codes 非空也有内容可展开
  assert.equal(C.turnMemoryRows({ used: [], codes: ['R7K2'] }, titles).carried.length, 1);
  const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'cv', 'AnswerFoot.tsx'), 'utf8');
  assert.match(src, /codes\.length > 0/u);
  assert.doesNotMatch(src, /mem\.codes\[i\]/u);
  assert.doesNotMatch(src, /\{mem\.used\.length \? \(\s*<button/u);
  // 旧格式任务记录只有 M 号：两份清单都空，不给一个点开是空的按钮（CUA C13 复验）
  const empty = C.turnMemoryRows({ used: [], codes: [] }, titles);
  assert.deepEqual([empty.used.length, empty.carried.length], [0, 0]);
  assert.match(src, /memCanOpen: boolean = !!memRows && \(memRows\.used\.length > 0 \|\| memRows\.carried\.length > 0\)/u);
  assert.match(src, /\{memCanOpen \? \(\s*<button/u);
});

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src');

test('首屏：非话题页（记忆中心、设置）晚 3 秒再预热话题，话题页和大厅马上开始', async () => {
  const W = await import('../client/src/cv/warmup.ts');
  assert.equal(W.warmupDelay('/'), 0);
  assert.equal(W.warmupDelay('/topics/abc'), 0);
  assert.equal(W.warmupDelay('/memory'), 3000);
  assert.equal(W.warmupDelay('/memory/review'), 3000);
  assert.equal(W.warmupDelay('/settings/memory'), 3000);
  assert.equal(W.warmupDelay('/memoryx'), 0);
  // 只看打开网页时的首屏路由：之后切页不重排预热
  const shell = readFileSync(join(SRC, 'cv', 'Shell.tsx'), 'utf8');
  assert.match(shell, /useState<number>\(\(\) => warmupDelay\(loc\.pathname\)\)/u);
  assert.match(shell, /useWarmup\(warmDelay\)/u);
  assert.match(readFileSync(join(SRC, 'hooks', 'use-workbench.ts'), 'utf8'), /delayMs \+ 150 \* i/u);
});


/* ---------- v45：一页式 ---------- */
const E = (code, o = {}) => ({
  key: `k:${code}`, code, kind: 'rule', scope: 'global', title: `标题 ${code}`, text: `正文 ${code}`, status: 'active', said: 'owner', by: 'owner',
  origin: { at: 100 }, rev: `rev-${code}`, ...o,
});
const PROJ = { 'aaaaaa': '演示服务', 'bbbbbb': '发布流程', 'cccccc': '数据看板', 'dddddd': '没规矩的专项' };
const synth = (entries) => ({ entries, review: [], log: [], projects: PROJ, topics: { t1: { number: 12, title: '演示话题' } } });
const P1 = (code, pk, o = {}) => E(code, { scope: 'project', project_key: pk, ...o });

test('index：认 v45 和老 Covey 的 v43，更老的、缺字段的不认', () => {
  assert.equal(idx.version, 45);
  assert.equal(C.isMemoryIndex(idx), true);
  assert.equal(C.isMemoryIndex({ ...idx, version: 43 }), true);
  assert.equal(C.isMemoryIndex({ ...idx, version: 37 }), false);
  assert.equal(C.isMemoryIndex({ on: true, groups: [] }), false);
  assert.equal(C.isMemoryIndex(null), false);
});

test('拉哪些键：全局、话题、有记忆的专项；没有记忆的专项、index 自己、没有 rev 的、v45 前的预览和导入页都不拉', () => {
  assert.deepEqual(C.keysToLoad(idx).map((k) => k.key), ['memory:global', 'memory:p:demo01', 'memory:topics']);
  assert.equal(C.keysToLoad(idx).find((k) => k.key === 'memory:global').rev, 'gv1');
  const revs = { ...idx.revs, 'memory:previews': 'x', 'memory:import:0': 'y', 'memory:p:empty1': 'z', 'memory:p:newone': 'n' };
  const scopes = [...idx.scopes, { key: 'p:empty1', title: '空', counts: { rule: 0, decision: 0, note: 0, pending: 0 }, rev: 'z' }];
  assert.deepEqual(C.keysToLoad({ revs, scopes }).map((k) => k.key), ['memory:global', 'memory:p:demo01', 'memory:p:newone', 'memory:topics'], 'index 里没有摘要的专项照拉');
  assert.deepEqual(C.keysToLoad(null), []);
  assert.deepEqual(C.keysToLoad({ revs: { 'memory:global': '' } }), []);
  // v45 起不再推这两种键，但正则仍认（否则删不掉库里的旧行）
  assert.ok(MEMORY_KEY_RE.test('memory:previews') && MEMORY_KEY_RE.test('memory:import:3'));
});

test('分组：全局、专项（最近活跃在前）、话题；没有在用规矩的专项不出现；决定不在分组里', () => {
  const entries = [
    E('R1'), E('R2', { said: 'summary', by: 'learn' }), E('N1', { kind: 'note', said: 'summary', by: 'learn' }),
    P1('R3', 'aaaaaa'), P1('R4', 'aaaaaa', { said: 'summary', by: 'lead' }), P1('R5', 'bbbbbb'), P1('N2', 'cccccc', { kind: 'note' }),
    P1('D1', 'aaaaaa', { kind: 'decision' }), P1('R6', 'dddddd', { status: 'archived' }),
    E('R7', { scope: 'topic', topic_key: 't1', topic_number: 12 }),
  ];
  const p = synth(entries);
  // 没有 updated：按 index 顺序
  assert.deepEqual(C.buildGroups(p, ['aaaaaa', 'bbbbbb', 'cccccc', 'dddddd']).map((g) => g.key), ['global', 'p:aaaaaa', 'p:bbbbbb', 'topics']);
  // 有 updated：新的在前，没有的排在后面
  assert.deepEqual(C.buildGroups(p, ['aaaaaa', 'bbbbbb'], { bbbbbb: 50 }).map((g) => g.key), ['global', 'p:bbbbbb', 'p:aaaaaa', 'topics']);
  const gs = C.buildGroups(p, ['aaaaaa', 'bbbbbb'], { aaaaaa: 9, bbbbbb: 50 });
  const all = gs.flatMap((g) => [...g.rules, ...g.sections.flatMap((s) => s.rows)]);
  assert.equal(all.some((e) => e.kind === 'decision'), false, '决定不在分组里');
  assert.deepEqual(C.defaultOpen(gs), new Set(['global', 'p:bbbbbb', 'p:aaaaaa']));
  const many = C.buildGroups(synth([P1('A', 'aaaaaa'), P1('B', 'bbbbbb'), P1('C', 'cccccc'), E('T', { scope: 'topic', topic_key: 't1' })]), ['aaaaaa', 'bbbbbb', 'cccccc']);
  assert.deepEqual([...C.defaultOpen(many)], ['p:aaaaaa', 'p:bbbbbb'], '只展开最近活跃的两个专项，话题默认折叠');
  assert.deepEqual(C.updatedOf([{ key: 'a', updated_at: 5 }, { key: 'b' }, { key: 'c', updated_at: 0 }]), { a: 5 });
});

test('组里：你定的规矩直接列，其余收进折叠小节；组标题带计数；资料和已收起各一节', () => {
  const entries = [
    E('R1', { origin: { at: 10 } }), E('R2', { origin: { at: 30 } }), E('R3', { said: 'summary', by: 'learn' }),
    E('N1', { kind: 'note', said: 'summary', by: 'learn' }), E('N2', { kind: 'note', status: 'archived' }), E('R9', { status: 'void' }),
    P1('P1', 'aaaaaa'), P1('P2', 'aaaaaa', { said: 'summary', by: 'lead' }), P1('P3', 'aaaaaa', { said: 'summary', by: 'lead' }),
    P1('PN', 'aaaaaa', { kind: 'note' }), P1('PS', 'aaaaaa', { status: 'archived' }), P1('PD', 'aaaaaa', { kind: 'decision' }),
  ];
  const [g, pj] = C.buildGroups(synth(entries), ['aaaaaa']);
  assert.equal(g.title, '全局 · 3 条 · 你的每一轮都带');
  assert.deepEqual(g.rules.map((e) => e.code), ['R2', 'R1'], '你定的直接列，新的在前');
  assert.deepEqual(g.sections.map((s) => s.title), ['自动学的 1 条', '资料 1 条（相关时才带）', '已收起 2 条']);
  assert.equal(g.sections[2].shelf, true);
  assert.deepEqual(g.sections[2].rows.map((e) => e.code).sort(), ['N2', 'R9'], '已收起 = 归档和作废的规矩、资料');
  assert.equal(pj.title, '专项「演示服务」 · 你定的 1 条 · 负责人记的 2 条');
  assert.deepEqual(pj.sections.map((s) => s.title), ['负责人记的 2 条 · 只在这个专项照做', '资料 1 条（相关时才带）', '已收起 1 条']);
  assert.deepEqual([pj.owner, pj.others, pj.total], [1, 2, 3]);
  assert.equal(C.groupTitle({ kind: 'topics', name: '话题', total: 2, owner: 2, others: 0 }), '话题 · 2 条');
  assert.equal(C.groupTitle({ kind: 'project', name: 'X', total: 2, owner: 0, others: 2 }), '专项「X」 · 负责人记的 2 条', '0 条的不写');
  // 行尾小标签：小节标题说过谁的就不再写；话题组里写 T 号
  const pool = synth([]);
  assert.deepEqual(C.rowTags(entries[0], { said: true, pool }), ['你定的']);
  assert.deepEqual(C.rowTags(entries[2], { said: false, pool }), []);
  assert.deepEqual(C.rowTags(E('T', { scope: 'topic', topic_key: 't1' }), { said: true, pool }), ['你定的', 'T12']);
});

test('超额：不画配额条，组标题后写“N 条只带标题”（只数在用规矩的 over_cap），0 不写', () => {
  const [g] = C.buildGroups(synth([E('R1'), E('R2', { over_cap: true }), E('R3', { over_cap: true }), E('R4', { over_cap: true, status: 'archived' })]));
  assert.equal(g.over, 2);
  assert.equal(C.overLine(g.over), '2 条只带标题');
  assert.equal(C.overLine(0), '');
  assert.equal(C.overLine(C.buildGroups(synth([E('R1')]))[0].over), '');
});

test('搜索：规矩、资料写它属于哪一组；决定写成“上次你答过：…（T12 · 11-26）”；再用 index.titles 补没取到的', () => {
  const entries = [
    E('R1', { title: '提交前先跑测试' }), P1('R2', 'aaaaaa', { title: '生产变更要确认', text: '测试环境除外' }),
    E('N1', { kind: 'note', title: '端口', text: '服务在 8080 端口', status: 'archived' }),
    E('R3', { scope: 'topic', topic_key: 't1', topic_number: 12, title: '话题只读' }),
    P1('D1', 'aaaaaa', { kind: 'decision', title: '先上线只读页', text: '第一版只做查看和审核', origin: { at: 1795000000, topic_number: 12 } }),
    P1('D2', 'aaaaaa', { kind: 'decision', title: '作废的测试决定', status: 'void' }),
  ];
  const pool = synth(entries);
  const titles = [['R1', '提交前先跑测试', 'k:R1'], ['RFAR1', '没取到的测试规矩', 'global:far'], ['NFAR2', '别的资料', 'global:far2']];
  const hits = C.searchMemory(pool, titles, '测试');
  assert.deepEqual(hits.map((h) => h.code), ['R1', 'R2', 'RFAR1'], '标题命中在前，正文命中其次，再补标题表里没取到的；作废的决定不搜');
  assert.equal(hits[0].line, '规矩 · 全局');
  assert.equal(hits[1].line, '规矩 · 专项「演示服务」');
  assert.equal(hits[2].entry, undefined);
  assert.equal(hits[2].line, '规矩');
  assert.equal(C.searchMemory(pool, titles, '8080')[0].line, '资料 · 全局 · 已收起', '已收起的也能搜到');
  assert.equal(C.searchMemory(pool, titles, '话题只读')[0].line, '规矩 · 话题 T12');
  const d = C.searchMemory(pool, titles, '只读页')[0];
  assert.equal(d.kind, 'decision');
  assert.match(d.line, /^上次你答过：第一版只做查看和审核（T12 · \d\d-\d\d）$/u);
  assert.equal(C.decisionLine({ text: '', title: '只有标题', origin: { at: 0 } }), '上次你答过：只有标题', '没有正文用标题，没有 T 号和日期不写括号');
  assert.equal(C.decisionLine({ text: '字'.repeat(80), title: 't', origin: { at: 0 }, topic_number: 7 }), `上次你答过：${'字'.repeat(60)}…（T7）`);
  assert.deepEqual(C.searchMemory(pool, titles, '  '), []);
  assert.deepEqual(C.searchMemory(pool, null, '不存在的词'), []);
  assert.equal(C.kindOfCode('DB2H5W'), 'decision');
  assert.equal(C.kindOfCode('nt6b3y'), 'note');
  assert.equal(C.decisionHit(pool, 'd1').entry.code, 'D1');
  assert.equal(C.decisionHit(pool, 'R1'), null);
});

test('链接：?code= 展开它所在的组和小节，决定当作搜索结果；?scope= 认三种范围；旧路径 /memory/<页签> 照样是这一页', () => {
  const entries = [E('R1'), P1('P1', 'aaaaaa'), P1('P2', 'aaaaaa', { said: 'summary', by: 'lead' }), P1('PS', 'aaaaaa', { status: 'archived' }), P1('D1', 'aaaaaa', { kind: 'decision' })];
  const pool = synth(entries);
  const groups = C.buildGroups(pool, ['aaaaaa']);
  const loc = (c) => C.locate(pool, groups, c);
  assert.deepEqual([loc('r1').group, loc('r1').section], ['global', ''], '你定的直接列：不用展开小节');
  assert.deepEqual([loc('P2').group, loc('P2').section], ['p:aaaaaa', 'others']);
  assert.deepEqual([loc('PS').group, loc('PS').section], ['p:aaaaaa', 'shelved']);
  assert.equal(loc('D1').decision, true);
  assert.equal(loc('NOPE'), null);
  assert.equal(loc(''), null);
  const q = (s) => new URLSearchParams(s);
  assert.deepEqual(C.parseLink(undefined, q('code=RK7M2Q&scope=p:abc123')), { code: 'RK7M2Q', scope: 'p:abc123', review: false, log: false });
  for (const s of ['global', 'topics', 'p:demo01']) assert.equal(C.parseLink(undefined, q(`scope=${s}`)).scope, s);
  assert.equal(C.parseLink(undefined, q('scope=all')).scope, '', '不认的范围当没有');
  assert.equal(C.parseLink(undefined, q('scope=p:')).scope, '');
  for (const tab of ['rules', 'decisions', 'notes', 'whatever']) assert.deepEqual(C.parseLink(tab, q('')), { code: '', scope: '', review: false, log: false }, tab);
  assert.equal(C.parseLink('review', q('')).review, true, '/memory/review 滚到待审核块');
  assert.equal(C.parseLink('log', q('')).log, true);
  assert.equal(C.parseLink(undefined, q('view=log')).log, true, '?view=log 记住在看记录');
  assert.equal(C.codeHref('RK7M2Q'), '/memory?code=RK7M2Q');
});

test('待审核：迁移项按专项折成一行汇总，其余逐条；每条一行灰字写是哪一种、采纳后会怎样；标题是正文开头就不重复', () => {
  const mk = (over) => ({ key: 'r', type: 'migrate', title: '', text: '', suggest: { kind: 'rule', scope: 'project', project_key: 'demo01' }, created_at: 1, ...over });
  const rows = [mk({ key: 'a', text: '长文 A' }), mk({ key: 'b', text: '长文 B' }), mk({ key: 'c', suggest: { kind: 'rule', scope: 'global' } }), mk({ key: 'd', type: 'learn', created_at: 5 }), mk({ key: 'e', type: 'learn', suggest: { kind: 'rule', scope: 'project', project_key: 'demo01', grant: 'deploy' }, created_at: 9 })];
  const { todo, groups } = C.reviewSplit(rows, { projects: { demo01: '推理服务快速部署' } });
  assert.deepEqual(todo.map((r) => r.key), ['e', 'd'], '新的在前');
  assert.deepEqual(groups.map((g) => [g.label, g.rows.length]), [['推理服务快速部署', 2], ['全局', 1]]);
  assert.equal(C.groupLine(groups[0]), '推理服务快速部署：2 条负责人约束在按规矩带');
  assert.equal(C.titleShown({ title: '提交前先跑测试', text: '提交前先跑测试，再推送' }), false);
  assert.equal(C.titleShown({ title: '提交前先跑测试…', text: '提交前先跑测试，再推送' }), false);
  assert.equal(C.titleShown({ title: '测试规矩', text: '提交前先跑测试' }), true);
  assert.equal(C.titleShown({ title: '只有标题', text: '' }), true);
  const pool = { projects: { demo01: '演示专项' } };
  const [learn, conflict, wider, grant] = K['memory:global'].review;
  // 待审核只剩两种（推断全局、放行类）和一次性的回滚冲突、旧版迁移；「不要」都是保持原样（T908 M3b）
  assert.equal(C.reviewLine(wider, pool), '推断要管全局 · 现在先在说的地方照做；采纳就挪到全局，不要就留在原处');
  assert.equal(C.reviewLine(wider, pool), C.WIDER_WORDS);
  assert.equal(C.reviewLine(grant, pool), C.GRANT_WORDS);
  assert.equal(C.reviewLine(learn, pool), '推断要管全局 · 还没生效；采纳后每一轮都照做，不要就不记');
  assert.equal(C.reviewLine(mk({ type: 'learn', suggest: { kind: 'rule', scope: 'project', project_key: 'demo01' } }), pool), '自动学到的 · 还没生效；采纳后在这里照做，不要就不记');
  assert.equal(C.reviewLine(mk({ type: 'migrate', suggest: { kind: 'rule', scope: 'project', project_key: 'demo01' } }), pool), '迁移带过来的负责人约束 · 采纳算你定的，不要就照旧是负责人记的');
  assert.equal(C.reviewLine(conflict, pool), '和已有的一条说法不同 · 采纳换成新的说法，不要就留原来的');
  for (const gone of ['lead', 'tidy', 'class3', 'candidate', 'import']) assert.equal(C.REVIEW_WORDS[gone], undefined, gone);
  assert.equal(C.reviewLine(mk({ type: 'nope' }), pool), C.REVIEW_OTHER);
  for (const r of [learn, conflict, wider, grant, ...rows]) assert.doesNotMatch(C.reviewLine(r, pool), /undefined|导入|体检|预览|改成资料|只关掉这一项/u, r.key);
  assert.equal(C.todoPending({ pending: 93, migrate: 90 }), 3, '角标口径不变：不含迁移项');
  assert.equal(C.todoPending({ pending: 90, migrate: 90 }), 0);
  assert.equal(C.todoPending({ pending: 5 }), 5);
  assert.equal(C.todoPending(undefined), 0);
  assert.match(readFileSync(join(SRC, 'cv', 'Shell.tsx'), 'utf8'), /todoPending\(mem\.index\?\.counts\)/u);
});

test('15 个动作：payload 都带 v: 43，都过服务端校验；动一条的带 base = 条目 rev；没有 import、ignore', () => {
  const p = pool();
  const rule = p.entries.find((e) => e.code === 'RK7M2Q');
  const topicNote = p.entries.find((e) => e.code === 'NT6B3Y');
  const dec = p.entries.find((e) => e.code === 'DB2H5W');
  const [learn, conflict] = p.review;
  const made = [
    C.addOp({ title: '提交前先跑测试', text: '提交前先跑相关测试。', kind: 'rule', scope: 'global' }),
    C.editOp(rule, { title: rule.title, text: '改过的正文', why: '' }),
    C.moveOp(rule, { scope: 'project', project_key: 'demo01' }),
    C.archiveOp(rule),
    C.restoreOp(rule),
    C.voidOp(dec, '决定已作废'),
    C.acceptOp(learn),
    C.rejectOp(learn),
    { ...C.op('accept_all', []) },
    { ...C.op('reject_all', []) },
    C.keepOp('keep_left', conflict),
    C.keepOp('keep_right', conflict),
    C.keepOp('keep_both', conflict),
    C.promoteOp(topicNote, C.promoteTarget(topicNote, 'demo01')),
    C.undoOp('u-0001'),
  ];
  assert.deepEqual(made.map((x) => x.action).sort(), [...MEMORY_EDIT_ACTIONS_V43].sort());
  for (const x of made) {
    assert.equal(x.v, 43);
    assert.ok(Array.isArray(x.keys));
    passes(x);
  }
  assert.equal('importOp' in C || 'ignoreOp' in C || 'allOp' in C, false);
  assert.equal(made[1].base, 'rrk7m2q');
  assert.equal(made[1].kind, 'rule', '改的时候带 kind，服务端按它数正文上限');
  assert.equal(made[2].base, 'rrk7m2q');
  assert.equal(made[3].base, 'rrk7m2q');
  assert.deepEqual(made[10].keys, ['global:g4d5e6', 'review:conflict:b2'], '左边是已有的，右边是新来的');
  assert.deepEqual(made[13].keys, ['topic:t001:n4e5f6']);
  assert.equal(made[13].project_key, 'demo01');
  assert.equal('why' in made[1], false, '空的不带');
  // 换管哪儿：话题要带 topic_key
  assert.deepEqual(C.moveOp(rule, { scope: 'topic', topic_key: 't001' }), { v: 43, action: 'move', keys: ['global:g1a2b3'], scope: 'topic', topic_key: 't001', base: 'rrk7m2q' });
  passes(C.moveOp(rule, { scope: 'topic', topic_key: 't001' }));
  // 决定正文能到 600 字
  passes(C.editOp(dec, { title: dec.title, text: '字'.repeat(600) }));
  assert.throws(() => checkMemoryEdit(C.editOp(rule, { title: rule.title, text: '字'.repeat(201) }), {}), /200/u);
  const acc = C.acceptOp(learn, { title: '回复先说结论再说原因', kind: 'rule', scope: 'project', project_key: 'demo01' });
  assert.equal(acc.title, '回复先说结论再说原因');
  assert.equal(C.acceptOp(learn, { title: learn.title }).title, undefined, '没改的不带');
  passes(acc);
});

test('⌘K：标题表秒搜，短码完全一样的最前，不分大小写，最多 n 条', () => {
  assert.deepEqual(C.searchTitles(idx.titles, '测试').map((h) => h.code), ['RK7M2Q']);
  assert.deepEqual(C.searchTitles(idx.titles, 'nq3x9a').map((h) => h.code), ['NQ3X9A']);
  assert.equal(C.searchTitles(idx.titles, '').length, 0);
  assert.equal(C.searchTitles(null, 'x').length, 0);
  const many = Array.from({ length: 30 }, (_, i) => [`R${i}`, `规矩 ${i}`, `global:g${i}`]);
  assert.equal(C.searchTitles(many, '规矩', 8).length, 8);
  assert.deepEqual(C.searchTitles([['RAB', 'x', 'k1'], ['RA', 'y', 'k2']], 'ra').map((h) => h.code), ['RA', 'RAB'], '完全一样的在前');
  const src = readFileSync(join(SRC, 'cv', 'Search.tsx'), 'utf8');
  assert.match(src, /KIND_WORD\[kindOfCode\(m\.code\)\]/u);
});

test('记录：谁、做了什么、因为哪句话；Bridge 写进变更记录的动作都有中文词', () => {
  const [a, b] = K['memory:global'].log;
  assert.deepEqual(C.logLine(a), { who: '你', action: '采纳', title: '提交前先跑测试', because: 'T12 “提交之前先跑测试”' });
  assert.equal(C.logLine(b).because, '');
  for (const act of MEMORY_EDIT_ACTIONS_V43) assert.ok(C.ACTION_WORD[act], act);
  assert.ok(C.ACTION_WORD.import && C.ACTION_WORD.ignore, '旧的变更记录里还有导入、留在原工具');
  const bridge = ['add', 'edit', 'archive', 'restore', 'void', 'settle', 'supersede', 'kind', 'confirm', 'move', 'undo', 'accept', 'reject', 'keep_left', 'keep_right', 'keep_both'];
  const core = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'covey');
  for (const f of ['core/memstore.py', 'core/memory_review.py', 'app/web_memory.py', 'app/memory_tidy.py']) {
    let src = '';
    try { src = readFileSync(join(core, f), 'utf8'); } catch { continue; }
    for (const m of src.matchAll(/\baction="([a-z_]+)"/gu)) bridge.push(m[1]);
  }
  for (const act of new Set(bridge)) {
    assert.ok(C.ACTION_WORD[act], `动作 ${act} 缺中文词`);
    assert.doesNotMatch(C.ACTION_WORD[act], /^[a-z_]+$/u, act);
  }
  assert.equal(C.logLine({ at: 1, who: 'owner', action: 'confirm', refs: [], title: '同事机器' }).action, '确认了');
});

test('右栏精简：一行“这个专项的规矩 N 条”，下面至多 n 条规矩，你定的在前；不看决定、资料', () => {
  const entries = [
    P1('A', 'aaaaaa', { said: 'summary', by: 'lead', origin: { at: 900 } }), P1('B', 'aaaaaa', { origin: { at: 100 } }), P1('C', 'aaaaaa', { origin: { at: 200 } }),
    P1('N', 'aaaaaa', { kind: 'note' }), P1('D', 'aaaaaa', { kind: 'decision', in_progress: true }), P1('X', 'aaaaaa', { status: 'archived' }), P1('O', 'aaaaaa', { over_cap: true, origin: { at: 50 } }),
  ];
  const b = C.briefOf(entries, 3);
  assert.deepEqual(b.rules.map((e) => e.code), ['C', 'B', 'O'], '你定的在前，新的在前');
  assert.equal(b.total, 4, '在用规矩：A B C O');
  assert.equal(C.briefOf(entries, 6).rules.at(-1).code, 'A', '负责人记的排在你定的后面');
  assert.equal(C.briefLine(b, 'p:aaaaaa'), '这个专项的规矩 4 条');
  assert.equal(C.briefLine(C.briefOf([]), 'topics'), '这个话题的规矩 0 条');
  const list = readFileSync(join(SRC, 'cv', 'MemoryList.tsx'), 'utf8');
  assert.doesNotMatch(list, /在办的决定|carried_next|资料 /u);
  assert.match(list, /只带标题/u);
  assert.match(list, /\/memory\?scope=/u);
});

test('一页式：页面没有页签、范围下拉、体检、配额条、预览、导入的残留；有待审核块、记录链接、搜索、记一条', () => {
  const src = readFileSync(join(SRC, 'cv', 'MemoryCenter.tsx'), 'utf8');
  for (const gone of ['TABS', 'mc-tabs', 'Health', 'Rooms', 'PreviewDrawer', 'ImportDrawer', '全部采纳', '全部不要', '下一轮带什么', 'carried_next', 'memory:previews', 'memory:import']) assert.doesNotMatch(src, new RegExp(gone, 'u'), gone);
  for (const have of ['memory-review', 'memory-log-link', 'memory-new', '搜索记忆', 'id="mc-review"']) assert.ok(src.includes(have), have);
  // 记一条只有规矩、资料
  assert.match(src, /\(\['rule', 'note'\] as MemoryKind\[\]\)/u);
  // 撤销失败有页面提示
  assert.match(src, /id\.startsWith\('undo:'\)\) toast\.error/u);
  const turn = readFileSync(join(SRC, 'cv', 'turnBits.tsx'), 'utf8');
  assert.match(turn, /superseded: '收起了'/u);
  assert.match(turn, /pending: '待审核'/u);
  assert.match(turn, /n\.kind === 'remembered' \|\| n\.kind === 'superseded' \|\| n\.kind === 'pending'/u);
});

test('手机（≤640px）：按钮和点按区不小于 40px，不写死颜色，不放横向滚动', () => {
  const css = readFileSync(join(SRC, 'cv', 'memorycenter.css'), 'utf8');
  const phone = css.slice(css.indexOf('@media (max-width:640px)'));
  for (const sel of ['.cv .mc-btn{height:40px', '.cv .mc-act,.cv .mc-ic{min-height:40px;min-width:40px}', '.cv .mc-link{min-height:40px;min-width:40px']) assert.ok(phone.includes(sel), sel);
  assert.doesNotMatch(css, /overflow-x:\s*(auto|scroll)/u);
  assert.match(css, /\.cv \.mc-gh\{[^}]*min-height:40px/u);
});

test('v45：侧栏和记忆页认 version 43 及以后的 memory:index（只认 43 时 v45 的 Covey 会退回旧的设置页「记忆」）', () => {
  const hook = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'hooks', 'use-workbench.ts'), 'utf8');
  assert.doesNotMatch(hook, /version\s*===\s*43/u);
  assert.match(hook, /v >= 43/u);
  assert.equal(golden.keys['memory:index'].version, 45);
  assert.ok(C.isMemoryIndex(golden.keys['memory:index']));
});

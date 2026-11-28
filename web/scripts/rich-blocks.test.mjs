// T943 富文本块：网页的切块器（client/src/cv/richBlocks.ts）和 Bridge 的 covey/core/rich_md.py 读同一份样例
// （covey tests/fixtures/rich/cases.json），切出来的块要一模一样；两边任何一边改了规则，这里或 Python 的 test_rich_md 就会红
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const src = readFileSync(new URL('../client/src/cv/richBlocks.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText;
const { richBlocks, shape, hasRich, metricItems } = await import(`data:text/javascript,${encodeURIComponent(js)}`);
const cases = JSON.parse(readFileSync(new URL('../../tests/fixtures/rich/cases.json', import.meta.url), 'utf8'));

test('样例够多', () => {
  assert.ok(cases.length >= 20);
});
for (const c of cases) {
  test(`和 Python 切得一样：${c.name}`, () => {
    assert.deepEqual(richBlocks(c.text).map(shape), c.blocks);
  });
}
test('没有块的回答一眼认出来', () => {
  assert.equal(hasRich('普通回答，`code` 和 [链接](https://x)'), false);
  assert.equal(hasRich('> [!NOTE]\n> 有'), true);
  assert.equal(hasRich('写到 metrics 这个词不算'), false);
});
test('指标的两种写法，表头行不算', () => {
  assert.deepEqual(metricItems('标签 | 数值 | 变化\n|---|---|---|\na | 1 | +2\nb：3'), [['a', '1', '+2'], ['b', '3', '']]);
});

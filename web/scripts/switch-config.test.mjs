// 话题页换配置（T616，Covey v20）：初值、有没有变化、网页上换不了的情况。跑：npm run test:switch
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as S from '../client/src/cv/switchConfig.ts';

const m = (key, extra = {}) => ({
  key, harness: key.split(':')[0], harness_title: key.split(':')[0], provider: '', provider_title: '自带', label: key,
  budgets: [{ value: 'high', text: '高' }, { value: 'low', text: '低' }], default_budget: 'high', start_budget: 'high',
  work_modes: [], ...extra,
});
const D = {
  models: [m('claude:opus'), m('codex:gpt', { work_modes: ['auto', 'fast'], start_budget: 'low' })],
  workspaces: [{ name: 'covey', device: '', harnesses: [] }, { name: 'ws-root', device: '', harnesses: [] }],
  run_modes: [{ value: 'direct', text: '原目录' }, { value: 'worktree', text: '独立 worktree' }],
  preselect: { model: 'claude:opus', effort: 'high', work_mode: '', workspace: 'covey', run_mode: 'direct' },
};
const C = { model: 'claude:opus', effort: 'high', work_mode: '', workspace: 'covey', run_mode: 'direct' };

test('初值补成模型能选的', () => {
  assert.deepEqual(S.fromConfig(D, C), C);
  assert.equal(S.fromConfig(D, { ...C, effort: 'max' }).effort, 'high');
  const g = S.fromConfig(D, { ...C, model: 'codex:gpt', effort: 'zzz' });
  assert.deepEqual([g.effort, g.work_mode], ['low', 'auto']);
  assert.equal(S.fromConfig(D, { ...C, model: 'gone:x' }).model, 'gone:x');
});

test('没动过不算变化，只补齐也不算', () => {
  const base = S.fromConfig(D, { ...C, effort: 'max' });
  assert.deepEqual(S.changedFields(base, { ...base }), []);
  assert.deepEqual(S.changedFields(base, { ...base, effort: 'low', workspace: 'ws-root' }), ['effort', 'workspace']);
});

test('换不了的情况', () => {
  const base = S.fromConfig(D, C);
  assert.equal(S.switchBlock(D, base, { ...base, effort: 'low' }, false), '');
  assert.equal(S.switchBlock(D, base, { ...base, effort: 'low' }, true), '在做时不能换，先停止');
  assert.match(S.switchBlock(D, base, { ...base, run_mode: 'worktree' }, false), /飞书的换配置卡/u);
  const wt = { ...base, run_mode: 'worktree' };
  assert.equal(S.switchBlock(D, wt, { ...wt, effort: 'low' }, false), '');
  assert.match(S.switchBlock(D, wt, { ...wt, workspace: 'ws-root' }, false), /飞书的换配置卡/u);
  assert.equal(S.switchBlock(D, wt, { ...wt, run_mode: 'direct' }, false), '');
  const gone = { ...base, model: 'gone:x' };
  assert.match(S.switchBlock(D, gone, gone, false), /重选模型/u);
});

// v36：专项负责人的换配置（同飞书专项卡上的「换配置」）：只列能当负责人的模型（bridge_tools），不查 worktree，在做时等它做完
const L = {
  ...D,
  models: [m('claude:opus', { bridge_tools: true }), m('codex:gpt', { bridge_tools: true }), m('pi:gpt')],
};

test('负责人的目录只留能当负责人的模型', () => {
  assert.deepEqual(S.leadData(L).models.map((x) => x.key), ['claude:opus', 'codex:gpt']);
  assert.deepEqual(S.leadData(D).models, []);
  // 别的字段原样（面板的工作区、运行方式等照旧从它取）
  assert.equal(S.leadData(L).workspaces, L.workspaces);
  assert.equal(L.models.length, 3);
});

test('负责人换不了的情况', () => {
  const base = S.fromConfig(L, C);
  assert.equal(S.switchBlock(L, base, { ...base, effort: 'low' }, false, true), '');
  assert.equal(S.switchBlock(L, base, { ...base, model: 'codex:gpt' }, false, true), '');
  assert.equal(S.switchBlock(L, base, { ...base, effort: 'low' }, true, true), '负责人在做时不能换，等它做完');
  assert.equal(S.switchBlock(L, base, { ...base, model: 'pi:gpt' }, false, true), S.LEAD_ONLY);
  // 负责人现在的模型不能当负责人（Harness 换过能力）：没改也说清为什么
  const pi = S.fromConfig(L, { ...C, model: 'pi:gpt' });
  assert.equal(S.switchBlock(L, pi, pi, false, true), S.LEAD_ONLY);
  assert.match(S.switchBlock(L, base, { ...base, model: 'gone:x' }, false, true), /重选模型/u);
  // 工作区、运行方式面板上不画，不查 worktree（Covey 那边拒绝换）
  const wt = { ...base, run_mode: 'worktree' };
  assert.equal(S.switchBlock(L, wt, { ...wt, effort: 'low' }, false, true), '');
  // 普通话题照旧：不看 bridge_tools
  assert.equal(S.switchBlock(L, base, { ...base, model: 'pi:gpt' }, false), '');
  assert.equal(S.switchBlock(L, base, { ...base, model: 'pi:gpt' }, false, false), '');
});

// T789：换配置面板按输入框的框固定定位（对话区 overflow:hidden，放大编辑时 absolute 的面板上半截被裁掉）
const P = await import('../client/src/cv/popPlace.ts');
test('面板贴着输入框上沿往上、和输入框一样宽，最多高到视口顶', () => {
  // 电脑 1440×900：输入框上沿 794，左 278 宽 800
  assert.deepEqual(P.popPlace({ top: 794, left: 278, width: 800 }, 900), { left: 278, width: 800, bottom: 114, room: 778 });
  // 放大编辑：输入框上沿 270（对话区里上面只剩 50 像素），面板照样能有 254 像素高，不靠对话区给地方
  assert.deepEqual(P.popPlace({ top: 270, left: 290, width: 800 }, 900), { left: 290, width: 800, bottom: 638, room: 254 });
  // 手机的配置栏一行，间隔 6
  assert.deepEqual(P.popPlace({ top: 690.4, left: 10, width: 370 }, 844, 6), { left: 10, width: 370, bottom: 160, room: 676 });
});

test('输入框跑出视口时面板不跟着跑出去：上沿按视口夹住，地方不会是负的', () => {
  assert.equal(P.popPlace({ top: -40, left: 0, width: 300 }, 800).room, 0);
  assert.equal(P.popPlace({ top: -40, left: 0, width: 300 }, 800).bottom, 808);
  assert.equal(P.popPlace({ top: 1200, left: 0, width: 300 }, 800).bottom, 8);
});

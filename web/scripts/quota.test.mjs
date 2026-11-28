// 数据约定 v42（covey docs/web.md 账号与额度）：Covey 推的设置条目 quota 怎么理、表里每一格写什么和什么颜色、折起来的来源一行写什么、
// 侧栏胶囊什么时候出现，发件 quota_refresh 的校验。跑：npm run test:quota
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as Q from '../client/src/cv/quota.ts';
import * as K from '../client/src/cv/opTrack.ts';
import * as S from '../server/modules/workbench/workbench.quota.ts';
import { OUTBOX_KINDS, SETTINGS_KEYS, isSettingsKey } from '../shared/api.interface.ts';

const CV = join(dirname(fileURLToPath(import.meta.url)), '..', 'client', 'src', 'cv');
const read = (name) => readFileSync(join(CV, name), 'utf8');
// 2026-11-26 14:32（上海）
const NOW = Date.parse('2026-11-26T14:32:00+08:00') / 1000;
const at = (h, m = 0, days = 0) => Date.parse(`2026-11-26T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00+08:00`) / 1000 + days * 86400;

const win = (kind, left, over = {}) => ({ kind, label: kind === '5h' ? '5 小时' : '周', left, resets_at: at(19, 5), checked_at: NOW - 60, ...over });
const pool = (windows, over = {}) => ({ key: '', label: '', active: false, state: 'ok', available_at: null, windows, ...over });
const acct = (name, pools, over = {}) => ({ name, state: 'ok', available_at: null, pools, ...over });
const source = (over = {}) => ({
  id: 'gw', kind: 'gateway', title: 'Claude 网关', collected_at: NOW - 30, ttl: 600, stale_after: 600, read_at: NOW - 30, failing: false,
  usable: 7, total: 8, next_available_at: null, low: false, dry: false, expand: false, relogin: [], accounts: [], ...over,
});

test('新操作和新的设置条目网页认得：发件 quota_refresh，settings 的 quota', () => {
  assert.ok(OUTBOX_KINDS.includes('quota_refresh'));
  assert.ok(SETTINGS_KEYS.includes('quota'));
  assert.equal(isSettingsKey('quota'), true);
});

test('发件 quota_refresh：不带话题和专项，payload 是空的', () => {
  assert.deepEqual(S.quotaRefreshShape({}, '', ''), { ok: true });
  assert.match(S.quotaRefreshShape({}, 't1', '').error, /不带 topic_key/u);
  assert.match(S.quotaRefreshShape({}, '', 'p1').error, /不带 topic_key/u);
  assert.match(S.quotaRefreshShape({ all: true }, '', '').error, /空的/u);
});

test('Covey 推来的条目理成页面要的形状：读不到是 null 不是 0，坏的字段当没有，只留 https 链接和像邮箱的登录', () => {
  assert.equal(S.quotaOf(null), undefined);
  assert.equal(S.quotaOf([]), undefined);
  assert.deepEqual(S.quotaOf({ sources: [] }).sources, []); // 没有来源也照给：页面据此收起这一页
  const d = S.quotaOf({
    at: NOW, read_at: NOW, relogin: [{ source: 'VPS', name: 'V2' }, { name: '' }, 5],
    refresh: { at: NOW - 100, busy: true, cooldown: 300 },
    sources: [
      { id: 'agy', kind: 'agy', title: 'agy', collected_at: NOW, ttl: 600, stale_after: 3600, accounts: [
        { name: '主号', login: 'main@example.com', state: 'ok', pools: [{ key: 'gemini', label: 'Gemini', active: true, state: 'ok', windows: [
          { kind: '5h', label: '5 小时', left: null }, { kind: '7d', left: 3 }, { kind: 'weird', left: 0.5 }, 7] }] },
        { name: '副号', login: 'not an email', state: 'nope', pools: [] }, { login: 'x@example.com' }] },
      { id: 'gw', kind: 'mystery', title: '', link: 'http://insecure.example.com', failing: 'yes', accounts: 'many' }, 'nope'],
  });
  assert.equal(d.sources.length, 2);
  const [agy, gw] = d.sources;
  assert.equal(agy.accounts.length, 2); // 没有名字的号丢掉
  const [main, spare] = agy.accounts;
  assert.equal(main.login, 'main@example.com');
  assert.equal(spare.login, undefined); // 不像邮箱的不留
  assert.equal(spare.state, 'unknown');
  assert.deepEqual(main.pools[0].windows.map((w) => [w.kind, w.left]), [['5h', null], ['7d', 1], ['other', 0.5]]);
  assert.equal(main.pools[0].active, true);
  assert.equal(gw.kind, 'command');
  assert.equal(gw.title, 'gw');
  assert.equal(gw.link, undefined);
  assert.equal(gw.failing, false);
  assert.deepEqual(gw.accounts, []);
  assert.deepEqual(d.relogin, [{ source: 'VPS', name: 'V2' }]);
  assert.deepEqual(d.refresh, { at: NOW - 100, busy: true, cooldown: 300 });
  assert.equal(S.quotaOf({ sources: [{ id: 'x', link: 'https://ops.example.com/a' }] }).sources[0].link, 'https://ops.example.com/a');
  const many = S.quotaOf({ sources: Array.from({ length: 30 }, (_, i) => ({ id: `s${i}` })) });
  assert.equal(many.sources.length, S.QUOTA_MAX.sources);
});

test('时刻怎么写：上海时间的几月几号几点几分，当天、明天、周窗口、过去的别的天一个写法（主人 11-27）', () => {
  assert.equal(Q.whenWords(at(19, 5)), '11月26日 19:05');
  assert.equal(Q.whenWords(at(3, 30, 1)), '11月27日 03:30'); // 不写“明天”、不写周几
  assert.equal(Q.whenWords(at(8, 0, 2)), '11月28日 08:00');
  assert.equal(Q.whenWords(at(0, 1, 7)), '12月3日 00:01');
  assert.equal(Q.whenWords(at(9, 0, 20)), '12月16日 09:00');
  assert.equal(Q.whenWords(at(22, 0, -1)), '11月25日 22:00');
  assert.equal(Q.whenWords(0), '');
  assert.equal(Q.agoWords(NOW - 6 * 3600, NOW), '6 小时前');
  assert.equal(Q.agoWords(NOW - 20, NOW), '刚刚');
});

test('跨零点、跨月、跨年：几月几号几点几分都是上海的，半夜写 00:00 不写 24:00', () => {
  const sh = (iso) => Date.parse(`${iso}+08:00`) / 1000;
  assert.equal(Q.whenWords(sh('2026-11-27T00:00:00')), '11月27日 00:00');
  assert.equal(Q.whenWords(sh('2026-12-01T08:00:00')), '12月1日 08:00');
  assert.equal(Q.whenWords(sh('2027-01-01T03:00:00')), '1月1日 03:00');
  assert.equal(Q.whenWords(sh('2026-12-31T23:59:00')), '12月31日 23:59');
  assert.equal(Q.whenWords(Date.parse('2026-11-26T16:00:00Z') / 1000 + 3720), '11月27日 01:02'); // UTC 还是 26 日，上海已是 27 日
});

test('用完的格、侧栏胶囊、标题行的读数时刻：同一个写法', () => {
  const s = source();
  const out = (back) => Q.cellOf(s, pool([], { state: 'spent', available_at: back }), '5h', NOW).at;
  assert.equal(out(at(23, 20)), '11月26日 23:20 恢复');
  assert.equal(out(at(8, 0, 1)), '11月27日 08:00 恢复');
  assert.equal(out(at(15, 2, 7)), '12月3日 15:02 恢复');
  const bar = Q.cellOf(s, pool([win('7d', 0.6, { resets_at: at(9, 0, 4) })]), '7d', NOW);
  assert.equal(bar.at, '11月30日 09:00'); // 没用完：只写重置时刻
  const dry = (back) => ({ at: NOW, read_at: NOW, relogin: [], refresh: { at: 0, busy: false, cooldown: 300 },
    sources: [source({ title: 'agy', low: true, dry: true, next_available_at: back })] });
  assert.equal(Q.capsuleOf(dry(at(8, 0, 1))), 'agy 额度见底 · 11月27日 08:00 恢复');
  assert.equal(Q.readWords(dry(0)), '11月26日 14:32');
});

test('时刻那一列：数字等宽，宽度放得下“12月31日 15:02 恢复”；表窄时时刻换到条下面；手机上时刻单占一行；侧栏那一行时刻整块换行', () => {
  const css = read('quota.css');
  const col = css.match(/\.cv \.mq-c\{display:grid;grid-template-columns:42px minmax\(40px,1fr\) (\d+)px;/u);
  assert.ok(col && Number(col[1]) >= 114 && Number(col[1]) <= 124, '第 3 列 114~124px');
  assert.match(css.match(/\.cv \.mq-c \.t\{([^}]*)\}/u)?.[1] ?? '', /font-variant-numeric:tabular-nums/u);
  const phone = css.slice(css.indexOf('@media (max-width:767px)'));
  assert.match(phone, /\.cv \.mq-c \.b,\.cv \.mq-c \.t\{grid-column:1\/-1\}/u);
  assert.equal(/\.mq-c \.b\{[^}]*grid-row/u.test(phone), false, '细条不钉在第 2 行：有池名时池名在上');
  // 表窄（768 的窄屏、1024 左右开着侧栏）时账号列和间距让一点，再窄时刻换到条下面；都写在手机那段前面，手机的排法压过它们
  assert.match(css.match(/\.cv \.mq\{([^}]*)\}/u)?.[1] ?? '', /container-type:inline-size/u);
  const narrow = css.indexOf('@container (max-width:780px)');
  const under = css.indexOf('@container (max-width:720px)');
  assert.ok(narrow > 0 && under > narrow && under < css.indexOf('@media (max-width:767px)'));
  assert.match(css.slice(under, css.indexOf('}', css.indexOf('.t{', under)) + 1), /\.cv \.mq-c \.t\{grid-column:2\}/u);
  assert.match(css, /\.cv \.qcap-w\{white-space:nowrap\}/u);
  assert.match(read('Shell.tsx'), /capsule\.split\(' · '\)[\s\S]*?className="qcap-w"/u);
});

test('一格：百分比、细条、时刻；剩余不到 20% 红；用完写“用完”和“几月几号几点几分 恢复”；读不到写“—”', () => {
  const s = source();
  const ok = Q.cellOf(s, pool([win('5h', 0.64, { resets_at: at(19, 5) })]), '5h', NOW);
  assert.deepEqual([ok.kind, ok.pct, ok.at, ok.tight, ok.stale], ['bar', 64, '11月26日 19:05', false, false]);
  const week = Q.cellOf(s, pool([win('7d', 0.71, { resets_at: at(9, 0, 1) })]), '7d', NOW);
  assert.equal(week.at, '11月27日 09:00');
  const tight = Q.cellOf(s, pool([win('5h', 0.12)]), '5h', NOW);
  assert.deepEqual([tight.kind, tight.pct, tight.tight], ['bar', 12, true]);
  assert.equal(Q.cellOf(s, pool([win('5h', 0.2)]), '5h', NOW).tight, false); // 正好 20% 不红
  const out = Q.cellOf(s, pool([win('5h', 0, { resets_at: at(17, 10) })], { state: 'spent', available_at: at(17, 10) }), '5h', NOW);
  assert.deepEqual([out.kind, out.pct, out.at, out.tight], ['out', 0, '11月26日 17:10 恢复', false]);
  const none = Q.cellOf(s, pool([win('5h', null)]), '5h', NOW);
  assert.deepEqual([none.kind, none.pct, none.at], ['none', null, '']); // 读不到：不画条、不写 0
  assert.equal(Q.cellOf(s, pool([]), '7d', NOW).kind, 'none');
  assert.equal(Q.cellOf(s, pool([win('5h', 1)], { state: 'ok' }), '7d', NOW).kind, 'none'); // 没有这个窗口
});

test('号 state 是用完而读数里没有哪个窗口为 0：写在 5 小时那一列；读数里为 0 的窗口写在它那一列', () => {
  const s = source();
  const spent = pool([], { state: 'spent', available_at: at(17, 10) });
  assert.equal(Q.cellOf(s, spent, '5h', NOW).at, '11月26日 17:10 恢复');
  assert.equal(Q.cellOf(s, spent, '7d', NOW).kind, 'none');
  const weekOut = pool([win('5h', 0.6), win('7d', 0, { resets_at: at(9, 0, 2) })], { state: 'spent', available_at: at(9, 0, 2) });
  assert.equal(Q.cellOf(s, weekOut, '5h', NOW).kind, 'bar');
  assert.equal(Q.cellOf(s, weekOut, '7d', NOW).at, '11月28日 09:00 恢复');
});

test('旧读数数字灰、悬停说多旧；已重置还没有新读数写“—”；太旧的来源整份写“—”；读取没成功的灰', () => {
  const agy = source({ kind: 'agy', stale_after: 3600 });
  const old = Q.cellOf(agy, pool([win('7d', 0.93, { checked_at: NOW - 6 * 3600, resets_at: at(9, 0, 2) })]), '7d', NOW);
  assert.deepEqual([old.kind, old.stale], ['bar', true]);
  assert.match(old.title, /6 小时前的读数（11月26日 08:32）/u);
  assert.equal(Q.cellOf(agy, pool([win('5h', 1, { checked_at: NOW - 600 })]), '5h', NOW).stale, false); // 10 分钟前不算旧
  const reset = Q.cellOf(agy, pool([win('5h', 0.3, { checked_at: at(10), resets_at: at(13) })]), '5h', NOW);
  // 已重置还没有新读数：数字写“—”，时刻那里写“已重置”，悬停说几点重置、重置前剩多少（不再是一格什么都没有）
  assert.deepEqual([reset.kind, reset.at, reset.title], ['none', '已重置', '11月26日 13:00 已重置，还没有新读数（重置前剩 30%）']);
  const stale = source({ collected_at: NOW - 3000, ttl: 600 });
  const gone = Q.cellOf(stale, pool([win('5h', 0.5, { checked_at: NOW - 3000 })]), '5h', NOW);
  assert.equal(gone.kind, 'none'); // 超过两倍 ttl
  assert.equal(gone.title, '读数停在 11月26日 13:42（50 分钟前），太旧不显示');
  const failing = source({ failing: true });
  assert.equal(Q.cellOf(failing, pool([win('5h', 0.5)]), '5h', NOW).stale, true);
});

test('同一种窗口有几个取剩得最少的', () => {
  const p = pool([win('7d', 0.9), win('7d', 0.3, { label: 'Fable 周' }), win('5h', 0.5)]);
  assert.equal(Q.pickWindow(p, '7d').left, 0.3);
  assert.equal(Q.pickWindow(pool([win('7d', null), win('7d', 0.4)]), '7d').left, 0.4);
  assert.equal(Q.pickWindow(pool([]), '5h'), null);
});

test('号的名字：登录邮箱完整写出来，读不到才用名字；要重新登录、暂停、到期是整行的话', () => {
  assert.equal(Q.nameOf(acct('主号', [], { login: 'main@example.com' })), 'main@example.com');
  assert.equal(Q.nameOf(acct('主号', [])), '主号');
  assert.equal(Q.rowNote(acct('V2', [], { state: 'login_required' })), '要重新登录');
  assert.equal(Q.rowNote(acct('V2', [], { state: 'paused' })), '已暂停');
  assert.equal(Q.rowNote(acct('V2', [], { state: 'expired' })), '已到期');
  assert.equal(Q.rowNote(acct('V2', [pool([win('5h', 0.5)])])), '');
  assert.equal(Q.accountActive(acct('a', [pool([], { key: 'gemini', active: true })])), true);
  assert.equal(Q.accountActive(acct('a', [pool([], { key: 'gemini', active: true }), pool([], { key: '3p' })])), false); // 两个池：蓝点在池名前
});

test('折起来的来源：各列取平均，k/n 个能用，有冷却或低额度的号时一个红点；展开的别的来源写“N 个号”，agy 写“用完自动顺延”', () => {
  const accounts = [
    acct('A1', [pool([win('5h', 0.78), win('7d', 0.9)])]),
    acct('A2', [pool([win('5h', 0.22), win('7d', 0.5)])]),
    acct('A3', [pool([win('5h', null), win('7d', 0.1)])]),
    acct('A4', [pool([], { state: 'cooling', available_at: at(15) })], { state: 'cooling' }),
    acct('A5', [], { state: 'login_required' }),
  ];
  const s = source({ accounts });
  const five = Q.avgCell(s, '5h', NOW);
  // (78 + 22 + 0 冷却 + 0 要重新登录) / 4：用不了的号按 0，读不到的（A3 的 5 小时）不算
  assert.deepEqual([five.kind, five.pct, five.at], ['bar', 25, '4 个号平均']);
  assert.match(five.title, /^4 个号的5 小时剩余平均：（78\+22\+0\+0）÷ 4 ≈ 25%/u);
  assert.match(five.title, /按 0 算：A4 冷却中，11月26日 15:00 恢复；A5 要重新登录/u);
  assert.match(five.title, /没算进去：A3 读不到/u);
  const week = Q.avgCell(s, '7d', NOW);
  assert.deepEqual([week.pct, week.at], [30, '5 个号平均']); // (90 + 50 + 10 + 0 + 0) / 5
  assert.equal(Q.avgCell(source({ accounts: [] }), '5h', NOW).kind, 'none');
  assert.equal(Q.avgCell(source({ accounts, collected_at: NOW - 3000 }), '5h', NOW).kind, 'none'); // 来源太旧：不拿旧状态算出个 0%
  assert.equal(Q.sourceWarn(s, NOW), true);
  assert.equal(Q.sourceWarn(source({ accounts: [acct('A1', [pool([win('5h', 0.78), win('7d', 0.9)])])] }), NOW), false);
  assert.equal(Q.groupNote(s, false), '7/8 个能用');
  assert.equal(Q.groupNote(s, true), '7/8 个能用'); // 网关展开了也写几个能用
  assert.equal(Q.groupNote(source({ kind: 'command', accounts }), true), '5 个号');
  assert.equal(Q.groupNote(source({ kind: 'agy', accounts: [acct('a', []), acct('b', [])] }), true), '2 个号 · 用完自动顺延');
  assert.equal(Q.isOpen(source({ expand: true }), undefined), true);
  assert.equal(Q.isOpen(source({ expand: true }), false), false); // 点过的听点的
  assert.equal(Q.isOpen(source(), undefined), false);
});

test('侧栏胶囊：只在某个来源整体见底或有号要重新登录时出现，否则空串；设置页签只在认 quota_refresh 且有来源时出现', () => {
  const fine = { at: NOW, read_at: NOW, relogin: [], sources: [source()], refresh: { at: 0, busy: false, cooldown: 300 } };
  assert.equal(Q.capsuleOf(fine), '');
  assert.equal(Q.capsuleOf(null), '');
  const dry = { ...fine, sources: [source({ title: 'agy', low: true, dry: true, next_available_at: at(17, 10) })] };
  assert.equal(Q.capsuleOf(dry), 'agy 额度见底 · 11月26日 17:10 恢复');
  assert.equal(Q.capsuleOf({ ...fine, sources: [source({ title: 'agy', low: true })] }), 'agy 额度见底'); // 能用的号只是剩得少：没有恢复时刻
  const login = { ...fine, relogin: [{ source: '我的 VPS', name: 'V2' }, { source: '我的 VPS', name: 'V3' }, { source: 'agy', name: 'a@example.com' }] };
  assert.equal(Q.capsuleOf(login), '我的 VPS 有 2 个号要重新登录');
  assert.equal(Q.capsuleOf({ ...login, sources: [source({ low: true, title: 'X' })] }), 'X 额度见底'); // 两样都有：先说见底
  assert.equal(Q.reloginLine(login), '我的 VPS · V2 要重新登录；我的 VPS · V3 要重新登录；agy · a@example.com 要重新登录');
  const r = { rev: '1', data: fine, at: '' };
  assert.equal(Q.quotaOn(undefined, r), false); // 旧 Covey 没有 ops
  assert.equal(Q.quotaOn(['say', 'upgrade'], r), false);
  assert.equal(Q.quotaOn(['say', 'quota_refresh'], null), false);
  assert.equal(Q.quotaOn(['say', 'quota_refresh'], { ...r, data: { ...fine, sources: [] } }), false); // 来源都没了：收起
  assert.equal(Q.quotaOn(['say', 'quota_refresh'], r), true);
  assert.equal(Q.readWords(fine), '11月26日 14:32');
  assert.equal(Q.readWords({ ...fine, read_at: 0 }), '');
});

test('发出 quota_refresh 以后重取条目（Covey 回执前推了“正在读取”，读完新的快照随后到）', () => {
  assert.deepEqual(K.pullPlan({ kind: 'quota_refresh', payload: {} }), { all: [{ t: 'quota' }], follow: [{ t: 'quota' }] });
  assert.equal(K.pullId({ t: 'quota' }), 'quota');
});

test('页面接线：只有一个「刷新」按钮，没有暂停、只用这个号、查全部账号；设置页和侧栏只在 Covey 认 quota_refresh 时取条目', () => {
  const panel = read('Quota.tsx');
  assert.match(panel, /kind: 'quota_refresh', payload: \{\}/u);
  assert.match(panel, /useOutboxOp\('quota:refresh'\)/u);
  assert.match(panel, /disabled=\{busy\}/u);
  for (const word of ['暂停', '只用这个号', '查全部账号', '运维台↗']) {
    assert.equal(panel.includes(word), word === '运维台↗', word);
  }
  assert.equal((panel.match(/<button/gu) ?? []).length, 1);
  assert.match(panel, /rel="noopener noreferrer"/u); // 运维台链接
  const settings = read('Settings.tsx');
  assert.match(settings, /canDo\(snap\?\.data\.ops, 'quota_refresh'\)/u);
  assert.match(settings, /useQuota\(quotable\)/u);
  const shell = read('Shell.tsx');
  assert.match(shell, /canDo\(ops, 'quota_refresh'\)/u);
  assert.match(shell, /useQuota\(on\)/u);
  assert.match(shell, /to="\/settings\/quota"/u);
});

test('窄条侧栏（收起）：同一个 capsule 条件，带红点的图标，悬停是胶囊那一句，点了到设置 → 账号与额度；不放进底部（那里是设置和深浅色）', () => {
  const shell = read('Shell.tsx');
  const mini = shell.match(/\{capsule && mini \? \(\s*<Link to="\/settings\/quota" className="qcap-mini" title=\{capsule\} aria-label=\{capsule\}>[\s\S]*?<\/Link>\s*\) : null\}/u);
  assert.ok(mini, '窄条的标记：条件、链接、悬停和读屏文字都是 capsule');
  assert.match(mini[0], /<i aria-hidden="true" \/>/u, '红点');
  assert.match(shell, /\{capsule && !mini \? \(/u, '展开时的胶囊仍在');
  const foot = shell.slice(shell.indexOf('className="sidefoot"'));
  assert.equal(foot.slice(0, foot.indexOf('</aside>')).includes('qcap'), false, '底部没有额度标记');
  const css = read('quota.css');
  const rule = css.match(/\.cv \.qcap-mini\{([^}]*)\}/u)?.[1] ?? '';
  assert.match(rule, /width:40px/u, '和窄条里别的图标位同宽，56px 的窄条（padding 6）放得下');
  assert.match(css, /\.cv \.qcap-mini i\{position:absolute/u);
  // 条件和展开时的胶囊一样：capsuleOf 给空串就不画
  assert.equal(Q.capsuleOf({ at: NOW, read_at: NOW, relogin: [], sources: [source()], refresh: { at: 0, busy: false, cooldown: 300 } }), '');
});

// 主人 11-29 的截图（T974）：Max-5x-8 周额度用完、不能用，5 小时窗口本身还剩 100%。
// 原来 5 小时平均把它当 100% 算（(100+44+80+100+73+25)/6≈70%），周平均却按 0 算（38%），两列口径不一样。
const shot = () => {
  const w5 = (left, over = {}) => win('5h', left, { label: '5 小时', ceiling: 0.98, ...over });
  const w7 = (left, over = {}) => win('7d', left, { label: '周', ceiling: 0.99, resets_at: at(19, 59, 4), ...over });
  const fable = (left) => win('7d', left, { label: 'Fable 周', resets_at: at(19, 59, 4), checked_at: NOW - 5 * 3600 });
  const one = (name, windows, over = {}) => acct(name, [pool(windows, over)], over);
  return source({
    usable: 5, total: 6, accounts: [
      one('Max-5x-4', [w5(1, { resets_at: undefined, checked_at: NOW - 25 * 60 }), w7(0.09, { checked_at: NOW - 25 * 60 }), fable(0.56)]),
      one('Max-5x-7', [w5(0.44), w7(0.45), fable(0.86)]),
      one('Max-5x-6', [w5(0.8), w7(0.75), fable(1)]),
      one('Max-5x-8', [w5(1, { resets_at: undefined }), w7(0.01, { resets_at: at(2, 59, 5) }), fable(0.77)], { state: 'spent', available_at: at(3, 2, 5) }),
      one('Max-5x-9', [w5(0.73), w7(0.06), fable(0.53)]),
      one('Max-5x-10', [w5(0.25), w7(0.9), fable(1)]),
    ],
  });
};

test('截图那组：用完的号 5 小时按 0 算进平均，两列一个口径；悬停写出算式', () => {
  const s = shot();
  const five = Q.avgCell(s, '5h', NOW);
  assert.deepEqual([five.pct, five.at], [54, '6 个号平均']); // (100+44+80+0+73+25)/6
  assert.match(five.title, /（100\+44\+80\+0\+73\+25）÷ 6 ≈ 54%/u);
  assert.match(five.title, /按 0 算：Max-5x-8 周用完，12月1日 03:02 恢复/u);
  const week = Q.avgCell(s, '7d', NOW);
  assert.deepEqual([week.pct, week.at], [38, '6 个号平均']); // (9+45+75+0+6+90)/6 = 37.5
  assert.match(week.title, /（9\+45\+75\+0\+6\+90）÷ 6/u);
  assert.equal(Q.usableTitle(s), '现在用不了的号\nMax-5x-8：周用完，12月1日 03:02 恢复');
});

test('用不了的号：另一列只是窗口本身的读数，灰、不红，悬停说为什么、平均里按 0；用完那一列照旧写恢复时刻', () => {
  const s = shot();
  const a = s.accounts[3];
  const five = Q.cellOf(s, a.pools[0], '5h', NOW, a);
  assert.deepEqual([five.kind, five.pct, five.off, five.tight, five.at], ['bar', 100, true, false, '未开始计时']);
  assert.match(five.title, /^这个号现在用不了（周用完，12月1日 03:02 恢复），这一格只是窗口本身的读数，平均里按 0 算/u);
  const week = Q.cellOf(s, a.pools[0], '7d', NOW, a);
  assert.deepEqual([week.kind, week.at, week.off], ['out', '12月1日 03:02 恢复', false]);
  assert.equal(Q.accountWarn(s, a, NOW), true);
  // 能用的号不灰；不传号（旧调用）也照旧
  const ok = s.accounts[1];
  assert.equal(Q.cellOf(s, ok.pools[0], '5h', NOW, ok).off, false);
  assert.equal(Q.cellOf(s, a.pools[0], '5h', NOW).off, false);
});

test('一格的悬停：同一种窗口都列出来（周、Fable 周），重置时刻、停派线；没开始计时的窗口写“未开始计时”', () => {
  const s = shot();
  const a = s.accounts[0];
  const week = Q.cellOf(s, a.pools[0], '7d', NOW, a);
  assert.equal(week.pct, 9); // 取剩得最少的
  const lines = week.title.split('\n');
  assert.equal(lines[0], '周 剩 9%，11月30日 19:59 重置，用到 99% 停派');
  assert.equal(lines[1], 'Fable 周 剩 56%，11月30日 19:59 重置');
  assert.match(lines[2], /^25 分钟前的读数/u);
  const five = Q.cellOf(s, a.pools[0], '5h', NOW, a);
  assert.deepEqual([five.pct, five.at, five.stale], [100, '未开始计时', true]);
  assert.match(five.title, /还没开始计时（下次用到这个号才开始算）/u);
  // 用过一点、来源没给重置时刻：不写“未开始计时”
  assert.equal(Q.cellOf(s, pool([win('5h', 0.5, { resets_at: undefined })]), '5h', NOW).at, '');
});

test('来源读不到时组名下一行说清楚：读数停在几点和原因；只是上次没读到也说；读得好好的什么都不写', () => {
  assert.equal(Q.sourceAlert(source(), NOW), '');
  assert.equal(Q.sourceAlert(source({ failing: true, why: 'HTTP 404', collected_at: NOW - 400 }), NOW), '上次读取没成功 · HTTP 404，数字是 11月26日 14:25 的');
  assert.equal(Q.sourceAlert(source({ failing: true, why: '连不上：URLError', collected_at: NOW - 3000 }), NOW),
    '读数停在 11月26日 13:42（50 分钟前） · 连不上：URLError');
  assert.equal(Q.sourceAlert(source({ collected_at: NOW - 3000 }), NOW), '读数停在 11月26日 13:42（50 分钟前）'); // 没在失败：Covey 没去读（比如 Bridge 停着）
  assert.equal(Q.sourceAlert(source({ failing: true, why: '', collected_at: 0, total: 0 }), NOW), '读不到');
  assert.equal(Q.groupNote(source({ failing: true, total: 0 }), false), ''); // 原因在 sourceAlert，不重复写“读不到”
  // 服务端：原因只在 failing 时留，最多 60 字
  const d = S.quotaOf({ sources: [{ id: 'gw', failing: true, why: 'x'.repeat(80) }, { id: 'ok', failing: false, why: 'stale' }, { id: 'old' }] });
  assert.deepEqual(d.sources.map((x) => x.why.length), [60, 0, 0]);
});

test('页面接线：格子传号进去（用不了的号才灰）、组名下的原因、表头悬停说口径、取不到时说会再取', () => {
  const panel = read('Quota.tsx');
  assert.match(panel, /cellOf\(s, a\.pools\[0\], kind, now, a\)/u);
  assert.match(panel, /sourceAlert\(s, now\)/u);
  assert.match(panel, /className="mq-why"/u);
  assert.match(panel, /title=\{HEAD_TITLE\}>5 小时剩余/u);
  assert.match(panel, /q\.isError \? '这次没取到账号与额度，30 秒后自动再取'/u);
  assert.match(read('Settings.tsx'), /tab === QUOTA_TAB\.key && quotable && \(qt\.isLoading \|\| \(qt\.isError && !qt\.data\)\)/u);
  assert.match(read('quota.css'), /\.cv \.mq-c\.off \.n\{color:var\(--t3\)/u);
});

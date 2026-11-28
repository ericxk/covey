// 自托管服务的接口级测试：起真服务（构建产物 dist-selfhost，PGlite 用临时目录），走完密钥、登录、会话、频控的全流程。
// 先 npm run build:selfhost:server（test:selfhost-api 脚本会先构建）。端口 18690–18699 里找一个空的。
// 公网 HTTPS 也一并测：公网监听绑 127.0.0.1 的另一个端口（18760–18779），用生成的自签证书做 ca 去连。
// 设了 SELFHOST_TEST_DATABASE_URL 就改连那个外部 Postgres（要一个空库），验 DATABASE_URL 那条路。
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { X509Certificate } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import net from 'node:net';

const WEB = new URL('../', import.meta.url).pathname;
const MAIN = join(WEB, 'dist-selfhost/server/selfhost/main.js');
const KEY = 'test-key-not-real-0123456789';
const SECOND_KEY = 'second-key-not-real-abcdef';
const CHROME_MAC = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

let dir;
let child;
let base;
let keyFile;
let pubPort;
const output = [];
const extra = []; // 额外起的服务（恢复、证书出错那几个），after 里一起停
const usedPorts = new Set();

function freePort(port) {
  return new Promise((ok) => {
    const s = net.createServer();
    s.once('error', () => ok(false));
    s.listen(port, '127.0.0.1', () => s.close(() => ok(true)));
  });
}

/** 18760–18779 里取一个没人用的端口 */
async function takePort() {
  for (let p = 18760; p <= 18779; p += 1) {
    if (usedPorts.has(p) || !(await freePort(p))) continue;
    usedPorts.add(p);
    return p;
  }
  throw new Error('18760-18779 没有空端口');
}

before(async () => {
  assert.ok(existsSync(MAIN), '先构建：npm run build:selfhost:server');
  let port = 0;
  for (let p = 18690; p <= 18699 && !port; p += 1) if (await freePort(p)) port = p;
  assert.ok(port, '18690-18699 没有空端口');
  pubPort = await takePort();
  dir = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'selfhost-api-'));
  keyFile = join(dir, 'key');
  writeFileSync(keyFile, `${KEY}\n`, { mode: 0o600 });
  const staticDir = join(dir, 'client');
  child = spawn(process.execPath, [MAIN], {
    cwd: WEB,
    env: { ...process.env, COVEY_WEB_PORT: String(port), COVEY_WEB_DB_DIR: join(dir, 'db'), COVEY_WEB_KEY_FILE: keyFile, COVEY_WEB_STATIC_DIR: staticDir, COVEY_WEB_PUBLIC_NAMES: '127.0.0.1', COVEY_WEB_PUBLIC_PORT: String(pubPort), COVEY_WEB_PUBLIC_HOST: '127.0.0.1', COVEY_WEB_TLS_DIR: join(dir, 'tls'), DATABASE_URL: process.env.SELFHOST_TEST_DATABASE_URL || '', NODE_ENV: 'production' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => output.push(String(d)));
  child.stderr.on('data', (d) => output.push(String(d)));
  base = `http://127.0.0.1:${port}`;
  const t0 = Date.now();
  for (;;) {
    try {
      await fetch(`${base}/api/me`);
      break;
    } catch {
      if (Date.now() - t0 > 60000) throw new Error(`服务没起来：${output.join('')}`);
      await new Promise((r) => setTimeout(r, 200));
    }
  }
  for (let i = 0; i < 100; i += 1) {
    const h = await (await fetch(`${base}/healthz`)).json();
    if (h.public?.listening || h.public?.error) break;
    await new Promise((r) => setTimeout(r, 100));
  }
});

after(() => {
  for (const e of extra) e.kill('SIGTERM');
  if (child && !child.killed) child.kill('SIGTERM');
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const cookieOf = (res, name) => {
  for (const c of res.headers.getSetCookie()) if (c.startsWith(`${name}=`)) return c;
  return null;
};
const cookiePair = (setCookie) => setCookie.split(';')[0];
const api = (path, { method = 'GET', cookie = '', body, headers = {} } = {}) =>
  fetch(`${base}${path}`, {
    method,
    headers: { 'user-agent': CHROME_MAC, ...(method !== 'GET' ? { 'x-covey-csrf': '1' } : {}), ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
const open = (path, { method = 'GET', body, key = KEY, headers = {} } = {}) =>
  fetch(`${base}/openapi/sync/${path}`, {
    method,
    headers: { ...(key ? { authorization: `Bearer ${key}` } : {}), ...(body ? { 'content-type': 'application/json' } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });

let ip = 10;
/** 发起一次登录，用 X-Forwarded-For（本机来的才信）换一个来源 IP，避开同 IP 30 秒一次 */
async function startLogin() {
  ip += 1;
  const res = await api('/api/auth/login-requests', { method: 'POST', headers: { 'x-forwarded-for': `203.0.113.${ip}` } });
  return res;
}
const decide = (id, decision) =>
  open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [{ type: 'login', key: id, rev: `d-${decision}`, data: { decision, decided_at: new Date().toISOString() } }] } });

test('开放接口：没有密钥、错密钥、格式不对都拒绝，对的放行', async () => {
  assert.equal((await open('inbox', { key: '' })).status, 401);
  assert.equal((await open('inbox', { key: 'wrong-key' })).status, 401);
  assert.equal((await open('inbox', { key: KEY.slice(0, -1) })).status, 401);
  assert.equal((await fetch(`${base}/openapi/sync/inbox`, { headers: { authorization: KEY } })).status, 401);
  const ok = await open('inbox');
  assert.equal(ok.status, 200);
  assert.deepEqual(await ok.json(), { items: [] });
});

test('大小写换个写法也绕不过：/OPENAPI 照样要密钥，/API 照样要会话', async () => {
  for (const p of ['/OPENAPI/sync/inbox', '/OpenApi/sync/inbox', '/openAPI/sync/inbox/']) {
    assert.equal((await fetch(`${base}${p}`)).status, 401, p);
    assert.equal((await fetch(`${base}${p}`, { headers: { authorization: 'Bearer wrong-key' } })).status, 401, p);
    assert.equal((await fetch(`${base}${p}`, { headers: { authorization: `Bearer ${KEY}`, 'x-forwarded-for': '198.51.100.9' } })).status, 403, p);
  }
  const forged = await fetch(`${base}/OPENAPI/sync/upsert`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ items: [{ type: 'login', key: 'a'.repeat(32), rev: 'x', data: { decision: 'approved' } }] }),
  });
  assert.equal(forged.status, 401);
  for (const p of ['/API/home', '/Api/auth/sessions', '/api/HOME']) assert.equal((await api(p)).status, 401, p);
  assert.equal((await api('/API/auth/logout-all', { method: 'POST' })).status, 401);
});

test('开放接口：带转发头的（不是直接从本机来）拒绝；密钥文件换了读到新的', async () => {
  assert.equal((await open('inbox', { headers: { 'x-forwarded-for': '198.51.100.7' } })).status, 403);
  writeFileSync(keyFile, `${SECOND_KEY}\n`);
  const future = new Date(Date.now() + 5000);
  (await import('node:fs')).utimesSync(keyFile, future, future);
  assert.equal((await open('inbox')).status, 401);
  assert.equal((await open('inbox', { key: SECOND_KEY })).status, 200);
});

test('开放接口：推数据、取发件箱', async () => {
  const now = Date.now() / 1000;
  const res = await open('upsert', {
    method: 'POST',
    key: SECOND_KEY,
    body: {
      items: [
        { type: 'project', key: 'p1', rev: 'r1', data: { title: '专项一', status: '在做', updated_at: now } },
        { type: 'topic', key: 't1', rev: 'r1', data: { title: '话题一', state: '在做', number: 1, project_key: 'p1', updated_at: now } },
        { type: 'turn', key: 't1:1', rev: 'r1', data: { topic_key: 't1', seq: 1, role: 'owner', status: 'done', body: '你好' } },
      ],
    },
  });
  assert.equal(res.status < 300, true);
  const body = await res.json();
  assert.equal(body.upserted, 3);
  assert.equal(body.skipped, 0);
});

test('页面接口：没登录 401；伪造平台身份头也是 401', async () => {
  assert.equal((await api('/api/me')).status, 401);
  assert.equal((await api('/api/snapshot')).status, 401);
  const forged = await api('/api/snapshot', { headers: { 'x-larkgw-suda-webuser': JSON.stringify({ userId: 'selfhost-owner', userName: 'x' }) } });
  assert.equal(forged.status, 401);
  const forgedCookie = await api('/api/snapshot', { cookie: 'covey_session=not-a-real-token' });
  assert.equal(forgedCookie.status, 401);
  assert.equal((await api('/api/auth/sessions')).status, 401);
});

test('页面接口：非 GET 没有 x-covey-csrf 被拒', async () => {
  const res = await fetch(`${base}/api/auth/login-requests`, { method: 'POST', headers: { 'x-forwarded-for': '203.0.113.200' } });
  assert.equal(res.status, 403);
});

let approved; // { cookie, id }
test('登录：发起得到 4 位对照码、绑浏览器的 cookie、发件箱里有 login 操作', async () => {
  const res = await startLogin();
  assert.equal(res.status, 200);
  const { id, code, expires_at } = await res.json();
  assert.match(code, /^[A-Z0-9]{4}$/u);
  assert.match(id, /^[0-9a-f]{32}$/u);
  const left = new Date(expires_at).getTime() - Date.now();
  assert.ok(left > 4 * 60 * 1000 && left <= 5 * 60 * 1000, `过期时间 ${left}`);
  const c = cookieOf(res, 'covey_login');
  assert.ok(c);
  assert.match(c, /HttpOnly/u);
  assert.match(c, /SameSite=Lax/u);
  assert.doesNotMatch(c, /Secure/u);
  const inbox = await (await open('inbox', { key: SECOND_KEY })).json();
  const op = inbox.items.find((i) => i.op_id === `login-${id}`);
  assert.ok(op, '发件箱里没有 login 操作');
  assert.equal(op.kind, 'login');
  assert.equal(op.payload.request_id, id);
  assert.equal(op.payload.code, code);
  assert.equal(op.payload.browser, 'Chrome');
  assert.equal(op.payload.os, 'macOS');
  // 秒数（Covey 按秒算 5 分钟有效期），不是 ISO 字符串
  assert.equal(typeof op.payload.at, 'number');
  assert.ok(Math.abs(op.payload.at - Date.now() / 1000) < 60, `at=${op.payload.at}`);
  approved = { id, loginCookie: cookiePair(c) };
});

test('登录：没批准前一直 pending；别的浏览器（没有 cookie）问不到', async () => {
  const mine = await api(`/api/auth/login-requests/${approved.id}`, { cookie: approved.loginCookie });
  assert.deepEqual(await mine.json(), { state: 'pending' });
  assert.equal(mine.headers.getSetCookie().length, 0);
  assert.equal((await api(`/api/auth/login-requests/${approved.id}`)).status, 404);
  assert.equal((await api(`/api/auth/login-requests/${approved.id}`, { cookie: 'covey_login=other' })).status, 404);
  assert.equal((await api('/api/auth/login-requests/not-an-id', { cookie: approved.loginCookie })).status, 404);
});

test('登录：推 approved 后轮询拿到会话 cookie；一次性', async () => {
  const up = await decide(approved.id, 'approved');
  assert.equal(up.status < 300, true);
  assert.equal((await up.json()).upserted, 1);
  // 别的浏览器仍拿不到
  assert.equal((await api(`/api/auth/login-requests/${approved.id}`, { cookie: 'covey_login=other' })).status, 404);
  const res = await api(`/api/auth/login-requests/${approved.id}`, { cookie: approved.loginCookie });
  assert.deepEqual(await res.json(), { state: 'approved' });
  const s = cookieOf(res, 'covey_session');
  assert.ok(s, '没有会话 cookie');
  assert.match(s, /HttpOnly/u);
  assert.match(s, /SameSite=Lax/u);
  assert.match(s, /Max-Age=2592000/u);
  approved.session = cookiePair(s);
  const again = await api(`/api/auth/login-requests/${approved.id}`, { cookie: approved.loginCookie });
  assert.deepEqual(await again.json(), { state: 'used' });
  assert.equal(cookieOf(again, 'covey_session'), null);
});

test('登录后：读看板、话题、发一句', async () => {
  const cookie = approved.session;
  const me = await api('/api/me', { cookie });
  assert.equal(me.status, 200);
  const snap = await (await api('/api/snapshot', { cookie })).json();
  assert.ok(snap.projects.some((p) => p.key === 'p1'));
  assert.ok(snap.topics.some((t) => t.key === 't1'));
  const turns = await api('/api/topics/t1/turns', { cookie });
  assert.equal(turns.status, 200);
  assert.equal((await turns.json()).turns.length, 1);
  assert.equal((await api('/api/topics/t1', { cookie })).status, 200);
  assert.equal((await api('/api/home', { cookie })).status, 200);
  const say = await api('/api/outbox', { method: 'POST', cookie, body: { op_id: 'say-1', kind: 'say', topic_key: 't1', payload: { text: '继续' } } });
  assert.equal(say.status < 300, true, await say.clone().text());
  const inbox = await (await open('inbox', { key: SECOND_KEY })).json();
  assert.ok(inbox.items.some((i) => i.op_id === 'say-1' && i.kind === 'say'));
  // 页面不能自己发 login 操作
  const forged = await api('/api/outbox', { method: 'POST', cookie, body: { op_id: 'login-x', kind: 'login', payload: {} } });
  assert.equal(forged.status, 400);
  // 页面发的东西不能走 /openapi
  assert.equal((await fetch(`${base}/openapi/sync/inbox`, { headers: { cookie } })).status, 401);
});

test('版本与更新（v39）：Covey 推 upgrade 条目、页面读到理好的形状；发件 upgrade 只收 check、run，不带话题', async () => {
  const cookie = approved.session;
  assert.equal((await (await api('/api/upgrade', { cookie })).text()), ''); // 没推过：空
  const pushed = await open('upsert', {
    method: 'POST',
    key: SECOND_KEY,
    body: { items: [{ type: 'settings', key: 'upgrade', rev: 'u1', data: {
      live: 'aaaaaaaa-bbbbbbbb', latest: 'aaaaaaaa-cccccccc', available: true, bridge: true,
      web: { kind: 'selfhost', current: false, manual: false }, count: 2,
      changes: [{ repo: 'covey-internal', sha: 'cccccccc', subject: '升级顺带更新网页', at: 1795600000 }, { repo: 'x', sha: '不是提交号' }],
      checked_at: 1795600000, checking: false, error: '', blocked: '', running: null, last: null } }] },
  });
  assert.equal(pushed.status < 300, true, await pushed.clone().text());
  const got = await (await api('/api/upgrade', { cookie })).json();
  assert.equal(got.rev, 'u1');
  assert.equal(got.data.latest, 'aaaaaaaa-cccccccc');
  assert.equal(got.data.changes.length, 1); // 不认得的提交丢掉
  assert.equal(got.data.count, 2);
  const run = await api('/api/outbox', { method: 'POST', cookie, body: { op_id: 'up-1', kind: 'upgrade', payload: { action: 'run' } } });
  assert.equal(run.status < 300, true, await run.clone().text());
  const inbox = await (await open('inbox', { key: SECOND_KEY })).json();
  assert.ok(inbox.items.some((i) => i.op_id === 'up-1' && i.kind === 'upgrade' && i.payload.action === 'run' && !i.topic_key));
  for (const [id, body] of [['up-2', { kind: 'upgrade', payload: { action: 'rollback' } }], ['up-3', { kind: 'upgrade', topic_key: 't1', payload: { action: 'check' } }],
    ['up-4', { kind: 'upgrade', payload: { action: 'run', ref: 'HEAD' } }]]) {
    assert.equal((await api('/api/outbox', { method: 'POST', cookie, body: { op_id: id, ...body } })).status, 400, id);
  }
});

test('账号与额度（v42）：Covey 推 quota 条目、页面读到理好的形状；发件 quota_refresh 不带话题、payload 空', async () => {
  const cookie = approved.session;
  assert.equal((await (await api('/api/quota', { cookie })).text()), ''); // 没推过：空
  const pushed = await open('upsert', {
    method: 'POST',
    key: SECOND_KEY,
    body: { items: [{ type: 'settings', key: 'quota', rev: 'q1', data: {
      at: 1795600000, read_at: 1795600000, relogin: [{ source: 'agy', name: 'a@example.com' }],
      refresh: { at: 0, busy: false, cooldown: 300 },
      sources: [{ id: 'agy', kind: 'agy', title: 'agy', collected_at: 1795600000, ttl: 600, stale_after: 3600, accounts: [
        { name: '主号', login: 'a@example.com', state: 'ok', pools: [{ key: 'gemini', label: 'Gemini', active: true, state: 'ok', windows: [{ kind: '5h', left: 0.64 }, { kind: '7d', left: 7 }] }] }, { login: 'x@example.com' }] }] } }] },
  });
  assert.equal(pushed.status < 300, true, await pushed.clone().text());
  const got = await (await api('/api/quota', { cookie })).json();
  assert.equal(got.rev, 'q1');
  assert.equal(got.data.sources[0].accounts.length, 1); // 没有名字的号丢掉
  assert.equal(got.data.sources[0].accounts[0].login, 'a@example.com');
  assert.equal(got.data.sources[0].accounts[0].pools[0].windows[1].left, 1); // 超出 0~1 的收回来
  const run = await api('/api/outbox', { method: 'POST', cookie, body: { op_id: 'qr-1', kind: 'quota_refresh', payload: {} } });
  assert.equal(run.status < 300, true, await run.clone().text());
  const inbox = await (await open('inbox', { key: SECOND_KEY })).json();
  assert.ok(inbox.items.some((i) => i.op_id === 'qr-1' && i.kind === 'quota_refresh' && !i.topic_key));
  for (const [id, body] of [['qr-2', { kind: 'quota_refresh', topic_key: 't1', payload: {} }], ['qr-3', { kind: 'quota_refresh', payload: { all: true } }]]) {
    assert.equal((await api('/api/outbox', { method: 'POST', cookie, body: { op_id: id, ...body } })).status, 400, id);
  }
});

test('更快的取数（v38）：取件 wait 挂着等、快照 since 增量、回执 wait 挂着等；不带参数和以前一样', async () => {
  const cookie = approved.session;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const items0 = (await (await open('inbox', { key: SECOND_KEY })).json());
  assert.equal('waited' in items0, false, '不带 wait 的响应没有 waited，旧 Covey 看到的和以前一样');
  const after = Math.max(0, ...items0.items.map((i) => i.seq));
  // 没有新操作：挂到 wait 秒才回，带 waited: true
  const t0 = Date.now();
  const idle = await (await open(`inbox?after=${after}&wait=1`, { key: SECOND_KEY })).json();
  assert.deepEqual(idle, { items: [], waited: true });
  assert.ok(Date.now() - t0 >= 900 && Date.now() - t0 < 2500, `挂了 ${Date.now() - t0} 毫秒`);
  // 挂着的时候页面发了一条：马上回来，不用等满
  const held = open(`inbox?after=${after}&wait=8`, { key: SECOND_KEY }).then((r) => r.json());
  await sleep(300);
  const t1 = Date.now();
  assert.equal((await api('/api/outbox', { method: 'POST', cookie, body: { op_id: 'wait-1', kind: 'say', topic_key: 't1', payload: { text: '挂着等' } } })).status < 300, true);
  const got = await held;
  assert.ok(got.waited && got.items.some((i) => i.op_id === 'wait-1'), JSON.stringify(got));
  assert.ok(Date.now() - t1 < 1500, `发出后 ${Date.now() - t1} 毫秒才取到`);
  // wait 上限 10 秒、不是数就不等
  assert.deepEqual(await (await open(`inbox?after=999999&wait=abc`, { key: SECOND_KEY })).json(), { items: [], waited: true });

  // 快照：每次都带 cursor；since 是现在（什么都没变）时 topics 为空、keys 是全部话题
  const full = await (await api('/api/snapshot', { cookie })).json();
  assert.ok(typeof full.cursor === 'string' && !full.partial && !full.keys);
  const quiet = await (await api(`/api/snapshot?since=${encodeURIComponent(new Date(Date.now() + 1000).toISOString())}`, { cookie })).json();
  assert.equal(quiet.partial, true);
  assert.deepEqual(quiet.topics, []);
  assert.ok(quiet.keys.includes('t1') && quiet.keys.length === full.topics.length);
  assert.ok(quiet.projects.some((p) => p.key === 'p1') && quiet.projects.find((p) => p.key === 'p1').counts.topics >= 1, '专项的话题数照所有话题算');
  // 话题变了：只有它回来；轮次变了也算它变了
  const mark = new Date().toISOString();
  await sleep(50);
  await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [{ type: 'topic', key: 't1', rev: 'v38-a', data: { title: '话题一（改过）', state: '在做', number: 1, project_key: 'p1', updated_at: Date.now() / 1000 } }] } });
  const changed = await (await api(`/api/snapshot?since=${encodeURIComponent(mark)}`, { cookie })).json();
  assert.deepEqual(changed.topics.map((t) => t.key), ['t1']);
  assert.equal(changed.topics[0].title, '话题一（改过）');
  const mark2 = new Date().toISOString();
  await sleep(50);
  await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [{ type: 'turn', key: 't1:2', rev: 'v38-b', data: { topic_key: 't1', seq: 2, role: 'agent', status: 'done', body: '第二轮的回答' } }] } });
  const viaTurn = await (await api(`/api/snapshot?since=${encodeURIComponent(mark2)}`, { cookie })).json();
  assert.deepEqual(viaTurn.topics.map((t) => t.key), ['t1']);
  assert.equal(viaTurn.topics[0].latest, '第二轮的回答');
  // 不合法的 since 当没带：整份
  const bad = await (await api('/api/snapshot?since=不是时间', { cookie })).json();
  assert.ok(!bad.partial && bad.topics.some((t) => t.key === 't1'));

  // 话题的轮次（2026-11-28 T932）：你那一行先推来没图（图还没传上），它后面那一轮在跑；页面只按 after 增量取，那一行再也不换。
  // 带上次的 cursor 做 since：这之后改过的行一起回来；不带 since 和以前一样，只回 after 之后的和在跑的
  await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [
    { type: 'turn', key: 't1:3', rev: 'img-a', data: { topic_key: 't1', seq: 3, role: 'owner', status: 'done', body: '看这张图\n附件：图片 1 张' } },
    { type: 'turn', key: 't1:4', rev: 'img-a', data: { topic_key: 't1', seq: 4, role: 'agent', status: 'running', body: '' } },
  ] } });
  const first = await (await api('/api/topics/t1/turns', { cookie })).json();
  assert.ok(typeof first.cursor === 'string', '每次都带 cursor');
  const later = new Date(Date.now() + 1000).toISOString();
  const plain = await (await api(`/api/topics/t1/turns?after=3&since=${encodeURIComponent(later)}`, { cookie })).json();
  assert.deepEqual(plain.turns.map((t) => t.key), ['t1:4'], '之后没改过：只有在跑的');
  const mark3 = new Date().toISOString();
  await sleep(50);
  await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [
    { type: 'turn', key: 't1:3', rev: 'img-b', data: { topic_key: 't1', seq: 3, role: 'owner', status: 'done', body: '看这张图\n附件：图片 1 张', images: [{ id: 'abc', kind: 'sent', w: 10, h: 10 }] } },
  ] } });
  const fresh = await (await api(`/api/topics/t1/turns?after=3&since=${encodeURIComponent(mark3)}`, { cookie })).json();
  assert.deepEqual(fresh.turns.map((t) => t.key), ['t1:3', 't1:4']);
  assert.equal(fresh.turns[0].images?.[0]?.id, 'abc', '换成带图的那一版');
  const old = await (await api('/api/topics/t1/turns?after=3', { cookie })).json();
  assert.deepEqual(old.turns.map((t) => t.key), ['t1:4'], '不带 since：和以前一样');
  const badSince = await (await api('/api/topics/t1/turns?after=3&since=不是时间', { cookie })).json();
  assert.deepEqual(badSince.turns.map((t) => t.key), ['t1:4'], '不合法的 since 当没带');

  // 回执：还没回执时挂着等，Covey 回执后马上回；等满了没回执返回 new；没有这条发件仍是 404
  const none = Date.now();
  assert.equal((await api('/api/outbox/不存在?wait=5', { cookie })).status, 404);
  assert.ok(Date.now() - none < 1500);
  const w0 = Date.now();
  const still = await (await api('/api/outbox/wait-1?wait=1', { cookie })).json();
  assert.equal(still.state, 'new');
  assert.ok(Date.now() - w0 >= 900 && Date.now() - w0 < 2500, `挂了 ${Date.now() - w0} 毫秒`);
  const waiting = api('/api/outbox/wait-1?wait=8', { cookie }).then((r) => r.json());
  await sleep(300);
  const a0 = Date.now();
  await open('ack', { method: 'POST', key: SECOND_KEY, body: { results: [{ op_id: 'wait-1', state: 'done', message: '已作为补充交给在做的任务 #7' }] } });
  const rec = await waiting;
  assert.equal(rec.state, 'done');
  assert.equal(rec.message, '已作为补充交给在做的任务 #7');
  assert.ok(Date.now() - a0 < 1500, `回执后 ${Date.now() - a0} 毫秒才取到`);
  // 不带 wait：马上回，不挂
  const quick = Date.now();
  assert.equal((await (await api('/api/outbox/wait-1', { cookie })).json()).state, 'done');
  assert.ok(Date.now() - quick < 1000);
});

test('登录后：已登录的浏览器列表标出当前这个', async () => {
  const res = await api('/api/auth/sessions', { cookie: approved.session });
  const { sessions } = await res.json();
  assert.equal(sessions.length, 1);
  assert.equal(sessions[0].current, true);
  assert.equal(sessions[0].browser, 'Chrome');
  assert.equal(sessions[0].os, 'macOS');
  assert.ok(sessions[0].created_at && sessions[0].last_used_at);
  assert.equal('token_hash' in sessions[0], false);
});

test('登录：推 denied 的请求拿不到会话；没批准的重复推送不改状态', async () => {
  const res = await startLogin();
  const { id } = await res.json();
  const loginCookie = cookiePair(cookieOf(res, 'covey_login'));
  assert.equal((await decide(id, 'denied')).status < 300, true);
  const poll = await api(`/api/auth/login-requests/${id}`, { cookie: loginCookie });
  assert.deepEqual(await poll.json(), { state: 'denied' });
  assert.equal(cookieOf(poll, 'covey_session'), null);
  // denied 之后再推 approved 不生效
  await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [{ type: 'login', key: id, rev: 'late', data: { decision: 'approved' } }] } });
  const again = await api(`/api/auth/login-requests/${id}`, { cookie: loginCookie });
  assert.deepEqual(await again.json(), { state: 'denied' });
  // 推不合法的 decision 是 400
  const bad = await open('upsert', { method: 'POST', key: SECOND_KEY, body: { items: [{ type: 'login', key: id, rev: 'x', data: { decision: 'maybe' } }] } });
  assert.equal(bad.status, 400);
});

test('频控：同一来源 30 秒内只能发一次（429 + Retry-After）；全局待定最多 5 个', async () => {
  const hdr = { 'x-forwarded-for': '198.51.100.50' };
  const first = await api('/api/auth/login-requests', { method: 'POST', headers: hdr });
  assert.equal(first.status, 200);
  const second = await api('/api/auth/login-requests', { method: 'POST', headers: hdr });
  assert.equal(second.status, 429);
  const retry = Number(second.headers.get('retry-after'));
  assert.ok(retry >= 1 && retry <= 30, `Retry-After ${retry}`);
  // 到这里待定的：上一条（first）+ 再来几个不同来源直到 5 个，第 6 个被拒
  let ok = 1;
  let refused = null;
  for (let i = 0; i < 8 && !refused; i += 1) {
    const r = await startLogin();
    if (r.status === 200) ok += 1;
    else refused = r;
  }
  assert.equal(ok, 5, `待定上限前放行了 ${ok} 个`);
  assert.equal(refused?.status, 429);
  assert.ok(Number(refused.headers.get('retry-after')) >= 1);
});

test('登出：logout 只掉当前这个，logout-all 掉全部，会话立刻失效', async () => {
  // 登出后再登录一个新会话，需要一个新的待定名额：把待定的都拒绝掉
  const inbox = await (await open('inbox', { key: SECOND_KEY })).json();
  for (const it of inbox.items.filter((i) => i.kind === 'login')) await decide(it.payload.request_id, 'denied');
  const out1 = await api('/api/auth/logout', { method: 'POST', cookie: approved.session });
  assert.equal(out1.status, 200);
  assert.match(cookieOf(out1, 'covey_session') ?? '', /Max-Age=0/u);
  assert.equal((await api('/api/snapshot', { cookie: approved.session })).status, 401);

  async function login() {
    const r = await startLogin();
    assert.equal(r.status, 200, await r.clone().text());
    const { id } = await r.json();
    const lc = cookiePair(cookieOf(r, 'covey_login'));
    await decide(id, 'approved');
    const p = await api(`/api/auth/login-requests/${id}`, { cookie: lc });
    assert.deepEqual(await p.json(), { state: 'approved' });
    return cookiePair(cookieOf(p, 'covey_session'));
  }
  const a = await login();
  const b = await login();
  assert.equal((await api('/api/snapshot', { cookie: a })).status, 200);
  assert.equal((await api('/api/snapshot', { cookie: b })).status, 200);
  const list = (await (await api('/api/auth/sessions', { cookie: a })).json()).sessions;
  assert.equal(list.length, 2);
  assert.equal(list.filter((s) => s.current).length, 1);
  const all = await api('/api/auth/logout-all', { method: 'POST', cookie: a });
  assert.equal(all.status, 200);
  assert.equal((await api('/api/snapshot', { cookie: a })).status, 401);
  assert.equal((await api('/api/snapshot', { cookie: b })).status, 401);
});

test('页面：没构建前端时 index 是 404 说明，/api 下没有的是 JSON 404', async () => {
  const page = await fetch(`${base}/settings`);
  assert.equal(page.status, 404);
  const missing = await api('/api/nope', { cookie: '' });
  assert.equal(missing.status, 401);
});


// ---------------------------------------------------------------- 公网 HTTPS、健康检查、备份与恢复

/** 走公网 HTTPS 监听：用服务生成的自签证书做 ca */
function pub(path, { method = 'GET', headers = {}, body, cookie = '' } = {}) {
  const ca = readFileSync(join(dir, 'tls', 'cert.pem'));
  return new Promise((ok, fail) => {
    const req = https.request(
      { host: '127.0.0.1', port: pubPort, path, method, ca, headers: { 'user-agent': CHROME_MAC, ...(method !== 'GET' ? { 'x-covey-csrf': '1' } : {}), ...(cookie ? { cookie } : {}), ...headers } },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => ok({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
      },
    );
    req.on('error', fail);
    req.end(body);
  });
}

const backup = (url = base, key = KEY) => fetch(`${url}/openapi/selfhost/backup`, { headers: key ? { authorization: `Bearer ${key}` } : {} });

/** 起一个额外的服务（自己的数据目录、端口），等回环 /healthz 通 */
async function startExtra(root, env = {}) {
  const port = await takePort();
  const out = [];
  const c = spawn(process.execPath, [MAIN], {
    cwd: WEB,
    env: { ...process.env, COVEY_WEB_PORT: String(port), COVEY_WEB_DB_DIR: join(root, 'db'), COVEY_WEB_KEY_FILE: keyFile, COVEY_WEB_STATIC_DIR: join(root, 'client'), DATABASE_URL: '', NODE_ENV: 'production', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  c.stdout.on('data', (d) => out.push(String(d)));
  c.stderr.on('data', (d) => out.push(String(d)));
  extra.push(c);
  const url = `http://127.0.0.1:${port}`;
  const t0 = Date.now();
  for (;;) {
    try {
      await fetch(`${url}/healthz`);
      break;
    } catch {
      if (Date.now() - t0 > 60000) throw new Error(`额外服务没起来：${out.join('')}`);
      await new Promise((r) => setTimeout(r, 200));
    }
  }
  const stop = () =>
    new Promise((ok) => {
      if (c.exitCode !== null) return ok();
      c.once('exit', () => ok());
      c.kill('SIGTERM');
    });
  return { url, stop, out };
}

test('所有响应都带安全头（回环和公网）', async () => {
  const want = { 'x-frame-options': 'DENY', 'content-security-policy': "frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'referrer-policy': 'same-origin' };
  for (const r of [await fetch(`${base}/healthz`), await fetch(`${base}/api/me`), await fetch(`${base}/settings`), await fetch(`${base}/openapi/sync/inbox`)]) {
    for (const [k, v] of Object.entries(want)) assert.equal(r.headers.get(k), v, `${r.url} ${k}`);
    assert.equal(r.headers.get('strict-transport-security'), null);
  }
  const p = await pub('/api/me');
  for (const [k, v] of Object.entries(want)) assert.equal(p.headers[k], v, `公网 ${k}`);
});

test('回环 /healthz：不要会话；public 段有指纹、listening=true', async () => {
  const res = await fetch(`${base}/healthz`);
  assert.equal(res.status, 200);
  const h = await res.json();
  assert.equal(h.ok, true);
  assert.equal(h.pid, child.pid);
  assert.equal(h.db, process.env.SELFHOST_TEST_DATABASE_URL ? 'postgres' : 'pglite');
  assert.ok(Math.abs(new Date(h.started_at) - Date.now()) < 120000);
  const cert = new X509Certificate(readFileSync(join(dir, 'tls', 'cert.pem')));
  assert.deepEqual(h.public, {
    host: '127.0.0.1', port: pubPort, names: ['127.0.0.1'], tls: 'self-signed', fingerprint: cert.fingerprint256,
    not_after: new Date(cert.validTo).toISOString(), listening: true, error: '',
  });
  // 大小写换个写法和带斜杠一样
  assert.equal((await fetch(`${base}/HEALTHZ/`)).status, 200);
  const files = readdirSync(join(dir, 'tls')).sort();
  assert.deepEqual(files, ['cert.pem', 'key.pem']);
});

test('回环 /healthz 只给本机直连：经反代来的（带转发头）当没有', async () => {
  for (const h of [{ 'x-forwarded-for': '198.51.100.9' }, { forwarded: 'for=198.51.100.9' }, { 'x-real-ip': '198.51.100.9' }]) {
    const r = await fetch(`${base}/healthz`, { headers: h });
    assert.equal(r.status, 404, JSON.stringify(h));
  }
  assert.equal((await fetch(`${base}/healthz`)).status, 200);
});

test('公网：只有 HTTPS，明文 HTTP 连公网端口失败', async () => {
  const ok = await pub('/api/me');
  assert.equal(ok.status, 401);
  await assert.rejects(
    new Promise((res, rej) => {
      const r = http.get({ host: '127.0.0.1', port: pubPort, path: '/api/me', timeout: 3000 }, (x) => {
        x.resume();
        x.on('end', () => res(x.statusCode));
      });
      r.on('error', rej);
      r.on('timeout', () => r.destroy(new Error('timeout')));
    }),
  );
  // 不信这张证书的客户端连不上（证书是自签的，没有系统信任）
  await assert.rejects(new Promise((res, rej) => https.get({ host: '127.0.0.1', port: pubPort, path: '/' }, res).on('error', rej)), /self[- ]signed|UNABLE_TO|DEPTH_ZERO/iu);
});

test('公网：/openapi 一律 403（有密钥、不分大小写也一样），/healthz 404', async () => {
  for (const p of ['/openapi/sync/inbox', '/OPENAPI/sync/inbox', '/openapi/selfhost/backup', '/Openapi']) {
    const r = await pub(p, { headers: { authorization: `Bearer ${SECOND_KEY}` } });
    assert.equal(r.status, 403, p);
    const nokey = await pub(p);
    assert.equal(nokey.status, 403, `${p} 没密钥`);
  }
  assert.equal((await pub('/openapi/sync/upsert', { method: 'POST', headers: { authorization: `Bearer ${SECOND_KEY}`, 'content-type': 'application/json' }, body: '{"items":[]}' })).status, 403);
  for (const p of ['/healthz', '/HealthZ/']) {
    const r = await pub(p);
    assert.equal(r.status, 404, p);
    assert.doesNotMatch(r.body.toString(), /<html/iu);
  }
});

test('公网：登录 cookie 带 Secure；回环仍不带', async () => {
  const hdr = { 'x-forwarded-for': '198.51.100.77' };
  const r = await pub('/api/auth/login-requests', { method: 'POST', headers: hdr });
  assert.equal(r.status, 200, r.body.toString());
  const c = [].concat(r.headers['set-cookie'] ?? []).find((x) => x.startsWith('covey_login='));
  assert.ok(c);
  assert.match(c, /Secure/u);
  assert.match(c, /HttpOnly/u);
  const { id } = JSON.parse(r.body.toString());
  assert.equal((await decide(id, 'approved')).status < 300, true);
  const poll = await pub(`/api/auth/login-requests/${id}`, { cookie: c.split(';')[0] });
  assert.deepEqual(JSON.parse(poll.body.toString()), { state: 'approved' });
  const sess = [].concat(poll.headers['set-cookie']).find((x) => x.startsWith('covey_session='));
  assert.match(sess, /Secure/u);
  assert.match(sess, /HttpOnly/u);
  // 会话在公网照样能用
  assert.equal((await pub('/api/me', { cookie: sess.split(';')[0] })).status, 200);
});

test('备份：带密钥 200 且是 tar.gz；没密钥或错密钥 401；带转发头 403', { skip: Boolean(process.env.SELFHOST_TEST_DATABASE_URL) }, async () => {
  assert.equal((await backup(base, '')).status, 401);
  assert.equal((await backup(base, 'wrong-key')).status, 401);
  assert.equal((await fetch(`${base}/openapi/selfhost/backup`, { headers: { authorization: `Bearer ${SECOND_KEY}`, 'x-forwarded-for': '198.51.100.7' } })).status, 403);
  assert.equal((await fetch(`${base}/OPENAPI/selfhost/backup`)).status, 401);
  const ok = await backup(base, SECOND_KEY);
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get('content-type'), 'application/gzip');
  const buf = Buffer.from(await ok.arrayBuffer());
  assert.equal(buf.length, Number(ok.headers.get('content-length')));
  assert.ok(gunzipSync(buf).length > 1000);
  const list = spawnSync('tar', ['-tzf', '-'], { input: buf, encoding: 'utf8' });
  assert.equal(list.status, 0, list.stderr);
  assert.match(list.stdout, /^\/?PG_VERSION$/mu);
});

test('备份超过 COVEY_WEB_BACKUP_MAX_BYTES 是 413；.restore 没有完成标记不动它', { skip: Boolean(process.env.SELFHOST_TEST_DATABASE_URL) }, async () => {
  const root = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'selfhost-restore-'));
  try {
    mkdirSync(join(root, 'db.restore'));
    writeFileSync(join(root, 'db.restore', 'half'), 'x');
    const s = await startExtra(root, { COVEY_WEB_BACKUP_MAX_BYTES: '100' });
    const big = await backup(s.url, SECOND_KEY);
    assert.equal(big.status, 413);
    assert.equal((await big.json()).error.code, 'TOO_LARGE');
    assert.ok(existsSync(join(root, 'db.restore', 'half')), '没标记的 .restore 原样留着');
    assert.deepEqual(readdirSync(root).filter((n) => n.includes('before-restore')), []);
    assert.match(s.out.join(''), /没有 \.covey-restore-complete/u);
    await s.stop();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('恢复交换：备份解到 db.restore 写上标记，下次启动换上，原库留作 before-restore，数据是备份时的', { skip: Boolean(process.env.SELFHOST_TEST_DATABASE_URL) }, async () => {
  const root = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'selfhost-restore-'));
  try {
    const snap = Buffer.from(await (await backup(base, SECOND_KEY)).arrayBuffer());
    // 先起一次空库的服务，让 db/ 存在（里面没有 say-1）
    const first = await startExtra(root);
    const inbox0 = await (await fetch(`${first.url}/openapi/sync/inbox`, { headers: { authorization: `Bearer ${SECOND_KEY}` } })).json();
    assert.equal(inbox0.items.some((i) => i.op_id === 'say-1'), false);
    await first.stop();
    assert.ok(existsSync(join(root, 'db')));
    // 解到 db.restore（成员名带前导 /，tar 会去掉），最后写标记
    mkdirSync(join(root, 'db.restore'));
    const x = spawnSync('tar', ['-xzf', '-', '-C', join(root, 'db.restore')], { input: snap, encoding: 'utf8' });
    assert.equal(x.status, 0, x.stderr);
    writeFileSync(join(root, 'db.restore', '.covey-restore-complete'), '');
    const second = await startExtra(root);
    assert.equal(existsSync(join(root, 'db.restore')), false);
    const aside = readdirSync(root).filter((n) => /^db\.before-restore-\d{8}-\d{6}$/u.test(n));
    assert.equal(aside.length, 1, readdirSync(root).join(','));
    assert.equal(existsSync(join(root, 'db', '.covey-restore-complete')), false);
    const inbox = await (await fetch(`${second.url}/openapi/sync/inbox`, { headers: { authorization: `Bearer ${SECOND_KEY}` } })).json();
    assert.ok(inbox.items.some((i) => i.op_id === 'say-1'), '恢复后应有备份时的 say-1');
    await second.stop();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('证书准备不了：不开公网，回环照常，/healthz 的 public.error 写原因', async () => {
  const root = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'selfhost-badtls-'));
  try {
    const port = await takePort();
    const s = await startExtra(root, { COVEY_WEB_PUBLIC_NAMES: '127.0.0.1', COVEY_WEB_PUBLIC_PORT: String(port), COVEY_WEB_PUBLIC_HOST: '127.0.0.1', COVEY_WEB_TLS_CERT: join(root, 'nope-cert.pem'), COVEY_WEB_TLS_KEY: join(root, 'nope-key.pem') });
    const h = await (await fetch(`${s.url}/healthz`)).json();
    assert.equal(h.ok, true);
    assert.equal(h.public.listening, false);
    assert.equal(h.public.tls, 'provided');
    assert.match(h.public.error, /读不了证书/u);
    assert.equal((await fetch(`${s.url}/api/me`)).status, 401);
    assert.equal(await freePort(port), true, '公网端口没被占用');
    assert.match(s.out.join(''), /公网 HTTPS 没开/u);
    await s.stop();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

/** 用给定的 ca 连一次公网监听，返回对方证书的 SHA-256 指纹（大写、冒号分隔，和 /healthz 的写法一样） */
function peerFingerprint(port, ca) {
  return new Promise((ok, fail) => {
    const req = https.request({ host: '127.0.0.1', port, path: '/api/me', ca, agent: false }, (res) => {
      const fp = res.socket.getPeerCertificate().fingerprint256;
      res.resume();
      res.on('end', () => ok(fp));
    });
    req.on('error', fail);
    req.end();
  });
}

test('证书换了（自己的证书续期）：不重启，下次检查就地换上，新连接拿到新证书', async () => {
  const root = mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'selfhost-renew-'));
  const cli = join(WEB, 'dist-selfhost/server/selfhost/tls-cli.js');
  try {
    const made = ['a', 'b'].map((n, i) => {
      const done = spawnSync(process.execPath, [cli, '--dir', join(root, n), '--names', i ? '127.0.0.1,localhost' : '127.0.0.1'], { encoding: 'utf8' });
      assert.equal(done.status, 0, done.stderr);
      return JSON.parse(done.stdout.trim());
    });
    assert.notEqual(made[0].fingerprint, made[1].fingerprint);
    const own = { cert: join(root, 'own-cert.pem'), key: join(root, 'own-key.pem') };
    writeFileSync(own.cert, readFileSync(made[0].cert));
    writeFileSync(own.key, readFileSync(made[0].key), { mode: 0o600 });
    const port = await takePort();
    const s = await startExtra(root, { COVEY_WEB_PUBLIC_NAMES: '127.0.0.1', COVEY_WEB_PUBLIC_PORT: String(port), COVEY_WEB_PUBLIC_HOST: '127.0.0.1', COVEY_WEB_TLS_CERT: own.cert, COVEY_WEB_TLS_KEY: own.key, COVEY_WEB_TLS_CHECK_SECONDS: '1' });
    try {
      assert.equal(await peerFingerprint(port, readFileSync(made[0].cert)), made[0].fingerprint);
      writeFileSync(own.key, readFileSync(made[1].key), { mode: 0o600 });
      writeFileSync(own.cert, readFileSync(made[1].cert));
      let h = null;
      for (let i = 0; i < 40; i += 1) {
        h = await (await fetch(`${s.url}/healthz`)).json();
        if (h.public.fingerprint === made[1].fingerprint) break;
        await new Promise((r) => setTimeout(r, 250));
      }
      assert.equal(h.public.fingerprint, made[1].fingerprint, '健康口的指纹换成新证书的');
      assert.equal(h.public.listening, true);
      assert.equal(h.public.error, '');
      assert.equal(await peerFingerprint(port, readFileSync(made[1].cert)), made[1].fingerprint, '新连接拿到新证书');
      assert.match(s.out.join(''), /公网证书已换上新的/u);
    } finally {
      await s.stop();
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

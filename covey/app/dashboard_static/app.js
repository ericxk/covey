// The sensitive-term dashboard page (app/dashboard.py serves it and its JSON API).
// Every value from the server goes in through textContent or .value, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { rows: [], dirty: false, loaded: false };

function show(id, on) { $(id).hidden = !on; }

function say(id, text) {
  $(id).textContent = text || "";
  show(id, Boolean(text));
}

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: { "X-Dashboard": "1" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    return { status: 0, data: { error: "连不上看板：" + err.message } };
  }
  let data = {};
  try { data = await response.json(); } catch (err) { data = { error: "看板的回答读不了" }; }
  if (response.status === 401 && path !== "/api/login") showLogin();
  return { status: response.status, data };
}

function showLogin() {
  show("app", false);
  show("login", true);
  $("login-token").focus();
}

function el(tag, attrs, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function when(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit",
                                                       hour: "2-digit", minute: "2-digit" });
}

function markDirty() {
  state.dirty = true;
  show("dirty", true);
  say("save-ok", "");
}

// The term table.

function renderTerms() {
  const box = $("terms");
  box.replaceChildren();
  state.rows.forEach((row, index) => box.appendChild(termRow(row, index)));
  show("terms-empty", state.rows.length === 0);
  $("st-terms").textContent = String(state.rows.filter((r) => r.term.trim()).length);
}

function termRow(row, index) {
  const line = el("div", { class: "term" });
  const term = el("input", { type: "text", "aria-label": "原词", placeholder: "原词", spellcheck: "false" });
  term.value = row.term;
  term.addEventListener("input", () => { row.term = term.value; row.saved = false; markDirty(); });
  const standIn = el("input", { type: "text", "aria-label": "替身词", placeholder: "留空自动生成", spellcheck: "false" });
  standIn.value = row.stand_in;
  if (!row.stand_in && row.effective && row.saved) standIn.placeholder = "自动：" + row.effective;
  standIn.addEventListener("input", () => { row.stand_in = standIn.value; row.saved = false; markDirty(); });
  const match = el("select", { "aria-label": "匹配" });
  match.append(el("option", { value: "word" }, "整词"), el("option", { value: "anywhere" }, "任意位置"));
  match.value = row.anywhere ? "anywhere" : "word";
  match.addEventListener("change", () => { row.anywhere = match.value === "anywhere"; markDirty(); });
  const tag = row.saved && row.restore ? row.restore : "—";
  const restore = el("span", { class: "tag " + ({ "还原": "tag-yes", "共用·不还原": "tag-shared", "不还原": "tag-no" }[tag] || "") }, tag);
  restore.title = "还原：回复里的替身词换回原词";
  const hits = el("span", { class: "hits", title: "近期记录里的命中次数" }, row.saved ? String(row.hits || 0) : "—");
  const remove = el("button", { type: "button", class: "remove", "aria-label": "删除这一行" }, "删除");
  remove.addEventListener("click", () => { state.rows.splice(index, 1); markDirty(); renderTerms(); });
  line.append(cell("原词", term), cell("替身词", standIn), cell("匹配", match), cell("还原", restore),
              cell("命中", hits), remove);
  return line;
}

function cell(label, child) {
  const box = el("div", { class: "cell" });
  box.append(el("span", { class: "cell-label" }, label), child);
  return box;
}

function blankRow() {
  return { term: "", stand_in: "", anywhere: false, saved: false };
}

function bulkAdd() {
  const fold = (s) => ($("ignore-case").checked ? s.toLowerCase() : s);
  let added = 0;
  for (const raw of $("bulk-text").value.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=>");
    const term = (at >= 0 ? line.slice(0, at) : line).trim();
    const standIn = at >= 0 ? line.slice(at + 2).trim() : "";
    if (!term) continue;
    const same = state.rows.find((r) => fold(r.term.trim()) === fold(term));
    if (same) {
      if (standIn) { same.stand_in = standIn; same.saved = false; }
    } else {
      state.rows.push({ ...blankRow(), term, stand_in: standIn });
    }
    added += 1;
  }
  if (added) {
    $("bulk-text").value = "";
    markDirty();
    renderTerms();
  }
}

// The config.

function currentConfig() {
  return {
    default_on: $("default-on").checked,
    ignore_case: $("ignore-case").checked,
    restore: $("restore").checked,
    upstream: $("upstream").value.trim(),
    terms: state.rows.filter((r) => r.term.trim() || r.stand_in.trim())
      .map((r) => ({ term: r.term.trim(), stand_in: r.stand_in.trim(), anywhere: Boolean(r.anywhere) })),
  };
}

function applyConfig(data) {
  if (data.error) {
    $("st-config").textContent = "读取失败";
    say("config-error", data.error + "。下面是空白的默认设置，保存会覆盖原来的文件。");
    data = { config: { default_on: false, ignore_case: true, restore: true, upstream: "" }, terms: [] };
  } else {
    $("st-config").textContent = "正常";
    say("config-error", "");
    $("st-updated").textContent = data.updated_at ? when(Date.parse(data.updated_at) / 1000) : "未保存过";
    $("st-week").textContent = String(data.week_hits || 0) + " 次";
  }
  const config = data.config;
  $("default-on").checked = config.default_on;
  $("ignore-case").checked = config.ignore_case;
  $("restore").checked = config.restore;
  $("upstream").value = config.upstream || "";
  state.rows = data.terms.map((t) => ({ ...t, saved: true }));
  state.dirty = false;
  show("dirty", false);
  renderTerms();
}

async function loadConfig() {
  const { status, data } = await api("GET", "/api/config");
  if (status === 401) return false;
  if (status !== 200) {
    say("config-error", data.error || "读取失败");
    return true;
  }
  applyConfig(data);
  return true;
}

async function save() {
  say("save-error", "");
  say("save-ok", "");
  $("save").disabled = true;
  const { status, data } = await api("PUT", "/api/config", currentConfig());
  $("save").disabled = false;
  if (status !== 200) {
    say("save-error", data.error || "保存失败");
    return;
  }
  applyConfig(data);
  say("save-ok", "已保存，下一轮生效。");
  loadStats();
}

async function detect() {
  const button = $("detect");
  button.disabled = true;
  say("detect-result", "探测中……个人网关可能要先建隧道，最长一分多钟。");
  const { status, data } = await api("POST", "/api/upstream/detect", {});
  button.disabled = false;
  const box = $("detect-result");
  box.replaceChildren();
  if (status !== 200) {
    box.textContent = data.error || "探测失败";
    return;
  }
  box.append(document.createTextNode("探测到 " + data.upstream + "（" + data.source + "） "));
  const use = el("button", { type: "button", class: "small" }, "填入");
  use.addEventListener("click", () => { $("upstream").value = data.upstream; markDirty(); });
  box.appendChild(use);
}

async function preview() {
  say("preview-error", "");
  const { status, data } = await api("POST", "/api/preview", { text: $("preview-text").value, config: currentConfig() });
  if (status !== 200) {
    say("preview-error", data.error || "没试成");
    show("preview-out", false);
    return;
  }
  $("preview-redacted").textContent = data.redacted;
  $("preview-restored").textContent = data.restored;
  const hits = Object.entries(data.hits);
  $("preview-hits").textContent = hits.length
    ? "替换了：" + hits.map(([term, n]) => term + " × " + n).join("，")
      + (data.restored === $("preview-text").value ? "；还原后和原文一样。" : "；还原后和原文不同（共用或不还原的替身词留着）。")
    : "没有命中词表里的词。";
  show("preview-out", true);
}

// The counts.

function topicName(item) {
  const number = item.number ? "T" + item.number : (item.topic_id || "").slice(0, 8);
  return item.title ? number + " · " + item.title : number;
}

async function loadStats() {
  const { status, data } = await api("GET", "/api/stats");
  if (status !== 200) return;
  $("st-week").textContent = String(data.week_hits || 0) + " 次";
  const topics = $("topics");
  topics.replaceChildren();
  for (const t of data.topics) {
    const item = el("li");
    item.append(el("strong", {}, topicName(t)),
                el("span", { class: "hint" }, " " + (t.workspace ? t.workspace + " · " : "") + t.turns + " 轮 · 替换 "
                   + t.hits + " 处 · 最近 " + when(t.last)));
    topics.appendChild(item);
  }
  if (!data.topics.length) topics.appendChild(el("li", { class: "hint" }, "还没有记录。"));
  const body = $("turns").querySelector("tbody");
  body.replaceChildren();
  for (const turn of data.turns) {
    const row = el("tr");
    row.append(el("td", {}, when(turn.ts)), el("td", {}, topicName(turn)),
               el("td", {}, [turn.harness, turn.model].filter(Boolean).join(" · ")),
               el("td", { class: "num" }, String(turn.requests || 0)), el("td", { class: "num" }, String(turn.replaced)));
    body.appendChild(row);
  }
  if (!data.turns.length) {
    const row = el("tr");
    row.appendChild(el("td", { colspan: "5", class: "hint" }, "还没有开着脱敏的一轮。"));
    body.appendChild(row);
  }
}

async function start() {
  if (!(await loadConfig())) return;
  show("login", false);
  show("app", true);
  state.loaded = true;
  loadStats();
}

async function login(event) {
  event.preventDefault();
  say("login-error", "");
  const { status, data } = await api("POST", "/api/login", { token: $("login-token").value.trim() });
  if (status !== 200) {
    say("login-error", data.error || "登录失败");
    return;
  }
  $("login-token").value = "";
  start();
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", login);
  $("add-row").addEventListener("click", () => { state.rows.push(blankRow()); markDirty(); renderTerms(); });
  $("toggle-bulk").addEventListener("click", () => show("bulk", $("bulk").hidden));
  $("bulk-add").addEventListener("click", bulkAdd);
  $("save").addEventListener("click", save);
  $("detect").addEventListener("click", detect);
  $("preview").addEventListener("click", preview);
  for (const id of ["default-on", "ignore-case", "restore"]) $(id).addEventListener("change", markDirty);
  $("upstream").addEventListener("input", markDirty);
  window.addEventListener("beforeunload", (event) => { if (state.dirty) event.preventDefault(); });
  start();
});

"""The web workbench screenshots in README.md and README.en.md (docs/media/screenshots/10-13): the self-hosted page
(web/, built by web/deploy/selfhost/build.sh) started on 127.0.0.1 with a throwaway database, filled with demo data
through Covey's own sync (WebSync.push_once over a state directory that holds only made-up records), logged in the way
the page logs in (a login request, the owner's approval pushed as Covey pushes it), and shot by a headless Chromium
(docs/testing.md 网页截图).

    bash web/deploy/selfhost/build.sh /absolute/covey-web                       # once: the self-hosted build
    <python with Playwright> -B scripts/web_shots.py --build /absolute/covey-web --out /absolute/shots [--browser /absolute/headless_shell]
    <python with Pillow> -B scripts/web_shots.py --out /absolute/shots --install   # the four README ones into docs/media/screenshots

Every page (看板, 专项, 大厅, a topic) at a desktop and a phone size, dark and light: web-<page>-<size>-<theme>.png.
Three more, asked by name (``--pages assist,tidy,memory``; their demo data is pushed only then, so the README's four stay as
they are): 设置's 辅助模型 with its 后台活最近一次 (``assist``, the section alone), the 看板's 整理专项 after a 整理 that failed
(``tidy``) and the one-page 记忆 (``memory``: 待审核 on top, 全局, three projects, a topic's rule; ``demo_memory``). ``--variants`` shoots the samples of docs/ui.md 第 12 节 too (VARIANTS: A is the page as built, B and C are drawn
over it in the browser only, never in the page's code): web-<page>-<variant>-<size>-<theme>.png.
Only demo data: a demo-app story (a topic waiting on a question, one running, one stopped, one with an unseen result, a
project with its lead and memory, the butler), no real name, chat, open_id or path. The server listens on 127.0.0.1
only and is stopped, its whole process group, before the script ends.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SHOTS_DIR = REPO / "docs" / "media" / "screenshots"
CHAT = "oc_demo_owner"
PID = "d3m0a1b2c3d4"
SWITCHES = {"COVEY_PROJECTS": "1", "COVEY_REVIEW": "1", "COVEY_REVISIT": "1", "COVEY_MEMORY": "1",
            "COVEY_KNOWLEDGE": "1", "COVEY_PROJECT_MEMORY": "1"}
TITLES = {"claude": "Claude Code", "codex": "Codex"}  # The Harnesses the demo routes, as the Bridge's apps name them.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
PAGES = {"board": "/", "project": f"/projects/{PID}", "hall": "/butler", "topic": "/topics/" + "7" * 32}
README_PAGES = ("board", "project", "hall", "topic")  # The default: the README's.
EXTRA = {"assist": "/settings/config", "tidy": "/", "memory": "/memory"}  # Shot only when named (samples, the one-page 记忆).
FOCUS = {"assist": ".st-assist", "tidy": ".td-panel"}  # What of the page is shot (assist: that element alone; tidy: scrolled to).
# The samples (docs/ui.md 第 12 节) drawn over the built page in the browser: (sizes, CSS, script). A is the page itself.
SAMPLE_CSS = {
    "assist-B": ".cv .st-sample-b summary{display:flex;align-items:center;gap:6px;margin:14px 2px 6px;font-size:13px;font-weight:600;"
                "color:var(--t2);cursor:pointer;list-style:none}.cv .st-sample-b summary em{font-style:normal;font-weight:400;"
                "color:var(--fail)}",
    "assist-C": ".cv .st-rec{border:0;background:none;display:flex;flex-direction:column;gap:8px;overflow:visible}"
                ".cv .st-rec .st-rec-h{display:none}.cv .st-rec .st-rec-r{border:1px solid var(--line);border-radius:12px;"
                "background:var(--s1)}.cv .st-rec .st-rec-r.bad{border-color:color-mix(in srgb,var(--fail) 45%,var(--line))}",
    "tidy-B": ".cv .td-sample-banner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 14px;padding:10px 14px;"
              "border-radius:10px;background:color-mix(in srgb,var(--st-stopped) 14%,var(--s1));color:var(--st-stopped);"
              "font-size:13.5px;line-height:1.5}.cv .td-sample-banner span{flex:1 1 240px}",
}
SAMPLE_JS = {
    "assist-B": """() => { const sec = document.querySelector('.st-assist'); const head = sec.querySelector('.st-h4');
        const body = sec.querySelector('.st-rec'); const rows = body ? body.querySelectorAll('.st-rec-r:not(.st-rec-h)') : [];
        const bad = body ? body.querySelectorAll('.st-rec-r.bad').length : 0;
        const box = document.createElement('details'); box.className = 'st-sample-b';
        const sum = document.createElement('summary');
        sum.innerHTML = '后台活最近一次 · ' + rows.length + ' 项' + (bad ? ' <em>' + bad + ' 项没成</em>' : '') + ' ▸';
        box.append(sum); if (body) box.append(body); head.replaceWith(box); }""",
    "tidy-B": """() => { const why = document.querySelector('.td-fail-w'); const panel = document.querySelector('.td-panel');
        const scroll = document.querySelector('.nb-scroll'); if (!why || !panel || !scroll) return;
        const bar = document.createElement('div'); bar.className = 'td-sample-banner';
        const words = document.createElement('span'); words.textContent = '整理专项 · ' + why.textContent;
        const go = document.createElement('a'); go.className = 'btn sm'; go.textContent = '去设置'; go.href = '/settings/config';
        const ok = document.createElement('button'); ok.className = 'btn sm txt'; ok.textContent = '知道了';
        bar.append(words, go, ok); panel.style.display = 'none'; scroll.prepend(bar); scroll.scrollTop = 0; }""",
}
VARIANTS = {"assist": {"B": ("desktop", "phone"), "C": ("phone",)}, "tidy": {"B": ("desktop", "phone")}}
SIZES = {"desktop": dict(viewport={"width": 1440, "height": 900}, device_scale_factor=2),
         "phone": dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)}
# The README's four: shot -> (its name in docs/media/screenshots, width in pixels).
README = {"web-board-desktop-dark.png": ("10-web-board.png", 1600), "web-project-desktop-light.png": ("11-web-project.png", 1600),
          "web-hall-desktop-dark.png": ("12-web-hall.png", 1600), "web-board-phone-dark.png": ("13-web-phone.png", 585)}


def demo_state(state, now):
    """Write the demo's records into the empty state directory ``state``: Covey's own writers, made-up words only."""
    sys.path[:0] = [str(REPO), str(REPO / "tests")]
    from support.records import write_job, write_topic
    from covey.core import butler, project_memory, projects
    from covey.core import butler_settings as settings
    from covey.core.butler import butler_topic_id
    from covey.core.history_storage import append_history
    from covey.core.state import write_json
    from covey.core.topic_seen import record_reads
    os.environ.update(SWITCHES)  # After the imports: the tests' support package clears COVEY_* for its own isolation.

    butler.configure_state(state)
    write_json(state / settings.FILE, {"mode": "agent"})
    butler.forget_mode()

    def history(topic, rows):
        for i, (role, text) in enumerate(rows):
            append_history(state / "topics" / topic / "history.jsonl", role, text, {"message_id": f"om_{topic[:6]}_{i}"})

    def iso(seconds):  # As job records keep a time (core/state.py epoch_seconds reads it).
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(seconds))

    owner = butler_topic_id(CHAT)
    write_topic(state, topic_id=owner, chat_id=CHAT, harness_id="claude", main_thread=True, workspace_name="demo-app",
                workdir="/absolute/demo-app")
    write_job(state, "k-butler", topic_id=owner, chat_id=CHAT, job_id=31, status="done", model="claude-opus-5-5",
              harness_id="claude", message_id="om_butler_1", started_at=now - 1500, finished_at=now - 1480,
              input_tokens=42_000, output_tokens=1_200, cache_read_tokens=38_000)
    history(owner, [("user", "今天把 demo-app 的登录页做掉，顺便给订单接口补集成测试"),
                    ("assistant", "开了两件：T7 做登录页，T8 补订单接口的集成测试。T5 停在模型额度上，等你决定换不换模型。")])

    projects.create(state, projects.new_record(chat_id=CHAT, name="demo-app 账号体系",
                                               goal="注册、登录、找回密码都上线，并有测试覆盖", project_id=PID, now=now - 86400))
    lead = projects.lead_topic_id(CHAT, PID)
    write_topic(state, topic_id=lead, chat_id=CHAT, harness_id="claude", project=projects.project_field(PID, projects.LEAD))
    write_job(state, "k-lead", topic_id=lead, chat_id=CHAT, job_id=41, status="done", model="claude-opus-5-5",
              harness_id="claude", message_id="om_lead_1", started_at=now - 900, finished_at=now - 870,
              input_tokens=64_000, output_tokens=2_100, cache_read_tokens=55_000)
    history(lead, [("user", "这个专项先把登录页做出来"),
                   ("assistant", "目标检查表 4 条：注册、登录、找回密码、测试覆盖。登录页交给 T7；找回密码的邮件模板 T9 已做完，"
                                 "等登录页合进去再一起验。")])

    def topic(tid, number, title, *, member=False, status, rows, harness="claude", model="claude-opus-5-5",
              tokens=(180_000, 6_000), **job):
        write_topic(state, {"topic_id": tid}, topic_id=tid, chat_id=CHAT, harness_id=harness, topic_number=number,
                    origin={"title": title}, workspace_name="demo-app",
                    **({"project": projects.project_field(PID, projects.MEMBER)} if member else {}))
        if member:
            projects.add_member(state, PID, tid)
        write_job(state, f"k-{tid[:6]}", topic_id=tid, chat_id=CHAT, job_id=number * 10, status=status, harness_id=harness,
                  model=model, message_id=f"om_{tid[:6]}", card_message_id=f"om_card_{tid[:6]}",
                  created_at=iso(job.get("started_at", now)), input_tokens=tokens[0], output_tokens=tokens[1],
                  cache_read_tokens=int(tokens[0] * 0.85), **job)
        history(tid, rows)

    t7, t8, t5, t6, t9, t4 = ("7" * 32, "8" * 32, "5" * 32, "6" * 32, "9" * 32, "4" * 32)
    topic(t7, 7, "给 demo-app 加登录页", member=True, status="waiting_user", started_at=now - 420,
          rows=[("user", "给 demo-app 加个登录页，邮箱加密码就行"),
                ("assistant", "登录表单和校验写好了，测试也过了。登录成功以后跳到哪里，需要你定一下。")])
    write_json(state / "questions" / "tokdemo0007.json", {
        "token": "tokdemo0007", "chat_id": CHAT, "topic_id": t7, "status": "pending", "created_at": now - 180,
        "job_id": 70, "blocking": True, "question_message_id": "om_q7",
        "questions": [{"id": "after", "header": "登录后", "question": "登录成功以后跳到哪里？",
                       "options": [{"label": "首页（推荐）", "description": "和现在注册成功后一样；选另一个要多记一个跳转参数"},
                                   {"label": "登录前打开的页面", "description": "用户少点一次，但要处理跳转参数"}]}]})
    topic(t8, 8, "给订单接口补集成测试", status="running", started_at=now - 300, harness="codex", model="gpt-6",
          tokens=(240_000, 9_000), rows=[("user", "给订单接口补集成测试，覆盖下单、取消和退款"),
                                        ("assistant", "下单和取消的用例写好了，正在跑全量测试。")])
    topic(t5, 5, "修登录接口的报错", member=True, status="failed", execution_status="failed", failure_code="quota_exhausted",
          started_at=now - 2400, finished_at=now - 2300,
          rows=[("user", "登录接口偶尔报 500，查一下"),
                ("assistant", "复现了：并发登录时会话表有竞争。改到一半，模型服务的额度用完了。")])
    topic(t6, 6, "升级依赖并跑回归", status="done", started_at=now - 3600, finished_at=now - 2500, tokens=(520_000, 14_000),
          rows=[("user", "把依赖升到最新的小版本，跑一遍回归"),
                ("assistant", "**结论**：依赖升级好了，回归全过。\n\n- 升了 12 个包，都是小版本\n- 回归 214 个用例全过")])
    topic(t9, 9, "找回密码的邮件模板", member=True, status="done", started_at=now - 7200, finished_at=now - 6600,
          tokens=(150_000, 8_000), rows=[("user", "写找回密码的邮件模板，中英文各一份"),
                                        ("assistant", "**结论**：两份模板写好了，预览图在产物里。")])
    topic(t4, 4, "整理 README 的安装步骤", status="done", started_at=now - 90000, finished_at=now - 89000,
          rows=[("user", "README 的安装步骤太长了，整理一下"), ("assistant", "**结论**：安装步骤缩成三步，旧的细节挪进了安装文档。")])

    # The project's memory: two lines the owner said (one an answer), one the lead summed up.
    project_memory.add(state, PID, "密码至少 8 位，登录失败 5 次锁 10 分钟", kind="constraint", source="owner", now=now - 80000,
                       origin={"message_id": "om_demo_rule"})
    project_memory.add(state, PID, "登录成功后默认跳首页", kind="decision", source="owner_answer", now=now - 3000,
                       origin={"question": "tokdemo0006"})
    project_memory.add(state, PID, "邮件模板中英文各一份，放在 templates/mail/", kind="fact", source="lead", now=now - 6500)
    record_reads(state, {t9: now - 6000, t4: now - 88000}, now=now)  # Two results read already; T6's not.


def demo_memory(state, now):
    """The one-page 记忆's demo (``--pages memory``): rules of 全局 (yours, one the learner summed up, one put away), a note,
    the project's (the owner's and the lead's) and two more projects', a topic's rule and the two kinds waiting in 待审核. Made-up
    words only, through core/memstore.py."""
    from covey.core import memstore, projects
    t7 = "7" * 32
    for text, title, said, by in (("回复先给结论，再展开原因", "回复先给结论", "owner", "owner"),
                                  ("改完代码先跑相关测试，过了再合并", "改完先跑测试", "owner", "owner"),
                                  ("写文档只写实际有的数据，不写估出来的数", "文档只写实际数据", "owner", "owner"),
                                  ("汇报里不贴整段日志，只贴关键几行", "汇报不贴整段日志", "summary", "learn")):
        memstore.add(state, kind="rule", scope="global", text=text, title=title, said=said, by=by, now=now - 50000,
                     origin={"topic_key": t7, "topic_number": 7, "quote": text})
    gone = memstore.add(state, kind="rule", scope="global", text="周五下午不发版", title="周五下午不发版", now=now - 90000)
    memstore.archive(state, gone.ref, now=now - 80000)
    memstore.add(state, kind="note", scope="global", text="演示环境的网页服务监听在 8080 端口", title="演示环境端口", said="summary",
                 by="learn", now=now - 40000)
    memstore.add(state, kind="rule", scope="project", owner_key=PID, text="发版前先在预发环境跑一遍冒烟", title="发版前跑冒烟",
                 said="summary", by="lead", now=now - 30000)
    for n, (name, text) in enumerate((("demo-app 发布流程", "发布单要写清回滚步骤"), ("demo-app 数据看板", "看板数字以日报口径为准"))):
        pid = f"d3m0e{n}f1a2b3"
        projects.create(state, projects.new_record(chat_id=CHAT, name=name, project_id=pid, now=now - 70000 - n * 9000))
        memstore.add(state, kind="rule", scope="project", owner_key=pid, text=text, now=now - 60000 - n * 9000)
    memstore.add(state, kind="rule", scope="topic", owner_key=t7, text="这个话题只改登录页，不动注册页", title="只改登录页",
                 now=now - 5000)
    # The two kinds of 待审核: a rule held where it was said that may be 全局, and one that grants (in force once accepted).
    held = memstore.add(state, kind="rule", scope="topic", owner_key=t7, text="以后回复都用中文，会话再长也不换成英文",
                        title="以后回复都用中文", now=now - 2000, origin={"topic_key": t7, "topic_number": 7, "quote": "以后回复都用中文吧"})
    memstore.review_put(state, {"type": "learn", "title": "以后回复都用中文", "text": held.text, "items": [held.ref],
                                "quote": "以后回复都用中文吧", "origin": {"topic_key": t7, "topic_number": 7},
                                "suggest": {"kind": "rule", "scope": "global"}}, now=now - 2000)
    memstore.review_put(state, {"type": "learn", "title": "改完直接部署到预发", "text": "演示专项里改完直接部署到预发环境，不用再问",
                                "quote": "以后改完直接部署到预发", "origin": {"topic_key": t7, "topic_number": 7},
                                "suggest": {"kind": "rule", "scope": "project", "project_key": PID, "grant": "deploy"}}, now=now - 1500)


def demo_assist(state, now):
    """辅助模型's demo (T884): the 设置 item's 配置 with its 辅助模型 named Codex, the background calls' latest outcomes
    (core/assist.py, as the calls leave them: 整理专项 refused a login on Codex, 分流 answered by Claude Code) and the 整理专项 item
    after that 整理 failed. Made-up words only; returns the two ``settings`` items."""
    from covey.app import assist, web_settings
    from covey.cards import butler_panel as cards
    from covey.core import assist as records
    from covey.core import web_sync
    for feature, harness, model, code, ms, ago, manual in (
            ("track", "claude", "haiku", "", 1400, 40, False), ("tidy", "codex", "gpt-6-astra", "authentication_error", 2100, 120, True),
            ("backfill", "codex", "gpt-6-astra", "", 7800, 180, False), ("look", "codex", "gpt-6-luna", "", 5200, 900, True),
            ("candidates", "codex", "gpt-6-luna", "timeout", 30000, 7200, False)):
        records.settle(state, feature=feature, harness=harness, model=model, code=code, ms=ms, manual=manual, now=now - ago)
    row = cards.assist_setting({"options": [(cards.ASSIST_AUTO, cards.ASSIST_WORDS[cards.ASSIST_AUTO]), ("claude", "Claude Code"),
                                            ("codex", "Codex"), ("agy", "Antigravity"),
                                            (cards.ASSIST_NONE, cards.ASSIST_WORDS[cards.ASSIST_NONE])], "current": "codex"})
    harness = {"key": "t_harness", "kind": cards.TOPIC_HARNESS, "label": "Harness", "current": "claude", "text": "Claude Code",
               "options": [{"value": "claude", "text": "Claude Code"}, {"value": "codex", "text": "Codex"}], "editable": True,
               "line": 1}
    config = {"summary": "新话题 Claude Code · 管家 Opus 5.5", "topic": {"title": "新话题", "rows": [harness], "note": cards.TOPIC_NOTE},
              "assist": {"title": cards.SECTIONS[cards.ASSIST_PART], "note": cards.ASSIST_NOTE,
                         "words": "现在：轻的判断用 Codex · gpt-6-luna，整理用 Codex · gpt-6-astra。",
                         "rows": web_settings.rows_of([row]), "recent": assist.recent(state)},
              "more": [], "more_note": "", "locked": ""}
    settings = {"tabs": [{"key": "config", "label": "配置"}], "feishu": "", "ops": list(web_sync.KINDS), "config": config}
    tidy = {"proposals": [], "running": None, "updated_at": now,
            "last_run": {"run": "demo-run", "state": "failed", "why": "Codex 登录失效或没有权限", "why_code": "authentication_error",
                         "via": "codex", "model": "gpt-6-astra", "retry": False, "origin": "web", "at": now - 120,
                         "progress": {"done": 0, "total": 0, "current": ""}}}
    return [web_sync.item("settings", web_sync.SETTINGS_KEY, settings), web_sync.item("settings", web_sync.TIDY_KEY, tidy)]


def push(state, base, key_file, now, extra=()):
    """Push the demo with Covey's own sync (two passes: what the first look learned, the lead's project, goes too), and
    本周用量 as app/web_settings.py counts it (the Bridge pushes it with its settings, which this demo has none of)."""
    from covey.app import weekly_usage
    from covey.app.web_sync import WebSync
    from covey.core import http_upstream, web_sync
    from covey.core.jobs import select_job_records
    from covey.core.state import write_json
    config = state / "web.json"
    write_json(config, {"base_url": base + "/openapi", "key_file": str(key_file)})
    os.environ.update({web_sync.ENV: "1", web_sync.CONFIG_ENV: str(config)})
    runtime = SimpleNamespace(log=lambda *words, **_: None, shutdown=threading.Event(), owner_open_id="ou_demo_owner",
                              access=SimpleNamespace(owner_chat=lambda: CHAT),
                              jobs=SimpleNamespace(topic_jobs=lambda topic, chat: []))
    harnesses = {name: SimpleNamespace(spec=SimpleNamespace(title=title)) for name, title in TITLES.items()}
    env = SimpleNamespace(state_dir=state, runtime=runtime, topics=SimpleNamespace(target=lambda *_: None, harnesses=harnesses))
    sync = WebSync(runtime, env)
    sync.client.opener = http_upstream.opener(direct=True)
    pushed = sync.push_once(now)
    time.sleep(1.5)
    pushed += sync.push_once(time.time())
    since = now - weekly_usage.WINDOW_DAYS * 86400
    found = weekly_usage.summarize(select_job_records(state, lambda r: (weekly_usage.created_at(r) or 0) >= since), now, TITLES)
    usage = {**found, "outcomes": dict(found["outcomes"]), "ended": dict(found["ended"]), "at": now,
             "history": weekly_usage.history(state, now, TITLES)}
    sync.client.request("POST", "/sync/upsert", {"items": [web_sync.item("settings", web_sync.USAGE_KEY, usage), *extra],
                                                 "deleted": []})
    return pushed


def call(base, path, *, method="GET", body=None, headers=None):
    request = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"user-agent": UA, **({"content-type": "application/json"} if body is not None else {}),
                                              **(headers or {})})
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30)


def cookie_of(response, name):
    return next(value.split(";")[0] for key, value in response.headers.items()
                if key.lower() == "set-cookie" and value.startswith(name + "="))


def login(base, key):
    """The page's session cookie (name, value): a login request, then the owner's approval pushed as Covey pushes it."""
    response = call(base, "/api/auth/login-requests", method="POST", headers={"x-covey-csrf": "1"})
    request = json.loads(response.read())
    waiting = cookie_of(response, "covey_login")
    call(base, "/openapi/sync/upsert", method="POST", headers={"authorization": f"Bearer {key}"},
         body={"items": [{"type": "login", "key": request["id"], "rev": "d-approved",
                          "data": {"decision": "approved", "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}]})
    name, _, value = cookie_of(call(base, f"/api/auth/login-requests/{request['id']}", headers={"cookie": waiting}),
                               "covey_session").partition("=")
    return name, value


def shoot(base, cookie, out, browser, pages, variants=False):
    """Each page at each size and theme; the T884 pages (EXTRA) as their FOCUS shows them, and with ``variants`` their
    samples (VARIANTS) too."""
    from playwright.sync_api import sync_playwright
    paths = {**PAGES, **EXTRA}
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(**({"executable_path": browser} if browser else {}))
        for size, options in SIZES.items():
            for theme in ("dark", "light"):
                context = chromium.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", **options)
                context.add_cookies([{"name": cookie[0], "value": cookie[1], "url": base}])
                context.add_init_script(f"try {{ localStorage.setItem('covey.theme', '{theme}'); }} catch (e) {{}}")
                page = context.new_page()
                for name in pages:
                    looks = [("A", None)] + ([(v, v) for v, sizes in VARIANTS.get(name, {}).items() if size in sizes]
                                             if variants else [])
                    for look, sample in looks:
                        page.goto(base + paths[name], wait_until="networkidle")
                        page.wait_for_timeout(1500)
                        if sample:
                            key = f"{name}-{sample}"
                            if key in SAMPLE_CSS:
                                page.add_style_tag(content=SAMPLE_CSS[key])
                            if key in SAMPLE_JS:
                                page.evaluate(SAMPLE_JS[key])
                            page.wait_for_timeout(300)
                        tag = f"-{look}" if name in EXTRA else ""
                        path = out / f"web-{name}{tag}-{size}-{theme}.png"
                        focus = page.locator(FOCUS[name]).first if name in FOCUS else None
                        if name == "assist" and focus is not None and focus.count():
                            focus.screenshot(path=str(path))
                        else:
                            if focus is not None and focus.count() and focus.is_visible():
                                focus.scroll_into_view_if_needed()
                            page.screenshot(path=str(path))
                        print(path)
                context.close()
        chromium.close()


def install(out):
    """The README's four (README) into docs/media/screenshots: scaled down, 256 colours by octree (median cut merges the
    page's orange and red), each well under 100 KB."""
    from PIL import Image
    for shot, (name, width) in README.items():
        image = Image.open(out / shot).convert("RGB")
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
        image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, kmeans=3, dither=Image.Dither.NONE).save(
            SHOTS_DIR / name, optimize=True)
        print(SHOTS_DIR / name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="where the shots go (and where --install takes them from)")
    parser.add_argument("--build", help="the self-hosted build (web/deploy/selfhost/build.sh's directory, with dist-selfhost/)")
    parser.add_argument("--browser", default="", help="a Chromium or headless_shell when Playwright has none of its own")
    parser.add_argument("--port", type=int, default=18741, help="the page's port on 127.0.0.1 (default 18741)")
    parser.add_argument("--pages", default=",".join(README_PAGES),
                        help=f"which of {', '.join([*PAGES, *EXTRA])} (default the README's: {', '.join(README_PAGES)})")
    parser.add_argument("--variants", action="store_true", help="the samples of assist and tidy too (VARIANTS)")
    parser.add_argument("--install", action="store_true", help="put the README's four into docs/media/screenshots (needs Pillow)")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if args.install:
        install(out)
        return 0
    if not args.build:
        parser.error("--build is needed to shoot")
    main_js = Path(args.build).expanduser().resolve() / "dist-selfhost" / "server" / "selfhost" / "main.js"
    if not main_js.is_file():
        parser.error(f"no {main_js}: build it first (bash web/deploy/selfhost/build.sh <dir>)")
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="covey-web-shots-") as scratch:
        scratch = Path(scratch)
        key_file = scratch / "key"
        key_file.write_text("demo-key-not-real-" + os.urandom(12).hex() + "\n")
        key_file.chmod(0o600)
        env = {**os.environ, "COVEY_WEB_PORT": str(args.port), "COVEY_WEB_DB_DIR": str(scratch / "db"),
               "COVEY_WEB_KEY_FILE": str(key_file), "COVEY_WEB_STATIC_DIR": str(main_js.parents[2] / "client"),
               "COVEY_WEB_TLS_DIR": str(scratch / "tls"), "DATABASE_URL": "", "NODE_ENV": "production"}
        with open(scratch / "server.log", "wb") as log:
            server = subprocess.Popen(["node", str(main_js)], cwd=str(main_js.parents[3]), env=env, stdin=subprocess.DEVNULL,
                                      stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for _ in range(60):
                try:
                    call(base, "/healthz").read()
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise SystemExit("the page did not start: " + (scratch / "server.log").read_text()[-2000:])
            state = scratch / "state"
            state.mkdir()
            now = time.time()
            demo_state(state, now)
            pages = [name for name in args.pages.split(",") if name]
            extra = demo_assist(state, now) if any(name in EXTRA for name in pages) else ()
            if "memory" in pages:
                demo_memory(state, now)
            print("pushed", push(state, base, key_file, now, extra), "items")
            shoot(base, login(base, key_file.read_text().strip()), out, args.browser, pages, args.variants)
        finally:
            os.killpg(server.pid, signal.SIGTERM)  # Its whole process group (start_new_session).
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

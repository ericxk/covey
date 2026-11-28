# scripts/

这里的脚本分两类。除特别说明外，都在仓库根目录运行。

## 使用者用

装机、升级和日常运维会用到的。

| 文件 | 用途 |
| --- | --- |
| `bootstrap_harness_bridge.sh` | 把服务注册到本机的服务管理器并启动；`./install` 和 `deploy_bridge.sh` 最后一步都调它（[运维](../docs/operations.md)） |
| `install_console_browser.sh` | 装 Covey 自动补齐飞书应用要用的无头浏览器：数据根下的 Python 虚拟环境加 Playwright 和 Chromium，并真启动一次验证（[飞书应用](../docs/feishu-app.md)） |
| `install_skills.sh` | 把 `skills/` 同步到 Codex 和 Claude Code 的 Skills 目录；`./install --skills` 也做这件事 |
| `upgrade.sh` | 日常升级：把 `./install` 装的机器人换到上游的新提交，替 `deploy_bridge.sh` 做前后几步（[部署](../docs/deploy.md)） |
| `setup_harness_bridge.sh` | **已弃用，用 `./install`。** 旧安装脚本，只在要手工控制每一步时用（[手工安装](../docs/manual-install.md)），运行时会在 stderr 提示 |

## 维护者用

开发、测试、发版和排障用的。

| 文件 | 用途 |
| --- | --- |
| `test_map.py` | 按改动选测试并运行；`run --full` 跑全量，`build` 重建测试地图（[测试与验收](../docs/testing.md)） |
| `land.py` | 批量合入：同一台机器上并行的话题交卷，它攒批、整批跑一次全量、绿了快进推 `main`，红了二分打回（[批量合入](../docs/testing.md#批量合入)） |
| `ci_retry_modules.py` | 全量失败后，从测试台账读出要重跑一遍的失败测试模块（CI 用） |
| `golden_trace.py` | 记录测试让 Bridge 做了什么并比较两次记录，用来确认“不改行为”的改动真没改 |
| `cross_release.py` | 让两个 release 的 Bridge 和任务进程通过 `runs/<任务键>/` 对话，检查跨版本兼容 |
| `compat_report.py` | 用线上数据判断标了 `# compat(...)` 的兼容代码能不能删 |
| `check_shim_compat.py` | 检查 `covey/core/device_shim.py` 只用标准库、能在 Python 3.9 上跑 |
| `check_doc_links.py` | 检查 Markdown 里的相对链接和锚点是否有效 |
| `code_map.py` | 从各模块的 docstring 生成 `AGENTS.md` 的模块表；`--check` 只检查 |
| `privacy_check.py` | 隐私检查：扫要公开的文件里的个人、公司信息，有命中就退出码 1 |
| `cardkit_check.py` | 把卡片 JSON 用机器人身份在 CardKit 建一遍（不发到任何聊天），检查它能不能被接受 |
| `card_sketches.py` | 用当前代码的卡片重画 README 和文档里的卡片示意图 |
| `ui_samples.py` | 改外观前的样张：从卡片金样取现在的样子，照 Bridge 的读法查几版样张、画出主人会收到的问题卡（[UI 规范 · 改外观先选样张](../docs/ui.md#12-改外观先选样张)） |
| `web_shots.py` | 用演示数据起一个自托管网页，用无头 Chromium 重拍 README 里的网页截图 |
| `export_look.py` | 把 `covey/core/look.py` 导出成网页读的 `look.json` |
| `bridge_timing.py` | 从 Bridge 的毫秒计时日志看各条流程的时间花在哪 |
| `question_stats.py` | 按时间窗统计问题卡、选推荐率和类别，比较改提问规则前后（[运维](../docs/operations.md#问题卡问得多不多)） |
| `memory_migrate.py` | 记忆迁移：预演、立刻迁一次、各范围条数、回滚前把新写的导回旧格式（[运维](../docs/operations.md#记忆的存储与迁移)） |
| `track_route_eval.py` | 用带标注的语料评估消息路由的准确度 |
| `assemble_release.py` | 把核心检出和若干扩展检出拼成一个 release 目录 |
| `precompile_release.py` | 给 release 目录写好 `.pyc`，服务启动时不用再编译 |
| `deploy_bridge.sh` | 从 git 检出部署：检查、拼 release、切换（[部署新版本](../docs/deploy.md#部署新版本)） |

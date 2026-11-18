<!-- privacy: allow-file 开发说明里描述扩展的模块表和测试命令 -->
# 仓库合并约定

名词以 [docs/glossary.md](docs/glossary.md) 为准：界面、提示词只用表里的中文名。

默认主干是 `main`，GitHub `ericxk/covey` 是准源。改动在分支上做；开发完成并跑完适用检查（见下文「怎么挑测试」）后，先同步最新 `main`，再快进合并进 `main` 并推到 GitHub，不 force 推送，也不覆盖别人的提交。检查存在已确认的基线失败时，记录验证结果和限制，不把它说成全部通过。GitHub Actions（`.github/workflows`）在 `main` 上跑测试，红了先修。

贡献者和 Coding Agent 都照下面几条做：

1. **凭据不进仓库**：账号登录、令牌、SSH 配置、Codex 的 `auth.json` 等不写进代码、文档、测试和提交说明；测试用一看就是假的值。
2. **个人环境靠配置传入，代码里不写**：主机名、路径、账号、open_id、群 ID、节点清单放自己数据根（`COVEY_DATA_HOME`，见 [数据目录](docs/operations.md#数据目录)）里的配置文件或服务的环境变量（`COVEY_*`）。
3. **部署记录不进仓库**：`docs/` 只写对每个部署都成立的用法、规则和已知限制，例子里的路径用 `/absolute/...` 这类占位。
4. **只有部分使用者要用的功能默认关闭**：没配置时不起后台线程、不加入口、不改已有行为；在对应文档写明怎么打开（如 [本机运行锁](docs/runtime-locks.md)；整块的可选功能做成[扩展](docs/architecture.md#扩展)）。
5. **公共核心的改动对所有使用者生效**：改锁、记录与状态、调度、投递、服务管理（见下文「最容易改坏的规则」）时，开关关着的行为要和原来一样，并跑全量。
6. 部署照 [部署新版本](docs/operations.md#部署新版本) 的步骤。

# 代码地图

先在这里找文件，再 `grep -n` 定位、按行号范围读。路径相对 `covey/`，`bin/` 只是入口壳。结构、数据流和不变量见 [架构与开发说明](docs/architecture.md)（先读[分层与包职责](docs/architecture.md#分层与包职责)、[一条消息的数据流](docs/architecture.md#一条消息的数据流)、[卡片点击的数据流](docs/architecture.md#卡片点击的数据流)），跨模块的分工见它的[代码位置](docs/architecture.md#代码位置)。

## 功能 → 从哪看起

| 功能 | 入口模块 | 文档 |
| --- | --- | --- |
| 收消息、路由、建任务 | `app/card_routing.py`、`app/topics.py`、`app/submission.py` | [一条消息的数据流](docs/architecture.md#一条消息的数据流) |
| 卡片按钮与点击 | `app/card_actions.py`、`app/card_routing.py`、`app/card_lanes.py` | [卡片点击的数据流](docs/architecture.md#卡片点击的数据流)、[cards.md](docs/cards.md) |
| 谁被服务、群聊 | `app/access.py`、`core/groups.py`、`app/group_manager.py` | [谁被服务](docs/architecture.md#谁被服务) |
| 管家（私聊模式） | `app/butler.py`、`core/butler.py`、`core/placement.py`、`app/butler_tools.py` | [butler.md](docs/butler.md) |
| 话题与回带 | `app/butler_topics.py`、`app/butler_relay.py`、`app/relay_targets.py`、`app/relay_send.py` | [butler.md](docs/butler.md) |
| 看板、Pin、手上的事 | `app/pins.py`、`app/status_cards.py`、`app/overview.py`、`core/board_read.py` | [手上的事](docs/usage.md#手上的事)、[配置](docs/configuration.md#手上的事与话题看板) |
| 整理专项与话题要点（`COVEY_PROJECT_TIDY`） | `app/project_tidy.py`（时机、整理、卡片点击）、`app/project_sorter.py`（小模型读什么、补要点）、`core/topic_gist.py`（话题要点）、`core/project_proposals.py`（提议与主人的决定）、`cards/project_proposal.py`、`app/projects.py` 的 `gather`/`join_topic`/`leave_topic` | [butler.md](docs/butler.md#整理专项开关默认关装好就开只提议)、[配置](docs/configuration.md#整理专项) |
| 早晚汇总 | `app/digest.py`、`cards/digest.py` | [早晚汇总](docs/usage.md#早晚汇总)、[配置](docs/configuration.md#早晚汇总) |
| 启动卡与工作区 | `app/launch.py`、`app/model_picker.py`、`app/directory_picker.py`、`core/workspaces.py` | [开始一个任务](docs/usage.md#开始一个任务) |
| 任务卡与执行过程 | `app/cards.py`、`app/task_card.py`、`core/trace.py`、`cards/streaming.py` | [cards.md](docs/cards.md)、[执行指标口径](docs/architecture.md#执行指标口径) |
| 问题卡与回答 | `app/questions.py`、`app/native_questions.py`、`app/checkpoint_questions.py`；「以后这类按推荐走」`app/standing_answers.py` | [问题与回答](docs/architecture.md#问题与回答) |
| 补充 | `app/steering.py`、`core/deliveries.py`、`core/steer.py` | [补充投递](docs/architecture.md#补充投递) |
| 换配置 | `app/harness_switch.py`、`app/binding.py`、`app/switch_handoff.py` | [话题中途换配置](docs/architecture.md#话题中途换配置) |
| 回退一步（Claude） | `app/rewind.py`、`backends/claude/rewind.py` | [回退一步](docs/architecture.md#回退一步) |
| 结果、投递与重投 | `app/turn_settle.py`、`app/worker.py`、`app/redelivery.py`、`app/outbox.py` | [结果授权与投递](docs/architecture.md#结果授权与投递) |
| 失败、恢复与自动重试 | `core/recovery.py`、`core/failure.py`（原因和下一步）、`app/recovery_cards.py`、`app/auto_retry.py` | [中断与恢复卡](docs/architecture.md#中断与恢复卡) |
| 设置卡与机器人菜单 | `app/bot_menu.py`、`app/menu_panel.py`、`app/settings_hub.py`、`feishu/events.py` | [机器人菜单](docs/usage.md#机器人菜单) |
| 代答 | `app/proxy.py`（批次）、`app/proxy_intake.py`（收消息）、`app/proxy_topics.py`（联系人话题）、`app/proxy_commands.py`（查找、群成员）、`app/proxy_send.py`（读回和发送器接口；发送器 `extensions/covey_internal/proxy_sender.py`、`extensions/covey_internal/proxy_login.py`）、`core/proxy.py` | [proxy.md](docs/proxy.md)、[代答](docs/architecture.md#代答) |
| 主人的原则 | `core/principles.py`（`entries`、`delete`）、`app/prompts.py` 的 `principles_block`、`Runs.principles`/`Runs.owner_turn`（`app/runs.py`）、`app/butler_tools.py` | [butler.md](docs/butler.md) |
| 知识条目与历史搜索 | `core/knowledge.py`、`app/knowledge_turn.py`、`app/knowledge_collect.py`、`app/butler_knowledge.py`、`app/prompts.py` 的 `knowledge_block`/`knowledge_rules`、`app/digest.py` 的 `knowledge_view` | [butler.md](docs/butler.md#知识库)、[配置](docs/configuration.md#知识条目与历史搜索) |
| 记忆（`COVEY_MEMORY=1`） | `core/memory.py`（读口：条目、编号、范围上限）、`core/topic_memory.py`（话题的记忆）、`app/memory_turn.py`（每轮的块、`memory_ids`、纠正规则）、`app/memory_collect.py`（一轮结束后记下 `memory`、作废 `memory_void`，“不对”点击 `undo`）、`app/prompts.py` 的 `memory_block`、`cards/answer_card.py` 的 `memory_element`、`app/answer_cards.py` 的 `put_memory` | [butler.md](docs/butler.md#记忆开关默认关)、[配置](docs/configuration.md#记忆) |
| 敏感词脱敏 | `core/redaction.py`、`core/redactor.py`、`backends/claude/redaction.py`、`app/redaction_panel.py`（设置卡「脱敏」标签）、`app/dashboard.py`（可选的网页看板） | [敏感词脱敏](docs/architecture.md#敏感词脱敏) |
| 官方拒绝与停止 | `app/policy_guard.py`、`core/model_policy.py`、`backends/claude/policy.py`；cyber/bio 拒绝换旧模型重试一次 `app/policy_retry.py` | [policy-stop.md](docs/policy-stop.md) |
| 任务进程与 `runs/` 协议 | `app/detached.py`、`app/runner_spawn.py`、`app/runner_ipc.py`、`app/runner.py`、`core/run_dir.py` | [排队与任务进程](docs/architecture.md#排队与任务进程)、[跨版本检查](docs/testing.md#任务进程协议与跨版本检查) |
| 记录、状态与锁 | `core/jobs.py`、`core/statuses.py`、`core/store.py`、`core/locks.py` | [状态与并发](docs/architecture.md#状态与并发) |
| Harness 与后端 | `core/harness.py`、`core/roster.py`、`core/providers.py`、`backends/<名字>/bridge.py` | [Harness 接口](docs/architecture.md#harness-接口)、[harnesses.md](docs/harnesses.md) |
| 扩展（`COVEY_EXTENSIONS` 或 `extensions/enabled` 启用的包，核心不按名字引用；扩展自带 README） | `core/extensions.py`（manifest 并进 `core/roster.py`、`core/providers.py` 的表，`features(app)` 并进 `app/unified.py` 的 `self.features`） | [扩展](docs/architecture.md#扩展) |
| 节点与机器状况（`COVEY_NODE_LISTEN`） | `core/node_agent.py`（设备和本机上常驻的节点）、`core/nodes.py`（名单、口令、记录）、`core/node_rules.py`（告警规则）、`app/node_gateway.py`（TLS 服务）、`app/node_alerts.py`、`app/nodes.py`、`cards/node_cards.py` | [节点](docs/architecture.md#节点默认关)、[配置](docs/configuration.md#节点与机器状况默认关) |
| 维护与备份 | `core/maintenance.py`（规则表在 `core/maintenance_rules.py`，备份在 `core/backup.py`）、`app/stale.py`、`core/migrate.py` | [备份、归档与维护](docs/operations.md#备份归档与维护) |
| 服务、看门狗、诊断 | `service/manager.py`、`service/boot.py`、`service/memguard.py`、`service/doctor.py` | [服务组成](docs/operations.md#服务组成)、[troubleshooting.md](docs/troubleshooting.md) |

## 模块表

由 `python3 -B scripts/code_map.py --write` 从各模块 docstring 的首句生成，不要手改：改 docstring 再重新生成。`test_structure` 的 `CodeMapTest` 核对表格和生成结果一致。

<!-- code-map:begin (scripts/code_map.py --write) -->
### app

| 文件 | 管什么 |
| --- | --- |
| `app/access.py` | Who the Bridge serves: the one place that decides it |
| `app/answer_cards.py` | The cards a finished turn's answer went out on: what they say under the answer and keeping their 产物 up to date |
| `app/answer_delivery.py` | TurnDelivery's answer card sent on its own |
| `app/answers.py` | Answers to questions, without saving or sending anything |
| `app/app.py` | BridgeApp: one Harness's components and its entry points |
| `app/app_setup.py` | The Feishu app's configuration kept up with the Bridge: the owner, 2026-11-09: 使用者只扫码，其余由 Bridge 用浏览器完成 |
| `app/artifacts.py` | Publish only explicitly recorded, bounded artifacts of a task |
| `app/auto_retry.py` | Automatic 检查并继续 after a turn that ended without a reply as one of KINDS |
| `app/availability.py` | Separate local backend readiness from recently observed model access |
| `app/binding.py` | A topic's binding (Harness, model, budget, work mode, place and 敏感词脱敏) and what may change it |
| `app/bot_menu.py` | The bot's custom menu (机器人自定义菜单): the owner's commands, since typed ones misfired |
| `app/butler.py` | The butler's session, only in the private chat mode 管家 |
| `app/butler_env.py` | What a butler tool handler gets besides its request |
| `app/butler_handoff.py` | What a topic gets when the butler hands it work |
| `app/butler_knowledge.py` | The butler's knowledge-entry tools and history_search, the one that asks the history command … |
| `app/butler_panel.py` | The 管家 settings card: the owner changes the butler's settings in the private chat |
| `app/butler_reactions.py` | Reactions on the owner's message for a butler turn when its topics hang under his question (core/placement.py … |
| `app/butler_relay.py` | A topic turn's outcome back at the private chat's top level, only with the butler on |
| `app/butler_reply.py` | How a butler turn ends on its task card, for the turns whose Traits say `answers_in_place` (the butler's) |
| `app/butler_search.py` | topics_search, the butler's tool that finds which topic, and which turn of it, a clue came from |
| `app/butler_tools.py` | The butler's tools as the Bridge runs them, unless COVEY_BUTLER=0 |
| `app/butler_topics.py` | The butler's tools that change topics |
| `app/butler_tracks.py` | 多路并行 at run time (docs/butler.md 多路并行 |
| `app/card_actions.py` | Task-card button kinds the unified bot accepts, in one table |
| `app/card_lanes.py` | Card clicks run in order within a topic and in parallel across topics |
| `app/card_queue.py` | A bounded queue of card clicks and the worker threads that run them |
| `app/card_routing.py` | Routing what Feishu sends the Bridge |
| `app/cards.py` | Cards: the one place that writes a Harness's task and process cards |
| `app/checkpoint_questions.py` | A question that comes with a checkpoint result |
| `app/compact_runner.py` | The butler's idle compaction runner: `bin/covey_runner.py --compact-only` |
| `app/compaction.py` | The butler's compaction requests: its next turn compacts its native session first |
| `app/dashboard.py` | The sensitive-term dashboard: a page on the intranet where the owner edits the redaction term list |
| `app/dashboard_entry.py` | Where the sensitive-term dashboard is |
| `app/dashboard_http.py` | The sensitive-term dashboard's HTTP side: its server, each request's route and access |
| `app/detached.py` | Run each task in its own process so a bridge restart or deploy does not stop it |
| `app/device_files.py` | Files between the hub and a device's topic, always through the device program |
| `app/devices.py` | Devices at the app's edge |
| `app/digest.py` | 早晚汇总: at the times of day COVEY_DIGEST_TIMES lists, one card at the top level of the owner's private chat … |
| `app/directory_picker.py` | Bounded directory browsing: one listing or name search at a time, never on the event threads |
| `app/dispatch_cards.py` | 派发卡, when COVEY_DISPATCH_CARD=1 |
| `app/forks.py` | Forks: a new topic that continues from another topic, opened by the butler's topic_fork |
| `app/form_cards.py` | Form cards: a card with a record of its own that every click on it reads |
| `app/group_boards.py` | 手上的事, the pinned board and 早晚汇总 in each personal group |
| `app/group_manager.py` | The 群聊 card: the owner turns groups on and off in the private chat |
| `app/group_menu.py` | The menu card of a personal group |
| `app/harness_switch.py` | Change a topic's Harness, provider, model, workspace, run mode or 敏感词脱敏 mid-conversation (🔁 换配置) |
| `app/harnesses.py` | The Harnesses of one Bridge, one object each |
| `app/health.py` | Read-only, bounded local health snapshots |
| `app/held_notes.py` | A message sent while several questions wait for answers is kept, not refused |
| `app/idle_compaction.py` | The butler's compaction while it is idle: the owner no longer waits for it |
| `app/interaction.py` | Backend event dispatch and task-card callbacks |
| `app/job_dispatch.py` | Running a dequeued job (process_reply_job |
| `app/knowledge_collect.py` | A topic's turn writes what the owner asked it to remember |
| `app/knowledge_turn.py` | What a turn reads of the knowledge entries and the history command (COVEY_HISTORY_CMD), for app/runs.py |
| `app/launch.py` | One local form selection and one server submission for unified topics |
| `app/launch_models.py` | Local launch choices |
| `app/launch_receipt.py` | Replace a submitted launch form with its immutable topic configuration |
| `app/leftovers.py` | After a turn's backend has closed, the processes its commands left running |
| `app/memguard_alerts.py` | The memory watchdog's alerts, sent by the Bridge |
| `app/memory_collect.py` | What the owner said in a topic's turn that holds from now on becomes memory, and a click takes it back |
| `app/memory_turn.py` | What a turn reads of the owner's memory, for app/runs.py |
| `app/menu_panel.py` | A card the bot menu sends to the owner's private chat: no topic, no job, one card a menu click |
| `app/model_picker.py` | The launch card's choices |
| `app/model_usage.py` | What each chat chose on the model selects: the order they list models in, and each model's usual budget |
| `app/native_questions.py` | A backend's question as it arrives |
| `app/node_alerts.py` | The nodes' alerts, sent by the Bridge |
| `app/node_gateway.py` | The hub's side of the nodes' connections: a TLS server in the Bridge, when COVEY_NODE_LISTEN says where |
| `app/nodes.py` | 节点 at the Bridge: the gateway the nodes dial, the hub's own node, the alerts |
| `app/outbox.py` | Resume delivery of authorized saved replies only |
| `app/overview.py` | 手上的事: one card in the owner's private chat with the bot, pinned (Pin 消息), that lists what waits for the owner … |
| `app/patrol.py` | The feature object of "what the butler would have told the owner", COVEY_PATROL=shadow |
| `app/patrol_judge.py` | The second layer: the small model reads the candidates app/patrol_rules.py found and says, for each, drop it … |
| `app/patrol_rules.py` | The rules of the first layer |
| `app/pins.py` | The pinned board: the owner's private chat's topics pinned (Pin 消息) in the order the Bridge keeps, under … |
| `app/policy_guard.py` | Independent policy stop consumer: covers adopted runners from older releases |
| `app/policy_retry.py` | One retry on an older model after an official safety refusal |
| `app/preselection.py` | What the launch card and the 换配置 card preselect when they are drawn: pure functions of what the callers read |
| `app/project_cards.py` | 专项卡, when COVEY_PROJECTS=1 |
| `app/project_context.py` | What a project's memory and library do in the Bridge, when COVEY_PROJECTS=1 and COVEY_PROJECT_MEMORY=1 |
| `app/project_lead.py` | The lead's runs: what a project's lead topic (the 负责人) reads and how its session lives |
| `app/project_loop.py` | Continuing runs and guards of a project |
| `app/project_sorter.py` | 整理专项's judging: what the small model reads and what it may say |
| `app/project_tidy.py` | 整理专项: projects grow out of the topics by a mechanism, not by the one look the butler takes when it opens a … |
| `app/project_tools.py` | The tools of a project's lead, handlers as app/butler_env.py describes them |
| `app/projects.py` | 专项的升级、直接开专项和降级 |
| `app/prompts.py` | Shared prompt builders: the bridge rules and each turn's message |
| `app/proxy.py` | Proxy replies (代答), the Bridge's side |
| `app/proxy_commands.py` | Proxy replies (代答): what the 代答设置 card asks for, its settings, and finding the contacts to whitelist |
| `app/proxy_intake.py` | Proxy replies (代答): taking messages in, as the owner (the IM events are the bot's and never show the owner's … |
| `app/proxy_panel.py` | The 代答设置 card: the owner's private-chat panel for 代答 |
| `app/proxy_resend.py` | The 重新发送 card: a 代答 reply that never went out, offered to the owner to send again |
| `app/proxy_send.py` | Proxy replies (代答): Feishu as the owner sees it |
| `app/proxy_topics.py` | Proxy replies (代答): each contact's topic in the proxy group |
| `app/question_display.py` | Where a question is shown |
| `app/questions.py` | Durable questions: the Questions component, answers and delivery recovery |
| `app/quiet_delivery.py` | The quiet butler turn's side of delivering a result |
| `app/record_cards.py` | Writes to a job's card from its record once its own writer has ended (finished, or the job is in another … |
| `app/recovery_cards.py` | The card of a task that did not end as it should (interrupted, failed, or its result not delivered) and the … |
| `app/redaction_panel.py` | The 设置 card's 脱敏 tab |
| `app/redelivery.py` | Sending a recorded task's saved result or question again, without running the model (检查并继续 on a task whose … |
| `app/relay_send.py` | Sending what goes back to the butler's top level |
| `app/relay_targets.py` | Where a topic turn's outcome goes back to at the butler's top level |
| `app/rewind.py` | ↩️ 回退: drop a topic's latest turn, on a Harness that can (Claude Code |
| `app/runner.py` | The task runner process: one job of the Bridge, to completion |
| `app/runner_ipc.py` | Messages between the Bridge and a job's task runner through its run directory |
| `app/runner_spawn.py` | Starting a job's task runner, the Bridge side of app/detached.py |
| `app/runs.py` | Runs: one job's turn on its Harness's backend |
| `app/runtime.py` | Process-wide Bridge state, one object passed by reference to every part that needs it |
| `app/saved_send.py` | A turn's saved reply, paused question or canonical text sent the same way on its first send and on each … |
| `app/sessions.py` | Sessions: one Harness's native-session pointer of a topic |
| `app/settings_hub.py` | The 设置 card: the bot menu's 设置 (event key MENU_KEY) puts the butler's settings in two pages, 私聊默认 and 新话题默认 … |
| `app/stale.py` | Close the records daily maintenance found waiting too long (core.stale.stale_records) |
| `app/standing_answers.py` | 以后这类按推荐走: the button under a question card whose every question has one recommended option |
| `app/status_cards.py` | Topic status cards: what the pinned board pins for a topic with a thread in the owner's private chat, in … |
| `app/steer_forms.py` | The 补充 forms of a task: a 补充 click, the form (steer_form_card), a submitted form (submit_steer_form |
| `app/steer_receipts_flow.py` | What became of a supplement, on its receipt card |
| `app/steering.py` | Supplements (补充) to a task of this topic: which task takes one, and how it gets there |
| `app/stuck_check.py` | 看看: a read-only look at a turn that may be stuck, from the button under its task card's status line, answered … |
| `app/submission.py` | Submitting a topic's message as a job |
| `app/switch_handoff.py` | A topic's first run after 🔁 换配置 rebinds it |
| `app/task_card.py` | The streaming task card on CardKit |
| `app/task_controls.py` | Task-card stop and retry controls, and showing a task's recovery card |
| `app/task_details.py` | 任务详情 and the process panel (执行过程) on a job's live task card, and the dashboard card's tiles or the 输入/输出 … |
| `app/topic_links.py` | Links that open a topic from a card (docs/butler.md 话题链接 |
| `app/topic_seen.py` | Which topics the owner has seen, from Feishu's read receipts |
| `app/topics.py` | Topics: which Harness app a topic's messages, clicks and jobs go to |
| `app/track_digest.py` | Memory merge for 多路并行 |
| `app/track_judge.py` | The small model that decides a new butler message the rules could not |
| `app/track_standby.py` | A Claude Code kept ready for the track judge |
| `app/triage.py` | 专项的问题分流 (docs/butler.md 专项的问题分流, COVEY_PROJECTS=1 and COVEY_PROJECT_TRIAGE=1 |
| `app/triage_view.py` | The questions held for a project's lead, as the lead reads them (app/triage.py is the rest of the question … |
| `app/turn_export.py` | 下载请求与结果: a button at the bottom of an ended task card that packs the turn's model requests and the model's … |
| `app/turn_images.py` | Images a running task shows its owner at once: a login QR code, a screenshot he has to act on |
| `app/turn_settle.py` | How a finished turn ended, settled before anything is delivered (rule 5's core) |
| `app/unified.py` | The Bridge: one Feishu ingress, immutable topic routes, one app per Harness |
| `app/web_ops.py` | 网页上发来的操作怎么执行: `Ops.run` does one operation of the page's outbox as the owner's own and says (state, message) … |
| `app/web_sync.py` | 网页工作台同步: pushes the state of the owner's projects, topics, turns and questions to the web page (the 妙搭 app) … |
| `app/web_turns.py` | What a turn tells the web page beside its words (docs/butler.md 网页工作台, COVEY_WEB=1 |
| `app/weekly_usage.py` | 本周用量: the bot menu's item answered with a card of the last WINDOW_DAYS days' tasks |
| `app/worker.py` | Ordinary topic-job execution, local records and in-topic final delivery |
| `app/workspace_manager.py` | Manage the saved workspace list |

### cards

| 文件 | 管什么 |
| --- | --- |
| `cards/answer_card.py` | What follows a finished turn's answer on its answer card in the topic and on its relay at the private chat's … |
| `cards/app_setup.py` | The 飞书应用待补 card: what the app lacks, then the run's steps, then what it added |
| `cards/board.py` | The bot menu's 看板: what is going on in the owner's private chat now, one coloured block a topic (the owner … |
| `cards/butler_panel.py` | The 管家 settings card, in the bot menu panels' style |
| `cards/card_cost.py` | What a card costs CardKit, which refuses one over its size (code 200860) |
| `cards/cardkit_actions.py` | What a CardKit write says, as plain JSON |
| `cards/digest.py` | The digest card: sent at the times the deployer set, at the top level of the owner's private chat |
| `cards/directory.py` | The directory browser card |
| `cards/dispatch_card.py` | The 派发卡 (app/dispatch_cards.py |
| `cards/elements.py` | Schema 2.0 card skeletons and the buttons several cards share |
| `cards/facts.py` | A turn's figures from its process snapshot, worked out in one place for the task card's tiles and the answer … |
| `cards/group_manager.py` | The 群聊 card, the bot menu's 群聊 panel |
| `cards/held_note.py` | A held note's card: a message sent while the topic's questions wait, saved to go with the answer |
| `cards/launch_config.py` | The launch card: its form and the model-scoped reasoning choices on it, and the settings list it shares with … |
| `cards/launch_receipt.py` | The card a submitted launch form becomes: its immutable topic configuration |
| `cards/node_cards.py` | The cards of nodes: an alert (⚠️ testbox 内存紧张, what was seen, since when, who used the most) and the same … |
| `cards/overview.py` | The 手上的事 card, pinned in the owner's private chat with the bot and redrawn whole as its tasks change |
| `cards/project_card.py` | The 专项卡 (app/project_cards.py |
| `cards/project_proposal.py` | 整理专项's cards: the proposal card (建个专项？), its rename form, the 整理专项 card of a scheduled or asked-for 整理, and … |
| `cards/proxy_panel.py` | The 代答 card, the bot menu's 代答 panel: on or off and the group in its title bar |
| `cards/proxy_resend.py` | The 重新发送 card: in a contact's 代答 topic, a reply that never went out (Enter never pressed, every try), with … |
| `cards/question_cards.py` | The question form, its standalone card, the feedback card and the answer notices |
| `cards/recovery.py` | The buttons that rerun a stopped task, and the card of one whose task card cannot take them any more … |
| `cards/redaction_link.py` | The web dashboard's link card |
| `cards/redaction_panel.py` | The 设置 card's 脱敏 tab, one layer |
| `cards/relay_card.py` | The card a topic turn's outcome takes back to the butler's top level |
| `cards/review.py` | Shared history and chat-card rendering helpers |
| `cards/rewind.py` | The ↩️ 回退 button and card |
| `cards/settings_hub.py` | The 设置 card's tab row |
| `cards/status_card.py` | The topic's status card |
| `cards/steer_form.py` | The 补充 form, what stands in for it once it can send nothing, and the task card's 最新补充 line |
| `cards/steer_receipts.py` | The receipt card of a supplement sent into a running task |
| `cards/streaming.py` | The streaming task card: the whole card built from a job (build_stream_card), its buttons, body and 任务详情 |
| `cards/switch_form.py` | The 🔁 换配置 card |
| `cards/task_figures.py` | The task card's figures: the tiles (开始 or 用时, token, 速度, 上下文 |
| `cards/task_status.py` | The task card's status line |
| `cards/topic_actions.py` | Buttons that act on a whole topic: 换配置 and 回退 |
| `cards/topic_card.py` | The topic card (话题卡): the root message of a topic the butler opens |
| `cards/trace.py` | Text helpers the process panel is drawn with |
| `cards/trace_updates.py` | Keep the process panel mounted while its content changes |
| `cards/trace_view.py` | Feishu rendering from an isolated execution snapshot |
| `cards/triage_card.py` | The 不对 form of the 专项卡's 分流记录: a card of its own with one input, because the 专项卡 is redrawn and holds no field |
| `cards/turn_export.py` | The 下载请求与结果 element at the bottom of an ended task card |
| `cards/weekly_usage.py` | The 本周用量 card, the bot menu's 用量 panel: the window in its title bar |
| `cards/workspace_manager.py` | The 工作区 card, the bot menu's 工作区 panel and the launch card's ⚙️ 添加或管理工作区… |

### core

| 文件 | 管什么 |
| --- | --- |
| `core/app_manifest.py` | What the Feishu app behind a Bridge must have, and what its online version lacks |
| `core/attachments.py` | Image attachments a Harness hands its model itself (Claude Code's stream-json input, pi's prompt) |
| `core/background.py` | The work a task runner finishes before it exits |
| `core/backup.py` | The daily backup of a state directory |
| `core/board_read.py` | What the owner's private chat's topics are doing, as the records say it |
| `core/browser_env.py` | Where the headless browser lives and how it starts |
| `core/butler.py` | The butler (管家), in the private chat mode 管家 |
| `core/butler_mcp.py` | The MCP server in the butler's session, run as bin/covey_butler_mcp.py by the Claude Code of a butler job |
| `core/butler_opened.py` | The butler's requests that opened, forked or handed on to a topic |
| `core/butler_requests.py` | The butler's tool channel |
| `core/butler_settings.py` | The butler's settings: <state>/main-thread.json |
| `core/butler_tool_specs.py` | The Bridge tools the butler's session gets |
| `core/butler_tracks.py` | 多路并行 (docs/butler.md 多路并行 |
| `core/card_sequence.py` | One rule for the sequence of every CardKit write to an existing card |
| `core/checkpoint.py` | Durable task checkpoints written by the agent and read by the bridge |
| `core/completion.py` | One post-root deadline, independent of model-request retry policy |
| `core/defaults.py` | Product defaults, separate from provider-native default metadata |
| `core/deliveries.py` | Supplements sent into a running task: one delivery record each |
| `core/device_link.py` | DeviceProcess: a Harness process that runs on a device, dressed as the `subprocess.Popen` the backends use |
| `core/device_reap.py` | What a device may still hold of a task the hub has given up on |
| `core/device_shim.py` | The device-side program the hub pushes over ssh: it supervises Harness tasks and serves files, stdlib only |
| `core/device_transport.py` | The hub's side of the device shim's wire protocol |
| `core/devices.py` | Devices: other machines (Linux over ssh) a topic can work on instead of this one |
| `core/diff.py` | Per-job workspace diff capture for shared Harness bridge jobs |
| `core/dispatch_records.py` | The records of the 派发卡 |
| `core/env.py` | Environment variable names: `COVEY_<X>` is the name, `LARK_BRIDGE_<X>` the old one that still works |
| `core/extensions.py` | Optional extension packages: which are enabled, where they are found, the data and features they add |
| `core/failure.py` | Why a turn did not succeed and what can be done next, in plain words |
| `core/file_lock.py` | Explicit host-local lock files |
| `core/gateways.py` | 模型网关的配置：一个网关是“OpenAI Responses / Anthropic Messages 兼容”的上游，由数据描述，可以有多个。 |
| `core/groups.py` | The group records: one per group, `<state>/groups/<sha256(chat_id)>.json`, with `chat_id`, `active` … |
| `core/harness.py` | Harness adapter contracts for the Feishu bridge |
| `core/harness_probe.py` | Harness 能不能真的调通：跑一次最便宜的真实调用（claude -p、codex exec），把失败分成几类。 |
| `core/history_storage.py` | A topic's history: appending a turn's rows (append_history), reading the latest ones and rounds … |
| `core/http_upstream.py` | Requests a local relay or gateway sends on to its upstream, and the answer it passes back |
| `core/ignored_env.py` | Backend parameters the environment no longer sets, and the two whose old values a service must not keep |
| `core/jobs.py` | Shared durable job-state helpers for the Feishu bridge scripts |
| `core/knowledge.py` | 知识条目: short entries the owner asked to be remembered ("记一下"), kept in one place so every Harness reads them … |
| `core/locks.py` | Resource-scoped locks |
| `core/logs.py` | Logs of a state directory |
| `core/longtask.py` | Long-task status: the thread that watches a running turn (TaskMonitor) |
| `core/loops.py` | The Bridge's background threads: `supervised` keeps one running, `every` is the loop most of them run |
| `core/maintenance.py` | Daily maintenance of a state directory: a verifiable backup, archival and bounded storage |
| `core/maintenance_report.py` | How maintenance's --dry-run words what it would do |
| `core/maintenance_rules.py` | The rules of daily maintenance |
| `core/memory.py` | The one read port of the owner's memory |
| `core/menu_choice.py` | A model menu's entries and the choices on them |
| `core/metrics.py` | Content-free job timings |
| `core/migrate.py` | One-time migrations of old records, so the code that reads records needs no compat branch for them |
| `core/model_names.py` | How model ids read: a readable name from an id, the budget a preset id ends with, and where a budget ranks |
| `core/model_policy.py` | Bounded model safety metadata and user-facing stop reasons |
| `core/model_settings.py` | Persisted model defaults with per-job configuration snapshots |
| `core/node_agent.py` | The Node: the program that stays on a device (and on the hub itself) and keeps one connection to its hub |
| `core/node_rules.py` | The rules that turn a node's samples into alerts, as pure functions |
| `core/nodes.py` | Nodes (节点): the machines that keep a connection to this hub, the hub itself included, and what they report |
| `core/paths.py` | Resolve service storage independently of the execution workspace |
| `core/patrol.py` | What the butler noticed and would have done: records and numbers, no judging |
| `core/placement.py` | Where a turn's words go in the private chat: one table, the layout by the role |
| `core/policy_feed.py` | Validate metadata-only policy observations and bind them to a single task interval |
| `core/policy_process.py` | Stop one identity-checked task tree, including children with their own sessions |
| `core/principles.py` | The owner's principles |
| `core/process.py` | Which processes belong to whom |
| `core/process_env.py` | Environment for backend child processes, and on/off switches read from the environment |
| `core/procfs.py` | A process's /proc/<pid>/stat and this boot's id, read one way for every reader in the package |
| `core/progress.py` | A running turn's progress worked out in one place (`progress`), for every reader that says how a turn is going |
| `core/project_gates.py` | 专项问题分流的硬闸（docs/butler.md 专项的问题分流）: the words that keep a project's topic's question from the lead |
| `core/project_library.py` | The library of a project |
| `core/project_memory.py` | The memory of a project |
| `core/project_proposals.py` | 整理专项's records and switches: what was proposed, what the owner decided and what 整理专项 must not do again |
| `core/projects.py` | 专项: a long job the butler hands to one lead topic (the 负责人) that dispatches and follows several member topics |
| `core/protocol.py` | Versioned public Harness events |
| `core/providers.py` | Where a Harness's models come from, in one table |
| `core/proxy.py` | Proxy replies (代答): the owner's whitelist of chats and each batch of their messages taken in |
| `core/qr.py` | 二维码：把一段文字（链接）编成 QR，给 ./install 在终端里画、存 PNG（service/installer.py）。 编码用的是 Project Nayuki 的 QR Code generator … |
| `core/qrcodegen.py` |  |
| `core/recovery.py` | What a failed, interrupted or stopped task's record allows next, decided in one place |
| `core/redaction.py` | Sensitive-term redaction: the owner's term list, which topics use it and what each turn replaced |
| `core/redaction_views.py` | The sensitive-term dashboard's views |
| `core/redactor.py` | Matching for sensitive-term redaction: terms to stand-ins in requests, stand-ins back in replies |
| `core/reply_state.py` | The verified reply and the outcome of one turn of Codex, Pi or dsh |
| `core/result.py` | Validated root reply identity, separate from execution and continuation rights |
| `core/roster.py` | The Harness backends the unified bot routes, in one table |
| `core/run_dir.py` | The run directory of a job handed to a task runner, `<state>/runs/<job>/` |
| `core/saved_results.py` | Private saved replies |
| `core/scheduler.py` | Bounded concurrency across topics |
| `core/serving.py` | The thread a local HTTP server serves on |
| `core/session_guard.py` | Block only the affected native identity |
| `core/session_state.py` | A topic's native session id in its state file, read (read_session_id) and replaced only from the revision it … |
| `core/spaces.py` | Spaces: the chats the Bridge works in, and what each is for |
| `core/stale.py` | Which waiting jobs and pending questions waited too long (`stale_records`), and the notes they are closed with |
| `core/state.py` | Shared local state and workspace helpers for Feishu bridge scripts |
| `core/status_line.py` | The task card's status line as pure functions of what one TaskMonitor tick saw |
| `core/statuses.py` | The status machines of the records |
| `core/steer.py` | What became of a supplement sent into a running task |
| `core/steps.py` | The steps an adapter publishes as `tool` events (a tool call, a command, a compaction) and the words a model … |
| `core/store.py` | Indexed JSON records with legacy-compatible reads and isolated invalid files |
| `core/task_processes.py` | What a task's commands leave running once its Harness has closed, found by the marker its runner puts in its … |
| `core/text.py` | Text shown on a card from what a task or a tool said: secrets hidden, bounded, escaped |
| `core/timing.py` | Millisecond timing of the live flows |
| `core/topic_gist.py` | 话题要点: a few lines each topic keeps of what it is about at heart, written by the topic's own model at the end … |
| `core/topic_memory.py` | The memory of one topic |
| `core/topic_seen.py` | When the owner last saw each topic |
| `core/topics.py` | Durable, chat-scoped Feishu topic -> Harness session bindings, and the readers and writers of a topic's … |
| `core/trace.py` | A turn's execution trace as data (ToolTrace) |
| `core/trace_store.py` | Process records on disk: one append-only file per task, outside the job record |
| `core/track_ledger.py` | The family ledger of 多路并行: what the butler's other tracks answered, for the butler's next turn to read |
| `core/track_route.py` | Where a new butler message goes when the family already has live tasks: the rule layer of the message router |
| `core/triage.py` | 问题分流的记录和开关: what a question record says about being held for a project's lead, and the switch |
| `core/ttft.py` | Numeric first-output timing |
| `core/turn_export_rows.py` | The rows of a turn export |
| `core/turn_words.py` | The words the cards, the pinned board and the butler say about a turn and a topic, in one place |
| `core/updates.py` | Per-task coalesced UI updates, isolated from backend event handling |
| `core/usage.py` | Numeric-only output throughput |
| `core/web_profile.py` | 代答's Feishu web login (its browser profile) |
| `core/web_sync.py` | 网页工作台同步的数据: the config, the four kinds of items pushed to the web page, and the ledger of the operations … |
| `core/workspaces.py` | Local allowlist and immutable topic workspace bindings |

### feishu

| 文件 | 管什么 |
| --- | --- |
| `feishu/app_config.py` | The app's online configuration, read as the bot through Feishu's official API (no login) |
| `feishu/app_console.py` | The Feishu developer console, driven by a headless Chromium logged in as the owner |
| `feishu/cardkit.py` | Shared CardKit API helpers for Feishu bridge scripts |
| `feishu/connections.py` | Linux event-bus transport watchdog, independent of user message activity |
| `feishu/delivery.py` | Shared Feishu message delivery helpers for bridge scripts |
| `feishu/events.py` | Shared Lark event consumer helpers for Feishu bridge scripts |
| `feishu/forward.py` | The model text of a quoted message (render_replied_message_context) and of a merged forward, nested forwards … |
| `feishu/install_browser.py` | ./install 用的浏览器会话：一个无头 Chromium 扫一次码，从头到尾替主人点完建应用、授权和开发者后台补配置。 在装了 Playwright 的 Python … |
| `feishu/media.py` | A received Feishu message as the text its model is sent (enrich_incoming_event) |
| `feishu/mentions.py` | Mentions in a Feishu message |
| `feishu/message_parts.py` | The content of a Feishu message as lark-cli and the message API give it |
| `feishu/message_read.py` | Reading from Feishu through lark-cli |
| `feishu/message_request.py` | The argv of the bot's `im +messages-send` / `+messages-reply` (message_argv), and the request lark-cli makes … |
| `feishu/messenger.py` | The Bridge's outbound Feishu boundary: text messages, card messages, published cards and CardKit writes |
| `feishu/outgoing_images.py` | The local images an answer's Markdown links to, uploaded to Feishu and linked by their image key instead … |
| `feishu/sdk.py` | Opt-in long-lived bot SDK for CardKit writes and message sends and reads |
| `feishu/transport.py` | Structured delivery failures and durable, bounded idempotent CLI writes |
| `feishu/web_client.py` | Feishu web, driven by a headless Chromium logged in as the owner, for the acceptance page of the browser … |
| `feishu/web_service.py` | Feishu web's browser service, for the acceptance page alone |

### service

| 文件 | 管什么 |
| --- | --- |
| `service/app_check.py` | The Feishu app check and the console run from a terminal, for the install and deploy scripts and by hand |
| `service/boot.py` | Small standalone boot watchdog |
| `service/browser_setup.py` | ./install 准备无头浏览器的全部细节（service/installer.py 调用）：用机器上已有的 Chrome/Chromium，没有就下载 Playwright 的 … |
| `service/device_check.py` | Check the devices of devices.json from the command line |
| `service/doctor.py` | Read-only layered diagnostics |
| `service/install.py` | Install an explicit boot registration without changing platform init files |
| `service/install_probe.py` | ./install 对 Harness 做的一次真实探测：薄适配层，判断本身在 core/harness_probe.py（安装器、doctor、可用性检查共用一份）。 |
| `service/installer.py` | ./install 的安装器本体：一遍跑完，扫一次码，最后发一句话就有回答（docs/install.md）。 |
| `service/launchd.py` | macOS 的 launchd 托管：`~/Library/LaunchAgents/<label>.plist` 让 Manager 登录后自动跑、挂了自动拉起。 |
| `service/manager.py` | Persistent single-host service management for all bridge entrypoints |
| `service/memguard.py` | Memory watchdog: stop runaway search tools before they exhaust the machine |
| `service/pairing.py` | Issue a one-time owner pairing code for an explicitly configured bot app |

### backends/claude

| 文件 | 管什么 |
| --- | --- |
| `backends/claude/argv.py` | How a turn's Claude CLI is started |
| `backends/claude/bridge.py` | The Claude Code Harness: its spec and backend |
| `backends/claude/compaction.py` | Compacting a resumed Claude Code session before a turn |
| `backends/claude/completion.py` | Claude result boundaries |
| `backends/claude/control.py` | The control side of a running Claude CLI (ClaudeControl) |
| `backends/claude/events.py` | Claude Code stream-json rows as public Harness events |
| `backends/claude/export.py` | One turn's model requests and responses, from Claude Code's own session records (the 下载请求与结果 button) |
| `backends/claude/handoff.py` | A handoff summary from a Claude Code session the topic is leaving |
| `backends/claude/models.py` | Public model metadata from the Claude initialization response, never account data |
| `backends/claude/policy.py` | Explicit upstream safety signals only |
| `backends/claude/process.py` | The Claude CLI's process and its JSONL transport |
| `backends/claude/redaction.py` | The loopback relay that redacts one Claude Code turn of a topic that chose redaction |
| `backends/claude/rewind.py` | ↩️ 回退 on Claude Code: drop the last turn of a session and put back the files it changed |
| `backends/claude/runtime.py` | One turn of the Claude CLI over its JSONL transport, following the official Agent SDK protocol (run_stream) |
| `backends/claude/sessions.py` | Claude Code session store for the Lark bridge |
| `backends/claude/subagents.py` | Claude Code's subagents on lighter models than the turn's (main-thread.json `subagents`, the 设置 card's 新话题 tab |
| `backends/claude/usage.py` | Current-invocation root and subagent usage and arrival-time latency, without model text |

### backends/codex

| 文件 | 管什么 |
| --- | --- |
| `backends/codex/appserver.py` | One Codex turn per task over its App Server (run_appserver) |
| `backends/codex/bridge.py` | The Codex Harness: its spec and backend |
| `backends/codex/events.py` | Codex App Server items as public Harness events |
| `backends/codex/export.py` | One turn's model requests and responses, from Codex's own rollout (the 下载请求与结果 button) |
| `backends/codex/gateway.py` | A model gateway's OpenAI Responses models inside the regular Codex Harness |
| `backends/codex/rpc.py` | The Codex App Server process and its JSON-RPC over stdio (AppServer) |
| `backends/codex/sessions.py` | Codex rollout session store for the Lark bridge |
| `backends/codex/turn.py` | One Codex turn read to its end (read_turn) |
| `backends/codex/usage.py` | A Codex turn's token counts from the App Server's cumulative counters |
| `backends/codex/workspace_reader.py` | Small read-only MCP server scoped to one task workspace (and the topic's received attachments) |

### backends/dsh

| 文件 | 管什么 |
| --- | --- |
| `backends/dsh/bridge.py` | DeepSeek Harness (dsh): a model gateway's Anthropic Messages models over dsh's headless profile |

### backends/gateway

| 文件 | 管什么 |
| --- | --- |
| `backends/gateway/catalog.py` | A gateway's Responses models on the launch card, and the relay's waits on the task card |
| `backends/gateway/connection.py` | 模型网关的连接：个人密钥、模型目录、请求头（网关由 core/gateways.py 的配置描述，可以有多个）。 |
| `backends/gateway/relay.py` | A local relay in front of a model gateway for one Codex or Pi task, so its 429s are ridden out |

### backends/pi

| 文件 | 管什么 |
| --- | --- |
| `backends/pi/bridge.py` | Pi: GPT models from the Codex login or from a model gateway |
| `backends/pi/events.py` | pi's RPC event rows to public Harness events |
| `backends/pi/home.py` | Pi's private home for one turn, and how its models reach it |
| `backends/pi/rpc.py` | One Pi turn over `pi --mode rpc`: a process per turn, JSONL both ways |

### bin

入口壳，只解析参数后调 `covey` 里的函数：`covey_app_console.py`、`covey_app_setup.py`、`covey_bridge.py`、`covey_butler_mcp.py`、`covey_devices.py`、`covey_doctor.py`、`covey_feishu_web.py`、`covey_install.py`、`covey_install_browser.py`、`covey_maintenance.py`、`covey_memguard.py`、`covey_pairing.py`、`covey_paths.py`、`covey_runner.py`、`covey_service.py`、`covey_service_install.py`、`covey_workspace_reader.py`、`covey_workspaces.py`。

改名前的入口名留作转发壳，服务清单、开机钩子和各部署的脚本还在用：`bridge_app_setup.py`、`bridge_doctor.py`、`bridge_main_mcp.py`、`bridge_memguard.py`、`bridge_pairing.py`、`bridge_service.py`、`bridge_service_install.py`、`covey_main_mcp.py`、`lark_app_console.py`、`lark_bridge_maintenance.py`、`lark_bridge_paths.py`、`lark_bridge_workspaces.py`、`lark_feishu_web.py`、`lark_harness_bridge.py`、`lark_install_browser.py`、`lark_task_runner.py`、`lark_workspace_reader.py`。
<!-- code-map:end -->

## 最容易改坏的规则

括号里是 `docs/architecture.md` 讲它的那一节，标 `cards.md` 的在 `docs/cards.md`。

1. 持记录锁不发网络请求：锁内存意图（`submitting`），锁外发，不确定转 `uncertain`，从不自动重发（主要记录与锁）。
2. `LOCK` 不跨飞书调用持有，持 `LOCK` 时不能申请 `topic_lock`（两者都在 `core/locks.py`；主要记录与锁）。
3. 已发卡片的 CardKit 写从 `reserve` 取 sequence，不回退不复用；发送都经 `Messenger`，任务卡只由 `Cards` 写（卡片 sequence）。
4. 状态只按 `JOB_TRANSITIONS`、`QUESTION_TRANSITIONS`、`NEXT`（限流自动重试 `auto_retry.py` 的 `AUTO_RETRY_TRANSITIONS`，安全拒绝换模型重试 `policy_retry.py` 的 `POLICY_RETRY_TRANSITIONS`）前进，终态不回退（状态）。
5. 只发经 `verified_reply` 保存并授权的回复；重投只发保存的正文，不跑模型（结果授权与投递）。
6. `RunHandle` 的写不自动重放：`SteerRefused`/`SteerNotReady` 是没发出，其他异常是可能已送达（一次运行）。
7. 后端不导入 `app/`、`feishu/`、`cards/`，也不导入别的后端（`backends/gateway` 除外），`TYPE_CHECKING` 下也不；扩展里隔离解释器导入的 `core/` 模块只用标准库（清单在扩展自己的结构测试里，如 `MASO_CORE`）；`__init__.py` 为空（硬约束）。
8. 新按钮 kind 先进 `ACTIONS`（带认领它的 `handler`），会建任务的设 `bot_only`；任务进程只改本任务的卡和记录（`cards.md` 添加一个卡片按钮）。
9. 表单变化时整卡替换；卡片 JSON 变了（`tests/golden/cards.json` 有 diff）要真实客户端验收（`cards.md` 卡片的客户端限制）。
10. 兼容（2026-11-14 改名 Covey：旧导入 `harness_bridge`、旧变量 `LARK_BRIDGE_*`、旧 bin 名、旧单元名都保留，见 docs/operations.md「改名 Covey 后的兼容」）：现网 `harnesses.json` 仍能解析（删功能时保留它写着的选项，如 `--claude-transport stream-json`）；`runs/<任务键>/` 与旧 release 的任务进程双向兼容；旧记录可读；已发卡片的 kind 不崩溃、不悄悄开工（硬约束）。
11. 谁被服务只经 `app/access.py`（`Runtime.access`）：消息 `admit`、点击 `allows`、主人专属 `is_owner`（菜单点击也用它），别处不比较 open_id（代答用 `owner_user_id` 认主人自己发的消息，不算）；群里的查询不在记录锁内（谁被服务）。

## 怎么挑测试

详见 [测试与验收](docs/testing.md)。

- 开发中只跑 `python3 -B scripts/test_map.py run --why "<原因>" [路径…]`：按改动的函数选模块，记进台账（`BRIDGE_TEST_LEDGER`）。
- `run`、`build` 每个测试模块一个子进程，`--jobs` 个并行（默认 8，`--jobs 1` 逐个）；测试的输出不显示，失败和错误照 unittest 格式列出。
- `run`、`build` 把测试临时文件放 `/dev/shm`（所有者同意，仅限测试临时文件，跑完即删）；它是 noexec，要直接执行写出的文件用 `support.exec_tempdir()`。
- 每个阶段结束一次全量：加了或改名了测试（`select` 提示地图过期）时 `build` 并提交 `tests/test_map.json`（地图过期时 `run --full` 拒绝），否则 `run --full`。不在没改的树上重跑全量，不拿全量当修复循环。
- 动了扩展里的 MASO 或它导入的 `core/` 模块：再用 MASO 解释器跑扩展的 `test_maso_*.py` 和 `--check-runtime`（命令见扩展自己的 README）。核心测试另在 `COVEY_EXTENSIONS=''` 下跑一次全量，确认没有扩展也过。
- 不改行为的改动：改前在基线上录两次 `scripts/golden_trace.py record`，改后录一次再 `compare --noise`；搬模块前跑 `scripts/test_map.py patches <模块>`，看哪些测试按旧路径打补丁（`docs/testing.md` 副作用轨迹与补丁清单）。
- 新测试在旧代码上该失败：`scripts/test_map.py prove --base <提交>`；部署用 `scripts/deploy_bridge.sh`（先 `--check-only`）。
- 改公共核心或 `runs/` 协议（规则 10）：先跑 `scripts/cross_release.py run --old <现网 release> --out <目录>` 再 `compare`；`tests/golden/run_dir.json` 有 diff 就是改了协议（`docs/testing.md` 任务进程协议与跨版本检查）。
- 新读环境变量或加命令行参数：在 `docs/configuration.md` 的「环境变量与选项一览」加一行（`test_structure` 的 `InventoryTest` 核对）。
- 加、删、搬模块或改了模块 docstring 首句：`python3 -B scripts/code_map.py --write`（`--check` 只检查）。
- `pyflakes covey bin tests extensions`（只许 2 条有意的未使用导入）；`bash -n scripts/*.sh`。

## 省 token

- 不整篇读文档和大文件。可能很长的 Bash 输出写进文件，只看结尾和 `grep -E '^(FAIL|ERROR)'`；派子代理时给确切的文件和行号范围。
- 只读要用的文档：结构、数据流和子系统规则读 `docs/architecture.md` 的那一节（`grep -n '^#'` 取行号），事件、执行过程、卡片点击和按钮读 `docs/cards.md`，管家 `docs/butler.md`，代答 `docs/proxy.md`；测试 `docs/testing.md`；部署、数据目录 `docs/operations.md`；配置和环境变量 `docs/configuration.md`；排障 `docs/troubleshooting.md`；Harness 参数 `docs/harnesses.md`；用户可见行为 `docs/usage.md`。`docs/history/` 一般不用读。

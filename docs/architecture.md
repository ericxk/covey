# 架构与开发说明

改代码前读[总览](#总览)、[代码布局与约束](#代码布局与约束)和两条数据流，再读要改的那一节（`grep -n '^#' docs/architecture.md` 取行号）。本文写结构、数据流和不变量；按文件找代码见根目录 `AGENTS.md` 的模块表，字段和细节见模块 docstring，常量、文案和日志格式以代码为准，规则的来由见架构决策记录。用户行为见[使用指南](usage.md)，部署见[运维指南](operations.md)和[配置](configuration.md)，验证见[测试与验收](testing.md)。

## 总览

进程、后台线程和谁重启谁见[服务组成](operations.md#服务组成)。

| 进程 | 做什么 | 入口 |
| --- | --- | --- |
| Bridge | 消费飞书事件、路由、建任务、排队、认领任务进程、重投、维护 | `bin/covey_bridge.py` → `app/unified.py` 的 `UnifiedBridgeApp.main()` |
| 任务进程 | 跑完一个任务（后端、任务卡、投递）；Bridge 重启或部署时继续 | `bin/covey_runner.py --run-dir <state>/runs/<任务键>` → `app/runner.py` 的 `runner_main` |
| Manager、看门狗 | 服务管理、开机拉起、内存兜底 | `service/manager.py`、`service/boot.py`、`service/memguard.py` |

Bridge 管家跑事件消费者（`lark-cli event consume <事件> --as bot`，每行一个事件）；其余后台线程（outbox、维护、看板等）异常退出后由 `core/loops.py` 的 `supervised` 重建。

## 代码布局与约束

### 分层与包职责

`covey/` 是唯一的实现包，`bin/` 只放入口壳。依赖只能从上往下：

```text
bin/  →  app/  →  feishu/ · cards/ · backends/<名字>/  →  core/
                                    service/（独立，只用 core/ 和标准库）
```

| 包 | 职责 | 不许做 |
| --- | --- | --- |
| `core/` | 和飞书无关的数据、契约和纯函数：记录、状态机、锁、调度、路径、进程、`runs/` 协议、Harness 契约 | 导入 `app/`（函数内也不） |
| `feishu/` | 飞书接口：出站 `Messenger`、幂等写与回执、SDK、CardKit、媒体、事件消费者 | 渲染卡片、改任务状态 |
| `cards/` | 只渲染：数据进、卡片 JSON 出；骨架和元素预算在 `elements.py`，保持首次发送时的键顺序 | 写状态、调飞书、处理点击 |
| `backends/<名字>/` | 一个后端一个目录（`codex`、`claude`、`dsh`、`agy`；[扩展](#扩展)带来的后端在扩展自己的目录里）；`gateway/` 是模型网关的连接、模型目录和中继，可被别的后端导入 | 导入 `app/`、`feishu/`、`cards/` 或别的后端 |
| `app/` | 组合根、各 Harness 的 `BridgeApp` 和组件、任务进程两端、worker、卡片控制器 | 写卡片 JSON 字面量 |
| `service/` | Manager、看门狗、doctor、安装 | 导入 `app/`、`cards/` |

这些方向由 `tests/test_structure.py` 检查，`if TYPE_CHECKING:` 下的导入也算。

### 对象怎么组合

- `UnifiedBridgeApp`（组合根；`app/card_routing.py` 的 `CardRouting`、`app/job_dispatch.py` 的 `JobDispatch` 是它的两半）建一个 `Runtime`（进程状态，`app/runtime.py`）、一个 `Messenger`，每个 Harness 一个 `Harness`（只有数据，`app/harnesses.py`）和一个 `BridgeApp`（`app/app.py`），加 `Topics` 和卡片控制器。任务进程按 `handoff.json` 里的 `Runtime.cli_argv` 再建一遍，用 `task_runner=True`：只有 Bridge 用的控制器、功能和机器人菜单（`UnifiedBridgeApp.PARTS`）第一次用到时才建、才导入模块（任务进程每轮都起，启动时间算在每个任务上；`runner.py` 的 `warm_parts` 在任务跑起来 3 秒后补建，第一次转发的点击不用等）；启动、维护和服务管理的模块只在 `main()` 里导入。新加的 Bridge 专用控制器加进 `PARTS`，不要在 `unified.py` 顶层导入；`test_unified_bridge` 的 `TaskRunnerPartsTests` 查任务进程启动后没有导入它们。
- `BridgeApp` 不继承任何类，入口只有话题消息、卡片点击、出队任务、启动时的过期任务；组件在 `__init__` 里按引用拿协作者。`Cards`（`app/cards.py`）是任务卡和执行过程面板唯一的写入方。
- 可选功能对象自报 `threads()`、`menu_items()`，汇总进后台线程和菜单表；没配置时不起后台线程、不加入口。`main()` 的停止步骤按顺序写在一个函数里，由 ExitStack 调用。

### 硬约束

- `bin/` 入口先把本 release 的仓库根插到 `sys.path` 最前再导入包，`ArgumentParser(prog=...)` 固定程序名。包内文件不作脚本运行；起子进程用 `core/paths.py` 的 `entry_script(name)`。
- `__init__.py` 全空。扩展的隔离解释器导入的 `core/` 模块只用标准库（公司扩展的清单在 扩展 tests 里的结构测试）；改了在那个解释器里跑扩展的测试，见扩展 README。
- `service/boot.py`（由 PID 1 加载）和 `service/memguard.py`（复制到本地盘运行）只用标准库、不导入本包。
- 原子写（临时文件加替换）收在 `core/state.py` 的 `atomic_write_bytes`，`write_private_text` 建在它上面；流式写（gzip、tar、复制大文件）和要保留特殊权限的写留在原处。`service/memguard.py`、`core/node_agent.py`、`core/device_shim.py` 要在设备上或独立进程里只用标准库运行，里面自带一份原子写，是故意的重复，不要合并进 `core/state.py`。
- 文字、卡片和 CardKit 写都经 `Messenger`；已发卡片的写从 `core/card_sequence.py` 的 `reserve` 取 sequence；按钮 kind 先登记在 `app/card_actions.py`；补充投递记录只经 `core/deliveries.py` 的 `update_delivery` 改。
- 子进程用 `core/process.py`：`stream_subprocess_lines` 超时和停止（`JobCancelled`）都停整个进程组；`stop_group` 先 SIGTERM 再 SIGKILL，活下来的只报告不抛。
- JSON 用 `core/state.py`：`read_json` 错误上抛；`write_json` 各写方用自己的临时文件再替换；`write_durable_json` 返回前落盘。
- 读了要写回同一个文件时用 `core/state.py` 的 `read_for_write`（或 `update_json`），在写方持的锁里读：没有文件给空值，文件在但读不出（网络盘一时出错、半截文件、不是对象）抛 `Unreadable`（是 `OSError`），这次不写，界面回“暂时读不出，未改动”。`read_object` 读不出给 `{}`，只给只读路径用；写回前用它读会把整份文件盖成只剩这次改的几项（`tests/test_structure.py` 的 `StateWriteTest` 查，现有的列在 `tests/golden/state_writes.json`，只减不增）。
- 按后端区分的路由和问答证明从 `core/roster.py` 派生；谁被服务只由 `app/access.py` 判断。
- 兼容：现网 `harnesses.json` 能解析；`runs/<任务键>/` 和旧 release 的任务进程双向兼容；旧记录照常读（旧的内嵌执行过程由 `core/migrate.py` 一次性写成 `traces/<任务键>.jsonl`）；旧卡上的 kind 照常工作或带提示拒绝。

### 代码位置

跨模块的分工（一个模块一行的表在 `AGENTS.md`）：

- **卡片渲染**：骨架、按钮、折叠面板和元素预算（`CARD_NODES`、`CARD_BYTES`）在 `cards/elements.py`，菜单面板、启动卡、回执、目录浏览共用它的面板样式；CardKit 写的 JSON 在 `cards/cardkit_actions.py`；表单卡的发布与整卡替换在 `app/form_cards.py`。
- **执行过程**：数据 `core/trace.py`（`ToolTrace`），落盘 `core/trace_store.py`，面板数据 `cards/trace_view.py`，画面板 `cards/trace_updates.py`，卡上数字 `cards/facts.py`。状态行：`core/longtask.py` 的 `TaskMonitor` 收集事实，`core/status_line.py` 的 `status_text` 出文字；状态词在 `core/turn_words.py`。各后端的事件适配器共用 `core/steps.py`。
- **启动卡与换配置**：`app/launch.py`、`app/launch_submit.py`（提交、worktree 启动）、`app/launch_models.py`（`CATALOG_FILTERS`）、`app/preselection.py`（纯函数预选）、`app/model_usage.py`、`app/model_picker.py`、`app/directory_picker.py`；换配置 `app/harness_switch.py`，改绑定 `app/binding.py`，交接 `app/switch_handoff.py`。
- **任务进程两端**：起进程 `app/runner_spawn.py`，收发 `app/runner_ipc.py`（`RemoteControl`、`RunnerLink`），认领和收尾 `app/detached.py`；文件名和收发原语 `core/run_dir.py`；退出前等的后台线程 `core/background.py`。
- **投递**：结算 `app/turn_settle.py`，发送 `app/turn_delivery.py` 的 `TurnDelivery`（`app/worker.py` 调它）和 `app/answer_delivery.py`，答案卡 `cards/answer_card.py`、`app/answer_cards.py`，重投 `app/redelivery.py`、`app/outbox.py`，恢复 `core/recovery.py`、`app/recovery_cards.py`。
- **看板**：`app/pins.py`、`app/status_cards.py`、`core/board_read.py`、`app/overview.py`、`app/topic_seen.py`。管家和回带见[管家](butler.md)，代答见[代答](proxy.md)。
- **整理专项**：`app/project_tidy.py` 的 `ProjectTidy` 是协调器，不继承任何类：`__init__` 里依次造出各部件（构造参数就是它的依赖），自己管后台线程（`tick`）和话题要点收集（`scan`、`saw`；实时归并已删），对外方法（`start`、`claim`、`matches`、`show_joined`、`web_*`）转给有它的部件。部件从下往上：纯函数和提议的认领 `app/tidy_facts.py`，共享状态 `app/tidy_state.py`（`busy`、`progress`、`counted`、`handed`），话题在卡上的字样 `app/tidy_words.py`，照办（建、归进、移出、合并、拆、收起，标话题的卡）`app/tidy_apply.py`，提议新专项、提议卡与跟着归并更新 `app/tidy_proposals.py`，定时或手动的一次整理与整理卡 `app/tidy_run.py`，卡片点击 `app/tidy_clicks.py`，网页面板 `app/tidy_web.py`。上面的部件只调下面的，`TidyPartsTest` 检查导入方向。

## 一条消息的数据流

```text
feishu/events.py consume：每个订阅一个 lark-cli 消费者，逐行交给回调
 → app/card_routing.py handle_event：配对；Access.admit；inbound-messages/ 去重；启用的扩展、管家或话题路由
 → app/topics.py Topics.route：core/workspaces.py 的 topic_context 找到或建立 topic.json，按 harness_id 选 BridgeApp
 → app/app.py BridgeApp.handle_event：附件、转发、被回复消息；有待答问题 → app/interaction.py 走问答；否则写历史
 → app/submission.py submit_topic_job：未开始 → waiting_workspace + 启动卡；已开始 → 排队卡；先写 queued/ 再入队
 → 前面的任务在跑（COVEY_TOPIC_STEER）→ app/steering.py steer_on_arrival：像点排队卡上的补充一样送进它，卡换成回执
 → core/scheduler.py WorkspaceScheduler：同话题串行，跨话题有上限地并行
 → app/job_dispatch.py process_reply_job：核对绑定和快照、预检、Harness.ensure_started
 → app/detached.py run_detached → app/runner_spawn.py start_runner：写 handoff.json，起任务进程
 → app/runner.py runner_main → app/worker.py process_bridge_reply_job
 → app/runs.py Runs：提示词（app/prompts.py）和 RunRequest → backends/<名字> 的 backend.run()
 → 后端事件 → Interaction.structured_harness_event → core/trace.py → cards/trace_view.py → Cards → app/task_card.py
 → RunResult → app/turn_settle.py settle_turn：保存 → 会话保护 → 授权
 → TurnDelivery.deliver：答案卡；管家回带挂钩 after_turn；失败留给 app/outbox.py
 → done.json、exit.json → Bridge 的 finish_detached 收尾，未结束的记录标 interrupted，原卡换恢复卡；再跑这一轮的收尾 after_job
```

### 主人消息的去向

主人的每条消息（飞书 `om_…`，网页 `web-<op_id>`）进来以后只有下面这些去向，`core/inbound_ledger.py` 的 `where(state_dir, message_id)` 读出来，只读：不加锁、不发请求、不建目录。`bin/covey_inbound.py where <id>` 和 `recent` 是它的命令行（`--state` 必填）。

| 去向 | 记录 | 主人看得见 |
| --- | --- | --- |
| 变成一轮（话题里的、管家的、网页发的） | `jobs/<消息 id>.json`（任务键就是消息 id）；条目的 `job` | 任务卡、答案卡、表情 |
| 开跑前并进排队的任务、或排队卡送进在做的任务（`COVEY_TOPIC_STEER`） | 本条的任务记录 `status=cancelled` 加 `merged_into_job_id`；补充的送达记录 | 补充回执卡、DONE |
| 管家的补充送进在做的一轮 | 送达记录 `interaction-deliveries/`；条目的 `supplement`（话题和任务号） | 开了同时做几件事是并进的表情；没开的现在也补上这个表情（以前没有） |
| 当成问题的回答 / 暂存等答案 | 问题记录、`held-notes/`；条目的 `answer`、`held_note` | 问题卡、暂存卡 |
| 停止词 | 条目的 `stop_word` | 停止的回复 |
| 斜杠命令、扩展认领 | 条目的 `command`、`extension` | 命令的卡 |
| 没处理：群里只有 @、没有读得出的内容、机器人身份未知、配对在群里 | 条目的 `ignored` 和原因（`Access.ignore`，只记主人的，群里没 @ 的不记） | 读不出内容的现在回一句；其余只有日志 |
| 被拒绝：话题冻结、回答被拒 | 条目的 `refused` | 话题里有说明 |
| 入队出错、没排上队 | 条目的 `error`、`not_queued` | 出错的现在回一句；没排上队本来就有说明 |
| 收下后没处理完（重启在中间） | 条目有 `at`、没有 `fate`，也没有任务记录 | 重放时发现的，回一句让主人再发（`lost`，只一次） |
| 重复投递 | 不新建；以第一次的记录为准 | 无需 |

条目是每条消息在 `inbound-messages/<sha256(消息 id)>` 里的一小段 JSON（原来是空标记，同一个文件，7 天后维护清掉，备份不收）：`at`、`chat_id`、`text`（前 40 字）、`fate`、`reason`、`topic_id`、`job_id`。写它尽力而为：从不抛错，只持进程内的一把小锁，锁内不发请求；读口先看任务记录和送达记录，再看条目，所以升级前的消息只要任务记录还在也查得到。一条消息的条目没有 `fate`、没有任务记录，过了 `STALE_SECONDS`（120 秒）就算“收下后没处理完”。

### 谁被服务

`app/access.py` 的 `Access`（`Runtime.access`，任务进程里也有）是唯一的判断处，别处不比较 open_id。

- **消息**（`admit`）：私聊只认主人，配对也只在私聊；群里只认 @ 了本机器人的消息（@所有人不算），发送者是主人或这个群已激活。被拒的记一行 `ignored message … reason=…`。
- **别人私聊**（`stranger`）：私聊里被拒的 `not_owner`，`app/strangers.py` 回对方一句纯文本“这是某某的个人助手，只处理某某的消息”，部署设了安装说明链接（`COVEY_INVITE_URL` 或扩展的 `invite_url`）时附上。每人每天最多一次：发之前 `O_EXCL` 建 `stranger-replies/<日期>-<sha256(open_id)>` 认领（日期按 Asia/Shanghai），没发出去也不重发。主人的名字是启动时 profile 里主人登录给的（`bot-identity.json` 的 `owner_name`），不知道时写“主人”。这句话不开话题、不发卡、不跑模型；`COVEY_STRANGER_REPLY=0` 时照旧只记日志。
- **群记录** `groups/<sha256(chat_id)>.json`（`core/groups.py`）：只有 Bridge 写（`group` 锁）。没有哪次 @ 会激活群：主人第一次 @ 时在锁外查群信息（`Access.owner_message`），外部群只记下；能做团队空间的（`team_allowed`）记 `pending_at`，等主人的话（`Access.pending`，期间按未激活的群服务）。`app/group_manager.py` 的 `ask` 在锁外往主人私聊（`owner_chat`）发一张启用确认卡，键由群和 `pending_at` 组成。一个群只发一张：锁内记 `asking_at` 认领，发出后记 `asked_at`、`asked_message`；没发出去（还没有私聊、投递失败）就去掉 `asking_at`，主人下次 @ 再发；`asking_at` 过了 `ASK_AGAIN`（10 分钟）还没结果的算丢了。确认卡的“启用”“先不启用”（`bridge_group_confirm`，`GroupManager.confirm`）在群还等着时就是 `Access.switch` 的启用、停用，定过以后再点什么都不改；群聊卡照样两头都能切，等着的群在那里也有这两个按钮。查询失败什么都不定；停用过的群不再问。旧记录照旧读：已激活的仍激活；等待中的记录在旧版本上读作未启用的群。
- **免@**（`COVEY_GROUP_ALL_MESSAGES=1`，见[群聊免@](configuration.md#群聊免)）：群记录的 `free_talk` 和 `free_talk_members` 决定谁不用 @（`talks_freely`）。免@ 不扩大谁被服务；对别人说的（`for_others`）、代答群（`proxy_chat`）不免@。
- **群成员只读**（群记录的 `member_mode`，没选过的按 `COVEY_GROUP_MEMBERS`，不设是 `full`）：`Access.read_only(chat, sender)` 说一轮是不是只读：团队空间里不是主人自己的一轮（`owner_turn`），且这个群的群成员只读；没留发送者的任务在已启用的群里算群成员的，只服务主人的群里算主人的。发送者跟着任务走：等启动卡的消息（`job_from_record`）、重试、检查并继续、回答后的下一轮（任务进程经 `enqueue_answer` 的 `sender_id` 交给 Bridge，旧任务进程不带时按没留发送者算）。怎么落到 Harness 上见 [Harness 接口 · 一次运行](#一次运行) 的 `TOOL_LIMIT`。
- **点击**（`allows`）：主人，或已激活群里的任何人。工作区管理、目录选择和菜单只给主人（`is_owner`），其他人点了在卡上拒绝（outcome `owner_only`）。
- **空间**（`core/spaces.py`，设置见[空间](configuration.md#空间spaces)）：`owner_accounts` 里的账号都是主人；`allow_team_spaces` 为 false 时没有群被激活，也不发启用确认卡；`personal_groups` 里的群只服务主人。默认设置下这些都不改变上面的规则。通知（早晚汇总、手上的事、告警）发到 `Access.notify_chat()`。个人群和 `owner_accounts` 的私聊各有管家（`Access.agent_chat`，`ButlerEnv.chat()` 随工具调用所属的管家），个人群有置顶菜单卡（`app/group_menu.py`，记录在 `<state>/group-menu.json`）和自己的看板记录（`app/group_boards.py`、`app/pins.py` 的 `record_path`；手上的事、话题看板、早晚汇总的记录名带 chat_id，`notify_space` 那个群用不带 chat_id 的默认那份）。
  - 记录里仍只有一个 `owner_open_id`；只有主人本人的私聊算“主人私聊”。`owner_accounts` 的私聊记在 `<state>/owner-chats.json`（open_id → chat_id），不覆盖主人本人的 `owner-chat.json`。
  - `<state>/spaces/<sha256(chat_id)>.json` 记每个个人群的类型和标题，只给设置卡「群聊」页显示，不参与判断。一个 chat 是什么空间由 `Access.space_of()` 回答。
- 代答用 `owner_user_id` 认出主人自己发的消息，不是谁被服务的判断。

## 卡片点击的数据流

```text
feishu/events.py card.action.trigger
 → app/card_routing.py handle_card_action_event：Access.allows；去重
 → EARLY 表（app/unified.py）：按事件认领——启用的扩展的卡片、工作区管理、启动卡模型选择、目录选择、换配置、回退
 → PANELS 表：按 value 认领菜单面板——代答、群聊、脱敏、管家、设置卡……
 → 话题和任务路由：任务进程活着 → app/runner_ipc.py forward_card_action 写进它的 inbox（outcome forwarded）；
     否则 BridgeApp.handle_card_action
 → app/card_actions.py ACTIONS[kind]：handler 指向组件方法；bot_only 的只在 Bridge 处理
 → 组件 3 秒内应答；慢的后续经 app/card_lanes.py 的 after_ack 排进 app/card_queue.py：同话题按序，跨话题并行
```

- 每次点击记一行 `card action kind=… outcome=… elapsed_ms=…`。
- 任务进程活着时独占本任务的卡片和记录：除 `bot_only` 按钮、启动表单提交和问题卡反馈，点击都转给它；文字回答和补充限时等确认，超时按送达未确认处理、不重发。会建新任务的操作（重试、补充重新排队、暂存消息排队）只在 Bridge 处理。
- 已下线按钮（侧聊、分叉、压缩上下文）的旧卡点击由 `app/task_controls.py` 的 `retired_action` 回一句。按钮和客户端限制见[事件、执行过程与卡片](cards.md)。

## 任务生命周期

### 状态

`jobs/<任务键>.json`，任务键是 `safe_slug(消息 ID)`；续答任务的消息 ID 是 `answer-<问题 token>`，暂存消息排队 `note-<token>`，补充重新排队 `requeue-<token 前 24 位>`。`core/statuses.py` 的 `JOB_TRANSITIONS`：

```text
waiting_workspace → queued, cancelled, failed                 话题未开始，等启动卡
queued            → running, waiting_user, done, cancel_requested, cancelled, interrupted, failed
running           → done, failed, waiting_user, cancel_requested, cancelled, interrupted
cancel_requested  → cancelled, interrupted, failed, done      用户点了停止
waiting_user      → continued, running, cancelled, interrupted 本轮结束，等必须回答的问题
continued、done、failed、cancelled、interrupted：终态
```

- 写入只经 `core/jobs.py` 的 `upsert_job_record`：`record` 锁内读改写、`revision` 加一、原子替换；表外的状态变化被丢弃，其余字段照常合并。按当前状态比较后再改用 `transition_job_record`。
- 越过转换表的只有：显式续接（`continued` 带 `resumed_by`）；投递修复（有保存正文的 `failed`/`interrupted` → `delivery_ok=True`）；延后问题的控件投递失败（→ `failed`）。
- `execution_status` 是执行事实，`status` 加 `delivery_ok` 是整体结果。没有后端运行过的任务记 `never_started=true`，统计时排除。
- 状态分组：`TERMINAL`；`ENDED`（`TERMINAL` 加 `continued`）；`NOT_ENDED`；`RUNNING = {queued, running, cancel_requested}`（重启中断、任务进程收尾、`restart_blockers` 都用它）；`WAITING`。
- 字段声明在 `core/jobs.py` 的 `BridgeReplyJob`。表单记录的状态变化列在 `FORM_TRANSITIONS`，只由测试核对。

### 排队与任务进程

`queued/<任务键>.json` 存整个任务，交接、取消或注销时删除；`job.execution_lock` 保护出队到交接。`runs/<任务键>/` 的文件名和收发在 `core/run_dir.py`：

| 文件 | 内容 |
| --- | --- |
| `handoff.json` | 重建参数和序列化任务 |
| `runner.json` | 任务进程身份和 release；Bridge 写，任务进程只在它没写对自己时补写（`registered`） |
| `wake.json` | 任务进程的身份；Bridge 每写一次收件箱就经 pidfd 发 SIGUSR1（`wake_runner`），旧 release 照旧轮询 |
| `runner.lock` | 运行锁：任务进程拿到交接后加排他锁（flock），一直持到退出；设了 `COVEY_LOCK_ROOT` 时锁文件在那里（`core/file_lock.py` 的 `open_lock`） |
| `inbox/` → `acks/` | 点击、回答、补充、重启通知、转给主人的活问题（`show_question`）与确认 |
| `requests/` → `replies/` | 续答入队请求 |
| `done.json`、`exit.json` | 只剩产物上传时、退出时 |

- 每条消息是原子写入的一个 JSON 文件，读方按文件名顺序取一条删一条。
- 预先拉起：接着就运行的任务在建任务卡之前由 `prestart_runner` 先起任务进程，它预热后等写明同一随机串的 `handoff.json`，30 秒没等到就退出。交给它时 `start_runner` 先写 `runner.json` 再写 `handoff.json`，任务进程拿到交接时 `runner.json` 已经写着自己，不再写一遍；两边同时替换这个文件时，ByteFuse 上 Bridge 那一读会失败，把刚接手的任务进程当成没起来。不是预先拉起的，Bridge 起进程后才知道 pid，仍是先写 `handoff.json`、起进程、再写 `runner.json`；任务进程读到时多半已写好，没写好（Bridge 起完进程就停了）才自己写。
- 任务进程从不自己建任务，续答经 `RunnerLink.enqueue` 请 Bridge 入队。`runner_argv()` 把路径改成绝对路径，`runner_env()` 清理 `PYTHONPATH`。任务进程里的 `enqueue_chat_job`、`reserve_submission`、`TaskControls.requeue` 一开头就拒绝（`Runtime.refuse_new_job`，`任务进程不新建任务`），什么记录都不写、什么也不发。
- 一轮的收尾只在 Bridge 跑（`app/detached.py` 的 `after_job`：知识条目、记忆、代发草稿、话题转交管家、协作、回带里的话题卡归属和专项回执、到点回来看）：任务进程退出后 `finish_detached` 跑一次；任务就在 Bridge 进程里跑时（没有任务进程，如测试），`JobDispatch` 在这一轮之后跑。任务进程也经 `JobDispatch._process_reply_job` 跑这一轮（它的 `runtime.detached()` 为假），`after_job` 在那里什么都不做：这些写的是别的记录、发的是别处的消息，转交管家还要排管家的一轮，任务进程只改本任务的卡和记录。在任务进程里跑过一遍时，转交管家发出了顶层消息，管家那一轮按任务进程自己的编号（#1）记成 `queued` 后被拒，没人排，部署一直等它。交给任务进程时 Bridge 记一笔 `turn-ends/<任务键>.json`（`end_due`），收尾做完删掉（`end_done`）；任务进程在没有 Bridge 看着时退出的（部署、重启的空档），下次启动在认领和中断标记之后补做（`end_stranded`，从 `handoff.json` 取任务），还活着的等它退出，Harness 已下架的或任务进程结束超过 `ENDS_KEPT`（24 小时）的丢弃不做。
- 入队失败：记录已经写成 `queued` 而 `enqueue_reply_job` 抛错时（`publish_submission`、`requeue`），`TaskControls.not_queued` 把记录改成 `failed`（`没排上队，没有执行：<错误类型>`），卡换恢复卡（发不出卡就在话题里说一句），错误照旧抛给调用方；不留没人排的 `queued`（`restart_blockers` 会一直等它）。
- 停止不只靠这一轮的线程：`cancel_reply_job_from_card` 设好 `cancel_event` 后、写卡和记录之前先 `hard_stop_later`，`HARD_STOP_AFTER`（5 秒）后后端进程还在就给它的进程组发 SIGTERM。平时这一轮的线程一两秒内自己看到停止、先关掉后端，这一下什么也不做；盘卡住时那条线程可能在记录锁上等几分钟，后端照样停。停止之后、后端还没做完时后端抛什么都按停止结算（`worker._turn_from_exception`）。
- `SIGTERM` 是中断，不是停止（`app/runner.py` 的 `interrupt`）：没做完的一轮不结算（`leave_interrupted`），记录留在 `running`，退出后由 Bridge 标 `interrupted`；已做完的结果照常投递。
- 同一个任务同时只有一个任务进程在跑（运行锁）：任务进程读到交接后、写任何东西之前对 `runner.lock` 加排他锁（`hold_run_lock`），一直持到退出，进程不管怎么结束，锁都由系统放掉。`RUN_LOCK_WAIT`（3 秒）内拿不到，说明这个任务已有一个任务进程在跑：它在 `runner.log` 记一行 `task runner job=#N pid=… ran nothing: another task runner holds its run directory` 就退出，不写 `runner.json`、`wake.json`、`done.json`、`exit.json`，不碰记录和卡。Bridge 看锁（`run_lock_held`）只试着加一下共享锁、马上放开，不建锁文件；任务进程加锁时会重试，不会因为碰上 Bridge 这一看就放弃。锁文件建不出来或文件系统不支持加锁时，任务进程记一行 `runs without a run lock` 照旧跑，只靠 Bridge 的判断（和没有运行锁之前一样）。同一进程问自己持有的锁不再打开文件：NFS 上锁属于进程，关掉同一文件的另一个描述符会把锁一起放掉。旧 release 的任务进程不加这把锁，对它们只有 `runner.json`、`wake.json` 两个文件说了算。
- 没起来：`watch_until_exit` 看到任务进程没了，`relaunch_unstarted` 先再问一次它在不在，等 `RELAUNCH_DELAY`（2 秒）后在任务的锁里再问一遍，并重读记录：仍是 `queued`、目录里没有 `exit.json` 和 `done.json`、也没有这个任务的保存结果索引（`core/saved_results.py` 的 `run_began`）、收件箱是空的，才再起一次（每个任务一次，`runner_relaunched`），新进程的卡片 sequence 接着重读到的记录。结果分三种：还活着（`ALIVE`，日志 `task runner of job #N still alive: watched again`，回去接着看，不进 `finish_detached`）、再起了一个（`RELAUNCHED`）、都不是（`ENDED`，`finish_detached` 收尾）。不再起的（记录已不是 `queued`、已经再起过、收件箱里有东西）收尾前隔 `GONE_AGAIN`（0.5 秒）再看一次（`_gone`），`release_done` 等上传完的那条线程也一样：一次看的几回读都失败，不能把活着的任务进程的任务标成中断。`start_runner` 抛错时 `not_started` 把记录写成 `failed`（`runner_start_failed`）并出恢复卡。`start_runner` 清目录前先看里面有没有活着的任务进程（`runner_here`），有就什么都不动、抛 `RunnerAlive`：再起时当作还活着，第一次交接时当作认领到的接着看，不碰它的记录。目录里没有 `handoff.json` 的活进程不算：那是还在等自己那份交接的预先拉起的进程（它的 Bridge 写完 `runner.json` 没来得及写交接就停了），什么都不跑，30 秒后自己退出，也不接不写着它的交接。
- `done.json`：Bridge 见到它（`job_done`）先交还调度槽位，同话题的下一条照常进行；点击仍转给活着的任务进程；outbox 和自动重试等它退出。
- 遗留进程：任务进程把 `COVEY_TASK_DIR` 设为运行目录，每条命令都继承。后端返回后 `app/leftovers.py` 用 `core/task_processes.py` 找出带标记、已脱离任务进程的存活进程：搜索工具（`SEARCH_TOOLS`）先 SIGTERM 再 SIGKILL，其余只在执行过程里列出。内存看门狗也读这个标记，把告警发到任务所在的话题。
- `runner_alive` 要求 pid 的启动时间、boot ID 和记录一致且 cmdline 含运行目录。读 `runner.json` 失败（`OSError`，或 JSON 不完整）而目录还在时，隔 10、40 毫秒再读（`read_steady`，`READ_PAUSES`），目录不在不等；认领时读 `handoff.json`、任务进程读交接、`end_stranded` 也这样读。决定再起一个或收尾的地方（`watch_until_exit`、`relaunch_unstarted`、`start_runner`、`prestart_runner`），以及重启认领（`adopt_runners`）和 Bridge 正看着的任务的点击转发（`runner_for`）用 `runner_here`：`runner.json` 说不活时，再看 `wake.json` 写的进程，最后看运行锁有没有人持着。前两个都是读 NAS 上的文件，ByteFuse 上读一次可能失败；锁不靠读文件，所以几次读都失败时 Bridge 也不会把还在跑的任务进程当成没了，清它的目录再起一个（两个任务进程写同一张卡，卡片更新会被飞书以 300315 Duplicate ID 反复拒绝）。
- 到点回来看（默认开，`COVEY_REVISIT=0` 关，`app/revisits.py`）：一轮结束后 Bridge 读它检查点的 `revisit`（几分钟后、看什么），写 `revisits/<话题>.json`；`revisits` 线程每 30 秒看一遍，到点且话题没有在跑、排队或等回答的任务时，先在锁内记 `fired` 和这一轮的消息 ID（`revisit-…`），再在锁外像 `app/project_loop.py` 叫醒负责人那样把它当话题里的一条消息入队；记了没入队的 120 秒后重排，Bridge 停着时结束的那一轮由线程补结算。检查点写 `"unchanged": true` 又约了下一次的那一轮，任务进程里 `TurnDelivery` 不发（`revisits.silent`，同专项叫醒的 `silent`，保存的回复算已投递，也不回带）。没有变化以外的结果、最后一次和第 24 次照常投递。

### 重启认领

`UnifiedBridgeApp.main` 先拿状态目录的实例锁，迁移旧记录（`core/migrate.py`），在后台线程预读任务和问题记录（`warm_records`，下面第 1 步要读全部，和 `init_target` 的飞书调用同时读），再 `init_target` 核对应用身份；开着 `COVEY_EVENT_TRANSPORT=sdk` 时紧接着连上事件长连接（`start_intake`，见[事件长连接走 SDK](#事件长连接走-sdk默认关)）。然后：

1. `recover_question_delivery` 核对问题记录；`recover_worktree_launches` 把建 worktree 时被打断的启动卡换回表单。
2. `adopt_runners` 按 `handoff.json` 认领存活任务进程（`runner_here`：`runner.json` 写的那个进程没了，`wake.json` 或运行锁说还有一个在跑也认领），往 inbox 写 `bridge_restarted`；再排上 `queued/` 里的任务。
3. `recover_stale_jobs` 把其余未结束记录标 `interrupted`；`close_retired_jobs`；`release_started_launches` 排上已绑定话题里还在 `waiting_workspace` 的消息；`queue_stranded` 排上任务进程留下的补充；`end_stranded` 补做 `turn-ends/` 里任务进程在没有 Bridge 看着时退出的那几轮的收尾（`after_job`，日志 `turn end run at start job=#N`），只读这一个通常为空的目录。

这三步做完才起后台线程和事件消费者：认领、标中断和核对问题都要在处理第一个点击或消息之前。所以这几步的耗时，就是重启后点卡片没反应（lark-cli 收事件）或先收下、稍后才处理（SDK 收事件）的时间。为让它在几秒内，这几步遵守：

- 哪些任务进程还活着（`core/run_dir.py` 的 `live_keys`、`live_dirs`，启动时要问四次）只问 `/proc` 里有本用户进程的命令行提到的 run 目录（`argv_dirs`），不逐个读 `runs/` 下上千个 `runner.json`（NAS 上每次几秒）；读不了 `/proc`，或调用方给了自己的判断函数时，照旧逐个问。
- 问题核对一次建好任务的索引（`_lookup`），不为每个问题扫一遍全部任务；已答问题和后续任务已经一致时不再加锁重读、重写。
- checkpoint 问题的核对（每个 Harness 一次）先按索引里的状态只取待答问题和它们的任务，有可能收起一张时才取全部（`_older_duplicate`）。
- 启动卡、补充记录这类每条一个文件的目录用 `core/state.py` 的 `read_each` 并行读。

### 中断与恢复卡

- 卡由 `app/recovery_cards.py` 画（kind 都是 `bridge_job_retry`），状态、按钮和点击后做什么由 `core/recovery.py` 的纯函数 `classify` 判定；按钮和点击之间有意保留的分歧写在该模块开头。
- 已授权或已保存的正文没送达 → 重新投递结果；暂停问题没送达 → 重新投递问题；其余失败或中断 → 检查并继续（按原模型快照新建任务）；未授权、已送达、需核对或已 `native_answer_once` → 无按钮。
- 话题还有运行或排队的任务时拒绝；每条记录只重试一次（`retried_by`）。任务没跑完时告诉用户：`app/task_controls.py` 的 `report_unfinished`。
- **死掉那一轮的子 Agent**：失败或被打断的 Claude 一轮（不在设备上）封了原生会话，下一轮（检查并继续、自动重试、主人的下一条消息）是新会话。`app/subagent_recovery.py` 读那一轮的 `<会话>/subagents/agent-*.jsonl`（`backends/claude/subagent_report.py`），每个子 Agent 取描述、结局（完成、出错、没做完）和最后一段文字（至多 1500 字），最多 8 个写进恢复提示的一块“已交付，别重做”，没做完的优先，其余全文写在那一轮运行目录的 `subagents.md` 并写明路径；再死一轮时往后带。读不到、格式不认识就没有这一块，不拒绝重试。敏感词脱敏的话题照常带：这一块在提示里，和历史、上一轮回复一样经本轮的脱敏中继出去。
- **原因和下一步**：没成功的一轮（失败、超时、重启打断、官方拒绝、任务进程没能启动；主人自己停的不算）由 `core/failure.py` 的纯函数 `failure_view(record)` 给出 `{title, detail, actions}`：`title` 一句人话，说原因和接下来会怎样或能做什么；`detail` 收内部用语（记录的状态行、错误码、子代理标识、任务进程启动时的异常）；`actions` 是 continue（检查并继续）、retry（现在重试，同一个点击）、diagnose（看原因）、split（拆小再试）、switch_model（换配置）里这条记录能做的。记录靠 `failure_code`（这一轮 `ExecutionOutcome` 的错误码）和 `failure_detail`（谁在做什么时被拦）说原因，二者由 `app/worker.py` 写（守护停的由 `app/policy_guard.py` 写 `failure_detail`）；更早的记录按状态和状态行推断。
  - 任务卡：失败的一轮正文换成这句（`TurnDelivery.card_text`，加粗一行），原来的那段话收进正文下的“技术详情”（`failure_details`，元素 `failure_details`），再是按钮行（`auto_retry.failed_controls`）：限流的检查并继续叫“现在重试”，另加“看原因”“拆小再试”；换配置用已有的“换配置”。限流（`rate_limited`）和额度用完（`quota_exhausted`）在“换个模型”后面再加“换个 Harness”（`auto_retry.other_harness`：另启用了别的 Harness、话题没开脱敏、记录有绑定；`cards/recovery.py` 的 `OTHER_HARNESS`），点击是同一个 `bridge_switch_open` 加 `other`，`app/harness_switch.py` 的 `other_harness_key` 在换配置卡上预选另一个 Harness 里排在最前、`app/availability.py` 的 `usable` 认为现在能用的模型；没有就回一句 `NO_OTHER_HARNESS`，不出卡。从不自动换，主人点“确认更换”才换。官方拒绝的正文本来就写谁在做什么时被拦（[policy-stop.md](policy-stop.md)），正文不换，只加“拆小再试”（换旧模型重试排上时什么都不加）。恢复卡（中断的）正文第一行也是这句，按钮后加同样的“看原因”“拆小再试”。
  - 恢复卡（`cards/recovery.py` 的 `recovery_view`）：开头是任务卡的标题行（`cards/streaming.py` 的 `task_head`：T 号头像、这一轮的标题、“停住了”标签，docs/ui.md 5.2），下面粗体一行原因、灰字一行下一步（`failure.split_title` 按第一个“；”拆，原因超过 40 字再在“，”处拆），没有结论带；被自动重试接走的（`moved_to`）标题行写“↪ ”加怎么接走的，下面一行 `MOVED_LINE`，只留有记录的执行过程；凡是 `failure_view` 给得出原因的（中断、官方拒绝、结果没发出去、任务进程没能启动）都用它的话，`detail` 收进卡底的“技术详情”（折叠），正文不出现异常名和内部状态串；正文只写检查点里模型写过的停在哪一步、已做完的和下一步（`app/recovery_cards.py` 的 `checkpoint_lines`，Bridge 垫的初始检查点 `SEED_PHASE`、`SEED_NEXT` 不写），“检查并继续”做什么收在按钮下面一行“怎么继续”里（`app/auto_retry.py` 的 `button_note`，失败的任务卡同一行）。
  - 回带的“停住了”（私聊顶层，`app/relay_stops.py`）：原因和下一步也是这句（不是记录的状态行），下面是任务卡的“检查并继续”（按记录叫“现在重试”“重新投递结果”）、“看原因”“拆小再试”和“换个模型”（记录有绑定才有，`cards/recovery.py` 的 `switch_route`）。按钮就是任务卡的 kind，值里多一个 `relay`，走同一个处理和同样的过期检查；点完回带卡原地重画（`Messenger.patch_card`），按钮换成一行灰字“↪ 已接着做 · 任务 #12”或没做的原因，不在话题串里回话。“换个模型”是换配置卡的 `bridge_switch_open`，另带 `prefer_other`：换配置卡开在私聊顶层（见[话题中途换配置](#话题中途换配置)），预选另一个 Harness 里现在能用的模型（`other_harness_key`），没有就预选话题自己的，照样出卡；在那张卡上确认换了绑定后，回带卡原地重画成一行“↪ 已换配置：<新模型>；回复这张卡说一句，就按新配置接着做”（`relay_stops.switched`，回带记录 `clicked` 记 `switch_model`）。
  - “看原因”（`bridge_job_diagnose`）和“拆小再试”（`bridge_job_split`）都是 `bot_only`，由 `Submission.follow_up` 在本话题排一轮：话题里显示“看原因：任务 #N 为什么没成功”“拆小再试：任务 #N”，模型读到的是 `core/failure.py` 的 `follow_up`：看原因是只读排查（不改文件、不重跑、不动任务和服务，最后说清哪一步、为什么、建议怎么做），拆小再试先核对已做的，再一小块一小块做，被拦的那块先跳过。话题还有任务在跑或排队时不排，告诉主人；这条记录不提供这一步时也不排。
  - 网页：每轮的 `failure` 字段同一个 `failure_view`，另带每个按钮的叫法、网页怎么做和排好的自动重试（`app/web_turns.py` 的 `failure_of`）；网页的按钮走 `recover` 操作（`app/web_ops.py`），和任务卡的点击是同一个调用（[web.md](web.md#网页发回的操作)）。

### 重启与维护

- 没有热重载，新代码靠重启；`SIGTERM`/`SIGINT` 只停 Bridge，停前等卡片分道里已开始的点击（`CardLanes.drain`，最多 6 秒）。事件长连接走 SDK 时，收下没处理的消息和点击留在 `event-spool/`，下次启动先补处理（[收下就落盘](#事件长连接走-sdk默认关)）。
- `restart_blockers`（`core/run_dir.py`）列出此刻重启会打断的任务：未结束、无存活任务进程、又不是已存盘的 `queued` 或 `cancel_requested`。
- 维护（清什么见[备份、归档与维护](operations.md#备份归档与维护)）：计划与执行在 `core/maintenance.py`；新的一类落盘数据在 `core/maintenance_rules.py` 的 `RULES` 加一条，再排进 `CLEANUP` 和 `DRY_RUN`；备份在 `core/backup.py`。备份不持锁、每天一次；其余清理等没有任务，归档每步在 `LOCK` 内。
- `SharedFileLock` 拿到锁后核对锁文件仍是同一个 inode，被删过就重开。陈旧的等待记录由 `app/stale.py` 按转换表转 `cancelled`。

## 状态与并发

同时跑几个任务：`core/scheduler.py` 的 `WorkspaceScheduler`，每个话题一次一个、话题之间先来先跑，最多 `COVEY_MAX_WORKERS`（默认 32）个；其中 `RESERVED`（2）个只给管家和专项负责人那一轮（`keeps_slot`），普通话题占满其余的也挤不住私聊和专项。排队提示的“并发已满（最多 N 个任务）”和调度器读同一份（`room_for`）。

### 主要记录与锁

完整目录见[数据目录](operations.md#数据目录)。

| 路径 | 内容 | 写入方 | 锁 |
| --- | --- | --- | --- |
| `jobs/` | 任务记录 | Bridge、任务进程 | `record`，跨进程 |
| `questions/`、`held-notes/`、`interaction-deliveries/` | 问题、暂存消息、补充投递 | 两者 | `question`，跨进程 |
| `topics/<topic_id>/topic.json` | 绑定：工作区、`harness_id`、`binding_revision`、模型快照 | Bridge | 改写持 `LOCK`，进程内（建话题、给旧话题编号、钉 Harness 另持 `_LOCK`）；读不持锁 |
| `topics/aliases/` | 消息、卡片和话题串到话题的对应 | Bridge | 每个别名文件一把进程内锁；读不持锁 |
| `topics/<topic_id>/*_session_id` | 原生会话指针，比较 revision 更新 | 两者 | 同名 `.lock` 的 flock |
| `verified-results/` | 已验证回复的候选与授权 | 两者 | 每个任务一把：`.locks/` 里挨着它索引的锁文件，跨进程；一个任务卡在锁里只挡它自己 |
| `native-session-guards/` | 会话保护 | 两者 | 目录下 `.lock` 的 flock |
| `inbound-messages/` | 每个收到的消息 ID 一个空标记，飞书重投时跳过 | Bridge | 无：`O_EXCL` 新建 |
| `event-spool/` | 事件长连接走 SDK 时，收下还没处理完的消息、卡片点击和菜单点击，每个一个文件，处理完删 | Bridge 的 `event-spool` 线程 | 无：只有这一个线程写，整文件替换 |
| `stranger-replies/` | 别人私聊时回过的那句：每人每天一个空标记 | Bridge | 无：`O_EXCL` 新建 |
| `revisits/` | 到点回来看（`COVEY_REVISIT`）：每个话题一个，等到点（`pending`）或那一轮已排出（`fired`） | Bridge | `revisit`，进程内 |
| `limit-notices/` | 几个话题同一原因停下的合并提示（`COVEY_LIMIT_NOTICE`）：每组（私聊、Harness、原因）一个，`sending` → `sent` 或 `uncertain`，不再发第二次 | Bridge（outbox 循环） | `record`（锁内不调飞书） |
| `answer-cards/` | 单独发出的答案卡：要发的卡（`outgoing`）、发出的卡和产物状态 | 两者 | `answer-cards`，跨进程；锁内只读写这个文件 |
| `fold-cards/` | 带“这一轮中途的汇报”折叠的回带卡，按消息一个，点正文收起时改写（`app/fold_close.py`）；同一张卡也记在 `answer-cards/` 时以那份为准、两份一起改 | Bridge | 无：点击在这张卡所在的卡片队列里依次跑 |
| 表单目录（`launch-forms/` 等）、`cards/.locks/` | 卡的 card_id、sequence、表单状态；卡片锁文件 | 发卡的进程 | 卡片锁 |
| `groups/` | 群记录 | Bridge | `group`，进程内 |

- **索引**：`core/store.py` 的 `JsonDirectoryStore` 给 `jobs/`、`questions/`、`held-notes/` 建内存索引，读目录的 `.journal` 追变化，定期列目录兜底；查询字段（`INDEXED`）常驻，整条记录被选中才读；`.index` 让新进程快速起步；`CARRIED`（`updated_at`）随 `.index` 一起存、不建索引项，查询的 `narrow` 在读记录之前先用它筛（管家快照、手上的事等的 `board_read.read` 靠它只读三天内的记录），旧 release 写的 `.index` 没有这一项时，记录自己决定，结果不变。手工改记录后调 `invalidate_directory`。一条消息、一次点击在 `one_scan()` 内处理。读盘（`.journal`、列目录、读记录、`.index`）都不持内存锁 `lock`，NFS 打开 `.journal` 卡过几分钟，所有读写记录的线程曾跟着等：同一时刻只有一个线程追（`refreshing`），别的线程等它至多到它开始后 `PATIENCE`（1 秒）；更久的当作卡住，照上一次追到的回答，本进程刚写的记录按名字重读。写记录和 `invalidate_directory` 从不等读盘。还没追完过一次（新进程起步）、要读只知道字段的记录、或有说不出名字的写入时照旧等它，`bounded` 内等超时抛 `Busy`。几个线程差不多同时读同一个文件、读到的不一样时，以后开始的那次为准，分不出先后就再读一次。
- **资源锁**：`core/locks.py` 的 `resource_lock(namespace, key)`。`record`、`question`、`answer-cards`、`card` 跨进程（进程内锁加 flock），其他命名空间只在进程内。
- **持记录锁不发网络请求**（规则 1）：锁内写意图（`submitting`）并存内容，锁外发送，返回后再转状态，不确定转 `uncertain`、从不自动重发。例外：实时问题的回答在后端的 `control_lock` 内核对并发送。问题表单在每个问题一把的 `question_card_lock` 内发送。
- **`LOCK` 和话题锁**（规则 2）：`LOCK` 只保护工作区配置、`topic.json` 改写和要看所有话题的检查，从不跨飞书调用持有。同话题的提交、启动、重试、换配置由 `topic_lock(ctx)` 排队：先话题锁、再短暂拿 `LOCK`，反过来直接报错。
- **读不持 `LOCK`、`_LOCK`**：`topic.json`、别名和工作区列表（`workspace-config.json`）都整文件替换，不加锁读到的也是完整的某一版。NFS 上一次打开可能挂住几分钟（D 状态），持锁读时所有点击和私聊消息的路由都跟着等。所以点击和消息的路由（`context_for_topic` 先不加锁查找，要建话题、写新别名、钉 Harness 时才拿 `_LOCK` 再查一遍）、`configured_context`、`load_config`、管家的 `write_topic`（没有要改的就不拿锁）都不持锁读；别名的写入各用自己文件的锁。话题编号读到一次就记在进程里（编号给了就不变），专项卡每秒取每个成员的编号也不再读盘；已有话题的路由不读工作区列表，只有新话题读。提交登记仍在 `LOCK` 内读 `topic.json`（维护和换配置要同时看到登记），但 `harness_locked` 已在时不再每条重写。
- **卡片锁**：只串行同一张卡，跨进程；卡锁内只套同一张卡的记录锁，从不在记录锁或 `LOCK` 内申请卡锁，所以没有等待环。

### 卡片 sequence

CardKit 只接受比同一张卡已有写入（含结果不确定的）都大的 sequence。`core/card_sequence.py` 是唯一选 sequence 的地方，卡锁内 `reserve` 取下一个。

- 下一个 = 最后一个 + 1，先存盘再发；失败或不确定也不回退、不复用。uuid 含 sequence，重试不复用 uuid。
- 别的进程也可能写过的卡（按记录重绘的任务卡、浏览卡、问题卡）起点取存盘值和当前秒数的最大值；只有 Bridge 写的启动卡和工作区管理卡以 `shared=False` 打开。
- 实时任务卡（`app/task_card.py`）在任务记录的 `card_sequence` 里预存一段上限（`LEASE`）；迟到的写被拒 300317 时按记录 `adopt` 再重写一次，其他失败不重写。

## 最容易改坏的规则

`AGENTS.md` 列规则原文；这里写每条防什么、落在哪。

| # | 规则 | 防什么 | 代码与章节 |
| --- | --- | --- | --- |
| 1 | 持记录锁不发网络请求，不确定不重发 | 飞书卡住时挡住同一记录的所有读写；重复消息 | [主要记录与锁](#主要记录与锁) |
| 2 | `LOCK` 不跨飞书调用；持 `LOCK` 不申请 `topic_lock`；读不持 `LOCK`、`_LOCK` | 一个话题拖垮全部；一次挂住的读挡住所有路由；锁顺序反转死锁 | `core/locks.py`、[主要记录与锁](#主要记录与锁) |
| 3 | sequence 从 `reserve` 取；发送经 `Messenger`；任务卡只由 `Cards` 写 | CardKit 拒收、旧写盖新写 | [卡片 sequence](#卡片-sequence) |
| 4 | 状态只按转换表前进，终态不回退 | 晚到的心跳把完成的任务改回运行 | [状态](#状态) |
| 5 | 只发经 `verified_reply` 保存并授权的回复 | 过程文字被当成答案；重投时再跑模型 | [结果授权与投递](#结果授权与投递) |
| 6 | `RunHandle` 的写不自动重放 | 补充或回答送达两次 | [一次运行](#一次运行) |
| 7 | 后端不导入 `app/`、`feishu/`、`cards/` 和别的后端 | 后端绑死飞书；扩展的隔离解释器里导入失败 | [硬约束](#硬约束) |
| 8 | 新按钮 kind 先进 `ACTIONS`，建任务的设 `bot_only` | 点击没人认领；任务进程里建不了任务 | [卡片点击的数据流](#卡片点击的数据流) |
| 9 | 表单变化整卡替换；卡片 JSON 变了要客户端验收 | 单个组件 PUT 后表单失效；看不出的渲染问题 | [cards.md](cards.md) |
| 10 | 兼容旧配置、旧 `runs/`、旧记录、旧卡 | 部署时在跑的任务进程是旧 release；回滚 | [跨版本检查](testing.md#任务进程协议与跨版本检查) |
| 11 | 谁被服务只经 `app/access.py` | 各处各自比较 open_id，规则漂移 | [谁被服务](#谁被服务) |

## Harness 接口

### 一次运行

- 契约在 `core/harness.py`：`backend.run(RunRequest) -> RunResult`；`session_id` 为空即新会话；`BridgeContext` 由 `core/jobs.py` 的 `context_for_job` 按任务快照生成。回调只从请求读：`request.observers`、`request.publish`、`request.emit`、`request.check_cancelled()`。
- `RunCallbacks.session(sid)` 一拿到原生会话 ID 就报告，共享层立即建会话保护；`verified_reply(None)` 作废候选并推进代际。
- `RunHandle` 的写绝不自动重放：`steer` 抛 `SteerRefused`/`SteerNotReady` 是没发出，其他异常是可能已送达。暂停型后端用 `wait_for_answer` 中断回合。共享层还读 `supports_steer`、`awaiting_answer`、`control_lock`、`verify_question_request(proof)`。
- `capabilities(ctx)` 的 `HarnessCapabilities` 决定任务卡控件、话题消息能否当回答、能否复制原生会话、自动重试（`rate_limit_retry`、`fault_retry`、`incomplete_retry`）、回退（`rewind`）、脱敏（`redaction`）、子 Agent 恢复读会话文件（`session_files`）、能否把一轮限在只读（`read_only`）。
- **一轮能做什么**（`core/harness.py` 的 `TOOL_LIMIT`，`app/tool_limits.py` 的 `limit` 决定，`Runs._prompt` 在这一轮任何东西跑之前写进上下文的 `options`）：`read` 是只读群里群成员的一轮（Claude Code `--tools Read,Grep,Glob` 加空的 `--strict-mcp-config`；Codex 只读沙箱，`turn/start` 另带 `sandboxPolicy`，app-server 关 apps、plugins、memories、hooks，续接原线程时在不含执行规则、钩子、插件和记忆的私有 `CODEX_HOME` 里跑（[harnesses.md 执行权限](harnesses.md#执行权限)）；`read_only` 为 False 的 Harness 这一轮不启动，错误码 `read_only_refused`）；`look` 是管家判为只读、没等主人点开始就开工的那一轮（Claude Code `--disallowedTools Edit,Write,NotebookEdit`，命令行还在；Codex 只读沙箱，关 apps、plugins、memories、hooks，执行规则里 allow 的命令仍在沙箱外执行；别的 Harness 只有任务说明里那句话）；`proxy` 是代答话题的一轮（Claude Code `--tools Read,Grep,Glob,Bash,Edit,Write`、不挂 MCP，PreToolUse 钩子 `bin/covey_tool_guard.py` 只放行只读的 lark-cli 命令和写联系人笔记目录，不读不写自带记忆；Codex 沙箱照旧（沙箱里 lark-cli 读不了聊天），app-server 关 memories、线程标成不进记忆，见[代答](proxy.md)）；`lead` 是专项负责人的一轮，不由 `limit` 定，`app/project_lead.py` 的 `held` 在负责人的轮里设（已经是 `consult`、`proxy`、`read` 的照旧，`look` 让位）：读、搜和 Bridge 工具，不改东西（Claude Code `--tools Read,Grep,Glob`，Bridge 工具照常；Codex 只读沙箱，Bridge 的 MCP 服务设 `default_tools_approval_mode = "approve"`，app-server 和 `read` 一样关 apps、plugins、memories、hooks，续接时同样用私有 `CODEX_HOME`；Antigravity 这一次运行的 `--settings` 拒绝规则，写文件、执行命令、读网址都拒；DeepSeek Harness 的 `read-only` 权限预设加 `--approval-policy never`，Bridge 工具经补丁里 `insert` 的 `dsh-mcp-client`；`bridge_tools` 为 False 的 Harness 这一轮不启动，见[专项 · 负责人只读](projects.md#负责人只读)）。都没有时一轮和以前一样。
- 提示词（`app/prompts.py`，`app/runs.py` 组装）：桥接规则同一话题每轮相同，四个核心 Harness 都放在系统级（`request.instructions`，见[续接轮的规则](#续接轮的规则)），消息里只有每轮变的；没有系统级规则的老会话和 `COVEY_SYSTEM_RULES=0` 时照旧放在用户消息里，续接轮附 `RESUME_REMINDERS` 等提醒。主人的原则由 `principles_block` 加入。检查点约定（`Runs.checkpoint_rules`）：新会话整份；老会话的续接轮带步骤和交付两段，`show_images` 只在主人说到图时放消息里（[检查点步骤与改动](cards.md#检查点步骤与改动)）。
- 后端继承 `BackendRuntime`。启动检查（`on_startup`）只由 worker 经 `Harness.ensure_started` 跑，失败后 `STARTUP_RETRY_SECONDS` 内的消息直接拒绝。模型目录由 Bridge 后台刷新到 `launch-models/<名字>.json`，点卡片时不探测后端。

### 后端名单

- `core/roster.py` 的 `HARNESSES` 是唯一的后端表（核心表 `CORE_HARNESSES` 加上启用的[扩展](#扩展)的项），每行：`module`、`store`（会话存储类）、`live_questions`（等回答时回合保持打开：Claude）、`paused_questions`（中断回合、带回答续接：Codex）、`devices`（这一轮能在[设备](#设备多设备默认关)上跑：Claude、Codex、Antigravity）。派生的 `NAMES`（第一个是默认路由）、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 供路由和问答证明使用，`DEVICE_HARNESSES` 是 `devices.json` 的 `harnesses` 能写的。
- 模型从哪里来是供应商，不是 Harness：`core/providers.py` 的 `PROVIDERS` 记供应商属于哪个 Harness、模型 ID 前缀和登录在哪，`provider_of` 按前缀认出供应商。
- `RETIRED`、`RETIRED_PREFIXES` 记已下架的 Harness 和供应商；带这些前缀的模型由 `core/model_settings.py` 在入队前拒绝（[已下线的功能与旧配置](legacy.md)）。

### 扩展

不是每个部署都要的功能放在核心之外的 Python 包里（`core/extensions.py`，只用标准库）。没有启用的扩展时，名单、供应商表和功能列表与没有这个机制时完全一样。

- **启用**：环境变量 `COVEY_EXTENSIONS`（逗号分隔的包名；设成空串表示一个都不要），没设时读仓库根的 `extensions/enabled`（每行一个包名，`#` 起注释，空行忽略；文件不存在就没有扩展），再按数据根的 `extensions/enabled` 加减：`+包`（或只写包名）启用、`-包` 停用，同一个包以后一行为准，加的排在 release 的后面。release 的名单随部署换，数据根这份是这个部署自己的选择，部署不动它。
- **装和卸**：`python3 -B bin/covey_plugins.py list` 看找到了哪些扩展、哪些启用、来自哪一层；`enable <包>`、`disable <包>` 只改数据根那份名单，写之前按 Bridge 的方式加载新名单（manifest、Harness 和供应商合并、卡片 kind），加载不了或冲突就拒绝、什么都不写；`doctor` 检查现在的名单能否一起加载。改完 `covey_service.py restart`（或下一次部署）生效，命令本身不启停任何东西。设了 `COVEY_EXTENSIONS` 时它盖过两份文件，命令会提示。扩展包本身随 release 带着（`scripts/assemble_release.py` 把扩展库里每个带 manifest 的包都放进 `extensions/`），所以启用一个包不用改核心代码，也不用改服务清单。
- **查找**：`COVEY_EXTENSION_PATH`（`os.pathsep` 分隔的目录），没设时是仓库根的 `extensions/`。有启用的包时才把这些目录幂等地放到 `sys.path` 前面。
- **`<包>.manifest`**：只放纯数据（`dict`、`list`、`str`），不导入 `covey`（它会被扩展的隔离解释器里的 `core/roster.py` 间接导入，也要避免循环）。可选的几项：`HARNESSES`（名字 → `module`、`store`、`store_module`、`live_questions`、`paused_questions`、`after`、`devices`，字段同 `RosterEntry`；`devices` 为真时它的进程要经 `core/process.py` 的 `spawn_process` 带上下文起、不在本机路径里留这一轮的东西，设备才能声明它；`after` 是它在 `NAMES` 里排在哪个 Harness 之后，启动卡和换配置卡的 Harness 顺序跟着它，不写就接在最后）、`PROVIDERS`（字典列表，字段同 `Provider`）、`RETIRED_PREFIXES`、`RETIRED_HARNESSES`，以及核心读的几张小表：`WORK_MODES`（模式 ID → [名称, 说明]）和 `DEFAULT_WORK_MODE`（`core/defaults.py`）、`AGENT_DIRS`（Agent 在工作区里放工作副本的隐藏目录，`app/artifact_policy.py` 不上传）、`IGNORED_ENV`（不再读取的变量 → [Harness, 替代选项]，`core/ignored_env.py`）。`core/roster.py`、`core/providers.py` 的纯函数 `merged` 把它们接在核心表后面，`NAMES`、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 从合并后的表派生。
- **`<包>.features`**（可选）：`features(app)` 返回功能对象的列表，形状同 `app/unified.py` 的 `self.features`：可有 `threads()`、`menu_items()`、`menu_folds()`；排在核心功能后面。
- **钩子和设置**（`core/extensions.py` 的 `HOOK_NAMES`、`SETTING_NAMES`）：manifest 的 `HOOKS`（钩子名 → 包内模块的点分名）让核心在需要时才导入那个模块：`proxy_sender`（`app/proxy.py`：代答发送器，有 `Sender`、`ProxyLogin`、`tool_dir`、`send_problem`）、`service_hosting`（`service/install.py`：`MODES` 模式名 → `install(root, args)`，`add_arguments(parser)`，可选 `auto_mode()` 适用时返回模式名，给 `--mode auto` 用）、`deploy_check`（`service/plugins.py` 的 `deploy-check`：`check(harness_config)` 返回 `{'problems': [...], 'notes': [...]}`，在要部署的 release 里跑，`scripts/deploy_bridge.sh` 的预检（有 `--ext-repo` 或个人扩展时）有问题就拦下；每个启用的包都问，问之前先把它所有钩子模块和 `features` 模块试导入一遍（`extensions.import_problems`），导入不了的不再问、直接算问题，结果的 `packages` 点名有问题的包）、`claude_turn`（`backends/claude/bridge.py`：包裹单轮 Claude Code 的上下文管理器 `turn(ctx, env, run_id=...) -> (ctx, env)`（每个启用的包都问，按启用顺序嵌套），在 `run`、`compact_session` 和 `handoff_summary` 外层执行；若写入 `SETTINGS_ENV` 的 `ANTHROPIC_BASE_URL`，脱敏中继自动串在其内层；若在 `ctx.options` 的 `claude_turn_hooks`（`backends/claude/argv.py` 的 `TURN_HOOKS`）放 Claude Code 的 hooks（事件名 → matcher 组的列表，和 Claude Code 的 settings 同一写法），它们并进这一轮唯一的 `--settings`：每个包拿到的 `ctx` 里没有别的包给的，只放自己的，核心按事件合并（同一事件按启用顺序接在后面，谁也不盖谁，`argv.merge_hooks`），形状不对时这一轮带包名失败；代答轮（只有它的工具守卫）和协作咨询轮（`CONSULT`）不加。hooks 的命令在 Claude Code 所在的机器上跑：设备上的轮次在设备上跑，扩展自己判断那台机器上有没有它要的东西。任何扩展都能这样给话题的每一轮加命令审计、改写、守卫，不用改用户或项目的设置（参考 Wyu981011/covey `50fc367e`））、`quota_sources`（`core/quota_sources.py`：`sources(environ)` 返回额度来源，核心照常读、存、画，见 [quota.md](quota.md#插件来源)；每个启用的包都问）、`agy_turn`（`backends/agy/bridge.py`：包裹本机一轮 Antigravity 的上下文管理器 `turn(ctx, env, run_id=...) -> (env, switch)`，`env` 是第一个 agy 进程的环境；某个进程以额度用完、限流或没登录失败（`SWITCHED`）时问 `switch(failure)`，给回 `(env, note)` 就在那个环境里接着同一个对话、`note` 写进执行过程，`None` 照旧失败；一轮最多换 `MAX_SWITCHES` 次，设备上的轮次不问；管家那一轮的私有主目录叠在钩子给的 HOME 上，换环境时重建。核心自带的 [agy 多账号](quota.md#agy-多账号)开着时不问这个钩子，两个都开时升级检查拦下（`service/plugins.py` 的 `agy_switching`），见 [harnesses.md](harnesses.md#antigravity)）。`SETTINGS`（名字 → str）给核心几处写死过的部署细节：`web_tenant_domain`、`contact_notes`、`contact_notes_dir`、`contact_notes_hint`、`invite_url`（别人私聊时那句话里的安装说明链接，`app/strangers.py`），以及共享盘上才用得着的搜索规则 `search_rules`（桥接规则里讲限定搜索范围、按进程组停命令的几行，每行一条，替换核心的一句“搜索限定目录并设超时”，`app/prompts.py` 的 `search_rules`）和 `search_reminder`（续接轮提醒里的那一句，`resume_reminders`）；核心不认识的名字会让 manifest 读不进来，所以扩展要在带这两个名字的核心之后才加。也可以放包自己的 `<包名>.<名字>`（`setting` 照读，扩展自己用）。`extensions.hook(名字)` 没有扩展提供时是 `None`，`extensions.setting(名字, 默认)` 给默认值。没有发送器时不起代答后台线程、没有菜单项和设置卡标签；没有 `service_hosting` 时安装器只有 `--mode systemd|launchd|manager|auto`（见 [服务托管](operations.md#服务托管)）。
- **自己的模型来源走扩展**（例如让 Claude Code 经自己的中转、包装命令跑一组模型，和官方账号并存、按话题选；核心不恢复已下线的 Claude 网关档位，见 [legacy.md](legacy.md)）：三步都在扩展里。① manifest 的 `PROVIDERS` 为 `claude` 声明一个带 `prefix` 的供应商，这组模型的 slug 都以它开头，核心据此给模型标签；这个来源就是 Claude Code 自己登录的那个账号时（比如本机 Claude Code 本来就登录在这个中转上），写 `"own_login": True`，模型选择里两者只列一次、用这个供应商的名字（`core/same_account.py`，同一账号不拆成两个供应商）；② `claude_models` 钩子（`backends/claude/models.py` 的 `with_extension_models`）的 `models()` 返回 `catalog_row` 形状的行，并进 Claude 的模型目录（启动卡、换配置卡的选项）；slug 不以**同一个包**为 `claude` 声明的前缀开头的行丢掉并记一次日志，钩子抛错或超过 5 秒也只记一次日志、不影响原生目录，目录里已有的 slug 不被覆盖；③ `claude_turn` 钩子按 `ctx.model` 换 `ctx.backend_cmd`、环境和真正发给 Claude Code 的模型名，核心只把所选模型照常放进 `ctx.model`，不解释它们。多个包都有 `claude_turn` 时全部生效，按启用顺序嵌套（排前面的在外层，最后一个包改过的 `ctx` 和环境到达 Claude Code），所以每个包只改自己的模型，不认识的 `ctx.model` 原样放行。没有包提供时目录和一轮的行为不变。最小示例（路径是占位）：

  ```python
  # /absolute/extensions/acme_relay/manifest.py
  PROVIDERS = [{"key": "acme", "harness": "claude", "name": "Acme", "prefix": "acme/", "access": "包装命令 /absolute/bin/acme-claude"}]
  HOOKS = {"claude_models": "acme_relay.models", "claude_turn": "acme_relay.turn"}

  # /absolute/extensions/acme_relay/models.py
  from covey.backends.claude.models import catalog_row   # 扩展可以导入 covey
  def models():
      return [catalog_row("acme/sonnet-x", "Acme Sonnet X", "经 Acme 中转", ["low", "high"], "")]

  # /absolute/extensions/acme_relay/turn.py
  import contextlib, dataclasses
  @contextlib.contextmanager
  def turn(ctx, env, *, run_id=""):
      if str(ctx.model or "").startswith("acme/"):
          ctx = dataclasses.replace(ctx, backend_cmd=["/absolute/bin/acme-claude"], model=ctx.model.removeprefix("acme/"))
          env = {**env, "ACME_TOKEN_FILE": "/absolute/acme.token"}
      yield ctx, env
  ```

  个人扩展进 release：放在数据根 `extensions/` 下的 git 检出，每次部署自己找到、拼在核心和 `--ext-repo` 的扩展后面（`covey/service/personal_extensions.py`，`scripts/assemble_release.py --personal`），见 [deploy.md 个人扩展](deploy.md#个人扩展)。同名包一律报错；启用照扩展库的规矩（检出根的 `enabled`，没有就全部），本部署的增减写数据根的 `extensions/enabled`。
- **功能对象和卡片按钮**：包的 `features` 模块的 `features(app)` 返回功能对象，接在核心功能后面（后台线程、菜单项同核心功能）；这些对象还可以有 `handle_event(ctx, event, owner)`（顶层消息，在管家和话题路由之前，认领返回 True）、`claim(ctx, event, raw, owner)`（卡片点击，在核心控制器之前，不是自己的返回 `None`）和 `close()`（Bridge 停止时）。manifest 的 `CARD_KINDS`（按钮 kind → 名称）并进 `app/card_actions.py` 的 `ACTIONS`（`bot_only`）。已下线功能（如 Paseo）留下的兼容项：`app/card_actions.py` 的 `MOVED_CARDS` 让点旧卡片的人收到一句“已下线”，不崩溃、不执行；`core/extensions.py` 的 `RETIRED_ENV` 列出这些功能的环境变量，还设着时启动日志记一行、忽略。
- **失败要响**：启用了的包导入失败、manifest 形状不对、Harness 名或供应商（Harness 加前缀）或已下线前缀与核心或别的扩展重复，都抛带包名的 `ExtensionError`，Bridge 启动即失败，不静默跳过。
- **核心不按名字引用扩展**：核心代码里不出现扩展的包名、Harness 名或供应商；要区别对待靠 manifest 的字段、`HarnessSpec` 上的能力字段（`refuses_questions`：原生问题能否被拒绝；`native_default_model`：任务记录里的空模型是否表示后端自己的默认）或后端对象上的方法（模型目录菜单的 `catalog_hidden`、`catalog_live(models, state_dir)`、`catalog_view(models)`，由 `app/launch_models.py` 的 `catalog_filter` 读）。
- **扩展的测试**：放在 `extensions/<包>/tests/`（`tests/support/` 并进核心的 `support` 包），扩展启用时 `scripts/test_map.py` 与核心测试一起收集；核心测试在 `COVEY_EXTENSIONS=''` 下也要全过。扩展的脚本放 `extensions/<包>/scripts/`。后端仍遵守“后端不导入 `app/`、`feishu/`、`cards/`”的规则。

### 设备端小程序

多设备功能（默认关）的设备端部分：`core/device_shim.py`，hub 把它的字节经 ssh 推到设备上运行，hub 不导入它，它也不导入 `covey` 里的任何东西。

- **约束**：单文件，只用标准库，语法和标准库用法按 Python 3.9（Mac 命令行工具自带的版本；开发机上没有 3.9）。`scripts/check_shim_compat.py` 用 `ast.parse(feature_version=(3, 9))` 加一份禁用清单（`match`、`zip(strict=)`、`int.bit_count`、`dataclass(slots=)`、3.10 以后才有的标准库名字和参数）检查，并要求保留 `from __future__ import annotations`、只导入标准库；`tests/test_device_shim.py` 跑它。改小程序后跑这个检查。
- **位置**：设备上 `~/.covey/shim/<id>/shim.py`，`<id>` 是文件内容 SHA-256 的前 16 位（内容变了才推新目录）；任务在 `~/.covey/tasks/<hub-id>/<任务键>/`：`spec.json`（`spawn` 规格的哈希，幂等的依据）、`state.json`（pid、pgid、boot id、proto、shim 路径、spec_sha；pgid 由监管进程自己 `getpgrp()` 写，双 fork 之后它的 pid 不是组号）、只追加的 `journal`、`inbox/<seq>.json`、`seq`、`ctl/`、`checkpoint.json`、`incoming/`、`err.log`（限 1 MiB）、Agent 结束后的 `exit.json`。
- **协议**：版本字段 `proto`（现为 1）。运行 shim 的命令（`python3 <shim 文件> <cmd>`）的 stdout 第一行永远是 `{"k":"hi","proto":1,"shim":"<id>"}`；一问一答类命令是 stdin 一行 JSON、stdout 一行 `{"ok":…}`（`get`、`put` 的头行写 `len`，后面跟 N 字节和换行；单段不超 64 MiB，大文件用 `offset`/`limit` 分段）；`attach` 是长连接，设备按字节偏移把 journal 里的记录（`out`、`cp`、`ack`、`exit`，带起止偏移 `o`、`e`）推给 hub，另有不落盘的 `hb`，hub 发 `send`、`sig`、`detach`。命令：`hello`、`spawn`、`attach`、`send`、`status`、`stop`、`reap`、`ls`、`stat`、`get`、`put`、`exec`、`gc`。一行最多 16 MiB，未知的键和 `k` 一律忽略。
- **幂等**：`spawn` 按任务键（同规格返回已有，规格哈希不同报 `conflict`）；输入按 `seq`（小于等于已投递的丢掉，同 `seq` 内容不同报 `conflict`）；记录按偏移；`put` 按哈希；`reap` 可重复。`reap` 只认 `state.json` 的 pgid，而且只杀确认是这个任务的组（监管进程或 Agent 的 pid 加启动时刻对得上，或 Linux 上成员的环境里有 `COVEY_TASK_DIR`），对不上就拒绝（`refused: identity`），免得误杀被回收的组号；它只保证杀进程组，自己 `setsid` 逃出组的子进程不管。
- **版本协商**：`hello` 互报 `proto`；字段只加不改，不兼容变更才升 `proto`。每个任务在 `state.json` 记自己的 shim 路径，之后对这个任务的调用都用它，所以新 hub 能接旧 shim 起的任务；`gc` 不删被任务引用的 shim 目录，也不删一小时内新建的。
- **平台**：Linux 完整；macOS 的进程查询走 `ps`、boot id 走 `sysctl`，按同样接口写，还没有在真机上验证。

### 话题中途换配置

- 入口见[换配置](usage.md#换配置)。按钮带 `harness_id`、`binding_revision`，不一致时 `current_binding_card`（`app/binding.py`）按 `job_id` 判断能否打开。
- 私聊顶层的回带卡也能开：“做完了”回带卡的“换配置”（`app/butler_relay.py` 的 `with_switch`，和“独立复核”同一行，没有复核就单独一行 `relay_switch_row`）、“停住了”回带卡的“换个模型”（`app/relay_stops.py`）。它们的值带 `relay`（`cards/topic_actions.py` 的 `RELAY`），换配置卡就发在私聊顶层，是自己的一条消息（不引用、不进话题串），副标题写“话题 T12 · 确认后回复这张卡接着做”；这条消息记成话题的（`remember_topic_message` 加 `route_message`），引用它就是跟那个话题说。记录里多一个 `top_level`：选项重画、确认、取消都在这张卡上原地改，`tell` 的几句（卡片没过期、目录没准备好、没送达、队列满、刷新失败、卡写不上时的退路）也发在顶层（`HarnessSwitch.here`）；“已换配置”卡最后一行写“回复这张卡说一句，就由新会话接着做”（`TOP_NEXT`），因为顶层的一句话是管家的。过期的按钮：“停住了”回带卡原地重画成 `STALE` 那行（`relay_stops.redraw`），“做完了”回带卡不存卡，在顶层回 `STALE_CARD`。不带 `relay` 的按钮（话题串里的任务卡、问题卡、恢复卡）照旧在话题串里开、在话题串里回话。合并回带卡（`merged_card`）是几个话题的，不放。
- 卡片（`cards/switch_form.py`）分级：Harness、供应商、模型只改记录里的 `selected_model` 再整卡重画；其余项记录后重画。独立 worktree 在卡的 worker 后台线程里建（`made_worktree`）。
- 模型行放不下时截短：一个供应商下的模型多到一张卡放不下（模型网关把几十条路由都当 Codex 自带，CardKit 按 200 个节点拒收，报 300305，Codex 就选不上），换配置卡和启动卡从模型行末尾一个个往下减到放得下（`cards/launch_config.py` 的 `fitted`），选中的和话题现在用的照样列；模型行下一行灰字写列了几个、共几个，其余的到网页工作台选。
- `switch_topic` 在话题空闲时于 `LOCK` 内改 `topic.json`：`binding_revision` 加一，清掉新 Harness 的会话指针（每次换都开新会话），`switches` 追加一条（`from` 记旧会话、会话文件、工作目录和是否脱敏 `redaction`；换时还有一步 ↩️ 回退没被下一轮接走的，再记 `rewind_keep`，回退标记 `read_marker` 也在 `app/binding.py`），旧绑定的 `pending` 问题转 `cancelled`。只改脱敏时版本和会话都不变。
- 新 Harness 首轮由 `SwitchHandoff.switch_handoff` 写 `<话题目录>/handoff/transcript-r<版本>.md`，提示词只给路径。“首轮”到新绑定有一轮带着可续接的会话结束为止（`settled`：本话题有 `binding_revision` 等于当前版本、有 `session_id`、`native_resume_allowed` 为真的任务记录，即 `turn_settle` 记下会话指针的那种结束；看到一次就写 `handoff/settled-r<版本>`，之后任务记录被清掉也不变）。之后同一绑定再开新会话（会话指针因失败被清、↩️ 回退了会话的第一轮）都用任务自己的 `history_snapshot`，不再带交接说明、摘要和“本话题刚从…更换”。安全拒绝后的重试（`job.policy_retry`，`Runs._turn_message`）也一样：它的快照已去掉被拒的那轮，交接会把那轮带回来。不用历史里有没有新回复来判断：失败、停止的一轮也写回复行，还没开跑就停下的首轮会因此丢了交接。
- 交接摘要：旧绑定有会话、它的后端有 `handoff_summary`（Claude、Codex：`backends/<名字>/handoff.py`；Antigravity、dsh 的命令行只能续接、不能分叉，没有）时，`SwitchHandoff.summary` 经 `Topics.apps` 找到旧绑定的 app，按 `from` 的模型、预算、工作目录调它（本轮的 `cancel_event` 传进去），结果写 `handoff/summary-r<版本>.md`，同一版本再跑首轮时直接读文件；有摘要时原文只给 `SUMMARY_ROUNDS`（1）轮，没有时 `HANDOFF_ROUNDS`（3）轮，每条由 `capped` 截到 `MESSAGE_CHARS`。`from.redaction` 或话题现在的脱敏为真时经中继（读不出不做；Codex 不能脱敏，这时不写）。没有会话、后端不支持、`from` 没有 `redaction`（之前换的）、有 `rewind_keep`、原目录不在、本轮会被拒（`ready`：新工作目录不在、脱敏拒绝）、失败或超时都退回 3 轮，日志一行 `switch summary …`。旧会话不能再续接时也不分叉它（`refused_session`）：这个会话最近一轮任务的记录有 `policy_retry` 或 `policy_stop`（被安全拒绝停过，或是拒绝后换旧模型的重试，分叉会把被拒的对话再发给拒绝它的模型），或 `session_is_blocked`（`core/session_guard.py`，读不出也算）为真；`switch_topic` 记的是会话指针，策略守卫停下的会话被封但指针还在。这几种在任务卡上各有一行说明（`REFUSED`，按原因：`policy_stop` 的 `kind` 是 `refusal` 写“被安全拒绝停过”，是 `fallback` 写“因官方模型降级停过”，只有 `policy_retry` 写“是安全拒绝后换旧模型的重试”，会话被封写“已禁止续接”），然后退回 3 轮。`ready` 在 `refused_session` 之前看：本轮会被拒时不写这行，免得说原文交给了不会开跑的新模型。不再请原模型写摘要的还有几种（任务进程里读不到主进程的可用性检查，所以都看落盘的东西）：旧会话最新一轮因登录失效、额度用完、模型不可用或被拦、安全拒绝、网关断开、请求挂住、服务故障、限流、进程退出或执行失败停下（`stopped_session`，`STOPPED_BY`）；旧 Harness 在辅助模型的熔断里（`<state>/assist-state.json`，模型不可用类只认同一个模型）、旧的（后端，模型）15 分钟内失败在 `availability.NOT_NOW` 或模型不可用、断连上、网关额度被挡（`unfit`）；旧后端的 `start_refusal` 拒开新一轮（`start_refused`，Antigravity 单账号额度挡住也走这条）；Claude Code 的模型所在供应商或网关已下线（`ClaudeBackend.handoff_summary` 不起进程）。这几种都在“正在请原模型写交接摘要”这句之前判，任务卡写原因，日志 `switch summary skipped …`。Claude Code 和 Codex 的交接摘要握手（`initialize`、首个回应）各最多等 30 秒（`backends/claude/handoff.py`、`backends/codex/handoff.py` 的 `FIRST_SECONDS` 等），网关断了很快退回原文，不再等满 3 分钟。等摘要时任务卡执行过程里有一行机器人说明（`WAITING`），之后改成结果；等摘要时点了停止或收到 SIGTERM，说明改成“已停止，没有写交接摘要”（`STOPPED`），`Runs.generate_reply_for_worker` 在开跑前看到 `cancel_event` 就抛 `JobCancelled`，新 Harness 不启动，这一轮按停止或中断结束；中继统计里摘要单独一行（`run_id` 为 `handoff-r<版本>`）。
- 交接块：`switch_handoff(show=)` 把 `cards/switch_form.py` 的 `handoff_block` 交给 `Runs._handoff_block`，它用 `Cards.set_task_element(before=<正文>)` 放在任务卡答案的上方，同一个 ID（`switch_handoff`）原地替换：等摘要时一次，交接完一次（有摘要时是折叠、点开是摘要，没有时写 `WHY_WORDS` 里的原因或后端给的结果，停了写已停止）；本轮会被拒（`ready` 为假）时不放。管家的窄卡不换配置，没有这一块。
- 消息恰好在换配置时到达：`reserve_submission` 抛 `HarnessChanged`，`BridgeApp.handle_event` 按新绑定再路由一次。
- 批量换配置（`app/batch_switch.py`、`cards/batch_switch.py`，[用法](usage.md#批量换配置)）：管家或负责人的 `topics_switch` 先逐个话题规划（`plan`：同 Harness 且没点名模型时沿用话题自己的模型，不查当天的菜单；否则照 `topic_start` 用 `butler_model_choice.resolve` 找模型和预算），写 `<state>/batch-switch/<请求 token>.json`，再在私聊发卡，同一请求再来只回存下的答复。点击在 `PANELS` 里认领（只认主人、一个事件一次），确认后在 `after_ack` 里逐个调 `switch_topic`：每行先记 `switching` 再换，结果记 `done`、`failed`（可“再试没换的”）或 `stale`（卡发出后 `harness_id` / `binding_revision` 变了，不再动它）；换配置不持这张卡的记录锁，卡用消息卡更新（`patch_card`）整卡重画。配置要有依据（`basis`），见[点名配置的依据](butler.md#点名配置的依据)。

### 回退一步

- 入口见[回退一步](usage.md#回退一步)：capability `rewind` 为真的 Harness（Claude）。点击由 `app/rewind.py` 的 `RewindCards` 在 EARLY 表里接住，不转给任务进程；记录在 `<state>/rewind-forms/`。
- Claude Code 回放本轮提问时，`Rewinds.prompt_read` 在任务记录写 `prompt_uuid`、`prompt_session`；能回退的只有当前会话最新一个有这两项的任务，且话题空闲。
- `backends/claude/rewind.py` 读会话文件找到保留条目和文件快照；先回退文件（失败则什么都不改），再在话题锁内移出最后一轮历史、取消待答问题、写 `rewind-next.json`。下一轮用 `--resume-session-at` 接上，不压缩。

### 续接轮的规则

桥接规则（身份与约束、主目录段和仓库地图、记忆的规矩、检查点契约和它的两条可选规则；管家和负责人连同自己的规则）放在每个核心 Harness 的系统级，续接轮的消息不再整份重发，会话压缩也压不掉（WAVE12 C9，主人定的；`app/rules_sent.py`）。系统级的文本每轮相同（`Runs.bridge_rules(system=True)`）：工作区最近话题的要点和专项资料库索引会随别的话题变，放在新会话第一条消息的开头（`_background`），不进系统级。

- **四家各自的通道**（`HarnessCapabilities.system_instructions`、`system_refresh`；按本地假模型服务抓到的请求实测）：

| Harness | 放在哪 | 续接时 | 压缩之后 |
|---|---|---|---|
| Claude Code（2.1.331） | 每次启动都带 `--append-system-prompt` | 沿用会话第一次调用存下的系统提示，传新的被忽略 | 按当次启动传的重新生成（`system_refresh="compaction"`） |
| Codex（0.172.0） | 新线程的 `developerInstructions` | 存在线程历史里，续接、fork 都改不了 | 保留（本地压缩路径实测）（`system_refresh=""`） |
| dsh（0.2.2-rc.5） | `--patch` 的 `system-prompt.personaSuffix`（连同默认的 `personaPrefix`；规则里的 `{{` 拆开） | 每轮按当前 patch 重新拼 | 每次请求重新拼（`"turn"`） |
| agy（1.5.18） | 每轮的私有主目录（`backends/agy/home.py`）里的 `~/.gemini/GEMINI.md`，超过 23000 字节的部分进 `config/GEMINI.md` | 每轮重读 | 保留（`"turn"`） |

- **版本不一致时补发**（`rules_sent.decide`，只对 Claude Code、Codex 这种不每轮重读的）：会话记录 `<state>/topics/<话题>/rules-sent.json` 里每个“Harness:会话”记着它系统级是哪一版（`system`，sha256 前 12 位）和消息里补发过哪一版（`override`）。这一轮的版本和会话的一样，消息里什么都不加（`system`）；不一样（部署改了规则、记忆的规矩多了一条），在消息里整份补发这一轮的，开头写明“以这份为准”（`override`，`OVERRIDE_HEAD`）；补发过同一版、且会话从那以后没压缩、上下文没骤降 30% 以上、上一轮善终、检查点没写 `rules_missing`、不满 `N_FULL`（8）轮，就不再补。新会话、Claude Code 压缩过的会话记成这一轮的版本。记录只在善终后写（`app/worker.py` 调 `rules_sent.settle`）；分叉的第一轮读源会话的记录；↩️ 回退（`app/rewind.py`）清掉本话题的记录；🔁 换配置后绑定对不上，等于没有记录。
- **缓存**：系统级文本在整段对话的最前面，变一次整段对话的缓存就全部失效一次。所以系统级的文本在一个会话里不变：Claude Code、Codex 本来就只认会话开头那份；dsh、agy 每轮重读，Bridge 把会话拿到的那份存在 `<state>/topics/<话题>/rules-text/<版本>.md`，之后每轮照原样写回（`rules_sent.pinned_text`、`system_text`），规则改了也走上面的消息补发，只在新会话和压缩之后（缓存反正没了，`CACHE_GONE`）换成新版。话题要点、到点回来看、话题转交管家这三段每轮都一样的规则也放在系统级（`Runs._system_notes`），消息里只留话题自己的要点。
- **老会话**：没在系统级带过规则的会话（`app/sessions.py` 的 `started_with_system_rules` 没有标记）照旧每个续接轮在消息里整份发；每轮重读的 dsh、agy 不分新老。代答话题、协作咨询轮也照旧把规则放在消息里。
- **管家和负责人**：规则在系统级时，管家的消息只有快照和这一轮的话，负责人的只有状态包和这一轮的话。会话完整（`rules_sent.lost` 为假）时，负责人状态包里三块只写变了的（`app/project_lead.py` 的 `state_pack(sent=…)`：话题清单只列没结束的、摘要变了的和本轮回执点到的，其余计数；专项记忆版本没变只写版本号；决定只列新增），读过什么记在 `<state>/projects/<专项>/pack-sent.json`（`pack_settle`，善终后写）；不完整时状态包整份，管家快照也整份。
- **Codex 每轮的说明**：线程只认创建时的 `developerInstructions`，所以那里只放线程不变的（`backends/codex/appserver.py` 的 `thread_instructions`：负责人的、或通用的）；协作咨询、群成员只读、只读首轮这类按轮次变的说法放在这一轮输入的末尾（`turn_instructions`）。
- **可观测**：任务记录多 `rules_sent`（`system`/`override`/`full`）和 `rules_sent_reason`，压缩过的一轮多 `compactions`；负责人每轮多 `session_rebuild_reason`（`""`/`tokens`/`idle`/`model`/`binding`/`prev_failed`，原来只在任务进程的日志里），缺 `context_tokens` 时不再拿累计 `input_tokens` 比 30 万，改按同一会话连续善终 `REBUILD_TURNS_FALLBACK`（12）轮重建。
- **退回**：`COVEY_SYSTEM_RULES=0`（每轮现读，不用重新部署）：规则回到用户消息，和原来一样。
- **代发规则**：`app/owner_send.py` 的 `turn_note` 不再接在主人原话后面（那样最近对话里最后一行和“用户最新的消息”对不上、去重失效，主人的话重复出现），改放消息末尾，只在有没结的草稿或这一轮的话提到“发给、通知”这类时带规则全文，刚结束的草稿结果照常一行一条。

### 敏感词脱敏

- 开关是 `topic.json` 的 `redaction`，词表在 `<state>/redaction/config.json`（`core/redaction.py`），每轮开始时读。只有 capability `redaction` 为真的 Harness（Claude）能跑；`app/runs.py` 在 `backend.run` 前兜底。
- 每轮（`backends/claude/redaction.py`）在 127.0.0.1 起一个只活这一轮的中继，`--settings` 把 `ANTHROPIC_BASE_URL` 指向它，所有请求都走中继。不能脱敏就不发：出错抛 `Unavailable`，本轮 `redaction_failed`，Claude Code 不启动。
- 脱敏的一轮不读也不写 Claude Code 自带的记忆（`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`，`backends/claude/argv.py` 的 `turn_env`）：仓库的 `MEMORY.md` 同一个工作区的话题都读，这一轮见过的原词不能经它到没开脱敏的话题。
- 匹配和还原在 `core/redactor.py`（规则见其模块说明）。每轮结束发 `redaction` 事件（卡上只显示个数），`turns.jsonl` 和 `relay.log` 不含内容和请求头。
- 替身词一律是自动的（`auto_stand_in`），词表每项只有 `term`（旧版本的 `"anywhere": true` 保留生效、不能新设）。改词表的接口在 `core/redaction.py`：`edit_terms(state_dir, add=, remove=, whole=)` 一次加、删一批（先删后加，删除按整词、按 `ignore_case` 比较；返回 `TermEdit`：加了哪些、删掉的行和原位置、已在词表里的、没找到的、加不进去的行号和不提原词的原因），`put_back` 把删掉的行放回原位，开关和上游地址用 `update_config`；三者都在 `EDIT_LOCK` 里读、改、存，没有变化就不写文件。
- 词表在设置卡的「脱敏」标签上改（一层，没有页签；`app/redaction_panel.py`，`PANELS` 里认领它的按钮）：点击在记录锁外改词表，在设置卡记录的锁里记下结果就回执，整卡重画在回执之后（`card_lanes.after_ack`；记录的 revision 已被后一次点击推进时这次不画）；「探测」在自己的后台线程里跑，跑完重画卡片。卡片、卡片记录和日志里都不出现原词：行名是 `masked`（首字和字数），自动替身不显示，按钮按 `<state>/redaction/card-key`（0600，第一次用时生成）下的 HMAC 指认行，查找只记下找到的行、框里输入的内容不写回卡片，拒绝原因是不提原词的 `Refused`，词表文件读不了时只说读不了（原来的错误可能引用原词）。“撤销”（`put_back`）的数据只在 Bridge 进程的内存里。记录区要读最近 500 轮涉及的话题，`core/redaction_views.py` 的 `topic_info` 按 topic.json 的修改时间缓存。旧词表的 `stand_in` 读时忽略、写时不写，迁移 `redaction-stand-in` 把旧词表重写一遍。
- 网页工作台的设置页也改词表（`app/web_ops.py` 经 `edit_terms`）。以前的独立网页看板已经去掉；它的“换口令”按钮（`bridge_dashboard_rotate`）留在 `ACTIONS` 和 `cards/redaction_panel.py` 的 `KINDS` 里，点了只说页面已经去掉（`RedactionPanel.page_gone`，设置卡上整卡重画并提示）；manifest 里旧的 `dashboard_entry` 钩子名照常接受（`RETIRED_HOOKS`）。

### 已下架的 Harness

绑定的 `harness_id` 不在 `HARNESSES` 里时 `route()` 抛 `RetiredHarness`：消息和旧卡点击只得到一句提示（`retired_notice`），不建任务；`adopt_runners` 不认领它的任务进程，`close_retired_jobs` 标 `interrupted`。下架时从 `HARNESSES` 和安装脚本删掉、在 `RETIRED` 加一行（`test_harness` 的 `RetiredHarnessTests`）。

### 加一个后端

1. `backends/<名字>/`：`<ID>_SPEC = HarnessSpec(...)`、后端类、会话存储类，末尾 `HARNESS_REGISTRY.register(...)`；名字不能和现有的或 `RETIRED` 里的重复。
2. `core/roster.py` 加一行，选 `live_questions` 或 `paused_questions`。
3. 只发公开事件；`capabilities(ctx)` 如实声明。
4. 结果经 `verified_reply` 存 `VerifiedRootReply`，返回带 `public_reply` 和 `outcome` 的 `RunResult`；一次运行只声明一个根会话。
5. 参数用 `add_cli_args` 注册（[配置 Harness 参数](harnesses.md#配置-harness-参数)）；CLI 名加进 `scripts/setup_harness_bridge.sh` 的探测列表。
6. `tests/support/backend_drivers.py` 加驱动（`test_backend_contract`），再加完成边界和事件转换测试；用法写进 `docs/harnesses.md`。

## 事件长连接走 SDK（默认关）

`COVEY_EVENT_TRANSPORT=sdk`（不设或 `cli` 是现状）。实现在 `feishu/ws_intake.py`；关着时 `app/unified.py` 的 `consume_events()` 走原来的 `feishu/events.py` `consume()`（lark-cli 消费者），`lark_oapi` 一个都不导入。

- **四类事件一起走**：`im.message.receive_v1`、`card.action.trigger`、`application.bot.menu_v6`、`im.message.message_read_v1`。同一个应用只能有一个事件长连接，SDK 连着时 lark-cli 的 `event consume` 拒绝启动，所以开着时 lark-cli 消费者一个都不起，处理函数还是各自原来的那一个（`handle_event`、`CardLanes.submit`、`handle_menu_event`、`TopicSeen.handle`），`consume_events()` 的调用方不变。
- **转换**：SDK 给的是 v2 原始信封，`ws_intake.py` 的 `message_event`、`card_action_event`、`menu_event`、`read_event` 转成与 `events.py` 里各 `*_JQ` 同形的 dict，下游分不出来源。消息事件带 `REPLY_CONTEXT`（原始事件本来就有类型、根、父、提及）；文本消息的 `content` 是读得懂的文字（`@_user_N` 换成名字）；富文本（`post`）照 lark-cli 的样子渲染（标题一行，段内元素连写，`**粗**`、`*斜*`、`<u>`、`~~`、Markdown 链接、`@名字`、图片写成 Markdown 图片（文字 Image、地址是 image_key）、代码块、`---`、`:表情:`）；其他类型保留消息 API 的 JSON，`message_parts.py` 两种都认。在测试机器人上对照过：多行文字、@ 提及、图片、文件、引用回复、话题里的回复、两种富文本，SDK 和 lark-cli 两条路交给 `enrich_incoming_event` 后的模型文字逐字相同；卡片表单提交的 `form_value` 同形。
- **toast**：卡片回调的应答在同一帧里回 `{"toast": {"type": "info", "content": "收到"}}`（常量 `TOAST_TEXT`；点击的消费者还没接上时是 `HELD_TOAST_TEXT`，见下文启动时先连）。应答前只做解析和入队，点击在卡片的消费线程（`cards`）里走原来的 `CardLanes.submit`；SDK 的回调线程同时跑心跳，不做重活。转换失败时不回 toast。
- **队列与线程**：每个 key 一个有界队列（256），由该 key 自己的消费线程取（和 lark-cli 时一样逐个处理，慢消息不拖连接）。`Intake.start`（或第一个 `consume()`）起 `event-websocket` 线程，在线程里才导入 `lark_oapi`、取凭据（`feishu/sdk.py` 的 `client()`，只在内存）；断线重连由 SDK 自己做，客户端抛出（如连接数超限）时线程 20 秒内重来。Bridge 停止时 `stop_children` 关连接，只关一次（信号处理和 `main` 结束各调一次，第二次在已停的事件循环上会白等 2 秒，推迟进程退出和 Manager 起下一个）；停时还有收下没处理的点击或消息，日志记 `event websocket: stopping with events not handled <key>=<个数>`，它们留在盘上，下次启动先补处理（下一条）。
- **启动时先连**：`main` 在核对应用身份前先起连接线程（`start_intake(connect=False)`，即 `Intake.prepare`），让它趁 `init_target` 的飞书调用导入 `lark_oapi` 的连接模块（导入时用 `_skip_pkg_resources` 绕开 `pkg_resources` 扫描全部已装包），核对完再调 `start_intake` 放行连上，并最多等 `INTAKE_WAIT_SECONDS`（3 秒）连上再开始恢复（恢复占着 CPU 时连接线程排不上），比消费者早；消费者等[重启认领](#重启认领)做完、和后台线程一起起来。这段时间收到的事件在各自队列里等，按到达顺序处理；卡片点击当场回 `HELD_TOAST`（“收到，机器人刚重启，稍后处理”），不再因为没有连接被飞书拒掉、填的内容白丢。连上时还没有消费者，日志记 `event websocket connected; events wait for the start to finish`；`[event] ready` 仍在消费者接上时才写，所以部署脚本和 doctor 看到的就绪还是“开始处理”。lark-cli 收事件时不这样做，消费者照旧随后台线程起。收下后进程没做完启动就退出（紧接着又一次部署、崩溃），队列里的事件留在 `event-spool/`，再下一次启动补处理（见下面“收下就落盘”）。
- **收下就落盘，处理完才删**（`feishu/event_spool.py`）：飞书那边事件交到连接上就算送达（点击已经回了“收到”），处理却在之后、在各自的消费线程里；只放在内存里时，停止、重启或崩溃会把排着的丢掉（消息线程在一把被 NAS 挂住的读攥着的锁上等了十几分钟，期间的一次重启就丢了排在后面的消息）。现在消息、卡片点击、菜单点击收下时各记一个文件 `<state>/event-spool/<收到的毫秒>-<序号>-<类别>-<event_id 摘要>.json`（0700/0600，不写点击的回调 token），处理函数返回后删掉；点击交给卡片分道或控制器队列（`CardActionQueue`）时，由跑完它的那个线程删（`event_spool.hold`、`settling`），停止时被丢下没开始的点击（`CardLanes` 的 `items.clear()`、队列里剩下的）因此留着。已读回执不记。
  - **写盘不在 SDK 的线程上**：`on_payload` 只把“记下”“删掉”排进内存队列，由 `event-spool` 线程写；同一批里先记后删的不落盘。NAS 挂住时卡的是这个线程，回 toast 和心跳照常。
  - **停止**：`ws_intake.stop()` 每调一次（信号处理里一次、`main` 收尾一次），断开连接之后等排着的写和删做完，最多 `FLUSH_SECONDS`（2 秒），没有排着的就一点不等；做完记 `event spool: N events taken in and not handled are kept for the next start`，被盘拖住时记还剩几笔没写。消息线程卡在慢操作里、信号处理迟迟轮不到，或 15 秒后被 Manager `SIGKILL` 时，收下时就排上的写早已由 `event-spool` 线程做完，同样留到下次。
  - **下次启动**：`Intake.prepare` 起连接线程时也起 `event-spool` 线程，它先按文件名顺序读上次留下的（这时本进程还没写过），过了时限的删掉（消息 24 小时、卡片点击 10 分钟、菜单 2 分钟，`REPLAY_SECONDS`），读不了的和写到一半的（`.` 开头的临时文件）也删，其余按类别排好，并把它们的 `event_id` 记进去重表。各类事件的消费者在重启认领之后接上时，最多等它读完 `LOAD_SECONDS`（5 秒），先按到达顺序处理上次留下的，再处理这次收的；处理完照样删。日志：`event spool: what the last run took in and did not handle goes first: <key>=<个数>`、`event spool: dropped …`。
  - **不重复**：消息靠 `inbound-messages/` 的 `claim_once`，上次已认领（处理到一半被杀）的这次记 `duplicate message id=… skipped`，不再建任务；菜单点击靠 `app/bot_menu.py` 按 `event_id` 的 `claim_once`；卡片点击再跑一次等于主人又点了一下，处理函数按卡和记录的状态判断（重复的提交、启动、重试本来就要挡）。上次已收下、这次飞书又推一遍的同一个 `event_id` 只处理一次。
  - **已知限制**：NAS 挂住时写不下去的，进程结束就没了（停止时日志写明还剩几笔）；处理到一半被杀的消息已经认领，不会重跑，只在日志里留 `duplicate … skipped`。lark-cli 收事件（默认）时什么都不记，行为和原来一样。
- **去重**：按 `event_id` 在内存里记最近 2048 个，重投或重连重发的不再处理（点击仍回 toast）；不改 `claim_once`。
- **健康**：每个已有消费者的 key 照旧一个 `ConsumerHealth`，连上时写 `[event] ready event_key=<key>`（`deploy_bridge.sh` 数的就是这行），断开记 `retrying/disconnected`，重连再写一遍。`feishu/connections.py` 的传输看门狗在这个模式下不看 lark-cli 总线，改报 SDK 连接状态（`receiving`/`disconnected`）；健康快照多一个 `event_transport: sdk`，`doctor` 据此看 Bridge 自己的消费者是否 ready，不因找不到总线报 degraded。
- **切回**：去掉变量重启即可，不留状态。已知限制：靠 `lark_oapi` 1.7.4 客户端的 `_conn` 判断是否连上；@ 提及的 `id` 对机器人是它的 open_id（lark-cli 给的是 app_id），判断是否提到本机器人用的 `Access.own_ids` 两个都认。

## 事件、执行过程与卡片

公开事件契约、执行过程与刷新、卡片控制器、动作表、客户端限制和添加一个卡片按钮，见[事件、执行过程与卡片](cards.md)。

## 补充投递

用户看到的见[补充当前任务](usage.md#补充当前任务)。`app/steering.py` 的 `Steering.steer_stage(job)` 是对任务标志位的唯一解读：

| 阶段 | 含义 | 补充怎么走 |
| --- | --- | --- |
| `queued` | 已登记、没被 worker 取走 | 并入请求，同步改 `queued/` 副本 |
| `starting` | 已开始、还没有控制句柄 | 存为 `held`，能接收时送入一次 |
| `running` | 有句柄（任务进程里的任务在 Bridge 看来是 `RemoteControl`） | 立即 `control.steer` |
| `paused`、`ended` | 等回答；已取消或答案已定稿 | 拒绝 |

每条补充一个 `interaction-deliveries/` 记录，状态按 `core/deliveries.py` 的 `NEXT`：

```text
submitting → held, sent, read, refused, uncertain
held       → sent, read, refused, uncertain, withdrawn
uncertain  → sent, read, refused, uncertain
sent       → read
read、refused、withdrawn：终态
```

- `update_delivery` 在 `question` 锁内丢弃表外转换，所以并发的发送方之间只前进，回执卡也只前进。
- `deliver_steer` 写 `submitting` 再发：`SteerNotReady` → `held`，`SteerRefused` → `refused`（什么都没发出），其他异常 → `uncertain`（可能已送达，从不自动重发），成功 → `sent`。
- `held` 由持有任务控制权的进程保存，到 `running` 才发；经任务进程时 Bridge 限时等 ack，超时 `uncertain`。
- 排队卡上的补充发给排队时绑定的目标任务。声明 `steer_reads` 的后端（Claude）一轮结束时仍是 `sent` 的补充记 `target_ended`，回执写模型没有读到。
- 话题里任务在跑时发来的消息（`COVEY_TOPIC_STEER`，默认开，`core/deliveries.py` 的 `topic_steer`）：`BridgeApp.handle_event` 照常写历史、`enqueue_chat_job(steer=True)`；`reserve_submission` 在 `LOCK` 内绑定目标（`steer_target_job_id`，前面最近的那个），`steers_on_arrival` 判断要不要马上送：开关开着、后端声明 `steering`、不是管家一家（`Traits.lists_supplements`，管家走自己的 `Butler.supplement`）、目标已开始（在等回答、正在收尾或已经结束的也试，好在卡上说原因）。要送的排队卡状态行先写“正在补充到 #N”，出了 `topic_lock` 后 `Steering.steer_on_arrival` 走和点击一样的 `steer_queued_job`（`auto=True`）：同一个 `queued-steer-<消息 id>` 送达记录，回执 `receipt` 带 `auto`（`on_arrival`），`requeue` 带发送人。没发出（`SteerRefused`、目标在等回答或已结束）这条照旧排队，卡上一行原因、状态行改回 `queued_line`；可能已送达的照规则 6 不单独执行。消息处理是一条线程按收到的顺序做的，连着发的几条各自送、按顺序。送进去了（送达记录 `sent` 或 `read`）后 `acknowledge_arrival` 在主人那条消息上加 DONE 表情（`Messenger.react`，和管家的结束表情同一个接口），回执卡照旧；等任务准备好才送的（`held`）、没发出的、可能已送达的都不加，表情发不出去只记日志。它在所有记录锁之外调用。
- 自动送进去的补充确定没被读到（`held` 后 `refused`，或 `target_ended`）时和管家的一样标记，只是标 `requeue_next`（`core/deliveries.py` 的 `marked`；老版本把每个 `requeue_auto` 都当管家的补充，回退到老版本时它们就只留回执上的“重新排队”）。`settle_steer`、`end_unread_steers` 里先标再画回执，由 `queue_lost` 经 `_queue_marked` 用 `_requeue` 排成新一轮：消息 id 是 `requeue-<token>`（原消息 id 已是那张排队卡的任务），上下文是话题的（`requeue_context`：topic.json 的 `root_id`，话题换了 Harness 就不排、回执留“重新排队”），只排一次（`requeue_state`）。任务进程只标记，结束后 `app/detached.py` 的 `queue_lost` 排（开关开着时话题任务也排）；Bridge 不在时由下次启动的 `queue_stranded` 排：只读一遍记录，管家的交给管家所在的 Harness，话题的交给话题所在的 Harness。主人停止的一轮不自动排；可能已送达的不排。
- 排队卡的补充按钮写“现在补给 #N”（`cards/streaming.py` 的 `steer_label`，管家的分档卡照旧“补充”）；卡发出时 `StreamingReply.steer_label` 记下这个字样，任务开始后第一次写状态行时（`Cards._start_top` 的同一批，或单独写一次 `relabel_steer`）改回“补充”。
- 回执卡的按钮由 `offers` 决定；“重新排队执行”每条只排一次（`requeue_state`、`requeued_job_id`）。
- 话题里同时有在跑和排队的任务时，管家转话（`topic_send`、`project_send`）和网页上说的话都交给在跑的那个（`app/runtime.py` 的 `supplement_target_of`：已开始的里最新的，没有才是排队的里最新的），回执写实际收到它的任务号；只有排队的任务时并进它的请求，回执写“并进排队中的任务 #N”（不能只交给排队的：在跑的那个会一条也收不到）。
- 没读到的补充（`app/steer_interrupt.py`）：持有任务后端的进程（任务进程，或 Bridge 自己跑的任务）在补充 `sent` 后起一个 `steer-unread` 线程，送达后 `URGENT_SECONDS`（20 秒，`COVEY_STEER_URGENT_SECONDS`）和 `REPAINT_AT` 各看一次：还没读到就重画回执（“已等 N 秒”）；第一次看时补充是急的（`core/steer.py` 的 `is_urgent`），就调后端的 `stop_command`。点“打断它，马上读”（`bridge_steer_interrupt`，按 `job_id` 转给任务进程）走同一个 `interrupt`。每条补充只试一次（`interrupt_at`；没东西可停时清掉，可以再点），记录里留 `interrupt`（结果的话）和 `interrupted`（停掉的命令）。
- `stop_command`（能力 `command_stop`，本机的 Claude 和 Antigravity）：从事件里取主任务正在等的前台 Bash 调用（不含后台命令和子任务的命令），在 Claude CLI 的子进程里按命令行找到它的 shell（`backends/claude/command_stop.py`），先送一条说明（为什么命令被停）再停它的进程组（SIGTERM，2 秒后 SIGKILL，pidfd 防 PID 复用）。`core/steer.py` 的 `keeps_running` 列的命令（推送、部署、发布、安装、远程拷贝、改服务、删除和移动）从不停。Antigravity 的 `AgyControl.stop_command` 不找 shell：agy 的补充先暂存，在跑的步骤（适配器报 `step_started`/`step_done`，带 shell 工具的命令）结束后才 SIGINT 重开（没有步骤在跑时立即）；停命令就是不等步骤结束，说明排在暂存的补充前面进下一个进程的消息（`docs/harnesses.md` 的 agy 补充）。
- 管家的补充没有回执卡：确定没读到的（`refused`，或 `target_ended`）标 `requeue_auto`，由 Bridge 用同一个 `_requeue` 以主人消息的 id 排成新一轮，同样只排一次（[butler.md](butler.md) 收尾时发来的消息）。
- 第四条出路（WAVE12 W2-B）：转进话题在跑那一轮的话——管家的 `topic_send`、`project_send`，负责人的 `project_topic_send`，撤回编辑送的补充——也没有回执卡，送达记录的 `requeue` 带 `topic: true`（`core/deliveries.py` 的 `handed`），以及下一轮的说明、标题和来源（`source`：`relay`/`lead`，W1-B 盖的章）。确定没读到（`target_ended`，或晚到的 `refused`）时标 `requeue_next`，和自动送进去的话题消息一样排进那个话题（`in_topic`；负责人话题没有根消息，用 `address` 里的 `project-lead-<id>` 别名），新一轮带原来的来源，不当主人的话；排之前话题在等主人答题就交给 `hold`（`app/app.py` 的 `workspace_command`，`note_only`）暂存，记 `requeue_state` 为 `held`，不开新一轮、不冒答。主人停掉的一轮不排，在话题串里说一行（`tell_topic_lost`），可能已送达的（`uncertain`）也只说一行；没有 `requeue` 的旧记录同样只说一行。Bridge 自己发的提醒（记录带 `internal: true`）既不排也不说。任务进程只标记，Bridge 在它结束后或下次启动时排，不受 `COVEY_TOPIC_STEER` 影响（关着时只不排自动送进去的话题消息）。

## 引用卡片接着说的表情

用户看到的见[使用参考 · 引用卡片接着说](reference.md#引用卡片接着说)。在顶层引用一张直达话题的卡（`core/topics.py` 的 `route_message` 记的 `routes_to`，或话题记过的卡）说话时，`context_for_topic` 把消息交给那个话题，回复进话题的串（`reply_to_message_id` 是话题的根），这条消息下面没有卡。`app/butler_reactions.py` 的 `top_message(ctx, event)` 认出它：`om_` 开头；`reply_to_message_id` 非空且不是它自己；不在话题串里（没有 `thread_id`，或有 `thread_id` 而 `root_id` 不是话题的根：在别的串里引用）；不是管家一家（`Traits.top_level`，它们归 `placement.marked`），也不是代答话题。只在 `BridgeApp.handle_event` 里、`supplement` 为空（话题自己的消息）时算，代答、管家转话、网页的消息不经过它。飞书发来的话题串里的消息都带 `thread_id`，没有 `root_id` 的串里消息也当作在话题串里。

- **记在哪**：`BridgeReplyJob.top_message`（声明的状态，handoff 的 `extras` 里非空才写），任务记录里非空才写 `top_message`，`job_from_record` 和 `requeue` 读回。老版本的任务进程把它当未知 extras 忽略，老记录没有它，都当没有，什么都不加。回退到老版本时，新版本排了队还没开跑的任务由老版本接着跑，表情停在 OneSecond 或 OnIt；新版本的任务进程被老 Bridge 接管时照常收尾。
- **入队**（`Submission.publish_submission` → `top_queued`）：马上能开工挂 OnIt；前面有本话题的任务或名额满了挂 OneSecond，在任务的 `execution_lock` 里看过 `execution_started` 才挂（交给任务进程、worker 开跑拿的是同一把锁：开跑前挂上的由 `begin` 摘掉，已经开跑的改挂 OnIt）。要自动补充给在跑任务的（`plan["steer"]`）入队时不挂。
- **开跑和结束**：`worker` 调的 `begin` 见到 `top_message` 就查一次机器人挂的 OneSecond，查到就摘掉、挂 OnIt（和多路并行的开关无关）；`butler_reply.react_end` 对它和管家一样调 `finish`，`routed` 恒为假，所以只会是 DONE、CrossMark 或不留。
- **补充**（`Steering.steer_queued_job`，自动的和点“现在补给”都走这里）：送进去后 `top_steered`：`sent`、`read` 挂 DONE（点击的先摘 OneSecond），`held` 挂 OneSecond，`uncertain` 不挂。自动的没送进去（`steer_on_arrival` 返回假）由 `enqueue_chat_job` 补一次 `top_queued(waits=True)`；`acknowledge_arrival` 对这类消息不再挂 DONE。送达记录的 `requeue` 记下 `top`：`settle_steer`（`held` 之后的去向）先 `top_settled`（摘 OneSecond，送进去了挂 DONE）再 `requeue_lost`；`watch_steer` 等到晚来的确认也挂 DONE；`_requeue` 排成的新一轮带上 `top`，并摘掉先前的 DONE。
- **回答**（引用问题卡的副本打字作答）：`interactive_command(top=)` → `submit_answer(top=)`。交给在跑任务的（包括转给任务进程的）`top_took` 挂 DONE；排成续接的，续接任务带 `top`；几道题时 `hold_note(top=)` 在暂存记录里记 `"top": true` 并挂 OneSecond，`settle_notes` 带走时换成 DONE，撤回时摘掉，留作下一轮的 `note-` 任务带 `top`；被拒的（结果或问题卡没送达、`ValueError`）`top_refused` 挂 CrossMark，原因照旧回在话题串里。
- **停止和恢复**：排队时停止或撤回，`unqueue` 摘 OneSecond 和 OnIt；`show_recovery_card` 和 `report_unfinished`（带原生问题直接返回的那条路也算）调 `TaskControls.top_stopped`：摘 OneSecond 和 OnIt，记录是 `failed`（开跑前就失败）时再挂 CrossMark；`requeue`（检查并继续、自动重试、换旧模型重试）先摘 DONE 和 CrossMark，再挂 OnIt。
- 摘表情都先用 `Messenger.reaction_ids` 列出再删（`clear`、`take_off`），不为拿 id 先加一个；只有结束时的 `finish` 沿用管家的做法。失败只记日志；都不持记录锁（规则 1、2），挂 OneSecond 那一次持的是任务自己的 `execution_lock`。

## 问题与回答

### 什么时候问

每一轮的执行约定（`core/checkpoint.py` 的 `CHECKPOINT_RULES`）里有两条管模型问不问：

- `ASK_RULE`：部署上线和发布、删除和其他撤不回的操作、花钱、对外发送、占机器（`ASK_KINDS`，简称 `ASK_SHORT`）照常问，主人这一轮明说要自己定的也问；其余能撤回、有明确推荐的不问，按推荐做，结论里写一行“按 X 做了；要改成 Y，回一句就行”（有开关、配置项、提交号就写那个）。要问的留到最后合成一次问，不在半路停下来等。问题卡、Claude 的 `AskUserQuestion`、Codex 的 `request_user_input` 都照这条。
- `PLAN_RULE`：主人要方案、设计、提案时，先交完整方案（推荐做法写全，缺的信息按假设写并标出），待定项列在方案后面，不先发问题卡问细节；待定项里有照常要问的那几类才进 `questions`。

管家和负责人派活时不用自己写“别发问题卡”：开话题的工具说明（`core/butler_tool_specs.py` 的 `ASK_NOTE`，在 `topic_start`、`topic_fork`、`project_topic_start` 的 `prompt` 上）和负责人规则都告诉它话题自己知道什么时候问；负责人交给话题的 Bridge 说明（`app/butler_handoff.py` 的 `lead_note`）只点 `ASK_SHORT` 照常问。Bridge 不替模型答题，卡片也没有默认答案：这只是模型问不问的规则。问得多不多用 `scripts/question_stats.py` 比，见[运维 · 问题卡问得多不多](operations.md#问题卡问得多不多)。

### 记录与状态

用户看到的见[回答模型的问题](usage.md#回答模型的问题)。`questions/<token>.json`，token 是 `sha256(任务消息ID-request_id)` 的前 16 位。`core/statuses.py` 的 `QUESTION_TRANSITIONS`：

```text
pending    → submitting, cancelled, superseded, set_aside
submitting → answered, uncertain, cancelled
answered   → uncertain
uncertain  → pending, answered, cancelled
set_aside  → pending, answered, cancelled
cancelled、superseded：终态
```

改动都走 `core/store.py` 的 `update_question`（`question` 锁内核对 `expected`，表外转换抛 `ValueError`）。组件：门面 `app/questions.py` 的 `Questions`，原生问题 `app/native_questions.py`，检查点问题 `app/checkpoint_questions.py`，卡片 `app/question_display.py`，表单到回答 `app/answers.py`，暂存消息 `app/held_notes.py`。

| 种类 | 后端 | 怎么交回答 |
| --- | --- | --- |
| 实时问题 | `LIVE_QUESTIONS` | 答案作为工具结果交回仍开着的回合：`control_lock` 内核对证明，写 `awaiting_ack` 再 `control.answer`；原执行进程不在了就不能提交 |
| 暂停问题 | `PAUSED_QUESTIONS` | `wait_for_answer` 中断回合，会话保护保持阻断；续答任务开始前核对正文已送达、回答没被消费过，写 `native_answer_once` 再续接 |
| 检查点问题 | 任何后端 | 本轮正常完成、检查点 `requires_user` 为真且有可问的（`question` 或 `questions`，`core/checkpoint.py` 的 `needs_owner`）、没有未答完的原生问题时登记；正文送达后才展示控件，任务转 `waiting_user` |

- 检查点问题登记前先归一化（`core/checkpoint.py` 的 `normalize_questions`，`runs._leave_checkpoint_question`、`CheckpointQuestions.defer`、重投三处共用）：数字或缺的 `id` 补成字符串、重复的加后缀，字符串题面、空题面用 `header`，字符串选项、`text`/`title` 当 `label`，`multiSelect`/`multi_select` 当 `multiple`，要密钥的题（`isSecret`）丢掉，其余字段原样保留；写对的原样不动。没有 `questions` 时用 `question` 出一道自由回答题；一题都不剩才算登记失败。`requires_user` 为真却什么都没问的不等主人（日志一行）。登记失败记 `checkpoint_question_tries`，重投时第二次还失败就不再等（`checkpoint_question_stopped`、检查点 `release_user_wait`），把问题用文字发到话题，直接回复即可，话题的下一句照常排成新的一轮（`Redelivery.stop_question_wait`）。
- 正文没送达时不展示控件、不接受回答。只有一个待答问题且只有一题时话题消息就是回答，否则存为暂存消息，回答真正发送时才取走（`claim_held`），只发一次或不发。
- 提交在锁内转 `submitting` 并存答案，锁外发送，异常转 `uncertain`；“核查已保存回答”用保存的答案重交。原任务已结束时 `enqueue_continuation` 建续答任务（`after_ack` 后续）。
- 收起旧问题（`fold_older_questions`，只在 Bridge 做）：较新的问题卡显示后，更早的仍是 `pending` 的问题转 `superseded`，卡整卡重画成收起样式；来源任务还在的不收。等它的暂存消息改等新问题（`HeldNotes.wait_instead`）。
- 停止任务时本任务的待答问题转 `cancelled`。启动时 `recover_question_delivery` 把 `submitting`、`awaiting_ack` 记 `uncertain` 等用户核查，不自动重发。
- 提交回答是表单唯一的发送按钮（只发补充说明、先问一句已并进来）：`interaction_action` 先过 `app/answers.py` 的 `with_note`，写了补充说明时没答的题记为“不回答此题”，只写补充说明就是每题都不答（`answer_message` 用 `UNANSWERED_NOTE` 做标题，告诉模型这可能是反馈、疑问或新要求）；没写补充说明时 `form_answers` 照旧要每题都处理。交不了（`AnswerIncomplete`）时 `refuse_on_card` 把点的那张卡（话题里的卡或顶层副本）原地重画，原因写在表单下，这次点击带的字段放进不存盘的 `draft`，重画时填回输入框和下拉框，打的字不丢；任务卡上的表单不单独重画，退回话题里的小卡（`show_answer_notice`）。旧卡上的只发补充说明（`bridge_question_skip`）和先问一句（`bridge_question_ask`）都走 `skip_all`：选了什么都不算，只发补充说明，补充说明是空的就在卡上拒绝并重画成新样子。
- 不处理（`bridge_question_set_aside`，`Questions.set_aside`）：只在 `aside_offered` 为真的表单上（话题的问题，不是 `native_live_question`、`native_paused_question` 那种只等这一个回答的会话），和提交回答并排、一样宽的按钮。卡锁内 `pending → set_aside`，存下每题“不回答此题”的答案（`set_aside_answers`，即只写补充说明却没写补充说明）、`response_kind: set_aside`，什么都不发。读 `pending` 的地方（等我的 `asks`、`pending_questions`、话题消息当回答、暂存消息、收起旧问题、每日维护）自然都不再算它；网页把只剩它的 `waiting_user` 任务读成结束了的一轮（`core/board_read.py` 的 `set_aside`）。撤销（`bridge_question_restore`，`restore`）`set_aside → pending`，去掉存的答案和时刻。话题下一轮（`publish_submission` 里的 `acknowledge_waiting_answer` 先调 `take_set_aside`，话题锁内、只改记录）把它转 `answered`（`continuation_job_id` 是这一轮），请求前面加 `set_aside_message`，暂存消息照回答的样子 `claim_held` 一起带上，卡和暂存消息卡另起线程重画。`retire_open_questions` 连它一起作废。
- 还有 N 件等你：主人自己答完一组题（`answered`，`response_kind` 不是反馈）或点了不处理（`set_aside`）后，`QuestionDisplay.paint` 在问题卡所在的私聊（或个人群）算一次 `overview.waiting_count`（`board_read.waits` 的 `count`，看板和手上的事数的同一个数）；不是 0 就把 `others_waiting`（不存盘）放进画卡的记录，`question_card` 在已答的卡下加一行灰字，有看板（`app_manifest.board`）时旁边「去看板」（`bridge_board_open`，`Digest.claim`：发一张新的看板卡）。点击重画（`clicked_card`、`copy_card`）不算这个数，那一次画出来的卡没有这行。
- 样张（[UI 规范 · 改外观先选样张](ui.md#12-改外观先选样张)）：检查点问题的选项带 `sample`（卡片 JSON 文件的绝对路径）时，`CheckpointQuestions.defer` 用 `core/ui_samples.py` 的 `carry` 读一次文件，把清成点不动的元素存进问题记录代替路径；`question_card` 在第一道题上方画出各版，答完就不画。带样张的问题不放在任务卡上（`deliver` 单独发卡），专项的话题问的不交负责人（`core/project_gates.py` 的 `SAMPLE`）。
- 一张卡放得下（`cards/question_cards.py` 的 `fitted`）：CardKit 一张卡最多 200 个带 tag 的节点（`plain_text` 也算，超了报 300305）、约 30 KB，问题卡按 195 个、29 KB 留余量，量的是发出去的整张卡：顶层副本的头像行、主人的话、这一轮输出和产物（`mirror_fit` 的 `outer`）跟表单一起算。放不下时一级一级少画：副本先把输出拆到前面单独的卡上（`mirror` 看 `Fit.whole`），再从最后一版往前不画样张本体（副本上不画的那版话题里的卡画了，就写“话题里的问题卡上有”），再把所有样张并成一行灰字加选项列表，再把每题的题目、背景、选项说明合成一段、去掉“怎么回答”的折叠，再去掉有选项的题的回答框，最后只列题目、留补充说明和提交回答（`BRIEF`：把回答写进补充说明提交，或到网页工作台逐题答；管家自己的题不提网页）。重画（已答、拒绝、产物刷新）走同一套。
- 卡没建成或没发出去（`feishu/messenger.py` 的 `publish_card`）：原地发一行字代替，写清哪个话题（T 号和标题，读不到就写读不到）、什么卡、为什么（`feishu/transport.py` 的 `reason`：300305 超 200 个组件、200860 超大小、230028 安全审核，其余按失败类别，未知的码带飞书原话的前 60 个字）、去哪接着做（调用方给的 `CardAbout.where`，问题卡是“在话题里回复作答，或到网页工作台答题”，副本是“到 T12 里的问题卡作答”），同一张卡只说一次（键不变）。顶层副本失败而这一轮的“等你”回带随后就发（`after_turn` 的布局有 `conclusion` 或 `stop`）时不另说，回带卡写了去哪答；自己会回一句的调用方（暂存卡、回答提示）传 `quiet`。
- 卡改不动（`feishu/messenger.py` 的 `patch_or_tell`）：整理专项、批量换配置、飞书应用待补、网页登录确认等卡整卡替换失败时，在卡下回一行说哪张卡没更新、为什么，同一张卡只说一次；超过飞书硬上限的卡在发送层就不发（`core/card_limits.py` 的 `check`，见 [cards.md](cards.md#卡片发不出去时)）。
- 先问一句现在只在代发草稿卡上（[代答 · 代发](proxy.md#代发)）：`Questions.queue_ask` 把 `ask_message` 写进历史，`enqueue_chat_job(steer=True)` 排成消息 id 为 `ask-…` 的一轮；`acknowledge_waiting_answer` 见到 `ask-` 不动在等回答的任务。问题卡上的先问一句已去掉，旧记录里留下的 `asked_first` 不再读。

## 撤回与编辑

主人撤回或编辑了已经发出的消息时，Bridge 跟着改（`app/source_watch.py`，`COVEY_SOURCE_WATCH=0` 关掉）。lark-cli 的事件总线既没有撤回事件也没有编辑事件，但消息读回来带着两个标记：撤回后 `deleted` 为真，编辑后 `updated` 为真、`update_time` 变新。所以靠读回。

- **登记**：`BridgeApp.handle_event` 把主人的一条消息变成任务或补充之后，记进 `Runtime.sources`（`core/source_book.py`），同时写进 `<state>/source-book.json`（`update_json`，锁内只写文件；开关关着不读不写）。每条带 `kind`：`task`（自己成了任务）、`steer`（补充）、`answer`（被当成对等着的问题的回答）、`held`（暂存，`app/held_notes.py`）；后两种由 `app/interaction.py` 登记（`source_watch.note_kind`）。只认主人（`Access.is_owner`）和 `om_` 开头的消息 ID（网页上发来的不算）；补充要能找到它进了哪个在跑的任务（那个任务的补充记录 `interaction-deliveries/`）。任务进程里不登记。
- **读回**：后台线程 `source-watch` 每 `WATCH_SECONDS`（15 秒）读一遍登记的消息（`GET /open-apis/im/v1/messages/<id>`，锁外）。任务还活着就一直读，结束后再读 `GRACE_SECONDS`（10 分钟），超过一天的不再读（`MAX_AGE_SECONDS`），最多同时登记 200 条。启动后先读回文件，重启前登记的消息接着读。读失败下一轮再读；读回说消息已撤回（`deleted`，或接口拒绝时报 230011）就按撤回处理。
- **撤回**：
  - 消息自己的任务还在排队：取消，不执行；在跑：像点了停止一样停下（`app/project_clicks.py` 的 `stop_task`，任务进程活着时转给它）。停止带 `by: recall`（`core/jobs.py` 的 `RECALLED`），记录的 `cancel_requested_by` 是 `recall`，卡上写“原消息已撤回”，不回文字（那条消息已经没了，回不了）。
  - 是一条补充：再给那个任务送一条补充，说主人撤回了它、不要再按它做。
  - 不管任务结没结束：这一轮记下的记忆（回答卡上的“记下了”，同“不对”）和知识条目（`by=topic`、`source.job_key` 是它）作废；管家这一轮开的、分叉的、交过活的话题（`core/butler_opened.py`）各在话题串里收到一张小卡“来源消息已撤回”，话题还有排队或在跑的任务时带一个停止按钮（就是任务卡的 `bridge_job_stop`，带 `by: recall`）。话题不自动停（主人定的）。答案已经发出的不撤回。
  - 沿派发链走到下游：管家把主人原话转交话题时，那一轮的轮次来源是 `relay`，`origin.message_id` 是主人这条消息（`core/turn_source.py` 的 `downstream` 列在跑的、`finished_downstream` 列已结束的）。撤回时这些任务所在的话题也各收到那张小卡（在跑的带停止，不自动停）。负责人派给成员话题的活（`lead`）来源里没有主人的消息 ID，链到负责人为止：成员话题由负责人决定，不越过它去动。
  - 被当成回答的消息撤回：话题里说一行“已作为回答提交，撤回不影响它”，什么都不改；暂存的消息撤回：像它卡上的“撤回”一样撤掉（已随回答带走的，同样说一行）。
- **编辑**：读回的正文和原来的不一样才算编辑（`same_words`，空白不计）。只看 `updated` 和 `update_time` 不够：消息下面开了话题串（管家把话题挂在主人那条消息下）时，飞书也把它标成更新过、更新时间变新，正文一字没改。真编辑时，改后的全文作为一条补充交给这条消息的任务（`Steering.deliver_steer`，补充 ID 是 `edit-<消息>-<update_time>`，同一次编辑只送一次）：排队中的并入它的请求，在跑的读到它的下一步（补充带 `requeue`，没读到时由 `app/steering.py` 重新排队）。任务已结束、在等回答，或后端不收补充时，在话题串里（管家的私聊在聊天区）发一句“你改了这条消息……这一轮是按改之前的内容做的，没有重做”，不自动再跑。沿派发链的下游同样：在跑的收到补充，已结束的在各自话题串里说一行。
- **被引用的消息**（`feishu/forward.py`）：回复一条已撤回的消息时，上下文写“已被发送者撤回”、不带它剩下的占位正文；编辑过的写“发出后编辑过”，正文是改后的。
- **重启不重发**：做过的动作（撤回的小卡、编辑的补充或一行说明）先记进登记条目的 `acted`，再去做；重启后重放核对，记过的不再发（规则 6：宁可漏一次不发两次）。
- **限制**：还在等启动卡（`waiting_workspace`）的任务、管家已经交给话题的任务说明、管家会话里的原文都不跟着改；编辑后的消息只取文字，新加的图片和文件不带。

## 结果授权与投递

公开回复、执行结果和续接权限是三个独立的事实：

- **公开回复**：`core/result.py` 的 `VerifiedRootReply`，只能由后端的完成检查构造；`fingerprint` 是全部字段的 SHA-256。只存本地，不代表允许发送。
- **执行结果**：`ExecutionOutcome`。`delivery_authorized` 要求身份、清理、保护都已确认；`resume_allowed` 只在 completed 且可投递时为真。
- **续接权限**：原生会话保护（`core/session_guard.py`）。后端一报告 SID 就建 active 记录；允许续接时删记录，否则标 `blocked`。

一轮的顺序（`app/worker.py`，结算在 `app/turn_settle.py` 的 `settle_turn`）：

1. 每次运行一个新的 `bridge_run_id`，后端报告的每个原生 SID 立即 `claim_native_session`。
2. 后端经 `verified_reply` 把候选存到 `verified-results/`，scope 是 backend、run_id、任务键、任务号、chat、topic、workdir；作废时代际加一。
3. 后端返回后：有 public_reply 没有 outcome 算错误；用户已停止时记 cancelled；多个根 SID 时禁止续接。
4. 先以不释放的方式结束会话保护，再 `authorize_verified_reply`（`core/saved_results.py`）：锁内核对 scope、代际和指纹，写 `authorized`；任务记录写 `outbox_state=pending`。
5. 授权且允许续接才释放保护并写会话指针。
6. 已授权的回复单独发送，幂等键 `<前缀>-verified-result-<任务键>-<指纹>`，首发和重投同一个键、同一种方式。

### 各后端的结果边界

什么算本轮的结果写在 [Harness 说明](harnesses.md)各节。根任务结束后等子任务和清理的期限由 `core/completion.py` 的 `CompletionBudget` 算。

### 投递、重投与 outbox

- **答案卡**：首发（`TurnDelivery.send`）和重投（`app/redelivery.py`）都经 `app/saved_send.py` 发同一张卡：同一个键、同一个位置（放置表的 `answer`），卡上有同样的产物、输入/输出两行和头像行（说话人）或状态行，所以重试时 argv 不变、飞书按 uuid 去重。首发在发之前把要发的卡记进 `answer-cards/<任务键>.json` 的 `outgoing`（锁内只写文件），重投按同一键取出原样再发（同一个键不配别的内容，否则回执按 `idempotency_conflict` 拦下），发出后再按上传状态改产物（`refresh_answer_cards`）。记下的卡带一个摘要（`digest`：卡和当时授权正文一起的 sha256），重投时用这次授权的正文重算，对不上或没有摘要（旧记录）就当没记下（规则 5）。没记下的（首发前任务进程就退了，或记录已过 7 天被清理）按任务记录和保存的执行过程重画；但这个键的卡已经发过（回执在）时不发重画的卡，记 `reconciliation_required` 停在 outbox，回复“可能已经送达”，不自动重发（规则 1、5）。以文件发的回答，文件之后跟一张产物与详情小卡（同样的头像行，你那句话收在它下面），重投也一样。一张卡放不下的回答（JSON 超过 `ANSWER_CARD_BYTES`，或再转义一次超过 `SENT_BYTES`，按加了 T 号链接后的卡量）分几张卡发（`cards/pages.py` 的 `answer_pages`）：每张同样的头像行写“1/3”，产物和输入/输出在最后一张，放不下时跟小卡；`outgoing` 的 `pages` 记下每张卡和它那段 Markdown（只有一张卡时不记，摘要和老版本的一样），重投原样逐张发，第 n 张键 `<键>-p<n>`；`answer_message_id` 是第一张，产物和记忆行写在带产物的那张。没记下、而这个键已按 Markdown 发过（老版本超长时这样发）的，重投仍整段按 Markdown 发同样的键（`feishu/delivery.py` 的 `markdown_tried`），产物与详情跟小卡。卡片可能已送达时不换格式；飞书明确拒收时改走 Markdown，同一段正文不会两条路线都可能送达。
- **暂停的问题和恢复原文**：问题正文（超过 `FILE_BYTES` 发文件）和问题控件失败后恢复原文，首发和重投都用 `app/saved_send.py` 的 `send_paused`、`restore_original`。问题正文只在没发任务卡的管家安静一轮发到顶层，其余回到它回复的位置；恢复原文先写回任务卡（重投没有任务卡可写），再发消息，最后发文件。
- **审核拒收**：飞书 DLP 拒收（230028，`audit_rejected`）时 `feishu/delivery.py` 按点名的类别打码（`mask_sensitive`），换 `<键>-masked` 发一次。单独发出的回答（`app/answer_delivery.py` 的 `_answer_apart`）打码后仍被拒时，`refused_as_file` 把同一段保存的正文作为文件发到回答本该在的地方，后面跟和长回答一样的小卡（产物、改动、输入/输出，`saved_send.plan` 的文件分支，键另起 `<键>-audit-file`，开头是这条回答的头像行：只有说话人和你的原话，不含被拒的回答），再发一行 `AUDIT_FILE_NOTE` 说明原因，任务卡也不放回答的首句；只发保存的正文，不重跑模型（规则 5）。文件也没发出去时仍记 `audit_rejected`，不给按钮。
- **任务卡发不出去**：`StreamingCard.create`（`app/task_card.py`）里卡片消息被明确拒绝（`feishu/delivery.py` 的 `refused_outright`，不含可能已送达的）时，`_send_elsewhere` 换 `<键>-again` 原处再回复一次，再换 `<键>-top` 发到会话顶层；每次失败写进任务的 `last_card_error`。仍没有卡时 `job.card_missing` 为真，随交接的 extras 带给任务进程，状态行的每次更新（`Cards.update_card_activity` → `resend_missing`）每 `CARD_RESEND_EVERY` 秒补发一次，最多 `CARD_RESENDS` 次，补上后把 `card_id`、`card_message_id` 写回任务记录。放弃卡改发文字的（`replace_card`）不补。没卡的任务结论照常单独发出（`TurnDelivery._answer_apart`、`_fallback`）。
- **送达闸门**：`CheckpointQuestions.undelivered_jobs` 列出还要重投的任务，话题的新消息和换配置据此拦下。
- **幂等写**：`feishu/transport.py` 发请求前先写 `delivery-receipts/` 回执并 fsync；同一 UUID 配不同内容失败（`idempotency_conflict`）；不确定的写只在有限时间内用原 UUID 重试，之后返回 `reconciliation_required`，不换键盲发。写已有卡片时，前一次不确定后的重试收到 200770 或 300315 算前一次已生效。
- **传输**：开了 SDK（[飞书 SDK 与直连](configuration.md#飞书-sdk-与直连)）时由 `feishu/sdk.py` 发，请求由 `feishu/message_request.py` 从 lark-cli 的 argv 重建、逐字节相同，新旧进程共用回执；请求发出后不换传输。
- **重投**：在这个任务的保存结果锁内核对任务号、运行 ID、代际和指纹，只读已授权的正文，不启动模型；放开锁再发。只有 Bridge 重投。
- **outbox**：`app/outbox.py` 定时扫描已授权、未送达、未暂停、到时的记录，从不运行 Harness；可重试的失败指数退避，用完转 `paused` 等用户处理。
- **自动重试**（`app/auto_retry.py`，见[自动重试](reference.md#自动重试)）按 `KINDS` 分限流、临时故障（`server_fault`）、本轮未全部完成、重启打断四种，各有次数上限，相加不相乘；做了实事的限流或故障重试把次数清零，在恢复时间重试后连续没有进展就停（`MAX_IDLE`）。恢复时间由 `core/limit_reset.py` 从 Claude 的输出读出，经 `RunResult.limit` 到 `plan`。到点时锁内 `pending` → `submitting`，锁外重试，状态只按 `AUTO_RETRY_TRANSITIONS` 前进；唯一例外是官方拒绝的停止直接转 `cancelled`。官方安全拒绝后换旧模型重试一次（`app/policy_retry.py`，[policy-stop.md](policy-stop.md#换旧模型重试一次)）不走 `auto_retry_state`，另用 `policy_retry_state` 和 `POLICY_RETRY_TRANSITIONS`（同样两步），由 `policy_retry.tick` 经 `TaskControls.requeue(fallback=…)` 排上。
- **重跑还是拒绝**：什么都没保存的一轮按原快照重新排队；有保留候选的拒绝重跑；候选全部作废、从没授权过的（`withdrawn_unauthorized`）算没保存。各后端的投递不变量由 `test_backend_contract` 验证。

## 等主人的账

等主人的事（等你、停住了、可能卡住、有结果）记在一本账上，看板、手上的事、早晚汇总、网页和管家的快照都从它数，同一时刻说同一个数；每件事一条，带着打开它的那张卡。Bridge 定时对账：账上有、聊天里没有卡的，再发一次，发了几次、发出几次都记下来，诊断里查得到。

- **账**（`app/owner_ledger.py` 的 `of`）：从读者已经做过的那次看板读取（`app/pins.py` 的 `PinnedTopics.look`）出发，取等我（`core/board_read.py` 的 `waits`）、要你处理（`Asks`：主人私聊里带整理专项等你定的提议，群里没有）和有结果（`unseen`）。每件事一条（`core/owner_ledger.py` 的 `Entry`）：`what`（问题、等开始、提议、停住的结果、护栏停住的专项、可能卡住、没看的结果）、稳定的 `key`（`what:id`）、话题、从什么时候起、打开它的卡。话题停住的结果算停住了，不再算有结果。`counts` 和各卡顶上的数一致（`tests/test_owner_ledger.py`、`tests/test_unseen_results.py` 的 `test_the_ledger_is_what_every_card_counts` 核对）。
- **没到聊天里的卡**（`unshown`）：问题还在等主人答，没交给负责人分流（不是 `held`，负责人答过的也不算），问了至少 `GRACE`（180 秒），不超过 `MISSING_AGE`（2 天），但卡从没发出去（`display_pending`，或没有 `question_message_id`）。`board_read.asks` 对它是假，所以以前哪张卡都不数它，也没人补发，主人就看不见。现在它作为 `missing` 记在账上，卡到了之前不计数。
- **对账**（`Reconcile`，`owner-ledger` 后台线程，`COVEY_LEDGER_RECONCILE` 秒一次，默认 300，`0` 不起线程）：对主人私聊和每个个人群做一次账，把计数和缺卡写进 `<state>/owner-ledger.json`。缺的卡用它自己的 Harness 再发一次（`app/triage.py` 的 `show`：在跑的问题交给任务进程发，检查点问题由 Bridge 发，并补顶层副本），发出去后把因为卡没到而记成失败的任务改回等回答（`mend`）。三种不归对账发：交给主人但卡没到的分流问题，归 `app/triage.py` 的定时扫；要先送结果的，归 outbox；任务已经不再等它的，归过期关闭。
- **补发只做有限次**：每次先在记录里记一次尝试，再在锁外发；发完记结果（发出 / 没发出和原因），持记录锁时不发飞书请求（规则 1、2）。同一张卡最多 `TRIES`（3）次，两次之间隔 `RESEND_EVERY`（600 秒）；发出过的不再发。记录只有 Bridge 写，保留最近 200 条尝试，`totals` 一直累计。
- **查**：设置卡「诊断」和 `covey_doctor.py` 的摘要多一行“等你的事”，有缺卡、补发没发出时标橙（`service/doctor.py` 的 `ledger_row`）；`python3 -B bin/covey_ledger.py --state-dir <状态目录> [--json]` 只读列出按记录现在数到的每件事、上次对账缺的卡和每次补发。

## 代答

实现见[代答](proxy.md)，用户行为见[代答](usage.md#代答)，配置见[代答的配置](configuration.md#代答)。它落在全局规则上的地方：

- **记录与状态**：`<state>/proxy/` 由 `core/proxy.py` 读写，只有 Bridge 写。批次状态只按 `BATCH_TRANSITIONS` 前进，由后台线程 `proxy` 写。
- **锁**（规则 1）：`Proxy.lock` 内只读写记录；发前闸门在锁内、转 `sending` 前再判断一次；锁外跑发送工具。`web_lock` 保证同一时刻一次发送或扫码登录。
- **不重发**（规则 1、6）：按过回车的只读回，找到是 `sent`，否则 `uncertain`。
- **卡片**（规则 3、8、9）：设置卡和重新发送卡在自己的卡锁内取 sequence、整卡替换；kind `bridge_proxy_*` 在 `ACTIONS`，由 Bridge 自己处理。
- **授权**（规则 5）：只发 `load_authorized_reply` 取到的已授权回复；重新发送不跑模型。
- **发前检查**（`Proxy.check_reply`，`COVEY_PROXY_REPLY_CHECK`）：`decide` 发之前查回复里的本机路径、内部编号、像密钥的字符串和没填的占位符，默认只记（`<state>/proxy/reply-checks.json`，只有 `proxy` 后台线程写），`hold` 时命中的转 `held` 留给主人；不调飞书，不进 `Proxy.lock`。代答话题的工具收窄见[一次运行](#一次运行)的 `TOOL_LIMIT`（`proxy`）。
- **主人**（规则 11）：主人判断来自 `Access`；`owner_user_id` 只用来认出主人自己发的消息。
- **网页端登录**：`<state>/proxy/web/profile`（0700）只有一份、不进备份。它的缓存目录（`core/maintenance.py` 的 `WEB_CACHES`）合计超过 200 MiB 时，维护后台线程（每小时看一次）在没有发送中的批次、没有进程用着它时删掉。

## 设备（多设备，默认关）

让话题的 Agent 在另一台 Linux 机器上跑（`COVEY_DEVICES=1` 加 `devices.json`），Bridge 仍在原来的机器上收发飞书消息、写卡片、存记录。配置、字段、浏览和自检的用法见[配置：多设备](configuration.md#多设备开发中默认关)，自检、排障和回滚见[运维](operations.md#设备自检)，Harness 在设备上的差别见[Harness 说明](harnesses.md#在设备上运行)。这里只写结构和不变量。

**总形状**：设备上跑一个单文件小程序（`core/device_shim.py`，协议见上面的[设备端小程序](#设备端小程序)），它起并看守 Agent、把输出写进只追加的 journal；hub 经 ssh 的一条主连接（`core/device_transport.py`）和它说话。后端不知道自己在设备上：`core/process.py` 的 `spawn_process` 在上下文带 `device` 时返回 `core/device_link.py` 的 `DeviceProcess`（像 `Popen`：stdin、stdout、`poll`、`wait`、信号），Claude 的 `ClaudeProcess`、Codex 的 `AppServer` 和 Antigravity 走的 `stream_subprocess_lines`（带上下文）照常读写管道。链路后台线程按字节偏移 `attach`、丢已见的帧、断线退避重连，请求账本和偏移存在 `runs/<任务键>/device.json`，所以重连和 runner 重启都不重放已投递的帧。`别名:路径` 在 hub 上解码成一个永远不存在的占位目录加 `device`、`remote_workdir`（`bind_workdir`），没改到的代码路径因此明确失败，不会碰到本机目录。

**设备门**：一个设备话题的每一轮要依次过下面几道，哪一道不过都是当场拒绝并在卡上写原因，不排队、不改到本机跑。
- **开关与名单**：`devices.enabled()` 和 `devices.json` 里有这个别名；开关关着时 `gpu1:/x` 只是一个普通（相对）路径，一切同旧。
- **谁能用、哪个 Harness**：`Access.allows_device(chat, device)`（规则 11：谁被服务只经 `app/access.py`）和设备声明的 `harnesses`；启动卡、换配置卡只列能用的设备，点击和每一轮开始前各再查一次（`app/runs.py` 的 `_device_refusal`）。设备能声明的是 `core/roster.py` 里 `devices` 为真的 Harness（`devices.harness_names`：核心是 claude、codex、agy；不写时是 claude、codex），`device_link.device_argv` 也只替换这几个的命令。管家的 `topic_start`（专项负责人的 `project_topic_start` 走同一个函数）开话题前查同样几道再加目录（`app/butler_topics.py` 的 `device_binding`），不过就不开话题；专项的负责人留在本机（`Projects.step_lead`，它的工具在本机），状态包写明设备工作区。
- **目录和可用性**：设备经 `stat` 答复目录在不在（5 秒，设备的话，不是 hub 的 `Path.exists`）；`app/availability.py` 读设备最近的 `hello` 和自检结果，不在 hub 上找命令。
- **能力位**：`core/harness.py` 的 `for_device(caps, ctx)`（`DEVICE_OFF`）在设备上关掉 `native_fork`、`compaction`、`session_files`、`redaction`、`rewind`、`command_stop`（补充打断命令要在本机找命令的进程）；Antigravity 在设备上另关 `steering`、`steer_reads`（它的补充靠 SIGINT 和本机进程树，设备端程序只转发 TERM、KILL）；各入口（回退、下载、压缩、分叉、启动卡的脱敏选项）都经 `backend_capabilities` 取值，所以按钮不出现、不是出现后点了才拒。几处没有 `pid` 的地方有闸门：Claude 和 Antigravity 的轮前压缩、两者的 `compact_session`、管家的空闲压缩和压缩子进程在设备上直接返回。话题开着脱敏时设备上的这一轮拒绝，不会不脱敏地发出去。
- **hub 上的副作用**：设备话题不取 git diff 快照，不在占位目录里跑 `git`、不写文件；`tests/test_device_gates.py` 在整轮执行期间用审计钩子（`tests/support/device_guard.py`）记录所有 `open`、`stat`、`subprocess`，断言占位目录从未被碰、spawn 规格的环境里没有 hub 的 `LARK_*`、`FEISHU_*`、`COVEY_*`、提示词和后台线程参数里没有 hub 的路径。设备话题里新加的任何 hub 侧文件操作都要走这个审计。

**检查点镜像**：`job.checkpoint_path` 仍是 hub 路径，hub 侧所有读写不变；提示词里写的是设备上的镜像路径（`~` 下 `.covey/checkpoints/<hub-id>/<任务键>.json`，`devices.checkpoint_mirror`）。首轮把 hub 当前的检查点作为种子文件随 `spawn` 放过去，设备端每秒比较文件的 mtime、大小和 inode，变了就写一条 `cp` 记录，链路后台线程原子写回 hub 那份（上限 1 MiB，超了只标记不截断）；Agent 退出后再比较一次才写 `exit`。hub 对检查点的改写（如 `release_user_wait`）只改 hub 那份，下一轮是新任务键、新文件，不需要回写。

**文件进出**（`app/device_files.py`，hub 从不打开设备上的路径）：
- **进**：提示词引用的 `incoming_media` 文件开跑前经 `put` 放到设备的 `.covey/incoming/<hub-id>/<任务键>/`，提示词里的 hub 路径换成设备路径；超限（单个 64 MiB、每轮 256 MiB）、读不了、设备不答复的文件跳过，原处写「未传到设备」，卡上记一行。执行中补充（`app/steering.py` 的 `deliver_steer`）、等回答时的补充说明、随原生问题的回答一起发出的附件和另外补充（`app/questions.py`）走同一个函数 `stage_message`，处理同首轮；已入队但还没开跑的任务不传，它的请求在开跑时统一处理。Claude 的图片仍由 hub 读本机文件再做 base64，Codex 的图片用设备上的副本。
- **出**：检查点里的产物和 `show_images` 经设备端 `stat`、`get`：名单（`SENSITIVE_NAMES`、代理目录）作为 `deny_names`、`deny_dirs` 发给设备，真实路径和越界在设备上算，hub 对设备报回的路径每一段再判一遍；取回到 `runs/<任务键>/device-files/`，再走和本机文件一样的上传。设备上的产物必须在设备工作区内，图不限；目录暂不支持。

**浏览与自检**：目录浏览器（`app/directory_picker.py`）在搜索框里认 `别名:路径`，列目录是设备端的 `ls`，搜索是在设备上跑一小段 Python，结果缓存的键带设备别名，视图带 `device`，设备视图从不展开 hub 的 `~`；谁能浏览仍是主人，而且聊天要在设备的 `spaces` 里。自检 `core/device_probe.py` 的 `self_check` 和 `bin/covey_devices.py check`、设置卡的设备状态行共用：ssh 探测、推小程序、`hello`、导入环境档案（只收白名单名字，带凭据的代理拒收）、对每个声明的 Harness 真跑一次最小请求，结论写进 `devices/<别名>/profile.json`。画卡时读缓存，不去问设备。

**离线与恢复**：设备上的 Agent 由设备端小程序守着，hub 连不上时它照常跑，所以「失联」和「停掉」是两件事。连接断开累计超过耐心值（`patience_minutes`，等回答时改用 `waiting_patience_hours`）这一轮结算为 `interrupted`、原因 `device_lost`，不自动再试。凡是要停设备上的进程但当场连不上的（失联、断线时点停止、任务进程退出、Bridge 重启、官方拒绝的停止），都只在 `devices/<别名>/pending-reap.json` 登记一条，`app/devices.py` 的 `devices` 后台线程（60 秒一轮，只在开关开着且有设备时才起）探活、重放登记、每小时让设备 `gc`；同一话题的下一轮开始前先 `reap` 上一个设备任务（`core/device_reap.py`），设备不确认就不起新一轮。`reap` 只杀确认属于该任务的进程组。收口挂在三处，都只登记或调 `reap`：`app/detached.py` 的 `finish_detached`、`app/app.py` 的 `notify_stale_jobs`、`app/policy_guard.py` 的 `stop_tree` 之后；官方拒绝的停止在设备确认前不标已确认（`cleanup_unconfirmed`）。状态行（`job.device_state` 进 `core/longtask.py` 的 `Facts`）的文案和各情形的处理见下面的「卡上写什么」。

**连接**：每台设备一条 ssh 主连接（`ControlMaster=auto`、`ControlPersist=600`、`ControlPath=<control_dir>/%C.sock`），之后的每次调用复用它，不再握手。命令行固定带 `-T`、`BatchMode=yes`（从不问密码）、`ServerAliveInterval=15`、`ConnectTimeout`、`LogLevel=ERROR`；`ssh.options` 排在这些之后，ssh 对同一个选项取第一个值，所以改不掉它们。`control_dir` 最长 41 字节：Unix 套接字路径加上 `/<40 位>.sock` 和 ssh 建套接字时的临时后缀后不能超过 104 字节（Linux 上 44 字节还能建连、45 字节起 ssh 报 `unix_listener: path ... too long`，多留的 3 字节给 macOS 的上限）；目录在第一次真连设备时才建（0700）。设备端程序按内容哈希放在设备的 `~/.covey/shim/<哈希>/shim.py`，设备上已经有同样内容就不再推。

**工作区和上下文**：工作区列表里存规范化后的 `别名:路径`（`//`、`.`、`..` 折掉，不留结尾的 `/`；`~` 留给设备展开），重名加 `-2`；登记时经设备端的 `stat` 检查，限时 5 秒；`covey_workspaces.py` 命令行不认设备值。`topic.json` 的 `workdir` 存这个规范化的值，读出来时才变成占位路径（`<状态目录>/devices/<别名>/standin/<12 位哈希>`，永远不存在）加 `device`、`remote_workdir`；上下文 `BridgeContext` 和任务记录只在非空时写这两个字段，所以旧记录照旧可读，设备话题的记录在旧版本上读到的是不存在的占位路径，会被拒绝，不会在本机跑。每一轮开始前设备答不出目录、或没声明当前 Harness，用 `workdir_missing` 错误码拒绝，说明文字写明是哪种。工作区管理页和 `covey_workspaces.py prune` 对设备工作区的目录检查返回「没答复」而不是「不存在」，所以不会把它们当成已删除的目录移除；工作区页设备那一行的目录状态是「未确认」。

**设备上的环境**：任务进程的环境从空开始，不是 Bridge 的环境；只给 `PWD`（设备上的目录）加设备环境档案里通过白名单的变量，再加 `devices.json` 的 `env`。白名单只有代理和证书路径（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写、`SSL_CERT_FILE`、`NODE_EXTRA_CA_CERTS`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`）；带 `user:密码@` 的代理地址拒收；`LARK_`、`FEISHU_`、`COVEY_` 开头的名字和 Claude、OpenAI 令牌即使写在 `env` 里也丢掉。日志只记被丢的变量名和原因，不记值。Claude 的令牌留在设备上，不经过 Bridge。

**自检的步骤**：5 秒 ssh（BatchMode）→ 推设备端程序（声明了 Codex 就再推 `workspace_reader.py`）→ `hello` → 导入环境档案（来源依次是设备 `~/.claude/settings.json` 的 `env`、设备进程环境里的代理变量、`login_shell: true` 时登录 shell 的变量，`devices.json` 的 `env` 最后覆盖）→ 对声明的每个 Harness 真跑一次最小请求（`core/harness_probe.py` 的 `command_for`：`claude -p`、`codex exec`，经设备端的 `exec`，带档案里的环境，限 60 秒；没有最小请求的 Harness 只查程序，`probe` 记 `probed: false`，结论写“只查了程序”；Antigravity 的登录是一个文件，设备端程序的 `hello` 看它在不在（`SIGN_IN_FILES`），不在时 `login` 为 `no`，自检判没登录，每轮开始前也按没登录拒绝）→ 写 `profile.json`。连不上模型服务时，档案里没有代理就说去哪里设 `HTTPS_PROXY`，有代理就说代理不通。结论写进 `profile.json` 的 `check`，设置卡的设备状态行和 `service/doctor.py` 的 `devices` 读同一份。

**设备上的可用性和模型目录**：每一轮开始前的可用性检查（`app/availability.py`）读设备最近的 `hello`（10 分钟内的缓存）和 `profile.json` 里最近一次最小请求：命令不在、`login` 为 `no`、最近一次最小请求判为没登录，都按「模型后端尚未登录/不可用」拒绝；没有近期的 `hello` 就放行，由这一轮自己的连接说话。Claude 的模型目录经 `spawn_process` 起在设备上；Codex 的 `models_cache.json` 用设备端的 `get` 从设备的 `~/.codex/` 取到 `devices/<别名>/codex-home/`（10 分钟内不重取，设备没答复用旧副本，没有就是空目录），再交给 `core/model_settings.py` 的 `model_catalog(root)`，所以设备上设了别的 `CODEX_HOME` 就取不到。启动卡和换配置卡列出的模型仍是 Bridge 本机的目录；设备上的话题不用 hub 上模型网关的检查结果。

**卡上写什么**：开新一轮时 5 秒内连不上，这一轮不开始，卡上写「gpu1 连不上（原因）…」，点检查并继续重试；运行中断线，状态行写「gpu1 连接中断，重连中（设备上照常在跑）· 已断 2 分 10 秒」，重连后补读日志，执行过程里多一条「gpu1 已重新连上」；等你回答时断线，写「任务停在等你回答，连上后你的回答会送过去」；超过耐心值写「gpu1 失联超过 15 分钟，这一轮按中断处理；设备上的进程连上后会停掉」；断线时点停止写「已请求停止，gpu1 连上后生效」；设备重启（设备说没有这个任务）按失败结算，写「gpu1 重启过，这一轮按失败处理；会话还在设备上，检查并继续会接着做」；Bridge 重启或任务进程退出（记录分不出这两种），恢复卡写「上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1 上这一轮可能还在跑；点检查并继续会先停掉它再接着做」。连上并稳定 5 分钟后，断线累计重新算。设备上的检查点超过 1 MiB 时不再同步，状态行加一行标注。

**文件的上限**：附件单个 64 MiB、一轮合计 256 MiB；产物的上限同本机（旧的逐文件发送 20 MB，云空间上传 200 MB）；`show_images` 的图不超过 10 MB、同一轮最多 5 张，不要求在工作区内（二维码常存在 `/tmp`），同一张图（设备、路径、大小、修改时间）一轮只发一次。产物名单同本机（`.ssh`、`.git`、`.codex`、`.claude` 等代理目录、`.env*`、`auth.json`、`credentials.json`、`.local-state`）。答案卡的「输入/输出」框不显示设备文件的大小；知识库提示里不带历史搜索命令（它是 Bridge 本机的命令）。

**设备上的清理**：`devices` 后台线程每小时让设备端 `gc` 一次：清过期的任务目录、没人引用的小程序目录，以及 `~/.covey/checkpoints/<hub-id>/` 和 `~/.covey/incoming/<hub-id>/` 下 7 天没变、也不属于运行中任务的条目。

**记录**（都在 `<状态目录>/devices/` 下）：`hub-id`（这台 Bridge 的编号，两个 Bridge 连同一台设备互不干扰）、`<别名>/hello.json`（最近一次 `hello`）、`<别名>/health.json`（后台线程最近一次探活）、`<别名>/pending-reap.json`（待收割的任务键、设备上的任务目录和进程组）、`<别名>/tasks/<话题>.json`（每个话题上一个设备任务）、`<别名>/profile.json`（环境档案：Harness 路径、版本、登录状态、通过白名单的变量、每个 Harness 最小请求的结果 `probe` 和最近一次自检的结论 `check`）、`<别名>/codex-home/models_cache.json`。

**设备端小程序协议的要点**（细节见上面的[设备端小程序](#设备端小程序)）：版本字段 `proto`，字段只加不改；命令的 stdout 第一行永远是 `hi`；`spawn` 按任务键和规格哈希幂等，输入按 `seq`、记录按偏移、`put` 按哈希、`reap` 可重复，所以 hub 这边「不确定就重发同一帧」是安全的；链路层错误（`LinkError`）不说明命令有没有执行，只重发幂等的命令。小程序只用标准库、按 Python 3.9 写，改它之后跑 `scripts/check_shim_compat.py`（它也检查 hub 写进 `~/.covey/` 的目录 `gc` 都清得到）。

**关着时不变**：关开关，或开了但 `devices.json` 里没有设备，不读文件、不建 `<状态目录>/devices/`、不起后台线程，上下文、任务记录里没有 `device` 字段，卡片 JSON 和 `runs/` 协议同旧；新增代码都以「记录或上下文里有 `device`」为前提。验证它：`tests/golden/run_dir.json` 和 `cards.json` 无 diff、`golden_trace` 对关着的模块无差异、`scripts/cross_release.py` 的 `devices-empty-new` 场景等于关着，以及回滚场景 `device-rollback`（[测试](testing.md#任务进程协议与跨版本检查)）。真机验收还在进行，所以默认关。

## 节点（默认关）

每台机器（Hub 本机和每台设备）上一个常驻的节点，主动连 Hub、一直连着，报告机器状况；设了 `COVEY_NODE_LISTEN` 才有（[配置](configuration.md#节点与机器状况默认关)）。第 1 期只做连接、加入、采样、记录和告警；设备上的任务仍走[设备](#设备多设备默认关)的 ssh 方式，改走这条连接是下一期。

- **节点程序**：`core/node_agent.py`，和设备端小程序同样的约束（单文件、只用标准库、Python 3.9，`scripts/check_shim_compat.py` 检查），不导入 `covey`。装在 `~/.covey/node/`，`flock` 保证一台机器一个；`join`、`run`、`ensure`、`status`、`uninstall`。
- **连接**：节点拨 Hub（TLS，按 Hub 证书 DER 的 SHA-256 固定），Hub 不连设备。`hello`（别名和密钥）→ `welcome`（Hub 已有的序号、应跑的版本）→ 补传 → 每个新样本一行；Hub 每 30 秒回 `ack`。断了按 1 秒起、翻倍到 60 秒重连；版本不同时节点从 `/node.py` 取新文件、校验哈希后重启自己，所以部署后各节点跟着升级。
- **加入**：一次性口令（`core/nodes.py` 的 `open_join`/`claim`）换一把设备密钥，Hub 只存哈希；粘贴的命令是 `curl --pinnedpubkey … /join/<口令> | python3 -`。同一个端口上 `GET` 走 HTTP（只有 `/join/<口令>` 和 `/node.py`），其余是节点协议。那行命令由 `app/node_gateway.py` 的 `paste_command` 给出（按 Hub 公钥固定，不走代理）；节点装好后写自启：`~/.bashrc` 顶部的登录钩子，有 systemd 用户服务或 launchd 时也装（`COVEY_NODE_AUTOSTART=hook` 只装钩子）。移除是 `core/nodes.py` 的 `revoke`。监听地址的 `$名字` 由 `core/listen.py` 的 `parse_bind` 展开；没设 `COVEY_NODE_ADDRESS` 时 Hub 推算的地址是本机默认路由的 IPv6 地址加端口（`link_host`）。
- **本机节点**：名字 `local`，`app/nodes.py` 每分钟确认 `~/.covey/node/` 里的节点在跑；它每 10 分钟试写一次数据根，写不进（配额满、挂了）就告警。
- **记录**：`<状态目录>/nodes/`：`registry.json`（节点名单，只存密钥的哈希）、`<节点>/latest.json`、`link.json`、`raw/`（原样，3 天）、`minutes/`（每分钟一行，30 天）、`alerts.jsonl`；`events/`、`alerts/` 是告警的待发和已发记录。
- **采样**（`Sampler`）：每 10 秒一份；内存到上限 80%、60 秒涨上限的 5%、OOM 计数增加时改 2 秒一份至少 5 分钟。cgroup v1/v2 的内存与 CPU（没有上限时用整机）、PSI、load、各路径的空间（加每 10 分钟一次试写，单独线程，卡住的路径报 `hung`）、`nvidia-smi`、RSS 和 CPU 最多的进程（环境里只读 `COVEY_TASK_DIR`，每个进程读一次，带时限）。为看盘：每 60 秒读一次各进程的 `/proc/<pid>/io`，报这段时间写入至少 16 MiB 的进程中最多的 5 个（`writers`，只读得到本用户的进程）；用到 85% 以上的路径还带 `growth`（每 10 分钟记一个已用量，报近 1 小时、近 24 小时涨了多少）和 `dirs`（`biggest`：同一块盘只数一次、30 分钟至多一次、单独线程、限时 120 秒，没数完标 `partial`；不进 `/proc/self/mountinfo` 里的任何挂载点，没有它时不进别的设备上的目录，所以不会走进 NAS；文件不按设备号筛，因为 overlay 给文件报的是各层的设备号，`du -x` 在 overlay 上会因此漏算。报两层目录，占总量四分之一以上的再展开一层，根和第一层目录里散落的文件单算一项并带最常见的文件名形状，如 `build-artifacts-*.tar`）。先写本地环形缓冲，再发 Hub。
- **Hub 端**：`app/node_gateway.py` 在 Bridge 里起 TLS 服务（`nodes-gateway` 线程），写 `<状态目录>/nodes/<节点>/` 的记录、折每分钟一行、按 `core/node_rules.py` 判规则，事件写进 `nodes/events/`；`app/node_alerts.py`（`nodes-alerts`）发卡、恢复时原地改卡、判失联；`app/nodes.py`（`nodes-local`）拉起本机节点、每小时清旧文件。Bridge 重启时连接断开，节点几秒到一分钟内重连并补传，样本不丢不重。
- **告警卡**：内存和 OOM 告警列内存占用最多的进程（`top`），发到第一个能对上任务的话题；盘满和写不进的告警不带 `top`（进程内存和谁占了盘无关），改列增长和按这个速度多久写满、写入最多的进程（`write_bytes` 是所有盘合计，含网络盘）、大目录（`cards/node_cards.py` 的 `disk_sections`），发到私聊。发在私聊顶层的告警（节点的、内存看门狗的）记进 `<state>/top-notices.json`（最近 200 条，`app/pins.py` 的 `remember_notice`），回复它交给管家，和回复「手上的事」一样（`app/pins.py` 的 `panel_message`）。
- **夜里**（`core/quiet_hours.py`）：不严重的告警（内存紧张、盘快满、失联，`app/node_alerts.py` 的 `severe` 以外的）留在待发里，夜里过完再发，卡上的“从 … 起”不是今天时带日期；夜里就恢复的照旧不发。有进程因内存不足被杀、盘写不进的随时发。
- **不变量**：关着时不读不写任何文件、不起线程；节点不监听端口、不读别的环境变量；没有记录锁被跨网络持有；告警只发一次，恢复只改原卡。

## 管家同时做几件事

使用者看到的行为见[管家 · 同时做几件事](butler.md#同时做几件事)。

目标：管家这一轮在做事时，主人的下一句不被挡在后面。做法是把管家变成一个**话题家族**：槽 0 是现在的管家话题（`butler_topic_id`），再加固定 4 个槽（`track_topic_id(chat, 1..4)`，同样是 32 位十六进制）；每个槽是一条“路”，路里的一轮是普通的管家任务。调度器不用改：`core/scheduler.py` 按 `(chat, topic)` 排队，不同槽并行、同槽先进先出。

底座（`core/butler_tracks.py`、家族谓词、`family_live`）：

- **开关**（`core/butler_tracks.py`）：默认开，`COVEY_BUTLER_TRACKS=0` 关；`COVEY_BUTLER_TRACKS_MAX` 是同时在跑的件数上限（默认 3，取值 2 到 4）。见[配置 · 环境变量](configuration.md#环境变量与选项一览)。
- **家族谓词**（`core/butler.py`）：`is_butler_topic` 开关开着时认槽 0 到 4（这些事的话题也是管家：工具只认管家任务、话题清单和看板排除、一轮的规则和卡片照管家），关着时只认槽 0，和原来一字不同；`is_butler_root` 只认槽 0。哪些调用点按家族、哪些只认根，改的时候看调用处的注释。`track_of(topic_id, chat)` 给槽号。
- **分叉点**：这些事的会话是槽 0 会话的分叉。`ctx.options["fork_session"]` 带 `ctx.options["fork_at"]` 时，Claude 后端加 `--resume <会话> --resume-session-at <fork_at> --fork-session`，分叉只含到那一条为止的内容；槽 0 正在跑的那一轮（提问已写进记录）取它提问的父条目，分叉里就没有这一轮的半截。
- **槽话题的 `topic.json`**：`Butler.keep_track_topic` 从槽 0 复制模型、思考预算、工作区、脱敏等设置，加 `track: k`；管家的设置变了，下次调用时同步过去。
- **存活查询**：`JobRegistry.family_live(chat)` 给家族里没结束、不在等回答的任务（只读内存），用来数件数。

**运行时**（`app/butler_tracks.py` 的 `Tracks`，`Butler.handle_event` 在原路径前调 `Tracks.handle`）：

- **开不开**：`COVEY_BUTLER_TRACKS` 没设成关闭值，并且管家允许、模式是管家（`Tracks.enabled`；`core/butler_tracks.enabled` 只看环境变量）。关着时 `handle_event` 一字不变；开着但家族里没有活任务，`handle` 回 False，也走原路径（槽 0，不分叉，不判断）。
- **活任务**：本进程登记表里家族的没结束的任务（排队、开始、在跑、在等回答），加上话题记录里 `waiting_user` 的（6 小时内；槽 0 的只在别处有活任务时才算）。每个交给路由层一条 `LiveJob`。
- **认引用**：`parent_id` 先查补充送达记录（`merged`），再看是不是某个活任务的主人原消息、卡片或回复目标（`live_card`），再查别名（槽里有在等回答的任务时当作它的问题卡；其余是 `ended`；谁也不认识是 `unknown`）。
- **分流**：`core/track_route.py` 的 `route` 九条规则；`ask_judge` 时调 `app/track_judge.py` 的 `judge`（处理事件的后台线程里同步等，不持锁），判完重读活任务，目标刚结束就新开一件。
- **每问现起一个进程**：判断经辅助模型（`app/assist.py` 的 `judge`）问一次，Claude Code 是一次性进程（冷起 p50 约 3.4 秒，硬上限 `COVEY_TRACK_JUDGE_MS` 默认 6 秒），Codex、Antigravity 是它们自己的命令行（默认 10 秒）。问之间不留进程：以前的待命进程每条管家消息都先起两个、各用一次真实请求热身，只换来每问快约 1 秒，已删。
- **输入变小**（`COVEY_TRACK_JUDGE_SLIM`，默认开）：Claude Code 命令行多带 `--setting-sources user --disable-slash-commands`，不再把工作目录一路向上的项目设置、记忆文件和技能清单放在每个问题前面，输入约 800 token（以前约 4800）。凭据不变，仍是 Harness 自己的环境和登录；Claude Code 不认这两个选项时记住、之后不再带。

日志行格式（`track judge route=… via=…`）不写主人的原话；`COVEY_BUTLER_TRACKS` 关了、模式不是管家、通道不是 `claude` 时不起任何进程。

转发、图片、文件、只有链接的消息之后 30 秒内的文字（没引用、规则落到第 9 条）直接并进那条消息去的那件事，不进分流。
- **动作**：`stop`（停止消息不建任务、不进模型）：任务在任务进程里时，把它任务卡上“停止”按钮的点击转给那个任务进程（`runner_ipc.forward_card_action`，同代答的 `proxy.stop_job`），由任务进程里的 `cancel_reply_job_from_card` 停自己的任务；任务还在 Bridge 里（排队、没交出去）才在 Bridge 里直接调它。只在 Bridge 里给任务的副本设停止标记到不了任务进程，后台命令和答案照常出来；主人引用的消息按 `reply_to` 认（lark-cli 1.0.69 起事件里只有 `reply_to`，没有 `parent_id`，`quoted_id`）；`merge` 把消息别名改到目标路再走 `app.handle_event`，`Butler.supplement` 带 `target`（目标任务）；目标在等回答时是回答，走原来的续接；`new` 按 `pick_slot` 选槽、`keep_track_topic`、改别名后入队。
- **送达被拒**：`SteerRefused`（任务刚结束、后端不收补充）或目标已结束，`supplement` 调 `fresh()` 把消息改成新开一件（返回新槽的上下文，`BridgeApp.handle_event` 用它入队）；`SteerUncertain` 照旧说一句、不重发。
- **选槽**（`pick_slot`）：槽 0 空闲就用槽 0，否则编号最小的空闲槽 1..4；在途（排队、开始、在跑）任务数到 `COVEY_BUTLER_TRACKS_MAX`，或没有空闲槽，就选在途最少的槽（并列取编号小的），排在它后面（同话题先进先出，调度器不改），消息记一次 `Tracks.noted("queued", chat, message, ctx=, app=)`。等回答的槽、有 `waiting_user` 记录的槽算占着，不分给别的路；空闲槽开始前清掉上一次遗留的会话指针。
- **这些事的一轮**（槽 ≥1，`butler_turn` → `track_start`）：会话指针为空时分叉槽 0 的会话（`fork_session`，槽 0 有一轮在跑且已记下 `prompt_uuid` 时加 `fork_at`＝`rewind_point(...)["keep"]`，没记就整个分叉），消息是续接式的（`resume_message`）加管家规则、**完整**快照（`take_snapshot(save=False)`，不动槽 0 的 `main-snapshot.json`）和「并行说明」（另外几件事此刻在做什么，每件一行，读任务记录，不依赖 Bridge 的登记表）。分叉不了（槽 0 没有会话、工作区不同、后端不支持、`keep` 为空、`rewind_point` 失败）退回新会话＋槽 0 最近的 `history.jsonl`（`first_turn_message`）。不做压缩检查，也不停空闲压缩；`compact_butler` 对槽 ≥1 直接返回。
- **一轮结束**：`process_bridge_reply_job` 在 `_run_turn` 之后调 `release_pointer`：槽 ≥1 且状态不是 `waiting_user` 就清会话指针（会话文件留着）；等回答的那件事指针保留，回答的续接轮接着用。
- **开了又关**：槽话题留在盘上，这时它们是普通话题（`traits` 给 TOPIC），回答、引用老卡都按 `topic_id` 找到会话指针继续；关着时槽 ≥1 不是管家，下次再开时空闲槽的遗留指针由 `enter` 清掉。

- **表情**（`app/butler_reactions.py`，只给主人自己的消息、话题挂在提问下面时的管家一轮，`Tracks.noted` 是调用点；失败只记日志，都在锁外）：排在忙槽后面的消息挂 **排队中**（`OneSecond`）而不挂 OnIt（`start` 见 `HELD` 就跳过）；这一轮在任务进程里开跑时（`worker.process_bridge_reply_job` 里的 `begin`）先摘排队中、再挂 OnIt。进程之间不传状态：`begin` 用 `Messenger.reaction_ids` 查机器人自己挂的排队中，查不到就什么也不做（没排过队的消息 OnIt 已在入队时挂了）；排队时被取消的任务由 `cancel_reply_job_from_card` 摘掉。并进成功的消息挂 **并进**（`THUMBSUP`），一直留着；主人引用它说「单独答」（规则 3）时摘掉，新开的那件事照常 OnIt → 结束表情。`Get` 已表示“交给话题”，不用。表情代号用接口试过：飞书接受 `OneSecond`、`THUMBSUP`，不接受 `WAIT`。开关关着时一个都不调。
- **停止的回复**：`stop` 把家族里其他还在做（不在等回答）的件数 k 传给 `cancel_reply_job_from_card(others=k)`，k>0 时文字回复末尾加“还有 k 件事在做。”。已经有任务卡的任务停下时没有文字回复：k 记在任务上（`stop_others`），停下后卡上的结尾文字（`worker._others_note`）末尾加同一句。k 随点击的 `action_value.others` 带到任务进程。任务卡的停止按钮和派发卡的停止也算同一个数：点击到达 Bridge 时（`card_routing.count_others`）按 `butler_tracks.stop_others` 算（只算家族的任务，或派发卡；多路并行和管家模式没开时为 0；和停止词共用 `others_going`），写进 `action_value.others`，Bridge 自己停和转给任务进程都带着。其他话题任务卡上的停止不变。
- **停止后不再发答案**：开关开着、属于家族的任务被主人停止（`cancel_requested_by` 有值）时，Claude 因“只剩后台任务在跑”而保留的最后一条完整回复（`backends/claude/runtime.py` 的 `keep_last_reply`）不再发出，这一轮按已停止收尾（`app/turn_settle.py`，`core/butler_tracks.py` 的 `owner_stop_drops_reply`）。开关关着、或普通话题的停止，仍然发出那条回复。Claude 收到 SIGTERM 时自己结束它的后台命令（它们不在 Claude 的进程组里）。


**记忆合并**（`core/track_ledger.py`、`app/track_digest.py`）：路是槽 0 会话的分叉，它答了什么不在管家自己的会话里，所以交给管家的下一轮。

- **账本**：槽 ≥1 的一轮结束、结果送达之后，`TurnDelivery.deliver` 末尾（`relay` 之后）调 `ledger` 回调（`track_digest.record`），写 `main-thread/tracks/<任务键>.json`：`key`、`chat_id`、`track`（槽号）、`owner_message`（主人消息号）、`request`（≤200 字）、`answer`（≤600 字，只取已送达、已授权的回复正文，没送达就是空）、`status`（`done`/`failed`/`cancelled`）、`ended_at`、`session_id`、`opened`（这一轮开的话题的 T 号）、`digested_by`（读过它的管家那一轮的任务键，起初为空）。等回答（`waiting_user`）和中断的一轮不写，回答后的续接轮写；槽 0 的不写（答案已在它自己的会话里）。写失败只记日志。
- **交给管家**：槽 0 的一轮在快照之后加一块“[另外几件事这段时间答了什么]”：12 小时内、最多 8 条（更早的记一行“还有 N 条没列”），每条“完成时刻（失败、已停止注明）请求「…」→ 答案前 300 字 → T 号”。块做好后用记录锁下的 compare-and-set 写 `digested_by=<这一轮的任务键>`；下一轮看到 `digested_by` 指向的那一轮没有正常结束（不在 `GOOD_ENDS` 里，同 `take_snapshot` 的 `settled`）时再给一次。
- **交给新开的那件事**：新开的事的第一轮在“此刻另外几件事在做”后面加同一块，不标记已读（管家自己还没读到）；分叉的会话已读过的（`digested_by` 那一轮正常结束了）不重复。
- **压缩**：摘要只在槽 0 做（槽 ≥1 不压缩）；`COMPACT_INSTRUCTIONS` 不变。这些事的内容只有被管家一轮读进之后才进它的记忆，压缩把它一并压进摘要；压缩后的那一轮照常拿到还没给过的账本条目（`tests/test_track_ledger.py`）。
- **维护**：账本保留 7 天（`Temporary` 规则的 `main-thread/tracks`）；这些事用过的 Claude 会话文件按账本里的 `session_id`，那一轮结束满 3 天且 3 天没改没读的删（`TrackSessions`，只动 `CLAUDE_CONFIG_DIR`（默认 `~/.claude`）下 `projects/*/<session_id>.jsonl`）。
- **开关**：`COVEY_BUTLER_TRACKS=0` 时不写账本、不加这一块。

管家的 Harness 是设置里的 `harness`，只用 `bridge_tools` 为真的 Harness（Claude Code、本机上的 Codex、Antigravity 和 DeepSeek Harness，见 [butler.md 换 Harness](butler.md#换-harness)）。空时由 `app/butler_harness.py` 跟着启用了的选：Claude Code 能用就是它（`core/butler.py` 的 `HARNESS`，和以前一样），否则是启用了的、能接工具的第一个（先 Codex、再 Antigravity，`app/butler_harness.py` 的 `FALLBACKS`，然后按 `core/roster.py` 的顺序）；设置卡和网页画的是这个（`drawn`），不写回文件。专项负责人话题建时先看管家这次点名的、再看设置的 `lead`，都没有才照它（`app/lead_harness.py` 的 `binding`；`unwritten_topic`：管家还没写 `topic.json` 时），`app/project_loop.py` 的 `lead_app` 先看负责人自己的 `harness_id`；已有负责人的换配置走专项卡（`app/harness_switch.py` 的 `lead_context`）和网页（`app/web_switch.py`），重绑同话题（[专项 · 负责人用哪个 Harness](projects.md#负责人用哪个-harness)）。Claude Code 没调通的新装由 `./install` 写成调通的 Codex 或 Antigravity（`core/butler_settings.py` 的 `preset_harness`），专项开关照写。选的那个没启用或接不了工具时，私聊消息照旧开话题，Bridge 启动日志写一行 `main thread is on but its Harness <名字> is not enabled or cannot take the Bridge tools`；没选也没有能用的时写 `main thread is on but no enabled Harness can take the Bridge tools`。

## 专项

专项是管家交给一个隐藏的负责人话题、由它开话题并跟进、要分成几个话题跨几天做完的事（使用者怎么看见见 [projects.md](projects.md)）。底座、负责人的运行、专项卡、三个入口（派发卡转成专项、管家直接开、不要专项降级）、问题分流、续轮与护栏、整理专项、专项记忆与资料库都已落地，下面按块写。`COVEY_PROJECTS` 默认开；设成 `0` 时下面的谓词全为假、放置表和看板和没有专项时一样，也不建任何记录。

- **记录** `<state>/projects/<id>.json`（`core/projects.py`，只用标准库，MCP 服务会导入）：`id`（6 到 32 位 `0-9a-z`）、`chat_id`、`name`、`goal`、`state`、`lead_topic_id`、`threads`（专项的话题 id）、`origin`、`steps`（升级逐步记账，重做时跳过已做的）、`card`（专项卡的 `card_id`、`message_id`、`sequence`）、`created_at`、`updated_at`。给第 3 期留的（旧记录里的 `direction` 没人读也不画，新记录不再写）`leader`、`claims`、`agreements`、`place` 只写不读。读写在 `resource_lock("record", …)` 下，锁内不发网络请求（规则 1）。
- **状态** 只按 `PROJECT_TRANSITIONS` 前进（规则 4）：`active` → `paused`、`done`、`cancelled`；`paused` → `active`、`done`、`cancelled`；`done` → `active`（只在主人又在专项里说话时，见下文“收尾与重开”）；`cancelled` 是终态。`update` 改状态前先查表，不合法抛 `ValueError`、什么都不写。
- **角色** 写在话题自己的 `topic.json` 里：`project: {id, role}`，`role` 是 `lead` 或 `thread`（盘上的值不改，代码里叫 `MEMBER`；`core/topics.py` 的 `set_topic_project`、`clear_topic_project`、`topic_project`）。读 `project.role` 一次 `stat`，文件变了才重读（`core/projects.py` 的 `topic_project`，写入换 inode，所以缓存立刻失效）。
  - **负责人**（`lead`）：话题 id 是 `lead_topic_id(chat, pid)`（`sha256("project-lead\0<pid>\0<chat>")` 取 32 位十六进制，不和管家、同时在做的事的 id 相撞，也不是任何消息的别名）；`is_lead_topic` 还核对这个 id，别的会话里抄来的 `topic.json` 不算。它属于管家家族：`is_butler_topic` 认它，`is_butler_root` 不认；`Traits` 是 `LEAD`（管家关着时 `LEAD_OFF`），放置角色 `lead`（回复只在主人触发的轮里发到顶层，不回带，不放问题卡副本，不开串）。
  - **专项的话题**（`thread`，代码里 `MEMBER`）：还是普通话题（`Traits` 是 `TOPIC`），放置角色 `project_member`：答案留在话题串，结论和停止平常不回带到顶层（投负责人收件箱），只有到主人的问题才在顶层放副本；负责人在主人在场的一轮里交出去的那一轮、主人在话题串以外引用它的卡追问的那一轮（`top_message`）例外，结论和停止照管家派出的话题回带（`relay_targets.awaited_target`，见 [projects.md 主人在等的结果](projects.md#主人在等的结果)）。
  - `core/placement.py` 的 `plan(state_dir, of)` 读 `topic.json` 的角色，所以转成专项时在跑的那一轮立即生效；传 `Traits` 行进去的人看不到专项的话题的角色（它不带话题 id），要传任务、记录或 `{"topic_id", "chat_id"}`。
- **看板** `core/board_read.py` 的 `groups` 收一个 `cells`（`read` 带，来自 `core/projects.py` 的 `cells`，没取消的专项的话题 → 负责人 id）：一个专项一格，格的键是负责人话题 id。同一格里最新的问题在等；在做的取先在跑再排队、再取最新（任务号是 Bridge 的全局计数，跨话题也能比）；结果取各话题最新一条里“主人没看过的最后结束的那条”，没有就取最后结束的。所以一条没看过的旧结果不会被一条看过的新结果盖掉，但一格只钉一个。手上的事、早晚汇总直接读任务记录，现在还不并格。
- **负责人的运行**（`app/project_lead.py`、`app/project_tools.py`，只在 `is_lead` 的话题里）：
  - **一轮** `butler_turn` 对负责人走 `lead_turn`：消息头是 `lead_rules`（身份、工具、风格、留给第 3 期专员的做法层 `PRACTICE`，分层拼接）加**状态包**（目标与完成标准、话题清单：状态、一句话、交付物，专项记忆（含近 30 天主人删掉的，别记回去）、资料库索引（`COVEY_PROJECT_MEMORY` 没关才有）、风格、自上次唤醒以来的收件箱、近 10 条决定），再是这一轮自己的消息。新会话给全部，续接的会话只给会变的部分（话题清单、风格、收件箱、决定；资料库只给上一轮开始以来新增的，至多 5 行；开着协作时检查表里满足的折成一行）。
  - **会话只是缓存**：不做摘要压缩（`compact_butler` 跳过负责人）。上一轮结束时对话超 30 万（`context_tokens`，下一个请求从这么长开始；不看 `input_tokens`，那是这一轮每个请求各读一遍对话加起来的，一轮几十个请求就远超，负责人会每轮新开会话、吃不上缓存；旧记录没有 `context_tokens` 时才用它）、话题空闲超 24 小时、或换了模型，`worker` 的 `_start_turn` 经 `settle_session` 清掉会话指针，这一轮新开会话、状态包给全。
  - **工具**：负责人只读（`core/harness.py` 的 `LEAD`，各 Harness 自己落实）：Claude Code 只给内置的 `Read`、`Grep`、`Glob`（`backends/claude/argv.py` 变成 `--tools`；MCP 工具不属内置工具，不受影响），Codex 跑只读轮（`backends/codex/bridge.py`，Bridge 工具服务设成自动批准），Antigravity 带只拒写文件、跑命令和读网址的规则（`backends/agy/bridge.py` 的 `READ_ONLY_SETTINGS`），DeepSeek Harness 用 read-only 权限预设（`backends/dsh/bridge.py` 的 `LEAD_ARGS`）；再加 Bridge 工具 `topics_list`、`topic_read`、`topics_search`、`repos_list`、`topics_switch`（只发批量换配置卡，主人确认才换，只能换本专项的话题）、`memory_search`（只查本专项和本专项话题的记忆，按非主人的一轮读：不读全局的、别的专项的和普通话题的，点名别的专项或不属本专项的话题直接拒绝；`app/butler_memory.py` 的 `memory_search`、`app/butler_knowledge.py` 的 `lead_scope`；这两个在 Antigravity 上按需加载，不占预载；`core/butler_tool_specs.py` 的 `LEAD_SHARED`）和 `project_*`。MCP 配置里有 `COVEY_PROJECT_LEAD`（专项 id）时服务只列这些，管家的列表一个字不变。`project_topic_start`、`project_topic_send` 走 `butler_topics` 的 `topic_start`、`topic_send`（负责人的任务调用时开出来的话题带 `project: {role: thread}`，写在绑定里，第一轮排队之前；同时在做的话题不设上限，风格里也没有“派活”档，旧记录的 `style.spawn` 照读不用）；`project_topic_send` 发给还不在任何专项的普通话题时，先用 `app/projects.py` 的 `join_topic`（`by: lead`）把它收进本专项再加话（`app/project_tools.py` 的 `take_in`；别的专项的话题、负责人话题、管家、代答话题和主人移出过专项的 `never_auto` 话题拒绝），负责人规则要它派活前先看专项里和私聊里已有的话题，能追加就追加；`project_topic_stop` 停专项里的一个话题（主人对管家说“停掉 T1”，管家用 `project_send` 交给负责人）：先用 `project_cards.stop_tasks` 停它排队和在跑的任务（转给任务进程，同停止专项），再用 `binding.retire_open_questions` 收掉它等主人回答的问题（`cancel_reason: lead_stop`，卡上写“负责人按你的意思停了这个话题”）并把等回答的任务记录记为 `cancelled`；话题仍在专项的话题清单里、算结束，只能停本专项的话题，专项已结束时拒绝；`project_note`（专项卡上一句，`report` 标要告诉主人的，按风格过滤，叫醒的轮由 Bridge 发笔记原文）、`project_ask`（要主人定的事：写进负责人这一轮的检查点，轮末出问题卡，问题记录带 `origin: project_ask`，`kind` 为 `deploy` 的题主人同意后记部署授权，见[负责人问主人与外发三通道](projects.md#负责人问主人与外发三通道)和[部署攒批与部署授权](projects.md#部署攒批与部署授权)；`project_topic_start`、`project_topic_send` 带 `approval` 时 Bridge 核对后写进交接说明开头）、`project_goal`（负责人写给主人看的目标和完成标准，记在 `lead_goal`，网页专项标题下显示它；记录的 `goal` 仍是开专项时的话，状态包里没写过就提醒一句）、`project_remember`、`project_style`、`project_finish`（还有话题在做时拒绝）写记录的 `notes`、`decisions`、`style`，`project_remember`、`project_style` 的记忆条目写进 `projects/<id>/memory.json`（见下面“记忆”）；`project_answer`、`project_escalate`（问题分流，`COVEY_PROJECT_TRIAGE` 默认开）只在开关开着时列给负责人，关着时直接调也只回答“问题分流没有开”（见下面的问题分流）。`project_answer` 要写 `evidence`，开不开协作都由 `app/triage.py` 的 `grounds` 逐条核对；开协作（`COVEY_COLLAB=1`）时多一个 `project_checklist`（目标检查表，纯函数在 `core/project_checklist.py`），`project_finish` 多第三道门 `checklist_gate`（还有没满足的条目或没处理的协作缺口就拒）；同样只在开着协作时，`project_goal` 多可选的 `background`，`project_checklist` 多 `actions`，`project_topic_start`、`project_topic_send` 多可选的 `action`、`item`（项目简报，纯函数在 `core/project_actions.py`：动作的增改校验、不存的状态、话题挂到动作和条目上）；使用者看到的见[负责人对照目标跟进](projects.md#负责人对照目标跟进)和[分流的依据](projects.md#分流的依据)。
  - **记忆** `projects/<id>/memory.json`（`core/project_memory.py`，只用标准库；使用者怎么看见见 [projects.md 专项记忆与资料库](projects.md#专项记忆与资料库)）：条目 `{id, text, kind, source, at, deleted}`，格式和旧版一样；`kind` 是 `decision`、`constraint`、`contact`、`fact`、`style` 之一（负责人工具旧名 `practice`、`pitfall` 记成 `constraint`），`source` 是写进来的那条路定的（`lead`、`owner`、`owner_answer`、`owner_fix`，模型说什么都不算）。
    - **限额与删除顺序**：一条至多 200 字（`TEXT`），问答的一条至多 600 字（`ANSWER_TEXT`）；活条目至多 120 条（`MAX_ENTRIES`）、文件至多 64 KB（`MAX_BYTES`），墓碑至多 20 个、30 天后清掉。超了按 `VICTIMS` 的顺序删最旧的：墓碑、问答（`owner_answer`、`owner_fix`）、负责人记的事实和联系人、负责人记的其他条目、主人自己的话，风格行（主人最后一次定的汇报风格）从不删。删活条目不是丢：连同它的旁路字段写进 `memory-archive.json`（留最近 300 条，`ARCHIVE_ENTRIES`），这个文件读不出来时这次写入失败、记忆保持原样。用户的“删”写墓碑（`deleted` 是删的时间，文字留着），状态包列出近 30 天的墓碑，负责人的 `project_remember` 也拒绝记回去。
    - **旁路与按题留底**：旧版本不认识的东西写在旁边的 `memory-meta.json`（条目 id → `META_KEYS`：出处 `origin`、`asserted_by`、`recorded_by`、层 `layer`、`applies`、`valid`、`superseded_by`、`fixes`），旧版本改写 `memory.json` 丢不掉它们；旧版本不读也不写 meta、archive 和 `answers/`。你答问题卡，每道答了的题记一条决定（`kind=decision`、`source=owner_answer`，条目 id 是问题记录的标识加题号，同一份回答只记一次、删掉的不再记回来），整份问题记录原样存进 `answers/<token>.json`（`answer_record`），条目文字截短了也能查回原文。
    - **一个读口**：专项的话题这一轮、负责人的状态包和网页都经 `core/memory.py` 的 `project_view` 读，条目、分段和取舍一样，带一个 `version`（`core/project_memory.py` 的 `version`，增删一条就变，三处同值）。每轮带三段，各有各的字数：长期约定 1000、当前决定 1000、待核对参考 800（`PART_LIMITS`），一条整条带或不带，哪段用不完，剩下的按 `SPARE_ORDER`（长期约定、当前决定、待核对参考；待核对参考借到后总用量封顶 `REFERENCE_CAP` 1000 字）让给放不下的，一段自己用的那部分不让出去；历史记录不每轮带。
    - **纠正**：`app/project_context.py` 的 `fix_memory`（`scope` 改适用范围、`once` 只限当次、`confirm` 确认长期；只认主人，调用方先核对点击或网页操作是主人的），每次纠正记在条目的 `fixes` 里。
    - **兼容与写入**：旧版把条目写在专项记录的 `memory` 字段里，读的时候两处合并，下一次写入把它们搬进 `memory.json`、记录里不再有这个字段。所有写都在专项记录同一把 `record` 锁下（`projects.update`），所以负责人的工具、卡上的“删”和 Bridge 自己记的不会互相覆盖；读不拿锁（每个文件整份替换）。写之前读不出来（权限、IO 错误）就让这次写失败，文件解析不了先改名留底 `<名字>.bad-<时间>` 再按空的继续。专项卡、状态包、`project_remember`、`project_style` 都经这个模块。
  - **资料库与带上记忆**（开关 `COVEY_PROJECT_MEMORY`，默认开，`0` 关，要专项开着；细节在[专项记忆与资料库](projects.md#专项记忆与资料库)）：`core/project_library.py`（只用标准库）管 `projects/<id>/library/` 和 `library.json`；`app/project_context.py` 是 Bridge 一侧的接线：专项的话题开工的 `block`（经 `app/prompts.py` 的 `project_block`，新会话进 `bridge_rules`、续接的进 `_with_rules`）、问题卡回答记决定的 `remember_answer`、主人文件进资料库的 `owner_files`、话题每轮产物和交付进索引的 `turn_to_library`、成员每轮回复全文进索引的 `result_to_library`（都由 `project_loop.after_runner` 在任务进程结束后调，任务进程自己不写专项的东西；全文由 `app/artifacts.py` 的 `write_result_file` 写进 `result-files/<任务键>.md`，已有的不覆写，只留底的带 `result_file_kept`、不算产物；来源话题开了脱敏的条目带 `redacted`，`index_lines(redacted_reader=)` 只给开了脱敏的读者列）。开关关了时这些都什么也不做、什么也不读。
  - **收件箱** `projects/<id>/inbox/<序号>.json`：`{id, kind, thread, label, text, at, taken_by}`（`label` 是写入时话题的 T 号，有才写；开工回执另有 `message_id`），`add_receipt` 写、`take_receipts` 读并标上这一轮（上一个领它的轮没有好好结束的，下一轮重新给；主人自己停掉的那一轮领走的不退回，不会再为它们叫醒，下一轮状态包写一行“上一轮被主人停了，它拿到的 N 条回执没读完”和前 5 条，`gives_back`、`stopped_block`）。一轮最多给 20 条：先给要先看的（`FIRST_KINDS`：协作缺口、扣住的问题、主人的选择、开工、主人在话题里说的话），其余新的优先，显示仍按时间；没给到的写“另有 N 条较早的回执这一轮没给，下一轮接着给”。谁来写见下面的“续轮与护栏”和问题分流。
  - **一轮的结语** 负责人有任务卡时（管家关着的 `topics` 布局，或旧版本发过的卡），卡和列表预览说它做了什么（`butler_reply.outcome_words`，`lead=True`）：开了或交给哪个话题，否则记了哪句进展或收尾了专项，否则回复的开头。话题区里负责人不出任务卡（下面的“不出任务卡”）。
- **专项卡**（`cards/project_card.py`、`app/project_cards.py`，细节见 [projects.md](projects.md#专项卡)）：`record["card"]` 是 `{card_id, message_id, sequence, created_at, generation, digest, wrote_at, closed, old, pending_card_id}`，只由 `ProjectCards` 经 `projects.update_card` 写（单写手，规则 3；sequence 存进这一个字段，不整条记录覆盖，免得盖掉负责人和升级同时写的别的字段）。`board` 钉专项卡时读这条记录的 `message_id`（`app/pins.py` 的 `aimed`/`project_aimed`）；格的名字（手上的事、早晚汇总）在 `core/topics.py` 的 `label_title` 里换成专项名。
- **转成专项、直接开、降级**（`app/projects.py`、`app/project_clicks.py` 的 `downgrade`）：
  - **转成专项** 开关 `COVEY_PROJECT_UPGRADE`：`off`（什么都不做）、`butler`（不设就是它：管家 `topic_send` 带 `project`，由管家按话的意思判断；不再匹配主人话里的关键词，派发记录的 `upgrade_hint.long_word` 不再写也不再读，旧记录里留着不管；旧值 `word` 照认，意思同 `butler`）、`auto`（再加该话题的轮数 ≥3：`rounds_for_topic` 取该话题派发记录数和主人亲自发起的轮——任务的消息 id 以 `om_` 开头——两者的较大者，不读旧记录的 `rounds`）。触发点是 `butler_topics.dispatch_card` 在 `dispatch_cards.created` 返回后调 `env.projects.consider(token)`（`ButlerEnv.projects`；没放进 `dispatch_cards._created` 是因为那样 `dispatch_cards`、`butler_topics`、`project_cards`、`projects` 会互相导入）。静默进行，卡上有“不要专项”。活专项（`active`）已有 `MAX_ACTIVE`（50）个时先腾位置（`project_loop.make_room`，见下面的“满额腾位置”），腾不出来才不升级。
  - **七步**（`Projects.upgrade` → `advance`，每步成功后在记录的 `steps` 里记一笔，重做时跳过；每一步在记账丢了的情况下重做也无害）：① 记录，id 由派发卡 token 算出（`project_id`，所以同一张卡永远是同一个专项），名字取话题标题；② 负责人话题：topic.json 抄管家的 `topic.json`（管家还没写时照它第一条消息会写的，`app/butler_harness.py` 的 `unwritten_topic`，没有能接 Bridge 工具的 Harness 时这一步失败、下次重做），Harness、模型和预算按 `app/lead_harness.py` 的 `binding`（管家点名的、设置的 `lead`、管家的），原话题的工作区，锁工作区，写 `project: {id, role: lead}`；③ 在原话题的 `topic_lock` 里 `set_topic_project(thread)`，记进 `threads`；④ 在 `resource_lock("card", card_id)` 下先把派发记录转 `settled`（`DISPATCH_TRANSITIONS`，`settled_by: upgrade`），再 `adopt_card`，sequence 从派发记录接着走（记录里已经是这条卡就不再 adopt，免得 sequence 退回去）；⑤ 同话题其余活着的派发卡写成“已并入专项 ›”并转 `settled`；⑥ `route_message`：卡和并入那几行的消息指到负责人话题；卡自己的别名也整个改归负责人（`move_topic_message`）：卡上按钮的值写的是负责人话题，点击按别名找话题，对不上就当别的话题的卡不理（`card_routing.py`，accept7 时升级卡的按钮因此全都点不动）；⑦ 写 `start` 回执（`add_receipt`，“接手一个已在进行的事”加话题、主人要的和现在的状况；先看收件箱里有没有，不写两次）。唤醒不在这里：`start` 回执由续轮的 `ProjectLoop` 去排。
  - **崩了怎么接**：`Projects.threads()` 给一个 30 秒一看的后台线程（`resume`），找 `steps` 没记全、60 秒没写过的记录再 `advance`，一直失败的退避到最长 10 分钟。进程内 `_busy` 防止两处同时跑同一个专项。`DispatchCards.write` 在拿到卡锁后重读记录，已经 `settled`（升级或并入了）就不写，免得盖掉专项卡。
  - **直接开** 管家 `topic_start` 带 `project: true`（专项开着即可，不看 `COVEY_PROJECT_UPGRADE`）→ `Projects.start`：记录（id 由请求算出，一次请求一个专项）、负责人话题（工作区取 `topic_start` 的 `workspace`）、`project_cards.created` 发卡（顶上“你问”块，读记录的 `ask`）、`start` 回执（主人的请求原文）；四步同样记在 `steps` 里，卡没发出去也由 `resume` 补。不开话题，由负责人去开。`topic_start` 的 `topics`（主人说要收进来的已有话题，T 号或话题 id）由 `wanted_topics` 在建记录前逐个查：找不到、是管家或代答话题、是负责人或已在别的专项里、超过 `MAX_JOINED` 个的记进 `origin.refused`，其余记进 `origin.topics`，这时步骤是 `JOIN_STEPS`（卡之后多一步 `members`，同整理专项建专项：只写话题的 `topic.json` 和记录的 `threads`，在跑的任务不停，等主人回答的问题不收；分流只管之后出的问题）；工具回复和 `start` 回执都逐个写挂上了哪些、没挂上的和原因。没有 `topics` 的仍是原来四步。
  - **管家工具** `projects_list`、`project_send`（`app/butler_projects.py` 的 `TOOLS`，只在专项开着时列，负责人用不了）；`topic_send` 遇到专项的话题回一句改用 `project_send`（负责人自己给话题加话走 `project_topic_send`，不受限）。`project_send` 的 `topics`（T 号或话题 id）在交给负责人之前用 `wanted_topics` 查、`join_topic`（`by: butler`）收进专项（`take_topics`），回复逐个写收进的和没收进的。
  - **话题转交管家**（`app/butler_asks.py`，专项开着且管家开着）：主人自己的一轮里话题在检查点写 `ask_butler`，一轮结束后 Bridge 在私聊顶层发“↪ T12 转给管家”，再在这条消息上给管家排一轮（来源 `handover`，见[话题转交管家](#话题转交管家)）；谁能交、怎么去重、转不了怎么说见[在话题里说开专项](projects.md#在话题里说开专项)。
  - **降级** 点“不要专项”→ `downgrade`：记录转 `cancelled`（`state_reason: downgraded`，之后 `ProjectCards` 把卡缩成“⏹ 专项已降为普通话题”）；停掉负责人排队和在跑的任务；专项的每个话题（含原话题）`clear_topic_project`（在 `topic_lock` 里，只清自己专项的）；卡消息和并入的几行的别名 `route_message` 指回原话题（直接开的指向第一个话题，没有话题就不指），卡自己的别名也改回去（`move_topic_message`）。每一步重做无害，所以一个降了一半的专项再点一次会接着降完。话题的任务不停，它们是普通话题了。
- **问题分流**（`app/triage.py`、`core/triage.py`、`core/project_gates.py`；开关 `COVEY_PROJECT_TRIAGE`，见[配置](configuration.md)；做法、硬闸词表、实验 E2/E4 在[专项的问题分流](projects.md#专项的问题分流)）：专项的话题的选择题先交给负责人。`Questions.triage.route` 在 `NativeQuestions.show`（活问题）和 `CheckpointQuestions.deliver`（检查点、暂停的问题）发卡前调；扣住 = 问题记录的 `triage.state == held`，`status`、`display_pending`、任务记录的 `question_delivery_ok` 都按“没发卡但也不是没投出去”写，`recover_question_delivery` 跳过扣住的。回执和超时归 Bridge 的 `sweep`（任务进程只写自己这道题的记录，规则 8），`project_answer` 走 `submit_answer`，`project_escalate` 和超时走原来的发卡口子。状态只按 `TRIAGE_TRANSITIONS`（`held → answered_by_lead | escalated`）走（规则 4）；记录锁内不发网络请求（规则 1）。
- **续轮与护栏**（`app/project_loop.py`；开关 `COVEY_PROJECT_AUTOWAKE`、`COVEY_PROJECT_BUDGET`，见[配置](configuration.md)）：
  - **谁唤醒**：`ProjectLoop`（功能对象，专项开着才起后台线程，启动后等 20 秒让 Bridge 自己先恢复被打断的任务，之后每 10 秒看一遍；每一遍在一个工作线程里跑，60 秒没有进展（看完一道扣住的问题、看完一个专项都算）就记一行 `project loop stuck … at: project:<id>`、丢下它，下一遍换新线程，在旧线程结束前不再看它卡住的那个专项；丢下的那一遍回来后什么都不写（不唤醒、不停、不收起）；还卡着的旧线程到 3 个先等，最多 300 秒；一遍超过 10 秒记 `project loop slow`，5 分钟至多一行。线程只有一条，卡在网络盘上一次，唤醒和 3 分钟到期转给你就一起停到重启。收件箱里已被好好结束的一轮领走的回执不会再变，每个进程只读一次：几个专项的收件箱各有上百条回执，每一遍都重读会开上千个文件）对每个 `active` 专项：有没人读过的回执（收件箱里没被领、或领它的那一轮没好好结束，即 `take_receipts` 的判定）、负责人没有排队、在跑或等主人的任务时，入队 1 个唤醒轮——和负责人话题里的任何消息一样走 `enqueue_chat_job`，消息 id `lead-<专项 id>-<n>`（`n` 数在记录的 `wakes` 里；先记数再入队，丢一个下次再找，不会重复），消息内容只有 Bridge 的一句说明，状态包和收件箱由 `lead_turn` 自己加。`start` 回执总是唤醒（只要专项开着）；其余都要 `COVEY_PROJECT_AUTOWAKE` 开着，设成 `0` 时它们留在收件箱，等负责人下一次被主人叫起时一起读，专项没有别的在排队、在跑时照常按空闲天数收起（`look` 里的 `resting`，不然它一直“在做”、永不收起）；负责人规则（`app/project_lead.py` 的 `NOT_WOKEN`）和 `project_topic_start` 的说明（`core/butler_tool_specs.py` 的 `lead_tools`，Bridge 把开关交给负责人的 MCP 服务）也照实说话题做完不会叫醒它。重启后同一遍查找就把盘上的未领回执重新入队。开协作时普通回执合批：最新一条到了 `COVEY_PROJECT_BATCH_SECONDS`（默认 20，`0` 不攒）秒没有新的才叫醒，最早一条最多等 4 倍（`ProjectLoop.look` 里的 `settling`）；`start`、主人在停住卡上的选择马上叫醒；扣住的问题、`collab_gap` 短合批（最新一条 5 秒没有新的再叫醒，最早一条最多 20 秒，协作关着也是），`collab_answered` 不叫醒、也不参与计时。
  - **回执谁写**：`thread_end`（专项的话题的一轮结束：任务号、状态、`turn_summary` 的摘要和全文路径）和 token 累加都由 **Bridge** 在任务进程退出后写（`butler_relay.after_runner` 的第一步调 `project_loop.after_runner`，所有任务结束的路径都经过它；规则 8 让任务进程只改本任务的卡和记录，写别的专项的收件箱和记录不算在内，所以放 Bridge 这边，旧任务进程也一样有回执）；`owner_said`（主人在专项的话题里直接说话）在 Bridge 收消息处（`app/app.py` `handle_event`）写；`stalled` 由循环写。`question` 由问题分流（`app/triage.py` 的 `sweep`）写，问题转给主人时由 `triage.escalate` 标成已处理（回执的 `done`，`project_lead.settle_receipt`：不再叫醒负责人，还没拿到它的负责人轮次也不再拿）。回执一律经 `add_receipt`。协作的咨询轮不写 `thread_end`、不进资料库；它的 token 由 `consult_tokens` 记进汇总方现在所在的专项，汇总方不在专项时记进被问的协助方所在的专项，按任务键只记一次（`token_jobs`）。
  - **主人触发的轮**：负责人话题里 message id 不以 `lead-` 开头的轮（主人引用专项卡回复、网页上对负责人说、管家 `project_send` 转交、回答的续接）。它入队时（`submission.enqueue_chat_job` → `project_loop.owner_turn`）把 `wakes` 的连续计数和最近一小时的记录清零、清掉停滞标记，并把因护栏（`tokens`、`wakes`、`stalled`）或主人点了停止专项而停住的专项恢复成 `active`；`tokens` 停住的从已用的数目重新开一份预算（`token_floor`），否则下一遍就又停了。主人在专项的话题里说话（`owner_said`）也清连续计数。
  - **Bridge 自造的消息 id**：唤醒轮的 `lead-<pid>-<n>` 和管家转交用的别名 `project-lead-<id>`（`projects.own_message`）在飞书里没有：回复不引用它（`core/topics.py` 的 `context_for_topic` 把 `reply_to` 留空，答案是聊天区的新消息），答案顶上也不画“你问”块（`app/answer_cards.py` 的 `asked`）。负责人哪一轮都不发任务卡（下一条）。管家 `project_send` 转来的那一轮消息 id 是 `main-<token>`，飞书里也没有：答案顶上的“你问”块（和头像行的点击）打开管家那一轮接的主人原话（`app/answer_cards.py` 的 `owner_message` 按 token 读这次请求的记录 `main-thread/opened/<token>.json`，取管家任务的消息），读不到就只有字、没有链接。
  - **不出任务卡**（主人定的）：话题区里负责人的每一轮都是安静轮、都不发任务卡，不论谁触发（唤醒、主人引用专项卡或负责人的回答、网页、管家 `project_send` 转交、回答的续接、重新排队的补充、重试）：放置表 `area` × `lead` 的 `task_card` 是 NONE，`placement.quiet` 对这一格不看消息 id，`submission` 把卡压着（`card_deferred`），`Cards.reveal` 跑过 `CARD_AFTER` 也不发，`TaskControls.requeue` 对旧版本发过卡的轮也不再建卡（`placement.cardless`）。负责人在做什么看专项卡的“负责人”一行；停止专项按话题别名把停止转给没有卡的安静轮。私聊里能看到的只有答案（顶层新消息，说话人“专项「名」· 负责人”）、该报的汇报和没做完的轮的一句话（`app/quiet_delivery.py`）；只有主人自己的飞书消息有表情。管家关着时（`topics` 布局）照旧。
  - **护栏**（全是机械检查，数字由主人定）：专项 `paused` 且 `state_reason` 写 `tokens`、`wakes`、`stalled`，之后不再唤醒，专项卡的“负责人”一行写“停住 · 原因”（`projects.reason_words`）。`COVEY_PROJECT_ASK` 开着（默认）时还发一张停住卡问主人继续、收尾还是停（见[专项停住后问主人](projects.md#专项停住后问主人)）；`COVEY_PROJECT_STALL_SECONDS` 能调小停滞的时间。
    - token：负责人和各话题任务的合计（任务的输入含缓存加输出，子任务算在内）每个任务结束时累加进 `tokens_used`，每个任务只算一次（`token_jobs` 记键），专项卡上写“token N”；只在部署设了 `COVEY_PROJECT_BUDGET`（旧名 `COVEY_PROJECT_TOKENS`）时到了才停，默认没有上限（主人定的：不按 token 总量停整个专项，也不请停话题、不发停住卡）。
    - 唤醒：按“无动作”算（W2-C）：叫醒轮结束时（`after_runner`，和 token 一起只记一次，`idle_turn`）这一轮没有 `acted` 行（开话题、加话、停话题、替答、转交、提问）也没写自己的笔记，就是一次无动作；无人介入连续 6 次无动作，或最近一小时 12 次无动作，停；做了事的那一轮让连续数归零。另留保险：最近一小时叫醒满 60 次（不论有没有动作）也停。主人自己的一轮、他在场的叫醒轮、他在成员话题里说话、停住卡上点继续都把各计数清零；他停掉的叫醒轮不算。旧记录没有 `idle`、`idle_recent`，按 0 读。
    - 限流或后端起不来时不叫醒（`project_loop.limit_wait`，不停专项、不算唤醒次数）：负责人最新一轮因限流、额度用完或模型服务临时故障（`auto_retry.OTHER_CODES`）失败，它的自动重试还没入队（`pending`、`submitting`）时不叫醒，等那次重试（额度命令或 Claude Code 说了恢复时间就在恢复后）：它和负责人的每一轮一样取收件箱，期间来的回执一轮读完；叫醒会作为话题里的新消息把重试取消，再在同一个限额上失败。重试过点 10 分钟（`LIMIT_REST`）还没跑就不再等。没有重试要来（阶梯用完、Harness 不重试、网关额度用完）时，那一轮结束后 10 分钟内不叫醒。负责人的模型用的网关额度被 hold（`core/quota_hold.py` 的 `blocks`）时也不叫醒，直到恢复或到复查时间。负责人最新一轮以下一轮也一样会失败的原因结束（`project_loop.stuck`：安全拒绝、模型不可用、登录失效、账号被拦，或什么都没做成、0 步 0 token 的通用失败）时，和后端起不来一样按 1、3、10 分钟的阶梯等（否则每 10 秒左右叫醒一次，每次都同样失败，直到唤醒次数到上限、整个专项停住）。负责人最新一轮后端没起来（登录检查失败、后端程序不可用、启动出错，记录写“后端初始化失败：…”；或任务进程没起来 `start_failed`，`project_loop.backend_down`）时，按最近连着几轮没起来，在最新一轮结束后 1、3、10 分钟内不叫醒（`DOWN_LADDER`，3 轮以上都是 10 分钟）；到点叫醒的那一轮就是试后端好了没有，第一轮起来的读完全部回执。叫醒轮没起来、而它之前那一轮也没起来（一小时内）时，不往主人那里回“后端暂不可用”、不发恢复卡（`project_loop.quiet_start`，日志记 `backend startup notice held`）：一段起不来只在第一轮告诉主人；主人自己的一轮照常告诉。那一轮结束后才来的开工、主人在停住卡上的选择（`OWN_WAKES`）照样叫醒一次；主人在负责人话题里说话是他自己的一轮，不经这里。开始等和等的东西变了时记一行 `project wake held project=… why=retry|rest|quota|down until=…`。
    - 停滞（要 `COVEY_PROJECT_AUTOWAKE` 开着）：只管**有活没动**。专项 `active`，还有负责人能推动的事（负责人或话题有排队的任务、有没读的回执，`project_loop.movable`），没有任务在跑，距上一次动静超过 30 分钟，写一条 `stalled` 回执（唤醒负责人提醒一次）；再过 30 分钟仍没有动静就停。“动静”是话题任务结束、主人触发的负责人一轮结束、收件箱来了 `stalled` 以外的回执、专项开始；唤醒轮自己不算。任务在跑不算停滞；只在等主人回答的任务或问题不算停滞（主人看得到问题卡，负责人没事可做），这时和没有要做的事一样按空闲算：等满空闲天数也收起（问题卡还在，主人一答就展开）；**没有要做的事不提醒、不停住**，只按下面的空闲算。
    - 空闲收起：没有要做的事（同上）、也没有动静，满 `COVEY_PROJECT_IDLE_DAYS` 天（默认 7，可写小数试用；不是正数用默认）就收起：状态仍是 `paused`，`state_reason` 写 `idle`（`projects.IDLE`，不新增状态，`PROJECT_TRANSITIONS` 不变），记录多一个 `idle: {at, days}`；不弹停住卡、不发问题卡（`ProjectLoop.rest`，不走 `pause`）。专项卡缩成一行“收起 · N 天没有新的事 / 有新的事会自动展开”（`ProjectCards.fold`，不关闭，不受 12 天重发影响）。主人在负责人话题里说话、在专项的话题里说话（`heard`）、管家 `project_send` 转来新的事（都经 `reopen`）时回到 `active`，下一次刷新把整张卡画回来；`idle_reopened_at` 让它在又有动静前不会再次收起。`idle` 算 `RESUMABLE`，不算守卫停住（`GUARD_REASONS`）。
    - 满额腾位置：活专项已有 `MAX_ACTIVE`（50）个时，新开（`Projects.start`）、转成专项（`upgrade`）、整理专项的建（`gather`、`tidy_apply` 的建、`tidy_clicks` 的建）和拆先调 `project_loop.make_room`：在做的专项里同样“没有要做的事、或只在等主人回答”（`resting`，和上面空闲收起同一个判断）、至少 `ROOM_IDLE`（一天）没有动静（`quiet_since`）的，收起最久没动静的那个，`idle` 记 `{at, days: 实际空了几天, by: room}`，其余同空闲收起。一个也收不了时返回拒绝的话，点名最久没动静的三个（`quietest`）。整理专项的提议只看能不能腾（`room_possible`，不改记录）。都只写记录，不发飞书（规则 1）。
    - 收尾与重开：专项只由负责人的 `project_finish` 记成 `done`。整理专项把已有话题收进来建的专项（`origin.kind` 是 `tidy`）在主人还没对它说过话（记录的 `owner_lead_at`：负责人话题里他的一轮或补充、管家 `project_send`、网页、停住卡上的点击，都经 `project_loop.owner_spoke`；他在成员话题里说话 `heard` 只记 `owner_at`，不算）、负责人也没开过或派过话题（`acted` 里的 `start`、`send`；停话题、替答、转交、提问不算）时，`project_finish` 拒绝（`project_tools.finish_gate`，W2-C 收紧），开工回执也写明“收进来的话题做完不等于专项完成”：此前拆出的专项在建好 5 秒后的第一次叫醒里就被收尾了。主人又对 `done` 的专项说话（负责人话题 `owner_turn`、管家 `project_send`、网页说话和重开，都经 `project_loop.owner_spoke`）时回到 `active`（`unfinish`；在专项的话题里说话 `heard` 不重开已收尾的专项）：记录的 `reopened` 记下时间和上次的结论，负责人下一轮的“专项状态”多一行“重开”；`state_job`、`conclusion_message_id` 清掉，专项卡的 `closed`、`digest` 清掉，下一次刷新在原处把整张卡画回来。管家快照列出 14 天内做完的专项（`butler.DONE_SHOWN`），`project_send` 能转给它。负责人的工具在不是 `active` 时的拒绝写清下一步（`project_tools.ended_words`）。`cancelled` 仍是终态。
  - **唤醒轮的回复默认不发，提问出卡、进展发笔记原文**（WAVE12 C4、C5）：唤醒轮和负责人的其他轮一样算安静轮（见上面的“不出任务卡”）：不显示任务卡，没有表情。主人不在场的唤醒轮（`project_loop.woken`：消息 id 是 `lead-…` 或来源是 `wake`，且没有主人的补充进来）做完或停在 `project_ask` 的问题上时，已授权回复保存、记为已送达（`TurnDelivery.silent`：`send` 直接返回），所以重投不重试、恢复卡不把它当未送达；停在问题上的照常出问题卡（`deliver_questions`，出卡失败照常写“问题控件发送失败”并提示重新投递）；没做完的轮（失败、停止、被拒）照旧在顶层说一句，不藏。这一轮用 `project_note` 记了 `report`、且按风格该报的笔记（`project_lead.reportable`；`project_finish` 的收尾也算），由 `project_loop.report_out` 以“专项「名」负责人：”加笔记原文合成一条发到回复本该去的地方（幂等键 `lead-report-<任务键>`；风格 `each_thread`、`verbose` 附一行这一轮做了什么，来自专项记录 `acted` 的 `whats`）；笔记说的是成员结果、而那一轮的结论主人已经或将要从回带收到时（`told_already` 现读成员轮的回带记录：`pending`、`submitting`、`sent`、`uncertain` 都算，`failed` 不算），不再转述。开了话题、加了话、停了话题、答或转交了问题这类动作不再让整段回复外发。主人的话中途作为补充进了这一轮（`app/steering.py` 的 `deliver_steer` → `project_loop.owner_steered`，补充发出前先写 `owner_present`，确定没送出时撤回），这一轮按主人在场算（`project_loop.present`）：回复照常发，派出去的活按主人在场的轮回带，叫醒次数清零。叫醒轮的来源是 `wake`（`ProjectLoop.enqueue` 经 `turn_source.stamp` 盖章，`answers_questions` 为否），开跑时不关负责人还在等主人回答的 `project_ask` 题；负责人停在这种题上时 `project_loop.lead_busy` 不算它忙，成员回执照常叫醒，分流照常交给它（`lead_free`：不忙、没被护栏停、不在限额等待里；接不住的题 0 秒给主人）。
- **维护** `projects/` 里状态是 `done`、`cancelled` 且 180 天没改的记录连同 `projects/<id>/` 一起删（`ProjectRecords`，`remove_projects`），`projects/.locks/` 归 `Locks` 规则。
- **网页工作台同步**（`COVEY_WEB=1`，默认关）：`web-push`（快看）、`web-pass`（全量）、`web-inbox`（取操作，交给各自的车道做）、`web-touch`（刚操作过的话题马上单独推）、`web-reread`（快看读卡住的话题换线程重读，有界）五条受监管线程，把专项、话题（含管家和专项负责人）、一轮（含过程和用量）、问题推给网页，取回网页上的说话（含对管家、负责人说）、答题、停止、重开、记为看过、作废一条专项记忆；不开没有线程。不持锁发请求，操作先记 `<state>/web/inbox.json` 再执行，不重放；说明和配置见[网页工作台](web.md)。

## 多参与者协作（开发中，默认关）

一件交付由几个参与者共同办成：主人点名几位，参与者带着各自的上下文往返，由一个汇总方交出一份带出处的结论，主人不用转述。本节是它的契约：参与者、交付和来源的语义，协作记录与状态机，执行闭环，轮次来源和各消费者的统一行为，能力与预检，两端，话题转交管家，开关、兼容与回退，示例消息流。已落地：兼容基线（`core/turn_source.py` 轮次来源、行为表、来源等级、开关 `COVEY_COLLAB`；`core/collab.py` 交付归属 `route`、两张状态表、限额、幂等键；出队处和任务进程开跑处的来源闸）和执行闭环（`core/collab_records.py` 协作记录，`app/collab_flow.py` 发起、派发、落账、续接、到期、停止、对账和对外的操作，`app/collab_checks.py` 预检，`app/collab_channel.py` 咨询通道，硬只读 `CONSULT`）。两端也已落地：网页的协作条目和操作，飞书的协作卡（`cards/collab_card.py`、`app/collab_cards.py`），见[两端](#两端)。负责人这一侧（目标检查表、收尾门禁、回执合批、咨询轮计预算、答题依据校验）也已落地，写在[专项](#专项)一节。

### 参与者、交付与来源

- **参与者**：能被点名、带着自己的上下文回答的一方，身份就是稳定的话题 id（`core/collab.py` 的 `Participant`）：管家（槽 0 的 `butler_topic_id`，同时做几件事的其余几个槽不算）、负责人（`lead_topic_id`）、话题（普通话题、专项的话题）。角色从记录读（`core/butler.py`、`core/projects.py`、topic.json 的 `project.role`），不建名册；换模型、压缩、换原生会话，身份不变。话题本身就是参与者：被问时在从自己原会话分叉出来的临时只读会话里答（[咨询通道](#执行闭环)），原会话不动，答完它原来的目标、任务和最近交付都不变。首版只在主人的私聊里，代答话题和群里的话题不参与。
- **交付与汇总方**：每次协作只有一个汇总方（`integrator`，记录里的一个参与者），对主人交那一份结论；其余是协助方（`helpers`）。汇总方是这一次协作上的责任，不是新角色：已有话题里请人帮忙，原话题就是汇总方。专项负责人对专项的目标负责，和某次协作的汇总方是两层，不各发一份相同的总结。
- **来源**：谁说的（`asserted_by`）和谁写下的（`recorded_by`）是两个字段，不合成一个；取值 `owner`、`butler`、`lead`、`topic`、`bridge`（`core/turn_source.py` 的 `PARTIES`）。来源等级 `level` 只由 Bridge 按真实入口写（`provenance`）：主人说的、而且出处是 Bridge 自己收进来的（飞书消息或网页 `say` 的消息 id、问题卡的 token），才是 `owner`；其余都是 `agent`，模型写的“主人同意了”也是。模型的工具参数里没有 `level`，把 `asserted_by` 写成 `owner` 也换不来更高的等级。转述主人的话引用同一个出处，不另起一条，免得越转越像“大家都确认过”。

### 协作记录与状态机

- **记录** `<state>/collab/<id>.json`（派发的实现写；只有 Bridge 写，`record` 锁内不发网络请求，规则 1）：`id`、`chat_id`、`root`（根请求：主人发起这次协作的那句话的消息 id 和任务键）、`goal_rev`（汇总方那时最新一轮主人的话的任务号：主人中途改了要求，旧的答复不当成新要求的答案）、`integrator`、`helpers`、`asks`、`state`、`stopped_by`、`created_at`、`updated_at`。`asks` 每条是 `id`、`to`、`from`、`number`（1 咨询，2 澄清）、`text`、`state`、`job_key`、`reply`（正文、证据、出处话题的轮次和核查时刻、产物版本）、`deadline`。
- **幂等**：`collab_id(根请求, 汇总方, 协助方)` 同一句话、同一组人永远是同一条（协助方不分先后），再提交一次找到的是原来那条；`ask_id(协作, 对象, 第几次)` 每位只有咨询和澄清两次（`ASKS_PER_HELPER`），重复提交不重复派发。
- **一次咨询的状态**（`ASK_TRANSITIONS`，规则 4，终态不回退）：

  ```text
  checking 待校验 → queued 排队 → taken 已接手 → replied 已答复 → read 已被汇总方读取
  checking、queued → not_taken 未接手；checking、queued、taken → failed 失败、cancelled 已取消；queued、taken → expired 到期
  ```

  - 待校验：准入、共享范围、脱敏、能力预检（见下），不过就是未接手，写原因。
  - 排队：进协作待办，等接收方手上的一轮安全结束再开，不走补充强插；主人排着的话先走。
  - 未接手：接收方在等主人回答（不消费那道题、不代答）、已停止（不唤醒）、能力不够、版本不支持。
  - 已答复：答复按 `verified_reply` 存，带证据、出处话题和核查时刻；可以是“不知道”“缺材料”。
- **一次协作的状态**（`COLLAB_TRANSITIONS`）：`open`，到汇总方交出结论是 `done`，主人停了（或汇总方续接两次都没完成，`stopped_by` 是 `bridge`）是 `cancelled`。
- **限额**（首版默认，试点后再调，都在 `core/collab.py`）：主人点名最多 3 位协助方（`HELPERS_NAMED`，多了发起前就拒，什么都不发），不是主人点名的最多 2 位（`HELPERS_DEFAULT`）；协助方不再去问别人；排队最多 10 分钟（`QUEUE_SECONDS`），整次 30 分钟（`WINDOW_SECONDS`），这两个时限可用 `COVEY_COLLAB_QUEUE_SECONDS`、`COVEY_COLLAB_WINDOW_SECONDS` 调（`core/collab_records.py`）。咨询轮的 token 记进**一个**专项：汇总方所在的，否则被问的协助方所在的（`project_loop.consult_tokens`，按任务键只记一次）；不两边各记，话题本身没有预算。
- **停止、到期、重启**：
  - 主人停这件事：协作 `cancelled`，它下面排队和在跑的咨询轮停掉，接收方别的工作不动；迟到的答复留档，不唤醒任何人。
  - 到期：那次咨询 `expired`；汇总方拿已有的答复和缺口接着做，不把主人原来的工作标成失败，也不自动去问主人。
  - 重启：按记录对账，记了 `taken` 而任务已结束的按任务终态补；不重跑模型来补投递，重投只发保存的答复（规则 5）。
- **投递边界**：咨询轮的答复只回到协作记录和汇总方：不发到私聊顶层、不占接收方答案卡的结论位置、不算接收方“有结果”、不改它的话题要点和完成标准、不进专项回执的“做完了”；接收方的话题串里只多一条“已协助 T3”的回执。汇总方的续接轮是汇总方自己的交付，照它的放置表发。

### 执行闭环

`app/collab_flow.py`（`COVEY_COLLAB=1`；关着时不起线程、不加规则行、不读不写任何协作记录）：

- **发起**：两个入口共用一套校验、预检、幂等和状态机，不双轨。
  - 检查点：主人在普通话题（或专项的话题）里亲自发的那一轮（`direct`：飞书 `om_…` 或网页 `web-…`，来源是旧来源或 `owner`，不是自动重试），规则里多一行 `RULE`（`turn_note`，只在 Harness 声明了 `collab` 时），模型在检查点写 `collab_ask`：`[{to, question}]`（`to` 是 T 号、`管家` 或 `负责人`）。Bridge 在任务进程结束后读（`Collaboration.after_runner`，经 `app/detached.py` 的 `collab_after`），只认结束得好（`done`、`waiting_user`）且回复已授权的那一轮。主人原话里点了某位的 T 号，算主人点名。
  - 操作：`Collaboration.start(ctx, chat, root=, integrator=, asks=, owner_named=, asserted_by=)`、`clarify(ctx, collab_id, helper, question)`、`stop(ctx, collab_id, by=)`、`view`、`of_chat`，给网页和飞书卡片直接调用；交付归属照 `collab.route` 先定好再调。
  - 根请求是主人那句话的消息 id，`collab_id(根请求, 汇总方, 协助方)` 再提交一次找到的是原来那条。汇总方的话题串里多一行“🤝 协作 xxxx：已请 T5 协助；T7 未接手（原因）”。
- **预检**（`app/collab_checks.py`）：发起时（`admission`）只在主人私聊（规则 11 的 `agent_chat`，群和代答都不行）、开关、汇总方的 Harness 声明 `collab`、不在设备上；人数：主人点名的最多 3 位，没点名的最多 2 位，多了什么都不发。每位协助方（`helper_refusal`）：不是自己、同一个私聊、不是代答话题或咨询通道、没下线、不在设备上、Harness 声明 `collab` 和 `read_only`、敏感词脱敏两边都开或都不开、在别的专项里只在主人亲自点名时。不过的那位是 `not_taken`，写原因。
- **咨询通道**（`app/collab_channel.py`）：每个（协作，协助方）一个临时话题目录 `topics/collab-<协作>-<协助方摘要>/`（`core/collab.py` 的 `channel_id`；`core/butler.py` 的 `CHANNEL`：没有话题串、没有 T 号、不上看板、不回带、卡片寻址不到），有自己的 `history.jsonl`、会话指针和任务记录，`topic.json` 抄协助方的绑定（Harness、模型、工作区、脱敏）。咨询轮在这里跑，从协助方当前的原生会话分叉（`fork_start`：Claude 遇到协助方正在跑的一轮或等着的回退，用那一处的 `keep` 作 `fork_at`，只读、不删标记；Codex 只在协助方没有在跑时 `thread/fork`）；没有能分叉的会话时从协助方最近的话题记录开新会话，证据级别写“话题记录（无原会话）”。分叉了什么写进这一轮自己的任务记录（`collab_evidence`）。协助方的会话指针、历史、回退标记、任务记录和最近交付，管家的快照记录、分路账本、压缩请求，负责人收件箱的 `taken_by`，一样都不动（问管家时带一份不保存的快照 `take_snapshot(save=False)`，问负责人时带一份只看不取回执的状态包 `project_lead.read_only_pack`）。
- **派发**（`dispatch`，发起时一次，之后 `collab` 线程每 15 秒）：协助方空闲才派：先把咨询记成 `taken`（消息 id `collab-<咨询>`，`core/collab.py` 的 `ask_message`），再在通道里 `enqueue_chat_job(source=turn_source.make(ASK, …))`；协助方有排队或在跑的任务就留在 `queued`（不当补充插进去，主人排着的话先走）；在等主人回答、从没开始、最近一轮被主人停了就是 `not_taken`，不碰那道题、不唤醒。咨询轮没有任务卡，结果静默落账（`own_delivery` 为否）。
- **落账**（`settle_ask`）：咨询轮 `done` 且回复已授权（规则 5，`load_authorized_reply`）是 `replied`，存答复原文、证据（分叉还是话题记录、截至协助方的哪个任务）和当时的 `goal_rev`；协助方的话题串里多一行“🤝 已协助 T3：…（只读咨询：本话题的会话、记录和手上的事都没有动）”。失败或中断的那一次由协作记录用同一个咨询再排一次（`RETRIES` 1，消息 id 加 `-r2`），不走恢复卡和自动重试；再不行是 `failed`。
- **续接**（`advance`）：没有咨询在等时，给汇总方排一轮 `collab_reply`（消息 id `collab-<协作>-reply-<n>`），带主人原话、每位的答复和证据、没答上的原因；这些咨询记成 `read`。续接轮是汇总方自己的交付（照它的放置表发）；它可以对同一位再澄清一次（检查点再写 `collab_ask`，第 2 次，`clarify`）：协助方在两次之间没跑过新任务，续接第 1 次的分叉（通道指针还在）；跑过，清空通道指针、重新分叉，提示里带第 1 次的问答原文和“又跑了 N 轮”。协助方不能再去请别人（咨询轮没有检查点）；对没请过的人只能等主人发话。续接轮没留下新的咨询就是 `done`。
- **主人的新话优先**：续接轮的消息里，`goal_rev` 之后主人在汇总方话题里又说的话（`goal_note`）写在最后，说明以主人最新的要求为准、旧答复只作参考；续接轮排在主人已排着的消息后面，不当补充，不消费等着的题（`answers_questions` 为否，`Runs._continue_previous`）。
- **到期、停止、重启**：排队超过 `QUEUE_SECONDS`，或整次超过 `WINDOW_SECONDS`，没答复的咨询 `expired`（在跑的那一轮停掉），汇总方拿已有的答复和缺口接着做，主人原来那一轮不标失败；主人停这件事（`stop`，或停汇总方的续接轮，按 `core/jobs.py` 的 `owner_stopped` 认：没写谁停的不算主人停的）：`cancelled`，排队和在跑的咨询轮、续接轮停掉，协助方别的工作不动，迟到的答复记在 `late` 里，不唤醒任何人；续接轮失败、中断或被别人停（负责人、守卫、Bridge）时，协作记录给它再排一次（`REPLY_RETRIES` 1，消息 id 加 `-r2`，失败那一轮的 `retried_by` 指向它，恢复卡的检查并继续不再排第二个；自动重试要来时等自动重试），再不行协作 `cancelled`、`stopped_by` 记 `bridge`，汇总方话题里说一行；重启后线程按记录对账：记了 `taken` 却没有任务记录的按同一个消息 id 再排一次，任务已结束而没人落账的按任务终态落账，从不重跑模型补投递。
- **负责人回执**：咨询有了结果，汇总方所在的专项（协助方在另一个专项时，那边也）收一条 `collab_answered`（不叫醒）；续接轮检查点写的 `collab_gap`（补不上的缺口、冲突、依赖）收一条 `collab_gap`（叫醒，`app/project_loop.py` 的 `autowake_due` 按 `collab.RECEIPT_WAKES`）。咨询轮不写 `thread_end`。开协作时普通回执合批（`settling`、`COVEY_PROJECT_BATCH_SECONDS`）；负责人醒来那一轮读到的只有已回带的 `thread_end` 或 `collab_answered`、它又只记了 `thread` 或 `note` 汇报时，回复不外发（`project_loop.told_already`），汇总方会自己交付，负责人不再转述。

### 轮次来源

- **字段** `turn_source`（`core/turn_source.py`，任务上是 `BridgeReplyJob.turn_source`）：`{v, kind, asserted_by, recorded_by, origin, level, collab, ask}`，只由 Bridge 建任务时写（`make`），有值时才进任务记录、`queued/` 和 `handoff.json` 的 `extras`。
- **没有这个字段就是旧来源**（`LEGACY`）：主人的消息、唤醒轮、到点回来看、自动重试，以及更早版本排下的转交和续接，都照原来的判断（消息 id 的前缀、发送者、话题的角色）。主人的消息仍不盖章；带这个字段的是咨询轮（`collab_ask`）、续接轮（`collab_reply`）、转交管家的那一轮（`handover`），以及 W1-B 起盖章的转交和续接（下面四个 kind）。
- **kind**（v1）：`owner`（盖了章的主人消息，还没有哪个版本盖）、`collab_ask`（协作咨询）、`collab_reply`（协作答复续接：答复到齐或到期后汇总方接着做的那一轮）、`handover`（话题转交管家，见下）。加新 kind 就是改协议：旧版本不认识的一律不跑，所以给已有来源补 kind 之前，回滚的目标版本要先认识它。
- **转交和续接的五个 kind**（W1-0 读者先行，认识、照行为表跑；W1-B 起各入口经 `stamp` 盖章，回滚到 W1-0 的 release 仍照表跑，回滚到更早的版本才会被取消，所以 W1-0 是回滚下限）：`relay`（管家转交）、`lead`（负责人派活）、`wake`（叫醒负责人，W1-A 盖）、`answer`（主人作答续接）、`answer_for`（负责人替答续接）。
  - **盖章口** `stamp(kind, *, asserted_by="", origin=None, via="")`：`asserted_by` 不给就按 kind 取（`STAMP_BY`：`relay` 管家、`lead` 负责人、`wake` Bridge、`answer` 主人、`answer_for` 负责人）。模块常量 `STAMPING` 改成 False 就全部返回 None，这些任务回到旧来源（单点急停，不读环境变量）。
  - **谁盖**：管家的 `topic_start`、`topic_send`、`topic_fork`（`app/butler_topics.py` 的 `source_of`）和 `project_send`（`app/butler_projects.py`）盖 `relay`：管家这一轮回答的是主人的一条消息（`butler_handoff.asked` 的 `message_id`）时 `asserted_by` 是主人、`origin.message_id` 是那条消息，否则是管家；负责人经同一组工具派活盖 `lead`（`origin.topic` 是负责人话题）。答题的续接（`app/questions.py` 的 `_queue_continuation`，在任务进程里经 `enqueue_answer` 的 `turn_source` 键交给 Bridge）按问题记录的 `answer_by` 盖：主人自己的、管家带主人消息代答的盖 `answer`（`origin.question` 是问题 token，管家代答另带 `origin.message_id`），负责人替答的盖 `answer_for`；不知道是谁答的（旧任务进程只给 open_id）不盖。字段另可带顶层的 `via`（`butler`、`lead`、`web`、`card`、`message`）。读口：`level`（主人的话还是别人的：旧来源看消息 id 是不是 `om_`、`web-` 开头，盖了章的看 `provenance`）、`origin`、`via`、`downstream`（某条主人消息派生出来的在跑任务）；app 层判断“是不是主人本人的原话”统一用 `app/provenance.py` 的 `owner_said`（再加 `Access.owner_turn` 和不是自动重试），`synthetic` 说这一轮是不是 Bridge 或模型造的。
- **行为表** `BEHAVIOR`：每个消费者只问 `behavior(任务或记录)`，不自己看 kind；不认识的 kind、更高的版本、格式不对的一律是 `NOTHING`（什么都不碰，按只读算）。

  | 行为 | 旧来源、`owner` | `collab_ask` | `collab_reply` | `handover` |
  | --- | --- | --- | --- | --- |
  | `owner_words`：话是主人的（记忆和记一下从它取，算负责人的主人触发轮） | 是 | 否 | 否 | 是，按消息 id 取主人原话 |
  | `answers_questions`：可以当话题里等着的题的回答 | 是 | 否 | 否 | 否 |
  | `steers`：可以作为补充进在跑的任务 | 是 | 否 | 否 | 否 |
  | `read_only`：只读 | 照任务 | 是 | 否，照汇总方原来的授权 | 否 |
  | `own_delivery`：结果是这个话题自己的交付 | 是 | 否 | 是 | 是 |
  | `owner_send`：可以出代发草稿 | 是 | 否 | 是，主人在卡上确认 | 否 |

  转交和续接的五个 kind（`read_only` 都是否，照任务；`own_delivery` 都是是）：

  | 行为 | `relay` | `lead` | `wake` | `answer` | `answer_for` |
  | --- | --- | --- | --- | --- | --- |
  | `owner_words` | 否（主人原话在 `origin` 里） | 否 | 否 | 是 | 否 |
  | `answers_questions` | 否 | 否 | 否 | 是 | 是 |
  | `steers` | 是 | 是 | 否 | 否 | 否 |
  | `owner_send` | 是 | 是 | 否 | 是 | 否 |

- **已接上的消费者**：主人纠正的记忆（`app/memory_collect.py`）和记一下（`app/knowledge_collect.py`）的 `reason_to_ignore` 多一个 `not-owner-words`；代发草稿（`app/owner_send.py` 的 `collect`）；`Submission.enqueue_chat_job` 的 `source`：不是主人的话不触发负责人的主人触发轮（`project_loop.owner_turn`；管家的 `project_send` 开的新一轮自己补调一次），也不当补充送进在跑的任务；`TaskControls.acknowledge_waiting_answer` 和 `Runs._continue_previous` 都问 `answers_questions`，转交和派活不收尾话题里等回答的那一轮。
- **谁答的** `AnswerBy`（`app/answers.py`，`party`、`channel`、`open_id`、`message_id`）：话题里的话（`owner/message`，消息 id）、卡上点（`owner/card`）、网页（`owner/web`，操作 id）、管家 `question_answer`（`butler/tool`，主人那条消息的 id，取不到就不代答）、负责人 `project_answer`（`lead/tool`）；发的人不是主人（群成员在自己的话题里答）时 `party` 为空。问题进 `submitting` 的同一次写落 `answer_by`（`party`、`channel`、`message_id`）；交给任务进程时 `by` 仍是 open_id 字符串（旧任务进程只读它），另加可选的 `answer_by`。只有主人自己的（他的 open_id）或管家带主人消息代答的记成主人的决定（`origin` 带 `question`、`message_id`、`via`）；卡上附带的“以后这类按推荐走”只认主人点卡和网页（`owner_clicked`）。
- **话题里一句话算回答的三个前提**（`app/interaction.py` 的 `_interactive_command`）：来源不是 `synthetic`（管家、负责人转来的话一律暂存，暂存记录带 `by`，随主人的回答带给模型时单列成“管家转来的补充（不是用户本人说的…）”，不进主人的原话；分流扣在负责人手里、主人还没看到的题也照样暂存）；题已经给主人看过（这句话刚让分流把题转给主人的，只暂存）；管家一家（顶层）的题只认引用了题卡（卡片本身、顶层副本或提问那一轮的任务卡）的消息，顶层没引用的新消息一律开新一轮，题照旧等着（多路并行里管家自己那一轨在等回答时也算在跑，见 `app/butler_tracks.py` 的 `live_entries`、`unasked`）。
- **代发准入** `owner_send.allowed(job, ctx, access)`：`turn_note`（这一轮被告知的规则）和 `collect`（检查点里的草稿）共用；来源不许（`owner_send` 为否）、盖了章但不是主人的话（负责人派活、没有主人消息的管家转交、协作续接）、代答话题、管家一家、只读、群成员的一轮，都不给代发草稿。
- **执行闭环接上的**：协作轮不经 `BridgeApp.handle_event` 和网页 `say`，只经 `enqueue_chat_job(source=…)`，所以不消费等着的题（`answers_questions` 为否时 `Runs._continue_previous` 也不收尾等着的那一轮）；`own_delivery` 为否（咨询轮）时：没有任务卡（`Submission.publish_submission`、`Cards.reveal`）、静默投递（`TurnDelivery.silent`，保存的回复照样算已投递）、不回带（`butler_relay.after_turn`）、不出恢复卡（`TaskControls.show_recovery_card`、`report_unfinished`）、不自动重试（`auto_retry.plan`、`plan_restart`、`policy_retry.at_stop`）、不写 `thread_end` 回执也不进资料库（`project_loop.after_runner`）、不排到点回来看（`revisits.after_runner`）；`owner_words` 为否时这一轮的规则里不加记忆的纠正规则和记一下（`app/memory_turn.py` 的 `fix_rule`、`app/knowledge_turn.py` 的 `remembers`）；检查并继续、自动重试再排同一轮时带上它的来源（`TaskControls.requeue`），续接轮重试还是续接轮，不会变成主人的话。咨询通道的任务不进看板、网页的话题、话题搜索和工作区的最近话题（`core/board_read.py` 的 `topic_jobs`、`app/butler_search.py`、`app/workspace_gists.py`）。
- **读主人的记忆和原则不看来源**：`Runs.owner_turn`、`Access.owner_turn` 回答的是“这一轮是不是在主人自己的空间里”（隐私闸：群成员的一轮不读主人的私人条目），协作轮在主人的私聊里，照常读；“这句话是不是主人说的”只看 `owner_words`。

### 能力与预检

- **三方都声明才派发**（`refusal`；`RUNS` 现在是 `owner`、`collab_ask`、`collab_reply`、`handover` 和转交续接的五个；只有协作的两个要开关和能力）：
  - Bridge：`COVEY_COLLAB=1`，而且本 release 的 `RUNS` 有这个 kind；
  - 任务进程：它自己 release 的 `RUNS` 有这个 kind，开跑前再查一次（`app/worker.py`）；
  - 后端：`HarnessCapabilities.collab`（咨询在接收方原会话的临时分叉里跑，限制只管这一轮，接收方的会话、指针、历史、回退标记都不动；真实拒写验过才声明），协作咨询另要 `read_only`（硬只读）。Claude Code 和 Codex 声明了（C8 见下）；Antigravity、dsh 和扩展的 Harness 没有，预检就是 `not_taken`，后端不启动；设备上不声明（`DEVICE_OFF`）。
- **硬只读 `CONSULT`**（`core/harness.py`，`app/tool_limits.py` 的 `limit`：咨询轮的来源 `behavior(...).read_only`；和群成员的 READ、管家判的 LOOK 分开，几种同时命中取最严：CONSULT、PROXY、READ、LOOK）：
  - Claude Code（`backends/claude/argv.py`）：只给 `--tools Read,Grep,Glob`，`--strict-mcp-config --mcp-config {}`，不管 `options` 里有没有 Bridge 的工具（`mcp_config`）或负责人的只读（`lead`）；没有 Bash、写工具、WebFetch、Agent、AskUserQuestion，没有检查点。
  - Codex（`backends/codex/consult.py`、`appserver.py`）：私有 `CODEX_HOME`（`<state>/tmp/codex-consult-*`，链接真实 home 除 `rules/`、`hooks*`、`plugins/`、`skills/`、`memories*`、`history.jsonl`、`config.toml` 外的全部条目，自写只含 `model_provider`、`model`、`service_tier` 和自定义 provider 表的 `config.toml`，用完删掉）；app-server 加 `features.apps/plugins/remote_plugin/memories/hooks/goals/image_generation=false`、`memories.generate_memories/use_memories=false`、`web_search="disabled"`、不开非阻塞提问；线程和本轮都是 read-only 沙箱；不挂 Bridge 的工具；模型的 `request_user_input` 一律拒绝（`rpc.no_questions`）。
  - 咨询轮不给检查点路径（规则里换成 `collab_channel.RULES`），Bridge 也不读它的检查点；不重试、不续接、不提问。
- **C8 真实拒写验收**：层 1 `tests/test_collab_backends.py`（参数、私有 home、合并、分叉计划）；层 2 `tests/test_collab_c8.py`（`BRIDGE_C8_REAL_CLI=1`，本机 CLI 经 `ClaudeBackend.run`、`CodexBackend.run` 接脚本化的本地假模型，不花模型调用费）：Claude Code 2.1.330 上 Write、Edit、NotebookEdit、Bash、WebFetch、Agent、EnterWorktree、三类 MCP、AskUserQuestion 都是“No such tool available”，模型只拿到 Read、Grep、Glob；codex-cli 0.171.0 上源 home 带 `touch`、`curl` 的 allow 规则也被拒（Read-only file system），curl 连不出去，工具里没有 Bridge 工具、应用、web_search；两家都是分叉出新会话、原会话文件哈希不变、工作区和场外金丝雀不变、回环接收端 0 次、MCP 没被启动、Codex 的记忆不变。同一文件另验 Codex 的只读轮：`read` 续接原线程时源 home 的 allow 规则也被拒，主人的下一轮续接同一线程；`look` 开的线程留在主人 home，下一轮能续接。Harness 升级后重跑层 2；层 3（飞书真机）随两端的实现做。
- **不支持时**：协作记录留在待校验或排队（协作待办），不建任务，绝不改成主人的消息再发。任务已经建了的（新版本写下、回滚后被这一版读到）：Bridge 出队处（`app/job_dispatch.py` 的 `refuse_source`）和任务进程开跑处都记 `cancelled`、`never_started`，状态说明写原因，不跑模型，也不出恢复卡；认识它的版本按任务终态对账。
- **预检还查**：准入（`app/access.py`，规则 11）；共享范围：只在同一个主人的私聊里，跨专项只在主人这次亲自点名时；脱敏：开了脱敏的话题的内容只发给同样开了脱敏的接收方，不送到限制更弱的一端；设备：只传接收方够得着的证据引用和有限摘录，够不着的写明，不把本机路径当附件；接收方此刻的状态（见上）。

### 两端

- 网页（大厅、话题页）和飞书（私聊顶层、话题串）用同一个协作 id、同一份来源和状态；卡片和网页只是投影，两边各点一次也不会跑两遍：网页操作按 `op_id` 去重（`<state>/web/inbox.json`），飞书消息按消息 id 去重（`inbound-messages/`），协作按 `collab_id` 去重，停止按协作记录的状态去重（已结束的再点只刷新，不再执行）。
- **网页**（已落地）：协作条目、发起、澄清、停止等操作和版本写在[网页工作台 · 协作与三层记忆](web.md#协作与三层记忆契约开发中)；网页经 `Collaboration` 的 `start`、`clarify`、`stop`、`view`、`of_chat` 读写，来源是网页的消息 id（前缀 `web-`）。
- **飞书的协作卡**（已落地，`COVEY_COLLAB=1` 才有，关着什么都不发、不加入口）：`cards/collab_card.py` 从 `collab_records.view` 画整张卡（纯函数），`app/collab_cards.py` 发它和更新它。卡发在汇总方的话题串里，汇总方是管家（顶层）或负责人时放在 `collab_flow.thread_root` 给 `tell` 的同一个地方；标题“🤝 协作 · 由 T3 汇总”，列参与方（汇总方、协助方）、每次咨询和澄清的逻辑发言者（“T3 → T5 · 咨询 · 已答复”，状态词是 `core/collab.py` 的 `ASK_WORDS`）、问题和答复的摘要、证据级别（`EVIDENCE_WORDS`）和核查时点、未接手、失败、到期、取消的原因，迟到的答复写“只留档，没有交给汇总方”。协作开着且有排队或在跑的咨询（或排着汇总方的续接）时有一个“停止这次协作”按钮，值是 `{kind: bridge_collab_stop, collab: <协作 id>}`。
  - **更新**：`collab_flow` 在发起、咨询落账、续接、停止、到期这些改记录的地方调 `CollabCards.refresh`；记录里的 `card`（`card_id`、`message_id`、`sequence`、`digest`）说卡上现在画的是哪一版，画出来一样就不写；整卡写一次、sequence 从 `reserve` 取并先存（规则 3），记录锁里只读和存、不发请求（规则 1），同一个协作的卡由 `resource_lock("collab-card")` 串起来。第一次发时 `card_id` 先存进记录，没送达就用同一张卡、同一个键再发；卡发不出去时才退回原来的一行文字，同一件事不发两遍。
  - **点击**：`bridge_collab_stop` 在 `ACTIONS` 里（不建任务，不设 `bot_only`），`CollabCards.claim` 在话题路由之前认领（`PANELS` 里的 `collab.cards`）；只认主人（`app/access.py`，规则 11）；协作还开着就在点击确认之后调 `Collaboration.stop(ctx, collab_id, by="card")`，已经结束的（包括网页先停了的）只刷新卡、不再执行 `stop`。
- 飞书在同一个机器人下显示逻辑发言者（“T5 → T3”）、往返和交付，用 T 号、引用和卡片上的选择发起；不承诺用飞书原生的 @ 找到并不存在的独立机器人账号。卡片 JSON 变了要做真实客户端验收（规则 9）。

### 话题转交管家

话题收到只有管家能办的要求（开专项、停别的话题、把话题收进专项）时，不让主人再说一遍：

已实现（`app/butler_asks.py`，专项开着且管家开着；使用者怎么看见见[在话题里说开专项](projects.md#在话题里说开专项)）：

- 话题在这一轮的检查点里写 `ask_butler`：`{ask, why}`，只写要办什么和自己的理解。这条规则（`RULE`）只放进主人发起的那一轮的消息（`offered`）。
- Bridge 先核对这一轮是主人发起的（`direct`）：任务的消息是 Bridge 收进来的主人消息（飞书消息 `om_…` 的发送者是主人，或网页 `say` 的 `web-…`），或主人自己对这个话题问题的回答（`answer-<token>`，`owner_answer`：问题记录在，回答不是管家的 `question_answer`（`main-…`）也不是负责人的 `project_answer`（`lead-…`、分流记成负责人答了）），来源是旧来源或 `owner`。唤醒轮、协作轮、到点回来看、管家或负责人替主人答的题、管家和负责人转来的（`main-…`）、自动重试（`app/auto_retry.py` 的计数、`policy_retry`）都不能转交；自动重试写了的，话题串里说一句没转。回答转交的，`handover` 的 `origin` 另带问题的 `question`。
- 核对过的，Bridge 先在私聊顶层发一条机器人的文字消息（“↪ T12 转给管家”“你在 T12 说：<原话>”“T12 的理解：<ask>”），再在这条消息上给管家排一轮（`queue_turn`：`keep_topic`、记成管家的消息、写历史、`enqueue_chat_job`），来源是 `handover`：`asserted_by` 是 `owner`，`recorded_by` 是 `bridge`，`origin` 是那个话题和主人那句话的消息 id，`level` 是 `owner`。`handover` 不进在跑的管家那一轮（`steers` 为假），排在它后面。管家读到的是主人原话（按消息 id 从话题的历史取，`owner_words`；取不到用任务记录），话题的理解只作附注；管家照常判断（规则 `app/prompts.py` 的 `ASKED_RULE`），开专项仍走它自己的工具（`topic_start` 带 `project`），归进已有专项用 `project_send` 的 `topics`。
- `handover` 在 `RUNS` 里。比兼容基线早的 release 不认识 `turn_source`，照主人的消息跑；兼容基线那一版认识它但不在 `RUNS`，回滚到它时排着的转交轮记为取消、不跑。

### 开关、兼容与回退

- `COVEY_COLLAB`：不设是关，不派发任何协作轮、不起 `collab` 线程、规则里没有 `collab_ask`，负责人的规则、状态包、工具和收尾都和原来逐字一样；`1`：三方都支持时派发（`collab_ask`、`collab_reply` 在 `RUNS` 里），负责人对照目标检查表跟进、答题要写依据，网页和飞书出协作条目和协作卡。
- **兼容基线**是提交 38dd405 那一版：认识 `turn_source`，不认识、不支持的不跑；没有 `turn_source` 的任务、`topic.json` 和 `runs/` 协议都和原来一字不差（`tests/golden/run_dir.json` 不变）。执行闭环这一版只多跑两个 kind，不改协议。
- 比兼容基线早的 release 不认识这个字段：`app/runner_spawn.py` 的 `load_job` 把它当一个不认识的属性带上，照主人的消息跑。所以有过协作任务的部署，回滚目标不得早于兼容基线；回滚到兼容基线时排着的协作轮记 `cancelled`、不跑，咨询通道的目录和 `collab/` 记录留着不读。
- **回退步骤**：
  1. 关 `COVEY_COLLAB` 重启，停新的派发；
  2. 等在跑的协作轮结束或停掉：`collab/` 里没有已接手的咨询，`jobs/` 里没有带 `turn_source` 又没结束的任务；
  3. 再回滚到不早于兼容基线的 release。

  网页的“停止这次协作”只在协作开着、有排队或在跑的咨询时出现；不认 `payload.collab` 的旧后端会把它当成普通停止（停掉汇总方话题手上的那一轮），所以先做完第 1、2 步再换后端，那时网页上已经没有这个按钮。

  没按顺序来时，兼容基线照样不跑新版本写下的协作任务（记 `cancelled`）；已经在跑的协作任务进程是新版本的代码，自己按协作的规则投递，Bridge 这边的收集器按 `behavior` 跳过主人的那几样。

### 示例消息流

`core/collab.py` 的 `route` 给每种说法一个交付的归属，`tests/test_collab.py` 逐条核对：

| 主人怎么说 | 交给谁 | 谁汇总 | 协作记录 |
| --- | --- | --- | --- |
| 大厅（私聊顶层）不 @ | 管家，照原来 | 管家自己判断 | 无 |
| 单 @ 一位（负责人或话题） | 直达那一位，照原来 | 那一位 | 无 |
| 多 @，引用了某个话题 | 协作 | 被引用的话题 | 有 |
| 多 @，没有上下文 | 协作 | 主人选的；没选就是第一个点到的普通话题，输入框写明、可以改 | 有 |
| 多 @，只点了负责人 | 管家归口一次，找出归谁 | 管家找出的那一位 | 归口后有 |
| 在话题里请别人帮忙 | 协作 | 这个话题 | 有 |
| 引用 A 的话 @ B | 直达 B，引用一起带上 | B | 无 |
| 引用一次协作里的话 | 那次协作 | 它的汇总方 | 同一条 |
| 负责人收到普通的协作回执 | 只进收件箱，不叫醒 | — | — |
| 明确点名负责人 | 负责人参与，作协助方或汇总方，会跑 | 按上面各行 | 有 |

主人说“由 X 汇总”时以他说的为准。协助方超过 3 位，发起前就拒。

## 执行指标口径

任务卡和任务详情上 token、缓存、平均 token/s 和 TTFT 的共同口径（面板见[任务卡与执行过程](usage.md#任务卡与执行过程)，延迟字段见[健康诊断](troubleshooting.md#健康诊断)）；各后端从哪里取数见 [Harness 说明](harnesses.md)各节的“执行指标”。

- **token**：本轮主任务加各层子任务的合计，是各次请求的累计。模型生成、token/s 和 TTFT 只算主任务。没提供或不完整的项不显示，不估算，不把未知记为零。数值写进任务记录和执行过程快照，结束时冻结。
- **上下文**：`token_usage` 可带 `context_tokens`（主任务最后一次请求的输入加输出）、`context_window` 和 `model`。轮前压缩算进本轮累计、不算上下文。
- **缓存**：命中和写入已含在输入总量里，不能再加。任务详情结束后把输入拆成“普通输入 + 缓存写入 + 缓存命中”。
- **子任务**：`subtask_usage` 事件（`core/usage.py` 的 `SubtaskReporter`）；`core/trace.py` 的 `turn_usage` 算合计，同一请求只记一次，有缺口写“部分统计”。
- **平均 token/s**：`core/usage.py` 的 `rate`，输出 token 除以测得的活跃生成时间，是含等待和思考的观测吞吐。工具运行期间的请求不计入分子（`MeasuredOutput`），这时卡上写明分子。
- **TTFT**：本轮主任务第一个模型输出的时刻，不含排队、进程启动、会话恢复和飞书渲染；`ttft_source` 记来源，dsh 没有。第一个输出是工具调用时不写。
- **阶段与区间**：`core/metrics.py` 的阶段 `received`、`submitted`、`queued`、`dispatched`、`backend_requested`、`backend_started`、`first_output`、`execution_finished`、`delivery_started`、`delivered` 各只记第一次。区间：`selection_ms` received → submitted，`submission_ms` → queued，`queue_ms` → dispatched，`prepare_ms` → backend_requested，`first_output_ms` → first_output，`execution_ms` backend_requested → execution_finished，`delivery_ms` delivery_started → delivered，`total_ms` received → delivered。用时的写法是 `run_seconds`、`took_text`。

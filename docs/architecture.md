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
| `backends/<名字>/` | 一个后端一个目录（`codex`、`claude`、`dsh`、`pi`；[扩展](#扩展)带来的后端在扩展自己的目录里）；`acme/` 是 Acme 网关和中继，可被别的后端导入 | 导入 `app/`、`feishu/`、`cards/` 或别的后端 |
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
- **整理专项**：`app/project_tidy.py` 的 `ProjectTidy` 是协调器，不继承任何类：`__init__` 里依次造出各部件（构造参数就是它的依赖），自己管后台线程（`tick`）、话题要点收集和时机（`scan`、`saw`、`drain`），对外方法（`start`、`claim`、`matches`、`show_joined`、`web_*`）转给有它的部件。部件从下往上：纯函数和提议的认领 `app/tidy_facts.py`，共享状态 `app/tidy_state.py`（`busy`、`progress`、`counted`、`handed`），话题在卡上的字样 `app/tidy_words.py`，照办（建、归进、移出、合并、拆、收起，标话题的卡）`app/tidy_apply.py`，提议新专项、提议卡与跟着归并更新 `app/tidy_proposals.py`，实时归并 `app/tidy_realtime.py`，定时或手动的一次整理与整理卡 `app/tidy_run.py`，卡片点击 `app/tidy_clicks.py`，网页面板 `app/tidy_web.py`。上面的部件只调下面的，`TidyPartsTest` 检查导入方向。

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
 → done.json、exit.json → Bridge 的 finish_detached 收尾，未结束的记录标 interrupted，原卡换恢复卡
```

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
| `runner.json` | 任务进程身份和 release |
| `wake.json` | 任务进程的身份；Bridge 每写一次收件箱就经 pidfd 发 SIGUSR1（`wake_runner`），旧 release 照旧轮询 |
| `inbox/` → `acks/` | 点击、回答、补充、重启通知与确认 |
| `requests/` → `replies/` | 续答入队请求 |
| `done.json`、`exit.json` | 只剩产物上传时、退出时 |

- 每条消息是原子写入的一个 JSON 文件，读方按文件名顺序取一条删一条。
- 预先拉起：接着就运行的任务在建任务卡之前由 `prestart_runner` 先起任务进程，它预热后等写明同一随机串的 `handoff.json`，30 秒没等到就退出。
- 任务进程从不自己建任务，续答经 `RunnerLink.enqueue` 请 Bridge 入队。`runner_argv()` 把路径改成绝对路径，`runner_env()` 清理 `PYTHONPATH`。
- 停止不只靠这一轮的线程：`cancel_reply_job_from_card` 设好 `cancel_event` 后、写卡和记录之前先 `hard_stop_later`，`HARD_STOP_AFTER`（5 秒）后后端进程还在就给它的进程组发 SIGTERM。平时这一轮的线程一两秒内自己看到停止、先关掉后端，这一下什么也不做；盘卡住时那条线程可能在记录锁上等几分钟，后端照样停。停止之后、后端还没做完时后端抛什么都按停止结算（`worker._turn_from_exception`）。
- `SIGTERM` 是中断，不是停止（`app/runner.py` 的 `interrupt`）：没做完的一轮不结算（`leave_interrupted`），记录留在 `running`，退出后由 Bridge 标 `interrupted`；已做完的结果照常投递。
- 没起来：退出时仍是 `queued` 且无 `exit.json` 时 `relaunch_unstarted` 再起一次；`start_runner` 抛错时 `not_started` 把记录写成 `failed`（`runner_start_failed`）并出恢复卡。
- `done.json`：Bridge 见到它（`job_done`）先交还调度槽位，同话题的下一条照常进行；点击仍转给活着的任务进程；outbox 和自动重试等它退出。
- 遗留进程：任务进程把 `COVEY_TASK_DIR` 设为运行目录，每条命令都继承。后端返回后 `app/leftovers.py` 用 `core/task_processes.py` 找出带标记、已脱离任务进程的存活进程：搜索工具（`SEARCH_TOOLS`）先 SIGTERM 再 SIGKILL，其余只在执行过程里列出。内存看门狗也读这个标记，把告警发到任务所在的话题。
- `runner_alive` 要求 pid 的启动时间、boot ID 和记录一致且 cmdline 含运行目录。
- 到点回来看（`COVEY_REVISIT=1`，`app/revisits.py`）：一轮结束后 Bridge 读它检查点的 `revisit`（几分钟后、看什么），写 `revisits/<话题>.json`；`revisits` 线程每 30 秒看一遍，到点且话题没有在跑、排队或等回答的任务时，先在锁内记 `fired` 和这一轮的消息 ID（`revisit-…`），再在锁外像 `app/project_loop.py` 叫醒负责人那样把它当话题里的一条消息入队；记了没入队的 120 秒后重排，Bridge 停着时结束的那一轮由线程补结算。检查点写 `"unchanged": true` 又约了下一次的那一轮，任务进程里 `TurnDelivery` 不发（`revisits.silent`，同专项叫醒的 `silent`，保存的回复算已投递，也不回带）。没有变化以外的结果、最后一次和第 24 次照常投递。

### 重启认领

`UnifiedBridgeApp.main` 先拿状态目录的实例锁，然后：

1. `recover_question_delivery` 核对问题记录；`recover_worktree_launches` 把建 worktree 时被打断的启动卡换回表单。
2. `adopt_runners` 按 `handoff.json` 认领存活任务进程，往 inbox 写 `bridge_restarted`；再排上 `queued/` 里的任务。
3. `recover_stale_jobs` 把其余未结束记录标 `interrupted`；`close_retired_jobs`；`release_started_launches` 排上已绑定话题里还在 `waiting_workspace` 的消息。

### 中断与恢复卡

- 卡由 `app/recovery_cards.py` 画（kind 都是 `bridge_job_retry`），状态、按钮和点击后做什么由 `core/recovery.py` 的纯函数 `classify` 判定；按钮和点击之间有意保留的分歧写在该模块开头。
- 已授权或已保存的正文没送达 → 重新投递结果；暂停问题没送达 → 重新投递问题；其余失败或中断 → 检查并继续（按原模型快照新建任务）；未授权、已送达、需核对或已 `native_answer_once` → 无按钮。
- 话题还有运行或排队的任务时拒绝；每条记录只重试一次（`retried_by`）。任务没跑完时告诉用户：`app/task_controls.py` 的 `report_unfinished`。
- **原因和下一步**：没成功的一轮（失败、超时、重启打断、官方拒绝；主人自己停的不算）由 `core/failure.py` 的纯函数 `failure_view(record)` 给出 `{title, detail, actions}`：`title` 一句人话，说原因和接下来会怎样或能做什么；`detail` 收内部用语（记录的状态行、错误码、子代理标识）；`actions` 是 continue（检查并继续）、retry（现在重试，同一个点击）、diagnose（看原因）、split（拆小再试）、switch_model（换配置）里这条记录能做的。记录靠 `failure_code`（这一轮 `ExecutionOutcome` 的错误码）和 `failure_detail`（谁在做什么时被拦）说原因，二者由 `app/worker.py` 写（守护停的由 `app/policy_guard.py` 写 `failure_detail`）；更早的记录按状态和状态行推断。
  - 任务卡：失败的一轮正文换成这句（`TurnDelivery.card_text`，加粗一行），原来的那段话收进正文下的“技术详情”（`failure_details`，元素 `failure_details`），再是按钮行（`auto_retry.failed_controls`）：限流的检查并继续叫“现在重试”，另加“看原因”“拆小再试”；换配置用已有的“换配置”。官方拒绝的正文本来就写谁在做什么时被拦（[policy-stop.md](policy-stop.md)），正文不换，只加“拆小再试”（换旧模型重试排上时什么都不加）。恢复卡（中断的）正文第一行也是这句，按钮后加同样的“看原因”“拆小再试”。
  - 恢复卡（`cards/recovery.py` 的 `recovery_view`）：开头是结论带（`cards/ui.py` 的 `conclusion_band`，docs/ui.md 5.2），小字“停住了 · T12 #431”，大字是原因、灰字是下一步（`failure.split_title` 按第一个“；”拆，原因超过 40 字再在“，”处拆），不再有彩色标题栏；正文是停在哪一步、已做完的和下一步。
  - “看原因”（`bridge_job_diagnose`）和“拆小再试”（`bridge_job_split`）都是 `bot_only`，由 `Submission.follow_up` 在本话题排一轮：话题里显示“看原因：任务 #N 为什么没成功”“拆小再试：任务 #N”，模型读到的是 `core/failure.py` 的 `follow_up`：看原因是只读排查（不改文件、不重跑、不动任务和服务，最后说清哪一步、为什么、建议怎么做），拆小再试先核对已做的，再一小块一小块做，被拦的那块先跳过。话题还有任务在跑或排队时不排，告诉主人；这条记录不提供这一步时也不排。
  - 网页：每轮的 `failure` 字段同一个 `failure_view`（`app/web_collect.py`）。

### 重启与维护

- 没有热重载，新代码靠重启；`SIGTERM`/`SIGINT` 只停 Bridge，停前等卡片分道里已开始的点击（`CardLanes.drain`，最多 6 秒）。
- `restart_blockers`（`core/run_dir.py`）列出此刻重启会打断的任务：未结束、无存活任务进程、又不是已存盘的 `queued` 或 `cancel_requested`。
- 维护（清什么见[备份、归档与维护](operations.md#备份归档与维护)）：计划与执行在 `core/maintenance.py`；新的一类落盘数据在 `core/maintenance_rules.py` 的 `RULES` 加一条，再排进 `CLEANUP` 和 `DRY_RUN`；备份在 `core/backup.py`。备份不持锁、每天一次；其余清理等没有任务，归档每步在 `LOCK` 内。
- `SharedFileLock` 拿到锁后核对锁文件仍是同一个 inode，被删过就重开。陈旧的等待记录由 `app/stale.py` 按转换表转 `cancelled`。

## 状态与并发

### 主要记录与锁

完整目录见[数据目录](operations.md#数据目录)。

| 路径 | 内容 | 写入方 | 锁 |
| --- | --- | --- | --- |
| `jobs/` | 任务记录 | Bridge、任务进程 | `record`，跨进程 |
| `questions/`、`held-notes/`、`interaction-deliveries/` | 问题、暂存消息、补充投递 | 两者 | `question`，跨进程 |
| `topics/<topic_id>/topic.json` | 绑定：工作区、`harness_id`、`binding_revision`、模型快照 | Bridge | `LOCK`，进程内 |
| `topics/<topic_id>/*_session_id` | 原生会话指针，比较 revision 更新 | 两者 | 同名 `.lock` 的 flock |
| `verified-results/`、`native-session-guards/` | 已验证回复的候选与授权；会话保护 | 两者 | 目录下 `.lock` 的 flock |
| `inbound-messages/` | 每个收到的消息 ID 一个空标记，飞书重投时跳过 | Bridge | 无：`O_EXCL` 新建 |
| `stranger-replies/` | 别人私聊时回过的那句：每人每天一个空标记 | Bridge | 无：`O_EXCL` 新建 |
| `revisits/` | 到点回来看（`COVEY_REVISIT`）：每个话题一个，等到点（`pending`）或那一轮已排出（`fired`） | Bridge | `revisit`，进程内 |
| `answer-cards/` | 单独发出的答案卡：要发的卡（`outgoing`）、发出的卡和产物状态 | 两者 | `answer-cards`，跨进程；锁内只读写这个文件 |
| 表单目录（`launch-forms/` 等）、`cards/.locks/` | 卡的 card_id、sequence、表单状态；卡片锁文件 | 发卡的进程 | 卡片锁 |
| `groups/` | 群记录 | Bridge | `group`，进程内 |

- **索引**：`core/store.py` 的 `JsonDirectoryStore` 给 `jobs/`、`questions/`、`held-notes/` 建内存索引，读目录的 `.journal` 追变化，定期列目录兜底；查询字段（`INDEXED`）常驻，整条记录被选中才读；`.index` 让新进程快速起步；`CARRIED`（`updated_at`）随 `.index` 一起存、不建索引项，查询的 `narrow` 在读记录之前先用它筛（管家快照、手上的事等的 `board_read.read` 靠它只读三天内的记录），旧 release 写的 `.index` 没有这一项时，记录自己决定，结果不变。手工改记录后调 `invalidate_directory`。一条消息、一次点击在 `one_scan()` 内处理。
- **资源锁**：`core/locks.py` 的 `resource_lock(namespace, key)`。`record`、`question`、`answer-cards`、`card` 跨进程（进程内锁加 flock），其他命名空间只在进程内。
- **持记录锁不发网络请求**（规则 1）：锁内写意图（`submitting`）并存内容，锁外发送，返回后再转状态，不确定转 `uncertain`、从不自动重发。例外：实时问题的回答在后端的 `control_lock` 内核对并发送。问题表单在每个问题一把的 `question_card_lock` 内发送。
- **`LOCK` 和话题锁**（规则 2）：`LOCK` 只保护工作区配置、`topic.json` 改写和要看所有话题的检查，从不跨飞书调用持有。同话题的提交、启动、重试、换配置由 `topic_lock(ctx)` 排队：先话题锁、再短暂拿 `LOCK`，反过来直接报错。
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
| 2 | `LOCK` 不跨飞书调用；持 `LOCK` 不申请 `topic_lock` | 一个话题拖垮全部；锁顺序反转死锁 | `core/locks.py` |
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
- `capabilities(ctx)` 的 `HarnessCapabilities` 决定任务卡控件、话题消息能否当回答、能否复制原生会话、自动重试（`rate_limit_retry`、`incomplete_retry`）、回退（`rewind`）、脱敏（`redaction`）、本轮请求导出（`turn_export`，后端的 `export_turn`，`app/turn_export.py`）、能否把一轮限在只读（`read_only`）。
- **一轮能做什么**（`core/harness.py` 的 `TOOL_LIMIT`，`app/tool_limits.py` 的 `limit` 决定，`Runs._prompt` 在这一轮任何东西跑之前写进上下文的 `options`）：`read` 是只读群里群成员的一轮（Claude Code `--tools Read,Grep,Glob` 加空的 `--strict-mcp-config`；Codex 只读沙箱，`turn/start` 另带 `sandboxPolicy`；`read_only` 为 False 的 Harness 这一轮不启动，错误码 `read_only_refused`）；`look` 是管家判为只读、没等主人点开始就开工的那一轮（Claude Code `--disallowedTools Edit,Write,NotebookEdit`，命令行还在；Codex 只读沙箱；别的 Harness 只有任务说明里那句话）；`proxy` 是代答话题的一轮（Claude Code `--tools Read,Grep,Glob,Bash,Edit,Write`、不挂 MCP，PreToolUse 钩子 `bin/covey_tool_guard.py` 只放行只读的 lark-cli 命令和写联系人笔记目录；Codex 照旧，沙箱里 lark-cli 读不了聊天）。都没有时一轮和以前一样。
- 提示词（`app/prompts.py`，`app/runs.py` 组装）：桥接规则同一话题每轮相同，放在用户消息里，`request.instructions` 为空；续接只给最新消息和 `RESUME_REMINDERS`；主人的原则由 `principles_block` 加入。`system_instructions` 通道保留，现在没有后端声明它。
- 后端继承 `BackendRuntime`。启动检查（`on_startup`）只由 worker 经 `Harness.ensure_started` 跑，失败后 `STARTUP_RETRY_SECONDS` 内的消息直接拒绝。模型目录由 Bridge 后台刷新到 `launch-models/<名字>.json`，点卡片时不探测后端。

### 后端名单

- `core/roster.py` 的 `HARNESSES` 是唯一的后端表（核心表 `CORE_HARNESSES` 加上启用的[扩展](#扩展)的项），每行：`module`、`store`（会话存储类）、`live_questions`（等回答时回合保持打开：Claude）、`paused_questions`（中断回合、带回答续接：Codex）。派生的 `NAMES`（第一个是默认路由）、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 供路由和问答证明使用。
- 模型从哪里来是供应商，不是 Harness：`core/providers.py` 的 `PROVIDERS` 记供应商属于哪个 Harness、模型 ID 前缀和登录在哪，`provider_of` 按前缀认出供应商。
- `RETIRED`、`RETIRED_PREFIXES` 记已下架的 Harness 和供应商；带这些前缀的模型由 `core/model_settings.py` 在入队前拒绝（[已下线的功能与旧配置](legacy.md)）。

### 扩展

不是每个部署都要的功能放在核心之外的 Python 包里（`core/extensions.py`，只用标准库）。没有启用的扩展时，名单、供应商表和功能列表与没有这个机制时完全一样。

- **启用**：环境变量 `COVEY_EXTENSIONS`（逗号分隔的包名；设成空串表示一个都不要），没设时读仓库根的 `extensions/enabled`（每行一个包名，`#` 起注释，空行忽略；文件不存在就没有扩展），再按数据根的 `extensions/enabled` 加减：`+包`（或只写包名）启用、`-包` 停用，同一个包以后一行为准，加的排在 release 的后面。release 的名单随部署换，数据根这份是这个部署自己的选择，部署不动它。
- **装和卸**：`python3 -B bin/covey_plugins.py list` 看找到了哪些扩展、哪些启用、来自哪一层；`enable <包>`、`disable <包>` 只改数据根那份名单，写之前按 Bridge 的方式加载新名单（manifest、Harness 和供应商合并、卡片 kind），加载不了或冲突就拒绝、什么都不写；`doctor` 检查现在的名单能否一起加载。改完 `covey_service.py restart`（或下一次部署）生效，命令本身不启停任何东西。设了 `COVEY_EXTENSIONS` 时它盖过两份文件，命令会提示。扩展包本身随 release 带着（`scripts/assemble_release.py` 把扩展库里每个带 manifest 的包都放进 `extensions/`），所以启用一个包不用改核心代码，也不用改服务清单。
- **查找**：`COVEY_EXTENSION_PATH`（`os.pathsep` 分隔的目录），没设时是仓库根的 `extensions/`。有启用的包时才把这些目录幂等地放到 `sys.path` 前面。
- **`<包>.manifest`**：只放纯数据（`dict`、`list`、`str`），不导入 `covey`（它会被扩展的隔离解释器里的 `core/roster.py` 间接导入，也要避免循环）。可选的几项：`HARNESSES`（名字 → `module`、`store`、`store_module`、`live_questions`、`paused_questions`、`after`，字段同 `RosterEntry`；`after` 是它在 `NAMES` 里排在哪个 Harness 之后，启动卡和换配置卡的 Harness 顺序跟着它，不写就接在最后）、`PROVIDERS`（字典列表，字段同 `Provider`）、`RETIRED_PREFIXES`、`RETIRED_HARNESSES`，以及核心读的几张小表：`WORK_MODES`（模式 ID → [名称, 说明]）和 `DEFAULT_WORK_MODE`（`core/defaults.py`）、`AGENT_DIRS`（Agent 在工作区里放工作副本的隐藏目录，`app/artifact_policy.py` 不上传）、`IGNORED_ENV`（不再读取的变量 → [Harness, 替代选项]，`core/ignored_env.py`）。`core/roster.py`、`core/providers.py` 的纯函数 `merged` 把它们接在核心表后面，`NAMES`、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 从合并后的表派生。
- **`<包>.features`**（可选）：`features(app)` 返回功能对象的列表，形状同 `app/unified.py` 的 `self.features`：可有 `threads()`、`menu_items()`、`menu_folds()`；排在核心功能后面。
- **钩子和设置**（`core/extensions.py` 的 `HOOK_NAMES`、`SETTING_NAMES`）：manifest 的 `HOOKS`（钩子名 → 包内模块的点分名）让核心在需要时才导入那个模块：`proxy_sender`（`app/proxy.py`：代答发送器，有 `Sender`、`ProxyLogin`、`tool_dir`、`send_problem`）、`service_hosting`（`service/install.py`：`MODES` 模式名 → `install(root, args)`，`add_arguments(parser)`，可选 `auto_mode()` 适用时返回模式名，给 `--mode auto` 用）、`deploy_check`（`service/plugins.py` 的 `deploy-check`：`check(harness_config)` 返回 `{'problems': [...], 'notes': [...]}`，在要部署的 release 里跑，`scripts/deploy_bridge.sh --ext-repo` 的预检有问题就拦下；每个启用的包都问，用 `extensions.hooks(名字)`，不只取第一个）、`claude_turn`（`backends/claude/bridge.py`：包裹单轮 Claude Code 的上下文管理器 `turn(ctx, env, run_id=...) -> (ctx, env)`，在 `run`、`compact_session` 和 `handoff_summary` 外层执行；若写入 `SETTINGS_ENV` 的 `ANTHROPIC_BASE_URL`，脱敏中继自动串在其内层）。`SETTINGS`（名字 → str）给核心几处写死过的部署细节：`web_tenant_domain`、`contact_notes`、`contact_notes_dir`、`contact_notes_hint`、`invite_url`（别人私聊时那句话里的安装说明链接，`app/strangers.py`），也可以放包自己的 `<包名>.<名字>`（`setting` 照读，扩展自己用）。`extensions.hook(名字)` 没有扩展提供时是 `None`，`extensions.setting(名字, 默认)` 给默认值。没有发送器时不起代答后台线程、没有菜单项和设置卡标签；没有 `service_hosting` 时安装器只有 `--mode systemd|launchd|manager|auto`（见 [服务托管](operations.md#服务托管)）。
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
- 卡片（`cards/switch_form.py`）分级：Harness、供应商、模型只改记录里的 `selected_model` 再整卡重画；其余项记录后重画。独立 worktree 在卡的 worker 后台线程里建（`made_worktree`）。
- `switch_topic` 在话题空闲时于 `LOCK` 内改 `topic.json`：`binding_revision` 加一，清掉新 Harness 的会话指针（每次换都开新会话），`switches` 追加一条（`from` 记旧会话、会话文件、工作目录和是否脱敏 `redaction`；换时还有一步 ↩️ 回退没被下一轮接走的，再记 `rewind_keep`，回退标记 `read_marker` 也在 `app/binding.py`），旧绑定的 `pending` 问题转 `cancelled`。只改脱敏时版本和会话都不变。
- 新 Harness 首轮由 `SwitchHandoff.switch_handoff` 写 `<话题目录>/handoff/transcript-r<版本>.md`，提示词只给路径。“首轮”到新绑定有一轮带着可续接的会话结束为止（`settled`：本话题有 `binding_revision` 等于当前版本、有 `session_id`、`native_resume_allowed` 为真的任务记录，即 `turn_settle` 记下会话指针的那种结束；看到一次就写 `handoff/settled-r<版本>`，之后任务记录被清掉也不变）。之后同一绑定再开新会话（会话指针因失败被清、↩️ 回退了会话的第一轮）都用任务自己的 `history_snapshot`，不再带交接说明、摘要和“本话题刚从…更换”。安全拒绝后的重试（`job.policy_retry`，`Runs._turn_message`）也一样：它的快照已去掉被拒的那轮，交接会把那轮带回来。不用历史里有没有新回复来判断：失败、停止的一轮也写回复行，还没开跑就停下的首轮会因此丢了交接。
- 交接摘要：旧绑定有会话、它的后端有 `handoff_summary`（只有 Claude，`backends/claude/handoff.py`）时，`SwitchHandoff.summary` 经 `Topics.apps` 找到旧绑定的 app，按 `from` 的模型、预算、工作目录调它（本轮的 `cancel_event` 传进去），结果写 `handoff/summary-r<版本>.md`，同一版本再跑首轮时直接读文件；有摘要时原文只给 `SUMMARY_ROUNDS`（1）轮，没有时 `HANDOFF_ROUNDS`（3）轮，每条由 `capped` 截到 `MESSAGE_CHARS`。`from.redaction` 或话题现在的脱敏为真时经中继（读不出不做）。没有会话、后端不支持、`from` 没有 `redaction`（之前换的）、有 `rewind_keep`、原目录不在、本轮会被拒（`ready`：新工作目录不在、脱敏拒绝）、失败或超时都退回 3 轮，日志一行 `switch summary …`。旧会话不能再续接时也不分叉它（`refused_session`）：这个会话最近一轮任务的记录有 `policy_retry` 或 `policy_stop`（被安全拒绝停过，或是拒绝后换旧模型的重试，分叉会把被拒的对话再发给拒绝它的模型），或 `session_is_blocked`（`core/session_guard.py`，读不出也算）为真；`switch_topic` 记的是会话指针，策略守卫停下的会话被封但指针还在。这几种在任务卡上各有一行说明（`REFUSED`，按原因：`policy_stop` 的 `kind` 是 `refusal` 写“被安全拒绝停过”，是 `fallback` 写“因官方模型降级停过”，只有 `policy_retry` 写“是安全拒绝后换旧模型的重试”，会话被封写“已禁止续接”），然后退回 3 轮。`ready` 在 `refused_session` 之前看：本轮会被拒时不写这行，免得说原文交给了不会开跑的新模型。等摘要时任务卡执行过程里有一行机器人说明（`WAITING`），之后改成结果；等摘要时点了停止或收到 SIGTERM，说明改成“已停止，没有写交接摘要”（`STOPPED`），`Runs.generate_reply_for_worker` 在开跑前看到 `cancel_event` 就抛 `JobCancelled`，新 Harness 不启动，这一轮按停止或中断结束；中继统计里摘要单独一行（`run_id` 为 `handoff-r<版本>`）。
- 消息恰好在换配置时到达：`reserve_submission` 抛 `HarnessChanged`，`BridgeApp.handle_event` 按新绑定再路由一次。

### 回退一步

- 入口见[回退一步](usage.md#回退一步)：capability `rewind` 为真的 Harness（Claude）。点击由 `app/rewind.py` 的 `RewindCards` 在 EARLY 表里接住，不转给任务进程；记录在 `<state>/rewind-forms/`。
- Claude Code 回放本轮提问时，`Rewinds.prompt_read` 在任务记录写 `prompt_uuid`、`prompt_session`；能回退的只有当前会话最新一个有这两项的任务，且话题空闲。
- `backends/claude/rewind.py` 读会话文件找到保留条目和文件快照；先回退文件（失败则什么都不改），再在话题锁内移出最后一轮历史、取消待答问题、写 `rewind-next.json`。下一轮用 `--resume-session-at` 接上，不压缩。

### 敏感词脱敏

- 开关是 `topic.json` 的 `redaction`，词表在 `<state>/redaction/config.json`（`core/redaction.py`），每轮开始时读。只有 capability `redaction` 为真的 Harness（Claude）能跑；`app/runs.py` 在 `backend.run` 前兜底。
- 每轮（`backends/claude/redaction.py`）在 127.0.0.1 起一个只活这一轮的中继，`--settings` 把 `ANTHROPIC_BASE_URL` 指向它，所有请求都走中继。不能脱敏就不发：出错抛 `Unavailable`，本轮 `redaction_failed`，Claude Code 不启动。
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
- **toast**：卡片回调的应答在同一帧里回 `{"toast": {"type": "info", "content": "收到"}}`（常量 `TOAST_TEXT`）。应答前只做解析和入队，点击在卡片的消费线程（`cards`）里走原来的 `CardLanes.submit`；SDK 的回调线程同时跑心跳，不做重活。转换失败时不回 toast。
- **队列与线程**：每个 key 一个有界队列（256），由该 key 自己的消费线程取（和 lark-cli 时一样逐个处理，慢消息不拖连接）。第一个 `consume()` 调用起 `event-websocket` 线程，在线程里才导入 `lark_oapi`、取凭据（`feishu/sdk.py` 的 `client()`，只在内存）；断线重连由 SDK 自己做，客户端抛出（如连接数超限）时线程 20 秒内重来。Bridge 停止时 `stop_children` 关连接。
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
- 话题里任务在跑时发来的消息（`COVEY_TOPIC_STEER`，默认开，`core/deliveries.py` 的 `topic_steer`）：`BridgeApp.handle_event` 照常写历史、`enqueue_chat_job(steer=True)`；`reserve_submission` 在 `LOCK` 内绑定目标（`steer_target_job_id`，前面最近的那个），`steers_on_arrival` 判断要不要马上送：开关开着、后端声明 `steering`、不是管家一家（`Traits.lists_supplements`，管家走自己的 `Butler.supplement`）、目标已开始（在等回答、正在收尾或已经结束的也试，好在卡上说原因）。要送的排队卡状态行先写“正在补充到 #N”，出了 `topic_lock` 后 `Steering.steer_on_arrival` 走和点击一样的 `steer_queued_job`（`auto=True`）：同一个 `queued-steer-<消息 id>` 送达记录，回执 `receipt` 带 `auto`（`on_arrival`），`requeue` 带发送人。没发出（`SteerRefused`、目标在等回答或已结束）这条照旧排队，卡上一行原因、状态行改回 `queued_line`；可能已送达的照规则 6 不单独执行。消息处理是一条线程按收到的顺序做的，连着发的几条各自送、按顺序。
- 自动送进去的补充确定没被读到（`held` 后 `refused`，或 `target_ended`）时和管家的一样标记，只是标 `requeue_next`（`core/deliveries.py` 的 `marked`；老版本把每个 `requeue_auto` 都当管家的补充，回退到老版本时它们就只留回执上的“重新排队”）。`settle_steer`、`end_unread_steers` 里先标再画回执，由 `queue_lost` 经 `_queue_marked` 用 `_requeue` 排成新一轮：消息 id 是 `requeue-<token>`（原消息 id 已是那张排队卡的任务），上下文是话题的（`requeue_context`：topic.json 的 `root_id`，话题换了 Harness 就不排、回执留“重新排队”），只排一次（`requeue_state`）。任务进程只标记，结束后 `app/detached.py` 的 `queue_lost` 排（开关开着时话题任务也排）；Bridge 不在时由下次启动的 `queue_stranded` 排：只读一遍记录，管家的交给管家所在的 Harness，话题的交给话题所在的 Harness。主人停止的一轮不自动排；可能已送达的不排。
- 排队卡的补充按钮写“现在补给 #N”（`cards/streaming.py` 的 `steer_label`，管家的分档卡照旧“补充”）；卡发出时 `StreamingReply.steer_label` 记下这个字样，任务开始后第一次写状态行时（`Cards._start_top` 的同一批，或单独写一次 `relabel_steer`）改回“补充”。
- 回执卡的按钮由 `offers` 决定；“重新排队执行”每条只排一次（`requeue_state`、`requeued_job_id`）。
- 话题里同时有在跑和排队的任务时，管家转话（`topic_send`、`project_send`）和网页上说的话都交给在跑的那个（`app/runtime.py` 的 `supplement_target_of`：已开始的里最新的，没有才是排队的里最新的），回执写实际收到它的任务号；只有排队的任务时并进它的请求，回执写“并进排队中的任务 #N”（不能只交给排队的：在跑的那个会一条也收不到）。
- 没读到的补充（`app/steer_interrupt.py`）：持有任务后端的进程（任务进程，或 Bridge 自己跑的任务）在补充 `sent` 后起一个 `steer-unread` 线程，送达后 `URGENT_SECONDS`（20 秒，`COVEY_STEER_URGENT_SECONDS`）和 `REPAINT_AT` 各看一次：还没读到就重画回执（“已等 N 秒”）；第一次看时补充是急的（`core/steer.py` 的 `is_urgent`），就调后端的 `stop_command`。点“打断它，马上读”（`bridge_steer_interrupt`，按 `job_id` 转给任务进程）走同一个 `interrupt`。每条补充只试一次（`interrupt_at`；没东西可停时清掉，可以再点），记录里留 `interrupt`（结果的话）和 `interrupted`（停掉的命令）。
- `stop_command`（能力 `command_stop`，只有本机的 Claude）：从事件里取主任务正在等的前台 Bash 调用（不含后台命令和子任务的命令），在 Claude CLI 的子进程里按命令行找到它的 shell（`backends/claude/command_stop.py`），先送一条说明（为什么命令被停）再停它的进程组（SIGTERM，2 秒后 SIGKILL，pidfd 防 PID 复用）。`core/steer.py` 的 `keeps_running` 列的命令（推送、部署、发布、安装、远程拷贝、改服务、删除和移动）从不停。
- 管家的补充没有回执卡：确定没读到的（`refused`，或 `target_ended`）标 `requeue_auto`，由 Bridge 用同一个 `_requeue` 以主人消息的 id 排成新一轮，同样只排一次（[butler.md](butler.md) 收尾时发来的消息）。

## 问题与回答

用户看到的见[回答模型的问题](usage.md#回答模型的问题)。`questions/<token>.json`，token 是 `sha256(任务消息ID-request_id)` 的前 16 位。`core/statuses.py` 的 `QUESTION_TRANSITIONS`：

```text
pending    → submitting, cancelled, superseded
submitting → answered, uncertain, cancelled
answered   → uncertain
uncertain  → pending, answered, cancelled
cancelled、superseded：终态
```

改动都走 `core/store.py` 的 `update_question`（`question` 锁内核对 `expected`，表外转换抛 `ValueError`）。组件：门面 `app/questions.py` 的 `Questions`，原生问题 `app/native_questions.py`，检查点问题 `app/checkpoint_questions.py`，卡片 `app/question_display.py`，表单到回答 `app/answers.py`，暂存消息 `app/held_notes.py`。

| 种类 | 后端 | 怎么交回答 |
| --- | --- | --- |
| 实时问题 | `LIVE_QUESTIONS` | 答案作为工具结果交回仍开着的回合：`control_lock` 内核对证明，写 `awaiting_ack` 再 `control.answer`；原执行进程不在了就不能提交 |
| 暂停问题 | `PAUSED_QUESTIONS` | `wait_for_answer` 中断回合，会话保护保持阻断；续答任务开始前核对正文已送达、回答没被消费过，写 `native_answer_once` 再续接 |
| 检查点问题 | 任何后端 | 本轮正常完成、检查点 `requires_user` 为真、没有未答完的原生问题时登记；正文送达后才展示控件，任务转 `waiting_user` |

- 正文没送达时不展示控件、不接受回答。只有一个待答问题且只有一题时话题消息就是回答，否则存为暂存消息，回答真正发送时才取走（`claim_held`），只发一次或不发。
- 提交在锁内转 `submitting` 并存答案，锁外发送，异常转 `uncertain`；“核查已保存回答”用保存的答案重交。原任务已结束时 `enqueue_continuation` 建续答任务（`after_ack` 后续）。
- 收起旧问题（`fold_older_questions`，只在 Bridge 做）：较新的问题卡显示后，更早的仍是 `pending` 的问题转 `superseded`，卡整卡重画成收起样式；来源任务还在的不收。等它的暂存消息改等新问题（`HeldNotes.wait_instead`）。
- 停止任务时本任务的待答问题转 `cancelled`。启动时 `recover_question_delivery` 把 `submitting`、`awaiting_ack` 记 `uncertain` 等用户核查，不自动重发。

## 撤回与编辑

主人撤回或编辑了已经发出的消息时，Bridge 跟着改（`app/source_watch.py`，`COVEY_SOURCE_WATCH=0` 关掉）。lark-cli 的事件总线既没有撤回事件也没有编辑事件，但消息读回来带着两个标记：撤回后 `deleted` 为真，编辑后 `updated` 为真、`update_time` 变新。所以靠读回。

- **登记**：`BridgeApp.handle_event` 把主人的一条消息变成任务或补充之后，记进 `Runtime.sources`（`core/source_book.py`，只在内存里）。只认主人（`Access.is_owner`）和 `om_` 开头的消息 ID（网页上发来的不算）；补充要能找到它进了哪个在跑的任务（那个任务的补充记录 `interaction-deliveries/`）。任务进程里不登记。
- **读回**：后台线程 `source-watch` 每 `WATCH_SECONDS`（15 秒）读一遍登记的消息（`GET /open-apis/im/v1/messages/<id>`，锁外）。任务还活着就一直读，结束后再读 `GRACE_SECONDS`（10 分钟），超过一天的不再读，最多同时登记 200 条。读失败下一轮再读；读回说消息已撤回（`deleted`，或接口拒绝时报 230011）就按撤回处理。
- **撤回**：
  - 消息自己的任务还在排队：取消，不执行；在跑：像点了停止一样停下（`app/project_clicks.py` 的 `stop_task`，任务进程活着时转给它）。停止带 `by: recall`（`core/jobs.py` 的 `RECALLED`），记录的 `cancel_requested_by` 是 `recall`，卡上写“原消息已撤回”，不回文字（那条消息已经没了，回不了）。
  - 是一条补充：再给那个任务送一条补充，说主人撤回了它、不要再按它做。
  - 不管任务结没结束：这一轮记下的记忆（回答卡上的“记下了”，同“不对”）和知识条目（`by=topic`、`source.job_key` 是它）作废；管家这一轮开的、分叉的、交过活的话题（`core/butler_opened.py`）各在话题串里收到一张小卡“来源消息已撤回”，话题还有排队或在跑的任务时带一个停止按钮（就是任务卡的 `bridge_job_stop`，带 `by: recall`）。话题不自动停（主人定的）。答案已经发出的不撤回。
- **编辑**：读回的正文和原来的不一样才算编辑（`same_words`，空白不计）。只看 `updated` 和 `update_time` 不够：消息下面开了话题串（管家把话题挂在主人那条消息下）时，飞书也把它标成更新过、更新时间变新，正文一字没改。真编辑时，改后的全文作为一条补充交给这条消息的任务（`Steering.deliver_steer`，补充 ID 是 `edit-<消息>-<update_time>`，同一次编辑只送一次）：排队中的并入它的请求，在跑的读到它的下一步。任务已结束、在等回答，或后端不收补充时，在话题串里（管家的私聊在聊天区）发一句“你改了这条消息……这一轮是按改之前的内容做的，没有重做”，不自动再跑。
- **被引用的消息**（`feishu/forward.py`）：回复一条已撤回的消息时，上下文写“已被发送者撤回”、不带它剩下的占位正文；编辑过的写“发出后编辑过”，正文是改后的。
- **限制**：登记只在内存里，Bridge 重启后，重启前的消息不再读回。还在等启动卡（`waiting_workspace`）的任务、管家已经交给话题的任务说明、管家会话里的原文都不跟着改；编辑后的消息只取文字，新加的图片和文件不带。

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

- **答案卡**：首发（`TurnDelivery.send`）和重投（`app/redelivery.py`）都经 `app/saved_send.py` 发同一张卡：同一个键、同一个位置（放置表的 `answer`），卡上有同样的产物、输入/输出灰框和“你问”块或状态行，所以重试时 argv 不变、飞书按 uuid 去重。首发在发之前把要发的卡记进 `answer-cards/<任务键>.json` 的 `outgoing`（锁内只写文件），重投按同一键取出原样再发（同一个键不配别的内容，否则回执按 `idempotency_conflict` 拦下），发出后再按上传状态改产物（`refresh_answer_cards`）。记下的卡带一个摘要（`digest`：卡和当时授权正文一起的 sha256），重投时用这次授权的正文重算，对不上或没有摘要（旧记录）就当没记下（规则 5）。没记下的（首发前任务进程就退了，或记录已过 7 天被清理）按任务记录和保存的执行过程重画；但这个键的卡已经发过（回执在）时不发重画的卡，记 `reconciliation_required` 停在 outbox，回复“可能已经送达”，不自动重发（规则 1、5）。以文件发的回答，文件之后跟一张产物与详情小卡，重投也一样。卡片可能已送达时不换格式；飞书明确拒收时改走 Markdown，同一段正文不会两条路线都可能送达。
- **暂停的问题和恢复原文**：问题正文（超过 `FILE_BYTES` 发文件）和问题控件失败后恢复原文，首发和重投都用 `app/saved_send.py` 的 `send_paused`、`restore_original`。问题正文只在没发任务卡的管家安静一轮发到顶层，其余回到它回复的位置；恢复原文先写回任务卡（重投没有任务卡可写），再发消息，最后发文件。
- **审核拒收**：飞书 DLP 拒收（230028，`audit_rejected`）时 `feishu/delivery.py` 按点名的类别打码（`mask_sensitive`），换 `<键>-masked` 发一次。单独发出的回答（`app/answer_delivery.py` 的 `_answer_apart`）打码后仍被拒时，`refused_as_file` 把同一段保存的正文作为文件发到回答本该在的地方（`Artifacts.send_result_file`，按文件版本只发一次），再发一行 `AUDIT_FILE_NOTE` 说明原因，任务卡不放回答的首句；只发保存的正文，不重跑模型（规则 5）。文件也没发出去时仍记 `audit_rejected`，不给按钮。
- **任务卡发不出去**：`StreamingCard.create`（`app/task_card.py`）里卡片消息被明确拒绝（`feishu/delivery.py` 的 `refused_outright`，不含可能已送达的）时，`_send_elsewhere` 换 `<键>-again` 原处再回复一次，再换 `<键>-top` 发到会话顶层；每次失败写进任务的 `last_card_error`。仍没有卡时 `job.card_missing` 为真，随交接的 extras 带给任务进程，状态行的每次更新（`Cards.update_card_activity` → `resend_missing`）每 `CARD_RESEND_EVERY` 秒补发一次，最多 `CARD_RESENDS` 次，补上后把 `card_id`、`card_message_id` 写回任务记录。放弃卡改发文字的（`replace_card`）不补。没卡的任务结论照常单独发出（`TurnDelivery._answer_apart`、`_fallback`）。
- **送达闸门**：`CheckpointQuestions.undelivered_jobs` 列出还要重投的任务，话题的新消息和换配置据此拦下。
- **幂等写**：`feishu/transport.py` 发请求前先写 `delivery-receipts/` 回执并 fsync；同一 UUID 配不同内容失败（`idempotency_conflict`）；不确定的写只在有限时间内用原 UUID 重试，之后返回 `reconciliation_required`，不换键盲发。写已有卡片时，前一次不确定后的重试收到 200770 或 300315 算前一次已生效。
- **传输**：开了 SDK（[飞书 SDK 与直连](configuration.md#飞书-sdk-与直连)）时由 `feishu/sdk.py` 发，请求由 `feishu/message_request.py` 从 lark-cli 的 argv 重建、逐字节相同，新旧进程共用回执；请求发出后不换传输。
- **重投**：在 `verified-results` 锁内核对任务号、运行 ID、代际和指纹，只读已授权的正文，不启动模型；放开锁再发。只有 Bridge 重投。
- **outbox**：`app/outbox.py` 定时扫描已授权、未送达、未暂停、到时的记录，从不运行 Harness；可重试的失败指数退避，用完转 `paused` 等用户处理。
- **自动重试**（`app/auto_retry.py`，见[自动重试](reference.md#自动重试)）按 `KINDS` 分限流、本轮未全部完成、重启打断三种，各有次数上限，相加不相乘。到点时锁内 `pending` → `submitting`，锁外重试，状态只按 `AUTO_RETRY_TRANSITIONS` 前进；唯一例外是官方拒绝的停止直接转 `cancelled`。官方安全拒绝后换旧模型重试一次（`app/policy_retry.py`，[policy-stop.md](policy-stop.md#换旧模型重试一次)）不走 `auto_retry_state`，另用 `policy_retry_state` 和 `POLICY_RETRY_TRANSITIONS`（同样两步），由 `policy_retry.tick` 经 `TaskControls.requeue(fallback=…)` 排上。
- **重跑还是拒绝**：什么都没保存的一轮按原快照重新排队；有保留候选的拒绝重跑；候选全部作废、从没授权过的（`withdrawn_unauthorized`）算没保存。各后端的投递不变量由 `test_backend_contract` 验证。

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

**总形状**：设备上跑一个单文件小程序（`core/device_shim.py`，协议见上面的[设备端小程序](#设备端小程序)），它起并看守 Agent、把输出写进只追加的 journal；hub 经 ssh 的一条主连接（`core/device_transport.py`）和它说话。后端不知道自己在设备上：`core/process.py` 的 `spawn_process` 在上下文带 `device` 时返回 `core/device_link.py` 的 `DeviceProcess`（像 `Popen`：stdin、stdout、`poll`、`wait`、信号），Claude 的 `ClaudeProcess` 和 Codex 的 `AppServer` 照常读写管道。链路后台线程按字节偏移 `attach`、丢已见的帧、断线退避重连，请求账本和偏移存在 `runs/<任务键>/device.json`，所以重连和 runner 重启都不重放已投递的帧。`别名:路径` 在 hub 上解码成一个永远不存在的占位目录加 `device`、`remote_workdir`（`bind_workdir`），没改到的代码路径因此明确失败，不会碰到本机目录。

**设备门**：一个设备话题的每一轮要依次过下面几道，哪一道不过都是当场拒绝并在卡上写原因，不排队、不改到本机跑。
- **开关与名单**：`devices.enabled()` 和 `devices.json` 里有这个别名；开关关着时 `gpu1:/x` 只是一个普通（相对）路径，一切同旧。
- **谁能用、哪个 Harness**：`Access.allows_device(chat, device)`（规则 11：谁被服务只经 `app/access.py`）和设备声明的 `harnesses`；启动卡、换配置卡只列能用的设备，点击和每一轮开始前各再查一次（`app/runs.py` 的 `_device_refusal`）。
- **目录和可用性**：设备经 `stat` 答复目录在不在（5 秒，设备的话，不是 hub 的 `Path.exists`）；`app/availability.py` 读设备最近的 `hello` 和自检结果，不在 hub 上找命令。
- **能力位**：`core/harness.py` 的 `for_device(caps, ctx)`（`DEVICE_OFF`）在设备上关掉 `native_fork`、`compaction`、`turn_export`、`redaction`、`rewind`、`command_stop`（补充打断命令要在本机找命令的进程）；各入口（回退、下载、压缩、分叉、启动卡的脱敏选项）都经 `backend_capabilities` 取值，所以按钮不出现、不是出现后点了才拒。几处没有 `pid` 的地方有闸门：Claude 的轮前压缩、`ClaudeBackend.compact_session`、管家的空闲压缩和压缩子进程在设备上直接返回。话题开着脱敏时设备上的这一轮拒绝，不会不脱敏地发出去。
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

**自检的步骤**：5 秒 ssh（BatchMode）→ 推设备端程序（声明了 Codex 就再推 `workspace_reader.py`）→ `hello` → 导入环境档案（来源依次是设备 `~/.claude/settings.json` 的 `env`、设备进程环境里的代理变量、`login_shell: true` 时登录 shell 的变量，`devices.json` 的 `env` 最后覆盖）→ 对声明的每个 Harness 真跑一次最小请求（`claude -p`、`codex exec`，经设备端的 `exec`，带档案里的环境，限 60 秒）→ 写 `profile.json`。连不上模型服务时，档案里没有代理就说去哪里设 `HTTPS_PROXY`，有代理就说代理不通。结论写进 `profile.json` 的 `check`，设置卡的设备状态行和 `service/doctor.py` 的 `devices` 读同一份。

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
- **告警卡**：内存和 OOM 告警列内存占用最多的进程（`top`），发到第一个能对上任务的话题；盘满和写不进的告警不带 `top`（进程内存和谁占了盘无关），改列增长和按这个速度多久写满、写入最多的进程（`write_bytes` 是所有盘合计，含网络盘）、大目录（`cards/node_cards.py` 的 `disk_sections`），发到私聊。
- **不变量**：关着时不读不写任何文件、不起线程；节点不监听端口、不读别的环境变量；没有记录锁被跨网络持有；告警只发一次，恢复只改原卡。

## 管家同时做几件事（默认关）

使用者看到的行为见[管家 · 同时做几件事](butler.md#同时做几件事开关默认关)。

目标：管家这一轮在做事时，主人的下一句不被挡在后面。做法是把管家变成一个**话题家族**：槽 0 是现在的管家话题（`butler_topic_id`），再加固定 4 个槽（`track_topic_id(chat, 1..4)`，同样是 32 位十六进制）；每个槽是一条“路”，路里的一轮是普通的管家任务。调度器不用改：`core/scheduler.py` 按 `(chat, topic)` 排队，不同槽并行、同槽先进先出。

底座（`core/butler_tracks.py`、家族谓词、`family_live`）：

- **开关**（`core/butler_tracks.py`）：`COVEY_BUTLER_TRACKS=1` 开；`COVEY_BUTLER_TRACKS_MAX` 是同时在跑的件数上限（默认 3，取值 2 到 4）。见[配置 · 环境变量](configuration.md#环境变量与选项一览)。
- **家族谓词**（`core/butler.py`）：`is_butler_topic` 开关开着时认槽 0 到 4（这些事的话题也是管家：工具只认管家任务、话题清单和看板排除、一轮的规则和卡片照管家），关着时只认槽 0，和原来一字不同；`is_butler_root` 只认槽 0。哪些调用点按家族、哪些只认根，改的时候看调用处的注释。`track_of(topic_id, chat)` 给槽号。
- **分叉点**：这些事的会话是槽 0 会话的分叉。`ctx.options["fork_session"]` 带 `ctx.options["fork_at"]` 时，Claude 后端加 `--resume <会话> --resume-session-at <fork_at> --fork-session`，分叉只含到那一条为止的内容；槽 0 正在跑的那一轮（提问已写进记录）取它提问的父条目，分叉里就没有这一轮的半截。
- **槽话题的 `topic.json`**：`Butler.keep_track_topic` 从槽 0 复制模型、思考预算、工作区、脱敏等设置，加 `track: k`；管家的设置变了，下次调用时同步过去。
- **存活查询**：`JobRegistry.family_live(chat)` 给家族里没结束、不在等回答的任务（只读内存），用来数件数。

**运行时**（`app/butler_tracks.py` 的 `Tracks`，`Butler.handle_event` 在原路径前调 `Tracks.handle`）：

- **开不开**：`COVEY_BUTLER_TRACKS=1`，并且管家允许、模式是管家（`Tracks.enabled`；`core/butler_tracks.enabled` 只看环境变量）。关着时 `handle_event` 一字不变；开着但家族里没有活任务，`handle` 回 False，也走原路径（槽 0，不分叉，不判断）。
- **活任务**：本进程登记表里家族的没结束的任务（排队、开始、在跑、在等回答），加上话题记录里 `waiting_user` 的（6 小时内；槽 0 的只在别处有活任务时才算）。每个交给路由层一条 `LiveJob`。
- **认引用**：`parent_id` 先查补充送达记录（`merged`），再看是不是某个活任务的主人原消息、卡片或回复目标（`live_card`），再查别名（槽里有在等回答的任务时当作它的问题卡；其余是 `ended`；谁也不认识是 `unknown`）。
- **分流**：`core/track_route.py` 的 `route` 九条规则；`ask_judge` 时调 `app/track_judge.py` 的 `judge`（处理事件的后台线程里同步等，不持锁），判完重读活任务，目标刚结束就新开一件。
- **小模型的待命进程**（`app/track_standby.py`，`COVEY_TRACK_JUDGE_WARM`，默认开）：一次性的 Claude Code 每问一次要先花约 1 秒起命令行，第一个请求也比后面的慢（一次实测：冷起 p50 约 3.4 秒，其中约 1.2 秒是起进程，其余是模型），过不了硬上限（`COVEY_TRACK_JUDGE_MS`，默认 3 秒）。所以 `Tracks.handle` 一收到管家消息（家族里有没有活任务都一样，这条消息可能就是下一条要比对的任务）就调 `track_judge.warm`：起 stream-json 模式的 Claude Code，先用一条虚构的消息预热（它的第一个请求和连接就热了，里面没有主人的话），再等在标准输入上；`judge` 用 `take` 取走、写进这一问、读答案，用完杀掉进程组，每个进程只回答一个问题，上下文里没有别的消息。另有三处做法：

- **取走时就起下一个**：`judge` 取走待命进程的同时调 `warm` 补上，不等答案回来；以前要等到 `Tracks.decide` 判完才补，这一问还在等时来的另一条消息（别的聊天、或同一聊天的下一句）拿不到待命进程，只能现起一次性进程（本机实测 5 秒多，超过硬上限）。
- **同时问两个**（`COVEY_TRACK_JUDGE_HEDGE`，默认 2，取值 1 到 3）：`warm` 多留几个待命进程，`judge` 把同一问同时交给它们（`track_standby.ask_first`），第一个解析得出的答案算数，别的立刻杀掉；都失败时报第一个失败的原因，都超时就是超时，照旧按新开一件。网关的慢请求是各请求自己的，两个里总有一个躲开；代价是每次判断多一个几十 token 的预热请求和一个小请求。1 就是只用一个，和以前一样。
- **输入变小**（`COVEY_TRACK_JUDGE_SLIM`，默认开）：Claude Code 命令行多带 `--setting-sources user --disable-slash-commands`，不再把工作目录一路向上的项目设置、记忆文件和技能清单放在每个问题前面，输入约 800 token（以前约 4800）。凭据不变，仍是 Harness 自己的环境和登录；Claude Code 不认这两个选项时记住、之后不再带。

拿不到待命进程（还在起、已经死了、argv 变了、开关关了）就照旧现起一个一次性进程，日志行格式不变（`via=claude`），也不写主人的原话。空闲 10 分钟（`IDLE_S`）、超过 30 分钟（`MAX_AGE_S`，下次 `warm` 换新的）、超过硬上限、Bridge 停止（`stop_children` → `Butler.close`）和解释器退出都杀它们；`COVEY_BUTLER_TRACKS` 没开、模式不是管家、通道不是 `claude`、`COVEY_TRACK_JUDGE_WARM=0` 时不起任何进程。

结论：同时问两个待命进程时，判断大多在 3 秒硬上限内；上限设到 2 秒会有约四成超时。下限在网关：一个很短的小模型请求本身就要 1.5 到 2 秒。

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
- **开关**：`COVEY_BUTLER_TRACKS` 关着不写账本、不加这一块。

管家的 Harness 是设置里的 `harness`（空是 Claude Code，`core/butler.py` 的 `HARNESS`），只用 `bridge_tools` 为真的 Harness（Claude Code、本机上的 Codex 和 Antigravity，见 [butler.md 换 Harness](butler.md#换-harness)）。选的那个没启用或接不了工具时，私聊消息照旧开话题，Bridge 启动日志写一行 `main thread is on but its Harness <名字> is not enabled or cannot take the Bridge tools`。

## 专项（第 2 期，开发中，默认关）

专项是管家交给一个隐藏的负责人话题、由它开话题并跟进、要分成几个话题跨几天做完的事（设计见第 2 期的专项设计稿）。**现在有底座、负责人的运行、专项卡和入口**：记录、角色、看板的格，负责人话题一轮怎么跑，聊天区的专项卡（[专项卡](projects.md#专项卡)），以及三个入口——派发卡转成专项、管家直接开、不要专项降级（见下）；续轮、问题分流是后面的包；`COVEY_PROJECTS` 不是 `1` 时下面的谓词全为假、放置表和看板和原来一样，也不建任何记录。

- **记录** `<state>/projects/<id>.json`（`core/projects.py`，只用标准库，MCP 服务会导入）：`id`（6 到 32 位 `0-9a-z`）、`chat_id`、`name`、`goal`、`state`、`lead_topic_id`、`threads`（专项的话题 id）、`origin`、`steps`（升级逐步记账，重做时跳过已做的）、`card`（专项卡的 `card_id`、`message_id`、`sequence`）、`created_at`、`updated_at`。给第 3 期留的（旧记录里的 `direction` 没人读也不画，新记录不再写）`leader`、`claims`、`agreements`、`place` 只写不读。读写在 `resource_lock("record", …)` 下，锁内不发网络请求（规则 1）。
- **状态** 只按 `PROJECT_TRANSITIONS` 前进（规则 4）：`active` → `paused`、`done`、`cancelled`；`paused` → `active`、`done`、`cancelled`；`done` → `active`（只在主人又在专项里说话时，见下文“收尾与重开”）；`cancelled` 是终态。`update` 改状态前先查表，不合法抛 `ValueError`、什么都不写。
- **角色** 写在话题自己的 `topic.json` 里：`project: {id, role}`，`role` 是 `lead` 或 `thread`（盘上的值不改，代码里叫 `MEMBER`；`core/topics.py` 的 `set_topic_project`、`clear_topic_project`、`topic_project`）。读 `project.role` 一次 `stat`，文件变了才重读（`core/projects.py` 的 `topic_project`，写入换 inode，所以缓存立刻失效）。
  - **负责人**（`lead`）：话题 id 是 `lead_topic_id(chat, pid)`（`sha256("project-lead\0<pid>\0<chat>")` 取 32 位十六进制，不和管家、同时在做的事的 id 相撞，也不是任何消息的别名）；`is_lead_topic` 还核对这个 id，别的会话里抄来的 `topic.json` 不算。它属于管家家族：`is_butler_topic` 认它，`is_butler_root` 不认；`Traits` 是 `LEAD`（管家关着时 `LEAD_OFF`），放置角色 `lead`（回复只在主人触发的轮里发到顶层，不回带，不放问题卡副本，不开串）。
  - **专项的话题**（`thread`，代码里 `MEMBER`）：还是普通话题（`Traits` 是 `TOPIC`），放置角色 `project_member`：答案留在话题串，结论和停止平常不回带到顶层（投负责人收件箱），只有到主人的问题才在顶层放副本；负责人在主人在场的一轮里交出去的那一轮例外，结论和停止照管家派出的话题回带（`relay_targets.awaited_target`，见 [projects.md 主人在等的结果](projects.md#主人在等的结果)）。
  - `core/placement.py` 的 `plan(state_dir, of)` 读 `topic.json` 的角色，所以转成专项时在跑的那一轮立即生效；传 `Traits` 行进去的人看不到专项的话题的角色（它不带话题 id），要传任务、记录或 `{"topic_id", "chat_id"}`。
- **看板** `core/board_read.py` 的 `groups` 收一个 `cells`（`read` 带，来自 `core/projects.py` 的 `cells`，没取消的专项的话题 → 负责人 id）：一个专项一格，格的键是负责人话题 id。同一格里最新的问题在等；在做的取先在跑再排队、再取最新（任务号是 Bridge 的全局计数，跨话题也能比）；结果取各话题最新一条里“主人没看过的最后结束的那条”，没有就取最后结束的。所以一条没看过的旧结果不会被一条看过的新结果盖掉，但一格只钉一个。手上的事、早晚汇总直接读任务记录，现在还不并格。
- **负责人的运行**（`app/project_lead.py`、`app/project_tools.py`，只在 `is_lead` 的话题里）：
  - **一轮** `butler_turn` 对负责人走 `lead_turn`：消息头是 `lead_rules`（身份、工具、风格、留给第 3 期专员的做法层 `PRACTICE`，分层拼接）加**状态包**（目标与完成标准、话题清单：状态、一句话、交付物，专项记忆（含近 30 天主人删掉的，别记回去）、资料库索引（`COVEY_PROJECT_MEMORY=1` 才有）、风格、自上次唤醒以来的收件箱、近 10 条决定），再是这一轮自己的消息。新会话给全部，续接的会话只给会变的部分（话题清单、风格、收件箱、决定）。
  - **会话只是缓存**：不做摘要压缩（`compact_butler` 跳过负责人）。上一轮 `input_tokens` 超 15 万、话题空闲超 24 小时、或换了模型，`worker` 的 `_start_turn` 经 `settle_session` 清掉会话指针，这一轮新开会话、状态包给全。
  - **工具**：只有 `Read`、`Grep`、`Glob`（`options["tools"]` 经 `backends/claude/argv.py` 变成 `--tools`；MCP 工具不属内置工具，不受影响）加 Bridge 工具 `topics_list`、`topic_read`、`topics_search`、`workspaces_list` 和 `project_*`。MCP 配置里有 `COVEY_PROJECT_LEAD`（专项 id）时服务只列这些，管家的列表一个字不变。`project_topic_start`、`project_topic_send` 走 `butler_topics` 的 `topic_start`、`topic_send`（负责人的任务调用时开出来的话题带 `project: {role: thread}`，写在绑定里，第一轮排队之前；同时在做的话题不设上限，风格里也没有“派活”档，旧记录的 `style.spawn` 照读不用）；`project_topic_send` 发给还不在任何专项的普通话题时，先用 `app/projects.py` 的 `join_topic`（`by: lead`）把它收进本专项再加话（`app/project_tools.py` 的 `take_in`；别的专项的话题、负责人话题、管家、代答话题和主人移出过专项的 `never_auto` 话题拒绝），负责人规则要它派活前先看专项里和私聊里已有的话题，能追加就追加；`project_topic_stop` 停专项里的一个话题（主人对管家说“停掉 T1”，管家用 `project_send` 交给负责人）：先用 `project_cards.stop_tasks` 停它排队和在跑的任务（转给任务进程，同停止专项），再用 `binding.retire_open_questions` 收掉它等主人回答的问题（`cancel_reason: lead_stop`，卡上写“负责人按你的意思停了这个话题”）并把等回答的任务记录记为 `cancelled`；话题仍在专项的话题清单里、算结束，只能停本专项的话题，专项已结束时拒绝；`project_note`（专项卡上一句，`report` 标要告诉主人的，按风格过滤）、`project_goal`（负责人写给主人看的目标和完成标准，记在 `lead_goal`，网页专项标题下显示它；记录的 `goal` 仍是开专项时的话，状态包里没写过就提醒一句）、`project_remember`、`project_style`、`project_finish`（还有话题在做时拒绝）写记录的 `notes`、`decisions`、`style`，`project_remember`、`project_style` 的记忆条目写进 `projects/<id>/memory.json`（见下面“记忆”）；`project_answer`、`project_escalate`（问题分流，`COVEY_PROJECT_TRIAGE=1`）只在开关开着时列给负责人，关着时直接调也只回答“问题分流没有开”（见下面的问题分流）。
  - **记忆** `projects/<id>/memory.json`（`core/project_memory.py`，只用标准库）：条目 `{id, text, kind, source, at, deleted}`，`text` 至多 200 字，`kind` 是 `decision`、`constraint`、`contact`、`fact`、`style` 之一（负责人工具旧名 `practice`、`pitfall` 记成 `constraint`），`source` 是谁写的（`lead` 等）。上限 40 条、6 KB，超了先丢最旧的墓碑、再丢最旧的非 `style` 条目；一行 `style` 是主人最后一次定的汇报风格。删不真删，写一条墓碑（`deleted` 是删的时间，文字留着），状态包列出近 30 天的墓碑，负责人的 `project_remember` 也拒绝记回去；墓碑 30 天后在下次写入时清掉。所有写都在专项记录同一把 `record` 锁下（`projects.update`），所以负责人的工具、卡上的“删”和 Bridge 自己记的不会互相覆盖。旧版把条目写在专项记录的 `memory` 字段里，卡读不到：读的时候两处合并，下一次写入把它们搬进 `memory.json`、记录里不再有这个字段，之后没有人再写它。专项卡、状态包、`project_remember`、`project_style` 都经这个模块。
  - **资料库与带上记忆**（开关 `COVEY_PROJECT_MEMORY=1`，要 `COVEY_PROJECTS=1`，默认关；细节在[专项记忆与资料库](projects.md#专项记忆与资料库)）：`core/project_library.py`（只用标准库）管 `projects/<id>/library/` 和 `library.json`；`app/project_context.py` 是 Bridge 一侧的接线：专项的话题开工的 `block`（经 `app/prompts.py` 的 `project_block`，新会话进 `bridge_rules`、续接的进 `_with_rules`）、问题卡回答记决定的 `remember_answer`、主人文件进资料库的 `owner_files`、话题每轮产物和交付进索引的 `turn_to_library`（由 `project_loop.after_runner` 在任务进程结束后调，任务进程自己不写专项的东西）。开关关着时这些都什么也不做、什么也不读。
  - **收件箱** `projects/<id>/inbox/<序号>.json`：`{id, kind, thread, text, at, taken_by}`，`add_receipt` 写、`take_receipts` 读并标上这一轮（上一个领它的轮没有好好结束的，下一轮重新给）。谁来写（专项的话题一轮结束、扣住的问题、主人在专项的话题里说话）是续轮的包。
  - **一轮的结语** 负责人的任务卡和列表预览说它做了什么（`butler_reply.outcome_words`，`lead=True`）：开了或交给哪个话题，否则记了哪句进展或收尾了专项，否则回复的开头。
- **专项卡**（`cards/project_card.py`、`app/project_cards.py`，细节见 [projects.md](projects.md#专项卡)）：`record["card"]` 是 `{card_id, message_id, sequence, created_at, generation, digest, wrote_at, closed, old, pending_card_id}`，只由 `ProjectCards` 经 `projects.update_card` 写（单写手，规则 3；sequence 存进这一个字段，不整条记录覆盖，免得盖掉负责人和升级同时写的别的字段）。`board` 钉专项卡时读这条记录的 `message_id`（`app/pins.py` 的 `aimed`/`project_aimed`）；格的名字（手上的事、早晚汇总）在 `core/topics.py` 的 `label_title` 里换成专项名。
- **转成专项、直接开、降级**（`app/projects.py`、`app/project_clicks.py` 的 `downgrade`）：
  - **转成专项** 开关 `COVEY_PROJECT_UPGRADE`：`off`（默认，什么都不做）、`butler`（管家 `topic_send` 带 `project`，由管家按话的意思判断；不再匹配主人话里的关键词，派发记录的 `upgrade_hint.long_word` 不再写也不再读，旧记录里留着不管；旧值 `word` 照认，意思同 `butler`）、`auto`（再加该话题的轮数 ≥3：`rounds_for_topic` 取该话题派发记录数和主人亲自发起的轮——任务的消息 id 以 `om_` 开头——两者的较大者，不读旧记录的 `rounds`）。触发点是 `butler_topics.dispatch_card` 在 `dispatch_cards.created` 返回后调 `env.projects.consider(token)`（`ButlerEnv.projects`；没放进 `dispatch_cards._created` 是因为那样 `dispatch_cards`、`butler_topics`、`project_cards`、`projects` 会互相导入）。静默进行，卡上有“不要专项”。活专项（`active`）已有 `MAX_ACTIVE`（3）个时不升级（`projects.active_count`）。
  - **七步**（`Projects.upgrade` → `advance`，每步成功后在记录的 `steps` 里记一笔，重做时跳过；每一步在记账丢了的情况下重做也无害）：① 记录，id 由派发卡 token 算出（`project_id`，所以同一张卡永远是同一个专项），名字取话题标题；② 负责人话题：topic.json 抄管家的模型设置（Claude Code、管家的模型和预算）、原话题的工作区，锁工作区，写 `project: {id, role: lead}`；③ 在原话题的 `topic_lock` 里 `set_topic_project(thread)`，记进 `threads`；④ 在 `resource_lock("card", card_id)` 下先把派发记录转 `settled`（`DISPATCH_TRANSITIONS`，`settled_by: upgrade`），再 `adopt_card`，sequence 从派发记录接着走（记录里已经是这条卡就不再 adopt，免得 sequence 退回去）；⑤ 同话题其余活着的派发卡写成“已并入专项 ›”并转 `settled`；⑥ `route_message`：卡和并入那几行的消息指到负责人话题；卡自己的别名也整个改归负责人（`move_topic_message`）：卡上按钮的值写的是负责人话题，点击按别名找话题，对不上就当别的话题的卡不理（`card_routing.py`，accept7 时升级卡的按钮因此全都点不动）；⑦ 写 `start` 回执（`add_receipt`，“接手一个已在进行的事”加话题、主人要的和现在的状况；先看收件箱里有没有，不写两次）。唤醒不在这里：`start` 回执由续轮的 `ProjectLoop` 去排。
  - **崩了怎么接**：`Projects.threads()` 给一个 30 秒一看的后台线程（`resume`），找 `steps` 没记全、60 秒没写过的记录再 `advance`，一直失败的退避到最长 10 分钟。进程内 `_busy` 防止两处同时跑同一个专项。`DispatchCards.write` 在拿到卡锁后重读记录，已经 `settled`（升级或并入了）就不写，免得盖掉专项卡。
  - **直接开** 管家 `topic_start` 带 `project: true`（`COVEY_PROJECTS=1` 即可，不看 `COVEY_PROJECT_UPGRADE`）→ `Projects.start`：记录（id 由请求算出，一次请求一个专项）、负责人话题（工作区取 `topic_start` 的 `workspace`）、`project_cards.created` 发卡（顶上“你问”块，读记录的 `ask`）、`start` 回执（主人的请求原文）；四步同样记在 `steps` 里，卡没发出去也由 `resume` 补。不开话题，由负责人去开。`topic_start` 的 `topics`（主人说要收进来的已有话题，T 号或话题 id）由 `wanted_topics` 在建记录前逐个查：找不到、是管家或代答话题、是负责人或已在别的专项里、超过 `MAX_JOINED` 个的记进 `origin.refused`，其余记进 `origin.topics`，这时步骤是 `JOIN_STEPS`（卡之后多一步 `members`，同整理专项建专项：只写话题的 `topic.json` 和记录的 `threads`，在跑的任务不停，等主人回答的问题不收；分流只管之后出的问题）；工具回复和 `start` 回执都逐个写挂上了哪些、没挂上的和原因。没有 `topics` 的仍是原来四步。
  - **管家工具** `projects_list`、`project_send`（`app/butler_projects.py` 的 `TOOLS`，只在 `COVEY_PROJECTS=1` 时列，负责人用不了）；`topic_send` 遇到专项的话题回一句改用 `project_send`（负责人自己给话题加话走 `project_topic_send`，不受限）。
  - **降级** 点“不要专项”→ `downgrade`：记录转 `cancelled`（`state_reason: downgraded`，之后 `ProjectCards` 把卡缩成“⏹ 专项已降为普通话题”）；停掉负责人排队和在跑的任务；专项的每个话题（含原话题）`clear_topic_project`（在 `topic_lock` 里，只清自己专项的）；卡消息和并入的几行的别名 `route_message` 指回原话题（直接开的指向第一个话题，没有话题就不指），卡自己的别名也改回去（`move_topic_message`）。每一步重做无害，所以一个降了一半的专项再点一次会接着降完。话题的任务不停，它们是普通话题了。
- **问题分流**（`app/triage.py`、`core/triage.py`、`core/project_gates.py`，第 2 期 P2-5；开关 `COVEY_PROJECT_TRIAGE`，见[配置](configuration.md)；做法、硬闸词表、实验 E2/E4 在[专项的问题分流](projects.md#专项的问题分流)）：专项的话题的选择题先交给负责人。`Questions.triage.route` 在 `NativeQuestions.show`（活问题）和 `CheckpointQuestions.deliver`（检查点、暂停的问题）发卡前调；扣住 = 问题记录的 `triage.state == held`，`status`、`display_pending`、任务记录的 `question_delivery_ok` 都按“没发卡但也不是没投出去”写，`recover_question_delivery` 跳过扣住的。回执和超时归 Bridge 的 `sweep`（任务进程只写自己这道题的记录，规则 8），`project_answer` 走 `submit_answer`，`project_escalate` 和超时走原来的发卡口子。状态只按 `TRIAGE_TRANSITIONS`（`held → answered_by_lead | escalated`）走（规则 4）；记录锁内不发网络请求（规则 1）。
- **续轮与护栏**（`app/project_loop.py`，第 2 期 P2-6；开关 `COVEY_PROJECT_AUTOWAKE`、`COVEY_PROJECT_BUDGET`，见[配置](configuration.md)）：
  - **谁唤醒**：`ProjectLoop`（功能对象，`COVEY_PROJECTS=1` 才起后台线程，启动后等 20 秒让 Bridge 自己先恢复被打断的任务，之后每 10 秒看一遍）对每个 `active` 专项：有没人读过的回执（收件箱里没被领、或领它的那一轮没好好结束，即 `take_receipts` 的判定）、负责人没有排队、在跑或等主人的任务时，入队 1 个唤醒轮——和负责人话题里的任何消息一样走 `enqueue_chat_job`，消息 id `lead-<专项 id>-<n>`（`n` 数在记录的 `wakes` 里；先记数再入队，丢一个下次再找，不会重复），消息内容只有 Bridge 的一句说明，状态包和收件箱由 `lead_turn` 自己加。`start` 回执总是唤醒（只要 `COVEY_PROJECTS=1`）；其余都要 `COVEY_PROJECT_AUTOWAKE=1`，关着时它们留在收件箱，等负责人下一次被主人叫起时一起读。重启后同一遍查找就把盘上的未领回执重新入队。
  - **回执谁写**：`thread_end`（专项的话题的一轮结束：一句话结论、状态、任务号）和 token 累加都由 **Bridge** 在任务进程退出后写（`butler_relay.after_runner` 的第一步调 `project_loop.after_runner`，所有任务结束的路径都经过它；规则 8 让任务进程只改本任务的卡和记录，写别的专项的收件箱和记录不算在内，所以放 Bridge 这边，旧任务进程也一样有回执）；`owner_said`（主人在专项的话题里直接说话）在 Bridge 收消息处（`app/app.py` `handle_event`）写；`stalled` 由循环写。`question` 留给 P2-5。回执一律经 `add_receipt`。
  - **主人触发的轮**：负责人话题里 message id 不以 `lead-` 开头的轮（主人引用专项卡回复、管家 `project_send` 转交、回答的续接）。它入队时（`submission.enqueue_chat_job` → `project_loop.owner_turn`）把 `wakes` 的连续计数和最近一小时的记录清零、清掉停滞标记，并把因护栏（`tokens`、`wakes`、`stalled`）或主人点了停止专项而停住的专项恢复成 `active`；`tokens` 停住的从已用的数目重新开一份预算（`token_floor`），否则下一遍就又停了。主人在专项的话题里说话（`owner_said`）也清连续计数。
  - **Bridge 自造的消息 id**：唤醒轮的 `lead-<pid>-<n>` 和管家转交用的别名 `project-lead-<id>`（`projects.own_message`）在飞书里没有：回复不引用它（`core/topics.py` 的 `context_for_topic` 把 `reply_to` 留空，答案是聊天区的新消息），答案顶上也不画“你问”块（`app/answer_cards.py` 的 `asked`）。accept7 时转交那一轮的卡先回复别名两次被拒，才改发到顶层。
  - **护栏**（全是机械检查，数字由主人定）：专项 `paused` 且 `state_reason` 写 `tokens`、`wakes`、`stalled`，之后不再唤醒，专项卡的“负责人”一行写“停住 · 原因”（`projects.reason_words`）。`COVEY_PROJECT_ASK=1` 时还发一张停住卡问主人继续、收尾还是停（见[专项停住后问主人](projects.md#专项停住后问主人)）；`COVEY_PROJECT_STALL_SECONDS` 能调小停滞的时间。
    - token：负责人和各话题任务的合计（任务的输入含缓存加输出，子任务算在内）每个任务结束时累加进 `tokens_used`，每个任务只算一次（`token_jobs` 记键），专项卡上写“token N”；只在部署设了 `COVEY_PROJECT_BUDGET`（旧名 `COVEY_PROJECT_TOKENS`）时到了才停，默认没有上限（主人定的：不按 token 总量停整个专项，也不请停话题、不发停住卡）。
    - 唤醒：无人介入连续 30 次，或最近一小时 12 次，停。
    - 停滞（要 `COVEY_PROJECT_AUTOWAKE=1`）：只管**有活没动**。专项 `active`，还有负责人能推动的事（负责人或话题有排队的任务、有没读的回执，`project_loop.movable`），没有任务在跑，距上一次动静超过 30 分钟，写一条 `stalled` 回执（唤醒负责人提醒一次）；再过 30 分钟仍没有动静就停。“动静”是话题任务结束、主人触发的负责人一轮结束、收件箱来了 `stalled` 以外的回执、专项开始；唤醒轮自己不算。任务在跑不算停滞；只在等主人回答的任务或问题不算停滞（主人看得到问题卡，负责人没事可做），这时和没有要做的事一样按空闲算：等满空闲天数也收起（问题卡还在，主人一答就展开）；**没有要做的事不提醒、不停住**，只按下面的空闲算。
    - 空闲收起：没有要做的事（同上）、也没有动静，满 `COVEY_PROJECT_IDLE_DAYS` 天（默认 7，可写小数试用；不是正数用默认）就收起：状态仍是 `paused`，`state_reason` 写 `idle`（`projects.IDLE`，不新增状态，`PROJECT_TRANSITIONS` 不变），记录多一个 `idle: {at, days}`；不弹停住卡、不发问题卡（`ProjectLoop.rest`，不走 `pause`）。专项卡缩成一行“收起 · N 天没有新的事 / 有新的事会自动展开”（`ProjectCards.fold`，不关闭，不受 12 天重发影响）。主人在负责人话题里说话、在专项的话题里说话（`heard`）、管家 `project_send` 转来新的事（都经 `reopen`）时回到 `active`，下一次刷新把整张卡画回来；`idle_reopened_at` 让它在又有动静前不会再次收起。`idle` 算 `RESUMABLE`，不算守卫停住（`GUARD_REASONS`）。
    - 收尾与重开：专项只由负责人的 `project_finish` 记成 `done`。整理专项把已有话题收进来建的专项（`origin.kind` 是 `tidy`）在主人还没在里面说过话（记录的 `owner_at`）、负责人也没派过活（`acted`）时，`project_finish` 拒绝（`project_tools.finish_gate`），开工回执也写明“收进来的话题做完不等于专项完成”：此前拆出的专项在建好 5 秒后的第一次叫醒里就被收尾了。主人又在 `done` 的专项里说话（负责人话题 `owner_turn`、专项的话题 `heard`、管家 `project_send`、网页说话和重开，都经 `project_loop.owner_spoke`）时回到 `active`（`unfinish`）：记录的 `reopened` 记下时间和上次的结论，负责人下一轮的“专项状态”多一行“重开”；`state_job`、`conclusion_message_id` 清掉，专项卡的 `closed`、`digest` 清掉，下一次刷新在原处把整张卡画回来。管家快照列出 14 天内做完的专项（`butler.DONE_SHOWN`），`project_send` 能转给它。负责人的工具在不是 `active` 时的拒绝写清下一步（`project_tools.ended_words`）。`cancelled` 仍是终态。
  - **唤醒轮的回复默认不发**：唤醒轮算安静轮（`placement.quiet` 认 `lead-` 前缀）：不显示任务卡（`Cards.reveal` 不发），没有表情。已授权回复保存、记为已送达（`TurnDelivery.silent`：`send` 直接返回），所以重投不重试、恢复卡不把它当未送达；没做完的轮（失败、停止、被拒）照旧在顶层说一句，不藏。例外：负责人在这一轮用 `project_note` 记了 `report`、且按风格该报（`project_lead.reportable`；`project_finish` 的收尾也算），这时和负责人主人触发的轮一样发到顶层。这一轮有变化也发（主人定的“有变化才发”）：用 `project_topic_start`、`project_topic_send` 派了活，`project_topic_stop` 停了话题，`project_answer`、`project_escalate` 替主人答了或转交了问题，工具都在专项记录的 `acted` 里记下这一轮（`app/project_tools.py` 的 `acted`，留最近 40 轮），`project_loop.reported` 认它。只是读了结果、没动手的唤醒轮照旧不发，只改专项卡。
- **维护** `projects/` 里状态是 `done`、`cancelled` 且 180 天没改的记录连同 `projects/<id>/` 一起删（`ProjectRecords`，`remove_projects`），`projects/.locks/` 归 `Locks` 规则。
- **主动记录**（`COVEY_PATROL=shadow`，只记不打扰）：规则每分钟、小模型每 30 分钟，写 `patrol/`，早晚汇总多一块；说明、可调数值和关法见[管家 · 我本来想找你的事](butler.md#我本来想找你的事开关默认关只记不打扰)。维护 `patrol/candidates/`、`patrol/decisions/` 里 14 天没改的日文件（`PatrolRecords`，`remove_patrol`）。
- **网页工作台同步**（`COVEY_WEB=1`，默认关）：`web-push`、`web-inbox` 两条受监管线程，把专项、话题（含管家和专项负责人）、一轮（含过程和用量）、问题推给网页，取回网页上的说话（含对管家、负责人说）、答题、停止、重开、记为看过、作废一条专项记忆；不开没有线程。不持锁发请求，操作先记 `<state>/web/inbox.json` 再执行，不重放；说明和配置见[网页工作台](web.md)。

## 执行指标口径

任务卡和任务详情上 token、缓存、平均 token/s 和 TTFT 的共同口径（面板见[任务卡与执行过程](usage.md#任务卡与执行过程)，延迟字段见[健康诊断](troubleshooting.md#健康诊断)）；各后端从哪里取数见 [Harness 说明](harnesses.md)各节的“执行指标”。

- **token**：本轮主任务加各层子任务的合计，是各次请求的累计。模型生成、token/s 和 TTFT 只算主任务。没提供或不完整的项不显示，不估算，不把未知记为零。数值写进任务记录和执行过程快照，结束时冻结。
- **上下文**：`token_usage` 可带 `context_tokens`（主任务最后一次请求的输入加输出）、`context_window` 和 `model`。轮前压缩算进本轮累计、不算上下文。
- **缓存**：命中和写入已含在输入总量里，不能再加。任务详情结束后把输入拆成“普通输入 + 缓存写入 + 缓存命中”。
- **子任务**：`subtask_usage` 事件（`core/usage.py` 的 `SubtaskReporter`）；`core/trace.py` 的 `turn_usage` 算合计，同一请求只记一次，有缺口写“部分统计”。
- **平均 token/s**：`core/usage.py` 的 `rate`，输出 token 除以测得的活跃生成时间，是含等待和思考的观测吞吐。工具运行期间的请求不计入分子（`MeasuredOutput`），这时卡上写明分子。
- **TTFT**：本轮主任务第一个模型输出的时刻，不含排队、进程启动、会话恢复和飞书渲染；`ttft_source` 记来源，dsh 没有。第一个输出是工具调用时不写。
- **阶段与区间**：`core/metrics.py` 的阶段 `received`、`submitted`、`queued`、`dispatched`、`backend_requested`、`backend_started`、`first_output`、`execution_finished`、`delivery_started`、`delivered` 各只记第一次。区间：`selection_ms` received → submitted，`submission_ms` → queued，`queue_ms` → dispatched，`prepare_ms` → backend_requested，`first_output_ms` → first_output，`execution_ms` backend_requested → execution_finished，`delivery_ms` delivery_started → delivered，`total_ms` received → delivered。用时的写法是 `run_seconds`、`took_text`。

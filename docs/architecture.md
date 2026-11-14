# 架构与开发说明

改代码前读[总览](#总览)、[代码布局与约束](#代码布局与约束)和两条数据流，再读要改的那一节（`grep -n '^#' docs/architecture.md` 取行号）。本文写结构、数据流和不变量；按文件找代码见根目录 `AGENTS.md` 的模块表，字段和细节见模块 docstring，常量、文案和日志格式以代码为准，规则的来由见架构决策记录。用户行为见[使用指南](usage.md)，部署见[运维指南](operations.md)和[配置](configuration.md)，验证见[测试与验收](testing.md)。

## 总览

进程、线程和谁重启谁见[服务组成](operations.md#服务组成)。

| 进程 | 做什么 | 入口 |
| --- | --- | --- |
| Bridge | 消费飞书事件、路由、建任务、排队、认领任务进程、重投、维护 | `bin/covey_bridge.py` → `app/unified.py` 的 `UnifiedBridgeApp.main()` |
| 任务进程 | 跑完一个任务（后端、任务卡、投递）；Bridge 重启或部署时继续 | `bin/covey_runner.py --run-dir <state>/runs/<任务键>` → `app/runner.py` 的 `runner_main` |
| Manager、看门狗 | 服务管理、开机拉起、内存兜底 | `service/manager.py`、`service/boot.py`、`service/memguard.py` |

Bridge 主线程跑事件消费者（`lark-cli event consume <事件> --as bot`，每行一个事件）；其余工作线程（outbox、维护、看板等）异常退出后由 `core/loops.py` 的 `supervised` 重建。

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

- `UnifiedBridgeApp`（组合根；`app/card_routing.py` 的 `CardRouting`、`app/job_dispatch.py` 的 `JobDispatch` 是它的两半）建一个 `Runtime`（进程状态，`app/runtime.py`）、一个 `Messenger`，每个 Harness 一个 `Harness`（只有数据，`app/harnesses.py`）和一个 `BridgeApp`（`app/app.py`），加 `Topics` 和卡片控制器。任务进程按 `handoff.json` 里的 `Runtime.cli_argv` 再建一遍。
- `BridgeApp` 不继承任何类，入口只有话题消息、卡片点击、出队任务、启动时的过期任务；组件在 `__init__` 里按引用拿协作者。`Cards`（`app/cards.py`）是任务卡和执行过程面板唯一的写入方。
- 可选功能对象自报 `threads()`、`menu_items()`，汇总进工作线程和菜单表；没配置时不起线程、不加入口。`main()` 的停止步骤按顺序写在一个函数里，由 ExitStack 调用。

### 硬约束

- `bin/` 入口先把本 release 的仓库根插到 `sys.path` 最前再导入包，`ArgumentParser(prog=...)` 固定程序名。包内文件不作脚本运行；起子进程用 `core/paths.py` 的 `entry_script(name)`。
- `__init__.py` 全空。扩展的隔离解释器导入的 `core/` 模块只用标准库（公司扩展的清单在 扩展 tests 里的结构测试）；改了在那个解释器里跑扩展的测试，见扩展 README。
- `service/boot.py`（由 PID 1 加载）和 `service/memguard.py`（复制到本地盘运行）只用标准库、不导入本包。
- 文字、卡片和 CardKit 写都经 `Messenger`；已发卡片的写从 `core/card_sequence.py` 的 `reserve` 取 sequence；按钮 kind 先登记在 `app/card_actions.py`；补充投递记录只经 `core/deliveries.py` 的 `update_delivery` 改。
- 子进程用 `core/process.py`：`stream_subprocess_lines` 超时和停止（`JobCancelled`）都停整个进程组；`stop_group` 先 SIGTERM 再 SIGKILL，活下来的只报告不抛。
- JSON 用 `core/state.py`：`read_json` 错误上抛；`write_json` 各写方用自己的临时文件再替换；`write_durable_json` 返回前落盘。
- 按后端区分的路由和问答证明从 `core/roster.py` 派生；谁被服务只由 `app/access.py` 判断。
- 兼容：现网 `harnesses.json` 能解析；`runs/<任务键>/` 和旧 release 的任务进程双向兼容；旧记录照常读（旧的内嵌执行过程由 `core/migrate.py` 一次性写成 `traces/<任务键>.jsonl`）；旧卡上的 kind 照常工作或带提示拒绝。

### 代码位置

跨模块的分工（一个模块一行的表在 `AGENTS.md`）：

- **卡片渲染**：骨架、按钮、折叠面板和元素预算（`CARD_NODES`、`CARD_BYTES`）在 `cards/elements.py`，菜单面板、启动卡、回执、目录浏览共用它的面板样式；CardKit 写的 JSON 在 `cards/cardkit_actions.py`；表单卡的发布与整卡替换在 `app/form_cards.py`。
- **执行过程**：数据 `core/trace.py`（`ToolTrace`），落盘 `core/trace_store.py`，面板数据 `cards/trace_view.py`，画面板 `cards/trace_updates.py`，卡上数字 `cards/facts.py`。状态行：`core/longtask.py` 的 `TaskMonitor` 收集事实，`core/status_line.py` 的 `status_text` 出文字；状态词在 `core/turn_words.py`。各后端的事件适配器共用 `core/steps.py`。
- **启动卡与换配置**：`app/launch.py`、`app/launch_models.py`（`CATALOG_FILTERS`）、`app/preselection.py`（纯函数预选）、`app/model_usage.py`、`app/model_picker.py`、`app/directory_picker.py`；换配置 `app/harness_switch.py`，改绑定 `app/binding.py`，交接 `app/switch_handoff.py`。
- **任务进程两端**：起进程 `app/runner_spawn.py`，收发 `app/runner_ipc.py`（`RemoteControl`、`RunnerLink`），认领和收尾 `app/detached.py`；文件名和收发原语 `core/run_dir.py`；退出前等的线程 `core/background.py`。
- **投递**：结算 `app/turn_settle.py`，发送 `app/worker.py` 的 `TurnDelivery` 和 `app/answer_delivery.py`，答案卡 `cards/answer_card.py`、`app/answer_cards.py`，重投 `app/redelivery.py`、`app/outbox.py`，恢复 `core/recovery.py`、`app/recovery_cards.py`。
- **看板**：`app/pins.py`、`app/status_cards.py`、`core/board_read.py`、`app/overview.py`、`app/topic_seen.py`。主线程和回带见[主线程](main-thread.md)，代答见[代答](proxy.md)。

## 一条消息的数据流

```text
feishu/events.py consume：每个订阅一个 lark-cli 消费者，逐行交给回调
 → app/card_routing.py handle_event：配对；Access.admit；inbound-messages/ 去重；启用的扩展、主线程或话题路由
 → app/topics.py Topics.route：core/workspaces.py 的 topic_context 找到或建立 topic.json，按 harness_id 选 BridgeApp
 → app/app.py BridgeApp.handle_event：附件、转发、被回复消息；有待答问题 → app/interaction.py 走问答；否则写历史
 → app/submission.py submit_topic_job：未开始 → waiting_workspace + 启动卡；已开始 → 排队卡；先写 queued/ 再入队
 → core/scheduler.py WorkspaceScheduler：同话题串行，跨话题有上限地并行
 → app/job_dispatch.py process_reply_job：核对绑定和快照、预检、Harness.ensure_started
 → app/detached.py run_detached → app/runner_spawn.py start_runner：写 handoff.json，起任务进程
 → app/runner.py runner_main → app/worker.py process_bridge_reply_job
 → app/runs.py Runs：提示词（app/prompts.py）和 RunRequest → backends/<名字> 的 backend.run()
 → 后端事件 → Interaction.structured_harness_event → core/trace.py → cards/trace_view.py → Cards → app/task_card.py
 → RunResult → app/turn_settle.py settle_turn：保存 → 会话保护 → 授权
 → TurnDelivery.deliver：答案卡；主线程回带挂钩 after_turn；失败留给 app/outbox.py
 → done.json、exit.json → Bridge 的 finish_detached 收尾，未结束的记录标 interrupted，原卡换恢复卡
```

### 谁被服务

`app/access.py` 的 `Access`（`Runtime.access`，任务进程里也有）是唯一的判断处，别处不比较 open_id。

- **消息**（`admit`）：私聊只认主人，配对也只在私聊；群里只认 @ 了本机器人的消息（@所有人不算），发送者是主人或这个群已激活。被拒的记一行 `ignored message … reason=…`。
- **群记录** `groups/<sha256(chat_id)>.json`（`core/groups.py`）：只有 Bridge 写（`group` 锁）。主人第一次 @ 时在锁外查群信息，不是外部群才激活；查询失败不激活；停用过的群不自动激活。
- **免@**（`COVEY_GROUP_ALL_MESSAGES=1`，见[群聊免@](configuration.md#群聊免)）：群记录的 `free_talk` 和 `free_talk_members` 决定谁不用 @（`talks_freely`）。免@ 不扩大谁被服务；对别人说的（`for_others`）、代答群（`proxy_chat`）不免@。
- **点击**（`allows`）：主人，或已激活群里的任何人。工作区管理、目录选择和菜单只给主人（`is_owner`），其他人点了在卡上拒绝（outcome `owner_only`）。
- **空间**（`core/spaces.py`，设置见[空间](configuration.md#空间spaces)）：`owner_accounts` 里的账号都是主人；`allow_team_spaces` 为 false 时没有群被激活；`personal_groups` 里的群只服务主人。默认设置下这些都不改变上面的规则。通知（早晚汇总、手上的事、告警）发到 `Access.notify_chat()`。个人群和 `owner_accounts` 的私聊各有主线程（`Access.agent_chat`，`MainEnv.chat()` 随工具调用所属的主线程），个人群有置顶菜单卡（`app/group_menu.py`）和自己的看板记录（`app/group_boards.py`、`app/pins.py` 的 `record_path`）。
- 代答用 `owner_user_id` 认出主人自己发的消息，不是谁被服务的判断。

## 卡片点击的数据流

```text
feishu/events.py card.action.trigger
 → app/card_routing.py handle_card_action_event：Access.allows；去重
 → EARLY 表（app/unified.py）：按事件认领——启用的扩展的卡片、工作区管理、启动卡模型选择、目录选择、换配置、回退
 → PANELS 表：按 value 认领菜单面板——代答、群聊、脱敏、主线程、设置卡……
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
- `SIGTERM` 是中断，不是停止（`app/runner.py` 的 `interrupt`）：没做完的一轮不结算（`leave_interrupted`），记录留在 `running`，退出后由 Bridge 标 `interrupted`；已做完的结果照常投递。
- 没起来：退出时仍是 `queued` 且无 `exit.json` 时 `relaunch_unstarted` 再起一次；`start_runner` 抛错时 `not_started` 把记录写成 `failed`（`runner_start_failed`）并出恢复卡。
- `done.json`：Bridge 见到它（`job_done`）先交还调度槽位，同话题的下一条照常进行；点击仍转给活着的任务进程；outbox 和自动重试等它退出。
- 遗留进程：任务进程把 `COVEY_TASK_DIR` 设为运行目录，每条命令都继承。后端返回后 `app/leftovers.py` 用 `core/task_processes.py` 找出带标记、已脱离任务进程的存活进程：搜索工具（`SEARCH_TOOLS`）先 SIGTERM 再 SIGKILL，其余只在执行过程里列出。内存看门狗也读这个标记，把告警发到任务所在的话题。
- `runner_alive` 要求 pid 的启动时间、boot ID 和记录一致且 cmdline 含运行目录。

### 重启认领

`UnifiedBridgeApp.main` 先拿状态目录的实例锁，然后：

1. `recover_question_delivery` 核对问题记录；`recover_worktree_launches` 把建 worktree 时被打断的启动卡换回表单。
2. `adopt_runners` 按 `handoff.json` 认领存活任务进程，往 inbox 写 `bridge_restarted`；再排上 `queued/` 里的任务。
3. `recover_stale_jobs` 把其余未结束记录标 `interrupted`；`close_retired_jobs`；`release_started_launches` 排上已绑定话题里还在 `waiting_workspace` 的消息。

### 中断与恢复卡

- 卡由 `app/recovery_cards.py` 画（kind 都是 `bridge_job_retry`），状态、按钮和点击后做什么由 `core/recovery.py` 的纯函数 `classify` 判定；按钮和点击之间有意保留的分歧写在该模块开头。
- 已授权或已保存的正文没送达 → 重新投递结果；暂停问题没送达 → 重新投递问题；其余失败或中断 → 检查并继续（按原模型快照新建任务）；未授权、已送达、需核对或已 `native_answer_once` → 无按钮。
- 话题还有运行或排队的任务时拒绝；每条记录只重试一次（`retried_by`）。任务没跑完时告诉用户：`app/task_controls.py` 的 `report_unfinished`。

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
| `answer-cards/` | 单独发出的答案卡：要发的卡（`outgoing`）、发出的卡和产物状态 | 两者 | `answer-cards`，跨进程；锁内只读写这个文件 |
| 表单目录（`launch-forms/` 等）、`cards/.locks/` | 卡的 card_id、sequence、表单状态；卡片锁文件 | 发卡的进程 | 卡片锁 |
| `groups/` | 群记录 | Bridge | `group`，进程内 |

- **索引**：`core/store.py` 的 `JsonDirectoryStore` 给 `jobs/`、`questions/`、`held-notes/` 建内存索引，读目录的 `.journal` 追变化，定期列目录兜底；查询字段（`INDEXED`）常驻，整条记录被选中才读；`.index` 让新进程快速起步。手工改记录后调 `invalidate_directory`。一条消息、一次点击在 `one_scan()` 内处理。
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
- `capabilities(ctx)` 的 `HarnessCapabilities` 决定任务卡控件、话题消息能否当回答、能否复制原生会话、自动重试（`rate_limit_retry`、`incomplete_retry`）、回退（`rewind`）、脱敏（`redaction`）、本轮请求导出（`turn_export`，后端的 `export_turn`，`app/turn_export.py`）。
- 提示词（`app/prompts.py`，`app/runs.py` 组装）：桥接规则同一话题每轮相同，放在用户消息里，`request.instructions` 为空；续接只给最新消息和 `RESUME_REMINDERS`；主人的原则由 `principles_block` 加入。`system_instructions` 通道保留，现在没有后端声明它。
- 后端继承 `BackendRuntime`。启动检查（`on_startup`）只由 worker 经 `Harness.ensure_started` 跑，失败后 `STARTUP_RETRY_SECONDS` 内的消息直接拒绝。模型目录由 Bridge 后台刷新到 `launch-models/<名字>.json`，点卡片时不探测后端。

### 后端名单

- `core/roster.py` 的 `HARNESSES` 是唯一的后端表（核心表 `CORE_HARNESSES` 加上启用的[扩展](#扩展)的项），每行：`module`、`store`（会话存储类）、`live_questions`（等回答时回合保持打开：Claude）、`paused_questions`（中断回合、带回答续接：Codex）。派生的 `NAMES`（第一个是默认路由）、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 供路由和问答证明使用。
- 模型从哪里来是供应商，不是 Harness：`core/providers.py` 的 `PROVIDERS` 记供应商属于哪个 Harness、模型 ID 前缀和登录在哪，`provider_of` 按前缀认出供应商。
- `RETIRED`、`RETIRED_PREFIXES` 记已下架的 Harness 和供应商；带这些前缀的模型由 `core/model_settings.py` 在入队前拒绝（[已下架项与旧配置](configuration.md#已下架项与旧配置)）。

### 扩展

不是每个部署都要的功能放在核心之外的 Python 包里（`core/extensions.py`，只用标准库）。没有启用的扩展时，名单、供应商表和功能列表与没有这个机制时完全一样。

- **启用**：环境变量 `COVEY_EXTENSIONS`（逗号分隔的包名；设成空串表示一个都不要），没设时读仓库根的 `extensions/enabled`（每行一个包名，`#` 起注释，空行忽略；文件不存在就没有扩展）。
- **查找**：`COVEY_EXTENSION_PATH`（`os.pathsep` 分隔的目录），没设时是仓库根的 `extensions/`。有启用的包时才把这些目录幂等地放到 `sys.path` 前面。
- **`<包>.manifest`**：只放纯数据（`dict`、`list`、`str`），不导入 `covey`（它会被扩展的隔离解释器里的 `core/roster.py` 间接导入，也要避免循环）。可选的几项：`HARNESSES`（名字 → `module`、`store`、`store_module`、`live_questions`、`paused_questions`、`after`，字段同 `RosterEntry`；`after` 是它在 `NAMES` 里排在哪个 Harness 之后，启动卡和换配置卡的 Harness 顺序跟着它，不写就接在最后）、`PROVIDERS`（字典列表，字段同 `Provider`）、`RETIRED_PREFIXES`、`RETIRED_HARNESSES`，以及核心读的几张小表：`WORK_MODES`（模式 ID → [名称, 说明]）和 `DEFAULT_WORK_MODE`（`core/defaults.py`）、`AGENT_DIRS`（Agent 在工作区里放工作副本的隐藏目录，`app/artifacts.py` 不上传）、`IGNORED_ENV`（不再读取的变量 → [Harness, 替代选项]，`core/ignored_env.py`）。`core/roster.py`、`core/providers.py` 的纯函数 `merged` 把它们接在核心表后面，`NAMES`、`LIVE_QUESTIONS`、`PAUSED_QUESTIONS` 从合并后的表派生。
- **`<包>.features`**（可选）：`features(app)` 返回功能对象的列表，形状同 `app/unified.py` 的 `self.features`：可有 `threads()`、`menu_items()`、`menu_folds()`；排在核心功能后面。
- **钩子和设置**（`core/extensions.py` 的 `HOOK_NAMES`、`SETTING_NAMES`）：manifest 的 `HOOKS`（钩子名 → 包内模块的点分名）让核心在需要时才导入那个模块：`proxy_sender`（`app/proxy.py`：代答发送器，有 `Sender`、`ProxyLogin`、`tool_dir`、`send_problem`）、`dashboard_entry`（`app/dashboard.py`：看板固定入口，有 `entry_alias`、`entry_url`、`publish`、`REPUBLISH_AFTER`）、`service_hosting`（`service/install.py`：`MODES` 模式名 → `install(root, args)`，`add_arguments(parser)`，可选 `auto_mode()` 适用时返回模式名，给 `--mode auto` 用）。`SETTINGS`（名字 → str）给核心几处写死过的部署细节：`web_tenant_domain`、`contact_notes`、`contact_notes_dir`、`contact_notes_hint`，也可以放包自己的 `<包名>.<名字>`（`setting` 照读，扩展自己用）。`extensions.hook(名字)` 没有扩展提供时是 `None`，`extensions.setting(名字, 默认)` 给默认值。没有发送器时不起代答线程、没有菜单项和设置卡标签；没有 `dashboard_entry` 时看板照常、没有固定入口；没有 `service_hosting` 时安装器只有 `--mode systemd|launchd|manager|auto`（见 [服务托管](operations.md#服务托管)）。
- **功能对象和卡片按钮**：包的 `features` 模块的 `features(app)` 返回功能对象，接在核心功能后面（线程、菜单项同核心功能）；这些对象还可以有 `handle_event(ctx, event, owner)`（顶层消息，在主线程和话题路由之前，认领返回 True）、`claim(ctx, event, raw, owner)`（卡片点击，在核心控制器之前，不是自己的返回 `None`）和 `close()`（Bridge 停止时）。manifest 的 `CARD_KINDS`（按钮 kind → 名称）并进 `app/card_actions.py` 的 `ACTIONS`（`bot_only`）。已发出的扩展卡片在扩展没启用时：`app/card_actions.py` 的 `MOVED_CARDS` 回一句“需要某扩展”，不崩溃、不执行；`core/extensions.py` 的 `MOVED_ENV` 列出已搬进扩展的功能的环境变量，没启用扩展却设了时启动日志记一行、忽略。
- **失败要响**：启用了的包导入失败、manifest 形状不对、Harness 名或供应商（Harness 加前缀）或已下线前缀与核心或别的扩展重复，都抛带包名的 `ExtensionError`，Bridge 启动即失败，不静默跳过。
- **核心不按名字引用扩展**：核心代码里不出现扩展的包名、Harness 名或供应商；要区别对待靠 manifest 的字段、`HarnessSpec` 上的能力字段（`refuses_questions`：原生问题能否被拒绝；`native_default_model`：任务记录里的空模型是否表示后端自己的默认）或后端对象上的方法（模型目录菜单的 `catalog_hidden`、`catalog_live(models, state_dir)`、`catalog_view(models)`，由 `app/launch_models.py` 的 `catalog_filter` 读）。
- **扩展的测试**：放在 `extensions/<包>/tests/`（`tests/support/` 并进核心的 `support` 包），扩展启用时 `scripts/test_map.py` 与核心测试一起收集；核心测试在 `COVEY_EXTENSIONS=''` 下也要全过。扩展的脚本放 `extensions/<包>/scripts/`。后端仍遵守“后端不导入 `app/`、`feishu/`、`cards/`”的规则。

### 话题中途换配置

- 入口见[换配置](usage.md#换配置)。按钮带 `harness_id`、`binding_revision`，不一致时 `current_binding_card`（`app/binding.py`）按 `job_id` 判断能否打开。
- 卡片（`cards/switch_form.py`）分级：Harness、供应商、模型只改记录里的 `selected_model` 再整卡重画；其余项记录后重画。独立 worktree 在卡的 worker 线程里建（`made_worktree`）。
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
- 词表在设置卡的「脱敏」标签上改（一层，没有页签；替身词自动生成，`update_config` 只放行词表里本来就有的自定义替身；`app/redaction_panel.py`，`PANELS` 里认领它的按钮）：点击在记录锁外经 `core/redaction.py` 的 `update_config`（`EDIT_LOCK`，一次一处）改词表，再在设置卡记录的锁里整卡重画；「探测」在自己的线程里跑。卡片、卡片记录和日志里都不出现原词：行名是 `masked`，按钮按 `card-key` 下的 HMAC 指认行，拒绝原因是不提原词的 `Refused`。
- 网页看板（`app/dashboard.py`，见[敏感词看板](configuration.md#敏感词看板)）可选，是 Bridge 的工作线程，链接在「脱敏」标签最后；入口记录在 `app/dashboard_entry.py`，持 `ENTRY_LOCK` 时不调外部命令、不发消息。

### 已下架的 Harness

绑定的 `harness_id` 不在 `HARNESSES` 里时 `route()` 抛 `RetiredHarness`：消息和旧卡点击只得到一句提示（`retired_notice`），不建任务；`adopt_runners` 不认领它的任务进程，`close_retired_jobs` 标 `interrupted`。下架时从 `HARNESSES` 和安装脚本删掉、在 `RETIRED` 加一行（`test_harness` 的 `RetiredHarnessTests`）。

### 加一个后端

1. `backends/<名字>/`：`<ID>_SPEC = HarnessSpec(...)`、后端类、会话存储类，末尾 `HARNESS_REGISTRY.register(...)`；名字不能和现有的或 `RETIRED` 里的重复。
2. `core/roster.py` 加一行，选 `live_questions` 或 `paused_questions`。
3. 只发公开事件；`capabilities(ctx)` 如实声明。
4. 结果经 `verified_reply` 存 `VerifiedRootReply`，返回带 `public_reply` 和 `outcome` 的 `RunResult`；一次运行只声明一个根会话。
5. 参数用 `add_cli_args` 注册（[配置 Harness 参数](harnesses.md#配置-harness-参数)）；CLI 名加进 `scripts/setup_harness_bridge.sh` 的探测列表。
6. `tests/support/backend_drivers.py` 加驱动（`test_backend_contract`），再加完成边界和事件转换测试；用法写进 `docs/harnesses.md`。

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
- 回执卡的按钮由 `offers` 决定；“重新排队执行”每条只排一次（`requeue_state`、`requeued_job_id`）。
- 主线程的补充没有回执卡：确定没读到的（`refused`，或 `target_ended`）标 `requeue_auto`，由 Bridge 用同一个 `_requeue` 以主人消息的 id 排成新一轮，同样只排一次（[main-thread.md](main-thread.md) 收尾时发来的消息）。

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
- **暂停的问题和恢复原文**：问题正文（超过 `FILE_BYTES` 发文件）和问题控件失败后恢复原文，首发和重投都用 `app/saved_send.py` 的 `send_paused`、`restore_original`。问题正文只在没发任务卡的主线程安静一轮发到顶层，其余回到它回复的位置；恢复原文先写回任务卡（重投没有任务卡可写），再发消息，最后发文件。
- **审核拒收**：飞书 DLP 拒收（230028，`audit_rejected`）时 `feishu/delivery.py` 按点名的类别打码（`mask_sensitive`），换 `<键>-masked` 发一次；仍被拒时不给按钮。
- **任务卡发不出去**：`StreamingCard.create`（`app/task_card.py`）里卡片消息被明确拒绝（`feishu/delivery.py` 的 `refused_outright`，不含可能已送达的）时，`_send_elsewhere` 换 `<键>-again` 原处再回复一次，再换 `<键>-top` 发到会话顶层；每次失败写进任务的 `last_card_error`。仍没有卡时 `job.card_missing` 为真，随交接的 extras 带给任务进程，状态行的每次更新（`Cards.update_card_activity` → `resend_missing`）每 `CARD_RESEND_EVERY` 秒补发一次，最多 `CARD_RESENDS` 次，补上后把 `card_id`、`card_message_id` 写回任务记录。放弃卡改发文字的（`replace_card`）不补。没卡的任务结论照常单独发出（`TurnDelivery._answer_apart`、`_fallback`）。
- **送达闸门**：`CheckpointQuestions.undelivered_jobs` 列出还要重投的任务，话题的新消息和换配置据此拦下。
- **幂等写**：`feishu/transport.py` 发请求前先写 `delivery-receipts/` 回执并 fsync；同一 UUID 配不同内容失败（`idempotency_conflict`）；不确定的写只在有限时间内用原 UUID 重试，之后返回 `reconciliation_required`，不换键盲发。写已有卡片时，前一次不确定后的重试收到 200770 或 300315 算前一次已生效。
- **传输**：开了 SDK（[飞书 SDK 与直连](configuration.md#飞书-sdk-与直连)）时由 `feishu/sdk.py` 发，请求由 `feishu/message_request.py` 从 lark-cli 的 argv 重建、逐字节相同，新旧进程共用回执；请求发出后不换传输。
- **重投**：在 `verified-results` 锁内核对任务号、运行 ID、代际和指纹，只读已授权的正文，不启动模型；放开锁再发。只有 Bridge 重投。
- **outbox**：`app/outbox.py` 定时扫描已授权、未送达、未暂停、到时的记录，从不运行 Harness；可重试的失败指数退避，用完转 `paused` 等用户处理。
- **自动重试**（`app/auto_retry.py`，见[自动重试](usage.md#自动重试)）按 `KINDS` 分限流、本轮未全部完成、重启打断三种，各有次数上限，相加不相乘。到点时锁内 `pending` → `submitting`，锁外重试，状态只按 `AUTO_RETRY_TRANSITIONS` 前进；唯一例外是官方拒绝的停止直接转 `cancelled`。官方安全拒绝后换旧模型重试一次（`app/policy_retry.py`，[policy-stop.md](policy-stop.md#换旧模型重试一次)）不走 `auto_retry_state`，另用 `policy_retry_state` 和 `POLICY_RETRY_TRANSITIONS`（同样两步），由 `policy_retry.tick` 经 `TaskControls.requeue(fallback=…)` 排上。
- **重跑还是拒绝**：什么都没保存的一轮按原快照重新排队；有保留候选的拒绝重跑；候选全部作废、从没授权过的（`withdrawn_unauthorized`）算没保存。各后端的投递不变量由 `test_backend_contract` 验证。

## 代答

实现见[代答](proxy.md)，用户行为见[代答](usage.md#代答)，配置见[代答的配置](configuration.md#代答)。它落在全局规则上的地方：

- **记录与状态**：`<state>/proxy/` 由 `core/proxy.py` 读写，只有 Bridge 写。批次状态只按 `BATCH_TRANSITIONS` 前进，由工作线程 `proxy` 写。
- **锁**（规则 1）：`Proxy.lock` 内只读写记录；发前闸门在锁内、转 `sending` 前再判断一次；锁外跑发送工具。`web_lock` 保证同一时刻一次发送或扫码登录。
- **不重发**（规则 1、6）：按过回车的只读回，找到是 `sent`，否则 `uncertain`。
- **卡片**（规则 3、8、9）：设置卡和重新发送卡在自己的卡锁内取 sequence、整卡替换；kind `bridge_proxy_*` 在 `ACTIONS`，由 Bridge 自己处理。
- **授权**（规则 5）：只发 `load_authorized_reply` 取到的已授权回复；重新发送不跑模型。
- **主人**（规则 11）：主人判断来自 `Access`；`owner_user_id` 只用来认出主人自己发的消息。

## 执行指标口径

任务卡和任务详情上 token、缓存、平均 token/s 和 TTFT 的共同口径（面板见[任务卡与执行过程](usage.md#任务卡与执行过程)，延迟字段见[健康诊断](troubleshooting.md#健康诊断)）；各后端从哪里取数见 [Harness 说明](harnesses.md)各节的“执行指标”。

- **token**：本轮主任务加各层子任务的合计，是各次请求的累计。模型生成、token/s 和 TTFT 只算主任务。没提供或不完整的项不显示，不估算，不把未知记为零。数值写进任务记录和执行过程快照，结束时冻结。
- **上下文**：`token_usage` 可带 `context_tokens`（主任务最后一次请求的输入加输出）、`context_window` 和 `model`。轮前压缩算进本轮累计、不算上下文。
- **缓存**：命中和写入已含在输入总量里，不能再加。任务详情结束后把输入拆成“普通输入 + 缓存写入 + 缓存命中”。
- **子任务**：`subtask_usage` 事件（`core/usage.py` 的 `SubtaskReporter`）；`core/trace.py` 的 `turn_usage` 算合计，同一请求只记一次，有缺口写“部分统计”。
- **平均 token/s**：`core/usage.py` 的 `rate`，输出 token 除以测得的活跃生成时间，是含等待和思考的观测吞吐。工具运行期间的请求不计入分子（`MeasuredOutput`），这时卡上写明分子。
- **TTFT**：本轮主任务第一个模型输出的时刻，不含排队、进程启动、会话恢复和飞书渲染；`ttft_source` 记来源，dsh 没有。第一个输出是工具调用时不写。
- **阶段与区间**：`core/metrics.py` 的阶段 `received`、`submitted`、`queued`、`dispatched`、`backend_requested`、`backend_started`、`first_output`、`execution_finished`、`delivery_started`、`delivered` 各只记第一次。区间：`selection_ms` received → submitted，`submission_ms` → queued，`queue_ms` → dispatched，`prepare_ms` → backend_requested，`first_output_ms` → first_output，`execution_ms` backend_requested → execution_finished，`delivery_ms` delivery_started → delivered，`total_ms` received → delivered。用时的写法是 `run_seconds`、`took_text`。

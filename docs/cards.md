# 事件、执行过程与卡片

本文是[架构与开发说明](architecture.md)拆出的一节：后端事件怎么变成任务卡上的执行过程，卡片点击怎么分发，以及改卡片和按钮时的限制。记录、锁、状态和投递见架构与开发说明。

## 模块一览

路径相对 `covey/`。

| 模块 | 职责 |
|---|---|
| `core/protocol.py` | 公开事件 `HarnessEvent`，观测类事件集合 `PROGRESS_EVENT_KINDS` |
| `core/trace.py` | 执行过程的数据模型：收事件（`ToolTrace`）、快照与恢复、`turn_usage`、预算 `TRACE_BYTES` |
| `core/trace_store.py` | 执行过程落盘 `traces/<任务键>.jsonl` |
| `core/longtask.py`、`core/status_line.py` | 盯一轮的后台线程 `TaskMonitor`；状态行纯函数 `status_text` |
| `core/updates.py` | 合并写卡的 `CoalescedUpdates` |
| `core/checkpoint.py` | 检查点，含可选的 `steps`、`delivery` |
| `cards/streaming.py` | 整张任务卡 `build_stream_card`、标题行 `task_head`、按钮行 |
| `cards/task_status.py` | 任务卡状态行 |
| `cards/task_figures.py` | 数字格和输入/输出两行 |
| `cards/card_cost.py` | CardKit 卡片大小估算和正文截断 |
| `cards/trace_view.py` | 从快照画执行过程 `TraceView` |
| `cards/trace_updates.py` | 实时更新的面板形状 `panel_json` |
| `cards/trace.py` | 画执行过程的文本小工具 |
| `cards/answer_card.py` | 答案卡的输入/输出两行 `facts_element`、任务卡的输入/输出 `box`、改动折叠、回答下面的折叠 `details` |
| `cards/review.py` | 结局行、单独发出的答案卡、卡片摘要 |
| `cards/cardkit_actions.py` | CardKit 写请求的 JSON |
| `app/task_card.py` | 任务卡在 CardKit 上的建、写、关、替换：`StreamingCard`、`StreamingReply` |
| `app/cards.py` | 任务卡唯一写入方 `Cards` |
| `app/task_details.py` | 运行中的执行过程面板和数字（`TaskDetails`，`Cards.details`；任务详情不再上卡，旧卡上的照旧不动） |
| `app/record_cards.py` | 写入方结束后按任务记录写卡、按保存地址重画浏览卡（`RecordCards`） |
| `app/interaction.py` | 事件入口 `Interaction.structured_harness_event` |
| `app/card_routing.py` | 消息、菜单、卡片点击的路由（`UnifiedBridgeApp` 的 mixin） |
| `app/card_actions.py` | 按钮动作表 `ACTIONS`、已下线按钮的回复 `RETIRED_TEXT` |
| `app/card_lanes.py`、`app/card_queue.py` | 点击的分道和后续；控制器的有界队列 |
| `app/steer_forms.py`、`app/steer_receipts_flow.py` | 补充表单；补充回执卡 |

## 公开事件契约

`core/protocol.py` 的 `HarnessEvent(kind, data, version=1)`，共享层入口 `Interaction.structured_harness_event`。

| kind | 含义 |
|---|---|
| `tool` | 工具步骤，有 `child_id` 的归到子任务 |
| `progress_note` | 公开说明，保留全文；机器人自己的说明不算首个后端进展；id 为 `backend-retry` 且带 `retry` 时更新状态行的重试信息；id 为 `backend-queue` 且带 `queue` 时状态行写“排队 · 等空位”，不算首个后端进展 |
| `agent_state` | 子任务状态 |
| `answer_snapshot` | 答案增量，覆盖正文，定稿后忽略 |
| `token_usage`、`subtask_usage`、`first_token` | 执行指标，见[执行指标口径](architecture.md#执行指标口径) |
| `question`、`question_resolved`、`question_cancelled`、`cancel_questions` | 见[问题与回答](architecture.md#问题与回答)；`question`、`turn_finished`、`cancel_questions` 由 `Questions` 分发 |
| `turn_finished`、`trace_finished` | 本轮结束；冻结执行过程 |
| `steer_read` | 补充已被模型读取 |

- 其他 kind 一律忽略。事件只带公开字段，转换在适配器：`CodexEventAdapter`、`ClaudeEventAdapter`、`DshEventAdapter`（不转发 `thinking`）；扩展的后端（扩展 Harness）在子进程里生成，Bridge 侧再校验。
- 观测类事件（`PROGRESS_EVENT_KINDS`）处理失败只记日志、不打断回合；问题和输入类事件保留失败语义；事件处理不等卡片的 HTTP 请求。

## 执行过程与刷新

### 任务卡的结构

`cards/streaming.py` 的 `build_stream_card` 画整张卡，`app/task_card.py` 的 `StreamingCard` 发出并记下这张卡的 `StreamingReply`（卡上有哪些元素、上次写了什么）。从上到下：

| 元素 ID | 内容 |
|---|---|
| `task_head` | 标题行（没有标题栏）：T 号头像、标题加灰色“#任务号”，右边一栏是 `activity` |
| `activity` | 状态行第一行：状态标签和时间（`status_markdown`），在标题行右边 |
| `activity_more` | 状态行其余部分（阶段、当前、停止原因），没有时为空串 |
| `task_io` | 输入/输出两行（`io_box`，`cards/answer_card.py` 的 `box`，没有灰底）；“输出”那一栏的 ID 是 `task_figures` |
| `answer` | 正文 |
| `current_question` | 卡上的问题 |
| `action_row` | 按钮行：补充（`primary`，`steer_action`；排队卡上写“现在补给 #N”，任务开始后改回“补充”）、停止（`danger`） |

- “输入”五段：Harness ｜ 供应商 ｜ 模型 ｜ 思考 ｜ 工作区（`input_lines`），来自建卡时存的 `job.input_parts`（`answer_cards.inputs`，同答案卡；`CARD_FACTS` 带它，替换卡不丢）。
- “输出”运行中写 开始 ｜ 已运行 ｜ token ｜ token/s ｜ 上下文（`running_figures`、`with_ran`，顺序 `RUNNING_LINES`），结束后写 用时 ｜ token/s ｜ token ｜ 缓存命中（`output_parts`、`output_lines`），还没有数字时是“—”。没开始处理就结束的（排队中取消）照结束写，也是“—”。
- 段间是灰色的“｜”（`SEPARATOR`），段内不断行（`unbroken`），窄屏只在“｜”后折行。
- 话题里各卡的按钮都用 `cards/elements.py` 的 `button_look`（带边框、`small`、宽度跟着文字）；菜单面板用 `text_button`。停止按钮不带 `confirm`（见[卡片的客户端限制](#卡片的客户端限制)）。
- 管家的分档卡（`Traits.slim_card`，`StreamingReply.slim`）：没有标题栏；第一行是说话人的头像行（`speaker_head`，元素 ID `speaker_row`：头像和“**管家**”，不是链接、不写灰字，固定不变，状态在下面的状态行；正文间距、内边距和别的头像行卡一样），然后是正文、一行状态（`slim_status_markdown`）、默认收起的“详情”（`SLIM_DETAILS_ID`），输入/输出、本轮请求和按钮行在“详情”里，ID 照旧；重试行、补充和问题紧跟正文（`under_body`）。说话人取不到时没有头像行，间距照旧。专项负责人也是分档卡，但话题区里它从不发任务卡（[放置表](butler-internals.md#放置表)的 `task_card`）。详见[管家](butler.md)的“分档卡”。

### 状态行

- 文字由产生方写成纯文本（`TaskMonitor`、`outcome_line`、`STOPPING`，记录里存的也是它），写卡时经 `status_markdown` 排版：第一行的状态词成带底色的标签（`tagged`，`state_pill`），后面的时间灰字，下面几行小灰字；以 `##` 开头的（旧卡的样子）原样不动。`status_rows` 把文字切成 `activity` 和 `activity_more` 两部分。
- `core/longtask.py` 的 `TaskMonitor.tick` 把这一刻的检查点、进程、执行记录和重试收进 `Facts`，`core/status_line.py` 的 `status_text`（卡上的一行）、`status_summary`（记录里的 `long_task_summary`）算出文字；持久化、`state` 回调和限频写卡留在 `tick`。
- 每一轮一开始就有执行记录（五个后端都报告结构化事件），状态行从第一次刷新起写“执行中 · 已运行 …”。有灰框的卡“已运行”不在状态行（`status_rows(io=True)`），在“输出”里；过了 3 分钟只写到分钟（`ran_of`、`ran_shown`），内容没变不写。
- `StreamingCard.update_activity` 只写变了的部分：平时只 PATCH `task_figures`。
- 没有步骤的执行记录：有输出时写 `THINKING`，只有推理开始时写 `REASONING`，都没有时写 `WAITING_FOR_MODEL`；声明 `model_requests` 的后端在 `model_request` 到达前写 `STARTING`（这个事件由 `app/runs.py` 只交给状态行）。后端进程退出码为 0 时写 `COLLECTING`“正在整理本轮结果”，非 0 才写“后端进程已退出”。
- 进展取整棵执行过程树里最新的一件事，写模型的命令说明或粗分类，不写原始命令；“当前”行写最近一步和最近一次动静（`core/progress.py` 的 `activity_line`：“2 分钟前在跑测试”，只到分钟，`ACTIVITY_SHOWN` 以内不写；“当前”写的已经是这件事本身时只写“2 分钟前”）；进程活着不算进展。没有 `Facts.progress` 的调用方照旧写“最近进展 N 前”。
- 进度（`core/progress.py` 的 `progress`，一个纯函数，任务卡、看板、手上的事、早晚汇总、管家快照和网页共用）：阶段、步骤、最近动静、预计（`eta_seconds`，估不出来为 None；按任务记录的 `step_times` 算这一轮平均每步用时，第一次看到计划时就已完成的步骤不算，减去当前这步已做的时间）、这步比平均慢（`slow_step`，此时预计为 None，状态行写“此前平均每步约 6 分钟”）、可能卡住（`maybe_stuck`）。`TaskMonitor` 每次刷新算一遍，`last_activity_at`、`last_activity` 写进任务记录（旧任务进程没有这两个字段，读的时候用 `health_checked_at` 减 `last_progress_age_seconds`），第一次估出预计时把这一轮的总用时（已跑加预计还要）写成 `estimate_seconds`、之后不改（网页的预计 vs 实际用时），整份放进 `job.turn_progress` 给任务详情。预计写在状态行第一行末尾；任务详情写时刻（“14:05 在跑测试”“（14:20 估）”），因为它只随执行过程面板一起写。看板、手上的事、汇总只写阶段和预计（`board_words`；Pin 的话题卡和手上的事写成“预计 15:20 左右做完”，只在预计变了才重画），不写最近动静。
- 可能卡住（最近动静已过 `COVEY_PROGRESS_STUCK_MINUTES` 分钟，默认 30；等回答、模型请求重试、排队等空位时不算，最新一步是一条还在跑的命令时也不算，见下一条）：状态行最后一行写 `stuck_words`，代替“较长时间没有新进展”；`TaskMonitor` 的 `stuck` 回调在它变化时调一次 `Cards.show_stuck`：在 `activity_more`（老卡在 `activity`）后面插入 `stuck_look` 按钮行（`cards/task_status.py` 的 `look_element`，kind `bridge_job_look`），有了新动静或这一轮结束（`delete_stop_action`）时删掉；每个方向只试一次。
- 在等命令（`progress` 的 `command`，网页的进度里也有，没有时不带这个键）：最新一步（整棵执行过程树里最新的）是一条还在跑的命令（执行命令、后台命令）时，这一轮不算可能卡住，不管跑多久：状态行首词仍是蓝色的“在做”，“现在：”行末写 `command_words`“在等一条命令跑完 · 已 38 分钟”（跑满一分钟起，代替“N 分钟前在跑…”和“较长时间没有新进展”），没有看看按钮；看板、手上的事、置顶、快照写“在等一条命令跑完”（跑满卡住的分钟数起，不写分钟，免得每分钟重画）。`TaskMonitor` 从执行过程算出命令开始的时刻，写进任务记录的 `command_since`（命令不再是最新一步时清空一次），读记录的地方照它判断。判断有边：Bridge 已知后端进程退出（记录的 `process_alive` 为假）而命令的结束没记下来时不再算，任务记录的 `health_checked_at` 超过 `COMMAND_FRESH`（10 分钟）没更新（任务进程不在了）时也不算，这两种情况回到 30 分钟的规矩；一轮结束执行过程把还在跑的步骤收成“结束状态未确认”。别的安静的一轮照旧 30 分钟。
- 回复已经给了、后台任务让任务开着（`core/progress.py` 的 `background_tasks`；Claude 后端）：执行过程里还在跑的后台命令（“后台命令”）和后台子任务（`agent_state` 的 `background` 为真，Claude Code 自己放到后台的也算：`backends/claude/events.py` 在发现时补发一次状态），且没有前台的工具步骤、没有模型在等的子任务、执行过程没结束时，“现在：”行下面多一行灰字 `background_words`：“还在等后台 2 个：跑全量测试、等 CI”（描述是模型给命令写的说明或子任务的描述，不写命令本身，每个最多 `BACKGROUND_CHARS` 字、转义；没写说明的命令按种类说“跑测试”，都没有时只写个数；最多点名 `BACKGROUND_NAMED` 个，多的写“还在等后台：A、B、C 等 5 个”）。不是橙色，结束了就没有。`TaskMonitor` 把描述放进 `Facts.background`。
- 排队中取消写“⏹ 已取消排队”；回答过的提问任务，状态行随正文改为“↪ 已收到回答”。
- 有在做的一步（检查点的 `steps`，见下文）时，状态行用它代替阶段（`step_text`）。

### 执行过程

- `core/trace.py` 的 `ToolTrace.accept` 把事件聚合进快照并返回是否有变化：结束的步骤不被重放的开始事件改回运行中，`output_tokens` 只增不减。
- worker 把本任务的检查点路径交给 `ToolTrace.checkpoint`：命令里含这个路径（Codex `commandExecution`、Claude `Bash`），或文件工具的路径正好是它（Claude `Write`/`Edit`、Codex 只改这一个文件的 `fileChange`）的步骤记成“更新检查点”，不存命令内容。
- `ToolTrace.finish` 把仍在运行的步骤记为 `unconfirmed`；任务被停止时快照另记 `stopped`，这些步骤画成 ⏹ 已停止（存的状态不变）。
- `core/trace_store.py` 存 `traces/<任务键>.jsonl`，每行只写变化，另一进程写过的文件接着追加；维护把已结束 30 天的压缩成 `.jsonl.gz`（`compress_trace`），`load_trace` 两种都读，之后再写时另起一个以整行快照开头的 `.jsonl`。

`cards/trace_view.py` 的 `TraceView` 只从快照生成元素：

- 面板是带框的折叠面板 `trace_details`，第一行 `trace_metrics`（运行中写“N 步”），然后是 `trace_body`。运行中也收起（`PanelModel.expanded` 只有老卡翻页重画时为真）：状态行已经写着“现在：…”。标题运行中固定写“执行过程 · 在做”，结束后写“执行过程 · N 步”：标题只能随整个折叠面板一起换，换了会把用户的展开状态翻回去，所以运行中只按 ID 换里面的元素，结束时标题变了才整块换一次（`incremental_actions` 比的是折叠面板除内容和 `expanded` 以外的部分，`frame`）。`TraceView.panel_model` 返回 `PanelModel`，`panel_json` 由它画出标题和第一行。中断卡 `panel_model(timed=True)` 在步数后加用时。
- 主任务和子任务的条目按开始时间排在一起，分成进展说明、子任务和机器人的行、两条之间的一段操作（`trace_items`）。
- 一段操作（`run_elements`）不超过 `SHOW_OPERATIONS`（8）项全列，更长的只列最后 `KEEP_OPERATIONS`（3）项，前面的收进 `ops_<哈希>` 折叠；运行中的始终列出。步骤一行：✓ ● ✕、种类、耗时或状态、开始时间、非 0 退出码，下一行灰字摘要。
- 进展说明是 `note_element`（蓝底）。代码在画的时候还原（`literal_code`，快照里是转义过的文字）；`text_chunks` 优先切在换行、句末、空格，不切开实体；子任务最后一条说明超过 `FOLD_CHARS` 时其余放进折叠；同一子任务连续的条目只在第一条写名字（`assemble`）。
- 标题为“机器人”的说明（`app/detached.py` 写的重启提示）单独一行；`name` 为 `context_compaction` 的工具步骤（压缩上下文）也单独一行（`compaction_line`）。
- 不分页。预算按 CardKit 整卡 30 KB、200 个元素留（快照里的 `TRACE_BYTES`，旧快照按 `OLD_SNAPSHOT_BYTES`；`TRACE_ELEMENTS`）。放不下时 `fit_items` 从最早的一段起换成一行计数；还放不下时 `fold_older` 从最早的起收进 `trace_older` 折叠（每条一行首句）。

### 刷新与合并

- 每个任务两个 `CoalescedUpdates`（`core/updates.py`）：`card_updates` 放答案、问题、重试状态，`trace_card_updates` 只放执行过程；同键只留最新，紧急的立即执行；任务结束时两个都关，不论最后一次写卡成败。
- 执行过程刷新由 `app/task_details.py` 的 `TaskDetails`（`queue_trace`、`trace_updates`）做：面板、任务详情的文字（`StreamingReply.live_details`）和“输出”的数字（`card_figures`，存进 `StreamingReply.figures_parts`）放进同一次 `batch_update`，没变的不写。所以运行中 token、速度、上下文只在执行过程有变化时更新，一直在走的是“已运行”。
- 任务详情不上卡（`task_details_panel` 总是 None）：新卡没有这个面板，`StreamingReply.details_panel` 为假，“底部”锚“本轮请求”（`details_anchor`）；之前发出的卡上有的，留着原来的字，不再写。

### 检查点步骤与改动

检查点的可选字段 `steps`、`delivery`（`core/checkpoint.py` 的 `checkpoint_steps`、`checkpoint_delivery`）：

- 写给模型的约定（`core/checkpoint.py` 的 `checkpoint_rules`）：每轮都带 `CHECKPOINT_RULES`（何时写、字段、要主人定时怎么写、恢复和文件），`steps`、`delivery`、`show_images` 三条用到才带（`app/runs.py` 的 `Runs.checkpoint_rules`）：话题新会话的第一条消息全带；续接的轮次在这个话题有一轮列过步骤时带 `steps`，有一轮改过代码（写过 `delivery`，或 Bridge 记到它改了文件）时带 `delivery`，主人这句话说到二维码、截图、登录时带 `show_images`；管家和负责人的轮次三条都不带，主人对管家说到图时这一轮的消息里带 `show_images` 那条（`_images_note`）。Bridge 读这些字段不看规则带没带，检查点的 JSON 不变。
- 容错：类型不对的项丢掉，未知状态和第二个 `doing` 当 `todo`，最多 `MAX_STEPS` 步。
- `clock_steps` 按 Bridge 的时钟记每步第一次 `doing` 和 `done`；有过步骤后 `persist` 带 `steps`、`step_times`，没有过不写这些键。
- 状态行、状态卡（`app/status_cards.py`）、快照（`app/butler.py`）、`topic_read`（`app/butler_tools.py` 的 `step_lines`）用同一套说法（`step_words`）。
- `delivery` 每项读原文，最多 `DELIVERY_CHARS` 字。改了代码的轮次（`shows_change`：有提交号，或 `core/diff.py` 记到这一轮改了文件且 `delivery` 有内容），答案卡、结论卡在产物折叠下面、灰框上面加“改动”折叠（`change_element`，`CHANGE_ID`）：标题“改动 · ”加 `change_summary`（最多 `CHANGE_LINE_WIDTH` 显示宽度），点开是提交（`delivery_commits`）、部署、验收三行原文。回带卡加上它超过 `ANSWER_CARD_BYTES` 时分几张卡发（`cards/relay_card.py` 的 `relay_pages`，`cards/pages.py`），每张都有头像行，改动在最后一张，放不下时跟一张同样头像行的尾卡。不改代码的轮次卡片 JSON 不变。
- 验收三行（#21）：检查点的 `acceptance`（`core/checkpoint.py` 的 `checkpoint_acceptance`：what、where、how，规则一行 `ACCEPTANCE_RULE` 接在 `CHECKPOINT_RULES` 末尾）有 what 或 how 时，`app/answer_cards.py` 的 `notes` 先放 `acceptance_element`（`ACCEPTANCE_ID`：改了什么、去哪看、怎么验三行，标签灰字），去哪看取 `acceptance_link`（第一个 https 或 applink，本机地址不算），没有就是灰字 `LOCAL_ONLY`“只做了本地验证”。没有这个字段的轮次卡片 JSON 不变。
- 完成标准（`core/criteria.py`，#22）：话题 topic.json 有 `criteria` 时，`app/answer_cards.py` 的 `notes` 拿检查点的 `criteria` 逐条对（按原文，对不上且条数相同时按顺序），没标满足的画成 `criteria_element`（`CRITERIA_ID`：灰字“完成标准”一列，每条一行，“没满足”用可能卡住的橙色，“没查”灰色），放在改动下面、灰框上面；回带卡同样（`facts["notes"]`）。都满足或话题没有标准时卡片 JSON 不变。
- 提交号由 Bridge 核对（`app/answer_cards.py` 的 `absent_commits`，#18）：话题不在设备上、有本机工作目录时，`core/diff.py` 的 `missing_in_repos` 在工作目录的仓库和话题用到的各仓库（worktree 和检出，写的在前，最多 `COMMIT_DIRS` 个目录）里各跑一次 `git cat-file --batch-check`，查 `delivery_commits` 里的十六进制提交号（每个 `<id>^{commit}`，每次限时 `COMMIT_CHECK_SECONDS`，不拿锁、不调模型、不懒取远端对象）；哪个仓库里都没有的记进 `change` 的 `not_found`，卡上在改动折叠下面加一行 `CHECK_ID`（“提交号对不上：写过的仓库里没有 …”，“提交号对不上”用可能卡住的橙色），展开后那个提交号后面写 `ABSENT`。不是仓库、没有 git、超时或输出读不懂时当作查不了，什么都不写；短号同时对上几个对象的不算没有。都找到时卡片 JSON 和原来一样。回带卡紧接着问同一批提交时用答案卡那次的结果（同一批目录和提交号缓存 120 秒），不再跑一次 git。

### 停止与结束

- 停止的第一次写卡是 `Cards.show_stopping`：一次 `batch_update` 改状态行并删按钮行；没有按钮行的旧卡或批量写失败时分两次写；状态行写不成时才在正文写“📌 阶段：正在停止”。点了停止以后 `TaskMonitor` 不会用“执行中”盖掉“正在停止…”（`Cards.update_card_activity` 在卡片锁里看 `cancel_requested_at`）。
- 运行结束时 `TaskMonitor.stop` 不再写卡，worker 用 `Cards.show_outcome(…, timed=True)` 一次 `batch_update`：状态行写结局（`outcome_line`：有结果、已停止、停住了、等你，`cards/task_status.py` 的 `marked` 画成带颜色圆点的状态词）和结束时刻（`Cards._ended_line`），删按钮行，结束执行过程面板，“输出”换成结束的数字。没有按钮行时先写面板再写状态行。之后 `finalize_trace` 只剩任务详情要写，然后才准备和发出结果，状态行不会空着。
- 用时是 `core/metrics.py` 的 `run_seconds`（起点 `run_start`，终点 `execution_finished`），写法 `took_text`；答案卡、回带、状态卡和 Pin 用同一个函数。
- 写成后 `StreamingReply.outcome` 记下结局，`Cards.render_stream_final` 据此让正文不重复结局；没写成时正文以结局开头。

### 追问只发答案卡

话题里的追问（`app/submission.py` 的 `quick_hold`：主人在话题串里或网页上（消息 id `web-<op_id>`）发的、话题答过至少一次（`follow_up`）、马上开工的一轮，话题自己的角色，回答单独发在串里，不是代答话题）先不发任务卡，和管家那一轮一样记 `job.card_deferred`，另记 `card_after` 秒（`COVEY_QUICK_ANSWER_SECONDS`，默认 10，0 关）。任务卡什么时候出：

- 状态行每次刷新时（`Cards.reveal`、`held_due`）：跑满 `card_after` 秒，或者执行过程有了第一步（工具调用、进展说明、子任务），当场发卡，已有的步骤随即画上（`queue_trace`）。
- 模型问问题时先发卡再问（`app/interaction.py`，`Cards.reveal_held`）。
- 结束时还没发卡的（`core/jobs.py` 的 `quick_held`）：完整、已授权的回答（`app/worker.py` 的 `quick_answer`）只发答案卡，答案卡底部带“换配置”“回退”（`topic_controls`，任务卡上本来有它们）；其余结局（停住、停止、等你、没授权）先发卡，后面照常写在卡上；答案卡没发出去的也补发任务卡，写上没送达和“重新投递结果”（`TurnDelivery.deliver`）。
- 发出的回答仍是经 `verified_reply` 授权、存下来的那份（规则 5），任务卡仍只由 `Cards` 写（规则 3）。

### 卡片摘要与回答开头

- 话题列表显示最新一条消息的预览，任务卡的 `config.summary` 跟着任务走：`build_stream_card` 发卡时写“🕒 … 排队中”（任务已开始时的替换卡写“⏳ … 执行中”）；检查点阶段或原生问题变化时 `Cards.show_running_summary` 只 `PATCH settings` 的 `summary`；`show_outcome` 写“话题号或 #任务号 · 结局 · 用时”（`task_card_summary`）；`StreamingCard.close` 关流式时带上它。header 发出后只有整卡 `PUT` 能改，任务卡不整卡替换。
- 单独发出的回答也是卡片（`cards/review.py` 的 `answer_card`，摘要由 `answer_summary` 写），见[投递、重投与 outbox](architecture.md#投递重投与-outbox)。
- 回答单独发出时正文写成“**结论**”下一行开头（`TurnDelivery.delivered_note`；有管家时话题里的答案卡已发在话题串里时正文留空）。
- 回答开头跳过“## 结论”这类标题（`answer_lead` 的 `GENERIC_HEADINGS`）。

### 之前发出的卡

新 Bridge 按 `StreamingReply` 记下的元素决定怎么写，旧卡照旧能写；旧 release 的任务进程不认新字段，照旧写 `activity`、`task_figures` 等原 ID，新卡都有。

| 卡的形状 | 识别 | 怎么写 |
|---|---|---|
| 有输入/输出（当前，标题行） | `io_box` 为真 | 如上 |
| 状态块加数字格 | `dashboard` 为真、没有 `task_io` | 写数字格 `task_figures`（`figures_element`，`status_block` 里嵌套的格子由 `find_element` 按 ID 找）；状态行照旧带“已运行” |
| 状态行分两部分 | `status_rows` 为真 | `activity` 和 `activity_more` 分开写 |
| 一行状态 | `status_rows` 为假 | `activity` 写整行；`set_record_activity` 只写 `activity` |
| 正文在上方 | `dashboard` 为假 | 回答单独发出时 `Cards.lead_first` 把正文移到 `current_question` 前，“正文后”的元素改插在 `task_details` 之后（`body_anchor`） |
| 带页码框的执行过程 | 面板形状不同 | `incremental_actions` 整块换 `process_panel`；页码框仍能点（`interaction.py` 的 `show_trace_page`），画唯一的一页 |

## 卡片控制器与动作队列

启动卡、换配置等表单卡上的点击由控制器在卡片回调后台线程外处理，不经任务卡动作表：

| 控制器 | 管什么 | 按钮名或 kind | outcome |
|---|---|---|---|
| `WorkspaceManager` | 工作区管理卡、启动卡的“添加或管理工作区”、管理卡打开的目录浏览页 | `bridge_wsm_<token>:<revision>:dir-<动作>` | `workspace_manager` |
| `LaunchModelPicker` | 启动卡的模型、预算、工作模式、工作区、运行方式 | 值带 `option` | `launch_option` |
| `DirectoryPicker` | 选择其他目录和目录浏览页（`app/directory_picker.py`） | `bridge_dir_<token>:<revision>:<动作>` | `directory` |
| `HarnessSwitch` | 任务卡的换配置和更换卡 | `bridge_switch_` 开头 | `harness_switch` |
| `RewindCards` | 结束任务卡的回退和回退卡（`app/rewind.py`），不转给任务进程 | `bridge_rewind_` 开头 | `rewind` |

- 启动卡和换配置卡没有下拉框：清单每行是一个 `collapsible_panel`（`element_id` `set_<项>_<token 前 4 位>`：客户端按 ID 记住开合，换 ID 才能让选完的一行收起），每个选项是一个 `interactive_container`，值带 `option`，`app/card_routing.py` 把它当作事件的 `option`。每个选择都整卡重画。
- 旧卡兼容：整卡换成选项页的 `bridge_launch_page`、`bridge_switch_page`（值带 `page`）重画成那一行展开，`page` 为空即全部收起；更早的下拉框回调照旧。
- 都继承 `CardActionQueue`（`app/card_queue.py`）：各自一个有界队列，第一次点击时起 worker。队列满时丢弃这次点击并记 `busy`。
- 表单内容要变时整卡原位替换，经 `app/launch.py` 的 `rebuild_launch_card`：卡锁内先登记新表单（新 token）再 PUT 整卡；只换卡面用 `app/form_cards.py` 的 `replace_form_card`。CardKit 确认后旧表单记 `replaced`（`replaced_by` 指向新表单），旧卡上点“开始任务”按新表单启动；未确认时新表单记 `unpublished`，永不启动。
- 两卡式或 schema 3 之前的启动卡（`retired_form`）不再启动任务，点了只回复一句，什么都不改。

不属于任何话题的主人卡片在话题路由之前由 `PANELS` 表处理（见下节第 3 步），kind 都在 `ACTIONS`，除欢迎卡的「发这句」外都不建任务。它们多数由机器人菜单（`app/bot_menu.py`）发到主人私聊；菜单同一项再点、它的卡还是私聊里最新的顶层消息时，卡经自己的记录原地收起或展开（`MenuPanel.toggle`）。回带卡上“这一轮中途的汇报”展开后点正文（`bridge_fold_close`，`app/fold_close.py`）也在这里认领：Bridge 用发出时记下的卡（`fold-cards/`，同一张卡也在 `answer-cards/` 记着时用那份）在回执之后整卡改写，折叠换下一个 ID（`<element_id>_s<n>`）收起，不建任务。

| 卡 | kind | 处理（outcome 以处理者的名字开头，群成员点了是 `owner_only`） |
|---|---|---|
| 代答设置卡 | `bridge_proxy_*` | `ProxyPanel.click`：`proxy_panel`；旧 revision 且动作已不适用 `proxy_stale`，不是最新那张 `proxy_expired` |
| 重新发送卡 | `bridge_proxy_resend` | `ProxyResend.click`，见[重新发送卡](proxy.md#重新发送卡) |
| 群管理卡 | `bridge_group_*` | `GroupManager.click`；启用、停用经 `Access.switch`，在卡锁之外，然后整卡重画；已启用的群的“设为只读”“取消只读”（`bridge_group_members`，带 `chat`、`mode`）经 `Access.set_member_mode` |
| 启用确认卡（主人第一次在群里 @ 时发到私聊，`GroupManager.ask`） | `bridge_group_confirm`（带 `chat`、`on`、`name`） | `GroupManager.confirm`：群还在等主人的话时经 `Access.switch` 启用或停用（`group_confirm_on`、`group_confirm_off`），已经定过的什么都不改（`group_confirm_decided`），没有群记录 `group_unknown`；卡没有记录，应答后（`after_ack`）用 `patch_card` 整卡换成结果，换不了就在私聊里说一句 |
| 已下线功能的旧按钮（敏感词脱敏的 `bridge_redaction_*`、`bridge_dashboard_rotate`、`bridge_main_redaction`、`bridge_main_topic_redaction`，独立复核；记忆的“升为全局”`bridge_memory_promote`、`bridge_memory_to_global`，旧记忆页的工作区行 `bridge_memory_workspace`，专项卡上删记忆 `bridge_project_mem_del`，早晚汇总的“你反复说的”`bridge_digest_remember`（`MEMORY_RETIRED`）；设置卡的工作区行、运行方式行、保存用途和记住的工作区 `bridge_main_workspace`、`bridge_main_topic_workspace`、`bridge_main_topic_run_mode`、`bridge_main_notes`、`bridge_main_untrust`（`WORKSPACE_KINDS`）等） | `MOVED_CARDS` 里的 kind，不在 `ACTIONS` | `card_actions` 在任何面板认领之前只回一句已下线（敏感词脱敏是“敏感词脱敏已下线，这次点击没有改任何设置。”），不改设置、不改记忆、不重画卡片（`extension_missing`）；网页旧页面改工作区那几行同样只回这一句；见[已下线的功能](legacy.md#已下线的功能) |
| 设置卡 | `bridge_settings_tab`（带 `tab`、`from`、`token`） | `SettingsHub.click`：`settings_tab`、`settings_fold`、`settings_unfold` |
| 设置卡「记忆」标签（`COVEY_MEMORY` 没关） | `bridge_memory_open`（带 `scope`：`review` 或 `rules`、`page`：翻页）、`bridge_memory_pin`（现在是待审核的“采纳”：`key` 是 `review:…`、`op` 是 `accept`；沿用原 kind，不新增）、`bridge_memory_delete`（待审核的“不要”：`op` 是 `reject`；没网页时规矩的“收起”：`key` 是条目、`op` 是 `ask`、`confirm`、`cancel`）；带 `on` 的 `bridge_memory_pin`（旧记忆页的“常驻”）只接旧卡片的点击，回“这个选项已下线”、不改任何东西；都带 `token`、`revision` | `MemoryPanel.click`：`memory_tab`；同一事件再投递 `duplicate`。采纳、不要走 `web_memory.review_decide`，收起走 `memstore.archive`，都在卡锁之外，整卡重画在应答之后（`after_ack`） |
| 晚间汇总的“整理记忆”（`COVEY_MEMORY` 没关） | `bridge_digest_memory`（带 `run`、`op`：只有 `undo`；旧卡上的 `confirm`、`wrong` 另带 `project`、`entry`） | `Digest.claim`：`digest_memory`，值不对 `digest_memory_ignored`，旧的 `confirm`、`wrong` 回一句已下线、不改记忆（`digest_memory_retired`）；只认主人。`undo` 应答后撤销这次收起（`memory_tidy.MemoryTidy.decide`），再照汇总卡保存的样子 `patch_card` 重画 |
| 手上的事 | `bridge_overview_dismiss`（带 `topics`） | `Overview.claim`：只改看板记录，应答后由 `tick` 整卡重画 |
| 不管了留下的那行灰字（手上的事、看板、早晚汇总）的「撤销」 | `bridge_overview_undismiss`（带 `taken`：{话题: 撤下时的 `at`}，早晚汇总和看板上还带 `from`） | `Overview.claim` → `Overview.undo`：只认主人；`undismiss` 把记录里 `at` 还对得上的话题放回（撤下之后又撤了一次的、问题已答或已收的，什么都不动）；应答后 `tick` 整卡重画，来源卡（`unhanded`：看板整卡按新读数重画，早晚汇总按保存的样子重画）也跟着画 |
| 答完的问题卡下的「去看板」 | `bridge_board_open` | `Digest.claim`：只认主人；有看板、点的是本聊天的卡时（`board_open`），应答后（`after_ack`）发一张新的看板卡（同菜单的「看板」，点击的 `event_id` 作键）；否则 `board_open_ignored` |
| 飞书应用待补 | `covey_app_setup`、`bridge_app_setup_skip` | `AppSetup.claim`（表的最后）：`app_setup_start`、`app_setup_busy`、`app_setup_stale` |
| 欢迎卡（新部署只发一次，`app/welcome.py`） | `bridge_welcome_try`（带 `example`，示例的序号；`bot_only`） | `Welcome.claim`：只认主人（别人点了 `owner_only`）；同一事件再投递 `duplicate`，不是发出的那张卡或序号不对 `welcome_stale`，这句已经点过 `welcome_again`；否则 `welcome_try`：锁内记下这句在发（`tried`），应答后整卡重画成「已发出」，再把这句作为一条文字消息发到私聊顶层，交给 `handle_event` 当作主人的消息处理（有管家是管家的一轮，没有是一个新话题和它的启动卡）；没发出去的这句可以再点，可能已发出的不再发 |

- 设置卡一张卡一条记录：点击在锁下核对后先应答，再整卡重画成目标标签，sequence 从这条记录取。点当前标签收起（`folded_hub`），再点展开。旧版每个面板一条记录的卡由 `takeover` 接过来，旧记录标成 `moved`。
- 代答设置卡的慢操作在点击里只记视图，结果在后续里画。

## 卡片动作表与分发

`app/card_actions.py` 的 `ACTIONS` 是任务卡按钮 kind 和失败提示里的名称（停止、重试、补充及其状态/重新排队/撤回、暂存消息、回答及其核查/状态、执行过程、产物），另有上节的主人卡片。

- 会建任务的是 `bot_only`：只由 Bridge 处理，从不转发给任务进程。包括重试（`bridge_job_retry`）、重新排队（`bridge_steer_requeue`）、暂存消息（`bridge_note`）、旧问题卡的只发补充说明和先问一句（`bridge_question_skip`、`bridge_question_ask`：都按只写补充说明交回答）、停止自动重试（`bridge_job_retry_cancel`）、看看（`bridge_job_look`，`app/stuck_check.py`：不建任务，但要由 Bridge 从外面看这一轮的任务进程，不能交给可能卡住的它）、复制诊断（`bridge_job_report`，`app/diag_summary.py`：不建任务，读记录回一段诊断摘要，这一轮的任务进程可能已经不在了）。欢迎卡的「发这句」（`bridge_welcome_try`）也是：它在话题路由之前由 `app/welcome.py` 认领，示例作为主人的消息进入私聊，开管家的一轮或一个新话题。
- 卡片事件只有一个消费后台线程，它把点击交给 `app/card_lanes.py` 的分道：同一话题按到达顺序逐个处理，跨话题并行，不在话题里的卡片各占一道。
- 处理器返回即点击已回执（`card action` 那行的 `elapsed_ms`）。`after_ack` 留下的慢活在本话题的后续道里按序执行（记 `card follow-up`），下一次点击不等它，失败按这次点击的口径回复。不在分道里（文字回答、任务进程、测试）时 `after_ack` 当场执行。停止服务时只跑后续道，`drain` 等已开始的做完或超时。
- 飞书的卡片回调 3 秒内要应答，所以点击里只做决定和改被点的卡，发新卡、发通知都放进后续。例如开始任务的任务卡（`Submission.release_waiting`；回执在另一条后续道同时写，`after_ack(beside=True)`），重新排队执行（锁内记 `requeue_state=submitting`，后续未完时再点回复“正在重新排队”），开新话题（同一事件重投用同一 token 只开一次）。任务卡总在 `enqueue_reply_job` 之前发出，任务不会在卡片出现前开始。
- 开始任务点击之后、后续之前在话题里发的消息，`submit_topic_job` 先放出还在 `waiting_workspace` 的消息再排自己；这时重启的，由 `release_started_launches` 放出。

`app/card_routing.py` 的 `handle_card_action_event` 对每次点击在 `bridge.log` 记一行 `card action`（格式见[飞书客户端验收](testing.md#飞书客户端验收)）。分发顺序和 outcome：

1. 操作者不是主人，聊天也不是已激活的群（`Access.allows`，见[谁被服务](architecture.md#谁被服务)）：`not_owner`。
2. `app/unified.py` 的 `EARLY` 表按事件依次认领（每个控制器的 `claim(ctx, event, raw, owner)` 返回 outcome，不是自己的点击返回 None）：启用的扩展的功能对象（`claim`，`core/extensions.py`）、`workspace_manager`、`launch_option`、`directory`、`harness_switch`、`rewind`。群成员点工作区管理或目录选择是 `owner_only`：控制器在卡上提示“仅机器人主人可以…”。
3. `action_value` 解析不了：`bad_value`；不是启动表单提交、kind 又不在 `ACTIONS`：`retired`（旧设置卡、菜单卡在此丢弃）；再由 `PANELS` 表按 `action_value` 依次认领（上节的主人卡片），都不经话题路由；卡片消息不在话题别名里且查不到：`unknown_message`。
4. 话题绑定的 Harness 已下架：`retired_harness`。
5. 按钮 `topic_id` 与卡片所在话题不同：`other_topic`。带 `job_id` 的按钮交给该任务记录的 Harness（换过模型的话题，旧任务的执行过程和产物仍由原 Harness 处理）；它在最近一次换配置之前、kind 又会建任务：`stale_job`；按钮的 `harness_id`/`binding_revision` 与当前绑定不一致：`stale_route`；两者都回复一句说明。
6. 任务进程活着且不是 Bridge 自己处理的点击：`forwarded`（`app/runner_ipc.py` 的 `forward_card_action`）。
7. 其余交 `BridgeApp.handle_card_action_event`，按 `ACTIONS` 的 `handler` 分派：`handled`。处理器抛 `ValueError` 回复“<名称>未完成：<原因>”，其他异常回复暂未完成，outcome 仍是 `handled`；只有前 6 步抛异常才是 `error`。

### 已下线的按钮

下线的按钮留在 `ACTIONS` 里（`bot_only`，`handler` 是 `controls.retired_action`），旧卡上点了由 `app/task_controls.py` 的 `TaskControls.retired_action` 在话题里回 `RETIRED_TEXT` 里的一句，别的什么都不做：

| kind | 原按钮 | 回复 |
|---|---|---|
| `bridge_fork` | 💬 侧聊、🔀 分叉 | 要从这个话题分出新话题，在私聊里跟管家说 |
| `bridge_compact` | 📦 压缩上下文 | 会话快到模型窗口时，Claude Code 会自己压缩 |
| `bridge_turn_export` | 📥 下载请求与结果 | 下载请求与结果已下线，这次点击没有打包任何东西 |
| `bridge_revisit_hint` | 答案卡下的「让 Covey 30 分钟后回看」 | 已下线，没有设回看：想让它过一会儿回来看，在这个话题里直接跟 Covey 说（检查点里的 `revisit` 不动） |
| `bridge_project_triage_wrong`、`bridge_project_triage_fix` | 专项卡“分流记录”里的「不对」和它的表单「提交纠正」 | 已下线，没有改任何东西：要纠正负责人替你答的，到那个专项话题里直接说 |

看板上的四个小按钮不走这条（看板的卡不属于任何话题，它们在 `ACTIONS` 里没有 `handler`，和看板的别的 kind 一样）：`bridge_board_seen`（记为看过）、`bridge_board_revisit_cancel`（取消到点回来看）、`bridge_board_project_go`（继续专项）、`bridge_board_recover`（检查并继续）仍由 `Digest.claim` 认领（`cards/board.py` 的 `RETIRED_KINDS`），只认主人，应答后（`after_ack`）回 `RETIRED_TEXT` 里的一句并按新样子重画看板，什么都不做（`board_retired`）：看过点进话题即可，取消到点回来看和检查并继续到话题或任务卡上做，专项的「继续」在专项卡上。

设置卡的 `bridge_main_start`、`bridge_main_start_row`（直接开工）不走这条：它们仍由设置卡认领（`app/butler_panel.py`，`RETIRED_KINDS`），回一句“直接开工已下线”并重画，不改设置。记住的工作区（`bridge_main_untrust`）和工作区那几行在 `MOVED_CARDS`，见上表。

### 设置卡配置页的格子版式

配置页一段里的各项排成格子（`cards/butler_panel.py` 的 `tile_lines`、`cards/launch_config.py` 的 `setting_line`），顺序不变：新话题 Harness、供应商、模型、思考预算 / 工作区、运行方式；管家 Harness、供应商、模型、思考预算 / 工作区、压缩阈值；专项负责人四项；更多是子 Agent。每段里 `FOLDED` 的几项（且是平常的值）不排在格子里，排在段末“更多：…”折叠里，顺序同上。（网页设置页读的 `lines` 仍是原来的三行分法，没变。）

- 每项还是原来的折叠行 `setting_row`（`cell=True`：灰色标签后面跟当前值，同一行，格子只有一行高），点开列选项，点一个选项照旧记下并整卡重画，现在是一个带细边框（`TILE_BORDER`，灰色、圆角 8px）的格子，下拉箭头都在格子右缘；去掉了原来整块灰底。不用下拉框（`select`）的原因不变：iOS 飞书的 select 是全屏选择器，取消按钮在状态栏下面（见 `cards/launch_config.py` 的模块说明）。
- 两格一行，`column_set` 的 `flex_mode` 取 `LINE_FLEX`（`stretch`），两个等宽（`weighted`）的 `column`：卡片宽时左右并排，窄（话题侧栏约 250–300px，手机）时自动上下叠成单列，所以不会把“Claude 自带”折成“Claude 自 / 带”。选的值很长的项（显示宽度超过 `WIDE`，如工作区、子 Agent）单独一行占满；落单的一项占半行（另一半放空的 `column`），格子不错位。`bisect`（任何宽度都两列，窄屏会断词）和 `flow` + `auto`（宽度参差）都试过，不用。
- 没展开的段是一个整块可点、带边框的容器（`closed_section`）：标题和蓝字“展开”同一行，下面一行灰字摘要（`summary`：只列值、不带标签、不带供应商，惯常的选择——默认工作区、原目录、压缩 20 万、交给管家——不列）。容器和展开的段共用同一个按钮 kind（`SECTION`），旧卡上的“展开”按钮照旧能点。
- 元素数比原来少（新话题段约少 12 个节点），仍按 `CARD_NODES` 的预算由 `fits` 裁选项。
- 卡片 JSON 变了，要真机验收（手机、话题侧栏、深色）：格子换行、点开的选项宽度、点开后其余格的位置。

## 卡片的客户端限制

- 不要单独替换表单里的一个组件，表单内容要变时整卡替换（来由）。
- 同一张卡上兄弟元素刷新时，客户端可能清掉表单里没提交的输入，所以实时问题的表单发在独立问题卡上。
- 卡片在确认框打开时重画，“确定”不会到达机器人，所以会重画的卡上的按钮不用 `confirm`。
- 这些只有真实客户端能证明：改卡片表单或回调时按[飞书客户端验收](testing.md#飞书客户端验收)验证，卡片 JSON 有变化时在发布记录里标“客户端待验”。
- `tests/golden/cards.json`（`test_card_golden`，各种卡片的固定样本）只要有 diff，就是卡片 JSON 变了，按上一条做客户端验收；失败信息列出变了的样本和第一处不同的位置。它只覆盖固定的样本：没有 diff 不等于卡片没变，改了哪个构造函数还要自己看那张卡；样本没画到的分支（新卡片、新状态、新按钮）先补样本。

## 添加一个卡片按钮

1. `ACTIONS` 加 kind（`bridge_` 前缀）、名称和认领它的组件方法（`handler`，如 `"controls.stop_action"`：`BridgeApp.handle_card_action_event` 按它分派，没有 kind 的启动表单提交和认领不了的点击再走启动表单、旧卡片提示）；会建新任务的设 `bot_only=True`。话题路由之前就由 Bridge 处理的主人卡片不写 `handler`，在 `app/unified.py` 的 `EARLY` 或 `PANELS` 里登记控制器。
2. 按钮 `value` 带 `kind` 和 `topic_id`（拒绝跨话题点击），与任务有关的带 `job_id`，与问题有关的带 `token`。按钮用 `cards/elements.py` 的 `topic_button`、`button_row`。
3. 处理器收 `(ctx, event)`，认领返回 `True`，拒绝抛 `ValueError`；不要自己重发或换键重试。运行中任务的点击在任务进程里执行，只能改本任务的卡和记录；要新任务时用 `Questions.enqueue_continuation` 请 Bridge 入队，或设 `bot_only`。慢活放进 `after_ack`。
4. 仍在跑的旧 release 任务进程把新 kind 按 `retired` 丢弃，它们结束后新按钮才生效。
5. 测试：`test_structure` 要求包里所有 `"kind": "bridge_…"` 字面量都在表里，且除话题路由之前处理的 kind 外每个都有恰好一个组件方法（`handler`）；每个 kind 和按钮名前缀恰好有一个认领者（`EARLY`/`PANELS` 里的控制器或 `handler`），包里 `bridge_…_` 形式的前缀字面量都要列进 `FORM_PREFIXES`，`BOT_ONLY` 写死在 `tests/test_cards.py`；再写处理器单元测试，补 `test_card_golden` 样本，并做客户端点击验收。
6. 元素 ID 须匹配 `^[A-Za-z][A-Za-z0-9_]{0,19}$` 且卡内不重复，一个不合规整卡被拒（300301）。`tests/test_cards.py` 的 `sample_cards()` 构建并校验每种卡片和面板；新卡片或面板在那里加样本。
7. 下线按钮时 kind 不删：改 `handler` 为 `controls.retired_action`，在 `RETIRED_TEXT` 写一句替代办法（规则 10：已发卡片的 kind 不崩溃、不悄悄开工）。

## 卡片发不出去时

- 任务卡首次创建被 CardKit 拒绝时，`StreamingCard.create` 去掉可选按钮只留“停止”重建一次，其他元素与完整卡相同；仍失败时在话题里说明（[任务卡没出来](reference.md#任务卡没出来)），日志记 `task card missing`。
- 飞书接口失败都记一行 `delivery failed`（类别、错误码、操作、是否可重试、次数和截断的说明），不记请求内容。
- 写卡出口预检（`core/card_limits.py` 的 `check`，`feishu/messenger.py` 的 `refused`）：`patch_card`、`create_card`、`put_card`、`write_card` 发出前都量一次。超过飞书硬上限的不发请求、直接按飞书会回的失败返回（`Failure`，错误码 300305 / 200860，`reason` 照常说清），日志记 `card refused before sending`：任何卡超过 200 个带 tag 的节点；卡片消息（IM 改卡）的 JSON 超过 30 KB。CardKit 的卡不按字节拦（它按渲染大小拒收，任务卡正文最多 28,000 字），只在渲染估算超过 `CARD_COST_BUDGET` 时记日志。超过目标值（195 个节点、29 KB）但没到硬上限的只记 `card over its target`（每种写法每分钟最多一行），不拦。
- 改卡失败（`Messenger.patch_or_tell`）：整卡替换没成时在这张卡下面回一行，写清哪张卡没更新（确定没发出的写“没更新”，可能已生效的写“可能没更新”）和原因（`transport.reason`），同一张卡只说一次（进程内记着、发送键按卡固定），这一行自己不再触发改卡提示。改卡的结果不许丢：`tests/test_structure.py` 的 `CardWriteTest`（门禁 G3）拦裸调用或赋给 `_` 的 `.patch_card(` / `.put_card(`。
- 卡片大小：CardKit 拒绝过大的卡（`200860 card over max size`），按渲染后的行、段落、行内标记和表格单元格算，不按字节：几千行短行会被拒，几万字的一段中文不会。`card_cost` 按字节加每行、每个标记、每个单元格的开销估算，实测的拒绝点都在 358,000 以上。
- 正文只用整卡预算 `CARD_COST_BUDGET`（300,000）减去其他元素（`StreamingReply.others`，加、改、删元素时随时更新）后剩下的部分，截在行尾并注明已截断（`truncate_card_content`）。截断后 `StreamingReply.cut` 为真，结果另以文件或分段消息完整发出。
- 执行过程面板自己也有上限，三样都算：JSON 24 KB（`core/trace.py` 的 `TRACE_BYTES`）、115 个节点（`cards/trace_view.py` 的 `TRACE_ELEMENTS`，外壳的 9 个在内：两层列、折叠面板和它的标题与箭头、数字行、旧卡的翻页提示）、大小 150,000（`TRACE_COST`）。很多很短的步骤（一个字的进展说明夹着一条条命令）字节不多，按上面的算法却很大：以前 23 KB 的面板要占掉整卡 300,000 里的 211,000，正文只剩三分之一。超了先把最早的几段操作收成计数，再把最早的记录收进“更早的 N 条记录”。正文先写得很长、面板后来才长时（Claude 回复以后后台任务还在跑），面板只用正文和其他部分剩下的（`app/task_card.py` 的 `panel_room`，至少 20,000），整卡仍在预算内。
- 正文仍被 200860 拒绝时立刻缩短重写，不计入“连续三次失败停用卡片”；缩到 `MIN_BODY_COST` 仍被拒，说明是卡上其他部分太大，按普通失败计。
- 卡片因连续三次写入失败停用（`failed_off`）后，运行结束时 `Cards.show_outcome` 再试一次结束时的写入（结局、结果、关闭流式），卡不会停在“执行中”；已关闭或已换成替换卡的卡不再写。

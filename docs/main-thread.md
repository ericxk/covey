# 主线程

主线程是私聊模式 Personal Agent 下一个常驻的 Claude Code 会话。主人在私聊**顶层**发的消息都交给它：能直接答的自己答，要干活的开话题区或转给已有话题，主人说“T412 选 A”时替主人答选择题。用户视角见[使用指南 · 主线程](usage.md)，部署开关见[配置](configuration.md)。

## 组成

- **私聊模式开关**：`core/main_thread.py` 的 `enabled`（见「私聊模式」），设置在 `core/main_thread_settings.py`。
- **主线程会话**：`app/main_thread.py` 把顶层消息交给主线程话题，每轮带规则、快照和 MCP 配置（见「会话」）。
- **MCP 工具**：会话里的 stdio MCP 服务 `core/main_mcp.py`（入口 `bin/covey_main_mcp.py`），清单 `core/main_tool_specs.py`。
- **请求通道**：`core/main_requests.py`，一次调用一个请求文件，由 Bridge 进程里的 `app/main_tools.py` 执行（见「工具通道」）。

## 私聊模式

`core/main_thread.py` 的 `enabled` 是唯一的判断，各处都问它。两层：

1. **部署允许**（`available`）：`COVEY_MAIN_THREAD=0` 时不允许：主线程的线程不起，菜单键不认，设置卡没有「私聊」「新话题」页。其他值都允许。
2. **主人选的模式**（`agent_mode`）：`main-thread.json` 的 `mode`，`agent` 是 Personal Agent，`topics` 是纯话题。主人在设置卡「私聊」页第一行切换，马上生效；Bridge 和任务进程每次 stat 一下，变了才重读。保存时同时写 `enabled` 给旧版本。

**默认值只写一次**：Bridge 启动、处理事件之前（`MainThread.settle` → `settle_mode`），文件里没有 `mode` 就写：状态目录里没有任务也没有话题时写 Personal Agent；否则 `COVEY_MAIN_THREAD=1` 且设置没关的写 Personal Agent，其余写纯话题。之后只看 `mode`。

**纯话题**下和没有主线程时一样：顶层每条消息开一个话题；引用主线程以前的卡当作新消息开话题（`core/topics.py` 的 `context_for_topic`），引用“交给一个话题”的主线程卡照旧进那个话题；正在跑的主线程那一轮跑完，按 `MAIN_OFF` 收尾；会话和记录留着；主线程开过的话题不再回带、不再放顶层问题卡副本。群聊两种模式都没有主线程。

## 和话题的不同

不同都在 `core/main_thread.py` 的 `Traits` 表里。调用处只查表，不比较 topic id，也不比较 `Traits.name`（`tests/test_main_contract.py` 的 `NoNameComparisonTest` 检查）。

- **判断**：`is_main_topic(topic_id, chat_id)`，和开关无关。
- **查表**：`traits(topic_id, chat_id)`；任务、记录、topic.json 用 `traits_of`；只有上下文时用 `core/topics.py` 的 `topic_traits`。
- **行**：`TOPIC`、`MAIN`（开关开着）、`MAIN_OFF`（开关已关：结构上的不同照旧，会话上的取消）。

| 字段 | `MAIN` | 读者 |
| --- | --- | --- |
| `switch_config`、`rewind` | 否：没有换配置、回退 | `app/switch_handoff.py` |
| `card_target` | 否：卡片点击不能指向它 | `Topics.target` |
| `numbered` | 否：不占 T 号 | `topic_number` |
| `top_level` | 是：引用回复每条消息 | `MainThread.configure` |
| `listed` | 否：不进看板和手上的事 | `core/board_read.py` |
| `own_turn` | 是：每轮带规则、快照、工具，按阈值压缩 | `main_turn` |
| `answers_in_place` | 是：回答可写在任务卡上 | `app/main_reply.py` |
| `lists_supplements` | 是：补充没有回执卡，停止或被拒时卡上列出 | `app/main_reply.py`、`app/steering.py` |
| `hold_seconds` | 30：没文字的消息等主人的话 | `MainThread.hold_for` |
| `slim_card` | 是：分档任务卡 | `build_stream_card` |
| `wording` | 卡上“本话题”换成主线程的说法 | `worded` |
| `relays`、`has_thread` | 否（话题是）：回带到顶层、有话题串 | `app/relay_targets.py`、`app/pins.py` |
| `linkify_body` | 是：正文里的 T 号做成链接 | `app/topic_links.py` |

`MAIN_OFF` 的会话字段取话题的值。

## 一轮怎么走

### 路由

`app/card_routing.py` 在启用的扩展的 `handle_event` 之后调 `MainThread.handle_event`，主人身份来自 `Runtime.access`（规则 11）。消息的 `root_id`、`thread_id`、`parent_id` 按别名查话题：查到别的话题交给它；查到主线程留在主线程；都没填是主线程的新一轮；填了但谁也不认识就照旧开话题。

主线程话题的 `topic.json` 带 `main_thread: true`，没有根消息和 `topic_number`。每条消息都同步设置：模型和预算下一轮生效；换工作区要等没有任务在跑，并开新会话。

- **补充**：有任务没结束时，新消息用 `deliver_steer` 并进最新一轮，不发回执卡。可能已送达的不重发（规则 6），在主人消息下说一句；卡上已列出的不再各说（`TurnDelivery.listed`）。
- **收尾时发来的消息**：确定这一轮没读到的补充，自动排成主线程的新一轮，和主人刚发一条新消息一样：同一个 message_id 作为这一轮的请求，放置、表情和回答都落在它上面，不开话题串、不说 `SUPPLEMENT_LOST`。“确定没读到”有两种：held 后被拒（从没发出，`drop_held_steers`、`_send_held`），和已发出但 Claude 到结束都没报告读取（`end_unread`）。Claude 交回结果之后到这一轮离开登记表之间来的补充也会 held（后端不再收）；任务离开登记表时（`BridgeApp.process_reply_job` 末尾，任务进程在收件箱停下后再一次）由 `Steering.settle_held_at_end` 当场按被拒结算，不等 steer-hold 线程下一秒的检查（任务进程退出会把它切断）。不确定的（`uncertain`）、主人停止的一轮（`cancel_event`）、没存 `requeue` 的旧记录照旧说一句；卡上已列出的（`listed`）不排也不说。做法：`MainThread.supplement` 在送达记录里存 `requeue`（原文、发给模型的文字、发送人）；发现丢了的一方（`Steering.requeue_lost`）给记录标 `requeue_auto`，在 Bridge 里马上、在任务进程里由 Bridge 在任务进程交还或退出时（`app/detached.py` 的 `queue_lost`）按 `saved_at` 先后各排一轮（`Steering.queue_lost`，走“重新排队执行”的 `_requeue`，`requeue_state`、`requeued_job_id` 保证只排一次，重启后也不再排）。那一轮在没有 Bridge 看着时结束的（Bridge 停着时任务进程退出了，或 Bridge 没来得及排就停了），由 Bridge 启动时在收养任务进程之后补排（`app/detached.py` 的 `queue_stranded`），还在跑的任务进程的留给它结束时排。排队出错的（可能已经排上，`requeue_state` 为 `uncertain`）不再排，在主人那条消息下说一句 `SUPPLEMENT_UNCERTAIN`。已经单独成轮的不再算进旧一轮的“你问”和交接（`app/main_handoff.py` 的 `_supplements`）。同一条消息的事件再来，`deliver_steer` 看到 `requeue_auto` 不再当补充。
- **等主人的话**：转发框里的留言会在转发后一两秒单独发来。没有任务时，没有自己文字的消息（`bare_message`：卡片、合并转发、图片、文件、只有链接）照常入队，但 `start_held_until` 设成 30 秒后（`HOLD_SECONDS`）。这期间主人的文字经 `merge_before_start` 并进请求并立即放行，写进 `asked_text`；到点没等到就开工。这个时间不存盘。

### 直答与转交

规则在 `app/prompts.py` 的 `main_thread_rules`（`AREA_OPENING`、`AREA_RULE`），放在每轮用户消息开头：

- 只读、一两分钟能做完的，主线程自己做、直接答；只问已有结果的用 `topic_read` 读了自己答。
- 改代码或文件、要跑几分钟以上、几件事并行、主人说开话题的，交给话题区：新的事 `topic_start`，同一件事 `topic_send`；两个以上话题区都对得上时用 AskUserQuestion 让主人选。
- 交出去后只说一句放到了哪，不报进度和结论（结论由 Bridge 回带，进度看 Pin）。
- 写给人看的工具说明用主人的语言。

**开工三档**（`starts_at_once`，设置 `start`）：只读的、主人在这条消息里亲口要的、主人记住过的“工作区 × 只读/改文件”直接开工；其余在话题里出预填好的启动卡。

### 提问不开串

话题区布局下主线程这一轮默认不发卡（`(AREA, MAIN)` 格子的 `Row.quiet`）。`placement.quiet` 还要求消息是主人自己的（`om_` 开头）或回答问题后的续接一轮（`CONTINUATION`）。

- **入队**：`app/submission.py` 设 `job.card_deferred`，不发卡；主人那条消息加表情 OnIt（`app/main_reactions.py`，`Messenger.react`）。
- **卡什么时候出**：跑过 `CARD_AFTER`（90 秒）还没结束，任务进程发卡（`Cards.reveal`），进主人那句话的话题串。只试一次。
- **结束表情**（`main_reply.react_end`）：DONE（自己答的）、Get（交给话题区）、CrossMark（失败、被拒）；停止和等回答只去掉 OnIt。失败只记日志。
- **答案**：聊天区新消息，带“你问”块。只做了交接的一轮（`handed_quietly`）不发答案，Get 就是回执；转给了已有话题（topic_send）的，再在主人那条消息下回一个小卡（`hook_topics`，`cards/answer_card.py` 的 `hook_card`）：每个话题一行“→ T453「标题」 ›”，点了打开那个话题串、定位到转入的那条（`hook_rows`，转交记录的 `thread_message_id`）。它是引用主人消息的顶层回复，不开串（D1）；只挂一个话题时，引用这张小卡说话进那个话题。新开或分叉的话题串本来就挂在主人那条消息下面，不列。
- **没卡时的失败**：发一条带“你问”的短消息（`app/quiet_delivery.py` 的 `top_notice`），被拒或停止时列出收下的补充；答案没发出去时另发一条带按钮的（`undelivered_notice`）。
- **问题卡**：还没有卡时，问题卡本身作为聊天区新消息发出（`QuestionDisplay.ask_block_for`）。
- **重试、续跑、续接**：原来没卡的同样延后发卡，表情落在主人原来的消息上（`reaction_message`，`placement.owner_message`）；回答问题后的续接一轮不加表情（`placement.marked` 只认 `om_`）。

### 一次请求一张卡

`app/main_reply.py`。回答先照旧经 `verified_reply` 保存、授权（规则 5）。

- **开了或转了话题**（`main-thread/opened/` 里这一轮成功的记录，`core/main_opened.py` 的 `kind_of`）：写在任务卡上，每个话题一行（“↪ 开了 **T405**「标题」”“↪ 转给 **T403**「标题」：note”），模型的话折进“详情”。
- **自己答的**：按放置表 `answer` 作为聊天区新消息发（`app/answer_delivery.py`）。
- **退回单独发**：没完成、要以文件发、超过卡片字数或写卡失败时，单独发回答卡（重投只发保存的正文）。
- **状态行**（`outcome_words`，`Cards.say_outcome`）：“✅ 已答：开头”“↪ 开了 T444「标题」”“↪ 交给 T442 接着做”“↪ 补充给 T444（它正在跑）”“↪ 分叉出 T445「标题」”“↪ 拆成 2 件”。
- **停止和被拒**：卡上列出收下的补充，说明不会自动重发；官方拒绝时写在 `app/policy_guard.py` 重画的恢复卡上。

**分档卡**（`slim_card`）：没有标题栏，正文、一行状态（`cards/task_status.py` 的 `slim_status_markdown`），其余折进默认收起的“详情”（`slim_details`）；样子见[卡片](cards.md)。

## 会话

`app/main_thread.py`，只在开关开着、任务属于主线程话题时起作用。

- **回复位置**：`MainThread.configure` 在 `build_context` 里（Bridge 和任务进程都走）给 `feishu/message_request.py` 的 `reply_in_thread` 设判断：对主线程消息的回复进那条消息的话题串。开话题区就用这个串。
- **说法**：`worded` 按 `WORDING` 改 Bridge 自己写的文字（“本话题” → “私聊里”），模型的回答和主人原话不动。
- **快照**：紧接规则，`snapshot_groups` 从 `core/board_read.py` 读（不调飞书，不持记录锁）。四组：等你回答、有结果未看（`result`，看板的口径，见[看板与 Pin](#看板与-pin)；等了一天以上的问题和没看的结果可以同时在两组）、进行中、主线程开的话题。存到 `topics/<id>/main-snapshot.json`，下一轮只列变了的行；新会话、压缩过（`compacted.json` 更新）、上一轮没正常结束时带全量。检查点有 `steps` 时写第几步和本步用时。看板撤下的话题不进前三组。
- **MCP 配置**：不改 `runs/` 协议。`main_turn` 算出 `mcp_config(...)` 放进 `ctx.options["mcp_config"]`；`backends/claude/runtime.py` 据此加 `--mcp-config`、`--allowedTools mcp__bridge`，并设 `ENABLE_TOOL_SEARCH=false`（`turn_env`），让工具随会话加载（否则模型会发出不带参数的调用）。不加 `--strict-mcp-config`。所有后端都不走系统级，规则和原则都在 `RunRequest.prompt` 里。
- **压缩**：一轮送达后上下文到了 `compact_tokens`（默认 20 万，0 不压），`compact_main_thread` 写压缩请求（`Compaction.set_requested`）。`/compact` 带 `COMPACT_INSTRUCTIONS`：摘要约 2000 token 以内，只留待办、话题、未决问题和偏好。话题没有自动阈值。
- **空闲压缩**（`app/idle_compaction.py`）：任务进程退出后（`finish_detached` 末尾调 `after_runner`）等 `IDLE_SECONDS`（90 秒）；那时这一轮是 `done`、话题里没有任务、请求没标 `failed`，就起一个只压缩的任务进程（`covey_runner.py --compact-only`），标记写在 `<state>/main-thread/compacting/<话题>/`，不建任务、不出卡。成了写 `compacted.json`，没成标 `failed`、下一轮开头先压。压缩中主人发来新消息时，新一轮（`stop_for_turn`）停掉它、放回请求。上下文到阈值 `FALLBACK`（1.5）倍的、`failed` 的，在下一轮开头压缩（`Compaction.turn_context`）。Bridge 启动时（`MainThread.settle`）清理遗留标记。

## 卡放在哪

### 放置表

`core/placement.py`：布局 × 角色 → 格子 `Row`。调用处只读格子；只有这一轮知道的事（开没开话题、多快、是不是主人自己停的）留在调用处。

- **布局**（`layout()`）：主线程开着是 `area`（话题区），否则 `topics`。
- **角色**（`ROLES`）：`main`（`MAIN`、`MAIN_OFF`）、`topic`。
- **位置**：`TOP` 聊天区新消息、不引用任何卡；`THREAD` 上下文的回复目标（话题串）；`NONE` 不发。
- `plan(state_dir, of)` 返回 `Placement`（`traits`、`layout`、`role`、`row`）。

| 格子 | `reply_top_level` | `answer` | `answer_on_card` | `conclusion` / `stop` | `question_copy` | `ask_block` | `status_card` | `answer_head` | `quiet` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `area` × `main` | 否 | TOP | `ROUTES` | NONE | TOP | 是 | 否 | 否 | 是 |
| `area` × `topic` | 按 `Traits` | THREAD | 无 | TOP | TOP | 是 | 否 | 是 | 否 |
| `topics` × 任意 | 否 | THREAD | 无 | NONE | NONE | 否 | 是 | 否 | 否 |

| 字段 | 含义 | 读者 |
| --- | --- | --- |
| `reply_top_level` | 回复主人消息放顶层还是进串（None 按 `Traits.top_level`） | `MainThread.configure` |
| `answer` | 已授权回复单独发到哪 | `TurnDelivery.send`、`app/redelivery.py` |
| `answer_on_card` | 写在任务卡上：`ROUTES` 开了或转了话题时 | `answers_in_place` |
| `conclusion`、`stop` | 做完、停止或失败的回带 | `main_relay.after_turn` |
| `question_copy` | 问题卡的顶层副本 | `QuestionDisplay.mirror` |
| `ask_block` | 顶层新消息带“你问”块 | `app/answer_delivery.py` |
| `status_card` | 看板 Pin 状态卡，否则 Pin 这一轮自己的卡 | `app/pins.py` 的 `status_pinned`、`aimed` |
| `answer_head` | 话题串里的答案卡顶上带状态行，任务卡不写“结论”；只在主人私聊 | `AnswerParts.answer_head`、`headed` |
| `quiet` | 这一轮不开串 | `placement.quiet`、`marked` |

### 消息落在哪

- **主线程的卡**在主人那句话的话题串里，跑过 90 秒才发。
- **答案、结论、问题卡副本**都是聊天区新消息，不引用任何卡：主人那句话下面已有话题串，回复它会掉进串里。
- **“你问”块**（`cards/answer_card.py` 的 `ask_block`）：灰底两行“你问 · 时间 ›”和原话，整块可点（`app/answer_cards.py` 的 `ask_url`）。按这一轮属于谁定跳到哪：话题的轮次（结论回带、问题卡副本，不论是开话题区、转交、补充还是主人在话题里接着说的，`relay_targets.ask_for` 传话题的 topic_id）跳到那个话题根消息的话题串，定位到这一轮的卡（`topic_links.topic_thread_link`，回带传 `turn_card`，问题卡副本传问题卡）；主线程自己做的轮次跳到主人那句话在聊天区的位置。话题根读不到话题串时退到根在聊天区的位置，什么都读不到时不给链接（不画 ›），两种都在日志记一行 `ask link fell back to the chat` / `ask link missing`。文字仍是主人原话：转交的轮次用主线程接这件事的那句话。
- **“你答”块**：表单回答开的一轮写“你答 · 时间 ›”和“问题 → 回答”，链接规则和“你问”一样（主线程自己的回答跳到提问那一轮的主人原话）。只有哪里都跳不了时才用默认收起的折叠（`answer_block`），展开是每道题和完整回答（`app/answers.py` 的 `answer_parts`）。

## 工具通道

```
主线程会话 ── MCP（stdio）── bin/covey_main_mcp.py
                                │ 写 <state>/main-thread/requests/<id>.json，等 <id>.answer.json
Bridge 进程 ── app/main_tools.py：pending → claim → 核对 job_key → 处理函数 → answer
```

- **请求** `{"id", "tool", "args", "job_key", "created_at"}`，**回答** `{"ok", "text", "data"}`。`job_key` 来自 `COVEY_MAIN_JOB`，状态目录来自 `COVEY_MAIN_STATE`，都由 `mcp_config` 写进配置。
- **MCP 服务**（`core/main_mcp.py`，只用标准库）：每行一条 JSON-RPC 2.0；`tools/call` 每次一个线程。等回答最多 120 秒（`main_requests.submit`）。
- **执行**（`MainTools`，线程 `main-tools`）：启动时把认领了没回答的（`orphans()`）回 `UNCERTAIN`，不重跑（规则 1、6）。之后每 0.3 秒取 `pending()`，`claim()` 成功才执行，一次一个。只接受任务记录存在、`topic_id` 是主线程、状态在 `RUNNING` 里的调用，否则回 `REFUSED`。循环不持锁，飞书调用都在锁外（规则 1、2）。
- **处理函数**：`handler(env, request) -> dict`，`env` 是 `app/main_env.py` 的 `MainEnv`；各模块用 `TOOLS` 字典登记，`app/unified.py` 合起来交给 `MainTools`。
- 话题写 T 号或 topic_id，只认主人私聊的；标题只来自 `core/topics.py` 的 `topic_title`。

### 工具清单

照 `core/main_tool_specs.py`（* 必填）：

| 工具 | 参数 | 做什么 |
| --- | --- | --- |
| `topics_list` | group、query、limit | 按等你回答 → 有结果未看 → 进行中 → 最近列话题 |
| `topics_search` | clues*、limit | 按线索找是哪个话题哪一轮做的 |
| `topic_read` | topic*、job、turns | 读话题最近几轮：问题、结论、检查点、没答的问题 |
| `workspaces_list` | 无 | 列工作区：名字、路径、用途、最近的话题 |
| `topic_start` | title*、prompt*、workspace*、readonly*、user_requested*、why*、attachments、harness、model、effort | 为新的事开话题区 |
| `topic_send` | topic*、text*、note、title、attachments | 把主人的话追加给已有话题区 |
| `topic_fork` | topic*、title*、prompt*、attachments | 从已有话题分叉 |
| `question_answer` | topic*、answers* | 替主人答选择题 |
| `topic_priority` | topic*、remove | 看板本组内提前，或撤下 |
| `principle_save` | scope*、workspace、text* | 记下主人的原则 |
| `principle_delete` | id* | 删一条原则 |
| `principles_list` | 无 | 列出原则 |
| `settings_panel` | 无 | 在私聊发设置卡（`app/main_thread_panel.py`） |
| `knowledge_save` | scope*、kind*、text*、workspace、why、source_topic | 记一条知识条目（只有 `COVEY_KNOWLEDGE=1` 才列） |
| `knowledge_update` | id*、text*、reason*、kind、why | 改写：新 id 取代旧 id（同上） |
| `knowledge_drop` | id*、reason* | 作废一条（同上） |
| `knowledge_search` | query*、workspace、history | 搜当前条目；`history` 时也搜已取代、已作废的（同上） |
| `history_search` | query*、workspace、since | 搜 Claude、Codex、Bridge 的历史轮次（只有 `COVEY_HISTORY_CMD` 是绝对路径才列） |

- 查询类工具（`app/main_tools.py`）只读记录，不持记录锁。`topics_list` 的“有结果未看”（`group: result`）是看板的口径（`board_results`），“最近”是 `TOOLS_AGE`（3 天）。`topic_read` 默认读 2 轮，检查点列阶段、已完成、产物、下一步、在问，以及 `steps` 和 `delivery`。
- `question_answer` 只答最新一组 pending 的问题：每题都要答，答案是选项原文，有自由文字题就整组拒绝。经 `app/answers.py` 的 `answer_fields` 转成表单字段，补充说明写 `PROXY_NOTE`，再经 `app/questions.py` 的 `submit_answer` 提交，和卡上点“提交回答”同一条路。
- `topic_priority` 写 `board-priority.json`；`remove` 经 `app/pins.py` 的 `dismiss` 写 `dismissed`，看板没开时回 false。

### 按线索找话题

`topics_search`（`app/main_search.py`）一次 1 到 `MAX_CLUES`（5）条线索，搜主人私聊每个话题的每一轮。

- **字段**：主人的话、结果（`final_text`）、状态行、检查点的全部字段（除 `SKIPPED`）。不读会话原文和 `traces/`，不搜群聊。
- **线索**：飞书链接取路径里的 token；16 位以上字母数字混合的串当 token；带 `/` 无空白的当路径；其余是关键词。token 写短（至少 `PREFIX` 位加省略号）、路径只对上最后两段，算部分匹配。
- **排序**：类型分（整段 3、部分 2、关键词 1）乘出现方式分（`CLASS_WEIGHT`：第一次出现、结果里也有、只是提到），“之前”按任务编号跨话题算。默认列 10 轮，按话题分组，带片段。
- **开销**：结束任务的检查点读一次后留在内存，线程 `main-search` 启动后预读。不加锁、不调飞书、不写文件。

## 开话题、转交、分叉

`app/main_topics.py`，只在主人私聊里动作，飞书调用都在锁外（规则 1、2）。

- **`topic_start`**：
  - 工作区只认已登记的名字（`core/workspaces.py`），设置了 `topic_defaults.workspace` 就总用它。
  - Harness、模型、预算逐项取值（`resolve`）：调用给的 > `topic_defaults` > 这个私聊上次开始任务的配置（`app/launch.py` 的 `launch_defaults`）> Bridge 默认。上次的值已不可用时往下找；调用给的不可用时回错误并列出可选。
  - topic.json 写 `origin`（`kind: "main"`、`request`、`job_key`、`why`、`readonly`、`user_requested`、`title`）。
  - `starts_at_once` 为真时照分叉绑定配置（`workspace_locked`、`harness_locked`），第一轮直接提交；否则（或运行方式是独立 worktree）话题里出启动卡，预选在 `origin.launch`。
- **`topic_send`**：主线程自己和代答话题不接。`text` 是任务说明，`note` 一句背景，都只给模型。
  - 有没结束的任务：作为补充交给最新那个（`deliver_steer`），话题串里出“主线程转来你 HH:MM 的补充：”加主人原话。被拒（`SteerRefused`）、不确定（`SteerUncertain`）都照实回答，不重发（规则 6）。
  - 已经停了：串里出“主线程转来你 HH:MM 的消息：”，再照话题里的消息处理（有没答的问题就作为回答，否则开下一轮）。还在等启动卡的话题不转。
  - 串里这条转入消息（`hand_line`）是一张灰底卡，整块可点，跳回主人那句话在聊天区的位置（`cards/answer_card.py` 的 `handoff_card`，链接同主线程轮次的“你问”）；读不到那句话的位置时照旧发文字。卡的消息 ID 记在转交记录的 `thread_message_id`。
- **`topic_fork`**：从源话题最近一次结束的任务分叉（`app/forks.py` 的 `create_fork`），绑定源话题的配置和脱敏，复制原生会话。源话题在跑或排队就回错误。
- **话题卡**（`cards/topic_card.py`）：“T412 · 标题”加一行灰字配置，没有按钮，拿到 T 号后整卡更新一次。
- **幂等**：每次调用在 `<state>/main-thread/opened/<请求>.json` 记做到哪一步。发送键和第一轮的消息 id（`main-<请求>`）从请求 id 派生；同一个请求再来返回存下的回答。别处经 `core/main_opened.py` 读。

### 交给话题的消息

`app/main_handoff.py`。交给话题的每一轮（开、下一轮、补充、分叉），模型收到 `handoff_prompt`，卡、历史和串里显示主人原话（`shown`、`thread_line`）。

- **Bridge 的说明**（`note`，开头 `[Bridge 说明]`）：你在哪个话题区；这件事是主线程按主人哪条消息交来的；路由已经办完，直接做事；你的回复就是结论。
- **任务说明**：主线程写的 `prompt`/`text`，后跟“主线程补充：note”。
- **主人原话**（`asked`）：主线程这一轮记录的 `user_text` 加运行中收到的补充，按时间引用。
- **附件**（`carry`）：只认主线程话题 `incoming_media/` 或 `<state>/incoming_media/` 下的文件（`core/attachments.py` 的 `parse_attachment_block`），按参数 `attachments`（`all`、`none`、序号）挑出，硬链接或复制到目标话题的 `incoming_media/`。补充只送文字。

## 话题区

主线程派出去的每件事是一个话题区：话题串挂在提出它的那条提问下面；同一件事用 `topic_send` 追加。

- **开**：主线程开着、查得到提问、提问的别名还指向主线程时（`main_topics.place`，`core/topics.py` 的 `claim_area_root`），以这条提问为根：先把别名改给新话题（`move_topic_message`），再建上下文，不发话题卡。`opened/` 记 `area_root`、`area_taken`。
- **一条提问只挂一个串**：同一条提问的第二个话题、查不到提问时，根是顶层新发的话题卡；分叉挂不上时把源话题合并转发到私聊当根。第二个话题的“你问”仍写那条提问，链接打开它自己的串（`ask_for` 读 `area_taken`）。
- **引用直达**：主线程这一轮的卡只交给了一个话题时，卡的别名记上 `routes_to`（`main_relay.after_runner` 调 `route_message`）。回带的结论、问题卡副本也记成话题的消息并带 `routes_to`。引用它们就是对那个话题说话（`context_for_topic` 先看 `routes_to`），不经主线程。纯话题模式下串里不看 `routes_to`。

## 结论回到顶层

主线程开的或转过去的那一轮结束时，Bridge 在聊天区发一条新消息，顶上是“你问”块。只在主线程开着时；不调模型。

- **哪些轮**（`relay_targets.relay_target`）：消息 id 是 `main-<请求>` 的任务、`topic_send` 作为补充送进的任务、它们的自动重试（`retry_of`）。话题区里回答问题后接着跑的、主人在串里开的一轮也回带（`area_target`）。主线程没碰过的话题不回带。
- **什么时候**：任务进程投递完、任务卡写完之后（`TurnDelivery.deliver` → `main_relay.after_turn`），出错只记日志。

| 结果 | 回带 |
| --- | --- |
| 完成，回答已送达 | 结论卡（`cards/relay_card.py` 的 `relay_card`）：回答全文、产物、输入/输出灰框，末尾 T 号链接（`relay_send.linked`）。只用 `turn.verified_delivery`（规则 5）。超过 `CARD_TEXT_BYTES` 分几条 Markdown 发，再补一张小卡 |
| 等你回答 | 问题卡顶层副本；放不了时发“**T410 需要你回答**”（`relay_question`） |
| 失败或停止，没排上自动重试 | “**T410 停了**：状态行”；主人自己点的停止不发，记 `skipped`、`why: owner_stop` |
| 没送达、排上了自动重试 | 不回带 |

- **只发一次**（`app/relay_send.py` 的 `send_once`）：记录 `<state>/main-thread/relays/<任务键>.json`。先画好卡，文件锁下写 `submitting`（不用全局 `LOCK`，规则 1、2），锁外发，发送键从任务键派生；发完写 `sent`，没确认写 `uncertain`，都不重发。写 `submitting` 前出错的记 `unsent`（`record_unsent`），不补发。日志 `main relay state=…`。可选部分（产物、输入/输出）出错时去掉那一项照发（`_part`）。
- **产物**：答案卡、回带卡记在 `<state>/answer-cards/<任务键>.json`，上传结束或“重新上传”后整卡改写（`app/answer_cards.py`，`Messenger.patch_card`）。
- 回带消息是话题的消息，看过它就算看过话题。
- `main-thread/opened/`、`relays/`、`answer-cards/` 7 天没动的由维护删（`core/maintenance.py`）。旧 release 的任务进程不回带。

### 问题卡在顶层也放一份

话题区的问题在顶层也放一份同样的表单（`app/question_display.py` 的 `mirror`，卡 `cards/question_cards.py` 的 `mirror_card`、`top_level_card`），聊天区新消息、带“你问”。在哪张上答都一样，答了两张一起收起。

- **表单上面是这一轮的输出**：原生问题（`NativeQuestions.mirror`）是到这时的进展说明和回答；检查点问题（`after_turn`）是任务卡正文。超过 `MIRROR_OUTPUT_BYTES` 的先单独发一条。
- 只有一道有选项的单选题时，每个选项一个按钮，点了就提交（`picks`、`app/answers.py` 的 `picked_fields`）。
- **记录**：问题记录多 `mirror_state`（`creating` → `sent`/`uncertain`，锁内写、锁外发，规则 1）、`mirror_message_id`、`mirror_card_id`；别名记成话题的并带 `routes_to`。
- **一起收起**：`QuestionDisplay.paint` 也重画副本（`paint_mirror`，sequence 从映射 `reserve`，规则 3）。
- 副本没能发出时，原生问题回带一行（`relay_question`，记在 `main-thread/relays/questions/`）。

## 看板与 Pin

要 `COVEY_PINNED_TOPICS=1` 和 `COVEY_PINNED_OVERVIEW=1` 都开，由手上的事（`app/overview.py`）的循环带着跑（`app/pins.py`）。主线程自己不进看板，它开的话题照常进。

- **读数**：`core/board_read.py` 的 `read` 一次读出私聊话题的问题、任务、已读和优先级，不持锁、不调飞书。看板、手上的事、早晚汇总、快照、`topics_list` 都从这里读。
- **有结果未看**（2026-11-10 起五处一个口径，以看板为准）：`core/board_read.py` 的 `groups` 按看板分组，`unseen` 取出没看过的结果，五处都经 `app/pins.py` 的 `board_results`（和看板的 `look` 同一份记录、同一套规则），同一时刻同一份数据得到同一组话题。
  - 哪些算结果：话题最新一个跑过的任务（排队时就取消的不算，`unrun`）完成、失败、中断或停止，并且有任务卡；话题正在等回答（一天内的问题）或在做时不算。
  - 怎样算看过：话题在结束之后读过（`seen_at`）；答案写在顶层卡上的，顶层在答案出现之后读过也算（`top_read`）。第一次看过的时间记在看板记录的 `results` 里，看过就一直算看过。
  - 没看过的不论多久都算，看板记录的 `results` 让 `read` 继续读到这个话题的旧任务。看板（两个开关都开，`laid`）开着时看板记录里的 `since`、`keep_since`、`stopped_since` 照旧生效；没开看板时没有记录，`read` 读到的（`RESULT_AGE`，3 天）结果都算没看过；以前开过看板留下的记录里撤下的话题（`dismissed`）也不算，什么都不过滤。
  - 组键都叫 `result`：快照和 `topics_list` 只列没看过的（文字“有结果未看”；`topics_list` 还认旧名 `unread`）；看板和手上的事的“有结果”另外留着看过不到 12 小时的，手上的事标“已看”并写“（未看 N）”；早晚汇总的“做完”在上次汇总之后结束的之外，还列出所有没看过的，其余标“已看”。各处的“最近 3 天”只用于 `topics_list` 的 recent 这类“最近动过”的列表，不再用来筛未看。
- **分组**（`GROUPS`）：等你回答 → 进行中 → 有结果；停止的也在“有结果”。
- **分组**（`GROUPS`）：等你回答 → 进行中 → 有结果；停止的也在“有结果”。
- **留多久**：没看过的一直留；看过的从第一次看过起留 `KEEP_SEEN`（12 小时）。“看过”看话题的已读时间（`seen_at`；已读事件 `app/topic_seen.py`、`core/topic_seen.py`）。
- **钉哪张卡**：话题区布局 Pin 这一轮在串里的卡：等回答是问题卡，在做是任务卡，做完是答案卡（`answer_message_id`，`aimed`）。纯话题布局 Pin 状态卡（`status_pinned`，`app/status_cards.py`）。
- **优先和撤下**：见 `topic_priority`；撤下的话题有新问题或新结果时自己回来。状态词都来自 `core/turn_words.py`。

### 个人空间的主线程与看板

`spaces.personal_groups` 里的群（和 `owner_accounts` 的私聊）各有自己的主线程：话题 id 按 chat 算（`main_topic_id(chat)`），记录不混；`Access.agent_chat` 判断哪个 chat 有主线程，`MainEnv.chat()` 在工具调用里取这个主线程的 chat。个人群没有已读回执，主人在群里发的消息算读过它的看板（`app/group_boards.py` 的 `owner_active`）。个人群各有一份手上的事、看板和早晚汇总，记录名带 chat_id：`pinned-topics.<chat_id>.json`、`pinned-overview.<chat_id>.json`、`digest.<chat_id>.json`（`app/pins.py` 的 `record_path`）；`notify_space` 那个 chat 用默认那一份。设置见 [配置的空间一节](configuration.md#空间spaces)。

## 主人的原则

Bridge 记一份主人的长期原则，所有 Harness 的话题和主线程都带上。

- **存储**（`core/principles.py`，只用标准库）：`<state>/principles.json`，`{"version": 1, "owner": [...], "workspaces": {"<工作区>": [...]}}`；每条 `{"id": "p" + 6 位十六进制, "text", "at", "source"}`。一条最多 `TEXT_LIMIT`（200）字，每组最多 `GROUP_LIMIT`（30）条，满了拒绝。写入在 `shared_file_lock` 下整体替换，读不加锁；文件坏了当空的读，下次写前改名 `principles.json.bad-<epoch>`。
- **工具**：`principle_save` 的 `scope` 是 `owner` 或 `workspace`（要已登记的工作区）；正文相同的返回已有 id。`principle_delete`、`principles_list` 用 `delete`、`entries`。
- **注入**（`app/runs.py` 的 `Runs.principles`，块由 `app/prompts.py` 的 `principles_block` 写）：工作区那组总带；通用那组只在主人自己的一轮带（`Runs.owner_turn`：主线程总是，其余经 `Access.owner_turn`，规则 11）。新会话在 `bridge_rules` 里，续接的在 `_with_rules` 补的提醒里，所以新原则下一轮就生效。总长约 `PRINCIPLES_CHARS`（2000）字，超了从旧的截。代答话题不带。

## 知识库

主人说“记一下”的东西集中存放，所有 Harness 的新会话都带上，主线程能搜索和修改。`COVEY_KNOWLEDGE=1` 才开（[配置](configuration.md#知识条目与历史搜索)）；关着时不建目录、规则和提示词一字不变、工具清单里没有这些工具、汇总里没有这一节。

- **存储**（`core/knowledge.py`，只用标准库）：`<state>/knowledge/owner.jsonl`（通用）和 `workspaces/<工作区>.jsonl`，只追加的事件，一行一个：`add`；`update`（新 id，带 `replaces`，旧的变已取代）；`drop`（必须有 `reason`）。读的时候从头重放，得到每条的状态（当前、已取代、已作废）。同目录有生成的 `.md`（每组当前条目）和 `owner-brief.md`（和注入同样 `KNOWLEDGE_CHARS` = 1500 字的摘要）。写在文件的跨进程锁里追加一行，锁里不调飞书、不起子进程；生成的 `.md` 写不出不影响事件。读不了的文件当空，下一次写前改名 `*.bad-<epoch>`；不是事件的行跳过。
- **条目**：`kind` 是 decision、practice、pitfall、fact、question；正文最多 200 字，`why` 可空；`by` 是 main（主线程的工具）、topic（话题里收集的）、import。同一组里正文去掉空白、大小写和句末标点后相同的不再新增，返回已有的 id。
- **主线程**：见上面的工具清单（`app/main_knowledge.py`）。规则里写：“记一下”调 `knowledge_save`（具体的事实、决定、坑；以后每次都要遵守的做法仍用 `principle_save`）；“把 T483 的结论记一下”先 `topic_read` 再起草；记完列出 id 和正文；“改成”“删了”用 `knowledge_update`、`knowledge_drop`；查以前的事按 `knowledge_search` → `topics_search` → `history_search`（`app/prompts.py` 的 `knowledge_rules`）。工作区名要已登记，不认识时返回可选的名字。
- **话题里说“记一下”**：规则里多一句（`REMEMBER_RULE`，只给主人自己的一轮、不是主线程、不是代答、没开脱敏的话题）：只有主人这一轮明确说了才把要记的写进检查点的 `remember: [{kind, text, why, scope}]`，最多 5 条，并在回复里列出。收集在 Bridge 进程里（规则 8），`app/detached.py` 的 `finish_detached` 在任务进程退出之后（在进程内跑的任务在 `app/job_dispatch.py`）调 `app/knowledge_collect.py` 的 `collect`，同时满足才写：这一轮正常结束（`done` 或 `waiting_user`）、`owner_turn`、原话里有“记一下 / 记住 / 记下来”、不是主线程转交的（`prompt_text` 非空）、不是代答也没开脱敏（配置读不出按开了处理）。不满足记 `knowledge remember ignored reason=…`；同一个任务键只写一次；整个收集包在 try 里，失败只记日志。
- **注入**（`app/knowledge_turn.py`，块由 `app/prompts.py` 的 `knowledge_block` 写，放在原则后面、检查点约定前面）：工作区那组谁的话题都带，通用那组只在 `owner_turn` 时带；从新到旧排，总长 1500 字，超出的截掉最旧的，末行写另有几条和 `.md` 在哪；新会话全量带，续接的会话只补话题上一轮结束以后新增或改动的（上一轮的结束时间从 `jobs/` 取，那一轮自己收集的不再补）；代答话题不带；没有条目整块不出现。
- **查历史**：设了 `COVEY_HISTORY_CMD`（绝对路径）时，主人自己的话题多一行“用 `<命令> search <一句话>`，不要自己 rg 状态目录或会话目录”（有没有条目都有，新会话和续接的每一轮都带），主线程多 `history_search`。命令本身（轮次索引、混合检索）不在本仓库；`history_search` 在 `app/main_knowledge.py`，自己的进程组里跑，70 秒超时整组杀，输出最多 20 KB，命令不存在、不能执行、超时、非零退出都回一句中文。主线程的 MCP 服务靠 `core/main_thread.py` 的 `mcp_config` 交给它的这两个变量决定列不列这些工具（`core/main_tool_specs.py` 的 `listed`）。
- **汇总**：`app/digest.py` 的 `knowledge_view` 在汇总卡和菜单「看板」多一节“新记的知识”：上次汇总以来新增、改写、作废的条目，最多 10 行（操作 · id · 组 · 正文前 40 字），多的写“另有 N 条”；没有变化没有这一节，它自己不会让汇总卡发出来。
- **回退**：去掉两个变量重新 `launch`；已经写下的 `knowledge/` 留着。

## 设置

`<state>/main-thread.json`，字段见 `Settings`：`mode`/`enabled`；主线程的 `model`、`effort`、`redaction`、`workspace`、`compact_tokens`；新话题的 `topic_defaults`（`harness`、`model`、`effort`、`run_mode`、`redaction`、`workspace`）；子 Agent 用什么模型 `subagents`；直接开工 `start`、记住的 `trusted`、工作区用途 `workspace_notes`。缺的或坏的字段按默认值读。只经 `update` 写，只有设置卡能写。

- **主线程的脱敏**：从下一轮起走敏感词中继；Bridge 每次交消息前写它的 `topic.json`（`keep_topic`），中继每轮读（`backends/claude/redaction.py`）。

### 设置卡

主线程的设置是设置卡（`app/settings_hub.py`，菜单键 `bridge_settings`）的前两个标签，标签行见 `cards/settings_hub.py` 的 `LABELS`。画卡和点击在 `app/main_thread_panel.py`、`cards/main_thread_panel.py`。

| 页 | 内容 |
| --- | --- |
| 私聊 | 私聊模式；主线程的供应商、模型、思考预算、脱敏、压缩阈值（Harness 固定 Claude Code） |
| 新话题 | Harness、供应商、模型、思考预算、运行方式、脱敏、工作区；子 Agent：轻量（Sonnet，搜索用 Haiku）/ 跟主模型；直接开工三档 |
| 工作区 | 工作区列表和每行的用途；记住的工作区，每项可删 |

- 不用下拉框：每行点开是选项，点一个就记下并整卡重画。可选项和启动卡同源（`cards/launch_config.py` 的 `tiers`、`budget_options`）。留空的项写实际会用什么（同 `resolve`）。
- **子 Agent** 在新话题页，但对整个机器人生效：Bridge 起的每一轮 Claude Code（话题和主线程，连同轮前压缩）都照它。`subagents` 是 `light`（默认，没有这一项或值不认识也算）时，进程带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，再用 `--agents` 换掉内置的 Explore：只读（Read、Grep、Glob、Bash），跑在 Haiku 上，只找文件、搜代码、看日志，报路径、行号和摘录，不改东西、不下判断；改文件和拿主意还是主模型。这一轮的模型是 Haiku 时不设 `CLAUDE_CODE_SUBAGENT_MODEL`，子 Agent 不比主模型大。`same` 什么都不加，每个子 Agent 都跑主模型。`backends/claude/subagents.py` 读这个设置；`ctx.options` 的 `subagent_model` 有值时压过设置（空字符串是不设），也不加 Haiku 的 Explore，内置的 Explore 跟着它（安全拒绝后的重试）。旧版本的任务进程不受影响。部署不允许主线程时没有这一页，按默认 `light`；要跟主模型，手动在 `main-thread.json` 里写 `"subagents": "same"`。
- 只有主人能改。旧卡的按钮照写：旧行按 `PAGE_OF` 落到对应页，下线的标签按 `RETIRED` 落到工作区页，更早的面板卡由 `SettingsHub.takeover` 接管。
- 旧菜单键 `main_thread_settings` 还认，发的也是设置卡；新卡发出后撤回上一张（`app/bot_menu.py` 的 `Panels`）。

## 话题链接

主线程卡、回带卡上的 T 号是链接（`app/topic_links.py` 的 `linkify`），打开话题并停在一张卡上：回带卡上它自己的话题停在回带的那一轮的卡，别的停在最新的任务卡。

- 有话题串的用 applink `client/thread/open?…&thread_position=…`，读不到位置时只打开话题串。
- 位置由 `feishu/message_read.py` 的 `fetch_message_detail` 读一次，记在 `<state>/topic-links.json`，每个话题留 `KEPT`（8）条。
- 读不到保持纯文字；读消息在所有锁外（规则 1、2）；只改显示的文字，保存的回复（规则 5）不变。

## 代码位置

| 模块 | 职责 |
| --- | --- |
| `core/main_thread.py` | 开关、`main_topic_id`、`Traits`、`worded`、`mcp_config`；只用标准库 |
| `core/main_thread_settings.py` | `Settings`、`update`、`settle_mode`、`starts_at_once` |
| `core/placement.py` | 放置表：`layout`、`plan`、`TABLE`、`quiet`、`marked` |
| `core/main_tool_specs.py` | 工具清单 |
| `core/main_requests.py`、`core/main_mcp.py` | 请求通道、会话里的 MCP 服务 |
| `core/main_opened.py` | 读 `main-thread/opened/`：`kind_of`、`answered` |
| `core/board_read.py` | 私聊话题的一次读和三种“未看”规则 |
| `core/turn_words.py` | 状态词、请求行 |
| `core/principles.py` | 主人的原则 |
| `core/knowledge.py` | 知识条目：存储、重放、搜索、生成的 `.md` |
| `app/knowledge_turn.py`、`app/knowledge_collect.py` | 一轮读哪些条目、话题里“记一下”的收集 |
| `app/main_knowledge.py` | `knowledge_*` 和 `history_search` 工具 |
| `core/topics.py` | 别名与路由：`context_for_topic`、`route_message`、`move_topic_message`、`claim_area_root`、`topic_title` |
| `app/main_thread.py` | `MainThread`：路由、回复位置、等主人的话、`main_turn`、快照、压缩请求 |
| `app/idle_compaction.py` | 两轮之间的空闲压缩 |
| `app/main_tools.py`、`app/main_env.py` | `MainTools` 循环、`MainEnv`，查询、代答、原则、看板的工具 |
| `app/main_search.py` | `topics_search`、预读线程 |
| `app/main_topics.py`、`cards/topic_card.py` | 开话题、转交、分叉，话题区的根，话题卡 |
| `app/main_handoff.py` | 交给话题的那一轮 |
| `app/main_reply.py` | 主线程一轮在卡上的结束 |
| `app/main_reactions.py` | 主人消息上的表情 |
| `app/quiet_delivery.py` | 没发卡的一轮的短消息 |
| `app/answer_delivery.py` | 单独发的答案卡和“你问”块 |
| `app/main_relay.py` | 回带入口：`after_turn`、`after_runner`、`question_asked` |
| `app/relay_targets.py` | 回带到哪：`relay_target`、`area_target`、`ask_for` |
| `app/relay_send.py`、`cards/relay_card.py` | 只发一次（`send_once`）、回带卡 |
| `app/question_display.py` | 问题卡和顶层副本 |
| `app/answer_cards.py`、`cards/answer_card.py` | 答案卡的产物、“你问”块 |
| `app/pins.py`、`app/overview.py`、`app/status_cards.py` | 看板、手上的事、状态卡 |
| `app/topic_seen.py`、`core/topic_seen.py` | 已读事件和记录 |
| `app/topic_links.py` | T 号链接 |
| `app/main_thread_panel.py`、`cards/main_thread_panel.py` | 设置卡的主线程页 |

## 前台话题（已下线，冻结）

`main-thread.json` 的 `desk_topics`（11-07）曾让主线程开“前台话题”：有 T 号，没有话题卡和话题串。现在：

- `desk_topics` 读到 `true` 也按 `false`，启动时记一行 warning（`DESK_TOPICS_RETIRED`），保存设置时原值照留；设置卡上没有这一项。
- 已开的（登记在 `<state>/main-thread/desk/`）由 `core/migrate.py` 的 `desk-retire` 冻结：topic.json 加 `retired: "desk"`，原字段都留着（`core/topics.py` 的 `retired_topic`）。
- 冻结后不再进它：引用它旧卡的顶层消息交给主线程（`MainThread.app_for`），回复链的根是它的消息时也在主线程里作为一条新的顶层消息处理（`MainThread.handle_event`），主线程不可用时按普通消息路由、只回一句；旧卡上任何按钮、被路由到它的消息只回一句 `RETIRED_TEXT`（`Topics.refuse_retired` → `retired_action`）；主线程工具按 T 号找到它时说明已下线（`find_topic`、`question_answer`）。
- 旧记录里挂起的交接和回带读到也不用；`inline` 状态照旧算回带过（`main_relay.relayed`）。

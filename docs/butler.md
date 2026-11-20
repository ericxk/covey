# 管家

管家是私聊里一个常驻的 Claude Code 会话（设置卡的私聊模式选“管家”时）。主人在私聊**顶层**发的消息都交给它：能直接答的自己答，要干活的开话题或转给已有话题，主人说“T12 选 A”时替主人答选择题。用户视角见[使用指南 · 管家](usage.md#管家)，部署开关见[配置](configuration.md)。

管家交给负责人跟进的长期的事见[专项](projects.md)，网页上的工作台见[网页工作台](web.md)。

## 组成

- **私聊模式开关**：`core/butler.py` 的 `enabled`（见「私聊模式」），设置在 `core/butler_settings.py`。
- **管家会话**：`app/butler.py` 把顶层消息交给管家话题，每轮带规则、快照和 MCP 配置（见「会话」）。
- **MCP 工具**：会话里的 stdio MCP 服务 `core/butler_mcp.py`（入口 `bin/covey_butler_mcp.py`），清单 `core/butler_tool_specs.py`。
- **请求通道**：`core/butler_requests.py`，一次调用一个请求文件，由 Bridge 进程里的 `app/butler_tools.py` 执行（见「工具通道」）。

各模块的职责见 [AGENTS.md 的代码地图](../AGENTS.md#代码地图)和[架构 · 代码位置](architecture.md#代码位置)。

## 私聊模式

`core/butler.py` 的 `enabled` 是唯一的判断，各处都问它。两层：

1. **部署允许**（`available`）：`COVEY_BUTLER=0` 时不允许：管家的后台线程不起，菜单键不认，设置卡没有「管家」页，「新话题」页没有直接开工。其他值都允许。
2. **主人选的模式**（`agent_mode`）：`main-thread.json` 的 `mode`，`agent` 是管家，`topics` 是纯话题。主人在设置卡「管家」页“更多”里的“私聊顶层的消息”一行切换（选“每条直接开一个话题”要再点“确定改”），保存即生效；Bridge 和任务进程每次 stat 一下，变了才重读。保存时同时写 `enabled` 给旧版本。

**默认值只写一次**：Bridge 启动、处理事件之前（`Butler.settle` → `settle_mode`），文件里没有 `mode` 就写：状态目录里没有任务也没有话题时写管家；否则 `COVEY_BUTLER=1` 且设置没关的写管家，其余写纯话题。之后只看 `mode`。

**纯话题**下和没有管家时一样：顶层每条消息开一个话题；引用管家以前的卡当作新消息开话题（`core/topics.py` 的 `context_for_topic`），引用“交给一个话题”的管家卡照旧进那个话题；正在跑的管家那一轮跑完，按 `BUTLER_OFF` 收尾；会话和记录留着；管家开过的话题不再回带、不再放顶层问题卡副本。群聊两种模式都没有管家。

管家只用 Claude Code（`app/butler.py` 的 `HARNESS`）。设置卡选的是管家、但 Claude Code 没启用时，私聊消息照旧开话题，Bridge 启动日志写一行 `main thread is on but its Harness claude is not enabled`。

## 和话题的不同

不同都在 `core/butler.py` 的 `Traits` 表里。调用处只查表，不比较 topic id，也不比较 `Traits.name`（`tests/test_butler_contract.py` 的 `NoNameComparisonTest` 检查）。

- **判断**：`is_butler_topic(topic_id, chat_id)`，和开关无关。
- **查表**：`traits(topic_id, chat_id)`；任务、记录、topic.json 用 `traits_of`；只有上下文时用 `core/topics.py` 的 `topic_traits`。
- **行**：`TOPIC`、`BUTLER`（开关开着）、`BUTLER_OFF`（开关已关：结构上的不同照旧，会话上的取消）。

| 字段 | `BUTLER` | 读者 |
| --- | --- | --- |
| `switch_config`、`rewind` | 否：没有换配置、回退 | `app/switch_handoff.py` |
| `card_target` | 否：卡片点击不能指向它 | `Topics.target` |
| `numbered` | 否：不占 T 号 | `topic_number` |
| `top_level` | 是：引用回复每条消息 | `Butler.configure` |
| `listed` | 否：不进看板和手上的事 | `core/board_read.py` |
| `own_turn` | 是：每轮带规则、快照、工具，按阈值压缩 | `butler_turn` |
| `answers_in_place` | 是：回答可写在任务卡上 | `app/butler_reply.py` |
| `lists_supplements` | 是：补充没有回执卡，停止或被拒时卡上列出 | `app/butler_reply.py`、`app/steering.py` |
| `hold_seconds` | 30：没文字的消息等主人的话 | `Butler.hold_for` |
| `slim_card` | 是：分档任务卡 | `build_stream_card` |
| `wording` | 卡上“本话题”换成管家的说法 | `worded` |
| `relays`、`has_thread` | 否（话题是）：回带到顶层、有话题串 | `app/relay_targets.py`、`app/pins.py` |
| `linkify_body` | 是：正文里的 T 号做成链接 | `app/topic_links.py` |

`BUTLER_OFF` 的会话字段取话题的值。

## 一轮怎么走

### 路由

`app/card_routing.py` 在启用的扩展的 `handle_event` 之后调 `Butler.handle_event`，主人身份来自 `Runtime.access`（规则 11）。消息的 `root_id`、`thread_id`、`parent_id` 按别名查话题：查到别的话题交给它；查到管家留在管家；都没填是管家的新一轮；填了但谁也不认识就照旧开话题。

管家话题的 `topic.json` 带 `main_thread: true`（字段名沿用旧名，不改），没有根消息和 `topic_number`。每条消息都同步设置：模型和预算下一轮生效；换工作区要等没有任务在跑，并开新会话。

- **补充**：有任务没结束时，新消息用 `deliver_steer` 并进最新一轮，不发回执卡（开了[同时做几件事](#同时做几件事开关默认关)时，并进哪件在做的事由分流定）。可能已送达的不重发（规则 6），在主人消息下说一句；卡上已列出的不再各说（`TurnDelivery.listed`）。
- **收尾时发来的消息**：确定这一轮没读到的补充（held 后被拒，或已发出但 Claude 到结束都没报告读取），自动排成管家的新一轮，和主人刚发一条新消息一样：同一个 message_id 作为这一轮的请求，放置、表情和回答都落在它上面，不说 `SUPPLEMENT_LOST`。不确定的、主人停止的一轮照旧说一句；排队出错的不再排，说一句 `SUPPLEMENT_UNCERTAIN`。做法（`requeue`、`requeue_auto`、任务进程退出时和 Bridge 重启后的补排）见[架构 · 补充投递](architecture.md#补充投递)。
- **等主人的话**：转发框里的留言会在转发后一两秒单独发来。没有任务时，没有自己文字的消息（`bare_message`：卡片、合并转发、图片、文件、只有链接）照常入队，但 `start_held_until` 设成 30 秒后（`HOLD_SECONDS`）。这期间主人的文字经 `merge_before_start` 并进请求并立即放行，写进 `asked_text`；到点没等到就开工。这个时间不存盘。

### 直答与转交

规则在 `app/prompts.py` 的 `butler_rules`（`AREA_OPENING`、`AREA_RULE`），放在每轮用户消息开头：

- 只读、一两分钟能做完的，管家自己做、直接答；只问已有结果的用 `topic_read` 读了自己答。
- 改代码或文件、要跑几分钟以上、几件事并行、主人说开话题的，交给话题：新的事 `topic_start`，同一件事 `topic_send`；两个以上话题都对得上时用 AskUserQuestion 让主人选。
- 交出去后只说一句放到了哪，不报进度和结论（结论由 Bridge 回带，进度看 Pin）。
- 写给人看的工具说明用主人的语言。

**开工三档**（`starts_at_once`，设置 `start`）：只读的、主人在这条消息里亲口要的、主人记住过的“工作区 × 只读/改文件”直接开工；其余在话题里出预填好的启动卡。

### 管家规则

`COVEY_BUTLER_RULES=1` 时，[直答与转交](#直答与转交)的前两条换成 `app/prompts.py` 的 `BUTLER_RULES`（两条），让管家只快答和分派，这样它接得住主人的连问；`topic_start` 的工具说明里同样的话也换了（`core/butler_tool_specs.py` 的 `START_BUTLER`；`core/butler.py` 的 `mcp_config` 把 `COVEY_BUTLER_RULES` 一并交给 MCP 子进程）；其余规则不动。

- **快答**：不用工具，或只用一两次只读的查询（看一眼文件或日志、查一项配置、`topic_read`、`topics_list`），几秒内能答完，直接答、不开话题。
- **派出去**：要查一圈、要跑命令、要改代码或文件、要联网或多步操作、预计超过半分钟的；拿不准也派。新的事 `topic_start`，和已有话题是同一件事 `topic_send`。
- **派出去后说什么**：一句话，放到哪个 T 号、大概多久，如“放到 T42「标题」里做，大概十分钟”；不写进度和结论（`AREA_RULE`）。
- **例外**：主人明确说“你直接做”“别开话题”“就在这儿做”时，这一轮自己做完再答。
- **兜底**：自己干了 90 秒的一轮照旧出卡（`CARD_AFTER`），不另设变量。
- **打开和关掉**：见[配置 · 管家规则](configuration.md#管家规则)。关着时规则、提示词和原来逐字节相同（`tests/test_butler_rules.py`、`tests/golden/butler_prompt.txt`）。

**什么时候生效**：`butler_message` 每一轮都重新拼规则，放在这一轮用户消息的开头，续接的会话也一样。所以服务带着变量重新注册之后，管家的下一个聊天轮就用新规则。已经在跑的那一轮用旧规则；续接的会话里以前各轮的消息开头还带着旧规则的原文，压缩之前模型会同时看到新旧两条，以最近一轮的为准。想立刻干净，让管家压缩一次或换新会话。

### 同时做几件事（开关默认关）

`COVEY_BUTLER_TRACKS=1` 打开（[配置](configuration.md#环境变量与选项一览)）。管家这一轮在做事时，主人的下一句不被挡在后面：管家变成一个**话题家族**，现在的管家话题之外再有固定 4 路，每路是管家会话的一个分叉，各自跑一轮普通的管家任务。`COVEY_BUTLER_TRACKS_MAX` 是同时在跑的件数上限（默认 3，取值 2 到 4），到了上限的新事排在最空的那一路后面。

- **新的一句去哪**：管家没有在做的事时，和没开一样。有在做的事时，先按规则判断（引用了哪件事的卡或消息就给哪件；转发、图片、文件、只有链接的消息之后 30 秒内的文字并进那条消息去的那件事），规则判不出时问小模型（`COVEY_TRACK_JUDGE_*`，默认硬上限 3 秒，超时按新开一件）：并进某件在做的事（作为补充，那件在等你回答时就是回答）、另起一件、或停止。
- **停止**：说“停”“别做了”这类话停掉对应的那件，不建任务、不进模型。回复末尾写“还有 k 件事在做。”；已经有任务卡的写在卡的结尾。被主人停掉的那件不再发答案。
- **表情**：排在忙的那一路后面的消息挂“排队中”，开跑时换成 OnIt；并进成功的消息挂“并进”，主人引用它说“单独答”时摘掉、另起一件。
- **别的几路答了什么**：每一路是分叉，它答了什么不在管家自己的会话里；管家的下一轮开头多一块“另外几件事这段时间答了什么”（12 小时内、最多 8 条），新开的那一路也带上，所以管家记得住。账本保留 7 天。
- **开了又关**：几路的话题留在盘上，这时它们是普通话题，回答、引用老卡照样能接着用。

底座（槽、分叉点、家族谓词）和运行时（分流规则、小模型的待命进程、选槽、账本、表情和停止的做法）见[架构 · 管家同时做几件事](architecture.md#管家同时做几件事默认关)。

### 提问不开串

话题挂在提问下面时管家这一轮默认不发卡（`(AREA, BUTLER)` 格子的 `Row.quiet`）。`placement.quiet` 还要求消息是主人自己的（`om_` 开头）或回答问题后的续接一轮（`CONTINUATION`）。

- **入队**：`app/submission.py` 设 `job.card_deferred`，不发卡；主人那条消息加表情 OnIt（`app/butler_reactions.py`，`Messenger.react`）。
- **卡什么时候出**：跑过 `CARD_AFTER`（90 秒）还没结束，任务进程发卡（`Cards.reveal`），进主人那句话的话题串。只试一次。
- **结束表情**（`butler_reply.react_end`）：DONE（自己答的）、Get（交给话题）、CrossMark（失败、被拒）；停止和等回答只去掉 OnIt。失败只记日志。
- **说话人**：聊天区每条顶层的答案、结论、问题卡副本和通知，“你问”块的第一行用一句话写谁对你哪句话做了什么（`core/board_read.py` 的 `speaker_of`，词表 `core/look.py` 的 `SPEAKERS`，句子 `cards/answer_card.py` 的 `SAYS`）：“**管家** 答复你 12:31 的问题 ›”，话题回带按结果写“**T12 标题** 做完了你 09:12 交代的事”“……要你定”“……时停了”，下一行是「你的原话」；负责人自己醒来的一轮写“**专项「名」· 负责人** 主动来汇报 · 09:40”。派发卡按任务状态写“在办你…交代的事”“做完了…”“…要你定”“…时停了”；专项话题的问题卡副本不引原话，只写“**T15 标题** 办负责人派的事，要你定”。
- **回复交给谁**：引用话题的回带交给那个话题，引用负责人的回答或专项卡交给负责人，引用管家的回答交给管家；引用手上的事或早晚汇总卡（Bridge 自己发的）也交给管家，不在它上面开新话题（`Butler.app_for`）。
- **答案**：聊天区新消息，带“你问”块。只做了交接的一轮（`handed_quietly`）不发答案，Get 就是回执；转给了已有话题（topic_send）的，再在主人那条消息下回一个小卡（`cards/answer_card.py` 的 `hook_card`）：每个话题一行“→ T12「标题」 ›”，点了打开那个话题串、定位到转入的那条。它是引用主人消息的顶层回复，不开串；只挂一个话题时，引用这张小卡说话进那个话题。新开或分叉的话题串本来就挂在主人那条消息下面，不列。
- **没卡时的失败**：发一条带“你问”的短消息（`app/quiet_delivery.py` 的 `top_notice`），被拒或停止时列出收下的补充；答案没发出去时另发一条带按钮的（`undelivered_notice`）。
- **问题卡**：还没有卡时，问题卡本身作为聊天区新消息发出（`QuestionDisplay.ask_block_for`）。要主人定的事，`AREA_OPENING` 的第三条要求管家写进检查点的 `requires_user`、`question` 和 `questions`，不只在回复末尾问一句。管家和负责人的问题也进“等你”：手上的事、看板和早晚汇总另读 `core/board_read.py` 的 `top_questions`，这一行写说话人（“管家”、“专项「名」· 负责人”），不写 T 号。
- **重试、续跑、续接**：原来没卡的同样延后发卡，表情落在主人原来的消息上（`placement.owner_message`）；回答问题后的续接一轮不加表情（`placement.marked` 只认 `om_`）。

### 一次请求一张卡

`app/butler_reply.py`。回答先照旧经 `verified_reply` 保存、授权（规则 5）。

- **开了或转了话题**（`main-thread/opened/` 里这一轮成功的记录，`core/butler_opened.py` 的 `kind_of`）：写在任务卡上，每个话题一行（“↪ 开了 **T14**「标题」”“↪ 转给 **T13**「标题」：note”），模型的话折进“详情”。
- **自己答的**：按放置表 `answer` 作为聊天区新消息发（`app/answer_delivery.py`）。
- **退回单独发**：没完成、要以文件发、超过卡片字数或写卡失败时，单独发回答卡（重投只发保存的正文）。
- **状态行**（`outcome_words`，`Cards.say_outcome`）：“✅ 已答：开头”“↪ 开了 T14「标题」”“↪ 交给 T12 接着做”“↪ 补充给 T14（它正在跑）”“↪ 分叉出 T15「标题」”“↪ 拆成 2 件”。
- **停止和被拒**：卡上列出收下的补充，说明不会自动重发；官方拒绝时写在 `app/policy_guard.py` 重画的恢复卡上。

**分档卡**（`slim_card`）：没有标题栏，正文、一行状态（`cards/task_status.py` 的 `slim_status_markdown`），其余折进默认收起的“详情”；样子见[卡片](cards.md)。

## 会话

`app/butler.py`，只在开关开着、任务属于管家话题时起作用。

- **回复位置**：`Butler.configure` 在 `build_context` 里（Bridge 和任务进程都走）给 `feishu/message_request.py` 的 `reply_in_thread` 设判断：对管家消息的回复进那条消息的话题串。开话题就用这个串。
- **说法**：`worded` 按 `WORDING` 改 Bridge 自己写的文字（“本话题” → “私聊里”），模型的回答和主人原话不动。
- **快照**：紧接规则，`snapshot_groups` 从 `core/board_read.py` 读（不调飞书，不持记录锁）。四组：等你回答（和汇总、看板、手上的事同一批）、有结果未看（看板的口径，见[看板与 Pin](#看板与-pin)）、进行中、管家开的话题；开了专项还有一栏“专项”。存到 `topics/<id>/main-snapshot.json`，下一轮只列变了的行；新会话、压缩过、上一轮没正常结束时带全量。检查点有 `steps` 时写第几步和本步用时。看板撤下的话题不进前三组。
- **MCP 配置**：不改 `runs/` 协议。`butler_turn` 算出 `mcp_config(...)` 放进 `ctx.options["mcp_config"]`；`backends/claude/runtime.py` 据此加 `--mcp-config`、`--allowedTools mcp__bridge`，并设 `ENABLE_TOOL_SEARCH=false`，让工具随会话加载（否则模型会发出不带参数的调用）。不加 `--strict-mcp-config`。所有后端都不走系统级，规则和原则都在 `RunRequest.prompt` 里。
- **压缩**：一轮送达后上下文到了 `compact_tokens`（默认 20 万，0 不压），`compact_butler` 写压缩请求。`/compact` 带 `COMPACT_INSTRUCTIONS`：摘要约 2000 token 以内，只留待办、话题、未决问题和偏好。话题没有自动阈值。
- **空闲压缩**（`app/idle_compaction.py`）：任务进程退出后等 `IDLE_SECONDS`（90 秒）；那时这一轮是 `done`、话题里没有任务、请求没标 `failed`，就起一个只压缩的任务进程（`covey_runner.py --compact-only`），标记写在 `<state>/main-thread/compacting/<话题>/`，不建任务、不出卡。成了写 `compacted.json`，没成标 `failed`、下一轮开头先压。压缩中主人发来新消息时，新一轮停掉它、放回请求。上下文到阈值 `FALLBACK`（1.5）倍的、`failed` 的，在下一轮开头压缩。Bridge 启动时清理遗留标记。
- **已下线的前台话题**：`main-thread.json` 的 `desk_topics` 读到 `true` 也按 `false`；以前开的前台话题由 `core/migrate.py` 的 `desk-retire` 冻结（topic.json 加 `retired: "desk"`），引用它旧卡的消息交给管家，旧卡上的按钮只回一句 `RETIRED_TEXT`。

## 卡放在哪

### 放置表

`core/placement.py`：布局 × 角色 → 格子 `Row`。调用处只读格子；只有这一轮知道的事（开没开话题、多快、是不是主人自己停的）留在调用处。

- **布局**（`layout()`）：管家开着是 `area`（话题挂在提问下面），否则 `topics`。
- **角色**（`ROLES`）：`butler`（`BUTLER`、`BUTLER_OFF`）、`topic`；`COVEY_PROJECTS=1` 时再有 `lead`（`LEAD`、`LEAD_OFF`）和 `project_member`（读 `topic.json` 的 `project.role`），见[架构 · 专项](architecture.md#专项第-2-期开发中默认关)。
- **位置**：`TOP` 聊天区新消息、不引用任何卡；`THREAD` 上下文的回复目标（话题串）；`NONE` 不发。
- `plan(state_dir, of)` 返回 `Placement`（`traits`、`layout`、`role`、`row`）。

| 格子 | `reply_top_level` | `answer` | `answer_on_card` | `conclusion` / `stop` | `question_copy` | `ask_block` | `status_card` | `answer_head` | `quiet` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `area` × `butler` | 否 | TOP | `ROUTES` | NONE | TOP | 是 | 否 | 否 | 是 |
| `area` × `topic` | 按 `Traits` | THREAD | 无 | TOP | TOP | 是 | 否 | 是 | 否 |
| `area` × `lead`（专项） | 否 | TOP | 无 | NONE | NONE | 是 | 否 | 否 | 是 |
| `area` × `project_member`（专项） | 按 `Traits` | THREAD | 无 | NONE | TOP | 是 | 否 | 是 | 否 |
| `topics` × 任意 | 否 | THREAD | 无 | NONE | NONE | 否 | 是 | 否 | 否 |

专项负责人的**唤醒轮**（消息 id 以 `lead-` 开头，`core/projects.py` 的 `is_wake`）也算安静轮：不显示任务卡、没有表情；它的已授权回复默认不发（`TurnDelivery.silent`），除非负责人在这一轮里记了一条按风格该报的 `report` 进展，或这一轮派了活、停了话题、替主人答了或转交了问题（专项记录的 `acted`）。负责人的回答从不写在它的任务卡上：主人发起、跑得久已经亮出任务卡的那一轮，结论也是顶层一条新消息，带说话人“专项「名」· 负责人”。

| 字段 | 含义 | 读者 |
| --- | --- | --- |
| `reply_top_level` | 回复主人消息放顶层还是进串（None 按 `Traits.top_level`） | `Butler.configure` |
| `answer` | 已授权回复单独发到哪 | `TurnDelivery.send`、`app/redelivery.py` |
| `answer_on_card` | 写在任务卡上：`ROUTES` 开了或转了话题时 | `answers_in_place` |
| `conclusion`、`stop` | 做完、停止或失败的回带 | `butler_relay.after_turn` |
| `question_copy` | 问题卡的顶层副本 | `QuestionDisplay.mirror` |
| `ask_block` | 顶层新消息带“你问”块 | `app/answer_delivery.py` |
| `status_card` | 看板 Pin 状态卡，否则 Pin 这一轮自己的卡 | `app/pins.py` 的 `status_pinned`、`aimed` |
| `answer_head` | 话题串里的答案卡顶上带状态行，任务卡不写“结论”；只在主人私聊 | `AnswerParts.answer_head`、`headed` |
| `quiet` | 这一轮不开串 | `placement.quiet`、`marked` |

### 消息落在哪

- **管家的卡**在主人那句话的话题串里，跑过 90 秒才发。
- **答案、结论、问题卡副本**都是聊天区新消息，不引用任何卡：主人那句话下面已有话题串，回复它会掉进串里。
- **“你问”块**（`cards/answer_card.py` 的 `ask_block`）：灰底两行“你问 · 时间 ›”和原话，整块可点。话题的轮次（结论回带、问题卡副本）跳到那个话题的话题串，定位到这一轮的卡（`topic_links.topic_thread_link`）；管家自己做的轮次跳到主人那句话在聊天区的位置。话题根读不到话题串时退到根在聊天区的位置，什么都读不到时不给链接，两种都在日志记一行 `ask link fell back to the chat` / `ask link missing`。文字仍是主人原话：转交的轮次用管家接这件事的那句话。
- **“你答”块**：表单回答开的一轮写“你答 · 时间 ›”和“问题 → 回答”，链接规则和“你问”一样。只有哪里都跳不了时才用默认收起的折叠（`answer_block`），展开是每道题和完整回答。

## 工具通道

```
管家会话 ── MCP（stdio）── bin/covey_butler_mcp.py
                                │ 写 <state>/main-thread/requests/<id>.json，等 <id>.answer.json
Bridge 进程 ── app/butler_tools.py：pending → claim → 核对 job_key → 处理函数 → answer
```

- **请求** `{"id", "tool", "args", "job_key", "created_at"}`，**回答** `{"ok", "text", "data"}`。`job_key` 来自 `COVEY_BUTLER_JOB`，状态目录来自 `COVEY_BUTLER_STATE`，都由 `mcp_config` 写进配置。
- **MCP 服务**（`core/butler_mcp.py`，只用标准库）：每行一条 JSON-RPC 2.0；`tools/call` 每次一个后台线程。等回答最多 120 秒（`butler_requests.submit`）。
- **执行**（`ButlerTools`，后台线程 `main-tools`）：启动时把认领了没回答的（`orphans()`）回 `UNCERTAIN`，不重跑（规则 1、6）。之后每 0.3 秒取 `pending()`，`claim()` 成功才执行，一次一个。只接受任务记录存在、`topic_id` 是管家、状态在 `RUNNING` 里的调用，否则回 `REFUSED`。循环不持锁，飞书调用都在锁外（规则 1、2）。
- **处理函数**：`handler(env, request) -> dict`，`env` 是 `app/butler_env.py` 的 `ButlerEnv`；各模块用 `TOOLS` 字典登记，`app/unified.py` 合起来交给 `ButlerTools`。
- 话题写 T 号或 topic_id，只认主人私聊的；标题只来自 `core/topics.py` 的 `topic_title`。

### 工具清单

照 `core/butler_tool_specs.py`（* 必填）：

| 工具 | 参数 | 做什么 |
| --- | --- | --- |
| `topics_list` | group、query、limit | 按等你回答 → 有结果未看 → 进行中 → 最近列话题 |
| `topics_search` | clues*、limit | 按线索找是哪个话题哪一轮做的 |
| `topic_read` | topic*、job、turns | 读话题最近几轮：问题、结论、检查点、没答的问题 |
| `workspaces_list` | 无 | 列工作区：名字、路径、用途、最近的话题 |
| `topic_start` | title*、prompt*、workspace*、readonly*、user_requested*、why*、attachments、harness、model、effort | 为新的事开话题 |
| `topic_send` | topic*、text*、note、title、attachments | 把主人的话追加给已有话题 |
| `topic_fork` | topic*、title*、prompt*、attachments | 从已有话题分叉 |
| `question_answer` | topic*、answers* | 替主人答选择题 |
| `topic_priority` | topic*、remove | 看板本组内提前，或撤下 |
| `principle_save` | scope*、workspace、text* | 记下主人的原则 |
| `principle_delete` | id* | 删一条原则 |
| `principles_list` | 无 | 列出原则 |
| `settings_panel` | 无 | 在私聊发设置卡（`app/butler_panel.py`） |
| `knowledge_save` | scope*、kind*、text*、workspace、why、source_topic | 记一条知识条目（只有 `COVEY_KNOWLEDGE=1` 才列） |
| `knowledge_update` | id*、text*、reason*、kind、why | 改写：新 id 取代旧 id（同上） |
| `knowledge_drop` | id*、reason* | 作废一条（同上） |
| `knowledge_search` | query*、workspace、history | 搜当前条目；`history` 时也搜已取代、已作废的（同上） |
| `history_search` | query*、workspace、since | 搜 Claude、Codex、Bridge 的历史轮次（只有 `COVEY_HISTORY_CMD` 是绝对路径才列） |
| `projects_list` | 无 | 列专项：id、名字、状态、专项的话题的 T 号、负责人最近一句（只有 `COVEY_PROJECTS=1` 才列） |
| `project_send` | pid*、text* | 把主人的话交给专项的负责人，按主人触发的一轮处理（同上） |
| `projects_tidy` | scope | 整理一遍专项（只在开了[整理专项](projects.md#整理专项)时列） |

- **专项**（`COVEY_PROJECTS=1` 时，Bridge 把它交给 MCP 服务）：管家的清单多出 `projects_list`、`project_send`（`app/butler_projects.py`），`topic_start`、`topic_send` 各多一个可选的 `project`。
  - `topic_start` 带 `project: true` 不开话题，开一个专项（`Projects.start`：记录、负责人话题、专项卡、`start` 回执），`title` 是专项名，`prompt` 是目标和完成标准，主人要收进来的已有话题放 `topics`（T 号的数组），挂进专项、不打断它们，挂不上的回复里逐个说原因。
  - `topic_send` 带 `project: true` 是管家判断这个话题要转成专项（主人要长期跟进、分成几个话题持续推进，或直接说“转成专项”；拿不准不带），`COVEY_PROJECT_UPGRADE` 不是 `off` 才生效。
  - 专项的话题不接 `topic_send`，回一句改用 `project_send`；`project_send` 的 `pid` 写 id、id 的开头或名字里的几个字，在跑的负责人那一轮收到补充，没有就开它的下一轮（消息 id `main-…`，算主人触发的一轮）。
  - 关着时清单和原来一字不差。专项本身见[专项](projects.md)。
- 查询类工具（`app/butler_tools.py`）只读记录，不持记录锁。`topics_list` 的“有结果未看”（`group: result`）是看板的口径（`board_results`），“最近”是 `TOOLS_AGE`（3 天）。`topic_read` 默认读 2 轮，检查点列阶段、已完成、产物、下一步、在问，以及 `steps` 和 `delivery`。
- `question_answer` 只答最新一组 pending 的问题：每题都要答，答案是选项原文，有自由文字题就整组拒绝。经 `app/answers.py` 的 `answer_fields` 转成表单字段，补充说明写 `PROXY_NOTE`，再经 `app/questions.py` 的 `submit_answer` 提交，和卡上点“提交回答”同一条路。
- `topic_priority` 写 `board-priority.json`；`remove` 经 `app/pins.py` 的 `dismiss` 写 `dismissed`，看板没开时回 false。

### 按线索找话题

`topics_search`（`app/butler_search.py`）一次 1 到 `MAX_CLUES`（5）条线索，搜主人私聊每个话题的每一轮。

- **字段**：主人的话、结果（`final_text`）、状态行、检查点的全部字段（除 `SKIPPED`）。不读会话原文和 `traces/`，不搜群聊。
- **线索**：飞书链接取路径里的 token；16 位以上字母数字混合的串当 token；带 `/` 无空白的当路径；其余是关键词。token 写短（至少 `PREFIX` 位加省略号）、路径只对上最后两段，算部分匹配。
- **排序**：类型分（整段 3、部分 2、关键词 1）乘出现方式分（`CLASS_WEIGHT`：第一次出现、结果里也有、只是提到），“之前”按任务编号跨话题算。默认列 10 轮，按话题分组，带片段。
- **开销**：结束任务的检查点读一次后留在内存，后台线程 `main-search` 启动后预读。不加锁、不调飞书、不写文件。

## 开话题、转交、分叉

`app/butler_topics.py`，只在主人私聊里动作，飞书调用都在锁外（规则 1、2）。

- **`topic_start`**：
  - 工作区只认已登记的名字（`core/workspaces.py`），设置了 `topic_defaults.workspace` 就总用它。
  - Harness、模型、预算逐项取值（`app/butler_model_choice.py`）：调用给的 > `topic_defaults` > 这个私聊上次开始任务的配置 > Bridge 默认。上次的值已不可用时往下找；调用给的不可用时回错误并列出可选。主人点名用哪个 Harness、模型、思考预算时，工具说明和管家规则都要求照填（`harness` 写 id，也认显示名，如 Claude Code，不分大小写、忽略空格和连字符）；没点名就不填。
  - topic.json 写 `origin`（`kind: "main"`、`request`、`job_key`、`why`、`readonly`、`user_requested`、`title`）。
  - `starts_at_once` 为真时照分叉绑定配置（`workspace_locked`、`harness_locked`），第一轮直接提交；否则（或运行方式是独立 worktree）话题里出启动卡，预选在 `origin.launch`。
- **`topic_send`**：管家自己和代答话题不接。`text` 是任务说明，`note` 一句背景，都只给模型。
  - 有没结束的任务：作为补充交给最新那个（`deliver_steer`），话题串里出“管家转来你 HH:MM 的补充：”加主人原话。被拒（`SteerRefused`）、不确定（`SteerUncertain`）都照实回答，不重发（规则 6）。
  - 已经停了：串里出“管家转来你 HH:MM 的消息：”，再照话题里的消息处理（有没答的问题就作为回答，否则开下一轮）。还在等启动卡的话题不转。
  - 串里这条转入消息是一张灰底卡，整块可点，跳回主人那句话在聊天区的位置（`cards/answer_card.py` 的 `handoff_card`）；读不到那句话的位置时照旧发文字。卡的消息 ID 记在转交记录的 `thread_message_id`。
- **`topic_fork`**：从源话题最近一次结束的任务分叉（`app/forks.py` 的 `create_fork`），绑定源话题的配置和脱敏，复制原生会话。源话题在跑或排队就回错误。
- **话题卡**（`cards/topic_card.py`）：“T12 · 标题”加一行灰字配置，没有按钮，拿到 T 号后整卡更新一次。
- **幂等**：每次调用在 `<state>/main-thread/opened/<请求>.json` 记做到哪一步（`core/butler_opened.py`）。发送键和第一轮的消息 id（`main-<请求>`）从请求 id 派生；同一个请求再来返回存下的回答。

### 交给话题的消息

`app/butler_handoff.py`。交给话题的每一轮（开、下一轮、补充、分叉），模型收到 `handoff_prompt`，卡、历史和串里显示主人原话（`shown`、`thread_line`）。

- **Bridge 的说明**（`note`，开头 `[Bridge 说明]`）：你在哪个话题；这件事是管家按主人哪条消息交来的；路由已经办完，直接做事；你的回复就是结论。
- **任务说明**：管家写的 `prompt`/`text`，后跟“管家补充：note”。
- **主人原话**（`asked`）：管家这一轮记录的 `user_text` 加运行中收到的补充，按时间引用。
- **附件**（`carry`）：只认管家话题 `incoming_media/` 或 `<state>/incoming_media/` 下的文件（`core/attachments.py` 的 `parse_attachment_block`），按参数 `attachments`（`all`、`none`、序号）挑出，硬链接或复制到目标话题的 `incoming_media/`。补充只送文字。

## 话题

管家派出去的每件事是一个话题：话题串挂在提出它的那条提问下面；同一件事用 `topic_send` 追加。

- **开**：管家开着、查得到提问、提问的别名还指向管家时（`core/topics.py` 的 `claim_area_root`），以这条提问为根：先把别名改给新话题（`move_topic_message`），再建上下文，不发话题卡。`opened/` 记 `area_root`、`area_taken`。
- **一条提问只挂一个串**：同一条提问的第二个话题、查不到提问时，根是顶层新发的话题卡；分叉挂不上时把源话题合并转发到私聊当根。第二个话题的“你问”仍写那条提问，链接打开它自己的串。
- **引用直达**：引用专项负责人的回答，交给负责人（`app/butler.py` 的 `app_for`；负责人算管家一家，`core/butler.py` 的 `is_lead`）。管家这一轮的卡只交给了一个话题时，卡的别名记上 `routes_to`。回带的结论、问题卡副本也记成话题的消息并带 `routes_to`。引用它们就是对那个话题说话（`context_for_topic` 先看 `routes_to`），不经管家。纯话题模式下串里不看 `routes_to`。

## 结论回到顶层

管家开的或转过去的那一轮结束时，Bridge 在聊天区发一条新消息，顶上是“你问”块。只在管家开着时；不调模型。

- **哪些轮**（`relay_targets.relay_target`）：消息 id 是 `main-<请求>` 的任务、`topic_send` 作为补充送进的任务、它们的自动重试（`retry_of`）。话题里回答问题后接着跑的、主人在串里开的一轮也回带（`area_target`）。管家没碰过的话题不回带。
- **什么时候**：任务进程投递完、任务卡写完之后（`TurnDelivery.deliver` → `butler_relay.after_turn`），出错只记日志。

| 结果 | 回带 |
| --- | --- |
| 完成，回答已送达 | 结论卡（`cards/relay_card.py` 的 `relay_card`）：回答全文、产物、输入/输出灰框，末尾 T 号链接。只用 `turn.verified_delivery`（规则 5）。超过 `CARD_TEXT_BYTES` 分几条 Markdown 发，再补一张小卡 |
| 等你回答 | 问题卡顶层副本；放不了时发回带卡：结论带“等你 · T10”和“要你定：”问题（`relay_question`） |
| 失败或停止，没排上自动重试 | “**T10 停了**：状态行”；主人自己点的停止不发，记 `skipped`、`why: owner_stop` |
| 没送达、排上了自动重试 | 不回带 |

- **只发一次**（`app/relay_send.py` 的 `send_once`）：记录 `<state>/main-thread/relays/<任务键>.json`。先画好卡，文件锁下写 `submitting`（不用全局 `LOCK`，规则 1、2），锁外发，发送键从任务键派生；发完写 `sent`，没确认写 `uncertain`，都不重发。写 `submitting` 前出错的记 `unsent`，不补发。日志 `main relay state=…`。可选部分（产物、输入/输出）出错时去掉那一项照发。
- **产物**：答案卡、回带卡记在 `<state>/answer-cards/<任务键>.json`，上传结束或“重新上传”后整卡改写（`app/answer_cards.py`）。
- 回带消息是话题的消息，看过它就算看过话题。
- `main-thread/opened/`、`relays/`、`answer-cards/` 7 天没动的由维护删。旧 release 的任务进程不回带。

### 问题卡在顶层也放一份

话题的问题在顶层也放一份同样的表单（`app/question_display.py` 的 `mirror`，卡 `cards/question_cards.py` 的 `mirror_card`、`top_level_card`），聊天区新消息、带“你问”。在哪张上答都一样，答了两张一起收起。

- **表单上面是这一轮的输出**：原生问题是到这时的进展说明和回答；检查点问题是任务卡正文。超过 `MIRROR_OUTPUT_BYTES` 的先单独发一条。
- 只有一道有选项的单选题时，每个选项一个按钮，点了就提交（`app/answers.py` 的 `picked_fields`）。
- **记录**：问题记录多 `mirror_state`（`creating` → `sent`/`uncertain`，锁内写、锁外发，规则 1）、`mirror_message_id`、`mirror_card_id`；别名记成话题的并带 `routes_to`。
- **一起收起**：`QuestionDisplay.paint` 也重画副本（`paint_mirror`，sequence 从映射 `reserve`，规则 3）。
- 副本没能发出时，原生问题回带一行（`relay_question`，记在 `main-thread/relays/questions/`）。
- **专项的话题也放**：专项的话题是负责人开的，没有管家的请求；它的问题照样放到顶层，不带“你问”块，卡上的话题号说明是谁在问。交给负责人分流的问题这一轮结束时不放（还不归你）；负责人转给你时，在话题串里发卡之后补放这一份（见[专项的问题分流](projects.md#专项的问题分流)）。

## 看板与 Pin

管家自己不进看板，它开的话题照常进。看板、手上的事、早晚汇总、管家的快照和 `topics_list` 都从 `core/board_read.py` 的 `read` 读，“等你”和“有结果未看”五处一个口径（以看板为准，经 `app/pins.py` 的 `board_results`）：话题最新一个跑过的任务完成、失败、中断或停止且主人在它结束后没读过，就是有结果未看，不论多久。使用者看到的样子和各组的规则见[使用指南 · 看板](usage.md#看板)和[手上的事](usage.md#手上的事)，开关见[配置 · 手上的事与话题看板](configuration.md#手上的事与话题看板)。

### 个人空间的管家与看板

`spaces.personal_groups` 里的群（和 `owner_accounts` 的私聊）各有自己的管家：话题 id 按 chat 算（`butler_topic_id(chat)`），记录不混；`Access.agent_chat` 判断哪个 chat 有管家，`ButlerEnv.chat()` 在工具调用里取这个管家的 chat。个人群没有已读回执，主人在群里发的消息算读过它的看板（`app/group_boards.py` 的 `owner_active`）。个人群各有一份手上的事、看板和早晚汇总，记录名带 chat_id（`pinned-topics.<chat_id>.json`、`pinned-overview.<chat_id>.json`、`digest.<chat_id>.json`）。设置见[配置的空间一节](configuration.md#空间spaces)。

## 主人的原则

Bridge 记一份主人的长期原则，所有 Harness 的话题和管家都带上。

- **存储**（`core/principles.py`，只用标准库）：`<state>/principles.json`，`{"version": 1, "owner": [...], "workspaces": {"<工作区>": [...]}}`；每条 `{"id": "p" + 6 位十六进制, "text", "at", "source"}`。一条最多 `TEXT_LIMIT`（200）字，每组最多 `GROUP_LIMIT`（30）条，满了拒绝。写入在 `shared_file_lock` 下整体替换，读不加锁；文件坏了当空的读，下次写前改名 `principles.json.bad-<epoch>`。
- **工具**：`principle_save` 的 `scope` 是 `owner` 或 `workspace`（要已登记的工作区）；正文相同的返回已有 id。`principle_delete`、`principles_list` 用 `delete`、`entries`。
- **注入**（`app/runs.py` 的 `Runs.principles`，块由 `app/prompts.py` 的 `principles_block` 写）：工作区那组总带；通用那组只在主人自己的一轮带（`Runs.owner_turn`：管家总是，其余经 `Access.owner_turn`，规则 11）。新会话在 `bridge_rules` 里，续接的在 `_with_rules` 补的提醒里，所以新原则下一轮就生效。总长约 `PRINCIPLES_CHARS`（2000）字，超了从旧的截。代答话题不带。
- **原则已授权的不再问**：原则块开头（`PRINCIPLES_HEAD`）和记忆的规则（`COVEY_MEMORY=1`）都写着：要问主人之前先看原则，原则里已经同意的事照着做，不要再问。

**以后这类按推荐走**（`app/standing_answers.py`；`COVEY_STANDING_ANSWERS=1` 打开，不设是关，`./install` 新装的服务写 `1`）：

- 话题里的问题卡上，每道题都恰好有一个标了“（推荐）”或“(Recommended)”的选项时，表单下多一个按钮「以后这类按推荐走」（不在表单里，不提交它）。只认主人的点击。
- 点了以后小模型（Haiku，最多 20 秒；开着脱敏的话题不发内容）把这道题概括成这个话题工作区的一条原则，例如“这个工作区：测试通过就合并进 main 并推送；没有在跑的任务时直接部署，不用再问”，另发一张卡给主人看这句话，带「记下」「改一下」「不管了」；小模型没给出时用题目和推荐项拼一句。
- 「改一下」把这句话放进输入框重画整张卡；「记下」才记成工作区原则（`source.by` 为 `standing_answer`），同时这道题还在等时按推荐项回答；已经答过或取消的只记原则。
- 话题没有工作区、题已经不在等、没有唯一的推荐项时不出卡，回一句为什么。卡的记录在 `<state>/standing-forms/`，卡只整张换。
- 部署也可以这样长期授权：记下“空闲时直接部署”就等于主人提前开了口。

## 知识库

主人说“记一下”的东西集中存放，所有 Harness 的新会话都带上，管家能搜索和修改。`COVEY_KNOWLEDGE=1` 才开（[配置](configuration.md#知识条目与历史搜索)）；关着时不建目录、规则和提示词一字不变、工具清单里没有这些工具、汇总里没有这一节。

- **存储**（`core/knowledge.py`，只用标准库）：`<state>/knowledge/owner.jsonl`（通用）和 `workspaces/<工作区>.jsonl`，只追加的事件，一行一个：`add`；`update`（新 id，带 `replaces`，旧的变已取代）；`drop`（必须有 `reason`）。读的时候从头重放，得到每条的状态（当前、已取代、已作废）。同目录有生成的 `.md`（每组当前条目）和 `owner-brief.md`（和注入同样 `KNOWLEDGE_CHARS` = 1500 字的摘要）。写在文件的跨进程锁里追加一行，锁里不调飞书、不起子进程；生成的 `.md` 写不出不影响事件。读不了的文件当空，下一次写前改名 `*.bad-<epoch>`；不是事件的行跳过。
- **条目**：`kind` 是 decision、practice、pitfall、fact、question；正文最多 200 字，`why` 可空；`by` 是 main（管家的工具）、topic（话题里收集的）、import。同一组里正文去掉空白、大小写和句末标点后相同的不再新增，返回已有的 id。
- **管家**：见上面的工具清单（`app/butler_knowledge.py`）。规则里写：“记一下”调 `knowledge_save`（具体的事实、决定、坑；以后每次都要遵守的做法仍用 `principle_save`）；“把 T12 的结论记一下”先 `topic_read` 再起草；记完列出 id 和正文；“改成”“删了”用 `knowledge_update`、`knowledge_drop`；查以前的事按 `knowledge_search` → `topics_search` → `history_search`（`app/prompts.py` 的 `knowledge_rules`）。工作区名要已登记，不认识时返回可选的名字。
- **话题里说“记一下”**：规则里多一句（`REMEMBER_RULE`，只给主人自己的一轮、不是管家、不是代答、没开脱敏的话题）：只有主人这一轮明确说了才把要记的写进检查点的 `remember: [{kind, text, why, scope}]`，最多 5 条，并在回复里列出。收集在 Bridge 进程里（规则 8），任务进程退出之后由 `app/knowledge_collect.py` 的 `collect` 写，同时满足才写：这一轮正常结束（`done` 或 `waiting_user`）、`owner_turn`、原话里有“记一下 / 记住 / 记下来”、不是管家转交的、不是代答也没开脱敏（配置读不出按开了处理）。不满足记 `knowledge remember ignored reason=…`；同一个任务键只写一次；失败只记日志。
- **注入**（`app/knowledge_turn.py`，块由 `app/prompts.py` 的 `knowledge_block` 写，放在原则后面、检查点约定前面）：工作区那组谁的话题都带，通用那组只在 `owner_turn` 时带；从新到旧排，总长 1500 字，超出的截掉最旧的，末行写另有几条和 `.md` 在哪；新会话全量带，续接的会话只补话题上一轮结束以后新增或改动的；代答话题不带；没有条目整块不出现。
- **查历史**：设了 `COVEY_HISTORY_CMD`（绝对路径）时，主人自己的话题多一行“用 `<命令> search <一句话>`，不要自己 rg 状态目录或会话目录”，管家多 `history_search`。命令本身（轮次索引、混合检索）不在本仓库；`history_search` 在自己的进程组里跑，70 秒超时整组杀，输出最多 20 KB，命令不存在、不能执行、超时、非零退出都回一句中文。
- **汇总**：`app/digest.py` 的 `knowledge_view` 在汇总卡和菜单「看板」多一节“新记的知识”：上次汇总以来新增、改写、作废的条目，最多 10 行，多的写“另有 N 条”；没有变化没有这一节，它自己不会让汇总卡发出来。
- **回退**：去掉两个变量重新注册；已经写下的 `knowledge/` 留着。

## 记忆（开关默认关）

`COVEY_MEMORY=1` 才开（[配置](configuration.md#记忆)）；关着时规则、续接提醒、答案卡和任务记录逐字不变，不读不写话题的 `memory.json`。它把“主人的原则”“知识库”“专项记忆”和话题自己的记忆合成一种东西，对主人只说“记忆”。

- **一个读口**（`core/memory.py`，只用标准库；每轮的接线 `app/memory_turn.py`，块由 `app/prompts.py` 的 `memory_block` 写）：原则、知识条目、专项记忆（`core/project_memory.py`）和话题记忆（`core/topic_memory.py`）拼成一块，每条一行“`- M12 · 管这个专项 · 你说的：…`”：编号（`M` 加两三位数，取自条目 id 的哈希，同一条每轮不变）、管哪儿（全局、这个工作区、这个专项、这个话题）、谁说的。
  - 各范围有字数上限（全局 1200、工作区 800、专项 2000、话题 800），“你说的”排在前面，同一类里新的先带，放不下的整条不带，末尾说“另有 N 条放不下”。
  - 新会话的规则和续接会话的提醒都整块带，开着时代替原来分开的原则块、知识条目块和专项记忆块。谁读什么没变：全局的只在主人自己的一轮，代答的话题什么都不带，管家也读这一块。每轮带了哪几条记进任务记录的 `memory_ids`。
- **标出处**：原则、知识条目算“你说的”；专项记忆来源 `owner`、`owner_answer`、`owner_fix` 算“你说的”，`lead` 和其他算“它总结的”；话题记忆只有主人的话。块后面跟四句规则（`MEMORY_RULES`）：谁说了算的顺序；“它总结的”默认照做、和主人的话冲突时让路；“它总结的”不能成为多问主人的理由；和 Harness 自己记的冲突以这里为准。来自开了脱敏的话题的条目只带给开了脱敏的话题。
- **纠正落地**（只管话题的一轮；管家照旧用原则工具）：主人这一轮的话里有“以后、每次、别再、不要再、记住、再也不要、从现在起”时，这一轮的规则多一段（`MEMORY_FIX_RULE`），模型可以在检查点里写 `memory: [{quote, text}]`（至多 3 条）和 `memory_void: [{id, quote}]`。
  - 一轮结束后 Bridge（`app/memory_collect.py`）核对：`quote` 必须是主人这一轮原话里的一段；`memory_void` 的编号必须是这一轮带过的、“它总结的”的专项记忆，才作废；新条目按最窄范围记成“你说的”：话题属于专项就记专项，不属于就记这个话题。`COVEY_KNOWLEDGE=1` 且这一轮本来就记知识条目时，“记住”“记一下”归知识条目。
  - 结果写进任务记录的 `memory_notes`，答案卡底部每条一行灰字：“记下了：… · 管这个专项”带“不对”按钮（`bridge_memory_wrong`，不建任务），“作废了：…”。点“不对”撤销那一条（墓碑），这一行改成“已撤销：…”。
- **还没有的**：小模型提取、每句话查一次、问之前先查、全局产物索引、网页上的记忆页；负责人的话题不走这条路。

## 设置

`<state>/main-thread.json`，字段见 `Settings`：`mode`/`enabled`；管家的 `model`、`effort`、`redaction`、`workspace`、`compact_tokens`；新话题的 `topic_defaults`（`harness`、`model`、`effort`、`run_mode`、`redaction`、`workspace`）；子 Agent 用什么模型 `subagents`；直接开工 `start`、记住的 `trusted`、工作区用途 `workspace_notes`。缺的或坏的字段按默认值读。只经 `update` 写，只有设置卡（和网页的设置页，走同一处）能写。

- **管家的脱敏**：从下一轮起走敏感词中继；Bridge 每次交消息前写它的 `topic.json`，中继每轮读（`backends/claude/redaction.py`）。

### 设置卡

管家的设置是设置卡（`app/settings_hub.py`，菜单键 `bridge_settings`）的前两个标签，标签行见 `cards/settings_hub.py` 的 `LABELS`：新话题 · 管家 · 工作区 · 脱敏 · 群聊（· 设备）。菜单打开时停在新话题。画卡和点击在 `app/butler_panel.py`、`cards/butler_panel.py`。新话题一页管所有新话题，代答在菜单里单独一项，私聊模式收在管家页的“更多”里。

| 页 | 内容 |
| --- | --- |
| 新话题 | 所有新话题的预选（启动卡 `app/preselection.py` 和管家开的话题都照它）：Harness、供应商、模型、思考预算、运行方式、脱敏、工作区；管家开着时“直接开工”一行（`START_CHOICES`：都直接开工 = 三档全开，只读的直接开工 = `readonly` + `trusted`，都等你点开始 = 不开），中间那档下面列记住的工作区，每项可删；“更多”里子 Agent：轻量（Sonnet，搜索用 Haiku）/ 跟主模型 |
| 管家 | 管家的供应商、模型、思考预算、脱敏（Harness 固定 Claude Code，写在副标题）；“更多”里压缩阈值和私聊模式（`MODE_CHOICES`）。纯话题模式下只有私聊模式一行 |
| 工作区 | 工作区列表；每行一行灰字写用途，“改用途”换成输入框，“保存用途”存 |

- 不用下拉框：每行点开是选项，点一个就记下并整卡重画。可选项和启动卡同源（`cards/launch_config.py` 的 `tiers`、`budget_options`）。留空的项（“跟上次一样”）写实际会用什么。
- **私聊模式**：管家模式下选“每条直接开一个话题”不马上存，卡上出一句后果（`MODE_CONFIRM`）和“取消”“确定改”；“确定改”是 `bridge_main_enable`（`on: false`），点别的任何一项都收起这句。选“交给管家”马上存。旧卡上的“改成纯话题 / 改成管家”按钮（同一个 kind）照旧马上生效。
- **直接开工**：三档不是 `START_CHOICES` 之一时（以前一档一档开关过），这一行照实写开着的几档，选一项后换成那一项。旧卡上一档一个的按钮（`bridge_main_start`）照旧能点。
- **脱敏默认**：只有新话题页这一行（`topic_defaults.redaction`）。脱敏标签以前的“新话题默认开”（`redaction/config.json` 的 `default_on`）不再读；`core/migrate.py` 的 `redaction-default` 在它开着时把这一行打开一次。
- **子 Agent** 在新话题页，但对整个机器人生效：Bridge 起的每一轮 Claude Code（话题和管家，连同轮前压缩）都照它。
  - `subagents` 是 `light`（默认，没有这一项或值不认识也算）时，进程带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，再用 `--agents` 换掉内置的 Explore：只读（Read、Grep、Glob、Bash），跑在 Haiku 上，只找文件、搜代码、看日志，不改东西、不下判断。这一轮的模型是 Haiku 时不设 `CLAUDE_CODE_SUBAGENT_MODEL`。
  - `same` 什么都不加，每个子 Agent 都跑主模型。
  - `backends/claude/subagents.py` 读这个设置；`ctx.options` 的 `subagent_model` 有值时压过设置（空字符串是不设），也不加 Haiku 的 Explore（安全拒绝后的重试用）。
- 只有主人能改。旧卡的按钮照写：旧行按 `PAGE_OF` 落到对应页，下线的标签按 `RETIRED` 落到工作区页，更早的面板卡由 `SettingsHub.takeover` 接管。
- 旧菜单键 `butler_settings` 还认，发的也是设置卡；新卡发出后撤回上一张（`app/bot_menu.py` 的 `Panels`）。

## 话题链接

管家卡、回带卡上的 T 号是链接（`app/topic_links.py` 的 `linkify`），打开话题并停在一张卡上：回带卡上它自己的话题停在回带的那一轮的卡，别的停在最新的任务卡。

- 有话题串的用 applink `client/thread/open?…&thread_position=…`，读不到位置时只打开话题串。
- 位置由 `feishu/message_read.py` 的 `fetch_message_detail` 读一次，记在 `<state>/topic-links.json`，每个话题留 `KEPT`（8）条。
- 读不到保持纯文字；读消息在所有锁外（规则 1、2）；只改显示的文字，保存的回复（规则 5）不变。

### 派发卡（开关默认关）

`COVEY_DISPATCH_CARD=1` 时（见[配置 · 派发卡](configuration.md#派发卡)），管家派出话题的那一刻在聊天区多一张**总览卡**：聊天区里的一条新消息，顶上一块“你问 · 12:31 ›”，不开串；话题照旧在原处、有串、有自己的任务卡。（它不能做成对主人那条提问的回复：飞书对已经有话题串的消息，回复一律进串。）关着时没有这个调用、没有后台线程。

- **内容**（`cards/dispatch_card.py` 的 `dispatch_card`）：卡头 = 主人请求的前 30 字 + 状态标签（在做、等你、做完、失败、已停）；一行灰字“T12 · 12:41 起 · 用时 2分05秒”；折叠面板（标题行 T 号 + 话题标题 + 工作区；在做和等你时展开）里是当前步骤或阶段；输入/输出灰框；补充、停止、打开话题三个按钮，能用的才画（补充和停止只在“在做”且已有任务时，等你时回答在话题里的问题卡上）。卡里**没有输入框**（会刷新的卡不放输入框，见 [cards.md](cards.md) 卡片的客户端限制）。
- **创建**（`app/dispatch_cards.py` 的 `created`）：话题接了活（`topic_start`、`topic_send`、`topic_fork`）之后发出；记 `route_message`，主人引用这张卡说话进那个话题。同一个请求只发一张。补充送进正在跑的任务、话题里已有一张正跟着这一轮的派发卡时，不再发新卡（日志 `dispatch card kept`）。发不出去只记日志，话题已经开了。
- **记录**：`<state>/main-thread/dispatch/<token>.json`（`core/dispatch_records.py`），`state` 只有 `live → settled`（`DISPATCH_TRANSITIONS`），`settled` 之后后台线程不再看它；`rounds`、`upgrade_hint`、`project_id` 留给转成专项用。
- **刷新**：`DispatchCards` 的后台线程每秒看一遍没结束的记录，只读话题的任务记录（没有飞书调用，不拿锁），摘要变了且离上次写至少 2.5 秒（`MIN_GAP`）才写一次整卡。写之前从 `reserve` 取 sequence 存进记录，失败按 2、4、8 … 最多 60 秒退避，下次取新的 sequence，从不重发同一个。任务进程不碰这张卡，Bridge 里只有这个后台线程写它。
- **结论沉底**：回带发出后，任务进程只留一张小便条 `<token>.relay.json`（不碰卡）；后台线程读到便条，把卡整卡换成一行“✅ 做完 · 结论在下方 ›”（链到结论消息，面板收起、按钮摘掉），写成功后记录转 `settled`。失败或停止同样写一行原因。回带 `uncertain` 或 `unsent` 时不留便条，卡原样留着让主人看到出了问题。任务结束后 10 分钟（`TERMINAL_GRACE`）还没有便条，卡就按最后的样子转 `settled`；3 天还没结束的也一样。
- **按钮**：`bridge_dispatch_steer`、`bridge_dispatch_stop` 在 `ACTIONS` 里（不是 `bot_only`）。点击那一刻 `card_routing` 先按话题现在的状态换成**当前在跑（或排队）的最新一轮**（`dispatch_cards.click_job`），这一轮结束后又开了新一轮也不会落空；话题没有在做的任务时回一句“这个话题现在没有在做的任务”。任务进程活着时点击转给它，和任务卡上的补充、停止一样。
- **点了停止马上显示**：停止点击到达 Bridge 时在本进程记下（`dispatch_cards.stop_clicked`），下一次刷新就画“正在停止…”，不等任务进程写记录；只在内存里，最多显示 `STOP_SHOWN`（600 秒）。

### 专项

专项卡、专项记忆与资料库、专项的问题分流、专项停住后问主人、整理专项、汇报风格的默认值，见 [projects.md](projects.md)。

### 我本来想找你的事（开关默认关，只记不打扰）

`COVEY_PATROL=shadow` 时，管家会替主人盯着手上的事，但只记下来、不打扰：不发消息、不叫醒负责人、不动专项。`on` 留给以后的版本，现在当作 `shadow`；其他值和不设都是关，关着时不起后台线程、不调模型、汇总不变。

- **三层**：
  - 第一层是规则（`app/patrol_rules.py`），每分钟一次、不调模型，从看板、手上的事、汇总已经在读的记录里找候选：问题等了很久没回（默认 120 分钟）；做完的结果没人看（360 分钟）；同一个话题连着失败 2 次，或任务 180 分钟没有动静；最近三天的话题里看起来是同一件事的，或该归进某个在做的专项的（开了整理专项时改读它的提议）；专项有回执没人读或被护栏停住后 720 分钟没有进展，或专项的话题太多太散。同一件事状态没变不重复记。
  - 第二层是小模型（`app/patrol_judge.py`），每 30 分钟一次，有新候选才调：给它候选、精简的上下文和主人的原则，它对每条给“丢掉 / 记下 / 该处理”、一句理由和本来要做的动作；每天最多 48 次，同一条试 4 轮仍不成就放弃。要脱敏的话题标题不外发。
  - 第三层是管家核查执行，还没有。
- **怎么看**：记录在 `<state>/patrol/`（`candidates/<日期>.jsonl`、`decisions/<日期>.jsonl`、`state.json`），保留 14 天。早晚汇总和「看板」在有“该处理”的决定时多一块“我本来想找你的事”：最多 5 条，只列上次汇总之后的。这一块自己不会让汇总发出来。
- **怎么教它**：在私聊里说“这类该找我”或“这类不用”，管家用 `principle_save` 记成原则，小模型下一轮读得到。
- **怎么关**：不设 `COVEY_PATROL` 重新注册（见[改开关三步](configuration.md#改开关三步)）；已写的记录 14 天后被维护清掉。
- **可调数值**：`COVEY_PATROL_ASK_MINUTES`、`COVEY_PATROL_UNSEEN_MINUTES`、`COVEY_PATROL_STALL_MINUTES`、`COVEY_PATROL_FAILS`、`COVEY_PATROL_PROJECT_MINUTES`、`COVEY_PATROL_TICK_SECONDS`、`COVEY_PATROL_JUDGE_MINUTES`、`COVEY_PATROL_DAILY_CALLS`、`COVEY_PATROL_JUDGE_MS`，默认值和范围见[配置](configuration.md)的一览；验收时把时间都调到几分钟、小模型间隔调到 1 分钟，几分钟内就能看到结果。

### 网页工作台

见 [web.md](web.md)。

# 管家

管家是私聊里一个常驻的会话（设置卡的私聊模式选“管家”时），默认跑在 Claude Code 上，也可以在设置卡「管家」页换成 Codex 或 Antigravity。主人在私聊**顶层**发的消息都交给它：能直接答的自己答，要干活的开话题或转给已有话题，主人说“T12 选 A”时替主人答选择题。用户视角见[使用指南 · 管家](usage.md#管家)，部署开关见[配置](configuration.md)。

管家交给负责人跟进的长期的事见[专项](projects.md)，网页上的工作台见[网页工作台](web.md)。这篇讲管家是什么、由哪几块组成、一轮怎么走；放置表的每一格、工具通道的协议、各功能的记录和刷新方式等改代码时才查的细节在[管家的实现细节](butler-internals.md)，标题和这里一一对应。

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

管家的 Harness 是设置里的 `harness`（空就是 Claude Code，`core/butler.py` 的 `HARNESS`），只能选能接 Bridge 工具的（`core/harness.py` 的 `bridge_tools`：Claude Code、本机上的 Codex 和 Antigravity；见[换 Harness](#换-harness)）。选的 Harness 没启用或接不了工具时，私聊消息照旧开话题，Bridge 启动日志写一行 `main thread is on but its Harness <名字> is not enabled or cannot take the Bridge tools`。新部署私聊里的[欢迎卡](usage.md#第一次使用)（`app/welcome.py` 的 `butler_state`）也当面说：没有管家时写为什么（管家用的 Harness 没启用或接不了工具，默认是 Claude Code；`COVEY_BUTLER=0`；私聊模式是纯话题；或它刚才没起来）和怎么补（装好并登录 Claude Code 再运行一次 `./install`，只有 Codex 或 Antigravity 时在设置卡「管家」页把 Harness 换成它；纯话题时在同一页改回交给管家），并说明每条消息会开一个话题、先出启动卡；有管家时说明你的话下面出现表情就是收到了，有东西要给你看时才发卡（一轮跑过 90 秒才发任务卡，见[提问不开串](#提问不开串)）。

## 和话题的不同

不同都在 `core/butler.py` 的 `Traits` 表里。调用处只查表，不比较 topic id，也不比较 `Traits.name`（`tests/test_butler_contract.py` 的 `NoNameComparisonTest` 检查）。

- **判断**：`is_butler_topic(topic_id, chat_id)`，和开关无关。
- **查表**：`traits(topic_id, chat_id)`；任务、记录、topic.json 用 `traits_of`；只有上下文时用 `core/topics.py` 的 `topic_traits`。
- **行**：`TOPIC`、`BUTLER`（开关开着）、`BUTLER_OFF`（开关已关：结构上的不同照旧，会话上的取消）。

`BUTLER` 一行概括起来：管家不占 T 号，没有换配置和回退，卡片点击不能指向它，不进看板和手上的事，不回带；每轮带规则、快照和工具，按阈值压缩；回答可以写在任务卡上，任务卡是分档卡。每个字段的取值和读者见[实现细节 · Traits 表](butler-internals.md#traits-表)。

## 一轮怎么走

### 路由

`app/card_routing.py` 在启用的扩展的 `handle_event` 之后调 `Butler.handle_event`，主人身份来自 `Runtime.access`（规则 11）。消息的 `root_id`、`thread_id`、`parent_id` 按别名查话题：查到别的话题交给它；查到管家留在管家；都没填是管家的新一轮；填了但谁也不认识就照旧开话题。

管家话题的 `topic.json` 带 `main_thread: true`（字段名沿用旧名，不改），没有根消息和 `topic_number`。每条消息都同步设置：模型和预算下一轮生效；换工作区要等没有任务在跑，并开新会话。

- **补充**：有任务没结束时，新消息用 `deliver_steer` 并进最新一轮，不发回执卡；可能已送达的不重发（规则 6）。
- **收尾时发来的消息**：确定这一轮没读到的补充，自动排成管家的新一轮；不确定的说一句。
- **等主人的话**：没有自己文字的消息（卡片、合并转发、图片、文件、只有链接）等 30 秒（`HOLD_SECONDS`），这期间主人的文字并进请求并立即开工。

三种情况的做法见[实现细节 · 路由的细节](butler-internals.md#路由的细节)。

### 直答与转交

规则在 `app/prompts.py` 的 `butler_rules`（`AREA_OPENING`、`AREA_RULE`），放在每轮用户消息开头：

- 只读、一两分钟能做完的，管家自己做、直接答；只问已有结果的用 `topic_read` 读了自己答。
- 改代码或文件、要跑几分钟以上、几件事并行、主人说开话题的，交给话题：新的事 `topic_start`，同一件事 `topic_send`；两个以上话题都对得上时用 AskUserQuestion 让主人选。
- 交出去后只说一句放到了哪，不报进度和结论（结论由 Bridge 回带，进度看 Pin）。
- 写给主人的话当他没看过别处：提到话题写它在做的事、T 号放括号里，话题里的数字和结论直接写进回复。
- 怎么做由管家定（先查什么、交给哪个话题、失败了换办法）；做什么、不可逆的、对外可见的、花钱的由主人定，问的时候先说倾向。
- 一轮结束后管家不会自己醒来：没有在跑的话题会把结果带回来时不说“稍后”“等下”，没答完的派进话题。
- **提醒一次**：管家这一轮自己调满 6 次 Bridge 以外的工具时，Claude Code 的 `PostToolUse` hook 提醒它一次把剩下的交给话题、说一句放到了哪里就结束（`app/prompts.py` 的 `TOOL_NUDGE_TEXT`，`backends/claude/hooks.py`）；主人这句话说了“你直接做”“别开话题”“就在这儿做”的不提醒。管家派出的子 Agent 调不了 Bridge 工具（[Harness 说明 · Claude Code](harnesses.md#claude-code)）。
- “写给人看的话（含工具说明）用主人的语言”只在每轮的桥接规则或续接提醒里说一次（`app/prompts.py` 的 `LANGUAGE_RULE`），管家规则不再重复。
- 主人问 Covey 本身怎么用（看板在哪、怎么开记忆），先读运行中这份代码自己的 `docs/quickstart.md`（[五分钟上手](quickstart.md)，规则里写的是它的绝对路径，`USAGE_RULE`）再答。

**开工三档**（`starts_at_once`，设置 `start`）：只读的、主人在这条消息里亲口要的、主人记住过的“工作区 × 只读/改文件”直接开工；其余在话题里出预填好的启动卡。

判为只读（`readonly`）又直接开工的，第一轮的任务说明末尾写“这一轮只读”（`app/tool_limits.py` 的 `LOOK_NOTE`；请求记录的计划里记 `read_only`，任务记录也记），并且照 Harness 能做到的限住（`TOOL_LIMIT` 的 `look`）：Claude Code 没有改文件的工具（Edit、Write、NotebookEdit，子 Agent 也没有；命令行还在，给只读的命令用），Codex 在只读沙箱里跑（命令能跑，写不了文件、上不了网），别的 Harness 只有那句话。只管这一轮：主人之后在话题里说的话、点重试或检查并继续，照常跑；等启动卡、主人点了开始的也照常。

### 管家规则

`COVEY_BUTLER_RULES=1` 时，[直答与转交](#直答与转交)的前两条换成 `app/prompts.py` 的 `BUTLER_RULES`（两条），让管家只快答和分派，这样它接得住主人的连问；`topic_start` 的工具说明里同样的话也换了；其余规则不动。

- **快答**：不用工具，或只用一两次只读的查询（看一眼文件或日志、查一项配置、`topic_read`、`topics_list`），几秒内能答完，直接答、不开话题。
- **派出去**：要查一圈、要跑命令、要改代码或文件、要联网或多步操作、预计超过半分钟的；拿不准也派。新的事 `topic_start`，和已有话题是同一件事 `topic_send`。
- **派出去后说什么**：一句话，放到哪个 T 号，如“放到 T42「标题」里做”；快照里有预计才报时间，不写进度和结论（`AREA_RULE`）。
- **例外**：主人明确说“你直接做”“别开话题”“就在这儿做”时，这一轮自己做完再答。
- **兜底**：自己干了 90 秒的一轮照旧出卡（`CARD_AFTER`），不另设变量。
- **打开和关掉**：见[配置 · 管家规则](configuration.md#管家规则)。关着时规则和提示词和原来逐字节相同。

规则每轮重新拼，重新注册后管家的下一轮就用新规则；续接会话里新旧规则怎么共存见[实现细节 · 管家规则什么时候生效](butler-internals.md#管家规则什么时候生效)。

### 同时做几件事（开关默认关）

`COVEY_BUTLER_TRACKS=1` 打开（[配置](configuration.md#环境变量与选项一览)）。管家这一轮在做事时，主人的下一句不被挡在后面：管家变成一个**话题家族**，现在的管家话题之外再有固定 4 路，每路是管家会话的一个分叉，各自跑一轮普通的管家任务。`COVEY_BUTLER_TRACKS_MAX` 是同时在跑的件数上限（默认 3，取值 2 到 4），到了上限的新事排在最空的那一路后面。

- 新的一句先按规则分流（引用了哪件事就给哪件），判不出时问小模型：并进某件在做的事、另起一件，或停止。
- 说“停”“别做了”停掉对应的那件，不建任务、不进模型。
- 管家的下一轮开头带上别的几路这段时间答了什么，所以管家记得住。
- 开了又关：几路的话题留在盘上，这时它们是普通话题，回答、引用老卡照样能接着用。

分流、表情、停止和账本的细节见[实现细节 · 同时做几件事的细节](butler-internals.md#同时做几件事的细节)，底座和运行时见[架构 · 管家同时做几件事](architecture.md#管家同时做几件事默认关)。

### 提问不开串

话题挂在提问下面时，管家这一轮默认不发卡（`(AREA, BUTLER)` 格子的 `Row.quiet`）：

- 主人那条消息加表情 OnIt；跑过 `CARD_AFTER`（90 秒）还没结束才发任务卡，进主人那句话的话题串。结束时表情换成 DONE（自己答的）、Get（交给话题）或 CrossMark（失败、被拒）。
- 答案、结论、问题卡副本和通知都是聊天区新消息，顶上“你问”块的第一行写谁对你哪句话做了什么（说话人，如“**管家** 答复你 12:31 的问题 ›”“**T12 标题** 做完了你 09:12 交代的事”）。
- 引用这些消息说话，交给发出它的话题、负责人或管家；引用手上的事或早晚汇总卡也交给管家（`Butler.app_for`）。
- 只做了交接的一轮不发答案，Get 就是回执；转给已有话题的，再在主人那条消息下回一张“→ T12「标题」 ›”的小卡。
- 要主人定的事写进检查点的问题，进“等你”，不只在回复末尾问一句。

表情、说话人的句式、没卡时的失败通知、重试和续接见[实现细节 · 提问不开串的细节](butler-internals.md#提问不开串的细节)。

### 一次请求一张卡

`app/butler_reply.py`。回答先照旧经 `verified_reply` 保存、授权（规则 5）。

- **开了或转了话题**（`main-thread/opened/` 里这一轮成功的记录，`core/butler_opened.py` 的 `kind_of`）：写在任务卡上，每个话题一行（“↪ 开了 **T14**「标题」”“↪ 转给 **T13**「标题」：note”），模型的话折进“详情”。
- **自己答的**：按放置表 `answer` 作为聊天区新消息发（`app/answer_delivery.py`）。
- **退回单独发**：没完成、要以文件发、超过卡片字数或写卡失败时，单独发回答卡（重投只发保存的正文）。
- **没人跟进的推迟**（#25）：做完的一轮，回答里有推迟的话（“稍后”“等下”“我去查”，`core/turn_words.py` 的 `defers`，提了 T 号的不算）、这一轮又没有成功开、分叉或转交任何话题时，显示的答案下面加一行 `UNFOLLOWED`（`unfollowed`）；只改显示，保存的回复和重投不变。巡查另记一条 `deferred`（[我本来想找你的事](#我本来想找你的事开关默认关只记不打扰)）。

状态行的写法、停止和被拒、分档卡见[实现细节 · 一次请求一张卡的细节](butler-internals.md#一次请求一张卡的细节)。

## 会话

`app/butler.py`，只在开关开着、任务属于管家话题时起作用。

- **回复位置和说法**：对管家消息的回复进那条消息的话题串，开话题就用这个串；Bridge 自己写的“本话题”换成“私聊里”。
- **快照**：紧接规则，`snapshot_groups` 从 `core/board_read.py` 读（不调飞书，不持记录锁）。四组：等你（和汇总、看板、手上的事同一批）、有结果没看（看板的口径，见[看板与 Pin](#看板与-pin)）、在做、管家开的话题（快照和 `topics_list` 的说明里分组名还是旧写法，见[名词表 · 状态词](glossary.md#状态词)）；开了专项还有一栏“专项”。下一轮只列变了的行。
- **MCP 配置**：`butler_turn` 把 `mcp_config(...)` 放进 `ctx.options`，Claude 后端据此加 `--mcp-config`，不改 `runs/` 协议。
- **压缩**：一轮送达后上下文到了 `compact_tokens`（默认 20 万，0 不压），`compact_butler` 写压缩请求；空闲时由 `app/idle_compaction.py` 在后台压，主人不用等。
- **已下线的前台话题**：旧设置读到也按关；以前开的由 `core/migrate.py` 的 `desk-retire` 冻结。

每一项的做法（快照怎么存、MCP 参数、空闲压缩的标记和失败处理、前台话题的旧卡）见[实现细节 · 会话的细节](butler-internals.md#会话的细节)。

## 卡放在哪

### 放置表

`core/placement.py`：布局 × 角色 → 格子 `Row`。调用处只读格子；只有这一轮知道的事（开没开话题、多快、是不是主人自己停的）留在调用处。

- **布局**：管家开着是 `area`（话题挂在提问下面），否则 `topics`。
- **角色**：`butler`、`topic`；开了专项再有 `lead`（负责人）和 `project_member`（专项的话题）。
- `area` × `butler`：答案是聊天区新消息，开了或转了话题时写在任务卡上，这一轮不开串。
- `area` × `topic`：答案进话题串，结论和问题卡副本回到聊天区。
- `topics` 布局：都在话题串里，看板 Pin 话题的状态卡。

完整的格子表、每个字段的读者和负责人的唤醒轮见[实现细节 · 放置表](butler-internals.md#放置表)。

### 消息落在哪

- **管家的卡**在主人那句话的话题串里，跑过 90 秒才发。
- **答案、结论、问题卡副本**都是聊天区新消息，不引用任何卡：主人那句话下面已有话题串，回复它会掉进串里。
- **“你问”块**（`cards/answer_card.py` 的 `ask_block`）：灰底两行“你问 · 时间 ›”和原话，整块可点，跳到这一轮在话题串或聊天区的位置；表单回答开的一轮是“你答”块。链接的退路见[实现细节 · “你问”与“你答”块](butler-internals.md#你问与你答块)。

## 工具通道

```
管家会话 ── MCP（stdio）── bin/covey_butler_mcp.py
                                │ 写 <state>/main-thread/requests/<id>.json，等 <id>.answer.json
Bridge 进程 ── app/butler_tools.py：pending → claim → 核对 job_key → 处理函数 → answer
```

- MCP 服务（`core/butler_mcp.py`）只用标准库，等回答最多 120 秒。
- Bridge 里的后台线程 `main-tools` 一次执行一个请求；启动时认领了没回答的回 `UNCERTAIN`，不重跑（规则 1、6）。只接受管家正在跑的任务发来的调用。
- 处理函数是 `handler(env, request) -> dict`，各模块用 `TOOLS` 字典登记，`app/unified.py` 合起来。

请求和回答的格式、执行循环的规则见[实现细节 · 工具通道的细节](butler-internals.md#工具通道的细节)。

### 工具清单

照 `core/butler_tool_specs.py`（* 必填）：

| 工具 | 参数 | 做什么 |
| --- | --- | --- |
| `topics_list` | group、query、limit | 按等你 → 有结果没看 → 在做 → 最近列话题 |
| `topics_search` | clues*、limit | 按线索找是哪个话题哪一轮做的 |
| `topic_read` | topic*、job、turns | 读话题最近几轮：问题、结论、检查点、没答的问题 |
| `workspaces_list` | 无 | 列工作区：名字、路径、用途、最近的话题 |
| `topic_start` | title*、prompt*、workspace*、readonly*、user_requested*、why*、attachments、criteria、harness、model、effort | 为新的事开话题 |
| `topic_send` | topic*、text*、note、title、criteria、attachments | 把主人的话追加给已有话题 |
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

开了专项时 `topic_start`、`topic_send` 各多一个可选的 `project`（开专项、把话题转成专项）。查询类工具只读记录、不持记录锁；`question_answer` 和卡上点“提交回答”走同一条路。各工具的规则见[实现细节 · 工具的细节](butler-internals.md#工具的细节)。

### 按线索找话题

`topics_search`（`app/butler_search.py`）一次 1 到 `MAX_CLUES`（5）条线索（飞书链接、token、路径或关键词），搜主人私聊每个话题每一轮的主人原话、结果、状态行和检查点，按匹配程度排序、按话题分组列出。不读会话原文、不搜群聊、不加锁。匹配和排序的规则见[实现细节 · 按线索找话题的细节](butler-internals.md#按线索找话题的细节)。

## 开话题、转交、分叉

`app/butler_topics.py`，只在主人私聊里动作，飞书调用都在锁外（规则 1、2）。

- **`topic_start`**：工作区只认已登记的名字；Harness、模型、预算逐项取值（`app/butler_model_choice.py`：调用给的 > `topic_defaults` > 这个私聊上次开始任务的配置 > Bridge 默认）。直接开工时第一轮直接提交，否则话题里出预填好的启动卡。
- **`topic_send`**：话题有没结束的任务时作为补充交给它，已经停了就作为下一条消息（有没答的问题就作为回答）。话题串里出一张“管家转来你 HH:MM 的消息”的灰底卡。
- **完成标准**（`criteria`，`core/criteria.py`，#22）：`topic_start` 可带 1 到 4 条，存进话题的 topic.json；主人要改时管家用 `topic_send` 的 `criteria` 换掉（空数组去掉）。问答、查询、调研不带。有标准的话题每一轮的消息在检查点文件后面多一段“完成标准”（`criteria.note`），模型在检查点 `criteria` 里逐条写满足 / 没满足 / 没查；答案卡和回带卡只列没标满足的（没写的算没查）。专项负责人的 `project_topic_start` 也能带。
- **`topic_fork`**：从源话题最近一次结束的任务分叉（`app/forks.py` 的 `create_fork`），复制原生会话。
- **话题卡**（`cards/topic_card.py`）：“T12 · 标题”加一行灰字配置，没有按钮。
- **幂等**：每次调用在 `<state>/main-thread/opened/<请求>.json` 记做到哪一步（`core/butler_opened.py`），同一个请求再来返回存下的回答。

取值的退路、`origin` 字段、补充被拒的处理见[实现细节 · 开话题、转交、分叉的细节](butler-internals.md#开话题转交分叉的细节)。

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

管家开的或转过去的那一轮结束时，Bridge 在聊天区发一条新消息，顶上是“你问”块。只在管家开着时；不调模型。回带的是管家开的、转过去的轮次和它们的自动重试，以及话题里回答问题后接着跑的、主人在串里开的一轮；管家没碰过的话题不回带。专项的话题只回带负责人在你在场的一轮里交出去的那一轮（[主人在等的结果](projects.md#主人在等的结果)）。

| 结果 | 回带 |
| --- | --- |
| 完成，回答已送达 | 结论卡（`cards/relay_card.py`）：回答全文、产物、输入/输出灰框，末尾 T 号链接 |
| 等你回答 | 问题卡顶层副本；放不了时发带“要你定：”的回带卡 |
| 失败或停止，没排上自动重试 | “**T10 停了**：状态行”；主人自己点的停止不发 |
| 没送达、排上了自动重试 | 不回带 |

- **只发一次**（`app/relay_send.py` 的 `send_once`）：锁内写意图、锁外发，没确认记 `uncertain`，从不重发（规则 1）。
- 回带消息是话题的消息，看过它就算看过话题。
- **连着做完的合成一条**（`send_merged`，`COVEY_RELAY_MERGE_SECONDS`，默认 20 秒，0 关）：“完成”的回带发出时聊天里还有别的话题的任务在跑或排队、或者已有回带在等，就先记 `pending`（`main-thread/relays/waiting.json` 按聊天列着）等这么多秒，再把这期间等着的一起发：只有自己的照常是结论卡，两条以上发一张「N 个话题做完了」卡（`cards/relay_card.py` 的 `merged_card`：每个话题一行 T 号、标题和回答第一句，点 T 号打开）。哪条记录都只从 `pending` 被取走一次（锁内改成 `submitting`），仍然只发一次；合成的那条算顶层的消息，引用它交给管家。没有别的任务在做时照旧马上发。“等你”和“停住了”从不等、不合并。

记录文件、产物更新和维护见[实现细节 · 结论回到顶层的细节](butler-internals.md#结论回到顶层的细节)。

### 问题卡在顶层也放一份

话题的问题在顶层也放一份同样的表单（`app/question_display.py` 的 `mirror`），聊天区新消息、带“你问”，表单上面是这一轮的输出。在哪张上答都一样，答了两张一起收起。只有一道有选项的单选题时，两张卡上都是每个选项一个按钮，点了就提交。

专项的话题也放：不带“你问”块，卡上的话题号说明是谁在问。交给负责人分流的问题这一轮结束时不放（还不归你）；负责人转给你时补放这一份（见[专项的问题分流](projects.md#专项的问题分流)）。副本的记录和收起见[实现细节 · 顶层问题卡副本的记录](butler-internals.md#顶层问题卡副本的记录)。

## 看板与 Pin

管家自己不进看板，它开的话题照常进。看板、手上的事、早晚汇总、管家的快照和 `topics_list` 都从 `core/board_read.py` 的 `read` 读，“等你”和“有结果未看”五处一个口径（以看板为准，经 `app/pins.py` 的 `board_results`）：话题最新一个跑过的任务完成、失败、中断或停止且主人在它结束后没读过，就是有结果未看，不论多久。使用者看到的样子和各组的规则见[使用指南 · 看板](usage.md#看板)和[手上的事](usage.md#手上的事)，开关见[配置 · 手上的事与话题看板](configuration.md#手上的事与话题看板)。

### 个人空间的管家与看板

`spaces.personal_groups` 里的群（和 `owner_accounts` 的私聊）各有自己的管家：话题 id 按 chat 算（`butler_topic_id(chat)`），记录不混；`Access.agent_chat` 判断哪个 chat 有管家，`ButlerEnv.chat()` 在工具调用里取这个管家的 chat。个人群没有已读回执，主人在群里发的消息算读过它的看板（`app/group_boards.py` 的 `owner_active`）。个人群各有一份手上的事、看板和早晚汇总，记录名带 chat_id（`pinned-topics.<chat_id>.json`、`pinned-overview.<chat_id>.json`、`digest.<chat_id>.json`）。设置见[配置的空间一节](configuration.md#空间spaces)。

## 主人的原则

Bridge 记一份主人的长期原则，所有 Harness 的话题和管家都带上。

- **存储**：`core/principles.py`，`<state>/principles.json`，分通用和各工作区两组；一条最多 200 字，每组最多 30 条。
- **工具**：管家用 `principle_save`、`principle_delete`、`principles_list` 记、删、列。
- **注入**（`app/runs.py` 的 `Runs.principles`，块由 `app/prompts.py` 的 `principles_block` 写）：工作区那组总带；通用那组只在主人自己的一轮带（`Runs.owner_turn`：管家总是，其余经 `Access.owner_turn`，规则 11）。新原则下一轮就生效，代答话题不带。
- **原则已授权的不再问**：原则块开头和记忆的规则都写着：要问主人之前先看原则，原则里已经同意的事照着做。部署也可以这样长期授权：记下“空闲时直接部署”就等于主人提前开了口。

**以后这类按推荐走**（`app/standing_answers.py`，`COVEY_STANDING_ANSWERS=1`，`./install` 新装的服务打开）：问题卡上每道题都恰好有一个推荐项时，表单下多一个按钮「以后这类按推荐走」；点了以后小模型把这道题概括成这个话题工作区的一条原则，主人在另一张卡上「记下」才记，同时按推荐项回答还在等的这道题。

文件格式、注入的长度上限和按推荐走的卡见[实现细节 · 原则的存储与注入](butler-internals.md#原则的存储与注入)。

## 知识库

主人说“记一下”的东西集中存放，所有 Harness 的新会话都带上，管家能搜索和修改。`COVEY_KNOWLEDGE=1` 才开（[配置](configuration.md#知识条目与历史搜索)）；关着时不建目录、规则和提示词一字不变、工具清单里没有这些工具、汇总里没有这一节。

- **存储**（`core/knowledge.py`，只用标准库）：`<state>/knowledge/` 下通用一份、每个工作区一份，只追加的事件（新增、改写、作废），读时重放。条目分 decision、practice、pitfall、fact、question 五类，正文最多 200 字。
- **管家**：`knowledge_save`、`knowledge_update`、`knowledge_drop`、`knowledge_search`（`app/butler_knowledge.py`）；以后每次都要遵守的做法仍用 `principle_save`。查以前的事按 `knowledge_search` → `topics_search` → `history_search`。
- **话题里说“记一下”**：主人这一轮明确说了，模型才把要记的写进检查点的 `remember`；任务进程退出之后由 Bridge 里的 `app/knowledge_collect.py` 核对后写入。
- **注入**（`app/knowledge_turn.py`）：工作区那组谁的话题都带，通用那组只在主人自己的一轮带，总长 1500 字；代答话题不带。
- **查历史**：设了 `COVEY_HISTORY_CMD`（绝对路径）时，主人自己的话题多一行怎么用这个命令，管家多 `history_search`。
- **汇总**：汇总卡和菜单「看板」多一节“新记的知识”。
- **回退**：去掉两个变量重新注册；已经写下的 `knowledge/` 留着。

存储格式、收集的条件和注入的顺序见[实现细节 · 知识库的细节](butler-internals.md#知识库的细节)。

## 记忆（开关默认关）

`COVEY_MEMORY=1` 才开（[配置](configuration.md#记忆)）；关着时规则、续接提醒、答案卡和任务记录逐字不变，不读不写话题的 `memory.json`。它把“主人的原则”“知识库”“专项记忆”和话题自己的记忆合成一种东西，对主人只说“记忆”。

- **一个读口**（`core/memory.py`；每轮的接线 `app/memory_turn.py`）：原则、知识条目、专项记忆（`core/project_memory.py`）和话题记忆（`core/topic_memory.py`）拼成一块，每条一行“`- M12 · 管这个专项 · 你说的：…`”：编号、管哪儿（全局、这个工作区、这个专项、这个话题）、谁说的。谁读什么和原来一样：全局的只在主人自己的一轮，代答的话题什么都不带。
- **标出处**：主人自己的话是“你说的”，负责人总结的是“它总结的”；块后面的规则写明谁说了算。
- **纠正落地**：主人在话题里说“以后、别再、从现在起”这类话时，模型可以在检查点里写要记的和要作废的；Bridge（`app/memory_collect.py`）核对引用的确是主人原话后记下，答案卡底部列出，每条带“不对”按钮可撤销。
- **设置卡「记忆」**（`app/memory_panel.py`）：按全局、工作区、专项、话题列出每一条，能删；最上面是选中工作区里新开话题、主人自己那一轮会带上的那一块（和每轮用同一段代码算），每个范围用了多少字、快满了或放不下几条都写在这里，不再只有模型看得到。
- **常驻**：主人可以在「记忆」上把自己立的原则标成常驻（`core/principles.py` 的 `pin`，每组最多 8 条）：范围放不下时它也一直带上，挤掉的是别的旧条目。
- **每天的候选**（`app/memory_candidates.py`，要早晚汇总）：白天仍然只记主人明说的。每天晚间汇总前从当天结束的话题里找主人在不同话题里重复说的纠正，最多 3 条，放进汇总，每条带来源话题和「记下」「不用」；不点不生效，开了脱敏的话题不读。细节见[实现细节 · 记忆候选](butler-internals.md#记忆候选)。

字数上限、出处规则和纠正的核对见[实现细节 · 记忆的细节](butler-internals.md#记忆的细节)。

## 设置

`<state>/main-thread.json`，字段见 `Settings`：`mode`/`enabled`；管家的 `model`、`effort`、`redaction`、`workspace`、`compact_tokens`；新话题的 `topic_defaults`（`harness`、`model`、`effort`、`run_mode`、`redaction`、`workspace`）；子 Agent 用什么模型 `subagents`；直接开工 `start`、记住的 `trusted`、工作区用途 `workspace_notes`。缺的或坏的字段按默认值读。只经 `update` 写，只有设置卡（和网页的设置页，走同一处）能写。

- **管家的脱敏**：从下一轮起走敏感词中继；Bridge 每次交消息前写它的 `topic.json`，中继每轮读（`backends/claude/redaction.py`）。

### 换 Harness

管家的 Harness 和开新话题一样在设置卡上选（「管家」页第一行），供应商、模型、思考预算跟着它走；换了 Harness，模型换成它列出的第一个，思考预算保留原来那一档（新模型没有这一档时换成它的次高档，没有次高档时换成不带编排的最高一档）。

- **什么时候生效**：管家手上没有在跑的一轮时（`app/butler.py` 的 `write_topic`，和换工作区一样）：`topic.json` 的 `harness_id` 换成新的，`binding_revision` 加一，新 Harness 的会话指针清掉（以前在它上面的会话落后于之后的对话）。有一轮在跑时新消息照旧进旧 Harness 的这一轮。新会话的第一轮带最近的对话（和换工作区一样），不做交接摘要。多路并行的各路也跟着换，各自等空闲。
- **工具**：MCP 服务还是 `bin/covey_butler_mcp.py`，`mcp_config` 由各后端自己接：Claude Code 是 `--mcp-config`，Codex 是线程配置的 `mcp_servers.bridge.*`，Antigravity 是这一轮私有主目录里的 `mcp_config.json`（[harnesses.md](harnesses.md#antigravity)）。规则和快照在每轮消息的正文里，和 Harness 无关。工具名 Claude Code、Codex 里是 `mcp__bridge__<工具名>`，Antigravity 里是 `mcp_bridge_<工具名>`，规则里两种都写。
- **压缩和快照**：只有 Claude Code 由 Bridge 压缩（capability `compaction`）。Codex、Antigravity 自己压缩，Bridge 看不到，所以每一轮都给完整快照，不只给变了的行。
- **脱敏**：只有 Claude Code 能脱敏。开着脱敏时不能换到别的 Harness；换过去以后脱敏一行不显示。
- **不跟着管家换的**：专项负责人仍是 Claude Code（`app/project_loop.py` 的 `HARNESS`）；多路并行的判断模型和待命进程仍用 Claude Code；子 Agent 一项只对 Claude Code 生效。
- **回退旧版本**：旧版本不读 `harness`，管家回到 Claude Code，但模型还是新 Harness 的模型 id；回退前先在设置卡上把 Harness 换回 Claude Code。

### 设置卡

管家的设置是设置卡（`app/settings_hub.py`，菜单键 `bridge_settings`）的前三个标签，标签行见 `cards/settings_hub.py` 的 `LABELS`：模型 · 新话题 · 管家 · 工作区 · 脱敏 · 群聊（· 记忆）（· 设备）· 能做什么 · 诊断。菜单打开时停在模型。画卡和点击在 `app/butler_panel.py`、`cards/butler_panel.py`。模型一页把要选模型的几处放在一起（新话题、管家、独立复核、子 Agent），新话题、管家两页管剩下的设置；代答的模型仍在菜单里单独的代答卡上，私聊模式收在管家页的“更多”里。

| 页 | 内容 |
| --- | --- |
| 模型 | 一段一处，每段 Harness、供应商、模型、思考预算：**新话题**（所有新话题预选的模型，启动卡 `app/preselection.py` 和管家开的话题都照它）；**管家**（管家自己的，能跑管家的 Harness 才列；管家没开或部署不允许时没有这段；开着脱敏时 Harness 只列能脱敏的，段下写一句先关脱敏才能换，`REDACT_FIRST`）；**独立复核**（`COVEY_REVIEW=1` 才有，`review_setting_list`：Harness 第一项“自动挑（优先换一家厂商）”，选了 Harness 才有下面三行，存在 `main-thread.json` 的 `review`）；“更多”里子 Agent：轻量（Sonnet，搜索用 Haiku）/ 跟主模型 |
| 新话题 | 所有新话题其余的预选：运行方式、脱敏、工作区；管家开着时“直接开工”一行（`START_CHOICES`：都直接开工 = 三档全开，只读的直接开工 = `readonly` + `trusted`，都等你点开始 = 不开），中间那档下面列记住的工作区，每项可删 |
| 管家 | 管家其余的设置：脱敏（Harness 能脱敏才有）、工作区；“更多”里压缩阈值（Bridge 会压缩的 Harness 才有，即 Claude Code）和私聊模式（`MODE_CHOICES`）。纯话题模式下只有私聊模式一行 |
| 工作区 | 工作区列表；每行一行灰字写用途，“改用途”换成输入框，“保存用途”存 |
| 记忆（`COVEY_MEMORY=1`） | 下一轮带上什么（选工作区，带几条、各范围用了多少、快满或放不下的提示、原文），按范围列出的条目，每条“删除”，原则多一个“常驻”（`app/memory_panel.py`） |
| 能做什么 | 只读：启用的 Harness 和现在用不了的、开着的功能和读的开关、没开的怎么开、启用的扩展（`app/about_panel.py`） |

每行点开是选项，点一个就记下并整卡重画；只有主人能改。模型和思考预算只列说得出是哪一个的选项：模型行没有 Claude Code 目录里的“Default (recommended)”，思考预算行没有“推荐”和“模型原生默认”（`core/menu_choice.py` 的 `explicit`、`plain_budgets`），因为看不出它们实际跑哪个；模型页管家、独立复核两段的这两行也没有“默认”。新话题段的空选项留着，写的是它现在跟到哪个（“跟上次一样：Opus 5.5”）。以前存下的这类值照旧画出、打勾，换掉后就不再出现。代答的模型行（`cards/proxy_panel.py`）用同一套行，也一样。启动卡不变。私聊模式改成纯话题要再点“确定改”。子 Agent 一项对 Bridge 起的每一轮 Claude Code 都生效（`backends/claude/subagents.py`）。独立复核选了模型时，点「独立复核」就用它（`app/review.py` 的 `configured`），同一家厂商也照用；它的 Harness 本机没启用或现在用不了、模型不在目录里了、原话题开着脱敏而它不能脱敏、或思考预算模型不再支持时，照没设时自动挑，并在开复核的那句回复里说一句原因。旧卡按钮的去向、脱敏默认的迁移和子 Agent 的参数见[实现细节 · 设置卡的细节](butler-internals.md#设置卡的细节)。

## 话题链接

管家卡、回带卡上的 T 号是链接（`app/topic_links.py` 的 `linkify`），打开话题并停在一张卡上：回带卡上它自己的话题停在回带的那一轮的卡，别的停在最新的任务卡。

- 有话题串的用 applink `client/thread/open?…&thread_position=…`，读不到位置时只打开话题串。
- 位置由 `feishu/message_read.py` 的 `fetch_message_detail` 读一次，记在 `<state>/topic-links.json`，每个话题留 `KEPT`（8）条。
- 读不到保持纯文字；读消息在所有锁外（规则 1、2）；只改显示的文字，保存的回复（规则 5）不变。

### 派发卡（开关默认关）

`COVEY_DISPATCH_CARD=1` 时（见[配置 · 派发卡](configuration.md#派发卡)），管家派出话题的那一刻在聊天区多一张**总览卡**：聊天区里的一条新消息，顶上一块“你问 · 12:31 ›”，不开串；话题照旧在原处、有串、有自己的任务卡。（它不能做成对主人那条提问的回复：飞书对已经有话题串的消息，回复一律进串。）关着时没有这个调用、没有后台线程。

卡上是请求的开头、状态、T 号和用时、当前步骤，以及补充、停止、打开话题三个按钮；Bridge 里一个后台线程（`app/dispatch_cards.py` 的 `DispatchCards`）跟着话题的任务记录刷新它，结论回带后整卡换成一行“✅ 做完 · 结论在下方 ›”。卡的内容、记录、刷新节奏、按钮落在哪一轮见[实现细节 · 派发卡](butler-internals.md#派发卡)。

### 专项

专项卡、专项记忆与资料库、专项的问题分流、专项停住后问主人、整理专项、汇报风格的默认值，见 [projects.md](projects.md)。

### 我本来想找你的事（开关默认关，只记不打扰）

`COVEY_PATROL=shadow` 时，管家会替主人盯着手上的事，但只记下来、不打扰：不发消息、不叫醒负责人、不动专项。`on` 留给以后的版本，现在当作 `shadow`；其他值和不设都是关，关着时不起后台线程、不调模型、汇总不变。

- **怎么找**：规则每分钟从看板、手上的事和汇总在读的记录里找候选（问题久没回、结果没人看、连着失败、像是同一件事、专项停滞），小模型每 30 分钟判一次“丢掉 / 记下 / 该处理”。
- **只记的两条**：管家说了“稍后”“我去查”却没开也没转话题（`deferred`），话题的结论做完 10 分钟还没确认发回聊天区（`unrelayed`）。这两条只写进 `patrol/candidates/`，不交给小模型、不进汇总，先看一阵它们记下了什么。
- **怎么看**：早晚汇总和「看板」在有“该处理”的决定时多一块“我本来想找你的事”：最多 5 条，只列上次汇总之后的。这一块自己不会让汇总发出来。
- **怎么教它**：在私聊里说“这类该找我”或“这类不用”，管家用 `principle_save` 记成原则，小模型下一轮读得到。
- **怎么关**：不设 `COVEY_PATROL` 重新注册（见[改开关三步](configuration.md#改开关三步)）；已写的记录 14 天后被维护清掉。

两层的规则、记录位置和可调数值见[实现细节 · 我本来想找你的事：三层](butler-internals.md#我本来想找你的事三层)。

### 网页工作台

见 [web.md](web.md)。

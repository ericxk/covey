# 名词表

Covey 里每个概念只有一个中文名和一个英文名。界面、提示词、文档写中文名；代码、开关、文档里提到代码时用英文名。定下的几条：管家叫 butler；干活的单位统一叫话题；长活、快事换成人话，转成专项交给管家判断；全面改名，旧名兼容；普通的事不配负责人。新写的界面文字和提示词只用这张表里的词；表里“不再用”的词出现在界面文字或提示词里，测试会失败。

## 原则

- **界面和提示词说人话**：主人看到的卡片、回复，以及模型读到的规则和工具说明，都用日常中文。提示词里的词会被模型学去，再出现在它的回复里，所以两者用同一套词。
- **一个概念一个名字**：同一个东西不换着叫，不同的东西不共用一个名字。“线程”同时指过管家、专项里的话题和后台线程，所以界面和提示词里不再用它。
- **代码跟着英文名**：模块、类、函数、开关名用英文名（旧名到新名的对照见文末）。盘上已有的文件名、JSON 字段、任务进程协议里的名字不改，读的时候新旧都认，保证旧记录、旧版本的任务进程、回滚都能用。

## 名词

| 概念 | 中文 | 英文 | 不再用 | 说明 |
|---|---|---|---|---|
| 机器人的使用者 | 主人 | owner | — | 不变。 |
| 私聊里先接住主人每句话的那个 Agent：快答，其余交给话题或专项 | 管家 | butler | 主线程、main thread、Personal Agent（模式名） | 设置卡管家页上的私聊模式写成“私聊顶层的消息：交给管家 / 每条直接开一个话题”，文档里简称管家模式 / 纯话题模式。 |
| 交出去在跑的一件事：一个 Harness 会话加一个飞书话题串，编号如 T12 | 话题 | topic | 线程（指话题时） | 专项里的话题叫“专项的话题”。“线程”只留给后台线程，飞书的消息串叫话题串。 |
| 话题在飞书里的那串消息 | 话题串 | thread | — | 只指飞书的消息串。 |
| 话题串在聊天区的根卡片 | 话题卡 | topic card | — | 不变。 |
| 新部署第一次启动后发到主人私聊的那张卡：这台机器有什么、怎么用、三句示例 | 欢迎卡 | welcome card | — | 卡的标题是“欢迎使用 Covey”；每个部署只发一次。 |
| 管家交出去的事在聊天区的进度卡 | 派发卡 | dispatch card | 派发活卡片 | 卡上不写这个词。 |
| 每一轮的流式卡片 | 任务卡 | task card | — | 不变。 |
| 管家归到一起的话题：要分成几个话题、跨几天做的事，或同一件事反复来 | 专项 | project | 长活、方向、专员、方向群 | 可长可短，不分类型；没活放久了收起，有新的事再展开。 |
| 带一个专项的那个 Agent，只拆、派、跟进、收尾 | 负责人 | lead | — | |
| 属于某个专项的话题 | 专项的话题 | project topic（代码角色 member） | 线程、project thread | 盘上仍写 thread（topic.json 的 project.role），代码里叫 member：新旧版本混跑时，老版本的任务进程认不出新值。 |
| 专项在聊天区的卡片 | 专项卡 | project card | — | |
| 普通话题后来变成专项 | 转成专项 | upgrade | 由快事升级、升级（界面上） | 由管家判断：主人要长期跟进、要分成几个话题持续推进，或直接说“开个专项”“转成专项”时，管家带上 project。不再匹配关键词。 |
| 把现有专项和最近 7 天动过的话题重新看一遍，给出新建、归进、移出、合并、拆开、改名、收起的建议；定时的、手动的、卡片和汇总里那一块都叫它 | 整理专项 | tidy（代码 project_tidy、project_sorter） | 专项整理 | 开关 `COVEY_PROJECT_TIDY`。界面和提示词里这个动作只说“整理”，不说梳理、merge、重排（这几个词会出现在主人自己起的话题标题里，测试只拦“专项整理”）。 |
| 每个话题自己维护的几行话：本质、现在的目标、涉及、相关话题、和上次比变了什么 | 话题要点 | topic gist（检查点字段 `topic_gist`） | 话题摘要 | 由话题自己的模型写：没有要点、或目标和涉及的范围变了的那一轮更新，没变沿用上一份；存在 `topics/<id>/gist.json`。 |
| 一件只交给一个话题、做完就结束的事 | 普通的事 / 普通话题 | — | 快事 | 只在需要和专项对比时说。 |
| 话题在检查点里约好几点、看什么，这一轮先结束，到点 Bridge 再起它一轮去看 | 到点回来看 | revisit | — | 开关 `COVEY_REVISIT`。一轮里反复查外部状态、不结束叫轮询，见[使用指南 · 到点回来看](usage.md#到点回来看)。 |
| 话题做到哪些才算完的 1 到 4 条，每轮结束逐条标满足 / 没满足 / 没查 | 完成标准 | criteria（topic.json 和检查点字段 `criteria`） | 验收标准（指话题的这几条时） | 管家开话题时写，主人让管家改；专项自己的“目标和完成标准”是另一回事（`project_goal`）。 |
| 另开一个只读话题，换一家的模型只看主人原话、完成标准和这一轮的改动来复核 | 独立复核 | review（`app/review.py`） | 评审、代码审查（按钮和卡上） | 主人在答案卡上点了才开（`COVEY_REVIEW=1`），从不自己开。 |
| 主人明确要求记下的东西（原则、知识条目、专项和话题里的约定）合成的一种东西 | 记忆 | memory | 原则/知识条目/专项记忆分开说（界面上） | 开关 `COVEY_MEMORY`。每条标“你说的”（主人明说的）或“它总结的”（负责人自己归纳的）；代码里的原则、知识条目、专项记忆各自的存储名不变。 |
| 主人在设置卡「记忆」上标的、范围放不下时也一直带上的那一条 | 常驻 | pinned（`principles.json` 的 `pinned`） | — | 只有主人立的原则能标，每组最多 8 条。 |
| 一次模型往返 | 一轮 | turn | — | 不变。 |
| 每一轮在 Bridge 里的编号 | 任务 #N | job | — | 不变。 |
| 在跑的一轮里追加的话 | 补充 | supplement（代码 steer） | — | 不变。 |
| 管家同时在做的几件事 | 同时在做的事（界面：“还有 N 件事在做”） | track | 路、多路、一路 | 代码保留 track，只在内部用。这个功能（开关 `COVEY_BUTLER_TRACKS`）在文档里叫“同时做几件事”。 |
| 话题写好、主人在卡上确认后以主人身份发给别人的一条飞书消息 | 代发（卡：代发草稿） | owner send（检查点字段 send_as_owner） | — | 和代答共用发送器；代答是替主人回白名单里的人，代发是话题主动发。 |
| 产品 | Covey | Covey | — | |
| 跑在开发机上的服务进程 | Bridge | Bridge | — | 只在内部、日志和文档里用。 |

## 状态词

卡片、菜单结果卡和网页说一件事的状态只用这几个词，词和颜色都从 `covey/core/look.py` 的 `STATES` 取，三处一样（[UI 规范](ui.md#3-状态词表)）。失败、中断、超时、官方拒绝都叫停住了，原因放第二行；主人自己停的叫已停止。

| 状态 | 词 | 英文 key | 要换掉的说法 | 说明 |
|---|---|---|---|---|
| 等主人回答或开工 | 等你 | waiting | 等你回答、需要你回答 | 要你处理。 |
| 没成功：失败、中断、超时、官方拒绝，或不是主人停的 | 停住了 | stopped | 失败、执行失败、中断、已中断 | 要你处理；原因放第二行（`core/failure.py`）。 |
| 在跑但很久没动静 | 可能卡住 | stuck | — | 要你看一眼。 |
| 做完了还没看 | 有结果 | result | 有结果未看、做完没看 | 分组标题写“有结果没看”。 |
| 在跑 | 在做 | running | 进行中 | 背景。 |
| 排着队 | 排队 | queued | — | 背景。 |
| 做完、看过了 | 做完 | done | 已完成、完成 | 背景，默认折起。 |
| 主人自己停的 | 已停止 | cancelled | — | 背景。 |
| 网页上没有任务的话题 | 空闲 | idle | — | 背景。 |

“要换掉的说法”只在已经发出去的旧卡片和旧记录里还有，读的时候照认，不改历史卡。管家读到的词（快照的分组名和结局 `core/turn_words.py` 的 `GROUP_NAMES`、`RESULTS`，`topics_list`、`topic_read` 的说明和输出，管家规则）也用上表的词，管家学去的就是卡上的说法。代码里还留着一处旧词，改的时候换：执行过程面板在跑时的标题“进行中”（`cards/trace_view.py`）。

## 只在内部用的词

下面这些词可以出现在文档、代码和日志里，界面上不出现。负责人的规则里可以用“收件箱”“叫醒”，因为它们是人话。

| 中文 | 英文 | 是什么 |
|---|---|---|
| 收件箱、回执 | inbox、receipt | 负责人每轮读的新情况（start、thread_end、owner_said、stalled）。 |
| 叫醒、唤醒轮 | wake、wake turn | Bridge 按回执让负责人跑一轮。 |
| 护栏 | guard | token、唤醒次数、停滞三道机械检查。 |
| 放置 | placement | 一轮的回复放在哪。 |
| 回带 | relay | 话题的结论发回聊天区。 |
| 后台线程 | worker thread | Bridge 进程里的线程（如 ProjectLoop）。文档里写全“后台线程”，不单写“线程”。 |

## 代码改名对照

管家、专项的话题这批改名已经做完，只改了代码里的名字，不改行为。读旧代码、旧提交时按这张表对照。

| 旧名 | 新名 |
|---|---|
| 模块 main_thread、main_tools、main_topics、main_relay、main_reply、main_search、main_handoff、main_reactions、main_env、main_knowledge、main_tracks、main_thread_panel、main_mcp、main_requests、main_opened、main_tool_specs、main_thread_settings | butler、butler_tools、butler_topics、butler_relay、butler_reply、butler_search、butler_handoff、butler_reactions、butler_env、butler_knowledge、butler_tracks、butler_panel、butler_mcp、butler_requests、butler_opened、butler_tool_specs、butler_settings |
| 类和函数里的 main_thread、MainThread、main_turn、is_main_topic 等 | butler、Butler、butler_turn、is_butler_topic 等 |
| 专项角色 THREAD = "thread"、project_thread、is_thread_topic、THREAD_ROLE | MEMBER = "thread"（代码里叫 member，盘上的值不改）、project_member、is_member_topic、MEMBER_ROLE |
| 负责人的工具 project_thread_start、project_thread_send | project_topic_start、project_topic_send。工具清单只列新名；MCP 服务和 Bridge 收到旧名照样执行（`core/butler_tool_specs.py` 的 `RENAMED_TOOLS`），在跑的会话可能还带着旧名。 |
| 开关 COVEY_MAIN_THREAD、COVEY_MAIN_BUTLER、COVEY_MAIN_TRACKS、COVEY_MAIN_TRACKS_MAX、COVEY_MAIN_DISPATCH_CARD、COVEY_MAIN_JOB、COVEY_MAIN_STATE | COVEY_BUTLER、COVEY_BUTLER_RULES、COVEY_BUTLER_TRACKS、COVEY_BUTLER_TRACKS_MAX、COVEY_DISPATCH_CARD、COVEY_BUTLER_JOB、COVEY_BUTLER_STATE。读的时候新名优先，其次 COVEY_MAIN_*，最后 LARK_BRIDGE_MAIN_*（`core/env.py` 的 `RENAMED`）。服务清单仍存 LARK_BRIDGE_MAIN_*（旧版本认得的名字），所以升级不改清单、回滚照样能读。 |
| COVEY_PROJECT_UPGRADE 的值 word | butler（word 照认，意思相同） |

**盘上的旧名不改，照常读写**：
- 文件和目录：状态目录下的 main-thread.json、main-thread/；
- 字段：topic.json 里的 main_thread、任务记录里的字段名；
- 名字：MCP 服务名 bridge，以及给模型看的其余工具名和参数名（topic_start、topic_send 等；project_topic_send 的参数仍叫 thread）；
- 数据目录里的名字：`bridge_main_*` 卡片按钮、锁名、合成消息 id 的 `main-` 前缀、后台线程名 `main-tools`、`main-search`；
- 任务进程协议（runs/）里的文件名和字段。

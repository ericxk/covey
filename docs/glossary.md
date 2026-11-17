# 名词表

Covey 里每个概念只有一个中文名和一个英文名。界面、提示词、文档写中文名；代码、开关、文档里提到代码时用英文名。主人在 2026-11-16 定下（管家 / butler；干活的单位统一叫话题；长活、快事换成人话，转成专项交给管家判断；全面改名，旧名兼容；普通的事不配负责人）。新写的界面文字和提示词只用这张表里的词；表里“不再用”的词出现在界面文字或提示词里，测试会失败。

## 原则

- **界面和提示词说人话**：主人看到的卡片、回复，以及模型读到的规则和工具说明，都用日常中文。提示词里的词会被模型学去，再出现在它的回复里，所以两者用同一套词。
- **一个概念一个名字**：同一个东西不换着叫，不同的东西不共用一个名字。“线程”同时指过管家、专项里的话题和后台线程，所以界面和提示词里不再用它。
- **代码跟着英文名**：模块、类、函数、开关名用英文名（第二批改名完成后）。盘上已有的文件名、JSON 字段、任务进程协议里的名字不改，读的时候新旧都认，保证旧记录、旧版本的任务进程、回滚都能用。

## 名词

| 概念 | 中文 | 英文 | 不再用 | 说明 |
|---|---|---|---|---|
| 机器人的使用者 | 主人 | owner | — | 不变。 |
| 私聊里先接住主人每句话的那个 Agent：快答，其余交给话题或专项 | 管家 | butler | 主线程、main thread、Personal Agent（模式名） | 设置卡上的模式叫“管家 / 纯话题”。 |
| 交出去在跑的一件事：一个 Harness 会话加一个飞书话题串，编号 T123 | 话题 | topic | 线程、话题 | 专项里的话题叫“专项的话题”。 |
| 话题在飞书里的那串消息 | 话题串 | thread | — | 只指飞书的消息串。 |
| 话题串在聊天区的根卡片 | 话题卡 | topic card | — | 不变。 |
| 管家交出去的事在聊天区的进度卡 | 派发卡 | dispatch card | 派发活卡片 | 卡上不写这个词。 |
| 每一轮的流式卡片 | 任务卡 | task card | — | 不变。 |
| 管家归到一起的话题：要分成几个话题、跨几天做的事，或同一件事反复来 | 专项 | project | 长活、方向、专员、方向群 | 可长可短，不分类型；没活放久了收起，有新的事再展开。 |
| 带一个专项的那个 Agent，只拆、派、跟进、收尾 | 负责人 | lead | — | |
| 属于某个专项的话题 | 专项的话题 | project topic（代码角色 member） | 线程、project thread | 盘上仍写 thread（topic.json 的 project.role），代码里叫 member：新旧版本混跑时，老版本的任务进程认不出新值。 |
| 专项在聊天区的卡片 | 专项卡 | project card | — | |
| 普通话题后来变成专项 | 转成专项 | upgrade | 由快事升级、升级（界面上） | 由管家判断：主人要长期跟进、要分成几个话题持续推进，或直接说“开个专项”“转成专项”时，管家带上 project。不再匹配关键词。 |
| 一件只交给一个话题、做完就结束的事 | 普通的事 / 普通话题 | — | 快事 | 只在需要和专项对比时说。 |
| 主人明确要求记下的东西（原则、知识条目、专项和话题里的约定）合成的一种东西 | 记忆 | memory | 原则/知识条目/专项记忆分开说（界面上） | 开关 `COVEY_MEMORY`。每条标“你说的”（主人明说的）或“它总结的”（负责人自己归纳的）；代码里的原则、知识条目、专项记忆各自的存储名不变。 |
| 一次模型往返 | 一轮 | turn | — | 不变。 |
| 每一轮在 Bridge 里的编号 | 任务 #N | job | — | 不变。 |
| 在跑的一轮里追加的话 | 补充 | supplement（代码 steer） | — | 不变。 |
| 管家同时在做的几件事 | 同时在做的事（界面：“还有 N 件事在做”） | track | 路、多路、一路 | 代码保留 track，只在内部用。 |
| 产品 | Covey | Covey | — | |
| 跑在开发机上的服务进程 | Bridge | Bridge | — | 只在内部、日志和文档里用。 |

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

## 第二批：代码改名对照

第二批只改代码里的名字，不改行为。

| 现在 | 改成 |
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

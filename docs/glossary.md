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
| 私聊里先接住主人每句话的那个 Agent：快答，其余交给话题或专项 | 管家 | butler | 主线程、main thread、Personal Agent（模式名） | 部署设 `COVEY_BUTLER=0` 时没有管家，文档写“没有管家”。设置卡上以前的“私聊顶层的消息：交给管家 / 每条直接开一个话题”（管家模式 / 纯话题模式）已下线，不再用这两个词。 |
| 网页工作台里你、管家和各专项的负责人在一条时间线上的那一页，和飞书私聊顶层对应 | 大厅 | room（`app/web_room.py`） | 私聊、私聊区（指网页这一页时） | 飞书里主人和机器人的一对一聊天仍叫私聊、它的顶层叫私聊顶层，不跟着改。专项页的负责人对话是大厅里只看这位负责人的那部分。 |
| 交出去在跑的一件事：一个 Harness 会话加一个飞书话题串，编号如 T12 | 话题 | topic | 线程（指话题时） | 专项里的话题叫“专项的话题”。“线程”只留给后台线程，飞书的消息串叫话题串。 |
| 话题在飞书里的那串消息 | 话题串 | thread | — | 只指飞书的消息串。 |
| 话题串在聊天区的根卡片 | 话题卡 | topic card | — | 不变。 |
| 新部署第一次启动后发到主人私聊的那张卡：这台机器有什么、怎么用、三句示例 | 欢迎卡 | welcome card | — | 卡的标题是“欢迎使用 Covey”；每个部署只发一次。 |
| 管家转给已有话题的事挂在提问下面的进度卡 | 派发卡 | dispatch card | 派发活卡片 | 卡上不写这个词。开新话题不发。 |
| 每一轮的流式卡片 | 任务卡 | task card | — | 不变。 |
| 管家归到一起的话题：要分成几个话题、跨几天做的事，或同一件事反复来 | 专项 | project | 长活、方向、专员、方向群 | 可长可短，不分类型；没活放久了收起，有新的事再展开。 |
| 带一个专项的那个 Agent，只拆、派、跟进、收尾 | 负责人 | lead | — | |
| 属于某个专项的话题 | 专项的话题 | project topic（代码角色 member） | 线程、project thread | 盘上仍写 thread（topic.json 的 project.role），代码里叫 member：新旧版本混跑时，老版本的任务进程认不出新值。 |
| 专项在聊天区的卡片 | 专项卡 | project card | — | |
| 话题把只有管家能办的事（开专项、收进专项、另开话题、交给别的话题）交给管家，私聊顶层一条“↪ T12 转给管家” | 转给管家 | ask_butler（检查点字段，`app/butler_asks.py`）；轮次来源 handover | — | 管家按主人在话题里的原话办，话题的理解只作参考。 |
| 普通话题后来变成专项 | 转成专项 | upgrade | 由快事升级、升级（界面上） | 由管家判断：主人要长期跟进、要分成几个话题持续推进，或直接说“开个专项”“转成专项”时，管家带上 project。不再匹配关键词。 |
| 把现有专项和最近 7 天动过的话题重新看一遍，给出新建、归进、移出、合并、拆开、改名、收起的建议；定时的、手动的、卡片和汇总里那一块都叫它 | 整理专项 | tidy（代码 project_tidy、project_sorter） | 专项整理 | 开关 `COVEY_PROJECT_TIDY`。界面和提示词里这个动作只说“整理”，不说梳理、merge、重排（这几个词会出现在主人自己起的话题标题里，测试只拦“专项整理”）。 |
| 每个话题自己维护的几行话：本质、现在的目标、涉及、相关话题、和上次比变了什么 | 话题要点 | topic gist（检查点字段 `topic_gist`） | 话题摘要 | 由话题自己的模型写：没有要点、或目标和涉及的范围变了的那一轮更新，没变沿用上一份；存在 `topics/<id>/gist.json`。 |
| 一件只交给一个话题、做完就结束的事 | 普通的事 / 普通话题 | — | 快事 | 只在需要和专项对比时说。 |
| 话题在检查点里约好几点、看什么，这一轮先结束，到点 Bridge 再起它一轮去看 | 到点回来看 | revisit | 约好回来看、该回来看了（界面上） | 开关 `COVEY_REVISIT`。功能叫到点回来看；看板和网页上的组和行主语是 Covey：组叫“Covey 定时回看”，行写“Covey 将于 21:40 自动回看：…”“Covey 正在自动回看：…”，不写“约好回来看”“该回来看了”（读着像等主人回来看）。一轮里反复查外部状态、不结束叫轮询，见[使用指南 · 到点回来看](usage.md#到点回来看)。 |
| Bridge 后台的一问一答（分流、整理专项、补话题要点、记忆自己长）问的那个 Harness，设置里一项 | 辅助模型 | assist（`app/assist.py`，`main-thread.json` 的 `assist`，记录 `assist-state.json`） | 小模型判断、后台模型、小模型（指这一项时） | 选项：自动（跟随管家）、点名一个 Harness、不用模型（只按规则）。见[配置 · 辅助模型](configuration.md#辅助模型)。“后台线程”是另一回事。 |
| 改卡片或网页的样子之前，给主人点选的 2~3 版：一道单选题，每个选项带一版卡片 | 样张 | sample（检查点问题选项的字段 `sample`，`covey/core/ui_samples.py`） | 款式（指这几版时） | 流程和 Bridge 怎么画见 [UI 规范 · 改外观先选样张](ui.md#12-改外观先选样张)。测试里固定的那组卡片（`tests/golden/cards.json`）叫卡片样本，不叫样张。 |
| 话题做到哪些才算完的 1 到 4 条，每轮结束逐条标满足 / 没满足 / 没查 | 完成标准 | criteria（topic.json 和检查点字段 `criteria`） | 验收标准（指话题的这几条时） | 管家开话题时写，主人让管家改；专项自己的“目标和完成标准”是另一回事（`project_goal`）。 |
| 一件交付上，主人点名的几位参与者之间的往返：咨询、答复、澄清，最后由汇总方交一份带出处的结论 | 协作 | collab（`core/collab.py`，开关 `COVEY_COLLAB`） | — | 开发中，见[多参与者协作](architecture.md#多参与者协作开发中默认关)。首版只读咨询，要改东西走正式分工。 |
| 能被点名、带着自己的上下文回答的一方：管家、负责人或话题 | 参与者 | participant | — | 话题本身就是参与者，身份是它的话题 id。大厅顶上那排仍叫成员。 |
| 对主人交这次协作结论的那个参与者 | 汇总方 | integrator | 整合者、主办方 | 界面写“由 T3 汇总”。在已有话题里请人帮忙时就是原话题。 |
| 负责人把专项的完成标准拆成的几条：每条由哪个话题交付、依赖哪几条、证据在哪、还缺什么，满足了没有 | 目标检查表 | checklist（`core/project_checklist.py`，工具 `project_checklist`，开关 `COVEY_COLLAB`） | — | 开发中，见[负责人对照目标跟进](projects.md#负责人对照目标跟进)。和话题自己的完成标准是两回事；还有没满足的条目或没处理的协作缺口时负责人收不了尾。 |
| 负责人写给主人看的专项概况：背景、目标、拆成的几个动作和各自的一句现状 | 项目简报（页面标签、界面上简称简报） | brief（`core/project_actions.py`，记录的 `lead_background`、`actions`，开关 `COVEY_COLLAB`） | — | 开发中，见[负责人对照目标跟进](projects.md#负责人对照目标跟进)。页面「简报」标签并进了原来的「进度」。背景用 `project_goal`、动作用 `project_checklist` 写；动作的状态由 Bridge 和页面按条目和话题现算，不存。 |
| 项目简报里把目标拆成的一件事，带着它的检查表条目和话题；3 到 8 个，没标不做了的至多 10 个 | 动作 | action（`core/project_actions.py`，id `a1`…，工具参数 `action`） | — | 状态词只用：等你、在做、做完、待核对、将要做。不做了的叫“不做了”（`dropped`），收起、不删。和检查点里的“步骤”、话题的“任务”不是一回事。 |
| 协作里一方问另一方的一次请求；对答复再问一次叫澄清 | 咨询、澄清 | ask（第 1、2 次） | — | 每位协助方各一次。 |
| 一次咨询在被问的那一方原会话的临时只读分叉里跑的那个话题目录 | 咨询通道 | channel（`app/collab_channel.py`，话题 id `collab-…`） | — | 内部词，主人看不到：没有话题串、没有 T 号、不上看板；被问那一方的会话和记录都不动。 |
| 主人要 Covey 记住的东西，分规矩、资料、决定三类，范围是全局、专项、话题 | 记忆 | memory（`core/memstore.py`，开关 `COVEY_MEMORY`） | 原则/知识条目/专项记忆分开说（界面上）；记忆中心；长期约定、当前决定、待核对参考、历史记录（指记忆的分组时） | 主人自己的话（话题里说的、问题卡上答的、对管家说的）只经一个口写进来（`core/memory_gate.py`）。网页上看和改全部记忆的那一页就叫「记忆」（`/memory`），只有一页：待审核（有才出现）、按全局 / 专项 / 话题分的组、页底“记录”。负责人自己记的规矩和资料标“负责人记的”，在它的专项里马上生效，但不能单独作为放行部署、删除、花钱、对外发送、占机器的依据。回复里不写短码，写那条记忆说的是什么；主人私聊里漏出来的真短码，发送前换成标题（`core/memory_words.py`），代码、链接和问题卡选项原文不动。收起的条目不带、查得到、能放回。代码里的原则、知识条目、专项记忆各自的存储名不变。 |
| 一轮结束后 Bridge 自己读主人的话记下长期要求、按需查放不下的那一组做法 | 记忆自己长 | memory learning（开关 `COVEY_MEMORY_LEARN`） | — | 界面上只说“记下了”“整理记忆”“查记忆”。 |
| 每天按规则把专项里办完的、说别的专项的、很久没用的资料和决定收起来（规矩一条不收） | 整理记忆 | memory tidy（`app/memory_tidy.py`） | — | 只收起不删除，不调模型；晚间汇总上能撤销。 |
| 话题一律从这里启动的那个目录：部署的 `--workdir` | 主目录 | home（`core/home.py`） | 选工作区（指开工前选目录时） | 取代“选工作区”：话题不再开工前选目录，用到哪个仓库由 Covey 记下（旧话题绑着的工作区照旧能续）。`--workdir` 参数名不变。管家或负责人开话题时标了 `local: false` 的外部话题（只解读外部资料、写报告）也从这里启动，但规则里不给常用路径和主目录的说明文件，各 Harness 也不加载它们（[管家 · 外部话题](butler.md#外部话题)）。 |
| 这个部署常在哪些仓库和目录里干活（本机的，或某台设备上的）：名字、路径、类型（仓库或目录）、设备、用途、状态、写入方式 | 常用路径 | repo map（`core/repomap.py`，`<状态目录>/repos.json`） | 仓库地图 | 网页设置的「常用路径」页管它。Harness 读渲染出来的 `repos.md`；命令 `covey repo`（`list`、`use`、`status`）。 |
| 一个话题实际读过或改过的仓库，改的那个带本话题的 worktree | 用到的仓库 | topic repos（`core/topic_repos.py`，`topic.json` 的 `repos`） | — | 改动对比、提交核对、同仓库并行提醒都按它逐个做。 |
| 一个话题的输出、日志、临时文件放的目录，由 Covey 按模板设好 | 话题产物目录 | topic run dir（`home.json` 的 `runs_template`） | — | |
| 每轮都全文带上的记忆：主人定的做法和约束 | 规矩 | rule（`core/memstore.py` 的 kind） | 长期约定 | 记忆的三类之一，范围只有全局、专项、话题（没有按仓库分的一层）。全局的规矩只放跨一切场景每一轮都照做的，有条数和字数上限，放不下的只列标题；只在某类事、某个对象上照做的记成全局资料。 |
| 主人在问题卡上的回答，只给在办这件事的话题带 | 决定记录 | decision（`decisions.jsonl`） | 当前决定 | 模型读到的记忆里叫“决定”。只记真回答（点了选项，或打字和选项原文一样的）。问这道题的话题一直带；负责人问的，带给在办的成员话题；话题结束、交付了或同一道题有了新回答才办完。一道题的回答只管它问的那个范围，换了批次或范围照常问。 |
| 相关时才带的记忆：带场景的交代（某类事怎么做、对谁），以及坑和事实 | 资料 | note | 待核对参考 | 和主人这一轮的话冲突时听主人的。全局的资料只在管家和负责人自己的轮里带（有条数和字数上限，放不下的只列标题），派活时挑给话题（工具参数 `memory`）；话题只带挑给它的和检索命中的，其余写成“相关但没带的”标题。 |
| 自动学到的规矩里推断要管全局的、涉及要主人点头的，等主人点了才算数的那一处 | 待审核 | review（`core/memory_review.py`，`memory/review.json`） | — | 只收这两种：推断全局（先在说的地方生效，「采纳」才提到全局）和放行类（学到的规矩或资料说到部署、删除、花钱、对外发送、占机器，「采纳」才写下）；另有回滚冲突、旧版迁移这类一次性的。「不要」是保持原样；只认主人自己的提交，负责人替答不算；负责人记的规矩不进这里；14 天没处理关成过期。 |
| 主目录里的话题能去的机器：设备名单里的一台，带用途、可写还是只读、能不能 ssh 过去，和它的监控节点并成一行 | 设备 | device（`core/devices.py`，`devices.json`；开关 `COVEY_DEVICES`） | 执行设备（只在旧入口的下线说明里出现） | 网页设置的「设备」页管它；没有 ssh 的只能看状况、不能执行。话题不开到设备上，从主目录用 `covey device` 去（只读的轮用设备工具）；Codex 话题可以把可写设备挂成自己的执行环境。 |
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

“要换掉的说法”只在已经发出去的旧卡片和旧记录里还有，读的时候照认，不改历史卡。看板、手上的事、专项卡、话题状态卡和派发卡，任务卡和它的执行过程、状态行，问题卡、恢复卡、答案卡，换配置的交接，设置卡的「能做什么」和「记忆」已经换完：它们的卡片样本和画它们的代码里出现这些说法，`tests/test_glossary.py` 会失败（它列着查哪些模块和样张）；早晚汇总和网页还在换。执行过程里一步没成功叫“出错”，它不是话题的状态；命令或检查自己报的错（`git diff 失败`、`创建 worktree 失败`）照原样写。管家读到的词（快照的分组名和结局 `core/turn_words.py` 的 `GROUP_NAMES`、`RESULTS`，`topics_list`、`topic_read` 的说明和输出，管家规则）也用上表的词，管家学去的就是卡上的说法。

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
| 等主人的账、对账、补发 | owner ledger、reconcile、resend | 等主人的事（等你、停住了、可能卡住、有结果）的一本账，各处都从它数；Bridge 定时核对它，没到聊天里的卡再发一次（`app/owner_ledger.py`）。界面上说“等你的事”“补发”。 |
| 轮次来源 | turn source | 一轮是怎么来的、话是谁的（`core/turn_source.py`）；只由 Bridge 写，没有就照原来。 |
| 来源等级 | level | Bridge 按真实入口给一句话的等级：主人的原话（`owner`）或别的（`agent`）。 |

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

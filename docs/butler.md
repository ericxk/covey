# 管家

管家是私聊里一个常驻的 Claude Code 会话（设置卡的私聊模式选“管家”时）。主人在私聊**顶层**发的消息都交给它：能直接答的自己答，要干活的开话题或转给已有话题，主人说“T412 选 A”时替主人答选择题。用户视角见[使用指南 · 管家](usage.md)，部署开关见[配置](configuration.md)。

## 组成

- **私聊模式开关**：`core/butler.py` 的 `enabled`（见「私聊模式」），设置在 `core/butler_settings.py`。
- **管家会话**：`app/butler.py` 把顶层消息交给管家话题，每轮带规则、快照和 MCP 配置（见「会话」）。
- **MCP 工具**：会话里的 stdio MCP 服务 `core/butler_mcp.py`（入口 `bin/covey_butler_mcp.py`），清单 `core/butler_tool_specs.py`。
- **请求通道**：`core/butler_requests.py`，一次调用一个请求文件，由 Bridge 进程里的 `app/butler_tools.py` 执行（见「工具通道」）。

## 私聊模式

`core/butler.py` 的 `enabled` 是唯一的判断，各处都问它。两层：

1. **部署允许**（`available`）：`COVEY_BUTLER=0` 时不允许：管家的后台线程不起，菜单键不认，设置卡没有「私聊」「新话题」页。其他值都允许。
2. **主人选的模式**（`agent_mode`）：`main-thread.json` 的 `mode`，`agent` 是管家，`topics` 是纯话题。主人在设置卡「私聊」页第一行切换，马上生效；Bridge 和任务进程每次 stat 一下，变了才重读。保存时同时写 `enabled` 给旧版本。

**默认值只写一次**：Bridge 启动、处理事件之前（`Butler.settle` → `settle_mode`），文件里没有 `mode` 就写：状态目录里没有任务也没有话题时写管家；否则 `COVEY_BUTLER=1` 且设置没关的写管家，其余写纯话题。之后只看 `mode`。

**纯话题**下和没有管家时一样：顶层每条消息开一个话题；引用管家以前的卡当作新消息开话题（`core/topics.py` 的 `context_for_topic`），引用“交给一个话题”的管家卡照旧进那个话题；正在跑的管家那一轮跑完，按 `BUTLER_OFF` 收尾；会话和记录留着；管家开过的话题不再回带、不再放顶层问题卡副本。群聊两种模式都没有管家。

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

- **补充**：有任务没结束时，新消息用 `deliver_steer` 并进最新一轮，不发回执卡（开了[同时做几件事](#同时做几件事开关默认关)时，并进哪件在做的事由分流定，`Butler.supplement` 接目标任务，被拒改成新开一件）。可能已送达的不重发（规则 6），在主人消息下说一句；卡上已列出的不再各说（`TurnDelivery.listed`）。
- **收尾时发来的消息**：确定这一轮没读到的补充，自动排成管家的新一轮，和主人刚发一条新消息一样：同一个 message_id 作为这一轮的请求，放置、表情和回答都落在它上面，不开话题串、不说 `SUPPLEMENT_LOST`。“确定没读到”有两种：held 后被拒（从没发出，`drop_held_steers`、`_send_held`），和已发出但 Claude 到结束都没报告读取（`end_unread`）。Claude 交回结果之后到这一轮离开登记表之间来的补充也会 held（后端不再收）；任务离开登记表时（`BridgeApp.process_reply_job` 末尾，任务进程在收件箱停下后再一次）由 `Steering.settle_held_at_end` 当场按被拒结算，不等 steer-hold 后台线程下一秒的检查（任务进程退出会把它切断）。不确定的（`uncertain`）、主人停止的一轮（`cancel_event`）、没存 `requeue` 的旧记录照旧说一句；卡上已列出的（`listed`）不排也不说。做法：`Butler.supplement` 在送达记录里存 `requeue`（原文、发给模型的文字、发送人）；发现丢了的一方（`Steering.requeue_lost`）给记录标 `requeue_auto`，在 Bridge 里马上、在任务进程里由 Bridge 在任务进程交还或退出时（`app/detached.py` 的 `queue_lost`）按 `saved_at` 先后各排一轮（`Steering.queue_lost`，走“重新排队执行”的 `_requeue`，`requeue_state`、`requeued_job_id` 保证只排一次，重启后也不再排）。那一轮在没有 Bridge 看着时结束的（Bridge 停着时任务进程退出了，或 Bridge 没来得及排就停了），由 Bridge 启动时在收养任务进程之后补排（`app/detached.py` 的 `queue_stranded`），还在跑的任务进程的留给它结束时排。排队出错的（可能已经排上，`requeue_state` 为 `uncertain`）不再排，在主人那条消息下说一句 `SUPPLEMENT_UNCERTAIN`。已经单独成轮的不再算进旧一轮的“你问”和交接（`app/butler_handoff.py` 的 `_supplements`）。同一条消息的事件再来，`deliver_steer` 看到 `requeue_auto` 不再当补充。
- **等主人的话**：转发框里的留言会在转发后一两秒单独发来。没有任务时，没有自己文字的消息（`bare_message`：卡片、合并转发、图片、文件、只有链接）照常入队，但 `start_held_until` 设成 30 秒后（`HOLD_SECONDS`）。这期间主人的文字经 `merge_before_start` 并进请求并立即放行，写进 `asked_text`；到点没等到就开工。这个时间不存盘。

### 直答与转交

规则在 `app/prompts.py` 的 `butler_rules`（`AREA_OPENING`、`AREA_RULE`），放在每轮用户消息开头：

- 只读、一两分钟能做完的，管家自己做、直接答；只问已有结果的用 `topic_read` 读了自己答。
- 改代码或文件、要跑几分钟以上、几件事并行、主人说开话题的，交给话题：新的事 `topic_start`，同一件事 `topic_send`；两个以上话题都对得上时用 AskUserQuestion 让主人选。
- 交出去后只说一句放到了哪，不报进度和结论（结论由 Bridge 回带，进度看 Pin）。
- 写给人看的工具说明用主人的语言。

**开工三档**（`starts_at_once`，设置 `start`）：只读的、主人在这条消息里亲口要的、主人记住过的“工作区 × 只读/改文件”直接开工；其余在话题里出预填好的启动卡。

### 管家规则

`COVEY_BUTLER_RULES=1` 时，[直答与转交](#直答与转交)的前两条（一两分钟能做完的自己做；其余交给话题，“可能超过一两分钟”）换成 `app/prompts.py` 的 `BUTLER_RULES`（两条），让管家只快答和分派，这样它接得住主人的连问；`topic_start` 的工具说明里同样的话也换了（`core/butler_tool_specs.py` 的 `START_BUTLER`；工具清单由 MCP 子进程按环境变量列，所以 `core/butler.py` 的 `mcp_config` 把 `COVEY_BUTLER_RULES` 一并交给它）；其余规则（`SHARED_RULES` 的后面各条、`AREA_OPENING`、`AREA_RULE`、知识库的规则）不动。

- **快答**：不用工具，或只用一两次只读的查询（看一眼文件或日志、查一项配置、`topic_read`、`topics_list`），几秒内能答完，直接答、不开话题。
- **派出去**：要查一圈、要跑命令、要改代码或文件、要联网或多步操作、预计超过半分钟的；拿不准也派。新的事 `topic_start`，和已有话题是同一件事 `topic_send`（怎么分仍看下面几条）。
- **派出去后说什么**：一句话，放到哪个 T 号、大概多久，如“放到 T432「标题」里做，大概十分钟”；不写进度和结论（`AREA_RULE`）。
- **例外**：主人明确说“你直接做”“别开话题”“就在这儿做”时，这一轮自己做完再答。
- **兜底**：自己干了 90 秒的一轮照旧出卡（`CARD_AFTER`），不另设变量。
- **打开和关掉**：见[配置 · 管家规则](configuration.md#管家规则)。关着时规则、提示词和原来逐字节相同（`tests/test_butler_rules.py`、`tests/golden/butler_prompt.txt`）。

**旧会话什么时候用上**：管家的规则不在会话的首轮快照里（会话沿用首轮的系统规则只管 `bridge_rules`，而且各后端现在都不走系统级）。`butler_message` 每一轮都重新拼规则，放在这一轮用户消息的开头，续接的会话也一样。所以变量生效的时机是：服务带着变量重新 `launch` 之后，管家的**下一个聊天轮**就用新规则，不用新开会话、不用等压缩。要注意两点：一是任务进程是 Bridge 按当时的环境启动的，已经在跑的那一轮用旧规则；二是续接的会话里，以前每一轮的消息开头也带着旧规则的原文，在压缩（`compact_tokens`、空闲压缩）把它们摘要掉之前，模型会同时看到新旧两条，以最近一轮的为准。想立刻干净，可以让管家压缩一次或换新会话。关掉变量反过来也一样。

### 同时做几件事（开关默认关）

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
- **小模型的待命进程**（`app/track_standby.py`，`COVEY_TRACK_JUDGE_WARM`，默认开）：一次性的 Claude Code 每问一次要先花约 1 秒起命令行，第一个请求也比后面的慢（一次实测：冷起 p50 约 3.4 秒，其中约 1.2 秒是起进程，其余是模型），过不了硬上限（`COVEY_TRACK_JUDGE_MS`，默认 3 秒）。所以 `Tracks.handle` 一收到管家消息（家族里有没有活任务都一样，这条消息可能就是下一条要比对的任务）就调 `track_judge.warm`：起一个 stream-json 模式的 Claude Code，先用一条虚构的消息预热（它的第一个请求和连接就热了，里面没有主人的话），再等在标准输入上；`judge` 用 `take` 取走它、写进这一问、读答案，用完杀掉进程组，`judge` 之后 `Tracks.decide` 立刻再 `warm` 起下一个，所以每个进程只回答一个问题，上下文里没有别的消息。拿不到待命进程（还在起、已经死了、argv 变了、开关关了）就照旧现起一个一次性进程，日志行格式不变（`via=claude`），也不写主人的原话。空闲 10 分钟（`IDLE_S`）、超过 30 分钟（`MAX_AGE_S`，下次 `warm` 换新的）、超过硬上限、Bridge 停止（`stop_children` → `Butler.close`）和解释器退出都杀它；`COVEY_BUTLER_TRACKS` 没开、模式不是管家、通道不是 `claude`、`COVEY_TRACK_JUDGE_WARM=0` 时不起任何进程。每个待命进程多一次预热的小请求（几十个 token）。底线是模型自己：一个只有几十个 token 的 Haiku 请求经本机网关就要 1.1 到 1.9 秒，所以待命后的 p50 约 1.8 秒（负载高的开发机上量的，最快约 1.0 秒），达不到“1 秒内”；2 秒的硬上限下仍有四成左右超时、按新开一件。要更稳可以把 `COVEY_TRACK_JUDGE_MS` 放宽到 3000，或经 `cmd:` 接更快的通道。转发、图片、文件、只有链接的消息之后 30 秒内的文字（没引用、规则落到第 9 条）直接并进那条消息去的那件事，不进分流。
- **动作**：`stop`（停止消息不建任务、不进模型）：任务在任务进程里时，把它任务卡上“停止”按钮的点击转给那个任务进程（`runner_ipc.forward_card_action`，同代答的 `proxy.stop_job`），由任务进程里的 `cancel_reply_job_from_card` 停自己的任务；任务还在 Bridge 里（排队、没交出去）才在 Bridge 里直接调它。只在 Bridge 里给任务的副本设停止标记到不了任务进程，后台命令和答案照常出来（2026-11-15 验收）；主人引用的消息按 `reply_to` 认（lark-cli 1.0.69 起事件里只有 `reply_to`，没有 `parent_id`，`quoted_id`）；`merge` 把消息别名改到目标路再走 `app.handle_event`，`Butler.supplement` 带 `target`（目标任务）；目标在等回答时是回答，走原来的续接；`new` 按 `pick_slot` 选槽、`keep_track_topic`、改别名后入队。
- **送达被拒**：`SteerRefused`（任务刚结束、后端不收补充）或目标已结束，`supplement` 调 `fresh()` 把消息改成新开一件（返回新槽的上下文，`BridgeApp.handle_event` 用它入队）；`SteerUncertain` 照旧说一句、不重发。
- **选槽**（`pick_slot`）：槽 0 空闲就用槽 0，否则编号最小的空闲槽 1..4；在途（排队、开始、在跑）任务数到 `COVEY_BUTLER_TRACKS_MAX`，或没有空闲槽，就选在途最少的槽（并列取编号小的），排在它后面（同话题先进先出，调度器不改），消息记一次 `Tracks.noted("queued", chat, message, ctx=, app=)`。等回答的槽、有 `waiting_user` 记录的槽算占着，不分给别的路；空闲槽开始前清掉上一次遗留的会话指针。
- **这些事的一轮**（槽 ≥1，`butler_turn` → `track_start`）：会话指针为空时分叉槽 0 的会话（`fork_session`，槽 0 有一轮在跑且已记下 `prompt_uuid` 时加 `fork_at`＝`rewind_point(...)["keep"]`，没记就整个分叉），消息是续接式的（`resume_message`）加管家规则、**完整**快照（`take_snapshot(save=False)`，不动槽 0 的 `main-snapshot.json`）和「并行说明」（另外几件事此刻在做什么，每件一行，读任务记录，不依赖 Bridge 的登记表）。分叉不了（槽 0 没有会话、工作区不同、后端不支持、`keep` 为空、`rewind_point` 失败）退回新会话＋槽 0 最近的 `history.jsonl`（`first_turn_message`）。不做压缩检查，也不停空闲压缩；`compact_butler` 对槽 ≥1 直接返回。
- **一轮结束**：`process_bridge_reply_job` 在 `_run_turn` 之后调 `release_pointer`：槽 ≥1 且状态不是 `waiting_user` 就清会话指针（会话文件留着）；等回答的那件事指针保留，回答的续接轮接着用。
- **开了又关**：槽话题留在盘上，这时它们是普通话题（`traits` 给 TOPIC），回答、引用老卡都按 `topic_id` 找到会话指针继续；关着时槽 ≥1 不是管家，下次再开时空闲槽的遗留指针由 `enter` 清掉。

- **表情**（`app/butler_reactions.py`，只给主人自己的消息、话题挂在提问下面时的管家一轮，`Tracks.noted` 是调用点；失败只记日志，都在锁外）：排在忙槽后面的消息挂 **排队中**（`OneSecond`）而不挂 OnIt（`start` 见 `HELD` 就跳过）；这一轮在任务进程里开跑时（`worker.process_bridge_reply_job` 里的 `begin`）先摘排队中、再挂 OnIt。进程之间不传状态：`begin` 用 `Messenger.reaction_ids` 查机器人自己挂的排队中，查不到就什么也不做（没排过队的消息 OnIt 已在入队时挂了）；排队时被取消的任务由 `cancel_reply_job_from_card` 摘掉。并进成功的消息挂 **并进**（`THUMBSUP`），一直留着；主人引用它说「单独答」（规则 3）时摘掉，新开的那件事照常 OnIt → 结束表情。`Get` 已表示“交给话题”，不用。表情代号用接口试过：飞书接受 `OneSecond`、`THUMBSUP`，不接受 `WAIT`。开关关着时一个都不调。
- **停止的回复**：`stop` 把家族里其他还在做（不在等回答）的件数 k 传给 `cancel_reply_job_from_card(others=k)`，k>0 时文字回复末尾加“还有 k 件事在做。”（带任务卡的停止只改卡，不加）。k 随点击的 `action_value.others` 带到任务进程。任务卡的停止按钮和派发卡的 ⏹ 停止也算同一个数：点击到达 Bridge 时（`card_routing.count_others`）按 `butler_tracks.stop_others` 算（只算家族的任务，或派发卡；多路并行和管家模式没开时为 0；和停止词共用 `others_going`），写进 `action_value.others`，Bridge 自己停和转给任务进程都带着。其他话题任务卡上的停止不变。
- **停止后不再发答案**：开关开着、属于家族的任务被主人停止（`cancel_requested_by` 有值）时，Claude 因“只剩后台任务在跑”而保留的最后一条完整回复（`backends/claude/runtime.py` 的 `keep_last_reply`）不再发出，这一轮按已停止收尾（`app/turn_settle.py`，`core/butler_tracks.py` 的 `owner_stop_drops_reply`）。开关关着、或普通话题的停止，仍然发出那条回复。Claude 收到 SIGTERM 时自己结束它的后台命令（它们不在 Claude 的进程组里）。


**记忆合并**（`core/track_ledger.py`、`app/track_digest.py`）：路是槽 0 会话的分叉，它答了什么不在管家自己的会话里，所以交给管家的下一轮。

- **账本**：槽 ≥1 的一轮结束、结果送达之后，`TurnDelivery.deliver` 末尾（`relay` 之后）调 `ledger` 回调（`track_digest.record`），写 `main-thread/tracks/<任务键>.json`：`key`、`chat_id`、`track`（槽号）、`owner_message`（主人消息号）、`request`（≤200 字）、`answer`（≤600 字，只取已送达、已授权的回复正文，没送达就是空）、`status`（`done`/`failed`/`cancelled`）、`ended_at`、`session_id`、`opened`（这一轮开的话题的 T 号）、`digested_by`（读过它的管家那一轮的任务键，起初为空）。等回答（`waiting_user`）和中断的一轮不写，回答后的续接轮写；槽 0 的不写（答案已在它自己的会话里）。写失败只记日志。
- **交给管家**：槽 0 的一轮在快照之后加一块“[另外几件事这段时间答了什么]”：12 小时内、最多 8 条（更早的记一行“还有 N 条没列”），每条“完成时刻（失败、已停止注明）请求「…」→ 答案前 300 字 → T 号”。块做好后用记录锁下的 compare-and-set 写 `digested_by=<这一轮的任务键>`；下一轮看到 `digested_by` 指向的那一轮没有正常结束（不在 `GOOD_ENDS` 里，同 `take_snapshot` 的 `settled`）时再给一次。
- **交给新开的那件事**：新开的事的第一轮在“此刻另外几件事在做”后面加同一块，不标记已读（管家自己还没读到）；分叉的会话已读过的（`digested_by` 那一轮正常结束了）不重复。
- **压缩**：摘要只在槽 0 做（槽 ≥1 不压缩）；`COMPACT_INSTRUCTIONS` 不变。这些事的内容只有被管家一轮读进之后才进它的记忆，压缩把它一并压进摘要；压缩后的那一轮照常拿到还没给过的账本条目（`tests/test_track_ledger.py`）。
- **维护**：账本保留 7 天（`Temporary` 规则的 `main-thread/tracks`）；这些事用过的 Claude 会话文件按账本里的 `session_id`，那一轮结束满 3 天且 3 天没改没读的删（`TrackSessions`，只动 `CLAUDE_CONFIG_DIR`（默认 `~/.claude`）下 `projects/*/<session_id>.jsonl`）。
- **开关**：`COVEY_BUTLER_TRACKS` 关着不写账本、不加这一块。

管家只用 Claude Code（`app/butler.py` 的 `HARNESS`）。设置卡选的是管家、但 Claude Code 没启用时，私聊消息照旧开话题，Bridge 启动日志写一行 `main thread is on but its Harness claude is not enabled`。

### 提问不开串

话题挂在提问下面时管家这一轮默认不发卡（`(AREA, BUTLER)` 格子的 `Row.quiet`）。`placement.quiet` 还要求消息是主人自己的（`om_` 开头）或回答问题后的续接一轮（`CONTINUATION`）。

- **入队**：`app/submission.py` 设 `job.card_deferred`，不发卡；主人那条消息加表情 OnIt（`app/butler_reactions.py`，`Messenger.react`）。
- **卡什么时候出**：跑过 `CARD_AFTER`（90 秒）还没结束，任务进程发卡（`Cards.reveal`），进主人那句话的话题串。只试一次。
- **结束表情**（`butler_reply.react_end`）：DONE（自己答的）、Get（交给话题）、CrossMark（失败、被拒）；停止和等回答只去掉 OnIt。失败只记日志。
- **答案**：聊天区新消息，带“你问”块。只做了交接的一轮（`handed_quietly`）不发答案，Get 就是回执；转给了已有话题（topic_send）的，再在主人那条消息下回一个小卡（`hook_topics`，`cards/answer_card.py` 的 `hook_card`）：每个话题一行“→ T453「标题」 ›”，点了打开那个话题串、定位到转入的那条（`hook_rows`，转交记录的 `thread_message_id`）。它是引用主人消息的顶层回复，不开串（D1）；只挂一个话题时，引用这张小卡说话进那个话题。新开或分叉的话题串本来就挂在主人那条消息下面，不列。
- **没卡时的失败**：发一条带“你问”的短消息（`app/quiet_delivery.py` 的 `top_notice`），被拒或停止时列出收下的补充；答案没发出去时另发一条带按钮的（`undelivered_notice`）。
- **问题卡**：还没有卡时，问题卡本身作为聊天区新消息发出（`QuestionDisplay.ask_block_for`）。
- **重试、续跑、续接**：原来没卡的同样延后发卡，表情落在主人原来的消息上（`reaction_message`，`placement.owner_message`）；回答问题后的续接一轮不加表情（`placement.marked` 只认 `om_`）。

### 一次请求一张卡

`app/butler_reply.py`。回答先照旧经 `verified_reply` 保存、授权（规则 5）。

- **开了或转了话题**（`main-thread/opened/` 里这一轮成功的记录，`core/butler_opened.py` 的 `kind_of`）：写在任务卡上，每个话题一行（“↪ 开了 **T405**「标题」”“↪ 转给 **T403**「标题」：note”），模型的话折进“详情”。
- **自己答的**：按放置表 `answer` 作为聊天区新消息发（`app/answer_delivery.py`）。
- **退回单独发**：没完成、要以文件发、超过卡片字数或写卡失败时，单独发回答卡（重投只发保存的正文）。
- **状态行**（`outcome_words`，`Cards.say_outcome`）：“✅ 已答：开头”“↪ 开了 T444「标题」”“↪ 交给 T442 接着做”“↪ 补充给 T444（它正在跑）”“↪ 分叉出 T445「标题」”“↪ 拆成 2 件”。
- **停止和被拒**：卡上列出收下的补充，说明不会自动重发；官方拒绝时写在 `app/policy_guard.py` 重画的恢复卡上。

**分档卡**（`slim_card`）：没有标题栏，正文、一行状态（`cards/task_status.py` 的 `slim_status_markdown`），其余折进默认收起的“详情”（`slim_details`）；样子见[卡片](cards.md)。

## 会话

`app/butler.py`，只在开关开着、任务属于管家话题时起作用。

- **回复位置**：`Butler.configure` 在 `build_context` 里（Bridge 和任务进程都走）给 `feishu/message_request.py` 的 `reply_in_thread` 设判断：对管家消息的回复进那条消息的话题串。开话题就用这个串。
- **说法**：`worded` 按 `WORDING` 改 Bridge 自己写的文字（“本话题” → “私聊里”），模型的回答和主人原话不动。
- **快照**：紧接规则，`snapshot_groups` 从 `core/board_read.py` 读（不调飞书，不持记录锁）。四组：等你回答、有结果未看（`result`，看板的口径，见[看板与 Pin](#看板与-pin)；等了一天以上的问题和没看的结果可以同时在两组）、进行中、管家开的话题。存到 `topics/<id>/main-snapshot.json`，下一轮只列变了的行；新会话、压缩过（`compacted.json` 更新）、上一轮没正常结束时带全量。检查点有 `steps` 时写第几步和本步用时。看板撤下的话题不进前三组。
- **MCP 配置**：不改 `runs/` 协议。`butler_turn` 算出 `mcp_config(...)` 放进 `ctx.options["mcp_config"]`；`backends/claude/runtime.py` 据此加 `--mcp-config`、`--allowedTools mcp__bridge`，并设 `ENABLE_TOOL_SEARCH=false`（`turn_env`），让工具随会话加载（否则模型会发出不带参数的调用）。不加 `--strict-mcp-config`。所有后端都不走系统级，规则和原则都在 `RunRequest.prompt` 里。
- **压缩**：一轮送达后上下文到了 `compact_tokens`（默认 20 万，0 不压），`compact_butler` 写压缩请求（`Compaction.set_requested`）。`/compact` 带 `COMPACT_INSTRUCTIONS`：摘要约 2000 token 以内，只留待办、话题、未决问题和偏好。话题没有自动阈值。
- **空闲压缩**（`app/idle_compaction.py`）：任务进程退出后（`finish_detached` 末尾调 `after_runner`）等 `IDLE_SECONDS`（90 秒）；那时这一轮是 `done`、话题里没有任务、请求没标 `failed`，就起一个只压缩的任务进程（`covey_runner.py --compact-only`），标记写在 `<state>/main-thread/compacting/<话题>/`，不建任务、不出卡。成了写 `compacted.json`，没成标 `failed`、下一轮开头先压。压缩中主人发来新消息时，新一轮（`stop_for_turn`）停掉它、放回请求。上下文到阈值 `FALLBACK`（1.5）倍的、`failed` 的，在下一轮开头压缩（`Compaction.turn_context`）。Bridge 启动时（`Butler.settle`）清理遗留标记。

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
| `area` × `lead`（专项，默认关） | 否 | TOP | 无 | NONE | NONE | 是 | 否 | 否 | 是 |
| `area` × `project_member`（专项，默认关） | 按 `Traits` | THREAD | 无 | NONE | TOP | 是 | 否 | 是 | 否 |
| `topics` × 任意 | 否 | THREAD | 无 | NONE | NONE | 否 | 是 | 否 | 否 |

专项负责人的**唤醒轮**（消息 id 以 `lead-` 开头，`core/projects.py` 的 `is_wake`）也算安静轮（`quiet`）：不显示任务卡（`Cards.reveal` 对它不发）、没有表情；它的已授权回复默认不发（`TurnDelivery.silent`，见[架构 · 专项](architecture.md#专项第-2-期开发中默认关)的续轮与护栏），除非负责人在这一轮里记了一条按风格该报的 `report` 进展。

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
- **“你问”块**（`cards/answer_card.py` 的 `ask_block`）：灰底两行“你问 · 时间 ›”和原话，整块可点（`app/answer_cards.py` 的 `ask_url`）。按这一轮属于谁定跳到哪：话题的轮次（结论回带、问题卡副本，不论是开话题、转交、补充还是主人在话题里接着说的，`relay_targets.ask_for` 传话题的 topic_id）跳到那个话题根消息的话题串，定位到这一轮的卡（`topic_links.topic_thread_link`，回带传 `turn_card`，问题卡副本传问题卡）；管家自己做的轮次跳到主人那句话在聊天区的位置。话题根读不到话题串时退到根在聊天区的位置，什么都读不到时不给链接（不画 ›），两种都在日志记一行 `ask link fell back to the chat` / `ask link missing`。文字仍是主人原话：转交的轮次用管家接这件事的那句话。
- **“你答”块**：表单回答开的一轮写“你答 · 时间 ›”和“问题 → 回答”，链接规则和“你问”一样（管家自己的回答跳到提问那一轮的主人原话）。只有哪里都跳不了时才用默认收起的折叠（`answer_block`），展开是每道题和完整回答（`app/answers.py` 的 `answer_parts`）。

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

- `COVEY_PROJECTS=1` 时（Bridge 把它交给 MCP 服务，`core/butler.py` 的 `mcp_config`）：管家的清单多出 `projects_list`、`project_send`（`app/projects.py`），`topic_start`、`topic_send` 各多一个可选的 `project`。`topic_start` 带 `project: true` 不开话题，开一个专项（`Projects.start`：记录、负责人话题、专项卡、`start` 回执），`title` 是专项名，`prompt` 是目标和完成标准；`topic_send` 带 `project: true` 是管家判断这个话题要转成专项（主人要长期跟进、分成几个话题持续推进，或直接说“转成专项”；拿不准不带），`COVEY_PROJECT_UPGRADE` 不是 `off` 才生效。专项的话题不接 `topic_send`，回一句改用 `project_send`；`project_send` 的 `pid` 写 id、id 的开头或名字里的几个字，在跑的负责人那一轮收到补充，没有就开它的下一轮（消息 id `main-…`，算主人触发的一轮，不是自动唤醒）。关着时清单和原来一字不差。
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
  - Harness、模型、预算逐项取值（`resolve`）：调用给的 > `topic_defaults` > 这个私聊上次开始任务的配置（`app/launch.py` 的 `launch_defaults`）> Bridge 默认。上次的值已不可用时往下找；调用给的不可用时回错误并列出可选。主人点名用哪个 Harness、模型、思考预算时，工具说明和管家规则都要求照填（`harness` 写 id，也认显示名，如 Claude Code，不分大小写、忽略空格和连字符）；没点名就不填。
  - topic.json 写 `origin`（`kind: "main"`、`request`、`job_key`、`why`、`readonly`、`user_requested`、`title`）。
  - `starts_at_once` 为真时照分叉绑定配置（`workspace_locked`、`harness_locked`），第一轮直接提交；否则（或运行方式是独立 worktree）话题里出启动卡，预选在 `origin.launch`。
- **`topic_send`**：管家自己和代答话题不接。`text` 是任务说明，`note` 一句背景，都只给模型。
  - 有没结束的任务：作为补充交给最新那个（`deliver_steer`），话题串里出“管家转来你 HH:MM 的补充：”加主人原话。被拒（`SteerRefused`）、不确定（`SteerUncertain`）都照实回答，不重发（规则 6）。
  - 已经停了：串里出“管家转来你 HH:MM 的消息：”，再照话题里的消息处理（有没答的问题就作为回答，否则开下一轮）。还在等启动卡的话题不转。
  - 串里这条转入消息（`hand_line`）是一张灰底卡，整块可点，跳回主人那句话在聊天区的位置（`cards/answer_card.py` 的 `handoff_card`，链接同管家轮次的“你问”）；读不到那句话的位置时照旧发文字。卡的消息 ID 记在转交记录的 `thread_message_id`。
- **`topic_fork`**：从源话题最近一次结束的任务分叉（`app/forks.py` 的 `create_fork`），绑定源话题的配置和脱敏，复制原生会话。源话题在跑或排队就回错误。
- **话题卡**（`cards/topic_card.py`）：“T412 · 标题”加一行灰字配置，没有按钮，拿到 T 号后整卡更新一次。
- **幂等**：每次调用在 `<state>/main-thread/opened/<请求>.json` 记做到哪一步。发送键和第一轮的消息 id（`main-<请求>`）从请求 id 派生；同一个请求再来返回存下的回答。别处经 `core/butler_opened.py` 读。

### 交给话题的消息

`app/butler_handoff.py`。交给话题的每一轮（开、下一轮、补充、分叉），模型收到 `handoff_prompt`，卡、历史和串里显示主人原话（`shown`、`thread_line`）。

- **Bridge 的说明**（`note`，开头 `[Bridge 说明]`）：你在哪个话题；这件事是管家按主人哪条消息交来的；路由已经办完，直接做事；你的回复就是结论。
- **任务说明**：管家写的 `prompt`/`text`，后跟“管家补充：note”。
- **主人原话**（`asked`）：管家这一轮记录的 `user_text` 加运行中收到的补充，按时间引用。
- **附件**（`carry`）：只认管家话题 `incoming_media/` 或 `<state>/incoming_media/` 下的文件（`core/attachments.py` 的 `parse_attachment_block`），按参数 `attachments`（`all`、`none`、序号）挑出，硬链接或复制到目标话题的 `incoming_media/`。补充只送文字。

## 话题

管家派出去的每件事是一个话题：话题串挂在提出它的那条提问下面；同一件事用 `topic_send` 追加。

- **开**：管家开着、查得到提问、提问的别名还指向管家时（`butler_topics.place`，`core/topics.py` 的 `claim_area_root`），以这条提问为根：先把别名改给新话题（`move_topic_message`），再建上下文，不发话题卡。`opened/` 记 `area_root`、`area_taken`。
- **一条提问只挂一个串**：同一条提问的第二个话题、查不到提问时，根是顶层新发的话题卡；分叉挂不上时把源话题合并转发到私聊当根。第二个话题的“你问”仍写那条提问，链接打开它自己的串（`ask_for` 读 `area_taken`）。
- **引用直达**：管家这一轮的卡只交给了一个话题时，卡的别名记上 `routes_to`（`butler_relay.after_runner` 调 `route_message`）。回带的结论、问题卡副本也记成话题的消息并带 `routes_to`。引用它们就是对那个话题说话（`context_for_topic` 先看 `routes_to`），不经管家。纯话题模式下串里不看 `routes_to`。

## 结论回到顶层

管家开的或转过去的那一轮结束时，Bridge 在聊天区发一条新消息，顶上是“你问”块。只在管家开着时；不调模型。

- **哪些轮**（`relay_targets.relay_target`）：消息 id 是 `main-<请求>` 的任务、`topic_send` 作为补充送进的任务、它们的自动重试（`retry_of`）。话题里回答问题后接着跑的、主人在串里开的一轮也回带（`area_target`）。管家没碰过的话题不回带。
- **什么时候**：任务进程投递完、任务卡写完之后（`TurnDelivery.deliver` → `butler_relay.after_turn`），出错只记日志。

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

话题的问题在顶层也放一份同样的表单（`app/question_display.py` 的 `mirror`，卡 `cards/question_cards.py` 的 `mirror_card`、`top_level_card`），聊天区新消息、带“你问”。在哪张上答都一样，答了两张一起收起。

- **表单上面是这一轮的输出**：原生问题（`NativeQuestions.mirror`）是到这时的进展说明和回答；检查点问题（`after_turn`）是任务卡正文。超过 `MIRROR_OUTPUT_BYTES` 的先单独发一条。
- 只有一道有选项的单选题时，每个选项一个按钮，点了就提交（`picks`、`app/answers.py` 的 `picked_fields`）。
- **记录**：问题记录多 `mirror_state`（`creating` → `sent`/`uncertain`，锁内写、锁外发，规则 1）、`mirror_message_id`、`mirror_card_id`；别名记成话题的并带 `routes_to`。
- **一起收起**：`QuestionDisplay.paint` 也重画副本（`paint_mirror`，sequence 从映射 `reserve`，规则 3）。
- 副本没能发出时，原生问题回带一行（`relay_question`，记在 `main-thread/relays/questions/`）。

## 看板与 Pin

要 `COVEY_PINNED_TOPICS=1` 和 `COVEY_PINNED_OVERVIEW=1` 都开，由手上的事（`app/overview.py`）的循环带着跑（`app/pins.py`）。管家自己不进看板，它开的话题照常进。

- **读数**：`core/board_read.py` 的 `read` 一次读出私聊话题的问题、任务、已读和优先级，不持锁、不调飞书。看板、手上的事、早晚汇总、快照、`topics_list` 都从这里读。
- **有结果未看**（2026-11-10 起五处一个口径，以看板为准）：`core/board_read.py` 的 `groups` 按看板分组，`unseen` 取出没看过的结果，五处都经 `app/pins.py` 的 `board_results`（和看板的 `look` 同一份记录、同一套规则），同一时刻同一份数据得到同一组话题。
  - 哪些算结果：话题最新一个跑过的任务（排队时就取消的不算，`unrun`）完成、失败、中断或停止，并且有任务卡；话题正在等回答（一天内的问题）或在做时不算。
  - 怎样算看过：话题在结束之后读过（`seen_at`）；答案写在顶层卡上的，顶层在答案出现之后读过也算（`top_read`）。第一次看过的时间记在看板记录的 `results` 里，看过就一直算看过。
  - 没看过的不论多久都算，看板记录的 `results` 让 `read` 继续读到这个话题的旧任务。看板（两个开关都开，`laid`）开着时看板记录里的 `since`、`keep_since`、`stopped_since` 照旧生效；没开看板时没有记录，`read` 读到的（`RESULT_AGE`，3 天）结果都算没看过；以前开过看板留下的记录里撤下的话题（`dismissed`）也不算，什么都不过滤。
  - 组键都叫 `result`：快照和 `topics_list` 只列没看过的（文字“有结果未看”；`topics_list` 还认旧名 `unread`）；看板和手上的事的“有结果”另外留着看过不到 12 小时的，手上的事标“已看”并写“（未看 N）”；早晚汇总的“做完”在上次汇总之后结束的之外，还列出所有没看过的，其余标“已看”。各处的“最近 3 天”只用于 `topics_list` 的 recent 这类“最近动过”的列表，不再用来筛未看。
- **分组**（`GROUPS`）：等你回答 → 进行中 → 有结果；停止的也在“有结果”。
- **分组**（`GROUPS`）：等你回答 → 进行中 → 有结果；停止的也在“有结果”。
- **留多久**：没看过的一直留；看过的从第一次看过起留 `KEEP_SEEN`（12 小时）。“看过”看话题的已读时间（`seen_at`；已读事件 `app/topic_seen.py`、`core/topic_seen.py`）。
- **钉哪张卡**：话题挂在提问下面的布局 Pin 这一轮在串里的卡：等回答是问题卡，在做是任务卡，做完是答案卡（`answer_message_id`，`aimed`）。纯话题布局 Pin 状态卡（`status_pinned`，`app/status_cards.py`）。
- **优先和撤下**：见 `topic_priority`；撤下的话题有新问题或新结果时自己回来。状态词都来自 `core/turn_words.py`。

### 个人空间的管家与看板

`spaces.personal_groups` 里的群（和 `owner_accounts` 的私聊）各有自己的管家：话题 id 按 chat 算（`butler_topic_id(chat)`），记录不混；`Access.agent_chat` 判断哪个 chat 有管家，`ButlerEnv.chat()` 在工具调用里取这个管家的 chat。个人群没有已读回执，主人在群里发的消息算读过它的看板（`app/group_boards.py` 的 `owner_active`）。个人群各有一份手上的事、看板和早晚汇总，记录名带 chat_id：`pinned-topics.<chat_id>.json`、`pinned-overview.<chat_id>.json`、`digest.<chat_id>.json`（`app/pins.py` 的 `record_path`）；`notify_space` 那个 chat 用默认那一份。设置见 [配置的空间一节](configuration.md#空间spaces)。

## 主人的原则

Bridge 记一份主人的长期原则，所有 Harness 的话题和管家都带上。

- **存储**（`core/principles.py`，只用标准库）：`<state>/principles.json`，`{"version": 1, "owner": [...], "workspaces": {"<工作区>": [...]}}`；每条 `{"id": "p" + 6 位十六进制, "text", "at", "source"}`。一条最多 `TEXT_LIMIT`（200）字，每组最多 `GROUP_LIMIT`（30）条，满了拒绝。写入在 `shared_file_lock` 下整体替换，读不加锁；文件坏了当空的读，下次写前改名 `principles.json.bad-<epoch>`。
- **工具**：`principle_save` 的 `scope` 是 `owner` 或 `workspace`（要已登记的工作区）；正文相同的返回已有 id。`principle_delete`、`principles_list` 用 `delete`、`entries`。
- **注入**（`app/runs.py` 的 `Runs.principles`，块由 `app/prompts.py` 的 `principles_block` 写）：工作区那组总带；通用那组只在主人自己的一轮带（`Runs.owner_turn`：管家总是，其余经 `Access.owner_turn`，规则 11）。新会话在 `bridge_rules` 里，续接的在 `_with_rules` 补的提醒里，所以新原则下一轮就生效。总长约 `PRINCIPLES_CHARS`（2000）字，超了从旧的截。代答话题不带。

## 知识库

主人说“记一下”的东西集中存放，所有 Harness 的新会话都带上，管家能搜索和修改。`COVEY_KNOWLEDGE=1` 才开（[配置](configuration.md#知识条目与历史搜索)）；关着时不建目录、规则和提示词一字不变、工具清单里没有这些工具、汇总里没有这一节。

- **存储**（`core/knowledge.py`，只用标准库）：`<state>/knowledge/owner.jsonl`（通用）和 `workspaces/<工作区>.jsonl`，只追加的事件，一行一个：`add`；`update`（新 id，带 `replaces`，旧的变已取代）；`drop`（必须有 `reason`）。读的时候从头重放，得到每条的状态（当前、已取代、已作废）。同目录有生成的 `.md`（每组当前条目）和 `owner-brief.md`（和注入同样 `KNOWLEDGE_CHARS` = 1500 字的摘要）。写在文件的跨进程锁里追加一行，锁里不调飞书、不起子进程；生成的 `.md` 写不出不影响事件。读不了的文件当空，下一次写前改名 `*.bad-<epoch>`；不是事件的行跳过。
- **条目**：`kind` 是 decision、practice、pitfall、fact、question；正文最多 200 字，`why` 可空；`by` 是 main（管家的工具）、topic（话题里收集的）、import。同一组里正文去掉空白、大小写和句末标点后相同的不再新增，返回已有的 id。
- **管家**：见上面的工具清单（`app/butler_knowledge.py`）。规则里写：“记一下”调 `knowledge_save`（具体的事实、决定、坑；以后每次都要遵守的做法仍用 `principle_save`）；“把 T483 的结论记一下”先 `topic_read` 再起草；记完列出 id 和正文；“改成”“删了”用 `knowledge_update`、`knowledge_drop`；查以前的事按 `knowledge_search` → `topics_search` → `history_search`（`app/prompts.py` 的 `knowledge_rules`）。工作区名要已登记，不认识时返回可选的名字。
- **话题里说“记一下”**：规则里多一句（`REMEMBER_RULE`，只给主人自己的一轮、不是管家、不是代答、没开脱敏的话题）：只有主人这一轮明确说了才把要记的写进检查点的 `remember: [{kind, text, why, scope}]`，最多 5 条，并在回复里列出。收集在 Bridge 进程里（规则 8），`app/detached.py` 的 `finish_detached` 在任务进程退出之后（在进程内跑的任务在 `app/job_dispatch.py`）调 `app/knowledge_collect.py` 的 `collect`，同时满足才写：这一轮正常结束（`done` 或 `waiting_user`）、`owner_turn`、原话里有“记一下 / 记住 / 记下来”、不是管家转交的（`prompt_text` 非空）、不是代答也没开脱敏（配置读不出按开了处理）。不满足记 `knowledge remember ignored reason=…`；同一个任务键只写一次；整个收集包在 try 里，失败只记日志。
- **注入**（`app/knowledge_turn.py`，块由 `app/prompts.py` 的 `knowledge_block` 写，放在原则后面、检查点约定前面）：工作区那组谁的话题都带，通用那组只在 `owner_turn` 时带；从新到旧排，总长 1500 字，超出的截掉最旧的，末行写另有几条和 `.md` 在哪；新会话全量带，续接的会话只补话题上一轮结束以后新增或改动的（上一轮的结束时间从 `jobs/` 取，那一轮自己收集的不再补）；代答话题不带；没有条目整块不出现。
- **查历史**：设了 `COVEY_HISTORY_CMD`（绝对路径）时，主人自己的话题多一行“用 `<命令> search <一句话>`，不要自己 rg 状态目录或会话目录”（有没有条目都有，新会话和续接的每一轮都带），管家多 `history_search`。命令本身（轮次索引、混合检索）不在本仓库；`history_search` 在 `app/butler_knowledge.py`，自己的进程组里跑，70 秒超时整组杀，输出最多 20 KB，命令不存在、不能执行、超时、非零退出都回一句中文。管家的 MCP 服务靠 `core/butler.py` 的 `mcp_config` 交给它的这两个变量决定列不列这些工具（`core/butler_tool_specs.py` 的 `listed`）。
- **汇总**：`app/digest.py` 的 `knowledge_view` 在汇总卡和菜单「看板」多一节“新记的知识”：上次汇总以来新增、改写、作废的条目，最多 10 行（操作 · id · 组 · 正文前 40 字），多的写“另有 N 条”；没有变化没有这一节，它自己不会让汇总卡发出来。
- **回退**：去掉两个变量重新 `launch`；已经写下的 `knowledge/` 留着。

## 设置

`<state>/main-thread.json`，字段见 `Settings`：`mode`/`enabled`；管家的 `model`、`effort`、`redaction`、`workspace`、`compact_tokens`；新话题的 `topic_defaults`（`harness`、`model`、`effort`、`run_mode`、`redaction`、`workspace`）；子 Agent 用什么模型 `subagents`；直接开工 `start`、记住的 `trusted`、工作区用途 `workspace_notes`。缺的或坏的字段按默认值读。只经 `update` 写，只有设置卡能写。

- **管家的脱敏**：从下一轮起走敏感词中继；Bridge 每次交消息前写它的 `topic.json`（`keep_topic`），中继每轮读（`backends/claude/redaction.py`）。

### 设置卡

管家的设置是设置卡（`app/settings_hub.py`，菜单键 `bridge_settings`）的前两个标签，标签行见 `cards/settings_hub.py` 的 `LABELS`。画卡和点击在 `app/butler_panel.py`、`cards/butler_panel.py`。

| 页 | 内容 |
| --- | --- |
| 私聊 | 私聊模式；管家的供应商、模型、思考预算、脱敏、压缩阈值（Harness 固定 Claude Code） |
| 新话题 | Harness、供应商、模型、思考预算、运行方式、脱敏、工作区；子 Agent：轻量（Sonnet，搜索用 Haiku）/ 跟主模型；直接开工三档 |
| 工作区 | 工作区列表和每行的用途；记住的工作区，每项可删 |

- 不用下拉框：每行点开是选项，点一个就记下并整卡重画。可选项和启动卡同源（`cards/launch_config.py` 的 `tiers`、`budget_options`）。留空的项写实际会用什么（同 `resolve`）。
- **子 Agent** 在新话题页，但对整个机器人生效：Bridge 起的每一轮 Claude Code（话题和管家，连同轮前压缩）都照它。`subagents` 是 `light`（默认，没有这一项或值不认识也算）时，进程带 `CLAUDE_CODE_SUBAGENT_MODEL=sonnet`，再用 `--agents` 换掉内置的 Explore：只读（Read、Grep、Glob、Bash），跑在 Haiku 上，只找文件、搜代码、看日志，报路径、行号和摘录，不改东西、不下判断；改文件和拿主意还是主模型。这一轮的模型是 Haiku 时不设 `CLAUDE_CODE_SUBAGENT_MODEL`，子 Agent 不比主模型大。`same` 什么都不加，每个子 Agent 都跑主模型。`backends/claude/subagents.py` 读这个设置；`ctx.options` 的 `subagent_model` 有值时压过设置（空字符串是不设），也不加 Haiku 的 Explore，内置的 Explore 跟着它（安全拒绝后的重试）。旧版本的任务进程不受影响。部署不允许管家时没有这一页，按默认 `light`；要跟主模型，手动在 `main-thread.json` 里写 `"subagents": "same"`。
- 只有主人能改。旧卡的按钮照写：旧行按 `PAGE_OF` 落到对应页，下线的标签按 `RETIRED` 落到工作区页，更早的面板卡由 `SettingsHub.takeover` 接管。
- 旧菜单键 `butler_settings` 还认，发的也是设置卡；新卡发出后撤回上一张（`app/bot_menu.py` 的 `Panels`）。

## 话题链接

管家卡、回带卡上的 T 号是链接（`app/topic_links.py` 的 `linkify`），打开话题并停在一张卡上：回带卡上它自己的话题停在回带的那一轮的卡，别的停在最新的任务卡。

- 有话题串的用 applink `client/thread/open?…&thread_position=…`，读不到位置时只打开话题串。
- 位置由 `feishu/message_read.py` 的 `fetch_message_detail` 读一次，记在 `<state>/topic-links.json`，每个话题留 `KEPT`（8）条。
- 读不到保持纯文字；读消息在所有锁外（规则 1、2）；只改显示的文字，保存的回复（规则 5）不变。

### 派发卡（开关默认关）

`COVEY_DISPATCH_CARD=1` 时（见[配置 · 派发卡](configuration.md#派发卡)），管家派出话题的那一刻在聊天区多一张**总览卡**：它是聊天区里的一条新消息，顶上一块“你问 · 12:31 ›”（`ask_block`，点开是主人那条提问在聊天里的位置），不开串；话题照旧在原处、有串、有自己的任务卡。它不能做成对主人那条提问的回复：话题挂在那条提问下面，飞书对已经有话题串的消息，回复一律进串，`reply_in_thread=false` 也不管用（2026-11-15 验收看到卡落进串里）。关着时没有这个调用、没有后台线程，`hook_card` 照旧（`app/butler_topics.py` 的 `dispatch_card` 先看开关）。

- **内容**（`cards/dispatch_card.py` 的 `dispatch_card`）：`answer_ask` 顶上的“你问”块（做完缩成一行的卡不带，那一行自己写着请求）；卡头 = 主人请求的前 30 字 + 状态标签（在做、等你、做完、失败、已停）；`dp_meta` 一行灰字“T453 · 12:41 起 · 用时 2分05秒”；折叠面板 `dp_thread`（标题行 T 号 + 话题标题 + 工作区；在做和等你时展开，做完、失败、已停收起）里是当前步骤或阶段（读法同 `app/butler.py` 的 `snapshot_groups`：`step_text`、`phase`）；`dp_io` 输入/输出灰框（`cards/answer_card.py` 的 `box`）；`dp_btns` 里补充（`dp_steer`）、停止（`dp_stop`）、› 打开话题串（`dp_open`），能用的才画：补充和停止只在“在做”且已有任务时，等你时回答在话题里的问题卡上。卡里**没有输入框**（会刷新的卡不放输入框，docs/cards.md 卡片的客户端限制）。
- **创建**（`app/dispatch_cards.py` 的 `created`）：话题接了活（`topic_start` 开完并提交第一轮、`topic_send` 送出或开了下一轮、`topic_fork`）之后，发一条聊天区的新消息（不带 `reply_to`；“你问”的时间和链接读一次飞书，记在记录的 `ask_at`、`ask_url` 里，读不到就没有链接）；记 `route_message`，主人引用这张卡说话进那个话题。同一个请求只发一张（记录里有 `card_message_id`）。发不出去只记日志，话题已经开了。
- **记录**：`<state>/main-thread/dispatch/<token>.json`（`core/dispatch_records.py`）：`token`（请求的）、`topic_id`、`job_key`（管家那一轮）、`mid`（话题里这一轮的任务键）、`owner_message`、`card_id`、`card_message_id`、`sequence`、`state`、`started_at`，以及留给专项用的 `rounds`（同一条提问再次派给同一个话题的次数）、`upgrade_hint`（管家转话时带了 `project` 才有 `project: true`；旧记录里的 `long_word` 不再写也不再读）、`project_id: null`。`state` 只有 `live → settled`（`DISPATCH_TRANSITIONS`），`settled` 之后后台线程不再看它。
- **刷新**：`DispatchCards` 的后台线程每秒看一遍没结束的记录，只读话题的任务记录（`find_job_record`、`topic_job_records`，没有飞书调用，不拿锁），画出整张卡，算摘要；摘要变了且离上次写至少 2.5 秒（`MIN_GAP`）才写一次整卡。写之前从 `reserve` 取 sequence 存进记录，失败了按 2、4、8 … 最多 60 秒退避，下次取新的 sequence，从不重发同一个。运行时进程不碰这张卡，Bridge 里只有这个后台线程写它。话题链接（›）读一次飞书、记在 `topic-links.json`，读不到 30 秒后再试。
- **结论沉底**（B8）：回带发出后，`app/relay_send.py` 的 `send_once` 调 `dispatch_note`，在运行时进程里只留一张小便条 `<token>.relay.json`（`mark_relayed`，不碰卡）；后台线程读到便条，把卡整卡换成一行“✅ 做完 · 结论在下方 ›”（链到结论消息，面板收起、按钮摘掉），写成功后记录转 `settled`。失败或停止同样写一行原因（主人自己停的话回带被跳过，也是这一行“⏹ 已停”）。回带发送 `uncertain` 或 `unsent` 时不留便条，卡原样留着让主人看到出了问题；等回答的回带（`WAITING`）也不留。任务结束后 10 分钟（`TERMINAL_GRACE`）还没有便条，卡就按最后的样子转 `settled`；3 天还没结束的也一样。
- **按钮**：`bridge_dispatch_steer`、`bridge_dispatch_stop` 在 `ACTIONS` 里（`handler` 是 `steering.dispatch_steer`、`controls.dispatch_stop`，不是 `bot_only`）。卡的消息记在话题名下，点击按话题路由；按钮的值带 `topic_id` 和 `job_id`，但点击那一刻 `card_routing` 先按话题现在的状态换成**当前在跑（或排队）的最新一轮**（`dispatch_cards.click_job`：先看内存里没结束的任务，再看任务记录），这一轮结束后又开了新一轮（回答问题、补充）也不会落空；话题没有在做的任务时回一句“这个话题现在没有在做的任务”，不转给任何任务进程。话题的任务进程活着时点击转给它，和任务卡上的补充、停止一样；任务进程里收到的点击不再换。补充另发一张不刷新的补充卡（`steer_form_card`），停止走 `cancel_reply_job_from_card`。

### 专项卡（开关默认关）

`COVEY_PROJECTS=1` 时，每个专项有一张卡：聊天区的一条新消息（不回复主人那条消息：飞书对已有串的消息，回复一律进串；主人那句话直接开的专项，顶上带“你问”块），不开串。点击和引用它回复都按消息别名路由到专项的负责人话题（`route_message`），所以按钮是负责人话题的点击。**只有 `ProjectCards`（`app/project_cards.py`）写它**（规则 3）。

- **内容**（`cards/project_card.py` 的 `project_card`，element_id 都不超过 20 个字符）：卡头 = 专项名 + 状态标签（在做、等你、停住、做完）+ 方向灰标签（读记录的 `direction`，没有就不画）；`pj_need`“需要你”（摘要 + 打开问题卡 ›）、`pj_meta`（话题数、用时、token）、`pj_lead`（负责人现状 + 最近一句 note）；话题折叠面板 `pt_<话题前 6 位>_<状态字母>`：在做和等你的展开、做完的收起，**状态变了元素 id 就换**（客户端对同一个 id 改 `expanded` 不一定重画），等你的排最前、做完的最后，最多 8 个、其余一行 `pj_more`“更早 N 条”；`pj_triage`、`pj_mem`、`pj_lib` 三个收起的面板（分流记录、专项记忆、资料库，空就不画，后两个每行一个“删”）；`pj_io` 灰框（输入是负责人自己的 Harness、模型、思考预算：取它最新任务，还没有任务时取它 topic.json 的绑定，不取话题的；各话题用的 Harness 写在自己折叠面板的末行）；`pj_btns`：补充、停止专项、不要专项（停住的没有停止，做完的没有按钮）。没有输入框：会重画的卡不放输入框，补充另发一张表单卡。体积 5–13 KB（`tests/golden/cards.json.xz` 的 `project.*` 样本）。
- **读什么**：只读任务记录和问题（没有飞书调用，不拿锁）。话题的状态和一句话沿用派发卡（`app/dispatch_cards.py` 的 `status_of`）；记录字段 `direction`（P2-8）、`lead_note`（`{text, at}` 或字符串，P2-2）、`tokens_used`（整数，P2-2）、`conclusion_message_id`（负责人收尾报告的消息，P2-2）、`triage`（行或 `{at, text}`）、专项记忆（`core/project_memory.py`：`projects/<id>/memory.json` 的 `{"entries": […]}`，也读旧版留在专项记录 `memory` 字段里的条目；删掉的和风格行不画；卡上的“删”写墓碑，不真删）、`projects/<id>/library/`（文件名）；空的不画。
- **刷新**：后台线程每秒看一遍，摘要变了且离上次写至少 2.5 秒才整卡 PUT；每次写前从 `reserve` 取 sequence 存进 `record["card"]`，失败按 2、4、8 … 最多 60 秒退避。摘要也存在记录里，重启不会为同一张卡再写一次。
- **做完**：专项 `done` 后卡缩成一行（`settled_card`，链到那条结论）。结论是 `conclusion_message_id`，没有就看收尾的那一轮（记录的 `state_job`，`project_finish` 写的；`finishing_answer`）：它结束了且在顶层发了回答，链到那条；结束了还没有回答的 id，再等 30 秒（`ANSWER_WAIT`：id 比状态晚一会儿写进记录），仍没有就缩、不带链接（安静的唤醒轮不发回复）；还在跑就等它，最多 10 分钟。`cancelled` 缩成“⏹ 专项已降为普通话题”。缩完记 `card.closed`，不再刷新。
- **14 天翻页**：CardKit 的卡实体 14 天后不能改。卡龄过第 12 天（`FLIP_AFTER`）新发一张卡（一条新消息，键里带代数；发不出去下次用同一个 `pending_card_id` 重试），记录、别名、Pin 改指新卡（`aimed` 读记录），旧卡下一次看时缩成“已移到新卡 ›”（用它自己的 sequence），写不了 3 次就撤回。
- **按钮**：`bridge_project_steer`、`_stop`、`_downgrade`、`_mem_del`、`_lib_del` 在 `ACTIONS` 里（`handler` 是 `steering.project_steer`、`controls.project_stop`、`controls.project_downgrade`、`controls.project_mem_del`、`controls.project_lib_del`），都不是 `bot_only`：没有一个新建任务，值带 `project_id` 和负责人的 `topic_id`，不带 `job_id`，所以任务进程不会收到。补充 = 负责人正在跑的那一轮的补充表单（`steer_action`）；负责人空闲时不新开一轮，回一句“引用这张卡回复”（引用会路由到负责人，走管家的正常入口）。停止专项 = 记录转 `paused`（停住）并停掉负责人和各话题里排队或在跑的任务（`project_cards.stop_task`：任务在活着的任务进程里，就把它卡上“停止”的点击转给任务进程，像管家同时做几件事和代答的停止那样；点击写的是话题自己的别名 `project-stop-<话题>`，没有任务卡的安静轮也路由得到。只改 Bridge 里那份任务对象，任务进程收不到，accept8 时话题里的命令照样跑完；“不要专项”停负责人也走这里）。不要专项调 `app/project_cards.py` 的 `downgrade`（降级，见下）。删记忆、删资料只删自己那一行（资料只认 `library/` 里的文件名，不认路径）。
- **转成专项的卡**：派发卡转成专项（[架构 · 专项](architecture.md#专项第-2-期开发中默认关)）时还是同一条消息、同一个 `card_id`，下一次刷新整卡换成专项卡，`pj_meta` 末尾多一句“由管家转成专项”或“来回 3 轮后转成专项”（读记录 `origin.note`；2026-11-16 以前写的旧说法按新说法显示，记录不改）；同话题其余还活着的派发卡写成一行“已并入专项 ›”（`merged_card`）。
- **不要专项**：点了之后专项记 `cancelled`，卡缩成“⏹ 专项已降为普通话题”，负责人在跑的任务停掉，专项的话题变回普通话题，这条卡和并入的那几行以后被引用回复，回到原话题（见架构里的降级）。
- **Pin**：看板的格是专项（`core/board_read.py` 的 `cells`）；在做钉专项卡，等你钉话题的问题卡，做完钉结束的那个话题的答案卡（没有就钉专项卡）；专项还没有卡时钉话题自己的卡。手上的事和早晚汇总的行用 `label_title`，格叫“专项 · 专项名”。

### 专项记忆与资料库（开关默认关）

**记忆**不受开关影响：负责人的 `project_remember`、`project_style`、状态包、专项卡和卡上的“删”都经 `core/project_memory.py`，读写同一个文件 `projects/<id>/memory.json`（以前工具写进专项记录、卡读这个文件，所以卡上永远看不到记忆）。旧版留在专项记录 `memory` 字段里的条目照常读出来、和文件合并显示，下一次写入搬进文件，之后不再写那个字段。

`COVEY_PROJECTS=1` 且 `COVEY_PROJECT_MEMORY=1` 时再多三件事：

- **话题开工带上专项记忆**：专项的话题（不含负责人，它有自己的状态包）的新会话，规则里在原则之后多一块“专项记忆”（至多 3000 字，超了先丢最旧的），后面再跟资料库索引（至多 2 KB）；续接的会话每一轮的提醒里只带记忆（`app/prompts.py` 的 `project_block`，`app/runs.py` 的 `bridge_rules`、`_with_rules`，内容由 `app/project_context.py` 的 `block` 取）。专项取消后、代答话题、普通话题都不带。
- **主人答问题卡记一条决定**：专项的话题或负责人发出的问题卡，主人提交回答后（`app/questions.py` 的 `_submit_answer`），记一条 `kind=decision`、`source=owner_answer`：“主人答：题目→回答”（每题题目至多 70 字、回答至多 50 字，合起来不超过 200 字）。反馈、全部跳过的不记。
- **资料库**：`projects/<id>/library/` 放文件，`library.json` 放索引（`core/project_library.py`）。来源一，主人回复或引用专项卡带的文件：消息进负责人话题时（`app/app.py`，经 `project_context.owner_files`）复制进来、记 sha256（同内容只一条；单个文件超过 25 MiB 或总量超过 200 MiB 的不复制，只记它原来的路径）。来源二，专项的话题每轮的产物（检查点的 `artifacts`）和 `delivery`：Bridge 在任务进程结束后（`project_loop.after_runner`，规则 8：任务进程不写专项的东西）只记路径、链接或一行交付，不复制。索引不超过 2 KB（最新的在前，超了写“另有 N 条”），进两处：负责人的状态包（“资料库”）和专项的话题的第一轮。设备上的话题读不到 hub 本机的文件，这一块留给以后的多设备文件传输。专项卡的“资料库”折叠面板列索引，每行有“删”（删索引、同时删复制的文件）；索引没有登记的旧文件照旧按文件名列、按文件名删。

维护（`core/maintenance_rules.py`）：专项做完 30 天后删资料库里的文件、索引留着；记忆条目 180 天；单个专项的文件不超过 200 MiB；`projects/` 不进备份。

### 专项的问题分流（开关默认关）

`COVEY_PROJECTS=1` 且 `COVEY_PROJECT_TRIAGE=1` 时，专项的话题问的选择题先交给负责人（第 2 期 P2-5；`app/triage.py`、`core/triage.py`、`core/project_gates.py`）。关着时问题卡的发法、重投、恢复和以前一样，问题记录里没有 `triage` 字段，负责人没有那两个工具。

- **谁交给负责人**：以下全部成立才交：开关开着；问题是专项的话题（`topic.json` 的 `project.role` 是 `thread`）问的，专项是 `active`；问题挡着这一轮（`blocking`）；**每一道题都有选项**（有一道自由文字就直接给主人）；题目、标题、选项文字里没有硬闸词。`Triage.route(ctx, job, record)` 在两个发卡的口子前调：原生的活问题（`NativeQuestions.show` 的 `live_question` 分支）和检查点、暂停的问题（`CheckpointQuestions.deliver`）；它出任何错都按“给主人”走。
- **扣住是什么样**：问题记录加 `triage: {state: held, at, deadline: 现在+180 秒, project, receipt}`，`status` 仍是 `pending`，`display_pending` 仍是真，所以所有“待回答的问题”的列表都看不到它，也没有主人能点的卡；任务记录的 `question_delivery_ok` 是真，所以 `undelivered_jobs` 不重投，恢复卡和没扣住时一样（实验 E2，见下）。话题的任务卡上那一行写“已交负责人”（`question_element` 对扣住的问题画的）。`state` 只有 `held → answered_by_lead | escalated`（`core/triage.py` 的 `TRIAGE_TRANSITIONS`），都是终态。
- **回执和 3 分钟**：任务进程只写自己这道题的记录（规则 8）。Bridge 的 `ProjectLoop` 每 10 秒扫一遍扣住的问题（`app/triage.py` 的 `sweep`）：没有回执的，往专项收件箱写一条 `question` 回执（问了什么、从哪个话题），这种回执不用 `COVEY_PROJECT_AUTOWAKE` 也唤醒负责人；到了 `deadline`、专项不再 `active` 的，发给主人。负责人的状态包里“交给你的话题问题”一段有每道题的 id、题目、选项和还剩几秒（`app/triage_view.py`）。
- **负责人的两个工具**（只在开关开着时列给负责人）：`project_answer(thread, answers)` 每题都要答，答案是选项原文，多选给数组；走 `Questions.submit_answer`，效果和主人点“提交回答”一样，话题照常续接，补充说明是“（负责人按专项记忆替主人答的）”。提交前先把 `display_pending` 记成假并写 `answering`（超时的扫描让它 90 秒）；提交失败就把问题转给主人。`project_escalate(thread, why)`：问题卡马上按原来的口子发给主人，卡上多一行“负责人转给你的理由：…”，任务卡上的那一行换成“转给你了”；卡发不出去时任务记录记 `question_delivery_ok: false`，主人那边是平常的“重新投递”。
- **硬闸**（`core/project_gates.py`，只用标准库）：一份词表，查题目、标题、选项的标签和说明：部署、发布、上线、下线、发给、对外、转账、付费、付款、购买、充值、花钱、额度、扣费、删除、清空、覆盖原/掉/现有/画板/文档、停用、排空、批一份、不可逆、无法恢复、强制、生产环境、线上环境/服务、到线上、密钥、密码、令牌，和英文整词 `rm`、`force`、`deploy`、`release`、`publish`、`delete`、`drop`、`truncate`、`purge`、`wipe`、`destroy`、`prod`、`production`、`sudo`、`password`、`secret`、`api key`、`payment`、`billing`。词表宁多勿漏：多一次命中只是主人多看一张本来就会看到的卡。
- **主人看得到**：负责人替主人答的每一条、每一次转给主人，都是专项卡“分流记录”里的一行（记录的 `triage` 列表，最多 40 条）；答的那行带一个“不对”按钮（`bridge_project_triage_wrong`，不是 `bot_only`）。点了发一张只有一个输入框的小表单卡（专项卡会重画，不放输入框；`cards/triage_card.py`），提交（`bridge_project_triage_fix`，`bot_only`：它在话题里排一条消息）后：这句话作为一条消息排进那个话题（消息 id `triage-fix-<题的 token>`，不是唤醒轮），写进专项的决定日志（负责人每一轮看最近 10 条），那一行记 `corrected`，之后显示“已纠正”。纠正不进专项记忆（记忆是 mem 一路在改的，纠正由负责人看到决定日志后用 `project_remember` 记）。
- **实验 E2**（扣住的问题会不会被当成“没投出去”）：`undelivered_jobs` 看任务记录的 `delivery_ok`、`question_delivery_ok`、`checkpoint_question_error`，扣住时 `deliver` 照样返回真、任务记录的 `question_delivery_ok` 是真，所以不重投；恢复判定 `core/recovery.py` 的 `classify` 只读任务记录，结果和问题卡已发出的一样。**Bridge 重启**：`recover_question_delivery` 原来把“有 `display_pending` 的检查点问题”当成没送出去，把任务改成 `failed`、`question_delivery_ok: false`，所以这里加了一条：扣住的问题跳过（重启后由 `sweep` 照常接着数 180 秒）。**任务进程**：问题记录是它自己的文件，`route` 只写这一个文件；扣住时任务进程不发卡也不碰收件箱，活问题的任务进程照常等答案，答案经 Bridge 的 `submit_answer` 转给它。`tests/test_project_triage.py` 的 `HeldDeliveryTest`、`HeldLiveQuestionTest` 逐条验证。
- **实验 E4**（词表回放）：用现网近 30 天的 1162 条问题记录的文字回放（只读，文中只写计数）：755 条全是选择题、407 条有自由文字的；词表第一版（含“线上”“token”“预算”“清理”“回滚”“覆盖”）命中 358 条，抽 45 条人工看约 20% 是误报（`token` 多是模型 token、`清理`多是“清理阶段”、`线上` 多是“线上已有 3 次”）；去掉这几个、加上漏报里看到的“停用”“排空”“批一份”“覆盖画板”后命中 301 条（选择题的 40%），其余 454 条会交给负责人；对这 454 条抽 40 条人工看，约 3 条（7%）该是主人的，补进词表后剩下的靠负责人自己判断、3 分钟超时和“不对”兜底。

### 专项停住后问主人（开关默认关）

`COVEY_PROJECT_ASK=1` 时，护栏停住专项（`state_reason` 是 `tokens`、`wakes`、`stalled`，见[架构 · 专项](architecture.md#专项第-2-期开发中默认关)），`ProjectLoop.pause` 在同一次写记录里加一份 `pause_ask`：`{reason, why, at, choice, halted, card}`；`ProjectCards`（`ask`）看到它，在专项卡所在的位置（聊天区的一条新消息，不回复，别名路由到负责人话题）发一张**停住卡**（`cards/project_card.py` 的 `ask_card`）：卡头专项名 + 橙色“停住”标签；`pa_why` 用一句话写为什么停（`project_loop.pause_words`：“token 用到上限 1000 万”“负责人连续叫醒 30 次没有你的消息”“负责人一小时内叫醒了 12 次”“62 分钟没有进展”）；`pa_note` 写在做的话题怎么样了；三个按钮 `pa_go` 继续、`pa_wrap` 收尾、`pa_stop` 停。

- **三个按钮**：`bridge_project_ask_continue`、`_finish`（`bot_only`：它们让负责人再跑一轮，是 Bridge 自己的安排，不转给任务进程）、`_stop`（不是 `bot_only`，和停止专项一样），`handler` 都是 `controls.project_ask`（`project_cards.ask_choice`）。点击先在记录锁里记选择（`project_loop.answer_pause`：只有专项还因这个原因停着、这张卡是当前这次停住的卡、还没选过才算数；否则回一句“选择已经做过了”“这是之前一次停住的卡”等，什么都不改），再做事：
  - **继续**：专项回到 `active`，唤醒计数清零（和主人自己在负责人话题里说话一样，`project_loop.reopen`）；`tokens` 停住再给一份预算（`token_floor`）；给负责人一条 `owner_choice` 回执，它永远唤醒负责人。
  - **收尾**：同样把专项重新开起来（否则没有人叫醒负责人），回执要负责人总结做完了什么、还有什么没做完，用 `project_note` 标 report 告诉主人，然后调 `project_finish`（还有话题在做时它会拒绝，等它们结束再收尾）。
  - **停**：记录仍是 `paused`，`state_reason` 改成“主人停止”，并停掉负责人和各话题里排队或在跑的任务（`project_cards.stop_tasks`，和停止专项同一个做法）；`tokens` 停住后选停的，之后主人在负责人话题里说话把它开起来时，照样给一份新预算（`token_floor`），不会马上又停。
- **在做的话题**：`tokens` 停住时 `ProjectLoop` 同时请专项的话题里排队、在跑的任务停下（`stop_tasks`，把卡上“停止”的点击转给任务进程），数记进 `pause_ask.halted`，卡上写“在做的 N 个话题已请它们停下”；`wakes` 停住不停它们，卡上写“不受影响”；`stalled` 本来就没有。
- **点了以后**：`ProjectCards` 把这张卡整卡写成一行（“你选了继续 / 收尾 / 停”），专项卡下一次刷新自然变成在做或停住。专项被别处处理了（主人在负责人话题里直接说话、负责人收尾）而没点，这张卡缩成“已在别处处理”。每张卡的写入用它自己的 sequence（`pause_ask.card`），不会和专项卡的混。
- **试用**：`COVEY_PROJECT_TOKENS=<小数>` 一会儿就停住；`COVEY_PROJECT_STALL_SECONDS=60` 并开 `COVEY_PROJECT_AUTOWAKE=1`，一个没有任务的专项约两分钟后停住。唤醒次数的上限不能调。

### 汇报风格的默认值（开关默认关）

`COVEY_PROJECT_STYLE_KEEP=1` 时，负责人用 `project_style` 改风格时，整份风格（`report`、`detail`、`spawn`）也记进 `<state>/project-style.json`；之后转成专项、直接开的新专项的记录从这份风格开始（`Projects.seed_style`），负责人的规则和状态包里就是它。已有的专项不动，负责人随时还能改。

### 方向标签（开关默认关）

`COVEY_DIRECTIONS=1` 时：主人的事分成几个方向（`<state>/directions.json`：`{"directions": [{name, aliases[], repos[], status}]}`，`core/directions.py`）；开着时派发卡和专项卡的卡头多一个灰色标签写方向，记录里写 `direction: {name, source}`（`source` 是 `owner`、`repo`、`butler`）。

- **谁说了算**，从高到低：主人明确说了（管家 `topic_start` 的 `direction` 带 `direction_by_owner: true`）；工作目录或仓库命中某个方向的 `repos`（绝对路径：目录在它下面；`owner/repo` 这样的名字：路径里有这几级目录）；管家开话题、开专项时填的 `direction`（从工具说明里列的已知方向里选，也可以起新名）；都没有就不显示。**只按工作区不判断**：工作区不会产生方向，只有方向自己的 `repos` 会命中。
- **新名先当候选**：第一次出现写成 `candidate`，第二次用到（另一次请求、另一个专项）变 `active`，只有 `active` 的才画标签；同一个请求重做不算第二次。方向的种子和别名由主人自己写进 `directions.json`。
- **在哪**：派发卡 `Dispatch.direction`、专项卡 `Project.direction`，都是卡头的 `text_tag`（`neutral`，最多 12 个字）；派发记录的 `direction` 转成专项时原样带过去（那一次使用已经记过）。卡头多一个标签，要真客户端验收。

## 代码位置

| 模块 | 职责 |
| --- | --- |
| `core/butler.py` | 开关、`butler_topic_id`、`Traits`、`worded`、`mcp_config`；只用标准库 |
| `core/butler_settings.py` | `Settings`、`update`、`settle_mode`、`starts_at_once` |
| `core/placement.py` | 放置表：`layout`、`plan`、`TABLE`、`quiet`、`marked` |
| `core/butler_tool_specs.py` | 工具清单 |
| `core/butler_requests.py`、`core/butler_mcp.py` | 请求通道、会话里的 MCP 服务 |
| `core/butler_opened.py` | 读 `main-thread/opened/`：`kind_of`、`answered` |
| `core/board_read.py` | 私聊话题的一次读和三种“未看”规则 |
| `core/turn_words.py` | 状态词、请求行 |
| `core/principles.py` | 主人的原则 |
| `core/knowledge.py` | 知识条目：存储、重放、搜索、生成的 `.md` |
| `app/knowledge_turn.py`、`app/knowledge_collect.py` | 一轮读哪些条目、话题里“记一下”的收集 |
| `app/butler_knowledge.py` | `knowledge_*` 和 `history_search` 工具 |
| `core/topics.py` | 别名与路由：`context_for_topic`、`route_message`、`move_topic_message`、`claim_area_root`、`topic_title` |
| `app/butler.py` | `Butler`：路由、回复位置、等主人的话、`butler_turn`、快照、压缩请求 |
| `app/idle_compaction.py` | 两轮之间的空闲压缩 |
| `app/butler_tools.py`、`app/butler_env.py` | `ButlerTools` 循环、`ButlerEnv`，查询、代答、原则、看板的工具 |
| `app/butler_search.py` | `topics_search`、预读后台线程 |
| `app/butler_topics.py`、`cards/topic_card.py` | 开话题、转交、分叉，话题的根，话题卡 |
| `app/butler_handoff.py` | 交给话题的那一轮 |
| `app/butler_reply.py` | 管家一轮在卡上的结束 |
| `app/butler_reactions.py` | 主人消息上的表情 |
| `app/quiet_delivery.py` | 没发卡的一轮的短消息 |
| `app/answer_delivery.py` | 单独发的答案卡和“你问”块 |
| `app/butler_relay.py` | 回带入口：`after_turn`、`after_runner`、`question_asked` |
| `app/relay_targets.py` | 回带到哪：`relay_target`、`area_target`、`ask_for` |
| `app/relay_send.py`、`cards/relay_card.py` | 只发一次（`send_once`）、回带卡 |
| `app/question_display.py` | 问题卡和顶层副本 |
| `app/dispatch_cards.py`、`cards/dispatch_card.py`、`core/dispatch_records.py` | 派发卡：创建、刷新后台线程、结论沉底、记录 |
| `app/answer_cards.py`、`cards/answer_card.py` | 答案卡的产物、“你问”块 |
| `app/pins.py`、`app/overview.py`、`app/status_cards.py` | 看板、手上的事、状态卡 |
| `app/topic_seen.py`、`core/topic_seen.py` | 已读事件和记录 |
| `app/topic_links.py` | T 号链接 |
| `app/butler_panel.py`、`cards/butler_panel.py` | 设置卡的管家页 |

## 前台话题（已下线，冻结）

`main-thread.json` 的 `desk_topics`（11-07）曾让管家开“前台话题”：有 T 号，没有话题卡和话题串。现在：

- `desk_topics` 读到 `true` 也按 `false`，启动时记一行 warning（`DESK_TOPICS_RETIRED`），保存设置时原值照留；设置卡上没有这一项。
- 已开的（登记在 `<state>/main-thread/desk/`）由 `core/migrate.py` 的 `desk-retire` 冻结：topic.json 加 `retired: "desk"`，原字段都留着（`core/topics.py` 的 `retired_topic`）。
- 冻结后不再进它：引用它旧卡的顶层消息交给管家（`Butler.app_for`），回复链的根是它的消息时也在管家里作为一条新的顶层消息处理（`Butler.handle_event`），管家不可用时按普通消息路由、只回一句；旧卡上任何按钮、被路由到它的消息只回一句 `RETIRED_TEXT`（`Topics.refuse_retired` → `retired_action`）；管家工具按 T 号找到它时说明已下线（`find_topic`、`question_answer`）。
- 旧记录里挂起的交接和回带读到也不用；`inline` 状态照旧算回带过（`butler_relay.relayed`）。

# 网页工作台

网页工作台是 Covey 的一个配套网页应用：私聊里的专项、话题、每一轮和等你回答的问题都在网页上看得到，也能在网页上说话、答题、停话题、开新话题、改设置。网页是主工作台，飞书管提醒和快速回复；两边读写的是同一份记录。

前半篇给使用者（是什么、怎么开、网页上能做什么），后半篇是数据契约，给写网页前端的人。

## 怎么打开

默认关，飞书里的功能不靠它。打开要三样东西，前两样问给你部署网页应用的人要：

- **网页应用**：Covey 不带网页，只按下文的[数据契约](#数据契约)把数据推给一个网页应用、从它那里取操作。网页应用要实现契约里的 `/sync/upsert`、`/sync/inbox` 等接口，可以自己按契约写，也可以用团队里已经搭好的（公司同事看扩展库 README 里有没有现成的地址）。每个主人一份网页应用，不和别人共用。
- **接口地址和密钥**：网页应用开放接口的根地址（形如 `https://<域名>/app/<应用 id>/openapi`）和调用它的密钥。
- **这台机器能访问那个地址**：Covey 不对外开端口，只主动去请求网页应用，所以机器上要能连上它（需要代理时和模型一样设在服务清单里）。
- **脱敏词表的原词会进网页应用的库**：这是有意的取舍，为的是能在网页设置页上批量编辑词表（替身词不推；飞书卡上只显示首字和字数），所以网页应用要部署在只有你能访问的地方，做不到就别开网页工作台。

步骤：

1. 把密钥写进一个只有你能读的文件，再在数据根里写一个 JSON（路径随意，例如 `/absolute/path/to/web.json`）：

   ```json
   {
     "base_url": "https://<域名>/app/<应用 id>/openapi",
     "key_file": "/absolute/path/to/web-openapi.key",
     "push_seconds": 1,
     "poll_seconds": 1
   }
   ```

   - `base_url` 不带末尾斜杠；`key_file` 是存密钥的文件，建议权限 600。
   - 密钥只从 `key_file` 读，不进日志、记录和报告；换了密钥文件，10 秒内自动读到。
   - `push_seconds`、`poll_seconds` 默认都是 1。文件缺了或写错，只记一行失败、退避重试。
2. 服务清单里设 `COVEY_WEB=1` 和 `COVEY_WEB_CONFIG=<上面那个文件>`，重新注册服务（见[配置 · 改开关三步](configuration.md#改开关三步)）。
3. **确认连上了**：一两分钟内网页上出现你私聊里最近的话题，“最后同步”的时间在走；在网页上对一个话题说一句，飞书的话题里出现这一轮。`bridge.log` 里没有 `web sync … failed`。
4. 关掉：设 `COVEY_WEB=0` 重新注册。网页上已有的内容留着，只是不再更新。

开着时 Bridge 里多两条后台线程：`web-push`（推）和 `web-inbox`（取网页上的操作并执行）；没开时没有线程、没有请求，行为和没有这个功能时一样。

### 连不上时

| 现象 | 先看 |
| --- | --- |
| 网页上一直没有内容 | `bridge.log` 的 `web sync push failed (N): …`：地址、密钥文件或网络不对；改好后不用重启，下一次重试就会推上去 |
| 网页上能看，点了没反应 | `web sync inbox failed` 是取不到操作；`web op <操作> failed` 是取到了但执行失败，网页上那条操作会显示失败原因 |
| 有的话题不更新 | `web sync push leaves topic … while its read is stuck`：读这个话题的记录卡住了（多半是网络盘），卡住的读结束后会自己补上 |
| 设置页是空的 | `web settings not built` / `not pushed`，其余同上 |

## 网页上能做什么

- **看**：每个专项（目标、话题、记忆、资料库、报告）、每个话题（状态、每一轮的你说和它答、执行过程、计划、用量、失败原因、图片），管家和专项负责人的对话也各是一个话题。状态色、“要你处理”“停住了”的算法和飞书的看板一致。
  - 管家、负责人和话题页的一轮画法一样：你说的话靠右、带浅色底（答题的那句是“问题 → 选择”的表），回答上面一行是谁和几点，再一颗“工具 N · 用时”点开看过程；话题页多一行“第 N 轮”当目录。电脑上对话区左边有一列刻度，一句你说的话（话题页是一轮）一条，鼠标停上去看那句和回答的开头，点一下跳过去（收起的更早几轮会展开），也能用上下键和回车。
- **说话**：在话题里发一句，话题在跑就是补充，没在跑就开下一轮；对管家说等于在私聊顶层发消息；对负责人说等于引用专项卡回复。
  - 输入框里 Enter 换行、⌘/Ctrl+Enter 发送，能放大、续写列表、预览 Markdown。
  - 加号或粘贴、拖进来可以带图片和文件（每个不超过 20 MB，一句最多 9 个），和在飞书里发附件一样交给模型。
- **换配置**：话题页输入框下面那行“工作区 · Harness · 模型”点开就是飞书的 🔁 换配置（Harness、模型、思考预算、工作模式、脱敏、工作区）。话题在做时不能换，先停止；要新建独立 worktree 的去飞书换配置卡上做。换完飞书话题里也有一条“已换配置”。
- **答题**：和飞书问题卡一样：点选项，或选项都不合适时自己写；某道题可以选“不回答此题”；也可以只发补充说明不答、先问一句再答，或点“不处理”先放下（话题下一轮前可撤销）。管家自己的问题只能在飞书里答。
- **停、重开、记为看过、不管了**：和飞书卡片上的同名按钮做同一件事。
- **开新话题**：选项和启动卡一样（模型、思考预算、工作模式、脱敏、工作区、运行方式），直接开工；要独立 worktree 的话题会在飞书里出一张填好的启动卡，点开始。
- **设置**：设置卡的各页都能看，第一页和卡上一样是“配置”，分新话题、管家、独立复核（开了独立复核才有）、更多几段；卡片一次只展开一段，网页各段都展开。能改的有新话题的每一行，管家的每一行（Harness、供应商、模型、思考预算、脱敏、工作区、压缩阈值）和私聊顶层的消息，独立复核的模型，子 Agent，工作区的用途和增删，脱敏的两个开关和词表的批量增删，群的停用与免@（只能改成关闭或只有你），用 ssh 添加和移除设备，代答的开关和白名单。其余只读，每页有“去飞书改”。
- **整理专项**：开了[整理专项](projects.md#整理专项)时，看板首屏有“整理专项 · 要你定 N 条”，可以直接定。
- **作废一条专项记忆**：和专项卡上的“删”一样。

## 数据契约

这一节写给网页前端：Covey 推什么、怎么推，网页能发回哪些操作。字段后面的“v N”是数据约定的版本，前端按版本判断有没有这个字段；新字段都是可选的，旧网页不认就忽略。代码在 `core/web_sync.py`（配置、条目、操作账本）和 `app/web_*.py`。

### 推

`web-push` 线程把四类条目推给网页：专项、话题、一轮、问题；另有几条设置条目（见下）。

- **只推变了的**：每条带内容哈希 `rev`。一批最多 500 条或 1 MB（按发出去的 UTF-8 字节算；网页收的请求体上限是 1 MiB，超了整批 500），POST `/sync/upsert`。
- **节奏**：
  - 每秒只看在跑的任务、等回答的问题、等你在启动卡上点开始的话题，以及刚停下或刚被网页操作过的话题。
  - 每 60 秒开始一轮全量（最近 30 天有任务的话题、全部专项）。一轮分成很多步：每步先看 25 个话题，再看在跑、等你、刚答题或刚停、刚被网页操作过的话题，一起推出去，所以要紧的话题每秒都跟得上，不用等全量读完；一轮看完才知道哪些该删。
  - 每步先推要紧的话题和全部专项里变了的（整份推），全量读到的变化排在后面，每步最多再推 1 MB，没推完的留到下一步，同一条读到更新的就换成新的。
- **网页收下了什么**：哪些条目、各是哪个 `rev`，存在 `<state>/web/sent.json`（最多每 30 秒写一次，一轮全量读完也写）。重启后接着用，只推这期间变了的；文件是推给别的网页地址的、或者从上次清空起已满一天，就全部再推一遍。进程不重启也每满一天清空一次，网页库被清空过也能补回来。
- **读法**：话题的状态和“做完没看”用看板的读法（`core/board_read.py`、`app/pins.py` 的 `board_results`），不每秒扫全部任务记录。在跑的一轮，正文是已落盘的进展说明，变了就推。每话题推最近 100 轮，单轮正文超过 64 KB 截断并写明。
- **失败**：请求失败按 2、4、8、16、30 秒退避重试，只记第一次和每第十次，不影响 Bridge 别的线程。全量对时没有要推的，也发一批空的，网页的“最后同步”照样往前走。
- **卡住**：推和取的每一步在各自的工作线程里跑。
  - 一步 60 秒没有进展（网络盘上的 open、read 可能一直不返回），记一行 `web sync push stuck`，丢下它，下一步换新线程；推这边同时从头开始一轮全量，已推过的记录（`sent`）留着，只推真变了的。
  - 丢下的那一步后来返回了，读到的也是旧的，一条都不推（不然网页会被改回旧状态）。
  - 推这边看完一个话题、网页收下一批都算进展，所以第一次全量推一个大私聊只要一直在动，就不算卡住；取这边按一步开始的时间算。
  - 卡在读某个话题上的，在那条旧线程结束前，推送不再读这个话题（网页上保留它原来的样子，不删），旧线程一结束就重新看它一次，日志记一行 `leaves topic`。
  - 还卡着的旧线程到 3 个就先不开新的，最多等 300 秒，到时照样换新线程接着推（记一行 `still stuck`）。

### 话题条目

- **普通话题**：`kind: topic`。另带 `round`（已有几轮）、`started_at`、`context`（最近一轮的）。
- **状态色**（v5）：`tone` 是 [UI 规范](ui.md#3-状态词表) 的状态 key（waiting、stopped、stuck、result、running、queued、done、cancelled、idle，`core/look.py` 的 `tone`）。网页按它上色、取词，不比较 `state` 的中文字；`state` 照旧。词和颜色由 `scripts/export_look.py` 导出成 `look.json` 给网页。
- **在看板的哪一组**（v6）：开了看板（`COVEY_PINNED_TOPICS=1` 且 `COVEY_PINNED_OVERVIEW=1`）时，普通话题另带 `board`：看板上的哪一组（waiting、running，结果的 stopped、result、done、cancelled），不在看板上（看过超过 12 小时的结果、点了“不管了”的、更早的）为空串。网页的“要你处理”“停住了”和侧栏红点按它算；没开看板时不带这个字段。
- **进度**（v4）：在跑的话题带 `progress`：`{phase, plan_done, plan_total, last_activity_at, last_activity, eta_seconds, slow_step, maybe_stuck}`，和任务卡、看板同一个函数（`core/progress.py`）。
  - `last_activity` 是“在跑测试”这样的人话。
  - `eta_seconds` 估不出来为 null，到分钟（按这一轮平均每步用时减去当前这步已做的时间）。
  - `slow_step`：当前这步已经比平均每步慢时是 `{number, since, seconds, usual}`，此时 `eta_seconds` 为 null，网页可写“第 3 步已做 35 分钟，此前平均每步约 6 分钟”；否则为 null。
  - `maybe_stuck`：超过 `COVEY_PROGRESS_STUCK_MINUTES` 分钟没有动静；最新一步是还在跑的命令时不算。
  - `command`：只在最新一步是还在跑的命令时才有这个键，`{since, seconds, long}`（开始的时刻、已跑的秒数到分钟、是否已跑满卡住的分钟数），网页可写“在等一条命令跑完 · 已 38 分钟”；旧网页不认识这个键，不受影响。
- **模型名**（v11）：`model_name` 是读得懂的名字（`core/model_names.py`：`router/claude-opus-5-5` → Opus 5.5，去掉路由前缀、预算后缀和 `[1m]`，`default` 为空串）；`model` 照旧是原值。
- **最后一轮的编号**（v12）：`last_seq` 是这次推的各轮里最大的 `seq`（没有轮次为 0）。网页收到后删掉这个话题 `seq` 比它大的轮次：在跑的一轮按“已有轮数 + 1”推，回答进来后编号可能落在更前面，原来那条“在跑”就再也不会被覆盖。收尾中的任务（回答已写进历史、记录还是“在跑”）不判“没发出去”，也不再画一条“在跑”的轮。
- **什么时候进的专项**（v13）：专项的成员话题带 `joined_at`（秒）。专项报告只算这个话题加入以后结束的轮次，和专项卡的 token 同一个口径。
- **话题要点**：开了[整理专项](projects.md#整理专项)时带 `gist`：`{essence, goal, involves, related, changed, updated_at}`，没有要点就没有这个字段。网页把 `goal` 当“目标”显示。
- **当前配置**（v21）：普通话题带 `config`：`{model, effort, work_mode, workspace, run_mode, redaction}`，`model` 和 `settings.launch.models` 的 `key` 是同一套（绑的是预算预设的，换算成那个模型加预设的预算），`run_mode` 是 `direct` 或 `worktree`，`redaction` 是 `on`/`off`（`app/web_switch.py` 的 `topic_config`，只读 topic.json）。网页的换配置面板从它开始选；`model` 不在目录里时要重选模型。管家、负责人不带。
- **从没开工的话题**：任务都没跑过、最后一个没开工就取消了（启动卡被停掉收起），推成“已停止”（`tone: cancelled`），全图归到折起来的做完组，不算空闲。
- **等你点开始的话题**：最新的任务在等你在启动卡上点开始（启动卡还能用）时，话题是“等你”（`tone: waiting`，开了看板时 `board` 也是 waiting），`harness`、`model`、`model_name`、`workspace` 是启动卡上现在选的（点开始就用它们），不是话题等开始时先占着的绑定（回复告警卡这类消息开的话题先绑默认的 Harness 和工作区）；不带 `config`，配置由启动卡定，点了开始才能在网页上换。

**管家**：私聊的管家话题推成一个话题条目（只推槽 0，[同时做几件事](butler.md#同时做几件事开关默认关)的其余几路不推）。

- `kind: butler`，`number` 为 0，标题“管家”，不属于专项，`unseen` 恒为 false，状态照话题的算（有任务在跑是“在做”，在等你答题是“等你”）。
- 各轮来自它的历史：你在私聊顶层说的话（`owner`）和管家的回答（`agent`）；任务在跑时和话题一样推一条进行中的一轮。正文里照原样写 T 号，网页把 `T\d+` 链到那个话题。
- 管家不在看板里，所以单独看：任务在跑或在等你答题时每秒看一次，被网页操作碰过之后看一次，每 60 秒全量对时再看一次；没有历史也没有任务就不推。

**专项负责人**（`COVEY_PROJECTS=1` 且专项有负责人话题时）：

- `kind: lead`，`number` 为 0，标题“负责人”，`project_key` 是专项 id，`unseen` 恒为 false。
- 各轮来自负责人话题的历史：你在里面说的话（`owner`）、负责人的回答（`agent`）。Bridge 叫醒负责人的那一轮（回执，不是你说话）不推，它的回复只有负责人标了要告诉你时才算回答。
- 看得懂的回执变成一行 `role: event`：话题做完、失败、中断、停住（`T34 做完了`）；负责人在唤醒轮里派出话题时，也有一行 `负责人派出了 T31 …`。
- 每轮的 `started_topics`（`{key, number, title, harness}`）是这一轮派出去的话题：话题第一个任务进来时，哪一轮负责人最近开始过，就归哪一轮。
- 负责人在跑或有问题等你时每秒看一次，被网页碰过后看一次，每 60 秒全量对时再看一次。

### 一轮条目

- **配上它的任务**（`app/web_pairing.py` 的 `pair`）：回答那一轮带 `job_id`，过程、计划、用量、失败原因都从这条任务记录来。
  - 回答行写入历史时就带任务号；没带的旧行按顺序配：主人或负责人那条消息的 message_id、答题的 `answer-<token>`；送进正在跑的任务的补充没有自己的任务，回答仍归那个任务。
  - 连着的几条回答（限流自动重试、停止）和其余没配上的，按“这一轮开始之后、任务记录最后写入后 5 分钟以内”找没被别的回答占用的任务。
  - 还配不上的，`bridge.log` 记一行 `web sync topic <话题 id 前 12 位>: N of M answer turns have no task record`（数目变了才记）。
- **过程和用量**（可选字段，没有就不带）：
  - `steps`：最近 80 步 `{id, kind, label, started_at, ended_at, status}`，可带 `detail`。`kind` 按步骤标题归为 read、edit、command、search、subagent、web、think、other；进展说明不算步骤；在跑的一步 `ended_at` 为 null。
  - 子任务（v8）是一步：`kind` 为 subagent，`label` 是“子任务：”加它自己的描述，起止取子任务真正开始和结束的时间，这一轮结束时它还没结束的按这一轮结束算；启动它的那次“分配子任务”调用不再单列。
  - `label`（v18）是纯文本，不带卡片用的转义。模型说了这一步做什么的，`label` 只写“标题：目的”，命令放在 `detail`（最多 400 字）；`label` 超过 80 字被截断的，`detail` 是完整的那段。
  - `plan`：检查点里的 steps（`{title, state}`）；这一轮已结束时还停在 doing 的那步推成 todo。
  - `usage`（`input`、`output`、`cache_read`、`total`）、`seconds`、`speed`（token/s）、`context`（`{used, limit}`）、`notes`（记忆那一路写的 `memory_notes`，原样推）。
  - 在跑的一轮随每秒的推送更新：`seconds` 每 10 秒才变一次、`speed` 取整，没有实质变化时 `rev` 不变。已结束的轮：`usage`、`seconds`、`speed`、`context` 每轮都带，`steps`、`plan` 只带最近 20 轮的。
  - 被 Bridge 重启打断、由自动重试接着做完的任务，它的 token 算进重试那一轮的 `usage`。
- **预计用时**：`estimate_seconds` 是这一轮第一次估出来的总用时（秒），写进任务记录后不改；专项报告的“预计 vs 实际用时”拿它比。没估出来过的轮次不带。
- **答题**（v4）：回答问题卡的那一轮带 `answers`：`[{question, choice}]`，只从问题记录取，不解析正文。答案作为新任务发出去的带在主人那一轮上，交回给还在跑的提问那一轮的带在那一轮的回答上；`choice` 是选中的选项或写的字，多选用“、”连起来，没答的写“没答”。
- **没开工就取消的一轮**：最后那句话的任务从没开工就取消了（启动卡被收起、排队时取消），补一行 `role: agent`、`status: stopped`，正文“没开工，已取消（原因）”。没有这一行时网页会一直写“排队中…”。
- **等你点开始的一轮**：最后那句话的任务在等你在启动卡上点开始，补一行 `role: agent`、`status: waiting`（`job_id` 是那个任务，`started_at` 是消息进来的时刻），正文第一句“在启动卡上点开始，这一轮才会开工。”，下面是点开始会用的配置和去飞书打开启动卡的链接（「手上的事」读到过那张卡在哪才有；推送不为它去读飞书）。没有这一行时网页写“排队中…”，而话题的 `round` 已经算上了这个任务，网页就把这句话编成下一轮、上面写“再早的 1 轮没有载入”。
- **没成功的一轮**（v4、v10）：带 `failure`（`{title, detail, actions}`，`core/failure.py` 的 `failure_view`，同任务卡上的那句话和按钮），只在失败、超时、重启打断、官方拒绝时有；`actions` 取 continue、retry、diagnose、split、switch_model。带 `failure` 的回答一轮 `status` 是 `failed`。
- **谁说的、给人看的正文**（v11，`app/web_said.py`）：
  - `owner` 那一轮带 `by`：`owner`（你）、`lead`（负责人）、`butler`（管家）。负责人、管家交来的话（消息 `main-<请求>`）按请求记录找到发起的那一轮：那一轮是回你的一条消息，交来的就是你的原话，`by` 为 `owner`、`via` 为 `lead` 或 `butler`；不是的，`by` 就是它。
  - 问题卡的回答：负责人分流替你答的是 `lead`，管家按你的话代答的是 `butler`，其余是你。
  - `display` 是去掉内部前缀的正文，和原文不同时才带：答题写成“答了 2 题：B；没答”；反馈问题写“没答，补充：…”；编辑过的消息只给改后的全文，另带 `edited: true`；撤回补充写“撤回了刚才那条补充：「…」”；纠正负责人的回答写“纠正负责人替你答的「…」：…”。
  - 答题那一轮另带 `note`（选项之外写的字）。`body` 仍是原文，旧网页照旧显示。
  - 编辑作为补充单独进了正在跑的任务的（消息 id `edit-<原消息 id>-<时刻>`），改后的全文放到原消息那一行，编辑这一行带 `merged: true`，网页不再单画（原消息那行不在推的轮次里时照旧单画）。
- **图片**（v19）：`images` 是 `[{id, w, h, kind, note?, gone?}]`。
  - `kind` 是 `sent`（你那一轮带的图，只认本状态目录 `topics/<话题>/incoming_media/` 下的文件）或 `shown`（任务检查点 `show_images` 推给你看的图，设备上的任务不推），`note` 是检查点里那句说明。
  - 图片只按编号给：`id` 是原图 SHA-256 的前 32 位，网页从自己的 `/api/images/<id>?v=thumb|full` 取（要登录、只有主人）。本机路径和公开直链都不给网页。
  - 传：每张一次，缩略图（长边 480）和大图（长边 2048）各一份 WebP，每份不超过 700 KB，POST `/sync/image`（`id`、`v`、`mime`、`w`、`h`、`expires_at`、`data` 是 base64）；传之前 POST `/sync/images/have` 问一次已有的。每轮最多 9 张，原图超过 20 MB 不传；推送线程每一步最多传 8 张、20 秒。机器上没有 Pillow 时，700 KB 以内的 PNG、JPEG、WebP、GIF 原样传，更大的不传。传过的记在 `<state>/web/images.json`。网页不收时只把图压后 5 分钟，条目照常推；网页拒收某一张（400、413）就不再传它。
  - 管家、负责人转给话题的那一行：历史里只存原话和“附件：图片 N 张”，图从它开的那个任务的提示词里取。
  - 留多久：消息之后 90 天（`expires_at`，网页每天清过期的）；更早的消息的图不传，条目里 `gone: true`、没有 `id`，网页写“已过期，飞书里还能看”。
  - 本机路径不进网页：你那一轮的正文（`body`、`display`）和问题的回答都先去掉路径（`app/web_images.py` 的 `scrub`）：“收到的附件”那块变成“附件：图片 N 张”，别处写出的路径写成“（图片）”。

### 问题条目

- 带 `asked_at`、`answered_at`（没答为 null）、`project_key`、`topic_number`。
- **题目不再等的时刻和方式**（v9）：`closed_at`（秒，还在等或不知道时为 null）、`closed_by`：`owner` 你答了、`feedback` 只发补充说明或提了反馈、`skipped` 每题都选了不回答（飞书卡上点了“不处理”的也是）、`lead` 负责人替你答了、`superseded` 话题又问了新题、`stale` 每日维护关掉的过期题、`switched` 换配置、`rewound` 回退、`lead_stop` 负责人停了话题、`web_stop` 你在网页上停了话题、`cancelled` 别的取消；还在等或在负责人手上为空串。
- 时刻来自问题记录自己的 `answered_at`、`cancelled_at`；更早的记录没有，依次取答案续跑任务的创建时间、负责人代答的时刻、收起它的新题的提问时间、提问任务最后一次写入。专项报告按它画“等你”：只有 `open` 的题算还在等。
- 负责人先接手的题（[问题分流](projects.md#专项的问题分流)）`state` 是 `held`：还轮不到你答，网页不算“等你”；转给你后变回 `open`。
- **和问题卡对齐的几样**（v20）：每道题带 `context`（卡上题目下面那行“背景”，没有为空串）；`extras` 说“先问一句”“不处理”能不能点（同卡片：话题自己的题，没有任务正停在原生提问上）；`asked` 是最近一次“先问一句”（`{at, text}`，没有为 null）；点了“不处理”、话题下一轮还没带走时 `closed_by` 是 `set_aside`、`restorable` 为 true，网页显示“撤销”。

### 专项条目

- `goal`（v11）是负责人用 `project_goal` 写的目标，没写过为空串（开专项时的那句话常常只是安排，不当目标显示）。另带 `started_at`。
- `memory` 每条带 `id`、`said_by`（你说的、它总结的）、`origin`（`{at}`）；`library` 每条带 `kind`（file、link、image、video）、`size`（知道才给）、`topic_number`（知道才给）。

### 设置条目

另推几条 `settings` 条目。设置条目由取操作的线程（`web-inbox`，每秒一次）单独推：网页改了一项设置，回执之前就推（网页看到“已保存”时读到的已是新值）；`main-thread.json`、工作区列表或脱敏词表一改，下一秒就推；推的线程全量对时也推一次（内容没变 `rev` 不变就不发）。

- **`settings`**（v7）：飞书设置卡的各页，同名同序：配置（v22；v21 及以前是新话题、管家两页 `topic`、`main`）、工作区、脱敏、群聊、设备（只在设了 `COVEY_NODE_LISTEN` 时有）。`config` 是 `{summary, topic, main?, review?, more, more_note, locked}`：`topic`（新话题：`title`、`rows`、`note`）、`main`（管家，`COVEY_BUTLER=0` 时没有：`title`、`on`、`rows`、`note`，私聊顶层的消息是 `rows` 的最后一行）、`review`（独立复核，`COVEY_REVIEW=1` 才有：`title`、`rows`、`note`），`more` 是子 Agent 一行，`summary` 是卡片标题栏那句。每页的行和卡片用同一套函数画（`cards/butler_panel.py` 的 `topic_settings`、`butler_setting_list` 等）：标签、当前值、可选项，`editable` 说网页能不能改。另带 `feishu`（打开机器人的 applink）。代码在 `app/web_settings.py`、`app/web_panels.py`。
  - 工作区带标签、路径尾部、最近使用和用途；每条带 `removable`（默认的那个不能移除），另带 `missing`（目录确认不存在的有几个）（v15）。
  - 脱敏带统计、开着脱敏的话题、两个开关和词表（v14：`terms` 按词表顺序的原词，另带 `ignore_case`、`max_terms`、`max_length` 给网页保存前检查；**替身词不推**；原词会进网页应用的库，为的是在网页上批量编辑）；`records` 是最近几轮替换了多少，只有数字（v15）。
  - 群用 chat id 的哈希当 key，带状态和免@；每个群带 `free_mode` 和 `free_choices`（v15）。群名照群聊卡的读法问飞书，同一个群 30 分钟问一次。
  - 设备每台带 `dot`（ok、tight、down、never，网页画色点）和去掉名字的那一行，另带 `joins`（网页发起的添加：安装中、失败及原因，30 分钟后不再列）和 `can_add`（v15）。
  - 管家的“私聊顶层的消息”行带 `confirm`（改成“每条直接开一个话题”前网页要问的那句话）（v15）。
  - 部署有代答发送器时多一页 `proxy`（v15）：开关、网页登录、发不出去的原因、模型的几行、白名单（名字、私聊或群 @我、暂停没有、来自哪个群）。名字会进网页应用的库。
- **`launch`**（v17）：`settings` 条目另带的启动卡选项，用启动卡同一套函数读（`app/web_launch.py` 的 `catalog`）。
  - `models[]` 按启动卡的菜单顺序，每个带 `key`、`harness`、`harness_title`、`provider`、`provider_title`、`label`、`budgets[]`、`default_budget`、`start_budget`（选到这个模型时卡上预选哪一档）、`redaction`（能不能脱敏）、`work_modes` 和 `work_mode_texts`。
  - `workspaces[]`（最近用过的在前，设备上的带 `device` 和那台设备声明的 `harnesses`）、`run_modes[]`、`preselect`（现在开一张启动卡会预选的）。
  - 网页照卡的两条规则联动：换 Harness 或供应商时，当前模型在新范围里就不动，否则取这个顺序里的第一个；换模型时取它的 `start_budget`。
- **key `tidy`**（v16，开了整理专项时）：网页的“整理专项 · 要你定 N 条”面板读它。
  - `proposals` 每条是 `id`、`run`、`kind`、`tag`、`verb`、`headline`、`from`（这几个词和卡片同一个函数）、`why`、`goal`、`topics`（`key`、`label`、`title`）、`state`（open、done、declined、failed、expired、never）、`note`（定了以后的结果）；列最近 7 天整理里还开着的，和 24 小时内定了的。`running` 是正在整理的进度。
  - 它单独一个请求推（整理记录和提议两个目录变了、或全量对时才推）；比 v16 旧的网页拒收这个 key 时只记日志，不耽误设置。
- **key `usage`**：本周用量，按菜单「用量」卡的算法（`app/weekly_usage.py` 的 `summarize`：往前 7×24 小时、按任务记录的创建时间、算后端真跑过的所有任务），推的线程全量对时推。

### 网页发回的操作

`web-inbox` 线程每秒 GET `/sync/inbox?after=<seq>`；先把 seq 和 op_id 记进 `<state>/web/inbox.json`，再执行，按 op_id 去重（重启后也一样；记了没记结果的算“结果不确定”，回 failed，不重发）；执行完 POST `/sync/ack`。话题、专项必须属于主人的私聊；不认识的、已结束的回 failed 并写原因，网页按 op_id 显示。

| 操作 | 做什么 |
| --- | --- |
| `say` | 作为你的话进这个话题，见下 |
| `answer` | 交给问题自己的提交路径（`answer_fields`）：没有选项的题收 `answers` 里写的文字，不是选项原文的文字也当作写的回答（同卡上的回答框），`{"skip": true}` 是这道题“不回答此题”（v20）。`skip` 为 true 是“只发补充说明”，`note` 必填。卡上的次要按钮（v20）：`ask` 为 true 是“先问一句”（`note` 是要问的话，必填；题目不关），`aside` 为 true 是“不处理”，`restore` 为 true 是它的“撤销”，走卡片同一处。管家自己的问题只能在飞书里答。问题已经不在等你时：和已存的回答一样是 `done`（多点了一次，没有再发）；否则 `failed`，消息说清是谁、怎么处理掉的（你在网页或飞书答过、负责人替你答了、后来的问题把它收起、每日维护关掉、话题停了或换了配置）和现在怎么办（`app/web_pairing.py` 的 `settled_words`） |
| `seen` | 把话题最新的结果记为看过，同在飞书里读到它；已经看过的回 done 且不写文件；管家回 done |
| `stop` | 停这个话题，见下 |
| `reopen` | 把收起或被停住的专项重新展开；已结束的回 failed |
| `forget` | `project_key`、`payload.entry_id`：作废一条专项记忆，同专项卡上的“删”（留墓碑，负责人 30 天内不会再记回去），回 done“已作废：<前 40 字>” |
| `dismiss`（v15） | `topic_key`：同看板卡的“不管了”，只改看板的记录；没开看板时回 failed |
| `tidy` | `{scope}`：开始一次整理专项，回 done“已开始整理，结果发到私聊”；正在整理时写进度；整理专项没开时回 failed |
| `tidy_decide`（v16） | `proposal` 是提议 id，`choice` 是 `ok` 或 `decline`：走那张卡自己的点击，卡原地重画成一样的结果；已经定过的回 failed |
| `launch`（v17） | 开新话题，见下 |
| `switch`（v21） | 换话题的配置，见下 |
| `settings` | 改一项设置，见下 |

**`say`**：

- 话题有任务在跑就是补充，没在跑就开下一轮（消息 id `web-<op_id>`）。
- 话题在等你答题时，和在飞书话题里发一句话一样：只有一道题在等时这句话就是回答；几道题在等、或题还在负责人手上时拒绝，题不会因此关掉。
- `topic_key` 是管家时，当作你在私聊顶层发了这句话，走和飞书消息同一个入口（准入、去重、扩展、管家），管家照常直接答、开话题、交给专项。管家没开时回 failed，不会变成一个新话题。`web-<op_id>` 不是飞书里的消息：这一轮的回复不引用它、不加表情，作为私聊里新的顶层消息发出。
- `topic_key` 是负责人时，等于引用专项卡回复：负责人在跑就作为补充，没在跑就开它的下一轮；专项已结束、没开 `COVEY_PROJECTS` 时回 failed。

**`say` 带附件**（v21）：`payload.files` 是 `[{id, name, mime, size}]`，有附件时 `text` 可以为空。

- 网页先把文件分段传进自己的库：`POST /api/uploads`（`{name, mime, size}`，回 `{id}`）、`POST /api/uploads/:id/parts`（`{n, data}`，每段原始字节不超过 512 KiB）、`POST /api/uploads/:id/done`。单个不超过 20 MB，一句最多 9 个，7 天后过期。
- Covey 执行 `say` 时经开放接口取回：`GET /sync/upload?id=<id>`（`{id, name, mime, size, parts, done}`，没有、过期、没传完是 404）、`GET /sync/upload/part?id=<id>&n=<n>`（`{n, data}`）。存进话题的 `incoming_media/`（文件名 `web-<id 前 16 位>-<净化过的原名>`），按飞书附件的格式列在话说完之后（`app/web_files.py`）：图片是“图片 N”并作为图片交给模型，其余是“文件 N（原名）”。
- 有一个取不到（过期、没传完、比说的大、超过 20 MB）整条 `say` 回 failed，写明哪个、为什么，不发半截。
- 这一轮推回网页时，图片照 v19 的 `images` 显示，正文里的附件块是“附件：图片 N 张”。开新话题（`launch`）不带附件。

**`switch`**（v21）：`topic_key`，`payload` 同 `launch` 去掉 `text`（`model`、`effort`、`work_mode`、`workspace`、`run_mode`、`redaction`）。

- 只认主人；管家、负责人回 failed。模型这几项的校验同 `launch`；工作区或运行方式和现在一样就不动，不一样时同换配置卡的检查（设备、声明的 Harness）。
- 同换配置卡的“确认更换”（`app/binding.py` 的 `switch_topic`）：有任务在排队或在做、什么都没变时回 failed；要新建独立 worktree 的回 failed“新建独立 worktree 要在飞书的换配置卡上做”（建 worktree 要几十秒，收操作的线程不等）。
- 换成了：飞书话题里出一条和换配置卡一样的“🔁 已换配置”，模型选择记进这个私聊的用量；回执“已换配置：<旧> → <新>；工作区 a → b，下一句话起由新会话接着做”，只改脱敏是“已打开脱敏，会话不变”。

**`stop`**：停这个话题在跑的任务，同卡片上的停止。话题在等你答题时，那道题一并收掉（原因 `web_stop`，卡上写“你在网页上停了这个话题”，看板上是“已停止”而不是“停住了”）；只有还没点开始的启动卡时把启动卡收掉；什么都没有时回 failed。

**`launch`**：`payload` 带 `text`（第一行当标题）、`model`（`launch.models` 的 `key`）、`effort`、`work_mode`、`redaction`（`on`/`off`）、`workspace`、`run_mode`。

- 只认主人；校验同启动卡的“开始任务”，不过回 failed 写原因。
- 开法同管家直接开工的话题：私聊顶层一张话题卡，第一轮就是你写的话；这次选择记成这个私聊的“上次开始任务”，下一张启动卡和网页的预选都跟着变。
- 独立 worktree 只有启动卡能建：这样的话题出一张按网页选择填好的启动卡，在飞书话题里点开始任务。
- 每一步记在 `<state>/web/launches/<op_id>.json`，同一条操作再来不会开第二个话题。回执“已开 T23「标题」：直接开工”，网页按 T 号跳过去。

**`settings`**：只认主人（`app/access.py` 的 `is_owner`），写设置走和卡片同一处，回执也是卡片那句。`payload.what`：

- `row`：`kind` 是卡片那一行的 kind、`option` 是选项值。也收管家的脱敏和“私聊顶层的消息”（`kind` 为 MODE；改成“每条直接开一个话题”要带 `confirm: true`）。
- `untrust`（`key`）：已下线（v23），回 failed“记住的工作区已下线：管家开的话题一律直接开工”。
- `note`（`workspace`、`note`）：工作区用途，空串清掉。
- `redaction`（`name`、`on`）：脱敏的“不区分大小写”“回复还原”。
- `terms`（`add`、`remove` 是原词列表）：一次先删后加；删的按“不区分大小写”开关比、不在的跳过，加的已在词表里跳过；有一个加不进（太短、太长、控制字符、`=>`、超 1000 个）就一个都不存。回执只说个数，不写原词（回执存在网页的发件箱里）。
- `group`（`group`、`on`）：群的停用与启用。
- `free`（`group`、`mode`）：免@ 只能改成 `""`（关闭）或 `owner`（只有你），代答群不能免@。“你和选中的人”“群里所有人”留在飞书改。
- `workspace_add`（`path`，绝对路径或“设备别名:路径”，查目录最多 5 秒）、`workspace_remove`（`workspace`，只是不再登记，目录和已开的话题不动；默认的那个不能移除）、`workspace_prune`（移除目录确认不存在的）。目录浏览不做。
- `node_add`（`name`、`title`、`ssh`）：只能经 ssh（不用 ssh 的方式要显示一次性口令，口令不进网页的库）。ssh 在单独的线程里跑，最长 90 秒，结果写进 `<state>/web/node-joins.json`。`node_remove`（`alias`）：同卡片，本机不能移除。`refresh`：什么都不改，只让设置马上重推。
- `proxy`（`op`）：`enable`、`disable`、`row`（`kind`、`option`）、`pause`、`resume`、`remove`（`contact`），和代答卡同一处。按名字查找添加、去登录留在飞书。
- 其余回 failed“这一项网页上只读”。

### 版本一览

| 版本 | 加了什么 |
| --- | --- |
| v4 | 话题的 `progress`、一轮的 `answers` 和 `failure` |
| v5 | 话题的 `tone` |
| v6 | 话题的 `board` |
| v7 | `settings` 条目 |
| v8 | 子任务是一步 |
| v9 | 问题的 `closed_at`、`closed_by` |
| v10 | 带 `failure` 的一轮 `status` 是 `failed` |
| v11 | 一轮的 `by`、`via`、`display`；话题的 `model_name`；专项的 `goal` |
| v12 | 话题的 `last_seq` |
| v13 | 成员话题的 `joined_at` |
| v14 | 脱敏页推原词 `terms` |
| v15 | 设置页补齐（设备、工作区、群、管家、脱敏记录、代答）；`dismiss` 操作 |
| v16 | `settings` 的 `tidy` 条目；`tidy_decide` 操作 |
| v17 | `settings` 的 `launch`；`launch` 操作 |
| v18 | 步骤 `label` 是纯文本 |
| v19 | 一轮的 `images` |
| v20 | 问题的 `context`、`extras`、`asked`、`restorable` 和 `closed_by` 的 `set_aside`；`answer` 的逐题不回答、`ask`、`aside`、`restore` |
| v21 | 话题的 `config`；`switch` 操作；`say` 的 `files` 和取附件的两个开放接口 |
| v22 | `settings` 的新话题、管家两页并成 `config`，加独立复核一段 |
| v23 | `config` 的每一行加 `line`（卡片上并排的那一行，从 1 起；行序按卡片的排法：新话题 工作区、运行方式、脱敏，管家 工作区、脱敏、压缩阈值）；去掉新话题的“直接开工”行、`start_note`、`trusted`；`untrust` 下线 |

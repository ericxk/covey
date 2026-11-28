# 运维指南

本文写给维护机器人服务的人：服务由哪些进程组成、数据放在哪里、日常操作、日志、备份维护、任务进程、内存看门狗和托管。升级、回滚和改了开关后的重新注册见[升级与部署](deploy.md)，飞书应用的权限和菜单见[飞书应用配置](feishu-app.md)，开关和参数见[配置](configuration.md)，出了问题按症状查[排障](troubleshooting.md)，内部实现见[架构与开发说明](architecture.md)。

## 你是谁看哪里

| 你是 | 要做的事 | 看哪里 |
| --- | --- | --- |
| 用 `./install` 装的（多数人） | 看状态、重启、停用 | [日常操作](#日常操作) |
| | 改开关、改参数 | [配置 · 改开关三步](configuration.md#改开关三步)、[升级与部署 · 改设置后重新注册](deploy.md#改设置后重新注册) |
| | 装、启用、停用扩展 | [扩展的装和卸](#扩展的装和卸) |
| | 升级到新版本、出事回滚 | `./install` 装的：`scripts/upgrade.sh`（[日常升级](deploy.md#日常升级)）；其余和回滚：[升级与部署 · 升级与回滚](deploy.md#升级与回滚) |
| | 看日志、备份、机器重启后没回来 | [日志](#日志)、[备份、归档与维护](#备份归档与维护)、[开机恢复](#开机恢复) |
| | 飞书应用缺权限、菜单不出来 | [飞书应用配置](feishu-app.md) |
| 自己注册服务、维护固定 release 的 | 以上全部，再加 | [服务组成](#服务组成)、[注册与启动服务](#注册与启动服务)、[部署新版本](deploy.md#部署新版本)、[服务托管](#服务托管) |
| 从改名前的老部署升级的 | 旧名还能不能用 | [改名与兼容](compat.md) |
| 不用 `./install`、手工建应用的 | 旧脚本、owner、首次验收 | [手工安装](manual-install.md)（新装不用看） |

### 找到数据根和 APP_ID

下文命令都在服务正在运行的代码目录（`./install` 的检出，或 `releases/` 下的 release 目录）里执行，并假设已设好这几个变量：

```bash
export COVEY_DATA_HOME="$(python3 -B bin/covey_paths.py)"   # 数据根；./install 装的默认是仓库同级的 <仓库目录名>-data
ls -d "$COVEY_DATA_HOME"/harness-cli_*                       # 每个机器人一个状态目录，目录名里就是 APP_ID
APP_ID=cli_xxx                                                # 换成上一行看到的
STATE_DIR="$COVEY_DATA_HOME/harness-$APP_ID"
SERVICE_ROOT="$(python3 -B bin/covey_paths.py service-root)"
```

- `bin/covey_paths.py` 不带参数打印数据根（`COVEY_DATA_HOME`，没设时按代码目录推算，见[数据目录](#数据目录)）；从 release 目录运行时推算出的不对，所以在 release 目录里先 `export COVEY_DATA_HOME=/absolute/covey-data`，或先[载入服务清单的环境](deploy.md#载入服务清单的环境)（`./install` 写进清单的 `LARK_BRIDGE_DATA_HOME` 就是数据根）。
- `SERVICE_ROOT` 是服务根目录，和 Manager、doctor、`covey_service_install.py` 不带 `--root` 时的默认值一样。Bridge 和任务进程的 shell 里没有 `COVEY_SERVICE_ROOT`，不要直接用它。
- 只操作自己的服务。不要对所有应用执行全局的事件总线停止。

## 服务组成

```text
开机守护（systemd 用户单元、launchd、crontab 的 @reboot，或扩展提供的托管方式）
└─ Manager：bin/covey_service.py run，每个服务根一个
   └─ Bridge：bin/covey_bridge.py --harness unified，每个机器人一个
      ├─ 事件消费者（lark-cli event consume 子进程）
      │   ├─ im.message.receive_v1          收消息
      │   ├─ card.action.trigger            卡片点击
      │   ├─ application.bot.menu_v6        机器人菜单（可选订阅）
      │   └─ im.message.message_read_v1     已读（COVEY_PINNED_TOPICS 没关时）
      ├─ 后台线程：scheduler、cards、menu、maintenance、connections、availability、outbox、policy，
      │   加上打开了的功能各自的后台线程（proxy、overview、digest、main-tools、app-setup 等）
      └─ 任务进程：bin/covey_runner.py，每个任务一个，在自己的会话里运行
          └─ 后端进程（codex app-server、claude、dsh、agy，或扩展的隔离解释器）

内存看门狗：bin/covey_memguard.py，独立于以上进程，由 Manager 拉起
自托管网页进程（可选，COVEY_WEB_SELFHOST=1）：node <目录>/server/selfhost/main.js，由 Manager 拉起，见 web.md 自托管
```

| 组件 | 谁拉起、谁重启它 | 它退出时 |
| --- | --- | --- |
| Manager | 开机守护。没有 Manager 时，`covey_service.py` 的 `launch`、`start`、`stop`、`restart` 也会用执行它的那份代码拉起一个。服务根写不进去（配额满、挂载卡住）时它不退出：`manager.json`、状态和错误文件写不成只在 `manager.log` 记一次，下一轮再试 | Bridge 和任务进程继续运行，下一个 Manager 按进程号、启动时间和 boot ID 核对后接管 |
| Bridge | Manager。意外退出（含退出码 0）按 5 秒到 5 分钟退避重启，稳定 5 分钟后清零失败计数；本地健康接口连续 120 秒无响应时替换 | 任务进程继续运行，新 Bridge 认领它们 |
| 事件消费者 | Bridge。启动 60 秒内没有输出 `[event] ready` 就替换；退出后 5 秒重启，卡片回调消费者异常退出时 300 秒后重启 | 任务不受影响 |
| 后台线程 | Bridge。异常退出 5 秒后重建，日志 `worker <名称> failed` | 任务不受影响 |
| 任务进程 | Bridge 在任务出队时启动 | 没到终态就退出时标为中断，不自动重跑，见[任务进程](#任务进程) |
| 自托管网页进程 | Manager，只在 `COVEY_WEB_SELFHOST=1` 时；意外退出按服务的退避重启，开关关掉、设置变了或 Manager 退出时整组停掉（和 Bridge 不同，它不留给下一个 Manager；Manager 被强杀时它几秒内自己退出） | 网页打不开，推送退避重试；任务不受影响，见[网页工作台 · 自托管](web.md#自托管本机上跑网页默认关) |
| 内存看门狗 | Manager 每 30 秒看一次，已启用而没在运行时拉起 | 见[内存看门狗](#内存看门狗) |

- 一个状态目录同时只能有一个 Bridge（单实例锁）。不要手工再启动一个，也不要删 `bridge.pid` 来绕过检查。
- Bridge 一直起不来或收不到消息时，Manager 会在私聊里告诉主人，见下文[机器人停了会告诉主人](#机器人停了会告诉主人)。
- 一个 lark-cli profile 只能被一份运行中的服务清单占用。另一个状态目录用同一 profile 注册时，`launch` 报 `another managed bridge already owns this profile`。

开了 `COVEY_EVENT_TRANSPORT=sdk`（[配置](configuration.md#飞书-sdk-与直连)）时没有 lark-cli 事件消费者子进程，四类事件由 Bridge 的 `event-websocket` 线程收；`bridge.log` 里的 `ready event_key=` 两行照旧有，事件总线和 `bus.log` 这一层没有。这时 Bridge 启动后一核对完身份就连上（`event websocket connected; events wait for the start to finish`），重启认领那几秒里的点击和消息先收下，认领完再处理，见[启动时先连](architecture.md#事件长连接走-sdk默认关)。停止时还没处理的消息和点击留在 `event-spool/`，下次启动先补处理：日志里停止时是 `event spool: N events taken in and not handled are kept for the next start`，启动时是 `event spool: what the last run took in and did not handle goes first: …`；停止时出现 `writes still queued` 说明盘挂住、有几条没记下来。

**一条消息怎么走。** 消费者读到事件 → Bridge 判断谁被服务、找话题 → 发启动卡或排队卡 → 调度器取到任务，写交接文件、启动任务进程 → 任务进程启动后端、读后端事件、写任务卡、投递结果 → 退出。点击先到 Bridge：任务进程还活着时整条转进它的收件箱，否则由 Bridge 处理。详见[架构与开发说明](architecture.md)。

### 机器人停了会告诉主人

Bridge 自己停了、或者和飞书断了，它自己说不了话。所以由托管它的 Manager 来说（`service/down_notice.py`，开关 `COVEY_DOWN_NOTICE`，默认开）：

| 什么时候 | 主人收到 |
| --- | --- |
| Manager 连续 3 次启动 Bridge 都失败（`status` 的 `failures` 到 3） | “⚠️ 机器人没起来：服务管理器已经连续 3 次启动它都失败了（最近一次退出码 …）…”，带排查入口 |
| 同一个启动错误（`<root>/errors/<id>.json`，如工作目录或解释器不在了）让 Bridge 2 分钟起不来；Bridge 进程还在跑时（比如只是状态文件写不进去）不算 | “⚠️ 服务管理器已经 N 分钟启动不了机器人（错误原文）…” |
| Bridge 在跑，但 10 分钟收不到飞书消息：健康快照里和飞书的连接 `stalled`、`disconnected`、`unresponsive`，或收消息的消费者一直没就绪 | “⚠️ 机器人已经 N 分钟收不到飞书消息了…” |
| 上面任一条发出去以后，Bridge 正常运行（健康快照 `ready`）满 5 分钟 | “✅ 机器人恢复了…”，只发一次 |

每个任务在自己的任务进程里跑，Bridge 停了也不停。所以前两种（Bridge 没在跑）的“停了”消息会写“在跑的 N 个任务在自己的进程里，没受影响”；“恢复了”消息写出问题前开始、现在还在跑的那几个（“出问题前开始的 N 个任务在自己的进程里，一直没停，还在跑”）。数的是 `<状态目录>/runs/` 下进程还活着的任务进程（`runner.json` 的 `started_at` 早于出问题的时刻），一个都没有就不写这句。收不到飞书消息的那种不写：任务进程可能也连不上飞书。

- 用的就是这个机器人：Manager 读服务清单里的 profile，以机器人身份（`im +messages-send --user-id … --as bot`）发给 `<状态目录>/bot-identity.json` 里的主人，带服务清单里的 `PATH` 和代理。Bridge 一次都没成功启动过（还没有 `bot-identity.json`）时发不了，`manager.log` 记一行。
- 发送在单独的线程里，Manager 的巡检不等它。没发出去的每 2 分钟用同一个幂等键再试（飞书一小时内同一个键只发一次），50 分钟后放弃。发之前先把要发的写进 `<root>/notices/<id>.json`，Manager 中途重启也接着发完，不重复。
- 冷却：同一个服务的“停了”消息至少隔 1 小时；没收到“停了”就不会收到“恢复了”；`stop` 停掉的服务什么都不说。
- `manager.log` 里对应的行：`told the owner it is down (crash|error|offline)`、`told the owner it is back (…)`、`the down message to the owner did not go out (…)`。
- 关掉：服务清单里设 `COVEY_DOWN_NOTICE=0`（`covey_service.py set --state-dir "$STATE_DIR" COVEY_DOWN_NOTICE=0`，见[配置 · 改开关](configuration.md#改开关三步)）。关着时 Manager 不读、不写、不发，和以前一样。

## 数据目录

数据根是 `COVEY_DATA_HOME`。没设时是代码目录同级的 `<代码目录名>-data`，从 `releases/<commit>/` 运行时会变成 `releases/<commit>-data`，所以服务清单里总要有它：`./install` 会写进去，手工注册时在终端里显式设置。

```text
<数据根>/                          COVEY_DATA_HOME
├─ harness-<APP_ID>/               状态目录 <state>（--state-dir），一个机器人一个
├─ bridge-services/                服务根 <root>（COVEY_SERVICE_ROOT）
│  ├─ services/<id>.json           服务清单；<id> 是状态目录绝对路径的 SHA-256 前 24 位
│  ├─ status/<id>.json             Manager 观测到的状态：进程身份、代次、失败次数、Bridge 健康快照
│  ├─ errors/<id>.json             Manager 处理这个服务时正在出的错，处理成功后删除
│  ├─ notices/<id>.json            告诉主人“机器人停了 / 恢复了”的记录（COVEY_DOWN_NOTICE）
│  ├─ manager.json、manager.log    当前 Manager 的进程身份、Manager 日志
│  ├─ boot-installation.json       开机注册回执
│  └─ memory-guard/                内存看门狗的现场、事件和告警
├─ releases/<commit>/              固定版本的代码
├─ worktrees/                      “独立 worktree”运行方式的检出
├─ <扩展名>-runtime/               扩展的隔离环境（source/、venv/）
├─ extensions/enabled              本部署在 release 自带名单上加减的扩展（covey_plugins.py enable/disable 写它）
├─ proxy-web/venv/                 装了 Playwright 的 Python（自动补齐、验收页面）
├─ tools/<发送工具名>/<提交>/      代答的发送工具；./install 装不进全局时的 lark-cli 也在 tools/
├─ install-logs/                   ./install 每一步的日志
├─ harness-probe/                  Harness 探测结果的缓存（./install、doctor --probe-harnesses）
├─ browsers/、tmp/、workspace/     ./install 用的无头浏览器、临时目录、默认工作区
├─ install.json                    ./install 上次注册时的设置（重跑时比较它，没变就不重新注册）
├─ install-harnesses.json          ./install 上次写 harnesses.json 时哪些 Harness 能用（重跑时据此只加新能用的）
├─ upgrade.json                    网页「版本与更新」和定时自动升级怎么查新版本、怎么升级（能升级这个部署的安装器写，见 web.md 版本与更新）
└─ harnesses.json                  --harness-config 指向它（./install 写这个文件）
```

状态目录是服务数据，不是项目输出目录：切换工作区不会移动它，不要提交、清空或复制给别人。各 Harness 自己的原生会话（例如 `~/.codex/sessions`、`~/.claude/projects`）不在这里。

### 状态目录

`<state>/` 下的内容按用途分组。“谁写”里的 Bridge 指常驻的 Bridge 进程，任务进程只改本任务的卡和记录。

```text
<state>/
├─ 进程与身份
│  ├─ bridge.pid                   当前 Bridge 进程号（单实例锁）
│  ├─ bridge.log*、bridge-console.log*   日志，见“日志”一节
│  ├─ bot-identity.json            应用 ID、owner、profile、机器人自己的 open_id、主人的名字；状态目录和应用一一对应
│  ├─ owner-chat.json              主人和机器人的私聊（菜单、手上的事、汇总都发在这里）
│  ├─ migrations.json              已做过的旧记录迁移（core/migrate.py）
│  ├─ maintenance-status.json、backups/   上次维护结果、每日备份；`backups/memory-v2-<时间>/` 是记忆第一次迁移前的原样拷贝
│  └─ tmp/                         Bridge 和任务进程的 TMPDIR
├─ 话题与任务
│  ├─ topics/<话题>/               topic.json、history.jsonl、收到的附件 incoming_media/、换配置的交接 handoff/；话题的记忆 memory.json、它的新字段 memory-v2.json、决定记录 decisions.jsonl
│  ├─ topics/aliases/              消息和卡片 → 话题的别名
│  ├─ jobs/、queued/               任务记录；存盘的排队任务，交给任务进程时删除
│  ├─ runs/<任务键>/                任务进程的运行目录，见“任务进程”一节
│  ├─ turn-ends/                   交给任务进程、Bridge 还没做收尾的任务（任务进程退出时没有 Bridge 在看的，下次启动补做）
│  ├─ traces/<任务键>.jsonl        执行过程记录
│  ├─ checkpoints/                 每个任务的检查点（模型写）
│  ├─ questions/、held-notes/      待答问题；多题待答时暂存的补充
│  ├─ interaction-deliveries/      补充和回答的送达记录
│  ├─ delivery-receipts/           飞书写操作的幂等回执
│  ├─ inbound-messages/            收到的消息的去重标记，里面是这条消息的去向（`bin/covey_inbound.py`）
│  ├─ event-spool/                 事件长连接走 SDK 时，收下还没处理完的消息和点击（下次启动先补处理）
│  ├─ stranger-replies/            别人私聊时回过的那句（每人每天一个标记）
│  ├─ verified-results/、result-files/   已授权的回复正文、长回复文件
│  ├─ answer-cards/                单独发出的答案卡（补产物链接用）
│  ├─ fold-cards/                  带“这一轮中途的汇报”折叠的回带卡（点正文收起时改写用）
│  ├─ session_index.jsonl、system-rules-sessions/、native-session-guards/   会话指针与续接保护
│  ├─ job-diffs/                   每轮的差异报告
│  ├─ artifact-drive/、artifact-deliveries/、artifact-upload/   产物上传到云空间的记录和中转
│  └─ policy-stops/、policy-inbox/、policy-source*.json   官方拒绝的停止记录（见 policy-stop.md）
├─ 卡片与表单
│  ├─ cards/                       卡片锁（.locks/）
│  ├─ launch-forms/、switch-forms/、rewind-forms/、browse-cards/、workspace-managers/   启动卡、换配置、回退、目录浏览、工作区管理的表单状态
│  ├─ launch-defaults/、launch-defaults.json   启动卡预选：每个聊天上一次的选择；最近一次的
│  ├─ model-usage.json             最近 14 天的模型选择，决定下拉顺序和常用思考预算
│  ├─ model-settings/              话题的模型设置
│  ├─ menu-panels.json、settings-cards/、group-managers/、main-thread-panels/、weekly-usage-cards/、digest-boards/、dashboard-links/   菜单发出的各张卡
│  └─ welcome.json                 欢迎卡：发了没有、发在哪、点过哪几句示例（新部署第一次启动时写 pending；那时已有任务、话题或私聊记录的部署写 skipped，永不发）
├─ 主人的设置
│  ├─ workspace-config.json        工作区列表和默认工作区
│  ├─ groups/                      每个主人 @ 过机器人的群：是否外部群、是否对成员开放、免@
│  ├─ principles.json              主人的原则（旧版写；新版迁进 `memory/global.json` 后只读）
│  ├─ knowledge/                   知识条目（`COVEY_KNOWLEDGE` 没关才有）：`owner.jsonl`/`.md`、`workspaces/<工作区>.jsonl`/`.md`、`owner-brief.md`（旧版写；新版迁进 `memory/global.json` 后只读）
│  ├─ memory/                      记忆（`core/memstore.py`）：全局条目 `global.json`、待审核 `review.json`、旧版 Harness 记忆导入留下的 `imports.json`（不再读写）、变更记录 `log.jsonl`（留 90 天，撤销用）、短码表 `codes.json`、有记忆的话题 `topics.json`、用到的次数 `usage.json`、同类问题静默到哪天 `dedup.json`（问前查重，Bridge 写）、自定义同义词 `synonyms.json`、给网页看有没有变的哨兵 `.rev`
│  ├─ repos.json、home.json        仓库地图（`core/repomap.py`）、主目录的设置（`core/home.py`）
│  ├─ repos.md                     从仓库地图画出来给每轮看的那份（接管已有主目录时放这里，不写进主目录）
│  ├─ main-thread.json             管家和新话题的设置，子 Agent 的模型 `subagents`（`COVEY_BUTLER=0` 时设置卡没有它，默认 `light`，要跟主模型手写 `"same"`）
│  └─ redaction/                   已下线的敏感词脱敏留下的词表等，不再读写、不进备份，原样留着（[已下线的功能](legacy.md)）
├─ 可选功能
│  ├─ main-thread/                 管家的工具请求、开过的话题、回带记录（目录名和上面的 main-thread.json 沿用旧名，不改：新旧版本混跑时互相认得）
│  ├─ proxy/                       代答：settings.json、contacts/、style.md、owner-context.md、batches/、send-as-user/、web/、shots/
│  ├─ pinned-overview.json、pinned-topics.json、board-priority.json、status-cards/、topic-seen.json   手上的事和话题看板
│  ├─ top-notices.json             发在私聊顶层的告警（回复它交给管家）
│  ├─ topic-links.json             卡上 T 号的话题链接
│  ├─ revisits/                    到点回来看（`COVEY_REVISIT`）：每个话题一个，几点回来看、看什么、排出的那一轮
│  ├─ limit-notices/               几个话题同一原因停下的合并提示（`COVEY_LIMIT_NOTICE`）：每组一个，发没发过
│  ├─ digest.json                  早晚汇总
│  ├─ memory-candidates.json       旧版晚间汇总里“你反复说的”候选和主人点的「记下」「不用」（`COVEY_MEMORY` 没关；新候选进待审核，不再写这里）
│  ├─ memory-usage.json、memory-tidy.json、memory-harvest.json   旧版每条记忆最后一次被用到的时间（只读）、每天整理记忆收起了什么（撤销用，`COVEY_MEMORY` 没关）、旧版读过的 Codex 和 Claude Code 自己的记忆
│  ├─ projects/                    专项（`COVEY_PROJECTS`）：`<专项>.json`；`<专项>/` 里是记忆 `memory.json`（旧版也读写）、它的新字段 `memory-v2.json`、决定记录 `decisions.jsonl`、资料库索引 `library.json` 和复制的文件 `library/`、收件箱 `inbox/`；整理专项的记录 `tidy/`
│  ├─ owner-send/                  代发的草稿和发送记录
│  ├─ butler-asks/                 话题转给管家的事（`COVEY_PROJECTS`）：每轮一个，转了什么、发没发出去
│  ├─ patrol/                      已下线的“我本来想找你的事”留下的记录，不再写、不再读、也不再被维护清理，可以手动删
│  ├─ devices/、nodes/             多设备在本机的工作文件；节点的名单、口令和采样
│  ├─ web/                         网页工作台：网页上发来的操作的账本 `inbox.json`、推过的内容、网页开的话题
│  └─ app-setup/                   自动补齐飞书应用的状态和运行目录
└─ 后端
   ├─ backend-health/              各后端最近一次任务的结果和模型目录查询结果，doctor 从这里读
   ├─ launch-models/、native-models/   模型目录缓存、扩展的实测档位
   ├─ claude-upstream.json         `claude --gateway-status` 报的上游地址，缓存 10 分钟（`backends/claude/upstream.py`，扩展在 Claude Code 前面放本机中继时用）
   ├─ <网关名>/、<扩展>-sessions/   后端自己的会话和运行文件（<网关名>/relay.log 是模型网关的繁忙记录）
   ├─ pi-sessions/                 已下线的 Pi 留下的会话（老部署才有），不再读写，可以删
   ├─ stuck-check/、track-judge/、memory-candidates/、memory-learn/、memory-tidy/、memory-harvest/   小模型一次调用的工作目录：同时做几件事的判断、记忆自己长（stuck-check/、memory-candidates/、memory-tidy/、memory-harvest/ 旧版用过，现在不写）
   ├─ codex-judge/、agy-judge/   没给工作目录时，小模型问 Codex、Antigravity 自己的命令行用的空目录（它们的临时主目录在 tmp/ 下，问完就删）
   └─ claude-diagnostics/、<扩展>-diagnostics/   后端诊断
```

`runs/<任务键>/` 是 Bridge 和任务进程之间的协议目录，新旧 release 双向兼容；文件名和读写规则在 `core/run_dir.py`。

### 记忆的存储与迁移

记忆的每一条，不管存在哪个文件，读出来都是同一个样子（`core/memstore.py` 的 `Entry`）：三类（规矩 `rule`、决定 `decision`、资料 `note`）、三个范围（全局、专项、话题）、一个固定短码（类别字母 R/D/N 加 4~6 位，`memory/codes.json` 记着每个码指哪条，给出后不变）。

| 范围 | 存在哪 | 旧版能不能读到 |
| --- | --- | --- |
| 全局 | `memory/global.json` | 读不到；旧版只读它自己的 `principles.json`、`knowledge/` |
| 专项 | 条目照旧在 `projects/<专项>/memory.json`；旧版不认识的字段（标题、三类、谁定的、适用仓库……）在旁边的 `memory-v2.json`；新的决定记录在 `decisions.jsonl` | 条目能读到，新字段和新决定读不到 |
| 话题 | 同专项：`topics/<话题>/memory.json`、`memory-v2.json`、`decisions.jsonl` | 同上 |

新字段从不写进旧版会按自己的键整个重写的 `memory-meta.json`、`memory-usage.json`，所以回滚到旧版再升级回来，新字段、短码、收起和作废的状态都还在。用到的次数也一样：新的计数在 `memory/usage.json`，读的时候和旧的 `memory-usage.json` 合起来算。

**迁移**（`core/migrate.py` 的 `memory_v2`）在每次启动和 `covey_maintenance.py --apply` 时检查：只看几个文件的修改时间和大小（全局的旧文件、每个专项的 `memory.json`），和上次迁移时一样就什么都不做，所以不随话题数变慢；话题条目的短码在第一次被读到时才给。有变化时：

- 第一次先把旧记忆文件原样拷到 `backups/memory-v2-<时间>/`。
- 旧版的通用原则、通用知识条目进 `memory/global.json`（旧编号 `principle:p…`、`knowledge:k…` 记成别名，旧编号照样查得到）。回滚期间旧版新加的照样补进来；旧版改了的，新版这边没改过就跟着改，改过就把旧版的写法放进待审核让主人挑；旧版删了的收起（能放回）。工作区的原则和知识条目放进待审核 `memory/review.json`（旧版没处理的“你反复说的”候选不再搬进来）。
- 负责人记的约束和决定（主人没确认过）原来都当参考带。早先的迁移把近 7 天每轮实际带过的当专项规矩带（标“别人总结的”），同时放进待审核；现在不再往待审核里放（负责人记的不排队审），以前放进去的照旧能采纳（算你定的）或不要（保持原样，仍是负责人记的），14 天没处理关成过期、照旧当规矩带。

| 旧来源 | 新的类别 | 谁定的 |
| --- | --- | --- |
| 原则 `principles.json` | 规矩 | 主人 |
| 知识条目：决定、做法 / 坑、事实、待定 | 规矩 / 资料 | 主人 |
| 专项：主人的话、主人确认过的 | 规矩（主人标“只限当次”的是决定） | 主人 |
| 专项：`project_style` 的风格行（汇报风格：…） | 不算记忆，只有负责人读（和以前一样） | — |
| 专项：问题卡的回答 | 决定 | 主人 |
| 专项：负责人记的约束、决定 | 资料；早先迁移时近 7 天带过的是规矩 | 别人总结的 |
| 专项：负责人记的事实、联系人 | 资料 | 别人总结的 |
| 话题的记忆 | 规矩（话题范围） | 主人 |

删掉的条目是作废，收进历史（旧版的 `valid` 是 done 或 superseded）的是收起（决定是办完）。

**回滚**：回到旧版时它读自己的旧文件；要把迁移后新记的全局条目和决定也带回旧版，停服务后跑

```bash
python3 -B scripts/memory_migrate.py --state "$STATE_DIR" --export-legacy
```

全局规矩写回 `principles.json`、资料写回知识条目、新决定写成专项 `memory.json` 里的回答（新版读的时候不会重复）。`--dry-run` 只报会迁什么，`--apply` 立刻跑一次迁移，`--stats` 打印各范围各类的条数。

## 注册与启动服务

注册链路是 `scripts/bootstrap_harness_bridge.sh` → `bin/covey_service.py launch` → Manager。`./install` 第 5 步先注册托管（`covey_service_install.py --mode auto --activate`），再调 bootstrap；旧安装脚本 `scripts/setup_harness_bridge.sh` 也是最后调 bootstrap。已经装好的机器上，重新注册、换 release 或改参数时直接用 bootstrap（先[载入服务清单的环境](deploy.md#载入服务清单的环境)）：

```bash
./scripts/bootstrap_harness_bridge.sh --workdir /absolute/workspace --profile my-bot --app-id "$APP_ID" \
  --state-dir "$STATE_DIR" --harness-config "$COVEY_DATA_HOME/harnesses.json"
```

- `--workdir`、`--profile`、`--app-id` 必填；`--state-dir` 默认是 `<数据根>/harness-<APP_ID>`。
- 其他参数原样传给 Bridge，例如 `--owner`、`--pairing-file`、`--harness-config` 和 Codex 参数。bootstrap 总会传 `--sandbox`，默认 `danger-full-access`，见[执行权限](harnesses.md#执行权限)。
- 启动命令由当前 PATH 里 `python3` 的绝对路径和 bootstrap 所在目录的 `bin/covey_bridge.py` 组成。bootstrap 执行 `launch`，再打印 `status`。

`launch` 写入服务清单，代次加一，Manager 随后换一个 Bridge 进程。清单保存启动命令的完整绝对 argv、工作目录、profile、状态目录、日志路径、`health_timeout`、代次、期望状态（`running` / `stopped`）和一组环境变量。哪些变量会被保存见[服务清单里的环境变量](configuration.md#服务清单里的环境变量)。

## 日常操作

| 命令：`python3 -B bin/covey_service.py <命令> --state-dir "$STATE_DIR"` | 效果 |
| --- | --- |
| `status` | 打印 JSON：`registered`、`desired_state`、`status`（Manager 观测的状态和 Bridge 健康快照）、`alive`、`restart_blockers` |
| `stop` | 先把期望状态持久写成 `stopped`，Manager 向 Bridge 的进程组发 SIGTERM，15 秒后仍在就 SIGKILL。命令最多等 25 秒确认，否则报 `service stop not confirmed`。机器重启后仍保持停用 |
| `start` | 期望状态写回 `running` |
| `restart` | 代次加一，Manager 换一个 Bridge 进程 |
| `set KEY=VALUE …`、`unset KEY …`（可加 `--dry-run`） | 只改服务清单里这几个变量，按原命令重新注册（代次加一），见[配置 · 改开关三步](configuration.md#改开关三步) |

- `stop`、`restart` 和部署都不结束任务进程。机器人停着期间任务照常执行、刷新卡片，但点卡片和发消息没有响应。
- Bridge 没有热重载，`SIGHUP` 不触发重新执行；换代码用 `restart` 或部署。

工作区列表平时在飞书的设置卡里管理（见[选择和管理工作区](usage.md#选择和管理工作区)），服务器上也可以：

```bash
python3 -B bin/covey_workspaces.py --state-dir "$STATE_DIR" list
python3 -B bin/covey_workspaces.py --state-dir "$STATE_DIR" add 训练代码 /absolute/path/to/training
python3 -B bin/covey_workspaces.py --state-dir "$STATE_DIR" default 训练代码
python3 -B bin/covey_workspaces.py --state-dir "$STATE_DIR" remove 旧项目 另一个
python3 -B bin/covey_workspaces.py --state-dir "$STATE_DIR" prune
```

`add` 的路径必须是已存在的目录，名称 1 至 40 个字符。设备上的工作区（`COVEY_DEVICES=1`）在飞书里添加，命令行不认 `别名:路径`。`remove` 只从列表移除，不删目录；默认工作区要先把别的设为默认才能移除。`prune` 移除确认已不存在的目录。改动只影响之后开始的话题，实时生效。状态目录里还没有 `workspace-config.json` 时，命令会用当前目录建一个默认列表，所以先确认 `--state-dir` 写对了。

### 载入服务清单的环境

重新注册时清单里的变量整组换成终端里的，所以改开关、手工升级（用 `scripts/upgrade.sh` 时它自己照清单载入，不用做）之前先把清单的变量载入终端，命令见[升级与部署 · 载入服务清单的环境](deploy.md#载入服务清单的环境)。

### 改设置后重新注册

改了开关或 Codex 参数要重新注册（`restart` 不换清单），做法见[升级与部署 · 改设置后重新注册](deploy.md#改设置后重新注册)。

### 扩展的装和卸

扩展是带清单（`manifest` 模块）的包。Bridge 在运行的代码目录的 `extensions/` 下找它（release 由 `deploy_bridge.sh --ext-repo` 拼进去，数据根 `extensions/` 下的个人扩展检出也每次自动拼进去，见 [deploy.md 个人扩展](deploy.md#个人扩展)；`./install` 的检出里按扩展自己的安装说明放），启用哪些看两份名单：代码目录里的 `extensions/enabled`（release 自带的默认），加上数据根的 `extensions/enabled`（本部署的增减，跨部署保留）。

```bash
python3 -B bin/covey_plugins.py list          # 找到了哪些、哪些启用、来自哪份名单
python3 -B bin/covey_plugins.py enable <包>   # 先按 Bridge 的方式试加载，加载不了或和别的冲突就不写
python3 -B bin/covey_plugins.py disable <包>
python3 -B bin/covey_plugins.py doctor        # 启用的扩展能不能一起加载
python3 -B bin/covey_plugins.py deploy-check  # 启用的扩展都试导入一遍、再自己检查这份代码能不能在本机跑（部署预检也跑它）
python3 -B bin/covey_service.py restart --state-dir "$STATE_DIR"   # 改了名单要重启 Bridge 才生效
```

- `enable`、`disable` 只写数据根的名单，不动代码目录和服务清单，所以之后的部署仍按这个选择。
- 环境变量 `COVEY_EXTENSIONS`（名单）和 `COVEY_EXTENSION_PATH`（搜索目录）会盖过上面两份名单，只给开发测试用；服务清单里有它们时 `deploy_bridge.sh --ext-repo` 的预检会拦下（`pre.extension_env`）。
- 扩展的安装钩子（`<包>/scripts/setup_hook.sh`）在 `./install` 第 3 步运行，所以新装时先放好、`enable`，再运行 `./install`。
- 机制和规则见[架构 · 扩展](architecture.md#扩展)。

### 设备自检

开了多设备（`COVEY_DEVICES=1` 和 `devices.json`，见[配置](configuration.md#多设备开发中默认关)）后，新加一台设备、改了它的 ssh 或代理设置、设备上重装了它声明的 Harness（Claude Code、Codex、Antigravity）以后，在 Bridge 所在机器上跑一次自检：

```bash
python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1          # 逐步打印，最后一段是结论
python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1 --json    # 一行 JSON
python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1 --no-probe  # 不真跑最小请求
python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" list                 # 每台设备最近一次自检，不碰设备
```

步骤是 `connect`（5 秒、ssh BatchMode）、`shim`（推设备端程序，声明了 Codex 就再推读取器）、`hello`、`profile`（环境档案）、`tools`（声明的 Harness 都装了）、`probe:<Harness>`（真跑一次最小请求，60 秒，带档案里的环境，会花一点点模型额度；没有最小请求的 Harness 只查程序，这一步写 `found, no minimal request`；Antigravity 另看设备上的登录文件，没登录这一步不过）。退出码：0 全部通过，2 有一步没过，3 多设备关着或别名不在 `devices.json` 里。结论写进状态目录 `devices/<别名>/profile.json` 的 `check`，设置卡「工作区」页的设备状态行和 `covey_doctor.py` 报告里每个服务的 `devices` 读的就是它（doctor 只读，不去连设备；设备没通过自检只报 `unverified`，不会让服务变成 `degraded`）。

常见失败：「连不上」先在 Bridge 机器上手动 `ssh <host>` 一次（BatchMode 不会问密码，也不会确认主机密钥）；「没有登录」在设备上登录对应的 Harness；「连不上模型服务，而且环境档案里没有代理」在设备的 `~/.claude/settings.json` 的 `env` 里设 `HTTPS_PROXY`，或写进 `devices.json` 里这台设备的 `env`，再自检。

### Harness 能不能调通

`core/harness_probe.py` 的 `probe(名字)` 对 Claude 跑 `claude -p`、对 Codex 跑 `codex exec`（短超时，一次最便宜的问法），返回 `ok`、`reason`（`ok|not_installed|not_logged_in|network|timeout|other`）和 `detail`。安装器、`covey_doctor --probe-harnesses` 用它；`covey_doctor` 不带参数时只读缓存（`<数据根>/harness-probe/<名字>.json`，通过 6 小时、失败 10 分钟，换了命令或代理设置作废）。Bridge 里的可用性检查不做真实调用，只看本机命令和登录；`COVEY_HARNESS_PROBE` 这个开关已经没有了。

### 问题卡问得多不多

改了话题什么时候问主人的规则（[问题与回答 · 什么时候问](architecture.md#什么时候问)）以后，用同一个脚本比改之前和之后各一段时间。只读，不改状态目录：

```bash
python3 -B scripts/question_stats.py --state "$STATE_DIR" --window 2026-11-11:2026-11-25 --window 2026-12-01:2026-12-07
python3 -B scripts/question_stats.py --state "$STATE_DIR" --window 2026-12-01:2026-12-07 --daily   # 再按天各一行
python3 -B scripts/question_stats.py --state "$STATE_DIR" --json                                   # 最近 7 天，JSON
ssh <host> python3 - --state /absolute/path/to/state --window 2026-12-01:2026-12-07 < scripts/question_stats.py  # 别的机器，只用标准库
```

每个时间窗（本地日期，两头都算）一段：

| 数 | 怎么算 |
| --- | --- |
| 问题卡、卡/天 | 时间窗里建的问题记录（`questions/*.json`），一条记录是一张卡，不管几道题 |
| 每百轮 | 问题卡数 ÷ 同期轮次（`jobs/*.json` 的 `created_at`）× 100；用得多的那周不会显得问得多 |
| 选推荐率 | 答案带“推荐”的题 ÷ 全部题。可撤回、有推荐的不再问以后，它和“其他”类的题数应该一起降 |
| 不回答 | 答“不回答此题”的题（点不处理的也算） |
| 第一轮就问 | 话题第一轮就发的卡，方案还没给就先问的多在这里 |
| 负责人替答 | 专项负责人分流替主人答掉的卡 |
| 按类别 | 每道题按标题和题面的词归到部署、删除、花钱、对外发送、占机器（这几类照常问），都不像的是其他；只是粗分，前后用的是同一套词 |

读记录有总时限（`--seconds`，默认 60），网络盘上卡住没读到的记成“未读”另行列出，这时的数偏少。

## 日志

| 文件 | 谁写 | 内容与轮转 |
| --- | --- | --- |
| `<state>/bridge.log` | Bridge | Bridge 拿到状态目录后的全部输出。每分钟看一次大小，达到 10 MiB 轮转，保留 `.1` 到 `.9`；启动时不轮转 |
| `<state>/bridge-console.log` | Manager | Bridge 接管日志之前的输出（导入出错、`another bridge already owns this state directory`） |
| `<root>/manager.log` | Manager | 每次启动、替换、停止 Bridge，退避重启，健康超时替换，处理服务出的新错误，各一行 `service <id 前 8 位> …`；10 MiB 轮转，保留 9 份 |
| `<state>/runs/<任务键>/runner.log` | 任务进程 | 任务进程的输出，随运行目录清理 |
| `~/.lark-cli/events/<APP_ID>/bus.log` | lark-cli 事件总线 | Bridge 在达到 10 MiB 时轮转 |
| `<root>/memory-guard/events.jsonl` | 内存看门狗 | 终止、告警、包装维护和错误 |

运行中的轮转是复制后截断，轮转瞬间可能丢几行；任务、授权结果和投递回执另有持久记录，不依赖日志。每行开头的时间精确到毫秒。日志里只有操作名、ID 末几位和数字，没有正文、令牌或用户文字。

每次启动的第一行是 `bridge started pid=… generation=N release=…`：代次和上一次相同，是 Manager 替换了退出或没有响应的 Bridge；加了一，是部署或 `restart`。

`bridge.log` 里常用的关键字：

| 关键字 | 含义 |
| --- | --- |
| `bridge started pid=` | 每次启动的第一行：进程号、Manager 代次、release |
| `ready event_key=` | 消费者就绪，每个订阅一行 |
| `event websocket connected; events wait for the start to finish` | SDK 收事件时，启动中先连上了：之后的事件排队，消费者就绪（上一行）后处理 |
| `event websocket: stopping with events not handled` | SDK 收事件时，停止时还有收下没处理的事件（按 key 计数），这些点击或消息要重新发 |
| `consumer exited rc=` | 事件消费者退出，同一行写着多久后重启 |
| `card action kind=` | 每次卡片点击一行，见[点按钮没有反应](troubleshooting.md#点按钮没有反应) |
| `card follow-up kind=` | 点击回执之后的慢活做完一行，`elapsed_ms` 是它自己的耗时 |
| `bot menu key=` | 每次菜单点击一行，`outcome` 是 `not_owner`、`unknown_key`、`duplicate`、`no_chat` 或 event_key 本身 |
| `timing flow=` / `feishu call` / `feishu calls` | 计时行，见[慢在哪里](troubleshooting.md#慢在哪里) |
| `task runner started` / `adopted task runner` / `task runner done` / `task runner ended` | 任务进程的启动、认领、放开话题、退出 |
| `re-queued job=` | 重启后重新排队的任务 |
| `released N waiting messages of started topic=` | 启动时把已开始话题里还在等启动的消息排上 |
| `card follow-ups not finished at stop:` | 停止时等了 6 秒仍有点击后续没做完；受影响的问题在下次启动时显示“送达未确认” |
| `launch catalog unavailable` | 模型目录读取失败，见[启动卡里缺少某个 Harness 的模型](troubleshooting.md#启动卡里缺少某个-harness-的模型) |
| `event bus TCP transport stalled` | 事件总线停滞后被替换，见[自动恢复](troubleshooting.md#自动恢复) |
| `worker <名称> failed` / `maintenance deferred` | 后台线程异常并重建 / 维护出错推迟 |
| `<isolate> step <模块>.<函数> failed: OSError: Disk quota exceeded` | 后台循环的一步失败：OSError 带上原因（不带路径），别的异常只写类型。写不进（Disk quota exceeded、No space left on device）另记下来，私聊里告诉主人一次（6 小时内最多一次，`app/storage_alerts.py`），`covey_doctor.py` 也说，见[状态目录写不进](troubleshooting.md#状态目录写不进) |
| `slow <什么>: N s so far, at <调用栈>` | 一次等锁、持锁或读记录超过 2 秒（`core/storage_watch.py`：`LOCK`、记录和卡片的文件锁、记录读盘），记一次调用栈；`covey_doctor.py` 说最近 6 小时有几次、最长多久 |
| `backend startup failed job=#N harness=<名字>: <类型>: <原文>` | 后端启动检查或启动失败，原文经脱敏、截到 160 字；卡上同样写“后端初始化失败：<类型>: <原文>” |
| `backend startup notice held job=#N: …` | 专项负责人的唤醒轮后端没起来，它前一轮也没起来：不再回“后端暂不可用”、不发恢复卡，巡检隔 1、3、10 分钟再叫醒试（见[架构 · 专项护栏](architecture.md)） |
| `project wake held project=<id> why=retry\|rest\|quota\|down until=HH:MM:SS` | 负责人这时叫醒只会失败（限流等重试、额度用完、网关额度 hold、后端起不来），先不叫醒，回执留在收件箱；开始等或等的东西变了时记一行 |
| `maintenance backup completed` / `idle maintenance … completed` | 每日备份 / 空闲时的清理 |
| `… is the old name of …` / `env <名字> is no longer read` | 设了环境变量的旧名（见[改名与兼容](compat.md#环境变量的旧名)）/ 不再读取的变量（见[已下线的功能与旧配置](legacy.md#不再读取的变量)） |
| `app setup check` / `app setup run` | 飞书应用检查和自动补齐，见[飞书应用配置](feishu-app.md#自动补齐飞书应用) |
| `outcome=retired_harness` | 已下架 Harness 的话题收到消息或点击，见[已下线的功能与旧配置](legacy.md) |
| `proxy …` | 代答，见[代答没有回复或没有发出](troubleshooting.md#代答没有回复或没有发出) |

汇总计时：`python3 -B scripts/bridge_timing.py --state "$STATE_DIR" --since 19:30`，见[慢在哪里](troubleshooting.md#慢在哪里)。

## 备份、归档与维护

Bridge 的 maintenance 后台线程每小时检查一次（`core/maintenance.py`，计划里每类落盘数据一条规则 `RULES`）：

- **备份**每天一次，有任务运行也做（备份只读、不持锁）。日志 `maintenance backup completed: bridge-<时间>.tar.gz; jobs are running, the cleanup waits until none is`。
- **清理**在没有在管的任务（排队、运行中或已认领的任务进程都算）、并且距上次清理满一天时执行，完成时记 `idle maintenance backup/archive completed` 和一行 `maintenance removed ...` 计数。备份期间有任务开始时，其余清理顺延到第二天（`cleanup deferred`）。
- **代答网页端登录的缓存**每次检查都看，超过 200 MiB 才删，见[代答](configuration.md#代答)。

一次清理做这些：

| 数据 | 规则 |
| --- | --- |
| 话题历史 `history.jsonl`、`job-progress.jsonl` | 活跃任务所在的话题不动；其他的达到 1 MiB 或 30 天未更新时压缩进同目录的 `<文件>.archive/`，读历史时照常包含 |
| 检查点 | 不在活跃状态、30 天未更新的压缩为 `.json.gz`，仍可用于“检查并继续” |
| 执行过程 `traces/<任务键>.jsonl` | 任务已结束、30 天未更新、任务进程也不在的压缩为 `.jsonl.gz`，不删除；先写好并落盘 `.gz` 再删原文件 |
| 任务记录 | 已结束的删掉没有代码再读的字段，其他字段和 `updated_at` 不变（`records_slimmed=`）；`jobs/`、`questions/`、`held-notes/` 的 `.journal` 超过 1 MiB 时删掉重来 |
| `tmp/`、`artifact-upload/`、`inbound-messages/`、`stranger-replies/`、`event-spool/`、`turn-ends/`、`proxy/shots/`、`main-thread/opened/`、`main-thread/relays/`、`main-thread/dispatch/`、`main-thread/tracks/`、`fold-cards/` | 7 天内没有改动、也没有进程用着的整个删除 |
| 同时做几件事里这些事用过的 Claude 会话文件 | 按 `main-thread/tracks/` 账本里的 `session_id`，那一轮结束满 3 天、文件 3 天没改没读的删（`remove_track_sessions`）；不在账本里的不动 |
| `runs/` 运行目录 | 任务进程已不在、最新文件早于 7 天的删除；任务记录和执行过程保留 |
| 代答批次 `proxy/batches/` | 只删已结束且 30 天没改动的 |
| 专项记录 `projects/`（`COVEY_PROJECTS`） | 只删状态是 `done` 或 `cancelled` 且 180 天没改动的，连同它的 `projects/<id>/` 目录（`remove_projects`）；`active`、`paused` 的不动 |
| 专项的记忆和资料库 `projects/<id>/memory.json`、`library/`（`COVEY_PROJECT_MEMORY`） | 已结束（做完、已取消）的专项，记忆条目写下 180 天后删（含墓碑，风格行留着，`trim_project_memory`；在做和停住的不动）；专项 `done` 且记录 30 天没改动后，资料库的文件删掉、索引留着名字和 sha256（`purge_project_files`）；单个专项的文件超过 200 MiB 时从最旧的删（话题的产物在前，主人发的文件在后）。备份收专项记录、记忆和资料库索引，不收资料库复制的文件 `library/` |
| 锁文件 `.locks/` | 记录已不存在或 7 天未改动的删除；`cards/.locks/` 里没人持有的都删 |
| `job-diffs/` | 超过 30 天的删除；总量超过 200 MiB 时从最旧的删，但只删云空间上传已确认的，一天内写的不删 |
| 备份 | 最多 7 份、合计不超过 400 MiB，超出时从最旧的删，最新一份总保留；重启打断留下的 `.tmp` 一天后删 |
| 记忆的文件 `memory/`、`decisions.jsonl` | 变更记录 `memory/log.jsonl` 只留 90 天；每个 `decisions.jsonl` 把同一条决定的几行合成一行，作废满 30 天的去掉；`memory/topics.json`（有记忆的话题）照话题目录重做一遍，这是唯一一处逐个看话题目录的地方，启动时不看 |
| 陈旧记录 | 等待工作区、等待回答的任务和待回答的问题超过 7 天没有动静时关闭，原卡改成“超过 7 天未处理，已自动关闭；需要继续请在本话题直接发消息。”；启动卡没送达的消息 1 天没有动静就关闭。送达未确认的问题和在跑的任务不关。每次关闭记一行 `stale ... closed` |

维护不删除工作区目录或用户产物。

**备份包含**话题（历史、Session 指针、收到的附件）、任务记录、排队任务、执行过程、检查点、问题、暂存的补充与投递记录、已验证结果和产物投递记录、启动卡和工作区管理卡状态、启动卡预选、`model-usage.json`、群记录、模型设置、后端观测，主人的原则 `principles.json`、知识条目 `knowledge/`、记忆 `memory/`、仓库地图 `repos.json` 和主目录设置 `home.json`、管家设置 `main-thread.json`、专项 `projects/`（不含资料库复制的文件）、手上的事和话题看板的记录（`pinned-overview.json`、`pinned-topics.json`、`board-priority.json`、`topic-seen.json`、`status-cards/`）、`digest.json`、私聊顶层告警的记录 `top-notices.json`、欢迎卡的记录 `welcome.json`、记忆的候选和记忆自己长的记录（`memory-candidates.json`、`memory-usage.json`、`memory-tidy.json`、`memory-harvest.json`）、网页工作台的操作记录 `web/`、`owner-chat.json`、`migrations.json`，以及 `workspace-config.json`、`session_index.jsonl`、`bot-identity.json`，还有代答的 `proxy/settings.json`、`proxy/contacts/`、`proxy/style.md`、`proxy/owner-context.md`（清单是 `core/backup.py` 的 `BACKUP_DIRS`、`BACKUP_FILES`）。

开了 `COVEY_WEB_SELFHOST=1`（并且 `COVEY_WEB=1`、`COVEY_WEB_CONFIG` 指向本机回环）时，每日备份还多一个成员 `selfhost-web/pglite.tar.gz`：向网页进程要的数据库导出（PGlite 的 `dumpDataDir`，几 MB，几秒；超过 256 MiB 不收），这一项做得成做不成都记在 `maintenance-status.json` 的 `web_backup`（`at`，`bytes` 或 `skipped`/`error` 的原因），不影响状态目录的备份；外部 Postgres 用 `pg_dump` 备份，不在这里。没开自托管时备份内容和状态文件和原来一样。网页的开放接口密钥文件和 TLS 私钥不进备份。状态目录的恢复（`restore_backup`）不碰 `selfhost-web/`；网页数据库用 `python3 -B -m covey.service.web_selfhost --restore-db <备份包或 pglite.tar.gz>` 恢复，见[网页工作台 · 备份与恢复](web.md#备份与恢复)。

**不包含**凭据（主人的网页端登录 `proxy/web/`、代答发送工具的 `proxy/send-as-user/`、节点的口令）、代答的截图和批次记录、专项资料库复制的文件、工作区源码与产物、各 Harness 自己的会话存储、`runs/`、扩展后端的会话目录（如 `<扩展>-sessions/`）、`job-diffs/`、已发出卡片的表单状态和日志；上面状态目录里的每一项不进备份的原因写在 `core/backup.py` 的 `NOT_BACKED_UP`，新加的状态文件要在那里或备份清单里写明（`tests/test_maintenance.py` 的 `BackupListTest` 核对）。完整的灾难恢复要另外保留这些。备份和归档含对话内容，要保持私有。

```bash
python3 -B bin/covey_maintenance.py --state-dir "$STATE_DIR"            # 预览维护计划（JSON）
python3 -B bin/covey_maintenance.py --state-dir "$STATE_DIR" --dry-run  # 逐项预演，带大小和文案
python3 -B bin/covey_maintenance.py --verify /absolute/bridge-NNN.tar.gz
python3 -B bin/covey_maintenance.py --restore /absolute/bridge-NNN.tar.gz --destination /absolute/new-state
```

- 预览和 `--dry-run` 不拿锁、不写任何东西，可以在服务运行时执行。`--dry-run` 写出备份的文件数和大小、要删的旧备份、每个归档和删除的条目、每条陈旧记录的任务号和卡上的改动。
- `--apply` 只能在服务停止时手工执行；Bridge 在运行时拒绝。`--apply` 和 Bridge 启动时都先跑一次旧记录迁移（`core/migrate.py`），做完记进 `migrations.json`，日志 `record migrations …`；写不成的任务键用 `failed_keys=` 列出，之后每次启动重试。
- `--verify` 按同名 `.manifest.json` 逐文件核对哈希。`--restore` 先校验，目标必须是尚不存在的新目录；它按 `.metadata.json` 记录的源状态目录改写恢复出的 `.json` 里的绝对路径，不启动服务、不重新提交任务。`.tar.gz`、`.manifest.json`、`.metadata.json` 三个文件放在一起保存。切换到恢复出的状态目录前，确认 Harness 会话存储、工作区和凭据仍然可用。

## 任务进程

每个任务出队后在自己的任务进程里跑完：后端、任务卡和结果投递都在这个进程里。它在自己的会话里运行，用启动它的 Bridge 的环境和 release，停止或重启 Bridge 都不会结束它。实现见[任务生命周期](architecture.md#任务生命周期)。

`pgrep -af covey_runner.py` 列出任务进程，命令行形如 `<release>/bin/covey_runner.py --run-dir <state>/runs/<任务键>`。运行目录里有：

| 文件 | 内容 |
| --- | --- |
| `runner.json` | 进程号、启动时间、boot ID、release、任务编号、Harness |
| `wake.json` | 任务进程能接 SIGUSR1：Bridge 写收件箱后用它立即唤醒（旧 release 的任务进程没有它） |
| `runner.lock` | 运行锁：跑这个任务的任务进程一直持着，同一任务的第二个任务进程拿不到就什么都不跑（空文件；设了 `COVEY_LOCK_ROOT` 时在那里，不在运行目录） |
| `done.json` | 卡片更新完、只剩产物上传时写入；Bridge 见到就放开这个话题，同话题的下一条照常开始 |
| `exit.json` | 退出时写入 |
| `runner.log` | 任务进程的日志 |

- 运行目录在任务结束 7 天后由空闲维护删除。单看 `runner.json` 判断不了进程是否还活着，要看 `exit.json` 或进程列表。
- 任务结束后，任务进程还会等产物上传最多 30 分钟、卡片更新最多 60 秒再退出。
- Bridge 一侧的日志：`task runner started job=#N pid=... dir=...`（交给任务进程）、`adopted task runner job=#N pid=...`（重启后认领仍存活的）、`task runner done job=#N`、`task runner ended job=#N status=... exit=...`（`exit=none` 表示没有 `exit.json`，进程被杀或崩溃）。`task runner of job #N still alive: watched again` 是看着像退出了、再问时还活着（读 `runner.json` 一时失败），没有再起第二个；`task runner relaunched job=#N` 是任务没开始就退出、再起了一次。
- 任务进程一侧（`runner.log`）：`task runner job=#N pid=... ran nothing: another task runner holds its run directory` 是同一任务已有一个任务进程持着运行锁在跑，这个什么都没做就退出了，任务和卡不受影响；`runs without a run lock` 是锁文件建不出来或文件系统不支持加锁，照旧跑（没有运行锁之前就是这样）。同一任务的 `runner.log` 里出现两行 `task runner job=#N harness=… pid=…`（两个不同的 pid）说明有两个任务进程都跑了它，属于故障，查 Bridge 日志里同一任务的 `relaunched` 行。
- 管家空闲时的压缩进程也是 `covey_runner.py`，命令行带 `--run-dir <state>/main-thread/compacting/<管家话题> --compact-only`：不建任务、不出卡、不算 `restart_blockers`。Bridge 重启时停掉上一个 Bridge 留下的这种进程，请求放回去。

操作：

- **结束任务进程**：`kill -TERM <pid>`。它结束自己的后端进程后退出，任务按中断处理，不算“已停止”：Bridge 把卡片换成带“检查并继续”的恢复卡，60 秒后自动检查并继续一次（见[自动重试](reference.md#自动重试)）。要按“已停止”结束，点任务卡上的“停止”。后端已经做完、正在发结果时收到它，结果照常发出。
- **不要用 `kill -9`**：任务进程来不及结束自己的后端进程；Bridge 同样把任务标为中断。
- **命令留下的进程**：任务进程给后端和它的命令都加了 `COVEY_TASK_DIR=<运行目录>`。每轮后端关闭后，带这个标记、仍在运行的搜索工具被结束，其他进程只在任务卡的执行过程里列出，`runner.log` 有一行 `task leftovers job=#N stopped=… pending=… running=… pids=…`。找某个任务留下的进程：`grep -lza "^COVEY_TASK_DIR=$STATE_DIR/runs/<任务键>$" /proc/[0-9]*/environ`；结束时按进程组（`kill -TERM -- -<PGID>`）。
- **边界**：`covey_doctor` 不检查任务进程。机器人停着时任务进程照常刷新任务卡，但点击和消息都由 Bridge 接收，这段时间点卡片没有响应。

### 多设备：设备上的任务没停掉

只在 `COVEY_DEVICES=1` 且配了设备时才有这些。现象是卡上写「失联」「已请求停止，gpu1 连上后生效」，或同一话题的新一轮因「上一轮可能还在运行，没能停掉」没有启动：

1. 看 `<状态目录>/devices/<别名>/pending-reap.json`：有条目说明设备上的任务还没确认停掉。`tries` 在涨、`last_try` 在更新说明 `devices` 后台线程在试；`health.json` 的 `ok` 为 `false` 是设备连不上，先修 ssh（`ssh <host> true`，`known_hosts`、跳板、代理），连上后一分钟内自动收割。
2. `bridge.log` 里 `devices: <别名> reaped N pending` 是收割结果；`reap` 一直被拒（设备上进程组不是这个任务的，常见于设备重启后进程号被别的进程用了）时，登到设备上确认 `~/.covey/tasks/<hub-id>/<任务键>/state.json` 的进程组已经没有 Agent，再从 `pending-reap.json` 和 `tasks/<话题>.json` 里删掉对应条目（改文件前先停 Bridge 或确认没有任务在跑）。
3. 设备磁盘上 `~/.covey/checkpoints/`（检查点镜像）或 `~/.covey/incoming/`（传到设备的附件）变大：后台线程每小时 `gc` 一次，7 天没变、也不属于运行中任务的才清；设备一直离线就不会清。
4. 设备失联后任务记录是 `interrupted`、`interrupted_by` 为 `device_lost`，不会自动重试；设备回来后点卡上的检查并继续即可（会先收割再 `--resume`）。耐心值怎么改见[多设备](configuration.md#多设备开发中默认关)的字段表。
5. 回滚到旧 release（[升级与回滚](deploy.md#升级与回滚)）：设备话题的 `topic.json` 里存的是 `gpu1:/路径`。不认设备的旧 release（或回滚时没带 `COVEY_DEVICES=1`）把它当一个不存在的相对路径，这一轮被拒绝（「本话题的工作目录不存在，后端没有启动」），不会在本机跑；带着开关的旧 release 按设备规则拒绝或照设备跑，取决于它有没有这一版的设备代码。回滚后设备上还在跑的 Agent 不受影响，也不会被停掉：升回来后同一话题的下一轮会先 `reap` 再 `--resume`。跨版本检查的 `device-rollback` 场景（[测试](testing.md#任务进程协议与跨版本检查)）每次发布前验证这一点。

## 内存看门狗

防止搜索命令吃满机器内存：一个递归遍历 NAS 的 `rg --files` 能在几分钟内占到上百 GiB。看门狗是 `covey/service/memguard.py`，只用标准库，告警由 Bridge 的 `app/memguard_alerts.py` 发。

- 看门狗是事后兜底；事前有[主目录搜索垫片](harnesses.md#执行权限)：`<数据根>/bin` 里的 `rg`、`find`、`fd`、`du`、`tree`、`grep -r`、`ls -R` 在每个本机 Harness 进程里拒绝不限深度地递归搜整个主目录（出错放行，程序自己递归的拦不住）。
- `enable` 把它复制到本地盘（`~/.local/libexec/lark-bridge/memguard.py`）后从那里运行，NAS 卡住时照常工作。
- 监督进程（`daemon`，abstract socket 保证一台机器一份）拉起工作进程（`run`）：工作进程退出就重启，心跳停 90 秒就替换，监督进程没了它也退出。
- Manager 每 30 秒看一次，已启用而没在运行时拉起它（最多每分钟一次），所以机器重启后它随 Manager 启动。

工作进程每 2 秒扫一遍 `/proc`：

| 规则 | 条件 | 动作 |
| --- | --- | --- |
| `search_rss` | 搜索工具（rg、grep、find、fd、ag、ack、ugrep、tree、du、locate 等）RSS 超过终止线 | 抓现场后 SIGKILL |
| `orphan` | 搜索工具已被 PID 1 收养、运行超过 120 秒，且 stdout 是没有读端的管道 | 抓现场后 SIGKILL；`find -exec/-delete` 等不处理 |
| `big_process` | 其他进程越过告警线 | 只抓现场和告警，从不发信号 |
| `container` | 容器匿名内存超过 80%（回落到 70% 以下后再次生效） | 告警，列出占用最多的进程 |
| `long_search` | 搜索工具运行超过 2 小时 | 只告警 |

- **会终止进程的两条默认关闭**：`kill_enabled` 为 false 时它们只抓现场和告警。`enable --kill on` 打开。
- **阈值**：不设时按容器内存上限算，终止线 3.5%（至少 1 GiB），告警线 14% / 28% / 56%；读不到上限时用 8 GiB 和 32 / 64 / 128 GiB。`--search-kill-mib`、`--alert-levels-mib` 写死。
- **rg 上限**（`rg_guard`，默认关闭，`enable --rg-guard on`）：把 Codex 自带的 `codex-path/rg` 换成 sh 包装，原程序移到同目录 `.real/rg`。包装设 4 GiB 地址空间上限和 1 字节 core 限制后 `exec` 原程序，失控的遍历几秒内自己退出（SIGABRT，退出码 134，`memory allocation of N bytes failed`）。`RG_GUARD_AS_MIB` 临时改上限（0 不限），`RG_GUARD_TIMEOUT` 加墙钟上限。Codex 重装会放回原程序，看门狗每 60 秒检查，文件稳定 120 秒后重新包装（每小时最多 3 次）。
- **现场**：进程状态与内存分类、各后台线程的 state/wchan/当前系统调用、前 256 个 fd、cwd、脱敏后的命令行、祖先进程链、任务标记、容器 cgroup 计数和 PSI。环境变量只读 `COVEY_TASK_DIR` 这一项。发信号前重新核对 PID 和启动时间；PID 1、看门狗自己和它的监督进程从不处理。
- **告警**：每条写进数据目录的 `alerts.jsonl` 和 `outbox/`。Bridge 每 5 秒取 `outbox/`：进程带本机器人某个任务的标记，发到那个任务的话题并注明任务号；没有标记的发到主人私聊（还不知道私聊时等着，超过一天丢弃）；别的机器人的任务留给它。每小时最多 6 条。

```bash
python3 -B bin/covey_memguard.py status                    # 监督进程、工作进程、心跳、rg 包装状态（只走本地 socket）
python3 -B bin/covey_memguard.py enable [--kill on|off] [--rg-guard on|off] [--search-kill-mib N] [--alert-levels-mib A,B,C] [--dry-run]
python3 -B bin/covey_memguard.py disable                   # 停止并禁用，Manager 不再拉起；文件和数据保留
python3 -B bin/covey_memguard.py uninstall                 # 停止并删除本地副本和配置
python3 -B bin/covey_memguard.py rg-guard status|install|uninstall   # uninstall 把原程序放回原处
```

- `enable` 安装或更新本地副本和配置（`~/.config/lark-bridge-service/memguard.json`，写出全部生效的设置），再重启看门狗；只改给出的选项。
- 数据目录默认是服务根下的 `memory-guard/`（`--data-dir` 改），必须是 Bridge 能读到的位置：`captures/*.json`（最多 300 份或 256 MiB）、`events.jsonl`（超过 16 MiB 轮转）、`alerts.jsonl`、`outbox/`、`status.json`（每 30 秒的心跳）。数据目录所在的挂载和 `enable` 时不同（NAS 掉了）时只丢弃记录，不写到本地盘，也不耽误终止。
- 命令以 137 结束、或 rg 以 134 结束时先查 `events.jsonl`：多半是遍历的范围太大，应缩小范围，不要原样重跑或调大上限。
- 边界：卡在 D 状态（例如 NAS 上）的进程要等它离开内核等待才会被 SIGKILL 结束，记为 `kill_pending`；只看得到本容器 PID namespace 里的进程。

## 服务托管

注册服务和托管是两件事：服务清单记着“跑什么”，托管让 **Manager** 开机后自动起来、挂了自动拉起（Bridge 和任务进程由 Manager 管）。托管只放 Manager，所以停掉或重启托管单元不会结束 Bridge 和任务进程，下一个 Manager 接管。安装器 `bin/covey_service_install.py` 的 `--mode`：

| `--mode` | 做什么 |
|---|---|
| `auto` | 自动选下面一种，输出里的 `auto_reason` 写为什么 |
| `systemd` | Linux 用户服务 |
| `launchd` | macOS 用户代理 |
| `cron` | Linux 没有 systemd 时：crontab 的一行 `@reboot` 拉起 Manager |
| `manager` | 不托管：只有 `covey_service.py launch` 起的 Manager，机器重启后不会自己回来 |
| 扩展的模式 | 扩展用 `service_hosting` 钩子提供的托管方式（如 PID 1 里的守护线程），见扩展 README |

**`auto` 的选择顺序**：① 扩展的 `service_hosting` 钩子有 `auto_mode()` 且它返回一个存在的模式（扩展在它认得的平台上返回自己的托管模式）；② macOS 且有 `launchctl`：`launchd`；③ Linux 且 `systemctl --user show-environment` 答得上（有用户 systemd）：`systemd`；④ Linux 有 `crontab`（且没加 `--no-autostart`）：`cron`，给用户的 crontab 加一行带 `# covey-bridge-autostart` 注释的 `@reboot ... covey_service.py --root <root> run`（日志 `<root>/cron-manager.log`，每个服务根一行，注释里带服务根的短哈希：同一服务根重复运行替换自己的行，不同服务根共存，`--uninstall` 只删自己的；机器的 cron 守护要在跑）；⑤ 其他：`manager`，输出 `autostart: false` 和一句说明，之后重启机器要重跑安装。没有扩展时只走 ②③④⑤。

```bash
python3 -B bin/covey_service_install.py --root "$SERVICE_ROOT" --mode auto --activate
python3 -B bin/covey_service_install.py --mode auto --status      # 现在注册和运行的样子
python3 -B bin/covey_service_install.py --mode auto --uninstall   # 删掉单元或 plist（auto 要先选出 systemd 或 launchd）
```

不带 `--activate` 只写文件，不启动、不加载。重复运行安全：launchd 先 `bootout` 旧的再 `bootstrap`，systemd 重写单元后 `daemon-reload`。回执写在 `<root>/boot-installation.json`，`covey_doctor` 的 `boot` 一节按它读：systemd 看单元是否 `enabled` 且 `active`，launchd 看 `launchctl print` 能否查到，`cron` 看 crontab 里这个服务根的那一行还在不在，`manager` 回执算 `ready` 但带 `autostart: false`。扩展的托管模式：PID 1 里的守护在跑算 `ready`；回执带 `login_hook`（登录时由 shell 启动文件起独立守护，见扩展 README）且那个文件确实启动这份配置时，独立守护在跑也算 `ready`，报告里的 `login_hook` 是该文件。

**systemd 模式**

- 写入 `~/.config/systemd/user/covey-manager.service`（改名前装过、已有 `lark-bridge-manager.service` 的机器沿用旧名）并 `daemon-reload`，`--activate` 时 `enable --now`。单元的启动命令是运行安装器的那个 Python 加安装器所在 release 的 `bin/covey_service.py`。
- 用户服务默认只在登录期间运行。用户没开 linger 时，安装器的输出带 `hint`：`sudo loginctl enable-linger <用户>`（要 root，安装器不替你跑）。
- 没有用户 systemd（`/run/systemd/system` 不存在，或 `systemctl --user` 连不上用户管理器，常见于容器和没登录过的 ssh）：`auto` 退回 `manager`；明确写 `--mode systemd` 时在前一种情况下报错。
- 单元配置 `Restart=always`（10 秒后重启）和 `KillMode=process`：停止或重启单元只结束 Manager，Bridge 和任务进程留着，由下一个 Manager 接管。单元停着期间 Bridge 照常运行，但它退出后没人重启。这些行为由单元配置推出，测试只核对单元文件内容。
- 单元停着时运行 `covey_service.py start`、`restart` 或 `launch`，会在单元外拉起一个 Manager。之后再启动单元，单元里的 Manager 拿不到单实例锁就退出，每 10 秒重试，直到单元外那个退出。所以先 `--activate` 托管，再注册服务。

**launchd 模式**（macOS）

- 写入 `~/Library/LaunchAgents/<label>.plist`（`--label`，默认 `com.covey.manager`）：`RunAtLoad`、`KeepAlive`、`ThrottleInterval 10`、`AbandonProcessGroup`（停掉 Manager 不连带结束子进程），日志在 `<root>/launchd.log`，Manager 自己的日志仍是 `manager.log`。
- plist 里的环境变量只有 Manager 自己要的：安装时终端的 `PATH`、`HOME`、`LANG`，以及设了的 `COVEY_DATA_HOME`、`COVEY_SERVICE_ROOT`、`COVEY_EXTENSIONS`。Bridge 的环境（代理、`CODEX_HOME` 等）仍在服务清单里，由 Manager 带给它。
- `--activate` 用 `launchctl bootstrap gui/<uid>` 加载。它是用户代理：Mac 登录后才运行，登录前不会自己起来。

`./install --browser-login-dir DIR`（验收用，不在 `--help` 里）：浏览器会话从 `DIR/profile`（或 `DIR` 本身）这份已登录的用户目录副本开始，跳过二维码；副本由安装器再拷一份到临时目录，用完删掉那份，`DIR` 里的由调用方删。

## 开机恢复

开机自启见上一节「服务托管」。扩展提供的托管方式的说明在扩展自己的 README。确认开机自启注册好了：`covey_doctor.py` 的摘要写“开机会自动拉起”（`--json` 报告的 `boot` 段 `status` 为 `ready`）；systemd、launchd、cron 也可以用 `python3 -B bin/covey_service_install.py --mode auto --status` 看注册。

**扩展提供的模式**（`--mode` 的其他取值）：守护 `service/boot.py` 在核心：每轮等待配置的挂载盘就绪，再通过本地 socket 检查 Manager；Manager 不存在就拉起；控制循环延迟超过 120 秒或失去响应时，只向核对过身份的 Manager 发 SIGTERM，20 秒后仍在就 SIGKILL。Manager 在挂载恢复前不启动任何服务。守护只在配置（扩展写的 `boot.json`）对得上的 PID 1 里起：入口是 `pid1_entrypoint`，`host_env` 指名的环境变量的值是 `devbox_id`；没写 `host_env` 的配置不起守护。

**整机重启验收**：`<root>/boot-installation.json` 里的 `reboot_verified` 总是 `false`，代码不会把它改成 `true`。本地进程退出、Manager 重建的测试不能代替整机重启：只有真实重启后核对 Manager、服务进程、事件连接、应用身份和会话完整性，才算通过（见[发布记录要求](testing.md#发布记录要求)）。任务进程会随整机重启结束，重启后这些任务显示为中断，并自动检查并继续一次。

## 升级与回滚

`./install` 装的机器人日常升级用 `scripts/upgrade.sh`（先 `--dry-run`），见[升级与部署 · 日常升级](deploy.md#日常升级)；它底下和手工的做法一样，用 `scripts/deploy_bridge.sh` 换到固定 release、出事加 `--allow-rollback` 换回去，见[升级与部署 · 升级与回滚](deploy.md#升级与回滚)。

## 部署新版本

部署脚本一步步做了什么、手工怎么部署、升级 Manager，见[升级与部署 · 部署新版本](deploy.md#部署新版本)。

## 改名 Covey 后的兼容

旧包名、旧环境变量名、旧入口和旧单元名都还能用，见[改名与兼容](compat.md#改名-covey-后的兼容)。

## 自动补齐飞书应用

Bridge 每天检查飞书应用缺不缺权限、事件和菜单，缺了在私聊发「飞书应用待补」卡，见[飞书应用配置 · 自动补齐飞书应用](feishu-app.md#自动补齐飞书应用)。

## 旧安装脚本与手动配置

`./install`（[安装](install.md)）取代了旧安装脚本 `scripts/setup_harness_bridge.sh`；它和 `scripts/bootstrap_harness_bridge.sh` 仍可用、不再推荐。手工控制每一步见[手工安装](manual-install.md)，在开发者后台手动配飞书应用见[飞书应用配置](feishu-app.md#在开发者后台手动配置)。

# 配置

本文写给部署机器人的人：机器人从哪几处读配置、每个可选功能怎么打开、全部环境变量和参数。旧配置还能不能用见[已下线的功能与旧配置](legacy.md)，服务怎么注册、部署和维护见[运维指南](operations.md)，排障见[排障](troubleshooting.md)，各 Harness 的参数见 [Harness 说明](harnesses.md)。

## 开关速查

可选功能都靠服务清单里的环境变量开关。下面几张表回答“我这个机器人开了什么、还能开什么”；每个开关的全部取值和细节在后文各节和[一览](#环境变量与选项一览)里。

### 默认已开

下表这些功能，代码里不设变量就是开的：新装的服务、装这个版本之前注册的服务都一样，清单里没写它们也开着；写了 `0`（早晚汇总写 `off`）才关，写 `1` 也照样是开。`./install` 只往服务清单里写飞书 SDK 的两个开关（`core/switches.py` 的 `INSTALL`，见表末和[飞书 SDK 与直连](#飞书-sdk-与直连)）；没有 Claude Code、只有 Codex 或 Antigravity 调通时也一样，见[安装 · 没有 Claude Code 时](install.md#没有-claude-code-时)。服务清单里已经设了的照旧生效，所以升级前后行为不变的只有设过的那些；没设过的老服务升级后会多出这些功能，见[升级 · 这个版本起默认开的功能](deploy.md#这个版本起默认开的功能)。

| 功能 | 开关 | 怎么确认开着 | 怎么关 |
| --- | --- | --- | --- |
| [管家](#管家) | `COVEY_BUTLER`（不设即允许） | 私聊发一句话由管家回答或开话题；设置卡「配置」页有管家一段。管家没选 Harness 时用 Claude Code，没启用 Claude Code 时用启用了的 Codex 或 Antigravity（再没有时 DeepSeek Harness）；设置卡「配置」页的“管家”一段可换；选的 Harness 没启用时私聊照旧开话题 | `COVEY_BUTLER=0`；或主人在设置卡「配置」页的“管家”一段把私聊顶层改成“每条直接开一个话题” |
| [专项](projects.md) | `COVEY_PROJECTS` | 管家把长期要跟进的事开成专项时，私聊出现专项卡 | `COVEY_PROJECTS=0`（下面专项的几个开关跟着没用） |
| [整理专项](#整理专项)（只提议） | `COVEY_PROJECT_TIDY`（不设是 `suggest`；要专项开着） | 话题多了以后私聊出现“建个专项？”的提议卡；看板卡上有「整理专项」 | `COVEY_PROJECT_TIDY=off` |
| [独立复核](usage.md#答案在哪里) | `COVEY_REVIEW` | 改了代码的答案卡下面多「独立复核」：点了才开一个只读话题，换一家的模型只看你的原话、完成标准和这一轮的改动 | `COVEY_REVIEW=0` |
| [到点回来看](usage.md#到点回来看) | `COVEY_REVISIT`（看和取消在菜单「看板」） | 话题要等训练、CI 这类外部的事时，这一轮说好几点回来看就结束，到点自己再起一轮；菜单「看板」里有“到点回来看”一组 | `COVEY_REVISIT=0` |
| [记忆](#记忆) | `COVEY_MEMORY` | 你在话题里说的长期要求一轮结束后记下，答案卡底部露一行和“不对”“改”（有网页时）按钮；设置卡多「记忆」标签 | `COVEY_MEMORY=0` |
| [记忆自己长](#记忆) | `COVEY_MEMORY_LEARN`（要 `COVEY_MEMORY` 开着） | 你自己的一轮里说了“以后、别再、记住”这类话，一轮结束后 Bridge 用辅助模型读一遍你的话，长期要求记下（答案卡上一行“记下了”带“不对”；处处适用的先在当地生效、同时进待审核问管不管全局，涉及部署、删除这类事的采纳前不生效）；每轮的记忆块末尾多一行查记忆的命令 | `COVEY_MEMORY_LEARN=0`（`COVEY_MEMORY=0` 时也跟着关） |
| [知识条目](#知识条目与历史搜索) | `COVEY_KNOWLEDGE` | 说“记一下”的事记成条目，那一轮结束时卡上说记下了；话题首轮带上，汇总多一节 | `COVEY_KNOWLEDGE=0` |
| [专项的进阶](projects.md#使用者概览) | `COVEY_PROJECT_AUTOWAKE`、`COVEY_PROJECT_ASK`、`COVEY_PROJECT_MEMORY`、`COVEY_PROJECT_TRIAGE`（都要专项开着） | 依次是：话题一轮结束就叫醒负责人（负责人多跑几轮，多花些 token）；专项停住时发卡问你继续、收尾还是停；专项有自己的记忆和资料库；话题的选择题先给负责人分流 | 各设成 `0` |
| [等你的事补卡](architecture.md#等主人的账) | `COVEY_LEDGER_RECONCILE`（默认每 300 秒） | 要你回答的问题卡没发出去时，几分钟内自动补发到它的话题（最多 3 次）；补发了几次、发出几次见设置卡「诊断」和 `bin/covey_ledger.py` | `COVEY_LEDGER_RECONCILE=0`（只记账不补发也不起线程） |
| [早晚汇总](#早晚汇总) | `COVEY_DIGEST_TIMES`（不设是 `09:30,21:30`，按 Asia/Shanghai） | 到点在私聊发汇总卡；菜单「看板」随时点开看现在等你、停住、在做的事；整理专项的定时、晚间的记忆整理也按这两个时间 | `COVEY_DIGEST_TIMES=off`（也认 `0`、`no`、`none`、`false`；菜单「看板」照旧在） |
| [手上的事与话题看板](#手上的事与话题看板) | `COVEY_PINNED_OVERVIEW`、`COVEY_PINNED_TOPICS`（话题自动 Pin 要「手上的事」也开着） | 私聊置顶一张「手上的事」；话题自动把等你、在做、有结果的话题 Pin 上。要应用有置顶权限和“消息已读”事件，缺了「飞书应用待补」卡会提示 | 各设成 `0` |
| [同时做几件事](butler.md#同时做几件事) | `COVEY_BUTLER_TRACKS` | 管家在做一件事时，你的下一句可以并进去或另起一件，不用等 | `COVEY_BUTLER_TRACKS=0` |
| [派发卡](#派发卡) | `COVEY_DISPATCH_CARD` | 管家把事转给已有话题后，你的提问下面挂一张实时刷新的卡 | `COVEY_DISPATCH_CARD=0` |
| 派出的事转成专项 | `COVEY_PROJECT_UPGRADE`（不设是 `butler`；要专项和派发卡开着） | 管家派出去的话题要长期跟进时，能静默转成专项。说明见[专项 · 使用者概览](projects.md#使用者概览) | `COVEY_PROJECT_UPGRADE=off` |
| [管家规则](#管家规则) | `COVEY_BUTLER_RULES` | 管家只快答和分派：几秒能答完的直接答，要查、要跑、要改的派给话题，只告诉你放到哪个 T 号 | `COVEY_BUTLER_RULES=0` |
| [新话题带上最近话题的要点](#新话题带上最近话题的要点) | `COVEY_WORKSPACE_GISTS`（不设是 3；话题要点要整理专项开着才有） | 新话题第一轮的规则多一块同工作区最近 3 个话题的要点；设置卡「能做什么」有这一行 | `COVEY_WORKSPACE_GISTS=0` |
| [飞书 SDK 收消息、写卡片](#飞书-sdk-与直连) | `COVEY_EVENT_TRANSPORT=sdk`、`COVEY_CARD_TRANSPORT=sdk`（`./install` 写；服务的 Python 里没有飞书 SDK 时安装器先用 pip 装 `lark-oapi`，装不上就不写，照旧走 lark-cli，最后几行会说） | `bridge.log` 有 `event-websocket`；收下的消息和点击处理完之前记在状态目录的 `event-spool/`，重启、部署时还没处理的下次启动补处理 | 两个都设成 `cli` |
| 撤回与编辑 | `COVEY_SOURCE_WATCH`（默认开） | 撤回一条已变成任务的消息，任务跟着停；编辑后任务收到改后的内容 | `COVEY_SOURCE_WATCH=0` |
| [任务在跑时发的话补充给它](usage.md#追问排队与并行) | `COVEY_TOPIC_STEER`（默认开） | 话题里任务在跑时发一句，那张卡变成回执“已补充到 #N，不另开一轮”，你那句话上加一个 ✅ 表情（DONE），不再排成下一轮 | `COVEY_TOPIC_STEER=0`（照旧排成下一轮） |
| [追问只发答案卡](usage.md#追问排队与并行) | `COVEY_QUICK_ANSWER_SECONDS`（默认 10） | 话题里追问一句、它不用工具几秒内就答完时，串里只有答案卡，没有任务卡 | `COVEY_QUICK_ANSWER_SECONDS=0`（每轮先出任务卡） |
| [夜里不打扰](usage.md#早晚汇总) | `COVEY_QUIET_HOURS`（没设时按早晚汇总的时间，默认就是 21:30 到 09:30；汇总只有一个时间或关了时没有） | 夜里整理专项自己发现的提议卡、不严重的机器告警攒到早上再发 | `COVEY_QUIET_HOURS=off` |
| [连着做完的回带合成一条](butler.md#结论回到顶层) | `COVEY_RELAY_MERGE_SECONDS`（默认 20，要管家开着） | 别的话题还在做时，“完成”的回带等 20 秒，这期间做完的合成一张「N 个话题做完了」 | `COVEY_RELAY_MERGE_SECONDS=0`（每条马上发） |
| [自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | `COVEY_APP_CHECK`（默认开） | 应用缺权限、事件或菜单时私聊出现「飞书应用待补」卡 | `COVEY_APP_CHECK=0` |
| [子 Agent 守则](harnesses.md#claude-code) | `COVEY_CLAUDE_HOOKS`（默认开） | Claude Code 的子 Agent（Explore 除外）开工时带上四句守则；子 Agent 调 Bridge 的工具（开话题、转交）被拒；管家这一轮自己调满 6 次别的工具时被提醒一次交给话题 | `COVEY_CLAUDE_HOOKS=0`（不注册这些 hook，和原来一样） |
| [规则放在系统级](architecture.md#续接轮的规则) | `COVEY_SYSTEM_RULES`（默认开） | 四个核心 Harness 的桥接规则放在系统级（Claude Code 的 `--append-system-prompt`、Codex 新线程的 `developerInstructions`、dsh 的 patch、agy 私有主目录的 `GEMINI.md`），续接轮消息里不再整份重发、压缩也压不掉；会话里是旧版时在消息里补发一次新版；任务记录多 `rules_sent`、`compactions`，负责人轮多 `session_rebuild_reason` | `COVEY_SYSTEM_RULES=0`（规则回到用户消息，和原来一样；每轮现读，不用重新部署） |
| [欢迎卡](usage.md#第一次使用) | `COVEY_WELCOME`（默认开） | 新部署第一次启动后私聊里有一张「欢迎使用 Covey」卡；升级前已在用的部署不发 | `COVEY_WELCOME=0` |
| [限流和临时故障后自动继续](reference.md#自动重试) | `COVEY_AUTO_RETRY_LONG`（默认开） | 限流后的自动重试最久等约 3 小时（Claude 说了恢复时间就等到那时），529/5xx/断连也自动重试几次，每次另加 0–30 秒随机；任务卡上写着几点自动继续 | `COVEY_AUTO_RETRY_LONG=0`（照旧限流只重试两次、临时故障不重试、没有随机） |
| [几个话题同因停下的合并提示](reference.md#自动重试) | `COVEY_LIMIT_NOTICE`（默认开） | 几个话题差不多同时因限流或临时故障停下、各自排好自动重试时，私聊里多一条提示，列出每个话题几点继续 | `COVEY_LIMIT_NOTICE=0`（没有这条提示，各话题的任务卡照旧） |
| [账号与额度](quota.md) | `COVEY_QUOTA`（默认开） | 扩展给了额度来源、数据根有 `quota-sources.json` 或配了两个以上 agy 号时，Bridge 每分钟看一次额度来源，快照存在状态目录 `quota/`；号池用完时失败卡写清几点恢复，6 小时内到点自动接着做 | `COVEY_QUOTA=0`（不读额度来源、不问网关，失败卡照旧） |
| [别人私聊时回一句](usage.md#基本概念) | `COVEY_STRANGER_REPLY`（默认开） | 让同事私聊你的机器人：他收到“这是你的个人助手，只处理你的消息”，同一个人每天最多一次；设了 `COVEY_INVITE_URL` 时后面带上安装说明的链接 | `COVEY_STRANGER_REPLY=0`（照旧不回） |
| [机器人停了告诉你](operations.md#机器人停了会告诉主人) | `COVEY_DOWN_NOTICE`（默认开） | Manager 连续 3 次拉不起机器人、启动出错持续 2 分钟，或机器人 10 分钟收不到飞书消息时，私聊里收到一条“⚠️ 机器人没起来…”，好了再收到一条“✅ 机器人恢复了” | `COVEY_DOWN_NOTICE=0` |
| [敏感词脱敏](#敏感词脱敏) | 没有开关 | 设置卡有「脱敏」标签；话题开了脱敏才替换，只支持 Claude 的模型 | 设置卡上关 |
| 菜单「看板」 | 没有开关 | 机器人菜单里一直有「看板」，应用里还没有这一项时「飞书应用待补」卡会补上（见[看板](usage.md#看板)） | 不能关 |
| [代发](proxy.md#代发) | `COVEY_OWNER_SEND`（默认开） | 只在有代答发送器时才有：话题能起草“以主人身份发出”的消息，主人在卡上确认后发 | `COVEY_OWNER_SEND=0` |
| [代答发前检查](proxy.md#发前闸门)（只记） | `COVEY_PROXY_REPLY_CHECK`（默认 `count`） | 只在有代答发送器时才有：回复里有本机路径、内部编号、像密钥的字符串、没填的占位符时，`bridge.log` 有 `proxy reply check`，`<state>/proxy/reply-checks.json` 计数；回复照常发。设 `hold` 才拦 | `COVEY_PROXY_REPLY_CHECK=off` |

### 推荐打开

不需要别的东西，设上变量、重新注册就有。

| 功能 | 开关 | 打开后多什么、怎么确认 |
| --- | --- | --- |
| [群成员只读](#群成员只读) | `COVEY_GROUP_MEMBERS=read` | 没在群聊卡上单独选过的群，群成员开的任务只能读文件和搜索；群聊卡上这些群写着「群成员只读」，启用确认卡也照这个写 |

### 按需打开

要额外准备东西，或只有部分人用得上。

| 功能 | 要准备什么 | 开关 |
| --- | --- | --- |
| [代答](#代答) | 带发送器的扩展（核心没有发送器）、主人本人登录的 lark-cli profile | `COVEY_PROXY_PROFILE`，见[代答 · 前置条件与怎么开](proxy.md#前置条件与怎么开) |
| [网页工作台](web.md) | 自托管：Node 22 以上和 npm，装的时候加 `./install --web`（[安装 · 网页工作台](install.md#网页工作台可选)）；或一个配套的网页应用和它的开放接口密钥 | `COVEY_WEB=1`、`COVEY_WEB_CONFIG`；自托管再加 `COVEY_WEB_SELFHOST=1`、`COVEY_WEB_SELFHOST_DIR`、`COVEY_WEB_SELFHOST_PORT`，公网直连再加 `COVEY_WEB_SELFHOST_PUBLIC`、`COVEY_WEB_SELFHOST_PUBLIC_PORT`、`COVEY_WEB_SELFHOST_BIND` |
| [多设备](#多设备开发中默认关)（开发中） | 能 ssh 过去的其他机器、`devices.json` | `COVEY_DEVICES=1` |
| [节点与机器状况](#节点与机器状况默认关) | 一个设备能连进来的监听地址 | `COVEY_NODE_LISTEN` |
| [群聊免@](#群聊免) | 应用要有收群里全部消息的权限（要管理员审核） | `COVEY_GROUP_ALL_MESSAGES=1` |
| [历史搜索](#知识条目与历史搜索) | 一个能按一句话搜历史的命令 | `COVEY_HISTORY_CMD=/absolute/path/to/搜索命令` |
| [内存看门狗](operations.md#内存看门狗) | 不是环境变量 | `python3 -B bin/covey_memguard.py enable` |
| [本机运行锁](runtime-locks.md) | 同一台机器上有几个服务要共用锁 | `COVEY_LOCK_ROOT` |
| [定时自动升级](deploy.md#定时自动升级) | 数据根里有 `upgrade.json`（扩展的 `./install`、核心在 git 检出里的 `./install` 装好或升级好后写） | `COVEY_AUTO_UPGRADE=05:00`（每天几点，按 Asia/Shanghai） |

### 演示套餐

演示、截图或想一次看全时用：在[默认已开](#默认已开)的基础上再加一项（`core/switches.py` 的 `SHOWCASE`，也就是安装器写的两个飞书 SDK 开关加这一项）。它只是一组开关，不装别的东西。

| 功能 | 开关 |
| --- | --- |
| [多参与者协作](architecture.md#多参与者协作开发中默认关)（开发中）和[负责人对照目标跟进](projects.md#负责人对照目标跟进) | `COVEY_COLLAB=1` |

- **新装**：`./install --showcase`。和安装器写的别的开关一样，终端里已经设了的（新名旧名都算）不动；没有 Claude Code 时也是整套。已经装好的服务再跑一次 `./install --showcase` 也行：服务清单里已有的设置照旧，只打开套餐里清单没有或关着的开关，效果和下一条一样。
- **已经装好的**：`python3 -B bin/covey_service.py set --state-dir "$STATE_DIR" --preset showcase`，先加 `--dry-run` 看会改什么（见[改开关三步](#改开关三步)）。它打开套餐里服务清单还没设、或设成关闭值（空、`0`、`off`、`false`、`no`）的开关；设了别的值的不动；同一条命令里另写的 `KEY=VALUE` 盖过套餐。管家和专项负责人在 Codex、Antigravity 上也能跑，所以只有它们能用的机器也照用（安装器最后几行给的就是这条命令）。
- 套餐没有一键关：不要这一项，照[改开关三步](#改开关三步)把它设成关闭值。

### 改开关三步

环境变量写在服务清单里，Bridge 只认清单，不认你登录终端里后来设的变量。改开关用 `covey_service.py set`，在服务正在运行的代码目录里（`STATE_DIR` 见[运维 · 找到数据根和 APP_ID](operations.md#找到数据根和-app_id)）：

1. **先看会改什么**：`python3 -B bin/covey_service.py set --state-dir "$STATE_DIR" --dry-run COVEY_DIGEST_TIMES=08:00,22:00`，打印一行 JSON，`changed` 里是每个变量的 `from` 和 `to`，什么都不改。
2. **改**：去掉 `--dry-run` 再跑一次。关一个开关时把它设成关闭值（多数是 `0`，各自的取值见上表和[一览](#环境变量与选项一览)），如 `set … COVEY_BUTLER_TRACKS=0`；去掉一个设置用 `unset`：`python3 -B bin/covey_service.py unset --state-dir "$STATE_DIR" COVEY_HISTORY_CMD`。一次可以写几个。
3. **确认**：输出的 `generation` 加了一，`bridge.log` 有新的 `bridge started`；再在飞书里按上表“怎么确认”那一列看一眼。

服务清单里设了 `COVEY_LOCK_ROOT`（[本机运行锁](runtime-locks.md)）时，`set`、`unset`、`start`、`stop`、`restart` 和 `covey_plugins.py enable|disable` 自己用清单里的锁目录，终端里不用再设；标准错误输出一行“用服务清单里的锁目录 …”。终端里设了另一个值、或几份清单不一致时它拒绝，什么都不改。

`set` 和 `unset` 做的就是[改设置后重新注册](deploy.md#改设置后重新注册)那几步，只是不经过终端：读出清单里现在的变量，只改给出的这几个（存成清单用的旧名 `LARK_BRIDGE_<X>`，见[环境变量的旧名](compat.md#环境变量的旧名)），按清单原样的启动命令、工作目录、profile、日志和健康超时重新注册，代次加一，Manager 换一个 Bridge 进程。所以终端里有什么变量都不影响它，清单里的代理等变量也不会丢；它不换代码（换代码是升级，见[升级与回滚](deploy.md#升级与回滚)）。值没变时什么都不做。

它拒绝、什么都不改的情况：

- 名字像凭据的（含 `SECRET`、`TOKEN`、`PASSWORD`、`CREDENTIAL`，或以 `KEY` 为一段）、不归清单管的（只收 `COVEY_*`、`LARK_*_BRIDGE_*`、`PATH`、`LANG`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME` 和代理变量）、Manager 和开机守护自己设的（`COVEY_MANAGED` 等）；值带换行，或代理带用户名密码。
- 部署预检（[部署新版本](deploy.md#部署新版本)）会挡的：已经不生效的权限变量设成了收紧的值、重启会打断的任务、正在建 worktree 的启动卡、正在发的代答批次、另一个部署正在进行（`deploy.lock`）。
- `unset` 的变量 Manager 自己的环境里也有（第一次 `launch` 时那个终端带进去的）：只删清单里的，Bridge 照样拿到它，所以拒绝并提示改用关闭值，见[服务清单里的环境变量](#服务清单里的环境变量)。
- 服务 `stop` 着：先 `covey_service.py start`。

`set` 管不到的，比如改 Codex 等写在服务命令行上的参数，照手工的做法：按[载入服务清单的环境](deploy.md#载入服务清单的环境)把清单里的变量载入一个新终端，改好后按[改设置后重新注册](deploy.md#改设置后重新注册)重新注册（检出运行的重跑 `./install`，release 运行的在那个 release 目录里跑 bootstrap）。

`covey_service.py restart` 只换进程、不换清单，所以改开关不能只 `restart`；`harnesses.json` 和 `devices.json` 才是改完文件 `restart` 就行。

## 配置放在哪

| 放在哪 | 管什么 | 怎么改 |
| --- | --- | --- |
| 服务命令行 | Bridge 自己的参数（`--workdir`、`--profile`、`--app-id`、`--state-dir`、`--owner`、`--harness-config`）和默认 Harness（Codex）的参数 | 重新注册（`scripts/bootstrap_harness_bridge.sh` 或 `scripts/deploy_bridge.sh`） |
| `harnesses.json`（`--harness-config`） | 其余 Harness 的参数和模型顺序 | 改文件后重启（`restart` 或重新注册） |
| `devices.json`（`$COVEY_DATA_HOME/devices.json`，或 `COVEY_DEVICES_FILE` 指的文件） | 多设备（开发中，默认关）：每台设备怎么 ssh、能跑哪些 Harness、给任务的环境和谁能用；`COVEY_DEVICES=1` 才读 | 改文件后重启 |
| 服务清单里的环境变量 | 可选功能的开关、Harness 的环境（`PATH`、`CODEX_HOME`、代理等） | `covey_service.py set`（[改开关三步](#改开关三步)），或在终端里设好后重新注册，见下文 |
| 状态目录里的设置文件 | 主人在飞书里改的设置：工作区列表、私聊模式、代答、脱敏词表等 | 在飞书的设置卡上改，实时生效 |

服务清单不保存 App Secret、访问令牌和 Harness 凭据。它们留在 lark-cli 和各 CLI 自己的配置里。

## harnesses.json

`--harness-config` 指向一个 JSON 文件。服务只记路径，每次启动时读，所以文件要放在持久位置（建议 `$COVEY_DATA_HOME/harnesses.json`）。文件里不放凭据。

```json
{"harnesses": {
  "<扩展 Harness id>": {"args": ["--<扩展参数>", "/absolute/runtime/venv/bin/python"]},
  "dsh": {"args": ["--dsh-cmd", "/absolute/bin/dsh"], "models_first": ["<模型 ID>"]}
}}
```

- `args`：这个 Harness 的命令行参数，可用的参数见 [配置 Harness 参数](harnesses.md#配置-harness-参数)。
- `models_first`：启动卡上这个 Harness 的模型里，写着的排在段首；不写时按模型目录的顺序（`app/launch_models.py` 的 `first_models`）。
- 不在当前名单里的键（包括已下架的 Harness）忽略，日志记 `harness config ignores Harnesses not in the roster: …`。
- 扩展可以另有自己的参数、环境变量和档位文件，列在扩展自己的 README 里（公司部署的扩展也是）。

文件格式的完整说明见 [`--harness-config` 文件](harnesses.md#--harness-config-文件)。

## 空间（`spaces`）

私聊和每个绑定的群都是一个“空间”（`core/spaces.py`）：**个人空间**只有主人（和主人的其他账号），私聊是默认的个人空间；**团队空间**是已激活的群，同事 @ 机器人开话题；**代答空间**是代答用的群。`harnesses.json` 的 `spaces` 段设置这些，没有这一段时行为和没有空间概念之前完全一样。

```json
{"spaces": {
  "owner_accounts": ["ou_<主人的另一个账号>"],
  "notify_space": "oc_<通知发到的群或私聊>",
  "allow_team_spaces": false,
  "personal_groups": ["oc_<主人自己的项目群>"]
}}
```

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `owner_accounts` | `[]` | 主人在这个应用里的其他 open_id；每个都按主人处理（消息、点击、菜单、已读）。记录里仍只有一个 `owner_open_id`；只有主人本人的私聊算“主人私聊”，另一个账号的私聊按普通私聊处理 |
| `notify_space` | `""`（主人私聊） | Bridge 自己发的通知去哪个 chat：早晚汇总、手上的事和话题看板、已读事件对应的聊天、没有任务可回的内存看门狗告警、应用待补卡、代答登录提示（扩展）。设了以后，它们对应的话题也按这个 chat 算 |
| `allow_team_spaces` | `true` | `false`：不激活任何群，也不发启用确认卡，群里 @ 也不服务（群管理卡的“启用”给出说明），只服务主人。`true` 时群也要主人在启用确认卡或群聊卡上点“启用”才对群成员开放 |
| `personal_groups` | `[]` | chat_id 列表：这些群是主人的个人空间，只服务主人（含 `owner_accounts`），不会被激活。个人群和私聊一样有自己的管家、置顶菜单卡（用量、看板、设置）、手上的事、话题看板和早晚汇总；主人在群里不用 @ 机器人（要应用有 `im:message.group_msg` 权限，没有时仍要 @） |

- `owner_accounts` 里的账号私聊机器人时也走管家，点机器人菜单回在它自己的私聊里。
- 设了 `spaces` 段后，设置卡「群聊」页每行带空间类型标签，个人群也列出、没有启用/停用按钮。`notify_space` 是个人群时，它的看板就是默认那一份。
- 类型不对（比如 `allow_team_spaces` 不是布尔，`notify_space` 不是 chat_id）时启动报错。
- 旧键 `owner_only`（取反即 `allow_team_spaces`）、`owner_aliases`、`home_chat`、`agent_groups` 仍可读，各映射到上表的字段，`spaces` 里写了同名字段时以它为准，启动日志每个旧键记一行 `… is deprecated: write spaces.…`。`group_menu`、`group_boards`、`owner_headed` 不再是开关（个人群自带），只读入并记一行说明。
- 判断和记录怎么存见[架构 · 谁被服务](architecture.md#谁被服务)。

## 服务清单里的环境变量

`launch` 每次都从执行它的终端取下面这些变量，整组替换清单里的旧值：

| 类别 | 变量 |
| --- | --- |
| 保存：固定白名单 | `PATH`、`LANG`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME`，以及 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 和它们的小写形式 |
| 保存：本项目的设置 | 所有 `COVEY_*` 和 `LARK_<名称>_BRIDGE_*`，例如 `COVEY_DATA_HOME`、`COVEY_CARD_TRANSPORT` |
| 不保存 | `COVEY_MANAGED`（Manager 启动 Bridge 时设置）、`COVEY_REQUIRED_MOUNT`（开机守护设置）、`COVEY_SERVICE_ROOT`（也不传给 Bridge）；名字像凭据的（含 `SECRET`、`TOKEN`、`PASSWORD`、`CREDENTIAL`，或以 `KEY` 为一段）；其他所有变量 |

- **终端里没有的变量，清单里也就没了。** 在没有代理变量的终端里重新注册，清单里的代理就会丢；终端里多出的 `LARK_*BRIDGE_*` 也会被保存。所以先[载入服务清单的环境](deploy.md#载入服务清单的环境)再改，重新注册前对比一遍（[部署新版本](deploy.md#部署新版本)第 3 步）。只改开关时用 `covey_service.py set`（[改开关三步](#改开关三步)），它从清单读、不看终端。
- **关开关要设成关闭值，不能只 `unset`。** Bridge 的环境是 Manager 自己的环境加上清单里的变量（再加 `PYTHONDONTWRITEBYTECODE=1`、`COVEY_MANAGED=1`、`TMPDIR=<state>/tmp`）。Manager 是第一次 `launch` 时从那个终端拉起的，带着当时的变量一直跑；之后的 `launch` 只换清单，所以终端里去掉的变量还留在 Manager 的环境里。设成关闭值（如 `COVEY_BUTLER_TRACKS=0`）后清单里的值会盖过它。实在要清掉 Manager 环境里的旧变量：取 `"$SERVICE_ROOT/manager.json"` 里 `process.pid`，`kill -TERM -- -<pid>` 结束它的进程组（Bridge 和任务进程各在自己的会话里，不受影响），`pgrep -g <pid>` 确认没有残留，再在干净的终端里 `launch`。systemd 等托管方式拉起的 Manager 不带终端变量，重启单元即可。
- **`restart` 不补变量。** 开机后 Manager 由开机守护拉起，不读交互 shell 的初始化文件，所以 `PATH`、代理和本项目的设置只能靠清单；清单里缺的变量要重新 `launch`。
- 变量值不能含换行。代理 URL 只能是 `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h`，要有主机名，不能带用户名、密码、路径、查询参数或片段（`NO_PROXY` 不校验）。不符合时 `launch` 拒绝写入，报 `only explicit non-secret runtime paths/settings can be persisted` 或 `only non-secret proxy URLs without credentials can be persisted`。带凭据的代理不能托管。
- 常用的变量：`COVEY_PROFILE`（Bridge 的 lark-cli profile，所有 Harness 共用）、`COVEY_OUTGOING_MEDIA_DIRS`（回复里本地 Markdown 图片额外查找的目录，用 `:` 分隔）。

**不再读取的变量**：以前给后端参数设默认值的 `LARK_BRIDGE_CODEX_*`、`LARK_CLAUDE_BRIDGE_*` 等变量现在不读，设了启动日志记一行 `env <名字> is no longer read; put <参数> in …`；其中两个权限变量设了非默认值会让诊断和部署预检失败。换成哪个参数、怎么清掉，见[已下线的功能与旧配置 · 不再读取的变量](legacy.md#不再读取的变量)。

## 可选开关

下表列每个功能多出的后台线程和入口，给要核对的人看（用户看到什么见[开关速查](#开关速查)）。只有部分部署要用的功能默认关闭：没配置时不起后台线程、不加菜单项、不改已有行为；其余的默认开，表里写着怎么关。打开、关掉都按[改开关三步](#改开关三步)做。

| 功能 | 怎么打开 | 打开后多什么 |
| --- | --- | --- |
| [代答](#代答) | `COVEY_PROXY_PROFILE` 和发送器 | `proxy`、`proxy-in` 后台线程；设置卡「代答」能用 |
| [手上的事与话题看板](#手上的事与话题看板)（默认开） | `COVEY_PINNED_OVERVIEW=0`、`COVEY_PINNED_TOPICS=0` 关 | `overview` 后台线程；Pin 话题时多一个已读事件消费者 |
| [早晚汇总](#早晚汇总)（默认开） | `COVEY_DIGEST_TIMES`；`off` 关 | `digest` 后台线程；菜单「看板」不管它开不开都有 |
| [知识条目与历史搜索](#知识条目与历史搜索) | `COVEY_KNOWLEDGE`（默认开，`0` 关）；`COVEY_HISTORY_CMD=/absolute/path/to/搜索命令` | 管家多 `knowledge_*` 和 `history_search` 工具；话题首轮带知识条目，规则多一行查历史的命令 |
| [记忆](#记忆)（默认开） | `COVEY_MEMORY=0` 关 | 每轮规则里多一块“记忆”；一轮结束后记下主人的长期要求，答案卡底部露一行和“不对”按钮；设置卡多「记忆」标签；`memory-tidy` 后台线程每天整理一次资料和决定 |
| [记忆自己长](#记忆)（默认开） | `COVEY_MEMORY_LEARN=0` 关（要 `COVEY_MEMORY` 开着，`COVEY_MEMORY=0` 时也关） | 主人的一轮结束、原话里有触发词时起一个短线程（`memory-learn`，一次一轮）用小模型读他的话；每轮的记忆块多一行查记忆的命令 |
| [新话题带上最近话题的要点](#新话题带上最近话题的要点)（默认开） | `COVEY_WORKSPACE_GISTS=0` 关 | 不多线程；新会话的规则多一块同工作区最近话题的要点 |
| [管家](#管家) | 默认允许；`COVEY_BUTLER=0` 不允许 | `main-tools`、`main-search` 后台线程；设置卡「配置」页多管家一段 |
| [管家规则](#管家规则)（默认开） | `COVEY_BUTLER_RULES=0` 关 | 不多线程，只换管家规则的两条 |
| [专项](projects.md)（默认开） | `COVEY_PROJECTS=0` 关 | `projects/` 记录、负责人话题和它的 `project_*` 工具、看板并格；进阶开关见[开关速查](#默认已开) |
| [整理专项](#整理专项)（默认开） | `COVEY_PROJECT_TIDY`：`suggest`（不设）、`auto`；`off` 关（要专项开着） | `project_tidy` 后台线程；管家多 `projects_tidy`，看板卡多「整理专项」，提议卡、整理专项卡 |
| [专项停住后问主人](projects.md#专项停住后问主人)（默认开） | `COVEY_PROJECT_ASK=0` 关（要专项开着） | 专项被护栏停住时多发一张卡：继续 / 收尾 / 停 |
| [同时做几件事](butler.md#同时做几件事)（默认开） | `COVEY_BUTLER_TRACKS=0` 关 | 管家消息的分流（规则定不了时问一次辅助模型，不留待命进程） |
| [网页工作台](web.md) | `COVEY_WEB=1` 加 `COVEY_WEB_CONFIG=/absolute/path/to/web.json` | `web-push`、`web-pass`、`web-inbox`、`web-touch`、`web-reread` 后台线程：把专项、话题、一轮、问题推给网页应用，取回网页上的操作 |
| [派发卡](#派发卡)（默认开） | `COVEY_DISPATCH_CARD=0` 关 | `dispatch_cards` 后台线程；管家转给已有话题后提问下面挂一张活卡片 |
| [群聊免@](#群聊免) | `COVEY_GROUP_ALL_MESSAGES=1` 加应用权限 | 设置卡「群聊」每个群多一个“免@” |
| [多设备](#多设备开发中默认关)（开发中） | `COVEY_DEVICES=1` 加 `devices.json` | `devices` 后台线程（有设备时才起）；设备上能跑 Claude Code、Codex 和 Antigravity 的话题，管家也能开 |
| [节点与机器状况](#节点与机器状况默认关) | `COVEY_NODE_LISTEN=<监听地址>` | `nodes-gateway`、`nodes-alerts`、`nodes-local` 后台线程；机器状况告警卡，设置卡「设备」标签 |
| [飞书 SDK 与直连](#飞书-sdk-与直连) | `COVEY_CARD_TRANSPORT=sdk`；直连默认开 | 卡片和消息走常驻 SDK 客户端 |
| [事件长连接走 SDK](#飞书-sdk-与直连) | `COVEY_EVENT_TRANSPORT=sdk` | `event-websocket` 线程代替 lark-cli 事件消费者；`event-spool` 线程把收下没处理完的消息和点击记在 `event-spool/`，重启后补处理 |
| [自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 默认开；`COVEY_APP_CHECK=0` 关 | `app-setup` 后台线程 |
| [欢迎卡](usage.md#第一次使用) | 默认开；`COVEY_WELCOME=0` 关 | 启动时欢迎卡还没发出才有 `welcome` 后台线程，发出后它不再做事 |
| [限流和临时故障后自动继续](reference.md#自动重试) | 默认开；`COVEY_AUTO_RETRY_LONG=0` 关 | 不多线程；任务记录多 `auto_retry_reset_at`、`auto_retry_progress`、`auto_retry_idle`、`auto_fault`、`auto_idle` |
| [几个话题同因停下的合并提示](reference.md#自动重试) | 默认开；`COVEY_LIMIT_NOTICE=0` 关 | 不多线程；Bridge 的 outbox 循环里每 20 秒看一眼；`limit-notices/` 记录 |
| [别人私聊时回一句](usage.md#基本概念) | 默认开；`COVEY_STRANGER_REPLY=0` 关 | 不多线程；别人私聊机器人时每人每天回一句纯文本，记在 `stranger-replies/` |
| [内存看门狗](operations.md#内存看门狗) | `bin/covey_memguard.py enable` | 独立进程；`memguard` 后台线程转发告警 |
| [本机运行锁](runtime-locks.md) | `COVEY_LOCK_ROOT` | 同机多个服务共用的锁 |
| [到点回来看](usage.md#到点回来看)（默认开） | `COVEY_REVISIT=0` 关 | `revisits` 后台线程；普通话题每轮多一行规则；`revisits/` 记录；看板多“到点回来看”一组 |
| [多参与者协作](architecture.md#多参与者协作开发中默认关)（开发中） | `COVEY_COLLAB=1` | `collab` 后台线程（派发、到期、重启对账）；主人在普通话题里的那一轮多一行规则（`collab_ask`）；`collab/` 记录，咨询通道话题目录 `topics/collab-*`；咨询轮只在声明了 `collab` 的 Harness（Claude Code、Codex）上跑 |

### 代答

[代答](usage.md#代答)要两样东西（实现见[代答](proxy.md)）：

- `COVEY_PROXY_PROFILE`：带主人本人登录的 lark-cli profile，要有 `im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`search:message`、`contact:user:search` 和 `offline_access`。用户 token 的刷新期是 7 天；轮询每分钟都在用它，正常会一直续上，代答关掉超过 7 天会过期。过期后 `lark-cli --profile <它> auth login` 重新授权。第一次授权：`lark-cli --profile <它> auth login --scope "im:message.p2p_msg:get_as_user im:message.group_msg:get_as_user search:message contact:user:search offline_access"`，用主人自己的飞书账号确认；可以就用 `./install` 建的那个 profile（它已有主人的登录，补上这些权限即可）。权限要先在应用的权限管理里开通，「飞书应用待补」卡不管这些。
- 发送器（扩展钩子 `proxy_sender`，见[发送器](proxy.md#发送器)）：没有发送器就没有代答入口。发送器的安装与配置见提供它的扩展的 README；开代答的完整步骤见[代答 · 前置条件与怎么开](proxy.md#前置条件与怎么开)。

代发（[话题以主人身份发消息](proxy.md#代发)）跟着发送器：有发送器就开，`COVEY_OWNER_SEND=0` 关掉。它用同一个 `COVEY_PROXY_PROFILE` 找收件人，另要 `contact:user:search`。

验收页面（`bin/covey_feishu_web.py serve|accept`，见[浏览器服务](testing.md#浏览器服务)）另用一个装了 Playwright 的 Python，例如 `$COVEY_DATA_HOME/proxy-web/venv/bin/python`，代答发送不用它。主人的网页端登录只留 `<state>/proxy/web/profile`（0700）一份，不进备份：

- 别的工具要用就复制一份到自己的运行目录，不要直接打开它（同一个 profile 只能有一个 Chromium）。
- 复制前确认 `<state>/proxy/batches/` 里没有 `status` 为 `sending` 的批次，并排除 `Cache`、`Code Cache`、`Service Worker`、`GPUCache`、`Dawn*Cache`、`IndexedDB`、`Singleton*`。
- 它的缓存超过 200 MiB 时由维护删掉（日志 `proxy web login caches removed: N MiB`），cookie 和登录不受影响；条件见[架构 · 代答](architecture.md#代答)。

发前检查（[代答 · 发前闸门](proxy.md#发前闸门)）：`COVEY_PROXY_REPLY_CHECK` 不设或 `count` 时只记命中（日志 `proxy reply check …`、`<state>/proxy/reply-checks.json`），回复照常发；`hold` 时命中的回复不发，留给你在重新发送卡上看；`off` 不查。先用默认的只记跑一周，看 `reply-checks.json` 的 `recent` 里有多少是误报，再决定开不开 `hold`。

代答的日志行见[排障 · 代答](troubleshooting.md#代答没有回复或没有发出)。

### 敏感词脱敏

脱敏的词表、开关和替换记录在设置卡的「脱敏」标签上，不用配置，所有部署都有；开了[网页工作台](web.md)时，它的设置页也能批量加删词。只有 Claude 的模型能脱敏，话题开了才替换（用法见[使用指南](usage.md)，机制见[架构 · 敏感词脱敏](architecture.md#敏感词脱敏)）。

- 「脱敏」标签（卡的标题“敏感词脱敏”）：顶上写新话题默认开还是关、只对 Claude Code 生效；填词框一次加或删一批（按行，删除要整词一致），替身词自动生成，下面紧跟着一直展开的「试一下」；折叠面板里是开关、替换记录和上游地址。卡上不出现原词，只写首字和字数。“撤销”只记在 Bridge 进程的内存里，重启后就没有了。
- 词表在 `<state>/redaction/config.json`（0600），每轮开始时读，改动下一轮生效；每轮的替换次数记在 `<state>/redaction/turns.jsonl`。「探测」在后台跑一次 `<claude 命令> --gateway-status`，只显示结果、不改配置。

### 手上的事与话题看板

- `COVEY_PINNED_OVERVIEW`：默认开，在主人私聊里置顶一张「手上的事」汇总卡（见[手上的事](usage.md#手上的事)）。设 `0`、`off`、`false`、`no` 才关，`1` 也照样是开。主人的私聊取 `<state>/owner-chat.json`（主人最近一次私聊机器人时记下），还没有就不发。卡片记录在 `<state>/pinned-overview.json`。置顶用 `im pins create`，机器人发消息的权限就够。日志：`pinned overview sent|pinned|retired …`，失败是 `pinned overview not sent|pin failed|update failed`。
- `COVEY_PINNED_TOPICS`：默认开，在「手上的事」上面自动 Pin 话题（见[话题看板](reference.md#看板自动-pin-话题)）。关的取值同上，而且要 `COVEY_PINNED_OVERVIEW` 也开着：由「手上的事」的后台线程管，它那张卡置顶之后才开始 Pin。最多 10 个话题（`app/pins.py` 的 `MAX_PINS`），顺序变了才动，最多每 60 秒一轮。私聊的 `im pins list` 读不到东西，Bridge 只信自己的记录 `<state>/pinned-topics.json`。`<state>/board-priority.json`（话题 → 时间）让话题在本组排最前，由管家的工具写。看板从不撤回消息。日志：`pinned board pin|unpin|move|dismissed message=… topic=… group=…`，失败是 `pinned board failed op=… failures=N`。
- 已读事件：`COVEY_PINNED_TOPICS` 开着时 Bridge 多起一个消费者 `lark-cli event consume im.message.message_read_v1`（后台线程 `topic-seen`，`app/topic_seen.py`）。只认主人读的消息；每条在主人私聊的话题别名里查，查到的话题记下最近一次读到的时间，写进 `<state>/topic-seen.json`（90 天没读的和超出 2000 个的较早话题删掉）。开发者后台要订阅「消息已读」事件，应用要有 `im:message` 和 `im:message:readonly`（「飞书应用待补」卡会提示补齐）；缺了消费者打印 `missing_scope` 后退出，每 300 秒重试，其余功能照常，健康检查不看它。

关掉：按[改开关三步](#改开关三步)设成 `0`。已置顶的卡和已 Pin 的话题留在原处，在私聊的 Pin 页里手动取消；记录文件留着不再更新。

### 早晚汇总

`COVEY_DIGEST_TIMES`：逗号分隔的 HH:MM，按 Asia/Shanghai，重复的只算一次，写错的跳过并记日志；不设或空是 `09:30,21:30`，写了时间就用写的。每到一个时间点，Bridge 在主人私聊的顶层发一张汇总卡（`app/digest.py`、`cards/digest.py`；用户看到什么见[早晚汇总](usage.md#早晚汇总)）。设成 `off`（也认 `0`、`no`、`none`、`false`）就不发汇总、不起后台线程；菜单「看板」不管汇总开不开都有。

- 记录在 `<state>/digest.json`：最后发出的时间点 `last_slot`、发送中的 `pending`、最近 6 张发出的卡 `sent`。一个时间点只发一次：Bridge 在时间点停着、60 分钟内起来就补发一次，超过就跳过。卡先存再发；没确认的 60 秒后用同一个幂等键再发，最多 3 次。
- 数据从记录读（`questions/`、`jobs/` 的索引），不持记录锁、不调模型，管家话题不列。
- 一天里最后一个时间点是晚间汇总（`last_of_day`），私聊那张多“本周找你 N 次”（`app/weekly_usage.py` 的 `reached`）和有代答发送器时的“今天代答发出 N 条”（`proxy_view`）；开着记忆、当天整理记忆收起了东西时再多“整理记忆”，另有一行“记忆：待审核 N 条”，见下文。群里的汇总没有这几样。
- 菜单「看板」（事件 key `digest_board`）每个部署都有：发同一张卡、按现在取数，不改 `last_slot`；汇总关了时“做完”列最近一天的。卡的记录在 `<state>/digest-boards/`。开发者后台的机器人菜单和斜杠命令里要有这一项（「飞书应用待补」卡会补）。
- 两个时间也是[夜里不打扰](usage.md#早晚汇总)的时段（没设 `COVEY_QUIET_HOURS` 时），整理专项的定时也按它们。
- 日志：`digest sent slot=HH:MM`、`digest not sent: nothing to list`、`digest not sent try=N`、`digest not sent after N tries; given up`、`digest card redrawn message=… cleared=N`、`digest board sent`。

### 整理专项

`COVEY_PROJECT_TIDY`（要专项开着；用户看到什么见[整理专项](usage.md#整理专项)，机制见[专项 · 整理专项](projects.md#整理专项)）：

- `suggest`（只提议）：每轮结束时话题自己的模型更新话题要点；到早晚汇总的时间点（`COVEY_DIGEST_TIMES`）或主人手动叫时整理一次（没写要点的话题在这时先补上）。没有实时归并：新话题、新专项该收进来的话题，都等下一次整理。能归进已有专项、把握很高的直接归，收起没事的专项也直接做；新建、合并、拆开、改名都发卡问，主人点了才做。
- `auto`：再加新建专项也直接做（专项卡上留「不要专项」撤回）。主人在提议卡上点了「以后直接建」，效果和 `auto` 一样。
- 不设或空：同 `suggest`。
- `off`、`0` 或别的值，以及专项关着：关。没有后台线程、管家没有 `projects_tidy`、设置卡没有这一行、每轮的消息里没有话题要点那段，行为和原来一样。

不设就是 `suggest`，新装的和老服务都一样（`core/switches.py` 的 `DEFAULTS` 记着各转正开关不设时的值，全部见[默认已开](#默认已开)）。要关，按[改开关三步](#改开关三步)设 `COVEY_PROJECT_TIDY=off`。

可调的数（都是正数，写错按默认）：`COVEY_PROJECT_TIDY_DAYS`（7，整理看最近几天的话题）、`COVEY_PROJECT_TIDY_MIN_TOPICS`（5，聊天里的话题少于这个数时不整理）、`COVEY_PROJECT_TIDY_JUDGE_SECONDS`（1200，一次判断调用的上限）、`COVEY_PROJECT_TIDY_READ_SECONDS`（300，补一个话题要点的上限）。上限只防进程挂住，不当成慢模型的截止时间：重的一问用跟的那份设置的预算，Antigravity 的 gemini-4-neon 判断 40 个话题，默认预算（high）要 3 分钟上下，xhigh 要 12 分钟上下，补一个要点 xhigh 要半分钟到两分钟；超时不换别的 Harness 重问（不烧两份额度）。补话题要点和判断都是一问一答，去[辅助模型](#辅助模型)选的地方，用那里的模型和思考预算（自动跟随时就是管家的）。判断不了时整理卡写原因、用的是谁和下一步（「换辅助模型」「看诊断」）；补话题要点时遇到登录失效、额度用完这类模型那边的问题，不把话题标成“读不了”，整理在这里停下并说明。

### 记忆

- `COVEY_MEMORY`：默认开，设 `0`、`off`、`false`、`no` 才关（[管家 · 记忆](butler.md#记忆)）。不依赖别的开关：原则一直有，`COVEY_KNOWLEDGE` 开着时知识条目也进来，专项和 `COVEY_PROJECT_MEMORY` 都开着时专项记忆也进来；话题自己的记忆放在 `<state>/topics/<话题>/memory.json`，有记忆才会有。
- 关着（`COVEY_MEMORY=0`）：规则、续接提醒、答案卡和任务记录都和没有这个功能时一样，不读不写话题的 `memory.json`，设置卡没有「记忆」标签。
- 开着时设置卡有一个「记忆」标签（`app/memory_panel.py`）：只看数、处理待审核、去网页。副标题是一行“规矩：全局 N · 专项 N · 话题 N · 待审核 N”；待审核每条写明是哪一种、点「采纳」「不要」各会怎样（走网页「记忆」同一套 `review_decide`）；有网页（`COVEY_WEB=1` 且配了 `page_url`）时给“打开网页”链接，没有时列出全局、专项和话题规矩的标题，每条能“收起”。常驻、工作区、下一轮带上什么都不在这里了，管理都去网页的「记忆」。
- 升为全局：飞书上已下线。答案卡上“记下了”那一行只有“不对”（有网页时多一个“改”，打开网页「记忆」里的那一条，`/memory?code=<短码>`）；要把一条提到更大的范围，到网页的「记忆」做。旧卡片上点“升为全局”只回一句已下线，不改东西、不开任务。
- 晚间候选（晚间汇总时找“你反复说的”放进待审核）已下线：不再找、不再进待审核，`<state>/memory-candidates.json` 里旧版留下的行不再读；待审核里旧版放进去的候选按过期算、不再列出。
- 待审核：开着的项 14 天没处理，每天的维护（`core/maintenance_rules.py` 的 `MemoryFiles`，`memstore.upkeep`）关成过期，留着不删；同一条提议还开着、被拒过或采纳过时不再进来，过期的可以再提。负责人用 `project_remember` 记的规矩当场在专项里生效、标“负责人记的”，不进待审核。
- 用到才留：检查点 `memory_used` 里写了、`covey memory show` 看过、问前查重拿它答过，这三种才算用过，记进 `<state>/memory/usage.json` 的 `last`（整条带上、只列标题不算；旧的 `<state>/memory-usage.json` 只在整理记忆算闲置时一起读）。
- 整理记忆（`app/memory_tidy.py`，自己的线程 `memory-tidy`，每天跑一次，只按规则、不调模型）：每个在做的专项（每天最多 4 个）里 30 天没用过的、问的事已经办完的、说的是别的专项的资料和决定（含 `decisions.jsonl`）收起，规矩一条不收。只收起不删除；开了早晚汇总时晚间汇总多一行“收起 N 条”带「撤销」。记录在 `<state>/memory-tidy.json`；日志 `memory tidy project=… put_away=N`、`memory tidy undo run=…`，不写原话。
- 关掉：按[改开关三步](#改开关三步)设成 `0`；已经写下的记忆留着，标了常驻的原则在原则块里照样不被挤掉，整理记忆也不再跑。
- 日志：`memory lines written=N of M job=#…`、`memory lines ignored reason=… job=#…`、`memory lines failed job=#…`、`memory line refused by the gate reason=… job=#…`。
- `COVEY_MEMORY_LEARN`：记忆自己长，默认开，设 `0`、`off`、`false`、`no` 才关；要 `COVEY_MEMORY` 开着，`COVEY_MEMORY=0` 时它也跟着关（[管家 · 记忆自己长](butler.md#记忆自己长)）。新装的和升级上来、清单里没设过它的服务都开着；关着时以下都没有，记忆和上面写的一字不差。
  - 每轮之后（`app/memory_learn.py`）：主人自己的一轮正常结束，他原话里有纠正的字眼，就由 Bridge 用[辅助模型](#辅助模型)（最多 45 秒，一次只问一轮）读一遍，引用核对过的长期要求这样记：只对这件事的当场记到专项或话题，答案卡上一行“记下了”带“不对”；通用的、某个工作区的先放进待审核（`memory/review.json`，类型 `learn`，带他的原话），采纳后才成条目，答案卡上写“待审核”（同一条已在待审核里或处理过就不再写）。不读：代答话题、负责人的话题、开了脱敏的话题、管家转交的轮次、协作轮。这时规则里不再有“以后/别再”那句检查点约定；辅助模型选了“不用模型”时这一遍读不了，那句检查点约定照旧带上。
  - 整理记忆不要这个开关（见上）；以前的“被新回答替代”和“新约定”（含「确认长期」「不对」）已下线。
  - 别处学到的（Harness 记忆导入）已下线：晚间不再读各 Harness 的记忆文件，网页上没有导入入口；`<state>/memory/imports.json` 原样留着，不再读写。
  - 查记忆：每轮的记忆块末尾多一行 `bin/covey_memory.py search` 的命令（设备上的话题没有），放不下的、收起的、答过的题都能查。
  - 日志：`memory learned written=N of M job=#…`、`memory learn skipped reason=… job=#…`、`memory learn failed reason=… job=#…`、`memory learn superseded …`、`memory learn dropped …`，都不写原话。

### 辅助模型

同时做几件事的分流、整理专项（判断，和整理时先补的话题要点）、记忆自己长是后台的一问一答：给一段 JSON，要一行 JSON，不用工具、不留会话，到点连进程组一起杀掉。它们只从一个出口出去（`app/assist.py`），由设置卡「配置」和网页设置页的**辅助模型**一节决定问谁、用哪个模型和思考预算，所有后台活共用这一套，不再分轻重两套模型（存在 `main-thread.json` 的 `assist`：`harness`、`model`、`effort`、`off`；只有 `harness` 的旧文件照读，模型和预算按 Harness 自己的；旧版本读不到这个字段，回滚后这一项回到“自动”）：

- **自动（跟随管家）**，默认：候选按这个顺序排，只取设置里出现过的 Harness：管家开着（`COVEY_BUTLER` 不是 `0`、私聊模式是管家）时管家的 Harness，再是新话题默认、专项负责人、独立复核里点名的 Harness；这些都没点名时用 Bridge 默认的 Harness。跟着谁就连它的模型、思考预算和供应商一起跟（管家是 Antigravity 的 `gemini-4-neon`、`xhigh`，后台活就都用它、`xhigh`，不悄悄换低一档；管家在 Codex 的某个网关模型上，后台活也走那个网关；`ultra`、`ultracode` 这类编排档按它的思考预算 `xhigh` 问，一问一答用不上编排；跟的设置没写模型时模型和预算都用 Harness 自己的）。过滤掉不能一问一答的（DeepSeek Harness 和扩展里没声明 `ask_once` 的 Harness）、现在用不了的（可用性检查的最近一次结果和最近一轮的失败，只读内存不读盘）、Antigravity 没登录或多账号都被挡住的、正在熔断的，用剩下的第一个。
- **点名一个 Harness**：只用它，和上面几节一样再选供应商、模型、思考预算（选 Harness 时先填上它的第一个模型和那个模型的推荐档，能改）。它没启用、用不了、在熔断，这次就不调模型，各功能走自己的退路，原因写清（“设置里选的 Codex 现在用不了（登录失效）”），**绝不换到别的 Harness**。点名的 Harness 下线了也照旧显示点名，不悄悄改回自动。
- **不用模型（只按规则）**：后台活一律不调模型：分流按新开一件；整理专项没有只按规则的整理，定时整理不跑（不再每天发一张注定失败的卡），你手动点的整理卡写为什么；记忆自己长开着时，主人说“以后/别再”由话题自己在检查点里记（和不开记忆自己长时一样）。

任务卡上的「看看」、晚间汇总的候选和整理记忆本来就只按规则，不调模型；实时归并、巡逻（“我本来想找你的事”）和分流的待命进程已下线（见 [legacy.md](legacy.md)）。

模型的先后：设置里的（点名时选的，或自动跟的那份设置的）> 环境变量 `COVEY_PROJECT_TIDY_MODEL`（Claude Code）、`COVEY_JUDGE_CODEX_MODEL`、`COVEY_JUDGE_AGY_MODEL`（设置里没有模型时才用，这时不带预算）> Harness 自己的（Claude Code 不带 `--model`，Codex 用 `config.toml` 的模型和它自己的预算，Antigravity 用 agy 自己的）。思考预算跟着设置里的模型走，那个模型没有这一档时用 Harness 自己的。三家一样，没有给分流、记忆自己长另挑轻量模型（以前 Claude Code 用 Haiku、Codex 用 `gpt-6-luna`、Antigravity 用 `gemini-3.8-flash`），也不再给整理专项硬写 `medium`。设置卡和网页这一节下面一行写着现在实际用的（“现在：跟随管家，Antigravity · gemini-4-neon · xhigh，分流、整理专项、记忆自己长都用它。”）。

慢模型的代价：分流在你发消息的路上，上限是 `COVEY_TRACK_JUDGE_MS`（Claude Code 6 秒，Codex、Antigravity 10 秒，最多 10 秒），记忆自己长 45 秒；跟着高档预算的大模型时它们常常到点没答（分流按新开一件，记忆这一轮不记），整理专项的上限见[整理专项](#整理专项)。实测 Antigravity `gemini-4-neon`：`xhigh` 补一个话题要点 35 到 116 秒，判断 40 个话题约 12 分钟；想快就在这一节点名一个 Harness，选轻一些的模型或低一些的预算。

**失败了怎么办**（原因码和 `core/result.py` 的 `ERROR_CODES` 同一套，三家各自的报错都先按它们正式轮次的分类认一遍，原话脱敏截断后只进日志和卡上，记录里只留原因码）：

- 熔断、冷却和当天被拦次数都按**通道**记：Harness、这次跑的模型和走的模型网关三样一起算（`core/assist.py` 的 `Lane`），一个通道出事不连累别的（Claude Code 的两个模型、Codex 自己的登录和经网关的模型各是一条；所有后台活用同一个模型，一条通道出事，分流、整理、记忆一起停）。
- 登录失效、额度用完、程序没起来、连不上模型服务、模型在账号里用不了：这条通道 **熔断 15 分钟**，这段时间自动触发的后台活不找它（不影响正式话题）；你自己点的（✨ 整理、管家的 `projects_tidy`）可以越过熔断、冷却和当天的拒绝闸试一次，同一条通道 **1 分钟内只试一次**（1 分钟内再点，照旧说熔断、冷却或拒绝闸的原因，不起进程），成功就解除熔断。自动模式下这次会换下一个候选（一次最多两个）；**分流不换**，它在你消息的路上，只试一次，失败就按新开一件。
- 超时、限流、模型服务临时故障：不熔断，不换家，过一会儿再试。
- 被官方内容安全拦了：不换下一家（同样的内容不再发给你的另一个账号）。整理判断这类整批的，这条通道对这一项 **6 小时内不再送**，卡上说明并给「换辅助模型」；补一个话题的要点、记忆自己长这类单条的，只记这一条。同一条通道一天被拦到 3 次，当天后台不再找它。
- 开了脱敏的话题的内容不外发：每个调用方都要说清这次的内容是不是脱敏话题的（`assist.ask` 的 `redacted` 必填，`tests/test_structure.py` 的 `AssistExitTest` 核对），是的直接拒绝（原因码 `redacted`），不起任何进程。
- 你改了辅助模型，熔断和冷却立即清空。

熔断、冷却、当天被拦次数（按通道记在 `lanes` 里；旧版本按 Harness 记的 `breakers`、`cooling`、`refusals` 照旧读，到期为止对这个 Harness 的每条通道都算数，不再新写）和每项后台活的最近一次（功能、用了谁、模型、网关、结果、原因码、耗时、时间，不存原话）加锁写在 `<state>/assist-state.json`，重启不丢；网页设置页的“后台活最近一次”读它，它一变就推（`app/web_settings.py` 的 `stamps`）。这张表只有三行：分流、整理专项（补话题要点记在这一行）、记忆自己长（`app/assist.py` 的 `SHOWN`）；旧版本记下的其他行不再列出。

**说了不用就不用**：所有后台活只能经 `app/assist.py`（`tests/test_structure.py` 的 `AssistExitTest` 封住直接调 `track_judge` 的 `ask`/`judge` 和后端的 `ask_once`）；起进程前出口再按当时的设置核一遍，不在允许的范围里就拒绝并记一行 `assist refused harness=… feature=…`；`tests/test_assist.py` 一格一格核对 Harness × 设置 × 功能，点名 Codex 和不用模型时 Claude Code 起进程的次数是 0。

**每问现起一个进程**：问之间不留任何进程（以前分流的待命进程每条管家消息都先用一次真实调用热身，已删）。扩展的 `claude_turn` 钩子（如 covey-internal 的协议归一中继 `COVEY_CLAUDE_CLI_NORMALIZE=1`）也包住后台的一次性 Claude Code 调用：它的地址合进 `--settings`，hook 照旧关着。

**慢**：Claude Code 冷启动一问约 3.4 秒，分流最多等 6 秒；Codex、Antigravity 每问都要起一次命令行，实测一次 6 到 15 秒，分流在这里最多等 10 秒（`COVEY_TRACK_JUDGE_MS` 设了就按它）。超时按新开一件，这段时间这句话等着分流。

`COVEY_TRACK_JUDGE=off` 或 `cmd:/absolute/path` 比这一项优先，只管分流，和以前一样。脱敏话题的内容哪里都不送；日志只记原因、毫秒和问的是谁（`via=claude|codex|agy`），不记你的原话。

不归辅助模型管、也不是偷跑的后台进程：每 5 分钟刷新一次启动卡的模型目录（`app/launch_models.py`：Claude Code 起一次 `claude -p` 跑完 `initialize` 列网关的模型、不发对话；Antigravity 跑 `agy models`）；管家空闲时的压缩（`app/idle_compaction.py`）跟着管家自己的 Harness；换配置后第一轮的交接摘要用离开的那个 Harness 和旧会话（`app/switch_handoff.py`：旧会话末轮因登录、额度、模型不可用、安全拒绝、网关断开等停下，或它在后台熔断、额度被挡时直接把最近几轮原文交给新模型，不再等它；握手 30 秒没回应也一样）；每分钟的可用性检查（`claude auth status`）和 Antigravity 的额度查询（`agy -p /usage`）。

### 新话题带上最近话题的要点

`COVEY_WORKSPACE_GISTS`（不设或空是 3，`1` 到 `10` 照写的数，`0` 或其他值是关）：一个话题的新会话开始时，规则里多一块“工作区 X 最近几个话题的要点”，是同一个聊天、同一个工作区里最近有过任务的这么多个别的话题的[话题要点](projects.md#整理专项)（本质和现在的目标，每个话题一行），让新话题知道这个工作区最近在忙什么（`app/workspace_gists.py`，块的开头一句在 `app/prompts.py` 的 `WORKSPACE_GISTS_HEAD`）。

- 只给新会话的规则，续接的轮次不带，所以一个会话的规则不会跟着别的话题变。
- 只在主人自己的一轮、普通话题里带；管家的话题、代答话题不带。只看同一个聊天的话题：群里的话题不会读到私聊的要点，反过来也一样。开了脱敏的话题的要点只给开了脱敏的话题。
- 话题要点是整理专项（`COVEY_PROJECT_TIDY`）开着时每轮写的；没有要点的话题跳过，所以整理专项关着时这一块总是空的。只看最近 14 天有过任务的话题。
- 设成 `0`：规则和没有这个功能时逐字相同。设置卡「诊断」页下半的“能做什么”里有这一行。

### 知识条目与历史搜索

两个变量互相独立：知识条目默认开，历史搜索要自己指一个命令。关着（`COVEY_KNOWLEDGE=0` 或没设 `COVEY_HISTORY_CMD`）时不建目录、不改规则和提示词、管家的工具清单里没有对应的工具、汇总里没有这一节。设计和实现见[管家 · 知识库](butler.md#知识库)，主人怎么用见[使用指南 · 知识条目](usage.md#知识条目)。

- `COVEY_KNOWLEDGE`：默认开，设 `0`、`off`、`false`、`no` 才关。记录在 `<state>/knowledge/`；管家的知识条目工具已换成一直列着的记忆工具（`memory_save`、`memory_revise`、`memory_search`，[butler.md 工具清单](butler.md#工具清单)），这个开关不再增减工具；每个话题的新会话带上条目，续接的会话只补上一轮之后新增或改动的；记忆（`COVEY_MEMORY`）关着时，主人在话题里明确说“记一下 / 记住 / 记下来”，Bridge 在那一轮结束后把检查点的 `remember` 记下来（记忆开着时归记忆的那一条检查点规则，记成资料）；早晚汇总多“新记的知识”。
- `COVEY_HISTORY_CMD=/absolute/path/to/kb-history`：历史搜索命令的**绝对路径**（相对路径等于没设）。设了以后，主人自己的话题规则里多一行“要查以前做过的事用 `<命令> search <一句话>`”，管家多 `history_search`（跑 `<命令> search [--workspace W] [--since Nd] -- <一句话>`，自己的进程组里最多等 70 秒，超时整组杀，输出最多 20 KB）。命令本身不在这个仓库。
- 关掉：按[改开关三步](#改开关三步)设成 `0`；已经写下的 `knowledge/` 留着。
- 日志：`knowledge remember written=N job=#…`、`knowledge remember ignored reason=… job=#…`、`knowledge remember failed job=#…`；管家工具的 `main tool name=knowledge_save …`。

### 管家

`COVEY_BUTLER` 决定部署允不允许管家（私聊模式“管家”，见[管家](butler.md#私聊模式)和[使用指南 · 管家](usage.md#管家)）：

- `0`：不允许。不起后台线程、不改消息路由，设置卡没有管家的标签。
- 不设或 `1`：允许。用不用由主人在设置卡上选的模式定（`<state>/main-thread.json` 的 `mode`），改模式不用重启。新部署第一次启动写成管家，日志 `main thread mode settled mode=agent|topics`。
- 管家的设置在 `<state>/main-thread.json`，工具请求在 `<state>/main-thread/requests/`。管家的会话没选 Harness 时用 Claude Code，没启用 Claude Code 时用启用了的 Codex 或 Antigravity，再没有时 DeepSeek Harness（`app/butler_harness.py`）；设置卡「配置」页的管家一段可以换（`harness`，见[管家 · 换 Harness](butler.md#换-harness)）。Claude Code 没调通的新装，`./install` 在服务第一次启动前写好调通的那个，如 `"harness": "codex"`（`core/butler_settings.py` 的 `preset_harness`，文件里已经有 Harness 的不动）。
- `main-thread.json` 里旧的 `"desk_topics": true` 读到也按 `false` 处理，启动时记一行 warning，见[已下线的功能与旧配置](legacy.md)。
- 专项开着时，话题能把只有管家能办的事转给管家（检查点 `ask_butler`，记录在 `<state>/butler-asks/`），没有单独的开关，见[在话题里说开专项](projects.md#在话题里说开专项)。

### 管家规则

`COVEY_BUTLER_RULES`：默认开，设 `0`、`off`、`false`、`no` 才关。管家的规则里，“只读的、一两分钟能做完的自己做完直接答”和“可能超过一两分钟的交给话题”这两条换成管家规则：几秒能答完的直接答，要查、要跑、要改、预计超过半分钟的用 `topic_start`、`topic_send` 派给话题，对主人只说一句放到哪个 T 号、大概多久。主人明确说“你直接做”时例外。`topic_start` 的工具说明也换成同样的说法（Bridge 把变量交给管家会话里的 MCP 服务，由它列工具）。其余规则一条不动；设成 `0` 时每轮提示词和工具说明都回到只读的自己做完、超过一两分钟交给话题的老说法（`tests/golden/butler_prompt.txt`）。规则全文和旧会话什么时候用上新规则，见[管家 · 管家规则](butler.md#管家规则)。

- 关掉按[改开关三步](#改开关三步)做：设 `COVEY_BUTLER_RULES=0`。
- 不影响话题的规则、工具清单（只换 `topic_start` 的说明）和卡片；跑过 `CARD_AFTER`（90 秒）才出卡的兜底照旧，没有另设变量。

### 派发卡

`COVEY_DISPATCH_CARD`：默认开，设 `0`、`off`、`false`、`no` 才关。管家用 `topic_send` 把一件事转给已有话题后，在主人那条提问下面发一张**活卡片**（回复到提问、进它的串，聊天区顶层只留管家的回答；`topic_start`、`topic_fork` 开新话题时不发：话题串挂在提问下面，或它的话题卡就在顶层；带 `project` 的转交照旧发在顶层，顶上“你问”块链到提问）：卡头是话题的 T 号和标题加状态（在做、等你、做完、失败、已停），一行灰字写开始时间和用时，折叠面板“进度”写工作区和当前步骤，下面是输入/输出灰框，按钮是补充、停止、打开话题。`dispatch_cards` 后台线程每秒读一次话题的任务记录，内容变了且离上次写超过 2.5 秒才整卡刷新一次。话题的结论回带发出后，卡缩成一行“✅ 做完 · 看结论 ›”（链到聊天区的结论），之后不再刷新。说明见[管家 · 派发卡](butler.md#派发卡)。

- 关掉按[改开关三步](#改开关三步)做：设 `COVEY_DISPATCH_CARD=0`；关着时不起后台线程、不发这张卡，话题的任务卡、回带和小卡（`hook_card`）和原来一样。
- 记录在 `<state>/main-thread/dispatch/`，7 天没动的由维护删。

### 群聊免@

`COVEY_GROUP_ALL_MESSAGES=1`：主人可以按群选谁说话不用 @ 机器人（见[在群里用](usage.md#在群里用)）。只认 `1`。

- 要先给应用开通 `im:message.group_msg`（获取群组中所有消息，敏感权限，可能要企业管理员审批）和 `im:chat.members:read`（选人时列群成员），并发布新版本。
- 设置记在 `<state>/groups/<…>.json` 的 `free_talk`、`free_talk_members`。不设变量时卡上没有免@，这些设置不生效。
- 开通权限后机器人所在每个群的全部消息都会推给 Bridge：没人免@ 的消息在准入一步丢掉，不写历史、不记日志。日志：`group chat=… free_talk=…`；被拒的免@ 消息是 `ignored message … reason=for_others|proxy_chat`。

### 群成员只读

群聊卡上每个已启用的群都可以“设为只读”：群成员（不是你）开的任务只能读文件和搜索（Claude 只有 Read、Grep、Glob，不挂你的 MCP；Codex 用只读沙箱；别的 Harness 做不到，这一轮不启动），你自己的消息照旧。选过的群记在 `<state>/groups/<…>.json` 的 `member_mode`。

`COVEY_GROUP_MEMBERS=read`：没在群聊卡上选过的群也按只读（启用确认卡照这个写）；不设或别的值是 `full`，和以前一样。卡上选过的以卡上为准。日志：`group chat=… member_mode=…`、每轮 `turn limit job=#N limit=read`。

### 主目录与仓库地图

主目录就是服务的 `--workdir`（参数名不变）。话题记录里写着 `home_start` 的话题从主目录启动；管家和负责人开的、启动卡和网页开的、专项负责人和分叉出来的话题都由 `core/topics.py` 的 `mark_home` 写它，同时只写一次 `started_at` 和 `slug`（话题产物目录按它们命名，之后不再变）。设备上开的话题不算。之前开的话题照它的 `workdir` 跑，规则里照旧是工作区那一段。第一次启动时 `core/migrate.py` 的 `home_v1`、`repos_v1` 两步写 `home.json` 和仓库地图（只在文件不在时写；`--apply` 的维护没有主目录，留给 Bridge 启动时做）。

- **主目录**（`core/home.py`）：接管的已有目录（你装之前就有的）一个文件都不写；Covey 新建的主目录（新装默认 `<数据根>/home`，`seed_home`）有一份不超过 60 行的 `AGENTS.md`（同样内容的 `CLAUDE.md`）、`.ignore`、`.skills/`（`.claude/skills`、`.agents/skills` 指向它）和 `tasks/{sessions,runs,shared}/`，另有标记文件 `.covey-home`。
- **`<状态目录>/home.json`**：话题产物目录和会话目录的模板（`runs_template`、`sessions_template`，变量 `{home} {date} {no} {slug} {topic8} {run}`）、`run_scope`（`topic_day` 同一话题同一天共用一个运行目录，`turn` 每轮一个）、`cache_env`（`true` 时 Harness 进程的 `TMPDIR`、`XDG_CACHE_HOME`、`PIP_CACHE_DIR`、`UV_CACHE_DIR`、`npm_config_cache` 指到运行目录的 `tmp/`、`cache/`，写不进就不设，不悄悄退回 `/tmp`）。有这个文件时话题规则里才写“本话题产物目录”；Covey 新建的主目录第一次启动时写默认值，接管的目录不写（产物放哪由它自己的 `AGENTS.md` 说）。
- **仓库地图** `<状态目录>/repos.json`（`core/repomap.py`，唯一来源）：每个仓库的名字、路径、用途、状态（`active`；`readonly`、`frozen` 不能改）、写入方式（`worktree` 按话题建 worktree，`inplace` 原地改；Covey 新建的主目录默认 `inplace`，接管的默认 `worktree`）、默认分支、规范文件。第一次启动从旧的工作区列表（`workspace-config.json`，用途取 `main-thread.json` 的 `workspace_notes`，写着“冻结”的记成 `frozen`）和主目录下一层的 git 仓库（`.git` 是目录的；`.git` 是文件的是 worktree，不列）生成，旧文件保留。话题碰过、地图里没有的主目录下的仓库，一轮结束时补进来（数据根和 `exclude` 里的目录除外）。`repos.md` 由它渲染、不要手改：Covey 新建的主目录放主目录里，接管的放 `<状态目录>/repos.md`。地图只经 `core/repomap.py` 的 `update` 写（文件锁 `repos.json.lock`；新版本写的键和读不懂的条目原样保留，读不出来的文件不覆盖），设置页每次改完立刻重新渲染 `repos.md`。
- **环境变量**：每个 Harness 进程（本机的，设备上的不给）都有 `COVEY_HOME`（主目录）、`COVEY_TOPIC`（话题 id）、`COVEY_STATE`（状态目录），`PATH` 最前面是 `<数据根>/bin`；任务目录沿用 `COVEY_TASK_DIR`。`<数据根>/bin/covey` 是转发壳（`covey repo …` 到 `bin/covey_repo.py`，`covey memory …` 到 `bin/covey_memory.py`），安装和每次部署（`scripts/deploy_bridge.sh`）时写成指向新 release。同一个目录里还有 `rg`、`find`、`fd`、`du`、`tree`、`grep`、`ls` 的搜索垫片（不限深度地递归搜整个主目录时拒绝，其余原样执行真程序），和 `covey` 一起写，规则见 [Harness · 执行权限](harnesses.md#执行权限)。
- **`covey repo`**：`list`（地图，标出本话题用到的）、`use <仓库|路径> [--write]`、`status`（本话题用到的仓库、目录、分支、没提交的改动）、`prune-plan [--legacy <目录>]`，都可加 `--json`。`use` 打印仓库目录和它的规范文件开头（`AGENTS.md`/`CLAUDE.md`，至多 4 KB），只写本任务的 `runs/<任务键>/repos.json`（`core/run_dir.py` 的 `REPOS`），一轮结束时 Bridge 把它并进话题记录。规范被截到 4 KB 时不算读过（`rules_cut`），话题里仍记“未读”。
- **worktree**：`use --write` 在 `<数据根>/worktrees/T<号>-<话题 id 前 8 位>/<仓库>/` 建或复用本话题的 worktree，分支 `topic/T<号>-<id8>`，从远端主干开（`git fetch` 至多 20 秒，取不到用本地主干），同一话题同一仓库加锁，不会建两次；`git worktree add` 中途失败时删掉它刚建的分支。基线提交（`base_oid`）记进 `repos.json`，这一轮的改动对比从它算。`inplace` 的仓库不建 worktree，直接在仓库里改；`readonly`、`frozen` 的仓库 `--write` 被拒。
- **只读用仓库**：不带 `--write` 的 `use` 不读主检出（它可能落后主干几十个提交），而是先 `git fetch`，在 `<数据根>/worktrees/_read/<仓库>/<提交前 12 位>/` 建或复用 `origin/<主干>` 的 detached 只读 worktree，打印它的路径、提交号和提交时间，规范开头也从它取；同一提交各话题共用。fetch 没成功时用本机上次取到的 `origin/<主干>`，写明“可能落后，未能确认是最新主干”；建不了（如 Codex 只读沙箱里还没有这个提交的只读 worktree）时读仓库目录本身，同样写明。没有 `origin` 的仓库和 `inplace` 的仓库照旧读仓库目录。话题记录里只读条目带这个 worktree，每轮的仓库清单写出它的提交号和时间；本话题已在改的仓库，只读不会换掉它自己的 worktree。
- **每轮提示**：从主目录启动的话题，系统规则里是主目录、仓库地图路径和主目录的规矩（话题内不变，能命中缓存）；每轮在消息末尾列本话题用到的仓库和规范读没读（`app/repos_turn.py`）。管家或负责人点名的仓库，第一轮就把规范开头（至多 4 KB）放进消息；某一轮改了一个仓库却没读它的规范，下一轮也放。放进去的是全文才算读过；只放了开头的仍记“未读”，不会每轮重放，之后写了它却还没读，下一轮再放；改到了仓库的主检出（不是本话题的 worktree），下一轮提醒改用 worktree。之前开的话题不列仓库清单，但后两条照样有（它自己 `workdir` 所在的仓库除外：那是它原来的工作方式）。一轮的结果发出之前，Bridge 把它用到的仓库并进话题记录，下一轮读得到。
- **改动对比**：一轮开始前对话题要写的每个仓库（它的 worktree，或原地改的仓库；worktree 还没建的不拍主检出）各拍一次快照；这一轮中途新建的 worktree 从它的 `base_oid` 算（复用的不算，之前留下的改动不归这一轮）；写过却没有基线的仓库只列文件名，不出 git diff（`core/diff.py`）。别的话题的 worktree 不会记成本话题的。
- **Codex 的沙箱**：`workspace-write` 时可写根除检查点目录外，还有本任务的运行目录、`<数据根>/worktrees/T<号>-<id8>`（开轮前先建好）和本话题要写的每个仓库的 `.git`（原地改的连仓库本身）。可写根在一轮开头定下：这一轮才用 `covey repo use --write` 声明的仓库，下一轮才进可写根，命令会这样提示。`danger-full-access` 不受影响。数据根在主目录里面时，worktree 本来就在 Codex 能写的范围里。
- **清理**：只读 worktree 每个用到它的话题每轮结束时续期（旁边的 `<提交前 12 位>.used`）；`use` 建新的只读 worktree 时，同一仓库一天没被用过、干净的只读 worktree 用 `git worktree remove`（不加 `--force`）删掉，有改动或未跟踪文件的留着。被删了的，话题的仓库清单会提示重跑 `covey repo use`。按话题分目录的写 worktree 不自动删。`covey repo prune-plan [--legacy <目录>]` 只读，列出每个 worktree 干不干净、合没合进主干、多久没动，以及清理用的命令（干净、已合入、一天没动的才给），不执行。
- 记忆的同义词表 `<状态目录>/memory/synonyms.json` 可选，和核心自带的通用表合并。

### 多设备（开发中，默认关）

让话题在另一台机器（Linux，经 ssh）上工作，而不是在 Bridge 所在的机器上。设备上能跑 Claude Code、Codex 和 Antigravity 的话题；默认关，真机验收在进行。结构、连接、离线恢复和文件进出见[架构 · 设备](architecture.md#设备多设备默认关)，Harness 在设备上的差别见[Harness 说明](harnesses.md#在设备上运行)。

**打开**：`COVEY_DEVICES=1`，再写 `devices.json`（`$COVEY_DATA_HOME/devices.json`，或 `COVEY_DEVICES_FILE` 指的文件），改完文件重启。设备要能从 Bridge 所在的机器免密 ssh 过去，而且 `known_hosts` 里已经有它的主机密钥（先手动 `ssh` 一次）：Covey 从不问密码。

```json
{"schema": 1,
 "defaults": {"patience_minutes": 15, "waiting_patience_hours": 6, "connect_seconds": 5},
 "devices": {
   "gpu1": {"title": "GPU 1", "ssh": {"host": "gpu1", "options": ["-p", "2222"]},
            "harnesses": ["claude", "codex"], "env": {"HTTPS_PROXY": "http://proxy.example:3128"}}
 }}
```

| 字段 | 含义 |
| --- | --- |
| `schema` | 必填，现在是 `1`。写错的字段名、多出来的字段都会报错，不会悄悄忽略 |
| `defaults` | `patience_minutes`（断线多久后这一轮按中断处理，默认 15，1 到 1440）、`waiting_patience_hours`（Agent 在等你回答时放宽到多久，默认 6，1 到 168）、`connect_seconds`（ssh 连接超时，默认 5，1 到 60） |
| 设备的 `patience_minutes`、`waiting_patience_hours` | 可选，只改这台的耐心值（范围同 `defaults`） |
| `control_dir` | ssh 主连接套接字放的本地目录：绝对路径，不放网络盘，最长 41 字节（Unix 套接字路径有长度上限，太长时配置报错）。不写用 `/tmp/covey-ssh-<uid>` |
| 设备别名 | `devices` 的键，`[a-z][a-z0-9-]{0,23}`。工作区写成 `别名:路径`，例如 `gpu1:/home/me/proj`、`gpu1:~/work`（`~` 是设备上的家目录） |
| `title` | 卡片上显示的名字，默认就是别名 |
| `home` | 可选，设备上的主目录：`/` 开头的绝对路径，或 `~`、`~/…`（`~` 是设备上的家目录，不在 Bridge 这边展开）。不写时照旧 |
| `ssh.host` | `~/.ssh/config` 里的别名或 `user@host`。不能有空格，不能以 `-` 开头 |
| `ssh.options` | 追加的 ssh 参数（`-J` 跳板、`-p`、`-i` 等）。Covey 自己的选项排在前面，这里改不掉它们 |
| `ssh.python` | 设备上的解释器，默认 `python3`（3.9 起） |
| `harnesses` | 这台设备上要用的 Harness：`claude`、`codex`、`agy` 的子集（`core/roster.py` 里 `devices` 为真的，扩展的 Harness 也看这一列），不写时是 `claude`、`codex`。`dsh` 不能写，配置报错时点名（原因见 [Harness 说明](harnesses.md#在设备上运行)） |
| `env` | 只写要覆盖设备环境档案的变量（比如这台机器出网要的代理）。值里不能带 `user:密码@` |
| `login_shell` | `true` 时自检再用登录 shell 取一次环境变量（`.bashrc` 里设的代理）。默认 `false` |
| `spaces` | 谁可以用这台设备：`owner_private`（默认，主人私聊）、`personal_groups`、`team`（显式写才开）。代答群和没绑定的群永远不能用 |

**用**：主人在启动卡「其他目录…」或设置卡「工作区」页「＋ 添加工作区…」的目录浏览器里输入 `gpu1:/home/me/proj`，登记成一个设备工作区（名字取 `别名-目录名`），开话题时选它。设备没答复、目录不存在、别名不在 `devices.json` 里都当场报错。管家和专项负责人也能把话题开在设备工作区（[管家 · 开话题](butler.md#开话题转交分叉)）：开之前一样查设备名单、`spaces`、声明的 Harness 和目录，查不过就不开，回复写明原因。

**自检**：`python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1`（`list` 只打印每台设备最近一次的结果），新加设备、改了 ssh 或代理设置后跑一次，见[运维 · 设备自检](operations.md#设备自检)。失败时写明是哪一步：连不上、没装、没登录、超时，或连不上模型服务（去哪里设 `HTTPS_PROXY`）。

**已知限制**：
- 只有 Linux 设备真机验证过；能上设备的是 Claude Code、Codex 和 Antigravity，dsh 不能。Antigravity 在设备上用设备自己的 Google 登录（没登录时这一轮当场拒绝），不能执行中补充（下一轮再说）。
- 设备上的话题没有回退、压缩、敏感词脱敏和分叉（按钮不出现）；开着脱敏的话题在设备上的一轮直接拒绝。
- 管家自己的工作区不能是设备；专项开在设备工作区时，负责人留在 Bridge 本机，专项的话题开在设备上。不能用「独立 worktree」（管家设置是独立 worktree 时，设备话题出预选原目录的启动卡），换配置不生成旧模型的摘要。
- 任务卡和答案卡不显示改动摘要；产物只能是设备工作区里的文件（目录请打包）；回复里指向设备文件的 Markdown 图片不上传。
- 设置卡上设备的状态行读的是最近一次自检的结果，不会自己去自检。

### 节点与机器状况（默认关）

设了 `COVEY_NODE_LISTEN` 才有：每台机器（本机和加进来的设备）上一个常驻的节点，每 10 秒报一次 CPU、内存、盘、GPU 和占用最多的进程，出事时发告警卡。结构、连接协议和记录见[架构 · 节点](architecture.md#节点默认关)。

- **监听**：`COVEY_NODE_LISTEN='[::]:$RESERVED_PORT'`（`[::]:端口`、`IPv4:端口` 或只写端口；`$名字`、`${名字}` 每次启动时从环境里取值，平台按机器分配端口时用单引号原样存进清单）。节点主动连这个端口（TLS，Hub 自签证书，节点按证书指纹认 Hub），设备上不开端口。设备连不到 Hub 推算的地址时用 `COVEY_NODE_ADDRESS=主机:端口` 指定。
- **本机**：开了就有一个本机节点（显示为“本机”），由 Bridge 每分钟确认在跑、没在跑就拉起；它独立于 Bridge 运行，样本先写本地盘（`~/.covey/node/data/`，最多 256 MiB、7 天）。
- **加设备**：设置卡的「设备」页给出一行命令（`curl … | python3 -`，口令 30 分钟有效、只能用一次），在设备上执行，节点就装好、写自启并连上来；Hub 能 ssh 进去的设备也可以在那里直接装。
- **告警**：内存工作集到上限的 90% 持续 1 分钟、发生 OOM、盘用到 90%、试写数据根失败、设备 10 分钟没消息。能对上任务的内存告警发到那个话题，其余发到私聊；恢复后原卡改成“已恢复”；每小时最多 6 张。开着时[内存看门狗](operations.md#内存看门狗)的“容器匿名内存”告警不再发（同一件事不收两张卡），它杀失控搜索进程的告警照发。
- **移除**：在设置卡「设备」页移除，节点下次连上来（或在线时立刻）删掉自启和 `~/.covey/node/` 后退出。

### 飞书 SDK 与直连

`./install` 新装的服务默认写下面两个开关（`core/switches.py` 的 `INSTALL`，它们是安装器唯一写进清单的开关，代码里的默认仍是 lark-cli，因为老服务的 Python 里可能没装 SDK）：服务的 Python 里没有 `lark-oapi` 时先 `pip install`（装进用户目录，或这个 Python 所在的虚拟环境），装不上就不写、照旧走 lark-cli，安装器最后几行说怎么补。装这个版本之前注册的服务不变，要的照下面装好依赖再用[改开关三步](#改开关三步)打开。

- `COVEY_CARD_TRANSPORT=sdk`：CardKit 卡片接口、消息的发送和回复、读消息详情都用常驻的 `lark-oapi` 客户端；上传文件和图片、转发话题等只有 lark-cli 做得了的仍走 lark-cli（见[投递、重投与 outbox](architecture.md#投递重投与-outbox)）。先在运行 Bridge 的那个 Python 里 `python3 -m pip install '.[feishu-sdk]'`。SDK 只支持显式命名的 profile，应用密钥只在内存里读取。请求发出前取不到凭据时退回 CLI（日志 `CardKit SDK unavailable locally; using CLI`）；请求已发出后不换传输重发。
- `COVEY_EVENT_TRANSPORT=sdk`：Bridge 不起 lark-cli 的事件消费者，用一个 `lark_oapi` WebSocket 长连接收四类事件（消息、卡片点击、机器人菜单、已读回执），卡片点击在应答里立刻回 toast“收到”。Bridge 重启时它在核对完身份后就连上，启动恢复期间的点击和消息先收下（点击回“收到，机器人刚重启，稍后处理”），恢复做完再处理。收下的消息和点击处理完之前一直记在状态目录的 `event-spool/`，停止、重启或崩溃时还没处理的，下次启动先补处理（消息 24 小时内、点击 10 分钟内、菜单 2 分钟内的），不会重复建任务。同一个应用只能有一个事件长连接，所以**打开后四类事件全走 SDK**，同时也不能在别处再跑这个应用的 `lark-cli event consume`。依赖同上（`.[feishu-sdk]`），要显式命名的 profile。切回：去掉这个变量（或设 `cli`）并重启 Bridge。细节见[事件长连接走 SDK](architecture.md#事件长连接走-sdk默认关)。
- `COVEY_FEISHU_DIRECT`：默认直连飞书开放平台，`0`（也认 `false`、`no`、`off`）时都走环境里的代理。直连时 Bridge 启动先试一次连 `open.feishu.cn:443`（lark 品牌是 `open.larksuite.com`），连得上就把它加进本进程的 `no_proxy`、`NO_PROXY`，之后的子进程都继承，日志 `Feishu direct: lark-cli reaches open.feishu.cn without the proxy`；连不上记 `Feishu direct unavailable (<异常类型>); lark-cli keeps the proxy`，环境不动。事件长连接的主机不在其中，仍走代理。

### 其他

- `COVEY_APP_CHECK=0`：不检查飞书应用、不发「飞书应用待补」卡。检查和补齐见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用)。
- `COVEY_REVIEW`：默认开，设 `0`、`off`、`false`、`no` 才关。改了代码的一轮（`core/diff.py` 记到改了文件，或检查点的 `delivery` 写了提交号），答案卡和回带卡的灰框下面多一个「独立复核」（`app/review.py`）。只在主人点了才开话题，从不自己开；用什么模型在设置卡「配置」页的“独立复核”一段选（存 `main-thread.json` 的 `review`，不选是自动挑），开关开着时这一段才出现；关了就没有这个按钮，已发出的卡上的按钮点了照样开（开关只管画不画）。设备上的话题不给。
- `--health-timeout`：`launch` 的参数，默认 120 秒，0 关闭。Bridge 本地健康接口连续这么久没有响应就替换进程。bootstrap 不传它，所以经 bootstrap 注册的都是 120。
- `COVEY_PRIVACY_DENYLIST`（只给 `scripts/privacy_check.py` 读，Bridge 本身不读）：指向仓库外的文本文件，每行一个公司域名或内部名词，隐私检查按它查；不设就只查通用规则，扩展里放有一份，见[隐私检查](testing.md#隐私检查)。

## 环境变量与选项一览

**命名**：设置的名字是 `COVEY_<X>`，旧名 `LARK_BRIDGE_<X>` 照常生效，不用改任何配置（`covey/core/env.py` 的 `get` 先读新名；只设了旧名时 stderr 记一行弃用）。下表只写新名，每一行的旧名就是把 `COVEY_` 换成 `LARK_BRIDGE_`；管家改名时连名字一起改了的七个开关在行里注明旧名，读取顺序是 `COVEY_<新>`、`COVEY_MAIN_<旧>`、`LARK_BRIDGE_MAIN_<旧>`（`core/env.py` 的 `RENAMED`）。改名之后才有的开关（`core/env.py` 的 `BORN_NEW`）没有旧名。`LARK_<HARNESS>_BRIDGE_*` 这类 Harness 自己的前缀不变。服务清单为什么还存旧名、子进程拿到哪些写法，见[已下线的功能与旧配置 · 环境变量的旧名](compat.md#环境变量的旧名)。

代码在 `covey/` 和 `bin/` 里读的每个环境变量和命令行参数都列在这里，`tests/test_structure.py` 的 `InventoryTest` 核对：代码读了就要有一行，行里写出读它的文件；表里写了的代码也要读。不列 `HOME`、`PATH`、`PYTHONPATH`、`XDG_CONFIG_HOME`、`NO_PROXY`/`no_proxy` 这类系统变量。不再读取的环境变量不在表里，只有两个权限变量还为拦截而读（见[不再读取的变量](legacy.md#不再读取的变量)）。

| 环境变量 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `COVEY_DATA_HOME` | `core/paths.py`、`service/doctor.py`（体检里自托管网页的数据根）、`service/web_selfhost.py`（`wanted`：自托管网页的数据放它下面的 `web/`）、`core/devices.py`（找 `devices.json`）、`core/extensions.py`（找本部署启用、停用的扩展 `extensions/enabled`） | 按 [数据目录](operations.md#数据目录) 推算 | 否 |
| `COVEY_SERVICE_ROOT` | `core/paths.py` | `<数据根>/bridge-services` | 否 |
| `COVEY_REVIEW` | `app/review.py`（`enabled`，`buttons` 据此画按钮） | 开；`0`、`off`、`false`、`no` 关：改了代码的答案卡和回带卡不画「独立复核」 | 否 |
| `COVEY_STATE_DIR`；`LARK_CLAUDE_BRIDGE_STATE_DIR`、`LARK_DSH_BRIDGE_STATE_DIR`、`COVEY_AGY_STATE_DIR` | `app/harnesses.py`（只有默认 Harness 读，其余总用 Bridge 的 `--state-dir`） | `<数据根>/<Harness 的目录名>` | 否 |
| `COVEY_PROFILE` | `backends/codex/bridge.py`、`backends/dsh/bridge.py`、`backends/agy/bridge.py`（Bridge 的 `--profile`） | 空：lark-cli 的默认 profile | 否 |
| `COVEY_MESSAGE_FORMAT`、`LARK_CLAUDE_BRIDGE_MESSAGE_FORMAT`、`LARK_DSH_BRIDGE_MESSAGE_FORMAT`、`COVEY_AGY_MESSAGE_FORMAT` | `feishu/messenger.py`（各 Harness 发消息时读） | `markdown` | 否 |
| `COVEY_MESSAGE_CHUNK_LIMIT`、`LARK_CLAUDE_BRIDGE_MESSAGE_CHUNK_LIMIT`、`LARK_DSH_BRIDGE_MESSAGE_CHUNK_LIMIT`、`COVEY_AGY_MESSAGE_CHUNK_LIMIT` | `feishu/messenger.py` | 12000 | 否 |
| `COVEY_CARD_TRANSPORT` | `feishu/sdk.py`（`enabled()`） | 空：lark-cli；`sdk` 走 SDK（`./install` 新装写 `sdk`） | 否 |
| `COVEY_EVENT_TRANSPORT` | `feishu/ws_intake.py`（`enabled()`）、`service/doctor.py`（诊断「要你补的」：还是 lark-cli 消费进程时提示） | 空：lark-cli 的事件消费者；`sdk` 走 SDK 长连接（`./install` 新装写 `sdk`） | 否 |
| `COVEY_AUTO_UPGRADE` | `app/auto_upgrade.py`（`at()`） | 空：不自动升级；`HH:MM`：每天这个时间（Asia/Shanghai）照 `upgrade.json` 安全升级一次，见 [deploy.md 定时自动升级](deploy.md#定时自动升级) | 否 |
| `COVEY_FEISHU_DIRECT` | `feishu/sdk.py` | 开：直连飞书，`0` 走代理 | 否 |
| `LARKSUITE_CLI_DATA_DIR` | `feishu/sdk.py`（找 lark-cli 的凭据） | `~/.local/share` | 否 |
| `COVEY_OUTGOING_MEDIA_DIRS` | `feishu/outgoing_images.py` | 空 | 否 |
| `COVEY_ACCEPT_CHATS` | `feishu/web_service.py`（验收页面，`accept_chats`） | 空：验收页面不打开、不操作任何会话。做真机验收时写验收用的会话标题，逗号分隔，设在起验收服务的那个进程的环境里 | 否 |
| `COVEY_FEISHU_WEB_HOST` | `feishu/web_client.py` | 扩展的 `web_tenant_domain`，没有时 `open.feishu.cn` | 否 |
| `PLAYWRIGHT_BROWSERS_PATH` | `feishu/web_client.py`（验收页面的浏览器）、`feishu/app_console.py`（开发者后台的浏览器）、`feishu/install_browser.py`（`./install` 的浏览器会话）、`core/browser_env.py`（定位与 `launch.json`） | `<数据根>/browsers`（存在时），否则 `~/.cache/ms-playwright`（`./install` 设成 `<数据根>/browsers`） | 否 |
| `COVEY_BROWSER_EXECUTABLE` | `feishu/web_client.py`（`launch`）、`core/browser_env.py`、`service/browser_setup.py` | 空：用 Playwright 下载的 Chromium；`./install` 发现机器上已有 Chrome/Chromium 时写进 `<数据根>/browsers/launch.json` | 否 |
| `LD_LIBRARY_PATH` | `core/browser_env.py`、`service/browser_setup.py`（无 root 缺 Chromium 系统库时，把 `<数据根>/browsers/syslibs/` 下解出的库目录加在前面，只给浏览器进程） | 不动 | 否 |
| `TMPDIR` | `service/browser_setup.py`（监视浏览器下载有没有进展的临时目录；`./install` 设成 `<数据根>/tmp`） | `<数据根>/tmp` | 否 |
| `ANTHROPIC_BASE_URL` | `app/redaction_panel.py`（「探测」）、`backends/claude/redaction.py`（脱敏中继的上游）、`core/same_account.py`（Claude Code 自己的登录去哪：和某个模型网关是同一账号时菜单只列一次） | 空 | 否 |
| `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY` | `core/same_account.py`（只取摘要比对 Claude Code 自己的登录和模型网关是不是同一把密钥，不存、不显示） | 空 | 否 |
| `COVEY_PINNED_OVERVIEW` | `app/overview.py`（`app/pins.py` 的 `laid` 也读：两个开关都开时看板记录的规则才算数；`app/prompts.py` 的 `area_rule` 也读：开着才在管家规则里说“进度主人看 Pin”） | 开；`0`、`off`、`false`、`no` 关 | 否 |
| `COVEY_GROUP_ALL_MESSAGES` | `app/access.py`（`free_talk_on`；群聊卡 `app/group_manager.py` 经它） | 关；`1` 开（还要应用开通 `im:message.group_msg`） | 否 |
| `COVEY_GROUP_MEMBERS` | `app/access.py`（`members_default`；群聊卡、启用确认卡和 `app/tool_limits.py` 经它） | `full`：没在群聊卡上选过的群，群成员的任务和你的一样；`read`：这些群的群成员任务只读（见[群成员只读](#群成员只读)） | 否 |
| `COVEY_DIGEST_TIMES` | `app/digest.py`、`core/app_manifest.py`（菜单「看板」不看它，每个部署都要）、`app/tidy_run.py`（`scheduled`：整理专项的定时也按这些时间）、`core/quiet_hours.py`（`COVEY_QUIET_HOURS` 没设时，夜里是最后一个时间到第一个时间） | 空：`09:30,21:30`；写了时间就用写的；`off`（也认 `0`、`no`、`none`、`false`）：不发早晚汇总，整理专项也没有定时的一次 | 否 |
| `COVEY_REVISIT` | `core/revisits.py`（`enabled`：`app/revisits.py` 的 `revisits` 后台线程、`app/runs.py` 普通话题每轮的规则行、`app/digest.py` 看板的“到点回来看”一组都看它；任务进程里 `app/turn_delivery.py` 据此决定没有变化的那一轮不发） | 开；`0`、`off`、`false`、`no` 关：检查点里的 `revisit` 不读，不写 `revisits/`，和没有这个功能时一样。见[到点回来看](usage.md#到点回来看) | 否 |
| `COVEY_COLLAB` | `core/turn_source.py`（`enabled`：`refusal` 据此决定协作轮跑不跑；问它的有 `app/job_dispatch.py` 的 `refuse_source`，`app/worker.py` 开跑前）、`app/collab_flow.py`（线程、规则行、检查点的 `collab_ask`）、`app/collab_checks.py`（预检）；负责人这一侧也问 `turn_source.enabled`：`app/project_lead.py`（协作职责规则、状态包的简报和目标检查表和缺口、记忆条目 id、收件箱分组）、`app/project_tools.py`（`project_checklist`、`project_goal` 的背景、`project_topic_start` 和 `project_topic_send` 的 `action`、`item`、`project_finish` 的 `checklist_gate`；`project_answer` 的依据不看它，总是核对）、`app/project_loop.py`（回执合批、收尾回执的检查表一句）；`core/butler.py`（`mcp_config` 只在负责人的任务里把它交给 MCP 服务）、`core/butler_tool_specs.py`（`lead_tools`：据此列 `project_checklist`、另几个工具多简报的可选参数；`project_answer` 总要 `evidence`） | 关：不派发协作轮、不起线程、不加规则行，负责人的规则、状态包、工具和收尾都和原来一样；`1`：Bridge、任务进程、后端三方都支持时派发（见[多参与者协作](architecture.md#多参与者协作开发中默认关)），负责人对照目标检查表跟进、写项目简报（见[负责人对照目标跟进](projects.md#负责人对照目标跟进)；替主人答题要写依据，开不开都一样） | 否 |
| `COVEY_COLLAB_QUEUE_SECONDS` | `core/collab_records.py`（`queue_seconds`：一次咨询等接收方空出来的时限） | 600 | 否 |
| `COVEY_COLLAB_WINDOW_SECONDS` | `core/collab_records.py`（`window_seconds`：一次协作的整个时限，到了没答复的咨询到期） | 1800 | 否 |
| `COVEY_PROJECT_BATCH_SECONDS` | `app/project_loop.py`（`batch_seconds`：`ProjectLoop.look` 的 `settling`） | 20：开了 `COVEY_COLLAB` 时，话题结束这类普通回执攒这么多秒再叫醒负责人（最新一条到了这么久没有新的才叫，最早一条最多等 4 倍），几个话题前后脚结束只叫醒一次；开工、主人在停住卡上的选择、扣住的问题、协作缺口马上叫醒。`0` 不攒；`COVEY_COLLAB` 关着时不读，和原来一样到下一次查看就叫醒 | 否 |
| `COVEY_KNOWLEDGE` | `core/knowledge.py`（`enabled`）、`core/butler.py`（`mcp_config` 交给管家的 MCP 服务）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此列知识工具） | 空：开；`0`、`off`、`false`、`no` 关：不开知识条目 | 否 |
| `COVEY_HISTORY_CMD` | `core/knowledge.py`（`history_cmd`）、`core/butler.py`（`mcp_config` 同上）、`core/butler_tool_specs.py`（`listed`：据此列 `history_search`） | 空：没有历史搜索；要绝对路径 | 否 |
| `COVEY_EXTENSIONS` | `core/extensions.py` | 空：读仓库根和数据根的 `extensions/enabled`；设成空串表示一个都不要。逗号分隔的 Python 包名，见[扩展](architecture.md#扩展) | 否 |
| `COVEY_EXTENSION_PATH` | `core/extensions.py` | 空：仓库根的 `extensions/`。`os.pathsep` 分隔的目录，扩展包在这些目录里找 | 否 |
| `COVEY_DEVICES` | `core/devices.py`（`enabled`） | 空：关；`1` 才读 `devices.json`，见[多设备](#多设备开发中默认关) | 否 |
| `COVEY_DEVICES_FILE` | `core/devices.py`（`config_path`） | 空：`<数据根>/devices.json` | 否 |
| `COVEY_NODE_LISTEN` | `core/nodes.py`（`enabled`、`listen_spec`）、`app/node_gateway.py`、`app/nodes.py` | 空：关；设了就在这里等节点连上来（`[::]:端口`、`IPv4:端口` 或端口，`$变量` 取环境，`core/listen.py` 的 `parse_bind`），起 `nodes-gateway`、`nodes-alerts`、`nodes-local` 三个后台线程，见[节点与机器状况](#节点与机器状况默认关) | 否 |
| `COVEY_NODE_ADDRESS` | `app/node_gateway.py`（`hub_address`） | 空：本机默认路由的 IPv6 地址加监听端口（`core/listen.py` 的 `link_host`；没有 IPv6 时用能解析的完整主机名）；设备连不到这个地址时写成 `主机:端口` | 否 |
| `COVEY_NODE_AUTOSTART` | `core/node_agent.py`（`install_autostart`，设备上读） | 空：登录钩子、systemd 用户服务、launchd 有哪个装哪个；`hook`：只装登录钩子（测试用临时 HOME 时） | 否 |
| `COVEY_BUTLER_RULES`（旧名 COVEY_MAIN_BUTLER、LARK_BRIDGE_MAIN_BUTLER） | `app/prompts.py`（`butler_on`）、`core/butler.py`（`mcp_config` 交给管家的 MCP 服务）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此换 `topic_start` 的工具说明） | 空：开，管家规则的前两条是管家规则（只快答和分派），`topic_start` 的工具说明同步；`0`、`off`、`false`、`no` 关：回到只读的自己做完、超过一两分钟交给话题的老说法 | 否 |
| `COVEY_DISPATCH_CARD`（旧名 COVEY_MAIN_DISPATCH_CARD、LARK_BRIDGE_MAIN_DISPATCH_CARD） | `app/dispatch_cards.py`（`enabled`；`app/butler_topics.py` 的 `dispatch_card` 和 `app/relay_send.py` 的 `dispatch_note` 据此调用，`threads()` 据此起 `dispatch_cards` 后台线程） | 空：开，管家转给已有话题后在提问的串里发一张活卡片，刷新到结论回带后缩成一行（开新话题不发）；`0`、`off`、`false`、`no` 关 | 否 |
| `COVEY_STEER_URGENT_SECONDS` | `app/steer_interrupt.py`（`urgent_seconds`，任务进程和 Bridge 都读） | `20`：急的补充（带“马上”“立刻”“停”“过期”这类词）送达这么多秒还没被读到，就停掉模型正在等的那条命令（推送、部署这类不停），见[架构 · 补充投递](architecture.md#补充投递)；`0` 关自动打断，回执卡上的“打断它，马上读”照常 | 否 |
| `COVEY_LEDGER_RECONCILE` | `app/owner_ledger.py`（`every`：`threads()` 据此起 `owner-ledger` 后台线程，两次对账之间的秒数） | 300：每 5 分钟对一次账，把没到聊天里的问题卡补发（最多 3 次，见[架构 · 等主人的账](architecture.md#等主人的账)）；`0` 关：不起线程、不写 `owner-ledger.json`，各处计数和原来一样 | 否 |
| `COVEY_SOURCE_WATCH` | `app/source_watch.py`（`enabled`：`note` 据此登记主人的消息，`threads()` 据此起 `source-watch` 后台线程） | 开：主人撤回或编辑了变成任务或补充的消息，任务随之停下或收到改后的内容（见[架构 · 撤回与编辑](architecture.md#撤回与编辑)）；`0` 关：不读回消息，和原来一样按收到时的原文做 | 否 |
| `COVEY_QUIET_HOURS` | `core/quiet_hours.py`（`window`、`quiet`：`app/tidy_proposals.py` 的 `release` 早上补发旧版本夜里扣下的提议卡，`app/node_alerts.py` 的 `deliver_all` 夜里不发不严重的告警） | 空：按 `COVEY_DIGEST_TIMES` 从最后一个时间到第一个时间（默认的 `09:30,21:30` 是 21:30 到 09:30；只有一个时间或汇总关了时没有夜里）；`HH:MM-HH:MM` 设定时段（Asia/Shanghai，含开始、不含结束）；`off` 或 `0`（或写错）：没有，每张卡马上发（以前的做法） | 否 |
| `COVEY_RELAY_MERGE_SECONDS` | `app/relay_send.py`（`merge_seconds`、`send_merged`；`app/butler_relay.py` 的 `relay` 只对“完成”的回带用它） | 20：“完成”的回带发出时聊天里还有别的话题的任务在跑、在排队，或已有回带在等，就等这么多秒，期间等着的合成一条「N 个话题做完了」；没有别的在做时照旧马上发。`0`：关，每条马上发（以前的做法）；0 到 45 以外和非整数按 20 | 否 |
| `COVEY_QUICK_ANSWER_SECONDS` | `app/submission.py`（`quick_seconds`、`quick_hold`：话题里的追问先不发任务卡，存进任务的 `card_after`；`app/cards.py` 的 `Cards.reveal` 到点或第一步时发卡，`app/worker.py` 答完时只发答案卡） | 10：话题里的追问（飞书话题串里或网页上说的）最多这么多秒不发任务卡，期间有了第一步（工具调用、进展说明、子任务）或问问题就发；这几秒内只答了话的只发答案卡，见[卡片 · 追问只发答案卡](cards.md#追问只发答案卡)。`0`：关，每轮先出任务卡（以前的做法）；0 到 120 以外和非整数按 10 | 否 |
| `COVEY_TOPIC_STEER` | `core/deliveries.py`（`topic_steer`：`app/submission.py` 的 `steers_on_arrival` 据此决定话题里的消息进不进在跑的任务，`app/detached.py` 据此在任务进程结束后、Bridge 启动时把没读到的补成新一轮） | 开：话题里任务在跑时发来的消息自动补充给那个任务（和点排队卡上的「现在补给 #N」一样，只送一次），没发出就照旧排成下一轮，确定模型没读到的自动排成新一轮，见[架构 · 补充投递](architecture.md#补充投递)；`0` 关：和原来一样排成下一轮，要补充时点排队卡上的按钮 | 否 |
| `COVEY_BUTLER_TRACKS`（旧名 COVEY_MAIN_TRACKS、LARK_BRIDGE_MAIN_TRACKS） | `core/butler_tracks.py`（`enabled`；`core/butler.py` 的 `is_butler_topic` 据此把槽 1 到 4 的话题也算管家；`app/butler_tracks.py` 的 `Tracks.enabled` 再加上管家允许、模式是管家 才分流） | 空：开[同时做几件事](butler.md#同时做几件事)：家族里有任务在做时，主人的下一句先按规则（`core/track_route.py`）和小模型分流，并进某件在做的事、停止或新开一件（槽 1 到 4）；`0`、`off`、`false`、`no` 关：消息的走法和没有这个功能时一样 | 否 |
| `COVEY_PROJECTS` | `core/projects.py`、`core/butler.py`、`core/placement.py`、`core/board_read.py`、`core/butler_tool_specs.py` | 空：开专项和负责人的运行（入口是管家直接开，和 `COVEY_PROJECT_UPGRADE` 的转成专项）；`0`、`off`、`false`、`no` 关：谓词全为假，放置表和看板和没有专项时一样，不建记录。见[架构 · 专项](architecture.md#专项) | 否 |
| `COVEY_BUTLER_HUNG_MINUTES` | `app/unstick.py`（`hung_seconds`，任务进程和 Bridge 都读） | `10`：管家或负责人的一轮这么多分钟没有任何动静（不在等命令、等回答、请求重试或排队等空位），而主人后来的话还在等它（没读到的补充、同一路排着的消息）时，停下这一轮（记失败、可检查并继续），后来的话接着跑；没动静满 3 分钟时先在每条等着的消息下说一句它在等哪一轮。`0` 关掉停下，只说那一句。0 到 1440 | 否 |
| `COVEY_BUTLER_TRACKS_MAX`（旧名 COVEY_MAIN_TRACKS_MAX、LARK_BRIDGE_MAIN_TRACKS_MAX） | `core/butler_tracks.py`（`limit`） | 3：同时在跑的件数上限，取值限制在 2 到 4，读不出来按 3 | 否 |
| `COVEY_BUTLER`（旧名 COVEY_MAIN_THREAD、LARK_BRIDGE_MAIN_THREAD） | `core/butler.py` | 允许（模式看 `main-thread.json`）；`0` 不允许 | 否 |
| `COVEY_TRACK_JUDGE` | `app/track_judge.py`（同时做几件事里，规则没能定的新消息交给小模型判断是并进哪件在做的事还是新开一件；`app/butler_tracks.py` 的 `Tracks.decide` 调用，`COVEY_BUTLER_TRACKS` 开着才会问） | `claude`（默认，名字沿用旧的）：问[辅助模型](#辅助模型)选的那个；`off` 不问，直接新开；`cmd:/absolute/path` 用自己的命令（标准输入一段 JSON，标准输出一行 JSON）。超时、出错、拿不准、脱敏话题都按新开一件 | 否 |
| `COVEY_TRACK_JUDGE_MS` | `app/track_judge.py`（`limit_ms`） | 6000：小模型硬上限（毫秒，200 到 10000），超过按新开一件（Claude Code 冷启动一问约 3.4 秒）；问的是 Codex 或 Antigravity 自己的命令行时不设默认 10000（它们一次要 6 到 12 秒），设了就按设的 | 否 |
| `COVEY_JUDGE_CODEX_MODEL` | `backends/codex/judge.py`（`choose`：辅助模型是 Codex 时用的模型） | 空：辅助模型的设置里没有模型时用 Codex 自己的默认模型和预算。写一个账号目录里有的模型 id：设置里没有模型时用它（目录里没有的不用），不带预算 | 否 |
| `COVEY_JUDGE_AGY_MODEL` | `backends/agy/judge.py`（`choose`：辅助模型是 Antigravity 时用的模型） | 空：辅助模型的设置里没有模型时用 agy 自己的默认模型和预算。写一个 `agy models` 列出的 id（带档位，如 `gemini-3.8-flash-low`）：设置里没有模型时用它 | 否 |
| `COVEY_AGY_EAGER_BYTES` | `backends/agy/home.py`（`eager_room`：管家和专项负责人跑在 Antigravity 上时，Bridge 的工具里预先加载的最多占多少字节，按描述加参数 schema 算；其余只在 agy 的提示里列名字，用到时模型先读说明再调）、`backends/agy/bridge.py`（`left_out` 的日志提到它） | 15000：交给话题的工具和其余常用的都预先加载，原则、知识条目、历史搜索这些排最后；`0`：全部只列名字。agy 的日志警告说丢了 Bridge 工具时调小 | 否 |
| `COVEY_AGY_ACCOUNTS` | `backends/agy/accounts.py`（`config_path`：每轮 agy、诊断 `service/doctor.py` 的 `agy_row`、升级检查 `service/plugins.py` 的 `agy_switching` 读多账号配置在哪） | 数据根的 `agy-accounts.json`；写了两个及以上的号才开 agy 多账号，见 [quota.md agy 多账号](quota.md#agy-多账号) | 否 |
| `AGY_ACCOUNTS_DIR` | `backends/agy/account_homes.py`（`accounts_dir`：终端命令 `agy-as` 把账号目录建在哪） | `~/.agy-accounts` | 否 |
| `AGY_CMD` | `backends/agy/account_homes.py`（`main`：`agy-as` 运行哪个 agy） | `agy`（PATH 上的） | 否 |
| `COVEY_TRACK_JUDGE_SLIM` | `app/track_judge.py`（`slim_enabled`，给小模型的 Claude Code 命令行多两个选项） | 空：开，小模型的 Claude Code 带 `--setting-sources user --disable-slash-commands`，不读项目设置和技能清单；`0`、`off`、`false`、`no` 关 | 否 |
| `COVEY_BUTLER_STATE`（旧名 COVEY_MAIN_STATE、LARK_BRIDGE_MAIN_STATE） | `core/butler_mcp.py`、（管家任务的 MCP 配置写进去，告诉工具服务 Bridge 的状态目录） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_BUTLER_JOB`（旧名 COVEY_MAIN_JOB、LARK_BRIDGE_MAIN_JOB） | `core/butler_mcp.py`（管家任务的任务键，Bridge 据此核对调用） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_PROJECT_LEAD` | `core/butler.py`（`mcp_config`：只在专项负责人的任务里把专项 id 写进 MCP 配置，要专项开着）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此只列负责人的工具） | 无；设了就列 `topic_read`、`topics_search`、`repos_list` 加 `project_*`，其余管家工具都不列 | 否 |
| `COVEY_PROJECT_AUTOWAKE` | `core/projects.py`（`autowake_enabled`，要专项开着）、`app/project_loop.py`（`ProjectLoop` 据此决定哪些回执唤醒负责人、要不要查停滞，关着时不叫醒谁的专项照常空闲收起）、`core/butler.py`（`mcp_config` 把它交给负责人的 MCP 服务）、`core/butler_tool_specs.py`（`lead_tools`：没开时 `project_topic_start` 的说明写话题结束不会叫醒负责人） | 空：开，话题一轮结束、主人在专项的话题里说话、停滞提醒也唤醒负责人，并开停滞检查；`0`、`off`、`false`、`no` 关：只有专项的第一条回执唤醒负责人。见[架构 · 专项](architecture.md#专项) | 否 |
| `COVEY_PROJECT_MEMORY` | `core/projects.py`（`memory_enabled`，要专项开着）、`app/project_context.py`（专项的话题开工带上记忆和资料库索引、主人答专项的问题卡记一条决定、主人发给负责人的文件和各话题的产物、交付进资料库）、`app/project_lead.py`（`library_index`：状态包的资料库）、`app/runs.py`（`bridge_rules`、`_with_rules` 取专项块） | 空：开，专项的话题开工带上专项记忆和资料库索引，主人的回答记成决定，主人发的文件和话题的产物进资料库；`0`、`off`、`false`、`no` 关：专项的话题开工不多带东西、不自动记、不收资料库。见[专项记忆与资料库](projects.md#专项记忆与资料库) | 否 |
| `COVEY_MEMORY` | `core/memory.py`（`enabled`）；用它的：`app/memory_turn.py`（每轮那一块“记忆”）、`app/memory_collect.py`（话题一轮结束后核对 `memory`、`memory_void`）、`app/runs.py`、`app/knowledge_turn.py`、`app/project_context.py`、`app/memory_tidy.py` 和 `app/digest.py`（整理记忆，每天一次） | 空：开，原则、知识条目、专项记忆、话题记忆拼成一块“记忆”，主人说的长期要求一轮结束后记下，每天按规则整理一次资料和决定；`0`、`off`、`false`、`no` 关：规则、续接提醒和答案卡与没有这个功能时逐字相同。见[记忆](#记忆) | 否 |
| `COVEY_MEMORY_LEARN` | `core/memory.py`（`learning`，要 `COVEY_MEMORY` 开着）；用它的：`app/memory_turn.py`（带上候选、查记忆的命令；辅助模型不是“不用模型”时不带“以后/别再”那句）、`app/memory_learn.py`（每轮之后读主人的话、记用到的编号）、`app/memory_panel.py` | 不设（或空、`1`）：开，记忆自己长，见[记忆](#记忆)；`0`、`off`、`false`、`no`：关，记忆和没有它时一样 | 否 |
| `COVEY_WORKSPACE_GISTS` | `app/workspace_gists.py`（`count`；`app/runs.py` 的 `Runs.bridge_rules` 只在新会话的规则里放这一块）、`app/about_panel.py`（设置卡「诊断」页下半“能做什么”的那一行） | 空：3；`1` 到 `10`：同一聊天、同一工作区新开的话题，第一轮的规则带上这个工作区最近这么多个别的话题的[话题要点](projects.md#整理专项)（要点要整理专项开着才会写）；只在主人自己的一轮、普通话题里带，脱敏话题的要点只给脱敏话题；`0` 或别的值：关，规则和没有这个功能时逐字相同。见[新话题带上最近话题的要点](#新话题带上最近话题的要点) | 否 |
| `COVEY_PROJECT_TRIAGE` | `core/triage.py`（`enabled`，要专项开着）、`core/butler.py`（`mcp_config` 把它交给负责人的 MCP 服务）、`core/butler_tool_specs.py`（`lead_tools`）、`app/triage.py`、`app/project_loop.py`（`ProjectLoop.tick` 扫扣住的问题） | 空：开，选择题先交给负责人，3 分钟内它能替主人答的就答，其余才发问题卡；`0`、`off`、`false`、`no` 关：专项的话题的问题直接发给主人。见[专项的问题分流](projects.md#专项的问题分流) | 否 |
| `COVEY_PROJECT_BUDGET` | `core/projects.py`（`token_limit`）、`app/project_loop.py`（`hold`） | 空：没有上限，专项不会因为 token 停住；正整数：一个专项的 token 合计（负责人和各话题，缓存算在输入里）到了就停住（`state_reason` 是 `tokens`） | 否 |
| `COVEY_PROJECT_TOKENS` | `core/projects.py`（`token_limit`） | `COVEY_PROJECT_BUDGET` 的旧名，它没设时才读。名字里有 TOKEN，服务清单把它当成密钥不保存，重新 `launch` 带不进去：要设上限用 `COVEY_PROJECT_BUDGET` | 否 |
| `COVEY_PROJECT_ASK` | `core/projects.py`（`ask_enabled`，要专项开着）、`app/project_loop.py`（`ProjectLoop.pause`）、`app/project_cards.py`（`ProjectCards.ask`、`ask_choice`） | 空：开，护栏停住的专项再发一张卡问 继续 / 收尾 / 停；`0`、`off`、`false`、`no` 关：只在专项卡上写“停住 · 原因” | 否 |
| `COVEY_PROJECT_STALL_SECONDS` | `core/projects.py`（`stall_seconds`）、`app/project_loop.py`（`ProjectLoop.stall`） | 1800：专项有负责人能推动的事却这么多秒没进展，提醒负责人一次，再过同样久就停住 | 否 |
| `COVEY_PROJECT_IDLE_DAYS` | `core/projects.py`（`idle_days`）、`app/project_loop.py`（`ProjectLoop.rest`）、`app/project_cards.py`（`ProjectCards.fold`） | 7：专项没有要做的事、主人也这么多天没提它，就收起（专项卡缩成一行）；主人对它说话或管家交给它新的事时自动展开。可以写小数 | 否 |
| `COVEY_PROGRESS_STUCK_MINUTES` | `core/progress.py`（`stuck_seconds`）、`core/longtask.py`（`TaskMonitor`） | 30：在跑的一轮这么多分钟没有任何动静就算“可能卡住了”：任务卡状态行、看板、汇总里这么写，状态行下出「看看」按钮。1 到 1440 | 否 |
| `COVEY_WEB` | `core/web_sync.py`（`enabled`）、`core/web_backup.py`（`snapshot`）、`app/web_sync.py`（`WebSync.threads`） | 空：关。`1`：开网页工作台同步，`web-push`、`web-pass`、`web-inbox`、`web-touch`、`web-reread` 五条线程（网页操作另在各自车道的短线程里做）；要同时设 `COVEY_WEB_CONFIG` | 否 |
| `COVEY_WEB_CONFIG` | `core/web_sync.py`（`load_config`）、`core/web_backup.py`（`snapshot`）、`app/web_push_client.py`（`config_stamp`：文件变了才重读） | 空。指向一个 JSON：`base_url`（`https://`，或本机回环的 `http://127.0.0.1:端口`、`http://localhost`、`http://[::1]` 供自托管用，其他 `http://` 拒绝）、`key_file`（只从这个文件读密钥）、`push_seconds`（默认 1）、`poll_seconds`（默认 0.5）、`direct`（可选，`true` 时连网页不走环境变量里的 https 代理）；文件缺了或写错只记一行失败、退避重试 | 否 |
| `COVEY_WEB_SELFHOST` | `service/web_selfhost.py`（`enabled`、`spec`、`wanted`）、`service/doctor.py`（体检）、`core/web_backup.py`（`snapshot`）、`service/manager.py`（`Manager.ensure_selfhost`） | 空：关，Manager 不起线程、不起进程。`1`：Manager 拉起自托管网页进程 `node <COVEY_WEB_SELFHOST_DIR>/server/selfhost/main.js`（`node` 按服务清单的 `PATH` 找），只监听 `127.0.0.1`，挂了按服务的退避重启，Manager 退出时整组回收（Manager 被强杀时网页进程发现父进程不在了自己退出）；要同时设 `COVEY_WEB_SELFHOST_DIR`，Bridge 一侧另要 `COVEY_WEB=1` 和 `COVEY_WEB_CONFIG`（`python3 -B -m covey.service.web_selfhost` 一次备好） | 否 |
| `COVEY_WEB_SELFHOST_DIR` | `service/web_selfhost.py`（`spec`） | 空。自托管网页的构建产物目录（`web/deploy/selfhost/build.sh` 打印的 `dist-selfhost`，`node_modules` 在它旁边），绝对路径，里面要有 `server/selfhost/main.js`；没设或不是绝对路径，开关开着也不起进程 | 否 |
| `COVEY_WEB_SELFHOST_PORT` | `service/web_selfhost.py`（`port_of`） | 18650：自托管网页进程监听的本机端口（1 到 65535，其他值按默认）；`web.json` 的 `base_url` 要和它一致 | 否 |
| `COVEY_WEB_SELFHOST_PUBLIC` | `service/web_selfhost.py`（`public_problem`、`public_names`、`spec`、`check`） | 空：不开公网。一个或几个地址（IPv4、IPv6 不带方括号、域名，逗号分隔；第一个写进 `page_url`）：网页进程在回环 HTTP 之外另开 HTTPS 监听（`COVEY_WEB_SELFHOST_PUBLIC_PORT`、`COVEY_WEB_SELFHOST_BIND`），证书是自签的或自己给的（`COVEY_WEB_SELFHOST_TLS_CERT`、`COVEY_WEB_SELFHOST_TLS_KEY`）；公网监听上 `/openapi` 一律拒绝，会话只在这个监听上带 `Secure`。有一个地址不合法就整项当没开，体检说原因（见[网页工作台 · 公网直连](web.md#公网直连自带-https)） | 否 |
| `COVEY_WEB_SELFHOST_PUBLIC_PORT` | `service/web_selfhost.py`（`public_port_of`） | 18443：公网 HTTPS 端口（1 到 65535，其他值按默认），要在防火墙和安全组放行 | 否 |
| `COVEY_WEB_SELFHOST_BIND` | `service/web_selfhost.py`（`bind_of`） | `0.0.0.0`：公网 HTTPS 监听的地址（IP；只在 IPv6 上听写 `::`；写不成 IP 按默认） | 否 |
| `COVEY_WEB_SELFHOST_TLS_CERT`、`COVEY_WEB_SELFHOST_TLS_KEY` | `service/web_selfhost.py`（`own_tls`、`spec`） | 空：用网页进程生成的自签证书（`<数据根>/web/tls/`）。两个都写绝对路径（PEM 证书链和私钥）才用自己的证书，只给一个按没给；私钥权限要 600，体检只看权限、不读内容 | 否 |
| `COVEY_PROJECT_TIDY` | `core/project_proposals.py`、`app/project_tidy.py`、`app/runs.py`、`core/butler.py`、`core/butler_tool_specs.py`、`app/web_sync.py`、`app/butler_panel.py` | 空或 `suggest`：只提议（能归进已有专项的直接归，新建要主人点）；`auto`：新建也直接做；`off`、`0` 或别的值，以及专项关着：关。见[整理专项](#整理专项) | 否 |
| `COVEY_PROJECT_TIDY_MODEL` | `core/project_proposals.py`（`model`）、`app/project_sorter.py`（`ask`） | 空：辅助模型是 Claude Code、设置里又没有模型时，后台活（分流、整理专项、补话题要点、记忆自己长）用 Claude Code 自己的默认模型；写了就用这个模型别名或 id（不带预算）；写错按空 | 否 |
| `COVEY_PROJECT_TIDY_DAYS` | `core/project_proposals.py`（`number`） | 7：整理看最近几天有过一轮的话题（专项里的话题不论多久都看） | 否 |
| `COVEY_PROJECT_TIDY_MIN_TOPICS` | `core/project_proposals.py`（`number`）、`app/tidy_run.py`（`start`） | 5：聊天里的话题少于这个数时不整理（定时、手动都不） | 否 |
| `COVEY_PROJECT_TIDY_JUDGE_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`judge`） | 1200：一次判断调用的硬上限，秒；超了这次判断作废，什么都不改 | 否 |
| `COVEY_PROJECT_TIDY_READ_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`backfill`） | 300：补一个老话题的要点那次调用的上限，秒；超了只跳过这个话题 | 否 |
| `COVEY_PROJECT_UPGRADE` | `app/projects.py`（`mode`：升级、`Projects.consider`；要 `COVEY_PROJECTS=1` 和 `COVEY_DISPATCH_CARD=1`） | 空或 `butler`：管家转话时带了 `project`，派发卡就静默转成专项（`word` 是旧值，同 `butler`）；`auto`：再加该话题来回到第 3 轮（管家开这个话题算第 1 轮）；`off` 或别的值：不转成专项。同时最多 50 个活专项，满了先收起最久没动静的一个（[专项可长可短](projects.md#专项可长可短)） | 否 |
| `COVEY_PINNED_TOPICS` | `core/topic_seen.py`（看板 `app/pins.py` 和已读消费者 `app/topic_seen.py` 都经它） | 开；`0`、`off`、`false`、`no` 关（还要 `COVEY_PINNED_OVERVIEW` 开着） | 否 |
| `COVEY_PROXY_PROFILE` | `app/proxy.py` | 空：代答不可用 | 否 |
| `COVEY_PROXY_GROUP_NAME` | `app/proxy.py` | `代答`：第一次开启代答时新建的群叫这个名字；已建的群按群 ID 找，名字照旧 | 否 |
| `COVEY_PROXY_REPLY_CHECK` | `core/proxy.py`（`reply_check_mode`；`app/proxy.py` 的 `check_reply` 经它） | `count`：代答回复的发前检查只记命中、照常发；`hold`：命中的不发，留给你看；`off` 或 `0`：不查 | 否 |
| `COVEY_OWNER_SEND` | `app/owner_send.py`（`enabled`） | 开（有代答发送器时）：话题以主人身份发消息走代发草稿卡；`0` 关，见[代发](proxy.md#代发) | 否 |
| `COVEY_APP_CHECK` | `app/app_setup.py` | 开：检查飞书应用、缺了发卡；`0` 关 | 否 |
| `COVEY_AVATARS` | `feishu/avatars.py`（`image_key`） | 开：私聊卡片的头像行放管家和负责人的头像（第一次用时上传到飞书，`image_key` 记在状态目录的 `avatars.json`）；`0` 关：不上传、卡上只有名字（测试套件默认关）。话题的头像是它的 T 号（卡片里画的，不是图片），不受这个开关影响 | 否 |
| `COVEY_WELCOME` | `app/welcome.py`（`enabled`：`settle` 据此写 `welcome.json`，`threads()` 据此起 `welcome` 后台线程） | 开：新部署第一次启动后在主人私聊发一张欢迎卡，每个部署只发一次（之前已有任务、话题或私聊记录的部署不发）；`0` 关：不写记录、不起线程、不发，和原来一样 | 否 |
| `COVEY_AUTO_RETRY_LONG` | `app/auto_retry.py`（`long_ladder`：`plan` 排自动重试时据此选等待时间表、是否有临时故障一类、是否读恢复时间、是否加随机、做了实事是否清零；`applies`、`status_line` 也看它） | 开（默认，只有 `0` 关）：限流 60 秒到 1 小时共六次、临时故障 30 秒到 5 分钟共三次、读得到恢复时间就等到那时、每次另加 0–30 秒随机；`0`：和原来一样，限流 60、180 秒两次，临时故障不重试，没有随机 | 否 |
| `COVEY_MAX_WORKERS` | `core/scheduler.py`（`worker_limit`：Bridge 起调度器时读一次；`app/submission.py` 排队时据它说“并发已满（最多 N 个任务）”） | 32：最多同时跑这么多个任务（每个话题一次一个），其中 2 个只留给管家和专项负责人那一轮，普通话题最多占其余的；写大于 0 的整数改它（最多 512），机器小就调低；改了要重新注册服务才生效 | 否 |
| `COVEY_LIMIT_NOTICE` | `app/limit_notice.py`（`enabled`：`tick` 在 Bridge 的 outbox 循环里据此决定看不看记录、发不发提示） | 开（默认，只有 `0` 关）：私聊里几个话题同一原因停下、各自排好自动重试时发一条合并提示；`0`：不读记录、不发，和原来一样 | 否 |
| `COVEY_QUOTA` | `core/quota_sources.py`（`enabled`：Bridge 的额度线程 `app/quota_watch.py`、失败说明 `app/quota_failure.py` 据此决定读不读额度来源） | 开（默认，`0`、`off` 才关）：读扩展给的额度来源和数据根 `quota-sources.json` 里的命令和网址，写 agy 多账号的快照，失败卡按快照说清几点恢复；`0`：都不读、不问，和原来一样。见 [quota.md](quota.md) | 否 |
| `COVEY_STRANGER_REPLY` | `app/strangers.py`（`enabled`） | 开：别人私聊机器人时回一句纯文本“这是某某的个人助手，只处理某某的消息”，每人每天最多一次；`0` 关：和原来一样不回，只记一行 `ignored message … reason=not_owner` | 否 |
| `COVEY_INVITE_URL` | `app/strangers.py`（`invite_url`） | 扩展的 `invite_url`，都没有时空：那句话不带链接。设了 http(s) 地址时末尾加“想要一个自己的：<地址>”，一般指向安装说明 | 否 |
| `COVEY_DOWN_NOTICE` | `service/down_notice.py`（`enabled`：Manager 每轮从各服务清单的环境里读，`service/manager.py` 的 `tell`） | 开：Bridge 连续 3 次没能启动、同一个启动错误持续 2 分钟，或运行中 10 分钟收不到飞书消息时，Manager 用这个服务的 lark-cli profile 以机器人身份给主人发一条文字消息，恢复（正常运行 5 分钟）后再发一条；同一个服务的“停了”消息至少隔 1 小时；`0` 关：Manager 不读、不写、不发，和原来一样。见[运维 · 机器人停了会告诉主人](operations.md#机器人停了会告诉主人) | 否 |
| `COVEY_PLAYWRIGHT_PYTHON` | `feishu/app_config.py`（`playwright_python`：自动补齐用的解释器） | `<数据根>/proxy-web/venv/bin/python` | 否 |
| `COVEY_CONSOLE_URL` | `app/app_setup.py`（交给 `feishu/app_console.py` 的 `--console`） | `https://open.feishu.cn`（登录后跟着跳到租户的域名） | 否 |
| `COVEY_LOCK_ROOT` | `core/file_lock.py`、`service/install.py`、`service/manager.py` | 不设：不用本机运行锁（见[本机运行锁](runtime-locks.md)）；管理命令在终端没设时用服务清单里的 | 否 |
| `COVEY_MANAGED` | `app/unified.py` | Manager 启动 Bridge 时设成 `1`，不要手设 | 否 |
| `COVEY_REQUIRED_MOUNT` | `service/manager.py` | 开机守护设置，不要手设 | 否 |
| `COVEY_RUNNER_PYTHONPATH` | `app/runner.py` | Bridge 交给任务进程，不要手设 | 否 |
| `COVEY_TASK_DIR` | `app/leftovers.py`、`core/device_link.py`、`core/topic_repos.py`（`covey repo use` 写本任务的 `repos.json`，Codex 的可写根） | 任务进程给本轮命令设置，不要手设 | 否 |
| `COVEY_STATE` | `core/repomap.py`（`bin/covey_repo.py` 找仓库地图和话题记录） | Bridge 给每个本机的 Harness 进程设成状态目录（`core/home.py` 的 `env`），不要手设；空时 `covey repo list` 说地图是空的，见[主目录与仓库地图](#主目录与仓库地图) | 否 |
| `COVEY_HOME` | `core/repomap.py`（`bin/covey_repo.py`：相对路径从这里算）、`backends/claude/tool_guard.py`（只读首轮的钩子：命令里的 `$COVEY_HOME` 按它解开） | Bridge 给每个本机的 Harness 进程设成主目录（服务的 `--workdir`），不要手设，见[主目录与仓库地图](#主目录与仓库地图) | 否 |
| `COVEY_TOPIC` | `core/repomap.py`（`bin/covey_repo.py`） | Bridge 给每个本机的 Harness 进程设成这一轮的话题 id，`covey repo` 据此找本话题的 worktree 目录和用到的仓库，不要手设 | 否 |
| `CODEX_HOME` | `core/paths.py`（`codex_home`，Codex 的会话目录从它取）、`core/model_settings.py`、`backends/codex/consult.py`（协作咨询轮的私有 home 从它链接）、`core/same_account.py`（读它的 config.toml / settings.json，看自己的登录和哪个模型网关是同一账号） | `~/.codex` | 否 |
| `COVEY_SYSTEM_RULES` | `app/rules_sent.py`（`enabled`：`app/runs.py` 据此把桥接规则放到 Harness 的系统级，`app/butler.py`、`app/project_lead.py` 的管家、负责人规则跟着走） | 开：声明了 `system_instructions` 的 Harness（现在四个核心 Harness 都是）规则放系统级，见[续接轮的规则](architecture.md#续接轮的规则)；`0` 关：规则放用户消息，和原来一样 | 否 |
| `COVEY_CLAUDE_HOOKS` | `backends/claude/hooks.py`（`enabled`：`TurnHooks.registration` 据此在每轮 Claude Code 的 initialize 里注册 SDK hook） | 开：子 Agent（Explore 除外）开工时收到守则（`SubagentStart`），子 Agent 调 `mcp__bridge__*` 被拒（`PreToolUse`），管家这一轮自己调满 6 次非 Bridge 工具时提醒一次交给话题（`PostToolUse`，主人说了“你直接做”的那一轮没有）；`0` 关：不注册，每轮和原来一样 | 否 |
| `CLAUDE_CONFIG_DIR` | `backends/claude/sessions.py`、`backends/claude/redaction.py`、`core/maintenance_rules.py`、`core/claude_retention.py`（只读它的 `settings.json`，诊断页和 `./install` 提示会话保留几天）、`core/same_account.py`（读它的 config.toml / settings.json，看自己的登录和哪个模型网关是同一账号） | `~/.claude` | 否 |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`、`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING` | `backends/claude/bridge.py`（Bridge 的环境里设了就用它的值，否则给 claude 子进程设成 `1`） | `1` | 否 |
| `DSH_HOME` | `backends/dsh/bridge.py` | `~/.dsh` | 否 |
| `LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` | `core/ignored_env.py`（不再设默认值，只为拦截而读：Bridge 启动日志、`service/doctor.py`、`scripts/deploy_bridge.sh`、`scripts/bootstrap_harness_bridge.sh`） | 不设 | 是：已不生效；服务命令行写 `--sandbox`，`harnesses.json` 写 `--no-skip-permissions` |

Bridge 的参数：默认 Harness（Codex）和 Bridge 自己的写在服务命令行，其余 Harness 的写进 `harnesses.json`（见 [Harness](harnesses.md)）。默认值以 `--help` 为准。

| 参数 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `--harness` | `bin/covey_bridge.py` | `unified`（唯一的选项） | 否 |
| `--state-dir`、`--workdir`、`--profile` | `app/harnesses.py`（每个 Harness；非默认的由 Bridge 填） | 状态目录、当前目录、`COVEY_PROFILE` | 否 |
| `--harness-config`、`--owner`、`--app-id`、`--pairing-file` | `app/unified.py`（`--harness-config` 另有 `service/plugins.py` 的 `deploy-check` 读，部署前扩展检查用） | 空 | 否 |
| `--run-dir`、`--await-handoff`、`--compact-only` | `app/runner.py`（任务进程，Bridge 填；`--compact-only` 是管家空闲时的压缩进程，`app/idle_compaction.py`） | 无 | 否 |
| `--model`、`--reasoning-effort`、`--sandbox`、`--codex-cmd`、`--codex-timeout` | `backends/codex/bridge.py` | `gpt-6-astra`、`max`、`danger-full-access`、`codex`、0（不限） | 否 |
| `--codex-transport` | `backends/codex/bridge.py` | `app-server`（唯一的选项） | 只为旧命令行能解析 |
| `--codex-magpie-url` | `backends/codex/bridge.py` | 值被忽略；设了非空值启动时记一行 `no longer used` | 只为旧服务命令行能解析（`compat_report: codex-magpie-url`） |
| `--model`、`--effort`、`--claude-cmd`、`--claude-timeout`、`--no-skip-permissions`、`--claude-login-shell`、`--claude-auto-compact-window`、`--claude-compact-between-turns` | `backends/claude/bridge.py` | `claude-fable-5`、`xhigh`、`claude`、0、关、关、空、空 | 否 |
| `--claude-profiles` | `backends/claude/bridge.py` | 已下线，值被忽略，设了启动记一行；见[已下线的功能与旧配置](legacy.md) | 否 |
| `--claude-transport`、`--traex-ppe-env`、`--traex-site` | `backends/claude/bridge.py` | 值被忽略 | 只为旧 `harnesses.json` 能解析（`compat_report: removed-options`） |
| `--model`、`--reasoning-effort`、`--dsh-cmd`、`--dsh-timeout` | `backends/dsh/bridge.py` | 见 `--help` | 否 |
| `--model`、`--reasoning-effort`、`--agy-cmd`、`--agy-timeout`、`--no-skip-permissions` | `backends/agy/bridge.py` | 空（agy 自己的默认模型）、空、`agy`、0、关 | 否 |

`scripts/deploy_bridge.sh` 自己的参数不在这张表里（表只列 Bridge 读的）：`--ext-repo`、`--ext-commit`、`--allow-core-mismatch` 让它把核心检出和扩展检出拼成一个 release，不给时只部署 `--repo` 一个检出，`--ext-commit` 默认 `HEAD`；`--allow-rollback` 让它部署不包含现网提交的版本（有意回滚），`--allow-extension-checks` 放行没过的扩展自检（`pre.extension_checks`），另一个部署正在跑时它退出码 3、不部署；用法见[部署新版本](deploy.md#部署新版本)。

`harnesses.json` 里除了 `args` 还读每个 Harness 的 `models_first`（`app/harnesses.py` 的 `models_first`，`app/unified.py` 读入，`app/launch_models.py` 的 `first_models` 排序；不写时顺序不变），见 [`--harness-config` 文件](harnesses.md#--harness-config-文件)。扩展的 Harness 另有的参数和档位文件见扩展自己的 README。

其他程序的参数，用法见各自的 `--help` 和对应文档：

| 参数 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `--root`、`--state-dir`、`--workdir`、`--profile`、`--interval`、`--health-timeout`、`--log`、`--dry-run`、`--preset` | `service/manager.py`（`bin/covey_service.py`；`--dry-run` 是 `set`、`unset` 的：只打印会改什么，见[改开关三步](#改开关三步)；`--preset showcase` 是 `set` 的：打开[演示套餐](#演示套餐)里清单没设或设成关的开关） | 见 `--help` | 否 |
| `--root`、`--mode`、`--activate`、`--label`、`--status`、`--uninstall`、`--no-autostart` | `service/install.py` | 见 `--help`（`--mode` 有 `auto`、`systemd`、`launchd`、`cron`、`manager`，扩展可加；`auto` 在没有 systemd/launchd 时给 crontab 加一行 `@reboot`，`--no-autostart` 跳过；`--label` 是 launchd 标签，默认 `com.covey.manager`；公司托管模式的 `--persistent-virtio-pfs` 见扩展 README） | 否 |
| `--root`、`--runtime-only`、`--skip-events`、`--probe-harnesses`、`--json`、`--summary` | `service/doctor.py`（`bin/covey_doctor.py`；`--json` 打完整的 JSON 报告，`--summary` 打中文摘要和每项的下一步，见[排障 · 健康诊断](troubleshooting.md#健康诊断)） | 都不给时：输出是终端就打中文摘要，管道和脚本照旧拿到 JSON | 否 |
| `--data-root`、`--dir`、`--port`、`--page-url`、`--public`、`--public-port`、`--bind`、`--tls-cert`、`--tls-key`、`--restore-db` | `service/web_selfhost.py`（`python3 -B -m covey.service.web_selfhost`，不另设 `bin/` 入口） | 准备自托管网页：写开放接口密钥文件（600，已有的不覆盖）和 `web.json`，打印要在服务清单里设的开关。`--data-root` 默认数据根，`--dir` 自托管构建产物目录（`dist-selfhost` 的绝对路径），`--port` 默认 18650，`--page-url` 写进 `web.json` 的 `page_url`（飞书卡片「打开网页」的地址，默认 `http://127.0.0.1:<端口>/`，已有的不给参数时保留）；`--public ADDR[,ADDR]` 开公网 HTTPS 直连（`auto` 在终端里用本机的公网地址问一次确认），`--public-port`（默认 18443）、`--bind`（默认 `0.0.0.0`）、`--tls-cert` 和 `--tls-key`（自己的证书，绝对路径、一起给），并打印要放行的端口、网页地址、证书指纹、第一次信任的步骤和飞书确认登录三步；`--restore-db PATH` 只做恢复：从每日备份包或 `pglite.tar.gz` 恢复网页数据库 | 否 |
| `--state-dir`、`--app-id` | `service/pairing.py` | 见 `--help` | 否 |
| `--config`、`--data-dir`、`--dry-run`、`--duration`、`--kill`、`--parent-pid`、`--target`、`--as-mib`、`--alert-levels-mib`、`--rg-guard`、`--search-kill-mib` | `service/memguard.py` | 见[内存看门狗](operations.md#内存看门狗) | 否 |
| `--state-dir`、`--apply`、`--dry-run`、`--destination`、`--restore`、`--verify` | `core/maintenance.py` | 见 [备份、归档与维护](operations.md#备份归档与维护) | 否 |
| `--legacy-root`、`--new` | `core/paths.py` | 见 `--help` | 旧 `.local-state` 退役后删 |
| `--state-dir` | `core/workspaces.py` | 见 `--help` | 否 |
| `--json` | `service/plugins.py`（`bin/covey_plugins.py`） | `list` 的 JSON 输出；子命令 `list`、`enable`、`disable`、`doctor` 见[扩展](architecture.md#扩展)的「装和卸」 | 否 |
| `--state`、`--topic`、`--workspace`、`--owner`、`--redacted`、`--limit`、`--json` | `core/memory_search.py`（`bin/covey_memory.py search`，记忆自己长的查记忆） | 状态目录（必填）、这一轮的话题、按哪个工作区查（默认话题自己的）、带上全局的、带上来自脱敏话题的、最多几条（8）、JSON 输出 | 否 |
| `--json`、`--write`、`--legacy` | `core/repomap.py`（`bin/covey_repo.py`：子命令 `list`、`use <仓库>`、`status`、`prune-plan`） | `--json` JSON 输出（放在子命令前后都行）；`--write` 是 `use` 的：要改这个仓库（建或复用本话题的 worktree）；`--legacy` 是 `prune-plan` 的：另看一个目录下一层的旧 worktree，见[主目录与仓库地图](#主目录与仓库地图) | 否 |
| `--state-dir`、`--json` | `app/owner_ledger.py`（`bin/covey_ledger.py`：等主人的账，只读） | 状态目录（必填）；`--json` JSON 输出。见[架构 · 等主人的账](architecture.md#等主人的账) | 否 |
| `--state`、`--limit`、`--json` | `core/inbound_ledger.py`（`bin/covey_inbound.py`，查主人消息的去向） | 状态目录（必填）、`recent` 最多列几条（10）、JSON 输出；子命令 `where <消息 id>`、`recent` | 否 |
| `--state-dir`、`--no-probe`、`--json` | `service/device_check.py`（`bin/covey_devices.py`） | 见 `--help`；见[多设备](#多设备开发中默认关)的「自检」 | 否 |
| `--root`、`--extra-root` | `backends/codex/workspace_reader.py` | 见 `--help` | 否 |
| `--allow-file`、`--allow-dir` | `backends/codex/workspace_reader.py`（给了才多一个 `workspace_write_file`）、`core/look_write.py`（`bin/covey_look_write.py`，Antigravity 只读首轮私有主目录里的服务）、`backends/claude/tool_guard.py`（`--mode look`）：管家判为只读的第一轮能写的检查点文件和产物目录，Bridge 自己传，见 [Harness · 执行权限](harnesses.md#执行权限) | 空：哪里都不能写 | 否 |
| `--mode`、`--notes`、`--home` | `backends/claude/tool_guard.py`（`bin/covey_tool_guard.py`：Claude Code 的 PreToolUse 钩子，Bridge 自己写进那一轮的 `--settings`。`--mode proxy` 是代答话题的，`--notes` 是联系人的笔记目录；`--mode home-search` 是从主目录启动的一轮的，`--home` 是主目录，只拦目标是主目录根（或它的上级）又没限深度的递归搜索，`cd <仓库> && rg`、具体子目录、`find -maxdepth` 照常跑，自己出错时放行；`--mode shim <命令名> <参数…>` 是 `<数据根>/bin` 里的搜索垫片问的（主目录取 `$COVEY_HOME`，规则同上，拒绝退出码 3，由垫片转成 2，出错一律放行）；`--mode look` 是管家判为只读的第一轮的，只放行写 `--allow-file`、`--allow-dir`，自己出错时拒绝） | `proxy`；`--notes` 空：编辑工具哪里都不能写 | 否 |
| `--home`、`--start`、`--idle`、`--max`、`--dy` | `feishu/web_client.py`（`bin/covey_feishu_web.py`） | 见 [浏览器服务](testing.md#浏览器服务) | 否 |
| `--app-id`、`--home`、`--plan`、`--login-from`、`--console`、`--login-seconds` | `feishu/app_console.py`（`bin/covey_app_console.py`） | 见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 否 |
| `--profile`、`--app-id`、`--review`、`--json`、`--python`、`--login-from`、`--work` | `service/app_check.py`（`bin/covey_app_setup.py`） | 见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 否 |
| `--reconfigure`、`--skills`、`--non-interactive`、`--data-home`、`--workdir`、`--keep-login`、`--browser`、`--no-browser`、`--no-autostart`、`--profile`、`--app-name`、`--no-start`、`--showcase`、`--web`、`--web-public`、`--browser-login-dir`（验收用，隐藏，见[运维](operations.md#服务托管)） | `service/installer.py`（`./install`、`bin/covey_install.py`） | 见[安装](install.md#选项)；`--showcase` 写[演示套餐](#演示套餐) | 否 |
| `--home`、`--console`、`--shots` | `feishu/install_browser.py`（`bin/covey_install_browser.py`，`./install` 起的浏览器会话） | 见[安装](install.md#它做了什么) | 否 |

## 已下架项与旧配置

已下架的 Harness 和供应商、不再读取的变量和参数、旧菜单键、旧卡片和旧安装方式现在会怎样，见[已下线的功能与旧配置](legacy.md)。只升级老部署时才用得到。

这个版本起不再读的开关，清单里留着也不报错：`COVEY_TURN_EXPORT`（任务卡「下载请求与结果」整块删了）、`COVEY_HARNESS_PROBE`（可用性检查不再做真实模型调用）、`COVEY_JUDGE_GATEWAY`（后台的一问一答不再走模型网关，改问[辅助模型](#辅助模型)选的 Harness 自己的命令行）、`COVEY_BOARD`（菜单「看板」每个部署都有）、`COVEY_PROJECT_STYLE_KEEP`（负责人改过的汇报风格总会记下）。各自现在的行为见[已下线的功能](legacy.md#已下线的功能)。

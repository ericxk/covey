# 配置

本文写给部署机器人的人：机器人从哪几处读配置、每个可选功能怎么打开、全部环境变量和参数。旧配置还能不能用见[已下线的功能与旧配置](legacy.md)，服务怎么注册、部署和维护见[运维指南](operations.md)，排障见[排障](troubleshooting.md)，各 Harness 的参数见 [Harness 说明](harnesses.md)。

## 开关速查

可选功能都靠服务清单里的环境变量开关。下面三张表回答“我这个机器人开了什么、还能开什么”；每个开关的全部取值和细节在后文各节和[一览](#环境变量与选项一览)里。

### 默认已开

`./install` 新装的服务就有这些（前三行是安装器写进服务清单的 `INSTALL_SWITCHES`，代码本身的默认是关；其余是代码默认开）。

| 功能 | 开关 | 怎么确认开着 | 怎么关 |
| --- | --- | --- | --- |
| [管家](#管家) | `COVEY_BUTLER`（不设即允许） | 私聊发一句话由管家回答或开话题；设置卡有「管家」标签。管家只用 Claude Code，Claude Code 没启用时私聊照旧开话题 | `COVEY_BUTLER=0`；或主人在设置卡「管家」页把私聊顶层改成“每条直接开一个话题” |
| [专项](projects.md) | `COVEY_PROJECTS=1`（`./install` 写） | 管家把长期要跟进的事开成专项时，私聊出现专项卡 | `COVEY_PROJECTS=0` |
| [整理专项](#整理专项)（只提议） | `COVEY_PROJECT_TIDY=suggest`（`./install` 写，要 `COVEY_PROJECTS=1`） | 话题多了以后私聊出现“建个专项？”的提议卡；开了早晚汇总时看板卡上有「整理专项」 | `COVEY_PROJECT_TIDY=off` |
| 以后这类按推荐走 | `COVEY_STANDING_ANSWERS=1`（`./install` 写） | 每题都有推荐项的问题卡下面多这个按钮 | `COVEY_STANDING_ANSWERS=0` |
| 撤回与编辑 | `COVEY_SOURCE_WATCH`（默认开） | 撤回一条已变成任务的消息，任务跟着停；编辑后任务收到改后的内容 | `COVEY_SOURCE_WATCH=0` |
| [自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | `COVEY_APP_CHECK`（默认开） | 应用缺权限、事件或菜单时私聊出现「飞书应用待补」卡 | `COVEY_APP_CHECK=0` |
| [敏感词脱敏](#敏感词脱敏) | 没有开关 | 设置卡有「脱敏」标签；话题开了脱敏才替换，只支持 Claude 的模型 | 设置卡上关 |
| [代发](proxy.md#代发) | `COVEY_OWNER_SEND`（默认开） | 只在有代答发送器时才有：话题能起草“以主人身份发出”的消息，主人在卡上确认后发 | `COVEY_OWNER_SEND=0` |

### 推荐打开

不需要别的东西，设上变量、重新注册就有。

| 功能 | 开关 | 打开后多什么、怎么确认 |
| --- | --- | --- |
| [早晚汇总](#早晚汇总) | `COVEY_DIGEST_TIMES=09:30,21:30`（按 Asia/Shanghai） | 到点在私聊发汇总卡；菜单多「看板」（应用里还没有这一项时，「飞书应用待补」卡会补上） |
| [手上的事与话题看板](#手上的事与话题看板) | `COVEY_PINNED_OVERVIEW=1`，再加 `COVEY_PINNED_TOPICS=1` | 私聊置顶一张「手上的事」；后者再把等你、在做、有结果的话题自动 Pin 上（要置顶权限和“消息已读”事件，「飞书应用待补」卡会提示） |
| [记忆](#记忆) | `COVEY_MEMORY=1` | 你在话题里说的长期要求一轮结束后记下，答案卡底部露一行和“不对”按钮 |
| [知识条目](#知识条目与历史搜索) | `COVEY_KNOWLEDGE=1` | 说“记一下”的事记成条目，话题首轮带上，汇总多一节 |
| [同时做几件事](butler.md#同时做几件事开关默认关) | `COVEY_BUTLER_TRACKS=1` | 管家在做一件事时，你的下一句可以并进去或另起一件，不用等 |
| [派发卡](#派发卡) | `COVEY_DISPATCH_CARD=1` | 管家把事交给话题后，私聊聊天区多一张实时刷新的卡 |
| 专项的进阶 | `COVEY_PROJECT_AUTOWAKE=1`、`COVEY_PROJECT_ASK=1`、`COVEY_PROJECT_MEMORY=1`、`COVEY_PROJECT_TRIAGE=1`、`COVEY_PROJECT_UPGRADE=butler`（都要 `COVEY_PROJECTS=1`，最后一个还要派发卡） | 依次是：话题一轮结束就叫醒负责人；专项停住时发卡问你继续、收尾还是停；专项有自己的记忆和资料库；话题的问题先给负责人分流；管家派出的事能静默转成专项。说明见[专项 · 专项卡](projects.md#专项卡) |

### 按需打开

要额外准备东西，或只有部分人用得上。

| 功能 | 要准备什么 | 开关 |
| --- | --- | --- |
| [代答](#代答) | 带发送器的扩展（核心没有发送器）、主人本人登录的 lark-cli profile | `COVEY_PROXY_PROFILE`，见[代答 · 前置条件与怎么开](proxy.md#前置条件与怎么开) |
| [网页工作台](web.md) | 一个配套的网页应用和它的开放接口密钥 | `COVEY_WEB=1`、`COVEY_WEB_CONFIG` |
| [多设备](#多设备开发中默认关)（开发中） | 能 ssh 过去的其他机器、`devices.json` | `COVEY_DEVICES=1` |
| [节点与机器状况](#节点与机器状况默认关) | 一个设备能连进来的监听地址 | `COVEY_NODE_LISTEN` |
| [群聊免@](#群聊免) | 应用要有收群里全部消息的权限（要管理员审核） | `COVEY_GROUP_ALL_MESSAGES=1` |
| [历史搜索](#知识条目与历史搜索) | 一个能按一句话搜历史的命令 | `COVEY_HISTORY_CMD=/absolute/path/to/搜索命令` |
| [管家规则](#管家规则) | 想让管家只快答和分派、不自己干活 | `COVEY_BUTLER_RULES=1` |
| [我本来想找你的事](butler.md#我本来想找你的事开关默认关只记不打扰) | 只记不打扰，看看管家会提醒你什么 | `COVEY_PATROL=shadow` |
| [飞书 SDK](#飞书-sdk-与直连) | 在运行 Bridge 的 Python 里装 `.[feishu-sdk]` | `COVEY_CARD_TRANSPORT=sdk`、`COVEY_EVENT_TRANSPORT=sdk` |
| [内存看门狗](operations.md#内存看门狗) | 不是环境变量 | `python3 -B bin/covey_memguard.py enable` |
| [本机运行锁](runtime-locks.md) | 同一台机器上有几个服务要共用锁 | `COVEY_LOCK_ROOT` |

### 改开关三步

环境变量写在服务清单里，Bridge 只认清单，不认你登录终端里后来设的变量。每次重新注册，清单里本项目的变量都**整组换成**执行注册的那个终端里的，所以先把现有的载入终端，再改：

1. **载入现在的设置**：新开一个终端，进入服务正在运行的代码目录，按[载入服务清单的环境](deploy.md#载入服务清单的环境)把清单里的变量载入终端。
2. **改，再重新注册**：`export COVEY_DIGEST_TIMES=09:30,21:30`；关一个开关时把它设成关闭值（多数是 `0`，各自的取值见上表和[一览](#环境变量与选项一览)），不要只 `unset`。然后：
   - 服务还从 `./install` 的检出运行（没有用 `deploy_bridge.sh` 升级过）：在检出里重新运行 `./install`，它发现设置变了会重新注册；
   - 服务从 `releases/` 下的固定 release 运行：在那个 release 目录里运行 bootstrap，见[改设置后重新注册](deploy.md#改设置后重新注册)。
3. **确认**：`python3 -B bin/covey_service.py status --state-dir "$STATE_DIR"` 里的 `generation` 加了一，`bridge.log` 有新的 `bridge started`；再在飞书里按上表“怎么确认”那一列看一眼。

`covey_service.py restart` 只换进程、不换清单，所以改开关不能只 `restart`；`harnesses.json` 和 `devices.json` 才是改完文件 `restart` 就行。

## 配置放在哪

| 放在哪 | 管什么 | 怎么改 |
| --- | --- | --- |
| 服务命令行 | Bridge 自己的参数（`--workdir`、`--profile`、`--app-id`、`--state-dir`、`--owner`、`--harness-config`）和默认 Harness（Codex）的参数 | 重新注册（`scripts/bootstrap_harness_bridge.sh` 或 `scripts/deploy_bridge.sh`） |
| `harnesses.json`（`--harness-config`） | 其余 Harness 的参数和模型顺序 | 改文件后重启（`restart` 或重新注册） |
| `devices.json`（`$COVEY_DATA_HOME/devices.json`，或 `COVEY_DEVICES_FILE` 指的文件） | 多设备（开发中，默认关）：每台设备怎么 ssh、能跑哪些 Harness、给任务的环境和谁能用；`COVEY_DEVICES=1` 才读 | 改文件后重启 |
| 服务清单里的环境变量 | 可选功能的开关、Harness 的环境（`PATH`、`CODEX_HOME`、代理等） | 在终端里设好后重新注册，见下文 |
| 状态目录里的设置文件 | 主人在飞书里改的设置：工作区列表、私聊模式、代答、脱敏词表等 | 在飞书的设置卡上改，实时生效 |

服务清单不保存 App Secret、访问令牌和 Harness 凭据。它们留在 lark-cli 和各 CLI 自己的配置里。

## harnesses.json

`--harness-config` 指向一个 JSON 文件。服务只记路径，每次启动时读，所以文件要放在持久位置（建议 `$COVEY_DATA_HOME/harnesses.json`）。文件里不放凭据。

```json
{"harnesses": {
  "<扩展 Harness id>": {"args": ["--<扩展参数>", "/absolute/runtime/venv/bin/python"]},
  "pi": {"args": ["--pi-cmd", "/absolute/pi/node_modules/.bin/pi"], "models_first": ["<模型 ID>"]}
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
| `allow_team_spaces` | `true` | `false`：不激活任何群，群里 @ 也不服务（群管理卡的“启用”给出说明），只服务主人 |
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

- **终端里没有的变量，清单里也就没了。** 在没有代理变量的终端里重新注册，清单里的代理就会丢；终端里多出的 `LARK_*BRIDGE_*` 也会被保存。所以先[载入服务清单的环境](deploy.md#载入服务清单的环境)再改，重新注册前对比一遍（[部署新版本](deploy.md#部署新版本)第 3 步）。
- **关开关要设成关闭值，不能只 `unset`。** Bridge 的环境是 Manager 自己的环境加上清单里的变量（再加 `PYTHONDONTWRITEBYTECODE=1`、`COVEY_MANAGED=1`、`TMPDIR=<state>/tmp`）。Manager 是第一次 `launch` 时从那个终端拉起的，带着当时的变量一直跑；之后的 `launch` 只换清单，所以终端里去掉的变量还留在 Manager 的环境里。设成关闭值（如 `COVEY_BUTLER_TRACKS=0`）后清单里的值会盖过它。实在要清掉 Manager 环境里的旧变量：取 `"$SERVICE_ROOT/manager.json"` 里 `process.pid`，`kill -TERM -- -<pid>` 结束它的进程组（Bridge 和任务进程各在自己的会话里，不受影响），`pgrep -g <pid>` 确认没有残留，再在干净的终端里 `launch`。systemd 等托管方式拉起的 Manager 不带终端变量，重启单元即可。
- **`restart` 不补变量。** 开机后 Manager 由开机守护拉起，不读交互 shell 的初始化文件，所以 `PATH`、代理和本项目的设置只能靠清单；清单里缺的变量要重新 `launch`。
- 变量值不能含换行。代理 URL 只能是 `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h`，要有主机名，不能带用户名、密码、路径、查询参数或片段（`NO_PROXY` 不校验）。不符合时 `launch` 拒绝写入，报 `only explicit non-secret runtime paths/settings can be persisted` 或 `only non-secret proxy URLs without credentials can be persisted`。带凭据的代理不能托管。
- 常用的变量：`COVEY_PROFILE`（Bridge 的 lark-cli profile，所有 Harness 共用）、`COVEY_OUTGOING_MEDIA_DIRS`（回复里本地 Markdown 图片额外查找的目录，用 `:` 分隔）。

**不再读取的变量**：以前给后端参数设默认值的 `LARK_BRIDGE_CODEX_*`、`LARK_CLAUDE_BRIDGE_*` 等变量现在不读，设了启动日志记一行 `env <名字> is no longer read; put <参数> in …`；其中两个权限变量设了非默认值会让诊断和部署预检失败。换成哪个参数、怎么清掉，见[已下线的功能与旧配置 · 不再读取的变量](legacy.md#不再读取的变量)。

## 可选开关

只有部分部署要用的功能默认关闭：没配置时不起后台线程、不加菜单项、不改已有行为。打开、关掉都按[改开关三步](#改开关三步)做；下表列每个开关打开后多出的后台线程和入口，给要核对的人看（打开后用户看到什么见[开关速查](#开关速查)）。

| 功能 | 怎么打开 | 打开后多什么 |
| --- | --- | --- |
| [代答](#代答) | `COVEY_PROXY_PROFILE` 和发送器 | `proxy`、`proxy-in` 后台线程；设置卡「代答」能用 |
| [手上的事与话题看板](#手上的事与话题看板) | `COVEY_PINNED_OVERVIEW=1`、`COVEY_PINNED_TOPICS=1` | `overview` 后台线程；Pin 话题时多一个已读事件消费者 |
| [早晚汇总](#早晚汇总) | `COVEY_DIGEST_TIMES` | `digest` 后台线程、菜单「看板」 |
| [知识条目与历史搜索](#知识条目与历史搜索) | `COVEY_KNOWLEDGE=1`；`COVEY_HISTORY_CMD=/absolute/path/to/搜索命令` | 管家多 `knowledge_*` 和 `history_search` 工具；话题首轮带知识条目，规则多一行查历史的命令 |
| [记忆](#记忆) | `COVEY_MEMORY=1` | 每轮规则里多一块“记忆”；一轮结束后记下主人的长期要求，答案卡底部露一行和“不对”按钮 |
| [管家](#管家) | 默认允许；`COVEY_BUTLER=0` 不允许 | `main-tools`、`main-search` 后台线程；设置卡多「管家」标签 |
| [管家规则](#管家规则) | `COVEY_BUTLER_RULES=1` | 不多线程，只换管家规则的两条 |
| [专项](projects.md)（`./install` 新装默认开） | `COVEY_PROJECTS=1` | `projects/` 记录、负责人话题和它的 `project_*` 工具、看板并格；进阶开关见[开关速查](#推荐打开) |
| [整理专项](#整理专项) | `COVEY_PROJECT_TIDY=suggest`（要 `COVEY_PROJECTS=1`） | `project_tidy` 后台线程；管家多 `projects_tidy`，看板卡多「整理专项」，提议卡、整理专项卡 |
| [专项停住后问主人](projects.md#专项停住后问主人) | `COVEY_PROJECT_ASK=1`（要 `COVEY_PROJECTS=1`） | 专项被护栏停住时多发一张卡：继续 / 收尾 / 停 |
| [同时做几件事](butler.md#同时做几件事开关默认关) | `COVEY_BUTLER_TRACKS=1` | 管家消息的分流；`COVEY_TRACK_JUDGE_WARM` 没关时多一个待命的小模型进程 |
| [我本来想找你的事](butler.md#我本来想找你的事开关默认关只记不打扰) | `COVEY_PATROL=shadow` | `patrol` 后台线程，只写 `patrol/` 记录；汇总和「看板」多一块，不发消息 |
| [网页工作台](web.md) | `COVEY_WEB=1` 加 `COVEY_WEB_CONFIG=/absolute/path/to/web.json` | `web-push`、`web-inbox` 后台线程：把专项、话题、一轮、问题推给网页应用，取回网页上的操作 |
| [派发卡](#派发卡) | `COVEY_DISPATCH_CARD=1` | `dispatch_cards` 后台线程；管家派出话题后多一张聊天区的活卡片 |
| [群聊免@](#群聊免) | `COVEY_GROUP_ALL_MESSAGES=1` 加应用权限 | 设置卡「群聊」每个群多一个“免@” |
| [多设备](#多设备开发中默认关)（开发中） | `COVEY_DEVICES=1` 加 `devices.json` | `devices` 后台线程（有设备时才起）；设备上能跑 Claude Code 和 Codex 的话题 |
| [节点与机器状况](#节点与机器状况默认关) | `COVEY_NODE_LISTEN=<监听地址>` | `nodes-gateway`、`nodes-alerts`、`nodes-local` 后台线程；机器状况告警卡，设置卡「设备」标签 |
| [飞书 SDK 与直连](#飞书-sdk-与直连) | `COVEY_CARD_TRANSPORT=sdk`；直连默认开 | 卡片和消息走常驻 SDK 客户端 |
| [事件长连接走 SDK](#飞书-sdk-与直连) | `COVEY_EVENT_TRANSPORT=sdk` | `event-websocket` 线程代替 lark-cli 事件消费者 |
| [自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 默认开；`COVEY_APP_CHECK=0` 关 | `app-setup` 后台线程 |
| [内存看门狗](operations.md#内存看门狗) | `bin/covey_memguard.py enable` | 独立进程；`memguard` 后台线程转发告警 |
| [本机运行锁](runtime-locks.md) | `COVEY_LOCK_ROOT` | 同机多个服务共用的锁 |

### 代答

[代答](usage.md#代答)要两样东西（实现见[代答](proxy.md)）：

- `COVEY_PROXY_PROFILE`：带主人本人登录的 lark-cli profile，要有 `im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`search:message`、`contact:user:search` 和 `offline_access`。用户 token 的刷新期是 7 天；轮询每分钟都在用它，正常会一直续上，代答关掉超过 7 天会过期。过期后 `lark-cli --profile <它> auth login` 重新授权。第一次授权：`lark-cli --profile <它> auth login --scope "im:message.p2p_msg:get_as_user im:message.group_msg:get_as_user search:message contact:user:search offline_access"`，用主人自己的飞书账号确认；可以就用 `./install` 建的那个 profile（它已有主人的登录，补上这些权限即可）。权限要先在应用的权限管理里开通，「飞书应用待补」卡不管这些。
- 发送器（扩展钩子 `proxy_sender`，见[发送器](proxy.md#发送器)）：没有发送器就没有代答入口。发送器的安装与配置见提供它的扩展的 README；开代答的完整步骤见[代答 · 前置条件与怎么开](proxy.md#前置条件与怎么开)。

代发（[话题以主人身份发消息](proxy.md#代发)）跟着发送器：有发送器就开，`COVEY_OWNER_SEND=0` 关掉。它用同一个 `COVEY_PROXY_PROFILE` 找收件人，另要 `contact:user:search`。

验收页面（`bin/covey_feishu_web.py serve|accept`，见[浏览器服务](proxy.md#浏览器服务)）另用一个装了 Playwright 的 Python，例如 `$COVEY_DATA_HOME/proxy-web/venv/bin/python`，代答发送不用它。主人的网页端登录只留 `<state>/proxy/web/profile`（0700）一份，不进备份：

- 别的工具要用就复制一份到自己的运行目录，不要直接打开它（同一个 profile 只能有一个 Chromium）。
- 复制前确认 `<state>/proxy/batches/` 里没有 `status` 为 `sending` 的批次，并排除 `Cache`、`Code Cache`、`Service Worker`、`GPUCache`、`Dawn*Cache`、`IndexedDB`、`Singleton*`。
- 它的缓存超过 200 MiB 时由维护删掉（日志 `proxy web login caches removed: N MiB`），cookie 和登录不受影响；条件见[架构 · 代答](architecture.md#代答)。

代答的日志行见[排障 · 代答](troubleshooting.md#代答没有回复或没有发出)。

### 敏感词脱敏

脱敏的词表、开关和替换记录在设置卡的「脱敏」标签上，不用配置，所有部署都有；开了[网页工作台](web.md)时，它的设置页也能批量加删词。只有 Claude 的模型能脱敏，话题开了才替换（用法见[使用指南](usage.md)，机制见[架构 · 敏感词脱敏](architecture.md#敏感词脱敏)）。

- 「脱敏」标签：填词框一次加或删一批（按行，删除要整词一致），替身词自动生成；折叠面板里是开关、替换记录、「试一下」和上游地址。卡上不出现原词，只写首字和字数。“撤销”只记在 Bridge 进程的内存里，重启后就没有了。
- 词表在 `<state>/redaction/config.json`（0600），每轮开始时读，改动下一轮生效；每轮的替换次数记在 `<state>/redaction/turns.jsonl`。「探测」在后台跑一次 `<claude 命令> --gateway-status`，只显示结果、不改配置。

### 手上的事与话题看板

- `COVEY_PINNED_OVERVIEW=1`：在主人私聊里置顶一张「手上的事」汇总卡（见[手上的事](usage.md#手上的事)）。只认 `1`。主人的私聊取 `<state>/owner-chat.json`（主人最近一次私聊机器人时记下），还没有就不发。卡片记录在 `<state>/pinned-overview.json`。置顶用 `im pins create`，机器人发消息的权限就够。日志：`pinned overview sent|pinned|retired …`，失败是 `pinned overview not sent|pin failed|update failed`。
- `COVEY_PINNED_TOPICS=1`：在「手上的事」上面自动 Pin 话题（见[话题看板](reference.md#看板自动-pin-话题)）。只认 `1`，而且要 `COVEY_PINNED_OVERVIEW=1` 也开：由「手上的事」的后台线程管，它那张卡置顶之后才开始 Pin。最多 10 个话题（`app/pins.py` 的 `MAX_PINS`），顺序变了才动，最多每 60 秒一轮。私聊的 `im pins list` 读不到东西，Bridge 只信自己的记录 `<state>/pinned-topics.json`。`<state>/board-priority.json`（话题 → 时间）让话题在本组排最前，由管家的工具写。看板从不撤回消息。日志：`pinned board pin|unpin|move|dismissed message=… topic=… group=…`，失败是 `pinned board failed op=… failures=N`。
- 已读事件：开了 `COVEY_PINNED_TOPICS=1` 时 Bridge 多起一个消费者 `lark-cli event consume im.message.message_read_v1`（后台线程 `topic-seen`，`app/topic_seen.py`）。只认主人读的消息；每条在主人私聊的话题别名里查，查到的话题记下最近一次读到的时间，写进 `<state>/topic-seen.json`（90 天没读的和超出 2000 个的较早话题删掉）。开发者后台要订阅「消息已读」事件，应用要有 `im:message` 和 `im:message:readonly`；缺了消费者打印 `missing_scope` 后退出，每 300 秒重试，其余功能照常，健康检查不看它。

关掉：按[改开关三步](#改开关三步)设成 `0`。已置顶的卡和已 Pin 的话题留在原处，在私聊的 Pin 页里手动取消；记录文件留着不再更新。

### 早晚汇总

`COVEY_DIGEST_TIMES=09:30,23:30`：逗号分隔的 HH:MM，按 Asia/Shanghai，重复的只算一次，写错的跳过并记日志。每到一个时间点，Bridge 在主人私聊的顶层发一张汇总卡（`app/digest.py`、`cards/digest.py`；用户看到什么见[早晚汇总](usage.md#早晚汇总)）。不设或一个合法时间都没有就不起后台线程，也没有菜单「看板」。

- 记录在 `<state>/digest.json`：最后发出的时间点 `last_slot`、发送中的 `pending`、最近 6 张发出的卡 `sent`。一个时间点只发一次：Bridge 在时间点停着、60 分钟内起来就补发一次，超过就跳过。卡先存再发；没确认的 60 秒后用同一个幂等键再发，最多 3 次。
- 数据从记录读（`questions/`、`jobs/` 的索引），不持记录锁、不调模型，管家话题不列。
- 菜单「看板」（事件 key `digest_board`）只在设了这个变量时有：发同一张卡、按现在取数，不改 `last_slot`。卡的记录在 `<state>/digest-boards/`。要在开发者后台的机器人菜单里加这一项（自动补齐会加）。
- 日志：`digest sent slot=HH:MM`、`digest not sent: nothing to list`、`digest not sent try=N`、`digest not sent after N tries; given up`、`digest card redrawn message=… cleared=N`、`digest board sent`。

### 整理专项

`COVEY_PROJECT_TIDY`（要 `COVEY_PROJECTS=1`；用户看到什么见[整理专项](usage.md#整理专项)，机制见[专项 · 整理专项](projects.md#整理专项)）：

- `suggest`（只提议）：每轮结束时话题自己的模型更新话题要点；到实时时机、早晚汇总的时间点（`COVEY_DIGEST_TIMES`）、或主人手动叫时整理一次。能归进已有专项、把握很高的直接归，收起没事的专项也直接做；新建、合并、拆开、改名都发卡问，主人点了才做。
- `auto`：再加新建专项也直接做（专项卡上留「不要专项」撤回）。主人在提议卡上点了「以后直接建」，效果和 `auto` 一样。
- 不设、`off` 或别的值：关。没有后台线程、管家没有 `projects_tidy`、设置卡没有这一行、每轮的消息里没有话题要点那段，行为和原来一样。

仓库代码里不设就是关（AGENTS.md 规则 4）；`./install` 写服务清单时，终端里没设的就写上 `COVEY_PROJECTS=1`、`COVEY_PROJECT_TIDY=suggest`、`COVEY_STANDING_ANSWERS=1` 这三个（`service/installer.py` 的 `INSTALL_SWITCHES`，见[默认已开](#默认已开)），终端里设了的（新名旧名都算）不动。要关，按[改开关三步](#改开关三步)设 `COVEY_PROJECT_TIDY=off`。

可调的数（都是正数，写错按默认）：`COVEY_PROJECT_TIDY_DAYS`（7，整理看最近几天的话题）、`COVEY_PROJECT_TIDY_AGE_DAYS`（3，话题跨进第几天算一次实时时机）、`COVEY_PROJECT_TIDY_WORDS`（15，主人在话题里说到第几句算一次）、`COVEY_PROJECT_TIDY_MIN_TOPICS`（5，聊天里的话题少于这个数时不整理）、`COVEY_PROJECT_TIDY_JUDGE_SECONDS`（90，一次判断调用的上限）、`COVEY_PROJECT_TIDY_READ_SECONDS`（60，补一个话题要点的上限）。补话题要点和判断用 Claude Code 的 `COVEY_PROJECT_TIDY_MODEL`（默认 `sonnet`：两者都要读整段对话、说出一件事的本质，小模型不够），走 Claude Harness 自己的包装，和[同时做几件事](butler.md#同时做几件事开关默认关)的小模型判断同一套做法；没有 Claude Harness 时判断不了，整理卡写“没有可用的 Claude Code 来判断”。

### 记忆

- `COVEY_MEMORY=1`（只认 `1`）：打开“记忆”（[管家 · 记忆](butler.md#记忆开关默认关)）。不依赖别的开关：原则一直有，`COVEY_KNOWLEDGE=1` 时知识条目也进来，`COVEY_PROJECTS=1` 且 `COVEY_PROJECT_MEMORY=1` 时专项记忆也进来；话题自己的记忆放在 `<state>/topics/<话题>/memory.json`，打开后才会有。
- 关着（默认）：规则、续接提醒、答案卡和任务记录都和没有这个功能时一样，不读不写话题的 `memory.json`。
- 关掉：按[改开关三步](#改开关三步)设成 `0`；已经写下的记忆留着。
- 日志：`memory lines written=N of M job=#…`、`memory lines ignored reason=… job=#…`、`memory lines failed job=#…`。

### 知识条目与历史搜索

两个变量互相独立，都只在主人自己的部署里设；没设时不建目录、不改规则和提示词、管家的工具清单里没有对应的工具、汇总里没有这一节。设计和实现见[管家 · 知识库](butler.md#知识库)，主人怎么用见[使用指南 · 知识条目](usage.md#知识条目)。

- `COVEY_KNOWLEDGE=1`（只认 `1`）：打开知识条目。记录在 `<state>/knowledge/`；管家多 `knowledge_save`、`knowledge_update`、`knowledge_drop`、`knowledge_search`；每个话题的新会话带上条目，续接的会话只补上一轮之后新增或改动的；主人在话题里明确说“记一下 / 记住 / 记下来”时，Bridge 在那一轮结束后把检查点的 `remember` 记下来；早晚汇总多“新记的知识”。
- `COVEY_HISTORY_CMD=/absolute/path/to/kb-history`：历史搜索命令的**绝对路径**（相对路径等于没设）。设了以后，主人自己的话题规则里多一行“要查以前做过的事用 `<命令> search <一句话>`”，管家多 `history_search`（跑 `<命令> search [--workspace W] [--since Nd] -- <一句话>`，自己的进程组里最多等 70 秒，超时整组杀，输出最多 20 KB）。命令本身不在这个仓库。
- 关掉：按[改开关三步](#改开关三步)设成 `0`；已经写下的 `knowledge/` 留着。
- 日志：`knowledge remember written=N job=#…`、`knowledge remember ignored reason=… job=#…`、`knowledge remember failed job=#…`；管家工具的 `main tool name=knowledge_save …`。

### 管家

`COVEY_BUTLER` 决定部署允不允许管家（私聊模式“管家”，见[管家](butler.md#私聊模式)和[使用指南 · 管家](usage.md#管家)）：

- `0`：不允许。不起后台线程、不改消息路由，设置卡没有管家的标签。
- 不设或 `1`：允许。用不用由主人在设置卡上选的模式定（`<state>/main-thread.json` 的 `mode`），改模式不用重启。新部署第一次启动写成管家，日志 `main thread mode settled mode=agent|topics`。
- 管家的设置在 `<state>/main-thread.json`，工具请求在 `<state>/main-thread/requests/`。管家的会话只能用 Claude Code。
- `main-thread.json` 里旧的 `"desk_topics": true` 读到也按 `false` 处理，启动时记一行 warning，见[已下线的功能与旧配置](legacy.md)。

### 管家规则

`COVEY_BUTLER_RULES=1`（只认 `1`）：管家的规则里，“只读的、一两分钟能做完的自己做完直接答”和“可能超过一两分钟的交给话题”这两条换成管家规则：几秒能答完的直接答，要查、要跑、要改、预计超过半分钟的用 `topic_start`、`topic_send` 派给话题，对主人只说一句放到哪个 T 号、大概多久。主人明确说“你直接做”时例外。`topic_start` 的工具说明也换成同样的说法（Bridge 把变量交给管家会话里的 MCP 服务，由它列工具）。其余规则一条不动；没设时每轮提示词和工具说明都和原来逐字节相同（`tests/golden/butler_prompt.txt`）。规则全文和旧会话什么时候用上新规则，见[管家 · 管家规则](butler.md#管家规则)。

- 打开、关掉都按[改开关三步](#改开关三步)做：设 `COVEY_BUTLER_RULES=1` 或 `0`。
- 不影响话题的规则、工具清单（只换 `topic_start` 的说明）和卡片；跑过 `CARD_AFTER`（90 秒）才出卡的兜底照旧，没有另设变量。

### 派发卡

`COVEY_DISPATCH_CARD=1`（只认 `1`）：管家用 `topic_start`、`topic_send`、`topic_fork` 把一件事交给话题后，在私聊的聊天区发一张**活卡片**（一条新消息，顶上“你问”块链到主人那条提问；不能回复那条提问，它下面已有话题串）：卡头是主人请求的前 30 字加状态（在做、等你、做完、失败、已停），一行灰字写 T 号、开始时间和用时，折叠面板“话题”写话题的标题、工作区和当前步骤，下面是输入/输出灰框，按钮是补充、停止、打开话题。`dispatch_cards` 后台线程每秒读一次话题的任务记录，内容变了且离上次写超过 2.5 秒才整卡刷新一次。话题的结论回带发出后，卡缩成一行“✅ 做完 · 结论在下方 ›”（链到结论），之后不再刷新。说明见[管家 · 派发卡](butler.md#派发卡开关默认关)。

- 打开、关掉都按[改开关三步](#改开关三步)做：设 `COVEY_DISPATCH_CARD=1` 或 `0`；关着时不起后台线程、不发这张卡，话题的任务卡、回带和小卡（`hook_card`）和原来一样。
- 记录在 `<state>/main-thread/dispatch/`，7 天没动的由维护删。

### 群聊免@

`COVEY_GROUP_ALL_MESSAGES=1`：主人可以按群选谁说话不用 @ 机器人（见[在群里用](usage.md#在群里用)）。只认 `1`。

- 要先给应用开通 `im:message.group_msg`（获取群组中所有消息，敏感权限，可能要企业管理员审批）和 `im:chat.members:read`（选人时列群成员），并发布新版本。
- 设置记在 `<state>/groups/<…>.json` 的 `free_talk`、`free_talk_members`。不设变量时卡上没有免@，这些设置不生效。
- 开通权限后机器人所在每个群的全部消息都会推给 Bridge：没人免@ 的消息在准入一步丢掉，不写历史、不记日志。日志：`group chat=… free_talk=…`；被拒的免@ 消息是 `ignored message … reason=for_others|proxy_chat`。

### 多设备（开发中，默认关）

让话题在另一台机器（Linux，经 ssh）上工作，而不是在 Bridge 所在的机器上。设备上能跑 Claude Code 和 Codex 的话题；默认关，真机验收在进行。结构、连接、离线恢复和文件进出见[架构 · 设备](architecture.md#设备多设备默认关)，Harness 在设备上的差别见[Harness 说明](harnesses.md#在设备上运行)。

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
| `ssh.host` | `~/.ssh/config` 里的别名或 `user@host`。不能有空格，不能以 `-` 开头 |
| `ssh.options` | 追加的 ssh 参数（`-J` 跳板、`-p`、`-i` 等）。Covey 自己的选项排在前面，这里改不掉它们 |
| `ssh.python` | 设备上的解释器，默认 `python3`（3.9 起） |
| `harnesses` | 这台设备上要用的 Harness，`claude`、`codex` 的子集，默认两个都要 |
| `env` | 只写要覆盖设备环境档案的变量（比如这台机器出网要的代理）。值里不能带 `user:密码@` |
| `login_shell` | `true` 时自检再用登录 shell 取一次环境变量（`.bashrc` 里设的代理）。默认 `false` |
| `spaces` | 谁可以用这台设备：`owner_private`（默认，主人私聊）、`personal_groups`、`team`（显式写才开）。代答群和没绑定的群永远不能用 |

**用**：主人在启动卡「其他目录…」或设置卡「工作区」页「＋ 添加工作区…」的目录浏览器里输入 `gpu1:/home/me/proj`，登记成一个设备工作区（名字取 `别名-目录名`），开话题时选它。设备没答复、目录不存在、别名不在 `devices.json` 里都当场报错。

**自检**：`python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1`（`list` 只打印每台设备最近一次的结果），新加设备、改了 ssh 或代理设置后跑一次，见[运维 · 设备自检](operations.md#设备自检)。失败时写明是哪一步：连不上、没装、没登录、超时，或连不上模型服务（去哪里设 `HTTPS_PROXY`）。

**已知限制**：
- 只有 Linux 设备真机验证过；只支持 Claude Code 和 Codex。
- 设备上的话题没有回退、压缩、下载请求与结果、敏感词脱敏和分叉（按钮不出现）；开着脱敏的话题在设备上的一轮直接拒绝。
- 不能由管家开（管家自己的工作区也不能是设备），不能用「独立 worktree」，换配置不生成旧模型的摘要。
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

- `COVEY_CARD_TRANSPORT=sdk`：CardKit 卡片接口、消息的发送和回复、读消息详情都用常驻的 `lark-oapi` 客户端；上传文件和图片、转发话题等只有 lark-cli 做得了的仍走 lark-cli（见[投递、重投与 outbox](architecture.md#投递重投与-outbox)）。先在运行 Bridge 的那个 Python 里 `python3 -m pip install '.[feishu-sdk]'`。SDK 只支持显式命名的 profile，应用密钥只在内存里读取。请求发出前取不到凭据时退回 CLI（日志 `CardKit SDK unavailable locally; using CLI`）；请求已发出后不换传输重发。
- `COVEY_EVENT_TRANSPORT=sdk`：Bridge 不起 lark-cli 的事件消费者，用一个 `lark_oapi` WebSocket 长连接收四类事件（消息、卡片点击、机器人菜单、已读回执），卡片点击在应答里立刻回 toast“收到”。同一个应用只能有一个事件长连接，所以**打开后四类事件全走 SDK**，同时也不能在别处再跑这个应用的 `lark-cli event consume`。依赖同上（`.[feishu-sdk]`），要显式命名的 profile。切回：去掉这个变量（或设 `cli`）并重启 Bridge。细节见[事件长连接走 SDK](architecture.md#事件长连接走-sdk默认关)。
- `COVEY_FEISHU_DIRECT`：默认直连飞书开放平台，`0`（也认 `false`、`no`、`off`）时都走环境里的代理。直连时 Bridge 启动先试一次连 `open.feishu.cn:443`（lark 品牌是 `open.larksuite.com`），连得上就把它加进本进程的 `no_proxy`、`NO_PROXY`，之后的子进程都继承，日志 `Feishu direct: lark-cli reaches open.feishu.cn without the proxy`；连不上记 `Feishu direct unavailable (<异常类型>); lark-cli keeps the proxy`，环境不动。事件长连接的主机不在其中，仍走代理。

### 其他

- `COVEY_APP_CHECK=0`：不检查飞书应用、不发「飞书应用待补」卡。检查和补齐见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用)。
- `--health-timeout`：`launch` 的参数，默认 120 秒，0 关闭。Bridge 本地健康接口连续这么久没有响应就替换进程。bootstrap 不传它，所以经 bootstrap 注册的都是 120。
- `COVEY_PRIVACY_DENYLIST`（只给 `scripts/privacy_check.py` 读，Bridge 本身不读）：指向仓库外的文本文件，每行一个公司域名或内部名词，隐私检查按它查；不设就只查通用规则，扩展里放有一份，见[隐私检查](testing.md#隐私检查)。

## 环境变量与选项一览

**命名**：设置的名字是 `COVEY_<X>`，旧名 `LARK_BRIDGE_<X>` 照常生效，不用改任何配置（`covey/core/env.py` 的 `get` 先读新名；只设了旧名时 stderr 记一行弃用）。下表只写新名，每一行的旧名就是把 `COVEY_` 换成 `LARK_BRIDGE_`；管家改名时连名字一起改了的七个开关在行里注明旧名，读取顺序是 `COVEY_<新>`、`COVEY_MAIN_<旧>`、`LARK_BRIDGE_MAIN_<旧>`（`core/env.py` 的 `RENAMED`）。改名之后才有的开关（`core/env.py` 的 `BORN_NEW`）没有旧名。`LARK_<HARNESS>_BRIDGE_*` 这类 Harness 自己的前缀不变。服务清单为什么还存旧名、子进程拿到哪些写法，见[已下线的功能与旧配置 · 环境变量的旧名](compat.md#环境变量的旧名)。

代码在 `covey/` 和 `bin/` 里读的每个环境变量和命令行参数都列在这里，`tests/test_structure.py` 的 `InventoryTest` 核对：代码读了就要有一行，行里写出读它的文件；表里写了的代码也要读。不列 `HOME`、`PATH`、`PYTHONPATH`、`XDG_CONFIG_HOME`、`NO_PROXY`/`no_proxy` 这类系统变量。不再读取的环境变量不在表里，只有两个权限变量还为拦截而读（见[不再读取的变量](legacy.md#不再读取的变量)）。

| 环境变量 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `COVEY_DATA_HOME` | `core/paths.py`、`core/devices.py`（找 `devices.json`）、`core/extensions.py`（找本部署启用、停用的扩展 `extensions/enabled`） | 按 [数据目录](operations.md#数据目录) 推算 | 否 |
| `COVEY_SERVICE_ROOT` | `core/paths.py` | `<数据根>/bridge-services` | 否 |
| `COVEY_STANDING_ANSWERS` | `cards/question_cards.py`（`standing_offered`） | 关；`1`：问题卡上给「以后这类按推荐走」。`./install` 新装的服务写 `1`（`service/installer.py` 的 `INSTALL_SWITCHES`） | 否 |
| `COVEY_STATE_DIR`；`LARK_CLAUDE_BRIDGE_STATE_DIR`、`LARK_DSH_BRIDGE_STATE_DIR`、`LARK_PI_BRIDGE_STATE_DIR`、`COVEY_AGY_STATE_DIR` | `app/harnesses.py`（只有默认 Harness 读，其余总用 Bridge 的 `--state-dir`） | `<数据根>/<Harness 的目录名>` | 否 |
| `COVEY_PROFILE` | `backends/codex/bridge.py`、`backends/dsh/bridge.py`、`backends/pi/bridge.py`、`backends/agy/bridge.py`（Bridge 的 `--profile`） | 空：lark-cli 的默认 profile | 否 |
| `COVEY_MESSAGE_FORMAT`、`LARK_CLAUDE_BRIDGE_MESSAGE_FORMAT`、`LARK_DSH_BRIDGE_MESSAGE_FORMAT`、`LARK_PI_BRIDGE_MESSAGE_FORMAT`、`COVEY_AGY_MESSAGE_FORMAT` | `feishu/messenger.py`（各 Harness 发消息时读） | `markdown` | 否 |
| `COVEY_MESSAGE_CHUNK_LIMIT`、`LARK_CLAUDE_BRIDGE_MESSAGE_CHUNK_LIMIT`、`LARK_DSH_BRIDGE_MESSAGE_CHUNK_LIMIT`、`LARK_PI_BRIDGE_MESSAGE_CHUNK_LIMIT`、`COVEY_AGY_MESSAGE_CHUNK_LIMIT` | `feishu/messenger.py` | 12000 | 否 |
| `COVEY_CARD_TRANSPORT` | `feishu/sdk.py`（`enabled()`） | 空：lark-cli；`sdk` 走 SDK | 否 |
| `COVEY_EVENT_TRANSPORT` | `feishu/ws_intake.py`（`enabled()`） | 空：lark-cli 的事件消费者；`sdk` 走 SDK 长连接 | 否 |
| `COVEY_FEISHU_DIRECT` | `feishu/sdk.py` | 开：直连飞书，`0` 走代理 | 否 |
| `LARKSUITE_CLI_DATA_DIR` | `feishu/sdk.py`（找 lark-cli 的凭据） | `~/.local/share` | 否 |
| `COVEY_OUTGOING_MEDIA_DIRS` | `feishu/outgoing_images.py` | 空 | 否 |
| `COVEY_ACCEPT_CHATS` | `feishu/web_service.py`（验收页面，`accept_chats`） | 空：验收页面不打开、不操作任何会话。做真机验收时写验收用的会话标题，逗号分隔，设在起验收服务的那个进程的环境里 | 否 |
| `COVEY_FEISHU_WEB_HOST` | `feishu/web_client.py` | 扩展的 `web_tenant_domain`，没有时 `open.feishu.cn` | 否 |
| `PLAYWRIGHT_BROWSERS_PATH` | `feishu/web_client.py`（验收页面的浏览器）、`feishu/app_console.py`（开发者后台的浏览器）、`feishu/install_browser.py`（`./install` 的浏览器会话）、`core/browser_env.py`（定位与 `launch.json`） | `<数据根>/browsers`（存在时），否则 `~/.cache/ms-playwright`（`./install` 设成 `<数据根>/browsers`） | 否 |
| `COVEY_BROWSER_EXECUTABLE` | `feishu/web_client.py`（`launch`）、`core/browser_env.py`、`service/browser_setup.py` | 空：用 Playwright 下载的 Chromium；`./install` 发现机器上已有 Chrome/Chromium 时写进 `<数据根>/browsers/launch.json` | 否 |
| `LD_LIBRARY_PATH` | `core/browser_env.py`、`service/browser_setup.py`（无 root 缺 Chromium 系统库时，把 `<数据根>/browsers/syslibs/` 下解出的库目录加在前面，只给浏览器进程） | 不动 | 否 |
| `TMPDIR` | `service/browser_setup.py`（监视浏览器下载有没有进展的临时目录；`./install` 设成 `<数据根>/tmp`） | `<数据根>/tmp` | 否 |
| `ANTHROPIC_BASE_URL` | `app/redaction_panel.py`（「探测」）、`backends/claude/redaction.py`（脱敏中继的上游） | 空 | 否 |
| `COVEY_PINNED_OVERVIEW` | `app/overview.py`（`app/pins.py` 的 `laid` 也读：两个开关都开时看板记录的规则才算数） | 关；`1` 开 | 否 |
| `COVEY_HARNESS_PROBE` | `app/availability.py`（`PROBE_ENV`；探测本身在 `core/harness_probe.py`） | 关；开了才在可用性检查里对 Claude、Codex 各做一次真实调用（缓存 6 小时，失败 10 分钟），没登录时启动卡上不给这个 Harness | 否 |
| `COVEY_GROUP_ALL_MESSAGES` | `app/access.py`（`free_talk_on`；群聊卡 `app/group_manager.py` 经它） | 关；`1` 开（还要应用开通 `im:message.group_msg`） | 否 |
| `COVEY_DIGEST_TIMES` | `app/digest.py`、`core/app_manifest.py`（有它才要菜单「看板」）、`app/tidy_run.py`（`scheduled`：整理专项的定时也按这些时间） | 空：不发早晚汇总，整理专项也没有定时的一次 | 否 |
| `COVEY_KNOWLEDGE` | `core/knowledge.py`（`enabled`）、`core/butler.py`（`mcp_config` 交给管家的 MCP 服务）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此列知识工具） | 空：不开知识条目 | 否 |
| `COVEY_HISTORY_CMD` | `core/knowledge.py`（`history_cmd`）、`core/butler.py`（`mcp_config` 同上）、`core/butler_tool_specs.py`（`listed`：据此列 `history_search`） | 空：没有历史搜索；要绝对路径 | 否 |
| `COVEY_EXTENSIONS` | `core/extensions.py` | 空：读仓库根和数据根的 `extensions/enabled`；设成空串表示一个都不要。逗号分隔的 Python 包名，见[扩展](architecture.md#扩展) | 否 |
| `COVEY_EXTENSION_PATH` | `core/extensions.py` | 空：仓库根的 `extensions/`。`os.pathsep` 分隔的目录，扩展包在这些目录里找 | 否 |
| `COVEY_DEVICES` | `core/devices.py`（`enabled`） | 空：关；`1` 才读 `devices.json`，见[多设备](#多设备开发中默认关) | 否 |
| `COVEY_DEVICES_FILE` | `core/devices.py`（`config_path`） | 空：`<数据根>/devices.json` | 否 |
| `COVEY_NODE_LISTEN` | `core/nodes.py`（`enabled`、`listen_spec`）、`app/node_gateway.py`、`app/nodes.py` | 空：关；设了就在这里等节点连上来（`[::]:端口`、`IPv4:端口` 或端口，`$变量` 取环境，`core/listen.py` 的 `parse_bind`），起 `nodes-gateway`、`nodes-alerts`、`nodes-local` 三个后台线程，见[节点与机器状况](#节点与机器状况默认关) | 否 |
| `COVEY_NODE_ADDRESS` | `app/node_gateway.py`（`hub_address`） | 空：本机默认路由的 IPv6 地址加监听端口（`core/listen.py` 的 `link_host`；没有 IPv6 时用能解析的完整主机名）；设备连不到这个地址时写成 `主机:端口` | 否 |
| `COVEY_NODE_AUTOSTART` | `core/node_agent.py`（`install_autostart`，设备上读） | 空：登录钩子、systemd 用户服务、launchd 有哪个装哪个；`hook`：只装登录钩子（测试用临时 HOME 时） | 否 |
| `COVEY_BUTLER_RULES`（旧名 COVEY_MAIN_BUTLER、LARK_BRIDGE_MAIN_BUTLER） | `app/prompts.py`（`butler_on`）、`core/butler.py`（`mcp_config` 交给管家的 MCP 服务）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此换 `topic_start` 的工具说明） | 空：关；`1` 把管家规则的前两条换成管家规则（只快答和分派），`topic_start` 的工具说明同步 | 否 |
| `COVEY_DISPATCH_CARD`（旧名 COVEY_MAIN_DISPATCH_CARD、LARK_BRIDGE_MAIN_DISPATCH_CARD） | `app/dispatch_cards.py`（`enabled`；`app/butler_topics.py` 的 `dispatch_card` 和 `app/relay_send.py` 的 `dispatch_note` 据此调用，`threads()` 据此起 `dispatch_cards` 后台线程） | 空：关；`1` 开派发卡：管家派出话题后发一张活卡片，刷新到结论回带后缩成一行 | 否 |
| `COVEY_STEER_URGENT_SECONDS` | `app/steer_interrupt.py`（`urgent_seconds`，任务进程和 Bridge 都读） | `20`：急的补充（带“马上”“立刻”“停”“过期”这类词）送达这么多秒还没被读到，就停掉模型正在等的那条命令（推送、部署这类不停），见[架构 · 补充投递](architecture.md#补充投递)；`0` 关自动打断，回执卡上的“打断它，马上读”照常 | 否 |
| `COVEY_SOURCE_WATCH` | `app/source_watch.py`（`enabled`：`note` 据此登记主人的消息，`threads()` 据此起 `source-watch` 后台线程） | 开：主人撤回或编辑了变成任务或补充的消息，任务随之停下或收到改后的内容（见[架构 · 撤回与编辑](architecture.md#撤回与编辑)）；`0` 关：不读回消息，和原来一样按收到时的原文做 | 否 |
| `COVEY_BUTLER_TRACKS`（旧名 COVEY_MAIN_TRACKS、LARK_BRIDGE_MAIN_TRACKS） | `core/butler_tracks.py`（`enabled`；`core/butler.py` 的 `is_butler_topic` 据此把槽 1 到 4 的话题也算管家；`app/butler_tracks.py` 的 `Tracks.enabled` 再加上管家允许、模式是管家 才分流） | 空：关；`1` 开[同时做几件事](butler.md#同时做几件事开关默认关)：家族里有任务在做时，主人的下一句先按规则（`core/track_route.py`）和小模型分流，并进某件在做的事、停止或新开一件（槽 1 到 4）。关着时消息的走法和原来一样 | 否 |
| `COVEY_PROJECTS` | `core/projects.py`、`core/butler.py`、`core/placement.py`、`core/board_read.py`、`core/butler_tool_specs.py` | 空：关（`./install` 新装的服务写 `1`）；`1` 开专项和负责人的运行（入口是管家直接开，和 `COVEY_PROJECT_UPGRADE` 的转成专项）。关着时谓词全为假，放置表和看板和原来一样，不建记录。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_BUTLER_TRACKS_MAX`（旧名 COVEY_MAIN_TRACKS_MAX、LARK_BRIDGE_MAIN_TRACKS_MAX） | `core/butler_tracks.py`（`limit`） | 3：同时在跑的件数上限，取值限制在 2 到 4，读不出来按 3 | 否 |
| `COVEY_BUTLER`（旧名 COVEY_MAIN_THREAD、LARK_BRIDGE_MAIN_THREAD） | `core/butler.py` | 允许（模式看 `main-thread.json`）；`0` 不允许 | 否 |
| `COVEY_TRACK_JUDGE` | `app/track_judge.py`（同时做几件事里，规则没能定的新消息交给小模型判断是并进哪件在做的事还是新开一件；`app/butler_tracks.py` 的 `Tracks.decide` 调用，`COVEY_BUTLER_TRACKS=1` 才会问） | `claude`（默认）：Claude Code 的 Haiku；`off` 不问，直接新开；`cmd:/absolute/path` 用自己的命令（标准输入一段 JSON，标准输出一行 JSON）。超时、出错、拿不准、脱敏话题都按新开一件 | 否 |
| `COVEY_TRACK_JUDGE_MS` | `app/track_judge.py` | 3000：小模型硬上限（毫秒，200 到 10000），超过按新开一件 | 否 |
| `COVEY_TRACK_JUDGE_WARM` | `app/track_judge.py`（`warm_enabled`；`Tracks.handle` 经 `warm` 调用，进程在 `app/track_standby.py`） | 空：开，收到管家消息时提前起好一个待命的 Claude Code 进程（空闲 10 分钟或 Bridge 停止时结束）；`0`、`off`、`false`、`no` 关，每次现起，慢约 1 秒 | 否 |
| `COVEY_TRACK_JUDGE_HEDGE` | `app/track_judge.py`（`hedge`；`warm` 据此多留几个待命进程，`judge` 同时问它们，进程在 `app/track_standby.py`） | 2：同一问同时交给几个待命进程（1 到 3），第一个能用的答案算数；只在待命开着、通道是 `claude` 时有用 | 否 |
| `COVEY_TRACK_JUDGE_SLIM` | `app/track_judge.py`（`slim_enabled`，给小模型的 Claude Code 命令行多两个选项） | 空：开，小模型的 Claude Code 带 `--setting-sources user --disable-slash-commands`，不读项目设置和技能清单；`0`、`off`、`false`、`no` 关 | 否 |
| `COVEY_BUTLER_STATE`（旧名 COVEY_MAIN_STATE、LARK_BRIDGE_MAIN_STATE） | `core/butler_mcp.py`、（管家任务的 MCP 配置写进去，告诉工具服务 Bridge 的状态目录） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_BUTLER_JOB`（旧名 COVEY_MAIN_JOB、LARK_BRIDGE_MAIN_JOB） | `core/butler_mcp.py`（管家任务的任务键，Bridge 据此核对调用） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_PROJECT_LEAD` | `core/butler.py`（`mcp_config`：只在专项负责人的任务里把专项 id 写进 MCP 配置，要 `COVEY_PROJECTS=1`）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此只列负责人的工具） | 无；设了就列 `topic_read`、`topics_search`、`workspaces_list` 加 `project_*`，其余管家工具都不列 | 否 |
| `COVEY_PROJECT_AUTOWAKE` | `core/projects.py`（`autowake_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_loop.py`（`ProjectLoop` 据此决定哪些回执唤醒负责人、要不要查停滞） | 空：关，只有专项的第一条回执唤醒负责人；`1`：话题一轮结束、主人在专项的话题里说话、停滞提醒也唤醒，并开停滞检查。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_PROJECT_MEMORY` | `core/projects.py`（`memory_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_context.py`（专项的话题开工带上记忆和资料库索引、主人答专项的问题卡记一条决定、主人发给负责人的文件和各话题的产物、交付进资料库）、`app/project_lead.py`（`library_index`：状态包的资料库）、`app/runs.py`（`bridge_rules`、`_with_rules` 取专项块） | 空：关，专项的话题开工不多带东西、不自动记、不收资料库；`1`：开工带上专项记忆和资料库索引，主人的回答记成决定，主人发的文件和话题的产物进资料库。见[专项记忆与资料库](projects.md#专项记忆与资料库) | 否 |
| `COVEY_MEMORY` | `core/memory.py`（`enabled`）；用它的：`app/memory_turn.py`（每轮那一块“记忆”）、`app/memory_collect.py`（话题一轮结束后核对 `memory`、`memory_void`）、`app/runs.py`、`app/knowledge_turn.py`、`app/project_context.py` | 空：关，规则、续接提醒和答案卡与没有这个功能时逐字相同；`1`：原则、知识条目、专项记忆、话题记忆拼成一块“记忆”，主人说的长期要求一轮结束后记下。见[记忆](#记忆) | 否 |
| `COVEY_PROJECT_TRIAGE` | `core/triage.py`（`enabled`，要 `COVEY_PROJECTS=1`）、`core/butler.py`（`mcp_config` 把它交给负责人的 MCP 服务）、`core/butler_tool_specs.py`（`lead_tools`）、`app/triage.py`、`app/project_loop.py`（`ProjectLoop.tick` 扫扣住的问题） | 空：关，专项的话题的问题直接发给主人；`1`：选择题先交给负责人，3 分钟内它能替主人答的就答，其余才发问题卡。见[专项的问题分流](projects.md#专项的问题分流) | 否 |
| `COVEY_PROJECT_BUDGET` | `core/projects.py`（`token_limit`）、`app/project_loop.py`（`hold`） | 空：没有上限，专项不会因为 token 停住；正整数：一个专项的 token 合计（负责人和各话题，缓存算在输入里）到了就停住（`state_reason` 是 `tokens`） | 否 |
| `COVEY_PROJECT_TOKENS` | `core/projects.py`（`token_limit`） | `COVEY_PROJECT_BUDGET` 的旧名，它没设时才读。名字里有 TOKEN，服务清单把它当成密钥不保存，重新 `launch` 带不进去：要设上限用 `COVEY_PROJECT_BUDGET` | 否 |
| `COVEY_PROJECT_ASK` | `core/projects.py`（`ask_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_loop.py`（`ProjectLoop.pause`）、`app/project_cards.py`（`ProjectCards.ask`、`ask_choice`） | 空：关，护栏停住的专项只在专项卡上写“停住 · 原因”；`1`：再发一张卡问 继续 / 收尾 / 停 | 否 |
| `COVEY_PROJECT_STALL_SECONDS` | `core/projects.py`（`stall_seconds`）、`app/project_loop.py`（`ProjectLoop.stall`） | 1800：专项有负责人能推动的事却这么多秒没进展，提醒负责人一次，再过同样久就停住 | 否 |
| `COVEY_PROJECT_IDLE_DAYS` | `core/projects.py`（`idle_days`）、`app/project_loop.py`（`ProjectLoop.rest`）、`app/project_cards.py`（`ProjectCards.fold`） | 7：专项没有要做的事、主人也这么多天没提它，就收起（专项卡缩成一行）；主人对它说话或管家交给它新的事时自动展开。可以写小数 | 否 |
| `COVEY_PROJECT_STYLE_KEEP` | `core/projects.py`（`style_keep_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_tools.py`（`project_style`）、`app/projects.py`（`seed_style`） | 空：关，新专项的汇报风格是默认值；`1`：负责人改过的风格记进 `<state>/project-style.json`，之后新开的专项从它开始 | 否 |
| `COVEY_PATROL` | `core/patrol.py`（`mode`）、`app/patrol.py`、`app/digest.py` | 空：关。`shadow`：只记不打扰；`on` 现在同 `shadow`；其他值都是关 | 否 |
| `COVEY_PATROL_ASK_MINUTES` | `core/patrol.py`（`number`） | 120：问题等主人这么多分钟算久（1 到 10080）；试用时调小，如 `3` | 否 |
| `COVEY_PATROL_UNSEEN_MINUTES` | `core/patrol.py`（`number`） | 360：做完的结果这么多分钟没看算久（1 到 10080） | 否 |
| `COVEY_PATROL_STALL_MINUTES` | `core/patrol.py`（`number`） | 180：在做的任务记录这么多分钟没变算没动静（1 到 10080） | 否 |
| `COVEY_PROGRESS_STUCK_MINUTES` | `core/progress.py`（`stuck_seconds`）、`core/longtask.py`（`TaskMonitor`） | 30：在跑的一轮这么多分钟没有任何动静就算“可能卡住了”：任务卡状态行、看板、汇总里这么写，状态行下出「看看」按钮。1 到 1440 | 否 |
| `COVEY_PATROL_FAILS` | `core/patrol.py`（`number`） | 2：同一个话题连着失败几次算（2 到 20） | 否 |
| `COVEY_PATROL_PROJECT_MINUTES` | `core/patrol.py`（`number`） | 720：专项有事没人动这么多分钟算（1 到 43200） | 否 |
| `COVEY_PATROL_TICK_SECONDS` | `core/patrol.py`（`number`） | 60：规则看一次记录的间隔，秒（5 到 3600） | 否 |
| `COVEY_PATROL_JUDGE_MINUTES` | `core/patrol.py`（`number`） | 30：小模型两次调用的间隔，分钟（1 到 1440）；试用时设 `1` | 否 |
| `COVEY_PATROL_DAILY_CALLS` | `core/patrol.py`（`number`） | 48：小模型一天最多调几次（1 到 1000） | 否 |
| `COVEY_PATROL_JUDGE_MS` | `core/patrol.py`（`number`） | 30000：小模型一次调用的硬上限，毫秒（1000 到 120000） | 否 |
| `COVEY_WEB` | `core/web_sync.py`（`enabled`）、`app/web_sync.py`（`WebSync.threads`） | 空：关。`1`：开网页工作台同步，`web-push`、`web-inbox` 两条线程；要同时设 `COVEY_WEB_CONFIG` | 否 |
| `COVEY_WEB_CONFIG` | `core/web_sync.py`（`load_config`） | 空。指向一个 JSON：`base_url`、`key_file`（只从这个文件读密钥）、`push_seconds`（默认 1）、`poll_seconds`（默认 1）；文件缺了或写错只记一行失败、退避重试 | 否 |
| `COVEY_PROJECT_TIDY` | `core/project_proposals.py`、`app/project_tidy.py`、`app/runs.py`、`core/butler.py`、`core/butler_tool_specs.py`、`app/web_sync.py`、`app/butler_panel.py` | 空或 `off`：关。`suggest`：只提议（能归进已有专项的直接归，新建要主人点）；`auto`：新建也直接做。`./install` 新装的服务写 `suggest`。见[整理专项](#整理专项) | 否 |
| `COVEY_PROJECT_TIDY_MODEL` | `core/project_proposals.py`（`model`）、`app/project_sorter.py`（`ask`） | `sonnet`：补话题要点和整理判断用的 Claude Code 模型别名或 id；写错按默认 | 否 |
| `COVEY_PROJECT_TIDY_DAYS` | `core/project_proposals.py`（`number`） | 7：整理看最近几天有过一轮的话题（专项里的话题不论多久都看） | 否 |
| `COVEY_PROJECT_TIDY_AGE_DAYS` | `core/project_proposals.py`（`number`） | 3：话题开了这么多天后又有一轮结束，算一次实时时机（每个话题一次） | 否 |
| `COVEY_PROJECT_TIDY_WORDS` | `core/project_proposals.py`（`number`） | 15：主人在话题里说到这么多句（他开的轮数），算一次实时时机（每个话题一次） | 否 |
| `COVEY_PROJECT_TIDY_MIN_TOPICS` | `core/project_proposals.py`（`number`）、`app/tidy_realtime.py`（`Realtime.handle`）、`app/tidy_run.py`（`start`） | 5：聊天里的话题少于这个数时不整理（实时、定时、手动都不） | 否 |
| `COVEY_PROJECT_TIDY_JUDGE_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`judge`） | 90：一次判断调用的硬上限，秒；超了这次判断作废，什么都不改 | 否 |
| `COVEY_PROJECT_TIDY_READ_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`backfill`） | 60：补一个老话题的要点那次调用的上限，秒；超了只跳过这个话题 | 否 |
| `COVEY_PROJECT_UPGRADE` | `app/projects.py`（`mode`：升级、`Projects.consider`；要 `COVEY_PROJECTS=1` 和 `COVEY_DISPATCH_CARD=1`） | 空或 `off`：不转成专项；`butler`：管家转话时带了 `project`，派发卡就静默转成专项（`word` 是旧值，同 `butler`）；`auto`：再加该话题来回到第 3 轮。同时最多 3 个活专项 | 否 |
| `COVEY_PINNED_TOPICS` | `core/topic_seen.py`（看板 `app/pins.py` 和已读消费者 `app/topic_seen.py` 都经它） | 关；`1` 开（还要 `COVEY_PINNED_OVERVIEW=1`） | 否 |
| `COVEY_PROXY_PROFILE` | `app/proxy.py` | 空：代答不可用 | 否 |
| `COVEY_PROXY_GROUP_NAME` | `app/proxy.py` | `代答`：第一次开启代答时新建的群叫这个名字；已建的群按群 ID 找，名字照旧 | 否 |
| `COVEY_OWNER_SEND` | `app/owner_send.py`（`enabled`） | 开（有代答发送器时）：话题以主人身份发消息走代发草稿卡；`0` 关，见[代发](proxy.md#代发) | 否 |
| `COVEY_APP_CHECK` | `app/app_setup.py` | 开：检查飞书应用、缺了发卡；`0` 关 | 否 |
| `COVEY_PLAYWRIGHT_PYTHON` | `feishu/app_config.py`（`playwright_python`：自动补齐用的解释器） | `<数据根>/proxy-web/venv/bin/python` | 否 |
| `COVEY_CONSOLE_URL` | `app/app_setup.py`（交给 `feishu/app_console.py` 的 `--console`） | `https://open.feishu.cn`（登录后跟着跳到租户的域名） | 否 |
| `COVEY_LOCK_ROOT` | `core/file_lock.py`、`service/install.py` | 不设：不用本机运行锁（见[本机运行锁](runtime-locks.md)） | 否 |
| `COVEY_MANAGED` | `app/unified.py` | Manager 启动 Bridge 时设成 `1`，不要手设 | 否 |
| `COVEY_REQUIRED_MOUNT` | `service/manager.py` | 开机守护设置，不要手设 | 否 |
| `MERLIN_JOB_ID` | `service/boot.py` | 开发机自带 | 否 | <!-- privacy: allow 平台自带的环境变量名，boot.py 按它认开发机 -->
| `COVEY_RUNNER_PYTHONPATH` | `app/runner.py` | Bridge 交给任务进程，不要手设 | 否 |
| `COVEY_TASK_DIR` | `app/leftovers.py`、`core/device_link.py` | 任务进程给本轮命令设置，不要手设 | 否 |
| `CODEX_HOME` | `core/paths.py`（`codex_home`，Codex 的会话目录和 Pi 借用的登录都从它取）、`core/model_settings.py` | `~/.codex` | 否 |
| `CLAUDE_CONFIG_DIR` | `backends/claude/sessions.py`、`backends/claude/redaction.py`、`core/maintenance_rules.py` | `~/.claude` | 否 |
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
| `--model`、`--reasoning-effort`、`--pi-cmd`、`--pi-timeout` | `backends/pi/bridge.py` | 见 `--help` | 否 |
| `--model`、`--reasoning-effort`、`--agy-cmd`、`--agy-timeout`、`--no-skip-permissions` | `backends/agy/bridge.py` | 空（agy 自己的默认模型）、空、`agy`、0、关 | 否 |

`scripts/deploy_bridge.sh` 自己的参数不在这张表里（表只列 Bridge 读的）：`--ext-repo`、`--ext-commit`、`--allow-core-mismatch` 让它把核心检出和扩展检出拼成一个 release，不给时只部署 `--repo` 一个检出，`--ext-commit` 默认 `HEAD`；`--allow-rollback` 让它部署不包含现网提交的版本（有意回滚），`--allow-extension-checks` 放行没过的扩展自检（`pre.extension_checks`），另一个部署正在跑时它退出码 3、不部署；用法见[部署新版本](deploy.md#部署新版本)。

`harnesses.json` 里除了 `args` 还读每个 Harness 的 `models_first`（`app/harnesses.py` 的 `models_first`，`app/unified.py` 读入，`app/launch_models.py` 的 `first_models` 排序；不写时顺序不变），见 [`--harness-config` 文件](harnesses.md#--harness-config-文件)。扩展的 Harness 另有的参数和档位文件见扩展自己的 README。

其他程序的参数，用法见各自的 `--help` 和对应文档：

| 参数 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `--root`、`--state-dir`、`--workdir`、`--profile`、`--interval`、`--health-timeout`、`--log` | `service/manager.py`（`bin/covey_service.py`） | 见 `--help` | 否 |
| `--root`、`--mode`、`--activate`、`--label`、`--status`、`--uninstall`、`--no-autostart` | `service/install.py` | 见 `--help`（`--mode` 有 `auto`、`systemd`、`launchd`、`cron`、`manager`，扩展可加；`auto` 在没有 systemd/launchd 时给 crontab 加一行 `@reboot`，`--no-autostart` 跳过；`--label` 是 launchd 标签，默认 `com.covey.manager`；公司托管模式的 `--persistent-virtio-pfs` 见扩展 README） | 否 |
| `--root`、`--runtime-only`、`--skip-events`、`--probe-harnesses` | `service/doctor.py` | 见 `--help` | 否 |
| `--state-dir`、`--app-id` | `service/pairing.py` | 见 `--help` | 否 |
| `--config`、`--data-dir`、`--dry-run`、`--duration`、`--kill`、`--parent-pid`、`--target`、`--as-mib`、`--alert-levels-mib`、`--rg-guard`、`--search-kill-mib` | `service/memguard.py` | 见[内存看门狗](operations.md#内存看门狗) | 否 |
| `--state-dir`、`--apply`、`--dry-run`、`--destination`、`--restore`、`--verify` | `core/maintenance.py` | 见 [备份、归档与维护](operations.md#备份归档与维护) | 否 |
| `--legacy-root`、`--new` | `core/paths.py` | 见 `--help` | 旧 `.local-state` 退役后删 |
| `--state-dir` | `core/workspaces.py` | 见 `--help` | 否 |
| `--json` | `service/plugins.py`（`bin/covey_plugins.py`） | `list` 的 JSON 输出；子命令 `list`、`enable`、`disable`、`doctor` 见[扩展](architecture.md#扩展)的「装和卸」 | 否 |
| `--state-dir`、`--no-probe`、`--json` | `service/device_check.py`（`bin/covey_devices.py`） | 见 `--help`；见[多设备](#多设备开发中默认关)的「自检」 | 否 |
| `--root`、`--extra-root` | `backends/codex/workspace_reader.py` | 见 `--help` | 否 |
| `--home`、`--start`、`--idle`、`--max`、`--dy` | `feishu/web_client.py`（`bin/covey_feishu_web.py`） | 见 [浏览器服务](proxy.md#浏览器服务) | 否 |
| `--app-id`、`--home`、`--plan`、`--login-from`、`--console`、`--login-seconds` | `feishu/app_console.py`（`bin/covey_app_console.py`） | 见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 否 |
| `--profile`、`--app-id`、`--review`、`--json`、`--python`、`--login-from`、`--work` | `service/app_check.py`（`bin/covey_app_setup.py`） | 见[自动补齐飞书应用](feishu-app.md#自动补齐飞书应用) | 否 |
| `--reconfigure`、`--skills`、`--non-interactive`、`--data-home`、`--workdir`、`--keep-login`、`--no-browser`、`--no-autostart`、`--profile`、`--app-name`、`--no-start`、`--browser-login-dir`（验收用，隐藏，见[运维](operations.md#服务托管)） | `service/installer.py`（`./install`、`bin/covey_install.py`） | 见[安装](install.md#选项) | 否 |
| `--home`、`--console`、`--shots` | `feishu/install_browser.py`（`bin/covey_install_browser.py`，`./install` 起的浏览器会话） | 见[安装](install.md#它做了什么) | 否 |

## 已下架项与旧配置

已下架的 Harness 和供应商、不再读取的变量和参数、旧菜单键、旧卡片和旧安装方式现在会怎样，见[已下线的功能与旧配置](legacy.md)。只升级老部署时才用得到。

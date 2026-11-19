# 配置

本文写给部署机器人的人：机器人从哪几处读配置、每个可选功能怎么打开、全部环境变量和参数，以及旧配置还能不能用。服务怎么注册、部署和维护见[运维指南](operations.md)，排障见[排障](troubleshooting.md)，各 Harness 的参数见 [Harness 说明](harnesses.md)。

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
| `personal_groups` | `[]` | chat_id 列表：这些群是主人的个人空间，只服务主人（含 `owner_accounts`），不会被激活。个人群里：主人不用 @ 机器人（飞书要把群里全部消息发给机器人，应用需有 `im:message.group_msg` 权限；没有时仍要 @）；顶层消息交给该群自己的管家（话题、回带、问题卡都在群里；管家按 chat 分开记录，不和私聊的混）；机器人置顶一张菜单卡（用量、看板、设置），点击仍只认主人；手上的事、话题看板、早晚汇总各自有一份（记录名带 chat_id，见 `docs/butler.md`）；答案卡顶上带状态行，和私聊一样 |

- 旧键 `owner_only`（取反即 `allow_team_spaces`）、`owner_aliases`、`home_chat`、`agent_groups` 仍可读，各映射到上表的字段，`spaces` 里写了同名字段时以它为准；启动日志每个旧键记一行 `… is deprecated: write spaces.…`。`group_menu`、`group_boards`、`owner_headed` 不再是开关：个人群自带这些，旧键只读入，启动日志各记一行说明。手上的事、看板、早晚汇总仍照原来的环境变量（`COVEY_PINNED_OVERVIEW`、`COVEY_PINNED_TOPICS`、`COVEY_DIGEST_TIMES`）开关。
- `owner_accounts` 里的账号私聊机器人时也走管家：每个账号的私聊记在 `<state>/owner-chats.json`（open_id → chat_id），不覆盖主人本人的 `owner-chat.json`；它点机器人菜单，回在它自己的私聊里。
- `<state>/spaces/<sha256(chat_id)>.json` 记每个个人群的类型和标题（只给设置卡「群聊」页显示，不参与判断）；设了 `spaces` 段（或旧键）后「群聊」页每行带空间类型标签，个人群也列出，没有启用/停用按钮；没有这一段时那一页和以前一样。菜单卡的记录在 `<state>/group-menu.json`。
- `notify_space` 是个人群时，它的看板就是默认那一份（记录不带 chat_id），不再另起一份。
- 类型不对（比如 `allow_team_spaces` 不是布尔，`notify_space` 不是 chat_id）时启动报错。
- 谁被服务只由 `app/access.py` 判断：`is_owner`、`admit`、`allows`；通知去向是 `Access.notify_chat()`；一个 chat 是什么空间是 `Access.space_of()`。

## 服务清单里的环境变量

`launch` 每次都从执行它的终端取下面这些变量，整组替换清单里的旧值：

| 类别 | 变量 |
| --- | --- |
| 保存：固定白名单 | `PATH`、`LANG`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`DSH_HOME`，以及 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 和它们的小写形式 |
| 保存：本项目的设置 | 所有 `COVEY_*` 和 `LARK_<名称>_BRIDGE_*`，例如 `COVEY_DATA_HOME`、`COVEY_CARD_TRANSPORT` |
| 不保存 | `COVEY_MANAGED`（Manager 启动 Bridge 时设置）、`COVEY_REQUIRED_MOUNT`（开机守护设置）、`COVEY_SERVICE_ROOT`（也不传给 Bridge）；名字像凭据的（含 `SECRET`、`TOKEN`、`PASSWORD`、`CREDENTIAL`，或以 `KEY` 为一段）；其他所有变量 |

- **关掉一个开关，光在终端里去掉变量不够。** Bridge 的环境是 Manager 自己的环境加清单（见下），而 Manager 是第一次 `launch` 时从那个终端拉起的（`ensure_manager`），带着当时终端里的变量一直跑下去；之后的 `launch` 只换清单，Manager 不退出，它自己环境里的旧开关就还在，新 Bridge 照旧带着（`service_environment` 只覆盖清单里有的名字）。二选一：① 在终端里把变量设成关闭值（如 `COVEY_BUTLER_TRACKS=0`），重新 `launch`，清单里的值盖过 Manager 的；只对“不是 `1` 就关”的开关有效，别的开关看各自一节写的取值；② 先结束 Manager 再 `launch`：`python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["process"]["pid"])' "$SERVICE_ROOT/manager.json"` 取 pid，`kill -TERM -- -<pid>` 结束它的进程组（Manager 在自己的会话里，进程组号就是它的 pid；Bridge 和任务进程各在自己的会话里，不受影响，下一个 Manager 接管），用 `pgrep -g <pid>` 确认没有残留，然后在没有该变量的终端里 `launch`。托管方式（systemd 等）拉起的 Manager 不带终端变量，重启单元即可，见[部署新版本](operations.md#部署新版本)第 6 步。
- 终端里没有的变量，清单里也就没了。在没有代理变量的终端里重新注册，清单里的代理就会丢；终端里多出的 `LARK_*BRIDGE_*` 也会被保存。重新注册前先对比，见[部署新版本](operations.md#部署新版本)第 3 步。
- 变量值不能含换行。代理 URL 只能是 `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h`，要有主机名，不能带用户名、密码、路径、查询参数或片段（`NO_PROXY` 不校验）。不符合时 `launch` 拒绝写入，报 `only explicit non-secret runtime paths/settings can be persisted` 或 `only non-secret proxy URLs without credentials can be persisted`。带凭据的代理不能托管。
- Bridge 进程的环境是 Manager 自己的环境，加上清单里的变量，再加 `PYTHONDONTWRITEBYTECODE=1`、`COVEY_MANAGED=1` 和 `TMPDIR=<state>/tmp`。开机后 Manager 由开机守护拉起，不读交互 shell 的初始化文件，所以 `PATH`、代理和本项目的设置只能靠清单。`restart` 不会补上清单里缺的变量，要重新 `launch`。
- 常用的变量：`COVEY_PROFILE`（Bridge 的 lark-cli profile，所有 Harness 共用）、`COVEY_OUTGOING_MEDIA_DIRS`（回复里本地 Markdown 图片额外查找的目录，用 `:` 分隔）。

**不再读取的变量。** 下面这些以前给后端参数设默认值（扩展的 Harness 另有，见扩展的 README），2026-11-10 起不再读取，后端用字面默认值（`--help` 里的那个）。参数改写在 Codex 的服务命令行或其余 Harness 的 `harnesses.json`：

- Codex：`LARK_BRIDGE_CODEX_{MODEL,REASONING_EFFORT,SANDBOX,CMD}`、`LARK_BRIDGE_TIMEOUT` → 服务命令行 `--model`、`--reasoning-effort`、`--sandbox`、`--codex-cmd`、`--codex-timeout`；
- Claude：`LARK_CLAUDE_BRIDGE_{MODEL,EFFORT,CLAUDE_CMD,TIMEOUT,SKIP_PERMISSIONS,LOGIN_SHELL}` → `--model`、`--effort`、`--claude-cmd`、`--claude-timeout`、`--no-skip-permissions`、`--claude-login-shell`；
- `LARK_DSH_BRIDGE_CMD`、`LARK_PI_BRIDGE_CMD` → `--dsh-cmd`、`--pi-cmd`。

服务清单会把终端里的它们存下来，所以老部署重启后还带着，只是不生效。Bridge 启动时对每个设了的变量在 `bridge.log` 记一行 `env <名字> is no longer read; put <参数> in …`（`core/ignored_env.py` 的 `IGNORED_ENV`）。在没有它们的终端里重新注册（[部署新版本](operations.md#部署新版本)）就清掉了。

其中两个以前用来收紧权限，不再读取后会悄悄放宽，所以设了非默认值时直接拦下（`core/ignored_env.py` 的 `loosened`）：`LARK_BRIDGE_CODEX_SANDBOX` 不是 `danger-full-access`（Codex 会按 `danger-full-access` 跑），`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 是 `0`/`false`/`no`/`off`（Claude 会跳过授权）。这时 `bridge.log` 的那一行写明会怎样，`bin/covey_doctor.py` 把这个服务报为 `degraded`、带 `permission_env` 并非 0 退出，`scripts/deploy_bridge.sh --check-only` 的预检 `permission_env` 失败（清单和当前终端里的都查），`scripts/bootstrap_harness_bridge.sh` 拒绝注册。改法见[权限变量让诊断或部署失败](troubleshooting.md#权限变量让诊断或部署失败)。

非默认 Harness 总是用 Bridge 的 `--profile` 和 `--state-dir`，所以 `LARK_CLAUDE_BRIDGE_PROFILE` 等和非默认 Harness 的 `<前缀>_STATE_DIR` 设了也不起作用。

## 可选开关

只有部分部署要用的功能默认关闭：没配置时不起后台线程、不加菜单项、不改已有行为。打开的办法都是在注册服务的终端里设变量，再重新 `launch`。关掉时不能只在终端里去掉变量，见[服务清单里的环境变量](#服务清单里的环境变量)第一条。

| 功能 | 怎么打开 | 打开后多什么 |
| --- | --- | --- |
| [代答](#代答) | `COVEY_PROXY_PROFILE` 和发送工具 | `proxy`、`proxy-in` 后台线程；设置卡「代答」能用 |
| [手上的事与话题看板](#手上的事与话题看板) | `COVEY_PINNED_OVERVIEW=1`、`COVEY_PINNED_TOPICS=1` | `overview` 后台线程；Pin 话题时多一个已读事件消费者 |
| [早晚汇总](#早晚汇总) | `COVEY_DIGEST_TIMES` | `digest` 后台线程、菜单「看板」 |
| [知识条目与历史搜索](#知识条目与历史搜索) | `COVEY_KNOWLEDGE=1`；`COVEY_HISTORY_CMD=/absolute/path/to/kb-history` | 管家多 `knowledge_*` 工具，话题首轮带知识条目，汇总多一节；管家多 `history_search`，话题规则多一行查历史的命令 |
| [记忆](#记忆) | `COVEY_MEMORY=1` | 每轮规则里原则、知识条目、专项记忆合成一块“记忆”；话题里主人明说的长期要求在一轮结束后记下，答案卡底部露一行和“不对”按钮 |
| [管家](#管家) | 默认允许；`COVEY_BUTLER=0` 不允许 | `main-tools`、`main-search` 后台线程；设置卡的私聊、新话题标签 |
| [管家规则](#管家规则) | `COVEY_BUTLER_RULES=1` | 管家只快答和分派，不在自己这一轮里干几分钟的活；其余规则不变 |
| [专项（第 2 期，开发中，默认关）](architecture.md#专项第-2-期开发中默认关) | `COVEY_PROJECTS=1` | 底座：`projects/` 记录、负责人话题和专项的话题的角色、看板并格，加负责人一轮的运行（规则、状态包、会话重建、`project_*` 工具、只留 `Read`/`Grep`/`Glob`）；入口是转成专项（`COVEY_PROJECT_UPGRADE=butler`：管家带 `project` 的派发卡静默转成专项）和管家直接开（`topic_start` 带 `project`）；续轮、问题分流是后面的包 |
| [整理专项](#整理专项) | `COVEY_PROJECT_TIDY=suggest`（要 `COVEY_PROJECTS=1`；`./install` 新装的服务默认写上这两个） | `project_tidy` 后台线程：每轮结束读话题要点、到时机就整理一次；管家多 `projects_tidy`，设置卡「私聊默认」多「整理专项」，网页发件箱认 `tidy`，网页推送的话题多 `gist`；提议卡、整理专项卡 |
| [专项停住后问主人](butler.md#专项停住后问主人开关默认关) | `COVEY_PROJECT_ASK=1`（要 `COVEY_PROJECTS=1`） | 专项被护栏停住时多发一张卡：继续 / 收尾 / 停；`COVEY_PROJECT_STYLE_KEEP=1` 另让新专项沿用上次设的汇报风格 |
| [同时做几件事](butler.md#同时做几件事开关默认关) | `COVEY_BUTLER_TRACKS=1` | 管家一轮在做时，下一句按规则和小模型并进某件在做的事或新开一件；`COVEY_TRACK_JUDGE_WARM` 没关时多一个待命的小模型进程，Bridge 停止时结束 |
| [我本来想找你的事](butler.md#我本来想找你的事开关默认关只记不打扰) | `COVEY_PATROL=shadow` | `patrol` 后台线程：每分钟用规则找候选，每 30 分钟让小模型筛，只写 `patrol/` 记录；汇总和「看板」多一块“我本来想找你的事”；不发消息、不动专项 |
| [网页工作台](butler.md#网页工作台开关默认关) | `COVEY_WEB=1` 加 `COVEY_WEB_CONFIG=/absolute/path/to/web.json` | `web-push`、`web-inbox` 后台线程：把专项、话题（含管家和专项负责人的对话）、一轮（含过程、步骤清单、用量）、问题推给妙搭网页，取回网页上的说话（含对管家、负责人说）、答题、停止、重开、记为看过、作废一条专项记忆 |
| [派发卡](#派发卡) | `COVEY_DISPATCH_CARD=1` | `dispatch_cards` 后台线程；管家派出话题后多一张聊天区的活卡片 |
| [群聊免@](#群聊免) | `COVEY_GROUP_ALL_MESSAGES=1` 加应用权限 | 设置卡「群聊」每个群多一个“免@” |
| [多设备（开发中，默认关）](#多设备开发中默认关) | `COVEY_DEVICES=1` 加 `devices.json` | 设备上能跑 Claude Code 和 Codex 的话题（经 ssh）；默认关，真机验收在进行。开了才有 `devices` 后台线程；关着，或开了但没有设备时，后台线程、文件和行为都同没有这个功能 |
| [节点与机器状况（默认关）](#节点与机器状况默认关) | `COVEY_NODE_LISTEN=<监听地址>` | `nodes-gateway`、`nodes-alerts`、`nodes-local` 后台线程：设备和本机各跑一个常驻节点，主动连 Hub，每 10 秒报一次 CPU、内存、盘、GPU 和占用最多的进程；内存、OOM、盘满或写不进、失联时发告警卡，恢复后原卡改成已恢复 |
| [飞书 SDK 与直连](#飞书-sdk-与直连) | `COVEY_CARD_TRANSPORT=sdk`；直连默认开 | 卡片和消息走常驻 SDK 客户端 |
| [自动补齐飞书应用](operations.md#自动补齐飞书应用) | 默认开；`COVEY_APP_CHECK=0` 关 | `app-setup` 后台线程 |
| [内存看门狗](operations.md#内存看门狗) | `bin/covey_memguard.py enable` | 独立进程；`memguard` 后台线程转发告警 |
| [本机运行锁](runtime-locks.md) | `COVEY_LOCK_ROOT` | 同机多个服务共用的锁 |

### 代答

[代答](usage.md#代答)要两样东西（实现见[代答](proxy.md)）：

- `COVEY_PROXY_PROFILE`：带主人本人登录的 lark-cli profile，要有 `im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`search:message`、`contact:user:search` 和 `offline_access`。用户 token 的刷新期是 7 天；轮询每分钟都在用它，正常会一直续上，代答关掉超过 7 天会过期。过期后 `lark-cli --profile <它> auth login` 重新授权。
- 发送器（扩展钩子 `proxy_sender`，见[发送器](proxy.md#发送器)）：没有发送器就没有代答入口。公司部署的发送器的安装与配置见扩展自己的 README。

代发（[话题以主人身份发消息](proxy.md#代发)）跟着发送器：有发送器就开，`COVEY_OWNER_SEND=0` 关掉。它用同一个 `COVEY_PROXY_PROFILE` 找收件人，另要 `contact:user:search`。

验收页面（`bin/covey_feishu_web.py serve|accept`，见[浏览器服务](proxy.md#浏览器服务)）另用一个装了 Playwright 的 Python，例如 `$COVEY_DATA_HOME/proxy-web/venv/bin/python`，代答发送不用它。主人的网页端登录只留 `<state>/proxy/web/profile`（0700）一份，不进备份：

- 别的工具要用就复制一份到自己的运行目录，不要直接打开它（同一个 profile 只能有一个 Chromium）。
- 复制前确认 `<state>/proxy/batches/` 里没有 `status` 为 `sending` 的批次，并排除 `Cache`、`Code Cache`、`Service Worker`、`GPUCache`、`Dawn*Cache`、`IndexedDB`、`Singleton*`。
- 它的缓存目录合计超过 200 MiB 时，维护后台线程（每小时看一次）在没有发送中的批次、没有进程用着它时删掉这些缓存（`core/maintenance.py` 的 `WEB_CACHES`），日志 `proxy web login caches removed: N MiB`。cookie 和登录不受影响。

代答的日志行见[排障 · 代答](troubleshooting.md#代答没有回复或没有发出)。

### 敏感词脱敏

脱敏的词表、开关和替换记录在设置卡的「脱敏」标签上（`app/redaction_panel.py`，卡片是 `cards/redaction_panel.py`），不用配置，所有部署都有；开了[网页工作台](butler.md#网页工作台开关默认关)时，它的设置页也能批量加删词。以前可选的独立网页看板（`COVEY_DASHBOARD`）2026-11-19 去掉了，见[已下架项与旧配置](#已下架项与旧配置)。

- 「脱敏」标签：一层（填词、搜索、词表，折叠面板里是开关和记录，试一下和上游地址的输入框点开再出现（折叠面板里放不了表单）；替身词一律自动生成：词表每项只有 `term`，读时忽略旧版本留下的 `stand_in`，写时不写，`core/migrate.py` 的 redaction-stand-in 把旧词表重写一遍；旧版本设的 `"anywhere": true` 照旧生效，`update_config` 拒绝新设），填词框一次加或删一批（“加入词表”“从词表删除”，按行，删除要整词一致；`core/redaction.py` 的 `edit_terms`，撤销是 `put_back`），开关一次一个（`update_config`）；都在 `EDIT_LOCK` 下读、改、存。点击改完词表、把结果记进设置卡记录就回执，整卡重画放在回执之后（`after_ack`；连点几下只画最后一次）。记录区要读最近 500 轮涉及的话题，`core/redaction_views.py` 的 `topic_info` 按 topic.json 的修改时间缓存。卡上不出现原词：词只写首字和字数（`masked`），自动替身不显示；按钮按 `<state>/redaction/card-key`（0600，第一次用时生成）下的 HMAC 指认哪一行；查找只记下找到的行，框里输入的内容不写回卡片；拒绝的原因（`Refused`）不提原词，词表文件读不了时只说读不了（原来的错误可能引用原词）。“撤销”只记在 Bridge 进程的内存里，重启后就没有了。「探测」在后台线程跑，跑完重画卡片。

- 词表在 `<state>/redaction/config.json`（0600），每轮开始时读，改动下一轮生效；每轮的替换次数记在 `<state>/redaction/turns.jsonl`。「探测」在后台跑一次 `<claude 命令> --gateway-status`，只显示结果、不改配置。

### 手上的事与话题看板

- `COVEY_PINNED_OVERVIEW=1`：在主人私聊里置顶一张「手上的事」汇总卡（见[手上的事](usage.md#手上的事)）。只认 `1`。主人的私聊取 `<state>/owner-chat.json`（主人最近一次私聊机器人时记下），还没有就不发。卡片记录在 `<state>/pinned-overview.json`。置顶用 `im pins create`，机器人发消息的权限就够。日志：`pinned overview sent|pinned|retired …`，失败是 `pinned overview not sent|pin failed|update failed`。
- `COVEY_PINNED_TOPICS=1`：在「手上的事」上面自动 Pin 话题（见[话题看板](usage.md#看板自动-pin-话题)）。只认 `1`，而且要 `COVEY_PINNED_OVERVIEW=1` 也开：由「手上的事」的后台线程管，它那张卡置顶之后才开始 Pin。最多 10 个话题（`app/pins.py` 的 `MAX_PINS`），顺序变了才动，最多每 60 秒一轮。私聊的 `im pins list` 读不到东西，Bridge 只信自己的记录 `<state>/pinned-topics.json`。`<state>/board-priority.json`（话题 → 时间）让话题在本组排最前，由管家的工具写。看板从不撤回消息。日志：`pinned board pin|unpin|move|dismissed message=… topic=… group=…`，失败是 `pinned board failed op=… failures=N`。
- 已读事件：开了 `COVEY_PINNED_TOPICS=1` 时 Bridge 多起一个消费者 `lark-cli event consume im.message.message_read_v1`（后台线程 `topic-seen`，`app/topic_seen.py`）。只认主人读的消息；每条在主人私聊的话题别名里查，查到的话题记下最近一次读到的时间，写进 `<state>/topic-seen.json`（90 天没读的和超出 2000 个的较早话题删掉）。开发者后台要订阅「消息已读」事件，应用要有 `im:message` 和 `im:message:readonly`；缺了消费者打印 `missing_scope` 后退出，每 300 秒重试，其余功能照常，健康检查不看它。

关掉：去掉变量后重新 `launch`。已置顶的卡和已 Pin 的话题留在原处，在私聊的 Pin 页里手动取消；记录文件留着不再更新。

### 早晚汇总

`COVEY_DIGEST_TIMES=09:30,23:30`：逗号分隔的 HH:MM，按 Asia/Shanghai，重复的只算一次，写错的跳过并记日志。每到一个时间点，Bridge 在主人私聊的顶层发一张汇总卡（`app/digest.py`、`cards/digest.py`；用户看到什么见[早晚汇总](usage.md#早晚汇总)）。不设或一个合法时间都没有就不起后台线程，也没有菜单「看板」。

- 记录在 `<state>/digest.json`：最后发出的时间点 `last_slot`、发送中的 `pending`、最近 6 张发出的卡 `sent`。一个时间点只发一次：Bridge 在时间点停着、60 分钟内起来就补发一次，超过就跳过。卡先存再发；没确认的 60 秒后用同一个幂等键再发，最多 3 次。
- 数据从记录读（`questions/`、`jobs/` 的索引），不持记录锁、不调模型，管家话题不列。
- 菜单「看板」（事件 key `digest_board`）只在设了这个变量时有：发同一张卡、按现在取数，不改 `last_slot`。卡的记录在 `<state>/digest-boards/`。要在开发者后台的机器人菜单里加这一项（自动补齐会加）。
- 日志：`digest sent slot=HH:MM`、`digest not sent: nothing to list`、`digest not sent try=N`、`digest not sent after N tries; given up`、`digest card redrawn message=… cleared=N`、`digest board sent`。

### 整理专项

`COVEY_PROJECT_TIDY`（要 `COVEY_PROJECTS=1`；用户看到什么见[整理专项](usage.md#整理专项)，机制见[管家 · 整理专项](butler.md#整理专项开关默认关装好就开只提议)）：

- `suggest`（只提议）：每轮结束时话题自己的模型更新话题要点；到实时时机、早晚汇总的时间点（`COVEY_DIGEST_TIMES`）、或主人手动叫时整理一次。能归进已有专项、把握很高的直接归，收起没事的专项也直接做；新建、合并、拆开、改名都发卡问，主人点了才做。
- `auto`：再加新建专项也直接做（专项卡上留「不要专项」撤回）。主人在提议卡上点了「以后直接建」，效果和 `auto` 一样。
- 不设、`off` 或别的值：关。没有后台线程、管家没有 `projects_tidy`、设置卡没有这一行、每轮的消息里没有话题要点那段，行为和原来一样。

仓库代码里不设就是关（AGENTS.md 规则 4）；`./install` 写服务清单时，终端里没设这两个变量就写上 `COVEY_PROJECTS=1`、`COVEY_PROJECT_TIDY=suggest`（`service/installer.py` 的 `INSTALL_SWITCHES`），已有的值不动。要关，在注册服务的终端里设 `COVEY_PROJECT_TIDY=off` 再 `launch`。

可调的数（都是正数，写错按默认）：`COVEY_PROJECT_TIDY_DAYS`（7，整理看最近几天的话题）、`COVEY_PROJECT_TIDY_AGE_DAYS`（3，话题跨进第几天算一次实时时机）、`COVEY_PROJECT_TIDY_WORDS`（15，主人在话题里说到第几句算一次）、`COVEY_PROJECT_TIDY_MIN_TOPICS`（5，聊天里的话题少于这个数时不整理）、`COVEY_PROJECT_TIDY_JUDGE_SECONDS`（90，一次判断调用的上限）、`COVEY_PROJECT_TIDY_READ_SECONDS`（60，补一个话题要点的上限）。补话题要点和判断用 Claude Code 的 `COVEY_PROJECT_TIDY_MODEL`（默认 `sonnet`：两者都要读整段对话、说出一件事的本质，小模型不够），走 Claude Harness 自己的包装，和多路并行的小模型判断同一套做法；没有 Claude Harness 时判断不了，整理卡写“没有可用的 Claude Code 来判断”。

### 记忆

- `COVEY_MEMORY=1`（只认 `1`）：打开“记忆”（[管家 · 记忆](butler.md#记忆开关默认关)）。不依赖别的开关：原则一直有，`COVEY_KNOWLEDGE=1` 时知识条目也进来，`COVEY_PROJECTS=1` 且 `COVEY_PROJECT_MEMORY=1` 时专项记忆也进来；话题自己的记忆放在 `<state>/topics/<话题>/memory.json`，打开后才会有。
- 关着（默认）：规则、续接提醒、答案卡和任务记录都和没有这个功能时一样，不读不写话题的 `memory.json`。
- 关掉：去掉变量、重新 `launch`；已经写下的记忆留着。
- 日志：`memory lines written=N of M job=#…`、`memory lines ignored reason=… job=#…`、`memory lines failed job=#…`。

### 知识条目与历史搜索

两个变量互相独立，都只在主人自己的部署里设；没设时不建目录、不改规则和提示词、管家的工具清单里没有对应的工具、汇总里没有这一节。设计和实现见[管家 · 知识库](butler.md#知识库)，主人怎么用见[使用指南 · 知识条目](usage.md#知识条目)。

- `COVEY_KNOWLEDGE=1`（只认 `1`）：打开知识条目。记录在 `<state>/knowledge/`；管家多 `knowledge_save`、`knowledge_update`、`knowledge_drop`、`knowledge_search`；每个话题的新会话带上条目，续接的会话只补上一轮之后新增或改动的；主人在话题里明确说“记一下 / 记住 / 记下来”时，Bridge 在那一轮结束后把检查点的 `remember` 记下来；早晚汇总多“新记的知识”。
- `COVEY_HISTORY_CMD=/absolute/path/to/kb-history`：历史搜索命令的**绝对路径**（相对路径等于没设）。设了以后，主人自己的话题规则里多一行“要查以前做过的事用 `<命令> search <一句话>`”，管家多 `history_search`（跑 `<命令> search [--workspace W] [--since Nd] -- <一句话>`，自己的进程组里最多等 70 秒，超时整组杀，输出最多 20 KB）。命令本身不在这个仓库。
- 关掉：去掉变量、重新 `launch`；已经写下的 `knowledge/` 留着。
- 日志：`knowledge remember written=N job=#…`、`knowledge remember ignored reason=… job=#…`、`knowledge remember failed job=#…`；管家工具的 `main tool name=knowledge_save …`。

### 管家

`COVEY_BUTLER` 决定部署允不允许管家（私聊模式“管家”，见[管家](butler.md#私聊模式)和[使用指南 · 管家](usage.md#管家)）：

- `0`：不允许。不起后台线程、不改消息路由，设置卡没有管家的标签。
- 不设或 `1`：允许。用不用由主人在设置卡上选的模式定（`<state>/main-thread.json` 的 `mode`），改模式不用重启。新部署第一次启动写成管家，日志 `main thread mode settled mode=agent|topics`。
- 管家的设置在 `<state>/main-thread.json`，工具请求在 `<state>/main-thread/requests/`。管家的会话只能用 Claude Code。
- `main-thread.json` 里旧的 `"desk_topics": true` 读到也按 `false` 处理，启动时记一行 warning，见[已下架项与旧配置](#已下架项与旧配置)。

### 管家规则

`COVEY_BUTLER_RULES=1`（只认 `1`）：管家的规则里，“只读的、一两分钟能做完的自己做完直接答”和“可能超过一两分钟的交给话题”这两条换成管家规则：几秒能答完的直接答，要查、要跑、要改、预计超过半分钟的用 `topic_start`、`topic_send` 派给话题，对主人只说一句放到哪个 T 号、大概多久。主人明确说“你直接做”时例外。`topic_start` 的工具说明也换成同样的说法（Bridge 把变量交给管家会话里的 MCP 服务，由它列工具）。其余规则一条不动；没设时每轮提示词和工具说明都和原来逐字节相同（`tests/golden/butler_prompt.txt`）。规则全文和旧会话什么时候用上新规则，见[管家 · 管家规则](butler.md#管家规则)。

- 打开：在注册服务的终端里设 `COVEY_BUTLER_RULES=1`，重新 `launch`。关掉：去掉变量、重新 `launch`。
- 不影响话题的规则、工具清单（只换 `topic_start` 的说明）和卡片；跑过 `CARD_AFTER`（90 秒）才出卡的兜底照旧，没有另设变量。

### 派发卡

`COVEY_DISPATCH_CARD=1`（只认 `1`）：管家用 `topic_start`、`topic_send`、`topic_fork` 把一件事交给话题后，在私聊的聊天区发一张**活卡片**（一条新消息，顶上“你问”块链到主人那条提问；不能回复那条提问，它下面已有话题串）：卡头是主人请求的前 30 字加状态（在做、等你、做完、失败、已停），一行灰字写 T 号、开始时间和用时，折叠面板“话题”写话题的标题、工作区和当前步骤，下面是输入/输出灰框，按钮是补充、停止、› 打开话题串。`dispatch_cards` 后台线程每秒读一次话题的任务记录，内容变了且离上次写超过 2.5 秒才整卡刷新一次。话题的结论回带发出后，卡缩成一行“✅ 做完 · 结论在下方 ›”（链到结论），之后不再刷新。说明见[管家 · 派发卡](butler.md#派发卡开关默认关)。

- 打开：在注册服务的终端里设 `COVEY_DISPATCH_CARD=1`，重新 `launch`。关掉：去掉变量、重新 `launch`；关着时不起后台线程、不发这张卡，话题的任务卡、回带和小卡（`hook_card`）和原来一样。
- 记录在 `<state>/main-thread/dispatch/`，7 天没动的由维护删。

### 群聊免@

`COVEY_GROUP_ALL_MESSAGES=1`：主人可以按群选谁说话不用 @ 机器人（见[在群里用](usage.md#在群里用)）。只认 `1`。

- 要先给应用开通 `im:message.group_msg`（获取群组中所有消息，敏感权限，可能要企业管理员审批）和 `im:chat.members:read`（选人时列群成员），并发布新版本。
- 设置记在 `<state>/groups/<…>.json` 的 `free_talk`、`free_talk_members`。不设变量时卡上没有免@，这些设置不生效。
- 开通权限后机器人所在每个群的全部消息都会推给 Bridge：没人免@ 的消息在准入一步丢掉，不写历史、不记日志。日志：`group chat=… free_talk=…`；被拒的免@ 消息是 `ignored message … reason=for_others|proxy_chat`。

### 多设备（开发中，默认关）

让话题在另一台机器（Linux，经 ssh）上工作，而不是在 Bridge 所在的机器上。设备上能跑 Claude Code 和 Codex 的话题；默认关，真机验收在进行（第 1 期只支持这两个 Harness）。这一节写配置和用法，结构和不变量见[架构：设备](architecture.md#设备多设备默认关)。

实现在 `core/devices.py`（配置、`别名:路径` 的解码、ssh 命令行、推送设备端小程序、`hello` 和环境档案、自检）、`core/device_transport.py`、`core/device_link.py`（任务进程）和设备端程序 `core/device_shim.py`。话题的上下文和工作区（`别名:路径` 写进工作区列表、启动卡、换配置和 topic.json，见下面「工作区和上下文」）、文件进出、浏览设备目录、离线和恢复各有一段，下面按顺序写。设备上的话题每一轮开始前先过一遍设备门（Bridge 上的目录检查换成设备的 `stat`、谁能用、Harness 在不在、设备答不答复），过不了就当场拒绝，不排队，也不会改到本机跑。

| 字段 | 含义 |
| --- | --- |
| `schema` | 必填，现在是 `1`。写错的字段名、多出来的字段都会报错，不会悄悄忽略 |
| `defaults` | `patience_minutes`（断线多久后放弃，默认 15，1 到 1440）、`waiting_patience_hours`（等人回答时放宽到多久，默认 6，1 到 168）、`connect_seconds`（ssh 连接超时，默认 5，1 到 60） |
| 设备的 `patience_minutes`、`waiting_patience_hours` | 可选，写在某台设备下，只改这台的耐心值（范围同 `defaults`），不写就用 `defaults` 的。设法见下面「离线、重启与恢复」 |
| `control_dir` | ssh ControlMaster 套接字放的本地目录，绝对路径，不要放网络盘。Unix 套接字路径有长度上限，加上 `/<40 位>.sock` 和 ssh 建套接字时的临时后缀后不能超过 104 字节，所以目录路径最长 41 字节（Linux 上实测 44 字节还能建连、45 字节起 ssh 报 `unix_listener: path ... too long`，多留的 3 字节给 macOS 的 104 字节上限）。不写用 `/tmp/covey-ssh-<uid>`。目录在第一次真连设备时才建（0700） |
| 设备别名 | `devices` 的键，`[a-z][a-z0-9-]{0,23}`。工作区写成 `别名:路径`，例如 `gpu1:/home/me/proj`，`gpu1:~/work`（`~` 是设备上的家目录） |
| `title` | 卡片上显示的名字，默认就是别名 |
| `ssh.host` | `~/.ssh/config` 里的别名或 `user@host`。不能有空格，不能以 `-` 开头 |
| `ssh.options` | 追加的 ssh 参数（`-J` 跳板、`-p`、`-i` 等）。Covey 自己的选项排在前面，ssh 对同一个选项取第一个值，所以这里改不掉它们 |
| `ssh.python` | 设备上的解释器，默认 `python3`。设备端程序只用标准库、按 Python 3.9 写 |
| `harnesses` | 这台设备上要用的 Harness，`claude`、`codex` 的子集，默认两个都要。启动卡选中这台设备的工作区时，模型预选它声明的第一个 Harness（机器人上启用的）；手选了没声明的 Harness，开始任务时被拒并写明原因，换配置确认时同样 |
| `env` | 只写要覆盖设备环境档案的变量（比如这台机器出网要的代理）。值里不能带 `user:密码@` |
| `login_shell` | `true` 时自检再用登录 shell 取一次环境变量（`.bashrc` 里设的代理）。默认 `false` |
| `spaces` | 谁可以用这台设备：`owner_private`（默认，主人私聊）、`personal_groups`、`team`（显式写才开） |

**怎么连**：每台设备一条 ssh 主连接（`ControlMaster=auto`、`ControlPersist=600`、`ControlPath=<control_dir>/%C.sock`），之后的每次调用复用它，不再握手。命令行固定带 `-T`、`BatchMode=yes`（从不问密码，所以设备上要有免密登录，而且 `known_hosts` 里已经有它的主机密钥：先手动 `ssh` 一次）、`ServerAliveInterval=15`、`ConnectTimeout`、`LogLevel=ERROR`。设备端程序按内容哈希放在设备的 `~/.covey/shim/<哈希>/shim.py`，设备上已经有同样内容就不再推。

**工作区和上下文**（`别名:路径`）：
- 写法：在启动卡「其他目录…」或设置卡「工作区」页「＋ 添加工作区…」的目录浏览器的搜索框里输入 `gpu1:/home/me/proj`（只有主人能输，见下面「浏览设备目录」）；`covey_workspaces.py` 命令行本期不认设备值。登记时经设备端程序的 `stat` 检查目录在不在，限时 5 秒；设备没答复、目录不存在、别名不在 `devices.json` 里都当场报错，不会登记。名字自动取 `别名-目录名`（重名加 `-2`），工作区列表里存的是规范化后的 `别名:路径`（`//`、`.`、`..` 折掉，不留结尾的 `/`；`~` 留给设备展开）。
- 话题：`topic.json` 的 `workdir` 存这个规范化的值；读出来时才变成占位路径加 `device`、`remote_workdir`（上下文 `BridgeContext` 和任务记录各有这两个字段，只在非空时写进记录，所以旧记录照旧可读；设备话题的记录在旧版本上读到的是不存在的占位路径，会被拒绝，不会在本机跑）。
- 谁能用（`spaces`）：`owner_private`（默认）= 主人和主人其他账号与机器人的私聊；`personal_groups` = 设置里列为个人空间的群；`team` = 已激活的群。代答群、没绑定的群永远不能用设备。启动卡和换配置卡只列这个聊天可用的设备工作区；点击时再查一次；每一轮开始前再查一次，不允许的聊天里的旧话题会被拒绝并说明原因。
- 每一轮开始前还要设备答复目录在、且这台设备声明了当前 Harness（`harnesses`），否则本轮不启动，卡上写原因（用 `workdir_missing` 这个错误码，说明文字里写明是设备、没答复还是目录不在）。
- 工作区管理页和 `covey_workspaces.py prune` 不检查设备工作区（目录检查对它们返回「没答复」而不是「不存在」），所以不会把设备工作区当成已删除的目录移除。
- 设备上的环境：任务进程的环境从空开始，不是 Bridge 的环境；只给 `PWD`（设备上的目录）加设备档案里通过白名单的代理和证书变量，再加 `devices.json` 的 `env`；`LARK_`、`FEISHU_`、`COVEY_` 开头的名字和 Claude、OpenAI 令牌即使写在 `env` 里也会丢掉，日志只记被丢的变量名，不记值。

**浏览设备目录**：目录浏览器（启动卡的「其他目录…」，设置卡「工作区」页的「＋ 添加工作区…」）在搜索框里认 `别名:路径`（`gpu1:~`、`gpu1:/home/me`）：输入后浏览器就在那台设备上，卡上目录名下面写「设备 GPU 1（gpu1）· 路径」。列目录是设备端程序的 `ls`，按名字搜索是在设备上跑一小段 Python（一次往返，走法同本机：先本层、再向下最多 3 层，不跟随链接，跳过隐藏目录、`node_modules`、`__pycache__`），Bridge 不对设备路径做 `stat`、`list`；`~` 由设备展开，设备视图里不会展开 Bridge 本机的 `~`，设备路径里再输入 `/路径` 或 `~/路径` 仍在这台设备上。缓存的键带设备别名，所以本机和设备上同名的路径不会串。选中的目录登记成 `别名:路径`，名字取 `别名-目录名`，用同样的 5 秒目录检查。
- 谁能浏览：只有主人（浏览器本来就只认主人），而且这个聊天要在设备的 `spaces` 里（默认只有主人私聊）；别名不在 `devices.json`、或聊天不允许，卡上写原因，浏览器留在原处。关了多设备，`gpu1:/x` 就是一次普通的名字搜索，浏览器和卡片同旧。
- 浏览器的搜索框提示在这个聊天能用设备时多一句「设备目录写 别名:路径」。启动卡的工作区一行和摘要里，设备工作区写成「名字 · 设备标题」；设置卡「工作区」页每台这个聊天能用的设备有一行状态（最近一次自检的结果，读 `profile.json`，画卡时不去问设备），设备工作区那一行带「设备 别名」标签，目录状态仍是「未确认」。

**自检**：`python3 -B bin/covey_devices.py --state-dir "$STATE_DIR" check gpu1`（`list` 打印每台设备最近一次自检、不碰设备），见[运维](operations.md#设备自检)。流程：5 秒 ssh（BatchMode）→ 推设备端程序（声明了 Codex 就再推 `workspace_reader.py`）→ `hello` → 导入环境档案 → 对设备声明的每个 Harness 真跑一次最小请求（`claude -p`、`codex exec`，经设备端程序的 `exec`，带档案里的环境，限 60 秒）→ 写 `profile.json`。环境档案只收白名单名字（代理和证书路径，见下面「安全默认」），来源是设备 `~/.claude/settings.json` 的 `env`、设备进程环境里的代理变量，`login_shell: true` 时再加登录 shell 的变量，`devices.json` 的 `env` 最后覆盖；带 `user:密码@` 的代理地址拒收。失败的话写明白是哪一步、怎么办：连不上（要免密登录、known_hosts 里要有主机密钥）、没装、没登录、超时，以及「连不上模型服务」——环境档案里没有代理时说去哪里设 `HTTPS_PROXY`，有代理时说代理不通。结果写进 `profile.json` 的 `check`（时间、是否通过、文字），设置卡的设备状态行和 `service/doctor.py` 的 `devices` 读的是同一份。

**设备上的可用性和模型目录**：每一轮开始前的可用性检查（`app/availability.py`）对设备话题不在 Bridge 上找命令，读设备最近的 `hello`（10 分钟内的缓存）和 `profile.json` 里最近一次最小请求：命令不在、`login` 为 `no`、最近一次最小请求判为没登录，都按「模型后端尚未登录/不可用」拒绝；没有近期的 `hello` 就放行，由这一轮自己的连接说话。Claude 的模型目录经 `spawn_process` 起在设备上，所以就是设备上 Claude Code 的目录；Codex 的 `models_cache.json` 用设备端程序的 `get` 从设备的 `~/.codex/` 取到状态目录 `devices/<别名>/codex-home/`（10 分钟内不重取，设备没答复就用旧副本，没有就是空目录），再交给 `core/model_settings.py` 的 `model_catalog(root)`。启动卡和换配置卡里列出的模型仍是 Bridge 本机的目录（每个 Harness 一份，不随工作区变）。

**离线、重启与恢复**（`COVEY_DEVICES=1` 且设备在 `devices.json` 里才有；实现在 `core/device_link.py`、`core/device_reap.py`、`app/devices.py`）：设备上的 Agent 由设备端小程序守着，Bridge 连不上时它照常在跑，所以「失联」和「停掉」是两件事：
- **耐心值**：连接断开累计超过 `patience_minutes`（默认 15 分钟）这一轮就按中断处理；Agent 正在等你回答（Claude 的 `control_request`、Codex 的 `requestUserInput`、暂停的问题）时改用 `waiting_patience_hours`（默认 6 小时）。两个数都可以在某台设备下单独写，例如 `"gpu1": {"patience_minutes": 45, "waiting_patience_hours": 12, ...}`；改完重启生效。连上并稳定 5 分钟后，断线累计重新算。
- **卡上写什么**：开新一轮时 5 秒内连不上，这一轮不开始、不排队、也不改到本机跑，卡上写「gpu1 连不上（原因）…」，点检查并继续重试；运行中断线，状态行写「gpu1 连接中断，重连中（设备上照常在跑）· 已断 2 分 10 秒」，重连后补读日志，执行过程里多一条「gpu1 已重新连上」；等你回答时断线，写「任务停在等你回答，连上后你的回答会送过去」；超过耐心值，这一轮结算为 `interrupted`（记录里 `interrupted_by` 是 `device_lost`，不会自动再试），卡上写「gpu1 失联超过 15 分钟，这一轮按中断处理；设备上的进程连上后会停掉」，按钮是检查并继续；断线时点停止，写「已请求停止，gpu1 连上后生效」；设备重启（设备说没有这个任务）按失败结算，写「gpu1 重启过，这一轮按失败处理；会话还在设备上，检查并继续会接着做」；机器人重启或只是任务进程被杀、退出（记录分不出这两种），恢复卡写「上一轮的任务进程中断过（机器人重启或任务进程退出），gpu1 上这一轮可能还在跑；点检查并继续会先停掉它再接着做」。设备上的检查点超过 1 MiB 时设备端不再同步，本机那份保持原样，任务卡的状态行加一行标注。
- **待收割（pending-reap）**：失联、任务进程退出、Bridge 重启、断线时的停止、官方拒绝的停止，都只在 `<状态目录>/devices/<别名>/pending-reap.json` 登记一条（任务键、设备上的任务目录和进程组），不当场连设备。设备恢复后，`devices` 后台线程（每 60 秒一轮，只在 `COVEY_DEVICES=1` 且有设备时才起）逐条让设备端 `reap`（只停确认属于这个任务的进程组），设备说干净了才删条目；同一话题的下一轮开始前也会先 `reap` 该话题上一个设备任务（`devices/<别名>/tasks/<话题>.json`），再用 `--resume` 接着做，设备不确认时这一轮不启动。官方拒绝的停止在设备确认前不标已确认（停止记录写 `cleanup_unconfirmed`，原因 `device_reap_pending`）。
- **设备上的清理**：后台线程每小时让设备端 `gc` 一次：清过期的任务目录、没人引用的小程序目录，以及 `~/.covey/checkpoints/<hub-id>/`（检查点镜像）和 `~/.covey/incoming/<hub-id>/`（传到设备的附件）下 7 天没变、也不属于运行中任务的条目。每轮探活的结果在 `devices/<别名>/health.json`。

**记录**（都在状态目录的 `devices/` 下，不是配置）：`hub-id`（这台 Bridge 的编号，两个 Bridge 连同一台设备互不干扰）、`<别名>/hello.json`（最近一次 `hello`）、`<别名>/health.json`（后台线程最近一次探活）、`<别名>/pending-reap.json`、`<别名>/tasks/`（每个话题上一个设备任务）、`<别名>/profile.json`（自检得到的环境档案：Harness 路径、版本、登录状态、通过白名单的代理和证书变量，每个 Harness 最小请求的结果 `probe`，以及最近一次自检的结论 `check`）、`<别名>/codex-home/models_cache.json`（设备上 Codex 的模型目录副本）。

**安全默认**：
- 环境档案只收代理和证书路径这十几个变量名（`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写、`SSL_CERT_FILE`、`NODE_EXTRA_CA_CERTS`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`），带 `user:密码@` 的代理地址拒收，被拒的只记变量名和原因，不记值。Claude 的令牌留在设备上，不经过 Bridge。
- 工作区的 `别名:路径` 在 Bridge 上变成一个**永远不存在**的占位路径 `<状态目录>/devices/<别名>/standin/<12 位哈希>`，设备上的路径只当字符串用、不在 Bridge 上做路径运算。没改到的代码路径会因此明确失败，而不是碰到 Bridge 本机的某个目录。

**已知限制**：
- 只有 Linux 设备在测试机上验证过；macOS 设备没有真机验证。
- 第 1 期只做 Claude Code 和 Codex；设备上的话题没有回退、压缩、导出、脱敏和分叉（按钮不出现）。
- `control_dir` 太长时配置报错，不会自动换目录。
- 设备上的话题没有压缩（轮前压缩、管家的空闲压缩都不会碰设备上的会话），能力位里的 `native_fork`、`compaction`、`turn_export`、`redaction`、`rewind` 一并关掉，卡上没有回退、下载请求与结果按钮。话题开着敏感词脱敏时，设备上的这一轮直接拒绝，不会不脱敏地发出去。
- 任务的检查点在 Bridge 本机那份不变；提示词里写的是设备上的镜像路径（设备用户目录下 `.covey` 里的 `checkpoints` 目录，每个任务一个文件），首轮把 Bridge 写好的检查点放过去，设备上每次改动约 1 秒内写回 Bridge 那份。检查点里登记的产物路径和 `show_images` 是设备上的路径，Bridge 不去本机找同名文件，一律经设备端程序取（见下一条）。
- 设备话题的文件进出都经设备端程序（`stat`、`get`、`put`），Bridge 不打开设备上的任何路径（`app/device_files.py`）：
  - **附件（进）**：消息里的图片和文件先存在 Bridge 本机的话题目录，开跑前把提示词里引用的那几个 `put` 到设备用户目录下 `.covey/incoming/<hub-id>/<任务键>/`，提示词里的路径换成设备上的路径，Bridge 本机的路径不会出现在提示词里。单个文件最多 64 MiB，一轮合计最多 256 MiB，超了的、读不了的、设备没答复的文件都不传：提示词里原来的路径位置写「未传到设备」并列出文件名和原因，任务卡的执行过程里记一行，日志一行 `device attachments`。设备上这个目录由设备端程序的 `gc` 清（见上面「设备上的清理」）。Claude 的图片仍由 Bridge 读本机文件、用 base64 发给模型；Codex 的图片用设备上的副本，没传成的图片不发给模型。执行中补充和等回答时的补充说明、随回答发出的附件和另外补充，也是同一套：先传到设备，文字里换成设备路径；超限和失败的处理同首轮，漏掉的文件在话题里回一行。任务还没开跑时补充，文字并进它的请求，等它开跑时再一起传。
  - **产物（出）**：检查点 `artifacts` 里的路径（相对路径按设备工作区算，`~` 是设备的家目录）先在 Bridge 上按名字判一遍（`.ssh`、`.git`、`.codex`、`.claude` 等代理目录、`.env*`、`auth.json`、`credentials.json`、`.local-state`，和本机产物同一张表），再让设备 `stat`：真实路径（跟着链接走）和是否越出工作区都在设备上算，同一张名字表作为 `deny_names`、`deny_dirs` 随请求发过去；Bridge 对设备报回的真实路径的每一段再判一遍。通过后 `get` 到 `<状态目录>/runs/<任务键>/device-files/<哈希>/`，再走和本机文件一样的云空间上传。设备上的产物必须在设备工作区之内（链接指到外面的也算在外面）；目录暂不支持（请打包成文件）；单个文件的上限同本机（旧的逐文件发送 20 MB，云空间上传 200 MB）。设备断线时这一项在卡上写「设备 gpu1 没有答复」，恢复后点重新上传。
  - **图（出）**：`show_images` 里的绝对路径（设备上的图片文件，不超过 10 MB，同一轮最多 5 张）经同一条路径检查后取到 Bridge 再发，不要求在工作区内（二维码常存在 `/tmp`）；同一张图（设备、路径、大小、修改时间）一轮只发一次。
  - 答案卡的「输入/输出」框本期不显示设备文件的大小。回复正文里的 Markdown 本地图片仍不上传。
- 设备上的话题不取 git diff 快照，所以任务卡和答案卡不显示改动摘要；回复里用 Markdown 图片语法指向设备上文件的本地图片本期不上传，原文保留；知识库提示里不带 `history` 命令（它是 Bridge 本机的命令）。
- 设备上的话题不能由管家开（管家开话题遇到设备工作区会明确拒绝，请主人在启动卡里选）；管家自己的工作区也不能是设备。
- 设备工作区不能用「独立 worktree」运行方式；换配置换到设备工作区只能选原目录。
- 换配置时设备话题不生成「旧模型的摘要」（旧会话在设备上）。
- 设置卡上设备的状态行读的是最近一次 `covey_devices.py check` 的结果，不会自己去自检；没有近期自检的设备也不会因此被拒绝。
- 设备上的 Codex 模型目录取自 `~/.codex/models_cache.json`（设备上设了别的 `CODEX_HOME` 就取不到）；设备上的话题不用 hub 上模型网关的检查结果。

### 节点与机器状况（默认关）

设了 `COVEY_NODE_LISTEN` 才有；结构和不变量见[架构 · 节点](architecture.md#节点默认关)。

- **监听**：`COVEY_NODE_LISTEN='[::]:$ARNOLD_RESERVED_N_CURRENT_PORT'`（`[::]:端口`、`IPv4:端口` 或只写端口；`$名字`、`${名字}` 每次启动时从环境里取值，平台按机器分配端口时用单引号原样存进清单；`core/listen.py`）。节点主动连这个端口（TLS，Hub 自签证书，节点按证书指纹认 Hub），设备上不开端口。设备连不到 Hub 推算的地址时用 `COVEY_NODE_ADDRESS=主机:端口` 指定。
- **本机**：开了就有一个本机节点（名字 `local`，显示为“本机”）：Bridge 每分钟确认 `~/.covey/node/` 里的节点在跑，没在跑就拉起；它独立于 Bridge 运行，样本先写本地盘（`~/.covey/node/data/`，最多 256 MiB、7 天），并每 10 分钟试写一次数据根，写不进（配额满、挂了）会告警。
- **加设备**：`core/nodes.py` 的 `open_join` 发一个 30 分钟有效、只能用一次的口令；设备上执行 `app/node_gateway.py` 的 `paste_command` 给出的那行命令（`curl … | python3 -`，按 Hub 公钥固定，不走代理），节点就装好、写自启（`~/.bashrc` 顶部的登录钩子；有 systemd 用户服务或 launchd 时也装）并连上来。设置卡的「设备」页给出这行命令，Hub 能 ssh 进去的设备也可以在那里直接装。
- **数据**：`<状态目录>/nodes/`：`registry.json`（节点名单，只存密钥的哈希）、`<节点>/latest.json`、`link.json`、`raw/`（原样，3 天）、`minutes/`（每分钟一行，30 天）、`alerts.jsonl`；`events/`、`alerts/` 是告警的待发和已发记录。
- **告警**：内存工作集到上限的 90% 持续 1 分钟、发生 OOM、盘用到 90%、试写失败、设备 10 分钟没消息；能对上任务的发到那个话题，否则发到私聊；恢复后原卡改成“已恢复”；每小时最多 6 张。开着时[内存看门狗](operations.md#内存看门狗)的“容器匿名内存”告警不再发（同一件事不收两张卡），它杀失控搜索进程的告警照发。
- **移除**：`core/nodes.py` 的 `revoke`：节点下次连上来（或在线时立刻）被告知，它删掉自启和 `~/.covey/node/` 后退出。

### 飞书 SDK 与直连

- `COVEY_CARD_TRANSPORT=sdk`：CardKit 卡片接口、消息的发送和回复、读消息详情都用常驻的 `lark-oapi` 客户端；上传文件和图片、转发话题等只有 lark-cli 做得了的仍走 lark-cli（见[投递、重投与 outbox](architecture.md#投递重投与-outbox)）。先在运行 Bridge 的那个 Python 里 `python3 -m pip install '.[feishu-sdk]'`。SDK 只支持显式命名的 profile，应用密钥只在内存里读取。请求发出前取不到凭据时退回 CLI（日志 `CardKit SDK unavailable locally; using CLI`）；请求已发出后不换传输重发。
- `COVEY_FEISHU_DIRECT`：默认直连飞书开放平台，`0`（也认 `false`、`no`、`off`）时都走环境里的代理。直连时 Bridge 启动先试一次连 `open.feishu.cn:443`（lark 品牌是 `open.larksuite.com`），连得上就把它加进本进程的 `no_proxy`、`NO_PROXY`，之后的子进程都继承，日志 `Feishu direct: lark-cli reaches open.feishu.cn without the proxy`；连不上记 `Feishu direct unavailable (<异常类型>); lark-cli keeps the proxy`，环境不动。事件长连接的主机不在其中，仍走代理。

### 其他

- `COVEY_APP_CHECK=0`：不检查飞书应用、不发「飞书应用待补」卡。检查和补齐见[自动补齐飞书应用](operations.md#自动补齐飞书应用)。
- `--health-timeout`：`launch` 的参数，默认 120 秒，0 关闭。Bridge 本地健康接口连续这么久没有响应就替换进程。bootstrap 不传它，所以经 bootstrap 注册的都是 120。
- `COVEY_PRIVACY_DENYLIST`（只给 `scripts/privacy_check.py` 读，Bridge 本身不读）：指向仓库外的文本文件，每行一个公司域名或内部名词，隐私检查按它查；不设就只查通用规则，扩展里放有一份，见[隐私检查](testing.md#隐私检查)。

## 环境变量与选项一览

**命名（改名 Covey 后）**：设置的名字是 `COVEY_<X>`，旧名 `LARK_BRIDGE_<X>` 照常生效，不用改任何配置。`covey/core/env.py` 的 `get` 先读 `COVEY_<X>`、再读 `LARK_BRIDGE_<X>`（新名优先；只设了旧名时，进程的环境里读到旧名会在 stderr 记一行弃用，每个名字一次）。下表只写新名；每一行的旧名就是把 `COVEY_` 换成 `LARK_BRIDGE_`（改名之后才有的开关没有旧名，见下面的“启动时只对真有旧名的变量打印”一条）。主线程改名为管家（2026-11-16）时有七个开关连名字一起改了，表里每行注明旧名；它们有三种写法，读取顺序是新名 `COVEY_<新>`、`COVEY_MAIN_<旧>`、`LARK_BRIDGE_MAIN_<旧>`（`covey/core/env.py` 的 `RENAMED`）：`COVEY_BUTLER`（旧 `MAIN_THREAD`）、`COVEY_BUTLER_RULES`（`MAIN_BUTLER`）、`COVEY_BUTLER_TRACKS`（`MAIN_TRACKS`）、`COVEY_BUTLER_TRACKS_MAX`（`MAIN_TRACKS_MAX`）、`COVEY_DISPATCH_CARD`（`MAIN_DISPATCH_CARD`）、`COVEY_BUTLER_JOB`（`MAIN_JOB`）、`COVEY_BUTLER_STATE`（`MAIN_STATE`）。`LARK_<HARNESS>_BRIDGE_*`（`LARK_CLAUDE_BRIDGE_*` 等 Harness 自己的前缀）和已不再读取的名字不变。

- **服务清单**：`runtime_environment()` 仍按旧名存（`COVEY_X` 存成 `LARK_BRIDGE_X`；上面七个存成 `LARK_BRIDGE_MAIN_*`，如 `COVEY_BUTLER` 存成 `LARK_BRIDGE_MAIN_THREAD`），所以升级不改清单，回滚到旧 release 也读得到；`scripts/deploy_bridge.sh` 的环境比对把清单和终端里的名字都折成旧名再比（`env.canonical`），只改了名字不算变化。Manager 启动服务时所有写法都设进服务进程（清单里的值盖过 Manager 自己环境里的同名设置），所以旧 release 的任务进程也读得到。
- **Bridge 给子进程设的变量**（`COVEY_TASK_DIR`、`COVEY_BUTLER_STATE`、`COVEY_BUTLER_JOB`、`COVEY_RUNNER_PYTHONPATH` 等）所有写法都设（管家的两个另设 `COVEY_MAIN_*`、`LARK_BRIDGE_MAIN_*`）；找任务遗留进程（`core/task_processes.py`、内存看门狗）认两种。
- 启动时只对真有旧名的变量打印“`LARK_BRIDGE_X` is the old name of `COVEY_X`”；改名之后才有的开关（`COVEY_PROJECT*`、`COVEY_PATROL*`、`COVEY_PROGRESS*`、`COVEY_TRACK_JUDGE*`、`COVEY_DEVICES*`、`COVEY_NODE*`，`core/env.py` 的 `BORN_NEW`）没有旧名，服务清单里存成的 `LARK_BRIDGE_X` 只是存放写法，读到也不提示。
- 下一个版本起清单才改存新名，旧名读取再保留至少一个版本。

代码在 `covey/` 和 `bin/` 里读的每个环境变量和命令行参数都列在这里，`tests/test_structure.py` 的 `InventoryTest` 核对：代码读了就要有一行，行里写出读它的文件；表里写了的代码也要读。不列 `HOME`、`PATH`、`PYTHONPATH`、`XDG_CONFIG_HOME`、`NO_PROXY`/`no_proxy` 这类系统变量。不再读取的环境变量不在表里，只有两个权限变量还为拦截而读（见[服务清单里的环境变量](#服务清单里的环境变量)）。

| 环境变量 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `COVEY_DATA_HOME` | `core/paths.py`、`core/devices.py`（找 `devices.json`） | 按 [数据目录](operations.md#数据目录) 推算 | 否 |
| `COVEY_SERVICE_ROOT` | `core/paths.py` | `<数据根>/bridge-services` | 否 |
| `COVEY_STANDING_ANSWERS` | `cards/question_cards.py`（`standing_offered`） | 关；`1`：问题卡上给「以后这类按推荐走」。`./install` 新装的服务写 `1`（`service/installer.py` 的 `INSTALL_SWITCHES`） | 否 |
| `COVEY_STATE_DIR`；`LARK_CLAUDE_BRIDGE_STATE_DIR`、`LARK_DSH_BRIDGE_STATE_DIR`、`LARK_PI_BRIDGE_STATE_DIR` | `app/harnesses.py`（只有默认 Harness 读，其余总用 Bridge 的 `--state-dir`） | `<数据根>/<Harness 的目录名>` | 否 |
| `COVEY_PROFILE` | `backends/codex/bridge.py`、`backends/dsh/bridge.py`、`backends/pi/bridge.py`（Bridge 的 `--profile`） | 空：lark-cli 的默认 profile | 否 |
| `COVEY_MESSAGE_FORMAT`、`LARK_CLAUDE_BRIDGE_MESSAGE_FORMAT`、`LARK_DSH_BRIDGE_MESSAGE_FORMAT`、`LARK_PI_BRIDGE_MESSAGE_FORMAT` | `feishu/messenger.py`（各 Harness 发消息时读） | `markdown` | 否 |
| `COVEY_MESSAGE_CHUNK_LIMIT`、`LARK_CLAUDE_BRIDGE_MESSAGE_CHUNK_LIMIT`、`LARK_DSH_BRIDGE_MESSAGE_CHUNK_LIMIT`、`LARK_PI_BRIDGE_MESSAGE_CHUNK_LIMIT` | `feishu/messenger.py` | 12000 | 否 |
| `COVEY_CARD_TRANSPORT` | `feishu/sdk.py`（`enabled()`） | 空：lark-cli；`sdk` 走 SDK | 否 |
| `COVEY_FEISHU_DIRECT` | `feishu/sdk.py` | 开：直连飞书，`0` 走代理 | 否 |
| `LARKSUITE_CLI_DATA_DIR` | `feishu/sdk.py`（找 lark-cli 的凭据） | `~/.local/share` | 否 |
| `COVEY_OUTGOING_MEDIA_DIRS` | `feishu/outgoing_images.py` | 空 | 否 |
| `COVEY_ACCEPT_CHATS` | `feishu/web_service.py`（验收页面） | 代码里的 `ACCEPT_CHATS` | 否 |
| `COVEY_FEISHU_WEB_HOST` | `feishu/web_client.py` | 扩展的 `web_tenant_domain`，没有时 `open.feishu.cn` | 否 |
| `PLAYWRIGHT_BROWSERS_PATH` | `feishu/web_client.py`（验收页面的浏览器）、`feishu/app_console.py`（开发者后台的浏览器）、`feishu/install_browser.py`（`./install` 的浏览器会话）、`core/browser_env.py`（定位与 `launch.json`） | `<数据根>/browsers`（存在时），否则 `~/.cache/ms-playwright`（`./install` 设成 `<数据根>/browsers`） | 否 |
| `COVEY_BROWSER_EXECUTABLE` | `feishu/web_client.py`（`launch`）、`core/browser_env.py`、`service/browser_setup.py` | 空：用 Playwright 下载的 Chromium；`./install` 发现机器上已有 Chrome/Chromium 时写进 `<数据根>/browsers/launch.json` | 否 |
| `LD_LIBRARY_PATH` | `core/browser_env.py`、`service/browser_setup.py`（无 root 缺 Chromium 系统库时，把 `<数据根>/browsers/syslibs/` 下解出的库目录加在前面，只给浏览器进程） | 不动 | 否 |
| `TMPDIR` | `service/browser_setup.py`（监视浏览器下载有没有进展的临时目录；`./install` 设成 `<数据根>/tmp`） | `<数据根>/tmp` | 否 |
| `ANTHROPIC_BASE_URL` | `app/redaction_panel.py`（「探测」）、`backends/claude/redaction.py`（脱敏中继的上游） | 空 | 否 |
| `COVEY_PINNED_OVERVIEW` | `app/overview.py`（`app/pins.py` 的 `laid` 也读：两个开关都开时看板记录的规则才算数） | 关；`1` 开 | 否 |
| `COVEY_HARNESS_PROBE` | `app/availability.py`（`PROBE_ENV`；探测本身在 `core/harness_probe.py`） | 关；开了才每 60 秒的可用性检查里对 Claude、Codex 各做一次真实调用（缓存 6 小时，失败 10 分钟）。缓存里是“没登录”时该 Harness 在启动卡上不可用，网络、超时等只写进健康检查。可由服务清单保存 | 否 |
| `COVEY_GROUP_ALL_MESSAGES` | `app/access.py`（`free_talk_on`；群聊卡 `app/group_manager.py` 经它） | 关；`1` 开（还要应用开通 `im:message.group_msg`） | 否 |
| `COVEY_DIGEST_TIMES` | `app/digest.py`、`core/app_manifest.py`（有它才要菜单「看板」）、`app/project_tidy.py`（`scheduled`：整理专项的定时也按这些时间） | 空：不发早晚汇总，整理专项也没有定时的一次 | 否 |
| `COVEY_KNOWLEDGE` | `core/knowledge.py`（`enabled`）、`core/butler.py`（`mcp_config` 交给管家的 MCP 服务）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此列知识工具） | 空：不开知识条目 | 否 |
| `COVEY_HISTORY_CMD` | `core/knowledge.py`（`history_cmd`）、`core/butler.py`（`mcp_config` 同上）、`core/butler_tool_specs.py`（`listed`：据此列 `history_search`） | 空：没有历史搜索；要绝对路径 | 否 |
| `COVEY_EXTENSIONS` | `core/extensions.py` | 空：读仓库根的 `extensions/enabled`（没有这个文件就没有扩展）；设成空串表示一个都不要。逗号分隔的 Python 包名，见[扩展](architecture.md#扩展) | 否 |
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
| `COVEY_BUTLER_TRACKS`（旧名 COVEY_MAIN_TRACKS、LARK_BRIDGE_MAIN_TRACKS） | `core/butler_tracks.py`（`enabled`；`core/butler.py` 的 `is_butler_topic` 据此把槽 1 到 4 的话题也算管家；`app/butler_tracks.py` 的 `Tracks.enabled` 再加上管家允许、模式是管家 才分流） | 空：关；`1` 开同时做几件事（已接线）：家族里有任务在做时，主人的下一句先过分流（`core/track_route.py` 的规则，拿不准的交给小模型 `COVEY_TRACK_JUDGE`）：并进某件在做的事、停止，或新开一件（槽 1 到 4，各是管家会话的分叉，同时跑；排队、并进有表情；另外几件事答了什么交给管家的下一轮）。关着时消息的走法和原来一样，也不起任何判断进程。见[管家 · 同时做几件事](butler.md#同时做几件事开关默认关) | 否 |
| `COVEY_PROJECTS` | `core/projects.py`（`enabled`；`core/butler.py` 的 `is_lead`、`core/placement.py` 的 `plan`、`core/board_read.py` 的 `read` 据此判断负责人话题、专项的话题和看板的格）、`core/butler.py`（`mcp_config` 把它交给 MCP 服务）、`core/butler_tool_specs.py`（`listed`：它是 `1` 时管家多出 `projects_list`、`project_send`，`topic_start`、`topic_send` 多一个 `project` 参数） | 空：关；`1` 开专项的底座和负责人的运行（第 2 期，开发中，还没有入口）：负责人话题（`topic.json` 的 `project.role` 是 `lead`）算管家家族，它的一轮用自己的规则、状态包和 `project_*` 工具（`app/project_lead.py`、`app/project_tools.py`）；专项的话题（`thread`）有自己的放置格、看板把一个专项的话题并成一格。关着时谓词全为假、放置表和看板和原来一样，也不建任何记录。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_BUTLER_TRACKS_MAX`（旧名 COVEY_MAIN_TRACKS_MAX、LARK_BRIDGE_MAIN_TRACKS_MAX） | `core/butler_tracks.py`（`limit`） | 3：同时在跑的件数上限，取值限制在 2 到 4，读不出来按 3 | 否 |
| `COVEY_BUTLER`（旧名 COVEY_MAIN_THREAD、LARK_BRIDGE_MAIN_THREAD） | `core/butler.py` | 允许（模式看 `main-thread.json`）；`0` 不允许 | 否 |
| `COVEY_TRACK_JUDGE` | `app/track_judge.py`（同时做几件事里，规则没能定的新消息交给小模型判断是并进哪件在做的事还是新开一件；`app/butler_tracks.py` 的 `Tracks.decide` 调用，`COVEY_BUTLER_TRACKS=1` 才会问） | `claude`（默认）：Claude Code 的 Haiku，一次问一条（默认用提前起好的待命进程，见 `COVEY_TRACK_JUDGE_WARM`）；`off` 不问，直接新开；`cmd:/absolute/path` 用自己的命令（标准输入一段 JSON，标准输出一行 JSON）。超时、出错、拿不准、脱敏话题都按新开一件 | 否 |
| `COVEY_TRACK_JUDGE_MS` | `app/track_judge.py` | 3000：小模型硬上限（毫秒），超过按进程组杀掉、按新开一件；取值限制在 200 到 10000。经网关调一次 Haiku 常要 1–2 秒，2 秒的上限下约四成会超时，所以默认 3 秒| 否 |
| `COVEY_TRACK_JUDGE_WARM` | `app/track_judge.py`（`warm_enabled`；`Tracks.handle` 经 `warm` 调用，进程在 `app/track_standby.py`） | 空：开；`0`、`off`、`false`、`no` 关。开着（且 `COVEY_BUTLER_TRACKS=1`、通道是 `claude`）时，Bridge 在收到管家消息时提前起好一个 Claude Code 待命进程并用一条虚构的消息预热，问一次就丢掉、立刻起下一个，空闲 10 分钟或 Bridge 停止时结束；关着每次问都现起一个一次性进程，慢约 1 秒多 | 否 |
| `COVEY_TRACK_JUDGE_HEDGE` | `app/track_judge.py`（`hedge`；`warm` 据此多留几个待命进程，`judge` 同时问它们，进程在 `app/track_standby.py`） | 空：2；取值 1 到 3。同一问同时交给这么多个待命进程，第一个能用的答案算数，其余立刻结束。网关的慢请求是各请求自己的（几次里有一次要多等 2 到 4 秒），两个里总有一个躲开；1 是只用一个，和以前一样。每多一个，每次判断多一个几十 token 的预热请求和一个小请求；只在 `COVEY_TRACK_JUDGE_WARM` 开着、通道是 `claude` 时有用 | 否 |
| `COVEY_TRACK_JUDGE_SLIM` | `app/track_judge.py`（`slim_enabled`，给小模型的 Claude Code 命令行多两个选项） | 空：开；`0`、`off`、`false`、`no` 关。开着时小模型的 Claude Code 带 `--setting-sources user --disable-slash-commands`：不再读工作目录一路向上的项目设置、记忆文件和技能清单，每次输入约 800 token（以前约 4800，2026-11-17 在本机量的）；Claude Code 不认这两个选项时记住、之后不再带 | 否 |
| `COVEY_BUTLER_STATE`（旧名 COVEY_MAIN_STATE、LARK_BRIDGE_MAIN_STATE） | `core/butler_mcp.py`、（管家任务的 MCP 配置写进去，告诉工具服务 Bridge 的状态目录） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_BUTLER_JOB`（旧名 COVEY_MAIN_JOB、LARK_BRIDGE_MAIN_JOB） | `core/butler_mcp.py`（管家任务的任务键，Bridge 据此核对调用） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_PROJECT_LEAD` | `core/butler.py`（`mcp_config`：只在专项负责人的任务里把专项 id 写进 MCP 配置，要 `COVEY_PROJECTS=1`）、`core/butler_tool_specs.py`（`listed`：MCP 服务据此只列负责人的工具） | 无；设了就列 `topic_read`、`topics_search`、`workspaces_list` 加 `project_*`，其余管家工具都不列 | 否 |
| `COVEY_PROJECT_AUTOWAKE` | `core/projects.py`（`autowake_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_loop.py`（`ProjectLoop` 据此决定哪些回执唤醒负责人、要不要查停滞） | 空：关，只有专项的第一条回执（`start`）唤醒负责人，其余回执留到负责人下一次被主人叫起；`1`：专项的话题一轮结束、主人在专项的话题里说话、停滞提醒也都唤醒，同一专项同一时刻最多排 1 个唤醒，并开停滞检查。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_PROJECT_MEMORY` | `core/projects.py`（`memory_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_context.py`（专项的话题开工带上记忆和资料库索引、主人答专项的问题卡记一条决定、主人发给负责人的文件和各话题的产物、交付进资料库）、`app/project_lead.py`（`library_index`：状态包的资料库）、`app/runs.py`（`bridge_rules`、`_with_rules` 取专项块） | 空：关，专项的话题开工什么都不多带，不自动记记忆，不收资料库；记忆本身（`core/project_memory.py`，卡上、状态包、负责人工具都读它）不受开关影响。`1`：专项的话题开工带上专项记忆（新会话再加不超过 2 KB 的资料库索引），主人答专项的话题或负责人发出的问题卡后记一条 `decision`（来源 `owner_answer`），主人引用专项卡发的文件复制进 `projects/<id>/library/`（记 sha256），话题每轮的产物和检查点交付只记路径或链接。见[管家 · 专项记忆与资料库](butler.md#专项记忆与资料库开关默认关) | 否 |
| `COVEY_MEMORY` | `core/memory.py`（`enabled`）；用它的：`app/memory_turn.py`（每轮那一块“记忆”）、`app/memory_collect.py`（话题一轮结束后核对 `memory`、`memory_void`）、`app/runs.py`、`app/knowledge_turn.py`、`app/project_context.py` | 空：关，规则、续接提醒和答案卡与没有这个功能时逐字相同，不读不写话题的 `memory.json`。`1`：原则、知识条目、专项记忆、话题记忆拼成一块“记忆”，每条带编号、管哪儿、谁说的，话题里主人说了“以后”“每次”“别再”之类时，一轮结束后记下并在答案卡底部露一行。见[管家 · 记忆](butler.md#记忆开关默认关) | 否 |
| `COVEY_PROJECT_TRIAGE` | `core/triage.py`（`enabled`，要 `COVEY_PROJECTS=1`）、`core/butler.py`（`mcp_config` 把它交给负责人的 MCP 服务）、`core/butler_tool_specs.py`（`lead_tools`）、`app/triage.py`、`app/project_loop.py`（`ProjectLoop.tick` 扫扣住的问题） | 空：关，专项的话题的问题照旧直接发给主人，负责人没有 `project_answer`、`project_escalate`；`1`：专项的话题问的选择题先交给负责人，3 分钟内它能依据专项记忆和已定的事回答的就替主人答，答不了、碰到硬闸词、转给主人或 3 分钟没处理的才发问题卡。自由文字的问题始终直接问主人。见[管家 · 专项的问题分流](butler.md#专项的问题分流开关默认关) | 否 |
| `COVEY_PROJECT_BUDGET` | `core/projects.py`（`token_limit`）、`app/project_loop.py`（`hold`） | 空：没有上限，专项不会因为 token 停住，也不请停话题、不发停住卡（2026-11-19 起；用量照样累加，专项卡上写“token N”）。设成正整数才有上限：一个专项的 token 合计（负责人和各话题任务的合计，缓存算在输入里）到了专项转停住、`state_reason` 是 `tokens`，不再唤醒，开着时和以前一样；不是正整数按没有上限 | 否 |
| `COVEY_PROJECT_TOKENS` | `core/projects.py`（`token_limit`） | `COVEY_PROJECT_BUDGET` 的旧名，它没设时才读。名字里有 TOKEN，服务清单把它当成密钥不保存，重新 `launch` 带不进去：要设上限用 `COVEY_PROJECT_BUDGET` | 否 |
| `COVEY_PROJECT_ASK` | `core/projects.py`（`ask_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_loop.py`（`ProjectLoop.pause`）、`app/project_cards.py`（`ProjectCards.ask`、`ask_choice`） | 空：关，护栏停住的专项只在专项卡上写“停住 · 原因”；`1`：专项被护栏停住时，在专项卡所在的位置再发一张卡，一句话写原因，三个按钮 继续 / 收尾 / 停；点了以后这张卡缩成一行写选了什么，专项卡的状态跟着变。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_PROJECT_STALL_SECONDS` | `core/projects.py`（`stall_seconds`）、`app/project_loop.py`（`ProjectLoop.stall`） | 1800：专项还有负责人能推动的事（有任务排队、有没处理的回执；只在等你回答不算）却没有任务做完、没有新回执，多少秒后提醒负责人一次，再过同样久还没动静就停住；没有要做的事的专项不会停滞，只按 `COVEY_PROJECT_IDLE_DAYS` 收起；不是正整数按默认值。试用停滞卡时调小（如 `60`） | 否 |
| `COVEY_PROJECT_IDLE_DAYS` | `core/projects.py`（`idle_days`）、`app/project_loop.py`（`ProjectLoop.rest`）、`app/project_cards.py`（`ProjectCards.fold`） | 7：专项没有要做的事（负责人和各专项的话题都没有排队或在跑的任务、没有没处理的回执；只剩等你回答的也算）、主人也这么多天没提它，就收起（状态仍是停住，原因 `idle`），专项卡缩成一行“收起 · N 天没有新的事”，不弹停住卡也不问；主人对它说话或管家把新的事交给它时自动展开。可以写小数，试用时调小：`0.001` 约 86 秒；不是正数、不是数字按默认值；要开 `COVEY_PROJECTS=1` | 否 |
| `COVEY_PROJECT_STYLE_KEEP` | `core/projects.py`（`style_keep_enabled`，要 `COVEY_PROJECTS=1`）、`app/project_tools.py`（`project_style`）、`app/projects.py`（`seed_style`） | 空：关，新专项的汇报风格是默认值；`1`：负责人每次用 `project_style` 改风格，就把整份风格记进 `<state>/project-style.json`，之后新开的专项（转成专项、直接开）从这份风格开始，之前的专项不动 | 否 |
| `COVEY_PATROL` | `core/patrol.py`（`mode`）、`app/patrol.py`、`app/digest.py` | 空：关。`shadow`：只记不打扰；`on` 现在同 `shadow`；其他值都是关 | 否 |
| `COVEY_PATROL_ASK_MINUTES` | `core/patrol.py`（`number`） | 120：问题等主人这么多分钟算久（1 到 10080）；试用时调小，如 `3` | 否 |
| `COVEY_PATROL_UNSEEN_MINUTES` | `core/patrol.py`（`number`） | 360：做完的结果这么多分钟没看算久（1 到 10080） | 否 |
| `COVEY_PATROL_STALL_MINUTES` | `core/patrol.py`（`number`） | 180：在做的任务记录这么多分钟没变算没动静（1 到 10080） | 否 |
| `COVEY_PROGRESS_STUCK_MINUTES` | `core/progress.py`（`stuck_seconds`）、`core/longtask.py`（`TaskMonitor`） | 30：在跑的一轮最近一次动静（执行过程的一步、流式输出、模型请求、检查点更新）已经过去这么多分钟，就算“可能卡住了”：任务卡状态行写“超过 30 分钟没有动静，可能卡住了”，状态行下面出「看看」按钮，看板、手上的事、早晚汇总和管家快照里写“可能卡住了”，网页推送的 `progress.maybe_stuck` 为 true。整数，1 到 1440，超出或不是整数按默认值；试用时调小，如 `2` | 否 |
| `COVEY_PATROL_FAILS` | `core/patrol.py`（`number`） | 2：同一个话题连着失败几次算（2 到 20） | 否 |
| `COVEY_PATROL_PROJECT_MINUTES` | `core/patrol.py`（`number`） | 720：专项有事没人动这么多分钟算（1 到 43200） | 否 |
| `COVEY_PATROL_TICK_SECONDS` | `core/patrol.py`（`number`） | 60：规则看一次记录的间隔，秒（5 到 3600） | 否 |
| `COVEY_PATROL_JUDGE_MINUTES` | `core/patrol.py`（`number`） | 30：小模型两次调用的间隔，分钟（1 到 1440）；试用时设 `1` | 否 |
| `COVEY_PATROL_DAILY_CALLS` | `core/patrol.py`（`number`） | 48：小模型一天最多调几次（1 到 1000） | 否 |
| `COVEY_PATROL_JUDGE_MS` | `core/patrol.py`（`number`） | 30000：小模型一次调用的硬上限，毫秒（1000 到 120000） | 否 |
| `COVEY_WEB` | `core/web_sync.py`（`enabled`）、`app/web_sync.py`（`WebSync.threads`） | 空：关。`1`：开网页工作台同步，`web-push`、`web-inbox` 两条线程；要同时设 `COVEY_WEB_CONFIG` | 否 |
| `COVEY_WEB_CONFIG` | `core/web_sync.py`（`load_config`） | 空。指向一个 JSON：`base_url`、`key_file`（只从这个文件读密钥）、`push_seconds`（默认 1）、`poll_seconds`（默认 1）；文件缺了或写错只记一行失败、退避重试 | 否 |
| `COVEY_PROJECT_TIDY` | `core/project_proposals.py`（`mode`、`enabled`，要 `COVEY_PROJECTS=1`）、`app/project_tidy.py`（`ProjectTidy.threads`）、`app/runs.py`（`_gist_note`：每轮消息里的话题要点规则）、`core/butler.py`（`mcp_config` 交给 MCP 服务）、`core/butler_tool_specs.py`（`tidy_listed`：管家多 `projects_tidy`）、`app/web_sync.py`（话题的 `gist`）、`app/butler_panel.py`（设置卡的「整理专项」） | 空或 `off`：关。`suggest`：只提议（能归进已有专项的直接归，新建要点「建专项」）；`auto`：新建也直接做。`./install` 新装的服务写 `suggest`。见[整理专项](#整理专项) | 否 |
| `COVEY_PROJECT_TIDY_MODEL` | `core/project_proposals.py`（`model`）、`app/project_sorter.py`（`ask`） | `sonnet`：补话题要点和整理判断用的 Claude Code 模型别名或 id；写错按默认 | 否 |
| `COVEY_PROJECT_TIDY_DAYS` | `core/project_proposals.py`（`number`） | 7：整理看最近几天有过一轮的话题（专项里的话题不论多久都看） | 否 |
| `COVEY_PROJECT_TIDY_AGE_DAYS` | `core/project_proposals.py`（`number`） | 3：话题开了这么多天后又有一轮结束，算一次实时时机（每个话题一次） | 否 |
| `COVEY_PROJECT_TIDY_WORDS` | `core/project_proposals.py`（`number`） | 15：主人在话题里说到这么多句（他开的轮数），算一次实时时机（每个话题一次） | 否 |
| `COVEY_PROJECT_TIDY_MIN_TOPICS` | `core/project_proposals.py`（`number`）、`app/project_tidy.py`（`realtime`、`start`） | 5：聊天里的话题少于这个数时不整理（实时、定时、手动都不） | 否 |
| `COVEY_PROJECT_TIDY_JUDGE_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`judge`） | 90：一次判断调用的硬上限，秒；超了这次判断作废，什么都不改 | 否 |
| `COVEY_PROJECT_TIDY_READ_SECONDS` | `core/project_proposals.py`（`number`）、`app/project_sorter.py`（`backfill`） | 60：补一个老话题的要点那次调用的上限，秒；超了只跳过这个话题 | 否 |
| `COVEY_PROJECT_UPGRADE` | `app/projects.py`（`mode`：升级、`Projects.consider`；要 `COVEY_PROJECTS=1` 和 `COVEY_DISPATCH_CARD=1`） | 空或 `off`：不转成专项（默认）；`butler`：管家转话时带了 `project`（`topic_send` 的 `project` 参数），派发卡就静默转成专项，是不是由管家按话的意思判断（主人要长期跟进、要分成几个话题持续推进，或直接说“开个专项”“转成专项”才带，拿不准不带），不再匹配主人话里的关键词；`word`：旧值，意思同 `butler`；`auto`：再加该话题来回到第 3 轮。同时最多 3 个活专项，超了不转。直接开专项（管家的 `topic_start` 带 `project`）不看它，只看 `COVEY_PROJECTS`。见[架构 · 专项](architecture.md#专项第-2-期开发中默认关) | 否 |
| `COVEY_PINNED_TOPICS` | `core/topic_seen.py`（看板 `app/pins.py` 和已读消费者 `app/topic_seen.py` 都经它） | 关；`1` 开（还要 `COVEY_PINNED_OVERVIEW=1`） | 否 |
| `COVEY_PROXY_PROFILE` | `app/proxy.py` | 空：代答不可用 | 否 |
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
| `--harness-config`、`--owner`、`--app-id`、`--pairing-file` | `app/unified.py` | 空 | 否 |
| `--run-dir`、`--await-handoff`、`--compact-only` | `app/runner.py`（任务进程，Bridge 填；`--compact-only` 是管家空闲时的压缩进程，`app/idle_compaction.py`） | 无 | 否 |
| `--model`、`--reasoning-effort`、`--sandbox`、`--codex-cmd`、`--codex-timeout` | `backends/codex/bridge.py` | `gpt-6-astra`、`max`、`danger-full-access`、`codex`、0（不限） | 否 |
| `--codex-transport` | `backends/codex/bridge.py` | `app-server`（唯一的选项） | 只为旧命令行能解析 |
| `--codex-magpie-url` | `backends/codex/bridge.py` | 值被忽略；设了非空值启动时记一行 `no longer used` | 只为旧服务命令行能解析（`compat_report: codex-magpie-url`） |
| `--model`、`--effort`、`--claude-cmd`、`--claude-timeout`、`--no-skip-permissions`、`--claude-login-shell`、`--claude-auto-compact-window`、`--claude-compact-between-turns` | `backends/claude/bridge.py` | `claude-fable-5`、`xhigh`、`claude`、0、关、关、空、空 | 否 |
| `--claude-profiles` | `backends/claude/bridge.py` | 已下线，值被忽略，设了启动记一行；见[已下架项与旧配置](#已下架项与旧配置) | 否 |
| `--claude-transport`、`--traex-ppe-env`、`--traex-site` | `backends/claude/bridge.py` | 值被忽略 | 只为旧 `harnesses.json` 能解析（`compat_report: removed-options`） |
| `--model`、`--reasoning-effort`、`--dsh-cmd`、`--dsh-timeout` | `backends/dsh/bridge.py` | 见 `--help` | 否 |
| `--model`、`--reasoning-effort`、`--pi-cmd`、`--pi-timeout` | `backends/pi/bridge.py` | 见 `--help` | 否 |

`scripts/deploy_bridge.sh` 自己的参数不在这张表里（表只列 Bridge 读的）：`--ext-repo`、`--ext-commit`、`--allow-core-mismatch` 让它把核心检出和扩展检出拼成一个 release，不给时只部署 `--repo` 一个检出，`--ext-commit` 默认 `HEAD`；`--allow-rollback` 让它部署不包含现网提交的版本（有意回滚），另一个部署正在跑时它退出码 3、不部署；用法见[部署新版本](operations.md#部署新版本)。

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
| `--state-dir`、`--no-probe`、`--json` | `service/device_check.py`（`bin/covey_devices.py`） | 见 `--help`；见[多设备](#多设备开发中默认关)的「自检」 | 否 |
| `--root`、`--extra-root` | `backends/codex/workspace_reader.py` | 见 `--help` | 否 |
| `--home`、`--start`、`--idle`、`--max`、`--dy` | `feishu/web_client.py`（`bin/covey_feishu_web.py`） | 见 [浏览器服务](proxy.md#浏览器服务) | 否 |
| `--app-id`、`--home`、`--plan`、`--login-from`、`--console`、`--login-seconds` | `feishu/app_console.py`（`bin/covey_app_console.py`） | 见[自动补齐飞书应用](operations.md#自动补齐飞书应用) | 否 |
| `--profile`、`--app-id`、`--review`、`--json`、`--python`、`--login-from`、`--work` | `service/app_check.py`（`bin/covey_app_setup.py`） | 见[自动补齐飞书应用](operations.md#自动补齐飞书应用) | 否 |
| `--reconfigure`、`--skills`、`--non-interactive`、`--data-home`、`--workdir`、`--keep-login`、`--no-browser`、`--no-autostart`、`--profile`、`--app-name`、`--no-start`、`--browser-login-dir`（验收用，隐藏，见[运维](operations.md#服务托管)） | `service/installer.py`（`./install`、`bin/covey_install.py`） | 见[安装](install.md#选项) | 否 |
| `--home`、`--console`、`--shots` | `feishu/install_browser.py`（`bin/covey_install_browser.py`，`./install` 起的浏览器会话） | 见[安装](install.md#它做了什么) | 否 |

## 已下架项与旧配置

兼容旧记录、旧任务进程和旧卡片的代码都带 `# compat(<提交>): …; retire when …` 注释。`scripts/compat_report.py --state <状态目录> --harness-config <Harness 配置>` 按这些注释数出本数据根里还有多少东西要它。它只读运行者自己的数据根，所以删掉一处兼容之前，要每个部署者各跑一次，那一项在所有部署里都是 0（“可删”）才能删；“不知道”不算 0。

| 已下架的 | 旧配置或旧话题现在会怎样 |
| --- | --- |
| Harness：`neeko`、`codex-w`、`claude-w`、`neeko-next`、`kiro`、`doubao`（`core/roster.py` 的 `RETIRED`），以及名单外的任何 Harness ID | 旧话题里发消息或点卡片，只回“本话题使用的 <名字> 已下架，不能继续。…”，不执行；日志带 `outcome=retired_harness`。Bridge 启动时把它们没结束、也没有存活任务进程的任务标为中断并通知一次，存盘的排队任务丢弃。`harnesses.json` 里的这些键忽略。话题、任务记录、原生会话和旧状态目录留在原处，机器人不读也不删 |
| Claude 的供应商 TRAE（`traex/…`）、豆包（`doubao/…`）、Kiro 共享网关（`kiro-shared/…`）（`core/providers.py` 的 `RETIRED_PREFIXES`） | 启动卡、换配置卡和保存的上次选择都不再给出它们的模型。停在它们上面的话题发消息时回复“本话题的模型来自已下线的供应商「…」…请点任务卡上的“🔁 换配置”…”，换配置后照常继续。旧 `harnesses.json` 里的 `--traex-ppe-env`、`--traex-site` 还能解析，值被忽略 |
| Claude 网关档位（`--claude-profiles` 配置文件、包装命令、手工 pending/ready、`profile/<id>/<模型>` 选项；`core/providers.py` 的 `RETIRED_PREFIXES`） | Covey 只用本机自己装好并登录的 Claude Code 或 Codex。启动卡、换配置卡不再列 `profile/…` 模型，保存的上次选择、排队任务和旧话题里的 `profile/…` 不启动，回复“本话题的模型来自已下线的供应商「Claude 网关档位」…请点任务卡上的“🔁 换配置”…”。`--claude-profiles` 仍能解析，值被忽略，设了启动记一行 `no longer used`；配置文件留在盘上，不再读 |
| 已删除的 Claude 网关模型（形如 `<网关>/<模型>`、不在当前目录里） | 不再列出；停在它上面的话题在启动 Claude Code 之前失败，卡上提示换配置 |
| `--claude-transport`、`--codex-transport` | 只为旧命令行和旧 `harnesses.json` 能解析，只有一个值 |
| 后端参数的 16 个环境变量（`core/ignored_env.py` 的 `IGNORED_ENV`） | 不再读取，设了会在启动日志提示换成哪个参数；`LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` 设了非默认值会让诊断和部署预检失败，bootstrap 拒绝注册（见上文[不再读取的变量](#服务清单里的环境变量)） |
| Ferry 自动加速（`COVEY_FERRY_AUTO_ACCELERATE`） | 不再读这个变量。Codex 话题运行 `ferry-*` 模型时单独发的“加速”“超频”和别的消息一样，按普通消息或补充处理 |
| Codex 的 Magpie 供应商（`magpie/<分组>/<模型>`，`core/providers.py` 的 `RETIRED_PREFIXES`） | 启动卡和换配置卡不再列出它的模型，`COVEY_MAGPIE_URL` 不再读。停在它的模型上的话题发消息时回复“本话题的模型来自已下线的供应商「Magpie」…请点任务卡上的“🔁 换配置”…”。服务命令行里的 `--codex-magpie-url` 还能解析，值被忽略，设了非空值时启动记一行 `no longer used`。旧的 `<state>/native-models/magpie.json` 缓存留在盘上，不再读 |
| `main-thread.json` 的 `desk_topics`（前台话题布局） | 读到 `true` 也按 `false` 处理，启动时记一行 warning，保存设置时原值留着。以前开的前台话题（登记在 `<state>/main-thread/desk/`）由启动时的迁移 `desk-retire` 冻结，不再接新消息 |
| 脱敏标签的“新话题默认开”（`redaction/config.json` 的 `default_on`） | 不再读：新话题默认开不开脱敏是设置卡「新话题」页的“脱敏”一行（`main-thread.json` 的 `topic_defaults.redaction`）。启动时的迁移 `redaction-default` 在它开着时把那一行打开一次；值留在 `config.json` 给旧版本。旧卡上点这个开关只说它挪到了哪里 |
| 文字命令、斜杠命令 | 以 `/` 开头的消息和以前的命令词（“代答”“本周用量”“管理工作区”“换配置”“停用本群”等）当作普通任务或补充。只有配对口令 `/pair` 还认。应用里残留的原生斜杠命令要在开发者后台删除并发布 |
| 旧菜单键 `manage_workspaces`、`redaction_dashboard`、`manage_groups`、`butler_settings`（`proxy_settings` 2026-11-19 起又是正式的「代答」项） | 照旧能点（`app/bot_menu.py` 的 `KEYS` 和各功能的 `menu_items`）；自动补齐不删它们，也算进 5 项上限 |
| 独立的敏感词网页看板（`COVEY_DASHBOARD`、`COVEY_DASHBOARD_URL`，扩展钩子 `dashboard_entry` 和它的 `COVEY_DASHBOARD_ENTRY`），2026-11-19 去掉 | 不再读这几个变量，清单里留着也照常启动；扩展的 manifest 还写着 `dashboard_entry` 钩子也照常加载（`core/extensions.py` 的 `RETIRED_HOOKS`），核心不再调它。状态目录里的 `redaction/dashboard-token` 和 `redaction/dashboard-entry.json` 不再用，可以删（`app/bot_menu.py` 还从后者读旧链接卡发去的私聊）。旧链接卡和设置卡「脱敏」标签上的“换口令”（`bridge_dashboard_rotate`）点了只说网页看板已经去掉；旧菜单键 `redaction_dashboard` 照旧打开「脱敏」标签 |
| 两卡式或 schema 3 之前的旧启动卡 | 点了只回“此启动卡已失效，请在主聊天重新发送消息开始任务。”，不改表单和等着的消息 |
| 其他已下线卡片的按钮 | 见[已下线的功能](usage.md#已下线的功能) |

旧安装：

- **`.local-state` 目录**：代码目录下已有 `.local-state/<组件名>` 时，按组件解析的默认路径继续用它，优先于 `COVEY_DATA_HOME`；显式 `--state-dir` 仍然最优先，bootstrap 不看它。解析到它时 stderr 记一行 `warning: legacy .local-state path resolved: …`。请把它移到数据根下，后续版本不再读取。`python3 -B bin/covey_paths.py <组件名>` 只读显示解析结果，`--new` 忽略 `.local-state`。
- **单后端机器人的旧状态目录**（`codex-lark-bridge/`、`claude-lark-bridge/` 等）留在原位，统一机器人不读也不删。要继续旧工作，在统一机器人里新开话题交接。
- **同一应用升级为统一机器人**：先停掉旧服务，避免同一应用有两套消费者；再显式用原目录作 `--state-dir`。旧话题默认归属 Codex。换新应用时必须用新的状态目录和 owner。
- **没有自动迁移**：任务、检查点和后端原生记录里有绝对路径。要搬状态目录时先停服务和任务，保留备份和旧路径，验证后再切换。
- **旧 Skill**：`scripts/install_skills.sh` 发现仍装着 `lark-codex-bridge`、`lark-claude-bridge`、`lark-neeko-bridge`、`lark-<旧名>-bridge`（旧 Skill 名，compat）或 `cr-mr-review-loop` 时给警告，不自动删除。

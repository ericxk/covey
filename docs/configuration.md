# 配置

本文写给部署机器人的人：机器人从哪几处读配置、每个可选功能怎么打开、全部环境变量和参数，以及旧配置还能不能用。服务怎么注册、部署和维护见[运维指南](operations.md)，排障见[排障](troubleshooting.md)，各 Harness 的参数见 [Harness 说明](harnesses.md)。

## 配置放在哪

| 放在哪 | 管什么 | 怎么改 |
| --- | --- | --- |
| 服务命令行 | Bridge 自己的参数（`--workdir`、`--profile`、`--app-id`、`--state-dir`、`--owner`、`--harness-config`）和默认 Harness（Codex）的参数 | 重新注册（`scripts/bootstrap_harness_bridge.sh` 或 `scripts/deploy_bridge.sh`） |
| `harnesses.json`（`--harness-config`） | 其余 Harness 的参数和模型顺序 | 改文件后重启（`restart` 或重新注册） |
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
| `personal_groups` | `[]` | chat_id 列表：这些群是主人的个人空间，只服务主人（含 `owner_accounts`），不会被激活。个人群里：主人不用 @ 机器人（飞书要把群里全部消息发给机器人，应用需有 `im:message.group_msg` 权限；没有时仍要 @）；顶层消息交给该群自己的主线程（话题、回带、问题卡都在群里；主线程按 chat 分开记录，不和私聊的混）；机器人置顶一张菜单卡（用量、看板、设置），点击仍只认主人；手上的事、话题看板、早晚汇总各自有一份（记录名带 chat_id，见 `docs/main-thread.md`）；答案卡顶上带状态行，和私聊一样 |

- 旧键 `owner_only`（取反即 `allow_team_spaces`）、`owner_aliases`、`home_chat`、`agent_groups` 仍可读，各映射到上表的字段，`spaces` 里写了同名字段时以它为准；启动日志每个旧键记一行 `… is deprecated: write spaces.…`。`group_menu`、`group_boards`、`owner_headed` 不再是开关：个人群自带这些，旧键只读入，启动日志各记一行说明。手上的事、看板、早晚汇总仍照原来的环境变量（`COVEY_PINNED_OVERVIEW`、`COVEY_PINNED_TOPICS`、`COVEY_DIGEST_TIMES`）开关。
- `owner_accounts` 里的账号私聊机器人时也走主线程：每个账号的私聊记在 `<state>/owner-chats.json`（open_id → chat_id），不覆盖主人本人的 `owner-chat.json`；它点机器人菜单，回在它自己的私聊里。
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

只有部分部署要用的功能默认关闭：没配置时不起线程、不加菜单项、不改已有行为。打开的办法都是在注册服务的终端里设变量，再重新 `launch`。

| 功能 | 怎么打开 | 打开后多什么 |
| --- | --- | --- |
| [代答](#代答) | `COVEY_PROXY_PROFILE` 和发送工具 | `proxy`、`proxy-in` 线程；设置卡「代答」能用 |
| [敏感词看板](#敏感词看板) | `COVEY_DASHBOARD` | `dashboard` 线程（本机 HTTP） |
| [手上的事与话题看板](#手上的事与话题看板) | `COVEY_PINNED_OVERVIEW=1`、`COVEY_PINNED_TOPICS=1` | `overview` 线程；Pin 话题时多一个已读事件消费者 |
| [早晚汇总](#早晚汇总) | `COVEY_DIGEST_TIMES` | `digest` 线程、菜单「看板」 |
| [知识条目与历史搜索](#知识条目与历史搜索) | `COVEY_KNOWLEDGE=1`；`COVEY_HISTORY_CMD=/absolute/path/to/kb-history` | 主线程多 `knowledge_*` 工具，话题首轮带知识条目，汇总多一节；主线程多 `history_search`，话题规则多一行查历史的命令 |
| [主线程](#主线程) | 默认允许；`COVEY_MAIN_THREAD=0` 不允许 | `main-tools`、`main-search` 线程；设置卡的私聊、新话题标签 |
| [管家规则](#管家规则) | `COVEY_MAIN_BUTLER=1` | 主线程只快答和分派，不在自己这一轮里干几分钟的活；其余规则不变 |
| [群聊免@](#群聊免) | `COVEY_GROUP_ALL_MESSAGES=1` 加应用权限 | 设置卡「群聊」每个群多一个“免@” |
| [飞书 SDK 与直连](#飞书-sdk-与直连) | `COVEY_CARD_TRANSPORT=sdk`；直连默认开 | 卡片和消息走常驻 SDK 客户端 |
| [自动补齐飞书应用](operations.md#自动补齐飞书应用) | 默认开；`COVEY_APP_CHECK=0` 关 | `app-setup` 线程 |
| [内存看门狗](operations.md#内存看门狗) | `bin/covey_memguard.py enable` | 独立进程；`memguard` 线程转发告警 |
| [本机运行锁](runtime-locks.md) | `COVEY_LOCK_ROOT` | 同机多个服务共用的锁 |

### 代答

[代答](usage.md#代答)要两样东西（实现见[代答](proxy.md)）：

- `COVEY_PROXY_PROFILE`：带主人本人登录的 lark-cli profile，要有 `im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`、`search:message`、`contact:user:search` 和 `offline_access`。用户 token 的刷新期是 7 天；轮询每分钟都在用它，正常会一直续上，代答关掉超过 7 天会过期。过期后 `lark-cli --profile <它> auth login` 重新授权。
- 发送器（扩展钩子 `proxy_sender`，见[发送器](proxy.md#发送器)）：没有发送器就没有代答入口。公司部署的发送器的安装与配置见扩展自己的 README。

验收页面（`bin/covey_feishu_web.py serve|accept`，见[浏览器服务](proxy.md#浏览器服务)）另用一个装了 Playwright 的 Python，例如 `$COVEY_DATA_HOME/proxy-web/venv/bin/python`，代答发送不用它。主人的网页端登录只留 `<state>/proxy/web/profile`（0700）一份，不进备份：

- 别的工具要用就复制一份到自己的运行目录，不要直接打开它（同一个 profile 只能有一个 Chromium）。
- 复制前确认 `<state>/proxy/batches/` 里没有 `status` 为 `sending` 的批次，并排除 `Cache`、`Code Cache`、`Service Worker`、`GPUCache`、`Dawn*Cache`、`IndexedDB`、`Singleton*`。
- 它的缓存目录合计超过 200 MiB 时，维护线程（每小时看一次）在没有发送中的批次、没有进程用着它时删掉这些缓存（`core/maintenance.py` 的 `WEB_CACHES`），日志 `proxy web login caches removed: N MiB`。cookie 和登录不受影响。

代答的日志行见[排障 · 代答](troubleshooting.md#代答没有回复或没有发出)。

### 敏感词看板

脱敏的词表、开关和替换记录在设置卡的「脱敏」标签上（`app/redaction_panel.py`，卡片是 `cards/redaction_panel.py`），不用配置，所有部署都有。网页看板（`app/dashboard.py`，页面在 `app/dashboard_static/`）可选：设了 `COVEY_DASHBOARD` 才起，它的链接是「脱敏」标签最后一块。

- 「脱敏」标签：一层（填词、搜索、词表，折叠面板里是开关和记录，试一下和上游地址的输入框点开再出现（折叠面板里放不了表单）；替身词自动生成，`update_config` 拒绝新给词指定 `stand_in` 或 `anywhere`，config.json 里已有的照旧生效），每次点击只改词表的一处，在 `core/redaction.py` 的 `update_config` 里读、改、存，一次一个，然后整卡重画。卡上不出现原词：词只写首字和字数（`masked`），自动替身不显示；按钮按 `<state>/redaction/card-key`（0600，第一次用时生成）下的 HMAC 指认哪一行；查找只记下找到的行，框里输入的内容不写回卡片；拒绝的原因（`Refused`）不提原词，词表文件读不了时只说读不了（原来的错误可能引用原词）。“撤销”只记在 Bridge 进程的内存里，重启后就没有了。「探测」在后台线程跑，跑完重画卡片。

- `COVEY_DASHBOARD`：监听地址。写 `[::]:10601`（IPv6 双栈）、`0.0.0.0:10601`，或只写端口（等于 `[::]:端口`）。设了才起看板线程。写法里的 `$名字`、`${名字}` 每次启动时从环境里取值，取不到就不起看板；平台按机器分配端口时，用单引号把 `'[::]:$端口变量'` 原样存进清单。起来后日志记 `dashboard listening on :: port N`；端口被占或写错时只记一次 `dashboard not started: …`，Bridge 其他部分照常。
- `COVEY_DASHBOARD_URL`（可选）：「脱敏」标签上网页看板的链接用的地址。不设时用本机默认路由的 IPv6 地址（有些开发机给预留端口登记的就是它），本机没有 IPv6 或只监听 IPv4（`0.0.0.0:端口`）时用本机能解析的完整主机名；端口取 `COVEY_DASHBOARD` 的。不用主机名打头，是因为浏览器可能只按 IPv4 解析它（Chrome 没有通往公网 IPv6 的路由时就这样），而开发机要经 IPv6 访问。浏览器所在网络到不了这个地址时，用这个变量改成它能到的地址。
- 看板固定入口（`dashboard_entry` 扩展钩子）：公司部署用 `COVEY_DASHBOARD_ENTRY` 配，见扩展自己的 README。没有钩子时看板照常，没有固定入口。
- 地址变了就推送：主人打开过设置卡「脱敏」标签后，看板记下链接发去的私聊和地址；之后某次启动发现地址不同，就把带口令的新链接发到这个私聊，每个地址只发一次。记录在 `<state>/redaction/dashboard-entry.json`。
- 口令：`<state>/redaction/dashboard-token`（0600），第一次用时生成。卡上是带口令的链接（`/?t=<口令>`）；打开后口令换成 cookie `hb_dash`（HttpOnly、SameSite=Strict，30 天）。点“换口令”（kind `bridge_dashboard_rotate`）后旧链接和已登录的浏览器马上失效。
- 安全：看板是内网明文 HTTP，同网段能抓包的人看得到口令。同一地址一分钟内输错 5 次口令就锁一分钟（429）。写操作要带请求头 `X-Dashboard: 1`，不开放跨域，页面不许被嵌入。`bridge.log` 每个请求记一行 `dashboard <方法> <路径> <状态>`，不含查询参数、口令、cookie 和词；读不成 HTTP 的请求（多半是浏览器用 https 连了这个 http 端口）记 `dashboard bad request <状态>`，不记内容。
- 词表在 `<state>/redaction/config.json`（0600），每轮开始时读，改动下一轮生效；每轮的替换次数记在 `<state>/redaction/turns.jsonl`。「探测」在后台跑一次 `<claude 命令> --gateway-status`，只显示结果、不改配置。

### 手上的事与话题看板

- `COVEY_PINNED_OVERVIEW=1`：在主人私聊里置顶一张「手上的事」汇总卡（见[手上的事](usage.md#手上的事)）。只认 `1`。主人的私聊取 `<state>/owner-chat.json`（主人最近一次私聊机器人时记下），还没有就不发。卡片记录在 `<state>/pinned-overview.json`。置顶用 `im pins create`，机器人发消息的权限就够。日志：`pinned overview sent|pinned|retired …`，失败是 `pinned overview not sent|pin failed|update failed`。
- `COVEY_PINNED_TOPICS=1`：在「手上的事」上面自动 Pin 话题（见[话题看板](usage.md#看板自动-pin-话题)）。只认 `1`，而且要 `COVEY_PINNED_OVERVIEW=1` 也开：由「手上的事」的线程管，它那张卡置顶之后才开始 Pin。最多 10 个话题（`app/pins.py` 的 `MAX_PINS`），顺序变了才动，最多每 60 秒一轮。私聊的 `im pins list` 读不到东西，Bridge 只信自己的记录 `<state>/pinned-topics.json`。`<state>/board-priority.json`（话题 → 时间）让话题在本组排最前，由主线程的工具写。看板从不撤回消息。日志：`pinned board pin|unpin|move|dismissed message=… topic=… group=…`，失败是 `pinned board failed op=… failures=N`。
- 已读事件：开了 `COVEY_PINNED_TOPICS=1` 时 Bridge 多起一个消费者 `lark-cli event consume im.message.message_read_v1`（线程 `topic-seen`，`app/topic_seen.py`）。只认主人读的消息；每条在主人私聊的话题别名里查，查到的话题记下最近一次读到的时间，写进 `<state>/topic-seen.json`（90 天没读的和超出 2000 个的较早话题删掉）。开发者后台要订阅「消息已读」事件，应用要有 `im:message` 和 `im:message:readonly`；缺了消费者打印 `missing_scope` 后退出，每 300 秒重试，其余功能照常，健康检查不看它。

关掉：去掉变量后重新 `launch`。已置顶的卡和已 Pin 的话题留在原处，在私聊的 Pin 页里手动取消；记录文件留着不再更新。

### 早晚汇总

`COVEY_DIGEST_TIMES=09:30,23:30`：逗号分隔的 HH:MM，按 Asia/Shanghai，重复的只算一次，写错的跳过并记日志。每到一个时间点，Bridge 在主人私聊的顶层发一张汇总卡（`app/digest.py`、`cards/digest.py`；用户看到什么见[早晚汇总](usage.md#早晚汇总)）。不设或一个合法时间都没有就不起线程，也没有菜单「看板」。

- 记录在 `<state>/digest.json`：最后发出的时间点 `last_slot`、发送中的 `pending`、最近 6 张发出的卡 `sent`。一个时间点只发一次：Bridge 在时间点停着、60 分钟内起来就补发一次，超过就跳过。卡先存再发；没确认的 60 秒后用同一个幂等键再发，最多 3 次。
- 数据从记录读（`questions/`、`jobs/` 的索引），不持记录锁、不调模型，主线程话题不列。
- 菜单「看板」（事件 key `digest_board`）只在设了这个变量时有：发同一张卡、按现在取数，不改 `last_slot`。卡的记录在 `<state>/digest-boards/`。要在开发者后台的机器人菜单里加这一项（自动补齐会加）。
- 日志：`digest sent slot=HH:MM`、`digest not sent: nothing to list`、`digest not sent try=N`、`digest not sent after N tries; given up`、`digest card redrawn message=… cleared=N`、`digest board sent`。

### 知识条目与历史搜索

两个变量互相独立，都只在主人自己的部署里设；没设时不建目录、不改规则和提示词、主线程的工具清单里没有对应的工具、汇总里没有这一节。设计和实现见[主线程 · 知识库](main-thread.md#知识库)，主人怎么用见[使用指南 · 知识条目](usage.md#知识条目)。

- `COVEY_KNOWLEDGE=1`（只认 `1`）：打开知识条目。记录在 `<state>/knowledge/`；主线程多 `knowledge_save`、`knowledge_update`、`knowledge_drop`、`knowledge_search`；每个话题的新会话带上条目，续接的会话只补上一轮之后新增或改动的；主人在话题里明确说“记一下 / 记住 / 记下来”时，Bridge 在那一轮结束后把检查点的 `remember` 记下来；早晚汇总多“新记的知识”。
- `COVEY_HISTORY_CMD=/absolute/path/to/kb-history`：历史搜索命令的**绝对路径**（相对路径等于没设）。设了以后，主人自己的话题规则里多一行“要查以前做过的事用 `<命令> search <一句话>`”，主线程多 `history_search`（跑 `<命令> search [--workspace W] [--since Nd] -- <一句话>`，自己的进程组里最多等 70 秒，超时整组杀，输出最多 20 KB）。命令本身不在这个仓库。
- 关掉：去掉变量、重新 `launch`；已经写下的 `knowledge/` 留着。
- 日志：`knowledge remember written=N job=#…`、`knowledge remember ignored reason=… job=#…`、`knowledge remember failed job=#…`；主线程工具的 `main tool name=knowledge_save …`。

### 主线程

`COVEY_MAIN_THREAD` 决定部署允不允许主线程（私聊模式 Personal Agent，见[主线程](main-thread.md#私聊模式)和[使用指南 · 主线程](usage.md#主线程)）：

- `0`：不允许。不起线程、不改消息路由，设置卡没有主线程的标签。
- 不设或 `1`：允许。用不用由主人在设置卡上选的模式定（`<state>/main-thread.json` 的 `mode`），改模式不用重启。新部署第一次启动写成 Personal Agent，日志 `main thread mode settled mode=agent|topics`。
- 主线程的设置在 `<state>/main-thread.json`，工具请求在 `<state>/main-thread/requests/`。主线程的会话只能用 Claude Code。
- `main-thread.json` 里旧的 `"desk_topics": true` 读到也按 `false` 处理，启动时记一行 warning，见[已下架项与旧配置](#已下架项与旧配置)。

### 管家规则

`COVEY_MAIN_BUTLER=1`（只认 `1`）：主线程的规则里，“只读的、一两分钟能做完的自己做完直接答”这一条换成管家规则：几秒能答完的直接答，要查、要跑、要改、要花几分钟的用 `topic_start`、`topic_send` 派给话题区，对主人只说一句放到哪个 T 号、大概多久。主人明确说“你直接做”时例外。其余规则一条不动；没设时每轮提示词和原来逐字节相同（`tests/golden/main_thread_prompt.txt`）。规则全文和旧会话什么时候用上新规则，见[主线程 · 管家规则](main-thread.md#管家规则)。

- 打开：在注册服务的终端里设 `COVEY_MAIN_BUTLER=1`，重新 `launch`。关掉：去掉变量、重新 `launch`。
- 不影响话题的规则、工具清单和卡片；跑过 `CARD_AFTER`（90 秒）才出卡的兜底照旧，没有另设变量。

### 群聊免@

`COVEY_GROUP_ALL_MESSAGES=1`：主人可以按群选谁说话不用 @ 机器人（见[在群里用](usage.md#在群里用)）。只认 `1`。

- 要先给应用开通 `im:message.group_msg`（获取群组中所有消息，敏感权限，可能要企业管理员审批）和 `im:chat.members:read`（选人时列群成员），并发布新版本。
- 设置记在 `<state>/groups/<…>.json` 的 `free_talk`、`free_talk_members`。不设变量时卡上没有免@，这些设置不生效。
- 开通权限后机器人所在每个群的全部消息都会推给 Bridge：没人免@ 的消息在准入一步丢掉，不写历史、不记日志。日志：`group chat=… free_talk=…`；被拒的免@ 消息是 `ignored message … reason=for_others|proxy_chat`。

### 飞书 SDK 与直连

- `COVEY_CARD_TRANSPORT=sdk`：CardKit 卡片接口、消息的发送和回复、读消息详情都用常驻的 `lark-oapi` 客户端；上传文件和图片、转发话题等只有 lark-cli 做得了的仍走 lark-cli（见[投递、重投与 outbox](architecture.md#投递重投与-outbox)）。先在运行 Bridge 的那个 Python 里 `python3 -m pip install '.[feishu-sdk]'`。SDK 只支持显式命名的 profile，应用密钥只在内存里读取。请求发出前取不到凭据时退回 CLI（日志 `CardKit SDK unavailable locally; using CLI`）；请求已发出后不换传输重发。
- `COVEY_FEISHU_DIRECT`：默认直连飞书开放平台，`0`（也认 `false`、`no`、`off`）时都走环境里的代理。直连时 Bridge 启动先试一次连 `open.feishu.cn:443`（lark 品牌是 `open.larksuite.com`），连得上就把它加进本进程的 `no_proxy`、`NO_PROXY`，之后的子进程都继承，日志 `Feishu direct: lark-cli reaches open.feishu.cn without the proxy`；连不上记 `Feishu direct unavailable (<异常类型>); lark-cli keeps the proxy`，环境不动。事件长连接的主机不在其中，仍走代理。

### 其他

- `COVEY_APP_CHECK=0`：不检查飞书应用、不发「飞书应用待补」卡。检查和补齐见[自动补齐飞书应用](operations.md#自动补齐飞书应用)。
- `--health-timeout`：`launch` 的参数，默认 120 秒，0 关闭。Bridge 本地健康接口连续这么久没有响应就替换进程。bootstrap 不传它，所以经 bootstrap 注册的都是 120。
- `COVEY_PRIVACY_DENYLIST`（只给 `scripts/privacy_check.py` 读，Bridge 本身不读）：指向仓库外的文本文件，每行一个公司域名或内部名词，隐私检查按它查；不设就只查通用规则，扩展里放有一份，见[隐私检查](testing.md#隐私检查)。

## 环境变量与选项一览

**命名（改名 Covey 后）**：设置的名字是 `COVEY_<X>`，旧名 `LARK_BRIDGE_<X>` 照常生效，不用改任何配置。`covey/core/env.py` 的 `get` 先读 `COVEY_<X>`、再读 `LARK_BRIDGE_<X>`（新名优先；只设了旧名时，进程的环境里读到旧名会在 stderr 记一行弃用，每个名字一次）。下表只写新名；每一行的旧名就是把 `COVEY_` 换成 `LARK_BRIDGE_`。`LARK_<HARNESS>_BRIDGE_*`（`LARK_CLAUDE_BRIDGE_*` 等 Harness 自己的前缀）和已不再读取的名字不变。

- **服务清单**：`runtime_environment()` 仍按旧名存（`COVEY_X` 存成 `LARK_BRIDGE_X`），所以升级不改清单，回滚到旧 release 也读得到；`scripts/deploy_bridge.sh` 的环境比对把清单和终端里的 `COVEY_` 名都当旧名比，只改了名字不算变化。Manager 启动服务时两种名字都设进服务进程（清单里的值盖过 Manager 自己环境里的同名设置），所以旧 release 的任务进程也读得到。
- **Bridge 给子进程设的变量**（`COVEY_TASK_DIR`、`COVEY_MAIN_STATE`、`COVEY_MAIN_JOB`、`COVEY_RUNNER_PYTHONPATH` 等）两种名字都设；找任务遗留进程（`core/task_processes.py`、内存看门狗）认两种。
- 下一个版本起清单才改存新名，旧名读取再保留至少一个版本。

代码在 `covey/` 和 `bin/` 里读的每个环境变量和命令行参数都列在这里，`tests/test_structure.py` 的 `InventoryTest` 核对：代码读了就要有一行，行里写出读它的文件；表里写了的代码也要读。不列 `HOME`、`PATH`、`PYTHONPATH`、`XDG_CONFIG_HOME`、`NO_PROXY`/`no_proxy` 这类系统变量。不再读取的环境变量不在表里，只有两个权限变量还为拦截而读（见[服务清单里的环境变量](#服务清单里的环境变量)）。

| 环境变量 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `COVEY_DATA_HOME` | `core/paths.py` | 按 [数据目录](operations.md#数据目录) 推算 | 否 |
| `COVEY_SERVICE_ROOT` | `core/paths.py` | `<数据根>/bridge-services` | 否 |
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
| `COVEY_DASHBOARD` | `app/dashboard.py` | 空：不开看板 | 否 |
| `COVEY_DASHBOARD_URL` | `app/dashboard_entry.py` | 空：`http://[<本机默认路由的 IPv6 地址>]:<端口>`，没有 IPv6 时 `http://<本机名>:<端口>` | 否 |
| `ANTHROPIC_BASE_URL` | `app/dashboard.py`、`backends/claude/redaction.py`（脱敏中继的上游） | 空 | 否 |
| `COVEY_PINNED_OVERVIEW` | `app/overview.py`（`app/pins.py` 的 `laid` 也读：两个开关都开时看板记录的规则才算数） | 关；`1` 开 | 否 |
| `COVEY_HARNESS_PROBE` | `app/availability.py`（`PROBE_ENV`；探测本身在 `core/harness_probe.py`） | 关；开了才每 60 秒的可用性检查里对 Claude、Codex 各做一次真实调用（缓存 6 小时，失败 10 分钟）。缓存里是“没登录”时该 Harness 在启动卡上不可用，网络、超时等只写进健康检查。可由服务清单保存 | 否 |
| `COVEY_GROUP_ALL_MESSAGES` | `app/access.py`（`free_talk_on`；群聊卡 `app/group_manager.py` 经它） | 关；`1` 开（还要应用开通 `im:message.group_msg`） | 否 |
| `COVEY_DIGEST_TIMES` | `app/digest.py`、`core/app_manifest.py`（有它才要菜单「看板」） | 空：不发早晚汇总 | 否 |
| `COVEY_KNOWLEDGE` | `core/knowledge.py`（`enabled`）、`core/main_thread.py`（`mcp_config` 交给主线程的 MCP 服务）、`core/main_tool_specs.py`（`listed`：MCP 服务据此列知识工具） | 空：不开知识条目 | 否 |
| `COVEY_HISTORY_CMD` | `core/knowledge.py`（`history_cmd`）、`core/main_thread.py`（`mcp_config` 同上）、`core/main_tool_specs.py`（`listed`：据此列 `history_search`） | 空：没有历史搜索；要绝对路径 | 否 |
| `COVEY_EXTENSIONS` | `core/extensions.py` | 空：读仓库根的 `extensions/enabled`（没有这个文件就没有扩展）；设成空串表示一个都不要。逗号分隔的 Python 包名，见[扩展](architecture.md#扩展) | 否 |
| `COVEY_EXTENSION_PATH` | `core/extensions.py` | 空：仓库根的 `extensions/`。`os.pathsep` 分隔的目录，扩展包在这些目录里找 | 否 |
| `COVEY_MAIN_BUTLER` | `app/prompts.py`（`butler_on`） | 空：关；`1` 把主线程规则的首条换成管家规则（只快答和分派） | 否 |
| `COVEY_MAIN_THREAD` | `core/main_thread.py` | 允许（模式看 `main-thread.json`）；`0` 不允许 | 否 |
| `COVEY_TRACK_JUDGE` | `app/track_judge.py`（多路并行里，规则没能定的新消息交给小模型判断是并进哪一路还是新开一路；这一包只有函数，接线在后） | `claude`：Claude Code 的 Haiku 起一次性进程；`off` 不调，直接新开；`cmd:/absolute/path` 用自己的命令（标准输入一段 JSON，标准输出一行 JSON）。超时、出错、拿不准、脱敏话题都按新开一路 | 否 |
| `COVEY_TRACK_JUDGE_MS` | `app/track_judge.py` | 2000：小模型硬上限（毫秒），超过按进程组杀掉、按新开一路；取值限制在 200 到 10000 | 否 |
| `COVEY_MAIN_STATE` | `core/main_mcp.py`（主线程任务的 MCP 配置写进去，告诉工具服务 Bridge 的状态目录） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_MAIN_JOB` | `core/main_mcp.py`（主线程任务的任务键，Bridge 据此核对调用） | 无；缺了每次调用都回错误 | 否 |
| `COVEY_PINNED_TOPICS` | `core/topic_seen.py`（看板 `app/pins.py` 和已读消费者 `app/topic_seen.py` 都经它） | 关；`1` 开（还要 `COVEY_PINNED_OVERVIEW=1`） | 否 |
| `COVEY_PROXY_PROFILE` | `app/proxy.py` | 空：代答不可用 | 否 |
| `COVEY_APP_CHECK` | `app/app_setup.py` | 开：检查飞书应用、缺了发卡；`0` 关 | 否 |
| `COVEY_PLAYWRIGHT_PYTHON` | `feishu/app_config.py`（`playwright_python`：自动补齐用的解释器） | `<数据根>/proxy-web/venv/bin/python` | 否 |
| `COVEY_CONSOLE_URL` | `app/app_setup.py`（交给 `feishu/app_console.py` 的 `--console`） | `https://open.feishu.cn`（登录后跟着跳到租户的域名） | 否 |
| `COVEY_LOCK_ROOT` | `core/file_lock.py`、`service/install.py` | 不设：不用本机运行锁（见[本机运行锁](runtime-locks.md)） | 否 |
| `COVEY_MANAGED` | `app/unified.py` | Manager 启动 Bridge 时设成 `1`，不要手设 | 否 |
| `COVEY_REQUIRED_MOUNT` | `service/manager.py` | 开机守护设置，不要手设 | 否 |
| `MERLIN_JOB_ID` | `service/boot.py` | 开发机自带 | 否 | <!-- privacy: allow 平台自带的环境变量名，boot.py 按它认开发机 -->
| `COVEY_RUNNER_PYTHONPATH` | `app/runner.py` | Bridge 交给任务进程，不要手设 | 否 |
| `COVEY_TASK_DIR` | `app/leftovers.py` | 任务进程给本轮命令设置，不要手设 | 否 |
| `CODEX_HOME` | `core/paths.py`（`codex_home`，Codex 的会话目录和 Pi 借用的登录都从它取）、`core/model_settings.py` | `~/.codex` | 否 |
| `CLAUDE_CONFIG_DIR` | `backends/claude/sessions.py`、`backends/claude/redaction.py` | `~/.claude` | 否 |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`、`CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING` | `backends/claude/bridge.py`（Bridge 的环境里设了就用它的值，否则给 claude 子进程设成 `1`） | `1` | 否 |
| `DSH_HOME` | `backends/dsh/bridge.py` | `~/.dsh` | 否 |
| `LARK_BRIDGE_CODEX_SANDBOX`、`LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS` | `core/ignored_env.py`（不再设默认值，只为拦截而读：Bridge 启动日志、`service/doctor.py`、`scripts/deploy_bridge.sh`、`scripts/bootstrap_harness_bridge.sh`） | 不设 | 是：已不生效；服务命令行写 `--sandbox`，`harnesses.json` 写 `--no-skip-permissions` |

Bridge 的参数：默认 Harness（Codex）和 Bridge 自己的写在服务命令行，其余 Harness 的写进 `harnesses.json`（见 [Harness](harnesses.md)）。默认值以 `--help` 为准。

| 参数 | 谁读 | 默认 | 弃用 |
| --- | --- | --- | --- |
| `--harness` | `bin/covey_bridge.py` | `unified`（唯一的选项） | 否 |
| `--state-dir`、`--workdir`、`--profile` | `app/harnesses.py`（每个 Harness；非默认的由 Bridge 填） | 状态目录、当前目录、`COVEY_PROFILE` | 否 |
| `--harness-config`、`--owner`、`--app-id`、`--pairing-file` | `app/unified.py` | 空 | 否 |
| `--run-dir`、`--await-handoff`、`--compact-only` | `app/runner.py`（任务进程，Bridge 填；`--compact-only` 是主线程空闲时的压缩进程，`app/idle_compaction.py`） | 无 | 否 |
| `--model`、`--reasoning-effort`、`--sandbox`、`--codex-cmd`、`--codex-timeout` | `backends/codex/bridge.py` | `gpt-6-astra`、`max`、`danger-full-access`、`codex`、0（不限） | 否 |
| `--codex-transport` | `backends/codex/bridge.py` | `app-server`（唯一的选项） | 只为旧命令行能解析 |
| `--codex-magpie-url` | `backends/codex/bridge.py` | 值被忽略；设了非空值启动时记一行 `no longer used` | 只为旧服务命令行能解析（`compat_report: codex-magpie-url`） |
| `--model`、`--effort`、`--claude-cmd`、`--claude-timeout`、`--no-skip-permissions`、`--claude-login-shell`、`--claude-auto-compact-window`、`--claude-compact-between-turns` | `backends/claude/bridge.py` | `claude-fable-5`、`xhigh`、`claude`、0、关、关、空、空 | 否 |
| `--claude-profiles` | `backends/claude/bridge.py` | 已下线，值被忽略，设了启动记一行；见[已下架项与旧配置](#已下架项与旧配置) | 否 |
| `--claude-transport`、`--traex-ppe-env`、`--traex-site` | `backends/claude/bridge.py` | 值被忽略 | 只为旧 `harnesses.json` 能解析（`compat_report: removed-options`） |
| `--model`、`--reasoning-effort`、`--dsh-cmd`、`--dsh-timeout` | `backends/dsh/bridge.py` | 见 `--help` | 否 |
| `--model`、`--reasoning-effort`、`--pi-cmd`、`--pi-timeout` | `backends/pi/bridge.py` | 见 `--help` | 否 |

`scripts/deploy_bridge.sh` 自己的参数不在这张表里（表只列 Bridge 读的）：`--ext-repo`、`--ext-commit`、`--allow-core-mismatch` 让它把核心检出和扩展检出拼成一个 release，不给时只部署 `--repo` 一个检出，`--ext-commit` 默认 `HEAD`，用法见[部署新版本](operations.md#部署新版本)。

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
| 文字命令、斜杠命令 | 以 `/` 开头的消息和以前的命令词（“代答”“本周用量”“管理工作区”“换配置”“停用本群”等）当作普通任务或补充。只有配对口令 `/pair` 还认。应用里残留的原生斜杠命令要在开发者后台删除并发布 |
| 旧菜单键 `manage_workspaces`、`redaction_dashboard`、`proxy_settings`、`manage_groups`、`main_thread_settings` | 照旧能点（`app/bot_menu.py` 的 `KEYS` 和各功能的 `menu_items`）；自动补齐不删它们，也算进 5 项上限 |
| 两卡式或 schema 3 之前的旧启动卡 | 点了只回“此启动卡已失效，请在主聊天重新发送消息开始任务。”，不改表单和等着的消息 |
| 其他已下线卡片的按钮 | 见[已下线的功能](usage.md#已下线的功能) |

旧安装：

- **`.local-state` 目录**：代码目录下已有 `.local-state/<组件名>` 时，按组件解析的默认路径继续用它，优先于 `COVEY_DATA_HOME`；显式 `--state-dir` 仍然最优先，bootstrap 不看它。解析到它时 stderr 记一行 `warning: legacy .local-state path resolved: …`。请把它移到数据根下，后续版本不再读取。`python3 -B bin/covey_paths.py <组件名>` 只读显示解析结果，`--new` 忽略 `.local-state`。
- **单后端机器人的旧状态目录**（`codex-lark-bridge/`、`claude-lark-bridge/` 等）留在原位，统一机器人不读也不删。要继续旧工作，在统一机器人里新开话题交接。
- **同一应用升级为统一机器人**：先停掉旧服务，避免同一应用有两套消费者；再显式用原目录作 `--state-dir`。旧话题默认归属 Codex。换新应用时必须用新的状态目录和 owner。
- **没有自动迁移**：任务、检查点和后端原生记录里有绝对路径。要搬状态目录时先停服务和任务，保留备份和旧路径，验证后再切换。
- **旧 Skill**：`scripts/install_skills.sh` 发现仍装着 `lark-codex-bridge`、`lark-claude-bridge`、`lark-neeko-bridge`、`lark-<旧名>-bridge`（旧 Skill 名，compat）或 `cr-mr-review-loop` 时给警告，不自动删除。

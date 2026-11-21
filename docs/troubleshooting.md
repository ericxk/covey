# 排障

本文按症状写：先看什么、怎么修。顺序是第一次装好后遇到问题的先后：装不上 → 扫码后没反应 → 发消息、点按钮没反应 → 模型调不通 → 任务出问题 → 代答、飞书应用、权限 → 慢。服务的组成和日志见[运维指南](operations.md)，开关和环境变量见[配置](configuration.md)，卡上的提示见[使用指南](usage.md)。

## 先找到数据根和 APP_ID

几乎每条诊断都从状态目录里的 `bridge.log` 开始。用 `./install` 装的没设过任何变量也能找到，在仓库目录里：

```bash
python3 -B bin/covey_doctor.py                      # 先跑一次诊断；不用设变量，退出码 0 就绪，2 降级
export COVEY_DATA_HOME="$(python3 -B bin/covey_paths.py)"   # 数据根，默认是仓库同级的 <仓库目录名>-data
ls -d "$COVEY_DATA_HOME"/harness-cli_*              # 状态目录，目录名里是 APP_ID
STATE_DIR="$COVEY_DATA_HOME/harness-cli_xxx"        # 换成上一行看到的
tail -n 100 "$STATE_DIR/bridge.log"
```

| 文件 | 内容 |
| --- | --- |
| `$STATE_DIR/bridge.log` | Bridge 的日志，下文说的日志行都在这里 |
| `$STATE_DIR/bridge-console.log` | Bridge 接管日志之前的输出（导入出错的 `Traceback` 在这里） |
| `$STATE_DIR/runs/<任务键>/runner.log` | 一个任务的任务进程的日志 |
| `$COVEY_DATA_HOME/install-logs/` | `./install` 每一步的日志 |
| `$COVEY_DATA_HOME/bridge-services/manager.log` | Manager 的日志 |

服务从 `releases/` 下的 release 运行时，`covey_paths.py` 推算的数据根不对，先 `export COVEY_DATA_HOME=/absolute/covey-data`（`./install` 装的就是仓库同级那个目录）。其余变量见[运维 · 找到数据根和 APP_ID](operations.md#找到数据根和-app_id)。Bridge 用的是服务清单里的环境，不是你登录终端的；看它实际的环境变量用 `tr '\0' '\n' < /proc/<pid>/environ`（pid 见 `covey_service.py status --state-dir "$STATE_DIR"` 的 `status.process.pid`）。doctor 每一项的含义见[健康诊断](#健康诊断)。

## 按现象找

| 现象 | 看哪一节 |
| --- | --- |
| `./install` 停在某一步、报错退出 | [安装停住或失败](#安装停住或失败) |
| 装完了、扫完码，私聊发消息没反应 | [扫码后机器人没反应](#扫码后机器人没反应)，再看[发消息后没有反应](#发消息后没有反应) |
| 以前好好的，现在发消息没反应 | [发消息后没有反应](#发消息后没有反应) |
| 机器重启后机器人没回来，或服务起不来 | [服务没有起来](#服务没有起来) |
| 点卡片按钮没反应 | [点按钮没有反应](#点按钮没有反应) |
| 启动卡上没有某个 Harness 或模型 | [启动卡里缺少某个 Harness 的模型](#启动卡里缺少某个-harness-的模型) |
| 模型调不通、报权限或限流 | [模型调用报权限错误](#模型调用报权限错误) |
| 机器要代理才能上网 | [机器要代理才能连模型](#机器要代理才能连模型) |
| 卡上说工作目录不存在 | [工作目录不存在](#工作目录不存在) |
| 任务中断、没起来，或卡上只说“连接发生异常” | [任务中断或没起来](#任务中断或没起来) |
| 结果、补充或回答“未确认”“待处理” | [送达未确认与投递待处理](#送达未确认与投递待处理) |
| 代答不回、不发 | [代答没有回复或没有发出](#代答没有回复或没有发出) |
| 网页工作台没内容、点了没反应 | [网页工作台 · 连不上时](web.md#连不上时) |
| 「飞书应用待补」卡、菜单少了一项或点了没反应 | [飞书应用缺权限或菜单](#飞书应用缺权限或菜单) |
| doctor 或部署报 `permission_env` | [权限变量让诊断或部署失败](#权限变量让诊断或部署失败) |
| 命令以 137 或 134 结束 | [命令被内存看门狗结束](#命令被内存看门狗结束) |
| 出卡片、点按钮慢 | [慢在哪里](#慢在哪里) |
| 想知道 doctor 报告每一项的意思 | [健康诊断](#健康诊断) |

## 安装停住或失败

安装器停下时最后一行是“安装停在这里：<原因>”，每步的完整输出在 `$COVEY_DATA_HOME/install-logs/<步骤>.log`。修好后重新运行 `./install`，它跳过已做完的、接着做缺的；常见原因和做法见[安装 · 出问题](install.md#出问题)。

## 扫码后机器人没反应

- 安装器说“在等你扫完手机第 N 步”：扫完后订阅约半分钟内生效，在那之前机器人收不到消息；也可以重新运行 `./install` 确认。
- `bridge.log` 最近一次启动后要有 `ready event_key=im.message.receive_v1` 和 `ready event_key=card.action.trigger` 两行。没有：飞书应用的事件订阅、卡片回调或版本发布没做完，私聊里会有「飞书应用待补」卡（见[飞书应用缺权限或菜单](#飞书应用缺权限或菜单)），或重新运行 `./install`。
- 有这两行但发消息还是没反应：看下一节。扫码的人就是主人，用别的账号发的消息机器人不处理。

## 发消息后没有反应

| 先看什么 | 怎么修 |
| --- | --- |
| `bridge.log` 最近一次启动后有没有 `ready event_key=im.message.receive_v1` | 没有：看 `consumer exited rc=` 和 doctor 的事件消费者一段；查应用的订阅、权限、发布是否都在同一个应用上 |
| `bridge.log` 有没有这条消息的 `ignored message … reason=…` | 有：消息被准入挡下。`not_owner` 是发消息的人不是主人（机器人只服务一个人，同事要自己部署）；群里 `not_mentioned` 是没有 @ 机器人；`group_inactive` 是这个群没对群成员开放：还在等你确认、你点了“先不启用”或停用了，见[在群里用](usage.md#在群里用) |
| 有 `timing flow=message` 却没看到卡 | 看同一行的 `outcome` 和后面的 `feishu call … result=`：飞书拒收时有错误码 |
| 在话题里发消息没有新任务 | 话题里有待答问题或没送达的结果时，消息按回答处理或被拦下，见[回答模型的问题](usage.md#回答模型的问题) |
| 机器人根本没在跑 | 看下一节 |

## 服务没有起来

`covey_service.py status --state-dir "$STATE_DIR"` 里能看到 `backoff`、`failures` 和 `retry_at`：Bridge 启动失败后由 Manager 按退避重试。按下表找原因：

| 去哪看 | 内容 | 处理 |
| --- | --- | --- |
| doctor 的 `manager` | socket 没有响应 | 没有 Manager：`covey_service.py start` 会拉起一个；装了开机自启的，看 `boot.watchdog` |
| `bridge-console.log` | Python 的 `Traceback`（导入出错等） | Bridge 在接管 `bridge.log` 之前就退出了 |
| `bridge-console.log` | `another bridge already owns this state directory` | 同一状态目录已有 Bridge 在运行 |
| `<root>/errors/<id>.json` | `state has a live unmanaged PID; stop/adopt its owner explicitly` | 有不归 Manager 管的进程占着状态目录。找到并停掉它，不要只删 `bridge.pid` |
| `launch` 的输出 | `another managed bridge already owns this profile` | 另一个状态目录用同一 profile 注册并在运行。先停掉它，或换 profile |
| `launch` 的输出 | `only explicit non-secret ...` / `only non-secret proxy URLs ...` | 终端里有值含换行的变量或带凭据的代理，见[服务清单里的环境变量](configuration.md#服务清单里的环境变量) |
| `bridge.log` | `owner is not verified; ...` / `pairing record is expired or belongs to another application` | 主人未确定，或配对口令过期、不属于此应用。按[启动机器人](manual-install.md#启动机器人)绑定或重新生成口令 |
| `bridge.log` | `explicit owner conflicts with the existing bot binding` | `--owner` 与 `bot-identity.json` 里已绑定的主人不同 |
| `bridge.log` | `state belongs to another Feishu application; ...` / `profile application identity does not match --app-id` | 状态目录属于别的应用（每个应用用自己的状态目录），或 profile 和 `--app-id` 不是同一个应用 |

机器重启后服务没回来：开机自启要单独安装，见[开机恢复](operations.md#开机恢复)。

## 点按钮没有反应

每次卡片点击到达 Bridge 时，`bridge.log` 写一行 `card action kind=<按钮类型> message=<消息 ID 末 10 位> event=<事件 ID 末 10 位> outcome=<结果> elapsed_ms=<耗时>`（表单按钮没有类型，记按钮名去掉令牌，开始任务记 `bridge_launch_submit`）。

找不到对应时间的这一行，说明点击没有到达机器人：机器人停着时点卡片都没有反应。看机器人是否在运行、`ready event_key=card.action.trigger` 是否在、卡片回调消费者是否正在等 300 秒重启，再查应用的 `card.action.trigger` 订阅、权限和发布状态。

找到时按 `outcome` 判断：

| outcome | 含义 |
| --- | --- |
| `handled` | Bridge 已处理。操作被拒绝时，话题里有一条“…未完成：原因”的回复 |
| `forwarded` | 任务进程还活着，点击整条转给它；去 `runs/<任务键>/runner.log` 找任务进程写的同样一行 |
| `workspace_manager`、`launch_option`、`directory` | 工作区管理卡、启动卡选项和目录选择，交给各自的后台队列处理 |
| `not_owner` | 点击者不是机器人绑定的主人，不处理也不回复 |
| `stale_route` | 卡片对应的 Harness 或话题绑定版本已过期，回复“此卡片已过期，请使用本话题最新的任务卡。” |
| `retired_harness` | 话题绑定的 Harness 已下架，回复下架提示，不执行，见[已下线的功能与旧配置](legacy.md) |
| `retired_topic` | 话题已冻结（旧版的前台话题），回复一句提示，什么都不做 |
| `retired`、`extension_missing` | 按钮类型不在当前接受的列表里（多为旧的设置卡、菜单卡），不处理；功能已移出或扩展没装时在卡下回一句说明 |
| `bad_value`、`other_topic` | 按钮的值无法解析，或记录的话题与卡片所在话题不一致，不处理 |
| `unknown_message` | 卡片所在的消息不在本机器人的话题记录里，也读不到消息详情 |
| `error` | 处理时抛出异常，看同一时间前后的日志 |

## 启动卡里缺少某个 Harness 的模型

看 `bridge.log` 里的 `launch catalog unavailable harness=<名称> error=<类型> detail=<原因>`。模型目录在后台每 5 分钟刷新一次，启动卡只列 15 分钟内成功读到的目录，所以一直读取失败的 Harness 会从启动卡上消失，新加的 Harness 要等下一轮（[模型目录](harnesses.md#模型目录)）。先查对应 CLI 是否在 Bridge 环境的 `PATH` 里、是否用同一系统账号登录过：

| Harness | 常见原因 | 怎么验证 |
| --- | --- | --- |
| 都没有 | Harness 不在 `harnesses.json` 的 `enabled_harnesses` 里 | 见[手工加一个 Harness](harnesses.md#手工加一个-harness) |
| Claude Code | 没登录（任务前会跑 `claude auth status`） | `python3 -B bin/covey_doctor.py --probe-harnesses` |
| Codex | 读不到 `~/.codex/models_cache.json`（在 CLI 里跑过一次就有），又没配模型网关 | 同上 |
| DeepSeek Harness | 没有声明给 dsh 用的模型网关，或网关密钥没配 | 日志的 `detail=` 写原因；配法见[最小网关示例](harnesses.md#最小网关示例) |
| Pi | 没有 Codex 登录也没配网关，或 `--pi-cmd` 不对 | 看 `detail=` |
| Antigravity | `agy` 没登录或 `agy models` 没回应 | 在服务的账号下跑 `agy models` |

已提交的任务在执行前再检查一次后端，不可用时话题里回复“后端暂不可用，任务尚未执行。”。

## 模型调用报权限错误

任务卡写“模型服务拒绝了当前身份或权限，请检查授权。”“模型服务限流或当前用量额度不足。”或“…工作区路由发现失败，请检查服务网络或代理配置。”时：

1. 飞书登录不等于模型登录，先查 Harness 自己的登录状态和供应商权限。
2. 看 Bridge 实际的 `PATH`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR` 和代理，缺少时按[服务清单里的环境变量](configuration.md#服务清单里的环境变量)重新注册。
3. 在同样的环境里发一次真实请求：`python3 -B bin/covey_doctor.py --probe-harnesses` 对 Claude Code、Codex 各调用一次，`harness_probe` 里每个的 `reason` 是 `ok`、`not_installed`、`not_logged_in`、`network`、`timeout` 或 `other`；别的 Harness 在服务的账号下直接跑一次它的 CLI。

## 机器要代理才能连模型

- 代理要在注册服务时写进服务清单：在终端里 `export HTTPS_PROXY=http://主机:端口` 后重新运行 `./install`（或按[改设置后重新注册](deploy.md#改设置后重新注册)），之后才设的代理不会进已在运行的服务。
- 代理地址不能带用户名、密码，否则注册时报 `only non-secret proxy URLs without credentials can be persisted`。
- 飞书的域名应当直连：`./install` 会把它们加进 `NO_PROXY`；Bridge 默认也会试一次直连飞书开放平台（`COVEY_FEISHU_DIRECT`，见[配置](configuration.md#飞书-sdk-与直连)）。
- 确认服务实际带了什么：`tr '\0' '\n' < /proc/<pid>/environ | grep -i proxy`；再用 `python3 -B bin/covey_doctor.py --probe-harnesses` 在服务的环境里真实调用一次。

## 工作目录不存在

- **启动卡提交时**：话题里回复“开始任务未完成：工作区目录不存在：…”，任务不启动。从旧启动卡选了已移出列表的工作区时，提示“未找到该工作区，可能已从列表移除；…”。换一个工作区或路径再点“开始任务”。
- **话题已开始、执行前发现目录不在**：任务卡写“本话题的工作目录不存在，后端没有启动。…”。常见原因是目录被删除或网络盘挂载丢失，恢复后点“检查并继续”。

## 任务中断或没起来

- **“任务已中断”**：任务进程退出了（机器重启、被 `kill`、崩溃）。看 `runs/<任务键>/runner.log` 的结尾和 `bridge.log` 的 `task runner ended job=#N … exit=…`（`exit=none` 是被杀或崩溃）。恢复卡 60 秒后自动检查并继续一次，见[自动重试](reference.md#自动重试)。
- **“任务未能启动”**：任务进程在调用模型前就退出了两次，或根本起不来（括号里写原因，例如 `BlockingIOError`，多是系统一时建不了进程）。
- **卡上只写“与执行后端的连接发生异常”**：看 `runner.log` 里的 `backend failure job=#N …`（Claude Code：失败的阶段 `stage`、异常类型 `exception`、原因 `reason`、退出码 `process_exit_code`/`exit_code`），下面几行 `backend stderr job=#N | …` 是 CLI 最后几十行 stderr（密钥已替换成 `***`）。不含 stderr 的摘要也在 `<state>/claude-diagnostics/<run id>.json`。取消的任务不写这几行。
- 要立刻结束一个任务进程，见[任务进程](operations.md#任务进程)。

## 送达未确认与投递待处理

机器人对可能已经发出的写操作不自动重发，规则见[结果授权与投递](architecture.md#结果授权与投递)。常见三类：

- **自动重试未确认**：自动重试提交时出错，或 Bridge 恰在提交途中停止，任务记录写 `auto_retry_state=uncertain`，卡片写“自动重试没有确认是否已提交，不会再自动重试；…”。先看话题里是否已有新的任务卡，没有再点“检查并继续”。
- **结果投递待处理**：任务卡写“已保存的结果尚未送达；不会重新执行模型任务。”。后台每 5 秒扫描，按退避重投已保存的结果，最多 12 次；发送结果不确定超过 3500 秒后进入待核对，停止自动发送。这时到飞书里核对结果是否已在话题里，不要手工换幂等键重发。恢复卡上的“重新投递结果”只重发已保存的回复，不重新执行任务。
- **补充或回答送达未确认**：补充转交任务进程后 8 秒内没有确认，卡片显示“送达未确认”；回答 45 秒内没有确认，提示“回答已转交当前任务，暂未确认送达；…”。两者都不会重复发送，可点“检查送达状态”，再看 `runner.log` 和任务进程是否存活。

## 代答没有回复或没有发出

代答的配置见[代答](configuration.md#代答)，行为见[使用指南 · 代答](usage.md#代答)。`bridge.log` 里的行：

| 日志 | 含义 |
| --- | --- |
| `proxy took in`、`proxy submitted` | 收进新消息、提交了代答任务 |
| `proxy skipped batch=…: the owner replied` | 主人已经亲自回了，不跑任务 |
| `proxy held batch=… job=#…: <原因>` | 关闭、暂停、过时，这批不发 |
| `proxy overtaken batch=…`、`proxy stop job=#N batch=…` | 对方的新消息顶掉了在跑的一轮；`proxy batch=… job=#N runs on: …` 是没顶掉的原因 |
| `proxy passed over batch=… job=#N: the later turn #M …` | 主人在话题里更新的一轮代替了这条回复 |
| `proxy send batch=… ok= type= submitted= message_id=`（发送器写的，在扩展里） | 一次发送；`type` 是发送工具的错误类型，`note=` 是已发出但回读没完成 |
| `proxy batch=… waits: the send tool's login is under way` | 发送工具正在扫码登录 |
| `proxy send tool logged out: the owner was told`（发送器写的，在扩展里） | 发送工具登录失效，私聊提示过一次；`proxy send tool login is back` 是恢复了 |
| `proxy batch=… failed: …` | 这个批次这一轮没推进，别的照常 |
| `proxy take-in failed`、`proxy tick failed` | 这一轮没收进新消息 / 轮询出错，下一轮照常 |
| `card action kind=bridge_proxy_… outcome=…` | 设置卡上的点击 |

批次记录在 `<state>/proxy/batches/`。用户 token 过期（刷新期 7 天）时，`lark-cli --profile <COVEY_PROXY_PROFILE> auth login` 重新授权。

## 飞书应用缺权限或菜单

应用该有哪些权限、事件和菜单，缺了怎么补，见[飞书应用配置](feishu-app.md)。排障时看这几处：

- 私聊里收到「飞书应用待补」卡：点“扫码自动补齐”。补齐失败时卡上写卡在哪一步和后台的报错原文，日志 `app setup run outcome=failed step=<步骤> …`，照手工步骤补即可。
- `bridge.log` 每次检查一行 `app setup check version=<线上版本> error=<-|no_self_manage|no_version|failed> lacking=N …`。`no_self_manage` 是应用缺 `application:application:self_manage`，卡上只列这一项，补上后下次检查才看得到其余的。
- 菜单项点了没反应：看 `bot menu key=… outcome=…`。`unknown_key` 是后台配的事件 key 和机器人认的不一致；没有这一行就是 `application.bot.menu_v6` 订阅没开或没发布。

## 权限变量让诊断或部署失败

doctor 的服务行带 `permission_env`、stderr 写 `service <id>: LARK_… is no longer read, so …`，或 `deploy_bridge.sh --check-only` 报 `pre-checks failed: [... 'permission_env' ...]`，或 bootstrap 报 `refused: LARK_…`：服务清单（或当前终端）里还留着以前收紧权限的变量，Bridge 已经不读它们（[不再读取的变量](legacy.md#不再读取的变量)），服务实际按放宽后的权限在跑。

| 变量 | 现在会怎样 | 改法 |
| --- | --- | --- |
| `LARK_BRIDGE_CODEX_SANDBOX` 不是 `danger-full-access` | Codex 按 `danger-full-access` 跑 | 服务命令行加 `--sandbox <原来的值>` |
| `LARK_CLAUDE_BRIDGE_SKIP_PERMISSIONS=0`（或 `false`/`no`/`off`） | Claude 跳过授权 | `harnesses.json` 的 `claude` 参数加 `--no-skip-permissions` |

然后在终端里 `unset` 这个变量，重新注册（[部署新版本](deploy.md#部署新版本)：`deploy_bridge.sh`，`--sandbox` 跟在 `--` 后面，命令行因此变了要加 `--allow-command-change`；或直接 bootstrap）。新清单里没有它，doctor 恢复 `ready`。

- 运行中的服务带它时 doctor 置 `degraded`，`--runtime-only` 也失败；停用的服务只在 stderr 提示、行状态仍是 `stopped`，只让完整报告降级，不影响 `--runtime-only`。
- 部署预检查目标服务的清单、当前终端和服务根下其他运行中服务的清单，后者命中时那一行以 `service <id>:` 开头。
- 其余不再读取的变量只在 `bridge.log` 记一行 `env <名字> is no longer read; …`，不拦，按那一行改完同样重新注册清掉。

## 命令被内存看门狗结束

命令以 137 结束、或 rg 以 134 结束（`memory allocation of N bytes failed`）时，先查服务根下 `memory-guard/events.jsonl` 和 `captures/`：多半是遍历的范围太大。缩小范围或改用已知清单，不要原样重跑或调大上限。规则见[内存看门狗](operations.md#内存看门狗)。

## 慢在哪里

从发消息到出卡片慢、点了按钮慢，先用汇总脚本看是哪一段：

```bash
python3 -B scripts/bridge_timing.py --state "$STATE_DIR" --since 19:30   # 可加 --until 20:00，或直接给日志文件
```

它读 `bridge.log`（含轮转的几份）和每个 `runs/*/runner.log`，按流程打印次数和 p50/最大值：飞书侧延迟、第一次写卡或发消息（first write）、两者之和（seen，约等于你看到点击有反应的时间）、总耗时、飞书 API 时间、Bridge 本地时间（总耗时减 API）和每个阶段。转给任务进程的点击按事件接上任务进程那一行；开始任务接上任务交接和任务进程启动。最后是按操作的飞书调用耗时。`--since`、`--until` 是最新一行那天的 HH:MM，也可以写完整时间。

要看单条，读日志里的计时行：每条消息、每次点击处理完写一行 `timing flow=`，每次飞书调用一行 `feishu call`：

```text
[2026-10-26T19:31:01.270+08:00] feishu call op=msg_reply api=im+messages-reply ms=400 proc_ms=350 via=cli attempts=1 result=ok thread=MainThread flow=message:8d509ee83b
[2026-10-26T19:31:02.510+08:00] timing flow=message outcome=launch_card message=8d509ee83b chat=p2p feishu_ms=910 event_ms=880 t0=1793014260100 total_ms=2410 api_ms=700 calls=2 phases=access:2,dedup:10,route:60,enrich:61,context:70,command:330,history:336,prepare:340,records:341,reserve:375,build:440,card_create:740,msg_reply:1170,saved:1200
```

- `feishu_ms`：飞书侧延迟，从消息的 `create_time`（点击用事件的 `timestamp`）到 Bridge 读到它，含两边的时钟差；`event_ms` 从事件生成时间算起。
- `phases=阶段:毫秒`：从读到事件起到每个阶段结束的毫秒数，前后两个相减是这一步的耗时。每次飞书调用也是一个阶段，用它的操作名（`card_create`、`msg_reply`、`card_put`、`msg_get`…）。`total_ms` 是总耗时，`api_ms`、`calls` 是其中的飞书调用；`jobs=#N` 是这次排上的任务。
- `feishu call`：`ms` 含回执落盘和重试，`proc_ms` 是 lark-cli 进程从启动到退出（`via=sdk` 时是 SDK 请求本身），`result` 是 `ok`、`receipt`（回执里已有，没再调用）或失败类别和错误码，`bytes` 是请求体字节数。`via=sdk` 时另有飞书响应头里的 `inner_ms`（飞书服务本身）和 `origin_ms`（含 `inner_ms` 的源站时间），`proc_ms` 减 `origin_ms` 是建连、代理和到边缘的路上。流程外的调用每分钟按 `op` 汇总一行 `feishu calls`。

这些行只有操作名、ID 末几位和数字，没有正文、令牌或用户文字。各流程的阶段：

| 行 | 流程 | 阶段 |
| --- | --- | --- |
| `flow=message outcome=launch_card` | 新消息 → 启动卡 | `access` 谁被服务、`dedup` 去重、`route` 话题、`enrich`（`detail=fetched`、`parent=fetched` 读了消息详情、被引用的消息；`parent=saved` 用的是话题第一次读到后存下的那份）、`command` 待送达的结果和问题、`records`、`reserve`、`build`、`card_create`、`msg_reply`、`saved` |
| `flow=message outcome=queued_card` | 话题里追发 → 排队卡 | 同上，之后 `ack_query` 查本话题等回答的任务（有则 `ack_upsert`）、`alias` 卡片别名、`state_write` 写任务记录、`recorded`、`queued` |
| `flow=menu outcome=<event_key>` | 点机器人菜单 | `dedup`、`chat`（找到主人私聊），之后是那张卡的调用 |
| `flow=click kind=bridge_launch_submit` | 开始任务 | `lane` 排到本话题的点击后台线程、`checked`、`bound`、`ack` 点击处理完；之后同时进行：`beside` 之后的 `card_put` 是回执，`followup` 之后是任务卡 |
| `flow=click outcome=forwarded` | 任务进程还活着时点任务卡（停止、补充等） | `forward` 写进任务进程的收件箱；`runner.log` 里同一 `event` 的 `flow=click … runner=#N` 是任务进程处理它，`inbox_ms` 是在收件箱等了多久 |
| `flow=job_start job=#N` | 调度器取到任务 → 任务进程启动 | `queued_ms` 排队、`check` 后端检查（Claude 是 `claude auth status`，1–2 秒；5 分钟内通过过就沿用）、`spawned` |
| `flow=runner job=#N`（`runner.log`） | 任务进程启动 → 它的第一次飞书调用 | 从 Bridge 写交接文件算起：`app`、`built`、`started`、`backend`（启动后端），然后是第一次调用；`snapshot` 是工作目录的 git 快照。`prestarted=yes` 是建任务卡之前就拉起的任务进程，`app` 接近 0。`sdk_client` 是建好 SDK 客户端（`COVEY_CARD_TRANSPORT=sdk` 时在后台线程 `sdk-warmup` 里与任务开始并行，顺带预热到飞书的连接）；建不成记 `CardKit SDK warmup unavailable: <异常类型>`，第一次卡片写再建或退回 CLI |

## 健康诊断

出问题先跑 doctor：

```bash
python3 -B bin/covey_doctor.py
python3 -B bin/covey_doctor.py --runtime-only     # 退出码只看运行状态，报告仍含开机守护
python3 -B bin/covey_doctor.py --skip-events      # 只查本地状态，不调用 lark-cli
python3 -B bin/covey_doctor.py --probe-harnesses  # 另对 Claude Code、Codex 各真实调用一次模型，说明调不通的原因
```

doctor 只读：不发消息、不修复状态；默认不调用模型，只有带 `--probe-harnesses` 才对 Claude Code、Codex 各发一次最小请求（其他 Harness 不探测）。不用设任何变量，`--root` 默认就是服务根。退出码 0 表示所选范围就绪，2 表示降级，或配置读不出来（这时只输出 `status: unavailable`）；某一项的 `unverified` 是这一项没法验证（比如设备还没自检），不一定让整体降级。它依次检查：

1. **开机守护**（`boot`）：按开机注册回执（`boot-installation.json`）的 `mode` 判断。`systemd-user` 要单元已注册、已启用、在运行；`launchd` 要已加载；`cron` 要 crontab 里还有那一行 `@reboot`；`manager`（没有可用的托管、有意不托管）固定算就绪；扩展提供的托管方式看它的守护是否在运行、心跳是否正常。没有回执（没注册开机自启）时这一段是 `unverified`，整份报告不算就绪，所以只关心服务现在是否在跑时（如部署后的检查）用 `--runtime-only`。
2. **Manager**：本地 socket 能否响应、进程身份是否匹配、控制循环延迟是否小于 120 秒。
3. **每个服务**（`services`）：进程是否存活、代次是否与清单一致；Bridge 健康快照（`bridge`）是否为 `ready`，即两个必需的消费者都就绪且单个事件处理不超过 60 秒、所有后台线程存活、事件总线不是停滞或断开；清单里有没有不再读取、会放宽权限的变量（`permission_env`，见[权限变量让诊断或部署失败](#权限变量让诊断或部署失败)）。
4. **事件消费者**（除非 `--skip-events`）：`lark-cli event status --current --json` 里属于这个 Bridge 的消费者是否覆盖 `im.message.receive_v1` 和 `card.action.trigger`（`events.owned_required_consumers`），丢弃计数是否为 0。菜单和已读两个消费者是可选的，不算在内。开了 `COVEY_EVENT_TRANSPORT=sdk` 时没有 lark-cli 事件总线这一层，doctor 看 Bridge 自己上报的长连接消费者（`events.scope` 为 `sdk_websocket`）。

报告的边界：

- 顶层 `model_access: not_probed`：这次没带 `--probe-harnesses`，`ready` 不代表模型可用；带上后是 `probed`，结果在 `harness_probe`（不带时那里是上次探测的缓存）。每个服务的 `bridge.model_access` 下有 `preflight`（程序是否存在，Claude 是否登录）和 `observations`（`backend-health/` 里最近任务的结果和扩展 Harness 模型目录查询结果，超过 15 分钟的标 `stale`）。
- CLI 不暴露 WebSocket 心跳，报告标 `websocket_heartbeat: not_exposed_by_cli`，订阅就绪只说明本地事件总线就绪。
- doctor 不检查任务进程。

### 自动恢复

- **消费者**：规则见[服务组成](operations.md#服务组成)。已就绪的订阅不会因为没有消息而重启。事件处理超过 60 秒时报告降级，但不重放该事件，也不杀任务。
- **事件总线**：每 30 秒检查本 profile 事件总线的 TCP 收包时间，超过 10 分钟算停滞。连续三次停滞、总线上的消费者全部属于这个 Bridge、距上次恢复超过 10 分钟时，向总线进程发 SIGTERM，5 秒后仍在就 SIGKILL，消费者随后重连，日志 `event bus TCP transport stalled`。总线 IPC 连续三次无响应、且之前确认过总线由本 Bridge 独占时同样替换。和别的消费者共享的总线不会被终止。模型任务都不受影响。

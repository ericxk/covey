# 运维指南

本文写给维护机器人服务的人：服务由哪些进程组成、数据放在哪里、怎么注册和日常操作、怎么部署新版本、开机恢复、备份维护、内存看门狗和日志。

- 第一次安装见[安装](install.md)；旧脚本和手动配飞书应用见[旧安装脚本与手动配置](#旧安装脚本与手动配置)。
- 开关、环境变量和参数见[配置](configuration.md)。
- 出了问题按症状查[排障](troubleshooting.md)。
- 内部实现见[架构与开发说明](architecture.md)。

下文命令都在机器人代码目录（仓库检出或 release 目录）里执行，并假设已设置：

```bash
export COVEY_DATA_HOME=/absolute/harness-bridge-data
SERVICE_ROOT="${COVEY_SERVICE_ROOT:-$COVEY_DATA_HOME/bridge-services}"
APP_ID=cli_xxx
STATE_DIR="$COVEY_DATA_HOME/harness-$APP_ID"
```

`SERVICE_ROOT` 是服务根目录，和 Manager、doctor、`covey_service_install.py` 不带 `--root` 时的默认值一样。`python3 -B bin/covey_paths.py service-root` 打印同一个路径。Bridge 和任务进程的 shell 里没有 `COVEY_SERVICE_ROOT`，不要直接用它。

只操作自己的服务。不要对所有应用执行全局的事件总线停止。

## 服务组成

```text
开机守护（扩展托管的 PID 1 里的线程，或 systemd 用户单元）
└─ Manager：bin/covey_service.py run，每个服务根一个
   └─ Bridge：bin/covey_bridge.py --harness unified，每个机器人一个
      ├─ 事件消费者（lark-cli event consume 子进程）
      │   ├─ im.message.receive_v1          收消息
      │   ├─ card.action.trigger            卡片点击
      │   ├─ application.bot.menu_v6        机器人菜单（可选订阅）
      │   └─ im.message.message_read_v1     已读（只在 COVEY_PINNED_TOPICS=1 时）
      ├─ 工作线程：scheduler、cards、menu、maintenance、connections、availability、outbox、policy，
      │   加上打开了的功能各自的线程（proxy、dashboard、overview、digest、main-tools、app-setup 等）
      └─ 任务进程：bin/covey_runner.py，每个任务一个，在自己的会话里运行
          └─ 后端进程（codex app-server、claude、pi、dsh，或扩展的隔离解释器）

内存看门狗：bin/covey_memguard.py，独立于以上进程，由 Manager 拉起
```

| 组件 | 谁拉起、谁重启它 | 它退出时 |
| --- | --- | --- |
| Manager | 开机守护。没有 Manager 时，`covey_service.py` 的 `launch`、`start`、`stop`、`restart` 也会用执行它的那份代码拉起一个 | Bridge 和任务进程继续运行，下一个 Manager 按进程号、启动时间和 boot ID 核对后接管 |
| Bridge | Manager。意外退出（含退出码 0）按 5 秒到 5 分钟退避重启，稳定 5 分钟后清零失败计数；本地健康接口连续 120 秒无响应时替换 | 任务进程继续运行，新 Bridge 认领它们 |
| 事件消费者 | Bridge。启动 60 秒内没有输出 `[event] ready` 就替换；退出后 5 秒重启，卡片回调消费者异常退出时 300 秒后重启 | 任务不受影响 |
| 工作线程 | Bridge。异常退出 5 秒后重建，日志 `worker <名称> failed` | 任务不受影响 |
| 任务进程 | Bridge 在任务出队时启动 | 没到终态就退出时标为中断，不自动重跑，见[任务进程](#任务进程) |
| 内存看门狗 | Manager 每 30 秒看一次，已启用而没在运行时拉起 | 见[内存看门狗](#内存看门狗) |

- 一个状态目录同时只能有一个 Bridge（单实例锁）。不要手工再启动一个，也不要删 `bridge.pid` 来绕过检查。
- 一个 lark-cli profile 只能被一份运行中的服务清单占用。另一个状态目录用同一 profile 注册时，`launch` 报 `another managed bridge already owns this profile`。

**一条消息怎么走。** 消费者读到事件 → Bridge 判断谁被服务、找话题 → 发启动卡或排队卡 → 调度器取到任务，写交接文件、启动任务进程 → 任务进程启动后端、读后端事件、写任务卡、投递结果 → 退出。点击先到 Bridge：任务进程还活着时整条转进它的收件箱，否则由 Bridge 处理。详见[架构与开发说明](architecture.md)。

## 数据目录

数据根是 `COVEY_DATA_HOME`。没设时是代码目录同级的 `<代码目录名>-data`，从 `releases/<commit>/` 运行时会变成 `releases/<commit>-data`，所以部署时总是显式设置它。

```text
<数据根>/                          COVEY_DATA_HOME
├─ harness-<APP_ID>/               状态目录 <state>（--state-dir），一个机器人一个
├─ bridge-services/                服务根 <root>（COVEY_SERVICE_ROOT）
│  ├─ services/<id>.json           服务清单；<id> 是状态目录绝对路径的 SHA-256 前 24 位
│  ├─ status/<id>.json             Manager 观测到的状态：进程身份、代次、失败次数、Bridge 健康快照
│  ├─ errors/<id>.json             Manager 处理这个服务时正在出的错，处理成功后删除
│  ├─ manager.json、manager.log    当前 Manager 的进程身份、Manager 日志
│  ├─ boot-installation.json       开机注册回执
│  └─ memory-guard/                内存看门狗的现场、事件和告警
├─ releases/<commit>/              固定版本的代码
├─ worktrees/                      “独立 worktree”运行方式的检出
├─ <扩展名>-runtime/               扩展的隔离环境（公司扩展：<扩展>-runtime/，source/、venv/）
├─ proxy-web/venv/                 装了 Playwright 的 Python（自动补齐、验收页面）
├─ tools/<发送工具名>/<提交>/ 代答的发送工具
└─ harnesses.json                  建议放这里，--harness-config 指向它
```

状态目录是服务数据，不是项目输出目录：切换工作区不会移动它，不要提交、清空或复制给别人。各 Harness 自己的原生会话（例如 `~/.codex/sessions`、`~/.claude/projects`）不在这里。

### 状态目录

`<state>/` 下的内容按用途分组。“谁写”里的 Bridge 指常驻的 Bridge 进程，任务进程只改本任务的卡和记录。

```text
<state>/
├─ 进程与身份
│  ├─ bridge.pid                   当前 Bridge 进程号（单实例锁）
│  ├─ bridge.log*、bridge-console.log*   日志，见“日志”一节
│  ├─ bot-identity.json            应用 ID、owner、profile、机器人自己的 open_id；状态目录和应用一一对应
│  ├─ owner-chat.json              主人和机器人的私聊（菜单、手上的事、汇总都发在这里）
│  ├─ migrations.json              已做过的旧记录迁移（core/migrate.py）
│  ├─ maintenance-status.json、backups/   上次维护结果、每日备份
│  └─ tmp/                         Bridge 和任务进程的 TMPDIR
├─ 话题与任务
│  ├─ topics/<话题>/               topic.json、history.jsonl、收到的附件 incoming_media/、换配置的交接 handoff/
│  ├─ topics/aliases/              消息和卡片 → 话题的别名
│  ├─ jobs/、queued/               任务记录；存盘的排队任务，交给任务进程时删除
│  ├─ runs/<任务键>/                任务进程的运行目录，见“任务进程”一节
│  ├─ traces/<任务键>.jsonl        执行过程记录
│  ├─ checkpoints/                 每个任务的检查点（模型写）
│  ├─ questions/、held-notes/      待答问题；多题待答时暂存的补充
│  ├─ interaction-deliveries/      补充和回答的送达记录
│  ├─ delivery-receipts/           飞书写操作的幂等回执
│  ├─ inbound-messages/            收到的消息的去重标记
│  ├─ verified-results/、result-files/   已授权的回复正文、长回复文件
│  ├─ answer-cards/                单独发出的答案卡（补产物链接用）
│  ├─ session_index.jsonl、system-rules-sessions/、native-session-guards/   会话指针与续接保护
│  ├─ job-diffs/                   每轮的差异报告
│  ├─ artifact-drive/、artifact-deliveries/、artifact-upload/   产物上传到云空间的记录和中转
│  ├─ turn-exports/                “下载请求与结果”的打包记录
│  └─ policy-stops/、policy-inbox/、policy-source*.json   官方拒绝的停止记录（见 policy-stop.md）
├─ 卡片与表单
│  ├─ cards/                       卡片锁（.locks/）
│  ├─ launch-forms/、switch-forms/、rewind-forms/、browse-cards/、workspace-managers/   启动卡、换配置、回退、目录浏览、工作区管理的表单状态
│  ├─ launch-defaults/、launch-defaults.json   启动卡预选：每个聊天上一次的选择；最近一次的
│  ├─ model-usage.json             最近 14 天的模型选择，决定下拉顺序和常用思考预算
│  ├─ model-settings/              话题的模型设置
│  └─ menu-panels.json、settings-cards/、group-managers/、main-thread-panels/、weekly-usage-cards/、digest-boards/、dashboard-links/   菜单发出的各张卡
├─ 主人的设置
│  ├─ workspace-config.json        工作区列表和默认工作区
│  ├─ groups/                      每个主人 @ 过机器人的群：是否外部群、是否对成员开放、免@
│  ├─ principles.json              主人的原则（主线程的工具写）
│  ├─ knowledge/                   知识条目（`COVEY_KNOWLEDGE=1` 才有）：`owner.jsonl`/`.md`、`workspaces/<工作区>.jsonl`/`.md`、`owner-brief.md`
│  ├─ main-thread.json             私聊模式和主线程设置，子 Agent 的模型 `subagents`（`COVEY_MAIN_THREAD=0` 时设置卡没有它，默认 `light`，要跟主模型手写 `"same"`）
│  └─ redaction/                   脱敏词表、看板口令、每轮替换数
├─ 可选功能
│  ├─ main-thread/                 主线程的工具请求、开过的话题、回带记录
│  ├─ proxy/                       代答：settings.json、contacts/、style.md、batches/、send-as-user/、web/、shots/
│  ├─ pinned-overview.json、pinned-topics.json、board-priority.json、status-cards/、topic-seen.json   手上的事和话题看板
│  ├─ topic-links.json             卡上 T 号的话题链接
│  ├─ digest.json                  早晚汇总
│  └─ app-setup/                   自动补齐飞书应用的状态和运行目录
└─ 后端
   ├─ backend-health/              各后端最近一次任务的结果和模型目录查询结果，doctor 从这里读
   ├─ launch-models/、native-models/   模型目录缓存、扩展的实测档位
   ├─ pi-sessions/、acme/、<扩展>-sessions/   后端自己的会话和运行文件
   └─ claude-diagnostics/、<扩展>-diagnostics/   后端诊断
```

`runs/<任务键>/` 是 Bridge 和任务进程之间的协议目录，新旧 release 双向兼容；文件名和读写规则在 `core/run_dir.py`。

## 注册与启动服务

注册链路是 `scripts/setup_harness_bridge.sh` → `scripts/bootstrap_harness_bridge.sh` → `bin/covey_service.py launch` → Manager。setup 负责安装检查和登录（见[运行安装脚本](#运行安装脚本)）。已经装好的机器上，重新注册、换 release 或改参数时直接用 bootstrap：

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

`add` 的路径必须是已存在的目录，名称 1 至 40 个字符。`remove` 只从列表移除，不删目录；默认工作区要先把别的设为默认才能移除。`prune` 移除确认已不存在的目录。改动只影响之后开始的话题，实时生效。状态目录里还没有 `workspace-config.json` 时，命令会用当前目录建一个默认列表，所以先确认 `--state-dir` 写对了。

## 部署新版本

从固定的 release 目录运行，更新代码时不会改变正在运行的服务和任务进程用的文件。命令用到文首设置的 `STATE_DIR` 和 `SERVICE_ROOT`。

`scripts/deploy_bridge.sh --workdir <目录> --profile <profile> [-- <bridge 参数>]` 按第 1–4 步做完并打印一行 JSON（变量名同文首），检查不过就停；`--check-only` 只做部署前的检查，参数见脚本开头。其中权限变量（`pre.permission_env`）查目标服务的清单、当前终端和服务根下其他运行中的服务的清单，和部署后 `covey_doctor --runtime-only` 的口径一致（停用的服务只告警）。它还在结果里报 `post.app_config`（飞书应用缺不缺东西），不算失败。

**核心和扩展分开检出时**（核心在 `covey/` 检出，公司等扩展在各自的库里，布局：扩展库根下每个含 manifest 文件的目录是一个扩展包）：加 `--ext-repo <扩展检出>` 和 `--ext-commit <提交>`（默认 `HEAD`），都可重复，按先后配对，例如主人的开发机再加一个 Paseo 扩展库。`--repo`/`--commit` 仍是核心。

```bash
scripts/deploy_bridge.sh --workdir <目录> --profile <profile> --repo /absolute/covey --ext-repo /absolute/covey-internal --check-only
```

- 打包改由 `scripts/assemble_release.py` 做：核心 `git archive` 到 release，每个扩展包（扩展库根目录下含清单模块的目录）`git archive` 到 `release/extensions/<包名>/`，去掉包里的 `standalone/`，包的 `bin/*.py` 入口（如扩展自己的运行时脚本）放进 `release/bin/` 盖掉核心里的转发脚本，写 `extensions/enabled`（按 `--ext-repo` 的顺序列出所有包名）和 `RELEASE_MANIFEST`（各检出的完整提交号），再重写 `AGENTS.md` 的模块表。形状和旧的单仓库 release 相同，服务清单、`runs/<任务键>/` 协议、回滚都不变。拼装按提交取，不看检出的工作区。
- release id 是 `<核心前 8 位>-<扩展前 8 位>[-<第二个扩展前 8 位>…]`，release 目录、`bridge started … release=<id>` 的匹配都用它；结果 JSON 的 `commit` 仍是核心完整提交号，另有 `ext_commits` 和 `release_id`。不给 `--ext-repo` 时一切照旧（release 目录名是核心提交号）。回滚只回一半：核心提交不变、`--ext-commit` 设成旧值，得到一个新 id 的 release。
- 额外的预检（`--check-only` 同样做，输出里有两个或更多提交）：扩展检出是 git 仓库、提交存在（否则退出码 2）；扩展库的 `core.lock` 写的核心提交和 `--commit` 不一致时拦下（`pre.core_lock_mismatch`；试新核心时加 `--allow-core-mismatch` 放行）；服务清单和当前终端里不能有 `COVEY_EXTENSION_PATH`、`COVEY_EXTENSIONS`（`pre.extension_env`；它们会盖过 release 里的 `extensions/`，unset 后重新注册）。

**1. 看有没有会被打断的任务。**

```bash
python3 -B bin/covey_service.py status --state-dir "$STATE_DIR"
grep -l '"status": "creating_worktree"' "$STATE_DIR"/launch-forms/*.json
```

- `restart_blockers` 为空就可以部署。每项含 `key`、`job_id`、`status`，是任务记录未结束、又没有存活任务进程的任务；已存盘、重启后会重新排队的不算，点过停止而没有任务进程的也不算。不为空时等它们结束。
- 第二条命令列出正在创建独立 worktree 的启动卡。重启会中止检出，卡片换回启动表单，已建好的 worktree 保留。

**2. 导出 release。** 在已经拉取并合入的检出里：

```bash
COMMIT="$(git rev-parse HEAD)"
RELEASE="$COVEY_DATA_HOME/releases/$COMMIT"
mkdir -p "$RELEASE"
git archive "$COMMIT" | tar -x -C "$RELEASE"
```

**3. 核对清单和终端环境，再用新 release 重新注册。**

```bash
MANIFEST="$(grep -l "\"state_dir\": \"$(realpath "$STATE_DIR")\"" "$SERVICE_ROOT"/services/*.json)"
python3 -I -c 'import json, sys
m = json.load(open(sys.argv[1]))
print(" ".join(m["command"]))
print(sorted(m["environment"]))' "$MANIFEST"
cd "$RELEASE"
# 当前终端执行 launch 时会保存的变量
python3 -B -c 'from covey.service.manager import runtime_environment; print(sorted(runtime_environment()))'
```

- 两份变量名一致再继续：缺的在终端里 export，多出的不需要就 unset。`PATH` 里的 `python3` 就是之后运行 Bridge 的 Python。
- 在这个机器人自己的任务里部署也可以：新 Bridge 认领仍在运行的任务进程，当前任务不会中断。任务 shell 继承 Manager 的环境，可能多出清单里没有的变量（例如 `HTTPS_PROXY`），同样先对比。
- 清单命令里的 `--workdir`、`--profile`、`--app-id`、`--state-dir` 对应 bootstrap 的同名参数；其余参数原样跟在后面。owner 已写进 `bot-identity.json` 后可以省略 `--owner` 和 `--pairing-file`。

然后从新 release 运行 bootstrap，并显式给出 `--state-dir`：

```bash
./scripts/bootstrap_harness_bridge.sh --workdir /absolute/workspace --profile my-bot --app-id "$APP_ID" \
  --state-dir "$STATE_DIR" --harness-config "$COVEY_DATA_HOME/harnesses.json"
```

Manager 发现代次变化后停掉旧 Bridge，用新命令启动。Manager 本身仍从它启动时的 release 运行，见第 6 步。bootstrap 最后打印的 `status` 可能还是旧进程；以第 4 步为准。

**4. 部署后检查。**

```bash
python3 -B bin/covey_service.py status --state-dir "$STATE_DIR"
python3 -B bin/covey_doctor.py --runtime-only
tail -n 200 "$STATE_DIR/bridge.log" | grep -E 'ready event_key=|adopted task runner|re-queued job'
```

- `covey_doctor.py --runtime-only` 退出码为 0；新进程的 `bridge started pid=… generation=N release=<commit>` 之后有两行 `ready event_key=...`；`/proc/<pid>/cmdline` 里的入口路径是新 release（pid 见 `status.process.pid`）。
- 部署前在跑的每个任务都有一行 `adopted task runner job=#N pid=…`，任务卡的执行过程里多一条“机器人 HH:MM 重启过，任务未中断。”。
- 在 Bridge 的实际环境里发一次真实模型请求，例如在测试话题里跑一个只读小任务。doctor 不探测模型，登录终端里能调用不代表服务环境里也能。

**5. 保留旧 release。** 任务进程从头到尾用启动它的 release，`pgrep -af covey_runner.py` 的命令行里能看到路径。旧 release 要保留到用它的任务进程都结束；Manager 正在用的 release 也不能删。

**6. 需要时升级 Manager。** 部署只换 Bridge。升级 Manager 时先用新 release 的安装器重写开机配置（不加 `--activate`），再只结束 Manager，由开机守护按新配置拉起：

```bash
# 其他托管方式由扩展的 service_hosting 钩子提供（公司的托管模式见扩展自己的 README）：同样用新 release 的安装器重写配置，再结束 manager.json 记录的 Manager

# systemd 模式：用要运行 Manager 的 Python 运行安装器，再重启单元
python3 -B "$RELEASE/bin/covey_service_install.py" --root "$SERVICE_ROOT" --mode systemd
systemctl --user restart lark-bridge-manager.service
```

- 新 Manager 按进程身份接管存活的 Bridge，不重启 Bridge，也不打断任务进程。扩展的开机守护（如公司托管模式的）每一轮都重新读取 `boot.json`；systemd 单元是 `KillMode=process`，重启单元只结束 Manager。安装器每次都把回执重置为 `reboot_verified: false`。
- 安装器打印的回执里 `active_pid1_hook` 总是 `false`，不说明守护的状态。以 doctor 报告的 `boot.active_pid1_hook` 和 `boot.watchdog` 为准：刚结束 Manager 时可能先报 `false`，守护拉起新 Manager 后转为 `true`。
- 没装开机自启、或 扩展的守护没在运行（doctor 的 `boot.watchdog` 为空）时，结束 Manager 后由下一次 `covey_service.py start`、`restart` 或 `launch` 用执行它的 release 拉起。
- Manager 的日志行（`service <id 前 8 位> …`）是 Manager 的代码写的，升级 Manager 后才有新版本的写法。

**7. 更新 Skills。** 运行 `./scripts/install_skills.sh`，它把本仓库的 Skills 同步到 `${CODEX_HOME:-~/.codex}/skills` 和 `${CLAUDE_HOME:-~/.claude}/skills`，同名目录整个替换。安装 Skills 不会重启机器人。

**8. 需要时更新内存看门狗。** 部署不会更新看门狗的本地副本；要用新代码时从新 release 执行一次 `bin/covey_memguard.py enable`。

## 任务进程

每个任务出队后在自己的任务进程里跑完：后端、任务卡和结果投递都在这个进程里。它在自己的会话里运行，用启动它的 Bridge 的环境和 release，停止或重启 Bridge 都不会结束它。实现见[任务生命周期](architecture.md#任务生命周期)。

`pgrep -af covey_runner.py` 列出任务进程，命令行形如 `<release>/bin/covey_runner.py --run-dir <state>/runs/<任务键>`。运行目录里有：

| 文件 | 内容 |
| --- | --- |
| `runner.json` | 进程号、启动时间、boot ID、release、任务编号、Harness |
| `wake.json` | 任务进程能接 SIGUSR1：Bridge 写收件箱后用它立即唤醒（旧 release 的任务进程没有它） |
| `done.json` | 卡片更新完、只剩产物上传时写入；Bridge 见到就放开这个话题，同话题的下一条照常开始 |
| `exit.json` | 退出时写入 |
| `runner.log` | 任务进程的日志 |

- 运行目录在任务结束 7 天后由空闲维护删除。单看 `runner.json` 判断不了进程是否还活着，要看 `exit.json` 或进程列表。
- 任务结束后，任务进程还会等产物上传最多 30 分钟、卡片更新最多 60 秒再退出。
- Bridge 一侧的日志：`task runner started job=#N pid=... dir=...`（交给任务进程）、`adopted task runner job=#N pid=...`（重启后认领仍存活的）、`task runner done job=#N`、`task runner ended job=#N status=... exit=...`（`exit=none` 表示没有 `exit.json`，进程被杀或崩溃）。
- 主线程空闲时的压缩进程也是 `covey_runner.py`，命令行带 `--run-dir <state>/main-thread/compacting/<主线程话题> --compact-only`：不建任务、不出卡、不算 `restart_blockers`。Bridge 重启时停掉上一个 Bridge 留下的这种进程，请求放回去。

操作：

- **结束任务进程**：`kill -TERM <pid>`。它结束自己的后端进程后退出，任务按中断处理，不算“已停止”：Bridge 把卡片换成带“检查并继续”的恢复卡，60 秒后自动检查并继续一次（见[自动重试](usage.md#自动重试)）。要按“已停止”结束，点任务卡上的“停止”。后端已经做完、正在发结果时收到它，结果照常发出。
- **不要用 `kill -9`**：任务进程来不及结束自己的后端进程；Bridge 同样把任务标为中断。
- **命令留下的进程**：任务进程给后端和它的命令都加了 `COVEY_TASK_DIR=<运行目录>`。每轮后端关闭后，带这个标记、仍在运行的搜索工具被结束，其他进程只在任务卡的执行过程里列出，`runner.log` 有一行 `task leftovers job=#N stopped=… pending=… running=… pids=…`。找某个任务留下的进程：`grep -lza "^COVEY_TASK_DIR=$STATE_DIR/runs/<任务键>$" /proc/[0-9]*/environ`；结束时按进程组（`kill -TERM -- -<PGID>`）。
- **边界**：`covey_doctor` 不检查任务进程。机器人停着时任务进程照常刷新任务卡，但点击和消息都由 Bridge 接收，这段时间点卡片没有响应。

## 改名 Covey 后的兼容

产品和包改名为 Covey，所有现有部署不改配置照常升级；下面这些旧名都还能用，保留至少一个版本。

| 旧 | 新 | 兼容做法 |
| --- | --- | --- |
| Python 包 `harness_bridge` | `covey` | 仓库根的 `harness_bridge.py`（单文件，不是包，`covey/__init__.py` 仍为空）装一个导入别名：`harness_bridge.X` 就是 `covey.X` 同一个模块对象，首次导入在 stderr 记一行弃用。给外部扩展用 |
| `LARK_BRIDGE_<X>` | `COVEY_<X>` | 两个名字都读，见[环境变量与选项一览](configuration.md#环境变量与选项一览) |
| `bin/lark_harness_bridge.py`、`lark_task_runner.py`、`bridge_service.py`、`bridge_service_install.py`、`bridge_doctor.py`、`bridge_memguard.py`、`bridge_pairing.py`、`bridge_main_mcp.py`、`bridge_app_setup.py`、`lark_bridge_paths.py`、`lark_bridge_maintenance.py`、`lark_bridge_workspaces.py`、`lark_app_console.py`、`lark_feishu_web.py`、`lark_install_browser.py`、`lark_workspace_reader.py`（扩展的入口见扩展 README，同样留了旧名转发壳） | `bin/covey_bridge.py`、`covey_runner.py`、`covey_service.py`、`covey_service_install.py`、`covey_doctor.py`、`covey_memguard.py`、`covey_pairing.py`、`covey_main_mcp.py`、`covey_app_setup.py`、`covey_paths.py`、`covey_maintenance.py`、`covey_workspaces.py`、`covey_app_console.py`、`covey_feishu_web.py`、`covey_install_browser.py`、`covey_workspace_reader.py`（`covey_install.py 本来就是新名） | 旧名留作两行转发壳（`runpy.run_path` 新名），服务清单、开机钩子、crontab、systemd 单元、各部署的脚本里写死的旧路径继续有效；新注册（bootstrap、installer、`bridge_service_install` 写的单元和 crontab 行）用新名 |
| `lark-bridge-manager.service` | `covey-manager.service` | 新装用新名；已装旧单元的机器继续用旧单元名（`unit_name()` 看 `~/.config/systemd/user/` 里有没有旧文件），不停不换；单元里的 `ExecStart` 下次装时改成新入口 |
| 抽象套接字 `\0lark-bridge-…`、`~/.config/lark-bridge-service/`、`~/.local/libexec/lark-bridge/memguard.py`、crontab 标记 | 不变 | 新旧 Manager、看门狗要互相认得，数据和注册位置都不改名 |
| `scripts/setup_harness_bridge.sh`、`scripts/bootstrap_harness_bridge.sh` | 不变 | 现网 `deploy_bridge.sh` 还用；保留到 P6 |

数据目录（`COVEY_DATA_HOME`/`LARK_BRIDGE_DATA_HOME` 指的目录、`harness-<APP_ID>` 状态目录、`releases/`、`bridge-services/`、`runs/<任务键>/` 里的文件名）一个都没改。`deploy_bridge.sh` 比的是清单里的命令参数，不比入口脚本的路径，所以现网清单写着旧入口时不会因为命令变了而拦。

## 服务托管

注册服务和托管是两件事：服务清单记着“跑什么”，托管让 **Manager** 开机后自动起来、挂了自动拉起（Bridge 和任务进程由 Manager 管）。托管只放 Manager，所以停掉或重启托管单元不会结束 Bridge 和任务进程，下一个 Manager 接管。安装器 `bin/covey_service_install.py` 的 `--mode`：

| `--mode` | 做什么 |
|---|---|
| `auto` | 自动选下面一种，输出里的 `auto_reason` 写为什么 |
| `systemd` | Linux 用户服务 |
| `launchd` | macOS 用户代理 |
| `cron` | Linux 没有 systemd 时：crontab 的一行 `@reboot` 拉起 Manager |
| `manager` | 不托管：只有 `covey_service.py launch` 起的 Manager，机器重启后不会自己回来 |
| 扩展的模式 | 如公司开发机的 PID-1 钩子模式，见扩展 README |

**`auto` 的选择顺序**：① 扩展的 `service_hosting` 钩子有 `auto_mode()` 且它返回一个存在的模式（公司扩展在开发机上返回它的 PID-1 钩子模式）；② macOS 且有 `launchctl`：`launchd`；③ Linux 且 `systemctl --user show-environment` 答得上（有用户 systemd）：`systemd`；④ Linux 有 `crontab`（且没加 `--no-autostart`）：`cron`，给用户的 crontab 加一行带 `# covey-bridge-autostart` 注释的 `@reboot ... covey_service.py --root <root> run`（日志 `<root>/cron-manager.log`，每个服务根一行，注释里带服务根的短哈希：同一服务根重复运行替换自己的行，不同服务根共存，`--uninstall` 只删自己的；机器的 cron 守护要在跑）；⑤ 其他：`manager`，输出 `autostart: false` 和一句说明，之后重启机器要重跑安装。没有扩展时只走 ②③④⑤。

```bash
python3 -B bin/covey_service_install.py --root "$SERVICE_ROOT" --mode auto --activate
python3 -B bin/covey_service_install.py --mode auto --status      # 现在注册和运行的样子
python3 -B bin/covey_service_install.py --mode auto --uninstall   # 删掉单元或 plist（auto 要先选出 systemd 或 launchd）
```

不带 `--activate` 只写文件，不启动、不加载。重复运行安全：launchd 先 `bootout` 旧的再 `bootstrap`，systemd 重写单元后 `daemon-reload`。回执写在 `<root>/boot-installation.json`，`covey_doctor` 的 `boot` 一节按它读：systemd 看单元是否 `enabled` 且 `active`，launchd 看 `launchctl print` 能否查到，`cron` 看 crontab 里这个服务根的那一行还在不在，`manager` 回执算 `ready` 但带 `autostart: false`。

**systemd 模式**

- 写入 `~/.config/systemd/user/lark-bridge-manager.service` 并 `daemon-reload`，`--activate` 时 `enable --now`。单元的启动命令是运行安装器的那个 Python 加安装器所在 release 的 `bin/covey_service.py`。
- 用户服务默认只在登录期间运行。用户没开 linger 时，安装器的输出带 `hint`：`sudo loginctl enable-linger <用户>`（要 root，安装器不替你跑）。
- 没有用户 systemd（`/run/systemd/system` 不存在，或 `systemctl --user` 连不上用户管理器，常见于容器和没登录过的 ssh）：`auto` 退回 `manager`；明确写 `--mode systemd` 时在前一种情况下报错。
- 单元配置 `Restart=always`（10 秒后重启）和 `KillMode=process`：停止或重启单元只结束 Manager，Bridge 和任务进程留着，由下一个 Manager 接管。单元停着期间 Bridge 照常运行，但它退出后没人重启。这些行为由单元配置推出，测试只核对单元文件内容。
- 单元停着时运行 `covey_service.py start`、`restart` 或 `launch`，会在单元外拉起一个 Manager。之后再启动单元，单元里的 Manager 拿不到单实例锁就退出，每 10 秒重试，直到单元外那个退出。所以先 `--activate` 托管，再注册服务。

**launchd 模式**（macOS）

- 写入 `~/Library/LaunchAgents/<label>.plist`（`--label`，默认 `com.covey.manager`）：`RunAtLoad`、`KeepAlive`、`ThrottleInterval 10`、`AbandonProcessGroup`（停掉 Manager 不连带结束子进程），日志在 `<root>/launchd.log`，Manager 自己的日志仍是 `manager.log`。
- plist 里的环境变量只有 Manager 自己要的：安装时终端的 `PATH`、`HOME`、`LANG`，以及设了的 `COVEY_DATA_HOME`、`COVEY_SERVICE_ROOT`、`COVEY_EXTENSIONS`。Bridge 的环境（代理、`CODEX_HOME` 等）仍在服务清单里，由 Manager 带给它。
- `--activate` 用 `launchctl bootstrap gui/<uid>` 加载。它是用户代理：Mac 登录后才运行，登录前不会自己起来。

**Harness 能不能调通**：`core/harness_probe.py` 的 `probe(名字)` 对 Claude 跑 `claude -p`、对 Codex 跑 `codex exec`（短超时，一次最便宜的问法），返回 `ok`、`reason`（`ok|not_installed|not_logged_in|network|timeout|other`）和 `detail`。安装器、`covey_doctor --probe-harnesses` 用它；`covey_doctor` 不带参数时只读缓存（`<数据根>/harness-probe/<名字>.json`，通过 6 小时、失败 10 分钟，换了命令或代理设置作废）。Bridge 里默认不探测，设 `COVEY_HARNESS_PROBE=1`（可写进服务清单）后，可用性检查把结果放进健康检查的 `model_access.preflight.<名字>.probe`，缓存里是“没登录”的 Harness 在启动卡上不可用（沿用现有的“模型后端尚未登录”提示），网络、超时等不挡任务。

`./install --browser-login-dir DIR`（验收用，不在 `--help` 里）：浏览器会话从 `DIR/profile`（或 `DIR` 本身）这份已登录的用户目录副本开始，跳过二维码；副本由安装器再拷一份到临时目录，用完删掉那份，`DIR` 里的由调用方删。

## 开机恢复

开机自启见上一节「服务托管」。扩展提供的托管方式（如公司的托管模式）的说明在扩展自己的 README。

**扩展提供的模式**（`--mode` 的其他取值，如公司的托管模式）：守护 `service/boot.py` 在核心：每轮等待 NAS 挂载，再通过本地 socket 检查 Manager；Manager 不存在就拉起；控制循环延迟超过 120 秒或失去响应时，只向核对过身份的 Manager 发 SIGTERM，20 秒后仍在就 SIGKILL。Manager 在挂载恢复前不启动任何服务。

**整机重启验收**：`<root>/boot-installation.json` 里的 `reboot_verified` 总是 `false`，代码不会把它改成 `true`。本地进程退出、Manager 重建的测试不能代替整机重启：只有真实重启后核对 Manager、服务进程、事件连接、应用身份和会话完整性，才算通过（见[发布记录要求](testing.md#发布记录要求)）。任务进程会随整机重启结束，重启后这些任务显示为中断，并自动检查并继续一次。

## 备份、归档与维护

Bridge 的 maintenance 线程每小时检查一次（`core/maintenance.py`，计划里每类落盘数据一条规则 `RULES`）：

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
| `tmp/`、`artifact-upload/`、`inbound-messages/`、`proxy/shots/`、`main-thread/opened/`、`main-thread/relays/` | 7 天内没有改动、也没有进程用着的整个删除 |
| `runs/` 运行目录 | 任务进程已不在、最新文件早于 7 天的删除；任务记录和执行过程保留 |
| 代答批次 `proxy/batches/` | 只删已结束且 30 天没改动的 |
| 锁文件 `.locks/` | 记录已不存在或 7 天未改动的删除；`cards/.locks/` 里没人持有的都删 |
| `job-diffs/` | 超过 30 天的删除；总量超过 200 MiB 时从最旧的删，但只删云空间上传已确认的，一天内写的不删 |
| 备份 | 最多 7 份、合计不超过 400 MiB，超出时从最旧的删，最新一份总保留；重启打断留下的 `.tmp` 一天后删 |
| 陈旧记录 | 等待工作区、等待回答的任务和待回答的问题超过 7 天没有动静时关闭，原卡改成“超过 7 天未处理，已自动关闭；需要继续请在本话题直接发消息。”；启动卡没送达的消息 1 天没有动静就关闭。送达未确认的问题和在跑的任务不关。每次关闭记一行 `stale ... closed` |

维护不删除工作区目录或用户产物。

**备份包含**话题（历史、Session 指针、收到的附件）、任务记录、排队任务、执行过程、检查点、问题与投递记录、已验证结果和产物投递记录、启动卡和工作区管理卡状态、启动卡预选、`model-usage.json`、群记录、模型设置、后端观测，以及 `workspace-config.json`、`session_index.jsonl`、`bot-identity.json`，还有代答的 `proxy/settings.json`、`proxy/contacts/`、`proxy/style.md`（清单是 `core/maintenance.py` 的 `BACKUP_DIRS`、`BACKUP_FILES`）。

**不包含**凭据（主人的网页端登录 `proxy/web/`、代答发送工具的 `proxy/send-as-user/`）、代答的截图和批次记录、工作区源码与产物、各 Harness 自己的会话存储、`runs/`、扩展后端的会话目录（如 `<扩展>-sessions/`）、`job-diffs/` 和日志。完整的灾难恢复要另外保留这些。备份和归档含对话内容，要保持私有。

```bash
python3 -B bin/covey_maintenance.py --state-dir "$STATE_DIR"            # 预览维护计划（JSON）
python3 -B bin/covey_maintenance.py --state-dir "$STATE_DIR" --dry-run  # 逐项预演，带大小和文案
python3 -B bin/covey_maintenance.py --verify /absolute/bridge-NNN.tar.gz
python3 -B bin/covey_maintenance.py --restore /absolute/bridge-NNN.tar.gz --destination /absolute/new-state
```

- 预览和 `--dry-run` 不拿锁、不写任何东西，可以在服务运行时执行。`--dry-run` 写出备份的文件数和大小、要删的旧备份、每个归档和删除的条目、每条陈旧记录的任务号和卡上的改动。
- `--apply` 只能在服务停止时手工执行；Bridge 在运行时拒绝。`--apply` 和 Bridge 启动时都先跑一次旧记录迁移（`core/migrate.py`），做完记进 `migrations.json`，日志 `record migrations …`；写不成的任务键用 `failed_keys=` 列出，之后每次启动重试。
- `--verify` 按同名 `.manifest.json` 逐文件核对哈希。`--restore` 先校验，目标必须是尚不存在的新目录；它按 `.metadata.json` 记录的源状态目录改写恢复出的 `.json` 里的绝对路径，不启动服务、不重新提交任务。`.tar.gz`、`.manifest.json`、`.metadata.json` 三个文件放在一起保存。切换到恢复出的状态目录前，确认 Harness 会话存储、工作区和凭据仍然可用。

## 内存看门狗

防止搜索命令吃满机器内存：一个递归遍历 NAS 的 `rg --files` 能在几分钟内占到上百 GiB。看门狗是 `covey/service/memguard.py`，只用标准库，告警由 Bridge 的 `app/memguard_alerts.py` 发。

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
- **现场**：进程状态与内存分类、各线程的 state/wchan/当前系统调用、前 256 个 fd、cwd、脱敏后的命令行、祖先进程链、任务标记、容器 cgroup 计数和 PSI。环境变量只读 `COVEY_TASK_DIR` 这一项。发信号前重新核对 PID 和启动时间；PID 1、看门狗自己和它的监督进程从不处理。
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

## 自动补齐飞书应用

Bridge 要用的权限、事件、卡片回调和机器人菜单（`core/app_manifest.py` 的 `required`，按本部署的开关算）由它自己检查。缺了就在主人私聊里发一张「飞书应用待补」卡；主人点「扫码自动补齐」后，Bridge 用浏览器在开发者后台补上并发布新版本，再用官方接口回读核对。

**检查。** 启动 90 秒后一次、之后每天一次（`app/app_setup.py`）：以机器人身份读应用和它线上版本的详情（`feishu/app_config.py`），比出缺的项。

- 读这两个接口要应用有 `application:application:self_manage`；没有时卡上只列这一项，补上后下次检查才看得到其余的。
- 同一组缺项只发一次卡（`<state>/app-setup/state.json`）；点「这些先不补」后，缺项变了才再发。主人还没和机器人私聊过时不发。
- 要租户管理员审核的权限（群聊免@ 的 `im:message.group_msg`，开了 `COVEY_GROUP_ALL_MESSAGES=1` 才算）单列在卡上、默认不申请，只缺它时不发卡；点「连同需审核的一起申请」才一起提交。
- 只增不删：应用里别的权限、事件和菜单项都不动。一级菜单最多 5 项，放不下的菜单项不补，卡上说明。
- 设 `COVEY_APP_CHECK=0` 不检查、不发卡。

**补齐。** 点「扫码自动补齐」后（只认主人，同一时间只跑一次），Bridge 在 `<state>/app-setup/runs/<时间>/`（0700）里用装了 Playwright 的 Python（`COVEY_PLAYWRIGHT_PYTHON`，默认 `<数据根>/proxy-web/venv/bin/python`；没有时用 `scripts/install_console_browser.sh` 装一个）起 `bin/covey_app_console.py`：

1. 登录：有代答的网页端登录（`<state>/proxy/web/profile`）就复制一份来用；没有或失效时把开发者后台的登录二维码回复在卡下面，过期自动换新的。
2. 读后台配置；后台有一个不是 Bridge 建的未发布版本时停下，请主人先处理。
3. 权限与事件：读应用清单，加上缺的权限、事件和回调写回，再读一次核对。
4. 菜单：按原样保存已有的菜单（图标、名字都在），再加上缺的菜单项（飞书内置线性图标）。
5. 发布：建一个比现有最高版本号大一的版本，可用范围照线上版本，申请发布；要管理员审核时卡上写“已提交，等租户管理员审核”。

结束后用官方接口再查一遍，卡上逐项写已生效、仍然缺；失败时写卡在哪一步和后台的报错原文，并回复那一刻的页面截图。运行目录（登录的副本、二维码、截图）在运行结束时整个删掉，不留登录。菜单补上后约 5 分钟出现在输入框上方。

**在终端里做。** `./install` 在授权后跑同样的检查和补齐（装机时放在同一个浏览器会话里，只扫一次码），二维码画在终端里（`--skip-app-setup` 跳过）。手动：

```bash
python3 -B bin/covey_app_setup.py check --profile my-bot          # 退出码 0 不缺，3 有要补的，1 读不到
python3 -B bin/covey_app_setup.py apply --profile my-bot          # 终端扫码；--review 连同需审核的权限
scripts/install_console_browser.sh                                 # 装浏览器环境，最后一行是解释器路径
```

**限制。** 写开发者后台用的是后台网页自己调的接口，不是公开 API，后台改版可能让某一步失败；失败时照[手动配置飞书应用](#配置飞书应用)补。公司租户在新机器上第一次网页登录可能要在桌面端确认设备，非公司设备可能被条件访问拦下，这时在能登录的机器上用 `covey_app_setup.py apply --app-id <应用>` 补（那台机器上要有这个应用的 lark-cli profile）。装不了 Chromium 的系统库时（要 root：`python -m playwright install-deps chromium`），检查照常、卡上给手工做法。

## 旧安装脚本与手动配置

`./install`（[安装](install.md)）取代了下面这套；旧脚本 `scripts/setup_harness_bridge.sh` 和 `scripts/bootstrap_harness_bridge.sh` 保留到 P6（现网部署 `deploy_bridge.sh` 还用 bootstrap），只在需要手工控制每一步时用。

### 运行安装脚本

给机器人用一个单独命名的 lark-cli profile（下例为 `my-bot`）。第一次加 `--no-start`，只做本机安装和登录：

```bash
./scripts/setup_harness_bridge.sh --profile my-bot --workdir /absolute/workspace --no-start
```

安装时用到的参数如下，其余（`--app-id`、`--state-dir`、`--skip-login`、`--non-interactive`、`--lark-domains`）见 `--help`：

| 参数 | 含义 |
| --- | --- |
| `--profile NAME` | 机器人应用的 lark-cli profile，默认 `harness` |
| `--workdir DIR` | 默认工作区，必须已存在，默认当前目录 |
| `--no-start` | 只安装和检查，不注册、不启动机器人 |
| `--skip-app-setup` | 不检查、不补飞书应用的配置 |
| `--harness-config FILE` | 各 Harness 的启动参数（JSON），见[启动机器人](#启动机器人) |
| `--owner OPEN_ID` / `--pairing-file FILE` | 指定 owner，见[启动机器人](#启动机器人) |

脚本依次做下面几件事；它不安装 Harness 或扩展的运行环境，不开通模型账号，也不注册开机自启：

- 检查 Bash、Python 和 `rsync`，报告找到的 Harness；一个都没有就在改动任何东西之前退出；
- 把本仓库 `skills/` 下的每个 Skill 同步到 `${CODEX_HOME:-~/.codex}/skills` 和 `${CLAUDE_HOME:-~/.claude}/skills`，同名目录整个替换；
- 缺少 `lark-cli` 时用 `npm install -g @larksuite/cli` 安装；
- profile 不存在时用 `lark-cli config init --new` 创建应用配置，再运行 `lark-cli auth login`，最后用 `auth status --verify` 确认；
- 检查飞书应用缺不缺 Bridge 要用的权限、事件、回调和菜单；缺了就在终端画一个二维码，用飞书手机端扫码登录开发者后台，浏览器补上并发布新版本，再回读核对（第一次先在数据根装浏览器环境，约 200 MB）；
- 没加 `--no-start` 时读取 profile 的 appId，注册并启动机器人（见[启动机器人](#启动机器人)）。

按终端给出的浏览器链接完成应用创建和用户授权。已有 profile 会复用；不要拿正在被别的服务使用的应用来装。需要手工创建时：

```bash
lark-cli config init --new --name my-bot --brand feishu --lang zh
lark-cli --profile my-bot auth login --domain im,docs,drive,contact
```

已有应用要录入 App Secret 时，用 lark-cli 从标准输入读入的方式（见 `lark-cli config init --help`）。授权提示要求应用先发布时，先完成下一节再重新运行。

### 配置飞书应用

一般不用自己做：安装脚本在 lark-cli 登录后检查应用，缺的东西扫码后由浏览器补上并发布（`bin/covey_app_setup.py`）；升级后 Bridge 自己再查，缺了在私聊发「飞书应用待补」卡，点「扫码自动补齐」即可，有代答的网页端登录时连码都不用扫。做法、限制和终端命令见下一节[自动补齐飞书应用](#自动补齐飞书应用)。

自动补齐做不了时（机器上装不了 Chromium、公司租户不让这台机器登录网页等），在[飞书开发者后台](https://open.feishu.cn/app)打开刚才配置的应用手工做：

1. 启用机器人能力，可用范围包含自己。
2. 在同一个应用下配置两项订阅：

   | 订阅 | 类型 | 用途 | 需要的权限 |
   | --- | --- | --- | --- |
   | `im.message.receive_v1` | 事件订阅 | 接收任务和追问 | `im:message.p2p_msg:readonly`；在群里用还要 `im:message.group_at_msg:readonly`（只收 @ 机器人的消息） |
   | `card.action.trigger` | 回调订阅 | 所有卡片按钮和选择 | `im:message:readonly` |
   | `application.bot.menu_v6` | 事件订阅 | 机器人菜单（下一步） | 无额外权限 |

   三项都选长连接接收。`application.bot.menu_v6` 可以不开：不开时机器人照常工作，只是菜单点了没有反应。

   权限以 `lark-cli event schema <订阅名>` 的 `Required Scopes` 为准。群里激活时机器人读取群信息（是否外部群），用 `im:chat:read`。`im:message.group_msg`（接收群里全部消息）只在要用群聊免@ 时申请，同时申请 `im:chat.members:read`，发布后服务设 `COVEY_GROUP_ALL_MESSAGES=1`；不用免@ 就不要申请，见[在群里用](usage.md#在群里用)。
3. 在“机器人”页启用自定义菜单，按下表配置，每项的动作都选“推送事件”，事件 key 一字不差（机器人按它认菜单项）；图标在“添加图标 → 上传图标”里传表中的文件（飞书 7.22 以上显示，卡片标题栏用的是同一套飞书线性图标）：

   | 菜单 | 事件 key | 图标 |
   | --- | --- | --- |
   | 用量 | `weekly_usage` | `docs/media/menu-icons/usage.png`（自动补齐用飞书内置的 `insert-chart_outlined`） |
   | 看板 | `digest_board` | 内置 `bitablekanban_outlined`；只在设了 `COVEY_DIGEST_TIMES` 时配 |
   | 设置 | `bridge_settings` | `docs/media/menu-icons/settings.png`（自动补齐用内置的 `setting_outlined`） |

   “设置”是一张卡上几个标签：主线程（设了 `COVEY_MAIN_THREAD=0` 时没有；第一行是私聊模式，新部署默认 Personal Agent）、工作区、脱敏、代答、群聊，见[机器人菜单](usage.md#机器人菜单)。以前单独配的“工作区”（`manage_workspaces`）、“脱敏”（`redaction_dashboard`）照旧能点。不想合在一起的，照旧配两项单独的卡，事件 key 没变：

   | 菜单 | 事件 key | 图标 |
   | --- | --- | --- |
   | 代答 | `proxy_settings` | `docs/media/menu-icons/proxy.png` |
   | 群聊 | `manage_groups` | `docs/media/menu-icons/groups.png` |

   展示形式选“悬浮菜单”（浮在输入框上方，不挡输入），都是一级菜单（飞书最多 5 个一级菜单），不用带子菜单的“更多”：手机上一行放不下时左右滑动。菜单只对主人有反应，结果发在主人和机器人的私聊里，见[机器人菜单](usage.md#机器人菜单)。
4. 申请授权提示列出的应用权限。发消息、更新卡片、上传文件的接口权限按授权提示和接口报错补齐；任务产物上传到云空间还要 `drive:file` 和 `space:folder:create`。用户登录授权不能代替应用权限和发布。
5. 发布一个版本（订阅和菜单都要发布后才生效）。

事件连接由 `lark-cli event consume` 建立，不需要公网回调地址。能收到文字消息不代表卡片权限已经齐全，要以首次验收为准。

### 启动机器人

应用已发布、`lark-cli --profile my-bot auth status --verify` 通过后再启动。

**1. 设置数据根。** 指向持久存储，并在之后注册服务的同一个终端里保持设置（它也会写进服务清单）：

```bash
export COVEY_DATA_HOME=/absolute/harness-bridge-data
```

不设置时默认是仓库同级的 `<仓库目录名>-data`，会随代码所在目录变化，所以建议总是显式设置。目录结构见[数据目录](operations.md#数据目录)。

**2. 确定 owner。** 依次取：`--owner` 指定的 open_id；状态目录 `bot-identity.json` 里已绑定的 owner（与 `--owner` 冲突时拒绝启动）；profile 已验证登录的用户。profile 只有机器人身份、又不知道自己的 open_id 时，生成一次性配对口令：

```bash
APP_ID=cli_xxx
STATE_DIR="$(python3 -B bin/covey_paths.py "harness-$APP_ID" --new)"
python3 -B bin/covey_pairing.py --state-dir "$STATE_DIR" --app-id "$APP_ID"
```

把输出里的 `pairing_file` 用 `--pairing-file` 传给安装脚本，机器人起来后在与它的单聊里发送 `message` 的内容（形如 `/pair ...`）。机器人回复“已绑定你的身份。发送任务后，在该消息下选择模型、推理强度和工作区。”即绑定成功，口令文件随之删除。口令 24 小时内有效；绑定前机器人不处理任何消息和卡片；已有 owner 时不能生成口令，也不能用口令替换 owner。不要把口令发到群聊。

**3. 启动。** 去掉 `--no-start` 重新运行：

```bash
./scripts/setup_harness_bridge.sh --profile my-bot --workdir /absolute/workspace \
  --harness-config "$COVEY_DATA_HOME/harnesses.json"
```

`--harness-config` 可选，各 Harness 可用的参数见[配置 Harness 参数](harnesses.md#配置-harness-参数)，Codex 的执行权限见[执行权限](harnesses.md#执行权限)。服务只记录这个文件的路径、每次启动时读取，所以文件放在持久位置；里面不放凭据。例如 扩展 Harness 环境不在默认位置时：

```json
{"harnesses": {"claude": {"args": ["--claude-cmd", "/absolute/bin/claude"]}}}
```

这一步调用 `scripts/bootstrap_harness_bridge.sh`：把机器人注册为托管服务（清单在 `$COVEY_DATA_HOME/bridge-services/`，状态目录默认 `$COVEY_DATA_HOME/harness-<APP_ID>/`），需要时拉起 Manager，打印服务状态。服务清单记录的是这次运行的脚本所在目录，之后在这个检出里 `git pull` 会改变下次启动的代码；长期使用建议换成固定的 release 目录，见[部署新版本](operations.md#部署新版本)。

**4. 确认就绪。**

```bash
STATE_DIR="$COVEY_DATA_HOME/harness-$APP_ID"
python3 -B bin/covey_service.py status --state-dir "$STATE_DIR"
python3 -B bin/covey_doctor.py --root "$COVEY_DATA_HOME/bridge-services" --runtime-only
grep 'ready event_key=' "$STATE_DIR/bridge.log" | tail -n 2
```

三项都满足才算就绪：

- `covey_service.py status` 输出 `"alive": true`，`status.health.status` 为 `ready`；
- `covey_doctor.py --runtime-only` 退出码为 0，这个服务的 `status` 为 `ready`，`events.owned_required_consumers` 为 `true`；
- 最近一次启动后，`bridge.log` 里有 `ready event_key=im.message.receive_v1` 和 `ready event_key=card.action.trigger` 两行；开了菜单订阅还有 `ready event_key=application.bot.menu_v6`。

报告里的 `model_access: not_probed` 表示还没验证过模型请求，由首次验收确认。开发机重启后机器人不会自动恢复，开机自启要单独注册，见[开机恢复](operations.md#开机恢复)。

### 首次验收

在飞书里依次完成下面五步，全部通过才算安装完成。本地测试不能代替飞书客户端里的点击和回调。卡片和按钮的完整用法见[使用指南](usage.md)。

1. 在与机器人的单聊发送“只告诉我当前工作目录，不修改文件”。消息下出现启动卡；选模型、思考预算和工作区，运行方式保持默认的“原目录运行”，点“开始任务”。核对答案里的目录与所选工作区一致。
2. 在同一话题追问“我刚才问了什么”，确认延续了上下文；再在主聊天另发一条消息，确认它开了一个独立的新话题。
3. 发送“等待 30 秒后报告当前目录，不修改文件”，执行中点任务卡上的“停止”。核对这个任务停下，其他话题不受影响。
4. 对支持原生问答的 Harness（见[支持的 Harness](harnesses.md#支持的-harness)），发送“先问我希望使用中文还是英文，收到回答再继续”。提交回答后，卡片显示明确的状态，任务继续执行。
5. 对每个要用的 Harness 各做一次第 1 步，确认启动卡里它们的模型都能真实调用。

### 安装常见问题

| 现象 | 优先核对 |
| --- | --- |
| `no usable Harness found` | CLI 是否在当前 PATH 里；扩展的运行环境是否通过它自己的检查（见扩展 README） |
| `lark-cli missing and npm is not available`，或装完仍找不到 `lark-cli` | Node/npm 是否可用、npm 全局安装目录是否在 PATH 里；不要因为命令找不到就反复创建应用 |
| `lark-cli auth missing for profile ...` | 去掉 `--skip-login` / `--non-interactive` 重新运行，完成浏览器授权 |
| `profile ... belongs to ..., not --app-id ...` | `--profile` 和 `--app-id` 不是同一个应用 |
| `only non-secret proxy URLs without credentials can be persisted` | 代理 URL 带了用户名或密码，不能写进服务清单；换不带凭据的代理再注册 |
| `--check-runtime` 输出 `"ok": false` | 按 `error` 对照公司扩展自己的 README 里的检查项 |
| 发消息后没有启动卡 | 两行 `ready event_key=...` 是否都在；订阅、权限、发布是否都在同一个应用上；发消息的人是不是 owner |
| 同事发消息没有回复 | 机器人只服务 owner，给同事部署独立的机器人 |
| 启动卡里缺少某个 Harness 的模型 | 这个 Harness 是否在服务的 PATH 里、是否已用同一系统账号登录；看状态目录 `bridge.log` 里的 `launch catalog unavailable` 日志 |

服务起不来、点按钮没反应、模型调用报权限错误等运行期问题见[排障](troubleshooting.md)。反馈问题时提供 Harness 名称、任务编号、发生时间和脱敏后的错误，不要贴 App Secret 或访问令牌。

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
| `worker <名称> failed` / `maintenance deferred` | 工作线程异常并重建 / 维护出错推迟 |
| `maintenance backup completed` / `idle maintenance … completed` | 每日备份 / 空闲时的清理 |
| `env <名字> is deprecated` | 设了弃用的环境变量，见[服务清单里的环境变量](configuration.md#服务清单里的环境变量) |
| `app setup check` / `app setup run` | 飞书应用检查和自动补齐，见[自动补齐飞书应用](#自动补齐飞书应用) |
| `outcome=retired_harness` | 已下架 Harness 的话题收到消息或点击，见[已下架项与旧配置](configuration.md#已下架项与旧配置) |
| `proxy …` | 代答，见[代答没有回复或没有发出](troubleshooting.md#代答没有回复或没有发出) |

汇总计时：`python3 -B scripts/bridge_timing.py --state "$STATE_DIR" --since 19:30`，见[慢在哪里](troubleshooting.md#慢在哪里)。

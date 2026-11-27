# 升级与部署

本文写给要换服务运行的代码或配置的人：改了开关要重新注册、升级到新版本、出事回滚，以及自己维护固定 release 时部署脚本一步步做了什么。服务由哪些进程组成、数据放在哪里、日常的启停见[运维指南](operations.md)；开关和环境变量见[配置](configuration.md)。

命令都在服务正在运行的代码目录（`./install` 的检出，或 `releases/` 下的 release 目录）里执行，并假设已按[运维 · 找到数据根和 APP_ID](operations.md#找到数据根和-app_id)设好 `COVEY_DATA_HOME`、`APP_ID`、`STATE_DIR` 和 `SERVICE_ROOT`。

| 要做的事 | 先做 | 再看 |
| --- | --- | --- |
| 改开关 | | `covey_service.py set`，见[配置 · 改开关三步](configuration.md#改开关三步) |
| 改 Codex 参数 | [载入服务清单的环境](#载入服务清单的环境) | [改设置后重新注册](#改设置后重新注册) |
| 升级到新版本（`./install` 装的、只有核心） | | `scripts/upgrade.sh`，见[日常升级](#日常升级) |
| 升级到新版本（带扩展、要指定参数） | [载入服务清单的环境](#载入服务清单的环境) | [升级与回滚](#升级与回滚) |
| 出事回滚 | 同上 | [升级与回滚](#升级与回滚)的“回滚” |
| 想知道部署脚本做了什么、要手工部署 | | [部署新版本](#部署新版本) |
| 升级 Manager、Skills、内存看门狗 | | [部署新版本](#部署新版本)第 6–8 步 |

公司同事用的扩展库有自己的升级说明（见扩展库 README 的「同事第一次安装」），照它的做；它底下用的仍是本篇的 `deploy_bridge.sh`。

## 载入服务清单的环境

Bridge 用的环境变量都在服务清单里（[配置 · 服务清单里的环境变量](configuration.md#服务清单里的环境变量)），重新注册时清单里的变量整组换成执行注册的那个终端里的。所以改开关、重新注册、用 `deploy_bridge.sh` 升级之前，先在一个新终端里把清单现在的变量载入：

```bash
MANIFEST="$(python3 -I -c 'import glob, json, os, sys
for f in glob.glob(os.path.join(sys.argv[1], "services", "*.json")):
    if os.path.realpath(json.load(open(f))["state_dir"]) == os.path.realpath(sys.argv[2]):
        print(f)' "$SERVICE_ROOT" "$STATE_DIR")"
eval "$(python3 -I -c 'import json, shlex, sys
m = json.load(open(sys.argv[1]))
print("清单里的启动命令：" + " ".join(m["command"]), file=sys.stderr)
for k, v in sorted(m["environment"].items()):
    print(f"export {k}={shlex.quote(v)}")' "$MANIFEST")"
```

- 清单里的名字是旧写法（`LARK_BRIDGE_<X>`），和 `COVEY_<X>` 同一个变量，之后 `export COVEY_<X>=…` 会盖过它（见[改名与兼容 · 环境变量的旧名](compat.md#环境变量的旧名)）。
- 启动命令打印在终端里：`bin/covey_bridge.py` 所在的目录就是服务正在运行的代码目录，后面的 `--workdir`、`--profile`、`--app-id`、`--state-dir` 等是重新注册时要照抄的参数。
- 新终端里多出来、清单里没有的本项目变量（比如 shell 初始化文件里设的 `COVEY_*`），重新注册时也会被存进去；不需要的先 `unset`。

## 改设置后重新注册

只改开关时用 `python3 -B bin/covey_service.py set --state-dir "$STATE_DIR" KEY=VALUE`（[改开关三步](configuration.md#改开关三步)）：它从清单读出现在的变量、只改这一个，再照下面的做法重新注册，不用先载入环境。改 Codex 的命令行参数，或要手工做时，在服务正在运行的代码目录里重新注册：

```bash
# 已按上一节载入清单的环境，再改要改的
export COVEY_DIGEST_TIMES=08:00,22:00
# 服务仍从 ./install 的检出运行（没用 deploy_bridge.sh 升级过）：
./install
# 服务从 releases/ 下的 release 运行：在那个 release 目录里，参数照清单命令抄
./scripts/bootstrap_harness_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" \
  --state-dir "$STATE_DIR" --harness-config "$COVEY_DATA_HOME/harnesses.json"
```

- `./install` 重跑时发现设置（或代码目录、代理、Harness）和上次注册的不一样才重新注册，一样就只说“服务已在运行，配置没变”。服务已经换到 release 运行之后不要再用 `./install` 改设置：它会把服务注册回检出目录。
- 在别的代码目录里运行 bootstrap 等于换代码，那是升级，见[升级与回滚](#升级与回滚)。
- 确认：`covey_service.py status` 的 `generation` 加一，`bridge.log` 有新的 `bridge started`。`restart` 不换清单，改开关不能只 `restart`。

bootstrap 的参数和注册链路见[运维 · 注册与启动服务](operations.md#注册与启动服务)。

## 日常升级

数据根里有 `upgrade.json` 时，网页「版本与更新」会提示新版本，点一下就照它把 Bridge 和网页一起升级，见 [web.md 版本与更新](web.md#版本与更新)；设了 `COVEY_AUTO_UPGRADE` 时每天到点自己安全地升一次，见[定时自动升级](#定时自动升级)。`upgrade.json` 由装好、升级好这台机器人的安装器写：扩展的安装器（比如 covey-internal 的 `./install`）写它自己的；只有核心时，在 git 检出里运行的 `./install` 和 `scripts/upgrade.sh` 写一份用 `scripts/upgrade.sh` 的（不覆盖扩展写的）。下面是在终端里升级。

`./install` 装的机器人（只有核心、没有扩展）升级只要一条命令，在仓库里运行：

```bash
scripts/upgrade.sh --dry-run    # 先看：取新提交、做部署前的检查，什么都不换
scripts/upgrade.sh              # 部署：机器人重启一次，成功后检出快进到新提交
```

它替你做[升级与回滚](#升级与回滚)里那几步，真正的部署还是 `scripts/deploy_bridge.sh`：

1. 检出要干净：有没提交的改动时停下（部署的是提交，不含这些改动；先提交或 `git stash`）。`git fetch` 上游（`--no-fetch` 不取），要升到的提交是当前分支的上游（没有上游时 `origin/main`，`--ref` 可改），它必须包含检出的 `HEAD`：检出里有它没有的提交时停下，从不强制拉取、不重置。
2. 找到机器人：服务根里只有一个时就是它，几个时用 `--state-dir` 指定。现网已经是这个提交（从检出直接运行的，看检出的 `HEAD`）、数据根 `extensions/` 下的[个人扩展](#个人扩展)也和现网带的一样时只说“已经是最新”；只有个人扩展有新提交时照样升级，核心不变。现网 release 带扩展（`--ext-repo` 装的）时停下，照扩展自己的升级说明做。
3. 核对依赖：服务清单里的 `python3` 是 3.10 以上，服务的 `PATH` 里有 `lark-cli`（没有只提示）。
4. 照服务清单载入环境：清单里的变量原样，终端里别的本项目变量（`COVEY_*`、代理）不带进去，所以开关和代理都不变；`BASH_ENV`、`ENV` 也不带（bash 会先执行它们指向的文件，有的 ssh 会话里它指向 `.bashrc`，改了的 `PATH` 让预检报 `env_value_diff`）；`--workdir`、`--profile`、`--app-id`、`--state-dir`、`--harness-config` 和其余参数从清单的启动命令抄。先跑 `deploy_bridge.sh --check-only`，没过的项用中文写出来（有任务重启会被打断、另一个部署在跑、权限变量……），什么都不换；`--dry-run` 到此为止。
5. 部署：机器人重启，几秒后就收得到消息和点击，在跑的任务不中断、由新进程接管。日志在 `$COVEY_DATA_HOME/deploy-runs/<时间>/`（检查是 `<时间>-check/`）。成功后检出快进到这个提交；第一次升级时还有任务进程用着检出里的代码的，先不快进，下次运行时补上。最后列“升级后要你补的”：这一版起默认打开、服务清单没设的开关（`pre.switches_on`），和诊断列出的要手工补的事（`post.owner_todo`，即 `covey_doctor.py` 每个服务的 `todo`：事件通道还是 lark-cli、飞书应用缺的权限、网页发布要的 lark-cli 能力）。
6. 网页：用 `web/deploy/selfhost/build.sh` 自托管的网页（`./install --web` 装的）跟着换成新 release 的 `web/`（`web/deploy/selfhost/build.sh <目录> --restart`，Manager 几秒内拉起新版，日志在 `deploy-runs/<时间>-web/`）；Bridge 已经是最新、网页还是旧的时只补网页。别的办法部署的网页只提示，不动；`--no-web` 不动网页。最后写 `upgrade.json`（见上）。

`scripts/upgrade.sh --status` 只查不换：取新提交，说现网、升级会部署的提交、新提交、网页要不要更新、现在为什么不能升（有没提交的改动、带扩展……）；加 `--json` 时最后一行是给 Bridge 的 JSON（网页「版本与更新」和定时自动升级读它）。

退出码：0 升级好了（或已经是最新、`--dry-run` 的检查都过了、`--status` 查完），1 某项检查没过（部署前停下的什么都没换），2 参数不对或找不到机器人，3 另一个部署正在跑（等它结束再运行），4 Bridge 升级好了、网页没更新上（不用回滚 Bridge，单独重跑日志里那条网页命令，或再运行一次）。再跑一次是安全的：已经是最新时什么都不做。

升级以后服务从 `releases/` 下的 release 运行：改开关用 `covey_service.py set`（[改开关三步](configuration.md#改开关三步)），不要再用 `./install`，它会把服务注册回检出目录。它不升级 Manager 和内存看门狗，需要时见下面[部署新版本](#部署新版本)第 6、8 步；回滚、带扩展和要改启动参数的，照下一节手工用 `deploy_bridge.sh`。

### 定时自动升级

不想每次手动升级时，在服务清单里设 `COVEY_AUTO_UPGRADE=05:00`（每天几点，按 Asia/Shanghai；[改开关三步](configuration.md#改开关三步)），要数据根里有 `upgrade.json`（见上；没有时 Bridge 启动记一行 `auto upgrade: ... no upgrade.json: off`，什么都不做）。到点 Bridge 照 `upgrade.json` 做和网页上点「全部更新」一样的事，只是规矩更保守：

- 先查（`upgrade.json` 的 `status`）：没有新版本就什么都不做，也不说；`status` 说现在不能升（检出有没提交的改动、和远端分叉、带扩展……）就不升，在私聊里说一次要人处理什么，第二天再试。
- 有任务在跑，或重启会打断任务（`restart_blockers`）时不升：15 分钟后再看，最多再看 3 次；还是忙就放弃这一天，私聊里说一句，第二天同一时间再试。从不强行重启。
- 升级本身先过部署前的检查（`deploy_bridge.sh --check-only`）才部署，网页跟着一起更新（扩展的安装器、`scripts/upgrade.sh` 都是这样）。检查时另一个部署在跑、或刚好来了任务的，按上一条推迟；别的没过的、部署没成的，**不自动回滚**，私聊里说一句和日志位置，等人看过再升。
- 升级会重启 Bridge，这一天的进度记在状态目录的 `upgrade/auto.json`，新起来的 Bridge 接着读结果（`upgrade/run.json`）；升完在私聊里发一条：升到哪个版本、有哪些新提交，或者网页没更新上、为什么。夜里不打扰的时段（`COVEY_QUIET_HOURS`）里攒到时段结束再发。日志在 `deploy-runs/<时间>-auto/`。
- 错过时间（那会儿 Bridge 没在运行）的，过了时间 2 小时就不再补，等第二天。

### 这个版本起默认开的功能

以前默认关的一批开关，这个版本起不设就是开的：专项（负责人、专项卡、整理专项只提议、专项的记忆和问题分流、停住后问你）、记忆和知识条目、独立复核、到点回来看、早晚汇总（09:30 和 21:30）、置顶的「手上的事」和话题自动 Pin、派发卡、同时做几件事、管家规则、新话题带上最近话题的要点，菜单「看板」则每个部署都有。全部名字和各自怎么关见[配置 · 开关速查](configuration.md#开关速查)。

帮同事升级时要知道：

- 服务清单里已经设了的开关照旧生效，设成 `0` 的仍是关。只有从没设过它们的老服务，升级后会多出这些功能；`./install` 重跑也不再往清单里写它们，只写飞书 SDK 的两个开关。
- 不想要的功能，升级前后都按[改开关三步](configuration.md#改开关三步)设成 `0`，早晚汇总设 `off`，整理专项设 `off`。夜里不打扰默认跟着早晚汇总（21:30 到 09:30），所以想保持没有夜里不打扰，设 `COVEY_QUIET_HOURS=off`。
- 话题自动 Pin 要应用订阅“消息已读”事件，「手上的事」要置顶权限。升级后私聊里的「飞书应用待补」卡会提示，点一下补齐；不补也不影响别的功能，只是 Pin 和置顶不生效。
- 后台的一问一答（同时做几件事的分流、整理专项等）现在经[辅助模型](configuration.md#辅助模型)，默认跟随管家；以前设过 `COVEY_JUDGE_GATEWAY` 的，这个变量不再读，清单里留着不报错。`COVEY_TURN_EXPORT`、`COVEY_HARNESS_PROBE`、`COVEY_BOARD`、`COVEY_PROJECT_STYLE_KEEP` 也一样，见[已下线的功能与旧配置](legacy.md#已下线的功能)。
- 任务卡上的“下载请求与结果”整块下线，已发出的卡上的旧按钮点了只回一句说明。

### 稳定通道

升级跟的是检出当前分支的上游：核心的 `scripts/upgrade.sh` 跟检出的分支（默认 `main`），扩展的安装器跟扩展检出的分支。部署频繁的仓库可以另维护一个稳定分支（比如 `stable`），只在新版本已在自己的机器人上跑稳之后才快进过去；使用者把检出切到这个分支（`git checkout stable`，上游是 `origin/stable`），网页上的升级和定时自动升级就只会升到稳定分支上的版本。扩展的安装器怎么切、谁来推进稳定分支，见扩展自己的说明。

## 升级与回滚

升级底下的做法只有一种（`scripts/upgrade.sh` 也是这样做）：用 `scripts/deploy_bridge.sh` 把要部署的提交导出成 `$COVEY_DATA_HOME/releases/<id>/` 下的固定 release，再让服务从它运行。不要在服务运行的检出里 `git pull` 了事：`./install` 装的服务直接从检出运行，拉代码之后新开的任务进程立刻用新代码，Bridge 却还是旧的，直到下次重启。固定 release 不会被之后的 `git pull` 改动，旧 release 留着就能回滚。

**升级**（第一次也一样，服务从 `./install` 的检出换到 release）：

```bash
cd /absolute/covey                       # 核心的 git 检出，./install 的那个就行
git fetch origin                          # 只取不合并：服务还从这个检出运行时，git pull 会让新任务马上用上新代码
# 按“载入服务清单的环境”一节载入清单的变量（预检要求终端和清单的变量一致），再设：
APP_ID=cli_xxx
RUN="$COVEY_DATA_HOME/deploy-runs/$(date +%Y%m%d-%H%M%S)"   # 部署的日志目录
scripts/deploy_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" --run-dir "$RUN" --commit origin/main --check-only
scripts/deploy_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" --run-dir "$RUN" --commit origin/main
```

- `--commit` 是要部署的提交（默认是检出的 `HEAD`）；服务换到 release 运行以后，检出就可以随意 `git pull` 了。`--workdir`、`--profile` 照清单命令里的抄（`./install` 装的 profile 默认是 `covey`）。`COVEY_DATA_HOME` 载入清单后就有（清单里写作 `LARK_BRIDGE_DATA_HOME`，两个名字都认）；`--run-dir` 和 `TMPDIR` 至少给一个，否则退出码 2。
- 带扩展的部署：每个扩展检出加一对 `--ext-repo /absolute/<扩展检出> --ext-commit <提交>`（不写提交就是它的 `HEAD`），release 里会带上这些扩展，见[核心和扩展分开检出](#核心和扩展分开检出)。
- 输出是一行 JSON：退出码 0 成功，1 某项检查没过（`error` 写明哪项；预检没过时什么都没换，部署后的检查没过看 `post`），2 参数不对，3 另一个部署正在跑（等它结束再跑一次）。先跑 `--check-only`，预检全过再去掉它。
- 预检里最常见的是 `env_missing`、`env_extra`、`env_value_diff`：终端和清单的变量不一致，按上面重新载入。要改开关的，先按[改设置后重新注册](#改设置后重新注册)改完，再升级。只有 `PATH` 不一样、终端设了 `BASH_ENV`（或 `ENV`）时，是 bash 在脚本第一行之前执行了它指向的文件（有的 ssh 会话里是 `.bashrc`），结果里 `pre.shell_startup_env` 会写出来：`env -u BASH_ENV -u ENV` 重跑。
- 部署后它自己核对新 Bridge 起来、事件就绪、`covey_doctor --runtime-only` 通过、原来在跑的任务都被接管；`post.app_config` 报飞书应用还缺什么（不算失败，补法见[飞书应用配置](feishu-app.md)）。
- 已知限制：脚本开头会 `unset HTTPS_PROXY`，所以清单里有大写 `HTTPS_PROXY` 的服务预检总报 `env_missing`。现在的 `./install` 把代理存成小写的 `https_proxy`；更早装的服务（在 `./install` 里输入过代理地址，或终端里设的是大写）清单里还是大写。先在载入清单的终端里 `export https_proxy="$HTTPS_PROXY"; unset HTTPS_PROXY`，按[改设置后重新注册](#改设置后重新注册)注册一次，代理就改存成小写的 `https_proxy`，之后照常部署。

**回滚**：换回上一对提交，加 `--allow-rollback`（不加时预检拦下比现网旧的提交）：

```bash
ls "$COVEY_DATA_HOME/releases/"          # 上一个 release；带扩展的看它的 RELEASE_MANIFEST 里各检出的提交
scripts/deploy_bridge.sh --workdir /absolute/workspace --profile covey --app-id "$APP_ID" --run-dir "$RUN" \
  --commit <上一个核心提交> [--ext-repo /absolute/<扩展检出> --ext-commit <上一个扩展提交>] --allow-rollback
```

release 目录已存在时不重新打包，只重新注册，所以回滚很快。旧 release 要留到用它的任务进程都结束（见下面第 5 步）。开了多设备时回滚的注意事项见[运维 · 多设备：设备上的任务没停掉](operations.md#多设备设备上的任务没停掉)第 5 条。开过多参与者协作的，回滚前照[兼容 · 新旧版本混跑](compat.md#新旧版本混跑)里轮次来源那一条做。

## 部署新版本

从固定的 release 目录运行，更新代码时不会改变正在运行的服务和任务进程用的文件。

`scripts/deploy_bridge.sh --workdir <目录> --profile <profile> [-- <bridge 参数>]` 按下面第 1–4 步做完并打印一行 JSON，检查不过就停；`--check-only` 只做部署前的检查，参数见脚本开头。其中权限变量（`pre.permission_env`）查目标服务的清单、当前终端和服务根下其他运行中的服务的清单，和部署后 `covey_doctor --runtime-only` 的口径一致（停用的服务只告警）。部署后的 `covey_doctor --runtime-only` 不是 0 时每 10 秒再问一次，最多 90 秒（刚启动时事件连接还在建立，会短暂报 degraded），`post.doctor_tries` 是问了几次。它还在结果里报 `post.app_config`（飞书应用缺不缺东西），不算失败。

### 一次只有一个部署，不往回部署

几个人或几个 Agent 都可能部署同一个服务，同时跑会互相覆盖，后跑完的生效，较旧的提交还可能盖掉较新的现网。所以：

- **排队**：真部署在部署前检查之前拿服务根下的 `deploy.lock`（`flock`，进程退出就释放，崩溃也一样；NFS 上同样互斥），一直持到结束；锁文件里写着谁拿着（`pid`、`host`、`commit`、`release_id`、`started`、`run_dir`、`task`）。别的部署正拿着时立刻退出，退出码 3，结果里的 `deploy_running` 是那位占用者，什么都不碰。等它部署完再跑一次就是排队。`--check-only` 不拿锁、不受它拦，只在 `pre.deploy_running` 里报有没有部署在跑（没有是 `null`）。
- **不回退**：现网服务清单里的入口在 `$COVEY_DATA_HOME/releases/<id>/` 下时，现网的提交（release 里的 `RELEASE_MANIFEST`，没有就按 id）必须是 `--commit` 的祖先；扩展个数和现网一样时，每个 `--ext-commit` 也按顺序这样比。不是就拦下（`pre.rollback` 写明哪个提交没包含现网的哪个，或检出里没有现网的提交、要先 fetch），`--check-only` 也报。要部署的提交是从旧 `main` 拿的，先同步最新 `main`；真要回滚时加 `--allow-rollback`。现网不是从 `releases/` 下运行的（如 `./install` 直接跑检出）不比，`pre.live_release` 是 `null`。

### 升级不丢配置

新版本下线一个功能时，它的配置往往只是“读到了、不再用”：以前只在启动日志里记一行 `no longer used`，要等启动卡上少了供应商、话题报“已下线”才发现。现在部署前的预检会列出来（`core/dropped_config.py`）：

- 列什么：Harness 参数里已下线的选项且带了值（默认 Harness 的在服务启动命令里，其余在 `harnesses.json` 的 `harnesses.<名字>.args`，如 Claude 网关档位的 `--claude-profiles`、Magpie 的 `--codex-magpie-url`）；`harnesses.json` 里还写着、或 `enabled_harnesses` 里启用的已下线 Harness；只给已下线 Harness 用的模型网关；服务环境里不再读的变量（`core/ignored_env.py` 的 `IGNORED_ENV`）。每项写明丢的是什么、改成什么。
- 什么时候拦：只拦这次升级才丢的（新版本列出、现网 release 自己的清单里没有的），`pre.dropped_config` 逐项写出；现网早就不读的只计数（`pre.dropped_config_known`）。看过、确认不要了，加 `--allow-dropped-config` 再部署（`scripts/upgrade.sh` 也收这个参数）。现网 release 还没有这份清单（比它旧，或不是从 `releases/` 运行）时没有可比的，不拦。
- 升级以后：`covey_doctor` 的报告每个服务多一个 `dropped_config`，终端摘要里一行“有 N 项配置新版本不再用”，只提示，不算故障。
- 以后下线功能的人：值不再读的选项加进 `RETIRED_OPTIONS`；下线 Harness、网关能接的 Harness、环境变量照原来的表（`core/roster.py` 的 `RETIRED`、`core/gateways.py` 的 `RETIRED_GATEWAY_HARNESSES`、`IGNORED_ENV`）改，清单自己会读到。

### 核心和扩展分开检出

核心在 `covey/` 检出，扩展在各自的库里（扩展库根下每个含 manifest 文件的目录是一个扩展包）。加 `--ext-repo <扩展检出>` 和 `--ext-commit <提交>`（默认 `HEAD`），都可重复，按先后配对（有几个扩展库就加几对）。`--repo`/`--commit` 仍是核心。release 的 `extensions/enabled` 列每个扩展检出根目录 `enabled` 文件里的包（没有这个文件就列它所有的包）；检出里没列的包随 release 带着但默认不启用，用 `covey_plugins.py enable` 打开（见[运维 · 扩展的装和卸](operations.md#扩展的装和卸)）。

```bash
scripts/deploy_bridge.sh --workdir <目录> --profile <profile> --repo /absolute/covey --ext-repo /absolute/extension --check-only
```

- 打包由 `scripts/assemble_release.py` 做：核心 `git archive` 到 release，每个扩展包 `git archive` 到 `release/extensions/<包名>/`，去掉包里的 `standalone/`，包的 `bin/*.py` 入口（如扩展自己的运行时脚本）放进 `release/bin/` 盖掉核心里的转发脚本，写 `extensions/enabled`（按 `--ext-repo` 的顺序列出所有包名）和 `RELEASE_MANIFEST`（各检出的完整提交号），再重写 `AGENTS.md` 的模块表。形状和单仓库的 release 相同，服务清单、`runs/<任务键>/` 协议、回滚都不变。拼装按提交取，不看检出的工作区。
- release id 是 `<核心前 8 位>-<扩展前 8 位>[-<第二个扩展前 8 位>…]`（有[个人扩展](#个人扩展)时接着是它们的前 8 位），release 目录、`bridge started … release=<id>` 的匹配都用它；结果 JSON 的 `commit` 仍是核心完整提交号，另有 `ext_commits` 和 `release_id`。不给 `--ext-repo` 时 release 目录名是核心提交号。回滚只回一半：核心提交不变、`--ext-commit` 设成旧值，得到一个新 id 的 release。
- 额外的预检（`--check-only` 同样做）：扩展检出是 git 仓库、提交存在（否则退出码 2）；扩展库的 `core.lock` 写的核心提交和 `--commit` 不一致时拦下（`pre.core_lock_mismatch`；试新核心时加 `--allow-core-mismatch` 放行）；服务清单和当前终端里不能有 `COVEY_EXTENSION_PATH`、`COVEY_EXTENSIONS`（`pre.extension_env`；它们会盖过 release 里的 `extensions/`，unset 后重新注册）；启用的扩展自己的检查要过（`pre.extension_checks`，说明在 `pre.extension_notes`；先把每个启用包的钩子模块和 `features` 模块试导入一遍，再问它的 `deploy_check` 钩子）：脚本把要部署的 release 拼到运行目录的 `tmp/`（release 已存在时直接用它），在里面跑 `bin/covey_plugins.py deploy-check --harness-config <配置>`，跑完删掉，所以扩展看到的是这次要部署的代码和服务实际用的配置（例如隔离解释器的源码是不是 release 固定的版本）；确认无碍时加 `--allow-extension-checks` 放行。回滚到还没有 `deploy-check` 的旧核心时不检查，`pre.extension_notes` 里说明。

### 个人扩展

只给自己用的扩展包（比如把 Claude Code 的命令转到别的机器上跑的），放在数据根的 `extensions/` 下，每个是一个 git 检出，布局和扩展库一样：根下每个含 manifest 文件的目录是一个包，根下可选的 `enabled` 文件列默认启用哪些（没有就全部启用）。

```text
$COVEY_DATA_HOME/extensions/
  enabled            # covey_plugins.py enable|disable 写的本部署选择（不是检出）
  <名字>/            # git 检出
    <包名>/manifest.py
    enabled          # 可选
```

- 每次部署（`deploy_bridge.sh`，以及套着它的 `scripts/upgrade.sh`、扩展的安装器、网页上的升级、定时自动升级）自己扫一层这个目录（`covey/service/personal_extensions.py`），把每个检出 `HEAD` 的包和核心、`--ext-repo` 的扩展一起拼进 release，排在最后；结果的 `personal_extensions` 列出它们，release id 末尾多它们的前 8 位，`RELEASE_MANIFEST` 里记成 `personal <提交> <包>…`。运行时仍只从 release 加载：回滚时个人扩展跟着回，改了检出要再升级一次才生效，没提交的改动不带（`pre.personal_notes` 提示）。
- 预检照常在拼好的 release 里跑扩展自检（`pre.extension_checks`），个人扩展也在里面：每个启用的包的钩子模块和 `features` 模块都试导入一遍，坏了的在部署前就拦下，什么都不换。拦下的是个人扩展（或数据根还启用着、release 里已经没有的包；`--ext-repo` 带的包除外）时，`pre.extension_disable` 给出临时关掉它的命令（`COVEY_DATA_HOME=… python3 -B …/bin/covey_plugins.py disable <包>`，写的是数据根的选择），关掉后照常升级，改好再 `enable`、升级一次。
- 不打包、只提示的：不是 git 检出的目录、`HEAD` 里没有包的检出。拦下的（`pre.personal_problems`）：属主不是当前用户、或同组和其他人可写的检出（它的代码以 Bridge 的权限运行），还没有提交的检出。几个检出里有同名的包时拼装报错，谁也不盖住谁。
- 能分享给同事的包，直接提进扩展库、不写进它的 `enabled`，要用的人 `covey_plugins.py enable <包>`；数据根的个人扩展只留给真正私有的代码。不要用 `COVEY_EXTENSION_PATH` 指过去：它只给开发用，服务清单里有它时预检会拦。

### 预编译字节码

服务的每个 Python 进程（Bridge、任务进程、管家的 MCP 服务）都带 `-B` 并设了 `PYTHONDONTWRITEBYTECODE=1`，自己不写 `.pyc`；release 里没有 `__pycache__` 时每次启动都要把源码从头编译一遍（`import covey.app.runner` 约多 0.4–0.5 秒）。

- `deploy_bridge.sh` 在 bootstrap 之前运行 `scripts/precompile_release.py`，用将要注册的那个 `python3`（`pre.command_diff` 已核对它和服务清单一致；`.pyc` 带解释器版本标签，版本不一致就白编）给 `covey/`、`extensions/` 和 `harness_bridge.py` 写 `.pyc`；release 目录已存在、跳过打包时同样会做。编译失败只在结果的 `precompile`（`ok: false`、`failed`）里报告，不让部署失败；已经编译过的文件会跳过。
- 手工用 `assemble_release.py` 拼装时加 `--precompile [PYTHON]`（默认不编）；手工 `git archive` 导出 release（第 2 步）后运行 `python3 -B -I scripts/precompile_release.py "$RELEASE"`（用服务的 python3）。
- `./install` 直接从检出目录运行服务，也会在步骤 5 先给检出预编译；`git pull` 后重跑 `./install` 刷新。

### 手工部署的步骤

`deploy_bridge.sh` 做的就是第 1–4 步；第 5–8 步要自己做。

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
python3 -B -I scripts/precompile_release.py "$RELEASE"   # 用服务的 python3；失败只是启动慢一点
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
- 部署前在跑的每个任务都有一行 `adopted task runner job=#N pid=…`，任务卡的执行过程里多一条“机器人 HH:MM 重启过，任务未中断。”，“输出”行马上多一项“重启 HH:MM 没打断”，结束后的答案卡和回带卡也有。
- 在 Bridge 的实际环境里发一次真实模型请求，例如在测试话题里跑一个只读小任务。doctor 不探测模型，登录终端里能调用不代表服务环境里也能。

**5. 保留旧 release。** 任务进程从头到尾用启动它的 release，`pgrep -af covey_runner.py` 的命令行里能看到路径。旧 release 要保留到用它的任务进程都结束；Manager 正在用的 release 也不能删。

**6. 需要时升级 Manager。** 部署只换 Bridge。升级 Manager 时先用新 release 的安装器重写开机配置（不加 `--activate`），再只结束 Manager，由开机守护按新配置拉起：

```bash
# 其他托管方式由扩展的 service_hosting 钩子提供（见扩展自己的 README）：同样用新 release 的安装器重写配置，再结束 manager.json 记录的 Manager

# systemd 模式：用要运行 Manager 的 Python 运行安装器，再重启单元
python3 -B "$RELEASE/bin/covey_service_install.py" --root "$SERVICE_ROOT" --mode systemd
systemctl --user restart covey-manager.service   # 改名前装的机器上单元名是 lark-bridge-manager.service
```

- 新 Manager 按进程身份接管存活的 Bridge，不重启 Bridge，也不打断任务进程。扩展提供的开机守护每一轮都重新读取 `boot.json`；systemd 单元是 `KillMode=process`，重启单元只结束 Manager。安装器每次都把回执重置为 `reboot_verified: false`。
- 安装器打印的回执里 `active_pid1_hook` 总是 `false`，不说明守护的状态。以 doctor 报告的 `boot.active_pid1_hook` 和 `boot.watchdog` 为准：刚结束 Manager 时可能先报 `false`，守护拉起新 Manager 后转为 `true`。
- 没装开机自启、或扩展的守护没在运行（doctor 的 `boot.watchdog` 为空）时，结束 Manager 后由下一次 `covey_service.py start`、`restart` 或 `launch` 用执行它的 release 拉起。
- Manager 的日志行（`service <id 前 8 位> …`）是 Manager 的代码写的，升级 Manager 后才有新版本的写法。

托管方式本身见[运维 · 服务托管](operations.md#服务托管)。

**7. 更新 Skills。** 运行 `./scripts/install_skills.sh`，它把本仓库的 Skills 同步到 `${CODEX_HOME:-~/.codex}/skills` 和 `${CLAUDE_HOME:-~/.claude}/skills`，同名目录整个替换。现有两个：`harness-bridge-onboarding`（带新人装机器人）和 `change-walkthrough`（把一批大改动写成一页走读，给同事讲清改了什么、怎么看、怎么验证、从哪接上）。安装 Skills 不会重启机器人。

**8. 需要时更新内存看门狗。** 部署不会更新看门狗的本地副本；要用新代码时从新 release 执行一次 `bin/covey_memguard.py enable`（见[运维 · 内存看门狗](operations.md#内存看门狗)）。
